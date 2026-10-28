"""Fixed target-side report for a managed no-data release probe.

This module deliberately uses only the Python standard library and does not
import the application.  The host executes it from the candidate immutable
generation inside a platform sandbox and independently validates every field.
"""

from __future__ import annotations

import importlib.metadata
import json
import os
import sys
from typing import Any

ATTESTATION_FORMAT = "carlina.managed-release-probe-attestation"
ATTESTATION_SCHEMA_VERSION = 1
PROBE_PROTOCOL = "carlina.managed-release-probe"
PROBE_ISOLATION = "host_filesystem_and_network_denied_v1"
PROBE_BACKEND = "linux-bwrap-v1"
_NAMESPACE_NAMES = ("user", "mnt", "pid", "net", "ipc", "uts")
_STATUS_NAMES = ("NoNewPrivs", "CapEff", "CapBnd", "CapAmb")


def _decode_mount_field(value: str) -> str:
    for encoded, decoded in (
        ("\\040", " "),
        ("\\011", "\t"),
        ("\\012", "\n"),
        ("\\134", "\\"),
    ):
        value = value.replace(encoded, decoded)
    return value


def _mounts() -> list[dict[str, Any]]:
    mounts: list[dict[str, Any]] = []
    with open("/proc/self/mountinfo", encoding="utf-8") as handle:
        for line in handle:
            before, separator, after = line.rstrip("\n").partition(" - ")
            fields = before.split()
            filesystem = after.split()
            if not separator or len(fields) < 6 or not filesystem:
                raise RuntimeError("invalid mountinfo record")
            mounts.append(
                {
                    "mount_point": _decode_mount_field(fields[4]),
                    "options": sorted(fields[5].split(",")),
                    "filesystem": filesystem[0],
                }
            )
    return sorted(mounts, key=lambda item: str(item["mount_point"]))


def _process_status() -> dict[str, str]:
    values: dict[str, str] = {}
    with open("/proc/self/status", encoding="utf-8") as handle:
        for line in handle:
            name, separator, value = line.partition(":")
            if separator and name in _STATUS_NAMES:
                values[name] = value.strip()
    return values


def _file_descriptors() -> list[int]:
    descriptors: list[int] = []
    for name in os.listdir("/proc/self/fd"):
        try:
            descriptor = int(name)
            # The descriptor used by listdir may already have disappeared.
            os.readlink(f"/proc/self/fd/{descriptor}")
        except FileNotFoundError, OSError, ValueError:
            continue
        descriptors.append(descriptor)
    return sorted(descriptors)


def _network_interfaces() -> list[str]:
    interfaces: set[str] = set()
    with open("/proc/net/route", encoding="utf-8") as handle:
        for index, line in enumerate(handle):
            fields = line.split()
            if index and fields:
                interfaces.add(fields[0])
    with open("/proc/net/ipv6_route", encoding="utf-8") as handle:
        for line in handle:
            fields = line.split()
            if fields:
                interfaces.add(fields[-1])
    return sorted(interfaces)


def _write_succeeds(path: str) -> bool:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags, 0o600)
        os.write(descriptor, b"probe")
        return True
    except OSError:
        return False
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(path)
        except OSError:
            pass


def _path_is_accessible(path: str) -> bool:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return False
    os.close(descriptor)
    return True


def build_attestation() -> dict[str, Any]:
    expected_version = os.environ["CARLINA_PROBE_EXPECTED_VERSION"]
    denied_paths = {
        "live_data_root": os.environ["CARLINA_PROBE_DENIED_DATA_ROOT"],
        "managed_program_root": os.environ["CARLINA_PROBE_DENIED_PROGRAM_ROOT"],
        "managed_program_state": os.environ["CARLINA_PROBE_DENIED_PROGRAM_STATE"],
    }
    return {
        "format": ATTESTATION_FORMAT,
        "schema_version": ATTESTATION_SCHEMA_VERSION,
        "protocol": os.environ["CARLINA_PROBE_PROTOCOL"],
        "isolation": os.environ["CARLINA_PROBE_ISOLATION"],
        "backend": os.environ["CARLINA_PROBE_BACKEND"],
        "status": "passed",
        "target_version": expected_version,
        "installed_version": importlib.metadata.version("carlina"),
        "python_executable": os.path.realpath(sys.executable),
        "module_origin": os.path.realpath(__file__),
        "sys_path": [os.path.realpath(value) for value in sys.path if value],
        "identity": {
            "pid": os.getpid(),
            "ppid": os.getppid(),
            "uid": os.getuid(),
            "euid": os.geteuid(),
            "gid": os.getgid(),
            "egid": os.getegid(),
            "groups": sorted(os.getgroups()),
        },
        "namespaces": {
            name: os.readlink(f"/proc/self/ns/{name}") for name in _NAMESPACE_NAMES
        },
        "process_status": _process_status(),
        "environment_names": sorted(os.environ),
        "file_descriptors": _file_descriptors(),
        "mounts": _mounts(),
        "network_interfaces": _network_interfaces(),
        "write_checks": {
            "scratch_writable": _write_succeeds("/scratch/probe-write"),
            "root_writable": _write_succeeds("/carlina-probe-write"),
            "target_writable": _write_succeeds(
                "/opt/carlina-target/.carlina-probe-write"
            ),
        },
        "denied_paths": {
            label: _path_is_accessible(path) for label, path in denied_paths.items()
        },
    }


def main() -> int:
    try:
        payload = build_attestation()
    except BaseException as exc:  # noqa: BLE001 - emit only the exception type.
        payload = {
            "format": ATTESTATION_FORMAT,
            "schema_version": ATTESTATION_SCHEMA_VERSION,
            "status": "error",
            "error_type": type(exc).__name__,
        }
        print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        return 1
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
