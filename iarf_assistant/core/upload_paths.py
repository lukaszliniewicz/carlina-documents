from __future__ import annotations

import os
import platform
import re
from os import PathLike
from pathlib import Path, PurePosixPath, PureWindowsPath

_WSL_DRIVE_PATH = re.compile(r"^/mnt/([a-zA-Z])(?:/(.*))?$")


def uploads_root(
    app_root_path: str | PathLike[str] | None = None,
    *,
    uploads_dir: str | PathLike[str] | None = None,
) -> Path:
    """Return the application's canonical uploads directory."""
    if uploads_dir is not None:
        return Path(uploads_dir).expanduser().resolve()

    if app_root_path is None:
        try:
            from flask import current_app, has_app_context

            if has_app_context():
                configured = current_app.config.get("CARLINA_UPLOADS_DIR")
                if configured:
                    return Path(configured).expanduser().resolve()
                paths = current_app.extensions.get("carlina_paths")
                if paths is not None and hasattr(paths, "uploads_dir"):
                    return Path(paths.uploads_dir).expanduser().resolve()
                app_root_path = current_app.root_path
        except (ImportError, RuntimeError):
            app_root_path = None

    if app_root_path is None:
        # Workers and command-line consumers may run without a Flask context.
        # Resolve the same platform/portable/legacy contract as the app factory
        # instead of falling back to the installed Python package directory.
        from iarf_assistant.core.app_paths import resolve_app_paths

        return resolve_app_paths().uploads_dir
    return (Path(app_root_path).expanduser() / "uploads").resolve()


def _upload_relative_path(reference: str) -> str | None:
    """Return the part after ``uploads/`` for stored web-path variants."""
    normalized = reference.replace("\\", "/")

    # Do not reinterpret a UNC path whose server happens to be named "uploads".
    if normalized.startswith("//"):
        return None

    lowered = normalized.casefold()
    for prefix in ("/uploads/", "uploads/"):
        if lowered.startswith(prefix):
            return normalized[len(prefix) :]
    if lowered in {"/uploads", "uploads"}:
        return ""
    return None


def is_upload_reference(reference: str | PathLike[str] | None) -> bool:
    if reference is None:
        return False
    raw = os.fspath(reference).strip()
    return bool(raw) and _upload_relative_path(raw) is not None


def _ensure_within(path: Path, root: Path, reference: str) -> Path:
    resolved = path.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"Upload path escapes the uploads directory: {reference}") from exc
    return resolved


def _windows_absolute_path_on_wsl(path: PureWindowsPath) -> PurePosixPath:
    if path.drive.startswith("\\\\") or len(path.drive) != 2 or path.drive[1] != ":":
        raise ValueError(f"Windows path is not mapped into WSL: {path}")
    return PurePosixPath("/mnt") / path.drive[0].lower() / PurePosixPath(*path.parts[1:])


def _is_wsl() -> bool:
    if os.name == "nt" or platform.system().casefold() != "linux":
        return False
    release = platform.release().casefold()
    return "microsoft" in release or bool(os.environ.get("WSL_DISTRO_NAME"))


def resolve_upload_reference(
    reference: str | PathLike[str] | None,
    app_root_path: str | PathLike[str] | None = None,
    *,
    uploads_dir: str | PathLike[str] | None = None,
    require_within_uploads: bool = False,
) -> Path | None:
    """Resolve a stored upload reference without confusing it with a Unix root path.

    ``/uploads/...``, ``uploads/...`` and their backslash variants are always
    resolved below the application uploads directory before native absolute-path
    rules are considered. Native Windows, WSL/Linux, and macOS absolute paths are
    preserved. Windows drive paths are mapped to ``/mnt/<drive>`` when running in
    WSL, and WSL drive paths are mapped back when running natively on Windows.
    Relative paths retain the application's historical uploads-relative meaning.
    """
    if reference is None:
        return None
    raw = os.fspath(reference).strip()
    if not raw:
        return None

    root = uploads_root(app_root_path, uploads_dir=uploads_dir)
    upload_relative = _upload_relative_path(raw)
    if upload_relative is not None:
        relative = PurePosixPath(upload_relative.replace("\\", "/"))
        return _ensure_within(root.joinpath(*relative.parts), root, raw)

    windows_path = PureWindowsPath(raw)
    if windows_path.is_absolute() and _is_wsl():
        resolved = Path(_windows_absolute_path_on_wsl(windows_path)).resolve()
    else:
        expanded = Path(raw).expanduser()
        if expanded.is_absolute():
            resolved = expanded.resolve()
        elif windows_path.is_absolute():
            raise ValueError(f"Windows absolute path is not accessible on this host: {raw}")
        else:
            normalized = raw.replace("\\", "/")
            posix_path = PurePosixPath(normalized)
            if posix_path.is_absolute():
                if os.name != "nt":
                    resolved = Path(normalized).resolve()
                else:
                    match = _WSL_DRIVE_PATH.fullmatch(normalized)
                    if not match:
                        raise ValueError(f"POSIX absolute path is not accessible on Windows: {raw}")
                    drive, tail = match.groups()
                    tail_parts = PurePosixPath(tail or "").parts
                    resolved = Path(f"{drive.upper()}:\\", *tail_parts).resolve()
            else:
                relative = PurePosixPath(normalized)
                resolved = _ensure_within(root.joinpath(*relative.parts), root, raw)

    if require_within_uploads:
        return _ensure_within(resolved, root, raw)
    return resolved


def upload_web_path(
    local_path: str | PathLike[str],
    app_root_path: str | PathLike[str] | None = None,
    *,
    uploads_dir: str | PathLike[str] | None = None,
) -> str:
    root = uploads_root(app_root_path, uploads_dir=uploads_dir)
    resolved = _ensure_within(Path(local_path), root, os.fspath(local_path))
    return "/uploads/" + resolved.relative_to(root).as_posix()
