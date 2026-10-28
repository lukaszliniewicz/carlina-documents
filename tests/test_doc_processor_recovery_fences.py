"""Independent-session interleavings at frozen passive worker write boundaries."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from content_storage_support import bind_content_policy
from flask import Flask
from sqlalchemy.exc import OperationalError

from iarf_assistant.addons.doc_processor import execution_recovery, worker
from iarf_assistant.addons.doc_processor.models import ProcessedDocument
from iarf_assistant.core.encryption import EncryptionService, InstallationKeyStore
from iarf_assistant.core.mcp_completion import (
    PassiveAttemptSuperseded,
    passive_execution,
)
from iarf_assistant.core.mcp_jobs import create_execution
from iarf_assistant.core.task_queue import JobRetryLaterError
from iarf_assistant.extensions import db
from iarf_assistant.models import McpExecution, User


@pytest.fixture
def run(tmp_path):
    app = Flask(__name__, instance_path=str(tmp_path / "instance"))
    bind_content_policy(app)
    uploads = tmp_path / "uploads"
    app.config.update(
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{tmp_path / 'fences.db'}",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        CARLINA_UPLOADS_DIR=str(uploads),
        TESTING=True,
    )
    db.init_app(app)
    app.extensions["carlina_encryption"] = EncryptionService(
        InstallationKeyStore.ephemeral()
    )
    source = uploads / "1" / "source.pdf"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"%PDF synthetic source for stub parser")
    with app.app_context():
        db.create_all()
        db.session.add(User(id=1, email="fence@example.test", password_hash="test"))
        db.session.commit()
        settings = {
            "engine_id": "llm_vision",
            "mcp_execution_mode": "passive",
            "active_job_id": "old",
        }
        doc = ProcessedDocument(
            id="source",
            user_id=1,
            filename="source.pdf",
            file_path=str(source),
            status="processing",
            settings=settings,
        )
        db.session.add(doc)
        db.session.flush()
        execution = create_execution(
            1,
            "doc_processor",
            "document",
            doc.id,
            {
                "executor_profile": {
                    "schema_version": "carlina.external-executor.v1",
                    "max_input_tokens": 32000,
                    "max_output_tokens": 4000,
                    "input_modalities": ["text", "image"],
                    "supports_tools": True,
                    "supports_structured_output": True,
                    "supports_reasoning": True,
                    "user_confirmed": True,
                },
                "recovery": execution_recovery.freeze_recovery_contract(
                    doc, source, settings
                ),
            },
            mode="passive",
            queue_job_id="old",
        )
        execution_id = execution.id
        doc.settings = {**settings, "mcp_execution_id": execution_id}
        db.session.commit()
        yield SimpleNamespace(
            app=app,
            execution_id=execution_id,
            document=doc,
            payload={
                "document_id": doc.id,
                "user_id": 1,
                "engine_id": "llm_vision",
                "mcp_execution_id": execution_id,
                "mcp_execution_mode": "passive",
            },
        )
        db.session.remove()
        db.drop_all()
        db.engine.dispose()


def _replace(run, *, newer=False):
    def change():
        with run.app.app_context():
            doc = db.session.get(ProcessedDocument, "source")
            execution = db.session.get(McpExecution, run.execution_id)
            if newer:
                doc.settings = {
                    **doc.settings,
                    "active_job_id": "new",
                    "progress_msg": "New attempt",
                }
                execution.queue_job_id = "new"
            else:
                doc.status = "cancelled"
                doc.settings = {"sentinel": "cancellation won"}
                execution.status = "cancelled"
            db.session.commit()

    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(change).result(timeout=5)


def _assert_cancelled():
    db.session.expire_all()
    doc = db.session.get(ProcessedDocument, "source")
    assert doc.status == "cancelled"
    assert doc.settings == {"sentinel": "cancellation won"}
    assert not doc.output_html
    assert doc.metadata_record is None


def test_cancel_between_source_validation_and_initial_commit_wins(run, monkeypatch):
    original = execution_recovery.validate_delivery

    def validate(document, payload, job_id, source):
        valid = original(document, payload, job_id, source)
        if source is not None:
            _replace(run)
        return valid

    monkeypatch.setattr(execution_recovery, "validate_delivery", validate)
    parser = Mock(side_effect=AssertionError("stale delivery must not parse"))
    monkeypatch.setattr(worker, "get_parser", parser)
    assert worker.process_document(run.payload, Mock(), "old")["superseded"]
    parser.assert_not_called()
    _assert_cancelled()


def test_old_progress_callback_cannot_overwrite_new_attempt(run):
    with passive_execution(
        execution_id=run.execution_id,
        owner_user_id=1,
        addon_id="doc_processor",
        resource_kind="document",
        resource_id="source",
        queue_job_id="old",
    ):
        callback = worker._make_document_progress_callback(run.document, Mock(), "old")
        _replace(run, newer=True)
        with pytest.raises(PassiveAttemptSuperseded):
            callback(88, "Stale progress")
    db.session.expire_all()
    assert run.document.settings["active_job_id"] == "new"
    assert run.document.settings["progress_msg"] == "New attempt"


@pytest.mark.parametrize(
    "status", ["WAITING_EXTERNAL", "PAUSED_IMAGE_REVIEW", "PARTIAL_ERROR"]
)
def test_cancel_before_parser_checkpoint_cannot_publish_metadata(
    run, monkeypatch, status
):
    def parse(**_kwargs):
        _replace(run)
        return status, {
            "html_parts": ["stale output"],
            "external_pending": {"work_item_id": "old-work"},
        }

    parser = SimpleNamespace(
        process=parse,
        metrics={},
        settings={
            **run.document.settings,
            "document_metadata": {"title": "Must never be published"},
        },
    )
    monkeypatch.setattr(worker, "LLMService", lambda **_kwargs: None)
    monkeypatch.setattr(worker, "get_parser", lambda *_args, **_kwargs: parser)
    assert worker.process_document(run.payload, Mock(), "old")["superseded"]
    _assert_cancelled()


def test_database_busy_during_initial_delivery_check_retries(run, monkeypatch):
    def busy(*_args):
        raise OperationalError(
            None, None, sqlite3.OperationalError("database is locked")
        )

    monkeypatch.setattr(execution_recovery, "validate_delivery", busy)
    with pytest.raises(JobRetryLaterError):
        worker.process_document(run.payload, Mock(), "old")
    db.session.expire_all()
    assert run.document.status == "processing"
    assert db.session.get(McpExecution, run.execution_id).status == "queued"
