from __future__ import annotations

import base64
import gzip
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import time
from dataclasses import replace
from pathlib import Path

import pytest

from carlina.core import publisher_bootstrap as bootstrap_core
from carlina.core.publisher_bootstrap import (
    PublisherBootstrapError,
    PublisherBootstrapPlan,
    PublisherUnpacker,
    parse_publisher_bootstrap_plan,
)
from carlina.core.publisher_bootstrap_scripts import (
    MAX_EMBEDDED_SKELETON_BYTES,
    publisher_bootstrap_filename,
    render_publisher_bootstrap,
)

pytestmark = [pytest.mark.fast, pytest.mark.contract]


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def publisher_bootstrap_fixture(
    monkeypatch: pytest.MonkeyPatch,
    *,
    platform: str = "linux-64",
    unpacker_bytes: bytes | None = None,
) -> tuple[PublisherBootstrapPlan, bytes, bytes, dict[str, bytes], str, bytes]:
    """Build a synthetic pinned plan for publisher and outer-adapter tests."""

    archive = bytearray(2048)
    package_payloads = {
        "https://files.pythonhosted.org/packages/ab/cd/demo-1.2.3-py3-none-any.whl": b"demo-wheel",
        f"https://conda.anaconda.org/conda-forge/{platform}/libdemo-1.2.3-0.conda": b"conda-package",
    }
    package_specs = [
        {
            "member_name": "pypi/demo-1.2.3-py3-none-any.whl",
            "offset_data": 512,
            "url": next(iter(package_payloads)),
            "component_ref": "pypi:demo=1.2.3",
        },
        {
            "member_name": f"channel/{platform}/libdemo-1.2.3-0.conda",
            "offset_data": 1024,
            "url": list(package_payloads)[1],
            "component_ref": "conda-forge::libdemo=1.2.3",
        },
    ]
    downloads: list[dict[str, object]] = []
    for spec in package_specs:
        package = package_payloads[spec["url"]]
        offset = spec["offset_data"]
        archive[offset : offset + len(package)] = package
        downloads.append(
            {
                **spec,
                "size": len(package),
                "sha256": _sha256(package),
            }
        )

    selected_unpacker_bytes = unpacker_bytes
    if selected_unpacker_bytes is None:
        selected_unpacker_bytes = (
            b"#!/bin/sh\n"
            b"set -eu\n"
            b'cp "$1" "$CARLINA_ARCHIVE_CAPTURE"\n'
            b"printf 'executed\\n' > \"$CARLINA_UNPACKER_MARKER\"\n"
            b"printf 'native-out\\n'\n"
            b"printf 'native-err\\n' >&2\n"
            b'exit "${CARLINA_UNPACKER_EXIT:-0}"\n'
        )
    unpacker_name = (
        "pixi-unpack-x86_64-pc-windows-msvc.exe"
        if platform == "win-64"
        else "pixi-unpack-x86_64-unknown-linux-musl"
    )
    unpacker_url = (
        "https://github.com/Quantco/pixi-pack/releases/download/v0.7.10/"
        + unpacker_name
    )
    unpacker = PublisherUnpacker(
        name=unpacker_name,
        url=unpacker_url,
        size=len(selected_unpacker_bytes),
        sha256=_sha256(selected_unpacker_bytes),
    )
    monkeypatch.setitem(bootstrap_core._UNPACKER_POLICY, platform, unpacker)

    archive_bytes = bytes(archive)
    skeleton = gzip.compress(bytes(2048), mtime=0)
    payload: dict[str, object] = {
        "format": "carlina.publisher-bootstrap.plan",
        "schema_version": 1,
        "platform": platform,
        "application_profile": "documents",
        "version": "1.2.3",
        "archive": {"size": len(archive_bytes), "sha256": _sha256(archive_bytes)},
        "skeleton": {
            "filename": "skeleton.tar.gz",
            "size": len(skeleton),
            "sha256": _sha256(skeleton),
            "uncompressed_size": len(archive_bytes),
        },
        "downloads": downloads,
        "unpacker": {
            "name": unpacker.name,
            "url": unpacker.url,
            "size": unpacker.size,
            "sha256": unpacker.sha256,
        },
    }
    return (
        parse_publisher_bootstrap_plan(payload),
        skeleton,
        archive_bytes,
        {**package_payloads, unpacker_url: selected_unpacker_bytes},
        unpacker_url,
        selected_unpacker_bytes,
    )


def _run_bash(
    tmp_path: Path,
    artifact: bytes,
    downloads: dict[str, bytes],
    *,
    unpacker_exit: int = 0,
    existing_environment: bool = False,
    terminate_group_on_curl_wait: bool = False,
    progress_records: bool = False,
    cancel_file: Path | str | None = None,
    create_cancel_after_first_download: bool = False,
    wait_on_cleanup: bool = False,
    fail_cleanup: bool = False,
    curl_wait_signal: int | None = None,
    extra_args: tuple[str, ...] = (),
) -> tuple[subprocess.CompletedProcess[str], list[str], Path, Path, Path]:
    if os.name == "nt":
        pytest.skip("The Linux publisher bootstrap uses a POSIX host.")
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("Bash is required to execute the generated Linux bootstrap.")

    destination = tmp_path / "install"
    destination.mkdir(parents=True)
    script = destination / "setup.sh"
    script.write_bytes(artifact)
    if existing_environment:
        environment_dir = destination / "env"
        environment_dir.mkdir()
        (environment_dir / "keep.txt").write_text(
            "existing environment", encoding="utf-8"
        )
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    curl = shim_dir / "curl"
    curl.write_text(
        "#!/usr/bin/env python3\n"
        "import base64, json, os, sys\n"
        "url = sys.argv[-1]\n"
        "with open(os.environ['CARLINA_CURL_LOG'], 'a', encoding='utf-8') as log:\n"
        "    log.write(url + '\\n')\n"
        "with open(os.environ['CARLINA_CURL_FIXTURE'], encoding='utf-8') as source:\n"
        "    payloads = json.load(source)\n"
        "if url not in payloads:\n"
        "    raise SystemExit(22)\n"
        "wait_marker = os.environ.get('CARLINA_CURL_WAIT_MARKER')\n"
        "wait_url = os.environ.get('CARLINA_CURL_WAIT_URL')\n"
        "if wait_marker and (not wait_url or url == wait_url):\n"
        "    import time\n"
        "    with open(wait_marker, 'w', encoding='utf-8') as marker:\n"
        "        marker.write(url)\n"
        "    release_marker = os.environ.get('CARLINA_CURL_RELEASE_MARKER')\n"
        "    while not release_marker or not os.path.exists(release_marker):\n"
        "        time.sleep(0.05)\n"
        "payload = base64.b64decode(payloads[url])\n"
        "if '-o' in sys.argv:\n"
        "    with open(sys.argv[sys.argv.index('-o') + 1], 'wb') as target: target.write(payload)\n"
        "else:\n"
        "    sys.stdout.buffer.write(payload)\n",
        encoding="utf-8",
    )
    curl.chmod(0o755)
    # These tiny mock inputs log to regular files. Real prlimit would cap those
    # logs too; the actual kernel/file-size bound has a separate real-TLS lane.
    prlimit = shim_dir / "prlimit"
    prlimit.write_text(
        '#!/bin/sh\nwhile [ "$1" != -- ]; do shift; done\nshift\nexec "$@"\n',
        encoding="utf-8",
    )
    prlimit.chmod(0o755)
    fixture_path = tmp_path / "curl-fixture.json"
    fixture_path.write_text(
        json.dumps(
            {
                url: base64.b64encode(payload).decode("ascii")
                for url, payload in downloads.items()
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    log_path = tmp_path / "curl-urls.txt"
    capture_path = tmp_path / "captured-archive.tar"
    marker_path = tmp_path / "unpacker-executed.txt"
    wait_path = tmp_path / "curl-waiting.txt"
    release_path = tmp_path / "curl-release.txt"
    cleanup_wait_path = tmp_path / "cleanup-waiting.txt"
    cleanup_release_path = tmp_path / "cleanup-release.txt"
    output_path = tmp_path / "bootstrap-stdout.txt"
    environment = os.environ.copy()
    environment.update(
        {
            "PATH": os.pathsep.join((str(shim_dir), environment.get("PATH", ""))),
            "CARLINA_CURL_FIXTURE": str(fixture_path),
            "CARLINA_CURL_LOG": str(log_path),
            "CARLINA_ARCHIVE_CAPTURE": str(capture_path),
            "CARLINA_UNPACKER_MARKER": str(marker_path),
            "CARLINA_UNPACKER_EXIT": str(unpacker_exit),
        }
    )
    if terminate_group_on_curl_wait:
        environment["CARLINA_CURL_WAIT_MARKER"] = str(wait_path)
    if curl_wait_signal is not None:
        environment["CARLINA_CURL_WAIT_MARKER"] = str(wait_path)
    if create_cancel_after_first_download:
        environment["CARLINA_CURL_WAIT_MARKER"] = str(wait_path)
        environment["CARLINA_CURL_WAIT_URL"] = next(iter(downloads))
        environment["CARLINA_CURL_RELEASE_MARKER"] = str(release_path)
    if wait_on_cleanup or fail_cleanup:
        rm = shim_dir / "rm"
        rm.write_text(
            "#!/bin/sh\n"
            '/bin/rm "$@" || exit $?\n'
            'if [ "$#" -eq 3 ] && [ "$1" = \'-rf\' ] && [ "$2" = \'--\' ]; then\n'
            '  target="$3"\n'
            '  case "${target##*/}" in .carlina-publisher.*)\n'
            '    [ ! -e "$target" ] || exit 73\n'
            "    if [ \"${CARLINA_FAIL_CLEANUP:-}\" = '1' ]; then exit 73; fi\n"
            '    if [ -n "${CARLINA_CLEANUP_WAIT_MARKER:-}" ]; then\n'
            '      : > "$CARLINA_CLEANUP_WAIT_MARKER"\n'
            '      while [ ! -e "$CARLINA_CLEANUP_RELEASE_MARKER" ]; do sleep 0.02; done\n'
            "    fi\n"
            "    ;; esac\n"
            "fi\n",
            encoding="utf-8",
        )
        rm.chmod(0o755)
        if wait_on_cleanup:
            environment["CARLINA_CLEANUP_WAIT_MARKER"] = str(cleanup_wait_path)
            environment["CARLINA_CLEANUP_RELEASE_MARKER"] = str(cleanup_release_path)
        if fail_cleanup:
            environment["CARLINA_FAIL_CLEANUP"] = "1"
    command = [bash, str(script)]
    if progress_records:
        command.append("--progress-records-v1")
    if cancel_file is not None:
        command.extend(("--cancel-file", str(cancel_file)))
    command.extend(extra_args)
    if (
        terminate_group_on_curl_wait
        or curl_wait_signal is not None
        or create_cancel_after_first_download
        or wait_on_cleanup
    ):
        output_stream = output_path.open("w+", encoding="utf-8")
        process = subprocess.Popen(
            command,
            cwd=destination,
            env=environment,
            stdout=output_stream,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        waits_for_download = (
            terminate_group_on_curl_wait
            or curl_wait_signal is not None
            or create_cancel_after_first_download
        )
        if waits_for_download:
            deadline = time.monotonic() + 10
            while (
                not wait_path.exists()
                and process.poll() is None
                and time.monotonic() < deadline
            ):
                time.sleep(0.02)
            was_waiting = wait_path.exists()
            scratch_was_created = bool(list(destination.glob(".carlina-publisher.*")))
        else:
            was_waiting = True
            scratch_was_created = True
        if create_cancel_after_first_download:
            if cancel_file is None:
                pytest.fail("A cancellation path is required for this barrier.")
            Path(cancel_file).write_bytes(
                b"opaque publisher cancellation request\x00\xff"
            )
            release_path.touch()
        if wait_on_cleanup:
            cleanup_deadline = time.monotonic() + 10
            while (
                not cleanup_wait_path.exists()
                and process.poll() is None
                and time.monotonic() < cleanup_deadline
            ):
                time.sleep(0.02)
            if not cleanup_wait_path.exists():
                if process.poll() is None:
                    process.kill()
                output_stream.close()
                pytest.fail("The scratch cleanup barrier was not reached.")
            output_stream.flush()
            output_stream.seek(0)
            assert not any(
                line.startswith("carlina.publisher-bootstrap/1 result ")
                for line in output_stream.read().splitlines()
            )
            assert list(destination.glob(".carlina-publisher.*")) == []
            cleanup_release_path.touch()
        elif (
            terminate_group_on_curl_wait or curl_wait_signal is not None
        ) and process.poll() is None:
            os.killpg(process.pid, curl_wait_signal or signal.SIGTERM)
        try:
            _, stderr = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            output_stream.close()
            pytest.fail(
                "The isolated bootstrap process group did not stop after SIGTERM."
            )
        output_stream.flush()
        output_stream.seek(0)
        stdout = output_stream.read()
        output_stream.close()
        result = subprocess.CompletedProcess(
            command, process.returncode, stdout, stderr
        )
        if waits_for_download and (not was_waiting or not scratch_was_created):
            pytest.fail(
                "The curl shim did not wait after bootstrap scratch was created."
            )
    else:
        result = subprocess.run(
            command,
            cwd=destination,
            env=environment,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    urls = (
        log_path.read_text(encoding="utf-8").splitlines() if log_path.exists() else []
    )
    return result, urls, destination, capture_path, marker_path


def _assert_progress_protocol(
    output: str,
    *,
    terminal: str,
) -> list[tuple[str, str, str, str]]:
    output.encode("ascii")
    lines = output.splitlines()
    prefix = "carlina.publisher-bootstrap/1"
    assert lines[0] == f"{prefix} ready"
    assert lines[-1] == terminal
    assert lines.count(f"{prefix} ready") == 1
    assert sum(line.startswith(f"{prefix} result ") for line in lines) == 1
    assert re.fullmatch(
        rf"{re.escape(prefix)} result (succeeded null false|cancelled cancelled true|failed (operation_failed|verification_failed|download_failed) (true|false))",
        lines[-1],
    )
    if lines[-1].startswith(f"{prefix} result failed "):
        _, _, _, code, retryable = lines[-1].split()
        assert retryable == str(code == "download_failed").lower()
    phases: list[tuple[str, str, str, str]] = []
    phase_pattern = re.compile(
        rf"{re.escape(prefix)} phase (preflight|download|verification|unpacking|environment) "
        r"(true|false) (null|0|[1-9][0-9]{0,14}) (null|0|[1-9][0-9]{0,14})"
    )
    for line in lines[1:-1]:
        match = phase_pattern.fullmatch(line)
        assert match, f"Malformed or unexpected progress record: {line!r}"
        phase, safe, completed, total = match.groups()
        if completed == "null" or total == "null":
            assert completed == total == "null"
        else:
            assert phase == "download"
            assert int(total) > 0
            assert int(completed) <= int(total)
        if phase == "download":
            assert completed != "null" and total != "null"
        else:
            assert completed == total == "null"
        phases.append((phase, safe, completed, total))
    return phases


def _powershell_mock_artifact(
    artifact: bytes,
    downloads: dict[str, bytes],
) -> bytes:
    text = artifact.decode("utf-8")
    parameter_end = "    [string]$CancelFile\n)\n"
    mock_response_type = """class MockPublisherResponse {
    [int]$StatusCode
    [hashtable]$Headers
    [long]$ContentLength
    [System.IO.MemoryStream]$Stream

    MockPublisherResponse([byte[]]$Payload) {
        $this.StatusCode = 200
        $this.Headers = @{}
        $this.ContentLength = $Payload.Length
        $this.Stream = [System.IO.MemoryStream]::new($Payload)
    }

    [System.IO.Stream] GetResponseStream() { return $this.Stream }
    [void] Dispose() { $this.Stream.Dispose() }
}

$script:PublisherTestPayloads = @{
"""
    for url, payload in downloads.items():
        quoted_url = "'" + url.replace("'", "''") + "'"
        mock_response_type += (
            f"    {quoted_url} = "
            f"[Convert]::FromBase64String('{base64.b64encode(payload).decode('ascii')}')\n"
        )
    mock_response_type += "}\n"
    if parameter_end not in text:
        raise AssertionError("The PowerShell parameter block changed unexpectedly.")
    text = text.replace(parameter_end, parameter_end + mock_response_type, 1)

    response_pattern = re.compile(
        r"(?ms)^function Get-PublisherResponse\(\[Uri\]\$Uri\) \{.*?^\}\n"
    )
    response_replacement = """function Get-PublisherResponse([Uri]$Uri) {
    [IO.File]::AppendAllText($env:CARLINA_POWERSHELL_URL_LOG, $Uri.AbsoluteUri + [Environment]::NewLine)
    if ($env:CARLINA_POWERSHELL_FAIL_URL -eq $Uri.AbsoluteUri) { throw 'Synthetic transport failure.' }
    $payload = $script:PublisherTestPayloads[$Uri.AbsoluteUri]
    if ($null -eq $payload) { throw 'Synthetic response is missing.' }
    return [MockPublisherResponse]::new($payload)
}
"""
    text, substitutions = response_pattern.subn(response_replacement, text, count=1)
    if substitutions != 1:
        raise AssertionError("The PowerShell HTTP factory seam was not found.")

    # Assembly/protocol fixtures retain an in-memory transport. The real event
    # transport is exercised independently over loopback TLS.
    event_pattern = re.compile(r"(?ms)^function Save-HttpsEventFile\(.*?^\}\n")
    event_replacement = """function Save-HttpsEventFile([string]$Url, [string]$Path, [long]$ExpectedSize = 0) {
    $response = Get-PublisherResponse ([Uri]$Url)
    $output = $null
    try {
        if ($response.ContentLength -ne $ExpectedSize) { throw 'Synthetic unexpected content length.' }
        $output = [IO.File]::Open($Path, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
        $response.GetResponseStream().CopyTo($output)
        $output.Flush($true)
    } finally {
        if ($null -ne $output) { $output.Dispose() }
        $response.Dispose()
    }
}
"""
    text, substitutions = event_pattern.subn(event_replacement, text, count=1)
    if substitutions != 1:
        raise AssertionError("The shared event HTTP transport seam was not found.")

    native_pattern = re.compile(
        r"(?ms)^function Invoke-NativeUnpackerProcess\(\[string\]\$UnpackerPath, \[string\]\$ArchivePath\) \{.*?^\}\n"
    )
    native_replacement = """function Invoke-NativeUnpackerProcess([string]$UnpackerPath, [string]$ArchivePath) {
    [IO.File]::Copy($ArchivePath, $env:CARLINA_POWERSHELL_ARCHIVE_CAPTURE, $true)
    [IO.File]::WriteAllText($env:CARLINA_POWERSHELL_UNPACKER_MARKER, 'executed')
    Write-Output 'native-out'
    Write-Error 'native-err' -ErrorAction Continue
    $global:LASTEXITCODE = [int]$env:CARLINA_POWERSHELL_UNPACKER_EXIT
}
"""
    text, substitutions = native_pattern.subn(native_replacement, text, count=1)
    if substitutions != 1:
        raise AssertionError("The PowerShell native unpacker seam was not found.")
    return text.encode("utf-8")


def _run_powershell(
    tmp_path: Path,
    artifact: bytes,
    downloads: dict[str, bytes],
    *,
    cancel_file: Path | None = None,
    fail_url: str | None = None,
    unpacker_exit: int = 0,
    extra_args: tuple[str, ...] = (),
    progress_records: bool = True,
) -> tuple[subprocess.CompletedProcess[str], list[str], Path, Path, Path]:
    pwsh = shutil.which("pwsh")
    if pwsh is None:
        pytest.skip("PowerShell Core is not installed for portable template checks.")
    destination = tmp_path / "install"
    destination.mkdir(parents=True)
    script = destination / "setup.ps1"
    script.write_bytes(_powershell_mock_artifact(artifact, downloads))
    url_log = tmp_path / "powershell-urls.txt"
    capture_path = tmp_path / "powershell-captured-archive.tar"
    marker_path = tmp_path / "powershell-unpacker-executed.txt"
    environment = os.environ.copy()
    environment.update(
        {
            "CARLINA_POWERSHELL_URL_LOG": str(url_log),
            "CARLINA_POWERSHELL_ARCHIVE_CAPTURE": str(capture_path),
            "CARLINA_POWERSHELL_UNPACKER_MARKER": str(marker_path),
            "CARLINA_POWERSHELL_UNPACKER_EXIT": str(unpacker_exit),
        }
    )
    if fail_url is not None:
        environment["CARLINA_POWERSHELL_FAIL_URL"] = fail_url
    command = [pwsh, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(script)]
    if progress_records:
        command.append("-ProgressRecordsV1")
    if cancel_file is not None:
        command.extend(("-CancelFile", str(cancel_file)))
    command.extend(extra_args)
    result = subprocess.run(
        command,
        cwd=destination,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    urls = url_log.read_text(encoding="utf-8").splitlines() if url_log.exists() else []
    return result, urls, destination, capture_path, marker_path


@pytest.mark.parametrize("platform", ["linux-64", "win-64"])
def test_native_scripts_are_deterministic_and_named_by_platform(
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
) -> None:
    plan, skeleton, *_ = publisher_bootstrap_fixture(monkeypatch, platform=platform)

    first = render_publisher_bootstrap(plan, skeleton)
    second = render_publisher_bootstrap(plan, skeleton)

    suffix = ".sh" if platform == "linux-64" else ".ps1"
    assert (
        publisher_bootstrap_filename(plan)
        == f"carlina-documents-1.2.3-{platform}{suffix}"
    )
    assert first == second
    assert b"@@" not in first
    if platform == "linux-64":
        assert first.startswith(b"#!/usr/bin/env bash\n")
    else:
        assert first.startswith(b"# Generated Carlina Documents publisher bootstrap.")


@pytest.mark.parametrize("problem", ["size", "digest"])
def test_renderer_rejects_scaffold_size_or_digest_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    problem: str,
) -> None:
    plan, skeleton, *_ = publisher_bootstrap_fixture(monkeypatch)
    if problem == "size":
        changed = skeleton + b"x"
    else:
        altered = bytearray(skeleton)
        altered[-1] ^= 1
        changed = bytes(altered)

    with pytest.raises(PublisherBootstrapError, match="scaffold"):
        render_publisher_bootstrap(plan, changed)


def test_renderer_rejects_scaffold_over_64_mib(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, *_ = publisher_bootstrap_fixture(monkeypatch)

    with pytest.raises(PublisherBootstrapError, match="too large"):
        render_publisher_bootstrap(plan, b"x" * (MAX_EMBEDDED_SKELETON_BYTES + 1))


def test_filename_and_renderer_revalidate_mutated_dataclass_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, skeleton, *_ = publisher_bootstrap_fixture(monkeypatch)
    mutated = replace(plan, version="01.2.3")

    with pytest.raises(PublisherBootstrapError, match="canonical PEP 440"):
        publisher_bootstrap_filename(mutated)
    with pytest.raises(PublisherBootstrapError, match="canonical PEP 440"):
        render_publisher_bootstrap(mutated, skeleton)


def test_generated_bash_reconstructs_exact_archive_before_unpacker_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, skeleton, archive, downloads, unpacker_url, _ = publisher_bootstrap_fixture(
        monkeypatch
    )
    artifact = render_publisher_bootstrap(plan, skeleton)

    result, urls, destination, capture, marker = _run_bash(
        tmp_path, artifact, downloads
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == (
        "Preparing the Carlina Documents runtime. Internet access is required.\n"
        "native-out\n"
    )
    assert "native-err\n" in result.stderr
    assert capture.read_bytes() == archive
    assert hashlib.sha256(capture.read_bytes()).hexdigest() == plan.archive.sha256
    assert marker.read_text(encoding="utf-8") == "executed\n"
    assert urls == [package.url for package in plan.downloads] + [unpacker_url]
    assert list(destination.glob(".carlina-publisher.*")) == []


def test_generated_bash_progress_protocol_orders_verified_bytes_and_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, skeleton, archive, downloads, unpacker_url, _ = publisher_bootstrap_fixture(
        monkeypatch
    )

    result, urls, destination, capture, marker = _run_bash(
        tmp_path,
        render_publisher_bootstrap(plan, skeleton),
        downloads,
        progress_records=True,
        cancel_file=tmp_path / "cancel-not-requested",
    )

    phases = _assert_progress_protocol(
        result.stdout,
        terminal="carlina.publisher-bootstrap/1 result succeeded null false",
    )
    expected = [
        ("preflight", "true", "null", "null"),
        ("preflight", "false", "null", "null"),
        ("preflight", "true", "null", "null"),
        ("preflight", "false", "null", "null"),
        ("verification", "false", "null", "null"),
        ("verification", "true", "null", "null"),
        ("unpacking", "false", "null", "null"),
        ("unpacking", "true", "null", "null"),
    ]
    for package in plan.downloads:
        size = str(package.size)
        expected.extend(
            [
                ("download", "true", "0", size),
                ("verification", "false", "null", "null"),
                ("download", "true", size, size),
                ("unpacking", "false", "null", "null"),
                ("unpacking", "true", "null", "null"),
            ]
        )
    unpacker_size = str(plan.unpacker.size)
    expected.extend(
        [
            ("verification", "false", "null", "null"),
            ("verification", "true", "null", "null"),
            ("download", "true", "0", unpacker_size),
            ("verification", "false", "null", "null"),
            ("download", "true", unpacker_size, unpacker_size),
            ("environment", "false", "null", "null"),
            ("environment", "true", "null", "null"),
        ]
    )
    assert phases == expected
    assert result.returncode == 0, result.stderr
    assert urls == [package.url for package in plan.downloads] + [unpacker_url]
    assert "Preparing the Carlina Documents runtime." in result.stderr
    assert "native-out\n" in result.stderr
    assert "native-err\n" in result.stderr
    assert capture.read_bytes() == archive
    assert marker.read_text(encoding="utf-8") == "executed\n"
    assert list(destination.glob(".carlina-publisher.*")) == []


def test_generated_bash_rejects_unknown_arguments_before_progress_or_scratch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, skeleton, _, downloads, _, _ = publisher_bootstrap_fixture(monkeypatch)

    result, urls, destination, _, _ = _run_bash(
        tmp_path,
        render_publisher_bootstrap(plan, skeleton),
        downloads,
        progress_records=True,
        extra_args=("--not-a-publisher-option",),
    )

    assert result.returncode == 2
    assert result.stdout == ""
    assert urls == []
    assert list(destination.glob(".carlina-publisher.*")) == []


def test_generated_bash_cancels_before_work_without_reading_request_contents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, skeleton, _, downloads, _, _ = publisher_bootstrap_fixture(monkeypatch)
    cancel_file = tmp_path / "cancel.request"
    opaque_request = b"opaque request\x00with non-text bytes\xff"
    cancel_file.write_bytes(opaque_request)

    result, urls, destination, capture, marker = _run_bash(
        tmp_path / "run",
        render_publisher_bootstrap(plan, skeleton),
        downloads,
        progress_records=True,
        cancel_file=cancel_file,
    )

    _assert_progress_protocol(
        result.stdout,
        terminal="carlina.publisher-bootstrap/1 result cancelled cancelled true",
    )
    assert result.returncode == 130
    assert urls == []
    assert cancel_file.read_bytes() == opaque_request
    assert not capture.exists()
    assert not marker.exists()
    assert list(destination.glob(".carlina-publisher.*")) == []


def test_generated_bash_accepts_dot_components_and_newline_cancel_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, skeleton, _, downloads, _, _ = publisher_bootstrap_fixture(monkeypatch)
    cancel_parent = tmp_path / "cancel-parent"
    nested = cancel_parent / "nested"
    nested.mkdir(parents=True)
    opaque_request = b"opaque path request\x00\xfc"
    effective_path = cancel_parent / "cancel\nrequest"
    effective_path.write_bytes(opaque_request)
    lexical_path = f"{cancel_parent}/./nested/../cancel\nrequest"

    result, urls, destination, _, _ = _run_bash(
        tmp_path / "run",
        render_publisher_bootstrap(plan, skeleton),
        downloads,
        progress_records=True,
        cancel_file=lexical_path,
    )

    _assert_progress_protocol(
        result.stdout,
        terminal="carlina.publisher-bootstrap/1 result cancelled cancelled true",
    )
    assert result.returncode == 130
    assert urls == []
    assert effective_path.read_bytes() == opaque_request
    assert list(destination.glob(".carlina-publisher.*")) == []


def test_generated_bash_cancels_after_blocked_verified_dependency_before_next(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, skeleton, _, downloads, unpacker_url, _ = publisher_bootstrap_fixture(
        monkeypatch
    )
    cancel_file = tmp_path / "cancel.request"

    result, urls, destination, capture, marker = _run_bash(
        tmp_path / "run",
        render_publisher_bootstrap(plan, skeleton),
        downloads,
        progress_records=True,
        cancel_file=cancel_file,
        create_cancel_after_first_download=True,
    )

    phases = _assert_progress_protocol(
        result.stdout,
        terminal="carlina.publisher-bootstrap/1 result cancelled cancelled true",
    )
    assert result.returncode == 130
    assert urls == [plan.downloads[0].url]
    assert unpacker_url not in urls
    assert phases[-1] == (
        "download",
        "true",
        str(plan.downloads[0].size),
        str(plan.downloads[0].size),
    )
    assert cancel_file.read_bytes() == b"opaque publisher cancellation request\x00\xff"
    assert not capture.exists()
    assert not marker.exists()
    assert list(destination.glob(".carlina-publisher.*")) == []


@pytest.mark.parametrize("invalid_path", ["directory", "symlink", "missing-parent"])
def test_generated_bash_rejects_invalid_cancel_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalid_path: str,
) -> None:
    plan, skeleton, _, downloads, _, _ = publisher_bootstrap_fixture(monkeypatch)
    if invalid_path == "directory":
        cancel_file = tmp_path / "cancel-dir"
        cancel_file.mkdir()
    elif invalid_path == "symlink":
        target = tmp_path / "opaque-target"
        target.write_bytes(b"do not touch")
        cancel_file = tmp_path / "cancel-link"
        cancel_file.symlink_to(target)
    else:
        cancel_file = tmp_path / "missing-parent" / "cancel.request"

    result, urls, destination, _, _ = _run_bash(
        tmp_path / "run",
        render_publisher_bootstrap(plan, skeleton),
        downloads,
        progress_records=True,
        cancel_file=cancel_file,
    )

    _assert_progress_protocol(
        result.stdout,
        terminal="carlina.publisher-bootstrap/1 result failed operation_failed false",
    )
    assert result.returncode == 1
    assert urls == []
    if invalid_path == "symlink":
        assert target.read_bytes() == b"do not touch"
    assert list(destination.glob(".carlina-publisher.*")) == []


@pytest.mark.parametrize(
    ("problem", "expected_result", "expected_urls"),
    [
        ("download", "download_failed true", 1),
        ("hash", "verification_failed false", 1),
    ],
)
def test_generated_bash_classifies_download_and_verification_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    problem: str,
    expected_result: str,
    expected_urls: int,
) -> None:
    plan, skeleton, _, downloads, _, _ = publisher_bootstrap_fixture(monkeypatch)
    first_package = plan.downloads[0]
    if problem == "download":
        del downloads[first_package.url]
    else:
        payload = downloads[first_package.url]
        downloads[first_package.url] = bytes([payload[0] ^ 1]) + payload[1:]

    result, urls, destination, _, _ = _run_bash(
        tmp_path,
        render_publisher_bootstrap(plan, skeleton),
        downloads,
        progress_records=True,
    )

    code, retryable = expected_result.split()
    _assert_progress_protocol(
        result.stdout,
        terminal=f"carlina.publisher-bootstrap/1 result failed {code} {retryable}",
    )
    assert result.returncode == 1
    assert urls == [first_package.url][:expected_urls]
    phases = [
        line
        for line in result.stdout.splitlines()
        if line.startswith("carlina.publisher-bootstrap/1 phase download ")
    ]
    assert phases == [
        f"carlina.publisher-bootstrap/1 phase download true 0 {first_package.size}"
    ]
    assert list(destination.glob(".carlina-publisher.*")) == []


def test_generated_bash_classifies_unpacker_failure_without_retryability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, skeleton, _, downloads, unpacker_url, _ = publisher_bootstrap_fixture(
        monkeypatch
    )

    result, urls, destination, capture, marker = _run_bash(
        tmp_path,
        render_publisher_bootstrap(plan, skeleton),
        downloads,
        unpacker_exit=17,
        progress_records=True,
    )

    _assert_progress_protocol(
        result.stdout,
        terminal="carlina.publisher-bootstrap/1 result failed operation_failed false",
    )
    assert result.returncode == 17
    assert urls == [package.url for package in plan.downloads] + [unpacker_url]
    assert capture.exists()
    assert marker.read_text(encoding="utf-8") == "executed\n"
    assert list(destination.glob(".carlina-publisher.*")) == []


def test_generated_bash_does_not_label_signal_130_as_cooperative_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, skeleton, _, downloads, unpacker_url, _ = publisher_bootstrap_fixture(
        monkeypatch
    )

    result, urls, destination, capture, marker = _run_bash(
        tmp_path,
        render_publisher_bootstrap(plan, skeleton),
        downloads,
        progress_records=True,
        curl_wait_signal=signal.SIGINT,
    )

    _assert_progress_protocol(
        result.stdout,
        terminal="carlina.publisher-bootstrap/1 result failed operation_failed false",
    )
    assert result.returncode == 130
    assert urls == [plan.downloads[0].url]
    assert unpacker_url not in urls
    assert not capture.exists()
    assert not marker.exists()
    assert list(destination.glob(".carlina-publisher.*")) == []


def test_generated_bash_emits_terminal_only_after_scratch_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, skeleton, _, downloads, _, _ = publisher_bootstrap_fixture(monkeypatch)

    result, _, destination, _, _ = _run_bash(
        tmp_path,
        render_publisher_bootstrap(plan, skeleton),
        downloads,
        progress_records=True,
        wait_on_cleanup=True,
    )

    _assert_progress_protocol(
        result.stdout,
        terminal="carlina.publisher-bootstrap/1 result succeeded null false",
    )
    assert result.returncode == 0
    assert list(destination.glob(".carlina-publisher.*")) == []


def test_generated_bash_reports_cleanup_failure_as_nonretryable_operation_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, skeleton, _, downloads, _, _ = publisher_bootstrap_fixture(monkeypatch)

    result, _, destination, _, _ = _run_bash(
        tmp_path,
        render_publisher_bootstrap(plan, skeleton),
        downloads,
        progress_records=True,
        fail_cleanup=True,
    )

    _assert_progress_protocol(
        result.stdout,
        terminal="carlina.publisher-bootstrap/1 result failed operation_failed false",
    )
    assert result.returncode == 1
    assert list(destination.glob(".carlina-publisher.*")) == []


def test_generated_powershell_progress_protocol_uses_stderr_for_native_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, skeleton, archive, downloads, unpacker_url, _ = publisher_bootstrap_fixture(
        monkeypatch,
        platform="win-64",
    )

    result, urls, destination, capture, marker = _run_powershell(
        tmp_path,
        render_publisher_bootstrap(plan, skeleton),
        downloads,
        cancel_file=tmp_path / "cancel-not-requested",
    )

    phases = _assert_progress_protocol(
        result.stdout,
        terminal="carlina.publisher-bootstrap/1 result succeeded null false",
    )
    expected = [
        ("preflight", "true", "null", "null"),
        ("preflight", "false", "null", "null"),
        ("verification", "false", "null", "null"),
        ("verification", "true", "null", "null"),
        ("unpacking", "false", "null", "null"),
        ("unpacking", "true", "null", "null"),
    ]
    for package in plan.downloads:
        size = str(package.size)
        expected.extend(
            [
                ("download", "true", "0", size),
                ("verification", "false", "null", "null"),
                ("download", "true", size, size),
                ("unpacking", "false", "null", "null"),
                ("unpacking", "true", "null", "null"),
            ]
        )
    unpacker_size = str(plan.unpacker.size)
    expected.extend(
        [
            ("verification", "false", "null", "null"),
            ("verification", "true", "null", "null"),
            ("download", "true", "0", unpacker_size),
            ("verification", "false", "null", "null"),
            ("download", "true", unpacker_size, unpacker_size),
            ("environment", "false", "null", "null"),
            ("environment", "true", "null", "null"),
        ]
    )
    assert phases == expected
    assert result.returncode == 0, result.stderr
    assert urls == [package.url for package in plan.downloads] + [unpacker_url]
    assert "Preparing the Carlina Documents runtime." in result.stderr
    assert "native-out\n" in result.stderr
    assert "native-err\n" in result.stderr
    assert capture.read_bytes() == archive
    assert marker.read_text(encoding="utf-8") == "executed"
    assert list(destination.glob(".carlina-publisher-*")) == []


def test_generated_powershell_legacy_invocation_emits_no_protocol_records(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, skeleton, _, downloads, _, _ = publisher_bootstrap_fixture(
        monkeypatch,
        platform="win-64",
    )

    result, urls, destination, _, _ = _run_powershell(
        tmp_path,
        render_publisher_bootstrap(plan, skeleton),
        downloads,
        progress_records=False,
    )

    assert result.returncode == 0, result.stderr
    assert "carlina.publisher-bootstrap/1" not in result.stdout
    assert "Preparing the Carlina Documents runtime." in result.stdout
    assert "native-out" in result.stdout
    assert "native-err" in result.stdout
    assert urls == [package.url for package in plan.downloads] + [plan.unpacker.url]
    assert list(destination.glob(".carlina-publisher-*")) == []


@pytest.mark.parametrize(
    ("problem", "code", "retryable"),
    [
        ("download", "download_failed", "true"),
        ("hash", "verification_failed", "false"),
    ],
)
def test_generated_powershell_classifies_download_and_hash_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    problem: str,
    code: str,
    retryable: str,
) -> None:
    plan, skeleton, _, downloads, _, _ = publisher_bootstrap_fixture(
        monkeypatch,
        platform="win-64",
    )
    first_package = plan.downloads[0]
    fail_url = first_package.url if problem == "download" else None
    if problem == "hash":
        payload = downloads[first_package.url]
        downloads[first_package.url] = bytes([payload[0] ^ 1]) + payload[1:]

    result, urls, destination, _, _ = _run_powershell(
        tmp_path,
        render_publisher_bootstrap(plan, skeleton),
        downloads,
        fail_url=fail_url,
    )

    _assert_progress_protocol(
        result.stdout,
        terminal=f"carlina.publisher-bootstrap/1 result failed {code} {retryable}",
    )
    assert result.returncode == 1
    assert urls == [first_package.url]
    assert list(destination.glob(".carlina-publisher-*")) == []


def test_generated_powershell_classifies_native_unpacker_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, skeleton, archive, downloads, unpacker_url, _ = publisher_bootstrap_fixture(
        monkeypatch,
        platform="win-64",
    )

    result, urls, destination, capture, marker = _run_powershell(
        tmp_path,
        render_publisher_bootstrap(plan, skeleton),
        downloads,
        unpacker_exit=17,
    )

    _assert_progress_protocol(
        result.stdout,
        terminal="carlina.publisher-bootstrap/1 result failed operation_failed false",
    )
    assert result.returncode == 1
    assert urls == [package.url for package in plan.downloads] + [unpacker_url]
    assert capture.read_bytes() == archive
    assert marker.read_text(encoding="utf-8") == "executed"
    assert "Publisher bootstrap operation failed." in result.stderr
    assert "native-out\n" in result.stderr
    assert "native-err" in result.stderr
    assert list(destination.glob(".carlina-publisher-*")) == []


def test_generated_powershell_cancels_before_work_and_preserves_opaque_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, skeleton, _, downloads, _, _ = publisher_bootstrap_fixture(
        monkeypatch,
        platform="win-64",
    )
    cancel_file = tmp_path / "cancel.request"
    opaque_request = b"powerShell cancellation request\x00\xfe"
    cancel_file.write_bytes(opaque_request)

    result, urls, destination, capture, marker = _run_powershell(
        tmp_path / "run",
        render_publisher_bootstrap(plan, skeleton),
        downloads,
        cancel_file=cancel_file,
    )

    _assert_progress_protocol(
        result.stdout,
        terminal="carlina.publisher-bootstrap/1 result cancelled cancelled true",
    )
    assert result.returncode == 130
    assert urls == []
    assert cancel_file.read_bytes() == opaque_request
    assert not capture.exists()
    assert not marker.exists()
    assert list(destination.glob(".carlina-publisher-*")) == []


@pytest.mark.parametrize(
    "invalid_type",
    ["directory", "symlink", "dangling-symlink", "missing-parent"],
)
def test_generated_powershell_rejects_invalid_cancel_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalid_type: str,
) -> None:
    plan, skeleton, _, downloads, _, _ = publisher_bootstrap_fixture(
        monkeypatch,
        platform="win-64",
    )
    target: Path | None = None
    if invalid_type == "directory":
        cancel_file = tmp_path / "cancel-dir"
        cancel_file.mkdir()
    elif invalid_type in {"symlink", "dangling-symlink"}:
        target = tmp_path / "opaque-target"
        if invalid_type == "symlink":
            target.write_bytes(b"do not touch")
        cancel_file = tmp_path / "cancel-link"
        try:
            cancel_file.symlink_to(target)
        except OSError as exc:
            pytest.skip(f"The filesystem does not permit symlinks: {exc}")
    else:
        cancel_file = tmp_path / "missing-parent" / "cancel.request"

    result, urls, destination, _, _ = _run_powershell(
        tmp_path / "run",
        render_publisher_bootstrap(plan, skeleton),
        downloads,
        cancel_file=cancel_file,
    )

    _assert_progress_protocol(
        result.stdout,
        terminal="carlina.publisher-bootstrap/1 result failed operation_failed false",
    )
    assert result.returncode == 1
    assert urls == []
    if invalid_type == "symlink":
        assert target is not None
        assert target.read_bytes() == b"do not touch"
    assert list(destination.glob(".carlina-publisher-*")) == []


@pytest.mark.parametrize("argument_case", ["unknown", "cancel-without-progress"])
def test_generated_powershell_rejects_invalid_arguments_before_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    argument_case: str,
) -> None:
    plan, skeleton, _, downloads, _, _ = publisher_bootstrap_fixture(
        monkeypatch,
        platform="win-64",
    )
    if argument_case == "unknown":
        cancel_file = None
        extra_args = ("-UnknownPublisherControl",)
        progress_records = True
    else:
        cancel_file = tmp_path / "cancel.request"
        extra_args = ()
        progress_records = False

    result, urls, destination, _, _ = _run_powershell(
        tmp_path,
        render_publisher_bootstrap(plan, skeleton),
        downloads,
        cancel_file=cancel_file,
        extra_args=extra_args,
        progress_records=progress_records,
    )

    assert result.returncode != 0
    assert result.stdout == ""
    assert urls == []
    assert list(destination.glob(".carlina-publisher-*")) == []


@pytest.mark.parametrize("problem", ["checksum", "truncated", "oversize"])
def test_generated_bash_rejects_invalid_package_download_and_cleans_scratch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    problem: str,
) -> None:
    plan, skeleton, _, downloads, unpacker_url, _ = publisher_bootstrap_fixture(
        monkeypatch
    )
    package_url = plan.downloads[0].url
    expected = downloads[package_url]
    if problem == "checksum":
        downloads[package_url] = b"x" * len(expected)
    elif problem == "truncated":
        downloads[package_url] = expected[:-1]
    else:
        downloads[package_url] = expected + b"x"

    result, urls, destination, capture, marker = _run_bash(
        tmp_path,
        render_publisher_bootstrap(plan, skeleton),
        downloads,
    )

    assert result.returncode != 0
    assert urls == [package_url]
    assert unpacker_url not in urls
    assert not capture.exists()
    assert not marker.exists()
    assert list(destination.glob(".carlina-publisher.*")) == []


def test_generated_bash_rejects_corrupt_embedded_scaffold_before_download(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, skeleton, _, downloads, unpacker_url, _ = publisher_bootstrap_fixture(
        monkeypatch
    )
    artifact = render_publisher_bootstrap(plan, skeleton)
    marker = b"__CARLINA_PUBLISHER_PAYLOAD__\n"
    prefix, separator, encoded = artifact.partition(marker)
    assert separator and encoded
    replacement = b"A" if encoded[:1] != b"A" else b"B"
    tampered = prefix + separator + replacement + encoded[1:]
    if not tampered.endswith(b"\n"):
        tampered += b"\n"

    result, urls, destination, capture, execution_marker = _run_bash(
        tmp_path,
        tampered,
        downloads,
        progress_records=True,
    )

    _assert_progress_protocol(
        result.stdout,
        terminal="carlina.publisher-bootstrap/1 result failed verification_failed false",
    )
    assert result.returncode == 1
    assert urls == []
    assert unpacker_url not in urls
    assert not capture.exists()
    assert not execution_marker.exists()
    assert list(destination.glob(".carlina-publisher.*")) == []


def test_generated_bash_checks_complete_tar_hash_before_unpacker_download(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, skeleton, _, downloads, unpacker_url, _ = publisher_bootstrap_fixture(
        monkeypatch
    )
    wrong_hash = "0" * 64
    if wrong_hash == plan.archive.sha256:
        wrong_hash = "1" * 64
    changed_plan = replace(plan, archive=replace(plan.archive, sha256=wrong_hash))

    result, urls, destination, capture, marker = _run_bash(
        tmp_path,
        render_publisher_bootstrap(changed_plan, skeleton),
        downloads,
    )

    assert result.returncode != 0
    assert urls == [package.url for package in plan.downloads]
    assert unpacker_url not in urls
    assert not capture.exists()
    assert not marker.exists()
    assert list(destination.glob(".carlina-publisher.*")) == []


def test_generated_bash_does_not_execute_unverified_unpacker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, skeleton, _, downloads, unpacker_url, unpacker = publisher_bootstrap_fixture(
        monkeypatch
    )
    downloads[unpacker_url] = b"x" * len(unpacker)

    result, urls, destination, capture, marker = _run_bash(
        tmp_path,
        render_publisher_bootstrap(plan, skeleton),
        downloads,
    )

    assert result.returncode != 0
    assert urls == [package.url for package in plan.downloads] + [unpacker_url]
    assert not capture.exists()
    assert not marker.exists()
    assert list(destination.glob(".carlina-publisher.*")) == []


def test_generated_bash_propagates_unpacker_failure_and_cleans_scratch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, skeleton, _, downloads, unpacker_url, _ = publisher_bootstrap_fixture(
        monkeypatch
    )

    result, urls, destination, capture, marker = _run_bash(
        tmp_path,
        render_publisher_bootstrap(plan, skeleton),
        downloads,
        unpacker_exit=17,
    )

    assert result.returncode == 17
    assert urls == [package.url for package in plan.downloads] + [unpacker_url]
    assert capture.exists()
    assert marker.read_text(encoding="utf-8") == "executed\n"
    assert list(destination.glob(".carlina-publisher.*")) == []


def test_generated_bash_cleans_scratch_after_handled_sigterm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, skeleton, _, downloads, unpacker_url, _ = publisher_bootstrap_fixture(
        monkeypatch
    )

    result, urls, destination, capture, marker = _run_bash(
        tmp_path,
        render_publisher_bootstrap(plan, skeleton),
        downloads,
        terminate_group_on_curl_wait=True,
    )

    assert result.returncode != 0
    assert urls == [plan.downloads[0].url]
    assert unpacker_url not in urls
    assert not capture.exists()
    assert not marker.exists()
    assert list(destination.glob(".carlina-publisher.*")) == []


def test_generated_bash_preserves_existing_environment_without_downloads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, skeleton, _, downloads, _, _ = publisher_bootstrap_fixture(monkeypatch)
    artifact = render_publisher_bootstrap(plan, skeleton)

    result, urls, destination, capture, marker = _run_bash(
        tmp_path,
        artifact,
        downloads,
        existing_environment=True,
    )

    assert result.returncode != 0
    assert urls == []
    assert (destination / "env" / "keep.txt").read_text(encoding="utf-8") == (
        "existing environment"
    )
    assert not capture.exists()
    assert not marker.exists()
    assert list(destination.glob(".carlina-publisher.*")) == []
