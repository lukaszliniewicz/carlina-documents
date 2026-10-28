from __future__ import annotations

import copy
import json
from typing import Any

from iarf_assistant.core.agent_events import (
    agent_event_chat_message,
    turn_failed_event,
)
from iarf_assistant.core.context_fabric import (
    ContextFabricError,
    ContextPolicyError,
    normalize_context_mode,
    transition_context_item,
)
from carlina.core.utc import utc_now_db
from iarf_assistant.database_utils import (
    commit_with_retry,
    public_error_message,
)
from iarf_assistant.extensions import db
from iarf_assistant.models import (
    ChatContextItem,
    ChatEvidence,
    ChatMemoryRevision,
    ChatMessage,
    ChatSession,
    ChatTurn,
)


MEMORY_SCHEMA_VERSION = 1
MEMORY_TOP_LEVEL_KEYS = {
    "schema_version",
    "intent",
    "decisions",
    "constraints",
    "facts",
    "open_questions",
    "task_state",
    "next_actions",
    "evidence_refs",
    "corrections",
    "_retractions",
    "_resolutions",
}
LOSSY_CONTEXT_ACTIONS = {
    "archive",
    "exclude",
    "delete",
    "summarize",
    "excerpt",
    "materialize_reference",
    "materialize_overflow",
}


FINALIZE_TURN_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "finalize_turn",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "response",
                "memory_patch",
                "context_dispositions",
                "citations",
                "continuation",
            ],
            "properties": {
                "response": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["content", "content_type"],
                    "properties": {
                        "content": {"type": "string"},
                        "content_type": {
                            "type": "string",
                            "enum": ["markdown", "text"],
                        },
                    },
                },
                "memory_patch": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["base_revision", "operations"],
                    "properties": {
                        "base_revision": {"type": "integer", "minimum": 0},
                        "operations": {
                            "type": "array",
                            "maxItems": 100,
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["op", "path"],
                                "properties": {
                                    "op": {
                                        "type": "string",
                                        "enum": ["add", "replace", "retract", "resolve"],
                                    },
                                    "path": {"type": "string"},
                                    "value": {},
                                    "evidence_refs": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                    },
                                    "reason": {"type": "string"},
                                },
                            },
                        },
                    },
                },
                "context_dispositions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": [
                            "context_item_id",
                            "expected_version",
                            "action",
                            "reason",
                        ],
                        "properties": {
                            "context_item_id": {"type": "string"},
                            "expected_version": {"type": "integer", "minimum": 1},
                            "action": {
                                "type": "string",
                                "enum": [
                                    "keep",
                                    "archive",
                                    "restore",
                                    "exclude",
                                    "delete",
                                    "pin",
                                    "unpin",
                                    "materialize_full",
                                    "materialize_reference",
                                    "materialize_overflow",
                                    "summarize",
                                    "excerpt",
                                ],
                            },
                            "reason": {"type": "string"},
                            "importance": {
                                "type": "string",
                                "enum": ["important", "not_important", "superseded"],
                            },
                            "content": {"type": "string"},
                            "authorized_by_user": {"type": "boolean"},
                        },
                    },
                },
                "citations": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "continuation": {
                    "anyOf": [
                        {"type": "null"},
                        {
                            "type": "object",
                            "additionalProperties": True,
                        },
                    ],
                },
            },
        },
    },
}


FINALIZE_TURN_INSTRUCTIONS = """\
At the terminal call, return one finalize_turn object. Write the user response,
apply an evidence-linked session-memory patch, and disposition context in the
same call. Archive only durable evidence that is no longer needed in the active
prompt. Important archived evidence must remain cited by ID in memory or the
response. Never archive a pinned item. In manual context mode, do not request a
lossy disposition unless the user explicitly authorized that exact item.
"""

FINALIZE_TURN_CONTRACT_INSTRUCTIONS = (
    FINALIZE_TURN_INSTRUCTIONS
    + "\nCanonical finalize_turn JSON Schema (return the object described by "
    "the inner schema, not a wrapper around it):\n"
    + json.dumps(
        FINALIZE_TURN_SCHEMA["json_schema"]["schema"],
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
)


class FinalizationError(ContextFabricError):
    pass


class MemoryRevisionConflict(FinalizationError):
    pass


class MemoryPatchError(FinalizationError):
    pass


def empty_memory_snapshot() -> dict[str, Any]:
    return {
        "schema_version": MEMORY_SCHEMA_VERSION,
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


def _decode_pointer(path: str) -> list[str]:
    if path == "":
        return []
    if not isinstance(path, str) or not path.startswith("/"):
        raise MemoryPatchError("Memory patch paths must be JSON pointers beginning with '/'.")
    parts = path.split("/")[1:]
    decoded = [part.replace("~1", "/").replace("~0", "~") for part in parts]
    if decoded and decoded[0] not in MEMORY_TOP_LEVEL_KEYS:
        raise MemoryPatchError(f"Unknown memory section: {decoded[0]}.")
    return decoded


def _container_and_key(document: Any, parts: list[str], *, create: bool) -> tuple[Any, str]:
    if not parts:
        raise MemoryPatchError("Replacing the memory root is not allowed.")
    current = document
    for part in parts[:-1]:
        if isinstance(current, dict):
            if part not in current:
                if not create:
                    raise MemoryPatchError(f"Memory path does not exist: /{'/'.join(parts)}.")
                current[part] = {}
            current = current[part]
            continue
        if isinstance(current, list):
            try:
                index = int(part)
            except (TypeError, ValueError):
                raise MemoryPatchError(f"Expected a list index at '{part}'.") from None
            if index < 0 or index >= len(current):
                raise MemoryPatchError(f"List index is out of range at '{part}'.")
            current = current[index]
            continue
        raise MemoryPatchError(f"Cannot traverse scalar memory value at '{part}'.")
    return current, parts[-1]


def _path_exists(document: Any, parts: list[str]) -> bool:
    try:
        container, key = _container_and_key(document, parts, create=False)
        if isinstance(container, dict):
            return key in container
        if isinstance(container, list):
            if key == "-":
                return False
            index = int(key)
            return 0 <= index < len(container)
    except (MemoryPatchError, TypeError, ValueError):
        return False
    return False


def _set_path(document: Any, parts: list[str], value: Any, *, replace: bool) -> None:
    container, key = _container_and_key(document, parts, create=not replace)
    if isinstance(container, dict):
        if replace and key not in container:
            raise MemoryPatchError(f"Replace path does not exist: /{'/'.join(parts)}.")
        container[key] = copy.deepcopy(value)
        return
    if isinstance(container, list):
        if key == "-":
            if replace:
                raise MemoryPatchError("Replace cannot use the '-' list index.")
            container.append(copy.deepcopy(value))
            return
        try:
            index = int(key)
        except (TypeError, ValueError):
            raise MemoryPatchError(f"Expected a list index at '{key}'.") from None
        if replace:
            if index < 0 or index >= len(container):
                raise MemoryPatchError(f"Replace list index is out of range: {index}.")
            container[index] = copy.deepcopy(value)
        else:
            if index < 0 or index > len(container):
                raise MemoryPatchError(f"Add list index is out of range: {index}.")
            container.insert(index, copy.deepcopy(value))
        return
    raise MemoryPatchError("Memory patch target is not a container.")


def _remove_path(document: Any, parts: list[str]) -> Any:
    container, key = _container_and_key(document, parts, create=False)
    if isinstance(container, dict):
        if key not in container:
            raise MemoryPatchError(f"Retract path does not exist: /{'/'.join(parts)}.")
        return container.pop(key)
    if isinstance(container, list):
        try:
            index = int(key)
        except (TypeError, ValueError):
            raise MemoryPatchError(f"Expected a list index at '{key}'.") from None
        if index < 0 or index >= len(container):
            raise MemoryPatchError(f"Retract list index is out of range: {index}.")
        return container.pop(index)
    raise MemoryPatchError("Memory patch target is not a container.")


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    values = value if isinstance(value, (list, tuple, set)) else [value]
    result = []
    seen = set()
    for item in values:
        normalized = str(item or "").strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


def apply_memory_patch(
    base_snapshot: dict[str, Any] | None,
    operations: list[dict[str, Any]] | None,
) -> tuple[dict[str, Any], list[str]]:
    snapshot = copy.deepcopy(base_snapshot) if isinstance(base_snapshot, dict) else empty_memory_snapshot()
    snapshot.setdefault("schema_version", MEMORY_SCHEMA_VERSION)
    if snapshot.get("schema_version") != MEMORY_SCHEMA_VERSION:
        raise MemoryPatchError("Unsupported session-memory schema version.")
    operations = operations or []
    if not isinstance(operations, list) or len(operations) > 100:
        raise MemoryPatchError("memory_patch.operations must be an array of at most 100 operations.")

    referenced_evidence: list[str] = []
    for index, operation in enumerate(operations):
        if not isinstance(operation, dict):
            raise MemoryPatchError(f"Memory operation {index} must be an object.")
        op = str(operation.get("op") or "").strip().lower()
        if op not in {"add", "replace", "retract", "resolve"}:
            raise MemoryPatchError(f"Unsupported memory operation: {op or '<empty>'}.")
        path = str(operation.get("path") or "")
        parts = _decode_pointer(path)
        evidence_refs = _string_list(operation.get("evidence_refs"))
        referenced_evidence.extend(evidence_refs)
        if op in {"add", "replace", "resolve"} and "value" not in operation:
            raise MemoryPatchError(f"Memory operation {index} requires value.")

        if op == "add":
            _set_path(snapshot, parts, operation.get("value"), replace=False)
        elif op == "replace":
            _set_path(snapshot, parts, operation.get("value"), replace=True)
        elif op == "retract":
            previous = _remove_path(snapshot, parts)
            snapshot.setdefault("_retractions", []).append({
                "path": path,
                "previous": previous,
                "evidence_refs": evidence_refs,
                "reason": str(operation.get("reason") or ""),
            })
        elif op == "resolve":
            existed = _path_exists(snapshot, parts)
            _set_path(snapshot, parts, operation.get("value"), replace=existed)
            snapshot.setdefault("_resolutions", []).append({
                "path": path,
                "evidence_refs": evidence_refs,
                "reason": str(operation.get("reason") or ""),
            })

    existing_refs = _string_list(snapshot.get("evidence_refs"))
    snapshot["evidence_refs"] = _string_list([*existing_refs, *referenced_evidence])
    return snapshot, snapshot["evidence_refs"]


def _latest_memory_revision(session_id: str) -> ChatMemoryRevision | None:
    return (
        ChatMemoryRevision.query
        .filter_by(session_id=session_id)
        .order_by(ChatMemoryRevision.revision.desc())
        .first()
    )


def _validate_evidence_refs(session_id: str, user_id: int, refs: list[str]) -> None:
    if not refs:
        return
    records = (
        ChatEvidence.query
        .filter(
            ChatEvidence.id.in_(refs),
            ChatEvidence.session_id == session_id,
            ChatEvidence.user_id == user_id,
            ChatEvidence.retention_state != "deleted",
        )
        .all()
    )
    found = {record.id for record in records}
    missing = [ref for ref in refs if ref not in found]
    if missing:
        raise MemoryPatchError(
            "Memory cites evidence that is unavailable in this session: "
            + ", ".join(missing[:10])
        )


def _validate_envelope(envelope: Any) -> dict[str, Any]:
    if not isinstance(envelope, dict):
        raise FinalizationError("finalize_turn must be an object.")
    response = envelope.get("response")
    if not isinstance(response, dict):
        raise FinalizationError("finalize_turn.response must be an object.")
    content = response.get("content")
    if not isinstance(content, str):
        raise FinalizationError("finalize_turn.response.content must be a string.")
    content_type = str(response.get("content_type") or "markdown").strip().lower()
    if content_type not in {"markdown", "text"}:
        raise FinalizationError("Unsupported response content type.")
    continuation = envelope.get("continuation")
    if not content.strip() and continuation is None:
        raise FinalizationError("A terminal response cannot be empty without a continuation.")

    patch = envelope.get("memory_patch")
    if not isinstance(patch, dict):
        raise FinalizationError("finalize_turn.memory_patch must be an object.")
    try:
        base_revision = int(patch.get("base_revision"))
    except (TypeError, ValueError):
        raise FinalizationError("memory_patch.base_revision must be an integer.") from None
    operations = patch.get("operations")
    if not isinstance(operations, list):
        raise FinalizationError("memory_patch.operations must be an array.")

    dispositions = envelope.get("context_dispositions")
    if not isinstance(dispositions, list):
        raise FinalizationError("context_dispositions must be an array.")
    citations = _string_list(envelope.get("citations"))
    return {
        "response": {
            "content": content,
            "content_type": content_type,
        },
        "memory_patch": {
            "base_revision": base_revision,
            "operations": copy.deepcopy(operations),
        },
        "context_dispositions": copy.deepcopy(dispositions),
        "citations": citations,
        "continuation": copy.deepcopy(continuation),
    }


def _disposition_transition(
    disposition: dict[str, Any],
    *,
    turn: ChatTurn,
    memory_evidence_refs: set[str],
    citations: set[str],
    user_authorized_context_item_ids: set[str],
) -> ChatContextItem | None:
    if not isinstance(disposition, dict):
        raise FinalizationError("Each context disposition must be an object.")
    item_id = str(disposition.get("context_item_id") or "").strip()
    if not item_id:
        raise FinalizationError("Context disposition is missing context_item_id.")
    try:
        expected_version = int(disposition.get("expected_version"))
    except (TypeError, ValueError):
        raise FinalizationError(
            f"Context disposition {item_id} has an invalid expected_version."
        ) from None
    action = str(disposition.get("action") or "").strip().lower()
    reason = str(disposition.get("reason") or "").strip()
    if not reason:
        raise FinalizationError(f"Context disposition {item_id} requires a reason.")
    if action == "keep":
        item = db.session.get(ChatContextItem, item_id)
        if item is None or item.session_id != turn.session_id or item.user_id != turn.user_id:
            raise FinalizationError(f"Context item {item_id} is unavailable.")
        if item.version != expected_version:
            raise FinalizationError(
                f"Context item {item_id} is at version {item.version}, not {expected_version}."
            )
        return None

    item = db.session.get(ChatContextItem, item_id)
    if item is None or item.session_id != turn.session_id or item.user_id != turn.user_id:
        raise FinalizationError(f"Context item {item_id} is unavailable.")

    explicit_user_authorization = (
        bool(disposition.get("authorized_by_user"))
        and item_id in user_authorized_context_item_ids
    )
    mode = normalize_context_mode(turn.context_stewardship)
    if mode == "manual" and action in LOSSY_CONTEXT_ACTIONS and not explicit_user_authorization:
        raise ContextPolicyError(
            f"Manual mode forbids unapproved '{action}' for context item {item_id}."
        )

    importance = str(disposition.get("importance") or "important").strip().lower()
    if action in {"archive", "exclude", "delete"} and importance == "important":
        if item.evidence_id not in memory_evidence_refs and item.evidence_id not in citations:
            raise ContextPolicyError(
                f"Important evidence {item.evidence_id} must be cited in memory or the response before {action}."
            )

    transition_kwargs: dict[str, Any] = {}
    if action == "archive":
        transition_kwargs["lifecycle_state"] = "archived"
    elif action == "restore":
        transition_kwargs["lifecycle_state"] = "active"
    elif action == "exclude":
        transition_kwargs["lifecycle_state"] = "excluded"
    elif action == "delete":
        transition_kwargs["lifecycle_state"] = "deleted"
    elif action == "pin":
        transition_kwargs["is_pinned"] = True
        transition_kwargs["lifecycle_state"] = "active"
    elif action == "unpin":
        transition_kwargs["is_pinned"] = False
    elif action == "materialize_full":
        transition_kwargs["materialization"] = "full"
    elif action == "materialize_reference":
        transition_kwargs["materialization"] = "reference"
    elif action == "materialize_overflow":
        transition_kwargs["materialization"] = "overflow"
    elif action == "summarize":
        transition_kwargs["materialization"] = "summary"
        transition_kwargs["materialized_content"] = disposition.get("content")
    elif action == "excerpt":
        transition_kwargs["materialization"] = "excerpt"
        transition_kwargs["materialized_content"] = disposition.get("content")
    else:
        raise FinalizationError(f"Unsupported context disposition action: {action}.")

    return transition_context_item(
        item_id,
        expected_version=expected_version,
        user_id=turn.user_id,
        session_id=turn.session_id,
        turn_id=turn.id,
        actor_type="assistant",
        actor_id=turn.model_id,
        reason=reason,
        metadata={
            "importance": importance,
            "authorized_by_user": explicit_user_authorization,
        },
        **transition_kwargs,
    )


def finalize_turn(
    turn_id: str,
    envelope: dict[str, Any],
    *,
    user_authorized_context_item_ids: list[str] | None = None,
    required_context_item_ids: list[str] | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    """Atomically commit response, memory revision, and context dispositions."""
    clean = _validate_envelope(envelope)
    authorized_ids = set(_string_list(user_authorized_context_item_ids))
    required_dispositions = set(_string_list(required_context_item_ids))
    disposition_ids = [
        str(item.get("context_item_id") or "").strip()
        for item in clean["context_dispositions"]
        if isinstance(item, dict)
    ]
    duplicate_ids = sorted({
        item_id
        for item_id in disposition_ids
        if item_id and disposition_ids.count(item_id) > 1
    })
    if duplicate_ids:
        raise FinalizationError(
            "Context items may be dispositioned only once per turn: "
            + ", ".join(duplicate_ids[:10])
        )
    missing_dispositions = sorted(required_dispositions - set(disposition_ids))
    if missing_dispositions:
        raise FinalizationError(
            "Terminal housekeeping must explicitly keep or change every new context item: "
            + ", ".join(missing_dispositions[:20])
        )

    try:
        with db.session.begin_nested():
            turn = db.session.get(ChatTurn, str(turn_id or ""))
            if turn is None:
                raise FinalizationError("Chat turn not found.")
            if turn.status == "finalized":
                if turn.finalization_json == clean:
                    memory = (
                        ChatMemoryRevision.query
                        .filter_by(turn_id=turn.id)
                        .one_or_none()
                    )
                    return {
                        "turn": turn.to_dict(),
                        "assistant_message_id": turn.assistant_message_id,
                        "memory_revision": memory.to_dict() if memory else None,
                        "idempotent": True,
                    }
                raise FinalizationError("Chat turn has already been finalized with a different envelope.")
            if turn.status not in {"pending", "repairing"}:
                raise FinalizationError(f"Chat turn cannot finalize from state '{turn.status}'.")

            session = db.session.get(ChatSession, turn.session_id)
            if session is None or session.user_id != turn.user_id:
                raise FinalizationError("Chat session for turn is unavailable.")

            latest = _latest_memory_revision(turn.session_id)
            latest_revision_number = latest.revision if latest else 0
            base_revision = clean["memory_patch"]["base_revision"]
            if base_revision != latest_revision_number:
                raise MemoryRevisionConflict(
                    f"Memory patch is based on revision {base_revision}; latest is {latest_revision_number}."
                )
            if (
                turn.base_memory_revision is not None
                and int(turn.base_memory_revision) != base_revision
            ):
                raise MemoryRevisionConflict(
                    f"Turn began at memory revision {turn.base_memory_revision}, not {base_revision}."
                )

            base_snapshot = latest.snapshot_json if latest else empty_memory_snapshot()
            snapshot, memory_evidence_refs = apply_memory_patch(
                base_snapshot,
                clean["memory_patch"]["operations"],
            )
            all_refs = _string_list([
                *memory_evidence_refs,
                *clean["citations"],
            ])
            _validate_evidence_refs(turn.session_id, turn.user_id, all_refs)

            disposition_results = []
            for disposition in clean["context_dispositions"]:
                transitioned = _disposition_transition(
                    disposition,
                    turn=turn,
                    memory_evidence_refs=set(memory_evidence_refs),
                    citations=set(clean["citations"]),
                    user_authorized_context_item_ids=authorized_ids,
                )
                if transitioned is not None:
                    disposition_results.append(transitioned.to_dict())

            revision = ChatMemoryRevision(
                session_id=turn.session_id,
                turn_id=turn.id,
                revision=latest_revision_number + 1,
                parent_revision=latest_revision_number or None,
                model_id=turn.model_id,
                patch_json=clean["memory_patch"],
                snapshot_json=snapshot,
                evidence_refs=memory_evidence_refs,
            )
            db.session.add(revision)
            db.session.flush()

            assistant_payload = {
                "role": "assistant",
                "content": clean["response"]["content"],
                "_context_fabric": {
                    "schema_version": 2,
                    "turn_id": turn.id,
                    "memory_revision": revision.revision,
                    "citations": clean["citations"],
                    "context_dispositions": copy.deepcopy(clean["context_dispositions"]),
                    "continuation": clean["continuation"],
                },
            }
            assistant_message = ChatMessage(
                session_id=turn.session_id,
                message=assistant_payload,
            )
            db.session.add(assistant_message)
            db.session.flush()

            # Keep the legacy session-memory reader functional during migration.
            session.working_memory = json.dumps(
                snapshot,
                ensure_ascii=False,
                sort_keys=True,
            )
            session.updated_at = utc_now_db()
            turn.assistant_message_id = assistant_message.id
            turn.base_memory_revision = base_revision
            turn.finalization_json = clean
            turn.status = "finalized"
            turn.finalized_at = utc_now_db()
            turn.updated_at = utc_now_db()
            db.session.flush()

            result = {
                "turn": turn.to_dict(),
                "assistant_message_id": assistant_message.id,
                "memory_revision": revision.to_dict(),
                "context_items": disposition_results,
                "idempotent": False,
            }
        if commit:
            db.session.commit()
        return result
    except Exception:
        if commit:
            db.session.rollback()
        raise


def mark_turn_failed(
    turn_id: str,
    error: BaseException | str,
    *,
    details: dict[str, Any] | None = None,
    commit: bool = True,
) -> ChatTurn:
    turn = db.session.get(ChatTurn, str(turn_id or ""))
    if turn is None:
        raise FinalizationError("Chat turn not found.")
    if turn.status == "finalized":
        raise FinalizationError("A finalized turn cannot be marked failed.")
    was_failed = turn.status == "failed"
    public_message = public_error_message(error)
    failure_details = dict(details) if isinstance(details, dict) else {}
    turn.status = "failed"
    turn.error_json = {
        "error_type": type(error).__name__ if isinstance(error, BaseException) else "error",
        "message": public_message,
        "details": failure_details,
    }
    turn.updated_at = utc_now_db()
    if not was_failed:
        db.session.add(
            ChatMessage(
                session_id=turn.session_id,
                message=agent_event_chat_message(
                    turn_failed_event(
                        turn_id=turn.id,
                        message=public_message,
                        phase=str(
                            failure_details.get("phase")
                            or "chat_orchestrator"
                        ),
                    )
                ),
                created_at=turn.updated_at,
            )
        )
    if commit:
        commit_with_retry(
            db.session,
            operation_name="mark_chat_turn_failed",
        )
    else:
        db.session.flush()
    return turn
