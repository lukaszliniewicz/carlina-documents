"""Contract tests for the publisher-bootstrap protocol and process bridge."""

from __future__ import annotations

import os
import queue
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import cast

import pytest

from carlina.core import publisher_installation as publisher
from carlina.core.installation_lifecycle import (
    InstallationCancelled,
    InstallationErrorCode,
    InstallationEvent,
    InstallationLifecycle,
    InstallationPhase,
    InstallationState,
)

pytestmark = [pytest.mark.fast, pytest.mark.contract]

_PREFIX = "carlina.publisher-bootstrap/1"
_MESSAGE = "Publisher bootstrap did not complete."
_ENVIRONMENT = {"PYTHONUNBUFFERED": "1"}
if "SYSTEMROOT" in os.environ:
    _ENVIRONMENT["SYSTEMROOT"] = os.environ["SYSTEMROOT"]


def _line(text: str, *, crlf: bool = False) -> bytes:
    return (text + ("\r\n" if crlf else "\n")).encode("ascii")


def _reader(*records: str) -> publisher.PublisherProgressReader:
    reader = publisher.PublisherProgressReader()
    for record in records:
        reader.feed(_line(record))
    return reader


def _script(*lines: str) -> str:
    return "\n".join(lines)


def _lifecycle(
    *,
    events: list[InstallationEvent] | None = None,
    cancel_requested: Callable[[], bool] | None = None,
) -> InstallationLifecycle:
    return InstallationLifecycle(
        "update",
        "documents",
        event_sink=events.append if events is not None else None,
        cancel_requested=cancel_requested,
    )


def _run(
    tmp_path: Path,
    source: str,
    *,
    lifecycle: InstallationLifecycle | None = None,
    cancellation_file: Path | None = None,
    timeout: float = 3600.0,
) -> None:
    selected_cancel_file = cancellation_file or (tmp_path / "publisher.cancel")
    publisher.run_publisher_bootstrap(
        [sys.executable, "-c", source, str(selected_cancel_file), str(tmp_path)],
        cwd=tmp_path,
        environment=_ENVIRONMENT,
        cancellation_file=selected_cancel_file,
        lifecycle=lifecycle or _lifecycle(),
        timeout=timeout,
    )


def _wait_for(path: Path, timeout: float = 4.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.01)
    return path.exists()


@pytest.mark.parametrize("exception_type", [KeyboardInterrupt, RuntimeError])
def test_parser_constructor_error_does_not_escape_child_ownership(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exception_type: type[BaseException],
) -> None:
    cancel_file = tmp_path / "publisher.cancel"
    release = tmp_path / "release"
    child_seen = tmp_path / "child-saw-cancel"
    source = _script(
        "import pathlib, sys, time",
        "cancel, root = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])",
        "candidate = root / 'publisher-candidate'",
        "candidate.write_text('staged')",
        f"print('{_PREFIX} ready', flush=True)",
        f"print('{_PREFIX} phase unpacking false null null', flush=True)",
        "deadline = time.monotonic() + 15",
        "while not cancel.exists() and time.monotonic() < deadline: time.sleep(0.01)",
        "if not cancel.exists(): raise SystemExit(31)",
        "(root / 'child-saw-cancel').write_text(candidate.read_text())",
        "while not (root / 'release').exists() and time.monotonic() < deadline: time.sleep(0.01)",
        "if not (root / 'release').exists(): raise SystemExit(32)",
        f"print('{_PREFIX} result cancelled cancelled true', flush=True)",
        "raise SystemExit(130)",
    )
    process: subprocess.Popen[bytes] | None = None
    reader_thread: threading.Thread | None = None
    reader_queue: queue.Queue[object] | None = None
    original_popen = cast(
        Callable[..., subprocess.Popen[bytes]], publisher.subprocess.Popen
    )
    original_thread_factory = cast(
        Callable[..., threading.Thread], publisher.threading.Thread
    )
    original_reader = publisher.PublisherProgressReader
    tripwire = exception_type("parser constructor tripwire")
    wait_calls = 0
    interrupted = False

    def release_after_child_observes_stop() -> None:
        assert _wait_for(cancel_file, timeout=8.0)
        assert _wait_for(child_seen, timeout=8.0)
        release.touch()

    releaser = original_thread_factory(
        target=release_after_child_observes_stop, daemon=True
    )
    releaser.start()

    def capture_popen(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        nonlocal process
        process = original_popen(*args, **kwargs)
        original_wait = process.wait

        def track_wait(timeout: float | None = None) -> int:
            nonlocal wait_calls
            wait_calls += 1
            return original_wait(timeout=timeout)

        process.wait = track_wait
        return process

    def capture_thread(*args: object, **kwargs: object) -> threading.Thread:
        nonlocal reader_thread, reader_queue
        thread = original_thread_factory(*args, **kwargs)
        if thread.name == "carlina-publisher-stdout":
            reader_thread = thread
            thread_args = cast(tuple[object, ...], kwargs.get("args", ()))
            if len(thread_args) == 2:
                reader_queue = cast(queue.Queue[object], thread_args[1])
        return thread

    def interrupt_parser_constructor() -> publisher.PublisherProgressReader:
        nonlocal interrupted
        assert process is not None
        if not interrupted:
            interrupted = True
            raise tripwire
        return original_reader()

    monkeypatch.setattr(publisher.subprocess, "Popen", capture_popen)
    monkeypatch.setattr(publisher.threading, "Thread", capture_thread)
    monkeypatch.setattr(
        publisher, "PublisherProgressReader", interrupt_parser_constructor
    )
    lifecycle = _lifecycle()

    try:
        with pytest.raises(exception_type) as error:
            _run(tmp_path, source, lifecycle=lifecycle, cancellation_file=cancel_file)

        assert process is not None
        assert reader_thread is not None
        assert reader_queue is not None
        assert error.value is tripwire
        assert child_seen.read_text() == "staged"
        assert (tmp_path / "publisher-candidate").read_text() == "staged"
        assert wait_calls >= 1
        assert process.poll() == 130
        assert not reader_thread.is_alive()
        assert reader_queue.empty()
        assert cancel_file.read_bytes() == b""
        if os.name != "nt":
            assert stat.S_IMODE(cancel_file.stat().st_mode) == 0o600
        assert lifecycle.cancellation_safe is True
    finally:
        with suppress(FileExistsError):
            descriptor = os.open(
                cancel_file,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
            os.close(descriptor)
        assert _wait_for(child_seen, timeout=8.0)
        release.touch()
        if process is not None:
            process.wait(timeout=8.0)
        if reader_thread is not None and reader_thread.ident is not None:
            drain_deadline = time.monotonic() + 8.0
            while time.monotonic() < drain_deadline:
                if not reader_thread.is_alive() and (
                    reader_queue is None or reader_queue.empty()
                ):
                    break
                if reader_queue is None:
                    break
                try:
                    reader_queue.get(timeout=0.05)
                except queue.Empty:
                    if not reader_thread.is_alive():
                        break
                except BaseException:
                    continue
            while reader_thread.is_alive() and time.monotonic() < drain_deadline:
                try:
                    reader_thread.join(timeout=0.05)
                except BaseException:
                    continue
            assert not reader_thread.is_alive()
        elif process is not None and process.stdout is not None:
            while process.stdout.read(4097):
                pass
        releaser.join(timeout=8.0)
        assert not releaser.is_alive()


@pytest.mark.parametrize(
    ("records", "exit_code", "state", "error_code", "retryable"),
    [
        (
            [f"{_PREFIX} ready", f"{_PREFIX} result succeeded null false"],
            0,
            InstallationState.SUCCEEDED,
            None,
            False,
        ),
        (
            [f"{_PREFIX} ready", f"{_PREFIX} result failed download_failed true"],
            9,
            InstallationState.FAILED,
            InstallationErrorCode.DOWNLOAD_FAILED,
            True,
        ),
        (
            [f"{_PREFIX} ready", f"{_PREFIX} result failed verification_failed false"],
            130,
            InstallationState.FAILED,
            InstallationErrorCode.VERIFICATION_FAILED,
            False,
        ),
        (
            [f"{_PREFIX} ready", f"{_PREFIX} result cancelled cancelled true"],
            130,
            InstallationState.CANCELLED,
            InstallationErrorCode.CANCELLED,
            True,
        ),
    ],
)
def test_reader_accepts_terminal_vectors(
    records: list[str],
    exit_code: int,
    state: InstallationState,
    error_code: InstallationErrorCode | None,
    retryable: bool,
) -> None:
    result = _reader(*records).finish(exit_code)
    assert result.state is state
    assert result.error_code is error_code
    assert result.retryable is retryable


def test_reader_accepts_crlf_and_download_progress() -> None:
    reader = publisher.PublisherProgressReader()
    assert reader.feed(_line(f"{_PREFIX} ready", crlf=True)) is None
    assert reader.feed(_line(f"{_PREFIX} phase download true 0 99", crlf=True)) == (
        publisher.PublisherPhase(InstallationPhase.DOWNLOAD, True, 0, 99)
    )


@pytest.mark.parametrize(
    "record",
    [
        b"",
        b"\n",
        f"{_PREFIX} ready".encode(),
        f"{_PREFIX}  ready\n".encode(),
        f"{_PREFIX} ready extra\n".encode(),
        f"{_PREFIX}\tready\n".encode(),
        f"{_PREFIX} ready\x00\n".encode(),
        f"{_PREFIX} ready \N{SNOWMAN}\n".encode(),
        f"{_PREFIX} ready\r\nextra\n".encode(),
    ],
)
def test_reader_rejects_malformed_records_with_fixed_diagnostic(record: bytes) -> None:
    with pytest.raises(publisher.PublisherBootstrapFailure) as error:
        publisher.PublisherProgressReader().feed(record)
    assert str(error.value) == _MESSAGE
    assert error.value.error_code is InstallationErrorCode.INCOMPATIBLE_RELEASE


@pytest.mark.parametrize(
    "record",
    [
        f"{_PREFIX} phase other true null null",
        f"{_PREFIX} phase download True null null",
        f"{_PREFIX} phase download true null 1",
        f"{_PREFIX} phase download true 1 null",
        f"{_PREFIX} phase preflight true 0 1",
        f"{_PREFIX} phase download true 1 0",
        f"{_PREFIX} phase download true 2 1",
        f"{_PREFIX} phase download true 01 10",
        f"{_PREFIX} phase download true +1 10",
        f"{_PREFIX} phase download true 1 1000000000000000",
        f"{_PREFIX} phase download true 1 2 extra",
        f"{_PREFIX} phase download true  2",
    ],
)
def test_reader_rejects_invalid_phase_records(record: str) -> None:
    reader = _reader(f"{_PREFIX} ready")
    with pytest.raises(publisher.PublisherBootstrapFailure):
        reader.feed(_line(record))


@pytest.mark.parametrize(
    "record",
    [
        f"{_PREFIX} result succeeded null true",
        f"{_PREFIX} result succeeded operation_failed false",
        f"{_PREFIX} result succeeded null false extra",
        f"{_PREFIX} result cancelled cancelled false",
        f"{_PREFIX} result failed download_failed false",
        f"{_PREFIX} result failed verification_failed true",
        f"{_PREFIX} result failed operation_failed true",
        f"{_PREFIX} result failed incompatible_release false",
        f"{_PREFIX} result failed cancelled false",
        f"{_PREFIX} result running null false",
    ],
)
def test_reader_rejects_invalid_result_combinations(record: str) -> None:
    reader = _reader(f"{_PREFIX} ready")
    with pytest.raises(publisher.PublisherBootstrapFailure):
        reader.feed(_line(record))


@pytest.mark.parametrize(
    ("records", "exit_code"),
    [
        ([f"{_PREFIX} phase download true null null"], 0),
        ([f"{_PREFIX} ready", f"{_PREFIX} ready"], 0),
        ([f"{_PREFIX} ready", f"{_PREFIX} result succeeded null false"], 1),
        ([f"{_PREFIX} ready", f"{_PREFIX} result cancelled cancelled true"], 0),
    ],
)
def test_reader_rejects_order_duplicate_and_exit_mismatch(
    records: list[str], exit_code: int
) -> None:
    reader = publisher.PublisherProgressReader()
    failed = False
    for record in records:
        try:
            reader.feed(_line(record))
        except publisher.PublisherBootstrapFailure:
            failed = True
            break
    if not failed:
        with pytest.raises(publisher.PublisherBootstrapFailure):
            reader.finish(exit_code)


def test_reader_rejects_after_result_oversize_and_bool_exit() -> None:
    reader = _reader(f"{_PREFIX} ready", f"{_PREFIX} result succeeded null false")
    with pytest.raises(publisher.PublisherBootstrapFailure):
        reader.feed(_line(f"{_PREFIX} ready"))
    with pytest.raises(publisher.PublisherBootstrapFailure):
        publisher.PublisherProgressReader().feed(b"x" * 4097 + b"\n")
    with pytest.raises(publisher.PublisherBootstrapFailure):
        _reader(f"{_PREFIX} ready", f"{_PREFIX} result succeeded null false").finish(
            cast(int, True)
        )


def test_public_records_are_frozen_and_failure_is_privacy_safe() -> None:
    phase = publisher.PublisherPhase(InstallationPhase.DOWNLOAD, True, 1, 2)
    result = publisher.PublisherResult(InstallationState.SUCCEEDED, None, False)
    with pytest.raises(FrozenInstanceError):
        phase.__setattr__("bytes_completed", 2)
    with pytest.raises(FrozenInstanceError):
        result.__setattr__("retryable", True)
    error = publisher.PublisherBootstrapFailure(InstallationErrorCode.DOWNLOAD_FAILED)
    assert str(error) == _MESSAGE
    assert error.retryable is True
    with pytest.raises(ValueError):
        publisher.PublisherBootstrapFailure(InstallationErrorCode.CANCELLED)


def test_runner_streams_substeps_with_one_parent_id_and_no_terminal_event(
    tmp_path: Path,
) -> None:
    events: list[InstallationEvent] = []
    source = _script(
        f"print('{_PREFIX} ready', flush=True)",
        f"print('{_PREFIX} phase preflight true null null', flush=True)",
        f"print('{_PREFIX} phase download false 3 9', flush=True)",
        f"print('{_PREFIX} result succeeded null false', flush=True)",
    )
    _run(tmp_path, source, lifecycle=_lifecycle(events=events))
    assert [event.sequence for event in events] == list(
        range(events[0].sequence, events[0].sequence + len(events))
    )
    assert len({event.operation_id for event in events}) == 1
    assert all(event.state is InstallationState.RUNNING for event in events)
    assert events[-1].bytes_completed is None
    assert events[-1].cancellation_safe is True
    assert events[2].bytes_completed == 3
    assert events[2].bytes_total == 9


def test_runner_observes_pending_cancel_at_initial_safe_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[InstallationEvent] = []

    def fail_spawn(*args: object, **kwargs: object) -> object:
        raise AssertionError("child must not start after an initial cancellation")

    monkeypatch.setattr(publisher.subprocess, "Popen", fail_spawn)
    lifecycle = _lifecycle(events=events, cancel_requested=lambda: True)

    with pytest.raises(InstallationCancelled):
        _run(tmp_path, "raise SystemExit(0)", lifecycle=lifecycle)

    assert len(events) == 1
    assert events[0].phase is InstallationPhase.PREFLIGHT
    assert events[0].cancellation_safe is True
    assert not (tmp_path / "publisher.cancel").exists()


def test_cancel_during_unsafe_phase_waits_for_child_and_uses_private_control(
    tmp_path: Path,
) -> None:
    trigger = tmp_path / "trigger"
    release = tmp_path / "release"
    seen = tmp_path / "child-saw-cancel"
    cancel_file = tmp_path / "publisher.cancel"
    events: list[InstallationEvent] = []
    observed_unsafe: list[bool] = []
    source = _script(
        "import pathlib, sys, time",
        "cancel, root = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])",
        f"print('{_PREFIX} ready', flush=True)",
        f"print('{_PREFIX} phase unpacking false null null', flush=True)",
        "deadline = time.monotonic() + 5",
        "while not cancel.exists() and time.monotonic() < deadline: time.sleep(0.01)",
        "if not cancel.exists(): raise SystemExit(7)",
        "(root / 'child-saw-cancel').write_text(str(cancel.stat().st_mode & 0o777))",
        "while not (root / 'release').exists() and time.monotonic() < deadline: time.sleep(0.01)",
        "if not (root / 'release').exists(): raise SystemExit(8)",
        f"print('{_PREFIX} result cancelled cancelled true', flush=True)",
        "raise SystemExit(130)",
    )
    lifecycle: InstallationLifecycle

    def callback() -> bool:
        observed_unsafe.append(not lifecycle.cancellation_safe)
        return trigger.exists()

    lifecycle = _lifecycle(events=events, cancel_requested=callback)

    def request_after_unsafe_phase() -> None:
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            if any(
                event.phase is InstallationPhase.UNPACKING
                and not event.cancellation_safe
                for event in events
            ):
                trigger.touch()
                return
            time.sleep(0.01)

    def release_child() -> None:
        assert _wait_for(cancel_file)
        assert _wait_for(seen)
        release.touch()

    releaser = threading.Thread(target=release_child, daemon=True)
    triggerer = threading.Thread(target=request_after_unsafe_phase, daemon=True)
    releaser.start()
    triggerer.start()
    with pytest.raises(InstallationCancelled):
        _run(tmp_path, source, lifecycle=lifecycle, cancellation_file=cancel_file)
    releaser.join(timeout=2)
    triggerer.join(timeout=2)
    assert not releaser.is_alive()
    assert not triggerer.is_alive()
    assert any(observed_unsafe)
    assert int(seen.read_text()) == stat.S_IMODE(cancel_file.stat().st_mode)
    assert cancel_file.read_bytes() == b""
    if os.name != "nt":
        assert stat.S_IMODE(cancel_file.stat().st_mode) == 0o600
    assert all(event.state is InstallationState.RUNNING for event in events)
    assert events[-1].cancellation_safe is True


def test_late_cancel_after_valid_success_is_honored(tmp_path: Path) -> None:
    trigger = tmp_path / "trigger"
    result_ready = tmp_path / "result-ready"
    cancel_file = tmp_path / "publisher.cancel"
    source = _script(
        "import pathlib, sys, time",
        "cancel, root = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])",
        f"print('{_PREFIX} ready', flush=True)",
        f"print('{_PREFIX} result succeeded null false', flush=True)",
        "(root / 'result-ready').touch()",
        "deadline = time.monotonic() + 5",
        "while not cancel.exists() and time.monotonic() < deadline: time.sleep(0.01)",
        "if not cancel.exists(): raise SystemExit(9)",
        "(root / 'child-saw-cancel').touch()",
    )
    lifecycle = _lifecycle(cancel_requested=lambda: trigger.exists())

    def trigger_after_result() -> None:
        assert _wait_for(result_ready)
        trigger.touch()

    triggerer = threading.Thread(target=trigger_after_result, daemon=True)
    triggerer.start()
    with pytest.raises(InstallationCancelled):
        _run(tmp_path, source, lifecycle=lifecycle, cancellation_file=cancel_file)
    triggerer.join(timeout=2)
    assert not triggerer.is_alive()
    assert (tmp_path / "child-saw-cancel").exists()
    assert cancel_file.read_bytes() == b""


@pytest.mark.parametrize(
    ("state", "code", "retryable", "exit_code", "expected"),
    [
        ("failed", "download_failed", "true", 1, InstallationErrorCode.DOWNLOAD_FAILED),
        (
            "failed",
            "verification_failed",
            "false",
            9,
            InstallationErrorCode.VERIFICATION_FAILED,
        ),
        (
            "failed",
            "operation_failed",
            "false",
            130,
            InstallationErrorCode.OPERATION_FAILED,
        ),
    ],
)
def test_runner_preserves_valid_failure_code_and_exit_130(
    tmp_path: Path,
    state: str,
    code: str,
    retryable: str,
    exit_code: int,
    expected: InstallationErrorCode,
) -> None:
    source = _script(
        "import os",
        f"print('{_PREFIX} ready', flush=True)",
        f"print('{_PREFIX} result {state} {code} {retryable}', flush=True)",
        f"os._exit({exit_code})",
    )
    with pytest.raises(publisher.PublisherBootstrapFailure) as error:
        _run(tmp_path, source)
    assert error.value.error_code is expected
    assert error.value.retryable is (expected is InstallationErrorCode.DOWNLOAD_FAILED)


def test_cancelled_result_without_a_stop_request_is_incompatible(
    tmp_path: Path,
) -> None:
    source = _script(
        "import sys",
        f"print('{_PREFIX} ready', flush=True)",
        f"print('{_PREFIX} result cancelled cancelled true', flush=True)",
        "raise SystemExit(130)",
    )
    with pytest.raises(publisher.PublisherBootstrapFailure) as error:
        _run(tmp_path, source)
    assert error.value.error_code is InstallationErrorCode.INCOMPATIBLE_RELEASE


@pytest.mark.parametrize(
    ("records", "exit_code"),
    [
        ([f"{_PREFIX} ready", f"{_PREFIX} result succeeded null false"], 1),
        ([f"{_PREFIX} ready"], 0),
        (["private-url-token"], 0),
    ],
)
def test_protocol_or_exit_failure_drains_child_before_raise(
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
    records: list[str],
    exit_code: int,
) -> None:
    marker = tmp_path / "child-cleaned"
    source = _script(
        "import pathlib, sys, time",
        "root = pathlib.Path(sys.argv[2])",
        "sys.stdout.write('\\n'.join("
        + repr(records)
        + ") + ('\\n' if "
        + str(records)
        + " else ''))",
        "sys.stdout.flush()",
        "time.sleep(0.06)",
        "(root / 'child-cleaned').write_text('done')",
        f"raise SystemExit({exit_code})",
    )
    with pytest.raises(publisher.PublisherBootstrapFailure) as error:
        _run(tmp_path, source)
    captured = capfd.readouterr()
    assert error.value.error_code is InstallationErrorCode.INCOMPATIBLE_RELEASE
    assert str(error.value) == _MESSAGE
    assert marker.read_text() == "done"
    assert "private-url-token" not in str(error.value)
    assert captured.out == ""


@pytest.mark.parametrize("failure_type", [KeyboardInterrupt, SystemExit])
def test_observer_baseexception_is_deferred_until_child_cleanup(
    tmp_path: Path, failure_type: type[BaseException]
) -> None:
    cancel_file = tmp_path / "publisher.cancel"
    marker = tmp_path / "child-cleaned"
    raised = False
    source = _script(
        "import pathlib, sys, time",
        "cancel, root = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])",
        f"print('{_PREFIX} ready', flush=True)",
        f"print('{_PREFIX} phase download false 1 2', flush=True)",
        "deadline = time.monotonic() + 5",
        "while not cancel.exists() and time.monotonic() < deadline: time.sleep(0.01)",
        "if not cancel.exists(): raise SystemExit(10)",
        "(root / 'child-cleaned').write_text('done')",
        f"print('{_PREFIX} result cancelled cancelled true', flush=True)",
        "raise SystemExit(130)",
    )

    def observer(event: InstallationEvent) -> None:
        nonlocal raised
        if event.phase is InstallationPhase.DOWNLOAD and not raised:
            raised = True
            raise failure_type("private observer detail")

    lifecycle = InstallationLifecycle("update", "documents", event_sink=observer)
    with pytest.raises(failure_type, match="private observer detail"):
        _run(tmp_path, source, lifecycle=lifecycle, cancellation_file=cancel_file)
    assert marker.read_text() == "done"
    assert cancel_file.read_bytes() == b""
    assert lifecycle.cancellation_safe is True


def test_callback_error_is_deferred_until_child_cleanup(tmp_path: Path) -> None:
    cancel_file = tmp_path / "publisher.cancel"
    marker = tmp_path / "child-cleaned"
    call_count = 0
    source = _script(
        "import pathlib, sys, time",
        "cancel, root = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])",
        f"print('{_PREFIX} ready', flush=True)",
        f"print('{_PREFIX} phase download false null null', flush=True)",
        "deadline = time.monotonic() + 5",
        "while not cancel.exists() and time.monotonic() < deadline: time.sleep(0.01)",
        "if not cancel.exists(): raise SystemExit(11)",
        "(root / 'child-cleaned').write_text('done')",
        f"print('{_PREFIX} result cancelled cancelled true', flush=True)",
        "raise SystemExit(130)",
    )

    def callback() -> bool:
        nonlocal call_count
        call_count += 1
        if call_count >= 3:
            raise RuntimeError("private callback detail")
        return False

    lifecycle = _lifecycle(cancel_requested=callback)
    with pytest.raises(RuntimeError, match="private callback detail"):
        _run(tmp_path, source, lifecycle=lifecycle, cancellation_file=cancel_file)
    assert marker.read_text() == "done"
    assert lifecycle.cancellation_safe is True


def test_keyboard_interrupt_during_queue_wait_is_deferred(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cancel_file = tmp_path / "publisher.cancel"
    marker = tmp_path / "child-cleaned"
    source = _script(
        "import pathlib, sys, time",
        "cancel, root = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])",
        f"print('{_PREFIX} ready', flush=True)",
        "deadline = time.monotonic() + 5",
        "while not cancel.exists() and time.monotonic() < deadline: time.sleep(0.01)",
        "if not cancel.exists(): raise SystemExit(12)",
        "(root / 'child-cleaned').write_text('done')",
        f"print('{_PREFIX} result cancelled cancelled true', flush=True)",
        "raise SystemExit(130)",
    )
    original_get = cast(Callable[..., object], publisher.queue.Queue.get)
    first = True

    def interrupt_first_wait(
        self: queue.Queue[object], block: bool = True, timeout: float | None = None
    ) -> object:
        nonlocal first
        if first:
            first = False
            raise KeyboardInterrupt("synthetic queue interrupt")
        return original_get(self, block, timeout)

    monkeypatch.setattr(publisher.queue.Queue, "get", interrupt_first_wait)
    lifecycle = _lifecycle()
    with pytest.raises(KeyboardInterrupt, match="synthetic queue interrupt"):
        _run(tmp_path, source, lifecycle=lifecycle, cancellation_file=cancel_file)
    assert marker.read_text() == "done"
    assert lifecycle.cancellation_safe is True


def test_timeout_is_a_stop_request_and_waits_for_atomic_step(tmp_path: Path) -> None:
    cancel_file = tmp_path / "publisher.cancel"
    seen = tmp_path / "child-saw-cancel"
    release = tmp_path / "release"
    source = _script(
        "import pathlib, sys, time",
        "cancel, root = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])",
        f"print('{_PREFIX} ready', flush=True)",
        f"print('{_PREFIX} phase unpacking false null null', flush=True)",
        "deadline = time.monotonic() + 5",
        "while not cancel.exists() and time.monotonic() < deadline: time.sleep(0.01)",
        "if not cancel.exists(): raise SystemExit(13)",
        "(root / 'child-saw-cancel').touch()",
        "while not (root / 'release').exists() and time.monotonic() < deadline: time.sleep(0.01)",
        "if not (root / 'release').exists(): raise SystemExit(14)",
        f"print('{_PREFIX} result cancelled cancelled true', flush=True)",
        "raise SystemExit(130)",
    )

    def release_after_observation() -> None:
        assert _wait_for(seen)
        time.sleep(0.18)
        release.touch()

    releaser = threading.Thread(target=release_after_observation, daemon=True)
    releaser.start()
    started = time.monotonic()
    with pytest.raises(publisher.PublisherBootstrapFailure) as error:
        _run(tmp_path, source, cancellation_file=cancel_file, timeout=0.15)
    elapsed = time.monotonic() - started
    releaser.join(timeout=2)
    assert error.value.error_code is InstallationErrorCode.OPERATION_FAILED
    assert elapsed >= 0.25
    assert not releaser.is_alive()
    assert seen.exists()
    assert cancel_file.read_bytes() == b""


def test_child_stderr_is_separate_from_child_stdout(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    source = _script(
        "import sys",
        "print('child-stderr-only', file=sys.stderr, flush=True)",
        "sys.stdout.write('sensitive stdout payload\\n')",
        "sys.stdout.flush()",
    )
    with pytest.raises(publisher.PublisherBootstrapFailure):
        _run(tmp_path, source)
    captured = capfd.readouterr()
    assert captured.out == ""
    assert "child-stderr-only" in captured.err
    assert "sensitive stdout payload" not in captured.out


def test_preexisting_cancel_file_is_not_clobbered_and_child_is_not_spawned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cancel_file = tmp_path / "publisher.cancel"
    cancel_file.write_bytes(b"sentinel")
    cancel_file.chmod(0o640)
    original_mode = stat.S_IMODE(cancel_file.stat().st_mode)
    spawned = False

    def fail_spawn(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        nonlocal spawned
        spawned = True
        raise AssertionError("preexisting cancellation file must reject before spawn")

    monkeypatch.setattr(publisher.subprocess, "Popen", fail_spawn)
    with pytest.raises(FileExistsError):
        _run(tmp_path, "raise SystemExit(0)", cancellation_file=cancel_file)
    assert not spawned
    assert cancel_file.read_bytes() == b"sentinel"
    assert stat.S_IMODE(cancel_file.stat().st_mode) == original_mode


def test_control_file_race_uses_exclusive_create_and_waits_for_child(
    tmp_path: Path,
) -> None:
    trigger = tmp_path / "trigger"
    cancel_file = tmp_path / "publisher.cancel"
    marker = tmp_path / "child-cleaned"
    source = _script(
        "import pathlib, sys, time",
        "cancel, root = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])",
        "cancel.write_bytes(b'sentinel')",
        "cancel.chmod(0o640)",
        f"print('{_PREFIX} ready', flush=True)",
        "deadline = time.monotonic() + 5",
        "while not (root / 'trigger').exists() and time.monotonic() < deadline: time.sleep(0.01)",
        "time.sleep(0.2)",
        "(root / 'child-cleaned').write_text('done')",
        f"print('{_PREFIX} result succeeded null false', flush=True)",
    )

    def trigger_later() -> None:
        time.sleep(0.12)
        trigger.touch()

    triggerer = threading.Thread(target=trigger_later, daemon=True)
    triggerer.start()
    lifecycle = _lifecycle(cancel_requested=lambda: trigger.exists())
    with pytest.raises(FileExistsError):
        _run(tmp_path, source, lifecycle=lifecycle, cancellation_file=cancel_file)
    triggerer.join(timeout=2)
    assert not triggerer.is_alive()
    assert marker.read_text() == "done"
    assert cancel_file.read_bytes() == b"sentinel"
    if os.name != "nt":
        assert stat.S_IMODE(cancel_file.stat().st_mode) == 0o640
    assert lifecycle.cancellation_safe is True


def test_reader_start_failure_stops_drains_and_reaps_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cancel_file = tmp_path / "publisher.cancel"
    marker = tmp_path / "child-cleaned"
    source = _script(
        "import pathlib, sys, time",
        "cancel, root = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])",
        f"print('{_PREFIX} ready', flush=True)",
        "deadline = time.monotonic() + 5",
        "while not cancel.exists() and time.monotonic() < deadline: time.sleep(0.01)",
        "if not cancel.exists(): raise SystemExit(15)",
        "(root / 'child-cleaned').write_text('done')",
        f"print('{_PREFIX} result cancelled cancelled true', flush=True)",
        "raise SystemExit(130)",
    )
    original_start = threading.Thread.start

    def fail_reader_start(thread: threading.Thread) -> None:
        if thread.name == "carlina-publisher-stdout":
            raise RuntimeError("synthetic reader start failure")
        original_start(thread)

    monkeypatch.setattr(publisher.threading.Thread, "start", fail_reader_start)
    lifecycle = _lifecycle()
    with pytest.raises(RuntimeError, match="synthetic reader start failure"):
        _run(tmp_path, source, lifecycle=lifecycle, cancellation_file=cancel_file)
    assert marker.read_text() == "done"
    assert cancel_file.read_bytes() == b""
    assert lifecycle.cancellation_safe is True


def test_popen_failure_propagates_before_a_child_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_spawn(*args: object, **kwargs: object) -> object:
        raise OSError("synthetic spawn failure")

    monkeypatch.setattr(publisher.subprocess, "Popen", fail_spawn)
    with pytest.raises(OSError, match="synthetic spawn failure"):
        _run(tmp_path, "raise SystemExit(0)")


@pytest.mark.parametrize("timeout", [True, False, 0, -1, float("inf"), float("nan")])
def test_runner_rejects_invalid_timeout_before_spawning(
    tmp_path: Path, timeout: float
) -> None:
    with pytest.raises(ValueError, match="finite and positive"):
        _run(tmp_path, "raise SystemExit(0)", timeout=timeout)


def test_signal_exit_is_a_nonzero_child_failure_not_cooperative_cancel(
    tmp_path: Path,
) -> None:
    if os.name == "nt":
        pytest.skip("POSIX signal status is not Windows acceptance evidence")
    source = _script(
        "import os, signal",
        f"print('{_PREFIX} ready', flush=True)",
        f"print('{_PREFIX} result failed operation_failed false', flush=True)",
        "os.kill(os.getpid(), signal.SIGTERM)",
    )
    with pytest.raises(publisher.PublisherBootstrapFailure) as error:
        _run(tmp_path, source)
    assert error.value.error_code is InstallationErrorCode.OPERATION_FAILED
