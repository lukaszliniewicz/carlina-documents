"""Opt-in preparation custody keeps the existing native adoption contract."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from filelock import FileLock, Timeout
from test_installer_handoff_cleanup import (
    _EVENT_UNPACKER,
    _assert_event_outcome,
    _event_fixture,
    _native_event_command,
    _publisher_fixture_payload,
)

from carlina.core.native_preparation import (
    PREPARATION_NAME,
    parse_preparation_completion,
)
from carlina.core.native_preparation_shell import render_shell_preparation_helpers

pytestmark = [pytest.mark.fast, pytest.mark.contract]


def _require_native_scope() -> None:
    if not sys.platform.startswith("linux"):
        pytest.skip("Native Linux user-scope custody")
    result = subprocess.run(
        [
            "sh",
            "-c",
            render_shell_preparation_helpers() + "\ncheck_preparation_capability",
        ],
        capture_output=True,
        timeout=10,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip("Linux cgroup v2 and systemd user manager >=254 required")


def _supervised_command(installer: Path, program: Path) -> list[str]:
    return [
        *_native_event_command(installer, program, "linux-64"),
        "--supervised-preparation",
        "--operation-id",
        "a" * 32,
    ]


@pytest.mark.parametrize("profile", ["documents", "workbench"])
@pytest.mark.parametrize("outcome", ["success", "unpack_failure"])
def test_supervised_shell_finishes_through_normal_profile_and_manager_checks(
    tmp_path: Path, profile: str, outcome: str
) -> None:
    _require_native_scope()
    installer, program, environment = _event_fixture(
        tmp_path, profile, "linux-64", outcome
    )
    result = subprocess.run(
        _supervised_command(installer, program),
        env=environment,
        capture_output=True,
        text=True,
        timeout=40,
        check=False,
    )
    _assert_event_outcome(
        result, program, profile, outcome, before_adoption=outcome != "success"
    )
    if outcome == "success":
        generation = program / "versions/1.2.3"
        record = parse_preparation_completion(
            generation.joinpath(PREPARATION_NAME).read_bytes()
        )
        assert record.succeeded
        binding = json.loads(
            generation.joinpath(".carlina-bootstrap-binding.json").read_bytes()
        )
        intent = json.loads(
            generation.joinpath(
                ".carlina-bootstrap-preparation-binding.json"
            ).read_bytes()
        )
        assert binding == intent


@pytest.mark.parametrize(
    ("profile", "publisher"),
    [("documents", False), ("workbench", False), ("documents", True)],
)
def test_dead_supervised_carrier_resumes_completed_extraction_without_unpacking(
    tmp_path: Path, profile: str, publisher: bool
) -> None:
    _require_native_scope()
    barrier = tmp_path / "native barrier"
    prefix = b"#!/bin/sh\nset -eu\n"
    original = (
        _publisher_fixture_payload("linux-64", "success")
        if publisher
        else _EVENT_UNPACKER
    )
    assert original.startswith(prefix)
    payload = (
        prefix
        + b"""
printf started > "$CARLINA_TEST_PREPARATION_BARRIER.started"
while [ ! -f "$CARLINA_TEST_PREPARATION_BARRIER.release" ]; do sleep 0.01; done
"""
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
    environment["CARLINA_TEST_PREPARATION_BARRIER"] = str(barrier)
    calls = tmp_path / "manager-calls.jsonl"
    environment["CARLINA_TEST_MANAGER_CALLS"] = str(calls)
    generation = program / "versions/1.2.3"
    record = generation / PREPARATION_NAME
    with (
        tmp_path.joinpath("events.jsonl").open("w") as stdout,
        tmp_path.joinpath("diagnostics.log").open("w") as stderr,
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
                assert controller.poll() is None, tmp_path.joinpath(
                    "diagnostics.log"
                ).read_text()
                assert time.monotonic() < deadline
                time.sleep(0.01)
            old_scratch = tuple(program.glob(".carlina-install.*"))
            assert len(old_scratch) == 1
            claim = generation.joinpath(".carlina-bootstrap-claim").read_bytes()
            controller.kill()
            assert controller.wait(timeout=5) == -signal.SIGKILL
            assert not record.exists()
            assert not calls.exists()
            with pytest.raises(Timeout):
                FileLock(program / "state/managed-install.lock").acquire(timeout=0)
            barrier.with_suffix(".release").write_text("continue")
            deadline = time.monotonic() + 30
            while not record.exists():
                assert time.monotonic() < deadline, [
                    path.read_text() for path in old_scratch[0].glob("*stderr")
                ]
                time.sleep(0.02)
            lease = FileLock(program / "state/managed-install.lock")
            with lease.acquire(timeout=5):
                assert parse_preparation_completion(record.read_bytes()).succeeded
            assert not generation.joinpath(".carlina-bootstrap-binding.json").exists()
            assert not generation.joinpath(
                ".carlina-bootstrap-preparation-binding.json"
            ).exists()
            assert program.joinpath("current").read_text().strip() == "1.0.0"
            environment.update(
                CARLINA_TEST_REQUIRE_RESUME="1", CARLINA_TEST_FORBID_UNPACK="1"
            )
            result = subprocess.run(
                _supervised_command(installer, program),
                env=environment,
                capture_output=True,
                text=True,
                timeout=40,
                check=False,
            )
            assert result.returncode == 0, result.stderr
            events = [json.loads(line) for line in result.stdout.splitlines()]
            assert events[-1]["state"] == "succeeded"
            assert {event["application_profile"] for event in events} == {profile}
            assert "unpacking" not in {event["phase"] for event in events}
            assert json.loads(calls.read_text())["resumed"]
            assert generation.joinpath(".carlina-bootstrap-claim").read_bytes() == claim
            assert program.joinpath("current").read_text().strip() == "1.2.3"
            assert tuple(program.glob(".carlina-install.*")) == old_scratch
            assert program.joinpath("versions/1.0.0/preserved").read_bytes() == (
                b"previous generation"
            )
            assert Path(environment["CARLINA_HOME"]).joinpath(
                "marker"
            ).read_bytes() == (b"synthetic private data")
        finally:
            barrier.with_suffix(".release").write_text("continue")
            if controller.poll() is None:
                controller.kill()
            controller.wait(timeout=5)


@pytest.mark.skipif(os.name == "nt", reason="POSIX carrier option validation")
@pytest.mark.parametrize(
    ("profile", "publisher"),
    [("documents", False), ("workbench", False), ("documents", True)],
)
def test_supervision_requires_event_mode(
    tmp_path: Path, profile: str, publisher: bool
) -> None:
    installer, program, environment = _event_fixture(
        tmp_path,
        profile,
        "linux-64",
        "success",
        publisher_payload=b"#!/bin/sh\nexit 0\n" if publisher else None,
    )
    command = _supervised_command(installer, program)
    command.remove("--events-jsonl")
    command.remove("--operation-id")
    command.remove("a" * 32)
    result = subprocess.run(
        command,
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 2
    assert "requires event mode and a supported runtime payload" in result.stderr
    assert not program.joinpath("versions/1.2.3").exists()
    assert not tuple(program.glob(".carlina-install.*"))


@pytest.mark.parametrize(
    ("tail_help", "supported"),
    [
        ("--pid=PID --sleep-interval=N", True),
        ("--sleep-interval=N", False),
        ("--pid=PID", False),
    ],
)
def test_supervised_publisher_checks_tail_capabilities_before_preparation(
    tmp_path: Path, tail_help: str, supported: bool
) -> None:
    _require_native_scope()
    real_tail = shutil.which("tail")
    assert real_tail is not None
    installer, program, environment = _event_fixture(
        tmp_path,
        "documents",
        "linux-64",
        "success",
        publisher_payload=_publisher_fixture_payload("linux-64", "success"),
    )
    tail = tmp_path / "shim" / "tail"
    tail.write_text(
        "#!/bin/sh\nset -eu\n"
        'case "$1" in\n'
        f"  --help) printf '%s\\n' {shlex.quote(tail_help)} ;;\n"
        "  --version) printf '%s\\n' 'compatible tail' ;;\n"
        f'  *) exec {shlex.quote(real_tail)} "$@" ;;\n'
        "esac\n",
        encoding="utf-8",
    )
    tail.chmod(0o700)
    result = subprocess.run(
        _supervised_command(installer, program),
        env=environment,
        capture_output=True,
        text=True,
        timeout=40,
        check=False,
    )
    if supported:
        _assert_event_outcome(result, program, "documents", "success")
        assert any(
            json.loads(line)["bytes_total"] == 17 for line in result.stdout.splitlines()
        )
    else:
        assert result.returncode == 1
        assert "tail must support --pid and --sleep-interval" in result.stderr
        assert not program.joinpath("versions/1.2.3").exists()


@pytest.mark.parametrize(
    "outcome",
    ["success", "crlf", "download_failed", "verification_failed", "cancelled"],
)
def test_supervised_publisher_retains_one_event_owner_and_known_outcomes(
    tmp_path: Path, outcome: str
) -> None:
    _require_native_scope()
    payload = _publisher_fixture_payload("linux-64", outcome)
    if outcome == "crlf":
        payload = payload.replace(b"printf '%s\\n'", b"printf '%s\\r\\n'")
    installer, program, environment = _event_fixture(
        tmp_path,
        "documents",
        "linux-64",
        "success",
        publisher_payload=payload,
    )
    cancel = tmp_path / "caller cancellation request"
    forwarded = tmp_path / "forwarded"
    environment.update(
        CARLINA_TEST_CANCEL_PATH=str(cancel),
        CARLINA_TEST_CANCEL_FORWARDED=str(forwarded),
    )
    result = subprocess.run(
        [*_supervised_command(installer, program), "--cancel-file", str(cancel)],
        env=environment,
        capture_output=True,
        text=True,
        timeout=40,
        check=False,
    )
    expected = (
        "success"
        if outcome in {"success", "crlf"}
        else "cancelled"
        if outcome == "cancelled"
        else "publisher_failure"
    )
    _assert_event_outcome(
        result, program, "documents", expected, before_adoption=expected != "success"
    )
    events = [json.loads(line) for line in result.stdout.splitlines()]
    assert forwarded.read_text() == "forwarded"
    assert not cancel.exists()
    assert "carlina.publisher-bootstrap" not in result.stdout
    assert any(event["bytes_total"] == 17 for event in events)
    if outcome in {"download_failed", "verification_failed"}:
        assert events[-1]["error_code"] == outcome
        assert events[-1]["retryable"] is (outcome == "download_failed")
    if outcome == "cancelled":
        assert result.returncode == 130


@pytest.mark.parametrize(
    "outcome",
    [
        "missing_ready",
        "invalid_phase",
        "invalid_bytes",
        "leading_zero",
        "mismatched_exit",
        "mismatched_cancel",
        "duplicate_result",
        "trailing_prose",
        "missing_result",
        "forged_process_exit",
    ],
)
def test_supervised_publisher_invalid_progress_cannot_become_retry_evidence(
    tmp_path: Path, outcome: str
) -> None:
    _require_native_scope()
    payload = _publisher_fixture_payload("linux-64", outcome)
    if outcome == "forged_process_exit":
        assert payload.endswith(b"exit 0\n")
        payload = payload.removesuffix(b"exit 0\n") + (
            b"printf 'carlina.publisher-process/1 exit 0\\n'\nexit 0\n"
        )
    installer, program, environment = _event_fixture(
        tmp_path,
        "documents",
        "linux-64",
        "success",
        publisher_payload=payload,
    )
    command = _supervised_command(installer, program)
    result = subprocess.run(
        command,
        env=environment,
        capture_output=True,
        text=True,
        timeout=40,
        check=False,
    )
    assert result.returncode != 0
    events = [json.loads(line) for line in result.stdout.splitlines()]
    assert all(event["state"] == "running" for event in events)
    generation = program / "versions/1.2.3"
    claim = generation.joinpath(".carlina-bootstrap-claim").read_bytes()
    assert not generation.joinpath(PREPARATION_NAME).exists()
    assert not generation.joinpath(".carlina-bootstrap-binding.json").exists()
    old_scratch = tuple(program.glob(".carlina-install.*"))
    assert len(old_scratch) == 1
    lease = FileLock(program / "state/managed-install.lock")
    with lease.acquire(timeout=5):
        assert not generation.joinpath(PREPARATION_NAME).exists()
    retry = subprocess.run(
        command,
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert retry.returncode != 0
    assert "not ready for this installer" in retry.stderr
    assert generation.joinpath(".carlina-bootstrap-claim").read_bytes() == claim
    assert tuple(program.glob(".carlina-install.*")) == old_scratch
    assert program.joinpath("current").read_text().strip() == "1.0.0"
    assert Path(environment["CARLINA_HOME"]).joinpath("marker").read_bytes() == (
        b"synthetic private data"
    )


def test_supervised_publisher_progress_precedes_native_completion(
    tmp_path: Path,
) -> None:
    _require_native_scope()
    installer, program, environment = _event_fixture(
        tmp_path,
        "documents",
        "linux-64",
        "success",
        publisher_payload=_publisher_fixture_payload("linux-64", "success"),
    )
    barrier = tmp_path / "publisher barrier"
    environment["CARLINA_TEST_PUBLISHER_WAIT"] = str(barrier)
    output = tmp_path / "events.jsonl"
    diagnostics = tmp_path / "diagnostics.log"
    command = _supervised_command(installer, program)
    with output.open("w") as stdout, diagnostics.open("w") as stderr:
        process = subprocess.Popen(
            command, env=environment, stdout=stdout, stderr=stderr
        )
        try:
            deadline = time.monotonic() + 20
            while True:
                events = [
                    json.loads(line)
                    for line in output.read_text().splitlines(keepends=True)
                    if line.endswith("\n")
                ]
                if any(event["bytes_total"] == 17 for event in events):
                    break
                assert process.poll() is None, diagnostics.read_text()
                assert time.monotonic() < deadline, diagnostics.read_text()
                time.sleep(0.01)
            assert barrier.with_suffix(".ready").exists()
            assert process.poll() is None
            assert all(event["state"] == "running" for event in events)
            assert not program.joinpath("versions/1.2.3", PREPARATION_NAME).exists()
        finally:
            barrier.with_suffix(".release").write_text("continue")
            process.wait(timeout=40)
    _assert_event_outcome(
        subprocess.CompletedProcess(
            command, process.returncode, output.read_text(), diagnostics.read_text()
        ),
        program,
        "documents",
        "success",
    )
