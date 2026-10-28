"""Focused contract tests for the bounded Translator document review service."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest
from content_storage_support import bind_content_policy
from flask import Flask
from flask_login import LoginManager

from carlina.core.semantic_html import SEMANTIC_HTML_CONTRACT_VERSION
from iarf_assistant.addons.translator.alignment_contract import AlignmentGroup
from iarf_assistant.addons.translator.alignment_persistence import (
    persist_alignment_revision,
    persist_target_span_set,
    rehydrate_target_span_set,
)
from iarf_assistant.addons.translator.block_contract import AttestedDocumentSnapshot
from iarf_assistant.addons.translator.block_persistence import (
    persist_source_plan,
    source_attestation_for_project,
)
from iarf_assistant.addons.translator.block_planner import plan_document_blocks
from iarf_assistant.addons.translator.block_workspace_api import (
    _rehydrate_workspace_objects,
    load_block_source_plan,
    persist_manual_candidate,
)
from iarf_assistant.addons.translator.document_review import (
    DocumentReviewError,
    _literal_matches,
    _source_page_map,
    explanation_evidence,
    review_fetch,
    review_outline,
    review_search,
    review_source_pages,
)
from iarf_assistant.addons.translator.models import TranslationProject
from iarf_assistant.addons.translator.scholarly_review import (
    ReviewActor,
    record_block_review,
)
from iarf_assistant.addons.translator.sentence_alignment import (
    normalize_sentence_alignment,
)
from iarf_assistant.addons.translator.target_contract import (
    TARGET_BLOCK_RESULT_CONTRACT_VERSION,
    TargetBlockCandidate,
    TargetTextPart,
    structural_fingerprint,
)
from iarf_assistant.addons.translator.target_persistence import (
    activate_target_revision,
    persist_target_revision,
)
from iarf_assistant.core.encryption import EncryptionService, InstallationKeyStore
from iarf_assistant.extensions import db
from iarf_assistant.models import User


@pytest.fixture
def app(tmp_path: Path):
    application = Flask(__name__, instance_path=str(tmp_path / "instance"))
    bind_content_policy(application)
    application.config.update(
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{tmp_path / 'review.db'}",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SECRET_KEY="document-review-test",
        TESTING=True,
    )
    db.init_app(application)
    login_manager = LoginManager()
    login_manager.init_app(application)

    @login_manager.user_loader
    def load_user(user_id: str):
        return db.session.get(User, int(user_id))

    key_store = InstallationKeyStore(tmp_path / "master-keys.json")
    key_store.initialize()
    application.extensions["carlina_encryption"] = EncryptionService(key_store)
    context = application.app_context()
    context.push()
    db.create_all()
    try:
        yield application
    finally:
        db.session.remove()
        with db.engine.begin() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            db.metadata.drop_all(connection)
        db.engine.dispose()
        context.pop()


def _project(name: str = "Review") -> TranslationProject:
    owner = User(email=f"{name.lower()}@example.test", password_hash="test")
    db.session.add(owner)
    db.session.flush()
    project = TranslationProject(
        user_id=owner.id,
        name=name,
        source_lang="en",
        target_lang="pl",
        status="setup",
        engine_contract="document-block-v1",
        document_id="doc-review",
        settings={
            "source_type": "document",
            "source_reference": {
                "version": 1,
                "capability": "document.processing",
                "provider_addon_id": "doc-provider",
                "provider_capability_id": "document-processing",
                "resource_type": "document",
                "resource_id": "doc-review",
            },
        },
    )
    db.session.add(project)
    db.session.commit()
    return project


def _plan(project: TranslationProject):
    source = (
        "<html><head><title>ספר</title></head><body>"
        "<h1>א. Introduction</h1>"
        "<p>First paragraph with ﬃ ligature.</p>"
        "<h2>Subheading</h2>"
        "<p>Second paragraph.</p>"
        "</body></html>"
    )
    digest = hashlib.sha256(source.encode()).hexdigest()
    snapshot = AttestedDocumentSnapshot(
        id="doc-review",
        document_id="doc-review",
        output_html=source,
        content_hash=digest,
        semantic_content_hash=digest,
        metadata={},
        provenance={},
        captured_at="2026-09-05T12:00:00+00:00",
        semantic_contract_version=SEMANTIC_HTML_CONTRACT_VERSION,
    )
    plan = plan_document_blocks(
        snapshot,
        project_id=project.id,
        source_attestation=source_attestation_for_project(project),
        source_language="en",
    )
    _record, _created = persist_source_plan(project, plan)
    db.session.commit()
    return plan


def _paged_plan(project: TranslationProject):
    source = (
        "<html><body>"
        '<span id="doc-page-1" class="pagebreak" role="doc-pagebreak" title="1"></span>'
        "<p>First physical page text.</p>"
        '<p>Crosses pages before <span id="doc-page-2" class="pagebreak" '
        'role="doc-pagebreak" title="2"></span> after boundary.</p>'
        "<p>Second physical page text.</p>"
        "</body></html>"
    )
    digest = hashlib.sha256(source.encode()).hexdigest()
    snapshot = AttestedDocumentSnapshot(
        id="doc-review",
        document_id="doc-review",
        output_html=source,
        content_hash=digest,
        semantic_content_hash=digest,
        metadata={},
        provenance={},
        captured_at="2026-09-19T00:00:00+00:00",
        semantic_contract_version=SEMANTIC_HTML_CONTRACT_VERSION,
    )
    plan = plan_document_blocks(
        snapshot,
        project_id=project.id,
        source_attestation=source_attestation_for_project(project),
        source_language="en",
    )
    _record, _created = persist_source_plan(project, plan)
    db.session.commit()
    return plan


def _large_plan(project: TranslationProject, count: int = 725):
    source = (
        "<html><body>"
        + "".join(
            f"<p>Large block {index:04d} marker.</p>" for index in range(1, count + 1)
        )
        + "</body></html>"
    )
    digest = hashlib.sha256(source.encode()).hexdigest()
    snapshot = AttestedDocumentSnapshot(
        id="doc-review",
        document_id="doc-review",
        output_html=source,
        content_hash=digest,
        semantic_content_hash=digest,
        metadata={},
        provenance={},
        captured_at="2026-09-05T12:00:00+00:00",
        semantic_contract_version=SEMANTIC_HTML_CONTRACT_VERSION,
    )
    plan = plan_document_blocks(
        snapshot,
        project_id=project.id,
        source_attestation=source_attestation_for_project(project),
        source_language="en",
    )
    assert len(plan.blocks) == count
    _record, _created = persist_source_plan(project, plan)
    db.session.commit()
    return plan


def _walk_keys(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        found.update(str(key).casefold() for key in value)
        for child in value.values():
            found.update(_walk_keys(child))
    elif isinstance(value, list):
        for child in value:
            found.update(_walk_keys(child))
    return found


def _candidate(workspace: Any, block: Any, *, suffix: str = "") -> dict[str, Any]:
    slots = {slot.id: slot for slot in block.slots}
    parts: list[dict[str, Any]] = []
    suffix_added = False
    for wire_part in block.wire:
        if wire_part.kind == "token":
            parts.append({"kind": "token", "token_id": wire_part.ref_id})
            continue
        slot = slots[wire_part.ref_id]
        if slot.disposition == "literal-preserved":
            parts.append({"kind": "literal", "literal_slot_id": slot.id})
        elif slot.disposition == "translatable":
            text = slot.source_value
            if suffix and not suffix_added:
                text += suffix
                suffix_added = True
            parts.append({"kind": "text", "text": text})
    return {
        "contract_version": TARGET_BLOCK_RESULT_CONTRACT_VERSION,
        "source_plan_id": workspace.record.id,
        "source_plan_hash": workspace.plan.plan_hash,
        "block_id": block.id,
        "structural_fingerprint": structural_fingerprint(workspace.plan, block),
        "parts": parts,
        "lexical_attributes": [],
    }


def _candidate_for_alignment_projection(project, workspace, block):
    candidate = TargetBlockCandidate(
        source_plan_id=workspace.record.id,
        source_plan_hash=workspace.plan.plan_hash,
        block_id=block.id,
        structural_fingerprint=structural_fingerprint(workspace.plan, block),
        parts=(TargetTextPart(kind="text", text="Combined translation."),),
        lexical_attributes=(),
    )
    manifest = normalize_sentence_alignment(
        block=block,
        candidate=candidate,
        value={
            "target_sentences": ["Combined translation."],
            "groups": [{"source": [1], "target": [1]}],
        },
        target_language=project.target_lang,
    )
    revision = persist_target_revision(
        workspace.record,
        candidate,
        target_language=project.target_lang,
        author_type="model",
        parent_revision_id=None,
    )
    span_set = persist_target_span_set(
        workspace.record,
        revision,
        verified_plan=workspace.plan,
        manifest=manifest,
    )
    spans = rehydrate_target_span_set(
        span_set, workspace.record, verified_plan=workspace.plan
    )
    groups = tuple(
        AlignmentGroup(
            tuple(block.anchors[index - 1].id for index in group.source_ordinals),
            tuple(spans[index - 1].id for index in group.target_ordinals),
            "provider-manifest-v1",
            0.99,
            "needs_review",
            {"source": "synthetic-test"},
        )
        for group in manifest.groups
    )
    alignment = persist_alignment_revision(
        workspace.record,
        span_set,
        groups=groups,
        method="provider-manifest-v1",
        review_state="needs_review",
        verified_plan=workspace.plan,
    )
    activate_target_revision(
        workspace.record,
        block_id=block.id,
        revision_id=revision.id,
        alignment_revision_id=alignment.id,
        expected_version=0,
        verified_plan=workspace.plan,
    )
    db.session.commit()


def test_partial_outline_and_fetch_are_safe_and_ordered(app):
    project = _project()
    plan = _plan(project)

    outline = review_outline(project)
    assert outline["contract"] == "translator-document-review-v1"
    assert [item["order"] for item in outline["items"]] == list(
        range(1, len(plan.blocks) + 1)
    )
    assert outline["items"][0]["heading_path"] == ["ספר"]
    assert "א." in outline["items"][1]["heading_path"][-1]
    assert all(item["has_target"] is False for item in outline["items"])

    selected = review_fetch(
        project,
        block_ids=[plan.blocks[2].id, plan.blocks[0].id],
    )
    assert [item["order"] for item in selected["items"]] == [1, 3]
    assert selected["items"][0]["target"] is None
    assert all("heading_path" in item for item in selected["items"])
    forbidden = {
        "hash",
        "locator",
        "receipt",
        "prompt",
        "settings",
        "source_reference",
        "protected_inline",
    }
    assert not any(
        any(part in key for part in forbidden) for key in _walk_keys(selected)
    )

    ranged = review_fetch(project, order_start=2, order_end=3)
    assert [item["order"] for item in ranged["items"]] == [2, 3]
    with pytest.raises(DocumentReviewError) as two_selectors:
        review_fetch(project, block_ids=[plan.blocks[0].id], order_start=1, order_end=1)
    assert two_selectors.value.code == "review-selector-required"


def test_review_fetch_cursor_tracks_target_and_review_head_versions(app):
    project = _project("Cursor")
    plan = _plan(project)
    paragraphs = [block for block in plan.blocks if block.role == "paragraph"]
    assert len(paragraphs) >= 2
    workspace = load_block_source_plan(project)
    target_block = paragraphs[1]
    first_candidate = _candidate(workspace, target_block)
    persist_manual_candidate(
        project,
        target_block.id,
        first_candidate,
        expected_active_version=0,
        author_user_id=project.user_id,
    )

    selected_ids = [paragraphs[0].id, target_block.id]
    first_page = review_fetch(project, block_ids=selected_ids, limit=1)
    cursor = first_page["pagination"]["next_cursor"]
    assert cursor

    workspace = load_block_source_plan(project)
    target_block = next(
        block for block in workspace.plan.blocks if block.id == target_block.id
    )
    persist_manual_candidate(
        project,
        target_block.id,
        _candidate(workspace, target_block, suffix=" revised"),
        expected_active_version=1,
        author_user_id=project.user_id,
    )
    with pytest.raises(DocumentReviewError) as target_changed:
        review_fetch(project, block_ids=selected_ids, limit=1, cursor=cursor)
    assert target_changed.value.code == "review-cursor-mismatch"

    current_page = review_fetch(project, block_ids=selected_ids, limit=1)
    review_cursor = current_page["pagination"]["next_cursor"]
    assert review_cursor
    record_block_review(
        project,
        target_block.id,
        "approved",
        "Current target reviewed.",
        expected_version=0,
        expected_target_version=2,
        actor=ReviewActor("owner", project.user_id),
        idempotency_key="cursor-review-head",
    )
    db.session.commit()
    with pytest.raises(DocumentReviewError) as review_changed:
        review_fetch(project, block_ids=selected_ids, limit=1, cursor=review_cursor)
    assert review_changed.value.code == "review-cursor-mismatch"


def test_review_fetch_preserves_current_and_historical_review_fields(app, monkeypatch):
    project = _project("ReviewFields")
    plan = _plan(project)
    workspace = load_block_source_plan(project)
    block_id = plan.blocks[0].id
    row = {
        "id": "review-revision-1",
        "revision_id": "review-revision-1",
        "project_id": project.id,
        "source_plan_id": workspace.record.id,
        "block_id": block_id,
        "decision": "approved",
        "effective_decision": "pending",
        "stale": True,
        "target_revision_id": "target-v1",
        "current_target_revision_id": "target-v2",
        "current_target_version": 2,
        "author_type": "owner",
        "author_user_id": project.user_id,
        "share_grant_id": None,
        "version": 1,
        "created_at": "2026-10-01T00:00:00",
    }

    def projection(name: str, *_args: Any, **_kwargs: Any):
        return [row] if name == "list_block_reviews" else []

    monkeypatch.setitem(review_fetch.__globals__, "_call_projection", projection)
    fetched = review_fetch(project, block_ids=[block_id])
    review = fetched["items"][0]["review"]
    assert review["decision"] == "approved"
    assert review["effective_decision"] == "pending"
    assert review["stale"] is True
    assert review["target_revision_id"] == "target-v1"
    assert review["current_target_revision_id"] == "target-v2"
    assert review["current_target_version"] == 2
    assert review["author_type"] == "owner"
    assert review["author_user_id"] == project.user_id
    assert review["id"] == review["revision_id"] == "review-revision-1"


def test_review_fetch_includes_only_verified_alignment_projection(app):
    project = _project("ReviewAlignment")
    plan = _plan(project)
    workspace = load_block_source_plan(project)
    block = next(item for item in plan.blocks if item.role == "paragraph")
    _candidate_for_alignment_projection(project, workspace, block)

    without_alignment = review_fetch(project, block_ids=[block.id])
    assert "alignment" not in without_alignment["items"][0]["target"]

    _targets, projected = _rehydrate_workspace_objects(
        workspace, include_alignment=True
    )
    with_alignment = review_fetch(project, block_ids=[block.id], include_alignment=True)
    assert (
        with_alignment["items"][0]["target"]["alignment"]
        == projected[block.id]["alignment"]
    )
    groups = with_alignment["items"][0]["target"]["alignment"]["groups"]
    assert len(groups) == 1
    assert groups[0]["kind"] == "one_to_one"


def test_large_plan_review_projections_are_scoped_and_chunked(
    app, monkeypatch: pytest.MonkeyPatch
):
    project = _project("LargeReview")
    plan = _large_plan(project)
    final_block_id = plan.blocks[-1].id
    calls: list[tuple[str, list[str]]] = []

    def projections(name: str, _project: Any, _plan: Any, *extra: Any):
        if name not in {"list_reader_notes", "list_block_reviews"}:
            return []
        block_ids = list(extra[0]) if extra else []
        calls.append((name, block_ids))
        if name == "list_reader_notes" and final_block_id in block_ids:
            return [
                {
                    "id": "large-note",
                    "version": 1,
                    "block_id": final_block_id,
                    "anchor_text": "marker",
                    "body": "large annotation",
                    "state": "active",
                }
            ]
        return []

    monkeypatch.setitem(review_outline.__globals__, "_call_projection", projections)

    outline = review_outline(project, limit=100)
    assert len(outline["items"]) == 100
    assert [item["order"] for item in outline["items"]] == list(range(1, 101))
    assert calls
    assert all(len(block_ids) <= 512 for _name, block_ids in calls)
    assert {block_id for _name, block_ids in calls for block_id in block_ids} == {
        block.id for block in plan.blocks[:100]
    }

    calls.clear()
    fetched = review_fetch(project, block_ids=[final_block_id])
    assert [item["order"] for item in fetched["items"]] == [725]
    assert calls == [
        ("list_reader_notes", [final_block_id]),
        ("list_block_reviews", [final_block_id]),
    ]

    calls.clear()

    def unexpected_glossary(*_args: Any, **_kwargs: Any):
        raise AssertionError("source-only search loaded glossary rows")

    with monkeypatch.context() as source_only:
        source_only.setitem(
            review_search.__globals__,
            "build_block_glossary_payload",
            unexpected_glossary,
        )
        source_search = review_search(
            project, query="marker", fields=("source",), limit=1
        )
    assert source_search["items"]
    assert calls == []

    annotation_search = review_search(
        project, query="large annotation", fields=("annotations",), limit=1
    )
    assert annotation_search["items"][0]["block_id"] == final_block_id
    note_calls = [block_ids for name, block_ids in calls if name == "list_reader_notes"]
    assert note_calls
    assert max(map(len, note_calls)) <= 512
    assert {block_id for block_ids in note_calls for block_id in block_ids} == {
        block.id for block in plan.blocks
    }


def test_nfkc_casefold_and_expanded_offsets_cover_original_text():
    assert _literal_matches("אב. Straße", "STRASSE") == [(4, 10, "Straße")]
    assert _literal_matches("prefix ﬃ suffix", "FFI") == [
        (7, 8, "ﬃ"),
        (11, 14, "ffi"),
    ]
    assert _literal_matches("A\N{COMBINING RING ABOVE}B", "å") == [
        (0, 2, "A\N{COMBINING RING ABOVE}")
    ]
    assert _literal_matches("가", "가") == [(0, 2, "가")]
    assert _literal_matches("ß", "s") == [(0, 1, "ß")]


def test_search_filters_role_field_and_rejects_changed_cursor(app):
    project = _project("Search")
    plan = _plan(project)
    first = review_search(
        project, query="paragraph", fields=("target", "source", "source"), limit=1
    )
    assert first["filters"]["fields"] == ["source", "target"]
    assert first["query"]["normalization_profile"] == "unicode-nfkc-casefold-v1"
    assert first["items"][0]["field"] == "source"
    assert first["items"][0]["role"] == "paragraph"
    assert first["items"][0]["span"][1] <= len(first["items"][0]["text"])
    assert (
        first["items"][0]["match"]
        == first["items"][0]["text"][slice(*first["items"][0]["span"])]
    )
    assert first["pagination"]["next_cursor"]

    with pytest.raises(DocumentReviewError) as changed:
        review_search(
            project,
            query="subheading",
            fields=("source",),
            cursor=first["pagination"]["next_cursor"],
        )
    assert changed.value.code == "review-cursor-mismatch"

    role_filtered = review_search(
        project, query="paragraph", fields=("source",), roles=("heading",)
    )
    assert role_filtered["items"] == []
    bounded_context = review_search(
        project, query="paragraph", fields=("source",), before=5, after=5
    )
    assert all(
        len(item["context"][direction]) <= 5
        for item in bounded_context["items"]
        for direction in ("before", "after")
    )
    assert plan.blocks


def test_regex_search_is_bounded_and_preserves_original_spans(app):
    project = _project("RegexSearch")
    _plan(project)

    result = review_search(
        project,
        query=r"PARA(?:GRAPH)",
        fields=("source",),
        mode="regex",
        regex_flags=("ignore_case",),
    )
    assert result["query"]["mode"] == "regex"
    assert result["query"]["flags"] == ["ignore_case"]
    assert result["query"]["timeout_ms_total"] == 2000
    assert result["items"]
    assert all(item["match"].casefold() == "paragraph" for item in result["items"])

    with pytest.raises(DocumentReviewError) as empty:
        review_search(
            project,
            query=r"(?=paragraph)",
            fields=("source",),
            mode="regex",
            regex_flags=("ignore_case",),
        )
    assert empty.value.code == "review-regex-empty-match"

    with pytest.raises(DocumentReviewError) as bad_flag:
        review_search(
            project,
            query="paragraph",
            fields=("source",),
            mode="regex",
            regex_flags=("best_effort",),
        )
    assert bad_flag.value.code == "review-regex-flags-invalid"


def test_regex_search_has_a_document_wide_timeout(app, monkeypatch: pytest.MonkeyPatch):
    project = _project("RegexBudget")
    _plan(project)
    monkeypatch.setattr(
        f"{review_search.__module__}.MAX_REGEX_TOTAL_SECONDS",
        0.0,
    )
    with pytest.raises(DocumentReviewError) as timed_out:
        review_search(
            project,
            query=r"paragraph",
            fields=("source",),
            mode="regex",
        )
    assert timed_out.value.code == "review-regex-timeout"


def test_review_exposes_physical_source_pages_and_resolves_page_images(app):
    project = _project("PagedReview")
    plan = _paged_plan(project)

    workspace = review_outline.__globals__["_load_workspace_plan"](project)
    page_map = _source_page_map(workspace)
    assert page_map
    assert any(pages == [1, 2] for pages in page_map.values())

    fetched = review_fetch(project, order_start=1, order_end=len(plan.blocks))
    assert all("source_pages" in item for item in fetched["items"])
    crossing = next(item for item in fetched["items"] if item["source_pages"] == [1, 2])
    resolved = review_source_pages(project, block_ids=[crossing["id"]])
    assert resolved["document_id"] == "doc-review"
    assert resolved["page_numbering"] == "physical_one_based"
    assert resolved["page_numbers"] == [1, 2]
    assert resolved["blocks"][0]["page_numbers"] == [1, 2]


def test_context_ceiling_and_proposed_note_privacy(
    app, monkeypatch: pytest.MonkeyPatch
):
    project = _project("Notes")
    plan = _plan(project)
    block_id = plan.blocks[1].id

    def projections(name: str, _project: Any, _plan: Any, *_extra: Any):
        if name == "list_reader_notes":
            return [
                {
                    "id": "active-note",
                    "version": 1,
                    "block_id": block_id,
                    "anchor_text": "א",
                    "body": "Visible",
                    "state": "active",
                },
                {
                    "id": "proposed-note",
                    "version": 1,
                    "block_id": block_id,
                    "anchor_text": "א",
                    "body": "Hidden",
                    "state": "proposed",
                },
            ]
        return []

    monkeypatch.setattr(
        "iarf_assistant.addons.translator.document_review._call_projection", projections
    )
    hidden = review_fetch(project, block_ids=[block_id])
    assert [note["id"] for note in hidden["items"][0]["reader_notes"]] == [
        "active-note"
    ]
    visible = review_fetch(project, block_ids=[block_id], include_proposals=True)
    assert {note["id"] for note in visible["items"][0]["reader_notes"]} == {
        "active-note",
        "proposed-note",
    }

    with pytest.raises(DocumentReviewError) as too_much:
        review_search(project, query="a", before=6)
    assert too_much.value.code == "review-before-invalid"


def test_explanation_marks_reconstruction_until_a_visible_note_records_context(
    app, monkeypatch: pytest.MonkeyPatch
):
    project = _project("Explain")
    plan = _plan(project)
    block_id = plan.blocks[1].id
    monkeypatch.setattr(
        "iarf_assistant.addons.translator.document_review._call_projection",
        lambda *_args: [],
    )
    reconstructed = explanation_evidence(project, block_ids=[block_id], question="Why?")
    assert reconstructed["historical_reason_status"] == "reconstruction_required"

    def active_note(name: str, _project: Any, _plan: Any, *_extra: Any):
        if name == "list_reader_notes":
            return [
                {
                    "id": "note-1",
                    "version": 1,
                    "block_id": block_id,
                    "anchor_text": "א",
                    "body": "The retained letter is a convention.",
                    "state": "active",
                }
            ]
        return []

    monkeypatch.setattr(
        "iarf_assistant.addons.translator.document_review._call_projection", active_note
    )
    recorded = explanation_evidence(
        project, block_ids=[block_id], question="convention"
    )
    assert recorded["historical_reason_status"] == "recorded"


def test_research_dossier_requires_exact_workspace_identity(
    app, monkeypatch: pytest.MonkeyPatch
):
    project = _project("DossierScope")
    plan = _plan(project)

    def scoped_dossier(name: str, _project: Any, workspace: Any, *extra: Any):
        current = {
            "id": "current-dossier",
            "project_id": project.id,
            "source_plan_id": workspace.record.id,
            "version": 1,
            "scope": {"kind": "document", "block_ids": []},
            "payload": {"document_brief": "Current-plan conclusions."},
        }
        if name == "list":
            return [
                {
                    "id": "identity-missing",
                    "version": 100,
                    "scope": {"kind": "document", "block_ids": []},
                },
                {
                    "id": "stale-dossier",
                    "project_id": project.id,
                    "source_plan_id": "stale-source-plan",
                    "version": 99,
                    "scope": {"kind": "document", "block_ids": []},
                },
                current,
            ]
        if name == "get":
            assert extra == ("current-dossier",)
            return current
        return []

    monkeypatch.setattr(
        "iarf_assistant.addons.translator.document_review._call_projection",
        scoped_dossier,
    )
    fetched = review_fetch(
        project, block_ids=[plan.blocks[0].id], include_research=True
    )
    assert fetched["research_dossier"]["id"] == "current-dossier"

    def mismatched_detail(name: str, _project: Any, workspace: Any, *extra: Any):
        current = {
            "id": "current-dossier",
            "project_id": project.id,
            "source_plan_id": workspace.record.id,
            "version": 1,
            "scope": {"kind": "document", "block_ids": []},
        }
        if name == "list":
            return [current]
        if name == "get":
            return {**current, "source_plan_id": "stale-source-plan"}
        return []

    monkeypatch.setattr(
        "iarf_assistant.addons.translator.document_review._call_projection",
        mismatched_detail,
    )
    with pytest.raises(DocumentReviewError) as stale:
        review_fetch(project, block_ids=[plan.blocks[0].id], include_research=True)
    assert stale.value.code == "review-dossier-invalid"


def test_search_fails_closed_when_a_literal_is_too_broad(
    app, monkeypatch: pytest.MonkeyPatch
):
    project = _project("BroadSearch")
    _plan(project)
    monkeypatch.setattr(
        "iarf_assistant.addons.translator.document_review.MAX_SEARCH_MATCHES", 3
    )
    monkeypatch.setattr(
        "iarf_assistant.addons.translator.document_review._direct_search_values",
        lambda *_args: iter((("synthetic", "aaaa", None, 0),)),
    )
    with pytest.raises(DocumentReviewError) as broad:
        review_search(project, query="a", fields=("source",), limit=1)
    assert broad.value.code == "review-search-too-broad"
