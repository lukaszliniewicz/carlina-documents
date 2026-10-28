from __future__ import annotations

import argparse
import errno
import getpass
import ipaddress
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from contextlib import ExitStack, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from dotenv import load_dotenv

from carlina.product_identity import (
    PRODUCT_IDENTITY,
    product_environment_value,
)
from carlina.profile_binding import bind_runtime_profile, validate_runtime_profile
from carlina.runtime_composition import resolve_runtime_composition
from carlina.runtime_profiles import RuntimeProfile, RuntimeProfileError
from iarf_assistant.core.app_paths import (
    WORKER_OWNERSHIP_XOPTION,
    AppPaths,
    load_runtime_environment,
    worker_ownership_token,
)
from iarf_assistant.core.frontend_orchestration import (
    build_frontend_workspace,
    inspect_frontend_workspace,
)
from iarf_assistant.core.legacy_secret import (
    LegacySecretFileError,
    legacy_secret_file_environment,
)
from iarf_assistant.core.runtime_ownership import (
    RUNTIME_ROLE_MANAGED,
    RUNTIME_ROLE_WEB,
    RuntimeLease,
    RuntimeOwnershipError,
    process_is_running,
)
from iarf_assistant.core.web_security import (
    LOCAL_PROFILE,
    SERVER_PROFILE,
    WebSecurityConfigurationError,
    resolve_web_security_policy,
    waitress_proxy_options,
)
from iarf_assistant.restart import configure_restart_environment

PROJECT_ROOT = Path(__file__).resolve().parents[1]
INSTANCE_DIR = PROJECT_ROOT / "instance"
LOG_DIR = INSTANCE_DIR / "logs"
SETUP_LOG = LOG_DIR / "setup.log"
START_LOG = LOG_DIR / "start.log"
ENV_FILE = PROJECT_ROOT / ".env"
DB_FILE = INSTANCE_DIR / "iarf_ai.db"
TAILWIND_OUTPUT = PROJECT_ROOT / "iarf_assistant" / "static" / "output.css"
TAILWIND_SCRIPT = PROJECT_ROOT / "scripts" / "build_tailwind.py"
SUPPORTED_PYTHON = (3, 14)
PROCESS_SNAPSHOT_TIMEOUT_SECONDS = 30.0
ACTIVE_PATHS: AppPaths | None = None


class BootstrapError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ManagedProcessIdentity:
    """One process captured with the creation time needed to reject PID reuse."""

    pid: int
    create_time: float


@dataclass(frozen=True, slots=True)
class ManagedProcessTreeSnapshot:
    """A verified manager and its descendants in child-before-parent order."""

    root: ManagedProcessIdentity
    descendants: tuple[ManagedProcessIdentity, ...]


def _resolve_server_runtime_role(explicit_role: str | None = None) -> str:
    """Resolve a serving role while keeping the normal launcher managed."""

    configured = (
        explicit_role
        if explicit_role is not None
        else product_environment_value("RUNTIME_ROLE", default="")
    )
    normalized = str(configured or "").strip().lower()
    if not normalized:
        return RUNTIME_ROLE_MANAGED
    if normalized not in {RUNTIME_ROLE_MANAGED, RUNTIME_ROLE_WEB}:
        raise BootstrapError(
            "A web server runtime role must be 'managed' or 'web'."
        )
    return normalized


def load_project_environment(
    project_root: Path = PROJECT_ROOT,
    *,
    profile: RuntimeProfile | str | None = None,
) -> bool:
    """Load checkout-local configuration without overriding explicit process env."""
    if resolve_runtime_composition(profile).profile is RuntimeProfile.DOCUMENTS:
        return False
    original_profile = os.environ.get("CARLINA_PROFILE")
    try:
        return bool(
            load_dotenv(
                dotenv_path=project_root / ".env",
                override=False,
            )
        )
    finally:
        if original_profile is None:
            os.environ.pop("CARLINA_PROFILE", None)
        else:
            os.environ["CARLINA_PROFILE"] = original_profile


def configure_runtime_paths(paths: AppPaths) -> None:
    """Point the legacy bootstrap helpers at the canonical mutable layout."""

    global ACTIVE_PATHS, DB_FILE, ENV_FILE, INSTANCE_DIR, LOG_DIR, SETUP_LOG, START_LOG
    ACTIVE_PATHS = paths
    INSTANCE_DIR = paths.instance_dir
    LOG_DIR = paths.log_dir
    SETUP_LOG = paths.log_dir / "setup.log"
    START_LOG = paths.log_dir / "start.log"
    ENV_FILE = paths.config_file
    DB_FILE = paths.database_file


def load_bootstrap_paths(
    project_root: Path = PROJECT_ROOT,
    *,
    profile: RuntimeProfile | str | None = None,
) -> AppPaths:
    """Load effective configuration and update bootstrap compatibility paths."""

    package_candidate = project_root / "iarf_assistant"
    package_root = (
        package_candidate
        if package_candidate.is_dir()
        else Path(__file__).resolve().parent
    )
    managed_program_root = product_environment_value(
        "PROGRAM_ROOT",
        default=str(project_root),
    )
    paths = load_runtime_environment(
        Path(managed_program_root or project_root),
        package_root=package_root,
        profile=profile,
    )
    configure_runtime_paths(paths)
    return paths


def _wait_for_port_to_be_free(host: str, port: int, max_attempts: int = 5) -> None:
    """Ensures the port is free before starting the application, retrying if occupied."""
    import errno
    import socket

    for attempt in range(1, max_attempts + 1):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
            s.close()
            return
        except OSError as exc:
            s.close()
            is_addr_in_use = (
                exc.errno == errno.EADDRINUSE
                or getattr(exc, "winerror", None) == 10048
                or "10048" in str(exc)
            )
            if is_addr_in_use:
                if attempt < max_attempts:
                    wait_time = 2 * attempt
                    msg = f"Port {port} is busy (address already in use). Waiting {wait_time}s to retry (attempt {attempt}/{max_attempts})..."
                    print(msg)
                    _append_log(START_LOG, f"[{_timestamp()}] {msg}\n")
                    time.sleep(wait_time)
                else:
                    raise BootstrapError(
                        f"Port {port} is already in use after {max_attempts} attempts. "
                        f"Please ensure no other processes are binding to this port."
                    ) from exc
            else:
                raise BootstrapError(
                    f"Failed to check port availability: {exc}"
                ) from exc


def ensure_runtime_dirs() -> None:
    INSTANCE_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)


def check_python_version() -> None:
    current_minor = tuple(sys.version_info[:2])
    if current_minor != SUPPORTED_PYTHON:
        supported = ".".join(str(part) for part in SUPPORTED_PYTHON)
        current = ".".join(str(part) for part in sys.version_info[:3])
        raise BootstrapError(
            f"Carlina requires Python {supported}.x; current interpreter is {current}. "
            "Use the managed Pixi runtime so core and add-on interpreters remain isolated."
        )


def _timestamp() -> str:
    return datetime.now(tz=UTC).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _append_log(log_path: Path, text: str) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(text)
        if not text.endswith("\n"):
            handle.write("\n")


def _display_command(command: list[str]) -> str:
    return subprocess.list2cmdline([str(part) for part in command])


def _write_console_output(text: str) -> None:
    stream = sys.stdout
    encoding = getattr(stream, "encoding", None) or "utf-8"
    safe_bytes = text.encode(encoding, errors="replace")
    buffer = getattr(stream, "buffer", None)
    if buffer is not None:
        buffer.write(safe_bytes)
        buffer.flush()
        return
    stream.write(safe_bytes.decode(encoding, errors="replace"))
    stream.flush()


def run_command(
    command: list[str],
    *,
    check: bool = True,
    env: dict[str, str] | None = None,
    log_path: Path | None = None,
    display_command: list[str] | None = None,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    ensure_runtime_dirs()
    effective_log_path = log_path or SETUP_LOG
    display = _display_command(display_command or command)
    _append_log(effective_log_path, f"\n[{_timestamp()}] $ {display}\n")
    result = subprocess.run(
        command,
        cwd=str(PROJECT_ROOT),
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
        **({"input": input_text} if input_text is not None else {}),
    )
    if result.stdout:
        _append_log(effective_log_path, result.stdout)
        output = result.stdout
        if not output.endswith("\n"):
            output += "\n"
        _write_console_output(output)
    _append_log(
        effective_log_path,
        f"[{_timestamp()}] exit code {result.returncode}\n",
    )
    if check and result.returncode != 0:
        raise BootstrapError(
            f"Command failed with exit code {result.returncode}: {display}"
        )
    return result


def _env_has_value(text: str, key: str) -> bool:
    prefix = f"{key}="
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or not line.startswith(prefix):
            continue
        return bool(line[len(prefix) :].strip().strip('"').strip("'"))
    return False


def ensure_env_file(secret_key: str | None = None) -> bool:
    ensure_runtime_dirs()
    existing = ENV_FILE.read_text(encoding="utf-8") if ENV_FILE.exists() else ""
    if _env_has_value(existing, "FLASK_SECRET_KEY"):
        return False

    secret = secret_key or secrets.token_urlsafe(48)
    prefix = "" if not existing or existing.endswith("\n") else "\n"
    with ENV_FILE.open("a", encoding="utf-8") as handle:
        handle.write(prefix)
        if not existing:
            handle.write(
                f"# Local {PRODUCT_IDENTITY.name} settings. Do not commit this file.\n"
            )
        handle.write(f"FLASK_SECRET_KEY={secret}\n")
    return True


def ensure_runtime_config_file() -> bool:
    """Create a non-secret native configuration file when it is absent."""

    ensure_runtime_dirs()
    if ENV_FILE.exists():
        return False
    ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    ENV_FILE.write_text(
        f"# Local {PRODUCT_IDENTITY.name} configuration.\n"
        "# Secrets are stored separately under the restricted secrets directory.\n",
        encoding="utf-8",
    )
    return True


def flask_env() -> dict[str, str]:
    env = os.environ.copy()
    env["FLASK_APP"] = "iarf_assistant.wsgi"
    env["CARLINA_RUNTIME_ROLE"] = "command"
    return env


def build_init_app_command(
    *,
    admin_email: str | None = None,
    admin_password: str | None = None,
    no_admin_prompt: bool = True,
    admin_stdin: bool = False,
) -> tuple[list[str], list[str]]:
    command = [sys.executable, "-m", "flask", "init-app"]
    display = list(command)
    if admin_stdin:
        command.append("--admin-stdin")
        display.append("--admin-stdin")
    elif admin_email or admin_password:
        command.extend(
            [
                "--admin-email",
                admin_email or "",
                "--admin-password",
                admin_password or "",
            ]
        )
        display.extend(["--admin-email", admin_email or "", "--admin-password", "***"])
    elif no_admin_prompt:
        command.append("--no-admin-prompt")
        display.append("--no-admin-prompt")
    return command, display


def _prompt_for_admin() -> tuple[str | None, str | None]:
    answer = input("Create the first admin user now? [Y/n]: ").strip().lower()
    if answer in {"n", "no"}:
        return None, None
    email = input("Admin email: ").strip()
    while not email:
        email = input("Admin email: ").strip()
    password = getpass.getpass("Admin password: ")
    confirm = getpass.getpass("Confirm admin password: ")
    if password != confirm:
        raise BootstrapError("Admin passwords did not match.")
    if not password:
        raise BootstrapError("Admin password cannot be empty.")
    return email, password


def install_project() -> None:
    composition = resolve_runtime_composition()
    target = ".[workbench]" if composition.assistant_services else "."
    run_command([sys.executable, "-m", "pip", "install", "-e", target])


def initialize_database(
    admin_email: str | None,
    admin_password: str | None,
    *,
    admin_input: str | None = None,
) -> None:
    no_prompt = not (admin_email and admin_password)
    command, display = build_init_app_command(
        admin_email=admin_email,
        admin_password=admin_password,
        no_admin_prompt=no_prompt,
        **({"admin_stdin": True} if admin_input is not None else {}),
    )
    options = {"input_text": admin_input} if admin_input is not None else {}
    run_command(command, env=flask_env(), display_command=display, **options)


def initialize_installation_secrets(paths: AppPaths) -> tuple[str, str]:
    """Idempotently initialize data and session keys during explicit setup."""

    from iarf_assistant.core.encryption import (
        InstallationKeyStore,
        ensure_session_key,
    )

    key_id = InstallationKeyStore(paths.master_keys_file).initialize()
    session_key = ensure_session_key(paths.session_key_file)
    return key_id, session_key


def build_tailwind() -> None:
    if not TAILWIND_SCRIPT.exists():
        raise BootstrapError(f"Tailwind build script is missing: {TAILWIND_SCRIPT}")

    command = [sys.executable, str(TAILWIND_SCRIPT)]
    result = run_command(command, check=False)
    if result.returncode == 0:
        return

    if os.name != "nt" or "EEXIST" not in (result.stdout or ""):
        raise BootstrapError(
            "Tailwind build failed. See instance/logs/setup.log for details."
        )

    with tempfile.TemporaryDirectory(
        prefix="tailwind-build-", dir=str(INSTANCE_DIR)
    ) as temp_dir:
        temp_output = Path(temp_dir) / "output.css"
        retry = [sys.executable, str(TAILWIND_SCRIPT), "--output", str(temp_output)]
        run_command(retry)
        shutil.copy2(temp_output, TAILWIND_OUTPUT)
        _append_log(
            SETUP_LOG, f"[{_timestamp()}] Copied Tailwind output to {TAILWIND_OUTPUT}\n"
        )
        print(f"Copied Tailwind output to {TAILWIND_OUTPUT}")


def _log_frontend_command_output(label: str, result: dict[str, object]) -> None:
    stdout = str(result.get("stdout") or "")
    stderr = str(result.get("stderr") or "")
    if stdout:
        _append_log(SETUP_LOG, f"[{_timestamp()}] {label} stdout:\n{stdout}")
    if stderr:
        _append_log(SETUP_LOG, f"[{_timestamp()}] {label} stderr:\n{stderr}")


def build_frontend_assets(*, force: bool = False) -> dict[str, object]:
    print("Validating core and addon frontend assets...")
    result = build_frontend_workspace(PROJECT_ROOT, force=force)
    dependency_result = result.get("dependencies") or {}
    if isinstance(dependency_result, dict):
        print(f"Frontend dependencies: {dependency_result.get('message')}")
        _log_frontend_command_output("frontend dependencies", dependency_result)

    core_result = result.get("core") or {}
    if isinstance(core_result, dict):
        core_label = "OK" if core_result.get("success") else "FAILED"
        print(f"{core_label} core: {core_result.get('message')}")
        _log_frontend_command_output("core frontend", core_result)

    addon_results = result.get("addons") or []
    if isinstance(addon_results, list):
        for addon_result in addon_results:
            if not isinstance(addon_result, dict):
                continue
            label = "OK" if addon_result.get("success") else "FAILED"
            addon_id = addon_result.get("addon_id") or "unknown"
            print(f"{label} {addon_id}: {addon_result.get('message')}")
            _log_frontend_command_output(
                f"addon {addon_id} frontend",
                addon_result,
            )

    if not result.get("success"):
        raise BootstrapError(
            "Frontend readiness failed. See instance/logs/setup.log and run "
            "'python scripts/manage.py --rebuild-frontend' after correcting the failure."
        )
    return result


def _runtime_frontend_verifies_sources() -> bool:
    value = str(
        product_environment_value(
            "FRONTEND_RUNTIME_VALIDATION",
            default="metadata",
        )
        or ""
    ).strip().lower()
    return value in {"full", "source", "sources"}


def setup_required_reasons(
    project_root: Path = PROJECT_ROOT,
    *,
    frontend_status: dict[str, object] | None = None,
    paths: AppPaths | None = None,
) -> list[str]:
    env_file = paths.config_file if paths is not None else project_root / ".env"
    db_file = (
        paths.database_file
        if paths is not None
        else project_root / "instance" / "iarf_ai.db"
    )
    output_css = project_root / "iarf_assistant" / "static" / "output.css"
    reasons: list[str] = []
    if paths is None and (
        not env_file.exists()
        or not _env_has_value(
            env_file.read_text(encoding="utf-8"),
            "FLASK_SECRET_KEY",
        )
    ):
        reasons.append("missing .env FLASK_SECRET_KEY")
    if not db_file.exists():
        reasons.append("missing instance/iarf_ai.db")
    if paths is not None and not paths.master_keys_file.is_file():
        reasons.append("missing installation master key; run carlina init")
    if paths is not None and not paths.session_key_file.is_file():
        reasons.append("missing persistent session key; run carlina init")
    if not output_css.exists():
        reasons.append("missing iarf_assistant/static/output.css")
    if (project_root / "package.json").is_file():
        resolved_frontend_status = frontend_status
        if resolved_frontend_status is None:
            resolved_frontend_status = inspect_frontend_workspace(
                project_root,
                verify_sources=_runtime_frontend_verifies_sources(),
            )
        core_status = resolved_frontend_status.get("core") or {}
        if core_status.get("status") != "built":
            reasons.append("core frontend assets need repair")
    return reasons


def _docker_cli_available_in_wsl(timeout: float = 2.5) -> bool:
    if os.name != "nt":
        return False
    wsl_exe = shutil.which("wsl.exe")
    if wsl_exe is None:
        return False
    distro = str(product_environment_value("DOCKER_WSL_DISTRO", default="")).strip()
    command = [wsl_exe]
    if distro:
        command.extend(["-d", distro])
    command.extend(
        [
            "sh",
            "-lc",
            "command -v docker >/dev/null 2>&1 && docker version --format '{{.Server.Version}}'",
        ]
    )
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def check_optional_tools() -> list[str]:
    warnings: list[str] = []
    if shutil.which("docker") is None and not _docker_cli_available_in_wsl():
        warnings.append(
            "Docker CLI was not found; Docker-backed addons will stay unavailable until Docker is installed."
        )
    return warnings


def setup_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=f"Set up a local {PRODUCT_IDENTITY.name} checkout."
    )
    parser.add_argument(
        "--application-profile",
        choices=[profile.value for profile in RuntimeProfile],
        default=None,
        help="Select the application composition for this data root.",
    )
    parser.add_argument(
        "--skip-install", action="store_true", help="Do not run pip install -e ."
    )
    parser.add_argument(
        "--skip-db", action="store_true", help="Do not run flask init-app."
    )
    parser.add_argument(
        "--skip-tailwind", action="store_true", help="Do not rebuild Tailwind CSS."
    )
    parser.add_argument(
        "--skip-frontend",
        action="store_true",
        help="Do not validate or repair Svelte frontend assets.",
    )
    parser.add_argument(
        "--force-frontend",
        action="store_true",
        help="Reinstall/rebuild all repository frontend assets.",
    )
    parser.add_argument(
        "--repair",
        action="store_true",
        help="Run only idempotent missing/stale readiness actions.",
    )
    parser.add_argument(
        "--no-admin-prompt",
        action="store_true",
        help="Do not prompt for the first admin user.",
    )
    parser.add_argument(
        "--admin-email",
        default=None,
        help="Create the first admin user with this email.",
    )
    parser.add_argument(
        "--admin-password",
        default=None,
        help="Create the first admin user with this password.",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Only report whether setup appears complete.",
    )
    parser.add_argument(
        "--admin-stdin",
        action="store_true",
        help="Read the first administrator email/password from a private JSON pipe.",
    )
    args = parser.parse_args(argv)

    may_write_log = False
    try:
        admin_input = None
        if args.admin_stdin:
            from carlina.core.initial_admin import (
                InitialAdminInputError,
                encode_initial_admin,
                read_initial_admin,
            )

            if (
                args.admin_email is not None
                or args.admin_password is not None
                or args.no_admin_prompt
                or args.skip_db
                or args.check_only
            ):
                raise BootstrapError("--admin-stdin cannot be combined with other administrator or database options.")
            try:
                admin_input = encode_initial_admin(read_initial_admin(sys.stdin.buffer))
            except InitialAdminInputError:
                raise BootstrapError("Initial administrator input is invalid.") from None
        paths = load_bootstrap_paths(profile=args.application_profile)
        check_python_version()
        try:
            composition = resolve_runtime_composition(args.application_profile)
            validate_runtime_profile(paths.data_root, composition.profile)
        except RuntimeProfileError as exc:
            raise BootstrapError(str(exc)) from exc
        if args.check_only:
            reasons = setup_required_reasons(paths=paths)
            if reasons:
                for reason in reasons:
                    print(f"Setup incomplete: {reason}")
                return 1
            print("Setup looks complete.")
            return 0

        try:
            bind_runtime_profile(paths.data_root, composition.profile)
        except RuntimeProfileError as exc:
            raise BootstrapError(str(exc)) from exc
        os.environ["CARLINA_PROFILE"] = composition.profile.value
        may_write_log = True

        ensure_runtime_dirs()
        paths.ensure_directories()
        paths.write_storage_manifest()

        print(f"Using Python: {sys.executable}")
        print(f"Writing setup log: {SETUP_LOG}")
        if ensure_runtime_config_file():
            print(f"Created configuration file: {ENV_FILE}")
        else:
            print(f"Configuration file already exists: {ENV_FILE}")
        key_id, _session_key = initialize_installation_secrets(paths)
        print(f"Installation encryption key ready: {key_id}")

        if not args.skip_install and not args.repair:
            install_project()

        admin_email = args.admin_email
        admin_password = args.admin_password
        if bool(admin_email) != bool(admin_password):
            raise BootstrapError(
                "--admin-email and --admin-password must be provided together."
            )
        if not admin_email and admin_input is None and not args.no_admin_prompt and sys.stdin.isatty():
            admin_email, admin_password = _prompt_for_admin()

        if not args.skip_db:
            if admin_input is None:
                initialize_database(admin_email, admin_password)
            else:
                initialize_database(None, None, admin_input=admin_input)

        if not args.skip_tailwind and (
            not args.repair or not TAILWIND_OUTPUT.is_file()
        ):
            build_tailwind()

        if not args.skip_frontend:
            build_frontend_assets(force=args.force_frontend)

        for warning in check_optional_tools() if composition.assistant_services else []:
            print(f"Warning: {warning}")
            _append_log(SETUP_LOG, f"[{_timestamp()}] WARNING: {warning}\n")

        print("Setup complete.")
        return 0
    except BootstrapError as exc:
        print(f"Setup failed: {exc}")
        if may_write_log:
            _append_log(SETUP_LOG, f"[{_timestamp()}] ERROR: {exc}\n")
        return 1


def _browser_url(host: str, port: int) -> str:
    browser_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    return f"http://{browser_host}:{port}/"


def _is_loopback_bind(host: str) -> bool:
    value = str(host or "").strip().strip("[]").casefold()
    if value == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def _configure_web_exposure(args):
    option_mapping = {
        "web_profile": "CARLINA_WEB_PROFILE",
        "external_url": "CARLINA_EXTERNAL_URL",
        "trusted_hosts": "CARLINA_TRUSTED_HOSTS",
        "proxy_hops": "CARLINA_PROXY_HOPS",
    }
    for attribute, environment_name in option_mapping.items():
        value = getattr(args, attribute, None)
        if value is not None:
            os.environ[environment_name] = str(value)
    try:
        policy = resolve_web_security_policy()
    except WebSecurityConfigurationError as exc:
        raise BootstrapError(str(exc)) from exc
    if policy.profile == LOCAL_PROFILE and not _is_loopback_bind(args.host):
        raise BootstrapError(
            "The local web profile only binds to loopback. To expose Carlina, "
            "select --profile server and provide --external-url, "
            "--trusted-hosts, and --proxy-hops behind a TLS reverse proxy."
        )
    return policy


def _iter_process_command_lines(
    *,
    include_non_python: bool = False,
) -> list[tuple[int, str]]:
    """Returns a best-effort snapshot of running process IDs and command lines."""
    psutil_processes = _iter_process_command_lines_with_psutil(
        include_non_python=include_non_python,
    )
    if psutil_processes is not None:
        return psutil_processes

    if os.name == "nt":
        command = (
            "$ErrorActionPreference='Stop'; "
            "Get-CimInstance Win32_Process | "
            "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"
        )
        shells: list[str] = []
        for executable_name in ("pwsh", "powershell"):
            executable = shutil.which(executable_name)
            if executable and executable.casefold() not in {
                item.casefold() for item in shells
            }:
                shells.append(executable)
        if not shells:
            # Preserve the historical best-effort behavior when PATH lookup is
            # unavailable or mocked by an embedding host.
            shells.append("powershell")

        for shell in shells:
            try:
                result = subprocess.run(
                    [
                        shell,
                        "-NoLogo",
                        "-NoProfile",
                        "-NonInteractive",
                        "-Command",
                        command,
                    ],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    check=False,
                    timeout=PROCESS_SNAPSHOT_TIMEOUT_SECONDS,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except (OSError, subprocess.TimeoutExpired):
                continue
            if result.returncode != 0:
                continue
            payload = (result.stdout or "").strip().lstrip("\ufeff")
            if not payload:
                continue
            try:
                rows = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if isinstance(rows, dict):
                rows = [rows]
            processes: list[tuple[int, str]] = []
            for row in rows or []:
                try:
                    pid = int((row or {}).get("ProcessId") or 0)
                except (AttributeError, TypeError, ValueError):
                    continue
                try:
                    cmdline = ((row or {}).get("CommandLine") or "").strip()
                except AttributeError:
                    cmdline = ""
                if pid > 0:
                    processes.append((pid, cmdline))
            return processes
        return []

    try:
        result = subprocess.run(
            ["ps", "-eo", "pid=,args="],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=PROCESS_SNAPSHOT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        # Minimal containers need not install procps merely for best-effort
        # orphan cleanup. Runtime ownership still provides the hard
        # single-leader guarantee when process enumeration is unavailable.
        return []
    if result.returncode != 0:
        return []
    processes = []
    for raw_line in (result.stdout or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        pid_text, _sep, cmdline = line.partition(" ")
        try:
            pid = int(pid_text)
        except ValueError:
            continue
        processes.append((pid, cmdline.strip()))
    return processes


def _iter_process_command_lines_with_psutil(
    *,
    include_non_python: bool = False,
) -> list[tuple[int, str]] | None:
    """Use the in-process cross-platform API when the core dependency exists."""

    try:
        import psutil
    except ImportError:
        return None

    processes: list[tuple[int, str]] = []
    python_executables = {
        "carlina",
        "carlina.exe",
        "python",
        "python.exe",
        "pythonw.exe",
        Path(sys.executable).name.casefold(),
    }
    try:
        candidates = psutil.process_iter(["pid", "name"], ad_value="")
    except psutil.Error:
        return None
    try:
        for process in candidates:
            name = str(process.info.get("name") or "").casefold()
            if not include_non_python and not (
                name in python_executables or name.startswith("python3.")
            ):
                continue
            try:
                command = " ".join(str(part) for part in process.cmdline())
                pid = int(process.info.get("pid") or process.pid)
            except (OSError, TypeError, ValueError, psutil.Error):
                continue
            if pid > 0:
                processes.append((pid, command))
    except psutil.Error:
        # A process can disappear while the operating-system snapshot is being
        # enumerated. The successfully read rows still form a useful cleanup
        # snapshot.
        pass
    return processes


def _iter_process_command_identities(
    *,
    include_non_python: bool = False,
) -> list[tuple[ManagedProcessIdentity, str]]:
    """Capture command lines and creation times in one process-table pass.

    Postmortem cleanup must not join a command-line snapshot to a later PID
    lookup: a short-lived worker PID could otherwise be recycled between the
    two operations.  The identity returned here is checked again immediately
    before every signal.
    """

    psutil = _load_psutil()
    python_executables = {
        "carlina",
        "carlina.exe",
        "python",
        "python.exe",
        "pythonw.exe",
        Path(sys.executable).name.casefold(),
    }
    try:
        candidates = psutil.process_iter(["pid", "name"], ad_value="")
    except psutil.Error as exc:
        raise OSError("could not enumerate managed worker processes") from exc

    processes: list[tuple[ManagedProcessIdentity, str]] = []
    try:
        for process in candidates:
            name = str(process.info.get("name") or "").casefold()
            if not include_non_python and not (
                name in python_executables or name.startswith("python3.")
            ):
                continue
            try:
                pid = int(process.info.get("pid") or process.pid)
                command = " ".join(str(part) for part in process.cmdline())
                identity = ManagedProcessIdentity(
                    pid=pid,
                    create_time=float(process.create_time()),
                )
            except (OSError, TypeError, ValueError, psutil.Error):
                continue
            if pid > 0:
                processes.append((identity, command))
    except psutil.Error as exc:
        raise OSError("could not finish enumerating managed worker processes") from exc
    return processes


def _normalize_for_match(text: str) -> str:
    return (text or "").replace("\\", "/").strip().lower()


def _command_references_root(command_line: str, root: Path) -> bool:
    """Return whether a command references this root, without prefix collisions."""

    normalized_command = _normalize_for_match(command_line)
    normalized_root = _normalize_for_match(str(root))
    if not normalized_command or not normalized_root:
        return False
    offset = normalized_command.find(normalized_root)
    while offset >= 0:
        end = offset + len(normalized_root)
        if end == len(normalized_command) or normalized_command[end] in {
            "/",
            "\\",
            "'",
            '"',
            " ",
            "\t",
            "\n",
        }:
            return True
        offset = normalized_command.find(normalized_root, offset + 1)
    return False


def _managed_process_roots(
    *,
    project_root: Path,
    data_root: Path | None = None,
    installed_addons_dir: Path | None = None,
    addon_environments_dir: Path | None = None,
) -> tuple[Path, ...]:
    """Return the installation-owned roots that may appear in worker commands."""

    candidates = (
        project_root,
        data_root,
        installed_addons_dir,
        addon_environments_dir,
    )
    roots: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        if candidate is None:
            continue
        normalized = _normalize_for_match(str(candidate))
        if not normalized or normalized in seen:
            continue
        roots.append(candidate)
        seen.add(normalized)
    return tuple(roots)


def _is_managed_worker_marker(normalized_command: str) -> bool:
    """Recognize only managed worker scripts and their bootstrap launcher."""

    if "/iarf_assistant/knowledge/worker.py" in normalized_command:
        return True
    if "/iarf_assistant/core/worker_process.py" in normalized_command:
        return True
    if (
        "/iarf_assistant/addons/" in normalized_command
        and "/worker.py" in normalized_command
    ):
        return True
    return "iarf_assistant.core.managed_worker_bootstrap" in normalized_command


def _has_python_xoption(command_line: str, value: str) -> bool:
    """Match an exact Python ``-X`` value without accepting a token prefix."""

    normalized = _normalize_for_match(command_line)
    expected = value.casefold()
    for prefix in (f"-x {expected}", f'-x "{expected}', f"-x '{expected}"):
        offset = normalized.find(prefix)
        while offset >= 0:
            end = offset + len(prefix)
            if end == len(normalized) or normalized[end] in {" ", "\t", "'", '"'}:
                return True
            offset = normalized.find(prefix, offset + 1)
    return False


def _has_worker_ownership_marker(command_line: str, ownership_token: str) -> bool:
    """Match an exact opaque ``python -X`` worker ownership marker."""

    return _has_python_xoption(
        command_line,
        f"{WORKER_OWNERSHIP_XOPTION}={ownership_token}",
    )


def _is_scoped_managed_worker_command(
    command_line: str,
    project_root: Path,
    *,
    data_root: Path | None = None,
    installed_addons_dir: Path | None = None,
    addon_environments_dir: Path | None = None,
) -> bool:
    normalized_command = _normalize_for_match(command_line)
    roots = _managed_process_roots(
        project_root=project_root,
        data_root=data_root,
        installed_addons_dir=installed_addons_dir,
        addon_environments_dir=addon_environments_dir,
    )
    if not normalized_command or not any(
        _command_references_root(command_line, root) for root in roots
    ):
        return False
    if (
        installed_addons_dir is not None
        and _command_references_root(command_line, installed_addons_dir)
        and "/worker.py" in normalized_command
    ):
        # Third-party installed add-ons do not necessarily live below the
        # source package's ``iarf_assistant/addons`` path.
        return True
    return _is_managed_worker_marker(normalized_command)


def _is_project_worker_command(
    command_line: str,
    project_root: Path,
    *,
    ownership_token: str,
    data_root: Path | None = None,
    installed_addons_dir: Path | None = None,
    addon_environments_dir: Path | None = None,
) -> bool:
    """Return whether a scoped managed worker carries this data-root token."""

    return _has_worker_ownership_marker(
        command_line, ownership_token
    ) and _is_scoped_managed_worker_command(
        command_line,
        project_root,
        data_root=data_root,
        installed_addons_dir=installed_addons_dir,
        addon_environments_dir=addon_environments_dir,
    )


def _has_any_worker_ownership_marker(command_line: str) -> bool:
    """Return whether a command identifies any upgraded data-root owner."""

    normalized = _normalize_for_match(command_line)
    marker = f"{WORKER_OWNERSHIP_XOPTION}="
    return any(
        launch_shape in normalized
        for launch_shape in (
            f"-x {marker}",
            f'-x "{marker}',
            f"-x '{marker}",
        )
    )


def _is_project_manager_command(command_line: str, project_root: Path) -> bool:
    normalized_command = _normalize_for_match(command_line)
    normalized_root = _normalize_for_match(str(project_root))
    if not normalized_command or normalized_root not in normalized_command:
        return False
    return (
        "scripts/manage.py" in normalized_command
        or "carlina.bootstrap_manager" in normalized_command
        or "iarf_assistant.bootstrap_manager" in normalized_command
    )


def _read_managed_server_pid(state_dir: Path) -> int | None:
    """Read the managed PID from the canonical state root, not mutable instance data."""

    pid_file = state_dir / "bootstrap" / "server.pid"
    if not pid_file.exists():
        return None
    try:
        pid_value = int(pid_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    return pid_value if pid_value > 0 else None


def _pid_is_running(pid: int) -> bool:
    return process_is_running(pid)


def _load_psutil():
    """Load the mandatory runtime process API with a clear safe-stop diagnostic."""

    try:
        import psutil
    except ImportError as exc:  # pragma: no cover - packaged runtime dependency.
        raise OSError(
            "psutil is required to stop managed process trees safely."
        ) from exc
    return psutil


def process_identity(pid: int) -> ManagedProcessIdentity | None:
    """Capture a PID and creation timestamp for later PID-reuse checks."""

    if pid <= 0:
        return None
    psutil = _load_psutil()
    try:
        process = psutil.Process(pid)
        return ManagedProcessIdentity(pid=pid, create_time=float(process.create_time()))
    except psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess:
        return None
    except psutil.Error:
        return None


def _identity_state(identity: ManagedProcessIdentity) -> str:
    """Return ``matching``, ``gone``, ``reused``, or ``inaccessible``."""

    psutil = _load_psutil()
    try:
        process = psutil.Process(identity.pid)
        if process.status() == psutil.STATUS_ZOMBIE:
            return "gone"
        current_create_time = float(process.create_time())
    except psutil.NoSuchProcess, psutil.ZombieProcess:
        return "gone"
    except psutil.AccessDenied:
        return "inaccessible"
    except psutil.Error:
        return "inaccessible"
    return "matching" if current_create_time == identity.create_time else "reused"


def snapshot_managed_process_tree(
    pid: int,
    *,
    expected_identity: ManagedProcessIdentity | None = None,
) -> ManagedProcessTreeSnapshot:
    """Snapshot one live manager tree before any member is signalled.

    Descendants are returned deepest-first.  Each entry has a creation time so
    a recycled PID cannot be signalled after a race or a partial shutdown.
    """

    psutil = _load_psutil()
    try:
        root_process = psutil.Process(pid)
        root = ManagedProcessIdentity(
            pid=pid, create_time=float(root_process.create_time())
        )
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess) as exc:
        raise OSError(f"managed process {pid} is no longer inspectable") from exc
    except psutil.Error as exc:
        raise OSError(f"could not inspect managed process {pid}: {exc}") from exc
    if expected_identity is not None and root != expected_identity:
        raise OSError(
            f"managed process PID {pid} changed identity before shutdown; refusing to signal it"
        )

    descendants: list[ManagedProcessIdentity] = []
    visited = {pid}

    def visit(process) -> None:
        try:
            children = process.children()
        except psutil.NoSuchProcess:
            return
        except (psutil.AccessDenied, psutil.ZombieProcess) as exc:
            raise OSError(
                f"could not inspect descendants of managed process {process.pid}"
            ) from exc
        for child in children:
            child_pid = int(child.pid)
            if child_pid in visited:
                continue
            visited.add(child_pid)
            try:
                child_identity = ManagedProcessIdentity(
                    pid=child_pid,
                    create_time=float(child.create_time()),
                )
            except psutil.NoSuchProcess:
                continue
            except (psutil.AccessDenied, psutil.ZombieProcess) as exc:
                raise OSError(
                    f"could not identify managed descendant {child_pid}"
                ) from exc
            visit(child)
            descendants.append(child_identity)

    visit(root_process)
    return ManagedProcessTreeSnapshot(root=root, descendants=tuple(descendants))


def _signal_process_identity(
    identity: ManagedProcessIdentity, signal_number: int
) -> bool:
    """Signal only the process captured in the preceding tree snapshot.

    Linux pidfds bind the signal to a process object rather than a numeric PID,
    closing the PID-reuse race after the initial creation-time check.  Other
    POSIX platforms retain the best-effort creation-time check immediately
    before ``kill(2)`` because they do not expose a comparable primitive.
    """

    state = _identity_state(identity)
    if state == "gone":
        return True
    if state != "matching":
        return False

    pidfd_open = getattr(os, "pidfd_open", None)
    pidfd_send_signal = getattr(signal, "pidfd_send_signal", None)
    if os.name == "posix" and callable(pidfd_open) and callable(pidfd_send_signal):
        try:
            pidfd = pidfd_open(identity.pid, 0)
        except ProcessLookupError:
            return True
        except OSError as exc:
            # A Python build may expose pidfd_open while running on a kernel
            # which does not implement it. That is equivalent to no pidfd.
            if exc.errno not in {errno.EINVAL, errno.ENOSYS}:
                return False
        else:
            try:
                # The pidfd pins the process selected by pidfd_open. Recheck
                # while it is held so a PID reused before the open cannot be
                # signalled as though it were the original worker.
                state = _identity_state(identity)
                if state == "gone":
                    return True
                if state != "matching":
                    return False
                pidfd_send_signal(pidfd, signal_number)
                return True
            except ProcessLookupError:
                return True
            except OSError:
                return False
            finally:
                os.close(pidfd)
    try:
        os.kill(identity.pid, signal_number)
    except ProcessLookupError:
        return True
    except PermissionError, OSError, SystemError:
        return False
    return True


def _taskkill_process_tree(identity: ManagedProcessIdentity) -> bool:
    """Run ``taskkill /T`` after an immediate creation-time revalidation.

    ``taskkill`` accepts only a PID, not a process handle, so Windows cannot
    offer pidfd-equivalent race protection here. The check directly before the
    command is intentionally fail-closed; a future native handle-based helper
    could strengthen this boundary further.
    """

    state = _identity_state(identity)
    if state == "gone":
        return True
    if state != "matching":
        return False
    try:
        result = subprocess.run(
            ["taskkill", "/PID", str(identity.pid), "/T", "/F"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
        )
    except OSError:
        return False
    return result.returncode == 0 or _identity_state(identity) != "matching"


def _snapshot_managed_direct_children(
    pid: int,
    *,
    expected_identity: ManagedProcessIdentity | None = None,
) -> tuple[ManagedProcessIdentity, ...]:
    """Capture a verified manager's direct children for Windows ``taskkill /T``.

    Each direct child is a tree root for taskkill, so an unmarked helper it
    spawns (for example Pandoc) is included even though it cannot be matched
    safely from a command-line orphan scan.
    """

    root_snapshot = snapshot_managed_process_tree(
        pid,
        expected_identity=expected_identity,
    )
    psutil = _load_psutil()
    try:
        root = psutil.Process(root_snapshot.root.pid)
        if float(root.create_time()) != root_snapshot.root.create_time:
            raise OSError("managed process changed identity before child shutdown")
        children = root.children()
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess) as exc:
        raise OSError("could not inspect managed process children") from exc
    except psutil.Error as exc:
        raise OSError("could not inspect managed process children") from exc

    identities: list[ManagedProcessIdentity] = []
    for child in children:
        try:
            identities.append(
                ManagedProcessIdentity(
                    pid=int(child.pid),
                    create_time=float(child.create_time()),
                )
            )
        except psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess:
            continue
        except psutil.Error as exc:
            raise OSError("could not identify managed process child") from exc
    return tuple(identities)


def quiesce_windows_managed_process_descendants(
    pid: int,
    *,
    expected_identity: ManagedProcessIdentity | None = None,
) -> dict[str, list[int]]:
    """Stop a verified Windows manager's direct-child trees, not its root."""

    if os.name != "nt":
        raise OSError("Windows taskkill process-tree shutdown is unavailable.")
    children = _snapshot_managed_direct_children(
        pid,
        expected_identity=expected_identity,
    )
    terminated: list[int] = []
    failed: list[int] = []
    for child in children:
        if _taskkill_process_tree(child):
            terminated.append(child.pid)
        else:
            failed.append(child.pid)
    return {"terminated_pids": terminated, "failed_pids": failed}


def _wait_for_process_identities(
    identities: list[ManagedProcessIdentity],
    *,
    timeout_seconds: float,
) -> tuple[list[int], list[int]]:
    """Wait under one global bound and report terminated versus unsafe survivors."""

    pending = {identity.pid: identity for identity in identities}
    terminated: list[int] = []
    unsafe: list[int] = []
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    while pending:
        for pid, identity in list(pending.items()):
            state = _identity_state(identity)
            if state == "gone":
                terminated.append(pid)
                pending.pop(pid)
            elif state != "matching":
                unsafe.append(pid)
                pending.pop(pid)
        if not pending or time.monotonic() >= deadline:
            break
        time.sleep(0.05)
    return terminated, [*pending, *unsafe]


def terminate_managed_process_identities(
    identities: tuple[ManagedProcessIdentity, ...] | list[ManagedProcessIdentity],
    *,
    term_timeout_seconds: float = 5.0,
    kill_timeout_seconds: float = 1.0,
) -> dict[str, list[int]]:
    """Terminate an already-snapshotted tree child-first under global bounds."""

    ordered = list(identities)
    for identity in ordered:
        _signal_process_identity(identity, signal.SIGTERM)

    terminated, remaining = _wait_for_process_identities(
        ordered,
        timeout_seconds=term_timeout_seconds,
    )
    remaining_identities = [
        identity for identity in ordered if identity.pid in set(remaining)
    ]
    if remaining_identities and hasattr(signal, "SIGKILL"):
        for identity in remaining_identities:
            _signal_process_identity(identity, signal.SIGKILL)
        killed, remaining = _wait_for_process_identities(
            remaining_identities,
            timeout_seconds=kill_timeout_seconds,
        )
        terminated.extend(killed)

    terminated_set = set(terminated)
    return {
        "terminated_pids": [
            identity.pid for identity in ordered if identity.pid in terminated_set
        ],
        # A failed signal attempt is not a final failure if the exact process
        # subsequently exits. Preserve only identities that remain matching,
        # changed, or inaccessible after the bounded shutdown attempt.
        "failed_pids": sorted(set(remaining)),
    }


def terminate_managed_process_tree(
    pid: int,
    *,
    expected_identity: ManagedProcessIdentity | None = None,
    term_timeout_seconds: float = 5.0,
    kill_timeout_seconds: float = 1.0,
) -> dict[str, object]:
    """Terminate an exact POSIX manager tree, descendants before the manager."""

    snapshot = snapshot_managed_process_tree(pid, expected_identity=expected_identity)
    descendant_result = terminate_managed_process_identities(
        snapshot.descendants,
        term_timeout_seconds=term_timeout_seconds,
        kill_timeout_seconds=kill_timeout_seconds,
    )
    root_result = terminate_managed_process_identities(
        (snapshot.root,),
        term_timeout_seconds=term_timeout_seconds,
        kill_timeout_seconds=kill_timeout_seconds,
    )
    return {
        "terminated_pids": descendant_result["terminated_pids"],
        "failed_pids": sorted(
            set(descendant_result["failed_pids"]) | set(root_result["failed_pids"])
        ),
        "manager_terminated": root_result["terminated_pids"] == [pid],
    }


def quiesce_managed_process_descendants(
    pid: int,
    *,
    expected_identity: ManagedProcessIdentity | None = None,
    term_timeout_seconds: float = 5.0,
    kill_timeout_seconds: float = 1.0,
) -> dict[str, list[int]]:
    """Stop only a verified manager's current descendants, retaining its root."""

    snapshot = snapshot_managed_process_tree(pid, expected_identity=expected_identity)
    return terminate_managed_process_identities(
        snapshot.descendants,
        term_timeout_seconds=term_timeout_seconds,
        kill_timeout_seconds=kill_timeout_seconds,
    )


def _terminate_pid(
    pid: int,
    *,
    timeout_seconds: float = 1.5,
    expected_identity: ManagedProcessIdentity | None = None,
) -> bool:
    """Terminates a process and returns True when it no longer appears alive."""
    if pid <= 0:
        return False
    if expected_identity is not None:
        state = _identity_state(expected_identity)
        if state == "gone":
            return True
        if state != "matching":
            return False
    if os.name == "nt":
        identity = expected_identity or process_identity(pid)
        return identity is not None and _taskkill_process_tree(identity)
    if expected_identity is not None:
        identities = [expected_identity]
        _signal_process_identity(expected_identity, signal.SIGTERM)
        _terminated, remaining = _wait_for_process_identities(
            identities,
            timeout_seconds=timeout_seconds,
        )
        if remaining and hasattr(signal, "SIGKILL"):
            _signal_process_identity(expected_identity, signal.SIGKILL)
            _terminated, remaining = _wait_for_process_identities(
                identities,
                timeout_seconds=min(1.0, timeout_seconds),
            )
        return not remaining
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return False
    except PermissionError:
        return False
    except OSError, SystemError:
        return False

    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if not _pid_is_running(pid):
            return True
        time.sleep(0.05)

    with suppress(OSError, SystemError):
        if (
            expected_identity is not None
            and _identity_state(expected_identity) != "matching"
        ):
            return False
        if hasattr(signal, "SIGKILL"):
            os.kill(pid, signal.SIGKILL)

    time.sleep(0.1)
    return not _pid_is_running(pid)


def _terminate_pids_concurrently(
    identities: list[ManagedProcessIdentity],
) -> dict[int, bool]:
    """Terminate independent worker trees without serial timeout penalties."""

    results: dict[int, bool] = {}
    result_lock = threading.Lock()

    def terminate(identity: ManagedProcessIdentity) -> None:
        try:
            terminated = _terminate_pid(
                identity.pid,
                expected_identity=identity,
            )
        except Exception:  # noqa: BLE001 - one worker must not block cleanup.
            terminated = False
        with result_lock:
            results[identity.pid] = terminated

    threads = [
        threading.Thread(
            target=terminate,
            args=(identity,),
            name=f"carlina-orphan-cleanup-{identity.pid}",
            daemon=True,
        )
        for identity in identities
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return results


def cleanup_orphan_worker_processes(
    *,
    project_root: Path = PROJECT_ROOT,
    instance_dir: Path = INSTANCE_DIR,
    state_dir: Path | None = None,
    data_root: Path | None = None,
    installed_addons_dir: Path | None = None,
    addon_environments_dir: Path | None = None,
    current_pid: int | None = None,
    skip_if_managed_server_active: bool = True,
) -> dict[str, object]:
    """
    Terminates stale worker processes from this installation before spawning new workers.

    The postmortem fallback deliberately includes Pixi and managed-bootstrap
    wrappers. Candidates must carry this data root's opaque command-line
    marker; a shared program root alone is never enough to authorize a kill.
    Unmarked legacy worker shapes are only reported so startup can fail closed
    rather than duplicate them or risk cross-install cleanup.
    """
    active_pid = int(current_pid or os.getpid())
    canonical_state_dir = state_dir or instance_dir
    ownership_token = worker_ownership_token(data_root or instance_dir)
    try:
        process_entries = _iter_process_command_identities(include_non_python=True)
    except OSError as exc:
        return {
            "terminated_pids": [],
            "failed_pids": [],
            "residual_pids": [],
            "legacy_residual_pids": [],
            "skipped_reason": None,
            "inspection_error": str(exc),
        }
    process_map = {identity.pid: cmdline for identity, cmdline in process_entries}
    managed_server_pid = _read_managed_server_pid(canonical_state_dir)

    if (
        skip_if_managed_server_active
        and managed_server_pid
        and managed_server_pid != active_pid
        and _pid_is_running(managed_server_pid)
    ):
        managed_cmdline = process_map.get(managed_server_pid, "")
        if _is_project_manager_command(managed_cmdline, project_root):
            return {
                "terminated_pids": [],
                "failed_pids": [],
                "residual_pids": [],
                "legacy_residual_pids": [],
                "skipped_reason": (f"managed_server_active:{managed_server_pid}"),
                "inspection_error": None,
            }

    candidates: list[ManagedProcessIdentity] = []
    seen = set()
    for identity, command_line in process_entries:
        pid = identity.pid
        if pid <= 0 or pid == active_pid or pid in seen:
            continue
        if _is_project_worker_command(
            command_line,
            project_root,
            ownership_token=ownership_token,
            data_root=data_root,
            installed_addons_dir=installed_addons_dir,
            addon_environments_dir=addon_environments_dir,
        ):
            candidates.append(identity)
            seen.add(pid)

    termination_results = _terminate_pids_concurrently(candidates)
    candidate_pids = [identity.pid for identity in candidates]
    terminated_pids = [
        pid for pid in candidate_pids if termination_results.get(pid, False)
    ]
    failed_pids = [
        pid for pid in candidate_pids if not termination_results.get(pid, False)
    ]

    try:
        residual_entries = _iter_process_command_identities(include_non_python=True)
        residual_pids = [
            identity.pid
            for identity, command_line in residual_entries
            if identity.pid > 0
            and identity.pid != active_pid
            and _is_project_worker_command(
                command_line,
                project_root,
                ownership_token=ownership_token,
                data_root=data_root,
                installed_addons_dir=installed_addons_dir,
                addon_environments_dir=addon_environments_dir,
            )
        ]
        legacy_residual_pids = [
            identity.pid
            for identity, command_line in residual_entries
            if identity.pid > 0
            and identity.pid != active_pid
            and not _has_any_worker_ownership_marker(command_line)
            and _is_scoped_managed_worker_command(
                command_line,
                project_root,
                data_root=data_root,
                installed_addons_dir=installed_addons_dir,
                addon_environments_dir=addon_environments_dir,
            )
        ]
    except OSError as exc:
        return {
            "terminated_pids": terminated_pids,
            "failed_pids": failed_pids,
            "residual_pids": [],
            "legacy_residual_pids": [],
            "skipped_reason": None,
            "inspection_error": str(exc),
        }

    return {
        "terminated_pids": terminated_pids,
        "failed_pids": failed_pids,
        "residual_pids": residual_pids,
        "legacy_residual_pids": legacy_residual_pids,
        "skipped_reason": None,
        "inspection_error": None,
    }


def start_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=f"Start {PRODUCT_IDENTITY.name} with Waitress."
    )
    parser.add_argument(
        "--application-profile",
        choices=[profile.value for profile in RuntimeProfile],
        default=None,
        help="Select the application composition for this data root.",
    )
    parser.add_argument(
        "--host",
        default=product_environment_value("HOST", default="127.0.0.1"),
        help="Host interface to bind (defaults to loopback).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Port to bind.",
    )
    parser.add_argument(
        "--profile", choices=(LOCAL_PROFILE, SERVER_PROFILE), dest="web_profile"
    )
    parser.add_argument("--external-url")
    parser.add_argument("--trusted-hosts")
    parser.add_argument("--proxy-hops", type=int)
    parser.add_argument(
        "--open-browser",
        action="store_true",
        help="Open the app in the default browser.",
    )
    parser.add_argument(
        "--restart", action="store_true", help="Restart the server if it exits."
    )
    parser.add_argument(
        "--runtime-role",
        choices=(RUNTIME_ROLE_MANAGED, RUNTIME_ROLE_WEB),
        help=(
            "Service ownership role. Defaults to managed unless "
            "CARLINA_RUNTIME_ROLE explicitly selects web."
        ),
    )
    parser.add_argument(
        "--legacy-secret-file",
        metavar="PATH",
        help="Absolute, owner-only file containing the legacy Flask secret.",
    )
    args = parser.parse_args(argv)

    secret_scope = ExitStack()
    try:
        secret_scope.enter_context(
            legacy_secret_file_environment(args.legacy_secret_file)
        )
    except LegacySecretFileError as exc:
        secret_scope.close()
        print(f"Start refused: {exc}")
        return 2

    bootstrap_started_at = time.perf_counter()
    bootstrap_timings_ms: dict[str, float] = {}
    runtime_lease = None
    pid_file = None
    may_write_log = False
    try:
        paths = load_bootstrap_paths(profile=args.application_profile)
        check_python_version()
        try:
            composition = resolve_runtime_composition(args.application_profile)
            if args.port is None:
                args.port = int(
                    product_environment_value(
                        "PORT", default=str(composition.default_http_port)
                    )
                )
            bind_runtime_profile(paths.data_root, composition.profile)
        except RuntimeProfileError as exc:
            raise BootstrapError(str(exc)) from exc
        os.environ["CARLINA_PROFILE"] = composition.profile.value
        may_write_log = True
        ensure_runtime_dirs()
        web_policy = _configure_web_exposure(args)
        server_runtime_role = _resolve_server_runtime_role(args.runtime_role)
        runtime_lease = RuntimeLease(
            paths,
            host=args.host,
            port=args.port,
            runtime_role=server_runtime_role,
            web_profile=web_policy.profile,
            external_url=web_policy.external_url,
            trusted_hosts=web_policy.trusted_hosts,
            proxy_hops=web_policy.proxy_hops,
        )
        try:
            runtime_lease.acquire()
        except RuntimeOwnershipError as exc:
            raise BootstrapError(str(exc)) from exc
        os.environ["CARLINA_RUNTIME_ROLE"] = server_runtime_role
        pid_file = paths.state_dir / "bootstrap" / "server.pid"
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        pid_file.write_text(str(os.getpid()), encoding="utf-8")
        try:
            from iarf_assistant.core.restore_service import apply_pending_restore

            applied_restore = apply_pending_restore(paths)
        except Exception as exc:
            raise BootstrapError(
                f"Pending restore could not be applied safely: {exc}"
            ) from exc
        if applied_restore is not None:
            print(
                "Applied verified backup "
                f"{applied_restore.pending.archive_filename} to "
                f"{applied_restore.restore.destination_root}."
            )
            paths = load_bootstrap_paths(profile=composition.profile)
            try:
                bind_runtime_profile(paths.data_root, composition.profile)
            except RuntimeProfileError as exc:
                raise BootstrapError(str(exc)) from exc
        bootstrap_timings_ms["ownership_and_restore"] = round(
            (time.perf_counter() - bootstrap_started_at) * 1000,
            3,
        )
        phase_started_at = time.perf_counter()
        frontend_status = inspect_frontend_workspace(
            PROJECT_ROOT,
            verify_sources=_runtime_frontend_verifies_sources(),
        )
        bootstrap_timings_ms["frontend_inspection"] = round(
            (time.perf_counter() - phase_started_at) * 1000,
            3,
        )
        reasons = setup_required_reasons(
            frontend_status=frontend_status,
            paths=paths,
        )
        if reasons:
            print("Setup is incomplete:")
            for reason in reasons:
                print(f"- {reason}")
            print("Run: python scripts/setup_dev.py")
            return 2

        degraded_addons = [
            status
            for status in frontend_status.get("addons", [])
            if status.get("status") != "built"
        ]
        if degraded_addons:
            addon_labels = ", ".join(
                str(status.get("addon_id") or "unknown") for status in degraded_addons
            )
            warning = (
                "Addon frontend assets need repair and those addons may be "
                f"degraded: {addon_labels}. Run "
                "'python scripts/manage.py --rebuild-frontend'."
            )
            print(f"Warning: {warning}")
            _append_log(START_LOG, f"[{_timestamp()}] WARNING: {warning}\n")

        phase_started_at = time.perf_counter()
        try:
            from waitress import serve

            from iarf_assistant.app import create_app
        except Exception as exc:
            raise BootstrapError(f"Could not import app dependencies: {exc}") from exc
        bootstrap_timings_ms["application_imports"] = round(
            (time.perf_counter() - phase_started_at) * 1000,
            3,
        )

        os.environ["FLASK_APP"] = "iarf_assistant.wsgi"
        restart_command = configure_restart_environment(args.host, args.port)
        url = web_policy.external_url or _browser_url(args.host, args.port)
        _append_log(
            START_LOG,
            f"\n[{_timestamp()}] Starting on {url} with Python {sys.executable}\n",
        )
        _append_log(
            START_LOG,
            f"[{_timestamp()}] Managed restart command: {_display_command(restart_command)}\n",
        )
        print(f"Starting {PRODUCT_IDENTITY.name} on {url}")
        if server_runtime_role == RUNTIME_ROLE_WEB:
            print("Web-only runtime enabled: schedulers and workers are disabled.")
        if web_policy.profile == SERVER_PROFILE:
            print(
                "Server profile enabled: Carlina trusts the configured reverse "
                "proxy hops and is no longer loopback-only."
            )
        print(f"Writing start log: {START_LOG}")

        if args.open_browser:
            threading.Timer(1.5, lambda: webbrowser.open(url)).start()

        while True:
            iteration_started_at = time.perf_counter()
            phase_started_at = time.perf_counter()
            cleanup = cleanup_orphan_worker_processes(
                project_root=paths.program_root,
                instance_dir=paths.instance_dir,
                state_dir=paths.state_dir,
                data_root=paths.data_root,
                installed_addons_dir=paths.installed_addons_dir,
                addon_environments_dir=paths.addon_environments_dir,
            )
            skipped_reason = cleanup.get("skipped_reason")
            terminated_pids = [int(pid) for pid in cleanup.get("terminated_pids", [])]
            failed_pids = [int(pid) for pid in cleanup.get("failed_pids", [])]
            residual_pids = [int(pid) for pid in cleanup.get("residual_pids", [])]
            legacy_residual_pids = [
                int(pid) for pid in cleanup.get("legacy_residual_pids", [])
            ]
            inspection_error = str(cleanup.get("inspection_error") or "").strip()
            if skipped_reason:
                _append_log(
                    START_LOG,
                    f"[{_timestamp()}] Skipped orphan worker cleanup ({skipped_reason}).\n",
                )
            elif terminated_pids:
                terminated_sample = ", ".join(str(pid) for pid in terminated_pids[:12])
                extra = " ..." if len(terminated_pids) > 12 else ""
                message = (
                    f"Orphan worker cleanup terminated {len(terminated_pids)} process(es): "
                    f"{terminated_sample}{extra}"
                )
                print(message)
                _append_log(START_LOG, f"[{_timestamp()}] {message}\n")
            if failed_pids:
                failed_sample = ", ".join(str(pid) for pid in failed_pids[:12])
                extra = " ..." if len(failed_pids) > 12 else ""
                warning = (
                    f"Orphan worker cleanup could not terminate {len(failed_pids)} process(es): "
                    f"{failed_sample}{extra}"
                )
                print(f"Warning: {warning}")
                _append_log(START_LOG, f"[{_timestamp()}] WARNING: {warning}\n")
            unsafe_pids = sorted(
                set(failed_pids) | set(residual_pids) | set(legacy_residual_pids)
            )
            if inspection_error or unsafe_pids:
                details: list[str] = []
                owned_residual_pids = sorted(set(failed_pids) | set(residual_pids))
                if owned_residual_pids:
                    details.append(
                        "managed worker residuals "
                        + ", ".join(str(pid) for pid in owned_residual_pids)
                    )
                if legacy_residual_pids:
                    details.append(
                        "unmarked legacy worker(s) cannot be attributed safely: "
                        + ", ".join(
                            str(pid) for pid in sorted(set(legacy_residual_pids))
                        )
                    )
                if inspection_error:
                    details.append(f"process inspection failed: {inspection_error}")
                raise BootstrapError(
                    "Refusing to start before application initialization because "
                    + "; ".join(details)
                    + "."
                )
            bootstrap_timings_ms["orphan_worker_cleanup"] = round(
                (time.perf_counter() - phase_started_at) * 1000,
                3,
            )

            # Ensure the port is free before initializing app and spawning workers
            phase_started_at = time.perf_counter()
            _wait_for_port_to_be_free(args.host, args.port)
            bootstrap_timings_ms["port_wait"] = round(
                (time.perf_counter() - phase_started_at) * 1000,
                3,
            )

            phase_started_at = time.perf_counter()
            app = create_app()
            bootstrap_timings_ms["application_factory"] = round(
                (time.perf_counter() - phase_started_at) * 1000,
                3,
            )
            phase_started_at = time.perf_counter()
            try:
                from iarf_assistant.core.mcp_gateway_runtime import (
                    start_configured_mcp_gateway,
                )

                mcp_gateway = start_configured_mcp_gateway(
                    app,
                    main_port=args.port,
                )
            except Exception as exc:
                raise BootstrapError(
                    f"The configured MCP gateway could not start safely: {exc}"
                ) from exc
            bootstrap_timings_ms["mcp_gateway"] = round(
                (time.perf_counter() - phase_started_at) * 1000,
                3,
            )
            if mcp_gateway is not None:
                mcp_host = (
                    f"[{mcp_gateway.host}]"
                    if ":" in mcp_gateway.host
                    else mcp_gateway.host
                )
                mcp_url = f"http://{mcp_host}:{mcp_gateway.port}/mcp"
                print(f"MCP gateway listening on {mcp_url}")
                _append_log(
                    START_LOG,
                    f"[{_timestamp()}] MCP gateway listening on {mcp_url}\n",
                )
            bootstrap_timings_ms["iteration_before_serve"] = round(
                (time.perf_counter() - iteration_started_at) * 1000,
                3,
            )
            bootstrap_timings_ms["process_total_before_serve"] = round(
                (time.perf_counter() - bootstrap_started_at) * 1000,
                3,
            )
            _append_log(
                START_LOG,
                f"[{_timestamp()}] Startup timings_ms="
                f"{json.dumps(bootstrap_timings_ms, sort_keys=True)}\n",
            )
            try:
                serve(
                    app,
                    host=args.host,
                    port=args.port,
                    max_request_body_size=5 * 1024 * 1024 * 1024,
                    channel_timeout=10800,
                    **waitress_proxy_options(web_policy),
                )
            finally:
                if mcp_gateway is not None:
                    mcp_gateway.stop()
            if not args.restart:
                return 0
            print("Server exited. Restarting in 2 seconds...")
            _append_log(START_LOG, f"[{_timestamp()}] Server exited; restarting.\n")
            time.sleep(2)
    except BootstrapError as exc:
        print(f"Start failed: {exc}")
        if may_write_log:
            _append_log(START_LOG, f"[{_timestamp()}] ERROR: {exc}\n")
        return 1
    finally:
        if pid_file is not None:
            pid_file.unlink(missing_ok=True)
        if runtime_lease is not None:
            runtime_lease.release()
        secret_scope.close()


if __name__ == "__main__":
    raise SystemExit(setup_main())
