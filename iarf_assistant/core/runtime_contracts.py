from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any, Callable


TOOL_RESULT_ENVELOPE_VERSION = 1
DEFAULT_INLINE_RESULT_CHARS = 12000
DEFAULT_RESULT_PREVIEW_CHARS = 2400


def stable_json(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        return str(value)


def estimate_tokens(value: Any) -> int:
    text = stable_json(value)
    return max(1, (len(text) + 3) // 4) if text else 0


def bounded_text(value: Any, limit: int) -> str:
    text = stable_json(value)
    safe_limit = max(0, int(limit or 0))
    if not safe_limit or len(text) <= safe_limit:
        return text
    suffix = "... [truncated]"
    return text[: max(0, safe_limit - len(suffix))].rstrip() + suffix


def provider_identifier_fingerprint(value: Any) -> str | None:
    text = str(value or "")
    if not text:
        return None
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]


@dataclass(frozen=True)
class RuntimeDataRef:
    id: str
    kind: str
    label: str = ""
    preview: str = ""
    mime_type: str | None = None
    size_bytes: int | None = None
    estimated_tokens: int | None = None
    lineage: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            key: value
            for key, value in asdict(self).items()
            if value not in (None, "", [], {})
        }


@dataclass(frozen=True)
class ToolResultEnvelope:
    tool_name: str
    internal_tool_call_id: str
    status: str
    action: str = "tool"
    summary: str = ""
    inline_result: Any = None
    data_refs: tuple[RuntimeDataRef, ...] = ()
    artifact_refs: tuple[str, ...] = ()
    source_refs: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    error: str | None = None
    truncated: bool = False
    raw_chars: int = 0
    estimated_raw_tokens: int = 0
    duration_ms: int | None = None
    estimated_cost: float | None = None
    provider_tool_call_fingerprint: str | None = None
    provider_tool_call_id_chars: int = 0
    schema_version: int = TOOL_RESULT_ENVELOPE_VERSION

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["data_refs"] = [item.to_dict() for item in self.data_refs]
        for key in (
            "artifact_refs",
            "source_refs",
            "evidence_refs",
            "warnings",
        ):
            payload[key] = list(payload[key])
        return {
            key: value
            for key, value in payload.items()
            if value not in (None, "", [], (), {})
        }


@dataclass(frozen=True)
class CostAttribution:
    chat_session_id: str | None = None
    workflow_run_id: str | None = None
    workflow_step_id: str | None = None
    runtime_job_id: str | None = None
    turn_id: str | None = None
    action_id: str | None = None

    @classmethod
    def from_value(cls, value: Any) -> "CostAttribution":
        if isinstance(value, cls):
            return value
        payload = value if isinstance(value, dict) else {}
        return cls(
            chat_session_id=_optional_text(
                payload.get("chat_session_id") or payload.get("session_id")
            ),
            workflow_run_id=_optional_text(payload.get("workflow_run_id")),
            workflow_step_id=_optional_text(payload.get("workflow_step_id")),
            runtime_job_id=_optional_text(payload.get("runtime_job_id")),
            turn_id=_optional_text(payload.get("turn_id")),
            action_id=_optional_text(payload.get("action_id")),
        )

    def merged(self, value: Any) -> "CostAttribution":
        other = self.from_value(value)
        return CostAttribution(
            chat_session_id=other.chat_session_id or self.chat_session_id,
            workflow_run_id=other.workflow_run_id or self.workflow_run_id,
            workflow_step_id=other.workflow_step_id or self.workflow_step_id,
            runtime_job_id=other.runtime_job_id or self.runtime_job_id,
            turn_id=other.turn_id or self.turn_id,
            action_id=other.action_id or self.action_id,
        )

    def to_dict(self) -> dict[str, str]:
        return {
            key: value
            for key, value in asdict(self).items()
            if value
        }


def _optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _string_refs(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    rows = value if isinstance(value, (list, tuple, set)) else [value]
    return tuple(dict.fromkeys(str(item) for item in rows if item))


def _result_status(result: Any, error: str | None) -> str:
    if error:
        return "error"
    if not isinstance(result, dict):
        return "ok"
    status = str(result.get("status") or "").strip().lower()
    if status:
        return status
    return "error" if result.get("error") else "ok"


def _result_summary(result: Any, *, status: str, preview_chars: int) -> str:
    if isinstance(result, dict):
        for key in ("summary", "message", "error", "text", "result"):
            value = result.get(key)
            if isinstance(value, (str, int, float, bool)) and str(value).strip():
                return bounded_text(value, min(preview_chars, 600))
    if status == "ok":
        return bounded_text(result, min(preview_chars, 600))
    return bounded_text(result, preview_chars)


def _result_cost(result: Any) -> float | None:
    if not isinstance(result, dict):
        return None
    for key in ("cost", "estimated_cost", "cost_usd"):
        value = result.get(key)
        try:
            if value is not None:
                return float(value)
        except (TypeError, ValueError):
            continue
    usage = result.get("usage")
    return _result_cost(usage) if isinstance(usage, dict) else None


def discover_runtime_refs(result: Any) -> tuple[
    tuple[RuntimeDataRef, ...],
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
]:
    if not isinstance(result, dict):
        return (), (), (), ()

    data_refs: list[RuntimeDataRef] = []
    artifact_refs = list(_string_refs(result.get("artifact_id")))
    artifact = result.get("artifact")
    if isinstance(artifact, dict) and artifact.get("id"):
        artifact_refs.append(str(artifact["id"]))
    raw_artifacts = result.get("artifacts")
    artifacts = raw_artifacts if isinstance(raw_artifacts, list) else []
    for item in artifacts:
        if isinstance(item, dict) and item.get("id"):
            artifact_refs.append(str(item["id"]))
        elif item:
            artifact_refs.append(str(item))

    context_ref = result.get("context_ref_id")
    if context_ref:
        data_refs.append(RuntimeDataRef(
            id=str(context_ref),
            kind="context_result",
            label=str(result.get("tool_name") or "Retained tool result"),
            preview=bounded_text(result.get("preview") or result.get("summary") or "", 600),
        ))

    resource_ids = (
        result.get("resource_reference_ids")
        or result.get("runtime_resource_ids")
        or result.get("data_ref_ids")
        or []
    )
    for ref_id in _string_refs(resource_ids):
        data_refs.append(RuntimeDataRef(id=ref_id, kind="resource"))

    for artifact_id in tuple(dict.fromkeys(artifact_refs)):
        data_refs.append(RuntimeDataRef(
            id=f"artifact:{artifact_id}",
            kind="artifact",
            label="Runtime artifact",
        ))

    source_refs = _string_refs(result.get("source_ids") or result.get("source_refs"))
    evidence_refs = _string_refs(result.get("evidence_ids") or result.get("evidence_refs"))
    return (
        tuple(data_refs),
        tuple(dict.fromkeys(artifact_refs)),
        source_refs,
        evidence_refs,
    )


def build_tool_result_envelope(
    *,
    tool_name: str,
    internal_tool_call_id: str,
    result: Any,
    action: str = "tool",
    provider_tool_call_id: str | None = None,
    duration_ms: int | None = None,
    error: str | None = None,
    inline_limit_chars: int = DEFAULT_INLINE_RESULT_CHARS,
    preview_chars: int = DEFAULT_RESULT_PREVIEW_CHARS,
    externalize: Callable[[str, Any], RuntimeDataRef | None] | None = None,
) -> ToolResultEnvelope:
    raw = stable_json(result)
    status = _result_status(result, error)
    truncated = len(raw) > max(1000, int(inline_limit_chars or DEFAULT_INLINE_RESULT_CHARS))
    data_refs, artifact_refs, source_refs, evidence_refs = discover_runtime_refs(result)
    refs = list(data_refs)
    warnings: list[str] = []

    if truncated and callable(externalize):
        try:
            external_ref = externalize(raw, result)
            if external_ref is not None:
                refs.append(external_ref)
                if external_ref.kind == "artifact":
                    artifact_id = external_ref.id.split(":", 1)[-1]
                    artifact_refs = tuple(dict.fromkeys([*artifact_refs, artifact_id]))
        except Exception as exc:
            warnings.append(f"Could not externalize the full tool result: {exc}")

    provider_id = str(provider_tool_call_id or "")
    return ToolResultEnvelope(
        tool_name=str(tool_name or ""),
        internal_tool_call_id=str(internal_tool_call_id or ""),
        status=status,
        action=str(action or "tool"),
        summary=_result_summary(result, status=status, preview_chars=preview_chars),
        inline_result=None if truncated else result,
        data_refs=tuple(refs),
        artifact_refs=artifact_refs,
        source_refs=source_refs,
        evidence_refs=evidence_refs,
        warnings=tuple(warnings),
        error=error or (
            str(result.get("error"))
            if isinstance(result, dict) and result.get("error")
            else None
        ),
        truncated=truncated,
        raw_chars=len(raw),
        estimated_raw_tokens=estimate_tokens(raw),
        duration_ms=duration_ms,
        estimated_cost=_result_cost(result),
        provider_tool_call_fingerprint=provider_identifier_fingerprint(provider_id),
        provider_tool_call_id_chars=len(provider_id),
    )


def tool_message_from_envelope(
    envelope: ToolResultEnvelope | dict[str, Any],
    *,
    result: Any,
    provider_tool_call_id: str | None = None,
) -> dict[str, Any]:
    """Adapt an envelope to the standard provider tool-message shape."""
    payload = envelope.to_dict() if isinstance(envelope, ToolResultEnvelope) else dict(envelope)
    compacted = bool(payload.get("truncated"))
    if compacted:
        content_payload = {
            "status": "result_externalized",
            "tool_name": payload.get("tool_name"),
            "summary": payload.get("summary") or "",
            "data_refs": payload.get("data_refs") or [],
            "artifact_refs": payload.get("artifact_refs") or [],
            "raw_chars": payload.get("raw_chars") or 0,
            "estimated_raw_tokens": payload.get("estimated_raw_tokens") or 0,
            "instruction": (
                "Use runtime_read_data_ref with a data_ref_id when a targeted "
                "slice of the retained payload is needed."
            ),
        }
    else:
        content_payload = result

    internal_id = str(payload.get("internal_tool_call_id") or "")
    message = {
        "role": "tool",
        "tool_call_id": internal_id,
        "name": str(payload.get("tool_name") or ""),
        "content": stable_json(content_payload),
        "_tool_result_envelope": payload,
    }
    provider_id = str(provider_tool_call_id or "")
    if provider_id and provider_id != internal_id:
        message["_provider_tool_call_id"] = provider_id
    return message
