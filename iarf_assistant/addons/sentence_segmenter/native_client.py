"""Host-side adapter for bounded, temporary native segmentation requests."""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from contextlib import suppress
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

from carlina.segmentation.native_protocol import (
    MAX_QUEUE_ITEMS,
    MAX_REQUEST_SECONDS,
    NATIVE_TASK_NAME,
    STATUS_TTL_SECONDS,
    JsonValue,
    NativeSegmentationError,
    native_request_payload,
)
from iarf_assistant.core.safe_sqlite_store import QueueCapacityError
from iarf_assistant.core.task_queue import TaskQueue


class _RequestQueue(Protocol):
    def enqueue_once(
        self,
        task_name: str,
        payload: dict[str, JsonValue],
        *,
        job_id: str,
        max_queue_items: int,
    ) -> str: ...

    def get_status(self, job_id: str) -> object: ...

    def request_cancel(self, job_id: str, *, reason: str) -> object: ...

    def close(self) -> None: ...


class _QueueFactory(Protocol):
    def __call__(
        self,
        instance_path: str,
        addon_id: str,
        *,
        mirror_runtime: bool,
        status_ttl_seconds: float,
    ) -> _RequestQueue: ...


@dataclass(frozen=True, slots=True)
class NativeResponse:
    """Minimal response surface shared by the existing add-on capability client."""

    payload: dict[str, JsonValue]
    status_code: int = 200

    @property
    def headers(self) -> dict[str, str]:
        return {"Retry-After": "2"} if self.status_code == 429 else {}

    def json(self) -> dict[str, JsonValue]:
        return deepcopy(self.payload)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            code = self.payload.get("error")
            raise NativeSegmentationError(
                code if isinstance(code, str) else "segmentation_worker_failed"
            )

    def close(self) -> None:
        """There is no network connection to release."""


def _failure(code: str, status_code: int = 503) -> NativeResponse:
    error = NativeSegmentationError(code)
    return NativeResponse({"error": error.code}, status_code)


def _request_expired(*, started: float, deadline_at: float) -> bool:
    return (
        time.monotonic() - started >= MAX_REQUEST_SECONDS or time.time() >= deadline_at
    )


class NativeQueueClient:
    """Use one caller-keyed delivery and never retain text in RuntimeJob."""

    def __init__(self, instance_path: Path, *, ensure_worker: Callable[[], None]):
        self._instance_path = instance_path
        self._ensure_worker = ensure_worker

    def request(
        self, operation: str, parameters: dict[str, JsonValue]
    ) -> NativeResponse:
        started = time.monotonic()
        payload = native_request_payload(operation, parameters, now=time.time())
        deadline_at = cast(float, payload["deadline_at"])
        job_id = f"native-sat-{uuid.uuid4()}"
        try:
            self._ensure_worker()
        except Exception:  # Host failures must not disclose source or credentials.
            return _failure("segmentation_runtime_unavailable")

        try:
            queue = cast(_QueueFactory, TaskQueue)(
                str(self._instance_path),
                "sentence_segmenter",
                mirror_runtime=False,
                status_ttl_seconds=STATUS_TTL_SECONDS,
            )
        except Exception:  # Normalize internal storage failures.
            return _failure("segmentation_runtime_unavailable")
        try:
            if _request_expired(started=started, deadline_at=deadline_at):
                return _failure("segmentation_request_expired", 504)
            try:
                queue.enqueue_once(
                    NATIVE_TASK_NAME,
                    payload,
                    job_id=job_id,
                    max_queue_items=MAX_QUEUE_ITEMS,
                )
            except QueueCapacityError:
                return _failure("segmentation_capacity_exhausted", 429)

            while not _request_expired(started=started, deadline_at=deadline_at):
                raw_status = queue.get_status(job_id)
                # A contended SQLite read can finish after the caller's budget.
                if _request_expired(started=started, deadline_at=deadline_at):
                    break
                if isinstance(raw_status, dict):
                    status = cast(dict[str, object], raw_status)
                    state = status.get("status")
                    if state == "completed":
                        result = status.get("result")
                        if isinstance(result, dict):
                            return NativeResponse(cast(dict[str, JsonValue], result))
                        return _failure("segmentation_response_invalid", 502)
                    if state == "failed":
                        raw_error = status.get("error")
                        code = NativeSegmentationError(
                            raw_error
                            if isinstance(raw_error, str)
                            else "segmentation_worker_failed"
                        ).code
                        error_status = {
                            "segmentation_identity_changed": 409,
                            "segmentation_request_invalid": 400,
                            "segmentation_request_expired": 504,
                        }.get(code, 503)
                        return _failure(code, error_status)
                    if state == "cancelled":
                        return _failure("segmentation_request_cancelled", 409)
                time.sleep(0.05)

            with suppress(Exception):
                queue.request_cancel(
                    job_id, reason="Native segmentation request expired."
                )
            return _failure("segmentation_request_expired", 504)
        except Exception:  # Keep internal errors out of the legacy logger.
            return _failure("segmentation_worker_failed")
        finally:
            with suppress(Exception):
                queue.close()
