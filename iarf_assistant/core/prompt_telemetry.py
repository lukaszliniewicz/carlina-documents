from __future__ import annotations

import copy
import json
import math
from typing import Any, Callable


PROMPT_TELEMETRY_VERSION = 1


def _json_text(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=True, sort_keys=True, default=str)
    except Exception:
        return str(value or "")


def _fallback_token_count(value: Any) -> int:
    text = _json_text(value)
    if not text:
        return 0
    return max(1, math.ceil(len(text) / 4))


def _count_messages(
    model: str,
    messages: list[dict[str, Any]],
    token_counter: Callable[..., Any] | None,
) -> tuple[int, str]:
    if not messages:
        return 0, "empty"
    if callable(token_counter):
        try:
            return max(0, int(token_counter(model=model, messages=messages))), "litellm"
        except Exception:
            pass
    return _fallback_token_count(messages), "chars_div_4"


def _count_text(
    model: str,
    text: str,
    token_counter: Callable[..., Any] | None,
) -> tuple[int, str]:
    if not text:
        return 0, "empty"
    if callable(token_counter):
        try:
            return max(0, int(token_counter(model=model, text=text))), "litellm"
        except Exception:
            pass
    return _fallback_token_count(text), "chars_div_4"


def measure_prompt_payload(
    *,
    model: str,
    messages: list[dict[str, Any]] | None,
    tools: list[dict[str, Any]] | None,
    token_counter: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Return content-free token and schema measurements for one provider payload."""
    clean_messages = [message for message in (messages or []) if isinstance(message, dict)]
    system_messages = [
        message for message in clean_messages
        if str(message.get("role") or "").lower() == "system"
    ]
    current_user_index = next(
        (
            index
            for index in range(len(clean_messages) - 1, -1, -1)
            if str(clean_messages[index].get("role") or "").lower() == "user"
        ),
        None,
    )
    current_user_messages = (
        [clean_messages[current_user_index]]
        if current_user_index is not None
        else []
    )
    history_messages = [
        message
        for index, message in enumerate(clean_messages)
        if str(message.get("role") or "").lower() != "system"
        and index != current_user_index
    ]

    system_tokens, system_method = _count_messages(model, system_messages, token_counter)
    history_tokens, history_method = _count_messages(model, history_messages, token_counter)
    user_tokens, user_method = _count_messages(model, current_user_messages, token_counter)
    message_tokens, message_method = _count_messages(model, clean_messages, token_counter)
    schema_text = _json_text(tools or []) if tools else ""
    schema_tokens, schema_method = _count_text(model, schema_text, token_counter)

    return {
        "system_tokens": system_tokens,
        "history_tokens": history_tokens,
        "current_user_tokens": user_tokens,
        "message_tokens": message_tokens,
        "declared_schema_tokens": schema_tokens,
        "declared_tool_count": len(tools or []),
        "estimated_context_tokens": message_tokens + schema_tokens,
        "message_count": len(clean_messages),
        "count_methods": {
            "system": system_method,
            "history": history_method,
            "current_user": user_method,
            "messages": message_method,
            "schemas": schema_method,
        },
    }


def ensure_turn_telemetry(sink: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(sink, dict):
        return None
    sink.setdefault("version", PROMPT_TELEMETRY_VERSION)
    sink.setdefault("first_payload", None)
    sink.setdefault("peak_turn_context_tokens", 0)
    sink.setdefault("model_requests", 0)
    sink.setdefault("model_turns", 0)
    sink.setdefault("tool_turns", 0)
    sink.setdefault("tool_calls", 0)
    sink.setdefault("tool_executions", 0)
    sink.setdefault("retries", 0)
    sink.setdefault("context_retries", 0)
    sink.setdefault("rate_limit_429s", 0)
    sink.setdefault("model_latency_ms", 0)
    sink.setdefault("tool_latency_ms", 0)
    sink.setdefault("prompt_tokens", 0)
    sink.setdefault("completion_tokens", 0)
    sink.setdefault("total_tokens", 0)
    sink.setdefault("cost", 0.0)
    sink.setdefault("calls", [])
    sink.setdefault("tool_batches", [])
    sink.setdefault("models", {})
    sink.setdefault("sources", {})
    return sink


def begin_model_call(
    sink: dict[str, Any] | None,
    *,
    model: str,
    source: str,
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    telemetry = ensure_turn_telemetry(sink)
    if telemetry is None:
        return None

    prompt_metrics = copy.deepcopy(payload)
    entry = {
        "index": len(telemetry["calls"]),
        "model": str(model or ""),
        "source": str(source or "unknown"),
        "status": "pending",
        "prompt": prompt_metrics,
    }
    telemetry["calls"].append(entry)
    telemetry["model_requests"] += 1
    if telemetry["first_payload"] is None:
        telemetry["first_payload"] = copy.deepcopy(prompt_metrics)
    telemetry["peak_turn_context_tokens"] = max(
        int(telemetry["peak_turn_context_tokens"] or 0),
        int(prompt_metrics.get("estimated_context_tokens") or 0),
    )
    return entry


def _usage_value(usage: Any, name: str) -> int:
    if isinstance(usage, dict):
        raw = usage.get(name, 0)
    else:
        raw = getattr(usage, name, 0) if usage is not None else 0
    try:
        return max(0, int(raw or 0))
    except (TypeError, ValueError):
        return 0


def _response_tool_call_count(response: Any) -> int:
    try:
        message = response.choices[0].message
        tool_calls = (
            message.get("tool_calls")
            if isinstance(message, dict)
            else getattr(message, "tool_calls", None)
        )
        return len(tool_calls or [])
    except Exception:
        return 0


def _rollup_bucket(
    container: dict[str, Any],
    key: str,
    *,
    prompt_tokens: int,
    completion_tokens: int,
    total_tokens: int,
    cost: float,
    latency_ms: int,
    retries: int,
    rate_limit_429s: int,
    tool_call_count: int,
) -> None:
    bucket = container.setdefault(
        key,
        {
            "model_turns": 0,
            "tool_turns": 0,
            "tool_calls": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "cost": 0.0,
            "latency_ms": 0,
            "retries": 0,
            "rate_limit_429s": 0,
        },
    )
    bucket["model_turns"] += 1
    bucket["tool_turns"] += int(tool_call_count > 0)
    bucket["tool_calls"] += tool_call_count
    bucket["prompt_tokens"] += prompt_tokens
    bucket["completion_tokens"] += completion_tokens
    bucket["total_tokens"] += total_tokens
    bucket["cost"] = round(float(bucket["cost"] or 0.0) + cost, 12)
    bucket["latency_ms"] += latency_ms
    bucket["retries"] += retries
    bucket["rate_limit_429s"] += rate_limit_429s


def finish_model_call(
    sink: dict[str, Any] | None,
    entry: dict[str, Any] | None,
    *,
    response: Any = None,
    cost: float | None = None,
    latency_ms: float = 0,
    attempts: int = 1,
    rate_limit_429s: int = 0,
    error: BaseException | None = None,
) -> None:
    telemetry = ensure_turn_telemetry(sink)
    if telemetry is None or entry is None:
        return

    attempts = max(1, int(attempts or 1))
    retries = max(0, attempts - 1)
    rate_limit_429s = max(0, int(rate_limit_429s or 0))
    rounded_latency = max(0, int(round(float(latency_ms or 0))))
    entry.update({
        "status": "error" if error is not None else "ok",
        "attempts": attempts,
        "retries": retries,
        "rate_limit_429s": rate_limit_429s,
        "latency_ms": rounded_latency,
    })
    telemetry["retries"] += retries
    telemetry["rate_limit_429s"] += rate_limit_429s
    telemetry["model_latency_ms"] += rounded_latency

    if error is not None:
        entry["error_type"] = type(error).__name__
        return

    usage = getattr(response, "usage", None)
    prompt_tokens = _usage_value(usage, "prompt_tokens")
    completion_tokens = _usage_value(usage, "completion_tokens")
    total_tokens = _usage_value(usage, "total_tokens")
    numeric_cost = float(cost) if isinstance(cost, (int, float)) else 0.0
    tool_call_count = _response_tool_call_count(response)

    entry["usage"] = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
    }
    entry["cost"] = numeric_cost
    entry["tool_call_count"] = tool_call_count
    telemetry["model_turns"] += 1
    telemetry["tool_turns"] += int(tool_call_count > 0)
    telemetry["tool_calls"] += tool_call_count
    telemetry["prompt_tokens"] += prompt_tokens
    telemetry["completion_tokens"] += completion_tokens
    telemetry["total_tokens"] += total_tokens
    telemetry["cost"] = round(float(telemetry["cost"] or 0.0) + numeric_cost, 12)
    telemetry["peak_turn_context_tokens"] = max(
        int(telemetry["peak_turn_context_tokens"] or 0),
        prompt_tokens,
    )

    model = str(entry.get("model") or "unknown")
    source = str(entry.get("source") or "unknown")
    for container, key in ((telemetry["models"], model), (telemetry["sources"], source)):
        _rollup_bucket(
            container,
            key,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            cost=numeric_cost,
            latency_ms=rounded_latency,
            retries=retries,
            rate_limit_429s=rate_limit_429s,
            tool_call_count=tool_call_count,
        )


def record_context_retry(sink: dict[str, Any] | None) -> None:
    telemetry = ensure_turn_telemetry(sink)
    if telemetry is None:
        return
    telemetry["context_retries"] += 1
    telemetry["retries"] += 1


def record_tool_executions(
    sink: dict[str, Any] | None,
    executions: list[Any] | None,
) -> None:
    telemetry = ensure_turn_telemetry(sink)
    if telemetry is None or not executions:
        return
    rows = []
    total_latency = 0
    for execution in executions:
        raw_duration = (
            execution.get("duration_ms", 0)
            if isinstance(execution, dict)
            else getattr(execution, "duration_ms", 0)
        )
        try:
            duration_ms = max(0, int(round(float(raw_duration or 0))))
        except (TypeError, ValueError):
            duration_ms = 0
        name = (
            execution.get("tool_name")
            if isinstance(execution, dict)
            else getattr(execution, "tool_name", None)
        )
        action = (
            execution.get("action")
            if isinstance(execution, dict)
            else getattr(execution, "action", None)
        )
        rows.append({
            "tool_name": str(name or "unknown"),
            "action": str(action or "execute"),
            "duration_ms": duration_ms,
        })
        total_latency += duration_ms

    telemetry["tool_executions"] += len(rows)
    telemetry["tool_latency_ms"] += total_latency
    telemetry["tool_batches"].append({
        "execution_count": len(rows),
        "total_latency_ms": total_latency,
        "max_latency_ms": max((row["duration_ms"] for row in rows), default=0),
        "executions": rows,
    })


def snapshot_turn_telemetry(sink: dict[str, Any] | None) -> dict[str, Any]:
    telemetry = ensure_turn_telemetry(sink)
    return copy.deepcopy(telemetry) if telemetry is not None else {}
