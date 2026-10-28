"""Durable document-block translation admission before queue publication."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy import select

from ...extensions import db
from ...models import User
from .block_continuation import (
    TRANSLATION_SCOPE_CONTRACT_VERSION,
    snapshot_protected_targets,
)
from .block_workflow_config import (
    freeze_block_workflow_config,
    persist_block_workflow_config,
    rehydrate_block_workflow_config,
)
from .models import (
    TranslationBlockWorkflowConfig,
    TranslationProject,
    TranslationSourcePlan,
)
from .operation_delivery import OperationPublisher
from .operations import (
    TranslationOperationActiveConflict,
    TranslationOperationIdempotencyConflict,
    create_operation,
)


def record_block_translation_job(
    project: TranslationProject,
    *,
    owner_user_id: int,
    source_plan: TranslationSourcePlan,
    execution_profile: Mapping[str, Any],
    request_payload: dict[str, Any],
    idempotency_key: str,
    context_mode: str,
    translation_reasoning: str | None = None,
    research_dossier: Mapping[str, Any] | None = None,
    publish: OperationPublisher,
) -> str:
    """Create and seal a block operation before publishing its delivery."""

    try:
        operation, created = create_operation(
            project,
            kind="document_block_translate",
            request_payload=request_payload,
            idempotency_key=idempotency_key,
            source_content_hash=source_plan.source_content_hash,
            source_plan_id=source_plan.id,
            source_plan_hash=source_plan.plan_hash,
            checkpoint=request_payload,
        )
        owner = db.session.get(User, owner_user_id)
        if owner is None:
            raise LookupError("Translation project owner not found.")
        if created:
            freeze_kwargs: dict[str, Any] = {
                "user": owner,
                "settings": project.settings or {},
                "window_profile": execution_profile["window_profile"],
                "protected_targets": snapshot_protected_targets(source_plan),
                "translation_scope": {
                    "contract_version": TRANSLATION_SCOPE_CONTRACT_VERSION,
                    "mode": "pending",
                    "block_ids": [],
                },
                "translation_reasoning": translation_reasoning,
                "executor_profile": execution_profile.get("executor_profile"),
                "context_mode": context_mode,
            }
            if research_dossier is not None:
                freeze_kwargs["research_dossier"] = research_dossier
            config = freeze_block_workflow_config(
                operation, source_plan, **freeze_kwargs
            )
            persist_block_workflow_config(operation, source_plan, config)
        else:
            row = db.session.execute(
                select(TranslationBlockWorkflowConfig).where(
                    TranslationBlockWorkflowConfig.operation_id == operation.id
                )
            ).scalar_one_or_none()
            if row is None:
                raise RuntimeError("The block workflow configuration is missing.")
            rehydrated_config = rehydrate_block_workflow_config(
                row, operation=operation, source_plan=source_plan
            )
            if research_dossier is not None and rehydrated_config.config_json.get(
                "research_dossier"
            ) != dict(research_dossier):
                raise RuntimeError(
                    "The existing block workflow configuration has a foreign research dossier."
                )
        db.session.commit()
    except (
        TranslationOperationActiveConflict,
        TranslationOperationIdempotencyConflict,
    ) as exc:
        raise RuntimeError(str(exc)) from exc
    return publish(
        project,
        operation,
        handler="translate_document_blocks",
        payload=request_payload,
    )
