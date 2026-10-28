"""Contract tests for the private terminal-event handoff codec."""

from __future__ import annotations

import json
import os
import stat
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from carlina.core.installation_handoff import (
    HANDOFF_PROTOCOL_VERSION,
    InstallationHandoffError,
    read_terminal_event,
    write_terminal_event,
)
from carlina.core.installation_lifecycle import (
    InstallationErrorCode,
    InstallationEvent,
    InstallationLifecycle,
    InstallationOperation,
)
from carlina.runtime_profiles import RuntimeProfile

pytestmark = [pytest.mark.fast, pytest.mark.contract]

_OPERATION_ID = "a" * 32
_DIAGNOSTIC = "Invalid installation handoff event."


def _terminal(outcome: str) -> tuple[InstallationEvent, int]:
    events: list[InstallationEvent] = []
    lifecycle = InstallationLifecycle(
        InstallationOperation.UPDATE,
        RuntimeProfile.DOCUMENTS,
        operation_id=_OPERATION_ID,
        sequence_start=2,
        event_sink=events.append,
    )
    if outcome == "succeeded":
        lifecycle.succeed()
        exit_code = 0
    elif outcome == "cancelled":
        lifecycle.cancel()
        exit_code = 130
    else:
        lifecycle.fail(
            InstallationErrorCode.DOWNLOAD_FAILED,
            retryable=True,
            cancellation_safe=True,
        )
        exit_code = 1
    return events[-1], exit_code


def _read(
    path: Path,
    *,
    operation: object = InstallationOperation.UPDATE,
    application_profile: object = RuntimeProfile.DOCUMENTS,
    operation_id: object = _OPERATION_ID,
    sequence_start: object = 2,
    exit_code: object = 0,
) -> InstallationEvent:
    return read_terminal_event(
        path,
        operation=cast(InstallationOperation | str, operation),
        application_profile=cast(RuntimeProfile | str, application_profile),
        operation_id=cast(str, operation_id),
        sequence_start=cast(int, sequence_start),
        exit_code=cast(int, exit_code),
    )


def _record(event: InstallationEvent) -> dict[str, object]:
    return event.to_dict()


@pytest.mark.parametrize("outcome", ["succeeded", "failed", "cancelled"])
def test_lifecycle_terminal_event_roundtrips_exactly(
    tmp_path: Path, outcome: str
) -> None:
    assert type(HANDOFF_PROTOCOL_VERSION) is int
    assert HANDOFF_PROTOCOL_VERSION == 1
    event, exit_code = _terminal(outcome)
    path = tmp_path / "handoff.json"

    write_terminal_event(path, event)

    assert path.read_bytes() == event.json_line().encode("utf-8")
    assert _read(path, exit_code=exit_code) == event


def test_writer_rejects_nonterminal_event_before_creating_file(tmp_path: Path) -> None:
    events: list[InstallationEvent] = []
    lifecycle = InstallationLifecycle("update", "documents", event_sink=events.append)
    lifecycle.advance("download", cancellation_safe=True)
    path = tmp_path / "handoff.json"

    with pytest.raises(InstallationHandoffError) as error:
        write_terminal_event(path, events[-1])

    assert str(error.value) == _DIAGNOSTIC
    assert not path.exists()


def test_writer_never_creates_an_event_larger_than_the_reader_limit(
    tmp_path: Path,
) -> None:
    event, _ = _terminal("succeeded")
    path = tmp_path / "oversized.json"
    with pytest.raises(InstallationHandoffError):
        write_terminal_event(path, replace(event, sequence=10**4096))
    assert not path.exists()


def test_writer_leaves_file_in_place_after_io_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    event, _ = _terminal("succeeded")
    path = tmp_path / "handoff.json"

    def fail_fsync(_: int) -> None:
        raise OSError("synthetic fsync failure")

    monkeypatch.setattr(os, "fsync", fail_fsync)
    with pytest.raises(OSError, match="synthetic fsync failure"):
        write_terminal_event(path, event)

    assert path.read_bytes() == event.json_line().encode("utf-8")


@pytest.mark.parametrize("existing", ["regular", "symlink"])
def test_writer_never_clobbers_existing_destination(
    tmp_path: Path, existing: str
) -> None:
    event, _ = _terminal("succeeded")
    path = tmp_path / "handoff.json"
    target = tmp_path / "target.json"
    target.write_bytes(b"preserve these bytes")
    if existing == "regular":
        path.write_bytes(b"preserve these bytes")
    else:
        try:
            path.symlink_to(target)
        except (NotImplementedError, OSError) as error:
            pytest.skip(f"symlink creation unavailable: {type(error).__name__}")

    with pytest.raises(OSError):
        write_terminal_event(path, event)

    assert target.read_bytes() == b"preserve these bytes"
    assert path.is_symlink() is (existing == "symlink")
    if existing == "regular":
        assert path.read_bytes() == b"preserve these bytes"


@pytest.mark.parametrize(
    "malformed_case",
    [
        "invalid_json",
        "invalid_utf8",
        "non_object",
        "missing_property",
        "extra_property",
        "duplicate_property",
        "wrong_format",
        "boolean_schema_version",
        "unknown_enum",
        "running_state",
        "uppercase_operation_id",
        "boolean_sequence",
        "non_null_bytes",
        "unknown_error_code",
        "non_boolean_flag",
        "inconsistent_success",
        "success_error_code",
        "success_retryable",
        "success_cancellation_safe",
        "failed_without_error_code",
        "failed_cancelled_error",
        "cancelled_wrong_error_code",
        "cancelled_not_retryable",
        "cancelled_not_safe",
        "oversized",
    ],
)
def test_reader_rejects_malformed_payload_with_fixed_diagnostic(
    tmp_path: Path, malformed_case: str
) -> None:
    event, _ = _terminal("succeeded")
    path = tmp_path / "handoff.json"
    payload = _record(event)
    if malformed_case == "invalid_json":
        raw = b"not json"
    elif malformed_case == "invalid_utf8":
        raw = b"\xff"
    elif malformed_case == "non_object":
        raw = b"[]"
    elif malformed_case == "missing_property":
        payload.pop("phase")
        raw = json.dumps(payload).encode("utf-8")
    elif malformed_case == "extra_property":
        payload["message"] = "not permitted"
        raw = json.dumps(payload).encode("utf-8")
    elif malformed_case == "duplicate_property":
        raw = (
            event.json_line()
            .encode("utf-8")
            .replace(
                b'"state":"succeeded"',
                b'"state":"succeeded","state":"succeeded"',
            )
        )
    elif malformed_case == "wrong_format":
        payload["format"] = "other"
        raw = json.dumps(payload).encode("utf-8")
    elif malformed_case == "boolean_schema_version":
        payload["schema_version"] = True
        raw = json.dumps(payload).encode("utf-8")
    elif malformed_case == "unknown_enum":
        payload["operation"] = "unknown"
        raw = json.dumps(payload).encode("utf-8")
    elif malformed_case == "running_state":
        payload["state"] = "running"
        raw = json.dumps(payload).encode("utf-8")
    elif malformed_case == "uppercase_operation_id":
        payload["operation_id"] = "A" * 32
        raw = json.dumps(payload).encode("utf-8")
    elif malformed_case == "boolean_sequence":
        payload["sequence"] = True
        raw = json.dumps(payload).encode("utf-8")
    elif malformed_case == "non_null_bytes":
        payload["bytes_completed"] = 0
        raw = json.dumps(payload).encode("utf-8")
    elif malformed_case == "unknown_error_code":
        payload["error_code"] = "unknown"
        raw = json.dumps(payload).encode("utf-8")
    elif malformed_case == "non_boolean_flag":
        payload["retryable"] = 1
        raw = json.dumps(payload).encode("utf-8")
    elif malformed_case == "inconsistent_success":
        payload["phase"] = "preflight"
        raw = json.dumps(payload).encode("utf-8")
    elif malformed_case == "success_error_code":
        payload["error_code"] = "operation_failed"
        raw = json.dumps(payload).encode("utf-8")
    elif malformed_case == "success_retryable":
        payload["retryable"] = True
        raw = json.dumps(payload).encode("utf-8")
    elif malformed_case == "success_cancellation_safe":
        payload["cancellation_safe"] = True
        raw = json.dumps(payload).encode("utf-8")
    elif malformed_case == "failed_without_error_code":
        payload.update(state="failed", phase="download", error_code=None)
        raw = json.dumps(payload).encode("utf-8")
    elif malformed_case == "failed_cancelled_error":
        payload.update(state="failed", phase="download", error_code="cancelled")
        raw = json.dumps(payload).encode("utf-8")
    elif malformed_case == "cancelled_wrong_error_code":
        payload.update(
            state="cancelled",
            error_code="operation_failed",
            retryable=True,
            cancellation_safe=True,
        )
        raw = json.dumps(payload).encode("utf-8")
    elif malformed_case == "cancelled_not_retryable":
        payload.update(
            state="cancelled",
            error_code="cancelled",
            retryable=False,
            cancellation_safe=True,
        )
        raw = json.dumps(payload).encode("utf-8")
    elif malformed_case == "cancelled_not_safe":
        payload.update(
            state="cancelled",
            error_code="cancelled",
            retryable=True,
            cancellation_safe=False,
        )
        raw = json.dumps(payload).encode("utf-8")
    else:
        raw = b" " * 4097
    path.write_bytes(raw)

    with pytest.raises(InstallationHandoffError) as error:
        _read(path)

    assert str(error.value) == _DIAGNOSTIC


@pytest.mark.parametrize(
    ("control", "value"),
    [
        ("operation", True),
        ("operation", 1),
        ("operation", "UPDATE"),
        ("application_profile", False),
        ("application_profile", 1),
        ("application_profile", "Documents"),
        ("operation_id", "A" * 32),
        ("sequence_start", True),
        ("sequence_start", -1),
        ("exit_code", False),
        ("exit_code", -1),
    ],
)
def test_reader_rejects_invalid_expected_controls(
    tmp_path: Path, control: str, value: object
) -> None:
    event, _ = _terminal("succeeded")
    path = tmp_path / "handoff.json"
    path.write_bytes(event.json_line().encode("utf-8"))

    with pytest.raises(InstallationHandoffError) as error:
        _read(path, **{control: value})

    assert str(error.value) == _DIAGNOSTIC


@pytest.mark.parametrize(
    ("outcome", "exit_code"),
    [("failed", 0), ("succeeded", 1), ("cancelled", 1)],
)
def test_reader_matches_terminal_state_to_exit_code(
    tmp_path: Path, outcome: str, exit_code: int
) -> None:
    event, _ = _terminal(outcome)
    path = tmp_path / "handoff.json"
    path.write_bytes(event.json_line().encode("utf-8"))

    with pytest.raises(InstallationHandoffError) as error:
        _read(path, exit_code=exit_code)

    assert str(error.value) == _DIAGNOSTIC


@pytest.mark.parametrize(
    ("control", "value"),
    [
        ("operation", InstallationOperation.INSTALL),
        ("application_profile", RuntimeProfile.WORKBENCH),
        ("operation_id", "b" * 32),
        ("sequence_start", 3),
    ],
)
def test_reader_rejects_identity_or_sequence_mismatch(
    tmp_path: Path, control: str, value: object
) -> None:
    event, _ = _terminal("succeeded")
    path = tmp_path / "handoff.json"
    path.write_bytes(event.json_line().encode("utf-8"))

    with pytest.raises(InstallationHandoffError) as error:
        _read(path, **{control: value})

    assert str(error.value) == _DIAGNOSTIC


@pytest.mark.parametrize("file_kind", ["directory", "symlink", "fifo"])
def test_reader_rejects_non_regular_paths_without_blocking(
    tmp_path: Path, file_kind: str
) -> None:
    event, _ = _terminal("succeeded")
    regular = tmp_path / "regular.json"
    regular.write_bytes(event.json_line().encode("utf-8"))
    path = tmp_path / "handoff"
    if file_kind == "directory":
        path.mkdir()
    elif file_kind == "symlink":
        try:
            path.symlink_to(regular)
        except (NotImplementedError, OSError) as error:
            pytest.skip(f"symlink creation unavailable: {type(error).__name__}")
    else:
        if not hasattr(os, "mkfifo"):
            pytest.skip("FIFO creation is unavailable")
        os.mkfifo(path)

    with pytest.raises(InstallationHandoffError) as error:
        _read(path)

    assert str(error.value) == _DIAGNOSTIC


def test_writer_does_not_create_parent_and_uses_private_mode(tmp_path: Path) -> None:
    event, _ = _terminal("succeeded")
    missing_parent = tmp_path / "missing" / "nested"
    with pytest.raises(OSError):
        write_terminal_event(missing_parent / "handoff.json", event)
    assert not missing_parent.exists()

    path = tmp_path / "handoff.json"
    write_terminal_event(path, event)
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_reader_does_not_create_a_missing_path(tmp_path: Path) -> None:
    path = tmp_path / "absent" / "handoff.json"

    with pytest.raises(InstallationHandoffError) as error:
        _read(path)

    assert str(error.value) == _DIAGNOSTIC
    assert not path.parent.exists()
