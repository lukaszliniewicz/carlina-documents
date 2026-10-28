"""Measured, failure-isolated loading of active add-on runtimes."""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable, Mapping
from typing import Any

from sqlalchemy.exc import OperationalError

from carlina.runtime_environment import is_worker_process, worker_addon_id
from iarf_assistant.core.addon_base import AddonContext
from iarf_assistant.core.addons.capabilities import CapabilityCatalog
from iarf_assistant.core.addons.contracts import AddonSourceKind
from iarf_assistant.extensions import db
from iarf_assistant.models import Addon

from .lifecycle import AddonLifecycleOwnershipError

logger = logging.getLogger(__name__)


def worker_runtime_addon_closure_for_roots(
    packages: Mapping[str, Any],
    active_addon_ids: set[str],
    root_addon_ids: set[str],
) -> set[str]:
    """Return exact worker roots plus every compatible dependency they may call."""

    active_packages = {
        addon_id: package
        for addon_id, package in packages.items()
        if addon_id in active_addon_ids and getattr(package, "contract", None)
    }
    normalized_roots = {
        str(addon_id)
        for addon_id in root_addon_ids
        if isinstance(addon_id, str) and addon_id
    }
    if normalized_roots != root_addon_ids or not normalized_roots.issubset(
        active_packages
    ):
        return set()
    catalog = CapabilityCatalog(
        package.contract for package in active_packages.values()
    )
    selected: set[str] = set()
    pending = sorted(normalized_roots, reverse=True)
    while pending:
        addon_id = pending.pop()
        if addon_id in selected:
            continue
        package = active_packages.get(addon_id)
        if package is None:
            continue
        selected.add(addon_id)
        contract = package.contract
        dependencies = {
            dependency_id
            for dependency_id in contract.addon_dependencies
            if dependency_id in active_packages
        }
        for requirement in contract.capability_requirements:
            dependencies.update(
                match.candidate.addon_id
                for match in catalog.matches(requirement)
                if match.compatible
            )
        pending.extend(sorted(dependencies - selected, reverse=True))
    return selected


def worker_runtime_addon_closure(
    packages: Mapping[str, Any],
    active_addon_ids: set[str],
    target_addon_id: str,
) -> set[str]:
    """Return one worker owner plus every compatible dependency it may call."""

    return worker_runtime_addon_closure_for_roots(
        packages,
        active_addon_ids,
        {target_addon_id},
    )


class BuiltinAddonReconciliationError(RuntimeError):
    """A built-in package cannot be reconciled safely with persisted state."""


def builtin_migration_reconciliation_state(
    manager: Any,
    record: Addon,
    package: Any,
) -> tuple[str | None, str | None, str | None, bool]:
    """Return recorded, physical, and packaged migration reconciliation state."""

    contract = getattr(package, "contract", None)
    migration = getattr(contract, "database_migration", None)
    package_revision = (
        str(getattr(migration, "head_revision", "") or "").strip() or None
    )
    installed_revision = str(record.alembic_revision or "").strip() or None
    bundle = getattr(contract, "migration_bundle", None)
    physical_revision = (
        manager._current_addon_db_revision_from_contract(bundle)
        if bundle is not None
        else None
    )
    return (
        installed_revision,
        physical_revision,
        package_revision,
        package_revision is not None
        and (
            installed_revision != package_revision
            or physical_revision != package_revision
        ),
    )


def builtin_runtime_reconciliation_required(
    manager: Any,
    record: Addon,
    package: Any,
) -> dict[str, str] | None:
    """Return why a built-in runtime is not yet safe to import, if applicable."""

    if package is None or package.source_kind != AddonSourceKind.BUILT_IN:
        return None
    version_changed = str(record.version) != str(package.contract.version)
    try:
        (
            installed_revision,
            physical_revision,
            package_revision,
            migration_changed,
        ) = builtin_migration_reconciliation_state(manager, record, package)
    except Exception as exc:  # noqa: BLE001 - admission must fail closed.
        return {
            "installed_version": str(record.version),
            "package_version": str(package.contract.version),
            "error": f"Could not inspect the physical migration ledger: {exc}",
        }
    if not version_changed and not migration_changed:
        return None

    reconciliation = {
        "installed_version": str(record.version),
        "package_version": str(package.contract.version),
    }
    if migration_changed:
        reconciliation.update(
            {
                "installed_revision": installed_revision or "base",
                "physical_revision": physical_revision or "base",
                "package_revision": package_revision or "base",
            }
        )
    if version_changed:
        try:
            manager._validate_builtin_version_transition(
                record.id,
                reconciliation["installed_version"],
                reconciliation["package_version"],
            )
        except BuiltinAddonReconciliationError as exc:
            reconciliation["error"] = str(exc)
    return reconciliation


def load_frozen_worker_runtime_addons(
    manager: Any,
    addon_ids: Iterable[str],
) -> frozenset[str]:
    """Load exact frozen add-on roots into a worker without lifecycle writes."""

    if not is_worker_process():
        raise AddonLifecycleOwnershipError(
            "Dynamic runtime loading is available only inside a managed worker."
        )
    if isinstance(addon_ids, (str, bytes)):
        raise TypeError("Worker runtime add-on IDs must be an iterable of IDs.")
    requested: list[str] = []
    for addon_id in addon_ids:
        if (
            not isinstance(addon_id, str)
            or not addon_id
            or addon_id != addon_id.strip()
            or not addon_id.isprintable()
            or len(addon_id.encode("utf-8")) > 128
            or addon_id in requested
        ):
            raise ValueError("Worker runtime add-on IDs must be exact and unique.")
        requested.append(addon_id)
    if not requested or len(requested) > 32:
        raise ValueError("Select between 1 and 32 worker runtime add-ons.")
    roots = set(requested)
    allowed_addon_ids = getattr(manager, "allowed_addon_ids", None)
    if allowed_addon_ids is None:
        allowed_addon_ids = getattr(manager, "_allowed_addon_ids", None)
    if allowed_addon_ids is not None and not roots.issubset(allowed_addon_ids):
        raise LookupError(
            "A selected worker runtime add-on is unavailable or inactive."
        )

    with manager._lifecycle_mutation_lock():
        packages = manager._addon_packages
        if allowed_addon_ids is not None:
            packages = {
                addon_id: package
                for addon_id, package in packages.items()
                if addon_id in allowed_addon_ids
            }
        active_query = Addon.query.filter_by(is_active=True)
        if allowed_addon_ids is not None:
            active_query = active_query.filter(Addon.id.in_(allowed_addon_ids))
        active_records = {str(record.id): record for record in active_query.all()}
        active_ids = set(active_records)
        if not roots.issubset(active_ids) or not roots.issubset(packages):
            raise LookupError(
                "A selected worker runtime add-on is unavailable or inactive."
            )
        closure = worker_runtime_addon_closure_for_roots(
            packages,
            active_ids,
            roots,
        )
        if not closure or not roots.issubset(closure):
            raise LookupError(
                "The selected worker runtime dependency closure is unavailable."
            )
        for addon_id in sorted(closure):
            reconciliation = builtin_runtime_reconciliation_required(
                manager,
                active_records[addon_id],
                packages[addon_id],
            )
            if reconciliation is not None:
                manager._builtin_reconciliation_required[addon_id] = reconciliation
                raise LookupError(
                    "A selected worker runtime requires package/schema "
                    "reconciliation before it can be imported."
                )
        with manager._runtime_load_lock():
            if closure.issubset(manager.loaded_addons) and closure.issubset(
                manager._dispatch_enabled_addons
            ):
                return frozenset(closure)

        previous_loaded = set(manager.loaded_addons)
        previous_dispatch = set(manager._dispatch_enabled_addons)
        previous_registries = manager.registries
        try:
            for addon_id in sorted(closure):
                manager._load_addon(addon_id, None)
            candidate_registries = manager._compile_registries(
                loaded_addons=dict(manager.loaded_addons)
            )
            with manager._runtime_load_lock():
                if not closure.issubset(manager.loaded_addons):
                    raise RuntimeError(
                        "A selected worker runtime was not published after loading."
                    )
                manager.registries = candidate_registries
                manager._dispatch_enabled_addons.update(closure)
        except Exception:
            with manager._runtime_load_lock():
                manager.registries = previous_registries
                manager._dispatch_enabled_addons.clear()
                manager._dispatch_enabled_addons.update(previous_dispatch)
                for addon_id in sorted(
                    set(manager.loaded_addons) - previous_loaded,
                    reverse=True,
                ):
                    manager._unload_addon_runtime_locked(addon_id)
            raise
    return frozenset(closure)


class ActiveAddonStartupLoader:
    """Discover packages and load only the runtimes needed by this process."""

    def __init__(self, manager: Any, *, is_worker_process: bool) -> None:
        self.manager = manager
        self.is_worker_process = bool(is_worker_process)
        self.started_at = time.perf_counter()
        self.record_samples: list[tuple[str, float]] = []
        self.load_samples: list[tuple[str, float]] = []
        self.metrics: dict[str, Any] = {
            "runtime_kind": "worker" if self.is_worker_process else "web",
            "environment_check_ms": 0.0,
            "environment_repair_ms": 0.0,
            "environment_repairs": 0,
            "active_record_ms": 0.0,
            "active_import_ms": 0.0,
            "active_records": 0,
            "loaded_records": 0,
            "failed_records": 0,
            "reconciliation_deferred": 0,
        }

    def run(self) -> None:
        """Execute the startup transaction and retain metrics even on failure."""

        try:
            with self.manager.app.app_context():
                try:
                    self._discover_packages()
                    records = self._active_records()
                    self._load_records(records)
                    self._rebuild_registries()
                except OperationalError as exc:
                    if "no such table" not in str(exc).lower():
                        raise
                    logger.warning(
                        "Addons table not found, skipping addon loading. This is "
                        "expected during initial setup."
                    )
                    db.session.rollback()
                    self._rebuild_registries()
        finally:
            self._finalize_metrics()

    def _discover_packages(self) -> None:
        started_at = time.perf_counter()
        self.manager._sync_addons_from_filesystem(
            sync_database=self.manager.manage_lifecycle and not self.is_worker_process,
        )
        self.metrics["filesystem_sync_ms"] = self._elapsed_ms(started_at)
        self.metrics["discovery"] = dict(self.manager._last_discovery_metrics)

    def _active_records(self) -> list[Addon]:
        started_at = time.perf_counter()
        allowed_addon_ids = getattr(self.manager, "allowed_addon_ids", None)
        if allowed_addon_ids is None:
            allowed_addon_ids = getattr(
                self.manager,
                "_allowed_addon_ids",
                None,
            )
        active_query = Addon.query.filter_by(is_active=True)
        if allowed_addon_ids is not None:
            active_query = active_query.filter(Addon.id.in_(allowed_addon_ids))
        records = active_query.all()
        self.metrics["active_query_ms"] = self._elapsed_ms(started_at)
        worker_target = worker_addon_id()
        if self.is_worker_process and worker_target:
            packages = self.manager._addon_packages
            if allowed_addon_ids is not None:
                packages = {
                    addon_id: package
                    for addon_id, package in packages.items()
                    if addon_id in allowed_addon_ids
                }
            scoped_ids = worker_runtime_addon_closure(
                packages,
                {str(record.id) for record in records},
                worker_target,
            )
            records = [record for record in records if record.id in scoped_ids]
            self.metrics["worker_scope"] = (
                "capability_closure" if scoped_ids else "target_unavailable"
            )
            self.metrics["worker_target"] = worker_target
            self.metrics["worker_addons"] = sorted(scoped_ids)
        else:
            self.metrics["worker_scope"] = (
                "profile_selected" if allowed_addon_ids is not None else "all_active"
            )
        self.metrics["active_records"] = len(records)
        return records

    def _load_records(self, records: list[Addon]) -> None:
        context = AddonContext(self.manager.app, self.manager)
        self.manager._builtin_reconciliation_required = {}
        for record in records:
            started_at = time.perf_counter()
            try:
                if self._defer_builtin_reconciliation(record):
                    continue
                self._prepare_environment(record)
                self._load_package(record, context)
                if not self.manager._set_addon_dispatch_enabled(record.id, True):
                    raise RuntimeError(
                        f"Add-on '{record.id}' disappeared before dispatch publication."
                    )
                self.metrics["loaded_records"] += 1
            except Exception as exc:  # noqa: BLE001 - isolate each package.
                self._handle_load_failure(record, exc)
            finally:
                self.record_samples.append(
                    (record.id, (time.perf_counter() - started_at) * 1000)
                )

    def _defer_builtin_reconciliation(self, record: Addon) -> bool:
        package = self.manager._addon_packages.get(record.id)
        reconciliation = builtin_runtime_reconciliation_required(
            self.manager,
            record,
            package,
        )
        if reconciliation is None:
            return False
        self.manager._builtin_reconciliation_required[record.id] = reconciliation
        if reconciliation.get("error"):
            logger.error(
                "Built-in add-on '%s' cannot be admitted during ordinary startup: %s",
                record.id,
                reconciliation["error"],
            )
            self.metrics["reconciliation_deferred"] += 1
            return True
        logger.warning(
            "Built-in add-on '%s' requires explicit package/schema reconciliation "
            "(version %s -> %s, migration row/physical %s/%s -> %s); "
            "skipping import during ordinary startup. Run 'carlina init --repair' "
            "while the managed runtime is stopped.",
            record.id,
            reconciliation["installed_version"],
            reconciliation["package_version"],
            reconciliation.get("installed_revision", "base"),
            reconciliation.get("physical_revision", "base"),
            reconciliation.get("package_revision", "none"),
        )
        self.metrics["reconciliation_deferred"] += 1
        return True

    def _prepare_environment(self, record: Addon) -> None:
        package = self.manager._addon_packages.get(record.id)
        started_at = time.perf_counter()
        missing = self.manager._missing_contract_python_environments(
            record.id,
            package,
        )
        self.metrics["environment_check_ms"] += (
            time.perf_counter() - started_at
        ) * 1000
        if not (
            self.manager.manage_lifecycle and not self.is_worker_process and missing
        ):
            return

        logger.info(
            "Addon '%s' has a missing or stale Python environment. Running "
            "dependency setup.",
            record.id,
        )
        self._repair_dependencies(record.id)

    def _repair_dependencies(self, addon_id: str) -> None:
        started_at = time.perf_counter()
        self.manager.install_dependencies(addon_id, prepare_docker=False)
        self.metrics["environment_repair_ms"] += (
            time.perf_counter() - started_at
        ) * 1000
        self.metrics["environment_repairs"] += 1

    def _load_package(self, record: Addon, context: AddonContext) -> None:
        started_at = time.perf_counter()
        try:
            try:
                self.manager._load_addon(record.id, context)
            except ModuleNotFoundError as exc:
                if self.is_worker_process or not self.manager.manage_lifecycle:
                    raise
                logger.warning(
                    "Addon '%s' failed to import due to missing module '%s'. "
                    "Attempting dependency setup and retry.",
                    record.id,
                    getattr(exc, "name", "<unknown>"),
                )
                self._repair_dependencies(record.id)
                self.manager._load_addon(record.id, context)
        finally:
            self.load_samples.append(
                (record.id, (time.perf_counter() - started_at) * 1000)
            )

    def _handle_load_failure(self, record: Addon, error: Exception) -> None:
        self.metrics["failed_records"] += 1
        logger.error(
            "Failed to load active addon '%s': %s",
            record.id,
            error,
            exc_info=(type(error), error, error.__traceback__),
        )
        if self.is_worker_process or not self.manager.manage_lifecycle:
            db.session.rollback()
            return

        try:
            self.manager.lifecycle.fail(
                record,
                event="startup_load_failed",
                error=error,
                metadata={"safe_mode": True},
            )
            logger.warning(
                "Safe Mode: Moved broken addon '%s' to the failed lifecycle "
                "state to prevent a crash loop.",
                record.id,
            )
        except Exception as exc:  # noqa: BLE001 - preserve the original isolation.
            db.session.rollback()
            logger.error(
                "Safe Mode: Failed to deactivate '%s': %s",
                record.id,
                exc,
            )

    def _rebuild_registries(self) -> None:
        started_at = time.perf_counter()
        self.manager.rebuild_registries()
        self.metrics["registry_rebuild_ms"] = self._elapsed_ms(started_at)

    def _finalize_metrics(self) -> None:
        self.metrics["environment_check_ms"] = round(
            float(self.metrics["environment_check_ms"]),
            3,
        )
        self.metrics["environment_repair_ms"] = round(
            float(self.metrics["environment_repair_ms"]),
            3,
        )
        self.metrics["active_record_ms"] = round(
            sum(duration_ms for _, duration_ms in self.record_samples),
            3,
        )
        self.metrics["active_import_ms"] = round(
            sum(duration_ms for _, duration_ms in self.load_samples),
            3,
        )
        self.metrics["slowest_active_records"] = self._slowest(self.record_samples)
        self.metrics["slowest_active_imports"] = self._slowest(self.load_samples)
        self.metrics["total_ms"] = self._elapsed_ms(self.started_at)

    @staticmethod
    def _slowest(samples: list[tuple[str, float]]) -> list[dict[str, Any]]:
        return [
            {"addon_id": addon_id, "duration_ms": round(duration_ms, 3)}
            for addon_id, duration_ms in sorted(
                samples,
                key=lambda item: item[1],
                reverse=True,
            )[:5]
        ]

    @staticmethod
    def _elapsed_ms(started_at: float) -> float:
        return round((time.perf_counter() - started_at) * 1000, 3)


__all__ = [
    "ActiveAddonStartupLoader",
    "BuiltinAddonReconciliationError",
    "builtin_runtime_reconciliation_required",
    "load_frozen_worker_runtime_addons",
    "worker_runtime_addon_closure",
    "worker_runtime_addon_closure_for_roots",
]
