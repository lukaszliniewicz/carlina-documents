from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from content_storage_support import bind_content_policy
from flask import Flask
from sqlalchemy import update
from sqlalchemy.orm.attributes import set_committed_value
from werkzeug.exceptions import NotFound

from iarf_assistant.addons.doc_processor import knowledge_adapter, routes
from iarf_assistant.addons.doc_processor.models import (
    DocumentMetadata,
    ProcessedDocument,
)
from iarf_assistant.extensions import db
from iarf_assistant.models import User


@pytest.fixture
def app(tmp_path):
    app = Flask(__name__, root_path=str(tmp_path), instance_path=str(tmp_path))
    bind_content_policy(app)
    app.config.update(
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{tmp_path / 'documents.sqlite'}",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SECRET_KEY="test",
        TESTING=True,
        CARLINA_UPLOADS_DIR=str(tmp_path / "uploads"),
    )
    db.init_app(app)
    with app.app_context():
        with db.engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA journal_mode=WAL")
            connection.commit()
        db.create_all()
        db.session.add_all(
            [
                User(id=1, email="owner@example.test", password_hash="test"),
                User(id=2, email="other@example.test", password_hash="test"),
            ]
        )
        db.session.commit()
        yield app
        db.session.remove()
        db.drop_all()
        db.engine.dispose()


def _document(app, *, status="completed"):
    source = Path(app.config["CARLINA_UPLOADS_DIR"]) / "1" / "source.pdf"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"synthetic source")
    document = ProcessedDocument(
        user_id=1,
        filename="source.pdf",
        file_path="/uploads/1/source.pdf",
        status=status,
        settings={
            "document_metadata": {"title": "Settings title"},
            "preserved_setting": "unchanged",
            "knowledge_sync_status": "ready",
        },
        output_html="<p>Before</p>",
        output_markdown="Before",
        error_message="old error",
    )
    document.metadata_record = DocumentMetadata(title="Database title")
    db.session.add(document)
    db.session.commit()
    return document


def _put(app, document_id, *, user_id=1, body=None, headers=None):
    with (
        app.test_request_context(
            f"/addons/doc_processor/api/documents/{document_id}/content",
            method="PUT",
            json=body,
            headers=headers,
        ),
        patch.object(routes, "current_user", SimpleNamespace(id=user_id)),
    ):
        result = routes.update_document_content.__wrapped__(document_id)
    if isinstance(result, tuple):
        return result
    return result, result.status_code


def _revision(document):
    db.session.refresh(document)
    return routes._document_revision(document)


def test_content_put_requires_a_nonempty_revision(app):
    document = _document(app)
    previous_revision = _revision(document)

    response, status_code = _put(
        app,
        document.id,
        body={"output_html": "<p>Edited</p>"},
    )

    assert status_code == 428
    assert response.get_json() == {
        "error": "Reload the document before saving corrections.",
        "code": "revision_required",
    }
    assert _revision(document) == previous_revision
    assert document.output_html == "<p>Before</p>"


@pytest.mark.parametrize(
    ("revision", "headers"),
    [
        ("   ", None),
        (None, {"If-Match": "  "}),
    ],
)
def test_content_put_treats_blank_revisions_as_missing(app, revision, headers):
    document = _document(app)
    body = {"output_html": "<p>Edited</p>"}
    if revision is not None:
        body["expected_updated_at"] = revision

    response, status_code = _put(app, document.id, body=body, headers=headers)

    assert status_code == 428
    assert response.get_json()["code"] == "revision_required"
    assert document.output_html == "<p>Before</p>"


@pytest.mark.parametrize(
    ("revision", "headers"),
    [
        (42, None),
        ("r" * 129, None),
        (None, {"If-Match": "r" * 129}),
    ],
)
def test_content_put_rejects_malformed_or_oversized_revisions(app, revision, headers):
    document = _document(app)
    body = {"output_html": "<p>Edited</p>"}
    if revision is not None:
        body["expected_updated_at"] = revision

    _response, status_code = _put(app, document.id, body=body, headers=headers)

    assert status_code == 400
    assert document.output_html == "<p>Before</p>"


@pytest.mark.parametrize(
    ("output_html", "expected_status"),
    [(42, 400), ("0123456789", 413)],
)
def test_content_put_rejects_invalid_html_without_mutation(
    app, output_html, expected_status
):
    document = _document(app)
    previous_revision = _revision(document)
    previous_settings = dict(document.settings)

    with (
        patch.object(routes, "MAX_EDITED_HTML_BYTES", 8),
        patch.object(knowledge_adapter, "sync_processed_document_source") as sync,
    ):
        _response, status_code = _put(
            app,
            document.id,
            body={
                "expected_updated_at": previous_revision,
                "output_html": output_html,
            },
        )

    assert status_code == expected_status
    db.session.refresh(document)
    assert document.updated_at.isoformat() == previous_revision
    assert document.output_html == "<p>Before</p>"
    assert document.settings == previous_settings
    assert document.metadata_record.title == "Database title"
    sync.assert_not_called()


def test_content_put_success_advances_revision_and_keeps_semantic_sync(app):
    document = _document(app)
    previous_revision = _revision(document)
    sync = patch.object(knowledge_adapter, "sync_processed_document_source")

    with sync as sync_source:
        response, status_code = _put(
            app,
            document.id,
            body={
                "expected_updated_at": previous_revision,
                "output_html": "<h1>Edited title</h1><p>Edited body.</p>",
            },
        )

    assert status_code == 200
    payload = response.get_json()
    db.session.refresh(document)
    assert payload["output_html"] == document.output_html
    assert "Edited title" in document.output_html
    assert "Edited body." in document.output_markdown
    assert document.updated_at.isoformat() != previous_revision
    assert payload["updated_at"] == document.updated_at.isoformat()
    assert document.status == "completed"
    assert document.error_message is None
    assert document.settings["metadata_stale"] is True
    assert document.settings["preserved_setting"] == "unchanged"
    assert document.settings["document_metadata"]["title"] == "Settings title"
    assert document.metadata_record.title == "Database title"
    sync_source.assert_called_once_with(document)


def test_content_put_rolls_back_claim_when_knowledge_sync_raises(app):
    document = _document(app)
    previous_revision = _revision(document)
    previous_settings = dict(document.settings)

    with (
        patch.object(
            knowledge_adapter,
            "sync_processed_document_source",
            side_effect=RuntimeError("knowledge sync failed"),
        ),
        pytest.raises(RuntimeError, match="knowledge sync failed"),
    ):
        _put(
            app,
            document.id,
            body={
                "expected_updated_at": previous_revision,
                "output_html": "<p>Edited before sync failure</p>",
            },
        )

    assert not db.session().in_transaction()
    db.session.refresh(document)
    assert document.updated_at.isoformat() == previous_revision
    assert document.output_html == "<p>Before</p>"
    assert document.output_markdown == "Before"
    assert document.settings == previous_settings
    assert document.status == "completed"
    assert document.error_message == "old error"
    assert document.metadata_record.title == "Database title"


def test_content_put_rolls_back_when_claim_raises_after_advancing_revision(app):
    document = _document(app)
    previous_revision = _revision(document)
    previous_settings = dict(document.settings)
    claim = routes._claim_metadata_mutation

    def claim_then_raise(current_document, data, **kwargs):
        claimed_document, conflict = claim(current_document, data, **kwargs)
        assert conflict is None
        assert routes._document_revision(claimed_document) != previous_revision
        raise RuntimeError("claim follow-up failed")

    with (
        patch.object(routes, "_claim_metadata_mutation", side_effect=claim_then_raise),
        pytest.raises(RuntimeError, match="claim follow-up failed"),
    ):
        _put(
            app,
            document.id,
            body={
                "expected_updated_at": previous_revision,
                "output_html": "<p>Should never be written</p>",
            },
        )

    assert not db.session().in_transaction()
    db.session.refresh(document)
    assert document.updated_at.isoformat() == previous_revision
    assert document.output_html == "<p>Before</p>"
    assert document.output_markdown == "Before"
    assert document.settings == previous_settings
    assert document.status == "completed"
    assert document.error_message == "old error"
    assert document.metadata_record.title == "Database title"


def test_content_put_accepts_the_if_match_revision_header(app):
    document = _document(app)
    previous_revision = _revision(document)

    with patch.object(knowledge_adapter, "sync_processed_document_source"):
        response, status_code = _put(
            app,
            document.id,
            body={"output_html": "<p>Header edit</p>"},
            headers={"If-Match": f'"{previous_revision}"'},
        )

    assert status_code == 200
    assert "Header edit" in response.get_json()["output_html"]


def test_second_content_put_with_stale_revision_keeps_the_winner(app):
    document = _document(app)
    stale_revision = _revision(document)

    with patch.object(knowledge_adapter, "sync_processed_document_source"):
        first_response, first_status = _put(
            app,
            document.id,
            body={
                "expected_updated_at": stale_revision,
                "output_html": "<p>Winning edit</p>",
            },
        )
    assert first_status == 200
    winning_revision = first_response.get_json()["updated_at"]

    with patch.object(knowledge_adapter, "sync_processed_document_source") as sync:
        response, status_code = _put(
            app,
            document.id,
            body={
                "expected_updated_at": stale_revision,
                "output_html": "<p>Stale overwrite</p>",
            },
        )

    assert status_code == 409
    payload = response.get_json()
    assert payload["code"] == "revision_conflict"
    assert payload["error"] == (
        "The document changed while you were editing. Your corrections have not "
        "been saved. Reload the current document before saving."
    )
    assert payload["document"]["updated_at"] == winning_revision
    assert "Winning edit" in payload["document"]["output_html"]
    db.session.refresh(document)
    assert "Winning edit" in document.output_html
    assert "Stale overwrite" not in document.output_html
    sync.assert_not_called()


def test_content_put_rejects_active_processing_status(app):
    document = _document(app, status="processing")
    previous_revision = _revision(document)

    response, status_code = _put(
        app,
        document.id,
        body={
            "expected_updated_at": previous_revision,
            "output_html": "<p>Edited</p>",
        },
    )

    assert status_code == 409
    payload = response.get_json()
    assert payload["code"] == "revision_conflict"
    assert payload["document"]["status"] == "processing"
    db.session.refresh(document)
    assert document.updated_at.isoformat() == previous_revision
    assert document.output_html == "<p>Before</p>"
    assert document.status == "processing"


def test_invalid_semantic_html_leaves_document_metadata_and_knowledge_untouched(app):
    document = _document(app)
    previous_revision = _revision(document)
    previous_settings = dict(document.settings)
    previous_metadata_title = document.metadata_record.title

    with patch.object(knowledge_adapter, "sync_processed_document_source") as sync:
        _response, status_code = _put(
            app,
            document.id,
            body={
                "expected_updated_at": previous_revision,
                "output_html": '<p id="duplicate">First</p>'
                '<p id="duplicate">Second</p>',
            },
        )

    assert status_code == 400
    db.session.refresh(document)
    assert document.updated_at.isoformat() == previous_revision
    assert document.output_html == "<p>Before</p>"
    assert document.output_markdown == "Before"
    assert document.settings == previous_settings
    assert document.metadata_record.title == previous_metadata_title
    sync.assert_not_called()


def test_content_put_owner_isolation_returns_not_found(app):
    document = _document(app)

    with pytest.raises(NotFound) as error:
        _put(
            app,
            document.id,
            user_id=2,
            body={
                "expected_updated_at": routes._document_revision(document),
                "output_html": "<p>Not owned</p>",
            },
        )

    assert error.value.code == 404
    db.session.refresh(document)
    assert document.output_html == "<p>Before</p>"


def test_content_put_cas_rejects_change_after_document_was_loaded(app):
    document = _document(app)
    stale_revision = _revision(document)
    expected_loaded_timestamp = document.updated_at
    winner_timestamp = expected_loaded_timestamp + timedelta(seconds=1)

    canonicalize = routes.canonicalize_document_html
    changed = False

    def write_concurrent_winner(content, **kwargs):
        nonlocal changed
        if not changed:
            changed = True
            session_connection = db.session.connection().connection.driver_connection
            independent_connection = db.engine.connect()
            try:
                assert (
                    independent_connection.connection.driver_connection
                    is not session_connection
                )
                db.session.rollback()
                with independent_connection.begin():
                    independent_connection.execute(
                        update(ProcessedDocument)
                        .where(ProcessedDocument.id == document.id)
                        .values(
                            updated_at=winner_timestamp,
                            output_html="<p>Independent winner</p>",
                            output_markdown="Independent winner",
                        )
                    )
            finally:
                independent_connection.close()
            set_committed_value(document, "updated_at", expected_loaded_timestamp)
        return canonicalize(content, **kwargs)

    with patch.object(
        routes, "canonicalize_document_html", side_effect=write_concurrent_winner
    ):
        response, status_code = _put(
            app,
            document.id,
            body={
                "expected_updated_at": stale_revision,
                "output_html": "<p>Stale request</p>",
            },
        )

    assert status_code == 409
    payload = response.get_json()
    assert payload["code"] == "revision_conflict"
    assert payload["document"]["updated_at"] == winner_timestamp.isoformat()
    assert payload["document"]["output_html"] == "<p>Independent winner</p>"
    db.session.refresh(document)
    assert document.updated_at == winner_timestamp
    assert document.output_html == "<p>Independent winner</p>"
