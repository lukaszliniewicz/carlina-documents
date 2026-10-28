"""Fast, category-aware storage accounting for Carlina-owned paths."""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from pathlib import Path

from iarf_assistant.core.app_paths import AppPaths


@dataclass(frozen=True, slots=True)
class StorageCategoryReport:
    category: str
    path: str
    exists: bool
    files: int
    bytes: int
    symlinks_skipped: int


@dataclass(frozen=True, slots=True)
class StorageReport:
    mode: str
    data_root: str
    categories: tuple[StorageCategoryReport, ...]
    total_files: int
    total_bytes: int

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _measure(category: str, root: Path) -> StorageCategoryReport:
    if not root.exists():
        return StorageCategoryReport(category, str(root), False, 0, 0, 0)
    if root.is_symlink():
        return StorageCategoryReport(category, str(root), True, 0, 0, 1)
    if root.is_file():
        return StorageCategoryReport(
            category, str(root), True, 1, root.stat().st_size, 0
        )
    files = 0
    total = 0
    symlinks = 0
    for directory, directory_names, file_names in os.walk(root, followlinks=False):
        base = Path(directory)
        retained_directories: list[str] = []
        for name in directory_names:
            candidate = base / name
            if candidate.is_symlink():
                symlinks += 1
            else:
                retained_directories.append(name)
        directory_names[:] = retained_directories
        for name in file_names:
            candidate = base / name
            if candidate.is_symlink():
                symlinks += 1
                continue
            try:
                total += candidate.stat().st_size
                files += 1
            except OSError:
                continue
    return StorageCategoryReport(
        category,
        str(root),
        True,
        files,
        total,
        symlinks,
    )


def build_storage_report(
    paths: AppPaths,
    *,
    managed_recovery_root: Path | None = None,
) -> StorageReport:
    categories: tuple[tuple[str, Path], ...] = (
        ("database", paths.database_file),
        ("uploads", paths.uploads_dir),
        ("artifacts", paths.artifacts_dir),
        ("installed_addons", paths.installed_addons_dir),
        ("addon_data", paths.addon_data_dir),
        ("addon_environments", paths.addon_environments_dir),
        ("backups", paths.backups_dir),
        ("logs", paths.log_dir),
        ("cache", paths.cache_dir),
        ("state", paths.state_dir),
        ("staging", paths.staging_dir),
        ("temporary", paths.temp_dir),
    )
    if managed_recovery_root is not None:
        categories += (("managed_recovery", managed_recovery_root),)
    reports = tuple(_measure(name, path) for name, path in categories)
    return StorageReport(
        mode=paths.mode,
        data_root=str(paths.data_root),
        categories=reports,
        total_files=sum(item.files for item in reports),
        total_bytes=sum(item.bytes for item in reports),
    )


def format_storage_report(report: StorageReport) -> str:
    lines = [
        "Carlina storage report",
        f"Layout: {report.mode} ({report.data_root})",
        "",
    ]
    for category in report.categories:
        state = "missing" if not category.exists else _human_bytes(category.bytes)
        suffix = (
            f"; skipped {category.symlinks_skipped} link(s)"
            if category.symlinks_skipped
            else ""
        )
        lines.append(
            f"{category.category:20} {state:>12}  {category.files:>8} file(s){suffix}"
        )
    lines.extend(
        [
            "",
            f"Total: {_human_bytes(report.total_bytes)} in {report.total_files} file(s).",
        ]
    )
    return "\n".join(lines)


def _human_bytes(value: int) -> str:
    size = float(value)
    for suffix in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or suffix == "TiB":
            return f"{size:.1f} {suffix}"
        size /= 1024
    return f"{size:.1f} TiB"


__all__ = [
    "StorageCategoryReport",
    "StorageReport",
    "build_storage_report",
    "format_storage_report",
]
