"""Retire only proven, unadopted native failures before retrying extraction."""

from __future__ import annotations

import base64
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from filelock import FileLock, Timeout
from test_installer_handoff_cleanup import (
    _EVENT_UNPACKER,
    _event_fixture,
    _publisher_fixture_payload,
)
from test_supervised_installers import _require_native_scope, _supervised_command

from carlina.core.bootstrap_ownership import BootstrapIdentity, bootstrap_claim_bytes
from carlina.core.native_preparation import (
    PREPARATION_NAME,
    PREPARATION_RETIREMENT_DIRNAME,
    PreparationCompletion,
    encode_preparation_completion,
    parse_preparation_completion,
)

pytestmark = [
    pytest.mark.fast,
    pytest.mark.contract,
    pytest.mark.skipif(
        not sys.platform.startswith("linux"), reason="Native Linux carrier"
    ),
]

_OLD_CLAIM = "0123456789abcdef0123456789abcdef"


def _snapshot(path: Path) -> dict[str, bytes]:
    return {
        str(entry.relative_to(path)): entry.read_bytes()
        for entry in path.rglob("*")
        if entry.is_file()
    }


def _seed_stopped_generation(
    tmp_path: Path,
) -> tuple[Path, Path, dict[str, str], Path]:
    installer, program, environment = _event_fixture(
        tmp_path, "documents", "linux-64", "success"
    )
    program.joinpath("current").unlink()
    state = program / "state"
    state.mkdir()
    state.joinpath("managed-install.lock").touch()
    generation = program / "versions/1.2.3"
    generation.mkdir()
    match = re.search(
        r"^BOOTSTRAP_IDENTITY_BASE64=(.+)$", installer.read_text(), re.MULTILINE
    )
    assert match is not None
    identity = BootstrapIdentity(
        **json.loads(base64.b64decode(shlex.split(match[1])[0]))
    )
    generation.joinpath(".carlina-bootstrap-claim").write_bytes(
        bootstrap_claim_bytes(identity, _OLD_CLAIM)
    )
    generation.joinpath(PREPARATION_NAME).write_bytes(
        encode_preparation_completion(
            PreparationCompletion(
                _OLD_CLAIM, identity.digest, str(program.resolve()), 7
            )
        )
    )
    generation.joinpath("partial output").write_bytes(b"preserved failed native bytes")
    return installer, program, environment, generation


@pytest.mark.parametrize(
    ("profile", "publisher"),
    [("documents", False), ("workbench", False), ("documents", True)],
)
def test_dead_controller_failed_preparation_retires_intact_and_reextracts(
    tmp_path: Path, profile: str, publisher: bool
) -> None:
    _require_native_scope()
    prefix = b"#!/bin/sh\nset -eu\n"
    original = (
        _publisher_fixture_payload("linux-64", "success")
        if publisher
        else _EVENT_UNPACKER
    )
    assert original.startswith(prefix)
    failure_result = (
        b"printf '%s\\n' 'carlina.publisher-bootstrap/1 ready' "
        b"'carlina.publisher-bootstrap/1 result failed operation_failed false'\n"
        if publisher
        else b""
    )
    payload = (
        prefix
        + b"""
printf '%s\n' "$CARLINA_TEST_FAIL_ONCE" >> "$CARLINA_TEST_UNPACK_CALLS"
if [ "$CARLINA_TEST_FAIL_ONCE" = 1 ]; then
  printf started > "$CARLINA_TEST_BARRIER.started"
  while [ ! -f "$CARLINA_TEST_BARRIER.release" ]; do sleep 0.01; done
  printf 'failed native output' > 'partial output'
"""
        + failure_result
        + b"exit 7\nfi\n"
        + original.removeprefix(prefix)
    )
    installer, program, environment = _event_fixture(
        tmp_path,
        profile,
        "linux-64",
        "success",
        publisher_payload=payload if publisher else None,
        unpacker_payload=None if publisher else payload,
    )
    program.joinpath("current").unlink()
    barrier = tmp_path / "preparation barrier"
    unpack_calls = tmp_path / "unpack-calls"
    manager_calls = tmp_path / "manager-calls.jsonl"
    environment.update(
        CARLINA_TEST_FAIL_ONCE="1",
        CARLINA_TEST_BARRIER=str(barrier),
        CARLINA_TEST_UNPACK_CALLS=str(unpack_calls),
        CARLINA_TEST_MANAGER_CALLS=str(manager_calls),
    )
    generation = program / "versions/1.2.3"
    proof = generation / PREPARATION_NAME
    with (
        tmp_path.joinpath("initial.stdout").open("w") as stdout,
        tmp_path.joinpath("initial.stderr").open("w") as stderr,
    ):
        controller = subprocess.Popen(
            _supervised_command(installer, program),
            env=environment,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 30
            while not barrier.with_suffix(".started").exists():
                assert controller.poll() is None
                assert time.monotonic() < deadline
                time.sleep(0.01)
            controller.kill()
            assert controller.wait(timeout=5) == -signal.SIGKILL
            assert not proof.exists()
            with pytest.raises(Timeout):
                FileLock(program / "state/managed-install.lock").acquire(timeout=0)
            competing = subprocess.run(
                _supervised_command(installer, program),
                env=environment,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            assert competing.returncode != 0
            assert "Another managed installation" in competing.stderr
            assert not program.joinpath(
                "state", PREPARATION_RETIREMENT_DIRNAME
            ).exists()
            barrier.with_suffix(".release").write_text("continue")
            deadline = time.monotonic() + 30
            while not proof.exists():
                assert time.monotonic() < deadline
                time.sleep(0.01)
            with FileLock(program / "state/managed-install.lock").acquire(timeout=5):
                assert (
                    parse_preparation_completion(proof.read_bytes()).native_exit_code
                    == 7
                )
            assert not manager_calls.exists()
            old_bytes = _snapshot(generation)
            old_inode = generation.stat().st_ino
            old_claim = (
                generation.joinpath(".carlina-bootstrap-claim")
                .read_text()
                .splitlines()[1]
            )
            old_scratch = tuple(program.glob(".carlina-install.*"))
            environment["CARLINA_TEST_FAIL_ONCE"] = "0"
            command = _supervised_command(installer, program)
            command[-1] = "b" * 32
            retry = subprocess.run(
                command,
                env=environment,
                capture_output=True,
                text=True,
                timeout=40,
                check=False,
            )
            assert retry.returncode == 0, retry.stderr
            retired = (
                program
                / "state"
                / PREPARATION_RETIREMENT_DIRNAME
                / f"1.2.3.{old_claim}"
            )
            assert _snapshot(retired) == old_bytes
            assert retired.stat().st_ino == old_inode
            assert (
                generation.joinpath(".carlina-bootstrap-claim")
                .read_text()
                .splitlines()[1]
                != old_claim
            )
            assert unpack_calls.read_text().splitlines() == ["1", "0"]
            assert not json.loads(manager_calls.read_text())["resumed"]
            assert program.joinpath("current").read_text().strip() == "1.2.3"
            assert tuple(program.glob(".carlina-install.*")) == old_scratch
            assert (
                program.joinpath("versions/1.0.0/preserved").read_bytes()
                == b"previous generation"
            )
            assert (
                Path(environment["CARLINA_HOME"]).joinpath("marker").read_bytes()
                == b"synthetic private data"
            )
            events = [json.loads(line) for line in retry.stdout.splitlines()]
            assert [event["sequence"] for event in events] == list(
                range(1, len(events) + 1)
            )
            assert {event["operation_id"] for event in events} == {"b" * 32}
            assert {event["application_profile"] for event in events} == {profile}
            recovery = [event for event in events if event["phase"] == "recovery"]
            assert [event["cancellation_safe"] for event in recovery] == [True, False]
            assert events[-1]["state"] == "succeeded"
            assert str(retired) not in retry.stdout
        finally:
            barrier.with_suffix(".release").write_text("continue")
            if controller.poll() is None:
                controller.kill()
            controller.wait(timeout=5)


@pytest.mark.parametrize(
    "problem",
    [
        "current",
        "previous",
        "installation",
        "journal",
        "unknown-state",
        "history",
        "receipt",
        "other-receipt",
        "binding",
        "binding-intent",
        "missing-proof",
        "successful-proof",
        "claim-drift",
        "proof-drift",
        "archive-link",
        "archive-collision",
        "lax-archive",
        "ordinary-mode",
        "bad-index",
        "bad-artifact",
        "state-after-download",
        "rename-collision",
    ],
)
def test_recovery_preserves_unknown_managed_or_changed_evidence(
    tmp_path: Path, problem: str
) -> None:
    _require_native_scope()
    installer, program, environment, generation = _seed_stopped_generation(tmp_path)
    state = program / "state"
    if problem in {"current", "previous"}:
        program.joinpath(problem).write_text("1.2.3\n")
    elif problem in {"installation", "journal", "unknown-state"}:
        name = {
            "installation": "installation.json",
            "journal": "update-journal.json",
        }.get(problem, "future-manager-state")
        state.joinpath(name).write_bytes(b"uninterpreted manager evidence")
    elif problem == "history":
        state.joinpath("history").mkdir()
    elif problem in {"receipt", "other-receipt"}:
        target = generation if problem == "receipt" else program / "versions/1.0.0"
        target.joinpath("release-receipt.json").write_bytes(
            b"malformed receipt still protects"
        )
    elif problem in {"binding", "binding-intent"}:
        name = (
            ".carlina-bootstrap-binding.json"
            if problem == "binding"
            else ".carlina-bootstrap-preparation-binding.json"
        )
        generation.joinpath(name).write_bytes(b"{}")
    elif problem == "missing-proof":
        generation.joinpath(PREPARATION_NAME).unlink()
    elif problem == "successful-proof":
        path = generation / PREPARATION_NAME
        path.write_bytes(path.read_bytes().replace(b"\n7\n", b"\n0\n"))
    elif problem in {"claim-drift", "proof-drift"}:
        path = generation / (
            ".carlina-bootstrap-claim" if problem == "claim-drift" else PREPARATION_NAME
        )
        path.write_bytes(path.read_bytes().replace(_OLD_CLAIM.encode(), b"f" * 32))
    elif problem == "archive-link":
        target = tmp_path / "foreign archive"
        target.mkdir()
        target.joinpath("marker").write_bytes(b"foreign bytes")
        state.joinpath(PREPARATION_RETIREMENT_DIRNAME).symlink_to(
            target, target_is_directory=True
        )
    elif problem == "archive-collision":
        target = state / PREPARATION_RETIREMENT_DIRNAME / f"1.2.3.{_OLD_CLAIM}"
        target.parent.mkdir(mode=0o700)
        target.mkdir()
        target.joinpath("marker").write_bytes(b"foreign bytes")
    elif problem == "lax-archive":
        target = state / PREPARATION_RETIREMENT_DIRNAME
        target.mkdir()
        target.chmod(0o755)
    elif problem == "bad-index":
        Path(environment["CARLINA_TEST_INDEX"]).write_bytes(b"changed index")
    elif problem == "bad-artifact":
        Path(environment["CARLINA_TEST_ARTIFACT"]).write_bytes(b"changed payload")
    elif problem == "state-after-download":
        curl = tmp_path / "shim/curl"
        curl.write_text(
            curl.read_text()
            + '\nprintf managed > "$CARLINA_TEST_STATE_AFTER_DOWNLOAD"\n'
        )
        environment["CARLINA_TEST_STATE_AFTER_DOWNLOAD"] = str(
            state / "future-manager-state"
        )
    elif problem == "rename-collision":
        shim = tmp_path / "shim/mv"
        shim.write_text(
            '#!/bin/sh\ncase "$*" in *--help*) exec /usr/bin/mv "$@" ;; esac\n'
            'for destination do :; done\nmkdir "$destination"\nprintf foreign > "$destination/marker"\n'
            'exec /usr/bin/mv "$@"\n'
        )
        shim.chmod(0o700)
    before = _snapshot(generation)
    command = _supervised_command(installer, program)
    if problem == "ordinary-mode":
        command.remove("--supervised-preparation")
    result = subprocess.run(
        command,
        env=environment,
        capture_output=True,
        text=True,
        timeout=40,
        check=False,
    )
    assert result.returncode != 0
    assert _snapshot(generation) == before
    if problem in {"archive-link", "archive-collision", "rename-collision"}:
        marker = (
            tmp_path / "foreign archive/marker"
            if problem == "archive-link"
            else state
            / PREPARATION_RETIREMENT_DIRNAME
            / f"1.2.3.{_OLD_CLAIM}"
            / "marker"
        )
        assert marker.read_bytes() == (
            b"foreign" if problem == "rename-collision" else b"foreign bytes"
        )
    assert (
        Path(environment["CARLINA_HOME"]).joinpath("marker").read_bytes()
        == b"synthetic private data"
    )


def test_retry_after_interruption_between_retirement_and_new_claim(
    tmp_path: Path,
) -> None:
    _require_native_scope()
    installer, program, environment, generation = _seed_stopped_generation(tmp_path)
    before = _snapshot(generation)
    original = installer.read_text()
    marker = '  create_bootstrap_generation || exit 1\nfi\nif [ "$RESUMING_BOOTSTRAP"'
    assert original.count(marker) == 1
    installer.write_text(
        original.replace(marker, '  kill -KILL $$\nfi\nif [ "$RESUMING_BOOTSTRAP"')
    )
    stopped = subprocess.run(
        _supervised_command(installer, program),
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert stopped.returncode == -signal.SIGKILL
    assert not generation.exists()
    retired = program / "state" / PREPARATION_RETIREMENT_DIRNAME / f"1.2.3.{_OLD_CLAIM}"
    assert _snapshot(retired) == before
    installer.write_text(original)
    retry = subprocess.run(
        _supervised_command(installer, program),
        env=environment,
        capture_output=True,
        text=True,
        timeout=40,
        check=False,
    )
    assert retry.returncode == 0, retry.stderr
    assert _snapshot(retired) == before
    assert program.joinpath("current").read_text().strip() == "1.2.3"


@pytest.mark.parametrize("after_move", [False, True])
def test_cancellation_respects_the_retirement_boundary(
    tmp_path: Path, after_move: bool
) -> None:
    _require_native_scope()
    installer, program, environment, generation = _seed_stopped_generation(tmp_path)
    before = _snapshot(generation)
    cancel = tmp_path / "caller cancellation"
    environment["CARLINA_TEST_RETIREMENT_CANCEL"] = str(cancel)
    if after_move:
        shim = tmp_path / "shim/mv"
        shim.write_text(
            '#!/bin/sh\ncase "$*" in *--help*) exec /usr/bin/mv "$@" ;; esac\n'
            '/usr/bin/mv "$@" || exit 1\n'
            'printf stop > "$CARLINA_TEST_RETIREMENT_CANCEL"\n'
        )
        shim.chmod(0o700)
    else:
        original = installer.read_text()
        marker = "  installation_checkpoint recovery true\n"
        assert original.count(marker) == 1
        installer.write_text(
            original.replace(
                marker, '  printf stop > "$CARLINA_TEST_RETIREMENT_CANCEL"\n' + marker
            )
        )
    result = subprocess.run(
        [*_supervised_command(installer, program), "--cancel-file", str(cancel)],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 130, result.stderr
    assert cancel.read_bytes() == b"stop"
    retired = program / "state" / PREPARATION_RETIREMENT_DIRNAME / f"1.2.3.{_OLD_CLAIM}"
    if after_move:
        assert not generation.exists()
        assert _snapshot(retired) == before
    else:
        assert _snapshot(generation) == before
        assert not retired.exists()
    assert not program.joinpath("current").exists()
    assert (
        Path(environment["CARLINA_HOME"]).joinpath("marker").read_bytes()
        == b"synthetic private data"
    )
    events = [json.loads(line) for line in result.stdout.splitlines()]
    assert events[-1]["state"] == "cancelled"


def test_real_rename_only_move_refuses_cross_filesystem_copy(tmp_path: Path) -> None:
    other = Path("/dev/shm")
    if not other.is_dir() or other.stat().st_dev == tmp_path.stat().st_dev:
        pytest.skip("Distinct writable temporary filesystem unavailable")
    source = tmp_path / "source"
    source.mkdir()
    source.joinpath("marker").write_bytes(b"retained source")
    destination = other / ("carlina-retirement-" + os.urandom(16).hex())
    result = subprocess.run(
        [
            "mv",
            "--no-copy",
            "--no-clobber",
            "--no-target-directory",
            "--",
            str(source),
            str(destination),
        ],
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode != 0
    assert source.joinpath("marker").read_bytes() == b"retained source"
    assert not destination.exists()
