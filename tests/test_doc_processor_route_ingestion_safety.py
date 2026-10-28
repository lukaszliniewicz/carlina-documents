import io
import tempfile
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from content_storage_support import bind_content_policy
from flask import Flask

from iarf_assistant.addons.doc_processor import routes
from iarf_assistant.addons.doc_processor.models import (
    DocumentProject,
    ProcessedDocument,
)
from iarf_assistant.extensions import db
from iarf_assistant.models import User


class _Queue:
    def get_status(self, _job_id):
        return {"status": "queued"}


def _app(temp_dir):
    app = Flask(__name__, root_path=temp_dir, instance_path=temp_dir)
    bind_content_policy(app)
    app.config.update(
        SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SECRET_KEY="test",
        TESTING=True,
        CARLINA_UPLOADS_DIR=str(Path(temp_dir) / "uploads"),
    )
    db.init_app(app)
    return app


def _create_source(temp_dir, *, filename):
    source = Path(temp_dir) / "uploads" / "1" / filename
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"source")
    return f"/uploads/1/{filename}"


def _cleanup(app):
    db.session.remove()
    db.drop_all()
    db.engine.dispose()


def test_sparse_process_retry_preserves_book_quality_snapshot_and_runtime_markers():
    with tempfile.TemporaryDirectory() as temp_dir:
        app = _app(temp_dir)
        with app.app_context():
            db.create_all()
            db.session.add(User(id=1, email="route@example.test", password_hash="test"))
            document = ProcessedDocument(
                user_id=1,
                filename="book.pdf",
                file_path=_create_source(temp_dir, filename="book.pdf"),
                status="error",
                settings={
                    "engine_id": "llm_vision",
                    "requested_engine_id": "auto",
                    "processing_profile": "book_quality",
                    "parsing_model": "provider/book-parser",
                    "metadata_model": "provider/book-meta",
                    "pdf_options": {
                        "evaluate": False,
                        "metadata_pages": 24,
                        "metadata_end_pages": 12,
                        "append_previous": True,
                        "toc_handling": "preserve",
                    },
                    "opaque_runtime_key": {"keep": "me"},
                },
            )
            db.session.add(document)
            db.session.commit()
            original = dict(document.settings)

            captured_settings = []
            with (
                app.test_request_context(
                    f"/addons/doc_processor/api/documents/{document.id}/process",
                    method="POST",
                    json={},
                ),
                patch.object(routes, "current_user", SimpleNamespace(id=1)),
                patch.object(
                    routes, "validate_pdf_model_readiness", return_value=None
                ) as readiness,
                patch.object(routes, "_get_task_queue", return_value=_Queue()),
                patch.object(
                    routes, "_enqueue_job", return_value="job-book"
                ) as enqueue,
            ):
                readiness.side_effect = lambda _filename, settings, **_kwargs: (
                    captured_settings.append(deepcopy(settings)) or None
                )
                response = routes.process_document.__wrapped__(document.id)

            assert response.status_code == 200
            readiness.assert_called_once()
            assert readiness.call_args.args[0] == document.file_path
            assert readiness.call_args.kwargs == {"user_id": 1}
            assert captured_settings == [original]
            enqueue.assert_called_once_with(
                "process_document",
                {"document_id": document.id, "engine_id": "llm_vision"},
                num_workers=1,
            )
            assert document.settings["processing_profile"] == "book_quality"
            assert document.settings["parsing_model"] == "provider/book-parser"
            assert document.settings["metadata_model"] == "provider/book-meta"
            assert document.settings["pdf_options"] == original["pdf_options"]
            assert document.settings["opaque_runtime_key"] == {"keep": "me"}
            assert document.settings["active_job_id"] == "job-book"
            _cleanup(app)


def test_process_retry_applies_only_explicit_supported_overrides():
    with tempfile.TemporaryDirectory() as temp_dir:
        app = _app(temp_dir)
        with app.app_context():
            db.create_all()
            db.session.add(User(id=1, email="route@example.test", password_hash="test"))
            document = ProcessedDocument(
                user_id=1,
                filename="notes.txt",
                file_path=_create_source(temp_dir, filename="notes.txt"),
                status="error",
                settings={
                    "engine_id": "native",
                    "parsing_model": "provider/original-parser",
                    "metadata_model": "provider/original-meta",
                    "processing_profile": "custom",
                    "pdf_options": {
                        "evaluate": False,
                        "metadata_pages": 7,
                        "legacy_only": "keep",
                    },
                    "opaque_runtime_key": "preserved",
                },
            )
            db.session.add(document)
            db.session.commit()

            with (
                app.test_request_context(
                    f"/addons/doc_processor/api/documents/{document.id}/process",
                    method="POST",
                    json={
                        "parsing_model": "provider/retry-parser",
                        "evaluate_pdf": True,
                        "metadata_pages": 9,
                    },
                ),
                patch.object(routes, "current_user", SimpleNamespace(id=1)),
                patch.object(routes, "_get_task_queue", return_value=_Queue()),
                patch.object(routes, "_enqueue_job", return_value="job-custom"),
            ):
                response = routes.process_document.__wrapped__(document.id)

            assert response.status_code == 200
            assert document.settings["engine_id"] == "native"
            assert document.settings["parsing_model"] == "provider/retry-parser"
            assert document.settings["metadata_model"] == "provider/original-meta"
            assert document.settings["pdf_options"]["evaluate"] is True
            assert document.settings["pdf_options"]["metadata_pages"] == 9
            assert document.settings["pdf_options"]["legacy_only"] == "keep"
            assert document.settings["opaque_runtime_key"] == "preserved"
            _cleanup(app)


def test_sparse_retry_preserves_engine_options_and_reasoning_snapshot():
    with tempfile.TemporaryDirectory() as temp_dir:
        app = _app(temp_dir)
        with app.app_context():
            db.create_all()
            db.session.add(User(id=1, email="route@example.test", password_hash="test"))
            reasoning_snapshot = {
                "version": 1,
                "slots": {
                    "parsing": {"model_id": "provider/parser", "level": "high"},
                    "metadata": {"model_id": "provider/meta", "level": "high"},
                },
            }
            document = ProcessedDocument(
                user_id=1,
                filename="scan.pdf",
                file_path=_create_source(temp_dir, filename="scan.pdf"),
                status="error",
                settings={
                    "engine_id": "pymupdf",
                    "parsing_model": "provider/parser",
                    "metadata_model": "provider/meta",
                    "pdf_options": {"evaluate": False, "metadata_pages": 7},
                    "engine_options": {
                        "use_ocr": False,
                        "force_ocr": True,
                        "paginate_output": True,
                        "exclude_footers": False,
                        "embed_images": True,
                    },
                    "reasoning_snapshot": reasoning_snapshot,
                },
            )
            db.session.add(document)
            db.session.commit()

            with (
                app.test_request_context(
                    f"/addons/doc_processor/api/documents/{document.id}/process",
                    method="POST",
                    json={"metadata_pages": 11},
                ),
                patch.object(routes, "current_user", SimpleNamespace(id=1)),
                patch.object(routes, "_get_task_queue", return_value=_Queue()),
                patch.object(routes, "_enqueue_job", return_value="job-engine"),
            ):
                response = routes.process_document.__wrapped__(document.id)

            assert response.status_code == 200
            assert document.settings["engine_id"] == "pymupdf"
            assert document.settings["engine_options"] == {
                "use_ocr": False,
                "force_ocr": True,
                "paginate_output": True,
                "exclude_footers": False,
                "embed_images": True,
            }
            assert document.settings["reasoning_snapshot"] == reasoning_snapshot
            assert document.settings["pdf_options"] == {
                "evaluate": False,
                "metadata_pages": 11,
            }
            _cleanup(app)


def test_pdf_state_without_completed_page_is_not_treated_as_resumable():
    document = SimpleNamespace(
        file_path="/uploads/1/empty.pdf",
        settings={"pdf_state": {"html_parts": [None, "", "   "]}},
    )

    assert routes._has_resumable_pdf_state(document) is False
    assert routes._has_pdf_recovery_state(document) is False


def test_pdf_retry_with_partial_state_uses_regular_parser_and_ignores_browser_overrides():
    with tempfile.TemporaryDirectory() as temp_dir:
        app = _app(temp_dir)
        with app.app_context():
            db.create_all()
            db.session.add(User(id=1, email="route@example.test", password_hash="test"))
            state = {"html_parts": ["<main_content>page one</main_content>", None]}
            document = ProcessedDocument(
                user_id=1,
                filename="resume.pdf",
                file_path=_create_source(temp_dir, filename="resume.pdf"),
                status="error",
                settings={
                    "engine_id": "llm_vision",
                    "parsing_model": "provider/stored-parser",
                    "metadata_model": "provider/stored-meta",
                    "pdf_options": {"evaluate": False, "append_previous": True},
                    "pdf_state": state,
                },
            )
            db.session.add(document)
            db.session.commit()

            captured_settings = []
            with (
                app.test_request_context(
                    f"/addons/doc_processor/api/documents/{document.id}/process",
                    method="POST",
                    json={
                        "parsing_model": "provider/browser-choice",
                        "evaluate_pdf": True,
                    },
                ),
                patch.object(routes, "current_user", SimpleNamespace(id=1)),
                patch.object(
                    routes, "validate_pdf_model_readiness", return_value=None
                ) as readiness,
                patch.object(routes, "_get_task_queue", return_value=_Queue()),
                patch.object(
                    routes, "_enqueue_job", return_value="job-resume"
                ) as enqueue,
            ):
                readiness.side_effect = lambda _filename, settings, **_kwargs: (
                    captured_settings.append(deepcopy(settings)) or None
                )
                response = routes.process_document.__wrapped__(document.id)

            assert response.status_code == 200
            readiness.assert_called_once()
            assert readiness.call_args.args[0] == document.file_path
            assert readiness.call_args.kwargs == {"user_id": 1}
            assert captured_settings == [
                {
                    "engine_id": "llm_vision",
                    "parsing_model": "provider/stored-parser",
                    "metadata_model": "provider/stored-meta",
                    "pdf_options": {"evaluate": False, "append_previous": True},
                    "pdf_state": state,
                }
            ]
            enqueue.assert_called_once_with(
                "process_document",
                {"document_id": document.id, "engine_id": "llm_vision"},
                num_workers=1,
            )
            assert document.settings["parsing_model"] == "provider/stored-parser"
            assert document.settings["pdf_options"]["evaluate"] is False
            assert document.settings["pdf_state"] == state
            _cleanup(app)


def test_pdf_retry_after_finalization_error_uses_direct_resume_worker():
    with tempfile.TemporaryDirectory() as temp_dir:
        app = _app(temp_dir)
        with app.app_context():
            db.create_all()
            db.session.add(User(id=1, email="route@example.test", password_hash="test"))
            state = {
                "phase": "PAUSED_IMAGE_REVIEW",
                "included_pages": [0],
                "html_parts": ["<main_content>page one</main_content>"],
                "png_paths": [None],
                "boundary_flags": {},
            }
            document = ProcessedDocument(
                user_id=1,
                filename="resume.pdf",
                file_path=_create_source(temp_dir, filename="resume.pdf"),
                status="error",
                error_message="Completed output has invalid semantic references.",
                settings={
                    "engine_id": "llm_vision",
                    "parsing_model": "provider/stored-parser",
                    "pdf_options": {"page_range": "1"},
                    "pdf_state": state,
                },
            )
            db.session.add(document)
            db.session.commit()

            with (
                app.test_request_context(
                    f"/addons/doc_processor/api/documents/{document.id}/process",
                    method="POST",
                    json={},
                ),
                patch.object(routes, "current_user", SimpleNamespace(id=1)),
                patch.object(routes, "validate_pdf_model_readiness", return_value=None),
                patch.object(routes, "_get_task_queue", return_value=_Queue()),
                patch.object(
                    routes, "_enqueue_job", return_value="job-finalize"
                ) as enqueue,
            ):
                response = routes.process_document.__wrapped__(document.id)

            assert response.status_code == 200
            enqueue.assert_called_once_with(
                "resume_pdf_parsing",
                {"document_id": document.id, "engine_id": "llm_vision"},
                num_workers=1,
            )
            assert document.status == "processing"
            assert document.settings["pdf_state"] == state
            _cleanup(app)


def test_pdf_image_review_state_is_resumable_only_when_complete_and_explicitly_phased():
    partial = SimpleNamespace(
        file_path="/uploads/1/review.pdf",
        status="reviewing_images",
        settings={
            "pdf_state": {
                "phase": "PAUSED_IMAGE_REVIEW",
                "html_parts": ["<main_content>page one</main_content>", None],
            }
        },
    )
    complete = SimpleNamespace(
        file_path="/uploads/1/review.pdf",
        status="reviewing_images",
        settings={
            "pdf_state": {
                "phase": "PAUSED_IMAGE_REVIEW",
                "html_parts": [
                    "<main_content>page one</main_content>",
                    "<main_content>page two</main_content>",
                ],
            }
        },
    )
    finalization_error = SimpleNamespace(
        file_path="/uploads/1/review.pdf",
        status="error",
        settings=complete.settings,
    )
    unphased = SimpleNamespace(
        file_path="/uploads/1/review.pdf",
        status="reviewing_images",
        settings={
            "pdf_state": {"html_parts": complete.settings["pdf_state"]["html_parts"]}
        },
    )
    malformed_selection = SimpleNamespace(
        file_path="/uploads/1/review.pdf",
        status="reviewing_images",
        settings={
            "pdf_state": {
                "phase": "PAUSED_IMAGE_REVIEW",
                "included_pages": [0, 4],
                "html_parts": complete.settings["pdf_state"]["html_parts"],
            }
        },
    )

    assert routes._has_resumable_pdf_state(partial) is False
    assert routes._has_pdf_recovery_state(partial) is True
    assert routes._has_resumable_pdf_state(unphased) is False
    assert routes._has_resumable_pdf_state(malformed_selection) is False
    assert routes._has_resumable_pdf_state(complete) is True
    assert routes._has_resumable_pdf_state(finalization_error) is True
    assert routes._has_pdf_recovery_state(finalization_error) is False


def test_standalone_upload_removes_only_its_new_source_directory_after_metadata_failure():
    with tempfile.TemporaryDirectory() as temp_dir:
        app = _app(temp_dir)
        with app.app_context():
            db.create_all()
            db.session.add(User(id=1, email="route@example.test", password_hash="test"))
            db.session.commit()
            upload_root = Path(temp_dir) / "uploads"
            unrelated = (
                upload_root / "2" / "doc_processor_docs" / "keep" / "sentinel.txt"
            )
            unrelated.parent.mkdir(parents=True)
            unrelated.write_text("keep", encoding="utf-8")

            with (
                app.test_request_context(
                    "/addons/doc_processor/api/documents",
                    method="POST",
                    data={
                        "file": (io.BytesIO(b"source"), "notes.txt"),
                        "engine": "native",
                    },
                    content_type="multipart/form-data",
                ),
                patch.object(routes, "current_user", SimpleNamespace(id=1)),
                patch(
                    "iarf_assistant.core.model_router.ModelRouter.get_model_for_tier",
                    return_value="provider/default",
                ),
                patch.object(
                    routes,
                    "initialize_document_metadata",
                    side_effect=RuntimeError("metadata boom"),
                ),
            ):
                response, status_code = routes.upload_document.__wrapped__()

            assert status_code == 500
            assert (
                response.get_json()["error"] == "Unable to save the uploaded document."
            )
            created_root = upload_root / "1" / "doc_processor_docs"
            assert not created_root.exists() or not list(created_root.iterdir())
            assert unrelated.read_text(encoding="utf-8") == "keep"
            assert db.session.query(ProcessedDocument).count() == 0
            _cleanup(app)


def test_project_upload_aggregate_cap_rejects_before_writing_any_source_files():
    with tempfile.TemporaryDirectory() as temp_dir:
        app = _app(temp_dir)
        app.config["DOC_PROCESSOR_MAX_PROJECT_UPLOAD_BYTES"] = 5
        with app.app_context():
            db.create_all()
            user = User(id=1, email="route@example.test", password_hash="test")
            project = DocumentProject(user_id=1, name="Aggregate", settings={})
            db.session.add_all([user, project])
            db.session.commit()
            upload_root = Path(temp_dir) / "uploads"

            with (
                app.test_request_context(
                    f"/addons/doc_processor/api/projects/{project.id}/documents/upload",
                    method="POST",
                    data={
                        "files": [
                            (io.BytesIO(b"abc"), "first.txt"),
                            (io.BytesIO(b"def"), "second.txt"),
                        ],
                        "engine": "native",
                    },
                    content_type="multipart/form-data",
                ),
                patch.object(routes, "current_user", SimpleNamespace(id=1)),
                patch(
                    "iarf_assistant.core.model_router.ModelRouter.get_model_for_tier",
                    return_value="provider/default",
                ),
                patch.object(routes, "uploads_root", return_value=upload_root),
            ):
                response, status_code = routes.upload_project_documents.__wrapped__(
                    project.id
                )

            assert status_code == 413
            assert "aggregate" in response.get_json()["error"].lower()
            assert db.session.query(ProcessedDocument).count() == 0
            assert not upload_root.exists()
            _cleanup(app)
