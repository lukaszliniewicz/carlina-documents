"""Plan and execute resumable migration from checkout-local Carlina storage.

The migration is deliberately copy-first. Source state is never renamed or
deleted, regular files are journaled individually, and the consistent SQLite
snapshot is activated last. Until that final atomic rename, AppPaths continues
to select the legacy database and a partially copied native data root is inert.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from filelock import FileLock, Timeout

from iarf_assistant.core.app_paths import AppPaths
from iarf_assistant.core.backup_service import (
    BackupRecord,
    create_backup,
    inspect_database,
    snapshot_database,
)
from iarf_assistant.core.encryption import InstallationKeyStore

MIGRATION_PLAN_FORMAT = "carlina.data-migration-plan"
MIGRATION_PLAN_VERSION = 1
MIGRATION_JOURNAL_FORMAT = "carlina.data-migration-journal"
MIGRATION_JOURNAL_VERSION = 1
_COPY_CHUNK_SIZE = 4 * 1024 * 1024
_FREE_SPACE_MARGIN = 256 * 1024**2
_JOURNAL_CHECKPOINT_FILE_COUNT = 128
_ATOMIC_REPLACE_ATTEMPTS = 8
_ATOMIC_REPLACE_INITIAL_DELAY_SECONDS = 0.025
_ATOMIC_REPLACE_MAX_DELAY_SECONDS = 0.25


class DataMigrationError(RuntimeError):
    """Base class for safe data-migration failures."""


class DataMigrationConflict(DataMigrationError):
    """The target or source changed in a way that prevents safe continuation."""


class DataMigrationConfirmationRequired(DataMigrationError):
    """The operator did not provide the exact confirmation token."""


@dataclass(frozen=True, slots=True)
class DatabaseCandidate:
    path: str
    size: int
    modified_ns: int
    sqlite_header: bool
    selected: bool


@dataclass(frozen=True, slots=True)
class MigrationFile:
    source: str
    destination: str
    category: str
    size: int
    modified_ns: int

    @property
    def key(self) -> str:
        return self.destination


@dataclass(frozen=True, slots=True)
class MigrationNotice:
    code: str
    message: str
    paths: tuple[str, ...] = ()
    blocking: bool = False


@dataclass(frozen=True, slots=True)
class DataMigrationPlan:
    format: str
    version: int
    plan_id: str
    created_at: str
    confirmation_token: str
    source_database: str
    target_database: str
    target_root: str
    files: tuple[MigrationFile, ...]
    database_candidates: tuple[DatabaseCandidate, ...]
    notices: tuple[MigrationNotice, ...]
    ignored_paths: tuple[str, ...]
    payload_bytes: int
    minimum_free_bytes: int
    applicable: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class DataMigrationResult:
    plan_id: str
    target_root: Path
    target_database: Path
    copied_files: int
    reused_files: int
    payload_bytes: int
    safety_backup: BackupRecord | None
    journal_path: Path


FaultInjector = Callable[[str, MigrationFile | None], None]


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _atomic_json_write(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    delay = _ATOMIC_REPLACE_INITIAL_DELAY_SECONDS
    for attempt in range(_ATOMIC_REPLACE_ATTEMPTS):
        try:
            os.replace(temporary, path)
            break
        except PermissionError:
            if attempt == _ATOMIC_REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(delay)
            delay = min(delay * 2, _ATOMIC_REPLACE_MAX_DELAY_SECONDS)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_COPY_CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def _is_sqlite_file(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size < 16:
        return False
    try:
        with path.open("rb") as handle:
            return handle.read(16) == b"SQLite format 3\x00"
    except OSError:
        return False


def _candidate_instance_roots(paths: AppPaths) -> tuple[Path, ...]:
    roots = [
        paths.instance_dir,
        paths.program_root / "instance",
        paths.package_root / "instance",
    ]
    roots.extend(
        location
        for location in paths.legacy_locations
        if location.name.casefold() == "instance"
    )
    return tuple(dict.fromkeys(root.expanduser().resolve() for root in roots))


def _database_candidates(paths: AppPaths) -> tuple[DatabaseCandidate, ...]:
    candidates: list[DatabaseCandidate] = []
    active = paths.database_file.resolve()
    seen: set[Path] = set()
    for root in _candidate_instance_roots(paths):
        if not root.is_dir():
            continue
        for candidate in sorted(root.glob("*.db")):
            resolved = candidate.resolve()
            if resolved in seen or not candidate.is_file():
                continue
            seen.add(resolved)
            stat_result = candidate.stat()
            candidates.append(
                DatabaseCandidate(
                    path=str(resolved),
                    size=stat_result.st_size,
                    modified_ns=stat_result.st_mtime_ns,
                    sqlite_header=_is_sqlite_file(resolved),
                    selected=resolved == active,
                )
            )
    return tuple(candidates)


def _relative_destination(value: str) -> PurePosixPath:
    destination = PurePosixPath(value)
    if destination.is_absolute() or not destination.parts or ".." in destination.parts:
        raise DataMigrationError(f"Unsafe migration destination: {value!r}")
    return destination


def _iter_mapping_files(
    source_root: Path,
    destination_root: PurePosixPath,
    category: str,
) -> Iterable[MigrationFile]:
    if not source_root.exists():
        return
    if source_root.is_symlink():
        raise DataMigrationError(
            f"Migration refuses symbolic-link source roots: {source_root}"
        )
    if not source_root.is_dir():
        raise DataMigrationError(f"Expected a migration directory: {source_root}")
    for source in sorted(source_root.rglob("*")):
        if source.is_symlink():
            raise DataMigrationError(
                f"Migration refuses symbolic links in owned data: {source}"
            )
        if not source.is_file():
            continue
        relative = PurePosixPath(*source.relative_to(source_root).parts)
        destination = destination_root / relative
        stat_result = source.stat()
        yield MigrationFile(
            source=str(source.resolve()),
            destination=str(destination),
            category=category,
            size=stat_result.st_size,
            modified_ns=stat_result.st_mtime_ns,
        )


def _plan_digest(payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _ignored_instance_paths(
    instance_root: Path,
    *,
    selected_database: Path,
) -> tuple[str, ...]:
    copied_names = {
        "addons",
        "artifacts",
        "bootstrap",
        "context-fabric-audits",
        "skills",
        "state",
        "task_queues",
    }
    explicitly_ephemeral = {"locks", "logs", "rate_limits", "sessions", "tmp"}
    ignored: list[str] = []
    if not instance_root.is_dir():
        return ()
    for child in sorted(instance_root.iterdir()):
        if child.resolve() == selected_database:
            continue
        folded = child.name.casefold()
        if folded in copied_names:
            continue
        if folded in explicitly_ephemeral or folded == "backups":
            ignored.append(str(child.resolve()))
            continue
        if child.suffix.casefold() in {".db", ".sqlite", ".sql", ".log"}:
            ignored.append(str(child.resolve()))
            continue
        if child.name.startswith("."):
            ignored.append(str(child.resolve()))
            continue
        ignored.append(str(child.resolve()))
    return tuple(ignored)


def build_data_migration_plan(
    paths: AppPaths,
    *,
    source_database: str | os.PathLike[str] | None = None,
    target_root: str | os.PathLike[str] | None = None,
) -> DataMigrationPlan:
    """Inventory legacy storage and return a deterministic, non-mutating plan."""

    notices: list[MigrationNotice] = []
    if not paths.is_legacy:
        notices.append(
            MigrationNotice(
                "not-legacy-layout",
                "The active installation is not using checkout-local legacy storage.",
                (str(paths.data_root),),
                True,
            )
        )

    candidates = _database_candidates(paths)
    viable = tuple(candidate for candidate in candidates if candidate.sqlite_header)
    requested = (
        Path(source_database).expanduser().resolve()
        if source_database is not None
        else paths.database_file.resolve()
    )
    candidate_paths = {Path(candidate.path): candidate for candidate in candidates}
    selected_candidate = candidate_paths.get(requested)
    if selected_candidate is None or not selected_candidate.sqlite_header:
        notices.append(
            MigrationNotice(
                "invalid-database-selection",
                "The selected source is not a viable SQLite database candidate.",
                (str(requested),),
                True,
            )
        )
    if source_database is None and len(viable) > 1:
        notices.append(
            MigrationNotice(
                "database-selection-required",
                "Multiple non-empty SQLite databases were found; rerun with an explicit source database.",
                tuple(candidate.path for candidate in viable),
                True,
            )
        )
    elif len(viable) > 1:
        notices.append(
            MigrationNotice(
                "alternative-databases-left-in-place",
                "Other database candidates will remain untouched for manual review.",
                tuple(candidate.path for candidate in viable if candidate.path != str(requested)),
            )
        )

    resolved_target = Path(target_root or paths.migration_target_root).expanduser().resolve()
    source_instance = requested.parent
    try:
        resolved_target.relative_to(source_instance)
    except ValueError:
        pass
    else:
        notices.append(
            MigrationNotice(
                "target-inside-source",
                "The migration target cannot be inside the legacy instance directory.",
                (str(resolved_target), str(source_instance)),
                True,
            )
        )
    if resolved_target == Path(resolved_target.anchor):
        notices.append(
            MigrationNotice(
                "unsafe-target-root",
                "The migration target cannot be a filesystem root.",
                (str(resolved_target),),
                True,
            )
        )

    mappings: list[tuple[Path, PurePosixPath, str]] = []
    uploads = paths.uploads_dir.resolve()
    if uploads.exists():
        mappings.append((uploads, PurePosixPath("uploads"), "uploads"))
    directory_contract = (
        ("artifacts", "artifacts", "artifacts"),
        ("addons", "addons", "addons"),
        ("state", "state", "state"),
        ("skills", "skills", "skills"),
        ("task_queues", "task_queues", "task-queues"),
        (
            "context-fabric-audits",
            "artifacts/context-fabric-audits",
            "artifacts",
        ),
        ("bootstrap", "state/bootstrap", "manager-state"),
    )
    for source_name, destination_name, category in directory_contract:
        source = source_instance / source_name
        if source.exists() and source.resolve() != uploads:
            mappings.append((source.resolve(), PurePosixPath(destination_name), category))

    files: list[MigrationFile] = []
    try:
        for source, destination, category in mappings:
            files.extend(_iter_mapping_files(source, destination, category))
    except DataMigrationError as exc:
        notices.append(
            MigrationNotice(
                "unsafe-source-entry",
                str(exc),
                blocking=True,
            )
        )

    destinations: dict[str, list[str]] = {}
    for entry in files:
        destinations.setdefault(entry.destination, []).append(entry.source)
    collisions = {
        destination: sources
        for destination, sources in destinations.items()
        if len(sources) > 1
    }
    if collisions:
        notices.append(
            MigrationNotice(
                "destination-collision",
                "Multiple legacy files map to the same target path.",
                tuple(
                    f"{destination} <- {', '.join(sources)}"
                    for destination, sources in sorted(collisions.items())
                ),
                True,
            )
        )

    files.sort(key=lambda entry: (entry.destination, entry.source))
    selected_candidates = tuple(
        replace(candidate, selected=Path(candidate.path) == requested)
        for candidate in candidates
    )
    ignored = _ignored_instance_paths(
        source_instance,
        selected_database=requested,
    )
    if ignored:
        notices.append(
            MigrationNotice(
                "paths-left-in-place",
                "Unowned, obsolete, backup, log, cache, and alternate database paths are not copied automatically.",
                ignored,
            )
        )
    if any(Path(path).suffix.casefold() == ".sql" for path in ignored):
        notices.append(
            MigrationNotice(
                "sql-dumps-require-manual-review",
                "SQL dumps are never imported or copied automatically.",
                tuple(path for path in ignored if Path(path).suffix.casefold() == ".sql"),
            )
        )

    database_size = selected_candidate.size if selected_candidate else 0
    payload_bytes = database_size + sum(entry.size for entry in files)
    minimum_free_bytes = payload_bytes * 2 + _FREE_SPACE_MARGIN
    digest_payload = {
        "format": MIGRATION_PLAN_FORMAT,
        "version": MIGRATION_PLAN_VERSION,
        "source_database": str(requested),
        "target_database": str(resolved_target / "database" / "carlina.db"),
        "target_root": str(resolved_target),
        "files": [asdict(entry) for entry in files],
        "database_candidates": [asdict(item) for item in selected_candidates],
        "notices": [asdict(item) for item in notices],
        "ignored_paths": list(ignored),
        "payload_bytes": payload_bytes,
        "minimum_free_bytes": minimum_free_bytes,
    }
    plan_id = _plan_digest(digest_payload)
    return DataMigrationPlan(
        format=MIGRATION_PLAN_FORMAT,
        version=MIGRATION_PLAN_VERSION,
        plan_id=plan_id,
        created_at=_utc_now(),
        confirmation_token=f"MIGRATE-{plan_id[:12].upper()}",
        source_database=str(requested),
        target_database=str(resolved_target / "database" / "carlina.db"),
        target_root=str(resolved_target),
        files=tuple(files),
        database_candidates=selected_candidates,
        notices=tuple(notices),
        ignored_paths=ignored,
        payload_bytes=payload_bytes,
        minimum_free_bytes=minimum_free_bytes,
        applicable=not any(notice.blocking for notice in notices),
    )


def write_data_migration_plan(plan: DataMigrationPlan, path: Path) -> None:
    """Persist a plan for operator review or a later exact application."""

    _atomic_json_write(path.expanduser().resolve(), plan.to_dict())


def load_data_migration_plan(path: Path) -> DataMigrationPlan:
    """Load a migration plan and reject unsupported or internally altered data."""

    try:
        payload = json.loads(path.expanduser().resolve().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DataMigrationError(f"Could not read migration plan: {path}") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("format") != MIGRATION_PLAN_FORMAT
        or payload.get("version") != MIGRATION_PLAN_VERSION
    ):
        raise DataMigrationError("Unsupported data-migration plan format.")
    try:
        files = tuple(MigrationFile(**item) for item in payload["files"])
        candidates = tuple(
            DatabaseCandidate(**item) for item in payload["database_candidates"]
        )
        notices = tuple(MigrationNotice(**item) for item in payload["notices"])
        plan = DataMigrationPlan(
            **{
                **payload,
                "files": files,
                "database_candidates": candidates,
                "notices": notices,
                "ignored_paths": tuple(payload["ignored_paths"]),
            }
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise DataMigrationError("Malformed data-migration plan.") from exc
    expected = build_data_migration_plan_digest(plan)
    if expected != plan.plan_id or plan.confirmation_token != f"MIGRATE-{expected[:12].upper()}":
        raise DataMigrationError("Data-migration plan integrity check failed.")
    return plan


def build_data_migration_plan_digest(plan: DataMigrationPlan) -> str:
    """Return the stable identity of a serialized plan."""

    return _plan_digest(
        {
            "format": plan.format,
            "version": plan.version,
            "source_database": plan.source_database,
            "target_database": plan.target_database,
            "target_root": plan.target_root,
            "files": [asdict(entry) for entry in plan.files],
            "database_candidates": [asdict(item) for item in plan.database_candidates],
            "notices": [asdict(item) for item in plan.notices],
            "ignored_paths": list(plan.ignored_paths),
            "payload_bytes": plan.payload_bytes,
            "minimum_free_bytes": plan.minimum_free_bytes,
        }
    )


def _source_stat_matches(entry: MigrationFile) -> bool:
    source = Path(entry.source)
    if not source.is_file() or source.is_symlink():
        return False
    stat_result = source.stat()
    return (
        stat_result.st_size == entry.size
        and stat_result.st_mtime_ns == entry.modified_ns
    )


def _safe_target(target_root: Path, destination: str) -> Path:
    relative = _relative_destination(destination)
    target = target_root.joinpath(*relative.parts).resolve()
    try:
        target.relative_to(target_root)
    except ValueError as exc:
        raise DataMigrationError(f"Migration target escapes data root: {target}") from exc
    return target


def _journal_path(target_root: Path, plan_id: str) -> Path:
    return target_root / "state" / "data-migrations" / f"{plan_id}.json"


def _initial_journal(plan: DataMigrationPlan) -> dict[str, Any]:
    now = _utc_now()
    return {
        "format": MIGRATION_JOURNAL_FORMAT,
        "version": MIGRATION_JOURNAL_VERSION,
        "plan_id": plan.plan_id,
        "status": "planned",
        "created_at": now,
        "updated_at": now,
        "completed_files": {},
        "database": {"status": "pending"},
        "safety_backup": None,
        "error": None,
    }


def _load_journal(path: Path, plan: DataMigrationPlan) -> dict[str, Any]:
    if not path.exists():
        return _initial_journal(plan)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DataMigrationError(f"Migration journal is unreadable: {path}") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("format") != MIGRATION_JOURNAL_FORMAT
        or payload.get("version") != MIGRATION_JOURNAL_VERSION
        or payload.get("plan_id") != plan.plan_id
    ):
        raise DataMigrationError(f"Migration journal does not match the plan: {path}")
    if not isinstance(payload.get("completed_files"), dict):
        raise DataMigrationError(f"Migration journal file records are invalid: {path}")
    return payload


def _save_journal(path: Path, journal: dict[str, Any]) -> None:
    journal["updated_at"] = _utc_now()
    _atomic_json_write(path, journal)


def _journaled_payload_bytes(
    plan: DataMigrationPlan,
    journal: dict[str, Any],
) -> int:
    planned_sizes = {entry.key: entry.size for entry in plan.files}
    durable_bytes = 0
    for key, record in journal["completed_files"].items():
        if not isinstance(record, dict):
            continue
        size = record.get("size")
        if isinstance(size, int) and size >= 0 and planned_sizes.get(key) == size:
            durable_bytes += size

    database = journal.get("database")
    if isinstance(database, dict) and database.get("status") in {"staged", "complete"}:
        size = database.get("size")
        if isinstance(size, int) and size >= 0:
            durable_bytes += size

    backup = journal.get("safety_backup")
    if isinstance(backup, dict):
        backup_path = backup.get("path")
        if isinstance(backup_path, str):
            try:
                durable_bytes += Path(backup_path).stat().st_size
            except OSError:
                pass
    return durable_bytes


def _copy_and_hash(entry: MigrationFile, target: Path, plan_id: str) -> str:
    if not _source_stat_matches(entry):
        raise DataMigrationConflict(f"Migration source changed after planning: {entry.source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.migration-{plan_id[:12]}.tmp")
    if temporary.exists():
        temporary.unlink()
    digest = hashlib.sha256()
    try:
        with Path(entry.source).open("rb") as source, temporary.open("xb") as sink:
            while chunk := source.read(_COPY_CHUNK_SIZE):
                digest.update(chunk)
                sink.write(chunk)
            sink.flush()
            os.fsync(sink.fileno())
        if not _source_stat_matches(entry):
            raise DataMigrationConflict(
                f"Migration source changed while it was copied: {entry.source}"
            )
        if temporary.stat().st_size != entry.size:
            raise DataMigrationConflict(
                f"Migration copy has an unexpected size: {temporary}"
            )
        if target.exists():
            raise DataMigrationConflict(
                f"Migration target appeared while copying: {target}"
            )
        os.replace(temporary, target)
        return digest.hexdigest()
    finally:
        temporary.unlink(missing_ok=True)


def _safety_paths(paths: AppPaths, source_database: Path) -> AppPaths:
    instance = source_database.parent
    return replace(
        paths,
        data_root=instance,
        instance_dir=instance,
        database_dir=instance,
        database_file=source_database,
        artifacts_dir=instance / "artifacts",
        addons_dir=instance / "addons",
        installed_addons_dir=instance / "addons" / "installed",
        addon_data_dir=instance / "addons" / "data",
        addon_logs_dir=paths.log_dir / "addons",
        state_dir=instance / "state",
    )


def _invoke_fault(
    fault_injector: FaultInjector | None,
    stage: str,
    entry: MigrationFile | None = None,
) -> None:
    if fault_injector is not None:
        fault_injector(stage, entry)


def apply_data_migration(
    plan: DataMigrationPlan,
    source_paths: AppPaths,
    *,
    confirmation: str,
    create_safety_backup: bool = True,
    key_store: InstallationKeyStore | None = None,
    fault_injector: FaultInjector | None = None,
) -> DataMigrationResult:
    """Apply or resume an exact plan while keeping legacy source state intact."""

    if confirmation != plan.confirmation_token:
        raise DataMigrationConfirmationRequired(
            f"Exact confirmation required: {plan.confirmation_token}"
        )
    if not plan.applicable:
        blocking = "; ".join(
            notice.message for notice in plan.notices if notice.blocking
        )
        raise DataMigrationError(f"Migration plan is not applicable: {blocking}")
    if build_data_migration_plan_digest(plan) != plan.plan_id:
        raise DataMigrationError("Migration plan integrity check failed.")

    target_root = Path(plan.target_root).expanduser().resolve()
    source_database = Path(plan.source_database).expanduser().resolve()
    target_database = Path(plan.target_database).expanduser().resolve()
    if target_database != target_root / "database" / "carlina.db":
        raise DataMigrationError("Migration database target is not canonical.")
    if not _is_sqlite_file(source_database):
        raise DataMigrationConflict(
            f"Selected source database is no longer viable: {source_database}"
        )
    selected = next(
        (candidate for candidate in plan.database_candidates if candidate.selected),
        None,
    )
    if selected is None:
        raise DataMigrationError("Migration plan has no selected database candidate.")
    source_stat = source_database.stat()
    if (
        source_stat.st_size != selected.size
        or source_stat.st_mtime_ns != selected.modified_ns
    ):
        raise DataMigrationConflict(
            "Selected source database changed after the plan was created."
        )

    target_root.mkdir(parents=True, exist_ok=True)
    lock_path = target_root / "locks" / "migrate-data.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    journal_path = _journal_path(target_root, plan.plan_id)
    lock = FileLock(lock_path, timeout=0)
    try:
        lock.acquire()
    except Timeout as exc:
        raise DataMigrationError("Another data migration is already running.") from exc

    safety_backup: BackupRecord | None = None
    copied_files = 0
    reused_files = 0
    journal: dict[str, Any] | None = None
    try:
        journal = _load_journal(journal_path, plan)
        if journal.get("status") == "complete":
            if not target_database.is_file():
                raise DataMigrationConflict(
                    "Completed migration journal exists but target database is missing."
                )
            return DataMigrationResult(
                plan.plan_id,
                target_root,
                target_database,
                0,
                len(plan.files),
                plan.payload_bytes,
                None,
                journal_path,
            )

        free_bytes = shutil.disk_usage(target_root).free
        journaled_bytes = _journaled_payload_bytes(plan, journal)
        remaining_minimum_free_bytes = max(
            _FREE_SPACE_MARGIN,
            plan.minimum_free_bytes - journaled_bytes,
        )
        if free_bytes < remaining_minimum_free_bytes:
            raise DataMigrationError(
                "Insufficient free space for a copy-first migration: "
                f"need at least {remaining_minimum_free_bytes} bytes for the "
                f"remaining work; have {free_bytes}."
            )
        journal["status"] = "running"
        journal["error"] = None
        _save_journal(journal_path, journal)

        backup_payload = journal.get("safety_backup")
        if create_safety_backup and not backup_payload:
            safety_backup = create_backup(
                _safety_paths(source_paths, source_database),
                key_store=key_store,
            )
            journal["safety_backup"] = {
                "path": str(safety_backup.path),
                "sha256": safety_backup.sha256,
                "backup_id": safety_backup.backup_id,
            }
            _save_journal(journal_path, journal)

        completed_files: dict[str, Any] = journal["completed_files"]
        pending_checkpoint_files = 0
        for entry in plan.files:
            target = _safe_target(target_root, entry.destination)
            completed = completed_files.get(entry.key)
            if completed is not None:
                expected_digest = str(completed.get("sha256") or "")
                if (
                    not target.is_file()
                    or target.stat().st_size != entry.size
                    or _sha256_file(target) != expected_digest
                    or not _source_stat_matches(entry)
                    or _sha256_file(Path(entry.source)) != expected_digest
                ):
                    raise DataMigrationConflict(
                        f"A completed migration file no longer matches: {target}"
                    )
                reused_files += 1
                continue
            if target.exists():
                if not target.is_file() or target.is_symlink():
                    raise DataMigrationConflict(
                        f"Migration target conflicts with an existing path: {target}"
                    )
                source_digest = _sha256_file(Path(entry.source))
                if target.stat().st_size != entry.size or _sha256_file(target) != source_digest:
                    raise DataMigrationConflict(
                        f"Migration target contains different data: {target}"
                    )
                digest = source_digest
                reused_files += 1
            else:
                digest = _copy_and_hash(entry, target, plan.plan_id)
                copied_files += 1
            completed_files[entry.key] = {
                "source": entry.source,
                "sha256": digest,
                "size": entry.size,
                "completed_at": _utc_now(),
            }
            pending_checkpoint_files += 1
            if pending_checkpoint_files >= _JOURNAL_CHECKPOINT_FILE_COUNT:
                _save_journal(journal_path, journal)
                pending_checkpoint_files = 0
            _invoke_fault(fault_injector, "after_file", entry)

        if pending_checkpoint_files:
            _save_journal(journal_path, journal)

        database_journal = journal["database"]
        _invoke_fault(fault_injector, "before_database_snapshot")
        if database_journal.get("status") == "complete":
            expected = str(database_journal.get("sha256") or "")
            if not target_database.is_file() or _sha256_file(target_database) != expected:
                raise DataMigrationConflict(
                    "The activated target database no longer matches the journal."
                )
        else:
            target_database.parent.mkdir(parents=True, exist_ok=True)
            staged_database = target_database.with_name(
                f".{target_database.name}.migration-{plan.plan_id[:12]}.tmp"
            )
            staged_digest = str(database_journal.get("sha256") or "")
            if database_journal.get("status") == "staged":
                if (
                    target_database.is_file()
                    and not staged_database.exists()
                    and _sha256_file(target_database) == staged_digest
                ):
                    inspect_database(target_database)
                    database_journal.update(
                        {
                            "status": "complete",
                            "completed_at": _utc_now(),
                        }
                    )
                    _save_journal(journal_path, journal)
                elif not staged_database.is_file() or _sha256_file(staged_database) != staged_digest:
                    raise DataMigrationConflict(
                        "The staged migration database no longer matches the journal."
                    )
            if database_journal.get("status") == "pending":
                staged_database.unlink(missing_ok=True)
                snapshot_database(source_database, staged_database)
                after_snapshot = source_database.stat()
                if (
                    after_snapshot.st_size != selected.size
                    or after_snapshot.st_mtime_ns != selected.modified_ns
                ):
                    staged_database.unlink(missing_ok=True)
                    raise DataMigrationConflict(
                        "Source database changed while the snapshot was created."
                    )
                staged_digest = _sha256_file(staged_database)
                database_journal.update(
                    {
                        "status": "staged",
                        "sha256": staged_digest,
                        "size": staged_database.stat().st_size,
                        "staged_at": _utc_now(),
                    }
                )
                _save_journal(journal_path, journal)
            if database_journal.get("status") == "staged":
                _invoke_fault(fault_injector, "after_database_stage")
                if target_database.exists():
                    raise DataMigrationConflict(
                        f"Target database already exists and was not activated by this plan: {target_database}"
                    )
                os.replace(staged_database, target_database)
                inspect_database(target_database)
                database_journal.update(
                    {
                        "status": "complete",
                        "sha256": staged_digest,
                        "completed_at": _utc_now(),
                    }
                )
                _save_journal(journal_path, journal)
                _invoke_fault(fault_injector, "after_database_activation")

        journal["status"] = "complete"
        journal["completed_at"] = _utc_now()
        _save_journal(journal_path, journal)
        return DataMigrationResult(
            plan.plan_id,
            target_root,
            target_database,
            copied_files,
            reused_files,
            plan.payload_bytes,
            safety_backup,
            journal_path,
        )
    except BaseException as exc:
        if journal is not None:
            journal["status"] = "interrupted"
            journal["error"] = {
                "type": type(exc).__name__,
                "message": str(exc),
                "at": _utc_now(),
            }
            _save_journal(journal_path, journal)
        raise
    finally:
        lock.release()


__all__ = [
    "MIGRATION_JOURNAL_FORMAT",
    "MIGRATION_JOURNAL_VERSION",
    "MIGRATION_PLAN_FORMAT",
    "MIGRATION_PLAN_VERSION",
    "DataMigrationConfirmationRequired",
    "DataMigrationConflict",
    "DataMigrationError",
    "DataMigrationPlan",
    "DataMigrationResult",
    "DatabaseCandidate",
    "MigrationFile",
    "MigrationNotice",
    "apply_data_migration",
    "build_data_migration_plan",
    "build_data_migration_plan_digest",
    "load_data_migration_plan",
    "write_data_migration_plan",
]
