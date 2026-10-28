"""Integrity-checked migration retention for removed installed add-ons."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from .contracts import ADDON_ID_PATTERN
from .schema_ownership import (
    addon_has_migrations,
    addon_migration_contract,
    addon_migration_contract_issues,
)

CAPSULE_FORMAT = "carlina.addon-migration-capsule"
CAPSULE_VERSION = 2
CAPSULE_METADATA = "capsule.json"
MAX_CAPSULE_FILES = 1_000
MAX_CAPSULE_BYTES = 64 * 1024 * 1024
MAX_CAPSULE_METADATA_BYTES = 8 * 1024 * 1024


class MigrationCapsuleError(RuntimeError):
    """Raised when retained migration code is missing, unsafe, or changed."""


@dataclass(frozen=True, slots=True)
class MigrationCapsule:
    addon_id: str
    path: Path
    digest: str
    file_count: int
    total_bytes: int
    format_version: int = CAPSULE_VERSION
    lifecycle_generation: int = 0
    package_version: str | None = None
    manifest_sha256: str | None = None
    contract_sha256: str | None = None
    physical_revision: str | None = None
    graph_sha256: str | None = None


def _capsule_path(paths: Any, addon_id: str) -> Path:
    normalized_id = str(addon_id or "").strip()
    if not ADDON_ID_PATTERN.fullmatch(normalized_id):
        raise MigrationCapsuleError(f"Unsafe add-on ID: {addon_id!r}.")
    root = (Path(paths.state_dir).resolve() / "addons").resolve()
    candidate = (root / normalized_id / "migration-capsule").resolve()
    candidate.relative_to(root)
    return candidate


def _sha256(path: Path, *, max_bytes: int = MAX_CAPSULE_BYTES) -> str:
    digest = hashlib.sha256()
    total_bytes = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            total_bytes += len(chunk)
            if total_bytes > max_bytes:
                raise MigrationCapsuleError(
                    f"Migration capsule file exceeds {max_bytes} bytes."
                )
            digest.update(chunk)
    return digest.hexdigest()


def _payload_digest(payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _migration_files(migrations_dir: Path) -> tuple[Path, ...]:
    files: list[Path] = []
    total_bytes = 0
    for candidate in sorted(migrations_dir.rglob("*")):
        relative = candidate.relative_to(migrations_dir)
        if "__pycache__" in relative.parts or candidate.suffix in {".pyc", ".pyo"}:
            continue
        if candidate.is_symlink():
            raise MigrationCapsuleError(
                f"Migration capsules cannot retain symbolic links: {relative}."
            )
        if candidate.is_dir():
            continue
        if not candidate.is_file():
            raise MigrationCapsuleError(
                f"Unsupported migration entry in capsule: {relative}."
            )
        total_bytes += candidate.stat().st_size
        if total_bytes > MAX_CAPSULE_BYTES:
            raise MigrationCapsuleError(
                f"Migration capsule exceeds {MAX_CAPSULE_BYTES} bytes."
            )
        files.append(candidate)
    if len(files) > MAX_CAPSULE_FILES:
        raise MigrationCapsuleError(
            f"Migration capsule contains more than {MAX_CAPSULE_FILES} files."
        )
    return tuple(files)


def _replace_directory(prepared: Path, target: Path) -> None:
    previous = target.with_name(f".{target.name}.previous-{uuid4().hex}")
    had_previous = target.exists()
    if had_previous:
        os.replace(target, previous)
    try:
        os.replace(prepared, target)
    except Exception:
        if had_previous and previous.exists() and not target.exists():
            os.replace(previous, target)
        raise
    if previous.exists():
        shutil.rmtree(previous)


def _migration_metadata(
    source_root: Path,
    addon_id: str,
    physical_revision: str | None,
) -> dict[str, Any]:
    contract = addon_migration_contract(source_root, addon_id)
    if contract is None:
        raise MigrationCapsuleError(
            f"Cannot retain '{addon_id}': its migration contract is invalid."
        )
    if physical_revision is not None and physical_revision not in contract.revision_ids:
        raise MigrationCapsuleError(
            f"Cannot retain '{addon_id}': physical revision "
            f"{physical_revision!r} is absent from its migration graph."
        )
    return {
        "format": "alembic_python_v1",
        "version_table": contract.version_table,
        "purge_tables": list(contract.purge_tables),
        "owned_tables": list(contract.owned_tables),
        "base_revision": contract.base_revision,
        "head_revision": contract.head_revision,
        "revision_ids": list(contract.revision_ids),
        "graph_sha256": contract.graph_sha256,
        "physical_revision": physical_revision,
    }


def _validate_retained_migration_metadata(
    target: Path,
    addon_id: str,
    metadata: dict[str, Any],
) -> tuple[str | None, str | None]:
    contract = addon_migration_contract(target, addon_id)
    if contract is None:
        issues = "; ".join(addon_migration_contract_issues(target, addon_id))
        raise MigrationCapsuleError(f"Retained migration capsule is invalid: {issues}.")
    if metadata.get("version") == 1:
        return None, None
    generation = metadata.get("lifecycle_generation")
    if (
        isinstance(generation, bool)
        or not isinstance(generation, int)
        or generation < 0
    ):
        raise MigrationCapsuleError(
            "Migration capsule lifecycle generation is malformed."
        )
    migration = metadata.get("migration")
    if not isinstance(migration, dict):
        raise MigrationCapsuleError("Migration capsule descriptor is missing.")
    expected = {
        "format": "alembic_python_v1",
        "version_table": contract.version_table,
        "purge_tables": list(contract.purge_tables),
        "owned_tables": list(contract.owned_tables),
        "base_revision": contract.base_revision,
        "head_revision": contract.head_revision,
        "revision_ids": list(contract.revision_ids),
        "graph_sha256": contract.graph_sha256,
    }
    if any(migration.get(key) != value for key, value in expected.items()):
        raise MigrationCapsuleError(
            "Migration capsule descriptor does not match its retained graph."
        )
    physical_revision = migration.get("physical_revision")
    if physical_revision is not None and physical_revision not in contract.revision_ids:
        raise MigrationCapsuleError(
            "Migration capsule records a revision absent from its retained graph."
        )
    if metadata.get("physical_revision") != physical_revision:
        raise MigrationCapsuleError(
            "Migration capsule physical revision metadata is inconsistent."
        )
    recorded_revision = metadata.get("alembic_revision")
    if recorded_revision is not None and recorded_revision not in contract.revision_ids:
        raise MigrationCapsuleError(
            "Migration capsule manager revision is absent from its retained graph."
        )
    return (
        str(physical_revision) if physical_revision is not None else None,
        contract.graph_sha256,
    )


def capture_migration_capsule(
    paths: Any,
    addon_id: str,
    addon_dir: str | Path,
    *,
    alembic_revision: str | None,
    manifest_sha256: str | None,
    contract_sha256: str | None,
    lifecycle_generation: int = 0,
    package_version: str | None = None,
    physical_revision: str | None = None,
) -> MigrationCapsule | None:
    """Copy reviewed migrations before executable package code is removed."""

    source_root = Path(addon_dir).resolve()
    target = _capsule_path(paths, addon_id)
    migrations_declared = (source_root / "migrations").exists()
    if not addon_has_migrations(source_root, addon_id):
        if migrations_declared:
            issues = "; ".join(addon_migration_contract_issues(source_root, addon_id))
            raise MigrationCapsuleError(
                f"Cannot remove '{addon_id}': its migration contract is "
                f"invalid ({issues})."
            )
        if alembic_revision:
            raise MigrationCapsuleError(
                f"Cannot remove '{addon_id}': database revision "
                f"{alembic_revision!r} is recorded but no reviewed migration "
                "environment is available for a later purge."
            )
        # A capsule retained from an older installation generation must never
        # be rebound to a package that now owns no migration graph.
        return None

    physical_revision = (
        physical_revision if physical_revision is not None else alembic_revision
    )
    migration_metadata = _migration_metadata(
        source_root,
        addon_id,
        physical_revision,
    )
    migrations_dir = source_root / "migrations"
    prepared = target.with_name(f".{target.name}.prepared-{uuid4().hex}")
    prepared_migrations = prepared / "migrations"
    prepared_migrations.mkdir(parents=True)
    try:
        file_hashes: dict[str, str] = {}
        total_bytes = 0
        for source in _migration_files(migrations_dir):
            relative = source.relative_to(migrations_dir)
            destination = prepared_migrations / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination, follow_symlinks=False)
            size = destination.stat().st_size
            total_bytes += size
            if total_bytes > MAX_CAPSULE_BYTES:
                raise MigrationCapsuleError(
                    f"Migration capsule exceeds {MAX_CAPSULE_BYTES} bytes."
                )
            file_hashes[f"migrations/{relative.as_posix()}"] = _sha256(
                destination,
                max_bytes=size,
            )

        payload: dict[str, Any] = {
            "format": CAPSULE_FORMAT,
            "version": CAPSULE_VERSION,
            "addon_id": addon_id,
            "captured_at": datetime.now(UTC).isoformat(),
            "alembic_revision": alembic_revision,
            "physical_revision": physical_revision,
            "lifecycle_generation": int(lifecycle_generation),
            "package_version": package_version,
            "manifest_sha256": manifest_sha256,
            "contract_sha256": contract_sha256,
            "migration": migration_metadata,
            "files": file_hashes,
            "file_count": len(file_hashes),
            "total_bytes": total_bytes,
        }
        digest = _payload_digest(payload)
        metadata = {**payload, "digest": digest}
        (prepared / CAPSULE_METADATA).write_text(
            json.dumps(metadata, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        load_migration_capsule_from_path(
            prepared,
            addon_id,
            expected_digest=digest,
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        _replace_directory(prepared, target)
        return MigrationCapsule(
            addon_id=addon_id,
            path=target,
            digest=digest,
            file_count=len(file_hashes),
            total_bytes=total_bytes,
            format_version=CAPSULE_VERSION,
            lifecycle_generation=int(lifecycle_generation),
            package_version=package_version,
            manifest_sha256=manifest_sha256,
            contract_sha256=contract_sha256,
            physical_revision=physical_revision,
            graph_sha256=str(migration_metadata["graph_sha256"]),
        )
    except Exception:
        shutil.rmtree(prepared, ignore_errors=True)
        raise


def load_migration_capsule(
    paths: Any,
    addon_id: str,
    *,
    expected_digest: str | None = None,
) -> MigrationCapsule | None:
    target = _capsule_path(paths, addon_id)
    if not target.exists():
        return None
    return load_migration_capsule_from_path(
        target,
        addon_id,
        expected_digest=expected_digest,
    )


def load_migration_capsule_from_path(
    target: Path,
    addon_id: str,
    *,
    expected_digest: str | None = None,
) -> MigrationCapsule:
    """Verify a retained capsule before Alembic imports any of its files."""

    metadata, actual_digest = _read_capsule_metadata(
        target,
        addon_id,
        expected_digest=expected_digest,
    )
    actual_files, total_bytes = _capsule_file_inventory(target)
    declared_files = metadata.get("files")
    if not isinstance(declared_files, dict):
        raise MigrationCapsuleError("Migration capsule file inventory is malformed.")
    expected_files = {str(key): str(value) for key, value in declared_files.items()}
    if actual_files != expected_files:
        raise MigrationCapsuleError("Migration capsule file integrity failed.")
    if len(actual_files) != metadata.get("file_count"):
        raise MigrationCapsuleError("Migration capsule file count mismatch.")
    if total_bytes != metadata.get("total_bytes"):
        raise MigrationCapsuleError("Migration capsule size mismatch.")
    physical_revision, graph_sha256 = _validate_retained_migration_metadata(
        target,
        addon_id,
        metadata,
    )
    return MigrationCapsule(
        addon_id=addon_id,
        path=target,
        digest=actual_digest,
        file_count=len(actual_files),
        total_bytes=total_bytes,
        format_version=int(metadata["version"]),
        lifecycle_generation=int(metadata.get("lifecycle_generation") or 0),
        package_version=(
            str(metadata["package_version"])
            if metadata.get("package_version") is not None
            else None
        ),
        manifest_sha256=(
            str(metadata["manifest_sha256"])
            if metadata.get("manifest_sha256") is not None
            else None
        ),
        contract_sha256=(
            str(metadata["contract_sha256"])
            if metadata.get("contract_sha256") is not None
            else None
        ),
        physical_revision=physical_revision,
        graph_sha256=graph_sha256,
    )


def _read_capsule_metadata(
    target: Path,
    addon_id: str,
    *,
    expected_digest: str | None,
) -> tuple[dict[str, Any], str]:
    if not target.is_dir() or target.is_symlink():
        raise MigrationCapsuleError("Migration capsule is not a real directory.")
    metadata_path = target / CAPSULE_METADATA
    try:
        if metadata_path.stat().st_size > MAX_CAPSULE_METADATA_BYTES:
            raise MigrationCapsuleError("Migration capsule metadata is oversized.")
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MigrationCapsuleError(
            "Migration capsule metadata is unreadable."
        ) from exc
    if not isinstance(metadata, dict):
        raise MigrationCapsuleError("Migration capsule metadata must be an object.")
    if metadata.get("format") != CAPSULE_FORMAT or metadata.get("version") not in {
        1,
        CAPSULE_VERSION,
    }:
        raise MigrationCapsuleError("Unsupported migration capsule format.")
    if metadata.get("addon_id") != addon_id:
        raise MigrationCapsuleError("Migration capsule add-on identity mismatch.")
    recorded_digest = str(metadata.get("digest") or "")
    payload = {key: value for key, value in metadata.items() if key != "digest"}
    actual_digest = _payload_digest(payload)
    if recorded_digest != actual_digest:
        raise MigrationCapsuleError("Migration capsule metadata integrity failed.")
    if expected_digest is not None and actual_digest != expected_digest:
        raise MigrationCapsuleError(
            "Migration capsule does not match its lifecycle journal."
        )
    return metadata, actual_digest


def _capsule_file_inventory(target: Path) -> tuple[dict[str, str], int]:
    actual_files: dict[str, str] = {}
    total_bytes = 0
    for candidate in sorted(target.rglob("*")):
        relative = candidate.relative_to(target)
        if relative.as_posix() == CAPSULE_METADATA:
            continue
        if candidate.is_symlink():
            raise MigrationCapsuleError(
                f"Migration capsule contains a symbolic link: {relative}."
            )
        if candidate.is_dir():
            continue
        if not candidate.is_file():
            raise MigrationCapsuleError(
                f"Migration capsule contains an unsupported entry: {relative}."
            )
        key = relative.as_posix()
        size = candidate.stat().st_size
        if total_bytes + size > MAX_CAPSULE_BYTES:
            raise MigrationCapsuleError(
                f"Migration capsule exceeds {MAX_CAPSULE_BYTES} bytes."
            )
        actual_files[key] = _sha256(
            candidate,
            max_bytes=size,
        )
        total_bytes += size
        if len(actual_files) > MAX_CAPSULE_FILES:
            raise MigrationCapsuleError(
                f"Migration capsule contains more than {MAX_CAPSULE_FILES} files."
            )
    return actual_files, total_bytes


__all__ = [
    "MigrationCapsule",
    "MigrationCapsuleError",
    "capture_migration_capsule",
    "load_migration_capsule",
]
