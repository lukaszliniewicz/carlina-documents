"""Acceptance tests for bounded v2 Translator segment context."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from content_storage_support import bind_content_policy
from flask import Flask

from iarf_assistant.addons.translator.block_translation import (
    build_window_material,
    continuity_slot,
    derive_window_profile,
    plan_ordered_window_materials,
)
from iarf_assistant.addons.translator.models import (
    TranslationTargetAlignmentActive,
)
from iarf_assistant.addons.translator.segment_context import (
    context_before_block,
    segment_continuity_slot,
)
from iarf_assistant.addons.translator.sentence_precedent import (
    SENTENCE_PRECEDENT_CONTRACT_VERSION,
    sentence_precedent_slot,
    sentence_precedent_v2_slot,
)
from iarf_assistant.addons.translator.window_contract import ContextSlot
from iarf_assistant.core.encryption import EncryptionService, InstallationKeyStore
from iarf_assistant.extensions import db
from tests.test_translator_block_translation import (
    _config,
    _profile,
    _seed_target_for_slot,
    _state,
    _window,
    _workflow_config,
)


@pytest.fixture
def app(tmp_path: Path):
    application = Flask(__name__, instance_path=str(tmp_path / "instance"))
    bind_content_policy(application)
    application.config.update(
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{tmp_path / 'segment-context.db'}",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SECRET_KEY="segment-context-test",
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


def _byte_counter(**kwargs) -> int:
    messages = kwargs.get("messages")
    if isinstance(messages, list):
        return sum(
            len(str(message.get("content", "")).encode("utf-8"))
            for message in messages
            if isinstance(message, dict)
        )
    text = kwargs.get("text")
    return len(str(text or "").encode("utf-8"))


def test_context_returns_only_the_last_three_complete_groups(app):
    source_sentences = [
        f"Source sentence {number} records a complete thought for alignment."
        for number in range(1, 6)
    ]
    target_sentences = [
        f"Zdanie zrodlowe {number} ma kompletne tlumaczenie."
        for number in range(1, 6)
    ]
    _project, plan, record, _operation = _state(
        f"<html><body><p>{' '.join(source_sentences)}</p>"
        "<p>The next source paragraph starts after this one.</p></body></html>"
    )
    predecessor, current = plan.blocks
    _seed_target_for_slot(
        _project,
        plan,
        record,
        predecessor,
        " ".join(target_sentences),
    )

    result = context_before_block(
        record,
        plan=plan,
        before_block_id=current.id,
    )
    groups = result["groups"]

    assert result["available"] is True
    assert result["truncated"] is True
    assert result["omitted_group_count"] == 2
    assert isinstance(groups, list) and len(groups) == 3
    assert [group["source_sentence_orders"] for group in groups] == [[3], [4], [5]]
    assert [group["target_span_orders"] for group in groups] == [[3], [4], [5]]
    assert all(
        len(group["source_sentences"]) == len(group["target_spans"]) == 1
        for group in groups
    )
    assert len(json.dumps(result, ensure_ascii=False).encode("utf-8")) <= 6_000

    slot = segment_continuity_slot(
        record,
        plan=plan,
        window=_window(plan, start=1, end=2),
    )
    assert slot is not None
    assert slot.required is False
    assert set(slot.dependency) == {
        "predecessor_block_id",
        "revision_id",
        "revision_record_hash",
        "active_pointer_version",
    }


def test_context_skips_navigation_roles_and_keeps_merged_groups_whole(app):
    source = (
        "The first source sentence explains the argument in enough detail. "
        "The second source sentence continues the same paragraph with evidence. "
        "The third source sentence extends the reasoning with another example. "
        "The fourth source sentence closes this paragraph with a conclusion."
    )
    html = (
        "<html><head><title>A document title that must not be selected</title></head>"
        "<body><nav role='doc-toc'><p class='toc-entry-title'>1</p></nav>"
        "<p>1</p><p>"
        + source
        + "</p><h2>A later heading is navigation rather than prose.</h2>"
        "<img alt='An accessibility description that is not translation prose.'>"
        "<p>The current paragraph asks for context before translation.</p>"
        "</body></html>"
    )
    project, plan, record, _operation = _state(html)
    prose = next(block for block in plan.blocks if block.projection == source)
    current = next(
        block
        for block in plan.blocks
        if block.projection.startswith("The current paragraph")
    )
    _seed_target_for_slot(
        project,
        plan,
        record,
        prose,
        "Pierwsze zdanie tlumaczenia. Drugie zdanie tlumaczenia.",
    )

    result = context_before_block(
        record,
        plan=plan,
        before_block_id=current.id,
    )

    assert result["available"] is True
    assert result["predecessor"]["block_id"] == prose.id
    assert result["predecessor"]["source_role"] == "paragraph"
    groups = result["groups"]
    assert isinstance(groups, list) and len(groups) == 2
    assert [group["source_sentence_orders"] for group in groups] == [[1, 2], [3, 4]]
    assert [group["target_span_orders"] for group in groups] == [[1], [2]]
    assert all(len(group["source_sentences"]) == 2 for group in groups)
    assert all(len(group["target_spans"]) == 1 for group in groups)


def test_context_reports_an_oversized_last_group_without_truncating_it(app):
    source = "A meaningful source sentence for context."
    target = "A target span that cannot fit the context budget " + ("verylong " * 900)
    _project, plan, record, _operation = _state(
        f"<html><body><p>{source}</p><p>The next paragraph requests a context tail.</p></body></html>"
    )
    predecessor, current = plan.blocks
    _seed_target_for_slot(_project, plan, record, predecessor, target)

    result = context_before_block(
        record,
        plan=plan,
        before_block_id=current.id,
    )

    assert result["available"] is False
    assert result["reason"] == "last_alignment_group_exceeds_budget"
    assert result["truncated"] is True
    assert result["groups"] == []
    assert result["before_block_id"] == current.id
    assert len(json.dumps(result, ensure_ascii=False).encode("utf-8")) <= 6_000


def test_context_rejects_a_stale_active_target_alignment_pair(app):
    project, plan, record, _operation = _state(
        "<html><body><p>A translated predecessor has enough words here.</p>"
        "<p>The current paragraph asks for prior translation context.</p></body></html>"
    )
    predecessor, current = plan.blocks
    _seed_target_for_slot(
        project,
        plan,
        record,
        predecessor,
        "Przetlumaczony poprzednik zachowuje znaczenie.",
    )
    pointer = db.session.get(
        TranslationTargetAlignmentActive,
        {"source_plan_id": record.id, "block_id": predecessor.id},
    )
    assert pointer is not None
    pointer.version += 1
    db.session.commit()

    with pytest.raises(ValueError):
        context_before_block(
            record,
            plan=plan,
            before_block_id=current.id,
        )


def test_v2_sentence_precedent_returns_complete_split_groups_and_deduplicates(app):
    repeated = "The rare phrase repeats with enough detail for exact matching."
    second = "A second source sentence stays in this same paragraph for alignment."
    html = (
        "<html><body><p>"
        + repeated
        + " "
        + second
        + "</p><p>"
        + repeated
        + "</p><p>"
        + repeated.replace("matching", "matching!")
        + "</p></body></html>"
    )
    project, plan, record, _operation = _state(html)
    candidate = plan.blocks[0]
    _seed_target_for_slot(
        project,
        plan,
        record,
        candidate,
        "Pierwsze tlumaczenie ma dalszy ciag. Drugie zdanie dopowiada tresc. "
        "Trzecia czesc zachowuje kontekst. Czwarty segment zamyka sens.",
    )

    slot = sentence_precedent_v2_slot(
        record,
        plan=plan,
        window=_window(plan, start=1, end=3),
    )

    assert slot is not None
    assert slot.required is False
    payload = json.loads(slot.content)
    assert payload["contract_version"] == "translation-sentence-precedent-v2"
    assert len(slot.content.encode("utf-8")) <= 8_000
    assert len(payload["matches"]) == 1
    assert len(payload["queries"]) == 2
    group = payload["matches"][0]
    assert group["source_sentence_orders"] == [1]
    assert group["target_span_orders"] == [1, 2]
    assert len(group["source_sentences"]) == 1
    assert len(group["target_spans"]) == 2
    assert "revision_record_hash" in group
    assert payload["queries"][0]["matches"][0]["match_kind"] == "exact"
    fuzzy = payload["queries"][1]["matches"][0]
    assert fuzzy["match_kind"] == "lexical_fuzzy"
    assert fuzzy["similarity"] == 1.0
    assert fuzzy["matched_source_text"].strip() == repeated
    assert fuzzy["matched_source_order"] == 1
    assert payload["queries"][0]["matches"][0]["group_id"] == group["id"]
    assert payload["queries"][1]["matches"][0]["group_id"] == group["id"]
    assert len(payload["queries"]) <= 32
    assert all(len(query["matches"]) <= 2 for query in payload["queries"])
    assert sum(len(query["matches"]) for query in payload["queries"]) <= 6
    assert slot.dependency["source_plan_id"] == record.id
    assert len(slot.dependency["active_targets"]) == 1


def test_v2_sentence_precedent_caps_total_references_and_deduplicates_groups(app):
    repeated = "A distinctive sentence makes this repeated passage easy to match."
    html = "<html><body>" + "".join(
        f"<p>{repeated}</p>" for _ in range(10)
    ) + "</body></html>"
    project, plan, record, _operation = _state(html)
    for block in plan.blocks[:7]:
        _seed_target_for_slot(
            project,
            plan,
            record,
            block,
            "Kazde tlumaczenie zachowuje ten sam kompletny sens.",
        )

    slot = sentence_precedent_v2_slot(
        record,
        plan=plan,
        window=_window(plan, start=7),
    )

    assert slot is not None
    payload = json.loads(slot.content)
    assert payload["truncated"] is True
    assert len(payload["matches"]) == 2
    assert len(payload["queries"]) == 3
    assert sum(len(query["matches"]) for query in payload["queries"]) == 6
    assert all(len(query["matches"]) <= 2 for query in payload["queries"])
    assert len(
        {
            (
                group["block_id"],
                group["revision_id"],
                group["alignment_revision_id"],
                group["id"],
            )
            for group in payload["matches"]
        }
    ) == len(payload["matches"])


def test_v2_retained_context_omits_text_and_frees_source_budget(app):
    previous_source = "The previous source carries a complete historical sentence. " * 28
    previous_target = "Wczesniejsze tlumaczenie zachowuje tresc. " * 28
    current_one = "The first current source paragraph contains enough useful text. " * 2
    current_two = "The second current source paragraph continues the same argument. " * 2
    _project, plan, record, _operation = _state(
        "<html><body><p>"
        + previous_source
        + "</p><p>"
        + current_one
        + "</p><p>"
        + current_two
        + "</p></body></html>"
    )
    first, second, third = plan.blocks
    _seed_target_for_slot(_project, plan, record, first, previous_target)
    profile = _profile()
    base_config = _config(plan, record, profile)
    retained_config = {
        **base_config,
        "source_language": "en",
        "translation_context": {
            "contract_version": "translation-context-v2",
            "continuity_mode": "retained",
        },
    }
    high_profile = derive_window_profile(
        profile,
        maximum_blocks=2,
        target_input_tokens=15_000,
    )
    first_window = _window(plan, start=1, end=2)
    two_block_window = _window(plan, start=1, end=3)
    workflow = _workflow_config()
    legacy_one = build_window_material(
        plan=plan,
        source_plan_id=record.id,
        window=first_window,
        frozen_config=base_config,
        workflow_config=workflow,
        execution_profile=profile,
        window_profile=high_profile,
        source_plan_record=record,
        token_counter=_byte_counter,
    )
    retained_one = build_window_material(
        plan=plan,
        source_plan_id=record.id,
        window=first_window,
        frozen_config=retained_config,
        workflow_config=workflow,
        execution_profile=profile,
        window_profile=high_profile,
        source_plan_record=record,
        token_counter=_byte_counter,
    )
    legacy_two = build_window_material(
        plan=plan,
        source_plan_id=record.id,
        window=two_block_window,
        frozen_config=base_config,
        workflow_config=workflow,
        execution_profile=profile,
        window_profile=high_profile,
        source_plan_record=record,
        token_counter=_byte_counter,
    )
    retained_two = build_window_material(
        plan=plan,
        source_plan_id=record.id,
        window=two_block_window,
        frozen_config=retained_config,
        workflow_config=workflow,
        execution_profile=profile,
        window_profile=high_profile,
        source_plan_record=record,
        token_counter=_byte_counter,
    )
    legacy_measurement = legacy_one.admission["measurement"]["total_input_tokens"]
    retained_measurement = retained_one.admission["measurement"]["total_input_tokens"]
    two_block_legacy_measurement = legacy_two.admission["measurement"]["total_input_tokens"]
    two_block_retained_measurement = retained_two.admission["measurement"]["total_input_tokens"]
    lower = max(legacy_measurement, two_block_retained_measurement)
    assert lower < two_block_legacy_measurement
    budget = (lower + two_block_legacy_measurement) // 2
    constrained_profile = derive_window_profile(
        profile,
        maximum_blocks=2,
        target_input_tokens=budget,
    )
    legacy_planned = plan_ordered_window_materials(
        plan=plan,
        source_plan_id=record.id,
        frozen_config=base_config,
        workflow_config=workflow,
        execution_profile=profile,
        window_profile=constrained_profile,
        source_plan_record=record,
        token_counter=_byte_counter,
        excluded_block_ids=(first.id,),
        max_windows=1,
    )
    retained_planned = plan_ordered_window_materials(
        plan=plan,
        source_plan_id=record.id,
        frozen_config=retained_config,
        workflow_config=workflow,
        execution_profile=profile,
        window_profile=constrained_profile,
        source_plan_record=record,
        token_counter=_byte_counter,
        excluded_block_ids=(first.id,),
        max_windows=1,
    )

    assert legacy_measurement > retained_measurement + 500
    assert legacy_planned.windows[0].block_ids == (second.id,)
    assert retained_planned.windows[0].block_ids == (second.id, third.id)
    retained_slots = retained_planned.materials[0].packet.slots
    assert not any(slot.slot_type == "continuity" for slot in retained_slots)
    policy = next(slot for slot in retained_slots if slot.slot_type == "project_policy")
    policy_data = json.loads(policy.content)
    assert policy_data["context_mode"] == "retained"
    assert policy_data["continuity_lookup"] == {
        "source_plan_id": record.id,
        "before_block_id": second.id,
    }
    assert previous_source not in policy.content
    assert previous_target not in policy.content


def test_v2_contextual_memory_is_optional_and_reduced_before_writable_source(app):
    _project, plan, record, _operation = _state(
        "<html><body><p>A short source block remains writable in full.</p></body></html>"
    )
    profile = _profile()
    config = {
        **_config(plan, record, profile),
        "source_language": "en",
        "translation_context": {
            "contract_version": "translation-context-v2",
            "continuity_mode": "self_contained",
        },
    }
    memory_content = "A prior phrasing is only a suggestion. " * 100
    memory = ContextSlot(
        "translation_memory",
        memory_content,
        {"source": "synthetic-test"},
        "translation-memory:test",
        "1",
        hashlib.sha256(memory_content.encode("utf-8")).hexdigest(),
        {},
    )
    high_profile = derive_window_profile(
        profile,
        maximum_blocks=1,
        target_input_tokens=15_000,
    )
    shared = {
        "plan": plan,
        "source_plan_id": record.id,
        "window": _window(plan),
        "frozen_config": config,
        "workflow_config": _workflow_config(),
        "execution_profile": profile,
        "source_plan_record": record,
        "token_counter": _byte_counter,
    }
    base = build_window_material(
        **shared,
        window_profile=high_profile,
    )
    retained_memory = build_window_material(
        **shared,
        window_profile=high_profile,
        contextual_memory=memory,
    )
    memory_slot = next(
        slot
        for slot in retained_memory.packet.slots
        if slot.slot_type == "translation_memory"
    )
    assert memory_slot.required is False

    base_measurement = base.admission["measurement"]["total_input_tokens"]
    constrained = build_window_material(
        **shared,
        window_profile=derive_window_profile(
            profile,
            maximum_blocks=1,
            target_input_tokens=base_measurement + 1,
        ),
        contextual_memory=memory,
    )
    assert all(
        slot.slot_type != "translation_memory"
        for slot in constrained.packet.slots
    )
    assert any(slot.slot_type == "writable_source" for slot in constrained.packet.slots)


def test_v1_precedent_and_continuity_content_remain_unchanged(app):
    source = "The same source sentence remains an exact precedent."
    target = "To samo zdanie zrodlowe ma utrwalony przeklad."
    project, plan, record, _operation = _state(
        f"<html><body><p>{source}</p><p>Intervening prose adds context here.</p>"
        f"<p>{source}</p></body></html>"
    )
    first, _middle, repeated = plan.blocks
    _seed_target_for_slot(project, plan, record, first, target)
    window = _window(plan, start=2, end=3)

    continuity = continuity_slot(
        record,
        plan=plan,
        window=_window(plan, start=1, end=2),
    )
    precedent = sentence_precedent_slot(record, plan=plan, window=window)

    assert continuity is not None
    assert json.loads(continuity.content)["contract_version"] == "translation-block-continuity-v2"
    assert precedent is not None
    assert json.loads(precedent.content)["contract_version"] == SENTENCE_PRECEDENT_CONTRACT_VERSION
    assert json.loads(precedent.content)["queries"][0]["source_text"] == repeated.projection
    match = json.loads(precedent.content)["queries"][0]["matches"][0]
    assert match["match_kind"] == "exact"
    assert match["target_text"] == target
    assert continuity.content == (
        '{"contract_version":"translation-block-continuity-v2",'
        '"source_role":"paragraph",'
        '"source_text":"The same source sentence remains an exact precedent.",'
        '"target":{"content":"To samo zdanie zrodlowe ma utrwalony przeklad.",'
        '"fields":[]}}'
    )
    assert precedent.content == (
        '{"contract_version":"translation-sentence-precedent-v1",'
        '"guidance":"Advisory current wording only: validate context and negation; '
        'preserve passages in other source languages; never infer note identity from '
        'similarity or a marker.","queries":[{"matches":[{"alignment_review_state":'
        '"needs_review","match_kind":"exact","origin":"human","similarity":1.0,'
        '"source_order":1,"source_role":"paragraph","source_sentence_order":1,'
        '"source_text":"The same source sentence remains an exact precedent.",'
        '"target_text":"To samo zdanie zrodlowe ma utrwalony przeklad."}],"n":1,'
        '"source_role":"paragraph","source_sentence_order":1,'
        '"source_text":"The same source sentence remains an exact precedent."}]}'
    )
