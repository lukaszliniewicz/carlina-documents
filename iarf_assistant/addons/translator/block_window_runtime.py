"""Crash-safe activation of staged document-block translation windows.

This module deliberately has no provider adapter.  A provider response is
first retained and staged by :mod:`window_execution`; only then can this
runtime turn the immutable staged revisions into active target/alignment pairs.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from time import perf_counter
from typing import Any, cast

from carlina.core.utc import utc_now_db

from ...database_utils import TransactionScopeError, run_transaction_with_retry
from ...extensions import db
from .alignment_contract import AlignmentGroup
from .alignment_persistence import (
    _anchors,
    _spans,
    persist_alignment_revision,
    persist_target_span_set,
    rehydrate_alignment_revision,
)
from .block_contract import (
    BlockContractError,
    TranslationSourcePlan,
    canonical_json,
    sha256_text,
)
from .block_notes import normalize_annotation_profile
from .block_persistence import rehydrate_source_plan
from .models import (
    TranslationAlignmentRevision,
    TranslationOperation,
    TranslationTargetAlignmentActive,
    TranslationTargetBlockActive,
    TranslationTargetBlockRevision,
    TranslationWindowAttempt,
    TranslationWindowAttemptRevision,
)
from .models import TranslationSourcePlan as SourcePlanRecord
from .sentence_alignment import SentenceAlignmentManifest
from .target_persistence import (
    activate_initial_target_revisions,
    activate_target_revision,
    lock_target_dependency_scope,
    rehydrate_target_revision,
    verify_active_pair,
)
from .window_contract import (
    LOCATED_SOURCE_WRITABLE_CONTRACT_VERSION,
    SENTENCE_ALIGNED_WRITABLE_SOURCE_CONTRACT_VERSION,
    SINGLE_COPY_WRITABLE_SOURCE_CONTRACT_VERSION,
    ProviderPacket,
    TranslationWindow,
)
from .window_execution import (
    _packet_target_language,
    _verify_dependency_fence,
    _writable_contract_version,
    normalize_window_response,
)
from .window_persistence import (
    VerifiedWindowEvidence,
    rehydrate_attempt_revision_links,
    rehydrate_window_attempt,
    rehydrate_window_provider_response,
)


@dataclass(frozen=True, slots=True)
class FinalizedWindowReceipt:
    """Bounded proof returned only after every pair is active and verified."""

    attempt_id: str
    source_plan_id: str
    block_ids: tuple[str, ...]
    revision_ids: tuple[str, ...]
    alignment_revision_ids: tuple[str, ...]
    receipt_hash: str


def _reject_caller_transaction(operation_name: str) -> None:
    """Fail before an owned boundary could discard caller-flushed writes."""

    active_session = db.session() if callable(db.session) else db.session
    if active_session.in_transaction():
        message = f"{operation_name} requires no active caller transaction"
        raise TransactionScopeError(message)


def _receipt(
    *,
    attempt_id: str,
    source_plan_id: str,
    block_ids: tuple[str, ...],
    revision_ids: tuple[str, ...],
    alignment_revision_ids: tuple[str, ...],
) -> FinalizedWindowReceipt:
    material = {
        "contract_version": "translator-window-finalization-v1",
        "attempt_id": attempt_id,
        "source_plan_id": source_plan_id,
        "block_ids": list(block_ids),
        "revision_ids": list(revision_ids),
        "alignment_revision_ids": list(alignment_revision_ids),
    }
    return FinalizedWindowReceipt(
        attempt_id=attempt_id,
        source_plan_id=source_plan_id,
        block_ids=block_ids,
        revision_ids=revision_ids,
        alignment_revision_ids=alignment_revision_ids,
        receipt_hash=sha256_text(canonical_json(material)),
    )


def _assert_attempt_scope(
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
    if (
        attempt.operation_id != operation.id
        or attempt.source_plan_id != source_plan.id
        or attempt.project_id != operation.project_id
        or attempt.owner_user_id != operation.owner_user_id
        or operation.source_content_hash != attempt.source_content_hash
        or operation.source_plan_id != attempt.source_plan_id
        or operation.source_plan_hash != attempt.source_plan_hash
    ):
        raise BlockContractError("window-finalize-owner")
    if (
        tuple(attempt.block_ids_json) != window.block_ids
        or tuple(attempt.block_fingerprints_json) != window.block_fingerprints
    ):
        raise BlockContractError("window-finalize-window")
    if attempt.dispatch_state != "staged":
        raise BlockContractError("window-finalize-state")
    return packet


def _ordered_links(
    attempt: TranslationWindowAttempt,
    source_plan: SourcePlanRecord,
    window: TranslationWindow,
) -> tuple[TranslationWindowAttemptRevision, ...]:
    links = rehydrate_attempt_revision_links(attempt, source_plan, window.block_ids)
    by_block = {link.block_id: link for link in links}
    if len(by_block) != len(window.block_ids) or set(by_block) != set(window.block_ids):
        raise BlockContractError("window-finalize-links")
    return tuple(by_block[block_id] for block_id in window.block_ids)


def _expected_versions(
    attempt: TranslationWindowAttempt,
    window: TranslationWindow,
) -> dict[str, int]:
    predecessors = attempt.predecessor_json
    if not isinstance(predecessors, Mapping) or set(predecessors) != set(
        window.block_ids
    ):
        raise BlockContractError("window-finalize-predecessor")
    result: dict[str, int] = {}
    for block_id in window.block_ids:
        item = predecessors[block_id]
        if not isinstance(item, Mapping):
            raise BlockContractError("window-finalize-predecessor")
        revision_id = item.get("revision_id")
        version = item.get("version")
        if revision_id is None:
            if version is not None:
                raise BlockContractError("window-finalize-predecessor")
            result[block_id] = 0
        elif (
            isinstance(version, int) and not isinstance(version, bool) and version >= 1
        ):
            result[block_id] = version
        else:
            raise BlockContractError("window-finalize-predecessor")
    return result


def _active_pair_for(
    source_plan: SourcePlanRecord,
    *,
    block_id: str,
) -> tuple[TranslationTargetBlockActive, TranslationTargetAlignmentActive] | None:
    pointer = db.session.get(
        TranslationTargetBlockActive,
        {"source_plan_id": source_plan.id, "block_id": block_id},
        populate_existing=True,
    )
    alignment = db.session.get(
        TranslationTargetAlignmentActive,
        {"source_plan_id": source_plan.id, "block_id": block_id},
        populate_existing=True,
    )
    if pointer is None and alignment is None:
        return None
    if pointer is None or alignment is None:
        raise BlockContractError("window-finalize-active-pair")
    verify_active_pair(pointer, alignment)
    return pointer, alignment


def _finalization_state(
    pair: tuple[
        TranslationTargetBlockActive,
        TranslationTargetAlignmentActive,
    ]
    | None,
    link: TranslationWindowAttemptRevision,
    predecessor: object,
) -> str:
    if not isinstance(predecessor, Mapping):
        raise BlockContractError("window-finalize-predecessor")
    if pair is None:
        if predecessor.get("revision_id") is not None:
            raise BlockContractError("window-finalize-predecessor-drift")
        return "predecessor"
    pointer, _alignment_pointer = pair
    if pointer.revision_id == link.revision_id:
        return "finalized"
    revision = pointer.revision
    if (
        revision is not None
        and pointer.revision_id == predecessor.get("revision_id")
        and pointer.version == predecessor.get("version")
        and revision.revision_record_hash == predecessor.get("revision_record_hash")
    ):
        return "predecessor"
    raise BlockContractError("window-finalize-predecessor-drift")


def _expected_alignment_method(
    packet: ProviderPacket,
) -> str:
    return (
        "provider-manifest-v1"
        if _writable_contract_version(packet)
        in {
            SENTENCE_ALIGNED_WRITABLE_SOURCE_CONTRACT_VERSION,
            SINGLE_COPY_WRITABLE_SOURCE_CONTRACT_VERSION,
            LOCATED_SOURCE_WRITABLE_CONTRACT_VERSION,
        }
        else "deterministic-count-v1"
    )


def _existing_receipt_or_none(
    source_plan: SourcePlanRecord,
    attempt: TranslationWindowAttempt,
    links: tuple[TranslationWindowAttemptRevision, ...],
    window: TranslationWindow,
    expected_method: str,
    *,
    verified_plan: TranslationSourcePlan,
) -> FinalizedWindowReceipt | None:
    """Recognize only a complete exact prior finalization, never a partial one."""

    predecessors = attempt.predecessor_json
    if not isinstance(predecessors, Mapping) or set(predecessors) != set(
        window.block_ids
    ):
        raise BlockContractError("window-finalize-predecessor")
    active: list[
        tuple[TranslationTargetBlockActive, TranslationTargetAlignmentActive] | None
    ] = [_active_pair_for(source_plan, block_id=link.block_id) for link in links]
    states = [
        _finalization_state(pair, link, predecessors[link.block_id])
        for link, pair in zip(links, active, strict=True)
    ]
    if all(state == "predecessor" for state in states):
        return None
    if any(state != "finalized" for state in states):
        raise BlockContractError("window-finalize-partial")
    alignment_ids: list[str] = []
    for link, pair in zip(links, active, strict=True):
        pointer, alignment_pointer = cast(
            tuple[TranslationTargetBlockActive, TranslationTargetAlignmentActive], pair
        )
        if pointer.revision_id != link.revision_id:
            raise BlockContractError("window-finalize-predecessor-drift")
        alignment_record = db.session.get(
            TranslationAlignmentRevision,
            alignment_pointer.alignment_revision_id,
            populate_existing=True,
        )
        if (
            alignment_record is None
            or alignment_record.revision_id != link.revision_id
            or alignment_record.method != expected_method
            or alignment_record.review_state != "needs_review"
        ):
            raise BlockContractError("window-finalize-active-pair")
        alignment = rehydrate_alignment_revision(
            alignment_record,
            source_plan,
            verified_plan=verified_plan,
        )
        # The returned groups are intentionally not transformed into sentence
        # memory here.  New model output starts as deterministic needs-review
        # alignment and needs a separate human approval path.
        del alignment
        alignment_ids.append(alignment_pointer.alignment_revision_id)
    return _receipt(
        attempt_id=links[0].attempt_id,
        source_plan_id=source_plan.id,
        block_ids=window.block_ids,
        revision_ids=tuple(link.revision_id for link in links),
        alignment_revision_ids=tuple(alignment_ids),
    )


def _require_live_operation(
    attempt: TranslationWindowAttempt,
    operation: TranslationOperation,
    source_plan: SourcePlanRecord,
    *,
    expected_version: int,
    expected_queue_job_id: str | None,
) -> None:
    if (
        expected_version < attempt.operation_version
        or expected_queue_job_id != attempt.queue_job_id
    ):
        raise BlockContractError("window-finalize-operation-fence")
    current = db.session.get(TranslationOperation, operation.id, populate_existing=True)
    now = utc_now_db()
    if (
        current is None
        or current.id != attempt.operation_id
        or current.project_id != attempt.project_id
        or current.owner_user_id != attempt.owner_user_id
        or current.state != "running"
        or current.version != expected_version
        or current.version < attempt.operation_version
        or current.queue_job_id != attempt.queue_job_id
        or current.cancel_requested_at is not None
        or current.lease_expires_at is None
        or current.lease_expires_at <= now
        or current.source_content_hash != attempt.source_content_hash
        or current.source_plan_id != source_plan.id
        or current.source_plan_hash != source_plan.plan_hash
    ):
        raise BlockContractError("window-finalize-operation-fence")


def _provider_manifests_for_attempt(
    attempt: TranslationWindowAttempt,
    source_plan: SourcePlanRecord,
    window: TranslationWindow,
    *,
    packet: ProviderPacket,
    links: tuple[TranslationWindowAttemptRevision, ...],
    verified_plan: TranslationSourcePlan,
) -> dict[str, SentenceAlignmentManifest]:
    """Re-normalize and bind the retained aligned response before activation."""

    if (
        _writable_contract_version(packet)
        not in {
            SENTENCE_ALIGNED_WRITABLE_SOURCE_CONTRACT_VERSION,
            SINGLE_COPY_WRITABLE_SOURCE_CONTRACT_VERSION,
            LOCATED_SOURCE_WRITABLE_CONTRACT_VERSION,
        }
    ):
        return {}
    writable = [slot for slot in packet.slots if slot.slot_type == "writable_source"]
    if len(writable) != 1:
        raise BlockContractError("window-finalize-v7-source")
    try:
        payload = json.loads(writable[0].content)
    except (TypeError, ValueError, RecursionError) as exc:
        raise BlockContractError("window-finalize-v7-source") from exc
    if not isinstance(payload, Mapping):
        raise BlockContractError("window-finalize-v7-source")
    profile = normalize_annotation_profile(payload.get("annotation_profile"))
    response = rehydrate_window_provider_response(attempt)
    normalized = normalize_window_response(
        response,
        plan=verified_plan,
        plan_id=source_plan.id,
        window=window,
        writable_source_contract_version=_writable_contract_version(packet),
        annotation_profile=profile,
        target_language=_packet_target_language(packet),
    )
    if not isinstance(normalized, tuple) or len(normalized) != 3:
        raise BlockContractError("window-finalize-v7-manifest")
    candidates, _notes, manifests = normalized
    if len(candidates) != len(links) or len(manifests) != len(window.block_ids):
        raise BlockContractError("window-finalize-v7-manifest")
    for block_id, candidate, link in zip(
        window.block_ids, candidates, links, strict=True
    ):
        revision = db.session.get(
            TranslationTargetBlockRevision,
            link.revision_id,
            populate_existing=True,
        )
        if (
            candidate.block_id != block_id
            or link.block_id != block_id
            or revision is None
            or revision.candidate_hash != link.candidate_hash
        ):
            raise BlockContractError("window-finalize-v7-candidate")
        persisted = rehydrate_target_revision(
            revision,
            source_plan,
            session=db.session,
            verified_plan=verified_plan,
        )
        if canonical_json(candidate.to_dict()) != canonical_json(persisted.to_dict()):
            raise BlockContractError("window-finalize-v7-candidate")
    by_block = {manifest.block_id: manifest for manifest in manifests}
    if set(by_block) != set(window.block_ids):
        raise BlockContractError("window-finalize-v7-manifest")
    return by_block


def _persist_alignment_dependencies(
    session: Any,
    source_plan: SourcePlanRecord,
    links: tuple[TranslationWindowAttemptRevision, ...],
    cancellation_boundary: Callable[[], None] | None,
    *,
    verified_plan: TranslationSourcePlan,
    manifests: Mapping[str, SentenceAlignmentManifest] | None = None,
    writable_source_contract_version: str = SENTENCE_ALIGNED_WRITABLE_SOURCE_CONTRACT_VERSION,
) -> tuple[str, ...]:
    alignment_ids: list[str] = []
    for link in links:
        if cancellation_boundary is not None:
            cancellation_boundary()
        revision = session.get(
            TranslationTargetBlockRevision,
            link.revision_id,
            populate_existing=True,
        )
        if (
            revision is None
            or revision.source_plan_id != source_plan.id
            or revision.block_id != link.block_id
            or revision.revision_record_hash != link.revision_record_hash
        ):
            raise BlockContractError("window-finalize-link")
        manifest = manifests.get(link.block_id) if manifests is not None else None
        span_set = persist_target_span_set(
            source_plan,
            revision,
            verified_plan=verified_plan,
            manifest=manifest,
        )
        if manifest is None:
            alignment = persist_alignment_revision(
                source_plan,
                span_set,
                method="deterministic-count-v1",
                review_state="needs_review",
                verified_plan=verified_plan,
            )
        else:
            anchors = _anchors(source_plan, link.block_id, session=session)
            spans = _spans(span_set, session=session)
            if len(anchors) == 0 or len(spans) != len(manifest.target_spans):
                raise BlockContractError("window-finalize-v7-manifest")
            groups = tuple(
                AlignmentGroup(
                    tuple(anchors[index - 1].id for index in group.source_ordinals),
                    tuple(spans[index - 1].id for index in group.target_ordinals),
                    "provider-manifest-v1",
                    0.5,
                    "needs_review",
                    {
                        "contract_version": writable_source_contract_version,
                        "source_ordinals": list(group.source_ordinals),
                        "target_ordinals": list(group.target_ordinals),
                    },
                )
                for group in manifest.groups
            )
            alignment = persist_alignment_revision(
                source_plan,
                span_set,
                groups=groups,
                method="provider-manifest-v1",
                review_state="needs_review",
                verified_plan=verified_plan,
            )
        alignment_ids.append(alignment.id)
    return tuple(alignment_ids)


def _activate_and_verify_pairs(
    source_plan: SourcePlanRecord,
    links: tuple[TranslationWindowAttemptRevision, ...],
    alignment_ids: tuple[str, ...],
    expected_versions: Mapping[str, int],
    cancellation_boundary: Callable[[], None] | None,
    *,
    verified_plan: TranslationSourcePlan,
) -> None:
    if all(expected_versions[link.block_id] == 0 for link in links):
        # Fresh translation is the common case. Seal all version-one pairs
        # using the existing atomic bulk path rather than taking one lock,
        # savepoint, flush and refresh cycle for every individual block.
        revisions = {
            row.id: row for row in db.session.query(TranslationTargetBlockRevision)
            .filter(TranslationTargetBlockRevision.id.in_(
                [link.revision_id for link in links]
            )).populate_existing().all()
        }
        alignments = {
            row.id: row for row in db.session.query(TranslationAlignmentRevision)
            .filter(TranslationAlignmentRevision.id.in_(alignment_ids))
            .populate_existing().all()
        }
        activations = []
        for link, alignment_id in zip(links, alignment_ids, strict=True):
            if cancellation_boundary is not None:
                cancellation_boundary()
            revision = revisions.get(link.revision_id)
            alignment = alignments.get(alignment_id)
            if (
                revision is None or alignment is None
                or revision.source_plan_id != source_plan.id
                or revision.block_id != link.block_id
                or alignment.source_plan_id != source_plan.id
                or alignment.block_id != link.block_id
                or alignment.revision_id != revision.id
            ):
                raise BlockContractError("window-finalize-link")
            activations.append((revision, alignment))
        # No verification cache crosses before_activation or the cancellation
        # callbacks: even a same-id target tamper must still be detected here.
        activate_initial_target_revisions(
            source_plan, activations, verified_plan=verified_plan,
        )
    else:
        for link, alignment_id in zip(links, alignment_ids, strict=True):
            if cancellation_boundary is not None:
                cancellation_boundary()
            activate_target_revision(
                source_plan,
                block_id=link.block_id,
                revision_id=link.revision_id,
                alignment_revision_id=alignment_id,
                expected_version=expected_versions[link.block_id],
                verified_plan=verified_plan,
            )
    db.session.flush()
    for link, alignment_id in zip(links, alignment_ids, strict=True):
        pair = _active_pair_for(source_plan, block_id=link.block_id)
        if (
            pair is None
            or pair[0].revision_id != link.revision_id
            or pair[1].alignment_revision_id != alignment_id
        ):
            raise BlockContractError("window-finalize-active-pair")


def finalize_staged_window_attempt(
    attempt: TranslationWindowAttempt,
    *,
    operation: TranslationOperation,
    source_plan: SourcePlanRecord,
    window: TranslationWindow,
    before_activation: Callable[[], None] | None = None,
    cancellation_boundary: Callable[[], None] | None = None,
    verified_evidence: VerifiedWindowEvidence | None = None,
) -> FinalizedWindowReceipt:
    """Atomically make every revision in one staged window active.

    This public boundary owns the transaction and commits only after every
    span/alignment/activation has succeeded and all pairs have been verified.
    The complete unit is replayed from a fresh transaction when SQLite reports
    a transient lock; all ORM rows are re-fetched for each replay so a rolled
    back attempt cannot leave stale state in the next write.
    """

    _reject_caller_transaction("translator_window_finalize")

    # Keep only immutable scalar identity/fence values outside the retry
    # callback.  A failed SQLite write rolls back and expires/discards ORM
    # state, so no caller-owned row may be consulted by a later replay.
    attempt_id = attempt.id
    operation_id = operation.id
    source_plan_id = source_plan.id
    expected_version = operation.version
    expected_queue_job_id = operation.queue_job_id
    if cancellation_boundary is not None:
        cancellation_boundary()

    started = perf_counter()
    timing: dict[str, object] = {"outcome": "failed"}
    retry_number = -1

    def finalize_transaction(_session: Any) -> FinalizedWindowReceipt:
        nonlocal retry_number
        retry_number += 1
        timing.clear()
        timing.update(retries=retry_number, outcome="failed")
        read_started = perf_counter()
        # The savepoint is entered before the reads intentionally: in SQLite
        # this establishes the deferred read snapshot that must be discarded
        # and rebuilt if the first write encounters SQLITE_BUSY_SNAPSHOT.
        with _session.begin_nested():
            _session.expire_all()
            current_attempt = _session.get(
                TranslationWindowAttempt, attempt_id, populate_existing=True
            )
            current_plan = _session.get(
                SourcePlanRecord, source_plan_id, populate_existing=True
            )
            current_operation = _session.get(
                TranslationOperation, operation_id, populate_existing=True
            )
            if (
                current_attempt is None
                or current_plan is None
                or current_operation is None
            ):
                raise BlockContractError("window-finalize-missing")
            packet = _assert_attempt_scope(
                current_attempt,
                operation=current_operation,
                source_plan=current_plan,
                window=window,
                verified_evidence=verified_evidence,
            )
            # The conductor has already fully verified this immutable source
            # during this invocation. Recheck its binding on every replay,
            # but do not decrypt the whole book again for each recovered window.
            verified_plan = (
                verified_evidence.plan_for(
                    current_plan, operation_id=current_operation.id,
                    project_id=current_operation.project_id,
                    owner_user_id=current_operation.owner_user_id,
                )
                if verified_evidence is not None
                else rehydrate_source_plan(current_plan)
            )
            links = _ordered_links(current_attempt, current_plan, window)
            expected_method = _expected_alignment_method(packet)
            existing = _existing_receipt_or_none(
                current_plan,
                current_attempt,
                links,
                window,
                expected_method,
                verified_plan=verified_plan,
            )
            if existing is not None:
                timing.update(
                    outcome="recovered", pre_lock_ms=(perf_counter() - read_started) * 1000,
                    lock_acquire_ms=0.0, write_work_ms=0.0,
                )
                return existing
            _require_live_operation(
                current_attempt,
                current_operation,
                current_plan,
                expected_version=expected_version,
                expected_queue_job_id=expected_queue_job_id,
            )
            # This read-only check remains inside the retry snapshot.
            manifests = _provider_manifests_for_attempt(
                current_attempt,
                current_plan,
                window,
                packet=packet,
                links=links,
                verified_plan=verified_plan,
            )
            # This lock is shared with staging and each paired target/alignment CAS.
            lock_started = perf_counter()
            timing["pre_lock_ms"] = (lock_started - read_started) * 1000
            lock_target_dependency_scope(current_plan.id)
            write_started = perf_counter()
            timing["lock_acquire_ms"] = (write_started - lock_started) * 1000
            # Recheck mutable operation state after the read-only preparation
            # and writer-lock acquisition, not merely before that interval.
            _require_live_operation(
                current_attempt, current_operation, current_plan,
                expected_version=expected_version,
                expected_queue_job_id=expected_queue_job_id,
            )
            _verify_dependency_fence(
                _session, current_attempt.id, current_plan.id, window,
                verified_plan=verified_plan,
            )
            expected_versions = _expected_versions(current_attempt, window)
            alignment_ids = _persist_alignment_dependencies(
                _session,
                current_plan,
                links,
                cancellation_boundary,
                verified_plan=verified_plan,
                manifests=manifests or None,
                writable_source_contract_version=_writable_contract_version(packet),
            )
            if before_activation is not None:
                before_activation()
            if cancellation_boundary is not None:
                cancellation_boundary()
            _activate_and_verify_pairs(
                current_plan,
                links,
                alignment_ids,
                expected_versions,
                cancellation_boundary,
                verified_plan=verified_plan,
            )
            timing["write_work_ms"] = (perf_counter() - write_started) * 1000
            timing["outcome"] = "finalized"
            return _receipt(
                attempt_id=current_attempt.id,
                source_plan_id=current_plan.id,
                block_ids=window.block_ids,
                revision_ids=tuple(link.revision_id for link in links),
                alignment_revision_ids=tuple(alignment_ids),
            )

    try:
        result = run_transaction_with_retry(
            db.session, finalize_transaction, operation_name="translator_window_finalize",
        )
    except BaseException:
        timing["outcome"] = "failed"
        raise
    else:
        return result
    finally:
        timing["total_ms"] = (perf_counter() - started) * 1000
        logging.getLogger(__name__).info(
            "Translator finalize timing attempt=%s window=%d blocks=%d metrics=%s",
            attempt_id, window.order, len(window.block_ids), timing,
        )


def recover_staged_window_attempt(
    *,
    operation: TranslationOperation,
    source_plan: SourcePlanRecord,
    window: TranslationWindow,
    expected_settings: Mapping[str, object],
    verified_evidence: VerifiedWindowEvidence | None = None,
) -> FinalizedWindowReceipt | None:
    """Recover the unique staged lineage before rebuilding mutable context.

    Finalization changes the active predecessor pointers used to construct a
    window packet.  Consequently, a conductor restart must discover a staged
    attempt by its immutable operation/plan/window/settings identity *before*
    it renders a fresh packet.  Otherwise an already activated window appears
    to be new work and can be sent to the provider twice.
    """

    _reject_caller_transaction("translator_window_recover")

    candidates = tuple(
        TranslationWindowAttempt.query.filter_by(
            operation_id=operation.id,
            source_plan_id=source_plan.id,
            window_order=window.order,
        ).all()
    )
    if not candidates:
        return None

    expected = dict(expected_settings)
    candidate_ids = {attempt.id for attempt in candidates}
    parent_ids: set[str] = set()
    for attempt in candidates:
        rehydrate_window_attempt(
            attempt, source_plan, verified_evidence=verified_evidence
        )
        if (
            attempt.project_id != operation.project_id
            or attempt.owner_user_id != operation.owner_user_id
            or tuple(attempt.block_ids_json) != window.block_ids
            or tuple(attempt.block_fingerprints_json) != window.block_fingerprints
            or dict(attempt.settings_json) != expected
        ):
            raise BlockContractError("window-recover-identity")
        lineage = tuple(
            item
            for item in (attempt.retry_of_attempt_id, attempt.rebase_of_attempt_id)
            if item is not None
        )
        if len(lineage) > 1 or any(item not in candidate_ids for item in lineage):
            raise BlockContractError("window-recover-lineage")
        parent_ids.update(lineage)

    roots = tuple(
        attempt
        for attempt in candidates
        if attempt.retry_of_attempt_id is None and attempt.rebase_of_attempt_id is None
    )
    tips = tuple(attempt for attempt in candidates if attempt.id not in parent_ids)
    if len(roots) != 1 or len(tips) != 1:
        raise BlockContractError("window-recover-lineage")

    selected = tips[0]
    if selected.dispatch_state != "staged":
        return None
    # Recovery's queries opened a read-only transaction.  End that owned
    # snapshot before entering the finalizer's independently replayable unit.
    db.session.rollback()
    return finalize_staged_window_attempt(
        selected,
        operation=operation,
        source_plan=source_plan,
        window=window,
        verified_evidence=verified_evidence,
    )


__all__ = [
    "FinalizedWindowReceipt",
    "finalize_staged_window_attempt",
    "recover_staged_window_attempt",
]
