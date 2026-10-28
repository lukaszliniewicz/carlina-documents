"""Fail-closed platform isolation for authenticated no-data releases."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from packaging.version import InvalidVersion, Version

from carlina.managed_release_probe import (
    ATTESTATION_FORMAT,
    ATTESTATION_SCHEMA_VERSION,
    PROBE_BACKEND,
    PROBE_ISOLATION,
    PROBE_PROTOCOL,
)

PROBE_SCHEMA_VERSION = 1
PROBE_TARGET_ROOT = PurePosixPath("/opt/carlina-target")
PROBE_SCRATCH_ROOT = PurePosixPath("/scratch")
PROBE_TIMEOUT_SECONDS = 120
# 0.8.0 is the first upstream release that contains both nested-userns
# controls required by this policy. Runtime flag inspection and the namespace
# self-test remain authoritative because downstream builds can vary.
MINIMUM_BWRAP_VERSION = Version("0.8.0")
_SANDBOX_UID = 65534
_SANDBOX_GID = 65534
_NAMESPACE_NAMES = ("user", "mnt", "pid", "net", "ipc", "uts")
_REQUIRED_BWRAP_FLAGS = frozenset(
    {
        "--unshare-user",
        "--unshare-pid",
        "--unshare-net",
        "--unshare-ipc",
        "--unshare-uts",
        "--disable-userns",
        "--assert-userns-disabled",
        "--new-session",
        "--die-with-parent",
        "--cap-drop",
        "--clearenv",
        "--remount-ro",
    }
)
_ATTESTATION_KEYS = frozenset(
    {
        "format",
        "schema_version",
        "protocol",
        "isolation",
        "backend",
        "status",
        "target_version",
        "installed_version",
        "python_executable",
        "module_origin",
        "sys_path",
        "identity",
        "namespaces",
        "process_status",
        "environment_names",
        "file_descriptors",
        "mounts",
        "network_interfaces",
        "write_checks",
        "denied_paths",
    }
)


class ManagedProbeError(RuntimeError):
    """A no-data release could not be proven isolated."""


@dataclass(frozen=True, slots=True)
class ManagedProbeCapability:
    available: bool
    backend: str
    code: str
    detail: str
    executable: str | None = None
    version: str | None = None
    executable_sha256: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "backend": self.backend,
            "code": self.code,
            "detail": self.detail,
            "executable": self.executable,
            "version": self.version,
            "executable_sha256": self.executable_sha256,
        }


@dataclass(frozen=True, slots=True)
class ManagedProbeAttestation:
    payload: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return json.loads(json.dumps(self.payload))


def unavailable_probe_capability(code: str, detail: str) -> ManagedProbeCapability:
    return ManagedProbeCapability(
        available=False,
        backend=PROBE_BACKEND,
        code=code,
        detail=detail,
    )


def _host_tool_environment() -> dict[str, str]:
    return {
        "HOME": "/",
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": os.defpath,
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _trusted_system_executable(name: str) -> Path:
    candidate = shutil.which(name, path=os.defpath)
    if candidate is None:
        raise ManagedProbeError(f"Required system executable {name!r} is missing.")
    try:
        resolved = Path(candidate).resolve(strict=True)
        metadata = resolved.stat()
    except OSError as exc:
        raise ManagedProbeError(
            f"Required system executable {name!r} is unreadable."
        ) from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        or metadata.st_mode & (stat.S_ISUID | stat.S_ISGID)
        or not os.access(resolved, os.X_OK)
    ):
        raise ManagedProbeError(
            f"Required system executable {name!r} is not a trusted root-owned binary."
        )
    try:
        file_capabilities = os.getxattr(resolved, "security.capability")
    except OSError as exc:
        if exc.errno not in {errno.ENODATA, errno.ENOTSUP, errno.EOPNOTSUPP}:
            raise ManagedProbeError(
                f"Required system executable {name!r} capabilities are unreadable."
            ) from exc
    else:
        if file_capabilities:
            raise ManagedProbeError(
                f"Required system executable {name!r} has unsupported file capabilities."
            )
    return resolved


def _run_tool(
    command: Sequence[str],
    *,
    timeout: float = 15,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(command),
            cwd="/",
            env=_host_tool_environment(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            close_fds=True,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ManagedProbeError(
            f"System tool {Path(command[0]).name!r} failed."
        ) from exc


def _inspect_bwrap(executable: Path) -> tuple[str, frozenset[str]]:
    version_result = _run_tool((str(executable), "--version"))
    match = re.search(r"\b(\d+\.\d+\.\d+)\b", version_result.stdout)
    if version_result.returncode != 0 or match is None:
        raise ManagedProbeError("bubblewrap did not report a usable version.")
    try:
        parsed_version = Version(match.group(1))
    except InvalidVersion as exc:
        raise ManagedProbeError("bubblewrap reported an invalid version.") from exc
    if parsed_version < MINIMUM_BWRAP_VERSION:
        raise ManagedProbeError(
            f"bubblewrap {parsed_version} is older than required "
            f"{MINIMUM_BWRAP_VERSION}."
        )
    help_result = _run_tool((str(executable), "--help"))
    if help_result.returncode != 0:
        raise ManagedProbeError("bubblewrap capability inspection failed.")
    flags = frozenset(re.findall(r"(?m)^\s+(--[a-z0-9-]+)\b", help_result.stdout))
    missing = sorted(_REQUIRED_BWRAP_FLAGS - flags)
    if missing:
        raise ManagedProbeError(
            "bubblewrap lacks required controls: " + ", ".join(missing)
        )
    return str(parsed_version), flags


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _allowed_system_library_roots() -> tuple[Path, ...]:
    roots: set[Path] = set()
    for value in ("/lib", "/lib64", "/usr/lib", "/usr/lib64"):
        candidate = Path(value)
        if candidate.exists():
            try:
                resolved = candidate.resolve(strict=True)
                metadata = resolved.stat()
            except OSError as exc:
                raise ManagedProbeError("A system library root is unreadable.") from exc
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid != 0
                or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            ):
                raise ManagedProbeError(
                    "A system library root is not a trusted root-owned directory."
                )
            roots.add(resolved)
    return tuple(sorted(roots, key=str))


def _isolated_dependency_trace(
    *,
    bwrap: Path,
    executable: Path,
    target_root: Path,
) -> subprocess.CompletedProcess[str]:
    library_roots = _allowed_system_library_roots()
    target_executable = (
        PROBE_TARGET_ROOT / executable.relative_to(target_root).as_posix()
    )
    mounts = [
        (target_root, PROBE_TARGET_ROOT),
        *((root, PurePosixPath(str(root))) for root in library_roots),
    ]
    command = [
        *_sandbox_prefix(bwrap),
        *_readonly_mount_arguments(mounts),
        *_sandbox_suffix(
            {
                "LANG": "C",
                "LC_ALL": "C",
                "LD_TRACE_LOADED_OBJECTS": "1",
                "PATH": "/usr/bin",
            }
        ),
        "--",
        str(target_executable),
    ]
    return _run_tool(command, timeout=60)


def _external_dependencies(
    executable: Path,
    target_root: Path | None,
    *,
    bwrap: Path | None = None,
) -> tuple[Path, ...]:
    if target_root is None:
        # Capability discovery inspects only the trusted host's root-owned
        # `true` binary. Candidate binaries must never reach this host-side
        # ldd path because ldd is permitted to execute its input on some
        # platforms.
        ldd = _trusted_system_executable("ldd")
        result = _run_tool((str(ldd), str(executable)), timeout=60)
    else:
        if bwrap is None:
            raise ManagedProbeError(
                "Candidate dependency inspection requires the isolation backend."
            )
        result = _isolated_dependency_trace(
            bwrap=bwrap,
            executable=executable,
            target_root=target_root,
        )
    output = f"{result.stdout}\n{result.stderr}"
    if "not found" in output:
        raise ManagedProbeError("The target runtime has unresolved native libraries.")
    if result.returncode != 0:
        if "not a dynamic executable" in output.casefold():
            return ()
        raise ManagedProbeError("Native dependency inspection failed.")
    roots = _allowed_system_library_roots()
    dependencies: set[Path] = set()
    for line in output.splitlines():
        for raw in re.findall(r"(?:=>\s+)?(/[^\s()]+)(?=\s|$)", line):
            try:
                dependency = Path(raw).resolve(strict=True)
                metadata = dependency.stat()
            except OSError as exc:
                raise ManagedProbeError(
                    "A native runtime dependency is unreadable."
                ) from exc
            if target_root is not None and _is_relative_to(dependency, target_root):
                continue
            if not any(_is_relative_to(dependency, root) for root in roots):
                raise ManagedProbeError(
                    "A native runtime dependency is outside the certified system "
                    "library roots."
                )
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != 0
                or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            ):
                raise ManagedProbeError(
                    "A native runtime dependency is not a trusted root-owned file."
                )
            dependencies.add(dependency)
    return tuple(sorted(dependencies, key=str))


def _directory_arguments(destinations: Sequence[PurePosixPath]) -> list[str]:
    directories: set[PurePosixPath] = set()
    for destination in destinations:
        parent = destination.parent
        while parent != PurePosixPath("/"):
            directories.add(parent)
            parent = parent.parent
    arguments: list[str] = []
    for directory in sorted(directories, key=lambda item: (len(item.parts), str(item))):
        arguments.extend(("--dir", str(directory)))
    return arguments


def _system_alias_arguments() -> list[str]:
    arguments: list[str] = []
    for name in ("lib", "lib64"):
        candidate = Path("/") / name
        if not candidate.is_symlink():
            continue
        target = os.readlink(candidate)
        if target not in {f"usr/{name}", f"/usr/{name}"}:
            raise ManagedProbeError(f"Unsupported system /{name} alias.")
        arguments.extend(("--symlink", f"usr/{name}", f"/{name}"))
    return arguments


def _sandbox_prefix(executable: Path) -> list[str]:
    return [
        str(executable),
        "--unshare-user",
        "--unshare-pid",
        "--unshare-net",
        "--unshare-ipc",
        "--unshare-uts",
        "--disable-userns",
        "--assert-userns-disabled",
        "--new-session",
        "--die-with-parent",
        "--cap-drop",
        "ALL",
        "--hostname",
        "carlina-probe",
        "--tmpfs",
        "/",
    ]


def _readonly_mount_arguments(
    mounts: Sequence[tuple[Path, PurePosixPath]],
) -> list[str]:
    destinations = [destination for _source, destination in mounts]
    arguments = _directory_arguments(destinations)
    for source, destination in mounts:
        arguments.extend(("--ro-bind", str(source), str(destination)))
    return arguments


def _sandbox_suffix(environment: Mapping[str, str]) -> list[str]:
    arguments = [
        *_system_alias_arguments(),
        "--proc",
        "/proc",
        "--dir",
        "/dev",
        "--tmpfs",
        str(PROBE_SCRATCH_ROOT),
        "--chmod",
        "0777",
        str(PROBE_SCRATCH_ROOT),
    ]
    for name in ("home", "data", "tmp", "cache", "config", "state"):
        path = PROBE_SCRATCH_ROOT / name
        arguments.extend(("--dir", str(path), "--chmod", "0777", str(path)))
    arguments.extend(
        (
            "--symlink",
            "scratch/tmp",
            "/tmp",
            "--remount-ro",
            "/proc",
            "--remount-ro",
            "/",
            "--uid",
            str(_SANDBOX_UID),
            "--gid",
            str(_SANDBOX_GID),
            "--clearenv",
        )
    )
    for name, value in sorted(environment.items()):
        arguments.extend(("--setenv", name, value))
    return arguments


def _capability_self_test(bwrap: Path) -> None:
    true_executable = _trusted_system_executable("true")
    dependencies = _external_dependencies(true_executable, None)
    mounts = [
        (true_executable, PurePosixPath(str(true_executable))),
        *((path, PurePosixPath(str(path))) for path in dependencies),
    ]
    command = [
        *_sandbox_prefix(bwrap),
        *_readonly_mount_arguments(mounts),
        *_sandbox_suffix({"LANG": "C", "LC_ALL": "C", "PATH": "/usr/bin"}),
        "--",
        str(true_executable),
    ]
    result = _run_tool(command, timeout=30)
    if result.returncode != 0:
        raise ManagedProbeError(
            "bubblewrap could not create the required user, mount, PID, network, "
            "IPC, and UTS namespaces on this host."
        )


def detect_managed_probe_capability(platform_tag: str) -> ManagedProbeCapability:
    if not sys_platform_is_linux() or not platform_tag.startswith("linux-"):
        return unavailable_probe_capability(
            "platform_backend_unavailable",
            "The certified no-data probe currently supports Linux managed artifacts only.",
        )
    try:
        executable = _trusted_system_executable("bwrap")
        version, _flags = _inspect_bwrap(executable)
        _capability_self_test(executable)
        digest = _sha256_file(executable)
    except ManagedProbeError as exc:
        return unavailable_probe_capability("linux_bwrap_unavailable", str(exc))
    return ManagedProbeCapability(
        available=True,
        backend=PROBE_BACKEND,
        code="available",
        detail=(
            "bubblewrap passed the required namespace and mount capability self-test; "
            "the target generation must still pass its closed attestation."
        ),
        executable=str(executable),
        version=version,
        executable_sha256=digest,
    )


def sys_platform_is_linux() -> bool:
    return os.name == "posix" and platform.system() == "Linux"


def _namespace_ids() -> dict[str, str]:
    try:
        return {name: os.readlink(f"/proc/self/ns/{name}") for name in _NAMESPACE_NAMES}
    except OSError as exc:
        raise ManagedProbeError("The host namespace identity is unavailable.") from exc


def _probe_environment(
    *,
    target_version: str,
    data_root: Path,
    program_root: Path,
    program_state: Path,
) -> dict[str, str]:
    return {
        "CARLINA_HOME": "/scratch/data",
        "CARLINA_PROBE_BACKEND": PROBE_BACKEND,
        "CARLINA_PROBE_DENIED_DATA_ROOT": str(data_root),
        "CARLINA_PROBE_DENIED_PROGRAM_ROOT": str(program_root),
        "CARLINA_PROBE_DENIED_PROGRAM_STATE": str(program_state),
        "CARLINA_PROBE_EXPECTED_VERSION": target_version,
        "CARLINA_PROBE_ISOLATION": PROBE_ISOLATION,
        "CARLINA_PROBE_PROTOCOL": PROBE_PROTOCOL,
        "HOME": "/scratch/home",
        "LANG": "C",
        "LC_ALL": "C",
        "LC_CTYPE": "C",
        "PATH": "/opt/carlina-target/env/bin",
        "PWD": "/",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "0",
        "PYTHONNOUSERSITE": "1",
        "TMPDIR": "/scratch/tmp",
        "XDG_CACHE_HOME": "/scratch/cache",
        "XDG_CONFIG_HOME": "/scratch/config",
        "XDG_DATA_HOME": "/scratch/data",
        "XDG_STATE_HOME": "/scratch/state",
    }


def _build_probe_command(
    *,
    bwrap: Path,
    target_root: Path,
    python: Path,
    dependencies: Sequence[Path],
    environment: Mapping[str, str],
) -> tuple[list[str], frozenset[str]]:
    target_python = PROBE_TARGET_ROOT / python.relative_to(target_root).as_posix()
    mounts = [
        (target_root, PROBE_TARGET_ROOT),
        *((path, PurePosixPath(str(path))) for path in dependencies),
    ]
    expected_mounts = frozenset(
        {
            "/",
            "/proc",
            str(PROBE_SCRATCH_ROOT),
            *(str(destination) for _source, destination in mounts),
        }
    )
    command = [
        *_sandbox_prefix(bwrap),
        *_readonly_mount_arguments(mounts),
        *_sandbox_suffix(environment),
        "--",
        str(target_python),
        "-I",
        "-B",
        "-m",
        "carlina.managed_release_probe",
    ]
    return command, expected_mounts


def _require_mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ManagedProbeError(f"Probe attestation field {name!r} is invalid.")
    return value


def _require_string_list(value: object, name: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ManagedProbeError(f"Probe attestation field {name!r} is invalid.")
    return value


def _verify_target_path(value: object, *, name: str) -> str:
    if not isinstance(value, str):
        raise ManagedProbeError(f"Probe attestation field {name!r} is invalid.")
    path = PurePosixPath(value)
    try:
        path.relative_to(PROBE_TARGET_ROOT)
    except ValueError as exc:
        raise ManagedProbeError(
            f"Probe attestation field {name!r} escaped the target generation."
        ) from exc
    return value


def _verify_probe_payload(
    payload: object,
    *,
    expected_version: str,
    expected_environment_names: frozenset[str],
    expected_mounts: frozenset[str],
    parent_namespaces: Mapping[str, str],
) -> dict[str, Any]:
    if not isinstance(payload, dict) or frozenset(payload) != _ATTESTATION_KEYS:
        raise ManagedProbeError("Probe attestation does not match the closed schema.")
    expected_scalars = {
        "format": ATTESTATION_FORMAT,
        "schema_version": ATTESTATION_SCHEMA_VERSION,
        "protocol": PROBE_PROTOCOL,
        "isolation": PROBE_ISOLATION,
        "backend": PROBE_BACKEND,
        "status": "passed",
        "target_version": expected_version,
    }
    for name, expected in expected_scalars.items():
        if payload.get(name) != expected:
            raise ManagedProbeError(f"Probe attestation field {name!r} is invalid.")
    try:
        installed_version = Version(str(payload.get("installed_version") or ""))
        target_version = Version(expected_version)
    except InvalidVersion as exc:
        raise ManagedProbeError("Probe reported an invalid package version.") from exc
    if installed_version != target_version:
        raise ManagedProbeError(
            "Probe target package version does not match the release."
        )
    python_executable = _verify_target_path(
        payload.get("python_executable"), name="python_executable"
    )
    module_origin = _verify_target_path(
        payload.get("module_origin"), name="module_origin"
    )
    sys_path = _require_string_list(payload.get("sys_path"), "sys_path")
    if not sys_path or any(
        _verify_target_path(value, name="sys_path") != value for value in sys_path
    ):
        raise ManagedProbeError("Probe import paths are invalid.")

    identity = _require_mapping(payload.get("identity"), "identity")
    expected_identity = {
        "ppid": 1,
        "uid": _SANDBOX_UID,
        "euid": _SANDBOX_UID,
        "gid": _SANDBOX_GID,
        "egid": _SANDBOX_GID,
    }
    if not isinstance(identity.get("pid"), int) or int(identity["pid"]) <= 1:
        raise ManagedProbeError("Probe PID namespace identity is invalid.")
    for name, expected in expected_identity.items():
        if identity.get(name) != expected:
            raise ManagedProbeError(
                f"Probe process identity field {name!r} is invalid."
            )
    groups = identity.get("groups")
    if (
        not isinstance(groups, list)
        or len(groups) > 64
        or not all(group == _SANDBOX_GID for group in groups)
    ):
        raise ManagedProbeError("Probe supplementary groups are invalid.")

    namespaces = _require_mapping(payload.get("namespaces"), "namespaces")
    if set(namespaces) != set(_NAMESPACE_NAMES):
        raise ManagedProbeError("Probe namespace attestation is incomplete.")
    for name in _NAMESPACE_NAMES:
        value = namespaces.get(name)
        if not isinstance(value, str) or value == parent_namespaces.get(name):
            raise ManagedProbeError(f"Probe did not isolate the {name} namespace.")

    process_status = _require_mapping(payload.get("process_status"), "process_status")
    if process_status != {
        "NoNewPrivs": "1",
        "CapEff": "0000000000000000",
        "CapBnd": "0000000000000000",
        "CapAmb": "0000000000000000",
    }:
        raise ManagedProbeError("Probe privilege attestation is invalid.")
    if (
        frozenset(
            _require_string_list(payload.get("environment_names"), "environment_names")
        )
        != expected_environment_names
    ):
        raise ManagedProbeError("Probe environment was not reduced to the allowlist.")
    descriptors = payload.get("file_descriptors")
    if descriptors != [0, 1, 2]:
        raise ManagedProbeError("Probe inherited unexpected file descriptors.")

    mounts = payload.get("mounts")
    if not isinstance(mounts, list):
        raise ManagedProbeError("Probe mount attestation is invalid.")
    mount_by_path: dict[str, Mapping[str, Any]] = {}
    for item in mounts:
        record = _require_mapping(item, "mounts")
        if frozenset(record) != {"mount_point", "options", "filesystem"}:
            raise ManagedProbeError("Probe mount attestation is invalid.")
        mount_point = record.get("mount_point")
        options = record.get("options")
        if (
            not isinstance(mount_point, str)
            or mount_point in mount_by_path
            or not isinstance(options, list)
            or not all(isinstance(option, str) for option in options)
            or not isinstance(record.get("filesystem"), str)
        ):
            raise ManagedProbeError("Probe mount attestation is invalid.")
        mount_by_path[mount_point] = record
    if frozenset(mount_by_path) != expected_mounts:
        raise ManagedProbeError("Probe observed an unexpected filesystem mount.")
    for mount_point, record in mount_by_path.items():
        options = set(record["options"])
        expected_mode = "rw" if mount_point == str(PROBE_SCRATCH_ROOT) else "ro"
        if expected_mode not in options:
            raise ManagedProbeError(
                f"Probe mount {mount_point!r} has an unsafe access mode."
            )
    if mount_by_path["/"]["filesystem"] != "tmpfs":
        raise ManagedProbeError("Probe root is not an empty temporary filesystem.")
    if mount_by_path["/proc"]["filesystem"] != "proc":
        raise ManagedProbeError("Probe process filesystem is invalid.")
    if mount_by_path[str(PROBE_SCRATCH_ROOT)]["filesystem"] != "tmpfs":
        raise ManagedProbeError("Probe scratch space is not temporary.")

    interfaces = _require_string_list(
        payload.get("network_interfaces"), "network_interfaces"
    )
    if any(name != "lo" for name in interfaces):
        raise ManagedProbeError("Probe network namespace exposes a non-loopback route.")
    if payload.get("write_checks") != {
        "scratch_writable": True,
        "root_writable": False,
        "target_writable": False,
    }:
        raise ManagedProbeError("Probe filesystem write checks failed.")
    if payload.get("denied_paths") != {
        "live_data_root": False,
        "managed_program_root": False,
        "managed_program_state": False,
    }:
        raise ManagedProbeError("Probe could access a denied managed root.")
    return {
        "target_version": expected_version,
        "installed_version": str(installed_version),
        "python_executable": python_executable,
        "module_origin": module_origin,
        "sys_path": sys_path,
        "identity": dict(identity),
        "namespaces": dict(namespaces),
        "process_status": dict(process_status),
        "environment_names": sorted(expected_environment_names),
        "file_descriptors": [0, 1, 2],
        "mounts": [dict(mount_by_path[name]) for name in sorted(mount_by_path)],
        "network_interfaces": interfaces,
        "write_checks": dict(payload["write_checks"]),
        "denied_paths": dict(payload["denied_paths"]),
    }


def run_managed_release_probe(
    *,
    capability: ManagedProbeCapability,
    target_root: Path,
    python: Path,
    target_version: str,
    data_root: Path,
    program_root: Path,
    program_state: Path,
) -> ManagedProbeAttestation:
    if not capability.available or capability.backend != PROBE_BACKEND:
        raise ManagedProbeError("The certified managed-probe backend is unavailable.")
    if (
        not capability.executable
        or not capability.version
        or not capability.executable_sha256
    ):
        raise ManagedProbeError("The managed-probe capability record is incomplete.")
    try:
        resolved_target = target_root.resolve(strict=True)
        resolved_python = python.resolve(strict=True)
        resolved_data = data_root.resolve(strict=True)
        resolved_program = program_root.resolve(strict=True)
        resolved_state = program_state.resolve(strict=True)
    except OSError as exc:
        raise ManagedProbeError("A managed-probe path is unavailable.") from exc
    if target_root.is_symlink() or not resolved_target.is_dir():
        raise ManagedProbeError("The target generation root is unsafe.")
    if not _is_relative_to(resolved_python, resolved_target):
        raise ManagedProbeError("The target Python executable escaped its generation.")
    try:
        bwrap = Path(capability.executable).resolve(strict=True)
    except OSError as exc:
        raise ManagedProbeError(
            "The bubblewrap backend disappeared after preflight."
        ) from exc
    version, _flags = _inspect_bwrap(bwrap)
    if (
        version != capability.version
        or _sha256_file(bwrap) != capability.executable_sha256
    ):
        raise ManagedProbeError("The bubblewrap backend changed after preflight.")

    dependencies = _external_dependencies(
        resolved_python,
        resolved_target,
        bwrap=bwrap,
    )
    environment = _probe_environment(
        target_version=target_version,
        data_root=resolved_data,
        program_root=resolved_program,
        program_state=resolved_state,
    )
    command, expected_mounts = _build_probe_command(
        bwrap=bwrap,
        target_root=resolved_target,
        python=resolved_python,
        dependencies=dependencies,
        environment=environment,
    )
    parent_namespaces = _namespace_ids()
    policy = {
        "backend": PROBE_BACKEND,
        "backend_version": version,
        "environment": {
            name: (
                "<denied-host-path>"
                if name.startswith("CARLINA_PROBE_DENIED_")
                else value
            )
            for name, value in sorted(environment.items())
        },
        "mounts": sorted(expected_mounts),
        "namespace_flags": sorted(_REQUIRED_BWRAP_FLAGS),
        "uid": _SANDBOX_UID,
        "gid": _SANDBOX_GID,
    }
    policy_sha256 = hashlib.sha256(
        json.dumps(policy, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    dependency_records = [
        {"path": str(path), "sha256": _sha256_file(path)} for path in dependencies
    ]
    dependency_set_sha256 = hashlib.sha256(
        json.dumps(dependency_records, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    try:
        result = subprocess.run(
            command,
            cwd="/",
            env=_host_tool_environment(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=PROBE_TIMEOUT_SECONDS,
            check=False,
            close_fds=True,
            start_new_session=True,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ManagedProbeError("The isolated target probe did not complete.") from exc
    if result.returncode != 0:
        raise ManagedProbeError(
            f"The isolated target probe failed with exit code {result.returncode}."
        )
    if result.stderr.strip():
        raise ManagedProbeError("The isolated target probe emitted diagnostics.")
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    if len(lines) != 1 or len(lines[0].encode("utf-8")) > 1024 * 1024:
        raise ManagedProbeError("The isolated target probe emitted invalid output.")
    try:
        payload = json.loads(lines[0])
    except json.JSONDecodeError as exc:
        raise ManagedProbeError(
            "The isolated target probe emitted invalid JSON."
        ) from exc
    verified = _verify_probe_payload(
        payload,
        expected_version=target_version,
        expected_environment_names=frozenset(environment),
        expected_mounts=expected_mounts,
        parent_namespaces=parent_namespaces,
    )
    receipt = {
        "format": ATTESTATION_FORMAT,
        "schema_version": ATTESTATION_SCHEMA_VERSION,
        "status": "passed",
        "protocol": PROBE_PROTOCOL,
        "probe_schema_version": PROBE_SCHEMA_VERSION,
        "isolation": PROBE_ISOLATION,
        "backend": PROBE_BACKEND,
        "backend_version": version,
        "backend_executable_sha256": capability.executable_sha256,
        "policy_sha256": policy_sha256,
        "dependency_set_sha256": dependency_set_sha256,
        "dependency_count": len(dependency_records),
        "host": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
        },
        "verified_at": datetime.now(UTC).isoformat(),
        **verified,
    }
    return ManagedProbeAttestation(receipt)
