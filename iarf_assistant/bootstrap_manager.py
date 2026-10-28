from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import webbrowser
import zipfile
from pathlib import Path
from typing import Any

from filelock import Timeout as FileLockTimeout

from carlina.product_identity import (
    PRODUCT_IDENTITY,
    product_environment_value,
)
from carlina.profile_binding import validate_runtime_profile
from carlina.runtime_composition import resolve_runtime_composition
from carlina.runtime_profiles import RuntimeProfile, RuntimeProfileError
from iarf_assistant import bootstrap
from iarf_assistant.bootstrap_state import (
    ACTION_CREATE_ADMIN,
    ACTION_OPEN,
    ACTION_SETUP,
    ACTION_START,
    InstallState,
    detect_install_state,
    state_to_jsonable,
    write_manager_state,
)
from iarf_assistant.core.legacy_secret import (
    LegacySecretFileError,
    legacy_secret_file_environment,
    read_legacy_secret_file,
)
from iarf_assistant.core.runtime_ownership import (
    load_runtime_leader,
    runtime_leader_lock,
)

_SYSTEMD_UNIT_PATTERN = re.compile(r"^[A-Za-z0-9_@%:.-]+\.service$")
_SYSTEMD_SHOW_PROPERTIES = "Id,LoadState,ActiveState,SubState,MainPID"
# The packaged unit uses TimeoutStopSec=180s; allow a small verification margin
# so the updater does not race a legitimate, slow service shutdown.
_SYSTEMD_COMMAND_TIMEOUT_SECONDS = 200.0


class _SystemdStopError(RuntimeError):
    """A safe, user-facing failure from the systemd stop/verification gate."""


class _SystemdOwnershipInspectionError(RuntimeError):
    """The supervisor boundary for a Linux process could not be established."""


def _validate_user_systemd_unit(unit: str) -> str:
    if (
        not isinstance(unit, str)
        or not unit
        or len(unit) > 255
        or not _SYSTEMD_UNIT_PATTERN.fullmatch(unit)
        or unit.startswith("-")
        or "/" in unit
    ):
        raise _SystemdStopError(
            "the systemd unit must be a single user .service unit name"
        )
    return unit


def _run_systemctl_user(command: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_SYSTEMD_COMMAND_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        del exc
        raise _SystemdStopError(
            "systemctl did not complete before the timeout"
        ) from None
    except OSError as exc:
        del exc
        raise _SystemdStopError("systemctl could not be executed") from None
    if result.returncode != 0:
        raise _SystemdStopError(
            "systemctl refused the requested user service operation"
        )
    return result


def _parse_systemctl_show(text: str, unit: str) -> bool:
    values: dict[str, str] = {}
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if not separator or key in values:
            return False
        values[key] = value
    stopped_state = (values.get("ActiveState"), values.get("SubState"))
    return (
        values.get("Id") == unit
        and values.get("LoadState") == "loaded"
        and values.get("MainPID") == "0"
        and set(values)
        == {
            "Id",
            "LoadState",
            "ActiveState",
            "SubState",
            "MainPID",
        }
        and stopped_state in {("inactive", "dead"), ("failed", "failed")}
    )


def _verify_user_systemd_unit_stopped(unit: str) -> None:
    """Verify one explicit user service is loaded and fully inactive."""

    if os.name != "posix" or sys.platform != "linux":
        raise _SystemdStopError("user systemd supervision is supported only on Linux")
    selected_unit = _validate_user_systemd_unit(unit)
    result = _run_systemctl_user(
        [
            "systemctl",
            "--user",
            "show",
            f"--property={_SYSTEMD_SHOW_PROPERTIES}",
            "--",
            selected_unit,
        ]
    )
    if not _parse_systemctl_show(result.stdout or "", selected_unit):
        raise _SystemdStopError(
            "the requested user service could not be verified as loaded and stopped"
        )


def _stop_and_verify_user_systemd_unit(unit: str) -> None:
    """Stop one explicit user service and verify it is fully inactive.

    This intentionally uses only a positional argument array and never exposes
    systemctl's output: unit names and subprocess output are operator-facing
    input, not trusted diagnostic text.
    """

    if os.name != "posix" or sys.platform != "linux":
        raise _SystemdStopError("user systemd supervision is supported only on Linux")
    selected_unit = _validate_user_systemd_unit(unit)
    _run_systemctl_user(["systemctl", "--user", "stop", "--", selected_unit])
    _verify_user_systemd_unit_stopped(selected_unit)


def _is_systemd_user_manager_unit(unit: str) -> bool:
    """Return whether ``unit`` is systemd's numeric per-user manager unit."""

    return bool(re.fullmatch(r"user@\d+\.service", unit))


def _detect_systemd_service_owner(
    pid: int,
    *,
    proc_root: Path | str = "/proc",
) -> tuple[str, str] | None:
    """Return ``(unit, scope)`` for a Linux PID's child service, if any.

    ``scope`` is ``user`` or ``system``.  The per-user manager unit
    (``user@UID.service``) is deliberately skipped so a child application
    service is selected when both appear in the cgroup path.
    """

    if os.name != "posix" or sys.platform != "linux" or pid <= 0:
        return None
    try:
        cgroup_text = (Path(proc_root) / str(pid) / "cgroup").read_text(
            encoding="utf-8", errors="replace"
        )
    except OSError as exc:
        del exc
        raise _SystemdOwnershipInspectionError(
            "the managed process supervisor could not be inspected"
        ) from None

    saw_cgroup_record = False
    for raw_line in cgroup_text.splitlines():
        fields = raw_line.split(":", 2)
        if len(fields) != 3:
            continue
        saw_cgroup_record = True
        cgroup_path = fields[2].strip()
        if not cgroup_path or cgroup_path == "/":
            continue
        parts = [part for part in cgroup_path.split("/") if part]
        service_indexes = [
            index
            for index, part in enumerate(parts)
            if part.endswith(".service") and not _is_systemd_user_manager_unit(part)
        ]
        if not service_indexes:
            continue
        unit_index = service_indexes[-1]
        unit = parts[unit_index]
        user_manager_indexes = [
            index
            for index, part in enumerate(parts)
            if _is_systemd_user_manager_unit(part)
        ]
        is_user_service = any(index < unit_index for index in user_manager_indexes)
        scope = "user" if is_user_service else "system"
        if "system.slice" in parts:
            scope = "system"
        return unit, scope
    if not saw_cgroup_record:
        raise _SystemdOwnershipInspectionError(
            "the managed process supervisor record was malformed"
        )
    return None


def _active_paths():
    return bootstrap.load_bootstrap_paths()


def _pid_file(paths=None) -> Path:
    active = paths or _active_paths()
    return active.state_dir / "bootstrap" / "server.pid"


def _verified_managed_server_identity(
    paths=None,
) -> bootstrap.ManagedProcessIdentity | None:
    """Return a process identity only when every ownership signal agrees."""

    active = paths or _active_paths()
    pid_path = _pid_file(active)
    if not pid_path.is_file():
        return None
    try:
        pid = int(pid_path.read_text(encoding="utf-8").strip())
    except OSError, ValueError:
        return None
    if pid <= 0 or not bootstrap._pid_is_running(pid):
        return None

    identity = bootstrap.process_identity(pid)
    if identity is None:
        return None

    leader = load_runtime_leader(active)
    if not leader:
        return None
    token = leader.get("token")
    try:
        leader_pid = int(leader.get("pid") or 0)
    except TypeError, ValueError:
        return None
    if (
        leader.get("format") != "carlina.runtime-leader"
        or leader.get("version") != 1
        or not isinstance(token, str)
        or not token.strip()
        or leader_pid != pid
    ):
        return None
    leader_create_time = leader.get("process_create_time")
    if leader_create_time is not None:
        try:
            if float(leader_create_time) != identity.create_time:
                return None
        except TypeError, ValueError:
            return None
    else:
        # Releases written before process_create_time existed remain stoppable,
        # relying on the held data-root lease. New releases use the stronger
        # durable PID-plus-creation-time check above.
        pass

    lock = runtime_leader_lock(active, timeout=0)
    try:
        lock.acquire()
    except FileLockTimeout:
        return identity
    except OSError:
        return None
    else:
        lock.release()
        return None


def _verified_managed_server_pid(paths=None) -> int | None:
    """Return the verified manager PID for compatibility with command callers."""

    identity = _verified_managed_server_identity(paths)
    return identity.pid if identity is not None else None


def _managed_server_running(paths=None) -> bool:
    return _verified_managed_server_pid(paths) is not None


def _print_header(state: InstallState) -> None:
    heading = f"{PRODUCT_IDENTITY.name} Manager"
    print(f"\n{heading}")
    print("=" * len(heading))
    print(f"Status: {state.summary}")
    print(f"URL:    {state.url}")
    print("\nChecklist:")
    for check in state.checks:
        marker = "OK" if check.ok else ("WARN" if not check.required else "FIX")
        print(f"  [{marker:4}] {check.label}: {check.detail}")
    if state.warnings:
        print("\nWarnings:")
        for warning in state.warnings:
            print(f"  - {warning}")


def _recommended_label(action: str) -> str:
    return {
        ACTION_OPEN: f"Open {PRODUCT_IDENTITY.name} in your browser",
        ACTION_SETUP: "Run setup or repair",
        ACTION_CREATE_ADMIN: "Create the first admin user",
        ACTION_START: f"Start {PRODUCT_IDENTITY.name}",
    }.get(action, "Manage installation")


def _prompt_choice(max_choice: int) -> int:
    while True:
        choice = input("Choose an option: ").strip()
        if choice.isdigit() and 1 <= int(choice) <= max_choice:
            return int(choice)
        print(f"Enter a number from 1 to {max_choice}.")


def _write_state_event(
    event: str, state: InstallState | None = None, **extra: object
) -> None:
    payload = {"last_event": event, "python_executable": sys.executable}
    payload.update(extra)
    if state is not None:
        payload["last_detected_state"] = state_to_jsonable(state)
    write_manager_state(payload, paths=bootstrap.ACTIVE_PATHS)


def open_browser(url: str) -> int:
    print(f"Opening {url}")
    webbrowser.open(url)
    _write_state_event("open_browser", last_url=url)
    return 0


def run_setup(
    interactive: bool = True,
    *,
    admin_email: str | None = None,
    admin_password: str | None = None,
    repair: bool = False,
) -> int:
    args: list[str] = [] if interactive else ["--no-admin-prompt"]
    if repair:
        args.append("--repair")
    if admin_email or admin_password:
        args.extend(
            [
                "--admin-email",
                admin_email or "",
                "--admin-password",
                admin_password or "",
            ]
        )
    result = bootstrap.setup_main(args)
    if result == 0:
        _write_state_event("setup_complete")
    return result


def create_admin_user() -> int:
    email = input("Admin email: ").strip()
    while not email:
        email = input("Admin email: ").strip()
    password = getpass.getpass("Admin password: ")
    confirm = getpass.getpass("Confirm admin password: ")
    if password != confirm:
        print("Passwords did not match.")
        return 1
    if not password:
        print("Password cannot be empty.")
        return 1

    command = [sys.executable, "-m", "flask", "create-user", email, password, "--admin"]
    display = [sys.executable, "-m", "flask", "create-user", email, "***", "--admin"]
    result = bootstrap.run_command(
        command, env=bootstrap.flask_env(), display_command=display
    )
    _write_state_event("admin_created", admin_email=email)
    return result.returncode


def start_app(
    host: str,
    port: int,
    open_browser_flag: bool,
    *,
    restart: bool = False,
    runtime_role: str | None = None,
    legacy_secret_file: str | None = None,
) -> int:
    try:
        with legacy_secret_file_environment(legacy_secret_file):
            _active_paths()
            _write_state_event("start_requested", host=host, port=port)
            args = ["--host", host, "--port", str(port)]
            if open_browser_flag:
                args.append("--open-browser")
            if restart:
                args.append("--restart")
            if runtime_role is not None:
                args.extend(["--runtime-role", runtime_role])
            return bootstrap.start_main(args)
    except LegacySecretFileError as exc:
        print(f"Start refused: {exc}")
        return 2


def _terminate_managed_server_process(
    pid: int,
    *,
    expected_identity: bootstrap.ManagedProcessIdentity | None = None,
) -> dict[str, object]:
    """Stop a verified managed server without leaving worker descendants.

    ``os.kill(..., SIGTERM)`` maps to ``TerminateProcess`` on Windows. That is
    an abrupt, single-process termination, so Python cleanup handlers do not
    run and managed workers become orphans. ``taskkill /T`` applies the same
    verified PID boundary to the complete descendant tree. POSIX uses the
    a creation-time-verified, child-first snapshot before the scoped orphan
    fallback handles any process born during shutdown.
    """

    if os.name == "nt":
        identity = expected_identity or bootstrap.process_identity(pid)
        if identity is None or not bootstrap._taskkill_process_tree(identity):
            raise OSError(
                "taskkill could not terminate the verified managed process tree."
            )
        return {
            "terminated_pids": [],
            "failed_pids": [],
            "manager_terminated": True,
        }

    result = bootstrap.terminate_managed_process_tree(
        pid,
        expected_identity=expected_identity,
        term_timeout_seconds=5.0,
        kill_timeout_seconds=1.0,
    )
    if not result.get("manager_terminated"):
        raise OSError("the managed process did not terminate within the timeout")
    return result


def stop_managed_server(paths=None, *, verified_pid: int | None = None) -> int:
    active = paths or _active_paths()
    current_identity = _verified_managed_server_identity(active)
    if current_identity is not None and (
        verified_pid is not None and current_identity.pid != verified_pid
    ):
        print("Managed server ownership changed; refusing to signal any process.")
        return 1
    if current_identity is None:
        cleanup = bootstrap.cleanup_orphan_worker_processes(
            project_root=active.program_root,
            instance_dir=active.instance_dir,
            state_dir=active.state_dir,
            data_root=active.data_root,
            installed_addons_dir=active.installed_addons_dir,
            addon_environments_dir=active.addon_environments_dir,
            skip_if_managed_server_active=False,
        )
        inspection_error = str(cleanup.get("inspection_error") or "").strip()
        if inspection_error:
            print(
                "No verified managed server ownership was found, and owned "
                f"worker processes could not be inspected: {inspection_error}."
            )
            return 1
        residual_pids = [int(item) for item in cleanup.get("residual_pids", [])]
        legacy_residual_pids = [
            int(item) for item in cleanup.get("legacy_residual_pids", [])
        ]
        failed_pids = [int(item) for item in cleanup.get("failed_pids", [])]
        if failed_pids or residual_pids or legacy_residual_pids:
            report = sorted(
                set(failed_pids) | set(residual_pids) | set(legacy_residual_pids)
            )
            print(
                "No verified managed server ownership was found, and owned "
                f"worker residuals remain: {', '.join(str(pid) for pid in report)}."
            )
            return 1
        terminated_pids = [int(item) for item in cleanup.get("terminated_pids", [])]
        if terminated_pids:
            print(
                "No verified managed server was running; stopped owned residual "
                f"process(es): {', '.join(str(pid) for pid in terminated_pids)}."
            )
            return 0
        else:
            print(
                "No verified managed server ownership was found; "
                "refusing to signal any process."
            )
        return 1

    pid = current_identity.pid
    if pid == os.getpid():
        print("Cannot stop the current manager process from inside itself.")
        return 1
    try:
        tree_result = _terminate_managed_server_process(
            pid,
            expected_identity=current_identity,
        )
    except OSError as exc:
        print(f"Could not stop process {pid}: {exc}")
        return 1

    cleanup = bootstrap.cleanup_orphan_worker_processes(
        project_root=active.program_root,
        instance_dir=active.instance_dir,
        state_dir=active.state_dir,
        data_root=active.data_root,
        installed_addons_dir=active.installed_addons_dir,
        addon_environments_dir=active.addon_environments_dir,
        skip_if_managed_server_active=False,
    )
    terminated_workers = sorted(
        {
            *[int(item) for item in tree_result.get("terminated_pids", [])],
            *[int(item) for item in cleanup.get("terminated_pids", [])],
        }
    )
    failed_workers = sorted(
        {
            *[int(item) for item in tree_result.get("failed_pids", [])],
            *[int(item) for item in cleanup.get("failed_pids", [])],
        }
    )
    residual_workers = [int(item) for item in cleanup.get("residual_pids", [])]
    legacy_residual_workers = [
        int(item) for item in cleanup.get("legacy_residual_pids", [])
    ]
    inspection_error = str(cleanup.get("inspection_error") or "").strip()
    _pid_file(active).unlink(missing_ok=True)
    _write_state_event(
        "stop_complete",
        stopped_pid=pid,
        stopped_worker_pids=terminated_workers,
        failed_worker_pids=failed_workers,
        residual_worker_pids=residual_workers,
    )
    if (
        failed_workers
        or residual_workers
        or legacy_residual_workers
        or inspection_error
    ):
        report = sorted(
            set(failed_workers) | set(residual_workers) | set(legacy_residual_workers)
        )
        details = (
            f"{', '.join(str(item) for item in report)}."
            if report
            else "none reported."
        )
        print(
            f"Stopped process {pid}, but owned managed worker residuals remain: "
            f"{details}"
        )
        if inspection_error:
            print(f"Managed worker post-stop inspection failed: {inspection_error}.")
        return 1
    suffix = (
        f" and {len(terminated_workers)} orphaned worker process(es)"
        if terminated_workers
        else ""
    )
    print(f"Stopped process {pid}{suffix}.")
    return 0


def _cleanup_owned_workers_for_distribution(
    paths: Any,
    *,
    operation: str,
) -> bool:
    """Run the existing owned-worker cleanup gate before release dispatch."""

    cleanup = bootstrap.cleanup_orphan_worker_processes(
        project_root=paths.program_root,
        instance_dir=paths.instance_dir,
        state_dir=paths.state_dir,
        data_root=paths.data_root,
        installed_addons_dir=paths.installed_addons_dir,
        addon_environments_dir=paths.addon_environments_dir,
        skip_if_managed_server_active=False,
    )
    failed = sorted(
        {
            *[int(item) for item in cleanup.get("failed_pids", [])],
            *[int(item) for item in cleanup.get("residual_pids", [])],
            *[int(item) for item in cleanup.get("legacy_residual_pids", [])],
        }
    )
    inspection_error = str(cleanup.get("inspection_error") or "").strip()
    if not failed and not inspection_error:
        return True
    print(
        f"{operation} refused because owned worker cleanup was not clean: "
        f"{', '.join(str(pid) for pid in failed) if failed else 'none reported'}"
        f"{f'; inspection failed: {inspection_error}' if inspection_error else ''}."
    )
    return False


def rebuild_assets() -> int:
    try:
        bootstrap.build_tailwind()
        bootstrap.build_frontend_assets(force=True)
        _write_state_event("frontend_built")
        return 0
    except bootstrap.BootstrapError as exc:
        print(f"Asset build failed: {exc}")
        return 1


def run_tests() -> int:
    result = bootstrap.run_command(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests"]
    )
    _write_state_event("tests_run", returncode=result.returncode)
    return result.returncode


def show_log(path: Path, lines: int = 80) -> None:
    if not path.exists():
        print(f"No log file yet: {path}")
        return
    text_lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    print(f"\nLast {min(lines, len(text_lines))} lines of {path}:")
    print("-" * 72)
    for line in text_lines[-lines:]:
        print(line)


def show_logs_menu() -> None:
    show_log(bootstrap.SETUP_LOG)
    show_log(bootstrap.START_LOG)


def run_recommended_action(
    state: InstallState,
    *,
    host: str,
    port: int,
    open_browser_flag: bool,
    interactive: bool,
) -> int:
    action = state.recommended_action
    if action == ACTION_OPEN:
        return open_browser(state.url)
    if action == ACTION_START:
        return start_app(host, port, open_browser_flag)
    if action == ACTION_CREATE_ADMIN:
        if not interactive:
            print("Admin creation needs interactive input. Re-run without --no-ui.")
            return 2
        return create_admin_user()
    if action == ACTION_SETUP:
        return run_setup(interactive=interactive)
    return 0


def _wait_for_pid_to_exit(
    pid: int | None, *, timeout_seconds: float = 20.0, poll_interval: float = 0.2
) -> bool:
    if not pid or pid <= 0:
        return True

    deadline = time.monotonic() + max(timeout_seconds, poll_interval)
    while time.monotonic() < deadline:
        if not bootstrap._pid_is_running(pid):
            return True
        time.sleep(poll_interval)
    return not bootstrap._pid_is_running(pid)


def menu_loop(host: str, port: int, open_browser_flag: bool) -> int:
    while True:
        state = detect_install_state(
            host=host,
            port=port,
            paths=bootstrap.ACTIVE_PATHS,
        )
        _write_state_event("state_checked", state)
        _print_header(state)
        print(f"\nRecommended: {_recommended_label(state.recommended_action)}")
        options = [
            _recommended_label(state.recommended_action),
            "Run setup or repair",
            f"Start {PRODUCT_IDENTITY.name}",
            "Open browser",
            "Create admin user",
            "Rebuild frontend assets",
            "Run tests",
            "View setup/start logs",
            "Stop managed server",
            "Refresh status",
            "Exit",
        ]
        print("\nOptions:")
        for index, label in enumerate(options, start=1):
            print(f"  {index}. {label}")

        choice = _prompt_choice(len(options))
        if choice == 1:
            result = run_recommended_action(
                state,
                host=host,
                port=port,
                open_browser_flag=open_browser_flag,
                interactive=True,
            )
            if result != 0:
                return result
        elif choice == 2:
            result = run_setup(interactive=True)
            if result != 0:
                return result
        elif choice == 3:
            return start_app(host, port, open_browser_flag)
        elif choice == 4:
            open_browser(state.url)
        elif choice == 5:
            result = create_admin_user()
            if result != 0:
                return result
        elif choice == 6:
            result = rebuild_assets()
            if result != 0:
                return result
        elif choice == 7:
            result = run_tests()
            if result != 0:
                return result
        elif choice == 8:
            show_logs_menu()
            input("\nPress Enter to continue...")
        elif choice == 9:
            stop_managed_server()
        elif choice == 10:
            continue
        elif choice == 11:
            return 0


_COMMAND_NAMES = {
    "addon",
    "backup",
    "doctor",
    "init",
    "install",
    "manage",
    "migrate-data",
    "migrate-addon-data",
    "migrate-encryption",
    "restore",
    "rollback",
    "start",
    "status",
    "stop",
    "storage",
    "uninstall",
    "update",
}


def _json_default(value: object) -> str:
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"Cannot serialize {type(value).__name__}.")


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, default=_json_default))


def _doctor_command(args: argparse.Namespace) -> int:
    from iarf_assistant.core.doctor import format_doctor_human, run_doctor

    paths = _active_paths()
    report = run_doctor(paths, deep=args.deep)
    if args.json:
        _print_json(report.to_dict())
    else:
        print(format_doctor_human(report))
    return report.exit_code(strict=args.strict)


def _addon_validate_command(args: argparse.Namespace) -> int:
    """Compile an Add-on API contract without importing package code."""

    from iarf_assistant.core.addons.contracts import validate_addon_contract
    from iarf_assistant.core.addons.installer import (
        extract_addon_archive,
        validate_addon_archive,
    )

    package_path = Path(args.path).expanduser().resolve()
    temporary: tempfile.TemporaryDirectory[str] | None = None
    try:
        if package_path.is_dir():
            addon_dir = package_path
            manifest_path = addon_dir / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        elif package_path.is_file() and package_path.suffix.casefold() == ".zip":
            temporary = tempfile.TemporaryDirectory(prefix="carlina-addon-validate-")
            addon_dir = Path(temporary.name) / "code"
            with zipfile.ZipFile(package_path, "r") as archive:
                validated_archive = validate_addon_archive(archive)
                extract_addon_archive(
                    archive,
                    addon_dir,
                    validated_archive.members,
                )
                manifest = validated_archive.manifest
        elif package_path.is_file() and package_path.name == "manifest.json":
            addon_dir = package_path.parent
            manifest = json.loads(package_path.read_text(encoding="utf-8"))
        else:
            raise FileNotFoundError(
                "PATH must be an add-on directory, manifest.json, or ZIP package."
            )
        result = validate_addon_contract(
            manifest,
            addon_dir=addon_dir,
            source_kind=args.source_kind,
            strict=args.source_kind == "installed",
            provenance={"validation_source": str(package_path)},
        )
        payload = result.to_dict(include_manifest=args.include_manifest)
        if args.json:
            _print_json(payload)
        else:
            label = "VALID" if result.valid else "INVALID"
            print(f"{label}: {package_path}")
            if result.contract is not None:
                print(
                    f"{result.contract.addon_id} {result.contract.version} · "
                    f"{result.contract.trust_tier.value}"
                )
                print(f"Contract SHA-256: {result.contract.contract_sha256}")
            for diagnostic in result.diagnostics:
                location = f" ({diagnostic.path})" if diagnostic.path else ""
                print(
                    f"{diagnostic.severity.upper()} {diagnostic.code}{location}: "
                    f"{diagnostic.message}"
                )
        return 0 if result.valid else 2
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        if args.json:
            _print_json({"valid": False, "error": str(exc), "diagnostics": []})
        else:
            print(f"INVALID: {package_path}")
            print(f"ERROR package.unreadable: {exc}")
        return 2
    finally:
        if temporary is not None:
            temporary.cleanup()


def _addon_private_overlay_takeover_command(args: argparse.Namespace) -> int:
    """Run the narrow stopped-instance private built-in takeover command."""

    from iarf_assistant.core.addons.private_overlays import (
        PrivateOverlayTakeoverError,
        PrivateOverlayTakeoverPartialError,
        takeover_private_addon_overlays,
        takeover_result_payload,
    )

    try:
        outcomes = takeover_private_addon_overlays(
            args.index,
            args.addon_ids,
            actor_user_id=args.actor_user_id,
            approve_private_host_code=args.approve_private_host_code,
            approve_private_takeover=args.approve_private_takeover,
            reviewed_notice_ids=args.reviewed_notice_ids,
        )
    except PrivateOverlayTakeoverPartialError as exc:
        _print_json(
            takeover_result_payload(
                success=False,
                state="partial_failure",
                outcomes=exc.results,
            )
        )
        return 1
    except PrivateOverlayTakeoverError:
        _print_json(
            takeover_result_payload(success=False, state="refused", outcomes=())
        )
        return 1
    _print_json(
        takeover_result_payload(success=True, state="completed", outcomes=outcomes)
    )
    return 0


def _managed_installation_layout():
    from iarf_assistant.core.managed_install import ManagedInstallationLayout

    return ManagedInstallationLayout.resolve(
        product_environment_value("PROGRAM_ROOT", default=None)
    )


def _status_command(args: argparse.Namespace) -> int:
    from iarf_assistant.bootstrap_state import app_url, is_runtime_running
    from iarf_assistant.core.doctor import run_doctor

    paths = _active_paths()
    url = app_url(args.host, args.port)
    running = is_runtime_running(paths, host=args.host, port=args.port)
    report = run_doctor(paths, deep=False)
    managed_layout = _managed_installation_layout()
    try:
        managed_version = managed_layout.current_version()
    except RuntimeError:
        managed_version = None
    payload = {
        "product": PRODUCT_IDENTITY.name,
        "application_profile": resolve_runtime_composition().profile.value,
        "running": running,
        "managed_process": _managed_server_running(paths),
        "url": url,
        "layout": report.layout,
        "health": report.to_dict()["summary"],
        "managed_install": {
            "program_root": str(managed_layout.root),
            "active_version": managed_version,
            "journal": str(managed_layout.journal_file)
            if managed_layout.journal_file.is_file()
            else None,
        },
    }
    if args.json:
        _print_json(payload)
    else:
        print(f"{PRODUCT_IDENTITY.name}: {'running' if running else 'stopped'}")
        print(f"URL: {url}")
        print(f"Layout: {paths.mode} ({paths.data_root})")
        print(
            f"Health: {len(report.errors)} error(s), {len(report.warnings)} warning(s)"
        )
        if managed_version:
            print(f"Managed release: {managed_version} ({managed_layout.root})")
    return report.exit_code()


def _backup_command(args: argparse.Namespace) -> int:
    from iarf_assistant.core.backup_service import BackupError, create_backup
    from iarf_assistant.core.encryption import InstallationKeyStore

    paths = _active_paths()
    if not paths.database_file.is_file():
        print(f"Backup failed: database is missing: {paths.database_file}")
        return 1
    try:
        record = create_backup(
            paths,
            output_path=args.output,
            key_store=InstallationKeyStore(paths.master_keys_file),
        )
    except (BackupError, OSError, ValueError) as exc:
        print(f"Backup failed: {exc}")
        return 1
    payload = {
        "backup_id": record.backup_id,
        "path": str(record.path),
        "sha256": record.sha256,
        "file_count": record.file_count,
        "expanded_bytes": record.expanded_bytes,
        "required_key_ids": list(record.required_key_ids),
    }
    if args.json:
        _print_json(payload)
    else:
        print(f"Created verified backup: {record.path}")
        print(f"SHA-256: {record.sha256}")
    return 0


def _restore_command(args: argparse.Namespace) -> int:
    from iarf_assistant.core.backup_service import BackupError
    from iarf_assistant.core.encryption import InstallationKeyStore
    from iarf_assistant.core.restore_service import (
        apply_pending_restore,
        cancel_pending_restore,
        schedule_restore,
    )

    paths = _active_paths()
    if args.apply_pending:
        if _managed_server_running(paths):
            print("Restore refused: stop the managed Carlina server first.")
            return 1
        try:
            result = apply_pending_restore(
                paths,
                key_store=InstallationKeyStore(paths.master_keys_file),
            )
        except (BackupError, OSError, ValueError) as exc:
            print(f"Restore failed: {exc}")
            return 1
        if result is None:
            print("No restore is pending.")
            return 0
        print(
            f"Activated backup {result.pending.archive_filename} at "
            f"{result.restore.destination_root}."
        )
        if result.restore.safety_root:
            print(f"Previous target retained at: {result.restore.safety_root}")
        return 0
    if args.cancel_pending:
        try:
            archived = cancel_pending_restore(paths)
        except (BackupError, OSError, ValueError) as exc:
            print(f"Restore cancellation failed: {exc}")
            return 1
        print(
            f"Pending restore archived at: {archived}"
            if archived
            else "No restore is pending."
        )
        return 0
    if not args.archive:
        print("Restore scheduling requires ARCHIVE or a pending-restore action.")
        return 2
    archive = Path(args.archive).expanduser().resolve()
    if args.confirm != archive.name:
        print(f"Restore confirmation must exactly equal: {archive.name}")
        return 2
    try:
        scheduled = schedule_restore(
            paths,
            archive,
            key_store=InstallationKeyStore(paths.master_keys_file),
        )
    except (BackupError, OSError, ValueError) as exc:
        print(f"Restore scheduling failed: {exc}")
        return 1
    print(f"Verified and scheduled backup: {scheduled.pending.archive_filename}")
    print(f"Safety backup: {scheduled.safety_backup.path}")
    print("Stop and restart Carlina, or run: carlina restore --apply-pending")
    return 0


def _print_migration_plan(plan: Any) -> None:
    print(f"Migration plan: {plan.plan_id}")
    print(f"Source database: {plan.source_database}")
    print(f"Target database: {plan.target_database}")
    print(
        f"Payload: {plan.payload_bytes} bytes in {len(plan.files)} file(s) plus SQLite"
    )
    print(f"Minimum free space: {plan.minimum_free_bytes} bytes")
    print("Database candidates:")
    for candidate in plan.database_candidates:
        marker = "selected" if candidate.selected else "candidate"
        viable = "SQLite" if candidate.sqlite_header else "not viable"
        print(f"  - [{marker}; {viable}] {candidate.path} ({candidate.size} bytes)")
    for notice in plan.notices:
        level = "BLOCK" if notice.blocking else "NOTE"
        print(f"{level} {notice.code}: {notice.message}")
        for path in notice.paths:
            print(f"    {path}")
    if plan.applicable:
        print(f"Confirmation token: {plan.confirmation_token}")


def _migrate_data_command(args: argparse.Namespace) -> int:
    from iarf_assistant.core.backup_service import BackupError
    from iarf_assistant.core.data_migration import (
        DataMigrationError,
        apply_data_migration,
        build_data_migration_plan,
        load_data_migration_plan,
        write_data_migration_plan,
    )
    from iarf_assistant.core.encryption import InstallationKeyStore

    paths = _active_paths()
    if args.apply_plan:
        if _managed_server_running(paths):
            print("Data migration refused: stop the managed Carlina server first.")
            return 1
        try:
            plan = load_data_migration_plan(Path(args.apply_plan))
            result = apply_data_migration(
                plan,
                paths,
                confirmation=args.confirm or "",
                create_safety_backup=not args.no_safety_backup,
                key_store=InstallationKeyStore(paths.master_keys_file),
            )
        except (DataMigrationError, BackupError, OSError, ValueError) as exc:
            print(f"Data migration failed: {exc}")
            return 1
        print(f"Migration complete: {result.target_database}")
        print(f"Journal: {result.journal_path}")
        print(f"Legacy source remains unchanged: {plan.source_database}")
        return 0

    try:
        plan = build_data_migration_plan(
            paths,
            source_database=args.database,
            target_root=args.target,
        )
        if args.plan_out:
            write_data_migration_plan(plan, Path(args.plan_out))
    except (DataMigrationError, OSError, ValueError) as exc:
        print(f"Could not build migration plan: {exc}")
        return 1
    if args.json:
        _print_json(plan.to_dict())
    else:
        _print_migration_plan(plan)
        if args.plan_out:
            print(f"Plan written to: {Path(args.plan_out).resolve()}")
    return 0 if plan.applicable else 2


def _migration_legacy_system_secret_candidates(
    args: argparse.Namespace,
) -> tuple[str, ...]:
    """Load migration-only secrets without changing the process environment."""

    raw_option = getattr(args, "legacy_secret_file", ()) or ()
    raw_paths = (raw_option,) if isinstance(raw_option, str) else tuple(raw_option)
    if "FLASK_SECRET_KEY" in os.environ and raw_paths:
        raise LegacySecretFileError(
            "FLASK_SECRET_KEY and legacy secret files cannot be combined."
        )
    normalized_paths: set[str] = set()
    for raw_path in raw_paths:
        candidate = os.path.normcase(os.path.abspath(os.path.expanduser(raw_path)))
        if candidate in normalized_paths:
            raise LegacySecretFileError("Legacy secret file paths are duplicated.")
        normalized_paths.add(candidate)
    candidates = tuple(read_legacy_secret_file(path) for path in raw_paths)
    if len(set(candidates)) != len(candidates):
        raise LegacySecretFileError("Legacy secret file values are duplicated.")
    if candidates:
        return candidates
    environment_secret = os.environ.get("FLASK_SECRET_KEY")
    return (environment_secret,) if environment_secret else ()


def _plan_requires_legacy_system_secret(plan) -> bool:
    return any(
        column.legacy_system_count
        or (column.spec.strategy == "system" and column.legacy_unprefixed_count)
        for column in plan.columns
    )


def _collect_encryption_migration_passwords(plan) -> dict[int, str] | None:
    if plan.required_user_ids and not sys.stdin.isatty():
        print(
            "Encryption migration requires interactive password entry for "
            "legacy user-key rows. Run it from a private terminal."
        )
        return None
    return {
        user_id: getpass.getpass(f"Password for legacy user {user_id}: ")
        for user_id in plan.required_user_ids
    }


def _migrate_encryption_command(args: argparse.Namespace) -> int:
    from carlina.profile_binding import read_runtime_profile
    from carlina.runtime_profiles import ContentPolicy, RuntimeProfileError
    from iarf_assistant.core.encryption import EncryptionError, InstallationKeyStore
    from iarf_assistant.core.encryption_migration import (
        MAX_LEGACY_SYSTEM_QUARANTINE_CELLS,
        EncryptionMigrationError,
        apply_encryption_migration,
        build_encryption_migration_plan,
        discover_encrypted_model_contract,
        load_encryption_migration_plan,
        verify_encryption_migration,
        write_encryption_migration_plan,
    )

    apply_plan = getattr(args, "apply_plan", None)
    verify_plan = getattr(args, "verify_plan", None)
    assigned_user_id = getattr(args, "assign_unresolved_user", None)
    approved_assignment = bool(
        getattr(args, "approve_unresolved_user_ownership", False)
    )
    expected_system_quarantine_count = getattr(
        args, "expected_system_quarantine_count", None
    )
    approved_system_quarantine = bool(getattr(args, "approve_system_quarantine", False))
    if (assigned_user_id is None) == approved_assignment:
        print(
            "Encryption migration refused: unresolved-owner assignment requires "
            "both --assign-unresolved-user and "
            "--approve-unresolved-user-ownership."
        )
        return 2
    if apply_plan and (assigned_user_id is not None or approved_assignment):
        print(
            "Encryption migration refused: unresolved-owner assignment options "
            "are planning-only and cannot be used with --apply-plan."
        )
        return 2
    if (expected_system_quarantine_count is None) == approved_system_quarantine:
        print(
            "Encryption migration refused: system quarantine requires both "
            "--expected-system-quarantine-count and --approve-system-quarantine."
        )
        return 2
    if expected_system_quarantine_count is not None and (
        isinstance(expected_system_quarantine_count, bool)
        or not isinstance(expected_system_quarantine_count, int)
        or not 1
        <= expected_system_quarantine_count
        <= MAX_LEGACY_SYSTEM_QUARANTINE_CELLS
    ):
        print(
            "Encryption migration refused: expected system quarantine count must be "
            "positive and within the supported safety bound."
        )
        return 2
    if (apply_plan or verify_plan) and (
        expected_system_quarantine_count is not None or approved_system_quarantine
    ):
        print(
            "Encryption migration refused: system quarantine attestation options "
            "are planning-only."
        )
        return 2
    if verify_plan and (
        getattr(args, "plan_out", None)
        or getattr(args, "initialize_key", False)
        or assigned_user_id is not None
        or approved_assignment
        or expected_system_quarantine_count is not None
        or approved_system_quarantine
    ):
        print(
            "Encryption migration refused: --verify-plan cannot be combined with "
            "planning or initialization options."
        )
        return 2
    try:
        legacy_system_secrets = _migration_legacy_system_secret_candidates(args)
    except LegacySecretFileError as exc:
        print(f"Encryption migration refused: {exc}")
        return 2
    if approved_system_quarantine and not getattr(args, "legacy_secret_file", None):
        print(
            "Encryption migration refused: system quarantine requires legacy "
            "secret file candidates."
        )
        return 2

    paths = _active_paths()
    try:
        binding = read_runtime_profile(paths.data_root)
    except RuntimeProfileError as exc:
        print(f"Encryption migration refused: {exc}")
        return 1
    if binding is not None and binding.content_policy is ContentPolicy.PLAINTEXT_V1:
        print("Encryption migration refused: ordinary content policy is plaintext-v1.")
        return 2
    key_store = InstallationKeyStore(paths.master_keys_file)
    if args.initialize_key:
        try:
            initialized_key_id = key_store.initialize()
        except (EncryptionError, OSError, ValueError) as exc:
            print(f"Could not initialize the installation key: {exc}")
            return 1
        print(f"Installation key ready: {initialized_key_id}")
    if apply_plan or verify_plan:
        if _managed_server_running(paths):
            print("Encryption migration refused: stop the managed server first.")
            return 1
        plan_path = apply_plan if apply_plan is not None else verify_plan
        if not isinstance(plan_path, (str, os.PathLike)):
            print("Encryption migration refused: a plan path is required.")
            return 2
        try:
            plan = load_encryption_migration_plan(Path(plan_path))
        except (EncryptionError, EncryptionMigrationError, OSError, ValueError) as exc:
            print(f"Could not load encryption plan: {exc}")
            return 1
        if _plan_requires_legacy_system_secret(plan) and not legacy_system_secrets:
            print(
                "Encryption migration refused: a legacy system secret candidate is "
                "required for this plan."
            )
            return 2
        passwords = _collect_encryption_migration_passwords(plan)
        if passwords is None:
            return 2
        if verify_plan:
            try:
                verification = verify_encryption_migration(
                    plan,
                    paths,
                    key_store,
                    user_passwords=passwords,
                    legacy_system_secrets=legacy_system_secrets,
                    batch_size=args.batch_size,
                )
            except (EncryptionMigrationError, OSError, ValueError) as exc:
                print(f"Encryption verification failed: {exc}")
                return 1
            if args.json:
                _print_json(verification.to_dict())
            else:
                quarantined_values = getattr(verification, "quarantined_values", 0)
                print(
                    "Encryption verification complete: "
                    f"{verification.verified_legacy_values} legacy value(s)."
                )
                print(f"Quarantined legacy system values: {quarantined_values}.")
                print(
                    "Users requiring password unlock: "
                    + (", ".join(map(str, verification.required_user_ids)) or "none")
                )
                for index, matched in enumerate(
                    verification.system_candidate_matches,
                    start=1,
                ):
                    print(f"System candidate {index}: {matched} value(s).")
            return 0
        try:
            result = apply_encryption_migration(
                plan,
                paths,
                key_store,
                confirmation=args.confirm or "",
                user_passwords=passwords,
                legacy_system_secrets=legacy_system_secrets,
                batch_size=args.batch_size,
                create_safety_backup=not args.no_safety_backup,
            )
        except (EncryptionMigrationError, OSError, ValueError) as exc:
            print(f"Encryption migration failed: {exc}")
            return 1
        if args.json:
            _print_json(
                {
                    "plan_id": result.plan_id,
                    "migrated_values": result.migrated_values,
                    "quarantined_values": result.quarantined_values,
                    "journal": str(result.journal_path),
                    "quarantine_ledger": (
                        str(result.quarantine_ledger)
                        if result.quarantine_ledger is not None
                        else None
                    ),
                }
            )
        else:
            print(f"Encryption migration complete: {result.migrated_values} value(s).")
            print(f"Quarantined legacy system values: {result.quarantined_values}.")
            print(f"Journal: {result.journal_path}")
            if result.quarantine_ledger is not None:
                print(f"Quarantine recovery ledger: {result.quarantine_ledger}")
        return 0

    try:
        specs, failures = discover_encrypted_model_contract(paths.package_root)
        plan = build_encryption_migration_plan(
            paths.database_file,
            specs,
            key_store,
            legacy_system_secret_available=bool(legacy_system_secrets),
            legacy_system_secrets=legacy_system_secrets,
            discovery_failures=failures,
            assign_unresolved_user_id=assigned_user_id,
            approve_unresolved_user_ownership=approved_assignment,
            expected_system_quarantine_count=expected_system_quarantine_count,
            approve_system_quarantine=approved_system_quarantine,
        )
        if args.plan_out:
            write_encryption_migration_plan(plan, Path(args.plan_out))
    except (EncryptionError, EncryptionMigrationError, OSError, ValueError) as exc:
        print(f"Could not build encryption migration plan: {exc}")
        if not key_store.exists:
            print(
                "Initialize the installation first with 'carlina init', or "
                "rerun this command with --initialize-key."
            )
        return 1
    if args.json:
        _print_json(plan.to_dict())
    else:
        print(f"Encryption migration plan: {plan.plan_id}")
        print(f"Database: {plan.database}")
        print(f"Declared encrypted columns: {len(plan.columns)}")
        print(f"Legacy values: {plan.legacy_value_count}")
        print(
            "Users requiring password unlock: "
            + (", ".join(map(str, plan.required_user_ids)) or "none")
        )
        for notice in plan.notices:
            print(
                f"{'BLOCK' if notice.blocking else 'NOTE'} "
                f"{notice.code}: {notice.message}"
            )
        if plan.applicable:
            print(f"Confirmation token: {plan.confirmation_token}")
        if args.plan_out:
            print(f"Plan written to: {Path(args.plan_out).resolve()}")
    return 0 if plan.applicable else 2


def _migrate_addon_data_command(args: argparse.Namespace) -> int:
    """Dry-run or apply the one-time credential/settings shape migration."""

    from iarf_assistant.core.addons.connection_migration import (
        migrate_legacy_connection_data,
    )
    from iarf_assistant.core.backup_service import BackupError, create_backup
    from iarf_assistant.core.encryption import InstallationKeyStore
    from iarf_assistant.core.logging_observability import redact_text
    from iarf_assistant.extensions import db

    paths = _active_paths()
    if _managed_server_running(paths):
        print("Add-on data migration refused: stop the managed Carlina server first.")
        return 1
    if not paths.database_file.is_file():
        print(
            f"Add-on data migration failed: database is missing: {paths.database_file}"
        )
        return 1
    if args.apply and args.confirm != "MIGRATE-ADDON-DATA":
        print("Confirmation must exactly equal: MIGRATE-ADDON-DATA")
        return 2

    backup_path = None
    if args.apply and not args.no_safety_backup:
        try:
            backup = create_backup(
                paths,
                key_store=InstallationKeyStore(paths.master_keys_file),
            )
            backup_path = str(backup.path)
        except (BackupError, OSError, ValueError) as exc:
            print(f"Add-on data migration backup failed: {exc}")
            return 1

    environment_name = "CARLINA_RUNTIME_ROLE"
    previous_role = os.environ.get(environment_name)
    os.environ[environment_name] = "command"
    try:
        from iarf_assistant import create_app

        app = create_app()
        with app.app_context():
            try:
                report = migrate_legacy_connection_data(
                    db.session,
                    dry_run=not args.apply,
                    commit=args.apply,
                )
            except Exception:
                db.session.rollback()
                raise
    except Exception as exc:  # noqa: BLE001 - CLI boundary must redact all failures.
        print(f"Add-on data migration failed: {redact_text(str(exc))}")
        return 1
    finally:
        if previous_role is None:
            os.environ.pop(environment_name, None)
        else:
            os.environ[environment_name] = previous_role

    payload = report.to_dict()
    payload["backup_path"] = backup_path
    payload["database"] = str(paths.database_file)
    if args.json:
        _print_json(payload)
    else:
        mode = "Migration complete" if args.apply else "Migration dry run"
        print(f"{mode}: {paths.database_file}")
        for field_name in (
            "connections_created",
            "secrets_created",
            "grants_created",
            "preferences_created",
            "email_accounts_linked",
            "bindings_created",
            "unchanged",
        ):
            print(f"  {field_name}: {payload[field_name]}")
        for warning in payload["warnings"]:
            print(f"  WARNING: {warning}")
        if backup_path:
            print(f"Safety backup: {backup_path}")
        if not args.apply:
            print(
                "Apply with: carlina migrate-addon-data --apply "
                "--confirm MIGRATE-ADDON-DATA"
            )
    return 0


def _build_command_parser() -> argparse.ArgumentParser:
    from iarf_assistant.distribution_cli import add_distribution_parsers

    parser = argparse.ArgumentParser(
        prog="carlina",
        description="Install, run, diagnose, back up, and maintain Carlina.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    addon_parser = subparsers.add_parser(
        "addon",
        help="Validate and manage Add-on API packages.",
    )
    addon_subparsers = addon_parser.add_subparsers(
        dest="addon_command",
        required=True,
    )
    addon_validate = addon_subparsers.add_parser(
        "validate",
        help="Compile a package contract without importing its code.",
    )
    addon_validate.add_argument("path")
    addon_validate.add_argument(
        "--source-kind",
        choices=("installed", "built_in"),
        default="installed",
    )
    addon_validate.add_argument("--json", action="store_true")
    addon_validate.add_argument("--include-manifest", action="store_true")
    addon_takeover = addon_subparsers.add_parser(
        "takeover-private-overlays",
        help=(
            "Take over exact active or package-missing private built-ins from a "
            "reviewed overlay index."
        ),
    )
    addon_takeover.add_argument("--index", required=True)
    addon_takeover.add_argument(
        "--addon",
        action="append",
        dest="addon_ids",
        required=True,
        metavar="ADDON_ID",
        help="Private built-in ID to take over; repeat for every explicit selection.",
    )
    addon_takeover.add_argument("--actor-user-id", required=True, type=int)
    addon_takeover.add_argument(
        "--reviewed-notice",
        action="append",
        dest="reviewed_notice_ids",
        default=[],
        metavar="NOTICE_ID",
        help="Explicitly acknowledge one declared dependency notice; repeat as needed.",
    )
    addon_takeover.add_argument(
        "--approve-private-host-code",
        action="store_true",
        help="Explicitly approve full host-code access for every selected overlay.",
    )
    addon_takeover.add_argument(
        "--approve-private-takeover",
        action="store_true",
        help="Explicitly approve replacement of the matching built-in package owner.",
    )

    init_parser = subparsers.add_parser(
        "init", help="Initialize or repair an installation."
    )
    init_parser.add_argument(
        "--application-profile",
        choices=[profile.value for profile in RuntimeProfile],
        help="Select the application composition for this data root.",
    )
    init_parser.add_argument("--admin-email")
    init_parser.add_argument("--admin-password")
    init_parser.add_argument("--admin-stdin", action="store_true")
    init_parser.add_argument("--no-admin-prompt", action="store_true")
    init_parser.add_argument("--repair", action="store_true")

    start_parser = subparsers.add_parser("start", help="Start the local web service.")
    start_parser.add_argument(
        "--application-profile",
        choices=[profile.value for profile in RuntimeProfile],
        help="Select the application composition for this data root.",
    )
    start_parser.add_argument(
        "--host",
        default=product_environment_value("HOST", default="127.0.0.1"),
    )
    start_parser.add_argument(
        "--port",
        type=int,
        default=None,
    )
    start_parser.add_argument("--open-browser", action="store_true")
    start_parser.add_argument("--profile", choices=("local", "server"))
    start_parser.add_argument("--external-url")
    start_parser.add_argument("--trusted-hosts")
    start_parser.add_argument("--proxy-hops", type=int)
    start_parser.add_argument(
        "--restart",
        action="store_true",
        help="Restart Waitress if the serving loop exits cleanly.",
    )
    start_parser.add_argument(
        "--runtime-role",
        choices=("managed", "web"),
        help="Run as the service owner (managed) or as a web-only process.",
    )
    start_parser.add_argument(
        "--legacy-secret-file",
        metavar="PATH",
        help="Absolute, owner-only file containing the legacy Flask secret.",
    )

    subparsers.add_parser("stop", help="Stop the managed local web service.")
    subparsers.add_parser("manage", help="Open the interactive management menu.")

    status_parser = subparsers.add_parser(
        "status", help="Show runtime and installation status."
    )
    status_parser.add_argument("--json", action="store_true")
    status_parser.add_argument("--host", default="127.0.0.1")
    status_parser.add_argument("--port", type=int, default=None)

    doctor_parser = subparsers.add_parser(
        "doctor", help="Run installation diagnostics."
    )
    doctor_parser.add_argument("--json", action="store_true")
    doctor_parser.add_argument(
        "--deep", action="store_true", help="Hash and verify the latest backup."
    )
    doctor_parser.add_argument(
        "--strict", action="store_true", help="Return exit code 2 for warnings."
    )

    backup_parser = subparsers.add_parser(
        "backup", help="Create a complete verified backup."
    )
    backup_parser.add_argument("--output")
    backup_parser.add_argument("--json", action="store_true")

    restore_parser = subparsers.add_parser(
        "restore", help="Schedule, apply, or cancel a restore."
    )
    restore_parser.add_argument("archive", nargs="?")
    action = restore_parser.add_mutually_exclusive_group()
    action.add_argument("--apply-pending", action="store_true")
    action.add_argument("--cancel-pending", action="store_true")
    restore_parser.add_argument("--confirm")

    migrate_parser = subparsers.add_parser(
        "migrate-data",
        help="Plan or resume migration from checkout-local storage.",
    )
    migrate_parser.add_argument("--database", help="Explicit legacy SQLite source.")
    migrate_parser.add_argument("--target", help="Canonical target data root.")
    migrate_parser.add_argument("--plan-out", help="Write the reviewed plan as JSON.")
    migrate_parser.add_argument("--apply-plan", help="Apply an exact saved plan.")
    migrate_parser.add_argument("--confirm", help="Exact token printed by the plan.")
    migrate_parser.add_argument("--no-safety-backup", action="store_true")
    migrate_parser.add_argument("--json", action="store_true")

    encryption_parser = subparsers.add_parser(
        "migrate-encryption",
        help="Plan or resume offline migration to authenticated v2 envelopes.",
    )
    encryption_parser.add_argument("--plan-out")
    encryption_action = encryption_parser.add_mutually_exclusive_group()
    encryption_action.add_argument("--apply-plan")
    encryption_action.add_argument("--verify-plan")
    encryption_parser.add_argument("--confirm")
    encryption_parser.add_argument(
        "--assign-unresolved-user",
        type=int,
        metavar="USER_ID",
        help="Assign one exact ownerless legacy-user cell set to this existing user.",
    )
    encryption_parser.add_argument(
        "--approve-unresolved-user-ownership",
        action="store_true",
        help="Attest the exact ownerless legacy-user assignments in the new plan.",
    )
    encryption_parser.add_argument(
        "--expected-system-quarantine-count",
        type=int,
        metavar="COUNT",
        help="Exact operator-approved count of irrecoverable legacy sys: values.",
    )
    encryption_parser.add_argument(
        "--approve-system-quarantine",
        action="store_true",
        help="Attest exact loss-marker replacement for the approved sys: cells.",
    )
    encryption_parser.add_argument(
        "--legacy-secret-file",
        metavar="PATH",
        action="append",
        help="Absolute, owner-only file containing the legacy Flask secret.",
    )
    encryption_parser.add_argument(
        "--batch-size",
        type=int,
        default=250,
    )
    encryption_parser.add_argument("--no-safety-backup", action="store_true")
    encryption_parser.add_argument(
        "--initialize-key",
        action="store_true",
        help="Explicitly create the first persistent installation key if absent.",
    )
    encryption_parser.add_argument("--json", action="store_true")

    addon_data_parser = subparsers.add_parser(
        "migrate-addon-data",
        help="Dry-run or apply the one-time Connections and bindings data migration.",
    )
    addon_data_parser.add_argument("--apply", action="store_true")
    addon_data_parser.add_argument("--confirm")
    addon_data_parser.add_argument("--no-safety-backup", action="store_true")
    addon_data_parser.add_argument("--json", action="store_true")
    add_distribution_parsers(subparsers)
    return parser


def _prepare_application_profile(profile: RuntimeProfile | str) -> None:
    """Select command paths and reject an incompatible root before side effects."""

    composition = resolve_runtime_composition(profile)
    paths = bootstrap.load_bootstrap_paths(profile=composition.profile)
    validate_runtime_profile(paths.data_root, composition.profile)
    os.environ["CARLINA_PROFILE"] = composition.profile.value


def _command_main(
    argv: list[str], *, profile: RuntimeProfile | str | None = None
) -> int:
    parser = _build_command_parser()
    args = parser.parse_args(argv)
    requested_profile = getattr(args, "application_profile", None)
    if (
        profile is not None
        and requested_profile is not None
        and RuntimeProfile(profile) != RuntimeProfile(requested_profile)
    ):
        parser.error("This application entry point has a fixed runtime profile.")
    selected_profile = profile if profile is not None else requested_profile
    if selected_profile is not None:
        try:
            _prepare_application_profile(selected_profile)
        except RuntimeProfileError as exc:
            print(f"Application selection refused: {exc}")
            return 1
    if hasattr(args, "port") and args.port is None:
        args.port = int(
            product_environment_value(
                "PORT",
                default=str(resolve_runtime_composition().default_http_port),
            )
        )
    if args.command == "start":
        if product_environment_value("PROGRAM_ROOT", default=None):
            try:
                from carlina.core.database_checkpoint import (
                    interrupted_database_switches,
                )

                managed_layout = _managed_installation_layout()
                pending_recovery = managed_layout.read_recovery_requirement()
                pending_rollback = managed_layout.read_pending_rollback()
                pending_update = managed_layout.read_pending_update()
                pending_database_switches = interrupted_database_switches(
                    _active_paths().state_dir / "database-checkpoint-recovery"
                )
            except RuntimeError as exc:
                print(f"Managed start refused: {exc}")
                return 1
            if pending_database_switches:
                print(
                    "Managed start refused: an interrupted database checkpoint "
                    "switch requires stopped maintenance recovery. Re-run the "
                    "managed update or rollback command before starting Carlina."
                )
                return 1
            if pending_rollback is not None:
                print(
                    "Managed start refused: a rollback stopped between data "
                    "recovery and the program-version commit. Re-run the managed "
                    "rollback or update command while Carlina is stopped so the "
                    "transaction can be reconciled."
                )
                return 1
            if pending_update is not None:
                print(
                    "Managed start refused: target execution stopped before the "
                    "release commit and may have changed live data. Re-run the "
                    "managed update or rollback command while Carlina is stopped "
                    "so the selected recovery material can be restored."
                )
                return 1
            if pending_recovery is not None:
                transaction_id = pending_recovery["transaction_id"]
                print(
                    "Managed start refused: a no-recovery update failed after "
                    "target execution and may have changed live data. Do not "
                    "restart the prior release blindly. Review the managed "
                    "recovery-required record, then deploy a compatible target "
                    "with --acknowledge-recovery-required "
                    f"{transaction_id}."
                )
                return 1
        for value, environment_name in (
            (args.profile, "CARLINA_WEB_PROFILE"),
            (args.external_url, "CARLINA_EXTERNAL_URL"),
            (args.trusted_hosts, "CARLINA_TRUSTED_HOSTS"),
            (args.proxy_hops, "CARLINA_PROXY_HOPS"),
        ):
            if value is not None:
                os.environ[environment_name] = str(value)
        if args.runtime_role is not None:
            if args.legacy_secret_file is not None:
                return start_app(
                    args.host,
                    args.port,
                    args.open_browser,
                    restart=args.restart,
                    runtime_role=args.runtime_role,
                    legacy_secret_file=args.legacy_secret_file,
                )
            return start_app(
                args.host,
                args.port,
                args.open_browser,
                restart=args.restart,
                runtime_role=args.runtime_role,
            )
        if args.legacy_secret_file is not None:
            return start_app(
                args.host,
                args.port,
                args.open_browser,
                restart=args.restart,
                legacy_secret_file=args.legacy_secret_file,
            )
        return start_app(
            args.host,
            args.port,
            args.open_browser,
            restart=args.restart,
        )

    return _dispatch_command(args, parser)


def _dispatch_command(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
) -> int:
    from carlina.core.recovery_policy import RecoveryPolicyError
    from iarf_assistant.distribution_cli import (
        handle_distribution_command,
        validate_installation_arguments,
    )

    if args.command == "migrate-encryption":
        return _migrate_encryption_command(args)
    if args.command in {"install", "update", "rollback"}:
        try:
            validate_installation_arguments(args)
        except RecoveryPolicyError as exc:
            print(
                str(exc),
                file=sys.stderr if getattr(args, "events_jsonl", False) else sys.stdout,
            )
            return 1
    systemd_unit = getattr(args, "systemd_unit", None)
    if (
        args.command == "update"
        and getattr(args, "check", False)
        and getattr(args, "stop", False)
    ):
        print("update --check cannot be combined with --stop.")
        return 2
    if (
        args.command in {"update", "rollback"}
        and systemd_unit is not None
        and not getattr(args, "stop", False)
    ):
        print("--systemd-unit is valid only together with --stop.")
        return 2
    paths = _active_paths()
    if args.command in {"update", "rollback"} and getattr(args, "stop", False):
        operation = "Update" if args.command == "update" else "Rollback"
        managed_pid = _verified_managed_server_pid(paths)
        try:
            owner = (
                _detect_systemd_service_owner(managed_pid)
                if managed_pid is not None
                else None
            )
        except _SystemdOwnershipInspectionError as exc:
            print(f"{operation} refused: {exc}.")
            return 1
        if systemd_unit is not None:
            try:
                _validate_user_systemd_unit(systemd_unit)
            except _SystemdStopError as exc:
                print(f"{operation} refused: {exc}.")
                return 1
            if managed_pid is None:
                try:
                    _verify_user_systemd_unit_stopped(systemd_unit)
                except _SystemdStopError as exc:
                    print(
                        f"{operation} refused: no verified managed PID is available "
                        f"and {exc}."
                    )
                    return 1
            else:
                if owner is None:
                    print(
                        f"{operation} refused: PID {managed_pid} is not supervised "
                        "by a detectable systemd service. Use plain --stop only "
                        "for a genuinely unsupervised runtime."
                    )
                    return 1
                detected_unit, scope = owner
                if scope == "system":
                    print(
                        f"{operation} refused: PID {managed_pid} is supervised by "
                        f"system service '{detected_unit}'. Stop that service "
                        "externally; the updater will not invoke systemctl --system."
                    )
                    return 1
                if detected_unit != systemd_unit:
                    print(
                        f"{operation} refused: PID {managed_pid} belongs to user "
                        f"service '{detected_unit}', not the requested unit "
                        f"'{systemd_unit}'."
                    )
                    return 1
                try:
                    _stop_and_verify_user_systemd_unit(systemd_unit)
                except _SystemdStopError as exc:
                    print(f"{operation} refused: {exc}.")
                    return 1
            if managed_pid is not None and not _wait_for_pid_to_exit(managed_pid):
                print(
                    f"{operation} refused: the previously verified managed PID "
                    f"{managed_pid} did not exit after stopping '{systemd_unit}'."
                )
                return 1
            reacquired_pid = _verified_managed_server_pid(paths)
            if reacquired_pid is not None:
                print(
                    f"{operation} refused: managed runtime PID {reacquired_pid} "
                    "reacquired ownership after the user service was stopped."
                )
                return 1
            if not _cleanup_owned_workers_for_distribution(
                paths,
                operation=operation,
            ):
                return 1
        elif managed_pid is not None:
            if owner is not None:
                detected_unit, scope = owner
                if scope == "system":
                    print(
                        f"{operation} refused: PID {managed_pid} is supervised by "
                        f"system service '{detected_unit}'. Stop that service "
                        "externally; the updater will not invoke systemctl --system."
                    )
                else:
                    print(
                        f"{operation} refused: PID {managed_pid} is supervised by "
                        f"user service '{detected_unit}'. Re-run with "
                        f"--stop --systemd-unit {detected_unit}."
                    )
                return 1
            if stop_managed_server(paths, verified_pid=managed_pid) != 0:
                return 1
            if not _wait_for_pid_to_exit(managed_pid):
                print(
                    f"{operation} refused: the previously verified managed PID "
                    f"{managed_pid} did not exit after direct shutdown."
                )
                return 1
        else:
            if not _cleanup_owned_workers_for_distribution(
                paths,
                operation=operation,
            ):
                return 1
    distribution_result = handle_distribution_command(args, paths)
    if distribution_result is not None:
        return distribution_result
    if args.command == "addon" and args.addon_command == "validate":
        return _addon_validate_command(args)
    if args.command == "addon" and args.addon_command == "takeover-private-overlays":
        return _addon_private_overlay_takeover_command(args)
    if args.command == "init":
        setup_args = ["--skip-install", "--skip-tailwind", "--skip-frontend"]
        if args.application_profile is not None:
            setup_args.extend(["--application-profile", args.application_profile])
        if args.repair:
            setup_args.append("--repair")
        if args.no_admin_prompt:
            setup_args.append("--no-admin-prompt")
        if getattr(args, "admin_stdin", False):
            setup_args.append("--admin-stdin")
        if args.admin_email:
            setup_args.extend(["--admin-email", args.admin_email])
        if args.admin_password:
            setup_args.extend(["--admin-password", args.admin_password])
        return bootstrap.setup_main(setup_args)
    if args.command == "stop":
        return stop_managed_server()
    if args.command == "manage":
        return menu_loop(
            "127.0.0.1", resolve_runtime_composition().default_http_port, True
        )
    if args.command == "status":
        return _status_command(args)
    if args.command == "doctor":
        return _doctor_command(args)
    if args.command == "backup":
        return _backup_command(args)
    if args.command == "restore":
        return _restore_command(args)
    if args.command == "migrate-data":
        return _migrate_data_command(args)
    if args.command == "migrate-addon-data":
        return _migrate_addon_data_command(args)
    parser.error(f"Unsupported command: {args.command}")
    return 2


def manager_main(
    argv: list[str] | None = None, *, profile: RuntimeProfile | str | None = None
) -> int:
    active_argv = list(sys.argv[1:] if argv is None else argv)
    if (active_argv and active_argv[0] in _COMMAND_NAMES) or active_argv in (
        ["--help"],
        ["-h"],
    ):
        return _command_main(active_argv, profile=profile)
    if profile is None:
        bootstrap.load_bootstrap_paths()
    else:
        try:
            _prepare_application_profile(profile)
        except RuntimeProfileError as exc:
            print(f"Application selection refused: {exc}")
            return 1
    parser = argparse.ArgumentParser(
        description=(f"Install, start, and manage {PRODUCT_IDENTITY.name}.")
    )
    parser.add_argument(
        "--manage", action="store_true", help="Always show the management menu."
    )
    parser.add_argument(
        "--setup",
        action="store_true",
        help="Run the setup/repair flow through the manager.",
    )
    parser.add_argument(
        "--repair",
        action="store_true",
        help="Repair only missing or stale setup/frontend state.",
    )
    parser.add_argument(
        "--rebuild-frontend",
        action="store_true",
        help="Force-rebuild legacy, core, and addon frontend assets.",
    )
    parser.add_argument(
        "--no-ui",
        action="store_true",
        help="Run the recommended action without showing the menu.",
    )
    parser.add_argument(
        "--admin-email",
        default=None,
        help="Create the first admin user with this email during setup.",
    )
    parser.add_argument(
        "--admin-password",
        default=None,
        help="Create the first admin user with this password during setup.",
    )
    parser.add_argument(
        "--host",
        default=product_environment_value("HOST", default="127.0.0.1"),
        help="Host interface to bind (defaults to loopback).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(
            product_environment_value(
                "PORT",
                default=str(resolve_runtime_composition().default_http_port),
            )
        ),
        help="Port to bind.",
    )
    parser.add_argument(
        "--open-browser",
        action="store_true",
        default=True,
        help="Open the browser when starting.",
    )
    parser.add_argument(
        "--no-open-browser",
        action="store_false",
        dest="open_browser",
        help="Do not open the browser.",
    )
    parser.add_argument(
        "--start-only",
        action="store_true",
        help="Start the app immediately without state detection.",
    )
    parser.add_argument(
        "--wait-for-pid",
        type=int,
        default=None,
        help="Wait for this PID to exit before starting the app.",
    )
    args = parser.parse_args(active_argv)

    if bool(args.admin_email) != bool(args.admin_password):
        print("--admin-email and --admin-password must be provided together.")
        return 2

    if args.rebuild_frontend:
        return rebuild_assets()

    if args.setup or args.repair:
        return run_setup(
            interactive=not args.no_ui,
            admin_email=args.admin_email,
            admin_password=args.admin_password,
            repair=args.repair,
        )

    if args.start_only:
        _wait_for_pid_to_exit(args.wait_for_pid)
        return start_app(args.host, args.port, args.open_browser)

    state = detect_install_state(
        host=args.host,
        port=args.port,
        paths=bootstrap.ACTIVE_PATHS,
    )
    _write_state_event("state_checked", state)

    if args.manage:
        return menu_loop(args.host, args.port, args.open_browser)

    if args.no_ui or state.setup_complete or state.running or not sys.stdin.isatty():
        return run_recommended_action(
            state,
            host=args.host,
            port=args.port,
            open_browser_flag=args.open_browser,
            interactive=not args.no_ui,
        )

    return menu_loop(args.host, args.port, args.open_browser)


if __name__ == "__main__":
    raise SystemExit(manager_main())
