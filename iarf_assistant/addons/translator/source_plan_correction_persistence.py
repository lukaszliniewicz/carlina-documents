"""Transactional active-plan corrections for the semantic block workspace."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any, cast

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from carlina.core.utc import utc_iso, utc_now_db

from ...extensions import db
from .alignment_persistence import (
    persist_alignment_revision,
    persist_target_span_set,
    rehydrate_alignment_revision,
)
from .block_contract import (
    BlockContractError,
    TranslationBlock,
    canonical_json,
    sha256_text,
)
from .block_persistence import (
    persist_source_plan,
    rehydrate_source_plan,
    require_active_source_plan,
)
from .integrity_receipts import make_receipt, verify_receipt
from .models import (
    TranslationAlignmentRevision,
    TranslationSourcePlanActive,
    TranslationSourcePlanCorrection,
    TranslationTargetAlignmentActive,
    TranslationTargetBlockActive,
    TranslationTargetBlockRevision,
)
from .models import TranslationSourcePlan as TranslationSourcePlanRecord
from .operations import (
    canonical_request_fingerprint,
    reconcile_expired_active_operation,
)
from .rehydration_cache import TranslationRehydrationCache
from .source_plan_corrections import (
    CORRECTION_SETTINGS_CONTRACT,
    SUPPORTED_CORRECTION_ACTIONS,
    build_successor_plan,
)
from .target_contract import TargetBlockCandidate, structural_fingerprint
from .target_persistence import (
    activate_initial_target_revisions,
    persist_target_revision,
    rehydrate_target_revision,
    verify_active_pair,
)

CORRECTION_RECEIPT_CONTRACT = "translation-source-plan-correction-v1"
_CORRECTION_RECEIPT_PURPOSE = "translator-source-plan-correction-receipt-v1"


class SourcePlanCorrectionError(RuntimeError):
    """A bounded correction failure safe for an API error code."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _block_semantic_identity(block: TranslationBlock) -> str:
    """Compare a rekeyed block without treating new plan identities as content."""

    return sha256_text(
        canonical_json(
            {
                "role": block.role,
                "locator": block.locator,
                "source_start": block.source_start,
                "source_end": block.source_end,
                "projection": block.projection,
                "projection_hash": block.projection_hash,
                "slots": [slot.to_dict() for slot in block.slots],
                "tokens": [token.to_dict() for token in block.protected_inline],
                "wire": [part.to_dict() for part in block.wire],
                "anchors": [
                    {
                        "order": anchor.order,
                        "start": anchor.start,
                        "end": anchor.end,
                        "source_hash": anchor.source_hash,
                        "splitter_profile": anchor.splitter_profile,
                        "splitter_version": anchor.splitter_version,
                    }
                    for anchor in block.anchors
                ],
            }
        )
    )


def _receipt_values(row: TranslationSourcePlanCorrection) -> dict[str, object]:
    return {
        "contract_version": row.contract_version,
        "id": row.id,
        "project_id": row.project_id,
        "owner_user_id": row.owner_user_id,
        "predecessor_source_plan_id": row.predecessor_source_plan_id,
        "successor_source_plan_id": row.successor_source_plan_id,
        "active_version": row.active_version,
        "action": row.action,
        "idempotency_key": row.idempotency_key,
        "request_fingerprint": row.request_fingerprint,
        "change": row.change_json,
        "target_reuse": row.target_reuse_json,
        "created_at": utc_iso(row.created_at, timespec="microseconds"),
    }


def _verify_correction(row: TranslationSourcePlanCorrection) -> None:
    if (
        row.contract_version != CORRECTION_RECEIPT_CONTRACT
        or row.action not in SUPPORTED_CORRECTION_ACTIONS
        or not isinstance(row.change_json, Mapping)
        or not isinstance(row.target_reuse_json, list)
    ):
        raise BlockContractError("source-plan-correction-corrupt")
    verify_receipt(
        stored=row.record_receipt,
        purpose=_CORRECTION_RECEIPT_PURPOSE,
        values=_receipt_values(row),
        code="source-plan-correction-corrupt",
    )


def _safe_result(row: TranslationSourcePlanCorrection) -> dict[str, object]:
    _verify_correction(row)
    change = cast(Mapping[str, object], row.change_json)
    return {
        "contract": CORRECTION_RECEIPT_CONTRACT,
        "correction_id": row.id,
        "action": row.action,
        "source_plan": {
            "id": row.successor_source_plan_id,
            "version": row.active_version,
        },
        "affected_block_count": int(change.get("affected_block_count", 0)),
        "reused_target_count": len(cast(list[object], row.target_reuse_json)),
    }


def _existing_correction(
    *, project_id: str, idempotency_key: str, request_fingerprint: str
) -> TranslationSourcePlanCorrection | None:
    row = TranslationSourcePlanCorrection.query.filter_by(
        project_id=project_id,
        idempotency_key=idempotency_key,
    ).one_or_none()
    if row is None:
        return None
    _verify_correction(row)
    if row.request_fingerprint != request_fingerprint:
        raise SourcePlanCorrectionError("source-plan-correction-idempotency-conflict")
    return row


def _carry_unchanged_targets(
    *,
    predecessor_record: TranslationSourcePlanRecord,
    successor_record: TranslationSourcePlanRecord,
    block_map: Mapping[str, str],
) -> list[dict[str, object]]:
    predecessor = rehydrate_source_plan(predecessor_record)
    successor = rehydrate_source_plan(successor_record)
    predecessor_blocks = {block.id: block for block in predecessor.blocks}
    successor_blocks = {block.id: block for block in successor.blocks}
    pointers = list(
        TranslationTargetBlockActive.query.populate_existing()
        .filter_by(source_plan_id=predecessor_record.id)
        .order_by(TranslationTargetBlockActive.block_id)
        .all()
    )
    pointer_block_ids = [pointer.block_id for pointer in pointers]
    alignment_pointers = (
        {
            pointer.block_id: pointer
            for pointer in TranslationTargetAlignmentActive.query.populate_existing()
            .filter(
                TranslationTargetAlignmentActive.source_plan_id
                == predecessor_record.id,
                TranslationTargetAlignmentActive.block_id.in_(pointer_block_ids),
            )
            .all()
        }
        if pointer_block_ids
        else {}
    )
    revisions = (
        {
            row.id: row
            for row in TranslationTargetBlockRevision.query.populate_existing()
            .filter(
                TranslationTargetBlockRevision.id.in_(
                    [pointer.revision_id for pointer in pointers]
                )
            )
            .all()
        }
        if pointers
        else {}
    )
    alignments = (
        {
            row.id: row
            for row in TranslationAlignmentRevision.query.populate_existing()
            .filter(
                TranslationAlignmentRevision.id.in_(
                    [
                        pointer.alignment_revision_id
                        for pointer in alignment_pointers.values()
                    ]
                )
            )
            .all()
        }
        if alignment_pointers
        else {}
    )
    verification_cache = TranslationRehydrationCache()
    activations: list[
        tuple[TranslationTargetBlockRevision, TranslationAlignmentRevision]
    ] = []
    reused: list[dict[str, object]] = []
    for pointer in pointers:
        successor_block_id = block_map.get(pointer.block_id)
        if successor_block_id is None:
            continue
        predecessor_block = predecessor_blocks.get(pointer.block_id)
        successor_block = successor_blocks.get(successor_block_id)
        if (
            predecessor_block is None
            or successor_block is None
            or _block_semantic_identity(predecessor_block)
            != _block_semantic_identity(successor_block)
        ):
            raise BlockContractError("source-plan-correction-reuse-identity")
        alignment_pointer = alignment_pointers.get(pointer.block_id)
        revision = revisions.get(pointer.revision_id)
        alignment = (
            None
            if alignment_pointer is None
            else alignments.get(alignment_pointer.alignment_revision_id)
        )
        if alignment_pointer is None or revision is None or alignment is None:
            raise BlockContractError("source-plan-correction-reuse-pair")
        verify_active_pair(
            pointer,
            alignment_pointer,
            revision=revision,
            alignment=alignment,
        )
        previous_candidate = rehydrate_target_revision(
            revision,
            predecessor_record,
            verified_plan=predecessor,
            verification_cache=verification_cache,
        )
        rehydrate_alignment_revision(
            alignment,
            predecessor_record,
            verified_plan=predecessor,
            verification_cache=verification_cache,
        )
        candidate = TargetBlockCandidate(
            source_plan_id=successor_record.id,
            source_plan_hash=successor.plan_hash,
            block_id=successor_block.id,
            structural_fingerprint=structural_fingerprint(successor, successor_block),
            parts=previous_candidate.parts,
            lexical_attributes=previous_candidate.lexical_attributes,
        )
        successor_revision = persist_target_revision(
            successor_record,
            candidate,
            target_language=revision.target_language,
            author_type="system",
            verified_plan=successor,
        )
        span_set = persist_target_span_set(
            successor_record,
            successor_revision,
            verified_plan=successor,
            verification_cache=verification_cache,
        )
        successor_alignment = persist_alignment_revision(
            successor_record,
            span_set,
            review_state="needs_review",
            verified_plan=successor,
            verification_cache=verification_cache,
        )
        activations.append((successor_revision, successor_alignment))
        reused.append(
            {
                "predecessor_block_id": predecessor_block.id,
                "successor_block_id": successor_block.id,
                "predecessor_revision_id": revision.id,
                "predecessor_revision_receipt": revision.revision_record_hash,
                "successor_revision_id": successor_revision.id,
                "successor_revision_receipt": successor_revision.revision_record_hash,
            }
        )
    activate_initial_target_revisions(
        successor_record,
        activations,
        verified_plan=successor,
        verification_cache=verification_cache,
    )
    return reused


def correct_active_source_plan(
    project: Any,
    *,
    block_id: str,
    action: str,
    expected_source_plan_id: str,
    expected_active_version: int,
    idempotency_key: str,
    owner_user_id: int,
) -> dict[str, object]:
    """Apply one owner correction and atomically advance the active plan."""

    request_material = {
        "project_id": str(project.id),
        "block_id": block_id,
        "action": action,
        "expected_source_plan_id": expected_source_plan_id,
        "expected_active_version": expected_active_version,
    }
    fingerprint = canonical_request_fingerprint(request_material)
    try:
        existing = _existing_correction(
            project_id=str(project.id),
            idempotency_key=idempotency_key,
            request_fingerprint=fingerprint,
        )
        if existing is not None:
            return _safe_result(existing)
        if action not in SUPPORTED_CORRECTION_ACTIONS:
            raise SourcePlanCorrectionError("source-plan-correction-action")
        if (
            not isinstance(expected_active_version, int)
            or isinstance(expected_active_version, bool)
            or expected_active_version < 1
        ):
            raise SourcePlanCorrectionError("source-plan-correction-version")
        active_operation = reconcile_expired_active_operation(project.id, owner_user_id)
        if active_operation is not None:
            raise SourcePlanCorrectionError("source-plan-correction-active-operation")
        pointer = require_active_source_plan(
            project.id,
            expected_source_plan_id,
            lock=True,
        )
        if pointer.version != expected_active_version:
            raise SourcePlanCorrectionError("source-plan-correction-conflict")
        predecessor_record = db.session.get(
            TranslationSourcePlanRecord, pointer.source_plan_id
        )
        if predecessor_record is None:
            raise BlockContractError("source-plan-active-missing")
        predecessor = rehydrate_source_plan(predecessor_record)
        result = build_successor_plan(
            predecessor,
            block_id=block_id,
            action=action,
        )
        successor_record, _created = persist_source_plan(project, result.plan)
        reused = _carry_unchanged_targets(
            predecessor_record=predecessor_record,
            successor_record=successor_record,
            block_map=result.unchanged_block_map,
        )
        next_version = pointer.version + 1
        now = utc_now_db()
        changed = db.session.execute(
            update(TranslationSourcePlanActive)
            .where(
                TranslationSourcePlanActive.project_id == project.id,
                TranslationSourcePlanActive.source_plan_id == predecessor_record.id,
                TranslationSourcePlanActive.version == pointer.version,
            )
            .values(
                source_plan_id=successor_record.id,
                version=next_version,
                updated_at=now,
            )
        )
        if getattr(changed, "rowcount", None) != 1:
            raise SourcePlanCorrectionError("source-plan-correction-conflict")
        change = {
            "settings_contract": CORRECTION_SETTINGS_CONTRACT,
            "predecessor_plan_hash": predecessor.plan_hash,
            "successor_plan_hash": result.plan.plan_hash,
            "predecessor_coverage_hash": predecessor.coverage.content_hash,
            "successor_coverage_hash": result.plan.coverage.content_hash,
            "affected_block_count": len(result.affected_block_ids),
            "affected_block_ids": list(result.affected_block_ids),
            "result_block_id": result.result_block_id,
            "block_map": dict(result.block_map),
        }
        row = cast(
            Any,
            TranslationSourcePlanCorrection(
                id=str(uuid.uuid4()),
                contract_version=CORRECTION_RECEIPT_CONTRACT,
                project_id=project.id,
                owner_user_id=owner_user_id,
                predecessor_source_plan_id=predecessor_record.id,
                successor_source_plan_id=successor_record.id,
                active_version=next_version,
                action=action,
                idempotency_key=idempotency_key,
                request_fingerprint=fingerprint,
                change_json=change,
                target_reuse_json=reused,
                record_receipt="0" * 64,
                created_at=now,
            ),
        )
        row.record_receipt = make_receipt(
            purpose=_CORRECTION_RECEIPT_PURPOSE,
            values=_receipt_values(row),
        )
        db.session.add(row)
        db.session.flush()
        _verify_correction(row)
        db.session.commit()
        return _safe_result(row)
    except SourcePlanCorrectionError:
        db.session.rollback()
        raise
    except BlockContractError as exc:
        db.session.rollback()
        raise SourcePlanCorrectionError(exc.code) from exc
    except IntegrityError as exc:
        db.session.rollback()
        existing = _existing_correction(
            project_id=str(project.id),
            idempotency_key=idempotency_key,
            request_fingerprint=fingerprint,
        )
        if existing is not None:
            return _safe_result(existing)
        raise SourcePlanCorrectionError("source-plan-correction-failed") from exc
    except Exception as exc:
        db.session.rollback()
        raise SourcePlanCorrectionError("source-plan-correction-failed") from exc


__all__ = [
    "CORRECTION_RECEIPT_CONTRACT",
    "SourcePlanCorrectionError",
    "correct_active_source_plan",
]
