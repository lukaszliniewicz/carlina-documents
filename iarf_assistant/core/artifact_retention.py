"""Retention policy for internal, externalized runtime payload artifacts."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import and_, or_

from carlina.core.utc import utc_now_db
from iarf_assistant.extensions import db
from iarf_assistant.models import RuntimeArtifact, RuntimeArtifactVersion, WorkflowRun


DEFAULT_RETENTION_DAYS = {
    "runtime_context_result": 30,
    "chat_context_result": 90,
}
TERMINAL_RUN_STATUSES = {"completed", "failed", "cancelled"}


def _artifact_path(record: Any, instance_path: str | Path) -> Path | None:
    uri = str(record.uri or "")
    if not uri.startswith("instance://"):
        return None
    root = Path(instance_path).resolve()
    candidate = (root / uri.removeprefix("instance://").lstrip("/\\")).resolve()
    candidate.relative_to(root)
    return candidate


def prune_expired_runtime_artifacts(
    *,
    instance_path: str | Path,
    now: datetime | None = None,
    retention_days: dict[str, int] | None = None,
    limit: int = 500,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Delete only explicitly ephemeral payload artifacts past retention."""

    policies = {
        str(policy): max(1, int(days))
        for policy, days in (retention_days or DEFAULT_RETENTION_DAYS).items()
    }
    current = now or utc_now_db()
    predicates = [
        and_(
            RuntimeArtifact.retention_policy == policy,
            RuntimeArtifact.updated_at < current - timedelta(days=days),
        )
        for policy, days in policies.items()
    ]
    if not predicates:
        return {"candidates": 0, "deleted_records": 0, "deleted_files": 0}
    candidates = (
        RuntimeArtifact.query.filter(or_(*predicates))
        .order_by(RuntimeArtifact.updated_at.asc(), RuntimeArtifact.id.asc())
        .limit(max(1, min(int(limit), 5_000)))
        .all()
    )
    run_ids = {str(row.workflow_run_id) for row in candidates if row.workflow_run_id}
    run_statuses = {
        str(run_id): str(status or "").lower()
        for run_id, status in (
            db.session.query(WorkflowRun.id, WorkflowRun.status)
            .filter(WorkflowRun.id.in_(run_ids))
            .all()
            if run_ids
            else []
        )
    }
    expired = [
        row
        for row in candidates
        if not row.workflow_run_id
        or run_statuses.get(str(row.workflow_run_id)) in TERMINAL_RUN_STATUSES
    ]
    artifact_ids = [str(row.id) for row in expired]
    versions = (
        RuntimeArtifactVersion.query.filter(
            RuntimeArtifactVersion.artifact_id.in_(artifact_ids)
        ).all()
        if artifact_ids
        else []
    )
    paths: set[Path] = set()
    unsafe_paths = 0
    for row in [*expired, *versions]:
        try:
            path = _artifact_path(row, instance_path)
        except ValueError:
            unsafe_paths += 1
            continue
        if path is not None:
            paths.add(path)
    result = {
        "candidates": len(candidates),
        "eligible": len(expired),
        "skipped_active_runs": len(candidates) - len(expired),
        "unsafe_paths": unsafe_paths,
        "deleted_records": 0,
        "deleted_versions": 0,
        "deleted_files": 0,
        "file_errors": [],
        "dry_run": bool(dry_run),
    }
    if dry_run or not artifact_ids:
        return result

    deleted_versions = RuntimeArtifactVersion.query.filter(
        RuntimeArtifactVersion.artifact_id.in_(artifact_ids)
    ).delete(synchronize_session=False)
    deleted_records = RuntimeArtifact.query.filter(
        RuntimeArtifact.id.in_(artifact_ids)
    ).delete(synchronize_session=False)
    db.session.commit()
    result["deleted_versions"] = int(deleted_versions or 0)
    result["deleted_records"] = int(deleted_records or 0)

    for path in paths:
        try:
            path.unlink(missing_ok=True)
            result["deleted_files"] += 1
        except OSError as exc:
            result["file_errors"].append({"path": str(path), "error": str(exc)})
    return result


def run_scheduled_artifact_retention(app: Any) -> dict[str, Any]:
    with app.app_context():
        result = prune_expired_runtime_artifacts(instance_path=app.instance_path)
        app.logger.info("Runtime artifact retention result=%s", result)
        return result


__all__ = [
    "DEFAULT_RETENTION_DAYS",
    "prune_expired_runtime_artifacts",
    "run_scheduled_artifact_retention",
]
