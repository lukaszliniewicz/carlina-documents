from __future__ import annotations

import copy
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import or_

from iarf_assistant.chat_execution_helpers import build_tool_result_message
from carlina.core.utc import utc_now_db
from iarf_assistant.extensions import db
from iarf_assistant.models import RuntimeApprovalRequest, RuntimeInteraction


INTERACTION_STATUSES = {
    "pending",
    "resolved",
    "resuming",
    "completed",
    "cancelled",
    "expired",
}
INTERACTION_TYPES = {
    "choice_question",
    "permission",
    "scope_expansion",
    "review_decision",
    "structured_input",
}
APPROVAL_INTERACTION_TYPES = {"permission", "scope_expansion"}
REVIEW_DECISIONS = {"approved", "changes_requested", "rejected"}
STALE_RESUME_AFTER = timedelta(minutes=5)
MAX_RESPONSE_CHARS = 4000


def _utcnow() -> datetime:
    return utc_now_db()


def _text(value: Any, limit: int = MAX_RESPONSE_CHARS) -> str:
    normalized = " ".join(str(value or "").split())
    return normalized[: max(0, int(limit or 0))]


def _dict(value: Any) -> dict[str, Any]:
    return copy.deepcopy(value) if isinstance(value, dict) else {}


def _list_of_dicts(value: Any, *, limit: int = 12) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [copy.deepcopy(item) for item in value if isinstance(item, dict)][:limit]


def _normalize_interaction_type(value: Any) -> str:
    normalized = str(value or "choice_question").strip().lower().replace("-", "_")
    aliases = {
        "choice": "choice_question",
        "review": "review_decision",
        "preview": "review_decision",
        "text": "structured_input",
        "input": "structured_input",
        "approval": "permission",
    }
    normalized = aliases.get(normalized, normalized)
    if normalized not in INTERACTION_TYPES:
        raise ValueError(f"Unsupported interaction type: {value}")
    return normalized


def _normalize_decision(value: Any, *, review: bool = False) -> str:
    normalized = str(value or "").strip().lower().replace("-", "_")
    aliases = {
        "approve": "approved",
        "yes": "approved",
        "allow": "approved",
        "reject": "rejected",
        "deny": "rejected",
        "no": "rejected",
        "request_changes": "changes_requested",
        "changes": "changes_requested",
        "cancel": "cancelled",
    }
    normalized = aliases.get(normalized, normalized)
    allowed = REVIEW_DECISIONS if review else {"approved", "rejected", "cancelled"}
    if normalized not in allowed:
        raise ValueError("A valid interaction decision is required.")
    return normalized


def normalize_interaction_response(
    interaction: RuntimeInteraction,
    response: Any,
) -> dict[str, Any]:
    payload = _dict(response)
    interaction_type = interaction.interaction_type
    request_payload = _dict(interaction.request_payload)

    if interaction_type == "choice_question":
        choices = _list_of_dicts(request_payload.get("choices"), limit=6)
        choice_id = _text(payload.get("choice_id"), 120)
        choice_value = _text(payload.get("value"), 500)
        selected = next(
            (
                choice
                for choice in choices
                if (choice_id and str(choice.get("id") or "") == choice_id)
                or (choice_value and str(choice.get("value") or "") == choice_value)
            ),
            None,
        )
        if selected:
            return {
                "choice_id": str(selected.get("id") or ""),
                "label": _text(selected.get("label"), 160),
                "value": _text(selected.get("value") or selected.get("label"), 500),
            }
        free_text = _text(payload.get("text") or payload.get("value"))
        if request_payload.get("allow_free_text") and free_text:
            return {"text": free_text, "value": free_text}
        raise ValueError("Choose one of the available answers.")

    if interaction_type == "structured_input":
        value = _text(payload.get("value") if "value" in payload else payload.get("text"))
        if not value and bool(request_payload.get("required", True)):
            raise ValueError("A response value is required.")
        return {"value": value}

    if interaction_type == "review_decision":
        return {
            "decision": _normalize_decision(payload.get("decision"), review=True),
            "note": _text(payload.get("note")),
        }

    if interaction_type in APPROVAL_INTERACTION_TYPES:
        return {
            "decision": _normalize_decision(payload.get("decision")),
            "note": _text(payload.get("note")),
        }

    raise ValueError(f"Unsupported interaction type: {interaction_type}")


def create_runtime_interaction(
    *,
    requester_user_id: int,
    chat_session_id: str,
    interaction_type: str,
    request_payload: dict[str, Any],
    resume_payload: dict[str, Any] | None = None,
    tool_call_id: str | None = None,
    tool_name: str | None = None,
    approval_id: str | None = None,
    workflow_run_id: str | None = None,
    expires_at: datetime | None = None,
    commit: bool = True,
) -> RuntimeInteraction:
    normalized_type = _normalize_interaction_type(interaction_type)
    tool_call_id = str(tool_call_id or "").strip() or None
    if tool_call_id:
        existing = RuntimeInteraction.query.filter_by(
            chat_session_id=chat_session_id,
            tool_call_id=tool_call_id,
        ).first()
        if existing:
            return existing

    record = RuntimeInteraction(
        id=str(uuid.uuid4()),
        requester_user_id=requester_user_id,
        chat_session_id=chat_session_id,
        workflow_run_id=workflow_run_id,
        approval_id=approval_id,
        tool_call_id=tool_call_id,
        tool_name=str(tool_name or "").strip() or None,
        interaction_type=normalized_type,
        request_payload=_dict(request_payload),
        resume_payload=_dict(resume_payload),
        status="pending",
        version=1,
        expires_at=expires_at,
    )
    db.session.add(record)
    if commit:
        db.session.commit()
    else:
        db.session.flush()
    return record


def create_runtime_interaction_from_tool_result(
    result: Any,
    *,
    requester_user_id: int,
    chat_session_id: str,
    tool_call_id: str,
    tool_name: str,
    tool_arguments: dict[str, Any] | None = None,
    resume_payload: dict[str, Any] | None = None,
) -> tuple[Any, RuntimeInteraction | None]:
    if not isinstance(result, dict):
        return result, None

    status = str(result.get("status") or "").strip().lower()
    interaction_type = None
    request_payload: dict[str, Any] = {}
    approval_id = None
    workflow_run_id = result.get("workflow_run_id")

    if status == "waiting_for_user":
        interaction_type = _normalize_interaction_type(result.get("interaction_type"))
        request_payload = {
            "question_id": result.get("question_id"),
            "question": result.get("question"),
            "choices": _list_of_dicts(result.get("choices"), limit=6),
            "reason": result.get("reason") or "",
            "allow_free_text": bool(result.get("allow_free_text")),
            "input_label": result.get("input_label") or "Answer",
            "input_placeholder": result.get("input_placeholder") or "",
            "input_type": result.get("input_type") or "text",
            "required": result.get("required", True) is not False,
            "preview": _dict(result.get("preview")),
        }
    elif status == "waiting_for_approval" and result.get("approval_id"):
        approval_id = str(result.get("approval_id"))
        approval = db.session.get(RuntimeApprovalRequest, approval_id)
        subject_type = (
            str(result.get("subject_type") or "").strip()
            or (approval.subject_type if approval else "")
        )
        interaction_type = "scope_expansion" if subject_type == "scope_expansion" else "permission"
        approval_preview = _dict(approval.preview_payload if approval else result.get("preview"))
        request_payload = {
            "question": (
                "Allow this scope expansion?"
                if interaction_type == "scope_expansion"
                else f"Allow {tool_name} to continue?"
            ),
            "message": result.get("message") or result.get("error") or "",
            "subject_type": subject_type or "runtime_action",
            "tool_id": result.get("tool_id") or (approval.tool_id if approval else None),
            "effects": list(approval.effects or []) if approval else [],
            "preview": approval_preview,
            "requested_scope": _dict(result.get("requested_scope")),
        }
    else:
        return result, None

    durable_resume = {
        **_dict(resume_payload),
        "tool_arguments": _dict(tool_arguments),
        "tool_name": tool_name,
    }
    record = create_runtime_interaction(
        requester_user_id=requester_user_id,
        chat_session_id=chat_session_id,
        interaction_type=interaction_type,
        request_payload=request_payload,
        resume_payload=durable_resume,
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        approval_id=approval_id,
        workflow_run_id=workflow_run_id,
    )
    augmented = {
        **result,
        "interaction_id": record.id,
        "interaction_version": record.version,
        "interaction_status": record.status,
    }
    return augmented, record


def get_runtime_interaction_for_user(
    interaction_id: str,
    *,
    requester_user_id: int,
    chat_session_id: str | None = None,
) -> RuntimeInteraction:
    record = db.session.get(RuntimeInteraction, interaction_id)
    if (
        not record
        or int(record.requester_user_id) != int(requester_user_id)
        or (chat_session_id and record.chat_session_id != chat_session_id)
    ):
        raise KeyError(f"Runtime interaction '{interaction_id}' not found.")
    return record


def resolve_runtime_interaction(
    interaction_id: str,
    response: Any,
    *,
    resolver_user_id: int,
    expected_version: int | None = None,
    commit: bool = True,
) -> RuntimeInteraction:
    record = get_runtime_interaction_for_user(
        interaction_id,
        requester_user_id=resolver_user_id,
    )
    now = _utcnow()
    if record.expires_at and record.expires_at <= now:
        if record.status == "pending":
            record.status = "expired"
            record.resolved_at = now
            record.updated_at = now
            record.version += 1
            db.session.commit()
        raise ValueError(f"Runtime interaction '{interaction_id}' is expired.")
    if record.status != "pending":
        raise ValueError(f"Runtime interaction '{interaction_id}' is already {record.status}.")

    version = int(expected_version if expected_version is not None else record.version)
    normalized_response = normalize_interaction_response(record, response)
    updated = RuntimeInteraction.query.filter_by(
        id=record.id,
        requester_user_id=resolver_user_id,
        status="pending",
        version=version,
    ).update(
        {
            RuntimeInteraction.status: "resolved",
            RuntimeInteraction.resolver_user_id: resolver_user_id,
            RuntimeInteraction.response_payload: normalized_response,
            RuntimeInteraction.resolved_at: now,
            RuntimeInteraction.updated_at: now,
            RuntimeInteraction.version: RuntimeInteraction.version + 1,
        },
        synchronize_session=False,
    )
    if updated != 1:
        db.session.rollback()
        raise ValueError("This interaction changed before the response was saved. Reload and try again.")

    if record.approval_id and record.interaction_type in APPROVAL_INTERACTION_TYPES:
        from iarf_assistant.core.runtime_state import resolve_approval_request

        resolve_approval_request(
            record.approval_id,
            normalized_response["decision"],
            approver_user_id=resolver_user_id,
            note=normalized_response.get("note"),
            commit=False,
        )

    if commit:
        db.session.commit()
    else:
        db.session.flush()
        db.session.expire(record)
    return db.session.get(RuntimeInteraction, interaction_id)


def claim_runtime_interaction_resume(
    interaction_id: str,
    *,
    requester_user_id: int,
    chat_session_id: str,
) -> RuntimeInteraction:
    record = get_runtime_interaction_for_user(
        interaction_id,
        requester_user_id=requester_user_id,
        chat_session_id=chat_session_id,
    )
    if not record.response_payload:
        raise ValueError("Answer this interaction before resuming the chat.")

    now = _utcnow()
    stale_before = now - STALE_RESUME_AFTER
    updated = RuntimeInteraction.query.filter(
        RuntimeInteraction.id == record.id,
        RuntimeInteraction.requester_user_id == requester_user_id,
        RuntimeInteraction.chat_session_id == chat_session_id,
        or_(
            RuntimeInteraction.status == "resolved",
            (
                (RuntimeInteraction.status == "resuming")
                & or_(
                    RuntimeInteraction.resume_started_at.is_(None),
                    RuntimeInteraction.resume_started_at < stale_before,
                )
            ),
        ),
    ).update(
        {
            RuntimeInteraction.status: "resuming",
            RuntimeInteraction.resume_started_at: now,
            RuntimeInteraction.updated_at: now,
            RuntimeInteraction.version: RuntimeInteraction.version + 1,
        },
        synchronize_session=False,
    )
    if updated != 1:
        db.session.rollback()
        fresh = db.session.get(RuntimeInteraction, interaction_id)
        state = fresh.status if fresh else "missing"
        raise ValueError(f"Runtime interaction '{interaction_id}' cannot resume from state '{state}'.")
    db.session.commit()
    return db.session.get(RuntimeInteraction, interaction_id)


def finish_runtime_interaction_resume(
    interaction: RuntimeInteraction,
    *,
    result_payload: dict[str, Any] | None = None,
    commit: bool = False,
) -> RuntimeInteraction:
    now = _utcnow()
    interaction.status = "completed"
    interaction.result_payload = _dict(result_payload)
    interaction.completed_at = now
    interaction.updated_at = now
    interaction.version += 1
    if commit:
        db.session.commit()
    return interaction


def release_runtime_interaction_resume(
    interaction_id: str,
    *,
    requester_user_id: int,
) -> RuntimeInteraction | None:
    now = _utcnow()
    updated = RuntimeInteraction.query.filter_by(
        id=interaction_id,
        requester_user_id=requester_user_id,
        status="resuming",
    ).update(
        {
            RuntimeInteraction.status: "resolved",
            RuntimeInteraction.resume_started_at: None,
            RuntimeInteraction.updated_at: now,
            RuntimeInteraction.version: RuntimeInteraction.version + 1,
        },
        synchronize_session=False,
    )
    if updated:
        db.session.commit()
    return db.session.get(RuntimeInteraction, interaction_id)


def resolved_tool_result_payload(interaction: RuntimeInteraction) -> dict[str, Any]:
    payload = {
        "status": "interaction_resolved",
        "interaction_id": interaction.id,
        "interaction_type": interaction.interaction_type,
        "response": _dict(interaction.response_payload),
    }
    if interaction.approval_id:
        payload["approval_id"] = interaction.approval_id
    if interaction.interaction_type in APPROVAL_INTERACTION_TYPES:
        decision = payload["response"].get("decision")
        payload["status"] = decision
        if decision == "approved":
            payload["instruction"] = (
                "Retry the blocked tool call with the approved _approval_id."
                if interaction.interaction_type == "permission"
                else "Continue with the approved expanded scope."
            )
    return payload


def replace_pending_tool_result(
    messages: list[dict[str, Any]],
    interaction: RuntimeInteraction,
) -> dict[str, Any] | None:
    target = str(interaction.tool_call_id or "")
    if not target:
        return None
    for message in reversed(messages):
        if (
            isinstance(message, dict)
            and message.get("role") == "tool"
            and str(message.get("tool_call_id") or "") == target
        ):
            replacement = build_tool_result_message(
                session_id=interaction.chat_session_id,
                tool_call_id=interaction.tool_call_id,
                provider_tool_call_id=message.get("_provider_tool_call_id"),
                tool_name=interaction.tool_name or message.get("name") or "tool",
                result=resolved_tool_result_payload(interaction),
            )
            replacement["_runtime_interaction"] = {
                "id": interaction.id,
                "status": "resolved",
                "version": interaction.version,
            }
            message.clear()
            message.update(replacement)
            return message
    return None


def interaction_resume_prompt(interaction: RuntimeInteraction) -> str:
    response = _dict(interaction.response_payload)
    if interaction.interaction_type == "choice_question":
        answer = response.get("value") or response.get("text") or response.get("label")
        detail = f"The user selected: {answer}."
    elif interaction.interaction_type == "structured_input":
        detail = f"The user supplied: {response.get('value') or ''}."
    else:
        detail = f"The user decision is: {response.get('decision') or 'resolved'}."
        if response.get("note"):
            detail += f" Note: {response['note']}"
    approval_instruction = ""
    if interaction.approval_id and response.get("decision") == "approved":
        approval_instruction = (
            f" When retrying the previously blocked tool, include "
            f"`\"_approval_id\": \"{interaction.approval_id}\"` in its arguments."
        )
    return (
        "(System): Resume the same suspended chat turn. "
        f"Interaction {interaction.id} ({interaction.interaction_type}) was resolved. "
        f"{detail}{approval_instruction} Continue the original task from the persisted tool context. "
        "Do not ask the same question again."
    )
