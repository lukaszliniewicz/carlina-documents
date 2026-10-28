from __future__ import annotations

import logging
import mimetypes
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from flask import current_app, has_app_context
from sqlalchemy import func, or_

from iarf_assistant.core.agent_events import make_agent_event
from iarf_assistant.core.execution_scope import ExecutionScope
from iarf_assistant.core.runs.projections import runtime_checkpoint_summary
from carlina.core.utc import as_utc, utc_now_db
from iarf_assistant.database_utils import run_transaction_with_retry
from ..extensions import db
from ..models import (
    RuntimeApprovalRequest,
    RuntimeArtifact,
    RuntimeArtifactVersion,
    RuntimeEvidenceRecord,
    RuntimeJob,
    RuntimeResourceReference,
    RuntimeSourceRecord,
    WorkflowStep,
    WorkflowRun,
    Setting,
)


APPROVAL_STATUSES = {"pending", "approved", "rejected", "expired", "cancelled"}
ARTIFACT_REVIEW_STATUSES = {"draft", "pending_review", "approved", "rejected", "changes_requested", "archived"}
JOB_STATUSES = {"queued", "running", "waiting_for_approval", "completed", "failed", "cancelled", "paused"}
WORKFLOW_RUN_STATUSES = {"draft", "ready", "running", "waiting_for_approval", "paused", "completed", "failed", "cancelled"}
RUN_KINDS = {"ad_hoc", "template"}
WORKFLOW_STEP_STATUSES = {"planned", "ready", "queued", "running", "waiting_for_approval", "waiting_for_review", "paused", "completed", "failed", "cancelled", "skipped"}
RUNTIME_CHECKPOINT_STATUSES = {"pending", "due", "resolved", "cancelled"}
RUNTIME_CHECKPOINT_ACTIONS = {"record_due", "create_review_step", "pause_run"}
TERMINAL_JOB_STATUSES = {"completed", "failed", "cancelled"}
TERMINAL_WORKFLOW_STATUSES = {"completed", "failed", "cancelled"}
TERMINAL_WORKFLOW_STEP_STATUSES = {"completed", "failed", "cancelled", "skipped"}
PAUSABLE_WORKFLOW_STEP_STATUSES = {"planned", "ready", "queued"}
RESUMABLE_WORKFLOW_STEP_STATUSES = {"paused", "ready", "queued"}

MUTATING_RESOURCE_PERMISSIONS = {"write", "overwrite", "delete", "rename", "send", "publish", "admin"}
SCOPE_EXPANSION_FIELDS = (
    "capability_ids",
    "skill_ids",
    "workflow_ids",
    "tool_ids",
    "tool_view_ids",
    "resource_reference_ids",
    "runtime_profile_ids",
    "model_profile_ids",
)


def _utcnow() -> datetime:
    return utc_now_db()


def _json_object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _json_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, set):
        return sorted(value)
    return [value]


def _string_list(value: Any) -> list[str]:
    return [str(item) for item in _json_list(value) if item]


def _append_unique(existing: Any, values: Any) -> list[str]:
    result = _string_list(existing)
    seen = set(result)
    for value in _string_list(values):
        if value not in seen:
            result.append(value)
            seen.add(value)
    return result


def _merge_json_object(existing: Any, updates: Any, *, replace: bool = False) -> dict[str, Any]:
    update_payload = _json_object(updates)
    if replace:
        return update_payload
    return {**_json_object(existing), **update_payload}


def _link_workflow_run_ids(workflow_run_id: str | None, **updates: Any) -> WorkflowRun | None:
    if not workflow_run_id:
        return None
    record = db.session.get(WorkflowRun, workflow_run_id)
    if not record:
        return None
    for attr, values in updates.items():
        if not hasattr(record, attr):
            continue
        setattr(record, attr, _append_unique(getattr(record, attr), values))
    record.updated_at = _utcnow()
    return record


def _link_workflow_step_ids(step_id: str | None, **updates: Any) -> WorkflowStep | None:
    if not step_id:
        return None
    record = db.session.get(WorkflowStep, step_id)
    if not record:
        return None
    for attr, values in updates.items():
        if not hasattr(record, attr):
            continue
        setattr(record, attr, _append_unique(getattr(record, attr), values))
    record.updated_at = _utcnow()
    return record


def normalize_job_status(status: str | None) -> str:
    mapping = {
        "pending": "queued",
        "processing": "running",
        "started": "running",
        "success": "completed",
        "error": "failed",
        "canceled": "cancelled",
        "waiting": "waiting_for_approval",
    }
    normalized = mapping.get(str(status or "queued").lower(), str(status or "queued").lower())
    return normalized if normalized in JOB_STATUSES else "running"


def normalize_workflow_status(status: str | None) -> str:
    mapping = {
        "active": "running",
        "in_progress": "running",
        "processing": "running",
        "started": "running",
        "queued": "queued",
        "pending": "ready",
        "success": "completed",
        "error": "failed",
        "canceled": "cancelled",
        "awaiting_approval": "waiting_for_approval",
    }
    normalized = mapping.get(str(status or "draft").lower(), str(status or "draft").lower())
    return normalized if normalized in WORKFLOW_RUN_STATUSES else "draft"


def normalize_workflow_step_status(status: str | None) -> str:
    mapping = {
        "pending": "planned",
        "created": "planned",
        "queued": "queued",
        "active": "running",
        "in_progress": "running",
        "processing": "running",
        "started": "running",
        "awaiting_approval": "waiting_for_approval",
        "awaiting_review": "waiting_for_review",
        "success": "completed",
        "error": "failed",
        "canceled": "cancelled",
    }
    normalized = mapping.get(str(status or "planned").lower(), str(status or "planned").lower())
    return normalized if normalized in WORKFLOW_STEP_STATUSES else "planned"


def normalize_artifact_review_status(status: str | None) -> str:
    mapping = {
        "needs_review": "pending_review",
        "reviewing": "pending_review",
        "accepted": "approved",
        "declined": "rejected",
        "denied": "rejected",
        "change_requested": "changes_requested",
        "needs_changes": "changes_requested",
    }
    normalized = mapping.get(str(status or "draft").lower(), str(status or "draft").lower())
    if normalized not in ARTIFACT_REVIEW_STATUSES:
        raise ValueError(f"Invalid artifact review status: {status}")
    return normalized


def normalize_run_kind(kind: str | None, workflow_id: str | None = None) -> str:
    normalized = str(kind or ("template" if workflow_id else "ad_hoc")).lower()
    return normalized if normalized in RUN_KINDS else "template"


def normalize_permissions(permissions: Any = None) -> list[str]:
    normalized = [str(item).lower() for item in _json_list(permissions) if item]
    return normalized or ["read"]


def normalize_effects(effects: Any) -> list[str]:
    return [str(effect) for effect in _json_list(effects) if effect]


def _approval_is_expired(record: RuntimeApprovalRequest, now: datetime | None = None) -> bool:
    return bool(record.expires_at and record.expires_at <= (now or _utcnow()))


def create_approval_request(
    *,
    requester_user_id: int | None,
    subject_type: str,
    subject_id: str,
    effects: Any = None,
    preview: dict[str, Any] | None = None,
    approver_user_id: int | None = None,
    tool_id: str | None = None,
    workflow_run_id: str | None = None,
    chat_session_id: str | None = None,
    expires_at: datetime | None = None,
    audit_log_source_id: str | None = None,
    log_source_ids: Any = None,
    commit: bool = True,
) -> RuntimeApprovalRequest:
    record = RuntimeApprovalRequest(
        id=str(uuid.uuid4()),
        requester_user_id=requester_user_id,
        approver_user_id=approver_user_id,
        subject_type=subject_type,
        subject_id=subject_id,
        tool_id=tool_id,
        workflow_run_id=workflow_run_id,
        chat_session_id=chat_session_id,
        effects=normalize_effects(effects),
        preview_payload=_json_object(preview),
        status="pending",
        expires_at=expires_at,
        audit_log_source_id=audit_log_source_id,
        log_source_ids=_string_list(log_source_ids),
    )
    db.session.add(record)
    _link_workflow_run_ids(workflow_run_id, approval_ids=[record.id])
    if commit:
        db.session.commit()
    return record


def resolve_approval_request(
    approval_id: str,
    status: str,
    *,
    approver_user_id: int | None = None,
    note: str | None = None,
    commit: bool = True,
) -> RuntimeApprovalRequest:
    normalized = str(status or "").lower()
    if normalized not in APPROVAL_STATUSES - {"pending"}:
        raise ValueError(f"Invalid approval resolution status: {status}")

    record = db.session.get(RuntimeApprovalRequest, approval_id)
    if not record:
        raise KeyError(f"Approval request '{approval_id}' not found.")
    if record.status != "pending":
        raise ValueError(f"Approval request '{approval_id}' is already {record.status}.")
    if _approval_is_expired(record):
        record.status = "expired"
        record.resolved_at = _utcnow()
        if commit:
            db.session.commit()
        raise ValueError(f"Approval request '{approval_id}' is expired.")

    record.status = normalized
    record.approver_user_id = approver_user_id or record.approver_user_id
    record.resolved_at = _utcnow()
    record.resolution_note = note
    if commit:
        db.session.commit()
    return record


def get_usable_approval(approval_id: str | None, *, requester_user_id: int | None = None, tool_id: str | None = None) -> RuntimeApprovalRequest | None:
    if not approval_id:
        return None
    record = db.session.get(RuntimeApprovalRequest, approval_id)
    if not record:
        return None
    if record.status != "approved" or _approval_is_expired(record):
        return None
    if requester_user_id is not None and record.requester_user_id not in {None, requester_user_id}:
        return None
    if tool_id and record.tool_id not in {None, tool_id}:
        return None
    return record


def create_resource_reference(
    *,
    owner_user_id: int | None,
    resource_type: str,
    resource_uri: str,
    provider: str | None = None,
    label: str | None = None,
    permissions: Any = None,
    scopes: Any = None,
    metadata: dict[str, Any] | None = None,
    expires_at: datetime | None = None,
    source_addon_id: str | None = None,
    workflow_run_id: str | None = None,
    commit: bool = True,
) -> RuntimeResourceReference:
    record = RuntimeResourceReference(
        id=str(uuid.uuid4()),
        owner_user_id=owner_user_id,
        resource_type=str(resource_type),
        resource_uri=str(resource_uri),
        provider=provider,
        label=label,
        permissions=normalize_permissions(permissions),
        scopes=_string_list(scopes),
        metadata_json=_json_object(metadata),
        expires_at=expires_at,
        source_addon_id=source_addon_id,
        workflow_run_id=workflow_run_id,
    )
    db.session.add(record)
    _link_workflow_run_ids(workflow_run_id, resource_reference_ids=[record.id])
    if commit:
        db.session.commit()
    return record


def describe_resource_references(
    reference_ids: Any,
    *,
    owner_user_id: int | None = None,
) -> list[dict[str, Any]]:
    """Return compact, prompt-safe metadata for selected resource grants."""
    descriptions: list[dict[str, Any]] = []
    for reference_id in _string_list(reference_ids):
        record = db.session.get(RuntimeResourceReference, reference_id)
        if not record:
            descriptions.append({
                "id": reference_id,
                "available": False,
                "reason": "not_found",
            })
            continue
        if record.expires_at and record.expires_at <= _utcnow():
            descriptions.append({
                "id": reference_id,
                "available": False,
                "reason": "expired",
            })
            continue
        if owner_user_id is not None and record.owner_user_id not in {None, owner_user_id}:
            descriptions.append({
                "id": reference_id,
                "available": False,
                "reason": "not_visible",
            })
            continue
        descriptions.append({
            "id": record.id,
            "available": True,
            "resource_type": record.resource_type,
            "resource_uri": record.resource_uri,
            "provider": record.provider,
            "label": record.label,
            "permissions": normalize_permissions(record.permissions),
            "scopes": _string_list(record.scopes),
            "source_addon_id": record.source_addon_id,
            "workflow_run_id": record.workflow_run_id,
            "metadata": _json_object(record.metadata_json),
            "expires_at": record.expires_at.isoformat() if record.expires_at else None,
        })
    return descriptions


def create_scope_expansion_request(
    *,
    requester_user_id: int | None,
    current_scope: Any,
    requested_scope: dict[str, Any],
    requested_scope_original: dict[str, Any] | None = None,
    scope_normalization: dict[str, Any] | None = None,
    reason: str | None = None,
    chat_session_id: str | None = None,
    workflow_run_id: str | None = None,
    log_source_ids: Any = None,
    commit: bool = True,
) -> RuntimeApprovalRequest:
    current_scope_payload = ExecutionScope.from_dict(current_scope).to_dict()
    requested_scope_payload = {
        "capability_ids": _string_list(requested_scope.get("capability_ids")),
        "skill_ids": _string_list(requested_scope.get("skill_ids")),
        "workflow_ids": _string_list(requested_scope.get("workflow_ids")),
        "tool_ids": _string_list(requested_scope.get("tool_ids")),
        "tool_view_ids": _string_list(requested_scope.get("tool_view_ids")),
        "resource_reference_ids": _string_list(requested_scope.get("resource_reference_ids")),
        "runtime_profile_ids": _string_list(requested_scope.get("runtime_profile_ids")),
        "model_profile_ids": _string_list(requested_scope.get("model_profile_ids")),
    }
    if not any(requested_scope_payload.values()):
        raise ValueError("Scope expansion request must include at least one requested scope item.")

    preview = {
        "reason": str(reason or "").strip(),
        "current_scope": current_scope_payload,
        "requested_scope": requested_scope_payload,
        "chat_session_id": chat_session_id,
        "instructions": (
            "Approving this request authorizes the requested scope items. "
            "The chat UI can merge them into the next scoped turn selection."
        ),
    }
    if requested_scope_original is not None:
        preview["requested_scope_original"] = {
            key: _string_list(_json_object(requested_scope_original).get(key))
            for key in SCOPE_EXPANSION_FIELDS
        }
    if scope_normalization:
        preview["scope_normalization"] = _json_object(scope_normalization)
    return create_approval_request(
        requester_user_id=requester_user_id,
        subject_type="scope_expansion",
        subject_id=f"scope-expansion-{uuid.uuid4()}",
        effects=["scope_expansion"],
        preview=preview,
        workflow_run_id=workflow_run_id,
        chat_session_id=chat_session_id,
        log_source_ids=log_source_ids,
        commit=commit,
    )


def scope_expansion_payload_from_approval(record: RuntimeApprovalRequest | None) -> dict[str, Any] | None:
    if not record or record.subject_type != "scope_expansion":
        return None
    preview = _json_object(record.preview_payload)
    requested = _json_object(preview.get("requested_scope"))
    current_scope = _json_object(preview.get("current_scope"))
    payload = {
        "approval_id": record.id,
        "status": record.status,
        "reason": str(preview.get("reason") or ""),
        "chat_session_id": preview.get("chat_session_id"),
        "workflow_run_id": record.workflow_run_id,
        "current_scope": current_scope,
        "requested_scope": {
            key: _string_list(requested.get(key))
            for key in SCOPE_EXPANSION_FIELDS
        },
    }
    if isinstance(preview.get("requested_scope_original"), dict):
        original = _json_object(preview.get("requested_scope_original"))
        payload["requested_scope_original"] = {
            key: _string_list(original.get(key))
            for key in SCOPE_EXPANSION_FIELDS
        }
    if isinstance(preview.get("scope_normalization"), dict):
        payload["scope_normalization"] = _json_object(preview.get("scope_normalization"))
    return payload


def merge_scope_selection(base_selection: Any, requested_scope: Any) -> dict[str, Any]:
    base = dict(base_selection) if isinstance(base_selection, dict) else {}
    requested = _json_object(requested_scope)
    merged = {**base}
    for key in SCOPE_EXPANSION_FIELDS:
        merged[key] = _append_unique(base.get(key), requested.get(key))
    return merged


def approved_scope_expansions_for_chat(
    chat_session_id: str | None,
    *,
    requester_user_id: int | None = None,
    approval_records: list[RuntimeApprovalRequest]
    | tuple[RuntimeApprovalRequest, ...]
    | None = None,
) -> list[dict[str, Any]]:
    if not chat_session_id:
        return []
    if approval_records is None:
        query = RuntimeApprovalRequest.query.filter_by(
            subject_type="scope_expansion",
            status="approved",
        )
        if requester_user_id is not None:
            query = query.filter(
                RuntimeApprovalRequest.requester_user_id == requester_user_id
            )
        query = query.filter(
            or_(
                RuntimeApprovalRequest.chat_session_id == chat_session_id,
                RuntimeApprovalRequest.chat_session_id.is_(None),
            )
        )
        rows = query.order_by(RuntimeApprovalRequest.created_at.asc()).limit(200).all()
    else:
        rows = approval_records
    expansions: list[dict[str, Any]] = []
    for row in rows:
        payload = scope_expansion_payload_from_approval(row)
        if not payload or payload.get("chat_session_id") != chat_session_id:
            continue
        expansions.append(payload)
    return expansions


def merge_approved_scope_expansions(
    base_selection: Any,
    *,
    chat_session_id: str | None,
    requester_user_id: int | None = None,
    approval_records: list[RuntimeApprovalRequest]
    | tuple[RuntimeApprovalRequest, ...]
    | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    merged = dict(base_selection) if isinstance(base_selection, dict) else {}
    applied: list[dict[str, Any]] = []
    for expansion in approved_scope_expansions_for_chat(
        chat_session_id,
        requester_user_id=requester_user_id,
        approval_records=approval_records,
    ):
        merged = merge_scope_selection(merged, expansion.get("requested_scope"))
        applied.append(expansion)
    return merged, applied


def validate_resource_references(
    reference_ids: Any,
    *,
    owner_user_id: int | None,
    required_permissions: Any = None,
) -> tuple[bool, list[str], list[RuntimeResourceReference]]:
    references = []
    reasons = []
    required = set(normalize_permissions(required_permissions)) - {"read"}

    for reference_id in _string_list(reference_ids):
        record = db.session.get(RuntimeResourceReference, reference_id)
        if not record:
            reasons.append(f"Resource reference '{reference_id}' was not found.")
            continue
        if record.expires_at and record.expires_at <= _utcnow():
            reasons.append(f"Resource reference '{reference_id}' is expired.")
            continue
        if owner_user_id is not None and record.owner_user_id not in {None, owner_user_id}:
            reasons.append(f"Resource reference '{reference_id}' belongs to another user.")
            continue
        permissions = set(normalize_permissions(record.permissions))
        missing = sorted(required - permissions)
        if missing:
            reasons.append(f"Resource reference '{reference_id}' lacks permissions: {', '.join(missing)}")
            continue
        references.append(record)

    return not reasons, reasons, references


def upsert_runtime_job(
    *,
    job_id: str,
    source_type: str,
    status: str,
    source_id: str | None = None,
    source_addon_id: str | None = None,
    owner_user_id: int | None = None,
    task_name: str | None = None,
    workflow_run_id: str | None = None,
    progress: float | None = None,
    message: str | None = None,
    error: str | None = None,
    input_payload: dict[str, Any] | None = None,
    result_payload: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    log_source_ids: Any = None,
    commit: bool = True,
) -> RuntimeJob:
    now = _utcnow()
    normalized_status = normalize_job_status(status)
    record = db.session.get(RuntimeJob, job_id)

    if not record:
        record = RuntimeJob(
            id=job_id,
            source_type=source_type,
            source_id=source_id,
            source_addon_id=source_addon_id,
            owner_user_id=owner_user_id,
            task_name=task_name,
            workflow_run_id=workflow_run_id,
            status=normalized_status,
            queued_at=now,
        )
        db.session.add(record)
    elif owner_user_id is not None and owner_user_id != record.owner_user_id:
        raise ValueError("Runtime job owner is immutable.")

    record.source_type = source_type or record.source_type
    record.source_id = source_id if source_id is not None else record.source_id
    record.source_addon_id = source_addon_id if source_addon_id is not None else record.source_addon_id
    record.task_name = task_name if task_name is not None else record.task_name
    record.workflow_run_id = workflow_run_id if workflow_run_id is not None else record.workflow_run_id
    record.status = normalized_status
    record.progress = progress if progress is not None else record.progress
    record.message = message if message is not None else record.message
    record.error = error if error is not None else record.error
    record.input_payload = _json_object(input_payload) if input_payload is not None else record.input_payload
    record.result_payload = result_payload if result_payload is not None else record.result_payload
    record.metadata_json = {**_json_object(record.metadata_json), **_json_object(metadata)}
    record.log_source_ids = _string_list(log_source_ids) if log_source_ids is not None else record.log_source_ids
    record.updated_at = now
    _link_workflow_run_ids(workflow_run_id, job_ids=[record.id])

    if normalized_status == "running" and not record.started_at:
        record.started_at = now
    if normalized_status in TERMINAL_JOB_STATUSES and not record.completed_at:
        record.completed_at = now

    if commit:
        db.session.commit()
    return record


def mirror_task_queue_status(
    *,
    addon_id: str,
    job_id: str,
    task_name: str | None,
    status_data: dict[str, Any],
    input_payload: dict[str, Any] | None = None,
    owner_user_id: int | None = None,
) -> RuntimeJob | None:
    if not has_app_context():
        return None

    try:
        existing = db.session.get(RuntimeJob, job_id)
        if existing is not None and (
            existing.source_type != "task_queue"
            or existing.source_id != addon_id
            or existing.source_addon_id != addon_id
        ):
            raise ValueError("Runtime job source is immutable.")

        log_source_ids = ["jobs.runtime", f"job.{addon_id}"]
        status = status_data.get("status")
        metadata = {
            "source_status": status,
            "queue_addon_id": addon_id,
        }
        for key in (
            "heartbeat_at",
            "last_heartbeat_at",
            "service_status",
            "service_job_id",
            "service_stage",
            "service_progress",
            "service_heartbeat_at",
            "service_updated_at",
            "service_protocol_version",
            "progress_source",
        ):
            if key in status_data:
                metadata[key] = status_data.get(key)
        result_payload = status_data.get("result") if isinstance(status_data.get("result"), dict) else None
        return upsert_runtime_job(
            job_id=job_id,
            source_type="task_queue",
            source_id=addon_id,
            source_addon_id=addon_id,
            owner_user_id=owner_user_id,
            task_name=task_name or status_data.get("task_name"),
            status=status,
            progress=status_data.get("progress"),
            message=status_data.get("message"),
            error=status_data.get("error"),
            input_payload=input_payload,
            result_payload=result_payload,
            metadata=metadata,
            log_source_ids=log_source_ids,
        )
    except Exception as exc:
        db.session.rollback()
        logging.debug("Skipped runtime job mirror for task queue '%s': %s", addon_id, exc)
        return None


def create_artifact(
    *,
    artifact_type: str = "file",
    job_id: str | None = None,
    workflow_run_id: str | None = None,
    step_id: str | None = None,
    source_addon_id: str | None = None,
    created_by_user_id: int | None = None,
    uri: str | None = None,
    path: str | None = None,
    remote_reference: str | None = None,
    mime_type: str | None = None,
    content_hash: str | None = None,
    size_bytes: int | None = None,
    label: str | None = None,
    source_ids: Any = None,
    evidence_ids: Any = None,
    metadata: dict[str, Any] | None = None,
    change_summary: str | None = None,
    review_status: str = "draft",
    retention_policy: str = "default",
    download_permission: str = "owner",
    commit: bool = True,
) -> RuntimeArtifact:
    run = db.session.get(WorkflowRun, workflow_run_id) if workflow_run_id else None
    if workflow_run_id and not run:
        raise KeyError(f"Workflow run '{workflow_run_id}' not found.")
    if step_id:
        step = db.session.get(WorkflowStep, step_id)
        if not step:
            raise KeyError(f"Workflow step '{step_id}' not found.")
        if workflow_run_id and step.workflow_run_id != workflow_run_id:
            raise ValueError("Artifact step_id must belong to the same run.")
        workflow_run_id = workflow_run_id or step.workflow_run_id
        run = run or db.session.get(WorkflowRun, workflow_run_id)

    owner_id = created_by_user_id if created_by_user_id is not None else (run.created_by_user_id if run else None)
    now = _utcnow()
    record = RuntimeArtifact(
        id=str(uuid.uuid4()),
        artifact_type=artifact_type,
        job_id=job_id,
        workflow_run_id=workflow_run_id,
        source_addon_id=source_addon_id,
        created_by_user_id=owner_id,
        uri=uri,
        path=path,
        remote_reference=remote_reference,
        mime_type=mime_type,
        content_hash=content_hash,
        size_bytes=size_bytes,
        label=label,
        metadata_json=_json_object(metadata),
        created_at=now,
        updated_at=now,
        review_status=normalize_artifact_review_status(review_status),
        retention_policy=retention_policy or "default",
        download_permission=download_permission or "owner",
    )
    db.session.add(record)
    version = _create_artifact_version_record(
        record,
        version_number=1,
        job_id=job_id,
        workflow_run_id=workflow_run_id,
        step_id=step_id,
        source_addon_id=source_addon_id,
        created_by_user_id=owner_id,
        artifact_type=artifact_type,
        uri=uri,
        path=path,
        remote_reference=remote_reference,
        mime_type=mime_type,
        content_hash=content_hash,
        size_bytes=size_bytes,
        label=label,
        source_ids=source_ids,
        evidence_ids=evidence_ids,
        metadata=metadata,
        change_summary=change_summary or "Initial artifact version.",
        created_at=now,
    )
    record.current_version_id = version.id
    _link_workflow_run_ids(workflow_run_id, artifact_ids=[record.id])
    _link_workflow_step_ids(step_id, artifact_ids=[record.id])
    if commit:
        db.session.commit()
    return record


def _create_artifact_version_record(
    artifact: RuntimeArtifact,
    *,
    version_number: int,
    job_id: str | None = None,
    workflow_run_id: str | None = None,
    step_id: str | None = None,
    source_addon_id: str | None = None,
    created_by_user_id: int | None = None,
    artifact_type: str | None = None,
    uri: str | None = None,
    path: str | None = None,
    remote_reference: str | None = None,
    mime_type: str | None = None,
    content_hash: str | None = None,
    size_bytes: int | None = None,
    label: str | None = None,
    source_ids: Any = None,
    evidence_ids: Any = None,
    metadata: dict[str, Any] | None = None,
    change_summary: str | None = None,
    created_at: datetime | None = None,
) -> RuntimeArtifactVersion:
    record = RuntimeArtifactVersion(
        id=str(uuid.uuid4()),
        artifact_id=artifact.id,
        version_number=version_number,
        job_id=job_id,
        workflow_run_id=workflow_run_id,
        step_id=step_id,
        source_addon_id=source_addon_id,
        created_by_user_id=created_by_user_id,
        artifact_type=artifact_type or artifact.artifact_type,
        uri=uri,
        path=path,
        remote_reference=remote_reference,
        mime_type=mime_type,
        content_hash=content_hash,
        size_bytes=size_bytes,
        label=label,
        source_ids=_string_list(source_ids),
        evidence_ids=_string_list(evidence_ids),
        metadata_json=_json_object(metadata),
        change_summary=change_summary,
        created_at=created_at or _utcnow(),
    )
    db.session.add(record)
    return record


def create_artifact_version(
    artifact_id: str,
    *,
    job_id: str | None = None,
    workflow_run_id: str | None = None,
    step_id: str | None = None,
    source_addon_id: str | None = None,
    created_by_user_id: int | None = None,
    artifact_type: str | None = None,
    uri: str | None = None,
    path: str | None = None,
    remote_reference: str | None = None,
    mime_type: str | None = None,
    content_hash: str | None = None,
    size_bytes: int | None = None,
    label: str | None = None,
    source_ids: Any = None,
    evidence_ids: Any = None,
    metadata: dict[str, Any] | None = None,
    change_summary: str | None = None,
    make_current: bool = True,
    reset_review_status: bool = True,
    commit: bool = True,
) -> RuntimeArtifactVersion:
    artifact = db.session.get(RuntimeArtifact, artifact_id)
    if not artifact:
        raise KeyError(f"Runtime artifact '{artifact_id}' not found.")

    target_run_id = workflow_run_id or artifact.workflow_run_id
    run = db.session.get(WorkflowRun, target_run_id) if target_run_id else None
    if target_run_id and not run:
        raise KeyError(f"Workflow run '{target_run_id}' not found.")
    if artifact.workflow_run_id and target_run_id and artifact.workflow_run_id != target_run_id:
        raise ValueError("Artifact version workflow_run_id must belong to the artifact run.")
    if step_id:
        step = db.session.get(WorkflowStep, step_id)
        if not step:
            raise KeyError(f"Workflow step '{step_id}' not found.")
        if not target_run_id:
            target_run_id = step.workflow_run_id
            run = db.session.get(WorkflowRun, target_run_id)
        elif step.workflow_run_id != target_run_id:
            raise ValueError("Artifact version step_id must belong to the same run.")

    max_version = db.session.query(func.max(RuntimeArtifactVersion.version_number)).filter_by(artifact_id=artifact.id).scalar() or 0
    owner_id = created_by_user_id if created_by_user_id is not None else artifact.created_by_user_id
    version = _create_artifact_version_record(
        artifact,
        version_number=int(max_version) + 1,
        job_id=job_id if job_id is not None else artifact.job_id,
        workflow_run_id=target_run_id,
        step_id=step_id,
        source_addon_id=source_addon_id if source_addon_id is not None else artifact.source_addon_id,
        created_by_user_id=owner_id,
        artifact_type=artifact_type if artifact_type is not None else artifact.artifact_type,
        uri=uri if uri is not None else artifact.uri,
        path=path if path is not None else artifact.path,
        remote_reference=remote_reference if remote_reference is not None else artifact.remote_reference,
        mime_type=mime_type if mime_type is not None else artifact.mime_type,
        content_hash=content_hash if content_hash is not None else artifact.content_hash,
        size_bytes=size_bytes if size_bytes is not None else artifact.size_bytes,
        label=label if label is not None else artifact.label,
        source_ids=source_ids,
        evidence_ids=evidence_ids,
        metadata=metadata if metadata is not None else artifact.metadata_json,
        change_summary=change_summary,
    )

    if make_current:
        artifact.current_version_id = version.id
        artifact.job_id = version.job_id
        artifact.workflow_run_id = version.workflow_run_id
        artifact.source_addon_id = version.source_addon_id
        artifact.artifact_type = version.artifact_type
        artifact.uri = version.uri
        artifact.path = version.path
        artifact.remote_reference = version.remote_reference
        artifact.mime_type = version.mime_type
        artifact.content_hash = version.content_hash
        artifact.size_bytes = version.size_bytes
        artifact.label = version.label
        artifact.metadata_json = _json_object(version.metadata_json)
        artifact.updated_at = _utcnow()
        if reset_review_status:
            artifact.review_status = "draft"
            artifact.reviewed_by_user_id = None
            artifact.reviewed_at = None
            artifact.review_note = None
        _link_workflow_run_ids(version.workflow_run_id, artifact_ids=[artifact.id])
        _link_workflow_step_ids(step_id, artifact_ids=[artifact.id])

    if commit:
        db.session.commit()
    return version


def update_artifact_review_status(
    artifact_id: str,
    review_status: str,
    *,
    reviewed_by_user_id: int | None = None,
    review_note: str | None = None,
    commit: bool = True,
) -> RuntimeArtifact:
    artifact = db.session.get(RuntimeArtifact, artifact_id)
    if not artifact:
        raise KeyError(f"Runtime artifact '{artifact_id}' not found.")
    normalized = normalize_artifact_review_status(review_status)
    artifact.review_status = normalized
    artifact.review_note = review_note
    artifact.updated_at = _utcnow()
    if normalized in {"approved", "rejected", "changes_requested", "archived"}:
        artifact.reviewed_by_user_id = reviewed_by_user_id
        artifact.reviewed_at = artifact.updated_at
    elif normalized in {"draft", "pending_review"}:
        artifact.reviewed_by_user_id = None
        artifact.reviewed_at = None
    if commit:
        db.session.commit()
    return artifact


def create_local_file_artifact(
    *,
    instance_path: str | Path,
    file_path: str | Path,
    job_id: str | None = None,
    workflow_run_id: str | None = None,
    step_id: str | None = None,
    source_addon_id: str | None = None,
    created_by_user_id: int | None = None,
    label: str | None = None,
    content_hash: str | None = None,
    source_ids: Any = None,
    evidence_ids: Any = None,
    metadata: dict[str, Any] | None = None,
    change_summary: str | None = None,
    review_status: str = "draft",
    retention_policy: str = "default",
    download_permission: str = "owner",
    commit: bool = True,
) -> RuntimeArtifact:
    root = Path(instance_path).resolve()
    resolved = Path(file_path).resolve()
    resolved.relative_to(root)
    relative_path = resolved.relative_to(root).as_posix()
    mime_type, _ = mimetypes.guess_type(str(resolved))
    return create_artifact(
        artifact_type="file",
        job_id=job_id,
        workflow_run_id=workflow_run_id,
        step_id=step_id,
        source_addon_id=source_addon_id,
        created_by_user_id=created_by_user_id,
        uri=f"instance://{relative_path}",
        path=relative_path,
        mime_type=mime_type or "application/octet-stream",
        content_hash=content_hash,
        size_bytes=resolved.stat().st_size if resolved.exists() else None,
        label=label or resolved.name,
        source_ids=source_ids,
        evidence_ids=evidence_ids,
        metadata=metadata,
        change_summary=change_summary,
        review_status=review_status,
        retention_policy=retention_policy,
        download_permission=download_permission,
        commit=commit,
    )


def resolve_artifact_path(artifact: RuntimeArtifact, *, instance_path: str | Path) -> Path | None:
    uri = artifact.uri or ""
    if not uri.startswith("instance://"):
        return None
    relative = uri[len("instance://"):].lstrip("/\\")
    root = Path(instance_path).resolve()
    candidate = (root / relative).resolve()
    candidate.relative_to(root)
    return candidate


def read_text_artifact_content(
    artifact: RuntimeArtifact,
    *,
    instance_path: str | Path,
    max_chars: int = 20000,
) -> dict[str, Any]:
    """Read a local text-like artifact for model/UI preview without bypassing path safety."""
    try:
        path = resolve_artifact_path(artifact, instance_path=instance_path)
    except ValueError:
        return {"available": False, "reason": "Artifact path is not safe."}

    if not path or not path.exists() or not path.is_file():
        return {"available": False, "reason": "Artifact file is not available."}

    mime_type = (artifact.mime_type or "").lower()
    text_mimes = {
        "application/json",
        "application/ld+json",
        "application/markdown",
        "application/xml",
        "application/yaml",
        "application/x-yaml",
        "application/javascript",
        "application/x-ndjson",
    }
    text_suffixes = {
        ".csv",
        ".json",
        ".log",
        ".md",
        ".markdown",
        ".txt",
        ".xml",
        ".yaml",
        ".yml",
    }
    if not (mime_type.startswith("text/") or mime_type in text_mimes or path.suffix.lower() in text_suffixes):
        return {"available": False, "reason": "Artifact is not a text artifact."}

    limit = max(0, min(int(max_chars or 0), 100000))
    text = path.read_text(encoding="utf-8", errors="replace")
    return {
        "available": True,
        "content": text[:limit],
        "truncated": len(text) > limit,
        "size_chars": len(text),
        "max_chars": limit,
    }


def create_workflow_run(
    *,
    title: str | None = None,
    workflow_id: str | None = None,
    kind: str | None = None,
    source_addon_id: str | None = None,
    status: str = "draft",
    input_payload: dict[str, Any] | None = None,
    understanding: dict[str, Any] | None = None,
    plan: dict[str, Any] | None = None,
    state: dict[str, Any] | None = None,
    current_step: str | None = None,
    created_by_user_id: int | None = None,
    chat_session_id: str | None = None,
    approval_ids: Any = None,
    resource_reference_ids: Any = None,
    job_ids: Any = None,
    step_ids: Any = None,
    source_ids: Any = None,
    evidence_ids: Any = None,
    artifact_ids: Any = None,
    log_source_ids: Any = None,
    resume_url: str | None = None,
    cancel_url: str | None = None,
    metadata: dict[str, Any] | None = None,
    commit: bool = True,
) -> WorkflowRun:
    run_kind = normalize_run_kind(kind, workflow_id)
    if run_kind == "template" and not workflow_id:
        raise ValueError("Template workflow runs require workflow_id.")
    state_payload = _json_object(state)
    record_id = str(uuid.uuid4())
    record_values = {
        "id": record_id,
        "title": _workflow_run_title(
            title,
            workflow_id=workflow_id,
            input_payload=input_payload,
            metadata=metadata,
        ),
        "kind": run_kind,
        "workflow_id": workflow_id,
        "source_addon_id": source_addon_id,
        "status": normalize_workflow_status(status),
        "input_payload": _json_object(input_payload),
        "understanding_json": _json_object(
            understanding
            if understanding is not None
            else state_payload.get("understanding")
        ),
        "plan_json": _json_object(
            plan if plan is not None else state_payload.get("plan")
        ),
        "state_json": state_payload,
        "current_step": current_step,
        "created_by_user_id": created_by_user_id,
        "chat_session_id": chat_session_id,
        "approval_ids": _string_list(approval_ids),
        "resource_reference_ids": _string_list(resource_reference_ids),
        "job_ids": _string_list(job_ids),
        "step_ids": _string_list(step_ids),
        "source_ids": _string_list(source_ids),
        "evidence_ids": _string_list(evidence_ids),
        "artifact_ids": _string_list(artifact_ids),
        "log_source_ids": _string_list(log_source_ids),
        "resume_url": resume_url,
        "cancel_url": cancel_url,
        "metadata_json": _json_object(metadata),
    }

    if not commit:
        record = WorkflowRun(**record_values)
        db.session.add(record)
        return record

    def persist_workflow_run(_session) -> WorkflowRun:
        existing = db.session.get(WorkflowRun, record_id)
        if existing is not None:
            return existing
        record = WorkflowRun(**record_values)
        db.session.add(record)
        return record

    return run_transaction_with_retry(
        db.session,
        persist_workflow_run,
        operation_name="create_workflow_run",
    )


def _workflow_run_title(
    title: str | None,
    *,
    workflow_id: str | None = None,
    input_payload: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> str | None:
    candidates = [title]
    metadata_payload = _json_object(metadata)
    candidates.extend(metadata_payload.get(key) for key in ("title", "name", "label"))
    input_data = _json_object(input_payload)
    candidates.extend(input_data.get(key) for key in ("title", "name", "task", "user_request", "request", "prompt"))
    if workflow_id:
        candidates.append(workflow_id.replace("_", " ").replace(".", " / ").title())

    for candidate in candidates:
        if not candidate:
            continue
        normalized = " ".join(str(candidate).split())
        if normalized:
            return normalized[:252] + "..." if len(normalized) > 255 else normalized
    return None


def _workflow_step_for_reference(workflow_run_id: str, reference: Any) -> WorkflowStep | None:
    if not reference:
        return None
    reference_text = str(reference)
    record = db.session.get(WorkflowStep, reference_text)
    if record:
        if record.workflow_run_id != workflow_run_id:
            raise ValueError("Dependency workflow steps must belong to the same run.")
        return record
    return WorkflowStep.query.filter_by(workflow_run_id=workflow_run_id, step_key=reference_text).order_by(WorkflowStep.created_at.asc()).first()


def set_workflow_run_status(workflow_run_id: str, status: str, *, current_step: str | None = None, commit: bool = True) -> WorkflowRun:
    record = db.session.get(WorkflowRun, workflow_run_id)
    if not record:
        raise KeyError(f"Workflow run '{workflow_run_id}' not found.")
    record.status = normalize_workflow_status(status)
    record.current_step = current_step if current_step is not None else record.current_step
    record.updated_at = _utcnow()
    if record.status in TERMINAL_WORKFLOW_STATUSES and not record.completed_at:
        record.completed_at = record.updated_at
    if commit:
        db.session.commit()
    return record


def pause_workflow_run(workflow_run_id: str, *, commit: bool = True) -> tuple[WorkflowRun, dict[str, int]]:
    record = db.session.get(WorkflowRun, workflow_run_id)
    if not record:
        raise KeyError(f"Workflow run '{workflow_run_id}' not found.")

    now = _utcnow()
    summary = {"paused_steps": 0, "running_steps": 0, "waiting_steps": 0}
    steps = WorkflowStep.query.filter_by(workflow_run_id=workflow_run_id).all()
    for step in steps:
        status = str(step.status or "").lower()
        if status in PAUSABLE_WORKFLOW_STEP_STATUSES:
            step.status = "paused"
            step.updated_at = now
            summary["paused_steps"] += 1
        elif status == "running":
            summary["running_steps"] += 1
        elif status in {"waiting_for_approval", "waiting_for_review"}:
            summary["waiting_steps"] += 1

    record.status = "paused"
    record.updated_at = now
    if commit:
        db.session.commit()
    return record, summary


def resume_workflow_run(workflow_run_id: str, *, commit: bool = True) -> tuple[WorkflowRun, dict[str, int]]:
    record = db.session.get(WorkflowRun, workflow_run_id)
    if not record:
        raise KeyError(f"Workflow run '{workflow_run_id}' not found.")

    now = _utcnow()
    summary = {"reopened_steps": 0, "running_steps": 0, "waiting_steps": 0}
    steps = WorkflowStep.query.filter_by(workflow_run_id=workflow_run_id).all()
    for step in steps:
        status = str(step.status or "").lower()
        if status in RESUMABLE_WORKFLOW_STEP_STATUSES:
            step.status = "planned"
            step.updated_at = now
            summary["reopened_steps"] += 1
        elif status == "running":
            summary["running_steps"] += 1
        elif status in {"waiting_for_approval", "waiting_for_review"}:
            summary["waiting_steps"] += 1

    record.status = "running"
    record.updated_at = now
    if commit:
        db.session.commit()
    return record, summary


def update_workflow_run_planning(
    workflow_run_id: str,
    *,
    title: str | None = None,
    understanding: dict[str, Any] | None = None,
    plan: dict[str, Any] | None = None,
    state: dict[str, Any] | None = None,
    status: str | None = None,
    current_step: str | None = None,
    metadata: dict[str, Any] | None = None,
    merge: bool = True,
    commit: bool = True,
) -> WorkflowRun:
    record = db.session.get(WorkflowRun, workflow_run_id)
    if not record:
        raise KeyError(f"Workflow run '{workflow_run_id}' not found.")

    state_payload = dict(_json_object(record.state_json))
    replace = not merge

    if state is not None:
        state_payload = _merge_json_object(state_payload, state, replace=replace)
    if understanding is not None:
        record.understanding_json = _merge_json_object(record.understanding_json, understanding, replace=replace)
        state_payload["understanding"] = record.understanding_json
    if plan is not None:
        record.plan_json = _merge_json_object(record.plan_json, plan, replace=replace)
        state_payload["plan"] = record.plan_json
    if metadata is not None:
        record.metadata_json = _merge_json_object(record.metadata_json, metadata, replace=False)
    if title is not None:
        record.title = _workflow_run_title(title, workflow_id=record.workflow_id, input_payload=record.input_payload, metadata=record.metadata_json)
    if status is not None:
        record.status = normalize_workflow_status(status)
    if current_step is not None:
        record.current_step = current_step

    record.state_json = state_payload
    record.updated_at = _utcnow()
    if record.status in TERMINAL_WORKFLOW_STATUSES and not record.completed_at:
        record.completed_at = record.updated_at
    if commit:
        db.session.commit()
    return record


def _runtime_checkpoints(metadata: Any) -> list[dict[str, Any]]:
    raw = _json_object(metadata).get("runtime_checkpoints")
    return [dict(item) for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []


def _completed_workflow_step_count(workflow_run_id: str) -> int:
    return WorkflowStep.query.filter_by(workflow_run_id=workflow_run_id, status="completed").count()


def _safe_int(value: Any, default: int | None = None) -> int | None:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _normalize_checkpoint_status(status: str | None) -> str:
    normalized = str(status or "pending").lower()
    if normalized not in RUNTIME_CHECKPOINT_STATUSES:
        raise ValueError(f"Invalid runtime checkpoint status: {status}")
    return normalized


def _normalize_checkpoint_action(action: Any = None, *, kind: str | None = None) -> str:
    normalized = str(action or "").strip().lower()
    if not normalized:
        kind_normalized = str(kind or "").strip().lower()
        if kind_normalized in {"before_side_effect", "approval_gate"}:
            return "pause_run"
        return "create_review_step"
    aliases = {
        "none": "record_due",
        "record": "record_due",
        "review": "create_review_step",
        "self_check": "create_review_step",
        "wake_orchestrator": "create_review_step",
        "pause": "pause_run",
    }
    normalized = aliases.get(normalized, normalized)
    return normalized if normalized in RUNTIME_CHECKPOINT_ACTIONS else "create_review_step"


def _normalize_checkpoint_condition(
    *,
    workflow_run_id: str,
    condition: dict[str, Any] | None = None,
    due_at: str | None = None,
    due_in_seconds: int | float | None = None,
    stall_after_seconds: int | float | None = None,
    after_completed_steps: int | None = None,
    target_completed_steps: int | None = None,
) -> dict[str, Any]:
    payload = _json_object(condition)
    now = _utcnow()
    triggers: list[str] = []

    explicit_due_at = due_at or payload.get("due_at")
    if due_in_seconds is None:
        due_in_seconds = payload.get("due_in_seconds")
    seconds = _safe_int(due_in_seconds)
    if seconds is not None:
        seconds = max(0, min(seconds, 365 * 24 * 60 * 60))
        explicit_due_at = (now + timedelta(seconds=seconds)).isoformat()
        payload["due_in_seconds"] = seconds
    if explicit_due_at:
        payload["due_at"] = str(explicit_due_at)
        triggers.append("time")

    if stall_after_seconds is None:
        stall_after_seconds = payload.get("stall_after_seconds")
    stall_seconds = _safe_int(stall_after_seconds)
    if stall_seconds is not None:
        stall_seconds = max(1, min(stall_seconds, 365 * 24 * 60 * 60))
        payload["stall_after_seconds"] = stall_seconds
        payload.setdefault("baseline_activity_at", now.isoformat())
        triggers.append("stall")

    absolute_target = _safe_int(target_completed_steps, _safe_int(payload.get("target_completed_steps")))
    relative_steps = _safe_int(after_completed_steps, _safe_int(payload.get("after_completed_steps")))
    if absolute_target is None and relative_steps is not None:
        relative_steps = max(1, relative_steps)
        baseline = _completed_workflow_step_count(workflow_run_id)
        absolute_target = baseline + relative_steps
        payload["baseline_completed_steps"] = baseline
        payload["after_completed_steps"] = relative_steps
    if absolute_target is not None:
        payload["target_completed_steps"] = max(0, absolute_target)
        triggers.append("step_completions")

    existing_type = str(payload.get("type") or "").strip()
    if not existing_type:
        if len(set(triggers)) > 1:
            payload["type"] = "composite"
        elif triggers:
            payload["type"] = triggers[0]
        else:
            payload["type"] = "manual"
    return payload


def create_runtime_checkpoint(
    *,
    workflow_run_id: str,
    title: str | None = None,
    kind: str | None = None,
    action: str | None = None,
    reason: str | None = None,
    instruction: str | None = None,
    condition: dict[str, Any] | None = None,
    due_at: str | None = None,
    due_in_seconds: int | float | None = None,
    stall_after_seconds: int | float | None = None,
    after_completed_steps: int | None = None,
    target_completed_steps: int | None = None,
    created_by_user_id: int | None = None,
    metadata: dict[str, Any] | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    record = db.session.get(WorkflowRun, workflow_run_id)
    if not record:
        raise KeyError(f"Workflow run '{workflow_run_id}' not found.")

    now = _utcnow()
    metadata_payload = dict(_json_object(record.metadata_json))
    checkpoints = _runtime_checkpoints(metadata_payload)
    checkpoint_kind = str(kind or "custom")
    checkpoint = {
        "id": str(uuid.uuid4()),
        "title": str(title or checkpoint_kind or "Runtime checkpoint"),
        "kind": checkpoint_kind,
        "action": _normalize_checkpoint_action(action, kind=checkpoint_kind),
        "status": "pending",
        "reason": str(reason or ""),
        "instruction": str(instruction or ""),
        "condition": _normalize_checkpoint_condition(
            workflow_run_id=record.id,
            condition=condition,
            due_at=due_at,
            due_in_seconds=due_in_seconds,
            stall_after_seconds=stall_after_seconds,
            after_completed_steps=after_completed_steps,
            target_completed_steps=target_completed_steps,
        ),
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
        "created_by_user_id": created_by_user_id,
        "metadata": _json_object(metadata),
    }
    checkpoints.append(checkpoint)
    metadata_payload["runtime_checkpoints"] = checkpoints
    record.metadata_json = metadata_payload
    record.updated_at = now
    if commit:
        db.session.commit()
    return checkpoint


def update_runtime_checkpoint(
    workflow_run_id: str,
    checkpoint_id: str,
    *,
    status: str | None = None,
    note: str | None = None,
    metadata: dict[str, Any] | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    record = db.session.get(WorkflowRun, workflow_run_id)
    if not record:
        raise KeyError(f"Workflow run '{workflow_run_id}' not found.")
    if not checkpoint_id:
        raise ValueError("checkpoint_id is required.")

    metadata_payload = dict(_json_object(record.metadata_json))
    checkpoints = _runtime_checkpoints(metadata_payload)
    now = _utcnow()
    updated_checkpoint: dict[str, Any] | None = None
    for checkpoint in checkpoints:
        if str(checkpoint.get("id")) != str(checkpoint_id):
            continue
        if status is not None:
            checkpoint["status"] = _normalize_checkpoint_status(status)
            if checkpoint["status"] in {"resolved", "cancelled"}:
                checkpoint[f"{checkpoint['status']}_at"] = now.isoformat()
        if note is not None:
            checkpoint["resolution_note"] = str(note)
        if metadata is not None:
            checkpoint["metadata"] = _merge_json_object(checkpoint.get("metadata"), metadata, replace=False)
        checkpoint["updated_at"] = now.isoformat()
        updated_checkpoint = checkpoint
        break

    if updated_checkpoint is None:
        raise KeyError(f"Runtime checkpoint '{checkpoint_id}' not found.")

    metadata_payload["runtime_checkpoints"] = checkpoints
    record.metadata_json = metadata_payload
    record.updated_at = now
    if commit:
        db.session.commit()
    return updated_checkpoint


def _runtime_event_list(metadata: Any) -> list[dict[str, Any]]:
    raw = _json_object(metadata).get("runtime_events")
    return [dict(item) for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []


def _checkpoint_due_event(run: WorkflowRun, checkpoint: dict[str, Any]) -> dict[str, Any]:
    packet = make_agent_event(
        kind="runtime.checkpoint.due",
        phase="runtime",
        status="warning",
        title=f"Checkpoint due: {checkpoint.get('title') or checkpoint.get('kind') or 'Runtime checkpoint'}",
        summary=checkpoint.get("reason") or checkpoint.get("instruction") or "",
        subject_type="runtime_checkpoint",
        subject_id=checkpoint.get("id"),
        payload={
            "workflow_run_id": run.id,
            "checkpoint_id": checkpoint.get("id"),
            "checkpoint_kind": checkpoint.get("kind"),
            "due_reasons": checkpoint.get("due_reasons") or [],
            "completed_step_count": checkpoint.get("completed_step_count"),
            "condition": checkpoint.get("condition") or {},
        },
    )
    return packet["event"]


def _checkpoint_action_event(run: WorkflowRun, checkpoint: dict[str, Any], *, action: str, status: str, summary: str) -> dict[str, Any]:
    packet = make_agent_event(
        kind=f"runtime.checkpoint.{action}",
        phase="runtime",
        status=status,
        title=f"Checkpoint action: {action.replace('_', ' ')}",
        summary=summary,
        subject_type="runtime_checkpoint",
        subject_id=checkpoint.get("id"),
        payload={
            "workflow_run_id": run.id,
            "checkpoint_id": checkpoint.get("id"),
            "checkpoint_kind": checkpoint.get("kind"),
            "action": action,
            "review_step_id": checkpoint.get("review_step_id"),
        },
    )
    return packet["event"]


def _runtime_checkpoint_review_instructions(checkpoint: dict[str, Any], run: WorkflowRun) -> str:
    checkpoint_id = checkpoint.get("id")
    instructions = (
        "This is an automatic runtime checkpoint review step.\n"
        f"Run id: {run.id}\n"
        f"Checkpoint id: {checkpoint_id}\n"
        f"Checkpoint kind: {checkpoint.get('kind') or 'custom'}\n"
        f"Reason: {checkpoint.get('reason') or 'No reason recorded.'}\n"
        f"Instruction: {checkpoint.get('instruction') or 'Inspect the run state and decide whether progress is healthy.'}\n\n"
        "Required procedure:\n"
        "1. Call `runs_observe` for compact progress, costs, failures, sampled results, artifacts, and checkpoint state. "
        "Use `runs_get` only if the compact observation identifies a specific issue that needs deeper inspection.\n"
        "2. Decide whether the run should continue, pause, or be adjusted. If action is needed, use available run tools.\n"
        "3. Call `runs_update_checkpoint` for this checkpoint with status `resolved` and a concise note describing what you checked.\n"
        "4. Return a compact JSON summary of the check."
    )
    if str(checkpoint.get("kind") or "").lower() == "time_self_nudge":
        instructions += (
            "\n5. This worker was launched by a chat self-nudge. Call `send_user_alert` with a concise "
            f"result and action_url `/workflow-runs?run_id={run.id}` so the user can inspect the durable run."
        )
    return instructions


def _unique_checkpoint_step_key(run_id: str, checkpoint_id: str) -> str:
    base = f"checkpoint_review_{checkpoint_id.replace('-', '')[:12] or uuid.uuid4().hex[:12]}"
    candidate = base
    suffix = 2
    while WorkflowStep.query.filter_by(workflow_run_id=run_id, step_key=candidate).first():
        candidate = f"{base}_{suffix}"
        suffix += 1
    return candidate


def _apply_runtime_checkpoint_action(
    run: WorkflowRun,
    checkpoint: dict[str, Any],
    *,
    now: datetime,
    runtime_events: list[dict[str, Any]],
) -> bool:
    action = _normalize_checkpoint_action(checkpoint.get("action"), kind=checkpoint.get("kind"))
    checkpoint["action"] = action
    if checkpoint.get("action_status") in {"review_step_planned", "run_paused", "recorded"}:
        return False

    if action == "record_due":
        checkpoint["action_status"] = "recorded"
        checkpoint["actioned_at"] = now.isoformat()
        runtime_events.append(_checkpoint_action_event(run, checkpoint, action=action, status="completed", summary="Checkpoint due state recorded."))
        return True

    if action == "pause_run":
        if str(run.status or "").lower() not in TERMINAL_WORKFLOW_STATUSES and str(run.status or "").lower() != "paused":
            pause_workflow_run(run.id, commit=False)
        checkpoint["action_status"] = "run_paused"
        checkpoint["actioned_at"] = now.isoformat()
        runtime_events.append(_checkpoint_action_event(run, checkpoint, action=action, status="completed", summary="Run paused because checkpoint action requested it."))
        return True

    if action == "create_review_step":
        if checkpoint.get("review_step_id"):
            checkpoint["action_status"] = "review_step_planned"
            return False
        run_metadata = _json_object(run.metadata_json)
        execution_scope = _json_object(run_metadata.get("execution_scope"))
        step_metadata = {
            "runtime_checkpoint_id": checkpoint.get("id"),
            "runtime_checkpoint_action": action,
            "completion_requirements": {
                "requires_checkpoint_resolution": checkpoint.get("id"),
            },
        }
        if execution_scope:
            step_metadata["execution_scope"] = execution_scope
        if isinstance(run_metadata.get("runtime_policy"), dict):
            step_metadata["runtime_policy"] = run_metadata["runtime_policy"]
        if run_metadata.get("max_tool_calls") is not None:
            step_metadata["max_tool_calls"] = run_metadata.get("max_tool_calls")
        step_metadata["parallel_tool_calls"] = 1
        step = create_workflow_step(
            workflow_run_id=run.id,
            step_type="runtime_checkpoint_review",
            title=f"Review checkpoint: {checkpoint.get('title') or checkpoint.get('kind') or 'Runtime checkpoint'}",
            step_key=_unique_checkpoint_step_key(run.id, str(checkpoint.get("id") or "")),
            status="planned",
            assigned_model_role=checkpoint.get("assigned_model_role") or "fast",
            created_by_user_id=run.created_by_user_id,
            instructions=_runtime_checkpoint_review_instructions(checkpoint, run),
            input_payload={
                "workflow_run_id": run.id,
                "checkpoint_id": checkpoint.get("id"),
                "checkpoint": checkpoint,
            },
            output_schema={
                "type": "object",
                "properties": {
                    "summary": {"type": "string"},
                    "decision": {"type": "string"},
                    "follow_up": {"type": "string"},
                },
                "required": ["summary"],
            },
            metadata=step_metadata,
            commit=False,
        )
        checkpoint["review_step_id"] = step.id
        checkpoint["action_status"] = "review_step_planned"
        checkpoint["actioned_at"] = now.isoformat()
        if (
            str(checkpoint.get("kind") or "").lower() == "time_self_nudge"
            and str(run.status or "").lower() not in TERMINAL_WORKFLOW_STATUSES
        ):
            run.status = "running"
            run.completed_at = None
            run.updated_at = now
            checkpoint["run_reentered"] = True
        runtime_events.append(_checkpoint_action_event(run, checkpoint, action=action, status="completed", summary=f"Created review step {step.id}."))
        return True

    return False


def evaluate_runtime_checkpoints(
    workflow_run_id: str | None = None,
    *,
    workflow_runs: list[WorkflowRun] | None = None,
    commit: bool = True,
) -> list[dict[str, Any]]:
    """Persist newly due runtime checkpoints and emit compact run-level events."""
    if workflow_runs is not None:
        runs = [run for run in workflow_runs if run is not None]
    elif workflow_run_id:
        runs = [db.session.get(WorkflowRun, workflow_run_id)]
    else:
        runs = WorkflowRun.query.filter(WorkflowRun.status.notin_(TERMINAL_WORKFLOW_STATUSES)).all()

    now = _utcnow()
    fired: list[dict[str, Any]] = []
    active_runs = [item for item in runs if item is not None]
    run_ids = [str(run.id) for run in active_runs]
    step_rows = (
        WorkflowStep.query.filter(WorkflowStep.workflow_run_id.in_(run_ids))
        .order_by(WorkflowStep.workflow_run_id.asc(), WorkflowStep.created_at.asc(), WorkflowStep.id.asc())
        .all()
        if run_ids
        else []
    )
    steps_by_run: dict[str, list[WorkflowStep]] = {run_id: [] for run_id in run_ids}
    for step in step_rows:
        steps_by_run[str(step.workflow_run_id)].append(step)
    any_changed = False
    for run in active_runs:
        metadata_payload = dict(_json_object(run.metadata_json))
        checkpoints = _runtime_checkpoints(metadata_payload)
        if not checkpoints:
            continue

        computed_by_id = {
            str(item.get("id")): item
            for item in (
                runtime_checkpoint_summary(
                    run,
                    steps_by_run.get(str(run.id), []),
                    now=as_utc(now),
                ).get("runtime_checkpoints")
                or []
            )
            if isinstance(item, dict) and item.get("id")
        }
        changed = False
        runtime_events = _runtime_event_list(metadata_payload)
        for checkpoint in checkpoints:
            checkpoint_id = str(checkpoint.get("id") or "")
            computed = computed_by_id.get(checkpoint_id)
            if not computed:
                continue
            stored_status = str(checkpoint.get("status") or "pending").lower()
            if stored_status not in {"pending", "due"}:
                continue
            is_due = computed.get("status") == "due"
            if not is_due:
                continue

            newly_due = stored_status == "pending"
            if newly_due:
                checkpoint["status"] = "due"
                checkpoint["due"] = True
                checkpoint["due_reasons"] = computed.get("due_reasons") or []
                checkpoint["completed_step_count"] = computed.get("completed_step_count")
                checkpoint["triggered_at"] = now.isoformat()
                checkpoint["updated_at"] = now.isoformat()
                runtime_events.append(_checkpoint_due_event(run, checkpoint))
                fired.append({
                    "workflow_run_id": run.id,
                    "checkpoint": dict(checkpoint),
                })
                changed = True
            if _apply_runtime_checkpoint_action(run, checkpoint, now=now, runtime_events=runtime_events):
                checkpoint["updated_at"] = now.isoformat()
                changed = True

        if changed:
            any_changed = True
            metadata_payload["runtime_checkpoints"] = checkpoints
            metadata_payload["runtime_events"] = runtime_events[-200:]
            run.metadata_json = metadata_payload
            run.updated_at = now

    if any_changed and commit:
        db.session.commit()
    return fired


def create_workflow_step(
    *,
    workflow_run_id: str,
    step_type: str,
    title: str | None = None,
    step_key: str | None = None,
    status: str = "planned",
    sequence: int | None = None,
    parent_step_id: str | None = None,
    depends_on_step_ids: Any = None,
    assigned_model_role: str | None = None,
    created_by_user_id: int | None = None,
    instructions: str | None = None,
    capability_ids: Any = None,
    skill_ids: Any = None,
    tool_ids: Any = None,
    tool_view_ids: Any = None,
    provider_requirements: Any = None,
    resource_reference_ids: Any = None,
    approval_ids: Any = None,
    job_ids: Any = None,
    artifact_ids: Any = None,
    source_ids: Any = None,
    evidence_ids: Any = None,
    input_schema: dict[str, Any] | None = None,
    output_schema: dict[str, Any] | None = None,
    input_payload: dict[str, Any] | None = None,
    output_payload: dict[str, Any] | None = None,
    error: str | None = None,
    metadata: dict[str, Any] | None = None,
    log_source_ids: Any = None,
    make_current: bool = False,
    commit: bool = True,
) -> WorkflowStep:
    run = db.session.get(WorkflowRun, workflow_run_id)
    if not run:
        raise KeyError(f"Workflow run '{workflow_run_id}' not found.")
    dependency_refs = _string_list(depends_on_step_ids)
    if parent_step_id and str(parent_step_id) not in dependency_refs:
        dependency_refs.insert(0, str(parent_step_id))

    dependency_ids: list[str] = []
    legacy_parent_step_id = None
    for dependency_ref in dependency_refs:
        parent = _workflow_step_for_reference(workflow_run_id, dependency_ref)
        resolved_ref = parent.id if parent else str(dependency_ref)
        if resolved_ref not in dependency_ids:
            dependency_ids.append(resolved_ref)
        if parent_step_id and str(dependency_ref) == str(parent_step_id) and parent:
            legacy_parent_step_id = parent.id
    if not legacy_parent_step_id and len(dependency_ids) == 1:
        parent = _workflow_step_for_reference(workflow_run_id, dependency_ids[0])
        if parent:
            legacy_parent_step_id = parent.id

    now = _utcnow()
    normalized_status = normalize_workflow_step_status(status)
    metadata_payload = _json_object(metadata)
    normalized_tool_view_ids = _string_list(tool_view_ids)
    if normalized_tool_view_ids:
        metadata_payload = {**metadata_payload, "tool_view_ids": normalized_tool_view_ids}
    record = WorkflowStep(
        id=str(uuid.uuid4()),
        workflow_run_id=workflow_run_id,
        parent_step_id=legacy_parent_step_id,
        depends_on_step_ids=dependency_ids,
        step_key=step_key,
        step_type=str(step_type),
        title=title,
        status=normalized_status,
        sequence=sequence,
        assigned_model_role=assigned_model_role,
        created_by_user_id=created_by_user_id if created_by_user_id is not None else run.created_by_user_id,
        instructions=instructions,
        capability_ids=_string_list(capability_ids),
        skill_ids=_string_list(skill_ids),
        tool_ids=_string_list(tool_ids),
        provider_requirements=_string_list(provider_requirements),
        resource_reference_ids=_string_list(resource_reference_ids),
        approval_ids=_string_list(approval_ids),
        job_ids=_string_list(job_ids),
        artifact_ids=_string_list(artifact_ids),
        source_ids=_string_list(source_ids),
        evidence_ids=_string_list(evidence_ids),
        input_schema=_json_object(input_schema),
        output_schema=_json_object(output_schema),
        input_payload=_json_object(input_payload),
        output_payload=_json_object(output_payload),
        error=error,
        metadata_json=metadata_payload,
        log_source_ids=_string_list(log_source_ids),
    )
    if normalized_status == "running":
        record.started_at = now
    if normalized_status in TERMINAL_WORKFLOW_STEP_STATUSES:
        record.completed_at = now
    db.session.add(record)

    run.step_ids = _append_unique(run.step_ids, [record.id])
    if make_current or normalized_status in {"running", "waiting_for_approval", "waiting_for_review"} or not run.current_step:
        run.current_step = record.id
    run.updated_at = now

    if commit:
        db.session.commit()
    return record


def set_workflow_step_status(
    step_id: str,
    status: str,
    *,
    output_payload: dict[str, Any] | None = None,
    error: str | None = None,
    make_current: bool = False,
    commit: bool = True,
) -> WorkflowStep:
    record = db.session.get(WorkflowStep, step_id)
    if not record:
        raise KeyError(f"Workflow step '{step_id}' not found.")

    now = _utcnow()
    normalized_status = normalize_workflow_step_status(status)
    record.status = normalized_status
    record.updated_at = now
    if output_payload is not None:
        record.output_payload = _json_object(output_payload)
    if error is not None:
        record.error = error
    elif normalized_status == "completed":
        record.error = None
    if normalized_status == "running" and not record.started_at:
        record.started_at = now
    if normalized_status in TERMINAL_WORKFLOW_STEP_STATUSES and not record.completed_at:
        record.completed_at = now

    run = db.session.get(WorkflowRun, record.workflow_run_id)
    if run:
        if make_current or normalized_status in {"running", "waiting_for_approval", "waiting_for_review"}:
            run.current_step = record.id
        run.updated_at = now

    if commit:
        db.session.commit()
    return record


def create_source_record(
    *,
    workflow_run_id: str,
    source_type: str,
    uri: str,
    step_id: str | None = None,
    provider: str | None = None,
    external_id: str | None = None,
    title: str | None = None,
    summary: str | None = None,
    content_hash: str | None = None,
    access_scope: str = "private",
    created_by_user_id: int | None = None,
    metadata: dict[str, Any] | None = None,
    commit: bool = True,
) -> RuntimeSourceRecord:
    run = db.session.get(WorkflowRun, workflow_run_id)
    if not run:
        raise KeyError(f"Workflow run '{workflow_run_id}' not found.")
    if step_id:
        step = db.session.get(WorkflowStep, step_id)
        if not step:
            raise KeyError(f"Workflow step '{step_id}' not found.")
        if step.workflow_run_id != workflow_run_id:
            raise ValueError("Source step_id must belong to the same run.")

    owner_id = created_by_user_id if created_by_user_id is not None else run.created_by_user_id
    record = RuntimeSourceRecord(
        id=str(uuid.uuid4()),
        workflow_run_id=workflow_run_id,
        step_id=step_id,
        source_type=str(source_type),
        uri=str(uri),
        provider=provider,
        external_id=external_id,
        title=title,
        summary=summary,
        content_hash=content_hash,
        access_scope=access_scope or "private",
        created_by_user_id=owner_id,
        metadata_json=_json_object(metadata),
    )
    db.session.add(record)
    _link_workflow_run_ids(workflow_run_id, source_ids=[record.id])
    _link_workflow_step_ids(step_id, source_ids=[record.id])
    if commit:
        db.session.commit()
    return record


def create_evidence_record(
    *,
    workflow_run_id: str,
    source_id: str,
    step_id: str | None = None,
    artifact_id: str | None = None,
    claim: str | None = None,
    quote: str | None = None,
    citation: dict[str, Any] | None = None,
    relevance_score: float | None = None,
    novelty_score: float | None = None,
    used_in_artifact: bool = False,
    created_by_user_id: int | None = None,
    metadata: dict[str, Any] | None = None,
    commit: bool = True,
) -> RuntimeEvidenceRecord:
    run = db.session.get(WorkflowRun, workflow_run_id)
    if not run:
        raise KeyError(f"Workflow run '{workflow_run_id}' not found.")
    source = db.session.get(RuntimeSourceRecord, source_id)
    if not source:
        raise KeyError(f"Runtime source '{source_id}' not found.")
    if source.workflow_run_id != workflow_run_id:
        raise ValueError("Evidence source_id must belong to the same run.")
    if step_id:
        step = db.session.get(WorkflowStep, step_id)
        if not step:
            raise KeyError(f"Workflow step '{step_id}' not found.")
        if step.workflow_run_id != workflow_run_id:
            raise ValueError("Evidence step_id must belong to the same run.")
    if artifact_id:
        artifact = db.session.get(RuntimeArtifact, artifact_id)
        if not artifact:
            raise KeyError(f"Runtime artifact '{artifact_id}' not found.")
        if artifact.workflow_run_id and artifact.workflow_run_id != workflow_run_id:
            raise ValueError("Evidence artifact_id must belong to the same run.")

    owner_id = created_by_user_id if created_by_user_id is not None else run.created_by_user_id
    record = RuntimeEvidenceRecord(
        id=str(uuid.uuid4()),
        workflow_run_id=workflow_run_id,
        step_id=step_id,
        source_id=source_id,
        artifact_id=artifact_id,
        claim=claim,
        quote=quote,
        citation=_json_object(citation),
        relevance_score=relevance_score,
        novelty_score=novelty_score,
        used_in_artifact=bool(used_in_artifact),
        created_by_user_id=owner_id,
        metadata_json=_json_object(metadata),
    )
    db.session.add(record)
    _link_workflow_run_ids(workflow_run_id, evidence_ids=[record.id])
    _link_workflow_step_ids(step_id, evidence_ids=[record.id])
    if artifact_id:
        _link_workflow_run_ids(workflow_run_id, artifact_ids=[artifact_id])
        _link_workflow_step_ids(step_id, artifact_ids=[artifact_id])
    if commit:
        db.session.commit()
    return record


@dataclass
class ExecutionGuardResult:
    allowed: bool
    status: str = "allowed"
    message: str = "Execution allowed."
    approval_id: str | None = None
    warnings: list[str] = field(default_factory=list)
    resource_reference_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "status": self.status,
            "message": self.message,
            "approval_id": self.approval_id,
            "warnings": self.warnings,
            "resource_reference_ids": self.resource_reference_ids,
        }

    def to_tool_response(self) -> dict[str, Any]:
        payload = self.to_dict()
        payload["error"] = self.message
        return payload


def guard_tool_execution(
    tool_record: Any,
    *,
    arguments: dict[str, Any] | None = None,
    requester_user_id: int | None = None,
    approval_id: str | None = None,
    resource_reference_ids: Any = None,
    workflow_run_id: str | None = None,
    audit_log_source_id: str | None = None,
    log_source_ids: Any = None,
    runtime_policy: dict[str, Any] | None = None,
) -> ExecutionGuardResult:
    if not tool_record:
        return ExecutionGuardResult(True)

    effects = normalize_effects(getattr(tool_record, "side_effects", []))
    approval_required = bool(getattr(tool_record, "approval_required", False))
    resource_requirements = _string_list(getattr(tool_record, "resource_requirements", []))
    references = _string_list(resource_reference_ids)

    if resource_requirements:
        required_permissions = [
            requirement
            for requirement in resource_requirements
            if requirement.lower() in MUTATING_RESOURCE_PERMISSIONS or requirement.lower() == "read"
        ]
        # Only enforce the runtime reference gate when:
        # (a) The caller explicitly provided reference IDs — validate them, or
        # (b) There are actual permission requirements (mutating ops) that need a validated reference.
        # Informational resource tags (e.g. "local_path:read", "googledrive:file:write" which
        # don't match MUTATING_RESOURCE_PERMISSIONS) with no provided references are allowed through.
        should_enforce = bool(references) or bool(required_permissions)
        if should_enforce:
            valid, reasons, resource_records = validate_resource_references(
                references,
                owner_user_id=requester_user_id,
                required_permissions=required_permissions,
            )
            if not valid or (required_permissions and not resource_records):
                return ExecutionGuardResult(
                    False,
                    status="missing_resource",
                    message="Tool execution requires resource references: " + "; ".join(reasons or resource_requirements),
                    resource_reference_ids=references,
                )

    if approval_required:
        disable_approvals = False
        policy_supplied = isinstance(runtime_policy, dict) and "disable_tool_approvals" in runtime_policy
        if policy_supplied:
            disable_approvals = bool(runtime_policy.get("disable_tool_approvals"))
        if has_app_context():
            setting = db.session.get(Setting, "disable_tool_approvals")
            if not policy_supplied and setting and setting.value == "true":
                disable_approvals = True

        if disable_approvals:
            return ExecutionGuardResult(
                True,
                status="approved",
                message=(
                    "Execution allowed due to active runtime policy."
                    if policy_supplied
                    else "Execution allowed due to disabled global approvals setting."
                ),
                resource_reference_ids=references,
            )

        approved = get_usable_approval(
            approval_id,
            requester_user_id=requester_user_id,
            tool_id=getattr(tool_record, "id", None),
        )
        if approved:
            return ExecutionGuardResult(
                True,
                status="approved",
                message="Execution approved.",
                approval_id=approved.id,
                resource_reference_ids=references,
            )

        subject_id = f"tool-call-{uuid.uuid4()}"
        approval = create_approval_request(
            requester_user_id=requester_user_id,
            subject_type="tool_call",
            subject_id=subject_id,
            tool_id=getattr(tool_record, "id", None),
            workflow_run_id=workflow_run_id,
            effects=effects,
            preview={
                "tool_id": getattr(tool_record, "id", None),
                "legacy_name": getattr(tool_record, "legacy_name", None),
                "arguments": _json_object(arguments),
            },
            audit_log_source_id=audit_log_source_id,
            log_source_ids=log_source_ids,
        )
        return ExecutionGuardResult(
            False,
            status="waiting_for_approval",
            message=f"Tool '{getattr(tool_record, 'id', 'unknown')}' requires approval before execution.",
            approval_id=approval.id,
            resource_reference_ids=references,
        )

    warnings = []
    if effects:
        source = getattr(tool_record, "source", "") or ""
        if source.startswith("legacy"):
            warnings.append("Legacy side-effect metadata is warning-only until the tool declares explicit approval policy.")
        else:
            warnings.append("Tool declares side effects but does not require approval.")

    return ExecutionGuardResult(True, warnings=warnings, resource_reference_ids=references)


def visible_to_user(record: Any, user_id: int, is_admin: bool = False) -> bool:
    if is_admin:
        return True
    for attr in ("created_by_user_id", "owner_user_id", "requester_user_id", "approver_user_id"):
        value = getattr(record, attr, None)
        if value is not None and int(value) == int(user_id):
            return True
    workflow_run_id = getattr(record, "workflow_run_id", None)
    if workflow_run_id:
        run = db.session.get(WorkflowRun, workflow_run_id)
        if run and visible_to_user(run, user_id, is_admin):
            return True
    return False


def attach_runtime_log_link_ids(source_ids: Any) -> list[dict[str, Any]]:
    if not has_app_context():
        return []
    registry = getattr(current_app, "log_registry", None)
    if not registry:
        return []
    links = []
    for source_id in _string_list(source_ids):
        try:
            links.append(registry.get_link_for_source(source_id))
        except KeyError:
            continue
    return links
