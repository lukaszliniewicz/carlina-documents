"""Immutable add-on dependency environment orchestration.

This mixin owns validation and materialization of locked pip and Pixi
environments. The host manager supplies package discovery, DataRoot paths,
managed-process backends, and vendor-path registration.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import shlex
import shutil
import subprocess
import sys
import sysconfig
import tempfile
from dataclasses import asdict
from pathlib import Path, PureWindowsPath
from typing import Any, Protocol

from carlina.core.utc import utc_iso

from ..manifest_schema import validate_manifest_against_schema
from .contracts import (
    AddonSourceKind,
    DependencyEnvironment,
    DependencyLock,
    PythonDependencyEnvironment,
    ResolvedAddonContract,
)
from .environments import (
    AddonEnvironmentPaths,
    python_environment_fingerprint,
)
from .imports import prepare_vendor_import_index, vendor_finder
from .managed_process_orchestration import (
    DEPENDENCY_ENV_IN_PROCESS_VENDOR,
    DEPENDENCY_ENV_MANAGED_PIXI,
)
from .managed_process_orchestration import (
    LEGACY_DEPENDENCY_ENVIRONMENTS as _LEGACY_DEPENDENCY_ENVIRONMENTS,
)
from .managed_process_runtime import PixiManagedProcessRuntimeBackend

logger = logging.getLogger(__name__)


_PATCHRIGHT_CHROMIUM_INSTALL_ARGV = (
    "python",
    "-m",
    "patchright",
    "install",
    "chromium",
)
_PATCHRIGHT_CHROMIUM_POSTCONDITION = "patchright-chromium-headless-launch-v1"
_PATCHRIGHT_CHROMIUM_POSTCONDITION_ARGV = (
    "python",
    "-c",
    (
        "from pathlib import Path\n"
        "from patchright.sync_api import sync_playwright\n"
        "with sync_playwright() as playwright:\n"
        "    executable = Path(playwright.chromium.executable_path)\n"
        "    if not executable.is_file():\n"
        "        raise RuntimeError("
        "f'Patchright Chromium executable is missing: {executable}'"
        ")\n"
        "    browser = playwright.chromium.launch(headless=True)\n"
        "    browser.close()"
    ),
)
_PATCHRIGHT_CHROMIUM_POSTCONDITION_TIMEOUT_SECONDS = 90


class DependencyOrchestrationHost(Protocol):
    """Manager surface required by dependency orchestration."""

    app: Any
    _addon_packages: dict[str, Any]

    def get_addon_directory(self, addon_id: str) -> Path: ...

    def _addon_source_kind(self, addon_id: str) -> AddonSourceKind: ...

    def _addon_environment_paths(
        self,
        addon_id: str,
        python_environment: PythonDependencyEnvironment | None = None,
    ) -> AddonEnvironmentPaths: ...

    def _python_environment_for_runtime(
        self,
        addon_id: str,
        runtime_unit: str | None = None,
        *,
        environment: DependencyEnvironment | None = None,
    ) -> PythonDependencyEnvironment | None: ...

    def _get_pixi_runtime_backend(self) -> PixiManagedProcessRuntimeBackend: ...

    def _prepare_addon_pixi_project(
        self,
        addon_id: str,
        *,
        runtime_unit: str | None = None,
        python_environment: PythonDependencyEnvironment | None = None,
        python_version: str | None = None,
        conda_packages: list[str] | tuple[str, ...] | None = None,
        pip_packages: list[str] | tuple[str, ...] | None = None,
    ) -> Path: ...

    def setup_pixi_environment(
        self,
        addon_id: str,
        conda_packages: list[str] | None = None,
        pip_packages: list[str] | None = None,
        python_version: str = "3.11",
        *,
        runtime_unit: str | None = None,
    ) -> str: ...

    def _refresh_docker_status(
        self,
        log_failure: bool = False,
        *,
        force: bool = False,
    ) -> bool: ...

    def _ensure_docker_image_prepared(
        self,
        addon_id: str,
        manifest: dict[str, Any] | None = None,
        force_refresh: bool = False,
    ) -> None: ...

    def resolve_container_service_image_name(
        self,
        addon_id: str,
        service_name: str,
        service_config: dict[str, Any],
        *,
        manifest: dict[str, Any] | None = None,
    ) -> str: ...


class DependencyOrchestrationMixin:
    """Materialize immutable per-add-on dependency environments."""

    def _ensure_addon_python_path(
        self,
        addon_id: str,
        python_environment: PythonDependencyEnvironment | None = None,
    ) -> None:
        """Register non-shadowing dependencies from the rebuildable data root."""
        package = (getattr(self, "_addon_packages", None) or {}).get(addon_id)
        contract = getattr(package, "contract", None)
        selected_environment = python_environment
        if selected_environment is None:
            selected_environment = self._python_environment_for_runtime(
                addon_id,
                environment=DependencyEnvironment.IN_PROCESS_VENDOR,
            )
        dependency_lock = (
            getattr(selected_environment, "lock", None)
            if selected_environment is not None
            else getattr(contract, "dependency_lock", None)
        )
        if (
            self._addon_source_kind(addon_id) == AddonSourceKind.INSTALLED
            and dependency_lock is None
        ):
            return
        app = getattr(self, "app", None)
        lib_dir = (
            self._addon_environment_paths(addon_id, selected_environment).lib_dir
            if app is not None
            else self.get_addon_directory(addon_id) / "lib"
        )
        if (
            not lib_dir.is_dir()
            and self._addon_source_kind(addon_id) == AddonSourceKind.BUILT_IN
            and int(getattr(contract, "manifest_schema_version", 1) or 1) < 2
        ):
            # Temporary read-only bridge for legacy pre-DataRoot source
            # checkouts. Schema 2 environments are lock-governed: silently
            # falling back to an old source-tree ``lib`` can mix dependency
            # generations and defeats the conflict checks below.
            legacy_lib = self.get_addon_directory(addon_id) / "lib"
            if legacy_lib.is_dir():
                lib_dir = legacy_lib
        if not lib_dir.exists():
            return

        finder = getattr(self, "_vendor_finder", None) or vendor_finder()
        self._vendor_finder = finder
        finder.register(addon_id, lib_dir)

    def prepare_addon_import_indexes(self, addon_id: str) -> int:
        """Prime immutable in-process import metadata during maintenance."""

        package = (getattr(self, "_addon_packages", None) or {}).get(addon_id)
        contract = getattr(package, "contract", None)
        if contract is None:
            return 0
        environments = tuple(getattr(contract, "python_environments", ()) or ())
        selected_environments: tuple[PythonDependencyEnvironment | None, ...]
        if environments:
            selected_environments = tuple(
                environment
                for environment in environments
                if getattr(environment, "environment", None)
                == DependencyEnvironment.IN_PROCESS_VENDOR
                and getattr(environment, "lock", None) is not None
            )
        elif (
            getattr(contract, "dependency_lock", None) is not None
            and getattr(contract, "dependency_environment", None)
            != DependencyEnvironment.MANAGED_PROCESS_PIXI
        ):
            selected_environments = (None,)
        else:
            selected_environments = ()

        prepared = 0
        for python_environment in selected_environments:
            environment_paths = self._addon_environment_paths(
                addon_id,
                python_environment,
            )
            if not environment_paths.lib_dir.is_dir():
                continue
            try:
                if prepare_vendor_import_index(environment_paths.lib_dir):
                    prepared += 1
            except (OSError, UnicodeError, ValueError) as exc:
                logger.warning(
                    "Could not prepare vendor import index for add-on '%s': %s",
                    addon_id,
                    exc,
                )
        return prepared

    @staticmethod
    def _normalize_post_install_commands(commands: object) -> list[list[str]]:
        if not commands:
            return []
        if isinstance(commands, (str, bytes)):
            commands = [commands]

        normalized = []
        for command in commands:
            if isinstance(command, (str, bytes)):
                parts = shlex.split(
                    command.decode() if isinstance(command, bytes) else command
                )
            elif isinstance(command, (list, tuple)):
                parts = [str(part) for part in command if str(part)]
            else:
                raise TypeError(
                    f"Invalid post-install command declaration: {command!r}"
                )
            if parts:
                normalized.append(parts)
        return normalized

    @staticmethod
    def _command_uses_python(command: list[str]) -> bool:
        if not command:
            return False
        token = str(command[0])
        names = {"py", "py.exe", "python", "python.exe", "python3", "python3.exe"}
        path_name = Path(token).name.lower()
        win_name = PureWindowsPath(token).name.lower()
        return (
            path_name in names
            or win_name in names
            or path_name.startswith("python")
            or win_name.startswith("python")
        )

    @staticmethod
    def _command_uses_pip(command: list[str]) -> bool:
        if not command:
            return False
        token = str(command[0])
        names = {"pip", "pip.exe", "pip3", "pip3.exe"}
        path_name = Path(token).name.lower()
        win_name = PureWindowsPath(token).name.lower()
        return (
            path_name in names
            or win_name in names
            or path_name.startswith("pip")
            or win_name.startswith("pip")
        )

    def _install_contract_python_environments(
        self,
        addon_id: str,
        contract: ResolvedAddonContract,
    ) -> None:
        """Install every immutable environment from a compiled contract."""

        environments = tuple(getattr(contract, "python_environments", ()) or ())
        if not environments:
            dependency_lock = getattr(contract, "dependency_lock", None)
            if dependency_lock is None:
                return
            if (
                getattr(contract, "dependency_environment", None)
                == DependencyEnvironment.MANAGED_PROCESS_PIXI
            ):
                self._install_locked_pixi_environment(addon_id, contract)
            else:
                self._install_locked_dependency_environment(addon_id, contract)
            self._install_declared_dependency_setup(addon_id, contract)
            return

        for python_environment in environments:
            dependency_lock = getattr(python_environment, "lock", None)
            if dependency_lock is not None:
                if (
                    python_environment.environment
                    == DependencyEnvironment.MANAGED_PROCESS_PIXI
                ):
                    self._install_locked_pixi_environment(
                        addon_id,
                        contract,
                        python_environment,
                    )
                else:
                    self._install_locked_dependency_environment(
                        addon_id,
                        contract,
                        python_environment,
                    )
            self._install_declared_dependency_setup(
                addon_id,
                contract,
                python_environment,
            )

    def _run_patchright_chromium_postcondition(
        self,
        addon_id: str,
        python_environment: PythonDependencyEnvironment,
        environment_paths: AddonEnvironmentPaths,
    ) -> subprocess.CompletedProcess[str]:
        """Run the fixed browser probe in the environment that owns it."""

        if python_environment.environment == DependencyEnvironment.MANAGED_PROCESS_PIXI:
            return self._get_pixi_runtime_backend().execute(
                environment_paths.pixi_manifest,
                _PATCHRIGHT_CHROMIUM_POSTCONDITION_ARGV,
                timeout_seconds=_PATCHRIGHT_CHROMIUM_POSTCONDITION_TIMEOUT_SECONDS,
                check=False,
            )

        env = os.environ.copy()
        existing_pythonpath = env.get("PYTHONPATH")
        env["PYTHONPATH"] = os.pathsep.join(
            item
            for item in (str(environment_paths.lib_dir), existing_pythonpath)
            if item
        )
        return subprocess.run(
            [sys.executable, *_PATCHRIGHT_CHROMIUM_POSTCONDITION_ARGV[1:]],
            cwd=str(environment_paths.root),
            env=env,
            capture_output=True,
            text=True,
            timeout=_PATCHRIGHT_CHROMIUM_POSTCONDITION_TIMEOUT_SECONDS,
            check=False,
        )

    def _declared_dependency_setup_is_ready(
        self,
        addon_id: str,
        python_environment: PythonDependencyEnvironment,
        environment_paths: AddonEnvironmentPaths,
    ) -> bool:
        """Probe declared browser actions during lifecycle readiness checks."""

        actions = tuple(getattr(python_environment, "setup", ()) or ())
        if not actions:
            return True
        dependency_lock = getattr(python_environment, "lock", None)
        if dependency_lock is None:
            return False

        for action in actions:
            if not (
                action.kind == "browser"
                and action.provider == "patchright"
                and action.target == "chromium"
            ):
                return False
            action_sha256 = hashlib.sha256(
                json.dumps(
                    asdict(action),
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest()
            marker = environment_paths.root / "setup" / f"{action.id}.json"
            try:
                marker_payload = json.loads(marker.read_text(encoding="utf-8"))
            except (OSError, TypeError, ValueError):
                return False
            if (
                marker_payload.get("action_sha256") != action_sha256
                or marker_payload.get("lock_sha256") != dependency_lock.sha256
                or marker_payload.get("postcondition")
                != _PATCHRIGHT_CHROMIUM_POSTCONDITION
            ):
                return False
            try:
                result = self._run_patchright_chromium_postcondition(
                    addon_id,
                    python_environment,
                    environment_paths,
                )
            except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
                logger.warning(
                    "Patchright Chromium readiness probe failed for add-on '%s': %s",
                    addon_id,
                    exc,
                )
                return False
            if result.returncode != 0:
                logger.warning(
                    "Patchright Chromium readiness probe failed for add-on '%s': %s",
                    addon_id,
                    self._dependency_setup_result_detail(result),
                )
                return False
        return True

    def _contract_python_environment_is_ready(
        self,
        addon_id: str,
        python_environment: PythonDependencyEnvironment,
    ) -> bool:
        """Return whether one declared immutable environment is usable."""

        dependency_lock = getattr(python_environment, "lock", None)
        if dependency_lock is None:
            return True
        environment_paths = self._addon_environment_paths(
            addon_id,
            python_environment,
        )
        if python_environment.environment == DependencyEnvironment.MANAGED_PROCESS_PIXI:
            required_path = (
                environment_paths.pixi_project_dir / ".pixi" / "envs" / "default"
            )
            expected_metadata = None
        else:
            required_path = environment_paths.lib_dir
            expected_metadata = self._in_process_python_identity()
        if not self._locked_environment_is_ready(
            environment_paths.root / "environment.json",
            lock_sha256=dependency_lock.sha256,
            required_path=required_path,
            expected_metadata=expected_metadata,
        ):
            return False
        return self._declared_dependency_setup_is_ready(
            addon_id,
            python_environment,
            environment_paths,
        )

    def install_dependencies(
        self,
        addon_id: str,
        *,
        prepare_docker: bool = True,
    ) -> None:
        """Installs addon dependencies and optionally prepares Docker images."""
        addon_dir = self.get_addon_directory(addon_id)
        req_path = addon_dir / "requirements.txt"
        manifest_path = addon_dir / "manifest.json"

        manifest = None
        if manifest_path.exists():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                logger.error(
                    "Failed to read manifest for add-on '%s': %s",
                    addon_id,
                    exc,
                )
                raise RuntimeError(
                    f"Failed to parse manifest.json for add-on '{addon_id}'. "
                    f"Details: {exc}"
                ) from exc
            manifest_errors = validate_manifest_against_schema(manifest)
            if manifest_errors:
                details = "; ".join(manifest_errors)
                raise RuntimeError(
                    f"Manifest validation failed for add-on '{addon_id}': {details}"
                )

        docker_config = manifest.get("docker") if manifest else None
        effective_docker_config = docker_config if prepare_docker else None

        package = (getattr(self, "_addon_packages", None) or {}).get(addon_id)
        contract = getattr(package, "contract", None)
        if (
            contract is not None
            and getattr(contract, "manifest_schema_version", 1) == 2
        ):
            # Schema 2 is authoritative. Never reinterpret a container's
            # requirements.txt as host dependencies or bypass its committed
            # lock merely because the package ships in the built-in tree.
            self._install_contract_python_environments(addon_id, contract)
            if effective_docker_config:
                if not self._refresh_docker_status(log_failure=False):
                    raise RuntimeError(
                        "Docker Engine is not available. Cannot prepare Docker-backed add-on dependencies."
                    )
                self._ensure_docker_image_prepared(
                    addon_id,
                    manifest=manifest,
                    force_refresh=True,
                )
            return

        if self._addon_source_kind(addon_id) == AddonSourceKind.INSTALLED:
            package = self._addon_packages.get(addon_id)
            contract = getattr(package, "contract", None)
            if contract is None:
                raise RuntimeError(
                    f"Installed add-on '{addon_id}' has no compiled Add-on API contract."
                )
            self._install_contract_python_environments(addon_id, contract)
            if effective_docker_config:
                if not self._refresh_docker_status(log_failure=False):
                    raise RuntimeError(
                        "Docker Engine is not available. Cannot prepare Docker-backed add-on dependencies."
                    )
                self._ensure_docker_image_prepared(
                    addon_id,
                    manifest=manifest,
                    force_refresh=True,
                )
            return

        # Check explicit dependencies in manifest
        manifest_deps = manifest.get("dependencies", {}) if manifest else {}
        python_deps = manifest_deps.get("python", {})
        environment_paths = self._addon_environment_paths(addon_id)

        # Select the dependency environment independently of the package's
        # runtime units. Legacy numbered tier values remain accepted inputs.
        source_pixi_toml_path = addon_dir / "pixi.toml"
        pixi_toml_path = environment_paths.pixi_manifest
        package = (getattr(self, "_addon_packages", None) or {}).get(addon_id)
        contract = getattr(package, "contract", None)
        contract_environment = getattr(contract, "dependency_environment", None)
        if (
            contract_environment is not None
            and getattr(
                contract,
                "manifest_schema_version",
                1,
            )
            == 2
        ):
            dependency_environment = contract_environment.value
        elif source_pixi_toml_path.exists():
            dependency_environment = DEPENDENCY_ENV_MANAGED_PIXI
        else:
            raw_environment = str(
                python_deps.get("environment")
                or python_deps.get("tier")
                or DEPENDENCY_ENV_IN_PROCESS_VENDOR
            ).strip()
            dependency_environment = _LEGACY_DEPENDENCY_ENVIRONMENTS.get(
                raw_environment,
                raw_environment,
            )
            if python_deps.get("conda_packages") or manifest_deps.get(
                "system_packages"
            ):
                dependency_environment = DEPENDENCY_ENV_MANAGED_PIXI

        pip_packages = list(python_deps.get("pip_packages", []))
        native_pip_packages = list(pip_packages)
        if req_path.exists():
            native_pip_packages.append(f"-r{req_path}")
            if not source_pixi_toml_path.exists():
                for raw_line in req_path.read_text(encoding="utf-8").splitlines():
                    requirement = raw_line.strip()
                    if not requirement or requirement.startswith("#"):
                        continue
                    if requirement.startswith("-"):
                        raise RuntimeError(
                            f"Add-on '{addon_id}' uses an advanced requirements.txt "
                            "directive. Ship a reviewed pixi.toml for managed execution."
                        )
                    pip_packages.append(requirement)

        def install_native_packages(packages: list[str]) -> None:
            """Install dependencies imported by in-process add-on modules."""

            if not packages:
                return
            lib_dir = environment_paths.lib_dir
            lib_dir.mkdir(parents=True, exist_ok=True)
            uv_bin = shutil.which("uv")
            if uv_bin:
                exec_cmd = [
                    uv_bin,
                    "pip",
                    "install",
                    "--python",
                    sys.executable,
                    "--target",
                    str(lib_dir),
                    *packages,
                ]
            else:
                exec_cmd = [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    "--target",
                    str(lib_dir),
                    *packages,
                ]
            logger.info(
                "Installing in-process Python dependencies for add-on '%s' into %s.",
                addon_id,
                lib_dir,
            )
            subprocess.check_call(exec_cmd, cwd=str(environment_paths.root))

        post_install_cmds = self._normalize_post_install_commands(
            (manifest or {}).get("post_install_commands", [])
        )

        filtered_cmds = []
        for cmd in post_install_cmds:
            if self._command_uses_pip(cmd) or (
                self._command_uses_python(cmd) and "-m" in cmd and "pip" in cmd
            ):
                logger.warning(
                    "Add-on '%s' uses pip install in post_install_commands; "
                    "moving those packages into its declared dependency environment.",
                    addon_id,
                )
                pkgs = [
                    arg
                    for arg in cmd
                    if arg
                    not in ("python", "python3", "py", "py.exe", "-m", "pip", "install")
                ]
                if pkgs:
                    pip_packages.extend(pkgs)
                    native_pip_packages.extend(pkgs)
            else:
                filtered_cmds.append(cmd)

        if (
            not pip_packages
            and not effective_docker_config
            and not filtered_cmds
            and dependency_environment != DEPENDENCY_ENV_MANAGED_PIXI
        ):
            return

        try:
            if dependency_environment == DEPENDENCY_ENV_MANAGED_PIXI:
                logger.info(
                    "Setting up managed Pixi environment for add-on '%s'...",
                    addon_id,
                )
                self.setup_pixi_environment(
                    addon_id,
                    conda_packages=list(python_deps.get("conda_packages") or ())
                    + list(manifest_deps.get("system_packages") or ()),
                    pip_packages=pip_packages,
                    python_version=str(python_deps.get("python_version") or "3.11"),
                )
                pixi_toml_path = self._prepare_addon_pixi_project(addon_id)
                # A Pixi manifest governs worker/subprocess dependencies. Any
                # manifest pip_packages are separately imported by trusted
                # in-process routes and therefore live in the non-shadowing
                # add-on vendor target registered by _load_addon().
                install_native_packages(native_pip_packages)
            elif pip_packages:
                logger.info(
                    "Setting up in-process vendor dependencies for add-on '%s'...",
                    addon_id,
                )
                install_native_packages(native_pip_packages)

            if contract is not None:
                self._install_declared_dependency_setup(addon_id, contract)

            if filtered_cmds:
                logger.info(f"Running post-install commands for '{addon_id}'...")
                env = os.environ.copy()
                lib_dir = environment_paths.lib_dir
                environment_paths.root.mkdir(parents=True, exist_ok=True)
                env["CARLINA_ADDON_CODE_DIR"] = str(addon_dir)
                env["CARLINA_ADDON_ENVIRONMENT_DIR"] = str(environment_paths.root)

                if (
                    dependency_environment == DEPENDENCY_ENV_IN_PROCESS_VENDOR
                    and lib_dir.exists()
                ):
                    existing_pythonpath = env.get("PYTHONPATH")
                    env["PYTHONPATH"] = os.pathsep.join(
                        path for path in (str(lib_dir), existing_pythonpath) if path
                    )

                for cmd in filtered_cmds:
                    exec_cmd = list(cmd)
                    if self._command_uses_python(exec_cmd):
                        if dependency_environment == DEPENDENCY_ENV_MANAGED_PIXI:
                            exec_cmd = self._get_pixi_runtime_backend().command(
                                pixi_toml_path,
                                ["python", *exec_cmd[1:]],
                            )
                        else:
                            exec_cmd[0] = sys.executable

                    logger.info(f"Executing: {' '.join(exec_cmd)}")
                    subprocess.check_call(
                        exec_cmd,
                        env=env,
                        cwd=str(environment_paths.root),
                    )
                logger.info(
                    f"Successfully ran all post-install commands for '{addon_id}'."
                )

            if effective_docker_config:
                if not self._refresh_docker_status(log_failure=False):
                    raise RuntimeError(
                        "Docker Engine is not available. Cannot prepare Docker-backed addon dependencies."
                    )
                self._ensure_docker_image_prepared(
                    addon_id, manifest=manifest, force_refresh=True
                )
                logger.info(
                    f"Successfully prepared Docker image for addon '{addon_id}'."
                )

        except subprocess.CalledProcessError as e:
            logger.error(f"Dependency setup failed for '{addon_id}': {e}")
            raise RuntimeError(
                f"Failed to install or setup dependencies for this addon. Details: {e}"
            )

    @staticmethod
    def _dependency_setup_result_detail(result: subprocess.CompletedProcess[str]) -> str:
        return (
            result.stderr
            or result.stdout
            or f"exit code {result.returncode}"
        ).strip()

    @staticmethod
    def _write_dependency_setup_marker(
        marker: Path,
        metadata: dict[str, Any],
    ) -> None:
        temporary = marker.with_name(f".{marker.name}.tmp-{os.getpid()}")
        temporary.write_text(
            json.dumps(metadata, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, marker)

    @staticmethod
    def _invalidate_dependency_setup_marker(marker: Path) -> None:
        """Atomically remove a stale success receipt from the active path."""

        if not marker.exists():
            return
        invalidated = marker.with_name(f".{marker.name}.invalid-{os.getpid()}")
        try:
            os.replace(marker, invalidated)
        except FileNotFoundError:
            return
        try:
            invalidated.unlink()
        except OSError:
            logger.warning(
                "Could not remove invalidated dependency setup marker '%s'.",
                invalidated,
            )

    def get_dependency_setup_status(
        self,
        addon_id: str,
        action_id: str,
        *,
        runtime_unit: str | None = None,
    ) -> dict[str, object]:
        """Return recorded, side-effect-free readiness for one setup action.

        This intentionally does not execute the action or a browser probe.  UI
        and request-time diagnostics must never cause a dependency download or
        repair.  The marker is trustworthy only because setup writes it after
        its action-specific postcondition succeeds.
        """

        package = (getattr(self, "_addon_packages", None) or {}).get(addon_id)
        contract = getattr(package, "contract", None)
        if contract is None:
            return {
                "state": "degraded",
                "reasons": [
                    f"The managed dependency contract for add-on '{addon_id}' is unavailable."
                ],
            }

        environments = tuple(getattr(contract, "python_environments", ()) or ())
        if runtime_unit is not None:
            environments = tuple(
                environment
                for environment in environments
                if runtime_unit
                in tuple(getattr(environment, "runtime_units", ()) or ())
            )
        for python_environment in environments:
            action = next(
                (
                    candidate
                    for candidate in tuple(
                        getattr(python_environment, "setup", ()) or ()
                    )
                    if candidate.id == action_id
                ),
                None,
            )
            if action is None:
                continue
            if not (
                action.kind == "browser"
                and action.provider == "patchright"
                and action.target == "chromium"
            ):
                return {
                    "state": "degraded",
                    "reasons": [
                        f"Dependency setup action '{action_id}' has no supported readiness check."
                    ],
                }
            dependency_lock = getattr(python_environment, "lock", None)
            if dependency_lock is None:
                return {
                    "state": "degraded",
                    "reasons": [
                        f"Dependency setup action '{action_id}' has no immutable lock."
                    ],
                }
            environment_paths = self._addon_environment_paths(
                addon_id,
                python_environment,
            )
            marker = environment_paths.root / "setup" / f"{action.id}.json"
            try:
                marker_payload = json.loads(marker.read_text(encoding="utf-8"))
            except (OSError, TypeError, ValueError):
                marker_payload = {}
            action_sha256 = hashlib.sha256(
                json.dumps(
                    asdict(action),
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest()
            if (
                marker_payload.get("action_sha256") != action_sha256
                or marker_payload.get("lock_sha256") != dependency_lock.sha256
                or marker_payload.get("postcondition")
                != _PATCHRIGHT_CHROMIUM_POSTCONDITION
            ):
                return {
                    "state": "degraded",
                    "reasons": [
                        (
                            "Patchright Chromium has not passed its managed-runtime "
                            "setup check. Prepare or activate the scraping add-on."
                        )
                    ],
                }
            if (
                python_environment.environment
                == DependencyEnvironment.MANAGED_PROCESS_PIXI
                and not (
                    environment_paths.pixi_project_dir
                    / ".pixi"
                    / "envs"
                    / "default"
                ).is_dir()
            ):
                return {
                    "state": "degraded",
                    "reasons": [
                        (
                            "The managed Patchright Pixi environment is missing; "
                            "prepare or activate the scraping add-on."
                        )
                    ],
                }
            return {
                "state": "available",
                "reasons": [
                    "Patchright Chromium passed the managed-runtime setup check."
                ],
            }
        return {
            "state": "degraded",
            "reasons": [
                f"Dependency setup action '{action_id}' is not declared for add-on '{addon_id}'."
            ],
        }

    def _install_declared_dependency_setup(
        self,
        addon_id: str,
        contract: ResolvedAddonContract,
        python_environment: PythonDependencyEnvironment | None = None,
    ) -> tuple[str, ...]:
        """Run bounded host-owned setup actions from the immutable contract.

        This is deliberately not a generic command runner. Each supported
        action is translated to a fixed argv by Carlina, and completion is
        recorded inside the rebuildable dependency environment.
        """

        from filelock import FileLock

        actions = tuple(
            getattr(
                python_environment,
                "setup",
                getattr(contract, "dependency_setup", ()),
            )
            or ()
        )
        if not actions:
            return ()
        dependency_lock = (
            getattr(python_environment, "lock", None)
            if python_environment is not None
            else getattr(contract, "dependency_lock", None)
        )
        if dependency_lock is None:
            raise RuntimeError(
                f"Add-on '{addon_id}' declares dependency setup without a lock."
            )

        environment_paths = (
            self._addon_environment_paths(addon_id, python_environment)
            if python_environment is not None
            else self._addon_environment_paths(addon_id)
        )
        setup_root = environment_paths.root / "setup"
        setup_root.mkdir(parents=True, exist_ok=True)
        app_paths = self.app.extensions["carlina_paths"]
        setup_lock = Path(app_paths.locks_dir) / "addons" / f"{addon_id}.setup.lock"
        setup_lock.parent.mkdir(parents=True, exist_ok=True)
        completed_actions: list[str] = []

        with FileLock(str(setup_lock), timeout=900):
            for action in actions:
                action_payload = asdict(action)
                action_sha256 = hashlib.sha256(
                    json.dumps(
                        action_payload,
                        separators=(",", ":"),
                        sort_keys=True,
                    ).encode("utf-8")
                ).hexdigest()
                marker = setup_root / f"{action.id}.json"
                metadata = {
                    "format": "carlina.addon-dependency-setup",
                    "version": 1,
                    "addon_id": addon_id,
                    "action": action_payload,
                    "action_sha256": action_sha256,
                    "lock_sha256": dependency_lock.sha256,
                    "postcondition": _PATCHRIGHT_CHROMIUM_POSTCONDITION,
                    "created_at": utc_iso(),
                }
                try:
                    marker_payload = json.loads(marker.read_text(encoding="utf-8"))
                except OSError, TypeError, ValueError:
                    marker_payload = {}

                if not (
                    action.kind == "browser"
                    and action.provider == "patchright"
                    and action.target == "chromium"
                ):
                    raise RuntimeError(
                        f"Unsupported dependency setup action for add-on '{addon_id}': "
                        f"{action.kind}/{action.provider}/{action.target}."
                    )

                managed_pixi = (
                    getattr(python_environment, "environment", None)
                    if python_environment is not None
                    else contract.dependency_environment
                ) == DependencyEnvironment.MANAGED_PROCESS_PIXI
                if managed_pixi:
                    pixi_manifest = (
                        self._prepare_addon_pixi_project(
                            addon_id,
                            runtime_unit=action.runtime_unit,
                            python_environment=python_environment,
                        )
                        if python_environment is not None
                        else self._prepare_addon_pixi_project(addon_id)
                    )
                    runtime_backend = self._get_pixi_runtime_backend()

                    def execute_setup_command(
                        argv: tuple[str, ...],
                        timeout_seconds: int,
                        _runtime_backend=runtime_backend,
                        _pixi_manifest=pixi_manifest,
                    ) -> subprocess.CompletedProcess[str]:
                        return _runtime_backend.execute(
                            _pixi_manifest,
                            argv,
                            timeout_seconds=timeout_seconds,
                            check=False,
                        )

                else:
                    env = os.environ.copy()
                    existing_pythonpath = env.get("PYTHONPATH")
                    env["PYTHONPATH"] = os.pathsep.join(
                        item
                        for item in (
                            str(environment_paths.lib_dir),
                            existing_pythonpath,
                        )
                        if item
                    )

                    def execute_setup_command(
                        argv: tuple[str, ...],
                        timeout_seconds: int,
                        _env=env,
                        _environment_root=environment_paths.root,
                    ) -> subprocess.CompletedProcess[str]:
                        return subprocess.run(
                            [sys.executable, *argv[1:]],
                            cwd=str(_environment_root),
                            env=_env,
                            capture_output=True,
                            text=True,
                            timeout=timeout_seconds,
                            check=False,
                        )

                marker_matches_contract = (
                    marker_payload.get("action_sha256") == action_sha256
                    and marker_payload.get("lock_sha256") == dependency_lock.sha256
                )
                if marker_matches_contract:
                    verification = execute_setup_command(
                        _PATCHRIGHT_CHROMIUM_POSTCONDITION_ARGV,
                        _PATCHRIGHT_CHROMIUM_POSTCONDITION_TIMEOUT_SECONDS,
                    )
                    if verification.returncode == 0:
                        if (
                            marker_payload.get("postcondition")
                            != _PATCHRIGHT_CHROMIUM_POSTCONDITION
                        ):
                            self._write_dependency_setup_marker(marker, metadata)
                        completed_actions.append(action.id)
                        continue
                    logger.warning(
                        "Patchright Chromium setup marker for add-on '%s' failed "
                        "its managed-runtime postcondition; provisioning again: %s",
                        addon_id,
                        self._dependency_setup_result_detail(verification),
                    )
                    self._invalidate_dependency_setup_marker(marker)

                self._invalidate_dependency_setup_marker(marker)
                result = execute_setup_command(
                    _PATCHRIGHT_CHROMIUM_INSTALL_ARGV,
                    1800,
                )
                if result.returncode != 0:
                    raise RuntimeError(
                        f"Could not provision {action.target} for add-on "
                        f"'{addon_id}': {self._dependency_setup_result_detail(result)}"
                    )

                verification = execute_setup_command(
                    _PATCHRIGHT_CHROMIUM_POSTCONDITION_ARGV,
                    _PATCHRIGHT_CHROMIUM_POSTCONDITION_TIMEOUT_SECONDS,
                )
                if verification.returncode != 0:
                    raise RuntimeError(
                        "Patchright Chromium postcondition failed for add-on "
                        f"'{addon_id}': {self._dependency_setup_result_detail(verification)}"
                    )

                self._write_dependency_setup_marker(marker, metadata)
                completed_actions.append(action.id)
        return tuple(completed_actions)

    def _verified_dependency_lock_path(
        self,
        addon_id: str,
        dependency_lock: DependencyLock,
    ) -> Path:
        addon_dir = self.get_addon_directory(addon_id).resolve()
        lock_path = (addon_dir / dependency_lock.path).resolve()
        lock_path.relative_to(addon_dir)
        if not lock_path.is_file():
            raise FileNotFoundError(f"Dependency lock not found: {lock_path}")
        actual_sha256 = hashlib.sha256(lock_path.read_bytes()).hexdigest()
        if actual_sha256 != dependency_lock.sha256:
            raise RuntimeError(
                f"Dependency lock changed after contract validation for add-on '{addon_id}'."
            )
        return lock_path

    def _verified_dependency_manifest_path(
        self,
        addon_id: str,
        python_environment: PythonDependencyEnvironment,
    ) -> Path:
        addon_dir = self.get_addon_directory(addon_id).resolve()
        declared_path = str(
            getattr(python_environment, "manifest_path", "") or ""
        ).strip()
        if not declared_path:
            raise RuntimeError(
                f"Add-on '{addon_id}' has no declared Pixi manifest path."
            )
        manifest_path = (addon_dir / declared_path).resolve()
        manifest_path.relative_to(addon_dir)
        if not manifest_path.is_file():
            raise FileNotFoundError(f"Dependency manifest not found: {manifest_path}")
        expected_sha256 = str(getattr(python_environment, "manifest_sha256", "") or "")
        actual_sha256 = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        if expected_sha256 and actual_sha256 != expected_sha256:
            raise RuntimeError(
                f"Dependency manifest changed after contract validation for add-on '{addon_id}'."
            )
        return manifest_path

    @staticmethod
    def _locked_environment_is_ready(
        marker: Path,
        *,
        lock_sha256: str,
        required_path: Path,
        expected_metadata: dict[str, object] | None = None,
    ) -> bool:
        if not marker.is_file() or not required_path.exists():
            return False
        try:
            metadata = json.loads(marker.read_text(encoding="utf-8"))
        except OSError, ValueError, TypeError:
            return False
        if not isinstance(metadata, dict):
            return False
        if metadata.get("lock_sha256") != lock_sha256:
            return False
        return all(
            metadata.get(key) == value
            for key, value in (expected_metadata or {}).items()
        )

    @staticmethod
    def _in_process_python_identity() -> dict[str, object]:
        """Describe the host ABI that a vendor environment must target."""

        return {
            "version": 2,
            "dependency_environment": DependencyEnvironment.IN_PROCESS_VENDOR.value,
            "lock_format": "pip_requirements",
            "python_version": platform.python_version(),
            "python_implementation": sys.implementation.name,
            "python_cache_tag": str(sys.implementation.cache_tag or ""),
            "python_soabi": str(sysconfig.get_config_var("SOABI") or ""),
            "python_platform": sysconfig.get_platform(),
        }

    @staticmethod
    def _require_compatible_in_process_python(
        contract: ResolvedAddonContract,
        python_environment: PythonDependencyEnvironment | None = None,
    ) -> None:
        """Reject vendor wheels targeting a different host Python ABI."""

        environment_kind = (
            getattr(python_environment, "environment", None)
            if python_environment is not None
            else contract.dependency_environment
        )
        if environment_kind != DependencyEnvironment.IN_PROCESS_VENDOR:
            return
        declared = str(
            getattr(
                python_environment,
                "python_version",
                getattr(contract, "python_version", ""),
            )
            or ""
        ).strip()
        if not declared:
            if int(getattr(contract, "manifest_schema_version", 1) or 1) < 2:
                return
            raise RuntimeError(
                f"Add-on '{contract.addon_id}' does not declare its in-process Python version."
            )
        current = f"{sys.version_info.major}.{sys.version_info.minor}"
        if declared != current:
            raise RuntimeError(
                f"Add-on '{contract.addon_id}' targets Python {declared}, but the "
                f"Carlina host is Python {current}. Use a compatible package release "
                "or move the add-on to a managed-process/container runtime."
            )

    def _install_locked_pixi_environment(
        self,
        addon_id: str,
        contract: ResolvedAddonContract,
        python_environment: PythonDependencyEnvironment | None = None,
    ) -> Path:
        """Materialize an immutable installed add-on Pixi project below DataRoot."""

        from filelock import FileLock

        dependency_lock = (
            getattr(python_environment, "lock", None)
            if python_environment is not None
            else contract.dependency_lock
        )
        if dependency_lock is None or dependency_lock.format != "pixi":
            raise ValueError("A Pixi dependency lock is required.")
        self._verified_dependency_lock_path(addon_id, dependency_lock)
        if python_environment is not None:
            self._verified_dependency_manifest_path(addon_id, python_environment)
        environment_paths = self._addon_environment_paths(
            addon_id,
            python_environment,
        )
        target = environment_paths.root
        marker = target / "environment.json"
        pixi_environment = environment_paths.pixi_project_dir / ".pixi"
        if self._locked_environment_is_ready(
            marker,
            lock_sha256=dependency_lock.sha256,
            required_path=pixi_environment,
        ):
            return target

        paths = self.app.extensions["carlina_paths"]
        install_lock = (
            Path(paths.locks_dir) / "addons" / f"{addon_id}.dependencies.lock"
        )
        install_lock.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(str(install_lock), timeout=900):
            if self._locked_environment_is_ready(
                marker,
                lock_sha256=dependency_lock.sha256,
                required_path=pixi_environment,
            ):
                return target
            manifest = self._prepare_addon_pixi_project(
                addon_id,
                python_environment=python_environment,
            )
            copied_lock = manifest.parent / "pixi.lock"
            if (
                not copied_lock.is_file()
                or hashlib.sha256(copied_lock.read_bytes()).hexdigest()
                != dependency_lock.sha256
            ):
                raise RuntimeError(
                    f"The materialized Pixi lock is invalid for add-on '{addon_id}'."
                )
            if pixi_environment.exists():
                shutil.rmtree(pixi_environment)
            self._get_pixi_runtime_backend().install(manifest, locked=True)
            metadata = {
                "format": "carlina.addon-environment",
                "version": 1,
                "addon_id": addon_id,
                "addon_version": contract.version,
                "environment_id": str(getattr(python_environment, "id", "default")),
                "dependency_environment": DependencyEnvironment.MANAGED_PROCESS_PIXI.value,
                "lock_format": dependency_lock.format,
                "lock_sha256": dependency_lock.sha256,
                "environment_fingerprint": (
                    python_environment_fingerprint(python_environment)
                    if python_environment is not None
                    else ""
                ),
                "created_at": utc_iso(),
            }
            marker.parent.mkdir(parents=True, exist_ok=True)
            temporary = marker.with_name(f".{marker.name}.tmp-{os.getpid()}")
            temporary.write_text(
                json.dumps(metadata, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, marker)
        return target

    def _install_locked_dependency_environment(
        self,
        addon_id: str,
        contract: ResolvedAddonContract,
        python_environment: PythonDependencyEnvironment | None = None,
    ) -> Path:
        """Materialize one immutable, hash-addressed installed-add-on environment."""

        from filelock import FileLock

        self._require_compatible_in_process_python(contract, python_environment)
        dependency_lock = (
            getattr(python_environment, "lock", None)
            if python_environment is not None
            else contract.dependency_lock
        )
        if dependency_lock is None or dependency_lock.format != "pip_requirements":
            raise ValueError("A hashed pip requirements lock is required.")
        paths = self.app.extensions["carlina_paths"]
        environment_paths = self._addon_environment_paths(
            addon_id,
            python_environment,
        )
        target = environment_paths.root
        addon_root = target.parent
        marker = target / "environment.json"
        lock_path = self._verified_dependency_lock_path(addon_id, dependency_lock)
        python_identity = self._in_process_python_identity()
        if self._locked_environment_is_ready(
            marker,
            lock_sha256=dependency_lock.sha256,
            required_path=target / "lib",
            expected_metadata=python_identity,
        ):
            self.prepare_addon_import_indexes(addon_id)
            self._ensure_addon_python_path(addon_id, python_environment)
            return target

        addon_root.mkdir(parents=True, exist_ok=True)
        install_lock = (
            Path(paths.locks_dir) / "addons" / f"{addon_id}.dependencies.lock"
        )
        install_lock.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(str(install_lock), timeout=900):
            if self._locked_environment_is_ready(
                marker,
                lock_sha256=dependency_lock.sha256,
                required_path=target / "lib",
                expected_metadata=python_identity,
            ):
                self.prepare_addon_import_indexes(addon_id)
                self._ensure_addon_python_path(addon_id, python_environment)
                return target
            if target.exists():
                shutil.rmtree(target)
            staging = Path(
                tempfile.mkdtemp(
                    prefix=f".{dependency_lock.sha256}.tmp-",
                    dir=addon_root,
                )
            )
            try:
                lib_dir = staging / environment_paths.lib_dir.name
                lib_dir.mkdir()
                uv_bin = shutil.which("uv")
                if uv_bin:
                    command = [
                        uv_bin,
                        "pip",
                        "install",
                        "--python",
                        sys.executable,
                        "--require-hashes",
                        "--target",
                        str(lib_dir),
                        "-r",
                        str(lock_path),
                    ]
                else:
                    command = [
                        sys.executable,
                        "-m",
                        "pip",
                        "install",
                        "--disable-pip-version-check",
                        "--require-hashes",
                        "--target",
                        str(lib_dir),
                        "-r",
                        str(lock_path),
                    ]
                logger.info(
                    "Installing locked dependencies for add-on '%s' into %s.",
                    addon_id,
                    target,
                )
                subprocess.check_call(
                    command, cwd=str(self.get_addon_directory(addon_id))
                )
                (staging / "environment.json").write_text(
                    json.dumps(
                        {
                            "format": "carlina.addon-environment",
                            "addon_id": addon_id,
                            "addon_version": contract.version,
                            "environment_id": str(
                                getattr(python_environment, "id", "default")
                            ),
                            "dependency_environment": (
                                DependencyEnvironment.IN_PROCESS_VENDOR.value
                            ),
                            "lock_sha256": dependency_lock.sha256,
                            "environment_fingerprint": (
                                python_environment_fingerprint(python_environment)
                                if python_environment is not None
                                else ""
                            ),
                            "created_at": utc_iso(),
                            **python_identity,
                        },
                        indent=2,
                        sort_keys=True,
                    )
                    + "\n",
                    encoding="utf-8",
                )
                try:
                    prepare_vendor_import_index(lib_dir)
                except (OSError, UnicodeError, ValueError) as exc:
                    logger.warning(
                        "Could not prepare vendor import index for add-on '%s': %s",
                        addon_id,
                        exc,
                    )
                if target.exists():
                    shutil.rmtree(staging)
                else:
                    os.replace(staging, target)
            except Exception:
                shutil.rmtree(staging, ignore_errors=True)
                raise
        self._ensure_addon_python_path(addon_id, python_environment)
        return target


__all__ = ["DependencyOrchestrationHost", "DependencyOrchestrationMixin"]
