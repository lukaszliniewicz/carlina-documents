"""Bounded, content-free RPC contract for the native SaT worker."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Literal, cast

type JsonValue = (
    str | int | float | bool | list[JsonValue] | dict[str, JsonValue] | None
)
type NativeOperation = Literal[
    "health", "split", "split_batch", "paragraphs", "boundary-probabilities"
]

NATIVE_TASK_NAME = "native_segmentation"
MAX_REQUEST_SECONDS = 300
MAX_QUEUE_ITEMS = 5
STATUS_TTL_SECONDS = 600
MAX_TEXT_CHARS = 1_000_000
MAX_BATCH_TEXTS = 32
MAX_BATCH_TOTAL_BYTES = 1_000_000

_OPERATIONS = frozenset(
    {"health", "split", "split_batch", "paragraphs", "boundary-probabilities"}
)
_ERROR_CODES = frozenset(
    {
        "segmentation_request_invalid",
        "segmentation_request_expired",
        "segmentation_identity_changed",
        "segmentation_model_missing",
        "segmentation_model_invalid",
        "segmentation_runtime_unavailable",
        "segmentation_inference_failed",
        "segmentation_response_invalid",
        "segmentation_capacity_exhausted",
        "segmentation_request_cancelled",
        "segmentation_worker_failed",
    }
)


class NativeSegmentationError(RuntimeError):
    """A fixed machine-readable failure that never contains input text."""

    def __init__(self, code: str):
        self.code = code if code in _ERROR_CODES else "segmentation_worker_failed"
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class NativeRequest:
    operation: NativeOperation
    # UTC Unix timestamp shared across the caller and worker processes.
    deadline_at: float
    texts: tuple[str, ...] = ()
    stride: int = 128
    block_size: int = 256
    expected_identity: str | None = None
    threshold: float | None = None
    paragraph_threshold: float | None = None


def _invalid() -> NativeSegmentationError:
    return NativeSegmentationError("segmentation_request_invalid")


def _mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise _invalid()
    raw = cast(dict[object, object], value)
    if any(not isinstance(key, str) for key in raw):
        raise _invalid()
    return cast(dict[str, object], raw)


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise _invalid()
    try:
        number = float(value)
    except OverflowError:
        raise _invalid() from None
    if not math.isfinite(number):
        raise _invalid()
    return number


def _chunk_size(value: object) -> int:
    if type(value) is not int or not 1 <= value <= 512:
        raise _invalid()
    return value


def _threshold(value: object) -> float | None:
    if value is None:
        return None
    result = _number(value)
    if not 0 <= result <= 1:
        raise _invalid()
    return result


def _text(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_TEXT_CHARS:
        raise _invalid()
    # Reject lone surrogates before a caller or worker serializes a request.
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise _invalid() from None
    return value


def check_deadline(deadline_at: float, *, now: float) -> None:
    if deadline_at <= now:
        raise NativeSegmentationError("segmentation_request_expired")


def parse_native_request(value: object, *, now: float) -> NativeRequest:
    """Validate again in the worker, independently of the calling adapter."""
    request = _mapping(value)
    if set(request) != {"schema_version", "operation", "deadline_at", "parameters"}:
        raise _invalid()
    if type(request["schema_version"]) is not int or request["schema_version"] != 1:
        raise _invalid()
    operation = request["operation"]
    if not isinstance(operation, str) or operation not in _OPERATIONS:
        raise _invalid()
    deadline = _number(request["deadline_at"])
    clock = _number(now)
    if deadline > clock + MAX_REQUEST_SECONDS:
        raise _invalid()
    check_deadline(deadline, now=clock)
    parameters = _mapping(request["parameters"])
    if operation == "health":
        if parameters:
            raise _invalid()
        return NativeRequest("health", deadline)

    allowed = {"stride", "block_size", "weighting", "treat_newline_as_space"}
    allowed.add("texts" if operation == "split_batch" else "text")
    if operation in {"split", "split_batch"}:
        allowed.add("expected_identity")
    if operation in {"paragraphs", "boundary-probabilities"}:
        allowed.add("threshold")
    if operation == "paragraphs":
        allowed.add("paragraph_threshold")
    if set(parameters) - allowed:
        raise _invalid()
    if parameters.get("weighting", "hat") != "hat":
        raise _invalid()
    if parameters.get("treat_newline_as_space", True) is not True:
        raise _invalid()
    stride = _chunk_size(parameters.get("stride", 128))
    block_size = _chunk_size(parameters.get("block_size", 256))
    identity = parameters.get("expected_identity")
    if identity is not None and (
        not isinstance(identity, str) or not re.fullmatch(r"[a-f0-9]{64}", identity)
    ):
        raise _invalid()
    if operation == "split_batch":
        raw_texts = parameters.get("texts")
        if not isinstance(raw_texts, list):
            raise _invalid()
        batch = cast(list[object], raw_texts)
        if not 1 <= len(batch) <= MAX_BATCH_TEXTS:
            raise _invalid()
        texts = tuple(_text(text) for text in batch)
        if sum(len(text.encode("utf-8")) for text in texts) > MAX_BATCH_TOTAL_BYTES:
            raise _invalid()
    else:
        texts = (_text(parameters.get("text")),)
    return NativeRequest(
        cast(NativeOperation, operation),
        deadline,
        texts,
        stride,
        block_size,
        identity,
        _threshold(parameters.get("threshold")),
        _threshold(parameters.get("paragraph_threshold")),
    )


def native_request_payload(
    operation: str, parameters: dict[str, JsonValue], *, now: float
) -> dict[str, JsonValue]:
    """Build a validated envelope; transport retries reuse these exact bytes."""
    payload: dict[str, JsonValue] = {
        "schema_version": 1,
        "operation": operation,
        "deadline_at": now + MAX_REQUEST_SECONDS,
        "parameters": parameters,
    }
    parse_native_request(payload, now=now)
    return payload
