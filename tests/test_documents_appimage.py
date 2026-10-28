"""Parent-owned setup adapter tests use synthetic, isolated installer processes."""

from __future__ import annotations

import json
import os
import queue
import threading
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

from carlina.core.installation_lifecycle import InstallationLifecycle
from scripts import documents_appimage as image


def _event(operation: str, *, state: str = "running", sequence: int = 1) -> bytes:
    return (
        json.dumps(
            {
                "format": "carlina.installation-event",
                "schema_version": 1,
                "operation_id": operation,
                "sequence": sequence,
                "operation": "install",
                "application_profile": "documents",
                "phase": "preflight",
                "state": state,
                "bytes_completed": None,
                "bytes_total": None,
                "error_code": "cancelled" if state == "cancelled" else None,
                "retryable": state == "cancelled",
                "cancellation_safe": True,
            }
        )
        + "\n"
    ).encode()


@pytest.mark.parametrize(
    "field,value",
    [
        ("operation_id", "f" * 32),
        ("sequence", 2),
        ("application_profile", "workbench"),
        ("schema_version", True),
        ("phase", "unknown"),
        ("bytes_completed", True),
    ],
)
def test_progress_cannot_switch_operation_or_profile(field: str, value: object) -> None:
    record = json.loads(_event("a" * 32))
    record[field] = value
    with pytest.raises(image.LauncherError):
        image.validate_event((json.dumps(record) + "\n").encode(), "a" * 32, 1)


def test_valid_progress_and_complete_frame() -> None:
    record = image.validate_event(_event("a" * 32), "a" * 32, 1)
    assert record["state"] == "running"
    with pytest.raises(image.LauncherError):
        image.validate_event(_event("a" * 32).rstrip(), "a" * 32, 1)


def test_shared_producer_records_are_accepted_without_reencoding() -> None:
    events = []
    lifecycle = InstallationLifecycle(
        "install", "documents", operation_id="a" * 32, event_sink=events.append
    )
    lifecycle.advance("preflight", cancellation_safe=True)
    assert len(events) == 1
    image.validate_event(events[0].json_line().encode(), "a" * 32, 1)


def test_preparation_state_does_not_prevent_explicit_native_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller = _controller(tmp_path, monkeypatch)
    (controller.program_root / "state").mkdir(parents=True)
    witness = controller.program_root / "state" / "retained-evidence"
    witness.write_bytes(b"retained")
    assert controller._probe() == "install"
    assert witness.read_bytes() == b"retained"
    (controller.program_root / "carlina").touch()
    with pytest.raises(image.LauncherError):
        controller._probe()


def _controller(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> image.DocumentsSetup:
    controller = object.__new__(image.DocumentsSetup)
    controller.appdir = tmp_path
    controller.program_root = tmp_path / "program"
    controller.events = queue.SimpleQueue()
    controller.cancel = threading.Event()
    monkeypatch.setenv("CARLINA_HOME", str(tmp_path / "synthetic-data"))
    return controller


def test_open_does_not_reuse_a_foreign_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller = _controller(tmp_path, monkeypatch)
    monkeypatch.setattr(
        controller,
        "_json_command",
        lambda command: {"running": True, "managed_process": False},
    )
    start = Mock()
    monkeypatch.setattr(image.subprocess, "Popen", start)
    with pytest.raises(image.LauncherError, match="Another runtime"):
        controller._start()
    start.assert_not_called()


@pytest.mark.parametrize("profile", ["documents", "workbench", None])
def test_installed_profile_is_explicit_despite_shared_framework_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, profile: str | None
) -> None:
    controller = _controller(tmp_path, monkeypatch)
    controller.program_root.mkdir()
    controller.launcher.write_text("synthetic installed launcher")
    controller.launcher.chmod(0o700)
    reports = {
        "status": {
            "product": "Carlina",
            "application_profile": profile,
            "managed_install": {"program_root": str(controller.program_root)},
        },
        "doctor": {"checks": [{"id": "accounts.admin", "status": "ok"}]},
    }
    monkeypatch.setattr(controller, "_json_command", reports.__getitem__)
    if profile == "documents":
        assert controller._probe() == "ready"
    else:
        message = (
            "does not report its application profile"
            if profile is None
            else "another application"
        )
        with pytest.raises(image.LauncherError, match=message):
            controller._probe()


@pytest.mark.skipif(os.name != "posix", reason="Linux shell installer adapter")
def test_cancel_reaches_installer_while_stdout_is_silent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller = _controller(tmp_path, monkeypatch)
    marker = tmp_path / "started"
    monkeypatch.setenv("APPIMAGE_TEST_MARKER", str(marker))
    # The producer emits no progress before cancellation. Its finite loop is a
    # cleanup bound, not the expected cancellation mechanism.
    template = _event("OPERATION", state="cancelled").decode().strip()
    (tmp_path / "installer.sh").write_text(
        "#!/bin/sh\nset -eu\n"
        'while [ $# -gt 0 ]; do case "$1" in\n'
        "--operation-id) operation=$2; shift 2;;\n"
        "--cancel-file) cancel=$2; shift 2;;\n"
        "--program-root) shift 2;;\n*) shift;;\nesac; done\n"
        ': > "$APPIMAGE_TEST_MARKER"\n'
        'count=0; while [ ! -f "$cancel" ] && [ $count -lt 200 ]; do\n'
        "sleep 0.05; count=$((count+1)); done\n"
        '[ -f "$cancel" ] || exit 7\n'
        f"printf '%s\\n' '{template}' | sed \"s/OPERATION/$operation/\"\n"
        "exit 130\n"
    )
    results: list[str] = []
    failures: list[BaseException] = []

    def run() -> None:
        try:
            results.append(controller._install())
        except BaseException as exc:
            failures.append(exc)

    worker = threading.Thread(target=run)
    worker.start()
    deadline = time.monotonic() + 5
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert marker.exists()
    started = time.monotonic()
    controller.cancel.set()
    worker.join(timeout=15)
    assert not worker.is_alive()
    assert not failures
    assert results == ["cancelled"]
    assert time.monotonic() - started < 3
    assert not controller.program_root.exists()


def test_initial_account_uses_private_input_and_clears_widgets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller = _controller(tmp_path, monkeypatch)
    controller.email = Mock(get=Mock(return_value="reader@example.test"))
    controller.password = Mock(get=Mock(return_value="synthetic-pipe-password"))
    controller.confirmation = Mock(get=Mock(return_value="synthetic-pipe-password"))
    initialize = Mock()
    monkeypatch.setattr(controller, "_initialize", initialize)
    controller.create_account()
    payload = json.loads(initialize.call_args.args[0])
    assert payload == {
        "email": "reader@example.test",
        "password": "synthetic-pipe-password",
    }
    controller.password.set.assert_called_once_with("")
    controller.confirmation.set.assert_called_once_with("")
    assert os.environ["CARLINA_HOME"] == str(tmp_path / "synthetic-data")
