"""Read-mostly installation diagnostics for operators and support bundles."""

from __future__ import annotations

import ast
import json
import os
import shutil
import sqlite3
import stat
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from iarf_assistant.core.addons.discovery import discover_addons
from iarf_assistant.core.app_paths import AppPaths
from iarf_assistant.core.backup_service import (
    BackupError,
    inspect_backup,
    inspect_database,
    verify_backup,
)
from iarf_assistant.core.encryption import EncryptionError, InstallationKeyStore
from iarf_assistant.core.migration_runtime import migrations_directory
from iarf_assistant.core.persistence.sqlite import sqlite_read_only_uri
from iarf_assistant.core.restore_service import (
    PendingRestoreError,
    load_pending_restore,
)
from iarf_assistant.core.runtime_ownership import (
    load_runtime_leader,
    verified_runtime_leader,
)
from iarf_assistant.core.web_security import (
    WebSecurityConfigurationError,
    resolve_web_security_policy,
)

DoctorStatus = Literal["ok", "info", "warning", "error"]
_STATUS_ORDER: dict[DoctorStatus, int] = {
    "ok": 0,
    "info": 1,
    "warning": 2,
    "error": 3,
}


@dataclass(frozen=True, slots=True)
class DoctorCheck:
    id: str
    title: str
    status: DoctorStatus
    summary: str
    details: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class DoctorReport:
    generated_at: str
    deep: bool
    layout: dict[str, Any]
    checks: tuple[DoctorCheck, ...]

    @property
    def errors(self) -> tuple[DoctorCheck, ...]:
        return tuple(check for check in self.checks if check.status == "error")

    @property
    def warnings(self) -> tuple[DoctorCheck, ...]:
        return tuple(check for check in self.checks if check.status == "warning")

    def exit_code(self, *, strict: bool = False) -> int:
        if self.errors:
            return 1
        if strict and self.warnings:
            return 2
        return 0

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["summary"] = {
            "errors": len(self.errors),
            "warnings": len(self.warnings),
            "ok": sum(check.status == "ok" for check in self.checks),
            "info": sum(check.status == "info" for check in self.checks),
        }
        return payload


def _check(
    check_id: str,
    title: str,
    status: DoctorStatus,
    summary: str,
    **details: Any,
) -> DoctorCheck:
    return DoctorCheck(check_id, title, status, summary, details or None)


def _migration_value(path: Path, name: str) -> str | tuple[str, ...] | None:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return None
    for node in tree.body:
        target_name = None
        value_node: ast.expr | None = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name):
                target_name = target.id
            value_node = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target_name = node.target.id
            value_node = node.value
        if target_name != name or value_node is None:
            continue
        try:
            value = ast.literal_eval(value_node)
        except (ValueError, TypeError):
            return None
        if value is None or isinstance(value, str):
            return value
        if isinstance(value, (tuple, list)) and all(
            isinstance(item, str) for item in value
        ):
            return tuple(value)
    return None


def migration_heads(migrations_dir: Path) -> set[str]:
    versions = migrations_dir / "versions"
    revisions: set[str] = set()
    parents: set[str] = set()
    if not versions.is_dir():
        return set()
    for path in versions.glob("*.py"):
        revision = _migration_value(path, "revision")
        down_revision = _migration_value(path, "down_revision")
        if isinstance(revision, str):
            revisions.add(revision)
        if isinstance(down_revision, str):
            parents.add(down_revision)
        elif isinstance(down_revision, tuple):
            parents.update(down_revision)
    return revisions - parents


def _database_revisions(path: Path) -> set[str]:
    connection = sqlite3.connect(sqlite_read_only_uri(path), uri=True)
    try:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='alembic_version'"
        ).fetchone()
        if not exists:
            return set()
        return {
            str(row[0])
            for row in connection.execute("SELECT version_num FROM alembic_version")
        }
    finally:
        connection.close()


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (name,),
        ).fetchone()
        is not None
    )


def _database_runtime_facts(path: Path) -> dict[str, Any]:
    connection = sqlite3.connect(sqlite_read_only_uri(path), uri=True)
    try:
        addon_rows: list[dict[str, Any]] = []
        if _table_exists(connection, "addons"):
            addon_rows = [
                {
                    "id": str(row[0]),
                    "active": bool(row[1]),
                    "version": str(row[2]),
                }
                for row in connection.execute(
                    "SELECT id, is_active, version FROM addons ORDER BY id"
                )
            ]
        admin_count: int | None = None
        if _table_exists(connection, "user"):
            admin_count = int(
                connection.execute(
                    "SELECT count(*) FROM user WHERE is_admin = 1"
                ).fetchone()[0]
            )
        return {"addons": addon_rows, "admin_count": admin_count}
    finally:
        connection.close()


def _path_check(paths: AppPaths) -> DoctorCheck:
    missing = [str(path) for path in paths.owned_directories if not path.is_dir()]
    unwritable = [
        str(path)
        for path in paths.owned_directories
        if path.is_dir() and not os.access(path, os.W_OK)
    ]
    if missing or unwritable:
        return _check(
            "storage.paths",
            "Storage paths",
            "error",
            "Owned storage paths are missing or not writable.",
            missing=missing,
            unwritable=unwritable,
        )
    status: DoctorStatus = "warning" if paths.is_legacy else "ok"
    summary = (
        "Legacy checkout-local data mode is active; run carlina migrate-data."
        if paths.is_legacy
        else f"{paths.mode} layout is writable."
    )
    return _check(
        "storage.paths",
        "Storage paths",
        status,
        summary,
        mode=paths.mode,
        data_root=str(paths.data_root),
        database=str(paths.database_file),
    )


def _database_checks(paths: AppPaths, migrations_dir: Path) -> list[DoctorCheck]:
    database = paths.database_file
    if not database.is_file():
        return [
            _check(
                "database.integrity",
                "Database integrity",
                "error",
                f"Database is missing: {database}",
            )
        ]
    checks: list[DoctorCheck] = []
    try:
        report = inspect_database(database)
        violation_count = int(report["foreign_key_violation_count"])
        checks.append(
            _check(
                "database.integrity",
                "Database integrity",
                "error" if violation_count else "ok",
                (
                    f"SQLite quick_check passed, but {violation_count} foreign-key violation(s) exist."
                    if violation_count
                    else "SQLite quick_check and foreign-key validation passed."
                ),
                size_bytes=database.stat().st_size,
                foreign_key_violation_count=violation_count,
                foreign_key_violation_sample=report["foreign_key_violation_sample"],
            )
        )
        journal_mode = str(report["journal_mode"]).casefold()
        wal_path = database.with_name(f"{database.name}-wal")
        wal_size = wal_path.stat().st_size if wal_path.is_file() else 0
        wal_limit = max(512 * 1024**2, database.stat().st_size // 2)
        wal_status: DoctorStatus = "ok"
        wal_summary = f"Journal mode is {journal_mode}; WAL size is {wal_size} bytes."
        if journal_mode != "wal":
            wal_status = "warning"
            wal_summary = (
                f"Journal mode is {journal_mode}; the app will request WAL on startup."
            )
        elif wal_size > wal_limit:
            wal_status = "warning"
            wal_summary = f"WAL is unusually large ({wal_size} bytes); inspect long-lived readers."
        checks.append(
            _check(
                "database.wal",
                "SQLite WAL",
                wal_status,
                wal_summary,
                journal_mode=journal_mode,
                wal_size_bytes=wal_size,
                warning_threshold_bytes=wal_limit,
            )
        )
    except (BackupError, OSError, sqlite3.Error) as exc:
        return [
            _check(
                "database.integrity",
                "Database integrity",
                "error",
                f"Database validation failed: {exc}",
            )
        ]

    expected_heads = migration_heads(migrations_dir)
    try:
        current = _database_revisions(database)
    except sqlite3.Error as exc:
        checks.append(
            _check(
                "database.migrations",
                "Database migrations",
                "error",
                f"Could not read migration state: {exc}",
            )
        )
    else:
        missing = sorted(expected_heads - current)
        if not expected_heads:
            checks.append(
                _check(
                    "database.migrations",
                    "Database migrations",
                    "warning",
                    "No packaged migration heads were found.",
                    migrations_dir=str(migrations_dir),
                    current=sorted(current),
                )
            )
        elif missing:
            checks.append(
                _check(
                    "database.migrations",
                    "Database migrations",
                    "error",
                    "Database is behind the packaged migration heads.",
                    current=sorted(current),
                    expected=sorted(expected_heads),
                    missing=missing,
                )
            )
        else:
            checks.append(
                _check(
                    "database.migrations",
                    "Database migrations",
                    "ok",
                    "Database contains every packaged migration head.",
                    current=sorted(current),
                    expected=sorted(expected_heads),
                )
            )
    return checks


def _key_checks(paths: AppPaths) -> list[DoctorCheck]:
    store = InstallationKeyStore(paths.master_keys_file)
    checks: list[DoctorCheck] = []
    try:
        metadata = store.public_metadata()
    except (EncryptionError, OSError, ValueError) as exc:
        return [
            _check(
                "encryption.keys",
                "Encryption keys",
                "error",
                f"Installation keys are unavailable or invalid: {exc}",
                key_file=str(paths.master_keys_file),
            )
        ]
    checks.append(
        _check(
            "encryption.keys",
            "Encryption keys",
            "ok",
            f"Active key {metadata['active_key_id']} is available.",
            backend=metadata["backend"],
            key_count=len(metadata["keys"]),
            active_key_id=metadata["active_key_id"],
        )
    )
    checks.append(
        _check(
            "encryption.backend",
            "Key protection",
            "warning",
            "Keys use the restricted-file fallback; export passphrase-protected recovery material and protect the host account.",
            backend=metadata["backend"],
            platform=sys.platform,
        )
    )
    if not paths.session_key_file.is_file():
        checks.append(
            _check(
                "encryption.session-key",
                "Session key",
                "error",
                f"Persistent session key is missing: {paths.session_key_file}",
            )
        )
    else:
        checks.append(
            _check(
                "encryption.session-key",
                "Session key",
                "ok",
                "Persistent Flask session key is present.",
            )
        )
    if os.name != "nt":
        insecure: list[str] = []
        for path in (paths.master_keys_file, paths.session_key_file):
            if path.exists() and stat.S_IMODE(path.stat().st_mode) & 0o077:
                insecure.append(str(path))
        if insecure:
            checks.append(
                _check(
                    "encryption.permissions",
                    "Secret permissions",
                    "error",
                    "Secret files are accessible beyond their owner.",
                    paths=insecure,
                )
            )
    return checks


def _backup_check(
    paths: AppPaths,
    *,
    deep: bool,
    key_store: InstallationKeyStore | None,
) -> DoctorCheck:
    archives = sorted(
        (path for path in paths.backups_dir.glob("*.zip") if path.is_file()),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )
    if not archives:
        return _check(
            "continuity.backup",
            "Recent backup",
            "warning",
            "No complete Carlina backup was found.",
            backup_dir=str(paths.backups_dir),
        )
    latest = archives[0]
    try:
        manifest = (
            verify_backup(
                latest,
                available_key_ids=(key_store.key_ids() if key_store else None),
            )
            if deep
            else inspect_backup(latest)
        )
    except (BackupError, OSError, ValueError) as exc:
        return _check(
            "continuity.backup",
            "Recent backup",
            "error",
            f"Latest backup is invalid: {exc}",
            path=str(latest),
            deep=deep,
        )
    age = datetime.now(UTC) - datetime.fromtimestamp(latest.stat().st_mtime, UTC)
    status: DoctorStatus = "warning" if age > timedelta(days=7) else "ok"
    return _check(
        "continuity.backup",
        "Recent backup",
        status,
        (
            f"Latest backup is {age.days} day(s) old."
            if status == "warning"
            else "A recent backup is available."
        ),
        path=str(latest),
        backup_id=manifest.get("backup_id"),
        created_at=manifest.get("created_at"),
        age_seconds=int(age.total_seconds()),
        deep_verified=deep,
    )


def _restore_check(paths: AppPaths) -> DoctorCheck:
    try:
        pending = load_pending_restore(paths)
    except PendingRestoreError as exc:
        return _check(
            "continuity.restore",
            "Pending restore",
            "error",
            f"Pending restore record is invalid: {exc}",
        )
    if pending is None:
        return _check(
            "continuity.restore",
            "Pending restore",
            "ok",
            "No restore is pending.",
        )
    return _check(
        "continuity.restore",
        "Pending restore",
        "warning",
        f"Backup {pending.archive_filename} is verified and awaiting offline activation.",
        backup_id=pending.backup_id,
        requested_at=pending.requested_at,
    )


def _active_runtime_leader(paths: AppPaths) -> dict[str, object] | None:
    return verified_runtime_leader(paths)


def _runtime_leader_check(paths: AppPaths) -> DoctorCheck:
    leader = load_runtime_leader(paths)
    if leader is None:
        return _check(
            "runtime.leader",
            "Runtime ownership",
            "info",
            "No Carlina runtime currently owns this data root.",
        )
    try:
        pid = int(leader.get("pid") or 0)
    except (TypeError, ValueError):
        pid = 0
    if verified_runtime_leader(paths) is not None:
        runtime_role = str(leader.get("runtime_role") or "").strip().lower()
        runtime_label = (
            f"{runtime_role.capitalize()} runtime"
            if runtime_role
            else "Carlina runtime"
        )
        return _check(
            "runtime.leader",
            "Runtime ownership",
            "ok",
            f"{runtime_label} PID {pid} owns this data root.",
            host=leader.get("host"),
            port=leader.get("port"),
            started_at=leader.get("started_at"),
            runtime_role=runtime_role or None,
        )
    return _check(
        "runtime.leader",
        "Runtime ownership",
        "warning",
        "A stale runtime ownership record exists; the next start will replace it.",
        pid=pid or None,
    )


def _runtime_web_policy_environment(
    leader: dict[str, object],
) -> dict[str, str] | None:
    profile = str(leader.get("web_profile") or "").strip()
    if not profile:
        return None
    environment = {"CARLINA_WEB_PROFILE": profile}
    external_url = leader.get("external_url")
    if external_url is not None:
        environment["CARLINA_EXTERNAL_URL"] = str(external_url)
    trusted_hosts = leader.get("trusted_hosts")
    if isinstance(trusted_hosts, (list, tuple)):
        environment["CARLINA_TRUSTED_HOSTS"] = ",".join(
            str(host) for host in trusted_hosts
        )
    elif trusted_hosts is not None:
        environment["CARLINA_TRUSTED_HOSTS"] = str(trusted_hosts)
    proxy_hops = leader.get("proxy_hops")
    if proxy_hops is not None:
        environment["CARLINA_PROXY_HOPS"] = str(proxy_hops)
    return environment


def _web_profile_check(paths: AppPaths) -> DoctorCheck:
    active_leader = _active_runtime_leader(paths)
    active_environment = (
        _runtime_web_policy_environment(active_leader)
        if active_leader is not None
        else None
    )
    if active_leader is not None and active_environment is None:
        return _check(
            "web.profile",
            "Web exposure profile",
            "info",
            (
                "The active runtime predates web-policy diagnostics; restart it "
                "with the current release to publish its effective policy."
            ),
            source="active-runtime",
            pid=active_leader.get("pid"),
        )
    try:
        policy = resolve_web_security_policy(environment=active_environment)
    except WebSecurityConfigurationError as exc:
        return _check(
            "web.profile",
            "Web exposure profile",
            "error",
            str(exc),
        )
    return _check(
        "web.profile",
        "Web exposure profile",
        "ok",
        (
            "Loopback-only local profile is configured."
            if policy.profile == "local"
            else "Explicit TLS reverse-proxy server profile is configured."
        ),
        profile=policy.profile,
        external_url=policy.external_url,
        trusted_hosts=list(policy.trusted_hosts),
        proxy_hops=policy.proxy_hops,
        source=("active-runtime" if active_leader is not None else "configuration"),
    )


def _disk_check(paths: AppPaths) -> DoctorCheck:
    try:
        usage = shutil.disk_usage(paths.data_root)
    except FileNotFoundError:
        probe = paths.data_root.parent
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        try:
            usage = shutil.disk_usage(probe)
        except OSError as exc:
            return _check(
                "storage.disk",
                "Free disk space",
                "error",
                f"The data root is missing and its volume could not be inspected: {exc}",
                data_root=str(paths.data_root),
            )
        return _check(
            "storage.disk",
            "Free disk space",
            "error",
            "The data root does not exist. Run 'carlina init' before starting Carlina.",
            data_root=str(paths.data_root),
            inspected_path=str(probe),
            free_bytes=usage.free,
            total_bytes=usage.total,
        )
    except OSError as exc:
        return _check(
            "storage.disk",
            "Free disk space",
            "error",
            f"The data volume could not be inspected: {exc}",
            data_root=str(paths.data_root),
        )
    database_size = (
        paths.database_file.stat().st_size if paths.database_file.exists() else 0
    )
    recommended = max(2 * 1024**3, database_size * 3)
    status: DoctorStatus = "warning" if usage.free < recommended else "ok"
    return _check(
        "storage.disk",
        "Free disk space",
        status,
        (
            "Free space is below the backup/update working-space recommendation."
            if status == "warning"
            else "Free space is sufficient for routine backup and update staging."
        ),
        free_bytes=usage.free,
        total_bytes=usage.total,
        recommended_free_bytes=recommended,
    )


def _managed_release_check(paths: AppPaths) -> DoctorCheck | None:
    """Validate the cross-platform pointer/receipt only for managed installs."""

    from iarf_assistant.core.managed_install import (
        ManagedInstallationLayout,
        ManagedInstallError,
    )

    layout = ManagedInstallationLayout.resolve(paths.program_root)
    if not layout.metadata_file.is_file() and not layout.current_file.is_file():
        return None
    try:
        active = layout.current_version()
        if active is None:
            raise ManagedInstallError("Managed installation has no active release.")
        receipt = layout.read_receipt(active)
        runtime = layout.python_executable(active)
    except (ManagedInstallError, OSError, ValueError) as exc:
        return _check(
            "release.managed-install",
            "Managed release",
            "error",
            f"Managed release metadata is inconsistent: {exc}",
            program_root=str(layout.root),
        )
    phase = None
    transaction_id = None
    if layout.journal_file.is_file():
        try:
            journal = json.loads(layout.journal_file.read_text(encoding="utf-8"))
            phase = journal.get("phase") if isinstance(journal, dict) else None
            transaction_id = (
                journal.get("transaction_id") if isinstance(journal, dict) else None
            )
        except (OSError, json.JSONDecodeError):
            phase = "unreadable"
    failed_releases = (
        sorted(path.name for path in layout.failed_dir.iterdir())
        if layout.failed_dir.is_dir()
        else []
    )
    status: DoctorStatus = (
        "warning" if phase in {"failed", "unreadable"} or failed_releases else "ok"
    )
    summary = (
        f"Carlina {active} is active; retained failed-update diagnostics require review."
        if status == "warning"
        else f"Carlina {active} has a valid pointer, runtime, and release receipt."
    )
    return _check(
        "release.managed-install",
        "Managed release",
        status,
        summary,
        program_root=str(layout.root),
        active_version=active,
        runtime=str(runtime),
        signer_key_id=receipt.get("signer_key_id"),
        update_phase=phase,
        transaction_id=transaction_id,
        failed_release_quarantines=failed_releases,
    )


def _addon_checks(paths: AppPaths, runtime_facts: dict[str, Any]) -> list[DoctorCheck]:
    discovery = discover_addons(
        paths.package_root / "addons",
        paths.installed_addons_dir,
    )

    active_ids = {
        str(item["id"])
        for item in runtime_facts.get("addons", [])
        if item.get("active")
    }
    missing = sorted(active_ids - set(discovery.packages))

    def affects_active_addon(diagnostic) -> bool:
        folded_path_parts = {
            part.casefold() for part in Path(diagnostic.path or ".").parts
        }
        folded_message = diagnostic.message.casefold()
        return any(
            addon_id.casefold() in folded_path_parts
            or f"'{addon_id.casefold()}'" in folded_message
            for addon_id in active_ids
        )

    discovery_errors = [
        item for item in discovery.diagnostics if item.severity == "error"
    ]
    invalid_active = [
        item.to_dict() for item in discovery_errors if affects_active_addon(item)
    ]
    invalid_inactive = [
        item.to_dict() for item in discovery_errors if not affects_active_addon(item)
    ]
    checks: list[DoctorCheck] = []
    if missing or invalid_active:
        checks.append(
            _check(
                "addons.contracts",
                "Add-on contracts",
                "error",
                "One or more enabled add-ons are missing or have invalid manifests.",
                active_count=len(active_ids),
                discovered_count=len(discovery.packages),
                missing_active=missing,
                invalid_active=invalid_active,
            )
        )
    else:
        checks.append(
            _check(
                "addons.contracts",
                "Add-on contracts",
                "ok",
                f"{len(discovery.packages)} package contract(s) validated; "
                f"{len(active_ids)} enabled.",
                active=sorted(active_ids),
            )
        )
    if invalid_inactive:
        checks.append(
            _check(
                "addons.inactive-contracts",
                "Inactive add-on contracts",
                "warning",
                "Some inactive add-on manifests are invalid.",
                invalid=invalid_inactive,
            )
        )

    active_version_mismatches: list[dict[str, str]] = []
    inactive_version_mismatches: list[dict[str, str]] = []
    for item in runtime_facts.get("addons", []):
        addon_id = str(item["id"])
        package = discovery.packages.get(addon_id)
        if package is None or package.source_kind.value != "built_in":
            continue
        installed_version = str(item.get("version") or "")
        package_version = str(package.contract.version)
        if installed_version == package_version:
            continue
        mismatch = {
            "addon_id": addon_id,
            "installed_version": installed_version,
            "package_version": package_version,
        }
        if item.get("active"):
            active_version_mismatches.append(mismatch)
        else:
            inactive_version_mismatches.append(mismatch)

    if active_version_mismatches:
        checks.append(
            _check(
                "addons.builtin-reconciliation",
                "Built-in add-on versions",
                "error",
                "Active built-in add-ons require explicit version reconciliation. "
                "Stop Carlina and run 'carlina init --repair'.",
                active=active_version_mismatches,
                deferred_inactive=inactive_version_mismatches,
            )
        )
    elif inactive_version_mismatches:
        checks.append(
            _check(
                "addons.builtin-reconciliation",
                "Built-in add-on versions",
                "info",
                "Inactive built-in upgrades are deferred until activation.",
                active=[],
                deferred_inactive=inactive_version_mismatches,
            )
        )
    return checks


def run_doctor(
    paths: AppPaths,
    *,
    deep: bool = False,
    migrations_dir: Path | None = None,
) -> DoctorReport:
    """Run deterministic local checks without creating a Flask application."""

    effective_migrations = migrations_dir or migrations_directory()
    checks: list[DoctorCheck] = [
        _check(
            "runtime.python",
            "Python runtime",
            "ok" if sys.version_info >= (3, 10) else "error",
            ".".join(str(part) for part in sys.version_info[:3]),
            executable=sys.executable,
        ),
        _path_check(paths),
        _disk_check(paths),
        _runtime_leader_check(paths),
        _web_profile_check(paths),
    ]
    managed_release_check = _managed_release_check(paths)
    if managed_release_check is not None:
        checks.append(managed_release_check)
    checks.extend(_database_checks(paths, effective_migrations))
    runtime_facts: dict[str, Any] = {"addons": [], "admin_count": None}
    if paths.database_file.is_file():
        try:
            runtime_facts = _database_runtime_facts(paths.database_file)
        except sqlite3.Error as exc:
            checks.append(
                _check(
                    "database.runtime-facts",
                    "Database runtime state",
                    "error",
                    f"Could not inspect admin/add-on state: {exc}",
                )
            )
    admin_count = runtime_facts.get("admin_count")
    if admin_count is not None:
        checks.append(
            _check(
                "accounts.admin",
                "Administrator account",
                "ok" if admin_count else "warning",
                (
                    f"{admin_count} administrator account(s) exist."
                    if admin_count
                    else "No administrator account exists yet."
                ),
                count=admin_count,
            )
        )
    key_store: InstallationKeyStore | None = None
    key_checks = _key_checks(paths)
    checks.extend(key_checks)
    if not any(
        check.id == "encryption.keys" and check.status == "error"
        for check in key_checks
    ):
        key_store = InstallationKeyStore(paths.master_keys_file)
    checks.append(_backup_check(paths, deep=deep, key_store=key_store))
    checks.append(_restore_check(paths))
    checks.extend(_addon_checks(paths, runtime_facts))
    from carlina.documents_setup import DocumentsSetupError, inspect_documents_setup

    try:
        documents_setup = inspect_documents_setup(paths)
    except DocumentsSetupError:
        checks.append(
            _check(
                "documents.setup",
                "Documents first-run setup",
                "error",
                "The saved Documents setup state could not be read.",
            )
        )
    else:
        if documents_setup != "absent":
            checks.append(
                _check(
                    "documents.setup",
                    "Documents first-run setup",
                    "ok" if documents_setup == "complete" else "warning",
                    "Document tools are prepared."
                    if documents_setup == "complete"
                    else "Documents setup needs to finish preparing its tools.",
                )
            )
    checks.sort(key=lambda item: (-_STATUS_ORDER[item.status], item.id))
    return DoctorReport(
        generated_at=datetime.now(UTC).isoformat(),
        deep=deep,
        layout={
            "mode": paths.mode,
            "data_root": str(paths.data_root),
            "config_dir": str(paths.config_dir),
            "cache_dir": str(paths.cache_dir),
            "log_dir": str(paths.log_dir),
            "database_file": str(paths.database_file),
        },
        checks=tuple(checks),
    )


def format_doctor_human(report: DoctorReport) -> str:
    """Render a compact human-readable diagnostic report."""

    markers = {"ok": "OK", "info": "INFO", "warning": "WARN", "error": "ERROR"}
    lines = [
        "Carlina doctor",
        f"Layout: {report.layout['mode']} ({report.layout['data_root']})",
        "",
    ]
    for check in report.checks:
        lines.append(f"[{markers[check.status]:5}] {check.title}: {check.summary}")
    lines.extend(
        [
            "",
            f"Result: {len(report.errors)} error(s), {len(report.warnings)} warning(s).",
        ]
    )
    return "\n".join(lines)


__all__ = [
    "DoctorCheck",
    "DoctorReport",
    "DoctorStatus",
    "format_doctor_human",
    "migration_heads",
    "run_doctor",
]
