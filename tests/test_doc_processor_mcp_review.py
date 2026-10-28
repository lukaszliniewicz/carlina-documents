from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from bs4 import BeautifulSoup
from content_storage_support import bind_content_policy
from flask import Flask
from sqlalchemy import text

from carlina.runtime_profiles import ContentPolicy, RuntimeProfile
from iarf_assistant.addons.doc_processor import mcp_review
from iarf_assistant.addons.doc_processor.document_review import DocumentReview
from iarf_assistant.addons.doc_processor.mcp_service import (
    DocProcessorMcpService,
    DocProcessorMcpServiceError,
)
from iarf_assistant.addons.doc_processor.mcp_settings import DocumentReviewEdit
from iarf_assistant.addons.doc_processor.models import ProcessedDocument
from iarf_assistant.addons.doc_processor.semantic_document import (
    canonicalize_document_html,
    semantic_settings,
)
from iarf_assistant.core.encryption import EncryptionService, InstallationKeyStore
from iarf_assistant.core.mcp_gateway import _mcp_image_result
from iarf_assistant.extensions import db
from iarf_assistant.models import User

DocumentReviewService = mcp_review.DocumentReviewService


def _topology_fixture(app):
    document = db.session.get(ProcessedDocument, "doc")
    semantic = canonicalize_document_html(
        '<main><span class="pagebreak" id="doc-page-70"></span>'
        '<p id="body">A citation<sup>29</sup>.</p>'
        '<ol start="29"><li id="body29">The local note.</li></ol></main>',
        user_id=1,
    )
    assert not semantic.errors
    document.output_html = semantic.html
    document.settings = semantic_settings({}, semantic)
    db.session.commit()
    api = service(app)
    refs = api.read("doc", "note_references")
    note = api.read("doc", "search", query="local note")["items"][0]["element_id"]
    return api, semantic.html, {
        "operations": [
            {"action": "promote_note", "element_id": note, "label": "29", "scope_id": "chapter-4"},
            {"action": "link_reference", "element_id": refs["items"][0]["element_id"],
             "marker_index": 0, "note_element_id": note,
             "expected_label": "29", "expected_href": None},
        ],
        "expected_revision": refs["revision"], "idempotency_key": "topology-29",
    }


def test_note_topology_preview_save_retry_and_exact_undo(review_app):
    with review_app.app_context():
        api, original, args = _topology_fixture(review_app)
        preview = api.repair_note_topology("doc", **args)
        assert preview["changed"] and preview["dry_run"]
        assert len(preview["note_topology_repairs"]) == 2
        assert db.session.get(ProcessedDocument, "doc").output_html == original
        assert api.history("doc")["items"] == []
        saved = api.repair_note_topology("doc", **args, dry_run=False)
        assert saved["existing_translator_snapshots_changed"] is False
        assert saved["edit_results"][0]["operation"] == "repair_note_topology"
        assert api.read("doc", "notes")["items"][0]["reference_count"] == 1
        assert api.repair_note_topology("doc", **args, dry_run=False)["duplicate"]
        with pytest.raises(DocProcessorMcpServiceError) as conflict:
            altered = [dict(item) for item in args["operations"]]
            altered[0]["scope_id"] = "different"
            api.repair_note_topology("doc", **{**args, "operations": altered}, dry_run=False)
        assert conflict.value.code == "idempotency_conflict"
        api.undo("doc", change_id=saved["change_id"], expected_revision=saved["revision"],
                 idempotency_key="undo-topology", dry_run=False)
        assert db.session.get(ProcessedDocument, "doc").output_html == original


def test_note_topology_owner_stale_revision_and_failed_publication(review_app, monkeypatch):
    with review_app.app_context():
        api, original, args = _topology_fixture(review_app)
        with pytest.raises(DocProcessorMcpServiceError) as foreign:
            service(review_app, 2).repair_note_topology("doc", **args, dry_run=False)
        assert foreign.value.code == "not_found"
        with pytest.raises(DocProcessorMcpServiceError) as stale:
            api.repair_note_topology("doc", **{**args, "expected_revision": "0" * 64})
        assert stale.value.code == "revision_conflict"

        def fail(*_args, **_kwargs):
            raise RuntimeError("synthetic publication failure")

        monkeypatch.setattr(mcp_review, "sync_processed_document_source", fail)
        with pytest.raises(RuntimeError, match="synthetic publication failure"):
            api.repair_note_topology("doc", **args, dry_run=False)
        db.session.rollback()
        assert db.session.get(ProcessedDocument, "doc").output_html == original
        assert api.history("doc")["items"] == []


def _note_repair_fixture(app):
    document = db.session.get(ProcessedDocument, "doc")
    semantic = canonicalize_document_html(
        '<main><span id="doc-page-1" class="pagebreak"></span>'
        '<h2 id="chapter">Chapter</h2><p id="p">A re-acquired item.</p></main>'
        '<section role="doc-endnotes"><aside id="note7" role="doc-endnote">A note.</aside></section>',
        user_id=1,
    )
    assert not semantic.errors
    document.output_html = semantic.html
    document.settings = semantic_settings({}, semantic)
    db.session.commit()
    api = service(app)
    body = api.read("doc", "search", query="re-acquired")
    notes = api.read("doc", "notes")
    return api, semantic.html, {
        "element_id": body["items"][0]["element_id"],
        "note_element_id": notes["items"][0]["element_id"],
        "after_text": "re-acquired", "label": "7",
        "expected_revision": body["revision"], "idempotency_key": "repair-note7",
    }


def test_note_reference_repair_preview_save_retry_and_exact_undo(review_app):
    with review_app.app_context():
        api, original, args = _note_repair_fixture(review_app)
        preview = api.repair_note_reference("doc", **args)
        assert preview["dry_run"] and preview["changed"]
        assert db.session.get(ProcessedDocument, "doc").output_html == original
        assert api.history("doc")["items"] == []
        saved = api.repair_note_reference("doc", **args, dry_run=False)
        assert saved["edit_results"][0]["operation"] == "repair_note_reference"
        assert saved["undo_available"]
        assert saved["existing_translator_snapshots_changed"] is False
        assert api.repair_note_reference("doc", **args, dry_run=False)["duplicate"]
        notes = api.read("doc", "notes")
        assert notes["items"][0]["reference_count"] == 1
        api.undo(
            "doc", change_id=saved["change_id"], expected_revision=saved["revision"],
            idempotency_key="undo-note7", dry_run=False,
        )
        assert db.session.get(ProcessedDocument, "doc").output_html == original


def test_note_reference_repair_owner_revision_and_failed_publication(review_app, monkeypatch):
    with review_app.app_context():
        api, original, args = _note_repair_fixture(review_app)
        with pytest.raises(DocProcessorMcpServiceError) as foreign:
            service(review_app, 2).repair_note_reference("doc", **args, dry_run=False)
        assert foreign.value.code == "not_found"
        with pytest.raises(DocProcessorMcpServiceError) as stale:
            api.repair_note_reference("doc", **{**args, "expected_revision": "0" * 64})
        assert stale.value.code == "revision_conflict"

        def fail(*_args, **_kwargs):
            raise RuntimeError("synthetic publication failure")

        monkeypatch.setattr(mcp_review, "sync_processed_document_source", fail)
        with pytest.raises(RuntimeError, match="synthetic publication failure"):
            api.repair_note_reference("doc", **args, dry_run=False)
        db.session.rollback()
        assert db.session.get(ProcessedDocument, "doc").output_html == original
        assert api.history("doc")["items"] == []


@pytest.fixture
def review_app(tmp_path, request):
    app = Flask(__name__)
    if getattr(request, "param", "encrypted") == "plaintext":
        bind_content_policy(
            app,
            profile=RuntimeProfile.DOCUMENTS,
            content_policy=ContentPolicy.PLAINTEXT_V1,
            legacy=False,
        )
    else:
        bind_content_policy(app)
    app.config.update(
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{tmp_path / 'review.db'}",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        TESTING=True,
        SECRET_KEY="test",
        CARLINA_UPLOADS_DIR=str(tmp_path / "uploads"),
    )
    db.init_app(app)
    app.extensions["carlina_encryption"] = EncryptionService(
        InstallationKeyStore.ephemeral()
    )
    with app.app_context():
        db.create_all()
        db.session.add_all(
            [
                User(id=i, email=f"review{i}@example.test", password_hash="x")
                for i in (1, 2)
            ]
        )
        semantic = canonicalize_document_html(
            '<main><span class="pagebreak" id="doc-page-1" role="doc-pagebreak" title="1"></span>'
            '<h1 id="title">A synthetic book</h1><h3 id="chapter">Chapter</h3>'
            '<p id="p1">A first fragment<span class="pagebreak" id="doc-page-2" role="doc-pagebreak" title="2"></span> and its ending.</p>'
            '<p id="p2">A second paragraph.</p></main>',
            user_id=1,
        )
        db.session.add(
            ProcessedDocument(
                id="doc",
                user_id=1,
                filename="synthetic.pdf",
                file_path=str(tmp_path / "source.pdf"),
                status="completed",
                output_html=semantic.html,
                settings=semantic_settings({}, semantic),
            )
        )
        db.session.commit()
    yield app
    with app.app_context():
        db.session.remove()
        db.drop_all()
        db.engine.dispose()


def service(app, owner=1):
    return DocumentReviewService(
        DocProcessorMcpService(SimpleNamespace(application=app), owner)
    )


def heading_edit(app, level=2):
    evidence = service(app).read("doc", "outline")
    chapter = next(item for item in evidence["headings"] if item["text"] == "Chapter")
    return evidence["revision"], DocumentReviewEdit(
        element_id=chapter["element_id"], heading_level=level
    )


def test_read_dry_run_apply_and_retry_keep_one_publication(review_app):
    with review_app.app_context():
        api = service(review_app)
        revision, edit = heading_edit(review_app)
        before = db.session.get(ProcessedDocument, "doc").output_html
        preview = api.apply(
            "doc",
            expected_revision=revision,
            idempotency_key="heading-1",
            edits=[edit],
            dry_run=True,
        )
        assert (
            preview["revision"] == revision and preview["proposed_revision"] != revision
        )
        assert db.session.get(ProcessedDocument, "doc").output_html == before
        saved = api.apply(
            "doc", expected_revision=revision, idempotency_key="heading-1", edits=[edit]
        )
        assert (
            saved["changed"]
            and saved["metadata_stale"]
            and saved["exports_need_regeneration"]
        )
        document = db.session.get(ProcessedDocument, "doc")
        version = document.settings["knowledge_source_version_id"]
        assert '<h2 id="chapter">Chapter</h2>' in document.output_html
        assert (
            document.settings["semantic_content_hash"] == saved["semantic_content_hash"]
        )
        assert "Chapter" in document.output_markdown
        retry = api.apply(
            "doc", expected_revision=revision, idempotency_key="heading-1", edits=[edit]
        )
        assert retry["duplicate"] and retry["applied_revision"] == saved["revision"]
        assert document.settings["knowledge_source_version_id"] == version
        with pytest.raises(DocProcessorMcpServiceError, match="idempotency"):
            api.apply(
                "doc",
                expected_revision=revision,
                idempotency_key="heading-1",
                edits=[DocumentReviewEdit(element_id=edit.element_id, heading_level=4)],
            )
        with pytest.raises(DocProcessorMcpServiceError) as stale:
            api.read(
                "doc",
                "fetch",
                element_ids=[edit.element_id],
                expected_revision=revision,
            )
        assert stale.value.code == "revision_conflict"


def test_owner_and_completed_state_are_checked_before_content(review_app):
    with review_app.app_context():
        with pytest.raises(DocProcessorMcpServiceError) as foreign:
            service(review_app, 2).read("doc", "outline")
        assert foreign.value.code == "not_found"
        revision, edit = heading_edit(review_app)
        with pytest.raises(DocProcessorMcpServiceError) as foreign_write:
            service(review_app, 2).apply(
                "doc", expected_revision=revision, idempotency_key="x", edits=[edit]
            )
        assert foreign_write.value.code == "not_found"
        document = db.session.get(ProcessedDocument, "doc")
        document.status = "processing"
        db.session.commit()
        with pytest.raises(DocProcessorMcpServiceError) as active:
            service(review_app).apply(
                "doc", expected_revision=revision, idempotency_key="x", edits=[edit]
            )
        assert active.value.code == "document_not_completed"


@pytest.mark.parametrize(
    "replacement",
    [
        '<h2 id="chapter" onclick="alert(1)">Chapter</h2>',
        '<h2 id="chapter"><a href="#missing">Chapter</a></h2>',
    ],
)
def test_invalid_edits_leave_html_hash_and_knowledge_unchanged(review_app, replacement):
    with review_app.app_context():
        revision, edit = heading_edit(review_app)
        with pytest.raises((ValueError, DocProcessorMcpServiceError)):
            service(review_app).apply(
                "doc",
                expected_revision=revision,
                idempotency_key="bad",
                edits=[
                    DocumentReviewEdit(
                        element_id=edit.element_id, replacement_html=replacement
                    )
                ],
            )
        document = db.session.get(ProcessedDocument, "doc")
        assert service(review_app).read("doc", "outline")["revision"] == revision
        assert "knowledge_source_version_id" not in document.settings


def test_knowledge_publication_failure_rolls_back_content(review_app, monkeypatch):
    with review_app.app_context():
        revision, edit = heading_edit(review_app)

        def fail(_document):
            raise RuntimeError("synthetic publication failure")

        monkeypatch.setattr(
            "iarf_assistant.addons.doc_processor.mcp_review.sync_processed_document_source",
            fail,
        )
        with pytest.raises(RuntimeError):
            service(review_app).apply(
                "doc", expected_revision=revision, idempotency_key="fail", edits=[edit]
            )
        db.session.expire_all()
        assert service(review_app).read("doc", "outline")["revision"] == revision


def test_two_independent_writers_cannot_overwrite_same_revision(
    review_app, monkeypatch
):
    with review_app.app_context():
        revision, edit = heading_edit(review_app)
    barrier = threading.Barrier(2)
    original = DocumentReview.apply_edits

    def meet(self, edits):
        result = original(self, edits)
        barrier.wait(timeout=15)
        return result

    monkeypatch.setattr(DocumentReview, "apply_edits", meet)

    def write(level):
        with review_app.app_context():
            try:
                return service(review_app).apply(
                    "doc",
                    expected_revision=revision,
                    idempotency_key=f"writer-{level}",
                    edits=[
                        DocumentReviewEdit(
                            element_id=edit.element_id, heading_level=level
                        )
                    ],
                )
            except DocProcessorMcpServiceError as exc:
                return {"error": exc.code}

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(write, [2, 4]))
    assert sum(item.get("changed") is True for item in results) == 1
    assert [item["error"] for item in results if "error" in item] == [
        "revision_conflict"
    ]
    with review_app.app_context():
        document = db.session.get(ProcessedDocument, "doc")
        assert len(document.settings["mcp_review_receipts"]) == 1
        assert BeautifulSoup(document.output_html, "html.parser").find(
            id="chapter"
        ).name in {"h2", "h4"}


def test_legacy_rows_are_readable_but_scoped_edits_do_not_normalize_unrelated_content(
    review_app,
):
    with review_app.app_context():
        document = db.session.get(ProcessedDocument, "doc")
        document.settings = {}
        db.session.commit()
        revision, edit = heading_edit(review_app)
        with pytest.raises(DocProcessorMcpServiceError) as legacy:
            service(review_app).apply(
                "doc",
                expected_revision=revision,
                idempotency_key="legacy",
                edits=[edit],
            )
        assert legacy.value.code == "invalid_document"
        assert document.settings == {}
        assert service(review_app).read("doc", "outline")["revision"] == revision


def test_index_contract_change_invalidates_old_element_ids(review_app, monkeypatch):
    with review_app.app_context():
        revision, edit = heading_edit(review_app)
        monkeypatch.setattr(mcp_review, "REVIEW_SCHEMA", "carlina.document-review.v2")
        with pytest.raises(DocProcessorMcpServiceError) as stale:
            service(review_app).apply(
                "doc",
                expected_revision=revision,
                idempotency_key="old-index",
                edits=[edit],
            )
        assert stale.value.code == "revision_conflict"
        assert (
            "mcp_review_receipts"
            not in db.session.get(ProcessedDocument, "doc").settings
        )


def test_source_images_are_native_bounded_and_owner_scoped(review_app, tmp_path):
    import fitz

    source = tmp_path / "source.pdf"
    with fitz.open() as pdf:
        for index in range(2):
            page = pdf.new_page(width=600, height=800)
            page.insert_text((30, 30), f"Synthetic page {index + 1}")
        pdf.save(source)
    with review_app.app_context():
        api = service(review_app)
        payload = api.source_pages("doc", [2, 1], max_edge=800)
        transported = _mcp_image_result(payload)
        assert [part.type for part in transported.content] == ["text", "image", "image"]
        assert "base64" not in transported.content[0].text
        assert str(tmp_path) not in transported.content[0].text
        assert [
            item["page_number"] for item in transported.structured_content["pages"]
        ] == [2, 1]
        assert all(
            max(item["width"], item["height"]) <= 801 for item in payload["pages"]
        )
        with pytest.raises(DocProcessorMcpServiceError):
            api.source_pages("doc", [3])
        with pytest.raises(DocProcessorMcpServiceError):
            api.source_pages("doc", [1, 1])
        with pytest.raises(DocProcessorMcpServiceError) as foreign:
            service(review_app, 2).source_pages("doc", [1])
        assert foreign.value.code == "not_found"


def test_preview_encrypted_history_and_exact_idempotent_undo(review_app):
    with review_app.app_context():
        api = service(review_app)
        original = db.session.get(ProcessedDocument, "doc").output_html
        evidence = api.read("doc", "search", query="second")
        edit = DocumentReviewEdit(
            element_id=evidence["items"][0]["element_id"],
            replace_text={
                "find": "second",
                "replacement": "corrected",
                "expected_matches": 1,
            },
        )
        args = {
            "expected_revision": evidence["revision"],
            "idempotency_key": "literal",
            "edits": [edit],
        }
        preview = api.apply("doc", **args, dry_run=True)
        assert preview["preview"]["fragments"]
        assert preview["edit_results"][0]["matched_count"] == 1
        assert preview["undo_available_on_save"]
        assert api.history("doc")["items"] == []
        saved = api.apply("doc", **args)
        document = db.session.get(ProcessedDocument, "doc")
        version = document.settings["knowledge_source_version_id"]
        assert saved["existing_exports_changed"] is False
        assert saved["existing_translator_snapshots_changed"] is False
        raw = db.session.execute(
            text("SELECT settings, output_html FROM processed_document WHERE id='doc'")
        ).one()
        assert "second" not in raw[0] and "corrected" not in raw[0]
        assert raw[1].startswith("enc:")
        assert json.loads(raw[0])[mcp_review.HISTORY_KEY][0].startswith("enc:")
        assert mcp_review.HISTORY_KEY not in document.to_dict()["settings"]
        history = api.history("doc")
        assert history["items"][0]["change_id"] == saved["change_id"]
        assert history["items"][0]["can_undo"]
        undo_args = {
            "change_id": saved["change_id"],
            "expected_revision": saved["revision"],
            "idempotency_key": "undo-literal",
        }
        undo_preview = api.undo("doc", **undo_args, dry_run=True)
        assert undo_preview["proposed_revision"] == evidence["revision"]
        assert document.settings["knowledge_source_version_id"] == version
        assert not api.history("doc")["items"][0]["undone"]
        undone = api.undo("doc", **undo_args)
        assert undone["revision"] == evidence["revision"]
        assert document.output_html == original
        assert "second" in document.output_markdown
        assert api.history("doc")["items"][0]["undone"]
        restored_version = document.settings["knowledge_source_version_id"]
        assert api.undo("doc", **undo_args)["duplicate"]
        assert document.settings["knowledge_source_version_id"] == restored_version


@pytest.mark.parametrize("review_app", ["encrypted", "plaintext"], indirect=True)
def test_undo_is_owner_scoped_ordered_and_revision_fenced(review_app):
    with review_app.app_context():
        api = service(review_app)
        revision, edit = heading_edit(review_app)
        first = api.apply(
            "doc", expected_revision=revision, idempotency_key="first", edits=[edit]
        )
        revision2, edit2 = heading_edit(review_app, 4)
        second = api.apply(
            "doc", expected_revision=revision2, idempotency_key="second", edits=[edit2]
        )
        for expected in (first["revision"], second["revision"]):
            with pytest.raises(DocProcessorMcpServiceError) as error:
                api.undo(
                    "doc",
                    change_id=first["change_id"],
                    expected_revision=expected,
                    idempotency_key="out-of-order",
                )
            assert error.value.code == "revision_conflict"
        with pytest.raises(DocProcessorMcpServiceError) as error:
            service(review_app, 2).history("doc")
        assert error.value.code == "not_found"
        with pytest.raises(DocProcessorMcpServiceError) as error:
            service(review_app, 2).undo(
                "doc",
                change_id=second["change_id"],
                expected_revision=second["revision"],
                idempotency_key="foreign",
            )
        assert error.value.code == "not_found"
        api.undo(
            "doc",
            change_id=second["change_id"],
            expected_revision=second["revision"],
            idempotency_key="undo-second",
        )
        assert api.history("doc")["items"][1]["can_undo"]
        api.undo(
            "doc",
            change_id=first["change_id"],
            expected_revision=first["revision"],
            idempotency_key="undo-first",
        )
        assert api.read("doc", "outline")["revision"] == revision


@pytest.mark.parametrize("tamper", ["ciphertext", "owner", "revision", "plaintext"])
@pytest.mark.parametrize("review_app", ["encrypted", "plaintext"], indirect=True)
def test_history_tampering_never_changes_document(review_app, tamper):
    with review_app.app_context():
        api = service(review_app)
        revision, edit = heading_edit(review_app)
        saved = api.apply(
            "doc", expected_revision=revision, idempotency_key="original", edits=[edit]
        )
        document = db.session.get(ProcessedDocument, "doc")
        settings = dict(document.settings)
        entries = api._history(document)
        if tamper == "ciphertext":
            encoded = settings[mcp_review.HISTORY_KEY][0]
            settings[mcp_review.HISTORY_KEY] = [encoded[:-8] + "abcdefgh"]
        elif tamper == "plaintext":
            settings[mcp_review.HISTORY_KEY] = [json.dumps(entries[0])]
        else:
            entries[0]["owner_user_id" if tamper == "owner" else "before_revision"] = (
                2 if tamper == "owner" else "0" * 64
            )
            settings[mcp_review.HISTORY_KEY] = api._encode_history(entries)
        document.settings = settings
        db.session.commit()
        with pytest.raises(DocProcessorMcpServiceError) as error:
            api.undo(
                "doc",
                change_id=saved["change_id"],
                expected_revision=saved["revision"],
                idempotency_key="tampered",
            )
        assert error.value.code == "history_unavailable"
        assert api.read("doc", "outline")["revision"] == saved["revision"]


def test_undo_publication_failure_rolls_back_history_receipts_and_content(
    review_app, monkeypatch
):
    with review_app.app_context():
        api = service(review_app)
        revision, edit = heading_edit(review_app)
        saved = api.apply(
            "doc", expected_revision=revision, idempotency_key="edit", edits=[edit]
        )
        before_settings = dict(db.session.get(ProcessedDocument, "doc").settings)

        def fail(_document):
            raise RuntimeError("synthetic publication failure")

        monkeypatch.setattr(mcp_review, "sync_processed_document_source", fail)
        with pytest.raises(RuntimeError):
            api.undo(
                "doc",
                change_id=saved["change_id"],
                expected_revision=saved["revision"],
                idempotency_key="undo",
            )
        db.session.expire_all()
        document = db.session.get(ProcessedDocument, "doc")
        assert document.settings == before_settings
        assert api.read("doc", "outline")["revision"] == saved["revision"]


def test_undo_racing_new_edit_has_one_atomic_winner(review_app, monkeypatch):
    with review_app.app_context():
        revision, edit = heading_edit(review_app)
        saved = service(review_app).apply(
            "doc", expected_revision=revision, idempotency_key="first", edits=[edit]
        )
    barrier = threading.Barrier(2)
    original = DocumentReviewService._save

    def meet(self, *args, **kwargs):
        barrier.wait(timeout=15)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(DocumentReviewService, "_save", meet)

    def write(undo):
        with review_app.app_context():
            api = service(review_app)
            try:
                if undo:
                    return api.undo(
                        "doc",
                        change_id=saved["change_id"],
                        expected_revision=saved["revision"],
                        idempotency_key="undo-race",
                    )
                return api.apply(
                    "doc",
                    expected_revision=saved["revision"],
                    idempotency_key="edit-race",
                    edits=[
                        DocumentReviewEdit(element_id=edit.element_id, heading_level=4)
                    ],
                )
            except DocProcessorMcpServiceError as error:
                return {"error": error.code}

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(write, [True, False]))
    assert sum(result.get("changed") is True for result in results) == 1
    assert [result["error"] for result in results if "error" in result] == [
        "revision_conflict"
    ]
    with review_app.app_context():
        document = db.session.get(ProcessedDocument, "doc")
        assert len(document.settings[mcp_review.RECEIPT_KEY]) == 2
        history = service(review_app).history("doc")["items"]
        assert (len(history) == 1 and history[0]["undone"]) or (
            len(history) == 2 and history[0]["can_undo"]
        )


def test_history_retention_and_noop_do_not_publish_extra_versions(
    review_app, monkeypatch
):
    monkeypatch.setattr(mcp_review, "MAX_HISTORY_ENTRIES", 2)
    with review_app.app_context():
        api = service(review_app)
        for index, level in enumerate((2, 4, 5)):
            revision, edit = heading_edit(review_app, level)
            api.apply(
                "doc",
                expected_revision=revision,
                idempotency_key=f"edit-{index}",
                edits=[edit],
            )
        assert len(api.history("doc")["items"]) == 2
        document = db.session.get(ProcessedDocument, "doc")
        version = document.settings["knowledge_source_version_id"]
        revision, edit = heading_edit(review_app, 5)
        noop = api.apply(
            "doc", expected_revision=revision, idempotency_key="noop", edits=[edit]
        )
        assert not noop["changed"] and not noop["undo_available"]
        assert document.settings["knowledge_source_version_id"] == version
        assert len(api.history("doc")["items"]) == 2


def test_invalid_references_can_be_inspected_but_not_edited(review_app):
    with review_app.app_context():
        document = db.session.get(ProcessedDocument, "doc")
        semantic = canonicalize_document_html(
            '<main><h1>Title</h1><p>Text<a role="doc-noteref" href="#missing">1</a></p></main>',
            user_id=1,
        )
        assert semantic.errors
        document.output_html = semantic.html
        document.settings = semantic_settings({}, semantic)
        db.session.commit()
        notes = service(review_app).read("doc", "notes")
        assert notes["issues"]
        with pytest.raises(DocProcessorMcpServiceError) as error:
            service(review_app).apply(
                "doc",
                expected_revision=notes["revision"],
                idempotency_key="invalid",
                edits=[DocumentReviewEdit(element_id="n000001", heading_level=2)],
            )
        assert error.value.code == "invalid_document"


def test_encrypted_byte_budget_evicts_oldest_records(review_app):
    with review_app.app_context():
        entries = [
            {"change_id": str(index), "splices": [{"old": "x" * 200000}]}
            for index in range(8)
        ]
        encoded = DocumentReviewService._encode_history(entries)
        assert 0 < len(encoded) < 8
        assert sum(len(item) for item in encoded) <= mcp_review.MAX_HISTORY_BYTES
        crypto = review_app.extensions["carlina_encryption"]
        assert json.loads(crypto.decrypt(encoded[-1]))["change_id"] == "7"


def test_delete_last_text_fails_before_dry_run_or_save(review_app):
    with review_app.app_context():
        document = db.session.get(ProcessedDocument, "doc")
        semantic = canonicalize_document_html(
            "<main><p>Only text.</p></main>", user_id=1
        )
        document.output_html = semantic.html
        document.settings = semantic_settings({}, semantic)
        db.session.commit()
        api = service(review_app)
        evidence = api.read("doc", "search", query="Only text.")
        edit = DocumentReviewEdit(
            element_id=evidence["items"][0]["element_id"],
            delete_element=True,
            expected_text="Only text.",
        )
        for dry_run in (True, False):
            with pytest.raises(DocProcessorMcpServiceError) as error:
                api.apply(
                    "doc",
                    expected_revision=evidence["revision"],
                    idempotency_key="last-text",
                    edits=[edit],
                    dry_run=dry_run,
                )
            assert error.value.code == "invalid_edit"
        assert document.output_html == semantic.html
        assert mcp_review.RECEIPT_KEY not in document.settings


@pytest.mark.parametrize(
    "fields",
    [
        {"delete_element": False, "expected_text": "text"},
        {"delete_element": 1, "expected_text": "text"},
        {"delete_element": True},
        {"heading_level": 2, "expected_text": "text"},
        {"delete_element": True, "expected_text": "text", "heading_level": 2},
        {"replace_text": {"find": "x", "replacement": "y", "expected_matches": True}},
        {"replace_text": {"find": "", "replacement": "y", "expected_matches": 1}},
        {
            "replace_text": {"find": "x", "replacement": "y", "expected_matches": 1},
            "end_element_id": "n000002",
        },
    ],
)
def test_new_edit_schemas_are_closed_and_strict(fields):
    with pytest.raises(ValueError):
        DocumentReviewEdit(element_id="n000001", **fields)


def _furniture_fixture(app, pages=3):
    raw = '<main><h1 id="real-title">Actual title</h1>' + ''.join(
        f'<span role="doc-pagebreak" id="doc-page-{page}" title="{page}"></span>'
        f'<div class="page_header" id="running-{page}">Running title {page}</div>'
        f'<h2>Chapter {page}: Über freie Religion</h2><p>Unique substantive paragraph {page}.</p>'
        f'<section class="page_footer">Printed label {page}</section>'
        for page in range(1, pages + 1)
    ) + '</main>'
    semantic = canonicalize_document_html(raw, user_id=1)
    doc = db.session.get(ProcessedDocument, "doc")
    doc.output_html = semantic.html
    doc.settings = semantic_settings({}, semantic)
    db.session.commit()
    return str(doc.output_html)


def test_cleanup_preview_save_replay_and_exact_undo(review_app):
    with review_app.app_context():
        before = _furniture_fixture(review_app)
        api = service(review_app)
        preview = api.cleanup("doc", page_numbers=[2], roles=["header"])
        assert preview["cleanup"]["removed_count"] == 1
        assert preview["cleanup"]["examples"][0]["page_numbers"] == [2]
        assert db.session.get(ProcessedDocument, "doc").output_html == before
        args = dict(page_numbers=[2], roles=["header"], expected_revision=preview["revision"], idempotency_key="cleanup-test", dry_run=False)
        saved = api.cleanup("doc", **args)
        result = db.session.get(ProcessedDocument, "doc").output_html
        assert "Running title 2" not in result and "Running title 1" in result
        assert "Printed label 2" in result and "Chapter 2" in result and "doc-page-2" in result
        assert saved["existing_translator_snapshots_changed"] is False
        assert saved["undo_available"] is True
        assert api.cleanup("doc", **args)["duplicate"] is True
        restored = api.undo("doc", change_id=saved["change_id"], expected_revision=saved["revision"], idempotency_key="cleanup-undo")
        assert restored["revision"] == preview["revision"]
        assert db.session.get(ProcessedDocument, "doc").output_html == before


def test_cleanup_whole_649_page_book_is_atomic_and_reversible(review_app):
    with review_app.app_context():
        before = _furniture_fixture(review_app, pages=649)
        api = service(review_app)
        preview = api.cleanup("doc")
        assert preview["cleanup"]["removed_count"] == 1298
        assert len(preview["cleanup"]["examples"]) == 12
        saved = api.cleanup("doc", expected_revision=preview["revision"], idempotency_key="book-cleanup", dry_run=False)
        assert saved["undo_available"]
        api.undo("doc", change_id=saved["change_id"], expected_revision=saved["revision"], idempotency_key="book-undo")
        assert db.session.get(ProcessedDocument, "doc").output_html == before


def test_cleanup_owner_revision_and_noop(review_app):
    with review_app.app_context():
        _furniture_fixture(review_app)
        api = service(review_app)
        with pytest.raises(DocProcessorMcpServiceError):
            service(review_app, owner=2).cleanup("doc")
        with pytest.raises(DocProcessorMcpServiceError):
            api.cleanup("doc", dry_run=False)
        preview = api.cleanup("doc")
        saved = api.cleanup("doc", expected_revision=preview["revision"], idempotency_key="first", dry_run=False)
        with pytest.raises(DocProcessorMcpServiceError, match="changed"):
            api.cleanup("doc", expected_revision=preview["revision"], idempotency_key="stale", dry_run=False)
        empty = api.cleanup("doc")
        assert empty["changed"] is False and empty["cleanup"]["removed_count"] == 0
        assert empty["revision"] == saved["revision"]


@pytest.mark.parametrize("options", [
    {"roles": []}, {"roles": ["heading"]}, {"roles": ["header", "header"]},
    {"roles": [{}]}, {"page_numbers": []}, {"page_numbers": [True]},
    {"page_numbers": [1, 1]}, {"page_numbers": [-1]},
])
def test_cleanup_rejects_ambiguous_selection(review_app, options):
    with review_app.app_context():
        with pytest.raises(DocProcessorMcpServiceError):
            service(review_app).cleanup("doc", **options)


def test_heading_edit_with_generated_unicode_ids_saves_and_undoes_exactly(review_app):
    with review_app.app_context():
        before = _furniture_fixture(review_app)
        api = service(review_app)
        evidence = api.read("doc", "outline")
        heading = next(item for item in evidence["headings"] if item["level"] == 2)
        args = dict(
            expected_revision=evidence["revision"], idempotency_key="unicode-heading",
            edits=[DocumentReviewEdit(element_id=heading["element_id"], heading_level=3)],
        )
        preview = api.apply("doc", **args, dry_run=True)
        assert preview["changed"] and not preview["diagnostics"]
        assert db.session.get(ProcessedDocument, "doc").output_html == before
        saved = api.apply("doc", **args)
        current = db.session.get(ProcessedDocument, "doc").output_html
        assert [tag["id"] for tag in BeautifulSoup(current, "html.parser").find_all(id=True)] == [
            tag["id"] for tag in BeautifulSoup(before, "html.parser").find_all(id=True)
        ]
        assert api.apply("doc", **args)["duplicate"] is True
        api.undo(
            "doc", change_id=saved["change_id"], expected_revision=saved["revision"],
            idempotency_key="unicode-heading-undo",
        )
        assert db.session.get(ProcessedDocument, "doc").output_html == before


@pytest.mark.parametrize("markup, diagnostic", [
    ('<main><p onclick="private-script-token">Private text.</p></main>', "unsafe_attribute_removed"),
    ('<main><a href="#private-missing-target">Private text.</a></main>', "unresolved_fragment_reference"),
])
def test_review_rejection_names_safe_diagnostic_codes_not_document_content(review_app, markup, diagnostic):
    with review_app.app_context():
        document = db.session.get(ProcessedDocument, "doc")
        with pytest.raises(mcp_review.DocumentReviewError) as caught:
            DocumentReviewService._validate_candidate(document, markup)
        assert caught.value.code == "invalid_edit"
        assert diagnostic in caught.value.detail
        assert "Private text" not in caught.value.detail
        assert "private-script-token" not in caught.value.detail
        assert "private-missing-target" not in caught.value.detail


def _boundary_source():
    return (
        '<main><span class="pagebreak" id="doc-page-1" role="doc-pagebreak" title="1"></span>'
        '<p id="left">Menschenhände ge-</p>'
        '<span class="pagebreak" id="doc-page-2" role="doc-pagebreak" title="2"></span>'
        '<p id="right">macht haben.</p></main>'
    )


def test_boundary_repair_service_preview_save_retry_and_exact_undo(review_app):
    with review_app.app_context():
        semantic = canonicalize_document_html(_boundary_source(), user_id=1)
        document = db.session.get(ProcessedDocument, "doc")
        document.output_html = semantic.html
        document.settings = semantic_settings({}, semantic)
        db.session.commit()
        before = document.output_html
        api = service(review_app)
        scanned = api.repair_boundaries("doc")
        assert scanned["dry_run"] and not scanned["changed"]
        candidate = scanned["boundary_repair"]["candidates"][0]
        decisions = [{"id": candidate["id"], "action": "dehyphenate"}]
        options = dict(decisions=decisions, expected_revision=scanned["revision"], idempotency_key="repair-1")
        preview = api.repair_boundaries("doc", **options)
        assert preview["changed"] and document.output_html == before
        saved = api.repair_boundaries("doc", **options, dry_run=False)
        assert saved["changed"] and saved["undo_available"]
        assert "gemacht" in BeautifulSoup(document.output_html, "html.parser").get_text()
        assert BeautifulSoup(document.output_html, "html.parser").find(id="right") is not None
        retry = api.repair_boundaries("doc", **options, dry_run=False)
        assert retry["duplicate"] and retry["change_id"] == saved["change_id"]
        assert len(api.history("doc")["items"]) == 1
        api.undo("doc", change_id=saved["change_id"], expected_revision=saved["revision"], idempotency_key="undo-repair-1")
        assert document.output_html == before


def test_boundary_repair_requires_selection_revision_and_owner(review_app):
    with review_app.app_context():
        api = service(review_app)
        with pytest.raises(DocProcessorMcpServiceError):
            api.repair_boundaries("doc", dry_run=False)
        with pytest.raises(DocProcessorMcpServiceError):
            service(review_app, owner=2).repair_boundaries("doc")
        with pytest.raises(DocProcessorMcpServiceError) as caught:
            api.repair_boundaries("doc", expected_revision="0" * 64)
        assert caught.value.code == "revision_conflict"


def test_grouped_replacements_preserve_one_atomic_revision_and_undo(review_app):
    with review_app.app_context():
        api = service(review_app)
        evidence = api.read("doc", "search", query="second")
        element_id = evidence["items"][0]["element_id"]
        before = api.documents.get_document("doc", include_output=True)
        edit = DocumentReviewEdit(
            element_id=element_id,
            replace_texts=[
                {
                    "find": "second",
                    "replacement": "dritte",
                    "expected_matches": 1,
                    "whole_word": True,
                },
                {
                    "find": "paragraph",
                    "replacement": "Überlegung",
                    "expected_matches": 1,
                },
            ],
        )
        args = dict(
            expected_revision=evidence["revision"],
            idempotency_key="grouped-one",
            edits=[edit],
        )
        preview = api.apply("doc", dry_run=True, **args)
        assert preview["edit_results"][0]["matched_count"] == 2
        assert (
            api.documents.get_document("doc", include_output=True)["output_html"]
            == before["output_html"]
        )
        saved = api.apply("doc", **args)
        fresh = api.documents.get_document(
            "doc", include_output=True, expected_revision=saved["revision"]
        )
        assert fresh["revision"] == fresh["output_revision"] == saved["revision"]
        assert "dritte Überlegung" in fresh["output_html"]
        assert "dritte Überlegung" in fresh["output_markdown"]
        assert api.apply("doc", **args)["duplicate"] is True
        with pytest.raises(DocProcessorMcpServiceError) as stale:
            api.documents.get_document("doc", expected_revision=evidence["revision"])
        assert stale.value.code == "revision_conflict"
        undone = api.undo(
            "doc",
            change_id=saved["change_id"],
            expected_revision=saved["revision"],
            idempotency_key="undo-grouped",
        )
        assert undone["revision"] == evidence["revision"]
        assert (
            api.documents.get_document("doc", include_output=True)["output_html"]
            == before["output_html"]
        )


def test_failed_batch_preserves_all_content_and_returns_each_count(review_app):
    with review_app.app_context():
        api = service(review_app)
        evidence = api.read("doc", "search", query="second")
        element = evidence["items"][0]["element_id"]
        before = api.documents.get_document("doc", include_output=True)
        edits = [
            DocumentReviewEdit(
                element_id=element,
                replace_texts=[
                    {
                        "find": "second",
                        "replacement": "private new text",
                        "expected_matches": 2,
                    },
                    {
                        "find": "absent",
                        "replacement": "private new text",
                        "expected_matches": 1,
                    },
                ],
            )
        ]
        with pytest.raises(DocProcessorMcpServiceError) as caught:
            api.apply(
                "doc",
                expected_revision=evidence["revision"],
                idempotency_key="fail-counts",
                edits=edits,
            )
        assert [
            (d["element_id"], d["expected_matches"], d["actual_matches"])
            for d in caught.value.diagnostics
        ] == [(element, 2, 1), (element, 1, 0)]
        assert "private new text" not in str(caught.value)
        assert (
            api.documents.get_document("doc", include_output=True)["output_html"]
            == before["output_html"]
        )
        assert not api.history("doc")["items"]


def test_fresh_document_get_refreshes_identity_map_after_independent_commit(review_app):
    from sqlalchemy.orm import Session

    with review_app.app_context():
        api = service(review_app)
        cached = db.session.get(ProcessedDocument, "doc")
        prior = api.documents.get_document("doc", include_output=True)
        with Session(db.engine) as writer:
            other = writer.get(ProcessedDocument, "doc")
            semantic = canonicalize_document_html(
                "<h1>New source</h1><p>frisch überarbeitet</p>", user_id=1
            )
            other.output_html = semantic.html
            other.output_markdown = "# New source\n\nfrisch überarbeitet"
            other.settings = semantic_settings(other.settings, semantic)
            writer.commit()
        assert (
            cached.output_html == prior["output_html"]
        )  # Reproduced stale ORM identity.
        current = api.documents.get_document("doc", include_output=True)
        outline = api.read("doc", "outline")
        assert "frisch überarbeitet" in current["output_html"]
        assert current["revision"] != prior["revision"]
        assert current["output_revision"] == outline["revision"]
        assert outline["headings"][0]["text"] == "New source"


def test_new_default_word_option_preserves_old_retry_hash(review_app):
    import hashlib

    with review_app.app_context():
        api = service(review_app)
        evidence = api.read("doc", "search", query="second")
        operation = {
            "element_id": evidence["items"][0]["element_id"],
            "replace_text": {
                "find": "second",
                "replacement": "third",
                "expected_matches": 1,
            },
        }
        original_request = json.dumps(
            {"revision": evidence["revision"], "edits": [operation]},
            sort_keys=True,
            ensure_ascii=False,
        )
        api.apply(
            "doc",
            expected_revision=evidence["revision"],
            idempotency_key="old-default",
            edits=[DocumentReviewEdit(**operation)],
        )
        document = db.session.get(ProcessedDocument, "doc")
        receipt = document.settings[mcp_review.RECEIPT_KEY][-1]
        assert (
            receipt["request_hash"]
            == hashlib.sha256(original_request.encode("utf-8")).hexdigest()
        )


def test_source_page_context_fenced_bounded_and_native(review_app, tmp_path):
    import fitz

    with fitz.open() as pdf:
        pdf.new_page(width=600, height=800)
        pdf.save(tmp_path / "source.pdf")
    with review_app.app_context():
        document = db.session.get(ProcessedDocument, "doc")
        semantic = canonicalize_document_html(
            '<main><span class="pagebreak" id="doc-page-1" role="doc-pagebreak" title="1"></span><h1>Book</h1>'
            + "".join(f"<h3>Heading {i}</h3>" for i in range(45))
            + "</main>",
            user_id=1,
        )
        document.output_html = semantic.html
        document.settings = semantic_settings(document.settings, semantic)
        db.session.commit()
        api = service(review_app)
        revision = api.read("doc", "outline")["revision"]
        payload = api.source_pages(
            "doc",
            [1],
            max_edge=400,
            include_review_context=True,
            expected_revision=revision,
        )
        transported = _mcp_image_result(payload)
        assert [p.type for p in transported.content] == ["text", "image"]
        context = payload["pages"][0]["review_context"]
        assert context["total_headings"] == 46 and len(context["headings"]) == 40
        assert context["omitted_headings"] == 6 and context["outline_next_offset"] == 40
        assert payload["image_delivery"]["model_visibility"] == "not_verified"
        assert revision == payload["revision"]
        assert "base64" not in transported.content[0].text
        with pytest.raises(DocProcessorMcpServiceError) as stale:
            api.source_pages(
                "doc", [1], include_review_context=True, expected_revision="old"
            )
        assert stale.value.code == "revision_conflict"
        document.status = "processing"
        db.session.commit()
        assert len(api.source_pages("doc", [1], max_edge=400)["pages"]) == 1


def test_inventory_uses_current_revision_and_full_heading_counts(review_app):
    with review_app.app_context():
        api = service(review_app)
        inventory = api.read("doc", "inventory", limit=1)
        assert inventory["coverage"]["heading_levels"]["h3"] == 1
        revision, edit = heading_edit(review_app)
        saved = api.apply(
            "doc",
            expected_revision=revision,
            idempotency_key="inventory-change",
            edits=[edit],
        )
        with pytest.raises(DocProcessorMcpServiceError):
            api.read(
                "doc",
                "inventory",
                offset=inventory["next_offset"],
                expected_revision=inventory["revision"],
            )
        current = api.read("doc", "inventory", expected_revision=saved["revision"])
        assert current["coverage"]["heading_levels"]["h2"] == 1
