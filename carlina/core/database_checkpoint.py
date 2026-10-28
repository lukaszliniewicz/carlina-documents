"""Transaction-bound whole-SQLite checkpoints for proportional recovery."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

DATABASE_CHECKPOINT_FORMAT = "carlina.database-checkpoint"
DATABASE_CHECKPOINT_VERSION = 1
DATABASE_CHECKPOINT_MANIFEST = "checkpoint.json"
DATABASE_CHECKPOINT_PAYLOAD = "database.sqlite3"
DATABASE_SWITCH_FORMAT = "carlina.database-checkpoint-switch"
DATABASE_SWITCH_VERSION = 1
DATABASE_SWITCH_WITNESS = "switch.json"
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ENVELOPE_KEY_PATTERN = re.compile(rb"enc:v2:(mk-[0-9a-f]{16}):")
_TERMINAL_SWITCH_PHASES = frozenset(
    {
        "completed",
        "rolled_back",
        "interrupted_rolled_back",
        "reactivation_completed",
    }
)


class DatabaseCheckpointError(RuntimeError):
    """A database checkpoint was unsafe, corrupt, or could not be restored."""


@dataclass(frozen=True, slots=True)
class DatabaseCheckpointRecord:
    checkpoint_id: str
    transaction_id: str
    created_at: str
    path: Path
    manifest_sha256: str
    database_sha256: str
    database_size: int
    required_key_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["path"] = str(self.path)
        return payload


@dataclass(frozen=True, slots=True)
class DatabaseCheckpointRestoreResult:
    destination_database: Path
    safety_dir: Path
    checkpoint: DatabaseCheckpointRecord


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(4 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _sqlite_read_only_uri(path: Path) -> str:
    normalized = str(path.expanduser().resolve()).replace("\\", "/")
    folded = normalized.casefold()
    if folded.startswith("//?/unc/"):
        normalized = "//" + normalized[8:]
    elif folded.startswith("//?/"):
        normalized = normalized[4:]
    encoded = quote(normalized, safe="/:")
    if normalized.startswith("//"):
        uri = "file://" + encoded
    elif len(normalized) >= 3 and normalized[1:3] == ":/":
        uri = "file:///" + encoded
    elif normalized.startswith("/"):
        uri = "file://" + encoded
    else:
        raise DatabaseCheckpointError(
            f"SQLite checkpoint path must be absolute: {path}"
        )
    return f"{uri}?mode=ro"


def _inspect_database(path: Path) -> dict[str, Any]:
    try:
        connection = sqlite3.connect(_sqlite_read_only_uri(path), uri=True)
        try:
            quick_rows = [
                str(row[0])
                for row in connection.execute("PRAGMA quick_check").fetchall()
            ]
            foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
            schema_heads: list[str] = []
            table_exists = connection.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='alembic_version'"
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
    except (OSError, sqlite3.Error) as exc:
        raise DatabaseCheckpointError(
            f"SQLite checkpoint database is unreadable: {path}"
        ) from exc
    if quick_rows != ["ok"]:
        raise DatabaseCheckpointError(
            f"SQLite quick_check failed for checkpoint: {quick_rows[:5]}"
        )
    return {
        "quick_check": "ok",
        "foreign_key_violation_count": len(foreign_keys),
        "foreign_key_violation_sample": [list(row) for row in foreign_keys[:25]],
        "schema_heads": schema_heads,
        "journal_mode": journal_mode,
    }


def _snapshot_database(source: Path, destination: Path) -> dict[str, Any]:
    unresolved_source = source.expanduser()
    if unresolved_source.is_symlink():
        raise DatabaseCheckpointError("Active database is a symbolic link.")
    resolved_source = unresolved_source.resolve()
    if not resolved_source.is_file():
        raise DatabaseCheckpointError(f"Active database is missing: {resolved_source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_connection: sqlite3.Connection | None = None
    target_connection: sqlite3.Connection | None = None
    try:
        source_connection = sqlite3.connect(
            _sqlite_read_only_uri(resolved_source),
            uri=True,
            timeout=30,
        )
        target_connection = sqlite3.connect(str(destination), timeout=30)
        source_connection.backup(target_connection, pages=2048, sleep=0.05)
        target_connection.commit()
    except sqlite3.Error as exc:
        raise DatabaseCheckpointError(
            f"Could not snapshot active SQLite database: {exc}"
        ) from exc
    finally:
        if target_connection is not None:
            target_connection.close()
        if source_connection is not None:
            source_connection.close()
    return _inspect_database(destination)


def _scan_required_key_ids(path: Path) -> tuple[str, ...]:
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


def _safe_id(value: str, *, label: str) -> str:
    normalized = str(value or "").strip()
    if not _SAFE_ID.fullmatch(normalized):
        raise DatabaseCheckpointError(f"Unsafe {label}: {value!r}.")
    return normalized


def _atomic_json(path: Path, payload: object) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _ensure_available_keys(
    required: tuple[str, ...],
    available_key_ids: tuple[str, ...],
) -> None:
    missing = sorted(set(required) - set(available_key_ids))
    if missing:
        raise DatabaseCheckpointError(
            "Database checkpoint requires unavailable encryption keys: "
            + ", ".join(missing)
        )


def _read_manifest(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DatabaseCheckpointError(
            f"Database checkpoint manifest is unreadable: {path}"
        ) from exc
    if not isinstance(payload, dict):
        raise DatabaseCheckpointError("Database checkpoint manifest must be an object.")
    return payload


def verify_database_checkpoint(
    checkpoint_path: str | os.PathLike[str],
    *,
    available_key_ids: tuple[str, ...],
    expected_manifest_sha256: str | None = None,
) -> DatabaseCheckpointRecord:
    """Verify a published checkpoint without trusting its receipt fields."""

    unresolved_root = Path(checkpoint_path).expanduser()
    if unresolved_root.is_symlink():
        raise DatabaseCheckpointError("Database checkpoint is a symbolic link.")
    root = unresolved_root.resolve()
    if not root.is_dir():
        raise DatabaseCheckpointError(f"Database checkpoint is missing: {root}")
    manifest_path = root / DATABASE_CHECKPOINT_MANIFEST
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise DatabaseCheckpointError("Database checkpoint manifest is missing.")
    manifest_sha256 = _sha256_file(manifest_path)
    if expected_manifest_sha256 is not None and (
        not _SHA256.fullmatch(expected_manifest_sha256)
        or manifest_sha256 != expected_manifest_sha256
    ):
        raise DatabaseCheckpointError("Database checkpoint manifest hash changed.")
    payload = _read_manifest(manifest_path)
    if set(payload) != {
        "format",
        "schema_version",
        "checkpoint_id",
        "transaction_id",
        "created_at",
        "source_version",
        "target_version",
        "database",
    }:
        raise DatabaseCheckpointError("Database checkpoint manifest shape is invalid.")
    if (
        payload.get("format") != DATABASE_CHECKPOINT_FORMAT
        or payload.get("schema_version") != DATABASE_CHECKPOINT_VERSION
    ):
        raise DatabaseCheckpointError("Database checkpoint format is unsupported.")
    checkpoint_id = _safe_id(
        str(payload.get("checkpoint_id") or ""), label="checkpoint ID"
    )
    transaction_id = _safe_id(
        str(payload.get("transaction_id") or ""), label="transaction ID"
    )
    if root.name != checkpoint_id:
        raise DatabaseCheckpointError(
            "Database checkpoint directory does not match its ID."
        )
    database = payload.get("database")
    if not isinstance(database, dict) or set(database) != {
        "filename",
        "sha256",
        "size",
        "required_key_ids",
        "health",
    }:
        raise DatabaseCheckpointError(
            "Database checkpoint payload metadata is invalid."
        )
    if database.get("filename") != DATABASE_CHECKPOINT_PAYLOAD:
        raise DatabaseCheckpointError(
            "Database checkpoint payload filename is invalid."
        )
    expected_database_sha256 = database.get("sha256")
    expected_size = database.get("size")
    required_raw = database.get("required_key_ids")
    if (
        not isinstance(expected_database_sha256, str)
        or not _SHA256.fullmatch(expected_database_sha256)
        or not isinstance(expected_size, int)
        or isinstance(expected_size, bool)
        or expected_size < 0
        or not isinstance(required_raw, list)
        or not all(isinstance(value, str) for value in required_raw)
    ):
        raise DatabaseCheckpointError(
            "Database checkpoint payload contract is invalid."
        )
    required_key_ids = tuple(sorted(set(required_raw)))
    if list(required_key_ids) != required_raw:
        raise DatabaseCheckpointError("Database checkpoint key IDs are not canonical.")
    database_path = root / DATABASE_CHECKPOINT_PAYLOAD
    if database_path.is_symlink() or not database_path.is_file():
        raise DatabaseCheckpointError("Database checkpoint payload is missing.")
    if database_path.stat().st_size != expected_size:
        raise DatabaseCheckpointError("Database checkpoint payload size changed.")
    if _sha256_file(database_path) != expected_database_sha256:
        raise DatabaseCheckpointError("Database checkpoint payload hash changed.")
    try:
        health = _inspect_database(database_path)
        scanned_key_ids = _scan_required_key_ids(database_path)
    except (DatabaseCheckpointError, OSError) as exc:
        raise DatabaseCheckpointError(
            f"Database checkpoint payload verification failed: {exc}"
        ) from exc
    expected_health = database.get("health")
    if not isinstance(expected_health, dict) or any(
        health.get(field) != expected_health.get(field)
        for field in (
            "quick_check",
            "foreign_key_violation_count",
            "schema_heads",
        )
    ):
        raise DatabaseCheckpointError("Database checkpoint health metadata changed.")
    if scanned_key_ids != required_key_ids:
        raise DatabaseCheckpointError("Database checkpoint key evidence changed.")
    _ensure_available_keys(required_key_ids, available_key_ids)
    created_at = payload.get("created_at")
    if not isinstance(created_at, str) or not created_at:
        raise DatabaseCheckpointError("Database checkpoint timestamp is invalid.")
    return DatabaseCheckpointRecord(
        checkpoint_id=checkpoint_id,
        transaction_id=transaction_id,
        created_at=created_at,
        path=root,
        manifest_sha256=manifest_sha256,
        database_sha256=expected_database_sha256,
        database_size=expected_size,
        required_key_ids=required_key_ids,
    )


def create_database_checkpoint(
    source_database: str | os.PathLike[str],
    checkpoint_root: str | os.PathLike[str],
    *,
    transaction_id: str,
    source_version: str | None,
    target_version: str,
    available_key_ids: tuple[str, ...],
) -> DatabaseCheckpointRecord:
    """Snapshot, verify, and atomically publish one transaction checkpoint."""

    safe_transaction = _safe_id(transaction_id, label="transaction ID")
    checkpoint_id = _safe_id(f"dbcp-{safe_transaction}", label="checkpoint ID")
    unresolved_root = Path(checkpoint_root).expanduser()
    if unresolved_root.is_symlink():
        raise DatabaseCheckpointError("Database checkpoint root is a symbolic link.")
    root = unresolved_root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    destination = root / checkpoint_id
    if destination.exists() or destination.is_symlink():
        raise DatabaseCheckpointError(
            f"Database checkpoint already exists: {destination}"
        )
    staging = root / f".{checkpoint_id}.tmp-{uuid.uuid4().hex}"
    staging.mkdir()
    try:
        database_path = staging / DATABASE_CHECKPOINT_PAYLOAD
        try:
            health = _snapshot_database(Path(source_database), database_path)
            required_key_ids = _scan_required_key_ids(database_path)
        except (DatabaseCheckpointError, OSError) as exc:
            raise DatabaseCheckpointError(
                f"Database checkpoint creation failed: {exc}"
            ) from exc
        _ensure_available_keys(required_key_ids, available_key_ids)
        payload = {
            "format": DATABASE_CHECKPOINT_FORMAT,
            "schema_version": DATABASE_CHECKPOINT_VERSION,
            "checkpoint_id": checkpoint_id,
            "transaction_id": safe_transaction,
            "created_at": _now(),
            "source_version": source_version,
            "target_version": target_version,
            "database": {
                "filename": DATABASE_CHECKPOINT_PAYLOAD,
                "sha256": _sha256_file(database_path),
                "size": database_path.stat().st_size,
                "required_key_ids": list(required_key_ids),
                "health": health,
            },
        }
        _atomic_json(staging / DATABASE_CHECKPOINT_MANIFEST, payload)
        os.replace(staging, destination)
        _fsync_directory(root)
        return verify_database_checkpoint(
            destination,
            available_key_ids=available_key_ids,
        )
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def _safe_database_file(path: Path, *, label: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise DatabaseCheckpointError(f"{label} is missing or unsafe: {path}")


def _move_database_sidecars(
    source: Path,
    evidence: Path,
) -> None:
    """Move exact SQLite sidecars beside their database evidence."""

    candidates: list[tuple[Path, Path]] = []
    for suffix in ("-wal", "-shm"):
        source_sidecar = Path(f"{source}{suffix}")
        evidence_sidecar = Path(f"{evidence}{suffix}")
        if source_sidecar.is_symlink() or evidence_sidecar.is_symlink():
            raise DatabaseCheckpointError("SQLite sidecar path is a symbolic link.")
        if source_sidecar.exists() and evidence_sidecar.exists():
            raise DatabaseCheckpointError(
                f"SQLite sidecar switch is ambiguous: {source_sidecar}"
            )
        if source_sidecar.exists():
            _safe_database_file(source_sidecar, label="SQLite sidecar")
            candidates.append((source_sidecar, evidence_sidecar))
        elif evidence_sidecar.exists():
            _safe_database_file(evidence_sidecar, label="SQLite sidecar evidence")
    for source_sidecar, evidence_sidecar in candidates:
        os.replace(source_sidecar, evidence_sidecar)


def _write_switch_witness(
    safety_dir: Path,
    witness: dict[str, Any],
    *,
    phase: str,
) -> None:
    witness["phase"] = phase
    witness["updated_at"] = _now()
    _atomic_json(safety_dir / DATABASE_SWITCH_WITNESS, witness)


def _read_switch_witness(safety_dir: Path) -> dict[str, Any]:
    witness_path = safety_dir / DATABASE_SWITCH_WITNESS
    if witness_path.is_symlink() or not witness_path.is_file():
        raise DatabaseCheckpointError(
            f"Database switch witness is missing or unsafe: {witness_path}"
        )
    try:
        payload = json.loads(witness_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DatabaseCheckpointError(
            f"Database switch witness is unreadable: {witness_path}"
        ) from exc
    if not isinstance(payload, dict) or set(payload) != {
        "format",
        "schema_version",
        "transaction_id",
        "created_at",
        "updated_at",
        "phase",
        "destination_database",
        "original_database_sha256",
        "checkpoint_database_sha256",
        "original_sidecars",
        "failed_database_name",
    }:
        raise DatabaseCheckpointError("Database switch witness shape is invalid.")
    if (
        payload.get("format") != DATABASE_SWITCH_FORMAT
        or payload.get("schema_version") != DATABASE_SWITCH_VERSION
    ):
        raise DatabaseCheckpointError("Database switch witness format is unsupported.")
    _safe_id(str(payload.get("transaction_id") or ""), label="switch transaction ID")
    for field in ("original_database_sha256", "checkpoint_database_sha256"):
        value = payload.get(field)
        if not isinstance(value, str) or not _SHA256.fullmatch(value):
            raise DatabaseCheckpointError(
                f"Database switch witness has an invalid {field}."
            )
    original_sidecars = payload.get("original_sidecars")
    if (
        not isinstance(original_sidecars, list)
        or any(value not in {"-wal", "-shm"} for value in original_sidecars)
        or original_sidecars != sorted(set(original_sidecars))
    ):
        raise DatabaseCheckpointError(
            "Database switch witness has invalid SQLite sidecars."
        )
    failed_name = payload.get("failed_database_name")
    if (
        not isinstance(failed_name, str)
        or Path(failed_name).name != failed_name
        or not failed_name.endswith(".sqlite3")
    ):
        raise DatabaseCheckpointError(
            "Database switch witness has an unsafe failed-database name."
        )
    if not isinstance(payload.get("phase"), str):
        raise DatabaseCheckpointError("Database switch witness phase is invalid.")
    return payload


def _recover_switch_to_original(
    safety_dir: Path,
    witness: dict[str, Any],
    *,
    expected_destination: Path,
    terminal_phase: str,
) -> Path:
    """Make the pre-switch database active from any witnessed rename layout."""

    raw_destination = witness.get("destination_database")
    if not isinstance(raw_destination, str):
        raise DatabaseCheckpointError(
            "Database switch witness has no destination database."
        )
    destination = Path(raw_destination).expanduser().resolve()
    if destination != expected_destination.resolve():
        raise DatabaseCheckpointError(
            "Database switch witness targets a different active database."
        )
    displaced = safety_dir / "database-before-restore.sqlite3"
    failed = safety_dir / str(witness["failed_database_name"])
    original_sha256 = str(witness["original_database_sha256"])
    original_sidecars = tuple(str(value) for value in witness["original_sidecars"])

    if displaced.exists():
        _safe_database_file(displaced, label="Displaced pre-switch database")
        if destination.exists():
            _safe_database_file(destination, label="Active checkpoint database")
            if failed.exists() or failed.is_symlink():
                raise DatabaseCheckpointError(
                    f"Failed checkpoint evidence already exists: {failed}"
                )
            _move_database_sidecars(destination, failed)
            os.replace(destination, failed)
            _fsync_directory(destination.parent)
            _fsync_directory(safety_dir)
        elif destination.is_symlink():
            raise DatabaseCheckpointError("Active database path is a symbolic link.")
        elif failed.exists():
            _safe_database_file(failed, label="Failed checkpoint database")
            _move_database_sidecars(destination, failed)
        os.replace(displaced, destination)
        _fsync_directory(destination.parent)
    elif destination.is_symlink() or not destination.is_file():
        raise DatabaseCheckpointError(
            "Database switch lost both active and displaced database files."
        )

    if _sha256_file(destination) != original_sha256:
        raise DatabaseCheckpointError(
            "Recovered database does not match the pre-switch database."
        )
    for suffix in ("-shm", "-wal"):
        active_sidecar = Path(f"{destination}{suffix}")
        displaced_sidecar = Path(f"{displaced}{suffix}")
        expected = suffix in original_sidecars
        if displaced_sidecar.exists():
            _safe_database_file(displaced_sidecar, label="Displaced SQLite sidecar")
            if active_sidecar.exists() or active_sidecar.is_symlink():
                raise DatabaseCheckpointError(
                    f"SQLite sidecar recovery is ambiguous: {active_sidecar}"
                )
            os.replace(displaced_sidecar, active_sidecar)
        elif expected:
            _safe_database_file(active_sidecar, label="Active original SQLite sidecar")
        elif active_sidecar.exists() or active_sidecar.is_symlink():
            raise DatabaseCheckpointError(
                f"Unexpected SQLite sidecar remains after recovery: {active_sidecar}"
            )
    _fsync_directory(destination.parent)
    _inspect_database(destination)
    _write_switch_witness(safety_dir, witness, phase=terminal_phase)
    return failed


def recover_interrupted_database_switches(
    safety_root: str | os.PathLike[str],
    destination_database: str | os.PathLike[str],
) -> tuple[Path, ...]:
    """Roll back every non-terminal checkpoint switch before new maintenance."""

    unresolved_root = Path(safety_root).expanduser()
    if unresolved_root.is_symlink():
        raise DatabaseCheckpointError("Database checkpoint safety root is unsafe.")
    root = unresolved_root.resolve()
    if not root.exists():
        return ()
    if not root.is_dir():
        raise DatabaseCheckpointError(
            "Database checkpoint safety root is not a directory."
        )
    expected_destination = Path(destination_database).expanduser().resolve()
    recovered: list[Path] = []
    for safety_dir in sorted(root.glob("checkpoint-restore-*")):
        if safety_dir.is_symlink() or not safety_dir.is_dir():
            raise DatabaseCheckpointError(
                f"Database checkpoint safety entry is unsafe: {safety_dir}"
            )
        witness_path = safety_dir / DATABASE_SWITCH_WITNESS
        if not witness_path.exists():
            # Pre-witness directories from older builds are retained as evidence
            # but cannot be resumed automatically.
            continue
        witness = _read_switch_witness(safety_dir)
        if witness["phase"] in _TERMINAL_SWITCH_PHASES:
            continue
        _recover_switch_to_original(
            safety_dir,
            witness,
            expected_destination=expected_destination,
            terminal_phase="interrupted_rolled_back",
        )
        recovered.append(safety_dir)
    return tuple(recovered)


def interrupted_database_switches(
    safety_root: str | os.PathLike[str],
) -> tuple[Path, ...]:
    """Return non-terminal switch witnesses without changing database state."""

    unresolved_root = Path(safety_root).expanduser()
    if unresolved_root.is_symlink():
        raise DatabaseCheckpointError("Database checkpoint safety root is unsafe.")
    root = unresolved_root.resolve()
    if not root.exists():
        return ()
    if not root.is_dir():
        raise DatabaseCheckpointError(
            "Database checkpoint safety root is not a directory."
        )
    pending: list[Path] = []
    for safety_dir in sorted(root.glob("checkpoint-restore-*")):
        if safety_dir.is_symlink() or not safety_dir.is_dir():
            raise DatabaseCheckpointError(
                f"Database checkpoint safety entry is unsafe: {safety_dir}"
            )
        witness_path = safety_dir / DATABASE_SWITCH_WITNESS
        if not witness_path.exists():
            continue
        witness = _read_switch_witness(safety_dir)
        if witness["phase"] not in _TERMINAL_SWITCH_PHASES:
            pending.append(safety_dir)
    return tuple(pending)


def discard_unwitnessed_database_switch(
    safety_dir: str | os.PathLike[str],
    destination_database: str | os.PathLike[str],
    *,
    transaction_id: str,
) -> None:
    """Discard exact pre-rename staging left before a witness was published."""

    safe_transaction = _safe_id(transaction_id, label="restore transaction ID")
    unresolved_safety = Path(safety_dir).expanduser()
    if unresolved_safety.is_symlink():
        raise DatabaseCheckpointError(
            "Unwitnessed database checkpoint safety state is unsafe."
        )
    resolved_safety = unresolved_safety.resolve()
    if (
        not resolved_safety.is_dir()
        or resolved_safety.name != f"checkpoint-restore-{safe_transaction}"
    ):
        raise DatabaseCheckpointError(
            "Unwitnessed database checkpoint safety state does not match its transaction."
        )
    if (resolved_safety / DATABASE_SWITCH_WITNESS).exists():
        raise DatabaseCheckpointError(
            "Witnessed database checkpoint state cannot use pre-switch cleanup."
        )
    try:
        entries = tuple(resolved_safety.iterdir())
    except OSError as exc:
        raise DatabaseCheckpointError(
            "Unwitnessed database checkpoint safety state is unreadable."
        ) from exc
    if entries:
        raise DatabaseCheckpointError(
            "Unwitnessed database checkpoint safety state contains unexpected evidence."
        )

    unresolved_destination = Path(destination_database).expanduser()
    if unresolved_destination.is_symlink():
        raise DatabaseCheckpointError("Active database is a symbolic link.")
    destination = unresolved_destination.resolve()
    _safe_database_file(destination, label="Active database")
    _inspect_database(destination)
    staged = destination.with_name(
        f".{destination.name}.checkpoint-{safe_transaction}.tmp"
    )
    if staged.is_symlink():
        raise DatabaseCheckpointError(
            "Unwitnessed database checkpoint staging file is unsafe."
        )
    if staged.exists():
        _safe_database_file(staged, label="Unwitnessed checkpoint staging file")
        staged.unlink()
        _fsync_directory(staged.parent)
    resolved_safety.rmdir()
    _fsync_directory(resolved_safety.parent)


def database_switch_phase(safety_dir: str | os.PathLike[str]) -> str:
    """Return the validated durable phase for one database switch."""

    unresolved = Path(safety_dir).expanduser()
    if unresolved.is_symlink():
        raise DatabaseCheckpointError("Database checkpoint safety state is unsafe.")
    resolved = unresolved.resolve()
    if not resolved.is_dir():
        raise DatabaseCheckpointError("Database checkpoint safety state is missing.")
    return str(_read_switch_witness(resolved)["phase"])


def reactivate_completed_database_switch(
    safety_dir: str | os.PathLike[str],
    destination_database: str | os.PathLike[str],
    *,
    transaction_id: str,
) -> Path:
    """Reactivate pre-switch data from a completed checkpoint restore."""

    safe_transaction = _safe_id(transaction_id, label="recovery transaction ID")
    unresolved_safety = Path(safety_dir).expanduser()
    if unresolved_safety.is_symlink():
        raise DatabaseCheckpointError("Database checkpoint safety state is unsafe.")
    resolved_safety = unresolved_safety.resolve()
    if not resolved_safety.is_dir():
        raise DatabaseCheckpointError("Database checkpoint safety state is unsafe.")
    destination = Path(destination_database).expanduser().resolve()
    _safe_database_file(destination, label="Active restored database")
    witness = _read_switch_witness(resolved_safety)
    if witness["phase"] == "reactivation_completed":
        return resolved_safety / str(witness["failed_database_name"])
    if witness["phase"] != "completed":
        raise DatabaseCheckpointError(
            "Database checkpoint restore is not in a reactivatable state."
        )
    displaced = resolved_safety / "database-before-restore.sqlite3"
    _safe_database_file(displaced, label="Displaced pre-restore database")
    failed = resolved_safety / f"failed-restored-{safe_transaction}.sqlite3"
    if failed.exists() or failed.is_symlink():
        raise DatabaseCheckpointError(
            f"Failed-restored database evidence already exists: {failed}"
        )
    witness["failed_database_name"] = failed.name
    _write_switch_witness(
        resolved_safety,
        witness,
        phase="reactivation_prepared",
    )
    return _recover_switch_to_original(
        resolved_safety,
        witness,
        expected_destination=destination,
        terminal_phase="reactivation_completed",
    )


def restore_database_checkpoint(
    checkpoint_path: str | os.PathLike[str],
    destination_database: str | os.PathLike[str],
    safety_root: str | os.PathLike[str],
    *,
    expected_manifest_sha256: str,
    available_key_ids: tuple[str, ...],
    transaction_id: str,
) -> DatabaseCheckpointRestoreResult:
    """Restore a checkpoint with a durable, crash-recoverable switch witness."""

    safe_transaction = _safe_id(transaction_id, label="restore transaction ID")
    checkpoint = verify_database_checkpoint(
        checkpoint_path,
        available_key_ids=available_key_ids,
        expected_manifest_sha256=expected_manifest_sha256,
    )
    unresolved_destination = Path(destination_database).expanduser()
    if unresolved_destination.is_symlink():
        raise DatabaseCheckpointError("Active database is a symbolic link.")
    destination = unresolved_destination.resolve()
    _safe_database_file(destination, label="Active database")
    unresolved_safety_parent = Path(safety_root).expanduser()
    if unresolved_safety_parent.is_symlink():
        raise DatabaseCheckpointError("Database checkpoint safety root is unsafe.")
    safety_parent = unresolved_safety_parent.resolve()
    safety_parent.mkdir(parents=True, exist_ok=True)
    if destination.parent.stat().st_dev != safety_parent.stat().st_dev:
        raise DatabaseCheckpointError(
            "Database checkpoint safety state must be on the database volume."
        )
    safety_dir = safety_parent / f"checkpoint-restore-{safe_transaction}"
    if safety_dir.exists() or safety_dir.is_symlink():
        raise DatabaseCheckpointError(
            f"Database checkpoint safety state already exists: {safety_dir}"
        )
    safety_dir.mkdir()
    staged = destination.with_name(
        f".{destination.name}.checkpoint-{safe_transaction}.tmp"
    )
    if staged.exists() or staged.is_symlink():
        raise DatabaseCheckpointError(
            f"Database checkpoint staging file already exists: {staged}"
        )
    displaced_database = safety_dir / "database-before-restore.sqlite3"
    displaced_sidecars: list[tuple[Path, Path]] = []
    witness: dict[str, Any] | None = None
    try:
        shutil.copyfile(checkpoint.path / DATABASE_CHECKPOINT_PAYLOAD, staged)
        with staged.open("rb") as handle:
            os.fsync(handle.fileno())
        if (
            staged.stat().st_size != checkpoint.database_size
            or _sha256_file(staged) != checkpoint.database_sha256
        ):
            raise DatabaseCheckpointError("Staged database checkpoint copy changed.")
        _inspect_database(staged)
        for suffix in ("-wal", "-shm"):
            source = Path(f"{destination}{suffix}")
            if not source.exists():
                continue
            _safe_database_file(source, label="SQLite sidecar")
            displaced_sidecars.append(
                (source, safety_dir / f"database-before-restore.sqlite3{suffix}")
            )
        witness = {
            "format": DATABASE_SWITCH_FORMAT,
            "schema_version": DATABASE_SWITCH_VERSION,
            "transaction_id": safe_transaction,
            "created_at": _now(),
            "updated_at": _now(),
            "phase": "prepared",
            "destination_database": str(destination),
            "original_database_sha256": _sha256_file(destination),
            "checkpoint_database_sha256": checkpoint.database_sha256,
            "original_sidecars": sorted(
                source.name.removeprefix(destination.name)
                for source, _displaced in displaced_sidecars
            ),
            "failed_database_name": (f"failed-checkpoint-{safe_transaction}.sqlite3"),
        }
        _write_switch_witness(safety_dir, witness, phase="prepared")
        os.replace(destination, displaced_database)
        _fsync_directory(destination.parent)
        _write_switch_witness(safety_dir, witness, phase="database_displaced")
        for source, displaced in displaced_sidecars:
            os.replace(source, displaced)
        _fsync_directory(safety_dir)
        _write_switch_witness(safety_dir, witness, phase="sidecars_displaced")
        os.replace(staged, destination)
        _fsync_directory(destination.parent)
        _write_switch_witness(safety_dir, witness, phase="checkpoint_activated")
        if (
            _sha256_file(destination) != checkpoint.database_sha256
            or _inspect_database(destination).get("quick_check") != "ok"
        ):
            raise DatabaseCheckpointError(
                "Restored database checkpoint failed verification."
            )
        _write_switch_witness(safety_dir, witness, phase="completed")
        return DatabaseCheckpointRestoreResult(
            destination_database=destination,
            safety_dir=safety_dir,
            checkpoint=checkpoint,
        )
    except BaseException as exc:
        if witness is not None:
            try:
                _recover_switch_to_original(
                    safety_dir,
                    witness,
                    expected_destination=destination,
                    terminal_phase="rolled_back",
                )
            except BaseException as rollback_exc:  # noqa: BLE001
                raise DatabaseCheckpointError(
                    "Database checkpoint restore failed and the displaced database "
                    f"could not be reactivated: {rollback_exc}"
                ) from exc
        if isinstance(exc, DatabaseCheckpointError):
            raise
        raise DatabaseCheckpointError(
            f"Database checkpoint restore failed: {exc}"
        ) from exc
    finally:
        staged.unlink(missing_ok=True)


def reactivate_checkpoint_safety(
    result: DatabaseCheckpointRestoreResult,
    *,
    transaction_id: str,
) -> Path:
    """Undo a completed restore through the same crash-recoverable witness."""

    return reactivate_completed_database_switch(
        result.safety_dir,
        result.destination_database,
        transaction_id=transaction_id,
    )
