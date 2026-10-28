"""Execute witnessed Translator research calls through the shared policy kernel.

The persistence layer owns exact encrypted evidence and operation fences.  This
adapter owns only the live boundary between a durably prepared call and
``ToolExecutor``.  In particular, it commits the one-shot claim before any
add-on code runs and never retries an executing call.
"""

from __future__ import annotations

import copy
import json
from typing import Any

from carlina.core.tool_risk import tool_is_read_only

from ...core.execution_kernel import AgentEventSink, ToolAuthorityContext, ToolExecutor
from ...core.runtime_state import guard_tool_execution
from ...extensions import db
from .block_contract import BlockContractError, TranslationSourcePlan
from .models import TranslationResearchToolCall
from .research_persistence import (
    ResearchVerificationContext,
    block_research_tool_call,
    claim_research_tool_call,
    complete_research_tool_call,
    fail_research_tool_call,
    refresh_research_verification_context,
    rehydrate_research_tool_call,
)

_AVAILABLE_TOOL_STATES = frozenset({"active", "degraded"})
_MAX_ENVELOPE_BYTES = 2 * 1024 * 1024
_SAFE_EVENT_STATUSES = frozenset({"running", "warning", "completed", "error"})
_SAFE_EVENT_KINDS = frozenset(
    {
        "tool_call.started",
        "tool_call.awaiting_approval",
        "tool_call.awaiting_user",
        "runtime.self_nudge.scheduled",
        "tool_call.blocked",
        "tool_call.failed",
        "tool_call.completed",
    }
)


class _BoundedResearchEventSink(AgentEventSink):
    """Keep shared lifecycle facts while excluding private research content."""

    def __init__(self, destination: AgentEventSink):
        self._destination = destination

    def emit(self, event: dict[str, Any]) -> dict[str, Any]:
        packet = copy.deepcopy(event) if isinstance(event, dict) else {}
        body = packet.get("event") if packet.get("type") == "agent_event" else packet
        if isinstance(body, dict):
            # Shared events deliberately carry rich user-facing summaries and
            # previews. Research evidence is private and already retained in
            # the witnessed Translator tables, so project the host event to a
            # lifecycle-only allowlist instead of trying to blacklist every
            # content-bearing shape a tool result may introduce.
            kind = str(body.get("kind") or "")
            status = str(body.get("status") or "")
            safe_body = {
                "schema_version": 1,
                "id": body.get("id"),
                "kind": kind if kind in _SAFE_EVENT_KINDS else "tool_call.failed",
                "phase": "tool_call",
                "status": status if status in _SAFE_EVENT_STATUSES else "error",
                "title": "Translator research tool lifecycle",
                "summary": "Content retained in encrypted Translator research evidence.",
                "timestamp": body.get("timestamp"),
                "payload": {
                    "content_retention": "encrypted_translator_research",
                },
            }
            if packet.get("type") == "agent_event":
                packet = {"type": "agent_event", "event": safe_body}
            else:
                packet = safe_body
        return self._destination.emit(packet)


def _canonical_envelope(value: object) -> dict[str, Any]:
    """Prove that shared runtime output is safe to retain as exact evidence."""

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
        raise BlockContractError("research-executor-envelope") from exc
    if (
        not isinstance(restored, dict)
        or restored != value
        or len(encoded) > _MAX_ENVELOPE_BYTES
    ):
        raise BlockContractError("research-executor-envelope")
    return restored


def _drop_untrusted_result_refs(envelope: object) -> object:
    if not isinstance(envelope, dict):
        return envelope
    projected = copy.deepcopy(envelope)
    for key in (
        "data_refs",
        "artifact_refs",
        "source_refs",
        "evidence_refs",
    ):
        projected.pop(key, None)
    return projected


def _validate_executor_result(value: object) -> None:
    """Reject coercive shared-runtime serialization, including truncated output."""

    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        restored = json.loads(encoded)
    except (TypeError, ValueError, RecursionError) as exc:
        raise BlockContractError("research-executor-result") from exc
    if restored != value:
        raise BlockContractError("research-executor-result")


def _blocked_result(reason: str, **details: object) -> dict[str, Any]:
    return {
        "status": "blocked",
        "policy": "translator_research_read_only",
        "error": reason,
        **details,
    }


def execute_research_tool_call(
    call: TranslationResearchToolCall | str,
    *,
    addon_manager: Any,
    event_sink: AgentEventSink | None = None,
    timeout_seconds: float | None = None,
    session: Any | None = None,
    verified_plan: TranslationSourcePlan | None = None,
    verification_context: ResearchVerificationContext | None = None,
) -> TranslationResearchToolCall:
    """Claim, execute once, and witness one prepared read-only research call.

    Unexpected failures after the claim deliberately leave the row in
    ``executing``.  A later worker may reconcile it to ``uncertain`` only after
    proving operation-fence loss; it must never replay the external call.
    """

    active = db.session() if session is None else session
    call_id = call.id if isinstance(call, TranslationResearchToolCall) else call
    if not isinstance(call_id, str) or not call_id:
        raise BlockContractError("research-call-id")

    evidence = rehydrate_research_tool_call(
        call_id,
        session=active,
        verified_plan=verified_plan,
        verification_context=verification_context,
    )
    durable = active.get(TranslationResearchToolCall, call_id, populate_existing=True)
    if durable is None:
        raise BlockContractError("research-call-missing")
    if durable.state != "prepared" or evidence["argument_state"] != "parsed":
        raise BlockContractError("research-tool-not-prepared")

    declaration = evidence["declaration"]
    frozen_provider_declaration = declaration["declaration"]
    frozen_arguments = evidence["arguments"]
    provider_domain = declaration["provider_domain"]
    connection_mode = declaration["connection_mode"]
    connection_id = declaration["connection_id"]
    dispatch_arguments = {
        key: value
        for key, value in frozen_arguments.items()
        if key not in {"_approval_id", "_resource_reference_ids"}
    }
    approval_id = frozen_arguments.get("_approval_id")
    resource_reference_ids = frozen_arguments.get("_resource_reference_ids")
    provider_tool_call = {
        "id": durable.provider_call_id,
        "type": "function",
        "function": {
            "name": durable.tool_name,
            "arguments": evidence["raw_arguments"],
        },
    }
    scope = {
        "call_id": durable.id,
        "run_id": durable.run_id,
        "turn_id": durable.turn_id,
        "operation_id": durable.operation_id,
        "source_plan_id": durable.source_plan_id,
        "owner_user_id": durable.owner_user_id,
        "tool_name": durable.tool_name,
        "tool_id": durable.tool_id,
        "addon_id": durable.addon_id,
        "provider_id": durable.provider_id,
        "provider_domain": provider_domain,
    }
    live_config = {"current_user_id": scope["owner_user_id"]}
    if connection_mode == "saved":
        live_config["connection_id"] = connection_id

    # This is the last safe point before arbitrary add-on code.  Commit the
    # claim independently so a crash cannot make the call look replayable.
    claimed = claim_research_tool_call(
        call_id,
        session=active,
        verified_plan=verified_plan,
        verification_context=verification_context,
    )
    active.commit()
    if verification_context is not None:
        refresh_research_verification_context(
            claimed.run_id,
            verification_context=verification_context,
            session=active,
            verified_plan=verified_plan,
        )

    def availability_for_record(
        record: Any,
        arguments: dict[str, Any],
        *,
        expected_arguments: dict[str, Any],
    ) -> dict[str, Any] | None:
        if record is None:
            return _blocked_result("The frozen research tool is no longer registered.")
        status = getattr(getattr(record, "status", None), "state", None)
        enabled = getattr(addon_manager, "_dispatch_enabled_addons", None)
        if enabled is None:
            enabled = {
                *getattr(addon_manager, "loaded_addons", {}),
                *getattr(addon_manager, "plugins", {}),
            }
        if (
            record.id != scope["tool_id"]
            or record.addon_id != scope["addon_id"]
            or status not in _AVAILABLE_TOOL_STATES
            or record.addon_id not in enabled
        ):
            return _blocked_result("The live tool identity or availability changed.")
        if not tool_is_read_only(record):
            return _blocked_result("The live tool is no longer read-only.")
        if record.function_schema != frozen_provider_declaration:
            return _blocked_result(
                "The live tool schema changed after research was frozen."
            )
        if arguments != expected_arguments:
            return _blocked_result(
                "The executor arguments differ from frozen evidence."
            )
        return None

    def provider_availability(provider: Any) -> dict[str, Any] | None:
        if provider is None:
            return _blocked_result(
                "The frozen research provider is no longer registered."
            )
        identities = {
            str(getattr(provider, "id", "")),
            str(getattr(provider, "provider_id", "")),
            *{str(item) for item in (getattr(provider, "aliases", None) or [])},
        }
        status = getattr(getattr(provider, "status", None), "state", None)
        if (
            scope["provider_id"] not in identities
            or getattr(provider, "domain", None) != scope["provider_domain"]
            or getattr(provider, "addon_id", None) != scope["addon_id"]
            or status not in _AVAILABLE_TOOL_STATES
        ):
            return _blocked_result(
                "The live research provider identity or availability changed."
            )
        return None

    def connection_availability() -> dict[str, Any] | None:
        if connection_mode == "connectionless":
            return None
        try:
            addon_manager.validate_visible_connection_binding(
                connection_id=connection_id,
                owner_user_id=scope["owner_user_id"],
                connector_addon_id=scope["addon_id"],
            )
        except (
            KeyError,
            LookupError,
            PermissionError,
            RuntimeError,
            ValueError,
        ) as exc:
            return _blocked_result(
                "The frozen research connection is no longer ready and visible.",
                connection_error_type=type(exc).__name__,
            )
        return None

    def availability(name: str, arguments: dict[str, Any]) -> dict[str, Any] | None:
        blocked = availability_for_record(
            addon_manager.get_tool_record(name),
            arguments,
            expected_arguments=frozen_arguments,
        )
        if blocked:
            return blocked
        blocked = provider_availability(
            addon_manager.get_provider_record(
                scope["provider_domain"], scope["provider_id"]
            )
        )
        return blocked or connection_availability()

    def validate(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        validated = addon_manager.validate_registered_tool_call(name, arguments)
        if validated != dispatch_arguments:
            raise ValueError("Live validation changed the frozen research arguments.")
        return validated

    def dispatch_bound(
        name: str,
        arguments: dict[str, Any],
        _prepared: Any,
    ) -> dict[str, Any]:
        # Resolve once at the final invocation boundary, copy the mutable
        # registry record, and bind the add-on instance before rechecking the
        # complete policy. The raw manager path receives those exact objects
        # and performs no further name lookup.
        live_record = addon_manager.get_tool_record(name)
        record = copy.deepcopy(live_record) if live_record is not None else None
        blocked = availability_for_record(
            record,
            arguments,
            expected_arguments=dispatch_arguments,
        )
        if blocked:
            return {"action": "blocked", "result": blocked}
        live_provider = addon_manager.get_provider_record(
            scope["provider_domain"], scope["provider_id"]
        )
        provider = copy.deepcopy(live_provider) if live_provider is not None else None
        blocked = provider_availability(provider)
        if blocked:
            return {"action": "blocked", "result": blocked}
        blocked = connection_availability()
        if blocked:
            return {"action": "blocked", "result": blocked}
        try:
            validated = addon_manager.validate_registered_tool_call(name, arguments)
        except (KeyError, RuntimeError, TypeError, ValueError) as exc:
            return {
                "action": "blocked",
                "result": _blocked_result(
                    "Live validation rejected the frozen research call.",
                    validation_error_type=type(exc).__name__,
                ),
            }
        if validated != dispatch_arguments:
            return {
                "action": "blocked",
                "result": _blocked_result(
                    "Live validation changed the frozen research arguments."
                ),
            }
        guard = guard_tool_execution(
            record,
            arguments=validated,
            requester_user_id=scope["owner_user_id"],
            approval_id=approval_id if isinstance(approval_id, str) else None,
            resource_reference_ids=resource_reference_ids,
            audit_log_source_id="translator.research",
            log_source_ids=["translator.research"],
            runtime_policy={},
        )
        if not guard.allowed:
            return {"action": "blocked", "result": guard.to_tool_response()}
        instance = getattr(addon_manager, "loaded_addons", {}).get(
            scope["addon_id"]
        ) or getattr(addon_manager, "plugins", {}).get(scope["addon_id"])
        if instance is None:
            return {
                "action": "blocked",
                "result": _blocked_result("The frozen research add-on is not loaded."),
            }
        return {
            "action": "tool",
            "result": addon_manager._execute_registered_tool_record_raw(
                record,
                name,
                validated,
                live_config,
                instance=instance,
            ),
        }

    host_sink = event_sink or AgentEventSink(
        runtime_context={
            "role": "translator_research",
            "research_run_id": scope["run_id"],
            "research_turn_id": scope["turn_id"],
            "research_tool_call_id": scope["call_id"],
            "operation_id": scope["operation_id"],
        }
    )
    executor = ToolExecutor(
        dispatcher=dispatch_bound,
        event_sink=_BoundedResearchEventSink(host_sink),
        authority=ToolAuthorityContext(
            requester_user_id=scope["owner_user_id"],
            runtime_policy={},
            audit_log_source_id="translator.research",
            log_source_ids=("translator.research",),
        ),
        tool_record_resolver=addon_manager.get_tool_record,
        argument_validator=validate,
        availability_checker=availability,
        timeout_seconds=timeout_seconds,
        inline_execution=True,
    )

    executed = executor.execute_call(provider_tool_call)
    envelope = executed.result_envelope
    if envelope is None:
        raise RuntimeError("ToolExecutor returned no result envelope.")
    try:
        _validate_executor_result(executed.result)
        exact_envelope = _canonical_envelope(_drop_untrusted_result_refs(envelope))
    except BlockContractError as exc:
        terminal = fail_research_tool_call(
            claimed.id,
            {
                "reason": "tool_result_evidence_invalid",
                "contract_code": exc.code,
            },
            session=active,
            verified_plan=verified_plan,
            verification_context=verification_context,
        )
    else:
        blob_kwargs = (
            {"raw_result_blob": executed.result}
            if exact_envelope.get("truncated") is True
            else {}
        )
        if executed.action == "blocked":
            terminal = block_research_tool_call(
                claimed.id,
                {"reason": "tool_executor_blocked", "result_envelope": exact_envelope},
                **blob_kwargs,
                session=active,
                verified_plan=verified_plan,
                verification_context=verification_context,
            )
        elif executed.action == "error":
            terminal = fail_research_tool_call(
                claimed.id,
                {"reason": "tool_executor_failed", "result_envelope": exact_envelope},
                **blob_kwargs,
                session=active,
                verified_plan=verified_plan,
                verification_context=verification_context,
            )
        else:
            terminal = complete_research_tool_call(
                claimed.id,
                exact_envelope,
                **blob_kwargs,
                session=active,
                verified_plan=verified_plan,
                verification_context=verification_context,
            )
    active.commit()
    if verification_context is not None:
        refresh_research_verification_context(
            terminal.run_id,
            verification_context=verification_context,
            session=active,
            verified_plan=verified_plan,
        )
    return terminal


__all__ = ["execute_research_tool_call"]
