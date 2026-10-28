"""Resident CPU SaT worker under Carlina's existing managed process lifecycle."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol, cast

from carlina.segmentation.native_engine import NativeSatEngine
from carlina.segmentation.native_protocol import (
    NATIVE_TASK_NAME,
    STATUS_TTL_SECONDS,
    JsonValue,
    NativeSegmentationError,
    parse_native_request,
)
from iarf_assistant.core.app_paths import resolve_app_paths
from iarf_assistant.core.worker_runtime import build_worker_node, run_worker_node

type Handler = Callable[[dict[str, JsonValue], object, str], dict[str, JsonValue]]


class _WorkerRegistration(Protocol):
    def handler(self, task_name: str) -> Callable[[Handler], Handler]: ...


_engine: NativeSatEngine | None = None
_engine_root: Path | None = None
_load_lock = threading.Lock()


def execute_native_request(
    payload: object, *, cache_root: Path
) -> dict[str, JsonValue]:
    """Validate queued work before loading a model or running inference."""
    global _engine, _engine_root

    request = parse_native_request(payload, now=time.time())
    try:
        with _load_lock:
            resolved_root = cache_root.resolve()
            if _engine is None:
                _engine = NativeSatEngine.load(cache_root)
                _engine_root = resolved_root
            elif _engine_root != resolved_root:
                raise NativeSegmentationError("segmentation_runtime_unavailable")
            engine = _engine
        return engine.execute(request)
    except NativeSegmentationError as error:
        if request.operation == "health":
            return {
                "status": "not_ready",
                "backend": "native",
                "error_code": error.code,
                "preparation_required": error.code == "segmentation_model_missing",
            }
        raise


def handle_segmentation(
    payload: dict[str, JsonValue], _queue: object, _job_id: str
) -> dict[str, JsonValue]:
    root = resolve_app_paths().cache_dir / "addons" / "sentence_segmenter" / "sat"
    try:
        return execute_native_request(payload, cache_root=root)
    except NativeSegmentationError:
        raise
    except Exception:  # WorkerNode stores exceptions; keep them content-free.
        raise NativeSegmentationError("segmentation_worker_failed") from None


def main() -> None:
    worker = build_worker_node(
        "sentence_segmenter",
        idle_timeout=0,
        mirror_runtime=False,
        status_ttl_seconds=STATUS_TTL_SECONDS,
    )
    cast(_WorkerRegistration, worker).handler(NATIVE_TASK_NAME)(handle_segmentation)
    run_worker_node(worker)


if __name__ == "__main__":
    main()
