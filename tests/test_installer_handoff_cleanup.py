"""A bootstrap failure must not delete a possibly committed managed generation."""

from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import os
import select
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from filelock import FileLock, Timeout
from jsonschema import Draft202012Validator
from test_publisher_bootstrap_scripts import (
    publisher_bootstrap_fixture as _publisher_plan_fixture,
)

from carlina.core.native_preparation_shell import render_shell_preparation_helpers
from carlina.core.publisher_bootstrap_scripts import render_publisher_bootstrap
from scripts.build_release_index import build_release_index
from scripts.render_managed_installers import (
    _powershell_bootstrap_ownership_helpers,
    render_installers,
)

pytestmark = [pytest.mark.fast, pytest.mark.contract]

_EVENT_UNPACKER = b"""#!/bin/sh
set -eu
if [ "${CARLINA_TEST_FORBID_UNPACK:-0}" = 1 ]; then exit 31; fi
printf 'synthetic unpack progress\\n'
if [ "$CARLINA_TEST_EVENT_OUTCOME" = unpack_failure ]; then exit 9; fi
mkdir -p env/bin
cat > env/bin/python <<'PYTHON'
#!/bin/sh
set -eu
if [ "$2" = '-S' ]; then exit 0; fi
last=''
for argument do last=$argument; done
if [ "${CARLINA_TEST_MISSING_RESUME:-0}" = 1 ] && [ "$last" = resume ]; then exit 6; fi
if [ "$CARLINA_TEST_EVENT_OUTCOME" = old_protocol ] && [ "$last" = probe ]; then exit 6; fi
if [ "$2" = '-m' ]; then
  exec "$CARLINA_TEST_REAL_PYTHON" "$CARLINA_TEST_MANAGER" "$@"
fi
exec "$CARLINA_TEST_REAL_PYTHON" "$@"
PYTHON
printf '#!/bin/sh\\nexit 0\\n' > env/bin/pixi
chmod +x env/bin/python env/bin/pixi
cp env/bin/python env/python.exe
cp env/bin/pixi env/bin/pixi.exe
"""

_EVENT_MANAGER = """import os, sys
from dataclasses import replace
from pathlib import Path
from carlina.core.installation_handoff import write_terminal_event
from carlina.core.installation_lifecycle import InstallationLifecycle, InstallationState
args = sys.argv
def option(name):
    return args[args.index(name) + 1]
outcome = os.environ['CARLINA_TEST_EVENT_OUTCOME']
resume = '--resume-bootstrap-claim' in args
if os.environ.get('CARLINA_TEST_REQUIRE_RESUME') == '1':
    assert resume
    frame = Path(option('--adopt-version-dir'), '.carlina-bootstrap-claim').read_text().splitlines()
    assert option('--resume-bootstrap-claim') == frame[1]
record_path = os.environ.get('CARLINA_TEST_MANAGER_CALLS')
if record_path:
    import json
    with Path(record_path).open('a') as handle:
        handle.write(json.dumps({'resumed': resume,
            'claim': option('--resume-bootstrap-claim') if resume else None,
            'index': option('--expected-index-sha256'),
            'operation_id': option('--operation-id') if '--operation-id' in args else None}) + '\\n')
if '--events-jsonl' not in args:
    Path(os.environ['CARLINA_PROGRAM_ROOT'], 'current').write_text('1.2.3\\n')
    raise SystemExit(0)
def emit(event):
    if event.state is InstallationState.RUNNING:
        sys.stdout.write(event.json_line()); sys.stdout.flush()
    else:
        if outcome == 'wrong_identity': event = replace(event, operation_id='b' * 32)
        write_terminal_event(Path(option('--event-terminal-file')), event)
context = InstallationLifecycle('install', os.environ['CARLINA_TEST_PROFILE'],
    operation_id=option('--operation-id'),
    sequence_start=int(option('--event-sequence-start')), event_sink=emit)
context.checkpoint('preflight')
if outcome == 'failure':
    context.fail('operation_failed'); raise SystemExit(1)
if outcome == 'cancelled':
    context.cancel(); raise SystemExit(130)
context.advance('activation', cancellation_safe=False)
Path(os.environ['CARLINA_PROGRAM_ROOT'], 'current').write_text('1.2.3\\n')
if outcome == 'crash_after_commit': os._exit(9)
context.succeed()
raise SystemExit(7 if outcome == 'invalid_exit' else 0)
"""

_SYNTHETIC_UNPACKER = b"""#!/bin/sh
set -eu
if [ "$CARLINA_TEST_STOP_PHASE" = unpack ]; then exit 9; fi
mkdir -p env/bin
cat > env/bin/python <<'PYTHON'
#!/bin/sh
set -eu
if [ "$2" = '-S' ]; then
  if [ "$CARLINA_TEST_STOP_PHASE" = profile ]; then exit 9; fi
  exit 0
fi
if [ "$2" = '-c' ]; then exec "$CARLINA_TEST_REAL_PYTHON" "$@"; fi
if [ "$CARLINA_TEST_STOP_PHASE" = adopt_before_commit ]; then exit 9; fi
printf '%s\\n' "$CARLINA_MANAGED_VERSION" > "$CARLINA_PROGRAM_ROOT/current"
if [ "$CARLINA_TEST_STOP_PHASE" = adopt_after_commit ]; then exit 9; fi
exit 0
PYTHON
printf '#!/bin/sh\\nexit 0\\n' > env/bin/pixi
chmod +x env/bin/python env/bin/pixi
"""


def _isolated_config_environment(tmp_path: Path) -> dict[str, str]:
    return {
        name: str(tmp_path / name.lower())
        for name in (
            "HOME",
            "XDG_CONFIG_HOME",
            "XDG_DATA_HOME",
            "XDG_STATE_HOME",
            "XDG_CACHE_HOME",
            "APPDATA",
            "LOCALAPPDATA",
        )
    }


def _installer(
    tmp_path: Path,
    profile: str,
    platform: str,
    *,
    payload_bytes: bytes | None = None,
    publisher: bool = False,
    base_url: str = "https://downloads.example.test/preview/",
) -> tuple[Path, Path, Path]:
    extension = (".ps1" if publisher else ".zip") if platform == "win-64" else ".sh"
    artifact = tmp_path / f"synthetic-{platform}{extension}"
    artifact.write_bytes(
        _SYNTHETIC_UNPACKER if payload_bytes is None else payload_bytes
    )
    metadata = tmp_path / "artifact.json"
    payload: dict[str, object] = {
        "format": "carlina.managed-artifact",
        "schema_version": 1,
        "version": "1.2.3",
        "platform": platform,
        "kind": "pixi-pack-bundle" if platform == "win-64" else "pixi-pack-executable",
        "filename": artifact.name,
        "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
        "size": artifact.stat().st_size,
    }
    if profile == "documents":
        payload["application_profile"] = profile
    if publisher:
        payload["kind"] = "publisher-bootstrap-executable"
    metadata.write_text(json.dumps(payload), encoding="utf-8")
    index, trust = build_release_index(
        [metadata],
        channel="preview",
        base_url=base_url,
        rollback="restore_required",
        addon_api_version="1.0",
        published_at="2026-10-24T12:00:00Z",
        signing_key=Ed25519PrivateKey.generate(),
        schema_version=3 if publisher else 1,
        execution_contract=(
            {
                "format": "carlina.release-execution-contract-input",
                "schema_version": 1,
                "release_version": "1.2.3",
                "platforms": [platform],
                "execution": {
                    "data_impact": "opaque",
                    "migration": {"mode": "python_alembic"},
                    "bootstrap": {"mode": "repair_and_reconciliation"},
                    "rollback": {"mode": "restore_required"},
                    "recovery": {"automatic_coverage": "full"},
                    "probe": {
                        "protocol": "carlina.managed-release-probe",
                        "schema_version": 1,
                        "isolation": "live_data_root",
                    },
                },
            }
            if publisher
            else None
        ),
    )
    index_path, trust_path = tmp_path / "index.json", tmp_path / "trust.json"
    index_path.write_text(json.dumps(index), encoding="utf-8")
    trust_path.write_text(json.dumps(trust), encoding="utf-8")
    outputs = render_installers(
        index_path,
        trust_path,
        index_url=base_url + "index.json",
        output_dir=tmp_path / "installers",
    )
    suffix = ".ps1" if platform == "win-64" else ".sh"
    return next(path for path in outputs if path.suffix == suffix), index_path, artifact


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell execution")
@pytest.mark.parametrize("profile", ["documents", "workbench"])
@pytest.mark.parametrize(
    "stop_phase",
    ["unpack", "profile", "adopt_before_commit", "adopt_after_commit", "success"],
)
def test_shell_handoff_retains_a_possibly_committed_generation(
    tmp_path: Path, profile: str, stop_phase: str
) -> None:
    installer, index, artifact = _installer(tmp_path, profile, "linux-64")
    shim = tmp_path / "shim"
    shim.mkdir()
    curl = shim / "curl"
    curl.write_text(
        """#!/bin/sh
set -eu
destination=''
url=''
while [ "$#" -gt 0 ]; do
  case "$1" in
    -o) destination=$2; shift 2 ;;
    https:*) url=$1; shift ;;
    *) shift ;;
  esac
done
case "$url" in
  */index.json) cp "$CARLINA_TEST_INDEX" "$destination" ;;
  */synthetic-linux-64.sh) cp "$CARLINA_TEST_ARTIFACT" "$destination" ;;
  *) exit 21 ;;
esac
""",
        encoding="utf-8",
    )
    curl.chmod(0o700)
    # This shim isolates space accounting from the test host's available disk.
    df = shim / "df"
    df.write_text(
        "#!/bin/sh\n"
        "printf 'Filesystem blocks used available capacity mount\\n'\n"
        "printf 'synthetic 16777216 0 16777216 0%% /\\n'\n",
        encoding="utf-8",
    )
    df.chmod(0o700)
    program = tmp_path / "program with spaces"
    previous = program / "versions/1.0.0"
    previous.mkdir(parents=True)
    previous.joinpath("preserved").write_bytes(b"previous generation")
    program.joinpath("current").write_text("1.0.0\n", encoding="utf-8")
    data = tmp_path / "owned data"
    data.mkdir()
    data.joinpath("marker").write_bytes(b"private synthetic data remains")
    result = subprocess.run(
        [
            "sh",
            str(installer),
            "--program-root",
            str(program),
            "--skip-init",
            "--no-path-link",
        ],
        env={
            **os.environ,
            **_isolated_config_environment(tmp_path),
            "PATH": str(shim) + os.pathsep + os.environ.get("PATH", ""),
            "CARLINA_HOME": str(data),
            "CARLINA_TEST_STOP_PHASE": stop_phase,
            "CARLINA_TEST_INDEX": str(index),
            "CARLINA_TEST_ARTIFACT": str(artifact),
            "CARLINA_TEST_REAL_PYTHON": sys.executable,
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert (result.returncode == 0) is (stop_phase == "success"), result.stderr
    retained = stop_phase in {"adopt_before_commit", "adopt_after_commit", "success"}
    assert program.joinpath("versions/1.2.3").is_dir() is retained
    assert previous.joinpath("preserved").read_bytes() == b"previous generation"
    expected_current = (
        "1.2.3" if stop_phase in {"adopt_after_commit", "success"} else "1.0.0"
    )
    assert (
        program.joinpath("current").read_text(encoding="utf-8").strip()
        == expected_current
    )
    assert data.joinpath("marker").read_bytes() == b"private synthetic data remains"
    assert not tuple(program.glob(".carlina-install.*"))


@pytest.mark.parametrize("profile", ["documents", "workbench"])
@pytest.mark.parametrize(
    ("handoff", "registered", "created", "retained"),
    [
        (False, False, True, False),
        (True, False, True, True),
        (True, True, True, True),
        (False, False, False, True),
    ],
)
def test_powershell_cleanup_retains_handed_off_and_preexisting_generations(
    tmp_path: Path,
    profile: str,
    handoff: bool,
    registered: bool,
    created: bool,
    retained: bool,
) -> None:
    powershell = shutil.which("powershell.exe") or shutil.which("pwsh")
    if powershell is None:
        pytest.skip("PowerShell is unavailable for portable cleanup execution.")
    installer, _index, _artifact = _installer(tmp_path, profile, "win-64")
    assert _powershell_bootstrap_ownership_helpers() in installer.read_text()
    probe = tmp_path / "cleanup.ps1"
    probe.write_text(
        "$ErrorActionPreference='Stop'\n"
        "$versionsRoot=$env:CARLINA_TEST_ROOT\n"
        "$versionDir=Join-Path $env:CARLINA_TEST_ROOT 'generation'\n"
        "$work=Join-Path $env:CARLINA_TEST_ROOT 'scratch'\n"
        "New-Item -ItemType Directory -Path $versionDir,$work | Out-Null\n"
        "$bootstrapClaimHeader='carlina.bootstrap-claim/1'\n"
        "$bootstrapClaimId='0123456789abcdef0123456789abcdef'\n"
        "$bootstrapIdentity='" + "a" * 64 + "'\n"
        "$bootstrapClaimSize=124\n"
        "$bootstrapClaimPath=Join-Path $versionDir '.carlina-bootstrap-claim'\n"
        '[IO.File]::WriteAllBytes($bootstrapClaimPath, [Text.Encoding]::ASCII.GetBytes("$bootstrapClaimHeader`n$bootstrapClaimId`n$bootstrapIdentity`n"))\n'
        "$script:bootstrapLease=[IO.FileStream]::new((Join-Path $versionsRoot 'lock'), [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)\n"
        "$script:bootstrapNativeActive=$false\n"
        f"$createdVersionDir=${str(created).lower()}\n"
        f"$registered=${str(registered).lower()}\n"
        f"$adoptionStarted=${str(handoff).lower()}\n"
        + _powershell_bootstrap_ownership_helpers()
        + "\nif (-not (Remove-OwnedBootstrapScratch)) { throw 'cleanup failed' }\nClose-BootstrapLease\n"
        + f"if ((Test-Path -LiteralPath $versionDir) -ne ${str(retained).lower()}) {{ throw 'generation retention mismatch' }}\n"
        + "if (Test-Path -LiteralPath $work) { throw 'scratch retained' }\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-File", str(probe)],
        env={**os.environ, "CARLINA_TEST_ROOT": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def _event_fixture(
    tmp_path: Path,
    profile: str,
    platform: str,
    outcome: str,
    *,
    publisher_payload: bytes | None = None,
    unpacker_payload: bytes | None = None,
    base_url: str = "https://downloads.example.test/preview/",
    native_downloads: bool = False,
) -> tuple[Path, Path, dict[str, str]]:
    installer, index, artifact = _installer(
        tmp_path,
        profile,
        platform,
        payload_bytes=(
            _EVENT_UNPACKER if unpacker_payload is None else unpacker_payload
        )
        if publisher_payload is None
        else publisher_payload,
        publisher=publisher_payload is not None,
        base_url=base_url,
    )
    manager = tmp_path / "manager.py"
    manager.write_text(_EVENT_MANAGER, encoding="utf-8")
    program = tmp_path / "program with spaces"
    previous = program / "versions/1.0.0"
    previous.mkdir(parents=True)
    previous.joinpath("preserved").write_bytes(b"previous generation")
    program.joinpath("current").write_text("1.0.0\n", encoding="utf-8")
    data = tmp_path / "owned data"
    data.mkdir()
    data.joinpath("marker").write_bytes(b"synthetic private data")
    shim = tmp_path / "shim"
    shim.mkdir()
    curl = shim / "curl"
    curl.write_text(
        """#!/bin/sh
set -eu
destination=''
url=''
while [ "$#" -gt 0 ]; do
  case "$1" in
    -o) destination=$2; shift 2 ;;
    https:*) url=$1; shift ;;
    *) shift ;;
  esac
done
case "$url" in
  */index.json) cp "$CARLINA_TEST_INDEX" "$destination" ;;
  *)
    if [ "$CARLINA_TEST_EVENT_OUTCOME" = offline ]; then exit 22; fi
    cp "$CARLINA_TEST_ARTIFACT" "$destination" ;;
esac
""",
        encoding="utf-8",
    )
    curl.chmod(0o700)
    df = shim / "df"
    df.write_text(
        "#!/bin/sh\nprintf 'Filesystem blocks used available capacity mount\\n'\n"
        "printf 'synthetic 16777216 0 16777216 0%% /\\n'\n",
        encoding="utf-8",
    )
    df.chmod(0o700)
    if outcome == "index_changed":
        index.write_bytes(index.read_bytes() + b"\n")
    if platform == "win-64":
        script = installer.read_text(encoding="utf-8")
        # Portable control-flow coverage: network and the native Windows unpacker
        # are synthetic; signed metadata/hash checks and event/handoff code run.
        overrides = (
            r"""
function Save-HttpsFile([string]$Url, [string]$Path) {
    if ($Url.EndsWith('/index.json')) {
        Copy-Item -LiteralPath $env:CARLINA_TEST_INDEX -Destination $Path
    } else {
        if ($env:CARLINA_TEST_EVENT_OUTCOME -eq 'offline') { throw 'synthetic offline download' }
        Copy-Item -LiteralPath $env:CARLINA_TEST_ARTIFACT -Destination $Path
    }
}
"""
            if not native_downloads
            else ""
        )
        overrides += r"""
function Expand-VerifiedWindowsBundle([string]$Path, [string]$Destination) {
    New-Item -ItemType Directory -Path $Destination -Force | Out-Null
    $native = Join-Path $Destination 'pixi-unpack.exe'
    Copy-Item -LiteralPath $env:CARLINA_TEST_ARTIFACT -Destination $native
    & /bin/chmod '+x' $native
    [IO.File]::WriteAllBytes((Join-Path $Destination 'archive.tar'), [byte[]]@())
}
"""
        marker = "try {\nInvoke-InstallationCheckpoint 'preflight' $true"
        assert script.count(marker) == 1
        script = script.replace(marker, overrides + "\n" + marker)
        script = script.replace(
            "Write-InstallationDiagnostic 'Installation did not complete; inspect its event stream and durable state.'",
            "Write-InstallationDiagnostic ([string]$_)",
        )
        assert script.count("$freeBytes = $programItem.PSDrive.Free") == 1
        script = script.replace(
            "$freeBytes = $programItem.PSDrive.Free", "$freeBytes = [long](16GB)"
        )
        if publisher_payload is not None:
            powershell = shutil.which("pwsh")
            if powershell is None:
                pytest.skip("PowerShell is unavailable")
            script = script.replace(
                "$unpackerPath = Join-Path $PSHOME 'powershell.exe'",
                "$unpackerPath = '" + powershell.replace("'", "''") + "'",
            )
        installer.write_text(script, encoding="utf-8")
    environment = {
        **os.environ,
        **_isolated_config_environment(tmp_path),
        "PATH": str(shim) + os.pathsep + os.environ.get("PATH", ""),
        "CARLINA_HOME": str(data),
        "CARLINA_TEST_EVENT_OUTCOME": outcome,
        "CARLINA_TEST_PROFILE": profile,
        "CARLINA_TEST_INDEX": str(index),
        "CARLINA_TEST_ARTIFACT": str(artifact),
        "CARLINA_TEST_MANAGER": str(manager),
        "CARLINA_TEST_REAL_PYTHON": sys.executable,
    }
    return installer, program, environment


def _assert_event_outcome(
    result: subprocess.CompletedProcess[str],
    program: Path,
    profile: str,
    outcome: str,
    *,
    before_adoption: bool = False,
    operation_id: str = "a" * 32,
) -> None:
    events = [json.loads(line) for line in result.stdout.splitlines()]
    assert events, result.stderr
    schema = json.loads(
        Path(__file__)
        .resolve()
        .parents[1]
        .joinpath("carlina/core/installation-event-v1.schema.json")
        .read_text(encoding="utf-8")
    )
    validator = Draft202012Validator(schema)
    for event in events:
        validator.validate(event)
    assert {event["operation_id"] for event in events} == {operation_id}
    assert {event["application_profile"] for event in events} == {profile}
    assert [event["sequence"] for event in events] == list(range(1, len(events) + 1))
    terminal = [event for event in events if event["state"] != "running"]
    if outcome in {"crash_after_commit", "wrong_identity", "invalid_exit"}:
        assert result.returncode != 0
        assert not terminal
    else:
        assert len(terminal) == 1, result.stderr
        expected = {"success": "succeeded", "cancelled": "cancelled"}.get(
            outcome, "failed"
        )
        assert terminal[0]["state"] == expected, result.stderr
        assert (result.returncode == 0) is (outcome == "success")
        if outcome == "offline":
            assert terminal[0]["error_code"] == "download_failed"
            assert terminal[0]["retryable"]
        if outcome == "index_changed":
            assert terminal[0]["error_code"] == "verification_failed"
        if outcome == "old_protocol":
            assert terminal[0]["error_code"] == "incompatible_release"
        if outcome == "unpack_failure":
            assert terminal[0]["phase"] == "unpacking"
    retained = not before_adoption and outcome in {
        "success",
        "failure",
        "cancelled",
        "crash_after_commit",
        "wrong_identity",
        "invalid_exit",
    }
    assert program.joinpath("versions/1.2.3").is_dir() is retained
    committed = outcome in {
        "success",
        "crash_after_commit",
        "wrong_identity",
        "invalid_exit",
    }
    assert program.joinpath("current").read_text().strip() == (
        "1.2.3" if committed else "1.0.0"
    )
    assert (
        program.joinpath("versions/1.0.0/preserved").read_bytes()
        == b"previous generation"
    )
    assert (
        program.parent.joinpath("owned data/marker").read_bytes()
        == b"synthetic private data"
    )
    assert not tuple(program.glob(".carlina-install*"))
    assert str(program) not in result.stdout
    assert "https://" not in result.stdout


@pytest.mark.skipif(os.name == "nt", reason="POSIX bootstrap execution")
@pytest.mark.parametrize("profile", ["documents", "workbench"])
@pytest.mark.parametrize(
    "outcome",
    [
        "success",
        "failure",
        "cancelled",
        "offline",
        "index_changed",
        "unpack_failure",
        "old_protocol",
        "crash_after_commit",
        "wrong_identity",
        "invalid_exit",
    ],
)
def test_shell_bootstrap_event_handoff(
    tmp_path: Path, profile: str, outcome: str
) -> None:
    installer, program, environment = _event_fixture(
        tmp_path, profile, "linux-64", outcome
    )
    result = subprocess.run(
        [
            "sh",
            str(installer),
            "--program-root",
            str(program),
            "--skip-init",
            "--no-path-link",
            "--events-jsonl",
            "--operation-id",
            "a" * 32,
        ],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    _assert_event_outcome(result, program, profile, outcome)


@pytest.mark.skipif(os.name == "nt", reason="Portable PowerShell control-flow fixture")
@pytest.mark.parametrize("profile", ["documents", "workbench"])
@pytest.mark.parametrize(
    "outcome",
    [
        "success",
        "failure",
        "cancelled",
        "offline",
        "index_changed",
        "unpack_failure",
        "old_protocol",
        "crash_after_commit",
        "wrong_identity",
        "invalid_exit",
    ],
)
def test_powershell_bootstrap_event_handoff(
    tmp_path: Path, profile: str, outcome: str
) -> None:
    powershell = shutil.which("pwsh")
    if powershell is None:
        pytest.skip("PowerShell is unavailable for portable control-flow execution.")
    installer, program, environment = _event_fixture(
        tmp_path, profile, "win-64", outcome
    )
    result = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(installer),
            "-ProgramRoot",
            str(program),
            "-SkipInit",
            "-NoPathUpdate",
            "-EventsJsonl",
            "-OperationId",
            "a" * 32,
        ],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    _assert_event_outcome(result, program, profile, outcome)


@pytest.mark.skipif(os.name == "nt", reason="Portable cleanup barrier uses POSIX pipes")
@pytest.mark.parametrize("platform", ["linux-64", "win-64"])
def test_bootstrap_terminal_waits_for_scratch_cleanup(
    tmp_path: Path, platform: str
) -> None:
    installer, program, environment = _event_fixture(
        tmp_path, "documents", platform, "success"
    )
    entered, release = tmp_path / "cleanup-entered", tmp_path / "cleanup-release"
    environment.update(
        {
            "CARLINA_TEST_CLEANUP_ENTERED": str(entered),
            "CARLINA_TEST_CLEANUP_RELEASE": str(release),
        }
    )
    if platform == "linux-64":
        real_rm = shutil.which("rm")
        assert real_rm is not None
        environment["CARLINA_TEST_REAL_RM"] = real_rm
        rm = tmp_path / "shim/rm"
        rm.write_text(
            """#!/bin/sh
set -eu
case "$3" in
  */.carlina-install.*)
    : > "$CARLINA_TEST_CLEANUP_ENTERED"
    count=0
    while [ ! -e "$CARLINA_TEST_CLEANUP_RELEASE" ]; do
      count=$((count + 1)); if [ "$count" -gt 1000 ]; then exit 19; fi
      sleep 0.01
    done ;;
esac
exec "$CARLINA_TEST_REAL_RM" "$@"
""",
            encoding="utf-8",
        )
        rm.chmod(0o700)
        command = [
            "sh",
            str(installer),
            "--program-root",
            str(program),
            "--skip-init",
            "--no-path-link",
            "--events-jsonl",
            "--operation-id",
            "a" * 32,
        ]
    else:
        powershell = shutil.which("pwsh")
        if powershell is None:
            pytest.skip("PowerShell is unavailable for the portable cleanup barrier.")
        script = installer.read_text(encoding="utf-8")
        hook = r"""
function Remove-Item {
    [CmdletBinding()]
    param([string]$LiteralPath, [switch]$Recurse, [switch]$Force)
    if ((Split-Path -Leaf $LiteralPath).StartsWith('.carlina-install-')) {
        [IO.File]::WriteAllText($env:CARLINA_TEST_CLEANUP_ENTERED, '')
        $count = 0
        while (-not (Test-Path -LiteralPath $env:CARLINA_TEST_CLEANUP_RELEASE)) {
            $count += 1
            if ($count -gt 1000) { throw 'cleanup barrier timed out' }
            Start-Sleep -Milliseconds 10
        }
    }
    Microsoft.PowerShell.Management\Remove-Item @PSBoundParameters
}
"""
        marker = "try {\nInvoke-InstallationCheckpoint 'preflight' $true"
        installer.write_text(
            script.replace(marker, hook + "\n" + marker), encoding="utf-8"
        )
        command = [
            powershell,
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(installer),
            "-ProgramRoot",
            str(program),
            "-SkipInit",
            "-NoPathUpdate",
            "-EventsJsonl",
            "-OperationId",
            "a" * 32,
        ]
    process = subprocess.Popen(
        command, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    assert process.stdout is not None
    collected = bytearray()
    os.set_blocking(process.stdout.fileno(), False)
    deadline = time.monotonic() + 20
    try:
        while not entered.exists() and time.monotonic() < deadline:
            if select.select([process.stdout], [], [], 0.05)[0]:
                collected.extend(os.read(process.stdout.fileno(), 65536))
            assert process.poll() is None, "bootstrap exited before cleanup barrier"
        assert entered.exists(), "bootstrap never reached cleanup"
        while select.select([process.stdout], [], [], 0.1)[0]:
            chunk = os.read(process.stdout.fileno(), 65536)
            if not chunk:
                break
            collected.extend(chunk)
        assert all(
            json.loads(line)["state"] == "running" for line in collected.splitlines()
        )
        assert tuple(program.glob(".carlina-install*")), (
            "scratch was removed before barrier"
        )
    finally:
        release.touch()
        remainder, errors = process.communicate(timeout=30)
    assert process.returncode is not None
    result = subprocess.CompletedProcess(
        command, process.returncode, (collected + remainder).decode(), errors.decode()
    )
    _assert_event_outcome(result, program, "documents", "success")


def _native_event_command(installer: Path, program: Path, platform: str) -> list[str]:
    if platform == "linux-64":
        return [
            "sh",
            str(installer),
            "--program-root",
            str(program),
            "--skip-init",
            "--no-path-link",
            "--events-jsonl",
        ]
    powershell = shutil.which("pwsh")
    if powershell is None:
        pytest.skip("PowerShell is unavailable for portable control-flow execution.")
    return [
        powershell,
        "-NoProfile",
        "-NonInteractive",
        "-File",
        str(installer),
        "-ProgramRoot",
        str(program),
        "-SkipInit",
        "-NoPathUpdate",
        "-EventsJsonl",
    ]


@pytest.mark.skipif(os.name == "nt", reason="Portable native-control fixtures")
@pytest.mark.parametrize("platform", ["linux-64", "win-64"])
@pytest.mark.parametrize("kind", ["request", "directory", "symlink", "missing_parent"])
def test_native_cancellation_control_is_metadata_only_and_owned_by_caller(
    tmp_path: Path, platform: str, kind: str
) -> None:
    installer, program, environment = _event_fixture(
        tmp_path, "documents", platform, "success"
    )
    request = tmp_path / "cancel request"
    target = tmp_path / "caller owned bytes"
    target.write_bytes(b"caller private bytes are never read or changed")
    if kind == "request":
        request.write_bytes(b"ignored content")
    elif kind == "directory":
        request.mkdir()
    elif kind == "symlink":
        request.symlink_to(target)
    else:
        request = tmp_path / "absent-parent/request"
    flag = "--cancel-file" if platform == "linux-64" else "-CancelFile"
    result = subprocess.run(
        [*_native_event_command(installer, program, platform), flag, str(request)],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    events = [json.loads(line) for line in result.stdout.splitlines()]
    assert len(events) == 2, result.stderr
    assert events[0]["state"] == "running"
    assert events[-1]["state"] == ("cancelled" if kind == "request" else "failed")
    assert events[-1]["error_code"] == (
        "cancelled" if kind == "request" else "operation_failed"
    )
    assert result.returncode == (130 if kind == "request" else 1)
    assert program.joinpath("current").read_text().strip() == "1.0.0"
    assert not program.joinpath("versions/1.2.3").exists()
    assert target.read_bytes() == b"caller private bytes are never read or changed"
    assert not tuple(program.glob(".carlina-install*"))
    assert "caller private" not in result.stdout + result.stderr
    if kind == "request":
        assert request.read_bytes() == b"ignored content"
    elif kind == "symlink":
        assert request.is_symlink()


@pytest.mark.skipif(os.name == "nt", reason="Portable native-control fixtures")
@pytest.mark.parametrize("platform", ["linux-64", "win-64"])
def test_native_event_mode_generates_one_fresh_id(
    tmp_path: Path, platform: str
) -> None:
    installer, program, environment = _event_fixture(
        tmp_path, "documents", platform, "success"
    )
    result = subprocess.run(
        _native_event_command(installer, program, platform),
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    events = [json.loads(line) for line in result.stdout.splitlines()]
    identifiers = {event["operation_id"] for event in events}
    assert len(identifiers) == 1
    operation_id = identifiers.pop()
    assert len(operation_id) == 32
    assert all(character in "0123456789abcdef" for character in operation_id)
    assert events[-1]["state"] == "succeeded"


def _publisher_fixture_payload(platform: str, outcome: str) -> bytes:
    records = [
        "carlina.publisher-bootstrap/1 ready",
        "carlina.publisher-bootstrap/1 phase download true 0 17",
        "carlina.publisher-bootstrap/1 phase download false 0 17",
        "carlina.publisher-bootstrap/1 phase verification true null null",
        "carlina.publisher-bootstrap/1 phase download true 17 17",
        "carlina.publisher-bootstrap/1 phase environment false null null",
    ]
    result = "succeeded null false"
    exit_code = 0
    if outcome == "download_failed":
        records = records[:3]
        result, exit_code = "failed download_failed true", 22
    elif outcome == "verification_failed":
        records = records[:4]
        result, exit_code = "failed verification_failed false", 1
    elif outcome == "cancelled":
        result, exit_code = "cancelled cancelled true", 130
    elif outcome == "missing_ready":
        records = records[1:]
    elif outcome == "invalid_phase":
        records.append(
            "carlina.publisher-bootstrap/1 phase private-path true null null"
        )
    elif outcome == "invalid_bytes":
        records.append("carlina.publisher-bootstrap/1 phase download true 18 17")
    elif outcome == "leading_zero":
        records.append("carlina.publisher-bootstrap/1 phase download true 01 17")
    elif outcome == "mismatched_exit":
        exit_code = 9
    elif outcome == "mismatched_cancel":
        result = "cancelled cancelled true"
    records.append("carlina.publisher-bootstrap/1 result " + result)
    if outcome == "duplicate_result":
        records.append(records[-1])
    elif outcome == "trailing_prose":
        records.append("unexpected publisher prose")
    elif outcome == "missing_result":
        records.pop()
    if outcome in {"cursor_failure", "cursor_collision"}:
        assert platform == "linux-64"
    unpacker_b64 = base64.b64encode(_EVENT_UNPACKER).decode("ascii")
    if platform == "linux-64":
        before = "\n".join("printf '%s\\n' '" + record + "'" for record in records[:-1])
        if outcome == "cursor_failure":
            before += '\nprintf corrupt > "$TMPDIR/../publisher-progress.cursor"\n'
        elif outcome == "cursor_collision":
            before += '\nprintf "14 environment operation_failed false 0 0\\n" > "$TMPDIR/../publisher-progress.cursor"\n'
        after = "printf '%s\\n' '" + records[-1] + "'"
        return (
            "#!/bin/sh\nset -eu\n"
            'if [ "$1" != --progress-records-v1 ]; then exit 2; fi\n'
            'if [ "$#" -gt 1 ]; then\n'
            '  if [ "$2" != --cancel-file ] || [ "$3" != "$CARLINA_TEST_CANCEL_PATH" ]; then exit 2; fi\n'
            '  printf forwarded > "$CARLINA_TEST_CANCEL_FORWARDED"\n'
            "fi\n"
            + before
            + '\nif [ -n "${CARLINA_TEST_PUBLISHER_WAIT:-}" ]; then\n'
            + '  printf "%s" "$$" > "$CARLINA_TEST_PUBLISHER_WAIT.pid"\n'
            + '  printf ready > "$CARLINA_TEST_PUBLISHER_WAIT.ready"\n'
            + '  while [ ! -f "$CARLINA_TEST_PUBLISHER_WAIT.release" ]; do sleep 0.01; done\n'
            + "fi\n"
            + "\nprintf 'synthetic publisher diagnostic\\n' >&2\n"
            + f"printf '%s' '{unpacker_b64}' | base64 --decode | bash >&2\n"
            + after
            + f"\nexit {exit_code}\n"
        ).encode()
    before = "\n".join(
        "[Console]::Out.WriteLine('" + record + "')" for record in records[:-1]
    )
    after = "[Console]::Out.WriteLine('" + records[-1] + "')"
    return (
        "param([switch]$ProgressRecordsV1, [string]$CancelFile)\n"
        "$ErrorActionPreference = 'Stop'\n"
        "if (-not $ProgressRecordsV1) { exit 2 }\n"
        "if ($CancelFile) {\n"
        "  if ($CancelFile -cne $env:CARLINA_TEST_CANCEL_PATH) { exit 2 }\n"
        "  [IO.File]::WriteAllText($env:CARLINA_TEST_CANCEL_FORWARDED, 'forwarded')\n"
        "}\n"
        + before
        + "\nif ($env:CARLINA_TEST_PUBLISHER_WAIT) {\n"
        + "  [IO.File]::WriteAllText($env:CARLINA_TEST_PUBLISHER_WAIT + '.pid', [string]$PID)\n"
        + "  [IO.File]::WriteAllText($env:CARLINA_TEST_PUBLISHER_WAIT + '.ready', 'ready')\n"
        + "  while (-not [IO.File]::Exists($env:CARLINA_TEST_PUBLISHER_WAIT + '.release')) { Start-Sleep -Milliseconds 10 }\n"
        + "}\n"
        + "\n[Console]::Error.WriteLine('synthetic publisher diagnostic')\n"
        + "$fixture = Join-Path $env:TEMP 'synthetic-publisher-unpack.sh'\n"
        + f"[IO.File]::WriteAllBytes($fixture, [Convert]::FromBase64String('{unpacker_b64}'))\n"
        + "& bash $fixture 2>&1 | ForEach-Object { [Console]::Error.WriteLine([string]$_) }\n"
        + after
        + f"\nexit {exit_code}\n"
    ).encode()


@pytest.mark.skipif(os.name == "nt", reason="Portable native adapter fixtures")
@pytest.mark.parametrize("platform", ["linux-64", "win-64"])
@pytest.mark.parametrize(
    "outcome",
    [
        "success",
        "download_failed",
        "verification_failed",
        "cancelled",
        "missing_ready",
        "invalid_phase",
        "invalid_bytes",
        "leading_zero",
        "mismatched_exit",
        "mismatched_cancel",
        "duplicate_result",
        "trailing_prose",
        "missing_result",
    ],
)
def test_native_publisher_progress_keeps_one_outer_event_owner(
    tmp_path: Path, platform: str, outcome: str
) -> None:
    installer, program, environment = _event_fixture(
        tmp_path,
        "documents",
        platform,
        "success",
        publisher_payload=_publisher_fixture_payload(platform, outcome),
    )
    cancel = tmp_path / "caller cancellation request"
    forwarded = tmp_path / "forwarded"
    environment.update(
        CARLINA_TEST_CANCEL_PATH=str(cancel),
        CARLINA_TEST_CANCEL_FORWARDED=str(forwarded),
    )
    command = _native_event_command(installer, program, platform)
    command += ["-OperationId" if platform == "win-64" else "--operation-id", "a" * 32]
    command += ["-CancelFile" if platform == "win-64" else "--cancel-file", str(cancel)]
    result = subprocess.run(
        command,
        env=environment,
        text=True,
        capture_output=True,
        timeout=40,
        check=False,
    )
    expected = (
        "success"
        if outcome == "success"
        else "cancelled"
        if outcome == "cancelled"
        else "publisher_failure"
    )
    _assert_event_outcome(
        result, program, "documents", expected, before_adoption=outcome != "success"
    )
    events = [json.loads(line) for line in result.stdout.splitlines()]
    assert forwarded.read_text() == "forwarded", result.stderr
    assert not cancel.exists()
    assert "carlina.publisher-bootstrap" not in result.stdout
    assert "synthetic publisher diagnostic" in result.stderr
    if outcome != "missing_ready":
        assert any(
            event["phase"] == "download"
            and event["bytes_completed"] == 0
            and event["bytes_total"] == 17
            for event in events
        )
    terminal = events[-1]
    if outcome in {"download_failed", "verification_failed"}:
        assert terminal["error_code"] == outcome
        assert terminal["retryable"] is (outcome == "download_failed")
    elif outcome == "cancelled":
        assert result.returncode == 130
        assert terminal["error_code"] == "cancelled"
    elif outcome != "success":
        assert terminal["error_code"] == "incompatible_release"


@pytest.mark.skipif(os.name == "nt", reason="Portable native adapter fixtures")
@pytest.mark.parametrize("platform", ["linux-64", "win-64"])
def test_publisher_progress_is_visible_while_child_is_still_running(
    tmp_path: Path, platform: str
) -> None:
    installer, program, environment = _event_fixture(
        tmp_path,
        "documents",
        platform,
        "success",
        publisher_payload=_publisher_fixture_payload(platform, "success"),
    )
    barrier = tmp_path / "publisher barrier"
    environment["CARLINA_TEST_PUBLISHER_WAIT"] = str(barrier)
    command = _native_event_command(installer, program, platform)
    command += ["-OperationId" if platform == "win-64" else "--operation-id", "a" * 32]
    output = tmp_path / "events.jsonl"
    diagnostics = tmp_path / "diagnostics.log"
    with output.open("w") as stdout, diagnostics.open("w") as stderr:
        process = subprocess.Popen(
            command, env=environment, stdout=stdout, stderr=stderr
        )
        try:
            deadline = time.monotonic() + 15
            while not barrier.with_suffix(".ready").exists():
                assert process.poll() is None, diagnostics.read_text()
                assert time.monotonic() < deadline, diagnostics.read_text()
                time.sleep(0.01)
            while True:
                events = [
                    json.loads(line)
                    for line in output.read_text().splitlines(keepends=True)
                    if line.endswith("\n")
                ]
                if any(event["bytes_total"] == 17 for event in events):
                    break
                assert time.monotonic() < deadline, diagnostics.read_text()
                time.sleep(0.01)
            assert process.poll() is None
            assert all(event["state"] == "running" for event in events)
            assert program.joinpath("current").read_text().strip() == "1.0.0"
        finally:
            barrier.with_suffix(".release").write_text("continue")
            process.wait(timeout=30)
    _assert_event_outcome(
        subprocess.CompletedProcess(
            command, process.returncode, output.read_text(), diagnostics.read_text()
        ),
        program,
        "documents",
        "success",
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX publisher cursor transport")
@pytest.mark.parametrize("outcome", ["cursor_failure", "cursor_collision"])
def test_publisher_cursor_failure_does_not_repeat_the_parent_sequence(
    tmp_path: Path, outcome: str
) -> None:
    installer, program, environment = _event_fixture(
        tmp_path,
        "documents",
        "linux-64",
        "success",
        publisher_payload=_publisher_fixture_payload("linux-64", outcome),
    )
    result = subprocess.run(
        _native_event_command(installer, program, "linux-64"),
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode != 0
    events = [json.loads(line) for line in result.stdout.splitlines()]
    assert [event["sequence"] for event in events] == list(range(1, len(events) + 1))
    assert all(event["state"] == "running" for event in events)
    assert not program.joinpath("versions/1.2.3").exists()
    assert not tuple(program.glob(".carlina-install*"))
    assert program.joinpath("current").read_text().strip() == "1.0.0"
    assert (
        program.joinpath("versions/1.0.0/preserved").read_bytes()
        == b"previous generation"
    )
    assert (
        program.parent.joinpath("owned data/marker").read_bytes()
        == b"synthetic private data"
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX publisher interruption boundary")
def test_interruption_after_publisher_success_keeps_a_failure_code(
    tmp_path: Path,
) -> None:
    installer, program, environment = _event_fixture(
        tmp_path,
        "documents",
        "linux-64",
        "success",
        publisher_payload=_publisher_fixture_payload("linux-64", "success"),
    )
    source = installer.read_text()
    invocation = 'if run_publisher_bootstrap; then BOOTSTRAP_NATIVE_ACTIVE=0; else UNPACK_EXIT=$?; BOOTSTRAP_NATIVE_ACTIVE=0; exit "$UNPACK_EXIT"; fi'
    assert source.count(invocation) == 1
    installer.write_text(
        source.replace(
            invocation,
            'if run_publisher_bootstrap; then BOOTSTRAP_NATIVE_ACTIVE=0; kill -TERM $$; else UNPACK_EXIT=$?; BOOTSTRAP_NATIVE_ACTIVE=0; exit "$UNPACK_EXIT"; fi',
        )
    )
    result = subprocess.run(
        [
            *_native_event_command(installer, program, "linux-64"),
            "--operation-id",
            "a" * 32,
        ],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    _assert_event_outcome(
        result, program, "documents", "publisher_failure", before_adoption=True
    )
    events = [json.loads(line) for line in result.stdout.splitlines()]
    assert events[-1]["error_code"] == "operation_failed"


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-only interruption")
@pytest.mark.parametrize("platform", ["linux-64", "win-64"])
def test_parent_signal_preserves_a_running_publishers_working_paths(
    tmp_path: Path, platform: str
) -> None:
    installer, program, environment = _event_fixture(
        tmp_path,
        "documents",
        platform,
        "success",
        publisher_payload=_publisher_fixture_payload(platform, "success"),
    )
    barrier = tmp_path / "publisher barrier"
    environment["CARLINA_TEST_PUBLISHER_WAIT"] = str(barrier)
    output = tmp_path / "interrupted-events.jsonl"
    diagnostics = tmp_path / "interrupted-diagnostics.log"
    with output.open("w") as stdout, diagnostics.open("w") as stderr:
        process = subprocess.Popen(
            _native_event_command(installer, program, platform),
            env=environment,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 15
            while not barrier.with_suffix(".ready").exists():
                assert process.poll() is None, diagnostics.read_text()
                assert time.monotonic() < deadline, diagnostics.read_text()
                time.sleep(0.01)
            child_pid = int(barrier.with_suffix(".pid").read_text())
            process.send_signal(signal.SIGTERM)
            # The child stays independently blocked until the finally release.
            time.sleep(0.2)
            os.kill(child_pid, 0)
            if platform == "linux-64":
                with pytest.raises(Timeout):
                    FileLock(program / "state/managed-install.lock").acquire(timeout=0)
            assert program.joinpath("versions/1.2.3").is_dir(), diagnostics.read_text()
            assert tuple(program.glob(".carlina-install*")), diagnostics.read_text()
            events = [json.loads(line) for line in output.read_text().splitlines()]
            assert all(event["state"] == "running" for event in events)
            frame = program / "versions/1.2.3/.carlina-bootstrap-claim"
            before_frame = frame.read_bytes()
            old_scratch = tuple(program.glob(".carlina-install*"))
            contender = _run_native_attempt(
                installer, program, environment, platform, "b" * 32
            )
            assert contender.returncode != 0
            os.kill(child_pid, 0)
            assert frame.read_bytes() == before_frame
            assert tuple(program.glob(".carlina-install*")) == old_scratch
            assert not program.joinpath(
                "versions/1.2.3/.carlina-bootstrap-binding.json"
            ).exists()
        finally:
            barrier.with_suffix(".release").write_text("continue")
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
                raise
    assert process.returncode != 0
    final_events = [json.loads(line) for line in output.read_text().splitlines()]
    assert all(event["state"] == "running" for event in final_events)
    assert [event["sequence"] for event in final_events] == list(
        range(1, len(final_events) + 1)
    )
    assert program.joinpath("current").read_text().strip() == "1.0.0"
    assert (
        program.joinpath("versions/1.0.0/preserved").read_bytes()
        == b"previous generation"
    )
    assert (
        program.parent.joinpath("owned data/marker").read_bytes()
        == b"synthetic private data"
    )


@pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="Linux pidfd parent-death evidence"
)
@pytest.mark.parametrize("platform", ["linux-64", "win-64"])
@pytest.mark.parametrize("profile", ["documents", "workbench"])
def test_dead_carrier_preserves_a_running_unpackers_preparing_generation(
    tmp_path: Path, platform: str, profile: str
) -> None:
    barrier = tmp_path / "unpacker barrier"
    pause = b"""set -eu
printf '%s' "$$" > "$CARLINA_TEST_UNPACK_WAIT.pid"
printf ready > "$CARLINA_TEST_UNPACK_WAIT.ready"
while [ ! -f "$CARLINA_TEST_UNPACK_WAIT.release" ]; do sleep 0.01; done
"""
    header = b"#!/bin/sh\nset -eu\n"
    assert _EVENT_UNPACKER.startswith(header)
    payload = b"#!/bin/sh\n" + pause + _EVENT_UNPACKER.removeprefix(header)
    installer, program, environment = _event_fixture(
        tmp_path, profile, platform, "success", unpacker_payload=payload
    )
    environment["CARLINA_TEST_UNPACK_WAIT"] = str(barrier)
    output = tmp_path / "dead-carrier-events.jsonl"
    diagnostics = tmp_path / "dead-carrier-diagnostics.log"
    # The locked Python build omits pidfd wrappers; libc exposes the same kernel
    # process handles without relying on a retained PID for cleanup.
    libc = ctypes.CDLL(None, use_errno=True)
    if not hasattr(libc, "pidfd_open") or not hasattr(libc, "pidfd_send_signal"):
        pytest.skip("libc pidfd process handles are unavailable")
    open_process = libc.pidfd_open
    open_process.argtypes = (ctypes.c_int, ctypes.c_uint)
    open_process.restype = ctypes.c_int
    signal_process = libc.pidfd_send_signal
    signal_process.argtypes = (
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_uint,
    )
    signal_process.restype = ctypes.c_int
    child_descriptor = None
    with output.open("w") as stdout, diagnostics.open("w") as stderr:
        process = subprocess.Popen(
            _native_event_command(installer, program, platform),
            env=environment,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 15
            while not barrier.with_suffix(".ready").exists():
                assert process.poll() is None, diagnostics.read_text()
                assert time.monotonic() < deadline, diagnostics.read_text()
                time.sleep(0.01)
            child_descriptor = int(
                open_process(int(barrier.with_suffix(".pid").read_text()), 0)
            )
            if child_descriptor < 0:
                child_descriptor = None
                raise OSError(ctypes.get_errno(), "Could not open owned child pidfd")
            assert not select.select([child_descriptor], [], [], 0)[0]
            process.kill()
            assert process.wait(timeout=5) == -signal.SIGKILL
            # The actual parent is gone before liveness or contender assertions.
            assert not barrier.with_suffix(".release").exists()
            assert not select.select([child_descriptor], [], [], 0)[0]
            if platform == "linux-64":
                with pytest.raises(Timeout):
                    FileLock(program / "state/managed-install.lock").acquire(timeout=0)
            frame = program / "versions/1.2.3/.carlina-bootstrap-claim"
            before_frame = frame.read_bytes()
            scratch = tuple(program.glob(".carlina-install*"))
            assert scratch
            contender = _run_native_attempt(
                installer, program, environment, platform, "b" * 32
            )
            assert contender.returncode != 0, contender.stderr
            assert not select.select([child_descriptor], [], [], 0)[0]
            assert frame.read_bytes() == before_frame
            assert tuple(program.glob(".carlina-install*")) == scratch
            assert not program.joinpath(
                "versions/1.2.3/.carlina-bootstrap-binding.json"
            ).exists()
        finally:
            barrier.with_suffix(".release").write_text("continue")
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            if child_descriptor is not None:
                try:
                    exited = bool(select.select([child_descriptor], [], [], 5)[0])
                    if not exited:
                        assert (
                            signal_process(child_descriptor, signal.SIGKILL, None, 0)
                            == 0
                        )
                        assert select.select([child_descriptor], [], [], 5)[0]
                    assert exited, "Owned unpacker did not exit after its release"
                finally:
                    os.close(child_descriptor)
    events = [json.loads(line) for line in output.read_text().splitlines()]
    assert events and all(event["state"] == "running" for event in events)
    assert [event["sequence"] for event in events] == list(range(1, len(events) + 1))
    assert frame.read_bytes() == before_frame
    assert tuple(program.glob(".carlina-install*")) == scratch
    assert not program.joinpath(
        "versions/1.2.3/.carlina-bootstrap-binding.json"
    ).exists()
    assert program.joinpath("current").read_text().strip() == "1.0.0"
    assert (
        program.joinpath("versions/1.0.0/preserved").read_bytes()
        == b"previous generation"
    )
    assert (
        program.parent.joinpath("owned data/marker").read_bytes()
        == b"synthetic private data"
    )


@pytest.mark.skipif(os.name == "nt", reason="Portable native lock interoperability")
@pytest.mark.parametrize("surface", ["flock", "powershell"])
def test_native_and_python_program_leases_exclude_each_other(
    tmp_path: Path, surface: str
) -> None:
    lock_path = tmp_path / "managed-install.lock"
    if surface == "flock":
        if shutil.which("flock") is None:
            pytest.skip("flock is unavailable")
        command = [
            "sh",
            "-c",
            'exec 9<>"$1"; flock -n 9 || exit 7; printf "ready\\n"; read -r release',
            "probe",
            str(lock_path),
        ]
    else:
        powershell = shutil.which("pwsh")
        if powershell is None:
            pytest.skip("Portable PowerShell is unavailable")
        command = [
            powershell,
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "$ErrorActionPreference='Stop'; "
            "$lease=[IO.FileStream]::new($env:CARLINA_TEST_LOCK, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None); "
            "try { Write-Output 'ready'; [Console]::ReadLine() | Out-Null } finally { $lease.Dispose() }",
        ]
    environment = {**os.environ, "CARLINA_TEST_LOCK": str(lock_path)}
    native = subprocess.Popen(
        command,
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert native.stdout is not None
        assert select.select([native.stdout], [], [], 15)[0], (
            "Native lock probe stalled"
        )
        assert native.stdout.readline().strip() == "ready"
        with pytest.raises(Timeout):
            FileLock(lock_path).acquire(timeout=0)
    finally:
        _stdout, stderr = native.communicate("release\n", timeout=15)
    assert native.returncode == 0, stderr
    # The reverse order uses a distinct OS process, not a reentrant FileLock.
    with FileLock(lock_path):
        contender = subprocess.run(
            command,
            env=environment,
            input="release\n",
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        assert contender.returncode != 0
        assert "ready" not in contender.stdout
    released = subprocess.run(
        command,
        env=environment,
        input="release\n",
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert released.returncode == 0, released.stderr


@pytest.mark.skipif(os.name == "nt", reason="Portable installer contention")
@pytest.mark.parametrize("platform", ["linux-64", "win-64"])
def test_competing_native_installer_cannot_claim_or_clean_an_owned_generation(
    tmp_path: Path, platform: str
) -> None:
    installer, program, environment = _event_fixture(
        tmp_path,
        "documents",
        platform,
        "success",
        publisher_payload=_publisher_fixture_payload(platform, "success"),
    )
    barrier = tmp_path / "publisher barrier"
    environment["CARLINA_TEST_PUBLISHER_WAIT"] = str(barrier)
    diagnostics = tmp_path / "owner.log"
    events_path = tmp_path / "owner.jsonl"
    command = _native_event_command(installer, program, platform)
    with events_path.open("w") as stdout, diagnostics.open("w") as stderr:
        owner = subprocess.Popen(
            command,
            env=environment,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 15
            while not barrier.with_suffix(".ready").exists():
                assert owner.poll() is None, diagnostics.read_text()
                assert time.monotonic() < deadline, diagnostics.read_text()
                time.sleep(0.01)
            frame = program / "versions/1.2.3/.carlina-bootstrap-claim"
            before = frame.read_bytes()
            scratch = tuple(program.glob(".carlina-install*"))
            contender = subprocess.run(
                command,
                env=environment,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            assert contender.returncode != 0
            assert frame.read_bytes() == before
            assert tuple(program.glob(".carlina-install*")) == scratch
            assert program.joinpath("current").read_text().strip() == "1.0.0"
        finally:
            barrier.with_suffix(".release").write_text("continue")
            try:
                owner.wait(timeout=30)
            except subprocess.TimeoutExpired:
                os.killpg(owner.pid, signal.SIGKILL)
                owner.wait(timeout=5)
                raise
    assert owner.returncode == 0, diagnostics.read_text()
    assert json.loads(events_path.read_text().splitlines()[-1])["state"] == "succeeded"


@pytest.mark.skipif(os.name == "nt", reason="Portable installer ownership refusal")
@pytest.mark.parametrize("platform", ["linux-64", "win-64"])
def test_manager_program_lease_blocks_native_claim_before_unpacking(
    tmp_path: Path, platform: str
) -> None:
    installer, program, environment = _event_fixture(
        tmp_path, "documents", platform, "success"
    )
    state = program / "state"
    state.mkdir()
    with FileLock(state / "managed-install.lock"):
        result = subprocess.run(
            _native_event_command(installer, program, platform),
            env=environment,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    assert result.returncode != 0
    assert not program.joinpath("versions/1.2.3").exists()
    assert not tuple(program.glob(".carlina-install*"))
    assert program.joinpath("current").read_text().strip() == "1.0.0"


@pytest.mark.skipif(os.name == "nt", reason="Portable PowerShell Legacy argument mode")
@pytest.mark.parametrize("profile", ["documents", "workbench"])
def test_native_ready_binding_survives_powershell_legacy_argument_passing(
    tmp_path: Path, profile: str
) -> None:
    installer, program, environment = _event_fixture(
        tmp_path, profile, "win-64", "success"
    )
    source = installer.read_text()
    marker = "$createdVersionDir = $false\n$registered = $false"
    assert source.count(marker) == 1
    installer.write_text(
        source.replace(marker, "$PSNativeCommandArgumentPassing='Legacy'\n" + marker)
    )
    result = subprocess.run(
        [
            *_native_event_command(installer, program, "win-64"),
            "-OperationId",
            "a" * 32,
        ],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    _assert_event_outcome(result, program, profile, "success")
    binding = json.loads(
        program.joinpath("versions/1.2.3/.carlina-bootstrap-binding.json").read_bytes()
    )
    assert binding["program_root"] == str(program.resolve())
    assert binding["data_root"] == str(Path(environment["CARLINA_HOME"]).resolve())
    environment.update(CARLINA_TEST_REQUIRE_RESUME="1", CARLINA_TEST_FORBID_UNPACK="1")
    second = _run_native_attempt(installer, program, environment, "win-64", "b" * 32)
    _assert_event_outcome(second, program, profile, "success", operation_id="b" * 32)


@pytest.mark.skipif(os.name == "nt", reason="Portable installer ownership refusal")
@pytest.mark.parametrize("platform", ["linux-64", "win-64"])
def test_native_cleanup_retains_a_generation_after_its_claim_changes(
    tmp_path: Path, platform: str
) -> None:
    unpacker = _EVENT_UNPACKER.replace(
        b"mkdir -p env/bin",
        b"printf 'foreign claim' > .carlina-bootstrap-claim\nexit 9\nmkdir -p env/bin",
    )
    installer, program, environment = _event_fixture(
        tmp_path,
        "documents",
        platform,
        "failure",
        unpacker_payload=unpacker,
    )
    result = subprocess.run(
        _native_event_command(installer, program, platform),
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode != 0
    assert (
        program.joinpath("versions/1.2.3/.carlina-bootstrap-claim").read_bytes()
        == b"foreign claim"
    )
    assert tuple(program.glob(".carlina-install*"))
    assert program.joinpath("current").read_text().strip() == "1.0.0"
    assert all(
        json.loads(line)["state"] == "running" for line in result.stdout.splitlines()
    )


def _run_native_attempt(
    installer: Path,
    program: Path,
    environment: dict[str, str],
    platform: str,
    operation_id: str | None = "a" * 32,
) -> subprocess.CompletedProcess[str]:
    command = _native_event_command(installer, program, platform)
    if operation_id is None:
        command.remove("--events-jsonl" if platform == "linux-64" else "-EventsJsonl")
    else:
        command += [
            "--operation-id" if platform == "linux-64" else "-OperationId",
            operation_id,
        ]
    return subprocess.run(
        command,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


@pytest.mark.skipif(os.name == "nt", reason="Portable native resume adapters")
@pytest.mark.parametrize("profile", ["documents", "workbench"])
@pytest.mark.parametrize("platform", ["linux-64", "win-64"])
@pytest.mark.parametrize("outcome", ["failure", "crash_after_commit"])
def test_ready_native_rerun_forwards_the_existing_claim_without_unpacking(
    tmp_path: Path, profile: str, platform: str, outcome: str
) -> None:
    installer, program, environment = _event_fixture(
        tmp_path, profile, platform, outcome
    )
    calls = tmp_path / "manager-calls.jsonl"
    environment["CARLINA_TEST_MANAGER_CALLS"] = str(calls)
    first = _run_native_attempt(installer, program, environment, platform)
    _assert_event_outcome(first, program, profile, outcome)
    generation = program / "versions/1.2.3"
    frame_path = generation / ".carlina-bootstrap-claim"
    binding_path = generation / ".carlina-bootstrap-binding.json"
    frame, binding = frame_path.read_bytes(), binding_path.read_bytes()
    binding_stat = binding_path.stat()
    environment.update(
        CARLINA_TEST_EVENT_OUTCOME="success",
        CARLINA_TEST_REQUIRE_RESUME="1",
        CARLINA_TEST_FORBID_UNPACK="1",
    )
    second = _run_native_attempt(installer, program, environment, platform, "b" * 32)
    _assert_event_outcome(second, program, profile, "success", operation_id="b" * 32)
    records = [json.loads(line) for line in calls.read_text().splitlines()]
    assert [record["resumed"] for record in records] == [False, True]
    assert records[1]["claim"] == frame.decode("ascii").splitlines()[1]
    assert (
        records[1]["index"]
        == hashlib.sha256(
            Path(environment["CARLINA_TEST_INDEX"]).read_bytes()
        ).hexdigest()
    )
    assert records[1]["operation_id"] == "b" * 32
    assert frame_path.read_bytes() == frame
    assert binding_path.read_bytes() == binding
    after = binding_path.stat()
    assert (after.st_ino, after.st_dev, after.st_mtime_ns) == (
        binding_stat.st_ino,
        binding_stat.st_dev,
        binding_stat.st_mtime_ns,
    )
    assert "unpacking" not in {
        json.loads(line)["phase"] for line in second.stdout.splitlines()
    }


@pytest.mark.skipif(os.name == "nt", reason="Portable ordinary native resume")
@pytest.mark.parametrize("profile", ["documents", "workbench"])
@pytest.mark.parametrize("platform", ["linux-64", "win-64"])
def test_ordinary_native_rerun_forwards_its_claim_without_event_controls(
    tmp_path: Path, profile: str, platform: str
) -> None:
    installer, program, environment = _event_fixture(
        tmp_path, profile, platform, "success"
    )
    calls = tmp_path / "manager-calls.jsonl"
    environment["CARLINA_TEST_MANAGER_CALLS"] = str(calls)
    first = _run_native_attempt(installer, program, environment, platform, None)
    assert first.returncode == 0, first.stderr
    binding = program / "versions/1.2.3/.carlina-bootstrap-binding.json"
    before = binding.read_bytes()
    environment.update(CARLINA_TEST_REQUIRE_RESUME="1", CARLINA_TEST_FORBID_UNPACK="1")
    second = _run_native_attempt(installer, program, environment, platform, None)
    assert second.returncode == 0, second.stderr
    records = [json.loads(line) for line in calls.read_text().splitlines()]
    assert [record["resumed"] for record in records] == [False, True]
    assert records[1]["operation_id"] is None
    assert binding.read_bytes() == before
    assert program.joinpath("current").read_text().strip() == "1.2.3"
    assert not tuple(program.glob(".carlina-install*"))
    assert (
        program.parent.joinpath("owned data/marker").read_bytes()
        == b"synthetic private data"
    )


@pytest.mark.skipif(os.name == "nt", reason="Portable native resume refusal")
@pytest.mark.parametrize("platform", ["linux-64", "win-64"])
@pytest.mark.parametrize(
    "change", ["preparing", "identity", "claim", "binding", "root", "index", "protocol"]
)
def test_native_rerun_refuses_changed_or_unready_evidence_without_adoption(
    tmp_path: Path, platform: str, change: str
) -> None:
    installer, program, environment = _event_fixture(
        tmp_path, "documents", platform, "failure"
    )
    calls = tmp_path / "manager-calls.jsonl"
    environment["CARLINA_TEST_MANAGER_CALLS"] = str(calls)
    first = _run_native_attempt(installer, program, environment, platform)
    _assert_event_outcome(first, program, "documents", "failure")
    frame = program / "versions/1.2.3/.carlina-bootstrap-claim"
    binding = program / "versions/1.2.3/.carlina-bootstrap-binding.json"
    if change == "preparing":
        binding.unlink()
    elif change in {"identity", "claim"}:
        parts = frame.read_bytes().split(b"\n")
        parts[2 if change == "identity" else 1] = b"f" * (
            64 if change == "identity" else 32
        )
        frame.write_bytes(b"\n".join(parts))
    elif change == "binding":
        binding.write_bytes(b"{")
    elif change == "root":
        environment["CARLINA_HOME"] = str(tmp_path / "foreign data")
    elif change == "index":
        index = Path(environment["CARLINA_TEST_INDEX"])
        index.write_bytes(index.read_bytes() + b"\n")
    else:
        environment["CARLINA_TEST_MISSING_RESUME"] = "1"
    before_frame = frame.read_bytes()
    before_binding = binding.read_bytes() if binding.exists() else None
    before_calls = calls.read_bytes()
    environment.update(
        CARLINA_TEST_EVENT_OUTCOME="success",
        CARLINA_TEST_REQUIRE_RESUME="1",
        CARLINA_TEST_FORBID_UNPACK="1",
    )
    result = _run_native_attempt(installer, program, environment, platform, "b" * 32)
    assert result.returncode != 0
    events = [json.loads(line) for line in result.stdout.splitlines()]
    assert events[-1]["state"] == "failed", result.stderr
    assert events[-1]["error_code"] == (
        "verification_failed" if change == "index" else "incompatible_release"
    )
    assert calls.read_bytes() == before_calls
    assert frame.read_bytes() == before_frame
    assert (binding.read_bytes() if binding.exists() else None) == before_binding
    assert program.joinpath("current").read_text().strip() == "1.0.0"
    assert (
        program.parent.joinpath("owned data/marker").read_bytes()
        == b"synthetic private data"
    )
    assert not (tmp_path / "foreign data").exists()
    assert not tuple(program.glob(".carlina-install*"))


@pytest.mark.skipif(os.name == "nt", reason="Portable ready-orphan interruption")
@pytest.mark.parametrize("platform", ["linux-64", "win-64"])
def test_ready_orphan_can_resume_without_removing_its_old_scratch(
    tmp_path: Path, platform: str
) -> None:
    installer, program, environment = _event_fixture(
        tmp_path, "documents", platform, "success"
    )
    original = installer.read_text()
    marker = (
        "  ADOPTION_STARTED=1\n  release_bootstrap_lease"
        if platform == "linux-64"
        else "        $adoptionStarted = $true\n        Close-BootstrapLease"
    )
    kill = (
        "  kill -KILL $$"
        if platform == "linux-64"
        else "        [Diagnostics.Process]::GetCurrentProcess().Kill()"
    )
    assert original.count(marker) == 1
    installer.write_text(original.replace(marker, kill))
    calls = tmp_path / "manager-calls.jsonl"
    environment["CARLINA_TEST_MANAGER_CALLS"] = str(calls)
    first = _run_native_attempt(installer, program, environment, platform)
    assert first.returncode != 0
    assert not calls.exists()
    assert all(
        json.loads(line)["state"] == "running" for line in first.stdout.splitlines()
    )
    scratch = tuple(program.glob(".carlina-install*"))
    assert len(scratch) == 1
    binding = program / "versions/1.2.3/.carlina-bootstrap-binding.json"
    before = binding.read_bytes()
    installer.write_text(original)
    environment.update(CARLINA_TEST_REQUIRE_RESUME="1", CARLINA_TEST_FORBID_UNPACK="1")
    second = _run_native_attempt(installer, program, environment, platform, "b" * 32)
    assert second.returncode == 0, second.stderr
    events = [json.loads(line) for line in second.stdout.splitlines()]
    assert events[-1]["state"] == "succeeded"
    assert all(event["operation_id"] == "b" * 32 for event in events)
    assert tuple(program.glob(".carlina-install*")) == scratch
    assert binding.read_bytes() == before
    assert len(calls.read_text().splitlines()) == 1


@pytest.mark.skipif(os.name == "nt", reason="POSIX actual publisher composition")
@pytest.mark.parametrize(
    "outcome", ["success", "corrupt_package", "cancel_after_dependency"]
)
@pytest.mark.parametrize("supervised", [False, True])
def test_shell_installer_composes_with_the_real_publisher_protocol(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str, supervised: bool
) -> None:
    if supervised:
        capability = subprocess.run(
            [
                "sh",
                "-c",
                render_shell_preparation_helpers() + "\ncheck_preparation_capability",
            ],
            capture_output=True,
            timeout=10,
            check=False,
        )
        if capability.returncode != 0:
            pytest.skip("Native Linux user-scope custody is unavailable")
    unpacker = (
        b'#!/bin/sh\nset -eu\ncp "$1" "$CARLINA_TEST_RECONSTRUCTED"\n' + _EVENT_UNPACKER
    )
    plan, skeleton, archive, downloads, unpacker_url, _ = _publisher_plan_fixture(
        monkeypatch, unpacker_bytes=unpacker
    )
    publisher = render_publisher_bootstrap(plan, skeleton)
    installer, program, environment = _event_fixture(
        tmp_path, "documents", "linux-64", "success", publisher_payload=publisher
    )
    first = plan.downloads[0]
    if outcome == "corrupt_package":
        downloads[first.url] = b"x" * first.size
    mapping: dict[str, str] = {}
    for number, (url, payload) in enumerate(downloads.items()):
        path = tmp_path / f"dependency-{number}"
        path.write_bytes(payload)
        mapping[url] = str(path)
    dependencies = tmp_path / "dependencies.json"
    dependencies.write_text(json.dumps(mapping))
    cancel = tmp_path / "caller cancel request"
    calls = tmp_path / "download-calls.jsonl"
    capture = tmp_path / "reconstructed.tar"
    curl = tmp_path / "shim/curl"
    curl.write_text(
        "#!" + sys.executable + "\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "args = sys.argv[1:]\n"
        "url = next(arg for arg in args if arg.startswith('https://'))\n"
        "with Path(os.environ['CARLINA_TEST_CALLS']).open('a') as log:\n"
        "    log.write(json.dumps(url) + '\\n')\n"
        "mapping = json.loads(Path(os.environ['CARLINA_TEST_DEPENDENCIES']).read_text())\n"
        "if url in mapping:\n"
        "    source = mapping[url]\n"
        "else:\n"
        "    source = os.environ['CARLINA_TEST_INDEX' if url.endswith('/index.json') else 'CARLINA_TEST_ARTIFACT']\n"
        "if '-o' in args:\n"
        "    Path(args[args.index('-o') + 1]).write_bytes(Path(source).read_bytes())\n"
        "else:\n"
        "    sys.stdout.buffer.write(Path(source).read_bytes())\n"
        "    sys.stdout.buffer.flush()\n"
        "if os.environ['CARLINA_TEST_PUBLISHER_OUTCOME'] == 'cancel_after_dependency' and url == os.environ['CARLINA_TEST_FIRST_DEPENDENCY']:\n"
        "    Path(os.environ['CARLINA_TEST_CANCEL_PATH']).write_bytes(b'opaque caller request')\n",
        encoding="utf-8",
    )
    curl.chmod(0o700)
    # This protocol fixture logs tiny synthetic downloads. Real file-size
    # enforcement is covered by the actual curl/prlimit TLS fixtures.
    prlimit = tmp_path / "shim/prlimit"
    prlimit.write_text(
        '#!/bin/sh\nwhile [ "$1" != -- ]; do shift; done\nshift\nexec "$@"\n'
    )
    prlimit.chmod(0o700)
    environment.update(
        CARLINA_TEST_DEPENDENCIES=str(dependencies),
        CARLINA_TEST_CALLS=str(calls),
        CARLINA_TEST_RECONSTRUCTED=str(capture),
        CARLINA_TEST_PUBLISHER_OUTCOME=outcome,
        CARLINA_TEST_FIRST_DEPENDENCY=first.url,
        CARLINA_TEST_CANCEL_PATH=str(cancel),
    )
    result = subprocess.run(
        [
            *_native_event_command(installer, program, "linux-64"),
            "--operation-id",
            "a" * 32,
            "--cancel-file",
            str(cancel),
            *(["--supervised-preparation"] if supervised else []),
        ],
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    expected = (
        "success"
        if outcome == "success"
        else "cancelled"
        if outcome == "cancel_after_dependency"
        else "publisher_failure"
    )
    _assert_event_outcome(
        result, program, "documents", expected, before_adoption=outcome != "success"
    )
    events = [json.loads(line) for line in result.stdout.splitlines()]
    assert any(event["bytes_total"] == first.size for event in events)
    if outcome == "success":
        assert capture.read_bytes() == archive
        assert not cancel.exists()
    else:
        assert not capture.exists()
        assert unpacker_url not in [
            json.loads(line) for line in calls.read_text().splitlines()
        ]
    if outcome == "corrupt_package":
        assert events[-1]["error_code"] == "verification_failed"
    elif outcome == "cancel_after_dependency":
        assert result.returncode == 130
        assert cancel.read_bytes() == b"opaque caller request"
        assert plan.downloads[1].url not in calls.read_text()
        assert "opaque caller request" not in result.stdout + result.stderr
