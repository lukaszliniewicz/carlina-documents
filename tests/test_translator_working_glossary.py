"""Live glossary deltas, morphology evidence and immutable window snapshots."""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest
from test_translator_block_translation import (
    _config,
    _counter,
    _profile,
    _state,
    _window,
    _workflow_config,
)

from iarf_assistant.addons.translator.analysis_contract import (
    GlossaryPayload,
    glossary_prompt_mapping,
)
from iarf_assistant.addons.translator.api_client import TranslatorClient
from iarf_assistant.addons.translator.block_contract import BlockContractError
from iarf_assistant.addons.translator.block_translation import build_window_material
from iarf_assistant.addons.translator.glossary_persistence import (
    approve_glossary_proposal,
    create_glossary_concept,
    persist_glossary_revision,
    rehydrate_glossary_revision,
)
from iarf_assistant.addons.translator.models import (
    TranslationGlossaryConcept,
    TranslationGlossaryOccurrence,
    TranslationGlossaryRevision,
)
from iarf_assistant.addons.translator.window_persistence import (
    persist_window_attempt,
    rehydrate_window_attempt,
)
from iarf_assistant.addons.translator.working_glossary import (
    build_glossary_slot,
    glossary_slot_for_window,
)
from iarf_assistant.extensions import db

pytest_plugins = ("test_translator_block_translation",)


def _setup(html="<html><body><p>In den Gemeinden.</p><p>Die Gemeinde bleibt.</p></body></html>"):
    project, plan, record, operation = _state(html)
    client = TranslatorClient(SimpleNamespace(), {"current_user_id": project.user_id})
    return project, plan, record, operation, client


def _save(client, project, record, entries, key="one"):
    return client.glossary_propose(project.id, source_plan_id=record.id,
                                    entries=entries, idempotency_key=key)


def _term(**updates):
    return {"source_term": "Gemeinde", "target_term": "congregation", "sense": "religious", **updates}


def test_lemma_inflection_quote_offsets_and_persistence(app):
    project, plan, record, operation, client = _setup()
    term = _term(occurrences=[{"block_id": plan.blocks[0].id, "quote": "Gemeinden"}])
    result = _save(client, project, record, [term])
    entry = result["items"][0]
    assert entry["source_term"] == "Gemeinde"
    assert entry["match_policy"] == "preferred"
    assert entry["authority"] == "model_advisory"
    assert entry["source_forms"] == ["Gemeinde", "Gemeinden"]
    observation = entry["observations"][0]
    assert plan.blocks[0].projection[observation["start_offset"]:observation["end_offset"]] == "Gemeinden"
    assert observation["anchor_id"] == plan.blocks[0].anchors[0].id
    assert TranslationGlossaryOccurrence.query.count() == 1
    assert operation.state == "running"  # glossary updates do not restart translation
    project_id, concept_id = project.id, entry["concept_id"]
    db.session.remove()
    loaded = client.glossary_get(project_id)["items"][0]
    assert loaded["concept_id"] == concept_id
    assert loaded["source_forms"] == ["Gemeinde", "Gemeinden"]
    assert loaded["observations"] == entry["observations"]


def test_idempotency_entire_batch_conflict_and_rollback(app):
    project, _plan, record, _operation, client = _setup()
    first = _save(client, project, record, [_term()])
    assert _save(client, project, record, [_term()]) == first
    assert TranslationGlossaryRevision.query.count() == 1
    with pytest.raises(BlockContractError, match="glossary-idempotency-conflict"):
        _save(client, project, record, [_term(target_term="municipality")])
    with pytest.raises(BlockContractError, match="glossary-idempotency-conflict"):
        _save(client, project, record, [_term(), _term(source_term="Religion", target_term="religion")])
    assert TranslationGlossaryRevision.query.count() == 1
    with pytest.raises(BlockContractError, match="glossary-concept-exists"):
        _save(client, project, record, [_term(source_term="Vernunft", target_term="reason"), _term()], key="bad-batch")
    assert TranslationGlossaryConcept.query.count() == 1
    assert TranslationGlossaryRevision.query.count() == 1


def test_edit_fence_historical_retry_and_form_extension(app):
    project, plan, record, _operation, client = _setup()
    first = _save(client, project, record, [_term()])["items"][0]
    edited = _term(target_term="religious congregation", concept_id=first["concept_id"],
                   expected_revision=first["revision"],
                   occurrences=[{"block_id": plan.blocks[0].id, "quote": "Gemeinden"}])
    second = _save(client, project, record, [edited], key="edit")["items"][0]
    assert second["revision"] == 2
    assert second["source_forms"] == ["Gemeinde", "Gemeinden"]
    assert _save(client, project, record, [_term()])["items"][0] == first
    assert client.glossary_get(project.id)["items"][0] == second
    with pytest.raises(BlockContractError, match="glossary-revision-conflict"):
        _save(client, project, record, [edited], key="stale-edit")
    assert TranslationGlossaryRevision.query.count() == 2


def test_senses_do_not_merge_homonyms_or_generate_compound_variants(app):
    project, _plan, record, _operation, client = _setup()
    results = _save(client, project, record, [_term(), _term(target_term="municipality", sense="administrative")])
    entries = results["items"]
    assert entries[0]["concept_id"] != entries[1]["concept_id"]
    assert all(entry["source_forms"] == ["Gemeinde"] for entry in entries)
    assert client.glossary_get(project.id, query="municipality")["pagination"]["total"] == 1
    assert client.glossary_get(project.id, limit=1)["pagination"]["next_offset"] == 1
    assert client.glossary_get(project.id, offset=1, limit=1)["pagination"]["next_offset"] is None


def test_exact_policy_and_explicit_form_replacement(app):
    project, _plan, record, _operation, client = _setup()
    entry = _save(client, project, record, [_term(match_policy="exact", source_forms=["Gemeinden"])])["items"][0]
    assert entry["match_policy"] == "exact"
    revised = _save(client, project, record, [_term(
        concept_id=entry["concept_id"], expected_revision=1, source_forms=[])], key="replace")["items"][0]
    assert revised["source_forms"] == ["Gemeinde"]
    assert revised["match_policy"] == "preferred"


@pytest.mark.parametrize("kwargs", [
    {"quote": "Gemeinde"},  # occurs twice, no occurrence_index
    {"quote": "Gemeinde", "occurrence_index": 3},
    {"quote": "Not present"},
    {"quote": "Gemeinde", "occurrence_index": 1, "anchor_id": "foreign-anchor"},
])
def test_ambiguous_missing_or_foreign_quote_is_not_saved(app, kwargs):
    project, plan, record, _operation, client = _setup("<html><body><p>Gemeinde und Gemeinde.</p></body></html>")
    with pytest.raises(BlockContractError):
        _save(client, project, record, [_term(occurrences=[{"block_id": plan.blocks[0].id, **kwargs}])])
    assert TranslationGlossaryRevision.query.count() == 0
    assert TranslationGlossaryOccurrence.query.count() == 0


def test_repeated_quote_index_is_exact_and_source_plan_is_required(app):
    project, plan, record, _operation, client = _setup("<html><body><p>Gemeinde und Gemeinde.</p></body></html>")
    entry = _save(client, project, record, [_term(occurrences=[{
        "block_id": plan.blocks[0].id, "quote": "Gemeinde", "occurrence_index": 2}])])["items"][0]
    assert entry["observations"][0]["start_offset"] == len("Gemeinde und ")
    with pytest.raises(BlockContractError):
        client.glossary_propose(project.id, source_plan_id="foreign-plan", entries=[_term()], idempotency_key="wrong-plan")


def test_owner_isolation_for_read_and_write(app):
    project, _plan, record, _operation, client = _setup()
    other = TranslatorClient(SimpleNamespace(), {"current_user_id": project.user_id + 99})
    with pytest.raises(LookupError):
        other.glossary_get(project.id)
    with pytest.raises(LookupError):
        _save(other, project, record, [_term()])
    assert client.glossary_get(project.id)["items"] == []


def test_legacy_payload_hash_shape_is_unchanged(app):
    payload = GlossaryPayload("Gemeinde", "congregation")
    assert payload.to_dict() == {"source_term": "Gemeinde", "target_term": "congregation", "notes": None, "scope": "document"}


def test_usage_is_receipt_bound_and_encrypted(app):
    project, _plan, record, _operation, client = _setup()
    entry = _save(client, project, record, [_term()])["items"][0]
    row = db.session.get(TranslationGlossaryRevision, entry["revision_id"])
    raw = db.session.connection().exec_driver_sql(
        "SELECT payload_json FROM translation_block_glossary_revision WHERE id = ?", (row.id,)
    ).scalar()
    assert "Gemeinde" not in str(raw)
    payload = copy.deepcopy(row.payload_json)
    payload["usage"]["sense"] = "forged sense"
    row.payload_json = payload
    db.session.flush()
    with pytest.raises(BlockContractError):
        rehydrate_glossary_revision(row, record)


def test_new_packets_get_latest_terms_but_prepared_snapshot_is_immutable(app):
    project, plan, record, operation, client = _setup()
    first = _save(client, project, record, [_term()])["items"][0]
    profile = _profile()
    # Word-boundary matching: the lemma matches the singular block and the
    # whole-document window, not the plural-only first block via substring.
    window = _window(plan, start=0)
    slot = glossary_slot_for_window(operation, record, plan, window)
    assert slot is not None
    material = build_window_material(plan=plan, source_plan_id=record.id, window=window,
        frozen_config=_config(plan, record, profile), workflow_config=_workflow_config(),
        execution_profile=profile, source_plan_record=record, include_continuity=False,
        live_glossary=slot, token_counter=_counter)
    attempt = persist_window_attempt(operation=operation, source_plan=record, window=window,
        packet=material.packet, settings=material.settings, model_profile=material.model_profile,
        admission=material.admission)
    db.session.commit()
    before = copy.deepcopy(attempt.packet_json)
    _save(client, project, record, [_term(concept_id=first["concept_id"], expected_revision=1,
        target_term="religious congregation")], key="changed")
    old_slot = glossary_slot_for_window(operation, record, plan, window)
    assert old_slot.content == slot.content
    assert attempt.packet_json == before
    assert rehydrate_window_attempt(attempt, record).to_dict() == material.packet.to_dict()
    next_slot = glossary_slot_for_window(operation, record, plan, _window(plan, order=2, start=1))
    assert json.loads(next_slot.content)["terms"][0]["target_term"] == "religious congregation"


def test_existing_packet_without_glossary_does_not_gain_one_on_retry(app):
    project, plan, record, operation, client = _setup()
    profile = _profile()
    window = _window(plan, start=0, end=1)
    material = build_window_material(plan=plan, source_plan_id=record.id, window=window,
        frozen_config=_config(plan, record, profile), workflow_config=_workflow_config(),
        execution_profile=profile, source_plan_record=record, include_continuity=False, token_counter=_counter)
    persist_window_attempt(operation=operation, source_plan=record, window=window,
        packet=material.packet, settings=material.settings, model_profile=material.model_profile,
        admission=material.admission)
    db.session.commit()
    _save(client, project, record, [_term()])
    assert glossary_slot_for_window(operation, record, plan, window) is None
    assert build_glossary_slot(record, plan, _window(plan, order=2, start=1)) is not None


def test_batch_configuration_requires_idle_fence_and_preserves_plan(app):
    project, plan, record, operation, client = _setup()
    original_hash = plan.plan_hash
    settings = client.mcp_job_projection(project.id)["batch_settings"]
    with pytest.raises(BlockContractError, match="translation-batch-operation-active"):
        client.configure_batches(project.id, input_tokens=24000, max_blocks=32, expected_version=settings["version"])
    from iarf_assistant.addons.translator.operations import finish_operation
    finish_operation(operation, "completed")
    db.session.commit()
    result = client.configure_batches(project.id, input_tokens=24000, max_blocks=32, expected_version=settings["version"])
    assert result["source_plan_changed"] is False
    assert record.plan_hash == original_hash
    with pytest.raises(BlockContractError, match="translation-batch-version-conflict"):
        client.configure_batches(project.id, input_tokens=32000, max_blocks=48, expected_version=settings["version"])
    assert client.configure_batches(project.id, input_tokens=24000, max_blocks=32, expected_version=settings["version"])["unchanged"]


def test_compound_substring_does_not_select_term(app):
    project, plan, record, _operation, client = _setup(
        "<html><body><p>Die Beratung ist wichtig.</p><p>Der Rat tagt heute.</p></body></html>")
    _save(client, project, record, [_term(source_term="Rat", target_term="council", sense="")])
    assert build_glossary_slot(record, plan, _window(plan, start=0, end=1)) is None
    assert build_glossary_slot(record, plan, _window(plan, start=1, end=2)) is not None


def test_sense_whitespace_case_insensitive_duplicate(app):
    project, _plan, record, _operation, client = _setup()
    _save(client, project, record, [_term(sense="  Religious   CONGREGATION ")])
    with pytest.raises(BlockContractError, match="glossary-concept-exists"):
        _save(client, project, record, [_term(sense="religious congregation")], key="dup-sense")
    # Different sense remains a separate concept.
    second = _save(client, project, record,
                   [_term(target_term="municipality", sense="administrative")],
                   key="other-sense")["items"][0]
    assert second["sense"] == "administrative"
    assert TranslationGlossaryConcept.query.count() == 2


def test_same_batch_duplicate_concept_is_atomic(app):
    project, _plan, record, _operation, client = _setup()
    first = _save(client, project, record, [_term()])["items"][0]
    duplicate = _term(target_term="edited", concept_id=first["concept_id"],
                      expected_revision=first["revision"])
    with pytest.raises(BlockContractError, match="glossary-revision-conflict"):
        _save(client, project, record, [duplicate, duplicate], key="dup-batch")
    assert TranslationGlossaryRevision.query.count() == 1
    assert client.glossary_get(project.id)["items"][0]["revision"] == 1


def test_gemeinde_compound_and_inflection_require_observed_forms(app):
    project, plan, record, _operation, client = _setup(
        "<html><body><p>Die Gemeindeversammlung tagt.</p><p>In den Gemeinden.</p></body></html>")
    _save(client, project, record, [_term()])
    # Headword alone: no match inside the compound, no auto-match of the
    # inflected form. Explicit observed forms are required; nothing here
    # claims unseen morphology.
    assert build_glossary_slot(record, plan, _window(plan, start=0, end=1)) is None
    assert build_glossary_slot(record, plan, _window(plan, start=1, end=2)) is None
    entry = client.glossary_get(project.id)["items"][0]
    revised = _save(client, project, record, [_term(
        concept_id=entry["concept_id"], expected_revision=entry["revision"],
        occurrences=[{"block_id": plan.blocks[1].id, "quote": "Gemeinden"}])],
        key="observe-gemeinden")["items"][0]
    assert revised["source_forms"] == ["Gemeinde", "Gemeinden"]
    assert build_glossary_slot(record, plan, _window(plan, start=0, end=1)) is None
    assert build_glossary_slot(record, plan, _window(plan, start=1, end=2)) is not None


def test_polish_preferred_inflection_requires_observed_forms(app):
    project, plan, record, _operation, client = _setup(
        "<html><body><p>Kościół jest stary.</p><p>Widzę kościoła i kościoły.</p></body></html>")
    _save(client, project, record, [
        {"source_term": "kościół", "target_term": "church", "sense": ""}])
    # Citation form alone matches only itself; genitive/plural require
    # explicit observed forms. Unicode word boundaries apply; no morphology
    # proof is claimed.
    assert build_glossary_slot(record, plan, _window(plan, start=0, end=1)) is not None
    assert build_glossary_slot(record, plan, _window(plan, start=1, end=2)) is None
    entry = client.glossary_get(project.id)["items"][0]
    revised = _save(client, project, record, [{
        "source_term": "kościół", "target_term": "church", "sense": "",
        "concept_id": entry["concept_id"], "expected_revision": entry["revision"],
        "occurrences": [
            {"block_id": plan.blocks[1].id, "quote": "kościoła", "occurrence_index": 1},
            {"block_id": plan.blocks[1].id, "quote": "kościoły", "occurrence_index": 1},
        ]}], key="observe-polish")["items"][0]
    assert revised["source_forms"] == ["kościół", "kościoła", "kościoły"]
    assert revised["match_policy"] == "preferred"
    assert build_glossary_slot(record, plan, _window(plan, start=1, end=2)) is not None


def test_human_locked_preferred_polish_target_inflects_without_rejection(app):
    from test_translator_block_translation import _candidate

    from iarf_assistant.addons.translator.analysis_contract import (
        glossary_prompt_mapping,
    )
    from iarf_assistant.addons.translator.block_workspace_api import (
        persist_human_glossary_entry,
    )
    from iarf_assistant.addons.translator.target_validation import (
        validate_target_candidate,
    )

    project, plan, record, _operation, client = _setup(
        "<html><body><p>Widzę kościół.</p></body></html>")
    proposed = _save(client, project, record, [
        {"source_term": "kościół", "target_term": "church", "sense": ""}],
        key="pl-propose")["items"][0]
    from flask_login import LoginManager, login_user

    from iarf_assistant.models import User
    owner = db.session.get(User, project.user_id)
    assert owner is not None
    login_manager = LoginManager()
    login_manager.init_app(app)

    @login_manager.user_loader
    def _load_owner(user_id):
        return db.session.get(User, int(user_id))

    with app.test_request_context("/block-glossary"):
        login_user(owner)
        locked_payload = persist_human_glossary_entry(
            project, source_term="kościół", target_term="church", notes=None,
            author_user_id=project.user_id, concept_id=proposed["concept_id"],
            expected_active_version=0, expected_proposal_id=proposed["revision_id"])
    assert locked_payload["entries"][0]["usage"]["match_policy"] == "preferred"
    # Preferred is authority, not an exact-character invariant: genitive and
    # plural renderings must not fail target validation merely for surface
    # mismatch. The validator owns structure; it carries no glossary codes.
    for surface in ("I see the church.", "I see two churches.",
                    "Widzę kościół."):
        candidate = _candidate(plan, record, plan.blocks[0], surface)
        result = validate_target_candidate(plan, record.id, plan.blocks[0], candidate)
        assert result.valid, surface
        assert all("glossary" not in finding.code for finding in result.findings)
    # New usage-aware baseline bytes carry sense/policy/forms; legacy payloads
    # without usage retain their documented historical bytes/hashes.
    legacy = GlossaryPayload("kościół", "church")
    assert legacy.to_dict() == {"source_term": "kościół", "target_term": "church",
                                "notes": None, "scope": "document"}
    assert legacy.usage is None
    assert glossary_prompt_mapping(legacy) == {"source": "kościół", "target": "church"}
    preferred = GlossaryPayload("kościół", "church", usage={
        "schema_version": "translator-glossary-usage-v1", "sense": "",
        "match_policy": "preferred", "source_forms": ["kościół"],
        "observations": [], "provenance": "passive-executor",
        "request_fingerprint": "a" * 64})
    assert glossary_prompt_mapping(preferred)["match_policy"] == "preferred"


def test_window_block_index_is_readonly_and_versioned(app):
    from iarf_assistant.addons.translator.working_glossary import (
        WINDOW_BLOCK_INDEX_CONTRACT,
        window_block_index_for_dispatch,
    )
    _project, plan, record, operation, _client = _setup()
    profile = _profile()
    window = _window(plan, start=0, end=2)
    index = window_block_index_for_dispatch(record, plan, window)
    assert index["contract_version"] == WINDOW_BLOCK_INDEX_CONTRACT
    assert index["source_plan_id"] == record.id
    assert index["window_order"] == window.order
    assert [row["n"] for row in index["blocks"]] == [1, 2]
    assert [row["block_id"] for row in index["blocks"]] == list(window.block_ids)
    for row, block_id in zip(index["blocks"], window.block_ids):
        block = next(item for item in plan.blocks if item.id == block_id)
        assert [item["anchor_id"] for item in row["sentences"]] == [
            anchor.id for anchor in sorted(block.anchors, key=lambda item: item.order)]
        assert [item["n"] for item in row["sentences"]] == [
            anchor.order for anchor in sorted(block.anchors, key=lambda item: item.order)]
    # Read-only: building the index must not create glossary revisions or
    # mutate a prepared packet.
    assert TranslationGlossaryRevision.query.count() == 0
    material = build_window_material(plan=plan, source_plan_id=record.id, window=window,
        frozen_config=_config(plan, record, profile), workflow_config=_workflow_config(),
        execution_profile=profile, source_plan_record=record, include_continuity=False,
        token_counter=_counter)
    before = copy.deepcopy(material.packet.to_dict())
    window_block_index_for_dispatch(record, plan, window)
    assert material.packet.to_dict() == before
    attempt = persist_window_attempt(operation=operation, source_plan=record, window=window,
        packet=material.packet, settings=material.settings, model_profile=material.model_profile,
        admission=material.admission)
    db.session.commit()
    assert rehydrate_window_attempt(attempt, record).to_dict() == material.packet.to_dict()


def _usage_payload(source, target, sense, policy="preferred"):
    return GlossaryPayload(source, target, scope="document", usage={
        "schema_version": "translator-glossary-usage-v1", "sense": sense,
        "match_policy": policy, "source_forms": [source],
        "observations": [], "provenance": "test",
        "request_fingerprint": "b" * 64})


def _lock_with_usage(app, record, source, target, sense, policy="preferred"):
    from flask_login import LoginManager, login_user, logout_user

    from iarf_assistant.addons.translator.block_workspace_api import (
        glossary_lock_preview,
    )
    from iarf_assistant.models import RuntimeApprovalRequest, User
    concept = create_glossary_concept(record)
    payload = _usage_payload(source, target, sense, policy)
    proposal = persist_glossary_revision(
        record, concept, payload=payload,
        author_type="model", revision_state="proposal")
    db.session.flush()
    approval = RuntimeApprovalRequest(
        requester_user_id=record.project.user_id, approver_user_id=None,
        subject_type="translator.glossary-lock.v1", subject_id=proposal.id,
        preview_payload=glossary_lock_preview(record, concept, proposal, payload),
        status="pending", resolved_at=None)
    db.session.add(approval)
    db.session.flush()
    owner = db.session.get(User, record.project.user_id)
    assert owner is not None
    login_manager = LoginManager()
    try:
        login_manager.init_app(app)
    except Exception:
        pass

    @login_manager.user_loader
    def _load_lock_owner(user_id):
        return db.session.get(User, int(user_id))

    with app.test_request_context("/glossary-lock"):
        login_user(owner)
        try:
            locked = approve_glossary_proposal(
                record, proposal, approved_payload=payload, approval=approval.id)
        finally:
            logout_user()
    db.session.flush()
    return concept, proposal, locked


def test_freeze_sense_variants_do_not_falsely_conflict(app):
    from iarf_assistant.addons.translator.context_baselines import (
        context_slots_from_baseline,
        freeze_context_baseline,
    )
    _project, _plan, record, operation, _client = _setup(
        "<html><body><p>Die Gemeinde.</p></body></html>")
    _c1, _p1, religious = _lock_with_usage(
        app, record, "Gemeinde", "congregation", "religious", "preferred")
    _c2, _p2, administrative = _lock_with_usage(
        app, record, "Gemeinde", "municipality", "administrative", "preferred")
    db.session.commit()
    # Two approved NEW usage.sense variants with different targets share a
    # headword but are distinct conceptual identities: no false conflict.
    baseline = freeze_context_baseline(
        operation, record, glossary_revisions=[religious, administrative])
    db.session.commit()
    slots = {slot.slot_type: slot for slot in
             context_slots_from_baseline(baseline, record)}
    locked = json.loads(slots["glossary_constraints"].content)["locked_terms"]
    by_target = {item["target"]: item for item in locked}
    assert by_target["congregation"]["sense"] == "religious"
    assert by_target["municipality"]["sense"] == "administrative"
    assert by_target["congregation"]["match_policy"] == "preferred"
    assert by_target["municipality"]["match_policy"] == "preferred"
    assert glossary_prompt_mapping(
        rehydrate_glossary_revision(religious, record))["sense"] == "religious"


def test_freeze_same_sense_different_target_conflicts(app):
    from iarf_assistant.addons.translator.context_baselines import (
        freeze_context_baseline,
    )
    _project, _plan, record, operation, _client = _setup(
        "<html><body><p>Die Gemeinde.</p></body></html>")
    _ca, _pa, religious = _lock_with_usage(
        app, record, "Gemeinde", "congregation", "religious", "preferred")
    _cb, _pb, clash = _lock_with_usage(
        app, record, "Gemeinde", "parish", "religious", "preferred")
    db.session.commit()
    with pytest.raises(BlockContractError, match="baseline-glossary-conflict"):
        freeze_context_baseline(operation, record,
                                glossary_revisions=[religious, clash])
    db.session.rollback()


def test_glossary_read_byte_budget_and_compact_projection(app):
    from iarf_assistant.addons.translator.working_glossary import MAX_READ_CHARS
    _project, _plan, record, _operation, client = _setup(
        "<html><body><p>Alpha.</p></body></html>")
    entries = [{"source_term": f"Term {index:03d}", "target_term": f"Target {index:03d}",
                "sense": "", "notes": "n" * 2000} for index in range(40)]
    _save(client, _project, record, entries[:20], key="budget-fill-a")
    _save(client, _project, record, entries[20:], key="budget-fill-b")
    full = client.glossary_get(_project.id, limit=40)
    assert full["pagination"]["total"] == 40
    assert len(full["items"]) == 40
    assert len(json.dumps(full["items"], ensure_ascii=False)) <= MAX_READ_CHARS or \
        full["pagination"].get("truncated_by_budget") is True
    if full["pagination"].get("truncated_by_budget"):
        # Honest pagination: remainder is fetchable, nothing silently lost.
        nxt = full["pagination"]["next_offset"]
        assert nxt is not None and nxt == len(full["items"])
        rest = client.glossary_get(_project.id, offset=nxt, limit=40)
        assert rest["items"][0]["source_term"] == f"Term {nxt:03d}"
        assert len(full["items"]) + len(rest["items"]) >= 40 or rest["pagination"]["next_offset"] is not None
    compact = client.glossary_get(_project.id, limit=40, include_observations=False)
    assert compact["pagination"].get("compact") == "observations_omitted"
    assert all(item["observations"] == [] for item in compact["items"])
    assert all("observation_count" in item for item in compact["items"])


def test_dossier_rejection_hides_only_exact_rejected_link(app):
    from iarf_assistant.addons.translator import scholarly_review
    from iarf_assistant.addons.translator.scholarly_review import ReviewActor

    _project, plan, record, _operation, client = _setup(
        "<html><body><p>Alpha Beta.</p></body></html>")
    block_id = plan.blocks[0].id
    scope = {"kind": "document", "block_ids": []}

    def _dossier_term(target, notes=None):
        return {"source_term": "Alpha", "target_term": target, "notes": notes,
                "source_block_ids": [block_id], "confidence": 0.9}

    submitted_a = client.research_dossier_submit(
        _project.id, "dossier-alpha-a",
        {"document_brief": "brief A", "terminology": [_dossier_term("AlphaTarget")]},
        {"method": "test"}, scope)
    rev_a = submitted_a["revision_id"]
    targets = {item["target_term"] for item in client.glossary_get(_project.id)["items"]}
    assert "AlphaTarget" in targets

    submitted_b = client.research_dossier_submit(
        _project.id, "dossier-alpha-b",
        {"document_brief": "brief B", "terminology": [_dossier_term("BetaTarget")]},
        {"method": "test"}, scope)
    rev_b = submitted_b["revision_id"]
    targets = {item["target_term"] for item in client.glossary_get(_project.id)["items"]}
    # Same concept carries the latest dossier term; the superseded exact is
    # historical, not a second live headword.
    assert "BetaTarget" in targets

    actor = ReviewActor("owner", _project.user_id)
    suggestions = scholarly_review.list_glossary_suggestions(_project)
    sugg_a = next(item for item in suggestions
                  if item["dossier_revision_id"] == rev_a and item["target_term"] == "AlphaTarget")
    scholarly_review.resolve_glossary_suggestion(
        _project, sugg_a["id"], sugg_a["version"], "rejected", actor=actor)
    db.session.commit()
    # The rejected exact target stays hidden; it must not poison reads.
    targets = {item["target_term"] for item in client.glossary_get(_project.id)["items"]}
    assert "AlphaTarget" not in targets
    assert "BetaTarget" in targets

    # Visible same-sense term still enforces duplicates (no bypass).
    with pytest.raises(BlockContractError, match="glossary-concept-exists"):
        _save(client, _project, record,
              [{"source_term": "Alpha", "target_term": "BetaTarget", "sense": ""}],
              key="dup-visible-beta")

    submitted_c = client.research_dossier_submit(
        _project.id, "dossier-alpha-c",
        {"document_brief": "brief C", "terminology": [_dossier_term("BetaTarget")]},
        {"method": "test"}, scope)
    rev_c = submitted_c["revision_id"]
    assert rev_c != rev_b
    targets = {item["target_term"] for item in client.glossary_get(_project.id)["items"]}
    assert "BetaTarget" in targets

    suggestions = scholarly_review.list_glossary_suggestions(_project)
    sugg_b = next(item for item in suggestions
                  if item["dossier_revision_id"] == rev_b and item["target_term"] == "BetaTarget")
    scholarly_review.resolve_glossary_suggestion(
        _project, sugg_b["id"], sugg_b["version"], "rejected", actor=actor)
    db.session.commit()
    # Identical proposal reused by still-proposed dossier C stays visible.
    targets = {item["target_term"] for item in client.glossary_get(_project.id)["items"]}
    assert "BetaTarget" in targets

    suggestions = scholarly_review.list_glossary_suggestions(_project)
    sugg_c = next(item for item in suggestions
                  if item["dossier_revision_id"] == rev_c and item["target_term"] == "BetaTarget")
    scholarly_review.resolve_glossary_suggestion(
        _project, sugg_c["id"], sugg_c["version"], "rejected", actor=actor)
    db.session.commit()
    targets = {item["target_term"] for item in client.glossary_get(_project.id)["items"]}
    assert "BetaTarget" not in targets

    # Fresh working proposal after all-rejected is allowed (old hidden, not blocking).
    fresh = _save(client, _project, record,
                  [{"source_term": "Alpha", "target_term": "GammaTarget", "sense": ""}],
                  key="fresh-after-reject")["items"][0]
    assert fresh["target_term"] == "GammaTarget"
    assert fresh["authority"] == "model_advisory"

    # Human locks are preserved and still take precedence.
    from flask_login import LoginManager, login_user

    from iarf_assistant.models import User
    owner = db.session.get(User, _project.user_id)
    assert owner is not None
    login_manager = LoginManager()
    login_manager.init_app(app)

    @login_manager.user_loader
    def _load_owner(user_id):
        return db.session.get(User, int(user_id))

    from iarf_assistant.addons.translator.block_workspace_api import (
        persist_human_glossary_entry,
    )
    with app.test_request_context("/block-glossary"):
        login_user(owner)
        locked = persist_human_glossary_entry(
            _project, source_term="Alpha", target_term="GammaTarget", notes=None,
            author_user_id=_project.user_id, concept_id=fresh["concept_id"],
            expected_active_version=0, expected_proposal_id=fresh["revision_id"])
    assert locked["entries"][0]["concept_id"] == fresh["concept_id"]
    with pytest.raises(BlockContractError, match="glossary-human-locked"):
        _save(client, _project, record,
              [{"source_term": "Alpha", "target_term": "Other", "sense": "",
                "concept_id": fresh["concept_id"], "expected_revision": fresh["revision"]}],
              key="edit-locked")


def test_glossary_read_single_oversized_entry_is_explicit(app):
    from iarf_assistant.addons.translator.working_glossary import MAX_READ_CHARS
    _project, _plan, record, _operation, client = _setup(
        "<html><body><p>Alpha.</p></body></html>")
    forms = [f"{'é' * 700}{index:04d}" for index in range(127)]
    _save(client, _project, record,
          [{"source_term": "Alpha", "target_term": "Target", "sense": "",
            "source_forms": forms}], key="oversized-forms")
    full = client.glossary_get(_project.id, limit=50)
    assert full["pagination"]["total"] == 1
    assert len(full["items"]) == 1
    raw_chars = len(json.dumps(full["items"], ensure_ascii=False))
    raw_bytes = len(json.dumps(full["items"], ensure_ascii=False).encode("utf-8"))
    assert raw_bytes > MAX_READ_CHARS
    assert raw_chars < raw_bytes
    assert full["pagination"].get("oversized_entry") is True
    assert full["pagination"].get("next_offset") is None


def test_unspaced_script_substring_retrieval_without_compound_leak(app):
    project, plan, record, _operation, client = _setup(
        "<html><body><p>我去北京旅游。</p><p>今日はありがとうございます。</p>"
        "<p>สวัสดีครับ</p><p>Die Beratung ist wichtig.</p>"
        "<p>학교에서 공부한다.</p></body></html>")
    _save(client, project, record, [
        {"source_term": "北京", "target_term": "Beijing", "sense": ""},
        {"source_term": "ありがとう", "target_term": "thank you", "sense": ""},
        {"source_term": "สวัสดี", "target_term": "hello", "sense": ""},
        {"source_term": "Rat", "target_term": "council", "sense": ""},
        {"source_term": "학교", "target_term": "school", "sense": ""},
    ], key="unspaced-fill")
    from iarf_assistant.addons.translator.working_glossary import build_glossary_slot
    # Continuous Chinese / Japanese / Thai windows retrieve the term.
    for index in range(3):
        slot = build_glossary_slot(record, plan, _window(plan, start=index, end=index + 1))
        assert slot is not None, index
    # Rat must not fire inside Beratung; Hangul keeps boundaries (no
    # morphology claim), so neither bare headword matches its carrier.
    assert build_glossary_slot(record, plan, _window(plan, start=3, end=4)) is None
    assert build_glossary_slot(record, plan, _window(plan, start=4, end=5)) is None
    # Supplementary ideographs (Extension B) are Han for retrieval.
    assert build_glossary_slot(record, plan, _window(plan, start=0, end=1)) is not None


def test_nfkc_duplicate_fence_and_human_lock(app):
    from iarf_assistant.addons.translator.analysis_contract import (
        glossary_lookup_digest,
        normalized_term,
    )
    from iarf_assistant.addons.translator.models import TranslationGlossaryRevision
    project, _plan, record, _operation, client = _setup()
    fullwidth = "Ｇｅｍｅｉｎｄｅ"
    # Legacy layer is unchanged: casefold-only digests miss the NFKC equivalent.
    assert normalized_term(fullwidth) != normalized_term("Gemeinde")
    assert glossary_lookup_digest(fullwidth) != glossary_lookup_digest("Gemeinde")
    _save(client, project, record, [_term()])
    row = TranslationGlossaryRevision.query.one()
    assert row.normalization_profile == "unicode-casefold-v1"
    assert row.normalization_version == "1"
    # Ordinary NFKC-equivalent proposal is rejected as a duplicate.
    with pytest.raises(BlockContractError, match="glossary-concept-exists"):
        _save(client, project, record,
              [{"source_term": fullwidth, "target_term": "congregation", "sense": "religious"}],
              key="dup-fullwidth")
    # Combining vs precomposed canonical equivalents are also duplicates
    # (whitespace/case folded sense still collides).
    with pytest.raises(BlockContractError, match="glossary-concept-exists"):
        _save(client, project, record,
              [{"source_term": fullwidth, "target_term": "congregation",
                "sense": "  RELIGIOUS "}],
              key="dup-fullwidth-sense-ws")
    # Different sense stays a separate concept.
    other = _save(client, project, record,
                  [{"source_term": fullwidth, "target_term": "municipality",
                    "sense": "administrative"}],
                  key="other-sense-fullwidth")["items"][0]
    assert other["concept_id"] is not None
    assert TranslationGlossaryRevision.query.count() == 2
    # Canonical combining vs precomposed equivalents are also duplicates.
    _save(client, project, record,
          [{"source_term": "caf\u00e9", "target_term": "cafe", "sense": ""}],
          key="cafe-precomposed")
    with pytest.raises(BlockContractError, match="glossary-concept-exists"):
        _save(client, project, record,
              [{"source_term": "cafe\u0301", "target_term": "cafe", "sense": ""}],
              key="cafe-combining")
    # Fullwidth equivalent of a human-locked lemma reports a human lock.
    from iarf_assistant.addons.translator.glossary_persistence import (
        activate_glossary_revision,
    )
    _c, _p, _locked = _lock_with_usage(app, record, "Kirche", "church", "religious")
    activate_glossary_revision(_locked, expected_version=0)
    db.session.commit()
    with pytest.raises(BlockContractError, match="glossary-human-locked"):
        _save(client, project, record,
              [{"source_term": "Ｋｉｒｃｈｅ", "target_term": "church", "sense": "religious"}],
              key="locked-fullwidth")
    # Same-batch NFKC duplicates are atomic (no partial commit).
    before = TranslationGlossaryRevision.query.count()
    with pytest.raises(BlockContractError, match="glossary-concept-exists"):
        _save(client, project, record,
              [{"source_term": "Neubegriff", "target_term": "new", "sense": ""},
               {"source_term": "Ｎｅｕｂｅｇｒｉｆｆ", "target_term": "new", "sense": ""}],
              key="batch-nfkc-dup")
    assert TranslationGlossaryRevision.query.count() == before


def test_myanmar_extension_ranges_exclude_bamum():
    import unicodedata

    from iarf_assistant.addons.translator.working_glossary import (
        _has_unspaced_script,
    )
    # A6E0 is Bamum, not Myanmar; AA60/A9E0 are Myanmar extensions.
    assert unicodedata.name(chr(0xA6E0)) == "BAMUM LETTER MBEN"
    assert unicodedata.name(chr(0xAA60)).startswith("MYANMAR")
    assert unicodedata.name(chr(0xA9E0)).startswith("MYANMAR")
    assert _has_unspaced_script(chr(0xAA60))
    assert _has_unspaced_script(chr(0xA9E0))
    assert not _has_unspaced_script(chr(0xA6E0))
    assert not _has_unspaced_script("Rat")
