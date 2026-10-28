"""CLI event sequence continuation and exclusive terminal-file handoff."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from carlina.core.installation_handoff import read_terminal_event
from carlina.core.installation_lifecycle import (
    InstallationErrorCode,
    InstallationEvent,
    InstallationLifecycle,
    InstallationPhase,
)
from carlina.core.recovery_policy import legacy_opaque_recovery_plan
from carlina.runtime_profiles import RuntimeProfile
from iarf_assistant import bootstrap_manager, distribution_cli
from iarf_assistant.core import app_paths
from iarf_assistant.core.app_paths import resolve_app_paths
from iarf_assistant.core.managed_install import (
    ManagedInstallError,
    RollbackResult,
    UpdateResult,
)

pytestmark = [pytest.mark.fast, pytest.mark.contract]


def _arguments(controls: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    distribution_cli.add_distribution_parsers(parser.add_subparsers(dest="command"))
    return parser.parse_args(["rollback", *controls])


@pytest.mark.parametrize("profile", list(RuntimeProfile))
@pytest.mark.parametrize(
    "outcome", ["success", "failure", "cancelled", "transport_failure"]
)
def test_cli_handoff_defers_terminal_until_action_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    profile: RuntimeProfile,
    outcome: str,
) -> None:
    monkeypatch.setattr(
        app_paths,
        "_platform_roots",
        lambda _profile=None: tuple(
            tmp_path / name for name in ("data", "config", "cache", "logs")
        ),
    )
    paths = resolve_app_paths(
        tmp_path / "source",
        environment={
            "CARLINA_HOME": str(tmp_path / "data"),
            "CARLINA_PROFILE": profile.value,
        },
        allow_legacy=False,
    )
    terminal = tmp_path / "terminal.jsonl"
    cancel = tmp_path / "cancel request"
    if outcome == "cancelled":
        cancel.touch()
    cleaned: list[bool] = []

    def rollback(
        *, restore_data: bool, lifecycle: InstallationLifecycle
    ) -> RollbackResult:
        assert not restore_data
        try:
            lifecycle.checkpoint(InstallationPhase.PREFLIGHT)
            if outcome == "failure":
                lifecycle.fail(InstallationErrorCode.OPERATION_FAILED)
                raise ManagedInstallError("synthetic failure")
            lifecycle.succeed()
            assert not terminal.exists()
            return RollbackResult("b" * 32, "1.1.0", "1.0.0", False, None)
        finally:
            cleaned.append(True)

    write = distribution_cli.write_terminal_event

    def observed_write(path: Path, event: InstallationEvent) -> None:
        assert cleaned == [True]
        if outcome == "transport_failure":
            raise OSError("synthetic failed handoff write")
        write(path, event)

    manager = SimpleNamespace(application_profile=profile, rollback=rollback)
    monkeypatch.setattr(distribution_cli, "_manager", lambda _args, _paths: manager)
    monkeypatch.setattr(distribution_cli, "write_terminal_event", observed_write)
    args = _arguments(
        [
            "--events-jsonl",
            "--operation-id",
            "a" * 32,
            "--event-sequence-start",
            "17",
            "--event-terminal-file",
            str(terminal),
            "--cancel-file",
            str(cancel),
        ]
    )
    code = distribution_cli.handle_distribution_command(args, paths)
    assert code is not None
    captured = capsys.readouterr()
    running = [json.loads(line) for line in captured.out.splitlines()]
    assert all(event["state"] == "running" for event in running)
    assert [event["sequence"] for event in running] == list(
        range(18, 18 + len(running))
    )
    assert cleaned == [True]
    assert (
        code
        == {"success": 0, "failure": 1, "cancelled": 130, "transport_failure": 1}[
            outcome
        ]
    )
    if outcome == "transport_failure":
        assert not terminal.exists()
    else:
        event = read_terminal_event(
            terminal,
            operation="rollback",
            application_profile=profile,
            operation_id="a" * 32,
            sequence_start=17,
            exit_code=code,
        )
        assert event.sequence == 18 + len(running)


@pytest.mark.parametrize(
    "controls",
    [
        ["--event-sequence-start", "0"],
        ["--event-terminal-file", "terminal"],
        ["--events-jsonl", "--event-sequence-start", "-1"],
    ],
)
def test_handoff_controls_reject_before_runtime_access(
    monkeypatch: pytest.MonkeyPatch, controls: list[str]
) -> None:
    monkeypatch.setattr(
        bootstrap_manager, "_active_paths", lambda: pytest.fail("runtime accessed")
    )
    assert (
        bootstrap_manager._dispatch_command(
            _arguments(controls), argparse.ArgumentParser()
        )
        == 1
    )


def test_existing_handoff_destination_is_not_clobbered_or_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    terminal = tmp_path / "existing.jsonl"
    terminal.write_bytes(b"existing owned bytes")
    monkeypatch.setattr(
        bootstrap_manager, "_active_paths", lambda: pytest.fail("runtime accessed")
    )
    args = _arguments(["--events-jsonl", "--event-terminal-file", str(terminal)])
    assert bootstrap_manager._dispatch_command(args, argparse.ArgumentParser()) == 1
    assert terminal.read_bytes() == b"existing owned bytes"


@pytest.mark.parametrize("profile", list(RuntimeProfile))
def test_real_manager_entrypoint_keeps_handoff_stdout_machine_readable(
    tmp_path: Path, profile: RuntimeProfile
) -> None:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("CARLINA_", "IARF_"))
    }
    environment.update(
        {
            "CARLINA_HOME": str(tmp_path / "owned-data"),
            "CARLINA_PROFILE": profile.value,
            "CARLINA_SECRETS_DIR": str(tmp_path / "secrets"),
            "CARLINA_ADDON_ENVIRONMENTS_DIR": str(tmp_path / "addon-environments"),
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_DATA_HOME": str(tmp_path / "platform-data"),
            "XDG_CACHE_HOME": str(tmp_path / "cache"),
            "XDG_STATE_HOME": str(tmp_path / "state"),
            "APPDATA": str(tmp_path / "appdata"),
            "LOCALAPPDATA": str(tmp_path / "localappdata"),
        }
    )
    terminal = tmp_path / "terminal.jsonl"
    entrypoint = (
        "carlina.documents"
        if profile is RuntimeProfile.DOCUMENTS
        else "carlina.bootstrap_manager"
    )
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-m",
            entrypoint,
            "rollback",
            "--program-root",
            str(tmp_path / "program"),
            "--events-jsonl",
            "--operation-id",
            "a" * 32,
            "--event-sequence-start",
            "9",
            "--event-terminal-file",
            str(terminal),
        ],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 1, result.stderr
    running = [json.loads(line) for line in result.stdout.splitlines()]
    assert running
    assert all(event["state"] == "running" for event in running)
    assert str(tmp_path) not in result.stdout
    event = read_terminal_event(
        terminal,
        operation="rollback",
        application_profile=profile,
        operation_id="a" * 32,
        sequence_start=9,
        exit_code=1,
    )
    assert event.sequence == 10 + len(running)


def _install_arguments(controls: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    distribution_cli.add_distribution_parsers(parser.add_subparsers(dest="command"))
    return parser.parse_args(
        [
            "install",
            "--manifest",
            "frozen-index.json",
            "--artifact",
            "verified-runtime",
            "--adopt-version-dir",
            "program/versions/1.1.0",
            *controls,
        ]
    )


@pytest.mark.parametrize("events", [False, True])
@pytest.mark.parametrize(
    "controls",
    [
        ["--expected-index-sha256", "a" * 63],
        ["--expected-index-sha256", "A" * 64],
        ["--expected-index-sha256", "g" * 64],
        ["--retry-transaction", "a" * 31],
        ["--retry-transaction", "A" * 32],
        ["--retry-transaction", "a" * 32],
        ["--retry-transaction", "a" * 32, "--version", "1.1.0"],
        ["--retry-transaction", "a" * 32, "--expected-index-sha256", "b" * 64],
        [
            "--retry-transaction",
            "a" * 32,
            "--expected-index-sha256",
            "b" * 64,
            "--version",
            "",
        ],
        ["--resume-bootstrap-claim", "A" * 32],
        ["--resume-bootstrap-claim", "a" * 31],
        ["--resume-bootstrap-claim", "a" * 32],
        ["--resume-bootstrap-claim", "a" * 32, "--version", "1.1.0"],
        ["--resume-bootstrap-claim", "a" * 32, "--expected-index-sha256", "b" * 64],
        [
            "--resume-bootstrap-claim",
            "a" * 32,
            "--retry-transaction",
            "b" * 32,
            "--version",
            "1.1.0",
            "--expected-index-sha256",
            "c" * 64,
        ],
    ],
)
def test_adoption_retry_controls_reject_before_runtime_access(
    monkeypatch: pytest.MonkeyPatch, controls: list[str], events: bool
) -> None:
    monkeypatch.setattr(
        bootstrap_manager, "_active_paths", lambda: pytest.fail("runtime accessed")
    )
    args = _install_arguments([*controls, *(["--events-jsonl"] if events else [])])
    assert bootstrap_manager._dispatch_command(args, argparse.ArgumentParser()) == 1


@pytest.mark.parametrize("profile", list(RuntimeProfile))
@pytest.mark.parametrize("output", ["plain", "json", "events"])
@pytest.mark.parametrize("control", ["retry", "native"])
def test_adoption_retry_cli_forwards_frozen_controls_and_preserves_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    profile: RuntimeProfile,
    output: str,
    control: str,
) -> None:
    calls: list[dict[str, object]] = []

    def adopt(
        manifest: str,
        artifact: str,
        version_dir: str,
        *,
        channel: str,
        requested_version: str | None,
        allow_unsigned_local: bool,
        **controls: object,
    ) -> UpdateResult:
        assert (manifest, artifact, version_dir) == (
            "frozen-index.json",
            "verified-runtime",
            "program/versions/1.1.0",
        )
        assert (channel, requested_version, allow_unsigned_local) == (
            "stable",
            "1.1.0",
            False,
        )
        calls.append(controls)
        context = controls.get("lifecycle")
        if isinstance(context, InstallationLifecycle):
            context.advance(InstallationPhase.VERIFICATION, cancellation_safe=True)
            context.succeed()
        return UpdateResult(
            "c" * 32,
            "1.0.0",
            "1.1.0",
            None,
            None,
            None,
            False,
            legacy_opaque_recovery_plan(),
        )

    manager = SimpleNamespace(
        application_profile=profile, adopt_installed_release=adopt
    )
    monkeypatch.setattr(distribution_cli, "_manager", lambda _args, _paths: manager)
    monkeypatch.setattr(
        app_paths,
        "_platform_roots",
        lambda _profile=None: tuple(
            tmp_path / name for name in ("data", "config", "cache", "logs")
        ),
    )
    paths = resolve_app_paths(
        tmp_path / "source",
        environment={
            "CARLINA_HOME": str(tmp_path / "data"),
            "CARLINA_PROFILE": profile.value,
        },
        allow_legacy=False,
    )
    controls = [
        "--version",
        "1.1.0",
        "--expected-index-sha256",
        "a" * 64,
        "--retry-transaction" if control == "retry" else "--resume-bootstrap-claim",
        "b" * 32,
    ]
    if output != "plain":
        controls.append("--events-jsonl" if output == "events" else "--json")
    args = _install_arguments(controls)
    assert distribution_cli.handle_distribution_command(args, paths) == 0
    assert len(calls) == 1
    forwarded = calls[0]
    assert forwarded["expected_index_sha256"] == "a" * 64
    key = "retry_transaction_id" if control == "retry" else "resume_bootstrap_claim_id"
    assert forwarded[key] == "b" * 32
    other = (
        "resume_bootstrap_claim_id" if control == "retry" else "retry_transaction_id"
    )
    assert other not in forwarded
    captured = capsys.readouterr()
    if output == "plain":
        assert "Installed and activated Carlina 1.1.0." in captured.out
        assert "lifecycle" not in forwarded
    elif output == "json":
        assert json.loads(captured.out)["transaction_id"] == "c" * 32
        assert "lifecycle" not in forwarded
    else:
        events = [json.loads(line) for line in captured.out.splitlines()]
        assert [event["state"] for event in events] == ["running", "succeeded"]
        assert {event["application_profile"] for event in events} == {profile.value}
        assert len({event["operation_id"] for event in events}) == 1
        assert "c" * 32 not in captured.out
    assert captured.err == ""


@pytest.mark.parametrize("expected", [None, "a" * 64])
def test_ordinary_adoption_cli_omits_unused_retry_keywords(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, expected: str | None
) -> None:
    calls: list[dict[str, object]] = []

    def adopt(*args: object, **options: object) -> UpdateResult:
        calls.append(options)
        return UpdateResult(
            "c" * 32,
            None,
            "1.1.0",
            None,
            None,
            None,
            True,
            legacy_opaque_recovery_plan(),
        )

    monkeypatch.setattr(
        distribution_cli,
        "_manager",
        lambda _args, _paths: SimpleNamespace(adopt_installed_release=adopt),
    )
    monkeypatch.setattr(
        app_paths,
        "_platform_roots",
        lambda _profile=None: tuple(
            tmp_path / name for name in ("data", "config", "cache", "logs")
        ),
    )
    paths = resolve_app_paths(
        tmp_path / "source",
        environment={"CARLINA_HOME": str(tmp_path / "data")},
        allow_legacy=False,
    )
    controls = ["--json"]
    if expected is not None:
        controls += ["--expected-index-sha256", expected]
    assert (
        distribution_cli.handle_distribution_command(
            _install_arguments(controls), paths
        )
        == 0
    )
    assert len(calls) == 1
    assert "retry_transaction_id" not in calls[0]
    assert "resume_bootstrap_claim_id" not in calls[0]
    assert "lifecycle" not in calls[0]
    assert calls[0].get("expected_index_sha256") == expected
    assert ("expected_index_sha256" in calls[0]) is (expected is not None)


@pytest.mark.parametrize("profile", list(RuntimeProfile))
def test_managed_start_refuses_an_unknown_isolated_activation_outcome(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    profile: RuntimeProfile,
) -> None:
    monkeypatch.setenv("CARLINA_PROFILE", profile.value)
    monkeypatch.setenv("CARLINA_PROGRAM_ROOT", str(tmp_path / "program"))
    monkeypatch.setattr(
        app_paths,
        "_platform_roots",
        lambda _profile=None: tuple(
            tmp_path / name for name in ("data", "config", "cache", "logs")
        ),
    )
    paths = resolve_app_paths(
        tmp_path / "source",
        environment={
            "CARLINA_HOME": str(tmp_path / "data"),
            "CARLINA_PROFILE": profile.value,
        },
        allow_legacy=False,
    )
    layout = distribution_cli.ManagedInstallationLayout.resolve(tmp_path / "program")
    layout.ensure()
    layout.version_dir("1.0.0").mkdir()
    layout.version_dir("1.1.0").mkdir()
    layout.activate("1.0.0")
    payload = {
        "format": "carlina.update-journal",
        "schema_version": 1,
        "transaction_id": "a" * 32,
        "operation": "adopt",
        "phase": "failed",
        "application_profile": profile.value,
        "previous_version": "1.0.0",
        "target_version": "1.1.0",
        "isolated_target_execution_started": True,
        "activation_outcome_unknown": True,
        "recovery_plan": {"selected_coverage": "none"},
    }
    layout.journal_file.write_text(json.dumps(payload), encoding="utf-8")
    journal_bytes = layout.journal_file.read_bytes()
    assert layout.read_pending_update() == payload
    monkeypatch.setattr(bootstrap_manager, "_active_paths", lambda: paths)
    monkeypatch.setattr(
        bootstrap_manager, "_managed_installation_layout", lambda: layout
    )
    monkeypatch.setattr(
        bootstrap_manager,
        "_dispatch_command",
        lambda *_args: pytest.fail("A fenced runtime was started."),
    )

    assert bootstrap_manager._command_main(["start"]) == 1

    assert "Managed start refused:" in capsys.readouterr().out
    assert layout.current_version() == "1.0.0"
    assert layout.journal_file.read_bytes() == journal_bytes
