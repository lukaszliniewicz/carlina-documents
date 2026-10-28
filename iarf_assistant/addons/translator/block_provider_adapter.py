"""Strict, one-shot provider adapters for the document-block workflow.

The block workflow freezes the model, credential, reasoning, and request
packet before it reaches this module.  This module is intentionally a very
small boundary: it validates that frozen material has not drifted, performs
one call through an injected :class:`LLMService`-like object, and validates
the provider response without parsing, repairing, retrying, or evaluating it.

Provider-specific response objects are reduced to a deliberately boring
assistant-message DTO.  In particular, tool-call argument strings remain
opaque strings here; parsing and tool execution belong to the durable research
boundary.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping
from typing import Any, Final, cast

from ...core.executor_profile import normalize_executor_profile

from .analysis_scope_contract import SUPPORTED_ANALYSIS_SCOPE_PLANNER_CONTRACTS
from .block_contract import BlockContractError
from .block_workflow_config import (
    EXTERNAL_EXECUTOR_IDENTITY_KIND,
    EXTERNAL_EXECUTOR_IDENTITY_VERSION,
    EXTERNAL_EXECUTOR_ROLE,
)
from ...core.mcp_completion import passive_execution_context
from .window_contract import PROVIDER_MESSAGE_MAX_BYTES, ProviderPacket

TRANSLATION_SOURCE: Final = "translator_block_translation_window"
ANALYSIS_SOURCE: Final = "translator_block_analysis"
RESEARCH_SOURCE: Final = "translator_block_research"
REASONING_ADDON_ID: Final = "translator"
PROVIDER_DEFAULT_OUTPUT_POLICY: Final = "provider-default-v1"

_MODEL_KEYS: Final = frozenset(
    {
        "model_id",
        "model_record_id",
        "model_owner_user_id",
        "model_revision",
        "model_updated_at",
        "provider_id",
        "provider_name",
        "provider_kind",
        "provider_owner_user_id",
        "api_key_id",
        "api_key_owner_user_id",
        "api_key_created_at",
        "credential_source",
        "credential_witness",
        "capability_profile",
        "capability_profile_hash",
    }
)
_MODEL_TRANSPORT_KEYS: Final = frozenset(
    {
        "contract_version",
        "provider_name",
        "provider_kind",
        "api_base",
        "api_version",
        "vertex_project",
        "vertex_location",
    }
)
_EXTERNAL_MODEL_KEYS: Final = frozenset(
    {
        "identity_kind",
        "identity_version",
        "role",
        "model_id",
        "executor_profile",
        "capability_profile",
        "capability_profile_hash",
    }
)
_PHASE_KEYS: Final = frozenset({"model", "reasoning"})
_ANALYSIS_PHASE_KEYS: Final = _PHASE_KEYS | {"scope_planner_contract"}
_REASONING_KEYS: Final = frozenset({"addon_id", "slot", "model_id", "config"})
_PACKET_KEYS: Final = frozenset(
    {"messages", "tools", "response_format", "max_completion_tokens"}
)
_SLOTS: Final = {
    "translation": "translation",
    "analysis": "terminology_analysis",
    "research": "researcher",
}
_MAX_STRING_BYTES = PROVIDER_MESSAGE_MAX_BYTES
_MAX_PACKET_BYTES = 8 * 1024 * 1024
RESEARCH_PROVIDER_MESSAGE_MAX_BYTES = 4 * 1024 * 1024
RESEARCH_PROVIDER_PACKET_MAX_BYTES = _MAX_PACKET_BYTES
_MAX_MESSAGES = 256
_MAX_TOOLS = 128
_MAX_TOOL_CALLS = 128

_LOGGER = logging.getLogger(__name__)


def _fail(code: str) -> BlockContractError:
    return BlockContractError(code)


def _bounded_string(
    value: object, code: str, *, allow_none: bool = False
) -> str | None:
    if value is None and allow_none:
        return None
    if not isinstance(value, str) or len(value.encode("utf-8")) > _MAX_STRING_BYTES:
        raise _fail(code)
    return value


def _identity_string(value: object, code: str, *, maximum: int = 512) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or not value.isprintable()
        or len(value.encode("utf-8")) > maximum
    ):
        raise _fail(code)
    return value


def _bounded_integer(value: object, code: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise _fail(code)
    return value


def _optional_bounded_integer(value: object, code: str) -> int | None:
    if value is None:
        return None
    return _bounded_integer(value, code)


def _canonical_json(value: object, code: str, *, maximum: int) -> Any:
    """Validate JSON without accepting tuple/number/string coercions."""

    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        restored = json.loads(encoded)
    except (TypeError, ValueError, RecursionError) as exc:
        raise _fail(code) from exc
    if len(encoded) > maximum or restored != value:
        raise _fail(code)
    return restored


def _validate_model(model: object) -> dict[str, Any]:
    if isinstance(model, Mapping) and frozenset(model) == _EXTERNAL_MODEL_KEYS:
        if (
            model.get("identity_kind") != EXTERNAL_EXECUTOR_IDENTITY_KIND
            or model.get("identity_version") != EXTERNAL_EXECUTOR_IDENTITY_VERSION
            or model.get("role") != EXTERNAL_EXECUTOR_ROLE
            or model.get("model_id") != EXTERNAL_EXECUTOR_ROLE
        ):
            raise _fail("provider-external-identity")
        try:
            profile = normalize_executor_profile(model.get("executor_profile"))
        except (TypeError, ValueError) as exc:
            raise _fail("provider-external-identity") from exc
        encoded = json.dumps(
            profile.capability_profile(EXTERNAL_EXECUTOR_ROLE).to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        capability = json.loads(encoded)
        if not isinstance(capability, dict):
            raise _fail("provider-external-identity")
        if (
            model.get("capability_profile") != capability
            or model.get("capability_profile_hash")
            != hashlib.sha256(encoded).hexdigest()
        ):
            raise _fail("provider-external-identity")
        result = dict(model)
        result["capability_profile"] = capability
        return result
    if not isinstance(model, Mapping) or frozenset(model) not in {
        _MODEL_KEYS,
        _MODEL_KEYS | {"transport_identity"},
    }:
        raise _fail("provider-model-shape")
    result = dict(model)
    _identity_string(result["model_id"], "provider-model-id", maximum=255)
    for key in (
        "model_revision",
        "model_updated_at",
        "provider_name",
        "provider_kind",
        "api_key_created_at",
        "credential_source",
        "credential_witness",
    ):
        _identity_string(result[key], "provider-model-identity", maximum=4096)
    for key in ("model_record_id", "provider_id", "api_key_id"):
        _bounded_integer(result[key], "provider-model-identity")
    for key in (
        "model_owner_user_id",
        "provider_owner_user_id",
        "api_key_owner_user_id",
    ):
        _optional_bounded_integer(result[key], "provider-model-identity")
    profile = _canonical_json(
        result["capability_profile"],
        "provider-model-capabilities",
        maximum=_MAX_PACKET_BYTES,
    )
    if not isinstance(profile, dict):
        raise _fail("provider-model-capabilities")
    profile_hash = _identity_string(
        result["capability_profile_hash"],
        "provider-model-capabilities",
        maximum=64,
    )
    encoded = json.dumps(
        profile,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    if (
        len(profile_hash) != 64
        or any(character not in "0123456789abcdef" for character in profile_hash)
        or hashlib.sha256(encoded).hexdigest() != profile_hash
        or profile.get("model_id") != result["model_id"]
        or profile.get("provider_name") != result["provider_name"]
        or profile.get("provider_kind") != result["provider_kind"]
    ):
        raise _fail("provider-model-capabilities")
    result["capability_profile"] = profile
    if "transport_identity" in result:
        transport = _canonical_json(
            result["transport_identity"],
            "provider-model-transport",
            maximum=8_192,
        )
        if (
            not isinstance(transport, dict)
            or set(transport) != _MODEL_TRANSPORT_KEYS
            or transport.get("contract_version") != "translator-model-transport-v1"
            or transport.get("provider_kind") != result["provider_kind"]
        ):
            raise _fail("provider-model-transport")
        result["transport_identity"] = transport
    return result


def _validate_phase(
    phase: object, *, name: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    allowed_phase_keys = (
        {_PHASE_KEYS, _ANALYSIS_PHASE_KEYS} if name == "analysis" else {_PHASE_KEYS}
    )
    if not isinstance(phase, Mapping) or frozenset(phase) not in allowed_phase_keys:
        raise _fail("provider-phase-shape")
    if (
        "scope_planner_contract" in phase
        and phase.get("scope_planner_contract")
        not in SUPPORTED_ANALYSIS_SCOPE_PLANNER_CONTRACTS
    ):
        raise _fail("provider-analysis-planner")
    model = _validate_model(phase["model"])
    reasoning = phase["reasoning"]
    if not isinstance(reasoning, Mapping) or set(reasoning) != _REASONING_KEYS:
        raise _fail("provider-reasoning-shape")
    if reasoning["addon_id"] != REASONING_ADDON_ID:
        raise _fail("provider-reasoning-addon")
    expected_slot = _SLOTS.get(name)
    if reasoning["slot"] != expected_slot:
        raise _fail("provider-reasoning-slot")
    if reasoning["model_id"] != model["model_id"]:
        raise _fail("provider-reasoning-model")
    config = reasoning["config"]
    if model.get("identity_kind") == EXTERNAL_EXECUTOR_IDENTITY_KIND and config is not None:
        raise _fail("provider-external-reasoning")
    if config is not None:
        if not isinstance(config, Mapping) or set(config) != {"level"}:
            raise _fail("provider-reasoning-config")
        if config["level"] not in {
            "default",
            "off",
            "minimal",
            "low",
            "medium",
            "high",
            "xhigh",
        }:
            raise _fail("provider-reasoning-config")
        # Materialize only after validation so a caller cannot change the
        # nested mapping while the service is constructing its request.
        config = {"level": config["level"]}
    else:
        config = None
    return model, {
        "addon_id": REASONING_ADDON_ID,
        "slot": expected_slot,
        "model_id": model["model_id"],
        "config": config,
    }


def _messages(
    value: object, *, input_maximum: int = _MAX_STRING_BYTES
) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value or len(value) > _MAX_MESSAGES:
        raise _fail("provider-packet-messages")
    result: list[dict[str, Any]] = []
    for message in value:
        if not isinstance(message, dict):
            raise _fail("provider-packet-message")
        keys = set(message)
        role = message.get("role")
        content = message.get("content")
        maximum = input_maximum if role in {"system", "user"} else _MAX_STRING_BYTES
        if not isinstance(content, str) or len(content.encode("utf-8")) > maximum:
            raise _fail("provider-packet-message")
        if keys == {"role", "content"} and role in {
            "system",
            "user",
            "assistant",
        }:
            result.append({"role": role, "content": content})
            continue
        if keys == {"role", "content", "tool_calls"} and role == "assistant":
            calls = message["tool_calls"]
            if not isinstance(calls, list) or not calls or len(calls) > _MAX_TOOL_CALLS:
                raise _fail("provider-packet-message")
            result.append(
                {
                    "role": "assistant",
                    "content": content,
                    "tool_calls": [_tool_call(call) for call in calls],
                }
            )
            continue
        if keys == {"role", "content", "tool_call_id", "name"} and role == "tool":
            result.append(
                {
                    "role": "tool",
                    "content": content,
                    "tool_call_id": _identity_string(
                        message["tool_call_id"],
                        "provider-packet-message",
                        maximum=16 * 1024,
                    ),
                    "name": _identity_string(
                        message["name"],
                        "provider-packet-message",
                        maximum=512,
                    ),
                }
            )
            continue
        raise _fail("provider-packet-message")
    return result


def _tools(value: object, *, maximum: int = _MAX_PACKET_BYTES) -> list[Any] | None:
    if value is None:
        return None
    if not isinstance(value, list) or len(value) > _MAX_TOOLS:
        raise _fail("provider-packet-tools")
    return cast(
        list[Any],
        _canonical_json(value, "provider-packet-tools", maximum=maximum),
    )


def _packet(
    packet: object,
    *,
    message_maximum: int = _MAX_STRING_BYTES,
    packet_maximum: int = _MAX_PACKET_BYTES,
    allow_unbounded_output: bool = False,
) -> dict[str, Any]:
    if isinstance(packet, ProviderPacket):
        packet = {
            "messages": packet.to_dict()["messages"],
            "tools": None,
            "response_format": packet.to_dict()["response_format"],
            "max_completion_tokens": packet.requested_output_tokens,
        }
    if not isinstance(packet, Mapping) or set(packet) != _PACKET_KEYS:
        raise _fail("provider-packet-shape")
    messages = _messages(packet["messages"], input_maximum=message_maximum)
    tools = _tools(packet["tools"], maximum=packet_maximum)
    response_format = packet["response_format"]
    if response_format is not None:
        if not isinstance(response_format, dict):
            raise _fail("provider-packet-response-format")
        response_format = _canonical_json(
            response_format,
            "provider-packet-response-format",
            maximum=packet_maximum,
        )
    output_tokens = packet["max_completion_tokens"]
    if output_tokens is None and allow_unbounded_output:
        pass
    elif (
        not isinstance(output_tokens, int)
        or isinstance(output_tokens, bool)
        or output_tokens < 1
        or output_tokens > 1_000_000
    ):
        raise _fail("provider-packet-output-tokens")
    # Validate the complete packet after the field-level checks.  This also
    # rejects custom Mapping/list implementations that would be coerced.
    _canonical_json(
        {
            "messages": messages,
            "tools": tools,
            "response_format": response_format,
            "max_completion_tokens": output_tokens,
        },
        "provider-packet-json",
        maximum=packet_maximum,
    )
    return {
        "messages": messages,
        "tools": tools,
        "response_format": response_format,
        "max_completion_tokens": output_tokens,
    }


def _response_message(response: object) -> dict[str, Any]:
    choices = (
        response.get("choices")
        if isinstance(response, Mapping)
        else getattr(response, "choices", None)
    )
    if not isinstance(choices, list) or len(choices) != 1:
        raise _fail("provider-response-choices")
    choice = choices[0]
    message = (
        choice.get("message")
        if isinstance(choice, Mapping)
        else getattr(choice, "message", None)
    )
    if isinstance(message, Mapping):
        material = dict(message)
    else:
        material = {}
        for key in ("role", "content", "tool_calls"):
            if hasattr(message, key):
                material[key] = getattr(message, key)
    if set(material) not in ({"role", "content"}, {"role", "content", "tool_calls"}):
        raise _fail("provider-response-message")
    if material.get("role") != "assistant":
        raise _fail("provider-response-role")
    _bounded_string(
        material.get("content"), "provider-response-content", allow_none=True
    )
    calls = material.get("tool_calls")
    if calls is not None:
        if not isinstance(calls, list) or not calls or len(calls) > _MAX_TOOL_CALLS:
            raise _fail("provider-response-tool-calls")
        material["tool_calls"] = [_tool_call(call) for call in calls]
    return {
        "role": "assistant",
        "content": material["content"],
        **({"tool_calls": material["tool_calls"]} if calls is not None else {}),
    }


def _log_provider_trace(
    *,
    capture: Mapping[str, Any],
    phase: str,
    action_id: str,
) -> None:
    """Log content-free provider evidence useful for live workflow diagnosis."""

    provider_calls = capture.get("provider_calls")
    admission = capture.get("context_admission")
    if not isinstance(provider_calls, list) or not provider_calls:
        return
    safe_admission: dict[str, object] = {}
    if isinstance(admission, Mapping):
        profile = admission.get("profile")
        measurement = admission.get("measurement")
        safe_admission = {
            "admitted": admission.get("admitted"),
            "requested_output_tokens": admission.get("requested_output_tokens"),
            "available_input_tokens": admission.get("available_input_tokens"),
            "overflow_tokens": admission.get("overflow_tokens"),
            "profile": (
                {
                    key: profile.get(key)
                    for key in (
                        "model_id",
                        "max_input_tokens",
                        "max_output_tokens",
                        "usable_input_tokens",
                    )
                }
                if isinstance(profile, Mapping)
                else None
            ),
            "measurement": dict(measurement)
            if isinstance(measurement, Mapping)
            else None,
        }
    # Alembic's logging ``fileConfig`` disables loggers that already existed
    # unless explicitly named. The application can legitimately run migration
    # discovery before this provider call, so fall back to the root pipeline
    # rather than silently losing the content-free trace.
    trace_logger = _LOGGER if not _LOGGER.disabled else logging.getLogger()
    trace_logger.info(
        "Translator provider trace: %s",
        json.dumps(
            {
                "schema_version": "translator-provider-trace-v1",
                "phase": phase,
                "action_id": action_id,
                "context_admission": safe_admission,
                "provider_calls": provider_calls,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
    )


def _tool_call(value: object) -> dict[str, Any]:
    if isinstance(value, Mapping):
        call = dict(value)
    else:
        call = {}
        for key in ("id", "type", "function"):
            if hasattr(value, key):
                call[key] = getattr(value, key)
    if set(call) != {"id", "type", "function"} or call["type"] != "function":
        raise _fail("provider-response-tool-call")
    function_value = call["function"]
    if isinstance(function_value, Mapping):
        function = dict(function_value)
    else:
        function = {}
        for key in ("name", "arguments"):
            if hasattr(function_value, key):
                function[key] = getattr(function_value, key)
    if set(function) != {"name", "arguments"}:
        raise _fail("provider-response-tool-call")
    call_id = _identity_string(
        call["id"], "provider-response-tool-call-id", maximum=16 * 1024
    )
    name = _identity_string(
        function["name"], "provider-response-tool-name", maximum=512
    )
    arguments = function["arguments"]
    if (
        not isinstance(arguments, str)
        or len(arguments.encode("utf-8")) > _MAX_STRING_BYTES
    ):
        raise _fail("provider-response-tool-arguments")
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


def _call(
    service: Any,
    *,
    phase: object,
    packet: object,
    action_id: object,
    name: str,
) -> dict[str, Any]:
    if not hasattr(service, "completion") or not callable(service.completion):
        raise _fail("provider-service")
    action = _identity_string(action_id, "provider-action-id", maximum=256)
    model, reasoning, request = _validated_call_request(
        phase=phase,
        packet=packet,
        name=name,
    )
    if name in {"translation", "analysis"} and request["tools"] is not None:
        raise _fail(f"provider-{name}-tools")
    external = model.get("identity_kind") == EXTERNAL_EXECUTOR_IDENTITY_KIND
    if external:
        context = passive_execution_context()
        if (
            context is None
            or context.addon_id != "translator"
            or context.resource_kind != "project"
            or context.executor_profile.to_dict() != model["executor_profile"]
        ):
            # Fail closed before invoking LLMService: an external frozen
            # identity is valid only inside its matching passive MCP context.
            raise _fail("provider-external-context")
        config = None
    else:
        config = reasoning["config"] or {"level": "default"}
    completion_kwargs: dict[str, Any] = {
        "model": model["model_id"],
        "messages": request["messages"],
        "tools": request["tools"],
        "response_format": request["response_format"],
        "thinking_config": config,
        "source": {
            "translation": TRANSLATION_SOURCE,
            "analysis": ANALYSIS_SOURCE,
            "research": RESEARCH_SOURCE,
        }[name],
        "reasoning_addon_id": REASONING_ADDON_ID,
        "reasoning_slot": reasoning["slot"],
        "model_capability_overrides": model["capability_profile"],
        "context_admission_mode": "manual",
        "enforce_context_admission": True,
        "log_to_db": False,
        "cost_attribution": {"action_id": action},
    }
    if not external:
        completion_kwargs.update(
            {
                "provider_max_attempts": 1,
                "redact_provider_error_logs": True,
                "expected_api_key_id": model["api_key_id"],
            }
        )
    if request["max_completion_tokens"] is not None:
        completion_kwargs["max_completion_tokens"] = request["max_completion_tokens"]
    debug_capture: dict[str, Any] = {}
    completion_kwargs["debug_capture_sink"] = debug_capture
    completion_kwargs["debug_capture_include_content"] = False
    try:
        response = service.completion(**completion_kwargs)
    finally:
        _log_provider_trace(capture=debug_capture, phase=name, action_id=action)
    return _response_message(response)


def _validated_call_request(
    *, phase: object, packet: object, name: str
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    model, reasoning = _validate_phase(phase, name=name)
    request = _packet(
        packet,
        message_maximum=(
            RESEARCH_PROVIDER_MESSAGE_MAX_BYTES
            if name == "research"
            else _MAX_STRING_BYTES
        ),
        packet_maximum=(
            RESEARCH_PROVIDER_PACKET_MAX_BYTES
            if name == "research"
            else _MAX_PACKET_BYTES
        ),
        # Translator phases reserve output space during context admission, but
        # current provider calls deliberately omit a generation ceiling. A
        # reasoning model may otherwise consume the allowance before it emits
        # the strict answer contract.
        allow_unbounded_output=True,
    )
    return model, reasoning, request


def validate_research_provider_request(
    *, phase: object, packet: object
) -> dict[str, Any]:
    """Render the exact bounded request shape used by ``call_research``."""

    _model, reasoning, request = _validated_call_request(
        phase=phase,
        packet=packet,
        name="research",
    )
    return {
        **request,
        "thinking_config": reasoning["config"] or {"level": "default"},
    }


def call_translation_window(
    service: Any, *, phase: object, packet: object, action_id: object
) -> str:
    """Make one strict translation request and return only assistant text."""

    message = _call(
        service, phase=phase, packet=packet, action_id=action_id, name="translation"
    )
    if "tool_calls" in message:
        raise _fail("provider-translation-tool-call")
    content = message["content"]
    if not isinstance(content, str):
        raise _fail("provider-translation-content")
    return content


def call_analysis(
    service: Any, *, phase: object, packet: object, action_id: object
) -> dict[str, Any]:
    """Make one analysis request and return its exact assistant DTO."""

    message = _call(
        service, phase=phase, packet=packet, action_id=action_id, name="analysis"
    )
    if "tool_calls" in message:
        raise _fail("provider-analysis-tool-call")
    if not isinstance(message["content"], str):
        raise _fail("provider-analysis-content")
    return message


def call_research(
    service: Any, *, phase: object, packet: object, action_id: object
) -> dict[str, Any]:
    """Make one research request, retaining opaque provider tool arguments."""

    message = _call(
        service, phase=phase, packet=packet, action_id=action_id, name="research"
    )
    calls = message.get("tool_calls")
    if calls is None:
        calls = []
    conclusion = message["content"]
    if conclusion is None and not calls:
        raise _fail("provider-research-content")
    return {"conclusion": conclusion, "tool_calls": calls}


__all__ = [
    "ANALYSIS_SOURCE",
    "PROVIDER_DEFAULT_OUTPUT_POLICY",
    "REASONING_ADDON_ID",
    "RESEARCH_PROVIDER_MESSAGE_MAX_BYTES",
    "RESEARCH_PROVIDER_PACKET_MAX_BYTES",
    "RESEARCH_SOURCE",
    "TRANSLATION_SOURCE",
    "call_analysis",
    "call_research",
    "call_translation_window",
    "validate_research_provider_request",
]
