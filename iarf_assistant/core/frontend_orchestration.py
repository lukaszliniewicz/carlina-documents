from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .frontend_builds import (
    STATUS_BUILT,
    STATUS_FAILED,
    STATUS_MISCONFIGURED,
    STATUS_MISSING,
    STATUS_STALE,
    _prepare_subprocess_command,
    hash_paths,
    inspect_frontend_build,
    run_frontend_build,
)


FRONTEND_API_VERSION = "1.0"
CORE_STAMP_SCHEMA_VERSION = 1
DEPENDENCY_STAMP_SCHEMA_VERSION = 1
CORE_COMMAND = ["npm", "run", "build:core"]
INSTALL_COMMAND = ["npm", "ci"]
CORE_STAMP_COMPARE_KEYS = {
    "schema_version",
    "frontend_api_version",
    "build_command",
    "lockfile_hash",
    "source_hash",
    "source_paths",
}
CORE_STAMP_METADATA_COMPARE_KEYS = CORE_STAMP_COMPARE_KEYS - {
    "lockfile_hash",
    "source_hash",
}
_RUNTIME_STATUS_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_RUNTIME_STATUS_CACHE_LOCK = threading.RLock()


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _project_paths(project_root: str | Path) -> dict[str, Path]:
    root = Path(project_root).resolve()
    output_root = root / "iarf_assistant" / "static" / "frontend"
    return {
        "root": root,
        "output_root": output_root,
        "manifest": output_root / "manifest.json",
        "stamp": output_root / ".frontend-build-stamp.json",
        "failure": output_root / ".frontend-build-failure.json",
        "package_json": root / "package.json",
        "lockfile": root / "package-lock.json",
        "dependency_stamp": root / "node_modules" / ".iarf-install-stamp.json",
    }


def _core_source_paths(root: Path) -> list[Path]:
    paths = [
        root / "frontend" / "apps" / "core" / "src",
        root / "frontend" / "apps" / "core" / "package.json",
        root / "frontend" / "apps" / "core" / "tsconfig.json",
        root / "frontend" / "apps" / "core" / "vite.config.ts",
        root / "frontend" / "tsconfig.base.json",
        root / "package.json",
        root / "package-lock.json",
    ]
    packages_root = root / "frontend" / "packages"
    if packages_root.is_dir():
        for package_root in sorted(
            (path for path in packages_root.iterdir() if path.is_dir()),
            key=lambda path: path.name,
        ):
            paths.extend(
                [
                    package_root / "src",
                    package_root / "package.json",
                    package_root / "tsconfig.json",
                ]
            )
    distribution_descriptor = root / "carlina" / "distribution.json"
    if distribution_descriptor.is_file():
        paths.append(distribution_descriptor)
    return paths


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _safe_output_path(output_root: Path, raw_path: Any) -> Path | None:
    value = str(raw_path or "").strip().replace("\\", "/")
    if not value or value.startswith("/") or "://" in value:
        return None
    candidate = (output_root / value).resolve()
    try:
        candidate.relative_to(output_root.resolve())
    except ValueError:
        return None
    return candidate


def _stamped_output_errors(
    project_root: Path,
    stamp: dict[str, Any] | None,
) -> list[str]:
    raw_paths = (stamp or {}).get("output_paths")
    if not raw_paths:
        return []
    if not isinstance(raw_paths, list):
        return ["Core frontend build stamp output paths are invalid."]

    missing: list[str] = []
    for raw_path in raw_paths:
        value = str(raw_path or "").strip().replace("\\", "/")
        if not value or value.startswith("/") or "://" in value:
            return ["Core frontend build stamp contains an invalid output path."]
        candidate = (project_root / value).resolve()
        try:
            candidate.relative_to(project_root)
        except ValueError:
            return ["Core frontend build stamp contains an invalid output path."]
        if not candidate.is_file():
            missing.append(candidate.name)

    if not missing:
        return []
    return [
        "Core frontend stamped output is missing: "
        + ", ".join(dict.fromkeys(missing))
        + "."
    ]


def read_core_asset_manifest(project_root: str | Path) -> tuple[dict[str, Any] | None, list[str]]:
    paths = _project_paths(project_root)
    manifest = _read_json(paths["manifest"])
    if manifest is None:
        return None, ["Core frontend manifest is missing or invalid."]

    entries = [
        (key, value)
        for key, value in manifest.items()
        if isinstance(value, dict) and value.get("isEntry")
    ]
    if len(entries) != 1:
        return None, ["Core frontend manifest must contain exactly one entry module."]

    entry_key, entry = entries[0]
    entry_path = _safe_output_path(paths["output_root"], entry.get("file"))
    if entry_path is None:
        return None, ["Core frontend entry path is invalid."]

    output_files: list[Path] = []
    for record_key, record in manifest.items():
        if not isinstance(record, dict):
            return None, [
                f"Core frontend manifest record '{record_key}' is invalid."
            ]
        record_file = _safe_output_path(
            paths["output_root"],
            record.get("file"),
        )
        if record_file is None:
            return None, [
                f"Core frontend output path for '{record_key}' is invalid."
            ]
        output_files.append(record_file)
        for field in ("css", "assets"):
            values = record.get(field) or []
            if not isinstance(values, list):
                return None, [
                    f"Core frontend {field} field for '{record_key}' must be a list."
                ]
            resolved_values = [
                _safe_output_path(paths["output_root"], value)
                for value in values
            ]
            if any(value is None for value in resolved_values):
                return None, [
                    f"Core frontend {field} path for '{record_key}' is invalid."
                ]
            output_files.extend(
                value for value in resolved_values if value is not None
            )
        for field in ("imports", "dynamicImports"):
            references = record.get(field) or []
            if not isinstance(references, list):
                return None, [
                    f"Core frontend {field} field for '{record_key}' must be a list."
                ]
            missing_records = [
                reference
                for reference in references
                if reference not in manifest
            ]
            if missing_records:
                return None, [
                    (
                        f"Core frontend manifest record '{record_key}' references "
                        f"missing {field}: {', '.join(missing_records)}."
                    )
                ]

    assets_root = paths["output_root"] / "assets"
    if assets_root.is_dir():
        output_files.extend(
            path
            for path in sorted(assets_root.rglob("*"))
            if path.is_file()
        )

    unique_output_files = list(dict.fromkeys(output_files))
    missing = [path for path in unique_output_files if not path.is_file()]
    if missing:
        labels = ", ".join(path.name for path in missing)
        return None, [f"Core frontend output is missing: {labels}."]

    initial_record_keys: list[str] = []
    visited_records: set[str] = set()

    def visit_initial_record(record_key: str) -> None:
        if record_key in visited_records:
            return
        visited_records.add(record_key)
        initial_record_keys.append(record_key)
        record = manifest[record_key]
        for dependency in record.get("imports") or []:
            visit_initial_record(dependency)

    visit_initial_record(entry_key)
    initial_js_paths = [
        _safe_output_path(
            paths["output_root"],
            manifest[record_key].get("file"),
        )
        for record_key in initial_record_keys
        if str(manifest[record_key].get("file") or "").endswith(".js")
    ]
    initial_css_paths = [
        _safe_output_path(paths["output_root"], value)
        for record_key in initial_record_keys
        for value in (manifest[record_key].get("css") or [])
    ]
    initial_js_paths = list(
        dict.fromkeys(
            path for path in initial_js_paths if path is not None
        )
    )
    css_paths = list(
        dict.fromkeys(
            path for path in initial_css_paths if path is not None
        )
    )

    entry_relative = entry_path.relative_to(paths["output_root"]).as_posix()
    initial_js_relative = [
        path.relative_to(paths["output_root"]).as_posix()
        for path in initial_js_paths
    ]
    css_relative = [
        path.relative_to(paths["output_root"]).as_posix()
        for path in css_paths
        if path is not None
    ]
    return {
        "entry_path": entry_relative,
        "entry_url": f"/static/frontend/{entry_relative}",
        "initial_js_paths": initial_js_relative,
        "css_paths": css_relative,
        "css_urls": [f"/static/frontend/{value}" for value in css_relative],
        "output_paths": [
            "iarf_assistant/static/frontend/manifest.json",
            *[
                (
                    "iarf_assistant/static/frontend/"
                    + path.relative_to(paths["output_root"]).as_posix()
                )
                for path in unique_output_files
            ],
        ],
    }, []


def build_expected_core_stamp(project_root: str | Path) -> dict[str, Any]:
    paths = _project_paths(project_root)
    root = paths["root"]
    source_paths = _core_source_paths(root)
    lock_hash = hashlib.sha256()
    if paths["lockfile"].is_file():
        lock_hash.update(paths["lockfile"].read_bytes())
    else:
        lock_hash.update(b"MISSING")
    return {
        **build_expected_core_stamp_metadata(root),
        "lockfile_hash": lock_hash.hexdigest(),
        "source_hash": hash_paths(source_paths, root),
    }


def build_expected_core_stamp_metadata(
    project_root: str | Path,
) -> dict[str, Any]:
    paths = _project_paths(project_root)
    root = paths["root"]
    source_paths = _core_source_paths(root)
    return {
        "schema_version": CORE_STAMP_SCHEMA_VERSION,
        "frontend_api_version": FRONTEND_API_VERSION,
        "build_command": list(CORE_COMMAND),
        "source_paths": [
            path.resolve().relative_to(root).as_posix()
            for path in source_paths
        ],
    }


def _core_stamp_matches(
    expected: dict[str, Any],
    existing: dict[str, Any] | None,
) -> bool:
    return bool(existing) and all(
        existing.get(key) == expected.get(key)
        for key in CORE_STAMP_COMPARE_KEYS
    )


def _core_stamp_metadata_matches(
    expected: dict[str, Any],
    existing: dict[str, Any] | None,
) -> bool:
    return bool(existing) and all(
        existing.get(key) == expected.get(key)
        for key in CORE_STAMP_METADATA_COMPARE_KEYS
    )


def inspect_core_frontend(
    project_root: str | Path,
    *,
    verify_sources: bool = True,
) -> dict[str, Any]:
    paths = _project_paths(project_root)
    root = paths["root"]
    assets, asset_errors = read_core_asset_manifest(root)
    existing_stamp = _read_json(paths["stamp"])
    asset_errors.extend(_stamped_output_errors(root, existing_stamp))
    failure = _read_json(paths["failure"])
    source_available = (
        (root / "frontend" / "apps" / "core").is_dir()
        and paths["package_json"].is_file()
        and paths["lockfile"].is_file()
    )

    if not source_available:
        if asset_errors:
            return {
                "status": STATUS_FAILED if failure else STATUS_MISSING,
                "rebuild_needed": False,
                "source_available": False,
                "reasons": asset_errors,
                "warnings": [
                    "Core frontend source is not included in this packaged installation."
                ],
                "assets": None,
                "build_id": None,
                "stamp_current": False,
                "last_failure": failure,
            }
        build_id = (
            str((existing_stamp or {}).get("source_hash") or "")[:12]
            or _asset_build_id(assets)
        )
        return {
            "status": STATUS_BUILT,
            "rebuild_needed": False,
            "source_available": False,
            "reasons": [],
            "warnings": [
                "Validated prebuilt core assets; frontend source is not installed."
            ],
            "assets": assets,
            "build_id": build_id,
            "stamp_current": existing_stamp is not None,
            "last_failure": failure,
        }

    expected_stamp = (
        build_expected_core_stamp(root)
        if verify_sources
        else build_expected_core_stamp_metadata(root)
    )
    stamp_current = (
        _core_stamp_matches(expected_stamp, existing_stamp)
        if verify_sources
        else _core_stamp_metadata_matches(expected_stamp, existing_stamp)
    )
    reasons = list(asset_errors)
    if asset_errors:
        status = STATUS_FAILED if failure else STATUS_MISSING
        rebuild_needed = True
    elif not stamp_current:
        reasons.append(
            "Core frontend build stamp is missing."
            if existing_stamp is None
            else "Core frontend build stamp is stale."
        )
        status = STATUS_FAILED if failure else STATUS_STALE
        rebuild_needed = True
    else:
        status = STATUS_BUILT
        rebuild_needed = False

    if failure and status == STATUS_FAILED:
        error = failure.get("error")
        if error:
            reasons.append(f"Last core frontend build failed: {error}")

    build_id = (
        str((existing_stamp or expected_stamp).get("source_hash") or "")[:12]
        if assets
        else None
    )
    return {
        "status": status,
        "rebuild_needed": rebuild_needed,
        "source_available": True,
        "reasons": reasons,
        "warnings": [],
        "assets": assets,
        "build_id": build_id,
        "stamp_path": paths["stamp"].relative_to(root).as_posix(),
        "stamp_current": stamp_current,
        "expected_stamp": expected_stamp,
        "existing_stamp": existing_stamp,
        "last_failure": failure,
        "validation_mode": "full" if verify_sources else "metadata",
    }


def _asset_build_id(assets: dict[str, Any] | None) -> str | None:
    if not assets:
        return None
    digest = hashlib.sha256(
        json.dumps(assets, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return digest[:12]


def _run_command(
    command: list[str],
    *,
    cwd: Path,
) -> subprocess.CompletedProcess[str]:
    prepared, _uses_shell = _prepare_subprocess_command(command)
    return subprocess.run(
        prepared,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _write_core_failure(
    project_root: str | Path,
    *,
    error: str,
    result: subprocess.CompletedProcess[str] | None = None,
) -> None:
    paths = _project_paths(project_root)
    _write_json(
        paths["failure"],
        {
            "error": error,
            "failed_at": _utc_timestamp(),
            "returncode": result.returncode if result else None,
            "stdout_tail": (result.stdout or "")[-4000:] if result else "",
            "stderr_tail": (result.stderr or "")[-4000:] if result else "",
        },
    )


def run_core_frontend_build(
    project_root: str | Path,
    *,
    force: bool = False,
) -> dict[str, Any]:
    paths = _project_paths(project_root)
    before = inspect_core_frontend(paths["root"])
    if not force and not before.get("rebuild_needed"):
        return {
            "success": True,
            "message": "Core frontend assets are already current.",
            "status": before,
            "stdout": "",
            "stderr": "",
            "returncode": 0,
        }
    if not before.get("source_available"):
        return {
            "success": False,
            "message": "Core frontend source is unavailable; reinstall prebuilt assets.",
            "status": before,
            "stdout": "",
            "stderr": "",
            "returncode": None,
        }

    try:
        result = _run_command(CORE_COMMAND, cwd=paths["root"])
    except OSError as exc:
        _write_core_failure(paths["root"], error=str(exc))
        return {
            "success": False,
            "message": str(exc),
            "status": inspect_core_frontend(paths["root"]),
            "stdout": "",
            "stderr": "",
            "returncode": None,
        }

    if result.returncode != 0:
        message = f"Core frontend command exited with code {result.returncode}."
        _write_core_failure(paths["root"], error=message, result=result)
        return {
            "success": False,
            "message": message,
            "status": inspect_core_frontend(paths["root"]),
            "stdout": result.stdout or "",
            "stderr": result.stderr or "",
            "returncode": result.returncode,
        }

    assets, errors = read_core_asset_manifest(paths["root"])
    if errors:
        message = errors[0]
        _write_core_failure(paths["root"], error=message, result=result)
        return {
            "success": False,
            "message": message,
            "status": inspect_core_frontend(paths["root"]),
            "stdout": result.stdout or "",
            "stderr": result.stderr or "",
            "returncode": result.returncode,
        }

    stamp = build_expected_core_stamp(paths["root"])
    stamp["built_at"] = _utc_timestamp()
    stamp["output_paths"] = list((assets or {}).get("output_paths") or [])
    _write_json(paths["stamp"], stamp)
    paths["failure"].unlink(missing_ok=True)
    return {
        "success": True,
        "message": "Core frontend assets built successfully.",
        "status": inspect_core_frontend(paths["root"]),
        "stdout": result.stdout or "",
        "stderr": result.stderr or "",
        "returncode": result.returncode,
    }


def _dependency_expected_stamp(project_root: str | Path) -> dict[str, Any]:
    paths = _project_paths(project_root)
    root = paths["root"]
    tracked = [
        root / "package.json",
        root / "package-lock.json",
        root / "pixi.toml",
    ]
    return {
        "schema_version": DEPENDENCY_STAMP_SCHEMA_VERSION,
        "dependency_hash": hash_paths(tracked, root),
        "tracked_paths": [
            path.relative_to(root).as_posix()
            for path in tracked
        ],
    }


def frontend_dependencies_current(project_root: str | Path) -> bool:
    paths = _project_paths(project_root)
    existing = _read_json(paths["dependency_stamp"])
    expected = _dependency_expected_stamp(paths["root"])
    return bool(
        (paths["root"] / "node_modules").is_dir()
        and existing
        and existing.get("schema_version") == expected["schema_version"]
        and existing.get("dependency_hash") == expected["dependency_hash"]
    )


def install_frontend_dependencies(
    project_root: str | Path,
    *,
    force: bool = False,
) -> dict[str, Any]:
    paths = _project_paths(project_root)
    if not force and frontend_dependencies_current(paths["root"]):
        return {
            "success": True,
            "message": "Frontend dependencies are already current.",
            "stdout": "",
            "stderr": "",
            "returncode": 0,
            "installed": False,
        }
    if not paths["package_json"].is_file() or not paths["lockfile"].is_file():
        return {
            "success": False,
            "message": "package.json or package-lock.json is missing.",
            "stdout": "",
            "stderr": "",
            "returncode": None,
            "installed": False,
        }

    try:
        result = _run_command(INSTALL_COMMAND, cwd=paths["root"])
    except OSError as exc:
        return {
            "success": False,
            "message": str(exc),
            "stdout": "",
            "stderr": "",
            "returncode": None,
            "installed": False,
        }
    if result.returncode != 0:
        return {
            "success": False,
            "message": f"npm ci exited with code {result.returncode}.",
            "stdout": result.stdout or "",
            "stderr": result.stderr or "",
            "returncode": result.returncode,
            "installed": False,
        }

    stamp = _dependency_expected_stamp(paths["root"])
    stamp["installed_at"] = _utc_timestamp()
    _write_json(paths["dependency_stamp"], stamp)
    return {
        "success": True,
        "message": "Frontend dependencies installed.",
        "stdout": result.stdout or "",
        "stderr": result.stderr or "",
        "returncode": result.returncode,
        "installed": True,
    }


def discover_declared_addon_frontends(
    project_root: str | Path,
) -> list[tuple[dict[str, Any], Path]]:
    root = Path(project_root).resolve()
    addons_root = root / "iarf_assistant" / "addons"
    declarations: list[tuple[dict[str, Any], Path]] = []
    for manifest_path in sorted(addons_root.glob("*/manifest.json")):
        manifest = _read_json(manifest_path)
        frontend = (manifest or {}).get("frontend")
        if (
            manifest
            and isinstance(frontend, dict)
            and frontend.get("build") not in (None, False)
        ):
            declarations.append((manifest, manifest_path.parent))
    return declarations


def inspect_frontend_workspace(
    project_root: str | Path,
    *,
    verify_sources: bool = True,
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    addon_statuses = [
        inspect_frontend_build(
            manifest,
            addon_dir,
            verify_sources=verify_sources,
        )
        for manifest, addon_dir in discover_declared_addon_frontends(root)
    ]
    core = inspect_core_frontend(root, verify_sources=verify_sources)
    status = {
        "core": core,
        "addons": addon_statuses,
        "ready": (
            core.get("status") == STATUS_BUILT
            and all(status.get("status") == STATUS_BUILT for status in addon_statuses)
        ),
        "repair_needed": bool(
            core.get("rebuild_needed")
            or any(status.get("rebuild_needed") for status in addon_statuses)
        ),
        "validation_mode": "full" if verify_sources else "metadata",
    }
    cache_frontend_workspace_status(root, status)
    return status


def cache_frontend_workspace_status(
    project_root: str | Path,
    status: dict[str, Any],
) -> None:
    """Publishes a process-local snapshot for shell and registry requests."""
    key = str(Path(project_root).resolve())
    with _RUNTIME_STATUS_CACHE_LOCK:
        _RUNTIME_STATUS_CACHE[key] = (
            time.monotonic(),
            copy.deepcopy(status),
        )


def get_cached_frontend_workspace_status(
    project_root: str | Path,
) -> dict[str, Any] | None:
    key = str(Path(project_root).resolve())
    with _RUNTIME_STATUS_CACHE_LOCK:
        entry = _RUNTIME_STATUS_CACHE.get(key)
        return copy.deepcopy(entry[1]) if entry else None


def get_runtime_frontend_workspace_status(
    project_root: str | Path,
) -> dict[str, Any]:
    """Returns the startup snapshot, falling back to metadata validation."""
    cached = get_cached_frontend_workspace_status(project_root)
    if cached is not None:
        return cached
    return inspect_frontend_workspace(project_root, verify_sources=False)


def get_runtime_core_frontend(
    project_root: str | Path,
) -> dict[str, Any]:
    return (
        get_runtime_frontend_workspace_status(project_root).get("core")
        or {}
    )


def invalidate_runtime_frontend_status(
    project_root: str | Path,
) -> None:
    key = str(Path(project_root).resolve())
    with _RUNTIME_STATUS_CACHE_LOCK:
        _RUNTIME_STATUS_CACHE.pop(key, None)


def runtime_frontend_status_cache_info(
    project_root: str | Path,
) -> dict[str, Any]:
    key = str(Path(project_root).resolve())
    with _RUNTIME_STATUS_CACHE_LOCK:
        entry = _RUNTIME_STATUS_CACHE.get(key)
        if not entry:
            return {"hit": False, "age_seconds": None}
        return {
            "hit": True,
            "age_seconds": max(0.0, time.monotonic() - entry[0]),
        }


def build_frontend_workspace(
    project_root: str | Path,
    *,
    force: bool = False,
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    before = inspect_frontend_workspace(root)
    needs_build = bool(force or before["repair_needed"])
    dependency_result = {
        "success": True,
        "message": "No frontend build is required.",
        "stdout": "",
        "stderr": "",
        "returncode": 0,
        "installed": False,
    }
    if needs_build:
        dependency_result = install_frontend_dependencies(root)
        if not dependency_result["success"]:
            return {
                "success": False,
                "message": dependency_result["message"],
                "dependencies": dependency_result,
                "core": None,
                "addons": [],
                "status": before,
            }

    core_before = before["core"]
    core_result = (
        run_core_frontend_build(root, force=force)
        if force or core_before.get("rebuild_needed")
        else {
            "success": True,
            "message": "Core frontend assets are already current.",
            "status": core_before,
            "stdout": "",
            "stderr": "",
            "returncode": 0,
        }
    )

    addon_results = []
    for manifest, addon_dir in discover_declared_addon_frontends(root):
        status = inspect_frontend_build(manifest, addon_dir)
        if force or status.get("rebuild_needed"):
            result = run_frontend_build(
                manifest,
                addon_dir,
                force=force,
            )
        else:
            result = {
                "success": True,
                "message": "Frontend assets are already current.",
                "status": status,
                "stdout": "",
                "stderr": "",
                "returncode": 0,
            }
        result["addon_id"] = str(manifest.get("id") or addon_dir.name)
        addon_results.append(result)

    final_status = inspect_frontend_workspace(root)
    success = bool(
        dependency_result["success"]
        and core_result["success"]
        and all(result["success"] for result in addon_results)
        and final_status["ready"]
    )
    return {
        "success": success,
        "message": (
            "All frontend assets are current."
            if success
            else "One or more frontend assets require repair."
        ),
        "dependencies": dependency_result,
        "core": core_result,
        "addons": addon_results,
        "status": final_status,
    }


__all__ = [
    "FRONTEND_API_VERSION",
    "STATUS_BUILT",
    "build_expected_core_stamp_metadata",
    "build_expected_core_stamp",
    "build_frontend_workspace",
    "cache_frontend_workspace_status",
    "discover_declared_addon_frontends",
    "frontend_dependencies_current",
    "get_cached_frontend_workspace_status",
    "get_runtime_core_frontend",
    "get_runtime_frontend_workspace_status",
    "invalidate_runtime_frontend_status",
    "inspect_core_frontend",
    "inspect_frontend_workspace",
    "install_frontend_dependencies",
    "read_core_asset_manifest",
    "runtime_frontend_status_cache_info",
    "run_core_frontend_build",
]
