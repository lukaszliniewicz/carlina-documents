from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from carlina.core.utc import utc_iso


VALID_FRONTEND_TIERS = {
    "schema",
    "jinja",
    "server_workspace",
    "compiled_workspace",
    "hybrid",
    "external_service",
}

STATUS_NOT_DECLARED = "not_declared"
STATUS_BUILT = "built"
STATUS_MISSING = "missing"
STATUS_STALE = "stale"
STATUS_FAILED = "failed"
STATUS_MISCONFIGURED = "misconfigured"

STAMP_SCHEMA_VERSION = 3
DEFAULT_STAMP_NAME = ".frontend-build-stamp.json"
DEFAULT_FAILURE_NAME = ".frontend-build-failure.json"
DEFAULT_LOCKFILES = (
    "package-lock.json",
    "npm-shrinkwrap.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "bun.lock",
    "bun.lockb",
)
EXCLUDED_SOURCE_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    "__pycache__",
    "env",
    "node_modules",
    ".venv",
    "venv",
}
STAMP_COMPARE_KEYS = {
    "schema_version",
    "addon_id",
    "addon_version",
    "frontend_api_range",
    "build_command",
    "lockfile_hash",
    "source_hash",
    "source_paths",
    "lockfile_paths",
    "output_paths",
}
STAMP_METADATA_COMPARE_KEYS = STAMP_COMPARE_KEYS - {
    "lockfile_hash",
    "source_hash",
}
_FILE_CONTENT_CACHE_MAX_BYTES = 64 * 1024 * 1024
_FILE_CONTENT_CACHE: "OrderedDict[tuple[str, int, int], bytes]" = OrderedDict()
_FILE_CONTENT_CACHE_BYTES = 0
_FILE_CONTENT_CACHE_LOCK = threading.RLock()
_FILE_CONTENT_CACHE_HITS = 0
_FILE_CONTENT_CACHE_MISSES = 0


@dataclass
class FrontendBuildConfig:
    addon_id: str
    addon_version: str
    frontend_api_range: str | None
    addon_dir: Path
    static_dir: Path
    source_dir: Path
    command: str | list[str] | None
    watch_command: str | list[str] | None
    output_paths: list[Path]
    output_relpaths: list[str]
    source_paths: list[Path]
    workspace_source_paths: list[Path]
    workspace_source_labels: list[str]
    lockfile_paths: list[Path]
    lockfile_labels: list[str]
    stamp_path: Path
    failure_path: Path
    warnings: list[str] = field(default_factory=list)


def frontend_build_declared(manifest: dict[str, Any]) -> bool:
    frontend = manifest.get("frontend") or {}
    if not isinstance(frontend, dict) or "build" not in frontend:
        return False
    build = frontend.get("build")
    return build is not None and build is not False


def validate_frontend_tier(frontend_tier: str) -> list[str]:
    if frontend_tier in VALID_FRONTEND_TIERS:
        return []
    return [
        f"Unknown frontend tier '{frontend_tier}'. Expected one of: {', '.join(sorted(VALID_FRONTEND_TIERS))}."
    ]


def inspect_frontend_build(
    manifest: dict[str, Any],
    addon_dir: str | Path,
    *,
    verify_sources: bool = True,
) -> dict[str, Any]:
    addon_dir = Path(addon_dir)
    addon_id = str(manifest.get("id") or addon_dir.name)
    frontend = manifest.get("frontend") or {}
    tier = frontend.get("tier") if isinstance(frontend, dict) else None
    tier = str(tier or "") if tier else None

    if not frontend_build_declared(manifest):
        return {
            "declared": False,
            "addon_id": addon_id,
            "tier": tier,
            "status": STATUS_NOT_DECLARED,
            "rebuild_needed": False,
            "reasons": [],
            "warnings": [],
            "output_paths": [],
            "missing_outputs": [],
            "log_source_id": f"addon.{addon_id}.build",
        }

    config, errors = load_frontend_build_config(manifest, addon_dir)
    if errors:
        return {
            "declared": True,
            "addon_id": addon_id,
            "tier": tier,
            "status": STATUS_MISCONFIGURED,
            "rebuild_needed": False,
            "reasons": errors,
            "warnings": [],
            "output_paths": [],
            "missing_outputs": [],
            "log_source_id": f"addon.{addon_id}.build",
        }

    assert config is not None
    expected_stamp = (
        build_expected_stamp(config)
        if verify_sources
        else build_expected_stamp_metadata(config)
    )
    existing_stamp = _read_json(config.stamp_path)
    failure = _read_json(config.failure_path)
    missing_outputs = [path for path in config.output_paths if not path.exists()]
    stamp_current = (
        _stamp_matches(expected_stamp, existing_stamp)
        if verify_sources
        else _stamp_metadata_matches(expected_stamp, existing_stamp)
    )
    packaged_prebuilt = (
        not config.source_dir.is_dir()
        and _find_workspace_root(config.addon_dir) is None
    )

    reasons: list[str] = []
    warnings = list(config.warnings)
    if packaged_prebuilt and missing_outputs:
        reasons.extend(f"Missing packaged frontend output: {relpath(path, config.addon_dir)}" for path in missing_outputs)
        warnings.append(
            "Frontend source is not installed; repair by reinstalling the addon package."
        )
        status = STATUS_FAILED if failure else STATUS_MISSING
        rebuild_needed = False
    elif packaged_prebuilt:
        warnings.append(
            "Validated prebuilt addon assets; frontend source is not installed."
        )
        status = STATUS_BUILT
        rebuild_needed = False
    elif missing_outputs:
        reasons.extend(f"Missing frontend output: {relpath(path, config.addon_dir)}" for path in missing_outputs)
        status = STATUS_FAILED if failure else STATUS_MISSING
        rebuild_needed = True
    elif not config.command:
        # Artifact-only mode: if outputs are present and no command is declared, treat as prebuilt/current.
        status = STATUS_BUILT
        rebuild_needed = False
        warnings.append("No frontend build command is declared; treating declared outputs as prebuilt artifacts.")
    elif not stamp_current:
        if existing_stamp is None:
            reasons.append("Frontend build stamp is missing.")
        else:
            reasons.append("Frontend build stamp is stale for the current sources, lockfiles, command, or outputs.")
        status = STATUS_FAILED if failure else STATUS_STALE
        rebuild_needed = True
    else:
        status = STATUS_BUILT
        rebuild_needed = False

    if failure and status == STATUS_FAILED:
        error = failure.get("error") or failure.get("message")
        if error:
            reasons.append(f"Last frontend build failed: {error}")

    if rebuild_needed:
        if not config.command:
            warnings.append("No frontend build command is declared.")
        else:
            missing_executable = _missing_executable_warning(config.command)
            if missing_executable:
                warnings.append(missing_executable)

    return {
        "declared": True,
        "addon_id": config.addon_id,
        "tier": tier,
        "status": status,
        "rebuild_needed": rebuild_needed,
        "source_available": not packaged_prebuilt,
        "reasons": reasons,
        "warnings": warnings,
        "source_dir": relpath(config.source_dir, config.addon_dir),
        "command": command_display(config.command),
        "watch_command": command_display(config.watch_command),
        "output_paths": list(config.output_relpaths),
        "missing_outputs": [relpath(path, config.addon_dir) for path in missing_outputs],
        "stamp_path": relpath(config.stamp_path, config.addon_dir),
        "failure_path": relpath(config.failure_path, config.addon_dir),
        "stamp_current": stamp_current,
        "expected_stamp": expected_stamp,
        "existing_stamp": existing_stamp,
        "last_failure": failure,
        "log_source_id": f"addon.{config.addon_id}.build",
        "validation_mode": "full" if verify_sources else "metadata",
    }


def run_frontend_build(
    manifest: dict[str, Any],
    addon_dir: str | Path,
    watch: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    addon_dir = Path(addon_dir)
    before_status = inspect_frontend_build(manifest, addon_dir)
    if not before_status.get("declared"):
        return {
            "success": True,
            "message": "No frontend build is declared.",
            "status": before_status,
            "stdout": "",
            "stderr": "",
            "returncode": 0,
        }

    config, errors = load_frontend_build_config(manifest, addon_dir)
    if errors:
        return _failed_result(manifest, addon_dir, errors[0], before_status, errors=errors)
    assert config is not None

    if watch:
        command = config.watch_command
        if not command:
            return _failed_result(manifest, addon_dir, "No frontend watch command is declared.", before_status)
    else:
        command = config.command
        if not force and not before_status.get("rebuild_needed"):
            return {
                "success": True,
                "message": "Frontend assets are already current.",
                "status": before_status,
                "stdout": "",
                "stderr": "",
                "returncode": 0,
            }
        if not command:
            return _failed_result(manifest, addon_dir, "No frontend build command is declared.", before_status)

    if not config.source_dir.exists() or not config.source_dir.is_dir():
        return _failed_result(
            manifest,
            addon_dir,
            f"Frontend source directory is missing: {relpath(config.source_dir, config.addon_dir)}",
            before_status,
        )

    started_at = utc_iso()
    subprocess_command, _uses_shell = _prepare_subprocess_command(command)
    try:
        completed = subprocess.run(
            subprocess_command,
            cwd=str(config.source_dir),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError as exc:
        return _failed_result(manifest, addon_dir, str(exc), before_status, started_at=started_at)
    except OSError as exc:
        return _failed_result(manifest, addon_dir, str(exc), before_status, started_at=started_at)

    if completed.returncode != 0:
        error = f"Command exited with code {completed.returncode}."
        return _failed_result(
            manifest,
            addon_dir,
            error,
            before_status,
            started_at=started_at,
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
            returncode=completed.returncode,
        )

    if watch:
        return {
            "success": True,
            "message": "Frontend watch command exited successfully.",
            "status": inspect_frontend_build(manifest, addon_dir),
            "stdout": completed.stdout or "",
            "stderr": completed.stderr or "",
            "returncode": completed.returncode,
        }

    missing_outputs = [path for path in config.output_paths if not path.exists()]
    if missing_outputs:
        missing = ", ".join(relpath(path, config.addon_dir) for path in missing_outputs)
        return _failed_result(
            manifest,
            addon_dir,
            f"Build completed but declared output(s) are missing: {missing}",
            before_status,
            started_at=started_at,
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
            returncode=completed.returncode,
        )

    stamp = build_expected_stamp(config)
    stamp["built_at"] = utc_iso()
    config.stamp_path.parent.mkdir(parents=True, exist_ok=True)
    config.stamp_path.write_text(json.dumps(stamp, indent=2, sort_keys=True), encoding="utf-8")
    try:
        config.failure_path.unlink()
    except FileNotFoundError:
        pass

    return {
        "success": True,
        "message": "Frontend assets built successfully.",
        "status": inspect_frontend_build(manifest, addon_dir),
        "stdout": completed.stdout or "",
        "stderr": completed.stderr or "",
        "returncode": completed.returncode,
    }


def load_frontend_build_config(
    manifest: dict[str, Any],
    addon_dir: str | Path,
) -> tuple[FrontendBuildConfig | None, list[str]]:
    addon_dir = Path(addon_dir).resolve()
    addon_id = str(manifest.get("id") or addon_dir.name)
    frontend = manifest.get("frontend") or {}
    if not isinstance(frontend, dict):
        return None, ["frontend must be an object when frontend.build is used."]
    if "build" not in frontend:
        return None, []
    build = frontend.get("build")
    if build is None or build is False:
        return None, []
    if isinstance(build, str) or isinstance(build, list):
        build = {"command": build}
    if build is True:
        build = {}
    if not isinstance(build, dict):
        return None, ["frontend.build must be an object, command string, command list, or true."]

    errors: list[str] = []
    warnings: list[str] = []
    static_dir = (addon_dir / "static").resolve()
    source_dir = _resolve_under(addon_dir, build.get("source_dir", "frontend"), "source_dir", errors)
    output_paths, output_relpaths = _resolve_output_paths(addon_dir, static_dir, build, errors)
    stamp_path = _resolve_output_control_path(
        addon_dir,
        static_dir,
        build.get("stamp_path", DEFAULT_STAMP_NAME),
        "stamp_path",
        errors,
    )
    failure_path = _resolve_output_control_path(
        addon_dir,
        static_dir,
        build.get("failure_path", DEFAULT_FAILURE_NAME),
        "failure_path",
        errors,
    )
    command, command_errors = _normalize_command(build.get("command") or build.get("build_command"))
    watch_command, watch_errors = _normalize_command(build.get("watch_command"))
    errors.extend(command_errors)
    errors.extend(watch_errors)

    source_paths = _resolve_source_paths(addon_dir, source_dir, build, errors)
    workspace_source_paths, workspace_source_labels = (
        _resolve_workspace_source_paths(
            addon_dir,
            source_dir,
            build,
            errors,
        )
    )
    lockfile_paths, lockfile_labels = _resolve_lockfile_paths(
        addon_dir,
        source_dir,
        build,
        errors,
    )

    if source_dir and not source_dir.exists():
        warnings.append(f"Frontend source directory does not exist yet: {relpath(source_dir, addon_dir)}")

    if errors:
        return None, errors

    return (
        FrontendBuildConfig(
            addon_id=addon_id,
            addon_version=str(manifest.get("version", "0.0.0")),
            frontend_api_range=(
                str(frontend.get("api_version"))
                if frontend.get("api_version")
                else None
            ),
            addon_dir=addon_dir,
            static_dir=static_dir,
            source_dir=source_dir,
            command=command,
            watch_command=watch_command,
            output_paths=output_paths,
            output_relpaths=output_relpaths,
            source_paths=source_paths,
            workspace_source_paths=workspace_source_paths,
            workspace_source_labels=workspace_source_labels,
            lockfile_paths=lockfile_paths,
            lockfile_labels=lockfile_labels,
            stamp_path=stamp_path,
            failure_path=failure_path,
            warnings=warnings,
        ),
        [],
    )


def build_expected_stamp(config: FrontendBuildConfig) -> dict[str, Any]:
    return {
        **build_expected_stamp_metadata(config),
        "lockfile_hash": hash_labeled_paths(
            config.lockfile_paths,
            config.lockfile_labels,
        ),
        "source_hash": _hash_build_sources(config),
    }


def build_expected_stamp_metadata(
    config: FrontendBuildConfig,
) -> dict[str, Any]:
    """Returns the stamp fields that can be checked without reading sources."""
    return {
        "schema_version": STAMP_SCHEMA_VERSION,
        "addon_id": config.addon_id,
        "addon_version": config.addon_version,
        "frontend_api_range": config.frontend_api_range,
        "build_command": _command_value(config.command),
        "source_paths": [
            *[
                relpath(path, config.addon_dir)
                for path in config.source_paths
            ],
            *config.workspace_source_labels,
        ],
        "lockfile_paths": list(config.lockfile_labels),
        "output_paths": list(config.output_relpaths),
    }


def hash_paths(paths: list[Path], base_dir: Path) -> str:
    digest = hashlib.sha256()
    base_dir = base_dir.resolve()
    for path in sorted((Path(path) for path in paths), key=lambda item: relpath(item, base_dir)):
        _hash_path(digest, path, base_dir)
    return digest.hexdigest()


def _hash_build_sources(config: FrontendBuildConfig) -> str:
    addon_hash = hash_paths(config.source_paths, config.addon_dir)
    if not config.workspace_source_paths:
        return addon_hash
    digest = hashlib.sha256()
    digest.update(b"@addon\0")
    digest.update(addon_hash.encode("ascii"))
    digest.update(b"@workspace\0")
    digest.update(
        hash_labeled_paths(
            config.workspace_source_paths,
            config.workspace_source_labels,
        ).encode("ascii")
    )
    return digest.hexdigest()


def hash_labeled_paths(paths: list[Path], labels: list[str]) -> str:
    if len(paths) != len(labels):
        raise ValueError("Path and label counts must match.")

    digest = hashlib.sha256()
    entries = sorted(zip(labels, paths), key=lambda entry: entry[0])
    for label, path in entries:
        digest.update(label.encode("utf-8"))
        if not path.exists():
            digest.update(b"\0MISSING")
        elif path.is_file():
            _hash_file(digest, path)
        else:
            _hash_path(digest, path, path)
    return digest.hexdigest()


def relpath(path: Path, base_dir: Path) -> str:
    """Return a portable label without repeating physical path resolution.

    Callers resolve and contain build paths while constructing the config. This
    helper is used only for display and stamp labels, so lexical normalization
    avoids an expensive Windows filesystem round-trip for every label.
    """

    normalized_path = Path(os.path.abspath(path))
    normalized_base = Path(os.path.abspath(base_dir))
    try:
        return normalized_path.relative_to(normalized_base).as_posix()
    except ValueError:
        return normalized_path.as_posix()


def command_display(command: str | list[str] | None) -> str | None:
    if command is None:
        return None
    if isinstance(command, str):
        return command
    return " ".join(str(part) for part in command)


def _hash_path(digest: "hashlib._Hash", path: Path, base_dir: Path) -> None:
    path = Path(path)
    digest.update(relpath(path, base_dir).encode("utf-8"))
    if not path.exists():
        digest.update(b"\0MISSING")
        return
    if path.is_file():
        _hash_file(digest, path)
        return
    if path.is_dir():
        digest.update(b"\0DIR")

        for root, dirnames, filenames in os.walk(path):
            dirnames[:] = sorted(dirname for dirname in dirnames if dirname not in EXCLUDED_SOURCE_DIRS)
            current_dir = Path(root)
            for dirname in dirnames:
                child_dir = current_dir / dirname
                digest.update(relpath(child_dir, base_dir).encode("utf-8"))
                digest.update(b"\0DIR")
            for filename in sorted(filenames):
                child_file = current_dir / filename
                digest.update(relpath(child_file, base_dir).encode("utf-8"))
                _hash_file(digest, child_file)


def _hash_file(digest: "hashlib._Hash", path: Path) -> None:
    digest.update(b"\0FILE")
    digest.update(_cached_file_bytes(path))


def _cached_file_bytes(path: Path) -> bytes:
    """Reads source bytes once per process and reuses them while stat data matches."""
    global _FILE_CONTENT_CACHE_BYTES
    global _FILE_CONTENT_CACHE_HITS
    global _FILE_CONTENT_CACHE_MISSES

    resolved = Path(path).resolve()
    stat = resolved.stat()
    key = (str(resolved), int(stat.st_mtime_ns), int(stat.st_size))
    with _FILE_CONTENT_CACHE_LOCK:
        cached = _FILE_CONTENT_CACHE.get(key)
        if cached is not None:
            _FILE_CONTENT_CACHE.move_to_end(key)
            _FILE_CONTENT_CACHE_HITS += 1
            return cached

    data = resolved.read_bytes()
    with _FILE_CONTENT_CACHE_LOCK:
        _FILE_CONTENT_CACHE_MISSES += 1
        if len(data) <= _FILE_CONTENT_CACHE_MAX_BYTES:
            stale_keys = [
                existing_key
                for existing_key in _FILE_CONTENT_CACHE
                if existing_key[0] == key[0] and existing_key != key
            ]
            for stale_key in stale_keys:
                _FILE_CONTENT_CACHE_BYTES -= len(
                    _FILE_CONTENT_CACHE.pop(stale_key)
                )
            previous = _FILE_CONTENT_CACHE.pop(key, None)
            if previous is not None:
                _FILE_CONTENT_CACHE_BYTES -= len(previous)
            _FILE_CONTENT_CACHE[key] = data
            _FILE_CONTENT_CACHE_BYTES += len(data)
            while (
                _FILE_CONTENT_CACHE
                and _FILE_CONTENT_CACHE_BYTES
                > _FILE_CONTENT_CACHE_MAX_BYTES
            ):
                _old_key, old_data = _FILE_CONTENT_CACHE.popitem(last=False)
                _FILE_CONTENT_CACHE_BYTES -= len(old_data)
    return data


def clear_frontend_file_cache() -> None:
    """Clears the bounded source-byte cache used by explicit build validation."""
    global _FILE_CONTENT_CACHE_BYTES
    global _FILE_CONTENT_CACHE_HITS
    global _FILE_CONTENT_CACHE_MISSES
    with _FILE_CONTENT_CACHE_LOCK:
        _FILE_CONTENT_CACHE.clear()
        _FILE_CONTENT_CACHE_BYTES = 0
        _FILE_CONTENT_CACHE_HITS = 0
        _FILE_CONTENT_CACHE_MISSES = 0


def frontend_file_cache_info() -> dict[str, int]:
    with _FILE_CONTENT_CACHE_LOCK:
        return {
            "entries": len(_FILE_CONTENT_CACHE),
            "bytes": _FILE_CONTENT_CACHE_BYTES,
            "hits": _FILE_CONTENT_CACHE_HITS,
            "misses": _FILE_CONTENT_CACHE_MISSES,
        }


def _resolve_under(base_dir: Path, raw_path: Any, label: str, errors: list[str]) -> Path:
    raw = str(raw_path or "").strip()
    if not raw:
        raw = "."
    candidate = (base_dir / raw).resolve()
    try:
        candidate.relative_to(base_dir)
    except ValueError:
        errors.append(f"frontend.build.{label} escapes the addon directory: {raw}")
    return candidate


def _resolve_output_paths(
    addon_dir: Path,
    static_dir: Path,
    build: dict[str, Any],
    errors: list[str],
) -> tuple[list[Path], list[str]]:
    raw_outputs = build.get("outputs") or build.get("output_paths") or build.get("output")
    raw_values = _normalize_string_list(raw_outputs)
    if not raw_values:
        errors.append("frontend.build.outputs must declare at least one output path under addon static/.")
        return [], []

    output_paths: list[Path] = []
    output_relpaths: list[str] = []
    for raw in raw_values:
        path = Path(raw)
        if path.is_absolute():
            errors.append(f"frontend.build output path must be relative: {raw}")
            continue
        normalized = raw.replace("\\", "/")
        candidate = (addon_dir / normalized).resolve() if normalized == "static" or normalized.startswith("static/") else (static_dir / normalized).resolve()
        try:
            candidate.relative_to(static_dir)
        except ValueError:
            errors.append(f"frontend.build output path must stay under addon static/: {raw}")
            continue
        output_paths.append(candidate)
        output_relpaths.append(relpath(candidate, addon_dir))
    return output_paths, output_relpaths


def _resolve_output_control_path(
    addon_dir: Path,
    static_dir: Path,
    raw_path: Any,
    label: str,
    errors: list[str],
) -> Path:
    raw = str(raw_path or "").strip() or DEFAULT_STAMP_NAME
    path = Path(raw)
    if path.is_absolute():
        errors.append(f"frontend.build.{label} must be relative.")
        return static_dir / DEFAULT_STAMP_NAME
    normalized = raw.replace("\\", "/")
    candidate = (addon_dir / normalized).resolve() if normalized == "static" or normalized.startswith("static/") else (static_dir / normalized).resolve()
    try:
        candidate.relative_to(static_dir)
    except ValueError:
        errors.append(f"frontend.build.{label} must stay under addon static/.")
    return candidate


def _resolve_source_paths(addon_dir: Path, source_dir: Path, build: dict[str, Any], errors: list[str]) -> list[Path]:
    declared = build.get("source_paths") or build.get("sources")
    if declared:
        return [_resolve_under(addon_dir, raw, "source_paths", errors) for raw in _normalize_string_list(declared)]
    return [source_dir]


def _resolve_workspace_source_paths(
    addon_dir: Path,
    source_dir: Path,
    build: dict[str, Any],
    errors: list[str],
) -> tuple[list[Path], list[str]]:
    declared = (
        build.get("workspace_source_paths")
        or build.get("workspace_sources")
    )
    if not declared:
        return [], []

    workspace_root = _find_workspace_root(addon_dir)
    if workspace_root is None:
        if source_dir.is_dir():
            errors.append(
                "frontend.build.workspace_source_paths requires an ancestor "
                "repository with package.json and pixi.toml."
            )
        return [], []

    paths: list[Path] = []
    labels: list[str] = []
    for raw in _normalize_string_list(declared):
        path = Path(raw)
        if path.is_absolute():
            errors.append(
                "frontend.build.workspace_source_paths paths must be relative."
            )
            continue
        candidate = (workspace_root / path).resolve()
        try:
            relative = candidate.relative_to(workspace_root).as_posix()
        except ValueError:
            errors.append(
                "frontend.build.workspace_source_paths path escapes the "
                f"repository workspace: {raw}"
            )
            continue
        paths.append(candidate)
        labels.append(f"@workspace/{relative}")
    return paths, labels


def _resolve_lockfile_paths(
    addon_dir: Path,
    source_dir: Path,
    build: dict[str, Any],
    errors: list[str],
) -> tuple[list[Path], list[str]]:
    paths: list[Path] = []
    labels: list[str] = []
    declared = build.get("lockfiles") or build.get("lockfile")
    if declared:
        source_prefix = source_dir.name.replace("\\", "/") + "/"
        for raw in _normalize_string_list(declared):
            normalized = raw.replace("\\", "/")
            base = addon_dir if normalized.startswith(source_prefix) else source_dir
            candidate = (base / normalized).resolve()
            try:
                candidate.relative_to(addon_dir)
            except ValueError:
                errors.append(f"frontend.build.lockfiles path escapes the addon directory: {raw}")
            paths.append(candidate)
            labels.append(relpath(candidate, addon_dir))
    else:
        for name in DEFAULT_LOCKFILES:
            candidate = (source_dir / name).resolve()
            if candidate.exists():
                paths.append(candidate)
                labels.append(relpath(candidate, addon_dir))

    workspace_declared = (
        build.get("workspace_lockfiles")
        or build.get("workspace_lockfile")
    )
    if workspace_declared:
        workspace_root = _find_workspace_root(addon_dir)
        if workspace_root is None:
            if source_dir.is_dir():
                errors.append(
                    "frontend.build.workspace_lockfiles requires an ancestor "
                    "repository with package.json and pixi.toml."
                )
        else:
            for raw in _normalize_string_list(workspace_declared):
                path = Path(raw)
                if path.is_absolute():
                    errors.append(
                        "frontend.build.workspace_lockfiles paths must be relative."
                    )
                    continue
                candidate = (workspace_root / path).resolve()
                try:
                    relative = candidate.relative_to(workspace_root).as_posix()
                except ValueError:
                    errors.append(
                        "frontend.build.workspace_lockfiles path escapes the "
                        f"repository workspace: {raw}"
                    )
                    continue
                paths.append(candidate)
                labels.append(f"@workspace/{relative}")

    return paths, labels


def _find_workspace_root(addon_dir: Path) -> Path | None:
    for candidate in (addon_dir, *addon_dir.parents):
        if (
            (candidate / "package.json").is_file()
            and (candidate / "pixi.toml").is_file()
        ):
            return candidate.resolve()
    return None


def _normalize_command(value: Any) -> tuple[str | list[str] | None, list[str]]:
    if value is None or value == "":
        return None, []
    if isinstance(value, str):
        return value, []
    if isinstance(value, list) and all(isinstance(item, (str, int, float)) for item in value):
        return [str(item) for item in value], []
    return None, ["frontend.build command values must be strings or lists of command arguments."]


def _prepare_subprocess_command(
    command: str | list[str],
) -> tuple[list[str], bool]:
    if isinstance(command, str):
        command = shlex.split(command, posix=True)
    if not command:
        return list(command), False

    prepared = list(command)
    resolved_executable = shutil.which(prepared[0])
    if not resolved_executable:
        return prepared, False

    if os.name == "nt" and os.path.splitext(resolved_executable)[1].lower() in {
        ".bat",
        ".cmd",
    }:
        # Windows cannot execute batch launchers (notably npm.cmd) directly.
        # Invoke the command processor explicitly while keeping shell=False so
        # Python never implicitly interprets a manifest string as shell code.
        command_processor = os.environ.get("COMSPEC") or shutil.which(
            "cmd.exe"
        ) or "cmd.exe"
        return [
            command_processor,
            "/d",
            "/s",
            "/c",
            "call",
            resolved_executable,
            *prepared[1:],
        ], False

    prepared[0] = resolved_executable
    return prepared, False


def _normalize_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if item]
    return [str(value)]


def _command_value(command: str | list[str] | None) -> str | list[str] | None:
    if isinstance(command, list):
        return list(command)
    return command


def _stamp_matches(expected: dict[str, Any], existing: dict[str, Any] | None) -> bool:
    if not existing:
        return False
    return all(existing.get(key) == expected.get(key) for key in STAMP_COMPARE_KEYS)


def _stamp_metadata_matches(
    expected: dict[str, Any],
    existing: dict[str, Any] | None,
) -> bool:
    if not existing:
        return False
    return all(
        existing.get(key) == expected.get(key)
        for key in STAMP_METADATA_COMPARE_KEYS
    )


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _write_failure_marker(
    manifest: dict[str, Any],
    addon_dir: Path,
    message: str,
    started_at: str | None = None,
    stdout: str = "",
    stderr: str = "",
    returncode: int | None = None,
) -> None:
    config, errors = load_frontend_build_config(manifest, addon_dir)
    if errors or not config:
        return
    config.failure_path.parent.mkdir(parents=True, exist_ok=True)
    failure = {
        "addon_id": config.addon_id,
        "error": message,
        "returncode": returncode,
        "started_at": started_at,
        "failed_at": utc_iso(),
        "stdout_tail": stdout[-4000:],
        "stderr_tail": stderr[-4000:],
    }
    config.failure_path.write_text(json.dumps(failure, indent=2, sort_keys=True), encoding="utf-8")


def _failed_result(
    manifest: dict[str, Any],
    addon_dir: Path,
    message: str,
    status: dict[str, Any],
    errors: list[str] | None = None,
    started_at: str | None = None,
    stdout: str = "",
    stderr: str = "",
    returncode: int | None = None,
) -> dict[str, Any]:
    _write_failure_marker(manifest, addon_dir, message, started_at, stdout, stderr, returncode)
    updated_status = inspect_frontend_build(manifest, addon_dir)
    if errors and updated_status.get("status") == STATUS_MISCONFIGURED:
        updated_status["reasons"] = errors
    return {
        "success": False,
        "message": message,
        "status": updated_status if updated_status.get("declared") else status,
        "stdout": stdout,
        "stderr": stderr,
        "returncode": returncode,
    }


def _missing_executable_warning(command: str | list[str]) -> str | None:
    if isinstance(command, list):
        executable = command[0] if command else None
    else:
        executable = command.strip().split(maxsplit=1)[0] if command.strip() else None
    if not executable:
        return None
    if os.path.sep in executable or (os.path.altsep and os.path.altsep in executable):
        return None
    if shutil.which(executable):
        return None
    return f"Frontend build executable is not on PATH: {executable}"
