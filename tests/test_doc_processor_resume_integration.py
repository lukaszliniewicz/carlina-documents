"""Real passive PDF/queue recovery with a child exiting before its handshake."""

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import fitz
import pytest
from content_storage_support import bind_content_policy
from flask import Flask

from iarf_assistant.addons.doc_processor import routes, worker
from iarf_assistant.addons.doc_processor.batching import normalize_document_batch_plan
from iarf_assistant.addons.doc_processor.execution_recovery import (
    freeze_recovery_contract,
    resume_execution,
)
from iarf_assistant.addons.doc_processor.mcp_service import DocProcessorMcpService
from iarf_assistant.addons.doc_processor.models import ProcessedDocument
from iarf_assistant.addons.doc_processor.parsers.pdf_parser import PdfParser
from iarf_assistant.addons.doc_processor.parsers.pdf_render_pool import (
    RenderConfig,
    prepared_pages,
)
from iarf_assistant.core.encryption import EncryptionService, InstallationKeyStore
from iarf_assistant.core.mcp_jobs import (
    claim_next_work,
    create_execution,
    issue_access_key,
    resolve_access_key,
    submit_work,
)
from iarf_assistant.core.task_queue import ExternalWorkPending, TaskQueue
from iarf_assistant.extensions import db
from iarf_assistant.models import McpExecution, McpWorkItem, User


def _neutral(schema):
    kind = schema.get("type")
    if isinstance(kind, list) and "null" in kind:
        return None
    if "enum" in schema:
        return schema["enum"][-1]
    if kind == "object":
        return {
            key: _neutral(schema["properties"][key])
            for key in schema.get("required", [])
        }
    return {"array": [], "string": "", "boolean": False, "integer": 0, "number": 0}[
        kind
    ]


@pytest.mark.parametrize("failure_page", [0, 2])
def test_renderer_failure_resumes_same_execution_without_repeat_submissions(
    tmp_path, monkeypatch, failure_page
):
    application = Flask(__name__, instance_path=str(tmp_path / "instance"))
    bind_content_policy(application)
    uploads = tmp_path / "uploads"
    application.config.update(
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{tmp_path / 'recovery.db'}",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        TESTING=True,
        CARLINA_UPLOADS_DIR=str(uploads),
    )
    db.init_app(application)
    application.extensions["carlina_encryption"] = EncryptionService(
        InstallationKeyStore.ephemeral()
    )
    source = uploads / "1" / "doc_processor_docs" / "synthetic.pdf"
    source.parent.mkdir(parents=True)
    with fitz.open() as pdf:
        for number in (1, 2, 3):
            page = pdf.new_page(width=240, height=320)
            page.insert_text((20, 35), f"Synthetic passage number {number}.")
        pdf.save(source)
    exit_script = tmp_path / "exit_worker.py"
    exit_script.write_text("import sys\nsys.exit(73)\n")
    original_prepare = PdfParser._prepared_page_images
    accepted_first = False
    failed = False

    @contextmanager
    def prepare(parser, document, indexes, *args, **kwargs):
        nonlocal failed
        if accepted_first and not failed and failure_page in indexes:
            failed = True
            with prepared_pages(
                source,
                parser.output_folder,
                [{"page_index": failure_page, "crop_box": None}],
                settings={},
                coordination_dir=tmp_path / "render-coordination",
                config=RenderConfig(1, 1, 10),
                _worker_script=exit_script,
            ) as pages:
                next(pages)
            pytest.fail("The child should have exited before ready")
        with original_prepare(parser, document, indexes, *args, **kwargs) as pages:
            yield pages

    monkeypatch.setattr(PdfParser, "_prepared_page_images", prepare)
    with application.app_context():
        db.create_all()
        db.session.add(User(id=1, email="recovery@example.test", password_hash="test"))
        db.session.commit()
        plan = normalize_document_batch_plan(
            {
                "scheduling_mode": "sequential",
                "batches": [
                    {"batch_id": "first", "start_page": 1, "end_page": 2},
                    {"batch_id": "last", "start_page": 3, "end_page": 3},
                ],
            }
        )
        execution = create_execution(
            1,
            "doc_processor",
            "document",
            "synthetic",
            {
                "executor_profile": {
                    "schema_version": "carlina.external-executor.v1",
                    "max_input_tokens": 128000,
                    "max_output_tokens": 4000,
                    "input_modalities": ["text", "image"],
                    "supports_tools": True,
                    "supports_structured_output": True,
                    "supports_reasoning": True,
                    "user_confirmed": True,
                },
                "batch_plan": plan,
            },
            mode="passive",
            queue_job_id="initial",
        )
        execution_id = execution.id
        settings = {
            "engine_id": "llm_vision",
            "mcp_execution_id": execution_id,
            "mcp_execution_mode": "passive",
            "active_job_id": "initial",
            "mcp_batch_plan": plan,
            "mcp_page_batch_protocol": "carlina.pdf-page-batch.v1",
            "mcp_image_transport": "png-2000-v1",
            "mcp_metadata_policy": "budgeted-v1",
            "pdf_options": {
                "metadata_prepass": True,
                "metadata_pages": 2,
                "metadata_end_pages": 0,
                "extract_images": False,
                "correct_images": False,
                "llm_boundary_merge": False,
                "prompt_cache": False,
                "skip_cropping": True,
            },
        }
        doc = ProcessedDocument(
            id="synthetic",
            user_id=1,
            filename="synthetic.pdf",
            file_path=str(source),
            status="processing",
            settings=settings,
        )
        db.session.add(doc)
        execution.request_json = {
            **execution.request_json,
            "recovery": freeze_recovery_contract(doc, source, settings),
        }
        db.session.commit()
        principal = resolve_access_key(
            issue_access_key(
                1, "recovery", ["mcp.work", "mcp.doc_processor"], []
            ).raw_token
        )
        db.session.commit()
        queue = TaskQueue(application.instance_path, "doc_processor")
        payload = {
            "document_id": doc.id,
            "user_id": 1,
            "mcp_execution_id": execution_id,
            "mcp_execution_mode": "passive",
            "engine_id": "llm_vision",
        }
        job_id = "initial"
        claims = []
        preserved = None
        try:
            with patch(
                "litellm.completion", side_effect=AssertionError("No managed provider")
            ):
                for _ in range(15):
                    try:
                        result = worker.process_document(payload, queue, job_id)
                    except ExternalWorkPending:
                        result = None
                    db.session.expire_all()
                    doc = db.session.get(ProcessedDocument, "synthetic")
                    if result and result.get("suspended"):
                        assert failed
                        assert (
                            db.session.get(McpExecution, execution_id).status
                            == "suspended"
                        )
                        preserved = {
                            item.id: (
                                item.request_fingerprint,
                                item.accepted_response_json,
                            )
                            for item in McpWorkItem.query.filter_by(
                                execution_id=execution_id, state="completed"
                            )
                        }
                        assert (
                            len(preserved) == 3
                        )  # Metadata, headings, first two pages.
                        checkpoint = (doc.settings or {}).get("pdf_state", {})
                        assert sum(
                            value is not None
                            for value in checkpoint.get("html_parts", [])
                        ) == (2 if failure_page == 2 else 0)
                        # Dispose all parser, ORM and queue state before recovering.
                        queue.close()
                        db.session.remove()
                        db.engine.dispose()
                        queue = TaskQueue(application.instance_path, "doc_processor")
                        context = SimpleNamespace(
                            ensure_worker_pool=lambda *a, **k: None,
                            get_task_queue=lambda addon, current_queue=queue: (
                                current_queue
                            ),
                        )
                        if failure_page == 2:
                            with (
                                application.test_request_context(
                                    "/process", method="POST", json={}
                                ),
                                patch.object(
                                    routes, "current_user", SimpleNamespace(id=1)
                                ),
                                patch.object(
                                    routes.blueprint,
                                    "addon_context",
                                    context,
                                    create=True,
                                ),
                            ):
                                response, code = routes.process_document.__wrapped__(
                                    "synthetic"
                                )
                                assert code == 202
                                resumed = response.get_json()
                                repeated, code = routes.process_document.__wrapped__(
                                    "synthetic"
                                )
                                assert code == 202
                                assert (
                                    repeated.get_json()["job_id"]
                                    == resumed["queue_job_id"]
                                )
                        else:
                            resumed = resume_execution(
                                DocProcessorMcpService(context, 1),
                                execution_id,
                                "resume-after-child-exit",
                            )
                        assert resumed["execution"]["id"] == execution_id
                        job_id = resumed["queue_job_id"]
                        envelope = queue.queue.popleft()
                        assert envelope["job_id"] == job_id
                        payload = envelope["payload"]
                        continue
                    if doc.status == "completed":
                        html = doc.output_html
                        assert all(
                            html.count(f"Synthetic passage number {n}.") == 1
                            for n in (1, 2, 3)
                        )
                        break
                    claim = claim_next_work(
                        principal, "recovery-worker", execution_id=execution_id
                    )
                    assert claim is not None
                    packet = claim.request
                    schema = packet["response_format"]["json_schema"]["schema"]
                    claims.append((claim.work_item_id, packet["source"]))
                    if packet["source"] == "doc_processor_pdf_batch":
                        numbers = schema["properties"]["pages"]["items"]["properties"][
                            "page_number"
                        ]["enum"]
                        response = {
                            "pages": [
                                {
                                    "page_number": n,
                                    "html": f"<main_content><p>Synthetic passage number {n}.</p></main_content><footnotes></footnotes>",
                                }
                                for n in numbers
                            ]
                        }
                        accepted_first = True
                    else:
                        response = _neutral(schema)
                        if packet["source"] == "doc_processor_pdf_metadata":
                            response.update(
                                title="Synthetic title",
                                table_of_contents_status="complete",
                                toc_page_request={"from_start": 0, "from_end": 0},
                            )
                    submitted = submit_work(
                        principal,
                        claim.work_item_id,
                        claim.lease_token,
                        {"content": response, "finish_reason": "stop"},
                    )
                    assert submitted["status"] == "accepted"
                    if failure_page == 0 and accepted_first and not failed:
                        # Cached images are disposable across a worker restart.
                        # Force regeneration before consuming the accepted batch.
                        for png in (
                            (doc.settings or {})
                            .get("pdf_state", {})
                            .get("png_paths", [])
                        ):
                            if png:
                                Path(png).unlink(missing_ok=True)
                else:
                    pytest.fail("Recovery did not finish within the bounded loop")
            assert failed and preserved is not None
            assert len(claims) == 4 and len({item[0] for item in claims}) == 4
            assert db.session.get(McpExecution, execution_id).status == "completed"
            for work_id, original in preserved.items():
                item = db.session.get(McpWorkItem, work_id)
                assert (
                    item.request_fingerprint,
                    item.accepted_response_json,
                ) == original
        finally:
            queue.close()
            db.session.remove()
            db.engine.dispose()
