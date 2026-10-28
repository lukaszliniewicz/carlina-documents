"""Container runtime selection and host-storage boundary helpers."""

from __future__ import annotations

import json
import logging
import platform
import shlex
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any, Protocol

try:
    import docker
except ImportError:  # pragma: no cover - exercised through no-SDK probes.
    docker = None

from ...product_identity import product_environment_value

logger = logging.getLogger(__name__)

CONTAINER_RUNTIME_NONE = "none"
CONTAINER_RUNTIME_DOCKER_SDK = "docker_sdk"
CONTAINER_RUNTIME_WSL_CLI = "wsl_cli"
LOOPBACK_HOST = "127.0.0.1"
DEFAULT_WSL_DOCKER_TIMEOUT_SECONDS = 8.0
DEFAULT_WSL_DOCKER_IMAGE_OP_TIMEOUT_SECONDS = 900.0
WSL_DOCKER_CONTAINER_TIMEOUT_BUFFER_SECONDS = 3.0
SELINUX_RELABEL_UNSUPPORTED_FILESYSTEMS = frozenset(
    {
        "9p",
        "cifs",
        "fuse.rclone",
        "fuse.sshfs",
        "nfs",
        "nfs4",
        "smb3",
    }
)


class ContainerRuntimeError(RuntimeError):
    """Runtime-neutral Docker transport failure."""


class ContainerObjectNotFound(ContainerRuntimeError):
    """A requested container, image, or network does not exist."""


def _api_error(message: str) -> Exception:
    if docker is not None:
        return docker.errors.APIError(message)
    return ContainerRuntimeError(message)


def _not_found_error(message: str) -> Exception:
    if docker is not None:
        return docker.errors.NotFound(message)
    return ContainerObjectNotFound(message)


def _short_identifier(
    identifier: str | None,
    *,
    keep_sha_prefix: bool = False,
) -> str:
    text = str(identifier or "").strip()
    if not text:
        return ""
    if text.startswith("sha256:"):
        digest = text.split(":", 1)[1]
        short_digest = digest[:12]
        return f"sha256:{short_digest}" if keep_sha_prefix else short_digest
    return text[:12]


class WslDockerImageRef:
    def __init__(
        self,
        tags: list[str] | None = None,
        image_id: str | None = None,
        attrs: dict | None = None,
    ) -> None:
        self.tags = list(tags or [])
        self.id = str(image_id or "")
        self.short_id = _short_identifier(self.id, keep_sha_prefix=True)
        self.attrs = attrs or {}


class WslDockerNetworkRef:
    def __init__(
        self,
        client: WslDockerClientAdapter,
        name: str,
        attrs: dict | None = None,
    ) -> None:
        self._client = client
        self.name = name
        self.attrs = attrs or {}

    def reload(self) -> None:
        self.attrs = self._client._inspect_network(self.name)

    def remove(self) -> None:
        self._client._run_docker(
            ["network", "rm", self.name],
            ignore_not_found=True,
        )


class WslDockerContainerRef:
    def __init__(self, client: WslDockerClientAdapter, identifier: str) -> None:
        self._client = client
        self._identifier = identifier
        self.id = identifier
        self.short_id = _short_identifier(identifier)
        self.name = identifier
        self.attrs: dict[str, Any] = {}
        self.status = "unknown"
        self.image = WslDockerImageRef([])
        self.ports: dict[str, Any] = {}
        self.reload()

    def reload(self) -> None:
        data = self._client._inspect_container(self._identifier)
        self.attrs = data
        self.id = data.get("Id", self._identifier)
        self.short_id = _short_identifier(self.id)
        self.name = (data.get("Name") or self._identifier).lstrip("/")
        self.status = ((data.get("State") or {}).get("Status")) or "unknown"
        configured_image = (data.get("Config") or {}).get("Image")
        image_id = data.get("Image") or ""
        self.image = WslDockerImageRef(
            [configured_image] if configured_image else [],
            image_id=image_id,
        )
        self.ports = (data.get("NetworkSettings") or {}).get("Ports") or {}

    def start(self) -> None:
        self._client._run_docker(
            ["start", self._identifier],
            ignore_not_found=True,
        )
        self.reload()

    def restart(self, timeout: int = 10) -> None:
        effective_timeout = self._client._resolve_container_control_timeout(timeout)
        self._client._run_docker(
            ["restart", "-t", str(timeout), self._identifier],
            ignore_not_found=True,
            timeout_seconds=effective_timeout,
        )
        self.reload()

    def stop(self, timeout: int = 10) -> None:
        effective_timeout = self._client._resolve_container_control_timeout(timeout)
        self._client._run_docker(
            ["stop", "-t", str(timeout), self._identifier],
            ignore_not_found=True,
            ignore_not_running=True,
            timeout_seconds=effective_timeout,
        )
        self.reload()

    def remove(self, force: bool = False) -> None:
        cmd = ["rm"]
        if force:
            cmd.append("-f")
        cmd.append(self._identifier)
        effective_timeout = self._client._resolve_container_control_timeout(
            10 if force else 2
        )
        self._client._run_docker(
            cmd,
            ignore_not_found=True,
            timeout_seconds=effective_timeout,
        )

    def logs(self, tail: int = 600) -> bytes:
        output = self._client._run_docker(
            ["logs", "--tail", str(tail), self._identifier],
            ignore_not_found=True,
        )
        return output.encode("utf-8", errors="replace")

    def stats(self, stream: bool = False) -> dict:
        del stream
        return {}


class WslDockerImagesManager:
    def __init__(self, client: WslDockerClientAdapter) -> None:
        self._client = client

    def get(self, image_name: str) -> WslDockerImageRef:
        data = self._client._inspect_image(image_name)
        return WslDockerImageRef(
            tags=data.get("RepoTags") or [],
            image_id=data.get("Id"),
            attrs=data,
        )

    def list(self) -> list[WslDockerImageRef]:
        output = self._client._run_docker(
            ["image", "ls", "--no-trunc", "--format", "{{json .}}"]
        ).strip()
        if not output:
            return []

        images = []
        for raw_line in output.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            image_id = str(row.get("ID") or "").strip()
            if not image_id:
                continue
            try:
                inspect_data = self._client._inspect_image(image_id)
            except Exception:  # noqa: BLE001 - one malformed Docker row.
                inspect_data = {}
            tags = inspect_data.get("RepoTags") if inspect_data else None
            if not tags:
                repo = str(row.get("Repository") or "<none>")
                tag = str(row.get("Tag") or "<none>")
                tags = [f"{repo}:{tag}"]
            images.append(
                WslDockerImageRef(
                    tags=tags,
                    image_id=inspect_data.get("Id") or image_id,
                    attrs=inspect_data,
                )
            )
        return images

    def pull(self, image_name: str) -> None:
        self._client._run_docker(
            ["pull", image_name],
            timeout_seconds=self._client.image_op_timeout_seconds,
        )

    def build(
        self,
        path: str,
        tag: str,
        rm: bool = True,
        forcerm: bool = False,
        labels: dict[str, str] | None = None,
        buildargs: dict[str, str] | None = None,
    ) -> tuple[WslDockerImageRef, list]:
        cmd = ["build", "-t", tag]
        if rm:
            cmd.append("--rm")
        if forcerm:
            cmd.append("--force-rm")
        for key, value in (labels or {}).items():
            cmd.extend(["--label", f"{key}={value}"])
        for key, value in (buildargs or {}).items():
            cmd.extend(["--build-arg", f"{key}={value}"])
        cmd.append(path)
        self._client._run_docker(
            cmd,
            timeout_seconds=self._client.image_op_timeout_seconds,
        )
        return WslDockerImageRef(
            tags=[tag],
            attrs={"Config": {"Labels": dict(labels or {})}},
        ), []

    def remove(self, image_name: str, force: bool = False) -> None:
        cmd = ["image", "rm"]
        if force:
            cmd.append("-f")
        cmd.append(image_name)
        self._client._run_docker(cmd, ignore_not_found=True)


class WslDockerContainersManager:
    def __init__(self, client: WslDockerClientAdapter) -> None:
        self._client = client

    def get(self, container_name: str) -> WslDockerContainerRef:
        return WslDockerContainerRef(self._client, container_name)

    def list(self, all: bool = False) -> list[WslDockerContainerRef]:
        cmd = ["ps"]
        if all:
            cmd.append("-a")
        cmd.extend(["--format", "{{json .}}"])
        output = self._client._run_docker(cmd).strip()
        if not output:
            return []
        containers = []
        for raw_line in output.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            identifier = str(row.get("Names") or row.get("ID") or "").strip()
            if not identifier:
                continue
            try:
                containers.append(WslDockerContainerRef(self._client, identifier))
            except Exception:
                logger.debug(
                    "Ignoring stale Docker container row for %s.",
                    identifier,
                    exc_info=True,
                )
                continue
        return containers

    def run(self, image_name: str, **kwargs: Any) -> WslDockerContainerRef:
        cmd = ["run"]
        if kwargs.get("detach", False):
            cmd.append("-d")
        for option, flag in (
            ("name", "--name"),
            ("network", "--network"),
            ("hostname", "--hostname"),
            ("mem_limit", "--memory"),
            ("shm_size", "--shm-size"),
        ):
            value = kwargs.get(option)
            if value:
                cmd.extend([flag, str(value)])
        for key, value in (kwargs.get("labels") or {}).items():
            cmd.extend(["--label", f"{key}={value}"])
        if kwargs.get("device_requests"):
            cmd.extend(["--gpus", "all"])
        for container_port, host_binding in (kwargs.get("ports") or {}).items():
            port = str(container_port)
            if host_binding is None:
                cmd.extend(["-p", port])
            elif isinstance(host_binding, (list, tuple)) and len(host_binding) == 2:
                host_ip, host_port = host_binding
                published = str(host_ip)
                published = (
                    f"{published}:{host_port}"
                    if host_port is not None
                    else f"{published}:"
                )
                cmd.extend(["-p", f"{published}:{port}"])
            else:
                cmd.extend(["-p", f"{host_binding}:{port}"])
        for host_path, volume_config in (kwargs.get("volumes") or {}).items():
            if isinstance(volume_config, dict):
                bind_path = volume_config.get("bind")
                mode = volume_config.get("mode", "rw")
                cmd.extend(["-v", f"{host_path}:{bind_path}:{mode}"])
            else:
                cmd.extend(["-v", f"{host_path}:{volume_config}"])
        for env_key, env_value in (kwargs.get("environment") or {}).items():
            cmd.extend(["-e", f"{env_key}={env_value}"])
        command = kwargs.get("command")
        cmd.append(str(image_name))
        if command:
            if isinstance(command, (list, tuple)):
                cmd.extend(str(part) for part in command)
            else:
                cmd.append(str(command))
        output = self._client._run_docker(
            cmd,
            timeout_seconds=kwargs.get("timeout_seconds") or 60.0,
        ).strip()
        container_ref = (
            output.splitlines()[-1].strip()
            if output
            else str(kwargs.get("name") or image_name)
        )
        return WslDockerContainerRef(self._client, container_ref)


class WslDockerNetworksManager:
    def __init__(self, client: WslDockerClientAdapter) -> None:
        self._client = client

    def get(self, network_name: str) -> WslDockerNetworkRef:
        attrs = self._client._inspect_network(network_name)
        return WslDockerNetworkRef(self._client, network_name, attrs=attrs)

    def create(
        self,
        network_name: str,
        driver: str = "bridge",
        labels: dict[str, str] | None = None,
    ) -> WslDockerNetworkRef:
        cmd = ["network", "create"]
        if driver:
            cmd.extend(["--driver", str(driver)])
        for key, value in (labels or {}).items():
            cmd.extend(["--label", f"{key}={value}"])
        cmd.append(network_name)
        self._client._run_docker(cmd)
        return WslDockerNetworkRef(
            self._client,
            network_name,
            attrs={"Labels": dict(labels or {}), "Containers": {}},
        )


class WslDockerClientAdapter:
    """Docker SDK-shaped adapter over ``wsl.exe -- docker``."""

    def __init__(self, wsl_exe: str, distro: str | None = None) -> None:
        self._wsl_exe = wsl_exe
        self._distro = distro
        self._timeout_seconds = self._resolve_timeout_seconds()
        self._image_op_timeout_seconds = self._resolve_image_op_timeout_seconds()
        self.images = WslDockerImagesManager(self)
        self.containers = WslDockerContainersManager(self)
        self.networks = WslDockerNetworksManager(self)

    @property
    def image_op_timeout_seconds(self) -> float:
        return self._image_op_timeout_seconds

    def _base_cmd(self) -> list[str]:
        cmd = [self._wsl_exe]
        if self._distro:
            cmd.extend(["-d", self._distro])
        return [*cmd, "--", "docker"]

    @staticmethod
    def _is_not_found_message(message: str) -> bool:
        lower = (message or "").lower()
        objects = ("container", "image", "network", "object")
        return (
            "not found" in lower
            and (
                "error response from daemon" in lower
                or any(item in lower for item in objects)
            )
        ) or any(f"no such {item}" in lower for item in objects)

    @staticmethod
    def _is_not_running_message(message: str) -> bool:
        lower = (message or "").lower()
        return "container" in lower and (
            "not running" in lower or "already stopped" in lower
        )

    def _resolve_container_control_timeout(
        self,
        grace_timeout_seconds: float | None = None,
    ) -> float:
        try:
            grace_timeout = float(grace_timeout_seconds)
        except TypeError, ValueError:
            grace_timeout = 10.0
        return max(
            self._timeout_seconds,
            max(0.0, grace_timeout) + WSL_DOCKER_CONTAINER_TIMEOUT_BUFFER_SECONDS,
        )

    def _run_docker(
        self,
        args: list[str],
        *,
        ignore_not_found: bool = False,
        ignore_not_running: bool = False,
        timeout_seconds: float | None = None,
    ) -> str:
        cmd = [*self._base_cmd(), *args]
        effective_timeout = max(1.0, float(timeout_seconds or self._timeout_seconds))
        try:
            process = subprocess.run(
                cmd,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=effective_timeout,
            )
        except subprocess.TimeoutExpired as exc:
            timeout_hint: int | float = effective_timeout
            if effective_timeout.is_integer():
                timeout_hint = int(effective_timeout)
            raise _api_error(
                f"Docker command timed out after {timeout_hint}s via WSL while "
                f"running `{shlex.join(cmd)}`. WSL or Docker may be unavailable."
            ) from exc
        if process.returncode != 0:
            message = (process.stderr or process.stdout or "").strip()
            if self._is_not_found_message(message):
                if ignore_not_found:
                    return ""
                raise _not_found_error(message)
            if ignore_not_running and self._is_not_running_message(message):
                return ""
            raise _api_error(
                message or f"Docker command failed with exit code {process.returncode}."
            )
        return (process.stdout or process.stderr or "").strip()

    @staticmethod
    def _resolve_timeout_seconds() -> float:
        raw = str(
            product_environment_value("DOCKER_WSL_TIMEOUT_SECONDS", default="")
        ).strip()
        if not raw:
            return DEFAULT_WSL_DOCKER_TIMEOUT_SECONDS
        try:
            return max(1.0, float(raw))
        except ValueError:
            return DEFAULT_WSL_DOCKER_TIMEOUT_SECONDS

    @staticmethod
    def _resolve_image_op_timeout_seconds() -> float:
        raw = (
            str(
                product_environment_value(
                    "DOCKER_WSL_IMAGE_OP_TIMEOUT_SECONDS",
                    default="",
                )
            ).strip()
            or str(
                product_environment_value(
                    "DOCKER_WSL_BUILD_TIMEOUT_SECONDS",
                    default="",
                )
            ).strip()
        )
        if not raw:
            return DEFAULT_WSL_DOCKER_IMAGE_OP_TIMEOUT_SECONDS
        try:
            return max(1.0, float(raw))
        except ValueError:
            return DEFAULT_WSL_DOCKER_IMAGE_OP_TIMEOUT_SECONDS

    def _inspect_container(self, identifier: str) -> dict:
        data = json.loads(
            self._run_docker(["inspect", "--type", "container", identifier])
        )
        if not data:
            raise _not_found_error(f"Container {identifier!r} not found.")
        return data[0]

    def _inspect_image(self, image_name: str) -> dict:
        data = json.loads(self._run_docker(["image", "inspect", image_name]))
        if not data:
            raise _not_found_error(f"Image {image_name!r} not found.")
        return data[0]

    def _inspect_network(self, network_name: str) -> dict:
        data = json.loads(self._run_docker(["network", "inspect", network_name]))
        if not data:
            raise _not_found_error(f"Network {network_name!r} not found.")
        return data[0]

    def ping(self) -> None:
        self._run_docker(["version", "--format", "{{.Server.Version}}"])


def create_wsl_docker_client() -> tuple[WslDockerClientAdapter, str | None]:
    """Create the WSL transport selected by Carlina configuration."""

    wsl_exe = shutil.which("wsl.exe")
    if not wsl_exe:
        raise ContainerRuntimeError("wsl.exe is not available on PATH.")
    distro = str(product_environment_value("DOCKER_WSL_DISTRO", default="")).strip()
    selected_distro = distro or None
    return WslDockerClientAdapter(wsl_exe, selected_distro), selected_distro


class ContainerClient(Protocol):
    def ping(self) -> Any: ...


@dataclass(frozen=True, slots=True)
class ContainerRuntimeSelection:
    available: bool
    kind: str
    client: ContainerClient | None
    detail: str
    wsl_distro: str | None = None
    failures: tuple[str, ...] = ()


class DockerSdkRuntimeBackend:
    """Probe a Docker-compatible daemon through the Python Docker SDK."""

    kind = CONTAINER_RUNTIME_DOCKER_SDK

    def __init__(self, docker_module: Any, existing_client: Any = None):
        self.docker_module = docker_module
        self.existing_client = existing_client

    def probe(self) -> ContainerRuntimeSelection:
        if self.docker_module is None:
            detail = "Docker Python SDK is not installed."
            return ContainerRuntimeSelection(
                available=False,
                kind=self.kind,
                client=None,
                detail=detail,
                failures=(detail,),
            )
        try:
            client = self.existing_client or self.docker_module.from_env()
            client.ping()
        except Exception as exc:  # noqa: BLE001 - external runtime probe boundary.
            detail = str(exc).strip() or exc.__class__.__name__
            return ContainerRuntimeSelection(
                available=False,
                kind=self.kind,
                client=None,
                detail=detail,
                failures=(detail,),
            )
        return ContainerRuntimeSelection(
            available=True,
            kind=self.kind,
            client=client,
            detail="Docker Engine is available through the Docker SDK.",
        )


class WslCliRuntimeBackend:
    """Probe a Docker-compatible daemon through ``wsl.exe -- docker``."""

    kind = CONTAINER_RUNTIME_WSL_CLI

    def __init__(
        self,
        client_factory: Callable[[], tuple[ContainerClient, str | None]] | None = None,
        *,
        existing_client: ContainerClient | None = None,
        existing_distro: str | None = None,
    ):
        self.client_factory = client_factory or create_wsl_docker_client
        self.existing_client = existing_client
        self.existing_distro = existing_distro

    def probe(self) -> ContainerRuntimeSelection:
        try:
            if self.existing_client is None:
                client, distro = self.client_factory()
            else:
                client = self.existing_client
                distro = self.existing_distro
            client.ping()
        except Exception as exc:  # noqa: BLE001 - external runtime probe boundary.
            detail = str(exc).strip() or exc.__class__.__name__
            return ContainerRuntimeSelection(
                available=False,
                kind=self.kind,
                client=None,
                detail=detail,
                wsl_distro=self.existing_distro,
                failures=(detail,),
            )
        suffix = f" ({distro})" if distro else ""
        return ContainerRuntimeSelection(
            available=True,
            kind=self.kind,
            client=client,
            detail=f"Docker Engine is available through WSL{suffix}.",
            wsl_distro=distro,
        )


def select_container_runtime(
    *,
    docker_module: Any,
    existing_client: ContainerClient | None,
    existing_kind: str,
    existing_wsl_distro: str | None,
    platform_name: str,
    wsl_client_factory: (
        Callable[[], tuple[ContainerClient, str | None]] | None
    ) = None,
) -> ContainerRuntimeSelection:
    """Select one backend without leaking platform branches into add-on logic."""

    if existing_client is not None and existing_kind == CONTAINER_RUNTIME_WSL_CLI:
        wsl_probe = WslCliRuntimeBackend(
            wsl_client_factory,
            existing_client=existing_client,
            existing_distro=existing_wsl_distro,
        ).probe()
        if wsl_probe.available:
            return wsl_probe

    sdk_probe = DockerSdkRuntimeBackend(
        docker_module,
        existing_client=(
            existing_client
            if existing_kind in {CONTAINER_RUNTIME_DOCKER_SDK, "sdk"}
            else None
        ),
    ).probe()
    if sdk_probe.available:
        return sdk_probe

    failures = list(sdk_probe.failures)
    if platform_name == "Windows":
        wsl_probe = WslCliRuntimeBackend(wsl_client_factory).probe()
        if wsl_probe.available:
            return wsl_probe
        failures.extend(f"WSL fallback failed: {item}" for item in wsl_probe.failures)

    detail = " | ".join(failures) or "No compatible container runtime was detected."
    return ContainerRuntimeSelection(
        available=False,
        kind=CONTAINER_RUNTIME_NONE,
        client=None,
        detail=f"Docker Engine is not available: {detail}",
        failures=tuple(failures),
    )


def windows_path_to_wsl(path: str | Path) -> str:
    """Translate a local Windows path for a Linux daemon reached through WSL."""

    raw_path = str(path)
    windows_path = PureWindowsPath(raw_path)
    if windows_path.drive.startswith("\\\\"):
        raise ValueError(
            "UNC/network paths cannot be mounted directly by the WSL Docker "
            f"backend: {raw_path}. Configure local staging instead."
        )
    drive = windows_path.drive.rstrip(":").lower()
    if not drive:
        return raw_path.replace("\\", "/")
    suffix = "/".join(windows_path.parts[1:])
    return f"/mnt/{drive}/{suffix}" if suffix else f"/mnt/{drive}"


class ContainerStorageBroker:
    """Centralize safe local paths and backend-specific path translation."""

    def __init__(self, runtime_kind: str):
        self.runtime_kind = runtime_kind

    @staticmethod
    def resolve_within(root: str | Path, relative_path: str | Path) -> Path:
        resolved_root = Path(root).resolve()
        resolved = (resolved_root / relative_path).resolve()
        if not resolved.is_relative_to(resolved_root):
            raise PermissionError(
                f"Container path {relative_path!s} escapes {resolved_root}."
            )
        return resolved

    def host_path(self, path: str | Path) -> str:
        if self.runtime_kind == CONTAINER_RUNTIME_WSL_CLI:
            return windows_path_to_wsl(path)
        return str(path)

    @staticmethod
    def _decode_mountinfo_path(value: str) -> str:
        decoded = str(value)
        for escaped, replacement in (
            (r"\040", " "),
            (r"\011", "\t"),
            (r"\012", "\n"),
            (r"\134", "\\"),
        ):
            decoded = decoded.replace(escaped, replacement)
        return decoded

    @classmethod
    def filesystem_type(
        cls,
        path: str | Path,
        *,
        mountinfo_text: str | None = None,
    ) -> str | None:
        """Return the longest matching Linux mount's filesystem type."""

        try:
            text = (
                mountinfo_text
                if mountinfo_text is not None
                else Path("/proc/self/mountinfo").read_text(encoding="utf-8")
            )
        except OSError:
            return None

        target = Path(path).resolve()
        best_match: tuple[int, str] | None = None
        for raw_line in text.splitlines():
            fields = raw_line.split()
            try:
                separator = fields.index("-")
                mount_path = Path(cls._decode_mountinfo_path(fields[4])).resolve()
                filesystem = fields[separator + 1].strip().lower()
            except IndexError, ValueError, OSError:
                continue
            if target != mount_path and not target.is_relative_to(mount_path):
                continue
            match = (len(mount_path.parts), filesystem)
            if best_match is None or match[0] > best_match[0]:
                best_match = match
        return best_match[1] if best_match is not None else None

    @classmethod
    def ensure_bind_mount_supported(
        cls,
        path: str | Path,
        *,
        mode: str,
        platform_name: str | None = None,
        mountinfo_text: str | None = None,
    ) -> str | None:
        """Reject known filesystems that cannot satisfy SELinux relabeling."""

        if (platform_name or platform.system()) != "Linux":
            return None
        modifiers = {item.strip() for item in str(mode).split(",")}
        if not modifiers.intersection({"z", "Z"}):
            return cls.filesystem_type(path, mountinfo_text=mountinfo_text)

        filesystem = cls.filesystem_type(path, mountinfo_text=mountinfo_text)
        if filesystem not in SELINUX_RELABEL_UNSUPPORTED_FILESYSTEMS:
            return filesystem
        raise RuntimeError(
            f"Container bind source '{Path(path)}' is on {filesystem}, which cannot "
            "be safely relabeled for an enforcing SELinux host. Move Carlina's "
            "add-on data root to a local label-capable filesystem such as btrfs, "
            "ext4, or xfs before starting this add-on."
        )

    @staticmethod
    def bind_mount_mode(
        *,
        read_only: bool = False,
        shared: bool = False,
        platform_name: str | None = None,
        selinux_enforcing: bool | None = None,
    ) -> str:
        """Return a Docker bind mode that works with enforcing SELinux hosts."""

        base_mode = "ro" if read_only else "rw"
        active_platform = platform_name or platform.system()
        if active_platform != "Linux":
            return base_mode

        if selinux_enforcing is None:
            configured = (
                str(
                    product_environment_value(
                        "DOCKER_SELINUX_RELABEL",
                        default="auto",
                    )
                )
                .strip()
                .lower()
            )
            if configured in {"0", "false", "no", "off", "disabled"}:
                selinux_enforcing = False
            elif configured in {"1", "true", "yes", "on", "enabled"}:
                selinux_enforcing = True
            else:
                try:
                    selinux_enforcing = (
                        Path("/sys/fs/selinux/enforce")
                        .read_text(encoding="utf-8")
                        .strip()
                        == "1"
                    )
                except OSError:
                    selinux_enforcing = False

        if not selinux_enforcing:
            return base_mode
        return f"{base_mode},{'z' if shared else 'Z'}"

    @staticmethod
    def dynamic_port_binding(
        internal_port: int,
        *,
        host: str = LOOPBACK_HOST,
    ) -> dict[str, tuple[str, None]]:
        """Publish a random host port on loopback, never all interfaces."""

        port = int(internal_port)
        if not 1 <= port <= 65535:
            raise ValueError("internal_port must be between 1 and 65535.")
        normalized_host = str(host or "").strip()
        if normalized_host not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError("Container ports may only bind to a loopback host.")
        return {f"{port}/tcp": (normalized_host, None)}


__all__ = [
    "CONTAINER_RUNTIME_DOCKER_SDK",
    "CONTAINER_RUNTIME_NONE",
    "CONTAINER_RUNTIME_WSL_CLI",
    "DEFAULT_WSL_DOCKER_IMAGE_OP_TIMEOUT_SECONDS",
    "DEFAULT_WSL_DOCKER_TIMEOUT_SECONDS",
    "LOOPBACK_HOST",
    "SELINUX_RELABEL_UNSUPPORTED_FILESYSTEMS",
    "ContainerObjectNotFound",
    "ContainerRuntimeError",
    "ContainerRuntimeSelection",
    "ContainerStorageBroker",
    "DockerSdkRuntimeBackend",
    "WslCliRuntimeBackend",
    "WslDockerClientAdapter",
    "WslDockerContainerRef",
    "create_wsl_docker_client",
    "select_container_runtime",
    "windows_path_to_wsl",
]
