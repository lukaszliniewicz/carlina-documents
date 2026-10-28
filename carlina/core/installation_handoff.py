"""Private-file handoff for one terminal installation lifecycle event."""

from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path
from typing import NoReturn, cast

from carlina.core.installation_lifecycle import (
    InstallationErrorCode,
    InstallationEvent,
    InstallationOperation,
    InstallationPhase,
    InstallationState,
)
from carlina.runtime_profiles import RuntimeProfile

_DIAGNOSTIC = "Invalid installation handoff event."
HANDOFF_PROTOCOL_VERSION = 1
_MAX_EVENT_BYTES = 4096
_OPERATION_ID = re.compile(r"[0-9a-f]{32}")
_EVENT_FIELDS = frozenset(
    {
        "format",
        "schema_version",
        "operation_id",
        "sequence",
        "operation",
        "application_profile",
        "phase",
        "state",
        "bytes_completed",
        "bytes_total",
        "error_code",
        "retryable",
        "cancellation_safe",
    }
)


class InstallationHandoffError(ValueError):
    """A terminal event cannot safely be handed to the parent installer."""

    def __init__(self) -> None:
        super().__init__(_DIAGNOSTIC)


def _invalid() -> InstallationHandoffError:
    return InstallationHandoffError()


def _strict_enum[E: str](enum_type: type[E], value: object) -> E:
    if type(value) is not str:
        raise _invalid()
    try:
        return enum_type(value)
    except ValueError:
        raise _invalid() from None


def _parse_event(payload: object) -> InstallationEvent:
    if type(payload) is not dict:
        raise _invalid()
    record = cast(dict[str, object], payload)
    if frozenset(record) != _EVENT_FIELDS:
        raise _invalid()
    if (
        type(record["format"]) is not str
        or record["format"] != "carlina.installation-event"
    ):
        raise _invalid()
    if type(record["schema_version"]) is not int or record["schema_version"] != 1:
        raise _invalid()

    operation_id = record["operation_id"]
    if type(operation_id) is not str or _OPERATION_ID.fullmatch(operation_id) is None:
        raise _invalid()
    sequence = record["sequence"]
    if type(sequence) is not int or sequence <= 0:
        raise _invalid()

    operation = _strict_enum(InstallationOperation, record["operation"])
    application_profile = _strict_enum(RuntimeProfile, record["application_profile"])
    phase = _strict_enum(InstallationPhase, record["phase"])
    state = _strict_enum(InstallationState, record["state"])
    error_value = record["error_code"]
    error_code = (
        None
        if error_value is None
        else _strict_enum(InstallationErrorCode, error_value)
    )

    if record["bytes_completed"] is not None or record["bytes_total"] is not None:
        raise _invalid()
    retryable = record["retryable"]
    cancellation_safe = record["cancellation_safe"]
    if type(retryable) is not bool or type(cancellation_safe) is not bool:
        raise _invalid()

    if state is InstallationState.RUNNING:
        raise _invalid()
    if state is InstallationState.SUCCEEDED:
        if (
            phase is not InstallationPhase.COMPLETE
            or error_code is not None
            or retryable
            or cancellation_safe
        ):
            raise _invalid()
    elif state is InstallationState.CANCELLED:
        if (
            error_code is not InstallationErrorCode.CANCELLED
            or not retryable
            or not cancellation_safe
        ):
            raise _invalid()
    elif error_code is None or error_code is InstallationErrorCode.CANCELLED:
        raise _invalid()

    return InstallationEvent(
        operation_id=operation_id,
        sequence=sequence,
        operation=operation,
        application_profile=application_profile,
        phase=phase,
        state=state,
        bytes_completed=None,
        bytes_total=None,
        error_code=error_code,
        retryable=retryable,
        cancellation_safe=cancellation_safe,
    )


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _invalid()
        result[key] = value
    return result


def _reject_constant(_: str) -> NoReturn:
    raise _invalid()


def _decode_event(data: bytes) -> InstallationEvent:
    try:
        text = data.decode("utf-8")
        payload: object = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except InstallationHandoffError:
        raise
    except UnicodeError, json.JSONDecodeError, ValueError:
        raise _invalid() from None
    return _parse_event(payload)


def write_terminal_event(path: Path, event: InstallationEvent) -> None:
    """Create an initially absent file containing one validated terminal event."""

    try:
        if type(event) is not InstallationEvent:
            raise _invalid()
        parsed = _parse_event(event.to_dict())
        if parsed != event:
            raise _invalid()
        data = event.json_line().encode("utf-8")
        if len(data) > _MAX_EVENT_BYTES:
            raise _invalid()
    except InstallationHandoffError:
        raise
    except Exception:
        raise _invalid() from None

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_BINARY", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            written = stream.write(data)
            if written != len(data):
                raise OSError("short installation handoff write")
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _expected_enum[E: str](enum_type: type[E], value: E | str) -> E:
    if type(value) is enum_type:
        return value
    if type(value) is not str:
        raise _invalid()
    return _strict_enum(enum_type, value)


def _read_bounded(descriptor: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while total <= _MAX_EVENT_BYTES:
        chunk = os.read(descriptor, _MAX_EVENT_BYTES + 1 - total)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    if total > _MAX_EVENT_BYTES:
        raise _invalid()
    return b"".join(chunks)


def read_terminal_event(
    path: Path,
    *,
    operation: InstallationOperation | str,
    application_profile: RuntimeProfile | str,
    operation_id: str,
    sequence_start: int,
    exit_code: int,
) -> InstallationEvent:
    """Read and validate a bounded terminal event against its invocation."""

    expected_operation = _expected_enum(InstallationOperation, operation)
    expected_profile = _expected_enum(RuntimeProfile, application_profile)
    if type(operation_id) is not str or _OPERATION_ID.fullmatch(operation_id) is None:
        raise _invalid()
    if type(sequence_start) is not int or sequence_start < 0:
        raise _invalid()
    if type(exit_code) is not int or exit_code < 0:
        raise _invalid()

    descriptor = -1
    try:
        before = os.lstat(path)
        if not stat.S_ISREG(before.st_mode) or before.st_size > _MAX_EVENT_BYTES:
            raise _invalid()

        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_NONBLOCK", 0)
        flags |= getattr(os, "O_BINARY", 0)
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or not os.path.samestat(before, opened)
            or opened.st_size > _MAX_EVENT_BYTES
        ):
            raise _invalid()
        data = _read_bounded(descriptor)
    except InstallationHandoffError:
        raise
    except OSError:
        raise _invalid() from None
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                raise _invalid() from None

    event = _decode_event(data)
    if (
        event.operation is not expected_operation
        or event.application_profile is not expected_profile
        or event.operation_id != operation_id
        or event.sequence <= sequence_start
    ):
        raise _invalid()
    if exit_code == 0:
        if event.state is not InstallationState.SUCCEEDED:
            raise _invalid()
    elif event.state is InstallationState.SUCCEEDED:
        raise _invalid()
    if event.state is InstallationState.CANCELLED and exit_code != 130:
        raise _invalid()
    return event


__all__ = [
    "HANDOFF_PROTOCOL_VERSION",
    "InstallationHandoffError",
    "read_terminal_event",
    "write_terminal_event",
]
