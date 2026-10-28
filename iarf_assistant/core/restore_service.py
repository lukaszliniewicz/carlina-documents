"""Durable scheduling and offline activation for verified Carlina restores."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from filelock import FileLock, Timeout

from iarf_assistant.core.app_paths import AppPaths
from iarf_assistant.core.backup_service import (
    BACKUP_VERSION,
    BackupError,
    BackupRecord,
    RestoreActivationWitness,
    RestoreResult,
    create_backup,
    resolve_backup_base,
    restore_backup,
    restore_safety_root,
    restore_staging_root,
    verify_backup,
)
from iarf_assistant.core.encryption import InstallationKeyStore

PENDING_RESTORE_FORMAT = "carlina.pending-restore"
PENDING_RESTORE_VERSION = 1
PENDING_RESTORE_FILENAME = "pending-restore.json"
RESTORE_HANDOFF_FORMAT = "carlina.restore-handoff"
RESTORE_HANDOFF_VERSION = 2
HANDOFF_PREPARED = "prepared"
HANDOFF_ACTIVATING = "activating"
HANDOFF_ACTIVATED = "activated"
HANDOFF_APPLIED = "applied"
HANDOFF_CANCELLED = "cancelled"
RESTORE_HANDOFF_WITNESS_PREFIX = ".restore-handoff-witness-"


class PendingRestoreError(BackupError):
    """A scheduled restore record is missing, unsafe, or inconsistent."""


class PendingRestoreExists(PendingRestoreError):
    """Only one verified restore may be pending at a time."""


@dataclass(frozen=True, slots=True)
class PendingRestore:
    format: str
    version: int
    archive_filename: str
    archive_sha256: str
    backup_id: str
    requested_at: str
    requested_by_user_id: int | None
    safety_backup_filename: str
    safety_backup_sha256: str
    archive_base_filename: str | None = None
    archive_base_sha256: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        if self.archive_base_filename is None:
            payload.pop("archive_base_filename")
            payload.pop("archive_base_sha256")
        return payload


@dataclass(frozen=True, slots=True)
class RestoreHandoff:
    """Durable record that spans an atomic data-root activation."""

    format: str
    version: int
    phase: str
    pending: PendingRestore
    archive_path: str
    safety_backup_path: str
    source_data_root: str
    journal_path: str
    applied_record_filename: str
    cancelled_record_filename: str
    safety_root: str | None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["pending"] = self.pending.to_dict()
        return payload


@dataclass(frozen=True, slots=True)
class ScheduledRestore:
    pending: PendingRestore
    safety_backup: BackupRecord
    pending_path: Path


@dataclass(frozen=True, slots=True)
class AppliedRestore:
    pending: PendingRestore
    restore: RestoreResult
    destination_paths: AppPaths
    applied_record_path: Path | None


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(4 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json_write(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    _fsync_directory(path.parent)


def _fsync_directory(path: Path) -> None:
    """Persist a directory mutation on POSIX; Windows is best-effort.

    Fedora/POSIX directory handles support ``fsync`` after a rename or unlink,
    which closes the metadata durability window. Python does not expose a
    portable Windows directory-handle flush, so Windows keeps the already
    fsynced file payload but treats the parent-directory update as best-effort.
    """

    if not _directory_fsync_supported():
        return
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _directory_fsync_supported() -> bool:
    return os.name != "nt"


def _unlink_file(path: Path) -> None:
    path.unlink()
    _fsync_directory(path.parent)


def pending_restore_path(paths: AppPaths) -> Path:
    return paths.state_dir / PENDING_RESTORE_FILENAME


def _restore_handoff_path(destination: Path) -> Path:
    """Return the bounded sibling record that survives a root switch."""

    token = _restore_lifecycle_token(destination)
    return destination.parent / f".restore-handoff-{token}.json"


def _restore_lifecycle_token(destination: Path) -> str:
    return hashlib.sha256(str(destination).encode("utf-8")).hexdigest()[:16]


def _restore_lifecycle_lock(destination: Path) -> FileLock:
    """Return the stable sibling lock for all restore lifecycle mutations."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    token = _restore_lifecycle_token(destination)
    return FileLock(destination.parent / f".restore-lifecycle-{token}.lock", timeout=0)


def _acquire_restore_lifecycle_lock(destination: Path) -> FileLock:
    lock = _restore_lifecycle_lock(destination)
    try:
        lock.acquire()
    except Timeout as exc:
        raise PendingRestoreError(
            "Another restore lifecycle operation is running."
        ) from exc
    return lock


def _known_handoff_source_roots(paths: AppPaths, destination: Path) -> set[Path]:
    roots = {paths.data_root.resolve(), destination}
    roots.update(
        location.resolve()
        for location in paths.legacy_locations
        if location.name == "instance"
    )
    return roots


def _pending_from_payload(payload: object, *, source: Path) -> PendingRestore:
    if (
        not isinstance(payload, dict)
        or payload.get("format") != PENDING_RESTORE_FORMAT
        or payload.get("version") != PENDING_RESTORE_VERSION
    ):
        raise PendingRestoreError(f"Unsupported pending restore record: {source}")
    normalized = dict(payload)
    has_base_filename = "archive_base_filename" in normalized
    has_base_sha256 = "archive_base_sha256" in normalized
    if has_base_filename != has_base_sha256:
        raise PendingRestoreError(f"Malformed pending restore record: {source}")
    try:
        pending = PendingRestore(**normalized)
    except (TypeError, ValueError) as exc:
        raise PendingRestoreError(
            f"Malformed pending restore record: {source}"
        ) from exc
    if len(pending.archive_sha256) != 64 or len(pending.safety_backup_sha256) != 64:
        raise PendingRestoreError(
            f"Pending restore contains invalid archive hashes: {source}"
        )
    if (pending.archive_base_filename is None) != (pending.archive_base_sha256 is None):
        raise PendingRestoreError(f"Malformed pending restore record: {source}")
    if pending.archive_base_filename is not None and (
        Path(pending.archive_base_filename).name != pending.archive_base_filename
        or not pending.archive_base_filename
        or pending.archive_base_sha256 is None
        or len(pending.archive_base_sha256) != 64
    ):
        raise PendingRestoreError(
            f"Pending restore contains an invalid base reference: {source}"
        )
    return pending


def _absolute_handoff_path(value: object, *, field: str, source: Path) -> Path:
    if not isinstance(value, str) or not value:
        raise PendingRestoreError(f"Restore handoff has an invalid {field}: {source}")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise PendingRestoreError(f"Restore handoff {field} must be absolute: {source}")
    return path.resolve()


def _load_restore_handoff(
    path: Path,
    *,
    paths: AppPaths,
    destination: Path,
) -> RestoreHandoff | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PendingRestoreError(f"Restore handoff is unreadable: {path}") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("format") != RESTORE_HANDOFF_FORMAT
        or payload.get("version") != RESTORE_HANDOFF_VERSION
    ):
        raise PendingRestoreError(f"Unsupported restore handoff: {path}")
    pending = _pending_from_payload(payload.get("pending"), source=path)
    phase = payload.get("phase")
    if phase not in {
        HANDOFF_PREPARED,
        HANDOFF_ACTIVATING,
        HANDOFF_ACTIVATED,
        HANDOFF_APPLIED,
        HANDOFF_CANCELLED,
    }:
        raise PendingRestoreError(f"Restore handoff has an invalid phase: {path}")
    source_data_root = _absolute_handoff_path(
        payload.get("source_data_root"),
        field="source data root",
        source=path,
    )
    if source_data_root not in _known_handoff_source_roots(paths, destination):
        raise PendingRestoreError(f"Restore handoff has an unknown source root: {path}")
    archive_path = _absolute_handoff_path(
        payload.get("archive_path"),
        field="archive path",
        source=path,
    )
    safety_backup_path = _absolute_handoff_path(
        payload.get("safety_backup_path"),
        field="safety backup path",
        source=path,
    )
    journal_path = _absolute_handoff_path(
        payload.get("journal_path"),
        field="journal path",
        source=path,
    )
    backup_root = source_data_root / "backups"
    if archive_path != backup_root / pending.archive_filename:
        raise PendingRestoreError(f"Restore handoff archive path mismatch: {path}")
    if safety_backup_path != backup_root / pending.safety_backup_filename:
        raise PendingRestoreError(
            f"Restore handoff safety backup path mismatch: {path}"
        )
    expected_journal_path = source_data_root / "state" / PENDING_RESTORE_FILENAME
    applied_record_filename = payload.get("applied_record_filename")
    if (
        not isinstance(applied_record_filename, str)
        or Path(applied_record_filename).name != applied_record_filename
        or not applied_record_filename.startswith(f"{pending.backup_id}-")
        or not applied_record_filename.endswith(".json")
    ):
        raise PendingRestoreError(
            f"Restore handoff has an invalid applied record name: {path}"
        )
    cancelled_record_filename = payload.get("cancelled_record_filename")
    if (
        not isinstance(cancelled_record_filename, str)
        or Path(cancelled_record_filename).name != cancelled_record_filename
        or not cancelled_record_filename.startswith(f"{pending.backup_id}-")
        or not cancelled_record_filename.endswith(".json")
    ):
        raise PendingRestoreError(
            f"Restore handoff has an invalid cancelled record name: {path}"
        )
    safety_root_value = payload.get("safety_root")
    safety_root = None
    if safety_root_value is not None:
        safety_root = str(
            _absolute_handoff_path(
                safety_root_value,
                field="safety root",
                source=path,
            )
        )
    if (
        phase
        in {
            HANDOFF_PREPARED,
            HANDOFF_ACTIVATING,
            HANDOFF_CANCELLED,
        }
        and safety_root is not None
    ):
        raise PendingRestoreError(f"Inactive restore handoff has a safety root: {path}")
    if safety_root is not None:
        safety_root_path = Path(safety_root)
        if (
            safety_root_path.parent != destination.parent
            or not safety_root_path.name.startswith(".pre-restore-")
            or safety_root_path != restore_safety_root(destination, pending.backup_id)
        ):
            raise PendingRestoreError(
                f"Restore handoff has an invalid safety root: {path}"
            )
    if journal_path != expected_journal_path:
        raise PendingRestoreError(f"Restore handoff journal path mismatch: {path}")
    return RestoreHandoff(
        format=RESTORE_HANDOFF_FORMAT,
        version=RESTORE_HANDOFF_VERSION,
        phase=phase,
        pending=pending,
        archive_path=str(archive_path),
        safety_backup_path=str(safety_backup_path),
        source_data_root=str(source_data_root),
        journal_path=str(journal_path),
        applied_record_filename=applied_record_filename,
        cancelled_record_filename=cancelled_record_filename,
        safety_root=safety_root,
    )


def _write_restore_handoff(path: Path, handoff: RestoreHandoff) -> None:
    _atomic_json_write(path, handoff.to_dict())


def _new_restore_handoff(
    pending: PendingRestore,
    *,
    archive: Path,
    safety_backup: Path,
    source_data_root: Path,
    journal_path: Path,
) -> RestoreHandoff:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return RestoreHandoff(
        format=RESTORE_HANDOFF_FORMAT,
        version=RESTORE_HANDOFF_VERSION,
        phase=HANDOFF_PREPARED,
        pending=pending,
        archive_path=str(archive.resolve()),
        safety_backup_path=str(safety_backup.resolve()),
        source_data_root=str(source_data_root.resolve()),
        journal_path=str(journal_path.resolve()),
        applied_record_filename=f"{pending.backup_id}-{timestamp}.json",
        cancelled_record_filename=f"{pending.backup_id}-{timestamp}.json",
        safety_root=None,
    )


def _safe_backup_path(paths: AppPaths, filename: str) -> Path:
    if Path(filename).name != filename or not filename:
        raise PendingRestoreError("Pending restore has an unsafe archive filename.")
    archive = (paths.backups_dir / filename).resolve()
    try:
        archive.relative_to(paths.backups_dir.resolve())
    except ValueError as exc:
        raise PendingRestoreError(
            "Pending restore archive escapes the backup directory."
        ) from exc
    return archive


def load_pending_restore(paths: AppPaths) -> PendingRestore | None:
    """Load and structurally validate the pending restore, if any."""

    pending_path = pending_restore_path(paths)
    if not pending_path.exists():
        return None
    try:
        payload = json.loads(pending_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PendingRestoreError(
            f"Pending restore record is unreadable: {pending_path}"
        ) from exc
    pending = _pending_from_payload(payload, source=pending_path)
    _safe_backup_path(paths, pending.archive_filename)
    _safe_backup_path(paths, pending.safety_backup_filename)
    if pending.archive_base_filename is not None:
        _safe_backup_path(paths, pending.archive_base_filename)
    return pending


def schedule_restore(
    paths: AppPaths,
    archive_path: str | os.PathLike[str],
    *,
    key_store: InstallationKeyStore | None = None,
    requested_by_user_id: int | None = None,
) -> ScheduledRestore:
    """Verify an archive, create a safety backup, and durably schedule restore."""

    destination = _restore_destination(paths)
    lock = _acquire_restore_lifecycle_lock(destination)
    try:
        paths.ensure_directories()
        pending_path = pending_restore_path(paths)
        handoff_path = _restore_handoff_path(destination)
        handoff = _load_restore_handoff(
            handoff_path,
            paths=paths,
            destination=destination,
        )
        pending = load_pending_restore(paths)
        if handoff is not None:
            raise PendingRestoreExists(
                "A restore handoff is unfinished; apply or cancel it before scheduling another restore."
            )
        if pending is not None:
            archive = _safe_backup_path(paths, pending.archive_filename)
            safety_backup = _safe_backup_path(paths, pending.safety_backup_filename)
            _write_restore_handoff(
                handoff_path,
                _new_restore_handoff(
                    pending,
                    archive=archive,
                    safety_backup=safety_backup,
                    source_data_root=paths.data_root,
                    journal_path=pending_path,
                ),
            )
            raise PendingRestoreExists(
                "A verified restore is already pending; apply or cancel it first."
            )
        archive = Path(archive_path).expanduser().resolve()
        try:
            archive.relative_to(paths.backups_dir.resolve())
        except ValueError as exc:
            raise PendingRestoreError(
                "Only archives in the managed backup directory may be scheduled."
            ) from exc
        if archive.parent != paths.backups_dir.resolve():
            raise PendingRestoreError(
                "Scheduled archives must be direct children of the managed backup directory."
            )
        active_store = key_store or InstallationKeyStore(paths.master_keys_file)
        manifest = verify_backup(archive, available_key_ids=active_store.key_ids())
        archive_base_filename: str | None = None
        archive_base_sha256: str | None = None
        if manifest["version"] != BACKUP_VERSION:
            base_archive, _base_manifest = resolve_backup_base(
                archive,
                manifest,
                available_key_ids=active_store.key_ids(),
            )
            archive_base_filename = base_archive.name
            archive_base_sha256 = str(manifest["base"]["sha256"])
        safety_backup = create_backup(paths, key_store=active_store)
        pending = PendingRestore(
            format=PENDING_RESTORE_FORMAT,
            version=PENDING_RESTORE_VERSION,
            archive_filename=archive.name,
            archive_sha256=_sha256_file(archive),
            backup_id=str(manifest["backup_id"]),
            requested_at=_now(),
            requested_by_user_id=requested_by_user_id,
            safety_backup_filename=safety_backup.path.name,
            safety_backup_sha256=safety_backup.sha256,
            archive_base_filename=archive_base_filename,
            archive_base_sha256=archive_base_sha256,
        )
        _atomic_json_write(pending_path, pending.to_dict())
        _write_restore_handoff(
            handoff_path,
            _new_restore_handoff(
                pending,
                archive=archive,
                safety_backup=safety_backup.path,
                source_data_root=paths.data_root,
                journal_path=pending_path,
            ),
        )
        return ScheduledRestore(pending, safety_backup, pending_path)
    finally:
        lock.release()


def _restore_destination(paths: AppPaths) -> Path:
    return (
        paths.migration_target_root.resolve()
        if paths.is_legacy
        else paths.data_root.resolve()
    )


def _destination_paths(paths: AppPaths, destination: Path) -> AppPaths:
    if destination == paths.data_root.resolve():
        return paths
    return replace(
        paths,
        mode="platform",
        data_root=destination,
        instance_dir=destination,
        database_dir=destination / "database",
        database_file=destination / "database" / "carlina.db",
        uploads_dir=destination / "uploads",
        artifacts_dir=destination / "artifacts",
        addons_dir=destination / "addons",
        installed_addons_dir=destination / "addons" / "installed",
        addon_data_dir=destination / "addons" / "data",
        addon_logs_dir=paths.log_dir / "addons",
        backups_dir=destination / "backups",
        staging_dir=destination / "staging",
        locks_dir=destination / "locks",
        secrets_dir=destination / "secrets",
        state_dir=destination / "state",
        temp_dir=destination / "tmp",
        master_keys_file=destination / "secrets" / "master-keys.json",
        session_key_file=destination / "secrets" / "session.key",
        storage_manifest_file=destination / "state" / "storage-layout.json",
    )


def _archive_applied_record(
    source: Path,
    destination: Path,
    pending: PendingRestore,
) -> Path:
    """Move the verified journal into the active root after activation."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        _validate_journal_contents(destination, pending)
        if source.exists():
            _validate_journal_contents(source, pending)
            _unlink_file(source)
        return destination
    if not source.is_file():
        raise PendingRestoreError(
            "Pending restore record disappeared during activation."
        )
    _validate_journal_contents(source, pending)
    try:
        os.replace(source, destination)
        _fsync_directory(destination.parent)
        if source.parent != destination.parent:
            _fsync_directory(source.parent)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        _copy_journal_across_filesystems(source, destination)
    return destination


def _validate_journal_contents(path: Path, pending: PendingRestore) -> None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PendingRestoreError(
            f"Pending restore journal is unreadable: {path}"
        ) from exc
    if payload != pending.to_dict():
        raise PendingRestoreError(
            f"Pending restore journal does not match its handoff: {path}"
        )


def _copy_journal_across_filesystems(source: Path, destination: Path) -> None:
    """Durably copy a journal before removing its cross-device source."""

    temporary = destination.with_name(f".{destination.name}.tmp-{os.getpid()}")
    try:
        with (
            source.open("rb") as input_handle,
            temporary.open("xb") as output_handle,
        ):
            shutil.copyfileobj(input_handle, output_handle)
            output_handle.flush()
            os.fsync(output_handle.fileno())
        os.replace(temporary, destination)
        _fsync_directory(destination.parent)
        _unlink_file(source)
    except BaseException:
        if temporary.exists():
            _unlink_file(temporary)
        raise


def _activation_witness_filename(pending: PendingRestore) -> str:
    """Return a collision-resistant, journal-bound witness basename."""

    token = hashlib.sha256(
        json.dumps(
            pending.to_dict(),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:16]
    return f"{RESTORE_HANDOFF_WITNESS_PREFIX}{token}.json"


def _activation_witness_path(destination: Path, pending: PendingRestore) -> Path:
    return destination / "state" / _activation_witness_filename(pending)


def _activation_witness_payload(pending: PendingRestore) -> bytes:
    return (
        json.dumps(pending.to_dict(), indent=2, sort_keys=True).encode("utf-8") + b"\n"
    )


def _activation_witness(pending: PendingRestore) -> RestoreActivationWitness:
    return RestoreActivationWitness(
        relative_path=(
            Path("state") / _activation_witness_filename(pending)
        ).as_posix(),
        payload=_activation_witness_payload(pending),
    )


def _is_legacy_handoff(handoff: RestoreHandoff, destination: Path) -> bool:
    return Path(handoff.source_data_root) != destination


def _remove_duplicate_source_journal(
    handoff: RestoreHandoff,
    destination: Path,
    applied_record: Path,
) -> None:
    """Remove the old journal only after the destination proof is archived."""

    _validate_journal_contents(applied_record, handoff.pending)
    if _is_legacy_handoff(handoff, destination):
        source = Path(handoff.journal_path)
    else:
        if handoff.safety_root is None:
            raise PendingRestoreError(
                "Activated in-place restore has no safety root for its source journal."
            )
        source = Path(handoff.safety_root) / "state" / PENDING_RESTORE_FILENAME
    if source.exists():
        _validate_journal_contents(source, handoff.pending)
        _unlink_file(source)


def _cancelled_record_path(handoff: RestoreHandoff) -> Path:
    return (
        Path(handoff.source_data_root)
        / "state"
        / "cancelled-restores"
        / handoff.cancelled_record_filename
    )


def _reconcile_cancelled_handoff(
    handoff_path: Path,
    handoff: RestoreHandoff,
) -> RestoreHandoff | None:
    """Promote a journal move that completed before its cancellation phase."""

    if handoff.phase != HANDOFF_PREPARED:
        return None
    cancelled_record = _cancelled_record_path(handoff)
    if not cancelled_record.is_file():
        return None
    _archive_applied_record(
        Path(handoff.journal_path),
        cancelled_record,
        handoff.pending,
    )
    cancelled = replace(handoff, phase=HANDOFF_CANCELLED)
    _write_restore_handoff(handoff_path, cancelled)
    return cancelled


def _destination_has_activation_witness(
    destination: Path,
    pending: PendingRestore,
) -> bool:
    """Return true only for the exact journal-bound post-switch proof."""

    witness = _activation_witness_path(destination, pending)
    if not witness.exists():
        return False
    _validate_journal_contents(witness, pending)
    return True


def _handoff_backup_path(
    handoff: RestoreHandoff,
    destination: Path,
    original: Path,
) -> Path:
    """Find a managed archive after an in-place root switch moved catalogs."""

    if original.is_file() or _is_legacy_handoff(handoff, destination):
        return original
    for root in (
        restore_safety_root(destination, handoff.pending.backup_id),
        restore_staging_root(destination, handoff.pending.backup_id),
    ):
        candidate = root / "backups" / original.name
        if candidate.is_file():
            return candidate
    return original


def _validate_handoff_archives(
    handoff: RestoreHandoff,
    destination: Path,
) -> tuple[Path, Path, Path | None]:
    archive = _handoff_backup_path(
        handoff,
        destination,
        Path(handoff.archive_path),
    )
    safety_backup = _handoff_backup_path(
        handoff,
        destination,
        Path(handoff.safety_backup_path),
    )
    if not archive.is_file() or _sha256_file(archive) != handoff.pending.archive_sha256:
        raise PendingRestoreError(
            "Scheduled restore archive is missing or changed; refusing activation."
        )
    try:
        manifest = verify_backup(archive)
    except BackupError as exc:
        raise PendingRestoreError(
            "Scheduled restore archive cannot be verified; refusing activation."
        ) from exc
    if (
        not safety_backup.is_file()
        or _sha256_file(safety_backup) != handoff.pending.safety_backup_sha256
    ):
        raise PendingRestoreError(
            "Scheduled restore safety backup is missing or changed; refusing activation."
        )
    base_archive: Path | None = None
    if manifest["version"] != BACKUP_VERSION:
        if (
            handoff.pending.archive_base_filename is None
            or handoff.pending.archive_base_sha256 is None
            or manifest["base"].get("filename") != handoff.pending.archive_base_filename
            or manifest["base"].get("sha256") != handoff.pending.archive_base_sha256
        ):
            raise PendingRestoreError(
                "Scheduled restore archive base does not match its handoff."
            )
        base_archive = archive.parent / handoff.pending.archive_base_filename
        if (
            not base_archive.is_file()
            or _sha256_file(base_archive) != handoff.pending.archive_base_sha256
        ):
            raise PendingRestoreError(
                "Scheduled restore archive base is missing or changed; refusing activation."
            )
    elif handoff.pending.archive_base_filename is not None:
        raise PendingRestoreError(
            "Complete scheduled restore archive has an unexpected base reference."
        )
    return archive, safety_backup, base_archive


def _activate_handoff(
    handoff: RestoreHandoff,
    destination: Path,
    *,
    key_store: InstallationKeyStore,
) -> RestoreResult:
    """Finish an ACTIVATING handoff without treating old roots as evidence."""

    archive, _safety_backup, _base_archive = _validate_handoff_archives(
        handoff,
        destination,
    )
    restore = restore_backup(
        archive,
        destination,
        key_store=key_store,
        activate=True,
        activation_witness=_activation_witness(handoff.pending),
    )
    if not _destination_has_activation_witness(destination, handoff.pending):
        raise PendingRestoreError(
            "Restore activation completed without its destination-side witness."
        )
    return restore


def apply_pending_restore(
    paths: AppPaths,
    *,
    key_store: InstallationKeyStore | None = None,
) -> AppliedRestore | None:
    """Apply a scheduled restore offline using a staged atomic root switch."""

    destination = _restore_destination(paths)
    lock = _acquire_restore_lifecycle_lock(destination)
    try:
        handoff_path = _restore_handoff_path(destination)
        handoff = _load_restore_handoff(
            handoff_path,
            paths=paths,
            destination=destination,
        )
        pending = load_pending_restore(paths)
        if pending is None and handoff is None:
            return None
        if handoff is None:
            assert pending is not None
            archive = _safe_backup_path(paths, pending.archive_filename)
            safety_backup = _safe_backup_path(paths, pending.safety_backup_filename)
            handoff = _new_restore_handoff(
                pending,
                archive=archive,
                safety_backup=safety_backup,
                source_data_root=paths.data_root,
                journal_path=pending_restore_path(paths),
            )
            _write_restore_handoff(handoff_path, handoff)
        elif pending is not None and pending != handoff.pending:
            raise PendingRestoreError(
                "Pending restore does not match the durable restore handoff."
            )

        reconciled_cancellation = _reconcile_cancelled_handoff(
            handoff_path,
            handoff,
        )
        if reconciled_cancellation is not None:
            handoff = reconciled_cancellation

        if handoff.phase == HANDOFF_CANCELLED:
            _archive_applied_record(
                Path(handoff.journal_path),
                _cancelled_record_path(handoff),
                handoff.pending,
            )
            _unlink_file(handoff_path)
            return None

        if handoff.phase == HANDOFF_APPLIED:
            destination_paths = _destination_paths(paths, destination)
            applied_record = _archive_applied_record(
                _activation_witness_path(destination, handoff.pending),
                destination_paths.state_dir
                / "applied-restores"
                / handoff.applied_record_filename,
                handoff.pending,
            )
            _remove_duplicate_source_journal(
                handoff,
                destination,
                applied_record,
            )
            _unlink_file(handoff_path)
            return AppliedRestore(
                handoff.pending,
                RestoreResult(
                    destination,
                    Path(handoff.safety_root) if handoff.safety_root else None,
                    {"backup_id": handoff.pending.backup_id},
                ),
                destination_paths,
                applied_record,
            )

        if handoff.phase == HANDOFF_PREPARED:
            _validate_handoff_archives(handoff, destination)
            handoff = replace(handoff, phase=HANDOFF_ACTIVATING)
            _write_restore_handoff(handoff_path, handoff)

        if handoff.phase == HANDOFF_ACTIVATING:
            active_store = key_store or InstallationKeyStore(paths.master_keys_file)
            restore = _activate_handoff(
                handoff,
                destination,
                key_store=active_store,
            )
            handoff = replace(
                handoff,
                phase=HANDOFF_ACTIVATED,
                safety_root=(
                    str(restore.safety_root)
                    if restore.safety_root is not None
                    else None
                ),
            )
            _write_restore_handoff(handoff_path, handoff)
        else:
            restore = RestoreResult(
                destination,
                Path(handoff.safety_root) if handoff.safety_root else None,
                {"backup_id": handoff.pending.backup_id},
            )

        destination_paths = _destination_paths(paths, destination)
        destination_paths.ensure_directories()
        destination_paths.write_storage_manifest()
        applied_record = _archive_applied_record(
            _activation_witness_path(destination, handoff.pending),
            destination_paths.state_dir
            / "applied-restores"
            / handoff.applied_record_filename,
            handoff.pending,
        )
        _remove_duplicate_source_journal(
            handoff,
            destination,
            applied_record,
        )
        handoff = replace(handoff, phase=HANDOFF_APPLIED)
        _write_restore_handoff(handoff_path, handoff)
        _unlink_file(handoff_path)
        return AppliedRestore(
            handoff.pending,
            restore,
            destination_paths,
            applied_record,
        )
    finally:
        lock.release()


def cancel_pending_restore(paths: AppPaths) -> Path | None:
    """Cancel without deletion by archiving the signed-off restore request."""

    destination = _restore_destination(paths)
    lock = _acquire_restore_lifecycle_lock(destination)
    try:
        handoff_path = _restore_handoff_path(destination)
        handoff = _load_restore_handoff(
            handoff_path,
            paths=paths,
            destination=destination,
        )
        pending = load_pending_restore(paths)
        if pending is None and handoff is None:
            return None
        if handoff is None:
            assert pending is not None
            handoff = _new_restore_handoff(
                pending,
                archive=_safe_backup_path(paths, pending.archive_filename),
                safety_backup=_safe_backup_path(
                    paths,
                    pending.safety_backup_filename,
                ),
                source_data_root=paths.data_root,
                journal_path=pending_restore_path(paths),
            )
            _write_restore_handoff(handoff_path, handoff)
        elif pending is not None and pending != handoff.pending:
            raise PendingRestoreError(
                "Pending restore does not match the durable restore handoff."
            )

        reconciled_cancellation = _reconcile_cancelled_handoff(
            handoff_path,
            handoff,
        )
        if reconciled_cancellation is not None:
            handoff = reconciled_cancellation

        if handoff.phase in {
            HANDOFF_ACTIVATING,
            HANDOFF_ACTIVATED,
            HANDOFF_APPLIED,
        }:
            raise PendingRestoreError(
                "Restore activation may have started or already completed; "
                "retry apply-pending to finish it."
            )

        cancelled_record = _cancelled_record_path(handoff)
        journal_source = Path(handoff.journal_path)
        if (
            handoff.phase == HANDOFF_PREPARED
            and not journal_source.exists()
            and not cancelled_record.exists()
        ):
            raise PendingRestoreError(
                "Prepared restore handoff has no pending journal; retry apply-pending."
            )
        _archive_applied_record(journal_source, cancelled_record, handoff.pending)
        handoff = replace(handoff, phase=HANDOFF_CANCELLED)
        _write_restore_handoff(handoff_path, handoff)
        _unlink_file(handoff_path)
        return cancelled_record
    finally:
        lock.release()


__all__ = [
    "PENDING_RESTORE_FILENAME",
    "PENDING_RESTORE_FORMAT",
    "PENDING_RESTORE_VERSION",
    "AppliedRestore",
    "PendingRestore",
    "PendingRestoreError",
    "PendingRestoreExists",
    "ScheduledRestore",
    "apply_pending_restore",
    "cancel_pending_restore",
    "load_pending_restore",
    "pending_restore_path",
    "schedule_restore",
]
