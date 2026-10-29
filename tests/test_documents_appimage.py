"""Parent-owned setup adapter tests use synthetic, isolated installer processes."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import queue
import subprocess
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


def _windows_bundle(tmp_path: Path) -> None:
    installer = tmp_path / "installer.ps1"
    installer.write_bytes(b"# synthetic Windows installer\n")
    (tmp_path / "launcher.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "product": "carlina-documents",
                "version": "0.2.0a2",
                "source_revision": "a" * 40,
                "platform": "win-64",
                "installer_sha256": hashlib.sha256(installer.read_bytes()).hexdigest(),
            }
        )
    )


def test_windows_resources_are_bound_to_the_installer_and_platform(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(image, "_is_windows", lambda: True)
    _windows_bundle(tmp_path)
    assert image.load_configuration(tmp_path)["platform"] == "win-64"
    (tmp_path / "installer.ps1").write_bytes(b"changed")
    with pytest.raises(image.LauncherError, match="installer has changed"):
        image.load_configuration(tmp_path)
    monkeypatch.setattr(image, "_is_windows", lambda: False)
    with pytest.raises(image.LauncherError, match="different platform"):
        image.load_configuration(tmp_path)


def test_powershell_arguments_preserve_unicode_and_cannot_add_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(image, "_is_windows", lambda: True)
    path = tmp_path / "P Ω it's" / "carlina-documents.ps1"
    arguments = image._script_command(path, ["status", "--json"])
    assert arguments[-2] == "-EncodedCommand"
    script = base64.b64decode(arguments[-1]).decode("utf-16le")
    assert "P Ω it''s" in script
    assert "'status' '--json'; exit $LASTEXITCODE" in script
    assert "UTF8Encoding" in script and "-NoProfile" in arguments
    assert image._process_options() == {"creationflags": 0x08000000}
    assert image._process_options(detached=True) == {"creationflags": 0x08000200}

    installer = image._script_command(
        path,
        ["-ProgramRoot", "C:\\P Ω it's; throw 'unsafe'", "-SkipInit"],
        named_parameters=True,
    )
    script = base64.b64decode(installer[-1]).decode("utf-16le")
    assert " -ProgramRoot 'C:\\P Ω it''s; throw ''unsafe''' -SkipInit;" in script


def test_frozen_environment_does_not_leak_into_installed_programs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller = _controller(tmp_path, monkeypatch)
    bundle = tmp_path / "unpacked"
    monkeypatch.setattr(image, "_is_windows", lambda: True)
    monkeypatch.setattr(image.sys, "_MEIPASS", str(bundle), raising=False)
    monkeypatch.setattr(image, "_restore_child_dll_search", Mock())
    for name in (
        "PYTHONHOME",
        "PYTHONPATH",
        "TCL_LIBRARY",
        "TK_LIBRARY",
        "_PYI_ARCHIVE_FILE",
    ):
        monkeypatch.setenv(name, str(bundle))
    external = tmp_path / "external-bin"
    monkeypatch.setenv(
        "PATH", os.pathsep.join([str(bundle), str(bundle / "bin"), str(external)])
    )
    environment = controller._environment()
    assert environment["PATH"] == str(external)
    assert environment["CARLINA_HOME"] == str(tmp_path / "synthetic-data")
    assert not {
        "PYTHONHOME",
        "PYTHONPATH",
        "TCL_LIBRARY",
        "TK_LIBRARY",
        "_PYI_ARCHIVE_FILE",
    }.intersection(environment)
    image._restore_child_dll_search.assert_called_once()


@pytest.mark.parametrize("safe", [True, False])
def test_windows_existing_launcher_checks_ownership_before_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, safe: bool
) -> None:
    controller = _controller(tmp_path, monkeypatch)
    controller.program_root.mkdir()
    monkeypatch.setattr(image, "_is_windows", lambda: True)
    controller.launcher.write_text("synthetic installed launcher")
    process = Mock(
        return_value=subprocess.CompletedProcess(
            [], 0, json.dumps({"safe": safe}).encode(), b""
        )
    )
    monkeypatch.setattr(image.subprocess, "run", process)
    reports = {
        "status": {
            "application_profile": "documents",
            "managed_install": {"program_root": str(controller.program_root)},
        },
        "doctor": {"checks": [{"id": "accounts.admin", "status": "ok"}]},
    }
    query = Mock(side_effect=reports.__getitem__)
    monkeypatch.setattr(controller, "_json_command", query)
    if safe:
        assert controller._probe() == "ready"
    else:
        with pytest.raises(image.LauncherError, match="another account"):
            controller._probe()
        query.assert_not_called()
    assert process.call_args.kwargs["creationflags"] == 0x08000000


def test_windows_install_uses_supervision_and_the_same_event_protocol(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller = _controller(tmp_path, monkeypatch)
    monkeypatch.setattr(image, "_is_windows", lambda: True)
    monkeypatch.setattr(controller, "_probe", lambda: "install")
    monkeypatch.setattr(image.uuid, "uuid4", lambda: Mock(hex="a" * 32))
    child = Mock(stdout=io.BytesIO(_event("a" * 32, state="succeeded")))
    child.poll.return_value = 0
    child.wait.return_value = 0
    start = Mock(return_value=child)
    monkeypatch.setattr(image.subprocess, "Popen", start)
    assert controller._install() == "account"
    script = base64.b64decode(start.call_args.args[0][-1]).decode("utf-16le")
    for argument in (
        "-SupervisedPreparation",
        "-EventsJsonl",
        "-NoPathUpdate",
        "-SkipInit",
        "-CancelFile",
    ):
        assert " " + argument + " " in script
        assert "'" + argument + "'" not in script
    assert "installer.ps1" in script and "--program-root" not in script
    assert start.call_args.kwargs["creationflags"] == 0x08000000
    child.wait.assert_called_once()


def test_windows_account_payload_stays_on_stdin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller = _controller(tmp_path, monkeypatch)
    monkeypatch.setattr(image, "_is_windows", lambda: True)
    controller.message = Mock()
    controller.detail = Mock()
    monkeypatch.setattr(controller, "_clear_content", Mock())
    monkeypatch.setattr(controller, "_probe", lambda: "ready")
    background = Mock()
    monkeypatch.setattr(controller, "_background", background)
    execute = Mock(return_value=subprocess.CompletedProcess([], 0, b"", b""))
    monkeypatch.setattr(image.subprocess, "run", execute)
    payload = json.dumps(
        {"email": "reader@example.test", "password": "Synthetic Ω account"},
        ensure_ascii=False,
    ).encode()
    controller._initialize(payload)
    action = background.call_args.args[1]
    assert action() == "ready"
    assert execute.call_args.kwargs["input"] == payload
    assert "Synthetic" not in repr(execute.call_args.args)
    script = base64.b64decode(execute.call_args.args[0][-1]).decode("utf-16le")
    assert "'init' '--admin-stdin'" in script and "Synthetic" not in script


def test_windowless_binary_can_write_diagnostics_without_opening_tk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _windows_bundle(tmp_path)
    monkeypatch.setattr(image, "_is_windows", lambda: True)
    monkeypatch.setattr(image, "__file__", str(tmp_path / "bootstrap.py"))
    monkeypatch.setattr(image.sys, "stdout", None)
    create = Mock()
    monkeypatch.setattr(image.tk, "Tk", create)
    interpreter = Mock()
    interpreter.eval.return_value = "8.6.16"
    monkeypatch.setattr(image.tk, "Tcl", Mock(return_value=interpreter))
    output = tmp_path / "diagnostics.json"
    assert image.main(["--diagnostics", str(output)]) == 0
    record = json.loads(output.read_text())
    assert record["platform"] == "win-64" and record["bundled_installer_verified"]
    assert "program_root" not in record
    assert image.main(["--version"]) == 0
    create.assert_not_called()


def test_windows_server_uses_the_installed_launcher_after_bootstrap_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller = _controller(tmp_path, monkeypatch)
    monkeypatch.setattr(image, "_is_windows", lambda: True)
    url = "http://127.0.0.1:5015/"
    report = Mock(
        side_effect=[
            {"running": False},
            {"running": True, "managed_process": True, "url": url},
        ]
    )
    monkeypatch.setattr(controller, "_json_command", report)
    child = Mock()
    child.poll.return_value = None
    start = Mock(return_value=child)
    monkeypatch.setattr(image.subprocess, "Popen", start)
    monkeypatch.setattr(image.time, "sleep", Mock())
    assert controller._start() == url
    script = base64.b64decode(start.call_args.args[0][-1]).decode("utf-16le")
    assert (
        str(controller.launcher) in script
        and str(controller.appdir / "bootstrap.py") not in script
    )
    assert "'start' '--host' '127.0.0.1'" in script
    assert start.call_args.kwargs["creationflags"] == 0x08000200
