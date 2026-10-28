"""Discovery snapshots and trusted in-process add-on runtime loading."""

from __future__ import annotations

import copy
import importlib
import inspect
import json
import logging
import threading
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from flask import has_app_context

from carlina.addons.package_runtime_ports import (
    AddonPackageRuntimeHost as AddonPackageRuntimeHost,
)
from carlina.runtime_environment import is_worker_process
from iarf_assistant.core.addon_base import AddonContext
from iarf_assistant.core.addon_contracts import (
    MISSING_DEPENDENCY,
    ElementStatus,
)
from iarf_assistant.extensions import db
from iarf_assistant.models import Addon

from .contracts import AddonSourceKind
from .dependency_contracts import extract_dependency_addon_ids
from .discovery import DiscoveredAddon, DiscoveryResult, discover_addons
from .frontend_cache_generation import changed_frontend_package_ids
from .imports import load_installed_addon_module
from .lifecycle import (
    CODE_REMOVED,
    DATA_PRESERVED,
    DATA_PURGED,
    DISCOVERED,
    FAILED,
    VALIDATED,
    AddonLifecycleOwnershipError,
)
from .migration_contracts import stored_migration_upgrade_issues
from .schema_ownership import addon_defines_database_models, core_database_table_names
from .startup_loading import (
    ActiveAddonStartupLoader,
    load_frozen_worker_runtime_addons,
)

logger = logging.getLogger(__name__)
_MUTATION_LOCK_INITIALIZATION = threading.Lock()


def _host_allowed_addon_ids(host: object) -> frozenset[str] | None:
    selection = getattr(host, "allowed_addon_ids", None)
    if selection is None:
        selection = getattr(host, "_allowed_addon_ids", None)
    return selection


def _require_host_selected_addon(host: object, addon_id: str) -> None:
    checker = getattr(host, "_require_selected_addon", None)
    if callable(checker):
        checker(addon_id)
        return
    allowed_addon_ids = _host_allowed_addon_ids(host)
    if allowed_addon_ids is not None and addon_id not in allowed_addon_ids:
        raise ValueError("Add-on is not selected in this runtime profile.")


@dataclass(frozen=True, slots=True)
class _DiscoverySnapshot:
    manifests: dict[str, dict[str, Any]]
    addon_dirs: dict[str, Path]
    packages: dict[str, DiscoveredAddon]
    rejected_packages: dict[str, tuple[Any, ...]]
    collisions: tuple[Any, ...]
    builtin_root_available: bool
    installed_root_available: bool
    diagnostics: tuple[Any, ...]
    metrics: dict[str, float | int]


if TYPE_CHECKING:

    class _AddonPackageRuntimeMixinBase(AddonPackageRuntimeHost):
        pass

else:

    class _AddonPackageRuntimeMixinBase:
        pass


class AddonPackageRuntimeMixin(_AddonPackageRuntimeMixinBase):
    """Own validated discovery snapshots and loaded Python add-on instances."""

    def _lifecycle_mutation_lock(self) -> threading.RLock:
        """Return the outermost lock for every package/runtime state writer."""

        lock = getattr(self, "_addon_lifecycle_mutation_lock", None)
        if lock is None:
            # Compatibility for deliberately partial managers in focused tests.
            # Production managers initialize this before they are published.
            with _MUTATION_LOCK_INITIALIZATION:
                lock = getattr(self, "_addon_lifecycle_mutation_lock", None)
                if lock is None:
                    lock = threading.RLock()
                    self._addon_lifecycle_mutation_lock = lock
        return lock

    def _require_lifecycle_owner(self, operation: str) -> None:
        """Fail closed before a worker can mutate durable lifecycle state."""

        maintenance_state = getattr(
            self,
            "_addon_lifecycle_maintenance_state",
            None,
        )
        if (
            getattr(self, "manage_lifecycle", False)
            or int(getattr(maintenance_state, "depth", 0)) > 0
        ):
            return
        raise AddonLifecycleOwnershipError(
            "This process does not own add-on lifecycle mutations and cannot "
            f"{operation}."
        )

    @contextmanager
    def lifecycle_maintenance_authority(self) -> Iterator[None]:
        """Grant one command thread bounded mutation authority.

        Command-mode startup remains read-only. The authority begins only after
        the core migration command enters this scope, and it never makes the
        process a scheduler, worker, or managed-service owner.
        """

        if not (
            getattr(self, "manage_lifecycle", False)
            or getattr(self, "allow_lifecycle_maintenance", False)
        ):
            raise AddonLifecycleOwnershipError(
                "This process is not authorized for add-on lifecycle maintenance."
            )
        with self._lifecycle_mutation_lock():
            state = getattr(self, "_addon_lifecycle_maintenance_state", None)
            if state is None:
                state = threading.local()
                self._addon_lifecycle_maintenance_state = state
            previous_depth = int(getattr(state, "depth", 0))
            state.depth = previous_depth + 1
            try:
                yield
            finally:
                state.depth = previous_depth

    def _get_addon_manifest(self, addon_id: str) -> dict[str, Any] | None:
        manifest_data = self.manifests.get(addon_id)
        if manifest_data:
            return manifest_data.get("manifest")

        manifest_path = self.get_addon_directory(addon_id) / "manifest.json"
        if not manifest_path.is_file():
            return None
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(manifest, dict):
                raise TypeError("manifest.json must contain a JSON object")
            return manifest
        except (OSError, UnicodeError, TypeError, json.JSONDecodeError) as exc:
            logger.error("Failed to read manifest for add-on '%s': %s", addon_id, exc)
            return None

    def _discovery_sync_lock(self) -> threading.RLock:
        lock = getattr(self, "_addon_discovery_lock", None)
        if lock is None:
            lock = threading.RLock()
            self._addon_discovery_lock = lock
        return lock

    @staticmethod
    def _manifest_record(package: DiscoveredAddon) -> dict[str, Any]:
        manifest = package.contract.to_dict(include_manifest=True)["manifest"]
        return {
            "manifest": manifest,
            "module_path": package.module_name,
            "addon_dir": str(package.package_dir),
            "source_kind": package.source_kind.value,
            "trust_tier": package.trust_tier.value,
            "contract": package.contract,
        }

    def refresh_addon_manifest(self, addon_id: str) -> dict[str, Any]:
        """Validate and atomically publish one edited package manifest."""

        self._require_lifecycle_owner("refresh add-on manifests")
        with self._lifecycle_mutation_lock(), self._discovery_sync_lock():
            return self._refresh_addon_manifest_locked(addon_id)

    def _refresh_addon_manifest_locked(self, addon_id: str) -> dict[str, Any]:
        """Refresh one package while holding the discovery transaction lock."""

        self._require_lifecycle_owner("refresh add-on manifests")

        normalized_id = str(addon_id or "").strip()
        if not normalized_id:
            raise ValueError("An add-on ID is required.")
        _require_host_selected_addon(self, normalized_id)

        allowed_addon_ids = _host_allowed_addon_ids(self)
        if allowed_addon_ids is None:
            result = discover_addons(
                self.builtin_addons_dir,
                self.installed_addons_dir,
            )
        else:
            result = discover_addons(
                self.builtin_addons_dir,
                self.installed_addons_dir,
                allowed_addon_ids=allowed_addon_ids,
            )
        package = result.packages.get(normalized_id)
        if package is None:
            relevant = [
                diagnostic.message
                for diagnostic in result.diagnostics
                if diagnostic.severity == "error"
                and (
                    normalized_id in str(diagnostic.path or "")
                    or normalized_id in diagnostic.message
                )
            ]
            detail = "; ".join(relevant[:3]) or "package discovery rejected it"
            raise ValueError(f"Cannot refresh add-on '{normalized_id}': {detail}.")

        manifest_data = self._manifest_record(package)
        manifest = manifest_data["manifest"]
        candidate_manifests = {**self.manifests, normalized_id: manifest_data}
        candidate_dirs = {
            **self._addon_dirs,
            normalized_id: package.package_dir,
        }
        candidate_packages = {
            **self._addon_packages,
            normalized_id: package,
        }
        candidate_registries = (
            self._compile_registries(
                manifests=candidate_manifests,
                loaded_addons=dict(self.loaded_addons),
                addon_dirs=candidate_dirs,
            )
            if has_app_context()
            else getattr(self, "registries", None)
        )
        with self._runtime_load_lock(), self._inventory_manifest_lock():
            previous_manifests = self.manifests
            previous_dirs = self._addon_dirs
            previous_packages = self._addon_packages
            previous_diagnostics = getattr(self, "_discovery_diagnostics", ())
            previous_registries = getattr(self, "registries", None)
            instance = self.loaded_addons.get(normalized_id)
            previous_instance_manifest = (
                getattr(instance, "manifest", None) if instance is not None else None
            )
            try:
                self.manifests = candidate_manifests
                self._addon_dirs = candidate_dirs
                self._addon_packages = candidate_packages
                self._discovery_diagnostics = result.diagnostics
                if instance is not None:
                    instance.manifest = manifest
                if candidate_registries is not None:
                    self.registries = candidate_registries
            except Exception:
                self.manifests = previous_manifests
                self._addon_dirs = previous_dirs
                self._addon_packages = previous_packages
                self._discovery_diagnostics = previous_diagnostics
                if previous_registries is not None:
                    self.registries = previous_registries
                if instance is not None:
                    instance.manifest = previous_instance_manifest
                self.invalidate_addon_inventory("package_refresh_rollback")
                raise
            if normalized_id in changed_frontend_package_ids(
                previous_packages,
                candidate_packages,
            ):
                self.invalidate_frontend_build_statuses((normalized_id,))
            self.invalidate_addon_inventory("package_refresh")
        return manifest

    @staticmethod
    def _extract_dependency_addon_ids(dependencies: object) -> list[str]:
        """Compatibility facade for normalized dependency ownership."""

        return extract_dependency_addon_ids(dependencies)

    def _dependency_status_reasons(
        self,
        manifest: dict[str, Any],
        active_addon_ids: set[str],
    ) -> list[str]:
        reasons = []
        for dependency_id in extract_dependency_addon_ids(
            manifest.get("dependencies", {})
        ):
            if dependency_id not in self.manifests:
                reasons.append(
                    f"Required addon dependency '{dependency_id}' is not installed."
                )
            elif dependency_id not in active_addon_ids:
                reasons.append(
                    f"Required addon dependency '{dependency_id}' is inactive."
                )
        return reasons

    @staticmethod
    def _apply_dependency_status(elements: Any, reasons: list[str]) -> None:
        if not reasons:
            return
        status = ElementStatus(MISSING_DEPENDENCY, reasons)
        elements.package.status = status
        for collection_name in (
            "tools",
            "tool_views",
            "ui_surfaces",
            "helpers",
            "ui_contributions",
            "providers",
            "services",
            "workers",
            "scheduled_jobs",
            "prompts",
            "skills",
            "workflows",
            "agent_profiles",
        ):
            for record in getattr(elements, collection_name, []):
                record.status = status

    def discover_and_load_addons(self) -> None:
        """Discover packages and load the runtimes required by this process."""

        with self._lifecycle_mutation_lock():
            loader = ActiveAddonStartupLoader(
                self,
                is_worker_process=is_worker_process(),
            )
            try:
                loader.run()
            finally:
                self._startup_metrics = copy.deepcopy(loader.metrics)

    def _runtime_load_lock(self) -> threading.RLock:
        lock = getattr(self, "_addon_runtime_lock", None)
        if lock is None:
            lock = threading.RLock()
            self._addon_runtime_lock = lock
        return lock

    def _set_addon_dispatch_enabled(self, addon_id: str, enabled: bool) -> bool:
        """Update dispatch state only while the selected runtime still exists."""

        normalized_id = str(addon_id)
        with self._lifecycle_mutation_lock(), self._runtime_load_lock():
            if enabled:
                if (
                    normalized_id not in self.loaded_addons
                    or normalized_id not in self.manifests
                ):
                    self._dispatch_enabled_addons.discard(normalized_id)
                    return False
                self._dispatch_enabled_addons.add(normalized_id)
                return True
            self._dispatch_enabled_addons.discard(normalized_id)
            return False

    def ensure_worker_runtime_addons(
        self,
        addon_ids: Iterable[str],
    ) -> frozenset[str]:
        """Load exact frozen add-on roots into this worker without lifecycle writes.

        Worker startup imports only its owner's static dependency closure.
        Durable workflows may additionally freeze exact tool-provider add-ons
        that cannot be known at process startup. This boundary loads only
        those active roots and their existing compatible dependency closure,
        then publishes one registry projection. It never discovers, installs,
        activates, repairs, or records package lifecycle state.
        """

        return load_frozen_worker_runtime_addons(self, addon_ids)

    def _load_addon(
        self,
        addon_id: str,
        context_unused: AddonContext | None,
        _loading_stack: tuple[str, ...] = (),
    ) -> None:
        del context_unused
        with self._lifecycle_mutation_lock():
            self._load_addon_serialized(str(addon_id), tuple(_loading_stack))

    def _load_addon_serialized(
        self,
        addon_id: str,
        loading_stack: tuple[str, ...],
    ) -> None:
        _require_host_selected_addon(self, addon_id)
        # Constructors and imports may invoke manager APIs. Keep the runtime
        # lock limited to publication so those callbacks cannot deadlock with
        # discovery or lifecycle work. The outer mutation lock prevents a
        # concurrent writer from publishing the same add-on.
        with self._runtime_load_lock():
            if addon_id in self.loaded_addons:
                return
        if addon_id in loading_stack:
            start = loading_stack.index(addon_id)
            cycle = (*loading_stack[start:], addon_id)
            raise ImportError(f"Circular add-on dependency: {' -> '.join(cycle)}")

        manifest_data = self.manifests.get(addon_id)
        if not manifest_data:
            raise ImportError(f"Manifest for addon '{addon_id}' not found.")
        manifest = manifest_data["manifest"]
        context = AddonContext(self.app, self, addon_id=addon_id)
        try:
            self._ensure_addon_python_path(addon_id)
            self._load_addon_dependencies(
                addon_id,
                manifest,
                (*loading_stack, addon_id),
            )
            addon_class = self._import_addon_runtime_class(
                addon_id,
                manifest_data,
            )
            instance = addon_class(context, manifest)
            with self._runtime_load_lock():
                self._publish_loaded_addon(addon_id, instance, manifest)
        except Exception:
            self._unload_addon_runtime(addon_id)
            raise
        logger.info(
            "Successfully loaded active add-on: %s",
            manifest.get("name", addon_id),
        )

    def _load_addon_dependencies(
        self,
        addon_id: str,
        manifest: dict[str, Any],
        loading_stack: tuple[str, ...],
    ) -> None:
        dependency_ids = extract_dependency_addon_ids(manifest.get("dependencies", {}))
        if not dependency_ids:
            return
        records = {
            record.id: record
            for record in Addon.query.filter(Addon.id.in_(dependency_ids)).all()
        }
        for dependency_id in dependency_ids:
            record = records.get(dependency_id)
            if record is None or not record.is_active:
                raise ImportError(
                    f"Cannot load '{addon_id}': Required dependency "
                    f"'{dependency_id}' is not active."
                )
            self._load_addon(dependency_id, None, loading_stack)

    def _import_addon_runtime_class(
        self,
        addon_id: str,
        manifest_data: dict[str, Any],
    ) -> type[Any]:
        manifest = manifest_data["manifest"]
        package = self._addon_packages.get(addon_id)
        package_module_path = str(manifest_data["module_path"])
        if self._addon_source_kind(addon_id) == AddonSourceKind.INSTALLED:
            load_installed_addon_module(
                addon_id,
                self.get_addon_directory(addon_id),
            )
        else:
            importlib.import_module(package_module_path)

        addon_dir = self.get_addon_directory(addon_id)
        if addon_defines_database_models(addon_dir):
            importlib.import_module(f"{package_module_path}.models")
            logger.info("Discovered models for add-on: %s", addon_id)

        contract = package.contract if package is not None else None
        entry_module = contract.entry_point_module if contract else "."
        full_module_path = (
            package_module_path
            if entry_module == "."
            else f"{package_module_path}.{entry_module}"
        )
        addon_module = importlib.import_module(full_module_path)
        class_name = (
            contract.entry_point_class
            if contract is not None
            else manifest.get("entry_point_class")
        )
        if not class_name:
            raise TypeError(
                f"Manifest for addon '{addon_id}' is missing an entry-point class."
            )
        addon_class = getattr(addon_module, class_name, None)
        if not addon_class or not inspect.isclass(addon_class):
            raise TypeError(
                f"Could not find class '{class_name}' in '{full_module_path}'."
            )
        return addon_class

    def _publish_loaded_addon(
        self,
        addon_id: str,
        instance: Any,
        manifest: dict[str, Any],
    ) -> None:
        addon_types = manifest.get("addon_type", [])
        if not isinstance(addon_types, list):
            addon_types = [addon_types]
        roles = manifest.get("roles", [])
        if not isinstance(roles, list):
            roles = [roles]
        ui_entries = manifest.get("ui_entry_points") or {}

        toolkit_roles = {"toolkit", "integration", "provider", "service", "system"}
        component_roles = {"ui_component", "helper"}
        workspace_roles = {"workspace", "workflow"}
        self.loaded_addons[addon_id] = instance
        if (
            "automation" in addon_types
            or workspace_roles.intersection(roles)
            or ui_entries.get("automation")
        ):
            self.automations[addon_id] = instance
        if (
            "plugin" in addon_types
            or toolkit_roles.intersection(roles)
            or manifest.get("tools")
            or manifest.get("providers")
        ):
            self.plugins[addon_id] = instance
        if (
            "helper" in addon_types
            or component_roles.intersection(roles)
            or manifest.get("ui_components")
            or manifest.get("helpers")
            or ui_entries.get("helper")
        ):
            self.helpers[addon_id] = instance

    @classmethod
    def _build_discovery_snapshot(
        cls,
        result: DiscoveryResult,
    ) -> _DiscoverySnapshot:
        started_at = time.perf_counter()
        packages = dict(result.packages)
        manifests = {
            addon_id: cls._manifest_record(package)
            for addon_id, package in packages.items()
        }
        metrics: dict[str, float | int] = {
            **dict(result.metrics),
            "projection_ms": round(
                (time.perf_counter() - started_at) * 1000,
                3,
            ),
            "database_sync_ms": 0.0,
        }
        return _DiscoverySnapshot(
            manifests=manifests,
            addon_dirs={
                addon_id: package.package_dir for addon_id, package in packages.items()
            },
            packages=packages,
            rejected_packages=dict(result.rejected_packages),
            collisions=tuple(result.collisions),
            builtin_root_available=result.builtin_root_available,
            installed_root_available=result.installed_root_available,
            diagnostics=tuple(result.diagnostics),
            metrics=metrics,
        )

    @staticmethod
    def _log_discovery_diagnostics(snapshot: _DiscoverySnapshot) -> None:
        for diagnostic in snapshot.diagnostics:
            log = logger.error if diagnostic.severity == "error" else logger.debug
            log(
                "Add-on discovery %s%s: %s",
                diagnostic.code,
                f" ({diagnostic.path})" if diagnostic.path else "",
                diagnostic.message,
            )

    @staticmethod
    def _contract_database_fields(package: DiscoveredAddon) -> dict[str, Any]:
        contract_payload = package.contract.to_dict()
        return {
            "source_kind": package.source_kind.value,
            "trust_tier": package.trust_tier.value,
            "manifest_sha256": package.contract.manifest_sha256,
            "contract_sha256": package.contract.contract_sha256,
            "resolved_contract_json": contract_payload,
            "provenance_json": contract_payload.get("provenance", {}),
            "code_path": str(package.package_dir),
        }

    @staticmethod
    def _apply_package_database_fields(
        record: Addon,
        package: DiscoveredAddon,
    ) -> None:
        existing_source = str(record.source_kind or "").strip()
        incoming_source = package.source_kind.value
        ownership_established = bool(
            record.code_path
            or record.manifest_sha256
            or record.contract_sha256
            or record.resolved_contract_json
        )
        if (
            ownership_established
            and existing_source
            and existing_source != incoming_source
        ):
            raise RuntimeError(
                f"Refusing to transfer add-on '{record.id}' package ownership "
                f"from {existing_source!r} to {incoming_source!r} without an "
                "explicit namespace migration."
            )
        for field_name, value in AddonPackageRuntimeMixin._contract_database_fields(
            package
        ).items():
            if hasattr(record, field_name):
                setattr(record, field_name, value)

    def _add_discovered_database_record(
        self,
        addon_id: str,
        package: DiscoveredAddon,
        manifest: dict[str, Any],
    ) -> None:
        record = Addon()
        record.id = addon_id
        record.version = str(manifest.get("version", "0.0.0"))
        record.is_active = False
        record.lifecycle_state = DISCOVERED
        self._apply_package_database_fields(record, package)
        db.session.add(record)
        self.lifecycle.transition(
            record,
            VALIDATED,
            event="package_discovered",
            metadata={
                "source_kind": package.source_kind.value,
                "trust_tier": package.trust_tier.value,
                "manifest_sha256": package.contract.manifest_sha256,
                "contract_sha256": package.contract.contract_sha256,
            },
            commit=False,
        )
        logger.info(
            "Discovered new add-on '%s'; registered it as inactive.",
            addon_id,
        )

    def _mark_database_record_code_missing(self, record: Addon) -> None:
        previous_active = bool(record.is_active)
        state = str(record.lifecycle_state or DISCOVERED)
        record.is_active = False
        if state not in {CODE_REMOVED, DATA_PRESERVED, DATA_PURGED}:
            self.lifecycle.transition(
                record,
                CODE_REMOVED,
                event="package_code_missing",
                metadata={"last_code_path": record.code_path},
                commit=False,
                force=True,
            )
        elif previous_active:
            self.lifecycle.record_event(
                record,
                event="package_code_missing_consistency_repaired",
                metadata={"previous_is_active": True},
                commit=False,
            )
        logger.warning(
            "Add-on '%s' has no selected code package; its record and data "
            "are preserved.",
            record.id,
        )

    def _mark_database_record_discovery_failed(
        self,
        record: Addon,
        diagnostics: tuple[Any, ...],
    ) -> None:
        messages = [str(item.message) for item in diagnostics if item.message]
        detail = "; ".join(messages[:3]) or "Package discovery rejected the code."
        metadata = {
            "diagnostic_codes": [str(item.code) for item in diagnostics],
            "diagnostic_count": len(diagnostics),
            "code_path": record.code_path,
        }
        previous_state = str(record.lifecycle_state or DISCOVERED)
        previous_error = str(record.lifecycle_error or "")
        previous_active = bool(record.is_active)
        if previous_state != FAILED:
            self.lifecycle.fail(
                record,
                event="package_discovery_failed",
                error=detail,
                metadata=metadata,
                commit=False,
            )
        else:
            record.is_active = False
            record.lifecycle_error = detail
            if previous_error != detail or previous_active:
                self.lifecycle.record_event(
                    record,
                    event="package_discovery_failure_updated",
                    success=False,
                    detail=detail,
                    metadata={
                        **metadata,
                        "previous_is_active": previous_active,
                    },
                    commit=False,
                )
        logger.error(
            "Add-on '%s' has a package on disk, but discovery rejected it: %s",
            record.id,
            detail,
        )

    def _sync_discovery_database(
        self,
        snapshot: _DiscoverySnapshot,
    ) -> set[str]:
        try:
            if not snapshot.builtin_root_available:
                raise RuntimeError(
                    "The built-in add-on root is unavailable; refusing to treat "
                    "the inventory as authoritative."
                )
            allowed_addon_ids = _host_allowed_addon_ids(self)
            if allowed_addon_ids is None:
                records = {record.id: record for record in Addon.query.all()}
            else:
                records = {
                    record.id: record
                    for record in Addon.query.filter(
                        Addon.id.in_(allowed_addon_ids)
                    ).all()
                }
            if not snapshot.installed_root_available and any(
                record.source_kind == AddonSourceKind.INSTALLED.value
                for record in records.values()
            ):
                raise RuntimeError(
                    "The installed add-on root is unavailable while installed "
                    "package records exist; refusing to mark their code removed."
                )
            self._validate_discovery_collisions(snapshot, records)
            disk_ids = set(snapshot.manifests)
            rejected_ids = set(snapshot.rejected_packages)
            database_ids = set(records)
            for addon_id in sorted(disk_ids - database_ids):
                self._add_discovered_database_record(
                    addon_id,
                    snapshot.packages[addon_id],
                    snapshot.manifests[addon_id]["manifest"],
                )
            for addon_id in sorted(disk_ids & database_ids):
                self._apply_package_database_fields(
                    records[addon_id],
                    snapshot.packages[addon_id],
                )
            rejected_record_ids = database_ids & rejected_ids
            for addon_id in sorted(rejected_record_ids):
                self._mark_database_record_discovery_failed(
                    records[addon_id],
                    snapshot.rejected_packages[addon_id],
                )
            missing_ids = database_ids - disk_ids - rejected_ids
            for addon_id in sorted(missing_ids):
                self._mark_database_record_code_missing(records[addon_id])
            db.session.commit()
            return missing_ids | rejected_record_ids
        except Exception:
            db.session.rollback()
            raise

    @staticmethod
    def _validate_discovery_collisions(
        snapshot: _DiscoverySnapshot,
        records: dict[str, Addon],
    ) -> None:
        for collision in snapshot.collisions:
            selected = snapshot.packages[collision.selected_id]
            folded_ids = {
                collision.selected_id.casefold(),
                collision.rejected_id.casefold(),
            }
            matching_records = [
                record
                for record in records.values()
                if record.id.casefold() in folded_ids
            ]
            for record in matching_records:
                selected_record_is_safe = (
                    record.id == collision.selected_id
                    and record.source_kind == selected.source_kind.value
                )
                if not selected_record_is_safe:
                    raise RuntimeError(
                        "Add-on namespace collision would change package/data "
                        f"ownership for '{record.id}': selected "
                        f"{collision.selected_source_kind.value} package "
                        f"'{collision.selected_id}' conflicts with "
                        f"{collision.rejected_source_kind.value} package "
                        f"'{collision.rejected_id}'."
                    )

        database_table_owners: dict[str, str] = {
            table_name.casefold(): "Carlina core"
            for table_name in core_database_table_names()
        }

        def claim_database_table(addon_id: str, table_name: object) -> None:
            normalized = str(table_name or "").strip()
            if not normalized:
                return
            folded = normalized.casefold()
            owner = database_table_owners.get(folded)
            if owner is not None and owner != addon_id:
                raise RuntimeError(
                    "Add-on database table ownership collision: "
                    f"'{addon_id}' and '{owner}' both claim {normalized!r}."
                )
            database_table_owners[folded] = addon_id

        for addon_id, record in records.items():
            payload = (
                record.resolved_contract_json
                if isinstance(record.resolved_contract_json, dict)
                else {}
            )
            descriptor = payload.get("database_migration")
            if isinstance(descriptor, dict):
                claim_database_table(addon_id, descriptor.get("version_table"))
                for table_name in descriptor.get("owned_tables") or ():
                    claim_database_table(addon_id, table_name)

        for addon_id, package in snapshot.packages.items():
            target = package.contract.migration_bundle
            if target is not None:
                claim_database_table(addon_id, target.version_table)
                for table_name in target.owned_tables:
                    claim_database_table(addon_id, table_name)
            record = records.get(addon_id)
            if record is None:
                continue
            payload = (
                record.resolved_contract_json
                if isinstance(record.resolved_contract_json, dict)
                else {}
            )
            raw_previous = payload.get("database_migration")
            previous = raw_previous if isinstance(raw_previous, dict) else None
            issues = list(stored_migration_upgrade_issues(previous, target))
            recorded_revision = str(record.alembic_revision or "").strip() or None
            if previous is None and recorded_revision is not None:
                if target is None:
                    issues.append(
                        "the selected package removes recorded migration state"
                    )
                elif recorded_revision not in target.revision_ids:
                    issues.append(
                        f"recorded revision {recorded_revision!r} is absent "
                        "from the selected migration graph"
                    )
            if issues:
                raise RuntimeError(
                    f"Unsafe migration contract transition for '{addon_id}': "
                    + "; ".join(dict.fromkeys(issues))
                    + "."
                )

    def _discard_missing_runtimes(self, addon_ids: set[str]) -> None:
        for addon_id in sorted(addon_ids):
            try:
                self._unload_addon_runtime_locked(addon_id)
            except Exception:
                logger.exception(
                    "Failed to fully unload missing add-on runtime '%s'.",
                    addon_id,
                )
                self._dispatch_enabled_addons.discard(addon_id)
                self.loaded_addons.pop(addon_id, None)
                self.plugins.pop(addon_id, None)
                self.automations.pop(addon_id, None)
                self.helpers.pop(addon_id, None)

    def _publish_discovery_snapshot(self, snapshot: _DiscoverySnapshot) -> None:
        with self._inventory_manifest_lock():
            changed_packages = changed_frontend_package_ids(
                self._addon_packages,
                snapshot.packages,
            )
            self.manifests = snapshot.manifests
            self._addon_dirs = snapshot.addon_dirs
            self._addon_packages = snapshot.packages
            self._discovery_diagnostics = snapshot.diagnostics
            self._last_discovery_metrics = snapshot.metrics
            self.invalidate_frontend_build_statuses(changed_packages)
            self.invalidate_addon_inventory("package_discovery")

    def _sync_addons_from_filesystem(self, sync_database: bool = True) -> None:
        """Publish a validated snapshot only after its database sync succeeds."""

        if sync_database:
            self._require_lifecycle_owner("synchronize the add-on database")
        with self._lifecycle_mutation_lock(), self._discovery_sync_lock():
            self._sync_addons_from_filesystem_locked(sync_database)

    def _sync_addons_from_filesystem_locked(self, sync_database: bool) -> None:
        """Execute one serialized discovery and database transaction."""

        if sync_database:
            self._require_lifecycle_owner("synchronize the add-on database")

        allowed_addon_ids = _host_allowed_addon_ids(self)
        if allowed_addon_ids is None:
            result = discover_addons(
                self.builtin_addons_dir,
                self.installed_addons_dir,
            )
        else:
            result = discover_addons(
                self.builtin_addons_dir,
                self.installed_addons_dir,
                allowed_addon_ids=allowed_addon_ids,
            )
        snapshot = self._build_discovery_snapshot(result)
        self._log_discovery_diagnostics(snapshot)
        if not snapshot.builtin_root_available:
            raise RuntimeError(
                "The built-in add-on root is unavailable; preserving the last "
                "known package snapshot."
            )
        if not snapshot.installed_root_available and any(
            package.source_kind == AddonSourceKind.INSTALLED
            and (allowed_addon_ids is None or addon_id in allowed_addon_ids)
            for addon_id, package in self._addon_packages.items()
        ):
            raise RuntimeError(
                "The installed add-on root is unavailable; preserving the last "
                "known installed package snapshot."
            )
        with self._runtime_load_lock():
            missing_runtime_ids = set(self.loaded_addons) - set(snapshot.packages)
            if sync_database:
                started_at = time.perf_counter()
                missing_runtime_ids.update(self._sync_discovery_database(snapshot))
                snapshot.metrics["database_sync_ms"] = round(
                    (time.perf_counter() - started_at) * 1000,
                    3,
                )
            self._discard_missing_runtimes(missing_runtime_ids)
            self._publish_discovery_snapshot(snapshot)


__all__ = ["AddonPackageRuntimeHost", "AddonPackageRuntimeMixin"]
