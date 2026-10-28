"""Managed-runtime role and single-leader ownership contract."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import secrets
import sys
import threading
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from filelock import FileLock
from filelock import Timeout as FileLockTimeout

from carlina.product_identity import product_environment_value
from carlina.runtime_environment import is_worker_process
from iarf_assistant.core.app_paths import AppPaths

RUNTIME_ROLE_WEB = "web"
RUNTIME_ROLE_MANAGED = "managed"
RUNTIME_ROLE_WORKER = "worker"
RUNTIME_ROLE_COMMAND = "command"
RUNTIME_ROLES = frozenset(
    {
        RUNTIME_ROLE_WEB,
        RUNTIME_ROLE_MANAGED,
        RUNTIME_ROLE_WORKER,
        RUNTIME_ROLE_COMMAND,
    }
)
_ADDON_SCHEMA_MAINTENANCE_COMMANDS = frozenset(
    {"upgrade", "downgrade", "stamp", "adopt"}
)
_LOCAL_RUNTIME_LEASES: dict[str, str] = {}
_LOCAL_RUNTIME_LEASES_LOCK = threading.Lock()


class RuntimeOwnershipError(RuntimeError):
    """A managed runtime cannot acquire or prove ownership."""


@dataclass(frozen=True, slots=True)
class RuntimeRole:
    name: str

    @property
    def owns_services(self) -> bool:
        return self.name == RUNTIME_ROLE_MANAGED

    @property
    def allows_lifecycle_maintenance(self) -> bool:
        """Allow explicit, bounded lifecycle work without owning services."""

        return self.name == RUNTIME_ROLE_COMMAND

    @property
    def is_worker(self) -> bool:
        return self.name == RUNTIME_ROLE_WORKER


def lifecycle_maintenance_cli_requested(
    argv: Sequence[str] | None = None,
) -> bool:
    """Recognize only CLI commands that enter explicit lifecycle maintenance."""

    active_argv = sys.argv if argv is None else argv
    arguments = [str(value).strip().lower() for value in active_argv[1:]]
    if "init-app" in arguments:
        return True
    return any(
        arguments[index : index + 2] == ["addons", "db"]
        and arguments[index + 2] in _ADDON_SCHEMA_MAINTENANCE_COMMANDS
        for index in range(max(0, len(arguments) - 2))
    )


@dataclass(frozen=True, slots=True)
class RuntimeLeaderRecord:
    format: str
    version: int
    token: str
    pid: int
    host: str
    port: int
    started_at: str
    process_create_time: float | None = None
    runtime_role: str | None = None
    web_profile: str | None = None
    external_url: str | None = None
    trusted_hosts: tuple[str, ...] = ()
    proxy_hops: int | None = None


def resolve_runtime_role() -> RuntimeRole:
    if is_worker_process():
        return RuntimeRole(RUNTIME_ROLE_WORKER)
    value = (
        (product_environment_value("RUNTIME_ROLE", default=RUNTIME_ROLE_WEB) or "")
        .strip()
        .lower()
    )
    if value not in RUNTIME_ROLES:
        raise RuntimeOwnershipError(
            "CARLINA_RUNTIME_ROLE must be web, managed, worker, or command."
        )
    return RuntimeRole(value)


def _atomic_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def load_runtime_leader(paths) -> dict[str, object] | None:
    metadata_path = paths.state_dir / "runtime" / "leader.json"
    try:
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def runtime_leader_lock_path(paths) -> Path:
    """Return the restore-stable lock shared by runtimes and maintenance."""

    return paths.data_root.parent / f".{paths.data_root.name}.runtime-leader.lock"


def _canonical_runtime_leader_lock_path(paths: AppPaths) -> str:
    return os.path.normcase(os.path.abspath(runtime_leader_lock_path(paths)))


def _register_local_runtime_lease(paths: AppPaths, token: str) -> None:
    with _LOCAL_RUNTIME_LEASES_LOCK:
        _LOCAL_RUNTIME_LEASES[_canonical_runtime_leader_lock_path(paths)] = token


def _unregister_local_runtime_lease(paths: AppPaths, token: str) -> None:
    lock_path = _canonical_runtime_leader_lock_path(paths)
    with _LOCAL_RUNTIME_LEASES_LOCK:
        if _LOCAL_RUNTIME_LEASES.get(lock_path) == token:
            del _LOCAL_RUNTIME_LEASES[lock_path]


def _has_local_runtime_lease(paths: AppPaths, token: str) -> bool:
    lock_path = _canonical_runtime_leader_lock_path(paths)
    with _LOCAL_RUNTIME_LEASES_LOCK:
        return _LOCAL_RUNTIME_LEASES.get(lock_path) == token


def prepare_runtime_leader_lock(paths) -> Path:
    """Materialize a lockable sentinel for every supported platform.

    ``msvcrt.locking`` does not provide an effective cross-process byte-range
    lock when the target file is empty. Current ``filelock`` releases no error
    in that case, so Windows peers can both appear to acquire the lease. A
    single sentinel byte makes the same OS lock enforce ownership correctly.
    """

    lock_path = runtime_leader_lock_path(paths)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with lock_path.open("a+b") as handle:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
                os.fsync(handle.fileno())
    except PermissionError:
        # Another Windows FileLock may already hold an open handle that denies
        # a second write-open. The subsequent lock acquisition/probe remains
        # authoritative; this branch preserves compatibility with such peers.
        pass
    return lock_path


def _runtime_leader_mutex_name(paths) -> str:
    canonical = _canonical_runtime_leader_lock_path(paths)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"Local\\CarlinaRuntimeLeader-{digest}"


def _acquire_windows_mutex(paths, timeout: float):
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [
        ctypes.c_void_p,
        wintypes.BOOL,
        wintypes.LPCWSTR,
    ]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.CreateMutexW(None, False, _runtime_leader_mutex_name(paths))
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())

    wait_milliseconds = (
        0xFFFFFFFF if timeout < 0 else min(0xFFFFFFFE, max(0, int(timeout * 1000)))
    )
    result = kernel32.WaitForSingleObject(handle, wait_milliseconds)
    if result in {0x00000000, 0x00000080}:  # acquired or abandoned
        return handle

    kernel32.CloseHandle(handle)
    if result == 0x00000102:  # WAIT_TIMEOUT
        raise FileLockTimeout(str(runtime_leader_lock_path(paths)))
    if result == 0xFFFFFFFF:  # WAIT_FAILED
        raise ctypes.WinError(ctypes.get_last_error())
    raise OSError(f"Unexpected Windows mutex wait result: 0x{result:08x}")


def _release_windows_mutex(handle) -> None:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.ReleaseMutex.argtypes = [wintypes.HANDLE]
    kernel32.ReleaseMutex.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    try:
        if not kernel32.ReleaseMutex(handle):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel32.CloseHandle(handle)


class RuntimeLeaderLock:
    """Cross-process runtime lock with a Windows named-mutex authority.

    The named mutex remains reliable for console-free and Task Scheduler
    processes where ``os.kill(pid, 0)`` and byte-range lock probing are not.
    The file lock is retained alongside it for compatibility with older
    Carlina maintenance processes during an in-place upgrade.
    """

    def __init__(self, paths, *, timeout: float = -1):
        self.paths = paths
        self.timeout = float(timeout)
        self.lock_path = runtime_leader_lock_path(paths)
        self._file_lock = FileLock(str(self.lock_path), timeout=self.timeout)
        self._mutex_handle = None
        self._owner_thread_id: int | None = None
        self._lock_counter = 0

    @property
    def is_locked(self) -> bool:
        return self._lock_counter > 0

    def acquire(self, timeout: float | None = None):
        active_timeout = self.timeout if timeout is None else float(timeout)
        thread_id = threading.get_ident()
        if self._lock_counter:
            if self._owner_thread_id != thread_id:
                raise RuntimeError("Runtime leader locks are owned by one thread.")
            self._lock_counter += 1
            return self

        prepare_runtime_leader_lock(self.paths)
        mutex_handle = None
        if os.name == "nt":
            mutex_handle = _acquire_windows_mutex(self.paths, active_timeout)
        try:
            self._file_lock.acquire(timeout=active_timeout)
        except BaseException:
            if mutex_handle is not None:
                _release_windows_mutex(mutex_handle)
            raise

        self._mutex_handle = mutex_handle
        self._owner_thread_id = thread_id
        self._lock_counter = 1
        return self

    def release(self) -> None:
        if not self._lock_counter:
            return
        if self._owner_thread_id != threading.get_ident():
            raise RuntimeError(
                "Runtime leader locks must be released by their owner thread."
            )
        self._lock_counter -= 1
        if self._lock_counter:
            return

        mutex_handle = self._mutex_handle
        self._mutex_handle = None
        self._owner_thread_id = None
        try:
            self._file_lock.release()
        finally:
            if mutex_handle is not None:
                _release_windows_mutex(mutex_handle)

    def __enter__(self):
        return self.acquire()

    def __exit__(self, _exc_type, _exc, _traceback):
        self.release()


def runtime_leader_lock(paths, *, timeout: float = -1) -> RuntimeLeaderLock:
    return RuntimeLeaderLock(paths, timeout=timeout)


def process_is_running(pid: int) -> bool:
    """Return whether a PID is alive without relying on Windows console signals."""

    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except (OSError, SystemError):
            return False
        return True

    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.OpenProcess(0x00100000, False, int(pid))  # SYNCHRONIZE
    if not handle:
        return ctypes.get_last_error() == 5  # access denied still proves existence
    try:
        return kernel32.WaitForSingleObject(handle, 0) == 0x00000102
    finally:
        kernel32.CloseHandle(handle)


def _current_process_create_time() -> float | None:
    """Persist an OS creation timestamp so a stale leader PID cannot be reused."""

    return _process_create_time(os.getpid())


def _process_create_time(pid: int) -> float | None:
    """Return a process creation timestamp, or None when psutil cannot prove it."""

    try:
        import psutil
    except ImportError:  # pragma: no cover - psutil is a runtime dependency.
        return None
    try:
        process = psutil.Process(pid)
        if not process.is_running():
            return None
        return float(process.create_time())
    except (psutil.Error, OSError, OverflowError, ValueError):
        return None


def _valid_process_create_time(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        timestamp = float(value)
    except (OverflowError, ValueError):
        return None
    if not math.isfinite(timestamp) or timestamp <= 0:
        return None
    return timestamp


def _runtime_leader_lock_is_held(paths: AppPaths, token: str) -> bool:
    try:
        lock = runtime_leader_lock(paths, timeout=0)
        lock.acquire()
    except FileLockTimeout:
        return True
    except OSError:
        return False

    locally_held = _has_local_runtime_lease(paths, token)
    try:
        lock.release()
    except OSError:
        return False
    return locally_held


def verified_runtime_leader(paths: AppPaths) -> dict[str, object] | None:
    """Return a defensive leader record only when its process owns this root."""

    try:
        leader = load_runtime_leader(paths)
    except UnicodeError:
        return None
    if leader is None:
        return None

    if leader.get("format") != "carlina.runtime-leader":
        return None
    version = leader.get("version")
    if type(version) is not int or version != 1:
        return None
    pid = leader.get("pid")
    if type(pid) is not int or pid <= 0:
        return None
    token = leader.get("token")
    if not isinstance(token, str) or not token.strip():
        return None

    recorded_create_time: float | None = None
    if leader.get("process_create_time") is not None:
        recorded_create_time = _valid_process_create_time(
            leader.get("process_create_time")
        )
        if recorded_create_time is None:
            return None

    current_create_time = _process_create_time(pid)
    if current_create_time is None:
        return None
    current_create_time = _valid_process_create_time(current_create_time)
    if current_create_time is None:
        return None
    if (
        recorded_create_time is not None
        and recorded_create_time != current_create_time
    ):
        return None

    if not _runtime_leader_lock_is_held(paths, token):
        return None
    return copy.deepcopy(leader)


class RuntimeLease:
    """OS-backed, non-blocking lease held for the complete web runtime."""

    def __init__(
        self,
        paths,
        *,
        host: str,
        port: int,
        runtime_role: str | None = None,
        web_profile: str | None = None,
        external_url: str | None = None,
        trusted_hosts: tuple[str, ...] = (),
        proxy_hops: int | None = None,
    ):
        self.paths = paths
        self.host = str(host)
        self.port = int(port)
        self.runtime_role = str(runtime_role) if runtime_role is not None else None
        self.web_profile = str(web_profile) if web_profile is not None else None
        self.external_url = (
            str(external_url) if external_url is not None else None
        )
        self.trusted_hosts = tuple(str(host) for host in trusted_hosts)
        self.proxy_hops = int(proxy_hops) if proxy_hops is not None else None
        # The lock must survive an atomic restore that renames the complete
        # data root. Keeping it beside that root prevents a second process
        # from acquiring a newly-created in-root lock during the switch.
        self.lock_path = runtime_leader_lock_path(paths)
        self.metadata_path = paths.state_dir / "runtime" / "leader.json"
        self._lock = runtime_leader_lock(paths)
        self._record: RuntimeLeaderRecord | None = None

    @property
    def record(self) -> RuntimeLeaderRecord:
        if self._record is None:
            raise RuntimeOwnershipError("The managed-runtime lease is not held.")
        return self._record

    def acquire(self) -> RuntimeLeaderRecord:
        prepare_runtime_leader_lock(self.paths)
        try:
            self._lock.acquire(timeout=0)
        except FileLockTimeout as exc:
            active = load_runtime_leader(self.paths) or {}
            detail = (
                f" PID {active.get('pid')} on {active.get('host')}:{active.get('port')}"
                if active
                else ""
            )
            raise RuntimeOwnershipError(
                "Another Carlina managed runtime already owns this data root." + detail
            ) from exc
        try:
            record = RuntimeLeaderRecord(
                format="carlina.runtime-leader",
                version=1,
                token=secrets.token_urlsafe(24),
                pid=os.getpid(),
                host=self.host,
                port=self.port,
                started_at=datetime.now(UTC).isoformat(),
                process_create_time=_current_process_create_time(),
                runtime_role=self.runtime_role,
                web_profile=self.web_profile,
                external_url=self.external_url,
                trusted_hosts=self.trusted_hosts,
                proxy_hops=self.proxy_hops,
            )
            _atomic_json(self.metadata_path, asdict(record))
            self._record = record
            _register_local_runtime_lease(self.paths, record.token)
            return record
        except Exception:
            self._lock.release()
            raise

    def release(self) -> None:
        if self._record is None:
            return
        active = load_runtime_leader(self.paths)
        if active and active.get("token") == self._record.token:
            self.metadata_path.unlink(missing_ok=True)
        _unregister_local_runtime_lease(self.paths, self._record.token)
        self._record = None
        self._lock.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, _exc_type, _exc, _traceback):
        self.release()


__all__ = [
    "RUNTIME_ROLE_COMMAND",
    "RUNTIME_ROLE_MANAGED",
    "RUNTIME_ROLE_WEB",
    "RUNTIME_ROLE_WORKER",
    "RuntimeLeaderLock",
    "RuntimeLeaderRecord",
    "RuntimeLease",
    "RuntimeOwnershipError",
    "RuntimeRole",
    "lifecycle_maintenance_cli_requested",
    "load_runtime_leader",
    "prepare_runtime_leader_lock",
    "process_is_running",
    "resolve_runtime_role",
    "runtime_leader_lock",
    "runtime_leader_lock_path",
    "verified_runtime_leader",
]
