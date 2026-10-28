from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from flask import Blueprint, current_app, jsonify, request
from flask_login import current_user, login_required
from sqlalchemy import false, func, or_

from iarf_assistant.context_frontend import (
    _humanize,
    _semantic_value,
)
from iarf_assistant.core.frontend_contracts import (
    FRONTEND_API_VERSION,
    set_frontend_api_version_headers,
)
from iarf_assistant.core.runtime_state import attach_runtime_log_link_ids
from iarf_assistant.extensions import db
from iarf_assistant.models import (
    RuntimeApprovalRequest,
    RuntimeJob,
    WorkflowRun,
)

frontend_runtime_operations_blueprint = Blueprint(
    "frontend_runtime_operations_api",
    __name__,
)

MAX_OPERATION_APPROVALS = 80
MAX_OPERATION_JOBS = 80
ACTIVE_JOB_STATUSES = {
    "queued",
    "running",
    "waiting_for_approval",
    "paused",
}


def _iso(value: Any) -> str | None:
    if not value or not hasattr(value, "isoformat"):
        return None
    rendered = value.isoformat()
    if isinstance(value, datetime) and value.tzinfo is None:
        return f"{rendered}Z"
    return rendered


def _safe_href(value: Any) -> str | None:
    raw = str(value or "").strip()
    if not raw or any(character in raw for character in ("\r", "\n", "\\")):
        return None
    if raw.startswith("/") and not raw.startswith("//"):
        return raw
    parsed = urlsplit(raw)
    if (
        parsed.scheme in {"http", "https"}
        and parsed.netloc
        and not parsed.username
        and not parsed.password
    ):
        return raw
    return None


def _clip_text(value: Any, *, maximum: int = 420) -> str:
    text = " ".join(str(value or "").strip().split())
    if len(text) <= maximum:
        return text
    return f"{text[: max(0, maximum - 1)].rstrip()}…"


def _runtime_query_for_user(model):
    query = model.query
    if bool(getattr(current_user, "is_admin", False)):
        return query

    user_id = int(current_user.id)
    filters = []
    for attribute in (
        "created_by_user_id",
        "owner_user_id",
        "requester_user_id",
        "approver_user_id",
    ):
        if hasattr(model, attribute):
            filters.append(getattr(model, attribute) == user_id)
    if hasattr(model, "workflow_run_id"):
        visible_run_ids = db.session.query(WorkflowRun.id).filter(
            WorkflowRun.created_by_user_id == user_id
        )
        filters.append(model.workflow_run_id.in_(visible_run_ids))
    if not filters:
        return query.filter(false())
    return query.filter(or_(*filters))


def _status_counts(query, status_column) -> dict[str, int]:
    rows = (
        query.with_entities(status_column, func.count()).group_by(status_column).all()
    )
    return {str(status or "unknown"): int(count or 0) for status, count in rows}


def _visible_runs(
    records: Iterable[RuntimeApprovalRequest | RuntimeJob],
) -> dict[str, WorkflowRun]:
    run_ids = {
        str(record.workflow_run_id) for record in records if record.workflow_run_id
    }
    if not run_ids:
        return {}
    query = WorkflowRun.query.filter(WorkflowRun.id.in_(run_ids))
    if not bool(getattr(current_user, "is_admin", False)):
        query = query.filter(WorkflowRun.created_by_user_id == int(current_user.id))
    return {str(run.id): run for run in query.all()}


def _run_title(run: WorkflowRun) -> str:
    return (
        str(run.title or "").strip()
        or _humanize(run.workflow_id, fallback="")
        or _humanize(run.kind, fallback="Run")
    )


def _run_link(
    workflow_run_id: str | None,
    runs_by_id: dict[str, WorkflowRun],
) -> dict[str, str] | None:
    if not workflow_run_id:
        return None
    run = runs_by_id.get(str(workflow_run_id))
    if not run:
        return None
    return {
        "id": str(run.id),
        "title": _run_title(run),
        "url": f"/runs/{quote(str(run.id), safe='')}",
    }


def _semantic_log_links(source_ids: Any) -> list[dict[str, str]]:
    links = attach_runtime_log_link_ids(source_ids)
    payload = []
    for link in links:
        url = _safe_href(link.get("url"))
        if not url:
            continue
        payload.append(
            {
                "id": str(link.get("id") or ""),
                "name": str(link.get("name") or "Logs"),
                "category": str(link.get("category") or "runtime"),
                "url": url,
            }
        )
    return payload


def _approval_payload(
    approval: RuntimeApprovalRequest,
    *,
    runs_by_id: dict[str, WorkflowRun],
) -> dict[str, Any]:
    user_id = int(current_user.id)
    is_admin = bool(getattr(current_user, "is_admin", False))
    requester = (
        approval.requester_user_id is not None
        and int(approval.requester_user_id) == user_id
    )
    assigned_approver = (
        approval.approver_user_id is not None
        and int(approval.approver_user_id) == user_id
    )
    unassigned_requester = approval.approver_user_id is None and requester
    pending = str(approval.status or "pending") == "pending"
    can_decide = pending and (is_admin or assigned_approver or unassigned_requester)
    run = _run_link(approval.workflow_run_id, runs_by_id)
    effects = [
        _humanize(value, fallback="Effect")
        for value in (approval.effects or [])
        if str(value or "").strip()
    ]
    subject_label = _humanize(
        approval.subject_type,
        fallback="Approval request",
    )
    tool_label = _humanize(approval.tool_id, fallback="") if approval.tool_id else ""
    title = run["title"] if run else tool_label or subject_label
    summary = (
        f"May affect {', '.join(effects[:3])}."
        if effects
        else f"Review this {subject_label.lower()} before it continues."
    )
    return {
        "id": str(approval.id),
        "title": title,
        "summary": summary,
        "status": str(approval.status or "pending"),
        "status_label": _humanize(
            approval.status,
            fallback="Pending",
        ),
        "subject_label": subject_label,
        "tool_label": tool_label or None,
        "effects": effects,
        "preview": _semantic_value(approval.preview_payload or {}),
        "resolution_note": _semantic_value(approval.resolution_note or ""),
        "created_at": _iso(approval.created_at),
        "expires_at": _iso(approval.expires_at),
        "resolved_at": _iso(approval.resolved_at),
        "can_approve": bool(can_decide),
        "can_reject": bool(can_decide),
        "can_cancel": bool(pending and (is_admin or requester)),
        "resolve_url": f"/api/runtime/approvals/{quote(str(approval.id), safe='')}",
        "run": run,
        "log_links": _semantic_log_links(
            approval.log_source_ids
            or ([approval.audit_log_source_id] if approval.audit_log_source_id else [])
        ),
    }


def _normalized_progress(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return max(0, min(100, round(float(value))))
    except TypeError, ValueError:
        return None


def _job_payload(
    job: RuntimeJob,
    *,
    runs_by_id: dict[str, WorkflowRun],
) -> dict[str, Any]:
    status = str(job.status or "queued")
    source_label = _humanize(
        job.source_addon_id or job.source_type,
        fallback="Runtime",
    )
    title = (
        _humanize(job.task_name, fallback="")
        or _humanize(job.source_id, fallback="")
        or f"{source_label} job"
    )
    message = _clip_text(job.error if status == "failed" else job.message)
    if not message:
        message = (
            "This job needs attention."
            if status in {"failed", "waiting_for_approval", "paused"}
            else f"{_humanize(status, fallback='Queued')} in {source_label}."
        )
    return {
        "id": str(job.id),
        "title": title,
        "summary": message,
        "source_label": source_label,
        "status": status,
        "status_label": _humanize(status, fallback="Queued"),
        "progress": _normalized_progress(job.progress),
        "active": status in ACTIVE_JOB_STATUSES,
        "needs_attention": status in {"failed", "waiting_for_approval", "paused"},
        "queued_at": _iso(job.queued_at),
        "started_at": _iso(job.started_at),
        "completed_at": _iso(job.completed_at),
        "updated_at": _iso(job.updated_at),
        "run": _run_link(job.workflow_run_id, runs_by_id),
        "log_links": _semantic_log_links(job.log_source_ids),
    }


def _manifest_record(manager: Any, addon_id: str) -> dict[str, Any]:
    value = (getattr(manager, "manifests", None) or {}).get(addon_id) or {}
    manifest = value.get("manifest") if isinstance(value, dict) else {}
    if not isinstance(manifest, dict):
        manifest = value if isinstance(value, dict) else {}
    return manifest


def _registered_log_link(source_id: str | None) -> dict[str, str] | None:
    if not source_id:
        return None
    registry = getattr(current_app, "log_registry", None)
    if not registry:
        return None
    try:
        link = registry.get_link_for_source(source_id)
    except KeyError:
        return None
    url = _safe_href(link.get("url"))
    if not url:
        return None
    return {
        "id": str(link.get("id") or source_id),
        "name": str(link.get("name") or "Logs"),
        "category": str(link.get("category") or "runtime"),
        "url": url,
    }


def _worker_payloads(manager: Any) -> list[dict[str, Any]]:
    payloads = []
    manifests = getattr(manager, "manifests", None) or {}
    for addon_id in sorted(manifests):
        try:
            state = manager.get_worker_state(addon_id) or {}
        except Exception:
            current_app.logger.exception(
                "Could not read worker state for addon '%s'.",
                addon_id,
            )
            continue
        configured = int(state.get("configured") or 0)
        running = int(state.get("running") or 0)
        if not configured and not running:
            continue
        manifest = _manifest_record(manager, addon_id)
        desired = state.get("desired")
        desired_count = (
            int(desired)
            if isinstance(desired, (int, float)) and int(desired) > 0
            else configured
        )
        healthy = bool(running and (not desired_count or running >= desired_count))
        entrypoint = str(state.get("entrypoint") or "").strip()
        log_link = _registered_log_link(state.get("log_source_id"))
        encoded_addon_id = quote(str(addon_id), safe="")
        payloads.append(
            {
                "id": str(addon_id),
                "name": str(manifest.get("name") or _humanize(addon_id)),
                "status": str(state.get("status") or "configured"),
                "status_label": (
                    "Running"
                    if healthy
                    else "Needs attention"
                    if configured
                    else "Stopped"
                ),
                "configured": configured,
                "running": running,
                "desired": desired_count,
                "healthy": healthy,
                "entrypoint": Path(entrypoint).name if entrypoint else None,
                "environment": _humanize(
                    state.get("execution_environment"),
                    fallback="Application",
                ),
                "pids": [
                    {
                        "value": int(pid),
                        "terminate_url": (
                            f"/api/addons/{encoded_addon_id}/workers/"
                            f"{int(pid)}/terminate"
                        ),
                    }
                    for pid in (state.get("pids") or [])
                    if isinstance(pid, int) or str(pid).isdigit()
                ],
                "stop_url": (f"/api/addons/{encoded_addon_id}/workers/stop"),
                "restart_url": (f"/api/addons/{encoded_addon_id}/workers/restart"),
                "log_link": log_link,
            }
        )
    return sorted(
        payloads,
        key=lambda item: (
            item["healthy"],
            item["name"].lower(),
        ),
    )


def _schedule_payloads(manager: Any) -> list[dict[str, Any]]:
    payloads = []
    manifests = getattr(manager, "manifests", None) or {}
    for addon_id in sorted(manifests):
        try:
            state = manager.get_scheduled_job_state(addon_id) or {}
        except Exception:
            current_app.logger.exception(
                "Could not read scheduled job state for addon '%s'.",
                addon_id,
            )
            continue
        manifest = _manifest_record(manager, addon_id)
        addon_name = str(manifest.get("name") or _humanize(addon_id))
        log_link = _registered_log_link(state.get("log_source_id"))
        encoded_addon_id = quote(str(addon_id), safe="")
        for job in state.get("jobs") or []:
            if not isinstance(job, dict) or not job.get("id"):
                continue
            job_id = str(job["id"])
            encoded_job_id = quote(job_id, safe="")
            base_url = f"/api/addons/{encoded_addon_id}/jobs/{encoded_job_id}"
            paused = bool(job.get("paused"))
            payloads.append(
                {
                    "id": f"{addon_id}:{job_id}",
                    "job_id": job_id,
                    "title": _humanize(
                        job.get("name") or job_id,
                        fallback="Scheduled job",
                    ),
                    "addon_id": str(addon_id),
                    "addon_name": addon_name,
                    "status": "paused" if paused else "scheduled",
                    "status_label": "Paused" if paused else "Scheduled",
                    "trigger": _clip_text(job.get("trigger"), maximum=180),
                    "next_run_at": job.get("next_run_at"),
                    "paused": paused,
                    "pending": bool(job.get("pending")),
                    "pause_url": f"{base_url}/pause",
                    "resume_url": f"{base_url}/resume",
                    "trigger_url": f"{base_url}/trigger",
                    "log_link": log_link,
                }
            )
    return sorted(
        payloads,
        key=lambda item: (
            item["paused"],
            item["next_run_at"] or "9999",
            item["title"].lower(),
        ),
    )


def _docker_log_links() -> list[dict[str, str]]:
    registry = getattr(current_app, "log_registry", None)
    if not registry:
        return []
    try:
        links = registry.get_links_by_category("docker")
    except KeyError, OSError, RuntimeError, TypeError, ValueError:
        return []
    payloads = []
    for link in links:
        url = _safe_href(link.get("url"))
        if url:
            payloads.append(
                {
                    "id": str(link.get("id") or ""),
                    "name": str(link.get("name") or "Docker logs"),
                    "category": "docker",
                    "url": url,
                }
            )
    return payloads


def _docker_payload(
    manager: Any,
    *,
    refresh: bool,
) -> dict[str, Any]:
    if refresh and hasattr(manager, "_refresh_docker_status"):
        try:
            manager._refresh_docker_status(log_failure=False)
        except Exception:
            current_app.logger.exception(
                "Could not refresh Docker availability for Operations."
            )

    available = bool(
        getattr(manager, "docker_available", False)
        and getattr(manager, "docker_client", None)
    )
    payload: dict[str, Any] = {
        "available": available,
        "status": "available" if available else "unavailable",
        "message": (
            "Docker Engine is available for addon-managed services."
            if available
            else (
                "Docker Engine is unavailable on this host. "
                "Application workers and schedules continue independently."
            )
        ),
        "containers": [],
        "log_links": _docker_log_links(),
    }
    if not available:
        return payload

    try:
        containers = manager.docker_client.containers.list(all=True)
    except Exception:
        current_app.logger.exception("Could not list addon-managed Docker containers.")
        payload["status"] = "degraded"
        payload["message"] = (
            "Docker Engine responded, but its addon containers could not be read."
        )
        return payload

    result = []
    for container in containers:
        identity = manager.identify_container_resource(container)
        if identity is None:
            continue
        name = str(getattr(container, "name", "") or "")
        short_id = str(
            getattr(container, "short_id", None) or getattr(container, "id", "")
        )
        if not short_id:
            continue
        status = str(getattr(container, "status", "unknown") or "unknown")
        image = getattr(container, "image", None)
        tags = list(getattr(image, "tags", None) or [])
        image_label = str(tags[0]) if tags else "Managed addon image"
        encoded_id = quote(short_id, safe="")
        result.append(
            {
                "id": short_id,
                "name": name,
                "status": status,
                "status_label": _humanize(status, fallback="Unknown"),
                "image": image_label,
                "created_at": (
                    (getattr(container, "attrs", None) or {}).get("Created")
                ),
                "action_url": (f"/api/system/docker/containers/{encoded_id}"),
                "log_links": [],
                "addon_id": str(identity["addon_id"]),
                "service_name": str(identity["service_name"]),
                "legacy": bool(identity.get("legacy")),
            }
        )
    payload["containers"] = sorted(
        result,
        key=lambda item: item["name"].lower(),
    )
    return payload


@frontend_runtime_operations_blueprint.after_request
def _frontend_runtime_operations_headers(response):
    set_frontend_api_version_headers(response)
    response.headers["Cache-Control"] = "private, no-cache"
    return response


@frontend_runtime_operations_blueprint.route(
    "/api/frontend/runtime/operations",
    methods=["GET"],
)
@login_required
def get_frontend_runtime_operations():
    approval_query = _runtime_query_for_user(RuntimeApprovalRequest)
    job_query = _runtime_query_for_user(RuntimeJob)

    approval_counts = _status_counts(
        approval_query,
        RuntimeApprovalRequest.status,
    )
    job_counts = _status_counts(job_query, RuntimeJob.status)

    approvals = (
        approval_query.order_by(RuntimeApprovalRequest.created_at.desc())
        .limit(MAX_OPERATION_APPROVALS)
        .all()
    )
    jobs = (
        job_query.order_by(RuntimeJob.updated_at.desc()).limit(MAX_OPERATION_JOBS).all()
    )
    runs_by_id = _visible_runs([*approvals, *jobs])

    is_admin = bool(getattr(current_user, "is_admin", False))
    workers: list[dict[str, Any]] = []
    schedules: list[dict[str, Any]] = []
    docker: dict[str, Any] | None = None
    manager = getattr(current_app, "addon_manager", None)
    if is_admin and manager:
        workers = _worker_payloads(manager)
        schedules = _schedule_payloads(manager)
        docker = _docker_payload(
            manager,
            refresh=request.args.get("refresh_docker") == "1",
        )
    elif is_admin:
        docker = {
            "available": False,
            "status": "unavailable",
            "message": "Runtime services are not initialized.",
            "containers": [],
            "log_links": [],
        }

    active_jobs = sum(job_counts.get(status, 0) for status in ACTIVE_JOB_STATUSES)
    failed_jobs = job_counts.get("failed", 0)
    pending_approvals = approval_counts.get("pending", 0)
    unhealthy_workers = sum(1 for worker in workers if not worker["healthy"])
    attention_jobs = sum(
        1
        for job in jobs
        if str(job.status or "") in {"failed", "waiting_for_approval", "paused"}
    )
    container_count = len((docker or {}).get("containers") or [])
    generated_at = (
        datetime.now(UTC)
        .isoformat()
        .replace(
            "+00:00",
            "Z",
        )
    )
    return jsonify(
        {
            "frontend_api_version": FRONTEND_API_VERSION,
            "generated_at": generated_at,
            "permissions": {
                "administer_system": is_admin,
            },
            "available_sections": [
                "overview",
                "approvals",
                "jobs",
                *(["workers", "schedules", "containers"] if is_admin else []),
            ],
            "summary": {
                "pending_approvals": pending_approvals,
                "active_jobs": active_jobs,
                "failed_jobs": failed_jobs,
                "attention_jobs": attention_jobs,
                "jobs_total": sum(job_counts.values()),
                "worker_processes": sum(worker["running"] for worker in workers),
                "worker_addons": len(workers),
                "unhealthy_workers": unhealthy_workers,
                "schedules": len(schedules),
                "containers": container_count,
                "attention": (pending_approvals + attention_jobs + unhealthy_workers),
            },
            "counts": {
                "approvals": approval_counts,
                "jobs": job_counts,
            },
            "approvals": [
                _approval_payload(approval, runs_by_id=runs_by_id)
                for approval in approvals
            ],
            "jobs": [_job_payload(job, runs_by_id=runs_by_id) for job in jobs],
            "workers": workers,
            "schedules": schedules,
            "docker": docker,
            "limits": {
                "approvals": MAX_OPERATION_APPROVALS,
                "jobs": MAX_OPERATION_JOBS,
            },
        }
    )


def register_frontend_runtime_operations_blueprint(app) -> None:
    app.register_blueprint(frontend_runtime_operations_blueprint)


__all__ = [
    "ACTIVE_JOB_STATUSES",
    "MAX_OPERATION_APPROVALS",
    "MAX_OPERATION_JOBS",
    "frontend_runtime_operations_blueprint",
    "register_frontend_runtime_operations_blueprint",
]
