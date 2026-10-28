"""Native Documents manager composition without a container backend."""

from __future__ import annotations

import logging
from typing import Any

from flask import Flask

from ..addon_manager_core import AddonManagerCore
from .container_lifecycle_support import ContainerLifecycleSupportMixin

_LOGGER = logging.getLogger(__name__)
_CONTAINER_SERVICES_DISABLED = "This application does not include container services."


class NativeAddonManager(AddonManagerCore, ContainerLifecycleSupportMixin):
    """Compose common add-on management with a fail-closed native backend."""

    def __init__(
        self,
        app: Flask,
        *,
        manage_lifecycle: bool = True,
        allow_lifecycle_maintenance: bool = False,
        allowed_addon_ids: frozenset[str] | None = None,
        container_services: bool = False,
    ) -> None:
        if type(container_services) is not bool:
            raise TypeError("Container service selection must be a boolean.")
        if container_services:
            raise ValueError(
                "The native add-on manager cannot enable container services."
            )
        super().__init__(
            app,
            manage_lifecycle=manage_lifecycle,
            allow_lifecycle_maintenance=allow_lifecycle_maintenance,
            allowed_addon_ids=allowed_addon_ids,
            container_services=False,
        )

    def _initialize_container_lifecycle(self) -> None:
        self._active_containers: dict[str, object] = {}
        self._container_last_accessed_ts: dict[str, float] = {}
        self.docker_client = None
        self.docker_available = False
        self.docker_status_message = _CONTAINER_SERVICES_DISABLED
        self._docker_backend = "none"
        self._container_runtime_kind = "none"
        self._docker_wsl_distro = None
        self.docker_status_checked = False

    def _cleanup_container_lifecycle(self) -> None:
        addon_ids = set(self._active_containers) | set(self._container_last_accessed_ts)
        for addon_id in sorted(addon_ids):
            try:
                self._stop_and_remove_container(addon_id)
            except Exception as error:
                _LOGGER.warning(
                    "Failed to clean up container bookkeeping for addon '%s' (%s).",
                    addon_id,
                    type(error).__name__,
                )

    def _stop_and_remove_container(
        self,
        addon_id: str,
        remove_image: bool = False,
        manifest: dict[str, Any] | None = None,
    ) -> None:
        del remove_image, manifest
        self._active_containers.pop(addon_id, None)
        self._container_last_accessed_ts.pop(addon_id, None)
        access_marker = self._container_access_marker_path(addon_id)
        try:
            access_marker.unlink()
        except FileNotFoundError:
            pass
        except OSError as error:
            _LOGGER.warning(
                "Failed to remove container access marker for addon '%s' (%s).",
                addon_id,
                type(error).__name__,
            )

    def container_runtime_available(
        self,
        *,
        log_failure: bool = False,
        force: bool = False,
    ) -> bool:
        del log_failure, force
        return False

    def _refresh_docker_status(
        self,
        log_failure: bool = False,
        *,
        force: bool = False,
    ) -> bool:
        del log_failure, force
        return False

    def get_container_runtime_status(self, *, force: bool = False) -> dict[str, object]:
        del force
        return {"available": False, "message": _CONTAINER_SERVICES_DISABLED}

    def container_image_exists(self, image_name: str) -> bool:
        del image_name
        return False

    def _docker_image_exists(self, image_name: str) -> bool:
        del image_name
        return False

    def reset_container(self, addon_id: str) -> None:
        self._stop_and_remove_container(str(addon_id))

    def get_container_url(self, addon_id: str) -> str:
        del addon_id
        raise RuntimeError(_CONTAINER_SERVICES_DISABLED)

    def container_use(self, addon_id: str, *, timeout: float = 600) -> None:
        del addon_id, timeout
        raise RuntimeError(_CONTAINER_SERVICES_DISABLED)

    def touch_container_access(self, addon_id: str) -> None:
        del addon_id
        raise RuntimeError(_CONTAINER_SERVICES_DISABLED)

    def prepare_container_images(
        self,
        addon_id: str,
        *,
        manifest: dict[str, Any] | None = None,
        force_refresh: bool = False,
    ) -> None:
        del addon_id, manifest, force_refresh
        raise RuntimeError(_CONTAINER_SERVICES_DISABLED)

    def _ensure_docker_image_prepared(
        self,
        addon_id: str,
        manifest: dict[str, Any] | None = None,
        force_refresh: bool = False,
    ) -> None:
        del addon_id, manifest, force_refresh
        raise RuntimeError(_CONTAINER_SERVICES_DISABLED)

    def remove_container_image(self, image_name: str, *, force: bool = False) -> None:
        del image_name, force
        raise RuntimeError(_CONTAINER_SERVICES_DISABLED)

    def manage_container_service(
        self,
        addon_id: str,
        service_name: str = "service",
        *,
        action: str,
    ) -> None:
        del addon_id, service_name, action
        raise RuntimeError(_CONTAINER_SERVICES_DISABLED)

    def reap_idle_containers(self) -> None:
        return None

    def start_always_on_containers(self) -> None:
        return None
