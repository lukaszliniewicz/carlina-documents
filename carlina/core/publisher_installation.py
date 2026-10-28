"""Bounded protocol and process bridge for native publisher bootstraps."""

from __future__ import annotations

import math
import os
import queue
import re
import stat
import subprocess
import threading
import time
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from carlina.core.installation_lifecycle import (
    InstallationCancelled,
    InstallationErrorCode,
    InstallationLifecycle,
    InstallationPhase,
    InstallationState,
)

_PROTOCOL = "carlina.publisher-bootstrap/1"
_MAX_RECORD_BYTES = 4096
_READ_SIZE = 4097
_QUEUE_SIZE = 4
_POLL_SECONDS = 0.05
_DECIMAL = re.compile(r"(?:0|[1-9][0-9]{0,14})\Z")
_ALLOWED_FAILURE_CODES = frozenset(
    {
        InstallationErrorCode.OPERATION_FAILED,
        InstallationErrorCode.INCOMPATIBLE_RELEASE,
        InstallationErrorCode.DOWNLOAD_FAILED,
        InstallationErrorCode.VERIFICATION_FAILED,
    }
)
_BOOTSTRAP_PHASES = {
    "preflight": InstallationPhase.PREFLIGHT,
    "download": InstallationPhase.DOWNLOAD,
    "verification": InstallationPhase.VERIFICATION,
    "unpacking": InstallationPhase.UNPACKING,
    "environment": InstallationPhase.ENVIRONMENT,
}


class PublisherBootstrapFailure(RuntimeError):
    """Privacy-safe failure reported by a publisher bootstrap or its bridge."""

    def __init__(self, error_code: InstallationErrorCode | str) -> None:
        try:
            selected_code = InstallationErrorCode(error_code)
        except TypeError, ValueError:
            raise ValueError("Unsupported publisher bootstrap error code.") from None
        if selected_code not in _ALLOWED_FAILURE_CODES:
            raise ValueError("Unsupported publisher bootstrap error code.")
        self.error_code = selected_code
        self.retryable = selected_code is InstallationErrorCode.DOWNLOAD_FAILED
        super().__init__("Publisher bootstrap did not complete.")


@dataclass(frozen=True, slots=True)
class PublisherPhase:
    """One validated publisher substep, without outer event identity."""

    phase: InstallationPhase
    cancellation_safe: bool
    bytes_completed: int | None
    bytes_total: int | None


@dataclass(frozen=True, slots=True)
class PublisherResult:
    """One validated publisher terminal result."""

    state: InstallationState
    error_code: InstallationErrorCode | None
    retryable: bool


def _incompatible() -> PublisherBootstrapFailure:
    return PublisherBootstrapFailure(InstallationErrorCode.INCOMPATIBLE_RELEASE)


def _parse_byte_count(token: str) -> int | None:
    if token == "null":
        return None
    if _DECIMAL.fullmatch(token) is None:
        raise _incompatible()
    return int(token)


class PublisherProgressReader:
    """Strict reader for the ASCII publisher-bootstrap line protocol."""

    def __init__(self) -> None:
        self._ready = False
        self._result: PublisherResult | None = None
        self._finished = False

    def feed(self, record: bytes) -> PublisherPhase | None:
        """Validate one complete LF-terminated record."""

        if self._finished or self._result is not None:
            raise _incompatible()
        if record.endswith(b"\r\n"):
            body = record[:-2]
        elif record.endswith(b"\n"):
            body = record[:-1]
        else:
            raise _incompatible()
        if len(body) > _MAX_RECORD_BYTES:
            raise _incompatible()
        try:
            text = body.decode("ascii")
        except UnicodeDecodeError:
            raise _incompatible() from None
        tokens = text.split(" ")
        if any(not token for token in tokens) or len(tokens) < 2:
            raise _incompatible()
        if tokens[0] != _PROTOCOL:
            raise _incompatible()

        kind = tokens[1]
        if kind == "ready":
            if len(tokens) != 2 or self._ready or self._result is not None:
                raise _incompatible()
            self._ready = True
            return None

        if not self._ready:
            raise _incompatible()

        if kind == "phase":
            if len(tokens) != 6:
                raise _incompatible()
            phase = _BOOTSTRAP_PHASES.get(tokens[2])
            if phase is None:
                raise _incompatible()
            if tokens[3] == "true":
                cancellation_safe = True
            elif tokens[3] == "false":
                cancellation_safe = False
            else:
                raise _incompatible()
            bytes_completed = _parse_byte_count(tokens[4])
            bytes_total = _parse_byte_count(tokens[5])
            if (bytes_completed is None) != (bytes_total is None):
                raise _incompatible()
            if bytes_completed is not None and (
                phase is not InstallationPhase.DOWNLOAD
                or bytes_total is None
                or bytes_total <= 0
                or bytes_completed > bytes_total
            ):
                raise _incompatible()
            return PublisherPhase(
                phase=phase,
                cancellation_safe=cancellation_safe,
                bytes_completed=bytes_completed,
                bytes_total=bytes_total,
            )

        if kind == "result":
            if len(tokens) != 5:
                raise _incompatible()
            try:
                state = InstallationState(tokens[2])
            except ValueError:
                raise _incompatible() from None
            if tokens[4] == "true":
                retryable = True
            elif tokens[4] == "false":
                retryable = False
            else:
                raise _incompatible()
            if state is InstallationState.SUCCEEDED:
                if tokens[3] != "null" or retryable:
                    raise _incompatible()
                error_code = None
            elif state is InstallationState.CANCELLED:
                if tokens[3] != InstallationErrorCode.CANCELLED.value or not retryable:
                    raise _incompatible()
                error_code = InstallationErrorCode.CANCELLED
            elif state is InstallationState.FAILED:
                try:
                    error_code = InstallationErrorCode(tokens[3])
                except ValueError:
                    raise _incompatible() from None
                if error_code not in {
                    InstallationErrorCode.DOWNLOAD_FAILED,
                    InstallationErrorCode.VERIFICATION_FAILED,
                    InstallationErrorCode.OPERATION_FAILED,
                }:
                    raise _incompatible()
                if retryable is not (
                    error_code is InstallationErrorCode.DOWNLOAD_FAILED
                ):
                    raise _incompatible()
            else:
                raise _incompatible()
            self._result = PublisherResult(
                state=state,
                error_code=error_code,
                retryable=retryable,
            )
            return None

        raise _incompatible()

    def finish(self, exit_code: int) -> PublisherResult:
        """Require a complete stream and validate its process exit status."""

        if self._finished or type(exit_code) is not int or not self._ready:
            raise _incompatible()
        result = self._result
        if result is None:
            raise _incompatible()
        if result.state is InstallationState.SUCCEEDED:
            valid_exit = exit_code == 0
        elif result.state is InstallationState.CANCELLED:
            valid_exit = exit_code == 130
        else:
            valid_exit = exit_code != 0
        if not valid_exit:
            raise _incompatible()
        self._finished = True
        return result


class _ReaderFault:
    __slots__ = ()


_READER_FAULT = _ReaderFault()
_STREAM_EOF = object()


def _pump_stdout(stdout: BinaryIO, output: queue.Queue[object]) -> None:
    """Drain a pipe in bounded chunks without parsing it off the owner thread."""

    try:
        while True:
            chunk = stdout.read(_READ_SIZE)
            if not chunk:
                break
            output.put(chunk)
    except BaseException:
        output.put(_READER_FAULT)
    finally:
        output.put(_STREAM_EOF)


def _validate_control_path(path: Path) -> None:
    try:
        parent_info = os.lstat(path.parent)
    except OSError:
        raise ValueError("Publisher cancellation directory is unavailable.") from None
    if not stat.S_ISDIR(parent_info.st_mode):
        raise ValueError("Publisher cancellation parent must be a regular directory.")
    try:
        os.lstat(path)
    except FileNotFoundError:
        return
    except OSError:
        raise ValueError("Publisher cancellation file cannot be inspected.") from None
    raise FileExistsError("Publisher cancellation file must not already exist.")


def _validate_timeout(timeout: float) -> None:
    if type(timeout) is bool:
        raise ValueError("Publisher timeout must be finite and positive.")
    try:
        finite = math.isfinite(timeout)
    except OverflowError, TypeError:
        finite = False
    if not finite or timeout <= 0:
        raise ValueError("Publisher timeout must be finite and positive.")


def _create_cancel_file(path: Path) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    descriptor = os.open(path, flags, 0o600)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)


@dataclass(slots=True)
class _PublisherOwnership:
    process: subprocess.Popen[bytes] | None = None
    output: queue.Queue[object] | None = None
    reader_thread: threading.Thread | None = None
    reader_started: bool = False
    stream_eof: bool = False
    ownership_resolved: bool = False


def _emergency_drain_reap(
    ownership: _PublisherOwnership,
    cancellation_file: Path,
) -> None:
    """Request cooperative stop, then drain and reap without replacing errors."""

    process = ownership.process
    if process is None:
        return

    stop_attempted = False
    while not stop_attempted:
        try:
            _create_cancel_file(cancellation_file)
        except FileExistsError:
            stop_attempted = True
        except OSError:
            stop_attempted = True
        except BaseException:
            continue
        else:
            stop_attempted = True

    reader_thread = ownership.reader_thread
    reader_started = ownership.reader_started or (
        reader_thread is not None and reader_thread.ident is not None
    )
    output = ownership.output
    if reader_started and output is not None:
        eof_observed = ownership.stream_eof
        while not eof_observed:
            try:
                item = output.get(timeout=_POLL_SECONDS)
                if item is _STREAM_EOF:
                    eof_observed = True
                    ownership.stream_eof = True
            except BaseException:
                if (
                    reader_thread is not None
                    and not reader_thread.is_alive()
                    and output.empty()
                ):
                    eof_observed = True
                    ownership.stream_eof = True
                continue
    else:
        stdout = process.stdout
        if stdout is not None:
            eof_observed = ownership.stream_eof
            while not eof_observed:
                try:
                    chunk = stdout.read(_READ_SIZE)
                except BaseException:
                    continue
                if not chunk:
                    eof_observed = True
                    ownership.stream_eof = True

    reaped = False
    while not reaped:
        try:
            process.wait()
        except BaseException:
            continue
        reaped = True

    reader = reader_thread
    joined = not reader_started or reader is None
    while not joined and reader is not None:
        try:
            if not reader.is_alive():
                joined = True
            else:
                reader.join(timeout=_POLL_SECONDS)
        except BaseException:
            continue

    stdout = process.stdout
    if stdout is not None:
        closed = False
        while not closed:
            try:
                stdout.close()
            except BaseException:
                continue
            closed = True
    ownership.ownership_resolved = True


def run_publisher_bootstrap(
    command: list[str],
    *,
    cwd: Path,
    environment: Mapping[str, str],
    cancellation_file: Path,
    lifecycle: InstallationLifecycle,
    timeout: float = 3600.0,
) -> None:
    """Run a publisher bootstrap while retaining ownership through cleanup."""

    ownership = _PublisherOwnership()
    try:
        return _run_publisher_bootstrap_inner(
            command,
            cwd=cwd,
            environment=environment,
            cancellation_file=cancellation_file,
            lifecycle=lifecycle,
            timeout=timeout,
            ownership=ownership,
        )
    except BaseException:
        if ownership.process is not None and not ownership.ownership_resolved:
            while not ownership.ownership_resolved:
                try:
                    _emergency_drain_reap(ownership, cancellation_file)
                except BaseException:
                    continue
            with suppress(BaseException):
                lifecycle.advance(lifecycle.phase, cancellation_safe=True)
        if ownership.process is not None and not lifecycle.cancellation_safe:
            with suppress(BaseException):
                lifecycle.advance(lifecycle.phase, cancellation_safe=True)
        raise


def _run_publisher_bootstrap_inner(
    command: list[str],
    *,
    cwd: Path,
    environment: Mapping[str, str],
    cancellation_file: Path,
    lifecycle: InstallationLifecycle,
    ownership: _PublisherOwnership,
    timeout: float = 3600.0,
) -> None:
    """Run and bridge a publisher child without owning its terminal lifecycle."""

    _validate_timeout(timeout)
    _validate_control_path(cancellation_file)

    lifecycle.advance(lifecycle.phase, cancellation_safe=True)
    if lifecycle.cancellation_requested():
        raise InstallationCancelled()

    started = time.monotonic()
    ownership.process = subprocess.Popen(
        command,
        cwd=cwd,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=None,
        shell=False,
        bufsize=0,
    )
    process = ownership.process
    assert process is not None
    assert process.stdout is not None

    original_error: BaseException | None = None
    pending_cancel = False
    timeout_failure: PublisherBootstrapFailure | None = None
    stream_failure: PublisherBootstrapFailure | None = None
    protocol_failure: PublisherBootstrapFailure | None = None
    stop_attempted = False
    stop_file_created = False

    def preserve_error(error: BaseException) -> None:
        nonlocal original_error
        if original_error is None:
            original_error = error

    def request_stop() -> None:
        nonlocal stop_attempted, stop_file_created
        if stop_attempted:
            return
        stop_attempted = True
        try:
            _create_cancel_file(cancellation_file)
        except BaseException as error:
            preserve_error(error)
        else:
            stop_file_created = True

    reader_thread: threading.Thread | None = None
    output: queue.Queue[object] | None = None
    reader_started = False
    try:
        output = queue.Queue(maxsize=_QUEUE_SIZE)
        ownership.output = output
        reader_thread = threading.Thread(
            target=_pump_stdout,
            args=(process.stdout, output),
            name="carlina-publisher-stdout",
            daemon=True,
        )
        ownership.reader_thread = reader_thread
        reader_thread.start()
        reader_started = True
        ownership.reader_started = True
    except BaseException as error:
        preserve_error(error)
        if reader_thread is not None and reader_thread.ident is not None:
            reader_started = True
            ownership.reader_started = True
        request_stop()

    reader = PublisherProgressReader()
    pending = bytearray()
    stream_eof = False
    process_exited = False
    next_cancel_sample = started
    discard_output = not reader_started

    def malformed() -> None:
        nonlocal protocol_failure, discard_output
        if protocol_failure is None:
            protocol_failure = _incompatible()
        discard_output = True
        pending.clear()

    def accept_record(raw_record: bytes) -> None:
        nonlocal original_error
        if discard_output or original_error is not None:
            return
        phase = reader.feed(raw_record)
        if phase is None:
            return
        lifecycle.advance(
            phase.phase,
            cancellation_safe=phase.cancellation_safe,
            bytes_completed=phase.bytes_completed,
            bytes_total=phase.bytes_total,
        )

    def consume_chunk(chunk: bytes) -> None:
        nonlocal discard_output, protocol_failure
        offset = 0
        while offset < len(chunk) and not discard_output:
            newline = chunk.find(b"\n", offset)
            if newline < 0:
                pending.extend(chunk[offset:])
                if len(pending) > _MAX_RECORD_BYTES + 1 or (
                    len(pending) == _MAX_RECORD_BYTES + 1 and pending[-1] != ord("\r")
                ):
                    malformed()
                return
            pending.extend(chunk[offset:newline])
            pending.append(ord("\n"))
            raw_record = bytes(pending)
            pending.clear()
            try:
                accept_record(raw_record)
            except PublisherBootstrapFailure as error:
                protocol_failure = error
                malformed()
            except BaseException as error:
                preserve_error(error)
                request_stop()
                discard_output = True
                pending.clear()
            offset = newline + 1

    def sample_cancellation() -> None:
        nonlocal pending_cancel
        if original_error is not None or stop_attempted:
            return
        try:
            requested = lifecycle.cancellation_requested()
        except BaseException as error:
            preserve_error(error)
            request_stop()
            return
        if requested:
            pending_cancel = True
            request_stop()

    def process_chunk_value(item: object) -> None:
        nonlocal stream_failure
        if isinstance(item, bytes):
            consume_chunk(item)
        elif item is _READER_FAULT:
            if stream_failure is None:
                stream_failure = PublisherBootstrapFailure(
                    InstallationErrorCode.OPERATION_FAILED
                )
            request_stop()
        elif item is _STREAM_EOF:
            nonlocal stream_eof
            ownership.stream_eof = True
            stream_eof = True

    if reader_started and output is not None:
        while not (stream_eof and process_exited):
            try:
                now = time.monotonic()
                if now >= next_cancel_sample:
                    sample_cancellation()
                    next_cancel_sample = now + _POLL_SECONDS
                if (
                    original_error is None
                    and not stop_attempted
                    and timeout_failure is None
                    and now - started >= timeout
                ):
                    timeout_failure = PublisherBootstrapFailure(
                        InstallationErrorCode.OPERATION_FAILED
                    )
                    request_stop()
                try:
                    item = output.get(timeout=_POLL_SECONDS)
                except queue.Empty:
                    item = None
                except BaseException as error:
                    preserve_error(error)
                    request_stop()
                    discard_output = True
                    pending.clear()
                    item = None
                if item is not None:
                    try:
                        process_chunk_value(item)
                    except PublisherBootstrapFailure as error:
                        protocol_failure = error
                        malformed()
                    except BaseException as error:
                        preserve_error(error)
                        request_stop()
                        discard_output = True
                        pending.clear()
                if process.poll() is not None:
                    process_exited = True
                if (
                    not stream_eof
                    and reader_thread is not None
                    and not reader_thread.is_alive()
                    and output.empty()
                ):
                    ownership.stream_eof = True
                    stream_eof = True
            except BaseException as error:
                preserve_error(error)
                request_stop()
                discard_output = True
                pending.clear()

        try:
            process.wait()
        except BaseException as error:
            preserve_error(error)
            request_stop()
            while True:
                try:
                    process.wait()
                    break
                except BaseException as repeated_error:
                    preserve_error(repeated_error)
        if reader_thread is not None:
            while reader_thread.is_alive():
                try:
                    reader_thread.join(timeout=_POLL_SECONDS)
                except BaseException as error:
                    preserve_error(error)
                    request_stop()
    else:
        request_stop()
        while True:
            try:
                chunk = process.stdout.read(_READ_SIZE)
            except BaseException as error:
                preserve_error(error)
                continue
            if not chunk:
                ownership.stream_eof = True
                stream_eof = True
                break
        while True:
            try:
                if process.poll() is not None:
                    break
                time.sleep(_POLL_SECONDS)
            except BaseException as error:
                preserve_error(error)
        while True:
            try:
                process.wait()
                break
            except BaseException as error:
                preserve_error(error)

    ownership.ownership_resolved = True

    if pending and protocol_failure is None and reader_started:
        malformed()
    try:
        exit_code = process.returncode
        if protocol_failure is None and original_error is None and reader_started:
            result = reader.finish(exit_code)
        else:
            result = None
    except PublisherBootstrapFailure as error:
        protocol_failure = error
        result = None
    except BaseException as error:
        preserve_error(error)
        result = None

    try:
        lifecycle.advance(lifecycle.phase, cancellation_safe=True)
    except BaseException as error:
        preserve_error(error)

    try:
        process.stdout.close()
    except BaseException as error:
        preserve_error(error)

    if original_error is not None:
        raise original_error
    if protocol_failure is not None:
        raise protocol_failure
    if stream_failure is not None:
        raise stream_failure
    if result is None:
        raise _incompatible()
    if result.state is InstallationState.FAILED:
        assert result.error_code is not None
        raise PublisherBootstrapFailure(result.error_code)
    if pending_cancel:
        raise InstallationCancelled()
    if timeout_failure is not None:
        raise timeout_failure
    if result.state is InstallationState.CANCELLED:
        if not stop_file_created:
            raise _incompatible()
        raise _incompatible()


__all__ = [
    "PublisherBootstrapFailure",
    "PublisherPhase",
    "PublisherProgressReader",
    "PublisherResult",
    "run_publisher_bootstrap",
]
