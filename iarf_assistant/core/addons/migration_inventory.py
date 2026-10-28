"""Read-only classification of physical and recorded add-on migration state."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

import sqlalchemy as sa
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ...models import Addon
from .contracts import ResolvedAddonContract

AddonMigrationClassification = Literal[
    "known_head",
    "known_behind",
    "mirror_mismatch",
    "uninitialized",
    "legacy_adoption_required",
    "unknown_quarantined",
    "orphaned_empty",
    "orphaned_unknown",
]


@dataclass(frozen=True, slots=True)
class AddonMigrationInventoryItem:
    addon_id: str | None
    version_table: str
    classification: AddonMigrationClassification
    physical_revision: str | None = None
    recorded_revision: str | None = None
    head_revision: str | None = None
    revision_count: int = 0
    mirror_mismatch: bool = False
    detail: str = ""


@dataclass(frozen=True, slots=True)
class _MigrationDescriptor:
    version_table: str
    head_revision: str
    revision_ids: tuple[str, ...]
    revision_count: int


def _stored_migration_descriptor(record: Addon) -> _MigrationDescriptor | None:
    payload = (
        record.resolved_contract_json
        if isinstance(record.resolved_contract_json, Mapping)
        else {}
    )
    migration = payload.get("database_migration")
    if not isinstance(migration, Mapping):
        return None
    version_table = str(migration.get("version_table") or "").strip()
    head_revision = str(migration.get("head_revision") or "").strip()
    raw_revision_ids = migration.get("revision_ids")
    if (
        not version_table
        or not head_revision
        or not isinstance(raw_revision_ids, (list, tuple))
    ):
        return None
    revision_ids = tuple(str(value) for value in raw_revision_ids if str(value))
    if not revision_ids or head_revision not in revision_ids:
        return None
    return _MigrationDescriptor(
        version_table=version_table,
        head_revision=head_revision,
        revision_ids=revision_ids,
        revision_count=len(revision_ids),
    )


def _version_rows(
    session: Session,
    table_name: str,
) -> tuple[list[str], str | None]:
    try:
        table = sa.Table(
            table_name,
            sa.MetaData(),
            autoload_with=session.get_bind(),
        )
        column = table.c.get("version_num")
        if column is None:
            return [], "the table has no version_num column"
        rows = list(session.execute(sa.select(column).limit(2)).scalars().all())
        if any(value is None for value in rows):
            return [], "the version table contains a NULL revision"
        return [str(value) for value in rows], None
    except SQLAlchemyError as exc:
        return [], f"the version table could not be read ({type(exc).__name__})"


def _known_item(
    *,
    addon_id: str,
    migration: Any,
    recorded: str | None,
    table_names: set[str],
    session: Session,
) -> AddonMigrationInventoryItem:
    table_name = migration.version_table
    common = {
        "addon_id": addon_id,
        "version_table": table_name,
        "recorded_revision": recorded,
        "head_revision": migration.head_revision,
        "revision_count": migration.revision_count,
    }
    if table_name not in table_names:
        classification: AddonMigrationClassification = (
            "legacy_adoption_required" if recorded else "uninitialized"
        )
        return AddonMigrationInventoryItem(
            **common,
            classification=classification,
            mirror_mismatch=bool(recorded),
            detail="The physical version table is missing.",
        )
    rows, read_error = _version_rows(session, table_name)
    if read_error or len(rows) > 1:
        detail = read_error or "the version table contains multiple rows"
        return AddonMigrationInventoryItem(
            **common,
            classification="unknown_quarantined",
            mirror_mismatch=True,
            detail=detail,
        )
    physical = rows[0] if rows else None
    if physical is None:
        classification = "legacy_adoption_required" if recorded else "uninitialized"
        return AddonMigrationInventoryItem(
            **common,
            classification=classification,
            mirror_mismatch=bool(recorded),
            detail="The physical version table is empty.",
        )
    known = set(migration.revision_ids)
    if physical not in known or (recorded is not None and recorded not in known):
        return AddonMigrationInventoryItem(
            **common,
            classification="unknown_quarantined",
            physical_revision=physical,
            mirror_mismatch=physical != recorded,
            detail="A physical or recorded revision is absent from the package graph.",
        )
    mismatch = physical != recorded
    if physical != migration.head_revision:
        classification = "known_behind"
        detail = "A reviewed forward migration is available."
    elif mismatch:
        classification = "mirror_mismatch"
        detail = "The physical revision is valid; manager bookkeeping is stale."
    else:
        classification = "known_head"
        detail = "The physical and recorded revisions match the packaged head."
    return AddonMigrationInventoryItem(
        **common,
        classification=classification,
        physical_revision=physical,
        mirror_mismatch=mismatch,
        detail=detail,
    )


def inspect_addon_migration_inventory(
    session: Session,
    contracts: Mapping[str, ResolvedAddonContract],
) -> tuple[AddonMigrationInventoryItem, ...]:
    """Classify migration metadata without reading add-on-owned application rows."""

    addon_records = {
        record.id: record for record in session.query(Addon).order_by(Addon.id).all()
    }
    migration_contracts: dict[str, Any] = {
        addon_id: contract.database_migration
        for addon_id, contract in contracts.items()
        if contract.database_migration is not None
    }
    for addon_id, record in addon_records.items():
        if addon_id not in migration_contracts:
            stored = _stored_migration_descriptor(record)
            if stored is not None:
                migration_contracts[addon_id] = stored
    addon_ids = sorted(migration_contracts)
    table_names = set(sa.inspect(session.get_bind()).get_table_names())
    items = [
        _known_item(
            addon_id=addon_id,
            migration=migration_contracts[addon_id],
            recorded=(
                addon_records[addon_id].alembic_revision
                if addon_id in addon_records
                else None
            ),
            table_names=table_names,
            session=session,
        )
        for addon_id in addon_ids
    ]
    owned_tables = {
        migration.version_table for migration in migration_contracts.values()
    }
    orphan_tables = sorted(
        table_name
        for table_name in table_names
        if table_name.startswith("alembic_version_") and table_name not in owned_tables
    )
    for table_name in orphan_tables:
        rows, read_error = _version_rows(session, table_name)
        classification: AddonMigrationClassification = (
            "orphaned_empty" if not rows and read_error is None else "orphaned_unknown"
        )
        items.append(
            AddonMigrationInventoryItem(
                addon_id=None,
                version_table=table_name,
                classification=classification,
                physical_revision=rows[0] if len(rows) == 1 else None,
                detail=(
                    read_error
                    or (
                        "No discovered package or retained contract owns this "
                        "version table."
                    )
                ),
            )
        )
    return tuple(items)


__all__ = [
    "AddonMigrationClassification",
    "AddonMigrationInventoryItem",
    "inspect_addon_migration_inventory",
]
