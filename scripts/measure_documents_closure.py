"""Collect content-free source, wheel, lock, runtime, and storage metrics."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import struct
import sys
import threading
import zipfile
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Final, NoReturn, TypedDict, cast

if TYPE_CHECKING:
    from flask import Flask

PROJECT_ROOT: Final = Path(__file__).resolve().parents[1]
if not __package__ and os.fspath(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, os.fspath(PROJECT_ROOT))

if TYPE_CHECKING or not __package__:
    from scripts.documents_dependency_inventory import (
        load_pixi_lock,
        load_requirements_lock,
    )
    from scripts.documents_source_inventory import documents_backend_members
    from scripts.generate_documents_inventory import documents_lock_contexts
else:
    from .documents_dependency_inventory import (
        load_pixi_lock,
        load_requirements_lock,
    )
    from .documents_source_inventory import documents_backend_members
    from .generate_documents_inventory import documents_lock_contexts

_ADDONS: Final = ("doc_processor", "sentence_segmenter", "translator")
_EXTENSION_NAMES: Final = (
    "carlina.compass_client_auth_service",
    "carlina.compass_client_principal_resolver",
    "carlina.compass_client_token_limiter",
)
_RUNTIME_FIELDS: Final = frozenset(
    {
        "module_count",
        "host_modules",
        "measurement_scope",
        "scheduler_job_ids",
        "active_thread_count",
        "extensions",
    }
)
_INPUT_LIMIT: Final = 512 * 1024 * 1024
_INVALID_INPUT: Final = "Documents closure inventory input is invalid."
_INVALID_OUTPUT: Final = "Documents closure output must be a new regular file."


class DirectoryMetrics(TypedDict):
    """Metadata totals; allocated bytes may be unavailable on this platform."""

    logical_bytes: int
    allocated_bytes: int | None
    file_count: int
    directory_count: int
    symlink_count: int
    other_entry_count: int


class _ArgumentError(ValueError):
    pass


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        del message
        raise _ArgumentError from None


def _read_hash(path: Path, *, limit: int | None = None) -> tuple[int, str]:
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or (
        limit is not None and before.st_size > limit
    ):
        raise ValueError(_INVALID_INPUT)
    digest = hashlib.sha256()
    length = 0
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (
            before.st_dev,
            before.st_ino,
        ):
            raise ValueError(_INVALID_INPUT)
        while chunk := stream.read(1024 * 1024):
            length += len(chunk)
            if limit is not None and length > limit:
                raise ValueError(_INVALID_INPUT)
            digest.update(chunk)
        if length != opened.st_size:
            raise ValueError(_INVALID_INPUT)
    return opened.st_size, digest.hexdigest()


def _source_owner(member: str) -> str:
    if member.startswith("carlina/"):
        return "carlina"
    parts = member.split("/")
    if len(parts) > 2 and parts[:2] == ["iarf_assistant", "addons"]:
        addon = parts[2]
        if addon in _ADDONS:
            return f"iarf_assistant.addons.{addon}"
    return "iarf_assistant.host"


def _module_name(member: str) -> str | None:
    if not member.endswith(".py"):
        return None
    parts = member[:-3].split("/")
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts) if parts else None


def source_metrics(root: Path) -> dict[str, object]:
    """Hash the selected source members and summarize their owning modules."""
    members = tuple(sorted(documents_backend_members(root)))
    group_names = (
        "carlina",
        "iarf_assistant.host",
        *(f"iarf_assistant.addons.{addon}" for addon in _ADDONS),
    )
    counts = dict.fromkeys(group_names, 0)
    sizes = dict.fromkeys(group_names, 0)
    modules = {name: set[str]() for name in group_names}
    group_members: dict[str, list[str]] = {name: [] for name in group_names}
    records: list[tuple[str, str]] = []
    byte_count = 0
    for member in members:
        size, file_hash = _read_hash(root / member)
        owner = _source_owner(member)
        counts[owner] += 1
        sizes[owner] += size
        group_members[owner].append(member)
        byte_count += size
        module_name = _module_name(member)
        if module_name is not None:
            modules[owner].add(module_name)
        records.append((member, file_hash))
    canonical = json.dumps(records, ensure_ascii=True, separators=(",", ":"))
    groups = [
        {
            "name": name,
            "member_count": counts[name],
            "bytes": sizes[name],
            "modules": sorted(modules[name]),
            "members": sorted(group_members[name]),
        }
        for name in sorted(group_names)
    ]
    return {
        "member_count": len(members),
        "bytes": byte_count,
        "sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "groups": groups,
    }


def _normalized_wheel_name(name: str, *, directory: bool) -> str:
    if not name or "\x00" in name or "\\" in name or name.startswith("/"):
        raise ValueError(_INVALID_INPUT)
    normalized = name[:-1] if directory and name.endswith("/") else name
    parts = normalized.split("/")
    if (
        not normalized
        or any(part in {"", ".", ".."} or ":" in part for part in parts)
        or PurePosixPath(normalized).as_posix() != normalized
        or any(ord(character) < 32 for character in normalized)
    ):
        raise ValueError(_INVALID_INPUT)
    return normalized


def wheel_metrics(path: Path) -> dict[str, object]:
    """Read wheel central-directory metadata without extracting or executing it."""
    wheel_bytes, wheel_hash = _read_hash(path, limit=_INPUT_LIMIT)
    package_groups: dict[str, dict[str, int]] = {}
    seen_names: set[str] = set()
    file_count = 0
    uncompressed_bytes = 0
    compressed_bytes = 0
    metadata_count = 0
    record_count = 0
    with zipfile.ZipFile(path, "r") as archive:
        for info in archive.infolist():
            name = _normalized_wheel_name(info.filename, directory=info.is_dir())
            if name in seen_names:
                raise ValueError(_INVALID_INPUT)
            seen_names.add(name)
            if info.is_dir():
                continue
            if info.file_size < 0 or info.compress_size < 0:
                raise ValueError(_INVALID_INPUT)
            file_count += 1
            uncompressed_bytes += info.file_size
            compressed_bytes += info.compress_size
            if uncompressed_bytes > _INPUT_LIMIT:
                raise ValueError(_INVALID_INPUT)
            package = name.split("/", 1)[0] if "/" in name else "<root>"
            metrics = package_groups.setdefault(
                package,
                {"file_count": 0, "uncompressed_bytes": 0, "compressed_bytes": 0},
            )
            metrics["file_count"] += 1
            metrics["uncompressed_bytes"] += info.file_size
            metrics["compressed_bytes"] += info.compress_size
            parts = name.split("/")
            if len(parts) >= 2 and parts[-2].endswith(".dist-info"):
                metadata_count += parts[-1] == "METADATA"
                record_count += parts[-1] == "RECORD"
    return {
        "file_sha256": wheel_hash,
        "file_bytes": wheel_bytes,
        "file_count": file_count,
        "uncompressed_bytes": uncompressed_bytes,
        "compressed_bytes": compressed_bytes,
        "metadata_count": metadata_count,
        "record_count": record_count,
        "groups": [
            {"name": name, **package_groups[name]} for name in sorted(package_groups)
        ],
    }


def locked_dependencies(root: Path, platform: str) -> dict[str, object]:
    """Summarize only identities from the declared Documents lock contexts."""
    contexts: list[dict[str, object]] = []
    for context, relative, lock_format, environment in documents_lock_contexts(root):
        lock_path = root / relative
        if lock_format == "pixi" and environment is not None:
            lock = load_pixi_lock(lock_path, environment=environment, platform=platform)
        elif lock_format == "pip_requirements":
            lock = load_requirements_lock(lock_path)
        else:
            raise ValueError(_INVALID_INPUT)
        packages = [
            {
                "name": package.name,
                "version": package.version,
                "ecosystem": package.ecosystem,
            }
            for package in lock.packages
        ]
        contexts.append(
            {
                "owner": context.split(":", 1)[0],
                "context": context,
                "lock_sha256": lock.lock_sha256,
                "package_count": len(lock.packages),
                "packages": packages,
            }
        )
    return {"platform": platform, "contexts": contexts}


def _walk_error(_error: OSError) -> NoReturn:
    raise ValueError(_INVALID_INPUT) from None


def directory_metrics(root: Path) -> DirectoryMetrics:
    """Sum per-entry stat sizes, including dirs/links; never follow symlinks.

    Hard links are counted per path, so neither total proves exclusive disk use.
    """
    root_status = root.lstat()
    if not stat.S_ISDIR(root_status.st_mode):
        raise ValueError(_INVALID_INPUT)
    logical_bytes = 0
    allocated_bytes = 0
    allocated_available = True
    file_count = 0
    directory_count = 0
    symlink_count = 0
    other_entry_count = 0

    def account(status: os.stat_result) -> None:
        nonlocal logical_bytes, allocated_bytes, allocated_available
        logical_bytes += status.st_size
        blocks = getattr(status, "st_blocks", None)
        if blocks is None:
            allocated_available = False
        elif allocated_available:
            allocated_bytes += blocks * 512

    account(root_status)
    directory_count = 1
    for current, directories, files in os.walk(
        root, topdown=True, onerror=_walk_error, followlinks=False
    ):
        current_path = Path(current)
        retained_directories: list[str] = []
        for name in directories:
            status = (current_path / name).lstat()
            account(status)
            if stat.S_ISLNK(status.st_mode):
                symlink_count += 1
            elif stat.S_ISDIR(status.st_mode):
                directory_count += 1
                retained_directories.append(name)
            else:
                other_entry_count += 1
        directories[:] = retained_directories
        for name in files:
            status = (current_path / name).lstat()
            account(status)
            if stat.S_ISLNK(status.st_mode):
                symlink_count += 1
            elif stat.S_ISREG(status.st_mode):
                file_count += 1
            else:
                other_entry_count += 1
    return {
        "logical_bytes": logical_bytes,
        "allocated_bytes": allocated_bytes if allocated_available else None,
        "file_count": file_count,
        "directory_count": directory_count,
        "symlink_count": symlink_count,
        "other_entry_count": other_entry_count,
    }


def runtime_snapshot(app: Flask) -> dict[str, object]:
    """Capture module names, scheduler IDs, thread count, and extension presence."""
    module_names = tuple(sorted(sys.modules))
    host_modules = sorted(
        name
        for name in module_names
        if name.startswith(("carlina.", "iarf_assistant."))
    )
    scheduler = getattr(app, "scheduler", None)
    scheduler_job_ids: list[str] | None = None
    if scheduler is not None:
        getter = getattr(scheduler, "get_jobs", None)
        if not callable(getter):
            raise ValueError(_INVALID_INPUT)
        jobs = cast(list[object], getter())
        scheduler_job_ids = sorted(
            identifier
            for job in jobs
            if isinstance((identifier := getattr(job, "id", None)), str)
        )
    extension_keys = app.extensions
    return {
        "module_count": len(module_names),
        "host_modules": host_modules,
        "measurement_scope": "current process including collector imports",
        "scheduler_job_ids": scheduler_job_ids,
        "active_thread_count": threading.active_count(),
        "extensions": {
            name: name in extension_keys for name in sorted(_EXTENSION_NAMES)
        },
    }


def _parse_tree(value: str) -> tuple[str, Path]:
    label, separator, raw_path = value.partition("=")
    if (
        not separator
        or not label
        or not raw_path
        or any(character in label for character in "/\\:")
        or any(ord(character) < 32 for character in label)
    ):
        raise _ArgumentError from None
    return label, Path(raw_path)


def _runtime_json(path: Path) -> dict[str, object]:
    status = path.lstat()
    if not stat.S_ISREG(status.st_mode) or status.st_size > 1024 * 1024:
        raise ValueError(_INVALID_INPUT)
    value: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(_INVALID_INPUT)
    snapshot = cast(dict[str, object], value)
    if set(snapshot) != set(_RUNTIME_FIELDS):
        raise ValueError(_INVALID_INPUT)
    for field in ("module_count", "active_thread_count"):
        if type(snapshot[field]) is not int or cast(int, snapshot[field]) < 0:
            raise ValueError(_INVALID_INPUT)
    if snapshot["measurement_scope"] != "current process including collector imports":
        raise ValueError(_INVALID_INPUT)
    extensions = snapshot["extensions"]
    if not isinstance(extensions, dict):
        raise ValueError(_INVALID_INPUT)
    flags = cast(dict[object, object], extensions)
    if set(flags) != set(_EXTENSION_NAMES) or any(
        type(flag) is not bool for flag in flags.values()
    ):
        raise ValueError(_INVALID_INPUT)
    modules = snapshot["host_modules"]
    if not isinstance(modules, list):
        raise ValueError(_INVALID_INPUT)
    if any(
        not isinstance(name, str)
        or not name.startswith(("carlina.", "iarf_assistant."))
        or not all(part.isidentifier() for part in name.split("."))
        for name in cast(list[object], modules)
    ):
        raise ValueError(_INVALID_INPUT)
    jobs = snapshot["scheduler_job_ids"]
    if jobs is not None and (
        not isinstance(jobs, list)
        or any(
            not isinstance(name, str) or not name.isprintable()
            for name in cast(list[object], jobs)
        )
    ):
        raise ValueError(_INVALID_INPUT)
    return snapshot


def _parser() -> _ArgumentParser:
    parser = _ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--platform", default="linux-64")
    parser.add_argument("--wheel", type=Path)
    parser.add_argument("--runtime-snapshot", type=Path)
    parser.add_argument("--tree", action="append", default=[], metavar="LABEL=PATH")
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        trees = [_parse_tree(value) for value in args.tree]
        if len({label for label, _ in trees}) != len(trees):
            raise _ArgumentError
        result: dict[str, object] = {
            "schema_version": 1,
            "source": source_metrics(args.root),
            "locks": locked_dependencies(args.root, args.platform),
            "shipped_wheel": wheel_metrics(args.wheel) if args.wheel else None,
            "imported_runtime": (
                _runtime_json(args.runtime_snapshot) if args.runtime_snapshot else None
            ),
            "storage": {
                "trees": [
                    {"label": label, **directory_metrics(path)}
                    for label, path in sorted(trees)
                ]
                if trees
                else None,
                "peak_install_bytes": None,
                "peak_install_bytes_note": "Installer peak is not measured by this snapshot.",
                "allocated_bytes_note": "Allocated bytes are not reclaimed-space or exclusive-use proof.",
                "tree_bytes_note": "Sizes include directory/link metadata; hard links are counted per path.",
            },
        }
    except _ArgumentError:
        print("Documents closure inventory arguments are invalid.", file=sys.stderr)
        return 2
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        zipfile.BadZipFile,
        EOFError,
        RuntimeError,
        NotImplementedError,
        struct.error,
    ):
        print(_INVALID_INPUT, file=sys.stderr)
        return 2

    try:
        with args.output.open("x", encoding="utf-8") as output:
            if not stat.S_ISREG(os.fstat(output.fileno()).st_mode):
                raise OSError
            json.dump(result, output, ensure_ascii=True, indent=2, sort_keys=True)
            output.write("\n")
    except OSError:
        print(_INVALID_OUTPUT, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
