"""Focused tests for bounded linked-note context."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from content_storage_support import bind_content_policy
from flask import Flask

from carlina.core.semantic_html import SEMANTIC_HTML_CONTRACT_VERSION
from iarf_assistant.addons.translator.block_contract import (
    AttestedDocumentSnapshot,
    SourceProviderAttestation,
)
from iarf_assistant.addons.translator.block_planner import plan_document_blocks
from iarf_assistant.addons.translator.linked_note_context import (
    LINKED_NOTES_CONTRACT_VERSION,
    linked_notes_slot,
)
from iarf_assistant.addons.translator.models import TranslationTargetBlockActive
from iarf_assistant.addons.translator.note_relations import build_note_relations
from iarf_assistant.addons.translator.target_contract import structural_fingerprint
from iarf_assistant.addons.translator.window_contract import TranslationWindow
from iarf_assistant.core.encryption import EncryptionService, InstallationKeyStore
from iarf_assistant.extensions import db
from tests.test_translator_block_translation import (
    _seed_target_for_slot,
    _state,
)
from tests.test_translator_note_relations import _by_role


def _plan(source: str):
    digest = hashlib.sha256(source.encode()).hexdigest()
    snapshot = AttestedDocumentSnapshot(
        id="document-1",
        document_id="document-1",
        output_html=source,
        content_hash=digest,
        semantic_content_hash=digest,
        metadata={},
        provenance={},
        semantic_contract_version=SEMANTIC_HTML_CONTRACT_VERSION,
    )
    return plan_document_blocks(
        snapshot,
        project_id="project-1",
        source_attestation=SourceProviderAttestation(
            provider_addon_id="document-provider",
            provider_capability_id="document.processing",
            owner_id="owner-1",
            resource_id="document-1",
        ),
        source_language="en",
    )


def _window(plan, block, *, order: int = 1) -> TranslationWindow:
    return TranslationWindow(
        order,
        (block.id,),
        (structural_fingerprint(plan, block),),
    )


@pytest.fixture
def app(tmp_path: Path):
    application = Flask(__name__, instance_path=str(tmp_path / "instance"))
    bind_content_policy(application)
    application.config.update(
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{tmp_path / 'linked-notes.db'}",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SECRET_KEY="linked-notes-test",
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
        db.engine.dispose()
        context.pop()


def test_distinct_targets_with_identical_body_stay_distinct() -> None:
    plan = _plan(
        '<html><body><p>Main <a class="footnote-ref" href="#n1">*</a>'
        '<a class="footnote-ref" href="#n2">*</a></p>'
        '<aside id="n1" role="doc-footnote"><p>Same body.</p></aside>'
        '<aside id="n2" role="doc-footnote"><p>Same body.</p></aside>'
        "</body></html>"
    )
    main = _by_role(plan, "paragraph")[0]
    slot = linked_notes_slot(plan, _window(plan, main), source_plan_id="plan-1")

    assert slot is not None
    payload = json.loads(slot.content)
    assert payload["contract_version"] == LINKED_NOTES_CONTRACT_VERSION
    assert [row["reference_ns"] for row in payload["relationships"]] == [[1], [1]]
    assert [row["note_number"] for row in payload["relationships"]] == [1, 2]
    assert [row["related_source"][0]["source_text"] for row in payload["relationships"]] == [
        "Same body.",
        "Same body.",
    ]


def test_repeated_reference_identity_has_one_relationship_row() -> None:
    plan = _plan(
        '<html><body><p>Main <a role="doc-noteref" href="#n">1</a>'
        '<a role="doc-noteref" href="#n">again</a></p>'
        '<aside id="n" role="doc-footnote"><p>Note body.</p></aside>'
        "</body></html>"
    )
    main = _by_role(plan, "paragraph")[0]
    slot = linked_notes_slot(plan, _window(plan, main), source_plan_id="plan-1")

    assert slot is not None
    row = json.loads(slot.content)["relationships"][0]
    assert row["reference_ns"] == [1]
    assert row["note_number"] == 1


def test_note_before_source_is_emitted_as_reverse_context() -> None:
    plan = _plan(
        '<html><body><aside id="n" role="doc-footnote"><p>Earlier note.</p>'
        '</aside><p>Main <a role="doc-noteref" href="#n">1</a></p>'
        "</body></html>"
    )
    main = _by_role(plan, "paragraph")[0]
    slot = linked_notes_slot(plan, _window(plan, main), source_plan_id="plan-1")

    assert slot is not None
    row = json.loads(slot.content)["relationships"][0]
    assert row["reference_ns"] == [1]
    assert row["related_source"][0]["source_text"] == "Earlier note."


def test_active_target_wording_is_verified_and_dependency_scoped(app) -> None:
    _project, plan, record, _operation = _state(
        '<html><body><p>Main <a role="doc-noteref" href="#n">1</a></p>'
        '<aside id="n" role="doc-footnote"><p>Note body.</p></aside>'
        "</body></html>"
    )
    main = _by_role(plan, "paragraph")[0]
    note = _by_role(plan, "note_body")[0]
    _seed_target_for_slot(_project, plan, record, note, "Aktualna treść noty.")

    slot = linked_notes_slot(
        plan,
        _window(plan, main),
        source_plan_id=record.id,
        source_plan=record,
    )

    assert slot is not None
    context = json.loads(slot.content)["relationships"][0]["related_source"][0]
    assert context["target_text"] == "Aktualna treść noty."
    assert slot.dependency["source_plan_id"] == record.id
    assert slot.dependency["active_targets"][0]["block_id"] == note.id
    assert db.session.get(
        TranslationTargetBlockActive,
        {"source_plan_id": record.id, "block_id": note.id},
    ) is not None


def test_relationship_and_outside_context_bounds_are_explicit() -> None:
    links = "".join(
        f'<a role="doc-noteref" href="#n{index}">*</a>' for index in range(13)
    )
    notes = "".join(
        f'<aside id="n{index}" role="doc-footnote"><p>Note {index}.</p></aside>'
        for index in range(13)
    )
    plan = _plan(f"<html><body><p>Main {links}</p>{notes}</body></html>")
    main = _by_role(plan, "paragraph")[0]
    slot = linked_notes_slot(plan, _window(plan, main), source_plan_id="plan-1")

    assert slot is not None
    payload = json.loads(slot.content)
    assert len(payload["relationships"]) == 12
    assert sum(len(row["related_source"]) for row in payload["relationships"]) <= 12
    assert payload["truncated"] is True
    assert len(slot.content.encode()) <= 8_000


def test_stateless_planning_does_not_need_a_database_context() -> None:
    plan = _plan(
        '<html><body><p>Main <a role="doc-noteref" href="#n">1</a></p>'
        '<aside id="n" role="doc-footnote"><p>Note body.</p></aside>'
        "</body></html>"
    )
    main = _by_role(plan, "paragraph")[0]
    slot = linked_notes_slot(plan, _window(plan, main), source_plan_id="planning-1")

    assert slot is not None
    assert slot.dependency == {"source_plan_id": "planning-1", "active_targets": []}


def test_random_nonadjacent_window_is_rejected() -> None:
    plan = _plan(
        "<html><body><p>First.</p><p>Middle.</p><p>Last.</p></body></html>"
    )
    with pytest.raises(ValueError, match="linked-notes-window-adjacency"):
        linked_notes_slot(
            plan,
            TranslationWindow(
                1,
                (plan.blocks[0].id, plan.blocks[2].id),
                tuple(structural_fingerprint(plan, block) for block in plan.blocks[::2]),
            ),
            source_plan_id="plan-1",
        )


def test_supplied_relations_are_used_without_replanning() -> None:
    plan = _plan(
        '<html><body><p>Main <a role="doc-noteref" href="#n">1</a></p>'
        '<aside id="n" role="doc-footnote"><p>Note body.</p></aside>'
        "</body></html>"
    )
    main = _by_role(plan, "paragraph")[0]
    relations = build_note_relations(plan)
    slot = linked_notes_slot(
        plan,
        _window(plan, main),
        source_plan_id="plan-1",
        relations=relations,
    )
    assert slot is not None
