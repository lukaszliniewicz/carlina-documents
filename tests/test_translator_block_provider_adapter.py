"""Focused tests for the one-shot document-block provider boundary."""

from __future__ import annotations

import hashlib
import json
import logging
from importlib import import_module
from types import SimpleNamespace
from typing import Any

import pytest

from iarf_assistant.addons.translator.block_contract import BlockContractError
from iarf_assistant.addons.translator.block_provider_adapter import (
    ANALYSIS_SOURCE,
    RESEARCH_SOURCE,
    TRANSLATION_SOURCE,
    call_analysis,
    call_research,
    call_translation_window,
)

_adapter = import_module(
    "iarf" + "_assistant.addons.translator.block_provider_adapter"
)


def _phase(
    *,
    slot: str = "translation",
    model_id: str = "translator-model",
    owner_user_id: int | None = 11,
):
    capability_profile = {
        "model_id": model_id,
        "provider_name": "Test provider",
        "provider_kind": "openai_compatible",
        "catalog_model_id": model_id,
        "max_input_tokens": 32_768,
        "max_output_tokens": 8_192,
        "input_safety_margin_tokens": 2_048,
        "usable_input_tokens": 30_720,
        "supports_vision": False,
        "supports_pdf": False,
        "supports_audio_input": False,
        "supports_reasoning": True,
        "supports_tools": True,
        "supports_tool_choice": True,
        "supports_structured_output": True,
        "structured_output_mode": "json_schema",
        "supports_streaming": True,
        "supports_prompt_caching": False,
        "detected_input_tokens": 32_768,
        "override_input_tokens": None,
        "detected_output_tokens": 8_192,
        "override_output_tokens": None,
        "metadata_sources": {},
        "confidence": "high",
        "missing_metadata": [],
    }
    encoded_profile = json.dumps(
        capability_profile,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    model = {
        "model_id": model_id,
        "model_record_id": 7,
        "model_owner_user_id": owner_user_id,
        "model_revision": "rev-1",
        "model_updated_at": "2026-08-26T00:00:00+00:00",
        "provider_id": 13,
        "provider_name": "Test provider",
        "provider_kind": "openai_compatible",
        "provider_owner_user_id": owner_user_id,
        "api_key_id": 17,
        "api_key_owner_user_id": owner_user_id,
        "api_key_created_at": "2026-08-26T00:00:00+00:00",
        "credential_source": "saved",
        "credential_witness": "witness",
        "capability_profile": capability_profile,
        "capability_profile_hash": hashlib.sha256(encoded_profile).hexdigest(),
    }
    return {
        "model": model,
        "reasoning": {
            "addon_id": "translator",
            "slot": slot,
            "model_id": model_id,
            "config": None,
        },
    }


def _external_phase():
    profile = {
        "schema_version": "carlina.external-executor.v1",
        "max_input_tokens": 64_000,
        "max_output_tokens": 16_000,
        "input_modalities": ["text"],
        "supports_tools": True,
        "supports_structured_output": True,
        "supports_reasoning": True,
        "user_confirmed": True,
    }
    normalized = _adapter.normalize_executor_profile(profile)
    encoded = json.dumps(
        normalized.capability_profile("translator").to_dict(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    capability = json.loads(encoded)
    return normalized, {
        "model": {
            "identity_kind": "external_executor",
            "identity_version": "translator-external-executor-v1",
            "role": "translator",
            "model_id": "translator",
            "executor_profile": profile,
            "capability_profile": capability,
            "capability_profile_hash": hashlib.sha256(encoded).hexdigest(),
        },
        "reasoning": {
            "addon_id": "translator",
            "slot": "translation",
            "model_id": "translator",
            "config": None,
        },
    }


def _packet(*, tools=None, response_format=None, output_tokens=128):
    return {
        "messages": [
            {"role": "system", "content": "system\ncontract"},
            {"role": "user", "content": "source block"},
        ],
        "tools": tools,
        "response_format": response_format,
        "max_completion_tokens": output_tokens,
    }


def _response(
    *,
    content: str | None = "translated\ntext",
    tool_calls: Any = None,
    role="assistant",
):
    message = {"role": role, "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(**message))]
    )


class _Service:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def completion(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


def _assert_common_call(
    call, *, source, action_id, tools, response_format, output_tokens=128
):
    assert call["model"] == "translator-model"
    assert call["messages"] == _packet()["messages"]
    assert call["tools"] == tools
    assert call["response_format"] == response_format
    if output_tokens is None:
        assert "max_completion_tokens" not in call
    else:
        assert call["max_completion_tokens"] == output_tokens
    assert call["thinking_config"] == {"level": "default"}
    assert call["source"] == source
    assert call["reasoning_addon_id"] == "translator"
    assert call["reasoning_slot"] in {
        "translation",
        "terminology_analysis",
        "researcher",
    }
    assert call["provider_max_attempts"] == 1
    assert call["redact_provider_error_logs"] is True
    assert call["expected_api_key_id"] == 17
    assert call["model_capability_overrides"]["max_input_tokens"] == 32_768
    assert call["model_capability_overrides"]["max_output_tokens"] == 8_192
    assert call["context_admission_mode"] == "manual"
    assert call["enforce_context_admission"] is True
    assert call["log_to_db"] is False
    assert call["cost_attribution"] == {"action_id": action_id}
    assert isinstance(call["debug_capture_sink"], dict)
    assert call["debug_capture_include_content"] is False


def test_translation_uses_strict_text_packet_and_exact_frozen_call():
    service = _Service(_response())
    packet = _packet(output_tokens=None)

    assert (
        call_translation_window(
            service, phase=_phase(), packet=packet, action_id="window-1"
        )
        == "translated\ntext"
    )

    assert len(service.calls) == 1
    _assert_common_call(
        service.calls[0],
        source=TRANSLATION_SOURCE,
        action_id="window-1",
        tools=None,
        response_format=None,
        output_tokens=None,
    )


def test_translation_preserves_an_explicit_legacy_output_limit():
    service = _Service(_response())

    assert (
        call_translation_window(
            service, phase=_phase(), packet=_packet(), action_id="legacy-window"
        )
        == "translated\ntext"
    )

    assert service.calls[0]["max_completion_tokens"] == 128


def test_analysis_accepts_an_unbounded_output_packet():
    service = _Service(_response(content="analysis"))

    assert call_analysis(
        service,
        phase=_phase(slot="terminology_analysis"),
        packet=_packet(output_tokens=None),
        action_id="analysis-unbounded",
    ) == {"role": "assistant", "content": "analysis"}

    _assert_common_call(
        service.calls[0],
        source=ANALYSIS_SOURCE,
        action_id="analysis-unbounded",
        tools=None,
        response_format=None,
        output_tokens=None,
    )


def test_research_accepts_an_unbounded_output_packet():
    service = _Service(_response(content="research"))

    assert call_research(
        service,
        phase=_phase(slot="researcher"),
        packet=_packet(output_tokens=None),
        action_id="research-unbounded",
    ) == {"conclusion": "research", "tool_calls": []}

    _assert_common_call(
        service.calls[0],
        source=RESEARCH_SOURCE,
        action_id="research-unbounded",
        tools=None,
        response_format=None,
        output_tokens=None,
    )


def test_analysis_preserves_native_schema_and_defaults_tools_to_absent():
    schema = {
        "type": "json_schema",
        "json_schema": {
            "name": "analysis",
            "strict": True,
            "schema": {"type": "object"},
        },
    }
    service = _Service(_response(content="analysis"))
    result = call_analysis(
        service,
        phase=_phase(slot="terminology_analysis"),
        packet=_packet(response_format=schema),
        action_id="analysis-1",
    )

    assert result == {"role": "assistant", "content": "analysis"}
    _assert_common_call(
        service.calls[0],
        source=ANALYSIS_SOURCE,
        action_id="analysis-1",
        tools=None,
        response_format=schema,
    )


def test_global_model_provider_and_key_owners_are_valid_frozen_identities():
    service = _Service(_response())

    assert (
        call_translation_window(
            service,
            phase=_phase(owner_user_id=None),
            packet=_packet(),
            action_id="global-window",
        )
        == "translated\ntext"
    )
    assert service.calls[0]["expected_api_key_id"] == 17


@pytest.mark.parametrize(
    ("call", "slot", "code"),
    [
        (call_translation_window, "translation", "provider-translation-tools"),
        (call_analysis, "terminology_analysis", "provider-analysis-tools"),
    ],
)
def test_nonresearch_phases_reject_tools_before_provider_io(call, slot, code):
    service = _Service(_response())
    with pytest.raises(BlockContractError, match=code):
        call(
            service,
            phase=_phase(slot=slot),
            packet=_packet(tools=[{"type": "function"}]),
            action_id="no-tools",
        )
    assert service.calls == []


def test_analysis_rejects_tool_calls_and_missing_text():
    tool_call = SimpleNamespace(
        id="call-1",
        type="function",
        function=SimpleNamespace(name="lookup", arguments="{}"),
    )
    for response, code in (
        (
            _response(content="analysis", tool_calls=[tool_call]),
            "provider-analysis-tool-call",
        ),
        (_response(content=None), "provider-analysis-content"),
    ):
        service = _Service(response)
        with pytest.raises(BlockContractError, match=code):
            call_analysis(
                service,
                phase=_phase(slot="terminology_analysis"),
                packet=_packet(),
                action_id="analysis-shape",
            )
        assert len(service.calls) == 1


def test_research_preserves_tool_ids_and_raw_arguments():
    call = {
        "id": "call-7",
        "type": "function",
        "function": {"name": "lookup", "arguments": '{"q":"a\\n b", "n": 1}'},
    }
    tools = [{"type": "function", "function": {"name": "lookup", "parameters": {}}}]
    service = _Service(_response(content=None, tool_calls=[SimpleNamespace(**call)]))

    result = call_research(
        service,
        phase=_phase(slot="researcher"),
        packet=_packet(tools=tools),
        action_id="research-1",
    )

    assert result == {"conclusion": None, "tool_calls": [call]}
    _assert_common_call(
        service.calls[0],
        source=RESEARCH_SOURCE,
        action_id="research-1",
        tools=tools,
        response_format=None,
    )


def test_research_preserves_strict_tool_evidence_continuation_messages():
    call = {
        "id": "call-7",
        "type": "function",
        "function": {"name": "lookup", "arguments": '{"q":"term"}'},
    }
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "research"},
        {"role": "assistant", "content": "", "tool_calls": [call]},
        {
            "role": "tool",
            "content": '{"status":"ok"}',
            "tool_call_id": "call-7",
            "name": "lookup",
        },
    ]
    packet = {
        **_packet(
            tools=[
                {"type": "function", "function": {"name": "lookup", "parameters": {}}}
            ]
        ),
        "messages": messages,
    }
    service = _Service(_response(content="Evidence-backed conclusion."))

    result = call_research(
        service,
        phase=_phase(slot="researcher"),
        packet=packet,
        action_id="research-continuation",
    )

    assert result == {
        "conclusion": "Evidence-backed conclusion.",
        "tool_calls": [],
    }
    assert service.calls[0]["messages"] == messages


def test_research_only_accepts_a_wider_source_message():
    service = _Service(_response(content="researched"))
    packet = _packet()
    packet["messages"][1]["content"] = "x" * (2 * 1024 * 1024 + 4_096)

    result = call_research(
        service,
        phase=_phase(slot="researcher"),
        packet=packet,
        action_id="research-large-source",
    )

    assert result == {"conclusion": "researched", "tool_calls": []}
    assert service.calls[0]["messages"] == packet["messages"]


def test_research_tool_messages_keep_the_ordinary_message_limit():
    service = _Service(_response(content="never"))
    packet = _packet()
    packet["messages"] = [
        packet["messages"][0],
        {
            "role": "tool",
            "tool_call_id": "call-large",
            "name": "lookup",
            "content": "x" * (2 * 1024 * 1024 + 1),
        },
    ]

    with pytest.raises(BlockContractError, match="provider-packet-message"):
        call_research(
            service,
            phase=_phase(slot="researcher"),
            packet=packet,
            action_id="research-large-tool",
        )
    assert service.calls == []


@pytest.mark.parametrize(
    "response",
    [
        SimpleNamespace(choices=[]),
        SimpleNamespace(
            choices=[
                SimpleNamespace(message=SimpleNamespace(role="assistant", content="a")),
                SimpleNamespace(message=SimpleNamespace(role="assistant", content="b")),
            ]
        ),
        _response(role="user"),
    ],
)
def test_response_shape_is_strict_and_never_coerced(response):
    service = _Service(response)
    with pytest.raises(BlockContractError):
        call_translation_window(
            service, phase=_phase(), packet=_packet(), action_id="x"
        )
    assert len(service.calls) == 1


def test_translation_rejects_any_tool_calls():
    service = _Service(
        _response(
            tool_calls=[
                SimpleNamespace(
                    id="call-1",
                    type="function",
                    function=SimpleNamespace(name="lookup", arguments="{}"),
                )
            ]
        )
    )
    with pytest.raises(BlockContractError, match="provider-translation-tool-call"):
        call_translation_window(
            service, phase=_phase(), packet=_packet(), action_id="x"
        )


def test_json_persisted_external_identity_reaches_passive_completion(monkeypatch):
    normalized, phase = _external_phase()
    service = _Service(_response(content="przetłumaczony tekst"))
    monkeypatch.setattr(
        _adapter,
        "passive_execution_context",
        lambda: SimpleNamespace(
            addon_id="translator",
            resource_kind="project",
            executor_profile=normalized,
        ),
    )

    assert (
        call_translation_window(
            service,
            phase=phase,
            packet=_packet(),
            action_id="external-json-roundtrip",
        )
        == "przetłumaczony tekst"
    )
    assert service.calls[0]["model"] == "translator"
    assert "provider_max_attempts" not in service.calls[0]


@pytest.mark.parametrize(
    "phase",
    [
        {"model": {}, "reasoning": {}},
        {
            "model": _phase()["model"],
            "reasoning": {**_phase()["reasoning"], "model_id": "other"},
        },
        {
            "model": _phase()["model"],
            "reasoning": {**_phase()["reasoning"], "slot": "researcher"},
        },
    ],
)
def test_frozen_phase_drift_is_rejected_before_service_call(phase):
    service = _Service(_response())
    with pytest.raises(BlockContractError):
        call_translation_window(service, phase=phase, packet=_packet(), action_id="x")
    assert service.calls == []


def test_tampered_capability_profile_is_rejected_before_provider_io():
    service = _Service(_response())
    phase = _phase()
    phase["model"]["capability_profile"]["usable_input_tokens"] = 12_345

    with pytest.raises(BlockContractError, match="provider-model-capabilities"):
        call_translation_window(
            service, phase=phase, packet=_packet(), action_id="tampered-profile"
        )

    assert service.calls == []


def test_malformed_packet_is_rejected_before_service_call():
    service = _Service(_response())
    malformed = {**_packet(), "messages": tuple(_packet()["messages"])}
    with pytest.raises(BlockContractError):
        call_translation_window(
            service, phase=_phase(), packet=malformed, action_id="x"
        )
    assert service.calls == []


def test_provider_failure_is_not_retried_or_fallback():
    class Failing(_Service):
        def completion(self, **kwargs):
            self.calls.append(kwargs)
            raise RuntimeError("provider failure")

    service = Failing(_response())
    with pytest.raises(RuntimeError):
        call_translation_window(
            service, phase=_phase(), packet=_packet(), action_id="x"
        )
    assert len(service.calls) == 1
    assert service.calls[0]["provider_max_attempts"] == 1


def test_provider_trace_logs_only_content_free_debug_evidence(caplog):
    class Traced(_Service):
        def completion(self, **kwargs):
            self.calls.append(kwargs)
            kwargs["debug_capture_sink"].update(
                {
                    "messages": [{"role": "user", "content": "private source text"}],
                    "context_admission": {
                        "admitted": True,
                        "requested_output_tokens": 128_000,
                        "available_input_tokens": 900_000,
                        "overflow_tokens": 0,
                        "profile": {
                            "model_id": "translator-model",
                            "max_input_tokens": 1_000_000,
                            "max_output_tokens": 384_000,
                            "usable_input_tokens": 997_952,
                            "credential_witness": "private witness",
                        },
                        "measurement": {"total_input_tokens": 12_345},
                    },
                    "provider_calls": [
                        {
                            "api_base": "https://example.test/v1",
                            "message_manifest": [
                                {"role": "user", "text": {"utf8_bytes": 61_280}}
                            ],
                            "outcome": {"status": "completed", "latency_ms": 250},
                        }
                    ],
                }
            )
            return self.response

    service = Traced(_response())
    module_logger = logging.getLogger(call_translation_window.__module__)
    previous_disabled = module_logger.disabled
    module_logger.disabled = True
    try:
        with caplog.at_level("INFO"):
            call_translation_window(
                service, phase=_phase(), packet=_packet(), action_id="trace-window"
            )
    finally:
        module_logger.disabled = previous_disabled

    assert "translator-provider-trace-v1" in caplog.text
    assert "trace-window" in caplog.text
    assert "private source text" not in caplog.text
    assert "private witness" not in caplog.text
    assert "61280" in caplog.text
