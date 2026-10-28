"""Acceptance tests for alignment-witnessed Translator memory evidence."""

from __future__ import annotations

import hashlib
from datetime import timedelta
from pathlib import Path

import pytest
from content_storage_support import bind_content_policy
from flask import Flask
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from carlina.core.semantic_html import SEMANTIC_HTML_CONTRACT_VERSION
from iarf_assistant.addons.translator.alignment_contract import AlignmentGroup
from iarf_assistant.addons.translator.alignment_persistence import (
    persist_alignment_revision,
    persist_target_span_set,
)
from iarf_assistant.addons.translator.block_contract import (
    AttestedDocumentSnapshot,
    BlockContractError,
)
from iarf_assistant.addons.translator.block_persistence import (
    persist_source_plan,
    source_attestation_for_project,
)
from iarf_assistant.addons.translator.block_planner import plan_document_blocks
from iarf_assistant.addons.translator.models import (
    TranslationMemoryEntry,
    TranslationProject,
    TranslationTargetBlockActive,
)
from iarf_assistant.addons.translator.target_contract import (
    TargetBlockCandidate,
    TargetTextPart,
    structural_fingerprint,
)
from iarf_assistant.addons.translator.target_persistence import (
    activate_target_revision,
    persist_target_revision,
)
from iarf_assistant.addons.translator.translation_memory_persistence import (
    derive_translation_memory,
    lookup_translation_memory,
    rehydrate_translation_memory_entry,
)
from iarf_assistant.core.encryption import EncryptionService, InstallationKeyStore
from iarf_assistant.extensions import db
from iarf_assistant.models import User


@pytest.fixture
def app(tmp_path: Path):
    application = Flask(__name__, instance_path=str(tmp_path / "instance"))
    bind_content_policy(application)
    application.config.update(
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{tmp_path / 'memory.db'}",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SECRET_KEY="memory-test",
        TESTING=True,
    )
    db.init_app(application)
    context = application.app_context()
    context.push()
    keys = InstallationKeyStore(tmp_path / "keys.json")
    keys.initialize()
    application.extensions["carlina_encryption"] = EncryptionService(keys)
    db.create_all()
    try:
        yield application
    finally:
        db.session.remove()
        db.drop_all()
        db.engine.dispose()
        context.pop()


def _project(name: str = "Memory") -> TranslationProject:
    owner = User(email=f"{name.lower()}@example.test", password_hash="x")
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


def _source_plan(project: TranslationProject, html: str):
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
    )
    record, _ = persist_source_plan(project, plan)
    db.session.flush()
    return plan, record


def _candidate(plan, record, block, target: str) -> TargetBlockCandidate:
    return TargetBlockCandidate(
        source_plan_id=record.id,
        source_plan_hash=plan.plan_hash,
        block_id=block.id,
        structural_fingerprint=structural_fingerprint(plan, block),
        parts=(TargetTextPart(kind="text", text=target),),
        lexical_attributes=(),
    )


def _activate(record, plan, target: str, *, groups=None, expected_version=0):
    block = plan.blocks[0]
    revision = persist_target_revision(
        record,
        candidate=_candidate(plan, record, block, target),
        author_type="model",
        target_language="pl",
    )
    span_set = persist_target_span_set(record, revision)
    normalized_groups = groups
    if normalized_groups is None:
        normalized_groups = [
            AlignmentGroup(
                (anchor.id,),
                (span.id,),
                "test-approved",
                0.95,
                "approved",
                {"test": True},
            )
            for anchor, span in zip(block.anchors, span_set.spans, strict=True)
        ]
    alignment = persist_alignment_revision(
        record,
        span_set,
        groups=normalized_groups,
        method="test-approved",
        review_state="approved",
    )
    activate_target_revision(
        record,
        block_id=block.id,
        revision_id=revision.id,
        alignment_revision_id=alignment.id,
        expected_version=expected_version,
    )
    db.session.commit()
    return record, block, revision, span_set, alignment


def test_verified_one_to_one_derives_encrypted_sentence_memory_and_is_idempotent(app):
    project = _project()
    plan, record = _source_plan(project, "<html><body><p>Hello.</p></body></html>")
    record, _block, revision, span_set, alignment = _activate(record, plan, "Cześć.")
    rows = derive_translation_memory(record)
    assert len(rows) == 1
    row = rows[0]
    assert row.classification == "contextual_precedent"
    assert row.granularity == "sentence"
    assert row.scope_kind == "project"
    assert row.target_revision_id == revision.id
    assert row.target_span_set_id == span_set.id
    assert row.alignment_revision_id == alignment.id
    raw = db.session.execute(text("SELECT source_text, provenance_json FROM translation_memory_entry")).one()
    assert "Hello." not in str(raw)
    assert "Cześć." not in str(raw)
    assert rehydrate_translation_memory_entry(row, record)["target_text"] == "Cześć."
    assert len(derive_translation_memory(record)) == 1
    assert TranslationMemoryEntry.query.count() == 1


def test_ambiguous_group_is_one_complete_block_entry_never_sentence_pairs(app):
    project = _project("Ambiguous")
    plan, record = _source_plan(project, "<html><body><p>One. Two.</p></body></html>")
    block = plan.blocks[0]
    target = "Jedno i dwa."
    revision = persist_target_revision(
        record,
        candidate=_candidate(plan, record, block, target),
        author_type="model",
        target_language="pl",
    )
    spans = persist_target_span_set(record, revision)
    alignment = persist_alignment_revision(
        record,
        spans,
        groups=[
            AlignmentGroup(
                tuple(anchor.id for anchor in block.anchors),
                tuple(span.id for span in spans.spans),
                "test-approved",
                0.95,
                "approved",
                {"test": True},
            )
        ],
        method="test-approved",
        review_state="approved",
    )
    activate_target_revision(record, block_id=block.id, revision_id=revision.id, alignment_revision_id=alignment.id, expected_version=0)
    db.session.commit()
    rows = derive_translation_memory(record)
    assert len(rows) == 1
    assert rows[0].granularity == "block"
    assert rows[0].source_text == "One. Two."
    assert rows[0].target_text == target


def test_each_witnessed_group_rehydrates_independently_in_mixed_alignment(app):
    project = _project("MixedGroups")
    plan, record = _source_plan(
        project, "<html><body><p>One. Two. Three.</p></body></html>"
    )
    block = plan.blocks[0]
    revision = persist_target_revision(
        record,
        candidate=_candidate(plan, record, block, "Jeden. Dwa i trzy."),
        author_type="model",
        target_language="pl",
    )
    spans = persist_target_span_set(record, revision)
    alignment = persist_alignment_revision(
        record,
        spans,
        groups=[
            AlignmentGroup(
                (block.anchors[0].id,),
                (spans.spans[0].id,),
                "mixed",
                0.95,
                "approved",
                {},
            ),
            AlignmentGroup(
                (block.anchors[1].id, block.anchors[2].id),
                (spans.spans[1].id,),
                "mixed",
                0.95,
                "approved",
                {},
            ),
        ],
        method="mixed",
        review_state="approved",
    )
    activate_target_revision(
        record,
        block_id=block.id,
        revision_id=revision.id,
        alignment_revision_id=alignment.id,
        expected_version=0,
    )
    db.session.commit()

    rows = derive_translation_memory(record)
    assert {row.granularity for row in rows} == {"sentence", "block"}
    sentence = next(row for row in rows if row.granularity == "sentence")
    ambiguous = next(row for row in rows if row.granularity == "block")
    sentence_value = rehydrate_translation_memory_entry(sentence, record)
    ambiguous_value = rehydrate_translation_memory_entry(ambiguous, record)
    assert sentence_value["source_text"] == "One. "
    assert sentence_value["target_text"] == "Jeden. "
    assert ambiguous_value["source_text"] == "Two. Three."
    assert ambiguous_value["target_text"] == "Dwa i trzy."
    assert sentence_value["alignment_group_ids"] != ambiguous_value["alignment_group_ids"]


def test_unknown_or_swapped_group_ids_fail_closed(app):
    project = _project("GroupTamper")
    plan, record = _source_plan(
        project, "<html><body><p>One. Two.</p></body></html>"
    )
    block = plan.blocks[0]
    revision = persist_target_revision(
        record,
        candidate=_candidate(plan, record, block, "Jeden. Dwa."),
        author_type="model",
        target_language="pl",
    )
    spans = persist_target_span_set(record, revision)
    alignment = persist_alignment_revision(
        record,
        spans,
        groups=[
            AlignmentGroup(
                (anchor.id,),
                (span.id,),
                "two",
                0.95,
                "approved",
                {},
            )
            for anchor, span in zip(block.anchors, spans.spans, strict=True)
        ],
        method="two",
        review_state="approved",
    )
    activate_target_revision(
        record,
        block_id=block.id,
        revision_id=revision.id,
        alignment_revision_id=alignment.id,
        expected_version=0,
    )
    db.session.commit()
    rows = derive_translation_memory(record)
    assert len(rows) == 2
    first, second = rows
    first.alignment_group_ids_json = ["unknown-group"]
    with pytest.raises(BlockContractError):
        rehydrate_translation_memory_entry(first, record)
    db.session.rollback()
    first.alignment_group_ids_json = second.alignment_group_ids_json
    with pytest.raises(BlockContractError):
        rehydrate_translation_memory_entry(first, record)
    db.session.rollback()


def test_historical_memory_survives_later_activation_but_receipt_tamper_does_not(app):
    project = _project("Historical")
    plan, record = _source_plan(project, "<html><body><p>Hello.</p></body></html>")
    _activate(record, plan, "Cześć.")
    old_row = derive_translation_memory(record)[0]
    _activate(record, plan, "Witaj.", expected_version=1)
    assert rehydrate_translation_memory_entry(old_row, record)["target_text"] == "Cześć."
    old_row.target_activation_at = old_row.target_activation_at + timedelta(seconds=1)
    with pytest.raises(BlockContractError):
        rehydrate_translation_memory_entry(old_row, record)
    db.session.rollback()
    old_row.active_pair_receipt = "0" * 64
    with pytest.raises(BlockContractError):
        rehydrate_translation_memory_entry(old_row, record)


def test_current_pointer_tamper_blocks_new_derivation(app):
    project = _project("PointerTamper")
    plan, record = _source_plan(project, "<html><body><p>Hello.</p></body></html>")
    _activate(record, plan, "Cześć.")
    pointer = db.session.get(
        TranslationTargetBlockActive,
        {"source_plan_id": record.id, "block_id": plan.blocks[0].id},
    )
    assert pointer is not None
    pointer.pair_receipt = "0" * 64
    db.session.commit()
    with pytest.raises(BlockContractError):
        derive_translation_memory(record)


def test_ineligible_alignment_derives_nothing_and_locked_exact_fails_closed(app):
    project = _project("Ineligible")
    plan, record = _source_plan(project, "<html><body><p>Hello.</p></body></html>")
    block = plan.blocks[0]
    revision = persist_target_revision(
        record,
        candidate=_candidate(plan, record, block, "Cześć."),
        author_type="model",
        target_language="pl",
    )
    span_set = persist_target_span_set(record, revision)
    alignment = persist_alignment_revision(
        record,
        span_set,
        groups=[AlignmentGroup((block.anchors[0].id,), (span_set.spans[0].id,), "low", 0.2, "needs_review", {})],
        method="low",
        review_state="needs_review",
    )
    activate_target_revision(record, block_id=block.id, revision_id=revision.id, alignment_revision_id=alignment.id, expected_version=0)
    db.session.commit()
    assert derive_translation_memory(record) == []
    with pytest.raises(BlockContractError, match="memory-locked-exact"):
        derive_translation_memory(record, classification="locked_exact")
    with pytest.raises(BlockContractError, match="memory-human-acceptance-required"):
        derive_translation_memory(record, classification="deliberate_variant")


def test_lookup_is_project_and_owner_scoped_and_key_rotation_preserves_memory(app):
    project = _project("Lookup")
    plan, record = _source_plan(project, "<html><body><p>Hello.</p></body></html>")
    _activate(record, plan, "Cześć.")
    row = derive_translation_memory(record)[0]
    assert lookup_translation_memory(owner_user_id=project.user_id, project_id=project.id, source_text="hello.", source_language="en", target_language="pl") == [row]
    assert lookup_translation_memory(owner_user_id=project.user_id + 1, project_id=project.id, source_text="Hello.", source_language="en", target_language="pl") == []
    service = app.extensions["carlina_encryption"]
    service.key_store.rotate()
    assert rehydrate_translation_memory_entry(row, record)["source_text"] == "Hello."


def test_independent_session_derivation_does_not_commit_and_tampering_fails_closed(app):
    project = _project("Independent")
    plan, record = _source_plan(project, "<html><body><p>Hello.</p></body></html>")
    _activate(record, plan, "Cześć.")
    factory = sessionmaker(bind=db.engine)
    independent = factory()
    try:
        with pytest.raises(BlockContractError, match="memory-source-plan-session"):
            derive_translation_memory(record, session=independent)
        independent_record = independent.get(type(record), record.id)
        assert independent_record is not None
        rows = derive_translation_memory(independent_record, session=independent)
        assert len(rows) == 1
        independent.rollback()
    finally:
        independent.close()
    assert TranslationMemoryEntry.query.count() == 0
    row = derive_translation_memory(record)[0]
    db.session.execute(text("UPDATE translation_memory_entry SET target_text = :value"), {"value": "tampered"})
    db.session.commit()
    with pytest.raises(BlockContractError):
        rehydrate_translation_memory_entry(row, record)
