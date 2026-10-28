"""Frontend build inspection, caching, logging, and execution."""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import threading
from collections.abc import Iterable
from contextlib import AbstractContextManager, nullcontext
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast

from ..addon_contracts import ACTIVE, DEGRADED, AddonElements, ElementStatus
from ..frontend_builds import (
    STATUS_BUILT,
    STATUS_FAILED,
    STATUS_MISCONFIGURED,
    STATUS_MISSING,
    STATUS_STALE,
    build_expected_stamp_metadata,
    inspect_frontend_build,
    load_frontend_build_config,
    run_frontend_build,
)
from .frontend_cache_generation import frontend_package_generation_identity

logger = logging.getLogger(__name__)
_CACHE_LOCK_INITIALIZATION = threading.Lock()


class FrontendBuildHost(Protocol):
    """Manager surface required by frontend build orchestration."""

    app: Any
    manifests: dict[str, dict[str, Any]]

    def _append_registered_log(self, source_id: str, message: str) -> None: ...

    def _get_addon_manifest(self, addon_id: str) -> dict[str, Any] | None: ...

    def get_addon_directory(self, addon_id: str) -> Path: ...

    def _inventory_manifest_lock(self) -> threading.RLock: ...


if TYPE_CHECKING:

    class _FrontendBuildOrchestrationMixinBase(FrontendBuildHost):
        pass

else:

    class _FrontendBuildOrchestrationMixinBase:
        pass


class FrontendBuildOrchestrationMixin(_FrontendBuildOrchestrationMixinBase):
    """Own add-on frontend build state without coupling routes to the manager."""

    def _frontend_build_cache_lock(self) -> threading.RLock:
        lock = getattr(self, "_frontend_build_status_cache_lock", None)
        if lock is None:
            with _CACHE_LOCK_INITIALIZATION:
                lock = getattr(self, "_frontend_build_status_cache_lock", None)
                if lock is None:
                    lock = threading.RLock()
                    self._frontend_build_status_cache_lock = lock
        return lock

    def _frontend_build_singleflight_lock(
        self,
        addon_id: str,
    ) -> threading.RLock:
        with _CACHE_LOCK_INITIALIZATION:
            locks = getattr(self, "_frontend_build_status_singleflight_locks", None)
            if not isinstance(locks, dict):
                locks = {}
                self._frontend_build_status_singleflight_locks = locks
            lock = locks.get(addon_id)
            if lock is None:
                lock = threading.RLock()
                locks[addon_id] = lock
            return lock

    def _frontend_build_manifest_guard(self) -> AbstractContextManager[Any]:
        lock_factory = getattr(self, "_inventory_manifest_lock", None)
        guard = lock_factory() if callable(lock_factory) else nullcontext()
        return cast(AbstractContextManager[Any], guard)

    @staticmethod
    def _canonical_digest(payload: object) -> str:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _frontend_build_generation(
        self,
        addon_id: str,
        manifest: dict[str, Any] | None,
        addon_dir: Path,
    ) -> str:
        """Fingerprint the exact selected package and its build contract."""

        package = (getattr(self, "_addon_packages", None) or {}).get(addon_id)
        build_inputs: dict[str, Any] | None = None
        build_errors: list[str] = []
        if manifest is not None:
            config, build_errors = load_frontend_build_config(
                manifest,
                addon_dir,
            )
            if config is not None:
                build_inputs = build_expected_stamp_metadata(config)

        payload = {
            "schema": 1,
            "addon_id": addon_id,
            "package_dir": str(addon_dir.resolve()),
            "manifest": manifest,
            "frontend": (manifest.get("frontend") if manifest is not None else None),
            "frontend_build_inputs": build_inputs,
            "frontend_build_errors": build_errors,
            "package_generation": frontend_package_generation_identity(package),
        }
        return self._canonical_digest(payload)

    def _frontend_build_snapshot(
        self,
        addon_id: str,
    ) -> tuple[dict[str, Any] | None, Path, str]:
        manifest = self._get_addon_manifest(addon_id)
        manifest_copy = copy.deepcopy(manifest) if manifest is not None else None
        addon_dir = self.get_addon_directory(addon_id)
        generation = self._frontend_build_generation(
            addon_id,
            manifest_copy,
            addon_dir,
        )
        return manifest_copy, addon_dir, generation

    def _frontend_build_cache_state(
        self,
    ) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
        cache = getattr(self, "_frontend_build_status_cache", None)
        if not isinstance(cache, dict):
            cache = {}
            self._frontend_build_status_cache = cache
        generations = getattr(
            self,
            "_frontend_build_status_cache_generations",
            None,
        )
        if not isinstance(generations, dict):
            generations = {}
            self._frontend_build_status_cache_generations = generations
        return cache, generations

    def _register_frontend_build_log_sources(self, addon_id: str) -> None:
        registry = getattr(self.app, "log_registry", None)
        if not registry:
            return
        try:
            registry.register_addon_sources(addon_id)
        except Exception as exc:  # noqa: BLE001 - optional foreign logging hook.
            logger.warning(
                "Failed to register frontend build log source for add-on '%s': %s",
                addon_id,
                exc,
            )

    def _append_frontend_build_log(self, addon_id: str, message: str) -> None:
        self._register_frontend_build_log_sources(addon_id)
        self._append_registered_log("addons.build", f"addon_id={addon_id} {message}")
        self._append_registered_log(f"addon.{addon_id}.build", message)

    def _frontend_manifest_and_dir(self, addon_id: str) -> tuple[dict | None, Path]:
        manifest = self._get_addon_manifest(addon_id)
        return manifest, self.get_addon_directory(addon_id)

    @staticmethod
    def _merge_element_status(
        current: ElementStatus,
        state: str,
        reasons: list[str],
    ) -> ElementStatus:
        if not reasons:
            return current
        if current.state == ACTIVE:
            return ElementStatus(state, reasons)
        if current.state == DEGRADED:
            return ElementStatus(state, [*current.reasons, *reasons])
        return current

    def _apply_frontend_build_status(
        self,
        elements: AddonElements,
        build_status: dict[str, Any],
    ) -> None:
        elements.package.frontend_build = self._frontend_build_public_status(
            build_status
        )
        if elements.package.status.state not in {ACTIVE, DEGRADED}:
            return
        if not build_status.get("declared"):
            return

        state = build_status.get("status")
        if state == STATUS_BUILT:
            return
        if state not in {
            STATUS_MISSING,
            STATUS_STALE,
            STATUS_FAILED,
            STATUS_MISCONFIGURED,
        }:
            return

        reasons = list(build_status.get("reasons") or [])
        reasons.extend(build_status.get("warnings") or [])
        if not reasons:
            reasons = [f"Frontend build status is {state}."]

        target_state = (
            STATUS_MISCONFIGURED if state == STATUS_MISCONFIGURED else DEGRADED
        )
        elements.package.status = self._merge_element_status(
            elements.package.status, target_state, reasons
        )
        for record in [*elements.ui_surfaces, *elements.helpers]:
            record.status = self._merge_element_status(
                record.status, target_state, reasons
            )

    def _cache_frontend_build_status(
        self,
        addon_id: str,
        status: dict[str, Any],
        generation: str,
    ) -> None:
        cache, generations = self._frontend_build_cache_state()
        cache[addon_id] = copy.deepcopy(status)
        generations[addon_id] = generation

    @staticmethod
    def _frontend_build_status_for_validation_mode(
        status: dict[str, Any],
        validation_mode: str,
    ) -> dict[str, Any]:
        """Copy a status while describing the evidence available to this read."""

        snapshot = copy.deepcopy(status)
        snapshot["validation_mode"] = validation_mode
        return snapshot

    def invalidate_frontend_build_statuses(
        self,
        addon_ids: Iterable[object] | str,
    ) -> None:
        """Discard cached statuses for atomically replaced package selections."""

        if isinstance(addon_ids, str):
            normalized_ids = {addon_ids.strip()} if addon_ids.strip() else set()
        else:
            normalized_ids = {
                str(addon_id).strip() for addon_id in addon_ids if str(addon_id).strip()
            }
        if not normalized_ids:
            return
        with self._frontend_build_cache_lock():
            cache, generations = self._frontend_build_cache_state()
            for addon_id in normalized_ids:
                cache.pop(addon_id, None)
                generations.pop(addon_id, None)

    @staticmethod
    def _missing_frontend_manifest_status(addon_id: str) -> dict[str, Any]:
        return {
            "declared": False,
            "addon_id": addon_id,
            "status": "missing_manifest",
            "rebuild_needed": False,
            "reasons": [f"Addon manifest not found for '{addon_id}'."],
            "warnings": [],
            "output_paths": [],
            "missing_outputs": [],
            "log_source_id": f"addon.{addon_id}.build",
        }

    def get_frontend_build_status(
        self,
        addon_id: str,
        *,
        refresh: bool = False,
    ) -> dict[str, Any]:
        """Return one frontend build status.

        ``refresh=False`` returns a generation-bound metadata snapshot. It does
        not prove that source files or outputs still match the cached result.
        Callers that make a startup or activation safety decision must request
        ``refresh=True`` for a new full source and output validation.
        """
        singleflight = self._frontend_build_singleflight_lock(addon_id)
        with singleflight:
            while True:
                with self._frontend_build_manifest_guard():
                    manifest, addon_dir, generation = self._frontend_build_snapshot(
                        addon_id
                    )
                with self._frontend_build_cache_lock():
                    cache, generations = self._frontend_build_cache_state()
                    if (
                        not refresh
                        and generations.get(addon_id) == generation
                        and addon_id in cache
                    ):
                        return self._frontend_build_status_for_validation_mode(
                            cache[addon_id],
                            "metadata",
                        )

                status = (
                    self._missing_frontend_manifest_status(addon_id)
                    if manifest is None
                    else inspect_frontend_build(
                        manifest,
                        addon_dir,
                        verify_sources=refresh,
                    )
                )
                status = self._frontend_build_status_for_validation_mode(
                    status,
                    "full" if refresh else "metadata",
                )
                with self._frontend_build_manifest_guard():
                    _manifest, _addon_dir, current_generation = (
                        self._frontend_build_snapshot(addon_id)
                    )
                    if current_generation != generation:
                        continue
                    with self._frontend_build_cache_lock():
                        cache, generations = self._frontend_build_cache_state()
                        cache.pop(addon_id, None)
                        generations.pop(addon_id, None)
                        self._cache_frontend_build_status(
                            addon_id,
                            status,
                            generation,
                        )
                return copy.deepcopy(status)

    def build_frontend_assets(
        self, addon_id: str, watch: bool = False, force: bool = False
    ) -> dict[str, Any]:
        """Runs one addon's declared frontend build or watch command."""
        with self._frontend_build_manifest_guard():
            manifest, addon_dir, generation = self._frontend_build_snapshot(addon_id)
            with self._frontend_build_cache_lock():
                cache, generations = self._frontend_build_cache_state()
                cache.pop(addon_id, None)
                generations.pop(addon_id, None)
        if not manifest:
            raise FileNotFoundError(f"Addon manifest not found for '{addon_id}'.")

        before = inspect_frontend_build(manifest, addon_dir)
        if not before.get("declared"):
            return {
                "success": True,
                "message": "No frontend build is declared.",
                "status": before,
                "stdout": "",
                "stderr": "",
                "returncode": 0,
            }

        action = "watch" if watch else "build"
        self._append_frontend_build_log(
            addon_id, f"Starting frontend {action}; force={force}."
        )
        result = run_frontend_build(manifest, addon_dir, watch=watch, force=force)

        stdout = result.get("stdout") or ""
        stderr = result.get("stderr") or ""
        if stdout:
            self._append_frontend_build_log(addon_id, "stdout:\n" + stdout)
        if stderr:
            self._append_frontend_build_log(addon_id, "stderr:\n" + stderr)

        status = result.get("status", {})
        with self._frontend_build_manifest_guard():
            _current_manifest, _current_dir, current_generation = (
                self._frontend_build_snapshot(addon_id)
            )
            if current_generation == generation:
                with self._frontend_build_cache_lock():
                    self._cache_frontend_build_status(
                        addon_id,
                        status,
                        generation,
                    )
        status_name = status.get("status", "unknown")
        if result.get("success"):
            self._append_frontend_build_log(
                addon_id, f"Frontend {action} completed; status={status_name}."
            )
        else:
            self._append_frontend_build_log(
                addon_id,
                f"Frontend {action} failed; status={status_name}; error={result.get('message')}",
            )
        return copy.deepcopy(result)

    def build_all_frontend_assets(
        self, watch: bool = False, force: bool = False
    ) -> list[dict[str, Any]]:
        """Runs declared frontend builds for all discovered addons."""
        results = []
        for addon_id in sorted(self.manifests.keys()):
            status = self.get_frontend_build_status(
                addon_id,
                refresh=True,
            )
            if not status.get("declared"):
                continue
            result = self.build_frontend_assets(addon_id, watch=watch, force=force)
            result["addon_id"] = addon_id
            results.append(result)
        return results

    def ensure_frontend_assets(self, addon_id: str) -> dict[str, Any]:
        """Checks declared frontend assets without forcing runtime builds during startup/activation."""
        status = self.get_frontend_build_status(addon_id, refresh=True)
        if not status.get("declared") or not status.get("rebuild_needed"):
            return status

        if status.get("status") == STATUS_STALE and not status.get("command"):
            self._append_frontend_build_log(
                addon_id,
                "Frontend assets are stale but no build command is declared; existing outputs will be used.",
            )
            return status

        self._append_frontend_build_log(
            addon_id,
            "Frontend assets require rebuild, but automatic builds are disabled. "
            "Expected prebuilt artifacts under addon static/.",
        )
        return status

    @staticmethod
    def _frontend_build_public_status(
        status: dict[str, Any] | None,
    ) -> dict[str, Any]:
        if not status:
            return {}
        public_keys = (
            "declared",
            "addon_id",
            "tier",
            "status",
            "rebuild_needed",
            "source_available",
            "reasons",
            "warnings",
            "source_dir",
            "command",
            "watch_command",
            "output_paths",
            "missing_outputs",
            "stamp_path",
            "stamp_current",
            "validation_mode",
            "log_source_id",
        )
        return {key: status.get(key) for key in public_keys if key in status}


__all__ = ["FrontendBuildHost", "FrontendBuildOrchestrationMixin"]
