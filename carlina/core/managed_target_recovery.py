"""Validate recovery material delegated to one managed target initialization.

The managed updater chooses and creates recovery material before it imports target
release code.  Target-side add-on migrations must reuse that exact transaction
guard instead of silently creating a second whole-data backup.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .database_checkpoint import (
    DATABASE_CHECKPOINT_FORMAT,
    DATABASE_CHECKPOINT_MANIFEST,
    DATABASE_CHECKPOINT_PAYLOAD,
    DATABASE_CHECKPOINT_VERSION,
)
from .recovery_policy import (
    RECOVERY_PLAN_FORMAT,
    RECOVERY_PLAN_VERSION,
    RecoveryCoverage,
    RecoveryMechanism,
)

MANAGED_UPDATE_TRANSACTION_ENVIRONMENT = "CARLINA_MANAGED_UPDATE_TRANSACTION_ID"
_UPDATE_JOURNAL_FORMAT = "carlina.update-journal"
_UPDATE_JOURNAL_VERSION = 1
_RECOVERY_REQUIRED_FORMAT = "carlina.recovery-required"
_RECOVERY_REQUIRED_VERSION = 1
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SAFE_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+!-]{0,79}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ManagedTargetRecoveryError(ValueError):
    """A target process could not authenticate its managed recovery guard."""


@dataclass(frozen=True, slots=True)
class ManagedTargetRecoveryContext:
    """Transaction-bound recovery authority safe to pass inside trusted code."""

    transaction_id: str
    target_version: str
    selected_coverage: RecoveryCoverage
    mechanism: RecoveryMechanism
    guard_id: str
    guard_sha256: str
    context_sha256: str
    source: str

    def event_metadata(self) -> dict[str, str]:
        payload = asdict(self)
        payload["selected_coverage"] = self.selected_coverage.value
        payload["mechanism"] = self.mechanism.value
        return payload


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _json_object(path: Path, *, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ManagedTargetRecoveryError(f"{label} is missing or unsafe.")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManagedTargetRecoveryError(f"{label} is unreadable.") from exc
    if not isinstance(payload, dict):
        raise ManagedTargetRecoveryError(f"{label} must be an object.")
    return payload


def _safe_id(value: object, *, label: str) -> str:
    normalized = str(value or "").strip()
    if not _SAFE_ID.fullmatch(normalized):
        raise ManagedTargetRecoveryError(f"{label} is unsafe.")
    return normalized


def _safe_version(value: object) -> str:
    normalized = str(value or "").strip()
    if not _SAFE_VERSION.fullmatch(normalized):
        raise ManagedTargetRecoveryError("Target version is unsafe.")
    return normalized


def _sha256(value: object, *, label: str) -> str:
    normalized = str(value or "").strip()
    if not _SHA256.fullmatch(normalized):
        raise ManagedTargetRecoveryError(f"{label} is invalid.")
    return normalized


def _path_beneath(
    value: object,
    *,
    expected_parent: Path,
    label: str,
    directory: bool,
) -> Path:
    if not isinstance(value, str) or not value:
        raise ManagedTargetRecoveryError(f"{label} path is missing.")
    unresolved = Path(value).expanduser()
    if unresolved.is_symlink():
        raise ManagedTargetRecoveryError(f"{label} is a symbolic link.")
    resolved = unresolved.resolve()
    parent = expected_parent.resolve()
    try:
        resolved.relative_to(parent)
    except ValueError as exc:
        raise ManagedTargetRecoveryError(f"{label} escaped its managed root.") from exc
    if directory and not resolved.is_dir():
        raise ManagedTargetRecoveryError(f"{label} directory is missing.")
    if not directory and not resolved.is_file():
        raise ManagedTargetRecoveryError(f"{label} file is missing.")
    return resolved


def _recovery_plan(
    journal: Mapping[str, Any],
) -> tuple[RecoveryCoverage, RecoveryMechanism]:
    payload = journal.get("recovery_plan")
    if not isinstance(payload, dict):
        raise ManagedTargetRecoveryError("Managed recovery plan is missing.")
    if (
        payload.get("format") != RECOVERY_PLAN_FORMAT
        or payload.get("schema_version") != RECOVERY_PLAN_VERSION
    ):
        raise ManagedTargetRecoveryError("Managed recovery plan is unsupported.")
    try:
        coverage = RecoveryCoverage(str(payload.get("selected_coverage") or ""))
        mechanism = RecoveryMechanism(str(payload.get("mechanism") or ""))
    except ValueError as exc:
        raise ManagedTargetRecoveryError(
            "Managed recovery selection is invalid."
        ) from exc
    allowed = {
        RecoveryCoverage.FULL: {RecoveryMechanism.COMPLETE_RECOVERY},
        RecoveryCoverage.DATABASE_ONLY: {RecoveryMechanism.DATABASE_CHECKPOINT},
        RecoveryCoverage.NONE: {
            RecoveryMechanism.CODE_POINTER,
            RecoveryMechanism.METADATA_JOURNAL,
            RecoveryMechanism.NONE,
        },
    }
    if coverage is RecoveryCoverage.AUTOMATIC or mechanism not in allowed[coverage]:
        raise ManagedTargetRecoveryError(
            "Managed recovery coverage and mechanism disagree."
        )
    return coverage, mechanism


def _database_checkpoint_guard(
    journal: Mapping[str, Any],
    *,
    program_root: Path,
    transaction_id: str,
    target_version: str,
) -> tuple[str, str]:
    checkpoint = journal.get("database_checkpoint")
    if not isinstance(checkpoint, dict):
        raise ManagedTargetRecoveryError("Database checkpoint receipt is missing.")
    checkpoint_id = _safe_id(checkpoint.get("checkpoint_id"), label="Checkpoint ID")
    if checkpoint_id != f"dbcp-{transaction_id}":
        raise ManagedTargetRecoveryError("Database checkpoint ID does not match.")
    if checkpoint.get("transaction_id") != transaction_id:
        raise ManagedTargetRecoveryError(
            "Database checkpoint transaction does not match."
        )
    expected_root = program_root / "state" / "recovery" / "database-checkpoints"
    root = _path_beneath(
        checkpoint.get("path"),
        expected_parent=expected_root,
        label="Database checkpoint",
        directory=True,
    )
    if root != (expected_root / checkpoint_id).resolve():
        raise ManagedTargetRecoveryError("Database checkpoint path does not match.")
    manifest_path = root / DATABASE_CHECKPOINT_MANIFEST
    manifest = _json_object(manifest_path, label="Database checkpoint manifest")
    manifest_sha256 = _sha256(
        checkpoint.get("manifest_sha256"),
        label="Database checkpoint manifest hash",
    )
    if _sha256_file(manifest_path) != manifest_sha256:
        raise ManagedTargetRecoveryError("Database checkpoint manifest changed.")
    if set(manifest) != {
        "format",
        "schema_version",
        "checkpoint_id",
        "transaction_id",
        "created_at",
        "source_version",
        "target_version",
        "database",
    }:
        raise ManagedTargetRecoveryError("Database checkpoint manifest shape changed.")
    if (
        manifest.get("format") != DATABASE_CHECKPOINT_FORMAT
        or manifest.get("schema_version") != DATABASE_CHECKPOINT_VERSION
        or manifest.get("checkpoint_id") != checkpoint_id
        or manifest.get("transaction_id") != transaction_id
        or manifest.get("target_version") != target_version
    ):
        raise ManagedTargetRecoveryError("Database checkpoint manifest does not match.")
    database = manifest.get("database")
    if not isinstance(database, dict) or set(database) != {
        "filename",
        "sha256",
        "size",
        "required_key_ids",
        "health",
    }:
        raise ManagedTargetRecoveryError("Database checkpoint payload is invalid.")
    database_sha256 = _sha256(
        database.get("sha256"),
        label="Database checkpoint payload hash",
    )
    database_size = database.get("size")
    if (
        database.get("filename") != DATABASE_CHECKPOINT_PAYLOAD
        or not isinstance(database_size, int)
        or isinstance(database_size, bool)
        or database_size < 0
        or checkpoint.get("database_sha256") != database_sha256
        or checkpoint.get("database_size") != database_size
    ):
        raise ManagedTargetRecoveryError("Database checkpoint payload does not match.")
    payload_path = root / DATABASE_CHECKPOINT_PAYLOAD
    if payload_path.is_symlink() or not payload_path.is_file():
        raise ManagedTargetRecoveryError("Database checkpoint payload is unsafe.")
    if payload_path.stat().st_size != database_size:
        raise ManagedTargetRecoveryError("Database checkpoint payload size changed.")
    if _sha256_file(payload_path) != database_sha256:
        raise ManagedTargetRecoveryError("Database checkpoint payload changed.")
    return checkpoint_id, manifest_sha256


def _full_backup_guard(
    journal: Mapping[str, Any],
    *,
    data_root: Path,
) -> tuple[str, str]:
    backup = journal.get("backup")
    if not isinstance(backup, dict):
        raise ManagedTargetRecoveryError("Managed full-backup receipt is missing.")
    backup_id = _safe_id(backup.get("backup_id"), label="Backup ID")
    backup_sha256 = _sha256(backup.get("sha256"), label="Backup hash")
    archive = _path_beneath(
        backup.get("path"),
        expected_parent=data_root / "backups",
        label="Managed full backup",
        directory=False,
    )
    if _sha256_file(archive) != backup_sha256:
        raise ManagedTargetRecoveryError("Managed full backup changed.")
    return backup_id, backup_sha256


def _no_recovery_guard(
    journal: Mapping[str, Any],
    *,
    program_root: Path,
    transaction_id: str,
    target_version: str,
) -> tuple[str, str]:
    expected = journal.get("recovery_required")
    if not isinstance(expected, dict):
        raise ManagedTargetRecoveryError("No-recovery fence receipt is missing.")
    fence_path = program_root / "state" / "recovery-required.json"
    fence = _json_object(fence_path, label="No-recovery fence")
    if fence != expected:
        raise ManagedTargetRecoveryError("No-recovery fence does not match journal.")
    if (
        fence.get("format") != _RECOVERY_REQUIRED_FORMAT
        or fence.get("schema_version") != _RECOVERY_REQUIRED_VERSION
        or fence.get("status") != "armed"
        or fence.get("transaction_id") != transaction_id
        or fence.get("failed_target_version") != target_version
        or fence.get("selected_coverage") != RecoveryCoverage.NONE
    ):
        raise ManagedTargetRecoveryError("No-recovery fence is not armed for target.")
    return transaction_id, _sha256_file(fence_path)


def _resolve_managed_target_recovery_context(
    environment: Mapping[str, str],
) -> ManagedTargetRecoveryContext:
    program_value = environment.get("CARLINA_PROGRAM_ROOT")
    data_value = environment.get("CARLINA_HOME")
    if not program_value or not data_value:
        raise ManagedTargetRecoveryError("Managed path environment is missing.")
    program_root = Path(program_value).expanduser().resolve()
    data_root = Path(data_value).expanduser().resolve()
    target_version = _safe_version(environment.get("CARLINA_MANAGED_VERSION"))
    binding_path = program_root / "state" / "data-root"
    if binding_path.is_symlink() or not binding_path.is_file():
        raise ManagedTargetRecoveryError("Managed data-root binding is unsafe.")
    try:
        bound_root = (
            Path(binding_path.read_text(encoding="utf-8").rstrip("\r\n"))
            .expanduser()
            .resolve()
        )
    except OSError as exc:
        raise ManagedTargetRecoveryError(
            "Managed data-root binding is unreadable."
        ) from exc
    if bound_root != data_root:
        raise ManagedTargetRecoveryError("Managed data-root binding does not match.")

    journal = _json_object(
        program_root / "state" / "update-journal.json",
        label="Managed update journal",
    )
    transaction_id = _safe_id(
        journal.get("transaction_id"), label="Update transaction ID"
    )
    if (
        journal.get("format") != _UPDATE_JOURNAL_FORMAT
        or journal.get("schema_version") != _UPDATE_JOURNAL_VERSION
        or journal.get("operation") not in {"update", "adopt"}
        or journal.get("phase") != "target_verification"
        or journal.get("target_execution_started") is not True
        or journal.get("migration_started") is not True
        or journal.get("target_version") != target_version
    ):
        raise ManagedTargetRecoveryError(
            "Managed update journal is not authorizing target migration."
        )
    transaction_hint = environment.get(MANAGED_UPDATE_TRANSACTION_ENVIRONMENT)
    if transaction_hint:
        if (
            _safe_id(transaction_hint, label="Update transaction hint")
            != transaction_id
        ):
            raise ManagedTargetRecoveryError(
                "Managed update transaction hint does not match."
            )
        source = "explicit_environment"
    else:
        # Compatibility for the first target release launched by an older
        # updater.  The exact active journal phase and guard still bind this
        # narrowly to one managed target initialization.
        source = "active_journal_compatibility"

    coverage, mechanism = _recovery_plan(journal)
    if coverage is RecoveryCoverage.FULL:
        guard_id, guard_sha256 = _full_backup_guard(
            journal,
            data_root=data_root,
        )
    elif coverage is RecoveryCoverage.DATABASE_ONLY:
        guard_id, guard_sha256 = _database_checkpoint_guard(
            journal,
            program_root=program_root,
            transaction_id=transaction_id,
            target_version=target_version,
        )
    else:
        guard_id, guard_sha256 = _no_recovery_guard(
            journal,
            program_root=program_root,
            transaction_id=transaction_id,
            target_version=target_version,
        )
    sealed = {
        "transaction_id": transaction_id,
        "target_version": target_version,
        "selected_coverage": coverage.value,
        "mechanism": mechanism.value,
        "guard_id": guard_id,
        "guard_sha256": guard_sha256,
        "data_root_sha256": hashlib.sha256(
            os.path.normcase(str(data_root)).encode("utf-8", errors="surrogatepass")
        ).hexdigest(),
        "source": source,
    }
    context_sha256 = hashlib.sha256(
        json.dumps(sealed, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return ManagedTargetRecoveryContext(
        transaction_id=transaction_id,
        target_version=target_version,
        selected_coverage=coverage,
        mechanism=mechanism,
        guard_id=guard_id,
        guard_sha256=guard_sha256,
        context_sha256=context_sha256,
        source=source,
    )


def resolve_managed_target_recovery_context(
    environment: Mapping[str, str] | None = None,
) -> ManagedTargetRecoveryContext | None:
    """Return exact managed recovery authority, or fail closed to local backup."""

    try:
        return _resolve_managed_target_recovery_context(
            environment if environment is not None else os.environ
        )
    except ManagedTargetRecoveryError, OSError, ValueError:
        return None


__all__ = [
    "MANAGED_UPDATE_TRANSACTION_ENVIRONMENT",
    "ManagedTargetRecoveryContext",
    "ManagedTargetRecoveryError",
    "resolve_managed_target_recovery_context",
]
