"""Signed bootstrap dispatch and owned failure-cleanup regression checks."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from carlina.core.installation_lifecycle import (
    InstallationCancelled,
    InstallationErrorCode,
    InstallationEvent,
    InstallationLifecycle,
    InstallationState,
)
from carlina.core.publisher_installation import PublisherBootstrapFailure
from iarf_assistant.core import managed_install as install
from iarf_assistant.core.release_manifest import (
    parse_release_index,
    sign_release_index,
    trusted_key_record,
)
from scripts.render_managed_installers import render_installers

pytestmark = [pytest.mark.fast, pytest.mark.contract]


@pytest.mark.parametrize(
    ("kind", "suffix"),
    [
        (None, ".ps1"),
        ("", ".sh"),
        ("unknown", ".sh"),
        ("pixi-pack-bundle", ".ps1"),
        ("pixi-pack-executable", ".ps1"),
        ("publisher-bootstrap-executable", ".zip"),
        ("publisher-bootstrap-executable", ".exe"),
    ],
)
def test_incompatible_artifact_rejected_before_scratch_creation(
    tmp_path: Path, kind: str | None, suffix: str
) -> None:
    destination = tmp_path / "program" / "versions" / "1.2.3"
    destination.mkdir(parents=True)
    artifact = destination / ("runtime" + suffix)
    artifact.write_bytes(b"unused")
    with pytest.raises(install.ManagedInstallError, match="incompatible"):
        install._unpack_runtime(artifact, destination, kind=kind)  # pyright: ignore[reportPrivateUsage]
    assert not (tmp_path / "program" / "staging").exists()


def test_invalid_windows_bundle_cleans_owned_scratch(tmp_path: Path) -> None:
    destination = tmp_path / "program" / "versions" / "1.2.3"
    destination.mkdir(parents=True)
    artifact = destination / "runtime.zip"
    artifact.write_bytes(b"invalid zip")
    with pytest.raises(install.ManagedInstallError, match="invalid"):
        install._unpack_runtime(artifact, destination, kind="pixi-pack-bundle")  # pyright: ignore[reportPrivateUsage]
    assert not tuple((tmp_path / "program" / "staging").iterdir())


@pytest.mark.parametrize("fails", [False, True])
def test_windows_publisher_dispatch_checks_exit_and_cleans_scratch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, fails: bool
) -> None:
    destination = tmp_path / "program" / "versions" / "1.2.3"
    destination.mkdir(parents=True)
    artifact = destination / "runtime.ps1"
    artifact.write_bytes(b"unused verified script")
    commands: list[list[str]] = []

    def run(
        command: list[str], *, cwd: Path, environment: dict[str, str], timeout: int
    ) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        assert cwd == destination
        assert timeout == 3600
        scratch = Path(environment["TEMP"])
        assert str(scratch) == environment["TMP"] == environment["TMPDIR"]
        assert scratch.parent == tmp_path / "program" / "staging"
        (scratch / "owned.txt").write_bytes(b"temporary")
        if fails:
            raise install.ManagedInstallError("synthetic failure")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(install, "_publisher_powershell", lambda: "owned-powershell")
    monkeypatch.setattr(install, "_run_checked", run)
    if fails:
        with pytest.raises(install.ManagedInstallError, match="synthetic failure"):
            install._unpack_runtime(
                artifact, destination, kind="publisher-bootstrap-executable"
            )  # pyright: ignore[reportPrivateUsage]
    else:
        install._unpack_runtime(
            artifact, destination, kind="publisher-bootstrap-executable"
        )  # pyright: ignore[reportPrivateUsage]
    assert commands == [
        [
            "owned-powershell",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(artifact),
        ]
    ]
    assert not tuple((tmp_path / "program" / "staging").iterdir())


def test_power_shell_resolution_failure_also_cleans_scratch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "program" / "versions" / "1.2.3"
    destination.mkdir(parents=True)
    artifact = destination / "runtime.ps1"
    artifact.write_bytes(b"unused")

    def unavailable() -> str:
        raise install.ManagedInstallError("PowerShell unavailable")

    monkeypatch.setattr(install, "_publisher_powershell", unavailable)
    with pytest.raises(install.ManagedInstallError, match="PowerShell unavailable"):
        install._unpack_runtime(
            artifact, destination, kind="publisher-bootstrap-executable"
        )  # pyright: ignore[reportPrivateUsage]
    assert not tuple((tmp_path / "program" / "staging").iterdir())


def test_signed_v3_index_drives_native_installer_branch(tmp_path: Path) -> None:
    payload = {
        "format": "carlina.release-index",
        "schema_version": 3,
        "application_profile": "documents",
        "channel": "preview",
        "generated_at": "2026-10-07T12:00:00Z",
        "signatures": [],
        "releases": [
            {
                "version": "1.2.3",
                "published_at": "2026-10-07T12:00:00Z",
                "addon_api_version": "1.0",
                "database": {
                    "known_revisions": ["head"],
                    "heads": ["head"],
                    "rollback": "restore_required",
                },
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
                "artifacts": [
                    {
                        "platform": platform,
                        "kind": "publisher-bootstrap-executable",
                        "filename": "runtime" + suffix,
                        "url": "runtime" + suffix,
                        "sha256": "a" * 64,
                        "size": 100,
                    }
                    for platform, suffix in [("linux-64", ".sh"), ("win-64", ".ps1")]
                ],
            }
        ],
    }
    private_key = Ed25519PrivateKey.generate()
    signed = sign_release_index(parse_release_index(payload), private_key)
    index_path = tmp_path / "index.json"
    index_path.write_text(json.dumps(signed.to_dict()), encoding="utf-8")
    trust_path = tmp_path / "trust.json"
    trust_path.write_text(
        json.dumps(
            {
                "format": "carlina.release-trust",
                "schema_version": 1,
                "keys": [trusted_key_record(private_key)],
            }
        ),
        encoding="utf-8",
    )
    outputs = render_installers(
        index_path,
        trust_path,
        index_url="https://downloads.example.test/index.json",
        output_dir=tmp_path / "installers",
    )
    windows = next(path for path in outputs if path.suffix == ".ps1").read_text(
        encoding="utf-8"
    )
    linux = next(path for path in outputs if path.suffix == ".sh").read_text(
        encoding="utf-8"
    )
    assert "Join-Path $PSHOME 'powershell.exe'" in windows
    assert "'-NonInteractive'" in windows
    assert "Expand-VerifiedWindowsBundle $artifactPath $bundleDir" not in windows
    assert windows.index("Assert-FileHash $artifactPath") < windows.index(
        "Invoke-CheckedNative -FilePath $unpackerPath"
    )
    assert windows.index("Assert-FileHash $artifactPath") < windows.index(
        "Invoke-PublisherBootstrap -FilePath $unpackerPath"
    )
    assert windows.index("Assert-ApplicationProfile $python") < windows.index(
        "'-I', '-m', 'carlina.documents'"
    )
    # Helper definitions precede execution; check admission against both calls.
    linux_execution = linux[linux.index("installation_checkpoint preflight true\n") :]
    assert linux_execution.index('hash_file "$ARTIFACT_PATH"') < linux_execution.index(
        'bash "$ARTIFACT_PATH"'
    )
    assert linux_execution.index('hash_file "$ARTIFACT_PATH"') < linux_execution.index(
        "if run_publisher_bootstrap; then"
    )
    assert "-I -m carlina.documents install" in linux


@pytest.mark.skipif(os.name == "nt", reason="Portable PowerShell control-flow fixture")
@pytest.mark.parametrize("outcome", ["success", "cancel", "failure"])
def test_event_unpack_executes_powershell_publisher_with_owned_controls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
    outcome: str,
) -> None:
    powershell = shutil.which("pwsh")
    if powershell is None:
        pytest.skip("PowerShell is unavailable for portable publisher execution.")
    destination = tmp_path / "program/versions/1.1.0"
    destination.mkdir(parents=True)
    artifact = tmp_path / "publisher.ps1"
    marker = tmp_path / "child-finished"
    artifact.write_text(
        "param([switch]$ProgressRecordsV1, [string]$CancelFile)\n"
        "$ErrorActionPreference = 'Stop'\n"
        "if (-not $ProgressRecordsV1 -or -not $CancelFile) { exit 2 }\n"
        "[Console]::Out.WriteLine('carlina.publisher-bootstrap/1 ready')\n"
        "[Console]::Out.WriteLine('carlina.publisher-bootstrap/1 phase environment false null null')\n"
        "[Console]::Error.WriteLine('synthetic publisher diagnostic')\n"
        "if ($env:CARLINA_TEST_UPDATER_OUTCOME -eq 'cancel') {\n"
        "  [IO.File]::WriteAllText($env:CARLINA_TEST_UPDATER_READY, 'ready')\n"
        "  while (-not [IO.File]::Exists($CancelFile)) { Start-Sleep -Milliseconds 10 }\n"
        "}\n"
        "[IO.File]::WriteAllText($env:CARLINA_TEST_UPDATER_FINISHED, 'finished')\n"
        "[Console]::Out.WriteLine('carlina.publisher-bootstrap/1 phase environment true null null')\n"
        "switch ($env:CARLINA_TEST_UPDATER_OUTCOME) {\n"
        "  'cancel' { [Console]::Out.WriteLine('carlina.publisher-bootstrap/1 result cancelled cancelled true'); exit 130 }\n"
        "  'failure' { [Console]::Out.WriteLine('carlina.publisher-bootstrap/1 result failed verification_failed false'); exit 7 }\n"
        "  default { [Console]::Out.WriteLine('carlina.publisher-bootstrap/1 result succeeded null false'); exit 0 }\n"
        "}\n",
        encoding="utf-8",
    )
    ready = tmp_path / "request-ready"
    monkeypatch.setenv("CARLINA_TEST_UPDATER_OUTCOME", outcome)
    monkeypatch.setenv("CARLINA_TEST_UPDATER_READY", str(ready))
    monkeypatch.setenv("CARLINA_TEST_UPDATER_FINISHED", str(marker))
    monkeypatch.setattr(install, "_publisher_powershell", lambda: powershell)
    events: list[InstallationEvent] = []
    context = InstallationLifecycle(
        "update", "documents", event_sink=events.append, cancel_requested=ready.exists
    )

    def execute() -> None:
        install._unpack_runtime(
            artifact,
            destination,
            kind="publisher-bootstrap-executable",
            lifecycle=context,
        )

    if outcome == "cancel":
        with pytest.raises(InstallationCancelled):
            execute()
    elif outcome == "failure":
        with pytest.raises(PublisherBootstrapFailure) as raised:
            execute()
        assert raised.value.error_code is InstallationErrorCode.VERIFICATION_FAILED
        assert raised.value.retryable is False
    else:
        execute()

    output = capfd.readouterr()
    assert output.out == ""
    assert "synthetic publisher diagnostic" in output.err
    assert marker.read_text() == "finished"
    assert not list((tmp_path / "program/staging").iterdir())
    assert all(event.state is InstallationState.RUNNING for event in events)
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    assert {event.operation_id for event in events} == {context.operation_id}
