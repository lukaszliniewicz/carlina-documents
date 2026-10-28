import json
import runpy
import sys
import threading
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from content_storage_support import bind_content_policy
from flask import Flask

from iarf_assistant.addons.doc_processor import worker
from iarf_assistant.addons.doc_processor.models import (
    DocumentProject,
    ProcessedDocument,
    ProjectRun,
    ProjectRunDocument,
)
from iarf_assistant.core.encryption import EncryptionService, InstallationKeyStore
from iarf_assistant.core.mcp_jobs import (
    MCP_REQUEST_TOMBSTONE,
    McpWorkError,
    get_or_create_work,
)
from iarf_assistant.extensions import db
from iarf_assistant.models import McpExecution, McpWorkItem, User


@pytest.fixture(autouse=True)
def worker_app_context(tmp_path):
    app = Flask(__name__, root_path=str(tmp_path))
    bind_content_policy(app)
    for filename in ("example.docx", "example.pdf"):
        source = tmp_path / "uploads" / "1" / filename
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"source")
    with app.app_context():
        yield app


@pytest.fixture
def mcp_worker_app(tmp_path):
    app = Flask(__name__, root_path=str(tmp_path / "mcp-installed"))
    bind_content_policy(app)
    app.config.update(
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{tmp_path / 'mcp-worker.db'}",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        CARLINA_UPLOADS_DIR=str(tmp_path / "mcp-uploads"),
        TESTING=True,
    )
    db.init_app(app)
    app.extensions["carlina_encryption"] = EncryptionService(
        InstallationKeyStore.ephemeral()
    )
    with app.app_context():
        db.create_all()
        db.session.add_all(
            [
                User(id=1, email="worker-mcp@example.test", password_hash="test"),
                User(id=2, email="worker-mcp-other@example.test", password_hash="test"),
            ]
        )
        db.session.commit()
        yield app
        db.session.remove()
        db.drop_all()
        db.engine.dispose()


def _mcp_document_run(
    document_id,
    execution_id,
    *,
    status="processing",
    settings=None,
    execution_status="running",
    execution_overrides=None,
):
    document = ProcessedDocument(
        id=document_id,
        user_id=1,
        filename=f"{document_id}.pdf",
        file_path=f"/uploads/1/{document_id}.pdf",
        status=status,
        settings=dict(
            settings
            if settings is not None
            else {
                "active_job_id": f"{execution_id}-job",
                "mcp_execution_id": execution_id,
                "mcp_execution_mode": "passive",
            }
        ),
    )
    execution_values = {
        "id": execution_id,
        "owner_user_id": 1,
        "addon_id": "doc_processor",
        "resource_kind": "document",
        "resource_id": document.id,
        "mode": "passive",
        "status": execution_status,
        "request_json": {"document_id": document.id},
    }
    execution_values.update(execution_overrides or {})
    execution = McpExecution(**execution_values)
    db.session.add_all([document, execution])
    db.session.commit()
    return document, execution


def _extraction_issue_report():
    return {
        "key": "name-1",
        "kind": "name_check",
        "excerpt": "Łódź",
        "reason": "The scan makes the final character uncertain.",
        "suggested_readings": ["Łódź"],
        "references": [],
    }


def test_direct_native_parser_keeps_source_html_unchanged():
    source_html = "<table><tr><td><span class='docx-style'>Kept structure</span></td></tr></table>"
    document = SimpleNamespace(
        file_path="/uploads/1/example.docx",
        user_id=1,
        settings={},
    )
    parser = MagicMock()
    parser.process.return_value = ("COMPLETED", source_html)
    parser.metrics = {}

    with patch.object(worker, "get_parser", return_value=parser) as get_parser:
        with patch.object(worker, "LLMService"):
            with patch.object(worker, "_sync_document_metadata"):
                with patch.object(worker, "flag_modified"):
                    with patch.object(worker.db.session, "commit"):
                        with patch(
                            "iarf_assistant.addons.doc_processor.parsers.pdf_utils.convert_html_to_clean_markdown",
                            return_value="Kept structure",
                        ):
                            result = worker._process_with_native_parser(document)

    assert result["html"] == source_html
    assert result["markdown"] == "Kept structure"
    expected_source = Path(worker.app.root_path) / "uploads" / "1" / "example.docx"
    assert Path(get_parser.call_args.args[0]) == expected_source.resolve()


def test_passive_parser_settings_replace_managed_models_with_neutral_roles():
    with patch(
        "iarf_assistant.core.mcp_completion.passive_execution_context",
        return_value=SimpleNamespace(),
    ):
        settings = worker._parser_settings(
            {
                "parsing_model": "configured/provider-model",
                "metadata_model": "configured/metadata-model",
                "reasoning_snapshot": {"managed": "only"},
            }
        )

    assert settings["parsing_model"] == "carlina.passive.document"
    assert settings["metadata_model"] == "carlina.passive.document.metadata"
    assert "reasoning_snapshot" not in settings


def test_native_worker_uses_configured_upload_root_when_package_root_is_split(tmp_path):
    package_root = tmp_path / "installed-package"
    uploads_root = tmp_path / "live-data" / "uploads"
    package_root.mkdir()
    source = uploads_root / "1" / "example.txt"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("Document title\nFirst paragraph", encoding="utf-8")
    app = Flask(__name__, root_path=str(package_root))
    bind_content_policy(app)
    app.config["CARLINA_UPLOADS_DIR"] = str(uploads_root)

    document = SimpleNamespace(
        id="doc-split-root",
        file_path="/uploads/1/example.txt",
        user_id=1,
        settings={},
    )
    parser = MagicMock()
    parser.process.return_value = ("COMPLETED", "<p>Parsed from live data</p>")
    parser.metrics = {}

    with app.app_context(), patch.object(worker, "get_parser", return_value=parser) as get_parser:
        with patch.object(worker, "LLMService"), patch.object(worker, "_sync_document_metadata"), patch.object(
            worker, "flag_modified"
        ), patch.object(worker.db.session, "commit"), patch(
            "iarf_assistant.addons.doc_processor.parsers.pdf_utils.convert_html_to_clean_markdown",
            return_value="Parsed from live data",
        ):
            result = worker._process_with_native_parser(document)

    assert result["markdown"] == "Parsed from live data"
    assert Path(get_parser.call_args.args[0]) == source.resolve()


def test_document_source_path_requires_a_file_and_uses_path_free_error(tmp_path):
    app = Flask(__name__, root_path=str(tmp_path / "installed"))
    bind_content_policy(app)
    app.config["CARLINA_UPLOADS_DIR"] = str(tmp_path / "uploads")
    document = SimpleNamespace(id="doc-missing", file_path="/uploads/1/missing.txt")

    with app.app_context(), pytest.raises(FileNotFoundError, match=r"^Source file missing\.$"):
        worker._document_source_path(document)


def test_process_worker_missing_source_synchronizes_project_entry(tmp_path):
    app = Flask(__name__, root_path=str(tmp_path / "installed"))
    bind_content_policy(app)
    app.config.update(
        SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        CARLINA_UPLOADS_DIR=str(tmp_path / "live-uploads"),
        TESTING=True,
    )
    db.init_app(app)

    class Queue:
        def __init__(self):
            self.enqueued = []

        def enqueue(self, task_name, payload):
            self.enqueued.append((task_name, payload))
            return "job-next"

    with app.app_context():
        db.create_all()
        db.session.add(User(id=1, email="worker@example.test", password_hash="test"))
        document = ProcessedDocument(
            user_id=1,
            filename="missing.txt",
            file_path="/uploads/missing.txt",
            status="processing",
            settings={"active_job_id": "job-stale", "active_project_run_document_id": "entry-1"},
        )
        project = DocumentProject(user_id=1, name="Worker race")
        run = ProjectRun(project=project, user_id=1, status="running")
        entry = ProjectRunDocument(
            id="entry-1",
            run=run,
            document=document,
            position=0,
            status="processing",
            job_id="job-stale",
        )
        db.session.add_all([document, project, run, entry])
        db.session.commit()
        queue = Queue()

        with pytest.raises(FileNotFoundError, match=r"^Source file missing\.$"):
            worker.process_document(
                {"document_id": document.id, "project_run_document_id": entry.id},
                queue,
                "job-stale",
            )

        db.session.expire_all()
        refreshed_entry = db.session.get(ProjectRunDocument, entry.id)
        refreshed_document = db.session.get(ProcessedDocument, document.id)
        assert refreshed_entry.status == "error"
        assert refreshed_entry.error_message == "Source file missing."
        assert refreshed_entry.job_id is None
        assert refreshed_document.status == "error"
        assert refreshed_document.error_message == "Source file missing."
        assert "active_job_id" not in refreshed_document.settings
        assert "active_project_run_document_id" not in refreshed_document.settings
        assert queue.enqueued == []

        db.session.remove()
        db.drop_all()
        db.engine.dispose()


def test_cancelled_document_fences_late_worker_output(tmp_path):
    application = Flask(__name__, root_path=str(tmp_path / "installed"))
    bind_content_policy(application)
    upload_root = tmp_path / "uploads"
    source = upload_root / "1" / "race.pdf"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"pdf")
    application.config.update(
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{tmp_path / 'worker-race.db'}",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        CARLINA_UPLOADS_DIR=str(upload_root),
        TESTING=True,
    )
    db.init_app(application)
    application.extensions["carlina_encryption"] = EncryptionService(InstallationKeyStore.ephemeral())

    with application.app_context():
        db.create_all()
        db.session.add(User(id=1, email="race@example.test", password_hash="test"))
        document = ProcessedDocument(
            user_id=1,
            filename="race.pdf",
            file_path="/uploads/1/race.pdf",
            status="processing",
            settings={
                "engine_id": "llm_vision",
                "active_job_id": "job-race",
                "mcp_execution_id": "exec-race",
                "mcp_execution_mode": "passive",
            },
        )
        db.session.add(document)
        db.session.commit()
        document_id = document.id

        db.session.add(McpExecution(
            id="exec-race", owner_user_id=1, addon_id="doc_processor",
            resource_kind="document", resource_id=document_id,
            mode="passive", status="running", queue_job_id="job-race", request_json={},
        ))
        db.session.commit()

        def cancel_before_result_persistence(doc, _queue, _job_id):
            doc.status = "cancelled"
            settings = dict(doc.settings or {})
            for key in (
                "active_job_id",
                "mcp_execution_id",
                "mcp_execution_mode",
            ):
                settings.pop(key, None)
            doc.settings = settings
            db.session.commit()
            return {
                "html": "<main_content><p>Must not publish</p></main_content>",
                "resolved_engine": "llm_vision",
            }

        queue = MagicMock()
        with (
            patch.object(
                worker,
                "_passive_execution_context",
                return_value=nullcontext(),
            ),
            patch.object(
                worker,
                "_process_with_native_parser",
                side_effect=cancel_before_result_persistence,
            ),
            patch.object(worker, "_store_semantic_output") as store_output,
            patch.object(worker, "sync_processed_document_source") as sync_source,
        ):
            result = worker.process_document(
                {
                    "document_id": document_id,
                    "user_id": 1,
                    "mcp_execution_id": "exec-race",
                    "mcp_execution_mode": "passive",
                    "engine_id": "llm_vision",
                },
                queue,
                "job-race",
            )

        db.session.expire_all()
        stored = db.session.get(ProcessedDocument, document_id)
        assert result["superseded"] is True
        assert result["state"] == "cancelled"
        assert stored.status == "cancelled"
        assert stored.output_html is None
        store_output.assert_not_called()
        sync_source.assert_not_called()
        db.session.remove()
        db.drop_all()
        db.engine.dispose()


def test_terminal_document_binding_cleanup_preserves_unrelated_settings():
    document = SimpleNamespace(
        status="completed",
        settings={
            "mcp_execution_id": "c13a15c1-9f4c-4d49-a3ac-bb73f8061a6a",
            "mcp_execution_mode": "passive",
            "engine_id": "native",
        }
    )

    with patch.object(worker, "flag_modified") as modified:
        worker._clear_mcp_execution_binding(document)

    assert document.settings == {
        "engine_id": "native",
        "mcp_last_execution_id": "c13a15c1-9f4c-4d49-a3ac-bb73f8061a6a",
    }
    modified.assert_called_once_with(document, "settings")


def test_terminal_document_binding_transitions_matching_execution_and_fences_work(
    mcp_worker_app,
):
    with mcp_worker_app.app_context():
        document, execution = _mcp_document_run(
            "terminal-binding-document",
            "terminal-binding-execution",
            status="completed",
            settings={
                "mcp_execution_id": "terminal-binding-execution",
                "mcp_execution_mode": "passive",
            },
        )
        work = get_or_create_work(
            execution.id, {"prompt": "redact this pending work"}
        )
        work_item = db.session.get(McpWorkItem, work.work_item_id)
        assert work_item is not None
        assert work_item.state == "pending"
        db.session.commit()

        worker._clear_mcp_execution_binding(document)
        assert document.settings == {
            "mcp_last_execution_id": "terminal-binding-execution",
        }
        assert db.session.get(McpExecution, execution.id).status == "completed"
        redacted = db.session.get(McpWorkItem, work.work_item_id)
        assert redacted.state == "cancelled"
        assert redacted.request_json == MCP_REQUEST_TOMBSTONE
        assert redacted.request_pruned_at is not None
        assert redacted.lease_token is None
        db.session.commit()


def test_fenced_document_error_persists_latest_preparation_diagnostics_and_checkpoint(
    mcp_worker_app,
):
    with mcp_worker_app.app_context():
        document, execution = _mcp_document_run(
            "metadata-failure-document",
            "metadata-execution",
            settings={
                "active_job_id": "metadata-job",
                "mcp_execution_id": "metadata-execution",
                "mcp_execution_mode": "passive",
                "pdf_state": {"html_parts": ["<main_content>saved</main_content>", None]},
                "metrics": {"existing": True},
            },
        )

        error = RuntimeError("metadata preparation failed")
        error.preparation_diagnostics = [
            {"stage": f"stage-{index}", "status": "failed"}
            for index in range(15)
        ]
        with patch.object(worker, "mark_processed_document_source_failed"):
            assert worker._mark_document_error(
                document.id,
                error,
                payload={"mcp_execution_id": execution.id},
                job_id="metadata-job",
            ) is True

        db.session.expire_all()
        stored = db.session.get(ProcessedDocument, document.id)
        assert stored.status == "error"
        assert stored.settings["pdf_state"]["html_parts"][1] is None
        assert stored.settings["metrics"]["metadata_preparation"] == [
            {"stage": f"stage-{index}", "status": "failed"}
            for index in range(3, 15)
        ]
        assert "active_job_id" not in stored.settings
        assert db.session.get(McpExecution, execution.id).status == "failed"


def test_stale_fenced_document_error_leaves_newer_run_and_execution_unchanged(
    mcp_worker_app,
):
    with mcp_worker_app.app_context():
        document, execution = _mcp_document_run(
            "stale-failure-document",
            "new-execution",
            settings={
                "active_job_id": "new-job",
                "mcp_execution_id": "new-execution",
                "mcp_execution_mode": "passive",
            },
        )

        with patch.object(worker, "mark_processed_document_source_failed") as mark_failed:
            assert worker._mark_document_error(
                document.id,
                RuntimeError("late old worker failure"),
                payload={"mcp_execution_id": "old-execution"},
                job_id="old-job",
            ) is False
            mark_failed.assert_not_called()

        db.session.expire_all()
        stored = db.session.get(ProcessedDocument, document.id)
        assert stored.status == "processing"
        assert stored.settings["active_job_id"] == "new-job"
        assert stored.settings["mcp_execution_id"] == "new-execution"
        assert db.session.get(McpExecution, execution.id).status == "running"


def test_terminal_error_transition_and_document_failure_roll_back_together_then_retry(
    mcp_worker_app,
):
    with mcp_worker_app.app_context():
        document, execution = _mcp_document_run(
            "rollback-document",
            "rollback-execution",
            settings={
                "active_job_id": "rollback-job",
                "mcp_execution_id": "rollback-execution",
                "mcp_execution_mode": "passive",
                "pdf_state": {"html_parts": ["<main_content>saved</main_content>", None]},
            },
        )
        work = get_or_create_work(execution.id, {"prompt": "rollback me"})
        db.session.commit()

        with patch.object(
            worker,
            "mark_processed_document_source_failed",
            side_effect=RuntimeError("source failure after transition"),
        ), pytest.raises(RuntimeError, match="source failure after transition"):
            worker._mark_document_error(
                document.id,
                RuntimeError("document failure"),
                payload={"mcp_execution_id": execution.id},
                job_id="rollback-job",
            )

        db.session.expire_all()
        assert db.session.get(ProcessedDocument, document.id).status == "processing"
        assert db.session.get(McpExecution, execution.id).status == "running"
        rolled_back_work = db.session.get(McpWorkItem, work.work_item_id)
        assert rolled_back_work.state == "pending"
        assert rolled_back_work.request_json == {"prompt": "rollback me"}

        with patch.object(worker, "mark_processed_document_source_failed"):
            assert worker._mark_document_error(
                document.id,
                RuntimeError("document failure"),
                payload={"mcp_execution_id": execution.id},
                job_id="rollback-job",
            ) is True

        db.session.expire_all()
        assert db.session.get(ProcessedDocument, document.id).status == "error"
        assert db.session.get(McpExecution, execution.id).status == "failed"
        retried_work = db.session.get(McpWorkItem, work.work_item_id)
        assert retried_work.state == "cancelled"
        assert retried_work.request_json == MCP_REQUEST_TOMBSTONE


def test_process_missing_source_marks_unbound_mcp_run_before_queue_status(
    mcp_worker_app,
):
    with mcp_worker_app.app_context():
        document, execution = _mcp_document_run(
            "preflight-missing-source-document",
            "preflight-missing-source-execution",
            settings={
                "mcp_execution_id": "preflight-missing-source-execution",
                "mcp_execution_mode": "managed",
                "enqueue_claim": {"token": "preflight-claim"},
            },
            execution_overrides={"mode": "managed"},
        )
        work = get_or_create_work(execution.id, {"prompt": "missing source"})
        db.session.commit()
        queue = MagicMock()

        with (
            patch.object(worker, "_sync_project_entry") as sync_entry,
            pytest.raises(FileNotFoundError, match=r"^Source file missing\.$"),
        ):
            worker.process_document(
                {
                    "document_id": document.id,
                    "mcp_execution_id": execution.id,
                    "mcp_execution_mode": "managed",
                },
                queue,
                "preflight-job",
            )

        queue.set_status.assert_not_called()
        sync_entry.assert_called_once()
        db.session.expire_all()
        stored = db.session.get(ProcessedDocument, document.id)
        assert stored.status == "error"
        assert stored.error_message == "Source file missing."
        assert "mcp_execution_id" not in stored.settings
        assert "enqueue_claim" not in stored.settings
        assert stored.settings["mcp_last_execution_id"] == execution.id
        assert db.session.get(McpExecution, execution.id).status == "failed"
        redacted_work = db.session.get(McpWorkItem, work.work_item_id)
        assert redacted_work.state == "cancelled"
        assert redacted_work.request_json == MCP_REQUEST_TOMBSTONE


def test_resume_invalid_review_state_marks_unbound_managed_run_before_parser(
    mcp_worker_app,
):
    with mcp_worker_app.app_context():
        document, execution = _mcp_document_run(
            "preflight-review-document",
            "preflight-review-execution",
            settings={
                "mcp_execution_id": "preflight-review-execution",
                "mcp_execution_mode": "managed",
                "enqueue_claim": {"token": "review-claim"},
                "pdf_state": {
                    "phase": "PAUSED_IMAGE_REVIEW",
                    "included_pages": [0, 1],
                    "html_parts": ["<main_content>page one</main_content>", None],
                },
            },
            execution_overrides={"mode": "managed"},
        )
        queue = MagicMock()

        with (
            patch.object(worker, "_sync_project_entry") as sync_entry,
            patch.object(worker, "get_parser") as get_parser,
            pytest.raises(ValueError, match="selected pages are missing"),
        ):
            worker.resume_pdf_parsing(
                {
                    "document_id": document.id,
                    "mcp_execution_id": execution.id,
                    "mcp_execution_mode": "managed",
                },
                queue,
                "review-preflight-job",
            )

        queue.set_status.assert_not_called()
        sync_entry.assert_called_once()
        get_parser.assert_not_called()
        db.session.expire_all()
        stored = db.session.get(ProcessedDocument, document.id)
        assert stored.status == "error"
        assert "enqueue_claim" not in stored.settings
        assert stored.settings["mcp_last_execution_id"] == execution.id
        assert db.session.get(McpExecution, execution.id).status == "failed"


def test_unbound_preflight_snapshot_rejects_newer_revision_in_independent_session(
    mcp_worker_app,
):
    old_context = mcp_worker_app.app_context()
    old_context.push()
    try:
        document, execution = _mcp_document_run(
            "preflight-revision-document",
            "preflight-revision-execution",
            settings={
                "mcp_execution_id": "preflight-revision-execution",
                "mcp_execution_mode": "managed",
                "revision": "old",
            },
            execution_overrides={"mode": "managed"},
        )
        old_document = db.session.get(ProcessedDocument, document.id)
        snapshot = worker._document_preflight_snapshot(old_document)

        newer_context = mcp_worker_app.app_context()
        newer_context.push()
        try:
            new_document = db.session.get(ProcessedDocument, document.id)
            new_document.settings = {
                **dict(new_document.settings or {}),
                "revision": "new",
            }
            db.session.commit()
        finally:
            newer_context.pop()

        assert worker._mark_document_error(
            old_document.id,
            RuntimeError("stale preflight failure"),
            payload={"mcp_execution_id": execution.id},
            job_id="old-preflight-job",
            preflight_snapshot=snapshot,
        ) is False

        db.session.expire_all()
        stored = db.session.get(ProcessedDocument, document.id)
        assert stored.status == "processing"
        assert stored.settings["revision"] == "new"
        assert "active_job_id" not in stored.settings
        assert db.session.get(McpExecution, execution.id).status == "running"
    finally:
        old_context.pop()


def test_preflight_snapshot_failure_accepts_matching_job_marker_arriving_late(
    mcp_worker_app,
):
    old_context = mcp_worker_app.app_context()
    old_context.push()
    try:
        document, execution = _mcp_document_run(
            "preflight-marker-document",
            "preflight-marker-execution",
            settings={
                "mcp_execution_id": "preflight-marker-execution",
                "mcp_execution_mode": "managed",
                "enqueue_claim": {"token": "marker-claim"},
            },
            execution_overrides={"mode": "managed"},
        )
        old_document = db.session.get(ProcessedDocument, document.id)
        snapshot = worker._document_preflight_snapshot(old_document)

        newer_context = mcp_worker_app.app_context()
        newer_context.push()
        try:
            new_document = db.session.get(ProcessedDocument, document.id)
            new_document.settings = {
                **dict(new_document.settings or {}),
                "active_job_id": "marker-job",
            }
            db.session.commit()
        finally:
            newer_context.pop()

        assert worker._mark_document_error(
            old_document.id,
            RuntimeError("marker-arrival failure"),
            payload={"mcp_execution_id": execution.id},
            job_id="marker-job",
            preflight_snapshot=snapshot,
        ) is True

        db.session.expire_all()
        stored = db.session.get(ProcessedDocument, document.id)
        assert stored.status == "error"
        assert "active_job_id" not in stored.settings
        assert "enqueue_claim" not in stored.settings
        assert db.session.get(McpExecution, execution.id).status == "failed"
    finally:
        old_context.pop()


def test_stale_worker_error_is_fenced_after_new_binding_commits_in_independent_session(
    mcp_worker_app,
):
    old_context = mcp_worker_app.app_context()
    old_context.push()
    try:
        old_session = db.session()
        document, old_execution = _mcp_document_run(
            "independent-stale-document",
            "old-execution",
            settings={
                "active_job_id": "old-job",
                "mcp_execution_id": "old-execution",
                "mcp_execution_mode": "passive",
            },
        )
        old_document = db.session.get(ProcessedDocument, document.id)
        old_work = get_or_create_work(old_execution.id, {"prompt": "old work"})
        db.session.commit()

        newer_context = mcp_worker_app.app_context()
        newer_context.push()
        try:
            new_session = db.session()
            assert new_session is not old_session
            new_document = db.session.get(ProcessedDocument, document.id)
            new_execution = McpExecution(
                id="new-execution",
                owner_user_id=1,
                addon_id="doc_processor",
                resource_kind="document",
                resource_id=document.id,
                mode="passive",
                status="running",
                request_json={"document_id": document.id, "run": "new"},
            )
            new_document.settings = {
                **dict(new_document.settings or {}),
                "active_job_id": "new-job",
                "mcp_execution_id": new_execution.id,
            }
            db.session.add(new_execution)
            db.session.commit()
            new_work = get_or_create_work(new_execution.id, {"prompt": "new work"})
            db.session.commit()
        finally:
            newer_context.pop()

        assert old_document.settings["mcp_execution_id"] == "old-execution"
        assert old_work.work_item_id != new_work.work_item_id
        assert worker._mark_document_error(
            old_document.id,
            RuntimeError("late old worker failure"),
            payload={"mcp_execution_id": "old-execution"},
            job_id="old-job",
        ) is False

        db.session.expire_all()
        stored = db.session.get(ProcessedDocument, document.id)
        assert stored.status == "processing"
        assert stored.settings["active_job_id"] == "new-job"
        assert stored.settings["mcp_execution_id"] == "new-execution"
        assert db.session.get(McpExecution, "old-execution").status == "running"
        assert db.session.get(McpExecution, "new-execution").status == "running"
        assert db.session.get(McpWorkItem, old_work.work_item_id).state == "pending"
        assert db.session.get(McpWorkItem, new_work.work_item_id).state == "pending"
    finally:
        old_context.pop()


@pytest.mark.parametrize(
    ("field", "value"),
    [("owner_user_id", 2), ("resource_id", "different-document")],
)
def test_mismatched_execution_binding_rolls_back_document_error(
    mcp_worker_app, field, value
):
    with mcp_worker_app.app_context():
        document, execution = _mcp_document_run(
            "mismatched-binding-document",
            "mismatch-execution",
            settings={
                "active_job_id": "mismatch-job",
                "mcp_execution_id": "mismatch-execution",
            },
            execution_overrides={field: value},
        )

        with pytest.raises(RuntimeError, match="MCP execution binding"):
            worker._mark_document_error(
                document.id,
                RuntimeError("document failure"),
                payload={"mcp_execution_id": execution.id},
                job_id="mismatch-job",
            )

        db.session.expire_all()
        assert db.session.get(ProcessedDocument, document.id).status == "processing"
        assert db.session.get(McpExecution, execution.id).status == "running"


def test_conflicting_terminal_execution_transition_rolls_back_document_error(
    mcp_worker_app,
):
    with mcp_worker_app.app_context():
        document, execution = _mcp_document_run(
            "conflicting-terminal-document",
            "conflict-execution",
            settings={
                "active_job_id": "conflict-job",
                "mcp_execution_id": "conflict-execution",
            },
            execution_status="completed",
        )

        with pytest.raises(McpWorkError) as raised:
            worker._mark_document_error(
                document.id,
                RuntimeError("document failure"),
                payload={"mcp_execution_id": execution.id},
                job_id="conflict-job",
            )
        assert raised.value.code == "execution_terminal"

        db.session.expire_all()
        assert db.session.get(ProcessedDocument, document.id).status == "processing"
        assert db.session.get(McpExecution, execution.id).status == "completed"


def test_passive_helpers_are_import_safe_when_worker_runs_as_script():
    runtime_module = sys.modules[worker.run_worker_node.__module__]
    with patch.object(runtime_module, "run_worker_node"):
        loaded = runpy.run_path(worker.__file__, run_name="__main__")

    assert loaded["__package__"] == worker.__package__
    assert loaded["_reasoning_run_metadata"]({}) is None
    assert loaded["_parser_settings"]({}) == {}


def test_native_worker_reads_txt_from_stored_upload_web_path(tmp_path, monkeypatch):
    source = tmp_path / "uploads" / "1" / "example.txt"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("Document title\nFirst paragraph", encoding="utf-8")
    document = SimpleNamespace(
        file_path="/uploads/1/example.txt",
        user_id=1,
        settings={},
    )

    with patch.object(worker, "LLMService", return_value=None), \
         patch.object(worker, "_sync_document_metadata"), \
         patch.object(worker, "flag_modified"), \
         patch.object(worker.db.session, "commit"):
        result = worker._process_with_native_parser(document)

    assert "<h1>Document title</h1>" in result["html"]
    assert "First paragraph" in result["markdown"]


def test_native_engine_preserves_direct_parser_html():
    document = SimpleNamespace(
        file_path="/uploads/1/example.docx",
        user_id=1,
        settings={"engine_options": {}},
    )
    client = MagicMock()
    client.select_engine.return_value = "native"
    queue = MagicMock()
    direct_result = {
        "paused": False,
        "partial_error": False,
        "html": "<p><span class='kept'>Semantic HTML</span></p>",
        "markdown": "Semantic HTML",
    }

    with patch.object(worker, "_get_client", return_value=client):
        with patch.object(worker, "_process_with_native_parser", return_value=direct_result) as direct_parser:
            result = worker._process_with_external_engine(document, "auto", queue, "job-1")

    direct_parser.assert_called_once_with(document, queue, "job-1")
    client.parse_document.assert_not_called()
    assert result["html"] == direct_result["html"]
    assert result["markdown"] == direct_result["markdown"]
    assert result["resolved_engine"] == "native"


def test_direct_native_pdf_parser_receives_progress_callback():
    document = SimpleNamespace(
        file_path="/uploads/1/example.pdf",
        user_id=1,
        settings={},
    )
    parser = MagicMock()
    parser.process.return_value = ("COMPLETED", "<p>Done</p>")
    parser.metrics = {}
    parser.settings = {"document_metadata": {"title": "Recovered metadata"}}
    queue = MagicMock()

    with patch.object(worker, "get_parser", return_value=parser), \
         patch.object(worker, "LLMService"), \
         patch.object(worker, "_sync_document_metadata"), \
         patch.object(worker, "flag_modified"), \
         patch.object(worker.db.session, "commit"), \
         patch(
             "iarf_assistant.addons.doc_processor.parsers.pdf_utils.convert_html_to_clean_markdown",
             return_value="Done",
         ):
        worker._process_with_native_parser(document, queue, "job-1")

    parser.process.assert_called_once()
    assert parser.process.call_args.kwargs["auto_mode"] is False
    assert callable(parser.process.call_args.kwargs["progress_callback"])
    assert callable(parser.process.call_args.kwargs["control_callback"])
    assert document.settings["document_metadata"]["title"] == "Recovered metadata"


def test_native_parser_returns_resumable_state_when_user_pauses():
    document = SimpleNamespace(
        file_path="/uploads/1/example.pdf",
        user_id=1,
        settings={},
    )
    parser = MagicMock()
    parser.process.return_value = (
        "PAUSED_USER",
        {
            "html_parts": ["<main_content><p>Page one</p></main_content>"],
            "png_paths": [None],
            "boundary_flags": {},
        },
    )
    parser.metrics = {}
    parser.settings = document.settings
    queue = MagicMock()
    queue.is_cancel_requested.return_value = False
    queue.get_status.return_value = {"pause_requested": True}

    with patch.object(worker, "get_parser", return_value=parser), \
         patch.object(worker, "LLMService"), \
         patch.object(worker, "_sync_document_metadata"), \
         patch.object(worker, "flag_modified"), \
         patch.object(worker.db.session, "commit"):
        result = worker._process_with_native_parser(document, queue, "job-1")

    assert result["interrupted"] is True
    assert result["control_action"] == "pause"
    assert result["pdf_state"]["html_parts"][0].startswith("<main_content>")
    assert document.settings["pdf_state"] == result["pdf_state"]


def test_passive_pdf_wait_checkpoints_page_wave_before_suspending():
    document = SimpleNamespace(
        file_path="/uploads/1/example.pdf",
        user_id=1,
        status="processing",
        settings={"active_job_id": "job-batch", "mcp_execution_id": "execution-batch"},
    )
    parser = MagicMock()
    parser.process.return_value = (
        "WAITING_EXTERNAL",
        {
            "html_parts": ["<main_content><p>Accepted</p></main_content>", None],
            "png_paths": ["page-1.png", "page-2.png"],
            "boundary_flags": {},
            "batch_boundaries": {"first": {"last_page": 1}},
            "external_pending": {
                "work_item_id": "work-page-2",
                "delay_seconds": 7,
            },
        },
    )
    parser.metrics = {"prompt_tokens": 10}
    parser.settings = document.settings

    with (
        patch.object(worker, "get_parser", return_value=parser),
        patch.object(worker, "LLMService"),
        patch.object(worker, "_sync_document_metadata"),
        patch.object(worker, "flag_modified"),
        patch.object(worker.db.session, "commit") as commit,
        pytest.raises(BaseException) as captured,
    ):
        worker._process_with_native_parser(document, MagicMock(), "job-batch")

    assert type(captured.value).__name__ == "ExternalWorkPending"
    assert captured.value.work_item_id == "work-page-2"
    assert document.settings["pdf_state"]["html_parts"][0].startswith(
        "<main_content>"
    )
    assert "external_pending" not in document.settings["pdf_state"]
    assert document.settings["pdf_state"]["execution_id"] == "execution-batch"
    assert commit.call_count >= 1


def test_extraction_issue_checkpoint_encrypts_unicode_and_binds_document_owner(
    mcp_worker_app,
):
    with mcp_worker_app.app_context():
        document, _execution = _mcp_document_run(
            "issue-checkpoint-document",
            "issue-checkpoint-execution",
            settings={"mcp_extraction_issues_policy": "issues-v1"},
        )
        issue = _extraction_issue_report()
        parser = SimpleNamespace(
            metrics_lock=threading.Lock(),
            extraction_issues_by_page={2: [issue], 1: []},
        )

        pages = worker._save_extraction_issues_checkpoint(document, parser)
        db.session.commit()

        ciphertext = document.settings["extraction_issues_checkpoint"]
        assert ciphertext.startswith("enc:")
        assert "Łódź" not in ciphertext
        assert "Łódź" not in repr(document.settings)
        decrypted = mcp_worker_app.extensions["carlina_encryption"].decrypt(ciphertext)
        assert "Łódź" in decrypted
        assert worker._load_extraction_issues_checkpoint(document) == {
            1: [],
            2: [issue],
        }
        assert pages == [
            {"page_number": 1, "issues": []},
            {"page_number": 2, "issues": [issue]},
        ]

        foreign_document = SimpleNamespace(
            id="another-document",
            user_id=document.user_id,
            settings=dict(document.settings),
        )
        with pytest.raises(
            RuntimeError,
            match=r"^Extraction issue checkpoint is unavailable or invalid\.$",
        ):
            worker._load_extraction_issues_checkpoint(foreign_document)


def test_accepted_issue_reports_survive_later_parser_error_and_error_rollback(
    mcp_worker_app,
):
    with mcp_worker_app.app_context():
        document, execution = _mcp_document_run(
            "issue-error-checkpoint-document",
            "issue-error-checkpoint-execution",
            settings={
                "active_job_id": "issue-error-job",
                "mcp_execution_id": "issue-error-checkpoint-execution",
                "mcp_execution_mode": "passive",
                "mcp_page_batch_protocol": "carlina.pdf-page-batch.v1",
                "mcp_extraction_issues_policy": "issues-v1",
            },
        )
        issue = _extraction_issue_report()
        parser = MagicMock()
        parser.settings = dict(document.settings)
        parser.metrics = {}
        parser.metrics_lock = threading.Lock()

        def fail_after_one_accepted_page(**_kwargs):
            parser.extraction_issues_by_page = {1: [issue]}
            raise RuntimeError("later parser failure")

        parser.process.side_effect = fail_after_one_accepted_page
        queue = MagicMock()
        payload = {
            "document_id": document.id,
            "user_id": document.user_id,
            "mcp_execution_id": execution.id,
            "mcp_execution_mode": "passive",
            "engine_id": "llm_vision",
        }

        with (
            patch.object(
                worker,
                "_passive_execution_context",
                return_value=nullcontext(),
            ),
            patch.object(worker, "_document_source_path", return_value=Path("/tmp/source.pdf")),
            patch.object(worker, "_wait_for_unchanged_passive_checkpoint"),
            patch.object(worker, "get_parser", return_value=parser),
            patch.object(worker, "LLMService"),
            patch.object(worker, "_sync_document_metadata"),
            patch.object(worker, "_sync_project_entry"),
            patch.object(worker, "mark_processed_document_source_failed"),
            pytest.raises(RuntimeError, match="^later parser failure$"),
        ):
            worker.process_document(payload, queue, "issue-error-job")

        db.session.expire_all()
        stored = db.session.get(ProcessedDocument, document.id)
        assert stored.status == "error"
        assert "extraction_issues_checkpoint" in stored.settings
        assert "Łódź" not in repr(stored.settings)
        envelope = json.loads(
            mcp_worker_app.extensions["carlina_encryption"].decrypt(
                stored.settings["extraction_issues_checkpoint"]
            )
        )
        assert envelope["document_id"] == stored.id
        assert envelope["owner_user_id"] == stored.user_id
        assert envelope["pages"] == [
            {"page_number": 1, "issues": [issue]},
        ]


def test_final_worker_imports_issues_after_canonical_output_and_clears_checkpoint(
    mcp_worker_app,
):
    with mcp_worker_app.app_context():
        document, _execution = _mcp_document_run(
            "issue-finalization-document",
            "issue-finalization-execution",
            settings={
                "active_job_id": "issue-finalization-job",
                "mcp_extraction_issues_policy": "issues-v1",
                "extraction_issues_checkpoint": "enc:private-ciphertext",
            },
        )
        pages = [{"page_number": 1, "issues": [_extraction_issue_report()]}]
        order = []

        def store_output(doc, _html, *, completed, image_crop_sources):
            assert completed is True
            assert image_crop_sources == {}
            doc.output_html = "<main_content><p>canonical</p></main_content><footnotes></footnotes>"
            order.append("store")

        def import_issues(doc, captured_pages):
            assert doc.output_html is not None
            assert captured_pages == pages
            order.append("import")

        with (
            patch.object(worker, "_document_source_path", return_value=Path("/tmp/source.pdf")),
            patch.object(worker, "_wait_for_unchanged_passive_checkpoint"),
            patch.object(
                worker,
                "_process_with_native_parser",
                return_value={"html": "<p>raw</p>", "extraction_issues": pages},
            ),
            patch.object(worker, "_reserve_document_result_persistence", return_value=True),
            patch.object(worker, "_store_semantic_output", side_effect=store_output),
            patch.object(worker, "_import_extraction_issues", side_effect=import_issues),
            patch.object(worker, "_clear_mcp_execution_binding"),
            patch.object(worker, "sync_processed_document_source"),
            patch.object(worker, "_sync_project_entry"),
        ):
            result = worker.process_document(
                {"document_id": document.id, "engine_id": "llm_vision"},
                MagicMock(),
                "issue-finalization-job",
            )

        db.session.expire_all()
        stored = db.session.get(ProcessedDocument, document.id)
        assert result == {"message": "Document parsed successfully."}
        assert order == ["store", "import"]
        assert stored.status == "completed"
        assert "extraction_issues_checkpoint" not in stored.settings


def test_batch_failure_retains_checkpoint_identity_and_safe_diagnostics(mcp_worker_app):
    saved_pages = ["<main_content><p>Saved</p></main_content><footnotes></footnotes>", None]
    error = worker.PageBatchValidationError({
        "batch_id": "second", "page": 2, "stage": "source_grounding",
        "reason": "Incomplete transcription.", "attempt": 3, "max_attempts": 3,
    })
    with mcp_worker_app.app_context():
        document, execution = _mcp_document_run(
            "batch-failure-document",
            "failed-execution",
            settings={
                "active_job_id": "failed-job",
                "mcp_execution_id": "failed-execution",
                "mcp_execution_mode": "passive",
                "pdf_state": {"html_parts": saved_pages},
                "metrics": {},
            },
        )

        with patch.object(worker, "mark_processed_document_source_failed"):
            assert worker._mark_document_error(
                document.id,
                error,
                payload={"mcp_execution_id": execution.id},
                job_id="failed-job",
            ) is True

        db.session.expire_all()
        stored = db.session.get(ProcessedDocument, document.id)
        assert stored.status == "error"
        assert stored.settings["mcp_last_execution_id"] == "failed-execution"
        assert "mcp_execution_id" not in stored.settings
        assert stored.settings["pdf_state"]["html_parts"] == saved_pages
        assert stored.settings["metrics"]["batch_validation_error"] == error.feedback
        assert db.session.get(McpExecution, execution.id).status == "failed"


def test_native_parser_marks_only_complete_image_review_state_for_resume():
    document = SimpleNamespace(
        file_path="/uploads/1/example.pdf",
        user_id=1,
        settings={},
        status="processing",
    )
    parser = MagicMock()
    parser.process.return_value = (
        "PAUSED_IMAGE_REVIEW",
        {
            "included_pages": [0, 1],
            "html_parts": [
                "<main_content><p>Page one</p></main_content>",
                "<main_content><p>Page two</p></main_content>",
            ],
            "png_paths": [None, None],
            "boundary_flags": {},
        },
    )
    parser.metrics = {}
    parser.settings = document.settings

    with (
        patch.object(worker, "get_parser", return_value=parser),
        patch.object(worker, "LLMService"),
        patch.object(worker, "_sync_document_metadata"),
        patch.object(worker, "flag_modified"),
        patch.object(worker.db.session, "commit"),
    ):
        result = worker._process_with_native_parser(document)

    assert result["paused"] is True
    assert document.status == "reviewing_images"
    assert document.settings["pdf_state"]["phase"] == "PAUSED_IMAGE_REVIEW"


def test_resume_pdf_worker_rejects_incomplete_review_state_before_parser_dispatch():
    document = SimpleNamespace(
        id="incomplete-review",
        status="processing",
        updated_at=None,
        settings={
            "pdf_state": {
                "phase": "PAUSED_IMAGE_REVIEW",
                "included_pages": [0, 1],
                "html_parts": ["<main_content>page one</main_content>", None],
            }
        },
    )
    queue = MagicMock()

    with (
        patch.object(worker.db.session, "get", return_value=document),
        patch.object(worker, "_mark_document_error") as mark_error,
        patch.object(worker, "_sync_project_entry") as sync_entry,
        patch.object(worker, "get_parser") as get_parser,
        pytest.raises(ValueError, match="selected pages are missing"),
    ):
        worker.resume_pdf_parsing(
            {"document_id": document.id},
            queue,
            "stale-job",
        )

    mark_error.assert_called_once()
    sync_entry.assert_called_once()
    get_parser.assert_not_called()
    assert document.settings["pdf_state"]["html_parts"][1] is None
