"""Shared filesystem predicates for add-on-owned database schemas."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa

from .migration_contracts import (
    AddonMigrationContract,
    inspect_addon_migration_contract,
)


def core_database_table_names() -> frozenset[str]:
    """Return tables mapped by core models, excluding add-on model modules."""

    from ... import models as core_models
    from ...extensions import db

    registry = getattr(db.Model, "registry", None)
    mapped_tables = {
        str(mapper.local_table.name)
        for mapper in getattr(registry, "mappers", ())
        if str(mapper.class_.__module__).startswith("iarf_assistant.models")
    }
    declared_tables = {
        str(value.name)
        for value in vars(core_models).values()
        if isinstance(value, sa.Table) and value.metadata is db.metadata
    }
    return frozenset(mapped_tables | declared_tables | {"alembic_version"})


def addon_defines_database_models(addon_dir: str | Path) -> bool:
    """Return whether a package owns a model module or model package."""

    root = Path(addon_dir)
    return (root / "models.py").is_file() or (root / "models" / "__init__.py").is_file()


def addon_migration_contract(
    addon_dir: str | Path,
    addon_id: str | None = None,
) -> AddonMigrationContract | None:
    """Return the validated static contract, if the bundle is usable."""

    root = Path(addon_dir)
    contract, _issues = inspect_addon_migration_contract(
        root,
        str(addon_id or root.name),
    )
    return contract


def addon_migration_contract_issues(
    addon_dir: str | Path,
    addon_id: str | None = None,
) -> tuple[str, ...]:
    """Return static layout, configuration, and revision-graph problems."""

    root = Path(addon_dir)
    _contract, issues = inspect_addon_migration_contract(
        root,
        str(addon_id or root.name),
    )
    return issues


def addon_has_migrations(
    addon_dir: str | Path,
    addon_id: str | None = None,
) -> bool:
    """Return whether a package ships a statically valid migration contract."""

    return addon_migration_contract(addon_dir, addon_id) is not None


__all__ = [
    "addon_defines_database_models",
    "addon_has_migrations",
    "addon_migration_contract",
    "addon_migration_contract_issues",
    "core_database_table_names",
]
