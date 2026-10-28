from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from threading import Timer
from typing import Any, BinaryIO

from carlina.product_identity import product_environment_value
from iarf_assistant.core.app_paths import load_runtime_environment
from iarf_assistant.core.managed_worker_quiescence import (
    ManagedWorkerQuiescenceAlreadyActive,
    managed_worker_quiescence_gate,
)

RESTART_COMMAND_ENV = "CARLINA_RESTART_COMMAND_JSON"
LEGACY_RESTART_COMMAND_ENV = "IARF_RESTART_COMMAND_JSON"
RESTART_STRATEGY_ENV = "CARLINA_RESTART_STRATEGY"
SUPERVISOR_RESTART_STRATEGY = "supervisor-exit"
SUPERVISOR_RESTART_EXIT_CODE = 75
DEFAULT_RESTART_HOST = "127.0.0.1"
DEFAULT_RESTART_PORT = 5003
_LOGGER = logging.getLogger(__name__)
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_PACKAGE_ROOT = _PROJECT_ROOT / "iarf_assistant"
_RESTART_HANDOFF_LOG_DIRECTORY = "restart-handoff"
_RESTART_HANDOFF_LOG_FILENAME = "successor.log"


def build_restart_command(host: str, port: int) -> list[str]:
    return [
        sys.executable,
        "-m",
        "carlina.bootstrap_manager",
        "--host",
        host,
        "--port",
        str(port),
        "--start-only",
        "--no-open-browser",
    ]


def _coerce_port(value: int | str | None) -> int | None:
    if value is None:
        return None
    try:
        port = int(value)
    except TypeError, ValueError:
        return None
    if 1 <= port <= 65535:
        return port
    return None


def resolve_restart_target(
    host: str | None = None, port: int | str | None = None
) -> tuple[str, int]:
    resolved_host = (
        str(host or product_environment_value("HOST") or DEFAULT_RESTART_HOST).strip()
        or DEFAULT_RESTART_HOST
    )
    resolved_port = _coerce_port(port)
    if resolved_port is None:
        resolved_port = (
            _coerce_port(product_environment_value("PORT")) or DEFAULT_RESTART_PORT
        )
    return resolved_host, resolved_port


def configure_restart_environment(host: str, port: int) -> list[str]:
    resolved_host, resolved_port = resolve_restart_target(host, port)
    command = build_restart_command(resolved_host, resolved_port)
    serialized_command = json.dumps(command)
    os.environ[RESTART_COMMAND_ENV] = serialized_command
    os.environ[LEGACY_RESTART_COMMAND_ENV] = serialized_command
    os.environ["CARLINA_HOST"] = resolved_host
    os.environ["CARLINA_PORT"] = str(resolved_port)
    os.environ["IARF_HOST"] = resolved_host
    os.environ["IARF_PORT"] = str(resolved_port)
    return command


def get_configured_restart_command() -> list[str] | None:
    raw_command = os.environ.get(RESTART_COMMAND_ENV) or os.environ.get(
        LEGACY_RESTART_COMMAND_ENV
    )
    if not raw_command:
        return None
    try:
        command = json.loads(raw_command)
    except json.JSONDecodeError:
        _LOGGER.exception("Invalid managed restart command JSON.")
        return None
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(part, str) for part in command)
    ):
        _LOGGER.error("Invalid managed restart command shape.")
        return None
    return command


def _restart_handoff_log_path() -> Path:
    """Return the private, canonical log path for one restart successor.

    The runtime path resolver is the owner of Carlina's mutable layout.  This
    deliberately does not accept a caller-provided path: the restart endpoint
    must never be able to redirect a child process's output arbitrarily.
    """

    program_root = product_environment_value("PROGRAM_ROOT", default=str(_PROJECT_ROOT))
    paths = load_runtime_environment(
        Path(program_root or _PROJECT_ROOT),
        package_root=_PACKAGE_ROOT,
    )
    handoff_dir = paths.log_dir / _RESTART_HANDOFF_LOG_DIRECTORY
    handoff_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        handoff_dir.chmod(0o700)
    return handoff_dir / _RESTART_HANDOFF_LOG_FILENAME


def _open_restart_handoff_log() -> BinaryIO:
    """Open the restart handoff log with owner-only file permissions."""

    log_path = _restart_handoff_log_path()
    descriptor = os.open(
        log_path,
        os.O_WRONLY | os.O_CREAT | os.O_APPEND,
        0o600,
    )
    try:
        if os.name != "nt":
            os.fchmod(descriptor, 0o600)
        return os.fdopen(descriptor, "ab")
    except Exception:
        os.close(descriptor)
        raise


def _restart_spawn_kwargs(
    handoff_log: BinaryIO,
    *,
    platform_name: str | None = None,
) -> dict[str, Any]:
    """Return isolated process settings for a managed restart successor.

    A restart is initiated from the web process and that process exits as soon
    as the successor has been spawned.  Leaving the successor in the same
    terminal/process group makes it vulnerable to the originating launcher
    closing its PTY while the ``--wait-for-pid`` guard is still running.  The
    replacement must therefore not inherit either the terminal streams or its
    process group.
    """

    spawn_kwargs: dict[str, Any] = {
        "cwd": str(Path(__file__).resolve().parents[1]),
        "stdin": subprocess.DEVNULL,
        "stdout": handoff_log,
        "stderr": handoff_log,
        "close_fds": True,
    }
    if (platform_name or os.name) == "nt":
        creation_flags = (
            int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
            | int(getattr(subprocess, "DETACHED_PROCESS", 0))
            | int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
        )
        if creation_flags:
            spawn_kwargs["creationflags"] = creation_flags
    else:
        spawn_kwargs["start_new_session"] = True
    return spawn_kwargs


def _quiesce_current_managed_workers() -> dict[str, list[int]]:
    """Stop this manager's workers before handing its port and data root onward."""

    # Import lazily: bootstrap imports the restart command builder at module
    # import time, while only the timer thread needs process-tree operations.
    from iarf_assistant import bootstrap

    program_root = product_environment_value("PROGRAM_ROOT", default=str(_PROJECT_ROOT))
    paths = load_runtime_environment(
        Path(program_root or _PROJECT_ROOT),
        package_root=_PACKAGE_ROOT,
    )
    current_pid = os.getpid()
    tree_result: dict[str, list[int]] = {
        "terminated_pids": [],
        "failed_pids": [],
    }
    current_identity = bootstrap.process_identity(current_pid)
    if current_identity is None:
        raise OSError(
            "could not verify the current managed process identity before restart"
        )
    if os.name == "nt":
        tree_result = bootstrap.quiesce_windows_managed_process_descendants(
            current_pid,
            expected_identity=current_identity,
        )
    else:
        tree_result = bootstrap.quiesce_managed_process_descendants(
            current_pid,
            expected_identity=current_identity,
            term_timeout_seconds=5.0,
            kill_timeout_seconds=1.0,
        )

    cleanup = bootstrap.cleanup_orphan_worker_processes(
        project_root=paths.program_root,
        instance_dir=paths.instance_dir,
        state_dir=paths.state_dir,
        data_root=paths.data_root,
        installed_addons_dir=paths.installed_addons_dir,
        addon_environments_dir=paths.addon_environments_dir,
        current_pid=current_pid,
        skip_if_managed_server_active=False,
    )
    inspection_error = str(cleanup.get("inspection_error") or "").strip()
    if inspection_error:
        raise OSError(
            "could not verify managed worker quiescence before restart: "
            f"{inspection_error}"
        )
    return {
        "terminated_pids": sorted(
            {
                *[int(pid) for pid in tree_result.get("terminated_pids", [])],
                *[int(pid) for pid in cleanup.get("terminated_pids", [])],
            }
        ),
        "failed_pids": sorted(
            {
                *[int(pid) for pid in tree_result.get("failed_pids", [])],
                *[int(pid) for pid in cleanup.get("failed_pids", [])],
                *[int(pid) for pid in cleanup.get("residual_pids", [])],
                *[
                    int(pid)
                    for pid in cleanup.get("legacy_residual_pids", [])
                ],
            }
        ),
    }


def _restart_under_supervisor() -> None:
    """Request an external supervisor restart after verified quiescence.

    The production systemd unit is the only supported declarative owner of
    this strategy.  It deliberately does not inspect the restart command
    environment: a request can therefore never select a successor command or
    unit name.  Exit status 75 is non-success, so the unit's existing
    ``Restart=on-failure`` policy starts the same unit again.
    """

    try:
        quiescence_owner = managed_worker_quiescence_gate.begin_quiescence()
    except ManagedWorkerQuiescenceAlreadyActive:
        _LOGGER.info(
            "Supervisor-managed restart skipped; another restart attempt "
            "already owns worker quiescence."
        )
        return
    except Exception:
        _LOGGER.exception(
            "Supervisor-managed restart could not begin quiescence; keeping "
            "current process alive."
        )
        return
    try:
        quiescence = _quiesce_current_managed_workers()
        failed_pids = quiescence["failed_pids"]
    except Exception:
        managed_worker_quiescence_gate.reopen(quiescence_owner)
        _LOGGER.exception(
            "Supervisor-managed restart verification failed; keeping current "
            "process alive."
        )
        return
    if failed_pids:
        managed_worker_quiescence_gate.reopen(quiescence_owner)
        _LOGGER.error(
            "Supervisor-managed restart refused because worker residuals remain: %s",
            ", ".join(str(pid) for pid in failed_pids),
        )
        return
    _LOGGER.info(
        "Managed workers quiesced; requesting supervisor restart with exit code %s.",
        SUPERVISOR_RESTART_EXIT_CODE,
    )
    os._exit(SUPERVISOR_RESTART_EXIT_CODE)


def restart_process() -> None:
    # Unknown values intentionally retain the historical successor handoff so
    # existing launchers continue to work.  Only this fixed allowlisted value
    # selects supervisor ownership, and that path never consumes command env.
    if os.environ.get(RESTART_STRATEGY_ENV) == SUPERVISOR_RESTART_STRATEGY:
        _restart_under_supervisor()
        return

    command = get_configured_restart_command()
    if not command:
        resolved_host, resolved_port = resolve_restart_target()
        command = configure_restart_environment(resolved_host, resolved_port)
        _LOGGER.warning(
            "No managed restart command configured; using fallback command for %s:%s.",
            resolved_host,
            resolved_port,
        )

    restart_command = list(command)
    if "--wait-for-pid" not in restart_command:
        restart_command.extend(["--wait-for-pid", str(os.getpid())])

    handoff_log: BinaryIO | None = None
    spawned = False
    try:
        quiescence = _quiesce_current_managed_workers()
        if quiescence["failed_pids"]:
            _LOGGER.error(
                "Managed restart refused because worker residuals remain: %s",
                ", ".join(str(pid) for pid in quiescence["failed_pids"]),
            )
            return
        _LOGGER.info(
            "Spawning successor process for managed restart: %s",
            " ".join(restart_command),
        )
        handoff_log = _open_restart_handoff_log()
        subprocess.Popen(restart_command, **_restart_spawn_kwargs(handoff_log))
        spawned = True
    except Exception:
        _LOGGER.exception(
            "Managed restart handoff failed; keeping current process alive."
        )
    finally:
        if handoff_log is not None:
            try:
                handoff_log.close()
            except OSError:
                _LOGGER.exception("Could not close the parent restart handoff log.")

    if spawned:
        os._exit(0)


def schedule_restart(delay: float = 1.0) -> None:
    Timer(delay, restart_process).start()
