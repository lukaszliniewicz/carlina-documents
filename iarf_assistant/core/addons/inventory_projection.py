"""Compact and compatibility projections for the add-on inventory APIs."""

from __future__ import annotations

from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from .contracts import classify_effective_code_access
from .dependency_contracts import extract_dependency_addon_ids
from .inventory_metadata import AddonInventoryMetadataMixin
from .lifecycle import DATA_PURGED as LIFECYCLE_DATA_PURGED

if TYPE_CHECKING:
    from collections.abc import Iterable

    from iarf_assistant.core.addon_contracts import AddonRegistries

_MANIFEST_SCHEMA_VERSION = 2
_RETAINED_DATA_DESCRIPTION = (
    "Add-on code is not installed; owned data and schema are preserved."
)


class AddonInventoryProjectionMixin(AddonInventoryMetadataMixin):
    """Project manifest, persistence, and runtime state for operator APIs."""

    registries: AddonRegistries
    _builtin_reconciliation_required: dict[str, dict[str, str]]

    def get_worker_states(
        self,
        addon_ids: Iterable[str],
    ) -> dict[str, dict[str, Any]]:
        """Return a worker-state snapshot supplied by the runtime manager."""
        raise NotImplementedError

    def get_scheduled_job_states(
        self,
        addon_ids: Iterable[str],
    ) -> dict[str, dict[str, Any]]:
        """Return a scheduler snapshot supplied by the runtime manager."""
        raise NotImplementedError

    def get_frontend_build_status(
        self,
        addon_id: str,
        *,
        refresh: bool = False,
    ) -> dict[str, Any]:
        """Return build state supplied by frontend-build orchestration."""
        raise NotImplementedError

    @staticmethod
    def _frontend_build_public_status(
        status: dict[str, Any] | None,
    ) -> dict[str, Any]:
        raise NotImplementedError

    @staticmethod
    def _normalize_dependency_notices(
        manifest: dict[str, Any],
    ) -> list[dict[str, Any]]:
        raise NotImplementedError

    def _builtin_reconciliation_projection(
        self,
        addon_id: str,
        db_record: Any,
        manifest: dict[str, Any],
        contract: dict[str, Any],
    ) -> dict[str, str] | None:
        if db_record.source_kind != "built_in":
            return None
        installed_version = str(db_record.version)
        package_version = str(manifest.get("version", "0.0.0"))
        migration = contract.get("database_migration")
        migration = migration if isinstance(migration, dict) else {}
        package_revision = str(migration.get("head_revision") or "").strip()
        installed_revision = str(db_record.alembic_revision or "").strip()
        migration_changed = bool(
            package_revision and installed_revision != package_revision
        )
        pending = self._builtin_reconciliation_required.get(addon_id, {})
        if (
            installed_version == package_version
            and not migration_changed
            and not pending
        ):
            return None
        reconciliation = {
            "installed_version": installed_version,
            "package_version": package_version,
        }
        if migration_changed:
            reconciliation.update(
                {
                    "installed_revision": installed_revision or "base",
                    "package_revision": package_revision,
                }
            )
        reconciliation.update(pending)
        return reconciliation

    def get_addon_inventory_summaries(
        self,
        *,
        requester_user_id: int | None = None,
        requester_is_admin: bool = False,
    ) -> list[dict[str, Any]]:
        """Return compact list projections without Doctor/catalogue/detail probes."""
        (
            db_addons,
            connection_counts,
            consumer_binding_counts,
            provider_binding_counts,
        ) = self._load_inventory_database(
            requester_user_id=requester_user_id,
            requester_is_admin=requester_is_admin,
        )
        manifests, metadata = self._inventory_projection_snapshot()
        manifest_index, spoke_to_hub, hub_to_spokes = metadata
        addon_ids = set(manifests) | set(db_addons)
        worker_states = self.get_worker_states(addon_ids)
        frontend_cache = getattr(self, "_frontend_build_status_cache", {})
        summaries: list[dict[str, Any]] = []

        for addon_id, manifest_data in sorted(
            manifests.items(),
            key=lambda item: item[1]["manifest"]["name"],
        ):
            db_record = db_addons.get(addon_id)
            if db_record is None:
                continue
            manifest = manifest_data["manifest"]
            metadata = manifest_index.get(addon_id, {})
            package_record = self.registries.packages.get(addon_id)
            roles = list(
                package_record.roles
                if package_record and package_record.roles
                else metadata.get("roles", [])
            )
            categories = list(
                package_record.categories
                if package_record and package_record.categories
                else metadata.get("categories", [])
            )
            addon_types = manifest.get("addon_type") or []
            if not isinstance(addon_types, list):
                addon_types = [addon_types]
            contract = (
                db_record.resolved_contract_json
                if isinstance(db_record.resolved_contract_json, dict)
                else {}
            )
            runtime_units = list(contract.get("runtime_units") or [])
            notices = self._normalize_dependency_notices(manifest)
            frontend_status = frontend_cache.get(addon_id) or {}
            summaries.append(
                {
                    "detail_level": "summary",
                    "detail_url": f"/api/addons/{addon_id}/detail",
                    "id": addon_id,
                    "name": manifest.get("name") or addon_id,
                    "version": manifest.get("version", "N/A"),
                    "installed_version": db_record.version,
                    "builtin_reconciliation_required": self._builtin_reconciliation_projection(
                        addon_id,
                        db_record,
                        manifest,
                        contract,
                    ),
                    "author": manifest.get("author", "Unknown"),
                    "description": manifest.get("description", ""),
                    "roles": roles,
                    "categories": categories,
                    "kind": contract.get("kind") or "toolkit",
                    "distribution": dict(contract.get("distribution") or {}),
                    "effective_code_access": classify_effective_code_access(
                        runtime_units,
                        database_migration=contract.get("database_migration"),
                        legacy_trust_tier=db_record.trust_tier,
                        runtime_declared=(
                            contract.get("manifest_schema_version")
                            == _MANIFEST_SCHEMA_VERSION
                        ),
                    ).value,
                    "connection_count": connection_counts.get(addon_id, 0),
                    "consumer_binding_count": consumer_binding_counts.get(
                        addon_id,
                        0,
                    ),
                    "provider_binding_count": provider_binding_counts.get(
                        addon_id,
                        0,
                    ),
                    "provider_domains": list(metadata.get("provider_domains", [])),
                    "addon_type": [str(value) for value in addon_types if value],
                    "dependencies": list(metadata.get("dependencies", [])),
                    "search_terms": self._inventory_search_terms(
                        contract,
                        notices,
                    ),
                    "is_active": bool(db_record.is_active),
                    "status": (
                        asdict(package_record.status)
                        if package_record
                        else {"state": "active", "reasons": []}
                    ),
                    "frontend_tier": (
                        package_record.frontend_tier if package_record else None
                    ),
                    "frontend_status": frontend_status.get("status"),
                    "frontend_rebuild_needed": bool(
                        frontend_status.get("rebuild_needed")
                    ),
                    "worker_running": int(
                        (worker_states.get(addon_id) or {}).get("running") or 0
                    ),
                    "has_settings": "settings_schema" in manifest
                    or bool(manifest.get("custom_settings_ui")),
                    "has_prompts": bool(
                        manifest.get("plugin_prompt")
                        or manifest.get("automation_prompts")
                        or manifest.get("prompts")
                    ),
                    "delegatable": bool(manifest.get("delegatable_credentials")),
                    "source_kind": db_record.source_kind,
                    "trust_tier": db_record.trust_tier,
                    "lifecycle_state": db_record.lifecycle_state,
                    "lifecycle_generation": db_record.lifecycle_generation,
                    "lifecycle_error": db_record.lifecycle_error,
                    "code_available": True,
                    "can_remove_code": db_record.source_kind == "installed",
                    "can_purge_data": not db_record.is_active,
                    "inferred_hub": self._inferred_hub(
                        addon_id,
                        manifest_index,
                        spoke_to_hub,
                        hub_to_spokes,
                    ),
                }
            )

        for addon_id in sorted(set(db_addons) - set(manifests)):
            db_record = db_addons[addon_id]
            contract = (
                db_record.resolved_contract_json
                if isinstance(db_record.resolved_contract_json, dict)
                else {}
            )
            summaries.append(
                {
                    "detail_level": "summary",
                    "detail_url": f"/api/addons/{addon_id}/detail",
                    "id": addon_id,
                    "name": contract.get("name") or addon_id,
                    "version": db_record.version,
                    "installed_version": db_record.version,
                    "builtin_reconciliation_required": None,
                    "author": "Unknown",
                    "description": _RETAINED_DATA_DESCRIPTION,
                    "roles": list(contract.get("roles") or []),
                    "categories": list(contract.get("categories") or []),
                    "kind": contract.get("kind") or "toolkit",
                    "distribution": dict(contract.get("distribution") or {}),
                    "effective_code_access": classify_effective_code_access(
                        list(contract.get("runtime_units") or []),
                        database_migration=contract.get("database_migration"),
                        legacy_trust_tier=db_record.trust_tier,
                        runtime_declared=(
                            contract.get("manifest_schema_version")
                            == _MANIFEST_SCHEMA_VERSION
                        ),
                    ).value,
                    "connection_count": connection_counts.get(addon_id, 0),
                    "consumer_binding_count": consumer_binding_counts.get(
                        addon_id,
                        0,
                    ),
                    "provider_binding_count": provider_binding_counts.get(
                        addon_id,
                        0,
                    ),
                    "provider_domains": [],
                    "addon_type": [],
                    "dependencies": list(contract.get("addon_dependencies") or []),
                    "search_terms": self._inventory_search_terms(contract, []),
                    "is_active": False,
                    "status": {
                        "state": db_record.lifecycle_state or "code_removed",
                        "reasons": ["The selected add-on code package is unavailable."],
                    },
                    "frontend_tier": None,
                    "frontend_status": "not_available",
                    "frontend_rebuild_needed": False,
                    "worker_running": 0,
                    "has_settings": False,
                    "has_prompts": False,
                    "delegatable": False,
                    "source_kind": db_record.source_kind,
                    "trust_tier": db_record.trust_tier,
                    "lifecycle_state": db_record.lifecycle_state,
                    "lifecycle_generation": db_record.lifecycle_generation,
                    "lifecycle_error": db_record.lifecycle_error,
                    "code_available": False,
                    "can_remove_code": False,
                    "can_purge_data": (
                        db_record.lifecycle_state != LIFECYCLE_DATA_PURGED
                    ),
                    "inferred_hub": {
                        "relation": "standalone",
                        "hub_id": None,
                        "hub_label": None,
                        "spoke_count": 0,
                    },
                }
            )

        summaries.sort(key=lambda item: (str(item["name"]).casefold(), item["id"]))
        return summaries

    def _project_addon_details(
        self,
        selected_addon_ids: set[str] | None = None,
        *,
        requester_user_id: int | None = None,
        requester_is_admin: bool = False,
    ) -> list[dict[str, Any]]:
        """Return merged detail projections for discovered add-ons."""
        details = []
        (
            db_addons,
            connection_counts,
            consumer_binding_counts,
            provider_binding_counts,
        ) = self._load_inventory_database(
            selected_addon_ids,
            requester_user_id=requester_user_id,
            requester_is_admin=requester_is_admin,
        )

        if selected_addon_ids is not None and not db_addons:
            return []

        manifests, metadata = self._inventory_projection_snapshot()
        addon_ids = (
            set(manifests) | set(db_addons)
            if selected_addon_ids is None
            else set(selected_addon_ids)
        )
        worker_states = self.get_worker_states(addon_ids)
        scheduled_job_states = self.get_scheduled_job_states(addon_ids)

        manifest_index, spoke_to_hub, hub_to_spokes = metadata
        manifest_items = (
            manifests.items()
            if selected_addon_ids is None
            else (
                (addon_id, manifest_data)
                for addon_id, manifest_data in manifests.items()
                if addon_id in selected_addon_ids
            )
        )
        for addon_id, manifest_data in sorted(
            manifest_items,
            key=lambda item: item[1]["manifest"]["name"],
        ):
            manifest = manifest_data["manifest"]
            db_record = db_addons.get(addon_id)

            if not db_record:
                # Should not happen if sync ran, but as a safeguard
                continue

            addon_types = manifest.get("addon_type", [])
            if not isinstance(addon_types, list):
                addon_types = [addon_types]

            manifest_meta = manifest_index.get(addon_id, {})
            roles = manifest_meta.get("roles", [])
            categories = manifest_meta.get("categories", [])
            provider_domains = manifest_meta.get("provider_domains", [])

            dependency_addon_ids = extract_dependency_addon_ids(
                manifest.get("dependencies", {})
            )
            log_links = []
            if hasattr(self.app, "log_registry"):
                log_links = self.app.log_registry.get_links_for_addon(addon_id)
            frontend_build_status = self.get_frontend_build_status(addon_id)
            package_record = self.registries.packages.get(addon_id)
            if package_record:
                roles = list(package_record.roles or roles)
                categories = list(package_record.categories or categories)

            resolved_contract = (
                db_record.resolved_contract_json
                if isinstance(db_record.resolved_contract_json, dict)
                else {}
            )
            provenance = (
                db_record.provenance_json
                if isinstance(db_record.provenance_json, dict)
                else {}
            )
            runtime_units = list(resolved_contract.get("runtime_units") or [])
            effective_code_access = classify_effective_code_access(
                runtime_units,
                database_migration=resolved_contract.get("database_migration"),
                legacy_trust_tier=db_record.trust_tier,
                runtime_declared=(
                    resolved_contract.get("manifest_schema_version")
                    == _MANIFEST_SCHEMA_VERSION
                ),
            ).value
            details.append(
                {
                    "id": manifest["id"],
                    "name": manifest["name"],
                    "version": manifest.get("version", "N/A"),
                    "installed_version": db_record.version,
                    "builtin_reconciliation_required": self._builtin_reconciliation_projection(
                        addon_id,
                        db_record,
                        manifest,
                        resolved_contract,
                    ),
                    "author": manifest.get("author", "Unknown"),
                    "description": manifest.get("description", ""),
                    "roles": list(roles),
                    "categories": list(categories),
                    "kind": resolved_contract.get("kind") or "toolkit",
                    "distribution": dict(resolved_contract.get("distribution") or {}),
                    "runtime_units": runtime_units,
                    "database_migration": resolved_contract.get("database_migration"),
                    "effective_code_access": effective_code_access,
                    "capability_requirements": list(
                        resolved_contract.get("capability_requirements") or []
                    ),
                    "capability_providers": list(
                        resolved_contract.get("capability_providers") or []
                    ),
                    "ui_entries": list(resolved_contract.get("ui_entries") or []),
                    "connection_count": connection_counts.get(addon_id, 0),
                    "consumer_binding_count": consumer_binding_counts.get(addon_id, 0),
                    "provider_binding_count": provider_binding_counts.get(addon_id, 0),
                    "provider_domains": list(provider_domains),
                    "addon_type": addon_types,
                    "dependencies": dependency_addon_ids,
                    "dependency_notices": self._normalize_dependency_notices(manifest),
                    "is_active": db_record.is_active,
                    "status": asdict(package_record.status)
                    if package_record
                    else {"state": "active", "reasons": []},
                    "frontend_tier": (
                        package_record.frontend_tier if package_record else None
                    ),
                    "frontend_build": self._frontend_build_public_status(
                        frontend_build_status
                    ),
                    "worker_state": worker_states[addon_id],
                    "scheduled_job_state": scheduled_job_states[addon_id],
                    "has_settings": "settings_schema" in manifest
                    or manifest.get("custom_settings_ui", False),
                    "has_prompts": bool(
                        manifest.get("plugin_prompt")
                        or manifest.get("automation_prompts")
                        or manifest.get("prompts")
                    ),
                    "delegatable": manifest.get("delegatable_credentials", False),
                    "source_kind": db_record.source_kind,
                    "trust_tier": db_record.trust_tier,
                    "lifecycle_state": db_record.lifecycle_state,
                    "lifecycle_generation": db_record.lifecycle_generation,
                    "lifecycle_error": db_record.lifecycle_error,
                    "manifest_sha256": db_record.manifest_sha256,
                    "contract_sha256": db_record.contract_sha256,
                    "provenance": provenance,
                    "permissions": list(resolved_contract.get("permissions") or []),
                    "resources": list(resolved_contract.get("resources") or []),
                    "storage": dict(resolved_contract.get("storage") or {}),
                    "code_available": True,
                    "can_remove_code": db_record.source_kind == "installed",
                    "can_purge_data": not db_record.is_active,
                    "log_links": log_links,
                    "inferred_hub": self._inferred_hub(
                        addon_id,
                        manifest_index,
                        spoke_to_hub,
                        hub_to_spokes,
                    ),
                }
            )

        # Code removal intentionally preserves the database row and owned
        # data. Keep those records visible so operators can inspect history,
        # reinstall, or make the separate destructive purge decision.
        for addon_id in sorted(set(db_addons) - set(manifests)):
            db_record = db_addons[addon_id]
            contract = (
                db_record.resolved_contract_json
                if isinstance(db_record.resolved_contract_json, dict)
                else {}
            )
            provenance = (
                db_record.provenance_json
                if isinstance(db_record.provenance_json, dict)
                else {}
            )
            runtime_units = list(contract.get("runtime_units") or [])
            effective_code_access = classify_effective_code_access(
                runtime_units,
                database_migration=contract.get("database_migration"),
                legacy_trust_tier=db_record.trust_tier,
                runtime_declared=contract.get("manifest_schema_version")
                == _MANIFEST_SCHEMA_VERSION,
            ).value
            details.append(
                {
                    "id": addon_id,
                    "name": contract.get("name") or addon_id,
                    "version": db_record.version,
                    "author": "Unknown",
                    "description": _RETAINED_DATA_DESCRIPTION,
                    "roles": list(contract.get("roles") or []),
                    "categories": list(contract.get("categories") or []),
                    "kind": contract.get("kind") or "toolkit",
                    "distribution": dict(contract.get("distribution") or {}),
                    "runtime_units": runtime_units,
                    "database_migration": contract.get("database_migration"),
                    "effective_code_access": effective_code_access,
                    "capability_requirements": list(
                        contract.get("capability_requirements") or []
                    ),
                    "capability_providers": list(
                        contract.get("capability_providers") or []
                    ),
                    "ui_entries": list(contract.get("ui_entries") or []),
                    "connection_count": connection_counts.get(addon_id, 0),
                    "consumer_binding_count": consumer_binding_counts.get(addon_id, 0),
                    "provider_binding_count": provider_binding_counts.get(addon_id, 0),
                    "provider_domains": [],
                    "addon_type": [],
                    "dependencies": list(contract.get("addon_dependencies") or []),
                    "dependency_notices": [],
                    "is_active": False,
                    "status": {
                        "state": db_record.lifecycle_state or "code_removed",
                        "reasons": ["The selected add-on code package is unavailable."],
                    },
                    "frontend_tier": None,
                    "frontend_build": {
                        "declared": False,
                        "status": "not_available",
                        "rebuild_needed": False,
                        "reasons": [],
                        "warnings": [],
                    },
                    "worker_state": worker_states[addon_id],
                    "scheduled_job_state": scheduled_job_states[addon_id],
                    "has_settings": False,
                    "has_prompts": False,
                    "delegatable": False,
                    "source_kind": db_record.source_kind,
                    "trust_tier": db_record.trust_tier,
                    "lifecycle_state": db_record.lifecycle_state,
                    "lifecycle_generation": db_record.lifecycle_generation,
                    "lifecycle_error": db_record.lifecycle_error,
                    "manifest_sha256": db_record.manifest_sha256,
                    "contract_sha256": db_record.contract_sha256,
                    "provenance": provenance,
                    "permissions": list(contract.get("permissions") or []),
                    "resources": list(contract.get("resources") or []),
                    "storage": dict(contract.get("storage") or {}),
                    "code_available": False,
                    "can_remove_code": False,
                    "can_purge_data": db_record.lifecycle_state
                    != LIFECYCLE_DATA_PURGED,
                    "log_links": [],
                    "inferred_hub": {
                        "relation": "standalone",
                        "hub_id": None,
                        "hub_label": None,
                        "spoke_count": 0,
                    },
                }
            )
        details.sort(key=lambda item: (str(item["name"]).casefold(), item["id"]))
        return details

    def get_all_addons_details(
        self,
        *,
        requester_user_id: int | None = None,
        requester_is_admin: bool = False,
    ) -> list[dict[str, Any]]:
        """Compatibility composition for callers that still need every detail."""
        return self._project_addon_details(
            requester_user_id=requester_user_id,
            requester_is_admin=requester_is_admin,
        )

    def get_addon_details(
        self,
        addon_ids: Iterable[str],
        *,
        requester_user_id: int | None = None,
        requester_is_admin: bool = False,
    ) -> list[dict[str, Any]]:
        """Return full projections for a bounded set of add-on identifiers."""
        normalized_ids = {
            str(addon_id).strip() for addon_id in addon_ids if str(addon_id).strip()
        }
        if not normalized_ids:
            return []
        return self._project_addon_details(
            normalized_ids,
            requester_user_id=requester_user_id,
            requester_is_admin=requester_is_admin,
        )

    def get_addon_detail(
        self,
        addon_id: str,
        *,
        requester_user_id: int | None = None,
        requester_is_admin: bool = False,
    ) -> dict[str, Any] | None:
        """Return one full add-on projection when the package is known."""
        details = self.get_addon_details(
            [addon_id],
            requester_user_id=requester_user_id,
            requester_is_admin=requester_is_admin,
        )
        return details[0] if details else None
