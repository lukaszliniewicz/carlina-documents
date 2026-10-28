from __future__ import annotations

import ctypes
import os
import re
import select
import shlex
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from carlina.core.bootstrap_ownership import (
    BOOTSTRAP_CLAIM_NAME,
    BootstrapIdentity,
    bootstrap_claim_bytes,
)
from carlina.core.native_preparation import (
    PREPARATION_NAME,
    PreparationCompletion,
    PreparationRecordError,
    encode_preparation_completion,
    match_preparation_completion,
    parse_preparation_completion,
)
from carlina.core.native_preparation_shell import (
    render_shell_preparation_custodian,
    render_shell_preparation_helpers,
)

pytestmark = [pytest.mark.fast, pytest.mark.contract]


@dataclass(frozen=True, slots=True)
class _Workspace:
    root: Path
    version: Path
    work: Path
    extract: Path
    lock: Path
    claim: Path
    record: Path
    artifact: Path
    helpers: Path
    identity: BootstrapIdentity
    nonce: str


def _identity() -> BootstrapIdentity:
    return BootstrapIdentity(
        application_profile="documents",
        release_version="1.0.0",
        channel="stable",
        index_sha256="a" * 64,
        artifact_platform="linux-64",
        artifact_kind="pixi-pack-executable",
        artifact_filename="synthetic-payload.sh",
        artifact_sha256="b" * 64,
        artifact_size=1,
    )


def _workspace(tmp_path: Path, payload: str) -> _Workspace:
    root = tmp_path / "Program Ω with space"
    root.mkdir()
    root = root.resolve(strict=True)
    version = root / "versions" / "1.0!preview"
    version.mkdir(parents=True)
    state = root / "state"
    state.mkdir()
    lock = state / "managed-install.lock"
    lock.touch()
    nonce = os.urandom(16).hex()
    identity = _identity()
    claim = version / BOOTSTRAP_CLAIM_NAME
    claim_payload = bootstrap_claim_bytes(identity, nonce)
    assert claim_payload.count(b"\n") == 3 and claim_payload.endswith(b"\n")
    claim.write_bytes(claim_payload)
    work = root / f".carlina-install.{nonce}"
    work.mkdir()
    extract = work / "extract"
    extract.mkdir()
    artifact = work / "payload.sh"
    artifact.write_text(payload, encoding="utf-8")
    artifact.chmod(0o700)
    helpers = work / "preparation-helpers.sh"
    helpers.write_text(render_shell_preparation_helpers(), encoding="utf-8")
    return _Workspace(
        root=root,
        version=version,
        work=work,
        extract=extract,
        lock=lock,
        claim=claim,
        record=version / PREPARATION_NAME,
        artifact=artifact,
        helpers=helpers,
        identity=identity,
        nonce=nonce,
    )


def _completion_bytes(
    workspace: _Workspace,
    *,
    claim_id: str | None = None,
    identity_sha256: str | None = None,
    program_root: str | None = None,
    native_exit_code: int = 0,
) -> bytes:
    return encode_preparation_completion(
        PreparationCompletion(
            claim_id=claim_id or workspace.nonce,
            identity_sha256=identity_sha256 or workspace.identity.digest,
            program_root=program_root or str(workspace.root),
            native_exit_code=native_exit_code,
        )
    )


def _shell_read_completion(
    workspace: _Workspace,
    *,
    program_root: Path | None = None,
    version: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    script = "\n".join(
        (
            f"PP_ROOT={shlex.quote(str(program_root or workspace.root))}",
            f"PP_VERSION={shlex.quote(str(version or workspace.version))}",
            f"PP_NONCE={shlex.quote(workspace.nonce)}",
            f"PP_IDENTITY={shlex.quote(workspace.identity.digest)}",
            f". {shlex.quote(str(workspace.helpers))}",
            'read_preparation_completion "$PP_ROOT" "$PP_VERSION" "$PP_NONCE" "$PP_IDENTITY"',
            "",
        )
    )
    return subprocess.run(
        ["sh", "-c", script],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
        env={**os.environ, "LC_ALL": "C"},
    )


def _snapshot(root: Path) -> tuple[tuple[str, str, object], ...]:
    entries: list[tuple[str, str, object]] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            entries.append((relative, "symlink", os.readlink(path)))
        elif path.is_file():
            stat = path.stat()
            entries.append(
                (
                    relative,
                    "file",
                    (
                        stat.st_ino,
                        stat.st_mode,
                        stat.st_size,
                        stat.st_mtime_ns,
                        path.read_bytes(),
                    ),
                )
            )
        elif path.is_dir():
            stat = path.stat()
            entries.append(
                (relative, "directory", (stat.st_ino, stat.st_mode, stat.st_mtime_ns))
            )
        else:
            entries.append((relative, "other", None))
    return tuple(entries)


def _shell_skip_reason() -> str | None:
    if not sys.platform.startswith("linux"):
        return "Rendered shell preparation helpers require Linux shell tools"
    required = (
        "sh",
        "sha256sum",
        "stat",
        "flock",
        "base64",
        "wc",
        "tr",
        "cut",
        "cat",
        "dirname",
    )
    missing = [command for command in required if shutil.which(command) is None]
    if missing:
        return f"Required shell tools are unavailable: {', '.join(missing)}"
    return None


def _capability_command(*command: str) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except OSError, subprocess.TimeoutExpired:
        return None


def _systemd_skip_reason() -> str | None:
    shell_reason = _shell_skip_reason()
    if shell_reason is not None:
        return shell_reason
    missing = [
        command
        for command in (
            "systemd-run",
            "systemctl",
            "bash",
            "uname",
            "head",
            "sync",
            "ln",
            "awk",
            "sleep",
        )
        if shutil.which(command) is None
    ]
    if missing:
        return f"Required systemd tools are unavailable: {', '.join(missing)}"
    cgroup_controllers = Path("/sys/fs/cgroup/cgroup.controllers")
    if not cgroup_controllers.is_file() or not Path("/proc/self/cgroup").is_file():
        return "Linux cgroup v2 is unavailable"
    filesystem = _capability_command("stat", "-fc", "%T", "/sys/fs/cgroup")
    if (
        filesystem is None
        or filesystem.returncode != 0
        or filesystem.stdout.strip() != "cgroup2fs"
    ):
        return "The cgroup filesystem is not cgroup v2"

    cli = _capability_command("systemd-run", "--version")
    if cli is None:
        return "systemd-run 254 or newer is unavailable"
    cli_match = re.match(r"systemd\s+(\d+)(?:\s|$)", cli.stdout)
    if cli.returncode != 0 or cli_match is None or int(cli_match.group(1)) < 254:
        return "systemd-run 254 or newer is unavailable"

    manager = _capability_command(
        "systemctl", "--user", "show", "--property=Version", "--value"
    )
    if manager is None:
        return "A systemd user manager version 254 or newer is unavailable"
    manager_match = re.match(r"(\d+)(?:[.\-]|$)", manager.stdout.strip())
    if (
        manager.returncode != 0
        or manager_match is None
        or int(manager_match.group(1)) < 254
    ):
        return "A systemd user manager version 254 or newer is unavailable"

    try:
        libc = ctypes.CDLL(None, use_errno=True)
        open_process = libc.pidfd_open
        signal_process = libc.pidfd_send_signal
    except AttributeError, OSError:
        return "libc pidfd process handles are unavailable"
    open_process.argtypes = (ctypes.c_int, ctypes.c_uint)
    open_process.restype = ctypes.c_int
    signal_process.argtypes = (
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_uint,
    )
    signal_process.restype = ctypes.c_int
    descriptor = open_process(os.getpid(), 0)
    if descriptor < 0:
        return "Kernel pidfd process handles are unavailable"
    os.close(descriptor)
    return None


_SHELL_SKIP_REASON = _shell_skip_reason()
_SYSTEMD_SKIP_REASON = _systemd_skip_reason()


@pytest.mark.skipif(_SHELL_SKIP_REASON is not None, reason=_SHELL_SKIP_REASON or "")
def test_rendered_custodian_is_embedded_in_shell_helpers() -> None:
    custodian = render_shell_preparation_custodian()

    assert custodian.startswith("#!/bin/sh\nset -eu\n")
    assert custodian in render_shell_preparation_helpers()


@pytest.mark.parametrize("native_exit_code", [0, 7, 4_294_967_295])
@pytest.mark.skipif(_SHELL_SKIP_REASON is not None, reason=_SHELL_SKIP_REASON or "")
def test_shell_completion_reader_accepts_exact_canonical_status(
    tmp_path: Path, native_exit_code: int
) -> None:
    workspace = _workspace(tmp_path, "#!/bin/sh\nexit 0\n")
    payload = _completion_bytes(workspace, native_exit_code=native_exit_code)
    workspace.record.write_bytes(payload)

    shell_result = _shell_read_completion(workspace)
    parsed = match_preparation_completion(
        payload,
        claim_id=workspace.nonce,
        identity_sha256=workspace.identity.digest,
        program_root=str(workspace.root),
    )

    assert shell_result.returncode == 0, shell_result.stderr
    assert shell_result.stdout == f"{native_exit_code}\n"
    assert parse_preparation_completion(payload) == parsed
    assert parsed.native_exit_code == native_exit_code


@pytest.mark.parametrize(
    "case",
    [
        "crlf",
        "trailing",
        "bad-nonce",
        "identity-mismatch",
        "root-mismatch",
        "base64",
        "overflow",
        "noncanonical-exit",
        "missing-newline",
    ],
)
@pytest.mark.skipif(_SHELL_SKIP_REASON is not None, reason=_SHELL_SKIP_REASON or "")
def test_shell_completion_reader_rejects_malformed_records_without_mutation(
    tmp_path: Path, case: str
) -> None:
    workspace = _workspace(tmp_path, "#!/bin/sh\nexit 0\n")
    expected = _completion_bytes(workspace)
    if case == "crlf":
        payload = expected.replace(b"\n", b"\r\n")
    elif case == "trailing":
        payload = expected + b"trailing\n"
    elif case == "bad-nonce":
        payload = _completion_bytes(workspace, claim_id="f" * 32)
    elif case == "identity-mismatch":
        payload = _completion_bytes(workspace, identity_sha256="f" * 64)
    elif case == "root-mismatch":
        payload = _completion_bytes(
            workspace, program_root=str(workspace.root.parent / "foreign program")
        )
    elif case == "base64":
        fields = expected.split(b"\n")
        fields[3] = b"%%%"
        payload = b"\n".join(fields)
    elif case == "overflow":
        fields = expected.split(b"\n")
        fields[4] = b"4294967296"
        payload = b"\n".join(fields)
    elif case == "noncanonical-exit":
        fields = expected.split(b"\n")
        fields[4] = b"00"
        payload = b"\n".join(fields)
    else:
        assert case == "missing-newline"
        payload = expected[:-1]
    workspace.record.write_bytes(payload)
    before = _snapshot(tmp_path)

    shell_result = _shell_read_completion(workspace)

    assert shell_result.returncode != 0
    assert _snapshot(tmp_path) == before
    with pytest.raises(PreparationRecordError):
        match_preparation_completion(
            payload,
            claim_id=workspace.nonce,
            identity_sha256=workspace.identity.digest,
            program_root=str(workspace.root),
        )


@pytest.mark.skipif(_SHELL_SKIP_REASON is not None, reason=_SHELL_SKIP_REASON or "")
def test_shell_completion_reader_rejects_symlink_record_without_mutation(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path, "#!/bin/sh\nexit 0\n")
    payload = _completion_bytes(workspace)
    workspace.record.write_bytes(payload)
    target = tmp_path / "completion target"
    workspace.record.rename(target)
    workspace.record.symlink_to(target)
    before = _snapshot(tmp_path)

    shell_result = _shell_read_completion(workspace)

    assert shell_result.returncode != 0
    assert _snapshot(tmp_path) == before
    assert target.read_bytes() == payload


def _controller_source(workspace: _Workspace, *, check_capability: bool) -> str:
    capability = "check_preparation_capability || exit 90\n" if check_capability else ""
    return f"""#!/bin/sh
set -u
PROGRAM_ROOT={shlex.quote(str(workspace.root))}
VERSION_DIR={shlex.quote(str(workspace.version))}
WORK={shlex.quote(str(workspace.work))}
EXTRACT_TMP={shlex.quote(str(workspace.extract))}
ARTIFACT_PATH={shlex.quote(str(workspace.artifact))}
BOOTSTRAP_CLAIM_ID={shlex.quote(workspace.nonce)}
BOOTSTRAP_IDENTITY={shlex.quote(workspace.identity.digest)}
exec 9>"$PROGRAM_ROOT/state/managed-install.lock"
flock -x 9
. {shlex.quote(str(workspace.helpers))}
{capability}PREPARATION_STATUS=0
if run_supervised_preparation; then
  PREPARATION_STATUS=0
else
  PREPARATION_STATUS=$?
fi
printf '%s\\n%s\\n%s\\n' "$PREPARATION_KNOWN" "${{PREPARATION_EXIT:-unknown}}" "${{PREPARATION_HOST_EXIT:-unknown}}" > "$WORK/controller-result"
exit "$PREPARATION_STATUS"
"""


def _start_controller(
    workspace: _Workspace, *, check_capability: bool
) -> subprocess.Popen[bytes]:
    controller_script = workspace.work / "controller.sh"
    controller_script.write_text(
        _controller_source(workspace, check_capability=check_capability),
        encoding="utf-8",
    )
    stdout_path = workspace.work / "controller.stdout"
    stderr_path = workspace.work / "controller.stderr"
    with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
        return subprocess.Popen(
            ["sh", str(controller_script)],
            cwd=workspace.work,
            env={**os.environ, "LC_ALL": "C"},
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            close_fds=True,
            start_new_session=True,
        )


def _diagnostics(workspace: _Workspace) -> str:
    paths = (
        workspace.work / "controller.stderr",
        workspace.work / "preparation-custodian.stderr",
        workspace.work / "preparation-native.stderr",
    )
    return "\n".join(
        f"{path.name}: {path.read_text(encoding='utf-8', errors='replace')}"
        for path in paths
        if path.exists()
    )


def _result(workspace: _Workspace) -> tuple[str, str, str]:
    values = (
        (workspace.work / "controller-result").read_text(encoding="ascii").splitlines()
    )
    assert len(values) == 3
    return values[0], values[1], values[2]


def _wait_for(
    predicate: Callable[[], bool | int | str | None],
    *,
    description: str,
    timeout: float = 30,
) -> bool | int | str:
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() >= deadline:
            raise AssertionError(f"Timed out waiting for {description}")
        time.sleep(0.01)


def _load_pidfd_api() -> tuple[ctypes.CDLL, Any, Any]:
    libc = ctypes.CDLL(None, use_errno=True)
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
    return libc, open_process, signal_process


def _open_pidfd(pid: int) -> int:
    libc, open_process, _signal_process = _load_pidfd_api()
    descriptor = open_process(pid, 0)
    if descriptor < 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, "Could not open pidfd for test-owned process")
    del libc
    return descriptor


def _send_pidfd_signal(descriptor: int, signal_number: int) -> None:
    _libc, _open_process, signal_process = _load_pidfd_api()
    if signal_process(descriptor, signal_number, None, 0) != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, "Could not signal test-owned pidfd")


def _pidfd_ready(descriptor: int, timeout: float = 0) -> bool:
    poller = select.poll()
    poller.register(descriptor, select.POLLIN)
    return bool(poller.poll(max(0, int(timeout * 1000))))


def _wait_pidfd(descriptor: int, timeout: float = 30) -> bool:
    return _pidfd_ready(descriptor, timeout)


def _wait_started(workspace: _Workspace, controller: subprocess.Popen[bytes]) -> int:
    started = workspace.work / "writer.started"
    pid_path = workspace.work / "writer.pid"

    def writer_started() -> bool:
        assert controller.poll() is None, _diagnostics(workspace)
        return started.is_file() and pid_path.is_file()

    _wait_for(writer_started, description="writer barrier")
    return int(pid_path.read_text(encoding="ascii"))


def _writer_artifact(workspace: _Workspace) -> None:
    writer = workspace.work / "writer.py"
    writer.write_text(
        """from __future__ import annotations
import os
import sys
import time
from pathlib import Path

started, pid_path, release, output = map(Path, sys.argv[1:])
if os.getsid(0) != os.getpid():
    raise SystemExit(91)
try:
    os.fstat(9)
except OSError:
    pass
else:
    raise SystemExit(92)
with pid_path.open("x", encoding="ascii") as target:
    target.write(str(os.getpid()))
with output.open("ab") as stream:
    stream.write(b"started\\n")
    stream.flush()
    started.write_bytes(b"ready")
    while not release.exists():
        stream.write(b"writer-live\\n")
        stream.flush()
        time.sleep(0.005)
""",
        encoding="utf-8",
    )
    launcher = workspace.work / "launch-writer.py"
    launcher.write_text(
        """from __future__ import annotations
import subprocess
import sys

subprocess.Popen(
    [sys.executable, sys.argv[1], *sys.argv[2:]],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    close_fds=True,
    start_new_session=True,
)
""",
        encoding="utf-8",
    )
    artifact = (
        "#!/bin/sh\n"
        "set -eu\n"
        "[ ! -e /proc/$$/fd/9 ] || exit 92\n"
        f"exec {shlex.quote(sys.executable)} {shlex.quote(str(launcher))} "
        f"{shlex.quote(str(writer))} "
        f"{shlex.quote(str(workspace.work / 'writer.started'))} "
        f"{shlex.quote(str(workspace.work / 'writer.pid'))} "
        f"{shlex.quote(str(workspace.work / 'writer.release'))} "
        f"{shlex.quote(str(workspace.work / 'writer.output'))}\n"
    )
    workspace.artifact.write_text(artifact, encoding="utf-8")
    workspace.artifact.chmod(0o700)


def _release_writer(workspace: _Workspace) -> None:
    release = workspace.work / "writer.release"
    if not release.exists():
        descriptor = os.open(
            release,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        os.close(descriptor)


def _lock_is_available(lock_path: Path) -> bool:
    import fcntl

    descriptor = os.open(lock_path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return True
    finally:
        os.close(descriptor)


def _proc_starttime(pid: int) -> str:
    stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    closing = stat.rfind(")")
    fields = stat[closing + 1 :].split()
    assert len(fields) > 19
    return fields[19]


def _proc_arguments(pid: int) -> tuple[bytes, ...]:
    content = Path(f"/proc/{pid}/cmdline").read_bytes()
    return tuple(argument for argument in content.split(b"\0") if argument)


def _custodian_pidfd(controller: subprocess.Popen[bytes], workspace: _Workspace) -> int:
    assert controller.poll() is None
    controller_children = (
        Path("/proc") / str(controller.pid) / "task" / str(controller.pid) / "children"
    )
    assert controller_children.resolve(strict=True) == controller_children
    custodian_script = workspace.work / "preparation-custodian.sh"
    assert custodian_script.parent.resolve(strict=True) == workspace.work
    assert custodian_script.is_file() and not custodian_script.is_symlink()
    rendered_custodian = render_shell_preparation_custodian()
    assert custodian_script.read_text(encoding="utf-8") in {
        rendered_custodian,
        rendered_custodian + "\n",
    }
    expected_argument = os.fsencode(str(custodian_script))
    child_pids = [int(value) for value in controller_children.read_text().split()]
    matches: list[int] = []
    for child_pid in child_pids:
        try:
            before_start = _proc_starttime(child_pid)
            arguments = _proc_arguments(child_pid)
        except FileNotFoundError:
            continue
        if expected_argument not in arguments:
            continue
        descriptor = _open_pidfd(child_pid)
        try:
            if _proc_starttime(
                child_pid
            ) == before_start and expected_argument in _proc_arguments(child_pid):
                matches.append(descriptor)
                descriptor = -1
        except FileNotFoundError:
            pass
        finally:
            if descriptor >= 0:
                os.close(descriptor)
    assert len(matches) == 1, "could not identify exactly one own custodian helper"
    return matches[0]


def _cgroup_path(workspace: _Workspace) -> Path:
    group_file = workspace.work / "preparation-group"
    fields = group_file.read_text(encoding="utf-8").splitlines()
    assert len(fields) == 3
    assert fields[0] == "carlina.preparation-group/1"
    assert fields[1] == workspace.nonce
    group = fields[2]
    assert group.startswith("/") and "/../" not in group and "//" not in group
    assert group.endswith(f"/carlina-preparation-{workspace.nonce}.scope")
    return Path("/sys/fs/cgroup") / group.lstrip("/")


def _cgroup_populated(workspace: _Workspace) -> str | None:
    path = _cgroup_path(workspace) / "cgroup.events"
    try:
        content = path.read_text(encoding="ascii")
    except FileNotFoundError:
        return None
    for line in content.splitlines():
        key, value = line.split()
        if key == "populated":
            return value
    raise AssertionError("cgroup.events omitted populated state")


def _cleanup_writer_scenario(
    workspace: _Workspace,
    controller: subprocess.Popen[bytes],
    *,
    writer_pidfd: int,
    custodian_pidfd: int = -1,
) -> None:
    _release_writer(workspace)
    if writer_pidfd >= 0 and not _wait_pidfd(writer_pidfd, 30):
        _send_pidfd_signal(writer_pidfd, signal.SIGKILL)
        assert _wait_pidfd(writer_pidfd, 5), "writer did not end after pidfd cleanup"

    try:
        if controller.poll() is None:
            try:
                controller.wait(timeout=30)
            except subprocess.TimeoutExpired:
                if custodian_pidfd < 0:
                    try:
                        custodian_pidfd = _custodian_pidfd(controller, workspace)
                    except AssertionError, FileNotFoundError, ProcessLookupError:
                        custodian_pidfd = -1
                if custodian_pidfd >= 0 and not _pidfd_ready(custodian_pidfd):
                    _send_pidfd_signal(custodian_pidfd, signal.SIGKILL)
                    assert _wait_pidfd(custodian_pidfd, 5)
                if controller.poll() is None:
                    controller.kill()
                    controller.wait(timeout=5)
        if (workspace.work / "preparation-group").exists():
            _wait_for(
                lambda: _cgroup_populated(workspace) in {None, "0"},
                description="native systemd scope cleanup",
            )
    finally:
        if writer_pidfd >= 0:
            os.close(writer_pidfd)
        if custodian_pidfd >= 0:
            try:
                assert _wait_pidfd(custodian_pidfd, 5), (
                    "custodian cleanup did not finish"
                )
            finally:
                os.close(custodian_pidfd)


@pytest.mark.skipif(
    _SHELL_SKIP_REASON is not None or _SYSTEMD_SKIP_REASON is not None,
    reason=_SYSTEMD_SKIP_REASON
    or _SHELL_SKIP_REASON
    or "native preparation unavailable",
)
@pytest.mark.parametrize(
    ("native_exit_code", "controller_status", "known"),
    [(0, 0, "1"), (7, 1, "1")],
)
def test_native_custodian_records_success_and_failure(
    tmp_path: Path,
    native_exit_code: int,
    controller_status: int,
    known: str,
) -> None:
    workspace = _workspace(tmp_path, f"#!/bin/sh\nset -eu\nexit {native_exit_code}\n")
    controller = _start_controller(workspace, check_capability=True)
    try:
        assert controller.wait(timeout=30) == controller_status, _diagnostics(workspace)
        claim, completion, host_exit = _result(workspace)
        assert claim == known
        assert completion == str(native_exit_code)
        assert host_exit == ("0" if native_exit_code == 0 else "1")
        parsed = match_preparation_completion(
            workspace.record.read_bytes(),
            claim_id=workspace.nonce,
            identity_sha256=workspace.identity.digest,
            program_root=str(workspace.root),
        )
        assert parsed.native_exit_code == native_exit_code
        assert _lock_is_available(workspace.lock)
    finally:
        if controller.poll() is None:
            controller.kill()
            controller.wait(timeout=5)


def _start_writer_scenario(
    tmp_path: Path,
) -> tuple[_Workspace, subprocess.Popen[bytes], int]:
    workspace = _workspace(tmp_path, "#!/bin/sh\nset -eu\nexit 0\n")
    _writer_artifact(workspace)
    controller = _start_controller(workspace, check_capability=True)
    writer_pidfd = -1
    try:
        writer_pid = _wait_started(workspace, controller)
        writer_pidfd = _open_pidfd(writer_pid)
        assert not _pidfd_ready(writer_pidfd)
        assert _cgroup_populated(workspace) == "1"
        return workspace, controller, writer_pidfd
    except BaseException:
        _cleanup_writer_scenario(workspace, controller, writer_pidfd=writer_pidfd)
        raise


def _wait_for_output_growth(workspace: _Workspace) -> int:
    output = workspace.work / "writer.output"
    initial = output.stat().st_size
    return int(
        _wait_for(
            lambda: output.stat().st_size if output.stat().st_size > initial else 0,
            description="writer output growth",
        )
    )


@pytest.mark.skipif(
    _SHELL_SKIP_REASON is not None or _SYSTEMD_SKIP_REASON is not None,
    reason=_SYSTEMD_SKIP_REASON
    or _SHELL_SKIP_REASON
    or "native preparation unavailable",
)
def test_controller_death_preserves_custody_until_descendant_finishes(
    tmp_path: Path,
) -> None:
    workspace, controller, writer_pidfd = _start_writer_scenario(tmp_path)
    custodian_pidfd = -1
    try:
        assert controller.poll() is None
        custodian_pidfd = _custodian_pidfd(controller, workspace)
        controller.kill()
        assert controller.wait(timeout=30) == -signal.SIGKILL
        assert not workspace.record.exists()
        assert not _lock_is_available(workspace.lock)
        held_size = (workspace.work / "writer.output").stat().st_size
        assert _wait_for_output_growth(workspace) > held_size
        _release_writer(workspace)
        assert _wait_pidfd(writer_pidfd, 30), (
            "detached writer did not exit after release"
        )
        assert _wait_pidfd(custodian_pidfd, 30), (
            "custodian did not finish after handoff"
        )
        completion = match_preparation_completion(
            workspace.record.read_bytes(),
            claim_id=workspace.nonce,
            identity_sha256=workspace.identity.digest,
            program_root=str(workspace.root),
        )
        assert completion.native_exit_code == 0
        assert _lock_is_available(workspace.lock)
        assert _wait_for(
            lambda: _cgroup_populated(workspace) in {None, "0"},
            description="native systemd scope to empty",
        )
    finally:
        _cleanup_writer_scenario(
            workspace,
            controller,
            writer_pidfd=writer_pidfd,
            custodian_pidfd=custodian_pidfd,
        )


@pytest.mark.skipif(
    _SHELL_SKIP_REASON is not None or _SYSTEMD_SKIP_REASON is not None,
    reason=_SYSTEMD_SKIP_REASON
    or _SHELL_SKIP_REASON
    or "native preparation unavailable",
)
def test_custodian_death_leaves_unknown_completion_while_writer_survives(
    tmp_path: Path,
) -> None:
    workspace, controller, writer_pidfd = _start_writer_scenario(tmp_path)
    custodian_pidfd = -1
    try:
        assert controller.poll() is None
        custodian_pidfd = _custodian_pidfd(controller, workspace)
        assert not _pidfd_ready(custodian_pidfd)
        _send_pidfd_signal(custodian_pidfd, signal.SIGKILL)
        assert _wait_pidfd(custodian_pidfd, 30), (
            "own custodian did not exit after pidfd signal"
        )
        assert controller.wait(timeout=30) != 0, _diagnostics(workspace)
        assert _result(workspace) == ("0", "unknown", "137")
        assert not workspace.record.exists()
        held_size = (workspace.work / "writer.output").stat().st_size
        assert _wait_for_output_growth(workspace) > held_size
        assert not workspace.record.exists()
        _release_writer(workspace)
        assert _wait_pidfd(writer_pidfd, 30), (
            "detached writer did not exit after release"
        )
        assert not workspace.record.exists()
        assert _wait_for(
            lambda: _cgroup_populated(workspace) in {None, "0"},
            description="native systemd scope to empty after custodian death",
        )
    finally:
        _cleanup_writer_scenario(
            workspace,
            controller,
            writer_pidfd=writer_pidfd,
            custodian_pidfd=custodian_pidfd,
        )


@pytest.mark.skipif(
    _SHELL_SKIP_REASON is not None or _SYSTEMD_SKIP_REASON is not None,
    reason=_SYSTEMD_SKIP_REASON
    or _SHELL_SKIP_REASON
    or "native preparation unavailable",
)
def test_claim_change_after_native_start_prevents_completion(
    tmp_path: Path,
) -> None:
    workspace, controller, writer_pidfd = _start_writer_scenario(tmp_path)
    changed_claim = bootstrap_claim_bytes(workspace.identity, "f" * 32)
    try:
        workspace.claim.write_bytes(changed_claim)
        assert workspace.claim.read_bytes() == changed_claim
        _release_writer(workspace)
        assert _wait_pidfd(writer_pidfd, 30), (
            "detached writer did not exit after release"
        )
        assert controller.wait(timeout=30) != 0, _diagnostics(workspace)
        assert _result(workspace)[0] == "0"
        assert not workspace.record.exists()
        assert not tuple(workspace.version.glob(f"{PREPARATION_NAME}.*.tmp"))
        assert workspace.claim.read_bytes() == changed_claim
        assert _lock_is_available(workspace.lock)
    finally:
        _cleanup_writer_scenario(workspace, controller, writer_pidfd=writer_pidfd)


@pytest.mark.skipif(_SHELL_SKIP_REASON is not None, reason=_SHELL_SKIP_REASON or "")
@pytest.mark.parametrize("unsafe_path", ["preexisting-record", "extract-symlink"])
def test_custodian_refuses_preexisting_unsafe_paths_without_mutation(
    tmp_path: Path, unsafe_path: str
) -> None:
    workspace = _workspace(tmp_path, "#!/bin/sh\nset -eu\nexit 0\n")
    target: Path | None = None
    preserved: bytes | None = None
    if unsafe_path == "preexisting-record":
        preserved = _completion_bytes(workspace)
        workspace.record.write_bytes(preserved)
    elif unsafe_path == "extract-symlink":
        target = tmp_path / "extract target"
        target.mkdir()
        (target / "marker").write_bytes(b"synthetic extraction data")
        preserved = (target / "marker").read_bytes()
        workspace.extract.rmdir()
        workspace.extract.symlink_to(target, target_is_directory=True)
    else:
        raise AssertionError(f"Unexpected unsafe path case: {unsafe_path}")

    controller = _start_controller(workspace, check_capability=False)
    try:
        assert controller.wait(timeout=30) != 0, _diagnostics(workspace)
        assert _result(workspace)[0] == "0"
        assert _lock_is_available(workspace.lock)
        if unsafe_path == "preexisting-record":
            assert workspace.record.read_bytes() == preserved
        elif unsafe_path == "extract-symlink":
            assert target is not None and (target / "marker").read_bytes() == preserved
            assert workspace.extract.is_symlink()
        if unsafe_path != "preexisting-record":
            assert not workspace.record.exists()
            assert not tuple(workspace.version.glob(f"{PREPARATION_NAME}.*.tmp"))
    finally:
        if controller.poll() is None:
            controller.kill()
            controller.wait(timeout=5)
