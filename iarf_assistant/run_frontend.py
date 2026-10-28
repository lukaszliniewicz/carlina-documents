from __future__ import annotations

import re
from typing import Any

from flask import Blueprint, jsonify, request
from flask_login import current_user, login_required
from sqlalchemy import func

from iarf_assistant.context_frontend import (
    REVIEWABLE_ARTIFACT_STATUSES,
    _artifact_summary,
    _artifact_version_numbers,
    _humanize,
    _safe_external_url,
    _semantic_value,
    _value_kind,
    _value_summary,
)
from iarf_assistant.core.runtime_state import (
    attach_runtime_log_link_ids,
    visible_to_user,
)
from iarf_assistant.core.runs.projections import load_run_projection, serialize_workflow_run
from carlina.core.utc import utc_iso, utc_now_db
from iarf_assistant.extensions import db
from iarf_assistant.models import (
    LLMCost,
    RuntimeApprovalRequest,
    RuntimeArtifact,
    RuntimeArtifactVersion,
    RuntimeEvidenceRecord,
    RuntimeInteraction,
    RuntimeJob,
    RuntimeResourceReference,
    RuntimeSourceRecord,
    WorkflowRun,
    WorkflowStep,
)


frontend_run_blueprint = Blueprint("frontend_run_api", __name__)

RUN_PAGE_DEFAULT_LIMIT = 40
RUN_PAGE_MAX_LIMIT = 100
STUDIO_RECORD_LIMIT = 500
TERMINAL_RUN_STATUSES = {"completed", "failed", "cancelled"}
TERMINAL_STEP_STATUSES = {"completed", "failed", "cancelled", "skipped"}
ATTENTION_STEP_STATUSES = {
    "failed",
    "waiting_for_approval",
    "waiting_for_review",
    "paused",
}


def _bounded_int(
    value: Any,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def _iso(value: Any) -> str | None:
    return value.isoformat() if value and hasattr(value, "isoformat") else None


def _duration_ms(started_at: Any, completed_at: Any) -> int | None:
    if not started_at:
        return None
    end = completed_at or utc_now_db()
    try:
        return max(0, round((end - started_at).total_seconds() * 1_000))
    except (TypeError, ValueError):
        return None


def _visible_run(run_id: str) -> WorkflowRun | None:
    run = db.session.get(WorkflowRun, run_id)
    if not run:
        return None
    if not visible_to_user(run, current_user.id, current_user.is_admin):
        return None
    return run


def _run_query():
    query = WorkflowRun.query
    if current_user.is_admin:
        return query
    return query.filter(
        WorkflowRun.created_by_user_id == int(current_user.id)
    )


def _run_title(run: WorkflowRun) -> str:
    title = str(run.title or "").strip()
    if title:
        return title
    if run.workflow_id:
        return _humanize(run.workflow_id, fallback="Run")
    return _humanize(run.kind, fallback="Run")


def _status_label(status: Any) -> str:
    return _humanize(status, fallback="Unknown")


def _step_title(step: WorkflowStep) -> str:
    return str(
        step.title
        or _humanize(step.step_key, fallback="")
        or _humanize(step.step_type, fallback="Step")
    )


def _step_dependencies(
    step: WorkflowStep,
    *,
    steps_by_id: dict[str, WorkflowStep],
    steps_by_key: dict[str, WorkflowStep],
) -> list[str]:
    raw_values = list(step.depends_on_step_ids or [])
    if not raw_values and step.parent_step_id:
        raw_values = [step.parent_step_id]
    resolved: list[str] = []
    seen: set[str] = set()
    for raw_value in raw_values:
        value = str(raw_value)
        dependency = steps_by_id.get(value) or steps_by_key.get(value)
        if dependency and dependency.id != step.id and dependency.id not in seen:
            resolved.append(str(dependency.id))
            seen.add(str(dependency.id))
    return resolved


def _jobs_for_step(
    step: WorkflowStep,
    *,
    jobs_by_id: dict[str, RuntimeJob],
    jobs_by_source: dict[str, list[RuntimeJob]],
) -> list[RuntimeJob]:
    rows: list[RuntimeJob] = []
    seen: set[str] = set()
    for job_id in step.job_ids or []:
        job = jobs_by_id.get(str(job_id))
        if job and job.id not in seen:
            rows.append(job)
            seen.add(job.id)
    for source_value in (step.id, step.step_key):
        if not source_value:
            continue
        for job in jobs_by_source.get(str(source_value), []):
            if job.id not in seen:
                rows.append(job)
                seen.add(job.id)
    return rows


def _step_progress(step: WorkflowStep, jobs: list[RuntimeJob]) -> int | None:
    values = [
        max(0, min(100, round(float(job.progress))))
        for job in jobs
        if job.progress is not None
    ]
    if values:
        return round(sum(values) / len(values))
    if step.status in TERMINAL_STEP_STATUSES:
        return 100
    if step.status == "running":
        return None
    return 0


def _step_purpose(step: WorkflowStep) -> str:
    metadata = step.metadata_json if isinstance(step.metadata_json, dict) else {}
    for key in ("summary", "purpose", "objective", "description"):
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return _value_summary(value)
    if step.instructions:
        return _value_summary(step.instructions)
    return _humanize(step.step_type, fallback="Workflow step")


def _step_summary(
    step: WorkflowStep,
    *,
    dependencies: list[str],
    jobs: list[RuntimeJob],
) -> dict[str, Any]:
    status = str(step.status or "planned")
    return {
        "id": str(step.id),
        "key": str(step.step_key or ""),
        "title": _step_title(step),
        "purpose": _step_purpose(step),
        "type": str(step.step_type or "task"),
        "status": status,
        "status_label": _status_label(status),
        "sequence": int(step.sequence) if step.sequence is not None else None,
        "role": str(step.assigned_model_role or ""),
        "parent_id": str(step.parent_step_id) if step.parent_step_id else None,
        "dependency_ids": dependencies,
        "duration_ms": _duration_ms(step.started_at, step.completed_at),
        "progress": _step_progress(step, jobs),
        "needs_attention": bool(
            status in ATTENTION_STEP_STATUSES or step.error
        ),
        "artifact_count": len(step.artifact_ids or []),
        "created_at": _iso(step.created_at),
        "started_at": _iso(step.started_at),
        "completed_at": _iso(step.completed_at),
        "updated_at": _iso(step.updated_at),
    }


def _run_progress(
    run: WorkflowRun,
    steps: list[WorkflowStep],
) -> dict[str, int]:
    completed = sum(
        1 for step in steps if step.status in TERMINAL_STEP_STATUSES
    )
    failed = sum(1 for step in steps if step.status == "failed")
    active = sum(
        1
        for step in steps
        if step.status
        in {
            "queued",
            "running",
            "waiting_for_approval",
            "waiting_for_review",
            "paused",
        }
    )
    total = len(steps)
    percent = (
        round((completed / total) * 100)
        if total
        else (100 if run.status == "completed" else 0)
    )
    return {
        "completed": completed,
        "failed": failed,
        "active": active,
        "total": total,
        "percent": percent,
    }


def _current_step(
    run: WorkflowRun,
    steps: list[WorkflowStep],
) -> WorkflowStep | None:
    if not run.current_step:
        return next(
            (step for step in steps if step.status == "running"),
            None,
        )
    return next(
        (
            step
            for step in steps
            if run.current_step in {step.id, step.step_key}
        ),
        None,
    )


def _run_controls(run: WorkflowRun) -> list[dict[str, Any]]:
    status = str(run.status or "draft")
    controls: list[dict[str, Any]] = []

    def add(
        control_id: str,
        label: str,
        *,
        tone: str = "neutral",
    ) -> None:
        controls.append(
            {
                "id": control_id,
                "label": label,
                "method": "POST",
                "url": f"/api/runtime/runs/{run.id}/{control_id}",
                "tone": tone,
            }
        )

    if status in {"draft", "ready"}:
        add("start", "Start run", tone="primary")
    elif status in {"running", "waiting_for_approval"}:
        add("pause", "Pause")
    elif status == "paused":
        add("resume", "Resume run", tone="primary")
    if status not in TERMINAL_RUN_STATUSES:
        add("cancel", "Cancel", tone="danger")
    return controls


def _cost_summary(run_id: str) -> dict[str, Any]:
    cost, prompt_tokens, completion_tokens, total_tokens = (
        db.session.query(
            func.coalesce(func.sum(LLMCost.cost), 0.0),
            func.coalesce(func.sum(LLMCost.prompt_tokens), 0),
            func.coalesce(func.sum(LLMCost.completion_tokens), 0),
            func.coalesce(func.sum(LLMCost.total_tokens), 0),
        )
        .filter(LLMCost.workflow_run_id == run_id)
        .one()
    )
    model_rows = (
        db.session.query(LLMCost.model, func.count(LLMCost.id))
        .filter(LLMCost.workflow_run_id == run_id)
        .group_by(LLMCost.model)
        .order_by(func.count(LLMCost.id).desc())
        .limit(12)
        .all()
    )
    return {
        "amount": round(float(cost or 0.0), 6),
        "currency": "USD",
        "prompt_tokens": int(prompt_tokens or 0),
        "completion_tokens": int(completion_tokens or 0),
        "total_tokens": int(total_tokens or 0),
        "models": [
            {"id": str(model), "calls": int(calls or 0)}
            for model, calls in model_rows
            if model
        ],
    }


def _document_sections(value: Any) -> dict[str, Any]:
    semantic = _semantic_value(value)
    if not isinstance(semantic, dict):
        return {
            "summary": _value_summary(semantic),
            "sections": (
                [
                    {
                        "id": "detail",
                        "title": "Detail",
                        "summary": _value_summary(semantic),
                        "kind": _value_kind(semantic),
                        "value": semantic,
                    }
                ]
                if semantic not in (None, "", [], {})
                else []
            ),
        }

    summary = ""
    for key in ("summary", "description", "objective", "message"):
        candidate = semantic.get(key)
        if isinstance(candidate, str) and candidate.strip():
            summary = _value_summary(candidate)
            break
    sections = []
    for key, item in semantic.items():
        if (
            key in {"summary", "description"}
            and _value_summary(item) == summary
        ):
            continue
        sections.append(
            {
                "id": re.sub(r"[^a-z0-9]+", "-", str(key).lower()).strip("-")
                or "section",
                "title": _humanize(key, fallback="Detail"),
                "summary": _value_summary(item),
                "kind": _value_kind(item),
                "value": item,
            }
        )
    return {
        "summary": summary or _value_summary(semantic),
        "sections": sections,
    }


def _source_summary(source: RuntimeSourceRecord) -> dict[str, Any]:
    return {
        "id": str(source.id),
        "step_id": str(source.step_id) if source.step_id else None,
        "title": str(
            source.title
            or _humanize(source.source_type, fallback="Source")
        ),
        "summary": _value_summary(source.summary or ""),
        "type": str(source.source_type or "source"),
        "provider": str(source.provider) if source.provider else None,
        "url": _safe_external_url(source.uri),
        "created_at": _iso(source.created_at),
    }


def _evidence_summary(
    evidence: RuntimeEvidenceRecord,
    sources_by_id: dict[str, RuntimeSourceRecord],
) -> dict[str, Any]:
    source = sources_by_id.get(str(evidence.source_id))
    return {
        "id": str(evidence.id),
        "step_id": str(evidence.step_id) if evidence.step_id else None,
        "artifact_id": (
            str(evidence.artifact_id) if evidence.artifact_id else None
        ),
        "claim": _value_summary(evidence.claim or ""),
        "quote": _value_summary(evidence.quote or ""),
        "citation": _semantic_value(evidence.citation or {}),
        "relevance_score": evidence.relevance_score,
        "novelty_score": evidence.novelty_score,
        "used_in_artifact": bool(evidence.used_in_artifact),
        "source": (
            {
                "id": str(source.id),
                "title": str(
                    source.title
                    or _humanize(source.source_type, fallback="Source")
                ),
                "url": _safe_external_url(source.uri),
            }
            if source
            else None
        ),
        "created_at": _iso(evidence.created_at),
    }


def _approval_summary(
    approval: RuntimeApprovalRequest,
) -> dict[str, Any]:
    user_id = int(current_user.id)
    can_resolve = bool(
        current_user.is_admin
        or (
            approval.approver_user_id is not None
            and int(approval.approver_user_id) == user_id
        )
        or (
            approval.approver_user_id is None
            and approval.requester_user_id is not None
            and int(approval.requester_user_id) == user_id
        )
    )
    return {
        "id": str(approval.id),
        "status": str(approval.status or "pending"),
        "subject_type": str(approval.subject_type or "run"),
        "subject_id": str(approval.subject_id or ""),
        "tool_id": str(approval.tool_id) if approval.tool_id else None,
        "effects": [
            _humanize(value, fallback="Effect")
            for value in (approval.effects or [])
        ],
        "preview": _semantic_value(approval.preview_payload or {}),
        "resolution_note": _semantic_value(approval.resolution_note or ""),
        "created_at": _iso(approval.created_at),
        "expires_at": _iso(approval.expires_at),
        "resolved_at": _iso(approval.resolved_at),
        "can_resolve": bool(
            approval.status == "pending" and can_resolve
        ),
        "resolve_url": f"/api/runtime/approvals/{approval.id}",
    }


def _interaction_summary(interaction: RuntimeInteraction) -> dict[str, Any]:
    return {
        "id": str(interaction.id),
        "status": str(interaction.status or "pending"),
        "type": str(interaction.interaction_type or "interaction"),
        "tool_name": (
            str(interaction.tool_name) if interaction.tool_name else None
        ),
        "request": _semantic_value(interaction.request_payload or {}),
        "response": _semantic_value(interaction.response_payload or {}),
        "version": int(interaction.version or 1),
        "created_at": _iso(interaction.created_at),
        "resolved_at": _iso(interaction.resolved_at),
        "can_resolve": bool(
            interaction.status == "pending"
            and int(interaction.requester_user_id) == int(current_user.id)
        ),
        "resolve_url": (
            f"/api/chat/interactions/{interaction.id}/resolve"
        ),
    }


def _run_directory_summary(
    run: WorkflowRun,
    *,
    steps: list[WorkflowStep],
    artifact_count: int,
    approval_count: int,
) -> dict[str, Any]:
    current_step = _current_step(run, steps)
    status = str(run.status or "draft")
    summary_value = (
        run.state_json
        or run.understanding_json
        or run.plan_json
        or run.metadata_json
        or {}
    )
    return {
        "id": str(run.id),
        "title": _run_title(run),
        "summary": _value_summary(_semantic_value(summary_value)),
        "kind": str(run.kind or "run"),
        "status": status,
        "status_label": _status_label(status),
        "active": status not in TERMINAL_RUN_STATUSES,
        "needs_attention": bool(
            status in {"failed", "paused", "waiting_for_approval"}
            or approval_count
        ),
        "current_step_label": (
            _step_title(current_step) if current_step else None
        ),
        "progress": _run_progress(run, steps),
        "artifact_count": artifact_count,
        "pending_approval_count": approval_count,
        "created_at": _iso(run.created_at),
        "updated_at": _iso(run.updated_at),
        "completed_at": _iso(run.completed_at),
        "open_url": f"/runs/{run.id}",
    }


def _schema_fields(schema: Any, payload: Any) -> list[dict[str, Any]]:
    schema_object = schema if isinstance(schema, dict) else {}
    properties = (
        schema_object.get("properties")
        if isinstance(schema_object.get("properties"), dict)
        else {}
    )
    payload_object = payload if isinstance(payload, dict) else {}
    required = {
        str(value) for value in (schema_object.get("required") or [])
    }
    ordered_keys = list(properties)
    ordered_keys.extend(
        key for key in payload_object if key not in properties
    )
    fields = []
    for key in ordered_keys[:120]:
        field_schema = (
            properties.get(key)
            if isinstance(properties.get(key), dict)
            else {}
        )
        value = _semantic_value(payload_object.get(key))
        fields.append(
            {
                "key": str(key),
                "label": str(
                    field_schema.get("title")
                    or _humanize(key, fallback="Field")
                ),
                "description": str(field_schema.get("description") or ""),
                "type": str(
                    field_schema.get("type")
                    or _value_kind(value)
                ),
                "format": (
                    str(field_schema.get("format"))
                    if field_schema.get("format")
                    else None
                ),
                "required": str(key) in required,
                "summary": _value_summary(value),
                "value": value,
            }
        )
    if not fields and payload not in (None, "", [], {}):
        semantic = _semantic_value(payload)
        fields.append(
            {
                "key": "value",
                "label": str(schema_object.get("title") or "Value"),
                "description": str(
                    schema_object.get("description") or ""
                ),
                "type": str(
                    schema_object.get("type") or _value_kind(semantic)
                ),
                "format": (
                    str(schema_object.get("format"))
                    if schema_object.get("format")
                    else None
                ),
                "required": True,
                "summary": _value_summary(semantic),
                "value": semantic,
            }
        )
    return fields


def _runtime_event(
    event: dict[str, Any],
    *,
    index: int,
    fallback_time: Any,
) -> dict[str, Any]:
    kind = str(event.get("kind") or event.get("type") or "event")
    title = str(
        event.get("title")
        or event.get("label")
        or _humanize(kind, fallback="Activity")
    )
    summary = _value_summary(
        event.get("summary")
        or event.get("message")
        or event.get("detail")
        or ""
    )
    progress = event.get("progress")
    try:
        normalized_progress = (
            max(0, min(100, round(float(progress))))
            if progress is not None
            else None
        )
    except (TypeError, ValueError):
        normalized_progress = None
    return {
        "id": str(event.get("id") or f"event-{index}"),
        "kind": kind,
        "title": title,
        "summary": summary,
        "status": str(event.get("status") or ""),
        "progress": normalized_progress,
        "occurred_at": str(
            event.get("occurred_at")
            or event.get("timestamp")
            or _iso(fallback_time)
            or ""
        )
        or None,
    }


def _artifact_detail_payload(
    artifact: RuntimeArtifact,
) -> dict[str, Any]:
    version_rows = (
        RuntimeArtifactVersion.query.filter_by(artifact_id=artifact.id)
        .order_by(
            RuntimeArtifactVersion.version_number.desc(),
            RuntimeArtifactVersion.created_at.desc(),
        )
        .limit(51)
        .all()
    )
    versions_truncated = len(version_rows) > 50
    visible_versions = version_rows[:50]
    current_version = next(
        (
            version
            for version in visible_versions
            if artifact.current_version_id
            and version.id == artifact.current_version_id
        ),
        visible_versions[0] if visible_versions else None,
    )
    evidence_rows = (
        RuntimeEvidenceRecord.query.filter_by(
            workflow_run_id=artifact.workflow_run_id
        )
        .filter(RuntimeEvidenceRecord.artifact_id == artifact.id)
        .order_by(RuntimeEvidenceRecord.created_at.desc())
        .limit(100)
        .all()
    )
    source_ids = {
        str(row.source_id) for row in evidence_rows if row.source_id
    }
    for version in visible_versions:
        source_ids.update(str(value) for value in (version.source_ids or []))
    source_rows = (
        RuntimeSourceRecord.query.filter(
            RuntimeSourceRecord.id.in_(source_ids)
        )
        .filter(
            RuntimeSourceRecord.workflow_run_id
            == artifact.workflow_run_id
        )
        .order_by(RuntimeSourceRecord.created_at.desc())
        .limit(100)
        .all()
        if source_ids
        else []
    )
    sources_by_id = {str(row.id): row for row in source_rows}
    summary = _artifact_summary(
        artifact,
        version_number=(
            int(current_version.version_number) if current_version else 0
        ),
    )
    return {
        "artifact": {
            **summary,
            "metadata": _semantic_value(artifact.metadata_json or {}),
            "review": {
                "status": str(artifact.review_status or "draft"),
                "note": _semantic_value(artifact.review_note or ""),
                "reviewed_at": _iso(artifact.reviewed_at),
                "reviewed_by_user_id": artifact.reviewed_by_user_id,
                "can_review": (
                    str(artifact.review_status or "draft")
                    in REVIEWABLE_ARTIFACT_STATUSES
                ),
            },
            "external_url": _safe_external_url(
                artifact.remote_reference or artifact.uri
            ),
            "content_url": (
                f"/api/runtime/artifacts/{artifact.id}/content"
                "?max_chars=100000"
            ),
        },
        "versions": [
            {
                "id": str(version.id),
                "version_number": int(version.version_number),
                "label": str(version.label or summary["label"]),
                "type": str(
                    version.artifact_type
                    or artifact.artifact_type
                    or "file"
                ),
                "mime_type": str(
                    version.mime_type
                    or artifact.mime_type
                    or "application/octet-stream"
                ),
                "size_bytes": (
                    int(version.size_bytes)
                    if version.size_bytes is not None
                    else None
                ),
                "change_summary": _semantic_value(
                    version.change_summary or ""
                ),
                "source_count": len(version.source_ids or []),
                "evidence_count": len(version.evidence_ids or []),
                "created_at": _iso(version.created_at),
                "current": bool(
                    (
                        artifact.current_version_id
                        and version.id == artifact.current_version_id
                    )
                    or (
                        not artifact.current_version_id
                        and current_version
                        and version.id == current_version.id
                    )
                ),
            }
            for version in visible_versions
        ],
        "versions_truncated": versions_truncated,
        "sources": [_source_summary(row) for row in source_rows],
        "evidence": [
            _evidence_summary(row, sources_by_id)
            for row in evidence_rows
        ],
    }


@frontend_run_blueprint.route("/api/frontend/runs", methods=["GET"])
@login_required
def list_frontend_runs():
    limit = _bounded_int(
        request.args.get("limit"),
        default=RUN_PAGE_DEFAULT_LIMIT,
        minimum=1,
        maximum=RUN_PAGE_MAX_LIMIT,
    )
    status = str(request.args.get("status") or "").strip().lower()
    query = _run_query()
    if status:
        query = query.filter(WorkflowRun.status == status)
    total = int(
        query.order_by(None)
        .with_entities(func.count(WorkflowRun.id))
        .scalar()
        or 0
    )
    rows = (
        query.order_by(
            WorkflowRun.updated_at.desc(),
            WorkflowRun.created_at.desc(),
        )
        .limit(limit)
        .all()
    )
    run_ids = [str(row.id) for row in rows]
    step_rows = (
        WorkflowStep.query.filter(WorkflowStep.workflow_run_id.in_(run_ids))
        .order_by(
            WorkflowStep.workflow_run_id.asc(),
            WorkflowStep.sequence.asc(),
            WorkflowStep.created_at.asc(),
        )
        .all()
        if run_ids
        else []
    )
    steps_by_run = {run_id: [] for run_id in run_ids}
    for step in step_rows:
        steps_by_run.setdefault(str(step.workflow_run_id), []).append(step)
    artifact_counts = {
        str(run_id): int(count or 0)
        for run_id, count in (
            db.session.query(
                RuntimeArtifact.workflow_run_id,
                func.count(RuntimeArtifact.id),
            )
            .filter(RuntimeArtifact.workflow_run_id.in_(run_ids))
            .filter(
                RuntimeArtifact.retention_policy != "chat_context_result"
            )
            .group_by(RuntimeArtifact.workflow_run_id)
            .all()
            if run_ids
            else []
        )
    }
    approval_counts = {
        str(run_id): int(count or 0)
        for run_id, count in (
            db.session.query(
                RuntimeApprovalRequest.workflow_run_id,
                func.count(RuntimeApprovalRequest.id),
            )
            .filter(RuntimeApprovalRequest.workflow_run_id.in_(run_ids))
            .filter(RuntimeApprovalRequest.status == "pending")
            .group_by(RuntimeApprovalRequest.workflow_run_id)
            .all()
            if run_ids
            else []
        )
    }
    response = jsonify(
        {
            "items": [
                _run_directory_summary(
                    run,
                    steps=steps_by_run.get(str(run.id), []),
                    artifact_count=artifact_counts.get(str(run.id), 0),
                    approval_count=approval_counts.get(str(run.id), 0),
                )
                for run in rows
            ],
            "total": total,
        }
    )
    response.headers["Cache-Control"] = "private, no-cache"
    return response


@frontend_run_blueprint.route(
    "/api/frontend/runs/<run_id>/studio",
    methods=["GET"],
)
@login_required
def get_frontend_run_studio(run_id: str):
    run = _visible_run(run_id)
    if run is None:
        return jsonify({"error": "Run not found."}), 404

    steps = (
        WorkflowStep.query.filter_by(workflow_run_id=run.id)
        .order_by(
            WorkflowStep.sequence.asc(),
            WorkflowStep.created_at.asc(),
        )
        .limit(STUDIO_RECORD_LIMIT)
        .all()
    )
    steps_by_id = {str(step.id): step for step in steps}
    steps_by_key = {
        str(step.step_key): step for step in steps if step.step_key
    }
    jobs = (
        RuntimeJob.query.filter_by(workflow_run_id=run.id)
        .order_by(RuntimeJob.queued_at.asc())
        .limit(STUDIO_RECORD_LIMIT)
        .all()
    )
    jobs_by_id = {str(job.id): job for job in jobs}
    jobs_by_source: dict[str, list[RuntimeJob]] = {}
    for job in jobs:
        if job.source_id:
            jobs_by_source.setdefault(str(job.source_id), []).append(job)
    dependencies_by_step = {
        str(step.id): _step_dependencies(
            step,
            steps_by_id=steps_by_id,
            steps_by_key=steps_by_key,
        )
        for step in steps
    }
    step_jobs = {
        str(step.id): _jobs_for_step(
            step,
            jobs_by_id=jobs_by_id,
            jobs_by_source=jobs_by_source,
        )
        for step in steps
    }
    step_summaries = [
        _step_summary(
            step,
            dependencies=dependencies_by_step[str(step.id)],
            jobs=step_jobs[str(step.id)],
        )
        for step in steps
    ]
    edges = [
        {
            "id": f"{source_id}:{step.id}",
            "source": source_id,
            "target": str(step.id),
        }
        for step in steps
        for source_id in dependencies_by_step[str(step.id)]
    ]

    artifacts = (
        RuntimeArtifact.query.filter_by(workflow_run_id=run.id)
        .filter(RuntimeArtifact.retention_policy != "chat_context_result")
        .order_by(RuntimeArtifact.updated_at.desc())
        .limit(STUDIO_RECORD_LIMIT)
        .all()
    )
    version_numbers = _artifact_version_numbers(
        [str(row.id) for row in artifacts]
    )
    source_rows = (
        RuntimeSourceRecord.query.filter_by(workflow_run_id=run.id)
        .order_by(RuntimeSourceRecord.created_at.desc())
        .limit(STUDIO_RECORD_LIMIT)
        .all()
    )
    sources_by_id = {str(row.id): row for row in source_rows}
    evidence_rows = (
        RuntimeEvidenceRecord.query.filter_by(workflow_run_id=run.id)
        .order_by(RuntimeEvidenceRecord.created_at.desc())
        .limit(STUDIO_RECORD_LIMIT)
        .all()
    )
    approval_rows = (
        RuntimeApprovalRequest.query.filter_by(workflow_run_id=run.id)
        .order_by(RuntimeApprovalRequest.created_at.desc())
        .limit(200)
        .all()
    )
    interaction_rows = (
        RuntimeInteraction.query.filter_by(workflow_run_id=run.id)
        .order_by(RuntimeInteraction.created_at.desc())
        .limit(200)
        .all()
    )
    current_step = _current_step(run, steps)
    started_values = [
        step.started_at for step in steps if step.started_at
    ]
    started_at = min(started_values) if started_values else None
    progress = _run_progress(run, steps)
    status = str(run.status or "draft")
    pending_approvals = sum(
        1 for row in approval_rows if row.status == "pending"
    )

    response = jsonify(
        {
            "revision": (
                f"{run.updated_at.isoformat() if run.updated_at else ''}:"
                f"{len(steps)}:{len(artifacts)}"
            ),
            "server_time": utc_iso(),
            "run": {
                "id": str(run.id),
                "title": _run_title(run),
                "summary": _value_summary(
                    _semantic_value(
                        run.state_json
                        or run.understanding_json
                        or run.plan_json
                        or {}
                    )
                ),
                "kind": str(run.kind or "run"),
                "workflow_id": (
                    str(run.workflow_id) if run.workflow_id else None
                ),
                "source_addon_id": (
                    str(run.source_addon_id)
                    if run.source_addon_id
                    else None
                ),
                "status": status,
                "status_label": _status_label(status),
                "needs_attention": bool(
                    status in {
                        "failed",
                        "paused",
                        "waiting_for_approval",
                    }
                    or pending_approvals
                ),
                "progress": progress,
                "current_step_id": (
                    str(current_step.id) if current_step else None
                ),
                "current_step_label": (
                    _step_title(current_step) if current_step else None
                ),
                "created_at": _iso(run.created_at),
                "started_at": _iso(started_at),
                "updated_at": _iso(run.updated_at),
                "completed_at": _iso(run.completed_at),
                "duration_ms": _duration_ms(
                    started_at or run.created_at,
                    run.completed_at
                    if status in TERMINAL_RUN_STATUSES
                    else None,
                ),
                "cost": _cost_summary(str(run.id)),
                "controls": _run_controls(run),
                "counts": {
                    "steps": len(steps),
                    "artifacts": len(artifacts),
                    "sources": len(source_rows),
                    "evidence": len(evidence_rows),
                    "approvals": len(approval_rows),
                    "pending_approvals": pending_approvals,
                    "interactions": len(interaction_rows),
                },
            },
            "documents": {
                "understanding": _document_sections(
                    run.understanding_json or {}
                ),
                "plan": _document_sections(run.plan_json or {}),
                "state": _document_sections(run.state_json or {}),
            },
            "graph": {
                "nodes": step_summaries,
                "edges": edges,
                "large": len(steps) >= 60,
                "truncated": len(steps) >= STUDIO_RECORD_LIMIT,
                "layout_direction": "RIGHT",
            },
            "timeline": step_summaries,
            "artifacts": [
                _artifact_summary(
                    row,
                    version_number=version_numbers.get(str(row.id), 0),
                )
                for row in artifacts
            ],
            "sources": [_source_summary(row) for row in source_rows],
            "evidence": [
                _evidence_summary(row, sources_by_id)
                for row in evidence_rows
            ],
            "approvals": [
                _approval_summary(row) for row in approval_rows
            ],
            "interactions": [
                _interaction_summary(row) for row in interaction_rows
            ],
            "selected_step_id": (
                str(current_step.id)
                if current_step
                else (str(steps[0].id) if steps else None)
            ),
            "developer_url": f"/api/frontend/runs/{run.id}/developer",
        }
    )
    response.headers["Cache-Control"] = "private, no-store"
    return response


@frontend_run_blueprint.route(
    "/api/frontend/runs/<run_id>/steps/<step_id>",
    methods=["GET"],
)
@login_required
def get_frontend_run_step(run_id: str, step_id: str):
    run = _visible_run(run_id)
    if run is None:
        return jsonify({"error": "Run not found."}), 404
    step = (
        WorkflowStep.query.filter_by(
            id=step_id,
            workflow_run_id=run.id,
        ).first()
    )
    if step is None:
        return jsonify({"error": "Step not found."}), 404

    steps = WorkflowStep.query.filter_by(workflow_run_id=run.id).all()
    steps_by_id = {str(row.id): row for row in steps}
    steps_by_key = {
        str(row.step_key): row for row in steps if row.step_key
    }
    dependencies = _step_dependencies(
        step,
        steps_by_id=steps_by_id,
        steps_by_key=steps_by_key,
    )
    jobs = (
        RuntimeJob.query.filter_by(workflow_run_id=run.id)
        .order_by(RuntimeJob.queued_at.asc())
        .limit(STUDIO_RECORD_LIMIT)
        .all()
    )
    jobs_by_id = {str(job.id): job for job in jobs}
    jobs_by_source: dict[str, list[RuntimeJob]] = {}
    for job in jobs:
        if job.source_id:
            jobs_by_source.setdefault(str(job.source_id), []).append(job)
    step_jobs = _jobs_for_step(
        step,
        jobs_by_id=jobs_by_id,
        jobs_by_source=jobs_by_source,
    )
    resource_ids = {str(value) for value in step.resource_reference_ids or []}
    resources = (
        RuntimeResourceReference.query.filter(
            RuntimeResourceReference.id.in_(resource_ids)
        )
        .limit(120)
        .all()
        if resource_ids
        else []
    )
    resources = [
        resource
        for resource in resources
        if visible_to_user(
            resource,
            int(current_user.id),
            bool(current_user.is_admin),
        )
    ]

    artifact_ids = {str(value) for value in step.artifact_ids or []}
    artifact_ids.update(
        str(value)
        for (value,) in (
            db.session.query(RuntimeArtifactVersion.artifact_id)
            .filter(RuntimeArtifactVersion.step_id == step.id)
            .distinct()
            .all()
        )
        if value
    )
    artifacts = (
        RuntimeArtifact.query.filter(
            RuntimeArtifact.id.in_(artifact_ids)
        )
        .filter(RuntimeArtifact.workflow_run_id == run.id)
        .order_by(RuntimeArtifact.updated_at.desc())
        .limit(120)
        .all()
        if artifact_ids
        else []
    )
    version_numbers = _artifact_version_numbers(
        [str(row.id) for row in artifacts]
    )
    metadata = step.metadata_json if isinstance(step.metadata_json, dict) else {}
    raw_events = (
        metadata.get("runtime_events")
        if isinstance(metadata.get("runtime_events"), list)
        else []
    )
    events = [
        _runtime_event(
            event,
            index=index,
            fallback_time=step.updated_at,
        )
        for index, event in enumerate(raw_events[-100:])
        if isinstance(event, dict)
    ]
    events.extend(
        {
            "id": f"job:{job.id}",
            "kind": "job",
            "title": str(
                job.task_name
                or _humanize(job.source_type, fallback="Runtime job")
            ),
            "summary": _value_summary(job.message or ""),
            "status": str(job.status or "queued"),
            "progress": (
                max(0, min(100, round(float(job.progress))))
                if job.progress is not None
                else None
            ),
            "occurred_at": _iso(
                job.completed_at or job.started_at or job.queued_at
            ),
        }
        for job in step_jobs
    )
    events.sort(
        key=lambda row: str(row.get("occurred_at") or ""),
    )
    log_source_ids = list(
        dict.fromkeys(
            [
                *[str(value) for value in (step.log_source_ids or [])],
                *[
                    str(value)
                    for job in step_jobs
                    for value in (job.log_source_ids or [])
                ],
            ]
        )
    )
    error = str(
        step.error
        or next(
            (job.error for job in reversed(step_jobs) if job.error),
            "",
        )
        or ""
    )
    parent_outputs = [
        {
            "step_id": str(parent.id),
            "title": _step_title(parent),
            "summary": _value_summary(
                _semantic_value(parent.output_payload or {})
            ),
            "value": _semantic_value(parent.output_payload or {}),
        }
        for dependency_id in dependencies
        if (parent := steps_by_id.get(dependency_id))
        and parent.output_payload not in (None, "", [], {})
    ]
    step_summary = _step_summary(
        step,
        dependencies=dependencies,
        jobs=step_jobs,
    )
    response = jsonify(
        {
            "step": {
                **step_summary,
                "instructions": _semantic_value(step.instructions or ""),
                "capabilities": [
                    str(value) for value in (step.capability_ids or [])
                ],
                "skills": [str(value) for value in (step.skill_ids or [])],
                "tools": [str(value) for value in (step.tool_ids or [])],
                "providers": _semantic_value(
                    step.provider_requirements or []
                ),
                "can_rerun": step.status in TERMINAL_STEP_STATUSES,
                "rerun_url": f"/api/runtime/steps/{step.id}/rerun",
            },
            "input": {
                "summary": _value_summary(
                    _semantic_value(step.input_payload or {})
                ),
                "fields": _schema_fields(
                    step.input_schema,
                    step.input_payload,
                ),
                "resources": [
                    {
                        "id": str(resource.id),
                        "label": str(
                            resource.label
                            or _humanize(
                                resource.resource_type,
                                fallback="Resource",
                            )
                        ),
                        "type": str(resource.resource_type or "resource"),
                        "provider": (
                            str(resource.provider)
                            if resource.provider
                            else None
                        ),
                        "permissions": [
                            str(value)
                            for value in (resource.permissions or ["read"])
                        ],
                    }
                    for resource in resources
                ],
                "parent_outputs": parent_outputs,
            },
            "output": {
                "summary": _value_summary(
                    _semantic_value(step.output_payload or {})
                ),
                "fields": _schema_fields(
                    step.output_schema,
                    step.output_payload,
                ),
                "artifacts": [
                    _artifact_summary(
                        row,
                        version_number=version_numbers.get(
                            str(row.id),
                            0,
                        ),
                    )
                    for row in artifacts
                ],
            },
            "activity": events,
            "problems": {
                "has_problem": bool(error),
                "summary": _value_summary(error) if error else "",
                "guidance": (
                    "Review the inputs and activity, then rerun this step "
                    "when the underlying issue is resolved."
                    if error
                    else ""
                ),
                "log_links": attach_runtime_log_link_ids(log_source_ids),
            },
            "developer_url": (
                f"/api/frontend/runs/{run.id}/developer"
                f"?step_id={step.id}"
            ),
        }
    )
    response.headers["Cache-Control"] = "private, no-store"
    return response


@frontend_run_blueprint.route(
    "/api/frontend/runs/<run_id>/artifacts/<artifact_id>",
    methods=["GET"],
)
@login_required
def get_frontend_run_artifact(run_id: str, artifact_id: str):
    run = _visible_run(run_id)
    if run is None:
        return jsonify({"error": "Run not found."}), 404
    artifact = RuntimeArtifact.query.filter_by(
        id=artifact_id,
        workflow_run_id=run.id,
    ).first()
    if artifact is None or artifact.retention_policy == "chat_context_result":
        return jsonify({"error": "Artifact not found."}), 404
    response = jsonify(_artifact_detail_payload(artifact))
    response.headers["Cache-Control"] = "private, no-store"
    return response


@frontend_run_blueprint.route(
    "/api/frontend/runs/<run_id>/developer",
    methods=["GET"],
)
@login_required
def get_frontend_run_developer(run_id: str):
    run = _visible_run(run_id)
    if run is None:
        return jsonify({"error": "Run not found."}), 404
    step_id = str(request.args.get("step_id") or "").strip()
    step_query = WorkflowStep.query.filter_by(workflow_run_id=run.id)
    if step_id:
        step_query = step_query.filter(WorkflowStep.id == step_id)
    steps = (
        step_query.order_by(
            WorkflowStep.sequence.asc(),
            WorkflowStep.created_at.asc(),
        )
        .limit(STUDIO_RECORD_LIMIT)
        .all()
    )
    jobs = (
        RuntimeJob.query.filter_by(workflow_run_id=run.id)
        .order_by(RuntimeJob.queued_at.asc())
        .limit(STUDIO_RECORD_LIMIT)
        .all()
    )
    payload = {
        "run": _semantic_value(
            serialize_workflow_run(run, load_run_projection(run)),
            max_items=160,
            max_string_chars=12_000,
        ),
        "steps": [
            _semantic_value(
                step.to_dict(),
                max_items=160,
                max_string_chars=12_000,
            )
            for step in steps
        ],
        "jobs": [
            _semantic_value(
                job.to_dict(),
                max_items=120,
                max_string_chars=8_000,
            )
            for job in jobs
        ],
        "notice": (
            "Sensitive credential-shaped fields remain redacted from this "
            "developer payload."
        ),
    }
    response = jsonify(payload)
    response.headers["Cache-Control"] = "private, no-store"
    return response


def register_frontend_run_blueprint(app):
    app.register_blueprint(frontend_run_blueprint)


__all__ = [
    "frontend_run_blueprint",
    "register_frontend_run_blueprint",
]
