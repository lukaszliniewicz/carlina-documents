"""Bounded, content-free preparation timing metrics for Document Processor."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from contextlib import nullcontext
from decimal import Decimal
from functools import wraps
from numbers import Real
from time import perf_counter, thread_time
from typing import Any, TypeVar

PREPARATION_PHASES = frozenset(
    {
        "metadata_total",
        "heading_total",
        "text_layer",
        "heading_candidates",
        "contact_sheets",
        "metadata_images",
        "heading_images",
        "page_images",
        "completion_dispatch",
    }
)

_NUMERIC_FIELDS = frozenset(
    {
        "calls",
        "wall_seconds",
        "max_wall_seconds",
        "cpu_seconds",
        "pages",
        "png_bytes",
        "encoded_bytes",
        "pool_wait_seconds",
        "worker_startup_seconds",
        "worker_render_seconds_sum",
        "worker_encode_seconds_sum",
        "peak_pool_rss_bytes",
        "incomplete_chunks",
    }
)
_INTEGER_FIELDS = frozenset(
    {
        "calls",
        "pages",
        "png_bytes",
        "encoded_bytes",
        "peak_pool_rss_bytes",
        "incomplete_chunks",
    }
)
_POOL_SUM_FIELDS = frozenset(
    {
        "pool_wait_seconds",
        "worker_startup_seconds",
        "worker_render_seconds_sum",
        "worker_encode_seconds_sum",
        "pages",
        "png_bytes",
        "encoded_bytes",
    }
)

_F = TypeVar("_F", bound=Callable[..., Any])


def _safe_number(field: str, value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (Decimal, Real)):
        return None

    if isinstance(value, int):
        if value < 0:
            return None
        return value

    if isinstance(value, Decimal):
        if not value.is_finite() or value < 0:
            return None
        integral = value == value.to_integral_value()
        if field in _INTEGER_FIELDS:
            return int(value) if integral else None
        if integral:
            return int(value)

    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    if field in _INTEGER_FIELDS:
        if not number.is_integer():
            return None
        return int(number)
    return round(number, 6)


def safe_preparation_timings(value: Any) -> dict[str, dict[str, int | float]]:
    """Return only allowlisted phases and finite, nonnegative numeric fields."""

    if not isinstance(value, Mapping):
        return {}

    safe: dict[str, dict[str, int | float]] = {}
    for stage, raw_values in value.items():
        if (
            len(safe) >= 10
            or not isinstance(stage, str)
            or stage not in PREPARATION_PHASES
            or not isinstance(raw_values, Mapping)
        ):
            continue
        values: dict[str, int | float] = {}
        for field, raw_value in raw_values.items():
            if not isinstance(field, str) or field not in _NUMERIC_FIELDS:
                continue
            number = _safe_number(field, raw_value)
            if number is not None:
                values[field] = number
        if values:
            safe[stage] = values
    return safe


def _round_total(current: Any, addition: float) -> int | float:
    base = _safe_number("wall_seconds", current)
    if base is None:
        base = 0.0
    try:
        total = float(base) + float(addition)
    except (OverflowError, TypeError, ValueError):
        return float(base) if not isinstance(base, int) else base
    if not math.isfinite(total):
        return float(base) if not isinstance(base, int) else base
    return round(total, 6)


def _record_timed_phase(instance: Any, stage: str, wall_seconds: float, cpu_seconds: float) -> None:
    if stage not in PREPARATION_PHASES:
        return

    metrics = getattr(instance, "metrics", None)
    if not isinstance(metrics, dict):
        return

    lock = getattr(instance, "metrics_lock", None)
    lock_context = lock if lock is not None else nullcontext()
    with lock_context:
        metrics = getattr(instance, "metrics", None)
        if not isinstance(metrics, dict):
            return

        timings = safe_preparation_timings(metrics.get("preparation_timings"))
        phase = dict(timings.get(stage, {}))
        safe_wall = _safe_number("wall_seconds", wall_seconds)
        safe_cpu = _safe_number("cpu_seconds", cpu_seconds)
        wall = round(float(safe_wall or 0.0), 6)
        cpu = round(float(safe_cpu or 0.0), 6)
        phase["calls"] = int(phase.get("calls", 0)) + 1
        phase["wall_seconds"] = _round_total(phase.get("wall_seconds"), wall)
        phase["max_wall_seconds"] = round(
            max(float(phase.get("max_wall_seconds", 0.0)), wall), 6
        )
        phase["cpu_seconds"] = _round_total(phase.get("cpu_seconds"), cpu)
        timings[stage] = phase
        metrics["preparation_timings"] = timings


def timed_phase(stage: str) -> Callable[[_F], _F]:
    """Measure wall and current-thread CPU time for one allowlisted phase."""

    def decorate(method: _F) -> _F:
        @wraps(method)
        def wrapped(instance: Any, *args: Any, **kwargs: Any) -> Any:
            wall_start = perf_counter()
            cpu_start = thread_time()
            try:
                return method(instance, *args, **kwargs)
            finally:
                try:
                    _record_timed_phase(
                        instance,
                        stage,
                        perf_counter() - wall_start,
                        thread_time() - cpu_start,
                    )
                except BaseException:  # noqa: BLE001, S110 - keep timing failures from masking parser outcomes.
                    # Instrumentation must never replace the method's result or
                    # exception, including when an optional metrics lock fails.
                    pass

        return wrapped  # type: ignore[return-value]

    return decorate


def _merge_numeric(phase: dict[str, int | float], field: str, raw_value: Any) -> None:
    value = _safe_number(field, raw_value)
    if value is None:
        return
    if field in _INTEGER_FIELDS:
        phase[field] = int(phase.get(field, 0)) + int(value)
    else:
        phase[field] = _round_total(phase.get(field), float(value))


def accumulate_render_metrics(
    metrics: Any, stage: str, values: Any
) -> None:
    """Merge one bounded PDF render-pool callback row into preparation metrics."""

    if (
        not isinstance(metrics, dict)
        or not isinstance(stage, str)
        or stage not in PREPARATION_PHASES
        or not isinstance(values, Mapping)
    ):
        return

    timings = safe_preparation_timings(metrics.get("preparation_timings"))
    phase = dict(timings.get(stage, {}))
    phase["calls"] = int(phase.get("calls", 0)) + 1

    wall = _safe_number("wall_seconds", values.get("preparation_wall_seconds"))
    if wall is not None:
        wall_value = float(wall)
        phase["wall_seconds"] = _round_total(phase.get("wall_seconds"), wall_value)
        phase["max_wall_seconds"] = round(
            max(float(phase.get("max_wall_seconds", 0.0)), wall_value), 6
        )

    for field in _POOL_SUM_FIELDS:
        if field in values:
            _merge_numeric(phase, field, values[field])

    if "peak_pool_rss_bytes" in values:
        peak = _safe_number("peak_pool_rss_bytes", values["peak_pool_rss_bytes"])
        if peak is not None:
            phase["peak_pool_rss_bytes"] = max(
                int(phase.get("peak_pool_rss_bytes", 0)), int(peak)
            )

    if "incomplete" in values:
        incomplete = values["incomplete"]
        if isinstance(incomplete, bool):
            incomplete = int(incomplete)
        _merge_numeric(phase, "incomplete_chunks", incomplete)

    timings[stage] = phase
    metrics["preparation_timings"] = timings


__all__ = [
    "PREPARATION_PHASES",
    "accumulate_render_metrics",
    "safe_preparation_timings",
    "timed_phase",
]
