import contextlib
import inspect
import json
import queue
import re
import threading
import time
import uuid
from datetime import timedelta
from typing import Any

from flask import Response, current_app, jsonify, request, session, stream_with_context
from flask_login import current_user, login_user
from sqlalchemy.orm.attributes import flag_modified

from carlina.core.runtime_policy import (
    apply_runtime_policy_to_settings,
    clamp_tool_call_iterations,
)
from carlina.core.tool_risk import tool_is_read_only
from iarf_assistant.chat_builtin_tools import (
    CONTEXT_MANAGE_RESULTS_TOOL_DEF,
    CONTEXT_READ_RESULT_TOOL_DEF,
    CONTEXT_SEARCH_RESULT_TOOL_DEF,
    KNOWLEDGE_QUERY_TOOL_DEF,
    LIST_KNOWLEDGE_COLLECTIONS_TOOL_DEF,
    REQUEST_TOOL_BUDGET_EXTENSION_TOOL_DEF,
    SOURCE_COVERAGE_LEDGER_ROLE,
    get_source_coverage,
    handle_knowledge_query,
    handle_list_knowledge_collections,
    handle_manage_long_term_memory,
    handle_memorize_fact,
    handle_rag_search,
    handle_recall_fact,
    handle_request_scope_expansion,
    handle_request_self_nudge,
    handle_request_tool_budget_extension,
    handle_request_user_choice,
    update_working_memory,
    upsert_source_coverage,
    view_attachment,
)
from iarf_assistant.chat_context_store import (
    externalize_tool_result_message,
    read_tool_result_artifact_raw_content,
)
from iarf_assistant.chat_execution_helpers import (
    apply_tool_result_retention,
    build_context_pressure_messages,
    compact_tool_results_for_context_retry,
    compact_tool_results_for_pressure,
    demote_inline_this_turn_results,
    estimate_text_tokens,
    find_tool_result_message_by_ref,
    is_context_overflow_error,
    normalize_context_ref_ids,
    read_context_result_from_message,
    sanitize_llm_message,
    sanitize_messages_for_llm_context,
    search_context_result_in_message,
    serialize_tool_output,
    tool_result_limits_for_policy,
    unique_tool_declarations,
)
from iarf_assistant.chat_prompt_builder import (
    build_chat_messages_and_tools,
    build_user_instructions_context,
    resolve_chat_invocation,
)
from iarf_assistant.core.agent_events import (
    assistant_message_event,
    context_compaction_retry_event,
    context_history_projection_retry_event,
    emit_agent_event_packet,
    interaction_state_event,
    runtime_policy_event,
    tool_budget_extended_event,
    turn_completed_event,
)
from iarf_assistant.core.agent_loop import (
    final_text_or_empty_note,
    message_content_text,
)
from iarf_assistant.core.chat_turns.tool_loop import (
    ChatToolLoop,
    ChatToolLoopHooks,
    ChatToolResultContext,
    ChatToolResultProcessor,
)
from iarf_assistant.core.context_fabric import (
    ContextFabricError,
    ContextPolicyError,
    ContextVersionConflict,
    accept_chat_turn,
    checkpoint_turn_event,
    create_context_item,
    get_context_item,
    project_messages_for_provider,
    read_context_item_evidence,
    search_context_archive,
    transition_context_item,
)
from iarf_assistant.core.discovery_policy import (
    DiscoveryRoutingGuard,
    execute_meta_capability_with_policy,
)
from iarf_assistant.core.execution_kernel import (
    AgentEventSink,
    AgentLoopRunner,
    ContextAssembler,
    ToolAuthorityContext,
    ToolExecutor,
)
from iarf_assistant.core.execution_scope import ExecutionScope, string_list
from iarf_assistant.core.finalization_runner import (
    finalize_turn_deterministically,
    finalize_turn_with_model,
)
from iarf_assistant.core.model_router import ModelRouter
from iarf_assistant.core.prompt_telemetry import (
    record_context_retry,
    record_tool_executions,
    snapshot_turn_telemetry,
)
from iarf_assistant.core.run_orchestrator import (
    execute_run_tool,
    get_run_tool_declarations,
)
from iarf_assistant.core.runtime_contracts import tool_message_from_envelope
from iarf_assistant.core.runtime_data_store import externalize_runtime_payload
from iarf_assistant.core.runtime_interactions import (
    claim_runtime_interaction_resume,
    create_runtime_interaction_from_tool_result,
    finish_runtime_interaction_resume,
    get_runtime_interaction_for_user,
    interaction_resume_prompt,
    release_runtime_interaction_resume,
    replace_pending_tool_result,
)
from iarf_assistant.core.runtime_state import (
    describe_resource_references,
    merge_approved_scope_expansions,
)
from iarf_assistant.core.settings_cache import get_settings_snapshot
from iarf_assistant.core.turn_finalization import mark_turn_failed
from iarf_assistant.database_utils import (
    commit_with_retry,
    public_error_message,
)
from iarf_assistant.extensions import db
from iarf_assistant.llm_service import LLMService
from iarf_assistant.markdown import md_parser
from iarf_assistant.models import (
    ChatEvidence,
    ChatMemoryRevision,
    ChatMessage,
    ChatSession,
    ChatTurn,
    DataContextCollection,
    LLMCost,
    RuntimeApprovalRequest,
    User,
)
from iarf_assistant.prompts import JUNIOR_ASSISTANT_MANDATE_DEFAULT_PROMPT


def apply_rerun_memory_snapshot(chat_session: ChatSession, message_to_rerun: dict) -> bool:
    if not isinstance(message_to_rerun, dict) or "working_memory_before" not in message_to_rerun:
        return False
    chat_session.working_memory = message_to_rerun.get("working_memory_before")
    return True


def is_llm_visible_history_message(message: dict) -> bool:
    if not isinstance(message, dict):
        return False
    return message.get("role") not in {"debug_prompt", "agent_event", SOURCE_COVERAGE_LEDGER_ROLE, "runtime_monitor"}


def assistant_message_stream_packet(
    message: dict,
    *,
    loop_index: int | None = None,
) -> dict | None:
    content = message_content_text(message)
    if not content.strip():
        return None
    return {
        "type": "assistant_message",
        "response": md_parser(content),
        "markdown_content": content,
        "loop_index": loop_index,
        "intermediate": bool(message.get("tool_calls")) if isinstance(message, dict) else False,
    }


def terminal_housekeeping_source_messages(
    provider_messages: list[dict[str, Any]] | None,
    candidate_response: str,
) -> list[dict[str, Any]]:
    """Remove the provisional terminal answer before the housekeeping call.

    The candidate is supplied explicitly in the finalization payload. Keeping
    the same assistant message in the source transcript would waste context and
    can make the model treat a draft as an already-committed response.
    """
    rows = [
        message
        for message in (provider_messages or [])
        if isinstance(message, dict)
    ]
    candidate = str(candidate_response or "").strip()
    if not candidate:
        return rows
    for index in range(len(rows) - 1, -1, -1):
        message = rows[index]
        if str(message.get("role") or "").lower() != "assistant":
            continue
        if message.get("tool_calls"):
            continue
        if message_content_text(message).strip() == candidate:
            return [*rows[:index], *rows[index + 1:]]
    return rows


def normalize_context_item_ids(arguments) -> list[str]:
    """Accept plural context-item IDs and common singular aliases."""
    args = arguments if isinstance(arguments, dict) else {}
    values = []
    for key in (
        "context_item_ids",
        "context_item_id",
        "item_ids",
        "item_id",
    ):
        raw = args.get(key)
        if raw in (None, "", [], {}):
            continue
        if isinstance(raw, (list, tuple, set)):
            values.extend(raw)
        else:
            values.append(raw)
    normalized = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in normalized:
            normalized.append(text)
    return normalized


def should_emit_tool_context_event(result: Any) -> bool:
    status = (
        str(result.get("status") or "").lower()
        if isinstance(result, dict)
        else ""
    )
    return status not in {"waiting_for_user", "waiting_for_approval"}


def _capability_id_list(values) -> list[str]:
    return string_list(values)


def resolve_turn_capability_ids(assistant_mode: str, selected_capability_ids, active_capability_ids) -> list[str]:
    """Returns the capability ids visible to this chat turn."""
    return ExecutionScope.from_assistant_selection(
        {"mode": assistant_mode, "capability_ids": selected_capability_ids},
        active_capability_ids=active_capability_ids,
    ).capability_ids


def restrict_capability_ids_to_scope(requested_capability_ids, allowed_capability_ids) -> tuple[list[str], list[str]]:
    scope = ExecutionScope(mode="scoped", capability_ids=allowed_capability_ids)
    check = scope.check_child_request(capability_ids=requested_capability_ids)
    return check.capability_ids, check.blocked_capability_ids


def _scope_from_value(value) -> ExecutionScope:
    if isinstance(value, ExecutionScope):
        return value
    if isinstance(value, dict):
        return ExecutionScope.from_dict(value)
    return ExecutionScope(mode="scoped", capability_ids=value)


def enforce_direct_capability_scope(tool_name: str, arguments: dict | None, allowed_capability_ids) -> tuple[dict, dict | None]:
    """Blocks direct-mode run steps from adding unselected capabilities."""
    clean_args = dict(arguments or {})
    if tool_name != "runs_add_step":
        return clean_args, None
    scope = _scope_from_value(allowed_capability_ids)
    check = scope.check_child_request(
        capability_ids=clean_args.get("capability_ids"),
        skill_ids=clean_args.get("skill_ids"),
        tool_ids=clean_args.get("tool_ids"),
        tool_view_ids=clean_args.get("tool_view_ids"),
        resource_reference_ids=clean_args.get("resource_reference_ids"),
    )
    if not check.allowed:
        error = check.to_error()
        error["error"] = "Direct capability mode cannot use tools, skills, capabilities, or resources outside the user's selected scope."
        error["allowed_capability_ids"] = list(scope.capability_ids)
        error["allowed_skill_ids"] = list(scope.skill_ids)
        error["allowed_tool_ids"] = list(scope.tool_ids)
        error["allowed_tool_view_ids"] = list(scope.tool_view_ids)
        error["allowed_resource_reference_ids"] = list(scope.resource_reference_ids)
        return clean_args, error
    if "capability_ids" in clean_args:
        clean_args["capability_ids"] = check.capability_ids
    if "skill_ids" in clean_args:
        clean_args["skill_ids"] = check.skill_ids
    if "tool_ids" in clean_args:
        clean_args["tool_ids"] = check.tool_ids
    if "tool_view_ids" in clean_args:
        clean_args["tool_view_ids"] = check.tool_view_ids
    if "resource_reference_ids" in clean_args:
        clean_args["resource_reference_ids"] = check.resource_reference_ids
    return clean_args, None


def bind_run_tool_scope(tool_name: str, arguments: dict | None, execution_scope, runtime_policy: dict | None) -> dict:
    """Binds run creation/step metadata to the authoritative current turn scope."""
    clean_args = dict(arguments or {})
    if tool_name not in {
        "runs_create",
        "runs_compile",
        "runs_add_step",
        "runs_execute_python",
        "runs_code_session_create",
        "runs_project_workspace_create",
    }:
        return clean_args
    scope = _scope_from_value(execution_scope)
    metadata = clean_args.get("metadata") if isinstance(clean_args.get("metadata"), dict) else {}
    metadata = {
        **metadata,
        "execution_scope": scope.to_dict(),
    }
    if isinstance(runtime_policy, dict):
        metadata["runtime_policy"] = runtime_policy
    clean_args["metadata"] = metadata
    if tool_name in {
        "runs_create",
        "runs_compile",
        "runs_execute_python",
        "runs_code_session_create",
        "runs_project_workspace_create",
    } and isinstance(runtime_policy, dict):
        clean_args["max_tool_calls"] = clean_args.get("max_tool_calls") or runtime_policy.get("max_tool_calls")
    return clean_args


def _dynamic_tool_operation_group(declaration: dict) -> str:
    raw_name = str((declaration.get("function") or {}).get("name") or "")
    boundary_text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", raw_name)
    tokens = set(re.findall(r"[a-z0-9]+", boundary_text.lower()))
    if tokens & {"delete", "discard", "remove", "trash"}:
        return "delete"
    if tokens & {"publish", "send", "reply"}:
        return "publish"
    if tokens & {"create", "add", "draft", "upload", "copy", "import"}:
        return "create"
    if tokens & {
        "update",
        "edit",
        "patch",
        "set",
        "write",
        "move",
        "rename",
        "archive",
        "sync",
        "clean",
        "cleanup",
    }:
        return "update"
    if tokens & {"search", "find", "query"}:
        return "search"
    if tokens & {"list", "browse"}:
        return "list"
    if "preview" in tokens:
        return "preview"
    return "read"


def select_dynamic_tool_declarations(
    declarations,
    *,
    operation_groups=None,
    max_tools=6,
    schema_budget_tokens=4000,
) -> dict:
    """Select a narrow, budgeted tool group for progressive capability loading."""
    requested_groups = {
        str(group or "").strip().lower()
        for group in (operation_groups or [])
        if str(group or "").strip()
    }
    if not requested_groups:
        requested_groups = {"read"}
    valid_groups = {
        "read",
        "search",
        "list",
        "preview",
        "write",
        "create",
        "update",
        "publish",
        "delete",
        "all",
    }
    requested_groups &= valid_groups
    if not requested_groups:
        requested_groups = {"read"}

    try:
        tool_limit = max(1, min(int(max_tools or 6), 12))
    except (TypeError, ValueError):
        tool_limit = 6
    try:
        schema_budget = max(256, int(schema_budget_tokens or 4000))
    except (TypeError, ValueError):
        schema_budget = 4000

    read_groups = {"read", "search", "list", "preview"}
    write_groups = {"create", "update", "publish", "delete"}

    def requested(group: str) -> bool:
        if "all" in requested_groups:
            return True
        if "write" in requested_groups and group in write_groups:
            return True
        if "read" in requested_groups and group in read_groups:
            return True
        if "preview" in requested_groups and group == "read":
            return True
        return group in requested_groups

    filtered = [
        declaration
        for declaration in declarations or []
        if isinstance(declaration, dict)
        and requested(_dynamic_tool_operation_group(declaration))
    ]
    selected = []
    omitted_names = []
    used_schema_tokens = 0
    for declaration in filtered:
        name = (declaration.get("function") or {}).get("name")
        schema_tokens = estimate_text_tokens(
            json.dumps(declaration, ensure_ascii=True, sort_keys=True, default=str)
        )
        if len(selected) >= tool_limit or used_schema_tokens + schema_tokens > schema_budget:
            if name:
                omitted_names.append(name)
            continue
        selected.append(declaration)
        used_schema_tokens += schema_tokens

    return {
        "declarations": selected,
        "operation_groups": sorted(requested_groups),
        "omitted_tool_names": omitted_names,
        "schema_tokens": used_schema_tokens,
        "schema_budget_tokens": schema_budget,
        "tool_limit": tool_limit,
    }


def resolve_turn_tool_call_budget(
    configured_max: int,
    routing_metadata: dict[str, Any] | None,
) -> int:
    """Treat the configured policy value as a ceiling, not a per-turn target."""
    ceiling = clamp_tool_call_iterations(configured_max)
    metadata = routing_metadata if isinstance(routing_metadata, dict) else {}
    flags = (
        metadata.get("route_flags")
        if isinstance(metadata.get("route_flags"), dict)
        else {}
    )
    if flags.get("one_shot_code_intent"):
        return min(ceiling, 3)
    if not any(
        flags.get(name)
        for name in (
            "broad_task",
            "project_intent",
            "run_intent",
            "document_intent",
            "has_attachments",
        )
    ):
        return min(ceiling, 10)
    return ceiling


def _admission_result_dict(result: Any) -> dict[str, Any]:
    if hasattr(result, "to_dict"):
        value = result.to_dict()
        return dict(value) if isinstance(value, dict) else {}
    return dict(result) if isinstance(result, dict) else {}


def _admission_value(result: Any, key: str, default: Any = None) -> Any:
    if isinstance(result, dict):
        return result.get(key, default)
    return getattr(result, key, default)


def _history_projection_plan(
    admission_result: Any,
    *,
    compacted: list[dict[str, Any]],
    previous_budget: int | None = None,
) -> dict[str, Any]:
    measurement = _admission_value(admission_result, "measurement", {})

    def measurement_value(key: str) -> int:
        value = _admission_value(measurement, key, 0)
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    available_input = max(
        0,
        int(_admission_value(admission_result, "available_input_tokens", 0) or 0),
    )
    overflow_tokens = max(
        0,
        int(_admission_value(admission_result, "overflow_tokens", 0) or 0),
    )
    non_message_tokens = sum(
        measurement_value(key)
        for key in (
            "tool_schema_tokens",
            "response_schema_tokens",
            "extra_parameter_tokens",
            "provider_overhead_tokens",
        )
    )
    unreserved_history_budget = max(1, available_input - non_message_tokens)
    projection_margin = min(
        max(256, int(unreserved_history_budget * 0.05)),
        max(0, unreserved_history_budget - 1),
    )
    history_budget = max(1, unreserved_history_budget - projection_margin)
    if previous_budget is not None:
        history_budget = min(
            history_budget,
            max(1, int(previous_budget) - max(overflow_tokens, projection_margin)),
        )

    profile = _admission_value(admission_result, "profile")
    metadata_sources = _admission_value(profile, "metadata_sources", {})
    capacity_source = (
        metadata_sources.get("max_input_tokens", "unknown")
        if isinstance(metadata_sources, dict)
        else "unknown"
    )
    estimated_savings = sum(
        max(
            0,
            int(row.get("before_tokens") or 0)
            - int(row.get("after_tokens") or 0),
        )
        for row in compacted
        if isinstance(row, dict)
    )
    return {
        "mode": "history_projection",
        "available_input_tokens": available_input,
        "non_message_tokens": non_message_tokens,
        "projection_margin_tokens": projection_margin,
        "history_budget_tokens": history_budget,
        "overflow_tokens": overflow_tokens,
        "estimated_tool_compaction_savings": estimated_savings,
        "compacted_count": len(compacted),
        "capacity_source": capacity_source,
        "capacity_source_label": (
            "Provisional fallback (unknown)"
            if capacity_source == "conservative_fallback"
            else "Resolved model capability"
        ),
        "confidence": _admission_value(profile, "confidence", "unknown") or "unknown",
        "raw_messages_preserved": True,
        "omission_is_provider_projection": True,
        "omitted_message_count": 0,
        "omitted_reasons": [],
    }


def complete_with_context_retry(
    llm_service,
    *,
    model: str,
    messages: list[dict],
    make_provider_messages,
    retry_state: dict | None = None,
    emit_agent_event=None,
    stream_status=None,
    raw_content_loader=None,
    on_context_compacted=None,
    on_history_projected=None,
    on_manual_overflow=None,
    loop_index: int | None = None,
    **completion_kwargs,
):
    """Adapt a context overflow before making at most one provider retry."""
    state = retry_state if isinstance(retry_state, dict) else {}
    try:
        return llm_service.completion(
            model=model,
            messages=make_provider_messages(),
            **completion_kwargs,
        )
    except Exception as exc:
        if not is_context_overflow_error(exc):
            raise
        admission_result = getattr(exc, "result", None)
        requested_mode = str(
            completion_kwargs.get("context_admission_mode")
            or completion_kwargs.get("context_stewardship")
            or "assisted"
        ).strip().lower()
        result_mode = str(_admission_value(admission_result, "mode", "")).lower()
        if requested_mode in {"manual", "off", "disabled"} or result_mode == "manual":
            if state.get("manual_overflow_used") or not callable(on_manual_overflow):
                raise
            manual_admission = admission_result
            if manual_admission is None:
                manual_admission = _admission_result_dict(
                    completion_kwargs.get("admission_sink")
                )
                manual_admission.update({
                    "admitted": False,
                    "mode": "manual",
                    "provider_reported_overflow": True,
                    "reason": (
                        "The provider reported that the rendered Manual-mode request "
                        "exceeded its context limit after local admission. No saved "
                        "input was compacted or projected."
                    ),
                })
            state["manual_overflow_used"] = True
            state["manual_overflow_admission"] = _admission_result_dict(
                manual_admission
            )
            if callable(stream_status):
                stream_status(
                    "Context limit hit in Manual mode; preparing a bounded "
                    "assistant explanation without changing the saved input..."
                )
            return on_manual_overflow(
                manual_admission,
                loop_index=loop_index,
            )
        if state.get("used"):
            raise
        compacted = compact_tool_results_for_context_retry(
            messages,
            raw_content_loader=raw_content_loader,
        )
        if not compacted and admission_result is None:
            raise
        if compacted and callable(on_context_compacted):
            updated_tools = on_context_compacted(compacted)
            if updated_tools is not None:
                completion_kwargs["tools"] = updated_tools
        state["compacted_refs"] = compacted

        # First let the real local admission gate evaluate the final compacted
        # message/tool surface. A structured failure proves no provider call was
        # made, so history projection can continue without spending the single
        # provider retry.
        if compacted:
            try:
                result = llm_service.completion(
                    model=model,
                    messages=make_provider_messages(),
                    **completion_kwargs,
                )
            except Exception as compacted_error:
                next_admission = getattr(compacted_error, "result", None)
                if not is_context_overflow_error(compacted_error) or next_admission is None:
                    raise
                admission_result = next_admission
                exc = compacted_error
            else:
                state["used"] = True
                if callable(stream_status):
                    stream_status("Context limit hit; compacted tool results and retried once.")
                if callable(emit_agent_event):
                    emit_agent_event(context_compaction_retry_event(
                        compacted_count=len(compacted),
                        error=str(exc),
                        loop_index=loop_index,
                        compacted_refs=compacted[:20],
                    ))
                record_context_retry(completion_kwargs.get("telemetry_sink"))
                return result

        if not callable(on_history_projected):
            raise

        previous_budget = None
        last_error = exc
        for local_attempt in range(1, 7):
            projection_facts = _history_projection_plan(
                admission_result,
                compacted=compacted,
                previous_budget=previous_budget,
            )
            projection_facts["local_admission_attempt"] = local_attempt
            history_budget = int(projection_facts["history_budget_tokens"])
            callback_facts = on_history_projected(
                history_budget,
                admission_result,
                projection_facts,
            )
            if callback_facts is False:
                raise last_error
            if isinstance(callback_facts, dict):
                projection_facts.update(callback_facts)
            telemetry_sink = completion_kwargs.get("telemetry_sink")
            if isinstance(telemetry_sink, dict):
                telemetry_sink["context_history_projection"] = dict(projection_facts)
            state["history_projection"] = dict(projection_facts)
            previous_budget = history_budget
            try:
                result = llm_service.completion(
                    model=model,
                    messages=make_provider_messages(),
                    **completion_kwargs,
                )
            except Exception as projected_error:
                next_admission = getattr(projected_error, "result", None)
                if not is_context_overflow_error(projected_error) or next_admission is None:
                    raise
                admission_result = next_admission
                last_error = projected_error
                continue

            if isinstance(telemetry_sink, dict):
                completed_projection = telemetry_sink.get(
                    "context_history_projection"
                )
                if isinstance(completed_projection, dict):
                    projection_facts.update(completed_projection)
            state["history_projection"] = dict(projection_facts)
            state["used"] = True
            if callable(stream_status):
                stream_status(
                    "Context limit hit; projected bounded chat history and retried once."
                )
            if callable(emit_agent_event):
                emit_agent_event(context_history_projection_retry_event(
                    history_projection=projection_facts,
                    error=str(last_error),
                    loop_index=loop_index,
                ))
            record_context_retry(completion_kwargs.get("telemetry_sink"))
            return result
        raise last_error


def build_manual_overflow_recovery_messages(
    admission_result,
    *,
    evidence_id: str | None = None,
    context_item_id: str | None = None,
    content_hash: str | None = None,
) -> list[dict[str, Any]]:
    """Build a bounded, content-free request for Manual overflow recovery."""
    result = (
        admission_result.to_dict()
        if hasattr(admission_result, "to_dict")
        else dict(admission_result or {})
    )
    measurement = (
        result.get("measurement")
        if isinstance(result.get("measurement"), dict)
        else {}
    )
    profile = (
        result.get("profile")
        if isinstance(result.get("profile"), dict)
        else {}
    )
    contributors = []
    for contributor in (result.get("overflow_manifest") or [])[:12]:
        if not isinstance(contributor, dict):
            continue
        contributors.append({
            "kind": str(contributor.get("kind") or "")[:64],
            "identifier": str(contributor.get("identifier") or "")[:160],
            "tokens": int(contributor.get("tokens") or 0),
            "required": bool(contributor.get("required")),
            "disposition": str(contributor.get("disposition") or "")[:80],
        })
    manifest = {
        "status": "manual_context_overflow",
        "policy": {
            "mode": "manual",
            "raw_input_preserved": True,
            "automatic_summary": False,
            "automatic_archive": False,
            "automatic_exclusion": False,
            "automatic_clipping": False,
        },
        "evidence": {
            "evidence_id": evidence_id,
            "context_item_id": context_item_id,
            "content_hash": content_hash,
        },
        "model": {
            "model_id": profile.get("model_id") or measurement.get("model_id"),
            "usable_input_tokens": result.get("available_input_tokens"),
            "requested_output_tokens": result.get("requested_output_tokens"),
        },
        "measurement": {
            "total_input_tokens": measurement.get("total_input_tokens"),
            "message_tokens": measurement.get("message_tokens"),
            "tool_schema_tokens": measurement.get("tool_schema_tokens"),
            "response_schema_tokens": measurement.get("response_schema_tokens"),
            "provider_overhead_tokens": measurement.get("provider_overhead_tokens"),
            "overflow_tokens": result.get("overflow_tokens"),
        },
        "largest_contributors": contributors,
        "allowed_next_steps": [
            "Retry with a model whose usable context window is large enough.",
            "Ask the user to select exact messages, sections, ranges, or attachments.",
            "Use explicit full-source processing for one document or email thread.",
            "Use exhaustive coverage with durable notes for a source set that cannot fit in one call.",
        ],
    }
    return [
        {
            "role": "system",
            "content": (
                "You are handling a context-admission refusal in literal Manual "
                "mode. The original request was stored durably but was not sent "
                "to the provider, so do not claim to have read or analyzed its "
                "body. Write a concise, helpful response to the user explaining "
                "the measured limit and the available explicit choices. Mention "
                "the evidence/context identifier so the work can be resumed. Do "
                "not call tools, invent contents, silently choose a lossy option, "
                "or say that anything was summarized, clipped, or archived."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(manifest, ensure_ascii=False, sort_keys=True),
        },
    ]


def enforce_turn_tool_availability(tool_name: str, available_tool_names, execution_scope=None) -> dict | None:
    """Blocks any model-emitted tool call that was not declared for the current turn."""
    available = string_list(available_tool_names)
    if str(tool_name or "") in set(available):
        return None
    scope = _scope_from_value(execution_scope) if execution_scope is not None else ExecutionScope.autonomous()
    message = (
        "This tool is not available in the current Scoped Tools turn. Use the selected tools only, "
        "or call request_scope_expansion if the missing capability is required."
        if scope.is_scoped
        else "This tool is not declared for the current turn. Use capability discovery/load tools first if a new tool is required."
    )
    return {
        "status": "blocked",
        "policy": "tool_not_declared_for_turn",
        "blocked_tool": str(tool_name or ""),
        "error": message,
        "current_scope": scope.to_dict(),
        "available_tool_names": available[:80],
    }


def stream_chat_response():
    """Handles chat messages using an asynchronous event queue and isolated worker context."""
    app = current_app._get_current_object()
    if hasattr(app, 'addon_manager_error') or not hasattr(app, 'addon_manager'):
        return jsonify({"error": "Addon manager not initialized."}), 503

    data = request.json or {}
    prompt = data.get('prompt')
    session_id = data.get('session_id')
    requested_turn_id = str(data.get("turn_id") or "").strip() or None
    rerun_message_index = data.get('rerun_message_index')
    rag_collection_ids = data.get('rag_collection_ids')
    verbose = data.get('verbose', False)
    model_name = data.get('model', 'gemini-2.5-pro')
    assistant_config = data.get('assistant', {'mode': 'default'})
    files_from_client = data.get('files', [])
    thinking_config = data.get('thinking_config')
    capture_debug_prompt = bool(data.get('capture_debug_prompt'))
    runtime_policy_override = data.get('runtime_policy') or data.get('policy')
    authorized_context_item_ids = string_list(
        data.get("authorized_context_item_ids")
    )
    resume_interaction_id = str(data.get('resume_interaction_id') or '').strip() or None
    uid = current_user.id

    if requested_turn_id:
        try:
            requested_turn_id = str(uuid.UUID(requested_turn_id))
        except (TypeError, ValueError, AttributeError):
            return jsonify({"error": "turn_id must be a valid UUID."}), 400

    if resume_interaction_id:
        if not session_id:
            return jsonify({"error": "session_id is required to resume an interaction."}), 400
        try:
            interaction_preview = get_runtime_interaction_for_user(
                resume_interaction_id,
                requester_user_id=uid,
                chat_session_id=session_id,
            )
        except KeyError as exc:
            return jsonify({"error": str(exc)}), 404
        if interaction_preview.status not in {"resolved", "resuming"}:
            return jsonify({
                "error": (
                    f"Runtime interaction '{resume_interaction_id}' cannot resume "
                    f"from state '{interaction_preview.status}'."
                )
            }), 409
        resume_context = (
            interaction_preview.resume_payload
            if isinstance(interaction_preview.resume_payload, dict)
            else {}
        )
        model_name = resume_context.get("model") or model_name
        assistant_config = resume_context.get("assistant") or assistant_config
        runtime_policy_override = resume_context.get("runtime_policy") or runtime_policy_override
        thinking_config = resume_context.get("thinking_config") or thinking_config
        verbose = bool(resume_context.get("verbose", verbose))
        capture_debug_prompt = bool(resume_context.get("capture_debug_prompt", capture_debug_prompt))
        authorized_context_item_ids = string_list(
            resume_context.get("authorized_context_item_ids")
            or authorized_context_item_ids
        )
        files_from_client = []
        rerun_message_index = None
        prompt = interaction_resume_prompt(interaction_preview)

    if not prompt and not files_from_client and rerun_message_index is None:
        return jsonify({"error": "Prompt cannot be empty."}), 400

    u_instructions = build_user_instructions_context(
        current_user.assistant_instructions,
        current_user.addon_settings,
    )
    app_obj = app
    encryption_key = session.get('encryption_key')
    settings = get_settings_snapshot()
    settings, runtime_policy = apply_runtime_policy_to_settings(settings, runtime_policy_override)
    normalized_assistant = app_obj.addon_manager.normalize_assistant_selection(assistant_config)
    assistant_mode = normalized_assistant['mode']
    selected_capability_ids = normalized_assistant['capability_ids']
    enable_delegation = normalized_assistant['enable_delegation']

    stream_q = queue.Queue()

    def chief_worker():
        """The master background worker that executes the entire Chief Agent loop."""
        nonlocal session_id, prompt, files_from_client, selected_capability_ids
        turn_started_at = time.perf_counter()
        if not isinstance(selected_capability_ids, list):
            selected_capability_ids = list(selected_capability_ids or [])
        with app_obj.app_context(), app_obj.test_request_context():
            user_record = db.session.get(User, uid)
            if user_record:
                login_user(user_record)

            session['encryption_key'] = encryption_key
            session['session_id'] = session_id

            resumed_interaction = None
            turn_id = None
            context_fabric_turn = None
            accepted_user_message_id = None
            try:
                create_chat_session = not session_id
                if create_chat_session:
                    if rerun_message_index is not None:
                        stream_q.put({"type": "error", "message": "Cannot rerun a message in a new chat."})
                        return
                    session_id = str(uuid.uuid4())
                    chat_session = ChatSession(id=session_id, user_id=uid)
                else:
                    chat_session = db.session.get(ChatSession, session_id)
                    if not chat_session or chat_session.user_id != uid:
                        stream_q.put({"type": "error", "message": "Chat session not found."})
                        return
                session['session_id'] = session_id

                if resume_interaction_id:
                    resumed_interaction = claim_runtime_interaction_resume(
                        resume_interaction_id,
                        requester_user_id=uid,
                        chat_session_id=chat_session.id,
                    )
                
                turn_assistant_mode = assistant_mode
                normalized_assistant_for_turn = normalized_assistant
                applied_scope_expansions = []
                if turn_assistant_mode == "direct":
                    expanded_selection, applied_scope_expansions = merge_approved_scope_expansions(
                        {**normalized_assistant, "mode": turn_assistant_mode},
                        chat_session_id=chat_session.id,
                        requester_user_id=uid,
                    )
                    if applied_scope_expansions:
                        normalized_assistant_for_turn = app_obj.addon_manager.normalize_assistant_selection(expanded_selection)
                        turn_assistant_mode = normalized_assistant_for_turn["mode"]

                turn_execution_scope = ExecutionScope.from_assistant_selection(
                    {**normalized_assistant_for_turn, "mode": turn_assistant_mode},
                    active_capability_ids=chat_session.active_capability_ids,
                )
                selected_capability_ids = list(turn_execution_scope.capability_ids)

                working_memory_before_turn = chat_session.working_memory
                if create_chat_session:
                    message_rows = []
                    messages = []
                else:
                    message_rows = chat_session.messages.all()
                    messages = [msg.message for msg in message_rows]
                if resumed_interaction:
                    replaced_message = replace_pending_tool_result(messages, resumed_interaction)
                    if replaced_message is None:
                        raise ValueError(
                            "The suspended tool result could not be found in this chat. "
                            "The interaction remains available to retry."
                        )
                    replaced_index = next(
                        (
                            index
                            for index, message in enumerate(messages)
                            if message is replaced_message
                        ),
                        None,
                    )
                    if replaced_index is None:
                        raise ValueError(
                            "The suspended tool result could not be mapped back to durable chat history."
                        )
                    message_rows[replaced_index].message = replaced_message
                    flag_modified(message_rows[replaced_index], "message")

                if rerun_message_index is not None:
                    message_to_rerun = messages[rerun_message_index]
                    apply_rerun_memory_snapshot(chat_session, message_to_rerun)
                    delete_start_index = rerun_message_index
                    if (
                        rerun_message_index > 0
                        and isinstance(messages[rerun_message_index - 1], dict)
                        and messages[rerun_message_index - 1].get('role') == 'debug_prompt'
                    ):
                        delete_start_index = rerun_message_index - 1

                    messages_to_delete = chat_session.messages.offset(delete_start_index).all()
                    for msg in messages_to_delete:
                        db.session.delete(msg)
                    db.session.commit()
                    working_memory_before_turn = chat_session.working_memory
                    messages = messages[:delete_start_index]

                    content_to_rerun = message_to_rerun.get('content')
                    if isinstance(content_to_rerun, list):
                        prompt = next((part.get('text') for part in content_to_rerun if part.get('type') == 'text'), '')
                    else:
                        prompt = content_to_rerun
                    files_from_client = message_to_rerun.get('files', [])

                original_prompt = None if resumed_interaction else prompt
                approval_resolved_id = None
                approval_tool_id = None

                if not resumed_interaction and isinstance(prompt, str):
                    prompt_lower = prompt.lower()
                    is_approval = False
                    if "approve" in prompt_lower or "approved" in prompt_lower:
                        if not any(neg in prompt_lower for neg in ["don't approve", "do not approve", "disapprove", "reject", "cancel"]):
                            is_approval = True
                    
                    if is_approval:
                        import re
                        uuid_match = re.search(r'([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})', prompt_lower)
                        target_request = None
                        if uuid_match:
                            req_id = uuid_match.group(1)
                            target_request = RuntimeApprovalRequest.query.filter_by(id=req_id, status='pending').first()
                        else:
                            # Fetch the most recent pending approval request for this user
                            target_request = RuntimeApprovalRequest.query.filter_by(
                                requester_user_id=uid,
                                status='pending'
                            ).order_by(RuntimeApprovalRequest.created_at.desc()).first()

                        if target_request:
                            try:
                                from iarf_assistant.core.runtime_state import (
                                    resolve_approval_request,
                                )
                                resolve_approval_request(target_request.id, 'approved', approver_user_id=uid)
                                approval_resolved_id = target_request.id
                                approval_tool_id = target_request.tool_id
                            except Exception as e:
                                app_obj.logger.error(f"Failed to auto-resolve approval request: {e}")

                if approval_resolved_id:
                    prompt = f"{prompt}\n\n(System Note: The approval request with ID '{approval_resolved_id}' for tool '{approval_tool_id}' has been successfully approved by the user. You must proceed by retrying the tool execution now, and you MUST include `\"_approval_id\": \"{approval_resolved_id}\"` in the tool arguments.)"

                turn_id = requested_turn_id or str(uuid.uuid4())
                latest_memory_revision = (
                    ChatMemoryRevision.query
                    .filter_by(session_id=session_id)
                    .order_by(ChatMemoryRevision.revision.desc())
                    .first()
                )
                user_message_for_llm = {'role': 'user', 'content': prompt}
                user_message_to_save = (
                    None
                    if resumed_interaction
                    else {
                        'role': 'user',
                        'content': original_prompt,
                        'files': files_from_client,
                    }
                )
                accepted_turn = accept_chat_turn(
                    session_id=session_id,
                    user_id=uid,
                    model_id=model_name,
                    context_stewardship=runtime_policy.get("context_stewardship"),
                    turn_id=turn_id,
                    base_memory_revision=(
                        latest_memory_revision.revision
                        if latest_memory_revision is not None
                        else 0
                    ),
                    user_message=user_message_to_save,
                    create_session_if_missing=create_chat_session,
                )
                accepted_user_message_id = accepted_turn.user_message_id
                user_evidence = (
                    db.session.get(ChatEvidence, accepted_turn.evidence_id)
                    if accepted_turn.evidence_id
                    else None
                )
                context_fabric_turn = db.session.get(
                    ChatTurn,
                    accepted_turn.turn_id,
                )
                chat_session = db.session.get(
                    ChatSession,
                    accepted_turn.session_id,
                )
                if context_fabric_turn is None or chat_session is None:
                    raise ContextFabricError(
                        "The accepted chat turn could not be reloaded."
                    )
                if user_message_to_save is not None and user_evidence is not None:
                    user_message_to_save["_context_fabric"] = {
                        "schema_version": 2,
                        "turn_id": turn_id,
                        "evidence_id": user_evidence.id,
                        "content_hash": user_evidence.content_hash,
                        "accepted_before_execution": True,
                    }

                stream_q.put({
                    "type": "turn_accepted",
                    "message": "Request saved.",
                    "session_id": session_id,
                    "turn_id": turn_id,
                    "user_message_id": accepted_user_message_id,
                })
                if accepted_turn.idempotent:
                    if (
                        context_fabric_turn.status == "finalized"
                        and context_fabric_turn.assistant_message_id
                    ):
                        assistant_row = db.session.get(
                            ChatMessage,
                            context_fabric_turn.assistant_message_id,
                        )
                        assistant_payload = (
                            assistant_row.message
                            if assistant_row is not None
                            and isinstance(assistant_row.message, dict)
                            else {}
                        )
                        stream_q.put({
                            "type": "final",
                            "response": md_parser(
                                str(assistant_payload.get("content") or "")
                            ),
                            "session_id": session_id,
                            "usage_metadata": {},
                            "cost": "0.000000",
                            "telemetry": {
                                "idempotent_replay": True,
                            },
                        })
                        return
                    if context_fabric_turn.status == "failed":
                        error_payload = (
                            context_fabric_turn.error_json
                            if isinstance(
                                context_fabric_turn.error_json,
                                dict,
                            )
                            else {}
                        )
                        stream_q.put({
                            "type": "error",
                            "message": public_error_message(
                                error_payload.get("message")
                                or "The saved turn previously failed."
                            ),
                        })
                        return
                    stream_q.put({
                        "type": "error",
                        "message": (
                            "This request was already accepted and is still "
                            "being processed."
                        ),
                    })
                    return
                stream_q.put({"type": "status", "message": "Analyzing request..."})

                start_costs = db.session.query(LLMCost).filter_by(session_id=session_id).all()
                start_cost_val = sum(c.cost for c in start_costs)
                start_prompt_val = sum(c.prompt_tokens for c in start_costs)
                start_comp_val = sum(c.completion_tokens for c in start_costs)
                start_total_val = sum(c.total_tokens for c in start_costs)

                llm_service = LLMService(
                    user_id=uid,
                    session_id=session_id,
                    cost_attribution={"turn_id": turn_id},
                )
                turn_telemetry = {}
                invocation = resolve_chat_invocation(prompt, app_obj.addon_manager)
                if invocation.error:
                    mark_turn_failed(
                        turn_id,
                        invocation.error,
                        details={"phase": "invocation"},
                        commit=True,
                    )
                    stream_q.put({"type": "error", "message": invocation.error})
                    return
                user_message_for_llm['content'] = invocation.user_content
                invoked_skill_id = invocation.skill_id
                invoked_skill_instructions = invocation.skill_instructions
                invoked_workflow_context = invocation.workflow_context
                invoked_run_context = invocation.run_context

                config = {
                    "verbose": verbose,
                    "current_user_id": uid,
                    "db_session": db.session,
                    "rag_service": app_obj.rag_service,
                    "default_fast_model": settings.get('default_fast_model'),
                    "default_medium_model": settings.get('default_medium_model'),
                    "default_powerful_model": settings.get('default_powerful_model'),
                    "current_model": model_name,
                    "app_root_path": app_obj.root_path,
                    "execution_scope": turn_execution_scope.to_dict(),
                    "applied_scope_expansions": applied_scope_expansions,
                    "runtime_policy": runtime_policy,
                }
                if turn_execution_scope.resource_reference_ids:
                    config["selected_resource_references"] = describe_resource_references(
                        turn_execution_scope.resource_reference_ids,
                        owner_user_id=uid,
                    )
                config.update(settings)

                max_tool_calls_str = settings.get('max_tool_calls', '30')
                max_tool_calls = int(max_tool_calls_str) if max_tool_calls_str.isdigit() else 30
                max_tool_calls = max(1, max_tool_calls)
                try:
                    chat_tool_budget_extension_max = int(settings.get('chat_tool_budget_extension_max', '4'))
                except (TypeError, ValueError):
                    chat_tool_budget_extension_max = 4
                chat_tool_budget_extension_max = max(0, min(chat_tool_budget_extension_max, 12))
                tool_budget_extension_used = False

                user_record = db.session.get(User, uid)
                user_addon_settings = user_record.addon_settings or {}
                for addon_id, addon_settings in user_addon_settings.items():
                    for key, value in addon_settings.items():
                        config[f"{addon_id}_{key}"] = value

                new_messages_from_turn = []
                durable_event_ids: set[str] = set()

                def emit_agent_event(event_packet):
                    emit_agent_event_packet(
                        event_packet,
                        stream_put=stream_q.put,
                        chat_messages=new_messages_from_turn,
                        logger=app_obj.logger,
                    )
                    event = (
                        event_packet.get("event")
                        if isinstance(event_packet, dict)
                        and isinstance(event_packet.get("event"), dict)
                        else {}
                    )
                    event_id = str(event.get("id") or "").strip()
                    if not event_id or not turn_id:
                        return
                    try:
                        checkpointed = checkpoint_turn_event(
                            turn_id,
                            event_packet,
                            commit=True,
                        )
                        if checkpointed is not None:
                            durable_event_ids.add(event_id)
                    except Exception:
                        db.session.rollback()
                        app_obj.logger.warning(
                            "Could not checkpoint chat progress event %s for turn %s.",
                            event_id,
                            turn_id,
                            exc_info=True,
                        )

                runtime_event_sink = AgentEventSink(
                    persist=emit_agent_event,
                    runtime_context={
                        "role": "chief",
                        "chat_session_id": session_id,
                        "turn_id": turn_id,
                    },
                )

                def emit_assistant_output(message, *, loop_index=None):
                    stream_packet = assistant_message_stream_packet(
                        message,
                        loop_index=loop_index,
                    )
                    if stream_packet is not None:
                        stream_q.put(stream_packet)
                    emit_agent_event(assistant_message_event(message, loop_index=loop_index))

                emit_agent_event(runtime_policy_event(runtime_policy))
                if resumed_interaction:
                    emit_agent_event(interaction_state_event(
                        resumed_interaction.to_dict(),
                        status="resuming",
                    ))

                stored_history_messages = list(messages)
                history_projection = project_messages_for_provider(
                    [
                        message
                        for message in messages
                        if is_llm_visible_history_message(message)
                    ],
                    session_id=session_id,
                    user_id=uid,
                )
                turn_telemetry["history_context_projection"] = (
                    history_projection.to_dict()
                )
                # Keep internal context IDs on the in-memory transcript so a
                # same-turn archive/restore can be re-projected immediately.
                # ContextAssembler sanitizes these fields before every provider
                # call, so raw private metadata never crosses the boundary.
                messages_for_llm = list(history_projection.messages)
                # Canonical knowledge collections are queryable regardless of
                # whether their legacy collection_mode was "vector".
                has_vector_collections = (
                    DataContextCollection.query.filter_by(user_id=uid).first()
                    is not None
                )
                meta_capability_tool_defs = app_obj.addon_manager.get_meta_capability_tool_declarations()
                meta_capability_tool_names = {tool["function"]["name"] for tool in meta_capability_tool_defs}
                runtime_run_tool_defs = get_run_tool_declarations()
                runtime_run_tool_names = {tool["function"]["name"] for tool in runtime_run_tool_defs}

                def handle_runtime_run_tool(tool_name, tool_args, executor_uid):
                    scoped_args = bind_run_tool_scope(tool_name, tool_args, turn_execution_scope, runtime_policy)
                    scoped_args, scope_error = enforce_direct_capability_scope(
                        tool_name,
                        scoped_args,
                        turn_execution_scope,
                    )
                    if scope_error:
                        return scope_error
                    user_record = db.session.get(User, executor_uid)
                    return execute_run_tool(
                        tool_name,
                        scoped_args,
                        user_id=executor_uid,
                        is_admin=bool(getattr(user_record, 'is_admin', False)),
                        addon_manager=app_obj.addon_manager if hasattr(app_obj, 'addon_manager') else None,
                        tool_config=config,
                        chat_session_id=session_id,
                    )

                def get_runtime_tool_view_parameters(f_name):
                    allowed_sets = []
                    if not invoked_skill_id or not hasattr(app_obj.addon_manager, 'get_skill_tool_view_allowed_parameters'):
                        skill_allowed = None
                    else:
                        skill_allowed = app_obj.addon_manager.get_skill_tool_view_allowed_parameters(invoked_skill_id, f_name)
                    if skill_allowed:
                        allowed_sets.append(set(skill_allowed))

                    try:
                        tool_record = app_obj.addon_manager.get_tool_record(f_name) if hasattr(app_obj, 'addon_manager') else None
                        tool_views = getattr(getattr(app_obj.addon_manager, "registries", None), "tools", None)
                        tool_views = getattr(tool_views, "tool_views", {}) if tool_views else {}
                        for view_id in turn_execution_scope.tool_view_ids:
                            view = tool_views.get(view_id)
                            if view and tool_record and view.tool_id == tool_record.id and view.allowed_parameters:
                                allowed_sets.append(set(view.allowed_parameters))
                    except Exception:
                        app_obj.logger.debug("Unable to resolve scoped tool view parameters for %s", f_name, exc_info=True)

                    if not allowed_sets:
                        return None
                    allowed = set.intersection(*allowed_sets) if len(allowed_sets) > 1 else allowed_sets[0]
                    return sorted(allowed)

                def execute_registered_addon_tool(
                    f_name,
                    f_args,
                    executor_uid,
                    tool_config=None,
                    *,
                    authority_checked=False,
                ):
                    raw_args = dict(f_args or {})
                    approval_id = raw_args.pop('_approval_id', None)
                    resource_reference_ids = raw_args.pop('_resource_reference_ids', None)
                    if authority_checked:
                        try:
                            result = app_obj.addon_manager._execute_registered_tool_raw(
                                f_name,
                                raw_args,
                                tool_config or config,
                            )
                            if inspect.isgenerator(result):
                                result = next((r for r in result if r.get('type') in ['finish', 'error']), {"status": "success", "message": "Streaming op complete."})
                            return result
                        except Exception as exc:
                            return {"error": str(exc), "status": "tool_execution_failed"}

                    allowed_parameters = get_runtime_tool_view_parameters(f_name)
                    return app_obj.addon_manager.execute_capability(
                        f_name,
                        raw_args,
                        tool_config or config,
                        requester_user_id=executor_uid,
                        runtime_policy=runtime_policy,
                        approval_id=approval_id,
                        resource_reference_ids=resource_reference_ids,
                        allowed_parameters=allowed_parameters,
                        audit_log_source_id="jobs.runtime",
                        log_source_ids=("jobs.runtime",),
                    )

                discovery_routing_guard = DiscoveryRoutingGuard()

                def execute_meta_capability_tool(f_name, f_args):
                    meta_args = dict(f_args or {})
                    if f_name in {"capabilities_runtime_inventory", "capabilities_source_coverage"}:
                        meta_args["_runtime_policy"] = runtime_policy
                    return execute_meta_capability_with_policy(
                        app_obj.addon_manager,
                        discovery_routing_guard,
                        f_name,
                        meta_args,
                        execution_scope=turn_execution_scope,
                    )

                def is_side_effect_tool(f_name):
                    tool_record = app_obj.addon_manager.get_tool_record(f_name) if hasattr(app_obj, 'addon_manager') else None
                    if not tool_record:
                        return False
                    return not tool_is_read_only(tool_record)

                def execute_simple_delegate_task(tool_args, executor_uid, executor_sid):
                    prompt_text = str((tool_args or {}).get("prompt") or "").strip()
                    if not prompt_text:
                        return {"status": "error", "text": "Delegation failed: prompt is required."}
                    coverage_purpose = str((tool_args or {}).get("coverage_purpose") or "").strip()

                    model_role = str((tool_args or {}).get("model_role") or "fast").strip().lower()
                    if model_role not in {"fast", "medium"}:
                        model_role = "fast"
                    target_model = ModelRouter.resolve_model(model_role, default_tier="fast", user_id=executor_uid)

                    try:
                        max_tool_calls = int((tool_args or {}).get("max_tool_calls") or 8)
                    except (TypeError, ValueError):
                        max_tool_calls = 8
                    max_tool_calls = max(1, min(max_tool_calls, 12))
                    try:
                        delegate_parallel_tool_calls = max(
                            1,
                            min(
                                int((tool_args or {}).get("parallel_tool_calls") or 4),
                                10,
                            ),
                        )
                    except (TypeError, ValueError):
                        delegate_parallel_tool_calls = 4

                    capability_ids = _capability_id_list((tool_args or {}).get("capability_ids") or [])
                    delegate_scope_check = turn_execution_scope.check_child_request(capability_ids=capability_ids)
                    if not delegate_scope_check.allowed:
                        error = delegate_scope_check.to_error()
                        error["error"] = "Delegation cannot use capabilities outside the current execution scope."
                        error["allowed_capability_ids"] = list(turn_execution_scope.capability_ids)
                        return error
                    capability_ids = delegate_scope_check.capability_ids

                    delegate_tools = []
                    if capability_ids and hasattr(app_obj.addon_manager, "get_tool_declarations_for_capability_ids"):
                        delegate_tools.extend(
                            app_obj.addon_manager.get_tool_declarations_for_capability_ids(
                                capability_ids,
                                config,
                                include_side_effect_tools=False,
                            )
                        )
                    if has_vector_collections:
                        delegate_tools.extend([
                            KNOWLEDGE_QUERY_TOOL_DEF,
                            LIST_KNOWLEDGE_COLLECTIONS_TOOL_DEF,
                        ])
                    delegate_tools = unique_tool_declarations(delegate_tools)

                    available_tool_names = [
                        tool.get("function", {}).get("name")
                        for tool in delegate_tools
                        if isinstance(tool, dict) and tool.get("function")
                    ]
                    mandate = settings.get("prompt_junior_assistant_mandate") or JUNIOR_ASSISTANT_MANDATE_DEFAULT_PROMPT
                    system_prompt = (
                        "You are a short-lived delegated assistant running inline inside the current chat turn. "
                        "Complete only the assigned subtask and return the result directly to the chief assistant. "
                        "Do not create workflow runs, durable artifacts, approvals, or background jobs. "
                        "You may only use read-only tools; if a needed tool would write, delete, require approval, "
                        "or mutate external state, stop and report that limitation.\n"
                        f"{mandate}\n\n"
                        "--- SIMPLE DELEGATION OVERRIDE ---\n"
                        "No `update_shared_memory` tool is available in this inline delegation mode. "
                        "Return concise findings directly in your final response.\n\n"
                        "--- COVERAGE REPORTING ---\n"
                        "If you inspect external sources, emails, documents, pages, files, posts, records, "
                        "or tool-result refs, include a final fenced JSON block with a `coverage_items` array. "
                        "Each item should include `source_type`, a stable `source_id` or `canonical_url`, "
                        "`title`, `status` (`inspected`, `skipped`, or `failed`), concise `summary`, "
                        "`confidence`, and any `evidence_refs`. Do not paste long raw content into this block. "
                        "If no inspectable sources were used, return `coverage_items: []`."
                    )
                    if coverage_purpose:
                        system_prompt += f"\nCoverage purpose label: {coverage_purpose}"
                    if available_tool_names:
                        system_prompt += "\n\nRead-only tools available to you: " + ", ".join(available_tool_names)

                    delegate_messages = [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": prompt_text},
                    ]
                    delegate_context_assembler = ContextAssembler(
                        "worker",
                        message_sanitizer=sanitize_messages_for_llm_context,
                    )
                    delegate_llm = LLMService(
                        user_id=executor_uid,
                        session_id=executor_sid,
                        cost_attribution={"turn_id": turn_id},
                    )
                    response_message = None

                    @contextlib.contextmanager
                    def delegate_tool_execution_context():
                        with app_obj.app_context(), app_obj.test_request_context():
                            delegate_user = db.session.get(User, executor_uid)
                            if delegate_user:
                                login_user(delegate_user)
                            session["encryption_key"] = encryption_key
                            session["session_id"] = executor_sid
                            yield

                    def check_delegate_tool_availability(f_name, _f_args):
                        if f_name not in available_tool_names:
                            return enforce_turn_tool_availability(
                                f_name,
                                available_tool_names,
                                turn_execution_scope,
                            )
                        if is_side_effect_tool(f_name):
                            return {
                                "status": "blocked",
                                "error": (
                                    "Simple delegation cannot execute write, delete, "
                                    "or approval-gated tools."
                                ),
                            }
                        return None

                    def dispatch_delegate_tool(f_name, f_args, _prepared):
                        if f_name == "knowledge_query":
                            result = handle_knowledge_query(
                                app_obj.rag_service,
                                f_args,
                                executor_uid,
                                rag_collection_ids,
                            )
                        elif f_name == "rag_search":
                            result = handle_rag_search(
                                app_obj.rag_service,
                                settings,
                                f_args.get("query"),
                                executor_uid,
                                rag_collection_ids,
                                f_args.get("num_results"),
                                f_args.get("collection_ids"),
                            )
                        elif f_name == "list_knowledge_collections":
                            result = handle_list_knowledge_collections(executor_uid)
                        else:
                            result = execute_registered_addon_tool(
                                f_name,
                                f_args,
                                executor_uid,
                                config,
                                authority_checked=True,
                            )
                        return {"action": "delegate_read", "result": result}

                    def externalize_delegate_tool_payload(prepared, raw_content, result):
                        return externalize_runtime_payload(
                            raw_content,
                            instance_path=app_obj.instance_path,
                            namespace=f"chat-delegate-{executor_sid}",
                            created_by_user_id=executor_uid,
                            label=f"Delegated tool result: {prepared.tool_name}",
                            kind="chat_delegate_tool_result",
                            lineage={
                                "chat_session_id": executor_sid,
                                "turn_id": turn_id,
                                "tool_call_id": prepared.tool_call_id,
                                "tool_name": prepared.tool_name,
                            },
                            commit=False,
                        )

                    delegate_tool_executor = ToolExecutor(
                        dispatcher=dispatch_delegate_tool,
                        authority=ToolAuthorityContext(
                            requester_user_id=executor_uid,
                            runtime_policy=runtime_policy,
                            audit_log_source_id="jobs.runtime",
                            log_source_ids=("jobs.runtime",),
                        ),
                        tool_record_resolver=(
                            app_obj.addon_manager.get_tool_record
                            if hasattr(app_obj.addon_manager, "get_tool_record")
                            else None
                        ),
                        argument_validator=(
                            app_obj.addon_manager.validate_registered_tool_call
                            if hasattr(app_obj.addon_manager, "validate_registered_tool_call")
                            else None
                        ),
                        availability_checker=check_delegate_tool_availability,
                        execution_context_factory=delegate_tool_execution_context,
                        result_externalizer=externalize_delegate_tool_payload,
                        timeout_seconds=runtime_tool_timeout_seconds,
                    )

                    for _delegate_call_index in range(max_tool_calls):
                        delegate_context = delegate_context_assembler.assemble(
                            delegate_messages,
                            metadata={
                                "chat_session_id": executor_sid,
                                "parent_turn_id": turn_id,
                            },
                        )
                        response = delegate_llm.completion(
                            model=target_model,
                            messages=list(delegate_context.messages),
                            tools=delegate_tools or None,
                            source="chat_delegate",
                            telemetry_sink=turn_telemetry,
                        )
                        response_message = response.choices[0].message
                        delegate_messages.append(sanitize_llm_message(response_message))
                        if not response_message.tool_calls:
                            final_text = response_message.content or ""
                            return {
                                "status": "ok",
                                "model": target_model,
                                "text": final_text,
                                "tool_names": available_tool_names,
                            }

                        executed_delegate_calls = delegate_tool_executor.execute_calls(
                            list(response_message.tool_calls),
                            loop_index=_delegate_call_index,
                            max_workers=min(
                                delegate_parallel_tool_calls,
                                len(response_message.tool_calls),
                            ),
                        )
                        for executed in executed_delegate_calls:
                            delegate_messages.append(tool_message_from_envelope(
                                executed.result_envelope or {},
                                result=executed.result,
                                provider_tool_call_id=executed.provider_tool_call_id,
                            ))

                    final_text = response_message.content if response_message else ""
                    return {
                        "status": "max_tool_calls_reached",
                        "model": target_model,
                        "text": final_text or "Delegated task reached the tool-call limit before producing a final answer.",
                        "tool_names": available_tool_names,
                    }

                def find_context_result_message(ref_id):
                    found = find_tool_result_message_by_ref(messages, ref_id)
                    if found:
                        for msg_wrapper in db.session.query(ChatMessage).filter_by(session_id=session_id).all():
                            payload = msg_wrapper.message if isinstance(msg_wrapper.message, dict) else {}
                            if find_tool_result_message_by_ref([payload], ref_id):
                                return found, msg_wrapper
                        return found, None
                    for msg_wrapper in db.session.query(ChatMessage).filter_by(session_id=session_id).all():
                        payload = msg_wrapper.message if isinstance(msg_wrapper.message, dict) else {}
                        if find_tool_result_message_by_ref([payload], ref_id):
                            return payload, msg_wrapper
                    found = find_tool_result_message_by_ref(stored_history_messages, ref_id)
                    if found:
                        return found, None
                    return None, None

                def load_raw_context_result_from_artifact(meta):
                    if not isinstance(meta, dict) or not meta.get("raw_content_artifact_id"):
                        return None
                    return read_tool_result_artifact_raw_content(
                        meta.get("raw_content_artifact_id"),
                        instance_path=app_obj.instance_path,
                        owner_user_id=uid,
                    )

                def context_item_id_from_message(message):
                    metadata = (
                        message.get("_context_fabric")
                        if isinstance(message, dict)
                        and isinstance(message.get("_context_fabric"), dict)
                        else {}
                    )
                    return str(metadata.get("context_item_id") or "").strip()

                def resolve_context_targets(tool_args):
                    item_ids = normalize_context_item_ids(tool_args)
                    refs = normalize_context_ref_ids(tool_args)
                    legacy_refs = []
                    ref_to_item = {}
                    for ref_id in refs:
                        message, _wrapper = find_context_result_message(ref_id)
                        item_id = context_item_id_from_message(message)
                        if item_id:
                            ref_to_item[ref_id] = item_id
                            if item_id not in item_ids:
                                item_ids.append(item_id)
                        else:
                            legacy_refs.append(ref_id)
                    return item_ids, refs, legacy_refs, ref_to_item

                def handle_context_read_result(tool_args):
                    args = tool_args if isinstance(tool_args, dict) else {}
                    item_ids, refs, _legacy_refs, ref_to_item = (
                        resolve_context_targets(args)
                    )
                    item_id = item_ids[0] if item_ids else ""
                    ref_id = refs[0] if refs else ""
                    if item_id:
                        try:
                            try:
                                max_chars = max(
                                    1,
                                    min(int(args.get("max_chars") or 8000), 50_000),
                                )
                            except (TypeError, ValueError):
                                max_chars = 8000
                            result = read_context_item_evidence(
                                item_id,
                                user_id=uid,
                                session_id=session_id,
                                start_offset=args.get("start_offset", 0),
                                max_chars=max_chars,
                            )
                            result["context_item_id"] = item_id
                            result["context_ref_id"] = next(
                                (
                                    candidate_ref
                                    for candidate_ref, candidate_item in ref_to_item.items()
                                    if candidate_item == item_id
                                ),
                                ref_id or None,
                            )
                            result["truncated"] = bool(result.get("has_more"))
                            return result
                        except ContextFabricError as exc:
                            return {
                                "status": "not_found",
                                "context_item_id": item_id,
                                "error": str(exc),
                            }
                    if not ref_id:
                        return {
                            "status": "error",
                            "error": (
                                "context_item_id or context_ref_id is required."
                            ),
                        }
                    message, _msg_wrapper = find_context_result_message(ref_id)
                    if not message:
                        return {
                            "status": "not_found",
                            "context_ref_id": ref_id,
                            "error": "No retained tool result with that context_ref_id was found in this chat session.",
                        }
                    return read_context_result_from_message(
                        message,
                        start_offset=(tool_args or {}).get("start_offset", 0),
                        max_chars=(tool_args or {}).get("max_chars", 8000),
                        raw_content_loader=load_raw_context_result_from_artifact,
                    ) or {
                        "status": "error",
                        "context_ref_id": ref_id,
                        "error": "The referenced message does not contain retained result content.",
                    }

                def handle_context_search_result(tool_args):
                    args = tool_args if isinstance(tool_args, dict) else {}
                    item_ids, refs, legacy_refs, _ref_to_item = (
                        resolve_context_targets(args)
                    )
                    query = str(args.get("query") or "").strip()
                    if not query:
                        return {"status": "error", "error": "query is required."}
                    results = []
                    missing = []
                    total_matches = 0
                    if item_ids or not refs:
                        requested_states = args.get("lifecycle_states")
                        if not isinstance(requested_states, (list, tuple, set)):
                            requested_states = (
                                ("active", "archived", "excluded")
                                if item_ids
                                else ("archived",)
                            )
                        try:
                            durable_search = search_context_archive(
                                session_id=session_id,
                                user_id=uid,
                                query=query,
                                lifecycle_states=requested_states,
                                context_item_ids=item_ids or None,
                                limit=args.get("max_matches", 10),
                            )
                            results.extend(durable_search.get("matches") or [])
                            total_matches += int(durable_search.get("count") or 0)
                        except ContextFabricError as exc:
                            return {"status": "error", "error": str(exc)}
                    for ref_id in legacy_refs[:20]:
                        message, _msg_wrapper = find_context_result_message(ref_id)
                        if not message:
                            missing.append(ref_id)
                            continue
                        search_result = search_context_result_in_message(
                            message,
                            query,
                            max_matches=args.get("max_matches", 10),
                            snippet_chars=args.get("snippet_chars", 320),
                            case_sensitive=bool(args.get("case_sensitive")),
                            raw_content_loader=load_raw_context_result_from_artifact,
                        )
                        if search_result.get("context_ref_id") is None:
                            search_result["context_ref_id"] = ref_id
                        total_matches += int(search_result.get("matched_count") or 0)
                        results.append(search_result)
                    return {
                        "status": "ok" if results else "not_found",
                        "query": query,
                        "searched_context_item_ids": item_ids,
                        "searched_context_ref_ids": refs,
                        "searched_count": len(item_ids) + len(legacy_refs),
                        "missing_context_ref_ids": missing,
                        "matched_count": total_matches,
                        "results": results,
                        "message": (
                            "Use context_read_result with context_item_id around "
                            "returned offsets when a match needs deeper inspection."
                            if total_matches
                            else "No matches found in the selected or archived context."
                        ),
                    }

                def handle_context_manage_results(tool_args):
                    args = tool_args if isinstance(tool_args, dict) else {}
                    item_ids, refs, legacy_refs, ref_to_item = (
                        resolve_context_targets(args)
                    )
                    retention_state = str(
                        args.get("retention_state") or ""
                    ).strip()
                    action = str(args.get("action") or "").strip().lower()
                    summary = args.get("summary")
                    key_facts = args.get("key_facts")
                    if not item_ids and not refs:
                        return {
                            "status": "error",
                            "error": (
                                "context_item_ids/context_item_id or "
                                "context_ref_ids/context_ref_id is required."
                            ),
                        }
                    if not action and not retention_state:
                        return {
                            "status": "error",
                            "error": "action or retention_state is required.",
                        }

                    legacy_to_action = {
                        "inline_keep": "materialize_full",
                        "inline_this_turn": "materialize_full",
                        "preview_ref": "materialize_reference",
                        "summary_ref": "summarize",
                        "archive_only": "archive",
                        "pin_for_chief": "pin",
                    }
                    effective_action = action or legacy_to_action.get(
                        retention_state, ""
                    )
                    if effective_action not in {
                        "keep",
                        "restore",
                        "archive",
                        "exclude",
                        "pin",
                        "unpin",
                        "materialize_full",
                        "materialize_reference",
                        "materialize_overflow",
                        "summarize",
                        "excerpt",
                    }:
                        return {
                            "status": "error",
                            "error": f"Unsupported context action: {effective_action or retention_state}.",
                        }

                    updated_items = []
                    conflicts = []
                    missing = []
                    persisted_updates = False
                    expected_versions = (
                        args.get("expected_versions")
                        if isinstance(args.get("expected_versions"), dict)
                        else {}
                    )
                    lossy_actions = {
                        "archive",
                        "exclude",
                        "materialize_reference",
                        "materialize_overflow",
                        "summarize",
                        "excerpt",
                    }
                    for item_id in item_ids:
                        try:
                            item = get_context_item(
                                item_id,
                                user_id=uid,
                                session_id=session_id,
                            )
                        except ContextFabricError:
                            missing.append(item_id)
                            continue
                        if (
                            str(
                                runtime_policy.get("context_stewardship")
                                or "assisted"
                            ).lower()
                            == "manual"
                            and effective_action in lossy_actions
                            and item_id not in authorized_context_item_ids
                        ):
                            conflicts.append({
                                "context_item_id": item_id,
                                "error": (
                                    "Manual mode requires explicit user "
                                    "authorization for this lossy projection."
                                ),
                            })
                            continue
                        try:
                            expected_version = int(
                                expected_versions.get(item_id)
                                or args.get("expected_version")
                                or item.version
                            )
                        except (TypeError, ValueError):
                            expected_version = item.version

                        transition_kwargs = {}
                        if effective_action == "keep":
                            if item.version != expected_version:
                                conflicts.append({
                                    "context_item_id": item_id,
                                    "error": (
                                        f"Context item is at version {item.version}, "
                                        f"not {expected_version}."
                                    ),
                                })
                                continue
                            updated = item
                        else:
                            if effective_action == "restore":
                                transition_kwargs.update(
                                    lifecycle_state="active",
                                    materialization="reference",
                                )
                            elif effective_action == "archive":
                                transition_kwargs.update(
                                    lifecycle_state="archived",
                                    materialization="reference",
                                )
                            elif effective_action == "exclude":
                                transition_kwargs.update(
                                    lifecycle_state="excluded",
                                    materialization="reference",
                                )
                            elif effective_action == "pin":
                                transition_kwargs.update(
                                    lifecycle_state="active",
                                    is_pinned=True,
                                )
                            elif effective_action == "unpin":
                                transition_kwargs["is_pinned"] = False
                            elif effective_action == "materialize_full":
                                transition_kwargs.update(
                                    lifecycle_state="active",
                                    materialization="full",
                                )
                            elif effective_action == "materialize_reference":
                                transition_kwargs.update(
                                    lifecycle_state="active",
                                    materialization="reference",
                                )
                            elif effective_action == "materialize_overflow":
                                transition_kwargs.update(
                                    lifecycle_state="active",
                                    materialization="overflow",
                                )
                            elif effective_action == "summarize":
                                summary_payload = serialize_tool_output({
                                    "summary": summary or "",
                                    "key_facts": key_facts or [],
                                })
                                if not str(summary or "").strip() and not key_facts:
                                    conflicts.append({
                                        "context_item_id": item_id,
                                        "error": (
                                            "summarize requires summary or key_facts."
                                        ),
                                    })
                                    continue
                                transition_kwargs.update(
                                    lifecycle_state="active",
                                    materialization="summary",
                                    materialized_content=summary_payload,
                                )
                            elif effective_action == "excerpt":
                                excerpt = (
                                    args.get("excerpt")
                                    or args.get("content")
                                    or summary
                                )
                                if not str(excerpt or ""):
                                    conflicts.append({
                                        "context_item_id": item_id,
                                        "error": "excerpt requires excerpt/content.",
                                    })
                                    continue
                                transition_kwargs.update(
                                    lifecycle_state="active",
                                    materialization="excerpt",
                                    materialized_content=str(excerpt),
                                )
                            try:
                                updated = transition_context_item(
                                    item.id,
                                    expected_version=expected_version,
                                    user_id=uid,
                                    session_id=session_id,
                                    turn_id=turn_id,
                                    actor_type="assistant",
                                    actor_id=model_name,
                                    reason=args.get("reason")
                                    or f"Model requested {effective_action}.",
                                    metadata={
                                        "tool_name": "context_manage_results",
                                        "legacy_retention_state": retention_state,
                                    },
                                    **transition_kwargs,
                                )
                            except (
                                ContextVersionConflict,
                                ContextPolicyError,
                                ContextFabricError,
                            ) as exc:
                                conflicts.append({
                                    "context_item_id": item_id,
                                    "error": str(exc),
                                })
                                continue
                        updated_items.append(updated.to_dict())
                        persisted_updates = True

                    # Keep legacy refs operational while Context Fabric becomes
                    # the sole authority for provider projection.
                    for ref_id in refs:
                        message, msg_wrapper = find_context_result_message(ref_id)
                        if not message:
                            if ref_id not in missing:
                                missing.append(ref_id)
                            continue
                        legacy_state = retention_state
                        if not legacy_state:
                            legacy_state = {
                                "keep": "inline_keep",
                                "restore": "preview_ref",
                                "archive": "archive_only",
                                "exclude": "archive_only",
                                "pin": "pin_for_chief",
                                "unpin": "inline_keep",
                                "materialize_full": "inline_keep",
                                "materialize_reference": "preview_ref",
                                "materialize_overflow": "preview_ref",
                                "summarize": "summary_ref",
                                "excerpt": "summary_ref",
                            }.get(effective_action, "preview_ref")
                        if not apply_tool_result_retention(
                            message,
                            legacy_state,
                            summary=summary,
                            key_facts=key_facts,
                            raw_content_loader=load_raw_context_result_from_artifact,
                        ):
                            if ref_id not in missing:
                                missing.append(ref_id)
                            continue
                        if msg_wrapper is not None:
                            msg_wrapper.message = message
                            flag_modified(msg_wrapper, "message")
                            persisted_updates = True
                    if persisted_updates:
                        commit_with_retry(db.session)
                    return {
                        "status": (
                            "ok"
                            if updated_items or (refs and not missing)
                            else "conflict"
                            if conflicts
                            else "not_found"
                        ),
                        "action": effective_action,
                        "updated_context_items": updated_items,
                        "updated_context_ref_ids": refs,
                        "missing_ids": missing,
                        "conflicts": conflicts,
                        "retention_state": retention_state or None,
                        "reason": args.get("reason") or "",
                        "memory_reminder": (
                            "If the compacted result contains important task state, persist the key facts "
                            "with update_session_memory before relying on future context compaction."
                        ),
                    }

                def run_tool_in_thread(
                    f_name,
                    f_args,
                    executor_uid,
                    executor_sid,
                    *,
                    shared_executor=False,
                ):
                    """Executes a single tool safely within an isolated application context."""
                    with app_obj.app_context(), app_obj.test_request_context():
                        user_record = db.session.get(User, executor_uid)
                        if user_record:
                            login_user(user_record)

                        session['encryption_key'] = encryption_key
                        session['session_id'] = executor_sid
                        try:
                            unavailable = enforce_turn_tool_availability(
                                f_name,
                                _tool_names_from_declarations(chief_tools),
                                turn_execution_scope,
                            )
                            if unavailable:
                                return {"action": "blocked", "result": unavailable}
                            if f_name == 'delegate_task':
                                return {"action": "delegate", "result": execute_simple_delegate_task(f_args, executor_uid, executor_sid)}
                            elif f_name == 'knowledge_query':
                                return {
                                    "action": "knowledge",
                                    "result": handle_knowledge_query(
                                        app_obj.rag_service,
                                        f_args,
                                        executor_uid,
                                        rag_collection_ids,
                                    ),
                                }
                            elif f_name == 'rag_search':
                                return {"action": "rag", "result": handle_rag_search(app_obj.rag_service, settings, f_args.get('query'), executor_uid, rag_collection_ids, f_args.get('num_results'), f_args.get('collection_ids'))}
                            elif f_name == 'list_knowledge_collections':
                                return {"action": "list_knowledge", "result": handle_list_knowledge_collections(executor_uid)}
                            elif f_name == 'update_session_memory':
                                return {
                                    "action": "memory",
                                    "result": update_working_memory(
                                        f_args.get("action"),
                                        f_args.get("key"),
                                        f_args.get("value"),
                                        executor_sid,
                                    ),
                                }
                            elif f_name == 'memorize_fact':
                                return {
                                    "action": "memory_fact",
                                    "result": handle_memorize_fact(
                                        f_args.get('scope'),
                                        f_args.get('fact'),
                                        executor_uid,
                                        category=f_args.get('category'),
                                        tier=f_args.get('tier'),
                                        importance=f_args.get('importance'),
                                        tier_locked=f_args.get('tier_locked'),
                                        source="chat_model",
                                    ),
                                }
                            elif f_name == 'recall_fact':
                                return {
                                    "action": "memory_fact",
                                    "result": handle_recall_fact(
                                        f_args.get('query'),
                                        executor_uid,
                                        scope=f_args.get('scope'),
                                        category=f_args.get('category'),
                                        include_tier2=f_args.get('include_tier2'),
                                        max_results=f_args.get('max_results'),
                                    ),
                                }
                            elif f_name == 'manage_long_term_memory':
                                return {
                                    "action": "memory_fact",
                                    "result": handle_manage_long_term_memory(
                                        f_args.get("action"),
                                        f_args.get("scope"),
                                        executor_uid,
                                        memory_id=f_args.get("memory_id"),
                                        fact=f_args.get("fact"),
                                        query=f_args.get("query"),
                                        limit=f_args.get("limit"),
                                        category=f_args.get("category"),
                                        tier=f_args.get("tier"),
                                        include_tier2=f_args.get("include_tier2"),
                                        importance=f_args.get("importance"),
                                        tier_locked=f_args.get("tier_locked"),
                                    ),
                                }
                            elif f_name == 'view_attachment':
                                return {"action": "view_attachment", "result": view_attachment(app_obj.root_path, f_args.get('filepath', ''))}
                            elif f_name == 'request_scope_expansion':
                                return {
                                    "action": "scope_expansion",
                                    "result": handle_request_scope_expansion(
                                        f_args,
                                        requester_user_id=executor_uid,
                                        current_scope=turn_execution_scope,
                                        chat_session_id=executor_sid,
                                        capability_id_normalizer=app_obj.addon_manager.normalize_capability_ids,
                                    ),
                                }
                            elif f_name == 'request_user_choice':
                                return {"action": "user_interaction", "result": handle_request_user_choice(f_args)}
                            elif f_name == 'request_self_nudge':
                                return {
                                    "action": "runtime_monitor",
                                    "result": handle_request_self_nudge(
                                        f_args,
                                        chat_session_id=executor_sid,
                                        requester_user_id=executor_uid,
                                        execution_scope=turn_execution_scope.to_dict(),
                                        runtime_policy=runtime_policy,
                                    ),
                                }
                            elif f_name == 'request_tool_budget_extension':
                                return {"action": "tool_budget_extension", "result": dict(f_args or {})}
                            elif f_name == 'context_read_result':
                                return {"action": "context_read", "result": handle_context_read_result(f_args)}
                            elif f_name == 'context_search_result':
                                return {"action": "context_search", "result": handle_context_search_result(f_args)}
                            elif f_name == 'context_manage_results':
                                return {"action": "context_manage", "result": handle_context_manage_results(f_args)}
                            elif f_name == 'source_coverage_upsert':
                                return {"action": "source_coverage", "result": upsert_source_coverage(f_args, executor_sid)}
                            elif f_name == 'source_coverage_get':
                                return {"action": "source_coverage", "result": get_source_coverage(f_args, executor_sid)}
                            elif f_name in meta_capability_tool_names:
                                return {"action": "meta_capability", "result": execute_meta_capability_tool(f_name, f_args)}
                            elif f_name in runtime_run_tool_names:
                                return {"action": "runtime_run", "result": handle_runtime_run_tool(f_name, f_args, executor_uid)}
                            else:
                                return {
                                    "action": "standard",
                                    "result": execute_registered_addon_tool(
                                        f_name,
                                        f_args,
                                        executor_uid,
                                        config,
                                        authority_checked=shared_executor,
                                    ),
                                }
                        except Exception as e:
                            app_obj.logger.error(f"Threaded tool crash: {e}", exc_info=True)
                            return {
                                "action": "error",
                                "result": {
                                    "error": (
                                        "Execution failed: "
                                        f"{public_error_message(e)}"
                                    )
                                },
                            }

                prompt_build = build_chat_messages_and_tools(
                    history_messages=messages_for_llm,
                    assistant_mode=turn_assistant_mode,
                    addon_manager=app_obj.addon_manager,
                    settings=settings,
                    user_instructions=u_instructions,
                    chat_session=chat_session,
                    user_message_for_llm=user_message_for_llm,
                    files_from_client=files_from_client,
                    selected_capability_ids=selected_capability_ids,
                    enable_delegation=enable_delegation,
                    invoked_skill_id=invoked_skill_id,
                    invoked_skill_instructions=invoked_skill_instructions,
                    invoked_workflow_context=invoked_workflow_context,
                    invoked_run_context=invoked_run_context,
                    has_vector_collections=has_vector_collections,
                    meta_capability_tool_defs=meta_capability_tool_defs,
                    runtime_run_tool_defs=runtime_run_tool_defs,
                    config=config,
                )
                if prompt_build.error:
                    stream_q.put({
                        "type": "error",
                        "message": prompt_build.error,
                        "routing_metadata": prompt_build.routing_metadata or {},
                    })
                    return
                configured_max_tool_calls = max_tool_calls
                max_tool_calls = resolve_turn_tool_call_budget(
                    configured_max_tool_calls,
                    prompt_build.routing_metadata,
                )
                route_flags = (
                    (prompt_build.routing_metadata or {}).get("route_flags")
                    if isinstance(
                        (prompt_build.routing_metadata or {}).get(
                            "route_flags"
                        ),
                        dict,
                    )
                    else {}
                )
                one_shot_code_intent = bool(
                    route_flags.get("one_shot_code_intent")
                )
                one_shot_code_completed = False
                turn_telemetry["tool_call_budget"] = {
                    "configured_ceiling": configured_max_tool_calls,
                    "effective": max_tool_calls,
                    "route_limited": (
                        max_tool_calls < configured_max_tool_calls
                    ),
                    "one_shot_code_intent": one_shot_code_intent,
                }
                messages = prompt_build.messages
                chief_tools = unique_tool_declarations(prompt_build.tools)
                if history_projection.reference_item_ids:
                    chief_tools = unique_tool_declarations([
                        *chief_tools,
                        CONTEXT_READ_RESULT_TOOL_DEF,
                        CONTEXT_SEARCH_RESULT_TOOL_DEF,
                        CONTEXT_MANAGE_RESULTS_TOOL_DEF,
                    ])

                def _tool_names_from_declarations(tool_declarations):
                    return [
                        tool.get("function", {}).get("name")
                        for tool in tool_declarations or []
                        if isinstance(tool, dict) and isinstance(tool.get("function"), dict) and tool["function"].get("name")
                    ]

                def prepare_direct_tools_for_capabilities(
                    capability_ids,
                    *,
                    operation_groups=None,
                    max_tools=None,
                ):
                    nonlocal chief_tools
                    if not capability_ids or not hasattr(app_obj.addon_manager, "get_tool_declarations_for_capability_ids"):
                        return {"added_tool_names": [], "total_tool_names": _tool_names_from_declarations(chief_tools)}
                    if isinstance(capability_ids, str):
                        capability_ids = [capability_ids]
                    scope_check = turn_execution_scope.check_child_request(capability_ids=capability_ids)
                    if not scope_check.allowed:
                        error = scope_check.to_error()
                        error["error"] = "Cannot prepare tools outside the current execution scope."
                        error["allowed_capability_ids"] = list(turn_execution_scope.capability_ids)
                        return error
                    capability_ids = scope_check.capability_ids
                    existing_names = set(_tool_names_from_declarations(chief_tools))
                    requested_groups = {
                        str(group or "").strip().lower()
                        for group in (operation_groups or [])
                        if str(group or "").strip()
                    }
                    effective_groups = requested_groups or {"read"}
                    include_side_effect_tools = bool(
                        effective_groups & {"write", "create", "update", "publish", "delete", "all"}
                    )
                    declarations = app_obj.addon_manager.get_tool_declarations_for_capability_ids(
                        capability_ids,
                        config,
                        include_side_effect_tools=include_side_effect_tools,
                    )
                    try:
                        schema_budget = max(
                            256,
                            int(config.get("chat_dynamic_schema_budget_tokens") or 4000),
                        )
                    except (TypeError, ValueError):
                        schema_budget = 4000
                    selected_group = select_dynamic_tool_declarations(
                        declarations,
                        operation_groups=effective_groups,
                        max_tools=max_tools,
                        schema_budget_tokens=schema_budget,
                    )
                    selected_declarations = selected_group["declarations"]

                    chief_tools = unique_tool_declarations([*chief_tools, *selected_declarations])
                    current_names = _tool_names_from_declarations(chief_tools)
                    added_names = [name for name in current_names if name not in existing_names]
                    return {
                        "capability_ids": capability_ids,
                        "operation_groups": selected_group["operation_groups"],
                        "added_tool_names": added_names,
                        "omitted_tool_names": selected_group["omitted_tool_names"],
                        "schema_tokens_added": selected_group["schema_tokens"],
                        "schema_budget_tokens": selected_group["schema_budget_tokens"],
                        "total_tool_names": current_names,
                        "message": (
                            "Prepared a budgeted operation group for direct inline use. Use these tools now if the task is simple; "
                            "create a durable run only if the task needs planning, provenance, review, or resumability."
                        ),
                    }

                debug_prompt_message = None
                initial_request_capture = {} if capture_debug_prompt else None
                if capture_debug_prompt:
                    debug_prompt_message = {
                        "role": "debug_prompt",
                        "system_prompt": (prompt_build.messages[0] if prompt_build.messages else {}).get("content", ""),
                        "user_message": prompt_build.messages[-1] if prompt_build.messages else dict(user_message_for_llm),
                        "tool_names": _tool_names_from_declarations(chief_tools),
                        "assistant_mode": turn_assistant_mode,
                        "selected_capability_ids": selected_capability_ids,
                        "execution_scope": turn_execution_scope.to_dict(),
                        "runtime_policy": runtime_policy,
                        "applied_scope_expansion_approval_ids": [
                            item.get("approval_id")
                            for item in applied_scope_expansions
                            if isinstance(item, dict) and item.get("approval_id")
                        ],
                        "enable_delegation": enable_delegation,
                        "first_payload_routing": prompt_build.routing_metadata or {},
                    }

                response = None
                waiting_for_user_requested = False
                context_retry_state = {}
                auto_compacted_ref_ids = set()
                current_admission = {}

                def ensure_context_result_tools():
                    nonlocal chief_tools
                    chief_tools = unique_tool_declarations([
                        *chief_tools,
                        CONTEXT_READ_RESULT_TOOL_DEF,
                        CONTEXT_SEARCH_RESULT_TOOL_DEF,
                        CONTEXT_MANAGE_RESULTS_TOOL_DEF,
                    ])
                    return chief_tools

                def persist_context_compactions(compacted, *, reason):
                    """Make reversible runtime compaction authoritative in Context Fabric."""
                    transitioned = []
                    for row in compacted or []:
                        ref_id = str((row or {}).get("context_ref_id") or "").strip()
                        if not ref_id:
                            continue
                        message, _wrapper = find_context_result_message(ref_id)
                        metadata = (
                            message.get("_context_fabric")
                            if isinstance(message, dict)
                            and isinstance(message.get("_context_fabric"), dict)
                            else {}
                        )
                        context_item_id = str(
                            metadata.get("context_item_id") or ""
                        ).strip()
                        if not context_item_id:
                            continue
                        try:
                            item = get_context_item(
                                context_item_id,
                                user_id=uid,
                                session_id=session_id,
                            )
                            if item.is_pinned:
                                continue
                            legacy_meta = (
                                message.get("_context_optimization")
                                if isinstance(message.get("_context_optimization"), dict)
                                else {}
                            )
                            has_summary = bool(
                                legacy_meta.get("summary")
                                or legacy_meta.get("key_facts")
                            )
                            next_materialization = (
                                "summary" if has_summary else "reference"
                            )
                            materialized_content = None
                            if has_summary:
                                materialized_content = serialize_tool_output({
                                    "summary": legacy_meta.get("summary") or "",
                                    "key_facts": legacy_meta.get("key_facts") or [],
                                })
                            if (
                                item.materialization == next_materialization
                                and (
                                    not has_summary
                                    or item.materialized_content == materialized_content
                                )
                            ):
                                continue
                            updated = transition_context_item(
                                item.id,
                                expected_version=item.version,
                                user_id=uid,
                                session_id=session_id,
                                turn_id=turn_id,
                                actor_type="runtime",
                                actor_id="context_admission",
                                materialization=next_materialization,
                                materialized_content=materialized_content,
                                reason=reason,
                                metadata={
                                    "context_ref_id": ref_id,
                                    "retention_state": (row or {}).get(
                                        "retention_state"
                                    ),
                                },
                            )
                            metadata["context_item_version"] = updated.version
                            transitioned.append(updated.id)
                        except ContextFabricError:
                            app_obj.logger.warning(
                                "Unable to persist Context Fabric compaction for %s",
                                ref_id,
                                exc_info=True,
                            )
                    if transitioned:
                        turn_telemetry.setdefault(
                            "context_compaction_item_ids", []
                        ).extend(transitioned)
                    return ensure_context_result_tools()

                chief_context_assembler = ContextAssembler(
                    "chief",
                    message_sanitizer=sanitize_messages_for_llm_context,
                )
                loop_runner = AgentLoopRunner(
                    max_tool_calls=max_tool_calls,
                    event_sink=runtime_event_sink,
                    warning_at_remaining=2,
                    # A one-shot scratch turn deliberately has a three-iteration
                    # safety ceiling. Treating that small ceiling as "nearly
                    # exhausted" on its second iteration only adds a misleading
                    # warning to an otherwise healthy execution.
                    minimum_warning_budget=4 if one_shot_code_intent else 3,
                    budget_increase_tool_name="request_tool_budget_extension",
                )

                @contextlib.contextmanager
                def shared_chat_tool_context():
                    with app_obj.app_context(), app_obj.test_request_context():
                        execution_user = db.session.get(User, uid)
                        if execution_user:
                            login_user(execution_user)
                        session["encryption_key"] = encryption_key
                        session["session_id"] = session_id
                        yield

                def shared_tool_record(f_name):
                    if not hasattr(app_obj, "addon_manager"):
                        return None
                    return app_obj.addon_manager.get_tool_record(f_name)

                def validate_shared_tool_arguments(f_name, f_args):
                    return app_obj.addon_manager.validate_registered_tool_call(
                        f_name,
                        f_args,
                        allowed_parameters=get_runtime_tool_view_parameters(f_name),
                    )

                def shared_tool_availability(f_name, _f_args):
                    return enforce_turn_tool_availability(
                        f_name,
                        _tool_names_from_declarations(chief_tools),
                        turn_execution_scope,
                    )

                def dispatch_shared_chat_tool(f_name, f_args, prepared):
                    output = run_tool_in_thread(
                        f_name,
                        f_args,
                        uid,
                        session_id,
                        shared_executor=True,
                    )
                    result = output.get("result") if isinstance(output, dict) else None
                    if not isinstance(result, dict) or str(result.get("status") or "").lower() not in {
                        "waiting_for_user",
                        "waiting_for_approval",
                    }:
                        return output

                    interaction_user = db.session.get(User, uid)
                    if interaction_user:
                        login_user(interaction_user)
                    session["encryption_key"] = encryption_key
                    session["session_id"] = session_id
                    augmented_result, _interaction = create_runtime_interaction_from_tool_result(
                        result,
                        requester_user_id=uid,
                        chat_session_id=session_id,
                        tool_call_id=prepared.tool_call_id,
                        tool_name=prepared.tool_name,
                        tool_arguments=prepared.arguments,
                        resume_payload={
                            "model": model_name,
                            "assistant": {
                                **normalized_assistant_for_turn,
                                "mode": turn_assistant_mode,
                            },
                            "runtime_policy": runtime_policy,
                            "thinking_config": thinking_config,
                            "verbose": verbose,
                            "capture_debug_prompt": capture_debug_prompt,
                        },
                    )
                    return {**output, "result": augmented_result}

                def externalize_chat_tool_payload(prepared, raw_content, _result):
                    with app_obj.app_context(), app_obj.test_request_context():
                        artifact_user = db.session.get(User, uid)
                        if artifact_user:
                            login_user(artifact_user)
                        session["encryption_key"] = encryption_key
                        session["session_id"] = session_id
                        return externalize_runtime_payload(
                            raw_content,
                            instance_path=app_obj.instance_path,
                            namespace=f"chat-{session_id}",
                            created_by_user_id=uid,
                            label=f"Tool result: {prepared.tool_name}",
                            kind="runtime_tool_result_payload",
                            lineage={
                                "chat_session_id": session_id,
                                "turn_id": turn_id,
                                "tool_call_id": prepared.tool_call_id,
                                "tool_name": prepared.tool_name,
                            },
                            commit=True,
                        )

                try:
                    runtime_tool_timeout_seconds = float(
                        settings.get("runtime_tool_timeout_seconds") or 300
                    )
                except (TypeError, ValueError):
                    runtime_tool_timeout_seconds = 300.0
                runtime_tool_timeout_seconds = (
                    max(1.0, runtime_tool_timeout_seconds)
                    if runtime_tool_timeout_seconds > 0
                    else None
                )

                def resolve_chat_tool_timeout(prepared):
                    name = str(prepared.tool_name or "")
                    arguments = (
                        prepared.arguments
                        if isinstance(prepared.arguments, dict)
                        else {}
                    )
                    if name == "runs_project_workspace_inspect":
                        return 30.0
                    if name.startswith("capabilities_"):
                        return min(
                            runtime_tool_timeout_seconds or 30.0,
                            30.0,
                        )
                    if name == "runs_execute_python":
                        try:
                            requested = float(
                                arguments.get("timeout_seconds") or 10
                            )
                        except (TypeError, ValueError):
                            requested = 10.0
                        return min(75.0, max(15.0, requested + 10.0))
                    if name in {
                        "runs_code_session_create",
                        "runs_code_session_get",
                        "runs_project_workspace_create",
                        "runs_project_workspace_get",
                        "runs_project_workspace_edit",
                        "runs_project_workspace_process",
                        "runs_project_workspace_close",
                    }:
                        return 60.0
                    if name in {
                        "runs_code_session_execute",
                        "runs_code_session_install_dependencies",
                        "runs_project_workspace_execute",
                    }:
                        try:
                            requested = float(
                                arguments.get("timeout_seconds") or 60
                            )
                        except (TypeError, ValueError):
                            requested = 60.0
                        return min(3_630.0, max(30.0, requested + 30.0))
                    return runtime_tool_timeout_seconds

                shared_result_limits = tool_result_limits_for_policy(runtime_policy)
                shared_tool_executor = ToolExecutor(
                    dispatcher=dispatch_shared_chat_tool,
                    event_sink=runtime_event_sink,
                    authority=ToolAuthorityContext(
                        requester_user_id=uid,
                        runtime_policy=runtime_policy,
                        audit_log_source_id="jobs.runtime",
                        log_source_ids=("jobs.runtime",),
                    ),
                    tool_record_resolver=shared_tool_record,
                    argument_validator=validate_shared_tool_arguments,
                    availability_checker=shared_tool_availability,
                    execution_context_factory=shared_chat_tool_context,
                    result_externalizer=externalize_chat_tool_payload,
                    inline_result_limit_chars=shared_result_limits["inline_limit_chars"],
                    preview_chars=shared_result_limits["preview_chars"],
                    timeout_seconds=runtime_tool_timeout_seconds,
                    timeout_resolver=resolve_chat_tool_timeout,
                )

                def apply_automatic_context_compaction():
                    stewardship_mode = str(
                        runtime_policy.get("context_stewardship") or "assisted"
                    ).strip().lower()
                    if stewardship_mode != "aggressive":
                        # Assisted mode keeps complete results until exact
                        # provider admission proves adaptation is necessary.
                        # Manual mode never adapts them automatically.
                        return
                    try:
                        pressure_threshold = max(
                            1,
                            int(
                                config.get("context_pressure_min_included_tokens")
                                or config.get("context_pressure_included_tokens")
                                or 8000
                            ),
                        )
                    except (TypeError, ValueError):
                        pressure_threshold = 8000
                    auto_compacted = compact_tool_results_for_pressure(
                        messages,
                        runtime_policy=runtime_policy,
                        max_included_tokens=pressure_threshold,
                        raw_content_loader=load_raw_context_result_from_artifact,
                    )
                    new_compactions = [
                        row
                        for row in auto_compacted
                        if row.get("context_ref_id") not in auto_compacted_ref_ids
                    ]
                    if new_compactions:
                        auto_compacted_ref_ids.update(
                            row.get("context_ref_id")
                            for row in new_compactions
                            if row.get("context_ref_id")
                        )
                        persist_context_compactions(
                            new_compactions,
                            reason=(
                                "Aggressive-mode prompt pressure created a "
                                "reversible reference projection."
                            ),
                        )
                        ensure_context_result_tools()
                        turn_telemetry["automatic_context_compactions"] = (
                            int(turn_telemetry.get("automatic_context_compactions") or 0)
                            + len(new_compactions)
                        )
                        turn_telemetry.setdefault("automatic_context_compaction_refs", []).extend([
                            {
                                "context_ref_id": row.get("context_ref_id"),
                                "tool_name": row.get("tool_name"),
                                "before_tokens": row.get("before_tokens"),
                                "after_tokens": row.get("after_tokens"),
                            }
                            for row in new_compactions
                        ])

                def current_provider_messages():
                    apply_automatic_context_compaction()
                    projection = project_messages_for_provider(
                        messages,
                        session_id=session_id,
                        user_id=uid,
                    )
                    pressure_messages = build_context_pressure_messages(
                        list(projection.messages),
                        runtime_policy=runtime_policy,
                    )
                    assembly = chief_context_assembler.assemble(
                        list(projection.messages),
                        supplemental_messages=pressure_messages,
                        metadata={
                            "chat_session_id": session_id,
                            "turn_id": turn_id,
                        },
                    )
                    turn_telemetry["context_projection"] = projection.to_dict()
                    turn_telemetry["context_assembly"] = assembly.to_dict()
                    if isinstance(turn_telemetry.get("context_history_projection"), dict):
                        turn_telemetry["context_history_projection"].update({
                            "omitted_message_count": assembly.omitted_message_count,
                            "omitted_reasons": list(assembly.omitted_reasons),
                            "projected_message_count": len(assembly.messages),
                        })
                    return list(assembly.messages)

                def apply_history_projection(
                    history_budget,
                    _admission_result,
                    projection_facts,
                ):
                    """Apply one capacity-derived provider history projection."""
                    chief_context_assembler.max_history_tokens = max(
                        1,
                        int(history_budget),
                    )
                    return {
                        "assembler_role": chief_context_assembler.role,
                        "history_projection_applied": True,
                        "raw_messages_preserved": True,
                    }

                manual_overflow_item = None

                def handle_manual_overflow(admission_result, *, loop_index=None):
                    """Obtain a model-authored explanation from a bounded manifest."""
                    nonlocal manual_overflow_item
                    admission_payload = _admission_result_dict(admission_result)
                    overflow_tokens = int(
                        _admission_value(admission_result, "overflow_tokens", 0)
                        or 0
                    )
                    available_input_tokens = max(
                        1,
                        int(
                            _admission_value(
                                admission_result,
                                "available_input_tokens",
                                0,
                            )
                            or 1
                        ),
                    )
                    if user_evidence is not None and manual_overflow_item is None:
                        manual_overflow_item = create_context_item(
                            evidence=user_evidence,
                            user_id=uid,
                            kind="user_message",
                            title="Manual-mode overflow input",
                            turn_id=turn_id,
                            lifecycle_state="active",
                            materialization="overflow",
                            priority=90,
                            source_ref={
                                "chat_turn_id": turn_id,
                                "source_type": "chat",
                            },
                            metadata={
                                "overflow_tokens": overflow_tokens,
                                "available_input_tokens": available_input_tokens,
                                "provider_reported_overflow": bool(
                                    admission_payload.get(
                                        "provider_reported_overflow"
                                    )
                                ),
                            },
                            actor_type="runtime",
                            actor_id="manual_context_admission",
                            reason=(
                                "Manual-mode input remained durable after exact "
                                "provider admission refused the rendered payload."
                            ),
                        )
                        user_is_material_overflow_contributor = (
                            int(user_evidence.token_estimate or 0)
                            >= max(1, available_input_tokens // 2)
                        )
                        if (
                            user_is_material_overflow_contributor
                            and isinstance(user_message_to_save, dict)
                        ):
                            fabric_metadata = user_message_to_save.setdefault(
                                "_context_fabric", {}
                            )
                            fabric_metadata["context_item_id"] = (
                                manual_overflow_item.id
                            )
                            fabric_metadata["context_item_version"] = (
                                manual_overflow_item.version
                            )
                        # Make the overflow item durable before asking a second
                        # model call to explain the admission refusal.
                        commit_with_retry(db.session)

                    recovery_messages = build_manual_overflow_recovery_messages(
                        admission_result,
                        evidence_id=(
                            user_evidence.id if user_evidence is not None else None
                        ),
                        context_item_id=(
                            manual_overflow_item.id
                            if manual_overflow_item is not None
                            else None
                        ),
                        content_hash=(
                            user_evidence.content_hash
                            if user_evidence is not None
                            else None
                        ),
                    )
                    recovery_admission = {}
                    context_retry_state["manual_recovery_messages"] = (
                        recovery_messages
                    )
                    context_retry_state["manual_recovery_loop_index"] = loop_index
                    try:
                        recovery_response = llm_service.completion(
                            model=model_name,
                            messages=recovery_messages,
                            stream=False,
                            tools=None,
                            thinking_config=thinking_config,
                            source="chat_manual_overflow_recovery",
                            telemetry_sink=turn_telemetry,
                            admission_sink=recovery_admission,
                            context_admission_mode="manual",
                        )
                    except Exception as recovery_error:
                        evidence_label = (
                            user_evidence.id
                            if user_evidence is not None
                            else "unavailable"
                        )
                        raise RuntimeError(
                            "The original request was preserved as evidence "
                            f"{evidence_label}, but even the bounded Manual-mode "
                            "overflow explanation could not be admitted: "
                            f"{recovery_error}"
                        ) from recovery_error
                    context_retry_state["manual_recovery_admission"] = (
                        recovery_admission
                    )
                    turn_telemetry["manual_context_overflow"] = {
                        "original_admission": admission_payload,
                        "recovery_admission": dict(recovery_admission),
                        "evidence_id": (
                            user_evidence.id if user_evidence is not None else None
                        ),
                        "context_item_id": (
                            manual_overflow_item.id
                            if manual_overflow_item is not None
                            else None
                        ),
                    }
                    return recovery_response

                def apply_tool_budget_extension_request(tool_args, loop_index=None):
                    nonlocal max_tool_calls, tool_budget_extension_used
                    result = handle_request_tool_budget_extension(
                        tool_args,
                        current_max_tool_calls=max_tool_calls,
                        max_extension=chat_tool_budget_extension_max,
                        already_used=tool_budget_extension_used,
                    )
                    if result.get("status") == "approved":
                        tool_budget_extension_used = True
                        max_tool_calls = int(result.get("new_max_tool_calls") or max_tool_calls)
                        loop_runner.max_tool_calls = max_tool_calls
                        emit_agent_event(tool_budget_extended_event(
                            added_tool_calls=int(result.get("added_tool_call_iterations") or 0),
                            new_max_tool_calls=max_tool_calls,
                            loop_index=loop_index,
                            reason=result.get("reason") or "",
                        ))
                    return result

                def request_finalization_response(reason, loop_index=None):
                    loop_runner.max_tool_calls = max_tool_calls
                    loop_runner.begin_finalization(
                        messages,
                        reason=reason,
                        loop_index=loop_index,
                    )
                    stream_q.put({"type": "status", "message": "Requesting final answer..."})
                    finalization_response = complete_with_context_retry(
                        llm_service,
                        model=model_name,
                        messages=messages,
                        make_provider_messages=current_provider_messages,
                        retry_state=context_retry_state,
                        emit_agent_event=emit_agent_event,
                        stream_status=lambda message: stream_q.put({"type": "status", "message": message}),
                        raw_content_loader=load_raw_context_result_from_artifact,
                        on_context_compacted=lambda compacted: persist_context_compactions(
                            compacted,
                            reason=(
                                "Exact provider admission required a reversible "
                                "reference projection before retry."
                            ),
                        ),
                        on_history_projected=apply_history_projection,
                        on_manual_overflow=handle_manual_overflow,
                        loop_index=loop_index,
                        stream=False,
                        source="chat_finalization",
                        telemetry_sink=turn_telemetry,
                        admission_sink=current_admission,
                        context_admission_mode=runtime_policy.get("context_stewardship"),
                    )
                    context_fabric_turn.admission_json = dict(current_admission)
                    clean_finalization_msg = sanitize_llm_message(
                        finalization_response.choices[0].message
                    )
                    messages.append(clean_finalization_msg)
                    final_content = message_content_text(clean_finalization_msg)
                    loop_runner.finish_finalization(
                        reason=reason,
                        content=final_content,
                        loop_index=loop_index,
                    )
                    return finalization_response

                def transform_tool_arguments(
                    _tool_name,
                    tool_arguments,
                    _tool_call,
                ):
                    return app_obj.addon_manager.run_hook(
                        "before_tool_call",
                        args_dict=tool_arguments,
                    )

                def on_tool_call_started(prepared):
                    if prepared.tool_name == "delegate_task":
                        target = (
                            prepared.arguments.get("capability_ids")
                            or prepared.arguments.get("plugin_prefixes")
                        )
                        stream_q.put({
                            "type": "status",
                            "message": f"Spawning sub-agent(s): {target}...",
                        })
                        return
                    stream_q.put({
                        "type": "status",
                        "message": f"Executing tool: {prepared.tool_name}...",
                    })

                def execute_chat_tool_calls(tool_calls, loop_index):
                    # Close the parent write transaction before isolated tool
                    # contexts perform network, process, or database work.
                    commit_with_retry(
                        db.session,
                        operation_name="chat_pre_tool_checkpoint",
                    )
                    return shared_tool_executor.execute_calls(
                        tool_calls,
                        loop_index=loop_index,
                        argument_transform=transform_tool_arguments,
                        on_started=on_tool_call_started,
                        max_workers=10,
                    )

                def capture_initial_request(loop_index):
                    if not (
                        loop_index == 0
                        and debug_prompt_message is not None
                        and isinstance(initial_request_capture, dict)
                        and initial_request_capture.get("messages")
                    ):
                        return
                    debug_prompt_message["initial_request_messages"] = (
                        initial_request_capture.get("messages")
                    )
                    debug_prompt_message["initial_request_tools"] = (
                        initial_request_capture.get("tools") or []
                    )
                    debug_prompt_message["initial_request_model"] = (
                        initial_request_capture.get("model") or model_name
                    )

                def complete_tool_loop_iteration(loop_index):
                    completion_kwargs = {
                        "telemetry_sink": turn_telemetry,
                        "admission_sink": current_admission,
                        "context_admission_mode": runtime_policy.get(
                            "context_stewardship"
                        ),
                    }
                    if loop_index == 0 and isinstance(
                        initial_request_capture,
                        dict,
                    ):
                        completion_kwargs["debug_capture_sink"] = (
                            initial_request_capture
                        )
                    # Compaction can change the declared tool surface.
                    apply_automatic_context_compaction()
                    iteration_response = complete_with_context_retry(
                        llm_service,
                        model=model_name,
                        messages=messages,
                        make_provider_messages=current_provider_messages,
                        retry_state=context_retry_state,
                        emit_agent_event=emit_agent_event,
                        stream_status=lambda message: stream_q.put({
                            "type": "status",
                            "message": message,
                        }),
                        raw_content_loader=load_raw_context_result_from_artifact,
                        on_context_compacted=lambda compacted: (
                            persist_context_compactions(
                                compacted,
                                reason=(
                                    "Exact provider admission required a "
                                    "reversible reference projection before "
                                    "retry."
                                ),
                            )
                        ),
                        on_history_projected=apply_history_projection,
                        on_manual_overflow=handle_manual_overflow,
                        loop_index=loop_index,
                        files=(
                            files_from_client
                            if turn_assistant_mode != "default"
                            else None
                        ),
                        tools=chief_tools,
                        thinking_config=thinking_config,
                        source="chat_chief",
                        **completion_kwargs,
                    )
                    context_fabric_turn.admission_json = dict(current_admission)
                    capture_initial_request(loop_index)
                    return iteration_response

                def add_budget_warning_tool():
                    nonlocal chief_tools
                    chief_tools = unique_tool_declarations([
                        *chief_tools,
                        REQUEST_TOOL_BUDGET_EXTENSION_TOOL_DEF,
                    ])

                def clear_chief_tools():
                    nonlocal chief_tools
                    chief_tools = []

                result_processor = ChatToolResultProcessor(
                    ChatToolResultContext(
                        app=app_obj,
                        session_id=session_id,
                        user_id=uid,
                        turn_id=turn_id,
                        model_name=model_name,
                        runtime_policy=runtime_policy,
                        messages=messages,
                        new_messages=new_messages_from_turn,
                        selected_capability_ids=selected_capability_ids,
                        one_shot_code_intent=one_shot_code_intent,
                        ensure_context_result_tools=ensure_context_result_tools,
                        clear_tools=clear_chief_tools,
                        emit_agent_event=emit_agent_event,
                        prepare_direct_tools=prepare_direct_tools_for_capabilities,
                        execute_run_tool=handle_runtime_run_tool,
                        apply_budget_extension=(
                            apply_tool_budget_extension_request
                        ),
                    )
                )
                tool_loop = ChatToolLoop(
                    messages=messages,
                    new_messages=new_messages_from_turn,
                    loop_runner=loop_runner,
                    hooks=ChatToolLoopHooks(
                        complete_iteration=complete_tool_loop_iteration,
                        execute_tool_calls=execute_chat_tool_calls,
                        process_tool_result=result_processor.process,
                        request_finalization=request_finalization_response,
                        emit_assistant_output=emit_assistant_output,
                        emit_status=lambda message: stream_q.put({
                            "type": "status",
                            "message": message,
                        }),
                        record_tool_executions=lambda executions: (
                            record_tool_executions(
                                turn_telemetry,
                                executions,
                            )
                        ),
                        add_budget_warning_tool=add_budget_warning_tool,
                        get_max_tool_calls=lambda: max_tool_calls,
                    ),
                    one_shot_code_intent=one_shot_code_intent,
                )
                loop_result = tool_loop.run()
                response = loop_result.response
                waiting_for_user_requested = loop_result.waiting_for_user
                one_shot_code_completed = (
                    result_processor.one_shot_code_completed
                )

                if waiting_for_user_requested:
                    final_text = "Waiting for your response in the chat card above."
                else:
                    final_text = final_text_or_empty_note(response.choices[0].message) if response is not None else final_text_or_empty_note({})

                stream_q.put({
                    "type": "status",
                    "message": "Finalizing response, memory, and context...",
                })

                def transform_final_response(text):
                    if not hasattr(app_obj, "addon_manager"):
                        return text
                    return app_obj.addon_manager.run_hook(
                        "after_llm_response",
                        text=text,
                        session_id=chat_session.id,
                    )

                if one_shot_code_completed:
                    housekeeping_result = finalize_turn_deterministically(
                        turn_id=turn_id,
                        candidate_response=final_text,
                        response_transform=transform_final_response,
                        commit=False,
                    )
                    finalization_mode = "deterministic_keep"
                else:
                    housekeeping_provider_messages = (
                        context_retry_state.get("manual_recovery_messages")
                        or current_provider_messages()
                    )
                    finalization_source_messages = (
                        terminal_housekeeping_source_messages(
                            housekeeping_provider_messages,
                            final_text,
                        )
                    )
                    housekeeping_result = finalize_turn_with_model(
                        llm_service=llm_service,
                        turn_id=turn_id,
                        candidate_response=final_text,
                        thinking_config=thinking_config,
                        telemetry_sink=turn_telemetry,
                        source_messages=finalization_source_messages,
                        response_transform=transform_final_response,
                        user_authorized_context_item_ids=authorized_context_item_ids,
                        commit=False,
                    )
                    finalization_mode = "model"
                final_text = str(
                    (housekeeping_result.envelope.get("response") or {}).get("content")
                    or ""
                )
                turn_telemetry["terminal_housekeeping"] = {
                    "mode": finalization_mode,
                    "attempts": housekeeping_result.attempts,
                    "repair_errors": list(housekeeping_result.errors),
                    "memory_revision": (
                        housekeeping_result.commit_result.get("memory_revision") or {}
                    ).get("revision"),
                    "context_disposition_count": len(
                        housekeeping_result.envelope.get("context_dispositions") or []
                    ),
                }

                response_html = md_parser(final_text)

                final_session_memory = db.session.get(ChatSession, session_id).working_memory
                telemetry_snapshot = snapshot_turn_telemetry(turn_telemetry)
                if user_message_to_save is not None:
                    user_message_to_save['working_memory_before'] = working_memory_before_turn
                    user_message_to_save['working_memory_after'] = final_session_memory
                    user_message_to_save['execution_scope'] = turn_execution_scope.to_dict()
                    user_message_to_save['runtime_policy'] = runtime_policy
                    user_message_to_save['turn_telemetry'] = telemetry_snapshot
                if debug_prompt_message is not None:
                    debug_prompt_message["turn_telemetry"] = telemetry_snapshot
                app_obj.logger.info(
                    "Chat turn telemetry session_id=%s metrics=%s",
                    session_id,
                    json.dumps(telemetry_snapshot, ensure_ascii=True, sort_keys=True),
                )
                if user_message_to_save is not None and applied_scope_expansions:
                    user_message_to_save['applied_scope_expansion_approval_ids'] = [
                        item.get("approval_id")
                        for item in applied_scope_expansions
                        if isinstance(item, dict) and item.get("approval_id")
                    ]

                end_costs = db.session.query(LLMCost).filter_by(session_id=session_id).all()
                end_cost_val = sum(c.cost for c in end_costs)
                end_prompt_val = sum(c.prompt_tokens for c in end_costs)
                end_comp_val = sum(c.completion_tokens for c in end_costs)
                end_total_val = sum(c.total_tokens for c in end_costs)
                usage_to_return = {
                    "prompt": end_prompt_val - start_prompt_val,
                    "candidates": end_comp_val - start_comp_val,
                    "total": end_total_val - start_total_val,
                }
                cost_to_return = end_cost_val - start_cost_val

                if resumed_interaction:
                    finish_runtime_interaction_resume(
                        resumed_interaction,
                        result_payload={
                            "telemetry": telemetry_snapshot,
                            "usage": usage_to_return,
                            "cost": cost_to_return,
                            "waiting_for_another_interaction": waiting_for_user_requested,
                        },
                    )
                    emit_agent_event(interaction_state_event(
                        resumed_interaction.to_dict(),
                        status="completed",
                    ))
                emit_agent_event(turn_completed_event(
                    usage=usage_to_return,
                    cost=cost_to_return,
                    telemetry=telemetry_snapshot,
                    resumed_interaction_id=resumed_interaction.id if resumed_interaction else None,
                    duration_ms=max(
                        0,
                        int(round((time.perf_counter() - turn_started_at) * 1000)),
                    ),
                ))

                if str(runtime_policy.get("context_stewardship") or "").lower() == "aggressive":
                    demote_inline_this_turn_results(
                        messages,
                        raw_content_loader=load_raw_context_result_from_artifact,
                    )
                    for msg_dict in new_messages_from_turn:
                        if isinstance(msg_dict, dict) and msg_dict.get("role") == "tool":
                            try:
                                externalize_tool_result_message(
                                    msg_dict,
                                    instance_path=app_obj.instance_path,
                                    session_id=session_id,
                                    created_by_user_id=uid,
                                    commit=False,
                                )
                            except Exception as exc:
                                app_obj.logger.warning(
                                    "Unable to externalize auto-demoted inline_this_turn result: %s",
                                    exc,
                                    exc_info=True,
                                )
                # The user row was committed when the turn was accepted. Patch
                # its optional telemetry now rather than inserting a duplicate.
                if (
                    accepted_user_message_id is not None
                    and user_message_to_save is not None
                ):
                    accepted_user_message = db.session.get(
                        ChatMessage,
                        accepted_user_message_id,
                    )
                    if accepted_user_message is None:
                        raise ContextFabricError(
                            "The accepted user message could not be reloaded."
                        )
                    accepted_user_message.message = user_message_to_save
                    flag_modified(accepted_user_message, "message")

                # The final assistant row was created by finalize_turn. Give
                # debug/tool/event rows timestamps anchored just after the
                # already-durable user row.
                pre_final_messages = []
                if debug_prompt_message:
                    pre_final_messages.append(debug_prompt_message)
                pre_final_messages.extend(
                    msg_dict
                    for msg_dict in new_messages_from_turn
                    if not (
                        isinstance(msg_dict, dict)
                        and msg_dict.get("role") == "agent_event"
                        and isinstance(msg_dict.get("event"), dict)
                        and str(msg_dict["event"].get("id") or "")
                        in durable_event_ids
                    )
                )
                pre_final_created_at = context_fabric_turn.created_at
                for message_index, msg_dict in enumerate(pre_final_messages):
                    db.session.add(ChatMessage(
                        session_id=session_id,
                        message=msg_dict,
                        created_at=(
                            pre_final_created_at
                            + timedelta(microseconds=message_index + 1)
                        ),
                    ))

                session_db_obj = db.session.get(ChatSession, session_id)
                session_db_obj.active_capability_ids = list(set(selected_capability_ids or []))
                session_db_obj.active_plugins = []
                if not session_db_obj.title and original_prompt:
                    session_db_obj.title = (
                        (original_prompt[:60] + '...')
                        if len(original_prompt) > 60
                        else original_prompt
                    )

                commit_with_retry(
                    db.session,
                    operation_name="finalize_chat_history",
                )

                stream_q.put({
                    "type": "final",
                    "response": response_html,
                    "session_id": session_id,
                    "usage_metadata": usage_to_return,
                    "cost": f"{cost_to_return:.6f}",
                    "telemetry": telemetry_snapshot,
                })

            except Exception as e:
                db.session.rollback()
                if turn_id:
                    try:
                        failed_turn = db.session.get(ChatTurn, turn_id)
                        if failed_turn is not None and failed_turn.status != "finalized":
                            mark_turn_failed(
                                turn_id,
                                e,
                                details={"phase": "chat_orchestrator"},
                                commit=True,
                            )
                    except Exception:
                        db.session.rollback()
                        app_obj.logger.exception(
                            "Could not persist failed context-fabric turn %s",
                            turn_id,
                        )
                app_obj.logger.exception("Error during threaded chat processing")
                stream_q.put({
                    "type": "error",
                    "message": public_error_message(e),
                })
            finally:
                if resumed_interaction is not None:
                    try:
                        release_runtime_interaction_resume(
                            resumed_interaction.id,
                            requester_user_id=uid,
                        )
                    except Exception:
                        db.session.rollback()
                        app_obj.logger.exception(
                            "Could not release interrupted runtime interaction %s",
                            resumed_interaction.id,
                        )
                stream_q.put({"type": "DONE"})

    def chat_stream():
        """Yields events from the queue, maintaining HTTP connection with keep-alives."""
        threading.Thread(target=chief_worker).start()
        while True:
            try:
                msg = stream_q.get(timeout=15)
                if msg.get("type") == "DONE":
                    break
                if isinstance(msg, dict) and session_id and "session_id" not in msg:
                    msg = {**msg, "session_id": session_id}
                yield f"data: {json.dumps(msg)}\n\n"
            except queue.Empty:
                yield ": keep-alive\n\n"

    return Response(stream_with_context(chat_stream()), mimetype='text/event-stream')
