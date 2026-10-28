from __future__ import annotations

import copy
import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from iarf_assistant.core.runtime_contracts import provider_identifier_fingerprint


DEFAULT_PREVIEW_CHARS = 800
MAX_INLINE_SUBJECT_ID_CHARS = 80


def stable_subject_id(raw_id: Any, prefix: str = "subject") -> str | None:
    if raw_id is None:
        return None
    value = str(raw_id)
    if len(value) <= MAX_INLINE_SUBJECT_ID_CHARS and all(ch.isprintable() for ch in value):
        return value
    digest = hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:16]
    return f"{prefix}_{digest}"


def bounded_preview(value: Any, max_chars: int = DEFAULT_PREVIEW_CHARS) -> str:
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=True, sort_keys=True, default=str)
        except Exception:
            text = str(value)
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    return text[:max_chars] + "... [truncated]"


def estimated_token_count(value: Any) -> int:
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=True, sort_keys=True, default=str)
        except Exception:
            text = str(value)
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


def serialized_char_count(value: Any) -> int:
    if isinstance(value, str):
        return len(value)
    try:
        return len(json.dumps(value, ensure_ascii=True, sort_keys=True, default=str))
    except Exception:
        return len(str(value))


def result_item_count(value: Any) -> int | None:
    if isinstance(value, (list, tuple, set)):
        return len(value)
    if not isinstance(value, dict):
        return None
    for key in ("result_count", "total_count", "total", "count"):
        raw = value.get(key)
        if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0:
            return raw
    for key in ("items", "results", "records", "rows", "messages", "files", "artifacts"):
        raw = value.get(key)
        if isinstance(raw, list):
            return len(raw)
    return None


def _numeric_cost_from_result(value: Any) -> float | None:
    if not isinstance(value, dict):
        return None
    for key in ("cost", "estimated_cost", "cost_usd"):
        raw = value.get(key)
        try:
            if raw is not None:
                return float(raw)
        except (TypeError, ValueError):
            continue
    usage = value.get("usage")
    return _numeric_cost_from_result(usage) if isinstance(usage, dict) else None


def _runtime_card_type(tool_name: str, result: Any) -> str:
    name = str(tool_name or "").lower()
    payload = result if isinstance(result, dict) else {}
    if name == "delegate_task" or "worker" in name or payload.get("worker_id"):
        return "worker"
    if name.startswith("runs_project_workspace_"):
        return "code"
    if name.startswith("runs_") or payload.get("run_data") or payload.get("workflow_run_id"):
        return "run"
    if (
        "artifact" in name
        or payload.get("artifact_id")
        or isinstance(payload.get("artifact"), dict)
        or isinstance(payload.get("artifacts"), list)
    ):
        return "artifact"
    if payload.get("_code_placeholder") or any(token in name for token in ("code_", "python_", "scratch_")):
        return "code"
    return "tool"


def _runtime_result_references(tool_name: str, result: Any) -> dict[str, Any]:
    if not isinstance(result, dict):
        return {}
    payload = result
    run_data = payload.get("run_data") if isinstance(payload.get("run_data"), dict) else None
    if run_data is None and isinstance(payload.get("run"), dict):
        run_data = payload["run"]
    run_id = payload.get("workflow_run_id") or payload.get("run_id")
    if not run_id and str(tool_name or "").startswith("runs_") and run_data:
        run_id = run_data.get("id") or run_data.get("run_id")
    if not run_id and str(tool_name or "").startswith("runs_"):
        run_id = payload.get("id")

    artifact_ids = []
    for value in (payload.get("artifact_id"),):
        if value:
            artifact_ids.append(str(value))
    artifact = payload.get("artifact")
    if isinstance(artifact, dict) and artifact.get("id"):
        artifact_ids.append(str(artifact["id"]))
    for item in payload.get("artifacts") if isinstance(payload.get("artifacts"), list) else []:
        if isinstance(item, dict) and item.get("id"):
            artifact_ids.append(str(item["id"]))
        elif item:
            artifact_ids.append(str(item))
    for nested_key in ("edit", "process"):
        nested = payload.get(nested_key)
        if not isinstance(nested, dict):
            continue
        for artifact_id in (
            nested.get("artifact_ids")
            if isinstance(nested.get("artifact_ids"), list)
            else []
        ):
            if artifact_id:
                artifact_ids.append(str(artifact_id))
    workspace = payload.get("workspace") if isinstance(payload.get("workspace"), dict) else {}
    process = payload.get("process") if isinstance(payload.get("process"), dict) else {}
    return {
        "run_id": str(run_id) if run_id else None,
        "artifact_ids": list(dict.fromkeys(artifact_ids))[:20],
        "worker_id": str(payload.get("worker_id")) if payload.get("worker_id") else None,
        "workspace_id": str(workspace.get("id")) if workspace.get("id") else None,
        "process_id": str(process.get("id")) if process.get("id") else None,
    }


def make_agent_event(
    *,
    kind: str,
    phase: str,
    status: str,
    title: str,
    summary: str = "",
    subject_type: str | None = None,
    subject_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    event = {
        "schema_version": 1,
        "id": f"evt_{uuid.uuid4().hex}",
        "kind": kind,
        "phase": phase,
        "status": status,
        "title": title,
        "summary": summary,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "payload": payload or {},
    }
    if subject_type or subject_id:
        event["subject"] = {
            "type": subject_type or "unknown",
            "id": subject_id,
        }
    return {"type": "agent_event", "event": event}


def agent_event_chat_message(event_packet: dict[str, Any]) -> dict[str, Any]:
    event = agent_event_from_packet(event_packet)
    return {
        "role": "agent_event",
        "event": event,
    }


def agent_event_from_packet(event_packet: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(event_packet, dict):
        raise ValueError("Expected an agent event packet.")
    if event_packet.get("type") == "agent_event":
        event = event_packet.get("event")
    else:
        event = event_packet
    if not isinstance(event, dict) or not event.get("kind"):
        raise ValueError("Agent event packet is missing an event object.")
    return event


def append_agent_event_chat_message(
    messages: list[dict[str, Any]],
    event_packet: dict[str, Any],
    *,
    logger: Any = None,
) -> bool:
    try:
        messages.append(agent_event_chat_message(event_packet))
        return True
    except ValueError:
        if logger is not None:
            logger.warning("Skipping malformed agent event packet: %r", event_packet)
        return False


def emit_agent_event_packet(
    event_packet: dict[str, Any],
    *,
    stream_put: Callable[[dict[str, Any]], Any] | None = None,
    chat_messages: list[dict[str, Any]] | None = None,
    logger: Any = None,
) -> bool:
    if stream_put is not None:
        stream_put(event_packet)
    if chat_messages is None:
        return True
    return append_agent_event_chat_message(chat_messages, event_packet, logger=logger)


def append_agent_event_to_metadata(
    metadata: dict[str, Any] | None,
    event_packet: dict[str, Any],
    *,
    event_key: str = "runtime_events",
    limit: int = 100,
) -> tuple[dict[str, Any], bool]:
    try:
        event = agent_event_from_packet(event_packet)
    except ValueError:
        return copy.deepcopy(metadata) if isinstance(metadata, dict) else {}, False

    updated = copy.deepcopy(metadata) if isinstance(metadata, dict) else {}
    events = updated.get(event_key) if isinstance(updated.get(event_key), list) else []
    events = [*events, event]
    updated[event_key] = events[-max(1, int(limit or 100)):]
    return updated, True


def append_agent_event_to_metadata_attr(
    target: Any,
    event_packet: dict[str, Any],
    *,
    metadata_attr: str = "metadata_json",
    event_key: str = "runtime_events",
    limit: int = 100,
) -> bool:
    metadata = getattr(target, metadata_attr, None)
    updated, appended = append_agent_event_to_metadata(
        metadata,
        event_packet,
        event_key=event_key,
        limit=limit,
    )
    if appended:
        setattr(target, metadata_attr, updated)
    return appended


def assistant_message_event(message: dict[str, Any], loop_index: int | None = None) -> dict[str, Any]:
    content = message.get("content") or ""
    tool_calls = message.get("tool_calls") or []
    tool_names = []
    for tool_call in tool_calls:
        if not isinstance(tool_call, dict):
            continue
        function = tool_call.get("function") or {}
        name = function.get("name")
        if name:
            tool_names.append(name)

    if tool_names:
        title = "Assistant requested tools"
        summary = ", ".join(tool_names[:5])
        if len(tool_names) > 5:
            summary += f", +{len(tool_names) - 5} more"
    else:
        title = "Assistant message"
        summary = bounded_preview(content, 240) if content else ""

    return make_agent_event(
        kind="assistant_message.generated",
        phase="assistant",
        status="completed",
        title=title,
        summary=summary,
        subject_type="assistant_message",
        payload={
            "loop_index": loop_index,
            "content_preview": bounded_preview(content, 600) if content else "",
            "has_visible_content": bool(str(content).strip()),
            "estimated_content_tokens": estimated_token_count(content) if content else 0,
            "tool_call_count": len(tool_calls),
            "tool_names": tool_names,
        },
    )


def tool_call_started_event(
    *,
    tool_call_id: Any,
    provider_tool_call_id: Any = None,
    tool_name: str,
    arguments: dict[str, Any] | None = None,
    loop_index: int | None = None,
) -> dict[str, Any]:
    subject_id = stable_subject_id(tool_call_id, prefix="tool_call")
    return make_agent_event(
        kind="tool_call.started",
        phase="tool_call",
        status="running",
        title=f"Running {tool_name}",
        summary=tool_name,
        subject_type="tool_call",
        subject_id=subject_id,
        payload={
            "loop_index": loop_index,
            "tool_name": tool_name,
            "tool_call_id": subject_id,
            "provider_tool_call_fingerprint": provider_identifier_fingerprint(
                provider_tool_call_id
            ),
            "provider_tool_call_id_chars": len(str(provider_tool_call_id or "")),
            "arguments_preview": bounded_preview(arguments or {}, 1000),
        },
    )


def tool_call_finished_event(
    *,
    tool_call_id: Any,
    provider_tool_call_id: Any = None,
    tool_name: str,
    action: str,
    result: Any,
    result_envelope: dict[str, Any] | None = None,
    loop_index: int | None = None,
    duration_ms: int | None = None,
) -> dict[str, Any]:
    subject_id = stable_subject_id(tool_call_id, prefix="tool_call")
    approval_payload = _approval_payload_from_result(result)
    user_interaction_payload = _user_interaction_payload_from_result(result)
    runtime_monitor_payload = _runtime_monitor_payload_from_result(result)
    is_blocked = isinstance(result, dict) and str(result.get("status") or "").lower() == "blocked"
    is_waiting_for_approval = approval_payload is not None
    is_waiting_for_user = user_interaction_payload is not None
    is_runtime_monitor = runtime_monitor_payload is not None
    is_error = not is_blocked and (action == "error" or (isinstance(result, dict) and bool(result.get("error"))))
    if is_waiting_for_approval:
        status = "warning"
        kind = "tool_call.awaiting_approval"
        title = f"Approval requested by {tool_name}"
        summary = approval_payload.get("message") or f"{tool_name} is waiting for approval."
    elif is_waiting_for_user:
        status = "warning"
        kind = "tool_call.awaiting_user"
        title = f"User input requested by {tool_name}"
        summary = user_interaction_payload.get("question") or user_interaction_payload.get("message") or f"{tool_name} is waiting for the user."
    elif is_runtime_monitor:
        status = "completed"
        kind = "runtime.self_nudge.scheduled"
        title = "Self-nudge scheduled"
        summary = runtime_monitor_payload.get("reason") or runtime_monitor_payload.get("message") or f"{tool_name} scheduled a runtime monitor."
    elif is_blocked:
        status = "warning"
        kind = "tool_call.blocked"
        policy = result.get("policy") or "runtime_policy"
        title = f"Blocked {tool_name}"
        summary = f"{tool_name} - {policy}"
    else:
        status = "error" if is_error else "completed"
        kind = "tool_call.failed" if is_error else "tool_call.completed"
        title = f"{'Failed' if is_error else 'Completed'} {tool_name}"
        summary = tool_name
    return make_agent_event(
        kind=kind,
        phase="tool_call",
        status=status,
        title=title,
        summary=summary,
        subject_type="tool_call",
        subject_id=subject_id,
        payload={
            "loop_index": loop_index,
            "tool_name": tool_name,
            "tool_call_id": subject_id,
            "provider_tool_call_fingerprint": provider_identifier_fingerprint(
                provider_tool_call_id
            ),
            "provider_tool_call_id_chars": len(str(provider_tool_call_id or "")),
            "action": action,
            "duration_ms": duration_ms,
            "policy": result.get("policy") if isinstance(result, dict) else None,
            "raw_result_chars": serialized_char_count(result),
            "estimated_raw_result_tokens": estimated_token_count(result),
            "result_count": result_item_count(result),
            "estimated_cost": _numeric_cost_from_result(result),
            "card_type": _runtime_card_type(tool_name, result),
            "runtime_references": _runtime_result_references(tool_name, result),
            "result_envelope": copy.deepcopy(result_envelope)
            if isinstance(result_envelope, dict)
            else None,
            "result_preview": bounded_preview(result, 1000),
            "approval": approval_payload,
            "user_interaction": user_interaction_payload,
            "runtime_monitor": runtime_monitor_payload,
        },
    )


def safe_context_optimization_payload(meta: Any) -> dict[str, Any]:
    if not isinstance(meta, dict):
        return {}
    return {
        str(key): value
        for key, value in meta.items()
        if key != "raw_content"
    }


def tool_call_context_event(
    *,
    tool_call_id: Any,
    tool_name: str,
    context_optimization: dict[str, Any] | None,
    loop_index: int | None = None,
) -> dict[str, Any]:
    subject_id = stable_subject_id(tool_call_id, prefix="tool_call")
    context_payload = safe_context_optimization_payload(context_optimization)
    retention_state = context_payload.get("retention_state") or "inline_keep"
    raw_chars = int(context_payload.get("raw_chars") or 0)
    raw_tokens = int(context_payload.get("estimated_raw_tokens") or 0)
    included_chars = int(context_payload.get("included_chars") or 0)
    included_tokens = int(context_payload.get("estimated_included_tokens") or 0)
    summary = (
        f"{tool_name} - retention: {retention_state}; "
        f"in context: {included_chars} chars / ~{included_tokens} tokens"
    )
    return make_agent_event(
        kind="tool_call.context_retained",
        phase="tool_call",
        status="completed",
        title=f"Completed {tool_name}",
        summary=summary,
        subject_type="tool_call",
        subject_id=subject_id,
        payload={
            "loop_index": loop_index,
            "tool_name": tool_name,
            "tool_call_id": subject_id,
            "retention_state": retention_state,
            "context_ref_id": context_payload.get("ref_id"),
            "raw_result_chars": raw_chars,
            "estimated_raw_result_tokens": raw_tokens,
            "included_context_chars": included_chars,
            "estimated_included_context_tokens": included_tokens,
            "context_optimization": context_payload,
        },
    )


def source_coverage_updated_event(
    *,
    tool_call_id: Any,
    tool_name: str,
    updated_count: int,
    ledger_counts: dict[str, Any] | None = None,
    loop_index: int | None = None,
    automatic: bool = True,
) -> dict[str, Any]:
    subject_id = stable_subject_id(tool_call_id, prefix="tool_call")
    count = max(0, int(updated_count or 0))
    return make_agent_event(
        kind="source_coverage.updated",
        phase="source_coverage",
        status="completed",
        title="Updated source coverage",
        summary=f"Recorded {count} source coverage item(s) from {tool_name}.",
        subject_type="tool_call",
        subject_id=subject_id,
        payload={
            "loop_index": loop_index,
            "tool_name": tool_name,
            "tool_call_id": subject_id,
            "updated_count": count,
            "ledger_counts": ledger_counts or {},
            "automatic": bool(automatic),
        },
    )


def _approval_payload_from_result(result: Any) -> dict[str, Any] | None:
    if not isinstance(result, dict):
        return None
    if str(result.get("status") or "").lower() != "waiting_for_approval":
        return None
    approval_id = result.get("approval_id")
    if not approval_id:
        return None
    return {
        "id": str(approval_id),
        "approval_id": str(approval_id),
        "status": "pending",
        "subject_type": result.get("subject_type") or result.get("approval_subject_type") or "runtime_action",
        "tool_id": result.get("tool_id"),
        "approval_url": result.get("approval_url"),
        "message": result.get("message"),
        "interaction_id": result.get("interaction_id"),
        "interaction_version": result.get("interaction_version"),
        "interaction_status": result.get("interaction_status") or "pending",
        "requested_scope": result.get("requested_scope") if isinstance(result.get("requested_scope"), dict) else None,
        "preview": {
            "reason": result.get("reason"),
            "requested_scope": result.get("requested_scope") if isinstance(result.get("requested_scope"), dict) else None,
            "requested_scope_original": result.get("requested_scope_original") if isinstance(result.get("requested_scope_original"), dict) else None,
            "scope_normalization": result.get("scope_normalization") if isinstance(result.get("scope_normalization"), dict) else None,
            "current_scope": result.get("current_scope") if isinstance(result.get("current_scope"), dict) else None,
        },
    }


def _user_interaction_payload_from_result(result: Any) -> dict[str, Any] | None:
    if not isinstance(result, dict):
        return None
    if str(result.get("status") or "").lower() != "waiting_for_user":
        return None
    interaction_type = str(result.get("interaction_type") or "").strip()
    if interaction_type not in {"choice_question", "review_decision", "structured_input"}:
        return None
    question = str(result.get("question") or "").strip()
    raw_choices = result.get("choices")
    choices = [choice for choice in raw_choices if isinstance(choice, dict)] if isinstance(raw_choices, list) else []
    if not question or (interaction_type != "structured_input" and len(choices) < 2):
        return None
    question_id = str(result.get("question_id") or "").strip()
    return {
        "interaction_type": interaction_type,
        "interaction_id": result.get("interaction_id"),
        "interaction_version": result.get("interaction_version"),
        "status": result.get("interaction_status") or "pending",
        "question_id": question_id,
        "question": question,
        "choices": choices[:6],
        "reason": result.get("reason") or "",
        "allow_free_text": bool(result.get("allow_free_text")),
        "input_label": result.get("input_label") or "Answer",
        "input_placeholder": result.get("input_placeholder") or "",
        "input_type": result.get("input_type") or "text",
        "required": result.get("required", True) is not False,
        "preview": result.get("preview") if isinstance(result.get("preview"), dict) else {},
        "message": result.get("message") or "",
    }


def _runtime_monitor_payload_from_result(result: Any) -> dict[str, Any] | None:
    if not isinstance(result, dict):
        return None
    if str(result.get("status") or "").lower() != "scheduled":
        return None
    if str(result.get("monitor_type") or "").strip() != "self_nudge":
        return None
    monitor_id = str(result.get("monitor_id") or "").strip()
    reason = str(result.get("reason") or "").strip()
    due_at = str(result.get("due_at") or "").strip()
    if not monitor_id or not reason or not due_at:
        return None
    return {
        "monitor_id": monitor_id,
        "monitor_type": "self_nudge",
        "status": result.get("status"),
        "due_at": due_at,
        "due_in_seconds": result.get("due_in_seconds"),
        "reason": reason,
        "state_request": result.get("state_request") or "",
        "checkpoint_type": result.get("checkpoint_type") or "",
        "chat_session_id": result.get("chat_session_id"),
        "requester_user_id": result.get("requester_user_id"),
        "message": result.get("message") or "",
    }


def tool_budget_exhausted_event(
    *,
    max_tool_calls: int,
    loop_index: int | None = None,
) -> dict[str, Any]:
    return make_agent_event(
        kind="turn.tool_budget.exhausted",
        phase="turn",
        status="warning",
        title="Tool budget reached",
        summary=f"Stopped after {max_tool_calls} tool-call iterations.",
        subject_type="turn",
        payload={
            "loop_index": loop_index,
            "max_tool_calls": max_tool_calls,
        },
    )


def tool_budget_warning_event(
    *,
    max_tool_calls: int,
    remaining_tool_calls: int,
    loop_index: int | None = None,
) -> dict[str, Any]:
    return make_agent_event(
        kind="turn.tool_budget.warning",
        phase="turn",
        status="warning",
        title="Tool budget nearly reached",
        summary=f"{remaining_tool_calls} tool-call iterations remaining.",
        subject_type="turn",
        payload={
            "loop_index": loop_index,
            "max_tool_calls": max_tool_calls,
            "remaining_tool_calls": remaining_tool_calls,
        },
    )


def tool_budget_extended_event(
    *,
    added_tool_calls: int,
    new_max_tool_calls: int,
    loop_index: int | None = None,
    reason: str = "",
) -> dict[str, Any]:
    return make_agent_event(
        kind="turn.tool_budget.extended",
        phase="turn",
        status="completed",
        title="Tool budget extended",
        summary=f"Added {added_tool_calls} tool-call iteration(s).",
        subject_type="turn",
        payload={
            "loop_index": loop_index,
            "added_tool_calls": added_tool_calls,
            "new_max_tool_calls": new_max_tool_calls,
            "reason": bounded_preview(reason, 600) if reason else "",
        },
    )


def finalization_event(
    *,
    status: str,
    reason: str,
    loop_index: int | None = None,
    content_preview: str = "",
) -> dict[str, Any]:
    if status == "running":
        title = "Requesting final answer"
        summary = reason.replace("_", " ")
        kind = "turn.finalization.started"
    elif status == "completed":
        title = "Final answer recovered"
        summary = bounded_preview(content_preview, 240) if content_preview else reason.replace("_", " ")
        kind = "turn.finalization.completed"
    else:
        title = "Final answer recovery failed"
        summary = reason.replace("_", " ")
        kind = "turn.finalization.failed"

    return make_agent_event(
        kind=kind,
        phase="turn",
        status=status,
        title=title,
        summary=summary,
        subject_type="turn",
        payload={
            "loop_index": loop_index,
            "reason": reason,
            "content_preview": bounded_preview(content_preview, 600) if content_preview else "",
        },
    )


def context_compaction_retry_event(
    *,
    compacted_count: int,
    error: str = "",
    loop_index: int | None = None,
    compacted_refs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    count = max(0, int(compacted_count or 0))
    return make_agent_event(
        kind="turn.context_compaction_retry",
        phase="turn",
        status="warning",
        title="Retried after context overflow",
        summary=f"Compacted {count} tool result(s) and retried the model call.",
        subject_type="turn",
        payload={
            "loop_index": loop_index,
            "compacted_count": count,
            "compacted_refs": compacted_refs or [],
            "error": bounded_preview(error, 600) if error else "",
        },
    )


def context_history_projection_retry_event(
    *,
    history_projection: dict[str, Any],
    error: str = "",
    loop_index: int | None = None,
) -> dict[str, Any]:
    facts = (
        copy.deepcopy(history_projection)
        if isinstance(history_projection, dict)
        else {}
    )
    omitted = max(0, int(facts.get("omitted_message_count") or 0))
    return make_agent_event(
        kind="turn.context_history_projection_retry",
        phase="turn",
        status="warning",
        title="Retried with bounded history projection",
        summary=(
            f"Omitted {omitted} older message(s) from the provider view and "
            "retried once; raw messages remain durable."
        ),
        subject_type="turn",
        payload={
            "loop_index": loop_index,
            "history_projection": facts,
            "error": bounded_preview(error, 600) if error else "",
        },
    )


def output_repair_event(
    *,
    status: str,
    reason: str,
    loop_index: int | None = None,
    content_preview: str = "",
    error: str = "",
) -> dict[str, Any]:
    if status == "running":
        title = "Repairing structured output"
        summary = reason.replace("_", " ")
        kind = "turn.output_repair.started"
    elif status == "completed":
        title = "Structured output repaired"
        summary = bounded_preview(content_preview, 240) if content_preview else reason.replace("_", " ")
        kind = "turn.output_repair.completed"
    else:
        title = "Structured output repair failed"
        summary = bounded_preview(error, 240) if error else reason.replace("_", " ")
        kind = "turn.output_repair.failed"

    return make_agent_event(
        kind=kind,
        phase="turn",
        status=status,
        title=title,
        summary=summary,
        subject_type="turn",
        payload={
            "loop_index": loop_index,
            "reason": reason,
            "content_preview": bounded_preview(content_preview, 600) if content_preview else "",
            "error": bounded_preview(error, 600) if error else "",
        },
    )


def runtime_policy_event(policy: dict[str, Any] | None) -> dict[str, Any]:
    payload = policy if isinstance(policy, dict) else {}
    profile = str(payload.get("profile") or "review")
    full_access = bool(payload.get("disable_tool_approvals"))
    return make_agent_event(
        kind="runtime.policy.applied",
        phase="turn",
        status="completed",
        title="Full access active" if full_access else "Review policy active",
        summary=(
            "Configured approval interruptions are bypassed; actions remain visible."
            if full_access
            else "Permission-sensitive actions pause for inline review."
        ),
        subject_type="runtime_policy",
        subject_id=profile,
        payload={
            "profile": profile,
            "approval_profile": payload.get("approval_profile"),
            "disable_tool_approvals": full_access,
            "max_tool_calls": payload.get("max_tool_calls"),
            "context_stewardship": payload.get("context_stewardship"),
        },
    )


def interaction_state_event(
    interaction: dict[str, Any],
    *,
    status: str,
    summary: str = "",
) -> dict[str, Any]:
    interaction_id = str(interaction.get("id") or interaction.get("interaction_id") or "")
    interaction_type = str(interaction.get("interaction_type") or "interaction")
    labels = {
        "resolved": "Response saved",
        "resuming": "Resuming chat",
        "completed": "Chat resumed",
        "cancelled": "Interaction cancelled",
        "expired": "Interaction expired",
    }
    event_status = "error" if status in {"cancelled", "expired"} else "running" if status == "resuming" else "completed"
    return make_agent_event(
        kind=f"interaction.{status}",
        phase="interaction",
        status=event_status,
        title=labels.get(status, f"Interaction {status}"),
        summary=summary or interaction_type.replace("_", " "),
        subject_type="interaction",
        subject_id=interaction_id or None,
        payload={
            "interaction_id": interaction_id,
            "interaction_type": interaction_type,
            "interaction_status": status,
            "version": interaction.get("version"),
            "response": interaction.get("response") if isinstance(interaction.get("response"), dict) else {},
            "tool_call_id": interaction.get("tool_call_id"),
        },
    )


def turn_completed_event(
    *,
    usage: dict[str, Any] | None,
    cost: float | None,
    telemetry: dict[str, Any] | None = None,
    resumed_interaction_id: str | None = None,
    duration_ms: int | None = None,
) -> dict[str, Any]:
    usage_payload = usage if isinstance(usage, dict) else {}
    total_tokens = int(usage_payload.get("total") or 0)
    summary_parts = [f"{total_tokens} tokens"] if total_tokens else []
    return make_agent_event(
        kind="turn.completed",
        phase="turn",
        status="completed",
        title="Turn complete",
        summary=" | ".join(summary_parts),
        subject_type="turn",
        payload={
            "usage": usage_payload,
            "cost": float(cost) if cost is not None else None,
            "telemetry": telemetry if isinstance(telemetry, dict) else {},
            "resumed_interaction_id": resumed_interaction_id,
            "duration_ms": duration_ms,
        },
    )


def turn_failed_event(
    *,
    turn_id: str,
    message: str,
    phase: str = "chat_orchestrator",
) -> dict[str, Any]:
    packet = make_agent_event(
        kind="turn.failed",
        phase="turn",
        status="failed",
        title="Response interrupted",
        summary=bounded_preview(message, 320),
        subject_type="turn",
        subject_id=turn_id,
        payload={
            "turn_id": turn_id,
            "failure_phase": phase,
        },
    )
    # Failure persistence can be retried. A stable event ID lets history
    # reconciliation collapse any legacy duplicate records safely.
    packet["event"]["id"] = f"turn:{turn_id}:failed"
    return packet
