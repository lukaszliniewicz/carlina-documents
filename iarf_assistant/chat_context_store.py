from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from iarf_assistant.core.runtime_state import create_local_file_artifact, resolve_artifact_path
from iarf_assistant.extensions import db
from iarf_assistant.models import RuntimeArtifact


_SAFE_REF_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def _safe_ref_filename(ref_id: str) -> str:
    value = _SAFE_REF_RE.sub("_", str(ref_id or "tool-result")).strip("._")
    if not value:
        value = "tool-result"
    return value[:120]


def _tool_result_artifact_path(instance_path: str | Path, session_id: str, ref_id: str) -> Path:
    session_segment = _safe_ref_filename(session_id or "session")
    ref_segment = _safe_ref_filename(ref_id)
    return Path(instance_path).resolve() / "artifacts" / "chat_tool_results" / session_segment / f"{ref_segment}.txt"


def externalize_tool_result_message(
    message: dict[str, Any],
    *,
    instance_path: str | Path,
    session_id: str,
    created_by_user_id: int | None = None,
    commit: bool = False,
) -> RuntimeArtifact | None:
    """Move retained raw tool-result content from message metadata into a runtime artifact.

    The message remains self-describing for the model/UI, but no longer stores the
    full raw payload in the chat row. If artifact creation is not possible, callers
    can keep using the message metadata fallback.
    """
    if not isinstance(message, dict):
        return None
    meta = message.get("_context_optimization")
    if not isinstance(meta, dict) or not meta.get("compacted"):
        return None
    raw_content = meta.get("raw_content")
    if raw_content is None:
        return None
    raw_content = str(raw_content)
    ref_id = str(meta.get("ref_id") or "").strip()
    if not ref_id:
        return None

    output_path = _tool_result_artifact_path(instance_path, session_id, ref_id)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(raw_content, encoding="utf-8")

    content_hash = hashlib.sha256(raw_content.encode("utf-8", errors="replace")).hexdigest()
    artifact = create_local_file_artifact(
        instance_path=instance_path,
        file_path=output_path,
        created_by_user_id=created_by_user_id,
        label=f"Tool result {ref_id}",
        content_hash=content_hash,
        metadata={
            "kind": "chat_tool_result_raw_payload",
            "chat_session_id": session_id,
            "context_ref_id": ref_id,
            "tool_call_id": meta.get("tool_call_id"),
            "tool_name": meta.get("tool_name"),
            "raw_chars": meta.get("raw_chars"),
            "estimated_raw_tokens": meta.get("estimated_raw_tokens"),
            "content_hash": content_hash,
        },
        change_summary="Stored retained raw chat tool result payload.",
        retention_policy="chat_context_result",
        download_permission="none",
        commit=commit,
    )
    meta["raw_content_artifact_id"] = artifact.id
    meta["raw_content_storage"] = "runtime_artifact"
    meta["raw_content_hash"] = content_hash
    meta.pop("raw_content", None)
    envelope = message.get("_tool_result_envelope")
    if isinstance(envelope, dict):
        artifact_refs = list(envelope.get("artifact_refs") or [])
        if artifact.id not in artifact_refs:
            artifact_refs.append(artifact.id)
        data_refs = list(envelope.get("data_refs") or [])
        data_ref_id = f"artifact:{artifact.id}"
        if not any(
            isinstance(item, dict) and item.get("id") == data_ref_id
            for item in data_refs
        ):
            data_refs.append({
                "id": data_ref_id,
                "kind": "artifact",
                "label": artifact.label or f"Tool result {ref_id}",
                "preview": meta.get("preview") or "",
                "mime_type": artifact.mime_type,
                "size_bytes": artifact.size_bytes,
                "estimated_tokens": meta.get("estimated_raw_tokens"),
                "lineage": {
                    "chat_session_id": session_id,
                    "tool_call_id": meta.get("tool_call_id"),
                    "tool_name": meta.get("tool_name"),
                },
                "metadata": {
                    "kind": "chat_tool_result_raw_payload",
                    "artifact_id": artifact.id,
                    "content_hash": content_hash,
                },
            })
        envelope["artifact_refs"] = artifact_refs
        envelope["data_refs"] = data_refs
    return artifact


def read_tool_result_artifact_raw_content(
    artifact_id: str,
    *,
    instance_path: str | Path,
    owner_user_id: int | None = None,
) -> str | None:
    artifact = db.session.get(RuntimeArtifact, artifact_id)
    if not artifact:
        return None
    if owner_user_id is not None and artifact.created_by_user_id not in {None, owner_user_id}:
        return None
    metadata = artifact.metadata_json if isinstance(artifact.metadata_json, dict) else {}
    if metadata.get("kind") not in {
        "chat_tool_result_raw_payload",
        "runtime_tool_result_payload",
    }:
        return None
    try:
        path = resolve_artifact_path(artifact, instance_path=instance_path)
    except ValueError:
        return None
    if not path or not path.exists() or not path.is_file():
        return None
    return path.read_text(encoding="utf-8", errors="replace")
