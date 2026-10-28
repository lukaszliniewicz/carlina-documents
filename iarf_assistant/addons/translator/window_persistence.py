"""Encrypted immutable evidence and transcript chains for Phase-3 windows."""

from __future__ import annotations

import json
import math
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from carlina.core.utc import utc_iso, utc_now_db
from iarf_assistant.extensions import db

from .alignment_persistence import rehydrate_alignment_revision
from .analysis_contract import private_witness, private_witness_candidates
from .block_contract import (
    BlockContractError,
    TranslationSourcePlan,
    canonical_json,
    sha256_text,
)
from .block_persistence import _ensure_database_transaction, rehydrate_source_plan
from .compact_markup import validate_located_source_block
from .context_adapters import (
    _SEGMENT_MEMORY_QUERY_EXCLUDED_ROLES,
    _segment_memory_query_text_is_eligible,
    safe_memory_projection,
)
from .context_baselines import rehydrate_context_baseline
from .embedding_projection import verify_pinned_embedding_reference
from .integrity_receipts import make_receipt, verify_receipt
from .models import (
    TranslationAlignmentRevision,
    TranslationContextBaseline,
    TranslationMemoryEntry,
    TranslationOperation,
    TranslationResearchRun,
    TranslationTargetAlignmentActive,
    TranslationTargetBlockActive,
    TranslationTargetBlockRevision,
    TranslationWindowAttempt,
    TranslationWindowAttemptEvent,
    TranslationWindowAttemptRevision,
)
from .models import TranslationSourcePlan as SourcePlanRecord
from .rehydration_cache import TranslationRehydrationCache
from .research_persistence import (
    ResearchVerificationContext,
    rehydrate_completed_research_conclusion,
)
from .target_contract import structural_fingerprint
from .target_persistence import verify_active_pair
from .translation_memory_persistence import rehydrate_translation_memory_entry
from .window_contract import (
    LOCATED_SOURCE_WRITABLE_CONTRACT_VERSION,
    ContextSlot,
    ProviderPacket,
    TranslationWindow,
    _writable_payload,
    context_dependency_manifest,
)

_MAX_EVENT_CHAIN = 10_000
_MAX_ATTEMPT_LINEAGE = 128
_MAX_PROVIDER_RESPONSE_BYTES = 2 * 1024 * 1024
_MAX_PROVIDER_RESPONSE_DEPTH = 16
_MAX_PROVIDER_RESPONSE_CONTAINER_ITEMS = 1_024
_MAX_PROVIDER_RESPONSE_KEY_BYTES = 256
_PROVIDER_RESPONSE_CONTRACT_VERSION = "translator-window-provider-response-v1"


@dataclass(frozen=True, slots=True)
class VerifiedWindowEvidence:
    """Invocation-local proof for immutable window dependencies.

    The value contains no ORM rows and is never persisted or shared between
    workers.  A conductor may create it only from a source plan and baseline
    slots it has already fully rehydrated during this invocation.  Dynamic
    operation, attempt, continuity, memory-pointer, and provider fences remain
    fresh database checks.
    """

    operation_id: str
    project_id: str
    owner_user_id: int
    source_plan_id: str
    source_plan_hash: str
    source_content_hash: str
    plan: TranslationSourcePlan
    research_slot_material: frozenset[str]

    @classmethod
    def from_verified_baseline(
        cls,
        *,
        operation: TranslationOperation,
        source_plan: SourcePlanRecord,
        plan: TranslationSourcePlan,
        baseline_slots: tuple[ContextSlot, ...],
    ) -> VerifiedWindowEvidence:
        if (
            operation.source_plan_id != source_plan.id
            or operation.source_plan_hash != plan.plan_hash
            or operation.source_content_hash != plan.snapshot.content_hash
            or operation.project_id != source_plan.project_id
            or operation.owner_user_id != source_plan.project.user_id
            or source_plan.plan_hash != plan.plan_hash
            or source_plan.source_content_hash != plan.snapshot.content_hash
            or plan.project_id != source_plan.project_id
        ):
            raise BlockContractError("window-verified-evidence")
        research = frozenset(
            canonical_json(slot.to_dict())
            for slot in baseline_slots
            if slot.slot_type == "research_evidence" and slot.dependency
        )
        return cls(
            operation_id=operation.id,
            project_id=operation.project_id,
            owner_user_id=operation.owner_user_id,
            source_plan_id=source_plan.id,
            source_plan_hash=plan.plan_hash,
            source_content_hash=plan.snapshot.content_hash,
            plan=plan,
            research_slot_material=research,
        )

    def plan_for(
        self,
        record: SourcePlanRecord,
        *,
        operation_id: str,
        project_id: str,
        owner_user_id: int,
    ) -> TranslationSourcePlan:
        if (
            self.operation_id != operation_id
            or self.project_id != project_id
            or self.owner_user_id != owner_user_id
            or self.source_plan_id != record.id
            or self.source_plan_hash != record.plan_hash
            or self.source_content_hash != record.source_content_hash
            or self.plan.project_id != record.project_id
            or self.plan.plan_hash != record.plan_hash
            or self.plan.snapshot.content_hash != record.source_content_hash
        ):
            raise BlockContractError("window-verified-evidence")
        return self.plan

    def verifies_research_slot(self, slot: ContextSlot) -> bool:
        return canonical_json(slot.to_dict()) in self.research_slot_material


def _hash(value: object, code: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise BlockContractError(code)
    return value


def _time(value: object) -> str:
    if not isinstance(value, datetime):
        raise BlockContractError("window-created-at")
    return utc_iso(value, timespec="microseconds")


def window_lease_fence(value: object) -> str:
    """Stable receipt for the immutable lease expiry captured by an attempt."""

    return sha256_text(canonical_json({"lease_expires_at": _time(value)}))


def _mapping(value: object, code: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise BlockContractError(code)
    return cast(Mapping[str, Any], value)


def _validate_provider_response_value(value: object, *, depth: int = 0) -> None:
    """Accept only bounded JSON material for the encrypted provider transcript."""

    if depth > _MAX_PROVIDER_RESPONSE_DEPTH:
        raise BlockContractError("window-response-depth")
    if value is None or isinstance(value, (bool, int, str)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise BlockContractError("window-response-material")
        return
    if isinstance(value, list):
        if len(value) > _MAX_PROVIDER_RESPONSE_CONTAINER_ITEMS:
            raise BlockContractError("window-response-items")
        for item in value:
            _validate_provider_response_value(item, depth=depth + 1)
        return
    if isinstance(value, Mapping):
        if len(value) > _MAX_PROVIDER_RESPONSE_CONTAINER_ITEMS:
            raise BlockContractError("window-response-items")
        for key, item in value.items():
            if (
                not isinstance(key, str)
                or not key
                or len(key.encode("utf-8")) > _MAX_PROVIDER_RESPONSE_KEY_BYTES
            ):
                raise BlockContractError("window-response-key")
            _validate_provider_response_value(item, depth=depth + 1)
        return
    raise BlockContractError("window-response-material")


def provider_response_event_payload(
    response: Mapping[str, Any] | str,
    *,
    transport: str,
) -> dict[str, object]:
    """Return bounded, encrypted-only material required for deterministic resume.

    The caller writes this payload only through ``append_attempt_event``; that
    column is encrypted.  Nothing from a provider response is copied to a
    plaintext status/error column.
    """

    if not isinstance(transport, str) or not transport:
        raise BlockContractError("window-response-transport")
    if isinstance(response, str):
        raw = response.encode("utf-8")
        if len(raw) > _MAX_PROVIDER_RESPONSE_BYTES:
            raise BlockContractError("window-response-size")
        # A strict-text provider may return malformed JSON.  Retaining that
        # bounded exact string lets normalisation fail durably and makes the
        # failed result inspectable.  Valid JSON is additionally bounded
        # structurally here; semantic shape remains normalisation's job.
        try:
            decoded = json.loads(response)
        except TypeError, ValueError:
            decoded = None
        else:
            _validate_provider_response_value(decoded)
        retained: Mapping[str, Any] | str = response
    elif isinstance(response, Mapping):
        _validate_provider_response_value(response)
        try:
            encoded = json.dumps(
                response,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise BlockContractError("window-response-material") from exc
        if len(encoded) > _MAX_PROVIDER_RESPONSE_BYTES:
            raise BlockContractError("window-response-size")
        # Round-tripping makes the retained value a plain JSON DTO rather than
        # an arbitrary Mapping implementation supplied by a provider adapter.
        retained = cast(Mapping[str, Any], json.loads(encoded))
    else:
        raise BlockContractError("window-response-material")
    return {
        "contract_version": _PROVIDER_RESPONSE_CONTRACT_VERSION,
        "response": retained,
        "response_hash": private_witness(
            retained, purpose="translator-window-provider-response-v1"
        ),
        "transport": transport,
    }


def rehydrate_window_provider_response(
    attempt: TranslationWindowAttempt,
) -> Mapping[str, Any] | str:
    """Load and verify the one exact response retained in an attempt transcript."""

    events = rehydrate_attempt_events(attempt)
    response_events = [
        event for event in events if event.event_type == "provider_response"
    ]
    if len(response_events) != 1:
        raise BlockContractError("window-response-missing")
    payload = _mapping(response_events[0].payload_json, "window-response-payload")
    if set(payload) != {"contract_version", "response", "response_hash", "transport"}:
        raise BlockContractError("window-response-payload")
    if payload["contract_version"] != _PROVIDER_RESPONSE_CONTRACT_VERSION:
        raise BlockContractError("window-response-contract")
    response = payload["response"]
    transport = payload["transport"]
    if not isinstance(transport, str) or not transport:
        raise BlockContractError("window-response-transport")
    expected = provider_response_event_payload(
        cast(Mapping[str, Any] | str, response), transport=transport
    )
    if payload["response_hash"] != expected["response_hash"] or transport != _mapping(
        attempt.packet_json, "window-packet"
    ).get("transport"):
        raise BlockContractError("window-response-hash")
    return cast(Mapping[str, Any] | str, response)


_ATTEMPT_RECEIPT_PURPOSE = "translator-window-attempt-receipt-v1"
_EVENT_RECEIPT_PURPOSE = "translator-window-event-receipt-v1"
_STATE_HEAD_RECEIPT_PURPOSE = "translator-window-state-head-receipt-v1"
_WITNESS_PURPOSES = {
    "packet": "translator-window-packet-v1",
    "prompt": "translator-window-prompt-v1",
    "settings": "translator-window-settings-v1",
    "context": "translator-window-context-v1",
    "context_manifest": "translator-window-context-manifest-v1",
    "model_profile": "translator-window-model-profile-v1",
    "predecessor": "translator-window-predecessor-v1",
    "event_payload": "translator-window-event-payload-v1",
}


def _attempt_payload(**values: object) -> dict[str, object]:
    return {"receipt_version": 1, **values}


def _attempt_hash(**values: object) -> str:
    """Make the retained-key receipt for immutable request evidence."""

    return make_receipt(
        purpose=_ATTEMPT_RECEIPT_PURPOSE, values=_attempt_payload(**values)
    )


def _attempt_valid(stored: object, **values: object) -> bool:
    try:
        verify_receipt(
            stored=stored,
            purpose=_ATTEMPT_RECEIPT_PURPOSE,
            values=_attempt_payload(**values),
            code="window-attempt-hash",
        )
    except BlockContractError:
        return False
    return True


def _witness(value: object, component: str) -> str:
    try:
        purpose = _WITNESS_PURPOSES[component]
    except KeyError as exc:
        raise BlockContractError("window-witness-purpose") from exc
    return private_witness(value, purpose=purpose)


def _witness_matches(value: object, stored: object, component: str) -> bool:
    if not isinstance(stored, str):
        return False
    try:
        purpose = _WITNESS_PURPOSES[component]
    except KeyError as exc:
        raise BlockContractError("window-witness-purpose") from exc
    return stored in private_witness_candidates(value, purpose=purpose)


def _stored_window_scope(attempt: TranslationWindowAttempt) -> dict[str, object]:
    return {
        "project_id": attempt.project_id,
        "owner_user_id": attempt.owner_user_id,
        "operation_id": attempt.operation_id,
        "source_plan_id": attempt.source_plan_id,
        "source_plan_hash": attempt.source_plan_hash,
        "window_order": attempt.window_order,
        "block_ids": attempt.block_ids_json,
        "block_fingerprints": attempt.block_fingerprints_json,
    }


def _stored_window_material(attempt: TranslationWindowAttempt) -> dict[str, object]:
    return {
        "packet": attempt.packet_json,
        "settings": attempt.settings_json,
        "predecessor": attempt.predecessor_json,
        "model_profile": attempt.model_profile_json,
        "admission": attempt.admission_json,
        "context_manifest": attempt.context_manifest_json,
    }


def _attempt_receipt_values(attempt: TranslationWindowAttempt) -> dict[str, object]:
    """The complete immutable, decrypted request evidence sealed by a receipt."""

    operation = attempt.operation
    if operation is None:
        raise BlockContractError("window-attempt-operation")
    return {
        "id": attempt.id,
        "project_id": attempt.project_id,
        "owner_user_id": attempt.owner_user_id,
        "operation_id": attempt.operation_id,
        "operation_request_fingerprint": operation.request_fingerprint,
        "operation_source_content_hash": operation.source_content_hash,
        "operation_source_plan_id": operation.source_plan_id,
        "operation_source_plan_hash": operation.source_plan_hash,
        "source_plan_id": attempt.source_plan_id,
        "source_plan_hash": attempt.source_plan_hash,
        "source_content_hash": attempt.source_content_hash,
        "operation_version": attempt.operation_version,
        "queue_job_id": attempt.queue_job_id,
        "operation_lease_expires_at": _time(attempt.operation_lease_expires_at),
        "lease_fence": attempt.lease_fence,
        "window_order": attempt.window_order,
        "packet_json": attempt.packet_json,
        "block_ids_json": attempt.block_ids_json,
        "block_fingerprints_json": attempt.block_fingerprints_json,
        "predecessor_json": attempt.predecessor_json,
        "model_profile_json": attempt.model_profile_json,
        "admission_json": attempt.admission_json,
        "context_manifest_json": attempt.context_manifest_json,
        "settings_json": attempt.settings_json,
        "packet_hash": attempt.packet_hash,
        "prompt_hash": attempt.prompt_hash,
        "settings_hash": attempt.settings_hash,
        "context_hash": attempt.context_hash,
        "context_manifest_hash": attempt.context_manifest_hash,
        "model_profile_hash": attempt.model_profile_hash,
        "predecessor_hash": attempt.predecessor_hash,
        "retry_of_attempt_id": attempt.retry_of_attempt_id,
        "retry_of_attempt_hash": attempt.retry_of_attempt_hash,
        "rebase_of_attempt_id": attempt.rebase_of_attempt_id,
        "rebase_of_attempt_hash": attempt.rebase_of_attempt_hash,
        "created_at": _time(attempt.created_at),
    }


def _ordered_revision_links(
    attempt: TranslationWindowAttempt, session: Any
) -> list[dict[str, object]]:
    links = list(
        session.query(TranslationWindowAttemptRevision)
        .filter_by(attempt_id=attempt.id)
        .order_by(TranslationWindowAttemptRevision.block_id)
        .all()
    )
    return [
        {
            "order": index,
            "id": link.id,
            "attempt_id": link.attempt_id,
            "source_plan_id": link.source_plan_id,
            "block_id": link.block_id,
            "revision_id": link.revision_id,
            "revision_record_hash": link.revision_record_hash,
            "candidate_hash": link.candidate_hash,
            "created_at": _time(link.created_at),
        }
        for index, link in enumerate(links, start=1)
    ]


def _state_head_payload(
    attempt: TranslationWindowAttempt, session: Any
) -> dict[str, object]:
    events = list(
        session.query(TranslationWindowAttemptEvent)
        .filter_by(attempt_id=attempt.id)
        .order_by(TranslationWindowAttemptEvent.sequence)
        .all()
    )
    head = events[-1].event_hash if events else None
    return {
        "receipt_version": 1,
        "attempt_id": attempt.id,
        "attempt_receipt": attempt.attempt_record_hash,
        "dispatch_state": attempt.dispatch_state,
        "dispatch_version": attempt.dispatch_version,
        "dispatch_claimed_at": (
            None
            if attempt.dispatch_claimed_at is None
            else _time(attempt.dispatch_claimed_at)
        ),
        "dispatch_finished_at": (
            None
            if attempt.dispatch_finished_at is None
            else _time(attempt.dispatch_finished_at)
        ),
        "event_sequence": len(events),
        "event_head_receipt": head,
        "revision_links": _ordered_revision_links(attempt, session),
    }


def seal_attempt_state_head(
    attempt: TranslationWindowAttempt, *, session: Any | None = None
) -> None:
    """Update the one mutable receipt after an atomic transcript/state change."""

    active_session = session or db.session
    active_session.flush()
    attempt.state_head_receipt = make_receipt(
        purpose=_STATE_HEAD_RECEIPT_PURPOSE,
        values=_state_head_payload(attempt, active_session),
    )
    active_session.flush()


def verify_attempt_state_head(
    attempt: TranslationWindowAttempt, *, session: Any | None = None
) -> None:
    active_session = session or db.session
    verify_receipt(
        stored=attempt.state_head_receipt,
        purpose=_STATE_HEAD_RECEIPT_PURPOSE,
        values=_state_head_payload(attempt, active_session),
        code="window-state-head",
    )


def _event_payload(
    *,
    event_id: object,
    attempt_id: object,
    sequence: object,
    event_type: object,
    payload_json: object,
    payload_hash: object,
    predecessor_event_hash: object,
    created_at: object,
) -> dict[str, object]:
    return {
        "receipt_version": 1,
        "id": event_id,
        "attempt_id": attempt_id,
        "sequence": sequence,
        "event_type": event_type,
        "payload_json": payload_json,
        "payload_hash": payload_hash,
        "predecessor_event_hash": predecessor_event_hash,
        "created_at": created_at,
    }


def _event_hash(**values: object) -> str:
    return make_receipt(purpose=_EVENT_RECEIPT_PURPOSE, values=_event_payload(**values))


def _event_valid(event: TranslationWindowAttemptEvent, previous: str | None) -> bool:
    try:
        verify_receipt(
            stored=event.event_hash,
            purpose=_EVENT_RECEIPT_PURPOSE,
            values=_event_payload(
                event_id=event.id,
                attempt_id=event.attempt_id,
                sequence=event.sequence,
                event_type=event.event_type,
                payload_json=event.payload_json,
                payload_hash=event.payload_hash,
                predecessor_event_hash=previous,
                created_at=_time(event.created_at),
            ),
            code="window-event-hash",
        )
    except BlockContractError:
        return False
    return True


def _record_plan(record: SourcePlanRecord) -> TranslationSourcePlan:
    state = inspect(record) if isinstance(record, SourcePlanRecord) else None
    if state is None or not state.persistent:
        raise BlockContractError("window-source-plan")
    return rehydrate_source_plan(record)


def predecessor_snapshot(
    record: SourcePlanRecord, window: TranslationWindow
) -> dict[str, dict[str, str | int | None]]:
    """Capture the active predecessor identity before provider I/O."""

    result: dict[str, dict[str, str | int | None]] = {}
    for block_id in window.block_ids:
        pointer = db.session.get(
            TranslationTargetBlockActive,
            {"source_plan_id": record.id, "block_id": block_id},
        )
        if pointer is None:
            result[block_id] = {
                "revision_id": None,
                "revision_record_hash": None,
                "version": None,
            }
        else:
            revision = pointer.revision
            if revision is None:
                raise BlockContractError("window-predecessor-missing")
            _verify_active_alignment(record, pointer, "window-predecessor-alignment")
            result[block_id] = {
                "revision_id": revision.id,
                "revision_record_hash": _hash(
                    revision.revision_record_hash, "window-predecessor-hash"
                ),
                "version": pointer.version,
            }
    return result


def _verify_active_alignment(
    record: SourcePlanRecord,
    pointer: TranslationTargetBlockActive,
    code: str,
    *,
    session: Any | None = None,
    verified_plan: TranslationSourcePlan | None = None,
    verification_cache: TranslationRehydrationCache | None = None,
) -> None:
    """Require the Phase-4 active witness for continuity material."""

    active_session = session or db.session
    alignment_pointer = active_session.get(
        TranslationTargetAlignmentActive,
        {"source_plan_id": record.id, "block_id": pointer.block_id},
    )
    if (
        alignment_pointer is None
        or alignment_pointer.version != pointer.version
        or alignment_pointer.revision_id != pointer.revision_id
    ):
        raise BlockContractError(code)
    try:
        verify_active_pair(pointer, alignment_pointer, session=active_session)
    except BlockContractError as exc:
        raise BlockContractError(code) from exc
    alignment = active_session.get(
        TranslationAlignmentRevision, alignment_pointer.alignment_revision_id
    )
    if (
        alignment is None
        or alignment.source_plan_id != record.id
        or alignment.block_id != pointer.block_id
        or alignment.revision_id != pointer.revision_id
    ):
        raise BlockContractError(code)
    rehydrate_alignment_revision(
        alignment,
        record,
        session=active_session,
        verified_plan=verified_plan,
        verification_cache=verification_cache,
    )


def _lineage(
    attempt_id: str | None,
    record: SourcePlanRecord,
    *,
    kind: str,
    verified_evidence: VerifiedWindowEvidence | None = None,
) -> tuple[str | None, str | None]:
    if attempt_id is None:
        return None, None
    if not isinstance(attempt_id, str) or not attempt_id:
        raise BlockContractError("window-lineage-id")
    prior = db.session.get(TranslationWindowAttempt, attempt_id)
    if prior is None or prior.source_plan_id != record.id:
        raise BlockContractError("window-lineage-owner")
    rehydrate_window_attempt(prior, record, verified_evidence=verified_evidence)
    return prior.id, _hash(prior.attempt_record_hash, f"window-{kind}-hash")


def persist_window_attempt(
    *,
    operation: TranslationOperation,
    source_plan: SourcePlanRecord,
    window: TranslationWindow,
    packet: ProviderPacket,
    settings: Mapping[str, Any],
    model_profile: Mapping[str, Any],
    admission: Mapping[str, Any],
    retry_of_attempt_id: str | None = None,
    rebase_of_attempt_id: str | None = None,
    verified_evidence: VerifiedWindowEvidence | None = None,
) -> TranslationWindowAttempt:
    """Append one exact provider packet; caller commits it before network I/O."""

    plan = (
        verified_evidence.plan_for(
            source_plan,
            operation_id=operation.id,
            project_id=operation.project_id,
            owner_user_id=operation.owner_user_id,
        )
        if verified_evidence is not None
        else _record_plan(source_plan)
    )
    if (
        operation.project_id != source_plan.project_id
        or operation.owner_user_id != source_plan.project.user_id
    ):
        raise BlockContractError("window-operation-owner")
    if (
        operation.source_content_hash != plan.snapshot.content_hash
        or operation.source_plan_id != source_plan.id
        or operation.source_plan_hash != plan.plan_hash
    ):
        raise BlockContractError("window-operation-plan")
    if (
        not isinstance(operation.version, int)
        or isinstance(operation.version, bool)
        or operation.version < 1
        or not isinstance(operation.queue_job_id, str)
        or not operation.queue_job_id.strip()
        or operation.queue_job_id != operation.queue_job_id.strip()
        or len(operation.queue_job_id.encode("utf-8")) > 128
        or operation.lease_expires_at is None
    ):
        raise BlockContractError("window-operation-fence")
    blocks = {block.id: block for block in plan.blocks}
    if tuple(blocks) and any(block_id not in blocks for block_id in window.block_ids):
        raise BlockContractError("window-block-owner")
    expected_fingerprints = tuple(
        structural_fingerprint(plan, blocks[block_id]) for block_id in window.block_ids
    )
    if expected_fingerprints != window.block_fingerprints:
        raise BlockContractError("window-fingerprint")
    retry_id, retry_hash = _lineage(
        retry_of_attempt_id,
        source_plan,
        kind="retry",
        verified_evidence=verified_evidence,
    )
    rebase_id, rebase_hash = _lineage(
        rebase_of_attempt_id,
        source_plan,
        kind="rebase",
        verified_evidence=verified_evidence,
    )
    if retry_id is not None and rebase_id is not None:
        raise BlockContractError("window-lineage-ambiguous")
    predecessors = predecessor_snapshot(source_plan, window)
    packet_data = packet.to_dict()
    settings_data = dict(settings)
    model_data = dict(model_profile)
    admission_data = dict(admission)
    if (
        admission_data.get("admitted") is not True
        or admission_data.get("requested_output_tokens")
        != packet.requested_output_tokens
    ):
        raise BlockContractError("window-admission")
    context_manifest = list(context_dependency_manifest(packet.slots))
    prompt_data = {
        "messages": packet_data["messages"],
        "response_format": packet_data["response_format"],
        "transport": packet.transport,
    }
    scope = {
        "project_id": operation.project_id,
        "owner_user_id": operation.owner_user_id,
        "operation_id": operation.id,
        "source_plan_id": source_plan.id,
        "source_plan_hash": plan.plan_hash,
        "window_order": window.order,
        "block_ids": list(window.block_ids),
        "block_fingerprints": list(window.block_fingerprints),
    }
    material = {
        "packet": packet_data,
        "settings": settings_data,
        "predecessor": predecessors,
        "model_profile": model_data,
        "admission": admission_data,
        "context_manifest": context_manifest,
    }
    witnesses = {
        "packet_hash": _witness(packet_data, "packet"),
        "prompt_hash": _witness(prompt_data, "prompt"),
        "settings_hash": _witness(settings_data, "settings"),
        "context_hash": _witness(packet_data["slots"], "context"),
        "context_manifest_hash": _witness(context_manifest, "context_manifest"),
        "model_profile_hash": _witness(model_data, "model_profile"),
        "predecessor_hash": _witness(predecessors, "predecessor"),
    }
    for kind, parent_id in (("retry", retry_id), ("rebase", rebase_id)):
        if parent_id is None:
            continue
        parent = db.session.get(TranslationWindowAttempt, parent_id)
        if parent is None:
            raise BlockContractError("window-lineage-owner")
        parent_scope = _stored_window_scope(parent)
        parent_material = _stored_window_material(parent)
        if kind == "retry" and (parent_scope != scope or parent_material != material):
            raise BlockContractError("window-retry-packet")
        if kind == "rebase" and (parent_scope != scope or parent_material == material):
            raise BlockContractError("window-rebase-identity")
        if kind == "retry":
            # Reuse verified opaque witnesses so retry identity remains stable
            # when installation keys rotate between attempts.
            witnesses = {
                field: _hash(getattr(parent, field), f"window-{field}")
                for field in witnesses
            }
    created_at = utc_now_db()
    attempt_id = str(uuid.uuid4())
    attempt = cast(Any, TranslationWindowAttempt)(
        id=attempt_id,
        project_id=operation.project_id,
        owner_user_id=operation.owner_user_id,
        operation_id=operation.id,
        source_plan_id=source_plan.id,
        source_plan_hash=plan.plan_hash,
        source_content_hash=plan.snapshot.content_hash,
        operation_version=operation.version,
        queue_job_id=operation.queue_job_id,
        operation_lease_expires_at=operation.lease_expires_at,
        lease_fence=window_lease_fence(operation.lease_expires_at),
        window_order=window.order,
        packet_json=packet_data,
        block_ids_json=list(window.block_ids),
        settings_json=settings_data,
        block_fingerprints_json=list(window.block_fingerprints),
        predecessor_json=predecessors,
        model_profile_json=model_data,
        admission_json=admission_data,
        packet_hash=witnesses["packet_hash"],
        context_manifest_json=context_manifest,
        prompt_hash=witnesses["prompt_hash"],
        settings_hash=witnesses["settings_hash"],
        context_hash=witnesses["context_hash"],
        context_manifest_hash=witnesses["context_manifest_hash"],
        model_profile_hash=witnesses["model_profile_hash"],
        predecessor_hash=witnesses["predecessor_hash"],
        retry_of_attempt_id=retry_id,
        retry_of_attempt_hash=retry_hash,
        rebase_of_attempt_id=rebase_id,
        rebase_of_attempt_hash=rebase_hash,
        attempt_record_hash="0" * 64,
        state_head_receipt="0" * 64,
        created_at=created_at,
    )
    db.session.add(attempt)
    db.session.flush()
    attempt.attempt_record_hash = _attempt_hash(**_attempt_receipt_values(attempt))
    db.session.flush()
    request_prepared_payload: dict[str, Any] = {
        "packet_hash": witnesses["packet_hash"],
        "block_ids": list(window.block_ids),
    }
    if retry_id is not None:
        request_prepared_payload["passive_retry_dispatch_version"] = 1
    append_attempt_event(
        attempt,
        "request_prepared",
        request_prepared_payload,
    )
    return attempt


def append_attempt_event(
    attempt: TranslationWindowAttempt,
    event_type: str,
    payload: Mapping[str, Any],
    *,
    session: Any | None = None,
) -> TranslationWindowAttemptEvent:
    if not isinstance(event_type, str) or not event_type or len(event_type) > 64:
        raise BlockContractError("window-event-type")
    active_session = session or db.session
    existing = (
        active_session.query(TranslationWindowAttemptEvent)
        .filter_by(attempt_id=attempt.id)
        .order_by(TranslationWindowAttemptEvent.sequence)
        .all()
    )
    if len(existing) >= _MAX_EVENT_CHAIN:
        raise BlockContractError("window-event-limit")
    sequence = len(existing) + 1
    prior = existing[-1].event_hash if existing else None
    created_at = utc_now_db()
    event_id = str(uuid.uuid4())
    payload_data = dict(payload)
    payload_hash = _witness(payload_data, "event_payload")
    event = cast(Any, TranslationWindowAttemptEvent)(
        id=event_id,
        attempt_id=attempt.id,
        sequence=sequence,
        event_type=event_type,
        payload_json=payload_data,
        payload_hash=payload_hash,
        predecessor_event_hash=prior,
        event_hash=_event_hash(
            event_id=event_id,
            attempt_id=attempt.id,
            sequence=sequence,
            event_type=event_type,
            payload_json=payload_data,
            payload_hash=payload_hash,
            predecessor_event_hash=prior,
            created_at=_time(created_at),
        ),
        created_at=created_at,
    )
    _ensure_database_transaction(active_session)
    try:
        with active_session.begin_nested():
            active_session.add(event)
            active_session.flush()
            seal_attempt_state_head(attempt, session=active_session)
    except IntegrityError as exc:
        raise BlockContractError("window-event-conflict") from exc
    return event


def rehydrate_window_attempt(
    attempt: TranslationWindowAttempt,
    source_plan: SourcePlanRecord | None = None,
    *,
    verified_plan: TranslationSourcePlan | None = None,
    verified_evidence: VerifiedWindowEvidence | None = None,
    research_verification_context: ResearchVerificationContext | None = None,
) -> ProviderPacket:
    return _rehydrate_window_attempt(
        attempt,
        source_plan,
        lineage=frozenset(),
        verified_plan=verified_plan,
        verified_evidence=verified_evidence,
        research_verification_context=research_verification_context,
    )


def _rehydrate_window_attempt(
    attempt: TranslationWindowAttempt,
    source_plan: SourcePlanRecord | None,
    *,
    lineage: frozenset[str],
    verified_plan: TranslationSourcePlan | None,
    verified_evidence: VerifiedWindowEvidence | None,
    research_verification_context: ResearchVerificationContext | None,
) -> ProviderPacket:
    try:
        if not isinstance(attempt, TranslationWindowAttempt):
            raise BlockContractError("window-attempt-type")
        if attempt.id in lineage or len(lineage) >= _MAX_ATTEMPT_LINEAGE:
            raise BlockContractError("window-lineage-cycle")
        record = cast(SourcePlanRecord, source_plan or attempt.source_plan)
        if record is None or record.id != attempt.source_plan_id:
            raise BlockContractError("window-attempt-plan")
        if verified_plan is not None and verified_evidence is not None:
            evidence_plan = verified_evidence.plan_for(
                record,
                operation_id=attempt.operation_id,
                project_id=attempt.project_id,
                owner_user_id=attempt.owner_user_id,
            )
            if verified_plan is not evidence_plan:
                raise BlockContractError("window-verified-evidence")
        plan = (
            verified_evidence.plan_for(
                record,
                operation_id=attempt.operation_id,
                project_id=attempt.project_id,
                owner_user_id=attempt.owner_user_id,
            )
            if verified_evidence is not None
            else verified_plan or _record_plan(record)
        )
        if (
            plan.project_id != record.project_id
            or plan.plan_hash != record.plan_hash
            or plan.snapshot.document_id != record.source_document_id
        ):
            raise BlockContractError("window-source-plan-pin")
        if (
            attempt.project_id != record.project_id
            or attempt.owner_user_id != record.project.user_id
        ):
            raise BlockContractError("window-attempt-owner")
        operation = db.session.get(TranslationOperation, attempt.operation_id)
        if (
            operation is None
            or operation.project_id != record.project_id
            or operation.owner_user_id != record.project.user_id
            or attempt.source_content_hash != plan.snapshot.content_hash
            or operation.source_content_hash != attempt.source_content_hash
            or operation.source_plan_id != record.id
            or operation.source_plan_hash != plan.plan_hash
        ):
            raise BlockContractError("window-attempt-operation")
        if (
            not isinstance(attempt.operation_version, int)
            or isinstance(attempt.operation_version, bool)
            or attempt.operation_version < 1
            or not isinstance(attempt.queue_job_id, str)
            or not attempt.queue_job_id.strip()
            or attempt.queue_job_id != attempt.queue_job_id.strip()
            or len(attempt.queue_job_id.encode("utf-8")) > 128
            or attempt.operation_lease_expires_at is None
            or attempt.lease_fence
            != window_lease_fence(attempt.operation_lease_expires_at)
        ):
            raise BlockContractError("window-attempt-fence")
        if not isinstance(attempt.block_ids_json, list) or not isinstance(
            attempt.block_fingerprints_json, list
        ):
            raise BlockContractError("window-block-ids")
        block_ids = tuple(attempt.block_ids_json)
        fingerprints = tuple(attempt.block_fingerprints_json)
        if (
            not block_ids
            or len(block_ids) != len(fingerprints)
            or len(set(block_ids)) != len(block_ids)
            or not all(isinstance(item, str) for item in block_ids)
            or not all(
                isinstance(item, str) and len(item) == 64 for item in fingerprints
            )
        ):
            raise BlockContractError("window-block-ids")
        blocks = {block.id: block for block in plan.blocks}
        if any(block_id not in blocks for block_id in block_ids):
            raise BlockContractError("window-block-owner")
        from .note_relations import window_blocks_are_linked

        if not window_blocks_are_linked(plan, block_ids):
            raise BlockContractError("window-block-adjacency")
        if (
            tuple(
                structural_fingerprint(plan, blocks[block_id]) for block_id in block_ids
            )
            != fingerprints
        ):
            raise BlockContractError("window-fingerprint")
        packet_data = _mapping(attempt.packet_json, "window-packet")
        if set(packet_data) != {
            "contract_version",
            "messages",
            "response_format",
            "transport",
            "requested_output_tokens",
            "slots",
        }:
            raise BlockContractError("window-packet")
        messages = packet_data.get("messages")
        slots = packet_data.get("slots")
        if not isinstance(messages, list) or not isinstance(slots, list):
            raise BlockContractError("window-packet")
        if tuple(
            message.get("role") for message in messages if isinstance(message, Mapping)
        ) != ("system", "user") or any(
            not isinstance(message, Mapping)
            or set(message) != {"role", "content"}
            or not all(isinstance(value, str) for value in message.values())
            for message in messages
        ):
            raise BlockContractError("window-packet-messages")
        if not _witness_matches(packet_data, attempt.packet_hash, "packet"):
            raise BlockContractError("window-packet-hash")
        predecessor_data = _mapping(attempt.predecessor_json, "window-predecessor")
        if set(predecessor_data) != set(block_ids):
            raise BlockContractError("window-predecessor")
        for predecessor in predecessor_data.values():
            item = _mapping(predecessor, "window-predecessor")
            if set(item) != {"revision_id", "revision_record_hash", "version"}:
                raise BlockContractError("window-predecessor")
            revision_id = item["revision_id"]
            revision_hash = item["revision_record_hash"]
            version = item["version"]
            if revision_id is None:
                if revision_hash is not None or version is not None:
                    raise BlockContractError("window-predecessor")
            elif (
                not isinstance(revision_id, str)
                or not isinstance(revision_hash, str)
                or not isinstance(version, int)
                or isinstance(version, bool)
                or version < 1
            ):
                raise BlockContractError("window-predecessor")
            else:
                _hash(revision_hash, "window-predecessor-hash")
        model_data = _mapping(attempt.model_profile_json, "window-model-profile")
        _mapping(attempt.admission_json, "window-admission")
        if not isinstance(attempt.context_manifest_json, list):
            raise BlockContractError("window-context-manifest")
        prompt_data = {
            "messages": messages,
            "response_format": packet_data["response_format"],
            "transport": packet_data["transport"],
        }
        if (
            not _witness_matches(prompt_data, attempt.prompt_hash, "prompt")
            or not _witness_matches(slots, attempt.context_hash, "context")
            or not _witness_matches(
                model_data, attempt.model_profile_hash, "model_profile"
            )
            or not _witness_matches(
                predecessor_data, attempt.predecessor_hash, "predecessor"
            )
        ):
            raise BlockContractError("window-attempt-evidence")
        settings_data = _mapping(attempt.settings_json, "window-settings")
        if not _witness_matches(settings_data, attempt.settings_hash, "settings"):
            raise BlockContractError("window-settings-hash")
        if (
            attempt.source_plan_hash != plan.plan_hash
            or attempt.source_content_hash != plan.snapshot.content_hash
            or not _attempt_valid(
                attempt.attempt_record_hash, **_attempt_receipt_values(attempt)
            )
        ):
            raise BlockContractError("window-attempt-hash")
        if (
            attempt.retry_of_attempt_id is not None
            and attempt.rebase_of_attempt_id is not None
        ):
            raise BlockContractError("window-lineage-ambiguous")
        for field, identifier, witness in (
            ("retry", attempt.retry_of_attempt_id, attempt.retry_of_attempt_hash),
            ("rebase", attempt.rebase_of_attempt_id, attempt.rebase_of_attempt_hash),
        ):
            if (identifier is None) != (witness is None):
                raise BlockContractError("window-lineage-witness")
            if identifier is not None:
                parent = db.session.get(TranslationWindowAttempt, identifier)
                if (
                    parent is None
                    or parent.source_plan_id != record.id
                    or parent.attempt_record_hash != witness
                ):
                    raise BlockContractError(f"window-{field}-witness")
                _rehydrate_window_attempt(
                    parent,
                    record,
                    lineage=lineage | {attempt.id},
                    verified_plan=plan,
                    verified_evidence=verified_evidence,
                    research_verification_context=research_verification_context,
                )
        rehydrate_attempt_events(attempt)
        rehydrate_attempt_revision_links(attempt, record, block_ids)
        verify_attempt_state_head(attempt)
        requested_output = packet_data.get("requested_output_tokens")
        if (
            isinstance(requested_output, bool)
            or not isinstance(requested_output, int)
            or requested_output < 1
        ):
            raise BlockContractError("window-packet")
        restored_slots: list[ContextSlot] = []
        for item in slots:
            slot = _mapping(item, "window-packet-slot")
            if set(slot) != {
                "type",
                "content",
                "provenance",
                "artifact_id",
                "artifact_version",
                "artifact_hash",
                "dependency",
                "required",
            }:
                raise BlockContractError("window-packet-slot")
            if not isinstance(slot["required"], bool):
                raise BlockContractError("window-packet-slot")
            restored_slots.append(
                ContextSlot(
                    str(slot["type"]),
                    str(slot["content"]),
                    _mapping(slot["provenance"], "window-packet-slot"),
                    str(slot["artifact_id"]),
                    str(slot["artifact_version"]),
                    str(slot["artifact_hash"]),
                    _mapping(slot["dependency"], "window-packet-slot"),
                    slot["required"],
                )
            )
        if list(
            context_dependency_manifest(restored_slots)
        ) != attempt.context_manifest_json or not _witness_matches(
            attempt.context_manifest_json,
            attempt.context_manifest_hash,
            "context_manifest",
        ):
            raise BlockContractError("window-context-manifest")
        writable = [slot for slot in restored_slots if slot.slot_type == "writable_source"]
        if len(writable) != 1:
            raise BlockContractError("window-writable-response-contract")
        writable_version, writable_data = _writable_payload(writable[0])
        if writable_version == LOCATED_SOURCE_WRITABLE_CONTRACT_VERSION:
            rows = writable_data["blocks"]
            if len(rows) != len(block_ids):
                raise BlockContractError("compact-source-locator-replay")
            for number, (block_id, row) in enumerate(zip(block_ids, rows, strict=True), 1):
                validate_located_source_block(blocks[block_id], row, number=number)
        _verify_continuity_dependencies(
            record,
            restored_slots,
            verified_plan=plan,
        )
        _verify_research_and_memory_dependencies(
            record,
            attempt,
            restored_slots,
            verified_plan=plan,
            verified_evidence=verified_evidence,
            research_verification_context=research_verification_context,
        )
        return ProviderPacket(
            tuple(cast(Mapping[str, str], item) for item in messages),
            cast(Mapping[str, Any] | None, packet_data.get("response_format")),
            str(packet_data.get("transport")),
            requested_output,
            tuple(restored_slots),
        )
    except BlockContractError:
        raise
    except Exception as exc:
        raise BlockContractError("window-attempt-corrupt") from exc


def _verify_continuity_dependencies(
    record: SourcePlanRecord,
    slots: list[ContextSlot],
    *,
    verified_plan: TranslationSourcePlan | None = None,
) -> None:
    for slot in slots:
        if slot.slot_type == "continuity":
            dependency = slot.dependency
            pointer = db.session.get(
                TranslationTargetBlockActive,
                {
                    "source_plan_id": record.id,
                    "block_id": dependency["predecessor_block_id"],
                },
            )
            if (
                pointer is None
                or pointer.revision_id != dependency["revision_id"]
                or pointer.version != dependency["active_pointer_version"]
                or pointer.revision is None
                or pointer.revision.revision_record_hash
                != dependency["revision_record_hash"]
            ):
                raise BlockContractError("window-context-drift")
            _verify_active_alignment(
                record,
                pointer,
                "window-context-alignment",
                verified_plan=verified_plan,
            )
            continue
        if slot.slot_type not in {"exact_precedent", "sentence_precedent", "linked_notes"} or not slot.dependency:
            continue
        dependency = slot.dependency
        if dependency["source_plan_id"] != record.id:
            raise BlockContractError("window-context-drift")
        for item in dependency["active_targets"]:
            pointer = db.session.get(
                TranslationTargetBlockActive,
                {
                    "source_plan_id": record.id,
                    "block_id": item["block_id"],
                },
            )
            if (
                pointer is None
                or pointer.revision_id != item["revision_id"]
                or pointer.version != item["active_pointer_version"]
                or pointer.revision is None
                or pointer.revision.revision_record_hash != item["revision_record_hash"]
            ):
                raise BlockContractError("window-context-drift")
            _verify_active_alignment(
                record,
                pointer,
                "window-context-alignment",
                verified_plan=verified_plan,
            )


def _verify_research_and_memory_dependencies(
    record: SourcePlanRecord,
    attempt: TranslationWindowAttempt,
    slots: list[ContextSlot],
    *,
    verified_plan: TranslationSourcePlan,
    verified_evidence: VerifiedWindowEvidence | None = None,
    research_verification_context: ResearchVerificationContext | None = None,
) -> None:
    """Rehydrate packet-pinned evidence without consulting current pointers."""

    for slot in slots:
        if slot.slot_type == "research_evidence":
            if not slot.dependency:
                continue
            if verified_evidence is not None:
                if not verified_evidence.verifies_research_slot(slot):
                    raise BlockContractError("window-verified-evidence")
                continue
            run = db.session.get(TranslationResearchRun, slot.dependency["run_id"])
            baseline_id = slot.provenance.get("baseline_id")
            baseline_hash = slot.provenance.get("baseline_hash")
            baseline = (
                db.session.get(TranslationContextBaseline, baseline_id)
                if isinstance(baseline_id, str)
                else None
            )
            if (
                run is None
                or run.source_plan_id != record.id
                or run.project_id != attempt.project_id
                or run.owner_user_id != attempt.owner_user_id
                or baseline is None
                or baseline.operation_id != attempt.operation_id
                or baseline.source_plan_id != record.id
                or baseline.project_id != attempt.project_id
                or baseline.owner_user_id != attempt.owner_user_id
                or baseline.record_hash != baseline_hash
            ):
                raise BlockContractError("window-research-dependency")
            # The baseline is operation-bound, while the immutable research run
            # may be authorized from an earlier operation by the authenticated
            # evidence-reuse receipt. Rehydrating the baseline verifies that
            # cross-operation selection before we compare the exact conclusion
            # witnesses retained in this packet.
            rehydrate_context_baseline(
                baseline,
                record,
                verified_plan=verified_plan,
                verification_context=research_verification_context,
            )
            selected = [
                item
                for item in cast(Any, baseline.items)
                if item.item_kind == "research_evidence"
            ]
            if (
                len(selected) != 1
                or selected[0].research_run_id != run.id
                or not isinstance(selected[0].reference_json, Mapping)
                or selected[0].reference_json.get("conclusion_hash")
                != slot.dependency["conclusion_hash"]
            ):
                raise BlockContractError("window-research-dependency")
            conclusion = rehydrate_completed_research_conclusion(
                run,
                verified_plan=verified_plan,
                verification_context=research_verification_context,
            )
            content = canonical_json(conclusion["conclusion"])
            if (
                sha256_text(content) != slot.artifact_hash
                or conclusion["conclusion_hash"] != slot.dependency["conclusion_hash"]
                or conclusion["run_record_hash"] != slot.dependency["run_record_hash"]
                or conclusion["state_head_receipt"]
                != slot.dependency["state_head_receipt"]
                or conclusion["final_turn_id"] != slot.dependency["turn_id"]
                or conclusion["final_turn_record_hash"]
                != slot.dependency["turn_record_hash"]
                or conclusion["final_attempt_id"] != slot.dependency["attempt_id"]
                or conclusion["final_attempt_record_hash"]
                != slot.dependency["attempt_record_hash"]
                or conclusion["final_attempt_request_hash"]
                != slot.dependency["attempt_request_hash"]
            ):
                raise BlockContractError("window-research-dependency")
        elif slot.slot_type == "translation_memory":
            if not slot.dependency:
                continue
            dependency = slot.dependency
            baseline = db.session.get(
                TranslationContextBaseline, dependency["baseline_id"]
            )
            if (
                baseline is None
                or baseline.operation_id != attempt.operation_id
                or baseline.source_plan_id != record.id
                or baseline.project_id != record.project_id
                or baseline.owner_user_id != attempt.owner_user_id
                or baseline.record_hash != dependency["baseline_hash"]
            ):
                raise BlockContractError("window-memory-dependency")
            # Authenticate the baseline manifest and its exact historical
            # pointer reference before checking packet-selected entries.
            rehydrate_context_baseline(baseline, record)
            baseline_items = [
                item
                for item in cast(Any, baseline.items)
                if item.item_kind == "translation_memory"
            ]
            if len(baseline_items) != 1 or not isinstance(
                baseline_items[0].reference_json, Mapping
            ):
                raise BlockContractError("window-memory-dependency")
            baseline_reference = dict(baseline_items[0].reference_json)
            baseline_pointer = baseline_reference.get("pointer")
            if (
                not isinstance(baseline_pointer, Mapping)
                or baseline_reference.get("generation_id")
                != dependency["generation_id"]
                or baseline_reference.get("generation_record_hash")
                != dependency["generation_record_hash"]
                or baseline_reference.get("generation_state_receipt")
                != dependency["generation_state_receipt"]
            ):
                raise BlockContractError("window-memory-dependency")
            pointer = slot.provenance.get("pointer_reference")
            if not isinstance(pointer, Mapping) or dict(pointer) != dict(
                baseline_pointer
            ):
                raise BlockContractError("window-memory-dependency")
            generation, _pointer = verify_pinned_embedding_reference(pointer)
            if (
                generation.id != slot.dependency["generation_id"]
                or generation.record_hash != slot.dependency["generation_record_hash"]
                or _pointer["generation_state_receipt"]
                != slot.dependency["generation_state_receipt"]
                or pointer["version"] != slot.dependency["pointer_version"]
                or pointer["pointer_receipt"] != slot.dependency["pointer_receipt"]
            ):
                raise BlockContractError("window-memory-dependency")
            try:
                material = json.loads(slot.content)
            except (TypeError, ValueError) as exc:
                raise BlockContractError("window-memory-dependency") from exc
            if not isinstance(material, dict) or material.get(
                "contract_version"
            ) not in {
                "translation-memory-context-v1",
                "translation-memory-context-v2",
            }:
                raise BlockContractError("window-memory-dependency")
            if (
                material.get("query_hash") != dependency["query_hash"]
                or material.get("query_profile_hash")
                != dependency["query_profile_hash"]
            ):
                raise BlockContractError("window-memory-dependency")
            matches = material.get("matches")
            if not isinstance(matches, list) or not matches:
                raise BlockContractError("window-memory-dependency")
            if material["contract_version"] == "translation-memory-context-v2":
                if (
                    set(material)
                    != {
                        "contract_version",
                        "match_kind",
                        "granularity",
                        "query_hash",
                        "query_profile_hash",
                        "query_segments",
                        "matches",
                    }
                    or material.get("match_kind") != "semantic"
                    or material.get("granularity") != "alignment_group"
                    or dependency.get("top_k") != 2
                    or len(matches) > 6
                    or len(slot.content.encode("utf-8")) > 8_000
                ):
                    raise BlockContractError("window-memory-dependency")
                query_segments = material.get("query_segments")
                if (
                    not isinstance(query_segments, list)
                    or not query_segments
                    or len(query_segments) > 6
                ):
                    raise BlockContractError("window-memory-dependency")
                query_hash_material: list[dict[str, Any]] = []
                writable_block_ids = attempt.block_ids_json
                if (
                    not isinstance(writable_block_ids, list)
                    or not writable_block_ids
                    or any(
                        not isinstance(block_id, str) for block_id in writable_block_ids
                    )
                ):
                    raise BlockContractError("window-memory-dependency")
                writable_ids = set(writable_block_ids)
                blocks_by_id = {block.id: block for block in verified_plan.blocks}
                seen_query_locators: set[tuple[str, str, int]] = set()
                covered_match_indices: set[int] = set()
                for query in query_segments:
                    if not isinstance(query, Mapping) or set(query) != {
                        "block_id",
                        "source_anchor_id",
                        "source_sentence_order",
                        "query_hash",
                        "matches",
                    }:
                        raise BlockContractError("window-memory-dependency")
                    block_id = query.get("block_id")
                    anchor_id = query.get("source_anchor_id")
                    sentence_order = query.get("source_sentence_order")
                    row_query_hash = query.get("query_hash")
                    query_matches = query.get("matches")
                    if (
                        not isinstance(block_id, str)
                        or block_id not in writable_ids
                        or not isinstance(anchor_id, str)
                        or not isinstance(sentence_order, int)
                        or isinstance(sentence_order, bool)
                        or sentence_order < 1
                        or not isinstance(row_query_hash, str)
                        or not isinstance(query_matches, list)
                        or len(query_matches) > 2
                    ):
                        raise BlockContractError("window-memory-dependency")
                    locator = (block_id, anchor_id, sentence_order)
                    if locator in seen_query_locators:
                        raise BlockContractError("window-memory-dependency")
                    seen_query_locators.add(locator)
                    block = blocks_by_id.get(block_id)
                    if (
                        block is None
                        or block.role in _SEGMENT_MEMORY_QUERY_EXCLUDED_ROLES
                    ):
                        raise BlockContractError("window-memory-dependency")
                    anchor = next(
                        (
                            candidate
                            for candidate in block.anchors
                            if candidate.id == anchor_id
                            and candidate.order == sentence_order
                        ),
                        None,
                    )
                    if anchor is None:
                        raise BlockContractError("window-memory-dependency")
                    query_text = block.projection[anchor.start : anchor.end]
                    if not _segment_memory_query_text_is_eligible(
                        query_text
                    ) or row_query_hash != sha256_text(query_text):
                        raise BlockContractError("window-memory-dependency")
                    query_match_indices: list[int] = []
                    for reference in query_matches:
                        if not isinstance(reference, Mapping) or set(reference) != {
                            "match_index",
                            "score",
                            "distance",
                        }:
                            raise BlockContractError("window-memory-dependency")
                        match_index = reference.get("match_index")
                        score = reference.get("score")
                        distance = reference.get("distance")
                        try:
                            finite_metrics = math.isfinite(
                                float(score)
                            ) and math.isfinite(float(distance))
                        except (OverflowError, TypeError, ValueError):
                            finite_metrics = False
                        if (
                            not isinstance(match_index, int)
                            or isinstance(match_index, bool)
                            or match_index < 0
                            or match_index >= len(matches)
                            or isinstance(score, bool)
                            or not isinstance(score, (int, float))
                            or not finite_metrics
                            or isinstance(distance, bool)
                            or not isinstance(distance, (int, float))
                        ):
                            raise BlockContractError("window-memory-dependency")
                        query_match_indices.append(match_index)
                    if len(query_match_indices) != len(set(query_match_indices)):
                        raise BlockContractError("window-memory-dependency")
                    covered_match_indices.update(query_match_indices)
                    query_hash_material.append(
                        {
                            "block_id": block_id,
                            "source_anchor_id": anchor_id,
                            "source_sentence_order": sentence_order,
                            "query_hash": row_query_hash,
                        }
                    )
                if sha256_text(canonical_json(query_hash_material)) != dependency[
                    "query_hash"
                ] or covered_match_indices != set(range(len(matches))):
                    raise BlockContractError("window-memory-dependency")
                v2_memory_ids: list[str] = []
                for match in matches:
                    if (
                        not isinstance(match, Mapping)
                        or set(match) != {"memory"}
                        or not isinstance(match.get("memory"), Mapping)
                        or not isinstance(match["memory"].get("id"), str)
                    ):
                        raise BlockContractError("window-memory-dependency")
                    v2_memory_ids.append(match["memory"]["id"])
                if len(v2_memory_ids) != len(set(v2_memory_ids)):
                    raise BlockContractError("window-memory-dependency")
            expected_embedding_ids = dependency["embedding_entry_ids"]
            expected_embedding_hashes = dependency["embedding_entry_record_hashes"]
            expected_ids = dependency["memory_entry_ids"]
            expected_hashes = dependency["memory_entry_record_hashes"]
            if len(matches) != len(expected_ids) or len(matches) != len(
                expected_embedding_ids
            ):
                raise BlockContractError("window-memory-dependency")
            entries_by_id = {entry.id: entry for entry in cast(Any, generation.entries)}
            for (
                match,
                expected_embedding_id,
                expected_embedding_hash,
                expected_id,
                expected_hash,
            ) in zip(
                matches,
                expected_embedding_ids,
                expected_embedding_hashes,
                expected_ids,
                expected_hashes,
                strict=True,
            ):
                expected_match_keys = (
                    {"memory"}
                    if material["contract_version"] == "translation-memory-context-v2"
                    else {"score", "distance", "memory"}
                )
                if not isinstance(match, Mapping) or set(match) != expected_match_keys:
                    raise BlockContractError("window-memory-dependency")
                entry = entries_by_id.get(expected_embedding_id)
                if (
                    entry is None
                    or entry.subject_type != "memory"
                    or entry.subject_id != expected_id
                    or entry.record_hash != expected_embedding_hash
                ):
                    raise BlockContractError("window-memory-dependency")
                memory = db.session.get(TranslationMemoryEntry, expected_id)
                if memory is None or memory.record_hash != expected_hash:
                    raise BlockContractError("window-memory-dependency")
                evidence = rehydrate_translation_memory_entry(
                    memory, record, session=db.session
                )
                if match.get("memory") != safe_memory_projection(evidence):
                    raise BlockContractError("window-memory-dependency")


def rehydrate_attempt_events(
    attempt: TranslationWindowAttempt,
) -> tuple[TranslationWindowAttemptEvent, ...]:
    events = tuple(
        db.session.query(TranslationWindowAttemptEvent)
        .filter_by(attempt_id=attempt.id)
        .order_by(TranslationWindowAttemptEvent.sequence)
        .all()
    )
    if not events or events[0].event_type != "request_prepared":
        raise BlockContractError("window-event-missing")
    previous: str | None = None
    for sequence, event in enumerate(events, start=1):
        if event.sequence != sequence or event.predecessor_event_hash != previous:
            raise BlockContractError("window-event-chain")
        if not _witness_matches(
            event.payload_json, event.payload_hash, "event_payload"
        ) or not _event_valid(event, previous):
            raise BlockContractError("window-event-hash")
        previous = event.event_hash
    event_types = tuple(event.event_type for event in events)
    grammar = {
        "prepared": (("request_prepared",),),
        "dispatched": (("request_prepared", "provider_dispatched"),),
        "external_pending": (
            (
                "request_prepared",
                "provider_dispatched",
                "external_work_pending",
            ),
        ),
        "response_received": (
            ("request_prepared", "provider_dispatched", "provider_response"),
            (
                "request_prepared",
                "provider_dispatched",
                "external_work_pending",
                "provider_response",
            ),
        ),
        "provider_failed": (
            ("request_prepared", "provider_dispatched", "provider_failure"),
            (
                "request_prepared",
                "provider_dispatched",
                "external_work_pending",
                "provider_failure",
            ),
        ),
        "provider_outcome_uncertain": (
            (
                "request_prepared",
                "provider_dispatched",
                "provider_outcome_uncertain",
            ),
        ),
    }
    expected = grammar.get(attempt.dispatch_state)
    if expected is not None and event_types not in expected:
        raise BlockContractError("window-event-transcript")
    if expected is None:
        response_prefixes = (
            ("request_prepared", "provider_dispatched", "provider_response"),
            (
                "request_prepared",
                "provider_dispatched",
                "external_work_pending",
                "provider_response",
            ),
        )
        response_prefix = next(
            (
                prefix
                for prefix in response_prefixes
                if event_types[: len(prefix)] == prefix
            ),
            None,
        )
        if response_prefix is None:
            raise BlockContractError("window-event-transcript")
        tail = event_types[len(response_prefix) :]
        terminal = {
            "normalization_failed": "normalization_failed",
            "fence_failed": "post_response_fence_failed",
            "staged": "revisions_staged",
        }.get(attempt.dispatch_state)
        if attempt.dispatch_state == "staging_failed":
            valid = bool(tail) and set(tail) == {"staging_failure"}
        elif terminal is not None:
            valid = (
                bool(tail)
                and tail[-1] == terminal
                and set(tail[:-1]) <= {"staging_failure"}
            )
        else:
            valid = False
        if not valid:
            raise BlockContractError("window-event-transcript")
    return events


def rehydrate_attempt_revision_links(
    attempt: TranslationWindowAttempt,
    record: SourcePlanRecord,
    block_ids: tuple[str, ...],
) -> tuple[TranslationWindowAttemptRevision, ...]:
    links = tuple(
        db.session.query(TranslationWindowAttemptRevision)
        .filter_by(attempt_id=attempt.id)
        .order_by(TranslationWindowAttemptRevision.block_id)
        .all()
    )
    if attempt.dispatch_state != "staged":
        if links:
            raise BlockContractError("window-revision-orphan")
        return links
    if {link.block_id for link in links} != set(block_ids) or len(links) != len(
        block_ids
    ):
        raise BlockContractError("window-revision-links")
    for link in links:
        revision = db.session.get(TranslationTargetBlockRevision, link.revision_id)
        if (
            revision is None
            or link.source_plan_id != record.id
            or revision.source_plan_id != record.id
            or revision.block_id != link.block_id
            or link.candidate_hash != revision.candidate_hash
            or link.revision_record_hash != revision.revision_record_hash
        ):
            raise BlockContractError("window-revision-link")
    return links


__all__ = [
    "VerifiedWindowEvidence",
    "append_attempt_event",
    "persist_window_attempt",
    "predecessor_snapshot",
    "provider_response_event_payload",
    "rehydrate_attempt_events",
    "rehydrate_attempt_revision_links",
    "rehydrate_window_attempt",
    "rehydrate_window_provider_response",
    "seal_attempt_state_head",
    "verify_attempt_state_head",
]
