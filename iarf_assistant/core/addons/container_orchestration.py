"""Container lifecycle orchestration shared by the add-on manager."""

from __future__ import annotations

import logging
import os
import platform
import re
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from filelock import FileLock
from filelock import Timeout as FileLockTimeout

try:
    import fcntl
except ImportError:  # Windows has no fcntl; fall back to exclusive FileLock.
    fcntl = None  # type: ignore[assignment]

import requests

from carlina.runtime_environment import is_worker_process

try:
    import docker
except ImportError:  # pragma: no cover - Docker SDK is optional.
    docker = None

from ...product_identity import PRODUCT_IDENTITY, product_environment_value
from ..logging_observability import redact_text
from ..upload_paths import uploads_root
from .container_lifecycle_support import ContainerLifecycleSupportMixin
from .container_runtime import (
    CONTAINER_RUNTIME_DOCKER_SDK,
    CONTAINER_RUNTIME_NONE,
    CONTAINER_RUNTIME_WSL_CLI,
    ContainerStorageBroker,
    create_wsl_docker_client,
    select_container_runtime,
)

_DEFAULT_DOCKER_STATUS_TTL_SECONDS = 60.0
_VALID_GPU_REQUIREMENTS = frozenset({"none", "optional", "required"})
_DOCKER_LABEL_PREFIX = "com.carlina"
_DOCKER_MANAGED_LABEL = f"{_DOCKER_LABEL_PREFIX}.managed"
_DOCKER_INSTALLATION_LABEL = f"{_DOCKER_LABEL_PREFIX}.installation"
_DOCKER_RESOURCE_LABEL = f"{_DOCKER_LABEL_PREFIX}.resource"
_DOCKER_ADDON_LABEL = f"{_DOCKER_LABEL_PREFIX}.addon"
_DOCKER_SERVICE_LABEL = f"{_DOCKER_LABEL_PREFIX}.service"
_DOCKER_VERSION_LABEL = f"{_DOCKER_LABEL_PREFIX}.version"
_DOCKER_OPERATION_LABEL = f"{_DOCKER_LABEL_PREFIX}.operation"
_DOCKER_TAG_PATTERN = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")
_LOGGER = logging.getLogger(__name__)


class ContainerOrchestrationMixin(ContainerLifecycleSupportMixin):
    """Own Docker service lifecycle behavior for :class:`AddonManager`."""

    def container_runtime_available(
        self,
        *,
        log_failure: bool = False,
        force: bool = False,
    ) -> bool:
        """Return current container-runtime readiness through the public API."""

        return self._refresh_docker_status(log_failure=log_failure, force=force)

    def get_container_runtime_status(self, *, force: bool = False) -> dict[str, object]:
        """Describe runtime readiness without exposing the selected backend client."""

        available = self.container_runtime_available(
            log_failure=False,
            force=force,
        )
        return {
            "available": available,
            "message": str(
                getattr(self, "docker_status_message", "")
                or (
                    "Container runtime is available."
                    if available
                    else "Container runtime is not available."
                )
            ),
        }

    def container_image_exists(self, image_name: str) -> bool:
        """Return whether an image is available in the selected container backend."""

        return self._docker_image_exists(str(image_name))

    def resolve_container_service_image_name(
        self,
        addon_id: str,
        service_name: str,
        service_config: dict,
        *,
        manifest: dict | None = None,
    ) -> str:
        """Return the image reference the orchestrator will use for a service."""

        effective_manifest = (
            manifest
            if manifest is not None
            else self._get_addon_manifest(str(addon_id))
        )
        return self._get_service_image_name(
            str(addon_id),
            str(service_name),
            service_config,
            effective_manifest,
        )

    def prepare_container_images(
        self,
        addon_id: str,
        *,
        manifest: dict | None = None,
        force_refresh: bool = False,
    ) -> None:
        """Prepare declared images without exposing the backend client to add-ons."""

        self._ensure_docker_image_prepared(
            str(addon_id),
            manifest=manifest,
            force_refresh=force_refresh,
        )

    def remove_container_image(self, image_name: str, *, force: bool = False) -> None:
        """Remove one explicitly named image through the selected backend."""

        if not self._refresh_docker_status(log_failure=False):
            raise RuntimeError(self.docker_status_message)
        self.docker_client.images.remove(str(image_name), force=bool(force))

    def get_container_service_status(
        self,
        addon_id: str,
        service_name: str = "service",
    ) -> str:
        """Return a service status or ``not_found`` without leaking SDK objects."""

        if not self._refresh_docker_status(log_failure=False):
            raise RuntimeError(self.docker_status_message)
        try:
            container = self.docker_client.containers.get(
                self._get_container_name(str(addon_id), str(service_name))
            )
            container.reload()
            if not self._docker_resource_is_owned(
                container,
                str(addon_id),
                "addon-container",
                service_name=str(service_name),
            ):
                return "ownership_conflict"
            return str(container.status)
        except docker.errors.NotFound:
            return "not_found"
        except docker.errors.APIError:
            return "error"

    def inspect_container_service(
        self,
        addon_id: str,
        service_name: str = "service",
        *,
        internal_port: int | None = None,
    ) -> dict[str, object]:
        """Return serializable service diagnostics without leaking SDK objects."""

        addon_id = str(addon_id)
        service_name = str(service_name)
        container_name = self._get_container_name(addon_id, service_name)
        result: dict[str, object] = {
            "name": service_name,
            "container": container_name,
        }
        if not self._refresh_docker_status(log_failure=False):
            return {
                **result,
                "state": "unavailable",
                "error": str(self.docker_status_message),
            }

        try:
            container = self.docker_client.containers.get(container_name)
        except docker.errors.NotFound as exc:
            return {**result, "state": "not_found", "error": str(exc)}
        except docker.errors.APIError as exc:
            return {**result, "state": "error", "error": str(exc)}

        try:
            container.reload()
            container_status = str(getattr(container, "status", "unknown") or "unknown")
        except docker.errors.APIError as exc:
            return {**result, "state": "error", "error": str(exc)}

        if not self._docker_resource_is_owned(
            container,
            addon_id,
            "addon-container",
            service_name=service_name,
        ):
            return {
                **result,
                "state": "ownership_conflict",
                "error": (
                    "A Docker container with this installation-scoped name is not "
                    "owned by this Carlina installation."
                ),
            }

        result.update(
            {
                "state": container_status,
                "container_status": container_status,
            }
        )
        if internal_port is not None and container_status == "running":
            try:
                result["base_url"] = self._get_container_base_url(
                    container,
                    int(internal_port),
                )
            except (docker.errors.APIError, RuntimeError, TypeError, ValueError) as exc:
                result.update({"state": "misconfigured", "error": str(exc)})
        return result

    def ensure_container_log_sources(
        self,
        addon_id: str,
        service_names: list[str] | tuple[str, ...],
    ) -> None:
        """Register log sources for declared services through the public API."""

        names = tuple(str(name) for name in service_names) or ("service",)
        self._register_docker_log_sources(
            str(addon_id),
            {name: {} for name in names},
        )

    def read_container_service_logs(
        self,
        addon_id: str,
        service_name: str = "service",
        *,
        lines: int = 100,
    ) -> str:
        """Read a bounded tail from one declared service container."""

        if not self._refresh_docker_status(log_failure=False):
            raise RuntimeError(self.docker_status_message)
        container = self.docker_client.containers.get(
            self._get_container_name(str(addon_id), str(service_name))
        )
        container.reload()
        if not self._docker_resource_is_owned(
            container,
            str(addon_id),
            "addon-container",
            service_name=str(service_name),
        ):
            raise RuntimeError(
                "Refusing to read logs from a Docker container owned by another "
                "installation."
            )
        payload = container.logs(tail=max(1, min(int(lines), 10_000)))
        if isinstance(payload, bytes):
            return payload.decode("utf-8", errors="replace")
        return str(payload)

    def manage_container_service(
        self,
        addon_id: str,
        service_name: str = "service",
        *,
        action: str,
    ) -> None:
        """Apply an allowlisted lifecycle action to one service container."""

        normalized_action = str(action).strip().lower()
        if normalized_action not in {"restart", "stop", "start", "remove"}:
            raise ValueError(f"Unknown container action: {action}")
        if not self._refresh_docker_status(log_failure=False):
            raise RuntimeError(self.docker_status_message)
        container = self.docker_client.containers.get(
            self._get_container_name(str(addon_id), str(service_name))
        )
        container.reload()
        if not self._docker_resource_is_owned(
            container,
            str(addon_id),
            "addon-container",
            service_name=str(service_name),
        ):
            raise RuntimeError(
                "Refusing to manage a Docker container owned by another installation."
            )
        if normalized_action == "remove":
            container.remove(force=True)
        else:
            getattr(container, normalized_action)()

    def touch_container_access(self, addon_id: str) -> None:
        """Mark an add-on container as active through the public manager API."""

        self._touch_container_access(str(addon_id))

    def _container_use_guard(
        self, addon_id: str, *, timeout: float = 0, exclusive: bool = True
    ):
        """Crash-released use lock; shared requests, exclusive idle reaping.

        A lock I/O failure is not evidence that the service is idle. Keep the
        sentinel after use: unlinking it could create independent locks for
        the same service. It contains no document data.
        """
        import errno

        marker = self._container_access_marker_path(addon_id)
        path = marker.with_name(marker.name + ".use.lock")
        if fcntl is None:  # pragma: no cover - Windows-only safe serial fallback.
            return FileLock(str(path), timeout=timeout)

        @contextmanager
        def _flock_hold():
            with path.open("a+b") as handle:
                mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
                deadline = time.monotonic() + max(0.0, float(timeout))
                while True:
                    try:
                        fcntl.flock(handle.fileno(), mode | fcntl.LOCK_NB)
                        break
                    except OSError as exc:
                        if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK):
                            raise
                        if exclusive or time.monotonic() >= deadline:
                            raise FileLockTimeout(str(path)) from exc
                        time.sleep(0.05)
                try:
                    yield
                finally:
                    # Closing the descriptor also releases the OS lock, even
                    # when an exceptional filesystem error prevents LOCK_UN.
                    try:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                    except OSError:
                        pass

        return _flock_hold()

    @contextmanager
    def container_use(self, addon_id: str, *, timeout: float = 600):
        """Hold shared cross-process use while a bounded service request runs.

        Concurrent holders coexist; the idle reaper cannot take its
        exclusive guard until every holder exits. Touch failures are
        best-effort so marker I/O never fails an otherwise good request.
        """
        with self._container_use_guard(
            str(addon_id), timeout=timeout, exclusive=False
        ):
            try:
                self._touch_container_access(str(addon_id))
            except Exception:  # noqa: BLE001 - marker I/O must never fail a live request
                _LOGGER.warning(
                    "Could not touch container access for '%s'.", addon_id
                )
            try:
                yield
            finally:
                try:
                    self._touch_container_access(str(addon_id))
                except Exception:  # noqa: BLE001 - marker I/O must never mask the request outcome
                    _LOGGER.warning(
                        "Could not touch container access for '%s'.", addon_id
                    )

    def reset_container(self, addon_id: str) -> None:
        """Stop an add-on's container stack so its next request starts fresh."""

        self._stop_and_remove_container(str(addon_id))

    def _touch_container_access(self, addon_id: str):
        """Cross-process safe way to mark a container as actively used."""
        now = time.time()
        # Keep an in-memory copy that is immune to OneDrive silently resetting the
        # file's mtime to a stale cloud version (which would trigger premature reaping).
        self._container_last_accessed_ts[addon_id] = now
        try:
            access_file = self._container_access_marker_path(addon_id)
            access_file.touch(exist_ok=True)
        except OSError as e:
            _LOGGER.warning("Could not touch access file for '%s': %s", addon_id, e)

    def _get_container_last_accessed(self, addon_id: str) -> float:
        """Returns the most recent access timestamp for the container.

        Uses max(in_memory_ts, file_mtime) so that OneDrive silently resetting the
        access file's mtime to an older cloud version never causes a false-positive
        idle reap while a request is actively in flight.
        """
        # In-memory value: always current for this process.
        in_memory_ts = self._container_last_accessed_ts.get(addon_id, 0.0)

        # File value: cross-process (e.g. worker subprocesses) but may lag due to OneDrive.
        access_file = self._container_access_marker_path(addon_id)
        file_ts = access_file.stat().st_mtime if access_file.exists() else 0.0

        best = max(in_memory_ts, file_ts)
        # If neither source has a timestamp (newly created stack), treat as just-accessed.
        return best if best > 0.0 else time.time()

    @staticmethod
    def _get_legacy_container_name(
        addon_id: str,
        service_name: str = "service",
    ) -> str:
        return f"iarf-addon-{addon_id}-{service_name}"

    @staticmethod
    def _get_legacy_network_name(addon_id: str) -> str:
        return f"iarf-addon-{addon_id}-net"

    @staticmethod
    def _get_legacy_image_name(addon_id: str) -> str:
        return f"iarf-addon-{addon_id}:latest"

    def _get_ordered_services(self, services: dict) -> list[str]:
        """Performs a topological sort of services based on depends_on."""
        ordered = []
        pending = set(services.keys())

        while pending:
            progress = False
            for name in list(pending):
                deps = services[name].get("depends_on", [])
                missing_deps = [dep for dep in deps if dep not in services]
                if missing_deps:
                    raise ValueError(
                        f"Service '{name}' depends on missing service(s): {', '.join(missing_deps)}."
                    )

                if all(dep not in pending for dep in deps):
                    ordered.append(name)
                    pending.remove(name)
                    progress = True

            if not progress:
                raise ValueError("Circular dependency detected in Docker services.")

        return ordered

    def _get_service_image_name(
        self,
        addon_id: str,
        service_name: str,
        service_config: dict,
        manifest: dict | None,
    ) -> str:
        """Resolve an explicit image or a versioned local-build identity."""

        explicit_image = str(service_config.get("image") or "").strip()
        if explicit_image:
            return explicit_image

        addon_component = self._docker_name_component(addon_id, default="addon")
        service_component = self._docker_name_component(
            service_name,
            default="service",
        )
        version_component = self._docker_name_component(
            (manifest or {}).get("version"),
            default="dev",
        )
        product_component = self._docker_name_component(
            PRODUCT_IDENTITY.distribution_name,
            default="carlina",
        )
        repository = self._bounded_docker_name(
            (
                f"{product_component}-addon-{self._container_installation_id()}-"
                f"{addon_component}-{service_component}"
            ),
            max_length=128,
        )
        return f"{repository}:{version_component}"

    def _legacy_local_build_image_matches(
        self,
        image_reference: str,
        addon_id: str,
        service_name: str,
    ) -> bool:
        """Match an exact pre-installation-scoped local image repository.

        The tag is intentionally allowed to differ from the current manifest
        version. A built-in update can publish a new manifest before Carlina
        first encounters and replaces the container from the previous version.
        """

        addon_component = self._docker_name_component(addon_id, default="addon")
        service_component = self._docker_name_component(
            service_name,
            default="service",
        )
        product_component = self._docker_name_component(
            PRODUCT_IDENTITY.distribution_name,
            default="carlina",
        )
        repository = f"{product_component}-addon-{addon_component}-{service_component}"
        prefix = f"{repository}:"
        if not image_reference.startswith(prefix):
            return False
        return bool(_DOCKER_TAG_PATTERN.fullmatch(image_reference[len(prefix) :]))

    def _docker_resource_labels(
        self,
        addon_id: str,
        resource_type: str,
        *,
        service_name: str | None = None,
        manifest: dict | None = None,
        operation_id: str | None = None,
    ) -> dict[str, str]:
        labels = {
            _DOCKER_MANAGED_LABEL: "true",
            _DOCKER_INSTALLATION_LABEL: self._container_installation_id(),
            _DOCKER_RESOURCE_LABEL: str(resource_type),
            _DOCKER_ADDON_LABEL: str(addon_id),
        }
        if service_name is not None:
            labels[_DOCKER_SERVICE_LABEL] = str(service_name)
        version = str((manifest or {}).get("version") or "").strip()
        if version:
            labels[_DOCKER_VERSION_LABEL] = version
        if operation_id:
            labels[_DOCKER_OPERATION_LABEL] = str(operation_id)
        return labels

    def _remove_owned_build_stage_images(
        self,
        addon_id: str,
        service_name: str,
        operation_id: str,
    ) -> None:
        """Remove intermediate images explicitly attributed to one build.

        Docker's ``--force-rm`` removes intermediate containers, not every
        image emitted by a multi-stage BuildKit build. Participating
        Dockerfiles label their build stages with the operation metadata the
        broker supplies, allowing exact cleanup without a global image prune.
        """

        expected_labels = self._docker_resource_labels(
            addon_id,
            "addon-build-stage",
            service_name=service_name,
            operation_id=operation_id,
        )
        try:
            images = self.docker_client.images.list()
        except docker.errors.APIError as error:
            _LOGGER.warning(
                "Could not enumerate Docker build stages for add-on '%s': %s",
                addon_id,
                error,
            )
            return

        for image in images:
            try:
                reload_image = getattr(image, "reload", None)
                if callable(reload_image):
                    reload_image()
                labels = self._inspected_docker_labels(image)
                if not all(
                    labels.get(key) == value for key, value in expected_labels.items()
                ):
                    continue
                image_id = str(getattr(image, "id", "") or "").strip()
                if not image_id:
                    continue
                self.docker_client.images.remove(image_id, force=True)
                self._append_docker_log(
                    addon_id,
                    service_name,
                    f"Removed Docker build-stage image '{image_id}'.",
                )
            except docker.errors.APIError as error:
                _LOGGER.warning(
                    "Could not remove owned Docker build-stage image for add-on "
                    "'%s' service '%s': %s",
                    addon_id,
                    service_name,
                    error,
                )

    @staticmethod
    def _inspected_docker_labels(resource: object) -> dict[str, str]:
        attrs = getattr(resource, "attrs", None)
        if not isinstance(attrs, dict):
            return {}
        direct_labels = attrs.get("Labels")
        config = attrs.get("Config")
        config_labels = config.get("Labels") if isinstance(config, dict) else None
        labels = direct_labels if isinstance(direct_labels, dict) else config_labels
        if not isinstance(labels, dict):
            return {}
        return {str(key): str(value) for key, value in labels.items()}

    def _docker_resource_is_owned(
        self,
        resource: object,
        addon_id: str,
        resource_type: str,
        *,
        service_name: str | None = None,
    ) -> bool:
        expected = self._docker_resource_labels(
            addon_id,
            resource_type,
            service_name=service_name,
        )
        labels = self._inspected_docker_labels(resource)
        return all(labels.get(key) == value for key, value in expected.items())

    def identify_container_resource(
        self,
        container: object,
    ) -> dict[str, str | bool] | None:
        """Identify a container owned by this installation, including legacy ones."""

        try:
            reload_container = getattr(container, "reload", None)
            if callable(reload_container):
                reload_container()
        except docker.errors.APIError:
            return None

        labels = self._inspected_docker_labels(container)
        addon_id = str(labels.get(_DOCKER_ADDON_LABEL) or "").strip()
        service_name = str(labels.get(_DOCKER_SERVICE_LABEL) or "").strip()
        resource_type = str(labels.get(_DOCKER_RESOURCE_LABEL) or "").strip()
        container_name = str(getattr(container, "name", "") or "").lstrip("/")
        if (
            addon_id
            and service_name
            and resource_type == "addon-container"
            and container_name == self._get_container_name(addon_id, service_name)
            and self._docker_resource_is_owned(
                container,
                addon_id,
                resource_type,
                service_name=service_name,
            )
        ):
            return {
                "managed": True,
                "legacy": False,
                "addon_id": addon_id,
                "service_name": service_name,
                "installation_id": self._container_installation_id(),
            }

        manifests = getattr(self, "manifests", {}) or {}
        for candidate_id in sorted(manifests, key=len, reverse=True):
            prefix = f"{self._get_legacy_container_name(candidate_id, '')}"
            if not container_name.startswith(prefix):
                continue
            candidate_service = container_name[len(prefix) :]
            manifest = self._get_addon_manifest(str(candidate_id)) or {}
            services = self._get_docker_services(manifest.get("docker") or {})
            service_config = services.get(candidate_service)
            if service_config is None:
                continue
            if self._legacy_container_is_compatible(
                container,
                candidate_id,
                candidate_service,
                service_config,
                manifest,
            ):
                return {
                    "managed": True,
                    "legacy": True,
                    "addon_id": str(candidate_id),
                    "service_name": candidate_service,
                    "installation_id": self._container_installation_id(),
                }
        return None

    def identify_image_resource(
        self,
        image: object,
    ) -> dict[str, str | bool] | None:
        """Identify a locally built image owned by this installation."""

        labels = self._inspected_docker_labels(image)
        addon_id = str(labels.get(_DOCKER_ADDON_LABEL) or "").strip()
        service_name = str(labels.get(_DOCKER_SERVICE_LABEL) or "").strip()
        if (
            not addon_id
            or not service_name
            or labels.get(_DOCKER_RESOURCE_LABEL) != "addon-image"
            or not self._docker_resource_is_owned(
                image,
                addon_id,
                "addon-image",
                service_name=service_name,
            )
        ):
            return None
        return {
            "managed": True,
            "legacy": False,
            "addon_id": addon_id,
            "service_name": service_name,
            "installation_id": self._container_installation_id(),
        }

    def _legacy_labels_are_compatible(
        self,
        resource: object,
        addon_id: str,
        resource_type: str,
        *,
        service_name: str | None = None,
    ) -> bool:
        labels = self._inspected_docker_labels(resource)
        expected = self._docker_resource_labels(
            addon_id,
            resource_type,
            service_name=service_name,
        )
        for key, expected_value in expected.items():
            if key in labels and labels[key] != expected_value:
                return False
        return True

    def _legacy_container_is_compatible(
        self,
        container: object,
        addon_id: str,
        service_name: str,
        service_config: dict,
        manifest: dict | None,
    ) -> bool:
        expected_name = self._get_legacy_container_name(addon_id, service_name)
        actual_name = str(getattr(container, "name", "") or "").lstrip("/")
        if actual_name != expected_name:
            return False
        if not self._legacy_labels_are_compatible(
            container,
            addon_id,
            "addon-container",
            service_name=service_name,
        ):
            return False

        attrs = getattr(container, "attrs", None)
        config = attrs.get("Config") if isinstance(attrs, dict) else None
        configured_image = (
            str(config.get("Image") or "").strip() if isinstance(config, dict) else ""
        )
        image = getattr(container, "image", None)
        image_tags = {
            str(tag).strip()
            for tag in (getattr(image, "tags", None) or [])
            if str(tag).strip()
        }
        observed_images = image_tags | (
            {configured_image} if configured_image else set()
        )
        explicit_image = str(service_config.get("image") or "").strip()
        if explicit_image:
            # Pulled images are external identities. Never broaden compatibility
            # beyond the exact manifest reference merely because the container
            # happens to use Carlina's historical name.
            return explicit_image in observed_images

        exact_images = {
            self._get_service_image_name(
                addon_id,
                service_name,
                service_config,
                manifest,
            ),
            self._get_legacy_image_name(addon_id),
        }
        return bool(observed_images & exact_images) or any(
            self._legacy_local_build_image_matches(
                image_reference,
                addon_id,
                service_name,
            )
            for image_reference in observed_images
        )

    def _legacy_network_is_compatible(
        self,
        network: object,
        addon_id: str,
    ) -> bool:
        actual_name = str(getattr(network, "name", "") or "")
        if actual_name != self._get_legacy_network_name(addon_id):
            return False
        if not self._legacy_labels_are_compatible(
            network,
            addon_id,
            "addon-network",
        ):
            return False
        attrs = getattr(network, "attrs", None)
        driver = attrs.get("Driver") if isinstance(attrs, dict) else None
        attached = attrs.get("Containers") if isinstance(attrs, dict) else None
        return driver == "bridge" and not attached

    def _log_unowned_docker_resource(
        self,
        addon_id: str,
        service_name: str,
        resource_type: str,
        resource_name: str,
    ) -> None:
        message = (
            f"Refusing to modify {resource_type} '{resource_name}' because its "
            "Carlina ownership metadata does not match this installation."
        )
        _LOGGER.warning(message)
        self._append_docker_log(addon_id, service_name, message)

    def _ensure_addon_network(
        self,
        addon_id: str,
        service_name: str,
        manifest: dict | None,
    ) -> str:
        network_name = self._get_network_name(addon_id)
        try:
            network = self.docker_client.networks.get(network_name)
        except docker.errors.NotFound:
            labels = self._docker_resource_labels(
                addon_id,
                "addon-network",
                manifest=manifest,
            )
            try:
                network = self.docker_client.networks.create(
                    network_name,
                    driver="bridge",
                    labels=labels,
                )
                self._append_docker_log(
                    addon_id,
                    service_name,
                    f"Created Docker network '{network_name}'.",
                )
            except docker.errors.APIError as create_error:
                # Another Carlina process may have won the name-creation race.
                try:
                    network = self.docker_client.networks.get(network_name)
                except docker.errors.NotFound:
                    raise create_error

        reload_network = getattr(network, "reload", None)
        if callable(reload_network):
            reload_network()
        if not self._docker_resource_is_owned(
            network,
            addon_id,
            "addon-network",
        ):
            self._log_unowned_docker_resource(
                addon_id,
                service_name,
                "Docker network",
                network_name,
            )
            raise RuntimeError(
                f"Docker network name collision for '{network_name}'. Remove or "
                "rename the unrelated resource before starting this add-on."
            )
        return network_name

    @staticmethod
    def _get_service_gpu_requirement(
        manifest: dict | None,
        service_name: str,
        service_config: dict,
    ) -> str:
        """Resolve GPU policy, preferring the schema-2 runtime contract.

        ``gpu_required`` remains a compatibility fallback for schema-1 add-ons.
        A matching schema-2 container unit is authoritative, including when it
        omits ``requirements.gpu`` (whose contract default is ``none``).
        """

        runtime = (manifest or {}).get("runtime")
        units = runtime.get("units") if isinstance(runtime, dict) else None
        if isinstance(units, list):
            for unit in units:
                if not isinstance(unit, dict) or unit.get("mode") != "container":
                    continue
                config = unit.get("config")
                config = config if isinstance(config, dict) else {}
                declared_service = config.get("service") or unit.get("id")
                if str(declared_service) != str(service_name):
                    continue
                requirements = unit.get("requirements")
                requirements = requirements if isinstance(requirements, dict) else {}
                requirement = str(requirements.get("gpu") or "none").strip().lower()
                if requirement not in _VALID_GPU_REQUIREMENTS:
                    raise ValueError(
                        f"Container service '{service_name}' has invalid GPU requirement "
                        f"{requirement!r}; expected none, optional, or required."
                    )
                return requirement

        compatibility_requirement = service_config.get("gpu")
        if compatibility_requirement is not None:
            compatibility_requirement = str(compatibility_requirement).strip().lower()
            if compatibility_requirement not in _VALID_GPU_REQUIREMENTS:
                raise ValueError(
                    f"Container service '{service_name}' has invalid GPU requirement "
                    f"{compatibility_requirement!r}; expected none, optional, or required."
                )
            return compatibility_requirement
        return "required" if service_config.get("gpu_required", False) else "none"

    @staticmethod
    def _is_gpu_runtime_error(error: Exception) -> bool:
        message = str(error).lower()
        if "timed out after" in message:
            return False
        exact_markers = (
            "could not select device driver",
            "no compatible gpu",
            "nvidia-container-cli",
            "unknown or invalid runtime name: nvidia",
            "requested device not found",
            "no devices found",
            "failed to inject cdi devices",
            "failed to discover gpu vendor from cdi",
            "no known gpu vendor found",
            "capabilities: [[gpu]]",
        )
        return any(marker in message for marker in exact_markers) or (
            "nvidia" in message
            and any(marker in message for marker in ("device", "driver", "runtime"))
        )

    def _run_container_with_gpu_policy(
        self,
        *,
        addon_id: str,
        service_name: str,
        image_name: str,
        run_kwargs: dict,
        gpu_requirement: str,
    ):
        """Start one service, falling back to CPU only for optional GPUs."""

        if gpu_requirement not in _VALID_GPU_REQUIREMENTS:
            raise ValueError(f"Invalid GPU requirement: {gpu_requirement!r}.")

        effective_kwargs = dict(run_kwargs)
        if gpu_requirement in {"optional", "required"}:
            effective_kwargs["device_requests"] = [
                docker.types.DeviceRequest(count=-1, capabilities=[["gpu"]])
            ]
        else:
            effective_kwargs.pop("device_requests", None)

        try:
            return self.docker_client.containers.run(image_name, **effective_kwargs)
        except docker.errors.APIError as error:
            is_gpu_error = self._is_gpu_runtime_error(error)
            if gpu_requirement == "required" and is_gpu_error:
                raise RuntimeError(
                    "A GPU is required, but the container runtime could not expose one. "
                    f"Docker error: {error}"
                ) from error
            if gpu_requirement != "optional" or not is_gpu_error:
                raise RuntimeError(
                    f"Failed to start service '{service_name}': {error}"
                ) from error

            self._append_docker_log(
                addon_id,
                service_name,
                "No usable container GPU was detected; retrying this optional-GPU "
                "service on CPU.",
            )
            container_name = effective_kwargs.get("name")
            if container_name:
                try:
                    stale_container = self.docker_client.containers.get(container_name)
                    stale_container.reload()
                    if self._docker_resource_is_owned(
                        stale_container,
                        addon_id,
                        "addon-container",
                        service_name=service_name,
                    ):
                        stale_container.remove(force=True)
                    else:
                        self._log_unowned_docker_resource(
                            addon_id,
                            service_name,
                            "Docker container",
                            str(container_name),
                        )
                except docker.errors.NotFound:
                    pass
                except docker.errors.APIError as cleanup_error:
                    _LOGGER.warning(
                        "Could not clean up failed optional-GPU container '%s': %s",
                        container_name,
                        cleanup_error,
                    )

            effective_kwargs.pop("device_requests", None)
            try:
                return self.docker_client.containers.run(
                    image_name,
                    **effective_kwargs,
                )
            except docker.errors.APIError as fallback_error:
                raise RuntimeError(
                    f"Failed to start optional-GPU service '{service_name}' on CPU: "
                    f"{fallback_error}"
                ) from fallback_error

    @staticmethod
    def _coerce_positive_int(value, default: int) -> int:
        try:
            parsed = int(value)
            if parsed > 0:
                return parsed
        except TypeError, ValueError:
            pass
        return default

    def _get_primary_service_name(self, services: dict[str, dict]) -> str:
        if not services:
            raise ValueError("No Docker services are configured.")
        ordered_services = self._get_ordered_services(services)
        return next(
            (
                name
                for name, config in services.items()
                if isinstance(config, dict) and config.get("primary")
            ),
            ordered_services[-1],
        )

    def _get_effective_startup_timeout(
        self, docker_config: dict, primary_config: dict
    ) -> int:
        timeout_value = primary_config.get(
            "startup_timeout_seconds", docker_config.get("startup_timeout_seconds", 120)
        )
        return self._coerce_positive_int(timeout_value, 120)

    def _get_effective_idle_timeout(
        self, docker_config: dict, primary_config: dict
    ) -> int:
        timeout_value = primary_config.get(
            "idle_timeout_seconds", docker_config.get("idle_timeout_seconds", 300)
        )
        return self._coerce_positive_int(timeout_value, 300)

    def _get_addon_container_lock(self, addon_id: str):
        with self._container_lock:
            lock = self._addon_container_locks.get(addon_id)
            if lock is None:
                lock = threading.RLock()
                self._addon_container_locks[addon_id] = lock
            return lock

    def _docker_image_exists(self, image_name: str) -> bool:
        if not self.docker_available or not self.docker_client:
            return False

        try:
            self.docker_client.images.get(image_name)
            return True
        except docker.errors.NotFound:
            return False

    @staticmethod
    def _resolve_docker_status_ttl_seconds() -> float:
        raw_value = str(
            product_environment_value(
                "DOCKER_STATUS_TTL_SECONDS",
                default="",
            )
        ).strip()
        if not raw_value:
            return _DEFAULT_DOCKER_STATUS_TTL_SECONDS
        try:
            parsed = float(raw_value)
        except ValueError:
            return _DEFAULT_DOCKER_STATUS_TTL_SECONDS
        return max(1.0, parsed)

    def _refresh_docker_status(
        self,
        log_failure: bool = False,
        *,
        force: bool = False,
    ) -> bool:
        if not getattr(self, "container_services_enabled", True):
            self.docker_available = False
            self.docker_status_message = (
                "This application does not include container services."
            )
            return False
        lock = getattr(self, "_docker_status_lock", None)
        if lock is None:
            lock = threading.RLock()
            self._docker_status_lock = lock

        with lock:
            now = time.monotonic()
            checked_at = float(getattr(self, "_docker_status_checked_at", 0.0) or 0.0)
            ttl = float(
                getattr(
                    self,
                    "_docker_status_ttl_seconds",
                    _DEFAULT_DOCKER_STATUS_TTL_SECONDS,
                )
            )
            if (
                not force
                and bool(getattr(self, "docker_status_checked", False))
                and now - checked_at < ttl
            ):
                return bool(self.docker_available)

            available = self._probe_docker_status(
                log_failure=log_failure,
            )
            self.docker_status_checked = True
            self._docker_status_checked_at = time.monotonic()
            return available

    def _probe_docker_status(self, log_failure: bool = False) -> bool:
        current_kind = getattr(self, "_container_runtime_kind", None)
        if not current_kind:
            current_kind = (
                CONTAINER_RUNTIME_WSL_CLI
                if getattr(self, "_docker_backend", "none") == "wsl_cli"
                else CONTAINER_RUNTIME_DOCKER_SDK
                if getattr(self, "_docker_backend", "none") == "sdk"
                else CONTAINER_RUNTIME_NONE
            )
        selection = select_container_runtime(
            docker_module=docker,
            existing_client=getattr(self, "docker_client", None),
            existing_kind=current_kind,
            existing_wsl_distro=getattr(self, "_docker_wsl_distro", None),
            platform_name=platform.system(),
            wsl_client_factory=self._create_wsl_docker_client,
        )
        self.docker_client = selection.client
        self.docker_available = selection.available
        self._container_runtime_kind = selection.kind
        self._docker_backend = (
            "wsl_cli"
            if selection.kind == CONTAINER_RUNTIME_WSL_CLI
            else "sdk"
            if selection.kind == CONTAINER_RUNTIME_DOCKER_SDK
            else "none"
        )
        self._docker_wsl_distro = selection.wsl_distro
        self.docker_status_message = selection.detail
        if selection.available:
            if selection.kind == CONTAINER_RUNTIME_WSL_CLI:
                distro_suffix = (
                    f" ({selection.wsl_distro})" if selection.wsl_distro else ""
                )
                self.docker_status_message = (
                    f"Docker Engine is available via WSL{distro_suffix}."
                )
            return True
        if log_failure:
            _LOGGER.warning(self.docker_status_message)
            self._append_registered_log(
                "docker.addon_services",
                self.docker_status_message,
            )
        return False

    def _create_wsl_docker_client(self):
        """Compatibility hook for the extracted WSL container backend."""

        return create_wsl_docker_client()

    def _ensure_docker_image_prepared(
        self,
        addon_id: str,
        manifest: dict | None = None,
        force_refresh: bool = False,
    ):
        if not self._refresh_docker_status(log_failure=False):
            raise RuntimeError(
                "Docker Engine is not available. Cannot prepare Docker images."
            )

        manifest = manifest or self._get_addon_manifest(addon_id)
        docker_config = manifest.get("docker") if manifest else None
        if not docker_config:
            return

        services = self._get_docker_services(docker_config)
        prepared_images = set()
        self._register_docker_log_sources(addon_id, services)

        for srv_name, srv_config in services.items():
            strategy = srv_config.get("strategy", "pull")
            image_name = self._get_service_image_name(
                addon_id,
                srv_name,
                srv_config,
                manifest,
            )
            cache_key = (strategy, image_name)

            if cache_key in prepared_images:
                continue

            if strategy == "pull":
                if force_refresh or not self._docker_image_exists(image_name):
                    _LOGGER.info(
                        f"Pulling Docker image '{image_name}' for service '{srv_name}'..."
                    )
                    self._append_docker_log(
                        addon_id, srv_name, f"Pulling Docker image '{image_name}'."
                    )
                    try:
                        self.docker_client.images.pull(image_name)
                        self._append_docker_log(
                            addon_id, srv_name, f"Pulled Docker image '{image_name}'."
                        )
                    except Exception as e:
                        self._append_docker_log(
                            addon_id,
                            srv_name,
                            f"Failed to pull Docker image '{image_name}': {e}",
                        )
                        raise
            elif strategy == "build":
                build_context = (
                    srv_config.get("build_context")
                    or docker_config.get("build_context")
                    or "container"
                )
                addon_dir = self.get_addon_directory(addon_id).resolve()
                container_dir = (addon_dir / str(build_context)).resolve()
                if not container_dir.is_relative_to(addon_dir):
                    raise PermissionError(
                        f"Docker build context for addon '{addon_id}' escapes the addon directory."
                    )
                if not container_dir.is_dir():
                    raise FileNotFoundError(
                        f"Docker build context not found for addon '{addon_id}': {container_dir}"
                    )

                image_exists = self._docker_image_exists(image_name)
                if image_exists:
                    existing_image = self.docker_client.images.get(image_name)
                    if not self._docker_resource_is_owned(
                        existing_image,
                        addon_id,
                        "addon-image",
                        service_name=srv_name,
                    ):
                        self._log_unowned_docker_resource(
                            addon_id,
                            srv_name,
                            "Docker image",
                            image_name,
                        )
                        raise RuntimeError(
                            f"Docker image name collision for '{image_name}'. Remove "
                            "or retag the unrelated image before building this add-on."
                        )

                if force_refresh or not image_exists:
                    _LOGGER.info(
                        f"Building Docker image '{image_name}' for service '{srv_name}'..."
                    )
                    self._append_docker_log(
                        addon_id,
                        srv_name,
                        f"Building Docker image '{image_name}' from '{build_context}'.",
                    )
                    build_operation_id = uuid.uuid4().hex
                    try:
                        build_path = str(container_dir)
                        if self._docker_backend == "wsl_cli":
                            build_path = self._translate_to_wsl2_path(container_dir)
                        self.docker_client.images.build(
                            path=build_path,
                            tag=image_name,
                            rm=True,
                            forcerm=True,
                            buildargs={
                                "CARLINA_MANAGED": "true",
                                "CARLINA_INSTALLATION_ID": self._container_installation_id(),
                                "CARLINA_ADDON_ID": str(addon_id),
                                "CARLINA_SERVICE_ID": str(srv_name),
                                "CARLINA_ADDON_VERSION": str(
                                    (manifest or {}).get("version") or ""
                                ),
                                "CARLINA_BUILD_OPERATION_ID": build_operation_id,
                            },
                            labels=self._docker_resource_labels(
                                addon_id,
                                "addon-image",
                                service_name=srv_name,
                                manifest=manifest,
                            ),
                        )
                        self._append_docker_log(
                            addon_id, srv_name, f"Built Docker image '{image_name}'."
                        )
                    except Exception as e:
                        self._append_docker_log(
                            addon_id,
                            srv_name,
                            f"Failed to build Docker image '{image_name}': {e}",
                        )
                        raise
                    finally:
                        self._remove_owned_build_stage_images(
                            addon_id,
                            srv_name,
                            build_operation_id,
                        )
            else:
                self._append_docker_log(
                    addon_id, srv_name, f"Unsupported Docker strategy '{strategy}'."
                )
                raise ValueError(
                    f"Unsupported Docker strategy '{strategy}' for service '{srv_name}'."
                )

            prepared_images.add(cache_key)

    def _is_wsl2_volume_translation_enabled(self) -> bool:
        value = os.environ.get("DOCKER_WSL2_VOLUME_TRANSLATION", "")
        if platform.system() != "Windows":
            return False

        # If we are actively using the WSL Docker CLI backend, path translation is mandatory.
        if self._docker_backend == "wsl_cli":
            if value.strip().lower() in {"0", "false", "no", "off"}:
                _LOGGER.warning(
                    "DOCKER_WSL2_VOLUME_TRANSLATION is disabled, but WSL Docker backend is active. "
                    "Volume mounts may fail."
                )
                return False
            return True

        return value.strip().lower() in {"1", "true", "yes", "on"}

    def _translate_to_wsl2_path(self, host_path) -> str:
        """
        Translates a Windows host path (C:\\projects\\app) into a WSL2 path
        (/mnt/c/projects/app) for Linux Docker daemons running inside WSL2.
        """
        return ContainerStorageBroker(CONTAINER_RUNTIME_WSL_CLI).host_path(host_path)

    def _resolve_docker_volumes(self, addon_id: str, docker_config: dict) -> dict:
        volumes = {}

        with self.app.app_context():
            uploads_dir = uploads_root()
        uploads_dir.mkdir(parents=True, exist_ok=True)

        app_paths = self.app.extensions["carlina_paths"]
        canonical_root = Path(app_paths.addon_data_dir).resolve()
        addon_data_dir = (canonical_root / addon_id).resolve()
        if not addon_data_dir.is_relative_to(canonical_root):
            raise ValueError(f"Unsafe add-on ID for Docker storage: {addon_id!r}")

        legacy_root = Path(app_paths.addons_dir).resolve()
        legacy_data_dir = (legacy_root / addon_id / "data").resolve()
        if not legacy_data_dir.is_relative_to(legacy_root):
            raise ValueError(f"Unsafe add-on ID for Docker storage: {addon_id!r}")
        if legacy_data_dir.is_dir() and not addon_data_dir.exists():
            addon_data_dir = legacy_data_dir
        addon_data_dir.mkdir(parents=True, exist_ok=True)

        token_map = {
            "{uploads_dir}": (uploads_dir, True),
            "{addon_data_dir}": (addon_data_dir, False),
        }

        for host_token, container_path in (docker_config.get("volumes") or {}).items():
            host_token_str = str(host_token)
            host_path_obj = None
            shared_mount = False

            for token, (base_path, token_is_shared) in token_map.items():
                if host_token_str == token:
                    host_path_obj = Path(base_path).resolve()
                    shared_mount = token_is_shared
                    break

                if host_token_str.startswith((f"{token}/", f"{token}\\")):
                    relative_path = (
                        host_token_str[len(token) :].lstrip("/").lstrip("\\")
                    )

                    # Prevent path traversal attacks like '../../etc'
                    if ".." in Path(relative_path).parts:
                        raise ValueError(
                            f"Path traversal not allowed in volume mount: {host_token}"
                        )

                    base_path_obj = Path(base_path).resolve()
                    host_path_obj = (base_path_obj / relative_path).resolve()
                    shared_mount = token_is_shared

                    # Double-check it doesn't escape the token's base directory
                    if not host_path_obj.is_relative_to(base_path_obj):
                        raise PermissionError(
                            f"Volume mapping '{host_token}' escapes the allowed directory."
                        )

                    break

            if host_path_obj is None:
                # If it's a simple name without slashes or braces, treat it as a Docker named volume.
                if (
                    "/" not in host_token_str
                    and "\\" not in host_token_str
                    and "{" not in host_token_str
                ):
                    volumes[host_token_str] = {"bind": container_path, "mode": "rw"}
                    continue

                # Treat unknown tokens as subdirectories inside the addon's data folder securely
                safe_token = host_token_str.lstrip("/").lstrip("\\")

                # Prevent path traversal attacks like '../../etc'
                if ".." in Path(safe_token).parts:
                    raise ValueError(
                        f"Path traversal not allowed in volume mount: {host_token}"
                    )

                host_path_obj = (addon_data_dir / safe_token).resolve()

                # Double-check it doesn't escape the addon data directory
                if not host_path_obj.is_relative_to(addon_data_dir):
                    raise PermissionError(
                        f"Volume mapping '{host_token}' escapes the addon data directory."
                    )

            # Only create the host path as a directory if it isn't already a file.
            # File bind-mounts (e.g. a YAML config) must be left as-is; calling mkdir
            # on them would replace the file with a directory of the same name.
            if host_path_obj.is_file():
                pass  # mount the file directly — no directory creation needed
            else:
                host_path_obj.mkdir(parents=True, exist_ok=True)

            mount_mode = ContainerStorageBroker.bind_mount_mode(shared=shared_mount)
            ContainerStorageBroker.ensure_bind_mount_supported(
                host_path_obj,
                mode=mount_mode,
            )

            host_path_str = str(host_path_obj)
            if self._is_wsl2_volume_translation_enabled():
                host_path_str = self._translate_to_wsl2_path(host_path_obj)

            volumes[host_path_str] = {
                "bind": container_path,
                "mode": mount_mode,
            }

        return volumes

    def _get_container_base_url(self, container, internal_port: int) -> str:
        container.reload()
        port_key = f"{internal_port}/tcp"
        port_bindings = (
            container.attrs.get("NetworkSettings", {}).get("Ports", {}).get(port_key)
        )

        if not port_bindings and getattr(container, "ports", None):
            port_bindings = container.ports.get(port_key)

        if not port_bindings:
            raise RuntimeError(f"Container did not publish expected port '{port_key}'.")

        if isinstance(port_bindings, dict):
            host_port = port_bindings.get("HostPort")
        else:
            host_port = port_bindings[0].get("HostPort") if port_bindings else None

        if not host_port:
            raise RuntimeError(
                f"Container port mapping for '{port_key}' is incomplete."
            )

        return f"http://localhost:{host_port}"

    def _stop_and_remove_container(
        self,
        addon_id: str,
        remove_image: bool = False,
        manifest: dict[str, Any] | None = None,
    ) -> None:
        manifest = manifest or self._get_addon_manifest(addon_id)
        addon_lock = self._get_addon_container_lock(addon_id)

        with addon_lock:
            with self._container_lock:
                containers_dict = self._active_containers.pop(addon_id, {})
            # Clear the in-memory timestamp so it doesn't keep the container "alive" after removal.
            self._container_last_accessed_ts.pop(addon_id, None)

            access_file = self._container_access_marker_path(addon_id)
            try:
                access_file.unlink()
            except FileNotFoundError:
                pass
            except OSError as e:
                _LOGGER.warning(
                    f"Failed to remove access file for addon '{addon_id}': {e}"
                )

            if not isinstance(containers_dict, dict):
                containers_dict = (
                    {"service": containers_dict} if containers_dict else {}
                )

            self._refresh_docker_status(log_failure=False)
            if self.docker_available and self.docker_client:
                docker_config = (manifest or {}).get("docker") or {}
                services = self._get_docker_services(docker_config)
                service_names = (
                    list(services.keys()) or list(containers_dict.keys()) or ["service"]
                )
                self._register_docker_log_sources(
                    addon_id, services or {name: {} for name in service_names}
                )
                legacy_container_removed = False

                for srv_name in service_names:
                    service_config = services.get(srv_name) or {}
                    current_name = self._get_container_name(addon_id, srv_name)
                    legacy_name = self._get_legacy_container_name(
                        addon_id,
                        srv_name,
                    )
                    candidates = []
                    active_container = containers_dict.get(srv_name)
                    if active_container is not None:
                        candidates.append(active_container)
                    for candidate_name in (current_name, legacy_name):
                        try:
                            candidates.append(
                                self.docker_client.containers.get(candidate_name)
                            )
                        except docker.errors.NotFound:
                            pass

                    seen_resources: set[str] = set()
                    for container in candidates:
                        try:
                            container.reload()
                        except docker.errors.APIError as reload_error:
                            _LOGGER.debug(
                                "Could not refresh Docker container '%s' before "
                                "ownership validation: %s",
                                getattr(container, "name", current_name),
                                reload_error,
                            )
                        container_name = str(
                            getattr(container, "name", "") or current_name
                        ).lstrip("/")
                        resource_key = str(
                            getattr(container, "id", "") or container_name
                        )
                        if resource_key in seen_resources:
                            continue
                        seen_resources.add(resource_key)

                        owned = (
                            container_name == current_name
                            and self._docker_resource_is_owned(
                                container,
                                addon_id,
                                "addon-container",
                                service_name=srv_name,
                            )
                        )
                        legacy_compatible = (
                            container_name == legacy_name
                            and self._legacy_container_is_compatible(
                                container,
                                addon_id,
                                srv_name,
                                service_config,
                                manifest,
                            )
                        )
                        if not owned and not legacy_compatible:
                            self._log_unowned_docker_resource(
                                addon_id,
                                srv_name,
                                "Docker container",
                                container_name,
                            )
                            continue
                        if legacy_compatible:
                            self._append_docker_log(
                                addon_id,
                                srv_name,
                                (
                                    f"Removing compatible legacy Docker container "
                                    f"'{container_name}' before creating its "
                                    "installation-scoped replacement."
                                ),
                            )

                        self._append_docker_log(
                            addon_id,
                            srv_name,
                            f"Stopping Docker container '{container_name}'.",
                        )
                        try:
                            container.stop(timeout=10)
                        except docker.errors.APIError as stop_error:
                            _LOGGER.debug(
                                "Could not stop Docker container '%s' before removal: %s",
                                container_name,
                                stop_error,
                            )

                        try:
                            container.remove(force=True)
                            if legacy_compatible:
                                legacy_container_removed = True
                            self._append_docker_log(
                                addon_id,
                                srv_name,
                                f"Removed Docker container '{container_name}'.",
                            )
                        except docker.errors.APIError as e:
                            status_code = getattr(
                                getattr(e, "response", None), "status_code", None
                            )
                            if status_code not in [404, 409]:
                                _LOGGER.warning(
                                    f"Failed to remove Docker container "
                                    f"'{container_name}': {e}"
                                )
                                self._append_docker_log(
                                    addon_id,
                                    srv_name,
                                    f"Failed to remove Docker container "
                                    f"'{container_name}': {e}",
                                )

                primary_service = service_names[0]
                for network_name, is_legacy in (
                    (self._get_network_name(addon_id), False),
                    (self._get_legacy_network_name(addon_id), True),
                ):
                    try:
                        network = self.docker_client.networks.get(network_name)
                        reload_network = getattr(network, "reload", None)
                        if callable(reload_network):
                            reload_network()
                        if is_legacy:
                            removable = (
                                legacy_container_removed
                                and self._legacy_network_is_compatible(
                                    network,
                                    addon_id,
                                )
                            )
                        else:
                            removable = self._docker_resource_is_owned(
                                network,
                                addon_id,
                                "addon-network",
                            )
                        if not removable:
                            self._log_unowned_docker_resource(
                                addon_id,
                                primary_service,
                                "Docker network",
                                network_name,
                            )
                            continue
                        network.remove()
                    except docker.errors.NotFound:
                        pass
                    except docker.errors.APIError as e:
                        status_code = getattr(
                            getattr(e, "response", None), "status_code", None
                        )
                        if status_code not in [403, 409]:
                            _LOGGER.warning(
                                f"Failed to remove Docker network '{network_name}': {e}"
                            )

            if (
                remove_image
                and self.docker_available
                and self.docker_client
                and manifest
            ):
                docker_config = manifest.get("docker") or {}
                services = self._get_docker_services(docker_config)
                removed_images = set()

                for srv_name, srv_config in services.items():
                    if srv_config.get("strategy") != "build":
                        continue

                    image_name = self._get_service_image_name(
                        addon_id,
                        srv_name,
                        srv_config,
                        manifest,
                    )
                    if image_name in removed_images:
                        continue

                    try:
                        image = self.docker_client.images.get(image_name)
                        if not self._docker_resource_is_owned(
                            image,
                            addon_id,
                            "addon-image",
                            service_name=srv_name,
                        ):
                            self._log_unowned_docker_resource(
                                addon_id,
                                srv_name,
                                "Docker image",
                                image_name,
                            )
                            removed_images.add(image_name)
                            continue
                        self._append_docker_log(
                            addon_id, srv_name, f"Removing Docker image '{image_name}'."
                        )
                        self.docker_client.images.remove(image_name, force=True)
                        self._append_docker_log(
                            addon_id, srv_name, f"Removed Docker image '{image_name}'."
                        )
                    except docker.errors.NotFound:
                        pass
                    except docker.errors.APIError as e:
                        _LOGGER.warning(
                            f"Failed to remove Docker image '{image_name}': {e}"
                        )
                        self._append_docker_log(
                            addon_id,
                            srv_name,
                            f"Failed to remove Docker image '{image_name}': {e}",
                        )

                    removed_images.add(image_name)

    def get_container_url(self, addon_id: str) -> str:
        """Boots the entire Docker Compose-lite stack for an addon and returns the primary gateway URL."""
        if not self._refresh_docker_status():
            raise RuntimeError(self.docker_status_message)

        addon = self.loaded_addons.get(addon_id)
        if not addon:
            raise ValueError(f"Addon '{addon_id}' is not active or loaded.")

        docker_config = addon.manifest.get("docker")
        if not docker_config:
            raise ValueError(f"Addon '{addon_id}' is not configured for Docker.")

        services = self._get_docker_services(docker_config)
        if not services:
            raise ValueError(
                f"Addon '{addon_id}' has an invalid Docker configuration: no services declared."
            )
        self._register_docker_log_sources(addon_id, services)

        ordered_services = self._get_ordered_services(services)
        primary_service_name = self._get_primary_service_name(services)
        primary_config = services[primary_service_name]

        try:
            internal_port = int(primary_config["internal_port"])
        except KeyError, TypeError, ValueError:
            raise ValueError(
                f"Primary service in '{addon_id}' has an invalid or missing 'internal_port'."
            )

        addon_lock = self._get_addon_container_lock(addon_id)

        def _collect_stack_logs(containers: dict) -> str:
            log_parts = []
            for srv_name, container in containers.items():
                status = "unknown"
                exit_code = None
                oom_killed = None
                try:
                    container.reload()
                    state = (
                        container.attrs.get("State", {})
                        if isinstance(container.attrs, dict)
                        else {}
                    )
                    status = (
                        state.get("Status")
                        or getattr(container, "status", None)
                        or "unknown"
                    )
                    exit_code = state.get("ExitCode")
                    oom_killed = state.get("OOMKilled")
                except docker.errors.APIError as reload_error:
                    _LOGGER.debug(
                        "Could not refresh Docker state for service '%s': %s",
                        srv_name,
                        reload_error,
                    )

                def _read_logs(target=container) -> str:
                    return target.logs(tail=1200).decode("utf-8", errors="replace")

                try:
                    service_logs = _read_logs()
                    if status == "exited" and len(service_logs) < 4096:
                        # Docker can report an exited status before the final stderr buffer is readable.
                        time.sleep(0.75)
                        refreshed_logs = _read_logs()
                        if len(refreshed_logs) > len(service_logs):
                            service_logs = refreshed_logs
                    service_logs = redact_text(service_logs, max_length=250_000)
                except docker.errors.APIError:
                    service_logs = "No container logs available."

                details = [f"status={status}"]
                if exit_code is not None:
                    details.append(f"exit_code={exit_code}")
                if oom_killed is not None:
                    details.append(f"oom_killed={oom_killed}")
                log_parts.append(
                    f"--- {srv_name} ({', '.join(details)}) ---\n{service_logs}"
                )
            return (
                "\n\n".join(log_parts) if log_parts else "No container logs available."
            )

        with addon_lock:
            self._touch_container_access(addon_id)
            with self._container_lock:
                addon_containers = self._active_containers.get(addon_id, {})
                if not isinstance(addon_containers, dict):
                    addon_containers = (
                        {"service": addon_containers} if addon_containers else {}
                    )
                self._active_containers[addon_id] = addon_containers

            force_image_refresh = self._is_docker_image_stale(addon_id)
            if force_image_refresh:
                _LOGGER.info(
                    f"Docker image for addon '{addon_id}' is stale; rebuilding on first use."
                )
                self._append_docker_log(
                    addon_id,
                    primary_service_name,
                    "Docker image is stale; rebuilding on first use.",
                )

            needs_boot = force_image_refresh
            for srv_name in ordered_services:
                if needs_boot:
                    break
                srv_config = services[srv_name]
                container = addon_containers.get(srv_name)
                if not container:
                    try:
                        container = self.docker_client.containers.get(
                            self._get_container_name(addon_id, srv_name)
                        )
                        addon_containers[srv_name] = container
                    except docker.errors.NotFound:
                        container = None

                if container is None:
                    needs_boot = True
                    break

                try:
                    container.reload()
                    if not self._docker_resource_is_owned(
                        container,
                        addon_id,
                        "addon-container",
                        service_name=srv_name,
                    ):
                        container_name = self._get_container_name(
                            addon_id,
                            srv_name,
                        )
                        self._log_unowned_docker_resource(
                            addon_id,
                            srv_name,
                            "Docker container",
                            container_name,
                        )
                        raise RuntimeError(
                            f"Docker container name collision for '{container_name}'. "
                            "Remove or rename the unrelated resource before starting "
                            "this add-on."
                        )
                    configured_image = self._get_service_image_name(
                        addon_id,
                        srv_name,
                        srv_config,
                        addon.manifest,
                    )
                    container_tags = (
                        container.image.tags
                        if container.image and container.image.tags
                        else []
                    )

                    if container.status not in ("running", "created", "restarting") or (
                        configured_image and configured_image not in container_tags
                    ):
                        needs_boot = True
                        break
                except RuntimeError:
                    raise
                except docker.errors.APIError:
                    needs_boot = True
                    break

            if needs_boot:
                self._append_docker_log(
                    addon_id, primary_service_name, "Booting Docker service stack."
                )
                self._stop_and_remove_container(
                    addon_id, remove_image=force_image_refresh, manifest=addon.manifest
                )
                self._ensure_docker_image_prepared(
                    addon_id, manifest=addon.manifest, force_refresh=force_image_refresh
                )

                network_name = self._ensure_addon_network(
                    addon_id,
                    primary_service_name,
                    addon.manifest,
                )

                addon_containers = {}
                with self._container_lock:
                    self._active_containers[addon_id] = addon_containers

                for srv_name in ordered_services:
                    srv_config = services[srv_name]
                    container_name = self._get_container_name(addon_id, srv_name)

                    ports = {}
                    if srv_name == primary_service_name:
                        ports = ContainerStorageBroker.dynamic_port_binding(
                            internal_port
                        )

                    gpu_requirement = self._get_service_gpu_requirement(
                        addon.manifest,
                        srv_name,
                        srv_config,
                    )

                    try:
                        _LOGGER.info(
                            f"Starting Docker service '{container_name}' for addon '{addon_id}'..."
                        )
                        self._append_docker_log(
                            addon_id,
                            srv_name,
                            f"Starting Docker service container '{container_name}'.",
                        )
                        run_kwargs = {
                            "name": container_name,
                            "detach": True,
                            "network": network_name,
                            "hostname": srv_name,
                            "labels": self._docker_resource_labels(
                                addon_id,
                                "addon-container",
                                service_name=srv_name,
                                manifest=addon.manifest,
                            ),
                            "ports": ports,
                            "volumes": self._resolve_docker_volumes(
                                addon_id, srv_config
                            ),
                            "environment": srv_config.get("environment") or {},
                            "command": srv_config.get("command"),
                        }

                        mem_limit = srv_config.get("mem_limit")
                        if mem_limit:
                            run_kwargs["mem_limit"] = mem_limit

                        shm_size = srv_config.get("shm_size")
                        if shm_size:
                            run_kwargs["shm_size"] = shm_size

                        image_name = self._get_service_image_name(
                            addon_id,
                            srv_name,
                            srv_config,
                            addon.manifest,
                        )
                        container = self._run_container_with_gpu_policy(
                            addon_id=addon_id,
                            service_name=srv_name,
                            image_name=image_name,
                            run_kwargs=run_kwargs,
                            gpu_requirement=gpu_requirement,
                        )
                        addon_containers[srv_name] = container
                        self._append_docker_log(
                            addon_id,
                            srv_name,
                            f"Started Docker service container '{container_name}'.",
                        )
                    except (docker.errors.APIError, RuntimeError) as e:
                        self._append_docker_log(
                            addon_id,
                            srv_name,
                            f"Failed to start Docker service container '{container_name}': {e}",
                        )
                        if isinstance(e, RuntimeError):
                            raise
                        raise RuntimeError(
                            f"Failed to start service '{srv_name}': {e}"
                        ) from e

            primary_container = addon_containers[primary_service_name]
            healthcheck_path = primary_config.get("healthcheck_path", "") or ""
            if healthcheck_path and not healthcheck_path.startswith("/"):
                healthcheck_path = f"/{healthcheck_path}"

            startup_timeout = self._get_effective_startup_timeout(
                docker_config, primary_config
            )

            start_time = time.time()
            while time.time() - start_time < startup_timeout:
                try:
                    base_url = self._get_container_base_url(
                        primary_container, internal_port
                    )
                    health_url = f"{base_url}{healthcheck_path}"
                    response = requests.get(health_url, timeout=2)
                    if response.status_code < 400:
                        self._touch_container_access(addon_id)
                        if force_image_refresh:
                            self._clear_docker_image_stale(addon_id)
                        _LOGGER.info("Stack '%s' is healthy at %s", addon_id, base_url)
                        self._append_docker_log(
                            addon_id,
                            primary_service_name,
                            f"Docker service stack is healthy at {base_url}.",
                        )
                        return base_url
                    if response.status_code == 503:
                        # Forward structured loading-phase messages (1B: descriptive health states).
                        try:
                            body = response.json()
                            detail = (
                                body.get("detail") if isinstance(body, dict) else None
                            )
                            if (
                                isinstance(detail, dict)
                                and detail.get("status") == "loading"
                            ):
                                msg = (
                                    detail.get("message")
                                    or f"Loading {detail.get('model', addon_id)}…"
                                )
                                self._append_docker_log(
                                    addon_id, primary_service_name, f"[startup] {msg}"
                                )
                        except (
                            requests.exceptions.JSONDecodeError,
                            TypeError,
                            ValueError,
                        ) as error:
                            _LOGGER.debug(
                                "Could not decode loading health response for '%s': %s",
                                addon_id,
                                error,
                            )
                except requests.RequestException:
                    pass
                except docker.errors.APIError, RuntimeError:
                    pass

                try:
                    for srv_name, container in addon_containers.items():
                        container.reload()
                        if container.status == "exited":
                            logs = _collect_stack_logs(addon_containers)
                            self._append_docker_log(
                                addon_id,
                                srv_name,
                                f"Docker service crashed on startup. Logs:\n{logs}",
                            )
                            self._stop_and_remove_container(addon_id)
                            raise RuntimeError(
                                f"Service '{srv_name}' crashed on startup. Logs:\n{logs}"
                            )
                except RuntimeError:
                    raise
                except docker.errors.APIError as inspect_error:
                    _LOGGER.debug(
                        "Could not inspect Docker startup state for '%s': %s",
                        addon_id,
                        inspect_error,
                    )

                time.sleep(2)

            logs = _collect_stack_logs(addon_containers)
            self._append_docker_log(
                addon_id,
                primary_service_name,
                f"Docker service stack timed out after {startup_timeout} seconds. Logs:\n{logs}",
            )
            self._stop_and_remove_container(addon_id)
            raise TimeoutError(
                f"Stack '{addon_id}' failed to become healthy after {startup_timeout} seconds. Logs:\n{logs}"
            )

    def reap_idle_containers(self):
        """Stops on-demand container stacks that have exceeded their idle timeout."""
        if not self._refresh_docker_status(log_failure=False):
            return

        current_time = time.time()
        to_probe = []
        to_reap = set()
        idle_timeouts = {}

        with self._container_lock:
            for addon_id, addon in list(self.loaded_addons.items()):
                if (
                    "docker" in addon.manifest
                    and addon_id not in self._active_containers
                ):
                    to_probe.append((addon_id, addon.manifest.get("docker") or {}))

        for addon_id, docker_config in to_probe:
            services = self._get_docker_services(docker_config)
            discovered_containers = {}
            all_services_present = True

            for srv_name in services:
                try:
                    container = self.docker_client.containers.get(
                        self._get_container_name(addon_id, srv_name)
                    )
                    container.reload()
                    if not self._docker_resource_is_owned(
                        container,
                        addon_id,
                        "addon-container",
                        service_name=srv_name,
                    ):
                        self._log_unowned_docker_resource(
                            addon_id,
                            srv_name,
                            "Docker container",
                            self._get_container_name(addon_id, srv_name),
                        )
                        all_services_present = False
                        break
                    if container.status in ("running", "created", "restarting"):
                        discovered_containers[srv_name] = container
                    else:
                        all_services_present = False
                        break
                except docker.errors.NotFound:
                    all_services_present = False
                    break
                except docker.errors.APIError as e:
                    _LOGGER.warning(
                        f"Failed to inspect Docker service '{srv_name}' for addon '{addon_id}': {e}"
                    )
                    all_services_present = False
                    break

            if all_services_present and discovered_containers:
                with self._container_lock:
                    self._active_containers[addon_id] = discovered_containers
            elif discovered_containers:
                to_reap.add(addon_id)

        with self._container_lock:
            tracked_addons = list(self._active_containers.keys())

        for addon_id in tracked_addons:
            addon = self.loaded_addons.get(addon_id)
            if not addon:
                to_reap.add(addon_id)
                continue

            docker_config = addon.manifest.get("docker") or {}
            if not docker_config:
                to_reap.add(addon_id)
                continue

            services = self._get_docker_services(docker_config)
            if not services:
                to_reap.add(addon_id)
                continue
            addon_containers = self._active_containers.get(addon_id, {})
            if not isinstance(addon_containers, dict):
                addon_containers = (
                    {"service": addon_containers} if addon_containers else {}
                )
                with self._container_lock:
                    self._active_containers[addon_id] = addon_containers

            is_healthy = True
            for srv_name in services:
                container = addon_containers.get(srv_name)
                if not container:
                    is_healthy = False
                    break

                try:
                    container.reload()
                    if not self._docker_resource_is_owned(
                        container,
                        addon_id,
                        "addon-container",
                        service_name=srv_name,
                    ) or container.status not in (
                        "running",
                        "created",
                        "restarting",
                    ):
                        is_healthy = False
                        break
                except docker.errors.APIError:
                    is_healthy = False
                    break

            if not is_healthy:
                to_reap.add(addon_id)
                continue

            if (
                self._get_effective_stack_lifecycle(docker_config, services)
                == "always_on"
            ):
                continue

            primary_service_name = self._get_primary_service_name(services)
            timeout = self._get_effective_idle_timeout(
                docker_config, services.get(primary_service_name, {})
            )

            last_accessed = self._get_container_last_accessed(addon_id)

            if (current_time - last_accessed) > timeout:
                to_reap.add(addon_id)
                idle_timeouts[addon_id] = timeout

        for addon_id in to_reap:
            try:
                with self._container_use_guard(addon_id, timeout=0, exclusive=True):
                    # A request can begin after the scan and finish before this
                    # lock is acquired. Its fresh timestamp must win that race.
                    timeout = idle_timeouts.get(addon_id)
                    if timeout is not None and (
                        time.time() - self._get_container_last_accessed(addon_id)
                    ) <= timeout:
                        continue
                    _LOGGER.info("Reaping idle container stack for addon '%s'.", addon_id)
                    self._append_docker_log(
                        addon_id, None, "Reaping idle Docker service stack."
                    )
                    self._stop_and_remove_container(addon_id)
            except (FileLockTimeout, OSError):
                # Busy or unavailable coordination never permits an unsafe stop.
                # A process crash releases the OS lock; there is no busy flag
                # that can survive its owner indefinitely.
                continue

    def start_always_on_containers(self) -> None:
        """Starts declared always-on services after the web app is available."""
        if is_worker_process():
            return
        for addon_id, addon_instance in self.loaded_addons.items():
            docker_config = addon_instance.manifest.get("docker") or {}
            if (
                not docker_config
                or self._get_effective_stack_lifecycle(docker_config) != "always_on"
            ):
                continue
            _LOGGER.info(
                "Starting deferred always-on Docker microservice for addon '%s'...",
                addon_id,
            )
            try:
                self.get_container_url(addon_id)
            except docker.errors.APIError, OSError, RuntimeError, ValueError:
                _LOGGER.exception(
                    "Failed to start always-on Docker microservice for addon "
                    "'%s'. It will retry on first use.",
                    addon_id,
                )
