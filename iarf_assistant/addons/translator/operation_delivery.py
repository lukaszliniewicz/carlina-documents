"""Durable Translator operation admission and queue delivery."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from ...extensions import db
from .models import TranslationOperation, TranslationProject
from .operations import (
    TranslationOperationActiveConflict,
    TranslationOperationIdempotencyConflict,
    TranslationOperationTransitionConflict,
    attach_queue_job,
    create_operation,
    finish_operation,
)


class OperationPublisher(Protocol):
    def __call__(
        self,
        project: TranslationProject,
        record: TranslationOperation,
        *,
        handler: str,
        payload: dict[str, Any],
    ) -> str: ...


def record_job(
    project: TranslationProject,
    *,
    handler: str,
    operation: str,
    payload: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
    source_plan_id: str | None = None,
    source_plan_hash: str | None = None,
    checkpoint: dict[str, Any] | None = None,
    publish: OperationPublisher,
) -> str:
    operation_payload = dict(payload or {})
    try:
        record, created = create_operation(
            project,
            kind=operation,
            request_payload=operation_payload,
            idempotency_key=idempotency_key,
            source_content_hash=project.source_content_hash,
            source_plan_id=source_plan_id,
            source_plan_hash=source_plan_hash,
            checkpoint=operation_payload if checkpoint is None else checkpoint,
        )
        db.session.commit()
    except (
        TranslationOperationActiveConflict,
        TranslationOperationIdempotencyConflict,
    ) as exc:
        raise RuntimeError(str(exc)) from exc
    # A process can die after committing the queued operation but before
    # publishing its delivery. Reuse that durable row on an exact retry.
    # Terminal or bound rows keep their identities and are not republished.
    if not created and (record.queue_job_id or record.state != "queued"):
        return str(record.queue_job_id or "")
    return publish(project, record, handler=handler, payload=operation_payload)


def deliver_operation(
    project: TranslationProject,
    record: TranslationOperation,
    *,
    owner_user_id: int,
    handler: str,
    payload: dict[str, Any],
    enqueue: Callable[[str, dict[str, Any]], str],
) -> str:
    """Publish one queued/unbound ledger row and bind its delivery identity."""

    if record.state != "queued" or record.queue_job_id:
        return str(record.queue_job_id or "")
    # The admitted ledger identity takes precedence over operation payload fields.
    job_payload = {
        **dict(payload),
        "project_id": project.id,
        "user_id": owner_user_id,
        "operation_id": record.id,
    }
    try:
        job_id = enqueue(handler, job_payload)
    except Exception as exc:
        db.session.rollback()
        failed = db.session.get(TranslationOperation, record.id)
        if failed is not None:
            finish_operation(
                failed,
                "failed",
                error_code="queue_publish_failed",
                error_detail=str(exc),
                message="The operation could not be queued. Retry it when the queue is available.",
            )
        db.session.commit()
        raise
    try:
        attach_queue_job(record, job_id)
        db.session.commit()
    except TranslationOperationTransitionConflict as exc:
        db.session.rollback()
        reloaded = db.session.get(TranslationOperation, record.id)
        if reloaded is None:
            raise RuntimeError("Translator operation lost its queue binding.") from exc
        if reloaded.queue_job_id:
            return str(reloaded.queue_job_id)
        raise RuntimeError("Translator operation lost its queue binding.") from exc
    return str(job_id)
