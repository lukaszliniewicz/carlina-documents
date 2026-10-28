"""Strict, dependency-free records for installer lifecycle progress."""

from __future__ import annotations

import json
import re
import threading
import uuid
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from enum import StrEnum

from carlina.runtime_profiles import RuntimeProfile


class InstallationOperation(StrEnum):
    """Installer operation represented by lifecycle events."""

    INSTALL = "install"
    UPDATE = "update"
    ROLLBACK = "rollback"


class InstallationPhase(StrEnum):
    """Current bounded phase of an installation operation."""

    PREFLIGHT = "preflight"
    DOWNLOAD = "download"
    VERIFICATION = "verification"
    UNPACKING = "unpacking"
    ENVIRONMENT = "environment"
    INITIALIZATION = "initialization"
    ACTIVATION = "activation"
    READINESS = "readiness"
    RECOVERY = "recovery"
    COMPLETE = "complete"


class InstallationState(StrEnum):
    """State carried by a lifecycle event."""

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class InstallationErrorCode(StrEnum):
    """Safe machine-readable failure categories for installer events."""

    INVALID_SOURCE = "invalid_source"
    VERIFICATION_FAILED = "verification_failed"
    INCOMPATIBLE_RELEASE = "incompatible_release"
    DOWNLOAD_FAILED = "download_failed"
    OPERATION_FAILED = "operation_failed"
    RECOVERY_REQUIRED = "recovery_required"
    CANCELLED = "cancelled"


class InstallationCancelled(RuntimeError):
    """A cancellation request observed at a safe lifecycle boundary."""

    def __init__(self) -> None:
        super().__init__("Installation cancellation requested at a safe boundary.")


@dataclass(frozen=True, slots=True)
class InstallationEvent:
    """One privacy-bounded installer lifecycle event."""

    format: str = field(default="carlina.installation-event", init=False)
    schema_version: int = field(default=1, init=False)
    operation_id: str
    sequence: int
    operation: InstallationOperation
    application_profile: RuntimeProfile
    phase: InstallationPhase
    state: InstallationState
    bytes_completed: int | None = None
    bytes_total: int | None = None
    error_code: InstallationErrorCode | None = None
    retryable: bool = False
    cancellation_safe: bool = False

    def to_dict(self) -> dict[str, object]:
        """Return exactly the stable JSON properties in contract order."""

        return {
            "format": self.format,
            "schema_version": self.schema_version,
            "operation_id": self.operation_id,
            "sequence": self.sequence,
            "operation": self.operation.value,
            "application_profile": self.application_profile.value,
            "phase": self.phase.value,
            "state": self.state.value,
            "bytes_completed": self.bytes_completed,
            "bytes_total": self.bytes_total,
            "error_code": self.error_code.value if self.error_code else None,
            "retryable": self.retryable,
            "cancellation_safe": self.cancellation_safe,
        }

    def json_line(self) -> str:
        """Return one compact ASCII JSON object followed by a newline."""

        return (
            json.dumps(
                self.to_dict(),
                ensure_ascii=True,
                separators=(",", ":"),
            )
            + "\n"
        )


def _exact_bool(value: object, name: str) -> bool:
    if type(value) is not bool:
        raise TypeError(f"{name} must be a bool.")
    return value


def _byte_value(value: int | None, name: str) -> int | None:
    if value is None:
        return None
    if type(value) is not int:
        raise TypeError(f"{name} must be a non-negative int or None.")
    if value < 0:
        raise ValueError(f"{name} must be a non-negative int or None.")
    return value


class InstallationLifecycle:
    """Single-threaded state machine that emits privacy-bounded events."""

    def __init__(
        self,
        operation: InstallationOperation | str,
        application_profile: RuntimeProfile | str,
        *,
        operation_id: str | None = None,
        sequence_start: int = 0,
        event_sink: Callable[[InstallationEvent], None] | None = None,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> None:
        selected_operation = InstallationOperation(operation)
        selected_profile = RuntimeProfile(application_profile)
        if type(sequence_start) is not int:
            raise TypeError("sequence_start must be an int.")
        if sequence_start < 0:
            raise ValueError("sequence_start must be non-negative.")
        if operation_id is None:
            selected_operation_id = uuid.uuid4().hex
        elif type(operation_id) is str and re.fullmatch(r"[0-9a-f]{32}", operation_id):
            selected_operation_id = operation_id
        else:
            raise ValueError(
                "operation_id must be 32 lowercase hexadecimal characters."
            )
        if event_sink is not None and not callable(event_sink):
            raise TypeError("event_sink must be callable.")
        if cancel_requested is not None and not callable(cancel_requested):
            raise TypeError("cancel_requested must be callable.")

        self._operation = selected_operation
        self._application_profile = selected_profile
        self._operation_id = selected_operation_id
        self._sequence = sequence_start
        self._event_sink = event_sink
        self._cancel_requested = cancel_requested
        self._phase = InstallationPhase.PREFLIGHT
        self._cancellation_safe = True
        self._state = InstallationState.RUNNING
        self._owner_thread = threading.get_ident()

    @property
    def operation(self) -> InstallationOperation:
        return self._operation

    @property
    def application_profile(self) -> RuntimeProfile:
        return self._application_profile

    @property
    def operation_id(self) -> str:
        return self._operation_id

    @property
    def phase(self) -> InstallationPhase:
        return self._phase

    @property
    def cancellation_safe(self) -> bool:
        return self._cancellation_safe

    @property
    def terminal(self) -> bool:
        return self._state is not InstallationState.RUNNING

    @property
    def sequence(self) -> int:
        return self._sequence

    def _assert_owner_thread(self) -> None:
        if threading.get_ident() != self._owner_thread:
            raise RuntimeError("Installation lifecycle belongs to another thread.")

    def _record_event(
        self,
        *,
        phase: InstallationPhase,
        state: InstallationState,
        cancellation_safe: bool,
        bytes_completed: int | None = None,
        bytes_total: int | None = None,
        error_code: InstallationErrorCode | None = None,
        retryable: bool = False,
    ) -> None:
        event = InstallationEvent(
            operation_id=self._operation_id,
            sequence=self._sequence + 1,
            operation=self._operation,
            application_profile=self._application_profile,
            phase=phase,
            state=state,
            bytes_completed=bytes_completed,
            bytes_total=bytes_total,
            error_code=error_code,
            retryable=retryable,
            cancellation_safe=cancellation_safe,
        )
        self._sequence = event.sequence
        self._phase = phase
        self._cancellation_safe = cancellation_safe
        self._state = state
        if self._event_sink is None:
            return
        with suppress(Exception):
            self._event_sink(event)

    def advance(
        self,
        phase: InstallationPhase | str,
        *,
        cancellation_safe: bool,
        bytes_completed: int | None = None,
        bytes_total: int | None = None,
    ) -> None:
        """Record a running event after validating all supplied state."""

        self._assert_owner_thread()
        if self.terminal:
            raise RuntimeError("Cannot advance a terminal installation lifecycle.")
        selected_phase = InstallationPhase(phase)
        selected_cancellation_safe = _exact_bool(cancellation_safe, "cancellation_safe")
        selected_bytes_completed = _byte_value(bytes_completed, "bytes_completed")
        selected_bytes_total = _byte_value(bytes_total, "bytes_total")
        if (
            selected_bytes_completed is not None
            and selected_bytes_total is not None
            and selected_bytes_completed > selected_bytes_total
        ):
            raise ValueError("bytes_completed cannot exceed bytes_total.")

        self._record_event(
            phase=selected_phase,
            state=InstallationState.RUNNING,
            cancellation_safe=selected_cancellation_safe,
            bytes_completed=selected_bytes_completed,
            bytes_total=selected_bytes_total,
        )

    def checkpoint(self, phase: InstallationPhase | str) -> None:
        """Emit a safe running checkpoint, then observe cancellation requests."""

        self.advance(phase, cancellation_safe=True)
        self.check_cancelled()

    def check_cancelled(self) -> None:
        """Raise at a safe boundary when the control predicate requests cancel."""

        self._assert_owner_thread()
        if not self._cancellation_safe:
            return
        if self.cancellation_requested():
            raise InstallationCancelled()

    def cancellation_requested(self) -> bool:
        """Sample a request without changing state or declaring a step safe."""

        self._assert_owner_thread()
        if self._cancel_requested is None:
            return False
        requested = self._cancel_requested()
        if type(requested) is not bool:
            raise TypeError("cancel_requested must return a bool.")
        return requested

    def progress(self, completed: int, total: int | None) -> None:
        """Check for safe cancellation, then emit current-phase byte progress."""

        self.check_cancelled()
        self.advance(
            self._phase,
            cancellation_safe=self._cancellation_safe,
            bytes_completed=completed,
            bytes_total=total,
        )

    def succeed(self) -> None:
        """Record one successful terminal event."""

        self._assert_owner_thread()
        if self.terminal:
            return
        self._record_event(
            phase=InstallationPhase.COMPLETE,
            state=InstallationState.SUCCEEDED,
            cancellation_safe=False,
        )

    def fail(
        self,
        code: InstallationErrorCode | str = InstallationErrorCode.OPERATION_FAILED,
        *,
        retryable: bool = False,
        cancellation_safe: bool = False,
    ) -> None:
        """Record one failed terminal event at the current phase."""

        self._assert_owner_thread()
        if self.terminal:
            return
        selected_code = InstallationErrorCode(code)
        if selected_code is InstallationErrorCode.CANCELLED:
            raise ValueError("Use cancel() to record a cancellation outcome.")
        selected_retryable = _exact_bool(retryable, "retryable")
        selected_cancellation_safe = _exact_bool(cancellation_safe, "cancellation_safe")
        self._record_event(
            phase=self._phase,
            state=InstallationState.FAILED,
            cancellation_safe=selected_cancellation_safe,
            error_code=selected_code,
            retryable=selected_retryable,
        )

    def cancel(self) -> None:
        """Record cancellation only after the owner reaches a safe boundary."""

        self._assert_owner_thread()
        if self.terminal:
            return
        if not self._cancellation_safe:
            raise RuntimeError("Installation cancellation is unsafe at this boundary.")
        self._record_event(
            phase=self._phase,
            state=InstallationState.CANCELLED,
            cancellation_safe=True,
            error_code=InstallationErrorCode.CANCELLED,
            retryable=True,
        )


__all__ = [
    "InstallationCancelled",
    "InstallationErrorCode",
    "InstallationEvent",
    "InstallationLifecycle",
    "InstallationOperation",
    "InstallationPhase",
    "InstallationState",
]
