from __future__ import annotations

import base64
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from flask import Blueprint, jsonify, request
from flask_login import current_user, login_required
from sqlalchemy import false, func, or_

from iarf_assistant.extensions import db
from iarf_assistant.core.context_fabric import (
    ContextFabricError,
    ContextPolicyError,
    ContextVersionConflict,
    read_context_item_evidence,
    transition_context_item,
)
from iarf_assistant.models import (
    ChatContextItem,
    ChatSession,
    RuntimeApprovalRequest,
    RuntimeArtifact,
    RuntimeArtifactVersion,
    RuntimeEvidenceRecord,
    RuntimeInteraction,
    RuntimeSourceRecord,
    UserMemory,
    WorkflowRun,
    WorkflowStep,
)


frontend_context_blueprint = Blueprint("frontend_context_api", __name__)

WORKSPACE_PAGE_DEFAULT_LIMIT = 30
WORKSPACE_PAGE_MAX_LIMIT = 80
CONTEXT_PREVIEW_DEFAULT_CHARS = 20_000
CONTEXT_PREVIEW_MAX_CHARS = 50_000
TERMINAL_RUN_STATUSES = {"completed", "failed", "cancelled"}
TERMINAL_STEP_STATUSES = {"completed", "failed", "cancelled", "skipped"}
ATTENTION_RUN_STATUSES = {
    "waiting_for_approval",
    "waiting_for_review",
    "paused",
}
REVIEWABLE_ARTIFACT_STATUSES = {
    "draft",
    "pending_review",
    "changes_requested",
}
SENSITIVE_METADATA_KEYS = {
    "access_token",
    "api_key",
    "authorization",
    "content",
    "password",
    "raw_content",
    "refresh_token",
    "secret",
    "token",
}
SENSITIVE_METADATA_KEY_ALIASES = {
    "accesstoken",
    "apikey",
    "authorization",
    "cookie",
    "credentials",
    "password",
    "rawcontent",
    "refreshtoken",
    "secret",
    "token",
}


def _is_sensitive_metadata_key(value: Any) -> bool:
    normalized = str(value or "").strip().lower()
    compact = re.sub(r"[^a-z0-9]+", "", normalized)
    return (
        normalized in SENSITIVE_METADATA_KEYS
        or compact in SENSITIVE_METADATA_KEY_ALIASES
    )


def _owned_chat_session(session_id: str) -> ChatSession | None:
    chat_session = db.session.get(ChatSession, session_id)
    if not chat_session or int(chat_session.user_id) != int(current_user.id):
        return None
    return chat_session


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


def _humanize(value: Any, *, fallback: str = "Untitled") -> str:
    text = str(value or "").strip()
    if not text:
        return fallback
    text = re.sub(r"[_\-.]+", " ", text)
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text)
    text = " ".join(text.split())
    return text[:1].upper() + text[1:]


def _semantic_value(
    value: Any,
    *,
    depth: int = 0,
    max_items: int = 60,
    max_string_chars: int = 4_000,
) -> Any:
    """Return bounded structured data suitable for a semantic renderer."""
    if depth >= 6:
        return "Additional nested detail omitted"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        clean = value.strip()
        if len(clean) <= max_string_chars:
            return clean
        return f"{clean[:max_string_chars]}\n… [truncated]"
    if isinstance(value, dict):
        bounded: dict[str, Any] = {}
        visible_items = [
            (str(key), item)
            for key, item in value.items()
            if not _is_sensitive_metadata_key(key)
        ]
        for key, item in visible_items[:max_items]:
            bounded[key] = _semantic_value(
                item,
                depth=depth + 1,
                max_items=max_items,
                max_string_chars=max_string_chars,
            )
        if len(visible_items) > max_items:
            bounded["additional_fields"] = (
                f"{len(visible_items) - max_items} more fields"
            )
        return bounded
    if isinstance(value, (list, tuple)):
        bounded_items = [
            _semantic_value(
                item,
                depth=depth + 1,
                max_items=max_items,
                max_string_chars=max_string_chars,
            )
            for item in value[:max_items]
        ]
        if len(value) > max_items:
            bounded_items.append(f"{len(value) - max_items} more items")
        return bounded_items
    return _semantic_value(
        str(value),
        depth=depth + 1,
        max_items=max_items,
        max_string_chars=max_string_chars,
    )


def _value_kind(value: Any) -> str:
    if value is None:
        return "empty"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, dict):
        return "structured"
    if isinstance(value, (list, tuple)):
        return "list"
    return "text"


def _value_summary(value: Any) -> str:
    if value is None:
        return "No value recorded"
    if isinstance(value, bool):
        return "Enabled" if value else "Disabled"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        clean = " ".join(value.split())
        return clean[:220] + ("…" if len(clean) > 220 else "")
    if isinstance(value, dict):
        for key in ("summary", "description", "objective", "message", "value"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return _value_summary(candidate)
        return f"{len(value)} recorded field{'s' if len(value) != 1 else ''}"
    if isinstance(value, (list, tuple)):
        if all(
            item is None or isinstance(item, (str, bool, int, float))
            for item in value
        ):
            rendered = ", ".join(str(item) for item in value[:4])
            if len(value) > 4:
                rendered += f", and {len(value) - 4} more"
            return rendered or "Empty list"
        return f"{len(value)} recorded item{'s' if len(value) != 1 else ''}"
    return _value_summary(str(value))


def _searchable_text(value: Any, *, depth: int = 0) -> str:
    if depth >= 5 or value is None:
        return ""
    if isinstance(value, dict):
        return " ".join(
            f"{key} {_searchable_text(item, depth=depth + 1)}"
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return " ".join(
            _searchable_text(item, depth=depth + 1) for item in value
        )
    return str(value)


def _cursor_signature(context: dict[str, Any]) -> str:
    serialized = json.dumps(
        context,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()[:16]


def _encode_offset_cursor(offset: int, *, context: dict[str, Any]) -> str:
    payload = json.dumps(
        {
            "v": 1,
            "offset": max(0, int(offset)),
            "signature": _cursor_signature(context),
        },
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_offset_cursor(
    value: str,
    *,
    context: dict[str, Any],
) -> int:
    if not value:
        return 0
    padded = value + "=" * (-len(value) % 4)
    payload = json.loads(
        base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
    )
    if (
        int(payload.get("v", 0)) != 1
        or payload.get("signature") != _cursor_signature(context)
    ):
        raise ValueError("Cursor context does not match.")
    offset = int(payload["offset"])
    if offset < 0 or offset > 1_000_000:
        raise ValueError("Cursor offset is invalid.")
    return offset


def _parse_working_memory(raw_memory: Any) -> Any:
    raw = str(raw_memory or "").strip()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return raw


def _session_memory_entries(chat_session: ChatSession) -> list[dict[str, Any]]:
    parsed = _parse_working_memory(chat_session.working_memory)
    records: list[tuple[str, Any]]
    if isinstance(parsed, dict):
        records = [
            (str(key), value)
            for key, value in parsed.items()
            if not _is_sensitive_metadata_key(key)
        ]
    elif isinstance(parsed, list):
        records = []
        for index, value in enumerate(parsed):
            label = ""
            if isinstance(value, dict):
                label = str(
                    value.get("title")
                    or value.get("label")
                    or value.get("name")
                    or value.get("key")
                    or ""
                )
            records.append((label or f"Memory {index + 1}", value))
    elif parsed in ("", None):
        records = []
    else:
        records = [("Working note", parsed)]

    updated_at = (
        chat_session.updated_at.isoformat()
        if chat_session.updated_at
        else None
    )
    entries: list[dict[str, Any]] = []
    for index, (raw_title, value) in enumerate(records):
        semantic = _semantic_value(value)
        # Compatibility identifier, not a security digest.
        identity = hashlib.sha1(
            f"{index}:{raw_title}".encode("utf-8"),
            usedforsecurity=False,
        ).hexdigest()[:12]
        entries.append(
            {
                "id": f"session:{identity}",
                "scope": "session",
                "title": _humanize(raw_title, fallback=f"Memory {index + 1}"),
                "summary": _value_summary(semantic),
                "value": semantic,
                "value_kind": _value_kind(semantic),
                "category": "workspace",
                "importance": "normal",
                "tier": None,
                "updated_at": updated_at,
            }
        )
    return entries


def _personal_memory_entries() -> list[dict[str, Any]]:
    rows = (
        UserMemory.query.filter_by(user_id=int(current_user.id))
        .order_by(UserMemory.updated_at.desc(), UserMemory.id.desc())
        .all()
    )
    entries: list[dict[str, Any]] = []
    for row in rows:
        metadata = (
            row.json_metadata if isinstance(row.json_metadata, dict) else {}
        )
        category = str(metadata.get("category") or "general")
        fact = _semantic_value(row.fact)
        entries.append(
            {
                "id": f"personal:{row.id}",
                "scope": "personal",
                "title": _humanize(category, fallback="Saved memory"),
                "summary": _value_summary(fact),
                "value": fact,
                "value_kind": "text",
                "category": category,
                "importance": str(metadata.get("importance") or "normal"),
                "tier": str(metadata.get("tier") or "tier1"),
                "updated_at": (
                    row.updated_at.isoformat() if row.updated_at else None
                ),
            }
        )
    return entries


def _session_run_query(session_id: str):
    return WorkflowRun.query.filter(
        WorkflowRun.chat_session_id == session_id
    )


def _session_run_ids(session_id: str):
    return db.session.query(WorkflowRun.id).filter(
        WorkflowRun.chat_session_id == session_id
    )


def _run_title(run: WorkflowRun) -> str:
    return str(
        run.title
        or _humanize(run.workflow_id, fallback="")
        or _humanize(run.kind, fallback="Run")
    )


def _first_named_text(*values: Any) -> str:
    preferred_keys = (
        "summary",
        "description",
        "objective",
        "message",
        "status_message",
    )
    for value in values:
        if isinstance(value, str) and value.strip():
            return _value_summary(value)
        if not isinstance(value, dict):
            continue
        for key in preferred_keys:
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return _value_summary(candidate)
    return ""


def _run_support(
    run_ids: list[str],
) -> tuple[
    dict[str, list[dict[str, Any]]],
    dict[str, int],
    dict[str, int],
]:
    steps_by_run: dict[str, list[dict[str, Any]]] = {
        run_id: [] for run_id in run_ids
    }
    artifact_counts = {run_id: 0 for run_id in run_ids}
    approval_counts = {run_id: 0 for run_id in run_ids}
    if not run_ids:
        return steps_by_run, artifact_counts, approval_counts

    step_rows = (
        db.session.query(
            WorkflowStep.workflow_run_id,
            WorkflowStep.id,
            WorkflowStep.step_key,
            WorkflowStep.title,
            WorkflowStep.step_type,
            WorkflowStep.status,
            WorkflowStep.sequence,
        )
        .filter(WorkflowStep.workflow_run_id.in_(run_ids))
        .order_by(
            WorkflowStep.workflow_run_id.asc(),
            WorkflowStep.sequence.asc(),
            WorkflowStep.created_at.asc(),
        )
        .all()
    )
    for row in step_rows:
        steps_by_run.setdefault(str(row.workflow_run_id), []).append(
            {
                "id": str(row.id),
                "key": str(row.step_key or ""),
                "title": str(
                    row.title
                    or _humanize(row.step_key, fallback="")
                    or _humanize(row.step_type, fallback="Step")
                ),
                "status": str(row.status or "planned"),
            }
        )

    for run_id, count in (
        db.session.query(
            RuntimeArtifact.workflow_run_id,
            func.count(RuntimeArtifact.id),
        )
        .filter(RuntimeArtifact.workflow_run_id.in_(run_ids))
        .filter(RuntimeArtifact.retention_policy != "chat_context_result")
        .group_by(RuntimeArtifact.workflow_run_id)
        .all()
    ):
        artifact_counts[str(run_id)] = int(count or 0)

    for run_id, count in (
        db.session.query(
            RuntimeApprovalRequest.workflow_run_id,
            func.count(RuntimeApprovalRequest.id),
        )
        .filter(RuntimeApprovalRequest.workflow_run_id.in_(run_ids))
        .filter(RuntimeApprovalRequest.status == "pending")
        .group_by(RuntimeApprovalRequest.workflow_run_id)
        .all()
    ):
        approval_counts[str(run_id)] = int(count or 0)

    return steps_by_run, artifact_counts, approval_counts


def _run_summaries(rows: list[WorkflowRun]) -> list[dict[str, Any]]:
    run_ids = [str(row.id) for row in rows]
    steps_by_run, artifact_counts, approval_counts = _run_support(run_ids)
    summaries: list[dict[str, Any]] = []
    for run in rows:
        steps = steps_by_run.get(str(run.id), [])
        completed = sum(
            1 for step in steps if step["status"] in TERMINAL_STEP_STATUSES
        )
        total = len(steps)
        if total:
            percent = round((completed / total) * 100)
        elif run.status == "completed":
            percent = 100
        else:
            percent = 0

        current_step = next(
            (
                step
                for step in steps
                if run.current_step
                and run.current_step in {step["id"], step["key"]}
            ),
            None,
        )
        if current_step is None and run.current_step:
            current_step_label = _humanize(run.current_step, fallback="")
        else:
            current_step_label = (
                str(current_step["title"]) if current_step else ""
            )

        pending_approvals = approval_counts.get(str(run.id), 0)
        status = str(run.status or "draft")
        summaries.append(
            {
                "id": str(run.id),
                "title": _run_title(run),
                "summary": _first_named_text(
                    run.state_json,
                    run.understanding_json,
                    run.metadata_json,
                ),
                "kind": str(run.kind or "run"),
                "status": status,
                "active": status not in TERMINAL_RUN_STATUSES,
                "needs_attention": (
                    status in ATTENTION_RUN_STATUSES
                    or pending_approvals > 0
                ),
                "current_step_label": current_step_label or None,
                "progress": {
                    "completed": completed,
                    "total": total,
                    "percent": percent,
                },
                "artifact_count": artifact_counts.get(str(run.id), 0),
                "pending_approval_count": pending_approvals,
                "created_at": (
                    run.created_at.isoformat() if run.created_at else None
                ),
                "updated_at": (
                    run.updated_at.isoformat() if run.updated_at else None
                ),
                "completed_at": (
                    run.completed_at.isoformat()
                    if run.completed_at
                    else None
                ),
                "open_url": (
                    "/runs/"
                    + str(run.id)
                ),
            }
        )
    return summaries


def _artifact_label(artifact: RuntimeArtifact) -> str:
    if artifact.label:
        return str(artifact.label)
    for candidate in (artifact.path, artifact.uri, artifact.remote_reference):
        if not candidate:
            continue
        normalized = str(candidate).replace("\\", "/")
        name = PurePosixPath(normalized).name
        if name:
            return name
    return f"{_humanize(artifact.artifact_type, fallback='Artifact')} artifact"


def _artifact_preview_kind(artifact: RuntimeArtifact) -> str:
    mime = str(artifact.mime_type or "").lower()
    suffix = PurePosixPath(
        str(artifact.path or artifact.uri or "").replace("\\", "/")
    ).suffix.lower()
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("audio/"):
        return "audio"
    if mime.startswith("video/"):
        return "video"
    if mime == "application/pdf" or suffix == ".pdf":
        return "pdf"
    if (
        "markdown" in mime
        or suffix in {".md", ".markdown"}
    ):
        return "markdown"
    if mime in {"text/html", "application/xhtml+xml"} or suffix in {
        ".html",
        ".htm",
    }:
        return "html"
    if "json" in mime or suffix == ".json":
        return "structured"
    if mime.startswith("text/") or suffix in {
        ".csv",
        ".log",
        ".txt",
        ".yaml",
        ".yml",
    }:
        return "text"
    return "download"


def _artifact_version_numbers(
    artifact_ids: list[str],
) -> dict[str, int]:
    if not artifact_ids:
        return {}
    return {
        str(artifact_id): int(version_number or 0)
        for artifact_id, version_number in (
            db.session.query(
                RuntimeArtifactVersion.artifact_id,
                func.max(RuntimeArtifactVersion.version_number),
            )
            .filter(RuntimeArtifactVersion.artifact_id.in_(artifact_ids))
            .group_by(RuntimeArtifactVersion.artifact_id)
            .all()
        )
    }


def _artifact_summary(
    artifact: RuntimeArtifact,
    *,
    version_number: int = 0,
) -> dict[str, Any]:
    download_url = None
    if (
        artifact.download_permission != "none"
        and str(artifact.uri or "").startswith("instance://")
    ):
        download_url = (
            f"/api/runtime/artifacts/{artifact.id}/download"
        )
    return {
        "id": str(artifact.id),
        "run_id": (
            str(artifact.workflow_run_id)
            if artifact.workflow_run_id
            else None
        ),
        "label": _artifact_label(artifact),
        "type": str(artifact.artifact_type or "file"),
        "mime_type": str(
            artifact.mime_type or "application/octet-stream"
        ),
        "size_bytes": (
            int(artifact.size_bytes)
            if artifact.size_bytes is not None
            else None
        ),
        "review_status": str(artifact.review_status or "draft"),
        "preview_kind": _artifact_preview_kind(artifact),
        "version_number": version_number or None,
        "source_addon_id": (
            str(artifact.source_addon_id)
            if artifact.source_addon_id
            else None
        ),
        "created_at": (
            artifact.created_at.isoformat()
            if artifact.created_at
            else None
        ),
        "updated_at": (
            artifact.updated_at.isoformat()
            if artifact.updated_at
            else None
        ),
        "download_url": download_url,
    }


def _session_artifact_query(session_id: str):
    return RuntimeArtifact.query.filter(
        RuntimeArtifact.workflow_run_id.in_(_session_run_ids(session_id))
    ).filter(RuntimeArtifact.retention_policy != "chat_context_result")


def _safe_external_url(value: Any) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    parsed = urlsplit(raw)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
    ):
        return None
    return raw


def _memory_counts(chat_session: ChatSession) -> tuple[int, int]:
    session_count = len(_session_memory_entries(chat_session))
    personal_count = int(
        db.session.query(func.count(UserMemory.id))
        .filter(UserMemory.user_id == int(current_user.id))
        .scalar()
        or 0
    )
    return session_count, personal_count


def _context_item_summary(item: ChatContextItem) -> dict[str, Any]:
    """Return an owner-facing manifest without evidence or materialized text."""
    evidence = item.evidence
    return {
        "id": str(item.id),
        "evidence_id": str(item.evidence_id),
        "title": str(item.title or _humanize(item.kind, fallback="Context item"))[
            :500
        ],
        "kind": str(item.kind or "context"),
        "lifecycle_state": str(item.lifecycle_state or "active"),
        "materialization": str(item.materialization or "reference"),
        "priority": int(item.priority or 0),
        "is_pinned": bool(item.is_pinned),
        "token_estimate": (
            int(item.token_estimate)
            if item.token_estimate is not None
            else None
        ),
        "char_count": int(evidence.char_count or 0),
        "byte_count": int(evidence.byte_count or 0),
        "mime_type": str(evidence.mime_type or "") or None,
        "content_hash": str(evidence.content_hash or ""),
        "version": int(item.version),
        "created_at": item.created_at.isoformat() if item.created_at else None,
        "updated_at": item.updated_at.isoformat() if item.updated_at else None,
    }


def _iso_max(*values: Any) -> str | None:
    normalized = [
        value.isoformat() if hasattr(value, "isoformat") else str(value)
        for value in values
        if value
    ]
    return max(normalized) if normalized else None


@frontend_context_blueprint.route(
    "/api/frontend/chats/<session_id>/workspace",
    methods=["GET"],
)
@login_required
def get_frontend_workspace_summary(session_id: str):
    chat_session = _owned_chat_session(session_id)
    if chat_session is None:
        return jsonify({"error": "Chat session not found."}), 404

    run_query = _session_run_query(session_id)
    run_count = int(
        db.session.query(func.count(WorkflowRun.id))
        .filter(WorkflowRun.chat_session_id == session_id)
        .scalar()
        or 0
    )
    active_run_count = int(
        db.session.query(func.count(WorkflowRun.id))
        .filter(WorkflowRun.chat_session_id == session_id)
        .filter(~WorkflowRun.status.in_(TERMINAL_RUN_STATUSES))
        .scalar()
        or 0
    )
    recent_runs = (
        run_query.order_by(
            WorkflowRun.updated_at.desc(),
            WorkflowRun.id.desc(),
        )
        .limit(6)
        .all()
    )
    run_summaries = _run_summaries(recent_runs)
    focus_run = next(
        (run for run in run_summaries if run["active"]),
        run_summaries[0] if run_summaries else None,
    )
    artifact_query = _session_artifact_query(session_id)
    artifact_count = int(
        db.session.query(func.count(RuntimeArtifact.id))
        .filter(
            RuntimeArtifact.workflow_run_id.in_(
                _session_run_ids(session_id)
            )
        )
        .filter(RuntimeArtifact.retention_policy != "chat_context_result")
        .scalar()
        or 0
    )
    review_count = int(
        db.session.query(func.count(RuntimeArtifact.id))
        .filter(
            RuntimeArtifact.workflow_run_id.in_(
                _session_run_ids(session_id)
            )
        )
        .filter(RuntimeArtifact.retention_policy != "chat_context_result")
        .filter(
            RuntimeArtifact.review_status.in_(
                REVIEWABLE_ARTIFACT_STATUSES
            )
        )
        .scalar()
        or 0
    )
    recent_artifact_rows = (
        artifact_query.order_by(
            RuntimeArtifact.updated_at.desc(),
            RuntimeArtifact.id.desc(),
        )
        .limit(4)
        .all()
    )
    version_numbers = _artifact_version_numbers(
        [str(row.id) for row in recent_artifact_rows]
    )
    recent_artifacts = [
        _artifact_summary(
            row,
            version_number=version_numbers.get(str(row.id), 0),
        )
        for row in recent_artifact_rows
    ]

    all_session_run_ids = _session_run_ids(session_id)
    pending_approval_count = int(
        db.session.query(func.count(RuntimeApprovalRequest.id))
        .filter(
            RuntimeApprovalRequest.workflow_run_id.in_(
                all_session_run_ids
            )
        )
        .filter(RuntimeApprovalRequest.status == "pending")
        .scalar()
        or 0
    )
    pending_interaction_count = int(
        db.session.query(func.count(RuntimeInteraction.id))
        .filter(RuntimeInteraction.chat_session_id == session_id)
        .filter(RuntimeInteraction.status == "pending")
        .scalar()
        or 0
    )
    session_memory_count, personal_memory_count = _memory_counts(
        chat_session
    )
    context_base_query = ChatContextItem.query.filter_by(
        session_id=session_id,
        user_id=int(current_user.id),
    ).filter(ChatContextItem.lifecycle_state != "deleted")
    context_count = int(context_base_query.count())
    active_context_count = int(
        context_base_query.filter(
            ChatContextItem.lifecycle_state == "active"
        ).count()
    )
    archived_context_count = int(
        context_base_query.filter(
            ChatContextItem.lifecycle_state == "archived"
        ).count()
    )
    excluded_context_count = int(
        context_base_query.filter(
            ChatContextItem.lifecycle_state == "excluded"
        ).count()
    )

    latest_run_update = (
        db.session.query(func.max(WorkflowRun.updated_at))
        .filter(WorkflowRun.chat_session_id == session_id)
        .scalar()
    )
    latest_artifact_update = (
        db.session.query(func.max(RuntimeArtifact.updated_at))
        .filter(
            RuntimeArtifact.workflow_run_id.in_(
                _session_run_ids(session_id)
            )
        )
        .filter(RuntimeArtifact.retention_policy != "chat_context_result")
        .scalar()
    )
    latest_personal_memory_update = (
        db.session.query(func.max(UserMemory.updated_at))
        .filter(UserMemory.user_id == int(current_user.id))
        .scalar()
    )
    latest_context_update = (
        db.session.query(func.max(ChatContextItem.updated_at))
        .filter(
            ChatContextItem.session_id == session_id,
            ChatContextItem.user_id == int(current_user.id),
        )
        .scalar()
    )
    memory_digest = hashlib.sha256(
        str(chat_session.working_memory or "").encode("utf-8")
    ).hexdigest()[:16]
    revision_source = {
        "runs": [
            run_count,
            latest_run_update.isoformat() if latest_run_update else None,
        ],
        "artifacts": [
            artifact_count,
            (
                latest_artifact_update.isoformat()
                if latest_artifact_update
                else None
            ),
        ],
        "memory": [
            session_memory_count,
            personal_memory_count,
            memory_digest,
            (
                latest_personal_memory_update.isoformat()
                if latest_personal_memory_update
                else None
            ),
        ],
        "context": [
            context_count,
            active_context_count,
            archived_context_count,
            excluded_context_count,
            latest_context_update.isoformat() if latest_context_update else None,
        ],
        "attention": [
            pending_approval_count,
            pending_interaction_count,
        ],
    }
    revision = hashlib.sha256(
        json.dumps(
            revision_source,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:24]

    attention: list[dict[str, Any]] = []
    if pending_interaction_count:
        attention.append(
            {
                "id": "interactions",
                "kind": "interaction",
                "title": (
                    f"{pending_interaction_count} "
                    f"decision{'s' if pending_interaction_count != 1 else ''} "
                    "need your input"
                ),
                "summary": (
                    "Review the request in the conversation to continue."
                ),
                "target_tab": "overview",
            }
        )
    if pending_approval_count:
        attention.append(
            {
                "id": "approvals",
                "kind": "approval",
                "title": (
                    f"{pending_approval_count} "
                    f"approval{'s' if pending_approval_count != 1 else ''} "
                    "waiting"
                ),
                "summary": "Open Runs to review the affected workflow.",
                "target_tab": "runs",
            }
        )
    if review_count:
        attention.append(
            {
                "id": "artifact-review",
                "kind": "review",
                "title": (
                    f"{review_count} "
                    f"artifact{'s' if review_count != 1 else ''} "
                    "ready for review"
                ),
                "summary": "Open Artifacts to preview and record a decision.",
                "target_tab": "artifacts",
            }
        )

    response = jsonify(
        {
            "session": {
                "id": chat_session.id,
                "title": str(
                    chat_session.title or "New conversation"
                ),
            },
            "revision": revision,
            "updated_at": _iso_max(
                chat_session.updated_at,
                latest_run_update,
                latest_artifact_update,
                latest_personal_memory_update,
                latest_context_update,
            ),
            "server_time": datetime.now(timezone.utc).isoformat(),
            "counts": {
                "runs": run_count,
                "active_runs": active_run_count,
                "artifacts": artifact_count,
                "artifact_reviews": review_count,
                "session_memory": session_memory_count,
                "personal_memory": personal_memory_count,
                "context_items": context_count,
                "active_context_items": active_context_count,
                "archived_context_items": archived_context_count,
                "excluded_context_items": excluded_context_count,
                "attention": (
                    pending_approval_count + pending_interaction_count
                ),
            },
            "focus_run": focus_run,
            "recent_runs": run_summaries[:3],
            "recent_artifacts": recent_artifacts,
            "attention": attention,
        }
    )
    response.headers["Cache-Control"] = "private, no-cache"
    return response


@frontend_context_blueprint.route(
    "/api/frontend/chats/<session_id>/runs",
    methods=["GET"],
)
@login_required
def list_frontend_workspace_runs(session_id: str):
    chat_session = _owned_chat_session(session_id)
    if chat_session is None:
        return jsonify({"error": "Chat session not found."}), 404

    limit = _bounded_int(
        request.args.get("limit"),
        default=WORKSPACE_PAGE_DEFAULT_LIMIT,
        minimum=1,
        maximum=WORKSPACE_PAGE_MAX_LIMIT,
    )
    view = str(request.args.get("view") or "all").strip().lower()
    if view not in {"all", "active", "attention", "completed"}:
        return jsonify({"error": "The run view is invalid."}), 400
    context = {"kind": "runs", "session_id": session_id, "view": view}
    try:
        offset = _decode_offset_cursor(
            str(request.args.get("cursor") or ""),
            context=context,
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return jsonify({"error": "The run cursor is invalid."}), 400

    query = _session_run_query(session_id)
    if view == "active":
        query = query.filter(
            ~WorkflowRun.status.in_(TERMINAL_RUN_STATUSES)
        )
    elif view == "attention":
        query = query.filter(
            WorkflowRun.status.in_(ATTENTION_RUN_STATUSES)
        )
    elif view == "completed":
        query = query.filter(
            WorkflowRun.status.in_(TERMINAL_RUN_STATUSES)
        )

    total = int(query.with_entities(func.count(WorkflowRun.id)).scalar() or 0)
    rows = (
        query.order_by(
            WorkflowRun.updated_at.desc(),
            WorkflowRun.id.desc(),
        )
        .offset(offset)
        .limit(limit + 1)
        .all()
    )
    has_more = len(rows) > limit
    visible_rows = rows[:limit]
    response = jsonify(
        {
            "items": _run_summaries(visible_rows),
            "total": total,
            "next_cursor": (
                _encode_offset_cursor(
                    offset + limit,
                    context=context,
                )
                if has_more
                else None
            ),
        }
    )
    response.headers["Cache-Control"] = "private, no-cache"
    return response


@frontend_context_blueprint.route(
    "/api/frontend/chats/<session_id>/memory",
    methods=["GET"],
)
@login_required
def list_frontend_workspace_memory(session_id: str):
    chat_session = _owned_chat_session(session_id)
    if chat_session is None:
        return jsonify({"error": "Chat session not found."}), 404

    limit = _bounded_int(
        request.args.get("limit"),
        default=WORKSPACE_PAGE_DEFAULT_LIMIT,
        minimum=1,
        maximum=WORKSPACE_PAGE_MAX_LIMIT,
    )
    scope = str(request.args.get("scope") or "all").strip().lower()
    if scope not in {"all", "session", "personal"}:
        return jsonify({"error": "The memory scope is invalid."}), 400
    query_text = " ".join(
        str(request.args.get("q") or "").strip().lower().split()
    )[:200]
    context = {
        "kind": "memory",
        "session_id": session_id,
        "scope": scope,
        "q": query_text,
    }
    try:
        offset = _decode_offset_cursor(
            str(request.args.get("cursor") or ""),
            context=context,
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return jsonify({"error": "The memory cursor is invalid."}), 400

    entries: list[dict[str, Any]] = []
    if scope in {"all", "session"}:
        entries.extend(_session_memory_entries(chat_session))
    if scope in {"all", "personal"}:
        entries.extend(_personal_memory_entries())

    if query_text:
        terms = query_text.split()
        entries = [
            entry
            for entry in entries
            if all(
                term
                in _searchable_text(entry).lower()
                for term in terms
            )
        ]
    entries.sort(
        key=lambda item: (
            str(item.get("updated_at") or ""),
            str(item.get("id") or ""),
        ),
        reverse=True,
    )
    total = len(entries)
    page = entries[offset : offset + limit]
    has_more = offset + limit < total
    response = jsonify(
        {
            "items": page,
            "total": total,
            "next_cursor": (
                _encode_offset_cursor(
                    offset + limit,
                    context=context,
                )
                if has_more
                else None
            ),
            "scope": scope,
            "query": query_text,
        }
    )
    response.headers["Cache-Control"] = "private, no-cache"
    return response


@frontend_context_blueprint.route(
    "/api/frontend/chats/<session_id>/context-items",
    methods=["GET"],
)
@login_required
def list_frontend_context_items(session_id: str):
    """List compact, addressable context manifests for direct user control."""
    chat_session = _owned_chat_session(session_id)
    if chat_session is None:
        return jsonify({"error": "Chat session not found."}), 404

    limit = _bounded_int(
        request.args.get("limit"),
        default=WORKSPACE_PAGE_DEFAULT_LIMIT,
        minimum=1,
        maximum=WORKSPACE_PAGE_MAX_LIMIT,
    )
    lifecycle_state = str(
        request.args.get("state") or "all"
    ).strip().lower()
    if lifecycle_state not in {"all", "active", "archived", "excluded"}:
        return jsonify({"error": "The context lifecycle state is invalid."}), 400
    materialization = str(
        request.args.get("materialization") or "all"
    ).strip().lower()
    if materialization not in {
        "all",
        "full",
        "excerpt",
        "summary",
        "reference",
        "overflow",
    }:
        return jsonify({"error": "The context materialization is invalid."}), 400
    query_text = " ".join(
        str(request.args.get("q") or "").strip().lower().split()
    )[:200]
    cursor_context = {
        "kind": "context_items",
        "session_id": session_id,
        "state": lifecycle_state,
        "materialization": materialization,
        "q": query_text,
    }
    try:
        offset = _decode_offset_cursor(
            str(request.args.get("cursor") or ""),
            context=cursor_context,
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return jsonify({"error": "The context cursor is invalid."}), 400

    base_query = ChatContextItem.query.filter_by(
        session_id=session_id,
        user_id=int(current_user.id),
    ).filter(ChatContextItem.lifecycle_state != "deleted")
    if lifecycle_state != "all":
        base_query = base_query.filter(
            ChatContextItem.lifecycle_state == lifecycle_state
        )
    if materialization != "all":
        base_query = base_query.filter(
            ChatContextItem.materialization == materialization
        )
    base_query = base_query.order_by(
        ChatContextItem.is_pinned.desc(),
        ChatContextItem.updated_at.desc(),
        ChatContextItem.id.desc(),
    )

    search_truncated = False
    if query_text:
        scan_limit = 5_000
        total_candidates = int(base_query.count())
        candidates = base_query.limit(scan_limit).all()
        terms = query_text.split()
        rows = [
            item
            for item in candidates
            if all(
                term
                in " ".join(
                    [
                        str(item.id),
                        str(item.evidence_id),
                        str(item.title or ""),
                        str(item.kind or ""),
                        str(item.lifecycle_state or ""),
                        str(item.materialization or ""),
                    ]
                ).lower()
                for term in terms
            )
        ]
        total = len(rows)
        page = rows[offset : offset + limit]
        search_truncated = total_candidates > scan_limit
    else:
        total = int(base_query.count())
        page = base_query.offset(offset).limit(limit).all()

    count_query = ChatContextItem.query.filter_by(
        session_id=session_id,
        user_id=int(current_user.id),
    ).filter(ChatContextItem.lifecycle_state != "deleted")
    counts = {
        state: int(
            count_query.filter(ChatContextItem.lifecycle_state == state).count()
        )
        for state in ("active", "archived", "excluded")
    }
    counts["all"] = sum(counts.values())
    has_more = offset + limit < total
    response = jsonify(
        {
            "items": [_context_item_summary(item) for item in page],
            "total": total,
            "next_cursor": (
                _encode_offset_cursor(
                    offset + limit,
                    context=cursor_context,
                )
                if has_more
                else None
            ),
            "state": lifecycle_state,
            "materialization": materialization,
            "query": query_text,
            "counts": counts,
            "search_truncated": search_truncated,
        }
    )
    response.headers["Cache-Control"] = "private, no-cache"
    return response


@frontend_context_blueprint.route(
    "/api/frontend/chats/<session_id>/context-items/<context_item_id>",
    methods=["GET"],
)
@login_required
def get_frontend_context_item(session_id: str, context_item_id: str):
    """Return one integrity-checked, bounded evidence preview."""
    chat_session = _owned_chat_session(session_id)
    if chat_session is None:
        return jsonify({"error": "Chat session not found."}), 404
    start_offset = _bounded_int(
        request.args.get("start_offset"),
        default=0,
        minimum=0,
        maximum=2_000_000_000,
    )
    max_chars = _bounded_int(
        request.args.get("max_chars"),
        default=CONTEXT_PREVIEW_DEFAULT_CHARS,
        minimum=1,
        maximum=CONTEXT_PREVIEW_MAX_CHARS,
    )
    try:
        payload = read_context_item_evidence(
            context_item_id,
            user_id=int(current_user.id),
            session_id=session_id,
            start_offset=start_offset,
            max_chars=max_chars,
        )
    except ContextFabricError:
        return jsonify({"error": "Context item not found."}), 404

    item = payload["context_item"]
    scoped_item = db.session.get(ChatContextItem, str(item["id"]))
    evidence = payload.get("evidence") or {}
    response = jsonify(
        {
            "item": _context_item_summary(scoped_item),
            "content": str(payload.get("content") or ""),
            "start_offset": int(payload.get("start_offset") or 0),
            "end_offset": int(payload.get("end_offset") or 0),
            "has_more": bool(payload.get("has_more")),
            "next_offset": payload.get("next_offset"),
            "integrity_verified": bool(payload.get("integrity_verified")),
            "evidence": {
                "id": str(evidence.get("id") or scoped_item.evidence_id),
                "kind": str(evidence.get("kind") or scoped_item.evidence.kind),
                "role": evidence.get("role"),
                "mime_type": (
                    evidence.get("mime_type")
                    or scoped_item.evidence.mime_type
                ),
                "char_count": int(
                    evidence.get("char_count")
                    or scoped_item.evidence.char_count
                    or 0
                ),
                "byte_count": int(
                    evidence.get("byte_count")
                    or scoped_item.evidence.byte_count
                    or 0
                ),
                "token_estimate": (
                    int(evidence["token_estimate"])
                    if evidence.get("token_estimate") is not None
                    else scoped_item.evidence.token_estimate
                ),
                "content_hash": str(
                    evidence.get("content_hash")
                    or scoped_item.evidence.content_hash
                    or ""
                ),
            },
        }
    )
    response.headers["Cache-Control"] = "private, no-store"
    return response


@frontend_context_blueprint.route(
    "/api/frontend/chats/<session_id>/context-items/<context_item_id>",
    methods=["PATCH"],
)
@login_required
def update_frontend_context_item(session_id: str, context_item_id: str):
    """Apply an explicit, optimistic and audited user lifecycle transition."""
    chat_session = _owned_chat_session(session_id)
    if chat_session is None:
        return jsonify({"error": "Chat session not found."}), 404
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"error": "A JSON request body is required."}), 400
    action = str(body.get("action") or "").strip().lower()
    action_aliases = {
        "materialize_full": "full",
        "materialize_reference": "reference",
        "activate": "restore",
        "summarize": "summary",
    }
    action = action_aliases.get(action, action)
    valid_actions = {
        "restore",
        "archive",
        "exclude",
        "full",
        "reference",
        "pin",
        "unpin",
        "summary",
        "excerpt",
        "priority",
    }
    if action not in valid_actions:
        return jsonify({"error": "The context action is invalid."}), 400
    expected_version = body.get("expected_version")
    if isinstance(expected_version, bool):
        return jsonify({"error": "expected_version must be an integer."}), 400
    try:
        expected_version = int(expected_version)
    except (TypeError, ValueError):
        return jsonify({"error": "expected_version must be an integer."}), 400
    if expected_version < 1:
        return jsonify({"error": "expected_version must be positive."}), 400

    transition: dict[str, Any] = {}
    if action == "restore":
        transition.update(
            lifecycle_state="active",
            materialization="reference",
        )
    elif action == "archive":
        transition.update(
            lifecycle_state="archived",
            materialization="reference",
        )
    elif action == "exclude":
        transition.update(
            lifecycle_state="excluded",
            materialization="reference",
        )
    elif action == "full":
        transition.update(
            lifecycle_state="active",
            materialization="full",
        )
    elif action == "reference":
        transition.update(
            lifecycle_state="active",
            materialization="reference",
        )
    elif action == "pin":
        transition["is_pinned"] = True
    elif action == "unpin":
        transition["is_pinned"] = False
    elif action in {"summary", "excerpt"}:
        materialized_content = str(body.get("content") or "").strip()
        if not materialized_content:
            return jsonify(
                {"error": f"{action} requires non-empty content."}
            ), 400
        if len(materialized_content) > 200_000:
            return jsonify(
                {"error": f"{action} content exceeds 200,000 characters."}
            ), 400
        transition.update(
            lifecycle_state="active",
            materialization=action,
            materialized_content=materialized_content,
        )
    elif action == "priority":
        priority = body.get("priority")
        if isinstance(priority, bool):
            return jsonify({"error": "priority must be an integer."}), 400
        try:
            priority = int(priority)
        except (TypeError, ValueError):
            return jsonify({"error": "priority must be an integer."}), 400
        if priority < 0 or priority > 100:
            return jsonify({"error": "priority must be between 0 and 100."}), 400
        transition["priority"] = priority

    reason = str(
        body.get("reason")
        or f"User selected {action} in Context Inspector."
    ).strip()[:1_000]
    try:
        item = transition_context_item(
            context_item_id,
            expected_version=expected_version,
            user_id=int(current_user.id),
            session_id=session_id,
            actor_type="user",
            actor_id=str(current_user.id),
            reason=reason,
            **transition,
        )
        db.session.commit()
    except ContextVersionConflict as exc:
        db.session.rollback()
        return jsonify(
            {
                "error": str(exc),
                "code": "context_version_conflict",
            }
        ), 409
    except ContextPolicyError as exc:
        db.session.rollback()
        return jsonify(
            {
                "error": str(exc),
                "code": "context_policy_error",
            }
        ), 409
    except ContextFabricError:
        db.session.rollback()
        return jsonify({"error": "Context item not found."}), 404
    except (TypeError, ValueError) as exc:
        db.session.rollback()
        return jsonify({"error": str(exc)}), 400

    response = jsonify(
        {
            "status": "updated",
            "action": action,
            "item": _context_item_summary(item),
        }
    )
    response.headers["Cache-Control"] = "private, no-store"
    return response


@frontend_context_blueprint.route(
    "/api/frontend/chats/<session_id>/artifacts",
    methods=["GET"],
)
@login_required
def list_frontend_workspace_artifacts(session_id: str):
    chat_session = _owned_chat_session(session_id)
    if chat_session is None:
        return jsonify({"error": "Chat session not found."}), 404

    limit = _bounded_int(
        request.args.get("limit"),
        default=24,
        minimum=1,
        maximum=WORKSPACE_PAGE_MAX_LIMIT,
    )
    status = str(request.args.get("status") or "all").strip().lower()
    if status not in {
        "all",
        "draft",
        "pending_review",
        "approved",
        "rejected",
        "changes_requested",
        "archived",
    }:
        return jsonify({"error": "The artifact status is invalid."}), 400
    query_text = " ".join(
        str(request.args.get("q") or "").strip().lower().split()
    )[:200]
    context = {
        "kind": "artifacts",
        "session_id": session_id,
        "status": status,
        "q": query_text,
    }
    try:
        offset = _decode_offset_cursor(
            str(request.args.get("cursor") or ""),
            context=context,
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return jsonify({"error": "The artifact cursor is invalid."}), 400

    query = _session_artifact_query(session_id)
    if status != "all":
        query = query.filter(RuntimeArtifact.review_status == status)
    query = query.order_by(
        RuntimeArtifact.updated_at.desc(),
        RuntimeArtifact.id.desc(),
    )

    search_truncated = False
    if query_text:
        candidates = query.limit(1_001).all()
        search_truncated = len(candidates) > 1_000
        terms = query_text.split()
        filtered = [
            artifact
            for artifact in candidates[:1_000]
            if all(
                term
                in " ".join(
                    [
                        _artifact_label(artifact),
                        str(artifact.artifact_type or ""),
                        str(artifact.mime_type or ""),
                        str(artifact.review_status or ""),
                        str(artifact.source_addon_id or ""),
                    ]
                ).lower()
                for term in terms
            )
        ]
        total = len(filtered)
        visible_rows = filtered[offset : offset + limit]
        has_more = offset + limit < total
    else:
        total = int(
            query.order_by(None)
            .with_entities(func.count(RuntimeArtifact.id))
            .scalar()
            or 0
        )
        rows = query.offset(offset).limit(limit + 1).all()
        has_more = len(rows) > limit
        visible_rows = rows[:limit]

    version_numbers = _artifact_version_numbers(
        [str(row.id) for row in visible_rows]
    )
    response = jsonify(
        {
            "items": [
                _artifact_summary(
                    row,
                    version_number=version_numbers.get(str(row.id), 0),
                )
                for row in visible_rows
            ],
            "total": total,
            "next_cursor": (
                _encode_offset_cursor(
                    offset + limit,
                    context=context,
                )
                if has_more
                else None
            ),
            "search_truncated": search_truncated,
        }
    )
    response.headers["Cache-Control"] = "private, no-cache"
    return response


def _owned_session_artifact(
    session_id: str,
    artifact_id: str,
) -> RuntimeArtifact | None:
    return (
        _session_artifact_query(session_id)
        .filter(RuntimeArtifact.id == artifact_id)
        .first()
    )


@frontend_context_blueprint.route(
    "/api/frontend/chats/<session_id>/artifacts/<artifact_id>",
    methods=["GET"],
)
@login_required
def get_frontend_workspace_artifact(
    session_id: str,
    artifact_id: str,
):
    chat_session = _owned_chat_session(session_id)
    if chat_session is None:
        return jsonify({"error": "Chat session not found."}), 404
    artifact = _owned_session_artifact(session_id, artifact_id)
    if artifact is None:
        return jsonify({"error": "Artifact not found."}), 404

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
    source_ids: set[str] = set()
    evidence_ids: set[str] = set()
    for version in visible_versions:
        source_ids.update(str(value) for value in (version.source_ids or []))
        evidence_ids.update(
            str(value) for value in (version.evidence_ids or [])
        )

    evidence_query = RuntimeEvidenceRecord.query.filter(
        RuntimeEvidenceRecord.workflow_run_id == artifact.workflow_run_id
    ).filter(
        or_(
            RuntimeEvidenceRecord.artifact_id == artifact.id,
            RuntimeEvidenceRecord.id.in_(evidence_ids)
            if evidence_ids
            else false(),
        )
    )
    evidence_rows = (
        evidence_query.order_by(RuntimeEvidenceRecord.created_at.desc())
        .limit(100)
        .all()
    )
    source_ids.update(
        str(row.source_id) for row in evidence_rows if row.source_id
    )
    source_rows = (
        RuntimeSourceRecord.query.filter(
            RuntimeSourceRecord.workflow_run_id
            == artifact.workflow_run_id
        )
        .filter(RuntimeSourceRecord.id.in_(source_ids))
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
            int(current_version.version_number)
            if current_version
            else 0
        ),
    )
    response = jsonify(
        {
            "artifact": {
                **summary,
                "metadata": _semantic_value(
                    artifact.metadata_json
                    if isinstance(artifact.metadata_json, dict)
                    else {}
                ),
                "review": {
                    "status": str(artifact.review_status or "draft"),
                    "note": _semantic_value(artifact.review_note or ""),
                    "reviewed_at": (
                        artifact.reviewed_at.isoformat()
                        if artifact.reviewed_at
                        else None
                    ),
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
                    "label": str(
                        version.label
                        or _artifact_label(artifact)
                    ),
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
                    "created_at": (
                        version.created_at.isoformat()
                        if version.created_at
                        else None
                    ),
                    "current": bool(
                        (
                            artifact.current_version_id
                            and version.id
                            == artifact.current_version_id
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
            "sources": [
                {
                    "id": str(source.id),
                    "title": str(
                        source.title
                        or _humanize(
                            source.source_type,
                            fallback="Source",
                        )
                    ),
                    "summary": _value_summary(source.summary or ""),
                    "type": str(source.source_type or "source"),
                    "provider": (
                        str(source.provider) if source.provider else None
                    ),
                    "url": _safe_external_url(source.uri),
                    "created_at": (
                        source.created_at.isoformat()
                        if source.created_at
                        else None
                    ),
                }
                for source in source_rows
            ],
            "evidence": [
                {
                    "id": str(evidence.id),
                    "claim": _value_summary(evidence.claim or ""),
                    "quote": _value_summary(evidence.quote or ""),
                    "citation": _semantic_value(evidence.citation or {}),
                    "relevance_score": evidence.relevance_score,
                    "novelty_score": evidence.novelty_score,
                    "used_in_artifact": bool(
                        evidence.used_in_artifact
                    ),
                    "source": (
                        {
                            "id": str(source.id),
                            "title": str(
                                source.title
                                or _humanize(
                                    source.source_type,
                                    fallback="Source",
                                )
                            ),
                            "url": _safe_external_url(source.uri),
                        }
                        if (
                            source := sources_by_id.get(
                                str(evidence.source_id)
                            )
                        )
                        else None
                    ),
                    "created_at": (
                        evidence.created_at.isoformat()
                        if evidence.created_at
                        else None
                    ),
                }
                for evidence in evidence_rows
            ],
        }
    )
    response.headers["Cache-Control"] = "private, no-store"
    return response


def register_frontend_context_blueprint(app):
    app.register_blueprint(frontend_context_blueprint)


__all__ = [
    "frontend_context_blueprint",
    "register_frontend_context_blueprint",
]
