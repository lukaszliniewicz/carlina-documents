"""Real local TLS transfers exercise the generated pre-Python download owners."""

from __future__ import annotations

import datetime as dt
import ipaddress
import json
import os
import shlex
import shutil
import signal
import ssl
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from filelock import FileLock, Timeout
from test_installer_handoff_cleanup import (
    _EVENT_UNPACKER,
    _assert_event_outcome,
    _event_fixture,
    _native_event_command,
)

pytestmark = [
    pytest.mark.fast,
    pytest.mark.contract,
    pytest.mark.skipif(
        os.name == "nt", reason="Local TLS and portable PowerShell lane"
    ),
]


def _certificate(root: Path) -> tuple[Path, Path]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = dt.datetime.now(dt.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=1))
        .not_valid_after(now + dt.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("localhost"),
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                ]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    certificate_path, key_path = root / "tls.pem", root / "tls.key"
    certificate_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return certificate_path, key_path


class _DownloadServer:
    """Only synthetic metadata/artifacts are served, behind deterministic barriers."""

    def __init__(self, root: Path) -> None:
        self.mode = "body"
        self.started = threading.Event()
        self.release = threading.Event()
        self.paths: list[str] = []
        self.http_hits = 0
        self.index = b""
        self.artifact = b""
        self.payloads: dict[str, bytes] = {}
        self.held_path: str | None = None
        self.certificate, key = _certificate(root)
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:
                fixture.respond(self)

            def handle(self) -> None:
                with suppress(BrokenPipeError, ConnectionResetError, ssl.SSLError):
                    super().handle()

            def log_message(self, format: str, *args: Any) -> None:
                pass

        self.plain = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.secure = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(self.certificate, key)
        self.secure.socket = context.wrap_socket(self.secure.socket, server_side=True)
        self.threads = [
            threading.Thread(
                target=server.serve_forever,
                kwargs={"poll_interval": 0.05},
                daemon=True,
            )
            for server in (self.plain, self.secure)
        ]
        for thread in self.threads:
            thread.start()

    @property
    def base_url(self) -> str:
        return f"https://127.0.0.1:{self.secure.server_port}/"

    def respond(self, request: BaseHTTPRequestHandler) -> None:
        if request.server is self.plain:
            self.http_hits += 1
            request.send_response(200)
            request.send_header("Content-Length", "0")
            request.end_headers()
            return
        self.paths.append(request.path)
        index = request.path.endswith("/index.json")
        selected = self.held_path is None or request.path == self.held_path
        if selected and self.mode in {"downgrade", "redirect_loop"}:
            request.send_response(302)
            location = (
                f"http://127.0.0.1:{self.plain.server_port}/forbidden"
                if self.mode == "downgrade"
                else request.path
            )
            request.send_header("Location", location)
            request.send_header("Content-Length", "0")
            request.end_headers()
            return
        if (
            self.mode == "relative_redirect"
            and selected
            and not index
            and request.path != "/payload"
        ):
            request.send_response(307)
            request.send_header("Location", "/payload")
            request.send_header("Content-Length", "0")
            request.end_headers()
            return
        body = self.payloads.get(request.path, self.index if index else self.artifact)
        header_selected = selected if self.held_path is not None else index
        body_selected = selected if self.held_path is not None else not index
        if self.mode == "headers" and header_selected:
            self.started.set()
            self.release.wait(20)
        if body_selected and self.mode.startswith("oversized"):
            body += b"excess"
        if body_selected and self.mode == "corrupt":
            body = bytes([body[0] ^ 1]) + body[1:]
        if body_selected and self.mode == "short_unknown_length":
            body = body[: len(body) // 2]
        request.send_response(200)
        chunked = (
            self.mode in {"oversized_chunked", "short_unknown_length"} and body_selected
        )
        request.send_header(
            "Transfer-Encoding" if chunked else "Content-Length",
            "chunked" if chunked else str(len(body)),
        )
        request.end_headers()
        if chunked:
            request.wfile.write(f"{len(body):x}\r\n".encode() + body + b"\r\n0\r\n\r\n")
            request.wfile.flush()
        elif body_selected and self.mode in {"body", "truncated"}:
            boundary = max(1, len(body) // 3)
            request.wfile.write(body[:boundary])
            request.wfile.flush()
            self.started.set()
            if self.mode == "truncated":
                request.close_connection = True
                return
            self.release.wait(20)
            request.wfile.write(body[boundary:])
            request.wfile.flush()
        else:
            request.wfile.write(body)
            request.wfile.flush()

    def close(self) -> None:
        self.release.set()
        for server in (self.plain, self.secure):
            server.shutdown()
            server.server_close()
        for thread in self.threads:
            thread.join(timeout=5)
            assert not thread.is_alive()


@pytest.fixture
def server(tmp_path: Path) -> Iterator[_DownloadServer]:
    fixture = _DownloadServer(tmp_path)
    try:
        yield fixture
    finally:
        fixture.close()


def _native_download_fixture(
    root: Path,
    server: _DownloadServer,
    profile: str,
    platform: str,
    *,
    trust_certificate: bool = True,
) -> tuple[Path, Path, dict[str, str]]:
    installer, program, environment = _event_fixture(
        root,
        profile,
        platform,
        "success",
        base_url=server.base_url,
        native_downloads=True,
        # Exceed curl's output buffer before the deterministic body barrier.
        unpacker_payload=_EVENT_UNPACKER + b"\n#" + b"x" * 32768 + b"\n",
    )
    server.index = Path(environment["CARLINA_TEST_INDEX"]).read_bytes()
    server.artifact = Path(environment["CARLINA_TEST_ARTIFACT"]).read_bytes()
    environment.update(NO_PROXY="127.0.0.1,localhost", no_proxy="127.0.0.1,localhost")
    if platform == "linux-64":
        curl = shutil.which("curl")
        assert curl is not None
        root.joinpath("shim/curl").write_text(
            '#!/bin/sh\nprintf "%s\\n" "$$" > "$CARLINA_TEST_CURL_PID"\n'
            f'exec {shlex.quote(curl)} "$@"\n'
        )
        environment["CARLINA_TEST_CURL_PID"] = str(root / "curl.pid")
        if trust_certificate:
            environment["CURL_CA_BUNDLE"] = str(server.certificate)
        else:
            environment.pop("CURL_CA_BUNDLE", None)
    elif trust_certificate:
        script = installer.read_text()
        marker = "$handler.AllowAutoRedirect = $false"
        assert script.count(marker) == 1
        # Portable PS7 fixture only. The production carrier keeps normal validation.
        script = script.replace(
            marker,
            marker + "\n    $handler.ServerCertificateCustomValidationCallback = "
            "[Net.Http.HttpClientHandler]::DangerousAcceptAnyServerCertificateValidator",
        )
        installer.write_text(script)
    return installer, program, environment


def _records(path: Path) -> list[dict[str, Any]]:
    # A writer may be between bytes of its newest record.
    lines = path.read_text().splitlines(keepends=True)
    return [json.loads(line) for line in lines if line.endswith("\n")]


def _wait_for_progress(
    output: Path, process: subprocess.Popen[str], errors: Path
) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if any(
            event["bytes_completed"] is not None
            and 0 < event["bytes_completed"] < event["bytes_total"]
            for event in _records(output)
        ):
            return
        assert process.poll() is None, errors.read_text()
        time.sleep(0.05)
    pytest.fail("No partial byte progress arrived while the response was held open.")


@pytest.mark.parametrize("profile", ["documents", "workbench"])
@pytest.mark.parametrize("platform", ["linux-64", "win-64"])
@pytest.mark.parametrize("stage", ["headers", "body"])
def test_cancel_stalled_native_transfer_releases_owned_paths(
    tmp_path: Path, server: _DownloadServer, profile: str, platform: str, stage: str
) -> None:
    server.mode = stage
    installer, program, environment = _native_download_fixture(
        tmp_path, server, profile, platform
    )
    request = tmp_path / "caller cancellation"
    command = _native_event_command(installer, program, platform)
    command += [
        "--operation-id" if platform == "linux-64" else "-OperationId",
        "a" * 32,
    ]
    command += [
        "--cancel-file" if platform == "linux-64" else "-CancelFile",
        str(request),
    ]
    output, errors = tmp_path / "events.jsonl", tmp_path / "diagnostics"
    with output.open("w") as stdout, errors.open("w") as stderr:
        process = subprocess.Popen(
            command,
            env=environment,
            stdout=stdout,
            stderr=stderr,
            text=True,
            start_new_session=True,
        )
        try:
            assert server.started.wait(15), errors.read_text()
            with (
                pytest.raises(Timeout),
                FileLock(program / "state/managed-install.lock", timeout=0),
            ):
                pass
            if stage == "body":
                _wait_for_progress(output, process, errors)
            started = time.monotonic()
            request.write_bytes(b"opaque request; contents must never be interpreted")
            process.wait(timeout=6)
            assert time.monotonic() - started < 5
            assert not server.release.is_set(), (
                "The transfer must stop before the server finishes."
            )
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
    result = subprocess.CompletedProcess(
        command, process.returncode, output.read_text(), errors.read_text()
    )
    _assert_event_outcome(result, program, profile, "cancelled", before_adoption=True)
    assert result.returncode == 130
    assert request.read_bytes().startswith(b"opaque request")
    assert not tuple(program.glob(".carlina-install*"))
    with FileLock(program / "state/managed-install.lock", timeout=0):
        pass
    if platform == "linux-64":
        with pytest.raises(ProcessLookupError):
            os.kill(int(tmp_path.joinpath("curl.pid").read_text()), 0)


@pytest.mark.parametrize("profile", ["documents", "workbench"])
@pytest.mark.parametrize("platform", ["linux-64", "win-64"])
def test_native_transfer_reports_partial_bytes_then_verified_completion(
    tmp_path: Path, server: _DownloadServer, profile: str, platform: str
) -> None:
    installer, program, environment = _native_download_fixture(
        tmp_path, server, profile, platform
    )
    if platform == "win-64":
        script = installer.read_text()
        marker = "$clock = [System.Diagnostics.Stopwatch]::StartNew()"
        assert script.count(marker) == 1
        # Hold the progress clock below its throttle interval deterministically.
        # The separate timeout cases retain the real running clock.
        installer.write_text(
            script.replace(marker, "$clock = [System.Diagnostics.Stopwatch]::new()")
        )
    command = _native_event_command(installer, program, platform)
    command += [
        "--operation-id" if platform == "linux-64" else "-OperationId",
        "a" * 32,
    ]
    output, errors = tmp_path / "events.jsonl", tmp_path / "diagnostics"
    with output.open("w") as stdout, errors.open("w") as stderr:
        process = subprocess.Popen(
            command,
            env=environment,
            stdout=stdout,
            stderr=stderr,
            text=True,
            start_new_session=True,
        )
        try:
            assert server.started.wait(15), errors.read_text()
            _wait_for_progress(output, process, errors)
            server.release.set()
            process.wait(timeout=30)
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
    result = subprocess.CompletedProcess(
        command, process.returncode, output.read_text(), errors.read_text()
    )
    _assert_event_outcome(result, program, profile, "success")
    progress = [
        event["bytes_completed"]
        for event in _records(output)
        if event["bytes_total"] == len(server.artifact)
    ]
    assert progress[0] == 0 and progress[-1] == len(server.artifact)
    assert progress == sorted(set(progress))
    assert any(0 < value < len(server.artifact) for value in progress)


@pytest.mark.parametrize("platform", ["linux-64", "win-64"])
@pytest.mark.parametrize(
    "mode",
    [
        "downgrade",
        "redirect_loop",
        "truncated",
        "oversized_header",
        "oversized_chunked",
        "relative_redirect",
        "untrusted_certificate",
    ],
)
def test_native_https_redirect_integrity_and_transport_failures(
    tmp_path: Path, server: _DownloadServer, platform: str, mode: str
) -> None:
    server.mode = mode
    installer, program, environment = _native_download_fixture(
        tmp_path,
        server,
        "documents",
        platform,
        trust_certificate=mode != "untrusted_certificate",
    )
    command = _native_event_command(installer, program, platform)
    command += [
        "--operation-id" if platform == "linux-64" else "-OperationId",
        "a" * 32,
    ]
    result = subprocess.run(
        command,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    success = mode == "relative_redirect"
    _assert_event_outcome(
        result,
        program,
        "documents",
        "success" if success else "transfer_failed",
        before_adoption=not success,
    )
    events = [json.loads(line) for line in result.stdout.splitlines()]
    if not success:
        expected = (
            "verification_failed" if mode.startswith("oversized") else "download_failed"
        )
        assert events[-1]["error_code"] == expected, result.stderr
        assert events[-1]["retryable"] is (expected == "download_failed")
        assert not any(
            event["bytes_completed"] == len(server.artifact) for event in events
        )
    if mode == "downgrade":
        assert server.http_hits == 0
    if mode == "relative_redirect":
        assert "/payload" in server.paths
    if mode == "untrusted_certificate":
        assert not server.paths
    assert not tuple(program.glob(".carlina-install*"))


@pytest.mark.parametrize("platform", ["linux-64", "win-64"])
@pytest.mark.parametrize("kind", ["directory", "symlink"])
def test_cancellation_control_revalidated_during_native_body_read(
    tmp_path: Path, server: _DownloadServer, platform: str, kind: str
) -> None:
    installer, program, environment = _native_download_fixture(
        tmp_path, server, "documents", platform
    )
    request = tmp_path / "caller control"
    command = _native_event_command(installer, program, platform)
    command += [
        "--operation-id" if platform == "linux-64" else "-OperationId",
        "a" * 32,
    ]
    command += [
        "--cancel-file" if platform == "linux-64" else "-CancelFile",
        str(request),
    ]
    output, errors = tmp_path / "events.jsonl", tmp_path / "diagnostics"
    with output.open("w") as stdout, errors.open("w") as stderr:
        process = subprocess.Popen(
            command,
            env=environment,
            stdout=stdout,
            stderr=stderr,
            text=True,
            start_new_session=True,
        )
        try:
            assert server.started.wait(15), errors.read_text()
            _wait_for_progress(output, process, errors)
            if kind == "directory":
                request.mkdir()
            else:
                target = tmp_path / "untouched target"
                target.write_bytes(b"caller-owned secret contents")
                request.symlink_to(target)
            process.wait(timeout=6)
            assert not server.release.is_set()
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
    result = subprocess.CompletedProcess(
        command, process.returncode, output.read_text(), errors.read_text()
    )
    _assert_event_outcome(
        result, program, "documents", "transfer_failed", before_adoption=True
    )
    terminal = _records(output)[-1]
    assert terminal["error_code"] == "operation_failed"
    assert not terminal["retryable"]
    assert request.is_dir() if kind == "directory" else request.is_symlink()
    if kind == "symlink":
        assert request.read_bytes() == b"caller-owned secret contents"
    assert not tuple(program.glob(".carlina-install*"))


@pytest.mark.parametrize("platform", ["linux-64", "win-64"])
@pytest.mark.parametrize("stage", ["headers", "body"])
def test_native_request_deadline_includes_stalled_response_body(
    tmp_path: Path, server: _DownloadServer, platform: str, stage: str
) -> None:
    server.mode = stage
    installer, program, environment = _native_download_fixture(
        tmp_path, server, "documents", platform
    )
    script = installer.read_text()
    marker = (
        "--max-time 900" if platform == "linux-64" else "$downloadTimeoutSeconds = 900"
    )
    assert script.count(marker) == 1
    installer.write_text(script.replace(marker, marker.replace("900", "2")))
    command = _native_event_command(installer, program, platform)
    command += [
        "--operation-id" if platform == "linux-64" else "-OperationId",
        "a" * 32,
    ]
    process = subprocess.Popen(
        command,
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
    result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    _assert_event_outcome(result, program, "documents", "offline", before_adoption=True)
    assert not tuple(program.glob(".carlina-install*"))


def test_shell_cancellation_reaps_a_download_that_ignores_term(tmp_path: Path) -> None:
    installer, program, environment = _event_fixture(
        tmp_path, "documents", "linux-64", "success"
    )
    pid_path = tmp_path / "stubborn download pid"
    environment["CARLINA_TEST_CURL_PID"] = str(pid_path)
    tmp_path.joinpath("shim/curl").write_text(
        f"#!{sys.executable}\n"
        "import os, signal, sys, time\nfrom pathlib import Path\n"
        "args = sys.argv[1:]\ntarget = Path(args[args.index('-o') + 1])\n"
        "if args[-1].endswith('/index.json'):\n"
        "    target.write_bytes(Path(os.environ['CARLINA_TEST_INDEX']).read_bytes())\n"
        "else:\n"
        "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "    handle = target.open('wb')\n"
        "    handle.write(Path(os.environ['CARLINA_TEST_ARTIFACT']).read_bytes()[:50]); handle.flush()\n"
        "    Path(os.environ['CARLINA_TEST_CURL_PID']).write_text(str(os.getpid()))\n"
        "    while True: time.sleep(0.1)\n"
    )
    request = tmp_path / "cancel request"
    command = _native_event_command(installer, program, "linux-64")
    command += ["--operation-id", "a" * 32, "--cancel-file", str(request)]
    output, errors = tmp_path / "events.jsonl", tmp_path / "diagnostics"
    with output.open("w") as stdout, errors.open("w") as stderr:
        process = subprocess.Popen(
            command,
            env=environment,
            stdout=stdout,
            stderr=stderr,
            text=True,
            start_new_session=True,
        )
        try:
            _wait_for_progress(output, process, errors)
            child_pid = int(pid_path.read_text())
            started = time.monotonic()
            request.touch()
            process.wait(timeout=6)
            assert 1.8 < time.monotonic() - started < 5
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)
    result = subprocess.CompletedProcess(
        command, process.returncode, output.read_text(), errors.read_text()
    )
    _assert_event_outcome(
        result, program, "documents", "cancelled", before_adoption=True
    )
    assert result.returncode == 130
    assert not tuple(program.glob(".carlina-install*"))
