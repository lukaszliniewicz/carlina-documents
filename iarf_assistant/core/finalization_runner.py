from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from typing import Any, Callable

from iarf_assistant.core.model_capabilities import resolve_model_capabilities
from iarf_assistant.core.turn_finalization import (
    FINALIZE_TURN_CONTRACT_INSTRUCTIONS,
    FINALIZE_TURN_SCHEMA,
    FinalizationError,
    finalize_turn,
)
from iarf_assistant.models import (
    ChatContextItem,
    ChatMemoryRevision,
    ChatTurn,
)


class FinalizationResponseError(FinalizationError):
    pass


@dataclass
class ModelFinalizationResult:
    envelope: dict[str, Any]
    commit_result: dict[str, Any]
    attempts: int
    raw_response: str
    errors: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "envelope": copy.deepcopy(self.envelope),
            "commit_result": copy.deepcopy(self.commit_result),
            "attempts": self.attempts,
            "errors": list(self.errors),
        }


def finalization_response_format_for_profile(profile: Any) -> dict[str, Any] | None:
    """Choose a provider-safe transport for the canonical terminal contract.

    The application validates the complete envelope after generation. Gemini's
    structured-output dialect accepts only a subset of JSON Schema, while the
    canonical finalization schema intentionally includes arbitrary JSON memory
    values and a nullable continuation object. Use JSON-object mode for Gemini
    transports rather than weakening the canonical server-side contract.
    """
    if not getattr(profile, "supports_structured_output", False):
        return None

    structured_output_mode = str(
        getattr(profile, "structured_output_mode", "") or ""
    ).casefold()
    provider_name = str(getattr(profile, "provider_name", "") or "").casefold()
    catalog_model_id = str(
        getattr(profile, "catalog_model_id", "") or ""
    ).casefold()
    if (
        provider_name in {"gemini", "vertex_ai"}
        or catalog_model_id.startswith(("gemini/", "vertex_ai/"))
        or structured_output_mode == "json_object"
    ):
        return {"type": "json_object"}
    if structured_output_mode == "none":
        return None
    return copy.deepcopy(FINALIZE_TURN_SCHEMA)


def finalization_response_format_for_model(
    model_id: str,
    *,
    user_id: int | None = None,
) -> dict[str, Any] | None:
    profile = resolve_model_capabilities(model_id, user_id=user_id)
    return finalization_response_format_for_profile(profile)


def _message_content(response: Any) -> Any:
    try:
        message = response.choices[0].message
    except Exception as exc:
        raise FinalizationResponseError("Finalization model returned no message.") from exc

    tool_calls = (
        message.get("tool_calls")
        if isinstance(message, dict)
        else getattr(message, "tool_calls", None)
    )
    for tool_call in tool_calls or []:
        function = (
            tool_call.get("function")
            if isinstance(tool_call, dict)
            else getattr(tool_call, "function", None)
        )
        name = (
            function.get("name")
            if isinstance(function, dict)
            else getattr(function, "name", None)
        )
        if name != "finalize_turn":
            continue
        arguments = (
            function.get("arguments")
            if isinstance(function, dict)
            else getattr(function, "arguments", None)
        )
        return arguments

    return (
        message.get("content")
        if isinstance(message, dict)
        else getattr(message, "content", None)
    )


def _content_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        return json.dumps(content, ensure_ascii=False)
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if text is not None:
                    parts.append(str(text))
        return "\n".join(parts)
    return str(content)


def parse_finalization_response(response: Any) -> tuple[dict[str, Any], str]:
    content = _message_content(response)
    if isinstance(content, dict):
        return copy.deepcopy(content), json.dumps(content, ensure_ascii=False)
    raw = _content_text(content).strip()
    if raw.startswith("```"):
        lines = raw.splitlines()
        if lines and lines[0].lstrip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        raw = "\n".join(lines).strip()
    if not raw:
        raise FinalizationResponseError("Finalization model returned empty content.")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise FinalizationResponseError(
            f"Finalization model returned invalid JSON at character {exc.pos}: {exc.msg}."
        ) from exc
    if not isinstance(parsed, dict):
        raise FinalizationResponseError("Finalization response must decode to an object.")
    return parsed, raw


def _latest_memory(turn: ChatTurn) -> tuple[int, dict[str, Any]]:
    latest = (
        ChatMemoryRevision.query
        .filter_by(session_id=turn.session_id)
        .order_by(ChatMemoryRevision.revision.desc())
        .first()
    )
    if latest is None:
        return 0, {
            "schema_version": 1,
            "intent": {},
            "decisions": [],
            "constraints": [],
            "facts": {},
            "open_questions": [],
            "task_state": {},
            "next_actions": [],
            "evidence_refs": [],
            "corrections": [],
        }
    return latest.revision, copy.deepcopy(latest.snapshot_json)


def _context_manifest(turn: ChatTurn) -> list[dict[str, Any]]:
    items = (
        ChatContextItem.query
        .filter_by(session_id=turn.session_id, user_id=turn.user_id)
        .filter(
            (ChatContextItem.first_turn_id == turn.id)
            | (ChatContextItem.last_turn_id == turn.id)
        )
        .order_by(ChatContextItem.created_at.asc())
        .all()
    )
    return [{
        "context_item_id": item.id,
        "evidence_id": item.evidence_id,
        "version": item.version,
        "kind": item.kind,
        "title": item.title,
        "lifecycle_state": item.lifecycle_state,
        "materialization": item.materialization,
        "is_pinned": item.is_pinned,
        "priority": item.priority,
        "token_estimate": item.token_estimate,
        "created_this_turn": item.first_turn_id == turn.id,
    } for item in items]


def build_finalization_messages(
    *,
    turn: ChatTurn,
    candidate_response: str,
    memory_revision: int,
    memory_snapshot: dict[str, Any],
    context_manifest: list[dict[str, Any]],
    source_messages: list[dict[str, Any]] | None = None,
    repair_error: str | None = None,
    invalid_response: str | None = None,
) -> list[dict[str, Any]]:
    mode_clause = {
        "manual": (
            "Manual mode is active. Every context item must be kept unless the "
            "user explicitly authorized its exact ID; emit action=keep otherwise."
        ),
        "aggressive": (
            "Aggressive mode is active. Prefer reversible references/summaries "
            "when useful, but preserve important evidence links and all pins."
        ),
    }.get(
        str(turn.context_stewardship or "assisted").lower(),
        (
            "Assisted mode is active. Explicitly keep or safely disposition each "
            "new item after recording important conclusions and evidence IDs."
        ),
    )
    payload = {
        "turn_id": turn.id,
        "model_id": turn.model_id,
        "context_stewardship": turn.context_stewardship,
        "memory_base_revision": memory_revision,
        "current_memory": memory_snapshot,
        "candidate_response": candidate_response,
        "context_items_requiring_housekeeping": context_manifest,
    }
    source_messages = [
        copy.deepcopy(message)
        for message in (source_messages or [])
        if isinstance(message, dict)
    ]
    source_system_text = "\n\n".join(
        str(message.get("content") or "")
        for message in source_messages
        if str(message.get("role") or "").lower() == "system"
    )
    source_non_system = [
        message
        for message in source_messages
        if str(message.get("role") or "").lower() != "system"
    ]
    messages = [
        {
            "role": "system",
            "content": (
                FINALIZE_TURN_CONTRACT_INSTRUCTIONS
                + "\n"
                + mode_clause
                + "\nReturn JSON only and conform exactly to the terminal contract."
                + (
                    "\n\nOriginal turn instructions:\n" + source_system_text
                    if source_system_text
                    else ""
                )
            ),
        },
        *source_non_system,
        {
            "role": "user",
            "content": json.dumps(payload, ensure_ascii=False, sort_keys=True),
        },
    ]
    if repair_error:
        messages.append({
            "role": "user",
            "content": (
                "Your previous finalize_turn result was rejected. Repair only the "
                "structural/policy error below while preserving truthful content.\n\n"
                f"Error: {repair_error}\n\n"
                "Previous result:\n"
                + str(invalid_response or "")[:20_000]
            ),
        })
    return messages


def finalize_turn_with_model(
    *,
    llm_service: Any,
    turn_id: str,
    candidate_response: str,
    thinking_config: dict[str, Any] | None = None,
    telemetry_sink: dict[str, Any] | None = None,
    source_messages: list[dict[str, Any]] | None = None,
    response_transform: Callable[[str], str] | None = None,
    user_authorized_context_item_ids: list[str] | None = None,
    max_repair_attempts: int = 2,
    commit: bool = True,
) -> ModelFinalizationResult:
    """Run terminal response + housekeeping and commit it as one transaction."""
    turn = ChatTurn.query.filter_by(id=turn_id).one_or_none()
    if turn is None:
        raise FinalizationError("Chat turn not found.")
    memory_revision, memory_snapshot = _latest_memory(turn)
    context_manifest = _context_manifest(turn)
    required_ids = [
        item["context_item_id"]
        for item in context_manifest
        if item["created_this_turn"] and item["lifecycle_state"] != "deleted"
    ]
    profile = resolve_model_capabilities(
        turn.model_id,
        user_id=turn.user_id,
    )
    response_format = finalization_response_format_for_profile(profile)

    errors: list[str] = []
    invalid_response = None
    attempts = max(1, int(max_repair_attempts or 0) + 1)
    for attempt in range(1, attempts + 1):
        messages = build_finalization_messages(
            turn=turn,
            candidate_response=candidate_response,
            memory_revision=memory_revision,
            memory_snapshot=memory_snapshot,
            context_manifest=context_manifest,
            source_messages=source_messages,
            repair_error=errors[-1] if errors else None,
            invalid_response=invalid_response,
        )
        response = llm_service.completion(
            model=turn.model_id,
            messages=messages,
            response_format=response_format,
            thinking_config=thinking_config,
            source="chat_housekeeping",
            telemetry_sink=telemetry_sink,
            context_admission_mode=turn.context_stewardship,
        )
        try:
            envelope, raw = parse_finalization_response(response)
            invalid_response = raw
            if callable(response_transform):
                transformed = response_transform(
                    str((envelope.get("response") or {}).get("content") or "")
                )
                envelope = copy.deepcopy(envelope)
                envelope.setdefault("response", {})["content"] = str(transformed or "")
            commit_result = finalize_turn(
                turn.id,
                envelope,
                user_authorized_context_item_ids=user_authorized_context_item_ids,
                required_context_item_ids=required_ids,
                commit=commit,
            )
            return ModelFinalizationResult(
                envelope=envelope,
                commit_result=commit_result,
                attempts=attempt,
                raw_response=raw,
                errors=errors,
            )
        except Exception as exc:
            errors.append(str(exc))
            if attempt >= attempts:
                raise FinalizationError(
                    f"Terminal housekeeping failed after {attempt} attempt(s): {exc}"
                ) from exc

    raise FinalizationError("Terminal housekeeping failed without a model response.")


def finalize_turn_deterministically(
    *,
    turn_id: str,
    candidate_response: str,
    response_transform: Callable[[str], str] | None = None,
    commit: bool = True,
) -> ModelFinalizationResult:
    """Finalize a bounded turn without spending another model invocation.

    This conservative path changes no session memory and explicitly keeps every
    context item created by the turn. It is suitable for one-shot deterministic
    execution where the preceding model response is already the user-facing
    answer and no semantic housekeeping is required.
    """
    turn = ChatTurn.query.filter_by(id=turn_id).one_or_none()
    if turn is None:
        raise FinalizationError("Chat turn not found.")
    memory_revision, _memory_snapshot = _latest_memory(turn)
    context_manifest = _context_manifest(turn)
    required_items = [
        item
        for item in context_manifest
        if item["created_this_turn"] and item["lifecycle_state"] != "deleted"
    ]
    content = str(candidate_response or "")
    if callable(response_transform):
        content = str(response_transform(content) or "")
    envelope = {
        "response": {
            "content": content,
            "content_type": "markdown",
        },
        "memory_patch": {
            "base_revision": memory_revision,
            "operations": [],
        },
        "context_dispositions": [
            {
                "context_item_id": item["context_item_id"],
                "expected_version": item["version"],
                "action": "keep",
                "reason": (
                    "Preserved by deterministic finalization for a bounded "
                    "one-shot execution."
                ),
                "importance": "important",
            }
            for item in required_items
        ],
        "citations": [],
        "continuation": None,
    }
    commit_result = finalize_turn(
        turn.id,
        envelope,
        required_context_item_ids=[
            item["context_item_id"] for item in required_items
        ],
        commit=commit,
    )
    return ModelFinalizationResult(
        envelope=envelope,
        commit_result=commit_result,
        attempts=0,
        raw_response="",
        errors=[],
    )
