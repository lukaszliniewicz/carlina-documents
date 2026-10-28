import atexit
import copy
import inspect
import json
import logging
import re
import threading
import time
from collections.abc import Iterable
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flask import Flask

from carlina.core.tool_risk import classify_tool_risk, tool_is_read_only

from ..extensions import db
from .addon_contracts import (
    ACTIVE,
    DEGRADED,
    MISSING_DEPENDENCY,
    AddonRegistries,
    ElementStatus,
    ManifestCompatibilityParser,
)
from .addons.builtin_reconciliation import AddonBuiltinReconciliationMixin
from .addons.capability_discovery import (
    CAPABILITY_SEARCH_DEFAULT_LIMIT,
    CapabilityDiscoveryMixin,
)
from .addons.capability_matching import CAPABILITY_TOKEN_RE
from .addons.contracts import ADDON_ID_PATTERN, AddonSourceKind
from .addons.database_migrations import AddonDatabaseMigrationMixin
from .addons.discovery import _validate_allowed_addon_ids
from .addons.dependency_orchestration import DependencyOrchestrationMixin
from .addons.frontend_build_orchestration import FrontendBuildOrchestrationMixin
from .addons.imports import vendor_finder
from .addons.inventory_projection import AddonInventoryProjectionMixin
from .addons.lifecycle import AddonLifecycleService
from .addons.lifecycle_orchestration import AddonLifecycleOrchestrationMixin
from .addons.lifecycle_teardown import AddonLifecycleTeardownMixin
from .addons.managed_process_orchestration import (
    ManagedProcessOrchestrationMixin,
)
from .addons.package_runtime import AddonPackageRuntimeMixin
from .addons.registry_rebuild import AddonRegistryRebuildMixin
from .addons.runtime_model_roles import AddonRuntimeModelRolesMixin
from .addons.runtime_planning import AddonRuntimePlanningMixin
from .addons.source_coverage import AddonSourceCoverageMixin
from .execution_scope import SCOPED, merge_unique, normalize_scope_mode, string_list
from .frontend_orchestration import (
    get_cached_frontend_workspace_status,
)
from .workflow_templates import (
    TemplateDefinitionError,
    compile_template_definition,
    executable_definition_from_addon_workflow,
)

__all__ = ["AddonManagerCore", "_compact_scope_alias"]

def _compact_scope_alias(value: object) -> str:
    return "".join(ch for ch in str(value or "").strip().lower() if ch.isalnum())


class AddonManagerCore(
    AddonLifecycleOrchestrationMixin,
    AddonLifecycleTeardownMixin,
    AddonBuiltinReconciliationMixin,
    AddonDatabaseMigrationMixin,
    AddonPackageRuntimeMixin,
    AddonRegistryRebuildMixin,
    ManagedProcessOrchestrationMixin,
    DependencyOrchestrationMixin,
    AddonRuntimePlanningMixin,
    AddonRuntimeModelRolesMixin,
    AddonSourceCoverageMixin,
    CapabilityDiscoveryMixin,
    FrontendBuildOrchestrationMixin,
    AddonInventoryProjectionMixin,
):
    def __init__(
        self,
        app: Flask,
        *,
        manage_lifecycle: bool = True,
        allow_lifecycle_maintenance: bool = False,
        allowed_addon_ids: frozenset[str] | None = None,
        container_services: bool = True,
    ) -> None:
        if type(container_services) is not bool:
            raise TypeError("Container service selection must be a boolean.")
        self._container_services_enabled = container_services
        self._allowed_addon_ids = _validate_allowed_addon_ids(allowed_addon_ids)
        self.app = app
        self.manage_lifecycle = bool(manage_lifecycle)
        self.allow_lifecycle_maintenance = bool(allow_lifecycle_maintenance)
        candidate_addons_dir = Path(app.root_path) / "addons"
        if not candidate_addons_dir.is_dir():
            package_root = Path(__file__).resolve().parents[1]
            if (package_root / "addons").is_dir():
                candidate_addons_dir = package_root / "addons"
        self.builtin_addons_dir = candidate_addons_dir.resolve()
        # Compatibility alias: existing built-in code expects ``addons_dir``
        # to refer to the immutable package tree.
        self.addons_dir = self.builtin_addons_dir
        app_paths = (getattr(app, "extensions", None) or {}).get("carlina_paths")
        self.installed_addons_dir = (
            Path(app_paths.installed_addons_dir).resolve()
            if app_paths is not None
            else (Path(app.instance_path) / "addons" / "installed").resolve()
        )
        self._addon_packages = {}
        self._addon_dirs: dict[str, Path] = {}
        self._vendor_finder = vendor_finder()
        self.lifecycle = AddonLifecycleService(db.session)
        self.automations = {}
        self.plugins = {}
        self.helpers = {}
        self.manifests = {}  # All discovered manifests from filesystem
        self.loaded_addons = {}  # Master list of all ACTIVE instances
        self._startup_metrics: dict[str, Any] = {}
        self._last_discovery_metrics: dict[str, float | int] = {}
        self._dispatch_enabled_addons: set[str] = set()
        self._builtin_reconciliation_required: dict[str, dict[str, str]] = {}
        self.compatibility_parser = ManifestCompatibilityParser()
        self.registries = AddonRegistries()
        self._addon_inventory_generation = 0
        self._addon_inventory_metadata_cache = None
        self._addon_inventory_cache_metrics = {
            "hits": 0,
            "misses": 0,
            "stale_hits": 0,
            "invalidations": 0,
            "evictions": 0,
            "builds": 0,
            "build_errors": 0,
            "last_build_ms": None,
            "total_build_ms": 0.0,
            "current_bytes": 0,
        }
        self._addon_inventory_cache_lock = threading.RLock()
        self._addon_inventory_last_invalidation_reason = None
        self._addon_inventory_last_invalidation_at = None
        self._addon_inventory_last_build_at = None

        # --- Runtime resource tracking ---
        self._initialize_managed_process_orchestration()
        self._manifest_lock = threading.RLock()
        # All package/runtime writers acquire this first. The only valid nested
        # order is lifecycle mutation -> discovery -> runtime -> inventory.
        self._addon_lifecycle_mutation_lock = threading.RLock()
        self._addon_lifecycle_maintenance_state = threading.local()
        self._addon_discovery_lock = threading.RLock()
        self._addon_runtime_lock = threading.RLock()
        self._initialize_container_lifecycle()
        project_root = Path(app.root_path).resolve().parent
        frontend_snapshot = self._frontend_workspace_status(project_root) or {}
        self._frontend_build_status_cache = {
            str(status.get("addon_id")): status
            for status in frontend_snapshot.get("addons", [])
            if status.get("addon_id")
        }

        atexit.register(self.cleanup_workers)

        logging.info(
            "Addon manager initialized for built-ins=%s installed=%s",
            self.builtin_addons_dir,
            self.installed_addons_dir,
        )

    @property
    def allowed_addon_ids(self) -> frozenset[str] | None:
        """Return the immutable package selection for this runtime composition."""

        return getattr(self, "_allowed_addon_ids", None)

    @property
    def container_services_enabled(self) -> bool:
        """Return whether this application composition includes containers."""

        return getattr(self, "_container_services_enabled", True)

    def _initialize_container_lifecycle(self) -> None:
        raise NotImplementedError

    def _cleanup_container_lifecycle(self) -> None:
        raise NotImplementedError

    def _frontend_workspace_status(
        self, project_root: Path
    ) -> dict[str, Any] | None:
        return get_cached_frontend_workspace_status(project_root)

    def _require_selected_addon(self, addon_id: str) -> None:
        allowed_addon_ids = self.allowed_addon_ids
        if allowed_addon_ids is not None and addon_id not in allowed_addon_ids:
            raise ValueError("Add-on is not selected in this runtime profile.")

    def get_addon_directory(self, addon_id: str) -> Path:
        """Return the selected immutable code directory for one add-on."""

        normalized_id = str(addon_id or "").strip()
        if not ADDON_ID_PATTERN.fullmatch(normalized_id):
            raise ValueError(f"Unsafe add-on ID: {addon_id!r}.")
        selected = (getattr(self, "_addon_dirs", None) or {}).get(normalized_id)
        if selected is not None:
            return selected
        builtin_root_value = getattr(self, "builtin_addons_dir", None)
        if builtin_root_value is None:
            builtin_root_value = getattr(self, "addons_dir", None)
        if builtin_root_value is None:
            raise RuntimeError("The built-in add-on root has not been configured.")
        builtin_root = Path(builtin_root_value).resolve()
        candidate = (builtin_root / normalized_id).resolve()
        candidate.relative_to(builtin_root)
        return candidate

    def get_startup_metrics(self) -> dict[str, Any]:
        """Return a defensive snapshot of the most recent startup work."""

        return copy.deepcopy(self._startup_metrics)

    def get_addon_contract(self, addon_id: str):
        """Return an add-on's immutable activation contract, if discovered.

        Consumers that only need declarative metadata must not load or inspect
        another add-on's Python implementation. This accessor keeps the package
        registry private while exposing the stable contract boundary.
        """

        package = (getattr(self, "_addon_packages", None) or {}).get(str(addon_id))
        return getattr(package, "contract", None)

    def get_loaded_addon(self, addon_id: str):
        """Return an active add-on instance through the supported runtime API."""

        return self.loaded_addons.get(str(addon_id))

    def _addon_source_kind(self, addon_id: str) -> AddonSourceKind:
        package = (getattr(self, "_addon_packages", None) or {}).get(str(addon_id))
        if package is not None:
            return package.source_kind
        return AddonSourceKind.BUILT_IN

    def _append_registered_log(self, source_id: str, message: str) -> None:
        registry = getattr(self.app, "log_registry", None)
        if not registry:
            return
        try:
            registry.append_to_source(source_id, message)
        except Exception as e:
            logging.warning(
                f"Failed to append to registered log source '{source_id}': {e}"
            )

    def _register_docker_log_sources(
        self, addon_id: str, services: dict | None = None
    ) -> None:
        registry = getattr(self.app, "log_registry", None)
        if not registry:
            return

        service_names = list((services or {"service": {}}).keys()) or ["service"]
        for service_name in service_names:
            try:
                registry.register_docker_source(
                    f"{addon_id}.{service_name}",
                    name=f"{addon_id} {service_name} Docker",
                    addon_id=addon_id,
                )
            except Exception as e:
                logging.warning(
                    f"Failed to register Docker log source for addon '{addon_id}' service '{service_name}': {e}"
                )

    def _append_docker_log(
        self, addon_id: str, service_name: str | None, message: str
    ) -> None:
        service_name = service_name or "service"
        self._register_docker_log_sources(addon_id, {service_name: {}})
        self._append_registered_log(
            "docker.addon_services",
            f"addon_id={addon_id} service={service_name} {message}",
        )
        self._append_registered_log(f"docker.{addon_id}.{service_name}", message)

    def cleanup_workers(self):
        """Ensures background worker processes and containers are terminated on server shutdown."""
        self.cleanup_managed_processes()
        self._cleanup_container_lifecycle()

    @staticmethod
    def _job_belongs_to_addon(addon_id: str, job_id: str | None) -> bool:
        if not addon_id or not job_id:
            return False
        text = str(job_id)
        prefixes = (
            f"{addon_id}_",
            f"{addon_id}.",
            f"{addon_id}-",
            f"{addon_id}:",
        )
        return text == addon_id or text.startswith(prefixes)

    def get_scheduled_job_states(
        self,
        addon_ids: Iterable[str],
    ) -> dict[str, dict[str, Any]]:
        """Return one scheduler snapshot indexed across multiple add-ons."""

        normalized_ids = list(
            dict.fromkeys(str(addon_id) for addon_id in addon_ids if addon_id)
        )
        requested_ids = set(normalized_ids)
        declared_by_addon: dict[str, list[dict[str, Any]]] = {
            addon_id: [] for addon_id in normalized_ids
        }
        registries = getattr(self, "registries", None)
        if registries and getattr(registries, "scheduled_jobs", None):
            for job in registries.scheduled_jobs.list():
                if job.addon_id in requested_ids:
                    declared_by_addon[job.addon_id].append(asdict(job))

        scheduled_by_addon: dict[str, list[dict[str, Any]]] = {
            addon_id: [] for addon_id in normalized_ids
        }
        scheduler = getattr(self.app, "scheduler", None)
        scheduler_jobs = (
            scheduler.get_jobs()
            if normalized_ids and scheduler and hasattr(scheduler, "get_jobs")
            else []
        )
        for job in scheduler_jobs:
            job_id = str(getattr(job, "id", "") or "")
            next_run_time = getattr(job, "next_run_time", None)
            public_job = {
                "id": job_id,
                "name": getattr(job, "name", job_id),
                "trigger": str(getattr(job, "trigger", "")),
                "next_run_at": next_run_time.isoformat() if next_run_time else None,
                "paused": next_run_time is None,
                "pending": bool(getattr(job, "pending", False)),
            }
            for addon_id in normalized_ids:
                if self._job_belongs_to_addon(addon_id, job_id):
                    scheduled_by_addon[addon_id].append(public_job)

        states: dict[str, dict[str, Any]] = {}
        for addon_id in normalized_ids:
            declared_records = declared_by_addon[addon_id]
            scheduled_jobs = sorted(
                scheduled_by_addon[addon_id],
                key=lambda job: str(job.get("id", "")),
            )
            status = (
                "running"
                if scheduled_jobs
                else ("configured" if declared_records else "not_declared")
            )
            states[addon_id] = {
                "status": status,
                "declared": len(declared_records),
                "registered": len(scheduled_jobs),
                "paused": sum(1 for job in scheduled_jobs if job.get("paused")),
                "jobs": scheduled_jobs,
                "declared_jobs": declared_records,
                "log_source_id": f"job.{addon_id}",
            }
        return states

    def get_scheduled_job_state(self, addon_id: str) -> dict[str, Any]:
        """Return scheduled job state for one add-on."""

        return self.get_scheduled_job_states([addon_id])[addon_id]

    def _resolve_owned_scheduled_job(self, addon_id: str, job_id: str):
        scheduler = getattr(self.app, "scheduler", None)
        if not scheduler:
            raise RuntimeError("Scheduler is not initialized.")
        if not job_id:
            raise ValueError("job_id is required.")
        if not self._job_belongs_to_addon(addon_id, job_id):
            raise ValueError(f"Job '{job_id}' does not belong to addon '{addon_id}'.")

        job = scheduler.get_job(job_id) if hasattr(scheduler, "get_job") else None
        if not job:
            raise KeyError(f"Scheduled job '{job_id}' was not found.")
        return scheduler, job

    def pause_scheduled_job(self, addon_id: str, job_id: str) -> dict:
        scheduler, _job = self._resolve_owned_scheduled_job(addon_id, job_id)
        scheduler.pause_job(job_id)
        return self.get_scheduled_job_state(addon_id)

    def resume_scheduled_job(self, addon_id: str, job_id: str) -> dict:
        scheduler, _job = self._resolve_owned_scheduled_job(addon_id, job_id)
        scheduler.resume_job(job_id)
        return self.get_scheduled_job_state(addon_id)

    def trigger_scheduled_job_now(self, addon_id: str, job_id: str) -> dict:
        scheduler, job = self._resolve_owned_scheduled_job(addon_id, job_id)
        if getattr(job, "next_run_time", None) is None:
            scheduler.resume_job(job_id)
        scheduler.modify_job(job_id, next_run_time=datetime.now(timezone.utc))
        return self.get_scheduled_job_state(addon_id)

    def register_blueprints(self, app) -> None:
        """Compatibility facade for the add-on HTTP registration adapter."""

        from ..addon_dispatch import register_addon_blueprints

        register_addon_blueprints(app, self)

    def get_automation_details(self):
        """Returns a list of details for all loaded automations with a dedicated UI page."""
        contribution_details = self._details_from_ui_contributions(
            entrypoint_kinds={"workspace_launcher", "sidebar"},
            contribution_kinds={"workspace"},
        )
        if contribution_details:
            return contribution_details

        if self.registries.ui_surfaces.records:
            details = []
            for surface in sorted(
                self.registries.ui_surfaces.list(), key=lambda item: item.name
            ):
                if surface.placement != "sidebar" or not surface.view_func:
                    continue
                if surface.status.state not in {"active", "degraded"}:
                    continue
                details.append(
                    {
                        "id": surface.addon_id,
                        "stable_id": surface.id,
                        "name": surface.name,
                        "description": self.manifests.get(surface.addon_id, {})
                        .get("manifest", {})
                        .get("description", ""),
                        "view_func": surface.view_func,
                        "icon": surface.icon,
                    }
                )
            if details:
                return details

        details = []
        sorted_automations = sorted(
            self.automations.values(), key=lambda x: x.manifest["name"]
        )

        for instance in sorted_automations:
            meta = instance.manifest
            ui_entry = meta.get("ui_entry_points", {}).get("automation")

            if ui_entry and ui_entry.get("view_func"):
                details.append(
                    {
                        "id": meta["id"],
                        "name": ui_entry.get("name", meta["name"]),
                        "description": ui_entry.get(
                            "description", meta.get("description", "")
                        ),
                        "view_func": ui_entry["view_func"],
                        "icon": ui_entry.get("icon", "fa-robot"),
                    }
                )
        return details

    def get_settings_schemas(self):
        """Returns a list of settings schemas from all loaded addons."""
        schemas = []
        # Sort addons by name for consistent UI
        sorted_addons = sorted(
            self.loaded_addons.values(), key=lambda x: x.manifest["name"]
        )
        for instance in sorted_addons:
            if (
                "settings_schema" in instance.manifest
                or "custom_settings_ui" in instance.manifest
            ):
                schemas.append(
                    {
                        "addon_id": instance.id,
                        "addon_name": instance.manifest.get("name", instance.id),
                        "schema": instance.manifest.get("settings_schema", []),
                        "custom_settings_ui": instance.manifest.get(
                            "custom_settings_ui", False
                        ),
                    }
                )
        return schemas

    @staticmethod
    def _normalize_dependency_notices(manifest: dict) -> list[dict]:
        """Return bounded dependency notices suitable for API/UI display."""

        raw_notices = manifest.get("dependency_notices")
        if not isinstance(raw_notices, list):
            return []
        notices = []
        seen_ids = set()
        for raw_notice in raw_notices[:50]:
            if not isinstance(raw_notice, dict):
                continue
            notice_id = str(raw_notice.get("id") or "").strip()[:128]
            raw_packages = raw_notice.get("packages")
            raw_packages = raw_packages if isinstance(raw_packages, list) else []
            packages = [
                str(package).strip()[:200]
                for package in raw_packages[:50]
                if str(package).strip()
            ]
            license_name = str(raw_notice.get("license") or "").strip()[:500]
            purpose = str(raw_notice.get("purpose") or "").strip()[:4_000]
            if (
                not notice_id
                or notice_id in seen_ids
                or not packages
                or not license_name
                or not purpose
            ):
                continue
            seen_ids.add(notice_id)
            notices.append(
                {
                    "id": notice_id,
                    "packages": packages,
                    "license": license_name,
                    "purpose": purpose,
                    "review_required": bool(raw_notice.get("review_required", True)),
                    "source_url": str(raw_notice.get("source_url") or "").strip()[
                        :2_000
                    ],
                }
            )
        return notices

    def get_addon_dependency_notices(self, addon_id: str) -> list[dict]:
        manifest_data = (self.manifests or {}).get(str(addon_id)) or {}
        manifest = manifest_data.get("manifest") or {}
        return self._normalize_dependency_notices(manifest)

    def get_workspace_ui_contribution_details(self):
        """Returns sidebar workspace navigation details from canonical UI contributions."""
        return self.get_automation_details()

    def get_automation_manifest(self):
        """Returns a list of details for all loaded automations, including their prompts."""
        manifest = []
        for instance in self.automations.values():
            meta = instance.manifest
            prompts = getattr(instance, "automation_prompts", {})
            if not prompts:
                prompts = {}
                for prompt in self.registries.prompts:
                    if prompt.addon_id != instance.id or not prompt.content:
                        continue
                    if prompt.prompt_type != "automation":
                        continue
                    prompt_id = prompt.id.split(f"{instance.id}.", 1)[-1]
                    if prompt_id.startswith("automation."):
                        prompt_id = prompt_id.split("automation.", 1)[1]
                    prompts[prompt_id] = prompt.content
            manifest.append(
                {
                    "id": meta["id"],
                    "name": meta["name"],
                    "automation_prompts": prompts,
                }
            )
        return manifest

    def get_helper_details(self):
        """Returns a list of details for all loaded helpers."""
        if self.registries.helpers.records:
            details = [
                item
                for item in self.registries.helpers.get_legacy_details()
                if item.get("view_func")
            ]
            if details:
                return details

        details = []
        for instance in sorted(self.helpers.values(), key=lambda x: x.manifest["name"]):
            meta = instance.manifest
            ui_entry = meta.get("ui_entry_points", {}).get("helper")

            if ui_entry and ui_entry.get("view_func"):
                details.append(
                    {
                        "id": meta["id"],
                        "name": ui_entry.get("name", meta["name"]),
                        "description": ui_entry.get(
                            "description", meta.get("description", "")
                        ),
                        "view_func": ui_entry[
                            "view_func"
                        ],  # The view_func for the modal's content
                        "icon": ui_entry.get("icon", "fa-puzzle-piece"),
                        "addon_id": meta["id"],
                        "css_assets": [f"static/{meta['id']}.css"]
                        if ui_entry.get("css_on_chat_page", False)
                        else [],
                        "js_assets": [f"static/{meta['id']}.js"]
                        if ui_entry.get("load_on_chat_page", False)
                        else [],
                        "load_on_chat_page": ui_entry.get("load_on_chat_page", False),
                        "css_on_chat_page": ui_entry.get("css_on_chat_page", False),
                    }
                )
        return details

    def get_quick_access_details(self):
        """Returns sidebar Quick Access entries declared by UI contribution entrypoints."""
        details = self._quick_access_details_from_ui_contributions()
        if self.registries.ui_contributions.records:
            return details
        return self.get_helper_details()

    def get_quick_access_ui_component_details(self):
        """Returns quick-access UI component launchers, with legacy helpers as fallback."""
        return self.get_quick_access_details()

    def _quick_access_details_from_ui_contributions(self):
        details = []
        seen_components = set()

        for contribution in sorted(
            self.registries.ui_contributions.list(), key=lambda item: item.title
        ):
            if contribution.status.state not in {ACTIVE, DEGRADED}:
                continue

            entrypoint = next(
                (
                    item
                    for item in contribution.entrypoints
                    if isinstance(item, dict) and item.get("kind") == "quick_access"
                ),
                None,
            )
            if not entrypoint:
                continue

            component = (
                self.registries.helpers.resolve(contribution.component_id)
                if contribution.component_id
                else None
            )
            if not component:
                for helper_id in contribution.required_helpers:
                    component = self.registries.helpers.resolve(helper_id)
                    if component:
                        break
            if not component or component.status.state not in {ACTIVE, DEGRADED}:
                continue
            if component.id in seen_components:
                continue
            seen_components.add(component.id)

            details.append(
                {
                    "id": component.addon_id,
                    "addon_id": component.addon_id,
                    "stable_id": component.id,
                    "component_id": component.id,
                    "ui_contribution_id": contribution.id,
                    "helper_id": component.helper_id,
                    "name": contribution.title or component.name,
                    "description": contribution.description or component.description,
                    "view_func": component.view_func,
                    "icon": entrypoint.get("icon") or component.icon,
                    "template": component.template,
                    "css_assets": component.css_assets,
                    "js_assets": component.js_assets,
                    "load_on_chat_page": component.load_on_chat_page,
                    "css_on_chat_page": component.css_on_chat_page,
                    "section": entrypoint.get("section", "Quick Access"),
                    "status": asdict(contribution.status),
                    "aliases": component.aliases,
                }
            )

        return sorted(
            details, key=lambda item: (item.get("section") or "", item["name"])
        )

    def get_page_details(self):
        """Returns a list of details for all loaded full-page UIs."""
        contribution_details = self._details_from_ui_contributions(
            entrypoint_kinds={"tools_tab"},
            exclude_kinds={"workspace"},
        )
        if contribution_details:
            return contribution_details

        if self.registries.ui_surfaces.records:
            details = []
            for surface in sorted(
                self.registries.ui_surfaces.list(), key=lambda item: item.name
            ):
                if surface.placement != "page" or not surface.view_func:
                    continue
                if surface.status.state not in {"active", "degraded"}:
                    continue
                details.append(
                    {
                        "id": surface.addon_id,
                        "stable_id": surface.id,
                        "name": surface.name,
                        "view_func": surface.view_func,
                        "icon": surface.icon,
                    }
                )
            if details:
                return details

        details = []
        for instance in sorted(
            self.loaded_addons.values(), key=lambda x: x.manifest["name"]
        ):
            meta = instance.manifest
            ui_entry = meta.get("ui_entry_points", {}).get("page")

            if ui_entry and ui_entry.get("view_func"):
                details.append(
                    {
                        "id": meta["id"],
                        "name": ui_entry.get("name", meta["name"]),
                        "view_func": ui_entry["view_func"],
                        "icon": ui_entry.get("icon", "fa-file"),
                    }
                )
        return details

    def get_page_ui_contribution_details(self):
        """Returns non-workspace page navigation details from canonical UI contributions."""
        return self.get_page_details()

    def _details_from_ui_contributions(
        self,
        entrypoint_kinds: set[str] | None = None,
        contribution_kinds: set[str] | None = None,
        exclude_kinds: set[str] | None = None,
    ):
        details = []
        entrypoint_kinds = entrypoint_kinds or set()
        contribution_kinds = contribution_kinds or set()
        exclude_kinds = exclude_kinds or set()

        for contribution in sorted(
            self.registries.ui_contributions.list(), key=lambda item: item.title
        ):
            if contribution.status.state not in {"active", "degraded"}:
                continue
            if contribution_kinds and contribution.kind not in contribution_kinds:
                continue
            if contribution.kind in exclude_kinds:
                continue
            if entrypoint_kinds:
                kinds = {
                    entrypoint.get("kind")
                    for entrypoint in contribution.entrypoints
                    if isinstance(entrypoint, dict)
                }
                if not kinds.intersection(entrypoint_kinds):
                    continue

            page_surface = next(
                (
                    surface
                    for surface in contribution.surfaces
                    if isinstance(surface, dict)
                    and surface.get("kind") == "page"
                    and surface.get("view_func")
                ),
                None,
            )
            if not page_surface:
                continue
            entrypoint = next(
                (
                    item
                    for item in contribution.entrypoints
                    if isinstance(item, dict)
                    and (not entrypoint_kinds or item.get("kind") in entrypoint_kinds)
                ),
                {},
            )
            active_ids = [contribution.id, contribution.addon_id]
            legacy_active_page = page_surface.get("active_page") or entrypoint.get(
                "active_page"
            )
            if legacy_active_page:
                active_ids.append(legacy_active_page)
            details.append(
                {
                    "id": contribution.addon_id,
                    "addon_id": contribution.addon_id,
                    "stable_id": contribution.id,
                    "kind": contribution.kind,
                    "name": contribution.title,
                    "description": contribution.description,
                    "view_func": page_surface.get("view_func"),
                    "icon": entrypoint.get("icon", "fa-puzzle-piece"),
                    "section": entrypoint.get("section"),
                    "entrypoint_kind": entrypoint.get("kind"),
                    "active_ids": sorted({str(item) for item in active_ids if item}),
                }
            )
        return details

    def get_slash_commands(self):
        """Returns a list of slash commands registered by active addons."""
        commands = [
            {
                "command": "run",
                "label": "run",
                "insert_text": "/run ",
                "description": "Start a durable ad hoc run with planning and provenance.",
                "search_text": "run ad hoc durable planning provenance workflow",
                "is_prompt_passthrough": True,
            }
        ]
        commands.append(
            {
                "command": "sk",
                "label": "sk",
                "insert_text": "/sk ",
                "description": "Find an available reusable skill.",
                "search_text": "sk skill reusable available picker",
                "is_prompt_passthrough": True,
                "skill_picker": True,
            }
        )
        for instance in self.loaded_addons.values():
            cmd_list = instance.manifest.get("slash_commands", [])
            for cmd in cmd_list:
                cmd_copy = cmd.copy()
                cmd_copy["helper_id"] = instance.id
                cmd_copy.setdefault("ui_component_id", instance.id)
                commands.append(cmd_copy)

        # Retrieve slash commands from ui_contributions entrypoints
        for contribution in self.registries.ui_contributions.list():
            if contribution.status.state not in {ACTIVE, DEGRADED}:
                continue
            for entrypoint in contribution.entrypoints:
                if (
                    isinstance(entrypoint, dict)
                    and entrypoint.get("kind") == "slash_command"
                ):
                    cmd_name = entrypoint.get("command")
                    if not cmd_name:
                        continue
                    existing = next(
                        (
                            command
                            for command in commands
                            if command.get("command") == cmd_name
                        ),
                        None,
                    )
                    canonical_fields = {
                        "is_inline": contribution.kind == "picker",
                        "helper_id": contribution.addon_id,
                        "ui_component_id": contribution.component_id,
                        "workflow_id": contribution.workflow_id,
                    }
                    # Explicit UI contributions are authoritative for how a
                    # legacy slash command opens. Merge their canonical
                    # component ID instead of leaving the addon ID behind.
                    if existing is not None:
                        existing.update(canonical_fields)
                        existing.setdefault(
                            "insert_text",
                            f"/{cmd_name} ",
                        )
                        continue
                    commands.append(
                        {
                            "command": cmd_name,
                            "label": cmd_name,
                            "insert_text": f"/{cmd_name} ",
                            "description": contribution.description
                            or contribution.title
                            or f"Search {cmd_name} inline",
                            "search_text": f"{cmd_name} {contribution.title} {contribution.description}",
                            **canonical_fields,
                        }
                    )

        commands.extend(self.registries.skills.slash_commands())
        commands.extend(self.registries.workflows.slash_commands())
        return commands

    def get_plugin_manifest(self):
        """Returns a list of details for all loaded plugins that have tools."""
        manifest = []
        for instance in self.plugins.values():
            meta = instance.manifest
            tool_records = [
                tool
                for tool in self.registries.tools.list()
                if tool.addon_id == instance.id
            ]
            # Only include addons that actually provide tools to the AI.
            if not tool_records:
                if not (hasattr(instance, "get_tools") and instance.get_tools()):
                    continue

            prompt_content = getattr(instance, "plugin_prompt", "")
            explicit_prompts = [
                prompt.content
                for prompt in self.registries.prompts
                if prompt.addon_id == instance.id
                and prompt.content
                and prompt.prompt_type in {"plugin", "toolkit"}
            ]
            if explicit_prompts:
                prompt_content = "\n\n".join(explicit_prompts)

            if not tool_records:
                tool_records = [
                    tool
                    for tool in self.registries.tools.list()
                    if tool.addon_id == instance.id
                ]

            if not tool_records and not (
                hasattr(instance, "get_tools") and instance.get_tools()
            ):
                continue

            manifest.append(
                {
                    "name": meta["name"],
                    "prefix": getattr(instance, "prefix", meta["id"]),
                    "description": meta["description"],
                    "plugin_prompt": prompt_content,
                    "capability_id": f"{instance.id}.toolkit",
                    "tool_ids": [tool.id for tool in tool_records],
                }
            )
        return manifest

    def get_meta_capability_tool_declarations(self) -> list[dict]:
        """Small always-available discovery tools exposed to the chat model."""
        return [
            {
                "type": "function",
                "function": {
                    "name": "capabilities_search",
                    "description": (
                        "Search available capabilities and return compact cards. "
                        "Use for unfamiliar or multi-system tasks to find relevant capability IDs "
                        "before calling capabilities_describe."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "Search term matched against capability name, ID, description, group, and addon ID.",
                            },
                            "groups": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": (
                                    "Filter by capability group. Pass exact group names only "
                                    "(e.g. 'Toolkits', 'Skills', 'Workflows', 'Agent Profiles', 'Core')."
                                ),
                            },
                            "capability_types": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": (
                                    "Filter by capability type. Actionable types (can prepare tools): "
                                    "'toolkit'. Slash-command types: 'skill', 'workflow'. "
                                    "UI/plumbing only (no tools): 'ui_component', 'helper', "
                                    "'ui_contribution', 'workspace', 'provider', 'agent_profile', 'core'."
                                ),
                            },
                            "limit": {
                                "type": "integer",
                                "default": CAPABILITY_SEARCH_DEFAULT_LIMIT,
                                "description": (
                                    "Maximum compact capability cards to return. Defaults to 5 and is capped at 5 "
                                    "to avoid context bloat; use capabilities_describe for shortlisted details."
                                ),
                            },
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "capabilities_runtime_inventory",
                    "description": (
                        "Return a compact planning inventory for a task: candidate capabilities, common tool "
                        "affordance cards, policy profiles, and execution path hints. Use before opening full "
                        "schemas when a request may involve tools, delegation, a run, or code in the loop."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "task": {
                                "type": "string",
                                "maxLength": 4096,
                                "description": "Short description of the user's task to rank relevant capabilities.",
                            },
                            "capability_ids": {
                                "type": "array",
                                "maxItems": 20,
                                "items": {"type": "string", "maxLength": 128},
                                "description": "Optional known capability IDs to inventory directly.",
                            },
                            "max_capabilities": {
                                "type": "integer",
                                "default": 8,
                                "description": "Maximum candidate capabilities to include (1-20).",
                            },
                            "max_common_tasks": {
                                "type": "integer",
                                "default": 20,
                                "description": "Maximum compact tool affordance cards to include (1-50).",
                            },
                        },
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "capabilities_source_coverage",
                    "description": (
                        "Return a compact coverage table for requested source classes such as email, "
                        "Google Drive, local files, web, WordPress, documents, or images. Use this before "
                        "broad search for multi-source tasks, and before declaring a requested source unavailable. "
                        "When coverage is sufficient, use recommended describe IDs instead of broad search."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "source_classes": {
                                "type": "array",
                                "maxItems": 20,
                                "items": {"type": "string", "maxLength": 128},
                                "description": (
                                    "Source classes to check, e.g. ['email', 'gdrive', 'local_files', 'web']."
                                ),
                            },
                            "task": {
                                "type": "string",
                                "maxLength": 4096,
                                "description": "Optional task text used to infer source classes when source_classes is empty.",
                            },
                            "max_tools_per_source": {
                                "type": "integer",
                                "default": 6,
                                "description": "Maximum compact read/write tool cards per source class (1-20).",
                            },
                            "include_tool_cards": {
                                "type": "boolean",
                                "default": False,
                                "description": (
                                    "If true, include sampled read/write tool cards for a single source class. "
                                    "Multi-source coverage stays compact; use capabilities_describe for details."
                                ),
                            },
                        },
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "capabilities_describe",
                    "description": (
                        "Return operational details for selected capabilities: tool names, side-effects, "
                        "approval requirements, and guidance summaries. "
                        "Set prepare_tools=true to also bind the capability's tools for inline use "
                        "in the current turn (toolkit capabilities only)."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "capability_ids": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Capability IDs to describe (e.g. ['googledrive.toolkit']).",
                            },
                            "prepare_tools": {
                                "type": "boolean",
                                "description": (
                                    "If true, bind a budgeted operation group from the capability for direct inline use "
                                    "in this turn. Only effective for toolkit capabilities."
                                ),
                            },
                            "operation_groups": {
                                "type": "array",
                                "items": {
                                    "type": "string",
                                    "enum": [
                                        "read",
                                        "search",
                                        "list",
                                        "preview",
                                        "write",
                                        "create",
                                        "update",
                                        "publish",
                                        "delete",
                                        "all",
                                    ],
                                },
                                "description": (
                                    "Filter described tools to these operations and, when prepare_tools is true, "
                                    "bind the same narrow group. Prefer the narrowest group that satisfies the request."
                                ),
                            },
                            "max_tools": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 12,
                                "default": 6,
                                "description": "Maximum selected schemas to bind; the runtime also enforces a token budget.",
                            },
                        },
                        "required": ["capability_ids"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "capabilities_get_guidance",
                    "description": (
                        "Fetch full guidance content for selected capabilities or guidance IDs. "
                        "Use only after search, runtime inventory, or describe identifies relevant guidance."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "capability_ids": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Capability IDs whose guidance should be fetched.",
                            },
                            "guidance_ids": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Specific guidance IDs to fetch.",
                            },
                            "task": {
                                "type": "string",
                                "description": "Optional task text used as a relevance filter.",
                            },
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "capabilities_load_to_session",
                    "description": (
                        "Dynamically registers and injects the full tool schemas of specified capabilities "
                        "into the current active chat session loop. Use this for Path A (Inline Execution)."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "capability_ids": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Capability IDs to load into the session (e.g. ['googledrive.toolkit']).",
                            },
                        },
                        "required": ["capability_ids"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "runs_upgrade_session_to_run",
                    "description": (
                        "Converts the current inline chat session into a stateful, database-backed Workflow Run. "
                        "Use this if an inline execution path unexpectedly scales in volume or complexity."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "reason": {
                                "type": "string",
                                "description": "Reason for upgrading to a stateful run.",
                            },
                        },
                        "required": ["reason"],
                    },
                },
            },
        ]

    def execute_meta_capability_tool(
        self, name: str, arguments: dict | None = None
    ) -> dict:
        arguments = arguments or {}
        if name == "capabilities_search":
            return self.search_capabilities_for_model(
                query=arguments.get("query", ""),
                groups=arguments.get("groups") or [],
                capability_types=arguments.get("capability_types") or [],
                limit=arguments.get("limit"),
            )
        if name == "capabilities_runtime_inventory":
            return self.get_runtime_inventory(
                task=arguments.get("task", ""),
                capability_ids=arguments.get("capability_ids") or [],
                max_capabilities=arguments.get("max_capabilities", 8),
                max_common_tasks=arguments.get("max_common_tasks", 20),
                runtime_policy=arguments.get("_runtime_policy"),
            )
        if name == "capabilities_source_coverage":
            requested_tool_cards = bool(arguments.get("include_tool_cards", False))
            result = self.get_source_coverage(
                source_classes=arguments.get("source_classes") or [],
                task=arguments.get("task", ""),
                max_tools_per_source=arguments.get("max_tools_per_source", 6),
                include_tool_cards=requested_tool_cards,
                runtime_policy=arguments.get("_runtime_policy"),
            )
            include_tool_cards = bool(
                requested_tool_cards and not result.get("detail_suppressed")
            )
            result["requested_include_tool_cards"] = requested_tool_cards
            result["include_tool_cards_effective"] = include_tool_cards
            if requested_tool_cards and not include_tool_cards:
                result["detail_limited_reason"] = (
                    "Detailed source tool cards were omitted because this is a multi-source coverage call. "
                    "Use recommended_describe_capability_ids for targeted details after selecting a path."
                )
            return result
        if name == "capabilities_describe":
            operation_groups = arguments.get("operation_groups") or (
                ["read"] if arguments.get("prepare_tools") else []
            )
            return {
                "capabilities": self.describe_capabilities(
                    arguments.get("capability_ids") or [],
                    operation_groups=operation_groups,
                    max_tools=arguments.get("max_tools", 6),
                )
            }
        if name == "capabilities_get_guidance":
            return {
                "guidance": self.get_guidance(
                    capability_ids=arguments.get("capability_ids") or [],
                    guidance_ids=arguments.get("guidance_ids") or [],
                    task=arguments.get("task", ""),
                )
            }
        if name == "capabilities_load_to_session":
            return {
                "_instruction": "load_to_session",
                "capability_ids": arguments.get("capability_ids") or [],
            }
        if name == "runs_upgrade_session_to_run":
            return {
                "_instruction": "upgrade_session",
                "reason": arguments.get("reason", ""),
            }
        raise KeyError(f"Unknown meta-capability tool '{name}'.")

    def format_capability_protocol_for_prompt(self) -> str:
        groups = sorted(
            {c.group for c in self.registries.capabilities.list() if c.group}
        )
        groups_str = ", ".join(f"'{g}'" for g in groups)
        return (
            "Capability discovery protocol:\\n"
            "1. Plain chat is valid when no external capability is needed.\\n"
            "2. For tool-like tasks, call `capabilities_runtime_inventory` with a short task summary first; "
            "it returns compact affordance cards without full schemas. If `common_tasks` proves the exact "
            "function name, risk, and required arguments for a planning-only or simple read path, stop "
            "discovery and answer or load only that selected toolkit.\\n"
            "3. For tasks that name multiple data repositories or source classes, call "
            "`capabilities_source_coverage` before broad search and before declaring any requested source unavailable. "
            "If coverage is sufficient, do not follow it with broad search for those same sources; use "
            "`recommended_describe_capability_ids` only when full schemas or write/script details are needed.\\n"
            "4. For unfamiliar tasks needing broader search, call `capabilities_search`, then "
            "`capabilities_describe` only for shortlisted capabilities. Set `prepare_tools=true` with the "
            "narrowest `operation_groups` when direct execution is needed. `capabilities_search` returns "
            "compact cards only: 5 by default and at most 5, with no full schemas or long guidance.\\n"
            "5. Exact function names in plans must come from source-coverage common task cards, "
            "`capabilities_describe`, or loaded tool schemas. Otherwise mark them as proposals/gaps.\\n"
            "6. For code-in-the-loop, durable-run runtime, runtime-profile, project-workspace, or tool-batch "
            "questions, you MUST call `capabilities_runtime_inventory` before answering. Do not name exact "
            "runtime tools, toolkit functions, or view ids for those tasks until inventory or loaded schemas "
            "ground them. Treat code as executable only when a runtime profile explicitly says the runtime is available. "
            "Use `runs_execute_python` for one-shot standard-library transforms. Use the `python.session` profile "
            "for maintained data/document dependencies, declared resources, resumable state, artifacts, selected "
            "host tools, or bounded agent mapping; its generated code never receives connector credentials. "
            "Normalized call lists outside a session can still use `runs_execute_tool_batch`. For software "
            "projects, use the permission-gated `project.workspace` profile rather than treating scratch "
            "Python as a terminal.\\n"
            "7. Evaluate the Complexity Matrix:\\n"
            "   - **Path A (Inline)**: If the task is simple, read-only, or sequential within a single system, "
            "call `capabilities_load_to_session` and execute the tools directly in the chat turn. "
            "Do not use DAG planning for this.\\n"
            "   - **Path B (Orchestrated)**: If the task involves multi-agent delegation, parallel Map-Reduce, "
            "background scheduling, or high-risk writes needing multi-step approvals, call `runs_compile` "
            "once with the run intent, plan, steps, policy, checkpoints, and start/preview preference. "
            "Use `runs_create`/`runs_add_step` only for incremental construction or repair.\\n"
            "   - If you are acting via Path A but the volume or complexity scales unexpectedly, "
            "call `runs_upgrade_session_to_run` to hand off the remainder to the background queue.\\n"
            f"Available groups: {groups_str}.\\n"
            "Types 'skill' and 'workflow' should be delegated via `runs.delegate_skill` or `/skill`."
        )

    def get_tool_declarations_for_capability_ids(
        self,
        capability_ids: list[str] | str | None,
        config: dict | None = None,
        *,
        include_side_effect_tools: bool = True,
    ) -> list[dict]:
        """Returns executable tool declarations linked to selected capabilities."""
        if isinstance(capability_ids, str):
            capability_ids = [capability_ids]
        config = config or {}
        tool_ids = []
        seen = set()
        addon_ids_to_initialize = set()

        for capability_id in capability_ids or []:
            capability = self.get_capability_record(str(capability_id))
            if not capability or capability.status.state not in {ACTIVE, DEGRADED}:
                continue
            for element_id in capability.element_ids:
                record = self.get_tool_record(element_id)
                if not record or record.status.state not in {ACTIVE, DEGRADED}:
                    continue
                if not include_side_effect_tools and not tool_is_read_only(record):
                    continue
                if record.id in seen:
                    continue
                seen.add(record.id)
                tool_ids.append(record.id)
                addon_ids_to_initialize.add(record.addon_id)

        available_tool_ids = list(tool_ids)
        for addon_id in sorted(addon_ids_to_initialize):
            instance = self.loaded_addons.get(addon_id) or self.plugins.get(addon_id)
            if not instance:
                available_tool_ids = [
                    tool_id
                    for tool_id in available_tool_ids
                    if not (
                        self.get_tool_record(tool_id)
                        and self.get_tool_record(tool_id).addon_id == addon_id
                    )
                ]
                continue
            try:
                self._initialize_addon_client(instance, config)
            except Exception:
                logging.exception(
                    "Error preparing direct tools for addon '%s'. It will be unavailable for this request.",
                    getattr(instance, "manifest", {}).get("name", addon_id),
                )
                available_tool_ids = [
                    tool_id
                    for tool_id in available_tool_ids
                    if not (
                        self.get_tool_record(tool_id)
                        and self.get_tool_record(tool_id).addon_id == addon_id
                    )
                ]

        return self.registries.tools.generate_tool_declarations(available_tool_ids)

    def _capability_card(self, capability) -> dict:
        status_state = capability.status.state
        if status_state not in {ACTIVE, DEGRADED}:
            next_action = "unavailable"
        elif capability.capability_type == "core":
            next_action = "use_declared_tool"
        elif capability.capability_type == "tool_view":
            next_action = "load_view"
        else:
            next_action = "describe"
        card = {
            "id": capability.id,
            "name": capability.name,
            "label": capability.label or capability.name,
            "description": capability.description,
            "group": capability.group,
            "capability_type": capability.capability_type,
            "source_type": self._capability_source_type(capability),
            "selection_mode": capability.selection_mode,
            "next_action": next_action,
            "source_addon_id": capability.source_addon_id or capability.addon_id,
            "status": asdict(capability.status),
            "risk": self._capability_risk_class(capability),
            "common_calls": self._capability_common_function_names(capability),
            "degraded_reasons": list(capability.status.reasons),
            "launch": capability.launch,
        }
        if capability.guidance_ids:
            card["guidance_ids"] = list(capability.guidance_ids)
        return card

    def _capability_source_type(self, capability) -> str:
        text = " ".join(
            [
                str(getattr(capability, "id", "") or ""),
                str(getattr(capability, "name", "") or ""),
                str(getattr(capability, "description", "") or ""),
                str(getattr(capability, "addon_id", "") or ""),
                str(getattr(capability, "source_addon_id", "") or ""),
                str(getattr(capability, "group", "") or ""),
                str(getattr(capability, "capability_type", "") or ""),
            ]
        ).lower()
        if any(
            token in text for token in ("gmail", "email", "mailbox", "imap", "smtp")
        ):
            return "email"
        if any(
            token in text
            for token in (
                "gdrive",
                "google drive",
                "googledrive",
                "docs",
                "sheets",
                "slides",
            )
        ):
            return "drive"
        if any(
            token in text
            for token in ("wordpress", "wp ", "taxonomy", "posts", "pages")
        ):
            return "app"
        if any(token in text for token in ("local", "filesystem", "folder", "file")):
            return "local"
        if any(token in text for token in ("web", "scrape", "browser", "internet")):
            return "web"
        if any(token in text for token in ("document", "pdf", "docx", "pptx", "xlsx")):
            return "document"
        if any(token in text for token in ("model", "agent", "llm")):
            return "model"
        if "run" in text or "workflow" in text:
            return "run"
        if getattr(capability, "capability_type", ""):
            return str(capability.capability_type)
        return "other"

    def _capability_common_function_names(
        self, capability, limit: int = 6
    ) -> list[str]:
        names = []
        for element_id in capability.element_ids:
            tool = self.registries.tools.resolve(element_id)
            if not tool or tool.status.state not in {ACTIVE, DEGRADED}:
                continue
            function_name, _source = self._tool_function_name_and_source(tool)
            if function_name and function_name not in names:
                names.append(function_name)
            if len(names) >= limit:
                break
        return names

    def _capability_risk_class(self, capability) -> str:
        saw_read = False
        saw_write = False
        saw_external_write = False
        saw_destructive = False
        for element_id in capability.element_ids:
            tool = self.registries.tools.resolve(element_id)
            if not tool or tool.status.state not in {ACTIVE, DEGRADED}:
                continue
            risk_class = self._tool_risk_class(tool)
            effects = {str(effect).lower() for effect in tool.side_effects or []}
            if risk_class == "read":
                saw_read = True
            elif risk_class == "write":
                saw_write = True
                if effects & {"external_write", "publish", "external_publish"}:
                    saw_external_write = True
            else:
                saw_destructive = True
        if saw_destructive:
            return "destructive"
        if saw_external_write:
            return "writes_external"
        if saw_write:
            return "writes_internal"
        if saw_read:
            return "read_only"
        return "unknown"

    def _slim_capability_description(self, capability) -> dict:
        """Returns a model-optimized slim description of a capability, stripping internal plumbing fields."""
        base = {
            "id": capability.id,
            "name": capability.name,
            "description": capability.description,
            "capability_type": capability.capability_type,
            "group": capability.group,
            "selection_mode": capability.selection_mode,
            "status": capability.status.state,
        }
        if capability.status.reasons:
            base["status_reasons"] = list(capability.status.reasons)
        guidance = self._guidance_summaries(capability.guidance_ids)
        if guidance:
            base["guidance"] = guidance

        elements: dict = {}
        for element_id in capability.element_ids:
            self._append_slim_element(elements, element_id)
        # Only include non-empty element buckets
        base.update({k: v for k, v in elements.items() if v})

        return base

    def _guidance_summaries(self, guidance_ids: list[str]) -> list[dict]:
        if not guidance_ids:
            return []
        guidance_by_id = {
            record.id: record for record in getattr(self.registries, "guidance", [])
        }
        summaries = []
        for guidance_id in guidance_ids:
            record = guidance_by_id.get(guidance_id)
            if not record or record.status.state not in {ACTIVE, DEGRADED}:
                continue
            summaries.append(
                {
                    "id": record.id,
                    "title": record.title,
                    "type": record.guidance_type,
                    "summary": record.summary,
                }
            )
        return summaries

    def _common_task_cards_for_capabilities(
        self, capabilities: list, limit: int
    ) -> list[dict]:
        cards = []
        seen_tool_ids = set()
        for capability in capabilities:
            if capability.status.state not in {ACTIVE, DEGRADED}:
                continue
            for element_id in capability.element_ids:
                tool = self.registries.tools.resolve(element_id)
                if (
                    not tool
                    or tool.id in seen_tool_ids
                    or tool.status.state not in {ACTIVE, DEGRADED}
                ):
                    continue
                seen_tool_ids.add(tool.id)
                cards.append(self._tool_affordance_card(tool, capability))
                if len(cards) >= limit:
                    return cards
        return cards

    def _tool_function_name_and_source(self, tool) -> tuple[str, str]:
        function = (
            (tool.function_schema.get("function") or {}) if tool.function_schema else {}
        )
        if function.get("name"):
            return function["name"], "function_schema"
        if tool.legacy_name:
            source = (
                "manifest.function_name"
                if tool.source == "explicit.tools"
                else "legacy_tool_declaration"
            )
            return tool.legacy_name, source
        return tool.id.replace(".", "_"), "derived_tool_id"

    def _tool_view_cards_for_tool(self, tool) -> list[dict]:
        cards = []
        for view in self.registries.tools.tool_views.values():
            if view.tool_id != tool.id or view.status.state not in {ACTIVE, DEGRADED}:
                continue
            code_callable = bool(getattr(view, "code_callable", False))
            cards.append(
                {
                    "id": view.id,
                    "name": view.name or view.id,
                    "allowed_parameters": list(view.allowed_parameters),
                    "description": view.description,
                    "code_callable": code_callable,
                    "code_callable_status": "code_callable"
                    if code_callable
                    else "model_tool_view_only",
                    "batch_policy": getattr(view, "batch_policy", "serial") or "serial",
                    "idempotency": getattr(view, "idempotency", "unknown") or "unknown",
                    "limits": dict(getattr(view, "limits", {}) or {}),
                }
            )
        return cards

    def _tool_affordance_card(self, tool, capability) -> dict:
        function = (
            (tool.function_schema.get("function") or {}) if tool.function_schema else {}
        )
        function_name, function_name_source = self._tool_function_name_and_source(tool)
        description = function.get("description") or tool.name
        input_schema = function.get("parameters") or tool.input_schema or {}
        properties = (
            input_schema.get("properties") if isinstance(input_schema, dict) else {}
        )
        properties = properties or {}
        required_args = (
            list(input_schema.get("required") or [])
            if isinstance(input_schema, dict)
            else []
        )
        optional_args = [name for name in properties if name not in set(required_args)]
        tool_views = self._tool_view_cards_for_tool(tool)

        operation = self._infer_tool_operation(tool, function_name)
        risk_class = self._tool_risk_class(tool)
        has_array_input = any(
            isinstance(schema, dict) and schema.get("type") == "array"
            for schema in properties.values()
        )
        supports_batch = (
            has_array_input
            or operation in {"search", "list"}
            or any(
                name in properties
                for name in ("limit", "max_results", "page_size", "query")
            )
        )
        code_callable_views = [view for view in tool_views if view.get("code_callable")]
        callable_from_code = bool(code_callable_views)
        batch_policy = (
            code_callable_views[0].get("batch_policy")
            if code_callable_views
            else "native_batch_or_paging"
            if supports_batch
            else "serial"
        )

        return {
            "id": f"{capability.id}:{tool.id}",
            "capability_id": capability.id,
            "tool_id": tool.id,
            "function_name": function_name,
            "function_name_source": function_name_source,
            "provenance": "runtime_inventory.common_tasks",
            "tool_name_provenance": "runtime_inventory.common_tasks",
            "may_name_function": True,
            "label": tool.name,
            "description": description,
            "operation": operation,
            "risk_class": risk_class,
            "side_effects": list(tool.side_effects or []),
            "approval_required": bool(tool.approval_required),
            "requires_approval_or_review": bool(
                tool.approval_required or risk_class in {"write", "high"}
            ),
            "required_args": required_args,
            "optional_args": optional_args[:12],
            "argument_names": list(properties.keys())[:20],
            "argument_count": len(properties),
            "schema_state": "summary_only",
            "full_schema_available_via": [
                "capabilities_describe",
                "capabilities_load_to_session",
            ],
            "supports_batch_or_paging": supports_batch,
            "batch_policy": batch_policy,
            "callable_from_code": callable_from_code,
            "code_callable_status": "code_callable_tool_view_available"
            if callable_from_code
            else "not_code_callable",
            "tool_views": tool_views,
            "planning_hint": self._tool_planning_hint(
                operation, risk_class, supports_batch
            ),
        }

    def _infer_tool_operation(self, tool, function_name: str) -> str:
        raw_text = f"{tool.id} {tool.name} {function_name}"
        boundary_text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", raw_text)
        tokens = set(CAPABILITY_TOKEN_RE.findall(boundary_text.lower()))
        checks = [
            ("delete", ("delete", "remove", "trash")),
            ("publish", ("publish", "send")),
            ("update", ("update", "edit", "patch", "set", "write")),
            ("create", ("create", "add", "insert", "upload")),
            ("export", ("export", "download")),
            ("search", ("search", "find", "query")),
            ("list", ("list", "browse")),
            ("read", ("get", "read", "fetch", "retrieve", "inspect", "preview")),
        ]
        for operation, operation_tokens in checks:
            if tokens.intersection(operation_tokens):
                return operation
        return "call"

    def _tool_risk_class(self, tool) -> str:
        return classify_tool_risk(tool)

    def _tool_planning_hint(
        self, operation: str, risk_class: str, supports_batch: bool
    ) -> str:
        if risk_class == "read" and supports_batch:
            return "Good candidate for discovery, batching, and parallel read workers."
        if risk_class == "read":
            return (
                "Safe candidate for direct inline use or delegated read-only subtasks."
            )
        if risk_class == "write":
            return "Use after validation; consider review checkpoints before applying many changes."
        return "Use only with an explicit policy/approval decision and validation before external side effects."

    def _append_slim_element(self, elements: dict, element_id: str) -> None:
        """Appends a slim, model-readable summary of an element to the appropriate bucket."""
        tool = self.registries.tools.resolve(element_id)
        if tool:
            fn_meta = (
                (tool.function_schema.get("function") or {})
                if tool.function_schema
                else {}
            )
            function_name, _function_name_source = self._tool_function_name_and_source(
                tool
            )
            input_schema = fn_meta.get("parameters") or tool.input_schema or {}
            properties = (
                input_schema.get("properties") if isinstance(input_schema, dict) else {}
            )
            properties = properties or {}
            required_args = (
                list(input_schema.get("required") or [])
                if isinstance(input_schema, dict)
                else []
            )
            optional_args = [
                name for name in properties if name not in set(required_args)
            ]
            operation = self._infer_tool_operation(tool, function_name)
            risk_class = self._tool_risk_class(tool)
            has_array_input = any(
                isinstance(schema, dict) and schema.get("type") == "array"
                for schema in properties.values()
            )
            supports_batch = (
                has_array_input
                or operation in {"search", "list"}
                or any(
                    name in properties
                    for name in ("limit", "max_results", "page_size", "query")
                )
            )
            tool_views = self._tool_view_cards_for_tool(tool)
            elements.setdefault("tools", []).append(
                {
                    "id": tool.id,
                    "tool_id": tool.id,
                    "function_name": function_name,
                    "tool_name_provenance": "capabilities_describe",
                    "may_name_function": True,
                    "name": tool.name,
                    "description": self._bounded_text(
                        fn_meta.get("description") or tool.name, 180
                    ),
                    "operation": operation,
                    "risk_class": risk_class,
                    "approval_required": bool(tool.approval_required),
                    "required_args": required_args,
                    "optional_args": optional_args[:10],
                    "schema_state": "summary_only",
                    "supports_batch_or_paging": supports_batch,
                    "tool_views": [
                        {
                            "id": view.get("id"),
                            "allowed_parameters": view.get("allowed_parameters") or [],
                            "code_callable": bool(view.get("code_callable")),
                        }
                        for view in tool_views[:4]
                    ],
                }
            )
            return
        skill = self.registries.skills.resolve(element_id)
        if skill:
            elements.setdefault("skills", []).append(
                {
                    "id": skill.id,
                    "name": skill.name,
                    "description": skill.description,
                }
            )
            return
        workflow = self.registries.workflows.resolve(element_id)
        if workflow:
            entry: dict = {
                "id": workflow.id,
                "name": workflow.name,
                "description": workflow.description,
                "dry_run_supported": workflow.dry_run_supported,
            }
            if workflow.launch_url:
                entry["launch_url"] = workflow.launch_url
            elements.setdefault("workflows", []).append(entry)
            return
        provider = self.registries.providers.get(element_id)
        if provider:
            elements.setdefault("providers", []).append(
                {
                    "id": provider.id,
                    "name": provider.name,
                    "operations": provider.operations,
                }
            )
            return
        helper = self.registries.helpers.resolve(element_id)
        if helper:
            elements.setdefault("ui_components", []).append(
                {
                    "id": helper.id,
                    "name": helper.name,
                    "description": helper.description,
                }
            )
            return
        contribution = self.registries.ui_contributions.get(element_id)
        if contribution:
            elements.setdefault("ui_contributions", []).append(
                {
                    "id": contribution.id,
                    "name": contribution.title,
                    "description": contribution.description,
                    "kind": contribution.kind,
                }
            )
            return
        agent_profile = self.registries.agent_profiles.get(element_id)
        if agent_profile:
            entry: dict = {
                "id": agent_profile.id,
                "name": agent_profile.name,
                "description": agent_profile.description,
            }
            if agent_profile.model_class:
                entry["model_class"] = agent_profile.model_class
            elements.setdefault("agent_profiles", []).append(entry)

    def get_toolkit_capability_id(self, plugin_id_or_prefix: str) -> str | None:
        """Resolves a toolkit capability id from a legacy plugin id or prefix."""
        if not plugin_id_or_prefix:
            return None
        value = str(plugin_id_or_prefix or "").strip()
        if not value:
            return None

        capability = self.get_capability_record(value)
        if capability and capability.capability_type == "toolkit":
            return capability.id

        candidates = [value]
        if value.endswith(".toolkit"):
            candidates.append(value[: -len(".toolkit")])

        source_toolkit_ids = {
            "email": "email.toolkit",
            "gdrive": "googledrive.toolkit",
            "local_files": "filesystem.toolkit",
            "wordpress": "wordpress.toolkit",
            "documents": "doc_processor.toolkit",
            "images": "openverse.toolkit",
        }
        for candidate in candidates:
            source_class = self._canonical_source_class(candidate)
            capability_id = source_toolkit_ids.get(source_class)
            capability = (
                self.get_capability_record(capability_id) if capability_id else None
            )
            if capability and capability.capability_type == "toolkit":
                return capability.id

        plugin = next(
            (
                self.plugins.get(candidate)
                for candidate in candidates
                if self.plugins.get(candidate)
            ),
            None,
        )
        if plugin is None:
            plugin = next(
                (
                    instance
                    for instance in self.plugins.values()
                    if any(
                        getattr(instance, "prefix", getattr(instance, "id", None))
                        == candidate
                        or getattr(instance, "id", None) == candidate
                        for candidate in candidates
                    )
                ),
                None,
            )
        if plugin is None:
            compact_candidates = {
                _compact_scope_alias(candidate) for candidate in candidates
            }
            plugin = next(
                (
                    instance
                    for instance in self.plugins.values()
                    if compact_candidates
                    & {
                        _compact_scope_alias(getattr(instance, "prefix", "")),
                        _compact_scope_alias(getattr(instance, "id", "")),
                        _compact_scope_alias(getattr(instance, "name", "")),
                        _compact_scope_alias(
                            getattr(instance, "manifest", {}).get("name", "")
                            if isinstance(getattr(instance, "manifest", {}), dict)
                            else ""
                        ),
                    }
                ),
                None,
            )
        if plugin is None:
            return None

        capability = self.get_capability_record(f"{plugin.id}.toolkit")
        return (
            capability.id
            if capability and capability.capability_type == "toolkit"
            else f"{plugin.id}.toolkit"
        )

    def normalize_toolkit_capability_ids(self, selected_values) -> list[str]:
        """Normalizes direct-mode toolkit selection to stable capability ids."""
        if isinstance(selected_values, str):
            selected_values = [selected_values]

        normalized = []
        seen = set()
        for raw_value in selected_values or []:
            value = str(raw_value or "").strip()
            if not value:
                continue

            capability = self.get_capability_record(value)
            capability_id = None
            if capability and capability.capability_type == "toolkit":
                capability_id = capability.id
            else:
                capability_id = self.get_toolkit_capability_id(value)

            if not capability_id or capability_id in seen:
                continue
            seen.add(capability_id)
            normalized.append(capability_id)
        return normalized

    def normalize_capability_ids(self, selected_values) -> list[str]:
        """Normalizes selected capability ids while preserving non-toolkit capabilities."""
        if isinstance(selected_values, str):
            selected_values = [selected_values]

        normalized = []
        seen = set()
        for raw_value in selected_values or []:
            value = str(raw_value or "").strip()
            if not value:
                continue

            capability = self.get_capability_record(value)
            capability_id = (
                capability.id if capability else self.get_toolkit_capability_id(value)
            )
            if not capability_id or capability_id in seen:
                continue
            seen.add(capability_id)
            normalized.append(capability_id)
        return normalized

    def resolve_execution_scope_ids(self, capability_ids) -> dict[str, list[str]]:
        """Expands selected capabilities into the concrete scope ids they imply."""
        resolved = {
            "skill_ids": [],
            "workflow_ids": [],
            "tool_ids": [],
            "tool_view_ids": [],
            "resource_reference_ids": [],
            "runtime_profile_ids": [],
            "model_profile_ids": [],
        }

        def add(key: str, values):
            merged = merge_unique(resolved.get(key), values)
            resolved[key] = merged

        for capability_id in self.normalize_capability_ids(capability_ids):
            capability = self.get_capability_record(capability_id)
            if not capability or capability.status.state not in {ACTIVE, DEGRADED}:
                continue

            element_ids = list(capability.element_ids or [])
            if capability.capability_type == "skill":
                skills = [
                    element_id
                    for element_id in element_ids
                    if self.registries.skills.resolve(element_id)
                ]
                add("skill_ids", skills)
            elif capability.capability_type == "workflow":
                workflows = [
                    element_id
                    for element_id in element_ids
                    if self.registries.workflows.resolve(element_id)
                ]
                add("workflow_ids", workflows)
            elif capability.capability_type == "agent_profile":
                profile = (
                    self.registries.agent_profiles.get(element_ids[0])
                    if element_ids
                    else None
                )
                if profile:
                    add("skill_ids", profile.skill_ids)
                    add("workflow_ids", profile.workflow_ids)
                    add("tool_ids", profile.tool_ids)

            for element_id in element_ids:
                tool = self.registries.tools.resolve(element_id)
                if tool and tool.status.state in {ACTIVE, DEGRADED}:
                    add("tool_ids", [tool.id])
                    continue

                view = self.registries.tools.tool_views.get(element_id)
                if view and view.status.state in {ACTIVE, DEGRADED}:
                    add("tool_view_ids", [view.id])
                    add("tool_ids", [view.tool_id])
                    continue

                skill = self.registries.skills.resolve(element_id)
                if skill and skill.status.state in {ACTIVE, DEGRADED}:
                    add("skill_ids", [skill.id])
                    add("tool_ids", skill.tool_ids or skill.required_tools)
                    add(
                        "tool_view_ids",
                        skill.tool_view_ids or skill.required_tool_views,
                    )
                    continue

                workflow = self.registries.workflows.resolve(element_id)
                if workflow and workflow.status.state in {ACTIVE, DEGRADED}:
                    add("workflow_ids", [workflow.id])
                    add("tool_ids", workflow.required_tools)
                    add("tool_view_ids", workflow.required_tool_views)

        return resolved

    def resolve_toolkit_plugin_ids(self, capability_ids) -> list[str]:
        """Resolves stable toolkit capability ids back to active legacy plugin prefixes."""
        plugin_ids = []
        seen = set()
        for capability_id in self.normalize_toolkit_capability_ids(capability_ids):
            capability = self.get_capability_record(capability_id)
            if not capability or capability.capability_type != "toolkit":
                continue

            plugin = self.plugins.get(capability.addon_id)
            if plugin is None:
                continue

            plugin_id = getattr(plugin, "prefix", plugin.id)
            if plugin_id in seen:
                continue
            seen.add(plugin_id)
            plugin_ids.append(plugin_id)
        return plugin_ids

    def normalize_assistant_selection(self, assistant_config: dict | None) -> dict:
        """Normalizes assistant selection to stable scope ids."""
        config = assistant_config if isinstance(assistant_config, dict) else {}
        selected_values = (
            config.get("capability_ids")
            or config.get("capabilities")
            or config.get("plugins")
            or []
        )
        capability_ids = self.normalize_capability_ids(selected_values)
        scope_from_capabilities = self.resolve_execution_scope_ids(capability_ids)
        selected_tool_view_ids = string_list(
            config.get("tool_view_ids") or config.get("tool_views") or []
        )
        selected_tool_ids = string_list(
            config.get("tool_ids") or config.get("selected_tool_ids") or []
        )
        for view_id in selected_tool_view_ids:
            view = self.registries.tools.tool_views.get(view_id)
            if view and view.tool_id:
                selected_tool_ids = merge_unique(selected_tool_ids, [view.tool_id])
        mode = (
            "direct"
            if normalize_scope_mode(config.get("mode")) == SCOPED
            else "default"
        )
        return {
            "mode": mode,
            "capability_ids": capability_ids,
            "plugin_ids": self.resolve_toolkit_plugin_ids(capability_ids),
            "skill_ids": merge_unique(
                config.get("skill_ids") or config.get("skills") or [],
                scope_from_capabilities["skill_ids"],
            ),
            "workflow_ids": merge_unique(
                config.get("workflow_ids") or config.get("workflows") or [],
                scope_from_capabilities["workflow_ids"],
            ),
            "tool_ids": merge_unique(
                selected_tool_ids, scope_from_capabilities["tool_ids"]
            ),
            "tool_view_ids": merge_unique(
                selected_tool_view_ids, scope_from_capabilities["tool_view_ids"]
            ),
            "resource_reference_ids": string_list(
                config.get("resource_reference_ids") or config.get("resources") or []
            ),
            "runtime_profile_ids": string_list(config.get("runtime_profile_ids") or []),
            "model_profile_ids": string_list(config.get("model_profile_ids") or []),
            "allow_discovery": config.get("allow_discovery", mode == "default")
            is not False
            and mode == "default",
            "allow_scope_expansion_request": config.get(
                "allow_scope_expansion_request", True
            )
            is not False,
            "enable_delegation": config.get("enable_delegation", True) is not False,
        }

    def normalize_session_assistant_selection(
        self, active_capability_ids=None, active_plugins=None
    ) -> dict:
        """Normalizes persisted chat selection, preferring canonical capability ids."""
        selected_values = (
            active_capability_ids
            if active_capability_ids is not None
            else active_plugins
        )
        capability_ids = self.normalize_toolkit_capability_ids(selected_values or [])
        return {
            "active_capability_ids": capability_ids,
            "active_plugins": self.resolve_toolkit_plugin_ids(capability_ids),
        }

    def get_skill_manifest(self, include_inactive: bool = False):
        """Returns discoverable skills from addon packages and the instance skills path."""
        skills = self.registries.skills.as_dicts()
        if include_inactive:
            return skills
        return [
            skill
            for skill in skills
            if skill.get("status", {}).get("state") in {"active", "degraded"}
        ]

    def get_workflow_manifest(self, include_inactive: bool = False):
        """Returns discoverable workflow definitions without claiming full execution support."""
        workflows = self.registries.workflows.as_dicts()
        if include_inactive:
            return workflows
        return [
            workflow
            for workflow in workflows
            if workflow.get("status", {}).get("state") in {"active", "degraded"}
        ]

    def get_tool_manifest(self, include_inactive: bool = False) -> list[dict]:
        """Returns compact registered tool metadata for scoped UI selection."""
        records = self.registries.tools.list()
        tools = []
        for tool in records:
            if not include_inactive and tool.status.state not in {ACTIVE, DEGRADED}:
                continue
            function = (
                (tool.function_schema.get("function") or {})
                if tool.function_schema
                else {}
            )
            function_name = (
                function.get("name") or tool.legacy_name or tool.id.replace(".", "_")
            )
            description = function.get("description") or tool.name
            tool_views = [
                {
                    "id": view.id,
                    "name": view.name or view.id,
                    "description": view.description,
                    "allowed_parameters": list(view.allowed_parameters),
                }
                for view in self.registries.tools.tool_views.values()
                if view.tool_id == tool.id
                and (include_inactive or view.status.state in {ACTIVE, DEGRADED})
            ]
            tools.append(
                {
                    "id": tool.id,
                    "addon_id": tool.addon_id,
                    "name": tool.name,
                    "description": description,
                    "function_name": function_name,
                    "operation": self._infer_tool_operation(tool, function_name),
                    "risk_class": self._tool_risk_class(tool),
                    "side_effects": list(tool.side_effects or []),
                    "approval_required": bool(tool.approval_required),
                    "resource_requirements": list(tool.resource_requirements or []),
                    "tool_view_ids": [view["id"] for view in tool_views],
                    "tool_views": tool_views,
                    "status": asdict(tool.status),
                }
            )
        return sorted(
            tools, key=lambda item: (item["addon_id"], item["name"], item["id"])
        )

    def get_tool_view_manifest(self, include_inactive: bool = False) -> list[dict]:
        """Returns compact tool-view metadata for scoped UI selection."""
        views = []
        for view in self.registries.tools.tool_views.values():
            if not include_inactive and view.status.state not in {ACTIVE, DEGRADED}:
                continue
            tool = self.registries.tools.resolve(view.tool_id)
            if not tool and not include_inactive:
                continue
            if (
                tool
                and not include_inactive
                and tool.status.state not in {ACTIVE, DEGRADED}
            ):
                continue
            views.append(
                {
                    "id": view.id,
                    "tool_id": view.tool_id,
                    "addon_id": tool.addon_id if tool else "",
                    "name": view.name or view.id,
                    "description": view.description,
                    "allowed_parameters": list(view.allowed_parameters),
                    "tool_name": tool.name if tool else view.tool_id,
                    "function_name": (
                        (
                            (tool.function_schema.get("function") or {}).get("name")
                            if tool and tool.function_schema
                            else None
                        )
                        or (tool.legacy_name if tool else None)
                        or view.tool_id.replace(".", "_")
                    ),
                    "status": asdict(view.status),
                }
            )
        return sorted(
            views,
            key=lambda item: (
                item["addon_id"],
                item["tool_name"],
                item["name"],
                item["id"],
            ),
        )

    def get_ui_contribution_manifest(self, include_inactive: bool = False):
        """Returns discoverable user-facing UI workflows/components."""
        contributions = self.registries.ui_contributions.as_dicts()
        if include_inactive:
            return contributions
        return [
            contribution
            for contribution in contributions
            if contribution.get("status", {}).get("state") in {"active", "degraded"}
        ]

    def get_ui_component_manifest(self, include_inactive: bool = False):
        """Returns canonical UI component records declared by addons."""
        components = self.registries.helpers.as_dicts()
        if include_inactive:
            return components
        return [
            component
            for component in components
            if component.get("status", {}).get("state") in {"active", "degraded"}
        ]

    def get_agent_profile_manifest(self, include_inactive: bool = False):
        profiles = self.registries.agent_profiles.as_dicts()
        if include_inactive:
            return profiles
        return [
            profile
            for profile in profiles
            if profile.get("status", {}).get("state") in {"active", "degraded"}
        ]

    def get_skill_record(self, skill_id: str):
        return self.registries.skills.resolve(skill_id)

    def get_workflow_record(self, workflow_id: str):
        return self.registries.workflows.resolve(workflow_id)

    def get_prompt_record(self, prompt_id: str, *, addon_id: str):
        """Resolve one declared prompt within an add-on namespace."""

        normalized_id = str(prompt_id or "").strip()
        normalized_addon_id = str(addon_id or "").strip()
        if not normalized_id or not normalized_addon_id:
            return None
        candidate_ids = [normalized_id]
        if not normalized_id.startswith(f"{normalized_addon_id}."):
            candidate_ids.append(f"{normalized_addon_id}.{normalized_id}")
        if not normalized_id.startswith("automation."):
            candidate_ids.extend(
                (
                    f"automation.{normalized_id}",
                    f"{normalized_addon_id}.automation.{normalized_id}",
                )
            )
        records = [
            record
            for record in self.registries.prompts
            if record.addon_id == normalized_addon_id
        ]
        for candidate_id in dict.fromkeys(candidate_ids):
            match = next(
                (record for record in records if record.id == candidate_id),
                None,
            )
            if match is not None:
                return match
        return next(
            (record for record in records if record.name == normalized_id),
            None,
        )

    def _workflow_execution_support(self, workflow) -> dict:
        definition = executable_definition_from_addon_workflow(workflow)
        if not definition:
            return {
                "full_execution_supported": False,
                "has_definition": False,
                "message": "No executable workflow definition was declared.",
                "step_count": 0,
            }
        try:
            compiled = compile_template_definition(
                definition, fallback_name=workflow.name
            )
        except TemplateDefinitionError as exc:
            return {
                "full_execution_supported": False,
                "has_definition": True,
                "message": f"Executable definition is invalid: {exc}",
                "step_count": 0,
            }
        return {
            "full_execution_supported": True,
            "has_definition": True,
            "message": "Executable definition is valid.",
            "step_count": len(compiled.steps),
        }

    def get_skill_tool_declarations(self, skill_id: str, open_mode: bool = False):
        return self.registries.skills.generate_tool_declarations(
            skill_id, self.registries.tools, open_mode=open_mode
        )

    def describe_skill(self, skill_id: str, include_inactive: bool = False):
        """Returns a skill record plus its helper and tool-schema discovery surface."""
        skill = self.get_skill_record(skill_id)
        if not skill:
            raise KeyError(f"Skill '{skill_id}' is not registered.")
        if not include_inactive and skill.status.state not in {ACTIVE, DEGRADED}:
            raise PermissionError(f"Skill '{skill_id}' is {skill.status.state}.")

        helpers = []
        for helper_id in skill.helper_ids:
            helper = self.registries.helpers.resolve(helper_id)
            helpers.append(
                {
                    "id": helper_id,
                    "registered": helper is not None,
                    "record": asdict(helper) if helper else None,
                }
            )

        return {
            "skill": asdict(skill),
            "tool_declarations": self.get_skill_tool_declarations(
                skill.id, open_mode=False
            ),
            "open_tool_declarations": self.get_skill_tool_declarations(
                skill.id, open_mode=True
            ),
            "helpers": helpers,
        }

    def validate_workflow_dry_run(self, workflow_id: str, payload: dict | None = None):
        """Validates a workflow definition without creating a stateful workflow run."""
        workflow = self.get_workflow_record(workflow_id)
        if not workflow:
            raise KeyError(f"Workflow '{workflow_id}' is not registered.")

        requirements = {
            "addons": self._describe_package_requirements(workflow.required_addons),
            "tools": self._describe_registry_requirements(
                workflow.required_tools, self.registries.tools, resolver="resolve"
            ),
            "tool_views": self._describe_tool_view_requirements(
                workflow.required_tool_views
            ),
            "helpers": self._describe_registry_requirements(
                workflow.required_helpers, self.registries.helpers, resolver="resolve"
            ),
            "providers": self._describe_registry_requirements(
                workflow.required_providers, self.registries.providers
            ),
            "services": self._describe_registry_requirements(
                workflow.required_services, self.registries.services
            ),
            "workers": self._describe_registry_requirements(
                workflow.required_workers, self.registries.workers
            ),
            "auth_scopes": self._describe_declared_requirements(workflow.auth_scopes),
            "resource_scopes": self._describe_declared_requirements(
                workflow.resource_scopes
            ),
            "approval_gates": self._describe_declared_requirements(
                workflow.approval_gates
            ),
        }
        blocking = self._blocking_requirement_reasons(requirements)
        available = workflow.status.state in {ACTIVE, DEGRADED}
        can_dry_run = bool(workflow.dry_run_supported and available and not blocking)
        execution_support = self._workflow_execution_support(workflow)

        message = "Workflow dry-run validation passed."
        if not workflow.dry_run_supported:
            message = "Workflow does not declare dry-run support; use launch handoff if available."
        elif not available:
            message = f"Workflow is {workflow.status.state}."
        elif blocking:
            message = "Workflow dry-run validation found blocking requirements."

        return {
            "mode": "dry_run",
            "workflow": asdict(workflow),
            "status": asdict(workflow.status),
            "input": payload or {},
            "available": available,
            "can_dry_run": can_dry_run,
            "full_execution_supported": execution_support["full_execution_supported"],
            "message": message,
            "blocking_reasons": blocking,
            "requirements": requirements,
            "execution_support": execution_support,
            "approval_gates": list(workflow.approval_gates),
            "expected_jobs": list(workflow.expected_jobs),
            "expected_artifacts": list(workflow.expected_artifacts),
            "links": {
                "launch_url": workflow.launch_url,
                "state_links": list(workflow.state_links),
                "log_links": list(workflow.log_links),
                "resume_links": list(workflow.resume_links),
            },
            "notes": [
                "No workflow run, job, approval request, artifact, or log record was created.",
                (
                    "Stateful background workflow execution can be started via runtime run creation "
                    "because this workflow declares an executable definition."
                )
                if execution_support["full_execution_supported"]
                else "Stateful background workflow execution is unavailable until an executable definition is declared.",
            ],
        }

    def get_workflow_launch(self, workflow_id: str):
        """Returns lightweight handoff metadata for workflows that expose a launch URL."""
        workflow = self.get_workflow_record(workflow_id)
        if not workflow:
            raise KeyError(f"Workflow '{workflow_id}' is not registered.")
        available = workflow.status.state in {ACTIVE, DEGRADED}
        execution_support = self._workflow_execution_support(workflow)
        return {
            "workflow_id": workflow.id,
            "name": workflow.name,
            "available": available,
            "status": asdict(workflow.status),
            "launch_supported": bool(workflow.launch_url and available),
            "launch_url": workflow.launch_url,
            "full_execution_supported": execution_support["full_execution_supported"],
            "execution_support": execution_support,
            "message": "Launch handoff is available."
            if workflow.launch_url and available
            else "No launch handoff is available for this workflow.",
        }

    def format_available_skills_for_prompt(self) -> str:
        snippets = self.registries.skills.prompt_snippets()
        if not snippets:
            return ""
        return (
            "Available reusable skills. Invoke explicitly with `/sk <id> <task>` (legacy `/skill <id> <task>` also works); do not auto-select a skill without a user request.\n"
            + "\n".join(snippets)
        )

    def render_skill_instructions(self, skill_id: str) -> str:
        skill = self.get_skill_record(skill_id)
        if not skill or skill.status.state not in {ACTIVE, DEGRADED}:
            return ""
        lines = [f"Skill: {skill.name} ({skill.id})", skill.description]
        if skill.tool_ids or skill.tool_view_ids:
            lines.append(
                f"Tools/tool views: {', '.join([*skill.tool_ids, *skill.tool_view_ids])}"
            )
        if skill.helper_ids:
            lines.append(f"Helpers: {', '.join(skill.helper_ids)}")
        if skill.instructions:
            lines.append(skill.instructions)
        return "\n\n".join(line for line in lines if line)

    def render_workflow_discovery(self, workflow_id: str) -> str:
        workflow = self.get_workflow_record(workflow_id)
        if not workflow or workflow.status.state not in {ACTIVE, DEGRADED}:
            return ""
        dry_run = self.validate_workflow_dry_run(workflow.id)
        lines = [f"Workflow: {workflow.name} ({workflow.id})", workflow.description]
        if workflow.dry_run_supported:
            if dry_run.get("full_execution_supported"):
                lines.append(
                    "Current support: dependency checks, dry-run discovery, and executable background runtime runs."
                )
                lines.append(
                    f"Executable steps: {dry_run.get('execution_support', {}).get('step_count', 0)}"
                )
            else:
                lines.append(
                    "Current support: dependency checks and dry-run discovery only; executable background runtime runs are not available yet."
                )
            lines.append(f"Dry-run status: {dry_run['message']}")
            if dry_run["blocking_reasons"]:
                lines.append(
                    "Blocking requirements: " + "; ".join(dry_run["blocking_reasons"])
                )
        if workflow.approval_gates:
            lines.append(f"Approval gates: {', '.join(workflow.approval_gates)}")
        if workflow.expected_jobs:
            lines.append(
                f"Expected jobs: {json.dumps(workflow.expected_jobs, default=str)}"
            )
        if workflow.expected_artifacts:
            lines.append(
                f"Expected artifacts: {json.dumps(workflow.expected_artifacts, default=str)}"
            )
        if workflow.launch_url:
            lines.append(f"Launch or handoff URL: {workflow.launch_url}")
        return "\n\n".join(line for line in lines if line)

    def _describe_package_requirements(self, addon_ids: list[str]) -> list[dict]:
        details = []
        for addon_id in addon_ids:
            package = self.registries.packages.get(addon_id)
            details.append(
                self._requirement_detail(addon_id, package, missing_label="addon")
            )
        return details

    def _describe_registry_requirements(
        self, record_ids: list[str], registry, resolver: str = "get"
    ) -> list[dict]:
        details = []
        resolve = getattr(registry, resolver)
        for record_id in record_ids:
            details.append(self._requirement_detail(record_id, resolve(record_id)))
        return details

    def _describe_tool_view_requirements(self, view_ids: list[str]) -> list[dict]:
        details = []
        for view_id in view_ids:
            details.append(
                self._requirement_detail(
                    view_id,
                    self.registries.tools.tool_views.get(view_id),
                    missing_label="tool view",
                )
            )
        return details

    def _describe_declared_requirements(self, values: list[str]) -> list[dict]:
        return [
            {
                "id": value,
                "registered": True,
                "state": ACTIVE,
                "reasons": [],
                "record": None,
            }
            for value in values
        ]

    def _requirement_detail(
        self, record_id: str, record, missing_label: str = "record"
    ) -> dict:
        if not record:
            return {
                "id": record_id,
                "registered": False,
                "state": MISSING_DEPENDENCY,
                "reasons": [
                    f"Required {missing_label} '{record_id}' is not registered."
                ],
                "record": None,
            }
        status = getattr(record, "status", ElementStatus(ACTIVE))
        return {
            "id": record_id,
            "registered": True,
            "state": status.state,
            "reasons": list(status.reasons),
            "record": asdict(record),
        }

    def _blocking_requirement_reasons(
        self, requirements: dict[str, list[dict]]
    ) -> list[str]:
        blocking = []
        for entries in requirements.values():
            for entry in entries:
                if entry.get("state") not in {ACTIVE, DEGRADED}:
                    blocking.extend(
                        entry.get("reasons")
                        or [
                            f"Required record '{entry.get('id')}' is {entry.get('state')}."
                        ]
                    )
        return blocking

    def get_registry_manifest(self):
        """Returns the full registry snapshot for diagnostics and tests."""
        return self.registries.as_manifest()

    def get_provider_records(self, domain: str = None, include_inactive: bool = False):
        """Returns provider records, optionally filtered by domain and availability."""
        records = (
            self.registries.providers.by_domain(domain)
            if domain
            else self.registries.providers.list()
        )
        if include_inactive:
            return records
        return [
            record for record in records if record.status.state in {ACTIVE, DEGRADED}
        ]

    def get_provider_record(self, domain: str, provider_id: str):
        """Resolves a provider by domain-qualified id, provider id, or alias."""
        if not provider_id:
            return None
        candidates = [provider_id, f"{domain}.{provider_id}"]
        for candidate in candidates:
            record = self.registries.providers.get(candidate)
            if record and record.domain == domain:
                return record
        for record in self.get_provider_records(domain):
            if provider_id in {
                record.provider_id,
                record.addon_id,
                record.id,
                *record.aliases,
            }:
                return record
        return None

    def get_provider_addon(self, domain: str, provider_id: str = None):
        """Returns the loaded addon instance that owns a provider record."""
        record = self.get_provider_record(domain, provider_id) if provider_id else None
        if not record:
            records = self.get_provider_records(domain)
            record = records[0] if records else None
        if not record or record.status.state not in {ACTIVE, DEGRADED}:
            return None
        return self.loaded_addons.get(record.addon_id)

    def validate_visible_connection_binding(
        self,
        *,
        connection_id: str,
        owner_user_id: int,
        connector_addon_id: str,
    ):
        """Validate one exact saved Connection without touching caller state.

        Durable operations use this immediately before client initialization
        so an omitted ID can never fall through to the current default account.
        The independent read session also avoids autoflushing or committing an
        unrelated request unit of work.
        """

        from sqlalchemy.orm import sessionmaker

        from .addons.connections import ConnectionService

        normalized = str(connection_id or "").strip()
        if not normalized:
            raise ValueError("An exact saved Connection ID is required.")
        checking_session = sessionmaker(bind=db.engine, expire_on_commit=False)()
        try:
            connection = ConnectionService(checking_session).require_visible(
                normalized,
                user_id=int(owner_user_id),
            )
            if connection.connector_addon_id != connector_addon_id:
                raise PermissionError(
                    "The saved Connection belongs to another connector add-on."
                )
            if connection.state != "ready":
                raise RuntimeError("The saved Connection is not ready.")
            return connection
        finally:
            checking_session.close()

    def get_helper_record(self, helper_id: str):
        return self.registries.helpers.resolve(helper_id)

    def get_ui_component_record(self, component_id: str):
        return self.registries.helpers.resolve(component_id)

    def get_tool_record(self, tool_id: str):
        return self.registries.tools.resolve(tool_id)

    def _resolve_plugin_instance(self, plugin_id: str):
        instance = self.plugins.get(plugin_id)
        if instance:
            return instance
        return next(
            (
                candidate
                for candidate in self.plugins.values()
                if plugin_id
                in {candidate.id, getattr(candidate, "prefix", candidate.id)}
            ),
            None,
        )

    def _initialize_addon_client(self, instance, config: dict):
        client = None
        if hasattr(instance, "initialize_client"):
            client = instance.initialize_client(config)
            auth_ok = True
            if hasattr(client, "test_authentication"):
                auth_ok = client.test_authentication()
            if not auth_ok:
                raise PermissionError(
                    f"Authentication failed for addon '{instance.manifest['name']}'."
                )
        return client

    @staticmethod
    def _get_addon_function_mapping(instance, client):
        """Call old and new mapping contracts without swallowing add-on errors.

        Older third-party add-ons expose ``get_function_mapping()`` while the
        request-local contract accepts the client returned by
        ``initialize_client``. Inspecting the bound signature preserves that
        compatibility without treating an internal ``TypeError`` as evidence
        of an old signature and then invoking the add-on twice.
        """

        method = instance.get_function_mapping
        parameters = inspect.signature(method).parameters.values()
        accepts_client = any(
            parameter.kind
            in {
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.VAR_POSITIONAL,
            }
            for parameter in parameters
        )
        return method(client) if accepts_client else method()

    def get_tool_declarations_for_plugin_ids(
        self,
        plugin_ids: list[str],
        config: dict | None = None,
        *,
        include_side_effect_tools: bool = True,
    ) -> list[dict]:
        """Return registered declarations for selected toolkit plugins.

        ``include_side_effect_tools=False`` is a fail-closed delegation boundary:
        approval-gated/write tools and legacy declarations without registry risk
        metadata are not exposed to the model.
        """
        config = config or {}
        tool_declarations = []

        for plugin_id in plugin_ids:
            instance = self._resolve_plugin_instance(plugin_id)
            if not instance:
                logging.warning(
                    f"Plugin with ID '{plugin_id}' requested but not found."
                )
                continue

            registry_tools = [
                tool
                for tool in self.registries.tools.list()
                if tool.addon_id == instance.id
            ]
            if not include_side_effect_tools:
                registry_tools = [
                    tool for tool in registry_tools if tool_is_read_only(tool)
                ]
                if not registry_tools:
                    continue

            try:
                self._initialize_addon_client(instance, config)
                if registry_tools:
                    tool_declarations.extend(
                        self.registries.tools.generate_tool_declarations(
                            [tool.id for tool in registry_tools]
                        )
                    )
                elif include_side_effect_tools and hasattr(instance, "get_tools"):
                    tool_declarations.extend(instance.get_tools())
            except Exception:
                logging.exception(
                    f"Error preparing tool declarations for addon '{instance.manifest['name']}'. It will be unavailable for this request."
                )

        return tool_declarations

    def get_skill_tool_view_allowed_parameters(
        self, skill_id: str | None, tool_name: str
    ) -> list[str] | None:
        """Returns the runtime parameter allow-list imposed by a skill's tool view."""
        if not skill_id or not tool_name:
            return None
        skill = self.get_skill_record(skill_id)
        tool = self.get_tool_record(tool_name)
        if not skill or not tool:
            return None

        allowed_sets = []
        for view_id in skill.tool_view_ids:
            view = self.registries.tools.tool_views.get(view_id)
            if view and view.tool_id == tool.id and view.allowed_parameters:
                allowed_sets.append(set(view.allowed_parameters))
        if not allowed_sets:
            return None

        allowed = set.intersection(*allowed_sets)
        return sorted(allowed)

    def validate_registered_tool_call(
        self,
        tool_name: str,
        arguments: dict | None,
        allowed_parameters: list[str] | None = None,
    ) -> dict:
        """Validates a tool call against registry status and optional tool-view constraints."""
        return self.registries.tools.validate_tool_call(
            tool_name, arguments or {}, allowed_parameters
        )

    def execute_capability(
        self,
        tool_name: str,
        arguments: dict | None,
        config: dict | None = None,
        *,
        requester_user_id: int | None = None,
        runtime_policy: dict | None = None,
        approval_id: str | None = None,
        resource_reference_ids: list[str] | None = None,
        allowed_parameters: list[str] | None = None,
        audit_log_source_id: str = "addons.execution",
        log_source_ids: tuple[str, ...] | list[str] = ("addons.execution",),
        workflow_run_id: str | None = None,
        workflow_step_id: str | None = None,
        runtime_job_id: str | None = None,
        event_sink=None,
    ):
        """Authorize, audit, and invoke one registered capability."""

        from .execution_kernel import (
            AgentEventSink,
            ToolAuthorityContext,
            ToolExecutor,
        )

        payload = dict(arguments or {})
        if approval_id:
            payload["_approval_id"] = approval_id
        if resource_reference_ids:
            payload["_resource_reference_ids"] = list(resource_reference_ids)

        def validate(name, values):
            return self.validate_registered_tool_call(
                name,
                values,
                allowed_parameters=allowed_parameters,
            )

        def availability(name, _values):
            record = self.get_tool_record(name)
            if record is None:
                return {
                    "status": "invalid_tool_call",
                    "error": f"Tool '{name}' is not registered.",
                }
            enabled = getattr(
                self,
                "_dispatch_enabled_addons",
                set(getattr(self, "loaded_addons", {})),
            )
            if record.addon_id not in enabled:
                return {
                    "status": "addon_inactive",
                    "error": f"Add-on '{record.addon_id}' is inactive.",
                }
            return None

        executor = ToolExecutor(
            dispatcher=lambda name, values, _prepared: {
                "action": "tool",
                "result": self._execute_registered_tool_raw(name, values, config),
            },
            event_sink=event_sink or AgentEventSink(),
            authority=ToolAuthorityContext(
                requester_user_id=requester_user_id,
                runtime_policy=dict(runtime_policy or {}),
                audit_log_source_id=audit_log_source_id,
                log_source_ids=tuple(log_source_ids),
                workflow_run_id=workflow_run_id,
                workflow_step_id=workflow_step_id,
                runtime_job_id=runtime_job_id,
            ),
            tool_record_resolver=self.get_tool_record,
            argument_validator=validate,
            availability_checker=availability,
            inline_execution=True,
        )
        executed = executor.execute_call(
            {
                "id": f"capability-{time.time_ns()}",
                "type": "function",
                "function": {"name": tool_name, "arguments": payload},
            }
        )
        return executed.result

    def execute_registered_tool(
        self,
        tool_name: str,
        arguments: dict | None,
        config: dict | None = None,
    ):
        """Compatibility entry point routed through the execution kernel."""

        return self.execute_capability(tool_name, arguments, config)

    def _execute_registered_tool_raw(
        self,
        tool_name: str,
        arguments: dict | None,
        config: dict | None = None,
    ):
        """Private callable binding; callers must already be inside ToolExecutor."""

        config = config or {}
        record = self.get_tool_record(tool_name)
        if not record:
            raise KeyError(f"Tool '{tool_name}' is not registered.")

        return self._execute_registered_tool_record_raw(
            record,
            tool_name,
            arguments,
            config,
        )

    def _execute_registered_tool_record_raw(
        self,
        record,
        tool_name: str,
        arguments: dict | None,
        config: dict | None = None,
        *,
        instance=None,
    ):
        """Invoke one already-resolved record without another mutable lookup.

        Policy callers that must close alias/registry reload races resolve and
        validate the exact record immediately before passing it here.  This
        method binds the add-on and callable solely from that record; it never
        resolves ``tool_name`` again.
        """

        config = config or {}
        enabled = getattr(self, "_dispatch_enabled_addons", None)
        if enabled is not None and record.addon_id not in enabled:
            raise RuntimeError(f"Add-on '{record.addon_id}' is inactive.")

        instance = instance or self.loaded_addons.get(
            record.addon_id
        ) or self.plugins.get(record.addon_id)
        if not instance:
            raise KeyError(
                f"Addon '{record.addon_id}' for tool '{record.id}' is not loaded."
            )

        client = self._initialize_addon_client(instance, config)
        if not hasattr(instance, "get_function_mapping"):
            raise RuntimeError(
                f"Addon '{record.addon_id}' does not expose tool function bindings."
            )
        mapping = self._get_addon_function_mapping(instance, client)

        legacy_prefix = record.legacy_prefix or getattr(instance, "prefix", instance.id)
        local_id = record.id.split(".", 1)[1] if "." in record.id else record.id
        candidate_names = [
            record.legacy_name,
            tool_name,
            record.id,
            record.id.replace(".", "_"),
            local_id,
            f"{legacy_prefix}_{local_id}",
            *record.aliases,
        ]
        api_function = next(
            (
                mapping.get(name)
                for name in candidate_names
                if name and mapping.get(name)
            ),
            None,
        )
        if not api_function:
            raise KeyError(f"Function binding for tool '{record.id}' was not found.")

        return api_function(**(arguments or {}))

    def get_tools_and_mappings(self, plugin_ids: list[str], config: dict):
        """
        Initializes clients for the given plugins for a single request, and returns their
        aggregated tool definitions and function mappings.
        """
        tool_declarations = []
        function_mappings = {}

        for plugin_id in plugin_ids:
            instance = self._resolve_plugin_instance(plugin_id)
            if not instance:
                logging.warning(
                    f"Plugin with ID '{plugin_id}' requested but not found."
                )
                continue

            try:
                client = self._initialize_addon_client(instance, config)

                package_record = self.registries.packages.get(instance.id)
                registry_tools = [
                    tool
                    for tool in self.registries.tools.list()
                    if tool.addon_id == instance.id
                ]
                use_registry_tools = bool(
                    package_record
                    and package_record.migration_status == "explicit_migrated"
                    and registry_tools
                )

                if (use_registry_tools or hasattr(instance, "get_tools")) and hasattr(
                    instance, "get_function_mapping"
                ):
                    if use_registry_tools:
                        tool_declarations.extend(
                            self.registries.tools.generate_tool_declarations(
                                [tool.id for tool in registry_tools]
                            )
                        )
                    else:
                        tool_declarations.extend(instance.get_tools())

                    mapping = self._get_addon_function_mapping(instance, client)

                    function_mappings.update(mapping)
                    logging.info(
                        f"Successfully loaded tools for plugin: {instance.manifest['name']}"
                    )

            except Exception:
                logging.exception(
                    f"Error initializing client or getting tools for plugin '{instance.manifest['name']}'. It will be unavailable for this request."
                )

        return tool_declarations, function_mappings

    def run_hook(self, hook_name, **kwargs):
        """
        Runs a specific hook method on all loaded addons that have it.
        """
        if hook_name == "before_tool_call":
            modified_args = kwargs.get("args_dict")
            for addon in self.loaded_addons.values():
                if hasattr(addon, f"on_{hook_name}"):
                    method = getattr(addon, f"on_{hook_name}")
                    modified_args = method(modified_args)
            return modified_args
        elif hook_name == "after_llm_response":
            modified_text = kwargs.get("text", "")
            for addon in self.loaded_addons.values():
                if hasattr(addon, f"on_{hook_name}"):
                    method = getattr(addon, f"on_{hook_name}")
                    modified_text = method(modified_text, kwargs.get("session_id"))
            return modified_text
        else:
            for addon in self.loaded_addons.values():
                if hasattr(addon, f"on_{hook_name}"):
                    method = getattr(addon, f"on_{hook_name}")
                    method(**kwargs)

    def collect_preserved_models(self):
        """Aggregates models from all active addons that should be preserved during reset/backup."""
        models = []
        for addon in self.loaded_addons.values():
            if hasattr(addon, "get_preserved_models"):
                models.extend(addon.get_preserved_models())
        return models

    def resolve_short_url(self, code, user):
        """Iterates active addons until one returns a URL for the short code."""
        for addon in self.loaded_addons.values():
            if hasattr(addon, "resolve_short_url"):
                url = addon.resolve_short_url(code, user)
                if url:
                    return url
        return None

    def get_knowledge_provider(self, source_type):
        """Finds the addon responsible for a specific source_type."""
        for addon in self.loaded_addons.values():
            if hasattr(addon, "get_knowledge_provider"):
                provider = addon.get_knowledge_provider(source_type)
                if provider:
                    return provider
        return None

    def notify_rag_rebuild(self):
        """Triggers the on_rag_rebuild_triggered hook on all loaded addons."""
        for addon_id, addon in self.loaded_addons.items():
            if hasattr(addon, "on_rag_rebuild_triggered"):
                try:
                    logging.info(f"Triggering RAG rebuild hook for addon: {addon_id}")
                    addon.on_rag_rebuild_triggered()
                except Exception as e:
                    logging.error(
                        f"Error triggering RAG rebuild hook for addon '{addon_id}': {e}",
                        exc_info=True,
                    )

    def register_scheduled_jobs(self):
        """Iterates through active addons and registers their jobs."""
        scheduler = getattr(self.app, "scheduler", None)
        if not scheduler:
            return

        with self.app.app_context():
            for addon_id, instance in self.loaded_addons.items():
                if hasattr(instance, "on_register_jobs"):
                    try:
                        instance.on_register_jobs(scheduler)
                    except Exception as e:
                        logging.error(
                            f"Error registering jobs for addon '{addon_id}': {e}",
                            exc_info=True,
                        )

    def unregister_scheduled_jobs(self, addon_id: str):
        """Removes all jobs associated with a specific addon."""
        scheduler = getattr(self.app, "scheduler", None)
        if not scheduler:
            return

        count = 0
        for job in scheduler.get_jobs():
            if self._job_belongs_to_addon(addon_id, getattr(job, "id", None)):
                job.remove()
                count += 1
        if count > 0:
            logging.info(
                f"Unregistered {count} background job(s) for addon '{addon_id}'."
            )
