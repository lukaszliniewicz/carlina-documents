"""Contract tests for the dependency-free installer lifecycle event API."""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Callable
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import cast
from unittest.mock import Mock

import pytest
from jsonschema import Draft202012Validator, ValidationError

from carlina.core.installation_lifecycle import (
    InstallationCancelled,
    InstallationErrorCode,
    InstallationEvent,
    InstallationLifecycle,
    InstallationOperation,
    InstallationPhase,
    InstallationState,
)
from carlina.runtime_profiles import RuntimeProfile

pytestmark = [pytest.mark.fast, pytest.mark.contract]

_SCHEMA_PATH = (
    Path(__file__).parents[1] / "carlina" / "core" / "installation-event-v1.schema.json"
)
_SCHEMA = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
_VALIDATOR = Draft202012Validator(_SCHEMA)
_EXPECTED_PROPERTIES = [
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
]


def test_schema_is_draft_2020_12_and_events_are_exact_ascii_jsonl() -> None:
    Draft202012Validator.check_schema(_SCHEMA)
    for profile in RuntimeProfile:
        events: list[InstallationEvent] = []
        lifecycle = InstallationLifecycle(
            InstallationOperation.UPDATE,
            profile,
            operation_id="a" * 32,
            event_sink=events.append,
        )
        assert events == []
        assert lifecycle.phase is InstallationPhase.PREFLIGHT
        assert lifecycle.cancellation_safe is True
        assert lifecycle.terminal is False
        assert lifecycle.sequence == 0

        lifecycle.advance(
            InstallationPhase.DOWNLOAD,
            cancellation_safe=True,
            bytes_completed=0,
            bytes_total=None,
        )
        lifecycle.succeed()

        jsonl = "".join(event.json_line() for event in events)
        lines = jsonl.splitlines(keepends=True)
        assert len(lines) == 2
        for line in lines:
            assert line.endswith("\n")
            assert line.count("\n") == 1
            assert line.isascii()
            payload = json.loads(line)
            _VALIDATOR.validate(payload)
            assert list(payload) == _EXPECTED_PROPERTIES
            assert payload["format"] == "carlina.installation-event"
            assert payload["schema_version"] == 1
            assert payload["operation"] == "update"
            assert payload["application_profile"] == profile.value
        assert json.loads(lines[0])["bytes_completed"] == 0
        assert json.loads(lines[0])["bytes_total"] is None
        assert json.loads(lines[0])["error_code"] is None
        assert json.loads(lines[1])["bytes_completed"] is None
        assert json.loads(lines[1])["bytes_total"] is None
        assert json.loads(lines[1])["error_code"] is None
        assert json.loads(lines[1])["state"] == "succeeded"


def test_event_is_frozen_and_has_no_extra_json_properties() -> None:
    event = InstallationEvent(
        operation_id="b" * 32,
        sequence=1,
        operation=InstallationOperation.INSTALL,
        application_profile=RuntimeProfile.DOCUMENTS,
        phase=InstallationPhase.PREFLIGHT,
        state=InstallationState.RUNNING,
    )

    assert list(event.to_dict()) == _EXPECTED_PROPERTIES
    with pytest.raises(FrozenInstanceError):
        event.__setattr__("sequence", 2)
    with pytest.raises(ValidationError):
        _VALIDATOR.validate({**event.to_dict(), "message": "not allowed"})
    with pytest.raises(ValidationError):
        _VALIDATOR.validate({**event.to_dict(), "operation_id": "b" * 32 + "\n"})


def test_constructor_defaults_and_generated_operation_id() -> None:
    sink = Mock()
    lifecycle = InstallationLifecycle(
        "install", "documents", event_sink=sink, sequence_start=4
    )

    assert re.fullmatch(r"[0-9a-f]{32}", lifecycle.operation_id)
    assert lifecycle.operation is InstallationOperation.INSTALL
    assert lifecycle.application_profile is RuntimeProfile.DOCUMENTS
    assert lifecycle.sequence == 4
    assert lifecycle.phase is InstallationPhase.PREFLIGHT
    assert lifecycle.cancellation_safe is True
    assert lifecycle.terminal is False
    sink.assert_not_called()


@pytest.mark.parametrize("operation", ["Install", "unknown"])
def test_constructor_rejects_unknown_operation_before_emission(operation: str) -> None:
    sink = Mock()
    with pytest.raises(ValueError):
        InstallationLifecycle(operation, "documents", event_sink=sink)
    sink.assert_not_called()


def test_constructor_rejects_unknown_profile_before_emission() -> None:
    sink = Mock()
    with pytest.raises(ValueError):
        InstallationLifecycle("install", "Documents", event_sink=sink)
    sink.assert_not_called()


@pytest.mark.parametrize("operation_id", ["A" * 32, "a" * 31, "g" * 32])
def test_constructor_requires_exact_lowercase_operation_id(operation_id: str) -> None:
    with pytest.raises(ValueError):
        InstallationLifecycle("install", "documents", operation_id=operation_id)


@pytest.mark.parametrize(
    ("sequence_start", "exception"),
    [
        (True, TypeError),
        (False, TypeError),
        (1.0, TypeError),
        ("1", TypeError),
        (-1, ValueError),
    ],
)
def test_constructor_requires_exact_nonnegative_sequence_start(
    sequence_start: object,
    exception: type[Exception],
) -> None:
    with pytest.raises(exception):
        InstallationLifecycle(
            "install", "documents", sequence_start=cast(int, sequence_start)
        )


@pytest.mark.parametrize(
    ("completed", "total", "safe", "exception"),
    [
        (True, 2, True, TypeError),
        (1, False, True, TypeError),
        (-1, 2, True, ValueError),
        (1, -1, True, ValueError),
        (3, 2, True, ValueError),
        (1, 2, 1, TypeError),
    ],
)
def test_advance_rejects_invalid_bytes_or_safety_without_mutation(
    completed: object,
    total: object,
    safe: object,
    exception: type[Exception],
) -> None:
    events: list[InstallationEvent] = []
    lifecycle = InstallationLifecycle("install", "documents", event_sink=events.append)

    with pytest.raises(exception):
        lifecycle.advance(
            "download",
            cancellation_safe=cast(bool, safe),
            bytes_completed=cast(int | None, completed),
            bytes_total=cast(int | None, total),
        )

    assert lifecycle.phase is InstallationPhase.PREFLIGHT
    assert lifecycle.cancellation_safe is True
    assert lifecycle.sequence == 0
    assert lifecycle.terminal is False
    assert events == []


def test_advance_accepts_unknown_total_and_does_not_poll_cancellation() -> None:
    events: list[InstallationEvent] = []
    cancel_requested = Mock(return_value=True)
    lifecycle = InstallationLifecycle(
        "install",
        "workbench",
        event_sink=events.append,
        cancel_requested=cancel_requested,
    )

    lifecycle.advance(
        InstallationPhase.DOWNLOAD,
        cancellation_safe=True,
        bytes_completed=7,
        bytes_total=None,
    )

    cancel_requested.assert_not_called()
    assert lifecycle.sequence == 1
    assert events[0].bytes_completed == 7
    assert events[0].bytes_total is None


def test_checkpoint_observes_cancel_without_emitting_terminal_event() -> None:
    events: list[InstallationEvent] = []
    lifecycle = InstallationLifecycle(
        "update",
        "documents",
        event_sink=events.append,
        cancel_requested=lambda: True,
    )

    with pytest.raises(InstallationCancelled) as failure:
        lifecycle.checkpoint("download")

    assert str(failure.value) == (
        "Installation cancellation requested at a safe boundary."
    )
    assert lifecycle.phase is InstallationPhase.DOWNLOAD
    assert lifecycle.cancellation_safe is True
    assert lifecycle.terminal is False
    assert lifecycle.sequence == 1
    assert [event.state for event in events] == [InstallationState.RUNNING]

    lifecycle.cancel()
    assert lifecycle.terminal is True
    assert lifecycle.sequence == 2
    assert events[-1].state is InstallationState.CANCELLED
    assert events[-1].error_code is InstallationErrorCode.CANCELLED
    assert events[-1].retryable is True
    assert events[-1].cancellation_safe is True


def test_progress_observes_safe_cancellation_before_emitting_progress() -> None:
    events: list[InstallationEvent] = []
    lifecycle = InstallationLifecycle(
        "install",
        "workbench",
        event_sink=events.append,
        cancel_requested=lambda: True,
    )

    with pytest.raises(InstallationCancelled):
        lifecycle.progress(1, 2)

    assert events == []
    assert lifecycle.sequence == 0
    assert lifecycle.terminal is False
    lifecycle.cancel()
    assert len(events) == 1
    assert events[0].state is InstallationState.CANCELLED


def test_unsafe_boundary_never_polls_or_cancels() -> None:
    events: list[InstallationEvent] = []
    cancel_requested = Mock(return_value=True)
    lifecycle = InstallationLifecycle(
        "update",
        "documents",
        event_sink=events.append,
        cancel_requested=cancel_requested,
    )
    lifecycle.advance("unpacking", cancellation_safe=False)

    lifecycle.check_cancelled()
    lifecycle.progress(4, 10)
    with pytest.raises(RuntimeError, match="unsafe at this boundary"):
        lifecycle.cancel()

    cancel_requested.assert_not_called()
    assert lifecycle.phase is InstallationPhase.UNPACKING
    assert lifecycle.cancellation_safe is False
    assert lifecycle.terminal is False
    assert lifecycle.sequence == 2
    assert events[-1].bytes_completed == 4
    assert events[-1].bytes_total == 10


@pytest.mark.parametrize("profile", ["documents", "workbench"])
def test_explicit_request_sample_does_not_make_an_unsafe_step_cancellable(
    profile: str,
) -> None:
    events: list[InstallationEvent] = []
    requested = Mock(return_value=True)
    lifecycle = InstallationLifecycle(
        "update", profile, event_sink=events.append, cancel_requested=requested
    )
    lifecycle.advance("unpacking", cancellation_safe=False)

    assert lifecycle.cancellation_requested() is True

    requested.assert_called_once_with()
    assert lifecycle.cancellation_safe is False
    assert lifecycle.terminal is False
    assert lifecycle.sequence == 1
    assert len(events) == 1
    lifecycle.check_cancelled()
    requested.assert_called_once_with()


def test_request_sample_validates_callback_without_mutating_events() -> None:
    lifecycle = InstallationLifecycle("update", "documents")
    assert lifecycle.cancellation_requested() is False
    assert lifecycle.sequence == 0

    malformed = InstallationLifecycle(
        "update", "documents", cancel_requested=cast("Callable[[], bool]", lambda: 1)
    )
    with pytest.raises(TypeError, match="cancel_requested must return a bool"):
        malformed.cancellation_requested()
    assert malformed.sequence == 0


def test_request_sample_rejects_non_owner_thread_without_polling() -> None:
    requested = Mock(return_value=True)
    lifecycle = InstallationLifecycle("update", "documents", cancel_requested=requested)
    errors: list[BaseException] = []

    def sample() -> None:
        try:
            lifecycle.cancellation_requested()
        except RuntimeError as error:
            errors.append(error)

    worker = threading.Thread(target=sample)
    worker.start()
    worker.join(timeout=5)

    assert not worker.is_alive()
    assert len(errors) == 1
    assert str(errors[0]) == "Installation lifecycle belongs to another thread."
    requested.assert_not_called()


def test_control_predicate_failure_propagates_without_terminal_event() -> None:
    events: list[InstallationEvent] = []

    def fail_predicate() -> bool:
        raise LookupError("predicate failed")

    lifecycle = InstallationLifecycle(
        "rollback",
        "workbench",
        event_sink=events.append,
        cancel_requested=fail_predicate,
    )
    with pytest.raises(LookupError, match="predicate failed"):
        lifecycle.check_cancelled()

    assert lifecycle.sequence == 0
    assert lifecycle.terminal is False
    assert events == []


def test_fail_rejects_cancel_code_and_non_boolean_options_without_mutation() -> None:
    events: list[InstallationEvent] = []
    lifecycle = InstallationLifecycle("update", "documents", event_sink=events.append)

    with pytest.raises(ValueError, match=r"Use cancel\(\)"):
        lifecycle.fail(InstallationErrorCode.CANCELLED)
    with pytest.raises(TypeError, match="retryable must be a bool"):
        lifecycle.fail("operation_failed", retryable=cast(bool, 1))

    assert lifecycle.sequence == 0
    assert lifecycle.terminal is False
    assert events == []


@pytest.mark.parametrize(
    ("terminal_method", "expected_state"),
    [
        ("succeed", InstallationState.SUCCEEDED),
        ("fail", InstallationState.FAILED),
        ("cancel", InstallationState.CANCELLED),
    ],
)
def test_terminal_methods_are_idempotent_and_emit_one_terminal_event(
    terminal_method: str,
    expected_state: InstallationState,
) -> None:
    events: list[InstallationEvent] = []
    lifecycle = InstallationLifecycle("install", "documents", event_sink=events.append)

    if terminal_method == "succeed":
        lifecycle.succeed()
    elif terminal_method == "fail":
        lifecycle.fail("verification_failed", retryable=True)
    else:
        lifecycle.cancel()
    lifecycle.succeed()
    lifecycle.fail("operation_failed")
    lifecycle.cancel()

    terminal_events = [
        event for event in events if event.state is not InstallationState.RUNNING
    ]
    assert len(terminal_events) == 1
    assert terminal_events[0].state is expected_state
    assert lifecycle.terminal is True
    assert lifecycle.sequence == 1
    with pytest.raises(RuntimeError, match="terminal installation lifecycle"):
        lifecycle.advance("recovery", cancellation_safe=True)


def test_sequence_is_monotonic_from_sequence_start() -> None:
    events: list[InstallationEvent] = []
    lifecycle = InstallationLifecycle(
        "rollback", "workbench", sequence_start=9, event_sink=events.append
    )

    lifecycle.advance("preflight", cancellation_safe=True)
    lifecycle.advance("recovery", cancellation_safe=False)
    lifecycle.succeed()

    assert [event.sequence for event in events] == [10, 11, 12]
    assert lifecycle.sequence == 12


@pytest.mark.parametrize("error_type", [RuntimeError, BrokenPipeError])
def test_event_sink_exceptions_do_not_abort_unsafe_or_terminal_emission(
    error_type: type[Exception],
) -> None:
    seen: list[InstallationEvent] = []
    cancel_requested = Mock(return_value=True)

    def broken_sink(event: InstallationEvent) -> None:
        seen.append(event)
        raise error_type("observer failure")

    lifecycle = InstallationLifecycle(
        "update",
        "documents",
        event_sink=broken_sink,
        cancel_requested=cancel_requested,
    )

    lifecycle.advance("unpacking", cancellation_safe=False)
    lifecycle.check_cancelled()
    lifecycle.succeed()

    cancel_requested.assert_not_called()
    assert [event.state for event in seen] == [
        InstallationState.RUNNING,
        InstallationState.SUCCEEDED,
    ]
    assert lifecycle.sequence == 2
    assert lifecycle.terminal is True


def test_event_sink_does_not_swallow_base_exception() -> None:
    def interrupt_sink(_event: InstallationEvent) -> None:
        raise KeyboardInterrupt

    lifecycle = InstallationLifecycle("install", "workbench", event_sink=interrupt_sink)

    with pytest.raises(KeyboardInterrupt):
        lifecycle.advance("download", cancellation_safe=False)

    assert lifecycle.sequence == 1
    assert lifecycle.phase is InstallationPhase.DOWNLOAD
    assert lifecycle.cancellation_safe is False
    assert lifecycle.terminal is False


def test_lifecycle_rejects_mutation_from_non_owner_thread() -> None:
    events: list[InstallationEvent] = []
    lifecycle = InstallationLifecycle("install", "documents", event_sink=events.append)
    failures: list[BaseException] = []

    def advance_from_other_thread() -> None:
        try:
            lifecycle.advance("download", cancellation_safe=True)
        except BaseException as exc:
            failures.append(exc)

    worker = threading.Thread(target=advance_from_other_thread)
    worker.start()
    worker.join()

    assert len(failures) == 1
    assert isinstance(failures[0], RuntimeError)
    assert lifecycle.sequence == 0
    assert events == []
