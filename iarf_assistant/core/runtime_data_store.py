from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from iarf_assistant.core.runtime_contracts import (
    RuntimeDataRef,
    bounded_text,
    estimate_tokens,
)
from iarf_assistant.core.runtime_state import (
    create_local_file_artifact,
    resolve_artifact_path,
)
from iarf_assistant.extensions import db
from iarf_assistant.models import RuntimeArtifact


_SAFE_SEGMENT_RE = re.compile(r"[^A-Za-z0-9_.-]+")
RUNTIME_PAYLOAD_KINDS = {
    "runtime_tool_result_payload",
    "runtime_dependency_output_payload",
    "chat_tool_result_raw_payload",
}


def _safe_segment(value: Any, fallback: str) -> str:
    text = _SAFE_SEGMENT_RE.sub("_", str(value or "")).strip("._")
    return (text or fallback)[:96]


def externalize_runtime_payload(
    raw_content: str,
    *,
    instance_path: str | Path,
    namespace: str,
    created_by_user_id: int | None = None,
    workflow_run_id: str | None = None,
    workflow_step_id: str | None = None,
    runtime_job_id: str | None = None,
    label: str = "Runtime result payload",
    kind: str = "runtime_tool_result_payload",
    lineage: dict[str, Any] | None = None,
    source_ids: Any = None,
    evidence_ids: Any = None,
    commit: bool = False,
) -> RuntimeDataRef:
    """Persist a large runtime payload once and return a bounded portable reference."""
    content = str(raw_content or "")
    content_hash = hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()
    lineage_payload = dict(lineage or {})
    lineage_seed = repr(sorted((str(key), str(value)) for key, value in lineage_payload.items()))
    lineage_hash = hashlib.sha256(lineage_seed.encode("utf-8", errors="replace")).hexdigest()[:12]
    existing_artifact = None
    candidates = RuntimeArtifact.query.filter_by(
        content_hash=content_hash,
        workflow_run_id=workflow_run_id,
        created_by_user_id=created_by_user_id,
    ).all()
    for candidate in candidates:
        candidate_metadata = (
            candidate.metadata_json
            if isinstance(candidate.metadata_json, dict)
            else {}
        )
        if (
            candidate_metadata.get("kind") == kind
            and candidate_metadata.get("namespace") == namespace
            and candidate_metadata.get("lineage") == lineage_payload
        ):
            try:
                candidate_path = resolve_artifact_path(
                    candidate,
                    instance_path=instance_path,
                )
            except ValueError:
                candidate_path = None
            if candidate_path and candidate_path.exists() and candidate_path.is_file():
                existing_artifact = candidate
                break
    if existing_artifact is not None:
        return RuntimeDataRef(
            id=f"artifact:{existing_artifact.id}",
            kind="artifact",
            label=existing_artifact.label or label,
            preview=bounded_text(content, 600),
            mime_type=existing_artifact.mime_type or "text/plain",
            size_bytes=existing_artifact.size_bytes,
            estimated_tokens=estimate_tokens(content),
            lineage=lineage_payload,
            metadata={
                "artifact_id": existing_artifact.id,
                "content_hash": content_hash,
                "kind": kind,
                "reused": True,
            },
        )

    output_path = (
        Path(instance_path).resolve()
        / "artifacts"
        / "runtime_payloads"
        / _safe_segment(namespace, "runtime")
        / f"{content_hash[:20]}-{lineage_hash}.txt"
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(content, encoding="utf-8")
    retention_policy = (
        "chat_context_result"
        if kind == "chat_tool_result_raw_payload"
        else "runtime_context_result"
    )

    artifact = create_local_file_artifact(
        instance_path=instance_path,
        file_path=output_path,
        job_id=runtime_job_id,
        workflow_run_id=workflow_run_id,
        step_id=workflow_step_id,
        created_by_user_id=created_by_user_id,
        label=label,
        content_hash=content_hash,
        source_ids=source_ids,
        evidence_ids=evidence_ids,
        metadata={
            "kind": kind,
            "namespace": namespace,
            "workflow_run_id": workflow_run_id,
            "workflow_step_id": workflow_step_id,
            "runtime_job_id": runtime_job_id,
            "raw_chars": len(content),
            "estimated_raw_tokens": estimate_tokens(content),
            "content_hash": content_hash,
            "lineage": lineage_payload,
        },
        change_summary="Stored a large runtime payload behind a durable reference.",
        retention_policy=retention_policy,
        download_permission="none",
        commit=commit,
    )
    return RuntimeDataRef(
        id=f"artifact:{artifact.id}",
        kind="artifact",
        label=label,
        preview=bounded_text(content, 600),
        mime_type=artifact.mime_type or "text/plain",
        size_bytes=len(content.encode("utf-8", errors="replace")),
        estimated_tokens=estimate_tokens(content),
        lineage=lineage_payload,
        metadata={
            "artifact_id": artifact.id,
            "content_hash": content_hash,
            "kind": kind,
        },
    )


def read_runtime_data_ref(
    ref_id: str,
    *,
    instance_path: str | Path,
    owner_user_id: int | None = None,
    workflow_run_id: str | None = None,
    start_offset: int = 0,
    max_chars: int = 8000,
) -> dict[str, Any]:
    """Read a bounded slice without exposing arbitrary artifact paths."""
    artifact_id = str(ref_id or "").strip()
    if artifact_id.startswith("artifact:"):
        artifact_id = artifact_id.split(":", 1)[1]
    artifact = db.session.get(RuntimeArtifact, artifact_id)
    if not artifact:
        return {"status": "not_found", "data_ref_id": ref_id}
    if owner_user_id is not None and artifact.created_by_user_id not in {None, owner_user_id}:
        return {"status": "forbidden", "data_ref_id": ref_id}
    if workflow_run_id is not None and artifact.workflow_run_id not in {None, workflow_run_id}:
        return {"status": "forbidden", "data_ref_id": ref_id}
    metadata = artifact.metadata_json if isinstance(artifact.metadata_json, dict) else {}
    if metadata.get("kind") not in RUNTIME_PAYLOAD_KINDS:
        return {
            "status": "unsupported",
            "data_ref_id": ref_id,
            "error": "The reference is not a readable runtime payload.",
        }
    try:
        path = resolve_artifact_path(artifact, instance_path=instance_path)
    except ValueError:
        path = None
    if not path or not path.exists() or not path.is_file():
        return {"status": "unavailable", "data_ref_id": ref_id}

    try:
        start = max(0, int(start_offset or 0))
    except (TypeError, ValueError):
        start = 0
    try:
        limit = max(1, min(int(max_chars or 8000), 50000))
    except (TypeError, ValueError):
        limit = 8000
    content = path.read_text(encoding="utf-8", errors="replace")
    end = min(len(content), start + limit)
    return {
        "status": "ok",
        "data_ref_id": f"artifact:{artifact.id}",
        "artifact_id": artifact.id,
        "label": artifact.label,
        "start_offset": start,
        "end_offset": end,
        "raw_chars": len(content),
        "estimated_raw_tokens": estimate_tokens(content),
        "truncated": end < len(content),
        "content": content[start:end],
        "lineage": metadata.get("lineage") or {},
    }
