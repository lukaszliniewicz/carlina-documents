"""Canonical Carlina program, configuration, and mutable-data paths.

The import/package directory is application code.  New installations never
write mutable state there.  Existing source checkouts may temporarily run in
an explicit legacy compatibility mode until ``carlina migrate-data`` moves the
owned state into the platform-native layout.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from dotenv import dotenv_values
from platformdirs import PlatformDirs

from carlina.product_identity import environment_value
from carlina.runtime_composition import resolve_runtime_composition
from carlina.runtime_profiles import RuntimeProfile

PathMode = Literal["platform", "portable", "legacy"]
LAYOUT_FORMAT = "carlina.storage-layout"
LAYOUT_VERSION = 1
WORKER_OWNERSHIP_XOPTION = "carlina-worker-owner"


@dataclass(frozen=True, slots=True)
class AppPaths:
    """Resolved immutable path contract for one Carlina installation."""

    program_root: Path
    package_root: Path
    mode: PathMode
    data_root: Path
    config_dir: Path
    cache_dir: Path
    log_dir: Path
    instance_dir: Path
    database_dir: Path
    database_file: Path
    uploads_dir: Path
    artifacts_dir: Path
    addons_dir: Path
    installed_addons_dir: Path
    addon_data_dir: Path
    addon_environments_dir: Path
    addon_logs_dir: Path
    backups_dir: Path
    staging_dir: Path
    locks_dir: Path
    secrets_dir: Path
    state_dir: Path
    temp_dir: Path
    config_file: Path
    master_keys_file: Path
    session_key_file: Path
    storage_manifest_file: Path
    migration_target_root: Path
    legacy_locations: tuple[Path, ...]

    @property
    def is_legacy(self) -> bool:
        return self.mode == "legacy"

    @property
    def installation_id(self) -> str:
        """Return the stable, non-secret identity for this data installation."""

        return installation_identity(self.data_root)

    @property
    def owned_directories(self) -> tuple[Path, ...]:
        """Directories Carlina may create and mutate for this layout."""

        return (
            self.data_root,
            self.config_dir,
            self.cache_dir,
            self.log_dir,
            self.instance_dir,
            self.database_dir,
            self.uploads_dir,
            self.artifacts_dir,
            self.addons_dir,
            self.installed_addons_dir,
            self.addon_data_dir,
            self.addon_environments_dir,
            self.addon_logs_dir,
            self.backups_dir,
            self.staging_dir,
            self.locks_dir,
            self.secrets_dir,
            self.state_dir,
            self.temp_dir,
        )

    def ensure_directories(self) -> None:
        """Create the owned layout without changing existing contents."""

        for directory in dict.fromkeys(self.owned_directories):
            directory.mkdir(parents=True, exist_ok=True)
        _restrict_directory(self.secrets_dir)

    def write_storage_manifest(self) -> None:
        """Persist storage ownership/retention metadata for backup and doctor."""

        self.ensure_directories()
        payload = {
            "format": LAYOUT_FORMAT,
            "version": LAYOUT_VERSION,
            "mode": self.mode,
            "categories": {
                "database": _category(self.database_dir, "persistent", True),
                "uploads": _category(self.uploads_dir, "user-managed", True),
                "artifacts": _category(self.artifacts_dir, "user-managed", True),
                "addon_data": _category(self.addon_data_dir, "persistent", True),
                "installed_addons": _category(
                    self.installed_addons_dir,
                    "persistent",
                    True,
                ),
                "addon_environments": _category(
                    self.addon_environments_dir,
                    "rebuildable",
                    False,
                ),
                "addon_logs": _category(self.addon_logs_dir, "rotating", False),
                "state": _category(self.state_dir, "persistent", True),
                "secrets": _category(self.secrets_dir, "secret", False),
                "backups": _category(self.backups_dir, "operator-managed", False),
                "logs": _category(self.log_dir, "rotating", False),
                "cache": _category(self.cache_dir, "rebuildable", False),
                "staging": _category(self.staging_dir, "temporary", False),
                "temporary": _category(self.temp_dir, "temporary", False),
                "locks": _category(self.locks_dir, "ephemeral", False),
            },
        }
        _atomic_json_write(self.storage_manifest_file, payload)

    def as_config(self) -> dict[str, str | bool]:
        """Return Flask configuration keys for consumers and add-ons."""

        return {
            "CARLINA_HOME": str(self.data_root),
            "CARLINA_INSTALLATION_ID": self.installation_id,
            "CARLINA_PATH_MODE": self.mode,
            "CARLINA_LEGACY_DATA_MODE": self.is_legacy,
            "CARLINA_DATABASE_FILE": str(self.database_file),
            "CARLINA_UPLOADS_DIR": str(self.uploads_dir),
            "CARLINA_ARTIFACTS_DIR": str(self.artifacts_dir),
            "CARLINA_ADDONS_DIR": str(self.addons_dir),
            "CARLINA_INSTALLED_ADDONS_DIR": str(self.installed_addons_dir),
            "CARLINA_ADDON_DATA_DIR": str(self.addon_data_dir),
            "CARLINA_ADDON_ENVIRONMENTS_DIR": str(self.addon_environments_dir),
            "CARLINA_BACKUPS_DIR": str(self.backups_dir),
            "CARLINA_LOG_DIR": str(self.log_dir),
            "CARLINA_CACHE_DIR": str(self.cache_dir),
            "CARLINA_SECRETS_DIR": str(self.secrets_dir),
            "CARLINA_MASTER_KEYS_FILE": str(self.master_keys_file),
            "CARLINA_SESSION_KEY_FILE": str(self.session_key_file),
            "CARLINA_STATE_DIR": str(self.state_dir),
            # Compatibility for add-ons migrating from package-root uploads.
            "UPLOAD_FOLDER": str(self.uploads_dir),
        }


def _category(path: Path, retention: str, backup: bool) -> dict[str, object]:
    return {
        "path": str(path),
        "owner": "carlina",
        "retention": retention,
        "included_in_backup": backup,
    }


def _atomic_json_write(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _restrict_directory(path: Path) -> None:
    if os.name != "nt":
        path.chmod(0o700)


def restrict_secret_file(path: Path) -> None:
    """Apply the strongest portable owner-only permissions available."""

    if os.name != "nt":
        path.chmod(0o600)


def _platform_roots(
    profile: RuntimeProfile = RuntimeProfile.WORKBENCH,
) -> tuple[Path, Path, Path, Path]:
    dirs = PlatformDirs(
        appname=(
            "Carlina Documents" if profile is RuntimeProfile.DOCUMENTS else "Carlina"
        ),
        appauthor=False,
        roaming=False,
        ensure_exists=False,
    )
    return (
        Path(dirs.user_data_path),
        Path(dirs.user_config_path),
        Path(dirs.user_cache_path),
        Path(dirs.user_log_path),
    )


def addon_environment_cache_path(
    data_root: str | os.PathLike[str],
    platform_cache: str | os.PathLike[str],
) -> Path:
    """Return the local, installation-scoped cache for add-on environments.

    Pixi environments contain native executables and links, so they are not
    portable data and cannot be relied on to work on SMB/UNC filesystems. The
    stable digest prevents installations that share one OS account from
    reusing each other's rebuildable environments.
    """

    resolved_data_root = Path(data_root).expanduser().resolve()
    resolved_platform_cache = Path(platform_cache).expanduser().resolve()
    digest = installation_identity(resolved_data_root)
    return resolved_platform_cache / "addon-environments" / digest


def installation_identity(data_root: str | os.PathLike[str]) -> str:
    """Return a stable identifier without exposing the installation path.

    The canonical, case-normalized data root is the installation boundary:
    two Carlina data roots owned by the same OS account must not share mutable
    runtime resources such as add-on environments or Docker objects.
    """

    resolved_data_root = Path(data_root).expanduser().resolve()
    identity = os.path.normcase(str(resolved_data_root)).encode(
        "utf-8",
        errors="surrogatepass",
    )
    return hashlib.sha256(identity).hexdigest()[:16]


def worker_ownership_token(data_root: str | os.PathLike[str]) -> str:
    """Return the opaque, full-width command-line ownership token for workers.

    It identifies a mutable data root without putting that path in the worker
    command line. It is not a credential; cleanup still requires a matching
    worker launch shape and treats the token as an installation boundary.
    """

    resolved_data_root = Path(data_root).expanduser().resolve()
    identity = os.path.normcase(str(resolved_data_root)).encode(
        "utf-8",
        errors="surrogatepass",
    )
    return hashlib.sha256(identity).hexdigest()


def worker_ownership_xoption(data_root: str | os.PathLike[str]) -> str:
    """Return the harmless Python ``-X`` value carried by every worker argv."""

    return f"{WORKER_OWNERSHIP_XOPTION}={worker_ownership_token(data_root)}"


def _legacy_locations(program_root: Path, package_root: Path) -> tuple[Path, ...]:
    candidates = (
        program_root / "instance",
        package_root / "instance",
        package_root / "uploads",
    )
    return tuple(path.resolve() for path in candidates if path.exists())


def _select_legacy_instance(program_root: Path, package_root: Path) -> Path | None:
    candidates = (program_root / "instance", package_root / "instance")
    for candidate in candidates:
        if (candidate / "iarf_ai.db").is_file():
            return candidate.resolve()
    return None


def resolve_app_paths(
    program_root: str | os.PathLike[str] | None = None,
    *,
    package_root: str | os.PathLike[str] | None = None,
    environment: Mapping[str, str] | None = None,
    allow_legacy: bool = True,
    profile: RuntimeProfile | str | None = None,
) -> AppPaths:
    """Resolve Carlina paths without creating or mutating the filesystem.

    ``CARLINA_HOME`` is a portable-data override. ``CARLINA_DATA_ROOT`` and the
    former ``IARF_*`` names remain temporary compatibility aliases. Rebuildable
    add-on environments remain in a platform-local cache unless
    ``CARLINA_ADDON_ENVIRONMENTS_DIR`` explicitly overrides that location.
    ``CARLINA_SECRETS_DIR`` may keep installation keys on a restricted local
    volume when the mutable data root itself lives on shared storage.
    """

    active_environment = environment if environment is not None else os.environ
    runtime_profile = resolve_runtime_composition(
        profile,
        environment=active_environment,
    ).profile
    resolved_package_root = (
        Path(package_root or Path(__file__).resolve().parents[1]).expanduser().resolve()
    )
    resolved_program_root = (
        Path(program_root or resolved_package_root.parent).expanduser().resolve()
    )

    portable_home = environment_value(
        "CARLINA_HOME",
        "CARLINA_DATA_ROOT",
        "IARF_HOME",
        "IARF_DATA_ROOT",
        environment=active_environment,
    )
    if runtime_profile is RuntimeProfile.WORKBENCH:
        # Preserve the original call shape for existing Workbench test seams.
        platform_data, platform_config, platform_cache, platform_logs = (
            _platform_roots()
        )
    else:
        platform_data, platform_config, platform_cache, platform_logs = _platform_roots(
            runtime_profile
        )
    platform_data = platform_data.expanduser().resolve()
    platform_config = platform_config.expanduser().resolve()
    platform_cache = platform_cache.expanduser().resolve()
    platform_logs = platform_logs.expanduser().resolve()
    legacy_locations = _legacy_locations(
        resolved_program_root,
        resolved_package_root,
    )

    legacy_instance = None
    if (
        portable_home is None
        and allow_legacy
        and runtime_profile is RuntimeProfile.WORKBENCH
    ):
        native_database = platform_data / "database" / "carlina.db"
        if not native_database.exists():
            legacy_instance = _select_legacy_instance(
                resolved_program_root,
                resolved_package_root,
            )

    if portable_home is not None:
        data_root = Path(portable_home).expanduser().resolve()
        config_dir = data_root / "config"
        cache_dir = data_root / "cache"
        log_dir = data_root / "logs"
        mode: PathMode = "portable"
        instance_dir = data_root
        database_dir = data_root / "database"
        database_file = database_dir / "carlina.db"
        uploads_dir = data_root / "uploads"
        secrets_dir = data_root / "secrets"
    elif legacy_instance is not None:
        # Database/uploads remain where the existing checkout expects them.
        # New configuration, secrets, logs, and caches are kept outside code.
        data_root = legacy_instance
        config_dir = platform_config
        cache_dir = platform_cache
        log_dir = platform_logs
        mode = "legacy"
        instance_dir = legacy_instance
        database_dir = legacy_instance
        database_file = legacy_instance / "iarf_ai.db"
        package_uploads = resolved_package_root / "uploads"
        uploads_dir = (
            package_uploads.resolve()
            if package_uploads.exists()
            else legacy_instance / "uploads"
        )
        secrets_dir = platform_data / "secrets"
    else:
        data_root = platform_data
        config_dir = platform_config
        cache_dir = platform_cache
        log_dir = platform_logs
        mode = "platform"
        instance_dir = data_root
        database_dir = data_root / "database"
        database_file = database_dir / "carlina.db"
        uploads_dir = data_root / "uploads"
        secrets_dir = data_root / "secrets"

    secrets_override = active_environment.get("CARLINA_SECRETS_DIR")
    if secrets_override is not None:
        if not secrets_override.strip():
            raise ValueError("CARLINA_SECRETS_DIR cannot be empty.")
        secrets_dir = Path(secrets_override).expanduser().resolve()

    artifacts_dir = data_root / "artifacts"
    addons_dir = data_root / "addons"
    installed_addons_dir = addons_dir / "installed"
    addon_data_dir = addons_dir / "data"
    addon_environments_override = active_environment.get(
        "CARLINA_ADDON_ENVIRONMENTS_DIR"
    )
    if addon_environments_override is not None:
        if not addon_environments_override.strip():
            raise ValueError("CARLINA_ADDON_ENVIRONMENTS_DIR cannot be empty.")
        addon_environments_dir = (
            Path(addon_environments_override).expanduser().resolve()
        )
    else:
        addon_environments_dir = addon_environment_cache_path(
            data_root,
            platform_cache,
        )
    addon_logs_dir = log_dir / "addons"
    backups_dir = data_root / "backups"
    staging_dir = data_root / "staging"
    locks_dir = data_root / "locks"
    state_dir = data_root / "state"
    temp_dir = data_root / "tmp"

    return AppPaths(
        program_root=resolved_program_root,
        package_root=resolved_package_root,
        mode=mode,
        data_root=data_root,
        config_dir=config_dir,
        cache_dir=cache_dir,
        log_dir=log_dir,
        instance_dir=instance_dir,
        database_dir=database_dir,
        database_file=database_file,
        uploads_dir=uploads_dir,
        artifacts_dir=artifacts_dir,
        addons_dir=addons_dir,
        installed_addons_dir=installed_addons_dir,
        addon_data_dir=addon_data_dir,
        addon_environments_dir=addon_environments_dir,
        addon_logs_dir=addon_logs_dir,
        backups_dir=backups_dir,
        staging_dir=staging_dir,
        locks_dir=locks_dir,
        secrets_dir=secrets_dir,
        state_dir=state_dir,
        temp_dir=temp_dir,
        config_file=config_dir / "carlina.env",
        master_keys_file=secrets_dir / "master-keys.json",
        session_key_file=secrets_dir / "session.key",
        storage_manifest_file=state_dir / "storage-layout.json",
        migration_target_root=platform_data,
        legacy_locations=legacy_locations,
    )


def load_runtime_environment(
    program_root: str | os.PathLike[str] | None = None,
    *,
    package_root: str | os.PathLike[str] | None = None,
    profile: RuntimeProfile | str | None = None,
) -> AppPaths:
    """Load canonical and checkout-local dotenv files with safe precedence.

    Explicit process variables win, followed by the platform-native Carlina
    configuration file, followed by the historical checkout ``.env`` for the
    Workbench profile. Documents loads only its selected canonical config file.
    Values are loaded into ``os.environ`` because Flask and several provider
    SDKs read configuration there directly.
    """

    resolved_profile = (
        resolve_runtime_composition(profile).profile if profile is not None else None
    )
    explicit = dict(os.environ)
    if resolved_profile is None:
        resolved_profile = resolve_runtime_composition(environment=explicit).profile

    initial_profile_value = explicit.get("CARLINA_PROFILE")
    try:
        resolved_package_root = (
            Path(package_root or Path(__file__).resolve().parents[1])
            .expanduser()
            .resolve()
        )
        resolved_program_root = (
            Path(program_root or resolved_package_root.parent).expanduser().resolve()
        )
        legacy_values = (
            {}
            if resolved_profile is RuntimeProfile.DOCUMENTS
            else {
                key: value
                for key, value in dotenv_values(resolved_program_root / ".env").items()
                if value is not None
            }
        )
        preliminary_environment = {**legacy_values, **explicit}
        preliminary = resolve_app_paths(
            resolved_program_root,
            package_root=resolved_package_root,
            environment=preliminary_environment,
            profile=resolved_profile,
        )
        canonical_values = {
            key: value
            for key, value in dotenv_values(preliminary.config_file).items()
            if value is not None
        }
        merged = {**legacy_values, **canonical_values, **explicit}
        for key, value in merged.items():
            os.environ[key] = value
        return resolve_app_paths(
            resolved_program_root,
            package_root=resolved_package_root,
            environment=merged,
            profile=resolved_profile,
        )
    finally:
        if initial_profile_value is None:
            os.environ.pop("CARLINA_PROFILE", None)
        else:
            os.environ["CARLINA_PROFILE"] = initial_profile_value


def app_paths_from_context() -> AppPaths:
    """Return the path contract registered on the current Flask app."""

    from flask import current_app

    paths = current_app.extensions.get("carlina_paths")
    if not isinstance(paths, AppPaths):
        raise TypeError("Carlina AppPaths have not been registered.")
    return paths


__all__ = [
    "LAYOUT_FORMAT",
    "LAYOUT_VERSION",
    "AppPaths",
    "PathMode",
    "addon_environment_cache_path",
    "app_paths_from_context",
    "load_runtime_environment",
    "resolve_app_paths",
    "restrict_secret_file",
]
