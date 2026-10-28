"""Focused contract checks for the document-block workflow conductor."""

import hashlib
import json
from importlib import import_module
from types import SimpleNamespace

import pytest

from carlina.core.semantic_html import SEMANTIC_HTML_CONTRACT_VERSION

_runtime = import_module("iarf" + "_assistant.addons.translator.block_workflow_runtime")
_analysis = import_module("iarf" + "_assistant.addons.translator.block_analysis")
_contract = import_module("iarf" + "_assistant.addons.translator.block_contract")
_models = import_module("iarf" + "_assistant.addons.translator.models")
_planner = import_module("iarf" + "_assistant.addons.translator.block_planner")
_capabilities = import_module("iarf" + "_assistant.core.model_capabilities")


class _RetrySession:
    def __init__(self, rows):
        self.rows = rows

    def get(self, _model, identifier):
        return self.rows.get(identifier)


def _reduced(*, unresolved):
    return _analysis.AnalysisReduceResult(
        source_plan_hash="a" * 64,
        document_brief={
            "summary": "A book.",
            "domains": ["fiction"],
            "audience": "adult",
        },
        style_guide={
            "register": "neutral",
            "guidance": ["Keep voice."],
            "prohibitions": [],
        },
        entity_register={"entities": []},
        glossary_proposals=(),
        unresolved=tuple(unresolved),
        scope_hashes=("b" * 64,),
    )


def _source_plan(*paragraphs: str):
    body = "<h1>Title</h1>" + "".join(f"<p>{item}</p>" for item in paragraphs)
    html = f"<html><body>{body}</body></html>"
    digest = hashlib.sha256(html.encode()).hexdigest()
    snapshot = _contract.AttestedDocumentSnapshot(
        id="research-doc",
        document_id="research-doc",
        output_html=html,
        content_hash=digest,
        semantic_content_hash=digest,
        metadata={"title": "Research document"},
        provenance={},
        captured_at="2026-08-30T00:00:00+00:00",
        semantic_contract_version=SEMANTIC_HTML_CONTRACT_VERSION,
    )
    return _planner.plan_document_blocks(
        snapshot,
        project_id="project",
        source_attestation=_contract.SourceProviderAttestation(
            provider_addon_id="doc",
            provider_capability_id="process",
            owner_id="project",
            resource_id="research-doc",
        ),
        source_language="en",
    )


def _research_phase(*, usable_input_tokens: int):
    profile = _capabilities.ModelCapabilityProfile(
        model_id="research-model",
        provider_name="test",
        provider_kind="native",
        catalog_model_id="research-model",
        max_input_tokens=usable_input_tokens + 100,
        max_output_tokens=128_000,
        input_safety_margin_tokens=100,
        usable_input_tokens=usable_input_tokens,
        supports_vision=False,
        supports_pdf=False,
        supports_audio_input=False,
        supports_reasoning=True,
        supports_tools=True,
        supports_tool_choice=True,
        supports_structured_output=False,
        structured_output_mode="none",
        supports_streaming=True,
        supports_prompt_caching=False,
    )
    profile_data = json.loads(json.dumps(profile.to_dict()))
    profile_hash = hashlib.sha256(
        json.dumps(
            profile_data,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return {
        "model": {
            "model_id": "research-model",
            "model_record_id": 1,
            "model_owner_user_id": None,
            "model_revision": "1",
            "model_updated_at": "2026-08-30T00:00:00+00:00",
            "provider_id": 1,
            "provider_name": "test",
            "provider_kind": "native",
            "provider_owner_user_id": None,
            "api_key_id": 1,
            "api_key_owner_user_id": None,
            "api_key_created_at": "2026-08-30T00:00:00+00:00",
            "credential_source": "saved",
            "credential_witness": "c" * 64,
            "capability_profile": profile_data,
            "capability_profile_hash": profile_hash,
        },
        "reasoning": {
            "addon_id": "translator",
            "slot": "researcher",
            "model_id": "research-model",
            "config": {"level": "high"},
        },
        "selected": {
            "budget": {"output_tokens": 128_000},
            "tool_declarations": [],
        },
    }


def _character_counter(**kwargs):
    if kwargs.get("messages") is not None:
        return len(json.dumps(kwargs["messages"], ensure_ascii=False))
    return len(str(kwargs.get("text") or ""))


def test_translation_scope_replaces_selected_active_blocks_only():
    plan_ids = frozenset({"block-1", "block-2", "block-3"})
    active_ids = frozenset({"block-1", "block-2"})

    assert (
        _runtime._translation_scope_exclusions(
            plan_block_ids=plan_ids,
            translation_scope={"mode": "pending", "block_ids": []},
            verified_active_target_ids=active_ids,
        )
        == active_ids
    )
    assert _runtime._translation_scope_exclusions(
        plan_block_ids=plan_ids,
        translation_scope={"mode": "explicit", "block_ids": ["block-2"]},
        verified_active_target_ids=active_ids,
    ) == frozenset({"block-1", "block-3"})

    with pytest.raises(_contract.BlockContractError, match="block-translation-scope"):
        _runtime._translation_scope_exclusions(
            plan_block_ids=plan_ids,
            translation_scope={"mode": "explicit", "block_ids": ["stale-block"]},
            verified_active_target_ids=active_ids,
        )


def test_translation_provider_projection_omits_profile_v4_and_v5_output_limits():
    packet = {
        "contract_version": "test",
        "messages": [{"role": "user", "content": "source"}],
        "response_format": None,
        "transport": "strict_text",
        "requested_output_tokens": 334,
        "slots": [],
    }

    assert _runtime._translation_provider_packet(packet, profile_version="4") == {
        "messages": packet["messages"],
        "tools": None,
        "response_format": None,
        "max_completion_tokens": None,
    }
    assert (
        _runtime._translation_provider_packet(packet, profile_version="5")[
            "max_completion_tokens"
        ]
        is None
    )
    assert (
        _runtime._translation_provider_packet(packet, profile_version="3")[
            "max_completion_tokens"
        ]
        == 334
    )

    with pytest.raises(_contract.BlockContractError, match="provider-packet"):
        _runtime._translation_provider_packet(packet, profile_version="6")


def test_research_inputs_preserve_whole_unresolved_items_with_omission_count():
    unresolved = [
        {
            "question": f"Question {index}",
            "reason": "ambiguity",
            "impact": "medium",
            "source_block_ids": [f"block-{index}"],
        }
        for index in range(3)
    ]

    brief, question = _runtime._research_inputs(_reduced(unresolved=unresolved))

    assert brief["source_plan_hash"] == "a" * 64
    assert question["unresolved"] == unresolved
    assert question["omitted_unresolved_count"] == 0
    assert question["require_tool_evidence"] is False


def test_research_inputs_create_a_preliminary_discovery_task_without_uncertainties():
    brief, question = _runtime._research_inputs(_reduced(unresolved=[]))

    assert brief["source_plan_hash"] == "a" * 64
    assert question["unresolved"] == []
    assert question["omitted_unresolved_count"] == 0
    assert question["require_tool_evidence"] is True
    assert "translation traps" in question["task"]
    assert "at least one selected read-only tool" in question["task"]


def test_research_source_admits_the_exact_full_document_when_it_fits():
    plan = _source_plan("First paragraph.", "Second paragraph.")
    brief, question = _runtime._research_inputs(_reduced(unresolved=[]))

    material, admission = _runtime._research_source_for_operation(
        plan=plan,
        phase=_research_phase(usable_input_tokens=100_000),
        semantic_brief=brief,
        research_question=question,
        token_counter=_character_counter,
    )

    assert material["scope"]["mode"] == "full_document"
    assert admission["admitted"] is True
    assert admission["requested_output_tokens"] == 128_000
    assert admission["measurement"]["extra_parameter_tokens"] > 0
    assert material["scope"]["omitted_block_count"] == 0
    assert [item["content"] for item in material["source_blocks"]] == [
        block.projection for block in plan.blocks
    ]


def test_research_transport_accepts_a_source_message_above_the_window_limit():
    plan = _source_plan("x" * (2 * 1024 * 1024 + 8_192))
    brief, question = _runtime._research_inputs(_reduced(unresolved=[]))

    material, admission = _runtime._research_source_for_operation(
        plan=plan,
        phase=_research_phase(usable_input_tokens=8_000_000),
        semantic_brief=brief,
        research_question=question,
        token_counter=_character_counter,
    )

    assert material["scope"]["mode"] == "full_document"
    assert material["scope"]["source_bytes"] > 2 * 1024 * 1024
    assert admission["admitted"] is True


def test_research_planner_checks_the_final_durable_envelope_with_large_tools():
    plan = _source_plan(*(f"Block {index}: " + "x" * 1_100_000 for index in range(3)))
    brief, question = _runtime._research_inputs(_reduced(unresolved=[]))
    phase = _research_phase(usable_input_tokens=9_000_000)
    phase["selected"]["tool_declarations"] = [
        {
            "declaration": {
                "type": "function",
                "function": {
                    "name": f"research_{index}",
                    "description": "d" * 100_000,
                    "parameters": {"type": "object"},
                },
            }
        }
        for index in range(32)
    ]

    material, admission = _runtime._research_source_for_operation(
        plan=plan,
        phase=phase,
        semantic_brief=brief,
        research_question=question,
        token_counter=_character_counter,
    )

    assert material["scope"]["mode"] == "full_document"
    assert admission["admitted"] is True


def test_research_source_falls_back_when_the_provider_envelope_overflows(monkeypatch):
    plan = _source_plan(*(f"Paragraph {index}." for index in range(4)))
    brief, question = _runtime._research_inputs(_reduced(unresolved=[]))
    validate = _runtime.validate_research_provider_request

    def bounded_provider_request(**kwargs):
        messages = kwargs["packet"]["messages"]
        payload = json.loads(messages[1]["content"])
        if len(payload["source_document"]["source_blocks"]) > 2:
            raise _contract.BlockContractError("provider-packet-json")
        return validate(**kwargs)

    monkeypatch.setattr(
        _runtime,
        "validate_research_provider_request",
        bounded_provider_request,
    )

    material, admission = _runtime._research_source_for_operation(
        plan=plan,
        phase=_research_phase(usable_input_tokens=100_000),
        semantic_brief=brief,
        research_question=question,
        token_counter=_character_counter,
    )

    assert material["scope"]["mode"] == "complete_block_prefix"
    assert material["scope"]["block_ids"] == [block.id for block in plan.blocks[:2]]
    assert material["scope"]["omitted_block_count"] == len(plan.blocks) - 2
    assert admission["admitted"] is True


def test_research_source_fallback_is_token_aware_and_never_slices_blocks():
    plan = _source_plan(*(f"Paragraph {index}: " + "x" * 800 for index in range(5)))
    brief, question = _runtime._research_inputs(_reduced(unresolved=[]))

    material, admission = _runtime._research_source_for_operation(
        plan=plan,
        phase=_research_phase(usable_input_tokens=4_000),
        semantic_brief=brief,
        research_question=question,
        token_counter=_character_counter,
    )

    scope = material["scope"]
    assert admission["admitted"] is True
    assert scope["mode"] == "complete_block_prefix"
    assert 0 < scope["omitted_block_count"] < len(plan.blocks)
    included = material["source_blocks"]
    assert [item["block_id"] for item in included] == [
        block.id for block in plan.blocks[: len(included)]
    ]
    assert [item["content"] for item in included] == [
        block.projection for block in plan.blocks[: len(included)]
    ]


def test_research_source_uses_language_first_markup_without_internal_scaffolding():
    plan = _source_plan(
        'Body <em>text</em> and <a href="https://example.test/source">link</a>.'
    )
    brief, question = _runtime._research_inputs(_reduced(unresolved=[]))

    material, _admission = _runtime._research_source_for_operation(
        plan=plan,
        phase=_research_phase(usable_input_tokens=100_000),
        semantic_brief=brief,
        research_question=question,
        token_counter=_character_counter,
    )

    block = material["source_blocks"][1]
    assert set(block) == {"block_id", "role", "content", "fields"}
    assert block["content"] == 'Body <em>text</em> and <a r="1">link</a>.'
    assert block["fields"] == []
    assert "https://example.test/source" not in json.dumps(block)


def test_research_source_fails_when_one_complete_block_cannot_fit():
    plan = _source_plan("x" * 4_000)
    brief, question = _runtime._research_inputs(_reduced(unresolved=[]))

    with pytest.raises(
        _contract.BlockContractError, match="block-research-oversized-block"
    ):
        _runtime._research_source_for_operation(
            plan=plan,
            phase=_research_phase(usable_input_tokens=500),
            semantic_brief=brief,
            research_question=question,
            token_counter=_character_counter,
        )


def test_enabled_research_runs_without_analysis_uncertainties(monkeypatch):
    durable = SimpleNamespace(state="completed")

    class _Query:
        def filter_by(self, **_kwargs):
            return self

        def all(self):
            return []

        def one_or_none(self):
            return durable

    class _ResearchRun:
        query = _Query()

    captured = {}
    monkeypatch.setattr(_runtime, "TranslationResearchRun", _ResearchRun)
    monkeypatch.setattr(
        _runtime,
        "db",
        SimpleNamespace(session=SimpleNamespace(expire_all=lambda: None)),
    )

    def run_research(**kwargs):
        captured.update(kwargs)
        captured["adapter_result"] = kwargs["model_adapter"](
            model=phase["model"],
            messages=[{"role": "user", "content": "research"}],
            tools=phase["selected"]["tool_declarations"],
            settings=kwargs["settings"],
            profile=kwargs["profile"],
            budget={"output_tokens": phase["selected"]["budget"]["output_tokens"]},
            provider_max_attempts=1,
            action_id="attempt-1",
        )

    monkeypatch.setattr(_runtime, "run_block_research", run_research)

    def call_research(*_args, **kwargs):
        captured["provider_phase"] = kwargs["phase"]
        captured["provider_packet"] = kwargs["packet"]
        return {"conclusion": "done", "tool_calls": []}

    monkeypatch.setattr(_runtime, "call_research", call_research)
    phase = {
        "model": {"model_id": "research-model"},
        "reasoning": {
            "addon_id": "translator",
            "slot": "researcher",
            "model_id": "research-model",
            "config": None,
        },
        "selected": {
            "budget": {
                "max_model_calls": 128,
                "max_tool_calls": 64,
                "output_tokens": 128_000,
            },
            "tool_declarations": [{"tool_name": "search"}],
            "selected_provider_ids": ["web"],
            "selected_addon_ids": ["web-addon"],
        },
    }

    result = _runtime._run_research(
        operation=SimpleNamespace(id="operation-1"),
        source_plan=object(),
        config={"enable_research": True, "research": phase},
        workflow=SimpleNamespace(
            id="workflow-1",
            config_hash="a" * 64,
            config_receipt="b" * 64,
        ),
        reduced=_reduced(unresolved=[]),
        service=object(),
        addon_manager=object(),
    )

    assert result is durable
    assert captured["enabled"] is True
    assert captured["research_question"]["unresolved"] == []
    assert captured["research_question"]["require_tool_evidence"] is True
    assert "translation traps" in captured["research_question"]["task"]
    assert captured["settings"]["research_input_bytes"] == 320 * 1024
    assert captured["settings"]["research_result_bytes"] == 2 * 1024 * 1024
    assert captured["adapter_result"] == {"conclusion": "done", "tool_calls": []}
    assert captured["provider_packet"]["max_completion_tokens"] is None
    assert captured["provider_phase"] == {
        "model": phase["model"],
        "reasoning": phase["reasoning"],
    }


def test_research_inputs_fail_when_one_whole_unresolved_item_cannot_fit():
    unresolved = [
        {
            "question": "x" * (40 * 1024),
            "reason": "ambiguity",
            "impact": "high",
            "source_block_ids": ["block-1"],
        }
    ]

    with pytest.raises(
        _contract.BlockContractError, match="block-research-question-too-large"
    ):
        _runtime._research_inputs(_reduced(unresolved=unresolved))


def test_retry_count_uses_dispatch_evidence_and_rejects_missing_parent(monkeypatch):
    root = _models.TranslationAnalysisAttempt(id="root", retry_of_attempt_id=None)
    retry = _models.TranslationAnalysisAttempt(id="retry", retry_of_attempt_id="root")
    session = _RetrySession({"root": root})
    events = {
        "root": (SimpleNamespace(event_type="request_prepared"),),
        "retry": (
            SimpleNamespace(event_type="request_prepared"),
            SimpleNamespace(event_type="provider_dispatched"),
        ),
        "orphan": (SimpleNamespace(event_type="request_prepared"),),
    }
    monkeypatch.setattr(
        _runtime,
        "rehydrate_analysis_attempt_events",
        lambda attempt, *, session: events[attempt.id],
    )

    assert (
        _runtime._retry_dispatch_count(retry, code="retry-lineage", session=session)
        == 1
    )

    with pytest.raises(_contract.BlockContractError, match="retry-lineage"):
        _runtime._retry_dispatch_count(
            _models.TranslationAnalysisAttempt(
                id="orphan", retry_of_attempt_id="missing"
            ),
            code="retry-lineage",
            session=session,
        )


def test_map_settlement_consumes_one_known_provider_failure_retry(monkeypatch):
    scope = SimpleNamespace()
    root = SimpleNamespace(id="root", dispatch_state="prepared")
    failed = SimpleNamespace(id="root", dispatch_state="provider_failed")
    retry = SimpleNamespace(id="retry", dispatch_state="prepared")
    result = SimpleNamespace()
    discoveries = iter(
        (
            _analysis.AnalysisAttemptDiscovery(None, None, False, False, False),
            _analysis.AnalysisAttemptDiscovery(failed, failed.id, False, False, False),
        )
    )
    prepared = iter((root, retry))
    executions = iter((RuntimeError("provider rejected output"), result))

    monkeypatch.setattr(
        _runtime, "discover_exact_map_attempt", lambda **_kwargs: next(discoveries)
    )
    monkeypatch.setattr(
        _runtime, "prepare_block_analysis_attempt", lambda **_kwargs: next(prepared)
    )
    monkeypatch.setattr(_runtime, "_refresh_analysis_attempt", lambda _attempt: failed)
    monkeypatch.setattr(_runtime, "_retry_dispatch_count", lambda *_args, **_kwargs: 1)

    def execute(attempt, **_kwargs):
        outcome = next(executions)
        if isinstance(outcome, Exception):
            raise outcome
        assert attempt is retry
        return outcome

    monkeypatch.setattr(_runtime, "execute_block_analysis_attempt", execute)

    settled, settled_attempt = _runtime._settle_map(
        operation=object(),
        source_plan=object(),
        scope=scope,
        phase={},
        settings={},
        budget={},
        service=object(),
    )

    assert settled is result
    assert settled_attempt is retry


def test_reduce_settlement_consumes_one_known_provider_failure_retry(monkeypatch):
    root = SimpleNamespace(id="root", dispatch_state="prepared")
    failed = SimpleNamespace(id="root", dispatch_state="provider_failed")
    retry = SimpleNamespace(id="retry", dispatch_state="prepared")
    result = _reduced(unresolved=[])
    discoveries = iter(
        (
            _analysis.AnalysisAttemptDiscovery(None, None, False, False, False),
            _analysis.AnalysisAttemptDiscovery(failed, failed.id, False, False, False),
        )
    )
    prepared = iter((root, retry))
    executions = iter((ValueError("invalid structured response"), result))

    monkeypatch.setattr(
        _runtime,
        "discover_exact_reduce_attempt",
        lambda **_kwargs: next(discoveries),
    )
    monkeypatch.setattr(
        _runtime,
        "prepare_reduce_analysis_attempt",
        lambda **_kwargs: next(prepared),
    )
    monkeypatch.setattr(_runtime, "_refresh_analysis_attempt", lambda _attempt: failed)
    monkeypatch.setattr(_runtime, "_retry_dispatch_count", lambda *_args, **_kwargs: 1)

    def execute(attempt, **_kwargs):
        outcome = next(executions)
        if isinstance(outcome, Exception):
            raise outcome
        assert attempt is retry
        return outcome

    monkeypatch.setattr(_runtime, "execute_reduce_analysis_attempt", execute)

    settled, settled_attempt = _runtime._settle_reduce(
        operation=object(),
        source_plan=object(),
        results=(SimpleNamespace(),),
        phase={},
        settings={},
        budget={},
        service=object(),
    )

    assert settled is result
    assert settled_attempt is retry


def test_memory_callback_unwraps_and_binds_frozen_generation(monkeypatch):
    pointer = {"generation_id": "generation-1", "pointer_receipt": "p" * 64}
    generation = SimpleNamespace(
        id="generation-1",
        record_hash="r" * 64,
        model_record_id="model-record-1",
        model_id="embedding-model",
        provider_id="provider-1",
        owner_user_id=7,
        task_type="RETRIEVAL_DOCUMENT",
        dimensions=768,
        api_key_id="key-1",
        signature_hash="s" * 64,
        namespace="memory-space",
    )
    reference = {
        "pointer": pointer,
        "generation_id": generation.id,
        "generation_record_hash": generation.record_hash,
        "model_record_id": generation.model_record_id,
        "model_id": generation.model_id,
        "provider_id": generation.provider_id,
    }
    baseline = SimpleNamespace(
        items=[
            SimpleNamespace(item_kind="translation_memory", reference_json=reference)
        ]
    )
    model = SimpleNamespace(id=generation.model_record_id)
    seen = {}

    def verify(candidate):
        assert candidate == pointer
        return generation, SimpleNamespace()

    class Store:
        def __init__(self, **kwargs):
            seen["store"] = kwargs
            self.namespace = None

    def contextual(*args, **kwargs):
        seen["context"] = (args, kwargs)
        return "memory-slot"

    monkeypatch.setattr(_runtime, "verify_pinned_embedding_reference", verify)
    monkeypatch.setattr(_runtime, "TranslatorDenseVectorStore", Store)
    monkeypatch.setattr(_runtime, "contextual_memory_slot", contextual)
    monkeypatch.setattr(
        _runtime.db.session,
        "get",
        lambda model_type, identifier, **_kwargs: (
            model
            if model_type is _runtime.LLMModel and identifier == model.id
            else None
        ),
    )

    callback = _runtime._pinned_memory_callback(
        baseline=baseline,
        source_plan=SimpleNamespace(id="plan-1"),
        config={"embedding_projection": {"projection_kind": "memory"}},
    )

    assert callback is not None
    assert callback((SimpleNamespace(id="block-1", projection="Source"),)) == (
        "memory-slot"
    )
    assert seen["store"]["expected_api_key_id"] == generation.api_key_id
    assert seen["context"][1]["backend"].namespace == generation.namespace
