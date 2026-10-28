"""Acceptance tests for the additive document-block target substrate."""

from __future__ import annotations

import hashlib
import sys
from collections.abc import Iterable
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from content_storage_support import bind_content_policy
from flask import Flask
from sqlalchemy import event, text
from sqlalchemy.orm import sessionmaker

from carlina.core.semantic_html import SEMANTIC_HTML_CONTRACT_VERSION
from carlina.core.utc import utc_now_db
from iarf_assistant.addons.translator.alignment_contract import (
    AlignmentGroup,
    target_alignment_projection,
)
from iarf_assistant.addons.translator.alignment_persistence import (
    persist_alignment_revision,
    persist_target_span_set,
    rehydrate_alignment_revision,
    rehydrate_target_span_set,
)
from iarf_assistant.addons.translator.block_contract import (
    BLOCK_PLANNER_VERSION,
    LEGACY_BLOCK_PLANNER_VERSION,
    AttestedDocumentSnapshot,
    BlockContractError,
    canonical_json,
    sha256_text,
)
from iarf_assistant.addons.translator.block_notes import ReaderNoteCandidate
from iarf_assistant.addons.translator.block_persistence import (
    persist_source_plan,
    rehydrate_source_plan,
    source_attestation_for_project,
)
from iarf_assistant.addons.translator.block_planner import plan_document_blocks
from iarf_assistant.addons.translator.models import (
    TranslationAlignmentRevision,
    TranslationProject,
    TranslationReaderNote,
    TranslationReaderNoteRevision,
    TranslationSourcePlan,
    TranslationTargetAlignmentActive,
    TranslationTargetBlockActive,
    TranslationTargetBlockNote,
    TranslationTargetBlockRevision,
    TranslationTargetSpanSet,
)
from iarf_assistant.addons.translator.scholarly_review import (
    ReviewActor,
    create_reader_note,
    revise_reader_note,
)
from iarf_assistant.addons.translator.target_contract import (
    TargetBlockCandidate,
    TargetLexicalAttribute,
    TargetTextPart,
    structural_fingerprint,
)
from iarf_assistant.addons.translator.target_persistence import (
    _active_pointer_map,
    _pair_receipt,
    activate_target_revision,
    active_target_candidates,
    persist_target_revision,
    rehydrate_target_revision,
    render_persisted_target_document,
)
from iarf_assistant.addons.translator.target_rendering import (
    render_target_document,
    validate_target_semantic_document,
)
from iarf_assistant.addons.translator.target_validation import validate_target_candidate
from iarf_assistant.core.encryption import EncryptionService, InstallationKeyStore
from iarf_assistant.extensions import db
from iarf_assistant.models import User


@pytest.fixture
def app(tmp_path: Path):
    application = Flask(__name__, instance_path=str(tmp_path / "instance"))
    bind_content_policy(application)
    application.config.update(
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{tmp_path / 'targets.db'}",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SECRET_KEY="target-block-test",
        TESTING=True,
    )
    db.init_app(application)
    context = application.app_context()
    context.push()
    key_store = InstallationKeyStore(tmp_path / "master-keys.json")
    key_store.initialize()
    application.extensions["carlina_encryption"] = EncryptionService(key_store)
    db.create_all()
    try:
        yield application
    finally:
        db.session.remove()
        db.drop_all()
        db.engine.dispose()
        context.pop()


def _project(name: str = "Target blocks") -> TranslationProject:
    owner = User(email=f"{name.lower().replace(' ', '-')}@example.test", password_hash="x")
    db.session.add(owner)
    db.session.flush()
    project = TranslationProject(
        user_id=owner.id,
        name=name,
        source_lang="en",
        target_lang="pl",
        engine_contract="document-block-v1",
        settings={
            "source_reference": {
                "version": 1,
                "capability": "document.processing",
                "provider_addon_id": "doc-provider",
                "provider_capability_id": "document-processing",
                "resource_type": "document",
                "resource_id": "doc-1",
            }
        },
    )
    db.session.add(project)
    db.session.commit()
    return project


def _source_plan(
    project: TranslationProject,
    html: str,
    *,
    planner_version: str = BLOCK_PLANNER_VERSION,
):
    digest = hashlib.sha256(html.encode()).hexdigest()
    snapshot = AttestedDocumentSnapshot(
        id="doc-1",
        document_id="doc-1",
        output_html=html,
        content_hash=digest,
        semantic_content_hash=digest,
        metadata={},
        provenance={},
        captured_at="2026-08-25T12:00:00+00:00",
        semantic_contract_version=SEMANTIC_HTML_CONTRACT_VERSION,
    )
    plan = plan_document_blocks(
        snapshot,
        project_id=project.id,
        source_attestation=source_attestation_for_project(project),
        source_language="en",
        planner_version=planner_version,
    )
    record, _created = persist_source_plan(project, plan)
    db.session.flush()
    return plan, record


def _candidate(
    plan,
    record,
    block,
    *,
    text: str = "Przekład.",
    lexical_value: str = "Przystępny",
    token_ids: Iterable[str] | None = None,
) -> TargetBlockCandidate:
    """Create a deliberately ordinary provider candidate for one fixture block."""

    tokens = iter(token_ids if token_ids is not None else [item.id for item in block.protected_inline])
    parts: list[TargetTextPart] = []
    has_text = any(slot.kind != "attribute" and slot.disposition == "translatable" for slot in block.slots)
    if has_text:
        # One text run on each side of protected tokens proves that sentence
        # shape is not constrained by the source anchor count.
        parts.append(TargetTextPart(kind="text", text=text))
        parts.extend(TargetTextPart(kind="token", token_id=token_id) for token_id in tokens)
    lexical = tuple(
        TargetLexicalAttribute(slot_id=slot.id, value=lexical_value)
        for slot in block.slots
        if slot.kind == "attribute" and slot.disposition == "translatable"
    )
    return TargetBlockCandidate(
        source_plan_id=record.id,
        source_plan_hash=plan.plan_hash,
        block_id=block.id,
        structural_fingerprint=structural_fingerprint(plan, block),
        parts=tuple(parts),
        lexical_attributes=lexical,
    )


def _activate_with_alignment(record, revision, *, expected_version: int):
    """Phase-4 activation fixture: no unchecked active-pointer shortcut."""

    span_set = persist_target_span_set(record, revision)
    alignment = persist_alignment_revision(record, span_set)
    return activate_target_revision(
        record,
        block_id=revision.block_id,
        revision_id=revision.id,
        alignment_revision_id=alignment.id,
        expected_version=expected_version,
    )


def test_candidate_is_exact_key_bounded_and_bound_to_one_plan_block(app):
    project = _project()
    plan, record = _source_plan(project, "<html><body><p>Hello.</p></body></html>")
    block = plan.blocks[0]
    candidate = _candidate(plan, record, block)
    assert TargetBlockCandidate.from_dict(candidate.to_dict()).candidate_hash == candidate.candidate_hash
    invalid = {**candidate.to_dict(), "unexpected": True}
    with pytest.raises(BlockContractError, match="target-candidate-dto-keys"):
        TargetBlockCandidate.from_dict(invalid)
    with pytest.raises(BlockContractError, match="target-text-size"):
        TargetBlockCandidate(
            source_plan_id=candidate.source_plan_id,
            source_plan_hash=candidate.source_plan_hash,
            block_id=candidate.block_id,
            structural_fingerprint=candidate.structural_fingerprint,
            parts=tuple(TargetTextPart(kind="text", text="x" * (200 * 1024)) for _ in range(3)),
            lexical_attributes=candidate.lexical_attributes,
        )
    result = validate_target_candidate(plan, record.id, block, candidate)
    assert result.valid and result.evidence()["findings"] == []


def test_target_candidate_allows_sentence_split_merge_without_anchor_slots(app):
    project = _project()
    plan, record = _source_plan(project, "<html><body><p>One. Two.</p></body></html>")
    block = plan.blocks[0]
    assert len(block.anchors) >= 2
    candidate = _candidate(plan, record, block, text="Jedno i dwa — jedno zdanie.")
    assert validate_target_candidate(plan, record.id, block, candidate).valid


def test_reader_note_target_anchor_uses_visible_occurrence_and_escapes_body(app):
    project = _project("reader note target anchor")
    plan, record = _source_plan(
        project, "<html><body><p>alpha &amp; beta alpha</p></body></html>"
    )
    block = plan.blocks[0]
    candidate = _candidate(plan, record, block, text="alpha & beta alpha")
    note = ReaderNoteCandidate(
        id="reader-note-stable-id",
        block_id=block.id,
        anchor_kind="target",
        anchor_text="alpha",
        occurrence_index=1,
        body="A <useful> note.",
    )
    entity_note = ReaderNoteCandidate(
        id="reader-note-entity",
        block_id=block.id,
        anchor_kind="target",
        anchor_text="alpha & beta",
        occurrence_index=0,
        body="Entity context.",
    )
    rendered = render_target_document(
        plan,
        record.id,
        {block.id: candidate},
        target_language="pl",
        notes={block.id: (entity_note, note)},
    )
    reference = '<a id="translator-reader-note-reader-note-stable-id-ref"'
    assert rendered.count(reference) == 1
    assert rendered.index(reference) > rendered.index("beta")
    assert 'beta<a id="translator-reader-note-reader-note-entity-ref"' in rendered
    assert "A &lt;useful&gt; note." in rendered
    assert validate_target_semantic_document(rendered).valid


def test_reader_note_target_anchor_spans_inline_markup(app):
    project = _project("reader note inline anchor")
    plan, record = _source_plan(
        project, "<html><body><p>alpha <strong>beta</strong> gamma</p></body></html>"
    )
    block = plan.blocks[0]
    slots = {slot.id: slot for slot in block.slots}
    parts: list[TargetTextPart] = []
    for wire_part in block.wire:
        if wire_part.kind == "token":
            parts.append(TargetTextPart(kind="token", token_id=wire_part.ref_id))
            continue
        slot = slots[wire_part.ref_id]
        if slot.disposition == "literal-preserved":
            parts.append(TargetTextPart(kind="literal", literal_slot_id=slot.id))
        else:
            parts.append(TargetTextPart(kind="text", text=slot.source_value))
    candidate = TargetBlockCandidate(
        source_plan_id=record.id,
        source_plan_hash=plan.plan_hash,
        block_id=block.id,
        structural_fingerprint=structural_fingerprint(plan, block),
        parts=tuple(parts),
        lexical_attributes=(),
    )
    note = ReaderNoteCandidate(
        id="inline-note",
        block_id=block.id,
        anchor_kind="target",
        anchor_text="alpha beta",
        occurrence_index=0,
        body="Inline context.",
    )
    rendered = render_target_document(
        plan,
        record.id,
        {block.id: candidate},
        target_language="pl",
        notes={block.id: (note,)},
    )
    assert "</strong><a id=\"translator-reader-note-inline-note-ref\"" in rendered
    assert validate_target_semantic_document(rendered).valid


def test_reader_note_target_anchor_spans_protected_literal_entity(app):
    project = _project("reader note protected literal")
    plan, record = _source_plan(
        project,
        '<html><body><p><span lang="he">&#x05d0;</span> heading</p></body></html>',
    )
    block = plan.blocks[0]
    slots = {slot.id: slot for slot in block.slots}
    parts: list[TargetTextPart] = []
    for wire_part in block.wire:
        if wire_part.kind == "token":
            parts.append(TargetTextPart(kind="token", token_id=wire_part.ref_id))
            continue
        slot = slots[wire_part.ref_id]
        if slot.disposition == "literal-preserved":
            parts.append(TargetTextPart(kind="literal", literal_slot_id=slot.id))
        else:
            parts.append(TargetTextPart(kind="text", text=slot.source_value))
    candidate = TargetBlockCandidate(
        source_plan_id=record.id,
        source_plan_hash=plan.plan_hash,
        block_id=block.id,
        structural_fingerprint=structural_fingerprint(plan, block),
        parts=tuple(parts),
        lexical_attributes=(),
    )
    note = ReaderNoteCandidate(
        id="protected-literal-note",
        block_id=block.id,
        anchor_kind="target",
        anchor_text="א",
        occurrence_index=0,
        body="Protected Hebrew context.",
    )
    rendered = render_target_document(
        plan,
        record.id,
        {block.id: candidate},
        target_language="pl",
        notes={block.id: (note,)},
    )
    assert (
        '</span><a id="translator-reader-note-protected-literal-note-ref"'
        in rendered
    )
    assert validate_target_semantic_document(rendered).valid


def test_reader_note_source_and_block_anchors_render_at_block_end(app):
    project = _project("reader note end anchors")
    plan, record = _source_plan(
        project, "<html><body><p>alpha beta</p></body></html>"
    )
    block = plan.blocks[0]
    candidate = _candidate(plan, record, block, text="alfa beta")
    source_note = ReaderNoteCandidate(
        id="source-note",
        block_id=block.id,
        anchor_kind="source",
        anchor_text="alpha",
        occurrence_index=0,
        body="Source context.",
    )
    block_note = ReaderNoteCandidate(
        id="block-note",
        block_id=block.id,
        anchor_kind="block",
        anchor_text=None,
        occurrence_index=None,
        body="Whole block context.",
    )
    rendered = render_target_document(
        plan,
        record.id,
        {block.id: candidate},
        target_language="pl",
        notes={block.id: (source_note, block_note)},
    )
    close = rendered.index("</p>")
    assert rendered.index('translator-reader-note-source-note-ref') < close
    assert rendered.index('translator-reader-note-block-note-ref') < close
    assert rendered.index('translator-reader-note-source-note-ref') < rendered.index(
        'translator-reader-note-block-note-ref'
    )


def test_persisted_rendering_selects_active_heads_and_keeps_durable_note_id(app):
    project = _project("reader note active heads")
    plan, record = _source_plan(
        project, "<html><body><p>alpha beta alpha</p></body></html>"
    )
    block = plan.blocks[0]
    revision = persist_target_revision(
        record,
        _candidate(plan, record, block, text="alpha beta alpha"),
        target_language="pl",
        author_type="system",
    )
    _activate_with_alignment(record, revision, expected_version=0)
    actor = ReviewActor("owner", project.user_id)
    active = create_reader_note(
        project,
        block_id=block.id,
        body="Visible active note.",
        citations=["ref-1"],
        actor=actor,
        anchor={"kind": "target", "anchor_text": "alpha", "occurrence_index": 1},
    )
    create_reader_note(
        project,
        block_id=block.id,
        body="Hidden proposed note.",
        actor=actor,
        state="proposed",
        anchor={"kind": "target", "anchor_text": "beta", "occurrence_index": 0},
    )
    rendered = render_persisted_target_document(record)
    stable_id = 'translator-reader-note-' + active["id"]
    assert "Visible active note." in rendered
    assert "Hidden proposed note." not in rendered
    assert stable_id in rendered
    assert 'class="translator-reader-note" data-label="Translator note"' in rendered
    assert '<span class="translator-note-citations">References: <cite>ref-1</cite></span>' in rendered

    revised = revise_reader_note(
        project,
        active["id"],
        expected_head_version=1,
        body="Revised active note.",
        actor=actor,
        anchor={
            "kind": "target",
            "anchor_text": "alpha",
            "occurrence_index": 1,
            "target_revision_id": revision.id,
            "target_version": 1,
        },
    )
    rendered_again = render_persisted_target_document(record)
    assert revised["revision_id"] != active["revision_id"]
    assert stable_id in rendered_again
    assert "Revised active note." in rendered_again


def test_retired_reader_head_suppresses_legacy_fallback(app):
    project = _project("reader note retired fallback")
    plan, record = _source_plan(
        project, "<html><body><p>alpha beta</p></body></html>"
    )
    block = plan.blocks[0]
    revision = persist_target_revision(
        record,
        _candidate(plan, record, block, text="alpha beta"),
        target_language="pl",
        author_type="model",
        notes=[{"anchor": "alpha", "body": "Legacy model note."}],
    )
    _activate_with_alignment(record, revision, expected_version=0)
    materialized_revision = TranslationReaderNoteRevision.query.one()
    note = db.session.get(TranslationReaderNote, materialized_revision.note_id)
    assert note is not None
    revise_reader_note(
        project,
        note.id,
        expected_head_version=1,
        state="retired",
        body="Legacy model note.",
        citations=["human-reference"],
        actor=ReviewActor("owner", project.user_id),
        anchor={
            "kind": "target",
            "anchor_text": "alpha",
            "occurrence_index": 0,
            "target_revision_id": revision.id,
            "target_version": 1,
        },
    )
    rendered = render_persisted_target_document(record)
    current_revision = TranslationReaderNoteRevision.query.order_by(
        TranslationReaderNoteRevision.revision_number.desc()
    ).first()
    assert current_revision is not None
    assert f"legacy-target-note-v1:{TranslationTargetBlockNote.query.one().id}" in (
        current_revision.citations_json
    )
    assert "human-reference" in current_revision.citations_json
    assert "Legacy model note." not in rendered
    assert "translator-reader-note-" not in rendered


@pytest.mark.parametrize(
    "mutate, expected",
    [
        (lambda candidate: {**candidate.to_dict(), "block_id": "unknown"}, "target-block-id"),
        (lambda candidate: {**candidate.to_dict(), "source_plan_hash": "0" * 64}, "target-plan-hash"),
        (lambda candidate: {**candidate.to_dict(), "structural_fingerprint": "0" * 64}, "target-structural-fingerprint"),
    ],
)
def test_target_candidate_rejects_cross_plan_unknown_and_stale_identity(app, mutate, expected):
    project = _project()
    plan, record = _source_plan(project, "<html><body><p>Hello.</p></body></html>")
    block = plan.blocks[0]
    candidate = TargetBlockCandidate.from_dict(mutate(_candidate(plan, record, block)))
    assert expected in {finding.code for finding in validate_target_candidate(plan, record.id, block, candidate).findings}


def test_token_deletion_duplicate_unknown_nesting_and_fixed_order_fail_closed(app):
    project = _project()
    plan, record = _source_plan(
        project,
        '<html><body><p>Go <a href="#n"><em>there</em></a> now.</p></body></html>',
    )
    block = plan.blocks[0]
    token_ids = [token.id for token in block.protected_inline]
    assert len(token_ids) == 4
    base = _candidate(plan, record, block, token_ids=token_ids)
    cases = {
        "missing": tuple(base.parts[:-1]),
        "duplicate": (*base.parts, TargetTextPart(kind="token", token_id=token_ids[0])),
        "unknown": tuple(
            [TargetTextPart(kind="text", text="x"), TargetTextPart(kind="token", token_id="unknown")]
            + [TargetTextPart(kind="token", token_id=item) for item in token_ids[1:]]
        ),
        "nesting": tuple(
            [TargetTextPart(kind="text", text="x")]
            + [TargetTextPart(kind="token", token_id=item) for item in (token_ids[0], token_ids[2], token_ids[1], token_ids[3])]
        ),
        "fixed-order": tuple(
            [TargetTextPart(kind="text", text="x")]
            + [TargetTextPart(kind="token", token_id=item) for item in reversed(token_ids)]
        ),
        "movable-escapes-fixed-scope": tuple(
            [TargetTextPart(kind="text", text="x")]
            + [
                TargetTextPart(kind="token", token_id=item)
                for item in (token_ids[0], token_ids[3], token_ids[1], token_ids[2])
            ]
        ),
    }
    for parts in cases.values():
        candidate = TargetBlockCandidate(
            source_plan_id=base.source_plan_id,
            source_plan_hash=base.source_plan_hash,
            block_id=base.block_id,
            structural_fingerprint=base.structural_fingerprint,
            parts=parts,
            lexical_attributes=base.lexical_attributes,
        )
        assert not validate_target_candidate(plan, record.id, block, candidate).valid


def test_candidate_text_and_attributes_escape_markup_and_reject_controls(app):
    project = _project()
    plan, record = _source_plan(project, '<html><body><img alt="Image" src="cover.jpg"></body></html>')
    block = plan.blocks[0]
    candidate = _candidate(plan, record, block)
    with pytest.raises(BlockContractError, match="target-part-text"):
        TargetTextPart(kind="text", text="bad\x00control")
    with pytest.raises(BlockContractError, match="target-part-text"):
        TargetTextPart(kind="text", text="bad\x80control")
    assert TargetLexicalAttribute(slot_id=block.slots[0].id, value='Math < x & "y"')
    malformed = TargetBlockCandidate(
        source_plan_id=candidate.source_plan_id,
        source_plan_hash=candidate.source_plan_hash,
        block_id=candidate.block_id,
        structural_fingerprint=candidate.structural_fingerprint,
        parts=candidate.parts,
        lexical_attributes=(
            TargetLexicalAttribute(slot_id=block.slots[0].id, value="Good"),
            TargetLexicalAttribute(slot_id=block.slots[0].id, value="Again"),
        ),
    )
    findings = {item.code for item in validate_target_candidate(plan, record.id, block, malformed).findings}
    assert {"target-lexical-duplicate", "target-lexical-order"} <= findings

    prose_plan, prose_record = _source_plan(
        _project("technical prose"), "<html><body><p>Source.</p></body></html>"
    )
    prose_block = prose_plan.blocks[0]
    provider_text = "<script>alert(1)</script> and x < y > z"
    prose_candidate = _candidate(prose_plan, prose_record, prose_block, text=provider_text)
    rendered = render_target_document(
        prose_plan,
        prose_record.id,
        {prose_block.id: prose_candidate},
        target_language="pl",
    )
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in rendered
    assert "<script" not in rendered
    assert validate_target_semantic_document(rendered).valid


def test_literal_slots_are_protected_references_not_provider_text(app):
    project = _project()
    plan, record = _source_plan(
        project, "<html><body><p>Use <code>&lt;value&gt;</code> now.</p></body></html>"
    )
    block = plan.blocks[0]
    literals = [
        slot
        for slot in sorted(block.slots, key=lambda slot: (slot.raw_start, slot.raw_end))
        if slot.disposition == "literal-preserved"
    ]
    token_ids = [token.id for token in block.protected_inline]
    candidate = TargetBlockCandidate(
        source_plan_id=record.id,
        source_plan_hash=plan.plan_hash,
        block_id=block.id,
        structural_fingerprint=structural_fingerprint(plan, block),
        parts=(
            TargetTextPart(kind="text", text="Użyj "),
            TargetTextPart(kind="token", token_id=token_ids[0]),
            *(TargetTextPart(kind="literal", literal_slot_id=slot.id) for slot in literals),
            TargetTextPart(kind="token", token_id=token_ids[1]),
            TargetTextPart(kind="text", text=" teraz."),
        ),
        lexical_attributes=(),
    )
    assert validate_target_candidate(plan, record.id, block, candidate).valid
    rendered = render_target_document(plan, record.id, {block.id: candidate}, target_language="pl")
    assert "<code>&lt;value&gt;</code>" in rendered
    missing = TargetBlockCandidate(
        source_plan_id=candidate.source_plan_id,
        source_plan_hash=candidate.source_plan_hash,
        block_id=candidate.block_id,
        structural_fingerprint=candidate.structural_fingerprint,
        parts=tuple(part for part in candidate.parts if part.kind != "literal"),
        lexical_attributes=(),
    )
    assert "target-literal-preservation" in {
        finding.code for finding in validate_target_candidate(plan, record.id, block, missing).findings
    }


def test_literals_cannot_escape_or_reorder_relative_to_fixed_references(app):
    project = _project("fixed literal order")
    plan, record = _source_plan(
        project,
        '<html><body><p><a href="#target"><code>literal</code> prose</a></p></body></html>',
    )
    block = plan.blocks[0]
    literal_parts = [
        TargetTextPart(kind="literal", literal_slot_id=slot.id)
        for slot in sorted(block.slots, key=lambda slot: (slot.raw_start, slot.raw_end))
        if slot.disposition == "literal-preserved"
    ]
    tokens = [token.id for token in block.protected_inline]
    # The candidate keeps syntactically valid token nesting, but moves the
    # literal outside the fixed <a> scope.
    escaped = TargetBlockCandidate(
        source_plan_id=record.id,
        source_plan_hash=plan.plan_hash,
        block_id=block.id,
        structural_fingerprint=structural_fingerprint(plan, block),
        parts=(
            TargetTextPart(kind="token", token_id=tokens[0]),
            TargetTextPart(kind="token", token_id=tokens[1]),
            TargetTextPart(kind="text", text="Przekład."),
            TargetTextPart(kind="token", token_id=tokens[2]),
            TargetTextPart(kind="token", token_id=tokens[3]),
            *literal_parts,
        ),
        lexical_attributes=(),
    )
    assert "target-fixed-reference-order" in {
        finding.code
        for finding in validate_target_candidate(plan, record.id, block, escaped).findings
    }


def test_rendering_nested_list_direct_prose_does_not_overlap_child_blocks(app):
    project = _project("nested direct prose export")
    source = (
        "<html><body><ol><li><strong>Lead<ol><li>Nested A.</li><li>Nested B.</li></ol></strong></li></ol></body></html>"
    )
    plan, record = _source_plan(project, source)
    candidates = {
        block.id: _candidate(plan, record, block, text=f"Target {block.order}.")
        for block in plan.blocks
    }

    rendered = render_target_document(
        plan, record.id, candidates, target_language="pl"
    )

    assert rendered.count("Target 1.") == 1
    assert rendered.count("Target 2.") == 1
    assert rendered.count("Target 3.") == 1
    assert rendered.count("<ol") == 2
    assert rendered.count("<li") == 3
    assert validate_target_semantic_document(rendered).valid


def test_rendering_legacy_direct_prose_coarse_span_preserves_nested_children(app):
    project = _project("legacy nested direct prose export")
    source = (
        "<html><body><ol><li>Lead<ol><li>Nested A.</li><li>Nested B.</li></ol></li></ol></body></html>"
    )
    plan, record = _source_plan(project, source)
    parent, first_child, second_child = plan.blocks
    # Emulate a persisted legacy plan whose coarse direct-prose envelope and
    # structural-only slots cover independently translated nested blocks.  The
    # translatable lexical slot itself remains the original disjoint witness.
    object.__setattr__(parent, "source_end", second_child.source_end)
    nested_source = source[first_child.source_start : second_child.source_end]
    structural_slot = replace(
        parent.slots[0],
        id=f"{parent.slots[0].id}-legacy-structural",
        locator=f"{parent.locator}/legacy-structural",
        disposition="structural-only",
        policy_code="whitespace",
        raw_start=first_child.source_start,
        raw_end=second_child.source_end,
        projection_start=None,
        projection_end=None,
        source_value=nested_source,
        source_hash=sha256_text(nested_source),
    )
    object.__setattr__(parent, "slots", (*parent.slots, structural_slot))
    candidates = {
        block.id: _candidate(plan, record, block, text=f"Target {block.order}.")
        for block in plan.blocks
    }

    rendered = render_target_document(
        plan, record.id, candidates, target_language="pl"
    )

    assert rendered.count("Target 1.") == 1
    assert rendered.count("Target 2.") == 1
    assert rendered.count("Target 3.") == 1
    assert rendered.count("<ol") == 2
    assert rendered.count("<li") == 3
    assert validate_target_semantic_document(rendered).valid


def test_rendering_reconstructs_document_and_derives_target_language(app):
    project = _project()
    html = """<html lang=\"en\"><body>\n<h1 id=\"chapter\">Title</h1><p>Text <em>inside</em>.</p>
<aside id=\"note-1\" role=\"doc-footnote\"><p>Note.</p></aside><p><a role=\"doc-noteref\" href=\"#note-1\">1</a></p>
<table><tr><th>Head</th></tr><tr><td>Cell</td></tr></table><figure><img alt=\"Cover\" src=\"cover.jpg\"><figcaption>Caption.</figcaption></figure>
<nav epub:type=\"toc\"><ol><li>Contents</li></ol></nav></body></html>"""
    plan, record = _source_plan(project, html)
    candidates = {block.id: _candidate(plan, record, block) for block in plan.blocks}
    rendered = render_target_document(plan, record.id, candidates, target_language="pl")
    assert 'lang="pl"' in rendered and 'xml:lang="pl"' in rendered
    assert rendered.count("<html") == rendered.count("<body") == 1
    assert 'src="cover.jpg"' in rendered
    assert validate_target_semantic_document(rendered).valid


def test_persisted_rendering_envelopes_attested_semantic_fragment_once(app):
    project = _project("semantic fragment")
    source = "<main><h1>Title</h1><p>Source.</p></main>"
    plan, record = _source_plan(project, source)

    for block in plan.blocks:
        revision = persist_target_revision(
            record,
            _candidate(plan, record, block),
            target_language="pl",
            author_type="system",
        )
        _activate_with_alignment(record, revision, expected_version=0)

    rendered = render_persisted_target_document(record)
    assert rendered.startswith('<html lang="pl" xml:lang="pl"><body><main>')
    assert rendered.endswith("</main></body></html>")
    assert rendered.count("<html") == rendered.count("<body") == 1
    assert "Przekład" in rendered
    assert plan.snapshot.output_html == source
    assert validate_target_semantic_document(rendered).valid


def test_persisted_rendering_reuses_one_verified_plan_and_batched_dependencies(
    app, monkeypatch
):
    project = _project("batched persisted rendering")
    source = "<main>" + "".join(
        f"<p>Source paragraph {index}.</p>" for index in range(1, 13)
    ) + "</main>"
    plan, record = _source_plan(project, source)
    for index, block in enumerate(plan.blocks, start=1):
        revision = persist_target_revision(
            record,
            _candidate(plan, record, block, text=f"Przekład {index}."),
            target_language="pl",
            author_type="system",
        )
        _activate_with_alignment(record, revision, expected_version=0)

    record_id = record.id
    db.session.flush()
    db.session.expire_all()
    record = db.session.get(TranslationSourcePlan, record_id)
    assert record is not None

    target_persistence = sys.modules[rehydrate_target_revision.__module__]
    alignment_persistence = sys.modules[rehydrate_alignment_revision.__module__]
    target_rehydrations = 0
    alignment_rehydrations = 0
    original_target_rehydrate = target_persistence.rehydrate_source_plan
    original_alignment_rehydrate = alignment_persistence.rehydrate_source_plan

    def count_target_rehydrate(*args, **kwargs):
        nonlocal target_rehydrations
        target_rehydrations += 1
        return original_target_rehydrate(*args, **kwargs)

    def count_alignment_rehydrate(*args, **kwargs):
        nonlocal alignment_rehydrations
        alignment_rehydrations += 1
        return original_alignment_rehydrate(*args, **kwargs)

    monkeypatch.setattr(
        target_persistence, "rehydrate_source_plan", count_target_rehydrate
    )
    monkeypatch.setattr(
        alignment_persistence,
        "rehydrate_source_plan",
        count_alignment_rehydrate,
    )
    statements: list[str] = []

    def count_statement(_connection, _cursor, statement, *_args):
        statements.append(str(statement))

    event.listen(db.engine, "after_cursor_execute", count_statement)
    try:
        rendered = render_persisted_target_document(record)
    finally:
        event.remove(db.engine, "after_cursor_execute", count_statement)

    assert "Przekład 12." in rendered
    assert target_rehydrations == 1
    assert alignment_rehydrations == 0
    # The numbering overlay adds one project-scoped read after the verified
    # target projection; preserve the existing 14-query ceiling otherwise.
    assert sum("FROM translation_numbering_overlay" in sql for sql in statements) == 1
    assert len(statements) <= 15, "\n".join(
        f"{index}: {' '.join(statement.split())[:240]}"
        for index, statement in enumerate(statements, start=1)
    )


def test_persisted_rendering_completes_head_only_semantic_fragment(app):
    project = _project("head fragment")
    source = "<head><title>Source title.</title></head>"
    plan, record = _source_plan(project, source)

    for block in plan.blocks:
        revision = persist_target_revision(
            record,
            _candidate(plan, record, block),
            target_language="pl",
            author_type="system",
        )
        _activate_with_alignment(record, revision, expected_version=0)

    rendered = render_persisted_target_document(record)
    assert rendered.startswith('<html lang="pl" xml:lang="pl"><head>')
    assert rendered.endswith("</head><body></body></html>")
    assert validate_target_semantic_document(rendered).valid


@pytest.mark.parametrize(
    "source",
    (
        "<main><body><p>Source.</p></body></main>",
        "<main><head><title>Source.</title></head></main>",
        "<!doctype html><main><p>Source.</p></main>",
        "<body><p>Source.</p></body><head><title>Late.</title></head>",
    ),
)
def test_rendering_rejects_nested_or_ambiguous_document_shell_fragments(app, source):
    project = _project(f"invalid shell {hashlib.sha256(source.encode()).hexdigest()[:8]}")
    plan, record = _source_plan(project, source)
    candidates = {block.id: _candidate(plan, record, block) for block in plan.blocks}

    with pytest.raises(BlockContractError, match="target-document-root"):
        render_target_document(
            plan,
            record.id,
            candidates,
            target_language="pl",
        )


def test_rendering_composes_nested_and_standalone_accessibility_attributes(app):
    project = _project("composed attributes")
    html = (
        '<html lang="en"><body><p id="target" title="Greeting">Hello '
        '<a href="#target" title="Link">world</a>.</p>'
        '<figure title="Frame" lang="de"><img alt="Cover" title="Book" '
        'src="cover.jpg" lang="fr" xml:lang="fr"></figure></body></html>'
    )
    plan, record = _source_plan(project, html)
    candidates = {block.id: _candidate(plan, record, block) for block in plan.blocks}
    attributes = sorted(
        [
            block
            for block in plan.blocks
            if block.role == "accessibility_text"
        ],
        key=lambda block: block.source_start,
    )
    assert len(attributes) == 5
    for block, value in zip(
        attributes,
        (
            'Paragraph <title>',
            'Link & "title"',
            'Frame & "title"',
            'Cover <alt>',
            'Book & "title"',
        ),
        strict=True,
    ):
        candidates[block.id] = _candidate(
            plan, record, block, lexical_value=value
        )
    rendered = render_target_document(plan, record.id, candidates, target_language="pl")
    assert 'title="Paragraph &lt;title&gt;"' in rendered
    assert 'title="Link &amp; &quot;title&quot;"' in rendered
    assert 'title="Frame &amp; &quot;title&quot;"' in rendered
    assert 'alt="Cover &lt;alt&gt;"' in rendered
    assert 'title="Book &amp; &quot;title&quot;"' in rendered
    assert 'lang="pl"' in rendered and 'xml:lang="pl"' in rendered
    assert (
        '<p id="target" title="Paragraph &lt;title&gt;" lang="pl" '
        'xml:lang="pl">' in rendered
    )
    assert (
        '<a href="#target" title="Link &amp; &quot;title&quot;" lang="pl" '
        'xml:lang="pl">' in rendered
    )
    assert (
        '<figure title="Frame &amp; &quot;title&quot;" lang="pl" xml:lang="pl">'
        in rendered
    )
    assert (
        '<img alt="Cover &lt;alt&gt;" title="Book &amp; &quot;title&quot;" '
        'src="cover.jpg" lang="pl" xml:lang="pl">' in rendered
    )
    assert 'href="#target"' in rendered and 'src="cover.jpg"' in rendered
    assert rendered.index('title="Paragraph') < rendered.index('href="#target"')
    assert rendered.index('title="Frame') < rendered.index('alt="Cover')
    for source_attribute in (
        'title="Greeting"',
        'title="Link"',
        'title="Frame"',
        'alt="Cover"',
        'title="Book"',
        'lang="de"',
        'lang="fr"',
    ):
        assert source_attribute not in rendered
    assert validate_target_semantic_document(rendered).valid


def test_footnote_label_is_immutable_metadata_not_a_translation_block(app):
    project = _project("structural footnote label")
    html = (
        '<html><body><ol><li role="doc-endnote" data-footnote-label="4">'
        "<p><strong>4.</strong> Source note.</p></li></ol></body></html>"
    )
    plan, record = _source_plan(project, html)
    label_entries = [
        entry
        for entry in plan.coverage.entries
        if entry.locator.endswith("/@data-footnote-label")
    ]
    assert len(label_entries) == 1
    assert label_entries[0].disposition == "structural-only"
    assert label_entries[0].policy_code == "immutable-reference"
    assert label_entries[0].block_id is None
    assert not any(
        block.role == "accessibility_text"
        and block.locator.endswith("/@data-footnote-label")
        for block in plan.blocks
    )

    candidates = {
        block.id: _candidate(plan, record, block, text="Przypis docelowy.")
        for block in plan.blocks
    }
    rendered = render_target_document(
        plan, record.id, candidates, target_language="pl"
    )
    assert 'data-footnote-label="4"' in rendered
    assert "Przypis docelowy." in rendered
    assert validate_target_semantic_document(rendered).valid


def test_legacy_footnote_label_plan_rehydrates_with_frozen_v1_semantics(app):
    project = _project("legacy structural footnote label")
    html = (
        '<html><body><ol><li role="doc-endnote" data-footnote-label="4">'
        "<p><strong>4.</strong> Source note.</p></li></ol></body></html>"
    )
    plan, record = _source_plan(
        project,
        html,
        planner_version=LEGACY_BLOCK_PLANNER_VERSION,
    )
    legacy_labels = [
        block
        for block in plan.blocks
        if block.role == "accessibility_text"
        and block.locator.endswith("/@data-footnote-label")
    ]
    assert len(legacy_labels) == 1
    assert plan.planner_version == LEGACY_BLOCK_PLANNER_VERSION

    db.session.expire_all()
    rehydrated = rehydrate_source_plan(db.session.get(TranslationSourcePlan, record.id))
    assert rehydrated.plan_hash == plan.plan_hash
    assert rehydrated._hash_payload() == plan._hash_payload()


def test_standalone_self_closing_image_attributes_keep_valid_language_scope(app):
    project = _project("self closing images")
    html = (
        '<html><body><img alt="Cover" title="Book" src="cover.jpg"/>'
        '<img alt="Map" title="Atlas" src="map.jpg" /></body></html>'
    )
    plan, record = _source_plan(project, html)
    candidates = {block.id: _candidate(plan, record, block) for block in plan.blocks}
    attributes = sorted(
        (block for block in plan.blocks if block.role == "accessibility_text"),
        key=lambda block: block.source_start,
    )
    assert len(attributes) == 4
    for block, value in zip(
        attributes,
        ("Okładka", "Książka", "Mapa docelowa", "Atlas docelowy"),
        strict=True,
    ):
        candidates[block.id] = _candidate(plan, record, block, lexical_value=value)
    rendered = render_target_document(plan, record.id, candidates, target_language="pl")
    assert (
        '<img alt="Okładka" title="Książka" src="cover.jpg" lang="pl" '
        'xml:lang="pl" />' in rendered
    )
    assert (
        '<img alt="Mapa docelowa" title="Atlas docelowy" src="map.jpg" lang="pl" '
        'xml:lang="pl" />' in rendered
    )
    assert 'src="cover.jpg"' in rendered and 'src="map.jpg"' in rendered
    for source_attribute in ('alt="Cover"', 'title="Book"', 'alt="Map"', 'title="Atlas"'):
        assert source_attribute not in rendered
    assert validate_target_semantic_document(rendered).valid


def test_target_semantic_validator_is_pure_and_rejects_unsafe_or_broken_structure():
    good = '<html lang="pl"><body>\n<h1 id="h">T</h1><p><a href="#h">go</a></p><img alt="x" src="cover.jpg"></body></html>'
    assert validate_target_semantic_document(good).valid
    for document in (
        '<html><body><p onclick="x">bad</p></body></html>',
        '<html><body><p><a href="#missing">bad</a></p></body></html>',
        '<html><body><ul><p>bad</p></ul></body></html>',
        '<html><body><script>x</script></body></html>',
        '<html><body><main><body>nested</body></main></body></html>',
        '<html><body>bad\x00</body></html>',
    ):
        assert not validate_target_semantic_document(document).valid


def test_revisions_are_append_only_encrypted_and_activate_by_cas(app):
    project = _project()
    plan, record = _source_plan(project, "<html><body><p>Hello.</p></body></html>")
    block = plan.blocks[0]
    first = persist_target_revision(
        record, _candidate(plan, record, block), target_language="pl", author_type="human", author_user_id=project.user_id
    )
    assert rehydrate_target_revision(first, record).block_id == block.id
    pointer = _activate_with_alignment(record, first, expected_version=0)
    assert pointer.version == 1
    second = persist_target_revision(
        record, _candidate(plan, record, block, text="Nowy przekład."), target_language="pl", author_type="human", author_user_id=project.user_id, parent_revision_id=first.id
    )
    with pytest.raises(BlockContractError, match="target-active-conflict"):
        _activate_with_alignment(record, second, expected_version=0)
    pointer = _activate_with_alignment(record, second, expected_version=1)
    assert pointer.revision_id == second.id and pointer.version == 2
    assert second.target_language == project.target_lang
    assert len(second.revision_record_hash) == 64
    db.session.flush()
    row = db.session.execute(text("SELECT normalized_candidate, rendered_fragment, validation_evidence, source_derived_target_text FROM translation_target_block_revision WHERE id = :id"), {"id": second.id}).one()
    assert all(str(value).startswith("enc:v2:") for value in row)
    assert "Nowy" not in str(row)


def test_target_revision_requires_exact_project_language(app):
    project = _project("target language binding")
    plan, record = _source_plan(project, "<html><body><p>Hello.</p></body></html>")
    with pytest.raises(BlockContractError, match="target-language-project-mismatch"):
        persist_target_revision(
            record,
            _candidate(plan, record, plan.blocks[0]),
            target_language="en",
            author_type="system",
        )


def test_target_revision_persistence_never_commits_the_caller_transaction(app):
    project = _project()
    plan, record = _source_plan(project, "<html><body><p>Hello.</p></body></html>")
    db.session.commit()
    persist_target_revision(
        record,
        _candidate(plan, record, plan.blocks[0]),
        target_language="pl",
        author_type="system",
    )
    assert db.session.query(TranslationTargetBlockActive).count() == 0
    assert db.session.execute(text("SELECT COUNT(*) FROM translation_target_block_revision")).scalar() == 1
    db.session.rollback()
    assert db.session.execute(text("SELECT COUNT(*) FROM translation_target_block_revision")).scalar() == 0


def test_revision_rehydration_rejects_tampering_parent_and_cross_plan(app):
    project = _project()
    plan, record = _source_plan(project, "<html><body><p>Hello.</p></body></html>")
    first = persist_target_revision(record, _candidate(plan, record, plan.blocks[0]), target_language="pl", author_type="system")
    original_record_hash = first.revision_record_hash
    original_candidate_hash = first.candidate_hash
    first.candidate_hash = "0" * 64
    with pytest.raises(BlockContractError, match="target-revision-hash"):
        rehydrate_target_revision(first, record)
    first.candidate_hash = original_candidate_hash
    original_fragment = first.rendered_fragment
    first.rendered_fragment = "<p>tampered</p>"
    with pytest.raises(BlockContractError, match="target-revision-hash"):
        rehydrate_target_revision(first, record)
    first.rendered_fragment = original_fragment
    original_evidence = first.validation_evidence
    first.validation_evidence = {"valid": True}
    with pytest.raises(BlockContractError, match="target-revision-hash"):
        rehydrate_target_revision(first, record)
    first.validation_evidence = original_evidence
    first.author_type = "model"
    with pytest.raises(BlockContractError, match="target-revision-hash"):
        rehydrate_target_revision(first, record)
    first.author_type = "system"
    first.target_language = "en"
    with pytest.raises(BlockContractError, match="target-language-project-mismatch"):
        rehydrate_target_revision(first, record)
    first.target_language = "pl"
    first.revision_record_hash = "0" * 64
    with pytest.raises(BlockContractError, match="target-revision-hash"):
        rehydrate_target_revision(first, record)
    # Restore the original record hash after the deliberately local tamper.
    first.revision_record_hash = original_record_hash
    sibling = persist_target_revision(
        record,
        _candidate(plan, record, plan.blocks[0], text="Sibling."),
        target_language="pl",
        author_type="system",
    )
    child = persist_target_revision(
        record,
        _candidate(plan, record, plan.blocks[0], text="Child."),
        target_language="pl",
        author_type="system",
        parent_revision_id=first.id,
    )
    child.parent_revision_id = sibling.id
    with pytest.raises(BlockContractError, match="target-revision-parent-witness"):
        rehydrate_target_revision(child, record)
    child.parent_revision_id = first.id
    child.author_type = "human"
    child.author_user_id = project.user_id
    with pytest.raises(BlockContractError, match="target-revision-hash"):
        rehydrate_target_revision(child, record)
    child.author_type = "system"
    child.author_user_id = project.user_id
    with pytest.raises(BlockContractError, match="target-revision-hash"):
        rehydrate_target_revision(child, record)
    # Avoid retaining an actual self-referential row in this SQLite fixture;
    # the parent-link integrity was already exercised above.
    child.parent_revision_id = None
    child.parent_revision_hash = None
    child.author_user_id = None
    other = _project("Other target")
    other_plan, other_record = _source_plan(other, "<html><body><p>Hello.</p></body></html>")
    other_revision = persist_target_revision(other_record, _candidate(other_plan, other_record, other_plan.blocks[0]), target_language="pl", author_type="system")
    with pytest.raises(BlockContractError, match="target-parent-owner"):
        persist_target_revision(record, _candidate(plan, record, plan.blocks[0]), target_language="pl", author_type="system", parent_revision_id=other_revision.id)
    with pytest.raises(BlockContractError, match="target-author-owner"):
        persist_target_revision(record, _candidate(plan, record, plan.blocks[0]), target_language="pl", author_type="human", author_user_id=other.user_id)


def test_revision_lineage_witness_and_timestamp_tampering_fail_closed(app):
    project = _project("lineage witness")
    plan, record = _source_plan(project, "<html><body><p>Hello.</p></body></html>")
    root = persist_target_revision(
        record,
        _candidate(plan, record, plan.blocks[0], text="Root."),
        target_language="pl",
        author_type="system",
    )
    child = persist_target_revision(
        record,
        _candidate(plan, record, plan.blocks[0], text="Child."),
        target_language="pl",
        author_type="system",
        parent_revision_id=root.id,
    )
    assert root.parent_revision_id is None and root.parent_revision_hash is None
    assert child.parent_revision_hash == root.revision_record_hash

    root.parent_revision_hash = "0" * 64
    with pytest.raises(BlockContractError, match="target-revision-parent-witness"):
        rehydrate_target_revision(root, record)
    root.parent_revision_hash = None

    child.parent_revision_hash = None
    with pytest.raises(BlockContractError, match="target-revision-parent-witness"):
        rehydrate_target_revision(child, record)
    child.parent_revision_hash = "0" * 64
    with pytest.raises(BlockContractError, match="target-revision-parent-witness"):
        rehydrate_target_revision(child, record)
    child.parent_revision_hash = root.revision_record_hash

    original_created_at = child.created_at
    child.created_at = original_created_at + timedelta(microseconds=1)
    with pytest.raises(BlockContractError, match="target-revision-hash"):
        rehydrate_target_revision(child, record)
    child.created_at = original_created_at

    original_parent_fragment = root.rendered_fragment
    root.rendered_fragment = "<p>tampered parent</p>"
    with pytest.raises(BlockContractError, match="target-revision-hash"):
        rehydrate_target_revision(child, record)
    root.rendered_fragment = original_parent_fragment

    sibling = persist_target_revision(
        record,
        _candidate(plan, record, plan.blocks[0], text="Sibling."),
        target_language="pl",
        author_type="system",
    )
    child.parent_revision_id = sibling.id
    with pytest.raises(BlockContractError, match="target-revision-parent-witness"):
        rehydrate_target_revision(child, record)
    child.parent_revision_id = root.id

    root.parent_revision_id = child.id
    root.parent_revision_hash = child.revision_record_hash
    with pytest.raises(BlockContractError, match="target-revision-lineage-cycle"):
        rehydrate_target_revision(child, record)
    root.parent_revision_id = None
    root.parent_revision_hash = None


def test_revision_lineage_depth_is_bounded(app):
    project = _project("lineage depth")
    plan, record = _source_plan(project, "<html><body><p>Hello.</p></body></html>")
    revision = persist_target_revision(
        record,
        _candidate(plan, record, plan.blocks[0], text="0"),
        target_language="pl",
        author_type="system",
    )
    for index in range(64):
        revision = persist_target_revision(
            record,
            _candidate(plan, record, plan.blocks[0], text=str(index + 1)),
            target_language="pl",
            author_type="system",
            parent_revision_id=revision.id,
        )
    with pytest.raises(BlockContractError, match="target-revision-lineage-depth"):
        rehydrate_target_revision(revision, record)


def test_active_revision_rehydration_requires_complete_unique_block_set(app):
    project = _project()
    plan, record = _source_plan(project, "<html><body><p>One.</p><p>Two.</p></body></html>")
    revisions = [
        persist_target_revision(record, _candidate(plan, record, block), target_language="pl", author_type="system")
        for block in plan.blocks
    ]
    _activate_with_alignment(record, revisions[0], expected_version=0)
    with pytest.raises(BlockContractError, match="target-active-cardinality"):
        active_target_candidates(record)
    _activate_with_alignment(record, revisions[1], expected_version=0)
    assert list(active_target_candidates(record)) == [block.id for block in plan.blocks]
    pointers = db.session.query(TranslationTargetBlockActive).all()
    with pytest.raises(BlockContractError, match="target-active-cardinality"):
        _active_pointer_map(plan, record.id, [*pointers, pointers[0]])
    assert "Przekład" in render_persisted_target_document(record)


def test_alignment_projection_and_witnesses_are_exact(app):
    """Tokens are zero-width; literals remain exact offset-bearing content."""

    from types import SimpleNamespace

    block_stub = SimpleNamespace(
        slots=(
            SimpleNamespace(
                id="literal",
                kind="text",
                disposition="literal-preserved",
                source_value="[KEEP]",
            ),
        ),
        wire=(SimpleNamespace(kind="slot", ref_id="literal"),),
    )
    candidate = TargetBlockCandidate(
        source_plan_id="p", source_plan_hash="a" * 64, block_id="b",
        structural_fingerprint="b" * 64,
        parts=(TargetTextPart(kind="text", text="A"), TargetTextPart(kind="token", token_id="inline"), TargetTextPart(kind="literal", literal_slot_id="literal"), TargetTextPart(kind="text", text="B")),
        lexical_attributes=(),
    )
    assert target_alignment_projection(block_stub, candidate) == "A[KEEP]B"

    project = _project("Alignment witness")
    plan, record = _source_plan(project, "<html><body><p>One. Two.</p></body></html>")
    revision = persist_target_revision(record, _candidate(plan, record, plan.blocks[0], text="Jeden. Dwa."), target_language="pl", author_type="system")
    span_set = persist_target_span_set(record, revision)
    spans = rehydrate_target_span_set(span_set, record)
    assert [(span.start, span.end) for span in spans] == [(0, 7), (7, 11)]
    alignment = persist_alignment_revision(record, span_set)
    assert alignment.review_state == "needs_review" and len(rehydrate_alignment_revision(alignment, record)) == 2
    pointer = activate_target_revision(record, block_id=revision.block_id, revision_id=revision.id, alignment_revision_id=alignment.id, expected_version=0)
    assert pointer.version == 1
    original = span_set.spans[0].text_hash
    span_set.spans[0].text_hash = "0" * 64
    with pytest.raises(BlockContractError, match="alignment-span-set-hash"):
        rehydrate_target_span_set(span_set, record)
    span_set.spans[0].text_hash = original
    anchors = list(record.anchors)
    if len(anchors) >= 2:
        with pytest.raises(BlockContractError, match="alignment-group-source-order"):
            persist_alignment_revision(record, span_set, groups=(AlignmentGroup((anchors[1].id, anchors[0].id), tuple(span.id for span in span_set.spans), "manual-v1", 0.1, "needs_review", {}),), method="manual-v1")


def test_two_span_boundary_tamper_rejects_even_after_public_hash_recompute(app):
    """Parent receipts remain authoritative when public hashes are forged."""

    project = _project("Boundary receipt")
    plan, record = _source_plan(
        project, "<html><body><p>One. Two.</p></body></html>"
    )
    revision = persist_target_revision(
        record,
        _candidate(plan, record, plan.blocks[0], text="Jeden. Dwa."),
        target_language="pl",
        author_type="system",
    )
    span_set = persist_target_span_set(record, revision)
    alignment = persist_alignment_revision(record, span_set)
    _activate_with_alignment(record, revision, expected_version=0)
    db.session.commit()
    rows = list(
        db.session.query(TranslationTargetSpanSet)
        .filter_by(id=span_set.id)
        .one()
        .spans
    )
    rows.sort(key=lambda item: item.order_index)
    assert len(rows) == 2
    first, second = rows
    first_end = first.end_offset - 1
    second_start = second.start_offset - 1
    first_span_hash = sha256_text(
        canonical_json(
            {
                "id": first.id,
                "order": first.order_index,
                "start": first.start_offset,
                "end": first_end,
                "text_hash": first.text_hash,
            }
        )
    )
    second_span_hash = sha256_text(
        canonical_json(
            {
                "id": second.id,
                "order": second.order_index,
                "start": second_start,
                "end": second.end_offset,
                "text_hash": second.text_hash,
            }
        )
    )
    db.session.execute(
        text(
            "UPDATE translation_target_span SET end_offset = :first_end, "
            "span_hash = :first_hash WHERE id = :first_id"
        ),
        {
            "first_end": first_end,
            "first_hash": first_span_hash,
            "first_id": first.id,
        },
    )
    db.session.execute(
        text(
            "UPDATE translation_target_span SET start_offset = :second_start, "
            "span_hash = :second_hash WHERE id = :second_id"
        ),
        {
            "second_start": second_start,
            "second_hash": second_span_hash,
            "second_id": second.id,
        },
    )
    forged_target_witness = sha256_text(
        canonical_json(
            [
                {
                    "id": first.id,
                    "order": first.order_index,
                    "start": first.start_offset,
                    "end": first_end,
                    "text_hash": first.text_hash,
                    "span_hash": first_span_hash,
                }
            ]
        )
    )
    db.session.execute(
        text(
            "UPDATE translation_alignment_group SET target_witness_hash = :hash "
            "WHERE alignment_revision_id = :alignment AND order_index = 1"
        ),
        {"hash": forged_target_witness, "alignment": alignment.id},
    )
    db.session.commit()
    db.session.expire_all()
    with pytest.raises(BlockContractError, match="alignment-span-set-hash"):
        rehydrate_target_span_set(
            db.session.get(TranslationTargetSpanSet, span_set.id), record
        )
    with pytest.raises(BlockContractError, match="alignment-span-set-hash"):
        rehydrate_alignment_revision(
            db.session.get(TranslationAlignmentRevision, alignment.id), record
        )
    with pytest.raises(BlockContractError, match="alignment-span-set-hash"):
        active_target_candidates(record)


def test_active_target_reader_ignores_dirty_identity_map_and_external_pointer_change(app):
    project = _project("Active reader freshness")
    plan, record = _source_plan(
        project, "<html><body><p>One.</p></body></html>"
    )
    first = persist_target_revision(
        record,
        _candidate(plan, record, plan.blocks[0], text="Pierwszy."),
        target_language="pl",
        author_type="system",
    )
    first_pointer = _activate_with_alignment(record, first, expected_version=0)
    second = persist_target_revision(
        record,
        _candidate(plan, record, plan.blocks[0], text="Drugi."),
        target_language="pl",
        author_type="system",
    )
    second_span_set = persist_target_span_set(record, second)
    second_alignment = persist_alignment_revision(record, second_span_set)
    db.session.commit()
    second_id = second.id
    second_alignment_id = second_alignment.id
    dirty_pointer = db.session.get(
        TranslationTargetBlockActive,
        {"source_plan_id": record.id, "block_id": first.block_id},
    )
    assert dirty_pointer is first_pointer
    dirty_pointer.revision_id = "not-the-durable-revision"
    project.name = "caller-dirty-but-not-flushed"
    session_factory = sessionmaker(bind=db.engine)
    external = session_factory()
    try:
        external_pointer = external.get(
            TranslationTargetBlockActive,
            {"source_plan_id": record.id, "block_id": first.block_id},
        )
        external_alignment_pointer = external.get(
            TranslationTargetAlignmentActive,
            {"source_plan_id": record.id, "block_id": first.block_id},
        )
        assert external_pointer is not None and external_alignment_pointer is not None
        external_pointer.revision_id = second_id
        external_pointer.version += 1
        external_pointer.updated_at = utc_now_db()
        external_alignment_pointer.revision_id = second_id
        external_alignment_pointer.alignment_revision_id = second_alignment_id
        external_alignment_pointer.version = external_pointer.version
        external_alignment_pointer.updated_at = external_pointer.updated_at
        external.flush()
        external.refresh(external_pointer)
        external.refresh(external_alignment_pointer)
        pair_receipt = _pair_receipt(
            external_pointer, external_alignment_pointer, session=external
        )
        external.execute(
            text(
                "UPDATE translation_target_block_active SET pair_receipt = :receipt "
                "WHERE source_plan_id = :plan AND block_id = :block"
            ),
            {
                "receipt": pair_receipt,
                "plan": record.id,
                "block": first.block_id,
            },
        )
        external.commit()
    finally:
        external.close()
    result = active_target_candidates(record)
    assert result[first.block_id].parts[0].text == "Drugi."
    assert dirty_pointer.revision_id == second_id
    assert project.name == "caller-dirty-but-not-flushed"
    db.session.rollback()
    db.session.expire_all()
    assert db.session.get(TranslationTargetBlockActive, {"source_plan_id": record.id, "block_id": first.block_id}).revision_id == second_id


def test_attribute_scope_aligns_exact_normalized_lexical_value_and_activates(app):
    project = _project("Attribute alignment")
    plan, record = _source_plan(project, '<html><body><img alt="Source alt" title="Source title" /></body></html>')
    blocks = [block for block in plan.blocks if block.role == "accessibility_text"]
    assert len(blocks) == 2
    for index, block in enumerate(blocks, start=1):
        revision = persist_target_revision(
            record,
            _candidate(plan, record, block, lexical_value=f"Docelowy opis {index}."),
            target_language="pl",
            author_type="system",
        )
        span_set = persist_target_span_set(record, revision)
        assert span_set.projection_scope == f"attribute:{block.slots[0].id}"
        assert span_set.projection == f"Docelowy opis {index}." and span_set.span_count == 1
        alignment = persist_alignment_revision(record, span_set)
        activate_target_revision(record, block_id=block.id, revision_id=revision.id, alignment_revision_id=alignment.id, expected_version=0)
    assert set(active_target_candidates(record)) == {block.id for block in blocks}


def test_active_pair_receipt_rejects_pointer_and_timestamp_tampering(app):
    project = _project("pair receipt")
    plan, record = _source_plan(project, "<html><body><p>One.</p></body></html>")
    revision = persist_target_revision(record, _candidate(plan, record, plan.blocks[0]), target_language="pl", author_type="system")
    pointer = _activate_with_alignment(record, revision, expected_version=0)
    active_alignment = db.session.get(
        TranslationTargetAlignmentActive,
        {"source_plan_id": record.id, "block_id": revision.block_id},
    )
    assert active_alignment is not None
    original_alignment_id = active_alignment.alignment_revision_id
    assert active_alignment.updated_at == pointer.updated_at
    alternate_revision = persist_target_revision(
        record,
        _candidate(plan, record, plan.blocks[0], text="Alternate."),
        target_language="pl",
        author_type="system",
    )
    alternate_span_set = persist_target_span_set(record, alternate_revision)
    persist_alignment_revision(record, alternate_span_set)
    alternate_same_revision_alignment = persist_alignment_revision(
        record,
        persist_target_span_set(record, revision),
    )
    db.session.commit()
    db.session.execute(
        text(
            "UPDATE translation_target_block_active SET revision_id = :revision "
            "WHERE source_plan_id = :plan AND block_id = :block"
        ),
        {"revision": alternate_revision.id, "plan": record.id, "block": revision.block_id},
    )
    db.session.commit()
    db.session.expire_all()
    with pytest.raises(BlockContractError, match="target-active"):
        active_target_candidates(record)
    db.session.execute(
        text(
            "UPDATE translation_target_block_active SET revision_id = :revision "
            "WHERE source_plan_id = :plan AND block_id = :block"
        ),
        {"revision": revision.id, "plan": record.id, "block": revision.block_id},
    )
    db.session.execute(
        text(
            "UPDATE translation_target_alignment_active SET alignment_revision_id = :alignment "
            "WHERE source_plan_id = :plan AND block_id = :block"
        ),
        {
            "alignment": alternate_same_revision_alignment.id,
            "plan": record.id,
            "block": revision.block_id,
        },
    )
    db.session.commit()
    db.session.expire_all()
    with pytest.raises(BlockContractError, match="target-active-pair"):
        active_target_candidates(record)
    db.session.execute(
        text(
            "UPDATE translation_target_alignment_active SET alignment_revision_id = :alignment "
            "WHERE source_plan_id = :plan AND block_id = :block"
        ),
        {"alignment": original_alignment_id, "plan": record.id, "block": revision.block_id},
    )
    db.session.commit()
    db.session.expire_all()
    assert active_target_candidates(record)[revision.block_id].block_id == revision.block_id
    original = pointer.updated_at
    db.session.execute(text("UPDATE translation_target_block_active SET updated_at = :timestamp WHERE source_plan_id = :plan AND block_id = :block"), {"timestamp": original.replace(year=original.year - 1), "plan": record.id, "block": revision.block_id})
    db.session.commit()
    db.session.expire_all()
    with pytest.raises(BlockContractError, match="target-active-pair"):
        active_target_candidates(record)


def test_retained_receipts_rehydrate_chain_and_seal_every_decrypted_field(app):
    """Old receipt keys remain valid, but no encrypted material is portable."""

    project = _project("receipt rotation")
    plan, record = _source_plan(project, "<html><body><p>One.</p></body></html>")
    revision = persist_target_revision(
        record,
        _candidate(plan, record, plan.blocks[0]),
        target_language="pl",
        author_type="system",
    )
    span_set = persist_target_span_set(record, revision)
    alignment = persist_alignment_revision(record, span_set)
    _activate_with_alignment(record, revision, expected_version=0)
    ids = (record.id, revision.id, span_set.id, alignment.id)
    db.session.commit()
    raw = db.session.execute(
        text(
            "SELECT normalized_candidate, rendered_fragment, validation_evidence, "
            "source_derived_target_text, candidate_hash, normalized_hash "
            "FROM translation_target_block_revision "
            "WHERE id = :id"
        ),
        {"id": revision.id},
    ).one()
    assert all(str(value).startswith("enc:v2:") for value in raw[:4])
    candidate = TargetBlockCandidate.from_dict(revision.normalized_candidate)
    assert raw.candidate_hash != candidate.candidate_hash
    assert raw.normalized_hash != sha256_text(canonical_json(candidate.to_dict()))
    first_span = span_set.spans[0]
    assert first_span.text_hash != sha256_text(
        span_set.projection[first_span.start_offset : first_span.end_offset]
    )
    app.extensions["carlina_encryption"].key_store.rotate()
    db.session.remove()

    rotated_record = db.session.get(TranslationSourcePlan, ids[0])
    rotated_revision = db.session.get(TranslationTargetBlockRevision, ids[1])
    rotated_span_set = db.session.get(TranslationTargetSpanSet, ids[2])
    rotated_alignment = db.session.get(TranslationAlignmentRevision, ids[3])
    assert rotated_record and rotated_revision and rotated_span_set and rotated_alignment
    assert rehydrate_target_revision(rotated_revision, rotated_record).block_id == revision.block_id
    assert rehydrate_target_span_set(rotated_span_set, rotated_record)
    assert rehydrate_alignment_revision(rotated_alignment, rotated_record)
    assert active_target_candidates(rotated_record)[revision.block_id].block_id == revision.block_id
    assert persist_target_span_set(rotated_record, rotated_revision).id == rotated_span_set.id

    def fresh_chain(label: str):
        item_project = _project(label)
        item_plan, item_record = _source_plan(
            item_project, "<html><body><p>One.</p></body></html>"
        )
        item_revision = persist_target_revision(
            item_record,
            _candidate(item_plan, item_record, item_plan.blocks[0]),
            target_language="pl",
            author_type="system",
        )
        item_span_set = persist_target_span_set(item_record, item_revision)
        item_alignment = persist_alignment_revision(item_record, item_span_set)
        db.session.commit()
        return item_record, item_revision, item_span_set, item_alignment

    for field, value in (
        (
            "normalized_candidate",
            {
                **rotated_revision.normalized_candidate,
                "parts": [
                    {
                        **rotated_revision.normalized_candidate["parts"][0],
                        "text": "Tampered.",
                    }
                ],
            },
        ),
        ("rendered_fragment", "<p>Tampered.</p>"),
        ("validation_evidence", {"findings": [{"code": "tampered"}]}),
        ("source_derived_target_text", "Tampered."),
    ):
        item_record, item_revision, _item_span_set, _item_alignment = fresh_chain(
            f"target receipt {field}"
        )
        setattr(item_revision, field, value)
        db.session.commit()
        db.session.expire_all()
        with pytest.raises(BlockContractError, match="target-revision-hash"):
            rehydrate_target_revision(
                db.session.get(TranslationTargetBlockRevision, item_revision.id), item_record
            )

    item_record, _item_revision, item_span_set, _item_alignment = fresh_chain(
        "span receipt projection"
    )
    item_span_set.projection = "Tampered."
    db.session.commit()
    db.session.expire_all()
    with pytest.raises(BlockContractError, match="alignment-span-set-hash"):
        rehydrate_target_span_set(
            db.session.get(TranslationTargetSpanSet, item_span_set.id), item_record
        )

    item_record, _item_revision, _item_span_set, item_alignment = fresh_chain(
        "alignment receipt payload"
    )
    item_alignment.payload = {"contract_version": "tampered", "groups": []}
    db.session.commit()
    db.session.expire_all()
    with pytest.raises(BlockContractError, match="alignment-revision-hash"):
        rehydrate_alignment_revision(
            db.session.get(TranslationAlignmentRevision, item_alignment.id), item_record
        )


def test_target_receipt_rejects_same_scope_ciphertext_and_hash_transplant(app):
    project = _project("same scope transplant")
    plan, record = _source_plan(project, "<html><body><p>One.</p></body></html>")
    source = persist_target_revision(
        record,
        _candidate(plan, record, plan.blocks[0], text="Source."),
        target_language="pl",
        author_type="system",
    )
    recipient = persist_target_revision(
        record,
        _candidate(plan, record, plan.blocks[0], text="Recipient."),
        target_language="pl",
        author_type="system",
    )
    db.session.commit()
    copied = db.session.execute(
        text(
            "SELECT normalized_candidate, rendered_fragment, validation_evidence, "
            "source_derived_target_text, candidate_hash, normalized_hash, fragment_hash, "
            "evidence_hash, revision_record_hash FROM translation_target_block_revision "
            "WHERE id = :id"
        ),
        {"id": source.id},
    ).mappings().one()
    db.session.execute(
        text(
            "UPDATE translation_target_block_revision SET "
            "normalized_candidate = :normalized_candidate, "
            "rendered_fragment = :rendered_fragment, "
            "validation_evidence = :validation_evidence, "
            "source_derived_target_text = :source_derived_target_text, "
            "candidate_hash = :candidate_hash, normalized_hash = :normalized_hash, "
            "fragment_hash = :fragment_hash, evidence_hash = :evidence_hash, "
            "revision_record_hash = :revision_record_hash WHERE id = :id"
        ),
        {**copied, "id": recipient.id},
    )
    db.session.commit()
    db.session.expire_all()
    with pytest.raises(BlockContractError, match="target-revision-hash"):
        rehydrate_target_revision(
            db.session.get(TranslationTargetBlockRevision, recipient.id), record
        )


def test_alignment_rejects_empty_ranges_and_false_approval(app):
    project = _project("Alignment confidence")
    plan, record = _source_plan(project, "<html><body><p>One.</p></body></html>")
    block = plan.blocks[0]
    revision = persist_target_revision(record, _candidate(plan, record, block), target_language="pl", author_type="system")
    span_set = persist_target_span_set(record, revision)
    anchor = record.anchors[0]
    span = span_set.spans[0]
    with pytest.raises(BlockContractError, match="alignment-group-cardinality"):
        AlignmentGroup((anchor.id,), (), "manual-v1", 0.5, "needs_review", {})
    with pytest.raises(BlockContractError, match="alignment-confidence-approval"):
        persist_alignment_revision(
            record,
            span_set,
            groups=(AlignmentGroup((anchor.id,), (span.id,), "manual-v1", 0.0, "approved", {}),),
            method="manual-v1",
            review_state="approved",
        )
