"""Pure workflow-run serializers and bounded bulk database projections."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from iarf_assistant.core.collections.projections import (
    build_run_collection_summary,
    load_collection_summaries,
)
from carlina.core.utc import as_utc, utc_now


@dataclass(frozen=True, slots=True)
class RunProjection:
    """All relational data needed to serialize a page of workflow runs."""

    steps_by_run: Mapping[str, Sequence[Any]]
    collection_summaries_by_run: Mapping[str, Sequence[Mapping[str, Any]]]

    def steps_for(self, run_id: str) -> Sequence[Any]:
        return self.steps_by_run.get(str(run_id), ())

    def collections_for(self, run_id: str) -> Sequence[Mapping[str, Any]]:
        return self.collection_summaries_by_run.get(str(run_id), ())


def fallback_run_title(run: Any) -> str:
    metadata = run.metadata_json if isinstance(run.metadata_json, dict) else {}
    for key in ("title", "name", "label"):
        if metadata.get(key):
            return str(metadata[key])[:255]
    input_payload = run.input_payload if isinstance(run.input_payload, dict) else {}
    for key in ("title", "name", "task", "user_request", "request", "prompt"):
        if input_payload.get(key):
            text = " ".join(str(input_payload[key]).split())
            return text[:252] + "..." if len(text) > 255 else text
    if run.workflow_id:
        return str(run.workflow_id).replace("_", " ").replace(".", " / ").title()
    return "Ad hoc run"


def _event_sort_key(
    event: Mapping[str, Any], owner: Any, index: int
) -> tuple[float, int]:
    timestamp = event.get("timestamp")
    if timestamp:
        try:
            return (
                datetime.fromisoformat(
                    str(timestamp).replace("Z", "+00:00")
                ).timestamp(),
                index,
            )
        except (TypeError, ValueError):
            pass
    fallback = (
        owner.updated_at
        or getattr(owner, "completed_at", None)
        or getattr(owner, "started_at", None)
        or owner.created_at
    )
    try:
        return (as_utc(fallback).timestamp(), index)
    except (AttributeError, TypeError, ValueError):
        return (0.0, index)


def runtime_event_summary(run: Any, steps: Sequence[Any]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    candidates: list[tuple[tuple[float, int], dict[str, Any]]] = []
    run_metadata = run.metadata_json if isinstance(run.metadata_json, dict) else {}
    owners = [(run, run_metadata.get("runtime_events") or [])]
    owners.extend(
        (
            step,
            (step.metadata_json or {}).get("runtime_events") or [],
        )
        for step in steps
        if isinstance(step.metadata_json, dict)
    )
    total = 0
    for owner, events in owners:
        for index, event in enumerate(events if isinstance(events, list) else []):
            if not isinstance(event, dict):
                continue
            total += 1
            kind = str(event.get("kind") or "unknown")
            counts[kind] = counts.get(kind, 0) + 1
            payload = dict(event)
            if owner is not run:
                payload.update(step_id=owner.id, step_title=owner.display_title())
            candidates.append((_event_sort_key(event, owner, index), payload))
    return {
        "latest_runtime_event": max(candidates, default=(None, None))[1],
        "runtime_event_count": total,
        "runtime_event_counts": counts,
    }


def _checkpoint_time_due(condition: Mapping[str, Any], now: datetime) -> bool:
    due_at = condition.get("due_at")
    if not due_at:
        return False
    try:
        parsed = datetime.fromisoformat(str(due_at).replace("Z", "+00:00"))
        return as_utc(parsed) <= now
    except (TypeError, ValueError):
        return False


def _checkpoint_stall_due(
    run: Any,
    condition: Mapping[str, Any],
    steps: Sequence[Any],
    now: datetime,
) -> bool:
    try:
        stall_seconds = int(condition.get("stall_after_seconds"))
    except (TypeError, ValueError):
        return False
    terminal = {"completed", "failed", "cancelled", "skipped"}
    if stall_seconds <= 0 or str(run.status or "").lower() != "running":
        return False
    if not any(str(step.status or "").lower() not in terminal for step in steps):
        return False
    timestamps: list[float] = []
    baseline = condition.get("baseline_activity_at")
    if baseline:
        try:
            timestamps.append(
                as_utc(
                    datetime.fromisoformat(str(baseline).replace("Z", "+00:00"))
                ).timestamp()
            )
        except (TypeError, ValueError):
            pass
    for step in steps:
        timestamps.extend(
            as_utc(value).timestamp()
            for value in (step.updated_at, step.started_at, step.completed_at)
            if value is not None
        )
    latest_activity = max(timestamps, default=0.0)
    return now.timestamp() - latest_activity >= stall_seconds


def _checkpoint_sort_key(
    checkpoint: Mapping[str, Any], index: int
) -> tuple[float, int]:
    for key in ("updated_at", "created_at"):
        try:
            return (
                datetime.fromisoformat(
                    str(checkpoint[key]).replace("Z", "+00:00")
                ).timestamp(),
                index,
            )
        except (KeyError, TypeError, ValueError):
            continue
    return (0.0, index)


def _project_checkpoint(
    run: Any,
    checkpoint: Mapping[str, Any],
    *,
    steps: Sequence[Any],
    completed_step_count: int,
    now: datetime,
) -> dict[str, Any]:
    payload = dict(checkpoint)
    condition = payload.get("condition")
    condition = condition if isinstance(condition, dict) else {}
    status = str(payload.get("status") or "pending").lower()
    if status not in {"pending", "due", "resolved", "cancelled"}:
        status = "pending"
    due_reasons = list(payload.get("due_reasons") or []) if status == "due" else []
    if status == "pending":
        if _checkpoint_time_due(condition, now):
            due_reasons.append("time")
        if _checkpoint_stall_due(run, condition, steps, now):
            due_reasons.append("stall")
        try:
            if completed_step_count >= int(condition["target_completed_steps"]):
                due_reasons.append("step_completions")
        except (KeyError, TypeError, ValueError):
            pass
    computed_status = "due" if due_reasons and status in {"pending", "due"} else status
    payload.update(
        status=computed_status,
        due=computed_status == "due",
        due_reasons=list(dict.fromkeys(due_reasons)),
        completed_step_count=completed_step_count,
        condition=condition,
    )
    return payload


def runtime_checkpoint_summary(
    run: Any,
    steps: Sequence[Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Compute checkpoint state entirely from a run projection."""

    metadata = run.metadata_json if isinstance(run.metadata_json, dict) else {}
    raw = metadata.get("runtime_checkpoints")
    raw = raw if isinstance(raw, list) else []
    completed = sum(str(step.status or "").lower() == "completed" for step in steps)
    projected = [
        _project_checkpoint(
            run,
            checkpoint,
            steps=steps,
            completed_step_count=completed,
            now=now or utc_now(),
        )
        for checkpoint in raw
        if isinstance(checkpoint, dict)
    ]
    due = [item for item in projected if item["status"] == "due"]
    return {
        "runtime_checkpoints": projected,
        "runtime_checkpoint_count": len(projected),
        "pending_runtime_checkpoint_count": sum(
            item["status"] == "pending" for item in projected
        ),
        "due_runtime_checkpoint_count": len(due),
        "latest_due_runtime_checkpoint": max(
            enumerate(due),
            key=lambda pair: _checkpoint_sort_key(pair[1], pair[0]),
            default=(None, None),
        )[1],
    }


def _legacy_collection_summary(run: Any) -> dict[str, Any]:
    state = run.state_json if isinstance(run.state_json, dict) else {}
    raw = state.get("runtime_collections")
    raw = raw if isinstance(raw, list) else []
    summaries: list[dict[str, Any]] = []
    for collection in (item for item in raw if isinstance(item, dict)):
        items = [
            item for item in collection.get("items") or [] if isinstance(item, dict)
        ]
        status_counts: dict[str, int] = {}
        for item in items:
            status = str(item.get("status") or "unknown").lower()
            status_counts[status] = status_counts.get(status, 0) + 1
        partitions = collection.get("partitions")
        partitions = partitions if isinstance(partitions, list) else []
        summaries.append(
            {
                "id": collection.get("id"),
                "title": collection.get("title"),
                "kind": collection.get("kind"),
                "status": collection.get("status") or "active",
                "item_count": len(items),
                "item_status_counts": status_counts,
                "annotation_count": sum(
                    len(item.get("annotations") or [])
                    for item in items
                    if isinstance(item.get("annotations"), list)
                ),
                "partition_count": len(partitions),
                "latest_partition_id": (
                    partitions[-1].get("id")
                    if partitions and isinstance(partitions[-1], dict)
                    else None
                ),
                "updated_at": collection.get("updated_at"),
            }
        )
    return build_run_collection_summary(summaries)


def _current_step_label(run: Any, steps: Sequence[Any]) -> str | None:
    if not run.current_step:
        return None
    for step in steps:
        if step.id == run.current_step or step.step_key == run.current_step:
            return step.display_title()
    return str(run.current_step)


def serialize_workflow_run(run: Any, projection: RunProjection) -> dict[str, Any]:
    """Serialize one workflow run without hidden database access."""

    steps = list(projection.steps_for(run.id))
    collection_rows = list(projection.collections_for(run.id))
    collections = (
        build_run_collection_summary(collection_rows)
        if collection_rows
        else _legacy_collection_summary(run)
    )
    events = runtime_event_summary(run, steps)
    checkpoints = runtime_checkpoint_summary(run, steps)
    display_title = run.title or fallback_run_title(run)
    return {
        "id": run.id,
        "title": run.title,
        "display_title": display_title,
        "name": display_title,
        "kind": run.kind,
        "workflow_id": run.workflow_id,
        "source_addon_id": run.source_addon_id,
        "status": run.status,
        "input": run.input_payload or {},
        "understanding": run.understanding_json or {},
        "plan": run.plan_json or {},
        "state": run.state_json or {},
        "current_step": run.current_step,
        "current_step_label": _current_step_label(run, steps),
        "created_by_user_id": run.created_by_user_id,
        "chat_session_id": run.chat_session_id,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "updated_at": run.updated_at.isoformat() if run.updated_at else None,
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
        "approval_ids": run.approval_ids or [],
        "resource_reference_ids": run.resource_reference_ids or [],
        "job_ids": run.job_ids or [],
        "step_ids": [step.id for step in steps] if steps else (run.step_ids or []),
        "source_ids": run.source_ids or [],
        "evidence_ids": run.evidence_ids or [],
        "artifact_ids": run.artifact_ids or [],
        "log_source_ids": run.log_source_ids or [],
        "resume_url": run.resume_url,
        "cancel_url": run.cancel_url,
        "metadata": run.metadata_json or {},
        **events,
        **checkpoints,
        **collections,
    }


def load_run_projections(runs: Sequence[Any]) -> RunProjection:
    """Load steps and collection aggregates in at most three SQL queries."""

    rows = list(runs)
    run_ids = [str(run.id) for run in rows]
    if not run_ids:
        return RunProjection({}, {})

    from iarf_assistant.models import RuntimeCollection, WorkflowStep

    step_rows = (
        WorkflowStep.query.filter(WorkflowStep.workflow_run_id.in_(run_ids))
        .order_by(
            WorkflowStep.workflow_run_id.asc(),
            WorkflowStep.created_at.asc(),
            WorkflowStep.id.asc(),
        )
        .all()
    )
    steps_by_run: dict[str, list[Any]] = {run_id: [] for run_id in run_ids}
    for step in step_rows:
        steps_by_run[str(step.workflow_run_id)].append(step)

    collections = (
        RuntimeCollection.query.filter(RuntimeCollection.workflow_run_id.in_(run_ids))
        .order_by(
            RuntimeCollection.workflow_run_id.asc(),
            RuntimeCollection.created_at.asc(),
            RuntimeCollection.id.asc(),
        )
        .all()
    )
    summaries = load_collection_summaries(collections)
    collections_by_run: dict[str, list[Mapping[str, Any]]] = {
        run_id: [] for run_id in run_ids
    }
    for collection in collections:
        collections_by_run[str(collection.workflow_run_id)].append(
            summaries[str(collection.id)]
        )
    return RunProjection(steps_by_run, collections_by_run)


def load_run_projection(run: Any) -> RunProjection:
    return load_run_projections([run])


__all__ = [
    "RunProjection",
    "fallback_run_title",
    "load_run_projection",
    "load_run_projections",
    "serialize_workflow_run",
]
