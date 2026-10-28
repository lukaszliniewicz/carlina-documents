"""Passive model dispatch for Carlina-owned add-on jobs.

The add-on worker remains the workflow authority.  This module swaps only the
provider-I/O step for a durable MCP work item and reconstructs an ordinary
LiteLLM response after an external harness submits the assistant message.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final

from iarf_assistant.core.litellm_runtime import litellm
from iarf_assistant.core.task_queue import ExternalWorkPending

from .executor_profile import (
    ExecutorProfile,
    normalize_workload_role,
    require_executor_profile,
)

PASSIVE_PACKET_VERSION: Final = "carlina.passive-completion.v2"
PASSIVE_RETRY_SECONDS: Final = 5.0


class PassiveReplayConflict(BaseException):
    """Stop recovery before requesting replacement work for retained responses."""

    def __init__(self, dispatch: Mapping[str, Any]):
        self.batch_id = dispatch.get("batch_id")
        self.page = dispatch.get("unit_index")
        super().__init__("recovery_replay_conflict")


class PassiveAttemptSuperseded(BaseException):
    """An older queue attempt must not create work for a resumed execution."""


_REPLAYED_WORK: ContextVar[set[str] | None] = ContextVar("replayed_passive_work", default=None)

# Passive provider source for document-block translation windows (mirrors
# TRANSLATION_SOURCE in the translator add-on provider adapter without
# importing add-on code into core). Only this source is eligible for the
# translator-window-index preservation lookup below.
_TRANSLATION_WINDOW_SOURCE: Final = "translator_block_translation_window"
_TRANSLATION_WINDOW_INDEX_KEY: Final = "translator_window_index"
# Only the known generated index contract is eligible. A future or foreign
# entry under the same key keeps exact semantics and is never normalized.
_TRANSLATION_WINDOW_INDEX_CONTRACT: Final = "translator-window-block-index-v1"

_ALLOWED_PARAMETERS = frozenset(
    {
        "frequency_penalty",
        "logit_bias",
        "max_completion_tokens",
        "max_output_tokens",
        "max_tokens",
        "modalities",
        "n",
        "parallel_tool_calls",
        "presence_penalty",
        "reasoning_effort",
        "seed",
        "stop",
        "temperature",
        "tool_choice",
        "top_p",
    }
)


@dataclass(frozen=True, slots=True)
class PassiveExecutionContext:
    execution_id: str
    owner_user_id: int
    addon_id: str
    resource_kind: str
    resource_id: str
    executor_profile: ExecutorProfile
    queue_job_id: str | None = None


_PASSIVE_EXECUTION: ContextVar[PassiveExecutionContext | None] = ContextVar(
    "carlina_passive_execution",
    default=None,
)
_PASSIVE_DISPATCH: ContextVar[Mapping[str, Any] | None] = ContextVar(
    "carlina_passive_dispatch",
    default=None,
)


def _freeze_dispatch(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {key: _freeze_dispatch(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_dispatch(item) for item in value)
    return copy.deepcopy(value)


def _copy_dispatch(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _copy_dispatch(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_copy_dispatch(item) for item in value]
    return copy.deepcopy(value)


def _canonical_fingerprint(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _strip_ephemeral_file_paths(value: Any) -> Any:
    """Remove local file-path hints from JSON-shaped message content."""

    if isinstance(value, Mapping):
        return {
            key: _strip_ephemeral_file_paths(item)
            for key, item in value.items()
            if key != "file_path"
        }
    if isinstance(value, list):
        return [_strip_ephemeral_file_paths(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_strip_ephemeral_file_paths(item) for item in value)
    return value


@contextmanager
def passive_execution(
    *,
    execution_id: str,
    owner_user_id: int,
    addon_id: str,
    resource_kind: str,
    resource_id: str,
    queue_job_id: str | None = None,
) -> Iterator[None]:
    """Activate passive completion only for an exact authorized execution."""

    from iarf_assistant.core.mcp_jobs import get_execution

    execution = get_execution(str(execution_id), owner_user_id=int(owner_user_id))
    if execution is None:
        raise RuntimeError("The passive MCP execution is unavailable.")
    if (
        execution.addon_id != str(addon_id)
        or execution.resource_kind != str(resource_kind)
        or execution.resource_id != str(resource_id)
    ):
        raise RuntimeError("The passive MCP execution does not match this job.")
    if execution.mode != "passive":
        yield
        return
    if execution.status in {"completed", "failed", "cancelled"}:
        raise RuntimeError("The passive MCP execution is already terminal.")
    request = execution.request_json
    if not isinstance(request, Mapping):
        raise TypeError("The passive MCP execution request is malformed.")
    try:
        executor_profile = require_executor_profile(request.get("executor_profile"))
    except ValueError as exc:
        raise RuntimeError("The passive MCP executor profile is invalid.") from exc

    token = _PASSIVE_EXECUTION.set(
        PassiveExecutionContext(
            execution_id=execution.id,
            owner_user_id=int(owner_user_id),
            addon_id=str(addon_id),
            resource_kind=str(resource_kind),
            resource_id=str(resource_id),
            executor_profile=executor_profile,
            queue_job_id=queue_job_id,
        )
    )
    replay_token = _REPLAYED_WORK.set(set())
    try:
        yield
    finally:
        _REPLAYED_WORK.reset(replay_token)
        _PASSIVE_EXECUTION.reset(token)


def passive_execution_active() -> bool:
    return _PASSIVE_EXECUTION.get() is not None


def passive_execution_context() -> PassiveExecutionContext | None:
    """Return the immutable active passive context for provider-free callers."""

    return _PASSIVE_EXECUTION.get()


@contextmanager
def passive_dispatch(dispatch: Mapping[str, Any] | None) -> Iterator[None]:
    """Temporarily merge immutable dispatch metadata for passive packets."""

    current = _PASSIVE_DISPATCH.get()
    if dispatch is None:
        merged = current
    else:
        if not isinstance(dispatch, Mapping):
            raise TypeError("dispatch must be a JSON object or None.")
        merged_values = _copy_dispatch(current) if current is not None else {}
        merged_values.update(_copy_dispatch(dispatch))
        merged = _freeze_dispatch(merged_values)
    token = _PASSIVE_DISPATCH.set(merged)
    try:
        yield
    finally:
        _PASSIVE_DISPATCH.reset(token)


def build_passive_packet(
    *,
    executor_profile: ExecutorProfile | Mapping[str, Any],
    workload_role: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    response_format: dict[str, Any] | None,
    parameters: Mapping[str, Any],
    source: str,
) -> dict[str, Any]:
    """Build the provider-neutral completion packet for a harness."""

    normalized_profile = (
        executor_profile
        if isinstance(executor_profile, ExecutorProfile)
        else require_executor_profile(executor_profile)
    )

    safe_parameters = {
        key: copy.deepcopy(parameters[key])
        for key in sorted(_ALLOWED_PARAMETERS)
        if key in parameters
    }
    packet = {
        "schema_version": PASSIVE_PACKET_VERSION,
        "kind": "assistant_completion",
        "workload_role": normalize_workload_role(workload_role),
        "executor_profile": normalized_profile.to_dict(),
        "source": str(source or "unknown")[:255],
        "messages": _strip_ephemeral_file_paths(copy.deepcopy(messages)),
        "tools": copy.deepcopy(tools or []),
        "response_format": copy.deepcopy(response_format),
        "parameters": safe_parameters,
        "submission_contract": {
            "content": "Return the assistant content as a string. It may be empty only when tool_calls is non-empty.",
            "tool_calls": "Omit tool_calls when no tool is called. null and [] are accepted as omission. For actual calls, use OpenAI-style function tool calls with JSON-object arguments encoded as a JSON string.",
            "finish_reason": "Use stop or tool_calls unless the model reports another bounded reason.",
        },
    }
    dispatch = _PASSIVE_DISPATCH.get()
    if dispatch is not None:
        packet["dispatch"] = _copy_dispatch(dispatch)
    packet["request_fingerprint"] = _canonical_fingerprint(packet)
    return packet


def _completion_response(
    response: Mapping[str, Any],
    *,
    model: str,
    work_item_id: str,
):
    usage = response.get("usage")
    if not isinstance(usage, Mapping):
        usage = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }
    message: dict[str, Any] = {
        "role": "assistant",
        "content": str(response.get("content") or ""),
    }
    tool_calls = response.get("tool_calls")
    if isinstance(tool_calls, list) and tool_calls:
        message["tool_calls"] = copy.deepcopy(tool_calls)
    return litellm.ModelResponse(
        id=f"carlina-mcp-{work_item_id}",
        model=str(model),
        choices=[
            {
                "index": 0,
                "message": message,
                "finish_reason": str(response.get("finish_reason") or "stop"),
            }
        ],
        usage=dict(usage),
    )


def _translation_window_index_stripped_variant(
    packet: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Counterpart translation-window packet without the generated index.

    Returns None unless this is a translator translation-window packet that
    carries the generated translator_window_index dispatch entry. The
    counterpart removes only that entry (preserving every other dispatch
    field, and dropping dispatch itself only when nothing remains) and
    recomputes the embedded canonical fingerprint exactly as
    :func:`build_passive_packet` does, so both historical shapes
    (pre-index without, indexed with) keep their exact identities.
    """

    if packet.get("source") != _TRANSLATION_WINDOW_SOURCE:
        return None
    dispatch = packet.get("dispatch")
    if not isinstance(dispatch, Mapping):
        return None
    index_entry = dispatch.get(_TRANSLATION_WINDOW_INDEX_KEY)
    if not isinstance(index_entry, Mapping):
        return None
    if index_entry.get("contract_version") != _TRANSLATION_WINDOW_INDEX_CONTRACT:
        return None
    counterpart = copy.deepcopy(dict(packet))
    stripped = {
        key: copy.deepcopy(value)
        for key, value in dispatch.items()
        if key != _TRANSLATION_WINDOW_INDEX_KEY
    }
    if stripped:
        counterpart["dispatch"] = stripped
    else:
        counterpart.pop("dispatch", None)
    counterpart.pop("request_fingerprint", None)
    counterpart["request_fingerprint"] = _canonical_fingerprint(counterpart)
    return counterpart


def resolve_passive_completion(packet: Mapping[str, Any]):
    """Return a submitted response or suspend the queue delivery durably."""

    execution = _PASSIVE_EXECUTION.get()
    if execution is None:
        return None

    from iarf_assistant.core.mcp_jobs import (
        McpWorkError,
        get_or_create_work,
        has_work_for_request,
        transition_execution,
    )
    from iarf_assistant.extensions import db

    current = dict(packet)
    if execution.addon_id == "doc_processor":
        from ..models import McpExecution, McpWorkItem

        record = db.session.get(McpExecution, execution.execution_id)
        recovery = (record.request_json or {}).get("recovery", {}) if record else {}
        if execution.queue_job_id is not None:
            from sqlalchemy import update

            # Serialize with suspension/resume before work creation or reuse.
            # The existing completion transaction releases this short lock.
            with db.session.no_autoflush:
                current_attempt = db.session.execute(
                    update(McpExecution).where(
                        McpExecution.id == execution.execution_id,
                        McpExecution.owner_user_id == execution.owner_user_id,
                        McpExecution.queue_job_id == execution.queue_job_id,
                        McpExecution.status.in_(("queued", "running", "waiting_external")),
                    ).values(updated_at=McpExecution.updated_at)
                    .execution_options(synchronize_session=False)
                ).rowcount
            if current_attempt != 1:
                db.session.rollback()
                raise PassiveAttemptSuperseded("The extraction attempt is no longer current.")
            db.session.refresh(record)
            recovery = (record.request_json or {}).get("recovery", {})
        if recovery.get("generation", 0) and not has_work_for_request(record.id, current):
            dispatch = current.get("dispatch") or {}
            previous = McpWorkItem.query.filter_by(
                execution_id=record.id,
                state="completed",
                batch_id=dispatch.get("batch_id"),
                dispatch_phase=dispatch.get("phase"),
                unit_index=dispatch.get("unit_index"),
            ).with_entities(McpWorkItem.id).all()
            seen = _REPLAYED_WORK.get() or set()
            if any(work_id not in seen for (work_id,) in previous):
                raise PassiveReplayConflict(dispatch)
    variant = _translation_window_index_stripped_variant(current)
    if variant is None:
        resolution = get_or_create_work(execution.execution_id, current)
    else:
        with db.session.no_autoflush:
            current_exists = has_work_for_request(execution.execution_id, current)
            variant_exists = has_work_for_request(execution.execution_id, variant)
        if current_exists and variant_exists:
            raise McpWorkError(
                "request_fingerprint_ambiguous",
                "Both translation-window packet shapes exist for this execution; refusing to choose.",
                status_code=409,
            )
        if variant_exists:
            resolution = get_or_create_work(execution.execution_id, variant)
        else:
            resolution = get_or_create_work(execution.execution_id, current)
    if resolution.response is not None:
        seen = _REPLAYED_WORK.get()
        if seen is not None:
            seen.add(resolution.work_item_id)
        transition_execution(execution.execution_id, "running")
        from iarf_assistant.extensions import db

        db.session.commit()
        return _completion_response(
            resolution.response,
            model=str(packet.get("workload_role") or "completion"),
            work_item_id=resolution.work_item_id,
        )

    transition_execution(execution.execution_id, "waiting_external")
    from iarf_assistant.extensions import db

    db.session.commit()
    raise ExternalWorkPending(
        "Waiting for an MCP worker to return model output.",
        delay_seconds=PASSIVE_RETRY_SECONDS,
        work_item_id=resolution.work_item_id,
    )


__all__ = [
    "PASSIVE_PACKET_VERSION",
    "PassiveExecutionContext",
    "build_passive_packet",
    "passive_dispatch",
    "passive_execution",
    "passive_execution_active",
    "passive_execution_context",
    "resolve_passive_completion",
]
