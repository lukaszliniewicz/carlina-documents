"""Bounded metadata cache and database reads for add-on inventory views."""

from __future__ import annotations

import json
import threading
import time
from contextlib import nullcontext
from datetime import UTC, datetime
from typing import Any

from flask import has_app_context
from sqlalchemy import func, literal, or_, select, union_all

from iarf_assistant.extensions import db
from iarf_assistant.models import (
    Addon,
    AddonConnection,
    CapabilityBinding,
    ConnectionGrant,
)

from .dependency_contracts import extract_dependency_addon_ids

ManifestInventory = dict[str, dict[str, Any]]
InventoryMetadata = tuple[
    ManifestInventory,
    dict[str, str],
    dict[str, list[str]],
]
InventoryDatabase = tuple[
    dict[str, Addon],
    dict[str, int],
    dict[str, int],
    dict[str, int],
]
InventoryCacheMetrics = dict[str, int | float | None]

_DOMAIN_HUB_HINTS: dict[str, frozenset[str]] = {
    "audio": frozenset({"audio_core"}),
    "scraping": frozenset({"scraping"}),
    "email": frozenset({"email"}),
    "document": frozenset({"doc_processor"}),
    "publishing": frozenset({"wordpress"}),
    "wordpress_extension": frozenset({"wordpress"}),
}


def _normalize_string_list(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if item]
    if value is None:
        return []
    return [str(value)]


class AddonInventoryMetadataMixin:
    """Own the non-authoritative metadata cache used by inventory projections."""

    app: Any
    manifests: dict[str, dict[str, Any]]

    def _inventory_cache_lock(self) -> threading.RLock:
        lock = getattr(self, "_addon_inventory_cache_lock", None)
        if lock is None:
            lock = threading.RLock()
            self._addon_inventory_cache_lock = lock
        return lock

    def _inventory_manifest_lock(self) -> threading.RLock:
        lock = getattr(self, "_manifest_lock", None)
        if lock is None:
            lock = threading.RLock()
            self._manifest_lock = lock
        return lock

    def _inventory_cache_metrics(self) -> InventoryCacheMetrics:
        metrics = getattr(self, "_addon_inventory_cache_metrics", None)
        if metrics is None:
            metrics = {
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
            self._addon_inventory_cache_metrics = metrics
        return metrics

    def invalidate_addon_inventory(
        self,
        reason: str = "manifest_state_changed",
    ) -> None:
        """Invalidate manifest metadata without touching authoritative state."""
        with self._inventory_cache_lock():
            self._addon_inventory_generation = (
                int(getattr(self, "_addon_inventory_generation", 0)) + 1
            )
            self._addon_inventory_metadata_cache = None
            metrics = self._inventory_cache_metrics()
            metrics["invalidations"] = int(metrics["invalidations"] or 0) + 1
            metrics["current_bytes"] = 0
            self._addon_inventory_last_invalidation_reason = str(
                reason or "unspecified"
            )
            self._addon_inventory_last_invalidation_at = (
                datetime.now(UTC).isoformat().replace("+00:00", "Z")
            )

    def get_addon_inventory_cache_status(self) -> dict[str, Any]:
        """Return the cache policy and bounded metrics for operator surfaces."""
        with self._inventory_cache_lock():
            metrics = dict(self._inventory_cache_metrics())
            return {
                "name": "addon_inventory_metadata",
                "owner": "core.addons.inventory_metadata",
                "purpose": "Immutable manifest metadata and hub relationships.",
                "data_classification": "public_metadata",
                "persistence": "memory_only",
                "key_schema": "manifest_generation:v1",
                "namespace": "carlina:addons:inventory:v1",
                "maximum_entries": 1,
                "maximum_bytes": None,
                "ttl_seconds": None,
                "stale_while_revalidate_seconds": None,
                "invalidation_triggers": [
                    "package discovery",
                    "package refresh",
                    "code install or removal",
                ],
                "concurrency": "process_local_rlock",
                "copy_behavior": "fresh public projections",
                "authoritative": False,
                "safe_reset": True,
                "generation": int(getattr(self, "_addon_inventory_generation", 0)),
                "entries": int(
                    getattr(self, "_addon_inventory_metadata_cache", None) is not None
                ),
                "last_invalidation_reason": getattr(
                    self,
                    "_addon_inventory_last_invalidation_reason",
                    None,
                ),
                "last_invalidation_at": getattr(
                    self,
                    "_addon_inventory_last_invalidation_at",
                    None,
                ),
                "last_build_at": getattr(
                    self,
                    "_addon_inventory_last_build_at",
                    None,
                ),
                **metrics,
            }

    def _manifest_inventory_record(
        self,
        addon_id: str,
        manifest_data: dict[str, Any],
    ) -> dict[str, Any]:
        manifest = manifest_data.get("manifest") or {}
        roles = _normalize_string_list(manifest.get("roles"))
        categories = _normalize_string_list(manifest.get("categories"))
        dependencies = [
            dependency_id
            for dependency_id in extract_dependency_addon_ids(
                manifest.get("dependencies", {})
            )
            if dependency_id != addon_id
        ]
        provider_domains = {
            str(provider["domain"])
            for provider in manifest.get("providers") or []
            if isinstance(provider, dict) and provider.get("domain")
        }
        for key, domain in (("audio", "audio"), ("scraping", "scraping")):
            section = manifest.get(key)
            if isinstance(section, dict) and section.get("providers"):
                provider_domains.add(domain)
        return {
            "name": manifest.get("name", addon_id),
            "roles": roles,
            "categories": categories,
            "dependencies": dependencies,
            "provider_domains": sorted(provider_domains),
        }

    @staticmethod
    def _hub_candidate_score(
        candidate_id: str,
        candidate_roles: set[str],
        preferred_hubs: set[str],
    ) -> int:
        return (
            6 * int(candidate_id in preferred_hubs)
            + 5 * int("hub" in candidate_roles)
            + 3 * int("workspace" in candidate_roles)
            + 2 * int("toolkit" in candidate_roles and bool(preferred_hubs))
        )

    @staticmethod
    def _hub_candidates(
        addon_id: str,
        metadata: dict[str, Any],
        manifest_index: ManifestInventory,
    ) -> list[tuple[int, str, str]]:
        preferred_hubs = {
            hub_id
            for domain in metadata["provider_domains"]
            for hub_id in _DOMAIN_HUB_HINTS.get(domain, ())
        }
        candidates: list[tuple[int, str, str]] = []
        for candidate_id in metadata["dependencies"]:
            candidate = manifest_index.get(candidate_id)
            if candidate is None:
                continue
            candidate_roles = set(candidate["roles"])
            score = AddonInventoryMetadataMixin._hub_candidate_score(
                candidate_id,
                candidate_roles,
                preferred_hubs,
            )
            if score > 0:
                candidates.append((score, candidate["name"], candidate_id))
        if candidates:
            return candidates
        return [
            (10, manifest_index[hinted_hub]["name"], hinted_hub)
            for hinted_hub in sorted(preferred_hubs)
            if hinted_hub in manifest_index and hinted_hub != addon_id
        ]

    def _compute_inventory_metadata(
        self,
        manifests: dict[str, dict[str, Any]],
    ) -> InventoryMetadata:
        manifest_index = {
            addon_id: self._manifest_inventory_record(addon_id, manifest_data)
            for addon_id, manifest_data in manifests.items()
        }
        spoke_to_hub: dict[str, str] = {}
        for addon_id, metadata in manifest_index.items():
            if "provider" not in set(metadata["roles"]):
                continue
            candidates = self._hub_candidates(addon_id, metadata, manifest_index)
            if candidates:
                candidates.sort(
                    key=lambda item: (-item[0], item[1].casefold(), item[2])
                )
                spoke_to_hub[addon_id] = candidates[0][2]
        hub_to_spokes: dict[str, list[str]] = {}
        for spoke_id, hub_id in spoke_to_hub.items():
            hub_to_spokes.setdefault(hub_id, []).append(spoke_id)
        for spoke_ids in hub_to_spokes.values():
            spoke_ids.sort()
        return manifest_index, spoke_to_hub, hub_to_spokes

    def _cached_inventory_metadata(
        self,
        manifests: dict[str, dict[str, Any]],
        generation: int,
    ) -> InventoryMetadata:
        with self._inventory_cache_lock():
            cached = getattr(self, "_addon_inventory_metadata_cache", None)
            metrics = self._inventory_cache_metrics()
            if cached is not None and cached[0] == generation:
                metrics["hits"] = int(metrics["hits"] or 0) + 1
                return cached[1]
            metrics["misses"] = int(metrics["misses"] or 0) + 1
            started_at = time.perf_counter()
            try:
                metadata = self._compute_inventory_metadata(manifests)
                current_bytes = len(
                    json.dumps(
                        metadata,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode("utf-8")
                )
            except KeyError, TypeError, ValueError:
                metrics["build_errors"] = int(metrics["build_errors"] or 0) + 1
                raise
            build_ms = (time.perf_counter() - started_at) * 1000
            metrics["builds"] = int(metrics["builds"] or 0) + 1
            metrics["last_build_ms"] = round(build_ms, 3)
            metrics["total_build_ms"] = round(
                float(metrics["total_build_ms"] or 0.0) + build_ms,
                3,
            )
            metrics["current_bytes"] = current_bytes
            self._addon_inventory_last_build_at = (
                datetime.now(UTC).isoformat().replace("+00:00", "Z")
            )
            self._addon_inventory_metadata_cache = (generation, metadata)
            return metadata

    def _inventory_projection_snapshot(
        self,
    ) -> tuple[dict[str, dict[str, Any]], InventoryMetadata]:
        with self._inventory_manifest_lock():
            manifests = dict(self.manifests)
            generation = int(getattr(self, "_addon_inventory_generation", 0))
            metadata = self._cached_inventory_metadata(manifests, generation)
            return manifests, metadata

    def _inventory_metadata(self) -> InventoryMetadata:
        return self._inventory_projection_snapshot()[1]

    def _load_inventory_database(
        self,
        addon_ids: set[str] | None = None,
        *,
        requester_user_id: int | None = None,
        requester_is_admin: bool = False,
    ) -> InventoryDatabase:
        """Load public state plus counts visible to one explicit requester.

        Administrators retain the installation-wide aggregate used by the
        management UI. Other authenticated users see owned or actively granted
        Connections and enabled bindings at installation scope or their own
        user scope. Context-free callers receive no user-sensitive counts.
        """
        context = nullcontext() if has_app_context() else self.app.app_context()
        with context:
            addon_statement = select(Addon).order_by(Addon.id)
            if addon_ids is not None:
                addon_statement = addon_statement.where(Addon.id.in_(addon_ids))
            db_addons = {
                addon.id: addon for addon in db.session.scalars(addon_statement)
            }

            connection_counts: dict[str, int] = {}
            consumer_binding_counts: dict[str, int] = {}
            provider_binding_counts: dict[str, int] = {}
            if requester_user_id is None:
                return (
                    db_addons,
                    connection_counts,
                    consumer_binding_counts,
                    provider_binding_counts,
                )

            connection_statement = select(
                AddonConnection.connector_addon_id,
                func.count().label("connection_count"),
            ).group_by(AddonConnection.connector_addon_id)
            if not requester_is_admin:
                visible_connection_ids = union_all(
                    select(AddonConnection.id).where(
                        AddonConnection.owner_user_id == int(requester_user_id)
                    ),
                    select(ConnectionGrant.connection_id).where(
                        ConnectionGrant.grantee_user_id == int(requester_user_id),
                        ConnectionGrant.revoked_at.is_(None),
                    ),
                )
                connection_statement = connection_statement.where(
                    AddonConnection.id.in_(visible_connection_ids)
                )
            if addon_ids is not None:
                connection_statement = connection_statement.where(
                    AddonConnection.connector_addon_id.in_(addon_ids)
                )
            connection_counts = {
                connector_addon_id: int(connection_count)
                for connector_addon_id, connection_count in db.session.execute(
                    connection_statement
                )
            }

            consumer_bindings = select(
                CapabilityBinding.consumer_addon_id.label("addon_id"),
                literal("consumer").label("direction"),
            ).where(CapabilityBinding.status == "enabled")
            provider_bindings = select(
                CapabilityBinding.provider_addon_id.label("addon_id"),
                literal("provider").label("direction"),
            ).where(CapabilityBinding.status == "enabled")
            if not requester_is_admin:
                visible_binding_scope = or_(
                    CapabilityBinding.scope == "installation",
                    (
                        (CapabilityBinding.scope == "user")
                        & (CapabilityBinding.scope_id == str(int(requester_user_id)))
                    ),
                )
                consumer_bindings = consumer_bindings.where(visible_binding_scope)
                provider_bindings = provider_bindings.where(visible_binding_scope)
            if addon_ids is not None:
                consumer_bindings = consumer_bindings.where(
                    CapabilityBinding.consumer_addon_id.in_(addon_ids)
                )
                provider_bindings = provider_bindings.where(
                    CapabilityBinding.provider_addon_id.in_(addon_ids)
                )
            binding_rows = union_all(
                consumer_bindings,
                provider_bindings,
            ).subquery()
            binding_statement = (
                select(
                    binding_rows.c.addon_id,
                    binding_rows.c.direction,
                    func.count().label("binding_count"),
                )
                .group_by(binding_rows.c.addon_id, binding_rows.c.direction)
                .order_by(binding_rows.c.addon_id, binding_rows.c.direction)
            )
            for bound_addon_id, direction, binding_count in db.session.execute(
                binding_statement
            ):
                target = (
                    consumer_binding_counts
                    if direction == "consumer"
                    else provider_binding_counts
                )
                target[bound_addon_id] = int(binding_count)

        return (
            db_addons,
            connection_counts,
            consumer_binding_counts,
            provider_binding_counts,
        )

    @staticmethod
    def _inventory_search_terms(
        contract: dict[str, Any],
        notices: list[dict[str, Any]],
    ) -> list[str]:
        values: list[str] = []
        for key in ("capability_requirements", "capability_providers"):
            for record in contract.get(key) or []:
                if not isinstance(record, dict):
                    continue
                values.extend(
                    str(value)
                    for value in (
                        record.get("id"),
                        record.get("capability"),
                        *(record.get("operations") or []),
                    )
                    if value
                )
        for notice in notices:
            values.extend(
                str(value)
                for value in (
                    notice.get("license"),
                    notice.get("purpose"),
                    *(notice.get("packages") or []),
                )
                if value
            )
        return list(dict.fromkeys(values))

    @staticmethod
    def _inferred_hub(
        addon_id: str,
        manifest_index: ManifestInventory,
        spoke_to_hub: dict[str, str],
        hub_to_spokes: dict[str, list[str]],
    ) -> dict[str, Any]:
        relation = "standalone"
        hub_id = None
        if addon_id in spoke_to_hub:
            relation = "spoke"
            hub_id = spoke_to_hub[addon_id]
        elif addon_id in hub_to_spokes:
            relation = "hub"
            hub_id = addon_id
        return {
            "relation": relation,
            "hub_id": hub_id,
            "hub_label": (
                manifest_index.get(hub_id, {}).get("name") if hub_id else None
            ),
            "spoke_count": len(hub_to_spokes.get(addon_id, [])),
        }
