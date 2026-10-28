"""Low-overhead SQL statement and latency instrumentation."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from collections import deque
from dataclasses import asdict, dataclass
import math
from time import perf_counter
import threading
from typing import Any, Iterator

from flask import Flask, current_app, g, request
from sqlalchemy import event
from sqlalchemy.engine import Engine


_collectors: ContextVar[tuple["QueryMetrics", ...]] = ContextVar(
    "carlina_query_metrics_collectors",
    default=(),
)
_registration_lock = threading.Lock()
_sample_lock = threading.Lock()
_request_samples: deque[dict[str, int | float]] = deque(maxlen=2_048)
_observed_request_count = 0
_observed_lock_error_count = 0
_observed_lock_wait_ms = 0.0
_WRITE_PREFIXES = {
    "ALTER",
    "CREATE",
    "DELETE",
    "DROP",
    "INSERT",
    "PRAGMA",
    "REINDEX",
    "REPLACE",
    "UPDATE",
    "VACUUM",
}


@dataclass(slots=True)
class QueryMetrics:
    statement_count: int = 0
    write_statement_count: int = 0
    total_duration_ms: float = 0.0
    write_duration_ms: float = 0.0
    slow_write_count: int = 0
    sqlite_lock_error_count: int = 0
    sqlite_lock_wait_ms: float = 0.0

    def to_dict(self) -> dict[str, int | float]:
        payload = asdict(self)
        return {
            key: round(value, 3) if isinstance(value, float) else value
            for key, value in payload.items()
        }


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * percentile) - 1))
    return round(float(ordered[index]), 3)


def _record_request_sample(metrics: QueryMetrics) -> None:
    global _observed_lock_error_count
    global _observed_lock_wait_ms
    global _observed_request_count
    payload = metrics.to_dict()
    with _sample_lock:
        _request_samples.append(payload)
        _observed_request_count += 1
        _observed_lock_error_count += metrics.sqlite_lock_error_count
        _observed_lock_wait_ms += metrics.sqlite_lock_wait_ms


def query_metrics_snapshot() -> dict[str, int | float]:
    """Return bounded process-local p50/p95 database request metrics."""

    with _sample_lock:
        samples = list(_request_samples)
        request_count = _observed_request_count
        lock_errors = _observed_lock_error_count
        lock_wait_ms = _observed_lock_wait_ms
    durations = [float(item["total_duration_ms"]) for item in samples]
    statements = [float(item["statement_count"]) for item in samples]
    writes = [float(item["write_duration_ms"]) for item in samples]
    return {
        "requests_observed": request_count,
        "sample_size": len(samples),
        "db_duration_p50_ms": _percentile(durations, 0.50),
        "db_duration_p95_ms": _percentile(durations, 0.95),
        "db_duration_max_ms": round(max(durations, default=0.0), 3),
        "statement_count_p95": _percentile(statements, 0.95),
        "write_duration_p95_ms": _percentile(writes, 0.95),
        "sqlite_lock_error_count": lock_errors,
        "sqlite_lock_wait_estimate_ms": round(lock_wait_ms, 3),
    }


def _is_write(statement: str) -> bool:
    prefix = str(statement or "").lstrip().split(None, 1)
    return bool(prefix and prefix[0].upper() in _WRITE_PREFIXES)


def _record(
    context: Any,
    statement: str,
    *,
    error: BaseException | None = None,
) -> None:
    if getattr(context, "_carlina_metrics_recorded", False):
        return
    started = getattr(context, "_carlina_metrics_started", None)
    if started is None:
        return
    context._carlina_metrics_recorded = True
    elapsed_ms = max(0.0, (perf_counter() - started) * 1_000)
    is_write = _is_write(statement)
    lock_error = bool(
        error
        and any(
            marker in str(error).casefold()
            for marker in ("database is locked", "database is busy", "sqlite_busy")
        )
    )
    for collector in _collectors.get():
        collector.statement_count += 1
        collector.total_duration_ms += elapsed_ms
        if is_write:
            collector.write_statement_count += 1
            collector.write_duration_ms += elapsed_ms
            if elapsed_ms >= 25:
                collector.slow_write_count += 1
                collector.sqlite_lock_wait_ms += elapsed_ms
        if lock_error:
            collector.sqlite_lock_error_count += 1
            if not is_write or elapsed_ms < 25:
                collector.sqlite_lock_wait_ms += elapsed_ms


def _before_cursor_execute(
    _conn: Any,
    _cursor: Any,
    _statement: str,
    _parameters: Any,
    context: Any,
    _executemany: bool,
) -> None:
    context._carlina_metrics_started = perf_counter()
    context._carlina_metrics_recorded = False


def _after_cursor_execute(
    _conn: Any,
    _cursor: Any,
    statement: str,
    _parameters: Any,
    context: Any,
    _executemany: bool,
) -> None:
    _record(context, statement)


def _handle_error(exception_context: Any) -> None:
    context = exception_context.execution_context
    if context is None:
        return
    _record(
        context,
        getattr(context, "statement", ""),
        error=exception_context.original_exception,
    )


def configure_query_metrics_engine(engine: Engine) -> None:
    """Attach instrumentation to one Carlina-owned SQLAlchemy engine."""

    with _registration_lock:
        listeners = (
            ("before_cursor_execute", _before_cursor_execute),
            ("after_cursor_execute", _after_cursor_execute),
            ("handle_error", _handle_error),
        )
        for event_name, listener in listeners:
            if not event.contains(engine, event_name, listener):
                event.listen(engine, event_name, listener)


@contextmanager
def measure_queries() -> Iterator[QueryMetrics]:
    """Measure SQL issued in the current context, including nested services."""

    collector = QueryMetrics()
    token = _collectors.set((*_collectors.get(), collector))
    try:
        yield collector
    finally:
        _collectors.reset(token)


def configure_query_metrics(app: Flask) -> None:
    """Attach per-request metrics, response timing, and slow-query logging."""

    from iarf_assistant.extensions import db

    with app.app_context():
        configure_query_metrics_engine(db.engine)
    app.config.setdefault("SQL_QUERY_WARN_MS", 250)
    app.config.setdefault("EXPOSE_SQL_QUERY_METRICS", bool(app.testing or app.debug))

    @app.before_request
    def _begin_request_query_metrics() -> None:
        collector = QueryMetrics()
        g.carlina_query_metrics = collector
        g.carlina_query_metrics_token = _collectors.set((*_collectors.get(), collector))

    @app.after_request
    def _publish_request_query_metrics(response):
        collector = getattr(g, "carlina_query_metrics", None)
        if not isinstance(collector, QueryMetrics):
            return response
        timing = f'db;dur={collector.total_duration_ms:.3f};desc="SQL"'
        existing = response.headers.get("Server-Timing")
        response.headers["Server-Timing"] = (
            f"{existing}, {timing}" if existing else timing
        )
        if current_app.config.get("EXPOSE_SQL_QUERY_METRICS"):
            response.headers["X-SQL-Query-Count"] = str(collector.statement_count)
            response.headers["X-SQL-Write-Count"] = str(collector.write_statement_count)
        if collector.total_duration_ms >= float(
            current_app.config["SQL_QUERY_WARN_MS"]
        ):
            current_app.logger.warning(
                "Slow SQL request metrics=%s method=%s path=%s",
                collector.to_dict(),
                request.method,
                request.path,
            )
        _record_request_sample(collector)
        return response

    @app.teardown_request
    def _end_request_query_metrics(_error: BaseException | None = None) -> None:
        token = getattr(g, "carlina_query_metrics_token", None)
        if token is not None:
            _collectors.reset(token)
            g.carlina_query_metrics_token = None


__all__ = [
    "QueryMetrics",
    "configure_query_metrics",
    "configure_query_metrics_engine",
    "measure_queries",
    "query_metrics_snapshot",
]
