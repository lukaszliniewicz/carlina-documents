"""Explicit, resumable reconciliation of release-owned built-in add-ons."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn, Protocol

from packaging.version import InvalidVersion, Version

from carlina.core.managed_target_recovery import ManagedTargetRecoveryContext
from iarf_assistant.core.addon_base import AddonContext
from iarf_assistant.extensions import db
from iarf_assistant.models import Addon

from .contracts import AddonSourceKind
from .discovery import DiscoveredAddon, dependency_order
from .schema_ownership import addon_has_migrations
from .startup_loading import (
    BuiltinAddonReconciliationError,
    builtin_migration_reconciliation_state,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _BuiltinUpgradePlan:
    addon_id: str
    record: Addon
    package: DiscoveredAddon
    installed_version: str
    package_version: str
    installed_revision: str | None
    physical_revision: str | None
    package_revision: str | None
    migration_changed: bool
    migration_applied: bool
    was_loaded: bool


class AddonBuiltinReconciliationHost(Protocol):
    """Manager services required by the managed update reconciliation pass."""

    app: Any
    lifecycle: Any
    loaded_addons: dict[str, Any]
    _addon_packages: dict[str, DiscoveredAddon]
    _builtin_reconciliation_required: dict[str, dict[str, str]]

    def _lifecycle_mutation_lock(self) -> threading.RLock: ...

    def _require_lifecycle_owner(self, operation: str) -> None: ...

    def _sync_addons_from_filesystem(self, sync_database: bool = True) -> None: ...

    def _contract_python_environment_is_ready(
        self,
        addon_id: str,
        environment: Any,
    ) -> bool: ...

    def install_dependencies(
        self,
        addon_id: str,
        *,
        prepare_docker: bool = True,
    ) -> Any: ...

    def prepare_addon_import_indexes(self, addon_id: str) -> int: ...

    def get_addon_directory(self, addon_id: str) -> Path: ...

    def _validate_addon_migration_contract(self, addon_id: str) -> None: ...

    def _current_addon_db_revision_from_contract(
        self,
        contract: Any,
    ) -> str | None: ...

    def upgrade_addon_db(
        self,
        addon_id: str,
        revision: str = "head",
        *,
        managed_recovery: ManagedTargetRecoveryContext | None = None,
    ) -> None: ...

    def _load_addon(
        self,
        addon_id: str,
        context: AddonContext | None,
        _loading_stack: tuple[str, ...] = (),
    ) -> None: ...

    def _unload_addon_runtime(self, addon_id: str) -> None: ...


if TYPE_CHECKING:

    class _AddonBuiltinReconciliationMixinBase(AddonBuiltinReconciliationHost):
        pass

else:

    class _AddonBuiltinReconciliationMixinBase:
        pass


class AddonBuiltinReconciliationMixin(_AddonBuiltinReconciliationMixinBase):
    """Apply built-in version transitions only during explicit maintenance."""

    @staticmethod
    def _validate_builtin_version_transition(
        addon_id: str,
        installed_version: str,
        package_version: str,
    ) -> None:
        """Reject implicit built-in downgrades and malformed versions."""

        try:
            installed = Version(str(installed_version))
            package = Version(str(package_version))
        except InvalidVersion as exc:
            raise BuiltinAddonReconciliationError(
                f"Built-in add-on '{addon_id}' has an invalid version transition "
                f"({installed_version!r} -> {package_version!r})."
            ) from exc
        if package < installed:
            raise BuiltinAddonReconciliationError(
                f"Built-in add-on '{addon_id}' cannot be downgraded implicitly "
                f"from {installed_version} to {package_version}. Restore the paired "
                "application backup when rolling back Carlina."
            )

    def _missing_contract_python_environments(
        self,
        addon_id: str,
        package: DiscoveredAddon | None,
    ) -> tuple[Any, ...]:
        declared = tuple(
            getattr(getattr(package, "contract", None), "python_environments", ()) or ()
        )
        return tuple(
            environment
            for environment in declared
            if not self._contract_python_environment_is_ready(
                addon_id,
                environment,
            )
        )

    @staticmethod
    def _new_builtin_reconciliation_report() -> dict[str, Any]:
        return {
            "checked": 0,
            "reconciled": [],
            "deferred_inactive": [],
            "prepared_environments": [],
            "prepared_import_indexes": [],
        }

    def _prepare_builtin_runtime_contract(
        self,
        addon_id: str,
        package: DiscoveredAddon,
        report: dict[str, Any],
    ) -> None:
        missing_environments = self._missing_contract_python_environments(
            addon_id,
            package,
        )
        if missing_environments:
            self.app.logger.info(
                "Built-in add-on '%s' requires %s dependency environment(s) "
                "before reconciliation; preparing locked dependencies.",
                addon_id,
                len(missing_environments),
            )
            self.install_dependencies(addon_id, prepare_docker=False)
            report["prepared_environments"].append(addon_id)
        if self.prepare_addon_import_indexes(addon_id):
            report["prepared_import_indexes"].append(addon_id)

    def _build_builtin_upgrade_plan(
        self,
        addon_id: str,
        record: Addon,
        package: DiscoveredAddon,
    ) -> _BuiltinUpgradePlan:
        (
            installed_revision,
            physical_revision,
            package_revision,
            migration_changed,
        ) = builtin_migration_reconciliation_state(self, record, package)
        return _BuiltinUpgradePlan(
            addon_id=addon_id,
            record=record,
            package=package,
            installed_version=str(record.version),
            package_version=str(package.contract.version),
            installed_revision=installed_revision,
            physical_revision=physical_revision,
            package_revision=package_revision,
            migration_changed=migration_changed,
            migration_applied=addon_has_migrations(
                self.get_addon_directory(addon_id),
                addon_id,
            ),
            was_loaded=addon_id in self.loaded_addons,
        )

    def _apply_builtin_upgrade(
        self,
        plan: _BuiltinUpgradePlan,
        *,
        managed_recovery: ManagedTargetRecoveryContext | None = None,
    ) -> dict[str, Any]:
        self._validate_addon_migration_contract(plan.addon_id)
        if plan.migration_applied:
            if managed_recovery is None:
                self.upgrade_addon_db(plan.addon_id)
            else:
                self.upgrade_addon_db(
                    plan.addon_id,
                    managed_recovery=managed_recovery,
                )
        self._load_addon(
            plan.addon_id,
            AddonContext(self.app, self),
        )
        instance = self.loaded_addons.get(plan.addon_id)
        if instance is None:
            raise RuntimeError(
                f"Built-in add-on '{plan.addon_id}' did not expose a runtime "
                "instance during reconciliation."
            )
        version_changed = plan.installed_version != plan.package_version
        if version_changed and hasattr(instance, "on_upgrade"):
            instance.on_upgrade(plan.installed_version)

        plan.record.version = plan.package_version
        self.lifecycle.record_event(
            plan.record,
            event="builtin_package_reconciled",
            metadata={
                "from_version": plan.installed_version,
                "to_version": plan.package_version,
                "migration_applied": plan.migration_applied,
                "migration_changed": plan.migration_changed,
                "from_revision": plan.installed_revision,
                "physical_revision": plan.physical_revision,
                "to_revision": plan.package_revision,
                "manifest_sha256": plan.package.contract.manifest_sha256,
                "contract_sha256": plan.package.contract.contract_sha256,
            },
        )
        self._builtin_reconciliation_required.pop(plan.addon_id, None)
        return {
            "addon_id": plan.addon_id,
            "from_version": plan.installed_version,
            "to_version": plan.package_version,
            "migration_applied": plan.migration_applied,
        }

    def _raise_builtin_reconciliation_failure(
        self,
        plan: _BuiltinUpgradePlan,
        error: Exception,
    ) -> NoReturn:
        db.session.rollback()
        if not plan.was_loaded:
            try:
                self._unload_addon_runtime(plan.addon_id)
            except Exception:
                logger.exception(
                    "Failed to unload built-in add-on '%s' after reconciliation error.",
                    plan.addon_id,
                )
        current_record = db.session.get(Addon, plan.addon_id)
        if current_record is not None:
            try:
                self.lifecycle.record_event(
                    current_record,
                    event="builtin_package_reconciliation_failed",
                    success=False,
                    detail=str(error),
                    metadata={
                        "from_version": plan.installed_version,
                        "to_version": plan.package_version,
                    },
                )
            except Exception:
                db.session.rollback()
                logger.exception(
                    "Failed to journal built-in reconciliation failure for '%s'.",
                    plan.addon_id,
                )
        raise BuiltinAddonReconciliationError(
            f"Could not reconcile built-in add-on '{plan.addon_id}' from "
            f"{plan.installed_version} to {plan.package_version}: {error}"
        ) from error

    def _reconcile_builtin_record(
        self,
        addon_id: str,
        record: Addon,
        package: DiscoveredAddon,
        report: dict[str, Any],
        *,
        managed_recovery: ManagedTargetRecoveryContext | None = None,
    ) -> None:
        plan = self._build_builtin_upgrade_plan(addon_id, record, package)
        version_changed = plan.installed_version != plan.package_version
        reconciliation_required = version_changed or plan.migration_changed
        try:
            if version_changed:
                self._validate_builtin_version_transition(
                    addon_id,
                    plan.installed_version,
                    plan.package_version,
                )
            if not reconciliation_required:
                self._builtin_reconciliation_required.pop(addon_id, None)
            else:
                reconciliation = {
                    "installed_version": plan.installed_version,
                    "package_version": plan.package_version,
                }
                if plan.migration_changed:
                    reconciliation.update(
                        {
                            "installed_revision": plan.installed_revision or "base",
                            "physical_revision": plan.physical_revision or "base",
                            "package_revision": plan.package_revision or "base",
                        }
                    )
                self._builtin_reconciliation_required[addon_id] = reconciliation

            if reconciliation_required and not record.is_active:
                deferred = {
                    "addon_id": addon_id,
                    **self._builtin_reconciliation_required[addon_id],
                }
                report["deferred_inactive"].append(deferred)
                return

            if not record.is_active:
                return
            self._prepare_builtin_runtime_contract(addon_id, package, report)
            if not reconciliation_required:
                return
            report["reconciled"].append(
                self._apply_builtin_upgrade(
                    plan,
                    managed_recovery=managed_recovery,
                )
            )
        except Exception as exc:  # noqa: BLE001 - journal the package boundary.
            self._raise_builtin_reconciliation_failure(plan, exc)

    def reconcile_builtin_addons(
        self,
        *,
        managed_recovery: ManagedTargetRecoveryContext | None = None,
    ) -> dict[str, Any]:
        """Serialize explicit release-owned package reconciliation."""

        self._require_lifecycle_owner("reconcile built-in add-ons")
        with self._lifecycle_mutation_lock():
            return self._reconcile_builtin_addons_serialized(
                managed_recovery=managed_recovery
            )

    def _reconcile_builtin_addons_serialized(
        self,
        *,
        managed_recovery: ManagedTargetRecoveryContext | None = None,
    ) -> dict[str, Any]:
        """Reconcile active built-ins during an explicit app update or repair."""

        self._sync_addons_from_filesystem(sync_database=True)
        report = self._new_builtin_reconciliation_report()
        for addon_id in dependency_order(self._addon_packages):
            package = self._addon_packages[addon_id]
            if package.source_kind != AddonSourceKind.BUILT_IN:
                continue
            record = db.session.get(Addon, addon_id)
            if record is None:
                continue
            report["checked"] += 1
            self._reconcile_builtin_record(
                addon_id,
                record,
                package,
                report,
                managed_recovery=managed_recovery,
            )
        return report


__all__ = [
    "AddonBuiltinReconciliationHost",
    "AddonBuiltinReconciliationMixin",
]
