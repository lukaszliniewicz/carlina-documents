"""Cheap progress counters, separate from verification and operation stages.

Polling must not decrypt and rehydrate an entire anthology. These counters
summarise persisted active pointers with accepted revisions; they are not a
fresh integrity audit, a quality score, or proof that export is ready.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import and_, func, select

from ...extensions import db
from .models import (
    TranslationSourcePlan,
    TranslationSourcePlanActive,
    TranslationTargetBlockActive,
    TranslationTargetBlockRevision,
)


def translation_progress(project_id: str) -> dict[str, Any]:
    """Count committed targets for the current plan without reading source text."""
    plan = db.session.execute(
        select(
            TranslationSourcePlan.id,
            TranslationSourcePlan.block_count,
            TranslationSourcePlan.anchor_count,
        )
        .join(
            TranslationSourcePlanActive,
            and_(
                TranslationSourcePlanActive.source_plan_id == TranslationSourcePlan.id,
                TranslationSourcePlanActive.project_id == TranslationSourcePlan.project_id,
            ),
        )
        .where(TranslationSourcePlan.project_id == project_id)
    ).one_or_none()
    result: dict[str, Any] = {
        "contract": "translator-progress-v1",
        "basis": "persisted_active_targets",
        "integrity_revalidated": False,
    }
    if plan is None:
        return {**result, "state": "source_plan_not_ready"}
    completed = db.session.scalar(
        select(func.count())
        .select_from(TranslationTargetBlockActive)
        .join(
            TranslationTargetBlockRevision,
            and_(
                TranslationTargetBlockRevision.id == TranslationTargetBlockActive.revision_id,
                TranslationTargetBlockRevision.source_plan_id == TranslationTargetBlockActive.source_plan_id,
                TranslationTargetBlockRevision.block_id == TranslationTargetBlockActive.block_id,
            ),
        )
        .where(
            TranslationTargetBlockActive.source_plan_id == plan.id,
            TranslationTargetBlockRevision.validation_state == "valid",
        )
    ) or 0
    total = plan.block_count
    if completed > total or total < 0 or plan.anchor_count < 0:
        return {**result, "state": "inconsistent", "source_plan_id": plan.id}
    return {
        **result,
        "state": "available",
        "source_plan_id": plan.id,
        "total_blocks": total,
        "source_anchors": plan.anchor_count,
        "translated_blocks": completed,
        "remaining_blocks": total - completed,
        "translation_percent": round(100 * completed / total, 4) if total else None,
    }
