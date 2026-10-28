"""Consistent, manifest-driven Carlina backup and staged restore services."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import uuid
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path, PurePosixPath
from typing import Any

from carlina.profile_binding import read_runtime_profile
from carlina.runtime_profiles import RuntimeProfileError
from iarf_assistant.core.app_paths import AppPaths
from iarf_assistant.core.encryption import InstallationKeyStore
from iarf_assistant.core.persistence.sqlite import sqlite_read_only_uri

BACKUP_FORMAT = "carlina.backup"
BACKUP_VERSION = 1
OVERLAY_BACKUP_VERSION = 2
MANIFEST_NAME = "manifest.json"
DEFAULT_MAX_FILES = 250_000
DEFAULT_MAX_EXPANDED_BYTES = 250 * 1024**3
BACKUP_STAGING_MINIMUM_OVERHEAD_BYTES = 64 * 1024**2
_ENVELOPE_KEY_PATTERN = re.compile(rb"enc:v2:(mk-[0-9a-f]{16}):")
_TASK_QUEUE_ADDON_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_UNSAFE_SQLITE_WAL_MESSAGE = "Unsafe SQLite WAL."
_EXCLUDED_DIRECTORY_NAMES = {
    ".cache",
    ".pixi",
    "__pycache__",
    "cache",
    "caches",
    "env",
    "environments",
    "logs",
    "node_modules",
    "tmp",
}
_OWNED_CATEGORIES = {
    "database",
    "uploads",
    "artifacts",
    "addons",
    "state",
    "doc_processor_exports",
    "translator_exports",
    "task_queues",
}
_V2_MANIFEST_KEYS = {
    "format",
    "version",
    "backup_id",
    "created_at",
    "carlina_version",
    "source_layout",
    "database",
    "encryption",
    "files",
    "file_count",
    "expanded_bytes",
    "storage_mode",
    "base",
    "changes",
}


class BackupError(RuntimeError):
    """Base class for backup/restore contract failures."""


class BackupIntegrityError(BackupError):
    """A backup is corrupt, incomplete, unsafe, or incompatible."""


class BackupKeyMismatch(BackupIntegrityError):
    """The current installation lacks keys required by a backup."""


@dataclass(frozen=True, slots=True)
class BackupRecord:
    path: Path
    backup_id: str
    created_at: str
    file_count: int
    expanded_bytes: int
    sha256: str
    required_key_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RestoreResult:
    destination_root: Path
    safety_root: Path | None
    manifest: dict[str, Any]


@dataclass(frozen=True, slots=True)
class RestoreActivationWitness:
    """A durable, journal-bound marker carried by an activated restore root.

    The target is deliberately relative to the restored data root.  A caller
    cannot use a witness to overwrite an arbitrary file next to the staging
    directory, and a collision is treated as an interrupted/ambiguous restore
    instead of silently replacing evidence.
    """

    relative_path: str
    payload: bytes


@dataclass(slots=True)
class _RootSwitchState:
    """Track whether an activation failure may have crossed a rename boundary."""

    started: bool = False
    rollback_completed: bool = False


def _now() -> datetime:
    return datetime.now(UTC)


def _package_version() -> str:
    try:
        return version("carlina")
    except PackageNotFoundError:
        return "0+unknown"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _database_integrity(path: Path) -> dict[str, Any]:
    connection = sqlite3.connect(sqlite_read_only_uri(path), uri=True)
    try:
        quick_rows = [
            str(row[0]) for row in connection.execute("PRAGMA quick_check").fetchall()
        ]
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
        schema_heads: list[str] = []
        table_exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='alembic_version'"
        ).fetchone()
        if table_exists:
            schema_heads = sorted(
                str(row[0])
                for row in connection.execute(
                    "SELECT version_num FROM alembic_version"
                ).fetchall()
            )
        journal_mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0])
    finally:
        connection.close()
    if quick_rows != ["ok"]:
        raise BackupIntegrityError(
            f"SQLite quick_check failed for {path}: {quick_rows[:5]}"
        )
    return {
        "quick_check": "ok",
        "foreign_key_violation_count": len(foreign_keys),
        "foreign_key_violation_sample": [list(row) for row in foreign_keys[:25]],
        "schema_heads": schema_heads,
        "journal_mode": journal_mode,
    }


def _snapshot_database(source: Path, destination: Path) -> dict[str, Any]:
    if not source.is_file():
        raise BackupError(f"Database file does not exist: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_connection = sqlite3.connect(
        sqlite_read_only_uri(source),
        uri=True,
        timeout=30,
    )
    target_connection = sqlite3.connect(str(destination), timeout=30)
    try:
        source_connection.backup(target_connection, pages=2048, sleep=0.05)
        target_connection.commit()
    finally:
        target_connection.close()
        source_connection.close()
    return _database_integrity(destination)


def inspect_database(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Validate a SQLite database and return its portable health metadata."""

    return _database_integrity(Path(path).expanduser().resolve())


def snapshot_database(
    source: str | os.PathLike[str],
    destination: str | os.PathLike[str],
) -> dict[str, Any]:
    """Create a consistent online SQLite snapshot and validate the result."""

    return _snapshot_database(
        Path(source).expanduser().resolve(),
        Path(destination).expanduser().resolve(),
    )


def scan_required_key_ids(path: Path) -> tuple[str, ...]:
    """Read a SQLite snapshot once and collect v2 key fingerprints."""

    found: set[str] = set()
    overlap = b""
    with path.open("rb") as handle:
        while chunk := handle.read(8 * 1024 * 1024):
            data = overlap + chunk
            found.update(
                match.group(1).decode("ascii")
                for match in _ENVELOPE_KEY_PATTERN.finditer(data)
            )
            overlap = data[-64:]
    return tuple(sorted(found))


def _archive_path(category: str, relative: PurePosixPath) -> str:
    return str(PurePosixPath("payload") / category / relative)


def _iter_owned_files(paths: AppPaths) -> Iterable[tuple[str, Path, PurePosixPath]]:
    categories = (
        ("uploads", paths.uploads_dir),
        ("artifacts", paths.artifacts_dir),
        ("addons", paths.addons_dir),
        ("state", paths.state_dir),
        ("doc_processor_exports", paths.instance_dir / "doc_processor_exports"),
        ("translator_exports", paths.instance_dir / "translator_exports"),
        ("task_queues", paths.instance_dir / "task_queues"),
    )
    seen: set[Path] = set()
    for category, root in categories:
        try:
            root_info = root.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(root_info.st_mode) or (
            getattr(root_info, "st_file_attributes", 0)
            & stat.FILE_ATTRIBUTE_REPARSE_POINT
        ):
            raise BackupError(
                f"Backup refuses symbolic links or reparse points in owned storage: {root}"
            )
        if not root.is_dir():
            continue
        for candidate in sorted(root.rglob("*")):
            candidate_info = candidate.lstat()
            if stat.S_ISLNK(candidate_info.st_mode) or (
                getattr(candidate_info, "st_file_attributes", 0)
                & stat.FILE_ATTRIBUTE_REPARSE_POINT
            ):
                raise BackupError(
                    f"Backup refuses symbolic links or reparse points in owned storage: {candidate}"
                )
            if not candidate.is_file():
                continue
            relative = candidate.relative_to(root)
            if category == "task_queues" and (
                len(relative.parts) != 2
                or not _TASK_QUEUE_ADDON_ID_PATTERN.fullmatch(relative.parts[0])
                or relative.parts[1] != "queue-v2.sqlite3"
            ):
                continue
            if (
                category == "state"
                and relative.as_posix() != "runtime/profile.json"
                and (
                    relative.as_posix()
                    in {
                        "pending-restore.json",
                        "storage-layout.json",
                    }
                    or relative.parts[0]
                    in {
                        "data-migrations",
                        "encryption-migrations",
                        "runtime",
                        "security",
                    }
                )
            ):
                continue
            if category != "task_queues" and any(
                part.casefold() in _EXCLUDED_DIRECTORY_NAMES for part in relative.parts
            ):
                continue
            resolved = candidate.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            yield category, candidate, PurePosixPath(*relative.parts)


def _sqlite_storage_bytes(source: Path) -> int:
    """Bound one SQLite snapshot by its database and committed WAL bytes."""

    total = source.stat().st_size
    wal = Path(f"{source}-wal")
    try:
        wal_info = wal.lstat()
    except FileNotFoundError:
        return total
    if not stat.S_ISREG(wal_info.st_mode) or (
        getattr(wal_info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    ):
        raise BackupError(_UNSAFE_SQLITE_WAL_MESSAGE)
    return total + wal_info.st_size


def estimate_backup_expanded_bytes(paths: AppPaths) -> int:
    """Conservatively estimate the uncompressed payload used by ``create_backup``.

    The update preflight uses the same ownership and exclusion rules as the
    backup itself, so large uploads or add-on state cannot be omitted from the
    rollback-space calculation.
    """

    total = paths.database_file.stat().st_size if paths.database_file.is_file() else 0
    return total + sum(
        _sqlite_storage_bytes(source)
        if category == "task_queues"
        else source.stat().st_size
        for category, source, _ in _iter_owned_files(paths)
    )


def _backup_staging_overhead(expanded_bytes: int) -> int:
    return max(BACKUP_STAGING_MINIMUM_OVERHEAD_BYTES, expanded_bytes // 100)


def _require_backup_destination_capacity(
    destination: Path,
    *,
    required_bytes: int,
) -> None:
    try:
        available_bytes = shutil.disk_usage(destination).free
    except OSError as exc:
        raise BackupError(
            f"Could not inspect free space on backup destination: {destination}"
        ) from exc
    if available_bytes < required_bytes:
        raise BackupError(
            "Backup destination lacks staging space: "
            f"{required_bytes} bytes required, {available_bytes} bytes available "
            f"at {destination}."
        )


def _logical_payload(
    paths: AppPaths,
    snapshot: Path,
) -> tuple[list[dict[str, Any]], dict[str, Path]]:
    """Return the complete logical table and current source for each member."""

    database_archive_path = "payload/database/carlina.db"
    files: list[dict[str, Any]] = [
        {
            "path": database_archive_path,
            "category": "database",
            "size": snapshot.stat().st_size,
            "sha256": _sha256_file(snapshot),
        }
    ]
    sources: dict[str, Path] = {database_archive_path: snapshot}
    for category, source, relative in _iter_owned_files(paths):
        archive_path = _archive_path(category, relative)
        archive_source = source
        if category == "task_queues":
            archive_source = snapshot.parent / "task_queues" / Path(*relative.parts)
            _snapshot_database(source, archive_source)
        files.append(
            {
                "path": archive_path,
                "category": category,
                "size": archive_source.stat().st_size,
                "sha256": _sha256_file(archive_source),
            }
        )
        sources[archive_path] = archive_source
    files.sort(key=lambda item: item["path"])
    if len(sources) != len(files):
        raise BackupError("Backup file mapping produced duplicate archive paths.")
    return files, sources


def _record_from_manifest(path: Path, manifest: dict[str, Any]) -> BackupRecord:
    """Build a public record only after a manifest has been verified."""

    encryption = manifest["encryption"]
    return BackupRecord(
        path=path,
        backup_id=str(manifest["backup_id"]),
        created_at=str(manifest["created_at"]),
        file_count=int(manifest["file_count"]),
        expanded_bytes=int(manifest["expanded_bytes"]),
        sha256=_sha256_file(path),
        required_key_ids=tuple(encryption["required_key_ids"]),
    )


def _overlay_changes(
    base_files: list[dict[str, Any]],
    current_files: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Describe every current logical difference directly against a v1 base."""

    base_by_path = {str(item["path"]): item for item in base_files}
    current_by_path = {str(item["path"]): item for item in current_files}
    changes: list[dict[str, Any]] = []
    for path in sorted(current_by_path):
        current = current_by_path[path]
        previous = base_by_path.get(path)
        if previous == current:
            continue
        changes.append(
            {
                "op": "add" if previous is None else "replace",
                **current,
            }
        )
    for path in sorted(set(base_by_path) - set(current_by_path)):
        changes.append({"op": "delete", "path": path})
    return sorted(changes, key=lambda item: (str(item["path"]), str(item["op"])))


def _safe_base_filename(value: object) -> str:
    if not isinstance(value, str) or not value or Path(value).name != value:
        raise BackupIntegrityError("Backup overlay has an unsafe base reference.")
    return value


def resolve_backup_base(
    archive_path: str | os.PathLike[str],
    manifest: dict[str, Any],
    *,
    catalog_path: str | os.PathLike[str] | None = None,
    available_key_ids: Iterable[str] | None = None,
    max_files: int = DEFAULT_MAX_FILES,
    max_expanded_bytes: int = DEFAULT_MAX_EXPANDED_BYTES,
) -> tuple[Path, dict[str, Any]]:
    """Resolve and verify the direct complete-v1 base for a v2 overlay.

    Overlay references are deliberately catalog-local: a basename, exact
    backup id, and SHA-256 bind the overlay to a neighbouring immutable v1
    archive.  The function never follows an overlay chain.
    """

    if manifest.get("version") != OVERLAY_BACKUP_VERSION:
        raise BackupIntegrityError("Backup does not declare an overlay storage mode.")
    base = manifest.get("base")
    if not isinstance(base, dict) or set(base) != {"filename", "backup_id", "sha256"}:
        raise BackupIntegrityError("Backup overlay base reference is invalid.")
    filename = _safe_base_filename(base.get("filename"))
    backup_id = base.get("backup_id")
    digest = base.get("sha256")
    if (
        not isinstance(backup_id, str)
        or not backup_id
        or not isinstance(digest, str)
        or not re.fullmatch(r"[0-9a-f]{64}", digest)
    ):
        raise BackupIntegrityError("Backup overlay base reference is invalid.")
    overlay = Path(archive_path).expanduser().resolve()
    catalog = (
        Path(catalog_path).expanduser().resolve()
        if catalog_path is not None
        else overlay.parent
    )
    candidate = catalog / filename
    if (
        candidate.is_symlink()
        or not candidate.is_file()
        or _sha256_file(candidate) != digest
    ):
        raise BackupIntegrityError("Backup overlay base is missing or changed.")
    base_manifest = verify_backup(
        candidate,
        available_key_ids=available_key_ids,
        max_files=max_files,
        max_expanded_bytes=max_expanded_bytes,
    )
    if (
        base_manifest.get("version") != BACKUP_VERSION
        or base_manifest.get("backup_id") != backup_id
    ):
        raise BackupIntegrityError(
            "Backup overlay base is not the referenced complete backup."
        )
    return candidate, base_manifest


def _load_create_overlay_base(
    base_archive: str | os.PathLike[str],
    *,
    final_path: Path,
    available_key_ids: Iterable[str] | None,
) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    """Load a v1 base that can safely be referenced from ``final_path``."""

    requested_base = Path(base_archive).expanduser()
    if requested_base.is_symlink():
        raise BackupError("Backup overlay base must be a regular catalog archive.")
    base_path = requested_base.resolve()
    if base_path.parent != final_path.parent or base_path == final_path:
        raise BackupError(
            "Backup overlays require a complete base in the same catalog."
        )
    if not base_path.is_file():
        raise BackupError("Backup overlay base is missing.")
    try:
        base_manifest = verify_backup(
            base_path,
            available_key_ids=available_key_ids,
        )
    except BackupIntegrityError as exc:
        raise BackupError("Backup overlay base cannot be verified.") from exc
    if base_manifest.get("version") != BACKUP_VERSION:
        raise BackupError("Backup overlays require a complete v1 base backup.")
    base_reference = {
        "filename": base_path.name,
        "backup_id": str(base_manifest["backup_id"]),
        "sha256": _sha256_file(base_path),
    }
    return base_path, base_manifest, base_reference


def create_backup(
    paths: AppPaths,
    *,
    output_path: str | os.PathLike[str] | None = None,
    key_store: InstallationKeyStore | None = None,
    base_archive: str | os.PathLike[str] | None = None,
) -> BackupRecord:
    """Create, verify, and atomically publish a Carlina backup.

    With no ``base_archive`` this preserves the complete v1 ZIP format.  The
    managed lifecycle may provide a verified neighbouring v1 archive to create
    a v2 overlay containing only direct differences from that complete base.
    """

    paths.ensure_directories()
    backup_id = f"{_now().strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:10]}"
    final_path = (
        Path(output_path or paths.backups_dir / f"carlina-backup-{backup_id}.zip")
        .expanduser()
        .resolve()
    )
    data_root = paths.data_root.resolve()
    backups_root = paths.backups_dir.resolve()
    if final_path.is_relative_to(data_root) and not final_path.is_relative_to(
        backups_root,
    ):
        message = (
            "Backup output inside the Carlina data root must use the owned "
            "backups directory."
        )
        raise BackupError(message)
    if final_path.exists():
        raise BackupError(f"Backup output already exists: {final_path}")
    final_path.parent.mkdir(parents=True, exist_ok=True)
    estimated_expanded_bytes = estimate_backup_expanded_bytes(paths)
    database_bytes = (
        paths.database_file.stat().st_size if paths.database_file.is_file() else 0
    )
    queue_snapshot_bytes = sum(
        _sqlite_storage_bytes(source)
        for category, source, _ in _iter_owned_files(paths)
        if category == "task_queues"
    )
    _require_backup_destination_capacity(
        final_path.parent,
        required_bytes=(
            database_bytes
            + queue_snapshot_bytes
            + estimated_expanded_bytes
            + _backup_staging_overhead(estimated_expanded_bytes)
        ),
    )
    publication_staging_root = final_path.parent / f"backup-{backup_id}"
    archive_staging = publication_staging_root / "backup.zip"
    snapshot = publication_staging_root / "snapshot" / "carlina.db"
    try:
        publication_staging_root.mkdir(parents=False, exist_ok=False)
        database_report = _snapshot_database(paths.database_file, snapshot)
        required_key_ids = scan_required_key_ids(snapshot)
        available_key_ids: tuple[str, ...] | None = None
        if required_key_ids:
            active_store = key_store or InstallationKeyStore(paths.master_keys_file)
            available = set(active_store.key_ids())
            available_key_ids = tuple(sorted(available))
            missing = sorted(set(required_key_ids) - available)
            if missing:
                raise BackupKeyMismatch(
                    "Database requires unavailable encryption keys: "
                    + ", ".join(missing)
                )

        files, source_by_archive = _logical_payload(paths, snapshot)
        created_at = _now().isoformat()
        manifest: dict[str, Any] = {
            "format": BACKUP_FORMAT,
            "version": BACKUP_VERSION,
            "backup_id": backup_id,
            "created_at": created_at,
            "carlina_version": _package_version(),
            "source_layout": {
                "mode": paths.mode,
                "database_name": paths.database_file.name,
                "data_root_name": paths.data_root.name,
            },
            "database": database_report,
            "encryption": {
                "required_key_ids": list(required_key_ids),
                "key_recovery_included": False,
            },
            "files": files,
            "file_count": len(files),
            "expanded_bytes": sum(int(item["size"]) for item in files),
        }
        archive_members = files
        if base_archive is not None:
            base_path, base_manifest, base_reference = _load_create_overlay_base(
                base_archive,
                final_path=final_path,
                available_key_ids=available_key_ids,
            )
            base_required_keys = set(base_manifest["encryption"]["required_key_ids"])
            if base_required_keys:
                if available_key_ids is None:
                    active_store = key_store or InstallationKeyStore(
                        paths.master_keys_file
                    )
                    available_key_ids = active_store.key_ids()
                missing_base_keys = sorted(base_required_keys - set(available_key_ids))
                if missing_base_keys:
                    raise BackupKeyMismatch(
                        "Backup overlay base requires unavailable encryption keys: "
                        + ", ".join(missing_base_keys)
                    )
            changes = _overlay_changes(base_manifest["files"], files)
            if not changes:
                # A byte-for-byte logical match is already a verified durable
                # recovery point.  Reuse it instead of emitting a zero-member
                # overlay that adds no protection or useful history.
                return _record_from_manifest(base_path, base_manifest)
            manifest.update(
                version=OVERLAY_BACKUP_VERSION,
                storage_mode="base_overlay",
                base=base_reference,
                changes=changes,
            )
            archive_members = [
                item for item in changes if item["op"] in {"add", "replace"}
            ]

        _require_backup_destination_capacity(
            publication_staging_root,
            required_bytes=(
                int(manifest["expanded_bytes"])
                + _backup_staging_overhead(int(manifest["expanded_bytes"]))
            ),
        )

        with zipfile.ZipFile(
            archive_staging,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=6,
            allowZip64=True,
        ) as archive:
            for item in archive_members:
                archive.write(source_by_archive[item["path"]], item["path"])
            archive.writestr(
                MANIFEST_NAME,
                json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            )

        verified = verify_backup(
            archive_staging,
            base_catalog=final_path.parent if base_archive is not None else None,
            available_key_ids=available_key_ids,
        )
        with archive_staging.open("rb+") as handle:
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(archive_staging, final_path)
        _fsync_directory(final_path.parent)
        return _record_from_manifest(final_path, verified)
    finally:
        _remove_staging_directory(
            publication_staging_root,
            allowed_parent=final_path.parent,
        )


def _safe_member_path(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise BackupIntegrityError(f"Unsafe backup member path: {name!r}")
    if any(part in {"", "."} for part in path.parts):
        raise BackupIntegrityError(f"Non-canonical backup member path: {name!r}")
    return path


def _manifest_from_archive(archive: zipfile.ZipFile) -> dict[str, Any]:
    try:
        raw = archive.read(MANIFEST_NAME)
        manifest = json.loads(raw)
    except KeyError as exc:
        raise BackupIntegrityError("Backup manifest is missing.") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BackupIntegrityError("Backup manifest is invalid JSON.") from exc
    if not isinstance(manifest, dict) or manifest.get("format") != BACKUP_FORMAT:
        raise BackupIntegrityError("Unsupported backup manifest format.")
    if manifest.get("version") not in {BACKUP_VERSION, OVERLAY_BACKUP_VERSION}:
        raise BackupIntegrityError("Unsupported backup manifest version.")
    return manifest


def _validate_encryption(
    manifest: dict[str, Any],
    *,
    available_key_ids: Iterable[str] | None,
) -> None:
    encryption = manifest.get("encryption")
    if (
        not isinstance(encryption, dict)
        or encryption.get("key_recovery_included") is not False
    ):
        raise BackupIntegrityError("Backup encryption metadata is invalid.")
    required = encryption.get("required_key_ids")
    if not isinstance(required, list) or not all(
        isinstance(item, str) for item in required
    ):
        raise BackupIntegrityError("Backup key requirements are invalid.")
    if available_key_ids is not None:
        missing_keys = sorted(set(required) - set(available_key_ids))
        if missing_keys:
            raise BackupKeyMismatch(
                "Backup requires unavailable encryption keys: "
                + ", ".join(missing_keys)
            )


def _validate_archive_members(
    archive: zipfile.ZipFile,
    expected: dict[str, dict[str, Any]],
    *,
    require_single_manifest: bool = False,
) -> None:
    """Require the ZIP payload to be exactly the declared physical members."""

    actual_names: set[str] = set()
    manifest_count = 0
    for info in archive.infolist():
        member = _safe_member_path(info.filename)
        if str(member) == MANIFEST_NAME:
            manifest_count += 1
            continue
        mode = info.external_attr >> 16
        if stat.S_ISLNK(mode):
            raise BackupIntegrityError(
                f"Backup contains a symbolic link: {info.filename}"
            )
        if info.is_dir():
            raise BackupIntegrityError(
                f"Backup contains an undeclared directory entry: {info.filename}"
            )
        name = str(member)
        if name not in expected:
            raise BackupIntegrityError(f"Undeclared backup member: {name}")
        if name in actual_names:
            raise BackupIntegrityError(f"Duplicate backup archive entry: {name}")
        if info.file_size != int(expected[name]["size"]):
            raise BackupIntegrityError(f"Size mismatch for backup member: {name}")
        digest = hashlib.sha256()
        with archive.open(info, "r") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
        if digest.hexdigest() != expected[name]["sha256"]:
            raise BackupIntegrityError(f"Hash mismatch for backup member: {name}")
        actual_names.add(name)
    if actual_names != set(expected):
        missing = sorted(set(expected) - actual_names)
        raise BackupIntegrityError(
            "Backup members are missing: " + ", ".join(missing[:10])
        )
    if require_single_manifest and manifest_count != 1:
        raise BackupIntegrityError("Backup overlay has an invalid manifest member set.")


def _validate_complete_manifest(
    archive: zipfile.ZipFile,
    manifest: dict[str, Any],
    *,
    available_key_ids: Iterable[str] | None,
    max_files: int,
    max_expanded_bytes: int,
) -> dict[str, Any]:
    """Keep complete-v1 validation behaviour compatible with existing archives."""

    raw_files = manifest.get("files")
    if not isinstance(raw_files, list):
        raise BackupIntegrityError("Backup manifest files must be a list.")
    if len(raw_files) > max_files or int(manifest.get("file_count", -1)) != len(
        raw_files
    ):
        raise BackupIntegrityError("Backup file-count budget or manifest count failed.")

    expected: dict[str, dict[str, Any]] = {}
    expanded_bytes = 0
    for item in raw_files:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise BackupIntegrityError("Backup file record is invalid.")
        member_path = _safe_member_path(item["path"])
        if not str(member_path).startswith("payload/"):
            raise BackupIntegrityError(
                f"Backup payload is outside the canonical root: {member_path}"
            )
        if str(member_path) in expected:
            raise BackupIntegrityError(f"Duplicate backup member: {member_path}")
        size = int(item.get("size", -1))
        digest = item.get("sha256")
        if size < 0 or not isinstance(digest, str) or len(digest) != 64:
            raise BackupIntegrityError(f"Invalid file metadata for {member_path}")
        expanded_bytes += size
        expected[str(member_path)] = item
    if (
        expanded_bytes > max_expanded_bytes
        or int(manifest.get("expanded_bytes", -1)) != expanded_bytes
    ):
        raise BackupIntegrityError(
            "Backup expanded-size budget or manifest total failed."
        )
    _validate_archive_members(archive, expected)
    _validate_encryption(manifest, available_key_ids=available_key_ids)
    return manifest


def _strict_logical_records(
    raw_files: object,
    *,
    max_files: int,
    max_expanded_bytes: int,
    declared_count: object,
    declared_expanded_bytes: object,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Validate the complete v2 logical table, not merely ZIP members."""

    if not isinstance(raw_files, list):
        raise BackupIntegrityError("Backup manifest files must be a list.")
    if (
        len(raw_files) > max_files
        or type(declared_count) is not int
        or declared_count != len(raw_files)
    ):
        raise BackupIntegrityError("Backup file-count budget or manifest count failed.")
    records: list[dict[str, Any]] = []
    by_path: dict[str, dict[str, Any]] = {}
    expanded_bytes = 0
    previous_path: str | None = None
    for item in raw_files:
        if not isinstance(item, dict) or set(item) != {
            "path",
            "category",
            "size",
            "sha256",
        }:
            raise BackupIntegrityError("Backup overlay logical file record is invalid.")
        path = item.get("path")
        category = item.get("category")
        size = item.get("size")
        digest = item.get("sha256")
        if not isinstance(path, str) or not isinstance(category, str):
            raise BackupIntegrityError("Backup overlay logical file record is invalid.")
        member = _safe_member_path(path)
        member_name = str(member)
        if (
            not member_name.startswith("payload/")
            or category not in _OWNED_CATEGORIES
            or member.parts[1] != category
        ):
            raise BackupIntegrityError("Backup overlay logical file path is invalid.")
        if category == "database" and member_name != "payload/database/carlina.db":
            raise BackupIntegrityError("Backup overlay database member is invalid.")
        if (
            type(size) is not int
            or size < 0
            or not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
        ):
            raise BackupIntegrityError(
                "Backup overlay logical file metadata is invalid."
            )
        if previous_path is not None and member_name <= previous_path:
            raise BackupIntegrityError(
                "Backup overlay logical members are not deterministic."
            )
        previous_path = member_name
        expanded_bytes += size
        records.append(item)
        by_path[member_name] = item
    if len(by_path) != len(records):
        raise BackupIntegrityError("Backup overlay has duplicate logical members.")
    if (
        expanded_bytes > max_expanded_bytes
        or type(declared_expanded_bytes) is not int
        or declared_expanded_bytes != expanded_bytes
    ):
        raise BackupIntegrityError(
            "Backup expanded-size budget or manifest total failed."
        )
    return records, by_path


def _strict_overlay_changes(
    raw_changes: object,
    *,
    base_files: dict[str, dict[str, Any]],
    logical_files: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    if not isinstance(raw_changes, list):
        raise BackupIntegrityError("Backup overlay changes must be a list.")
    changed: dict[str, dict[str, Any]] = {}
    rebuilt = dict(base_files)
    previous_key: tuple[str, str] | None = None
    physical: dict[str, dict[str, Any]] = {}
    for change in raw_changes:
        if not isinstance(change, dict):
            raise BackupIntegrityError("Backup overlay change is invalid.")
        operation = change.get("op")
        path = change.get("path")
        if operation not in {"add", "replace", "delete"} or not isinstance(path, str):
            raise BackupIntegrityError("Backup overlay change is invalid.")
        member = _safe_member_path(path)
        name = str(member)
        if not name.startswith("payload/"):
            raise BackupIntegrityError("Backup overlay change path is invalid.")
        key = (name, str(operation))
        if previous_key is not None and key <= previous_key:
            raise BackupIntegrityError("Backup overlay changes are not deterministic.")
        previous_key = key
        if name in changed:
            raise BackupIntegrityError(
                "Backup overlay changes duplicate a logical member."
            )
        changed[name] = change
        if operation == "delete":
            if set(change) != {"op", "path"} or name not in rebuilt:
                raise BackupIntegrityError(
                    "Backup overlay delete tombstone is invalid."
                )
            rebuilt.pop(name)
            continue
        if set(change) != {"op", "path", "category", "size", "sha256"}:
            raise BackupIntegrityError("Backup overlay physical change is invalid.")
        category = change.get("category")
        size = change.get("size")
        digest = change.get("sha256")
        if (
            not isinstance(category, str)
            or category not in _OWNED_CATEGORIES
            or member.parts[1] != category
            or type(size) is not int
            or size < 0
            or not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
        ):
            raise BackupIntegrityError("Backup overlay physical change is invalid.")
        logical = logical_files.get(name)
        physical_record = {
            "path": name,
            "category": category,
            "size": size,
            "sha256": digest,
        }
        if logical != physical_record:
            raise BackupIntegrityError(
                "Backup overlay change disagrees with logical table."
            )
        if operation == "add" and name in rebuilt:
            raise BackupIntegrityError("Backup overlay add conflicts with its base.")
        if operation == "replace" and name not in rebuilt:
            raise BackupIntegrityError(
                "Backup overlay replace is absent from its base."
            )
        rebuilt[name] = physical_record
        physical[name] = physical_record
    rebuilt_records = [rebuilt[path] for path in sorted(rebuilt)]
    logical_records = [logical_files[path] for path in sorted(logical_files)]
    if rebuilt_records != logical_records:
        raise BackupIntegrityError(
            "Backup overlay changes do not reconstruct its logical table."
        )
    return physical


def _validate_overlay_manifest(
    archive_path: Path,
    archive: zipfile.ZipFile,
    manifest: dict[str, Any],
    *,
    catalog_path: Path | None,
    available_key_ids: Iterable[str] | None,
    max_files: int,
    max_expanded_bytes: int,
) -> dict[str, Any]:
    if (
        set(manifest) != _V2_MANIFEST_KEYS
        or manifest.get("storage_mode") != "base_overlay"
    ):
        raise BackupIntegrityError("Backup overlay manifest schema is invalid.")
    if not all(
        isinstance(manifest.get(field), str) and manifest[field]
        for field in ("backup_id", "created_at", "carlina_version")
    ):
        raise BackupIntegrityError("Backup overlay identity metadata is invalid.")
    if not isinstance(manifest.get("source_layout"), dict) or not isinstance(
        manifest.get("database"), dict
    ):
        raise BackupIntegrityError("Backup overlay source metadata is invalid.")
    logical_records, logical_by_path = _strict_logical_records(
        manifest.get("files"),
        max_files=max_files,
        max_expanded_bytes=max_expanded_bytes,
        declared_count=manifest.get("file_count"),
        declared_expanded_bytes=manifest.get("expanded_bytes"),
    )
    if not any(
        item["path"] == "payload/database/carlina.db" for item in logical_records
    ):
        raise BackupIntegrityError(
            "Backup overlay is missing its logical database member."
        )
    _validate_encryption(manifest, available_key_ids=available_key_ids)
    _base_path, base_manifest = resolve_backup_base(
        archive_path,
        manifest,
        catalog_path=catalog_path,
        available_key_ids=available_key_ids,
        max_files=max_files,
        max_expanded_bytes=max_expanded_bytes,
    )
    base_records, base_by_path = _strict_logical_records(
        base_manifest.get("files"),
        max_files=max_files,
        max_expanded_bytes=max_expanded_bytes,
        declared_count=base_manifest.get("file_count"),
        declared_expanded_bytes=base_manifest.get("expanded_bytes"),
    )
    # v1 did not require this strict representation, but Carlina-created v1
    # bases always have it. A v2 overlay cannot rely on a weaker logical table.
    if not base_records:
        raise BackupIntegrityError("Backup overlay base has no logical members.")
    physical = _strict_overlay_changes(
        manifest.get("changes"),
        base_files=base_by_path,
        logical_files=logical_by_path,
    )
    _validate_archive_members(archive, physical, require_single_manifest=True)
    return manifest


def verify_backup(
    archive_path: str | os.PathLike[str],
    *,
    base_catalog: str | os.PathLike[str] | None = None,
    available_key_ids: Iterable[str] | None = None,
    max_files: int = DEFAULT_MAX_FILES,
    max_expanded_bytes: int = DEFAULT_MAX_EXPANDED_BYTES,
) -> dict[str, Any]:
    """Verify paths, types, budgets, hashes, DB integrity, and key requirements."""

    path = Path(archive_path).expanduser().resolve()
    try:
        archive = zipfile.ZipFile(path, "r")
    except (OSError, zipfile.BadZipFile) as exc:
        raise BackupIntegrityError(f"Backup archive is unreadable: {path}") from exc
    with archive:
        manifest = _manifest_from_archive(archive)
        if manifest["version"] == BACKUP_VERSION:
            return _validate_complete_manifest(
                archive,
                manifest,
                available_key_ids=available_key_ids,
                max_files=max_files,
                max_expanded_bytes=max_expanded_bytes,
            )
        return _validate_overlay_manifest(
            path,
            archive,
            manifest,
            catalog_path=(
                Path(base_catalog).expanduser().resolve()
                if base_catalog is not None
                else None
            ),
            available_key_ids=available_key_ids,
            max_files=max_files,
            max_expanded_bytes=max_expanded_bytes,
        )


def inspect_backup(archive_path: str | os.PathLike[str]) -> dict[str, Any]:
    """Read validated manifest structure without hashing every payload file."""

    path = Path(archive_path).expanduser().resolve()
    try:
        with zipfile.ZipFile(path, "r") as archive:
            manifest = _manifest_from_archive(archive)
    except (OSError, zipfile.BadZipFile) as exc:
        raise BackupIntegrityError(f"Backup archive is unreadable: {path}") from exc
    if not isinstance(manifest.get("files"), list):
        raise BackupIntegrityError("Backup manifest files must be a list.")
    return manifest


def _restore_sibling_path(
    destination: Path,
    restore_id: str,
    *,
    phase: str,
) -> Path:
    """Return a short, deterministic sibling path for restore transactions.

    Destination names and backup IDs can both be long. Repeating them in the
    staging component needlessly consumes Windows' path budget before archive
    member names are appended, especially when a mapped drive resolves to UNC.
    The digest keeps concurrent destinations distinct without exposing either
    path in the temporary basename.
    """

    token = hashlib.sha256(
        f"{destination.name}\0{restore_id}\0{phase}".encode()
    ).hexdigest()[:16]
    return destination.parent / f".{phase}-{token}"


def restore_safety_root(
    destination_root: str | os.PathLike[str],
    backup_id: str,
) -> Path:
    """Return the deterministic old-root location for an activating restore.

    Restore handoff recovery needs the exact transaction sibling without
    duplicating the path-token algorithm.  This is intentionally narrow: it
    exposes only the bounded pre-restore location, not a general staging API.
    """

    destination = Path(destination_root).expanduser().resolve()
    return _restore_sibling_path(destination, backup_id, phase="pre-restore")


def restore_staging_root(
    destination_root: str | os.PathLike[str],
    backup_id: str,
) -> Path:
    """Return the deterministic staged-root location for an activating restore."""

    destination = Path(destination_root).expanduser().resolve()
    return _restore_sibling_path(destination, backup_id, phase="restore")


def _directory_fsync_supported() -> bool:
    return os.name != "nt"


def _fsync_directory(path: Path) -> None:
    """Persist a directory mutation on POSIX; Windows is best-effort."""

    if not _directory_fsync_supported():
        return
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _activation_witness_target(
    staging_root: Path,
    witness: RestoreActivationWitness,
) -> Path:
    if not isinstance(witness.relative_path, str) or not witness.relative_path:
        raise BackupError("Restore activation witness has an invalid target.")
    if not isinstance(witness.payload, bytes) or not witness.payload:
        raise BackupError("Restore activation witness has an invalid payload.")
    if len(witness.payload) > 1024 * 1024:
        raise BackupError("Restore activation witness payload is too large.")
    try:
        relative = PurePosixPath(witness.relative_path)
    except TypeError as exc:  # pragma: no cover - PurePosixPath is defensive
        raise BackupError("Restore activation witness has an invalid target.") from exc
    if (
        relative.is_absolute()
        or not relative.parts
        or "\\" in witness.relative_path
        or "\x00" in witness.relative_path
        or any(":" in part for part in relative.parts)
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise BackupError("Restore activation witness target is unsafe.")
    target = staging_root.joinpath(*relative.parts)
    try:
        target.resolve().relative_to(staging_root.resolve())
    except ValueError as exc:
        raise BackupError("Restore activation witness target escapes staging.") from exc
    return target


def _write_activation_witness(
    staging_root: Path,
    witness: RestoreActivationWitness,
) -> Path:
    """Write and fsync a collision-free activation proof into staging."""

    target = _activation_witness_target(staging_root, witness)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() or target.is_symlink():
        raise BackupError(f"Restore activation witness already exists: {target}")
    with target.open("xb") as handle:
        handle.write(witness.payload)
        handle.flush()
        os.fsync(handle.fileno())
    _fsync_directory(target.parent)
    if target.parent != staging_root:
        _fsync_directory(staging_root)
    return target


def _validate_activation_witness(
    root: Path,
    witness: RestoreActivationWitness,
) -> Path:
    target = _activation_witness_target(root, witness)
    if not target.is_file() or target.is_symlink():
        raise BackupError(f"Restore activation witness is missing: {target}")
    try:
        payload = target.read_bytes()
    except OSError as exc:
        raise BackupError(
            f"Restore activation witness is unreadable: {target}"
        ) from exc
    if payload != witness.payload:
        raise BackupError(f"Restore activation witness does not match: {target}")
    return target


def _validate_restored_root(root: Path, manifest: dict[str, Any]) -> None:
    if root.is_symlink() or not root.is_dir():
        raise BackupError(f"Restore root is unsafe: {root}")
    try:
        read_runtime_profile(root)
    except RuntimeProfileError:
        raise BackupIntegrityError("Restored runtime profile is invalid.") from None
    restored_database = root / "database" / "carlina.db"
    restored_report = _database_integrity(restored_database)
    if restored_report["schema_heads"] != manifest["database"]["schema_heads"]:
        raise BackupIntegrityError(
            "Restored database migration heads differ from the manifest."
        )


def preserve_local_backup_catalog(
    source_root: Path,
    destination_root: Path,
    *,
    validate_only: bool = False,
) -> None:
    """Validate or move one catalog across a witnessed switch without merging."""

    source = source_root / "backups"
    target = destination_root / "backups"
    if source.exists() or source.is_symlink() or source.is_junction():
        if source.is_symlink() or source.is_junction() or not source.is_dir():
            raise BackupError(f"Refusing to preserve unsafe local directory: {source}")
        if target.exists() or target.is_symlink() or target.is_junction():
            raise BackupError("Restore split has local backup catalogs in both roots.")
        if validate_only:
            return
        os.replace(source, target)
        _fsync_directory(source_root)
        _fsync_directory(destination_root)
    elif (target.exists() or target.is_symlink() or target.is_junction()) and (
        target.is_symlink() or target.is_junction() or not target.is_dir()
    ):
        raise BackupError(f"Restore split has an unsafe local directory: {target}")


def _activate_staging_root(
    *,
    staging: Path,
    destination: Path,
    safety_path: Path,
    manifest: dict[str, Any],
    state: _RootSwitchState,
    require_safe_roots: bool,
) -> RestoreResult:
    """Move a validated staging root into place with rollback-aware handling."""

    if require_safe_roots and (staging.is_symlink() or not staging.is_dir()):
        raise BackupError(f"Restore staging root is unsafe: {staging}")
    safety_root: Path | None = None
    moved_local_state: list[tuple[Path, Path]] = []
    staging_promoted = False
    try:
        destination_present = destination.exists() or (
            require_safe_roots and destination.is_symlink()
        )
        if destination_present:
            if require_safe_roots and (
                destination.is_symlink() or not destination.is_dir()
            ):
                raise BackupError(f"Restore destination is unsafe: {destination}")
            _preserve_local_restore_state(destination, staging)
            safety_root = safety_path
            if safety_root.exists() or (
                require_safe_roots and safety_root.is_symlink()
            ):
                raise BackupError(f"Restore safety path already exists: {safety_root}")
            # Keep staging after an uncertain rename error.  The witness lets a
            # later retry distinguish a safe split from an ambiguous layout.
            state.started = True
            os.replace(destination, safety_root)
            _fsync_directory(destination.parent)
        if safety_root is not None:
            moved_local_state = _move_local_restore_state(safety_root, staging)
        state.started = True
        os.replace(staging, destination)
        staging_promoted = True
        _fsync_directory(destination.parent)
        return RestoreResult(destination, safety_root, manifest)
    except BaseException:
        promoted_or_indeterminate = staging_promoted or (
            not staging.exists() and destination.exists()
        )
        if not promoted_or_indeterminate:
            for source, target in reversed(moved_local_state):
                if target.exists() and not source.exists():
                    os.replace(target, source)
                    _fsync_directory(source.parent)
                    if target.parent != source.parent:
                        _fsync_directory(target.parent)
            if (
                safety_root is not None
                and not destination.exists()
                and (not require_safe_roots or not destination.is_symlink())
            ):
                os.replace(safety_root, destination)
                _fsync_directory(destination.parent)
            state.rollback_completed = True
        raise


def _recover_activation_split(
    *,
    destination: Path,
    staging: Path,
    safety_root: Path,
    manifest: dict[str, Any],
    witness: RestoreActivationWitness,
) -> RestoreResult | None:
    """Finish only the uniquely identifiable post-first-rename transaction.

    A destination root is never sufficient evidence.  The proof has to be the
    exact witness written after extraction and validation in staging.  If the
    old root has already moved to the deterministic safety sibling, a matching
    staging witness lets us complete the second rename without extracting the
    archive again; every other partial shape is rejected as ambiguous.
    """

    staging_exists = staging.exists() or staging.is_symlink()
    safety_exists = safety_root.exists() or safety_root.is_symlink()
    destination_exists = destination.exists() or destination.is_symlink()
    if destination_exists:
        if destination.is_symlink() or not destination.is_dir():
            raise BackupError(f"Restore destination is unsafe: {destination}")
        destination_witness = _activation_witness_target(destination, witness)
        if destination_witness.exists() or destination_witness.is_symlink():
            _validate_activation_witness(destination, witness)
            if staging_exists:
                raise BackupError(
                    "Restore activation has both destination and staging roots."
                )
            if safety_exists and (safety_root.is_symlink() or not safety_root.is_dir()):
                raise BackupError(f"Restore safety root is unsafe: {safety_root}")
            _validate_restored_root(destination, manifest)
            _fsync_directory(destination.parent)
            return RestoreResult(
                destination,
                safety_root if safety_exists else None,
                manifest,
            )
        if staging_exists and not safety_exists:
            if staging.is_symlink() or not staging.is_dir():
                raise BackupError(f"Restore staging root is unsafe: {staging}")
            staging_witness = _activation_witness_target(staging, witness)
            if staging_witness.is_symlink():
                raise BackupError(
                    f"Restore activation witness is unsafe: {staging_witness}"
                )
            if not staging_witness.exists():
                # No root rename has occurred and the exact transaction staging
                # tree has not reached its commit-ready witness. It may be a
                # partial extraction left by process death; discard only this
                # deterministic sibling and rebuild it from the verified archive.
                _remove_staging_directory(
                    staging,
                    allowed_parent=destination.parent,
                )
                _fsync_directory(destination.parent)
                return None
            _validate_restored_root(staging, manifest)
            _validate_activation_witness(staging, witness)
            state = _RootSwitchState()
            return _activate_staging_root(
                staging=staging,
                destination=destination,
                safety_path=safety_root,
                manifest=manifest,
                state=state,
                require_safe_roots=True,
            )
        if staging_exists or safety_exists:
            raise BackupError(
                "Restore activation state is ambiguous; refusing recovery."
            )
        return None

    if not staging_exists and not safety_exists:
        return None
    if not staging_exists:
        raise BackupError("Restore activation is missing its validated staging root.")
    if staging.is_symlink() or not staging.is_dir():
        raise BackupError(f"Restore staging root is unsafe: {staging}")
    if safety_exists and (safety_root.is_symlink() or not safety_root.is_dir()):
        raise BackupError(f"Restore safety root is unsafe: {safety_root}")
    _validate_restored_root(staging, manifest)
    _validate_activation_witness(staging, witness)
    if safety_exists:
        preserve_local_backup_catalog(safety_root, staging)
    os.replace(staging, destination)
    _fsync_directory(destination.parent)
    return RestoreResult(destination, safety_root if safety_exists else None, manifest)


def _extract_backup_member(
    archive: zipfile.ZipFile,
    member: str,
    staging: Path,
) -> None:
    """Extract one already-verified logical member without path traversal."""

    source_member = PurePosixPath(member)
    relative = PurePosixPath(*source_member.parts[1:])
    target = staging.joinpath(*relative.parts)
    resolved_target = target.resolve()
    try:
        resolved_target.relative_to(staging)
    except ValueError as exc:
        raise BackupIntegrityError(
            f"Restore target escapes staging: {source_member}"
        ) from exc
    target.parent.mkdir(parents=True, exist_ok=True)
    with (
        archive.open(str(source_member), "r") as source,
        target.open("xb") as sink,
    ):
        shutil.copyfileobj(source, sink, length=1024 * 1024)


def _extract_verified_backup(
    archive_path: Path,
    manifest: dict[str, Any],
    staging: Path,
    *,
    available_key_ids: Iterable[str] | None,
    max_files: int,
    max_expanded_bytes: int,
) -> None:
    """Materialize either a complete v1 archive or a one-level v2 overlay."""

    if manifest["version"] == BACKUP_VERSION:
        with zipfile.ZipFile(archive_path, "r") as archive:
            for item in manifest["files"]:
                _extract_backup_member(archive, str(item["path"]), staging)
        return

    base_path, base_manifest = resolve_backup_base(
        archive_path,
        manifest,
        available_key_ids=available_key_ids,
        max_files=max_files,
        max_expanded_bytes=max_expanded_bytes,
    )
    changed_paths = {str(change["path"]) for change in manifest["changes"]}
    with zipfile.ZipFile(base_path, "r") as base_archive:
        for item in base_manifest["files"]:
            member = str(item["path"])
            if member not in changed_paths:
                _extract_backup_member(base_archive, member, staging)
    with zipfile.ZipFile(archive_path, "r") as overlay_archive:
        for change in manifest["changes"]:
            if change["op"] != "delete":
                _extract_backup_member(overlay_archive, str(change["path"]), staging)


def restore_backup(
    archive_path: str | os.PathLike[str],
    destination_root: str | os.PathLike[str],
    *,
    key_store: InstallationKeyStore | None = None,
    activate: bool = True,
    activation_witness: RestoreActivationWitness | None = None,
    max_files: int = DEFAULT_MAX_FILES,
    max_expanded_bytes: int = DEFAULT_MAX_EXPANDED_BYTES,
) -> RestoreResult:
    """Extract to a sibling staging root, validate, then atomically activate."""

    archive_path = Path(archive_path).expanduser().resolve()
    destination = Path(destination_root).expanduser().resolve()
    if destination == Path(destination.anchor) or destination == destination.parent:
        raise BackupError("Refusing to restore over a filesystem root.")
    if activation_witness is not None and not activate:
        raise BackupError("Restore activation witnesses require an active restore.")
    if activation_witness is not None and not isinstance(
        activation_witness,
        RestoreActivationWitness,
    ):
        raise BackupError("Restore activation witness has an invalid type.")
    available = key_store.key_ids() if key_store is not None else None
    manifest = verify_backup(
        archive_path,
        available_key_ids=available,
        max_files=max_files,
        max_expanded_bytes=max_expanded_bytes,
    )
    restore_id = str(manifest["backup_id"])
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = _restore_sibling_path(destination, restore_id, phase="restore")
    safety_path = _restore_sibling_path(destination, restore_id, phase="pre-restore")
    if activation_witness is not None:
        recovered = _recover_activation_split(
            destination=destination,
            staging=staging,
            safety_root=safety_path,
            manifest=manifest,
            witness=activation_witness,
        )
        if recovered is not None:
            return recovered
    if staging.exists():
        raise BackupError(f"Restore staging path already exists: {staging}")
    if activation_witness is not None and (
        safety_path.exists() or safety_path.is_symlink()
    ):
        raise BackupError(
            "Restore activation has an unexpected pre-existing safety root."
        )
    # Restored artifacts inherit this ACL before the root is activated. A
    # default-mode sibling can grant access beyond the owner on Windows.
    staging.mkdir(mode=0o700, parents=False)
    staging_created = True
    switch_state = _RootSwitchState()
    try:
        _extract_verified_backup(
            archive_path,
            manifest,
            staging,
            available_key_ids=available,
            max_files=max_files,
            max_expanded_bytes=max_expanded_bytes,
        )

        _validate_restored_root(staging, manifest)
        if activation_witness is not None:
            _write_activation_witness(staging, activation_witness)
        if not activate:
            return RestoreResult(staging, None, manifest)

        return _activate_staging_root(
            staging=staging,
            destination=destination,
            safety_path=safety_path,
            manifest=manifest,
            state=switch_state,
            require_safe_roots=activation_witness is not None,
        )
    except BaseException:
        if (
            staging_created
            and staging.exists()
            and (not switch_state.started or switch_state.rollback_completed)
        ):
            _remove_staging_directory(staging, allowed_parent=destination.parent)
        raise


def _preserve_local_restore_state(source_root: Path, staging_root: Path) -> None:
    """Carry machine-local configuration and keys into a data-root switch."""

    for name in ("config", "secrets"):
        source = source_root / name
        target = staging_root / name
        if not source.exists() or target.exists():
            continue
        if source.is_symlink():
            raise BackupError(f"Refusing to preserve symbolic link: {source}")
        if source.is_dir():
            shutil.copytree(source, target, symlinks=False)
        elif source.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)


def _move_local_restore_state(
    safety_root: Path,
    staging_root: Path,
) -> list[tuple[Path, Path]]:
    """Transfer large local catalogs into the new root without duplicating them."""

    transfers: list[tuple[Path, Path]] = []
    for name in ("backups",):
        source = safety_root / name
        target = staging_root / name
        if not source.exists():
            continue
        if source.is_symlink() or not source.is_dir():
            raise BackupError(f"Refusing to preserve unsafe local directory: {source}")
        if target.exists():
            raise BackupError(
                f"Restored payload conflicts with local directory: {target}"
            )
        transfers.append((source, target))

    moved: list[tuple[Path, Path]] = []
    try:
        for source, target in transfers:
            os.replace(source, target)
            moved.append((source, target))
    except BaseException:
        for source, target in reversed(moved):
            if target.exists() and not source.exists():
                os.replace(target, source)
        raise
    return moved


def _remove_staging_directory(path: Path, *, allowed_parent: Path) -> None:
    if not path.exists():
        return
    resolved = path.resolve()
    parent = allowed_parent.resolve()
    if resolved.parent != parent or not (
        resolved.name.startswith("backup-") or ".restore-" in resolved.name
    ):
        raise BackupError(f"Refusing to clean unexpected staging path: {resolved}")
    shutil.rmtree(resolved)


__all__ = [
    "BACKUP_FORMAT",
    "BACKUP_VERSION",
    "OVERLAY_BACKUP_VERSION",
    "BackupError",
    "BackupIntegrityError",
    "BackupKeyMismatch",
    "BackupRecord",
    "RestoreActivationWitness",
    "RestoreResult",
    "preserve_local_backup_catalog",
    "create_backup",
    "estimate_backup_expanded_bytes",
    "inspect_backup",
    "inspect_database",
    "resolve_backup_base",
    "restore_backup",
    "restore_safety_root",
    "restore_staging_root",
    "scan_required_key_ids",
    "snapshot_database",
    "verify_backup",
]
