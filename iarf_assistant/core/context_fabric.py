from __future__ import annotations

import copy
import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable

from iarf_assistant.core.agent_events import (
    agent_event_chat_message,
    turn_failed_event,
)
from iarf_assistant.database_utils import (
    commit_with_retry,
    public_error_message,
    run_transaction_with_retry,
)
from carlina.core.utc import utc_now_db
from iarf_assistant.extensions import db
from iarf_assistant.models import (
    ChatContextEvent,
    ChatContextItem,
    ChatEvidence,
    ChatEvidenceChunk,
    ChatMessage,
    ChatSession,
    ChatTurn,
)


DEFAULT_EVIDENCE_CHUNK_CHARS = 250_000
DEFAULT_INLINE_EVIDENCE_CHARS = 250_000
VALID_CONTEXT_STATES = {"active", "archived", "excluded", "deleted"}
VALID_MATERIALIZATIONS = {"full", "excerpt", "summary", "reference", "overflow"}
VALID_CONTEXT_MODES = {"assisted", "manual", "aggressive"}


class ContextFabricError(ValueError):
    pass


class EvidenceIntegrityError(ContextFabricError):
    pass


class ContextVersionConflict(ContextFabricError):
    pass


class ContextPolicyError(ContextFabricError):
    pass


@dataclass(frozen=True)
class ContextProjectionResult:
    """Provider-facing messages plus content-free projection diagnostics."""

    messages: tuple[dict[str, Any], ...]
    projected_item_ids: tuple[str, ...]
    full_item_ids: tuple[str, ...]
    reference_item_ids: tuple[str, ...]
    unavailable_item_ids: tuple[str, ...]
    lifecycle_counts: dict[str, int]
    materialization_counts: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_count": len(self.messages),
            "projected_item_ids": list(self.projected_item_ids),
            "full_item_ids": list(self.full_item_ids),
            "reference_item_ids": list(self.reference_item_ids),
            "unavailable_item_ids": list(self.unavailable_item_ids),
            "lifecycle_counts": dict(self.lifecycle_counts),
            "materialization_counts": dict(self.materialization_counts),
        }


@dataclass(frozen=True)
class AcceptedChatTurn:
    """Identifiers durably committed before model or tool execution."""

    session_id: str
    turn_id: str
    evidence_id: str | None
    user_message_id: int | None
    idempotent: bool = False


def normalize_context_mode(value: str | None) -> str:
    normalized = str(value or "assisted").strip().lower()
    if normalized in {"manual", "off", "disabled"}:
        return "manual"
    if normalized in {"aggressive", "compact"}:
        return "aggressive"
    return "assisted"


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="strict")).hexdigest()


def _estimate_tokens(value: str) -> int:
    if not value:
        return 0
    return max(1, (len(value) + 3) // 4)


def _serialize_content(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        return str(value)


def _session_for_user(session_id: str, user_id: int) -> ChatSession:
    session = db.session.get(ChatSession, str(session_id or ""))
    if session is None or int(session.user_id) != int(user_id):
        raise ContextFabricError("Chat session not found for this user.")
    return session


def create_turn(
    *,
    session_id: str,
    user_id: int,
    model_id: str | None,
    context_stewardship: str = "assisted",
    turn_id: str | None = None,
    base_memory_revision: int | None = None,
    admission: dict[str, Any] | None = None,
) -> ChatTurn:
    session = _session_for_user(session_id, user_id)
    mode = normalize_context_mode(context_stewardship)
    turn = ChatTurn(
        id=turn_id,
        session_id=session.id,
        user_id=user_id,
        status="pending",
        context_stewardship=mode,
        model_id=str(model_id or "") or None,
        base_memory_revision=base_memory_revision,
        admission_json=dict(admission) if isinstance(admission, dict) else None,
    )
    db.session.add(turn)
    session.context_stewardship = mode
    db.session.flush()
    return turn


def record_evidence(
    *,
    session_id: str,
    user_id: int,
    kind: str,
    content: Any,
    turn_id: str | None = None,
    role: str | None = None,
    mime_type: str | None = "text/plain",
    source_type: str | None = None,
    source_id: str | None = None,
    source_version: str | None = None,
    metadata: dict[str, Any] | None = None,
    evidence_id: str | None = None,
    inline_limit_chars: int = DEFAULT_INLINE_EVIDENCE_CHARS,
    chunk_chars: int = DEFAULT_EVIDENCE_CHUNK_CHARS,
) -> ChatEvidence:
    """Persist complete raw evidence, chunking large content without truncation."""
    session = _session_for_user(session_id, user_id)
    text = _serialize_content(content)
    encoded = text.encode("utf-8", errors="strict")
    content_hash = hashlib.sha256(encoded).hexdigest()
    inline_limit = max(0, int(inline_limit_chars))
    chunk_size = max(1_000, int(chunk_chars))

    if turn_id:
        turn = db.session.get(ChatTurn, turn_id)
        if turn is None or turn.session_id != session.id or int(turn.user_id) != int(user_id):
            raise ContextFabricError("Turn does not belong to this chat session.")

    evidence = ChatEvidence(
        id=evidence_id,
        session_id=session.id,
        user_id=user_id,
        turn_id=turn_id,
        kind=str(kind or "unknown")[:64],
        role=str(role or "")[:32] or None,
        mime_type=str(mime_type or "")[:255] or None,
        source_type=str(source_type or "")[:64] or None,
        source_id=str(source_id or "")[:512] or None,
        source_version=str(source_version or "")[:128] or None,
        content_hash=content_hash,
        content=text if len(text) <= inline_limit else None,
        char_count=len(text),
        byte_count=len(encoded),
        token_estimate=_estimate_tokens(text),
        metadata_json=dict(metadata) if isinstance(metadata, dict) else None,
        retention_state="retained",
    )
    db.session.add(evidence)
    db.session.flush()

    if evidence.content is None:
        for ordinal, start in enumerate(range(0, len(text), chunk_size)):
            chunk_text = text[start:start + chunk_size]
            db.session.add(ChatEvidenceChunk(
                evidence_id=evidence.id,
                ordinal=ordinal,
                start_offset=start,
                end_offset=start + len(chunk_text),
                content_hash=_hash_text(chunk_text),
                content=chunk_text,
            ))
        db.session.flush()
    return evidence


def _message_context_turn_id(row: ChatMessage) -> str | None:
    message = row.message if isinstance(row.message, dict) else {}
    context = (
        message.get("_context_fabric")
        if isinstance(message.get("_context_fabric"), dict)
        else {}
    )
    value = context.get("turn_id")
    return str(value) if value else None


def _message_event_id(row: ChatMessage) -> str | None:
    message = row.message if isinstance(row.message, dict) else {}
    event = message.get("event") if isinstance(message.get("event"), dict) else {}
    value = event.get("id")
    return str(value) if value else None


def accept_chat_turn(
    *,
    session_id: str,
    user_id: int,
    model_id: str | None,
    context_stewardship: str = "assisted",
    turn_id: str | None = None,
    base_memory_revision: int | None = None,
    user_message: dict[str, Any] | None = None,
    create_session_if_missing: bool = False,
    retry_timeout_seconds: float | None = None,
) -> AcceptedChatTurn:
    """Atomically persist the received turn before any model work begins."""
    accepted_session_id = str(session_id or "")
    accepted_turn_id = str(turn_id or uuid.uuid4())
    accepted_evidence_id = (
        f"ev_{uuid.uuid4().hex}" if user_message is not None else None
    )
    original_message = (
        copy.deepcopy(user_message)
        if isinstance(user_message, dict)
        else None
    )

    if not accepted_session_id:
        raise ContextFabricError("A chat session ID is required.")

    def persist_received_turn(_session) -> AcceptedChatTurn:
        chat_session = db.session.get(ChatSession, accepted_session_id)
        if chat_session is None:
            if not create_session_if_missing:
                raise ContextFabricError("Chat session not found for this user.")
            chat_session = ChatSession(
                id=accepted_session_id,
                user_id=user_id,
            )
            db.session.add(chat_session)
            db.session.flush()
        elif int(chat_session.user_id) != int(user_id):
            raise ContextFabricError("Chat session not found for this user.")

        existing_turn = db.session.get(ChatTurn, accepted_turn_id)
        if existing_turn is not None:
            if (
                existing_turn.session_id != chat_session.id
                or int(existing_turn.user_id) != int(user_id)
            ):
                raise ContextFabricError(
                    "Turn does not belong to this chat session."
                )
            evidence = (
                ChatEvidence.query.filter_by(
                    session_id=chat_session.id,
                    turn_id=existing_turn.id,
                    kind="user_message",
                )
                .order_by(ChatEvidence.created_at.asc())
                .first()
            )
            message_row = next(
                (
                    row
                    for row in ChatMessage.query.filter_by(
                        session_id=chat_session.id
                    ).all()
                    if _message_context_turn_id(row) == existing_turn.id
                    and (
                        row.message
                        if isinstance(row.message, dict)
                        else {}
                    ).get("role")
                    == "user"
                ),
                None,
            )
            return AcceptedChatTurn(
                session_id=chat_session.id,
                turn_id=existing_turn.id,
                evidence_id=evidence.id if evidence is not None else None,
                user_message_id=(
                    message_row.id if message_row is not None else None
                ),
                idempotent=True,
            )

        turn = create_turn(
            session_id=chat_session.id,
            user_id=user_id,
            model_id=model_id,
            context_stewardship=context_stewardship,
            turn_id=accepted_turn_id,
            base_memory_revision=base_memory_revision,
        )
        evidence = None
        message_row = None
        if original_message is not None:
            evidence = record_evidence(
                session_id=chat_session.id,
                user_id=user_id,
                turn_id=turn.id,
                kind="user_message",
                role="user",
                content=original_message.get("content"),
                source_type="chat",
                source_id=turn.id,
                metadata={
                    "files": copy.deepcopy(
                        original_message.get("files")
                        if isinstance(original_message.get("files"), list)
                        else []
                    ),
                },
                evidence_id=accepted_evidence_id,
            )
            message_payload = copy.deepcopy(original_message)
            message_payload["_context_fabric"] = {
                "schema_version": 2,
                "turn_id": turn.id,
                "evidence_id": evidence.id,
                "content_hash": evidence.content_hash,
                "accepted_before_execution": True,
            }
            message_row = ChatMessage(
                session_id=chat_session.id,
                message=message_payload,
                created_at=turn.created_at,
            )
            db.session.add(message_row)
            db.session.flush()

            prompt = str(original_message.get("content") or "").strip()
            if not chat_session.title and prompt:
                chat_session.title = (
                    f"{prompt[:60]}..." if len(prompt) > 60 else prompt
                )
        chat_session.updated_at = utc_now_db()
        db.session.flush()
        return AcceptedChatTurn(
            session_id=chat_session.id,
            turn_id=turn.id,
            evidence_id=evidence.id if evidence is not None else None,
            user_message_id=message_row.id if message_row is not None else None,
        )

    return run_transaction_with_retry(
        db.session,
        persist_received_turn,
        operation_name="accept_chat_turn",
        timeout_seconds=retry_timeout_seconds,
    )


def checkpoint_turn_event(
    turn_id: str,
    event_packet: dict[str, Any],
    *,
    commit: bool = True,
) -> ChatMessage | None:
    """Persist user-visible progress while a turn is still running.

    Terminal history used to be written only after model housekeeping. A
    process exit in the middle of a tool loop therefore erased every visible
    progress card even though evidence had already been retained. Checkpoints
    intentionally contain only bounded agent-event payloads; full tool output
    remains in ChatEvidence.
    """
    turn = db.session.get(ChatTurn, str(turn_id or ""))
    if turn is None:
        raise ContextFabricError("Chat turn not found.")
    if turn.status not in {"pending", "repairing"}:
        return None

    message_payload = agent_event_chat_message(event_packet)
    event = (
        message_payload.get("event")
        if isinstance(message_payload.get("event"), dict)
        else {}
    )
    event_id = str(event.get("id") or "").strip()
    if not event_id:
        raise ContextFabricError("Agent event is missing an event ID.")

    # Encrypted JSON cannot be filtered portably in SQL. The current turn's
    # bounded history tail is sufficient because event IDs are turn-local.
    existing_rows = (
        ChatMessage.query.filter_by(session_id=turn.session_id)
        .order_by(ChatMessage.id.desc())
        .limit(500)
        .all()
    )
    for row in existing_rows:
        if _message_event_id(row) == event_id:
            return row

    now = utc_now_db()
    message_payload["_context_fabric"] = {
        "schema_version": 2,
        "turn_id": turn.id,
        "durable_progress_checkpoint": True,
    }
    row = ChatMessage(
        session_id=turn.session_id,
        message=message_payload,
        created_at=now,
    )
    db.session.add(row)
    turn.updated_at = now
    chat_session = db.session.get(ChatSession, turn.session_id)
    if chat_session is not None:
        chat_session.updated_at = now
    if commit:
        commit_with_retry(
            db.session,
            operation_name="checkpoint_chat_turn_event",
        )
    else:
        db.session.flush()
    return row


def recover_abandoned_chat_turns(*, commit: bool = True) -> dict[str, int]:
    """Fail pending turns left behind by a previous application process."""
    pending_turns = (
        ChatTurn.query.filter(ChatTurn.status.in_(("pending", "repairing")))
        .order_by(ChatTurn.created_at.asc())
        .all()
    )
    if not pending_turns:
        return {"interrupted_turns": 0, "failure_events": 0}

    existing_rows_by_session: dict[str, list[ChatMessage]] = {}
    now = utc_now_db()
    failure_events = 0
    for turn in pending_turns:
        turn.status = "failed"
        turn.error_json = {
            "error_type": "InterruptedTurn",
            "message": (
                "The response was interrupted before it could be finalized. "
                "Your request and completed tool activity were preserved."
            ),
            "details": {
                "phase": "application_restart_recovery",
                "recoverable": True,
            },
        }
        turn.updated_at = now
        rows = existing_rows_by_session.setdefault(
            turn.session_id,
            ChatMessage.query.filter_by(session_id=turn.session_id).all(),
        )
        failed_event_id = f"turn:{turn.id}:failed"
        if failed_event_id not in {
            event_id
            for row in rows
            for event_id in [_message_event_id(row)]
            if event_id
        }:
            failure_row = ChatMessage(
                session_id=turn.session_id,
                message=agent_event_chat_message(
                    turn_failed_event(
                        turn_id=turn.id,
                        message=turn.error_json["message"],
                        phase="application_restart_recovery",
                    )
                ),
                created_at=now,
            )
            db.session.add(failure_row)
            rows.append(failure_row)
            failure_events += 1
        chat_session = db.session.get(ChatSession, turn.session_id)
        if chat_session is not None:
            chat_session.updated_at = now

    if commit:
        commit_with_retry(
            db.session,
            operation_name="recover_abandoned_chat_turns",
        )
    else:
        db.session.flush()
    return {
        "interrupted_turns": len(pending_turns),
        "failure_events": failure_events,
    }


def _evidence_for_scope(
    evidence_id: str,
    *,
    user_id: int | None = None,
    session_id: str | None = None,
) -> ChatEvidence:
    evidence = db.session.get(ChatEvidence, str(evidence_id or ""))
    if evidence is None:
        raise ContextFabricError("Evidence not found.")
    if user_id is not None and int(evidence.user_id) != int(user_id):
        raise ContextFabricError("Evidence not found.")
    if session_id is not None and evidence.session_id != str(session_id):
        raise ContextFabricError("Evidence not found.")
    if evidence.retention_state == "deleted" or evidence.deleted_at is not None:
        raise ContextFabricError("Evidence has been deleted under its retention policy.")
    return evidence


def read_evidence(
    evidence_id: str,
    *,
    user_id: int | None = None,
    session_id: str | None = None,
    start_offset: int = 0,
    max_chars: int | None = None,
    verify_integrity: bool = True,
) -> dict[str, Any]:
    """Read an exact evidence slice and validate the stored hashes."""
    evidence = _evidence_for_scope(evidence_id, user_id=user_id, session_id=session_id)
    start = max(0, int(start_offset or 0))
    requested_end = evidence.char_count if max_chars is None else min(
        evidence.char_count,
        start + max(0, int(max_chars)),
    )
    if start > evidence.char_count:
        start = evidence.char_count
    requested_end = max(start, requested_end)

    if evidence.content is not None:
        full_content = str(evidence.content)
        if verify_integrity and _hash_text(full_content) != evidence.content_hash:
            raise EvidenceIntegrityError(f"Evidence hash mismatch for {evidence.id}.")
        content = full_content[start:requested_end]
    else:
        parts: list[str] = []
        full_hasher = hashlib.sha256()
        expected_ordinal = 0
        expected_offset = 0
        for chunk in evidence.chunks:
            if chunk.ordinal != expected_ordinal or chunk.start_offset != expected_offset:
                raise EvidenceIntegrityError(f"Evidence chunk ordering mismatch for {evidence.id}.")
            chunk_text = str(chunk.content)
            if verify_integrity and _hash_text(chunk_text) != chunk.content_hash:
                raise EvidenceIntegrityError(
                    f"Evidence chunk hash mismatch for {evidence.id}:{chunk.ordinal}."
                )
            if verify_integrity:
                full_hasher.update(chunk_text.encode("utf-8", errors="strict"))
            if chunk.end_offset > start and chunk.start_offset < requested_end:
                local_start = max(0, start - chunk.start_offset)
                local_end = min(len(chunk_text), requested_end - chunk.start_offset)
                parts.append(chunk_text[local_start:local_end])
            expected_ordinal += 1
            expected_offset = chunk.end_offset
        if expected_offset != evidence.char_count:
            raise EvidenceIntegrityError(f"Evidence chunk coverage mismatch for {evidence.id}.")
        if verify_integrity and full_hasher.hexdigest() != evidence.content_hash:
            raise EvidenceIntegrityError(f"Evidence hash mismatch for {evidence.id}.")
        content = "".join(parts)

    return {
        "evidence": evidence.to_dict(include_content=False),
        "content": content,
        "start_offset": start,
        "end_offset": requested_end,
        "has_more": requested_end < evidence.char_count,
        "next_offset": requested_end if requested_end < evidence.char_count else None,
        "integrity_verified": bool(verify_integrity),
    }


def recover_incomplete_turn_history(
    *,
    session_id: str,
    user_id: int,
    stale_pending_after_seconds: float | None = 900,
    now: datetime | None = None,
    commit: bool = True,
) -> dict[str, int]:
    """Repair legacy pending/failed turns whose evidence is not in history."""
    chat_session = _session_for_user(session_id, user_id)
    turns = (
        ChatTurn.query.filter(
            ChatTurn.session_id == chat_session.id,
            ChatTurn.user_id == user_id,
            ChatTurn.status.in_(("pending", "failed")),
        )
        .order_by(ChatTurn.created_at.asc())
        .all()
    )
    if not turns:
        return {
            "user_messages": 0,
            "failure_events": 0,
            "interrupted_turns": 0,
        }

    recovered_at = now or utc_now_db()
    interrupted_turns = 0
    if stale_pending_after_seconds is not None:
        try:
            stale_after = max(0.0, float(stale_pending_after_seconds))
        except (TypeError, ValueError):
            stale_after = 900.0
        stale_before = recovered_at - timedelta(seconds=stale_after)
        for turn in turns:
            if (
                turn.status == "pending"
                and (turn.updated_at or turn.created_at) <= stale_before
            ):
                turn.status = "failed"
                turn.error_json = {
                    "error_type": "InterruptedTurn",
                    "message": (
                        "The response stopped before finalization. Your request "
                        "and completed tool activity were preserved."
                    ),
                    "details": {
                        "phase": "stale_turn_recovery",
                        "recoverable": True,
                    },
                }
                turn.updated_at = recovered_at
                interrupted_turns += 1

    existing_rows = (
        ChatMessage.query.filter_by(session_id=chat_session.id)
        .order_by(ChatMessage.id.asc())
        .all()
    )
    existing_user_turn_ids = {
        turn_id
        for row in existing_rows
        if (
            (row.message if isinstance(row.message, dict) else {}).get("role")
            == "user"
        )
        for turn_id in [_message_context_turn_id(row)]
        if turn_id
    }
    existing_event_ids = {
        event_id
        for row in existing_rows
        for event_id in [_message_event_id(row)]
        if event_id
    }

    recovered_user_messages = 0
    recovered_failure_events = 0
    for turn in turns:
        if turn.id not in existing_user_turn_ids:
            evidence = (
                ChatEvidence.query.filter_by(
                    session_id=chat_session.id,
                    user_id=user_id,
                    turn_id=turn.id,
                    kind="user_message",
                    retention_state="retained",
                )
                .order_by(ChatEvidence.created_at.asc())
                .first()
            )
            if evidence is not None and evidence.deleted_at is None:
                recovered = read_evidence(
                    evidence.id,
                    user_id=user_id,
                    session_id=chat_session.id,
                )
                files = (
                    evidence.metadata_json.get("files")
                    if isinstance(evidence.metadata_json, dict)
                    and isinstance(evidence.metadata_json.get("files"), list)
                    else []
                )
                message_payload = {
                    "role": "user",
                    "content": recovered["content"],
                    "files": copy.deepcopy(files),
                    "_context_fabric": {
                        "schema_version": 2,
                        "turn_id": turn.id,
                        "evidence_id": evidence.id,
                        "content_hash": evidence.content_hash,
                        "recovered_from_evidence": True,
                    },
                }
                db.session.add(
                    ChatMessage(
                        session_id=chat_session.id,
                        message=message_payload,
                        created_at=evidence.created_at or turn.created_at,
                    )
                )
                recovered_user_messages += 1
                existing_user_turn_ids.add(turn.id)
                prompt = str(recovered["content"] or "").strip()
                if not chat_session.title and prompt:
                    chat_session.title = (
                        f"{prompt[:60]}..." if len(prompt) > 60 else prompt
                    )

        failed_event_id = f"turn:{turn.id}:failed"
        if (
            turn.status == "failed"
            and failed_event_id not in existing_event_ids
        ):
            error_payload = (
                turn.error_json if isinstance(turn.error_json, dict) else {}
            )
            details = (
                error_payload.get("details")
                if isinstance(error_payload.get("details"), dict)
                else {}
            )
            message = public_error_message(
                error_payload.get("message") or "The response was interrupted."
            )
            db.session.add(
                ChatMessage(
                    session_id=chat_session.id,
                    message=agent_event_chat_message(
                        turn_failed_event(
                            turn_id=turn.id,
                            message=message,
                            phase=str(
                                details.get("phase") or "chat_orchestrator"
                            ),
                        )
                    ),
                    created_at=turn.updated_at or utc_now_db(),
                )
            )
            recovered_failure_events += 1
            existing_event_ids.add(failed_event_id)

    recovered_count = (
        recovered_user_messages
        + recovered_failure_events
        + interrupted_turns
    )
    if recovered_count:
        chat_session.updated_at = utc_now_db()
        if commit:
            commit_with_retry(
                db.session,
                operation_name="recover_incomplete_turn_history",
            )
        else:
            db.session.flush()
    return {
        "user_messages": recovered_user_messages,
        "failure_events": recovered_failure_events,
        "interrupted_turns": interrupted_turns,
    }


def create_context_item(
    *,
    evidence: ChatEvidence | str,
    user_id: int,
    kind: str | None = None,
    title: str | None = None,
    turn_id: str | None = None,
    lifecycle_state: str = "active",
    materialization: str = "full",
    materialized_content: str | None = None,
    priority: int = 50,
    is_pinned: bool = False,
    source_ref: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    actor_type: str = "runtime",
    actor_id: str | None = None,
    reason: str = "Context item created from durable evidence.",
) -> ChatContextItem:
    evidence_record = (
        _evidence_for_scope(evidence, user_id=user_id)
        if isinstance(evidence, str)
        else evidence
    )
    if int(evidence_record.user_id) != int(user_id):
        raise ContextFabricError("Evidence not found.")
    state = str(lifecycle_state or "active").strip().lower()
    if state not in VALID_CONTEXT_STATES:
        raise ContextFabricError(f"Invalid context lifecycle state: {state}.")
    materialization = str(materialization or "full").strip().lower()
    if materialization not in VALID_MATERIALIZATIONS:
        raise ContextFabricError(f"Invalid context materialization: {materialization}.")
    priority_value = max(0, min(100, int(priority)))
    materialized_text = str(materialized_content) if materialized_content is not None else None
    content_hash = (
        _hash_text(materialized_text)
        if materialized_text is not None
        else evidence_record.content_hash
    )
    item = ChatContextItem(
        session_id=evidence_record.session_id,
        user_id=user_id,
        evidence_id=evidence_record.id,
        first_turn_id=turn_id or evidence_record.turn_id,
        last_turn_id=turn_id or evidence_record.turn_id,
        kind=str(kind or evidence_record.kind)[:64],
        title=str(title) if title is not None else None,
        lifecycle_state=state,
        materialization=materialization,
        materialized_content=materialized_text,
        priority=priority_value,
        is_pinned=bool(is_pinned),
        token_estimate=(
            _estimate_tokens(materialized_text)
            if materialized_text is not None
            else evidence_record.token_estimate
        ),
        content_hash=content_hash,
        source_ref_json=dict(source_ref) if isinstance(source_ref, dict) else None,
        metadata_json=dict(metadata) if isinstance(metadata, dict) else None,
        version=1,
    )
    db.session.add(item)
    db.session.flush()
    db.session.add(ChatContextEvent(
        context_item_id=item.id,
        session_id=item.session_id,
        turn_id=turn_id or evidence_record.turn_id,
        actor_type=str(actor_type or "runtime")[:24],
        actor_id=str(actor_id or "")[:255] or None,
        event_type="created",
        from_state=None,
        to_state=state,
        from_materialization=None,
        to_materialization=materialization,
        expected_version=None,
        result_version=1,
        reason=reason,
    ))
    db.session.flush()
    return item


def _context_item_for_scope(
    context_item_id: str,
    *,
    user_id: int | None = None,
    session_id: str | None = None,
) -> ChatContextItem:
    item = db.session.get(ChatContextItem, str(context_item_id or ""))
    if item is None:
        raise ContextFabricError("Context item not found.")
    if user_id is not None and int(item.user_id) != int(user_id):
        raise ContextFabricError("Context item not found.")
    if session_id is not None and item.session_id != str(session_id):
        raise ContextFabricError("Context item not found.")
    return item


def get_context_item(
    context_item_id: str,
    *,
    user_id: int | None = None,
    session_id: str | None = None,
) -> ChatContextItem:
    """Return one context item after applying the standard ownership scope."""
    return _context_item_for_scope(
        context_item_id,
        user_id=user_id,
        session_id=session_id,
    )


def transition_context_item(
    context_item_id: str,
    *,
    expected_version: int,
    user_id: int | None = None,
    session_id: str | None = None,
    turn_id: str | None = None,
    actor_type: str = "assistant",
    actor_id: str | None = None,
    lifecycle_state: str | None = None,
    materialization: str | None = None,
    materialized_content: str | None = None,
    is_pinned: bool | None = None,
    priority: int | None = None,
    reason: str | None = None,
    metadata: dict[str, Any] | None = None,
    allow_archive_pinned: bool = False,
) -> ChatContextItem:
    """Apply one optimistic, append-only-audited context transition."""
    item = _context_item_for_scope(
        context_item_id,
        user_id=user_id,
        session_id=session_id,
    )
    expected = int(expected_version)
    if item.version != expected:
        raise ContextVersionConflict(
            f"Context item {item.id} is at version {item.version}, not {expected}."
        )

    next_state = item.lifecycle_state
    if lifecycle_state is not None:
        next_state = str(lifecycle_state).strip().lower()
        if next_state not in VALID_CONTEXT_STATES:
            raise ContextFabricError(f"Invalid context lifecycle state: {next_state}.")
    next_materialization = item.materialization
    if materialization is not None:
        next_materialization = str(materialization).strip().lower()
        if next_materialization not in VALID_MATERIALIZATIONS:
            raise ContextFabricError(
                f"Invalid context materialization: {next_materialization}."
            )
    next_pinned = item.is_pinned if is_pinned is None else bool(is_pinned)
    if item.is_pinned and next_state in {"archived", "excluded", "deleted"} and not allow_archive_pinned:
        raise ContextPolicyError(
            f"Pinned context item {item.id} cannot be moved to {next_state}."
        )
    if next_pinned and next_state == "deleted":
        raise ContextPolicyError("A deleted context item cannot remain pinned.")
    if next_materialization in {"excerpt", "summary"} and materialized_content is None and item.materialized_content is None:
        raise ContextPolicyError(
            f"{next_materialization} materialization requires durable materialized content."
        )

    before_state = item.lifecycle_state
    before_materialization = item.materialization
    item.lifecycle_state = next_state
    item.materialization = next_materialization
    item.is_pinned = next_pinned
    item.last_turn_id = turn_id or item.last_turn_id
    if priority is not None:
        item.priority = max(0, min(100, int(priority)))
    if materialized_content is not None:
        item.materialized_content = str(materialized_content)
        item.content_hash = _hash_text(item.materialized_content)
        item.token_estimate = _estimate_tokens(item.materialized_content)
    elif next_materialization in {"full", "reference", "overflow"}:
        item.materialized_content = None
        item.content_hash = item.evidence.content_hash
        item.token_estimate = item.evidence.token_estimate
    item.version += 1
    item.updated_at = utc_now_db()

    changed_fields = []
    if before_state != item.lifecycle_state:
        changed_fields.append("lifecycle")
    if before_materialization != item.materialization or materialized_content is not None:
        changed_fields.append("materialization")
    if is_pinned is not None:
        changed_fields.append("pin")
    if priority is not None:
        changed_fields.append("priority")
    event_type = "+".join(changed_fields) if changed_fields else "touched"
    db.session.add(ChatContextEvent(
        context_item_id=item.id,
        session_id=item.session_id,
        turn_id=turn_id,
        actor_type=str(actor_type or "assistant")[:24],
        actor_id=str(actor_id or "")[:255] or None,
        event_type=event_type[:32],
        from_state=before_state,
        to_state=item.lifecycle_state,
        from_materialization=before_materialization,
        to_materialization=item.materialization,
        expected_version=expected,
        result_version=item.version,
        reason=str(reason) if reason else None,
        metadata_json=dict(metadata) if isinstance(metadata, dict) else None,
    ))
    db.session.flush()
    return item


def materialize_context_item(
    context_item_id: str,
    *,
    user_id: int | None = None,
    session_id: str | None = None,
    start_offset: int = 0,
    max_chars: int | None = None,
) -> dict[str, Any]:
    item = _context_item_for_scope(
        context_item_id,
        user_id=user_id,
        session_id=session_id,
    )
    if item.lifecycle_state == "deleted":
        raise ContextFabricError("Context item has been deleted.")
    if item.materialization in {"summary", "excerpt"}:
        content = str(item.materialized_content or "")
        start = max(0, int(start_offset or 0))
        end = len(content) if max_chars is None else min(len(content), start + max(0, int(max_chars)))
        return {
            "context_item": item.to_dict(include_materialized_content=False),
            "content": content[start:end],
            "start_offset": start,
            "end_offset": end,
            "has_more": end < len(content),
            "next_offset": end if end < len(content) else None,
            "evidence_id": item.evidence_id,
        }
    if item.materialization in {"reference", "overflow"}:
        return {
            "context_item": item.to_dict(include_materialized_content=False),
            "content": "",
            "evidence_id": item.evidence_id,
            "manifest": {
                "materialization": item.materialization,
                "char_count": item.evidence.char_count,
                "token_estimate": item.evidence.token_estimate,
                "content_hash": item.evidence.content_hash,
                "next_action": "Read the durable evidence by ID or rematerialize this context item.",
            },
        }
    evidence_payload = read_evidence(
        item.evidence_id,
        user_id=item.user_id,
        session_id=item.session_id,
        start_offset=start_offset,
        max_chars=max_chars,
    )
    return {
        "context_item": item.to_dict(include_materialized_content=False),
        **evidence_payload,
    }


def read_context_item_evidence(
    context_item_id: str,
    *,
    user_id: int | None = None,
    session_id: str | None = None,
    start_offset: int = 0,
    max_chars: int | None = 8_000,
) -> dict[str, Any]:
    """Read the raw evidence behind an addressable context item.

    Unlike ``materialize_context_item``, this operation deliberately crosses a
    reference/archive projection. It is the bounded, integrity-checked
    dereference used by the model-facing archive tool.
    """
    item = _context_item_for_scope(
        context_item_id,
        user_id=user_id,
        session_id=session_id,
    )
    if item.lifecycle_state == "deleted":
        raise ContextFabricError("Context item has been deleted.")
    payload = read_evidence(
        item.evidence_id,
        user_id=item.user_id,
        session_id=item.session_id,
        start_offset=start_offset,
        max_chars=max_chars,
        verify_integrity=True,
    )
    return {
        "status": "ok",
        "context_item": item.to_dict(include_materialized_content=False),
        **payload,
    }


def _context_projection_envelope(
    item: ChatContextItem | None,
    *,
    context_item_id: str,
    message: dict[str, Any],
) -> str:
    legacy_meta = (
        message.get("_context_optimization")
        if isinstance(message.get("_context_optimization"), dict)
        else {}
    )
    legacy_ref = legacy_meta.get("ref_id")
    if item is None:
        payload = {
            "status": "context_item_unavailable",
            "context_item_id": context_item_id,
            "context_ref_id": legacy_ref,
            "message": (
                "This message referenced a context item that is unavailable in "
                "the current user/session scope. Raw message content was withheld."
            ),
        }
        return _serialize_content(payload)

    state = str(item.lifecycle_state or "active")
    materialization = str(item.materialization or "reference")
    payload = {
        "status": (
            "context_archived"
            if state == "archived"
            else "context_excluded"
            if state == "excluded"
            else "context_deleted"
            if state == "deleted"
            else "context_reference"
        ),
        "context_item_id": item.id,
        "evidence_id": item.evidence_id,
        "context_ref_id": legacy_ref,
        "title": item.title or "",
        "kind": item.kind,
        "lifecycle_state": state,
        "materialization": materialization,
        "version": item.version,
        "is_pinned": bool(item.is_pinned),
        "char_count": item.evidence.char_count,
        "estimated_tokens": item.evidence.token_estimate,
        "content_hash": item.evidence.content_hash,
        "message": (
            "Raw evidence is outside the current provider payload. Use "
            "context_read_result with context_item_id for a bounded exact slice, "
            "context_search_result to locate it, or context_manage_results to "
            "restore/materialize it when policy permits."
        ),
        "next_actions": [
            "Search by context_item_id or across the durable context archive.",
            "Read a bounded exact slice with integrity verification.",
            "Restore or materialize the item through a versioned transition.",
        ],
    }
    return _serialize_content(payload)


def project_messages_for_provider(
    messages: Iterable[dict[str, Any]] | None,
    *,
    session_id: str,
    user_id: int,
) -> ContextProjectionResult:
    """Project context-tagged chat messages from authoritative lifecycle state.

    The function never mutates stored messages. Tool protocol rows are retained
    even when their evidence is archived, excluded, or deleted; only their
    provider-visible content is replaced.
    """
    _session_for_user(session_id, user_id)
    rows = [
        copy.deepcopy(message)
        for message in (messages or [])
        if isinstance(message, dict)
    ]
    referenced_ids: list[str] = []
    for message in rows:
        metadata = message.get("_context_fabric")
        context_item_id = (
            str(metadata.get("context_item_id") or "").strip()
            if isinstance(metadata, dict)
            else ""
        )
        if context_item_id and context_item_id not in referenced_ids:
            referenced_ids.append(context_item_id)

    item_map: dict[str, ChatContextItem] = {}
    if referenced_ids:
        scoped_items = (
            ChatContextItem.query
            .filter(
                ChatContextItem.id.in_(referenced_ids),
                ChatContextItem.session_id == str(session_id),
                ChatContextItem.user_id == int(user_id),
            )
            .all()
        )
        item_map = {item.id: item for item in scoped_items}

    projected: list[str] = []
    full: list[str] = []
    referenced: list[str] = []
    unavailable: list[str] = []
    lifecycle_counts: dict[str, int] = {}
    materialization_counts: dict[str, int] = {}

    for message in rows:
        metadata = message.get("_context_fabric")
        context_item_id = (
            str(metadata.get("context_item_id") or "").strip()
            if isinstance(metadata, dict)
            else ""
        )
        if not context_item_id:
            continue
        projected.append(context_item_id)
        item = item_map.get(context_item_id)
        if item is None:
            unavailable.append(context_item_id)
            message["content"] = _context_projection_envelope(
                None,
                context_item_id=context_item_id,
                message=message,
            )
            continue

        state = str(item.lifecycle_state or "active")
        materialization = str(item.materialization or "reference")
        lifecycle_counts[state] = lifecycle_counts.get(state, 0) + 1
        materialization_counts[materialization] = (
            materialization_counts.get(materialization, 0) + 1
        )

        if state == "active" and materialization == "full":
            payload = read_evidence(
                item.evidence_id,
                user_id=user_id,
                session_id=session_id,
                verify_integrity=True,
            )
            message["content"] = payload["content"]
            full.append(context_item_id)
            continue

        if state == "active" and materialization in {"excerpt", "summary"}:
            message["content"] = _serialize_content({
                "status": f"context_{materialization}",
                "context_item_id": item.id,
                "evidence_id": item.evidence_id,
                "title": item.title or "",
                "kind": item.kind,
                "lifecycle_state": state,
                "materialization": materialization,
                "version": item.version,
                "content_hash": item.content_hash,
                "content": str(item.materialized_content or ""),
                "message": (
                    "This is a durable materialized projection. Use "
                    "context_read_result with context_item_id for exact raw evidence."
                ),
            })
            referenced.append(context_item_id)
            continue

        message["content"] = _context_projection_envelope(
            item,
            context_item_id=context_item_id,
            message=message,
        )
        referenced.append(context_item_id)

    return ContextProjectionResult(
        messages=tuple(rows),
        projected_item_ids=tuple(projected),
        full_item_ids=tuple(full),
        reference_item_ids=tuple(referenced),
        unavailable_item_ids=tuple(unavailable),
        lifecycle_counts=lifecycle_counts,
        materialization_counts=materialization_counts,
    )


def store_tool_result(
    *,
    session_id: str,
    user_id: int,
    turn_id: str | None,
    tool_name: str,
    tool_call_id: str,
    result: Any,
    title: str | None = None,
    context_stewardship: str = "assisted",
    metadata: dict[str, Any] | None = None,
) -> tuple[ChatEvidence, ChatContextItem]:
    """Store a complete tool result before any prompt projection is produced."""
    raw = _serialize_content(result)
    evidence = record_evidence(
        session_id=session_id,
        user_id=user_id,
        turn_id=turn_id,
        kind="tool_result",
        role="tool",
        content=raw,
        source_type="tool",
        source_id=str(tool_call_id or "") or None,
        metadata={
            "tool_name": str(tool_name or ""),
            "tool_call_id": str(tool_call_id or ""),
            "context_stewardship": normalize_context_mode(context_stewardship),
            **(dict(metadata) if isinstance(metadata, dict) else {}),
        },
    )
    stewardship = normalize_context_mode(context_stewardship)
    item = create_context_item(
        evidence=evidence,
        user_id=user_id,
        kind="tool_result",
        title=title or f"Tool result: {tool_name}",
        turn_id=turn_id,
        lifecycle_state="active",
        materialization=(
            "reference"
            if stewardship == "aggressive" and len(raw) > 4_000
            else "full"
        ),
        source_ref={
            "tool_name": str(tool_name or ""),
            "tool_call_id": str(tool_call_id or ""),
        },
        metadata={
            "context_stewardship": stewardship,
        },
        actor_type="runtime",
        reason="Complete tool result persisted before prompt projection.",
    )
    return evidence, item


def _find_literal_in_evidence(
    evidence: ChatEvidence,
    needle: str,
) -> int:
    """Find a case-folded literal without joining a chunked evidence body."""
    if evidence.content is not None:
        content = str(evidence.content)
        if _hash_text(content) != evidence.content_hash:
            raise EvidenceIntegrityError(
                f"Evidence hash mismatch for {evidence.id}."
            )
        return content.casefold().find(needle)

    full_hasher = hashlib.sha256()
    expected_ordinal = 0
    expected_offset = 0
    first_match = -1
    overlap = max(0, len(needle) - 1)
    tail = ""
    for chunk in evidence.chunks:
        if chunk.ordinal != expected_ordinal or chunk.start_offset != expected_offset:
            raise EvidenceIntegrityError(
                f"Evidence chunk ordering mismatch for {evidence.id}."
            )
        chunk_text = str(chunk.content)
        if _hash_text(chunk_text) != chunk.content_hash:
            raise EvidenceIntegrityError(
                f"Evidence chunk hash mismatch for {evidence.id}:{chunk.ordinal}."
            )
        full_hasher.update(chunk_text.encode("utf-8", errors="strict"))
        if first_match < 0:
            combined = tail + chunk_text
            local_match = combined.casefold().find(needle)
            if local_match >= 0:
                first_match = (
                    chunk.start_offset - len(tail) + local_match
                )
            tail = combined[-overlap:] if overlap else ""
        expected_ordinal += 1
        expected_offset = chunk.end_offset
    if expected_offset != evidence.char_count:
        raise EvidenceIntegrityError(
            f"Evidence chunk coverage mismatch for {evidence.id}."
        )
    if full_hasher.hexdigest() != evidence.content_hash:
        raise EvidenceIntegrityError(
            f"Evidence hash mismatch for {evidence.id}."
        )
    return first_match


def search_context_archive(
    *,
    session_id: str,
    user_id: int,
    query: str,
    lifecycle_states: Iterable[str] = ("archived",),
    context_item_ids: Iterable[str] | None = None,
    limit: int = 10,
    scan_limit: int = 500,
) -> dict[str, Any]:
    """Search durable context with literal case-insensitive matching.

    This encrypted-store baseline intentionally does not pretend to be semantic
    retrieval. A later search projection can accelerate it without changing the
    evidence or context identifiers.
    """
    _session_for_user(session_id, user_id)
    needle = str(query or "").strip().casefold()
    if not needle:
        raise ContextFabricError("query is required")
    if len(needle) > 20_000:
        raise ContextFabricError("query is too long for literal archive search")
    states = {
        str(state).strip().lower()
        for state in lifecycle_states
        if str(state).strip().lower() in VALID_CONTEXT_STATES
    }
    if not states:
        states = {"archived"}
    result_limit = max(1, min(100, int(limit or 10)))
    scan_cap = max(result_limit, min(5_000, int(scan_limit or 500)))
    candidate_query = (
        ChatContextItem.query
        .filter_by(session_id=session_id, user_id=user_id)
        .filter(ChatContextItem.lifecycle_state.in_(states))
        .order_by(ChatContextItem.updated_at.desc())
    )
    selected_ids = {
        str(item_id or "").strip()
        for item_id in (context_item_ids or [])
        if str(item_id or "").strip()
    }
    if selected_ids:
        candidate_query = candidate_query.filter(
            ChatContextItem.id.in_(sorted(selected_ids))
        )
    candidates = candidate_query.limit(scan_cap).all()
    matches = []
    for item in candidates:
        evidence = item.evidence
        title = str(item.title or "")
        metadata_text = _serialize_content({
            "item": item.metadata_json or {},
            "source_ref": item.source_ref_json or {},
            "evidence": evidence.metadata_json or {},
        })
        metadata_match = needle in "\n".join([title, metadata_text]).casefold()
        content_index = _find_literal_in_evidence(evidence, needle)
        if not metadata_match and content_index < 0:
            continue
        if content_index >= 0:
            start = max(0, content_index - 160)
            end = min(
                evidence.char_count,
                content_index + len(query) + 240,
            )
            snippet = read_evidence(
                evidence.id,
                user_id=user_id,
                session_id=session_id,
                start_offset=start,
                max_chars=end - start,
                verify_integrity=True,
            )["content"]
        else:
            start = None
            end = None
            snippet = title or metadata_text[:400]
        matches.append({
            "context_item_id": item.id,
            "evidence_id": evidence.id,
            "title": title,
            "lifecycle_state": item.lifecycle_state,
            "materialization": item.materialization,
            "version": item.version,
            "snippet": snippet,
            "match_start_offset": content_index if content_index >= 0 else None,
            "snippet_start_offset": start,
            "snippet_end_offset": end,
            "content_hash": evidence.content_hash,
            "updated_at": item.updated_at.isoformat() if item.updated_at else None,
        })
        if len(matches) >= result_limit:
            break
    return {
        "query": query,
        "match_type": "literal_case_insensitive",
        "lifecycle_states": sorted(states),
        "context_item_ids": sorted(selected_ids),
        "scanned": len(candidates),
        "matches": matches,
        "count": len(matches),
    }


def active_context_manifest(*, session_id: str, user_id: int) -> list[dict[str, Any]]:
    _session_for_user(session_id, user_id)
    items = (
        ChatContextItem.query
        .filter_by(
            session_id=session_id,
            user_id=user_id,
            lifecycle_state="active",
        )
        .order_by(
            ChatContextItem.is_pinned.desc(),
            ChatContextItem.priority.desc(),
            ChatContextItem.created_at.asc(),
        )
        .all()
    )
    return [item.to_dict(include_materialized_content=False) for item in items]
