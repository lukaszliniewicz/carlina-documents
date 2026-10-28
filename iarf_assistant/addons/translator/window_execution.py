"""Internal injected-provider coordinator for Phase-3 translation windows."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from time import perf_counter
from typing import Any, cast

from sqlalchemy import exists
from sqlalchemy.orm import sessionmaker

from carlina.core.utc import utc_now_db
from iarf_assistant.core.model_access import resolve_effective_model_record
from iarf_assistant.core.model_capabilities import (
    ModelCapabilityProfile,
    admit_provider_payload,
    resolve_model_capabilities,
)
from iarf_assistant.core.model_readiness import require_model_ready
from iarf_assistant.core.task_queue import ExternalWorkPending
from iarf_assistant.extensions import db

from .block_contract import (
    BlockContractError,
    TranslationSourcePlan,
    canonical_json,
    sha256_text,
)
from .block_notes import (
    ANNOTATION_PROFILE_NONE,
    ANNOTATION_PROFILE_RESTRAINED,
    ANNOTATION_PROFILE_STUDY,
    MAX_BLOCK_NOTES_PER_WINDOW,
    MAX_STUDY_NOTES_PER_WINDOW,
    BlockNoteCandidate,
    normalize_annotation_profile,
)
from .block_persistence import rehydrate_source_plan
from .compact_markup import normalize_compact_result
from .models import (
    TranslationOperation,
    TranslationTargetAlignmentActive,
    TranslationTargetBlockActive,
    TranslationWindowAttempt,
    TranslationWindowAttemptRevision,
)
from .models import TranslationSourcePlan as SourcePlanRecord
from .operations import (
    TranslationOperationCancellation,
    TranslationOperationNotRunnable,
    TranslationOperationTransitionConflict,
)
from .rehydration_cache import TranslationRehydrationCache
from .sentence_alignment import (
    SentenceAlignmentManifest,
    normalize_marked_sentence_alignment,
    normalize_sentence_alignment,
)
from .target_contract import TargetBlockCandidate, normalize_target_candidate
from .target_persistence import lock_target_dependency_scope, persist_target_revision
from .target_validation import (
    TargetCandidateValidationError,
    require_valid_target_candidate,
)
from .window_contract import (
    COMPACT_WRITABLE_SOURCE_CONTRACT_VERSION,
    LEGACY_WRITABLE_SOURCE_CONTRACT_VERSION,
    LOCATED_SOURCE_WRITABLE_CONTRACT_VERSION,
    NOTES_WRITABLE_SOURCE_CONTRACT_VERSION,
    PARTS_WRITABLE_SOURCE_CONTRACT_VERSION,
    SENTENCE_ALIGNED_WRITABLE_SOURCE_CONTRACT_VERSION,
    SINGLE_COPY_WRITABLE_SOURCE_CONTRACT_VERSION,
    STUDY_NOTES_WRITABLE_SOURCE_CONTRACT_VERSION,
    WRITABLE_SOURCE_CONTRACT_VERSION,
    ProviderPacket,
    TranslationWindow,
)
from .window_persistence import (
    VerifiedWindowEvidence,
    _verify_active_alignment,
    append_attempt_event,
    persist_window_attempt,
    predecessor_snapshot,
    provider_response_event_payload,
    rehydrate_window_attempt,
    rehydrate_window_provider_response,
)

logger = logging.getLogger(__name__)

_JSON_FENCE = re.compile(
    r"```(?:json)?[ \t]*\r?\n(?P<payload>.*)\r?\n```",
    flags=re.IGNORECASE | re.DOTALL,
)
_MAX_UNESCAPED_QUOTE_REPAIRS = 4_096


def _packet_target_language(packet: ProviderPacket) -> str:
    """Recover the exact target-language pin from a retained provider packet."""

    slots = tuple(slot for slot in packet.slots if slot.slot_type == "project_policy")
    if len(slots) != 1:
        raise BlockContractError("window-target-language")
    try:
        payload = json.loads(slots[0].content)
    except (TypeError, ValueError, RecursionError) as exc:
        raise BlockContractError("window-target-language") from exc
    target_language = (
        payload.get("target_language") if isinstance(payload, Mapping) else None
    )
    if not isinstance(target_language, str) or not target_language.strip():
        raise BlockContractError("window-target-language")
    return target_language.strip()


def _next_non_space(value: str, start: int) -> tuple[int, str]:
    index = start
    while index < len(value) and value[index].isspace():
        index += 1
    return index, value[index] if index < len(value) else ""


def _json_quote_closes_string(value: str, index: int) -> bool:
    following_index, following = _next_non_space(value, index + 1)
    if following == ",":
        _, after_comma = _next_non_space(value, following_index + 1)
        return after_comma in {'"', "{", "[", "}", "]"}
    # Quotes, objects, or arrays without a delimiter remain structural failures;
    # a quote repair must never turn the next key or value into prose.
    return following == "" or following in {'"', "{", "[", "}", "]", ":"}


def _repair_unescaped_json_string_quotes(value: str) -> str | None:
    """Escape only quotes that cannot legally close their current JSON string.

    Translation models occasionally emit a typographic opening quotation mark
    and an unescaped ASCII closing quote inside an otherwise exact JSON string.
    This repair changes serialization only: decoding produces the model's exact
    character again, and the ordinary strict response, markup, and source-owner
    validators still run afterwards. Missing delimiters and arbitrary prose are
    deliberately not repaired.
    """

    output: list[str] = []
    in_string = False
    escaped = False
    repairs = 0

    for index, character in enumerate(value):
        if not in_string:
            output.append(character)
            if character == '"':
                in_string = True
            continue
        if escaped:
            output.append(character)
            escaped = False
            continue
        if character == "\\":
            output.append(character)
            escaped = True
            continue
        if character != '"':
            output.append(character)
            continue

        if _json_quote_closes_string(value, index):
            output.append(character)
            in_string = False
            continue

        repairs += 1
        if repairs > _MAX_UNESCAPED_QUOTE_REPAIRS:
            return None
        output.append('\\"')

    if repairs == 0:
        return None
    return "".join(output)


@dataclass(frozen=True, slots=True)
class ModelExecutionProfile:
    model_id: str
    capability_profile: Mapping[str, Any]
    transport: str
    fingerprint: str

    def to_dict(self) -> dict[str, object]:
        return {
            "model_id": self.model_id,
            "capability_profile": dict(self.capability_profile),
            "transport": self.transport,
            "fingerprint": self.fingerprint,
        }


def execution_profile_from_frozen_phase(
    phase: Mapping[str, Any],
) -> ModelExecutionProfile:
    """Build admission/transport evidence without consulting mutable catalogues."""

    model = phase.get("model") if isinstance(phase, Mapping) else None
    if not isinstance(model, Mapping):
        raise BlockContractError("window-model-profile")
    data = model.get("capability_profile")
    profile_hash = model.get("capability_profile_hash")
    if not isinstance(data, Mapping) or not isinstance(profile_hash, str):
        raise BlockContractError("window-model-profile")
    try:
        encoded = canonical_json(dict(data)).encode("utf-8")
        capability = ModelCapabilityProfile(**dict(data))
    except (TypeError, ValueError, RecursionError) as exc:
        raise BlockContractError("window-model-profile") from exc
    if (
        hashlib.sha256(encoded).hexdigest() != profile_hash
        or capability.model_id != model.get("model_id")
        or capability.provider_name != model.get("provider_name")
        or capability.provider_kind != model.get("provider_kind")
    ):
        raise BlockContractError("window-model-profile")
    native = (
        capability.supports_structured_output
        and capability.structured_output_mode == "json_schema"
    )
    transport = "native_json_schema" if native else "strict_text_json"
    raw = {
        "contract_version": "frozen-model-execution-profile-v1",
        "model_id": capability.model_id,
        "model_record_id": model.get("model_record_id"),
        "provider_id": model.get("provider_id"),
        "capability_profile": dict(data),
        "capability_profile_hash": profile_hash,
        "transport": transport,
    }
    return ModelExecutionProfile(
        capability.model_id,
        raw,
        transport,
        sha256_text(canonical_json(raw)),
    )


def resolve_execution_profile(
    project: Any,
    *,
    model_id: str | None = None,
) -> ModelExecutionProfile:
    """Resolve only the project's selected configured model; never route/fallback."""

    selected = str(
        model_id
        if model_id is not None
        else (getattr(project, "settings", None) or {}).get("translator_model") or ""
    ).strip()
    if not selected:
        raise BlockContractError("window-model-unselected")
    readiness = require_model_ready(
        selected, user_id=project.user_id, required_model_type="chat"
    )
    if not readiness.ready:
        raise BlockContractError("window-model-unready")
    record = resolve_effective_model_record(selected, user_id=project.user_id)
    if record is None or not record.is_active or record.model_type != "chat":
        raise BlockContractError("window-model-unready")
    profile = resolve_model_capabilities(
        selected, user_id=project.user_id, model_record=record
    )
    # Native schema is opt-in only; JSON-object/drop_params is not equivalent.
    native = (
        profile.supports_structured_output
        and profile.structured_output_mode == "json_schema"
    )
    transport = "native_json_schema" if native else "strict_text_json"
    raw = {
        "model_id": selected,
        "model_record_id": record.id,
        "provider_id": record.provider_id,
        "readiness": readiness.to_dict(),
        "capability_profile": profile.to_dict(),
        "transport": transport,
    }
    return ModelExecutionProfile(
        selected, raw, transport, sha256_text(canonical_json(raw))
    )


def admit_packet(
    packet: ProviderPacket,
    profile: ModelExecutionProfile,
    *,
    token_counter: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    data = profile.capability_profile.get("capability_profile")
    if not isinstance(data, Mapping):
        raise BlockContractError("window-model-profile")
    capability = ModelCapabilityProfile(**dict(data))
    if packet.requested_output_tokens > capability.max_output_tokens:
        raise BlockContractError("window-output-reserve")
    result = admit_provider_payload(
        profile=capability,
        messages=[dict(message) for message in packet.messages],
        response_format=dict(packet.response_format)
        if packet.response_format
        else None,
        requested_output_tokens=packet.requested_output_tokens,
        mode="manual",
        token_counter=token_counter,
    )
    evidence = result.to_dict()
    if evidence["requested_output_tokens"] != packet.requested_output_tokens:
        raise BlockContractError("window-output-reserve")
    return evidence


def prepare_and_commit_window_attempt(
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
) -> Any:
    """Commit request evidence before any caller may invoke its provider adapter."""

    attempt = persist_window_attempt(
        operation=operation,
        source_plan=source_plan,
        window=window,
        packet=packet,
        settings=settings,
        model_profile=model_profile,
        admission=admission,
        retry_of_attempt_id=retry_of_attempt_id,
        rebase_of_attempt_id=rebase_of_attempt_id,
        verified_evidence=verified_evidence,
    )
    db.session.commit()
    return attempt


def normalize_window_response(
    value: Mapping[str, Any] | str,
    *,
    plan: TranslationSourcePlan,
    plan_id: str,
    window: TranslationWindow,
    writable_source_contract_version: str | None = None,
    annotation_profile: object = None,
    target_language: str | None = None,
) -> (
    tuple[TargetBlockCandidate, ...]
    | tuple[tuple[TargetBlockCandidate, ...], tuple[BlockNoteCandidate, ...]]
    | tuple[
        tuple[TargetBlockCandidate, ...],
        tuple[BlockNoteCandidate, ...],
        tuple[SentenceAlignmentManifest, ...],
    ]
):
    """Normalize either native payload or strict JSON text into Phase-2 DTOs."""

    if isinstance(value, str):
        if len(value.encode("utf-8")) > 2 * 1024 * 1024:
            raise BlockContractError("window-response-size")
        candidate = value.strip()
        fenced = _JSON_FENCE.fullmatch(candidate)
        if fenced is not None:
            candidate = fenced.group("payload")
        try:
            value = json.loads(candidate)
        except (TypeError, ValueError, RecursionError) as original:
            repaired = _repair_unescaped_json_string_quotes(candidate)
            if repaired is None:
                raise BlockContractError("window-response-json") from original
            try:
                value = json.loads(repaired)
            except (TypeError, ValueError, RecursionError) as repaired_error:
                raise BlockContractError("window-response-json") from repaired_error
    if (
        not isinstance(value, Mapping)
        or set(value) != {"results"}
        or not isinstance(value["results"], list)
    ):
        raise BlockContractError("window-response-shape")
    results = value["results"]
    if len(results) != len(window.block_ids):
        raise BlockContractError("window-response-cardinality")
    blocks = {block.id: block for block in plan.blocks}
    sentence_aligned = (
        writable_source_contract_version
        in {
            SENTENCE_ALIGNED_WRITABLE_SOURCE_CONTRACT_VERSION,
            SINGLE_COPY_WRITABLE_SOURCE_CONTRACT_VERSION,
            LOCATED_SOURCE_WRITABLE_CONTRACT_VERSION,
        }
    )
    effective_annotation_profile = (
        normalize_annotation_profile(annotation_profile)
        if sentence_aligned
        else (
            ANNOTATION_PROFILE_RESTRAINED
            if writable_source_contract_version
            == NOTES_WRITABLE_SOURCE_CONTRACT_VERSION
            else (
                ANNOTATION_PROFILE_STUDY
                if writable_source_contract_version
                == STUDY_NOTES_WRITABLE_SOURCE_CONTRACT_VERSION
                else ANNOTATION_PROFILE_NONE
            )
        )
    )
    notes_enabled = effective_annotation_profile != ANNOTATION_PROFILE_NONE
    compact_keys = {"n", "content", "fields"}
    if notes_enabled:
        compact_keys.add("notes")
    if sentence_aligned:
        compact_keys.update({"notes", "alignment"})
        compact_key_sets = {frozenset(compact_keys)}
    else:
        compact_key_sets = {
            frozenset(compact_keys),
            frozenset((*compact_keys, "role")),
        }
    if notes_enabled:
        compact_key_sets = {frozenset(compact_keys)}
    compact = all(
        isinstance(item, Mapping) and frozenset(item) in compact_key_sets
        for item in results
    )
    if writable_source_contract_version in {
        COMPACT_WRITABLE_SOURCE_CONTRACT_VERSION,
        WRITABLE_SOURCE_CONTRACT_VERSION,
        NOTES_WRITABLE_SOURCE_CONTRACT_VERSION,
        STUDY_NOTES_WRITABLE_SOURCE_CONTRACT_VERSION,
        SENTENCE_ALIGNED_WRITABLE_SOURCE_CONTRACT_VERSION,
        SINGLE_COPY_WRITABLE_SOURCE_CONTRACT_VERSION,
        LOCATED_SOURCE_WRITABLE_CONTRACT_VERSION,
    }:
        if not compact:
            raise BlockContractError("window-response-contract")
    elif writable_source_contract_version in {
        LEGACY_WRITABLE_SOURCE_CONTRACT_VERSION,
        PARTS_WRITABLE_SOURCE_CONTRACT_VERSION,
    }:
        if compact:
            raise BlockContractError("window-response-contract")
    elif writable_source_contract_version is not None:
        raise BlockContractError("window-response-contract")
    if compact:
        candidates_list: list[TargetBlockCandidate] = []
        notes_list: list[BlockNoteCandidate] = []
        manifests_raw: list[Mapping[str, Any]] = []
        marker_ends: list[list[int]] = []
        for index, (block_id, item) in enumerate(
            zip(window.block_ids, results, strict=True), 1
        ):
            block = blocks[block_id]
            # Some strict-text models harmlessly echo the input-only semantic
            # role beside an otherwise exact result. Accept it only when it is
            # byte-for-byte the source-owned role, then discard it before the
            # compact candidate boundary. No model-authored metadata survives.
            if "role" in item and item["role"] != block.role:
                raise BlockContractError("window-response-contract")
            compact_value = {
                key: item[key] for key in compact_keys if key != "alignment"
            }
            sentence_ends: list[int] = []
            marker_ends.append(sentence_ends)
            normalized = normalize_compact_result(
                plan=plan,
                plan_id=plan_id,
                block=block,
                expected_number=index,
                value=compact_value,
                implicit_outer_wrappers=(
                    writable_source_contract_version
                    in {
                        WRITABLE_SOURCE_CONTRACT_VERSION,
                        NOTES_WRITABLE_SOURCE_CONTRACT_VERSION,
                        STUDY_NOTES_WRITABLE_SOURCE_CONTRACT_VERSION,
                        SENTENCE_ALIGNED_WRITABLE_SOURCE_CONTRACT_VERSION,
                        SINGLE_COPY_WRITABLE_SOURCE_CONTRACT_VERSION,
                        LOCATED_SOURCE_WRITABLE_CONTRACT_VERSION,
                    }
                ),
                annotation_profile=effective_annotation_profile,
                require_notes_array=sentence_aligned,
                sentence_ends=(
                    sentence_ends
                    if writable_source_contract_version in {
                        SINGLE_COPY_WRITABLE_SOURCE_CONTRACT_VERSION,
                        LOCATED_SOURCE_WRITABLE_CONTRACT_VERSION,
                    }
                    else None
                ),
            )
            if notes_enabled or sentence_aligned:
                candidate, item_notes = cast(
                    tuple[TargetBlockCandidate, tuple[BlockNoteCandidate, ...]],
                    normalized,
                )
                candidates_list.append(candidate)
                notes_list.extend(item_notes)
            else:
                candidates_list.append(cast(TargetBlockCandidate, normalized))
            if sentence_aligned:
                alignment = item.get("alignment")
                if not isinstance(alignment, Mapping):
                    raise BlockContractError("sentence-alignment-shape")
                manifests_raw.append(alignment)
        max_notes_per_window = (
            MAX_STUDY_NOTES_PER_WINDOW
            if effective_annotation_profile == ANNOTATION_PROFILE_STUDY
            else MAX_BLOCK_NOTES_PER_WINDOW
        )
        if len(notes_list) > max_notes_per_window:
            raise BlockContractError("block-note-window-cap")
        candidates = tuple(candidates_list)
    else:
        candidates = tuple(normalize_target_candidate(item) for item in results)
        if tuple(candidate.block_id for candidate in candidates) != window.block_ids:
            raise BlockContractError("window-response-cardinality")
    for candidate in candidates:
        require_valid_target_candidate(
            plan, plan_id, blocks[candidate.block_id], candidate
        )
    if sentence_aligned:
        if not isinstance(target_language, str) or not target_language.strip():
            raise BlockContractError("sentence-alignment-target-language")
        manifests = tuple(
            (
                normalize_marked_sentence_alignment(
                    block=blocks[candidate.block_id],
                    candidate=candidate,
                    value=manifest,
                    sentence_ends=ends,
                    target_language=target_language,
                )
                if writable_source_contract_version in {
                    SINGLE_COPY_WRITABLE_SOURCE_CONTRACT_VERSION,
                    LOCATED_SOURCE_WRITABLE_CONTRACT_VERSION,
                }
                else normalize_sentence_alignment(
                    block=blocks[candidate.block_id],
                    candidate=candidate,
                    value=manifest,
                    target_language=target_language,
                )
            )
            for candidate, manifest, ends in zip(
                candidates, manifests_raw, marker_ends, strict=True
            )
        )
        return candidates, tuple(notes_list), manifests
    if notes_enabled:
        return candidates, tuple(notes_list)
    return candidates


def _writable_contract_version(packet: ProviderPacket) -> str:
    writable = [slot for slot in packet.slots if slot.slot_type == "writable_source"]
    if len(writable) != 1:
        raise BlockContractError("window-response-contract")
    try:
        payload = json.loads(writable[0].content)
    except (TypeError, ValueError, RecursionError) as exc:
        raise BlockContractError("window-response-contract") from exc
    version = payload.get("contract_version") if isinstance(payload, Mapping) else None
    if version not in {
        LEGACY_WRITABLE_SOURCE_CONTRACT_VERSION,
        PARTS_WRITABLE_SOURCE_CONTRACT_VERSION,
        COMPACT_WRITABLE_SOURCE_CONTRACT_VERSION,
        WRITABLE_SOURCE_CONTRACT_VERSION,
        NOTES_WRITABLE_SOURCE_CONTRACT_VERSION,
        STUDY_NOTES_WRITABLE_SOURCE_CONTRACT_VERSION,
        SENTENCE_ALIGNED_WRITABLE_SOURCE_CONTRACT_VERSION,
        SINGLE_COPY_WRITABLE_SOURCE_CONTRACT_VERSION,
        LOCATED_SOURCE_WRITABLE_CONTRACT_VERSION,
    }:
        raise BlockContractError("window-response-contract")
    return cast(str, version)


def _writable_annotation_profile(
    packet: ProviderPacket, writable_contract_version: str
) -> str:
    """Resolve the note policy frozen into a retained provider packet."""

    if writable_contract_version in {
        SENTENCE_ALIGNED_WRITABLE_SOURCE_CONTRACT_VERSION,
        SINGLE_COPY_WRITABLE_SOURCE_CONTRACT_VERSION,
        LOCATED_SOURCE_WRITABLE_CONTRACT_VERSION,
    }:
        writable = [
            slot for slot in packet.slots if slot.slot_type == "writable_source"
        ]
        if len(writable) != 1:
            raise BlockContractError("window-response-contract")
        try:
            payload = json.loads(writable[0].content)
        except (TypeError, ValueError, RecursionError) as exc:
            raise BlockContractError("window-response-contract") from exc
        if not isinstance(payload, Mapping):
            raise BlockContractError("window-response-contract")
        return normalize_annotation_profile(payload.get("annotation_profile"))
    if writable_contract_version == STUDY_NOTES_WRITABLE_SOURCE_CONTRACT_VERSION:
        return ANNOTATION_PROFILE_STUDY
    if writable_contract_version == NOTES_WRITABLE_SOURCE_CONTRACT_VERSION:
        return ANNOTATION_PROFILE_RESTRAINED
    return ANNOTATION_PROFILE_NONE


def _independent_session():
    return sessionmaker(bind=db.engine, expire_on_commit=False)()


def _lineage_fence_reason(
    attempt: TranslationWindowAttempt, operation: TranslationOperation | None
) -> str | None:
    now = utc_now_db()
    if (
        operation is None
        or operation.id != attempt.operation_id
        or operation.project_id != attempt.project_id
        or operation.owner_user_id != attempt.owner_user_id
    ):
        return "operation"
    if (
        operation.state in {"cancel_requested", "cancelled"}
        or operation.cancel_requested_at is not None
    ):
        return "cancelled"
    if (
        operation.state != "running"
        or operation.version < attempt.operation_version
        or operation.queue_job_id != attempt.queue_job_id
        or operation.lease_expires_at is None
        or operation.lease_expires_at <= now
        or operation.source_content_hash != attempt.source_content_hash
        or operation.source_plan_id != attempt.source_plan_id
        or operation.source_plan_hash != attempt.source_plan_hash
    ):
        return "fence"
    return None


def _live_operation_lineage(attempt: TranslationWindowAttempt, now: Any) -> Any:
    """Match a live recovery generation of the same durable queue delivery."""

    return exists().where(
        TranslationOperation.id == attempt.operation_id,
        TranslationOperation.project_id == attempt.project_id,
        TranslationOperation.owner_user_id == attempt.owner_user_id,
        TranslationOperation.state == "running",
        TranslationOperation.version >= attempt.operation_version,
        TranslationOperation.queue_job_id == attempt.queue_job_id,
        TranslationOperation.cancel_requested_at.is_(None),
        TranslationOperation.lease_expires_at > now,
        TranslationOperation.source_content_hash == attempt.source_content_hash,
        TranslationOperation.source_plan_id == attempt.source_plan_id,
        TranslationOperation.source_plan_hash == attempt.source_plan_hash,
    )


def _exact_dispatch_fence(attempt: TranslationWindowAttempt, now: Any) -> Any:
    """Match only the operation generation that prepared this provider call."""

    return exists().where(
        TranslationOperation.id == attempt.operation_id,
        TranslationOperation.project_id == attempt.project_id,
        TranslationOperation.owner_user_id == attempt.owner_user_id,
        TranslationOperation.state == "running",
        TranslationOperation.version == attempt.operation_version,
        TranslationOperation.queue_job_id == attempt.queue_job_id,
        TranslationOperation.cancel_requested_at.is_(None),
        TranslationOperation.lease_expires_at == attempt.operation_lease_expires_at,
        TranslationOperation.lease_expires_at > now,
        TranslationOperation.source_content_hash == attempt.source_content_hash,
        TranslationOperation.source_plan_id == attempt.source_plan_id,
        TranslationOperation.source_plan_hash == attempt.source_plan_hash,
    )


def _fresh_fence(
    attempt_id: str,
    operation_id: str | None = None,
    expected_version: int | None = None,
) -> None:
    """Read the operation ledger outside the worker's pending transaction."""

    session = _independent_session()
    try:
        attempt = session.get(TranslationWindowAttempt, attempt_id)
        operation = (
            session.get(TranslationOperation, attempt.operation_id) if attempt else None
        )
        record = (
            session.get(SourcePlanRecord, attempt.source_plan_id) if attempt else None
        )
        if attempt is None:
            raise BlockContractError("window-attempt-operation")
        if (
            operation_id is not None
            and operation_id != attempt.operation_id
            or expected_version is not None
            and expected_version != attempt.operation_version
        ):
            raise BlockContractError("window-attempt-operation")
        if (
            record is None
            or record.plan_hash != attempt.source_plan_hash
            or record.source_content_hash != attempt.source_content_hash
        ):
            raise BlockContractError("window-source-drift")
        reason = _lineage_fence_reason(attempt, operation)
        if reason == "cancelled":
            raise TranslationOperationCancellation(
                "Translator operation cancellation was observed at a safe boundary."
            )
        if reason:
            if operation is None:
                raise BlockContractError("window-attempt-operation")
            if operation.version != attempt.operation_version:
                raise TranslationOperationTransitionConflict(
                    operation.id, attempt.operation_version
                )
            raise TranslationOperationNotRunnable(
                operation, "Translation operation no longer has a live worker claim."
            )
    finally:
        session.close()


def _verify_dependency_fence(
    session: Any,
    attempt_id: str,
    source_plan_id: str,
    window: TranslationWindow,
    *,
    verified_plan: TranslationSourcePlan | None = None,
) -> None:
    attempt = session.get(TranslationWindowAttempt, attempt_id)
    if attempt is None or attempt.source_plan_id != source_plan_id:
        raise BlockContractError("window-dependency-drift")
    predecessors = attempt.predecessor_json
    if not isinstance(predecessors, Mapping) or set(predecessors) != set(
        window.block_ids
    ):
        raise BlockContractError("window-dependency-drift")
    record = session.get(SourcePlanRecord, source_plan_id)
    operation = session.get(TranslationOperation, attempt.operation_id)
    if (
        record is None
        or record.plan_hash != attempt.source_plan_hash
        or record.source_content_hash != attempt.source_content_hash
        or operation is None
        or operation.project_id != attempt.project_id
        or operation.owner_user_id != attempt.owner_user_id
        or operation.source_content_hash != attempt.source_content_hash
        or operation.source_plan_id != record.id
        or operation.source_plan_hash != record.plan_hash
    ):
        raise BlockContractError("window-dependency-drift")
    # Immutable source content is verified once per fence, never per target.
    # Callers may reuse their invocation-local plan; all mutable pointer and
    # operation checks above and below still read the current transaction.
    dependency_plan = verified_plan
    verification_cache = TranslationRehydrationCache()

    def verify_alignment(pointer: TranslationTargetBlockActive, code: str) -> None:
        nonlocal dependency_plan
        if dependency_plan is None:
            dependency_plan = rehydrate_source_plan(record)
        _verify_active_alignment(
            record, pointer, code, session=session,
            verified_plan=dependency_plan, verification_cache=verification_cache,
        )

    for block_id, expected in predecessors.items():
        if not isinstance(expected, Mapping):
            raise BlockContractError("window-dependency-drift")
        pointer = session.get(
            TranslationTargetBlockActive,
            {"source_plan_id": source_plan_id, "block_id": block_id},
        )
        revision_id = expected.get("revision_id")
        if revision_id is None:
            if pointer is not None:
                raise BlockContractError("window-dependency-drift")
            continue
        if (
            pointer is None
            or pointer.revision_id != revision_id
            or pointer.version != expected.get("version")
            or pointer.revision is None
            or pointer.revision.revision_record_hash
            != expected.get("revision_record_hash")
        ):
            raise BlockContractError("window-dependency-drift")
        verify_alignment(pointer, "window-dependency-alignment")
    manifest = attempt.context_manifest_json
    if not isinstance(manifest, list):
        raise BlockContractError("window-context-drift")
    for item in manifest:
        if not isinstance(item, Mapping):
            continue
        slot_type = item.get("type")
        dependency = item.get("dependency")
        if not isinstance(dependency, Mapping):
            raise BlockContractError("window-context-drift")
        if slot_type in {"exact_precedent", "sentence_precedent", "linked_notes"}:
            if not dependency:
                continue
            if dependency.get("source_plan_id") != source_plan_id or not isinstance(
                dependency.get("active_targets"), list
            ):
                raise BlockContractError("window-context-drift")
            for target in dependency["active_targets"]:
                if not isinstance(target, Mapping):
                    raise BlockContractError("window-context-drift")
                pointer = session.get(
                    TranslationTargetBlockActive,
                    {
                        "source_plan_id": source_plan_id,
                        "block_id": target.get("block_id"),
                    },
                )
                if (
                    pointer is None
                    or pointer.revision_id != target.get("revision_id")
                    or pointer.version != target.get("active_pointer_version")
                    or pointer.revision is None
                    or pointer.revision.revision_record_hash
                    != target.get("revision_record_hash")
                ):
                    raise BlockContractError("window-context-drift")
                verify_alignment(pointer, "window-context-alignment")
            continue
        if slot_type != "continuity":
            continue
        pointer = session.get(
            TranslationTargetBlockActive,
            {
                "source_plan_id": source_plan_id,
                "block_id": dependency.get("predecessor_block_id"),
            },
        )
        if (
            pointer is None
            or pointer.revision_id != dependency.get("revision_id")
            or pointer.version != dependency.get("active_pointer_version")
            or pointer.revision is None
            or pointer.revision.revision_record_hash
            != dependency.get("revision_record_hash")
        ):
            raise BlockContractError("window-context-drift")
        verify_alignment(pointer, "window-context-alignment")


def _fresh_dependency_fence(
    attempt_id: str, source_plan_id: str, window: TranslationWindow
) -> None:
    """Re-read active predecessor/continuity evidence in an isolated session."""

    session = _independent_session()
    try:
        _verify_dependency_fence(session, attempt_id, source_plan_id, window)
    finally:
        session.close()


def _require_persisted_attempt(attempt_id: str) -> None:
    session = _independent_session()
    try:
        if session.get(TranslationWindowAttempt, attempt_id) is None:
            raise BlockContractError("window-attempt-uncommitted")
    finally:
        session.close()


def _transition_dispatch(
    attempt_id: str,
    *,
    expected_state: str,
    next_state: str,
    event_type: str,
    payload: Mapping[str, Any],
    require_lost_fence: bool = False,
) -> None:
    session = _independent_session()
    try:
        attempt = session.get(TranslationWindowAttempt, attempt_id)
        if attempt is None or attempt.dispatch_state != expected_state:
            raise BlockContractError("window-dispatch-replay")
        now = utc_now_db()
        values: dict[Any, Any] = {
            TranslationWindowAttempt.dispatch_state: next_state,
            TranslationWindowAttempt.dispatch_version: attempt.dispatch_version + 1,
        }
        if next_state in {
            "provider_failed",
            "provider_outcome_uncertain",
            "normalization_failed",
            "fence_failed",
            "staging_failed",
            "staged",
        }:
            values[TranslationWindowAttempt.dispatch_finished_at] = now
        query = session.query(TranslationWindowAttempt).filter(
            TranslationWindowAttempt.id == attempt_id,
            TranslationWindowAttempt.dispatch_state == expected_state,
            TranslationWindowAttempt.dispatch_version == attempt.dispatch_version,
        )
        if expected_state == "response_received" and next_state == "staged":
            query = query.filter(_live_operation_lineage(attempt, now))
        elif require_lost_fence:
            query = query.filter(~_exact_dispatch_fence(attempt, now))
        changed = query.update(values, synchronize_session=False)
        if changed != 1:
            if require_lost_fence or (
                expected_state == "response_received" and next_state == "staged"
            ):
                raise BlockContractError("window-dispatch-fence")
            raise BlockContractError("window-dispatch-replay")
        session.expire_all()
        attempt = session.get(TranslationWindowAttempt, attempt_id)
        if attempt is None:
            raise BlockContractError("window-dispatch-replay")
        append_attempt_event(attempt, event_type, payload, session=session)
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _claim_dispatch(
    attempt_id: str,
    operation_id: str,
    expected_version: int,
    expected_queue_job_id: str | None,
    before_compare_and_set: Callable[[], None] | None = None,
) -> None:
    """Claim dispatch with one cross-table conditional update transaction."""

    session = _independent_session()
    try:
        attempt = session.get(TranslationWindowAttempt, attempt_id)
        if attempt is None:
            raise BlockContractError("window-attempt-uncommitted")
        if (
            operation_id != attempt.operation_id
            or expected_version != attempt.operation_version
            or expected_queue_job_id != attempt.queue_job_id
        ):
            raise BlockContractError("window-attempt-operation")
        record = session.get(SourcePlanRecord, attempt.source_plan_id)
        if record is None or record.plan_hash != attempt.source_plan_hash:
            raise BlockContractError("window-source-drift")
        if before_compare_and_set is not None:
            before_compare_and_set()
        now = utc_now_db()
        live_operation = _exact_dispatch_fence(attempt, now)
        changed = (
            session.query(TranslationWindowAttempt)
            .filter(
                TranslationWindowAttempt.id == attempt_id,
                TranslationWindowAttempt.dispatch_state == "prepared",
                TranslationWindowAttempt.dispatch_version == attempt.dispatch_version,
                TranslationWindowAttempt.operation_id == attempt.operation_id,
                live_operation,
            )
            .update(
                {
                    TranslationWindowAttempt.dispatch_state: "dispatched",
                    TranslationWindowAttempt.dispatch_version: attempt.dispatch_version
                    + 1,
                    TranslationWindowAttempt.dispatch_claimed_at: now,
                },
                synchronize_session=False,
            )
        )
        if changed != 1:
            session.rollback()
            _fresh_fence(attempt_id)
            raise BlockContractError("window-dispatch-replay")
        session.expire_all()
        attempt = session.get(TranslationWindowAttempt, attempt_id)
        if attempt is None:
            raise BlockContractError("window-dispatch-replay")
        append_attempt_event(
            attempt,
            "provider_dispatched",
            {"packet_hash": attempt.packet_hash},
            session=session,
        )
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def execute_window_attempt(
    attempt: Any,
    *,
    operation: TranslationOperation,
    source_plan: SourcePlanRecord,
    window: TranslationWindow,
    provider_call: Callable[[Mapping[str, Any]], Mapping[str, Any] | str],
    before_dispatch_claim: Callable[[], None] | None = None,
    verified_evidence: VerifiedWindowEvidence | None = None,
    allow_external_reentry: bool = False,
) -> tuple[str, ...]:
    """Dispatch once or poll one durable passive dispatch, then stage revisions."""

    packet = rehydrate_window_attempt(
        attempt, source_plan, verified_evidence=verified_evidence
    )
    if (
        attempt.operation_id != operation.id
        or attempt.project_id != operation.project_id
        or attempt.owner_user_id != operation.owner_user_id
        or operation.source_content_hash != attempt.source_content_hash
        or operation.source_plan_id != attempt.source_plan_id
        or operation.source_plan_hash != attempt.source_plan_hash
    ):
        raise BlockContractError("window-attempt-operation")
    if (
        tuple(attempt.block_ids_json) != window.block_ids
        or tuple(attempt.block_fingerprints_json) != window.block_fingerprints
    ):
        raise BlockContractError("window-attempt-window")
    expected_operation_version = operation.version
    expected_queue_job_id = operation.queue_job_id
    stored_predecessor = attempt.predecessor_json
    if stored_predecessor != predecessor_snapshot(source_plan, window):
        raise BlockContractError("window-dependency-drift")
    _require_persisted_attempt(attempt.id)
    _fresh_dependency_fence(attempt.id, source_plan.id, window)
    dispatch_state = attempt.dispatch_state
    if dispatch_state == "prepared":
        _claim_dispatch(
            attempt.id,
            operation.id,
            expected_operation_version,
            expected_queue_job_id,
            before_dispatch_claim,
        )
        provider_state = "dispatched"
    elif (
        dispatch_state in {"dispatched", "external_pending"} and allow_external_reentry
    ):
        # The external work item is already the one durable provider-neutral
        # dispatch, or may have committed immediately before the process died.
        # Re-entering the same leased queue delivery calls only the passive
        # adapter, whose request fingerprint is idempotent; it never replays a
        # managed call.
        _fresh_fence(attempt.id)
        provider_state = dispatch_state
    else:
        raise BlockContractError("window-dispatch-replay")
    provider_payload = packet.to_dict()
    # Exactly one adapter call per invocation. In passive mode a later invocation
    # resolves the same durable work item rather than issuing another model call.
    try:
        response = provider_call(provider_payload)
    except ExternalWorkPending as exc:
        if provider_state == "dispatched":
            _transition_dispatch(
                attempt.id,
                expected_state="dispatched",
                next_state="external_pending",
                event_type="external_work_pending",
                payload={"work_item_id": exc.work_item_id},
            )
        raise
    except Exception as exc:
        _transition_dispatch(
            attempt.id,
            expected_state=provider_state,
            next_state="provider_failed",
            event_type="provider_failure",
            payload={"error_type": type(exc).__name__},
        )
        raise
    _transition_dispatch(
        attempt.id,
        expected_state=provider_state,
        next_state="response_received",
        event_type="provider_response",
        payload=provider_response_event_payload(response, transport=packet.transport),
    )
    return _stage_window_response(
        attempt,
        operation=operation,
        source_plan=source_plan,
        window=window,
        packet=packet,
        response=response,
    )


def _stage_window_response(
    attempt: Any,
    *,
    operation: TranslationOperation,
    source_plan: SourcePlanRecord,
    window: TranslationWindow,
    packet: ProviderPacket,
    response: Mapping[str, Any] | str,
    expected_dispatch_state: str = "response_received",
) -> tuple[str, ...]:
    """Normalize one already-durable response and atomically stage revisions."""

    stage_started = perf_counter()
    stage_attempt_id = attempt.id
    try:
        _fresh_fence(attempt.id)
        db.session.expire_all()
        refreshed_source_plan = db.session.get(SourcePlanRecord, source_plan.id)
        if refreshed_source_plan is None:
            raise BlockContractError("window-attempt-plan")
        source_plan = cast(SourcePlanRecord, refreshed_source_plan)
        _fresh_dependency_fence(attempt.id, source_plan.id, window)
        plan = rehydrate_source_plan(source_plan)
    except TranslationOperationCancellation:
        _transition_dispatch(
            attempt.id,
            expected_state=expected_dispatch_state,
            next_state="fence_failed",
            event_type="post_response_fence_failed",
            payload={"code": "cancelled"},
        )
        raise
    except (
        TranslationOperationNotRunnable,
        TranslationOperationTransitionConflict,
        BlockContractError,
    ) as exc:
        _transition_dispatch(
            attempt.id,
            expected_state=expected_dispatch_state,
            next_state="fence_failed",
            event_type="fence_failed",
            payload={"code": type(exc).__name__},
        )
        raise
    # Candidate normalization is a model-output boundary.  Keep it separate
    # from the operation/source/dependency fences above: target validation can
    # fail with any BlockContractError code (including target-validation-*),
    # and must therefore remain retryable model output rather than becoming a
    # misleading lineage fence.
    try:
        writable_contract_version = _writable_contract_version(packet)
        annotation_profile = _writable_annotation_profile(
            packet, writable_contract_version
        )
        normalized_response = normalize_window_response(
            response,
            plan=plan,
            plan_id=source_plan.id,
            window=window,
            writable_source_contract_version=writable_contract_version,
            annotation_profile=annotation_profile,
            target_language=(
                _packet_target_language(packet)
                if writable_contract_version
                in {
                    SENTENCE_ALIGNED_WRITABLE_SOURCE_CONTRACT_VERSION,
                    SINGLE_COPY_WRITABLE_SOURCE_CONTRACT_VERSION,
                    LOCATED_SOURCE_WRITABLE_CONTRACT_VERSION,
                }
                else None
            ),
        )
        manifests: tuple[SentenceAlignmentManifest, ...] = ()
        if (
            isinstance(normalized_response, tuple)
            and len(normalized_response) in {2, 3}
            and isinstance(normalized_response[0], tuple)
        ):
            if len(normalized_response) == 3:
                candidates, validated_notes, manifests = normalized_response
                manifests = cast(tuple[SentenceAlignmentManifest, ...], manifests)
            else:
                candidates, validated_notes = normalized_response
        else:
            candidates = cast(tuple[TargetBlockCandidate, ...], normalized_response)
            validated_notes = ()
    except BlockContractError as exc:
        payload: dict[str, object] = {"code": exc.code}
        if isinstance(exc, TargetCandidateValidationError):
            payload["finding_codes"] = list(exc.finding_codes)
        _transition_dispatch(
            attempt.id,
            expected_state=expected_dispatch_state,
            next_state="normalization_failed",
            event_type="normalization_failed",
            payload=payload,
        )
        logger.warning(
            "Translator window normalization failed: %s%s",
            exc.code,
            (
                f" ({', '.join(exc.finding_codes)})"
                if isinstance(exc, TargetCandidateValidationError)
                else ""
            ),
        )
        raise
    write_started = perf_counter()
    revision_ids: list[str] = []
    revision_hashes: list[str] = []
    try:
        with db.session.begin_nested():
            # Share a source-plan write lock with every active-pointer CAS.
            # This serializes the pointer-absent case too, then the dependency
            # evidence is checked inside the same transaction as the results.
            lock_target_dependency_scope(source_plan.id)
            db.session.expire_all()
            _verify_dependency_fence(
                db.session, attempt.id, source_plan.id, window, verified_plan=plan
            )
            notes_by_block: dict[str, tuple[BlockNoteCandidate, ...]] = {}
            for note in validated_notes:
                notes_by_block.setdefault(note.block_id, ())
                notes_by_block[note.block_id] += (note,)
            for candidate in candidates:
                revision = persist_target_revision(
                    source_plan,
                    candidate,
                    target_language=source_plan.project.target_lang,
                    author_type="model",
                    verified_plan=plan,
                    notes=notes_by_block.get(candidate.block_id, ()),
                    annotation_profile=annotation_profile,
                )
                revision_ids.append(revision.id)
                revision_hashes.append(revision.candidate_hash)
                db.session.add(
                    cast(Any, TranslationWindowAttemptRevision)(
                        attempt_id=attempt.id,
                        source_plan_id=source_plan.id,
                        block_id=candidate.block_id,
                        revision_id=revision.id,
                        candidate_hash=revision.candidate_hash,
                        revision_record_hash=revision.revision_record_hash,
                    )
                )
            db.session.flush()
            db.session.expire_all()
            _verify_dependency_fence(
                db.session, attempt.id, source_plan.id, window, verified_plan=plan
            )
            current = db.session.get(TranslationWindowAttempt, attempt.id)
            if current is None:
                raise BlockContractError("window-stage-conflict")
            now = utc_now_db()
            live_operation = _live_operation_lineage(current, now)
            changed = (
                db.session.query(TranslationWindowAttempt)
                .filter(
                    TranslationWindowAttempt.id == attempt.id,
                    TranslationWindowAttempt.dispatch_state == expected_dispatch_state,
                    TranslationWindowAttempt.dispatch_version
                    == current.dispatch_version,
                    live_operation,
                )
                .update(
                    {
                        TranslationWindowAttempt.dispatch_state: "staged",
                        TranslationWindowAttempt.dispatch_version: current.dispatch_version
                        + 1,
                        TranslationWindowAttempt.dispatch_finished_at: now,
                    },
                    synchronize_session=False,
                )
            )
            if changed != 1:
                raise BlockContractError("window-stage-conflict")
            db.session.expire_all()
            staged_attempt = db.session.get(TranslationWindowAttempt, attempt.id)
            if staged_attempt is None:
                raise BlockContractError("window-stage-conflict")
            append_attempt_event(
                staged_attempt,
                "revisions_staged",
                {"revision_ids": revision_ids, "candidate_hashes": revision_hashes},
            )
    except Exception:
        db.session.rollback()
        _transition_dispatch(
            attempt.id,
            expected_state=expected_dispatch_state,
            next_state="staging_failed",
            event_type="staging_failure",
            payload={"failure_hash": sha256_text("target_revision_staging_failed")},
        )
        raise
    db.session.commit()
    logger.info(
        "Translator stage timing attempt=%s window=%d blocks=%d prepare_ms=%.3f write_ms=%.3f",
        stage_attempt_id, window.order, len(revision_ids), (write_started - stage_started) * 1000,
        (perf_counter() - write_started) * 1000,
    )
    return tuple(revision_ids)


def _attempt_matches_window(
    attempt: TranslationWindowAttempt,
    *,
    operation: TranslationOperation,
    source_plan: SourcePlanRecord,
    window: TranslationWindow,
    verified_evidence: VerifiedWindowEvidence | None = None,
) -> ProviderPacket:
    packet = rehydrate_window_attempt(
        attempt, source_plan, verified_evidence=verified_evidence
    )
    if attempt.operation_id != operation.id:
        raise BlockContractError("window-attempt-operation")
    if (
        tuple(attempt.block_ids_json) != window.block_ids
        or tuple(attempt.block_fingerprints_json) != window.block_fingerprints
    ):
        raise BlockContractError("window-attempt-window")
    return packet


def _staged_revision_ids(
    attempt: TranslationWindowAttempt,
    source_plan: SourcePlanRecord,
    window: TranslationWindow,
) -> tuple[str, ...]:
    from .window_persistence import rehydrate_attempt_revision_links

    links = rehydrate_attempt_revision_links(attempt, source_plan, window.block_ids)
    by_block = {link.block_id: link.revision_id for link in links}
    if set(by_block) != set(window.block_ids):
        raise BlockContractError("window-revision-links")
    active = [
        db.session.get(
            TranslationTargetBlockActive,
            {"source_plan_id": source_plan.id, "block_id": block_id},
            populate_existing=True,
        )
        for block_id in window.block_ids
    ]
    if any(pointer is not None for pointer in active):
        if any(pointer is None for pointer in active) or any(
            pointer.revision_id != by_block[block_id]
            for block_id, pointer in zip(window.block_ids, active, strict=True)
            if pointer is not None
        ):
            raise BlockContractError("window-dependency-drift")
        for block_id, pointer in zip(window.block_ids, active, strict=True):
            alignment = db.session.get(
                TranslationTargetAlignmentActive,
                {"source_plan_id": source_plan.id, "block_id": block_id},
                populate_existing=True,
            )
            if (
                pointer is None
                or alignment is None
                or alignment.revision_id != pointer.revision_id
            ):
                raise BlockContractError("window-dependency-drift")
        return tuple(by_block[block_id] for block_id in window.block_ids)
    _fresh_dependency_fence(attempt.id, source_plan.id, window)
    return tuple(by_block[block_id] for block_id in window.block_ids)


def resume_window_attempt(
    attempt: TranslationWindowAttempt,
    *,
    operation: TranslationOperation,
    source_plan: SourcePlanRecord,
    window: TranslationWindow,
    verified_evidence: VerifiedWindowEvidence | None = None,
) -> tuple[str, ...]:
    """Reconcile a durable attempt without issuing any provider request.

    A process can crash after dispatch but before it durably records the exact
    provider result.  Retrying that external request would be non-idempotent,
    so a recovered dispatched attempt is explicitly terminally uncertain.
    """

    packet = _attempt_matches_window(
        attempt,
        operation=operation,
        source_plan=source_plan,
        window=window,
        verified_evidence=verified_evidence,
    )
    state = attempt.dispatch_state
    if state == "prepared":
        raise BlockContractError("window-resume-prepared")
    if state == "dispatched":
        raise BlockContractError("window-provider-outcome-unknown")
    if state == "response_received":
        response = rehydrate_window_provider_response(attempt)
        return _stage_window_response(
            attempt,
            operation=operation,
            source_plan=source_plan,
            window=window,
            packet=packet,
            response=response,
        )
    if state == "staged":
        return _staged_revision_ids(attempt, source_plan, window)
    if state == "staging_failed":
        response = rehydrate_window_provider_response(attempt)
        return _stage_window_response(
            attempt,
            operation=operation,
            source_plan=source_plan,
            window=window,
            packet=packet,
            response=response,
            expected_dispatch_state="staging_failed",
        )
    if state in {
        "provider_failed",
        "provider_outcome_uncertain",
        "normalization_failed",
        "fence_failed",
    }:
        raise BlockContractError(f"window-attempt-terminal-{state}")
    raise BlockContractError("window-attempt-state")


def reconcile_dispatched_window_attempt(
    attempt: TranslationWindowAttempt,
) -> None:
    """Record a lost-fence dispatch as uncertain without ever calling a provider."""

    persisted = _independent_session()
    try:
        current = persisted.get(TranslationWindowAttempt, attempt.id)
        if current is None:
            raise BlockContractError("window-attempt-uncommitted")
        rehydrate_window_attempt(current)
        if current.dispatch_state == "provider_outcome_uncertain":
            return
        if current.dispatch_state != "dispatched":
            raise BlockContractError("window-dispatch-not-reconcilable")
    finally:
        persisted.close()
    try:
        _transition_dispatch(
            attempt.id,
            expected_state="dispatched",
            next_state="provider_outcome_uncertain",
            event_type="provider_outcome_uncertain",
            payload={"code": "operation-fence-lost"},
            require_lost_fence=True,
        )
    except BlockContractError as exc:
        if exc.code == "window-dispatch-fence":
            raise BlockContractError("window-operation-fence-live") from exc
        raise


__all__ = [
    "ModelExecutionProfile",
    "admit_packet",
    "execute_window_attempt",
    "execution_profile_from_frozen_phase",
    "normalize_window_response",
    "prepare_and_commit_window_attempt",
    "reconcile_dispatched_window_attempt",
    "resolve_execution_profile",
    "resume_window_attempt",
]
