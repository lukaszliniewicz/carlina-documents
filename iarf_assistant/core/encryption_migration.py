"""Offline, resumable migration of legacy encrypted SQL columns to v2."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
import sqlite3
import stat
import tempfile
from collections import deque
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from filelock import FileLock, Timeout
from sqlalchemy import MetaData, Table
from sqlalchemy.exc import NoReferencedColumnError, NoReferencedTableError
from werkzeug.security import check_password_hash

from iarf_assistant.core.app_paths import AppPaths
from iarf_assistant.core.backup_service import (
    BackupError,
    BackupRecord,
    create_backup,
    verify_backup,
)
from iarf_assistant.core.encryption import (
    EncryptionError,
    EncryptionService,
    InstallationKeyStore,
    derive_legacy_system_key,
    derive_legacy_user_key,
)
from iarf_assistant.core.persistence.sqlite import sqlite_read_only_uri
from iarf_assistant.core.runtime_ownership import (
    load_runtime_leader,
    process_is_running,
    runtime_leader_lock,
)
from iarf_assistant.database.types import EncryptedType

ENCRYPTION_MIGRATION_FORMAT = "carlina.encryption-migration-plan"
ENCRYPTION_MIGRATION_VERSION = 3
ENCRYPTION_MIGRATION_LEGACY_VERSION = 1
ENCRYPTION_MIGRATION_OWNERSHIP_VERSION = 2
ENCRYPTION_JOURNAL_FORMAT = "carlina.encryption-migration-journal"
ENCRYPTION_JOURNAL_VERSION = 3
ENCRYPTION_QUARANTINE_LEDGER_FORMAT = "carlina.encryption-migration-quarantine"
ENCRYPTION_QUARANTINE_LEDGER_VERSION = 1
LEGACY_SYSTEM_QUARANTINE_POLICY = "legacy-sys-unrecoverable-v1"
LEGACY_SYSTEM_QUARANTINE_REASON = "legacy-system-secret-unavailable"
# This is a package-wide safety ceiling, not an operational loss count.  The
# caller must separately attest the exact count discovered in its own database.
MAX_LEGACY_SYSTEM_QUARANTINE_CELLS = 1_000
DEFAULT_BATCH_SIZE = 250
# Every journal write is bounded before publication. Pending batches contain
# only row identity and hashes, but a pathological SQLite primary key can still
# make one batch too large to resume safely, so reject it before DB mutation.
_JOURNAL_MAX_BYTES = 1024 * 1024
# A v3 plan is database-wide metadata.  Four MiB accommodates a conservative
# large-installation inventory plus all 1,000 exact-cell entries,
# while remaining a finite parser/read bound.
_QUARANTINE_PLAN_MAX_BYTES = 4 * 1024 * 1024
# A recovery ledger retains the original token for every quarantined cell.
# Bound one UTF-8 token to 64 KiB and reserve a further 16 MiB for 1,000 JSON
# entries, primary-key identities, and ledger metadata: 80 MiB total.
_QUARANTINE_CIPHERTEXT_MAX_BYTES = 64 * 1024
_QUARANTINE_LEDGER_MAX_BYTES = 80 * 1024 * 1024
_LEGACY_SYSTEM_DISCOVERY_BATCH_SIZE = 128
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_JOURNAL_STATUSES = frozenset({"planned", "running", "interrupted", "complete"})


class EncryptionMigrationError(RuntimeError):
    """Base class for safe offline encryption migration failures."""


class EncryptionMigrationConflict(EncryptionMigrationError):
    """Database state changed or a row could not be updated optimistically."""


class EncryptionMigrationConfirmationRequired(EncryptionMigrationError):
    """The exact plan confirmation token was not supplied."""


@dataclass(frozen=True, slots=True)
class OwnershipHop:
    local_table: str
    local_columns: tuple[str, ...]
    remote_table: str
    remote_columns: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EncryptedColumnSpec:
    table: str
    column: str
    strategy: str
    primary_key_columns: tuple[str, ...]
    ownership_path: tuple[OwnershipHop, ...] | None
    is_json: bool = False

    @property
    def key(self) -> str:
        return f"{self.table}.{self.column}"


@dataclass(frozen=True, slots=True)
class ColumnEncryptionInventory:
    spec: EncryptedColumnSpec
    non_null_count: int
    v2_count: int
    legacy_system_count: int
    legacy_user_count: int
    legacy_unprefixed_count: int
    unknown_envelope_count: int
    required_user_ids: tuple[int, ...]
    missing_owner_count: int

    @property
    def legacy_count(self) -> int:
        return (
            self.legacy_system_count
            + self.legacy_user_count
            + self.legacy_unprefixed_count
        )


@dataclass(frozen=True, slots=True)
class EncryptionMigrationNotice:
    code: str
    message: str
    columns: tuple[str, ...] = ()
    blocking: bool = False


@dataclass(frozen=True, slots=True)
class LegacyOwnerOverride:
    """One operator-attested owner for a single ownerless legacy user cell."""

    table: str
    column: str
    rowid: int
    primary_key: tuple[tuple[str, str | int | float | bool | None], ...]
    ciphertext_sha256: str
    assigned_user_id: int


@dataclass(frozen=True, slots=True)
class LegacySystemQuarantine:
    """An exact, operator-attested irrecoverable legacy ``sys:`` value."""

    table: str
    column: str
    rowid: int
    primary_key: tuple[tuple[str, str | int | float | bool | None], ...]
    ciphertext_sha256: str
    policy: str


@dataclass(frozen=True, slots=True)
class EncryptionMigrationPlan:
    format: str
    version: int
    plan_id: str
    created_at: str
    confirmation_token: str
    database: str
    database_size: int
    database_modified_ns: int
    active_key_id: str
    columns: tuple[ColumnEncryptionInventory, ...]
    required_user_ids: tuple[int, ...]
    notices: tuple[EncryptionMigrationNotice, ...]
    applicable: bool
    ownership_overrides: tuple[LegacyOwnerOverride, ...] = ()
    system_quarantines: tuple[LegacySystemQuarantine, ...] = ()

    @property
    def legacy_value_count(self) -> int:
        return sum(column.legacy_count for column in self.columns)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        if self.version < ENCRYPTION_MIGRATION_VERSION:
            for column in payload["columns"]:
                column["spec"].pop("is_json", None)
        if self.version == ENCRYPTION_MIGRATION_LEGACY_VERSION:
            payload.pop("ownership_overrides", None)
            payload.pop("system_quarantines", None)
        elif self.version == ENCRYPTION_MIGRATION_OWNERSHIP_VERSION:
            payload.pop("system_quarantines", None)
        payload["legacy_value_count"] = self.legacy_value_count
        return payload


@dataclass(frozen=True, slots=True)
class _MigrationCell:
    rowid: int
    value: str
    owner_id: int | None
    primary_key: tuple[tuple[str, str | int | float | bool | None], ...]


@dataclass(frozen=True, slots=True)
class EncryptionMigrationResult:
    plan_id: str
    migrated_values: int
    quarantined_values: int
    reused_batches: int
    safety_backup: BackupRecord | None
    journal_path: Path
    quarantine_ledger: Path | None


@dataclass(frozen=True, slots=True)
class EncryptionMigrationVerification:
    """Redacted result of read-only authentication of remaining legacy values."""

    plan_id: str
    verified_legacy_values: int
    quarantined_values: int
    system_candidate_matches: tuple[int, ...]
    required_user_ids: tuple[int, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "verified_legacy_values": self.verified_legacy_values,
            "quarantined_values": self.quarantined_values,
            "recovery_ledger_required": self.quarantined_values > 0,
            "system_candidate_matches": [
                {"candidate_ordinal": index + 1, "values": count}
                for index, count in enumerate(self.system_candidate_matches)
            ],
            "required_user_ids": list(self.required_user_ids),
            "required_user_count": len(self.required_user_ids),
        }


@dataclass(slots=True)
class _LegacySystemKeyCandidates:
    """One command's pre-derived legacy system Fernet keys and match counters."""

    fernets: tuple[Fernet, ...]
    matches: list[int]


FaultInjector = Callable[[str, str | None], None]


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _atomic_json_write(
    path: Path, payload: object, *, owner_only: bool = False
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.tmp-",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    temporary_stat = os.fstat(descriptor)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            if owner_only and os.name == "posix":
                os.fchmod(handle.fileno(), 0o600)
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if os.name == "posix":
            directory_descriptor = os.open(
                path.parent,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
    finally:
        try:
            current_stat = os.lstat(temporary)
        except FileNotFoundError:
            pass
        else:
            if (
                current_stat.st_dev == temporary_stat.st_dev
                and current_stat.st_ino == temporary_stat.st_ino
            ):
                temporary.unlink()


def _constraint_rank(local_columns: tuple[str, ...]) -> tuple[int, str]:
    preferred = {
        "user_id": 0,
        "owner_user_id": 1,
        "created_by_user_id": 2,
        "requester_user_id": 3,
        "delegator_id": 4,
        "resolver_user_id": 5,
        "reviewed_by_user_id": 6,
        "approver_user_id": 7,
    }
    first = local_columns[0] if local_columns else ""
    return preferred.get(first, 100), first


def _outgoing_hops(table: Table, metadata: MetaData) -> tuple[OwnershipHop, ...]:
    hops: list[OwnershipHop] = []
    for constraint in table.foreign_key_constraints:
        elements = tuple(constraint.elements)
        if not elements:
            continue
        try:
            remote_table = elements[0].column.table
            remote_columns = tuple(element.column.name for element in elements)
        except NoReferencedColumnError, NoReferencedTableError:
            remote_table = None
            remote_columns = ()
        if remote_table is None:
            continue
        if remote_table.name not in metadata.tables:
            continue
        local_columns = tuple(element.parent.name for element in elements)
        hops.append(
            OwnershipHop(
                table.name,
                local_columns,
                remote_table.name,
                remote_columns,
            )
        )
    hops.sort(key=lambda hop: (_constraint_rank(hop.local_columns), hop.remote_table))
    return tuple(hops)


def _ownership_path(
    source: Table,
    metadata: MetaData,
    *,
    max_depth: int = 8,
) -> tuple[OwnershipHop, ...] | None:
    if source.name == "user":
        return ()
    queue: deque[tuple[Table, tuple[OwnershipHop, ...], frozenset[str]]] = deque(
        [(source, (), frozenset({source.name}))]
    )
    while queue:
        table, path, visited = queue.popleft()
        if len(path) >= max_depth:
            continue
        for hop in _outgoing_hops(table, metadata):
            if hop.remote_table in visited:
                continue
            candidate_path = (*path, hop)
            if hop.remote_table == "user":
                return candidate_path
            remote = metadata.tables.get(hop.remote_table)
            if remote is not None:
                queue.append((remote, candidate_path, visited | {hop.remote_table}))
    return None


def encrypted_columns_from_metadata(
    metadata: MetaData,
) -> tuple[EncryptedColumnSpec, ...]:
    """Extract the persisted EncryptedType contract from loaded model metadata."""

    specs: list[EncryptedColumnSpec] = []
    for table in sorted(metadata.tables.values(), key=lambda item: item.name):
        owner_path: tuple[OwnershipHop, ...] | None = None
        owner_resolved = False
        for column in table.columns:
            if not isinstance(column.type, EncryptedType):
                continue
            if not owner_resolved:
                owner_path = _ownership_path(table, metadata)
                owner_resolved = True
            specs.append(
                EncryptedColumnSpec(
                    table=table.name,
                    column=column.name,
                    strategy=column.type.strategy,
                    primary_key_columns=tuple(
                        item.name for item in table.primary_key.columns
                    ),
                    ownership_path=owner_path,
                    is_json=column.type.is_json,
                )
            )
    return tuple(specs)


def discover_encrypted_model_contract(
    package_root: Path,
) -> tuple[tuple[EncryptedColumnSpec, ...], tuple[str, ...]]:
    """Import model modules and return their encryption columns and failures."""

    from iarf_assistant.extensions import db

    importlib.import_module("iarf_assistant.models")
    failures: list[str] = []
    addon_root = package_root / "addons"
    if addon_root.is_dir():
        for path in sorted(addon_root.glob("*/models.py")):
            module_name = f"iarf_assistant.addons.{path.parent.name}.models"
            try:
                importlib.import_module(module_name)
            except Exception as exc:  # noqa: BLE001 - report isolated add-on imports
                failures.append(f"{module_name}: {type(exc).__name__}: {exc}")
    return encrypted_columns_from_metadata(db.metadata), tuple(failures)


def _join_sql(spec: EncryptedColumnSpec) -> tuple[str, str]:
    if spec.ownership_path is None:
        return "", "NULL"
    if not spec.ownership_path:
        return "", '"t0"."id"'
    joins: list[str] = []
    for index, hop in enumerate(spec.ownership_path, start=1):
        previous = f"t{index - 1}"
        current = f"t{index}"
        predicates = [
            f"{_quote(previous)}.{_quote(local)} = {_quote(current)}.{_quote(remote)}"
            for local, remote in zip(
                hop.local_columns,
                hop.remote_columns,
                strict=True,
            )
        ]
        joins.append(
            f" LEFT JOIN {_quote(hop.remote_table)} AS {_quote(current)} ON "
            + " AND ".join(predicates)
        )
    return "".join(joins), f'{_quote(f"t{len(spec.ownership_path)}")}."id"'


def _column_counts(
    connection: sqlite3.Connection,
    spec: EncryptedColumnSpec,
) -> tuple[int, int, int, int, int, int]:
    column = _quote(spec.column)
    table = _quote(spec.table)
    row = connection.execute(
        f"""
        SELECT
            count({column}),
            coalesce(sum(CASE WHEN {column} LIKE 'enc:v2:%' THEN 1 ELSE 0 END), 0),
            coalesce(sum(CASE WHEN {column} LIKE 'sys:%' THEN 1 ELSE 0 END), 0),
            coalesce(sum(CASE WHEN {column} LIKE 'usr:%' THEN 1 ELSE 0 END), 0),
            coalesce(sum(CASE WHEN {column} LIKE 'enc:%' AND {column} NOT LIKE 'enc:v2:%' THEN 1 ELSE 0 END), 0),
            coalesce(sum(CASE WHEN {column} NOT LIKE 'enc:%' AND {column} NOT LIKE 'sys:%' AND {column} NOT LIKE 'usr:%' THEN 1 ELSE 0 END), 0)
        FROM {table}
        """
    ).fetchone()
    return tuple(int(value or 0) for value in row)  # type: ignore[return-value]


def _table_column_counts(
    connection: sqlite3.Connection,
    table: str,
    specs: list[EncryptedColumnSpec],
) -> dict[str, tuple[int, int, int, int, int, int]]:
    """Inventory every encrypted column in a table with one table scan."""

    expressions: list[str] = []
    for spec in specs:
        column = _quote(spec.column)
        expressions.extend(
            [
                f"count({column})",
                f"coalesce(sum(CASE WHEN {column} LIKE 'enc:v2:%' THEN 1 ELSE 0 END), 0)",
                f"coalesce(sum(CASE WHEN {column} LIKE 'sys:%' THEN 1 ELSE 0 END), 0)",
                f"coalesce(sum(CASE WHEN {column} LIKE 'usr:%' THEN 1 ELSE 0 END), 0)",
                f"coalesce(sum(CASE WHEN {column} LIKE 'enc:%' AND {column} NOT LIKE 'enc:v2:%' THEN 1 ELSE 0 END), 0)",
                f"coalesce(sum(CASE WHEN {column} NOT LIKE 'enc:%' AND {column} NOT LIKE 'sys:%' AND {column} NOT LIKE 'usr:%' THEN 1 ELSE 0 END), 0)",
            ]
        )
    row = connection.execute(
        f"SELECT {', '.join(expressions)} FROM {_quote(table)}"
    ).fetchone()
    result: dict[str, tuple[int, int, int, int, int, int]] = {}
    for index, spec in enumerate(specs):
        offset = index * 6
        result[spec.key] = tuple(int(value or 0) for value in row[offset : offset + 6])  # type: ignore[assignment]
    return result


def _required_owners(
    connection: sqlite3.Connection,
    spec: EncryptedColumnSpec,
) -> tuple[tuple[int, ...], int]:
    if spec.strategy != "user":
        return (), 0
    joins, owner_expression = _join_sql(spec)
    column = f'"t0".{_quote(spec.column)}'
    needs_user_key = (
        f"({column} LIKE 'usr:%' OR "
        f"({column} NOT LIKE 'enc:%' AND {column} NOT LIKE 'sys:%' "
        f"AND {column} NOT LIKE 'usr:%'))"
    )
    rows = connection.execute(
        f"SELECT DISTINCT {owner_expression} AS owner_id "
        f'FROM {_quote(spec.table)} AS "t0"{joins} '
        f"WHERE {column} IS NOT NULL AND {needs_user_key}"
    ).fetchall()
    owners: set[int] = set()
    missing = 0
    for row in rows:
        if row[0] is None:
            missing += 1
            continue
        try:
            owners.add(int(row[0]))
        except TypeError, ValueError:
            missing += 1
    if missing:
        missing = int(
            connection.execute(
                f'SELECT count(*) FROM {_quote(spec.table)} AS "t0"{joins} '
                f"WHERE {column} IS NOT NULL AND {needs_user_key} "
                f"AND {owner_expression} IS NULL"
            ).fetchone()[0]
        )
    return tuple(sorted(owners)), missing


def _legacy_user_owner_predicate(column: str) -> str:
    return (
        f"({column} LIKE 'usr:%' OR "
        f"({column} NOT LIKE 'enc:%' AND {column} NOT LIKE 'sys:%' "
        f"AND {column} NOT LIKE 'usr:%'))"
    )


def _requires_legacy_user_owner(spec: EncryptedColumnSpec, value: str) -> bool:
    return spec.strategy == "user" and (
        value.startswith("usr:") or not value.startswith(("enc:", "sys:", "usr:"))
    )


def _ciphertext_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _primary_key_identity(
    spec: EncryptedColumnSpec,
    values: tuple[object, ...],
) -> tuple[tuple[str, str | int | float | bool | None], ...]:
    if len(values) != len(spec.primary_key_columns):
        raise EncryptionMigrationError(
            f"Could not read the declared primary key for {spec.key}."
        )
    identity: list[tuple[str, str | int | float | bool | None]] = []
    for column, value in zip(spec.primary_key_columns, values, strict=True):
        if value is not None and not isinstance(value, (str, int, float, bool)):
            raise EncryptionMigrationError(
                f"Primary key for {spec.key} has an unsupported SQLite value."
            )
        identity.append((column, value))
    return tuple(identity)


def _unresolved_owner_cells(
    connection: sqlite3.Connection,
    spec: EncryptedColumnSpec,
) -> tuple[_MigrationCell, ...]:
    if spec.strategy != "user":
        return ()
    if not spec.primary_key_columns:
        raise EncryptionMigrationError(
            f"Ownerless legacy values in {spec.key} require a declared primary key."
        )
    joins, owner_expression = _join_sql(spec)
    column = f'"t0".{_quote(spec.column)}'
    primary_key_columns = ", ".join(
        f'"t0".{_quote(name)}' for name in spec.primary_key_columns
    )
    rows = connection.execute(
        f'SELECT "t0".rowid, {column}, {owner_expression} AS owner_id, '
        f'{primary_key_columns} FROM {_quote(spec.table)} AS "t0"{joins} '
        f"WHERE {column} IS NOT NULL AND {_legacy_user_owner_predicate(column)} "
        f'AND {owner_expression} IS NULL ORDER BY "t0".rowid'
    ).fetchall()
    cells: list[_MigrationCell] = []
    for row in rows:
        value = row[1]
        if not isinstance(value, str):
            raise EncryptionMigrationError(
                f"Legacy encrypted value in {spec.key} is not text."
            )
        cells.append(
            _MigrationCell(
                rowid=int(row[0]),
                value=value,
                owner_id=None,
                primary_key=_primary_key_identity(spec, tuple(row[3:])),
            )
        )
    return tuple(cells)


def _plan_digest_payload(plan: EncryptionMigrationPlan) -> dict[str, Any]:
    columns = [asdict(column) for column in plan.columns]
    if plan.version < ENCRYPTION_MIGRATION_VERSION:
        for column in columns:
            spec = column.get("spec")
            if isinstance(spec, dict):
                spec.pop("is_json", None)
    payload = {
        "format": plan.format,
        "version": plan.version,
        "database": plan.database,
        "database_size": plan.database_size,
        "database_modified_ns": plan.database_modified_ns,
        "active_key_id": plan.active_key_id,
        "columns": columns,
        "required_user_ids": list(plan.required_user_ids),
        "notices": [asdict(notice) for notice in plan.notices],
    }
    if plan.version >= ENCRYPTION_MIGRATION_OWNERSHIP_VERSION:
        payload["ownership_overrides"] = [
            asdict(override) for override in plan.ownership_overrides
        ]
    if plan.version >= ENCRYPTION_MIGRATION_VERSION:
        payload["system_quarantines"] = [
            asdict(quarantine) for quarantine in plan.system_quarantines
        ]
    return payload


def encryption_migration_plan_digest(plan: EncryptionMigrationPlan) -> str:
    canonical = json.dumps(
        _plan_digest_payload(plan),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _validate_quarantine_plan_size(plan: EncryptionMigrationPlan) -> None:
    if not plan.system_quarantines:
        return
    try:
        encoded = json.dumps(plan.to_dict(), sort_keys=True).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise EncryptionMigrationError(
            "Encryption migration plan is malformed."
        ) from exc
    if len(encoded) > _QUARANTINE_PLAN_MAX_BYTES:
        raise EncryptionMigrationError("Legacy system quarantine plan is too large.")


def build_encryption_migration_plan(
    database: Path,
    specs: tuple[EncryptedColumnSpec, ...],
    key_store: InstallationKeyStore,
    *,
    legacy_system_secret_available: bool,
    legacy_system_secrets: Sequence[str | bytes] | None = None,
    discovery_failures: tuple[str, ...] = (),
    assign_unresolved_user_id: int | None = None,
    approve_unresolved_user_ownership: bool = False,
    expected_system_quarantine_count: int | None = None,
    approve_system_quarantine: bool = False,
) -> EncryptionMigrationPlan:
    """Inventory every declared encrypted column without mutating the database."""

    if (assign_unresolved_user_id is None) == approve_unresolved_user_ownership:
        raise EncryptionMigrationError(
            "Assigning unresolved legacy owners requires both explicit approval "
            "and a target user id."
        )
    if (expected_system_quarantine_count is None) == approve_system_quarantine:
        raise EncryptionMigrationError(
            "System quarantine requires both an expected count and explicit approval."
        )
    if expected_system_quarantine_count is not None and (
        isinstance(expected_system_quarantine_count, bool)
        or not isinstance(expected_system_quarantine_count, int)
        or not 1
        <= expected_system_quarantine_count
        <= MAX_LEGACY_SYSTEM_QUARANTINE_CELLS
    ):
        raise EncryptionMigrationError("System quarantine expected count is invalid.")
    legacy_system_keys: _LegacySystemKeyCandidates | None = None
    if approve_system_quarantine:
        if not legacy_system_secrets:
            raise EncryptionMigrationError(
                "System quarantine requires legacy system secret candidates."
            )
        legacy_system_keys = _prepare_legacy_system_key_candidates(
            legacy_system_secret=None,
            legacy_system_secrets=legacy_system_secrets,
        )
    legacy_system_secret_available = legacy_system_secret_available or bool(
        legacy_system_secrets
    )
    resolved_database = database.expanduser().resolve()
    if not resolved_database.is_file():
        raise EncryptionMigrationError(f"Database does not exist: {resolved_database}")
    key_store.load()
    notices: list[EncryptionMigrationNotice] = []
    if discovery_failures:
        notices.append(
            EncryptionMigrationNotice(
                "model-discovery-failed",
                "Some add-on model modules could not be inspected.",
                discovery_failures,
                True,
            )
        )
    inventory: list[ColumnEncryptionInventory] = []
    ownership_overrides: list[LegacyOwnerOverride] = []
    system_quarantines: list[LegacySystemQuarantine] = []
    connection = sqlite3.connect(sqlite_read_only_uri(resolved_database), uri=True)
    try:
        existing_tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if assign_unresolved_user_id is not None and (
            "user" not in existing_tables
            or connection.execute(
                'SELECT 1 FROM "user" WHERE id = ?',
                (assign_unresolved_user_id,),
            ).fetchone()
            is None
        ):
            raise EncryptionMigrationError(
                "The approved unresolved-owner target user does not exist."
            )
        specs_by_table: dict[str, list[EncryptedColumnSpec]] = {}
        for spec in specs:
            if spec.table in existing_tables:
                specs_by_table.setdefault(spec.table, []).append(spec)
        for table_name, table_specs in sorted(specs_by_table.items()):
            table_columns = {
                str(row[1])
                for row in connection.execute(
                    f"PRAGMA table_info({_quote(table_name)})"
                )
            }
            present_specs = [
                spec for spec in table_specs if spec.column in table_columns
            ]
            if not present_specs:
                continue
            count_map = _table_column_counts(
                connection,
                table_name,
                present_specs,
            )
            for spec in present_specs:
                total, v2, system, user, unknown, unprefixed = count_map[spec.key]
                required_users, missing_owners = (
                    _required_owners(connection, spec)
                    if spec.strategy == "user" and (user or unprefixed)
                    else ((), 0)
                )
                column_inventory = ColumnEncryptionInventory(
                    spec,
                    total,
                    v2,
                    system,
                    user,
                    unprefixed,
                    unknown,
                    required_users,
                    missing_owners,
                )
                inventory.append(column_inventory)
                if assign_unresolved_user_id is not None and missing_owners:
                    for cell in _unresolved_owner_cells(connection, spec):
                        ownership_overrides.append(
                            LegacyOwnerOverride(
                                table=spec.table,
                                column=spec.column,
                                rowid=cell.rowid,
                                primary_key=cell.primary_key,
                                ciphertext_sha256=_ciphertext_sha256(cell.value),
                                assigned_user_id=assign_unresolved_user_id,
                            )
                        )
        if legacy_system_keys is not None:
            if expected_system_quarantine_count is None:
                raise EncryptionMigrationError(
                    "System quarantine expected count is invalid."
                )
            for column in inventory:
                for cell in _iter_legacy_system_cells(connection, column.spec):
                    if any(
                        _try_legacy_fernet(fernet, cell.value[4:]) is not None
                        for fernet in legacy_system_keys.fernets
                    ):
                        continue
                    _validate_quarantine_ciphertext_size(cell.value)
                    if len(system_quarantines) >= expected_system_quarantine_count:
                        raise EncryptionMigrationError(
                            "Approved system quarantine count is lower than "
                            "unavailable legacy system cells."
                        )
                    system_quarantines.append(
                        LegacySystemQuarantine(
                            table=column.spec.table,
                            column=column.spec.column,
                            rowid=cell.rowid,
                            primary_key=cell.primary_key,
                            ciphertext_sha256=_ciphertext_sha256(cell.value),
                            policy=LEGACY_SYSTEM_QUARANTINE_POLICY,
                        )
                    )
    finally:
        connection.close()

    unknown_columns = tuple(
        column.spec.key for column in inventory if column.unknown_envelope_count
    )
    if unknown_columns:
        notices.append(
            EncryptionMigrationNotice(
                "unknown-envelope-version",
                "Unknown enc: envelope versions require code support before migration.",
                unknown_columns,
                True,
            )
        )
    ownerless = tuple(
        column.spec.key for column in inventory if column.missing_owner_count
    )
    if ownerless and not ownership_overrides:
        notices.append(
            EncryptionMigrationNotice(
                "legacy-user-owner-unresolved",
                "Some legacy user-key rows cannot be mapped to exactly one user.",
                ownerless,
                True,
            )
        )
    if ownership_overrides:
        expected_override_count = sum(
            column.missing_owner_count for column in inventory
        )
        if len(ownership_overrides) != expected_override_count:
            raise EncryptionMigrationError(
                "Could not create an exact override for every unresolved legacy cell."
            )
        ownership_overrides.sort(key=lambda item: (item.table, item.column, item.rowid))
        assigned_users = tuple(
            sorted({item.assigned_user_id for item in ownership_overrides})
        )
        notices.append(
            EncryptionMigrationNotice(
                "legacy-user-owner-attested",
                "Operator approved exact ownership overrides for "
                f"{len(ownership_overrides)} legacy cell(s) assigned to user "
                f"id(s): {', '.join(map(str, assigned_users))}.",
                tuple(f"{item.table}.{item.column}" for item in ownership_overrides),
                False,
            )
        )
    if legacy_system_keys is not None:
        system_quarantines.sort(key=lambda item: (item.table, item.column, item.rowid))
        if len(system_quarantines) != expected_system_quarantine_count:
            raise EncryptionMigrationError(
                "Approved system quarantine count does not match unavailable legacy "
                "system cells."
            )
        quarantine_columns = tuple(
            dict.fromkeys(f"{item.table}.{item.column}" for item in system_quarantines)
        )
        notices.append(
            EncryptionMigrationNotice(
                "legacy-system-quarantine-attested",
                "Operator approved exact quarantine of "
                f"{len(system_quarantines)} unrecoverable legacy system cell(s) "
                f"under policy {LEGACY_SYSTEM_QUARANTINE_POLICY}.",
                quarantine_columns,
                False,
            )
        )
    needs_system_secret = tuple(
        column.spec.key
        for column in inventory
        if column.legacy_system_count
        or (column.spec.strategy == "system" and column.legacy_unprefixed_count)
    )
    if needs_system_secret and not legacy_system_secret_available:
        notices.append(
            EncryptionMigrationNotice(
                "legacy-system-secret-required",
                "The former Flask secret is required to authenticate legacy system rows.",
                needs_system_secret,
                True,
            )
        )
    required_user_ids = tuple(
        sorted(
            {
                *(
                    user_id
                    for column in inventory
                    for user_id in column.required_user_ids
                ),
                *(item.assigned_user_id for item in ownership_overrides),
            }
        )
    )
    stat_result = resolved_database.stat()
    placeholder = EncryptionMigrationPlan(
        format=ENCRYPTION_MIGRATION_FORMAT,
        version=ENCRYPTION_MIGRATION_VERSION,
        plan_id="",
        created_at=_now(),
        confirmation_token="",
        database=str(resolved_database),
        database_size=stat_result.st_size,
        database_modified_ns=stat_result.st_mtime_ns,
        active_key_id=key_store.active_key_id,
        columns=tuple(inventory),
        required_user_ids=required_user_ids,
        notices=tuple(notices),
        applicable=not any(notice.blocking for notice in notices),
        ownership_overrides=tuple(ownership_overrides),
        system_quarantines=tuple(system_quarantines),
    )
    digest = encryption_migration_plan_digest(placeholder)
    result = EncryptionMigrationPlan(
        **{
            **asdict(placeholder),
            "plan_id": digest,
            "confirmation_token": f"ENCRYPT-{digest[:12].upper()}",
            "columns": placeholder.columns,
            "notices": placeholder.notices,
            "ownership_overrides": placeholder.ownership_overrides,
            "system_quarantines": placeholder.system_quarantines,
        }
    )
    _validate_quarantine_plan_size(result)
    return result


def write_encryption_migration_plan(
    plan: EncryptionMigrationPlan,
    path: Path,
) -> None:
    _validate_quarantine_plan_size(plan)
    _atomic_json_write(
        path.expanduser().resolve(),
        plan.to_dict(),
        owner_only=True,
    )


def _spec_from_dict(payload: dict[str, Any]) -> EncryptedColumnSpec:
    raw_path = payload.get("ownership_path")
    ownership_path = (
        None if raw_path is None else tuple(OwnershipHop(**item) for item in raw_path)
    )
    return EncryptedColumnSpec(
        table=payload["table"],
        column=payload["column"],
        strategy=payload["strategy"],
        primary_key_columns=tuple(payload["primary_key_columns"]),
        ownership_path=ownership_path,
        is_json=bool(payload.get("is_json", False)),
    )


def _owner_override_from_dict(payload: object) -> LegacyOwnerOverride:
    if not isinstance(payload, dict) or set(payload) != {
        "table",
        "column",
        "rowid",
        "primary_key",
        "ciphertext_sha256",
        "assigned_user_id",
    }:
        raise EncryptionMigrationError("Malformed encryption ownership override.")
    table = payload["table"]
    column = payload["column"]
    rowid = payload["rowid"]
    ciphertext_sha256 = payload["ciphertext_sha256"]
    assigned_user_id = payload["assigned_user_id"]
    raw_primary_key = payload["primary_key"]
    if (
        not isinstance(table, str)
        or not table
        or not isinstance(column, str)
        or not column
        or isinstance(rowid, bool)
        or not isinstance(rowid, int)
        or rowid < 1
        or not isinstance(ciphertext_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", ciphertext_sha256) is None
        or isinstance(assigned_user_id, bool)
        or not isinstance(assigned_user_id, int)
        or assigned_user_id < 1
        or not isinstance(raw_primary_key, list)
    ):
        raise EncryptionMigrationError("Malformed encryption ownership override.")
    primary_key: list[tuple[str, str | int | float | bool | None]] = []
    for item in raw_primary_key:
        if (
            not isinstance(item, list)
            or len(item) != 2
            or not isinstance(item[0], str)
            or not item[0]
            or (
                item[1] is not None and not isinstance(item[1], (str, int, float, bool))
            )
        ):
            raise EncryptionMigrationError("Malformed encryption ownership override.")
        primary_key.append((item[0], item[1]))
    return LegacyOwnerOverride(
        table=table,
        column=column,
        rowid=rowid,
        primary_key=tuple(primary_key),
        ciphertext_sha256=ciphertext_sha256,
        assigned_user_id=assigned_user_id,
    )


def _system_quarantine_from_dict(payload: object) -> LegacySystemQuarantine:
    if not isinstance(payload, dict) or set(payload) != {
        "table",
        "column",
        "rowid",
        "primary_key",
        "ciphertext_sha256",
        "policy",
    }:
        raise EncryptionMigrationError("Malformed legacy system quarantine entry.")
    table = payload["table"]
    column = payload["column"]
    rowid = payload["rowid"]
    ciphertext_sha256 = payload["ciphertext_sha256"]
    policy = payload["policy"]
    raw_primary_key = payload["primary_key"]
    if (
        not isinstance(table, str)
        or not table
        or not isinstance(column, str)
        or not column
        or isinstance(rowid, bool)
        or not isinstance(rowid, int)
        or rowid < 1
        or not isinstance(ciphertext_sha256, str)
        or _SHA256_PATTERN.fullmatch(ciphertext_sha256) is None
        or policy != LEGACY_SYSTEM_QUARANTINE_POLICY
        or not isinstance(raw_primary_key, list)
    ):
        raise EncryptionMigrationError("Malformed legacy system quarantine entry.")
    primary_key: list[tuple[str, str | int | float | bool | None]] = []
    for item in raw_primary_key:
        if (
            not isinstance(item, list)
            or len(item) != 2
            or not isinstance(item[0], str)
            or not item[0]
            or (
                item[1] is not None and not isinstance(item[1], (str, int, float, bool))
            )
        ):
            raise EncryptionMigrationError("Malformed legacy system quarantine entry.")
        primary_key.append((item[0], item[1]))
    return LegacySystemQuarantine(
        table=table,
        column=column,
        rowid=rowid,
        primary_key=tuple(primary_key),
        ciphertext_sha256=ciphertext_sha256,
        policy=policy,
    )


def _override_locator(override: LegacyOwnerOverride) -> tuple[str, str, int]:
    return override.table, override.column, override.rowid


def _override_cell_key(
    spec: EncryptedColumnSpec,
    cell: _MigrationCell,
) -> tuple[
    str,
    str,
    int,
    tuple[tuple[str, str | int | float | bool | None], ...],
    str,
]:
    return (
        spec.table,
        spec.column,
        cell.rowid,
        cell.primary_key,
        _ciphertext_sha256(cell.value),
    )


def _override_key(
    override: LegacyOwnerOverride,
) -> tuple[
    str,
    str,
    int,
    tuple[tuple[str, str | int | float | bool | None], ...],
    str,
]:
    return (
        override.table,
        override.column,
        override.rowid,
        override.primary_key,
        override.ciphertext_sha256,
    )


def _quarantine_locator(
    quarantine: LegacySystemQuarantine,
) -> tuple[str, str, int]:
    return quarantine.table, quarantine.column, quarantine.rowid


def _quarantine_key(
    quarantine: LegacySystemQuarantine,
) -> tuple[
    str,
    str,
    int,
    tuple[tuple[str, str | int | float | bool | None], ...],
    str,
]:
    return (
        quarantine.table,
        quarantine.column,
        quarantine.rowid,
        quarantine.primary_key,
        quarantine.ciphertext_sha256,
    )


def _validate_ownership_override_structure(plan: EncryptionMigrationPlan) -> None:
    if plan.version == ENCRYPTION_MIGRATION_LEGACY_VERSION:
        if plan.ownership_overrides or plan.system_quarantines:
            raise EncryptionMigrationError(
                "Legacy v1 encryption plans cannot contain migration extensions."
            )
        return
    if plan.version not in {
        ENCRYPTION_MIGRATION_OWNERSHIP_VERSION,
        ENCRYPTION_MIGRATION_VERSION,
    }:
        raise EncryptionMigrationError("Unsupported encryption migration plan.")
    if (
        plan.version == ENCRYPTION_MIGRATION_OWNERSHIP_VERSION
        and plan.system_quarantines
    ):
        raise EncryptionMigrationError(
            "Legacy v2 encryption plans cannot contain system quarantine entries."
        )
    specs = {
        (column.spec.table, column.spec.column): column.spec for column in plan.columns
    }
    locators: set[tuple[str, str, int]] = set()
    override_user_ids: set[int] = set()
    for override in plan.ownership_overrides:
        spec = specs.get((override.table, override.column))
        if (
            spec is None
            or spec.strategy != "user"
            or not spec.primary_key_columns
            or tuple(name for name, _value in override.primary_key)
            != spec.primary_key_columns
            or len({name for name, _value in override.primary_key})
            != len(override.primary_key)
        ):
            raise EncryptionMigrationError("Invalid encryption ownership override.")
        locator = _override_locator(override)
        if locator in locators:
            raise EncryptionMigrationError("Duplicate encryption ownership override.")
        locators.add(locator)
        override_user_ids.add(override.assigned_user_id)
    if not override_user_ids.issubset(set(plan.required_user_ids)):
        raise EncryptionMigrationError(
            "Encryption ownership override user is missing from the plan unlock set."
        )
    if len(plan.system_quarantines) > MAX_LEGACY_SYSTEM_QUARANTINE_CELLS:
        raise EncryptionMigrationError("Too many legacy system quarantine entries.")
    quarantine_locators: set[tuple[str, str, int]] = set()
    previous_sort_key: tuple[str, str, int] | None = None
    for quarantine in plan.system_quarantines:
        spec = specs.get((quarantine.table, quarantine.column))
        if (
            spec is None
            or not spec.primary_key_columns
            or quarantine.policy != LEGACY_SYSTEM_QUARANTINE_POLICY
            or tuple(name for name, _value in quarantine.primary_key)
            != spec.primary_key_columns
            or len({name for name, _value in quarantine.primary_key})
            != len(quarantine.primary_key)
        ):
            raise EncryptionMigrationError("Invalid legacy system quarantine entry.")
        locator = _quarantine_locator(quarantine)
        if locator in quarantine_locators or locator in locators:
            raise EncryptionMigrationError("Duplicate encryption migration cell entry.")
        if previous_sort_key is not None and locator <= previous_sort_key:
            raise EncryptionMigrationError(
                "Legacy system quarantine entries are unsorted."
            )
        previous_sort_key = locator
        quarantine_locators.add(locator)
    quarantine_notices = [
        notice
        for notice in plan.notices
        if notice.code == "legacy-system-quarantine-attested"
    ]
    if bool(plan.system_quarantines) != bool(quarantine_notices):
        raise EncryptionMigrationError(
            "Legacy system quarantine attestation is missing."
        )
    if len(quarantine_notices) > 1 or any(
        notice.blocking for notice in quarantine_notices
    ):
        raise EncryptionMigrationError("Invalid legacy system quarantine attestation.")


def load_encryption_migration_plan(path: Path) -> EncryptionMigrationPlan:
    try:
        resolved = path.expanduser().resolve()
        if resolved.stat().st_size > _QUARANTINE_PLAN_MAX_BYTES:
            raise EncryptionMigrationError("Encryption migration plan is too large.")
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EncryptionMigrationError(
            f"Could not read encryption migration plan: {path}"
        ) from exc
    if (
        not isinstance(payload, dict)
        or payload.get("format") != ENCRYPTION_MIGRATION_FORMAT
        or payload.get("version")
        not in {
            ENCRYPTION_MIGRATION_LEGACY_VERSION,
            ENCRYPTION_MIGRATION_OWNERSHIP_VERSION,
            ENCRYPTION_MIGRATION_VERSION,
        }
    ):
        raise EncryptionMigrationError("Unsupported encryption migration plan.")
    try:
        version = payload["version"]
        required = {
            "format",
            "version",
            "plan_id",
            "created_at",
            "confirmation_token",
            "database",
            "database_size",
            "database_modified_ns",
            "active_key_id",
            "columns",
            "required_user_ids",
            "notices",
            "applicable",
            "legacy_value_count",
        }
        if version >= ENCRYPTION_MIGRATION_OWNERSHIP_VERSION:
            required.add("ownership_overrides")
        if version >= ENCRYPTION_MIGRATION_VERSION:
            required.add("system_quarantines")
        if set(payload) != required:
            raise EncryptionMigrationError("Malformed encryption migration plan.")
        if not isinstance(payload["columns"], list):
            raise EncryptionMigrationError("Malformed encryption migration plan.")
        if version == ENCRYPTION_MIGRATION_VERSION and any(
            not isinstance(item, dict)
            or not isinstance(item.get("spec"), dict)
            or not isinstance(item["spec"].get("is_json"), bool)
            for item in payload["columns"]
        ):
            raise EncryptionMigrationError("Malformed encryption migration plan.")
        columns = tuple(
            ColumnEncryptionInventory(
                spec=_spec_from_dict(item["spec"]),
                non_null_count=item["non_null_count"],
                v2_count=item["v2_count"],
                legacy_system_count=item["legacy_system_count"],
                legacy_user_count=item["legacy_user_count"],
                legacy_unprefixed_count=item["legacy_unprefixed_count"],
                unknown_envelope_count=item["unknown_envelope_count"],
                required_user_ids=tuple(item["required_user_ids"]),
                missing_owner_count=item["missing_owner_count"],
            )
            for item in payload["columns"]
        )
        notices = tuple(
            EncryptionMigrationNotice(
                code=item["code"],
                message=item["message"],
                columns=tuple(item.get("columns") or ()),
                blocking=bool(item.get("blocking")),
            )
            for item in payload["notices"]
        )
        if version == ENCRYPTION_MIGRATION_LEGACY_VERSION:
            ownership_overrides: tuple[LegacyOwnerOverride, ...] = ()
            system_quarantines: tuple[LegacySystemQuarantine, ...] = ()
        elif version == ENCRYPTION_MIGRATION_OWNERSHIP_VERSION:
            raw_overrides = payload["ownership_overrides"]
            if not isinstance(raw_overrides, list):
                raise EncryptionMigrationError("Malformed encryption migration plan.")
            ownership_overrides = tuple(
                _owner_override_from_dict(item) for item in raw_overrides
            )
            system_quarantines = ()
        else:
            raw_overrides = payload["ownership_overrides"]
            raw_quarantines = payload["system_quarantines"]
            if not isinstance(raw_overrides, list) or not isinstance(
                raw_quarantines, list
            ):
                raise EncryptionMigrationError("Malformed encryption migration plan.")
            ownership_overrides = tuple(
                _owner_override_from_dict(item) for item in raw_overrides
            )
            system_quarantines = tuple(
                _system_quarantine_from_dict(item) for item in raw_quarantines
            )
        if not isinstance(payload["applicable"], bool):
            raise EncryptionMigrationError("Malformed encryption migration plan.")
        plan = EncryptionMigrationPlan(
            format=payload["format"],
            version=payload["version"],
            plan_id=payload["plan_id"],
            created_at=payload["created_at"],
            confirmation_token=payload["confirmation_token"],
            database=payload["database"],
            database_size=payload["database_size"],
            database_modified_ns=payload["database_modified_ns"],
            active_key_id=payload["active_key_id"],
            columns=columns,
            required_user_ids=tuple(payload["required_user_ids"]),
            notices=notices,
            applicable=payload["applicable"],
            ownership_overrides=ownership_overrides,
            system_quarantines=system_quarantines,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise EncryptionMigrationError("Malformed encryption migration plan.") from exc
    if (
        not _is_nonnegative_int(payload["legacy_value_count"])
        or payload["legacy_value_count"] != plan.legacy_value_count
    ):
        raise EncryptionMigrationError("Malformed encryption migration plan.")
    _validate_ownership_override_structure(plan)
    _validate_quarantine_plan_size(plan)
    if plan.version >= ENCRYPTION_MIGRATION_OWNERSHIP_VERSION and plan.applicable != (
        not any(notice.blocking for notice in plan.notices)
    ):
        raise EncryptionMigrationError("Malformed encryption migration plan.")
    if plan.version == ENCRYPTION_MIGRATION_LEGACY_VERSION and any(
        notice.blocking for notice in plan.notices
    ):
        plan = replace(plan, applicable=False)
    digest = encryption_migration_plan_digest(plan)
    if (
        digest != plan.plan_id
        or plan.confirmation_token != f"ENCRYPT-{digest[:12].upper()}"
    ):
        raise EncryptionMigrationError(
            "Encryption migration plan integrity check failed."
        )
    return plan


def _journal_path(paths: AppPaths, plan_id: str) -> Path:
    return paths.state_dir / "encryption-migrations" / f"{plan_id}.json"


def _quarantine_ledger_relative_path(plan: EncryptionMigrationPlan) -> Path:
    return Path("encryption-quarantine") / f"{plan.plan_id}.json"


def _quarantine_ledger_path(paths: AppPaths, plan: EncryptionMigrationPlan) -> Path:
    return paths.secrets_dir / _quarantine_ledger_relative_path(plan)


def _is_reparse_point(file_stat: os.stat_result) -> bool:
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    attributes = getattr(file_stat, "st_file_attributes", 0)
    return bool(reparse_flag and attributes & reparse_flag)


def _validate_private_directory_stat(
    file_stat: os.stat_result,
    *,
    require_owner_only: bool,
) -> None:
    if (
        stat.S_ISLNK(file_stat.st_mode)
        or _is_reparse_point(file_stat)
        or not stat.S_ISDIR(file_stat.st_mode)
    ):
        raise EncryptionMigrationError(
            "Encryption quarantine ledger directory is unsafe."
        )
    if os.name == "posix" and (
        file_stat.st_uid != os.geteuid()
        or (require_owner_only and stat.S_IMODE(file_stat.st_mode) & 0o077)
    ):
        raise EncryptionMigrationError(
            "Encryption quarantine ledger directory is unsafe."
        )


def _validate_private_directory(path: Path) -> None:
    try:
        file_stat = os.lstat(path)
    except OSError as exc:
        raise EncryptionMigrationError(
            "Encryption quarantine ledger directory is unsafe."
        ) from exc
    _validate_private_directory_stat(file_stat, require_owner_only=True)


def _harden_quarantine_ledger_directory(path: Path) -> None:
    """Owner-harden one directory without following a replaced path."""

    try:
        before_open = os.lstat(path)
        _validate_private_directory_stat(before_open, require_owner_only=False)
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
    except EncryptionMigrationError:
        raise
    except OSError as exc:
        raise EncryptionMigrationError(
            "Could not prepare encryption quarantine ledger."
        ) from exc
    try:
        opened = os.fstat(descriptor)
        _validate_private_directory_stat(opened, require_owner_only=False)
        if opened.st_dev != before_open.st_dev or opened.st_ino != before_open.st_ino:
            raise EncryptionMigrationError(
                "Encryption quarantine ledger directory is unsafe."
            )
        os.fchmod(descriptor, 0o700)
        os.fsync(descriptor)
        opened = os.fstat(descriptor)
        _validate_private_directory_stat(opened, require_owner_only=True)
        after_open = os.lstat(path)
        _validate_private_directory_stat(after_open, require_owner_only=True)
        if after_open.st_dev != opened.st_dev or after_open.st_ino != opened.st_ino:
            raise EncryptionMigrationError(
                "Encryption quarantine ledger directory is unsafe."
            )
    except EncryptionMigrationError:
        raise
    except OSError as exc:
        raise EncryptionMigrationError(
            "Could not prepare encryption quarantine ledger."
        ) from exc
    finally:
        os.close(descriptor)


def _ensure_quarantine_ledger_directory(paths: AppPaths) -> Path:
    root = paths.secrets_dir
    _validate_private_directory(root)
    directory = root / "encryption-quarantine"
    try:
        directory.mkdir(mode=0o700, exist_ok=True)
    except OSError as exc:
        raise EncryptionMigrationError(
            "Could not prepare encryption quarantine ledger."
        ) from exc
    if os.name == "posix":
        _harden_quarantine_ledger_directory(directory)
    _validate_private_directory(directory)
    return directory


def _quarantine_ledger_payload(
    plan: EncryptionMigrationPlan,
    cells: Mapping[
        tuple[
            str,
            str,
            int,
            tuple[tuple[str, str | int | float | bool | None], ...],
            str,
        ],
        _MigrationCell,
    ],
) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    for quarantine in plan.system_quarantines:
        cell = cells.get(_quarantine_key(quarantine))
        if cell is None or not cell.value.startswith("sys:"):
            raise EncryptionMigrationConflict(
                "A quarantined legacy system value is no longer available for recovery."
            )
        _validate_quarantine_ciphertext_size(cell.value)
        entries.append(
            {
                **asdict(quarantine),
                "ciphertext": cell.value,
            }
        )
    return {
        "format": ENCRYPTION_QUARANTINE_LEDGER_FORMAT,
        "version": ENCRYPTION_QUARANTINE_LEDGER_VERSION,
        "plan_id": plan.plan_id,
        "database": plan.database,
        "database_size": plan.database_size,
        "database_modified_ns": plan.database_modified_ns,
        "policy": LEGACY_SYSTEM_QUARANTINE_POLICY,
        "reason": LEGACY_SYSTEM_QUARANTINE_REASON,
        "entries": entries,
    }


def _validate_quarantine_ciphertext_size(value: str) -> None:
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise EncryptionMigrationError(
            "Legacy system quarantine ciphertext is invalid."
        ) from exc
    if len(encoded) > _QUARANTINE_CIPHERTEXT_MAX_BYTES:
        raise EncryptionMigrationError(
            "Legacy system quarantine ciphertext is too large."
        )


def _quarantine_ledger_bytes(payload: Mapping[str, Any]) -> bytes:
    try:
        encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise EncryptionMigrationError(
            "Encryption quarantine ledger is malformed."
        ) from exc
    if len(encoded) > _QUARANTINE_LEDGER_MAX_BYTES:
        raise EncryptionMigrationError("Encryption quarantine ledger is too large.")
    return encoded


def _read_private_ledger(path: Path) -> bytes:
    try:
        before_open = os.lstat(path)
        if (
            stat.S_ISLNK(before_open.st_mode)
            or _is_reparse_point(before_open)
            or not stat.S_ISREG(before_open.st_mode)
        ):
            raise EncryptionMigrationError("Encryption quarantine ledger is unsafe.")
        if os.name == "posix" and (
            before_open.st_uid != os.geteuid()
            or stat.S_IMODE(before_open.st_mode) & 0o077
        ):
            raise EncryptionMigrationError("Encryption quarantine ledger is unsafe.")
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
    except EncryptionMigrationError:
        raise
    except OSError as exc:
        raise EncryptionMigrationError(
            "Could not securely read encryption quarantine ledger."
        ) from exc
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or _is_reparse_point(opened)
            or opened.st_dev != before_open.st_dev
            or opened.st_ino != before_open.st_ino
            or opened.st_size > _QUARANTINE_LEDGER_MAX_BYTES
            or (
                os.name == "posix"
                and (
                    opened.st_uid != os.geteuid()
                    or stat.S_IMODE(opened.st_mode) & 0o077
                )
            )
        ):
            raise EncryptionMigrationError("Encryption quarantine ledger is unsafe.")
        chunks: list[bytes] = []
        remaining = _QUARANTINE_LEDGER_MAX_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        encoded = b"".join(chunks)
        if len(encoded) > _QUARANTINE_LEDGER_MAX_BYTES:
            raise EncryptionMigrationError("Encryption quarantine ledger is too large.")
        return encoded
    except OSError as exc:
        raise EncryptionMigrationError(
            "Could not securely read encryption quarantine ledger."
        ) from exc
    finally:
        os.close(descriptor)


def _validate_quarantine_ledger_payload(
    payload: object,
    plan: EncryptionMigrationPlan,
) -> None:
    if not isinstance(payload, dict) or set(payload) != {
        "format",
        "version",
        "plan_id",
        "database",
        "database_size",
        "database_modified_ns",
        "policy",
        "reason",
        "entries",
    }:
        raise EncryptionMigrationError("Encryption quarantine ledger is malformed.")
    if (
        payload["format"] != ENCRYPTION_QUARANTINE_LEDGER_FORMAT
        or payload["version"] != ENCRYPTION_QUARANTINE_LEDGER_VERSION
        or payload["plan_id"] != plan.plan_id
        or payload["database"] != plan.database
        or payload["database_size"] != plan.database_size
        or payload["database_modified_ns"] != plan.database_modified_ns
        or payload["policy"] != LEGACY_SYSTEM_QUARANTINE_POLICY
        or payload["reason"] != LEGACY_SYSTEM_QUARANTINE_REASON
        or not isinstance(payload["entries"], list)
        or len(payload["entries"]) != len(plan.system_quarantines)
    ):
        raise EncryptionMigrationError("Encryption quarantine ledger is malformed.")
    for raw_entry, quarantine in zip(
        payload["entries"], plan.system_quarantines, strict=True
    ):
        if not isinstance(raw_entry, dict) or set(raw_entry) != {
            "table",
            "column",
            "rowid",
            "primary_key",
            "ciphertext_sha256",
            "policy",
            "ciphertext",
        }:
            raise EncryptionMigrationError("Encryption quarantine ledger is malformed.")
        raw_quarantine = {
            key: value for key, value in raw_entry.items() if key != "ciphertext"
        }
        if _system_quarantine_from_dict(raw_quarantine) != quarantine:
            raise EncryptionMigrationError("Encryption quarantine ledger is malformed.")
        ciphertext = raw_entry["ciphertext"]
        if (
            not isinstance(ciphertext, str)
            or not ciphertext.startswith("sys:")
            or _ciphertext_sha256(ciphertext) != quarantine.ciphertext_sha256
        ):
            raise EncryptionMigrationError("Encryption quarantine ledger is malformed.")
        try:
            _validate_quarantine_ciphertext_size(ciphertext)
        except EncryptionMigrationError as exc:
            raise EncryptionMigrationError(
                "Encryption quarantine ledger is malformed."
            ) from exc


def _load_quarantine_ledger(path: Path, plan: EncryptionMigrationPlan) -> bytes:
    encoded = _read_private_ledger(path)
    try:
        payload = json.loads(encoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EncryptionMigrationError(
            "Encryption quarantine ledger is malformed."
        ) from exc
    _validate_quarantine_ledger_payload(payload, plan)
    return encoded


def _write_exclusive_private_ledger(path: Path, encoded: bytes) -> None:
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        raise
    except OSError as exc:
        raise EncryptionMigrationError(
            "Could not create encryption quarantine ledger."
        ) from exc
    created_stat = os.fstat(descriptor)
    try:
        if os.name == "posix":
            os.fchmod(descriptor, 0o600)
        view = memoryview(encoded)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
    except OSError as exc:
        try:
            current = os.lstat(path)
            if (
                current.st_dev == created_stat.st_dev
                and current.st_ino == created_stat.st_ino
            ):
                os.unlink(path)
        except OSError:
            pass
        raise EncryptionMigrationError(
            "Could not persist encryption quarantine ledger."
        ) from exc
    finally:
        os.close(descriptor)
    try:
        directory_descriptor = os.open(
            path.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except OSError as exc:
        raise EncryptionMigrationError(
            "Could not persist encryption quarantine ledger."
        ) from exc


def _ensure_quarantine_ledger(
    paths: AppPaths,
    plan: EncryptionMigrationPlan,
    cells: Mapping[
        tuple[
            str,
            str,
            int,
            tuple[tuple[str, str | int | float | bool | None], ...],
            str,
        ],
        _MigrationCell,
    ],
) -> tuple[Path, dict[str, str | int]]:
    directory = _ensure_quarantine_ledger_directory(paths)
    path = directory / f"{plan.plan_id}.json"
    if path != _quarantine_ledger_path(paths, plan):
        raise EncryptionMigrationError("Encryption quarantine ledger path is invalid.")
    expected: bytes | None = None
    if len(cells) == len(plan.system_quarantines):
        expected = _quarantine_ledger_bytes(_quarantine_ledger_payload(plan, cells))
    if path.exists():
        encoded = _load_quarantine_ledger(path, plan)
        if expected is not None and encoded != expected:
            raise EncryptionMigrationConflict(
                "Encryption quarantine ledger identity changed."
            )
    else:
        if expected is None:
            raise EncryptionMigrationConflict(
                "Encryption quarantine ledger is missing for an interrupted migration."
            )
        try:
            _write_exclusive_private_ledger(path, expected)
            encoded = expected
        except FileExistsError:
            encoded = _load_quarantine_ledger(path, plan)
            if encoded != expected:
                raise EncryptionMigrationConflict(
                    "Encryption quarantine ledger identity changed."
                )
    return path, {
        "relative_path": _quarantine_ledger_relative_path(plan).as_posix(),
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "count": len(plan.system_quarantines),
    }


def _initial_journal(plan: EncryptionMigrationPlan) -> dict[str, Any]:
    now = _now()
    return {
        "format": ENCRYPTION_JOURNAL_FORMAT,
        "version": ENCRYPTION_JOURNAL_VERSION,
        "plan_id": plan.plan_id,
        "status": "planned",
        "created_at": now,
        "updated_at": now,
        "columns": {},
        "migrated_values": 0,
        "quarantined_values": 0,
        "quarantine_ledger": None,
        "safety_backup": None,
        "safety_backup_mode": None,
        "pending_batch": None,
        "error": None,
    }


def _is_nonnegative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _journal_error(message: str) -> EncryptionMigrationError:
    return EncryptionMigrationError(
        f"Encryption migration journal is invalid: {message}"
    )


def _journal_primary_key(
    raw_identity: object,
    spec: EncryptedColumnSpec,
) -> tuple[tuple[str, str | int | float | bool | None], ...]:
    if not isinstance(raw_identity, list) or len(raw_identity) != len(
        spec.primary_key_columns
    ):
        raise _journal_error("pending batch primary key")
    identity: list[tuple[str, str | int | float | bool | None]] = []
    for item in raw_identity:
        if not isinstance(item, list) or len(item) != 2:
            raise _journal_error("pending batch primary key")
        name, value = item
        if (
            not isinstance(name, str)
            or value is not None
            and not isinstance(value, (str, int, float, bool))
        ):
            raise _journal_error("pending batch primary key")
        identity.append((name, value))
    if tuple(name for name, _value in identity) != spec.primary_key_columns:
        raise _journal_error("pending batch primary key")
    return tuple(identity)


def _journal_specs(plan: EncryptionMigrationPlan) -> dict[str, EncryptedColumnSpec]:
    specs = {column.spec.key: column.spec for column in plan.columns}
    if len(specs) != len(plan.columns):
        raise _journal_error("duplicate plan columns")
    return specs


def _validate_journal_quarantine_record(
    record: object,
    plan: EncryptionMigrationPlan,
    quarantined_values: object,
) -> None:
    if not _is_nonnegative_int(quarantined_values):
        raise _journal_error("quarantine count")
    assert isinstance(quarantined_values, int)
    if not plan.system_quarantines:
        if quarantined_values != 0 or record is not None:
            raise _journal_error("unexpected quarantine ledger")
        return
    if int(quarantined_values) > len(plan.system_quarantines):
        raise _journal_error("quarantine count")
    if not isinstance(record, dict) or set(record) != {
        "relative_path",
        "sha256",
        "count",
    }:
        raise _journal_error("quarantine ledger")
    if (
        record["relative_path"] != _quarantine_ledger_relative_path(plan).as_posix()
        or not isinstance(record["sha256"], str)
        or _SHA256_PATTERN.fullmatch(record["sha256"]) is None
        or record["count"] != len(plan.system_quarantines)
    ):
        raise _journal_error("quarantine ledger")


def _validate_pending_batch(
    pending: object,
    plan: EncryptionMigrationPlan,
    specs: Mapping[str, EncryptedColumnSpec],
    progress: Mapping[str, object],
) -> None:
    if pending is None:
        return
    if not isinstance(pending, dict) or set(pending) != {
        "column",
        "first_rowid",
        "last_rowid",
        "count",
        "cells",
    }:
        raise _journal_error("pending batch schema")
    key = pending["column"]
    if not isinstance(key, str) or key not in specs:
        raise _journal_error("pending batch column")
    first_rowid = pending["first_rowid"]
    last_rowid = pending["last_rowid"]
    count = pending["count"]
    cells = pending["cells"]
    if (
        not _is_positive_int(first_rowid)
        or not _is_positive_int(last_rowid)
        or not _is_positive_int(count)
        or first_rowid > last_rowid
        or not isinstance(cells, list)
        or len(cells) != count
    ):
        raise _journal_error("pending batch range")
    column_progress = progress.get(key)
    if not isinstance(column_progress, Mapping) or bool(column_progress["complete"]):
        raise _journal_error("pending batch progress")
    if first_rowid <= int(column_progress["last_rowid"]):
        raise _journal_error("pending batch ordering")
    previous_rowid = 0
    identities: set[
        tuple[int, tuple[tuple[str, str | int | float | bool | None], ...]]
    ] = set()
    for cell in cells:
        if not isinstance(cell, dict) or set(cell) != {
            "rowid",
            "primary_key",
            "ciphertext_sha256",
            "owner_id",
            "quarantined",
        }:
            raise _journal_error("pending batch cell")
        rowid = cell["rowid"]
        owner_id = cell["owner_id"]
        digest = cell["ciphertext_sha256"]
        if (
            not _is_positive_int(rowid)
            or rowid <= previous_rowid
            or owner_id is not None
            and not _is_positive_int(owner_id)
            or not isinstance(digest, str)
            or _SHA256_PATTERN.fullmatch(digest) is None
            or not isinstance(cell["quarantined"], bool)
        ):
            raise _journal_error("pending batch cell")
        identity = _journal_primary_key(cell["primary_key"], specs[key])
        expected_quarantine = (
            specs[key].table,
            specs[key].column,
            rowid,
            identity,
            digest,
        ) in {_quarantine_key(item) for item in plan.system_quarantines}
        if cell["quarantined"] != expected_quarantine:
            raise _journal_error("pending batch quarantine")
        locator = (rowid, identity)
        if locator in identities:
            raise _journal_error("pending batch duplicate cell")
        identities.add(locator)
        previous_rowid = rowid
    if cells[0]["rowid"] != first_rowid or cells[-1]["rowid"] != last_rowid:
        raise _journal_error("pending batch range")


def _validate_journal(
    payload: object,
    plan: EncryptionMigrationPlan,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise _journal_error("schema")
    required = {
        "format",
        "version",
        "plan_id",
        "status",
        "created_at",
        "updated_at",
        "columns",
        "migrated_values",
        "quarantined_values",
        "quarantine_ledger",
        "safety_backup",
        "safety_backup_mode",
        "pending_batch",
        "error",
    }
    if set(payload) != required:
        raise _journal_error("schema")
    if payload["format"] != ENCRYPTION_JOURNAL_FORMAT:
        raise _journal_error("format")
    version = payload["version"]
    if not isinstance(version, int) or isinstance(version, bool):
        raise _journal_error("version")
    if version != ENCRYPTION_JOURNAL_VERSION:
        if version in {1, 2}:
            raise EncryptionMigrationError(
                "Encryption migration journal v1/v2 cannot resume safely; create a "
                "fresh encryption migration plan."
            )
        raise _journal_error("version")
    if payload["plan_id"] != plan.plan_id:
        raise _journal_error("plan identity")
    if (
        not isinstance(payload["status"], str)
        or payload["status"] not in _JOURNAL_STATUSES
        or not isinstance(payload["created_at"], str)
        or not payload["created_at"]
        or not isinstance(payload["updated_at"], str)
        or not payload["updated_at"]
        or not _is_nonnegative_int(payload["migrated_values"])
    ):
        raise _journal_error("metadata")
    specs = _journal_specs(plan)
    columns = payload["columns"]
    if not isinstance(columns, dict) or any(key not in specs for key in columns):
        raise _journal_error("column progress")
    migrated_values = 0
    for item in columns.values():
        if not isinstance(item, dict) or set(item) != {
            "last_rowid",
            "migrated",
            "complete",
        }:
            raise _journal_error("column progress")
        if (
            not _is_nonnegative_int(item["last_rowid"])
            or not _is_nonnegative_int(item["migrated"])
            or not isinstance(item["complete"], bool)
        ):
            raise _journal_error("column progress")
        migrated_values += int(item["migrated"])
    _validate_journal_quarantine_record(
        payload["quarantine_ledger"],
        plan,
        payload["quarantined_values"],
    )
    if migrated_values != (
        int(payload["migrated_values"]) + int(payload["quarantined_values"])
    ):
        raise _journal_error("migration count")
    mode = payload["safety_backup_mode"]
    backup = payload["safety_backup"]
    if not isinstance(mode, str) or mode not in {"required", "disabled"}:
        raise _journal_error("safety backup mode")
    if mode == "disabled" and backup is not None:
        raise _journal_error("disabled safety backup")
    if mode == "required":
        if not isinstance(backup, dict) or set(backup) != {
            "path",
            "backup_id",
            "sha256",
        }:
            raise _journal_error("safety backup")
        if (
            not isinstance(backup["path"], str)
            or not Path(backup["path"]).is_absolute()
            or not isinstance(backup["backup_id"], str)
            or not backup["backup_id"]
            or not isinstance(backup["sha256"], str)
            or _SHA256_PATTERN.fullmatch(backup["sha256"]) is None
        ):
            raise _journal_error("safety backup")
    error = payload["error"]
    if error is not None and (
        not isinstance(error, dict)
        or set(error) != {"type", "at"}
        or not isinstance(error["type"], str)
        or not error["type"]
        or not isinstance(error["at"], str)
        or not error["at"]
    ):
        raise _journal_error("error record")
    _validate_pending_batch(payload["pending_batch"], plan, specs, columns)
    if payload["status"] == "complete" and payload["pending_batch"] is not None:
        raise _journal_error("complete pending batch")
    return payload


def _load_journal(path: Path, plan: EncryptionMigrationPlan) -> dict[str, Any]:
    if not path.exists():
        return _initial_journal(plan)
    try:
        if path.stat().st_size > _JOURNAL_MAX_BYTES:
            raise _journal_error("size")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EncryptionMigrationError(
            f"Encryption migration journal is unreadable: {path}"
        ) from exc
    return _validate_journal(payload, plan)


def _save_journal(path: Path, journal: dict[str, Any]) -> None:
    journal["updated_at"] = _now()
    try:
        encoded = (json.dumps(journal, indent=2, sort_keys=True) + "\n").encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise EncryptionMigrationError(
            "Encryption migration journal contains non-serializable metadata."
        ) from exc
    if len(encoded) > _JOURNAL_MAX_BYTES:
        raise EncryptionMigrationError(
            "Encryption migration batch metadata exceeds the durable journal limit."
        )
    _atomic_json_write(path, journal, owner_only=True)


def _record_safety_backup(record: BackupRecord) -> dict[str, str]:
    return {
        "path": str(record.path.resolve()),
        "backup_id": record.backup_id,
        "sha256": record.sha256,
    }


def _verify_journal_safety_backup(
    paths: AppPaths,
    journal: Mapping[str, Any],
    key_store: InstallationKeyStore,
) -> None:
    if journal["safety_backup_mode"] == "disabled":
        return
    record = journal["safety_backup"]
    assert isinstance(record, Mapping)  # Validated by _validate_journal.
    candidate = Path(str(record["path"]))
    backup_root = paths.backups_dir.resolve()
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(backup_root)
    except (OSError, ValueError) as exc:
        raise EncryptionMigrationConflict(
            "Recorded encryption migration safety backup is unavailable or out of scope."
        ) from exc
    if candidate.is_symlink() or not resolved.is_file():
        raise EncryptionMigrationConflict(
            "Recorded encryption migration safety backup is invalid."
        )
    try:
        with resolved.open("rb") as handle:
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
        manifest = verify_backup(resolved, available_key_ids=key_store.key_ids())
    except (OSError, BackupError) as exc:
        raise EncryptionMigrationConflict(
            "Recorded encryption migration safety backup failed verification."
        ) from exc
    if digest != record["sha256"] or manifest.get("backup_id") != record["backup_id"]:
        raise EncryptionMigrationConflict(
            "Recorded encryption migration safety backup identity changed."
        )


def _verify_journal_quarantine_ledger(
    paths: AppPaths,
    journal: Mapping[str, Any],
    plan: EncryptionMigrationPlan,
) -> Path | None:
    if not plan.system_quarantines:
        return None
    record = journal["quarantine_ledger"]
    assert isinstance(record, Mapping)  # Validated by _validate_journal.
    _validate_private_directory(paths.secrets_dir)
    path = _quarantine_ledger_path(paths, plan)
    try:
        encoded = _load_quarantine_ledger(path, plan)
    except EncryptionMigrationError as exc:
        raise EncryptionMigrationConflict(
            "Recorded encryption quarantine ledger failed verification."
        ) from exc
    if hashlib.sha256(encoded).hexdigest() != record["sha256"]:
        raise EncryptionMigrationConflict(
            "Recorded encryption quarantine ledger identity changed."
        )
    return path


@contextmanager
def _stopped_instance_runtime_lease(paths: AppPaths):
    """Hold the same leader lease as a managed runtime for all offline work."""

    lease = runtime_leader_lock(paths, timeout=0)
    try:
        lease.acquire()
    except Timeout as exc:
        raise EncryptionMigrationError(
            "Encryption migration requires a stopped Carlina instance."
        ) from exc
    try:
        leader = load_runtime_leader(paths) or {}
        raw_pid = leader.get("pid")
        try:
            pid = int(raw_pid) if isinstance(raw_pid, (int, str)) else 0
        except TypeError, ValueError:
            pid = 0
        if pid > 0 and process_is_running(pid):
            raise EncryptionMigrationError(
                "Encryption migration refused a running Carlina data root."
            )
        yield
    finally:
        lease.release()


def _derive_user_keys(
    connection: sqlite3.Connection,
    required_user_ids: tuple[int, ...],
    passwords: Mapping[int, str],
) -> dict[int, bytes]:
    keys: dict[int, bytes] = {}
    for user_id in required_user_ids:
        password = passwords.get(user_id)
        if password is None:
            raise EncryptionMigrationError(
                f"Password is required to migrate legacy data for user {user_id}."
            )
        row = connection.execute(
            'SELECT password_hash FROM "user" WHERE id = ?',
            (user_id,),
        ).fetchone()
        if row is None or not check_password_hash(str(row[0]), password):
            raise EncryptionMigrationError(
                f"Password authentication failed for user {user_id}."
            )
        keys[user_id] = derive_legacy_user_key(password, str(row[0]))
    return keys


def _fetch_batch(
    connection: sqlite3.Connection,
    spec: EncryptedColumnSpec,
    last_rowid: int,
    batch_size: int,
) -> list[_MigrationCell]:
    joins, owner_expression = _join_sql(spec)
    column = f'"t0".{_quote(spec.column)}'
    selected = ['"t0".rowid', column, f"{owner_expression} AS owner_id"]
    selected.extend(f'"t0".{_quote(name)}' for name in spec.primary_key_columns)
    rows = connection.execute(
        f"SELECT {', '.join(selected)} "
        f'FROM {_quote(spec.table)} AS "t0"{joins} '
        f'WHERE "t0".rowid > ? AND {column} IS NOT NULL '
        f"AND {column} NOT LIKE 'enc:v2:%' "
        f'ORDER BY "t0".rowid LIMIT ?',
        (last_rowid, batch_size),
    ).fetchall()
    result: list[_MigrationCell] = []
    for row in rows:
        value = row[1]
        if not isinstance(value, str):
            raise EncryptionMigrationError(
                f"Legacy encrypted value in {spec.key} is not text."
            )
        owner_id = row[2]
        if owner_id is None:
            resolved_owner_id = None
        else:
            try:
                resolved_owner_id = int(owner_id)
            except (TypeError, ValueError) as exc:
                raise EncryptionMigrationError(
                    f"Legacy encrypted value in {spec.key} has an invalid owner."
                ) from exc
        result.append(
            _MigrationCell(
                rowid=int(row[0]),
                value=value,
                owner_id=resolved_owner_id,
                primary_key=_primary_key_identity(spec, tuple(row[3:])),
            )
        )
    return result


def _iter_legacy_system_cells(
    connection: sqlite3.Connection,
    spec: EncryptedColumnSpec,
) -> Iterator[_MigrationCell]:
    """Stream strict legacy ``sys:`` cells for attested planning."""

    if not spec.primary_key_columns:
        raise EncryptionMigrationError(
            f"Legacy system values in {spec.key} require a declared primary key."
        )
    joins, owner_expression = _join_sql(spec)
    column = f'"t0".{_quote(spec.column)}'
    selected = ['"t0".rowid', column, f"{owner_expression} AS owner_id"]
    selected.extend(f'"t0".{_quote(name)}' for name in spec.primary_key_columns)
    cursor = connection.execute(
        f"SELECT {', '.join(selected)} "
        f'FROM {_quote(spec.table)} AS "t0"{joins} '
        f"WHERE {column} LIKE 'sys:%' ORDER BY \"t0\".rowid"
    )
    try:
        while rows := cursor.fetchmany(_LEGACY_SYSTEM_DISCOVERY_BATCH_SIZE):
            for row in rows:
                value = row[1]
                if not isinstance(value, str):
                    raise EncryptionMigrationError(
                        f"Legacy encrypted value in {spec.key} is not text."
                    )
                owner_value = row[2]
                try:
                    owner_id = None if owner_value is None else int(owner_value)
                except (TypeError, ValueError) as exc:
                    raise EncryptionMigrationError(
                        f"Legacy encrypted value in {spec.key} has an invalid owner."
                    ) from exc
                yield _MigrationCell(
                    rowid=int(row[0]),
                    value=value,
                    owner_id=owner_id,
                    primary_key=_primary_key_identity(spec, tuple(row[3:])),
                )
    finally:
        cursor.close()


def _fetch_cell_at_rowid(
    connection: sqlite3.Connection,
    spec: EncryptedColumnSpec,
    rowid: int,
) -> _MigrationCell | None:
    joins, owner_expression = _join_sql(spec)
    column = f'"t0".{_quote(spec.column)}'
    selected = ['"t0".rowid', column, f"{owner_expression} AS owner_id"]
    selected.extend(f'"t0".{_quote(name)}' for name in spec.primary_key_columns)
    row = connection.execute(
        f"SELECT {', '.join(selected)} "
        f'FROM {_quote(spec.table)} AS "t0"{joins} '
        'WHERE "t0".rowid = ?',
        (rowid,),
    ).fetchone()
    if row is None:
        return None
    value = row[1]
    if not isinstance(value, str):
        raise EncryptionMigrationConflict(
            f"Migration row in {spec.key} is missing or not text."
        )
    owner_value = row[2]
    try:
        owner_id = None if owner_value is None else int(owner_value)
    except (TypeError, ValueError) as exc:
        raise EncryptionMigrationConflict(
            f"Migration row in {spec.key} has an invalid owner."
        ) from exc
    return _MigrationCell(
        rowid=int(row[0]),
        value=value,
        owner_id=owner_id,
        primary_key=_primary_key_identity(spec, tuple(row[3:])),
    )


def _pending_batch(
    spec: EncryptedColumnSpec,
    batch: list[_MigrationCell],
    system_quarantines: Mapping[
        tuple[
            str,
            str,
            int,
            tuple[tuple[str, str | int | float | bool | None], ...],
            str,
        ],
        LegacySystemQuarantine,
    ]
    | None = None,
) -> dict[str, Any]:
    if not batch:
        raise EncryptionMigrationError("Cannot journal an empty encryption batch.")
    quarantines = {} if system_quarantines is None else system_quarantines
    return {
        "column": spec.key,
        "first_rowid": batch[0].rowid,
        "last_rowid": batch[-1].rowid,
        "count": len(batch),
        "cells": [
            {
                "rowid": cell.rowid,
                "primary_key": [list(item) for item in cell.primary_key],
                "ciphertext_sha256": _ciphertext_sha256(cell.value),
                "owner_id": cell.owner_id,
                "quarantined": _override_cell_key(spec, cell) in quarantines,
            }
            for cell in batch
        ],
    }


def _reconcile_pending_batch(
    connection: sqlite3.Connection,
    journal: dict[str, Any],
    plan: EncryptionMigrationPlan,
    service: EncryptionService,
) -> bool:
    """Reconcile pending state in memory; persist only after full preflight."""

    pending = journal["pending_batch"]
    if pending is None:
        return False
    assert isinstance(pending, Mapping)  # Validated by _validate_journal.
    specs = _journal_specs(plan)
    quarantines = {_quarantine_key(item): item for item in plan.system_quarantines}
    key = str(pending["column"])
    spec = specs[key]
    cells = pending["cells"]
    assert isinstance(cells, list)
    states: list[str] = []
    for recorded in cells:
        assert isinstance(recorded, Mapping)
        current = _fetch_cell_at_rowid(connection, spec, int(recorded["rowid"]))
        if (
            current is None
            or current.primary_key
            != _journal_primary_key(recorded["primary_key"], spec)
            or current.owner_id != recorded["owner_id"]
        ):
            raise EncryptionMigrationConflict(
                "Pending encryption migration batch no longer matches the database."
            )
        if (
            not current.value.startswith("enc:v2:")
            and _ciphertext_sha256(current.value) == recorded["ciphertext_sha256"]
        ):
            states.append("legacy")
            continue
        if current.value.startswith("enc:v2:"):
            quarantine = quarantines.get(
                (
                    spec.table,
                    spec.column,
                    int(recorded["rowid"]),
                    _journal_primary_key(recorded["primary_key"], spec),
                    str(recorded["ciphertext_sha256"]),
                )
            )
            if bool(recorded["quarantined"]):
                if quarantine is None:
                    raise EncryptionMigrationConflict(
                        "Pending encryption migration batch quarantine is invalid."
                    )
                _validate_quarantine_marker(
                    service,
                    current.value,
                    plan,
                    spec,
                    quarantine,
                )
            else:
                try:
                    plaintext = service.decrypt(current.value)
                    del plaintext
                except EncryptionError as exc:
                    raise EncryptionMigrationConflict(
                        "Pending encryption migration batch contains an invalid v2 value."
                    ) from exc
            states.append("v2")
            continue
        raise EncryptionMigrationConflict(
            "Pending encryption migration batch contains an unexpected value."
        )
    if len(set(states)) != 1:
        raise EncryptionMigrationConflict(
            "Pending encryption migration batch is partially committed."
        )
    if states[0] == "legacy":
        journal["pending_batch"] = None
        return True
    progress = journal["columns"][key]
    assert isinstance(progress, dict)
    if int(progress["last_rowid"]) >= int(pending["first_rowid"]):
        raise EncryptionMigrationConflict(
            "Pending encryption migration batch progress is ambiguous."
        )
    progress["last_rowid"] = int(pending["last_rowid"])
    progress["migrated"] = int(progress["migrated"]) + int(pending["count"])
    quarantined_count = sum(
        1 for item in cells if isinstance(item, Mapping) and item["quarantined"]
    )
    journal["migrated_values"] = int(journal["migrated_values"]) + (
        int(pending["count"]) - quarantined_count
    )
    journal["quarantined_values"] = (
        int(journal["quarantined_values"]) + quarantined_count
    )
    journal["pending_batch"] = None
    return True


def _prepare_legacy_system_key_candidates(
    *,
    legacy_system_secret: str | bytes | None,
    legacy_system_secrets: Sequence[str | bytes] | None,
) -> _LegacySystemKeyCandidates:
    if legacy_system_secret is not None and legacy_system_secrets is not None:
        raise EncryptionMigrationError(
            "Specify either one legacy system secret or a candidate sequence, not both."
        )
    if legacy_system_secrets is None:
        raw_candidates = () if legacy_system_secret is None else (legacy_system_secret,)
    else:
        if isinstance(legacy_system_secrets, (str, bytes)):
            raise EncryptionMigrationError(
                "Legacy system secret candidates must be an explicit sequence."
            )
        raw_candidates = tuple(legacy_system_secrets)
    if any(
        not isinstance(candidate, (str, bytes)) or not candidate
        for candidate in raw_candidates
    ):
        raise EncryptionMigrationError("Legacy system secret candidates are invalid.")
    normalized_candidates = tuple(
        candidate.encode("utf-8") if isinstance(candidate, str) else bytes(candidate)
        for candidate in raw_candidates
    )
    if len(set(normalized_candidates)) != len(normalized_candidates):
        raise EncryptionMigrationError(
            "Legacy system secret candidates are duplicated."
        )
    return _LegacySystemKeyCandidates(
        fernets=tuple(
            Fernet(derive_legacy_system_key(secret)) for secret in normalized_candidates
        ),
        matches=[0] * len(normalized_candidates),
    )


def _try_legacy_fernet(fernet: Fernet, value: str) -> str | None:
    try:
        return fernet.decrypt(value.encode("utf-8")).decode("utf-8", errors="replace")
    except InvalidToken, ValueError:
        return None


def _decrypt_system_envelope(
    value: str,
    spec: EncryptedColumnSpec,
    rowid: int,
    candidates: _LegacySystemKeyCandidates,
    *,
    record_match: bool,
) -> str:
    for index, fernet in enumerate(candidates.fernets):
        plaintext = _try_legacy_fernet(fernet, value[4:])
        if plaintext is None:
            continue
        if record_match:
            candidates.matches[index] += 1
        return plaintext
    raise EncryptionMigrationError(
        f"Could not authenticate legacy system value at {spec.key} rowid {rowid} (sys)."
    )


def _decrypt_prefixless_legacy_value(
    value: str,
    spec: EncryptedColumnSpec,
    user_key: bytes | None,
    candidates: _LegacySystemKeyCandidates,
    *,
    record_match: bool,
) -> str:
    if spec.strategy == "system":
        system_first = True
    else:
        system_first = False
    if system_first:
        for index, fernet in enumerate(candidates.fernets):
            plaintext = _try_legacy_fernet(fernet, value)
            if plaintext is not None:
                if record_match:
                    candidates.matches[index] += 1
                return plaintext
    if user_key is not None:
        plaintext = _try_legacy_fernet(Fernet(user_key), value)
        if plaintext is not None:
            return plaintext
    if not system_first:
        for index, fernet in enumerate(candidates.fernets):
            plaintext = _try_legacy_fernet(fernet, value)
            if plaintext is not None:
                if record_match:
                    candidates.matches[index] += 1
                return plaintext
    return value


def _decrypt_legacy_value(
    service: EncryptionService,
    value: str,
    spec: EncryptedColumnSpec,
    owner_id: int | None,
    *,
    rowid: int,
    legacy_system_keys: _LegacySystemKeyCandidates,
    user_keys: Mapping[int, bytes],
    record_system_match: bool,
) -> str:
    user_key = user_keys.get(owner_id) if owner_id is not None else None
    if value.startswith("usr:") and user_key is None:
        raise EncryptionMigrationError(
            f"Legacy user envelope in {spec.key} has no authenticated owner key."
        )
    if (
        not value.startswith(("enc:", "sys:", "usr:"))
        and spec.strategy == "user"
        and user_key is None
    ):
        raise EncryptionMigrationError(
            f"Unprefixed legacy value in {spec.key} has no authenticated owner key."
        )
    if value.startswith("sys:"):
        return _decrypt_system_envelope(
            value,
            spec,
            rowid,
            legacy_system_keys,
            record_match=record_system_match,
        )
    if not value.startswith(("enc:", "usr:")):
        return _decrypt_prefixless_legacy_value(
            value,
            spec,
            user_key,
            legacy_system_keys,
            record_match=record_system_match,
        )
    return service.decrypt(
        value,
        legacy_user_key=user_key,
        legacy_strategy=spec.strategy,
    )


def _quarantine_marker(
    plan: EncryptionMigrationPlan,
    spec: EncryptedColumnSpec,
    quarantine: LegacySystemQuarantine,
) -> str:
    marker = {
        "plan_id": plan.plan_id,
        "policy": quarantine.policy,
        "reason": LEGACY_SYSTEM_QUARANTINE_REASON,
    }
    if spec.is_json:
        return json.dumps(
            {"carlina_encryption_quarantine": marker},
            sort_keys=True,
            separators=(",", ":"),
        )
    return (
        "[Carlina encryption quarantine: legacy system value unrecoverable; "
        f"plan={plan.plan_id}; policy={quarantine.policy}; "
        f"reason={LEGACY_SYSTEM_QUARANTINE_REASON}]"
    )


def _validate_quarantine_marker(
    service: EncryptionService,
    value: str,
    plan: EncryptionMigrationPlan,
    spec: EncryptedColumnSpec,
    quarantine: LegacySystemQuarantine,
) -> None:
    if not value.startswith("enc:v2:"):
        raise EncryptionMigrationConflict(
            "Quarantined legacy system value has an unexpected replacement."
        )
    try:
        plaintext = service.decrypt(value)
    except EncryptionError as exc:
        raise EncryptionMigrationConflict(
            "Quarantined legacy system value has an invalid replacement."
        ) from exc
    if plaintext != _quarantine_marker(plan, spec, quarantine):
        raise EncryptionMigrationConflict(
            "Quarantined legacy system value replacement does not match the plan."
        )


def _validated_system_quarantines(
    connection: sqlite3.Connection,
    plan: EncryptionMigrationPlan,
    service: EncryptionService,
    legacy_system_keys: _LegacySystemKeyCandidates,
) -> dict[
    tuple[
        str,
        str,
        int,
        tuple[tuple[str, str | int | float | bool | None], ...],
        str,
    ],
    _MigrationCell,
]:
    """Validate every attested loss cell without allowing a broad fallback."""

    _validate_ownership_override_structure(plan)
    specs = {
        (column.spec.table, column.spec.column): column.spec for column in plan.columns
    }
    source_cells: dict[
        tuple[
            str,
            str,
            int,
            tuple[tuple[str, str | int | float | bool | None], ...],
            str,
        ],
        _MigrationCell,
    ] = {}
    for quarantine in plan.system_quarantines:
        spec = specs[(quarantine.table, quarantine.column)]
        cell = _fetch_cell_at_rowid(connection, spec, quarantine.rowid)
        if cell is None or cell.primary_key != quarantine.primary_key:
            raise EncryptionMigrationConflict(
                "A quarantined legacy system cell no longer matches the plan."
            )
        key = _quarantine_key(quarantine)
        if cell.value.startswith("sys:"):
            if _ciphertext_sha256(cell.value) != quarantine.ciphertext_sha256:
                raise EncryptionMigrationConflict(
                    "A quarantined legacy system cell ciphertext changed."
                )
            if any(
                _try_legacy_fernet(fernet, cell.value[4:]) is not None
                for fernet in legacy_system_keys.fernets
            ):
                raise EncryptionMigrationConflict(
                    "A quarantined legacy system value now authenticates; create a "
                    "fresh no-loss plan."
                )
            source_cells[key] = cell
            continue
        _validate_quarantine_marker(service, cell.value, plan, spec, quarantine)
    return source_cells


def _migrate_value(
    service: EncryptionService,
    value: str,
    spec: EncryptedColumnSpec,
    owner_id: int | None,
    *,
    rowid: int,
    legacy_system_keys: _LegacySystemKeyCandidates,
    user_keys: Mapping[int, bytes],
) -> str:
    plaintext = _decrypt_legacy_value(
        service,
        value,
        spec,
        owner_id,
        rowid=rowid,
        legacy_system_keys=legacy_system_keys,
        user_keys=user_keys,
        record_system_match=False,
    )
    return service.encrypt(plaintext)


def _validated_ownership_overrides(
    connection: sqlite3.Connection,
    plan: EncryptionMigrationPlan,
    journal: Mapping[str, Any],
    *,
    batch_size: int,
) -> dict[
    tuple[
        str,
        str,
        int,
        tuple[tuple[str, str | int | float | bool | None], ...],
        str,
    ],
    LegacyOwnerOverride,
]:
    _validate_ownership_override_structure(plan)
    overrides = {_override_key(item): item for item in plan.ownership_overrides}
    by_locator = {_override_locator(item): item for item in plan.ownership_overrides}
    for user_id in sorted({item.assigned_user_id for item in overrides.values()}):
        if (
            connection.execute(
                'SELECT 1 FROM "user" WHERE id = ?', (user_id,)
            ).fetchone()
            is None
        ):
            raise EncryptionMigrationConflict(
                "An approved encryption ownership override target user is missing."
            )

    used_locators: set[tuple[str, str, int]] = set()
    for column in plan.columns:
        spec = column.spec
        if spec.strategy != "user":
            continue
        last_rowid = 0
        while True:
            batch = _fetch_batch(connection, spec, last_rowid, batch_size)
            if not batch:
                break
            for cell in batch:
                if not _requires_legacy_user_owner(spec, cell.value):
                    continue
                locator = (spec.table, spec.column, cell.rowid)
                if cell.owner_id is not None:
                    if locator in by_locator:
                        raise EncryptionMigrationConflict(
                            "An encryption ownership override no longer targets an "
                            "ownerless legacy value."
                        )
                    continue
                if _override_cell_key(spec, cell) not in overrides:
                    raise EncryptionMigrationConflict(
                        "An unresolved legacy user value has no exact approved "
                        "ownership override."
                    )
                used_locators.add(locator)
            last_rowid = batch[-1].rowid

    column_progress = journal.get("columns")
    if not isinstance(column_progress, Mapping):
        raise EncryptionMigrationError("Encryption migration journal is malformed.")
    specs = {
        (column.spec.table, column.spec.column): column.spec for column in plan.columns
    }
    for locator, override in by_locator.items():
        if locator in used_locators:
            continue
        spec = specs[(override.table, override.column)]
        selected_primary_key = ", ".join(
            _quote(name) for name in spec.primary_key_columns
        )
        row = connection.execute(
            f"SELECT {_quote(spec.column)}, {selected_primary_key} "
            f"FROM {_quote(spec.table)} WHERE rowid = ?",
            (override.rowid,),
        ).fetchone()
        if row is None or not isinstance(row[0], str):
            raise EncryptionMigrationConflict(
                "An encryption ownership override targets a missing or invalid row."
            )
        if _primary_key_identity(spec, tuple(row[1:])) != override.primary_key:
            raise EncryptionMigrationConflict(
                "An encryption ownership override primary key changed after planning."
            )
        progress = column_progress.get(spec.key)
        migrated_through = (
            int(progress.get("last_rowid") or 0) if isinstance(progress, Mapping) else 0
        )
        if not row[0].startswith("enc:v2:") or migrated_through < override.rowid:
            raise EncryptionMigrationConflict(
                "An encryption ownership override is stale or unused."
            )
    return overrides


def _resolve_cell_owner(
    spec: EncryptedColumnSpec,
    cell: _MigrationCell,
    overrides: Mapping[
        tuple[
            str,
            str,
            int,
            tuple[tuple[str, str | int | float | bool | None], ...],
            str,
        ],
        LegacyOwnerOverride,
    ],
) -> int | None:
    if not _requires_legacy_user_owner(spec, cell.value) or cell.owner_id is not None:
        return cell.owner_id
    override = overrides.get(_override_cell_key(spec, cell))
    if override is None:
        raise EncryptionMigrationConflict(
            "An unresolved legacy user value has no exact approved ownership override."
        )
    return override.assigned_user_id


def _preflight_legacy_values(
    connection: sqlite3.Connection,
    plan: EncryptionMigrationPlan,
    service: EncryptionService,
    *,
    legacy_system_keys: _LegacySystemKeyCandidates,
    user_keys: Mapping[int, bytes],
    ownership_overrides: Mapping[
        tuple[
            str,
            str,
            int,
            tuple[tuple[str, str | int | float | bool | None], ...],
            str,
        ],
        LegacyOwnerOverride,
    ],
    system_quarantines: Mapping[
        tuple[
            str,
            str,
            int,
            tuple[tuple[str, str | int | float | bool | None], ...],
            str,
        ],
        _MigrationCell,
    ],
    batch_size: int,
) -> int:
    verified_values = 0
    for column in plan.columns:
        last_rowid = 0
        while True:
            batch = _fetch_batch(connection, column.spec, last_rowid, batch_size)
            if not batch:
                break
            for cell in batch:
                if _override_cell_key(column.spec, cell) in system_quarantines:
                    continue
                owner_id = _resolve_cell_owner(
                    column.spec,
                    cell,
                    ownership_overrides,
                )
                plaintext = _decrypt_legacy_value(
                    service,
                    cell.value,
                    column.spec,
                    owner_id,
                    rowid=cell.rowid,
                    legacy_system_keys=legacy_system_keys,
                    user_keys=user_keys,
                    record_system_match=True,
                )
                del plaintext
                verified_values += 1
            last_rowid = batch[-1].rowid
    return verified_values


def _assert_journal_complete(
    connection: sqlite3.Connection,
    plan: EncryptionMigrationPlan,
    journal: Mapping[str, Any],
    service: EncryptionService,
) -> None:
    columns = journal["columns"]
    assert isinstance(columns, Mapping)
    incomplete = [
        column.spec.key
        for column in plan.columns
        if column.legacy_count
        and (
            not isinstance(columns.get(column.spec.key), Mapping)
            or not bool(columns[column.spec.key]["complete"])
        )
    ]
    if incomplete:
        raise EncryptionMigrationConflict(
            "Completed encryption migration journal has incomplete column progress."
        )
    remaining = []
    for column in plan.columns:
        counts = _column_counts(connection, column.spec)
        if counts[2] or counts[3] or counts[4] or counts[5]:
            remaining.append(column.spec.key)
    if remaining:
        raise EncryptionMigrationConflict(
            "Completed encryption migration still has legacy or unknown values: "
            + ", ".join(remaining[:20])
        )
    if int(journal["quarantined_values"]) != len(plan.system_quarantines):
        raise EncryptionMigrationConflict(
            "Completed encryption migration quarantine count does not match the plan."
        )
    specs = {
        (column.spec.table, column.spec.column): column.spec for column in plan.columns
    }
    for quarantine in plan.system_quarantines:
        spec = specs[(quarantine.table, quarantine.column)]
        cell = _fetch_cell_at_rowid(connection, spec, quarantine.rowid)
        if cell is None or cell.primary_key != quarantine.primary_key:
            raise EncryptionMigrationConflict(
                "Completed encryption migration quarantine cell no longer matches."
            )
        _validate_quarantine_marker(service, cell.value, plan, spec, quarantine)


def _validate_migration_plan_identity(
    plan: EncryptionMigrationPlan,
    paths: AppPaths,
    key_store: InstallationKeyStore,
) -> Path:
    if not plan.applicable:
        reasons = "; ".join(
            notice.message for notice in plan.notices if notice.blocking
        )
        raise EncryptionMigrationError(f"Plan is not applicable: {reasons}")
    _validate_ownership_override_structure(plan)
    if encryption_migration_plan_digest(plan) != plan.plan_id:
        raise EncryptionMigrationError("Encryption migration plan integrity failed.")
    if plan.confirmation_token != f"ENCRYPT-{plan.plan_id[:12].upper()}":
        raise EncryptionMigrationError("Encryption migration plan integrity failed.")
    database = Path(plan.database).expanduser().resolve()
    if database != paths.database_file.resolve():
        raise EncryptionMigrationError(
            "Plan database does not match the active Carlina database."
        )
    key_store.load()
    if key_store.active_key_id != plan.active_key_id:
        raise EncryptionMigrationConflict(
            "Active encryption key changed after planning; create a fresh plan."
        )
    return database


def verify_encryption_migration(
    plan: EncryptionMigrationPlan,
    paths: AppPaths,
    key_store: InstallationKeyStore,
    *,
    user_passwords: Mapping[int, str],
    legacy_system_secret: str | bytes | None = None,
    legacy_system_secrets: Sequence[str | bytes] | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> EncryptionMigrationVerification:
    """Authenticate every remaining legacy value without any migration writes."""

    if batch_size < 1 or batch_size > 10_000:
        raise ValueError("batch_size must be between 1 and 10,000.")
    legacy_system_keys = _prepare_legacy_system_key_candidates(
        legacy_system_secret=legacy_system_secret,
        legacy_system_secrets=legacy_system_secrets,
    )
    database = _validate_migration_plan_identity(plan, paths, key_store)
    journal_path = _journal_path(paths, plan.plan_id)
    connection: sqlite3.Connection | None = None
    try:
        with _stopped_instance_runtime_lease(paths):
            journal_exists = journal_path.exists()
            journal = _load_journal(journal_path, plan)
            if not journal_exists:
                stat_result = database.stat()
                if (
                    stat_result.st_size != plan.database_size
                    or stat_result.st_mtime_ns != plan.database_modified_ns
                ):
                    raise EncryptionMigrationConflict(
                        "Database changed after the encryption plan was created."
                    )
            else:
                if journal["safety_backup_mode"] == "required":
                    _verify_journal_safety_backup(paths, journal, key_store)
                _verify_journal_quarantine_ledger(paths, journal, plan)

            connection = sqlite3.connect(
                sqlite_read_only_uri(database),
                uri=True,
                timeout=30,
            )
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=30000")
            connection.execute("PRAGMA query_only=ON")
            service = EncryptionService(key_store)
            _reconcile_pending_batch(connection, journal, plan, service)
            if journal["status"] == "complete":
                _assert_journal_complete(connection, plan, journal, service)
                return EncryptionMigrationVerification(
                    plan.plan_id,
                    0,
                    len(plan.system_quarantines),
                    tuple(legacy_system_keys.matches),
                    plan.required_user_ids,
                )
            user_keys = _derive_user_keys(
                connection,
                plan.required_user_ids,
                user_passwords,
            )
            ownership_overrides = _validated_ownership_overrides(
                connection,
                plan,
                journal,
                batch_size=batch_size,
            )
            system_quarantines = _validated_system_quarantines(
                connection,
                plan,
                service,
                legacy_system_keys,
            )
            verified_legacy_values = _preflight_legacy_values(
                connection,
                plan,
                service,
                legacy_system_keys=legacy_system_keys,
                user_keys=user_keys,
                ownership_overrides=ownership_overrides,
                system_quarantines=system_quarantines,
                batch_size=batch_size,
            )
            return EncryptionMigrationVerification(
                plan.plan_id,
                verified_legacy_values,
                len(plan.system_quarantines),
                tuple(legacy_system_keys.matches),
                plan.required_user_ids,
            )
    except (EncryptionError, sqlite3.Error) as exc:
        raise EncryptionMigrationError(str(exc)) from exc
    finally:
        if connection is not None:
            connection.close()


def apply_encryption_migration(
    plan: EncryptionMigrationPlan,
    paths: AppPaths,
    key_store: InstallationKeyStore,
    *,
    confirmation: str,
    user_passwords: Mapping[int, str],
    legacy_system_secret: str | bytes | None = None,
    legacy_system_secrets: Sequence[str | bytes] | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    create_safety_backup: bool = True,
    fault_injector: FaultInjector | None = None,
) -> EncryptionMigrationResult:
    """Apply or resume an offline v2 migration in small committed batches."""

    if confirmation != plan.confirmation_token:
        raise EncryptionMigrationConfirmationRequired(
            f"Exact confirmation required: {plan.confirmation_token}"
        )
    if batch_size < 1 or batch_size > 10_000:
        raise ValueError("batch_size must be between 1 and 10,000.")
    legacy_system_keys = _prepare_legacy_system_key_candidates(
        legacy_system_secret=legacy_system_secret,
        legacy_system_secrets=legacy_system_secrets,
    )
    database = _validate_migration_plan_identity(plan, paths, key_store)

    journal_path = _journal_path(paths, plan.plan_id)
    lock_path = database.with_name(f".{database.name}.encryption-migration.lock")
    lock = FileLock(lock_path, timeout=0)
    try:
        lock.acquire()
    except Timeout as exc:
        raise EncryptionMigrationError(
            "Another encryption migration is already running."
        ) from exc

    safety_backup: BackupRecord | None = None
    quarantine_ledger: Path | None = None
    reused_batches = 0
    connection: sqlite3.Connection | None = None
    journal_written = False
    journal: dict[str, Any] | None = None
    try:
        with _stopped_instance_runtime_lease(paths):
            journal_exists = journal_path.exists()
            journal = _load_journal(journal_path, plan)
            if not journal_exists:
                stat_result = database.stat()
                if (
                    stat_result.st_size != plan.database_size
                    or stat_result.st_mtime_ns != plan.database_modified_ns
                ):
                    raise EncryptionMigrationConflict(
                        "Database changed after the encryption plan was created."
                    )
            else:
                if journal["safety_backup_mode"] == "required":
                    _verify_journal_safety_backup(paths, journal, key_store)
                quarantine_ledger = _verify_journal_quarantine_ledger(
                    paths, journal, plan
                )

            connection = sqlite3.connect(database, timeout=30)
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=30000")
            connection.execute("PRAGMA query_only=ON")
            service = EncryptionService(key_store)
            _reconcile_pending_batch(
                connection,
                journal,
                plan,
                service,
            )
            if journal["status"] == "complete":
                _assert_journal_complete(connection, plan, journal, service)
                return EncryptionMigrationResult(
                    plan.plan_id,
                    int(journal["migrated_values"]),
                    int(journal["quarantined_values"]),
                    len(plan.columns),
                    None,
                    journal_path,
                    quarantine_ledger,
                )

            user_keys = _derive_user_keys(
                connection,
                plan.required_user_ids,
                user_passwords,
            )
            ownership_overrides = _validated_ownership_overrides(
                connection,
                plan,
                journal,
                batch_size=batch_size,
            )
            system_quarantine_cells = _validated_system_quarantines(
                connection,
                plan,
                service,
                legacy_system_keys,
            )
            _preflight_legacy_values(
                connection,
                plan,
                service,
                legacy_system_keys=legacy_system_keys,
                user_keys=user_keys,
                ownership_overrides=ownership_overrides,
                system_quarantines=system_quarantine_cells,
                batch_size=batch_size,
            )
            connection.execute("PRAGMA query_only=OFF")

            quarantine_entries = {
                _quarantine_key(item): item for item in plan.system_quarantines
            }
            if plan.system_quarantines:
                if fault_injector is not None:
                    fault_injector("before_quarantine_ledger", None)
                quarantine_ledger, ledger_record = _ensure_quarantine_ledger(
                    paths,
                    plan,
                    system_quarantine_cells,
                )
                journal["quarantine_ledger"] = ledger_record
                if fault_injector is not None:
                    fault_injector("after_quarantine_ledger_before_journal", None)

            if journal["safety_backup_mode"] is None:
                journal["safety_backup_mode"] = (
                    "required" if create_safety_backup else "disabled"
                )
                if journal["safety_backup_mode"] == "required":
                    safety_backup = create_backup(paths, key_store=key_store)
                    journal["safety_backup"] = _record_safety_backup(safety_backup)
            journal["status"] = "running"
            journal["error"] = None
            _save_journal(journal_path, journal)
            journal_written = True

            column_journals = journal["columns"]
            assert isinstance(column_journals, dict)
            for column in plan.columns:
                if not column.legacy_count:
                    continue
                spec = column.spec
                progress = column_journals.setdefault(
                    spec.key,
                    {"last_rowid": 0, "migrated": 0, "complete": False},
                )
                assert isinstance(progress, dict)
                if progress["complete"]:
                    reused_batches += 1
                    continue
                while True:
                    batch = _fetch_batch(
                        connection,
                        spec,
                        int(progress["last_rowid"]),
                        batch_size,
                    )
                    if not batch:
                        progress["complete"] = True
                        _save_journal(journal_path, journal)
                        break
                    journal["pending_batch"] = _pending_batch(
                        spec,
                        batch,
                        quarantine_entries,
                    )
                    _save_journal(journal_path, journal)
                    try:
                        connection.execute("BEGIN IMMEDIATE")
                        for cell in batch:
                            quarantine = quarantine_entries.get(
                                _override_cell_key(spec, cell)
                            )
                            if quarantine is not None:
                                replacement = service.encrypt(
                                    _quarantine_marker(plan, spec, quarantine)
                                )
                            else:
                                owner_id = _resolve_cell_owner(
                                    spec,
                                    cell,
                                    ownership_overrides,
                                )
                                replacement = _migrate_value(
                                    service,
                                    cell.value,
                                    spec,
                                    owner_id,
                                    rowid=cell.rowid,
                                    legacy_system_keys=legacy_system_keys,
                                    user_keys=user_keys,
                                )
                            cursor = connection.execute(
                                f"UPDATE {_quote(spec.table)} SET {_quote(spec.column)} = ? "
                                "WHERE rowid = ? AND "
                                f"{_quote(spec.column)} = ?",
                                (replacement, cell.rowid, cell.value),
                            )
                            if cursor.rowcount != 1:
                                raise EncryptionMigrationConflict(
                                    "Concurrent update detected during encryption migration."
                                )
                        connection.commit()
                    except BaseException:
                        connection.rollback()
                        raise
                    if fault_injector is not None:
                        fault_injector(
                            "after_db_commit_before_progress_journal", spec.key
                        )
                    progress["last_rowid"] = batch[-1].rowid
                    progress["migrated"] = int(progress["migrated"]) + len(batch)
                    quarantined_count = sum(
                        1
                        for cell in batch
                        if _override_cell_key(spec, cell) in quarantine_entries
                    )
                    journal["migrated_values"] = int(journal["migrated_values"]) + (
                        len(batch) - quarantined_count
                    )
                    journal["quarantined_values"] = (
                        int(journal["quarantined_values"]) + quarantined_count
                    )
                    journal["pending_batch"] = None
                    _save_journal(journal_path, journal)
                    if fault_injector is not None:
                        fault_injector("after_batch", spec.key)

            _assert_journal_complete(connection, plan, journal, service)
            journal["status"] = "complete"
            _save_journal(journal_path, journal)
            return EncryptionMigrationResult(
                plan.plan_id,
                int(journal["migrated_values"]),
                int(journal["quarantined_values"]),
                reused_batches,
                safety_backup,
                journal_path,
                quarantine_ledger,
            )
    except (EncryptionError, sqlite3.Error) as exc:
        if journal_written and journal is not None and journal["pending_batch"] is None:
            journal["status"] = "interrupted"
            journal["error"] = {
                "type": type(exc).__name__,
                "at": _now(),
            }
            _save_journal(journal_path, journal)
        raise EncryptionMigrationError(str(exc)) from exc
    except BaseException as exc:
        if journal_written and journal is not None and journal["pending_batch"] is None:
            journal["status"] = "interrupted"
            journal["error"] = {
                "type": type(exc).__name__,
                "at": _now(),
            }
            _save_journal(journal_path, journal)
        raise
    finally:
        if connection is not None:
            connection.close()
        lock.release()


__all__ = [
    "DEFAULT_BATCH_SIZE",
    "ENCRYPTION_JOURNAL_FORMAT",
    "ENCRYPTION_JOURNAL_VERSION",
    "ENCRYPTION_MIGRATION_FORMAT",
    "ENCRYPTION_MIGRATION_LEGACY_VERSION",
    "ENCRYPTION_MIGRATION_OWNERSHIP_VERSION",
    "ENCRYPTION_MIGRATION_VERSION",
    "ENCRYPTION_QUARANTINE_LEDGER_FORMAT",
    "ENCRYPTION_QUARANTINE_LEDGER_VERSION",
    "LEGACY_SYSTEM_QUARANTINE_POLICY",
    "MAX_LEGACY_SYSTEM_QUARANTINE_CELLS",
    "ColumnEncryptionInventory",
    "EncryptedColumnSpec",
    "EncryptionMigrationConfirmationRequired",
    "EncryptionMigrationConflict",
    "EncryptionMigrationError",
    "EncryptionMigrationNotice",
    "EncryptionMigrationPlan",
    "EncryptionMigrationResult",
    "EncryptionMigrationVerification",
    "LegacyOwnerOverride",
    "LegacySystemQuarantine",
    "OwnershipHop",
    "apply_encryption_migration",
    "build_encryption_migration_plan",
    "discover_encrypted_model_contract",
    "encrypted_columns_from_metadata",
    "encryption_migration_plan_digest",
    "load_encryption_migration_plan",
    "verify_encryption_migration",
    "write_encryption_migration_plan",
]
