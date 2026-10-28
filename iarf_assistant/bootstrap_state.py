from __future__ import annotations

import importlib.metadata
import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from carlina.product_identity import (
    PRODUCT_IDENTITY,
    product_environment_value,
)
from iarf_assistant.bootstrap import PROJECT_ROOT, SUPPORTED_PYTHON, _env_has_value
from iarf_assistant.core.app_paths import AppPaths
from iarf_assistant.core.frontend_orchestration import inspect_frontend_workspace
from iarf_assistant.core.runtime_ownership import verified_runtime_leader

ACTION_OPEN = "open_browser"
ACTION_SETUP = "setup"
ACTION_CREATE_ADMIN = "create_admin"
ACTION_START = "start"
ACTION_MANAGE = "manage"
_DEFAULT_DOCKER_STATUS_TIMEOUT_SECONDS = 2.5


@dataclass
class StateCheck:
    id: str
    label: str
    ok: bool
    detail: str
    required: bool = True


@dataclass
class InstallState:
    project_root: str
    url: str
    running: bool
    summary: str
    recommended_action: str
    checks: list[StateCheck] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def required_failures(self) -> list[StateCheck]:
        return [check for check in self.checks if check.required and not check.ok]

    @property
    def setup_complete(self) -> bool:
        return not self.required_failures


def manager_state_path(
    project_root: Path = PROJECT_ROOT,
    *,
    paths: AppPaths | None = None,
) -> Path:
    if paths is not None:
        return paths.state_dir / "bootstrap" / "state.json"
    return project_root / "instance" / "bootstrap" / "state.json"


def write_manager_state(
    data: dict,
    project_root: Path = PROJECT_ROOT,
    *,
    paths: AppPaths | None = None,
) -> None:
    path = manager_state_path(project_root, paths=paths)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = read_manager_state(project_root, paths=paths)
    existing.update(data)
    existing["updated_at"] = datetime.now().isoformat(timespec="seconds")
    path.write_text(json.dumps(existing, indent=2, sort_keys=True), encoding="utf-8")


def read_manager_state(
    project_root: Path = PROJECT_ROOT,
    *,
    paths: AppPaths | None = None,
) -> dict:
    path = manager_state_path(project_root, paths=paths)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def app_url(host: str = "127.0.0.1", port: int = 5003) -> str:
    browser_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    return f"http://{browser_host}:{port}/"


def is_app_running(url: str, timeout: float = 1.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return 200 <= response.status < 500
    except urllib.error.HTTPError as exc:
        return 200 <= exc.code < 500
    except OSError:
        return False


def is_runtime_running(paths: AppPaths, host: str, port: int) -> bool:
    """Probe an endpoint only after this data root proves ownership of it."""

    leader = verified_runtime_leader(paths)
    if leader is None:
        return False

    leader_host = leader.get("host")
    leader_port = leader.get("port")
    if not isinstance(host, str) or not host.strip():
        return False
    if type(port) is not int or not 1 <= port <= 65535:
        return False
    if not isinstance(leader_host, str) or not leader_host.strip():
        return False
    if type(leader_port) is not int or not 1 <= leader_port <= 65535:
        return False
    if app_url(leader_host, leader_port) != app_url(host, port):
        return False
    return is_app_running(app_url(host, port))


def package_version() -> str | None:
    for distribution_name in (
        PRODUCT_IDENTITY.distribution_name,
        "iarf-assistant",
    ):
        try:
            return importlib.metadata.version(distribution_name)
        except importlib.metadata.PackageNotFoundError:
            continue
    return None


def _sqlite_scalar(db_path: Path, sql: str) -> object | None:
    connection = None
    try:
        connection = sqlite3.connect(str(db_path))
        cursor = connection.execute(sql)
        row = cursor.fetchone()
        cursor.close()
        return row[0] if row else None
    except sqlite3.Error:
        return None
    finally:
        if connection is not None:
            connection.close()


def _table_exists(db_path: Path, table_name: str) -> bool:
    value = _sqlite_scalar(
        db_path,
        "select name from sqlite_master where type='table' and name='%s'" % table_name.replace("'", "''"),
    )
    return value == table_name


def admin_user_exists(db_path: Path) -> bool:
    if not db_path.exists() or not _table_exists(db_path, "user"):
        return False
    value = _sqlite_scalar(db_path, "select count(*) from user where is_admin = 1")
    return bool(value)


def current_db_revisions(db_path: Path) -> set[str]:
    if not db_path.exists() or not _table_exists(db_path, "alembic_version"):
        return set()
    connection = None
    try:
        connection = sqlite3.connect(str(db_path))
        cursor = connection.execute("select version_num from alembic_version")
        revisions = {str(row[0]) for row in cursor}
        cursor.close()
        return revisions
    except sqlite3.Error:
        return set()
    finally:
        if connection is not None:
            connection.close()


def migration_heads(project_root: Path = PROJECT_ROOT) -> set[str]:
    versions_dir = project_root / "iarf_assistant" / "migrations" / "versions"
    if not versions_dir.is_dir():
        versions_dir = project_root / "migrations" / "versions"
    revisions: set[str] = set()
    parents: set[str] = set()
    for path in versions_dir.glob("*.py"):
        revision = None
        down_revision = None
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("revision ="):
                revision = stripped.split("=", 1)[1].strip().strip("'\"")
            elif stripped.startswith("down_revision ="):
                raw = stripped.split("=", 1)[1].strip()
                if raw not in {"None", "null"}:
                    down_revision = raw.strip("'\"")
            if revision and down_revision is not None:
                break
        if revision:
            revisions.add(revision)
        if down_revision:
            parents.add(down_revision)
    return revisions - parents


def migrations_current(db_path: Path, project_root: Path = PROJECT_ROOT) -> bool:
    heads = migration_heads(project_root)
    if not heads:
        return db_path.exists()
    return heads.issubset(current_db_revisions(db_path))


def migration_status_detail(db_path: Path, project_root: Path = PROJECT_ROOT) -> str:
    heads = migration_heads(project_root)
    if not db_path.exists():
        return "missing database"
    if not heads:
        return "current"
    revisions = current_db_revisions(db_path)
    if heads.issubset(revisions):
        return "current"
    if not revisions:
        return "not initialized"
    missing = ", ".join(sorted(heads - revisions))
    current = ", ".join(sorted(revisions))
    return f"needs upgrade (current {current}; missing head {missing})"


def _check_env(project_root: Path) -> StateCheck:
    env_path = project_root / ".env"
    if not env_path.exists():
        return StateCheck("env", ".env secret", False, "missing .env")
    try:
        text = env_path.read_text(encoding="utf-8")
    except OSError as exc:
        return StateCheck("env", ".env secret", False, f"cannot read .env: {exc}")
    if not _env_has_value(text, "FLASK_SECRET_KEY"):
        return StateCheck("env", ".env secret", False, "missing FLASK_SECRET_KEY")
    return StateCheck("env", ".env secret", True, "configured")


def _docker_status_timeout_seconds() -> float:
    raw = str(
        product_environment_value(
            "DOCKER_STATUS_TIMEOUT_SECONDS",
            default="",
        )
    ).strip()
    if not raw:
        return _DEFAULT_DOCKER_STATUS_TIMEOUT_SECONDS
    try:
        parsed = float(raw)
    except ValueError:
        return _DEFAULT_DOCKER_STATUS_TIMEOUT_SECONDS
    return max(0.5, parsed)


def _docker_unavailable_detail(raw_detail: str) -> str:
    detail = (raw_detail or "").strip()
    lower = detail.lower()
    if os.name == "nt" and "wsl" in lower:
        return "optional; Docker daemon unreachable (WSL is unavailable)"
    if "cannot connect to the docker daemon" in lower or "error during connect" in lower:
        return "optional; Docker daemon unreachable"
    if "is the docker daemon running" in lower:
        return "optional; Docker daemon is not running"
    if not detail:
        return "optional; Docker daemon unavailable"
    first_line = detail.splitlines()[0].strip()
    if len(first_line) > 180:
        first_line = first_line[:177].rstrip() + "..."
    return f"optional; {first_line}"


def _docker_available_detail(engine_version: str, backend: str = "") -> str:
    backend_suffix = f" via {backend}" if backend else ""
    return f"available{backend_suffix} (engine {engine_version})" if engine_version else f"available{backend_suffix}"


def _run_docker_version_probe(cmd: list[str], timeout: float, *, backend: str = "") -> StateCheck:
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        timeout_hint = int(timeout) if timeout.is_integer() else timeout
        backend_suffix = f" via {backend}" if backend else ""
        return StateCheck(
            "docker",
            "Docker Engine",
            False,
            f"optional; Docker status check{backend_suffix} timed out after {timeout_hint}s (daemon or WSL may be unavailable)",
            required=False,
        )
    except OSError as exc:
        return StateCheck(
            "docker",
            "Docker Engine",
            False,
            f"optional; Docker status check failed: {exc}",
            required=False,
        )

    if proc.returncode != 0:
        detail = proc.stderr or proc.stdout or ""
        if backend and not detail.strip():
            detail = f"Docker CLI not found {backend}"
        return StateCheck(
            "docker",
            "Docker Engine",
            False,
            _docker_unavailable_detail(detail),
            required=False,
        )

    engine_version = (proc.stdout or "").strip() or (proc.stderr or "").strip()
    return StateCheck("docker", "Docker Engine", True, _docker_available_detail(engine_version, backend), required=False)


def _wsl_docker_command() -> list[str] | None:
    if os.name != "nt":
        return None
    wsl_exe = shutil.which("wsl.exe")
    if wsl_exe is None:
        return None
    distro = str(
        product_environment_value("DOCKER_WSL_DISTRO", default="")
    ).strip()
    cmd = [wsl_exe]
    if distro:
        cmd.extend(["-d", distro])
    cmd.extend(["sh", "-lc", "command -v docker >/dev/null 2>&1 && docker version --format '{{.Server.Version}}'"])
    return cmd


def _check_wsl_docker_engine(timeout: float) -> StateCheck | None:
    cmd = _wsl_docker_command()
    if cmd is None:
        return None
    return _run_docker_version_probe(cmd, timeout, backend="WSL")


def _check_docker_engine() -> StateCheck:
    timeout = _docker_status_timeout_seconds()
    docker_exe = shutil.which("docker")
    if docker_exe is None:
        wsl_check = _check_wsl_docker_engine(timeout)
        if wsl_check is not None:
            return wsl_check
        return StateCheck(
            "docker",
            "Docker Engine",
            False,
            "optional; Docker CLI not found (Docker-backed addons disabled)",
            required=False,
        )

    native_check = _run_docker_version_probe([docker_exe, "version", "--format", "{{.Server.Version}}"], timeout)
    if native_check.ok:
        return native_check

    wsl_check = _check_wsl_docker_engine(timeout)
    if wsl_check is not None and wsl_check.ok:
        return wsl_check
    return native_check


def _frontend_state_checks(project_root: Path) -> list[StateCheck]:
    if not (project_root / "package.json").is_file():
        return [
            StateCheck(
                "frontend_core",
                "Svelte core assets",
                True,
                "not declared in this package",
            ),
            StateCheck(
                "frontend_addons",
                "Addon frontend assets",
                True,
                "no repository workspace declarations",
                required=False,
            ),
        ]

    workspace = inspect_frontend_workspace(project_root)
    core = workspace.get("core") or {}
    addon_statuses = workspace.get("addons") or []
    degraded = [
        status
        for status in addon_statuses
        if status.get("status") != "built"
    ]
    return [
        StateCheck(
            "frontend_core",
            "Svelte core assets",
            core.get("status") == "built",
            (
                "built"
                if core.get("status") == "built"
                else "; ".join(core.get("reasons") or ["repair required"])
            ),
        ),
        StateCheck(
            "frontend_addons",
            "Addon frontend assets",
            not degraded,
            (
                f"{len(addon_statuses)} declared build(s) current"
                if not degraded
                else "optional degradation; repair: "
                + ", ".join(
                    str(status.get("addon_id") or "unknown")
                    for status in degraded
                )
            ),
            required=False,
        ),
    ]


def detect_install_state(
    *,
    project_root: Path = PROJECT_ROOT,
    host: str = "127.0.0.1",
    port: int = 5003,
    paths: AppPaths | None = None,
) -> InstallState:
    db_path = paths.database_file if paths is not None else project_root / "instance" / "iarf_ai.db"
    output_css = (
        paths.package_root / "static" / "output.css"
        if paths is not None
        else project_root / "iarf_assistant" / "static" / "output.css"
    )
    url = app_url(host, port)
    version = package_version()

    checks = [
        StateCheck(
            "python",
            "Python version",
            tuple(sys.version_info[:2]) == SUPPORTED_PYTHON,
            ".".join(str(part) for part in sys.version_info[:3]),
        ),
        StateCheck(
            "package_import",
            "App import",
            importlib.util.find_spec("iarf_assistant") is not None,
            "importable" if importlib.util.find_spec("iarf_assistant") is not None else "not importable",
        ),
        StateCheck(
            "package_install",
            "Package install",
            version is not None,
            version or "not installed editable/package",
        ),
        (
            StateCheck(
                "env",
                "Persistent session key",
                paths.session_key_file.is_file(),
                (
                    "configured"
                    if paths.session_key_file.is_file()
                    else f"missing {paths.session_key_file}"
                ),
            )
            if paths is not None
            else _check_env(project_root)
        ),
        StateCheck("database", "Database", db_path.exists(), "present" if db_path.exists() else "missing"),
        StateCheck(
            "migrations",
            "Database migrations",
            migrations_current(db_path, project_root),
            migration_status_detail(db_path, project_root),
        ),
        StateCheck(
            "admin",
            "Admin user",
            admin_user_exists(db_path),
            "exists" if admin_user_exists(db_path) else "missing",
        ),
        StateCheck(
            "tailwind",
            "Legacy stylesheet",
            output_css.exists(),
            "built" if output_css.exists() else "missing output.css",
        ),
        *_frontend_state_checks(project_root),
        _check_docker_engine(),
    ]

    running = (
        is_app_running(url)
        if paths is None
        else is_runtime_running(paths, host, port)
    )
    required_failures = [check for check in checks if check.required and not check.ok]
    missing_ids = {check.id for check in required_failures}

    if missing_ids == {"admin"}:
        summary = "Admin setup needed"
        recommended = ACTION_CREATE_ADMIN
    elif running:
        summary = "Running"
        recommended = ACTION_OPEN
    elif not required_failures:
        summary = "Ready to start"
        recommended = ACTION_START
    elif "package_install" in missing_ids or "database" in missing_ids or "migrations" in missing_ids:
        summary = "Setup or repair needed"
        recommended = ACTION_SETUP
    else:
        summary = "Setup incomplete"
        recommended = ACTION_SETUP

    warnings = [check.detail for check in checks if not check.required and not check.ok]
    return InstallState(
        project_root=str(project_root),
        url=url,
        running=running,
        summary=summary,
        recommended_action=recommended,
        checks=checks,
        warnings=warnings,
    )


def state_to_jsonable(state: InstallState) -> dict:
    return asdict(state)
