import json
import logging
from collections.abc import Mapping
from typing import Any

from .core.litellm_runtime import (
    litellm,
    load_litellm,
)


llm_logger = logging.getLogger("llm_api_logger")
llm_logger.setLevel(logging.INFO)


_USAGE_FIELDS = (
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)
_USAGE_DETAIL_FIELDS = (
    "audio_tokens",
    "cached_tokens",
    "reasoning_tokens",
    "text_tokens",
    "image_tokens",
    "video_tokens",
    "accepted_prediction_tokens",
    "rejected_prediction_tokens",
)


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _safe_nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except TypeError, ValueError:
        return None
    return parsed if parsed >= 0 else None


def _safe_usage(usage: Any) -> dict[str, Any] | None:
    """Keep numeric accounting only; provider payloads can contain source text."""

    if usage is None:
        return None
    result: dict[str, Any] = {}
    for name in _USAGE_FIELDS:
        parsed = _safe_nonnegative_int(_field(usage, name))
        if parsed is not None:
            result[name] = parsed
    for detail_name in ("prompt_tokens_details", "completion_tokens_details"):
        detail = _field(usage, detail_name)
        safe_detail = {}
        for name in _USAGE_DETAIL_FIELDS:
            parsed = _safe_nonnegative_int(_field(detail, name))
            if parsed is not None:
                safe_detail[name] = parsed
        if safe_detail:
            result[detail_name] = safe_detail
    return result or None


def _safe_finish_reasons(response: Any) -> list[str]:
    reasons: list[str] = []
    choices = _field(response, "choices", [])
    if not isinstance(choices, (list, tuple)):
        return reasons
    for choice in choices[:32]:
        reason = str(_field(choice, "finish_reason", "") or "").strip().casefold()
        reason = "".join(
            character
            for character in reason
            if character.isalnum() or character in "_-"
        )[:64]
        if reason and reason not in reasons:
            reasons.append(reason)
    return reasons


def _request_shape(kwargs: Any) -> dict[str, Any]:
    values = kwargs if isinstance(kwargs, Mapping) else {}
    messages = values.get("messages")
    tools = values.get("tools")
    response_format = values.get("response_format")
    if isinstance(response_format, Mapping):
        response_format_type = str(response_format.get("type") or "")[:64] or None
    else:
        response_format_type = None
    return {
        "message_count": len(messages) if isinstance(messages, (list, tuple)) else 0,
        "tool_count": len(tools) if isinstance(tools, (list, tuple)) else 0,
        "response_format_type": response_format_type,
        "stream": values.get("stream") is True,
    }


def _safe_status_code(exception: Any) -> int | None:
    candidates = (
        getattr(exception, "status_code", None),
        getattr(exception, "http_status", None),
        getattr(getattr(exception, "response", None), "status_code", None),
    )
    for candidate in candidates:
        parsed = _safe_nonnegative_int(candidate)
        if parsed is not None and 100 <= parsed <= 599:
            return parsed
    return None


def log_successful_call(kwargs, response, start_time, end_time):
    """Log source-free provider telemetry for a successful model call."""
    log_data = {
        "status": "success",
        "model": kwargs.get("model"),
        "start_time": start_time.isoformat(),
        "duration_s": (end_time - start_time).total_seconds(),
        "request": _request_shape(kwargs),
        "response": {
            "id": str(_field(response, "id", "") or "")[:128] or None,
            "finish_reasons": _safe_finish_reasons(response),
            "usage": _safe_usage(_field(response, "usage")),
            "system_fingerprint": str(_field(response, "system_fingerprint", "") or "")[
                :128
            ]
            or None,
        },
    }
    llm_logger.info(json.dumps(log_data, indent=2, default=str))


def log_failed_call(kwargs, exception, start_time, end_time):
    """Log source-free provider telemetry for a failed model call."""
    actual_exception = exception
    if actual_exception is None and isinstance(kwargs, Mapping):
        candidate = kwargs.get("exception") or kwargs.get("error")
        actual_exception = candidate if isinstance(candidate, BaseException) else None
    log_data = {
        "status": "failure",
        "model": kwargs.get("model"),
        "start_time": start_time.isoformat(),
        "duration_s": (end_time - start_time).total_seconds(),
        "request": _request_shape(kwargs),
        "error": {
            "type": (
                type(actual_exception).__name__
                if actual_exception is not None
                else "UnknownProviderError"
            ),
            "status_code": _safe_status_code(actual_exception),
        },
    }
    llm_logger.error(json.dumps(log_data, indent=2, default=str))


def _has_callback(callbacks, callback):
    for existing in callbacks:
        if existing is callback:
            return True
        if (
            getattr(existing, "__module__", None) == callback.__module__
            and getattr(existing, "__name__", None) == callback.__name__
        ):
            return True
    return False


def _append_callback_once(callbacks, callback):
    if not _has_callback(callbacks, callback):
        callbacks.append(callback)


def configure_litellm_logging(litellm_module=None):
    """Registers LiteLLM callbacks once per process."""
    module = litellm_module or load_litellm()
    _append_callback_once(module.success_callback, log_successful_call)
    _append_callback_once(module.failure_callback, log_failed_call)
