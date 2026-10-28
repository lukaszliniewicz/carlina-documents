"""Host-resource admission checks for add-on lifecycle operations."""

from __future__ import annotations

import ctypes
import os
import platform
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

MIB = 1024**2
GIB = 1024**3
DEFAULT_TEMPORARY_HEADROOM_BYTES = 512 * MIB


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except TypeError, ValueError:
        return None
    return parsed if parsed > 0 else None


def _available_memory_bytes() -> int | None:
    """Return currently available host memory without adding a dependency."""

    if os.name == "nt":

        class MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("length", ctypes.c_ulong),
                ("memory_load", ctypes.c_ulong),
                ("total_physical", ctypes.c_ulonglong),
                ("available_physical", ctypes.c_ulonglong),
                ("total_page_file", ctypes.c_ulonglong),
                ("available_page_file", ctypes.c_ulonglong),
                ("total_virtual", ctypes.c_ulonglong),
                ("available_virtual", ctypes.c_ulonglong),
                ("available_extended_virtual", ctypes.c_ulonglong),
            ]

        status = MemoryStatus()
        status.length = ctypes.sizeof(status)
        try:
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return int(status.available_physical)
        except AttributeError, OSError:
            return None
        return None

    meminfo = Path("/proc/meminfo")
    if meminfo.is_file():
        try:
            for line in meminfo.read_text(encoding="utf-8").splitlines():
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
        except OSError, UnicodeError, ValueError, IndexError:
            pass

    try:
        pages = os.sysconf("SC_AVPHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        if pages > 0 and page_size > 0:
            return int(pages * page_size)
    except AttributeError, OSError, TypeError, ValueError:
        pass
    return None


def _platform_architecture() -> str:
    machine = platform.machine().strip().lower()
    architecture = {
        "amd64": "amd64",
        "x86_64": "amd64",
        "aarch64": "arm64",
        "arm64": "arm64",
    }.get(machine, machine or "unknown")
    system = platform.system().strip().lower() or "unknown"
    return f"{system}/{architecture}"


def _runtime_units(contract: Any) -> list[dict[str, Any]]:
    if isinstance(contract, Mapping):
        units = contract.get("runtime_units")
        if not isinstance(units, Sequence) or isinstance(units, (str, bytes)):
            runtime = contract.get("runtime")
            units = runtime.get("units") if isinstance(runtime, Mapping) else []
        return [dict(unit) for unit in units or [] if isinstance(unit, Mapping)]

    result = []
    for unit in getattr(contract, "runtime_units", ()) or ():
        requirements = getattr(unit, "requirements", {}) or {}
        result.append(
            {
                "id": str(getattr(unit, "id", "")),
                "mode": str(
                    getattr(getattr(unit, "mode", None), "value", "")
                    or getattr(unit, "mode", "")
                ),
                "lifecycle": str(getattr(unit, "lifecycle", "")),
                "requirements": dict(requirements),
            }
        )
    return result


def _manifest(contract: Any) -> dict[str, Any]:
    if isinstance(contract, Mapping):
        manifest = contract.get("manifest")
        return dict(manifest) if isinstance(manifest, Mapping) else dict(contract)
    value = getattr(contract, "manifest", {}) or {}
    return dict(value) if isinstance(value, Mapping) else {}


def _gpu_probe() -> tuple[str, str]:
    executable = shutil.which("nvidia-smi")
    if not executable:
        return (
            "unknown",
            "No host NVIDIA probe is available; container GPU access will be verified when the service starts.",
        )
    try:
        completed = subprocess.run(
            [executable, "-L"],
            capture_output=True,
            check=False,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return "unknown", f"The host GPU probe could not complete: {exc}"
    if completed.returncode == 0 and completed.stdout.strip():
        return "available", "An NVIDIA GPU is visible to the host."
    return "unavailable", "No NVIDIA GPU was reported by the host probe."


def _disk_check(path: Path, required_bytes: int, *, label: str) -> dict[str, Any]:
    try:
        path.mkdir(parents=True, exist_ok=True)
        free_bytes = int(shutil.disk_usage(path).free)
    except OSError as exc:
        return {
            "label": label,
            "path": str(path),
            "status": "unknown",
            "required_bytes": required_bytes,
            "available_bytes": None,
            "message": f"Free space could not be measured: {exc}",
        }
    allowed = free_bytes >= required_bytes
    return {
        "label": label,
        "path": str(path),
        "status": "ok" if allowed else "blocked",
        "required_bytes": required_bytes,
        "available_bytes": free_bytes,
        "message": (
            "Enough free space is available."
            if allowed
            else "The operation does not have enough free space."
        ),
    }


def evaluate_addon_admission(
    *,
    paths: Any,
    contract: Any,
    archive_size: int | None = None,
    manager: Any = None,
) -> dict[str, Any]:
    """Return a serializable, conservative resource-admission report.

    Declared estimates are authoritative when present. Unknown container image
    and model costs remain visible warnings instead of invented hard limits.
    """

    manifest = _manifest(contract)
    estimates = manifest.get("installation")
    estimates = dict(estimates) if isinstance(estimates, dict) else {}
    units = _runtime_units(contract)
    container_services_enabled = bool(
        getattr(manager, "container_services_enabled", True)
    )
    container_units = [
        unit
        for unit in units
        if str(unit.get("mode")) == "container"
        and (
            container_services_enabled
            or str(unit.get("lifecycle") or "").strip().lower() != "on_demand"
        )
    ]
    gpu_requirements = {
        str((unit.get("requirements") or {}).get("gpu") or "none").lower()
        for unit in container_units
    }

    archive_bytes = (
        _positive_int(archive_size)
        or _positive_int(estimates.get("download_bytes"))
        or 0
    )
    unpacked_bytes = _positive_int(estimates.get("unpacked_image_bytes")) or 0
    model_bytes = _positive_int(estimates.get("model_cache_bytes")) or 0
    build_cache_bytes = _positive_int(estimates.get("build_cache_bytes")) or 0
    explicit_required_disk = _positive_int(estimates.get("required_disk_bytes"))
    required_data_bytes = explicit_required_disk or (
        archive_bytes * 3
        + unpacked_bytes
        + model_bytes
        + build_cache_bytes
        + DEFAULT_TEMPORARY_HEADROOM_BYTES
    )
    disk_checks = [
        _disk_check(
            Path(paths.staging_dir),
            required_data_bytes,
            label="package staging and persistent data",
        )
    ]
    environment_path = Path(paths.addon_environments_dir)
    if environment_path.resolve() != Path(paths.staging_dir).resolve():
        environment_bytes = (
            _positive_int(estimates.get("environment_bytes"))
            or DEFAULT_TEMPORARY_HEADROOM_BYTES
        )
        disk_checks.append(
            _disk_check(
                environment_path,
                environment_bytes,
                label="rebuildable add-on environments",
            )
        )

    warnings: list[str] = []
    if container_units and not any(
        _positive_int(estimates.get(key))
        for key in ("unpacked_image_bytes", "build_cache_bytes")
    ):
        warnings.append(
            "Container image and build-cache sizes are not declared; the disk check covers only package staging and safety headroom."
        )

    minimum_ram = _positive_int(estimates.get("minimum_ram_bytes"))
    available_ram = _available_memory_bytes()
    memory_status = "unknown"
    if minimum_ram is not None and available_ram is not None:
        memory_status = "ok" if available_ram >= minimum_ram else "blocked"
    elif minimum_ram is not None:
        warnings.append("Available RAM could not be measured on this host.")
    elif container_units:
        warnings.append("The package does not declare a minimum-RAM estimate.")
    memory = {
        "status": memory_status,
        "required_bytes": minimum_ram,
        "available_bytes": available_ram,
        "message": (
            "Enough available RAM was measured."
            if memory_status == "ok"
            else "The operation does not have enough available RAM."
            if memory_status == "blocked"
            else "RAM admission is informational because no complete estimate is available."
        ),
    }

    runtime_available = True
    runtime_message = "No container runtime is required."
    if container_units:
        if not container_services_enabled:
            runtime_available = False
            runtime_message = "This application does not include container services."
        else:
            runtime_available = bool(
                manager
                and manager.container_runtime_available(
                    log_failure=False,
                    force=True,
                )
            )
            runtime_message = str(
                getattr(manager, "docker_status_message", "")
                or (
                    "Container runtime is available."
                    if runtime_available
                    else "Container runtime is not available."
                )
            )

    gpu_requirement = (
        "required"
        if "required" in gpu_requirements
        else "optional"
        if "optional" in gpu_requirements
        else "none"
    )
    gpu_status, gpu_message = (
        _gpu_probe()
        if gpu_requirement != "none"
        else ("not_required", "No GPU is required.")
    )
    if gpu_requirement == "optional" and gpu_status != "available":
        gpu_message += " The package declares a CPU fallback."
    if gpu_requirement == "required" and gpu_status == "unavailable":
        warnings.append(
            "A required GPU was not visible to the host probe; the container runtime remains the final authority."
        )

    declared_architectures = estimates.get("architectures")
    declared_architectures = (
        [
            str(item).strip().lower()
            for item in declared_architectures
            if str(item).strip()
        ]
        if isinstance(declared_architectures, list)
        else []
    )
    current_architecture = _platform_architecture()
    architecture_status = (
        "ok"
        if not declared_architectures or current_architecture in declared_architectures
        else "blocked"
    )
    if container_units and not declared_architectures:
        warnings.append(
            "The package does not declare an installation architecture matrix."
        )

    blocked = [check for check in disk_checks if check["status"] == "blocked"]
    if memory_status == "blocked":
        blocked.append(memory)
    if container_units and not runtime_available:
        blocked.append({"status": "blocked", "message": runtime_message})
    if architecture_status == "blocked":
        blocked.append(
            {
                "status": "blocked",
                "message": f"This package supports {', '.join(declared_architectures)}, not {current_architecture}.",
            }
        )

    return {
        "version": 1,
        "allowed": not blocked,
        "estimated_seconds": _positive_int(estimates.get("estimated_seconds")),
        "costs": {
            "download_bytes": archive_bytes or None,
            "unpacked_image_bytes": unpacked_bytes or None,
            "model_cache_bytes": model_bytes or None,
            "build_cache_bytes": build_cache_bytes or None,
            "required_disk_bytes": required_data_bytes,
        },
        "disk": disk_checks,
        "memory": memory,
        "container_runtime": {
            "required": bool(container_units),
            "status": "ok" if runtime_available else "blocked",
            "message": runtime_message,
        },
        "gpu": {
            "requirement": gpu_requirement,
            "status": gpu_status,
            "message": gpu_message,
        },
        "architecture": {
            "current": current_architecture,
            "supported": declared_architectures,
            "status": architecture_status,
        },
        "warnings": warnings,
        "blocking_reasons": [
            str(item.get("message") or "Admission failed.") for item in blocked
        ],
    }


__all__ = ["evaluate_addon_admission"]
