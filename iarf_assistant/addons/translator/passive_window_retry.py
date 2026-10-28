"""Content-free dispatch metadata for passive translation-window retries."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import cast

_MAX_IDENTIFIER_LENGTH = 128
_MAX_FINDING_CODES = 20
_SAFE_CODE = re.compile(rf"[a-z0-9_-]{{1,{_MAX_IDENTIFIER_LENGTH}}}")
_RETRY_STATES = frozenset({"prepared", "normalization_failed", "provider_failed"})
_RETRY_VERSION_KEY = "passive_retry_dispatch_version"
_INVALID_EVENTS = "retry event transcript is invalid"
_DUPLICATE_REQUEST = "retry request event is duplicated"
_MISPLACED_VERSION = "retry version marker is misplaced"
_MISSING_REQUEST = "retry request event is missing"
_UNSUPPORTED_VERSION = "retry dispatch version is unsupported"
_INVALID_IDENTIFIERS = "retry attempt identifiers are invalid"
_INVALID_STATE = "retry predecessor state is invalid"
_MISSING_FEEDBACK = "retry normalization feedback is missing"


def retry_dispatch_version(
    events: Iterable[tuple[object, object]],
) -> int | None:
    """Return the authenticated request marker, or ``None`` for old retries."""

    request_payload: Mapping[str, object] | None = None
    for event_type, payload in events:
        if type(event_type) is not str or not isinstance(payload, Mapping):
            raise ValueError(_INVALID_EVENTS)
        event_payload = cast(Mapping[str, object], payload)
        has_marker = _RETRY_VERSION_KEY in event_payload
        if event_type == "request_prepared":
            if request_payload is not None:
                raise ValueError(_DUPLICATE_REQUEST)
            request_payload = event_payload
        elif has_marker:
            raise ValueError(_MISPLACED_VERSION)

    if request_payload is None:
        raise ValueError(_MISSING_REQUEST)
    if _RETRY_VERSION_KEY not in request_payload:
        return None

    version = request_payload[_RETRY_VERSION_KEY]
    if type(version) is not int or version != 1:
        raise ValueError(_UNSUPPORTED_VERSION)
    return version


def _valid_identifier(value: object) -> bool:
    return (
        type(value) is str
        and bool(value)
        and value == value.strip()
        and len(value) <= _MAX_IDENTIFIER_LENGTH
    )


def _normalization_codes(
    events: Iterable[tuple[str, Mapping[str, object]]],
) -> list[str]:
    latest_payload: Mapping[str, object] | None = None
    for event_type, payload in events:
        if event_type == "normalization_failed":
            latest_payload = payload
    if latest_payload is None:
        raise ValueError(_MISSING_FEEDBACK)

    candidates: list[object] = [latest_payload.get("code")]
    finding_codes = latest_payload.get("finding_codes")
    if isinstance(finding_codes, list):
        bounded_findings = cast(list[object], finding_codes[:_MAX_FINDING_CODES])
        candidates.extend(code for code in bounded_findings if type(code) is str)
    validation_codes: list[str] = []
    for candidate in candidates:
        if (
            type(candidate) is str
            and _SAFE_CODE.fullmatch(candidate) is not None
            and candidate not in validation_codes
        ):
            validation_codes.append(candidate)
    return validation_codes


def build_retry_dispatch(
    *,
    attempt_id: str,
    retry_of_attempt_id: str,
    dispatch_state: str,
    events: Iterable[tuple[str, Mapping[str, object]]],
) -> dict[str, object]:
    """Return bounded retry identity and safe validation codes only."""

    if not _valid_identifier(attempt_id) or not _valid_identifier(retry_of_attempt_id):
        raise ValueError(_INVALID_IDENTIFIERS)
    if type(dispatch_state) is not str or dispatch_state not in _RETRY_STATES:
        raise ValueError(_INVALID_STATE)

    return {
        "contract_version": 1,
        "attempt_id": attempt_id,
        "retry_of_attempt_id": retry_of_attempt_id,
        "dispatch_state": dispatch_state,
        "validation_codes": _normalization_codes(events)
        if dispatch_state == "normalization_failed"
        else [],
    }
