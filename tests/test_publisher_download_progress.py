"""Publisher-internal transfers use actual local TLS and native download owners."""

from __future__ import annotations

import gzip
import hashlib
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest
from test_installer_download_progress import _DownloadServer
from test_installer_handoff_cleanup import _isolated_config_environment
from test_publisher_bootstrap_scripts import (
    _assert_progress_protocol,
    publisher_bootstrap_fixture,
)

from carlina.core.installation_lifecycle import (
    InstallationCancelled,
    InstallationEvent,
    InstallationLifecycle,
    InstallationState,
)
from carlina.core.native_download_scripts import render_shell_download_helpers
from carlina.core.publisher_bootstrap import PublisherBootstrapPlan
from carlina.core.publisher_bootstrap_scripts import render_publisher_bootstrap
from carlina.core.publisher_installation import run_publisher_bootstrap

pytestmark = [
    pytest.mark.fast,
    pytest.mark.contract,
    pytest.mark.skipif(
        os.name == "nt", reason="Linux and portable PowerShell TLS lane"
    ),
]


@pytest.fixture
def server(tmp_path: Path) -> Iterator[_DownloadServer]:
    fixture = _DownloadServer(tmp_path)
    try:
        yield fixture
    finally:
        fixture.close()


def _fixture(
    root: Path,
    server: _DownloadServer,
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
) -> tuple[list[str], Path, dict[str, str], PublisherBootstrapPlan, bytes]:
    base, _, _, downloads, unpacker_url, _ = publisher_bootstrap_fixture(
        monkeypatch, platform=platform
    )
    first_payload = bytes(number % 251 for number in range(32768))
    first = replace(
        base.downloads[0],
        size=len(first_payload),
        sha256=hashlib.sha256(first_payload).hexdigest(),
    )
    second = replace(base.downloads[1], offset_data=33792)
    archive = bytearray(65536)
    archive[first.offset_data : first.offset_data + first.size] = first_payload
    archive[second.offset_data : second.offset_data + second.size] = downloads[
        second.url
    ]
    scaffold = gzip.compress(bytes(len(archive)), mtime=0)
    plan = replace(
        base,
        archive=replace(
            base.archive, size=len(archive), sha256=hashlib.sha256(archive).hexdigest()
        ),
        skeleton=replace(
            base.skeleton,
            size=len(scaffold),
            sha256=hashlib.sha256(scaffold).hexdigest(),
            uncompressed_size=len(archive),
        ),
        downloads=(first, second),
    )
    downloads[first.url] = first_payload
    source = render_publisher_bootstrap(plan, scaffold).decode()
    for number, (url, payload) in enumerate(downloads.items()):
        path = f"/dependency-{number}"
        server.payloads[path] = payload
        assert source.count(url) == 1
        # The production plan remains allowlisted and immutable. Only this
        # disposable script's transport destinations point to the TLS fixture.
        source = source.replace(url, server.base_url + path.lstrip("/"))
    server.held_path = "/dependency-0"
    server.artifact = first_payload
    destination = root / "candidate version"
    destination.mkdir()
    old = root / "previous version"
    old.mkdir()
    old.joinpath("marker").write_bytes(b"unchanged previous generation")
    data = root / "owned data"
    data.mkdir()
    data.joinpath("marker").write_bytes(b"unchanged private data")
    shim = root / "native tools"
    shim.mkdir()
    environment = {
        **os.environ,
        **_isolated_config_environment(root),
        "CARLINA_HOME": str(data),
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
        "CARLINA_ARCHIVE_CAPTURE": str(root / "archive.capture"),
        "CARLINA_UNPACKER_MARKER": str(root / "unpacker.executed"),
        "CARLINA_TEST_CURL_PID": str(root / "curl.pid"),
        "PATH": str(shim) + os.pathsep + os.environ.get("PATH", ""),
    }
    if platform == "linux-64":
        curl = shutil.which("curl")
        bash = shutil.which("bash")
        assert curl and bash and shutil.which("prlimit")
        shim.joinpath("curl").write_text(
            '#!/bin/sh\nprintf "%s\\n" "$$" > "$CARLINA_TEST_CURL_PID"\n'
            f'exec {shlex.quote(curl)} "$@"\n'
        )
        shim.joinpath("curl").chmod(0o700)
        environment["CURL_CA_BUNDLE"] = str(server.certificate)
        script = root / "publisher.sh"
        command = [bash, str(script), "--progress-records-v1"]
    else:
        powershell = shutil.which("pwsh")
        if powershell is None:
            pytest.skip("Portable PowerShell is unavailable")
        marker = "$handler.AllowAutoRedirect = $false"
        assert source.count(marker) == 1
        source = source.replace(
            marker,
            marker
            + "\n        $handler.ServerCertificateCustomValidationCallback = [Net.Http.HttpClientHandler]::DangerousAcceptAnyServerCertificateValidator",
        )
        pattern = r"(?ms)^function Invoke-NativeUnpackerProcess\(.*?^\}\n"
        source, replacements = re.subn(
            pattern,
            """function Invoke-NativeUnpackerProcess([string]$UnpackerPath, [string]$ArchivePath) {
    & /bin/bash $UnpackerPath $ArchivePath
}
""",
            source,
            count=1,
        )
        assert replacements == 1
        script = root / "publisher.ps1"
        command = [
            powershell,
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(script),
            "-ProgressRecordsV1",
        ]
    script.write_text(source)
    assert unpacker_url in downloads
    return command, destination, environment, plan, bytes(archive)


def _phases(output: Path) -> list[tuple[str, str, str, str]]:
    records = []
    for line in output.read_text().splitlines():
        fields = line.split()
        if len(fields) == 6 and fields[:2] == [
            "carlina.publisher-bootstrap/1",
            "phase",
        ]:
            records.append((fields[2], fields[3], fields[4], fields[5]))
    return records


def _wait_partial(
    output: Path, process: subprocess.Popen[str], errors: Path, size: int
) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if any(
            phase == "download"
            and safe == "true"
            and completed != "null"
            and 0 < int(completed) < size
            and int(total) == size
            for phase, safe, completed, total in _phases(output)
        ):
            return
        assert process.poll() is None, errors.read_text()
        time.sleep(0.05)
    pytest.fail("No partial publisher progress while the body remained open")


def _preserved(root: Path, destination: Path) -> None:
    assert (
        root.joinpath("previous version/marker").read_bytes()
        == b"unchanged previous generation"
    )
    assert root.joinpath("owned data/marker").read_bytes() == b"unchanged private data"
    assert not tuple(destination.glob(".carlina-publisher.*"))


@pytest.mark.parametrize("platform", ["linux-64", "win-64"])
@pytest.mark.parametrize("stage", ["headers", "body"])
@pytest.mark.parametrize("control_kind", ["request", "directory"])
def test_publisher_cancels_during_held_internal_transfer(
    tmp_path: Path,
    server: _DownloadServer,
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    stage: str,
    control_kind: str,
) -> None:
    server.mode = stage
    command, destination, environment, plan, _ = _fixture(
        tmp_path, server, monkeypatch, platform
    )
    request = tmp_path / "caller cancellation"
    command += [
        "--cancel-file" if platform == "linux-64" else "-CancelFile",
        str(request),
    ]
    output, errors = tmp_path / "progress.records", tmp_path / "diagnostics"
    with output.open("w") as stdout, errors.open("w") as stderr:
        process = subprocess.Popen(
            command,
            cwd=destination,
            env=environment,
            stdout=stdout,
            stderr=stderr,
            text=True,
            start_new_session=True,
        )
        try:
            assert server.started.wait(15), errors.read_text()
            if stage == "body":
                _wait_partial(output, process, errors, plan.downloads[0].size)
            started = time.monotonic()
            if control_kind == "request":
                request.write_bytes(b"opaque caller-owned bytes")
            else:
                request.mkdir()
            process.wait(timeout=6)
            assert time.monotonic() - started < 5
            assert not server.release.is_set()
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
    terminal = (
        "cancelled cancelled true"
        if control_kind == "request"
        else "failed operation_failed false"
    )
    _assert_progress_protocol(
        output.read_text(), terminal="carlina.publisher-bootstrap/1 result " + terminal
    )
    assert process.returncode == (130 if control_kind == "request" else 1)
    assert server.paths == ["/dependency-0"]
    assert not tmp_path.joinpath("unpacker.executed").exists()
    assert (
        request.read_bytes() == b"opaque caller-owned bytes"
        if control_kind == "request"
        else request.is_dir()
    )
    _preserved(tmp_path, destination)
    if platform == "linux-64":
        with pytest.raises(ProcessLookupError):
            os.kill(int(tmp_path.joinpath("curl.pid").read_text()), 0)


@pytest.mark.parametrize("platform", ["linux-64", "win-64"])
def test_publisher_partial_bytes_precede_verified_archive_and_cleanup(
    tmp_path: Path,
    server: _DownloadServer,
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
) -> None:
    command, destination, environment, plan, archive = _fixture(
        tmp_path, server, monkeypatch, platform
    )
    if platform == "win-64":
        script = Path(command[command.index("-File") + 1])
        text = script.read_text()
        marker = "$clock = [System.Diagnostics.Stopwatch]::StartNew()"
        assert text.count(marker) == 1
        script.write_text(
            text.replace(marker, "$clock = [System.Diagnostics.Stopwatch]::new()")
        )
    output, errors = tmp_path / "progress.records", tmp_path / "diagnostics"
    with output.open("w") as stdout, errors.open("w") as stderr:
        process = subprocess.Popen(
            command,
            cwd=destination,
            env=environment,
            stdout=stdout,
            stderr=stderr,
            text=True,
            start_new_session=True,
        )
        try:
            assert server.started.wait(15), errors.read_text()
            _wait_partial(output, process, errors, plan.downloads[0].size)
            server.release.set()
            process.wait(timeout=30)
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
    assert process.returncode == 0, errors.read_text()
    phases = _assert_progress_protocol(
        output.read_text(),
        terminal="carlina.publisher-bootstrap/1 result succeeded null false",
    )
    progress = [
        int(completed)
        for phase, _, completed, total in phases
        if phase == "download" and total == str(plan.downloads[0].size)
    ]
    assert progress[0] == 0 and progress[-1] == plan.downloads[0].size
    assert progress == sorted(set(progress))
    assert tmp_path.joinpath("archive.capture").read_bytes() == archive
    assert tmp_path.joinpath("unpacker.executed").exists()
    _preserved(tmp_path, destination)


@pytest.mark.parametrize("platform", ["linux-64", "win-64"])
@pytest.mark.parametrize(
    "mode",
    [
        "downgrade",
        "redirect_loop",
        "truncated",
        "oversized_header",
        "oversized_chunked",
        "corrupt",
        "short_unknown_length",
        "relative_redirect",
    ],
)
def test_publisher_real_transport_rejects_unverified_or_unsafe_input(
    tmp_path: Path,
    server: _DownloadServer,
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    mode: str,
) -> None:
    server.mode = mode
    command, destination, environment, plan, archive = _fixture(
        tmp_path, server, monkeypatch, platform
    )
    result = subprocess.run(
        command,
        cwd=destination,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    success = mode == "relative_redirect"
    verification_failure = mode == "corrupt" or (
        mode == "short_unknown_length" and platform == "linux-64"
    )
    category = (
        "verification_failed false" if verification_failure else "download_failed true"
    )
    terminal = "succeeded null false" if success else "failed " + category
    phases = _assert_progress_protocol(
        result.stdout, terminal="carlina.publisher-bootstrap/1 result " + terminal
    )
    assert (result.returncode == 0) is success, result.stderr
    if not success:
        assert not any(
            phase == "download" and completed == total == str(plan.downloads[0].size)
            for phase, _, completed, total in phases
        )
        assert not tmp_path.joinpath("unpacker.executed").exists()
    else:
        assert tmp_path.joinpath("archive.capture").read_bytes() == archive
    if mode == "downgrade":
        assert server.http_hits == 0
    if mode == "redirect_loop":
        assert len(server.paths) == 9
    _preserved(tmp_path, destination)


@pytest.mark.parametrize("platform", ["linux-64", "win-64"])
@pytest.mark.parametrize("stage", ["headers", "body"])
def test_publisher_deadline_covers_pending_internal_reads(
    tmp_path: Path,
    server: _DownloadServer,
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    stage: str,
) -> None:
    server.mode = stage
    command, destination, environment, _, _ = _fixture(
        tmp_path, server, monkeypatch, platform
    )
    script = Path(
        command[1] if platform == "linux-64" else command[command.index("-File") + 1]
    )
    text = script.read_text()
    marker = (
        "--max-time 900" if platform == "linux-64" else "$downloadTimeoutSeconds = 900"
    )
    assert marker in text
    script.write_text(text.replace(marker, marker.replace("900", "2")))
    process = subprocess.Popen(
        command,
        cwd=destination,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        assert server.started.wait(15)
        stdout, stderr = process.communicate(timeout=6)
        assert not server.release.is_set()
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
    _assert_progress_protocol(
        stdout,
        terminal="carlina.publisher-bootstrap/1 result failed download_failed true",
    )
    assert process.returncode != 0, stderr
    _preserved(tmp_path, destination)


@pytest.mark.parametrize("platform", ["linux-64", "win-64"])
def test_installed_bridge_cancellation_waits_for_real_publisher_cleanup(
    tmp_path: Path,
    server: _DownloadServer,
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
) -> None:
    command, destination, environment, plan, _ = _fixture(
        tmp_path, server, monkeypatch, platform
    )
    request, control, pid_file = (
        tmp_path / "caller request",
        tmp_path / "owned bridge control",
        tmp_path / "publisher.pid",
    )
    command += [
        "--cancel-file" if platform == "linux-64" else "-CancelFile",
        str(control),
    ]
    environment["CARLINA_TEST_PUBLISHER_PID"] = str(pid_file)
    source = "import os,sys; from pathlib import Path; os.setsid(); Path(os.environ['CARLINA_TEST_PUBLISHER_PID']).write_text(str(os.getpid())); os.execvpe(sys.argv[1], sys.argv[1:], os.environ)"
    command = [sys.executable, "-I", "-S", "-c", source, *command]
    events: list[InstallationEvent] = []

    def execute() -> None:
        lifecycle = InstallationLifecycle(
            "update",
            "documents",
            operation_id="a" * 32,
            event_sink=events.append,
            cancel_requested=request.exists,
        )
        run_publisher_bootstrap(
            command,
            cwd=destination,
            environment=environment,
            cancellation_file=control,
            lifecycle=lifecycle,
        )

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(execute)
        try:
            assert server.started.wait(15), (
                str(future.exception())
                if future.done()
                else "Publisher did not reach the request barrier"
            )
            deadline = time.monotonic() + 5
            while not any(
                event.bytes_completed is not None
                and 0 < event.bytes_completed < plan.downloads[0].size
                for event in events
            ):
                assert not future.done()
                assert time.monotonic() < deadline
                time.sleep(0.05)
            request.write_bytes(b"caller control remains opaque")
            with pytest.raises(InstallationCancelled):
                future.result(timeout=6)
            assert not server.release.is_set()
        finally:
            if not future.done():
                if pid_file.exists():
                    os.killpg(int(pid_file.read_text()), signal.SIGKILL)
                server.release.set()
                future.exception(timeout=6)
    assert control.read_bytes() == b"" and control.stat().st_mode & 0o777 == 0o600
    assert request.read_bytes() == b"caller control remains opaque"
    assert events and all(event.state is InstallationState.RUNNING for event in events)
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    assert {event.operation_id for event in events} == {"a" * 32}
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text()), 0)
    _preserved(tmp_path, destination)


def test_linux_publisher_bounds_unknown_length_even_without_curl_size_guard(
    tmp_path: Path, server: _DownloadServer
) -> None:
    server.mode = "oversized_chunked"
    server.held_path = "/bounded"
    server.payloads["/bounded"] = b"x" * 32768
    curl = shutil.which("curl")
    bash = shutil.which("bash")
    assert curl and bash and shutil.which("prlimit")
    shim = tmp_path / "shim"
    shim.mkdir()
    shim.joinpath("curl").write_text(
        f"#!{bash}\nargs=()\n"
        'while [ "$#" -gt 0 ]; do\n'
        '  if [ "$1" = --max-filesize ]; then shift 2; else args+=("$1"); shift; fi\n'
        "done\n"
        'printf "%s\\n" "$BASHPID" > "$CARLINA_TEST_CURL_PID"\n'
        f'exec {shlex.quote(curl)} "${{args[@]}}"\n'
    )
    shim.joinpath("curl").chmod(0o700)
    target = tmp_path / "bounded.output"
    source = (
        "set -eu\nBOOTSTRAP_NATIVE_ACTIVE=0\nBOOTSTRAP_DOWNLOAD_PID=''\n"
        "check_download_cancellation() { :; }\n"
        "report_download_progress() { :; }\n"
        "mark_download_size_failure() { :; }\n"
        + render_shell_download_helpers(publisher=True)
        + "\ntrap stop_bootstrap_download EXIT\n"
        + f"download_https_event {shlex.quote(server.base_url + 'bounded')} {shlex.quote(str(target))} 32768\n"
    )
    script = tmp_path / "bounded.sh"
    script.write_text(source)
    result = subprocess.run(
        [bash, str(script)],
        env={
            **os.environ,
            "PATH": str(shim) + os.pathsep + os.environ.get("PATH", ""),
            "CURL_CA_BUNDLE": str(server.certificate),
            "CARLINA_TEST_CURL_PID": str(tmp_path / "curl.pid"),
            "NO_PROXY": "127.0.0.1",
        },
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode != 0, result.stderr
    assert target.stat().st_size == 32769
    with pytest.raises(ProcessLookupError):
        os.kill(int(tmp_path.joinpath("curl.pid").read_text()), 0)


def test_linux_publisher_requires_prlimit_only_for_progress_mode(
    tmp_path: Path, server: _DownloadServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    command, destination, environment, _, _ = _fixture(
        tmp_path, server, monkeypatch, "linux-64"
    )
    restricted = tmp_path / "restricted tools"
    restricted.mkdir()
    for name in (
        "curl",
        "base64",
        "gzip",
        "dd",
        "head",
        "tail",
        "wc",
        "tr",
        "mktemp",
        "cut",
        "chmod",
        "rm",
    ):
        tool = shutil.which(name)
        assert tool
        restricted.joinpath(name).symlink_to(tool)
    environment["PATH"] = str(restricted)
    result = subprocess.run(
        command,
        cwd=destination,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode != 0
    assert "prlimit" in result.stderr
    _assert_progress_protocol(
        result.stdout,
        terminal="carlina.publisher-bootstrap/1 result failed operation_failed false",
    )
    assert not server.paths
    _preserved(tmp_path, destination)
