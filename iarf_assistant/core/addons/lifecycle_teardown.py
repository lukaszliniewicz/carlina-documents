"""Add-on deactivation, code removal, and explicit destructive-data boundaries."""

from __future__ import annotations

import logging
import shutil
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import sqlalchemy as sa

from ...extensions import db
from ...models import Addon, AddonLifecycleEvent
from ..addon_base import AddonContext
from ..persistence.sqlite import ensure_sqlite_ddl_transaction
from .contracts import AddonSourceKind
from .imports import unload_installed_addon_module
from .lifecycle import (
    CODE_REMOVED as LIFECYCLE_CODE_REMOVED,
)
from .lifecycle import (
    DATA_PRESERVED as LIFECYCLE_DATA_PRESERVED,
)
from .lifecycle import (
    DATA_PURGED as LIFECYCLE_DATA_PURGED,
)
from .lifecycle import (
    DISABLED as LIFECYCLE_DISABLED,
)
from .lifecycle import (
    DISABLING as LIFECYCLE_DISABLING,
)
from .lifecycle import (
    PURGE_PENDING as LIFECYCLE_PURGE_PENDING,
)
from .lifecycle import (
    REMOVAL_PENDING as LIFECYCLE_REMOVAL_PENDING,
)
from .migration_capsules import (
    MigrationCapsule,
    capture_migration_capsule,
    load_migration_capsule,
)
from .schema_ownership import addon_has_migrations, addon_migration_contract

logger = logging.getLogger(__name__)


class AddonLifecycleTeardownHost(Protocol):
    """Manager state required by lifecycle teardown orchestration."""

    app: Any
    manifests: dict[str, dict[str, Any]]
    loaded_addons: dict[str, Any]
    plugins: dict[str, Any]
    automations: dict[str, Any]
    helpers: dict[str, Any]
    _dispatch_enabled_addons: set[str]
    _vendor_finder: Any
    lifecycle: Any

    def _lifecycle_mutation_lock(self) -> threading.RLock: ...

    def _require_lifecycle_owner(self, operation: str) -> None: ...

    def _set_addon_dispatch_enabled(self, addon_id: str, enabled: bool) -> bool: ...

    def _runtime_load_lock(self) -> threading.RLock: ...

    def _addon_source_kind(self, addon_id: str) -> AddonSourceKind: ...

    def stop_addon_workers(self, addon_id: str) -> int: ...

    def unregister_scheduled_jobs(self, addon_id: str) -> None: ...

    def rebuild_registries(self) -> None: ...

    def _load_addon(self, addon_id: str, context: AddonContext) -> None: ...

    def _stop_and_remove_container(
        self,
        addon_id: str,
        remove_image: bool = False,
        manifest: dict[str, Any] | None = None,
    ) -> None: ...

    def _get_addon_manifest(self, addon_id: str) -> dict[str, Any] | None: ...

    def _clear_worker_spawn_preferences(self, addon_id: str) -> None: ...

    def _sync_addons_from_filesystem(self, sync_database: bool = True) -> None: ...

    def get_addon_directory(self, addon_id: str) -> Path: ...

    def downgrade_addon_db(
        self,
        addon_id: str,
        revision: str = "base",
        *,
        migration_root: Path | None = None,
        safety_backup: bool = True,
    ) -> None: ...

    def _validate_migration_state(
        self,
        addon_id: str,
        contract: Any,
        *,
        direction: str,
        target: str,
    ) -> str | None: ...

    def _current_addon_db_revision_from_contract(
        self,
        contract: Any,
    ) -> str | None: ...


if TYPE_CHECKING:

    class _AddonLifecycleTeardownMixinBase(AddonLifecycleTeardownHost):
        pass

else:

    class _AddonLifecycleTeardownMixinBase:
        pass


class AddonLifecycleTeardownMixin(_AddonLifecycleTeardownMixinBase):
    """Stop runtimes and preserve data unless an explicit purge is requested."""

    def _unload_addon_runtime(self, addon_id: str) -> None:
        with self._lifecycle_mutation_lock(), self._runtime_load_lock():
            self._unload_addon_runtime_locked(addon_id)

    def _unload_addon_runtime_locked(self, addon_id: str) -> None:
        """Remove one runtime while the mutation and runtime locks are held."""

        self._dispatch_enabled_addons.discard(addon_id)
        self.loaded_addons.pop(addon_id, None)
        self.plugins.pop(addon_id, None)
        self.automations.pop(addon_id, None)
        self.helpers.pop(addon_id, None)
        self._vendor_finder.unregister(addon_id)
        if self._addon_source_kind(addon_id) == AddonSourceKind.INSTALLED:
            unload_installed_addon_module(addon_id)

    def deactivate_addon(self, addon_id: str) -> None:
        """Serialize complete deactivation in the lifecycle-owner process."""

        self._require_lifecycle_owner("deactivate add-ons")
        with self._lifecycle_mutation_lock():
            self._deactivate_addon_serialized(addon_id)

    def _deactivate_addon_serialized(self, addon_id: str) -> None:
        """Stop new dispatch before draining add-on runtime resources."""

        addon_record = db.session.get(Addon, addon_id)
        if not addon_record:
            raise FileNotFoundError(f"Addon '{addon_id}' not found in database.")
        if not addon_record.is_active:
            logger.warning("Addon '%s' is already inactive.", addon_id)
            self._set_addon_dispatch_enabled(addon_id, False)
            self.stop_addon_workers(addon_id)
            self.unregister_scheduled_jobs(addon_id)
            self._unload_addon_runtime(addon_id)
            self.rebuild_registries()
            return

        instance = self.loaded_addons.get(addon_id)
        if not instance:
            self._load_addon(addon_id, AddonContext(self.app, self))
            instance = self.loaded_addons.get(addon_id)

        try:
            self._set_addon_dispatch_enabled(addon_id, False)
            self.lifecycle.transition(
                addon_record,
                LIFECYCLE_DISABLING,
                event="deactivation_started",
            )
            # Prevent new tool/UI dispatch while runtime cleanup drains.
            self.rebuild_registries()

            if instance and hasattr(instance, "on_deactivate"):
                logger.info("Running on_deactivate hook for addon '%s'...", addon_id)
                instance.on_deactivate()

            self.unregister_scheduled_jobs(addon_id)
            self.stop_addon_workers(addon_id)
            self._stop_and_remove_container(addon_id)
            self._unload_addon_runtime(addon_id)
            self.lifecycle.transition(
                addon_record,
                LIFECYCLE_DISABLED,
                event="deactivation_completed",
            )
            logger.info("Addon '%s' deactivated successfully.", addon_id)
        except Exception as exc:
            db.session.rollback()
            self.lifecycle.fail(
                addon_record,
                event="deactivation_failed",
                error=exc,
            )
            logger.exception(
                "Error during deactivation of addon '%s'.",
                addon_id,
            )
            raise
        finally:
            self.rebuild_registries()

    def uninstall_addon(self, addon_id: str) -> dict[str, Any]:
        """Compatibility alias for non-destructive installed-code removal."""

        return self.remove_addon_code(addon_id)

    def remove_addon_code(self, addon_id: str) -> dict[str, Any]:
        """Serialize non-destructive installed-code removal."""

        self._require_lifecycle_owner("remove add-on code")
        with self._lifecycle_mutation_lock():
            return self._remove_addon_code_serialized(addon_id)

    def _remove_addon_code_serialized(self, addon_id: str) -> dict[str, Any]:
        """Remove third-party code while preserving all owned data and schema."""

        from .installer import AddonPackageInstaller

        addon_record = db.session.get(Addon, addon_id)
        if not addon_record:
            raise FileNotFoundError(f"Add-on '{addon_id}' is not registered.")
        if addon_record.source_kind != AddonSourceKind.INSTALLED.value:
            raise ValueError("Built-in add-on code is managed by the Carlina release.")
        if addon_record.is_active:
            self.deactivate_addon(addon_id)

        manifest = self._get_addon_manifest(addon_id)
        paths = self.app.extensions["carlina_paths"]
        addon_dir = self.get_addon_directory(addon_id)
        migration_contract = addon_migration_contract(addon_dir, addon_id)
        physical_revision = None
        if migration_contract is not None:
            physical_revision = self._validate_migration_state(
                addon_id,
                migration_contract,
                # Reuse the non-destructive state/direction validator without
                # invoking Alembic. "upgrade" also accepts the uninitialized
                # base/base case, whereas stamp deliberately requires a human
                # schema confirmation when no ledger exists.
                direction="upgrade",
                target=addon_record.alembic_revision or "base",
            )
        capsule = capture_migration_capsule(
            paths,
            addon_id,
            addon_dir,
            alembic_revision=addon_record.alembic_revision,
            manifest_sha256=addon_record.manifest_sha256,
            contract_sha256=addon_record.contract_sha256,
            lifecycle_generation=addon_record.lifecycle_generation,
            package_version=addon_record.version,
            physical_revision=physical_revision,
        )
        self.lifecycle.transition(
            addon_record,
            LIFECYCLE_REMOVAL_PENDING,
            event="code_removal_started",
            metadata=self._capsule_metadata(capsule),
            force=addon_record.lifecycle_state != LIFECYCLE_DISABLED,
        )
        try:
            self.stop_addon_workers(addon_id)
            self._clear_worker_spawn_preferences(addon_id)
            self.unregister_scheduled_jobs(addon_id)
            self._stop_and_remove_container(
                addon_id,
                remove_image=True,
                manifest=manifest,
            )
            removed_path = AddonPackageInstaller(paths).remove_code(addon_id)
            self._unload_addon_runtime(addon_id)
            self.lifecycle.transition(
                addon_record,
                LIFECYCLE_CODE_REMOVED,
                event="code_removed",
                metadata={
                    "removed_path": str(removed_path),
                    **self._capsule_metadata(capsule),
                },
            )
            self.lifecycle.transition(
                addon_record,
                LIFECYCLE_DATA_PRESERVED,
                event="data_preserved",
            )
            self._sync_addons_from_filesystem()
            self.rebuild_registries()
            return {
                "addon_id": addon_id,
                "removed_path": str(removed_path),
                "lifecycle_state": addon_record.lifecycle_state,
                "data_preserved": True,
            }
        except Exception as exc:
            db.session.rollback()
            self.lifecycle.fail(
                addon_record,
                event="code_removal_failed",
                error=exc,
            )
            raise

    @staticmethod
    def _capsule_metadata(capsule: MigrationCapsule | None) -> dict[str, Any]:
        if capsule is None:
            return {"migration_capsule": None}
        return {
            "migration_capsule": {
                "path": str(capsule.path),
                "sha256": capsule.digest,
                "file_count": capsule.file_count,
                "total_bytes": capsule.total_bytes,
                "lifecycle_generation": capsule.lifecycle_generation,
                "physical_revision": capsule.physical_revision,
                "graph_sha256": capsule.graph_sha256,
            }
        }

    @staticmethod
    def _recorded_capsule_anchor(
        addon_id: str,
    ) -> tuple[str, int | None] | None:
        event = (
            AddonLifecycleEvent.query.filter(
                AddonLifecycleEvent.addon_id == addon_id,
                AddonLifecycleEvent.event.in_(("code_removed", "code_removal_started")),
                AddonLifecycleEvent.success.is_(True),
            )
            .order_by(AddonLifecycleEvent.id.desc())
            .first()
        )
        if event is None:
            return None
        metadata = event.metadata_json
        capsule = (
            metadata.get("migration_capsule") if isinstance(metadata, dict) else None
        )
        if not isinstance(capsule, dict):
            return None
        digest = str(capsule.get("sha256") or "").strip()
        if not digest:
            return None
        raw_generation = capsule.get("lifecycle_generation")
        if raw_generation is None:
            return digest, None
        if isinstance(raw_generation, bool) or not isinstance(raw_generation, int):
            # This is durable-state corruption, not a caller type error.
            raise RuntimeError(  # noqa: TRY004
                f"Cannot purge '{addon_id}': its migration capsule journal "
                "generation is malformed."
            )
        transition_offset = 2 if event.event == "code_removed" else 1
        if event.generation != raw_generation + transition_offset:
            raise RuntimeError(
                f"Cannot purge '{addon_id}': its migration capsule is not "
                "bound to the recorded removal transition."
            )
        return digest, raw_generation

    @staticmethod
    def _quoted_sqlite_identifier(identifier: str) -> str:
        return '"' + str(identifier).replace('"', '""') + '"'

    def _purge_addon_vector_tables(self, addon_id: str) -> list[str]:
        from sqlalchemy import text

        from .storage import (
            addon_storage_key,
            escape_sql_like,
            legacy_addon_storage_key,
        )

        storage_keys = [addon_storage_key(addon_id)]
        legacy_key = legacy_addon_storage_key(addon_id)
        legacy_collisions = [
            record.id
            for record in Addon.query.with_entities(Addon.id).all()
            if record.id != addon_id
            and legacy_addon_storage_key(record.id).casefold() == legacy_key.casefold()
        ]
        if legacy_collisions:
            logger.warning(
                "Legacy vector tables for add-on '%s' were preserved because their old "
                "key collides with: %s",
                addon_id,
                ", ".join(sorted(legacy_collisions)),
            )
        else:
            storage_keys.append(legacy_key)
        patterns = tuple(
            pattern
            for key in dict.fromkeys(storage_keys)
            for pattern in (
                f"vec_addon_{escape_sql_like(key)}\\_%",
                f"fts_vec_addon_{escape_sql_like(key)}\\_%",
            )
        )
        removed: list[str] = []
        with db.engine.connect() as connection, connection.begin():
            ensure_sqlite_ddl_transaction(connection)
            tables = connection.execute(
                text(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND ("
                    + " OR ".join(
                        f"name LIKE :pattern_{index} ESCAPE '\\'"
                        for index in range(len(patterns))
                    )
                    + ")"
                ),
                {f"pattern_{index}": pattern for index, pattern in enumerate(patterns)},
            ).fetchall()
            for (table_name,) in tables:
                connection.exec_driver_sql(
                    f"DROP TABLE IF EXISTS {self._quoted_sqlite_identifier(table_name)}"
                )
                removed.append(str(table_name))
        return removed

    def purge_addon_data(self, addon_id: str) -> dict[str, Any]:
        """Serialize destructive data purge in the lifecycle-owner process."""

        self._require_lifecycle_owner("purge add-on data")
        with self._lifecycle_mutation_lock():
            return self._purge_addon_data_serialized(addon_id)

    def _verified_purge_migration_root(
        self,
        addon_id: str,
        addon_dir: Path,
        paths: Any,
        addon_record: Addon,
    ) -> Path | None:
        if (addon_dir / "migrations").exists() and not addon_has_migrations(
            addon_dir,
            addon_id,
        ):
            raise RuntimeError(
                f"Cannot purge '{addon_id}': its declared Alembic migration "
                "contract is invalid. The safety backup was retained."
            )
        migration_root = (
            addon_dir if addon_has_migrations(addon_dir, addon_id) else None
        )
        # A selected package graph is authoritative after update/reinstall. A
        # capsule is a tombstone for removed code, never an override for live
        # package code.
        if migration_root is not None:
            return self._verified_live_purge_migration_root(
                addon_id,
                migration_root,
                addon_record,
            )

        migration_root = self._verified_capsule_purge_migration_root(
            addon_id,
            paths,
            addon_record,
        )
        recorded_contract = (
            addon_record.resolved_contract_json
            if isinstance(addon_record.resolved_contract_json, dict)
            else {}
        )
        recorded_migration = recorded_contract.get("database_migration")
        if (
            addon_record.alembic_revision or isinstance(recorded_migration, dict)
        ) and migration_root is None:
            raise RuntimeError(
                f"Cannot purge '{addon_id}': database revision "
                f"{addon_record.alembic_revision!r} is recorded, but no verified migration "
                "environment is available. The safety backup was retained."
            )
        return migration_root

    def _verified_live_purge_migration_root(
        self,
        addon_id: str,
        migration_root: Path,
        addon_record: Addon,
    ) -> Path:
        live_contract = addon_migration_contract(migration_root, addon_id)
        recorded_contract = (
            addon_record.resolved_contract_json
            if isinstance(addon_record.resolved_contract_json, dict)
            else {}
        )
        recorded_migration = recorded_contract.get("database_migration")
        expected_descriptor = (
            {
                "format": "alembic_python_v1",
                "version_table": live_contract.version_table,
                "purge_tables": list(live_contract.purge_tables),
                "owned_tables": list(live_contract.owned_tables),
                "base_revision": live_contract.base_revision,
                "head_revision": live_contract.head_revision,
                "revision_ids": list(live_contract.revision_ids),
                "revision_count": len(live_contract.revisions),
                "graph_sha256": live_contract.graph_sha256,
            }
            if live_contract is not None
            else None
        )
        if not isinstance(recorded_migration, dict) or any(
            recorded_migration.get(key) != value
            for key, value in (expected_descriptor or {}).items()
        ):
            raise RuntimeError(
                f"Cannot purge '{addon_id}': its live migration graph does "
                "not match the recorded package contract. The safety backup "
                "was retained."
            )
        return migration_root

    def _verified_capsule_purge_migration_root(
        self,
        addon_id: str,
        paths: Any,
        addon_record: Addon,
    ) -> Path | None:
        anchor = self._recorded_capsule_anchor(addon_id)
        recorded_digest = anchor[0] if anchor is not None else None
        capsule = load_migration_capsule(
            paths,
            addon_id,
            expected_digest=recorded_digest,
        )
        if capsule is not None:
            if anchor is None:
                raise RuntimeError(
                    f"Cannot purge '{addon_id}': its migration capsule is not "
                    "anchored in the lifecycle journal. The safety backup was "
                    "retained."
                )
            anchor_generation = anchor[1]
            if capsule.format_version >= 2:
                if anchor_generation is None or (
                    capsule.lifecycle_generation != anchor_generation
                ):
                    raise RuntimeError(
                        f"Cannot purge '{addon_id}': its migration capsule "
                        "generation does not match the lifecycle journal."
                    )
                if (
                    capsule.package_version != addon_record.version
                    or capsule.manifest_sha256 != addon_record.manifest_sha256
                    or capsule.contract_sha256 != addon_record.contract_sha256
                ):
                    raise RuntimeError(
                        f"Cannot purge '{addon_id}': its migration capsule "
                        "belongs to a different package generation."
                    )
            capsule_contract = addon_migration_contract(capsule.path, addon_id)
            if capsule_contract is None:
                raise RuntimeError(
                    f"Cannot purge '{addon_id}': its migration capsule contract "
                    "is invalid. The safety backup was retained."
                )
            recorded_contract = (
                addon_record.resolved_contract_json
                if isinstance(addon_record.resolved_contract_json, dict)
                else {}
            )
            recorded_migration = recorded_contract.get("database_migration")
            if capsule.format_version >= 2 and (
                not isinstance(recorded_migration, dict)
                or recorded_migration.get("graph_sha256")
                != capsule.graph_sha256
            ):
                raise RuntimeError(
                    f"Cannot purge '{addon_id}': its migration capsule graph "
                    "does not match the recorded package contract."
                )
            physical_revision = self._current_addon_db_revision_from_contract(
                capsule_contract
            )
            if capsule.format_version >= 2 and (
                physical_revision != capsule.physical_revision
            ):
                raise RuntimeError(
                    f"Cannot purge '{addon_id}': its physical migration revision "
                    "changed after code removal. The safety backup was retained."
                )
            return capsule.path
        return None

    def _purge_declared_schema_tables(
        self,
        addon_id: str,
        migration_root: Path,
    ) -> list[str]:
        contract = addon_migration_contract(migration_root, addon_id)
        if contract is None:
            return []
        removed: list[str] = []
        with db.engine.begin() as connection:
            ensure_sqlite_ddl_transaction(connection)
            existing = set(sa.inspect(connection).get_table_names())
            for table_name in (*contract.purge_tables, contract.version_table):
                if table_name not in existing:
                    continue
                connection.exec_driver_sql(
                    "DROP TABLE " + self._quoted_sqlite_identifier(table_name)
                )
                removed.append(table_name)
        return removed

    def _purge_addon_data_serialized(self, addon_id: str) -> dict[str, Any]:
        """Back up, then explicitly purge add-on files and declared schema."""

        from ..backup_service import create_backup
        from ..encryption import InstallationKeyStore

        addon_record = db.session.get(Addon, addon_id)
        if not addon_record:
            raise FileNotFoundError(f"Add-on '{addon_id}' is not registered.")
        if addon_record.is_active:
            raise RuntimeError("Disable the add-on before purging its data.")
        paths = self.app.extensions["carlina_paths"]
        backup = create_backup(
            paths,
            key_store=InstallationKeyStore(paths.master_keys_file),
        )
        self.lifecycle.transition(
            addon_record,
            LIFECYCLE_PURGE_PENDING,
            event="data_purge_started",
            metadata={
                "safety_backup": str(backup.path),
                "backup_sha256": backup.sha256,
            },
            force=True,
        )
        removed_paths: list[str] = []
        try:
            addon_dir = self.get_addon_directory(addon_id)
            migration_root = self._verified_purge_migration_root(
                addon_id,
                addon_dir,
                paths,
                addon_record,
            )
            instance = self.loaded_addons.get(addon_id)
            if instance is None and addon_id in self.manifests:
                self._load_addon(addon_id, AddonContext(self.app, self))
                instance = self.loaded_addons.get(addon_id)
            if instance is not None and hasattr(instance, "on_purge_data"):
                instance.on_purge_data()
                migration_root = self._verified_purge_migration_root(
                    addon_id,
                    addon_dir,
                    paths,
                    addon_record,
                )
            if migration_root is not None:
                self.downgrade_addon_db(
                    addon_id,
                    migration_root=migration_root,
                    safety_backup=False,
                )
            removed_tables = (
                self._purge_declared_schema_tables(addon_id, migration_root)
                if migration_root is not None
                else []
            )
            removed_tables.extend(self._purge_addon_vector_tables(addon_id))
            roots = (
                paths.addon_data_dir,
                paths.addon_environments_dir,
                paths.addon_logs_dir,
                paths.cache_dir / "addons",
                paths.artifacts_dir / "addons",
                paths.uploads_dir / "addons",
                paths.state_dir / "addons",
                paths.temp_dir / "addons",
                paths.locks_dir / "addons",
            )
            for root in roots:
                resolved_root = Path(root).resolve()
                candidate = (resolved_root / addon_id).resolve()
                candidate.relative_to(resolved_root)
                if candidate.is_dir():
                    shutil.rmtree(candidate)
                    removed_paths.append(str(candidate))
            legacy_data = (paths.addons_dir / addon_id / "data").resolve()
            if (
                legacy_data.is_relative_to(paths.addons_dir.resolve())
                and legacy_data.is_dir()
            ):
                shutil.rmtree(legacy_data)
                removed_paths.append(str(legacy_data))
            self.lifecycle.transition(
                addon_record,
                LIFECYCLE_DATA_PURGED,
                event="data_purge_completed",
                metadata={
                    "removed_paths": removed_paths,
                    "removed_tables": removed_tables,
                    "safety_backup": str(backup.path),
                },
            )
            self._unload_addon_runtime(addon_id)
            self.rebuild_registries()
            return {
                "addon_id": addon_id,
                "lifecycle_state": addon_record.lifecycle_state,
                "removed_paths": removed_paths,
                "removed_tables": removed_tables,
                "safety_backup": str(backup.path),
                "backup_sha256": backup.sha256,
            }
        except Exception as exc:
            db.session.rollback()
            self.lifecycle.fail(
                addon_record,
                event="data_purge_failed",
                error=exc,
                metadata={"safety_backup": str(backup.path)},
            )
            raise


__all__ = ["AddonLifecycleTeardownHost", "AddonLifecycleTeardownMixin"]
