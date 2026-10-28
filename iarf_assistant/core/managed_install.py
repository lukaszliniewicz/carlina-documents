"""Side-by-side managed install, update, health-gate, and rollback lifecycle."""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from importlib.resources import files
from pathlib import Path
from typing import Any, TypedDict

from filelock import FileLock
from filelock import Timeout as FileLockTimeout
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from carlina.core.bootstrap_ownership import (
    BootstrapIdentity,
    BootstrapOwnershipError,
    bootstrap_markers_present,
    validate_bootstrap_generation,
)
from carlina.core.database_checkpoint import (
    DatabaseCheckpointError,
    DatabaseCheckpointRecord,
    DatabaseCheckpointRestoreResult,
    create_database_checkpoint,
    database_switch_phase,
    discard_unwitnessed_database_switch,
    interrupted_database_switches,
    reactivate_checkpoint_safety,
    reactivate_completed_database_switch,
    recover_interrupted_database_switches,
    restore_database_checkpoint,
)
from carlina.core.installation_lifecycle import (
    InstallationCancelled,
    InstallationErrorCode,
    InstallationLifecycle,
    InstallationOperation,
    InstallationPhase,
)
from carlina.core.managed_probe import (
    ManagedProbeAttestation,
    ManagedProbeCapability,
    ManagedProbeError,
    detect_managed_probe_capability,
    run_managed_release_probe,
)
from carlina.core.managed_target_recovery import (
    MANAGED_UPDATE_TRANSACTION_ENVIRONMENT,
)
from carlina.core.publisher_installation import (
    PublisherBootstrapFailure,
    run_publisher_bootstrap,
)
from carlina.core.recovery_policy import (
    RecoveryCoverage,
    RecoveryDecisionSource,
    RecoveryPlan,
    legacy_opaque_recovery_plan,
    plan_recovery,
)
from carlina.profile_binding import read_runtime_profile, validate_runtime_profile
from carlina.runtime_composition import resolve_runtime_composition
from carlina.runtime_profiles import RuntimeProfile
from iarf_assistant.core.app_paths import AppPaths
from iarf_assistant.core.backup_service import (
    BACKUP_VERSION,
    BackupError,
    BackupRecord,
    RestoreActivationWitness,
    RestoreResult,
    create_backup,
    estimate_backup_expanded_bytes,
    inspect_backup,
    preserve_local_backup_catalog,
    resolve_backup_base,
    restore_backup,
    restore_safety_root,
    restore_staging_root,
    verify_backup,
)
from iarf_assistant.core.encryption import EncryptionError, InstallationKeyStore
from iarf_assistant.core.managed_bundle import (
    WindowsBundleError,
    extract_windows_bundle,
)
from iarf_assistant.core.manifest_schema import validate_manifest_against_schema
from iarf_assistant.core.persistence.sqlite import sqlite_read_only_uri
from iarf_assistant.core.release_downloads import (
    ReleaseDownloadError,
    download_release_artifact,
    is_local_release_source,
    read_release_index_source,
    resolve_artifact_source,
    sha256_file,
)
from iarf_assistant.core.release_manifest import (
    PUBLISHER_BOOTSTRAP_EXECUTABLE_KIND,
    ManagedRelease,
    ReleaseArtifact,
    ReleaseIndex,
    ReleaseManifestError,
    TrustedReleaseKey,
    current_platform_tag,
    load_trusted_keys,
    parse_release_index,
    release_contract_sha256,
    verify_release_index,
)
from iarf_assistant.core.runtime_ownership import (
    RuntimeLeaderLock,
    load_runtime_leader,
    process_is_running,
    runtime_leader_lock,
)

MANAGED_INSTALL_FORMAT = "carlina.managed-install"
MANAGED_INSTALL_VERSION = 1
UPDATE_JOURNAL_FORMAT = "carlina.update-journal"
UPDATE_JOURNAL_VERSION = 1
RELEASE_RECEIPT_FORMAT = "carlina.release-receipt"
RELEASE_RECEIPT_VERSION = 3
SUPPORTED_RELEASE_RECEIPT_VERSIONS = frozenset({1, 2, RELEASE_RECEIPT_VERSION})
RECOVERY_REQUIRED_FORMAT = "carlina.recovery-required"
RECOVERY_REQUIRED_VERSION = 1
ROLLBACK_TRANSACTION_FORMAT = "carlina.rollback-transaction"
ROLLBACK_TRANSACTION_VERSION = 1
PROGRAM_ROOT_ENVIRONMENT = "CARLINA_PROGRAM_ROOT"
_VERSION_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+!-]{0,79}$")
_LOWER_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_TRANSACTION_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_MAX_RETAINED_ADOPTION_JOURNAL_BYTES = 1024 * 1024
MIN_PROGRAM_HEADROOM_BYTES = 512 * 1024**2
MIN_DATA_HEADROOM_BYTES = 256 * 1024**2
MIN_UNPACK_TRANSACTION_BYTES = 3 * 1024**3
RECOVERY_ARCHIVE_MINIMUM_OVERHEAD_BYTES = 64 * 1024**2
QUARANTINE_RENAME_ATTEMPTS = 10
QUARANTINE_RENAME_DELAY_SECONDS = 0.5
# Restored private roots can legitimately spend more than 30 minutes rebuilding
# indexes and verifying SQLite/file coherence during the target runtime's
# supported initialization path. Keep the gate bounded, but leave enough time
# for that work to finish before the updater triggers its paired restore.
TARGET_INIT_TIMEOUT_SECONDS = 3_600
_MAX_TARGET_PIXI_PACKAGE_RECORD_BYTES = 1024 * 1024


class _DuplicateJSONKey(ValueError):
    """A duplicate member appeared in a retained ownership record."""


def _json_object_without_duplicate_keys(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJSONKey
        result[key] = value
    return result


def _read_bounded_regular_json_object(path: Path) -> dict[str, Any]:
    """Read a bounded, regular, non-symlink JSON object without leaking data."""

    descriptor: int | None = None
    try:
        path_info = path.lstat()
        if (
            not stat.S_ISREG(path_info.st_mode)
            or path.is_symlink()
            or path_info.st_size > _MAX_RETAINED_ADOPTION_JOURNAL_BYTES
        ):
            raise ManagedInstallError(
                "Retained adoption journal is unsafe or exceeds its size limit."
            )
        flags = os.O_RDONLY
        flags |= getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_NONBLOCK", 0)
        descriptor = os.open(path, flags)
        opened_info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened_info.st_mode)
            or opened_info.st_size > _MAX_RETAINED_ADOPTION_JOURNAL_BYTES
            or (opened_info.st_dev, opened_info.st_ino)
            != (path_info.st_dev, path_info.st_ino)
        ):
            raise ManagedInstallError(
                "Retained adoption journal is unsafe or exceeds its size limit."
            )
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = None
            raw = handle.read(_MAX_RETAINED_ADOPTION_JOURNAL_BYTES + 1)
            final_info = os.fstat(handle.fileno())
        if (
            len(raw) > _MAX_RETAINED_ADOPTION_JOURNAL_BYTES
            or final_info.st_size > _MAX_RETAINED_ADOPTION_JOURNAL_BYTES
        ):
            raise ManagedInstallError(
                "Retained adoption journal is unsafe or exceeds its size limit."
            )
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_json_object_without_duplicate_keys,
        )
    except ManagedInstallError:
        raise
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        _DuplicateJSONKey,
    ) as exc:
        raise ManagedInstallError(
            "Retained adoption journal is unreadable or invalid."
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if not isinstance(payload, dict):
        raise ManagedInstallError("Retained adoption journal is invalid.")
    return payload


def _validate_adoption_retry_arguments(
    *,
    requested_version: str | None,
    expected_index_sha256: str | None,
    retry_transaction_id: str | None,
    resume_bootstrap_claim_id: str | None = None,
) -> None:
    if retry_transaction_id is not None and resume_bootstrap_claim_id is not None:
        raise ManagedInstallError(
            "Native bootstrap resume and explicit adoption retry cannot be combined."
        )
    if expected_index_sha256 is not None and (
        not isinstance(expected_index_sha256, str)
        or not _LOWER_SHA256_PATTERN.fullmatch(expected_index_sha256)
    ):
        raise ReleaseManifestError(
            "Expected release-index SHA-256 must be 64 lowercase hexadecimal characters."
        )
    if requested_version is not None:
        _safe_version(requested_version)
    if retry_transaction_id is not None:
        if not isinstance(
            retry_transaction_id, str
        ) or not _TRANSACTION_ID_PATTERN.fullmatch(retry_transaction_id):
            raise ManagedInstallError(
                "Retained adoption transaction ID must be 32 lowercase hexadecimal characters."
            )
        if requested_version is None or expected_index_sha256 is None:
            raise ManagedInstallError(
                "Retained adoption retry requires an explicit version and index hash."
            )
    if resume_bootstrap_claim_id is not None:
        if not isinstance(
            resume_bootstrap_claim_id, str
        ) or not _TRANSACTION_ID_PATTERN.fullmatch(resume_bootstrap_claim_id):
            raise ManagedInstallError(
                "Native bootstrap resume claim must be 32 lowercase hexadecimal characters."
            )
        if requested_version is None or expected_index_sha256 is None:
            raise ManagedInstallError(
                "Native bootstrap resume requires an explicit version and index hash."
            )


def _recovery_plan_payload(plan: RecoveryPlan) -> dict[str, Any]:
    """Normalize tuple/enumeration fields exactly as they appear in JSON."""

    return json.loads(json.dumps(plan.to_dict()))


class ManagedInstallError(RuntimeError):
    """A managed lifecycle operation was unsafe or could not complete."""


class ManagedCompatibilityError(ManagedInstallError):
    """A release cannot safely use the current data or add-on contracts."""


class ManagedRuntimeActiveError(ManagedInstallError):
    """An update or rollback requires the managed server to be stopped."""


@dataclass(frozen=True, slots=True)
class CompatibilityIssue:
    code: str
    message: str
    blocking: bool = True
    details: Mapping[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CompatibilityReport:
    target_version: str
    database_revisions: tuple[str, ...]
    installed_addons: tuple[str, ...]
    program_free_bytes: int
    data_free_bytes: int
    recovery_plan: RecoveryPlan
    issues: tuple[CompatibilityIssue, ...]

    @property
    def compatible(self) -> bool:
        return not any(issue.blocking for issue in self.issues)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["recovery_plan"] = self.recovery_plan.to_dict()
        payload["compatible"] = self.compatible
        return payload


@dataclass(frozen=True, slots=True)
class ReleaseCheckResult:
    channel: str
    current_version: str | None
    available_version: str | None
    platform: str
    signer_key_id: str | None
    compatibility: CompatibilityReport | None

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "compatibility": (
                self.compatibility.to_dict() if self.compatibility else None
            ),
        }


@dataclass(frozen=True, slots=True)
class UpdateResult:
    transaction_id: str
    previous_version: str | None
    active_version: str
    backup_path: Path | None
    database_checkpoint_path: Path | None
    signer_key_id: str | None
    first_install: bool
    recovery_plan: RecoveryPlan

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["backup_path"] = str(self.backup_path) if self.backup_path else None
        payload["database_checkpoint_path"] = (
            str(self.database_checkpoint_path)
            if self.database_checkpoint_path
            else None
        )
        payload["recovery_plan"] = self.recovery_plan.to_dict()
        return payload


@dataclass(frozen=True, slots=True)
class RollbackResult:
    transaction_id: str
    previous_version: str
    active_version: str
    data_restored: bool
    restore_safety_root: Path | None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["restore_safety_root"] = (
            str(self.restore_safety_root) if self.restore_safety_root else None
        )
        return payload


@dataclass(frozen=True, slots=True)
class _RetainedAdoptionRetry:
    journal: dict[str, Any]
    release: ManagedRelease
    artifact: ReleaseArtifact
    target: str
    previous: str | None
    recovery_plan: RecoveryPlan
    source_artifact: Path
    version_dir: Path
    receipt: dict[str, Any] | None
    committed: bool


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_json(path: Path, payload: object) -> None:
    _atomic_text(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _fsync_directory(path: Path) -> None:
    """Persist a directory mutation on POSIX; Windows is best-effort."""

    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _durable_atomic_json(path: Path, payload: object) -> None:
    _atomic_json(path, payload)
    _fsync_directory(path.parent)


def _safe_version(value: str) -> str:
    raw = str(value or "").strip()
    if not _VERSION_NAME_PATTERN.fullmatch(raw):
        raise ManagedInstallError(f"Unsafe managed version name: {value!r}.")
    try:
        normalized = str(Version(raw))
    except InvalidVersion as exc:
        raise ManagedInstallError(f"Invalid managed version: {value!r}.") from exc
    if not _VERSION_NAME_PATTERN.fullmatch(normalized):
        raise ManagedInstallError(f"Unsafe normalized version name: {normalized!r}.")
    return normalized


def default_program_root() -> Path:
    """Return a per-user program root separate from mutable Carlina data."""

    profile = resolve_runtime_composition().profile
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        name = "CarlinaDocuments" if profile is RuntimeProfile.DOCUMENTS else "Carlina"
        return (base / "Programs" / name).resolve()
    if sys.platform == "darwin":
        name = "CarlinaDocuments" if profile is RuntimeProfile.DOCUMENTS else "Carlina"
        return (
            Path.home() / "Library" / "Application Support" / name / "Program"
        ).resolve()
    name = "carlina-documents" if profile is RuntimeProfile.DOCUMENTS else "carlina"
    return (Path.home() / ".local" / "lib" / name).resolve()


@dataclass(frozen=True, slots=True)
class ManagedInstallationLayout:
    root: Path

    @classmethod
    def resolve(
        cls, root: str | os.PathLike[str] | None = None
    ) -> ManagedInstallationLayout:
        resolved_root = (
            root or os.environ.get(PROGRAM_ROOT_ENVIRONMENT) or default_program_root()
        )
        return cls(Path(resolved_root).expanduser().resolve())

    @property
    def versions_dir(self) -> Path:
        return self.root / "versions"

    @property
    def staging_dir(self) -> Path:
        return self.root / "staging"

    @property
    def state_dir(self) -> Path:
        return self.root / "state"

    @property
    def history_dir(self) -> Path:
        return self.state_dir / "history"

    @property
    def recovery_dir(self) -> Path:
        return self.state_dir / "recovery"

    @property
    def database_checkpoints_dir(self) -> Path:
        return self.recovery_dir / "database-checkpoints"

    @property
    def failed_dir(self) -> Path:
        """Quarantined, never-active runtimes retained for update diagnostics."""

        return self.state_dir / "failed-releases"

    @property
    def current_file(self) -> Path:
        return self.root / "current"

    @property
    def journal_file(self) -> Path:
        return self.state_dir / "update-journal.json"

    @property
    def recovery_required_file(self) -> Path:
        return self.state_dir / "recovery-required.json"

    @property
    def rollback_transaction_file(self) -> Path:
        return self.state_dir / "rollback-transaction.json"

    @property
    def metadata_file(self) -> Path:
        return self.state_dir / "installation.json"

    @property
    def data_root_file(self) -> Path:
        return self.state_dir / "data-root"

    @property
    def lock_file(self) -> Path:
        return self.state_dir / "managed-install.lock"

    def ensure(self) -> None:
        for path in (
            self.root,
            self.versions_dir,
            self.staging_dir,
            self.state_dir,
            self.history_dir,
            self.recovery_dir,
            self.database_checkpoints_dir,
            self.failed_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def version_dir(self, version: str) -> Path:
        normalized = _safe_version(version)
        candidate = (self.versions_dir / normalized).resolve()
        try:
            candidate.relative_to(self.versions_dir.resolve())
        except ValueError as exc:
            raise ManagedInstallError(
                "Managed version escaped the versions directory."
            ) from exc
        return candidate

    def receipt_file(self, version: str) -> Path:
        return self.version_dir(version) / "release-receipt.json"

    def current_version(self) -> str | None:
        try:
            raw = self.current_file.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise ManagedInstallError(
                f"Managed current-version pointer is unreadable: {self.current_file}"
            ) from exc
        normalized = _safe_version(raw)
        if not self.version_dir(normalized).is_dir():
            raise ManagedInstallError(
                f"Managed current version is missing: {self.version_dir(normalized)}"
            )
        return normalized

    def activate(self, version: str) -> None:
        normalized = _safe_version(version)
        if not self.version_dir(normalized).is_dir():
            raise ManagedInstallError(f"Cannot activate missing release {normalized}.")
        _atomic_text(self.current_file, normalized + "\n")

    def python_executable(self, version: str) -> Path:
        root = self.version_dir(version) / "env"
        candidates = (
            (root / "python.exe", root / "Scripts" / "python.exe")
            if os.name == "nt"
            else (root / "bin" / "python",)
        )
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        raise ManagedInstallError(
            f"Managed release {version} has no unpacked Python runtime under {root}."
        )

    def pixi_executable(self, version: str) -> Path:
        """Return only the Pixi binary declared by managed launchers.

        A managed release must never silently select a system Pixi binary: its
        add-on locks need the release-pinned toolchain.  Keep this path aligned
        with ``write_launchers`` and reject incomplete unpacking before target
        application code is imported.
        """

        root = self.version_dir(version) / "env"
        candidate = root / "bin" / ("pixi.exe" if os.name == "nt" else "pixi")
        if candidate.is_file() and (os.name == "nt" or os.access(candidate, os.X_OK)):
            return candidate
        raise ManagedInstallError(
            f"Managed release {version} has no bundled Pixi executable at {candidate}."
        )

    def read_receipt(self, version: str) -> dict[str, Any]:
        path = self.receipt_file(version)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ManagedInstallError(f"Release receipt is unreadable: {path}") from exc
        if (
            not isinstance(payload, dict)
            or payload.get("format") != RELEASE_RECEIPT_FORMAT
            or payload.get("schema_version") not in SUPPORTED_RELEASE_RECEIPT_VERSIONS
        ):
            raise ManagedInstallError(f"Unsupported release receipt: {path}")
        return payload

    def validate_installation_binding(
        self, profile: RuntimeProfile, *, data_root: Path | None = None
    ) -> None:
        """Keep a program root owned by its recorded product and data root."""

        if self.metadata_file.is_file():
            try:
                metadata = json.loads(self.metadata_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ManagedInstallError(
                    "Managed installation metadata is unreadable."
                ) from exc
            if (
                not isinstance(metadata, dict)
                or metadata.get("format") != MANAGED_INSTALL_FORMAT
            ):
                raise ManagedInstallError("Managed installation metadata is invalid.")
            recorded_profile = metadata.get("application_profile", "workbench")
            if recorded_profile != profile.value:
                raise ManagedInstallError(
                    "Managed program root belongs to another application profile."
                )
            recorded_data_root = metadata.get("data_root")
            if (
                data_root is not None
                and recorded_data_root is not None
                and recorded_data_root != str(data_root.resolve())
            ):
                raise ManagedInstallError(
                    "Managed program root belongs to another data root."
                )

    def read_recovery_requirement(self) -> dict[str, Any] | None:
        path = self.recovery_required_file
        if path.is_file():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ManagedInstallError(
                    f"Managed recovery-required marker is unreadable: {path}"
                ) from exc
        else:
            # The failed update journal is a second durable fence if creating
            # the dedicated marker was itself interrupted or denied.
            try:
                journal = json.loads(self.journal_file.read_text(encoding="utf-8"))
            except OSError, json.JSONDecodeError:
                return None
            if not isinstance(journal, dict) or journal.get("phase") != "failed":
                return None
            if "recovery_required" not in journal:
                # Failures before target execution do not create a recovery
                # fence. Keep the failed journal as diagnostics, but do not
                # turn its absence of recovery state into a false fence that
                # blocks the active release or the next update.
                return None
            payload = journal.get("recovery_required")
        if (
            not isinstance(payload, dict)
            or payload.get("format") != RECOVERY_REQUIRED_FORMAT
            or payload.get("schema_version") != RECOVERY_REQUIRED_VERSION
            or payload.get("status") not in {"armed", "required", "resolved"}
            or not isinstance(payload.get("transaction_id"), str)
            or not _VERSION_NAME_PATTERN.fullmatch(payload["transaction_id"])
        ):
            raise ManagedInstallError(
                f"Managed recovery-required marker is invalid: {path}"
            )
        if payload["status"] == "resolved":
            return None
        target_version = payload.get("failed_target_version")
        try:
            current = self.current_version()
            receipt = self.read_receipt(current) if current is not None else {}
        except ManagedInstallError:
            receipt = {}
            current = None
        if (
            isinstance(target_version, str)
            and current == target_version
            and receipt.get("transaction_id") == payload["transaction_id"]
        ) or receipt.get("acknowledged_recovery_required") == payload["transaction_id"]:
            # Activation is the commit point. The active receipt proves either
            # this fence's own target or a later target that explicitly
            # acknowledged it, even if post-commit diagnostics never ran.
            return None
        return payload

    def read_pending_rollback(self) -> dict[str, Any] | None:
        path = self.rollback_transaction_file
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ManagedInstallError(
                f"Managed rollback transaction is unreadable: {path}"
            ) from exc
        if (
            not isinstance(payload, dict)
            or payload.get("format") != ROLLBACK_TRANSACTION_FORMAT
            or payload.get("schema_version") != ROLLBACK_TRANSACTION_VERSION
            or payload.get("phase")
            not in {
                "prepared",
                "data_restored",
                "reactivation_started",
                "completed",
                "aborted",
            }
            or not isinstance(payload.get("transaction_id"), str)
            or not _VERSION_NAME_PATTERN.fullmatch(payload["transaction_id"])
            or payload.get("coverage") not in {"full", "database_only"}
            or not isinstance(payload.get("from_version"), str)
            or not isinstance(payload.get("to_version"), str)
        ):
            raise ManagedInstallError(
                f"Managed rollback transaction is invalid: {path}"
            )
        _safe_version(payload["from_version"])
        _safe_version(payload["to_version"])
        if payload["phase"] in {"completed", "aborted"}:
            return None
        return payload

    def read_pending_update(self) -> dict[str, Any] | None:
        """Return an update that may have executed target code without recovery."""

        path = self.journal_file
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ManagedInstallError(
                f"Managed update journal is unreadable: {path}"
            ) from exc
        if (
            not isinstance(payload, dict)
            or payload.get("format") != UPDATE_JOURNAL_FORMAT
            or payload.get("schema_version") != UPDATE_JOURNAL_VERSION
            or not isinstance(payload.get("transaction_id"), str)
            or not _VERSION_NAME_PATTERN.fullmatch(payload["transaction_id"])
        ):
            raise ManagedInstallError(f"Managed update journal is invalid: {path}")
        isolated_only = bool(
            payload.get("isolated_target_execution_started")
        ) and not bool(payload.get("target_execution_started"))
        activation_unknown = payload.get("activation_outcome_unknown") is True
        target_started = activation_unknown or (
            not isolated_only
            and (
                bool(payload.get("target_execution_started"))
                or str(payload.get("phase") or "")
                in {
                    "runtime_probe_pending",
                    "runtime_probe_passed",
                    "target_verification",
                    "health_gate_passed",
                    "activation_pending",
                    "recovery_restore_pending",
                }
            )
        )
        if not target_started or (
            payload.get("phase") == "completed" and not activation_unknown
        ):
            return None
        recovery_plan = payload.get("recovery_plan")
        selected_coverage = (
            recovery_plan.get("selected_coverage")
            if isinstance(recovery_plan, dict)
            else None
        )
        if selected_coverage == RecoveryCoverage.NONE and not activation_unknown:
            # The separately fsynced recovery-required record owns this state.
            return None
        automatic_restore = payload.get("automatic_data_restore")
        if (
            payload.get("phase") == "failed"
            and isinstance(automatic_restore, dict)
            and automatic_restore.get("completed") is True
            and not activation_unknown
        ):
            return None
        target = payload.get("target_version")
        if isinstance(target, str):
            try:
                current = self.current_version()
                receipt = self.read_receipt(target)
            except ManagedInstallError:
                current = None
                receipt = {}
            committed = (
                current == target
                and receipt.get("transaction_id") == payload["transaction_id"]
            )
            if activation_unknown:
                try:
                    expected_profile = RuntimeProfile(
                        payload.get("application_profile")
                    ).value
                except TypeError, ValueError:
                    expected_profile = None
                try:
                    installation = json.loads(
                        self.metadata_file.read_text(encoding="utf-8")
                    )
                except OSError, json.JSONDecodeError:
                    installation = None
                installed_profile = (
                    installation.get("application_profile", "workbench")
                    if isinstance(installation, dict)
                    and installation.get("format") == MANAGED_INSTALL_FORMAT
                    else None
                )
                committed = committed and (
                    receipt.get("version") == target
                    and expected_profile is not None
                    and expected_profile == installed_profile
                    and receipt.get("application_profile") == expected_profile
                )
            if committed:
                return None
        return payload

    def write_launchers(
        self,
        *,
        data_root: str | os.PathLike[str] | None = None,
        application_profile: RuntimeProfile | str | None = None,
    ) -> None:
        configured_data_root: str | None = None
        if data_root is not None:
            configured_data_root = str(Path(data_root).expanduser().resolve())
            if not configured_data_root or any(
                character in configured_data_root for character in "\r\n"
            ):
                raise ManagedInstallError("Managed data root contains a line break.")
        if self.data_root_file.is_file():
            pinned_data_root = self.data_root_file.read_text(encoding="utf-8").rstrip(
                "\r\n"
            )
            if not pinned_data_root or any(
                character in pinned_data_root for character in "\r\n"
            ):
                raise ManagedInstallError(
                    f"Managed data-root binding is invalid: {self.data_root_file}"
                )
            if (
                configured_data_root is not None
                and configured_data_root != pinned_data_root
            ):
                raise ManagedInstallError(
                    "Managed data-root binding conflicts with requested root."
                )
            configured_data_root = pinned_data_root

        if application_profile is not None:
            profile = resolve_runtime_composition(application_profile).profile
        elif configured_data_root is not None:
            binding = read_runtime_profile(Path(configured_data_root))
            profile = (
                binding.profile
                if binding is not None
                else resolve_runtime_composition().profile
            )
        else:
            profile = resolve_runtime_composition().profile
        if configured_data_root is not None:
            validate_runtime_profile(Path(configured_data_root), profile)
        self.validate_installation_binding(
            profile,
            data_root=Path(configured_data_root)
            if configured_data_root is not None
            else None,
        )

        launcher_name = (
            "carlina-documents" if profile is RuntimeProfile.DOCUMENTS else "carlina"
        )
        entry_module = (
            "carlina.documents"
            if profile is RuntimeProfile.DOCUMENTS
            else "carlina.bootstrap_manager"
        )
        shell_profile = (
            "export CARLINA_PROFILE=documents\n"
            if profile is RuntimeProfile.DOCUMENTS
            else ""
        )
        command_profile = (
            'set "CARLINA_PROFILE=documents"\n'
            if profile is RuntimeProfile.DOCUMENTS
            else ""
        )
        powershell_profile = (
            "$env:CARLINA_PROFILE = 'documents'\n"
            if profile is RuntimeProfile.DOCUMENTS
            else ""
        )

        self.ensure()
        if data_root is not None and configured_data_root is not None:
            _atomic_text(self.data_root_file, configured_data_root + "\n")
        managed_launcher_source = (
            files("carlina")
            .joinpath("managed_launcher.py")
            .read_text(encoding="utf-8")
        )
        _atomic_text(
            self.state_dir / "managed-launcher.py",
            managed_launcher_source,
        )

        shell_data_root = (
            """DATA_ROOT=$(tr -d '\\r\\n' < "$ROOT/state/data-root")
if [ -z "$DATA_ROOT" ]; then
  echo "Carlina managed data-root binding is empty." >&2
  exit 2
fi
export CARLINA_HOME="$DATA_ROOT"
"""
            if configured_data_root is not None
            else ""
        )
        shell_launcher = (
            """#!/usr/bin/env sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
VERSION=$(tr -d '\\r\\n' < "$ROOT/current")
case "$VERSION" in
  ''|*[!A-Za-z0-9._+!-]*) echo "Carlina current-version pointer is invalid." >&2; exit 2 ;;
esac
PYTHON="$ROOT/versions/$VERSION/env/bin/python"
if [ ! -x "$PYTHON" ]; then
  echo "Carlina runtime is missing: $PYTHON" >&2
  exit 2
fi
export CARLINA_PROGRAM_ROOT="$ROOT"
export CARLINA_MANAGED_VERSION="$VERSION"
export CARLINA_PIXI_EXECUTABLE="$ROOT/versions/$VERSION/env/bin/pixi"
export PYTHONNOUSERSITE=1
"""
            + shell_profile
            + shell_data_root
            + f'exec "$PYTHON" -I -m {entry_module} "$@"\n'
        )
        shell_path = self.root / launcher_name
        _atomic_text(shell_path, shell_launcher)
        if os.name != "nt":
            shell_path.chmod(0o755)

        command_data_root = (
            r' --data-root-file "%~dp0state\data-root"'
            if configured_data_root is not None
            else ""
        )
        command_launcher = (
            r"""@echo off
setlocal EnableExtensions DisableDelayedExpansion
set "CARLINA_PROGRAM_ROOT=%~dp0"
set /p "CARLINA_MANAGED_VERSION="<"%~dp0current"
echo(%CARLINA_MANAGED_VERSION%| %SystemRoot%\System32\findstr.exe /R /X "[0-9A-Za-z][0-9A-Za-z._+!-]*" >nul
if errorlevel 1 (
  echo Carlina current-version pointer is invalid. 1>&2
  exit /b 2
)
set "CARLINA_PYTHON=%~dp0versions\%CARLINA_MANAGED_VERSION%\env\python.exe"
set "CARLINA_PIXI_EXECUTABLE=%~dp0versions\%CARLINA_MANAGED_VERSION%\env\bin\pixi.exe"
if not exist "%CARLINA_PYTHON%" (
  echo Carlina runtime is missing: %CARLINA_PYTHON% 1>&2
  exit /b 2
)
set "PYTHONNOUSERSITE=1"
"""
            + command_profile
            + (
                f'"%CARLINA_PYTHON%" -X utf8 -I '
                '"%~dp0state\\managed-launcher.py" '
                f'--entry-module {entry_module}{command_data_root} -- %*\n'
                'exit /b %ERRORLEVEL%\n'
            )
        )
        _atomic_text(self.root / f"{launcher_name}.cmd", command_launcher)

        powershell_data_root = (
            r"""$dataRoot = (Get-Content -LiteralPath (Join-Path $root 'state/data-root') -Raw -Encoding UTF8).TrimEnd("`r", "`n")
if (-not $dataRoot) {
    throw 'Carlina managed data-root binding is empty.'
}
$env:CARLINA_HOME = $dataRoot
"""
            if configured_data_root is not None
            else ""
        )
        powershell_launcher = (
            r"""$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$version = (Get-Content -LiteralPath (Join-Path $root 'current') -Raw).Trim()
if ($version -notmatch '^[0-9A-Za-z][0-9A-Za-z._+!-]*$') {
    throw 'Carlina current-version pointer is invalid.'
}
$python = Join-Path $root "versions/$version/env/python.exe"
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Carlina runtime is missing: $python"
}
$env:CARLINA_PROGRAM_ROOT = $root
$env:CARLINA_MANAGED_VERSION = $version
$env:CARLINA_PIXI_EXECUTABLE = Join-Path $root "versions/$version/env/bin/pixi.exe"
$env:PYTHONNOUSERSITE = '1'
"""
            + powershell_profile
            + powershell_data_root
            + f"& $python -X utf8 -I -m {entry_module} @args\nexit $LASTEXITCODE\n"
        )
        _atomic_text(self.root / f"{launcher_name}.ps1", powershell_launcher)
        _atomic_json(
            self.metadata_file,
            {
                "format": MANAGED_INSTALL_FORMAT,
                "schema_version": MANAGED_INSTALL_VERSION,
                "program_root": str(self.root),
                "data_root": configured_data_root,
                "updated_at": _now(),
                **(
                    {"application_profile": profile.value}
                    if profile is RuntimeProfile.DOCUMENTS
                    else {}
                ),
            },
        )


def _database_revisions(database: Path) -> tuple[str, ...]:
    if not database.is_file():
        return ()
    connection = sqlite3.connect(sqlite_read_only_uri(database), uri=True)
    try:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='alembic_version'"
        ).fetchone()
        if not exists:
            return ()
        return tuple(
            sorted(
                str(row[0])
                for row in connection.execute("SELECT version_num FROM alembic_version")
            )
        )
    finally:
        connection.close()


def _existing_disk_anchor(path: Path) -> Path:
    """Find an existing ancestor for a mutation-free free-space query."""

    candidate = path.expanduser().resolve()
    while not candidate.exists():
        parent = candidate.parent
        if parent == candidate:
            raise ManagedInstallError(
                f"No existing volume anchor was found for {path}."
            )
        candidate = parent
    return candidate


@dataclass(frozen=True, slots=True)
class _InstalledAddonSelection:
    addon_id: str
    package_dir: Path | None
    error: str | None = None


def _selected_installed_addon_dirs(
    installed_root: Path,
) -> Iterable[_InstalledAddonSelection]:
    if not installed_root.is_dir():
        return
    resolved_installed_root = installed_root.resolve()
    for addon_root in sorted(
        installed_root.iterdir(), key=lambda item: item.name.casefold()
    ):
        if not addon_root.is_dir():
            continue
        try:
            resolved_addon_root = addon_root.resolve()
            resolved_addon_root.relative_to(resolved_installed_root)
            if addon_root.is_symlink():
                raise ValueError("the add-on directory is a symbolic link")
        except (OSError, ValueError) as exc:
            yield _InstalledAddonSelection(
                addon_root.name,
                None,
                f"Unsafe installed add-on directory: {exc}.",
            )
            continue
        marker = addon_root / "active.json"
        selected: Path | None = None
        if marker.is_file():
            try:
                payload = json.loads(marker.read_text(encoding="utf-8"))
                if not isinstance(payload, dict):
                    raise TypeError("active.json must contain an object")
            except (OSError, TypeError, ValueError) as exc:
                yield _InstalledAddonSelection(
                    addon_root.name,
                    None,
                    f"Unreadable active.json: {exc}.",
                )
                continue
            if payload.get("disabled") is True:
                continue
            version_name = str(payload.get("version") or "").strip()
            if not version_name:
                yield _InstalledAddonSelection(
                    addon_root.name,
                    None,
                    "active.json does not declare a version.",
                )
                continue
            try:
                Version(version_name)
                unresolved = addon_root / version_name
                selected = unresolved.resolve()
                selected.relative_to(resolved_addon_root)
                if unresolved.is_symlink():
                    raise ValueError("the selected version is a symbolic link")
                if not selected.is_dir():
                    raise ValueError(f"selected version {version_name!r} is missing")
            except (InvalidVersion, OSError, ValueError) as exc:
                yield _InstalledAddonSelection(
                    addon_root.name,
                    None,
                    f"Unsafe active add-on version: {exc}.",
                )
                continue
        else:
            candidates: list[tuple[Version, Path]] = []
            for child in addon_root.iterdir():
                if not child.is_dir() or child.is_symlink():
                    continue
                try:
                    resolved_child = child.resolve()
                    resolved_child.relative_to(resolved_addon_root)
                    candidates.append((Version(child.name), resolved_child))
                except InvalidVersion, OSError, ValueError:
                    continue
            if candidates:
                selected = max(candidates, key=lambda item: item[0])[1]
        if selected is not None:
            yield _InstalledAddonSelection(addon_root.name, selected)


def _addon_compatibility(
    paths: AppPaths,
    release: ManagedRelease,
) -> tuple[tuple[str, ...], list[CompatibilityIssue]]:
    installed: list[str] = []
    issues: list[CompatibilityIssue] = []
    target_api = Version(release.addon_api_version)
    for selection in _selected_installed_addon_dirs(paths.installed_addons_dir):
        addon_id = selection.addon_id
        installed.append(addon_id)
        if selection.error or selection.package_dir is None:
            issues.append(
                CompatibilityIssue(
                    "addon.selection_invalid",
                    f"Installed add-on {addon_id!r} has an invalid active-version selection: "
                    f"{selection.error or 'no package directory was selected'}",
                    details={"root": str(paths.installed_addons_dir / addon_id)},
                )
            )
            continue
        package_dir = selection.package_dir
        manifest_path = package_dir / "manifest.json"
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            issues.append(
                CompatibilityIssue(
                    "addon.manifest_unreadable",
                    f"Installed add-on {addon_id!r} has no readable manifest: {exc}",
                    details={"path": str(manifest_path)},
                )
            )
            continue
        schema_errors = (
            validate_manifest_against_schema(payload)
            if isinstance(payload, dict)
            else ["Manifest root must be an object."]
        )
        if schema_errors:
            issues.append(
                CompatibilityIssue(
                    "addon.manifest_invalid",
                    f"Installed add-on {addon_id!r} has an invalid manifest.",
                    details={"errors": schema_errors[:20], "path": str(manifest_path)},
                )
            )
            continue
        requires = str((payload.get("addon_api") or {}).get("requires") or "").strip()
        try:
            compatible = bool(requires) and target_api in SpecifierSet(requires)
        except InvalidSpecifier:
            compatible = False
        if not compatible:
            issues.append(
                CompatibilityIssue(
                    "addon.api_incompatible",
                    f"Installed add-on {addon_id!r} requires Add-on API "
                    f"{requires or '(undeclared)'}, but release {release.version} "
                    f"provides {release.addon_api_version}.",
                    details={"path": str(manifest_path)},
                )
            )
    return tuple(installed), issues


def _validate_reusable_receipt(
    receipt: Mapping[str, Any],
    index: ReleaseIndex,
    release: ManagedRelease,
    artifact: ReleaseArtifact,
) -> None:
    """Require immutable runtime facts to match the newly authenticated index."""

    try:
        receipt_version = _safe_version(str(receipt.get("version") or ""))
    except ManagedInstallError as exc:
        raise ManagedInstallError(
            "Installed release receipt has an invalid version."
        ) from exc
    receipt_artifact = receipt.get("artifact")
    receipt_database = receipt.get("database")
    artifact_fields = ("platform", "kind", "filename", "sha256", "size")
    artifact_matches = isinstance(receipt_artifact, dict) and all(
        receipt_artifact.get(field) == getattr(artifact, field)
        for field in artifact_fields
    )
    database_matches = False
    if isinstance(receipt_database, dict):
        known_revisions = receipt_database.get("known_revisions")
        heads = receipt_database.get("heads")
        if (
            isinstance(known_revisions, (list, tuple))
            and all(isinstance(value, str) for value in known_revisions)
            and isinstance(heads, (list, tuple))
            and all(isinstance(value, str) for value in heads)
        ):
            database_matches = (
                tuple(sorted(known_revisions)) == release.database.known_revisions
                and tuple(sorted(heads)) == release.database.heads
                and receipt_database.get("rollback") == release.database.rollback
            )
    versioned_contract_matches = True
    if index.schema_version >= 2:
        versioned_contract_matches = (
            receipt.get("schema_version") == RELEASE_RECEIPT_VERSION
            and receipt.get("release_index_schema_version") == index.schema_version
            and receipt.get("release_contract_sha256")
            == release_contract_sha256(index, release)
        )
        if release.execution is not None and release.execution.data_impact == "none":
            probe_attestation = receipt.get("probe_attestation")
            versioned_contract_matches = versioned_contract_matches and (
                isinstance(probe_attestation, dict)
                and probe_attestation.get("format")
                == "carlina.managed-release-probe-attestation"
                and probe_attestation.get("schema_version") == 1
                and probe_attestation.get("status") == "passed"
                and probe_attestation.get("protocol")
                == release.execution.probe_protocol
                and probe_attestation.get("probe_schema_version")
                == release.execution.probe_schema_version
                and probe_attestation.get("isolation")
                == release.execution.probe_isolation
                and probe_attestation.get("target_version") == release.version
            )
    if (
        receipt_version != _safe_version(release.version)
        or not artifact_matches
        or not database_matches
        or receipt.get("addon_api_version") != release.addon_api_version
        or not versioned_contract_matches
    ):
        raise ManagedInstallError(
            f"Installed release {release.version} conflicts with the authenticated "
            "release contract; refusing to reuse it."
        )


def _active_receipt_overlay_base_archive_bytes(
    paths: AppPaths,
    layout: ManagedInstallationLayout,
) -> int | None:
    """Return actual v1 base bytes for a possible next overlay recovery set.

    This intentionally performs only receipt/catalog shape checks. The managed
    transaction later verifies the receipt hash, archive, base, and key set
    before it actually uses an overlay.  Reserving for a stale candidate is
    safe; under-reserving for a valid one is not.
    """

    try:
        current = layout.current_version()
        if current is None:
            return None
        receipt = layout.read_receipt(current)
        backup = receipt.get("backup")
        if not isinstance(backup, dict) or not isinstance(backup.get("path"), str):
            return None
        candidate = Path(backup["path"]).expanduser()
        if candidate.is_symlink():
            return None
        candidate = candidate.resolve()
        if candidate.parent != paths.backups_dir.resolve() or not candidate.is_file():
            return None
        manifest = inspect_backup(candidate)
        if manifest["version"] == BACKUP_VERSION:
            return candidate.stat().st_size
        base = manifest.get("base")
        filename = base.get("filename") if isinstance(base, dict) else None
        if not isinstance(filename, str) or Path(filename).name != filename:
            return None
        base_archive = candidate.parent / filename
        if base_archive.is_symlink() or not base_archive.is_file():
            return None
        return base_archive.stat().st_size
    except BackupError, ManagedInstallError, OSError, ValueError:
        return None


def _worst_case_backup_archive_bytes(expanded_bytes: int) -> int:
    """Bound a ZIP member set when only its uncompressed logical size is known."""

    return expanded_bytes + max(
        RECOVERY_ARCHIVE_MINIMUM_OVERHEAD_BYTES,
        expanded_bytes // 100,
    )


def preflight_release(
    paths: AppPaths,
    layout: ManagedInstallationLayout,
    release: ManagedRelease,
    artifact: ReleaseArtifact,
    *,
    artifact_already_staged: bool = False,
    recovery_plan: RecoveryPlan | None = None,
    execution_authenticated: bool = False,
    probe_capability: ManagedProbeCapability | None = None,
) -> CompatibilityReport:
    """Inspect data, installed add-ons, keys, and disk without mutating them."""

    selected_recovery = recovery_plan or legacy_opaque_recovery_plan()
    issues: list[CompatibilityIssue] = []
    if release.execution is not None and release.execution.data_impact != "opaque":
        if not execution_authenticated:
            issue_code = "release.execution_contract_unauthenticated"
            issue_message = (
                "This execution contract is not authenticated by a trusted release "
                "signature and cannot authorize a reduced-recovery path."
            )
            issue_details = {"data_impact": release.execution.data_impact}
        elif release.execution.data_impact == "none":
            capability = probe_capability
            if capability is not None and capability.available:
                issue_code = ""
                issue_message = ""
                issue_details = {}
            else:
                issue_code = "release.no_data_isolation_unavailable"
                issue_message = (
                    "This no-data execution contract requires certified host-filesystem "
                    "and network isolation, but the platform backend is unavailable."
                )
                issue_details = {
                    "data_impact": release.execution.data_impact,
                    "required_isolation": release.execution.probe_isolation,
                    "probe_protocol": release.execution.probe_protocol,
                    "capability": capability.to_dict() if capability else None,
                }
        else:
            issue_code = "release.constrained_execution_unavailable"
            issue_message = (
                "This signed release requires a constrained host execution "
                "coordinator that is not implemented."
            )
            issue_details = {
                "data_impact": release.execution.data_impact,
                "required_isolation": release.execution.probe_isolation,
                "probe_protocol": release.execution.probe_protocol,
            }
        if issue_code:
            issues.append(
                CompatibilityIssue(
                    issue_code,
                    issue_message,
                    details=issue_details,
                )
            )
    database_readable = True
    try:
        revisions = _database_revisions(paths.database_file)
    except sqlite3.Error as exc:
        revisions = ()
        database_readable = False
        issues.append(
            CompatibilityIssue(
                "database.unreadable",
                f"The database cannot be inspected safely: {exc}",
                details={"path": str(paths.database_file)},
            )
        )
    unknown = sorted(set(revisions) - set(release.database.known_revisions))
    if unknown:
        issues.append(
            CompatibilityIssue(
                "database.unknown_revision",
                "The database contains revisions unknown to the target release.",
                details={"unknown": unknown},
            )
        )
    if paths.database_file.is_file() and database_readable and not revisions:
        issues.append(
            CompatibilityIssue(
                "database.unversioned",
                "The existing database has no Alembic revision; initialize or repair it before updating.",
            )
        )
    installed_addons, addon_issues = _addon_compatibility(paths, release)
    issues.extend(addon_issues)

    if paths.database_file.is_file():
        try:
            InstallationKeyStore(paths.master_keys_file).public_metadata()
        except (EncryptionError, OSError, ValueError) as exc:
            issues.append(
                CompatibilityIssue(
                    "encryption.keys_unavailable",
                    f"Installation encryption keys are unavailable: {exc}",
                )
            )

    program_anchor = _existing_disk_anchor(layout.root.parent)
    data_anchor = _existing_disk_anchor(paths.data_root)
    program_free = shutil.disk_usage(program_anchor).free
    data_free = shutil.disk_usage(data_anchor).free
    database_size = (
        paths.database_file.stat().st_size if paths.database_file.is_file() else 0
    )
    backup_expanded_bytes = 0
    if (
        paths.database_file.is_file()
        and selected_recovery.selected_coverage is RecoveryCoverage.FULL
    ):
        try:
            backup_expanded_bytes = estimate_backup_expanded_bytes(paths)
        except (BackupError, OSError) as exc:
            issues.append(
                CompatibilityIssue(
                    "backup.preflight_failed",
                    f"The rollback backup payload cannot be measured safely: {exc}",
                )
            )
    runtime_staging_required = (
        MIN_PROGRAM_HEADROOM_BYTES
        if artifact_already_staged
        else max(MIN_UNPACK_TRANSACTION_BYTES, artifact.size * 6)
    )
    # A recovery copy lives outside the data root so it survives the atomic
    # root switch. When the active receipt has a catalog-local paired backup,
    # the next update may emit an overlay. Automatic rollback then needs a
    # private recovery catalog holding both a complete base and the overlay;
    # reserve the actual v1 base plus a worst-case overlay, not the old single
    # payload estimate.
    recovery_archive_required = 0
    base_archive_bytes = None
    if selected_recovery.selected_coverage is RecoveryCoverage.FULL:
        recovery_archive_required = backup_expanded_bytes
        base_archive_bytes = _active_receipt_overlay_base_archive_bytes(paths, layout)
    elif selected_recovery.selected_coverage is RecoveryCoverage.DATABASE_ONLY:
        recovery_archive_required = _worst_case_backup_archive_bytes(database_size)
    if backup_expanded_bytes and base_archive_bytes is not None:
        recovery_archive_required = (
            base_archive_bytes + _worst_case_backup_archive_bytes(backup_expanded_bytes)
        )
    program_required = runtime_staging_required + recovery_archive_required
    program_space_details: dict[str, int] = {
        "free": program_free,
        "required": program_required,
    }
    program_space_message = "The program volume lacks space for side-by-side staging."
    if recovery_archive_required:
        program_space_message = (
            "The program volume lacks space for side-by-side staging and "
            "the selected recovery material."
        )
        program_space_details["recovery_archive_required"] = recovery_archive_required
    protected_data_bytes = (
        backup_expanded_bytes
        if selected_recovery.selected_coverage is RecoveryCoverage.FULL
        else database_size
    )
    data_required = (
        max(
            MIN_DATA_HEADROOM_BYTES,
            protected_data_bytes * 2 + database_size + MIN_DATA_HEADROOM_BYTES,
        )
        if paths.database_file.is_file()
        else MIN_DATA_HEADROOM_BYTES
    )
    if program_free < program_required:
        issues.append(
            CompatibilityIssue(
                "storage.program_space",
                program_space_message,
                details=program_space_details,
            )
        )
    if data_free < data_required:
        issues.append(
            CompatibilityIssue(
                "storage.data_space",
                "The data volume lacks space for the selected update recovery plan.",
                details={"free": data_free, "required": data_required},
            )
        )
    try:
        same_volume = program_anchor.stat().st_dev == data_anchor.stat().st_dev
    except OSError as exc:
        issues.append(
            CompatibilityIssue(
                "storage.volume_unknown",
                f"The program and data volumes cannot be compared safely: {exc}",
            )
        )
    else:
        combined_required = program_required + data_required
        if same_volume and min(program_free, data_free) < combined_required:
            issues.append(
                CompatibilityIssue(
                    "storage.combined_space",
                    "The shared program/data volume lacks space for the complete "
                    "staging and rollback transaction.",
                    details={
                        "free": min(program_free, data_free),
                        "required": combined_required,
                    },
                )
            )
    return CompatibilityReport(
        target_version=release.version,
        database_revisions=revisions,
        installed_addons=installed_addons,
        program_free_bytes=program_free,
        data_free_bytes=data_free,
        recovery_plan=selected_recovery,
        issues=tuple(issues),
    )


def _pid_is_running(pid: int) -> bool:
    return process_is_running(pid)


def _runtime_leader_pid(leader: Mapping[str, object]) -> int:
    """Parse persisted JSON leader metadata without trusting its value type."""

    raw_pid = leader.get("pid")
    if not isinstance(raw_pid, (int, float, str)):
        return 0
    try:
        return int(raw_pid)
    except TypeError, ValueError:
        return 0


def _assert_runtime_stopped(paths: AppPaths) -> None:
    leader = load_runtime_leader(paths)
    if not leader:
        return
    pid = _runtime_leader_pid(leader)
    if pid > 0 and _pid_is_running(pid):
        raise ManagedRuntimeActiveError(
            f"Managed runtime PID {pid} is active. Run 'carlina stop' before updating or rolling back."
        )


def _acquire_runtime_maintenance_lock(paths: AppPaths) -> RuntimeLeaderLock:
    """Exclude a managed runtime for the complete data-mutation transaction."""

    lock = runtime_leader_lock(paths, timeout=0)
    try:
        lock.acquire()
    except FileLockTimeout as exc:
        leader = load_runtime_leader(paths) or {}
        detail = f" PID {leader.get('pid')}" if leader.get("pid") else ""
        raise ManagedRuntimeActiveError(
            "The managed runtime is active and holds the data-root lease."
            f"{detail} Stop Carlina before updating or rolling back."
        ) from exc
    try:
        leader = load_runtime_leader(paths) or {}
        leader_pid = _runtime_leader_pid(leader)
        if leader_pid > 0 and _pid_is_running(leader_pid):
            raise ManagedRuntimeActiveError(
                "Runtime leader metadata names live PID "
                f"{leader_pid}, but no process holds the runtime lease. "
                "Refusing maintenance because ownership is ambiguous."
            )

        # Owning the lease proves that no managed runtime can be active. These
        # files may survive a crash and must not later authorize signalling a
        # different process after PID reuse.
        (paths.state_dir / "runtime" / "leader.json").unlink(missing_ok=True)
        (paths.state_dir / "bootstrap" / "server.pid").unlink(missing_ok=True)
    except Exception:
        lock.release()
        raise
    return lock


def _current_distribution_version() -> str:
    try:
        return package_version("carlina")
    except PackageNotFoundError:
        return "0+development"


def _target_environment(
    paths: AppPaths,
    layout: ManagedInstallationLayout,
    version: str,
    *,
    application_profile: RuntimeProfile | str | None = None,
    update_transaction_id: str | None = None,
) -> dict[str, str]:
    selected_profile = resolve_runtime_composition(application_profile).profile
    environment = dict(os.environ)
    environment.update(
        {
            "CARLINA_HOME": str(paths.data_root),
            "CARLINA_PROGRAM_ROOT": str(layout.root),
            "CARLINA_MANAGED_VERSION": version,
            "CARLINA_RUNTIME_ROLE": "command",
            "CARLINA_PROFILE": selected_profile.value,
            "PYTHONNOUSERSITE": "1",
        }
    )
    if update_transaction_id is not None:
        if not _VERSION_NAME_PATTERN.fullmatch(update_transaction_id):
            raise ManagedInstallError("Managed update transaction ID is unsafe.")
        environment[MANAGED_UPDATE_TRANSACTION_ENVIRONMENT] = update_transaction_id
    else:
        environment.pop(MANAGED_UPDATE_TRANSACTION_ENVIRONMENT, None)
    return environment


def _run_checked(
    command: list[str],
    *,
    cwd: Path,
    environment: Mapping[str, str] | None = None,
    timeout: float = 900,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        cwd=str(cwd),
        env=dict(environment) if environment is not None else None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        if len(detail) > 4000:
            detail = detail[-4000:]
        raise ManagedInstallError(
            f"Command failed ({result.returncode}): {' '.join(command[:4])}\n{detail}"
        )
    return result


def _publisher_powershell() -> str:
    if os.name == "nt":
        native = (
            Path(os.environ.get("SystemRoot", r"C:\Windows"))
            / "System32"
            / "WindowsPowerShell"
            / "v1.0"
            / "powershell.exe"
        )
        if native.is_file():
            return str(native)
    executable = shutil.which("powershell.exe") or shutil.which("pwsh")
    if executable is None:
        raise ManagedInstallError(
            "PowerShell is required for the Windows runtime setup."
        )
    return executable


def _unpack_runtime(
    artifact: Path,
    destination: Path,
    *,
    kind: str | None = None,
    lifecycle: InstallationLifecycle | None = None,
) -> None:
    suffix = artifact.suffix.casefold()
    # Preserve direct legacy callers while the signed release path always
    # supplies its verified artifact kind. Never infer a new executable kind.
    selected_kind = (
        {".zip": "pixi-pack-bundle", ".sh": "pixi-pack-executable"}.get(suffix)
        if kind is None
        else kind
    )
    if (selected_kind, suffix) not in {
        ("pixi-pack-bundle", ".zip"),
        ("pixi-pack-executable", ".sh"),
        (PUBLISHER_BOOTSTRAP_EXECUTABLE_KIND, ".sh"),
        (PUBLISHER_BOOTSTRAP_EXECUTABLE_KIND, ".ps1"),
    }:
        raise ManagedInstallError(
            "Runtime artifact kind and filename are incompatible."
        )
    staging_root = destination.parent.parent / "staging"
    staging_root.mkdir(parents=True, exist_ok=True)
    scratch = staging_root / f"unpack-{uuid.uuid4().hex}"
    scratch.mkdir(mode=0o700, parents=False, exist_ok=False)
    environment = dict(os.environ)
    # pixi-unpack expands both the conda cache and wheel payload beneath the
    # process temp directory. Keep that potentially multi-gigabyte work on the
    # preflighted program volume instead of inheriting a small system drive.
    environment.update(
        {
            "TEMP": str(scratch),
            "TMP": str(scratch),
            "TMPDIR": str(scratch),
        }
    )
    try:
        if selected_kind == "pixi-pack-bundle":
            try:
                payloads = extract_windows_bundle(artifact, scratch)
            except WindowsBundleError as exc:
                raise ManagedInstallError(
                    f"Windows runtime bundle is invalid: {exc}"
                ) from exc
            command = [
                str(payloads["pixi-unpack.exe"]),
                str(payloads["archive.tar"]),
            ]
        elif suffix == ".ps1":
            command = [
                _publisher_powershell(),
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(artifact),
            ]
        else:
            artifact.chmod(0o700)
            command = ["bash", str(artifact)]
        if (
            lifecycle is not None
            and selected_kind == PUBLISHER_BOOTSTRAP_EXECUTABLE_KIND
        ):
            cancellation_file = scratch / "publisher.cancel"
            if suffix == ".ps1":
                command.extend(
                    ["-ProgressRecordsV1", "-CancelFile", str(cancellation_file)]
                )
            else:
                command.extend(
                    ["--progress-records-v1", "--cancel-file", str(cancellation_file)]
                )
            run_publisher_bootstrap(
                command,
                cwd=destination,
                environment=environment,
                cancellation_file=cancellation_file,
                lifecycle=lifecycle,
                timeout=3600,
            )
        else:
            _run_checked(
                command,
                cwd=destination,
                environment=environment,
                timeout=3600,
            )
    finally:
        # The name and parent are created above from immutable layout paths;
        # never apply this recursive cleanup to caller-controlled locations.
        shutil.rmtree(scratch, ignore_errors=True)


def _verify_unpacked_application_profile(
    layout: ManagedInstallationLayout,
    version: str,
    expected_profile: RuntimeProfile,
) -> None:
    """Inspect package identity as data before executing any target Python."""

    from carlina.distribution import parse_application_distribution

    environment_root = layout.version_dir(version) / "env"
    relative_path = (
        Path("Lib/site-packages/carlina/distribution.json")
        if os.name == "nt"
        else Path("lib/python3.14/site-packages/carlina/distribution.json")
    )
    descriptor = environment_root / relative_path
    try:
        descriptor_info = descriptor.lstat()
    except FileNotFoundError:
        actual_profile = RuntimeProfile.WORKBENCH
    else:
        try:
            if not stat.S_ISREG(descriptor_info.st_mode):
                raise ManagedInstallError("Unpacked application descriptor is unsafe.")
            current = environment_root
            for part in relative_path.parts[:-1]:
                current = current / part
                if current.is_symlink():
                    raise ManagedInstallError(
                        "Unpacked application descriptor is unsafe."
                    )
            if not descriptor.resolve().is_relative_to(environment_root.resolve()):
                raise ManagedInstallError("Unpacked application descriptor is unsafe.")
            descriptor_fd = os.open(
                descriptor,
                os.O_RDONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0),
            )
            try:
                if not stat.S_ISREG(os.fstat(descriptor_fd).st_mode):
                    raise ManagedInstallError(
                        "Unpacked application descriptor is unsafe."
                    )
                with os.fdopen(descriptor_fd, "rb") as handle:
                    descriptor_fd = -1
                    payload = handle.read(4097)
            finally:
                if descriptor_fd >= 0:
                    os.close(descriptor_fd)
            if len(payload) > 4096:
                raise ManagedInstallError(
                    "Unpacked application descriptor is too large."
                )
            actual_profile = parse_application_distribution(payload).profile
        except OSError as exc:
            raise ManagedInstallError(
                "Unpacked application descriptor is unreadable."
            ) from exc
    if actual_profile is not expected_profile:
        raise ManagedInstallError(
            "Unpacked runtime application profile is incompatible: "
            f"expected {expected_profile.value}, got {actual_profile.value}."
        )


def _target_pixi_version(
    layout: ManagedInstallationLayout,
    version: str,
) -> Version:
    """Read the Pixi version declared by the exact unpacked runtime as data."""

    version_dir = layout.versions_dir / _safe_version(version)
    environment_root = version_dir / "env"
    conda_meta = environment_root / "conda-meta"
    directories = (
        layout.root,
        layout.versions_dir,
        version_dir,
        environment_root,
        conda_meta,
    )
    try:
        for directory in directories:
            info = directory.lstat()
            is_junction = getattr(directory, "is_junction", lambda: False)()
            if not stat.S_ISDIR(info.st_mode) or directory.is_symlink() or is_junction:
                raise ManagedInstallError(
                    "Unpacked runtime Pixi package metadata is unsafe."
                )
        records = [
            path
            for path in conda_meta.iterdir()
            if path.name.startswith("pixi-") and path.name.endswith(".json")
        ]
    except OSError as exc:
        raise ManagedInstallError(
            "Unpacked runtime Pixi package metadata is unreadable."
        ) from exc
    if len(records) != 1:
        raise ManagedInstallError(
            "Unpacked runtime Pixi package metadata is missing or ambiguous."
        )

    record_path = records[0]
    record_fd: int | None = None
    try:
        record_info = record_path.lstat()
        is_junction = getattr(record_path, "is_junction", lambda: False)()
        if (
            not stat.S_ISREG(record_info.st_mode)
            or record_path.is_symlink()
            or is_junction
        ):
            raise ManagedInstallError(
                "Unpacked runtime Pixi package metadata is unsafe."
            )
        if record_info.st_size > _MAX_TARGET_PIXI_PACKAGE_RECORD_BYTES:
            raise ManagedInstallError(
                "Unpacked runtime Pixi package metadata exceeds the size limit."
            )
        flags = os.O_RDONLY
        flags |= getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_NONBLOCK", 0)
        record_fd = os.open(record_path, flags)
        opened_info = os.fstat(record_fd)
        if (
            not stat.S_ISREG(opened_info.st_mode)
            or opened_info.st_size > _MAX_TARGET_PIXI_PACKAGE_RECORD_BYTES
            or (opened_info.st_dev, opened_info.st_ino)
            != (record_info.st_dev, record_info.st_ino)
        ):
            raise ManagedInstallError(
                "Unpacked runtime Pixi package metadata is unsafe."
            )
        with os.fdopen(record_fd, "rb") as handle:
            record_fd = None
            payload = handle.read(_MAX_TARGET_PIXI_PACKAGE_RECORD_BYTES + 1)
            final_info = os.fstat(handle.fileno())
        if (
            len(payload) > _MAX_TARGET_PIXI_PACKAGE_RECORD_BYTES
            or final_info.st_size > _MAX_TARGET_PIXI_PACKAGE_RECORD_BYTES
        ):
            raise ManagedInstallError(
                "Unpacked runtime Pixi package metadata exceeds the size limit."
            )
    except OSError as exc:
        raise ManagedInstallError(
            "Unpacked runtime Pixi package metadata is unreadable."
        ) from exc
    finally:
        if record_fd is not None:
            os.close(record_fd)

    try:
        record = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManagedInstallError(
            "Unpacked runtime Pixi package metadata is invalid."
        ) from exc
    if not isinstance(record, dict) or record.get("name") != "pixi":
        raise ManagedInstallError("Unpacked runtime Pixi package metadata is invalid.")
    raw_version = record.get("version")
    package_files = record.get("files")
    expected_file = "bin/pixi.exe" if os.name == "nt" else "bin/pixi"
    if (
        not isinstance(raw_version, str)
        or not isinstance(package_files, list)
        or any(not isinstance(path, str) for path in package_files)
        or expected_file not in package_files
    ):
        raise ManagedInstallError("Unpacked runtime Pixi package metadata is invalid.")
    try:
        target_version = Version(raw_version)
    except InvalidVersion as exc:
        raise ManagedInstallError(
            "Unpacked runtime Pixi package metadata is invalid."
        ) from exc
    if (
        str(target_version) != raw_version
        or target_version.epoch != 0
        or len(target_version.release) != 3
        or target_version <= Version("0")
        or target_version.pre is not None
        or target_version.post is not None
        or target_version.dev is not None
        or target_version.local is not None
    ):
        raise ManagedInstallError("Unpacked runtime Pixi package metadata is invalid.")
    return target_version


def _verify_target_runtime(
    layout: ManagedInstallationLayout,
    paths: AppPaths,
    version: str,
    *,
    initialize_data: bool,
    application_profile: RuntimeProfile | str | None = None,
    update_transaction_id: str | None = None,
    lifecycle: InstallationLifecycle | None = None,
) -> None:
    if lifecycle is not None:
        lifecycle.advance(InstallationPhase.ENVIRONMENT, cancellation_safe=False)
    selected_profile = resolve_runtime_composition(application_profile).profile
    entry_module = (
        "carlina.documents"
        if selected_profile is RuntimeProfile.DOCUMENTS
        else "carlina.bootstrap_manager"
    )
    pixi = layout.pixi_executable(version)
    expected_pixi_version = _target_pixi_version(layout, version)
    pixi_probe = _run_checked(
        [str(pixi), "--version"],
        cwd=layout.version_dir(version),
    )
    reported = re.fullmatch(
        r"pixi (\d+\.\d+\.\d+)(?:\r?\n)?",
        pixi_probe.stdout,
    )
    reported_version = None
    if reported is not None:
        try:
            parsed_reported = Version(reported.group(1))
        except InvalidVersion:
            pass
        else:
            if str(parsed_reported) == reported.group(1):
                reported_version = parsed_reported
    if reported_version != expected_pixi_version:
        actual = (
            str(reported_version)
            if reported_version is not None
            else "unrecognized output"
        )
        raise ManagedInstallError(
            "Unpacked runtime Pixi version is incompatible: "
            f"declared {expected_pixi_version}, reported {actual}."
        )

    python = layout.python_executable(version)
    environment = _target_environment(
        paths,
        layout,
        version,
        application_profile=selected_profile,
        update_transaction_id=update_transaction_id,
    )
    probe = _run_checked(
        [
            str(python),
            "-c",
            ("import importlib.metadata as m; print(m.version('carlina'))"),
        ],
        cwd=layout.version_dir(version),
        environment=environment,
    )
    installed_version = probe.stdout.strip().splitlines()[-1]
    if Version(installed_version) != Version(version):
        raise ManagedInstallError(
            f"Unpacked runtime reports Carlina {installed_version}, expected {version}."
        )
    profile_probe = (
        "import importlib.util; "
        "spec=importlib.util.find_spec('carlina.distribution'); "
        "profile='workbench' if spec is None else __import__("
        "'carlina.distribution', fromlist=['read_application_distribution']"
        ").read_application_distribution().profile.value; "
        "print(profile)"
    )
    profile_result = _run_checked(
        [str(python), "-I", "-c", profile_probe],
        cwd=layout.version_dir(version),
        environment=environment,
    )
    installed_profile = profile_result.stdout.strip()
    if installed_profile != selected_profile.value:
        raise ManagedInstallError(
            "Unpacked runtime application profile is incompatible: "
            f"expected {selected_profile.value}, got {installed_profile!r}."
        )
    _run_checked(
        [str(python), "-m", entry_module, "--help"],
        cwd=layout.version_dir(version),
        environment=environment,
    )
    if not initialize_data:
        return
    if lifecycle is not None:
        lifecycle.advance(InstallationPhase.INITIALIZATION, cancellation_safe=False)
    _run_checked(
        [
            str(python),
            "-m",
            entry_module,
            "init",
            "--repair",
            "--no-admin-prompt",
        ],
        cwd=layout.version_dir(version),
        environment=environment,
        timeout=TARGET_INIT_TIMEOUT_SECONDS,
    )
    if lifecycle is not None:
        lifecycle.advance(InstallationPhase.READINESS, cancellation_safe=False)
    _run_checked(
        [
            str(python),
            "-m",
            entry_module,
            "doctor",
            "--json",
        ],
        cwd=layout.version_dir(version),
        environment=environment,
        timeout=600,
    )
    health_code = (
        "import json; "
        "from carlina import create_app; "
        "app=create_app(); "
        "client=app.test_client(); "
        "response=client.get('/health/ready'); "
        "payload=response.get_json(); "
        "assert response.status_code == 200 and payload.get('status') == 'ready', payload; "
        "print(json.dumps(payload, sort_keys=True))"
    )
    _run_checked(
        [str(python), "-c", health_code],
        cwd=layout.version_dir(version),
        environment=environment,
        timeout=900,
    )


class _TargetLifecycleOptions(TypedDict, total=False):
    lifecycle: InstallationLifecycle


class ManagedReleaseManager:
    """Coordinate a durable, offline-safe managed release transaction."""

    def __init__(
        self,
        paths: AppPaths,
        *,
        program_root: str | os.PathLike[str] | None = None,
        platform_tag: str | None = None,
        trust_store: str | os.PathLike[str] | None = None,
    ) -> None:
        self.paths = paths
        self.application_profile = resolve_runtime_composition().profile
        validate_runtime_profile(paths.data_root, self.application_profile)
        self.layout = ManagedInstallationLayout.resolve(program_root)
        self.layout.validate_installation_binding(
            self.application_profile, data_root=paths.data_root
        )
        self.platform_tag = platform_tag or current_platform_tag()
        self.trust_store = (
            Path(trust_store or Path(__file__).with_name("release_trust.json"))
            .expanduser()
            .resolve()
        )

    def _verified_index(
        self,
        source: str | os.PathLike[str],
        *,
        channel: str,
        allow_unsigned_local: bool,
    ) -> tuple[ReleaseIndex, str | None, str]:
        raw = read_release_index_source(source)
        index = parse_release_index(raw)
        if index.channel != channel:
            raise ManagedInstallError(
                f"Requested {channel!r}, but the release index is for {index.channel!r}."
            )
        allow_unsigned = allow_unsigned_local and is_local_release_source(source)
        if allow_unsigned_local and not is_local_release_source(source):
            raise ManagedInstallError(
                "Unsigned release indexes are permitted only for explicit local-file testing."
            )
        trusted: tuple[TrustedReleaseKey, ...] = ()
        if index.signatures or not allow_unsigned:
            trusted = load_trusted_keys(self.trust_store)
        signer = verify_release_index(index, trusted, allow_unsigned=allow_unsigned)
        if index.effective_application_profile != self.application_profile.value:
            raise ManagedInstallError(
                "Release index application profile does not match managed installation: "
                f"expected {self.application_profile.value}, "
                f"got {index.effective_application_profile}."
            )
        return index, signer, sha256_file_bytes(raw)

    def _probe_capability(
        self,
        release: ManagedRelease,
        *,
        signer_key_id: str | None,
    ) -> ManagedProbeCapability | None:
        execution = release.execution
        if (
            signer_key_id is None
            or execution is None
            or execution.data_impact != "none"
        ):
            return None
        return detect_managed_probe_capability(self.platform_tag)

    def check(
        self,
        index_source: str | os.PathLike[str],
        *,
        channel: str = "stable",
        requested_version: str | None = None,
        allow_unsigned_local: bool = False,
        recovery_coverage: RecoveryCoverage | str = RecoveryCoverage.AUTOMATIC,
        recovery_decision_source: RecoveryDecisionSource
        | str = RecoveryDecisionSource.AUTOMATIC,
        recovery_actor: str | None = None,
        recovery_reason: str | None = None,
        recovery_policy_id: str | None = None,
    ) -> ReleaseCheckResult:
        index, signer, _index_sha = self._verified_index(
            index_source,
            channel=channel,
            allow_unsigned_local=allow_unsigned_local,
        )
        managed_current = self.layout.current_version()
        current = managed_current or _current_distribution_version()
        release = index.select(
            current_version=managed_current,
            requested_version=requested_version,
        )
        if release is None:
            return ReleaseCheckResult(
                channel,
                current,
                None,
                self.platform_tag,
                signer,
                None,
            )
        artifact = release.artifact_for(self.platform_tag)
        recovery_plan = self._recovery_plan(
            managed_current,
            release=release,
            signer_key_id=signer,
            requested_coverage=recovery_coverage,
            decision_source=recovery_decision_source,
            actor=recovery_actor,
            reason=recovery_reason,
            policy_id=recovery_policy_id,
        )
        probe_capability = self._probe_capability(
            release,
            signer_key_id=signer,
        )
        compatibility = preflight_release(
            self.paths,
            self.layout,
            release,
            artifact,
            recovery_plan=recovery_plan,
            execution_authenticated=signer is not None,
            probe_capability=probe_capability,
        )
        return ReleaseCheckResult(
            channel,
            current,
            release.version,
            self.platform_tag,
            signer,
            compatibility,
        )

    def _write_journal(self, payload: dict[str, Any], *, phase: str) -> None:
        payload["phase"] = phase
        payload["updated_at"] = _now()
        _atomic_json(self.layout.journal_file, payload)

    def _archive_existing_journal(self) -> None:
        if not self.layout.journal_file.is_file():
            return
        try:
            payload = json.loads(self.layout.journal_file.read_text(encoding="utf-8"))
        except OSError, json.JSONDecodeError:
            payload = {"transaction_id": "unreadable", "archived_at": _now()}
        transaction_id = str(payload.get("transaction_id") or "unknown")
        safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", transaction_id)[:80]
        destination = self.layout.history_dir / f"update-{safe_id}.json"
        if destination.exists():
            destination = (
                self.layout.history_dir
                / f"update-{safe_id}-{uuid.uuid4().hex[:8]}.json"
            )
        os.replace(self.layout.journal_file, destination)

    def _quarantine_incomplete_release(
        self,
        version: str,
        *,
        transaction_id: str,
    ) -> Path | None:
        """Move a never-adopted target aside so a verified retry can proceed.

        A release with a valid receipt is immutable history and is never
        removed automatically. An active release is likewise untouchable.
        Only a non-symlink directory without a valid receipt is eligible.
        """

        lexical_target = self.layout.versions_dir / _safe_version(version)
        if (
            self.layout.versions_dir.is_symlink()
            or self.layout.versions_dir.is_junction()
            or lexical_target.is_symlink()
            or lexical_target.is_junction()
        ):
            raise ManagedInstallError(
                "Refusing to quarantine a linked managed generation."
            )
        try:
            if bootstrap_markers_present(lexical_target):
                raise ManagedInstallError(
                    "A native bootstrap owns this generation; use bound adoption recovery."
                )
        except BootstrapOwnershipError as exc:
            raise ManagedInstallError(
                "Native bootstrap ownership cannot be inspected safely."
            ) from exc
        target = self.layout.version_dir(version)
        if not target.exists():
            return None
        if target.is_symlink():
            raise ManagedInstallError(
                f"Refusing to alter symbolic-link release directory: {target}"
            )
        if self.layout.current_version() == _safe_version(version):
            raise ManagedInstallError(
                f"Refusing to quarantine active release {version}."
            )
        try:
            self.layout.read_receipt(version)
        except ManagedInstallError:
            pass
        else:
            raise ManagedInstallError(
                f"Release {version} is already installed with a valid receipt."
            )
        self.layout.failed_dir.mkdir(parents=True, exist_ok=True)
        destination = self.layout.failed_dir / (
            f"{_safe_version(version)}-{transaction_id[:12]}"
        )
        if destination.exists():
            destination = self.layout.failed_dir / (
                f"{_safe_version(version)}-{transaction_id[:12]}-{uuid.uuid4().hex[:8]}"
            )
        for attempt in range(QUARANTINE_RENAME_ATTEMPTS):
            try:
                os.replace(target, destination)
                break
            except PermissionError:
                # Windows/SMB can retain a child process' working-directory
                # handle briefly after it exits. Retry only this exact,
                # already-validated rename; all other failures remain fatal.
                if attempt + 1 >= QUARANTINE_RENAME_ATTEMPTS:
                    raise
                time.sleep(QUARANTINE_RENAME_DELAY_SECONDS)
        return destination

    def _acquire_lock(self) -> FileLock:
        self.layout.ensure()
        lock = FileLock(str(self.layout.lock_file), timeout=0)
        try:
            lock.acquire()
        except FileLockTimeout as exc:
            raise ManagedInstallError(
                "Another managed install operation is running."
            ) from exc
        return lock

    def _select_managed_backup_base(
        self,
        active_version: str | None,
        *,
        key_store: InstallationKeyStore,
    ) -> Path | None:
        """Return only a receipt-bound, verified complete backup base.

        A managed update must never infer a base from catalog recency.  The
        current release receipt is the sole authority, and every broken or
        incomplete candidate deliberately degrades to a new complete v1
        backup rather than risking a weak recovery point.
        """

        if active_version is None:
            return None
        try:
            receipt = self.layout.read_receipt(active_version)
            backup = receipt.get("backup")
            if not isinstance(backup, dict):
                return None
            source_value = backup.get("path")
            expected_digest = backup.get("sha256")
            if (
                not isinstance(source_value, str)
                or not isinstance(expected_digest, str)
                or not re.fullmatch(r"[0-9a-f]{64}", expected_digest)
            ):
                return None
            source = Path(source_value).expanduser().resolve()
            if (
                source.parent != self.paths.backups_dir.resolve()
                or not source.is_file()
            ):
                return None
            if sha256_file(source) != expected_digest:
                return None
            manifest = verify_backup(
                source,
                available_key_ids=key_store.key_ids(),
            )
            if manifest["version"] == BACKUP_VERSION:
                return source
            base, _base_manifest = resolve_backup_base(
                source,
                manifest,
                available_key_ids=key_store.key_ids(),
            )
            return base
        except BackupError, ManagedInstallError, OSError, ValueError:
            return None

    def _create_managed_backup(
        self,
        active_version: str | None,
        *,
        key_store: InstallationKeyStore,
    ) -> BackupRecord:
        """Create a complete backup or a direct overlay from the receipt base."""

        return create_backup(
            self.paths,
            key_store=key_store,
            base_archive=self._select_managed_backup_base(
                active_version,
                key_store=key_store,
            ),
        )

    def _recent_recovery_anchor_id(self, active_version: str | None) -> str | None:
        """Return receipt-bound full-recovery identity without payload traversal."""

        if active_version is None:
            return None
        try:
            receipt = self.layout.read_receipt(active_version)
        except ManagedInstallError:
            return None
        material = receipt.get("backup")
        if isinstance(material, dict) and isinstance(material.get("backup_id"), str):
            return str(material["backup_id"])
        return None

    def _recovery_plan(
        self,
        active_version: str | None,
        *,
        release: ManagedRelease,
        signer_key_id: str | None,
        requested_coverage: RecoveryCoverage | str,
        decision_source: RecoveryDecisionSource | str,
        actor: str | None,
        reason: str | None,
        policy_id: str | None,
    ) -> RecoveryPlan:
        """Use only authenticated v2 impact; legacy/unsigned remains opaque."""

        return plan_recovery(
            declared_impact=(
                release.execution.data_impact if release.execution is not None else None
            ),
            declaration_authenticated=(
                release.execution is not None and signer_key_id is not None
            ),
            requested_coverage=requested_coverage,
            decision_source=decision_source,
            actor=actor,
            reason=reason,
            policy_id=policy_id,
            recent_recovery_anchor=self._recent_recovery_anchor_id(active_version),
        )

    def _read_retained_adoption_receipt(
        self,
        target: str,
    ) -> dict[str, Any] | None:
        path = self.layout.versions_dir / target / "release-receipt.json"
        if not path.exists() and not path.is_symlink():
            return None
        receipt = _read_bounded_regular_json_object(path)
        if (
            receipt.get("format") != RELEASE_RECEIPT_FORMAT
            or type(receipt.get("schema_version")) is not int
            or receipt.get("schema_version") != RELEASE_RECEIPT_VERSION
        ):
            raise ManagedInstallError("Retained adoption receipt is invalid.")
        return receipt

    def _validate_native_bootstrap_generation(
        self,
        version_dir: Path,
        index: ReleaseIndex,
        release: ManagedRelease,
        artifact: ReleaseArtifact,
        index_sha256: str,
        *,
        required_claim_id: object = None,
    ) -> str | None:
        try:
            if not bootstrap_markers_present(version_dir):
                if required_claim_id is not None:
                    raise BootstrapOwnershipError()
                return None
            identity = BootstrapIdentity(
                self.application_profile.value,
                release.version,
                index.channel,
                index_sha256,
                artifact.platform,
                artifact.kind,
                artifact.filename,
                artifact.sha256,
                artifact.size,
            )
            claim_id = validate_bootstrap_generation(
                version_dir,
                identity,
                program_root=self.layout.root,
                data_root=self.paths.data_root,
            )
            if required_claim_id is not None and claim_id != required_claim_id:
                raise BootstrapOwnershipError()
            return claim_id
        except (BootstrapOwnershipError, OSError) as exc:
            raise ManagedInstallError(
                "Native bootstrap ownership does not match this managed operation."
            ) from exc

    def _validate_native_journal_roots(self) -> None:
        """Fence native recovery before any restore or journal replacement."""

        self.layout.validate_installation_binding(
            self.application_profile, data_root=self.paths.data_root
        )
        path = self.layout.journal_file
        if not path.exists() and not path.is_symlink():
            return
        journal = _read_bounded_regular_json_object(path)
        if not {"bootstrap_claim_id", "bootstrap_index_sha256"}.intersection(journal):
            return
        claim_id = journal.get("bootstrap_claim_id")
        index_sha = journal.get("bootstrap_index_sha256")
        if (
            not isinstance(claim_id, str)
            or re.fullmatch(r"[0-9a-f]{32}", claim_id) is None
            or not isinstance(index_sha, str)
            or _LOWER_SHA256_PATTERN.fullmatch(index_sha) is None
            or journal.get("application_profile") != self.application_profile.value
            or journal.get("program_root") != str(self.layout.root.resolve())
            or journal.get("data_root") != str(self.paths.data_root.resolve())
        ):
            raise ManagedInstallError(
                "Native bootstrap recovery belongs to another installation boundary."
            )

    def _reject_bootstrap_candidate_receipt(self, target: str) -> None:
        """Fresh adoption must not replace existing candidate commit evidence."""

        try:
            self.layout.receipt_file(target).lstat()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise ManagedInstallError(
                "Native bootstrap resume cannot reconcile the retained transaction."
            ) from exc
        raise ManagedInstallError(
            "Native bootstrap resume cannot reconcile the retained transaction."
        )

    def _bootstrap_resume_transaction(
        self,
        claim_id: str,
        target: str,
    ) -> str | None:
        """Select a native resume transaction while both maintenance leases are held."""

        error_message = (
            "Native bootstrap resume cannot reconcile the retained transaction."
        )
        self._reject_pending_adoption_fences()

        journal_path = self.layout.journal_file
        try:
            journal_path.lstat()
        except FileNotFoundError:
            journal_is_present = False
        except OSError as exc:
            raise ManagedInstallError(error_message) from exc
        else:
            journal_is_present = True
        if not journal_is_present:
            self._reject_bootstrap_candidate_receipt(target)
            try:
                current = self.layout.current_version()
            except ManagedInstallError as exc:
                raise ManagedInstallError(error_message) from exc
            if current is not None and Version(target) <= Version(current):
                raise ManagedInstallError(error_message)
            return None

        try:
            journal = _read_bounded_regular_json_object(journal_path)
        except ManagedInstallError as exc:
            raise ManagedInstallError(error_message) from exc

        if journal.get("bootstrap_claim_id") == claim_id:
            transaction_id = journal.get("transaction_id")
            if not isinstance(
                transaction_id, str
            ) or not _TRANSACTION_ID_PATTERN.fullmatch(transaction_id):
                raise ManagedInstallError(error_message)
            return transaction_id

        self._reject_bootstrap_candidate_receipt(target)
        if journal.get("target_version") == target:
            raise ManagedInstallError(error_message)

        transaction_id = journal.get("transaction_id")
        old_target_raw = journal.get("target_version")
        activation_outcome_unknown = journal.get("activation_outcome_unknown", False)
        if (
            journal.get("format") != UPDATE_JOURNAL_FORMAT
            or type(journal.get("schema_version")) is not int
            or journal.get("schema_version") != UPDATE_JOURNAL_VERSION
            or journal.get("operation") not in {"adopt", "update"}
            or journal.get("phase") != "completed"
            or type(activation_outcome_unknown) is not bool
            or activation_outcome_unknown
            or not isinstance(transaction_id, str)
            or not _TRANSACTION_ID_PATTERN.fullmatch(transaction_id)
            or journal.get("program_root") != str(self.layout.root.resolve())
            or journal.get("data_root") != str(self.paths.data_root.resolve())
            or journal.get("application_profile") != self.application_profile.value
            or "previous_version" not in journal
            or not isinstance(old_target_raw, str)
        ):
            raise ManagedInstallError(error_message)
        try:
            old_target = _safe_version(old_target_raw)
        except ManagedInstallError as exc:
            raise ManagedInstallError(error_message) from exc
        if old_target != old_target_raw or old_target == target:
            raise ManagedInstallError(error_message)

        previous_raw = journal.get("previous_version")
        if previous_raw is not None and not isinstance(previous_raw, str):
            raise ManagedInstallError(error_message)
        try:
            previous = _safe_version(previous_raw) if previous_raw is not None else None
        except ManagedInstallError as exc:
            raise ManagedInstallError(error_message) from exc
        if previous_raw != previous:
            raise ManagedInstallError(error_message)
        try:
            current = self.layout.current_version()
        except ManagedInstallError as exc:
            raise ManagedInstallError(error_message) from exc
        if current != old_target or Version(target) <= Version(old_target):
            raise ManagedInstallError(error_message)
        try:
            committed = self._activation_commit_state(
                old_target,
                previous,
                transaction_id,
            )
        except ManagedInstallError as exc:
            raise ManagedInstallError(error_message) from exc
        if not committed:
            raise ManagedInstallError(error_message)
        return None

    @staticmethod
    def _validate_adoption_backup(value: object) -> None:
        if value is None:
            return
        if not isinstance(value, dict):
            raise ManagedInstallError("Retained adoption backup record is invalid.")
        backup_path = value.get("path")
        backup_sha256 = value.get("sha256")
        backup_id = value.get("backup_id")
        if (
            not isinstance(backup_path, str)
            or not backup_path
            or not isinstance(backup_sha256, str)
            or not _LOWER_SHA256_PATTERN.fullmatch(backup_sha256)
            or not isinstance(backup_id, str)
            or not backup_id
        ):
            raise ManagedInstallError("Retained adoption backup record is invalid.")

    def _validate_retained_adoption_receipt(
        self,
        receipt: Mapping[str, Any],
        journal: Mapping[str, Any],
        *,
        index: ReleaseIndex,
        release: ManagedRelease,
        artifact: ReleaseArtifact,
        target: str,
        previous: str | None,
        signer_key_id: str | None,
        index_sha256: str,
        recovery_plan: RecoveryPlan,
    ) -> None:
        _validate_reusable_receipt(receipt, index, release, artifact)
        expected_fields = {
            "format": RELEASE_RECEIPT_FORMAT,
            "schema_version": RELEASE_RECEIPT_VERSION,
            "version": target,
            "transaction_id": journal.get("transaction_id"),
            "channel": journal.get("channel"),
            "signer_key_id": signer_key_id,
            "application_profile": self.application_profile.value,
            "index_sha256": index_sha256,
            "release_index_schema_version": index.schema_version,
            "release_contract_sha256": journal.get("release_contract_sha256"),
            "program_root": str(self.layout.root.resolve()),
            "data_root": str(self.paths.data_root.resolve()),
            "previous_version": previous,
            "database_checkpoint": None,
            "recovery_plan": _recovery_plan_payload(recovery_plan),
            "adopted": True,
        }
        if (
            not expected_fields.keys() <= receipt.keys()
            or "backup" not in receipt
            or receipt.get("adopted") is not True
            or any(receipt.get(key) != value for key, value in expected_fields.items())
            or receipt.get("bootstrap_claim_id") != journal.get("bootstrap_claim_id")
            or receipt.get("bootstrap_index_sha256")
            != journal.get("bootstrap_index_sha256")
        ):
            raise ManagedInstallError(
                "Retained adoption receipt conflicts with its owned transaction."
            )
        journal_backup = journal.get("backup")
        self._validate_adoption_backup(journal_backup)
        self._validate_adoption_backup(receipt.get("backup"))
        if receipt.get("backup") != journal_backup:
            raise ManagedInstallError(
                "Retained adoption receipt conflicts with its owned transaction."
            )

    def _prepare_retained_adoption_retry(
        self,
        index: ReleaseIndex,
        *,
        artifact_path: str | os.PathLike[str],
        version_dir: str | os.PathLike[str],
        channel: str,
        requested_version: str,
        signer_key_id: str | None,
        index_sha256: str,
        retry_transaction_id: str,
    ) -> _RetainedAdoptionRetry:
        release = index.select(requested_version=requested_version)
        if release is None:
            raise ManagedInstallError("No release is available to adopt.")
        target = _safe_version(release.version)
        artifact = release.artifact_for(self.platform_tag)

        versions_dir = self.layout.versions_dir
        target_path = versions_dir / target
        supplied_version_dir = Path(version_dir).expanduser()
        if (
            versions_dir.is_symlink()
            or getattr(versions_dir, "is_junction", lambda: False)()
            or target_path.is_symlink()
            or getattr(target_path, "is_junction", lambda: False)()
            or supplied_version_dir.is_symlink()
            or getattr(supplied_version_dir, "is_junction", lambda: False)()
        ):
            raise ManagedInstallError(
                "Adopted runtime directory must not be a symbolic link."
            )
        try:
            expected_version_dir = target_path.resolve()
            actual_version_dir = supplied_version_dir.resolve()
        except OSError as exc:
            raise ManagedInstallError(
                "Adopted runtime directory is unreadable."
            ) from exc
        if (
            actual_version_dir != expected_version_dir
            or not expected_version_dir.is_dir()
        ):
            raise ManagedInstallError(
                "Adopted runtime must already be at the selected managed version path."
            )

        try:
            source_artifact = Path(artifact_path).expanduser().resolve()
            if (
                not source_artifact.is_file()
                or source_artifact.stat().st_size != artifact.size
                or sha256_file(source_artifact) != artifact.sha256
            ):
                raise ManagedInstallError(
                    "Adopted release artifact failed signed hash checks."
                )
        except OSError as exc:
            raise ManagedInstallError(
                "Adopted release artifact could not be verified."
            ) from exc

        journal = _read_bounded_regular_json_object(self.layout.journal_file)
        claim_id = self._validate_native_bootstrap_generation(
            expected_version_dir,
            index,
            release,
            artifact,
            index_sha256,
            required_claim_id=journal.get("bootstrap_claim_id"),
        )
        required_fields = {
            "format",
            "schema_version",
            "operation",
            "transaction_id",
            "application_profile",
            "program_root",
            "data_root",
            "channel",
            "signer_key_id",
            "index_sha256",
            "target_version",
            "release_index_schema_version",
            "release_contract_sha256",
            "previous_version",
            "artifact",
            "recovery_plan",
            "phase",
        }
        if (
            not required_fields <= journal.keys()
            or not isinstance(journal.get("phase"), str)
            or not journal.get("phase")
            or journal.get("format") != UPDATE_JOURNAL_FORMAT
            or type(journal.get("schema_version")) is not int
            or journal.get("schema_version") != UPDATE_JOURNAL_VERSION
            or journal.get("operation") != "adopt"
            or journal.get("transaction_id") != retry_transaction_id
            or journal.get("application_profile") != self.application_profile.value
            or journal.get("program_root") != str(self.layout.root.resolve())
            or journal.get("data_root") != str(self.paths.data_root.resolve())
            or journal.get("channel") != channel
            or journal.get("signer_key_id") != signer_key_id
            or journal.get("index_sha256") != index_sha256
            or journal.get("target_version") != target
            or journal.get("bootstrap_claim_id") != claim_id
            or (
                claim_id is not None
                and journal.get("bootstrap_index_sha256") != index_sha256
            )
            or type(journal.get("release_index_schema_version")) is not int
            or journal.get("release_index_schema_version") != index.schema_version
            or journal.get("release_contract_sha256")
            != release_contract_sha256(index, release)
        ):
            raise ManagedInstallError(
                "Retained adoption journal does not match the frozen authenticated inputs."
            )
        self._validate_adoption_backup(journal.get("backup"))
        if journal.get("database_checkpoint") is not None:
            raise ManagedInstallError(
                "Retained adoption journal has unexpected recovery material."
            )

        previous_raw = journal.get("previous_version")
        if previous_raw is not None and not isinstance(previous_raw, str):
            raise ManagedInstallError(
                "Retained adoption journal has an invalid prior version."
            )
        try:
            previous = _safe_version(previous_raw) if previous_raw is not None else None
        except ManagedInstallError as exc:
            raise ManagedInstallError(
                "Retained adoption journal has an invalid prior version."
            ) from exc
        if previous_raw != previous:
            raise ManagedInstallError(
                "Retained adoption journal has a noncanonical prior version."
            )
        journal_artifact = journal.get("artifact")
        expected_artifact = asdict(artifact)
        if (
            not isinstance(journal_artifact, dict)
            or any(
                journal_artifact.get(key) != value
                for key, value in expected_artifact.items()
            )
            or journal_artifact.get("adopted_version_dir") != str(expected_version_dir)
        ):
            raise ManagedInstallError(
                "Retained adoption journal artifact does not match the signed selection."
            )

        current = self.layout.current_version()
        if previous is not None and Version(target) <= Version(previous):
            raise ManagedInstallError(
                "Retained adoption journal does not advance its prior version."
            )
        if current not in {previous, target}:
            raise ManagedInstallError(
                "Retained adoption does not match the active managed release."
            )

        recovery_plan = self._recovery_plan(
            previous,
            release=release,
            signer_key_id=signer_key_id,
            requested_coverage=RecoveryCoverage.AUTOMATIC,
            decision_source=RecoveryDecisionSource.AUTOMATIC,
            actor=None,
            reason=None,
            policy_id=None,
        )
        if journal.get("recovery_plan") != _recovery_plan_payload(recovery_plan):
            raise ManagedInstallError(
                "Retained adoption journal recovery plan has changed."
            )

        receipt = self._read_retained_adoption_receipt(target)
        if current == target:
            if receipt is None:
                raise ManagedInstallError(
                    "Active retained adoption has no matching release receipt."
                )
            self._validate_retained_adoption_receipt(
                receipt,
                journal,
                index=index,
                release=release,
                artifact=artifact,
                target=target,
                previous=previous,
                signer_key_id=signer_key_id,
                index_sha256=index_sha256,
                recovery_plan=recovery_plan,
            )
            self._reject_pending_adoption_fences()
            return _RetainedAdoptionRetry(
                journal=journal,
                release=release,
                artifact=artifact,
                target=target,
                previous=previous,
                recovery_plan=recovery_plan,
                source_artifact=source_artifact,
                version_dir=expected_version_dir,
                receipt=receipt,
                committed=True,
            )

        phase = journal.get("phase")
        allowed_phases = {
            "preflight_complete",
            "backup_complete",
            "runtime_probe_passed",
            "health_gate_passed",
            "isolated_probe_passed",
            "activation_pending",
            "failed",
        }
        unknown_raw = journal.get("activation_outcome_unknown", False)
        if type(unknown_raw) is not bool or unknown_raw:
            raise ManagedInstallError(
                "Retained adoption has an unresolved activation outcome."
            )
        if not isinstance(phase, str) or phase not in allowed_phases:
            raise ManagedInstallError(
                "Retained adoption stopped at a phase that cannot be retried safely."
            )
        if receipt is not None:
            self._validate_retained_adoption_receipt(
                receipt,
                journal,
                index=index,
                release=release,
                artifact=artifact,
                target=target,
                previous=previous,
                signer_key_id=signer_key_id,
                index_sha256=index_sha256,
                recovery_plan=recovery_plan,
            )
        self._reject_pending_adoption_fences()
        return _RetainedAdoptionRetry(
            journal=journal,
            release=release,
            artifact=artifact,
            target=target,
            previous=previous,
            recovery_plan=recovery_plan,
            source_artifact=source_artifact,
            version_dir=expected_version_dir,
            receipt=receipt,
            committed=False,
        )

    def _reject_pending_adoption_fences(self) -> None:
        if self.layout.read_recovery_requirement() is not None:
            raise ManagedInstallError(
                "An unresolved recovery requirement blocks retained adoption."
            )
        if self.layout.read_pending_rollback() is not None:
            raise ManagedInstallError(
                "An unresolved rollback blocks retained adoption."
            )
        try:
            pending_switches = interrupted_database_switches(
                self.paths.state_dir / "database-checkpoint-recovery"
            )
        except (DatabaseCheckpointError, OSError) as exc:
            raise ManagedInstallError(
                "Database checkpoint recovery state blocks retained adoption."
            ) from exc
        if pending_switches:
            # Adoption uses full recovery or no-data execution. A checkpoint
            # switch belongs to another transaction and cannot be reconciled here.
            raise ManagedInstallError(
                "An interrupted database checkpoint switch blocks retained adoption."
            )

    def _activation_commit_state(
        self,
        target: str,
        previous: str | None,
        transaction_id: str,
    ) -> bool:
        """Return whether activation committed using only pointer and receipt."""

        try:
            current = self.layout.current_version()
        except BaseException as exc:
            raise ManagedInstallError(
                "Managed activation state cannot be read safely."
            ) from exc
        if current == previous:
            return False
        if current != target:
            raise ManagedInstallError(
                "Managed activation state conflicts with the transaction."
            )
        target_dir = self.layout.versions_dir / target
        if (
            self.layout.versions_dir.is_symlink()
            or getattr(self.layout.versions_dir, "is_junction", lambda: False)()
            or target_dir.is_symlink()
            or getattr(target_dir, "is_junction", lambda: False)()
        ):
            raise ManagedInstallError("Managed activation target is unsafe.")
        receipt_path = target_dir / "release-receipt.json"
        try:
            receipt_info = receipt_path.lstat()
            if not stat.S_ISREG(receipt_info.st_mode) or receipt_path.is_symlink():
                raise ManagedInstallError("Managed activation receipt is unsafe.")
            receipt = self.layout.read_receipt(target)
        except BaseException as exc:
            if isinstance(exc, ManagedInstallError):
                raise
            raise ManagedInstallError(
                "Managed activation receipt cannot be read safely."
            ) from exc
        if (
            receipt.get("transaction_id") != transaction_id
            or receipt.get("version") != target
            or receipt.get("application_profile") != self.application_profile.value
        ):
            raise ManagedInstallError(
                "Managed activation receipt conflicts with the transaction."
            )
        return True

    def _create_database_checkpoint(
        self,
        *,
        transaction_id: str,
        previous_version: str | None,
        target_version: str,
        key_store: InstallationKeyStore,
    ) -> DatabaseCheckpointRecord:
        return create_database_checkpoint(
            self.paths.database_file,
            self.layout.database_checkpoints_dir,
            transaction_id=transaction_id,
            source_version=previous_version,
            target_version=target_version,
            available_key_ids=key_store.key_ids(),
        )

    def _restore_database_checkpoint(
        self,
        checkpoint: Mapping[str, Any],
        *,
        transaction_id: str,
    ) -> DatabaseCheckpointRestoreResult:
        source = checkpoint.get("path")
        expected_manifest_sha256 = checkpoint.get("manifest_sha256")
        if not isinstance(source, str) or not isinstance(expected_manifest_sha256, str):
            raise ManagedInstallError(
                "Database checkpoint receipt is missing its bound path or hash."
            )
        try:
            return restore_database_checkpoint(
                source,
                self.paths.database_file,
                self.paths.state_dir / "database-checkpoint-recovery",
                expected_manifest_sha256=expected_manifest_sha256,
                available_key_ids=InstallationKeyStore(
                    self.paths.master_keys_file
                ).key_ids(),
                transaction_id=transaction_id,
            )
        except DatabaseCheckpointError as exc:
            raise ManagedInstallError(str(exc)) from exc

    def _recover_interrupted_database_switches(self) -> tuple[Path, ...]:
        try:
            return recover_interrupted_database_switches(
                self.paths.state_dir / "database-checkpoint-recovery",
                self.paths.database_file,
            )
        except DatabaseCheckpointError as exc:
            raise ManagedInstallError(
                f"An interrupted database checkpoint switch requires recovery: {exc}"
            ) from exc

    @staticmethod
    def _rollback_restore_witness(
        transaction_id: str,
    ) -> RestoreActivationWitness:
        if not _VERSION_NAME_PATTERN.fullmatch(transaction_id):
            raise ManagedInstallError("Rollback transaction ID is unsafe.")
        payload = (
            json.dumps(
                {
                    "format": ROLLBACK_TRANSACTION_FORMAT,
                    "schema_version": ROLLBACK_TRANSACTION_VERSION,
                    "transaction_id": transaction_id,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )
        return RestoreActivationWitness(
            relative_path=f"state/managed-rollback-{transaction_id}.json",
            payload=payload,
        )

    def _write_rollback_transaction(
        self,
        transaction: dict[str, Any],
        *,
        phase: str,
    ) -> None:
        transaction["phase"] = phase
        transaction["updated_at"] = _now()
        _durable_atomic_json(self.layout.rollback_transaction_file, transaction)

    def _reactivate_full_restore(
        self,
        *,
        safety_root: Path,
        transaction_id: str,
        witness: RestoreActivationWitness,
    ) -> Path:
        """Restore the pre-rollback root from every witnessed rename layout."""

        destination = self.paths.data_root.resolve()
        safety = safety_root.resolve()
        if safety.parent != destination.parent:
            raise ManagedInstallError(
                "Rollback safety root is not a sibling of the active data root."
            )
        failed = destination.parent / (
            f".{destination.name}.failed-rollback-{transaction_id}"
        )

        def has_witness(root: Path) -> bool:
            if root.is_symlink():
                raise ManagedInstallError(
                    f"Rollback evidence root is a symbolic link: {root}"
                )
            if not root.exists():
                return False
            if not root.is_dir():
                raise ManagedInstallError(
                    f"Rollback evidence root is not a directory: {root}"
                )
            target = root.joinpath(*Path(witness.relative_path).parts)
            if target.is_symlink() or not target.is_file():
                return False
            try:
                return target.read_bytes() == witness.payload
            except OSError as exc:
                raise ManagedInstallError(
                    f"Rollback activation witness is unreadable: {target}"
                ) from exc

        if safety.exists() or safety.is_symlink():
            if safety.is_symlink() or not safety.is_dir():
                raise ManagedInstallError(f"Rollback safety root is unsafe: {safety}")
            if destination.exists() or destination.is_symlink():
                if destination.is_symlink() or not destination.is_dir():
                    raise ManagedInstallError(
                        f"Active restored data root is unsafe: {destination}"
                    )
                if failed.exists() or failed.is_symlink():
                    raise ManagedInstallError(
                        "Rollback data reactivation has an ambiguous root layout."
                    )
                if not has_witness(destination):
                    raise ManagedInstallError(
                        "Active restored data root lacks the rollback witness."
                    )
                preserve_local_backup_catalog(destination, safety, validate_only=True)
                os.replace(destination, failed)
                _fsync_directory(destination.parent)
            elif not has_witness(failed):
                raise ManagedInstallError(
                    "Rollback data reactivation lost its witnessed restored root."
                )
            preserve_local_backup_catalog(failed, safety)
            os.replace(safety, destination)
            _fsync_directory(destination.parent)
        elif not (
            destination.is_dir()
            and not destination.is_symlink()
            and has_witness(failed)
        ):
            raise ManagedInstallError(
                "Rollback data reactivation has no provable completed state."
            )
        else:
            preserve_local_backup_catalog(failed, destination)
        return failed

    def _recover_interrupted_rollback(self) -> dict[str, Any] | None:
        """Reconcile a data switch that did not reach the program commit point."""

        transaction = self.layout.read_pending_rollback()
        if transaction is None:
            return None
        transaction_id = str(transaction["transaction_id"])
        from_version = _safe_version(str(transaction["from_version"]))
        to_version = _safe_version(str(transaction["to_version"]))
        current = self.layout.current_version()
        if current == to_version:
            self._write_rollback_transaction(transaction, phase="completed")
            return transaction
        if current != from_version:
            raise ManagedInstallError(
                "Interrupted rollback does not match the active managed release."
            )

        coverage = str(transaction["coverage"])
        if coverage == "database_only":
            safety = (
                self.paths.state_dir
                / "database-checkpoint-recovery"
                / f"checkpoint-restore-{transaction_id}"
            )
            if safety.exists() or safety.is_symlink():
                try:
                    try:
                        phase = database_switch_phase(safety)
                    except DatabaseCheckpointError:
                        discard_unwitnessed_database_switch(
                            safety,
                            self.paths.database_file,
                            transaction_id=transaction_id,
                        )
                        phase = "unwitnessed_discarded"
                    if phase == "completed":
                        reactivate_completed_database_switch(
                            safety,
                            self.paths.database_file,
                            transaction_id=transaction_id,
                        )
                    elif phase not in {
                        "rolled_back",
                        "interrupted_rolled_back",
                        "reactivation_completed",
                        "unwitnessed_discarded",
                    }:
                        raise DatabaseCheckpointError(
                            f"database switch is still non-terminal ({phase})"
                        )
                except DatabaseCheckpointError as exc:
                    raise ManagedInstallError(
                        f"Interrupted rollback database recovery failed: {exc}"
                    ) from exc
        else:
            backup = transaction.get("backup")
            if not isinstance(backup, dict):
                raise ManagedInstallError(
                    "Interrupted full rollback has no bound backup record."
                )
            backup_id = backup.get("backup_id")
            backup_path = backup.get("path")
            backup_sha256 = backup.get("sha256")
            if not all(
                isinstance(value, str) and value
                for value in (
                    backup_id,
                    backup_path,
                    backup_sha256,
                )
            ):
                raise ManagedInstallError(
                    "Interrupted full rollback backup record is invalid."
                )
            witness = self._rollback_restore_witness(transaction_id)
            destination = self.paths.data_root.resolve()
            staging = restore_staging_root(destination, backup_id)
            safety = restore_safety_root(destination, backup_id)
            failed = destination.parent / (
                f".{destination.name}.failed-rollback-{transaction_id}"
            )
            witness_target = destination.joinpath(*Path(witness.relative_path).parts)
            physical_switch_started = any(
                path.exists() or path.is_symlink()
                for path in (staging, safety, failed, witness_target)
            )
            if physical_switch_started:
                if not failed.exists() and (
                    staging.exists() or safety.exists() or witness_target.exists()
                ):
                    restored = self._restore_paired_backup(
                        backup_path,
                        expected_sha256=backup_sha256,
                        transaction_id=transaction_id,
                        activation_witness=witness,
                    )
                    safety = restored.safety_root or safety
                self._reactivate_full_restore(
                    safety_root=safety,
                    transaction_id=transaction_id,
                    witness=witness,
                )

        transaction["recovered_at"] = _now()
        self._write_rollback_transaction(transaction, phase="aborted")
        return transaction

    def _recover_interrupted_update(self) -> dict[str, Any] | None:
        """Restore pre-update data when target execution died before commit."""

        journal = self.layout.read_pending_update()
        if journal is None:
            return None
        transaction_id = str(journal["transaction_id"])
        previous_raw = journal.get("previous_version")
        target_raw = journal.get("target_version")
        if previous_raw is not None and not isinstance(previous_raw, str):
            raise ManagedInstallError(
                "Interrupted managed update has an invalid prior version."
            )
        if not isinstance(target_raw, str):
            raise ManagedInstallError(
                "Interrupted managed update lacks its version boundary."
            )
        previous = _safe_version(previous_raw) if previous_raw is not None else None
        _safe_version(target_raw)
        if self.layout.current_version() != previous:
            raise ManagedInstallError(
                "Interrupted update does not match the active managed release."
            )

        checkpoint = journal.get("database_checkpoint")
        backup = journal.get("backup")
        if isinstance(checkpoint, dict) and isinstance(backup, dict):
            raise ManagedInstallError(
                "Interrupted update has ambiguous recovery material."
            )
        attempt_raw = journal.get("recovery_attempt_transaction_id")
        attempt = (
            str(attempt_raw)
            if isinstance(attempt_raw, str)
            and _VERSION_NAME_PATTERN.fullmatch(attempt_raw)
            else None
        )
        restored_coverage: RecoveryCoverage
        safety_path: Path | None
        if isinstance(checkpoint, dict):
            restored_coverage = RecoveryCoverage.DATABASE_ONLY
            candidates = [value for value in (attempt, transaction_id) if value]
            restored = False
            safety_path = None
            for candidate in candidates:
                candidate_safety = (
                    self.paths.state_dir
                    / "database-checkpoint-recovery"
                    / f"checkpoint-restore-{candidate}"
                )
                if not (candidate_safety.exists() or candidate_safety.is_symlink()):
                    continue
                try:
                    phase = database_switch_phase(candidate_safety)
                except DatabaseCheckpointError as exc:
                    try:
                        discard_unwitnessed_database_switch(
                            candidate_safety,
                            self.paths.database_file,
                            transaction_id=candidate,
                        )
                    except DatabaseCheckpointError as cleanup_exc:
                        raise ManagedInstallError(
                            "Interrupted update checkpoint state is invalid: "
                            f"{cleanup_exc}"
                        ) from exc
                    continue
                if phase == "completed":
                    restored = True
                    safety_path = candidate_safety
                    break
                if phase not in {
                    "rolled_back",
                    "interrupted_rolled_back",
                    "reactivation_completed",
                }:
                    raise ManagedInstallError(
                        "Interrupted update checkpoint recovery is non-terminal."
                    )
            if not restored:
                attempt = f"recover-{uuid.uuid4().hex}"
                journal["recovery_attempt_transaction_id"] = attempt
                self._write_journal(journal, phase="recovery_restore_pending")
                restored_checkpoint = self._restore_database_checkpoint(
                    checkpoint,
                    transaction_id=attempt,
                )
                safety_path = restored_checkpoint.safety_dir
        elif isinstance(backup, dict):
            restored_coverage = RecoveryCoverage.FULL
            if attempt is None:
                attempt = f"recover-{uuid.uuid4().hex}"
                journal["recovery_attempt_transaction_id"] = attempt
                self._write_journal(journal, phase="recovery_restore_pending")
            restored_backup = self._restore_paired_backup(
                str(backup.get("path") or ""),
                expected_sha256=str(backup.get("sha256") or ""),
                transaction_id=attempt,
                activation_witness=self._update_restore_witness(
                    original_transaction_id=transaction_id,
                    recovery_transaction_id=attempt,
                ),
            )
            safety_path = restored_backup.safety_root
        else:
            raise ManagedInstallError(
                "Interrupted update executed target code without recoverable material."
            )

        journal["automatic_data_restore"] = {
            "completed": True,
            "coverage": restored_coverage,
            "safety_path": str(safety_path) if safety_path else None,
            "recovered_after_process_death": True,
        }
        journal["error"] = (
            "Managed target execution ended before the activation commit; "
            "pre-update data was restored during stopped maintenance."
        )
        self._write_journal(journal, phase="failed")
        return journal

    def _restore_failed_update_data(
        self,
        journal: dict[str, Any],
        *,
        backup: Mapping[str, Any] | None,
        checkpoint: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        """Start automatic recovery with a durable, retryable attempt identity."""

        if (backup is None) == (checkpoint is None):
            raise ManagedInstallError(
                "Failed update recovery material is missing or ambiguous."
            )
        transaction_id = str(journal["transaction_id"])
        attempt = f"recover-{uuid.uuid4().hex}"
        journal["recovery_attempt_transaction_id"] = attempt
        self._write_journal(journal, phase="recovery_restore_pending")
        if backup is not None:
            restored = self._restore_paired_backup(
                str(backup.get("path") or ""),
                expected_sha256=str(backup.get("sha256") or ""),
                transaction_id=attempt,
                activation_witness=self._update_restore_witness(
                    original_transaction_id=transaction_id,
                    recovery_transaction_id=attempt,
                ),
            )
            safety_path = restored.safety_root
            restored_coverage = RecoveryCoverage.FULL
        else:
            if checkpoint is None:
                raise AssertionError("Checkpoint recovery lost its record.")
            restored_checkpoint = self._restore_database_checkpoint(
                checkpoint,
                transaction_id=attempt,
            )
            safety_path = restored_checkpoint.safety_dir
            restored_coverage = RecoveryCoverage.DATABASE_ONLY
        evidence: dict[str, Any] = {
            "completed": True,
            "coverage": restored_coverage,
            "safety_path": str(safety_path) if safety_path else None,
        }
        if restored_coverage is RecoveryCoverage.FULL:
            evidence["safety_root"] = evidence["safety_path"]
        else:
            evidence["safety_database"] = evidence["safety_path"]
        return evidence

    @staticmethod
    def _update_restore_witness(
        *,
        original_transaction_id: str,
        recovery_transaction_id: str,
    ) -> RestoreActivationWitness:
        for value in (original_transaction_id, recovery_transaction_id):
            if not _VERSION_NAME_PATTERN.fullmatch(value):
                raise ManagedInstallError("Update recovery transaction ID is unsafe.")
        payload = (
            json.dumps(
                {
                    "format": UPDATE_JOURNAL_FORMAT,
                    "schema_version": UPDATE_JOURNAL_VERSION,
                    "transaction_id": original_transaction_id,
                    "recovery_transaction_id": recovery_transaction_id,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )
        return RestoreActivationWitness(
            relative_path=(
                f"state/managed-update-recovery-{original_transaction_id}.json"
            ),
            payload=payload,
        )

    def _run_installation_operation[T](
        self,
        operation: InstallationOperation,
        lifecycle: InstallationLifecycle | None,
        action: Callable[[], T],
    ) -> T:
        if lifecycle is None:
            return action()
        if (
            lifecycle.operation is not operation
            or lifecycle.application_profile is not self.application_profile
            or lifecycle.terminal
        ):
            raise ManagedInstallError(
                "Installation event context does not match this operation."
            )
        try:
            lifecycle.checkpoint(InstallationPhase.PREFLIGHT)
            result = action()
        except InstallationCancelled:
            # The action's cleanup and lock release finish before a terminal
            # cancellation record is observable by the caller.
            lifecycle.cancel()
            raise
        except BaseException as exc:
            try:
                recovery_required = (
                    self.layout.read_recovery_requirement() is not None
                    or self.layout.read_pending_update() is not None
                    or self.layout.read_pending_rollback() is not None
                    or bool(
                        interrupted_database_switches(
                            self.paths.state_dir / "database-checkpoint-recovery"
                        )
                    )
                )
            except ManagedInstallError, DatabaseCheckpointError, OSError:
                recovery_required = True
            if recovery_required:
                code = InstallationErrorCode.RECOVERY_REQUIRED
            elif isinstance(exc, ReleaseManifestError):
                code = InstallationErrorCode.VERIFICATION_FAILED
            elif isinstance(exc, ManagedCompatibilityError):
                code = InstallationErrorCode.INCOMPATIBLE_RELEASE
            elif isinstance(exc, ReleaseDownloadError):
                code = InstallationErrorCode.DOWNLOAD_FAILED
            elif isinstance(exc, PublisherBootstrapFailure):
                code = exc.error_code
            else:
                code = InstallationErrorCode.OPERATION_FAILED
            lifecycle.fail(
                code,
                retryable=(code is InstallationErrorCode.DOWNLOAD_FAILED),
                cancellation_safe=not recovery_required,
            )
            raise
        lifecycle.succeed()
        return result

    def update(
        self,
        index_source: str | os.PathLike[str],
        *,
        channel: str = "stable",
        requested_version: str | None = None,
        allow_unsigned_local: bool = False,
        recovery_coverage: RecoveryCoverage | str = RecoveryCoverage.AUTOMATIC,
        recovery_decision_source: RecoveryDecisionSource
        | str = RecoveryDecisionSource.AUTOMATIC,
        recovery_actor: str | None = None,
        recovery_reason: str | None = None,
        recovery_policy_id: str | None = None,
        acknowledge_recovery_required: str | None = None,
        lifecycle: InstallationLifecycle | None = None,
    ) -> UpdateResult:
        return self._run_installation_operation(
            InstallationOperation.UPDATE,
            lifecycle,
            lambda: self._update(
                index_source,
                channel=channel,
                requested_version=requested_version,
                allow_unsigned_local=allow_unsigned_local,
                recovery_coverage=recovery_coverage,
                recovery_decision_source=recovery_decision_source,
                recovery_actor=recovery_actor,
                recovery_reason=recovery_reason,
                recovery_policy_id=recovery_policy_id,
                acknowledge_recovery_required=acknowledge_recovery_required,
                lifecycle=lifecycle,
            ),
        )

    def _update(
        self,
        index_source: str | os.PathLike[str],
        *,
        channel: str = "stable",
        requested_version: str | None = None,
        allow_unsigned_local: bool = False,
        recovery_coverage: RecoveryCoverage | str = RecoveryCoverage.AUTOMATIC,
        recovery_decision_source: RecoveryDecisionSource
        | str = RecoveryDecisionSource.AUTOMATIC,
        recovery_actor: str | None = None,
        recovery_reason: str | None = None,
        recovery_policy_id: str | None = None,
        acknowledge_recovery_required: str | None = None,
        lifecycle: InstallationLifecycle | None = None,
    ) -> UpdateResult:
        target_lifecycle_options: _TargetLifecycleOptions = (
            {"lifecycle": lifecycle} if lifecycle is not None else {}
        )
        index, signer, index_sha = self._verified_index(
            index_source,
            channel=channel,
            allow_unsigned_local=allow_unsigned_local,
        )
        if lifecycle is not None:
            lifecycle.checkpoint(InstallationPhase.VERIFICATION)
        lock = self._acquire_lock()
        runtime_lock: RuntimeLeaderLock | None = None
        transaction_id = uuid.uuid4().hex
        journal: dict[str, Any] = {
            "format": UPDATE_JOURNAL_FORMAT,
            "schema_version": UPDATE_JOURNAL_VERSION,
            "transaction_id": transaction_id,
            "operation": "update",
            **(
                {"operation_id": lifecycle.operation_id}
                if lifecycle is not None
                else {}
            ),
            "started_at": _now(),
            "index_source": str(index_source),
            "index_sha256": index_sha,
            "channel": channel,
            "signer_key_id": signer,
            "application_profile": self.application_profile.value,
            "program_root": str(self.layout.root.resolve()),
            "data_root": str(self.paths.data_root.resolve()),
        }
        backup: BackupRecord | None = None
        database_checkpoint: DatabaseCheckpointRecord | None = None
        migration_started = False
        target_execution_started = False
        target: str | None = None
        previous: str | None = None
        reused_receipt: dict[str, Any] | None = None
        native_claim_id: str | None = None
        native_index_sha256: str | None = None
        activation_started = False
        journal_owned = False
        pending_recovery: dict[str, Any] | None = None
        current_recovery_fence: dict[str, Any] | None = None
        recovery_plan: RecoveryPlan | None = None
        probe_attestation: ManagedProbeAttestation | None = None
        try:
            runtime_lock = _acquire_runtime_maintenance_lock(self.paths)
            _assert_runtime_stopped(self.paths)
            self.layout.ensure()
            if lifecycle is not None:
                lifecycle.advance(InstallationPhase.RECOVERY, cancellation_safe=False)
            self._validate_native_journal_roots()
            recovered_switches = self._recover_interrupted_database_switches()
            recovered_rollback = self._recover_interrupted_rollback()
            recovered_update = self._recover_interrupted_update()
            if lifecycle is not None:
                lifecycle.checkpoint(InstallationPhase.PREFLIGHT)
            pending_recovery = self.layout.read_recovery_requirement()
            if pending_recovery is not None:
                pending_transaction = str(pending_recovery["transaction_id"])
                journal["recovery_required"] = pending_recovery
                if acknowledge_recovery_required != pending_transaction:
                    raise ManagedInstallError(
                        "A prior no-recovery update may have changed live data. "
                        "Do not restart the active release blindly. Review "
                        f"{self.layout.recovery_required_file} and re-run the "
                        "update with --acknowledge-recovery-required "
                        f"{pending_transaction} only when the next target can "
                        "safely continue from that state."
                    )
                journal["acknowledged_recovery_required"] = pending_transaction
            elif acknowledge_recovery_required is not None:
                raise ManagedInstallError(
                    "--acknowledge-recovery-required was supplied, but no "
                    "recovery-required state exists."
                )
            self._archive_existing_journal()
            journal_owned = True
            if recovered_switches:
                journal["recovered_database_switches"] = [
                    str(path) for path in recovered_switches
                ]
            if recovered_rollback is not None:
                journal["recovered_rollback_transaction"] = str(
                    recovered_rollback["transaction_id"]
                )
            if recovered_update is not None:
                journal["recovered_update_transaction"] = str(
                    recovered_update["transaction_id"]
                )
            previous = self.layout.current_version()
            release = index.select(
                current_version=previous,
                requested_version=requested_version,
            )
            if release is None:
                raise ManagedInstallError("No newer release is available.")
            target = _safe_version(release.version)
            if previous is not None and Version(target) <= Version(previous):
                raise ManagedInstallError(
                    f"Managed updates must advance beyond active version {previous}."
                )
            artifact = release.artifact_for(self.platform_tag)
            recovery_plan = self._recovery_plan(
                previous,
                release=release,
                signer_key_id=signer,
                requested_coverage=recovery_coverage,
                decision_source=recovery_decision_source,
                actor=recovery_actor,
                reason=recovery_reason,
                policy_id=recovery_policy_id,
            )
            execution_authenticated = signer is not None
            no_data_execution = bool(
                execution_authenticated
                and release.execution is not None
                and release.execution.data_impact == "none"
            )
            probe_capability = self._probe_capability(
                release,
                signer_key_id=signer,
            )
            if self.layout.version_dir(target).exists():
                try:
                    candidate_receipt = self.layout.read_receipt(target)
                except ManagedInstallError:
                    quarantined = self._quarantine_incomplete_release(
                        target,
                        transaction_id=transaction_id,
                    )
                    journal["prior_incomplete_release"] = str(quarantined)
                else:
                    _validate_reusable_receipt(
                        candidate_receipt,
                        index,
                        release,
                        artifact,
                    )
                    native_claim_id = self._validate_native_bootstrap_generation(
                        self.layout.versions_dir / target,
                        index,
                        release,
                        artifact,
                        str(candidate_receipt.get("bootstrap_index_sha256") or ""),
                        required_claim_id=candidate_receipt.get("bootstrap_claim_id"),
                    )
                    if candidate_receipt.get("bootstrap_claim_id") != native_claim_id:
                        raise ManagedInstallError(
                            "Reusable release receipt lacks its native bootstrap claim."
                        )
                    if native_claim_id is not None:
                        native_index_sha256 = str(
                            candidate_receipt["bootstrap_index_sha256"]
                        )
                        journal["bootstrap_claim_id"] = native_claim_id
                        journal["bootstrap_index_sha256"] = native_index_sha256
                    reused_receipt = candidate_receipt
                    journal["reused_release_transaction_id"] = candidate_receipt.get(
                        "transaction_id"
                    )
            compatibility = preflight_release(
                self.paths,
                self.layout,
                release,
                artifact,
                artifact_already_staged=reused_receipt is not None,
                recovery_plan=recovery_plan,
                execution_authenticated=execution_authenticated,
                probe_capability=probe_capability,
            )
            if not compatibility.compatible:
                messages = "; ".join(
                    issue.message for issue in compatibility.issues if issue.blocking
                )
                raise ManagedCompatibilityError(messages)
            journal.update(
                previous_version=previous,
                target_version=target,
                release_index_schema_version=index.schema_version,
                release_contract_sha256=release_contract_sha256(index, release),
                compatibility=compatibility.to_dict(),
                recovery_plan=compatibility.recovery_plan.to_dict(),
                reused_installed_release=reused_receipt is not None,
            )
            self._write_journal(journal, phase="preflight_complete")

            downloaded = None
            if reused_receipt is None:
                if lifecycle is not None:
                    lifecycle.checkpoint(InstallationPhase.DOWNLOAD)
                artifact_source = resolve_artifact_source(
                    artifact.url,
                    index_source=index_source,
                )
                staged_artifact = self.layout.staging_dir / (
                    f"{transaction_id}-{artifact.filename}"
                )
                downloaded = download_release_artifact(
                    artifact_source,
                    staged_artifact,
                    expected_size=artifact.size,
                    expected_sha256=artifact.sha256,
                    **(
                        {
                            "progress": lifecycle.progress,
                            "cancellation_check": lifecycle.check_cancelled,
                        }
                        if lifecycle is not None
                        else {}
                    ),
                )
                if lifecycle is not None:
                    lifecycle.checkpoint(InstallationPhase.VERIFICATION)
                journal["artifact"] = {
                    **asdict(artifact),
                    "resolved_source": artifact_source,
                    "staged_path": str(downloaded.path),
                }
                self._write_journal(journal, phase="artifact_verified")
            else:
                journal["artifact"] = {**asdict(artifact), "reused": True}
                self._write_journal(journal, phase="runtime_reuse_selected")

            if self.paths.database_file.is_file():
                if lifecycle is not None:
                    lifecycle.advance(
                        InstallationPhase.RECOVERY, cancellation_safe=False
                    )
                key_store = InstallationKeyStore(self.paths.master_keys_file)
                if recovery_plan.selected_coverage is RecoveryCoverage.FULL:
                    backup = self._create_managed_backup(
                        previous,
                        key_store=key_store,
                    )
                    journal["backup"] = {
                        "path": str(backup.path),
                        "sha256": backup.sha256,
                        "backup_id": backup.backup_id,
                    }
                    self._write_journal(journal, phase="backup_complete")
                elif recovery_plan.selected_coverage is RecoveryCoverage.DATABASE_ONLY:
                    database_checkpoint = self._create_database_checkpoint(
                        transaction_id=transaction_id,
                        previous_version=previous,
                        target_version=target,
                        key_store=key_store,
                    )
                    journal["database_checkpoint"] = database_checkpoint.to_dict()
                    self._write_journal(
                        journal,
                        phase="database_checkpoint_complete",
                    )
                else:
                    self._write_journal(
                        journal,
                        phase="recovery_selection_complete",
                    )
            else:
                self._write_journal(
                    journal,
                    phase="recovery_selection_complete",
                )

            if reused_receipt is None:
                if downloaded is None:
                    raise ManagedInstallError(
                        "Verified release artifact is unavailable."
                    )
                if lifecycle is not None:
                    lifecycle.checkpoint(InstallationPhase.UNPACKING)
                    lifecycle.advance(
                        InstallationPhase.UNPACKING, cancellation_safe=False
                    )
                version_dir = self.layout.version_dir(target)
                version_dir.mkdir(parents=False, exist_ok=False)
                runtime_artifact = version_dir / artifact.filename
                os.replace(downloaded.path, runtime_artifact)
                if (
                    lifecycle is not None
                    and artifact.kind == PUBLISHER_BOOTSTRAP_EXECUTABLE_KIND
                ):
                    _unpack_runtime(
                        runtime_artifact,
                        version_dir,
                        kind=artifact.kind,
                        lifecycle=lifecycle,
                    )
                else:
                    _unpack_runtime(runtime_artifact, version_dir, kind=artifact.kind)
                runtime_artifact.unlink(missing_ok=True)
                if bootstrap_markers_present(version_dir):
                    raise ManagedInstallError(
                        "A runtime artifact must not supply native bootstrap ownership markers."
                    )
                self._write_journal(journal, phase="runtime_unpacked")

            if lifecycle is not None:
                lifecycle.checkpoint(InstallationPhase.VERIFICATION)
            _verify_unpacked_application_profile(
                self.layout, target, self.application_profile
            )

            # Arm the no-recovery fence before importing any target code. A
            # hard process death cannot pass this point and leave the prior
            # program eligible to start against possibly changed data.
            if lifecycle is not None:
                lifecycle.checkpoint(InstallationPhase.ENVIRONMENT)
                lifecycle.advance(
                    InstallationPhase.ENVIRONMENT, cancellation_safe=False
                )
            if (
                recovery_plan.selected_coverage is RecoveryCoverage.NONE
                and not no_data_execution
            ):
                current_recovery_fence = {
                    "format": RECOVERY_REQUIRED_FORMAT,
                    "schema_version": RECOVERY_REQUIRED_VERSION,
                    "status": "armed",
                    "transaction_id": transaction_id,
                    "created_at": _now(),
                    "active_version": previous,
                    "failed_target_version": target,
                    "effective_impact": recovery_plan.effective_impact,
                    "selected_coverage": recovery_plan.selected_coverage,
                    "uncovered_domains": list(recovery_plan.uncovered_domains),
                    "error": None,
                }
                _durable_atomic_json(
                    self.layout.recovery_required_file,
                    current_recovery_fence,
                )
                journal["recovery_required"] = current_recovery_fence
                self._write_journal(journal, phase="recovery_fence_armed")

            if no_data_execution:
                if lifecycle is not None:
                    lifecycle.advance(
                        InstallationPhase.READINESS, cancellation_safe=False
                    )
                if probe_capability is None:
                    raise ManagedCompatibilityError(
                        "The no-data managed-probe capability disappeared after preflight."
                    )
                journal["isolated_target_execution_started"] = True
                self._write_journal(journal, phase="isolated_probe_pending")
                try:
                    probe_attestation = run_managed_release_probe(
                        capability=probe_capability,
                        target_root=self.layout.version_dir(target),
                        python=self.layout.python_executable(target),
                        target_version=target,
                        data_root=self.paths.data_root,
                        program_root=self.layout.root,
                        program_state=self.layout.state_dir,
                    )
                except ManagedProbeError as exc:
                    raise ManagedCompatibilityError(str(exc)) from exc
                journal["probe_attestation"] = probe_attestation.to_dict()
                journal["migration_started"] = False
                journal["migration_completed"] = False
                self._write_journal(journal, phase="isolated_probe_passed")
            else:
                # From this point onward target release code can access live
                # data. Failures after this boundary restore the selected
                # recovery material or preserve the no-recovery fence.
                target_execution_started = True
                journal["target_execution_started"] = True
                self._write_journal(journal, phase="runtime_probe_pending")
                _verify_target_runtime(
                    self.layout,
                    self.paths,
                    target,
                    initialize_data=False,
                    application_profile=self.application_profile,
                    **target_lifecycle_options,
                )
                self._write_journal(journal, phase="runtime_probe_passed")

                initialize_data = self.paths.database_file.is_file()
                migration_started = initialize_data
                journal["migration_started"] = migration_started
                if initialize_data:
                    self._write_journal(journal, phase="target_verification")
                    _verify_target_runtime(
                        self.layout,
                        self.paths,
                        target,
                        initialize_data=True,
                        application_profile=self.application_profile,
                        update_transaction_id=transaction_id,
                        **target_lifecycle_options,
                    )
                journal["migration_completed"] = migration_started
                self._write_journal(journal, phase="health_gate_passed")

            if lifecycle is not None:
                lifecycle.advance(InstallationPhase.ACTIVATION, cancellation_safe=False)
            receipt = {
                "format": RELEASE_RECEIPT_FORMAT,
                "schema_version": RELEASE_RECEIPT_VERSION,
                "version": target,
                "installed_at": _now(),
                "transaction_id": transaction_id,
                **(
                    {"operation_id": lifecycle.operation_id}
                    if lifecycle is not None
                    else {}
                ),
                "channel": channel,
                "signer_key_id": signer,
                "application_profile": self.application_profile.value,
                "program_root": str(self.layout.root.resolve()),
                "data_root": str(self.paths.data_root.resolve()),
                "index_source": str(index_source),
                "index_sha256": index_sha,
                "release_index_schema_version": index.schema_version,
                **(
                    {
                        "bootstrap_claim_id": native_claim_id,
                        "bootstrap_index_sha256": native_index_sha256,
                    }
                    if native_claim_id is not None
                    else {}
                ),
                "release_contract_sha256": release_contract_sha256(index, release),
                "execution_contract": (
                    release.execution.to_dict() if release.execution else None
                ),
                "artifact": asdict(artifact),
                "database": asdict(release.database),
                "addon_api_version": release.addon_api_version,
                "previous_version": previous,
                "acknowledged_recovery_required": (
                    str(pending_recovery["transaction_id"])
                    if pending_recovery is not None
                    else None
                ),
                "backup": journal.get("backup"),
                "database_checkpoint": journal.get("database_checkpoint"),
                "recovery_plan": compatibility.recovery_plan.to_dict(),
                "probe_attestation": (
                    probe_attestation.to_dict() if probe_attestation else None
                ),
            }
            _atomic_json(self.layout.receipt_file(target), receipt)
            self.layout.write_launchers(
                data_root=self.paths.data_root,
                application_profile=self.application_profile,
            )
            self._write_journal(journal, phase="activation_pending")
            activation_started = True
            self.layout.activate(target)
            try:
                recovery_records = [
                    record
                    for record in (pending_recovery, current_recovery_fence)
                    if record is not None
                ]
                for record in recovery_records:
                    resolved_recovery = {
                        **record,
                        "status": "resolved",
                        "resolved_at": _now(),
                        "resolved_by_transaction_id": transaction_id,
                        "resolved_by_version": target,
                    }
                    if record is recovery_records[-1]:
                        _durable_atomic_json(
                            self.layout.recovery_required_file,
                            resolved_recovery,
                        )
                    _atomic_json(
                        self.layout.history_dir
                        / (f"recovery-resolved-{record['transaction_id']}.json"),
                        resolved_recovery,
                    )
                self._write_journal(journal, phase="completed")
            except OSError:
                # Activation is the commit point. A diagnostic write failure
                # after the atomic pointer switch must not roll data back
                # underneath the newly active runtime.
                pass
            return UpdateResult(
                transaction_id=transaction_id,
                previous_version=previous,
                active_version=target,
                backup_path=backup.path if backup else None,
                database_checkpoint_path=(
                    database_checkpoint.path if database_checkpoint else None
                ),
                signer_key_id=signer,
                first_install=previous is None,
                recovery_plan=compatibility.recovery_plan,
            )
        except BaseException as exc:
            if not journal_owned:
                # An older transaction still owns the journal until recovery
                # and archival finish. Never replace its data-recovery fence
                # with diagnostics from this not-yet-started operation.
                raise
            journal["error"] = f"{type(exc).__name__}: {exc}"
            activation_committed = False
            activation_unknown = False
            if activation_started and target is not None:
                try:
                    activation_committed = self._activation_commit_state(
                        target,
                        previous,
                        transaction_id,
                    )
                except BaseException:
                    # Preserve the original activation error when commit
                    # inspection is itself interrupted or unreadable.
                    activation_unknown = True
                if activation_committed:
                    journal["activation_commit_observed"] = True
                elif activation_unknown:
                    journal["activation_outcome_unknown"] = True
            recover_before_commit = not activation_started or (
                not activation_committed and not activation_unknown
            )
            if (
                recover_before_commit
                and target_execution_started
                and (backup is not None or database_checkpoint is not None)
            ):
                if lifecycle is not None:
                    lifecycle.advance(
                        InstallationPhase.RECOVERY, cancellation_safe=False
                    )
                try:
                    journal["automatic_data_restore"] = (
                        self._restore_failed_update_data(
                            journal,
                            backup=(
                                {
                                    "path": str(backup.path),
                                    "sha256": backup.sha256,
                                    "backup_id": backup.backup_id,
                                }
                                if backup is not None
                                else None
                            ),
                            checkpoint=(
                                database_checkpoint.to_dict()
                                if database_checkpoint is not None
                                else None
                            ),
                        )
                    )
                # Preserve the original update failure even if recovery is
                # interrupted; the journal must retain both error conditions.
                except BaseException as restore_exc:  # noqa: BLE001
                    journal["automatic_data_restore"] = {
                        "completed": False,
                        "error": f"{type(restore_exc).__name__}: {restore_exc}",
                    }
            elif (
                target_execution_started
                and (recover_before_commit or activation_unknown)
                and recovery_plan is not None
                and recovery_plan.selected_coverage is RecoveryCoverage.NONE
            ):
                recovery_required = {
                    **(current_recovery_fence or {}),
                    "format": RECOVERY_REQUIRED_FORMAT,
                    "schema_version": RECOVERY_REQUIRED_VERSION,
                    "status": "required",
                    "transaction_id": transaction_id,
                    "created_at": (current_recovery_fence or {}).get(
                        "created_at", _now()
                    ),
                    "active_version": journal.get("previous_version"),
                    "failed_target_version": target,
                    "effective_impact": recovery_plan.effective_impact,
                    "selected_coverage": recovery_plan.selected_coverage,
                    "uncovered_domains": list(recovery_plan.uncovered_domains),
                    "error": journal["error"],
                }
                try:
                    _durable_atomic_json(
                        self.layout.recovery_required_file,
                        recovery_required,
                    )
                except OSError as marker_exc:
                    journal["recovery_required_marker_error"] = (
                        f"{type(marker_exc).__name__}: {marker_exc}"
                    )
                journal["recovery_required"] = recovery_required
            if recover_before_commit and target is not None:
                try:
                    receipt_path = self.layout.receipt_file(target)
                    if receipt_path.is_file():
                        try:
                            receipt_payload = json.loads(
                                receipt_path.read_text(encoding="utf-8")
                            )
                        except OSError, json.JSONDecodeError:
                            receipt_payload = {}
                        if receipt_payload.get("transaction_id") == transaction_id:
                            if reused_receipt is not None:
                                _atomic_json(receipt_path, reused_receipt)
                            else:
                                receipt_path.unlink(missing_ok=True)
                    if reused_receipt is None:
                        quarantined = self._quarantine_incomplete_release(
                            target,
                            transaction_id=transaction_id,
                        )
                        if quarantined is not None:
                            journal["failed_release_quarantine"] = str(quarantined)
                # Quarantine is best-effort cleanup during an already failing
                # transaction and must not replace the primary exception.
                except BaseException as quarantine_exc:  # noqa: BLE001
                    journal["failed_release_quarantine_error"] = (
                        f"{type(quarantine_exc).__name__}: {quarantine_exc}"
                    )
            try:
                self._write_journal(journal, phase="failed")
            except OSError:
                pass
            raise
        finally:
            for path in self.layout.staging_dir.glob(f"{transaction_id}-*"):
                if path.is_file():
                    path.unlink(missing_ok=True)
            if runtime_lock is not None:
                runtime_lock.release()
            lock.release()

    def adopt_installed_release(
        self,
        index_source: str | os.PathLike[str],
        artifact_path: str | os.PathLike[str],
        version_dir: str | os.PathLike[str],
        *,
        channel: str = "stable",
        requested_version: str | None = None,
        expected_index_sha256: str | None = None,
        retry_transaction_id: str | None = None,
        resume_bootstrap_claim_id: str | None = None,
        allow_unsigned_local: bool = False,
        lifecycle: InstallationLifecycle | None = None,
    ) -> UpdateResult:
        """Verify and transactionally adopt a pack at its immutable final path."""

        _validate_adoption_retry_arguments(
            requested_version=requested_version,
            expected_index_sha256=expected_index_sha256,
            retry_transaction_id=retry_transaction_id,
            resume_bootstrap_claim_id=resume_bootstrap_claim_id,
        )
        return self._run_installation_operation(
            InstallationOperation.INSTALL,
            lifecycle,
            lambda: self._adopt_installed_release(
                index_source,
                artifact_path,
                version_dir,
                channel=channel,
                requested_version=requested_version,
                expected_index_sha256=expected_index_sha256,
                retry_transaction_id=retry_transaction_id,
                resume_bootstrap_claim_id=resume_bootstrap_claim_id,
                allow_unsigned_local=allow_unsigned_local,
                lifecycle=lifecycle,
            ),
        )

    def _adopt_installed_release(
        self,
        index_source: str | os.PathLike[str],
        artifact_path: str | os.PathLike[str],
        version_dir: str | os.PathLike[str],
        *,
        channel: str = "stable",
        requested_version: str | None = None,
        expected_index_sha256: str | None = None,
        retry_transaction_id: str | None = None,
        resume_bootstrap_claim_id: str | None = None,
        allow_unsigned_local: bool = False,
        lifecycle: InstallationLifecycle | None = None,
    ) -> UpdateResult:
        """Verify and transactionally adopt a pack at its immutable final path."""

        _validate_adoption_retry_arguments(
            requested_version=requested_version,
            expected_index_sha256=expected_index_sha256,
            retry_transaction_id=retry_transaction_id,
            resume_bootstrap_claim_id=resume_bootstrap_claim_id,
        )
        target_lifecycle_options: _TargetLifecycleOptions = (
            {"lifecycle": lifecycle} if lifecycle is not None else {}
        )
        index, signer, index_sha = self._verified_index(
            index_source,
            channel=channel,
            allow_unsigned_local=allow_unsigned_local,
        )
        if expected_index_sha256 is not None and index_sha != expected_index_sha256:
            raise ReleaseManifestError(
                "Release index SHA-256 does not match the expected frozen input."
            )
        if lifecycle is not None:
            lifecycle.checkpoint(InstallationPhase.VERIFICATION)
        lock = self._acquire_lock()
        runtime_lock: RuntimeLeaderLock | None = None
        transaction_id = uuid.uuid4().hex
        journal: dict[str, Any] = {
            "format": UPDATE_JOURNAL_FORMAT,
            "schema_version": UPDATE_JOURNAL_VERSION,
            "transaction_id": transaction_id,
            "operation": "adopt",
            **(
                {"operation_id": lifecycle.operation_id}
                if lifecycle is not None
                else {}
            ),
            "started_at": _now(),
            "index_source": str(index_source),
            "index_sha256": index_sha,
            "channel": channel,
            "signer_key_id": signer,
            "application_profile": self.application_profile.value,
            "program_root": str(self.layout.root.resolve()),
            "data_root": str(self.paths.data_root.resolve()),
        }
        backup: BackupRecord | None = None
        data_recovery_required = False
        target: str | None = None
        previous: str | None = None
        activation_started = False
        journal_owned = False
        retry_state: _RetainedAdoptionRetry | None = None
        retry_receipt: dict[str, Any] | None = None
        native_claim_id: str | None = None
        native_target: str | None = None
        effective_retry_transaction_id = retry_transaction_id
        probe_attestation: ManagedProbeAttestation | None = None
        try:
            runtime_lock = _acquire_runtime_maintenance_lock(self.paths)
            _assert_runtime_stopped(self.paths)
            self.layout.ensure()
            if lifecycle is not None:
                lifecycle.advance(InstallationPhase.RECOVERY, cancellation_safe=False)
            try:
                native_marked = bootstrap_markers_present(
                    Path(version_dir).expanduser()
                )
            except BootstrapOwnershipError as exc:
                if resume_bootstrap_claim_id is None:
                    raise ManagedInstallError(
                        "Native bootstrap ownership cannot be inspected safely."
                    ) from exc
                native_marked = False
            if native_marked:
                if requested_version is None or expected_index_sha256 is None:
                    raise ManagedInstallError(
                        "Native adoption requires an exact version and index hash."
                    )
                native_release = index.select(requested_version=requested_version)
                if native_release is None:
                    raise ManagedInstallError(
                        "No native bootstrap release is available."
                    )
                native_target = _safe_version(native_release.version)
                native_path = Path(version_dir).expanduser()
                if native_path.resolve() != self.layout.version_dir(native_target):
                    raise ManagedInstallError(
                        "Native bootstrap is outside the selected generation path."
                    )
                native_artifact = native_release.artifact_for(self.platform_tag)
                native_source = Path(artifact_path).expanduser().resolve()
                if (
                    not native_source.is_file()
                    or native_source.stat().st_size != native_artifact.size
                    or sha256_file(native_source) != native_artifact.sha256
                ):
                    raise ManagedInstallError(
                        "Adopted release artifact failed signed hash checks."
                    )
                try:
                    native_claim_id = self._validate_native_bootstrap_generation(
                        native_path,
                        index,
                        native_release,
                        native_artifact,
                        index_sha,
                    )
                except ManagedInstallError:
                    if resume_bootstrap_claim_id is None:
                        raise
                    native_claim_id = None
                journal["bootstrap_claim_id"] = native_claim_id
                journal["bootstrap_index_sha256"] = index_sha
            self._validate_native_journal_roots()
            if resume_bootstrap_claim_id is not None:
                if (
                    native_claim_id != resume_bootstrap_claim_id
                    or native_target is None
                ):
                    raise ManagedInstallError(
                        "Native bootstrap resume does not match the ready generation."
                    )
                effective_retry_transaction_id = self._bootstrap_resume_transaction(
                    resume_bootstrap_claim_id,
                    native_target,
                )
            if effective_retry_transaction_id is not None:
                if requested_version is None or expected_index_sha256 is None:
                    raise AssertionError("Retry arguments were already validated.")
                retry_state = self._prepare_retained_adoption_retry(
                    index,
                    artifact_path=artifact_path,
                    version_dir=version_dir,
                    channel=channel,
                    requested_version=requested_version,
                    signer_key_id=signer,
                    index_sha256=index_sha,
                    retry_transaction_id=effective_retry_transaction_id,
                )
                retry_receipt = retry_state.receipt
                if retry_state.committed:
                    if lifecycle is not None:
                        retry_state.journal["last_reconciled_operation_id"] = (
                            lifecycle.operation_id
                        )
                    try:
                        self._write_journal(retry_state.journal, phase="completed")
                    except OSError:
                        # The receipt proves commit. Failure to persist readback
                        # diagnostics does not invalidate the original result.
                        pass
                    backup_record = retry_state.journal.get("backup")
                    return UpdateResult(
                        transaction_id=str(retry_state.journal["transaction_id"]),
                        previous_version=retry_state.previous,
                        active_version=retry_state.target,
                        backup_path=(
                            Path(backup_record["path"])
                            if isinstance(backup_record, dict)
                            else None
                        ),
                        database_checkpoint_path=None,
                        signer_key_id=signer,
                        first_install=retry_state.previous is None,
                        recovery_plan=retry_state.recovery_plan,
                    )
                journal["retried_adoption_transaction_id"] = (
                    effective_retry_transaction_id
                )
            recovered_switches = self._recover_interrupted_database_switches()
            recovered_rollback = self._recover_interrupted_rollback()
            recovered_update = self._recover_interrupted_update()
            if lifecycle is not None:
                lifecycle.checkpoint(InstallationPhase.PREFLIGHT)
            self._archive_existing_journal()
            journal_owned = True
            if recovered_switches:
                journal["recovered_database_switches"] = [
                    str(path) for path in recovered_switches
                ]
            if recovered_rollback is not None:
                journal["recovered_rollback_transaction"] = str(
                    recovered_rollback["transaction_id"]
                )
            if recovered_update is not None:
                journal["recovered_update_transaction"] = str(
                    recovered_update["transaction_id"]
                )
            if retry_state is not None:
                previous = retry_state.previous
                release = retry_state.release
                artifact = retry_state.artifact
                target = retry_state.target
                actual_dir = retry_state.version_dir
                source_artifact = retry_state.source_artifact
            else:
                previous = self.layout.current_version()
                release = index.select(
                    current_version=previous,
                    requested_version=requested_version,
                )
                if release is None:
                    raise ManagedInstallError("No release is available to adopt.")
                target = _safe_version(release.version)
                if previous is not None and Version(target) <= Version(previous):
                    raise ManagedInstallError(
                        f"Managed adoption must advance beyond active version {previous}."
                    )
                expected_dir = self.layout.version_dir(target)
                actual_dir = Path(version_dir).expanduser().resolve()
                if actual_dir != expected_dir or not actual_dir.is_dir():
                    raise ManagedInstallError(
                        f"Adopted runtime must already be at {expected_dir}."
                    )
                artifact = release.artifact_for(self.platform_tag)
                source_artifact = Path(artifact_path).expanduser().resolve()
                if (
                    not source_artifact.is_file()
                    or source_artifact.stat().st_size != artifact.size
                    or sha256_file(source_artifact) != artifact.sha256
                ):
                    raise ManagedInstallError(
                        "Adopted release artifact failed signed hash checks."
                    )
            recovery_plan = self._recovery_plan(
                previous,
                release=release,
                signer_key_id=signer,
                requested_coverage=RecoveryCoverage.AUTOMATIC,
                decision_source=RecoveryDecisionSource.AUTOMATIC,
                actor=None,
                reason=None,
                policy_id=None,
            )
            execution_authenticated = signer is not None
            no_data_execution = bool(
                execution_authenticated
                and release.execution is not None
                and release.execution.data_impact == "none"
            )
            probe_capability = self._probe_capability(
                release,
                signer_key_id=signer,
            )
            compatibility = preflight_release(
                self.paths,
                self.layout,
                release,
                artifact,
                artifact_already_staged=True,
                recovery_plan=recovery_plan,
                execution_authenticated=execution_authenticated,
                probe_capability=probe_capability,
            )
            if not compatibility.compatible:
                raise ManagedCompatibilityError(
                    "; ".join(
                        issue.message
                        for issue in compatibility.issues
                        if issue.blocking
                    )
                )
            journal.update(
                previous_version=previous,
                target_version=target,
                release_index_schema_version=index.schema_version,
                release_contract_sha256=release_contract_sha256(index, release),
                compatibility=compatibility.to_dict(),
                recovery_plan=compatibility.recovery_plan.to_dict(),
                artifact={
                    **asdict(artifact),
                    "staged_path": str(source_artifact),
                    "adopted_version_dir": str(actual_dir),
                },
            )
            self._write_journal(journal, phase="preflight_complete")

            if lifecycle is not None:
                lifecycle.checkpoint(InstallationPhase.VERIFICATION)
            _verify_unpacked_application_profile(
                self.layout, target, self.application_profile
            )

            if (
                self.paths.database_file.is_file()
                and recovery_plan.selected_coverage is RecoveryCoverage.FULL
            ):
                if lifecycle is not None:
                    lifecycle.advance(
                        InstallationPhase.RECOVERY, cancellation_safe=False
                    )
                backup = self._create_managed_backup(
                    previous,
                    key_store=InstallationKeyStore(self.paths.master_keys_file),
                )
                journal["backup"] = {
                    "path": str(backup.path),
                    "sha256": backup.sha256,
                    "backup_id": backup.backup_id,
                }
                self._write_journal(journal, phase="backup_complete")

            if lifecycle is not None:
                lifecycle.checkpoint(InstallationPhase.ENVIRONMENT)
                lifecycle.advance(
                    InstallationPhase.ENVIRONMENT, cancellation_safe=False
                )
            if no_data_execution:
                if lifecycle is not None:
                    lifecycle.advance(
                        InstallationPhase.READINESS, cancellation_safe=False
                    )
                if probe_capability is None:
                    raise ManagedCompatibilityError(
                        "The no-data managed-probe capability disappeared after preflight."
                    )
                journal["isolated_target_execution_started"] = True
                self._write_journal(journal, phase="isolated_probe_pending")
                try:
                    probe_attestation = run_managed_release_probe(
                        capability=probe_capability,
                        target_root=self.layout.version_dir(target),
                        python=self.layout.python_executable(target),
                        target_version=target,
                        data_root=self.paths.data_root,
                        program_root=self.layout.root,
                        program_state=self.layout.state_dir,
                    )
                except ManagedProbeError as exc:
                    raise ManagedCompatibilityError(str(exc)) from exc
                journal["probe_attestation"] = probe_attestation.to_dict()
                journal["migration_started"] = False
                journal["migration_completed"] = False
                self._write_journal(journal, phase="isolated_probe_passed")
            else:
                # The ordinary target probe imports executable release code
                # with live-data access, so recovery precedes this boundary.
                data_recovery_required = True
                journal["target_execution_started"] = True
                self._write_journal(journal, phase="runtime_probe_pending")
                _verify_target_runtime(
                    self.layout,
                    self.paths,
                    target,
                    initialize_data=False,
                    application_profile=self.application_profile,
                    **target_lifecycle_options,
                )
                self._write_journal(journal, phase="runtime_probe_passed")

                if self.paths.database_file.is_file():
                    journal["migration_started"] = True
                    self._write_journal(journal, phase="target_verification")
                    _verify_target_runtime(
                        self.layout,
                        self.paths,
                        target,
                        initialize_data=True,
                        application_profile=self.application_profile,
                        update_transaction_id=transaction_id,
                        **target_lifecycle_options,
                    )
                    journal["migration_completed"] = True
                    self._write_journal(journal, phase="health_gate_passed")

            if lifecycle is not None:
                lifecycle.advance(InstallationPhase.ACTIVATION, cancellation_safe=False)
            receipt = {
                "format": RELEASE_RECEIPT_FORMAT,
                "schema_version": RELEASE_RECEIPT_VERSION,
                "version": target,
                "installed_at": _now(),
                "transaction_id": transaction_id,
                **(
                    {"operation_id": lifecycle.operation_id}
                    if lifecycle is not None
                    else {}
                ),
                "channel": channel,
                "signer_key_id": signer,
                "application_profile": self.application_profile.value,
                "program_root": str(self.layout.root.resolve()),
                "data_root": str(self.paths.data_root.resolve()),
                "index_source": str(index_source),
                "index_sha256": index_sha,
                "release_index_schema_version": index.schema_version,
                "release_contract_sha256": release_contract_sha256(index, release),
                "execution_contract": (
                    release.execution.to_dict() if release.execution else None
                ),
                "artifact": asdict(artifact),
                "database": asdict(release.database),
                "addon_api_version": release.addon_api_version,
                "previous_version": previous,
                "backup": journal.get("backup"),
                "database_checkpoint": None,
                "recovery_plan": compatibility.recovery_plan.to_dict(),
                "probe_attestation": (
                    probe_attestation.to_dict() if probe_attestation else None
                ),
                "adopted": True,
                **(
                    {
                        "bootstrap_claim_id": native_claim_id,
                        "bootstrap_index_sha256": index_sha,
                    }
                    if native_claim_id is not None
                    else {}
                ),
                **(
                    {
                        "retried_adoption_transaction_id": (
                            effective_retry_transaction_id
                        )
                    }
                    if effective_retry_transaction_id is not None
                    else {}
                ),
            }
            _atomic_json(self.layout.receipt_file(target), receipt)
            self.layout.write_launchers(
                data_root=self.paths.data_root,
                application_profile=self.application_profile,
            )
            self._write_journal(journal, phase="activation_pending")
            activation_started = True
            self.layout.activate(target)
            try:
                self._write_journal(journal, phase="completed")
            except OSError:
                pass
            return UpdateResult(
                transaction_id=transaction_id,
                previous_version=previous,
                active_version=target,
                backup_path=backup.path if backup else None,
                database_checkpoint_path=None,
                signer_key_id=signer,
                first_install=previous is None,
                recovery_plan=compatibility.recovery_plan,
            )
        except BaseException as exc:
            if not journal_owned:
                # Preserve an unresolved prior transaction and its recovery
                # material when reconciliation itself is interrupted.
                raise
            journal["error"] = f"{type(exc).__name__}: {exc}"
            activation_committed = False
            activation_unknown = False
            if activation_started and target is not None:
                try:
                    activation_committed = self._activation_commit_state(
                        target,
                        previous,
                        transaction_id,
                    )
                except BaseException:
                    # Preserve the original activation error when commit
                    # inspection is itself interrupted or unreadable.
                    activation_unknown = True
                if activation_committed:
                    journal["activation_commit_observed"] = True
                elif activation_unknown:
                    journal["activation_outcome_unknown"] = True
            recover_before_commit = not activation_started or (
                not activation_committed and not activation_unknown
            )
            if recover_before_commit and data_recovery_required and backup is not None:
                if lifecycle is not None:
                    lifecycle.advance(
                        InstallationPhase.RECOVERY, cancellation_safe=False
                    )
                try:
                    journal["automatic_data_restore"] = (
                        self._restore_failed_update_data(
                            journal,
                            backup={
                                "path": str(backup.path),
                                "sha256": backup.sha256,
                                "backup_id": backup.backup_id,
                            },
                            checkpoint=None,
                        )
                    )
                except BaseException as restore_exc:  # noqa: BLE001
                    journal["automatic_data_restore"] = {
                        "completed": False,
                        "error": f"{type(restore_exc).__name__}: {restore_exc}",
                    }
            if recover_before_commit and target is not None:
                receipt_path = self.layout.receipt_file(target)
                if receipt_path.is_file():
                    try:
                        receipt_payload = json.loads(
                            receipt_path.read_text(encoding="utf-8")
                        )
                    except OSError, json.JSONDecodeError:
                        receipt_payload = {}
                    if receipt_payload.get("transaction_id") == transaction_id:
                        if retry_receipt is not None:
                            _atomic_json(receipt_path, retry_receipt)
                        else:
                            receipt_path.unlink(missing_ok=True)
            try:
                self._write_journal(journal, phase="failed")
            except OSError:
                pass
            raise
        finally:
            if runtime_lock is not None:
                runtime_lock.release()
            lock.release()

    def _restore_paired_backup(
        self,
        backup_path: str | os.PathLike[str],
        *,
        expected_sha256: str,
        transaction_id: str,
        activation_witness: RestoreActivationWitness | None = None,
    ) -> RestoreResult:
        source = Path(backup_path).expanduser().resolve()
        if (
            not re.fullmatch(r"[0-9a-f]{64}", expected_sha256)
            or not source.is_file()
            or sha256_file(source) != expected_sha256
        ):
            raise ManagedInstallError("Paired update backup is missing or changed.")

        key_store = InstallationKeyStore(self.paths.master_keys_file)
        try:
            manifest = verify_backup(source, available_key_ids=key_store.key_ids())
        except BackupError as exc:
            raise ManagedInstallError(
                "Paired update backup cannot be verified."
            ) from exc

        base_source: Path | None = None
        if manifest["version"] != BACKUP_VERSION:
            try:
                base_source, _base_manifest = resolve_backup_base(
                    source,
                    manifest,
                    available_key_ids=key_store.key_ids(),
                )
            except BackupError as exc:
                raise ManagedInstallError(
                    "Paired update backup base cannot be verified."
                ) from exc

        # A v1 archive keeps the original recovery filename.  An overlay gets
        # a transaction-private catalog containing both immutable inputs, so a
        # data-root switch cannot strand the base between verification and
        # restore.
        recovery_root = self.layout.recovery_dir
        recovery_set: Path | None = None
        if base_source is None:
            recovery_copy = recovery_root / f"{transaction_id}-{source.name}"
        else:
            recovery_set = recovery_root / f"{transaction_id}-backup-set"
            if recovery_set.exists() or recovery_set.is_symlink():
                if recovery_set.is_symlink() or not recovery_set.is_dir():
                    raise ManagedInstallError("Recovery backup set is unsafe.")
            else:
                recovery_set.mkdir(parents=False)
                _fsync_directory(recovery_set.parent)
            recovery_copy = recovery_set / source.name

        def stage(source_file: Path, destination: Path, digest: str) -> None:
            if destination.exists():
                if destination.is_symlink() or not destination.is_file():
                    raise ManagedInstallError("Recovery copy is unsafe.")
                if sha256_file(destination) != digest:
                    raise ManagedInstallError("Recovery copy already exists changed.")
                return
            temporary = destination.with_name(
                f".{destination.name}.part-{os.getpid()}-{uuid.uuid4().hex}"
            )
            try:
                shutil.copy2(source_file, temporary)
                with temporary.open("rb+") as handle:
                    handle.flush()
                    os.fsync(handle.fileno())
                if sha256_file(temporary) != digest:
                    raise ManagedInstallError("Recovery copy failed verification.")
                os.replace(temporary, destination)
                _fsync_directory(destination.parent)
                if sha256_file(destination) != digest:
                    raise ManagedInstallError("Recovery copy failed verification.")
            finally:
                temporary.unlink(missing_ok=True)

        try:
            if base_source is not None:
                base_digest = str(manifest["base"]["sha256"])
                stage(base_source, recovery_copy.parent / base_source.name, base_digest)
            stage(source, recovery_copy, expected_sha256)
            try:
                verify_backup(
                    recovery_copy,
                    available_key_ids=key_store.key_ids(),
                )
            except BackupError as exc:
                raise ManagedInstallError(
                    "Recovery backup copy cannot be verified."
                ) from exc
        except BaseException:
            if recovery_set is not None:
                shutil.rmtree(recovery_set, ignore_errors=True)
            else:
                recovery_copy.unlink(missing_ok=True)
            raise
        return restore_backup(
            recovery_copy,
            self.paths.data_root,
            key_store=key_store,
            activate=True,
            activation_witness=activation_witness,
        )

    def rollback(
        self,
        *,
        restore_data: bool = False,
        lifecycle: InstallationLifecycle | None = None,
    ) -> RollbackResult:
        return self._run_installation_operation(
            InstallationOperation.ROLLBACK,
            lifecycle,
            lambda: self._rollback(
                restore_data=restore_data,
                lifecycle=lifecycle,
            ),
        )

    def _rollback(
        self,
        *,
        restore_data: bool = False,
        lifecycle: InstallationLifecycle | None = None,
    ) -> RollbackResult:
        target_lifecycle_options: _TargetLifecycleOptions = (
            {"lifecycle": lifecycle} if lifecycle is not None else {}
        )
        lock = self._acquire_lock()
        runtime_lock: RuntimeLeaderLock | None = None
        transaction_id = uuid.uuid4().hex
        try:
            runtime_lock = _acquire_runtime_maintenance_lock(self.paths)
            _assert_runtime_stopped(self.paths)
            if lifecycle is not None:
                lifecycle.advance(InstallationPhase.RECOVERY, cancellation_safe=False)
            self._validate_native_journal_roots()
            self._recover_interrupted_database_switches()
            self._recover_interrupted_rollback()
            self._recover_interrupted_update()
            if lifecycle is not None:
                lifecycle.checkpoint(InstallationPhase.PREFLIGHT)
            current = self.layout.current_version()
            if current is None:
                raise ManagedInstallError("No managed release is active.")
            receipt = self.layout.read_receipt(current)
            previous_raw = receipt.get("previous_version")
            if not previous_raw:
                raise ManagedInstallError(
                    f"Release {current} has no prior managed version to roll back to."
                )
            previous = _safe_version(str(previous_raw))
            self.layout.python_executable(previous)
            _verify_unpacked_application_profile(
                self.layout, previous, self.application_profile
            )
            if lifecycle is not None:
                lifecycle.checkpoint(InstallationPhase.VERIFICATION)
                lifecycle.advance(InstallationPhase.ENVIRONMENT, cancellation_safe=False)
            _verify_target_runtime(
                self.layout,
                self.paths,
                previous,
                initialize_data=False,
                application_profile=self.application_profile,
                **target_lifecycle_options,
            )
            database = receipt.get("database") or {}
            restore_required = database.get("rollback") == "restore_required"
            if restore_required and not restore_data:
                raise ManagedInstallError(
                    "This program rollback requires its paired data recovery material. "
                    "Re-run with --restore-data after reviewing the receipt."
                )

            restored: RestoreResult | None = None
            restored_checkpoint: DatabaseCheckpointRestoreResult | None = None
            rollback_transaction: dict[str, Any] | None = None
            if restore_data:
                backup = receipt.get("backup")
                checkpoint = receipt.get("database_checkpoint")
                if isinstance(backup, dict) and isinstance(checkpoint, dict):
                    raise ManagedInstallError(
                        f"Release {current} has ambiguous recovery material."
                    )
                if isinstance(backup, dict):
                    rollback_transaction = {
                        "format": ROLLBACK_TRANSACTION_FORMAT,
                        "schema_version": ROLLBACK_TRANSACTION_VERSION,
                        "transaction_id": transaction_id,
                        **(
                            {"operation_id": lifecycle.operation_id}
                            if lifecycle is not None
                            else {}
                        ),
                        "started_at": _now(),
                        "from_version": current,
                        "to_version": previous,
                        "application_profile": self.application_profile.value,
                        "coverage": "full",
                        "backup": backup,
                    }
                    if lifecycle is not None:
                        lifecycle.advance(
                            InstallationPhase.RECOVERY, cancellation_safe=False
                        )
                    self._write_rollback_transaction(
                        rollback_transaction,
                        phase="prepared",
                    )
                    restored = self._restore_paired_backup(
                        str(backup.get("path") or ""),
                        expected_sha256=str(backup.get("sha256") or ""),
                        transaction_id=transaction_id,
                        activation_witness=self._rollback_restore_witness(
                            transaction_id
                        ),
                    )
                elif isinstance(checkpoint, dict):
                    rollback_transaction = {
                        "format": ROLLBACK_TRANSACTION_FORMAT,
                        "schema_version": ROLLBACK_TRANSACTION_VERSION,
                        "transaction_id": transaction_id,
                        **(
                            {"operation_id": lifecycle.operation_id}
                            if lifecycle is not None
                            else {}
                        ),
                        "started_at": _now(),
                        "from_version": current,
                        "to_version": previous,
                        "application_profile": self.application_profile.value,
                        "coverage": "database_only",
                        "database_checkpoint": checkpoint,
                    }
                    if lifecycle is not None:
                        lifecycle.advance(
                            InstallationPhase.RECOVERY, cancellation_safe=False
                        )
                    self._write_rollback_transaction(
                        rollback_transaction,
                        phase="prepared",
                    )
                    restored_checkpoint = self._restore_database_checkpoint(
                        checkpoint,
                        transaction_id=transaction_id,
                    )
                else:
                    raise ManagedInstallError(
                        f"Release {current} has no selected recovery material."
                    )
                self._write_rollback_transaction(
                    rollback_transaction,
                    phase="data_restored",
                )
            try:
                _verify_target_runtime(
                    self.layout,
                    self.paths,
                    previous,
                    initialize_data=self.paths.database_file.is_file(),
                    application_profile=self.application_profile,
                    **target_lifecycle_options,
                )
                if lifecycle is not None:
                    lifecycle.advance(
                        InstallationPhase.ACTIVATION, cancellation_safe=False
                    )
                self.layout.write_launchers(
                    data_root=self.paths.data_root,
                    application_profile=self.application_profile,
                )
                self.layout.activate(previous)
            except BaseException as rollback_exc:
                if restored is not None or restored_checkpoint is not None:
                    recovery_event_error: BaseException | None = None
                    if lifecycle is not None:
                        try:
                            lifecycle.advance(
                                InstallationPhase.RECOVERY, cancellation_safe=False
                            )
                        except BaseException as observer_exc:
                            # An observational interrupt must not skip the data
                            # reactivation already required by this failure.
                            recovery_event_error = observer_exc
                    try:
                        if restored is not None:
                            if restored.safety_root is None:
                                raise ManagedInstallError(
                                    "Full rollback restore has no safety root."
                                )
                            if rollback_transaction is not None:
                                self._write_rollback_transaction(
                                    rollback_transaction,
                                    phase="reactivation_started",
                                )
                            failed_root = self._reactivate_full_restore(
                                safety_root=restored.safety_root,
                                transaction_id=transaction_id,
                                witness=self._rollback_restore_witness(transaction_id),
                            )
                        else:
                            if restored_checkpoint is None:
                                raise AssertionError(
                                    "Checkpoint rollback lost its restore record."
                                )
                            failed_root = reactivate_checkpoint_safety(
                                restored_checkpoint,
                                transaction_id=transaction_id,
                            )
                        if rollback_transaction is not None:
                            rollback_transaction["failure"] = (
                                f"{type(rollback_exc).__name__}: {rollback_exc}"
                            )
                            self._write_rollback_transaction(
                                rollback_transaction,
                                phase="aborted",
                            )
                    except BaseException as revert_exc:  # noqa: BLE001
                        raise ManagedInstallError(
                            "Rollback failed and Carlina could not reactivate the "
                            f"pre-rollback data root: {revert_exc}"
                        ) from rollback_exc
                    try:
                        _atomic_json(
                            self.layout.history_dir
                            / f"rollback-failed-{transaction_id}.json",
                            {
                                "format": UPDATE_JOURNAL_FORMAT,
                                "schema_version": UPDATE_JOURNAL_VERSION,
                                "transaction_id": transaction_id,
                                "operation": "rollback",
                                **(
                                    {"operation_id": lifecycle.operation_id}
                                    if lifecycle is not None
                                    else {}
                                ),
                                "application_profile": self.application_profile.value,
                                "failed_at": _now(),
                                "from_version": current,
                                "to_version": previous,
                                "error": (
                                    f"{type(rollback_exc).__name__}: {rollback_exc}"
                                ),
                                "data_reactivated": True,
                                "failed_restore_root": str(failed_root),
                            },
                        )
                    except OSError:
                        pass
                    if recovery_event_error is not None:
                        raise recovery_event_error from rollback_exc
                raise
            if rollback_transaction is not None:
                try:
                    self._write_rollback_transaction(
                        rollback_transaction,
                        phase="completed",
                    )
                except OSError:
                    # The managed current-version pointer is the commit proof.
                    # Maintenance recovery will reconcile this diagnostic state.
                    pass
            history = {
                "format": UPDATE_JOURNAL_FORMAT,
                "schema_version": UPDATE_JOURNAL_VERSION,
                "transaction_id": transaction_id,
                "operation": "rollback",
                **(
                    {"operation_id": lifecycle.operation_id}
                    if lifecycle is not None
                    else {}
                ),
                "application_profile": self.application_profile.value,
                "completed_at": _now(),
                "from_version": current,
                "to_version": previous,
                "data_restored": bool(restored or restored_checkpoint),
                "restore_safety_root": str(restored.safety_root)
                if restored is not None and restored.safety_root
                else (
                    str(restored_checkpoint.safety_dir)
                    if restored_checkpoint is not None
                    else None
                ),
            }
            try:
                _atomic_json(
                    self.layout.history_dir / f"rollback-{transaction_id}.json",
                    history,
                )
            except OSError:
                # The pointer switch is the commit point; a diagnostic write
                # must not make a successful rollback appear to have failed.
                pass
            return RollbackResult(
                transaction_id,
                current,
                previous,
                bool(restored or restored_checkpoint),
                restored.safety_root
                if restored is not None
                else (
                    restored_checkpoint.safety_dir
                    if restored_checkpoint is not None
                    else None
                ),
            )
        finally:
            if runtime_lock is not None:
                runtime_lock.release()
            lock.release()


def sha256_file_bytes(value: bytes) -> str:
    import hashlib

    return hashlib.sha256(value).hexdigest()


__all__ = [
    "MANAGED_INSTALL_FORMAT",
    "MANAGED_INSTALL_VERSION",
    "CompatibilityIssue",
    "CompatibilityReport",
    "ManagedCompatibilityError",
    "ManagedInstallError",
    "ManagedInstallationLayout",
    "ManagedReleaseManager",
    "ManagedRuntimeActiveError",
    "ReleaseCheckResult",
    "RollbackResult",
    "UpdateResult",
    "default_program_root",
    "preflight_release",
]
