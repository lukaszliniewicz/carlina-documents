"""Workbench compatibility facade for the shared add-on manager core."""

from __future__ import annotations

import atexit
import logging
import threading
import time
from pathlib import Path
from typing import Any

from .addon_manager_core import AddonManagerCore, _compact_scope_alias
from .addons.container_orchestration import ContainerOrchestrationMixin
from .addons.container_runtime import CONTAINER_RUNTIME_NONE
from .frontend_orchestration import get_cached_frontend_workspace_status

__all__ = [
    "AddonManager",
    "_compact_scope_alias",
    "atexit",
    "get_cached_frontend_workspace_status",
    "time",
]


class AddonManager(AddonManagerCore, ContainerOrchestrationMixin):
    """Preserve the Workbench manager name and container-backed behavior."""

    def _initialize_container_lifecycle(self) -> None:
        self._active_containers: dict[str, object] = {}
        self._container_lock = threading.RLock()
        self._addon_container_locks: dict[str, threading.RLock] = {}
        self._container_last_accessed_ts: dict[str, float] = {}
        self.docker_client = None
        self.docker_available = False
        self.docker_status_message = "Docker status has not been checked yet."
        self._docker_backend = "none"
        self._container_runtime_kind = CONTAINER_RUNTIME_NONE
        self._docker_wsl_distro = None
        self.docker_status_checked = False
        self._docker_status_checked_at = 0.0
        self._docker_status_lock = threading.RLock()
        self._docker_status_ttl_seconds = self._resolve_docker_status_ttl_seconds()
        self.docker_status_message = (
            "Docker status has not been checked; it will be probed on first "
            "Docker-backed request."
        )
        logging.info(self.docker_status_message)

    def _cleanup_container_lifecycle(self) -> None:
        for addon_id in list(self._active_containers.keys()):
            try:
                self._stop_and_remove_container(addon_id)
            except Exception as error:
                logging.warning(
                    "Failed to clean up Docker container for addon '%s': %s",
                    addon_id,
                    error,
                )

    def _stop_and_remove_container(
        self,
        addon_id: str,
        remove_image: bool = False,
        manifest: dict[str, Any] | None = None,
    ) -> None:
        """Keep container dispatch explicit across the shared-core MRO."""

        ContainerOrchestrationMixin._stop_and_remove_container(
            self,
            addon_id,
            remove_image=remove_image,
            manifest=manifest,
        )

    def _frontend_workspace_status(self, project_root: Path) -> dict[str, Any] | None:
        return get_cached_frontend_workspace_status(project_root)
