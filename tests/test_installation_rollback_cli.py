"""Rollback CLI event protocol and legacy-output compatibility."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from carlina.core.installation_lifecycle import (
    InstallationErrorCode,
    InstallationLifecycle,
    InstallationPhase,
)
from carlina.runtime_profiles import RuntimeProfile
from iarf_assistant import bootstrap_manager, distribution_cli
from iarf_assistant.core import app_paths
from iarf_assistant.core.app_paths import resolve_app_paths
from iarf_assistant.core.managed_install import ManagedInstallError, RollbackResult

pytestmark = [pytest.mark.fast, pytest.mark.contract]


def _arguments(controls: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    distribution_cli.add_distribution_parsers(
        parser.add_subparsers(dest="command", required=True)
    )
    return parser.parse_args(["rollback", *controls])


@pytest.mark.parametrize("profile", list(RuntimeProfile))
@pytest.mark.parametrize("mode", ["plain", "json", "events", "cancel", "failure"])
def test_rollback_cli_protocol_and_legacy_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    profile: RuntimeProfile,
    mode: str,
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
    calls: list[tuple[bool, bool]] = []
    result = RollbackResult("b" * 32, "1.1.0", "1.0.0", True, None)

    def rollback(
        *, restore_data: bool, lifecycle: InstallationLifecycle | None = None
    ) -> RollbackResult:
        calls.append((restore_data, lifecycle is not None))
        if lifecycle is not None:
            lifecycle.checkpoint(InstallationPhase.PREFLIGHT)
            if mode == "failure":
                lifecycle.fail(InstallationErrorCode.OPERATION_FAILED)
                raise ManagedInstallError("private-path-or-provider-value")
            print("bounded maintainer diagnostic")
            lifecycle.advance(InstallationPhase.ACTIVATION, cancellation_safe=False)
            lifecycle.succeed()
        return result

    manager = SimpleNamespace(application_profile=profile, rollback=rollback)
    monkeypatch.setattr(distribution_cli, "_manager", lambda _args, _paths: manager)
    controls = ["--restore-data"]
    if mode == "json":
        controls.append("--json")
    elif mode in {"events", "cancel", "failure"}:
        controls.extend(["--events-jsonl", "--operation-id", "a" * 32])
    request = tmp_path / "cancel request"
    if mode == "cancel":
        request.touch()
        controls.extend(["--cancel-file", str(request)])

    code = distribution_cli.handle_distribution_command(_arguments(controls), paths)
    captured = capsys.readouterr()
    assert calls == [(True, mode not in {"plain", "json"})]
    assert code == {"cancel": 130, "failure": 1}.get(mode, 0)
    if mode == "plain":
        assert captured.out == (
            "Rolled back Carlina 1.1.0 to 1.0.0.\nPaired data snapshot restored.\n"
        )
        assert not captured.err
    elif mode == "json":
        assert json.loads(captured.out) == result.to_dict()
        assert not captured.err
    else:
        events = [json.loads(line) for line in captured.out.splitlines()]
        assert {event["operation"] for event in events} == {"rollback"}
        assert {event["application_profile"] for event in events} == {profile.value}
        assert {event["operation_id"] for event in events} == {"a" * 32}
        assert [event["sequence"] for event in events] == list(
            range(1, len(events) + 1)
        )
        terminal = [event for event in events if event["state"] != "running"]
        assert len(terminal) == 1
        assert terminal[0]["state"] == {"cancel": "cancelled", "failure": "failed"}.get(
            mode, "succeeded"
        )
        assert "private-path-or-provider-value" not in captured.out + captured.err
        if mode == "cancel":
            assert request.is_file()
        elif mode == "events":
            assert captured.err == "bounded maintainer diagnostic\n"


@pytest.mark.parametrize(
    "controls",
    [
        ["--operation-id", "a" * 32],
        ["--cancel-file", "request"],
        ["--events-jsonl", "--stop"],
        ["--events-jsonl", "--systemd-unit", "synthetic.service"],
    ],
)
def test_rollback_event_controls_validate_before_runtime_access(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    controls: list[str],
) -> None:
    args = _arguments(controls)
    monkeypatch.setattr(
        bootstrap_manager,
        "_active_paths",
        lambda: pytest.fail("rollback validation inspected runtime paths"),
    )
    assert bootstrap_manager._dispatch_command(args, argparse.ArgumentParser()) == 1
    captured = capsys.readouterr()
    if args.events_jsonl:
        assert not captured.out
        assert captured.err
    else:
        assert captured.out


def test_rollback_json_and_events_are_mutually_exclusive() -> None:
    with pytest.raises(SystemExit):
        _arguments(["--json", "--events-jsonl"])
