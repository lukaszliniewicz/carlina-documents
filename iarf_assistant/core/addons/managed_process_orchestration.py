"""Pixi environment provisioning and supervised add-on worker lifecycle.

The Flask-facing add-on manager owns discovery, registries, and activation. This
module owns managed-process mechanics: immutable DataRoot environment paths,
Pixi command execution, process launch policy, tracking, logs, and cleanup.
Add-ons reach this boundary through :class:`AddonContext`; they do not need the
manager implementation or a concrete environment-manager SDK.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import sysconfig
import threading
import time
from collections.abc import Iterable, Mapping
from dataclasses import asdict
from pathlib import Path
from typing import Any, Protocol

from carlina.runtime_environment import is_worker_process, mark_worker_environment
from iarf_assistant.core.app_paths import (
    load_runtime_environment,
    worker_ownership_xoption,
)
from iarf_assistant.core.managed_worker_quiescence import (
    managed_worker_quiescence_gate,
)

from .contracts import (
    DEPENDENCY_ENVIRONMENT_ALIASES,
    AddonSourceKind,
    DependencyEnvironment,
)
from .environments import (
    AddonEnvironmentPaths,
    dependency_fingerprint,
    dependency_generation_name,
    prepare_generated_pixi_project,
    prepare_pixi_project,
    python_environment_fingerprint,
    resolve_environment_paths,
)
from .managed_process_runtime import (
    PixiManagedProcessRuntimeBackend,
    managed_process_creation_flags,
)

logger = logging.getLogger(__name__)

WORKER_ENV_AUTO = "auto"
WORKER_ENV_APP = "app"
WORKER_ENV_ADDON_PIXI = "addon_pixi"
VALID_WORKER_EXECUTION_ENVIRONMENTS = frozenset(
    {WORKER_ENV_AUTO, WORKER_ENV_APP, WORKER_ENV_ADDON_PIXI}
)
WORKER_APP_RUNTIME_BRIDGE_NONE = "none"
WORKER_APP_RUNTIME_BRIDGE_HOST_V1 = "host_v1"
VALID_WORKER_APP_RUNTIME_BRIDGES = frozenset(
    {WORKER_APP_RUNTIME_BRIDGE_NONE, WORKER_APP_RUNTIME_BRIDGE_HOST_V1}
)

DEPENDENCY_ENV_IN_PROCESS_VENDOR = DependencyEnvironment.IN_PROCESS_VENDOR.value
DEPENDENCY_ENV_MANAGED_PIXI = DependencyEnvironment.MANAGED_PROCESS_PIXI.value
LEGACY_DEPENDENCY_ENVIRONMENTS = {
    key: value.value for key, value in DEPENDENCY_ENVIRONMENT_ALIASES.items()
}

_GLOBAL_WORKER_LOCK = threading.RLock()
_GLOBAL_WORKERS_BY_ADDON: dict[str, list] = {}
_GLOBAL_WORKER_LOG_HANDLES: dict[str, list] = {}
_GLOBAL_WORKER_TARGET_COUNTS: dict[str, int] = {}
_GLOBAL_WORKER_ENTRYPOINTS: dict[str, str] = {}
_GLOBAL_WORKER_EXECUTION_ENVIRONMENTS: dict[str, str] = {}


class ManagedProcessHost(Protocol):
    """Host surface required by the managed-process orchestration mixin."""

    app: object
    manifests: dict
    loaded_addons: dict
    registries: object

    def get_addon_directory(self, addon_id: str) -> Path: ...

    def _addon_source_kind(self, addon_id: str) -> AddonSourceKind: ...


def _is_process_running(process) -> bool:
    try:
        return process.poll() is None
    except Exception:  # noqa: BLE001 - foreign process handles are best effort.
        return False


def _worker_ownership_root(app) -> Path:
    """Resolve the canonical data root used in all add-on worker argv."""

    paths = (getattr(app, "extensions", None) or {}).get("carlina_paths")
    if paths is not None:
        return Path(paths.data_root)
    package_root = Path(getattr(app, "root_path", Path(__file__).resolve().parents[1]))
    return load_runtime_environment(
        package_root.parent,
        package_root=package_root,
    ).data_root


def _worker_temporary_directory(data_root: Path, addon_id: str) -> Path:
    """Create the data-root-owned scratch directory for a managed worker."""

    if (
        not isinstance(addon_id, str)
        or not addon_id
        or addon_id in {".", ".."}
        or any(character in addon_id for character in ("/", "\\", "\0"))
    ):
        raise ValueError("Managed worker addon_id must be one filesystem component.")

    directory = data_root / "tmp" / "managed-workers" / addon_id
    directory.mkdir(parents=True, exist_ok=True)
    return directory


class ManagedProcessOrchestrationMixin:
    """Reusable manager mixin implementing strict Pixi worker orchestration."""

    def _initialize_managed_process_orchestration(self) -> None:
        self._active_workers = []
        self._active_workers_by_addon = _GLOBAL_WORKERS_BY_ADDON
        self._worker_log_handles = _GLOBAL_WORKER_LOG_HANDLES
        self._worker_target_counts = _GLOBAL_WORKER_TARGET_COUNTS
        self._worker_entrypoints = _GLOBAL_WORKER_ENTRYPOINTS
        self._worker_execution_environments = (
            _GLOBAL_WORKER_EXECUTION_ENVIRONMENTS
        )
        self._worker_lock = _GLOBAL_WORKER_LOCK

    def _addon_environment_paths(self, addon_id: str, python_environment=None):
        """Resolve rebuildable dependency paths without touching program files."""

        app = getattr(self, "app", None)
        if app is None:
            raise RuntimeError("The Flask application has not been configured.")
        app_paths = (getattr(app, "extensions", None) or {}).get("carlina_paths")
        environments_root = (
            Path(app_paths.addon_environments_dir)
            if app_paths is not None
            else Path(app.instance_path) / "addons" / "environments"
        )
        package = (getattr(self, "_addon_packages", None) or {}).get(str(addon_id))
        contract = getattr(package, "contract", None)
        dependency_lock = (
            getattr(python_environment, "lock", None)
            if python_environment is not None
            else getattr(contract, "dependency_lock", None)
        )
        dependency_kind = (
            getattr(python_environment, "environment", None)
            if python_environment is not None
            else getattr(contract, "dependency_environment", None)
        )
        explicit_environments = tuple(
            getattr(contract, "python_environments", ()) or ()
        )
        explicit_environment_shape = bool(
            isinstance(getattr(contract, "manifest", None), Mapping)
            and isinstance(
                ((contract.manifest.get("dependencies") or {}).get("python") or {}),
                Mapping,
            )
            and "environments"
            in ((contract.manifest.get("dependencies") or {}).get("python") or {})
        )
        generation = (
            (
                dependency_generation_name(
                    "default",
                    dependency_lock.sha256,
                    contract.contract_sha256,
                )
                if dependency_kind == DependencyEnvironment.MANAGED_PROCESS_PIXI
                else dependency_generation_name(
                    "default",
                    dependency_lock.sha256,
                )
            )
            if self._addon_source_kind(addon_id) == AddonSourceKind.INSTALLED
            and dependency_lock is not None
            else None
        )
        if python_environment is not None and (
            explicit_environment_shape or len(explicit_environments) > 1
        ):
            environment_id = str(getattr(python_environment, "id", "default"))
            dependency_generation = (
                dependency_lock.sha256
                if dependency_lock is not None
                else dependency_fingerprint(self.get_addon_directory(addon_id))
            )
            contract_generation = python_environment_fingerprint(
                python_environment
            )
            generation = dependency_generation_name(
                environment_id,
                dependency_generation,
                contract_generation,
            )
        resolved_paths = resolve_environment_paths(
            environments_root,
            str(addon_id),
            self.get_addon_directory(addon_id),
            generation=generation,
        )
        if (
            generation is None
            or dependency_lock is None
            or resolved_paths.root.exists()
        ):
            return resolved_paths
        compatible = self._compatible_existing_environment_paths(
            environments_root,
            str(addon_id),
            python_environment=python_environment,
            dependency_lock_sha256=dependency_lock.sha256,
        )
        return compatible or resolved_paths

    def _compatible_existing_environment_paths(
        self,
        environments_root: Path,
        addon_id: str,
        *,
        python_environment,
        dependency_lock_sha256: str,
    ) -> AddonEnvironmentPaths | None:
        """Reuse a marker-compatible generation after naming-policy changes."""

        addon_root = (Path(environments_root).resolve() / addon_id).resolve()
        try:
            addon_root.relative_to(Path(environments_root).resolve())
        except ValueError:
            return None
        if not addon_root.is_dir():
            return None
        environment_id = str(
            getattr(python_environment, "id", "default") or "default"
        )
        environment_kind = getattr(
            python_environment,
            "environment",
            DependencyEnvironment.IN_PROCESS_VENDOR,
        )
        environment_kind_value = str(
            getattr(environment_kind, "value", environment_kind) or ""
        )
        is_in_process = (
            environment_kind_value
            == DependencyEnvironment.IN_PROCESS_VENDOR.value
        )
        expected_fingerprint = (
            python_environment_fingerprint(python_environment)
            if python_environment is not None
            else ""
        )
        expected_python = (
            self._in_process_python_identity()
            if is_in_process
            and hasattr(self, "_in_process_python_identity")
            else {}
        )
        marker_candidates: list[tuple[int, Path]] = []
        for marker in addon_root.glob("*/environment.json"):
            try:
                marker_candidates.append((marker.stat().st_mtime_ns, marker))
            except OSError:
                continue
        for _mtime_ns, marker in sorted(
            marker_candidates,
            key=lambda item: item[0],
            reverse=True,
        ):
            try:
                generation_root = marker.parent.resolve()
                generation_root.relative_to(addon_root)
                metadata = json.loads(marker.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, ValueError, TypeError):
                continue
            if not isinstance(metadata, dict):
                continue
            stored_addon_id = str(metadata.get("addon_id") or "")
            if stored_addon_id and stored_addon_id != addon_id:
                continue
            if metadata.get("lock_sha256") != dependency_lock_sha256:
                continue
            if str(metadata.get("environment_id") or "default") != environment_id:
                continue
            stored_fingerprint = str(
                metadata.get("environment_fingerprint") or ""
            )
            if (
                stored_fingerprint
                and expected_fingerprint
                and stored_fingerprint != expected_fingerprint
            ):
                continue
            candidate = resolve_environment_paths(
                environments_root,
                addon_id,
                self.get_addon_directory(addon_id),
                generation=generation_root.name,
            )
            if is_in_process:
                if not candidate.lib_dir.is_dir() or any(
                    metadata.get(key) != value
                    for key, value in expected_python.items()
                ):
                    continue
            else:
                if not (
                    candidate.pixi_project_dir / ".pixi" / "envs" / "default"
                ).is_dir():
                    continue
                stored_kind = str(
                    metadata.get("dependency_environment") or ""
                )
                if stored_kind and stored_kind != environment_kind_value:
                    continue
            return candidate
        return None

    def _contract_python_environments(self, addon_id: str) -> tuple:
        package = (getattr(self, "_addon_packages", None) or {}).get(str(addon_id))
        contract = getattr(package, "contract", None)
        return tuple(getattr(contract, "python_environments", ()) or ())

    def _python_environment_for_runtime(
        self,
        addon_id: str,
        runtime_unit: str | None = None,
        *,
        environment: DependencyEnvironment | None = None,
    ):
        candidates = self._contract_python_environments(addon_id)
        if runtime_unit:
            candidates = tuple(
                item
                for item in candidates
                if str(runtime_unit) in tuple(getattr(item, "runtime_units", ()) or ())
            )
        if environment is not None:
            candidates = tuple(
                item
                for item in candidates
                if getattr(item, "environment", None) == environment
            )
        if not candidates:
            return None
        if len(candidates) > 1:
            qualifier = (
                f"runtime unit {runtime_unit!r}"
                if runtime_unit
                else f"environment kind {environment.value!r}"
                if environment is not None
                else "the requested operation"
            )
            raise RuntimeError(
                f"Add-on '{addon_id}' has multiple Python environments for {qualifier}; "
                "the runtime unit must be explicit."
            )
        return candidates[0]

    def _get_pixi_runtime_backend(self) -> PixiManagedProcessRuntimeBackend:
        backend = getattr(self, "_pixi_runtime_backend", None)
        if backend is None:
            paths = (getattr(self.app, "extensions", None) or {}).get(
                "carlina_paths"
            )
            project_root = (
                Path(paths.program_root)
                if paths is not None
                else Path(self.app.root_path).parent
            )
            backend = PixiManagedProcessRuntimeBackend(project_root)
            self._pixi_runtime_backend = backend
        return backend

    def _get_pixi_binary(self) -> Path:
        """Return the sole supported managed-process environment executable."""

        return self._get_pixi_runtime_backend().resolve_binary()

    def _manifest_python_dependencies(self, addon_id: str) -> dict:
        manifest = (getattr(self, "manifests", None) or {}).get(addon_id)
        if not isinstance(manifest, dict):
            manifest_path = self.get_addon_directory(addon_id) / "manifest.json"
            if manifest_path.is_file():
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        dependencies = (manifest or {}).get("dependencies") or {}
        python_dependencies = dependencies.get("python") or {}
        return python_dependencies if isinstance(python_dependencies, dict) else {}

    def _addon_declares_pixi_environment(
        self,
        addon_id: str,
        runtime_unit: str | None = None,
    ) -> bool:
        if runtime_unit is None and any(
            getattr(item, "environment", None)
            == DependencyEnvironment.MANAGED_PROCESS_PIXI
            for item in self._contract_python_environments(addon_id)
        ):
            return True
        declared_environment = self._python_environment_for_runtime(
            addon_id,
            runtime_unit,
            environment=DependencyEnvironment.MANAGED_PROCESS_PIXI,
        )
        if declared_environment is not None:
            return True
        package = (getattr(self, "_addon_packages", None) or {}).get(addon_id)
        contract = getattr(package, "contract", None)
        if getattr(contract, "dependency_environment", None) == (
            DependencyEnvironment.MANAGED_PROCESS_PIXI
        ):
            return True
        dependencies = self._manifest_python_dependencies(addon_id)
        raw = str(
            dependencies.get("environment") or dependencies.get("tier") or ""
        ).strip()
        resolved = LEGACY_DEPENDENCY_ENVIRONMENTS.get(raw, raw)
        return resolved == DEPENDENCY_ENV_MANAGED_PIXI

    def _prepare_addon_pixi_project(
        self,
        addon_id: str,
        *,
        runtime_unit: str | None = None,
        python_environment=None,
        python_version: str | None = None,
        conda_packages: list[str] | tuple[str, ...] | None = None,
        pip_packages: list[str] | tuple[str, ...] | None = None,
    ) -> Path:
        selected_environment = python_environment or self._python_environment_for_runtime(
            addon_id,
            runtime_unit,
            environment=DependencyEnvironment.MANAGED_PROCESS_PIXI,
        )
        if (
            runtime_unit is not None
            and selected_environment is None
            and self._contract_python_environments(addon_id)
        ):
            raise RuntimeError(
                f"Add-on '{addon_id}' has no managed Pixi environment bound to "
                f"runtime unit {runtime_unit!r}."
            )
        paths = self._addon_environment_paths(addon_id, selected_environment)
        addon_dir = self.get_addon_directory(addon_id)
        declared_manifest = str(
            getattr(selected_environment, "manifest_path", "") or "pixi.toml"
        )
        declared_lock = getattr(selected_environment, "lock", None)
        declared_lock_path = str(
            getattr(declared_lock, "path", "") or "pixi.lock"
        )
        if (addon_dir / declared_manifest).is_file():
            return prepare_pixi_project(
                paths,
                addon_dir,
                manifest_path=declared_manifest,
                lock_path=declared_lock_path,
            )
        declared = self._manifest_python_dependencies(addon_id)
        return prepare_generated_pixi_project(
            paths,
            addon_id,
            python_version=str(
                python_version
                or getattr(selected_environment, "python_version", "")
                or declared.get("python_version")
                or "3.11"
            ),
            conda_packages=list(
                conda_packages
                if conda_packages is not None
                else getattr(selected_environment, "conda_packages", ())
                or declared.get("conda_packages")
                or ()
            ),
            pip_packages=list(
                pip_packages
                if pip_packages is not None
                else getattr(selected_environment, "pip_packages", ())
                or declared.get("pip_packages")
                or ()
            ),
        )

    def setup_pixi_environment(
        self,
        addon_id: str,
        conda_packages: list[str] | None = None,
        pip_packages: list[str] | None = None,
        python_version: str = "3.11",
        *,
        runtime_unit: str | None = None,
    ) -> str:
        """Create or update an add-on's managed Pixi environment."""

        addon_dir = self.get_addon_directory(addon_id)
        if not addon_dir.exists():
            raise FileNotFoundError(f"Add-on directory does not exist: {addon_dir}")
        manifest = self._prepare_addon_pixi_project(
            addon_id,
            runtime_unit=runtime_unit,
            python_version=python_version,
            conda_packages=conda_packages,
            pip_packages=pip_packages,
        )
        result = self._get_pixi_runtime_backend().install(
            manifest,
            locked=(manifest.parent / "pixi.lock").is_file(),
        )
        output = "\n".join(
            part.strip() for part in (result.stdout, result.stderr) if part.strip()
        )
        return output or f"Pixi environment is ready: {manifest.parent}"

    def lock_pixi_project(self, addon_id: str) -> Path:
        """Resolve a local-development add-on's source Pixi lock for packaging."""

        addon_dir = self.get_addon_directory(addon_id)
        manifest = addon_dir / "pixi.toml"
        if not manifest.is_file():
            raise FileNotFoundError(f"Pixi manifest not found: {manifest}")
        self._get_pixi_runtime_backend().lock(manifest)
        lockfile = addon_dir / "pixi.lock"
        if not lockfile.is_file():
            raise RuntimeError(f"Pixi did not produce the expected lockfile: {lockfile}")
        return lockfile

    def inspect_pixi_environment(
        self,
        addon_id: str,
        *,
        runtime_unit: str | None = None,
    ) -> str:
        manifest = self._prepare_addon_pixi_project(
            addon_id,
            runtime_unit=runtime_unit,
        )
        result = self._get_pixi_runtime_backend().list_packages(manifest)
        return result.stdout

    def run_managed_process(
        self,
        addon_id: str,
        argv,
        *,
        runtime_unit: str | None = None,
        input_text: str | None = None,
        timeout_seconds: float | None = None,
        check: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        """Execute one command in an add-on's DataRoot-owned Pixi project."""

        if isinstance(argv, (str, bytes)) or not argv:
            raise ValueError("Managed-process argv must be a non-empty sequence.")
        manifest = self._prepare_addon_pixi_project(
            addon_id,
            runtime_unit=runtime_unit,
        )
        return self._get_pixi_runtime_backend().execute(
            manifest,
            [str(item) for item in argv],
            input_text=input_text,
            timeout_seconds=timeout_seconds,
            check=check,
        )

    def cleanup_managed_processes(self) -> None:
        """Terminate every worker tracked by this application process."""

        for addon_id in list(self._get_worker_map()):
            self.stop_addon_workers(addon_id)

        # Older extensions may still have appended to the flat tracking list.
        for process in list(getattr(self, "_active_workers", [])):
            if not _is_process_running(process):
                continue
            logger.info("Terminating background worker PID %s...", process.pid)
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
        self._active_workers = []

    def _get_worker_lock(self):
        if not hasattr(self, "_worker_lock") or self._worker_lock is None:
            self._worker_lock = threading.RLock()
        return self._worker_lock

    def _get_worker_map(self):
        if (
            not hasattr(self, "_active_workers_by_addon")
            or self._active_workers_by_addon is None
        ):
            self._active_workers_by_addon = {}
        return self._active_workers_by_addon

    def _get_worker_log_handles(self):
        if not hasattr(self, "_worker_log_handles") or self._worker_log_handles is None:
            self._worker_log_handles = {}
        return self._worker_log_handles

    def _get_worker_target_counts(self):
        if (
            not hasattr(self, "_worker_target_counts")
            or self._worker_target_counts is None
        ):
            self._worker_target_counts = {}
        return self._worker_target_counts

    def _get_worker_entrypoints(self):
        if not hasattr(self, "_worker_entrypoints") or self._worker_entrypoints is None:
            self._worker_entrypoints = {}
        return self._worker_entrypoints

    def _get_worker_execution_environments(self):
        if (
            not hasattr(self, "_worker_execution_environments")
            or self._worker_execution_environments is None
        ):
            self._worker_execution_environments = {}
        return self._worker_execution_environments

    def _clear_worker_spawn_preferences(self, addon_id: str) -> None:
        self._get_worker_target_counts().pop(addon_id, None)
        self._get_worker_entrypoints().pop(addon_id, None)
        self._get_worker_execution_environments().pop(addon_id, None)

    def _close_worker_log_handles(self, addon_id: str) -> None:
        handles = self._get_worker_log_handles().pop(addon_id, [])
        seen: set[int] = set()
        for handle in handles:
            if id(handle) in seen:
                continue
            seen.add(id(handle))
            try:
                handle.close()
            except Exception:
                logger.debug("Could not close worker log handle.", exc_info=True)

    def _prune_worker_tracking(self, addon_id: str | None = None) -> None:
        worker_map = self._get_worker_map()
        targets = [addon_id] if addon_id else list(worker_map)

        for target_addon_id in targets:
            live = [
                process
                for process in worker_map.get(target_addon_id, [])
                if _is_process_running(process)
            ]
            if live:
                worker_map[target_addon_id] = live
            else:
                worker_map.pop(target_addon_id, None)
                self._close_worker_log_handles(target_addon_id)

        flat = []
        seen: set[int] = set()
        for processes in worker_map.values():
            for process in processes:
                if id(process) in seen or not _is_process_running(process):
                    continue
                flat.append(process)
                seen.add(id(process))

        for process in getattr(self, "_active_workers", []):
            if id(process) in seen or not _is_process_running(process):
                continue
            flat.append(process)
            seen.add(id(process))
        self._active_workers = flat

    def _live_worker_processes(self, addon_id: str) -> list:
        with self._get_worker_lock():
            self._prune_worker_tracking(addon_id)
            return list(self._get_worker_map().get(addon_id, []))

    def track_worker_process(self, addon_id: str, process, log_handle=None) -> None:
        """Track a spawned worker by owner so it can be stopped independently."""

        with self._get_worker_lock():
            worker_map = self._get_worker_map()
            addon_processes = worker_map.setdefault(addon_id, [])
            if all(id(existing) != id(process) for existing in addon_processes):
                addon_processes.append(process)

            if not hasattr(self, "_active_workers") or self._active_workers is None:
                self._active_workers = []
            if all(id(existing) != id(process) for existing in self._active_workers):
                self._active_workers.append(process)

            if log_handle is not None:
                self._get_worker_log_handles().setdefault(addon_id, []).append(
                    log_handle
                )

    def stop_addon_workers(self, addon_id: str) -> int:
        """Terminate all tracked worker processes owned by one add-on."""

        with self._get_worker_lock():
            processes = list(self._get_worker_map().pop(addon_id, []))
            process_ids = {id(process) for process in processes}
            self._active_workers = [
                process
                for process in getattr(self, "_active_workers", [])
                if id(process) not in process_ids
            ]

        stopped = 0
        for process in processes:
            if not _is_process_running(process):
                continue
            stopped += 1
            logger.info(
                "Terminating worker for addon '%s' PID %s...", addon_id, process.pid
            )
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()

        self._close_worker_log_handles(addon_id)
        if stopped:
            logger.info(
                "Stopped %s worker process(es) for addon '%s'.", stopped, addon_id
            )
        return stopped

    def terminate_addon_worker_pid(self, addon_id: str, pid: int | str) -> dict:
        """Terminate one tracked worker process owned by an add-on."""

        try:
            target_pid = int(pid)
        except (TypeError, ValueError) as exc:
            raise ValueError("pid must be an integer greater than zero.") from exc
        if target_pid < 1:
            raise ValueError("pid must be greater than zero.")

        with self._get_worker_lock():
            self._prune_worker_tracking(addon_id)
            tracked = list(self._get_worker_map().get(addon_id, []))
            target_process = None
            remaining = []
            for process in tracked:
                if target_process is None and getattr(process, "pid", None) == target_pid:
                    target_process = process
                else:
                    remaining.append(process)

            if target_process is None:
                raise KeyError(
                    f"No tracked worker process with PID {target_pid} for addon "
                    f"'{addon_id}'."
                )
            if remaining:
                self._get_worker_map()[addon_id] = remaining
            else:
                self._get_worker_map().pop(addon_id, None)
            self._active_workers = [
                process
                for process in getattr(self, "_active_workers", [])
                if id(process) != id(target_process)
            ]

        was_running = _is_process_running(target_process)
        if was_running:
            logger.info(
                "Terminating worker for addon '%s' PID %s...", addon_id, target_pid
            )
            target_process.terminate()
            try:
                target_process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                target_process.kill()

        with self._get_worker_lock():
            self._prune_worker_tracking(addon_id)
        return {
            "addon_id": addon_id,
            "pid": target_pid,
            "terminated": was_running,
            "worker_state": self.get_worker_state(addon_id),
        }

    def get_worker_states(
        self,
        addon_ids: Iterable[str],
    ) -> dict[str, dict[str, Any]]:
        """Return one consistent worker snapshot for multiple add-ons."""

        normalized_ids = list(
            dict.fromkeys(str(addon_id) for addon_id in addon_ids if addon_id)
        )
        requested_ids = set(normalized_ids)
        worker_records_by_addon = {addon_id: [] for addon_id in normalized_ids}
        registries = getattr(self, "registries", None)
        if registries and getattr(registries, "workers", None):
            for worker in registries.workers.list():
                if worker.addon_id in requested_ids:
                    worker_records_by_addon[worker.addon_id].append(asdict(worker))

        with self._get_worker_lock():
            self._prune_worker_tracking()
            worker_map = self._get_worker_map()
            live_by_addon = {
                addon_id: list(worker_map.get(addon_id, []))
                for addon_id in normalized_ids
            }
            target_counts = dict(self._get_worker_target_counts())
            entrypoints = dict(self._get_worker_entrypoints())
            execution_environments = dict(self._get_worker_execution_environments())

        states: dict[str, dict[str, Any]] = {}
        for addon_id in normalized_ids:
            live_processes = live_by_addon[addon_id]
            worker_records = worker_records_by_addon[addon_id]
            status = (
                "running"
                if live_processes
                else ("configured" if worker_records else "not_declared")
            )
            states[addon_id] = {
                "status": status,
                "configured": len(worker_records),
                "running": len(live_processes),
                "pids": [process.pid for process in live_processes],
                "workers": worker_records,
                "log_source_id": f"worker.{addon_id}",
                "desired": target_counts.get(addon_id),
                "entrypoint": entrypoints.get(addon_id),
                "execution_environment": execution_environments.get(addon_id),
                # Environment fallback is deliberately unavailable. A Pixi worker
                # must never escape its declared dependency environment.
                "allow_environment_fallback": False,
            }
        return states

    def get_worker_state(self, addon_id: str) -> dict[str, Any]:
        """Return supervised worker state for administration and diagnostics."""

        return self.get_worker_states([addon_id])[addon_id]

    def restart_addon_workers(
        self,
        addon_id: str,
        *,
        num_workers: int | None = None,
        entrypoint: str | None = None,
    ) -> dict:
        """Restart one add-on's workers using its last declared preferences."""

        if num_workers is not None:
            try:
                desired_workers = int(num_workers)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "num_workers must be an integer greater than zero."
                ) from exc
            if desired_workers < 1:
                raise ValueError("num_workers must be greater than zero.")
        else:
            desired_workers = None

        before_state = self.get_worker_state(addon_id)
        worker_records = before_state.get("workers") or []
        known_entrypoints = {
            record.get("entrypoint")
            for record in worker_records
            if record.get("entrypoint")
        }
        if entrypoint:
            script_name = str(entrypoint)
        else:
            script_name = self._get_worker_entrypoints().get(addon_id)
            if not script_name:
                if len(known_entrypoints) > 1:
                    raise ValueError(
                        f"Addon '{addon_id}' declares multiple worker entrypoints. "
                        "Specify an entrypoint explicitly."
                    )
                if len(known_entrypoints) == 1:
                    script_name = next(iter(known_entrypoints))
        if not script_name and worker_records:
            script_name = "worker.py"
        if not script_name:
            fallback_path = self.get_addon_directory(addon_id) / "worker.py"
            if fallback_path.exists():
                script_name = "worker.py"
        if not script_name:
            raise ValueError(
                f"Could not determine a worker entrypoint for addon '{addon_id}'. "
                "No declared worker entrypoint was found."
            )

        if desired_workers is None:
            stored_target = self._get_worker_target_counts().get(addon_id)
            if stored_target:
                desired_workers = int(stored_target)
            elif before_state.get("running"):
                desired_workers = int(before_state["running"])
            elif worker_records:
                desired_workers = max(1, len(worker_records))
            else:
                desired_workers = 1

        execution_environment = self._resolve_worker_execution_environment(
            addon_id, script_name
        )
        with self._get_worker_lock():
            self._get_worker_target_counts()[addon_id] = desired_workers
            self._get_worker_entrypoints()[addon_id] = script_name
            self._get_worker_execution_environments()[addon_id] = execution_environment

        stopped = self.stop_addon_workers(addon_id)
        spawned = self.spawn_managed_workers(
            addon_id,
            script_name,
            num_workers=desired_workers,
            execution_environment=execution_environment,
        )
        after_state = self.get_worker_state(addon_id)
        return {
            "addon_id": addon_id,
            "entrypoint": script_name,
            "execution_environment": execution_environment,
            "allow_environment_fallback": False,
            "requested_workers": desired_workers,
            "stopped_workers": stopped,
            "spawned_workers": len(spawned),
            "before": before_state,
            "after": after_state,
        }

    def _normalize_worker_execution_environment(
        self,
        value: str | None,
        *,
        addon_id: str,
        source: str,
        default: str = WORKER_ENV_AUTO,
    ) -> str:
        normalized = str(value or "").strip().lower() or default
        if normalized not in VALID_WORKER_EXECUTION_ENVIRONMENTS:
            raise ValueError(
                f"Addon '{addon_id}' declares unsupported worker execution "
                f"environment {value!r} via {source}. Expected one of: "
                + ", ".join(sorted(VALID_WORKER_EXECUTION_ENVIRONMENTS))
                + "."
            )
        return normalized

    def _get_declared_worker_record(self, addon_id: str, entrypoint: str | None = None):
        registries = getattr(self, "registries", None)
        workers_registry = getattr(registries, "workers", None) if registries else None
        if not workers_registry:
            return None
        worker_records = [
            record for record in workers_registry.list() if record.addon_id == addon_id
        ]
        if not worker_records:
            return None
        if entrypoint:
            for record in worker_records:
                if str(record.entrypoint or "worker.py") == str(entrypoint):
                    return record
        return worker_records[0] if len(worker_records) == 1 else None

    def _runtime_unit_for_entrypoint(
        self,
        addon_id: str,
        entrypoint: str | None,
    ) -> str | None:
        package = (getattr(self, "_addon_packages", None) or {}).get(addon_id)
        contract = getattr(package, "contract", None)
        candidates = [
            item
            for item in tuple(getattr(contract, "runtime_units", ()) or ())
            if str((getattr(item, "config", {}) or {}).get("script") or "")
            == str(entrypoint or "")
        ]
        if len(candidates) == 1:
            return str(candidates[0].id)
        declared_worker = self._get_declared_worker_record(addon_id, entrypoint)
        declared_id = str(getattr(declared_worker, "id", "") or "")
        if declared_id and any(
            str(getattr(item, "id", "")) == declared_id
            for item in tuple(getattr(contract, "runtime_units", ()) or ())
        ):
            return declared_id
        managed_environments = tuple(
            item
            for item in self._contract_python_environments(addon_id)
            if getattr(item, "environment", None)
            == DependencyEnvironment.MANAGED_PROCESS_PIXI
        )
        runtime_ids = tuple(
            dict.fromkeys(
                runtime_id
                for item in managed_environments
                for runtime_id in tuple(getattr(item, "runtime_units", ()) or ())
            )
        )
        return runtime_ids[0] if len(runtime_ids) == 1 else None

    def _resolve_worker_execution_environment(
        self,
        addon_id: str,
        entrypoint: str | None = None,
    ) -> str:
        addon_dir = self.get_addon_directory(addon_id)
        has_addon_pixi = (
            addon_dir / "pixi.toml"
        ).exists() or self._addon_declares_pixi_environment(addon_id)

        execution_environment = WORKER_ENV_AUTO
        addon = (getattr(self, "loaded_addons", None) or {}).get(addon_id)
        if addon is not None:
            addon_environment = getattr(addon, "worker_execution_environment", None)
            if addon_environment is not None:
                execution_environment = self._normalize_worker_execution_environment(
                    addon_environment,
                    addon_id=addon_id,
                    source=f"{type(addon).__name__}.worker_execution_environment",
                )

        declared_worker = self._get_declared_worker_record(addon_id, entrypoint)
        if declared_worker is not None:
            if bool(getattr(declared_worker, "allow_environment_fallback", False)):
                raise ValueError(
                    f"Addon '{addon_id}' requests worker environment fallback. "
                    "Fallback is retired; declare either 'app' or 'addon_pixi'."
                )
            execution_environment = self._normalize_worker_execution_environment(
                getattr(declared_worker, "execution_environment", WORKER_ENV_AUTO),
                addon_id=addon_id,
                source=f"manifest workers[{declared_worker.id}]",
            )

        if execution_environment == WORKER_ENV_AUTO:
            return WORKER_ENV_ADDON_PIXI if has_addon_pixi else WORKER_ENV_APP
        if execution_environment == WORKER_ENV_ADDON_PIXI and not has_addon_pixi:
            raise RuntimeError(
                f"Addon '{addon_id}' declares an addon_pixi worker but has no "
                "managed Pixi dependency environment."
            )
        return execution_environment

    def _resolve_worker_app_runtime_bridge(
        self,
        addon_id: str,
        entrypoint: str | None = None,
    ) -> str:
        declared_worker = self._get_declared_worker_record(addon_id, entrypoint)
        value = str(
            getattr(
                declared_worker,
                "app_runtime_bridge",
                WORKER_APP_RUNTIME_BRIDGE_NONE,
            )
            or WORKER_APP_RUNTIME_BRIDGE_NONE
        ).strip().lower()
        if value not in VALID_WORKER_APP_RUNTIME_BRIDGES:
            raise ValueError(
                f"Addon '{addon_id}' declares unsupported app runtime bridge "
                f"{value!r}. Expected one of: "
                + ", ".join(sorted(VALID_WORKER_APP_RUNTIME_BRIDGES))
                + "."
            )
        return value

    def spawn_managed_workers(
        self,
        addon_id: str,
        script_name: str,
        num_workers: int = 1,
        *,
        execution_environment: str | None = None,
        allow_environment_fallback: bool | None = None,
    ) -> list:
        """Start and track a declared worker pool in its exact environment."""

        if allow_environment_fallback:
            raise ValueError(
                "Worker environment fallback is retired. Declare either 'app' "
                "or 'addon_pixi' and make that environment complete."
            )
        if is_worker_process():
            logger.info(
                "Skipping worker spawn for addon '%s' inside a worker process.",
                addon_id,
            )
            return []
        if num_workers < 1:
            return []

        addon_dir = self.get_addon_directory(addon_id)
        script_path = addon_dir / script_name
        if not script_path.is_file():
            raise FileNotFoundError(
                f"Worker script {script_path} not found in addon {addon_id}."
            )

        if execution_environment is None:
            resolved_environment = self._resolve_worker_execution_environment(
                addon_id, script_name
            )
        else:
            resolved_environment = self._normalize_worker_execution_environment(
                execution_environment,
                addon_id=addon_id,
                source="spawn_managed_workers",
                default=WORKER_ENV_APP,
            )
            if resolved_environment == WORKER_ENV_AUTO:
                resolved_environment = self._resolve_worker_execution_environment(
                    addon_id, script_name
                )
        app_runtime_bridge = self._resolve_worker_app_runtime_bridge(
            addon_id,
            script_name,
        )
        if (
            app_runtime_bridge != WORKER_APP_RUNTIME_BRIDGE_NONE
            and resolved_environment != WORKER_ENV_ADDON_PIXI
        ):
            raise ValueError(
                f"Addon '{addon_id}' declares app runtime bridge "
                f"{app_runtime_bridge!r} outside an addon_pixi worker."
            )
        host_runtime_bridge_script: Path | None = None
        if app_runtime_bridge == WORKER_APP_RUNTIME_BRIDGE_HOST_V1:
            host_runtime_bridge_script = (
                Path(__file__).resolve().parent.parent / "managed_worker_bootstrap.py"
            )
            if not host_runtime_bridge_script.is_file():
                raise RuntimeError(
                    "The managed worker host-runtime bridge is unavailable."
                )

        pixi_manifest = None
        if resolved_environment == WORKER_ENV_ADDON_PIXI:
            pixi_manifest = self._prepare_addon_pixi_project(
                addon_id,
                runtime_unit=self._runtime_unit_for_entrypoint(
                    addon_id,
                    script_name,
                ),
            )

        with self._get_worker_lock():
            self._get_worker_target_counts()[addon_id] = int(num_workers)
            self._get_worker_entrypoints()[addon_id] = str(script_name)
            self._get_worker_execution_environments()[addon_id] = resolved_environment

        live_workers = self._live_worker_processes(addon_id)
        if len(live_workers) >= num_workers:
            logger.info(
                "Worker spawn skipped for addon '%s'; %s process(es) are running.",
                addon_id,
                len(live_workers),
            )
            return live_workers[:num_workers]
        workers_to_spawn = num_workers - len(live_workers)

        log_registry = getattr(self.app, "log_registry", None)
        if log_registry is not None:
            log_registry.register_worker_source(
                addon_id, name=f"{addon_id} Worker", addon_id=addon_id
            )
            log_source_id = f"worker.{addon_id}"
            log_file = log_registry.get_source(log_source_id).path
            output_handle = log_registry.open_for_append(log_source_id)
        else:
            logs_dir = addon_dir / "logs"
            logs_dir.mkdir(parents=True, exist_ok=True)
            log_file = logs_dir / "worker.log"
            output_handle = open(  # noqa: SIM115 - retained for worker lifetime.
                log_file, "a", encoding="utf-8"
            )

        env = os.environ.copy()
        env["PYTHONNOUSERSITE"] = "1"
        runtime_profile = getattr(getattr(self, "app", None), "config", {}).get(
            "CARLINA_PROFILE"
        )
        if isinstance(runtime_profile, str):
            env["CARLINA_PROFILE"] = runtime_profile
        ownership_root = _worker_ownership_root(self.app)
        ownership_xoption = worker_ownership_xoption(ownership_root)
        worker_temp_dir = _worker_temporary_directory(ownership_root, addon_id)
        for variable in ("TMPDIR", "TEMP", "TMP"):
            env[variable] = str(worker_temp_dir)
        existing_pythonpath = env.get("PYTHONPATH", "")
        project_root = str(Path(self.app.root_path).parent.resolve())
        extra_paths = [project_root]
        if resolved_environment == WORKER_ENV_APP:
            # The application interpreter already owns its site-packages. Only
            # add the owning package's non-shadowing vendor target explicitly.
            in_process_environment = self._python_environment_for_runtime(
                addon_id,
                environment=DependencyEnvironment.IN_PROCESS_VENDOR,
            )
            environment_paths = (
                self._addon_environment_paths(addon_id, in_process_environment)
                if in_process_environment is not None
                else self._addon_environment_paths(addon_id)
            )
            extra_paths.append(str(environment_paths.lib_dir))
            for site_path in (
                sysconfig.get_path("purelib"),
                sysconfig.get_path("platlib"),
            ):
                if site_path and site_path not in extra_paths:
                    extra_paths.append(site_path)
        combined = os.pathsep.join(extra_paths)
        env["PYTHONPATH"] = (
            f"{combined}{os.pathsep}{existing_pythonpath}"
            if existing_pythonpath
            else combined
        )
        mark_worker_environment(env, addon_id=addon_id)
        env.setdefault("UV_LINK_MODE", "copy")

        if resolved_environment == WORKER_ENV_ADDON_PIXI:
            if pixi_manifest is None:
                raise RuntimeError(
                    f"Addon '{addon_id}' Pixi project could not be materialized."
                )
            if app_runtime_bridge == WORKER_APP_RUNTIME_BRIDGE_HOST_V1:
                if host_runtime_bridge_script is None:
                    raise RuntimeError(
                        "The managed worker host-runtime bridge is unavailable."
                    )
                env.pop("PYTHONPATH", None)
                env.pop("PYTHONHOME", None)
                host_site_paths = list(
                    dict.fromkeys(
                        path
                        for path in (
                            sysconfig.get_path("purelib"),
                            sysconfig.get_path("platlib"),
                        )
                        if path
                    )
                )
                if not host_site_paths:
                    raise RuntimeError(
                        "The app runtime bridge could not resolve host site-packages."
                    )
                worker_argv = [
                    "python",
                    "-I",
                    "-X",
                    ownership_xoption,
                    "-u",
                    str(host_runtime_bridge_script),
                ]
                for host_site_path in host_site_paths:
                    worker_argv.extend(["--host-site", host_site_path])
                worker_argv.extend(["--", str(script_path)])
                launch_label = "Addon Pixi + host runtime bridge"
            else:
                worker_argv = [
                    "python",
                    "-X",
                    ownership_xoption,
                    "-u",
                    str(script_path),
                ]
                launch_label = "Addon Pixi"
            launch_command = self._get_pixi_runtime_backend().command(
                pixi_manifest,
                worker_argv,
            )
        else:
            launch_command = [
                sys.executable,
                "-X",
                ownership_xoption,
                "-u",
                str(script_path),
            ]
            launch_label = "App Python"

        processes = []
        try:
            with managed_worker_quiescence_gate.spawn_section():
                for index in range(workers_to_spawn):
                    process = subprocess.Popen(
                        launch_command,
                        stdout=output_handle,
                        stderr=subprocess.STDOUT,
                        cwd=str(addon_dir),
                        env=env,
                        creationflags=managed_process_creation_flags(),
                    )
                    # Popen success is not worker readiness. Surface immediate
                    # dependency/entrypoint failures for every environment
                    # instead of leaving durable jobs queued without a live
                    # consumer.
                    time.sleep(1.25)
                    exit_code = process.poll()
                    if exit_code is not None:
                        raise RuntimeError(
                            f"{launch_label} worker for addon '{addon_id}' exited "
                            f"immediately with code {exit_code}. Inspect {log_file}."
                        )
                    processes.append(process)
                    self.track_worker_process(addon_id, process)
                    logger.info(
                        "Spawned %s worker %s/%s for '%s' (PID: %s). Logs at %s",
                        launch_label,
                        len(live_workers) + index + 1,
                        num_workers,
                        addon_id,
                        process.pid,
                        log_file,
                    )
        except Exception:
            process_ids = {id(process) for process in processes}
            with self._get_worker_lock():
                tracked = self._get_worker_map().get(addon_id, [])
                remaining = [
                    process for process in tracked if id(process) not in process_ids
                ]
                if remaining:
                    self._get_worker_map()[addon_id] = remaining
                else:
                    self._get_worker_map().pop(addon_id, None)
                self._active_workers = [
                    process
                    for process in getattr(self, "_active_workers", [])
                    if id(process) not in process_ids
                ]
            for process in processes:
                if _is_process_running(process):
                    process.terminate()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill()
            output_handle.close()
            raise

        if processes:
            self._get_worker_log_handles().setdefault(addon_id, []).append(
                output_handle
            )
        else:
            output_handle.close()
        return live_workers + processes

    def spawn_isolated_worker(
        self,
        addon_id: str,
        script_name: str,
        num_workers: int = 1,
        *,
        execution_environment: str | None = None,
        allow_environment_fallback: bool | None = None,
    ) -> list:
        """Compatibility name for core callers; add-ons use ensure_worker_pool."""

        return self.spawn_managed_workers(
            addon_id,
            script_name,
            num_workers=num_workers,
            execution_environment=execution_environment,
            allow_environment_fallback=allow_environment_fallback,
        )


__all__ = [
    "DEPENDENCY_ENV_IN_PROCESS_VENDOR",
    "DEPENDENCY_ENV_MANAGED_PIXI",
    "LEGACY_DEPENDENCY_ENVIRONMENTS",
    "VALID_WORKER_EXECUTION_ENVIRONMENTS",
    "WORKER_ENV_ADDON_PIXI",
    "WORKER_ENV_APP",
    "WORKER_ENV_AUTO",
    "ManagedProcessOrchestrationMixin",
]
