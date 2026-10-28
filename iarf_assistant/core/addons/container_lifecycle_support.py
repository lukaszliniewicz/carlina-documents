"""Container naming and manifest helpers shared by manager compositions."""

from __future__ import annotations

import hashlib
import logging
import tempfile
from pathlib import Path
from typing import Any, cast

from flask import Flask

from ...product_identity import PRODUCT_IDENTITY
from ..app_paths import installation_identity

_DOCKER_NAME_MAX_LENGTH = 63
_LOGGER = logging.getLogger(__name__)


class ContainerLifecycleSupportMixin:
    """Provide installation-scoped naming and manifest lifecycle policy."""

    app: Flask

    def _container_installation_id(self) -> str:
        cached_identity = str(
            getattr(self, "_container_installation_identity", "") or ""
        ).strip()
        if cached_identity:
            return cached_identity

        app = cast(Flask | None, getattr(self, "app", None))
        extensions = cast(dict[str, Any], getattr(app, "extensions", None) or {})
        app_paths = extensions.get("carlina_paths")
        if app_paths is not None:
            configured_identity = str(
                getattr(app_paths, "installation_id", "") or ""
            ).strip()
            if configured_identity:
                self._container_installation_identity = configured_identity
                return configured_identity
            data_root = getattr(app_paths, "data_root", None)
            if data_root:
                resolved_identity = installation_identity(data_root)
                self._container_installation_identity = resolved_identity
                return resolved_identity

        instance_path = getattr(app, "instance_path", None)
        if instance_path:
            resolved_identity = installation_identity(instance_path)
            self._container_installation_identity = resolved_identity
            return resolved_identity
        raise RuntimeError(
            "Carlina's installation identity is unavailable; the container runtime "
            "requires a resolved data root."
        )

    @staticmethod
    def _bounded_docker_name(value: str, *, max_length: int) -> str:
        if len(value) <= max_length:
            return value
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:10]
        prefix = value[: max_length - len(digest) - 1].rstrip(".-_")
        return f"{prefix}-{digest}"

    def _docker_resource_name(self, *components: object) -> str:
        safe_components = [
            self._docker_name_component(component, default="resource")
            for component in components
        ]
        return self._bounded_docker_name(
            "-".join(safe_components),
            max_length=_DOCKER_NAME_MAX_LENGTH,
        )

    def _get_container_name(self, addon_id: str, service_name: str = "service") -> str:
        return self._docker_resource_name(
            PRODUCT_IDENTITY.distribution_name,
            "addon",
            self._container_installation_id(),
            addon_id,
            service_name,
        )

    def _get_network_name(self, addon_id: str) -> str:
        return self._docker_resource_name(
            PRODUCT_IDENTITY.distribution_name,
            "addon",
            self._container_installation_id(),
            addon_id,
            "net",
        )

    def _container_access_marker_path(self, addon_id: str) -> Path:
        marker_name = self._docker_resource_name(
            ".carlina",
            self._container_installation_id(),
            addon_id,
            "last-accessed",
        )
        return Path(tempfile.gettempdir()) / marker_name

    def _docker_refresh_marker_path(self, addon_id: str) -> Path:
        return Path(self.app.instance_path) / f".{addon_id}_docker_image_stale"

    def mark_docker_image_stale(
        self, addon_id: str, version: str | None = None
    ) -> None:
        """Mark a Docker-backed add-on image for a lazy rebuild."""
        marker_path = self._docker_refresh_marker_path(addon_id)
        try:
            marker_path.parent.mkdir(parents=True, exist_ok=True)
            marker_path.write_text(str(version or ""), encoding="utf-8")
            _LOGGER.info(
                "Marked Docker image for addon '%s' stale; it will rebuild on first use.",
                addon_id,
            )
        except OSError as error:
            _LOGGER.warning(
                "Failed to mark Docker image for addon '%s' stale: %s",
                addon_id,
                error,
            )

    def _is_docker_image_stale(self, addon_id: str) -> bool:
        return self._docker_refresh_marker_path(addon_id).exists()

    def _clear_docker_image_stale(self, addon_id: str) -> None:
        try:
            self._docker_refresh_marker_path(addon_id).unlink()
        except FileNotFoundError:
            pass
        except OSError as error:
            _LOGGER.warning(
                "Failed to clear Docker stale marker for addon '%s': %s",
                addon_id,
                error,
            )

    @staticmethod
    def _get_docker_services(
        docker_config: dict[str, Any] | None,
    ) -> dict[str, dict[str, Any]]:
        if not isinstance(docker_config, dict) or not docker_config:
            return {}

        services = docker_config.get("services")
        if isinstance(services, dict) and services:
            normalized: dict[str, dict[str, Any]] = {}
            for name, config in cast(dict[object, object], services).items():
                if isinstance(config, dict):
                    normalized[str(name)] = cast(dict[str, Any], config)
            return normalized

        return {"service": docker_config}

    @staticmethod
    def _docker_name_component(value: object, *, default: str) -> str:
        """Return a conservative Docker repository/tag component."""

        normalized = "".join(
            character.lower()
            if character.isalnum() or character in {".", "_", "-"}
            else "-"
            for character in str(value or "").strip()
        ).strip(".-_")
        return normalized or default

    @staticmethod
    def _normalize_docker_lifecycle(value: object, default: str = "on_demand") -> str:
        normalized = str(value or default).strip().lower()
        if normalized not in {"on_demand", "always_on"}:
            return default
        return normalized

    def _get_effective_stack_lifecycle(
        self,
        docker_config: dict[str, Any],
        services: dict[str, dict[str, Any]] | None = None,
    ) -> str:
        services = services or self._get_docker_services(docker_config)
        stack_default = self._normalize_docker_lifecycle(
            docker_config.get("lifecycle"), default="on_demand"
        )
        for service in services.values():
            lifecycle = self._normalize_docker_lifecycle(
                service.get("lifecycle"), default=stack_default
            )
            if lifecycle == "always_on":
                return "always_on"
        return stack_default
