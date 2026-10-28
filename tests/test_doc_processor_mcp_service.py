from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from content_storage_support import bind_content_policy
from flask import Flask

from iarf_assistant.addons.doc_processor import mcp_service
from iarf_assistant.addons.doc_processor.models import ProcessedDocument
from iarf_assistant.extensions import db
from iarf_assistant.models import McpExecution, User

EXECUTOR_PROFILE = {
    "schema_version": "carlina.external-executor.v1",
    "max_input_tokens": 32_000,
    "max_output_tokens": 4_000,
    "input_modalities": ["text", "image"],
    "supports_tools": True,
    "supports_structured_output": True,
    "supports_reasoning": True,
    "user_confirmed": True,
}


class Queue:
    def __init__(self, *, fail=False):
        self.enqueued = []
        self.cancelled = []
        self.fail = fail

    def enqueue(self, task_name, payload):
        if self.fail:
            raise RuntimeError("queue unavailable")
        job_id = f"job-{len(self.enqueued) + 1}"
        self.enqueued.append((task_name, payload))
        return job_id

    def get_status(self, job_id):
        return {"status": "queued"} if job_id else {"status": "unknown"}

    def request_cancel(self, job_id, requested_by=None, reason=None):
        self.cancelled.append((job_id, requested_by, reason))
        return {"status": "cancelled", "cancel_requested": True}


class Context:
    def __init__(self, app, queue=None):
        self.application = app
        self.queue = queue or Queue()
        self.worker_pool_calls = []

    def capability_candidates(self, _domain, include_inactive=False):
        del include_inactive
        return ()

    def ensure_worker_pool(self, script_name, *, num_workers=1):
        self.worker_pool_calls.append((script_name, num_workers))

    def get_task_queue(self, addon_id):
        assert addon_id == "doc_processor"
        return self.queue


@pytest.fixture
def app(tmp_path, monkeypatch):
    application = Flask(__name__, instance_path=str(tmp_path / "instance"))
    bind_content_policy(application)
    upload_root = tmp_path / "uploads"
    application.config.update(
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{tmp_path / 'doc-mcp.db'}",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SECRET_KEY="doc-mcp-test",
        TESTING=True,
        CARLINA_UPLOADS_DIR=str(upload_root),
        DOC_PROCESSOR_MAX_UPLOAD_BYTES=32,
    )
    db.init_app(application)
    with application.app_context():
        db.create_all()
        db.session.add_all(
            [
                User(id=1, email="doc-one@example.test", password_hash="test"),
                User(id=2, email="doc-two@example.test", password_hash="test"),
            ]
        )
        db.session.commit()
        monkeypatch.setattr(
            mcp_service,
            "validate_pdf_model_readiness",
            lambda *_args, **_kwargs: None,
        )
        yield application, upload_root
        db.session.remove()
        db.drop_all()
        db.engine.dispose()


def _source(tmp_path, filename="notes.txt", content=b"hello"):
    source = tmp_path / "incoming" / filename
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(content)
    return source


def _service(application, owner_id, queue=None):
    return mcp_service.DocProcessorMcpService(Context(application, queue), owner_id)


def test_import_is_owner_scoped_copy_only_created_and_safe(app, tmp_path):
    application, upload_root = app
    source = _source(tmp_path, "unsafe notes.txt")
    with application.app_context():
        service = _service(application, 1)
        result = service.import_document(source)

        assert result["status"] == "created"
        assert "file_path" not in result
        assert "settings" not in result
        assert source.exists()
        stored = list((upload_root / "1" / "doc_processor_docs").rglob("*"))
        assert [item.name for item in stored if item.is_file()] == ["unsafe_notes.txt"]
        with pytest.raises(mcp_service.DocProcessorMcpServiceError) as error:
            _service(application, 2).get_document(result["id"])
        assert error.value.code == "not_found"


def test_import_rejects_symlink_missing_oversize_and_cleans_failure(
    app, tmp_path, monkeypatch
):
    application, upload_root = app
    source = _source(tmp_path)
    link = tmp_path / "incoming" / "link.txt"
    link.symlink_to(source)
    with application.app_context():
        service = _service(application, 1)
        with pytest.raises(mcp_service.DocProcessorMcpServiceError) as symlink_error:
            service.import_document(link)
        assert symlink_error.value.code == "invalid_source"

        with pytest.raises(mcp_service.DocProcessorMcpServiceError) as missing_error:
            service.import_document(tmp_path / "incoming" / "missing.txt")
        assert missing_error.value.code == "source_unavailable"

        oversized = _source(tmp_path, "large.txt", b"x" * 33)
        with pytest.raises(mcp_service.DocProcessorMcpServiceError) as size_error:
            service.import_document(oversized)
        assert size_error.value.code == "upload_too_large"

        monkeypatch.setattr(
            mcp_service,
            "ensure_processed_document_source",
            lambda _document: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        with pytest.raises(mcp_service.DocProcessorMcpServiceError) as failure:
            service.import_document(source)
        assert failure.value.code == "import_failed"
        assert not list((upload_root / "1" / "doc_processor_docs").glob("*/**/*"))
        assert db.session.query(ProcessedDocument).count() == 0


def test_configure_rejects_unknown_paths_and_ids_without_mutation(
    app, tmp_path, monkeypatch
):
    application, _upload_root = app
    source = _source(tmp_path)
    with application.app_context():
        service = _service(application, 1)
        document = service.import_document(source)
        seen = []
        monkeypatch.setattr(
            mcp_service,
            "validate_pdf_model_readiness",
            lambda _filename, settings, **_kwargs: seen.append(settings) or None,
        )
        stored = db.session.get(ProcessedDocument, document["id"])
        before = dict(stored.settings or {})
        with pytest.raises(mcp_service.DocProcessorMcpServiceError) as error:
            service.configure_document(
                document["id"],
                {
                    "engine": "native",
                    "file_path": "/tmp/secret",
                    "knowledge_source_id": "foreign-source",
                    "pdf_options": {
                        "metadata_prepass": False,
                        "secret_id": "x",
                    },
                },
            )
        assert error.value.code == "invalid_settings"
        assert "file_path" in str(error.value)
        assert "knowledge_source_id" in str(error.value)
        assert "pdf_options.secret_id" in str(error.value)
        assert not seen
        stored = db.session.get(ProcessedDocument, document["id"])
        assert stored.settings == before


def test_configure_rejects_coerced_setting_types_without_mutation(app, tmp_path):
    application, _upload_root = app
    source = _source(tmp_path)
    with application.app_context():
        service = _service(application, 1)
        document = service.import_document(source)
        stored = db.session.get(ProcessedDocument, document["id"])
        before = dict(stored.settings or {})

        with pytest.raises(mcp_service.DocProcessorMcpServiceError) as error:
            service.configure_document(
                document["id"],
                {"pdf_options": {"skip_cropping": "false"}},
            )

        assert error.value.code == "invalid_settings"
        assert "pdf_options.skip_cropping" in str(error.value)
        assert db.session.get(ProcessedDocument, document["id"]).settings == before


@pytest.mark.parametrize("accepted_before_error", [False, True])
def test_initial_passive_enqueue_failure_retains_reconcilable_intent(
    app, tmp_path, accepted_before_error
):
    import fitz

    from iarf_assistant.addons.doc_processor.execution_recovery import reconcile_resume_dispatch
    from iarf_assistant.addons.doc_processor.mcp_execution import start_mcp_execution
    from iarf_assistant.core.encryption import EncryptionService, InstallationKeyStore
    from iarf_assistant.core.executor_profile import require_executor_profile
    from iarf_assistant.core.mcp_jobs import serialize_execution

    application, _ = app
    application.extensions["carlina_encryption"] = EncryptionService(InstallationKeyStore.ephemeral())
    application.config["DOC_PROCESSOR_MAX_UPLOAD_BYTES"] = 1024 * 1024
    source = tmp_path / "initial.pdf"
    with fitz.open() as pdf:
        pdf.new_page()
        pdf.save(source)

    class RecoveringQueue(Queue):
        def enqueue_once(self, name, payload, *, job_id):
            if self.fail:
                if accepted_before_error and not self.enqueued:
                    self.enqueued.append((name, dict(payload), job_id))
                raise RuntimeError("synthetic dispatch interruption")
            if not self.enqueued:
                self.enqueued.append((name, payload, job_id))
            return job_id

        def get_status(self, job_id):
            return {"status": "queued" if self.enqueued else "unknown"}

    queue = RecoveringQueue(fail=True)
    service = _service(application, 1, queue)
    document = service.import_document(source)
    started = start_mcp_execution(
        owner_user_id=1, document_id=document["id"], mode="passive",
        service_factory=lambda: service, execution_payload=serialize_execution,
        executor_profile=require_executor_profile(EXECUTOR_PROFILE),
        batch_plan={"schema_version": "carlina.document-batches.v1", "scheduling_mode": "sequential",
                    "batches": [{"batch_id": "whole", "sequence": 0, "start_page": 1, "end_page": 1, "depends_on": []}]},
    )
    execution = db.session.get(McpExecution, started["execution"]["id"])
    assert execution is not None
    assert started["dispatch_pending"] is True
    assert execution.status == "queued"
    assert execution.request_json["recovery"]["initial_attempt"]["job_id"] == started["queue_job_id"]
    projection = service.job_projection(document["id"])
    assert projection["state"] == "queued"
    if not accepted_before_error:
        assert projection["delivery"]["reason"] == "queue_dispatch_pending"
    else:
        assert "delivery" not in projection
        assert len(queue.enqueued) == 1
    initial_attempt = dict(execution.request_json["recovery"]["initial_attempt"])
    queue.fail = False
    assert reconcile_resume_dispatch(service, execution)
    assert reconcile_resume_dispatch(service, execution)
    assert len(queue.enqueued) == 1
    assert queue.enqueued[0][2] == started["queue_job_id"]
    assert execution.request_json["recovery"]["initial_attempt"] == initial_attempt


def test_real_pdf_import_configure_and_passive_start_skips_initial_crop(app, tmp_path):
    from pypdf import PdfWriter

    application, _upload_root = app
    application.config["DOC_PROCESSOR_MAX_UPLOAD_BYTES"] = 1024 * 1024
    source = tmp_path / "incoming" / "book.pdf"
    source.parent.mkdir(parents=True, exist_ok=True)
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.add_blank_page(width=612, height=792)
    with source.open("wb") as output:
        writer.write(output)

    queue = Queue()
    plan = {
        "schema_version": "carlina.document-batches.v1",
        "scheduling_mode": "sequential",
        "batches": [
            {
                "batch_id": "whole",
                "sequence": 0,
                "start_page": 1,
                "end_page": 2,
                "depends_on": [],
            }
        ],
    }
    with application.app_context():
        service = _service(application, 1, queue)
        imported = service.import_document(source)
        assert imported["source"]["extension"] == ".pdf"
        assert imported["source"]["media_type"] == "application/pdf"
        assert imported["source"]["byte_size"] == source.stat().st_size
        assert imported["source"]["page_count"] == 2
        assert imported["effective_settings"]["pdf_options"]["skip_cropping"] is False

        configured = service.configure_document(imported["id"], {})
        assert configured["source"]["page_count"] == 2
        assert configured["effective_settings"]["pdf_options"]["skip_cropping"] is False

        started = service.start_document(
            imported["id"],
            execution_id="exec-real-passive-pdf",
            mode="passive",
            executor_profile=EXECUTOR_PROFILE,
            batch_plan=plan,
        )
        assert started["state"] == "queued"
        assert started["document"]["status"] == "processing"
        assert (
            started["document"]["effective_settings"]["pdf_options"]["skip_cropping"]
            is True
        )
        assert queue.enqueued[0][0] == "process_document"
        stored = db.session.get(ProcessedDocument, imported["id"])
        assert stored.status == "processing"
        assert stored.settings["pdf_options"]["skip_cropping"] is True
        assert stored.settings["mcp_batch_plan"] == plan
        assert stored.settings["mcp_page_batch_protocol"] == "carlina.pdf-page-batch.v1"
        assert stored.settings["mcp_page_blank_policy"] == "agent-declared-v1"
        assert stored.settings["mcp_image_transport"] == "png-2000-v1"
        assert stored.settings["mcp_metadata_policy"] == "budgeted-v1"
        assert stored.settings["mcp_note_arrangement_policy"] == "source-arrangement-v1"
        assert stored.settings["mcp_semantic_structure_policy"] == "lists-diagrams-v1"
        assert stored.settings["mcp_image_accessibility_policy"] == "image-descriptions-v1"
        assert stored.settings["mcp_native_vision_authoring_policy"] == "direct-v1"
        assert stored.settings["mcp_extraction_issues_policy"] == "issues-v1"
        assert stored.settings["mcp_extraction_input_policy"] == "footnotes-v1"
        assert "mcp_metadata_complete" not in stored.settings
        assert stored.settings["mcp_batch_feedback"] == "validation-v1"


def test_partial_pdf_restart_requires_explicit_discard_and_reports_checkpoint(app, tmp_path):
    application, _ = app
    queue = Queue()
    with application.app_context():
        service = _service(application, 1, queue)
        document = service.import_document(_source(tmp_path, "partial.pdf"), settings={
            "pdf_options": {"metadata_prepass": False},
        })
        stored = db.session.get(ProcessedDocument, document["id"])
        stored.status = "error"
        checkpoint = {
            "html_parts": ["<main_content><p>Saved</p></main_content><footnotes></footnotes>", None],
            "batch_boundaries": {"first": {"last_page": 1}},
        }
        stored.settings = {**stored.settings, "pdf_state": checkpoint,
                           "mcp_last_execution_id": "old-execution"}
        db.session.commit()
        projection = service.get_document(document["id"])
        assert projection["checkpoint"] == {
            "validated_pages": 1, "validated_batches": 1, "new_execution_resume_supported": False,
        }
        assert service.job_projection(document["id"])["execution_id"] == "old-execution"
        plan = {"scheduling_mode": "sequential", "batches": [
            {"batch_id": "whole", "start_page": 1, "end_page": 2},
        ]}
        with pytest.raises(mcp_service.DocProcessorMcpServiceError) as error:
            service.start_document(document["id"], execution_id="new-execution", mode="passive",
                                   executor_profile=EXECUTOR_PROFILE, batch_plan=plan)
        assert error.value.code == "checkpoint_restart_required"
        assert stored.settings["pdf_state"] == checkpoint
        assert not queue.enqueued
        with pytest.raises(mcp_service.DocProcessorMcpServiceError):
            service.preflight_passive_document(document["id"],
                mcp_service.require_executor_profile(EXECUTOR_PROFILE))
        started = service.start_document(
            document["id"], execution_id="new-execution", mode="passive",
            executor_profile=EXECUTOR_PROFILE, batch_plan=plan, restart_from_scratch=True,
        )
        assert started["state"] == "queued"
        assert "pdf_state" not in stored.settings
        assert len(queue.enqueued) == 1


def test_malformed_pdf_source_descriptor_is_safe(app, tmp_path):
    application, _upload_root = app
    source = _source(tmp_path, "malformed.pdf", b"not a pdf")
    with application.app_context():
        imported = _service(application, 1).import_document(source)
        assert imported["source"] == {
            "extension": ".pdf",
            "media_type": "application/pdf",
            "byte_size": len(b"not a pdf"),
            "page_count": None,
        }


def test_start_enqueues_with_execution_markers_and_pool(app, tmp_path):
    application, _upload_root = app
    queue = Queue()
    source = _source(tmp_path)
    with application.app_context():
        service = _service(application, 1, queue)
        document = service.import_document(source)
        result = service.start_document(
            document["id"],
            execution_id="exec-1",
            mode="passive",
            executor_profile=EXECUTOR_PROFILE,
        )
        assert result["state"] == "queued"
        assert result["queue_job_id"] == "job-1"
        assert queue.enqueued == [
            (
                "process_document",
                {
                    "document_id": document["id"],
                    "user_id": 1,
                    "mcp_execution_id": "exec-1",
                    "mcp_execution_mode": "passive",
                    "engine_id": "native",
                },
            )
        ]
        stored = db.session.get(ProcessedDocument, document["id"])
        assert stored.settings["mcp_execution_id"] == "exec-1"
        assert stored.settings["mcp_execution_mode"] == "passive"
        assert stored.settings["active_job_id"] == "job-1"
        assert "enqueue_claim" not in stored.settings


def test_cancel_execution_signals_exact_queue_and_clears_binding(app, tmp_path):
    application, _upload_root = app
    queue = Queue()
    source = _source(tmp_path)
    with application.app_context():
        service = _service(application, 1, queue)
        document = service.import_document(source)
        started = service.start_document(
            document["id"],
            execution_id="exec-cancel",
            mode="passive",
            executor_profile=EXECUTOR_PROFILE,
        )

        result = service.cancel_execution(
            document["id"],
            execution_id="exec-cancel",
            queue_job_id=started["queue_job_id"],
        )

        assert result["state"] == "cancelled"
        assert queue.cancelled == [
            (
                started["queue_job_id"],
                1,
                "Cancelled through the MCP execution.",
            )
        ]
        stored = db.session.get(ProcessedDocument, document["id"])
        assert stored.status == "cancelled"
        for key in ("active_job_id", "mcp_execution_id", "mcp_execution_mode"):
            assert key not in stored.settings
        assert service.cancel_execution(
            document["id"],
            execution_id="exec-cancel",
            queue_job_id=started["queue_job_id"],
        )["state"] == "cancelled"


def test_cancel_waiting_review_accepts_historical_execution_queue_binding(
    app, tmp_path
):
    application, _upload_root = app
    queue = Queue()
    source = _source(tmp_path)
    with application.app_context():
        service = _service(application, 1, queue)
        document = service.import_document(source)
        started = service.start_document(
            document["id"],
            execution_id="exec-review-cancel",
            mode="passive",
            executor_profile=EXECUTOR_PROFILE,
        )
        stored = db.session.get(ProcessedDocument, document["id"])
        stored.status = "reviewing_images"
        settings = dict(stored.settings or {})
        settings.pop("active_job_id", None)
        stored.settings = settings
        db.session.commit()

        result = service.cancel_execution(
            document["id"],
            execution_id="exec-review-cancel",
            queue_job_id=started["queue_job_id"],
        )

        assert result["state"] == "cancelled"
        assert queue.cancelled == []


@pytest.mark.parametrize(
    ("document_status", "expected_state"),
    [("completed", "completed"), ("error", "failed")],
)
def test_cancel_execution_preserves_terminal_product_truth(
    app, tmp_path, document_status, expected_state
):
    application, _upload_root = app
    queue = Queue()
    source = _source(tmp_path)
    with application.app_context():
        service = _service(application, 1, queue)
        document = service.import_document(source)
        started = service.start_document(
            document["id"],
            execution_id="exec-terminal",
            mode="passive",
            executor_profile=EXECUTOR_PROFILE,
        )
        stored = db.session.get(ProcessedDocument, document["id"])
        stored.status = document_status
        db.session.commit()

        result = service.cancel_execution(
            document["id"],
            execution_id="exec-terminal",
            queue_job_id=started["queue_job_id"],
        )

        assert result["state"] == expected_state
        assert queue.cancelled == []
        assert db.session.get(ProcessedDocument, document["id"]).status == document_status


def test_cancel_execution_loses_cleanly_when_worker_completion_wins_race(
    app, tmp_path
):
    application, _upload_root = app
    queue = Queue()
    source = _source(tmp_path)
    with application.app_context():
        service = _service(application, 1, queue)
        document = service.import_document(source)
        started = service.start_document(
            document["id"],
            execution_id="exec-completion-race",
            mode="passive",
            executor_profile=EXECUTOR_PROFILE,
        )

        original_request_cancel = queue.request_cancel

        def complete_before_cancel_cas(job_id, requested_by=None, reason=None):
            stored = db.session.get(ProcessedDocument, document["id"])
            assert stored is not None
            stored.status = "completed"
            db.session.commit()
            return original_request_cancel(job_id, requested_by, reason)

        queue.request_cancel = complete_before_cancel_cas

        result = service.cancel_execution(
            document["id"],
            execution_id="exec-completion-race",
            queue_job_id=started["queue_job_id"],
        )

        assert result["state"] == "completed"
        assert db.session.get(ProcessedDocument, document["id"]).status == "completed"


def test_start_pdf_without_prepass_waits_for_review_without_queue(app, tmp_path):
    application, _upload_root = app
    queue = Queue()
    source = _source(tmp_path, "scan.pdf", b"pdf")
    with application.app_context():
        service = _service(application, 1, queue)
        document = service.import_document(
            source,
            settings={
                "engine": "llm_vision",
                "pdf_options": {"metadata_prepass": False},
            },
        )
        result = service.start_document(
            document["id"], execution_id="exec-review", mode="managed"
        )
        assert result["state"] == "waiting_review"
        assert result["queue_job_id"] is None
        assert queue.enqueued == []
        stored = db.session.get(ProcessedDocument, document["id"])
        assert stored.status == "needs_cropping"


def test_passive_pdf_vision_requires_declared_image_capability(app, tmp_path):
    application, _upload_root = app
    source = _source(tmp_path, "scan.pdf", b"pdf")
    text_only_profile = {**EXECUTOR_PROFILE, "input_modalities": ["text"]}
    with application.app_context():
        service = _service(application, 1, Queue())
        document = service.import_document(
            source,
            settings={
                "engine": "llm_vision",
                "pdf_options": {"metadata_prepass": False},
            },
        )
        with pytest.raises(mcp_service.DocProcessorMcpServiceError) as error:
            service.start_document(
                document["id"],
                execution_id="exec-capability",
                mode="passive",
                executor_profile=text_only_profile,
                batch_plan={
                    "schema_version": "carlina.document-batches.v1",
                    "scheduling_mode": "sequential",
                    "batches": [
                        {
                            "batch_id": "whole",
                            "start_page": 1,
                            "end_page": 1,
                        }
                    ],
                },
            )
        assert error.value.code == "executor_profile_capability"


def test_passive_pdf_preserves_later_image_review_handoff(app, tmp_path):
    from pypdf import PdfWriter

    application, _upload_root = app
    application.config["DOC_PROCESSOR_MAX_UPLOAD_BYTES"] = 1024 * 1024
    queue = Queue()
    source = tmp_path / "incoming" / "image-review.pdf"
    source.parent.mkdir(parents=True, exist_ok=True)
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    with source.open("wb") as output:
        writer.write(output)

    with application.app_context():
        service = _service(application, 1, queue)
        document = service.import_document(source)
        stored = db.session.get(ProcessedDocument, document["id"])
        stored.status = "reviewing_images"
        db.session.commit()

        result = service.start_document(
            document["id"],
            execution_id="exec-image-review",
            mode="passive",
            executor_profile=EXECUTOR_PROFILE,
            batch_plan={
                "schema_version": "carlina.document-batches.v1",
                "scheduling_mode": "sequential",
                "batches": [
                    {"batch_id": "whole", "start_page": 1, "end_page": 1}
                ],
            },
        )

        assert result["state"] == "waiting_review"
        assert result["queue_job_id"] is None
        assert result["document"]["status"] == "reviewing_images"
        assert result["document"]["effective_settings"]["pdf_options"][
            "skip_cropping"
        ] is True
        assert queue.enqueued == []


@pytest.mark.parametrize(
    "existing_policy",
    [None, "image-descriptions-v1", "legacy-image-accessibility-v0"],
)
def test_passive_pdf_accessibility_policy_is_preserved_on_review_continuation(
    app, tmp_path, existing_policy
):
    from pypdf import PdfWriter

    application, _upload_root = app
    application.config["DOC_PROCESSOR_MAX_UPLOAD_BYTES"] = 1024 * 1024
    source = tmp_path / "incoming" / "review-accessibility.pdf"
    source.parent.mkdir(parents=True, exist_ok=True)
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    with source.open("wb") as output:
        writer.write(output)

    plan = {
        "schema_version": "carlina.document-batches.v1",
        "scheduling_mode": "sequential",
        "batches": [{"batch_id": "whole", "start_page": 1, "end_page": 1}],
    }
    with application.app_context():
        service = _service(application, 1, Queue())
        imported = service.import_document(source)
        stored = db.session.get(ProcessedDocument, imported["id"])
        stored.status = "reviewing_images"
        settings = dict(stored.settings or {})
        settings["mcp_extraction_input_policy"] = "legacy-footnotes-v0"
        if existing_policy is not None:
            settings["mcp_image_accessibility_policy"] = existing_policy
        stored.settings = settings
        db.session.commit()

        result = service.start_document(
            imported["id"],
            execution_id="exec-review-accessibility",
            mode="passive",
            executor_profile=EXECUTOR_PROFILE,
            batch_plan=plan,
        )

        assert result["state"] == "waiting_review"
        refreshed = db.session.get(ProcessedDocument, imported["id"])
        assert refreshed.settings["mcp_extraction_input_policy"] == "legacy-footnotes-v0"
        if existing_policy is None:
            assert "mcp_image_accessibility_policy" not in refreshed.settings
        else:
            assert refreshed.settings["mcp_image_accessibility_policy"] == existing_policy


@pytest.mark.parametrize("existing_marker", [None, "png-2000-v1"])
@pytest.mark.parametrize("authoring_policy", [None, "direct-v1"])
@pytest.mark.parametrize(
    "note_arrangement_policy",
    [None, "source-arrangement-v1", "legacy-arrangement-v0"],
)
def test_passive_pdf_image_transport_marker_is_frozen_on_review_continuation(
    app, tmp_path, existing_marker, authoring_policy, note_arrangement_policy
):
    from pypdf import PdfWriter

    application, _upload_root = app
    application.config["DOC_PROCESSOR_MAX_UPLOAD_BYTES"] = 1024 * 1024
    source = tmp_path / f"review-{existing_marker or 'legacy'}.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    with source.open("wb") as output:
        writer.write(output)

    plan = {
        "schema_version": "carlina.document-batches.v1",
        "scheduling_mode": "sequential",
        "batches": [{"batch_id": "whole", "start_page": 1, "end_page": 1}],
    }
    with application.app_context():
        service = _service(application, 1, Queue())
        imported = service.import_document(source)
        stored = db.session.get(ProcessedDocument, imported["id"])
        stored.status = "reviewing_images"
        if authoring_policy is not None:
            stored.settings = {
                **dict(stored.settings or {}),
                "mcp_native_vision_authoring_policy": authoring_policy,
            }
        if note_arrangement_policy is not None:
            settings = dict(stored.settings or {})
            settings["mcp_note_arrangement_policy"] = note_arrangement_policy
            stored.settings = settings
        if existing_marker is not None:
            settings = dict(stored.settings or {})
            settings["mcp_image_transport"] = existing_marker
            settings["mcp_page_batch_protocol"] = "carlina.pdf-page-batch.v1"
            settings["mcp_page_blank_policy"] = "agent-declared-v1"
            stored.settings = settings
        db.session.commit()

        result = service.start_document(
            imported["id"],
            execution_id="exec-review-marker",
            mode="passive",
            executor_profile=EXECUTOR_PROFILE,
            batch_plan=plan,
        )

        assert result["state"] == "waiting_review"
        refreshed = db.session.get(ProcessedDocument, imported["id"])
        assert (
            refreshed.settings.get("mcp_native_vision_authoring_policy") == authoring_policy
        )
        if note_arrangement_policy is None:
            assert "mcp_note_arrangement_policy" not in refreshed.settings
        else:
            assert (
                refreshed.settings["mcp_note_arrangement_policy"]
                == note_arrangement_policy
            )
        if existing_marker is None:
            assert "mcp_image_transport" not in refreshed.settings
            assert "mcp_page_batch_protocol" not in refreshed.settings
            assert "mcp_page_blank_policy" not in refreshed.settings
        else:
            assert refreshed.settings["mcp_image_transport"] == existing_marker
            assert refreshed.settings["mcp_page_batch_protocol"] == "carlina.pdf-page-batch.v1"
            assert refreshed.settings["mcp_page_blank_policy"] == "agent-declared-v1"


def test_start_clears_note_arrangement_marker_for_managed_and_unbatched_passive_runs(
    app, tmp_path
):
    application, _upload_root = app
    with application.app_context():
        service = _service(application, 1, Queue())
        managed = service.import_document(_source(tmp_path, "managed.txt"))
        stored = db.session.get(ProcessedDocument, managed["id"])
        stored.settings = {
            **dict(stored.settings or {}),
            "mcp_batch_plan": {"legacy": True},
            "mcp_page_batch_protocol": "carlina.pdf-page-batch.v1",
            "mcp_page_blank_policy": "agent-declared-v1",
            "mcp_image_transport": "png-2000-v1",
            "mcp_metadata_policy": "budgeted-v1",
            "mcp_native_vision_authoring_policy": "direct-v1",
            "mcp_image_accessibility_policy": "image-descriptions-v1",
            "mcp_note_arrangement_policy": "source-arrangement-v1",
            "mcp_extraction_input_policy": "footnotes-v1",
            "mcp_metadata_complete": True,
        }
        db.session.commit()

        service.start_document(
            managed["id"], execution_id="exec-managed-clear", mode="managed"
        )
        refreshed = db.session.get(ProcessedDocument, managed["id"])
        assert "mcp_batch_plan" not in refreshed.settings
        assert "mcp_page_batch_protocol" not in refreshed.settings
        assert "mcp_page_blank_policy" not in refreshed.settings
        assert "mcp_image_transport" not in refreshed.settings
        assert "mcp_metadata_policy" not in refreshed.settings
        assert "mcp_native_vision_authoring_policy" not in refreshed.settings
        assert "mcp_image_accessibility_policy" not in refreshed.settings
        assert "mcp_note_arrangement_policy" not in refreshed.settings
        assert "mcp_extraction_input_policy" not in refreshed.settings
        assert "mcp_metadata_complete" not in refreshed.settings

        passive = service.import_document(_source(tmp_path, "passive-unbatched.txt"))
        stored_passive = db.session.get(ProcessedDocument, passive["id"])
        stored_passive.settings = {
            **dict(stored_passive.settings or {}),
            "mcp_note_arrangement_policy": "source-arrangement-v1",
            "mcp_image_accessibility_policy": "image-descriptions-v1",
            "mcp_extraction_input_policy": "footnotes-v1",
        }
        db.session.commit()

        service.start_document(
            passive["id"],
            execution_id="exec-passive-unbatched-clear",
            mode="passive",
            executor_profile=EXECUTOR_PROFILE,
        )
        refreshed_passive = db.session.get(ProcessedDocument, passive["id"])
        assert "mcp_note_arrangement_policy" not in refreshed_passive.settings
        assert "mcp_image_accessibility_policy" not in refreshed_passive.settings
        assert "mcp_extraction_input_policy" not in refreshed_passive.settings


def test_job_projection_maps_passive_and_manual_review_states(app, tmp_path):
    application, _upload_root = app
    queue = Queue()
    source = _source(tmp_path)
    with application.app_context():
        service = _service(application, 1, queue)
        document = service.import_document(source)
        started = service.start_document(
            document["id"],
            execution_id="exec-projection",
            mode="passive",
            executor_profile=EXECUTOR_PROFILE,
        )
        projection = service.job_projection(document["id"])
        assert projection["state"] == "queued"
        assert projection["queue_job_id"] == started["queue_job_id"]
        stored = db.session.get(ProcessedDocument, document["id"])
        stored.status = "needs_cropping"
        db.session.commit()
        review = service.job_projection(document["id"])
        assert review["state"] == "waiting_review"
        assert review["ui_path"] == "/addons/doc_processor/"
        assert review["manual_review_path"] == "/addons/doc_processor/"


@pytest.mark.parametrize(
    ("document_status", "queue_status", "expected"),
    [
        ("error", "completed", "failed"),
        ("cancelled", "failed", "cancelled"),
        ("completed", "cancelled", "completed"),
    ],
)
def test_terminal_document_status_overrides_stale_queue_status(
    app,
    tmp_path,
    monkeypatch,
    document_status,
    queue_status,
    expected,
):
    application, _upload_root = app
    queue = Queue()
    monkeypatch.setattr(queue, "get_status", lambda _job: {"status": queue_status})
    with application.app_context():
        service = _service(application, 1, queue)
        imported = service.import_document(_source(tmp_path))
        document = db.session.get(ProcessedDocument, imported["id"])
        document.status = document_status
        document.settings = {"active_job_id": "stale-job"}
        db.session.commit()
        assert service.job_projection(document.id)["state"] == expected


def test_preparation_projections_expose_only_bounded_content_free_fields(app, tmp_path):
    application, _upload_root = app
    with application.app_context():
        service = _service(application, 1, Queue())
        imported = service.import_document(_source(tmp_path))
        document = db.session.get(ProcessedDocument, imported["id"])
        safe = {
            "stage": "heading_round_1",
            "status": "provisional",
            "reason": "input_budget",
            "requested_input_tokens": 154000,
            "admitted_input_tokens": 128000,
        }
        document.settings = {
            "metrics": {
                "metadata_preparation": [
                    {"stage": "metadata_initial", "status": "admitted"},
                    *([None] * 9),
                    {"stage": "private document text", "status": "failed"},
                    {"stage": "metadata_initial", "status": "private response"},
                    {
                        **safe,
                        "prompt": "private document text",
                        "response": "private response",
                        "message_tokens": True,
                        "schema_tokens": -1,
                        "output_token_limit": 1_000_000_001,
                        "declared_input_tokens": "256000",
                    },
                ]
            }
        }
        db.session.commit()
        assert service.get_document(document.id)["metadata_preparation"] == [safe]
        assert service.job_projection(document.id)["metadata_preparation"] == [safe]


@pytest.mark.parametrize("terminal_status", ["completed", "error", "cancelled"])
def test_worker_terminal_state_survives_mcp_enqueue_marker_publication(
    app, tmp_path, monkeypatch, terminal_status,
):
    application, _upload_root = app
    queue = Queue()
    enqueue = queue.enqueue

    def finish_before_enqueue_returns(task_name, payload):
        job_id = enqueue(task_name, payload)
        with application.app_context():
            document = db.session.get(ProcessedDocument, payload["document_id"])
            document.status = terminal_status
            settings = dict(document.settings)
            settings["mcp_last_execution_id"] = settings.pop("mcp_execution_id")
            settings.pop("mcp_execution_mode", None)
            settings.pop("enqueue_claim", None)
            settings["metrics"] = {"worker_evidence": True}
            document.settings = settings
            db.session.commit()
        return job_id

    monkeypatch.setattr(queue, "enqueue", finish_before_enqueue_returns)
    with application.app_context():
        service = _service(application, 1, queue)
        imported = service.import_document(_source(tmp_path))
        result = service.start_document(imported["id"], execution_id="terminal-enqueue", mode="managed")
        assert result["state"] == ("failed" if terminal_status == "error" else terminal_status)
        db.session.expire_all()
        document = db.session.get(ProcessedDocument, imported["id"])
        assert document.status == terminal_status
        assert document.settings["mcp_last_execution_id"] == "terminal-enqueue"
        assert document.settings["metrics"] == {"worker_evidence": True}
        assert "mcp_execution_id" not in document.settings
        assert "active_job_id" not in document.settings
        assert "enqueue_claim" not in document.settings


def test_passive_wrapper_enters_context_for_each_named_handler(monkeypatch):
    from iarf_assistant.addons.doc_processor import worker

    entered = []

    @contextmanager
    def fake_context(**kwargs):
        entered.append(kwargs)
        yield

    monkeypatch.setattr(worker, "passive_execution", fake_context)
    monkeypatch.setattr(
        worker.db.session,
        "get",
        lambda _model, _document_id: SimpleNamespace(
            id="doc-1", user_id=7, settings={}
        ),
    )
    for name in ("prepare_crop_review", "process_document", "resume_pdf_parsing"):
        sentinel = worker._with_passive_execution(
            lambda *_args, handler_name=name: handler_name
        )
        assert (
            sentinel(
                {
                    "document_id": "doc-1",
                    "user_id": 7,
                    "mcp_execution_id": "exec-1",
                    "mcp_execution_mode": "passive",
                },
                None,
                "job-1",
            )
            == name
        )
    assert len(entered) == 3
    assert all(item["resource_kind"] == "document" for item in entered)


@pytest.mark.parametrize(
    ("pages", "options", "input_tokens", "rejected"),
    [
        (390, {}, 128_000, True),
        (390, {}, 256_000, False),
        (390, {"metadata_pages": 10}, 128_000, False),
        (390, {"metadata_pages": 10, "metadata_end_pages": 10}, 128_000, True),
        (12, {"metadata_pages": 10, "metadata_end_pages": 10}, 128_000, False),
        (20, {"excluded_pages": list(range(10))}, 128_000, False),
        (390, {"page_range": "1-4"}, 128_000, True),
        (390, {"metadata_prepass": False}, 128_000, False),
    ],
)
def test_passive_metadata_budget_preflight(
    app, tmp_path, pages, options, input_tokens, rejected,
):
    from pypdf import PdfWriter

    application, _upload_root = app
    application.config["DOC_PROCESSOR_MAX_UPLOAD_BYTES"] = 1024 * 1024
    source = tmp_path / "budget.pdf"
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=612, height=792)
    with source.open("wb") as output:
        writer.write(output)
    queue = Queue()
    with application.app_context():
        service = _service(application, 1, queue)
        document = service.import_document(source, settings={"pdf_options": options})
        stored = db.session.get(ProcessedDocument, document["id"])
        before_settings = dict(stored.settings)
        before_status = stored.status
        kwargs = {
            "execution_id": "budget-test", "mode": "passive",
            "executor_profile": {**EXECUTOR_PROFILE, "max_input_tokens": input_tokens},
            "batch_plan": {
                "schema_version": "carlina.document-batches.v1",
                "scheduling_mode": "sequential",
                "batches": [{"batch_id": "whole", "start_page": 1, "end_page": pages}],
            },
        }
        if rejected:
            with pytest.raises(mcp_service.DocProcessorMcpServiceError) as error:
                service.start_document(document["id"], **kwargs)
            assert error.value.code == "metadata_input_budget_exceeded"
            assert "163840" in str(error.value)
            assert "128000" in str(error.value)
            assert queue.enqueued == []
            db.session.refresh(stored)
            assert stored.status == before_status
            assert stored.settings == before_settings
        else:
            service.start_document(document["id"], **kwargs)
            assert len(queue.enqueued) == 1
