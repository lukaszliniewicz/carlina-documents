from __future__ import annotations

import concurrent.futures
import copy
import hashlib
import json
from dataclasses import dataclass
import time
from typing import Any, Callable


INTERNAL_TOOL_CALL_PREFIX = "tc_"
MAX_INLINE_PROVIDER_TOOL_CALL_ID_CHARS = 80
FINALIZATION_REASON_LABELS = {
    "tool_call_limit": "the tool-call budget was reached",
    "empty_assistant_response": "the assistant produced no user-facing text",
}


@dataclass(frozen=True)
class LoopBudgetStatus:
    loop_index: int
    max_tool_call_iterations: int
    used_tool_call_iterations: int
    remaining_tool_call_iterations: int
    should_warn: bool
    should_force_finalization: bool


@dataclass(frozen=True)
class PreparedToolCall:
    index: int
    tool_call: Any
    tool_call_id: str
    provider_tool_call_id: str
    tool_name: str
    arguments: dict[str, Any]
    argument_error: str | None = None


@dataclass(frozen=True)
class ExecutedToolCall:
    prepared: PreparedToolCall
    output: dict[str, Any]
    duration_ms: int
    error: str | None = None
    result_envelope: dict[str, Any] | None = None

    @property
    def tool_call(self) -> Any:
        return self.prepared.tool_call

    @property
    def tool_call_id(self) -> str:
        return self.prepared.tool_call_id

    @property
    def provider_tool_call_id(self) -> str:
        return self.prepared.provider_tool_call_id

    @property
    def tool_name(self) -> str:
        return self.prepared.tool_name

    @property
    def arguments(self) -> dict[str, Any]:
        return self.prepared.arguments

    @property
    def action(self) -> str:
        return str(self.output.get("action") or "tool")

    @property
    def result(self) -> Any:
        return self.output.get("result")


def _message_value(message: Any, key: str, default: Any = None) -> Any:
    if isinstance(message, dict):
        return message.get(key, default)
    return getattr(message, key, default)


def _positive_int(value: Any, default: int) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return max(1, int(default))


def _nonnegative_int(value: Any, default: int = 0) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return max(0, int(default))


def loop_budget_status(
    loop_index: int,
    max_tool_calls: int,
    *,
    has_tool_calls: bool = False,
    warning_at_remaining: int = 2,
    minimum_warning_budget: int = 3,
) -> LoopBudgetStatus:
    max_iterations = _positive_int(max_tool_calls, 1)
    index = _nonnegative_int(loop_index)
    remaining = max(0, max_iterations - index)
    warn_remaining = _positive_int(warning_at_remaining, 2)
    warn_minimum = _positive_int(minimum_warning_budget, 1)
    return LoopBudgetStatus(
        loop_index=index,
        max_tool_call_iterations=max_iterations,
        used_tool_call_iterations=index,
        remaining_tool_call_iterations=remaining,
        should_warn=max_iterations >= warn_minimum and remaining == warn_remaining,
        should_force_finalization=bool(has_tool_calls) and index >= max_iterations - 1,
    )


def message_content_text(message: Any) -> str:
    content = _message_value(message, "content", "")
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        text_parts = []
        for part in content:
            if isinstance(part, str):
                text_parts.append(part)
            elif isinstance(part, dict):
                text = part.get("text")
                if isinstance(text, str):
                    text_parts.append(text)
        return "\n".join(part for part in text_parts if part)
    try:
        return json.dumps(content, ensure_ascii=True, default=str)
    except Exception:
        return str(content)


def message_tool_calls(message: Any) -> list[Any]:
    tool_calls = _message_value(message, "tool_calls", None) or []
    return list(tool_calls) if isinstance(tool_calls, (list, tuple)) else []


def tool_call_id(tool_call: Any) -> str:
    value = _message_value(tool_call, "id", None)
    return str(value or "")


def internal_tool_call_id(
    provider_tool_call_id: Any,
    *,
    index: int | None = None,
    tool_name: str | None = None,
) -> str:
    """Return a compact, provider-agnostic identifier for durable runtime state."""
    provider_id = str(provider_tool_call_id or "")
    if (
        provider_id
        and len(provider_id) <= MAX_INLINE_PROVIDER_TOOL_CALL_ID_CHARS
        and all(character.isprintable() for character in provider_id)
    ):
        return provider_id
    seed = provider_id or f"missing:{index if index is not None else ''}:{tool_name or ''}"
    digest = hashlib.sha256(seed.encode("utf-8", errors="replace")).hexdigest()[:24]
    return f"{INTERNAL_TOOL_CALL_PREFIX}{digest}"


def normalize_message_tool_call_ids(message: dict[str, Any]) -> dict[str, Any]:
    """Store compact IDs while retaining exact provider IDs in private metadata."""
    normalized = copy.deepcopy(message)
    if normalized.get("role") != "assistant":
        return normalized
    tool_calls = normalized.get("tool_calls")
    if not isinstance(tool_calls, list):
        return normalized

    existing_map = (
        normalized.get("_provider_tool_call_ids")
        if isinstance(normalized.get("_provider_tool_call_ids"), dict)
        else {}
    )
    provider_map: dict[str, str] = {}
    for index, call in enumerate(tool_calls):
        if not isinstance(call, dict):
            continue
        current_id = str(call.get("id") or "")
        provider_id = str(existing_map.get(current_id) or current_id)
        function = call.get("function") if isinstance(call.get("function"), dict) else {}
        compact_id = internal_tool_call_id(
            provider_id,
            index=index,
            tool_name=function.get("name"),
        )
        call["id"] = compact_id
        if provider_id and provider_id != compact_id:
            provider_map[compact_id] = provider_id
    if provider_map:
        normalized["_provider_tool_call_ids"] = provider_map
    else:
        normalized.pop("_provider_tool_call_ids", None)
    return normalized


def restore_provider_tool_call_ids(message: dict[str, Any]) -> dict[str, Any]:
    """Restore exact provider IDs at the network boundary and drop private metadata."""
    restored = copy.deepcopy(message)
    role = restored.get("role")
    if role == "assistant":
        provider_map = (
            restored.get("_provider_tool_call_ids")
            if isinstance(restored.get("_provider_tool_call_ids"), dict)
            else {}
        )
        for call in restored.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            compact_id = str(call.get("id") or "")
            if provider_map.get(compact_id):
                call["id"] = provider_map[compact_id]
    elif role == "tool":
        provider_id = str(restored.get("_provider_tool_call_id") or "")
        if provider_id:
            restored["tool_call_id"] = provider_id
    for key in list(restored):
        if str(key).startswith("_"):
            restored.pop(key, None)
    return restored


def tool_call_function_name(tool_call: Any) -> str:
    function = _message_value(tool_call, "function", {}) or {}
    return str(_message_value(function, "name", "") or "")


def tool_call_function_arguments(tool_call: Any) -> dict[str, Any]:
    function = _message_value(tool_call, "function", {}) or {}
    raw_arguments = _message_value(function, "arguments", {}) or {}
    if isinstance(raw_arguments, dict):
        return dict(raw_arguments)
    parsed = json.loads(raw_arguments)
    if not isinstance(parsed, dict):
        raise ValueError("tool arguments must be a JSON object")
    return parsed


def prepare_tool_call(
    tool_call: Any,
    index: int,
    *,
    argument_transform: Callable[[str, dict[str, Any], Any], dict[str, Any]] | None = None,
) -> PreparedToolCall:
    name = tool_call_function_name(tool_call)
    provider_id = tool_call_id(tool_call)
    argument_error = None
    try:
        arguments = tool_call_function_arguments(tool_call)
        if argument_transform is not None:
            arguments = argument_transform(name, arguments, tool_call)
        if not isinstance(arguments, dict):
            raise ValueError("transformed tool arguments must be a JSON object")
        arguments = dict(arguments)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        arguments = {}
        argument_error = f"Malformed arguments for {name or 'unknown tool'}: {exc}"
    return PreparedToolCall(
        index=index,
        tool_call=tool_call,
        tool_call_id=internal_tool_call_id(
            provider_id,
            index=index,
            tool_name=name,
        ),
        provider_tool_call_id=provider_id,
        tool_name=name,
        arguments=arguments,
        argument_error=argument_error,
    )


def normalize_tool_execution_output(output: Any) -> dict[str, Any]:
    if isinstance(output, dict):
        return output
    return {"action": "tool", "result": output}


def execute_ordered_tool_calls_parallel(
    tool_calls: list[Any],
    *,
    execute_tool: Callable[[PreparedToolCall], Any],
    argument_transform: Callable[[str, dict[str, Any], Any], dict[str, Any]] | None = None,
    on_started: Callable[[PreparedToolCall], Any] | None = None,
    on_finished: Callable[[ExecutedToolCall], Any] | None = None,
    max_workers: int = 10,
    timeout_seconds: float | None = None,
    timeout_resolver: Callable[[PreparedToolCall], float | None] | None = None,
    time_fn: Callable[[], float] | None = None,
) -> list[ExecutedToolCall]:
    if not tool_calls:
        return []
    clock = time_fn or time.monotonic
    prepared_calls = [
        prepare_tool_call(tool_call, index, argument_transform=argument_transform)
        for index, tool_call in enumerate(tool_calls)
    ]
    worker_count = max(1, min(int(max_workers or 1), len(prepared_calls)))
    completed_by_index: dict[int, ExecutedToolCall] = {}

    executor = concurrent.futures.ThreadPoolExecutor(max_workers=worker_count)
    futures = {}
    abandoned_running_call = False
    try:
        for prepared in prepared_calls:
            if on_started is not None:
                on_started(prepared)
            started_at = clock()
            if prepared.argument_error:
                future = executor.submit(
                    lambda item=prepared: {
                        "action": "error",
                        "result": {
                            "status": "invalid_tool_arguments",
                            "error": item.argument_error,
                        },
                    }
                )
            else:
                future = executor.submit(execute_tool, prepared)
            call_timeout = timeout_seconds
            if callable(timeout_resolver):
                call_timeout = timeout_resolver(prepared)
            try:
                call_timeout = (
                    float(call_timeout)
                    if call_timeout is not None and float(call_timeout) > 0
                    else None
                )
            except (TypeError, ValueError):
                try:
                    call_timeout = (
                        float(timeout_seconds)
                        if timeout_seconds and float(timeout_seconds) > 0
                        else None
                    )
                except (TypeError, ValueError):
                    call_timeout = None
            futures[future] = {
                "prepared": prepared,
                "started_at": started_at,
                "timeout_seconds": call_timeout,
            }

        pending = set(futures)
        while pending:
            wait_timeout = None
            if any(
                futures[future]["timeout_seconds"] is not None
                for future in pending
            ):
                remaining = [
                    max(
                        0.0,
                        futures[future]["timeout_seconds"]
                        - (clock() - futures[future]["started_at"]),
                    )
                    for future in pending
                    if futures[future]["timeout_seconds"] is not None
                ]
                wait_timeout = min(remaining) if remaining else 0.0
            done, _ = concurrent.futures.wait(
                pending,
                timeout=wait_timeout,
                return_when=concurrent.futures.FIRST_COMPLETED,
            )
            now = clock()
            expired = {
                future
                for future in pending - done
                if (
                    futures[future]["timeout_seconds"] is not None
                    and now - futures[future]["started_at"]
                    >= futures[future]["timeout_seconds"]
                )
            }

            for future in [*done, *expired]:
                if future not in pending:
                    continue
                pending.remove(future)
                meta = futures[future]
                prepared = meta["prepared"]
                duration_ms = int((now - meta["started_at"]) * 1000)
                error = None
                if future in expired:
                    future.cancel()
                    abandoned_running_call = abandoned_running_call or future.running()
                    call_timeout = meta["timeout_seconds"]
                    error = (
                        "Tool execution exceeded the "
                        f"{call_timeout:g}-second timeout."
                    )
                    output = {
                        "action": "error",
                        "result": {
                            "status": "tool_timeout",
                            "error": error,
                        },
                    }
                else:
                    try:
                        output = normalize_tool_execution_output(future.result())
                    except Exception as exc:
                        error = str(exc)
                        output = {"action": "error", "result": {"error": error}}
                executed = ExecutedToolCall(
                    prepared=prepared,
                    output=output,
                    duration_ms=duration_ms,
                    error=error,
                )
                completed_by_index[prepared.index] = executed
                if on_finished is not None:
                    on_finished(executed)
    finally:
        executor.shutdown(
            wait=not abandoned_running_call,
            cancel_futures=True,
        )

    return [completed_by_index[index] for index in range(len(prepared_calls))]


def message_has_visible_content(message: Any) -> bool:
    return bool(message_content_text(message).strip())


def should_request_finalization(message: Any) -> bool:
    return not message_tool_calls(message) and not message_has_visible_content(message)


def build_finalization_request_message(
    *,
    reason: str,
    max_tool_calls: int | None = None,
) -> dict[str, str]:
    reason_label = FINALIZATION_REASON_LABELS.get(reason, reason.replace("_", " "))
    budget_text = f" after {max_tool_calls} loop iterations" if max_tool_calls else ""
    return {
        "role": "user",
        "content": (
            "(System): Stop using tools and answer the user now. "
            f"Reason: {reason_label}{budget_text}. "
            "Summarize the work completed, key tool results, unresolved failures or missing permissions, "
            "and the best next step. If the task is incomplete, say so directly. "
            "Return user-facing text immediately."
        ),
    }


def build_tool_budget_warning_message(
    *,
    used_tool_call_iterations: int,
    max_tool_calls: int,
    remaining_tool_call_iterations: int,
    budget_increase_tool_name: str | None = None,
) -> dict[str, str]:
    parts = [
        (
            f"WARNING: You have used {used_tool_call_iterations} out of {max_tool_calls} "
            f"tool-call iterations. You have {remaining_tool_call_iterations} remaining."
        ),
        (
            "If you have enough information, stop calling tools and return the final user-facing answer now. "
            "If more tool calls are essential, use only the highest-value task-critical calls."
        ),
        "Do not spend the remaining budget on optional discovery or repeated checks.",
    ]
    if budget_increase_tool_name:
        parts.append(
            f"If the task cannot be completed within the remaining turns, call `{budget_increase_tool_name}` "
            "immediately with a concise reason."
        )
    return {"role": "system", "content": "\n".join(parts)}


def final_text_or_empty_note(message: Any) -> str:
    text = message_content_text(message).strip()
    if text:
        return text
    return (
        "*(System Note: The assistant did not produce a final response after the recovery prompt. "
        "Review the in-chat activity events above for tool results and retry with a higher tool budget if needed.)*"
    )
