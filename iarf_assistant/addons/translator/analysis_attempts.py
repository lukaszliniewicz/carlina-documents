"""Durable, provider-agnostic evidence for Phase-5 analysis calls.

The module owns no provider, tool executor, or artefact semantics.  It only
preserves the exact request and response boundary that those later stages use.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, cast

from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import make_transient_to_detached

from carlina.core.utc import utc_iso, utc_now_db
from iarf_assistant.extensions import db

from .analysis_contract import private_witness, private_witness_candidates
from .block_contract import (
    BlockContractError,
    TranslationSourcePlan as CanonicalSourcePlan,
    sha256_text,
)
from .block_persistence import rehydrate_source_plan
from .models import (
    TranslationAnalysisAttempt,
    TranslationAnalysisAttemptEvent,
    TranslationOperation,
)
from .models import TranslationSourcePlan as SourcePlanRecord

ANALYSIS_ATTEMPT_KINDS = frozenset(
    {
        "document_brief",
        "style_guide",
        "entity_register",
        "terminology_consolidation",
        "research",
        "embedding_projection",
        "memory_derivation",
    }
)
ANALYSIS_EVENT_TYPES = frozenset(
    {
        "request_prepared",
        "provider_dispatched",
        "provider_response",
        "provider_failure",
        "post_response_fence_failed",
        "provider_outcome_uncertain",
        "cancelled",
        "completed",
        "failed",
    }
)
_MAX_JSON_BYTES = 4 * 1024 * 1024
_MAX_EVENT_CHAIN = 10_000
_MAX_LINEAGE = 128
_MAX_MESSAGE_BYTES = 2 * 1024 * 1024
# Only an authenticated research request may use the wider source-input
# envelope. Provider responses, tool arguments/results, events, and every
# other attempt kind retain the generic limits above.
_MAX_RESEARCH_PACKET_BYTES = 8 * 1024 * 1024
_MAX_RESEARCH_INPUT_MESSAGE_BYTES = 4 * 1024 * 1024
_MAX_TOOL_CALLS_PER_MESSAGE = 128
_MAX_TOOL_ID_BYTES = 16 * 1024
_MAX_TOOL_NAME_BYTES = 512
ANALYSIS_CONTRACT_VERSION_V1 = "analysis-attempt-v1"
ANALYSIS_CONTRACT_VERSION_V2 = "analysis-attempt-v2"
ANALYSIS_CONTRACT_VERSIONS = frozenset(
    {ANALYSIS_CONTRACT_VERSION_V1, ANALYSIS_CONTRACT_VERSION_V2}
)
# Compatibility alias for existing non-agentic analysis callers. New research
# turns must opt into v2 explicitly; silently changing v1 would make old durable
# evidence mean something different after an upgrade.
ANALYSIS_CONTRACT_VERSION = ANALYSIS_CONTRACT_VERSION_V1

_WITNESS_PURPOSES = {
    "packet": "translator-analysis-attempt-packet-v1",
    "model": "translator-analysis-attempt-model-v1",
    "profile": "translator-analysis-attempt-profile-v1",
    "settings": "translator-analysis-attempt-settings-v1",
    "budget": "translator-analysis-attempt-budget-v1",
    "tools": "translator-analysis-attempt-tools-v1",
    "event_payload": "translator-analysis-attempt-event-payload-v1",
    "record": "translator-analysis-attempt-record-v1",
    "event": "translator-analysis-attempt-event-v1",
    "dispatch": "translator-analysis-attempt-dispatch-v1",
}


@dataclass(slots=True)
class AnalysisVerificationContext:
    """Invocation-local reuse for already verified attempt evidence.

    Callers may preload one durable graph in bounded queries. Every cache miss
    still performs the complete witness, lineage, receipt, and event-chain
    verification. State/version receipts keep a changed attempt from reusing an
    older result; a new invocation starts with an empty context and therefore
    re-verifies durable truth from the database.
    """

    _attempts: dict[str, TranslationAnalysisAttempt] = field(
        default_factory=dict, init=False, repr=False
    )
    _attempt_keys: dict[str, tuple[str, str, str, str, int]] = field(
        default_factory=dict, init=False, repr=False
    )
    _event_rows: dict[str, tuple[TranslationAnalysisAttemptEvent, ...]] = field(
        default_factory=dict, init=False, repr=False
    )
    _verified_events: dict[
        tuple[str, str, str, str, int],
        tuple[TranslationAnalysisAttemptEvent, ...],
    ] = field(default_factory=dict, init=False, repr=False)
    _attempt_data: dict[tuple[str, str, str, str, int], dict[str, Any]] = field(
        default_factory=dict, init=False, repr=False
    )
    _outputs: dict[tuple[str, str, str, str, int], Any] = field(
        default_factory=dict, init=False, repr=False
    )

    def prime(
        self,
        attempts: Sequence[TranslationAnalysisAttempt],
        events: Sequence[TranslationAnalysisAttemptEvent],
    ) -> None:
        attempt_rows = {
            attempt.id: cast(
                TranslationAnalysisAttempt,
                _detached_evidence_snapshot(attempt),
            )
            for attempt in attempts
        }
        if len(attempt_rows) != len(attempts):
            raise BlockContractError("analysis-verification-preload")
        grouped: dict[str, list[TranslationAnalysisAttemptEvent]] = {
            attempt_id: [] for attempt_id in attempt_rows
        }
        for source_event in events:
            event = cast(
                TranslationAnalysisAttemptEvent,
                _detached_evidence_snapshot(source_event),
            )
            if event.attempt_id not in grouped:
                raise BlockContractError("analysis-verification-preload")
            grouped[event.attempt_id].append(event)
        self._attempts.update(attempt_rows)
        for attempt_id, attempt in attempt_rows.items():
            key = _verification_key(attempt)
            previous = self._attempt_keys.get(attempt_id)
            if previous is not None and previous != key:
                self._verified_events.pop(previous, None)
                self._attempt_data.pop(previous, None)
                self._outputs.pop(previous, None)
            self._attempt_keys[attempt_id] = key
        self._event_rows.update(
            {
                attempt_id: tuple(sorted(rows, key=lambda event: event.sequence))
                for attempt_id, rows in grouped.items()
            }
        )


def _detached_evidence_snapshot(row: Any) -> Any:
    """Copy loaded columns into a commit-proof, read-only ORM snapshot."""

    mapper = inspect(type(row))
    snapshot = type(row)()
    for attribute in mapper.column_attrs:
        setattr(snapshot, attribute.key, getattr(row, attribute.key))
    make_transient_to_detached(snapshot)
    return snapshot


def _verification_key(
    attempt: TranslationAnalysisAttempt,
) -> tuple[str, str, str, str, int]:
    values = (
        attempt.id,
        attempt.attempt_record_hash,
        attempt.dispatch_receipt_hash,
        attempt.dispatch_state,
    )
    if (
        not all(isinstance(value, str) and value for value in values)
        or not isinstance(attempt.dispatch_version, int)
        or isinstance(attempt.dispatch_version, bool)
        or attempt.dispatch_version < 1
    ):
        raise BlockContractError("analysis-verification-key")
    return (*cast(tuple[str, str, str, str], values), attempt.dispatch_version)


def _ensure_transaction(session: Any) -> None:
    connection = session.connection()
    if connection.dialect.name != "sqlite":
        return
    proxied = connection.connection
    driver = getattr(proxied, "driver_connection", proxied)
    if not bool(getattr(driver, "in_transaction", False)):
        connection.exec_driver_sql("BEGIN")


def _canonical(value: object, code: str, *, maximum: int = _MAX_JSON_BYTES) -> Any:
    """Return a JSON round-trip, rejecting coercion and oversized material."""

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
        raise BlockContractError(code) from exc
    if len(encoded) > maximum or restored != value:
        raise BlockContractError(code)
    return restored


def canonical_analysis_value(
    value: object, code: str = "analysis-json", *, maximum: int = _MAX_JSON_BYTES
) -> Any:
    """Public bounded JSON rematerialization for injected adapter boundaries."""

    return _canonical(value, code, maximum=maximum)


def _hash(value: object, code: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise BlockContractError(code)
    return value


def _time(value: object, code: str = "analysis-attempt-created-at") -> str:
    if not isinstance(value, datetime):
        raise BlockContractError(code)
    return utc_iso(value, timespec="microseconds")


def _mapping(
    value: object, code: str, *, maximum: int = _MAX_JSON_BYTES
) -> dict[str, Any]:
    material = _canonical(value, code, maximum=maximum)
    if not isinstance(material, dict):
        raise BlockContractError(code)
    return cast(dict[str, Any], material)


def _version(value: object, code: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value.encode("utf-8")) > 64
        or value not in ANALYSIS_CONTRACT_VERSIONS
    ):
        raise BlockContractError(code)
    return value


def _message_content(
    value: object,
    *,
    allow_none: bool = False,
    maximum: int = _MAX_MESSAGE_BYTES,
) -> str | None:
    if allow_none and value is None:
        return None
    if not isinstance(value, str) or len(value.encode("utf-8")) > maximum:
        raise BlockContractError("analysis-packet-message")
    return value


def _tool_identifier(value: object, code: str, *, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or not value.isprintable()
        or len(value.encode("utf-8")) > maximum
    ):
        raise BlockContractError(code)
    return value


def _validate_v1_messages(messages: list[Any]) -> None:
    for message in messages:
        if not isinstance(message, dict) or set(message) != {"role", "content"}:
            raise BlockContractError("analysis-packet-message")
        if not isinstance(message["role"], str) or message["role"] not in {
            "system",
            "user",
            "assistant",
            "tool",
        }:
            raise BlockContractError("analysis-packet-message")
        _message_content(message["content"])


def _validate_v2_assistant_message(
    message: dict[str, Any],
    *,
    seen_call_ids: set[str],
    pending: dict[str, str],
    duplicate_code: str = "analysis-packet-tool-call",
) -> None:
    if set(message) not in (
        {"role", "content"},
        {"role", "content", "tool_calls"},
    ):
        raise BlockContractError("analysis-packet-message")
    tool_calls = message.get("tool_calls")
    _message_content(message["content"], allow_none=tool_calls is not None)
    if tool_calls is None:
        return
    if (
        not isinstance(tool_calls, list)
        or not tool_calls
        or len(tool_calls) > _MAX_TOOL_CALLS_PER_MESSAGE
    ):
        raise BlockContractError("analysis-packet-tool-call")
    for call in tool_calls:
        if (
            not isinstance(call, dict)
            or set(call) != {"id", "type", "function"}
            or call.get("type") != "function"
            or not isinstance(call.get("function"), dict)
            or set(call["function"]) != {"name", "arguments"}
        ):
            raise BlockContractError("analysis-packet-tool-call")
        call_id = _tool_identifier(
            call["id"],
            "analysis-packet-tool-call-id",
            maximum=_MAX_TOOL_ID_BYTES,
        )
        name = _tool_identifier(
            call["function"]["name"],
            "analysis-packet-tool-name",
            maximum=_MAX_TOOL_NAME_BYTES,
        )
        arguments = call["function"]["arguments"]
        if (
            not isinstance(arguments, str)
            or len(arguments.encode("utf-8")) > _MAX_MESSAGE_BYTES
            or call_id in seen_call_ids
        ):
            if call_id in seen_call_ids:
                raise BlockContractError(duplicate_code)
            raise BlockContractError("analysis-packet-tool-call")
        seen_call_ids.add(call_id)
        pending[call_id] = name


def canonical_analysis_assistant_message(value: object) -> dict[str, Any]:
    """Validate and retain one exact replayable assistant response message."""

    message = _mapping(value, "analysis-assistant-message")
    if message.get("role") != "assistant":
        raise BlockContractError("analysis-assistant-message")
    _validate_v2_assistant_message(message, seen_call_ids=set(), pending={})
    return message


def validate_analysis_response_extension(
    packet: Mapping[str, Any], response: object
) -> dict[str, Any]:
    """Validate one v2 assistant response as an exact packet extension.

    A provider response is not an independent assistant message: it extends
    the immutable transcript that was sent to the provider.  Revalidate the
    complete packet first, then validate the response against its global tool
    call-id set.  A response containing tool calls is intentionally allowed to
    leave those calls pending; the research turn persists the corresponding
    tool results in the next witnessed packet.  This preserves the provider's
    exact IDs and raw argument strings without pretending that an unexecuted
    call already has a result.
    """

    if not isinstance(packet, Mapping):
        raise BlockContractError("analysis-response-packet")
    try:
        packet_data = _packet(
            {
                "messages": packet["messages"],
                "input": packet["input"],
            },
            ANALYSIS_CONTRACT_VERSION_V2,
            analysis_kind="research",
        )
    except (KeyError, TypeError) as exc:
        raise BlockContractError("analysis-response-packet") from exc
    response_data = canonical_analysis_assistant_message(response)

    seen_call_ids: set[str] = set()
    pending: dict[str, str] = {}
    for message in packet_data["messages"]:
        if isinstance(message, dict) and message.get("role") == "assistant":
            _validate_v2_assistant_message(
                message,
                seen_call_ids=seen_call_ids,
                pending=pending,
            )
        elif isinstance(message, dict) and message.get("role") == "tool":
            call_id = message["tool_call_id"]
            pending.pop(call_id, None)
    if pending:
        # `_packet` currently rejects this, but keep the invariant local if
        # the packet validator gains an allow-pending mode in the future.
        raise BlockContractError("analysis-response-packet")

    _validate_v2_assistant_message(
        response_data,
        seen_call_ids=seen_call_ids,
        pending=pending,
        duplicate_code="analysis-response-tool-call-duplicate",
    )
    requested = response_data.get("tool_calls")
    if requested is not None:
        declarations = packet.get("tool_declarations")
        declared_names: set[str] = set()
        if isinstance(declarations, list):
            for declaration in declarations:
                if not isinstance(declaration, Mapping):
                    continue
                function = declaration.get("function")
                if isinstance(function, Mapping) and isinstance(
                    function.get("name"), str
                ):
                    declared_names.add(function["name"])
        for call in requested:
            function = call["function"]
            if function["name"] not in declared_names:
                raise BlockContractError("analysis-response-tool-undeclared")
    return response_data


def _validate_v2_messages(
    messages: list[Any], *, input_message_maximum: int = _MAX_MESSAGE_BYTES
) -> None:
    """Validate a replayable provider transcript without normalizing it.

    Provider tool-call identifiers and raw argument strings are deliberately
    retained exactly. Parsed/validated arguments belong to the separate tool
    execution evidence, not this network-bound request packet.
    """

    pending: dict[str, str] = {}
    seen_call_ids: set[str] = set()
    for message in messages:
        if not isinstance(message, dict):
            raise BlockContractError("analysis-packet-message")
        role = message.get("role")
        if role in {"system", "user"}:
            if pending or set(message) != {"role", "content"}:
                raise BlockContractError("analysis-packet-message")
            _message_content(message["content"], maximum=input_message_maximum)
            continue
        if role == "assistant":
            if pending:
                raise BlockContractError("analysis-packet-message")
            _validate_v2_assistant_message(
                message,
                seen_call_ids=seen_call_ids,
                pending=pending,
            )
            continue
        if role == "tool":
            if set(message) != {"role", "tool_call_id", "name", "content"}:
                raise BlockContractError("analysis-packet-message")
            call_id = _tool_identifier(
                message["tool_call_id"],
                "analysis-packet-tool-call-id",
                maximum=_MAX_TOOL_ID_BYTES,
            )
            name = _tool_identifier(
                message["name"],
                "analysis-packet-tool-name",
                maximum=_MAX_TOOL_NAME_BYTES,
            )
            if pending.get(call_id) != name:
                raise BlockContractError("analysis-packet-tool-result")
            _message_content(message["content"])
            pending.pop(call_id)
            continue
        raise BlockContractError("analysis-packet-message")
    if pending:
        raise BlockContractError("analysis-packet-tool-result")


def _packet(
    value: Mapping[str, Any],
    contract_version: str,
    *,
    analysis_kind: str | None = None,
) -> dict[str, Any]:
    research_packet = (
        contract_version == ANALYSIS_CONTRACT_VERSION_V2 and analysis_kind == "research"
    )
    material = _mapping(
        value,
        "analysis-packet",
        maximum=(_MAX_RESEARCH_PACKET_BYTES if research_packet else _MAX_JSON_BYTES),
    )
    if set(material) != {"messages", "input"} or not isinstance(
        material["messages"], list
    ):
        raise BlockContractError("analysis-packet")
    if len(material["messages"]) > 256:
        raise BlockContractError("analysis-packet")
    if contract_version == ANALYSIS_CONTRACT_VERSION_V1:
        _validate_v1_messages(material["messages"])
    elif contract_version == ANALYSIS_CONTRACT_VERSION_V2:
        _validate_v2_messages(
            material["messages"],
            input_message_maximum=(
                _MAX_RESEARCH_INPUT_MESSAGE_BYTES
                if research_packet
                else _MAX_MESSAGE_BYTES
            ),
        )
    else:  # Defensive parity with the durable CHECK constraint.
        raise BlockContractError("analysis-contract-version")
    return material


def _tools(value: object) -> list[Any]:
    material = _canonical(value, "analysis-tools")
    if not isinstance(material, list) or len(material) > 128:
        raise BlockContractError("analysis-tools")
    return material


def validate_research_attempt_packet(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the final sealed envelope for one research attempt."""

    return _packet(
        value,
        ANALYSIS_CONTRACT_VERSION_V2,
        analysis_kind="research",
    )


def _digest(value: object) -> str:
    return sha256_text(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    )


def _witness(value: object, component: str) -> str:
    try:
        purpose = _WITNESS_PURPOSES[component]
    except KeyError as exc:
        raise BlockContractError("analysis-witness-purpose") from exc
    return private_witness(value, purpose=purpose)


def _witness_matches(value: object, stored: object, component: str) -> bool:
    if not isinstance(stored, str):
        return False
    try:
        purpose = _WITNESS_PURPOSES[component]
    except KeyError as exc:
        raise BlockContractError("analysis-witness-purpose") from exc
    return stored in private_witness_candidates(value, purpose=purpose)


def _request_identity(
    *,
    analysis_kind: str,
    contract_version: str,
    source_plan_hash: str,
    source_content_hash: str,
    packet_hash: str,
    model_hash: str,
    profile_hash: str,
    settings_hash: str,
    budget_hash: str,
    tools_hash: str,
) -> dict[str, str]:
    return {
        "analysis_kind": analysis_kind,
        "contract_version": contract_version,
        "source_plan_hash": source_plan_hash,
        "source_content_hash": source_content_hash,
        "packet_hash": packet_hash,
        "model_hash": model_hash,
        "profile_hash": profile_hash,
        "settings_hash": settings_hash,
        "budget_hash": budget_hash,
        "tools_hash": tools_hash,
    }


def _record_values(attempt: TranslationAnalysisAttempt) -> dict[str, object]:
    return {
        "id": attempt.id,
        "project_id": attempt.project_id,
        "owner_user_id": attempt.owner_user_id,
        "operation_id": attempt.operation_id,
        "source_plan_id": attempt.source_plan_id,
        "analysis_kind": attempt.analysis_kind,
        "contract_version": attempt.contract_version,
        "source_plan_hash": attempt.source_plan_hash,
        "source_content_hash": attempt.source_content_hash,
        "operation_version": attempt.operation_version,
        "queue_job_id": attempt.queue_job_id,
        "operation_lease_expires_at": _time(attempt.operation_lease_expires_at),
        "lease_fence": attempt.lease_fence,
        "packet_hash": attempt.packet_hash,
        "model_hash": attempt.model_hash,
        "profile_hash": attempt.profile_hash,
        "settings_hash": attempt.settings_hash,
        "budget_hash": attempt.budget_hash,
        "tools_hash": attempt.tools_hash,
        "request_hash": attempt.request_hash,
        "retry_of_attempt_id": attempt.retry_of_attempt_id,
        "retry_of_attempt_hash": attempt.retry_of_attempt_hash,
        "rebase_of_attempt_id": attempt.rebase_of_attempt_id,
        "rebase_of_attempt_hash": attempt.rebase_of_attempt_hash,
        "created_at": _time(attempt.created_at),
    }


def _attempt_hash(values: Mapping[str, object]) -> str:
    return private_witness(dict(values), purpose=_WITNESS_PURPOSES["record"])


def _event_hash(event: TranslationAnalysisAttemptEvent) -> str:
    return private_witness(
        {
            "id": event.id,
            "attempt_id": event.attempt_id,
            "sequence": event.sequence,
            "event_type": event.event_type,
            "payload_hash": event.payload_hash,
            "predecessor_event_hash": event.predecessor_event_hash,
            "created_at": _time(event.created_at, "analysis-event-created-at"),
        },
        purpose=_WITNESS_PURPOSES["event"],
    )


def _dispatch_receipt_values(
    attempt: TranslationAnalysisAttempt,
    event: TranslationAnalysisAttemptEvent,
) -> dict[str, object]:
    return {
        "attempt_id": attempt.id,
        "dispatch_state": attempt.dispatch_state,
        "dispatch_version": attempt.dispatch_version,
        "dispatch_claimed_at": (
            _time(attempt.dispatch_claimed_at, "analysis-dispatch-claimed-at")
            if attempt.dispatch_claimed_at is not None
            else None
        ),
        "dispatch_finished_at": (
            _time(attempt.dispatch_finished_at, "analysis-dispatch-finished-at")
            if attempt.dispatch_finished_at is not None
            else None
        ),
        "event_sequence": event.sequence,
        "event_head": event.event_hash,
    }


def _dispatch_receipt_hash(
    attempt: TranslationAnalysisAttempt,
    event: TranslationAnalysisAttemptEvent,
) -> str:
    return private_witness(
        _dispatch_receipt_values(attempt, event),
        purpose=_WITNESS_PURPOSES["dispatch"],
    )


def _active_session(session: Any | None = None) -> Any:
    return db.session() if session is None else session


def _durable_source_plan(
    source_plan: SourcePlanRecord,
    *,
    session: Any | None = None,
    verified_plan: CanonicalSourcePlan | None = None,
) -> SourcePlanRecord:
    if not isinstance(source_plan, SourcePlanRecord) or not source_plan.id:
        raise BlockContractError("analysis-source-plan")
    active_session = _active_session(session)
    with active_session.no_autoflush:
        record = active_session.get(
            SourcePlanRecord, source_plan.id, populate_existing=True
        )
    if record is None:
        raise BlockContractError("analysis-source-plan")
    if verified_plan is None:
        rehydrate_source_plan(record)
    elif (
        verified_plan.project_id != record.project_id
        or verified_plan.plan_hash != record.plan_hash
        or verified_plan.snapshot.document_id != record.source_document_id
        or verified_plan.snapshot.content_hash != record.source_content_hash
    ):
        raise BlockContractError("analysis-source-plan")
    return cast(SourcePlanRecord, record)


def _durable_operation(
    operation: TranslationOperation,
    source_plan: SourcePlanRecord,
    *,
    session: Any | None = None,
) -> TranslationOperation:
    """Use only a clean, same-session running operation as a packet fence."""

    active_session = _active_session(session)
    state = inspect(operation) if isinstance(operation, TranslationOperation) else None
    if (
        state is None
        or not state.persistent
        or state.session is not active_session
        or state.deleted
        or state.modified
        or not operation.id
    ):
        raise BlockContractError("analysis-operation-durable")
    with active_session.no_autoflush:
        durable = active_session.get(
            TranslationOperation, operation.id, populate_existing=True
        )
    if durable is None or durable is not operation:
        raise BlockContractError("analysis-operation-durable")
    if (
        durable.state != "running"
        or durable.cancel_requested_at is not None
        or not isinstance(durable.queue_job_id, str)
        or not durable.queue_job_id.strip()
        or len(durable.queue_job_id.encode("utf-8")) > 128
        or durable.lease_expires_at is None
        or durable.lease_expires_at <= utc_now_db()
        or not isinstance(durable.version, int)
        or isinstance(durable.version, bool)
        or durable.version < 1
        or durable.project_id != source_plan.project_id
        or durable.owner_user_id != source_plan.project.user_id
        or durable.source_plan_id != source_plan.id
        or durable.source_plan_hash != source_plan.plan_hash
        or durable.source_content_hash != source_plan.source_content_hash
    ):
        raise BlockContractError("analysis-operation-fence")
    return cast(TranslationOperation, durable)


def _lineage(
    attempt_id: str | None,
    source_plan: SourcePlanRecord,
    operation: TranslationOperation,
    *,
    kind: str,
    session: Any | None = None,
    verified_plan: CanonicalSourcePlan | None = None,
    verification_context: AnalysisVerificationContext | None = None,
) -> tuple[str | None, str | None, TranslationAnalysisAttempt | None]:
    if attempt_id is None:
        return None, None, None
    if not isinstance(attempt_id, str) or not attempt_id:
        raise BlockContractError("analysis-lineage-id")
    active_session = _active_session(session)
    parent = (
        verification_context._attempts.get(attempt_id)
        if verification_context is not None
        else None
    )
    if parent is None:
        with active_session.no_autoflush:
            parent = active_session.get(
                TranslationAnalysisAttempt, attempt_id, populate_existing=True
            )
    if (
        parent is None
        or parent.source_plan_id != source_plan.id
        or parent.project_id != operation.project_id
        or parent.owner_user_id != operation.owner_user_id
        or parent.operation_id != operation.id
    ):
        raise BlockContractError("analysis-lineage-owner")
    rehydrate_analysis_attempt(
        parent,
        source_plan,
        session=active_session,
        verified_plan=verified_plan,
        verification_context=verification_context,
    )
    if parent.dispatch_state not in {
        "completed",
        "provider_failed",
        "provider_outcome_uncertain",
        "fence_failed",
        "cancelled",
        "failed",
    }:
        raise BlockContractError("analysis-lineage-terminal")
    return parent.id, parent.attempt_record_hash, parent


def persist_analysis_attempt(
    *,
    operation: TranslationOperation,
    source_plan: SourcePlanRecord,
    analysis_kind: str,
    contract_version: str,
    packet: Mapping[str, Any],
    model: Mapping[str, Any],
    profile: Mapping[str, Any],
    settings: Mapping[str, Any],
    budget: Mapping[str, Any],
    tool_declarations: object | None = None,
    retry_of_attempt_id: str | None = None,
    rebase_of_attempt_id: str | None = None,
    session: Any | None = None,
    verified_plan: CanonicalSourcePlan | None = None,
    verification_context: AnalysisVerificationContext | None = None,
) -> TranslationAnalysisAttempt:
    """Create exact request evidence.  Callers must commit before I/O."""

    if analysis_kind not in ANALYSIS_ATTEMPT_KINDS:
        raise BlockContractError("analysis-kind")
    active_session = _active_session(session)
    version = _version(contract_version, "analysis-contract-version")
    # Reject caller-side operation edits before any source-plan rehydration can
    # touch the ORM identity map or trigger an incidental flush.
    operation = _durable_operation(operation, source_plan, session=active_session)
    record = _durable_source_plan(
        source_plan,
        session=active_session,
        verified_plan=verified_plan,
    )
    packet_data = _packet(packet, version, analysis_kind=analysis_kind)
    model_data = _mapping(model, "analysis-model")
    profile_data = _mapping(profile, "analysis-profile")
    settings_data = _mapping(settings, "analysis-settings")
    budget_data = _mapping(budget, "analysis-budget")
    tools_data = _tools([] if tool_declarations is None else tool_declarations)
    retry_id, retry_hash, retry_parent = _lineage(
        retry_of_attempt_id,
        record,
        operation,
        kind="retry",
        session=active_session,
        verified_plan=verified_plan,
        verification_context=verification_context,
    )
    rebase_id, rebase_hash, rebase_parent = _lineage(
        rebase_of_attempt_id,
        record,
        operation,
        kind="rebase",
        session=active_session,
        verified_plan=verified_plan,
        verification_context=verification_context,
    )
    if retry_id is not None and rebase_id is not None:
        raise BlockContractError("analysis-lineage-ambiguous")
    material = {
        "packet": packet_data,
        "model": model_data,
        "profile": profile_data,
        "settings": settings_data,
        "budget": budget_data,
        "tools": tools_data,
    }
    hashes = {name: _witness(value, name) for name, value in material.items()}
    identity = _request_identity(
        analysis_kind=analysis_kind,
        contract_version=version,
        source_plan_hash=record.plan_hash,
        source_content_hash=record.source_content_hash,
        packet_hash=hashes["packet"],
        model_hash=hashes["model"],
        profile_hash=hashes["profile"],
        settings_hash=hashes["settings"],
        budget_hash=hashes["budget"],
        tools_hash=hashes["tools"],
    )
    request_hash = _digest(identity)

    def parent_material(parent: TranslationAnalysisAttempt) -> dict[str, Any]:
        verified = rehydrate_analysis_attempt(
            parent,
            record,
            session=active_session,
            verified_plan=verified_plan,
            verification_context=verification_context,
        )
        return {
            "packet": {
                "messages": verified["messages"],
                "input": verified["input"],
            },
            "model": verified["model"],
            "profile": verified["profile"],
            "settings": verified["settings"],
            "budget": verified["budget"],
            "tools": verified["tool_declarations"],
        }

    if retry_parent is not None:
        if (
            retry_parent.analysis_kind != analysis_kind
            or retry_parent.contract_version != version
            or parent_material(retry_parent) != material
        ):
            raise BlockContractError("analysis-retry-packet")
        # The parent has just been fully rehydrated, so these are verified
        # opaque witnesses.  Reusing them makes retry identity stable when
        # the installation key has rotated between attempts.
        hashes = {
            name: _hash(
                getattr(retry_parent, f"{name}_hash"),
                f"analysis-{name}-hash",
            )
            for name in material
        }
        request_hash = _hash(retry_parent.request_hash, "analysis-request-hash")
    if rebase_parent is not None and (
        rebase_parent.analysis_kind != analysis_kind
        or rebase_parent.contract_version != version
        or parent_material(rebase_parent) == material
    ):
        raise BlockContractError("analysis-rebase-identity")
    created_at = utc_now_db()
    attempt = cast(Any, TranslationAnalysisAttempt)(
        id=str(uuid.uuid4()),
        project_id=operation.project_id,
        owner_user_id=operation.owner_user_id,
        operation_id=operation.id,
        source_plan_id=record.id,
        analysis_kind=analysis_kind,
        contract_version=version,
        source_plan_hash=record.plan_hash,
        source_content_hash=record.source_content_hash,
        operation_version=operation.version,
        queue_job_id=operation.queue_job_id,
        operation_lease_expires_at=operation.lease_expires_at,
        lease_fence=_digest({"lease_expires_at": _time(operation.lease_expires_at)}),
        packet_json=packet_data,
        model_json=model_data,
        profile_json=profile_data,
        settings_json=settings_data,
        budget_json=budget_data,
        tool_declarations_json=tools_data,
        packet_hash=hashes["packet"],
        model_hash=hashes["model"],
        profile_hash=hashes["profile"],
        settings_hash=hashes["settings"],
        budget_hash=hashes["budget"],
        tools_hash=hashes["tools"],
        request_hash=request_hash,
        retry_of_attempt_id=retry_id,
        retry_of_attempt_hash=retry_hash,
        rebase_of_attempt_id=rebase_id,
        rebase_of_attempt_hash=rebase_hash,
        created_at=created_at,
        attempt_record_hash="0" * 64,
        dispatch_state="prepared",
        dispatch_version=1,
        dispatch_receipt_hash="0" * 64,
    )
    attempt.attempt_record_hash = _attempt_hash(_record_values(attempt))
    # Validate all encrypted payloads before the parent row is flushed.  This
    # makes malformed evidence atomic rather than leaving a usable orphan.
    rehydrate_analysis_attempt(
        attempt,
        record,
        session=active_session,
        allow_unpersisted=True,
        verified_plan=verified_plan,
        verification_context=verification_context,
        cache_result=False,
    )
    try:
        _ensure_transaction(active_session)
        with active_session.begin_nested():
            active_session.add(attempt)
            active_session.flush()
            append_analysis_attempt_event(
                attempt,
                "request_prepared",
                {"request_hash": request_hash},
                session=active_session,
            )
    except (BlockContractError, IntegrityError) as exc:
        raise BlockContractError("analysis-attempt-conflict") from exc
    return attempt


def append_analysis_attempt_event(
    attempt: TranslationAnalysisAttempt,
    event_type: str,
    payload: Mapping[str, Any],
    *,
    session: Any | None = None,
) -> TranslationAnalysisAttemptEvent:
    if event_type not in ANALYSIS_EVENT_TYPES:
        raise BlockContractError("analysis-event-type")
    active_session = _active_session(session)
    payload_data = _mapping(payload, "analysis-event-payload")
    try:
        _ensure_transaction(active_session)
        with active_session.begin_nested():
            existing = tuple(
                active_session.query(TranslationAnalysisAttemptEvent)
                .filter_by(attempt_id=attempt.id)
                .order_by(TranslationAnalysisAttemptEvent.sequence)
                .all()
            )
            if len(existing) >= _MAX_EVENT_CHAIN:
                raise BlockContractError("analysis-event-limit")
            event = cast(Any, TranslationAnalysisAttemptEvent)(
                id=str(uuid.uuid4()),
                attempt_id=attempt.id,
                sequence=len(existing) + 1,
                event_type=event_type,
                payload_json=payload_data,
                payload_hash=_witness(payload_data, "event_payload"),
                predecessor_event_hash=existing[-1].event_hash if existing else None,
                created_at=utc_now_db(),
                event_hash="0" * 64,
            )
            event.event_hash = _event_hash(event)
            active_session.add(event)
            active_session.flush()
            durable_attempt = active_session.get(
                TranslationAnalysisAttempt, attempt.id, populate_existing=True
            )
            if durable_attempt is None:
                raise BlockContractError("analysis-attempt-operation")
            durable_attempt.dispatch_receipt_hash = _dispatch_receipt_hash(
                durable_attempt, event
            )
            active_session.flush()
    except (IntegrityError, OperationalError) as exc:
        raise BlockContractError("analysis-event-conflict") from exc
    return event


def rehydrate_analysis_attempt_events(
    attempt: TranslationAnalysisAttempt,
    *,
    session: Any | None = None,
    verification_context: AnalysisVerificationContext | None = None,
    cache_result: bool = True,
) -> tuple[TranslationAnalysisAttemptEvent, ...]:
    active_session = _active_session(session)
    cache_key = _verification_key(attempt)
    if verification_context is not None and cache_result:
        cached = verification_context._verified_events.get(cache_key)
        if cached is not None:
            return cached
        events = verification_context._event_rows.get(attempt.id)
    else:
        events = None
    if events is None:
        with active_session.no_autoflush:
            events = tuple(
                active_session.query(TranslationAnalysisAttemptEvent)
                .filter_by(attempt_id=attempt.id)
                .order_by(TranslationAnalysisAttemptEvent.sequence)
                .all()
            )
    if not events or events[0].event_type != "request_prepared":
        raise BlockContractError("analysis-event-missing")
    previous: str | None = None
    for expected, event in enumerate(events, start=1):
        payload = _mapping(event.payload_json, "analysis-event-payload")
        if (
            event.sequence != expected
            or event.predecessor_event_hash != previous
            or event.event_type not in ANALYSIS_EVENT_TYPES
            or not _witness_matches(payload, event.payload_hash, "event_payload")
            or event.event_hash
            not in private_witness_candidates(
                {
                    "id": event.id,
                    "attempt_id": event.attempt_id,
                    "sequence": event.sequence,
                    "event_type": event.event_type,
                    "payload_hash": event.payload_hash,
                    "predecessor_event_hash": event.predecessor_event_hash,
                    "created_at": _time(event.created_at, "analysis-event-created-at"),
                },
                purpose=_WITNESS_PURPOSES["event"],
            )
        ):
            raise BlockContractError("analysis-event-chain")
        previous = event.event_hash
    if verification_context is not None and cache_result:
        verification_context._verified_events[cache_key] = events
    return events


def _verify_dispatch_receipt(
    attempt: TranslationAnalysisAttempt,
    events: tuple[TranslationAnalysisAttemptEvent, ...],
) -> None:
    if not events or attempt.dispatch_receipt_hash not in private_witness_candidates(
        _dispatch_receipt_values(attempt, events[-1]),
        purpose=_WITNESS_PURPOSES["dispatch"],
    ):
        raise BlockContractError("analysis-dispatch-receipt")


def _verify_dispatch_transcript(
    attempt: TranslationAnalysisAttempt,
    events: tuple[TranslationAnalysisAttemptEvent, ...],
) -> None:
    substantive = tuple(event.event_type for event in events)
    valid = {
        "prepared": {("request_prepared",)},
        "dispatched": {("request_prepared", "provider_dispatched")},
        "response_received": {
            ("request_prepared", "provider_dispatched", "provider_response")
        },
        "completed": {
            (
                "request_prepared",
                "provider_dispatched",
                "provider_response",
                "completed",
            )
        },
        "provider_outcome_uncertain": {
            (
                "request_prepared",
                "provider_dispatched",
                "provider_outcome_uncertain",
            )
        },
        "provider_failed": {
            ("request_prepared", "provider_dispatched", "provider_failure")
        },
        # A live-operation fence may be lost either before dispatch or while
        # the response waits for normalization; both cases are terminal and
        # distinguished by their transcript length.
        "fence_failed": {
            ("request_prepared", "post_response_fence_failed"),
            (
                "request_prepared",
                "provider_dispatched",
                "provider_response",
                "post_response_fence_failed",
            ),
        },
        "cancelled": {
            ("request_prepared", "cancelled"),
            (
                "request_prepared",
                "provider_dispatched",
                "provider_response",
                "cancelled",
            ),
        },
        "failed": {
            ("request_prepared", "provider_dispatched", "failed"),
            ("request_prepared", "provider_dispatched", "provider_response", "failed"),
        },
    }
    if (
        attempt.dispatch_state not in valid
        or substantive not in valid[attempt.dispatch_state]
    ):
        raise BlockContractError("analysis-dispatch-transcript")


def rehydrate_analysis_attempt(
    attempt: TranslationAnalysisAttempt,
    source_plan: SourcePlanRecord | None = None,
    *,
    lineage: frozenset[str] = frozenset(),
    allow_unpersisted: bool = False,
    session: Any | None = None,
    verified_plan: CanonicalSourcePlan | None = None,
    verification_context: AnalysisVerificationContext | None = None,
    cache_result: bool = True,
) -> dict[str, Any]:
    """Fail closed on a changed encrypted field, scope, witness, or lineage."""

    try:
        active_session = _active_session(session)
        if (
            not isinstance(attempt, TranslationAnalysisAttempt)
            or attempt.id in lineage
            or len(lineage) >= _MAX_LINEAGE
        ):
            raise BlockContractError("analysis-attempt-type")
        record = source_plan or attempt.source_plan
        if (
            not isinstance(record, SourcePlanRecord)
            or record.id != attempt.source_plan_id
        ):
            raise BlockContractError("analysis-attempt-plan")
        cache_key = _verification_key(attempt)
        if verification_context is not None and not allow_unpersisted:
            attempt_state = inspect(attempt)
            if attempt_state is None or attempt_state.modified or attempt_state.deleted:
                raise BlockContractError("analysis-attempt-dirty")
            if cache_result:
                cached = verification_context._attempt_data.get(cache_key)
                if cached is not None:
                    return dict(cached)
        plan = (
            _durable_source_plan(
                record,
                session=active_session,
                verified_plan=verified_plan,
            )
            if not allow_unpersisted
            else record
        )
        if (
            attempt.source_plan_hash != plan.plan_hash
            or attempt.source_content_hash != plan.source_content_hash
        ):
            raise BlockContractError("analysis-attempt-plan")
        with active_session.no_autoflush:
            operation = active_session.get(TranslationOperation, attempt.operation_id)
        if operation is None and not allow_unpersisted:
            raise BlockContractError("analysis-attempt-operation")
        if operation is not None and (
            operation.project_id != attempt.project_id
            or operation.owner_user_id != attempt.owner_user_id
            or operation.source_plan_id != plan.id
            or operation.source_plan_hash != attempt.source_plan_hash
            or operation.source_content_hash != attempt.source_content_hash
        ):
            raise BlockContractError("analysis-attempt-operation")
        if (
            attempt.project_id != plan.project_id
            or attempt.owner_user_id != plan.project.user_id
        ):
            raise BlockContractError("analysis-attempt-owner")
        if attempt.analysis_kind not in ANALYSIS_ATTEMPT_KINDS:
            raise BlockContractError("analysis-kind")
        _version(attempt.contract_version, "analysis-contract-version")
        if (
            not isinstance(attempt.operation_version, int)
            or isinstance(attempt.operation_version, bool)
            or attempt.operation_version < 1
            or not isinstance(attempt.queue_job_id, str)
            or not attempt.queue_job_id.strip()
            or attempt.queue_job_id != attempt.queue_job_id.strip()
            or len(attempt.queue_job_id.encode("utf-8")) > 128
            or not isinstance(attempt.operation_lease_expires_at, datetime)
            or attempt.lease_fence
            != _digest({"lease_expires_at": _time(attempt.operation_lease_expires_at)})
        ):
            raise BlockContractError("analysis-attempt-fence")
        packet = _packet(
            cast(Mapping[str, Any], attempt.packet_json),
            attempt.contract_version,
            analysis_kind=attempt.analysis_kind,
        )
        model = _mapping(attempt.model_json, "analysis-model")
        profile = _mapping(attempt.profile_json, "analysis-profile")
        settings = _mapping(attempt.settings_json, "analysis-settings")
        budget = _mapping(attempt.budget_json, "analysis-budget")
        tools = _tools(attempt.tool_declarations_json)
        hashes = {
            "packet": attempt.packet_hash,
            "model": attempt.model_hash,
            "profile": attempt.profile_hash,
            "settings": attempt.settings_hash,
            "budget": attempt.budget_hash,
            "tools": attempt.tools_hash,
        }
        if any(
            not _witness_matches(
                {
                    "packet": packet,
                    "model": model,
                    "profile": profile,
                    "settings": settings,
                    "budget": budget,
                    "tools": tools,
                }[name],
                _hash(hashes[name], f"analysis-{name}-hash"),
                name,
            )
            for name in hashes
        ):
            raise BlockContractError("analysis-attempt-evidence")
        identity = _request_identity(
            analysis_kind=attempt.analysis_kind,
            contract_version=attempt.contract_version,
            source_plan_hash=attempt.source_plan_hash,
            source_content_hash=attempt.source_content_hash,
            packet_hash=hashes["packet"],
            model_hash=hashes["model"],
            profile_hash=hashes["profile"],
            settings_hash=hashes["settings"],
            budget_hash=hashes["budget"],
            tools_hash=hashes["tools"],
        )
        if attempt.request_hash != _digest(
            identity
        ) or attempt.attempt_record_hash not in (
            private_witness_candidates(
                _record_values(attempt), purpose=_WITNESS_PURPOSES["record"]
            )
        ):
            raise BlockContractError("analysis-attempt-hash")
        if (
            attempt.retry_of_attempt_id is not None
            and attempt.rebase_of_attempt_id is not None
        ):
            raise BlockContractError("analysis-lineage-ambiguous")
        for kind, identifier, witness in (
            ("retry", attempt.retry_of_attempt_id, attempt.retry_of_attempt_hash),
            ("rebase", attempt.rebase_of_attempt_id, attempt.rebase_of_attempt_hash),
        ):
            if (identifier is None) != (witness is None):
                raise BlockContractError("analysis-lineage-witness")
            if identifier is not None:
                parent = (
                    verification_context._attempts.get(identifier)
                    if verification_context is not None
                    else None
                )
                if parent is None:
                    parent = active_session.get(TranslationAnalysisAttempt, identifier)
                if (
                    parent is None
                    or parent.source_plan_id != plan.id
                    or parent.project_id != attempt.project_id
                    or parent.owner_user_id != attempt.owner_user_id
                    or parent.operation_id != attempt.operation_id
                    or parent.attempt_record_hash != witness
                ):
                    raise BlockContractError(f"analysis-{kind}-witness")
                rehydrate_analysis_attempt(
                    parent,
                    plan,
                    lineage=lineage | {attempt.id},
                    session=active_session,
                    verified_plan=verified_plan,
                    verification_context=verification_context,
                )
        if not allow_unpersisted:
            events = rehydrate_analysis_attempt_events(
                attempt,
                session=active_session,
                verification_context=verification_context,
                cache_result=cache_result,
            )
            _verify_dispatch_receipt(attempt, events)
            _verify_dispatch_transcript(attempt, events)
        restored = {
            "messages": packet["messages"],
            "input": packet["input"],
            "model": model,
            "profile": profile,
            "settings": settings,
            "budget": budget,
            "tool_declarations": tools,
        }
        if verification_context is not None and cache_result:
            verification_context._attempt_data[cache_key] = dict(restored)
        return restored
    except BlockContractError:
        raise
    except Exception as exc:
        raise BlockContractError("analysis-attempt-corrupt") from exc


def rehydrate_completed_analysis_output(
    attempt: TranslationAnalysisAttempt,
    source_plan: SourcePlanRecord | None = None,
    *,
    session: Any | None = None,
    verified_plan: CanonicalSourcePlan | None = None,
    verification_context: AnalysisVerificationContext | None = None,
) -> Any:
    """Return the exact witnessed output of one completed provider call.

    Research orchestration must derive a turn from the provider evidence that
    actually completed, rather than accepting an independently supplied copy
    that could disagree with the attempt transcript.
    """

    active_session = _active_session(session)
    rehydrate_analysis_attempt(
        attempt,
        source_plan,
        session=active_session,
        verified_plan=verified_plan,
        verification_context=verification_context,
    )
    if attempt.dispatch_state != "completed":
        raise BlockContractError("analysis-output-not-completed")
    cache_key = _verification_key(attempt)
    if verification_context is not None and cache_key in verification_context._outputs:
        return verification_context._outputs[cache_key]
    events = rehydrate_analysis_attempt_events(
        attempt,
        session=active_session,
        verification_context=verification_context,
    )
    completed = tuple(event for event in events if event.event_type == "completed")
    if len(completed) != 1:
        raise BlockContractError("analysis-output-missing")
    payload = _mapping(completed[0].payload_json, "analysis-event-payload")
    if set(payload) != {"output"}:
        raise BlockContractError("analysis-output-payload")
    output = _canonical(payload["output"], "analysis-output")
    if not isinstance(output, (str, dict)):
        raise BlockContractError("analysis-output-payload")
    if verification_context is not None:
        verification_context._outputs[cache_key] = output
    return output


__all__ = [
    "AnalysisVerificationContext",
    "ANALYSIS_ATTEMPT_KINDS",
    "ANALYSIS_CONTRACT_VERSION",
    "ANALYSIS_CONTRACT_VERSIONS",
    "ANALYSIS_CONTRACT_VERSION_V1",
    "ANALYSIS_CONTRACT_VERSION_V2",
    "ANALYSIS_EVENT_TYPES",
    "append_analysis_attempt_event",
    "canonical_analysis_assistant_message",
    "persist_analysis_attempt",
    "rehydrate_analysis_attempt",
    "rehydrate_analysis_attempt_events",
    "rehydrate_completed_analysis_output",
    "validate_analysis_response_extension",
    "validate_research_attempt_packet",
]
