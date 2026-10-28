from __future__ import annotations

from datetime import timedelta
from threading import Lock
from time import sleep
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from content_storage_support import bind_content_policy
from flask import Flask
from sqlalchemy import event, select

from carlina.core.utc import utc_now_db
from iarf_assistant.addons.doc_processor import mcp_service, worker
from iarf_assistant.addons.doc_processor.models import ProcessedDocument
from iarf_assistant.addons.doc_processor.performance_metrics import (
    accumulate_render_metrics,
    safe_preparation_timings,
    timed_phase,
)
from iarf_assistant.core.encryption import EncryptionService, InstallationKeyStore
from iarf_assistant.core.task_queue import ExternalWorkPending
from iarf_assistant.extensions import db
from iarf_assistant.models import McpExecution, McpWorkItem, User


@pytest.fixture
def metrics_app(tmp_path):
    application = Flask(__name__, root_path=str(tmp_path))
    bind_content_policy(application)
    application.config.update(
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{tmp_path / 'doc-metrics.db'}",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        CARLINA_UPLOADS_DIR=str(tmp_path / "uploads"),
        SECRET_KEY="synthetic-metrics-test",
        TESTING=True,
    )
    db.init_app(application)
    application.extensions["carlina_encryption"] = EncryptionService(
        InstallationKeyStore.ephemeral()
    )
    with application.app_context():
        db.create_all()
        connection = db.engine.raw_connection()
        try:
            cursor = connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()
            connection.commit()
        finally:
            connection.close()
        db.session.add_all(
            [
                User(id=1, email="metrics-one@example.test", password_hash="test"),
                User(id=2, email="metrics-two@example.test", password_hash="test"),
            ]
        )
        db.session.commit()
        yield application
        db.session.remove()
        db.drop_all()
        db.engine.dispose()


def _document(
    document_id: str,
    *,
    owner_id: int = 1,
    status: str = "processing",
    settings: dict | None = None,
) -> ProcessedDocument:
    document = ProcessedDocument(
        id=document_id,
        user_id=owner_id,
        filename=f"{document_id}.pdf",
        file_path=f"/uploads/{owner_id}/{document_id}.pdf",
        status=status,
        settings=dict(settings or {}),
    )
    db.session.add(document)
    return document


def _parser_with_timings(timings: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        metrics={
            "preparation_timings": timings
            or {"metadata_total": {"calls": 1, "wall_seconds": 1.25}},
            "private_provider_payload": "synthetic response text",
        }
    )


def test_safe_preparation_timings_filters_phases_fields_and_values():
    raw = {
        "metadata_total": {
            "calls": 2.0,
            "wall_seconds": 1.23456789,
            "cpu_seconds": float("nan"),
            "pages": 2.25,
            "png_bytes": True,
            "completion_dispatch": "model response text",
            "negative": -1,
        },
        "not-a-phase": {"calls": 10},
        "heading_images": "private image path",
    }

    assert safe_preparation_timings(raw) == {
        "metadata_total": {"calls": 2, "wall_seconds": 1.234568}
    }
    assert safe_preparation_timings(None) == {}


def test_timed_phase_records_success_and_baseexception_without_masking():
    marker = _MarkerBaseException("original marker")

    class Timed:
        def __init__(self):
            self.metrics = {}
            self.metrics_lock = Lock()

        @timed_phase("metadata_total")
        def succeed(self):
            sleep(0.002)
            return "ok"

        @timed_phase("metadata_total")
        def fail(self):
            sleep(0.002)
            raise marker

    timed = Timed()
    assert timed.succeed() == "ok"
    with pytest.raises(_MarkerBaseException) as caught:
        timed.fail()
    assert caught.value is marker

    phase = timed.metrics["preparation_timings"]["metadata_total"]
    assert phase["calls"] == 2
    assert phase["wall_seconds"] >= 0
    assert phase["max_wall_seconds"] >= 0
    assert phase["cpu_seconds"] >= 0


def test_timed_phase_is_optional_when_metrics_are_absent_or_not_a_dict():
    class WithoutMetrics:
        @timed_phase("heading_total")
        def run(self):
            return "ok"

    class InvalidMetrics:
        metrics = None

        @timed_phase("heading_total")
        def run(self):
            return "ok"

    assert WithoutMetrics().run() == "ok"
    assert InvalidMetrics().run() == "ok"


def test_render_accumulator_merges_only_bounded_numeric_fields():
    metrics = {"preparation_timings": {"page_images": {"calls": 2}}}

    accumulate_render_metrics(
        metrics,
        "page_images",
        {
            "preparation_wall_seconds": 1.2345678,
            "pool_wait_seconds": 0.25,
            "worker_startup_seconds": 0.5,
            "worker_render_seconds_sum": 1.75,
            "worker_encode_seconds_sum": 0.25,
            "pages": 2,
            "png_bytes": 4096,
            "encoded_bytes": 1000,
            "peak_pool_rss_bytes": 8192,
            "incomplete": True,
            "source_path": "/private/source.pdf",
            "model_text": "private response",
        },
    )

    assert metrics["preparation_timings"]["page_images"] == {
        "calls": 3,
        "wall_seconds": 1.234568,
        "max_wall_seconds": 1.234568,
        "pool_wait_seconds": 0.25,
        "worker_startup_seconds": 0.5,
        "worker_render_seconds_sum": 1.75,
        "worker_encode_seconds_sum": 0.25,
        "pages": 2,
        "png_bytes": 4096,
        "encoded_bytes": 1000,
        "peak_pool_rss_bytes": 8192,
        "incomplete_chunks": 1,
    }


@pytest.mark.parametrize("pending", [True, False])
def test_parser_exception_persists_only_safe_timings_and_reraises_same_error(
    metrics_app, tmp_path, pending
):
    with metrics_app.app_context():
        document_id = f"doc-pending-{pending}"
        settings = {
            "active_job_id": "job-current",
            "mcp_execution_id": "exec-current",
            "progress": 42,
            "progress_msg": "Existing progress",
            "finalized": False,
            "document_metadata": {"title": "Retain existing title"},
            "metrics": {"existing_metric": "keep"},
        }
        _document(document_id, settings=settings)
        db.session.commit()
        with db.engine.connect() as connection:
            updated_before = connection.execute(
                select(ProcessedDocument.updated_at).where(
                    ProcessedDocument.id == document_id
                )
            ).scalar_one()

        source = tmp_path / "uploads" / "1" / f"{document_id}.pdf"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"synthetic PDF source")
        document = SimpleNamespace(
            id=document_id,
            user_id=1,
            file_path=f"/uploads/1/{document_id}.pdf",
            status="processing",
            settings=dict(settings),
        )
        error = (
            ExternalWorkPending(
                "synthetic pending work",
                delay_seconds=4.0,
                work_item_id="synthetic-work",
            )
            if pending
            else _MarkerBaseException("synthetic parser failure")
        )
        parser = MagicMock()
        parser.metrics = {
            "preparation_timings": {
                "metadata_total": {"calls": 2, "wall_seconds": 1.25},
                "unsafe_phase": {"calls": 1},
            },
            "private_provider_payload": "do not persist",
        }
        parser.process.side_effect = error

        with (
            patch.object(worker, "_reasoning_run_metadata", return_value=None),
            patch.object(worker, "_parser_settings", return_value={}),
            patch.object(worker, "_document_source_path", return_value=source),
            patch.object(worker, "get_parser", return_value=parser),
            patch.object(worker, "LLMService"),
            pytest.raises(type(error)) as caught,
        ):
            worker._process_with_native_parser(document, job_id="job-current")

        assert caught.value is error
        db.session.remove()
        stored = db.session.get(ProcessedDocument, document_id)
        assert stored.status == "processing"
        assert stored.updated_at == updated_before
        assert stored.settings["active_job_id"] == "job-current"
        assert stored.settings["mcp_execution_id"] == "exec-current"
        assert stored.settings["progress"] == 42
        assert stored.settings["progress_msg"] == "Existing progress"
        assert stored.settings["finalized"] is False
        assert stored.settings["document_metadata"] == {
            "title": "Retain existing title"
        }
        assert stored.settings["metrics"] == {
            "existing_metric": "keep",
            "preparation_timings": {
                "metadata_total": {"calls": 2, "wall_seconds": 1.25}
            },
        }


def test_pending_metrics_write_succeeds_after_parser_progress_commit(
    metrics_app, tmp_path
):
    with metrics_app.app_context():
        settings = {
            "active_job_id": "job-progress",
            "mcp_execution_id": "exec-progress",
            "metrics": {"existing_metric": "keep"},
        }
        stored_document = _document("doc-progress-commit", settings=settings)
        db.session.commit()
        document_id = stored_document.id
        document = db.session.get(ProcessedDocument, document_id)
        source = tmp_path / "uploads" / "1" / "doc-progress-commit.pdf"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"synthetic PDF source")
        error = ExternalWorkPending(
            "synthetic pending work",
            delay_seconds=4.0,
            work_item_id="synthetic-progress-work",
        )
        parser = MagicMock()
        parser.metrics = {
            "preparation_timings": {
                "metadata_total": {"calls": 1, "wall_seconds": 0.5}
            }
        }

        def commit_progress_then_suspend(**kwargs):
            kwargs["progress_callback"](55, "synthetic parser progress")
            raise error

        parser.process.side_effect = commit_progress_then_suspend
        task_queue = SimpleNamespace(
            set_status=MagicMock(),
            get_status=MagicMock(return_value={}),
        )

        with (
            patch.object(worker, "_reasoning_run_metadata", return_value=None),
            patch.object(worker, "_parser_settings", return_value={}),
            patch.object(worker, "_document_source_path", return_value=source),
            patch.object(worker, "get_parser", return_value=parser),
            patch.object(worker, "LLMService"),
            pytest.raises(ExternalWorkPending) as caught,
        ):
            worker._process_with_native_parser(
                document, task_queue=task_queue, job_id="job-progress"
            )

        assert caught.value is error
        db.session.remove()
        reloaded = db.session.get(ProcessedDocument, document_id)
        assert reloaded.status == "processing"
        assert reloaded.settings["active_job_id"] == "job-progress"
        assert reloaded.settings["mcp_execution_id"] == "exec-progress"
        assert reloaded.settings["progress"] == 55
        assert reloaded.settings["metrics"] == {
            "existing_metric": "keep",
            "preparation_timings": {
                "metadata_total": {"calls": 1, "wall_seconds": 0.5}
            },
        }


def test_metrics_update_is_fenced_and_does_not_commit_unrelated_dirty_objects(
    metrics_app,
):
    with metrics_app.app_context():
        settings = {
            "active_job_id": "job-current",
            "mcp_execution_id": "exec-current",
            "metrics": {"existing_metric": "keep"},
        }
        target = _document("doc-target", settings=settings)
        stale_job = _document(
            "doc-stale-job",
            settings={"active_job_id": "job-new", "mcp_execution_id": "exec-current"},
        )
        stale_execution = _document(
            "doc-stale-execution",
            settings={"active_job_id": "job-current", "mcp_execution_id": "exec-new"},
        )
        cancelled = _document(
            "doc-cancelled",
            status="cancelled",
            settings=settings,
        )
        wrong_owner = _document(
            "doc-wrong-owner", owner_id=2, settings=settings
        )
        unrelated = _document("doc-unrelated", settings={"note": "original"})
        db.session.commit()

        parser = _parser_with_timings()
        assert worker._persist_partial_preparation_metrics(
            parser, target.id, 1, "job-current", "exec-current"
        )
        assert not worker._persist_partial_preparation_metrics(
            parser, stale_job.id, 1, "job-current", "exec-current"
        )
        assert not worker._persist_partial_preparation_metrics(
            parser, stale_execution.id, 1, "job-current", "exec-current"
        )
        assert not worker._persist_partial_preparation_metrics(
            parser, cancelled.id, 1, "job-current", "exec-current"
        )
        assert not worker._persist_partial_preparation_metrics(
            parser, wrong_owner.id, 1, "job-current", "exec-current"
        )

        dirty = db.session.get(ProcessedDocument, unrelated.id)
        dirty.filename = "uncommitted-change.pdf"
        assert dirty in db.session.dirty
        assert not worker._persist_partial_preparation_metrics(
            parser, target.id, 1, "job-stale", "exec-current"
        )

        with db.engine.connect() as connection:
            stored_name = connection.execute(
                select(ProcessedDocument.filename).where(
                    ProcessedDocument.id == unrelated.id
                )
            ).scalar_one()
        assert stored_name == "doc-unrelated.pdf"
        assert dirty.filename == "uncommitted-change.pdf"
        assert dirty in db.session.dirty


def _execution(
    execution_id: str,
    document_id: str,
    *,
    owner_id: int = 1,
) -> McpExecution:
    return McpExecution(
        id=execution_id,
        owner_user_id=owner_id,
        addon_id="doc_processor",
        resource_kind="document",
        resource_id=document_id,
        mode="passive",
        status="running",
        request_json={"synthetic": "private execution request"},
    )


def _work_item(
    execution_id: str,
    item_id: str,
    state: str,
    *,
    created_at,
    leased_at=None,
    completed_at=None,
    attempt_count: int = 0,
    batch_id: str | None = None,
) -> McpWorkItem:
    item = McpWorkItem(
        id=item_id,
        execution_id=execution_id,
        request_fingerprint=f"fingerprint-{item_id}",
        request_json={"synthetic": "private work request"},
        batch_id=batch_id,
        state=state,
        attempt_count=attempt_count,
        created_at=created_at,
        leased_at=leased_at,
        completed_at=completed_at,
    )
    if state == "leased":
        item.lease_token = f"token-{item_id}"
        item.lease_owner = "synthetic-worker"
        item.lease_expires_at = utc_now_db() + timedelta(minutes=5)
    return item


def test_status_projection_adds_exact_owner_scoped_content_free_work_timing(
    metrics_app,
):
    with metrics_app.app_context():
        now = utc_now_db()
        settings = {
            "active_job_id": "job-current",
            "mcp_execution_id": "current-execution",
            "metrics": {
                "preparation_timings": {
                    "metadata_total": {
                        "calls": 1,
                        "wall_seconds": 1.25,
                        "response_text": "private parser response",
                    },
                    "unknown_phase": {"calls": 99},
                }
            },
        }
        document = _document("doc-work-timing", settings=settings)
        created = now - timedelta(seconds=65)
        leased = now - timedelta(seconds=20)
        completed = now - timedelta(seconds=11)
        execution = _execution("historical-execution", document.id)
        db.session.add_all(
            [
                execution,
                _execution("foreign-owner-execution", document.id, owner_id=2),
                _execution("foreign-resource-execution", "another-document"),
                _execution("current-execution", document.id),
                _work_item(
                    execution.id,
                    "unclaimed-nonbatch",
                    "pending",
                    created_at=created,
                ),
                _work_item(
                    execution.id,
                    "retried-pending",
                    "pending",
                    created_at=now - timedelta(seconds=70),
                    attempt_count=1,
                    batch_id="synthetic-batch",
                ),
                _work_item(
                    execution.id,
                    "leased-item",
                    "leased",
                    created_at=now - timedelta(seconds=25),
                    leased_at=leased,
                    attempt_count=1,
                ),
                _work_item(
                    execution.id,
                    "completed-item",
                    "completed",
                    created_at=now - timedelta(seconds=30),
                    leased_at=leased,
                    completed_at=completed,
                    attempt_count=1,
                ),
                _work_item(
                    execution.id,
                    "cancelled-item",
                    "cancelled",
                    created_at=now - timedelta(seconds=35),
                ),
            ]
        )
        db.session.commit()

        service = mcp_service.DocProcessorMcpService(None, 1)
        statements = []

        def capture_sql(_connection, _cursor, statement, _parameters, _context, _many):
            lowered = statement.lower()
            if "from mcp_work_item" in lowered:
                statements.append(lowered)

        event.listen(db.engine, "before_cursor_execute", capture_sql)
        try:
            projection = service.job_projection(
                document.id, execution_id="historical-execution"
            )
            foreign_owner = service.job_projection(
                document.id, execution_id="foreign-owner-execution"
            )
            foreign_resource = service.job_projection(
                document.id, execution_id="foreign-resource-execution"
            )
        finally:
            event.remove(db.engine, "before_cursor_execute", capture_sql)

        timing = projection["external_work_timing"]
        assert timing == {
            "pending_count": 2,
            "leased_count": 1,
            "completed_count": 1,
            "oldest_unclaimed_seconds": pytest.approx(65.0, abs=1.0),
            "latest_lease_to_completion_seconds": {
                "sum": 9.0,
                "count": 1,
            },
            "truncated": False,
        }
        assert projection["execution_id"] == "current-execution"
        assert "external_work_timing" not in foreign_owner
        assert "external_work_timing" not in foreign_resource
        assert "preparation_timings" not in projection or projection[
            "preparation_timings"
        ] == {
            "metadata_total": {"calls": 1, "wall_seconds": 1.25}
        }
        assert "private parser response" not in repr(projection)
        assert "private work request" not in repr(projection)
        assert "private execution request" not in repr(projection)
        assert statements
        for statement in statements:
            assert "request_json" not in statement
            assert "accepted_response_json" not in statement
            assert "lease_token" not in statement
            assert "lease_owner" not in statement


def test_status_external_work_timing_marks_over_limit_execution_truncated(
    metrics_app, monkeypatch
):
    with metrics_app.app_context():
        document = _document("doc-truncated-work", settings={})
        db.session.add(_execution("oversized-execution", document.id))
        db.session.commit()
        original_query = db.session.query
        observed_limit = []

        class WorkItemQueryProbe:
            def __init__(self, query):
                self.query = query

            def join(self, *args, **kwargs):
                self.query = self.query.join(*args, **kwargs)
                return self

            def filter(self, *args, **kwargs):
                self.query = self.query.filter(*args, **kwargs)
                return self

            def order_by(self, *args, **kwargs):
                self.query = self.query.order_by(*args, **kwargs)
                return self

            def limit(self, count):
                observed_limit.append(count)
                self.query = self.query.limit(count)
                return self

            def all(self):
                timestamp = utc_now_db()
                return [
                    SimpleNamespace(
                        id=str(index),
                        state="pending",
                        created_at=timestamp,
                        leased_at=None,
                        completed_at=None,
                        attempt_count=0,
                    )
                    for index in range(5001)
                ]

        def query_factory(*entities):
            query = original_query(*entities)
            if entities and entities[0] is McpWorkItem.id:
                return WorkItemQueryProbe(query)
            return query

        monkeypatch.setattr(db.session, "query", query_factory)
        projection = mcp_service.DocProcessorMcpService(None, 1).job_projection(
            document.id, execution_id="oversized-execution"
        )

        assert observed_limit == [5001]
        assert projection["external_work_timing"] == {
            "pending_count": None,
            "leased_count": None,
            "completed_count": None,
            "oldest_unclaimed_seconds": None,
            "latest_lease_to_completion_seconds": {
                "sum": None,
                "count": None,
            },
            "truncated": True,
        }


def test_safe_document_and_status_projection_omit_empty_or_unsafe_timing_maps(
    metrics_app, monkeypatch
):
    with metrics_app.app_context():
        document = _document(
            "doc-safe-projection",
            settings={
                "metrics": {
                    "preparation_timings": {
                        "metadata_total": {
                            "calls": 1,
                            "wall_seconds": 2,
                            "private_text": "must not pass through",
                        },
                        "private_phase": {"calls": 99},
                    }
                }
            },
        )
        db.session.commit()
        service = mcp_service.DocProcessorMcpService(None, 1)
        monkeypatch.setattr(
            service,
            "_client",
            lambda: SimpleNamespace(
                _source_payload=lambda _document, include_content=False: {
                    "id": document.id
                }
            ),
        )
        monkeypatch.setattr(service, "_source_descriptor", lambda _document: {})

        safe_document = service._safe_document(
            document.id, document_record=document
        )
        status = service.job_projection(document.id)

        expected = {"metadata_total": {"calls": 1, "wall_seconds": 2}}
        assert safe_document["preparation_timings"] == expected
        assert status["preparation_timings"] == expected
        assert "must not pass through" not in repr(safe_document)
        assert "private_phase" not in repr(status)


@pytest.mark.parametrize("mode", ["managed", "passive"])
@pytest.mark.parametrize(
    ("binding_key", "previous_id", "new_id", "keep_timings"),
    [
        ("mcp_execution_id", "old-execution", "new-execution", False),
        ("mcp_last_execution_id", "old-execution", "new-execution", False),
        ("mcp_execution_id", "same-execution", "same-execution", True),
        ("mcp_last_execution_id", "same-execution", "same-execution", True),
    ],
)
def test_start_document_resets_preparation_timings_only_for_new_execution(
    metrics_app,
    tmp_path,
    monkeypatch,
    mode,
    binding_key,
    previous_id,
    new_id,
    keep_timings,
):
    class Queue:
        def enqueue(self, _task_name, _payload):
            return "job-metrics-start"

    class Context:
        def __init__(self, application):
            self.application = application
            self.queue = Queue()

        def ensure_worker_pool(self, _script_name, *, num_workers=1):
            assert num_workers == 1

        def capability_candidates(self, _domain, include_inactive=False):
            del include_inactive
            return ()

        def get_task_queue(self, _addon_id):
            return self.queue

    executor_profile = {
        "schema_version": "carlina.external-executor.v1",
        "max_input_tokens": 1_024,
        "max_output_tokens": 256,
        "input_modalities": ["text"],
        "supports_tools": False,
        "supports_structured_output": False,
        "supports_reasoning": False,
        "user_confirmed": True,
    }

    with metrics_app.app_context():
        source = tmp_path / f"start-{mode}-{binding_key}.txt"
        source.write_text("synthetic document", encoding="utf-8")
        context = Context(metrics_app)
        service = mcp_service.DocProcessorMcpService(context, 1)
        if mode == "managed":
            monkeypatch.setattr(service, "_readiness", MagicMock())
        imported = service.import_document(source)
        document = db.session.get(ProcessedDocument, imported["id"])
        document.status = "completed"
        document_settings = dict(document.settings or {})
        document_settings[binding_key] = previous_id
        document_settings["metrics"] = {
            "existing_metric": "preserve",
            "metadata_preparation": {"calls": 7},
            "preparation_timings": {
                "metadata_total": {"calls": 4, "wall_seconds": 3.5}
            },
        }
        document.settings = document_settings
        db.session.commit()

        result = service.start_document(
            document.id,
            execution_id=new_id,
            mode=mode,
            executor_profile=executor_profile if mode == "passive" else None,
        )

        assert result["state"] == "queued"
        stored_metrics = db.session.get(ProcessedDocument, document.id).settings[
            "metrics"
        ]
        assert stored_metrics["existing_metric"] == "preserve"
        assert stored_metrics["metadata_preparation"] == {"calls": 7}
        if keep_timings:
            assert stored_metrics["preparation_timings"] == {
                "metadata_total": {"calls": 4, "wall_seconds": 3.5}
            }
        else:
            assert "preparation_timings" not in stored_metrics


class _MarkerBaseException(BaseException):
    pass
