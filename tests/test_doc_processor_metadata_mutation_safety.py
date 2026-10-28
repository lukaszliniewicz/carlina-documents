import tempfile
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from content_storage_support import bind_content_policy
from flask import Flask

from iarf_assistant.addons.doc_processor import routes
from iarf_assistant.addons.doc_processor.metadata_service import sync_document_metadata
from iarf_assistant.addons.doc_processor.models import ProcessedDocument
from iarf_assistant.extensions import db
from iarf_assistant.models import User


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


def _source(temp_dir, filename="source.pdf"):
    source = Path(temp_dir) / "uploads" / "1" / filename
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"source")
    return f"/uploads/1/{filename}"


def _document(temp_dir, *, status="completed", settings=None):
    document = ProcessedDocument(
        user_id=1,
        filename="source.pdf",
        file_path=_source(temp_dir),
        status=status,
        settings=settings or {"document_metadata": {"title": "Original"}},
    )
    db.session.add(document)
    db.session.commit()
    return document


def _cleanup():
    db.session.remove()
    db.drop_all()
    db.engine.dispose()


def _response_and_status(result):
    if isinstance(result, tuple):
        return result
    return result, result.status_code


def test_metadata_put_rejects_active_document_status():
    with tempfile.TemporaryDirectory() as temp_dir:
        app = _app(temp_dir)
        with app.app_context():
            db.create_all()
            db.session.add(
                User(id=1, email="metadata@example.test", password_hash="test")
            )
            document = _document(temp_dir, status="processing")

            with (
                app.test_request_context(
                    f"/addons/doc_processor/api/documents/{document.id}/metadata",
                    method="PUT",
                    json={"metadata": {"title": "Blocked"}},
                ),
                patch.object(routes, "current_user", SimpleNamespace(id=1)),
            ):
                response, status_code = _response_and_status(
                    routes.update_document_metadata.__wrapped__(document.id)
                )

            assert status_code == 409
            assert "processing is active" in response.get_json()["error"]
            assert document.settings["document_metadata"]["title"] == "Original"
            _cleanup()


def test_metadata_put_rejects_stale_expected_revision():
    with tempfile.TemporaryDirectory() as temp_dir:
        app = _app(temp_dir)
        with app.app_context():
            db.create_all()
            db.session.add(
                User(id=1, email="metadata@example.test", password_hash="test")
            )
            document = _document(temp_dir)

            with (
                app.test_request_context(
                    f"/addons/doc_processor/api/documents/{document.id}/metadata",
                    method="PUT",
                    json={
                        "metadata": {"title": "Blocked"},
                        "expected_updated_at": "2000-01-01T00:00:00",
                    },
                ),
                patch.object(routes, "current_user", SimpleNamespace(id=1)),
            ):
                response, status_code = _response_and_status(
                    routes.update_document_metadata.__wrapped__(document.id)
                )

            assert status_code == 409
            payload = response.get_json()
            assert payload["document"]["id"] == document.id
            assert document.settings["document_metadata"]["title"] == "Original"
            _cleanup()


def test_metadata_claim_compare_and_set_rejects_change_after_row_was_loaded():
    with tempfile.TemporaryDirectory() as temp_dir:
        app = _app(temp_dir)
        with app.app_context():
            db.create_all()
            db.session.add(
                User(id=1, email="metadata@example.test", password_hash="test")
            )
            document = _document(temp_dir)
            stale_revision = routes._document_revision(document)
            replacement_revision = document.updated_at + timedelta(seconds=1)
            ProcessedDocument.query.filter_by(id=document.id).update(
                {ProcessedDocument.updated_at: replacement_revision},
                synchronize_session=False,
            )
            db.session.flush()
            assert routes._document_revision(document) == stale_revision

            with (
                app.test_request_context(
                    f"/addons/doc_processor/api/documents/{document.id}/metadata",
                    method="PUT",
                    json={
                        "metadata": {"title": "Should not win"},
                        "expected_updated_at": stale_revision,
                    },
                ),
                patch.object(routes, "current_user", SimpleNamespace(id=1)),
            ):
                response, status_code = _response_and_status(
                    routes.update_document_metadata.__wrapped__(document.id)
                )

            assert status_code == 409
            assert "changed while this edit was in progress" in response.get_json()[
                "error"
            ]
            db.session.refresh(document)
            assert document.settings["document_metadata"]["title"] == "Original"
            _cleanup()


def test_metadata_reextract_rejects_active_document_status():
    with tempfile.TemporaryDirectory() as temp_dir:
        app = _app(temp_dir)
        with app.app_context():
            db.create_all()
            db.session.add(
                User(id=1, email="metadata@example.test", password_hash="test")
            )
            document = _document(temp_dir, status="processing")

            with (
                app.test_request_context(
                    f"/addons/doc_processor/api/documents/{document.id}/metadata/reextract",
                    method="POST",
                    json={},
                ),
                patch.object(routes, "current_user", SimpleNamespace(id=1)),
            ):
                response, status_code = _response_and_status(
                    routes.reextract_document_metadata.__wrapped__(document.id)
                )

            assert status_code == 409
            assert "processing is active" in response.get_json()["error"]
            _cleanup()


class _FakePdfDocument:
    def __len__(self):
        return 1

    def close(self):
        pass


def _fake_parser_class(on_extract=None):
    class FakeParser:
        def __init__(self, _path, _user_id, _llm_service, *, settings):
            self.settings = dict(settings)
            self.document_metadata = {}

        def _build_page_side_map(self, _included_pages, *, parity_pages):
            return {}

        def _extract_document_metadata(self, *_args):
            if on_extract:
                on_extract()
            self.settings["document_metadata"] = {"title": "Provider title"}

    return FakeParser


def test_metadata_reextract_preserves_worker_and_recovery_settings():
    with tempfile.TemporaryDirectory() as temp_dir:
        app = _app(temp_dir)
        with app.app_context():
            db.create_all()
            db.session.add(
                User(id=1, email="metadata@example.test", password_hash="test")
            )
            retained_state = {"html_parts": ["<main_content>done</main_content>", None]}
            document = _document(
                temp_dir,
                settings={
                    "engine_id": "llm_vision",
                    "progress": 41,
                    "progress_msg": "Worker progress",
                    "pdf_state": retained_state,
                    "document_metadata": {"title": "Original"},
                },
            )

            with (
                app.test_request_context(
                    f"/addons/doc_processor/api/documents/{document.id}/metadata/reextract",
                    method="POST",
                    json={"expected_updated_at": routes._document_revision(document)},
                ),
                patch.object(routes, "current_user", SimpleNamespace(id=1)),
                patch.object(
                    routes,
                    "fitz",
                    SimpleNamespace(open=lambda _path: _FakePdfDocument()),
                ),
                patch(
                    "iarf_assistant.addons.doc_processor.parsers.pdf_parser.PdfParser",
                    _fake_parser_class(),
                ),
                patch("iarf_assistant.llm_service.LLMService"),
                patch(
                    "iarf_assistant.core.model_router.ModelRouter.get_model_for_tier",
                    return_value="model",
                ),
            ):
                _response, status_code = _response_and_status(
                    routes.reextract_document_metadata.__wrapped__(document.id)
                )

            assert status_code == 200
            assert document.settings["document_metadata"]["title"] == "Provider title"
            assert document.settings["engine_id"] == "llm_vision"
            assert document.settings["progress"] == 41
            assert document.settings["progress_msg"] == "Worker progress"
            assert document.settings["pdf_state"] == retained_state
            _cleanup()


def test_metadata_reextract_conflicts_with_manual_update_during_provider_call():
    with tempfile.TemporaryDirectory() as temp_dir:
        app = _app(temp_dir)
        with app.app_context():
            db.create_all()
            db.session.add(
                User(id=1, email="metadata@example.test", password_hash="test")
            )
            document = _document(temp_dir)
            expected_revision = routes._document_revision(document)

            def manual_update():
                sync_document_metadata(document, {"title": "Manual correction"})
                db.session.commit()

            with (
                app.test_request_context(
                    f"/addons/doc_processor/api/documents/{document.id}/metadata/reextract",
                    method="POST",
                    json={"expected_updated_at": expected_revision},
                ),
                patch.object(routes, "current_user", SimpleNamespace(id=1)),
                patch.object(
                    routes,
                    "fitz",
                    SimpleNamespace(open=lambda _path: _FakePdfDocument()),
                ),
                patch(
                    "iarf_assistant.addons.doc_processor.parsers.pdf_parser.PdfParser",
                    _fake_parser_class(manual_update),
                ),
                patch("iarf_assistant.llm_service.LLMService"),
                patch(
                    "iarf_assistant.core.model_router.ModelRouter.get_model_for_tier",
                    return_value="model",
                ),
            ):
                _response, status_code = _response_and_status(
                    routes.reextract_document_metadata.__wrapped__(document.id)
                )

            assert status_code == 409
            db.session.refresh(document)
            assert (
                document.settings["document_metadata"]["title"] == "Manual correction"
            )
            _cleanup()
