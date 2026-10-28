"""Typed manager contract for native add-on package runtime publication."""

from __future__ import annotations

import threading
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Protocol

from iarf_assistant.core.addon_contracts import AddonRegistries
from iarf_assistant.core.addons.contracts import AddonSourceKind
from iarf_assistant.core.addons.discovery import DiscoveredAddon


class AddonPackageRuntimeHost(Protocol):
    """Manager state used by discovery and native runtime publication."""

    app: Any
    manage_lifecycle: bool
    allow_lifecycle_maintenance: bool
    builtin_addons_dir: Path
    installed_addons_dir: Path
    manifests: dict[str, dict[str, Any]]

    @property
    def allowed_addon_ids(self) -> frozenset[str] | None: ...

    loaded_addons: dict[str, Any]
    automations: dict[str, Any]
    plugins: dict[str, Any]
    helpers: dict[str, Any]
    lifecycle: Any
    _addon_dirs: dict[str, Path]
    _addon_packages: dict[str, DiscoveredAddon]
    _builtin_reconciliation_required: dict[str, dict[str, str]]
    _dispatch_enabled_addons: set[str]
    _startup_metrics: dict[str, Any]
    registries: AddonRegistries

    def get_addon_directory(self, addon_id: str) -> Path: ...

    def _addon_source_kind(self, addon_id: str) -> AddonSourceKind: ...

    def _ensure_addon_python_path(self, addon_id: str) -> Path | None: ...

    def _inventory_manifest_lock(self) -> threading.RLock: ...

    def _lifecycle_mutation_lock(self) -> threading.RLock: ...

    def invalidate_addon_inventory(self, reason: str = ...) -> None: ...

    def invalidate_frontend_build_statuses(
        self,
        addon_ids: Iterable[object] | str,
    ) -> None: ...

    def rebuild_registries(self) -> None: ...

    def _compile_registries(
        self,
        *,
        manifests: dict[str, dict[str, Any]] | None = ...,
        loaded_addons: dict[str, Any] | None = ...,
        addon_dirs: dict[str, Path] | None = ...,
    ) -> AddonRegistries: ...

    def _unload_addon_runtime(self, addon_id: str) -> None: ...

    def _unload_addon_runtime_locked(self, addon_id: str) -> None: ...


__all__ = ["AddonPackageRuntimeHost"]
