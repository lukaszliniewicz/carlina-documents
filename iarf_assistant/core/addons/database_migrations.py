"""Reviewed Alembic execution for add-on-owned database schemas."""

from __future__ import annotations

import hashlib
import json
import logging
import tempfile
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import sqlalchemy as sa

from carlina.core.managed_target_recovery import ManagedTargetRecoveryContext

from ...extensions import db
from ...models import Addon
from ..persistence.sqlite import (
    configure_sqlite_engine,
    ensure_sqlite_ddl_transaction,
)
from .migration_contracts import MAX_REVISION_ID_LENGTH, AddonMigrationContract
from .schema_ownership import (
    addon_defines_database_models,
    addon_migration_contract,
    addon_migration_contract_issues,
    core_database_table_names,
)

logger = logging.getLogger(__name__)


class AddonDatabaseMigrationHost(Protocol):
    """Manager state required by add-on database migration orchestration."""

    app: Any

    def get_addon_directory(self, addon_id: str) -> Path: ...

    def _lifecycle_mutation_lock(self) -> threading.RLock: ...

    def _require_lifecycle_owner(self, operation: str) -> None: ...


if TYPE_CHECKING:

    class _AddonDatabaseMigrationMixinBase(AddonDatabaseMigrationHost):
        pass

else:

    class _AddonDatabaseMigrationMixinBase:
        pass


class AddonDatabaseMigrationMixin(_AddonDatabaseMigrationMixinBase):
    """Run checked add-on migrations and persist their actual revision."""

    @staticmethod
    def _alembic_option(value: object) -> str:
        """Escape ConfigParser interpolation while preserving Alembic's value."""

        return str(value).replace("%", "%%")

    def _get_alembic_config(
        self,
        addon_id: str,
        *,
        migration_root: Path | None = None,
    ) -> Any | None:
        from alembic.config import Config as AlembicConfig

        addon_dir = Path(migration_root or self.get_addon_directory(addon_id))
        contract = self._migration_contract_for(addon_id, addon_dir)
        if contract is None:
            return None
        runtime_dir = Path(__file__).with_name("alembic_runtime")
        # The package configuration is parsed by the static contract compiler,
        # but it is never used as Alembic runtime configuration. This prevents
        # package-controlled import paths, recursive discovery, hooks, and
        # logging settings from influencing host execution.
        config = AlembicConfig()
        config.set_main_option(
            "script_location",
            self._alembic_option(runtime_dir),
        )
        config.set_main_option(
            "version_locations",
            self._alembic_option(contract.versions_dir),
        )
        config.set_main_option("path_separator", "os")
        config.set_main_option("version_table", contract.version_table)
        config.set_main_option(
            "version_num_length",
            str(MAX_REVISION_ID_LENGTH),
        )
        config.set_main_option(
            "sqlalchemy.url",
            self._alembic_option(self.app.config["SQLALCHEMY_DATABASE_URI"]),
        )
        config.attributes["carlina.addon_migration_contract"] = contract
        return config

    @staticmethod
    def _migration_contract_for(
        addon_id: str,
        addon_dir: Path,
    ) -> AddonMigrationContract | None:
        if not (addon_dir / "migrations").exists():
            return None
        contract = addon_migration_contract(addon_dir, addon_id)
        if contract is not None:
            return contract
        issues = "; ".join(addon_migration_contract_issues(addon_dir, addon_id))
        raise RuntimeError(
            f"Add-on '{addon_id}' has an invalid migration contract: {issues}."
        )

    def _validate_addon_migration_contract(self, addon_id: str) -> None:
        addon_dir = self.get_addon_directory(addon_id)
        migrations_declared = (addon_dir / "migrations").exists()
        if addon_defines_database_models(addon_dir) and not migrations_declared:
            raise RuntimeError(
                f"Add-on '{addon_id}' defines database models but ships no "
                "migrations. Runtime migration generation is disabled; generate "
                "and review migrations in the add-on development workflow."
            )
        if migrations_declared:
            self._migration_contract_for(addon_id, addon_dir)

    @staticmethod
    def _current_addon_db_revision(config: Any) -> str | None:
        from alembic.migration import MigrationContext

        options = {}
        for option in ("version_table", "version_table_schema"):
            value = config.get_main_option(option)
            if value:
                options[option] = value
        with db.engine.connect() as connection:
            context = MigrationContext.configure(connection, opts=options)
            return context.get_current_revision()

    @staticmethod
    def _record_addon_db_revision(addon_id: str, revision: str | None) -> None:
        record = db.session.get(Addon, addon_id)
        if record is None or record.alembic_revision == revision:
            return
        record.alembic_revision = revision
        db.session.commit()

    @staticmethod
    def _configured_contract(config: Any) -> AddonMigrationContract:
        contract = config.attributes.get("carlina.addon_migration_contract")
        if not isinstance(contract, AddonMigrationContract):
            raise TypeError(
                "Alembic configuration has no validated add-on contract."
            )
        return contract

    @staticmethod
    def _schema_fingerprint(
        engine: sa.Engine,
        table_names: tuple[str, ...],
    ) -> dict[str, Any]:
        inspector = sa.inspect(engine)
        available = set(inspector.get_table_names())
        tables: dict[str, Any] = {}
        for table_name in sorted(table_names):
            if table_name not in available:
                tables[table_name] = None
                continue
            columns = [
                {
                    "name": str(column["name"]),
                    "type": str(column["type"]).casefold(),
                    "nullable": bool(column.get("nullable", True)),
                    "default": (
                        str(column["default"])
                        if column.get("default") is not None
                        else None
                    ),
                }
                for column in inspector.get_columns(table_name)
            ]
            primary_key = inspector.get_pk_constraint(table_name) or {}
            indexes = sorted(
                (
                    {
                        "name": str(index.get("name") or ""),
                        "columns": [
                            str(value) for value in index.get("column_names") or ()
                        ],
                        "unique": bool(index.get("unique", False)),
                    }
                    for index in inspector.get_indexes(table_name)
                ),
                key=lambda item: (item["name"], item["columns"]),
            )
            unique_constraints = sorted(
                (
                    {
                        "name": str(item.get("name") or ""),
                        "columns": [
                            str(value) for value in item.get("column_names") or ()
                        ],
                    }
                    for item in inspector.get_unique_constraints(table_name)
                ),
                key=lambda item: (item["name"], item["columns"]),
            )
            foreign_keys = sorted(
                (
                    {
                        "columns": [
                            str(value)
                            for value in item.get("constrained_columns") or ()
                        ],
                        "referred_table": str(item.get("referred_table") or ""),
                        "referred_columns": [
                            str(value)
                            for value in item.get("referred_columns") or ()
                        ],
                        "ondelete": str(
                            (item.get("options") or {}).get("ondelete") or ""
                        ).casefold(),
                    }
                    for item in inspector.get_foreign_keys(table_name)
                ),
                key=lambda item: (
                    item["columns"],
                    item["referred_table"],
                    item["referred_columns"],
                ),
            )
            checks = sorted(
                (
                    {
                        "name": str(item.get("name") or ""),
                        "sqltext": " ".join(str(item.get("sqltext") or "").split()),
                    }
                    for item in inspector.get_check_constraints(table_name)
                ),
                key=lambda item: (item["name"], item["sqltext"]),
            )
            tables[table_name] = {
                "columns": columns,
                "primary_key": [
                    str(value)
                    for value in primary_key.get("constrained_columns") or ()
                ],
                "indexes": indexes,
                "unique_constraints": unique_constraints,
                "foreign_keys": foreign_keys,
                "checks": checks,
            }
        return {"format": "carlina.sqlite-schema-v1", "tables": tables}

    @staticmethod
    def _fingerprint_digest(fingerprint: dict[str, Any]) -> str:
        encoded = json.dumps(
            fingerprint,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _expected_addon_schema_fingerprint(
        self,
        addon_id: str,
        contract: AddonMigrationContract,
        target: str,
    ) -> dict[str, Any]:
        from alembic import command as alembic_command

        paths = self.app.extensions["carlina_paths"]
        Path(paths.temp_dir).mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix=f"migration-adoption-{addon_id}-",
            dir=paths.temp_dir,
        ) as temporary:
            scratch_path = Path(temporary) / "expected.db"
            engine = sa.create_engine(f"sqlite:///{scratch_path}")
            configure_sqlite_engine(engine)
            try:
                core_metadata = sa.MetaData()
                for table_name in sorted(core_database_table_names()):
                    table = db.metadata.tables.get(table_name)
                    if table is not None:
                        table.to_metadata(core_metadata)
                core_metadata.create_all(engine)
                config = self._get_alembic_config(addon_id)
                if config is None:
                    raise RuntimeError(
                        f"Add-on '{addon_id}' has no migration environment."
                    )
                with engine.begin() as connection:
                    ensure_sqlite_ddl_transaction(connection)
                    config.attributes["connection"] = connection
                    try:
                        alembic_command.upgrade(config, target)
                    finally:
                        config.attributes.pop("connection", None)
                return self._schema_fingerprint(engine, contract.owned_tables)
            finally:
                engine.dispose()

    def adopt_addon_db(
        self,
        addon_id: str,
        revision: str = "head",
    ) -> dict[str, Any]:
        """Adopt an exact legacy schema after backup and structural rehearsal."""

        from ..backup_service import create_backup
        from ..encryption import InstallationKeyStore

        self._require_lifecycle_owner("adopt an existing add-on database schema")
        with self._lifecycle_mutation_lock():
            self._validate_addon_migration_contract(addon_id)
            addon_dir = self.get_addon_directory(addon_id)
            contract = self._migration_contract_for(addon_id, addon_dir)
            if contract is None:
                raise RuntimeError(
                    f"Add-on '{addon_id}' has no migration contract to adopt."
                )
            target_revision = contract.resolve_target(revision)
            if target_revision is None:
                raise RuntimeError("A legacy schema cannot be adopted at base.")
            physical_revision = self._current_addon_db_revision_from_contract(contract)
            if physical_revision is not None:
                raise RuntimeError(
                    f"Add-on '{addon_id}' already has physical migration revision "
                    f"{physical_revision!r}; adoption is only for a missing ledger."
                )
            record = db.session.get(Addon, addon_id)
            recorded_revision = record.alembic_revision if record is not None else None
            if recorded_revision not in {None, target_revision}:
                raise RuntimeError(
                    f"Add-on '{addon_id}' records revision {recorded_revision!r}, "
                    f"which conflicts with adoption target {target_revision!r}."
                )

            expected = self._expected_addon_schema_fingerprint(
                addon_id,
                contract,
                target_revision,
            )
            actual = self._schema_fingerprint(db.engine, contract.owned_tables)
            expected_digest = self._fingerprint_digest(expected)
            actual_digest = self._fingerprint_digest(actual)
            if actual != expected:
                raise RuntimeError(
                    f"Cannot adopt '{addon_id}': the physical schema does not "
                    "exactly match the rehearsed migration target "
                    f"(expected {expected_digest}, found {actual_digest})."
                )

            paths = self.app.extensions["carlina_paths"]
            backup = create_backup(
                paths,
                key_store=InstallationKeyStore(paths.master_keys_file),
            )
            self._run_addon_db_migration(
                addon_id,
                target_revision,
                direction="stamp",
                confirm_schema_match=True,
            )
            record = db.session.get(Addon, addon_id)
            lifecycle = getattr(self, "lifecycle", None)
            if record is not None and lifecycle is not None:
                lifecycle.record_event(
                    record,
                    event="legacy_schema_adopted",
                    metadata={
                        "revision": target_revision,
                        "schema_sha256": actual_digest,
                        "safety_backup": str(backup.path),
                        "backup_sha256": backup.sha256,
                    },
                )
            return {
                "addon_id": addon_id,
                "revision": target_revision,
                "schema_sha256": actual_digest,
                "safety_backup": str(backup.path),
                "backup_sha256": backup.sha256,
            }

    def _validate_migration_state(
        self,
        addon_id: str,
        contract: AddonMigrationContract,
        *,
        direction: str,
        target: str,
        confirm_schema_match: bool = False,
    ) -> str | None:
        current = self._current_addon_db_revision_from_contract(contract)
        record = db.session.get(Addon, addon_id)
        recorded = record.alembic_revision if record is not None else None
        if current is not None and current not in contract.revision_ids:
            raise RuntimeError(
                f"Add-on '{addon_id}' database is at unknown revision {current!r}."
            )
        if recorded is not None and recorded not in contract.revision_ids:
            raise RuntimeError(
                f"Add-on '{addon_id}' records unknown revision {recorded!r}."
            )
        resolved_target = contract.resolve_target(target)
        if direction == "stamp":
            if current is None:
                if not confirm_schema_match:
                    raise RuntimeError(
                        f"Add-on '{addon_id}' has no physical migration ledger. "
                        "Use the backup-first 'addons db adopt' command to "
                        "rehearse and fingerprint the existing schema."
                    )
                if recorded is not None and recorded != resolved_target:
                    raise RuntimeError(
                        f"Stamp target {target!r} conflicts with the manager's "
                        f"recorded revision {recorded!r}."
                    )
                return current
            if current != resolved_target:
                raise RuntimeError(
                    f"Refusing to stamp from physical revision {current!r} to "
                    f"{target!r}; stamping must not skip migration code."
                )
            return current
        if current is None and recorded is not None:
            raise RuntimeError(
                f"Add-on '{addon_id}' records revision {recorded!r}, but its "
                "physical Alembic version table is missing or empty."
            )
        if current != recorded:
            logger.warning(
                "Repairing stale recorded revision for add-on '%s': %r -> %r.",
                addon_id,
                recorded,
                current,
            )
        current_index = contract.revision_index(current)
        target_index = contract.revision_index(resolved_target)
        if direction == "upgrade" and target_index < current_index:
            raise RuntimeError(
                f"Upgrade target {target!r} precedes current revision {current!r}."
            )
        if direction == "downgrade" and target_index > current_index:
            raise RuntimeError(
                f"Downgrade target {target!r} follows current revision {current!r}."
            )
        return current

    @staticmethod
    def _current_addon_db_revision_from_contract(
        contract: AddonMigrationContract,
    ) -> str | None:
        from alembic.migration import MigrationContext

        with db.engine.connect() as connection:
            context = MigrationContext.configure(
                connection,
                opts={"version_table": contract.version_table},
            )
            return context.get_current_revision()

    def _run_addon_db_migration(
        self,
        addon_id: str,
        revision: str,
        *,
        direction: str,
        migration_root: Path | None = None,
        confirm_schema_match: bool = False,
    ) -> None:
        from alembic import command as alembic_command

        addon_dir = Path(migration_root or self.get_addon_directory(addon_id))
        config = self._get_alembic_config(
            addon_id,
            migration_root=addon_dir,
        )
        if config is None:
            logger.info(
                "No Alembic environment found for add-on '%s'; skipping %s.",
                addon_id,
                direction,
            )
            return
        contract = self._configured_contract(config)

        actions = {
            "upgrade": alembic_command.upgrade,
            "downgrade": alembic_command.downgrade,
            "stamp": alembic_command.stamp,
        }
        try:
            action = actions[direction]
        except KeyError as exc:
            raise ValueError(
                f"Unsupported add-on migration direction: {direction}"
            ) from exc
        with self.app.app_context():
            try:
                self._validate_migration_state(
                    addon_id,
                    contract,
                    direction=direction,
                    target=revision,
                    confirm_schema_match=confirm_schema_match,
                )
                with db.engine.begin() as connection:
                    # sqlite3 legacy mode does not begin for DDL, while modern
                    # PEP 249 mode already has a transaction. The helper safely
                    # handles both without issuing a nested BEGIN.
                    ensure_sqlite_ddl_transaction(connection)
                    config.attributes["connection"] = connection
                    try:
                        action(config, revision)
                    finally:
                        config.attributes.pop("connection", None)
                current_revision = self._current_addon_db_revision(config)
                self._record_addon_db_revision(addon_id, current_revision)
            except Exception:
                db.session.rollback()
                logger.exception(
                    "Add-on database %s failed for '%s' at revision '%s'.",
                    direction,
                    addon_id,
                    revision,
                )
                raise
        logger.info(
            "Add-on database %s completed for '%s' at revision '%s'.",
            direction,
            addon_id,
            current_revision,
        )

    def _create_migration_safety_backup(
        self,
        addon_id: str,
        *,
        direction: str,
        current_revision: str,
        target_revision: str | None,
    ) -> dict[str, str]:
        from ..backup_service import create_backup
        from ..encryption import InstallationKeyStore

        paths = self.app.extensions["carlina_paths"]
        backup = create_backup(
            paths,
            key_store=InstallationKeyStore(paths.master_keys_file),
        )
        metadata = {
            "safety_backup": str(backup.path),
            "backup_sha256": backup.sha256,
            "direction": direction,
            "from_revision": current_revision,
            "to_revision": target_revision or "base",
        }
        record = db.session.get(Addon, addon_id)
        lifecycle = getattr(self, "lifecycle", None)
        if record is not None and lifecycle is not None:
            lifecycle.record_event(
                record,
                event="migration_safety_backup_created",
                metadata=metadata,
            )
        return metadata

    def _prepare_schema_change(
        self,
        addon_id: str,
        contract: AddonMigrationContract,
        *,
        direction: str,
        target: str,
        safety_backup: bool,
        managed_recovery: ManagedTargetRecoveryContext | None = None,
    ) -> None:
        current = self._current_addon_db_revision_from_contract(contract)
        resolved_target = contract.resolve_target(target)
        if direction == "upgrade" and current is None:
            existing_tables = set(sa.inspect(db.engine).get_table_names())
            legacy_tables = sorted(set(contract.owned_tables) & existing_tables)
            if legacy_tables:
                raise RuntimeError(
                    f"Add-on '{addon_id}' has unversioned owned tables "
                    f"({', '.join(legacy_tables)}). Refusing a blind upgrade; "
                    "use 'addons db adopt' after an exact schema rehearsal."
                )
        if (
            safety_backup
            and current is not None
            and current != resolved_target
        ):
            if managed_recovery is None:
                self._create_migration_safety_backup(
                    addon_id,
                    direction=direction,
                    current_revision=current,
                    target_revision=resolved_target,
                )
            else:
                record = db.session.get(Addon, addon_id)
                lifecycle = getattr(self, "lifecycle", None)
                if record is None or lifecycle is None:
                    raise RuntimeError(
                        "Managed migration recovery delegation cannot be audited "
                        f"for add-on '{addon_id}'."
                    )
                lifecycle.record_event(
                    record,
                    event="migration_recovery_delegated",
                    metadata={
                        **managed_recovery.event_metadata(),
                        "direction": direction,
                        "from_revision": current,
                        "to_revision": resolved_target or "base",
                    },
                )

    def upgrade_addon_db(
        self,
        addon_id: str,
        revision: str = "head",
        *,
        managed_recovery: ManagedTargetRecoveryContext | None = None,
    ) -> None:
        """Upgrade one add-on schema from reviewed migration files."""

        self._require_lifecycle_owner("upgrade add-on database schemas")
        with self._lifecycle_mutation_lock():
            self._validate_addon_migration_contract(addon_id)
            contract = self._migration_contract_for(
                addon_id,
                self.get_addon_directory(addon_id),
            )
            if contract is not None:
                self._prepare_schema_change(
                    addon_id,
                    contract,
                    direction="upgrade",
                    target=revision,
                    safety_backup=True,
                    managed_recovery=managed_recovery,
                )
            self._run_addon_db_migration(
                addon_id,
                revision,
                direction="upgrade",
            )

    def downgrade_addon_db(
        self,
        addon_id: str,
        revision: str = "base",
        *,
        migration_root: Path | None = None,
        safety_backup: bool = True,
    ) -> None:
        """Downgrade one add-on schema and retain its actual resulting revision."""

        self._require_lifecycle_owner("downgrade add-on database schemas")
        with self._lifecycle_mutation_lock():
            addon_dir = Path(migration_root or self.get_addon_directory(addon_id))
            contract = self._migration_contract_for(addon_id, addon_dir)
            if contract is not None:
                self._prepare_schema_change(
                    addon_id,
                    contract,
                    direction="downgrade",
                    target=revision,
                    safety_backup=safety_backup,
                )
            self._run_addon_db_migration(
                addon_id,
                revision,
                direction="downgrade",
                migration_root=migration_root,
            )

    def stamp_addon_db(
        self,
        addon_id: str,
        revision: str = "head",
    ) -> None:
        """Reconcile manager state with an already-present physical ledger."""

        self._require_lifecycle_owner("stamp add-on database schemas")
        with self._lifecycle_mutation_lock():
            self._run_addon_db_migration(
                addon_id,
                revision,
                direction="stamp",
            )


__all__ = ["AddonDatabaseMigrationHost", "AddonDatabaseMigrationMixin"]
