from __future__ import annotations

import copy
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any


def _utc_timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class StartupReadiness:
    """Thread-safe startup phase and timing state exposed to the UI."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._started_monotonic = time.monotonic()
        self._started_at = _utc_timestamp()
        self._completed_at: str | None = None
        self._state = "starting"
        self._phase = "application"
        self._timings_ms: dict[str, float] = {}
        self._details: dict[str, dict[str, Any]] = {}
        self._errors: list[dict[str, str]] = []

    @contextmanager
    def phase(self, name: str) -> Iterator[None]:
        started_at = time.perf_counter()
        with self._lock:
            self._phase = name
            if self._state in {"ready", "degraded"}:
                self._state = "starting"
                self._completed_at = None
        try:
            yield
        finally:
            duration_ms = (time.perf_counter() - started_at) * 1000
            with self._lock:
                self._timings_ms[name] = round(duration_ms, 3)

    def add_error(self, phase: str, error: BaseException) -> None:
        with self._lock:
            self._errors.append(
                {
                    "phase": phase,
                    "message": str(error) or error.__class__.__name__,
                }
            )

    def set_detail(self, name: str, detail: Mapping[str, Any]) -> None:
        """Publish structured diagnostics for one startup subsystem."""

        with self._lock:
            self._details[str(name)] = copy.deepcopy(dict(detail))

    def mark_ready(self) -> None:
        with self._lock:
            self._phase = "ready"
            self._state = "degraded" if self._errors else "ready"
            self._completed_at = _utc_timestamp()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "state": self._state,
                "phase": self._phase,
                "started_at": self._started_at,
                "completed_at": self._completed_at,
                "elapsed_ms": round(
                    (time.monotonic() - self._started_monotonic) * 1000,
                    3,
                ),
                "timings_ms": dict(self._timings_ms),
                "details": copy.deepcopy(self._details),
                "errors": list(self._errors),
            }


def get_startup_readiness(app: Any) -> StartupReadiness:
    extensions = getattr(app, "extensions", None)
    if extensions is None:
        app.extensions = {}
        extensions = app.extensions
    readiness = extensions.get("iarf_startup_readiness")
    if not isinstance(readiness, StartupReadiness):
        readiness = StartupReadiness()
        extensions["iarf_startup_readiness"] = readiness
    return readiness


def startup_readiness_snapshot(app: Any) -> dict[str, Any]:
    if app is None:
        return {
            "state": "unknown",
            "phase": "unknown",
            "started_at": None,
            "completed_at": None,
            "elapsed_ms": 0.0,
            "timings_ms": {},
            "details": {},
            "errors": [],
        }
    return get_startup_readiness(app).snapshot()


__all__ = [
    "StartupReadiness",
    "get_startup_readiness",
    "startup_readiness_snapshot",
]
