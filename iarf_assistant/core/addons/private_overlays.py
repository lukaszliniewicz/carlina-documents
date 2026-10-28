"""Stopped-instance takeover of reviewed private built-in add-on overlays.

This deliberately narrow maintenance path moves one active private built-in,
or one inactive private built-in with exact current package-loss evidence, to
an exact reviewed installed generation.  It is not a general package installer:
it insists on an identity-equivalent built-in row, creates a verified application
backup before the first ownership change, and uses the normal installation
coordinator only after the explicit takeover transition is journaled.  An
inactive built-in is eligible only when its current lifecycle generation records
a successful package-code-missing transition to ``code_removed``.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from filelock import Timeout as FileLockTimeout

from carlina import create_app
from carlina.runtime_environment import is_worker_process
from iarf_assistant.core.addons.contracts import AddonSourceKind, compile_addon_contract
from iarf_assistant.core.addons.installation import AddonInstallationCoordinator
from iarf_assistant.core.addons.installer import AddonPackageInstaller
from iarf_assistant.core.addons.migration_contracts import (
    stored_migration_upgrade_issues,
)
from iarf_assistant.core.backup_service import BackupError, create_backup, verify_backup
from iarf_assistant.core.encryption import InstallationKeyStore
from iarf_assistant.core.runtime_ownership import (
    load_runtime_leader,
    process_is_running,
    runtime_leader_lock,
)
from iarf_assistant.extensions import db
from iarf_assistant.models import Addon, AddonLifecycleEvent, User

INDEX_FORMAT = "carlina.private-addon-overlay-index"
INDEX_SCHEMA_VERSION = 1
MAX_INDEX_BYTES = 2 * 1024 * 1024
MAX_OVERLAYS = 500
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
REVISION_PATTERN = re.compile(r"^[0-9a-f]{40}$")
ADDON_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]*$")
TAKEOVER_JOURNAL_FORMAT = "carlina.private-addon-takeover-journal"
TAKEOVER_JOURNAL_VERSION = 2
CONTACT_SCRAPING_ID = "contact_scraping"
MAX_JOURNAL_BYTES = 1 * 1024 * 1024
MAX_JOURNAL_FILES = 500
_JOURNAL_PHASES = frozenset(
    {
        "preflighted",
        "takeover_started",
        "source_kind_committed",
        "activated",
        "already_completed",
        "recovery_required",
        "not_attempted",
    }
)
_JOURNAL_STATUSES = frozenset({"preflighted", "partial_failure", "complete"})
_UNFINISHED_JOURNAL_STATUSES = frozenset({"preflighted", "partial_failure"})


class PrivateOverlayTakeoverError(RuntimeError):
    """A private overlay takeover cannot safely proceed."""


class PrivateOverlayTakeoverPartialError(PrivateOverlayTakeoverError):
    """A selected package changed state and later work was not attempted."""

    def __init__(self, results: tuple[PrivateOverlayTakeoverResult, ...]) -> None:
        super().__init__("A private overlay takeover needs recovery review.")
        self.results = results


@dataclass(frozen=True, slots=True)
class PrivateOverlayRecord:
    addon_id: str
    version: str
    source_revision: str
    archive_filename: str
    archive_path: Path
    archive_size: int
    archive_sha256: str
    manifest_sha256: str
    manifest_file_sha256: str
    requirements_lock_sha256: str | None
    contract_sha256: str


@dataclass(frozen=True, slots=True)
class PrivateOverlayIndex:
    path: Path
    sha256: str
    source_revision: str
    records: tuple[PrivateOverlayRecord, ...]


@dataclass(frozen=True, slots=True)
class PrivateOverlayTakeoverResult:
    addon_id: str
    version: str
    state: str
    source_kind: str
    requires_restart: bool
    recovery_action: str
    source_revision: str
    index_sha256: str
    archive_sha256: str
    manifest_sha256: str
    manifest_file_sha256: str
    contract_sha256: str
    requirements_lock_sha256: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "addon_id": self.addon_id,
            "version": self.version,
            "state": self.state,
            "source_kind": self.source_kind,
            "requires_restart": self.requires_restart,
            "recovery_action": self.recovery_action,
            "provenance": {
                "source_revision": self.source_revision,
                "index_sha256": self.index_sha256,
                "archive_sha256": self.archive_sha256,
                "manifest_sha256": self.manifest_sha256,
                "manifest_file_sha256": self.manifest_file_sha256,
                "contract_sha256": self.contract_sha256,
                "requirements_lock_sha256": self.requirements_lock_sha256,
            },
        }


@dataclass(frozen=True, slots=True)
class _AddonSnapshot:
    values: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _TakeoverPlan:
    record: PrivateOverlayRecord
    staged: Any
    builtin_contract: Any | None
    addon_snapshot: _AddonSnapshot | None
    reviewed_notice_ids: tuple[str, ...]
    already_completed: bool
    resume_after_flip: bool = False


def _json_object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PrivateOverlayTakeoverError(
                "The private overlay index contains duplicate JSON object keys."
            )
        result[key] = value
    return result


def _required_string(
    payload: dict[str, Any],
    key: str,
    *,
    pattern: re.Pattern[str] | None = None,
) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise PrivateOverlayTakeoverError(
            f"The private overlay index requires a non-empty {key!r} value."
        )
    if pattern is not None and pattern.fullmatch(value) is None:
        raise PrivateOverlayTakeoverError(
            f"The private overlay index has an invalid {key!r} value."
        )
    return value


def _required_sha256(payload: dict[str, Any], key: str) -> str:
    return _required_string(payload, key, pattern=SHA256_PATTERN)


def _requirements_lock_sha256(payload: dict[str, Any]) -> str | None:
    value = payload.get("requirements_lock_sha256")
    if value is None:
        return None
    if not isinstance(value, str) or SHA256_PATTERN.fullmatch(value) is None:
        raise PrivateOverlayTakeoverError(
            "The private overlay index has an invalid requirements lock digest."
        )
    return value


def _archive_path(index_dir: Path, filename: str) -> Path:
    candidate = Path(filename)
    if (
        candidate.name != filename
        or candidate.is_absolute()
        or "/" in filename
        or "\\" in filename
        or "\x00" in filename
    ):
        raise PrivateOverlayTakeoverError(
            "The private overlay index contains an unsafe archive filename."
        )
    try:
        resolved = (index_dir / candidate).resolve(strict=True)
        resolved.relative_to(index_dir)
    except (OSError, ValueError) as exc:
        raise PrivateOverlayTakeoverError(
            "The private overlay archive must resolve inside the index directory."
        ) from exc
    if not resolved.is_file():
        raise PrivateOverlayTakeoverError(
            "The private overlay archive is not a regular file."
        )
    return resolved


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_record(
    payload: object,
    *,
    source_revision: str,
    index_dir: Path,
) -> PrivateOverlayRecord:
    if not isinstance(payload, dict):
        raise PrivateOverlayTakeoverError(
            "Every private overlay record must be a JSON object."
        )
    required_keys = {
        "addon_id",
        "version",
        "source_revision",
        "archive_filename",
        "archive_size",
        "archive_sha256",
        "manifest_sha256",
        "manifest_file_sha256",
        "requirements_lock_sha256",
        "contract_sha256",
    }
    if set(payload) != required_keys:
        raise PrivateOverlayTakeoverError(
            "A private overlay record does not match index schema version 1."
        )
    addon_id = _required_string(payload, "addon_id", pattern=ADDON_ID_PATTERN)
    version = _required_string(payload, "version", pattern=VERSION_PATTERN)
    record_revision = _required_string(
        payload,
        "source_revision",
        pattern=REVISION_PATTERN,
    )
    if record_revision != source_revision:
        raise PrivateOverlayTakeoverError(
            "Every private overlay record must use the index source revision."
        )
    archive_filename = _required_string(payload, "archive_filename")
    archive_size = payload.get("archive_size")
    if (
        isinstance(archive_size, bool)
        or not isinstance(archive_size, int)
        or archive_size <= 0
    ):
        raise PrivateOverlayTakeoverError(
            "The private overlay index has an invalid archive size."
        )
    archive_path = _archive_path(index_dir, archive_filename)
    if archive_path.stat().st_size != archive_size:
        raise PrivateOverlayTakeoverError(
            "The private overlay archive size differs from its index record."
        )
    archive_sha256 = _required_sha256(payload, "archive_sha256")
    if _file_sha256(archive_path) != archive_sha256:
        raise PrivateOverlayTakeoverError(
            "The private overlay archive digest differs from its index record."
        )
    return PrivateOverlayRecord(
        addon_id=addon_id,
        version=version,
        source_revision=record_revision,
        archive_filename=archive_filename,
        archive_path=archive_path,
        archive_size=archive_size,
        archive_sha256=archive_sha256,
        manifest_sha256=_required_sha256(payload, "manifest_sha256"),
        manifest_file_sha256=_required_sha256(payload, "manifest_file_sha256"),
        requirements_lock_sha256=_requirements_lock_sha256(payload),
        contract_sha256=_required_sha256(payload, "contract_sha256"),
    )


def load_private_overlay_index(index_path: str | Path) -> PrivateOverlayIndex:
    """Load one exact builder index and verify every indexed archive on disk."""

    try:
        path = Path(index_path).expanduser().resolve(strict=True)
    except OSError as exc:
        raise PrivateOverlayTakeoverError(
            "The private overlay index is unavailable."
        ) from exc
    if not path.is_file():
        raise PrivateOverlayTakeoverError("The private overlay index is not a file.")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise PrivateOverlayTakeoverError(
            "The private overlay index cannot be read."
        ) from exc
    if len(raw) > MAX_INDEX_BYTES:
        raise PrivateOverlayTakeoverError("The private overlay index exceeds 2 MiB.")
    try:
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_json_object_without_duplicate_keys,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PrivateOverlayTakeoverError(
            "The private overlay index is not valid UTF-8 JSON."
        ) from exc
    if not isinstance(payload, dict):
        raise PrivateOverlayTakeoverError(
            "The private overlay index must be an object."
        )
    required_keys = {"format", "schema_version", "source_revision", "overlays"}
    if set(payload) != required_keys:
        raise PrivateOverlayTakeoverError(
            "The private overlay index does not match schema version 1."
        )
    schema_version = payload.get("schema_version")
    if (
        payload.get("format") != INDEX_FORMAT
        or isinstance(schema_version, bool)
        or schema_version != INDEX_SCHEMA_VERSION
    ):
        raise PrivateOverlayTakeoverError(
            "The private overlay index format or schema version is unsupported."
        )
    source_revision = _required_string(
        payload,
        "source_revision",
        pattern=REVISION_PATTERN,
    )
    overlays = payload.get("overlays")
    if not isinstance(overlays, list) or not overlays or len(overlays) > MAX_OVERLAYS:
        raise PrivateOverlayTakeoverError(
            "The private overlay index must contain 1 through 500 overlay records."
        )
    index_dir = path.parent.resolve()
    records = tuple(
        _parse_record(entry, source_revision=source_revision, index_dir=index_dir)
        for entry in overlays
    )
    addon_ids = [record.addon_id for record in records]
    archive_names = [record.archive_filename.casefold() for record in records]
    if len(set(addon_ids)) != len(addon_ids):
        raise PrivateOverlayTakeoverError(
            "The private overlay index contains duplicate add-on records."
        )
    if len(set(archive_names)) != len(archive_names):
        raise PrivateOverlayTakeoverError(
            "The private overlay index contains duplicate archive names."
        )
    return PrivateOverlayIndex(
        path=path,
        sha256=hashlib.sha256(raw).hexdigest(),
        source_revision=source_revision,
        records=records,
    )


def select_private_overlay_records(
    index: PrivateOverlayIndex,
    addon_ids: Sequence[str],
) -> tuple[PrivateOverlayRecord, ...]:
    """Select explicit overlay IDs in operator-requested sequential order."""

    selected_ids = tuple(str(addon_id or "").strip() for addon_id in addon_ids)
    if not selected_ids or any(not addon_id for addon_id in selected_ids):
        raise PrivateOverlayTakeoverError("Select at least one overlay with --addon.")
    if len(set(selected_ids)) != len(selected_ids):
        raise PrivateOverlayTakeoverError("Each private overlay may be selected once.")
    records_by_id = {record.addon_id: record for record in index.records}
    if any(addon_id not in records_by_id for addon_id in selected_ids):
        raise PrivateOverlayTakeoverError(
            "The private overlay index does not contain every selected add-on."
        )
    return tuple(records_by_id[addon_id] for addon_id in selected_ids)


def _require_runtime_environment() -> None:
    for variable in ("CARLINA_HOME", "CARLINA_SECRETS_DIR"):
        if not str(os.environ.get(variable) or "").strip():
            raise PrivateOverlayTakeoverError(
                f"{variable} must be explicitly set for private overlay takeover."
            )
    if is_worker_process():
        raise PrivateOverlayTakeoverError(
            "Private overlay takeover must run in command role, never from a worker."
        )


@contextmanager
def _stopped_instance_lock(paths) -> Iterator[None]:
    """Hold the runtime lease and reject a live data-root owner."""

    lock = runtime_leader_lock(paths, timeout=0)
    try:
        lock.acquire()
    except FileLockTimeout as exc:
        raise PrivateOverlayTakeoverError(
            "Private overlay takeover requires a stopped Carlina instance."
        ) from exc
    try:
        leader = load_runtime_leader(paths) or {}
        raw_leader_pid = leader.get("pid")
        try:
            leader_pid = (
                int(raw_leader_pid) if isinstance(raw_leader_pid, (int, str)) else 0
            )
        except TypeError, ValueError:
            leader_pid = 0
        if leader_pid > 0 and process_is_running(leader_pid):
            raise PrivateOverlayTakeoverError(
                "Private overlay takeover refused a running Carlina data root."
            )
        yield
    finally:
        lock.release()


def _verify_runtime_paths(paths) -> None:
    expected_home = Path(str(os.environ["CARLINA_HOME"])).expanduser().resolve()
    expected_secrets = (
        Path(str(os.environ["CARLINA_SECRETS_DIR"])).expanduser().resolve()
    )
    if Path(paths.data_root).resolve() != expected_home:
        raise PrivateOverlayTakeoverError(
            "The managed package did not bind the requested CARLINA_HOME."
        )
    if Path(paths.secrets_dir).resolve() != expected_secrets:
        raise PrivateOverlayTakeoverError(
            "The managed package did not bind the requested CARLINA_SECRETS_DIR."
        )


def _admin_actor(actor_user_id: int) -> User:
    actor = db.session.get(User, actor_user_id)
    if actor is None or not bool(actor.is_admin):
        raise PrivateOverlayTakeoverError(
            "Private overlay takeover requires an existing administrator actor."
        )
    return actor


def _private_overlay_provenance(
    index: PrivateOverlayIndex,
    record: PrivateOverlayRecord,
) -> dict[str, Any]:
    return {
        "type": "private_addon_overlay_takeover",
        "source_revision": record.source_revision,
        "index_sha256": index.sha256,
        "archive_sha256": record.archive_sha256,
        "manifest_sha256": record.manifest_sha256,
        "manifest_file_sha256": record.manifest_file_sha256,
        "contract_sha256": record.contract_sha256,
        "requirements_lock_sha256": record.requirements_lock_sha256,
    }


def _verify_staged_installed_record(staged, record: PrivateOverlayRecord) -> None:
    contract = staged.contract
    if (
        contract.addon_id != record.addon_id
        or contract.version != record.version
        or contract.manifest_sha256 != record.manifest_sha256
        or contract.contract_sha256 != record.contract_sha256
    ):
        raise PrivateOverlayTakeoverError(
            "A staged private overlay does not match its reviewed index record."
        )
    manifest_path = Path(staged.code_dir) / "manifest.json"
    if (
        not manifest_path.is_file()
        or _file_sha256(manifest_path) != record.manifest_file_sha256
    ):
        raise PrivateOverlayTakeoverError(
            "A staged private overlay manifest file differs from its index record."
        )
    requirements_path = Path(staged.code_dir) / "requirements.lock"
    requirements_hash = (
        _file_sha256(requirements_path) if requirements_path.is_file() else None
    )
    if requirements_hash != record.requirements_lock_sha256:
        raise PrivateOverlayTakeoverError(
            "A staged private overlay requirements lock differs from its index record."
        )


def _explicit_reviewed_notice_ids(
    reviewed_notice_ids: Sequence[object],
) -> frozenset[str]:
    explicit: set[str] = set()
    for raw_notice_id in reviewed_notice_ids:
        if not isinstance(raw_notice_id, str):
            raise PrivateOverlayTakeoverError(
                "Every reviewed dependency notice ID must be a string."
            )
        notice_id = raw_notice_id.strip()
        if not notice_id or len(notice_id) > 128:
            raise PrivateOverlayTakeoverError(
                "Every reviewed dependency notice ID must be 1 through 128 characters."
            )
        explicit.add(notice_id)
    return frozenset(explicit)


def _required_reviewed_notice_ids(staged) -> tuple[str, ...]:
    """Validate raw manifest notices before accepting an acknowledgement."""

    manifest = staged.contract.to_dict(include_manifest=True).get("manifest")
    if not isinstance(manifest, dict):
        raise PrivateOverlayTakeoverError(
            "The staged private overlay does not expose a manifest for review."
        )
    notices = manifest.get("dependency_notices", [])
    if not isinstance(notices, list) or len(notices) > 50:
        raise PrivateOverlayTakeoverError(
            "The staged private overlay has invalid dependency notices."
        )
    required: set[str] = set()
    review_required_ids: set[str] = set()
    required_fields = {"id", "packages", "license", "purpose", "review_required"}
    allowed_fields = {*required_fields, "source_url"}
    for notice in notices:
        if (
            not isinstance(notice, dict)
            or not required_fields.issubset(notice)
            or not set(notice).issubset(allowed_fields)
        ):
            raise PrivateOverlayTakeoverError(
                "The staged private overlay has malformed dependency notices."
            )
        raw_notice_id = notice.get("id")
        if not isinstance(raw_notice_id, str):
            raise PrivateOverlayTakeoverError(
                "The staged private overlay has malformed dependency notices."
            )
        notice_id = raw_notice_id.strip()
        if not notice_id or len(notice_id) > 128 or notice_id in required:
            raise PrivateOverlayTakeoverError(
                "The staged private overlay has malformed dependency notices."
            )
        packages = notice.get("packages")
        if not isinstance(packages, list) or not packages or len(packages) > 50:
            raise PrivateOverlayTakeoverError(
                "The staged private overlay has malformed dependency notices."
            )
        if any(
            not isinstance(package, str)
            or not package.strip()
            or len(package.strip()) > 200
            for package in packages
        ):
            raise PrivateOverlayTakeoverError(
                "The staged private overlay has malformed dependency notices."
            )
        license_name = notice.get("license")
        purpose = notice.get("purpose")
        if (
            not isinstance(license_name, str)
            or not license_name.strip()
            or len(license_name.strip()) > 500
            or not isinstance(purpose, str)
            or not purpose.strip()
            or len(purpose.strip()) > 4_000
        ):
            raise PrivateOverlayTakeoverError(
                "The staged private overlay has malformed dependency notices."
            )
        source_url = notice.get("source_url", "")
        if not isinstance(source_url, str) or len(source_url.strip()) > 2_000:
            raise PrivateOverlayTakeoverError(
                "The staged private overlay has malformed dependency notices."
            )
        review_required = notice["review_required"]
        if not isinstance(review_required, bool):
            raise PrivateOverlayTakeoverError(
                "The staged private overlay has malformed dependency notices."
            )
        required.add(notice_id)
        if review_required:
            review_required_ids.add(notice_id)
    return tuple(sorted(review_required_ids))


def _migration_descriptor(contract) -> dict[str, Any] | None:
    payload = contract.to_dict()
    descriptor = payload.get("database_migration")
    return descriptor if isinstance(descriptor, dict) else None


def _require_exact_migration_identity(
    row: Addon,
    builtin_contract,
    staged_builtin,
    *,
    manager,
) -> None:
    builtin_descriptor = _migration_descriptor(builtin_contract)
    staged_descriptor = _migration_descriptor(staged_builtin)
    stored_payload = (
        row.resolved_contract_json
        if isinstance(row.resolved_contract_json, dict)
        else {}
    )
    stored_descriptor = stored_payload.get("database_migration")
    if (
        stored_descriptor != builtin_descriptor
        or staged_descriptor != builtin_descriptor
    ):
        raise PrivateOverlayTakeoverError(
            "The private overlay changes the reviewed built-in migration identity."
        )
    bundle = builtin_contract.migration_bundle
    if bundle is None:
        if row.alembic_revision is not None:
            raise PrivateOverlayTakeoverError(
                "The built-in row records migration state without a migration contract."
            )
        return
    if tuple(stored_migration_upgrade_issues(stored_descriptor, bundle)):
        raise PrivateOverlayTakeoverError(
            "The stored migration contract is not exactly compatible with the overlay."
        )
    if str(row.alembic_revision or "") != bundle.head_revision:
        raise PrivateOverlayTakeoverError(
            "The built-in add-on is not at its reviewed migration head."
        )
    try:
        physical_revision = manager._current_addon_db_revision_from_contract(bundle)
    except Exception as exc:
        raise PrivateOverlayTakeoverError(
            "The built-in add-on migration head cannot be verified."
        ) from exc
    if physical_revision != bundle.head_revision:
        raise PrivateOverlayTakeoverError(
            "The built-in add-on database is not at its reviewed migration head."
        )


def _compile_builtin_contract(staged):
    manifest = staged.contract.to_dict(include_manifest=True).get("manifest")
    if not isinstance(manifest, dict):
        raise PrivateOverlayTakeoverError(
            "The staged private overlay does not expose a manifest."
        )
    try:
        return compile_addon_contract(
            manifest,
            addon_dir=staged.code_dir,
            source_kind=AddonSourceKind.BUILT_IN,
            strict=False,
        )
    except Exception as exc:
        raise PrivateOverlayTakeoverError(
            "The staged overlay cannot prove its built-in contract identity."
        ) from exc


def _is_discovered_builtin_contract(contract: object) -> bool:
    """Return whether a manager-discovered contract still owns built-in code."""

    source_kind = getattr(contract, "source_kind", None)
    source_kind_value = getattr(source_kind, "value", source_kind)
    return source_kind_value == AddonSourceKind.BUILT_IN.value


def _has_current_code_removed_event(row: Addon) -> bool:
    """Return whether the row has exact journal proof of package code loss.

    The lifecycle journal is append-only, so the row's current generation is
    the boundary that prevents an old package-loss event from authorizing a
    later, unrelated inactive state.  Require an actual state change as well
    as the event name and target state; a same-state bookkeeping event is not
    proof of the package-code-missing transition.
    """

    event = (
        AddonLifecycleEvent.query.filter(
            AddonLifecycleEvent.addon_id == row.id,
            AddonLifecycleEvent.event == "package_code_missing",
            AddonLifecycleEvent.from_state != "code_removed",
            AddonLifecycleEvent.to_state == "code_removed",
            AddonLifecycleEvent.success.is_(True),
            AddonLifecycleEvent.generation == int(row.lifecycle_generation),
        )
        .order_by(AddonLifecycleEvent.id.desc())
        .first()
    )
    return event is not None


def _provenance_matches(
    index: PrivateOverlayIndex, record: PrivateOverlayRecord, row: Addon
) -> bool:
    provenance = row.provenance_json if isinstance(row.provenance_json, dict) else {}
    source = provenance.get("source") if isinstance(provenance, dict) else None
    if not isinstance(source, dict):
        return False
    expected = _private_overlay_provenance(index, record)
    return all(source.get(key) == value for key, value in expected.items())


def _is_exact_completed_overlay(
    index: PrivateOverlayIndex,
    record: PrivateOverlayRecord,
    row: Addon,
    paths,
) -> bool:
    generation = (
        Path(paths.installed_addons_dir) / record.addon_id / record.version
    ).resolve()
    try:
        generation.relative_to(Path(paths.installed_addons_dir).resolve())
    except ValueError:
        return False
    return (
        row.source_kind == AddonSourceKind.INSTALLED.value
        and bool(row.is_active)
        and row.version == record.version
        and row.manifest_sha256 == record.manifest_sha256
        and row.contract_sha256 == record.contract_sha256
        and generation.is_dir()
        and _provenance_matches(index, record, row)
    )


def _capture_snapshot(row: Addon) -> _AddonSnapshot:
    fields = (
        "version",
        "is_active",
        "alembic_revision",
        "source_kind",
        "trust_tier",
        "lifecycle_state",
        "lifecycle_generation",
        "lifecycle_error",
        "manifest_sha256",
        "contract_sha256",
        "resolved_contract_json",
        "provenance_json",
        "code_path",
        "installed_at",
        "lifecycle_updated_at",
    )
    return _AddonSnapshot(
        {field: copy.deepcopy(getattr(row, field)) for field in fields}
    )


def _takeover_result(
    record: PrivateOverlayRecord,
    index: PrivateOverlayIndex,
    *,
    state: str,
    source_kind: str,
    requires_restart: bool,
    recovery_action: str,
) -> PrivateOverlayTakeoverResult:
    return PrivateOverlayTakeoverResult(
        addon_id=record.addon_id,
        version=record.version,
        state=state,
        source_kind=source_kind,
        requires_restart=requires_restart,
        recovery_action=recovery_action,
        source_revision=record.source_revision,
        index_sha256=index.sha256,
        archive_sha256=record.archive_sha256,
        manifest_sha256=record.manifest_sha256,
        manifest_file_sha256=record.manifest_file_sha256,
        contract_sha256=record.contract_sha256,
        requirements_lock_sha256=record.requirements_lock_sha256,
    )


def _preflight_plan(
    index: PrivateOverlayIndex,
    record: PrivateOverlayRecord,
    staged,
    *,
    paths,
    manager,
    explicit_review_ids: frozenset[str],
    journal_overlay: dict[str, Any] | None = None,
) -> _TakeoverPlan:
    _verify_staged_installed_record(staged, record)
    reviewed_notice_ids = _required_reviewed_notice_ids(staged)
    if set(reviewed_notice_ids) - explicit_review_ids:
        raise PrivateOverlayTakeoverError(
            "Explicit review is required for every declared dependency notice."
        )
    get_contract = getattr(manager, "get_addon_contract", None)
    if not callable(get_contract):
        raise PrivateOverlayTakeoverError(
            "The managed package cannot prove that the built-in artifact was pruned."
        )
    discovered_contract = get_contract(record.addon_id)
    if discovered_contract is not None and _is_discovered_builtin_contract(
        discovered_contract
    ):
        raise PrivateOverlayTakeoverError(
            "The private built-in artifact is still discovered; prune it before takeover."
        )
    row = db.session.get(Addon, record.addon_id)
    if row is None:
        raise PrivateOverlayTakeoverError(
            "The selected private overlay has no existing built-in add-on row."
        )
    if _is_exact_completed_overlay(index, record, row, paths):
        return _TakeoverPlan(
            record,
            staged,
            None,
            None,
            reviewed_notice_ids,
            True,
        )
    phase = str((journal_overlay or {}).get("phase") or "")
    resuming_after_flip = (
        row.source_kind == AddonSourceKind.INSTALLED.value
        and phase
        in {
            "takeover_started",
            "source_kind_committed",
            "recovery_required",
        }
    )
    code_removed_recovery = (
        row.source_kind == AddonSourceKind.BUILT_IN.value
        and not row.is_active
        and row.lifecycle_state == "code_removed"
    )
    if row.source_kind != AddonSourceKind.BUILT_IN.value and not resuming_after_flip:
        raise PrivateOverlayTakeoverError(
            "Takeover is limited to an active built-in row, an exact code-removed recovery, or a matching recorded recovery."
        )
    if row.source_kind == AddonSourceKind.BUILT_IN.value and not row.is_active:
        if not code_removed_recovery:
            raise PrivateOverlayTakeoverError(
                "Takeover is limited to an active built-in add-on or an exact code-removed recovery."
            )
        if not _has_current_code_removed_event(row):
            raise PrivateOverlayTakeoverError(
                "The inactive code-removed built-in lacks a successful current-generation package-code-missing event."
            )
    builtin_contract = _compile_builtin_contract(staged)
    if (
        builtin_contract.addon_id != record.addon_id
        or builtin_contract.version != record.version
        or builtin_contract.manifest_sha256 != record.manifest_sha256
        or row.version != record.version
        or row.manifest_sha256 != builtin_contract.manifest_sha256
    ):
        raise PrivateOverlayTakeoverError(
            "The private overlay does not exactly match the active built-in identity."
        )
    expected_row_contracts = {builtin_contract.contract_sha256}
    if resuming_after_flip:
        expected_row_contracts.add(record.contract_sha256)
    if row.contract_sha256 not in expected_row_contracts:
        raise PrivateOverlayTakeoverError(
            "The private overlay does not exactly match the active built-in identity."
        )
    if journal_overlay is not None:
        recorded_plan = journal_overlay.get("plan")
        if (
            not isinstance(recorded_plan, dict)
            or recorded_plan.get("builtin_contract_sha256")
            != builtin_contract.contract_sha256
            or tuple(recorded_plan.get("reviewed_notice_ids") or ())
            != reviewed_notice_ids
        ):
            raise PrivateOverlayTakeoverError(
                "The private overlay journal plan no longer matches staged code."
            )
    if builtin_contract.distribution.channel.value != "private":
        raise PrivateOverlayTakeoverError(
            "Takeover is restricted to private built-in add-ons."
        )
    _require_exact_migration_identity(
        row,
        builtin_contract,
        builtin_contract,
        manager=manager,
    )
    return _TakeoverPlan(
        record,
        staged,
        builtin_contract,
        _capture_snapshot(row),
        reviewed_notice_ids,
        False,
        resuming_after_flip,
    )


def _journal_path(
    paths, index: PrivateOverlayIndex, records: Sequence[PrivateOverlayRecord]
) -> Path:
    selection_hash = hashlib.sha256(
        "\n".join(f"{record.addon_id}:{record.version}" for record in records).encode()
    ).hexdigest()[:16]
    root = (Path(paths.state_dir) / "private-addon-takeovers").resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root / f"{index.sha256[:16]}-{selection_hash}.json"


def _canonical_json_sha256(value: Any) -> str:
    try:
        raw = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("ascii")
    except (TypeError, ValueError) as exc:
        raise PrivateOverlayTakeoverError(
            "The private overlay takeover state contains non-serializable metadata."
        ) from exc
    return hashlib.sha256(raw).hexdigest()


def _redacted_snapshot(snapshot: _AddonSnapshot | None) -> dict[str, Any] | None:
    if snapshot is None:
        return None
    values = snapshot.values
    return {
        "version": str(values["version"]),
        "is_active": bool(values["is_active"]),
        "alembic_revision": values["alembic_revision"],
        "source_kind": str(values["source_kind"]),
        "trust_tier": str(values["trust_tier"]),
        "lifecycle_state": str(values["lifecycle_state"]),
        "lifecycle_generation": int(values["lifecycle_generation"]),
        "manifest_sha256": str(values["manifest_sha256"]),
        "contract_sha256": str(values["contract_sha256"]),
        "resolved_contract_sha256": _canonical_json_sha256(
            values["resolved_contract_json"]
        ),
        "provenance_sha256": _canonical_json_sha256(values["provenance_json"]),
        "code_path_sha256": hashlib.sha256(
            str(values["code_path"] or "").encode()
        ).hexdigest(),
    }


def _journal_plan(plan: _TakeoverPlan) -> dict[str, Any]:
    record = plan.record
    return {
        "source_revision": record.source_revision,
        "archive_sha256": record.archive_sha256,
        "manifest_sha256": record.manifest_sha256,
        "manifest_file_sha256": record.manifest_file_sha256,
        "requirements_lock_sha256": record.requirements_lock_sha256,
        "contract_sha256": record.contract_sha256,
        "builtin_contract_sha256": (
            plan.builtin_contract.contract_sha256
            if plan.builtin_contract is not None
            else None
        ),
        "reviewed_notice_ids": list(plan.reviewed_notice_ids),
    }


def _write_journal(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    root = path.parent.resolve()
    if path.resolve().parent != root:
        raise PrivateOverlayTakeoverError(
            "The private overlay journal path escapes its state directory."
        )
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    payload["updated_at"] = datetime.now(UTC).isoformat()
    encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if len(encoded) > MAX_JOURNAL_BYTES:
        raise PrivateOverlayTakeoverError("The private overlay journal exceeds 1 MiB.")
    try:
        with temporary.open("xb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def _validate_journal(
    payload: object,
    *,
    index: PrivateOverlayIndex,
    records: Sequence[PrivateOverlayRecord],
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise PrivateOverlayTakeoverError("The private overlay journal is invalid.")
    required = {
        "format",
        "version",
        "created_at",
        "updated_at",
        "index",
        "selection",
        "status",
        "backup",
        "overlays",
    }
    if set(payload) != required:
        raise PrivateOverlayTakeoverError(
            "The private overlay journal schema is invalid."
        )
    if (
        payload["format"] != TAKEOVER_JOURNAL_FORMAT
        or payload["version"] != TAKEOVER_JOURNAL_VERSION
        or payload["status"] not in _JOURNAL_STATUSES
        or any(
            not isinstance(payload[key], str) or len(payload[key]) > 64
            for key in ("created_at", "updated_at")
        )
    ):
        raise PrivateOverlayTakeoverError(
            "The private overlay journal header is invalid."
        )
    journal_index = payload["index"]
    if not isinstance(journal_index, dict) or journal_index != {
        "sha256": index.sha256,
        "source_revision": index.source_revision,
    }:
        raise PrivateOverlayTakeoverError(
            "The existing private overlay journal does not match this reviewed index."
        )
    expected_selection = [
        {"addon_id": record.addon_id, "version": record.version} for record in records
    ]
    if payload["selection"] != expected_selection:
        raise PrivateOverlayTakeoverError(
            "The existing private overlay journal does not match this selection."
        )
    overlays_payload = payload["overlays"]
    if not isinstance(overlays_payload, dict) or set(overlays_payload) != {
        record.addon_id for record in records
    }:
        raise PrivateOverlayTakeoverError(
            "The private overlay journal plans are invalid."
        )
    for record in records:
        entry = overlays_payload[record.addon_id]
        if not isinstance(entry, dict) or set(entry) != {
            "version",
            "phase",
            "recovery_action",
            "plan",
            "snapshot",
        }:
            raise PrivateOverlayTakeoverError(
                "The private overlay journal plan is invalid."
            )
        if (
            entry["version"] != record.version
            or entry["phase"] not in _JOURNAL_PHASES
            or not isinstance(entry["recovery_action"], str)
            or len(entry["recovery_action"]) > 128
        ):
            raise PrivateOverlayTakeoverError(
                "The private overlay journal phase is invalid."
            )
        plan = entry["plan"]
        expected_plan = {
            "source_revision": record.source_revision,
            "archive_sha256": record.archive_sha256,
            "manifest_sha256": record.manifest_sha256,
            "manifest_file_sha256": record.manifest_file_sha256,
            "requirements_lock_sha256": record.requirements_lock_sha256,
            "contract_sha256": record.contract_sha256,
        }
        if (
            not isinstance(plan, dict)
            or any(plan.get(key) != value for key, value in expected_plan.items())
            or set(plan)
            != {*expected_plan, "builtin_contract_sha256", "reviewed_notice_ids"}
            or not isinstance(plan["reviewed_notice_ids"], list)
            or len(plan["reviewed_notice_ids"]) > 50
            or any(
                not isinstance(item, str) or not item or len(item) > 128
                for item in plan["reviewed_notice_ids"]
            )
            or len(set(plan["reviewed_notice_ids"])) != len(plan["reviewed_notice_ids"])
            or (
                plan["builtin_contract_sha256"] is not None
                and (
                    not isinstance(plan["builtin_contract_sha256"], str)
                    or SHA256_PATTERN.fullmatch(plan["builtin_contract_sha256"]) is None
                )
            )
        ):
            raise PrivateOverlayTakeoverError(
                "The private overlay journal plan is invalid."
            )
        snapshot = entry["snapshot"]
        if snapshot is not None and (
            not isinstance(snapshot, dict)
            or set(snapshot)
            != {
                "version",
                "is_active",
                "alembic_revision",
                "source_kind",
                "trust_tier",
                "lifecycle_state",
                "lifecycle_generation",
                "manifest_sha256",
                "contract_sha256",
                "resolved_contract_sha256",
                "provenance_sha256",
                "code_path_sha256",
            }
            or not isinstance(snapshot["is_active"], bool)
            or not isinstance(snapshot["lifecycle_generation"], int)
            or snapshot["lifecycle_generation"] < 0
            or not isinstance(snapshot["alembic_revision"], (str, type(None)))
            or any(
                not isinstance(snapshot[key], str) or len(snapshot[key]) > 256
                for key in (
                    "version",
                    "source_kind",
                    "trust_tier",
                    "lifecycle_state",
                )
            )
            or any(
                not isinstance(snapshot[key], str)
                or SHA256_PATTERN.fullmatch(snapshot[key]) is None
                for key in (
                    "manifest_sha256",
                    "contract_sha256",
                    "resolved_contract_sha256",
                    "provenance_sha256",
                    "code_path_sha256",
                )
            )
        ):
            raise PrivateOverlayTakeoverError(
                "The private overlay journal snapshot is invalid."
            )
    backup = payload["backup"]
    if backup is not None and (
        not isinstance(backup, dict)
        or set(backup)
        != {"backup_id", "filename", "sha256", "file_count", "expanded_bytes"}
        or not isinstance(backup["backup_id"], str)
        or not backup["backup_id"]
        or len(backup["backup_id"]) > 128
        or not isinstance(backup["filename"], str)
        or Path(backup["filename"]).name != backup["filename"]
        or not isinstance(backup["sha256"], str)
        or SHA256_PATTERN.fullmatch(backup["sha256"]) is None
        or any(
            isinstance(backup[key], bool)
            or not isinstance(backup[key], int)
            or backup[key] < 0
            for key in ("file_count", "expanded_bytes")
        )
    ):
        raise PrivateOverlayTakeoverError(
            "The private overlay journal backup is invalid."
        )
    return payload


def _read_journal(
    path: Path,
    *,
    index: PrivateOverlayIndex,
    records: Sequence[PrivateOverlayRecord],
) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise PrivateOverlayTakeoverError(
            "The private overlay journal cannot be read."
        ) from exc
    if not raw or len(raw) > MAX_JOURNAL_BYTES:
        raise PrivateOverlayTakeoverError("The private overlay journal is invalid.")
    try:
        payload = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_json_object_without_duplicate_keys
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PrivateOverlayTakeoverError(
            "The private overlay journal is invalid."
        ) from exc
    return _validate_journal(payload, index=index, records=records)


def _existing_journal(
    paths,
    index: PrivateOverlayIndex,
    records: Sequence[PrivateOverlayRecord],
) -> tuple[Path, dict[str, Any] | None]:
    path = _journal_path(paths, index, records)
    root = path.parent
    candidates = list(root.glob("*.json"))
    if len(candidates) > MAX_JOURNAL_FILES:
        raise PrivateOverlayTakeoverError(
            "Too many private overlay journals require review."
        )
    requested_ids = {record.addon_id for record in records}
    for candidate in candidates:
        try:
            if candidate.is_symlink() or not candidate.is_file():
                raise ValueError("not a regular journal file")
            if candidate.stat().st_size > MAX_JOURNAL_BYTES:
                raise ValueError("journal exceeds size limit")
        except (OSError, ValueError) as exc:
            raise PrivateOverlayTakeoverError(
                "An existing private overlay journal requires manual review."
            ) from exc
        if candidate == path:
            continue
        try:
            raw = candidate.read_bytes()
            payload = json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=_json_object_without_duplicate_keys,
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PrivateOverlayTakeoverError(
                "An existing private overlay journal requires manual review."
            ) from exc
        selection = payload.get("selection") if isinstance(payload, dict) else None
        status = payload.get("status") if isinstance(payload, dict) else None
        candidate_ids = (
            tuple(
                item.get("addon_id")
                for item in selection
                if isinstance(item, dict) and isinstance(item.get("addon_id"), str)
            )
            if isinstance(selection, list)
            else ()
        )
        if status in _UNFINISHED_JOURNAL_STATUSES and (
            set(candidate_ids) & requested_ids
        ):
            raise PrivateOverlayTakeoverError(
                "A different unfinished private overlay journal already owns one or more selected add-ons."
            )
    if path.exists() and (path.is_symlink() or not path.is_file()):
        raise PrivateOverlayTakeoverError(
            "The private overlay journal requires manual review."
        )
    return (
        path,
        _read_journal(path, index=index, records=records) if path.exists() else None,
    )


def _backup_journal_entry(paths, backup) -> dict[str, Any]:
    try:
        path = Path(backup.path).resolve(strict=True)
        path.relative_to(Path(paths.backups_dir).resolve())
    except (AttributeError, OSError, ValueError) as exc:
        raise PrivateOverlayTakeoverError(
            "The safety backup was not published under the managed backups directory."
        ) from exc
    return {
        "backup_id": str(backup.backup_id),
        "filename": path.name,
        "sha256": str(backup.sha256),
        "file_count": int(backup.file_count),
        "expanded_bytes": int(backup.expanded_bytes),
    }


def _verify_journal_backup(paths, journal: dict[str, Any]) -> None:
    backup = journal.get("backup")
    if not isinstance(backup, dict):
        raise PrivateOverlayTakeoverError(
            "An unfinished private overlay journal lacks a verified safety backup."
        )
    try:
        path = (Path(paths.backups_dir) / backup["filename"]).resolve(strict=True)
        path.relative_to(Path(paths.backups_dir).resolve())
        if _file_sha256(path) != backup["sha256"]:
            raise ValueError("digest mismatch")
        manifest = verify_backup(
            path,
            available_key_ids=InstallationKeyStore(paths.master_keys_file).key_ids(),
        )
    except (BackupError, OSError, ValueError) as exc:
        raise PrivateOverlayTakeoverError(
            "The recorded private overlay safety backup is unavailable or invalid."
        ) from exc
    if (
        manifest.get("backup_id") != backup["backup_id"]
        or manifest.get("file_count") != backup["file_count"]
        or manifest.get("expanded_bytes") != backup["expanded_bytes"]
    ):
        raise PrivateOverlayTakeoverError(
            "The recorded private overlay safety backup does not match its journal."
        )


def _new_journal(
    index: PrivateOverlayIndex, plans: Sequence[_TakeoverPlan]
) -> dict[str, Any]:
    return {
        "format": TAKEOVER_JOURNAL_FORMAT,
        "version": TAKEOVER_JOURNAL_VERSION,
        "created_at": datetime.now(UTC).isoformat(),
        "updated_at": datetime.now(UTC).isoformat(),
        "index": {
            "sha256": index.sha256,
            "source_revision": index.source_revision,
        },
        "selection": [
            {"addon_id": plan.record.addon_id, "version": plan.record.version}
            for plan in plans
        ],
        "status": "preflighted",
        "backup": None,
        "overlays": {
            plan.record.addon_id: {
                "version": plan.record.version,
                "phase": "already_completed"
                if plan.already_completed
                else "preflighted",
                "recovery_action": "none",
                "plan": _journal_plan(plan),
                "snapshot": _redacted_snapshot(plan.addon_snapshot),
            }
            for plan in plans
        },
    }


def _retain_disabled_after_failure(
    manager, row: Addon, plan: _TakeoverPlan, actor_user_id: int
) -> None:
    row.source_kind = AddonSourceKind.INSTALLED.value
    row.is_active = False
    db.session.commit()
    manager.lifecycle.record_event(
        row,
        event="private_overlay_takeover_recovery_required",
        success=False,
        detail="Installed generation retained disabled for recovery review.",
        metadata={
            "archive_sha256": plan.record.archive_sha256,
            "contract_sha256": plan.record.contract_sha256,
        },
        actor_user_id=actor_user_id,
    )


def _after_source_kind_committed(_plan: _TakeoverPlan) -> None:
    """Crash-injection seam; production deliberately has no post-commit work here."""


def _safe_error_result(
    index: PrivateOverlayIndex,
    plan: _TakeoverPlan,
    *,
    state: str,
    source_kind: str,
    recovery_action: str,
) -> PrivateOverlayTakeoverResult:
    return _takeover_result(
        plan.record,
        index,
        state=state,
        source_kind=source_kind,
        requires_restart=True,
        recovery_action=recovery_action,
    )


def takeover_private_addon_overlays(
    index_path: str | Path,
    addon_ids: Sequence[str],
    *,
    actor_user_id: int,
    approve_private_host_code: bool,
    approve_private_takeover: bool,
    reviewed_notice_ids: Sequence[object] = (),
    app=None,
) -> tuple[PrivateOverlayTakeoverResult, ...]:
    """Preflight, back up, and take over selected exact private built-ins.

    Each successful package activates immediately.  Every selected package is
    staged and checked before the first source-kind flip; later packages are
    deliberately not attempted after a failure so the journal can name an
    unambiguous recovery action for every selected ID.
    """

    if approve_private_host_code is not True or approve_private_takeover is not True:
        raise PrivateOverlayTakeoverError(
            "Pass both private host-code and private takeover approvals."
        )
    index = load_private_overlay_index(index_path)
    records = select_private_overlay_records(index, addon_ids)
    if CONTACT_SCRAPING_ID in {record.addon_id for record in records}:
        raise PrivateOverlayTakeoverError(
            "contact_scraping is outside this private-overlay takeover safety boundary."
        )
    explicit_review_ids = _explicit_reviewed_notice_ids(reviewed_notice_ids)
    _require_runtime_environment()

    previous_role = os.environ.get("CARLINA_RUNTIME_ROLE")
    os.environ["CARLINA_RUNTIME_ROLE"] = "command"
    staged: list[Any] = []
    try:
        active_app = app or create_app()
        paths = active_app.extensions.get("carlina_paths")
        if paths is None:
            raise PrivateOverlayTakeoverError(
                "The managed package did not expose application paths."
            )
        _verify_runtime_paths(paths)
        manager = getattr(active_app, "addon_manager", None)
        if manager is None or not callable(
            getattr(manager, "lifecycle_maintenance_authority", None)
        ):
            raise PrivateOverlayTakeoverError(
                "The managed package cannot perform lifecycle maintenance."
            )

        with active_app.app_context(), _stopped_instance_lock(paths):
            _admin_actor(actor_user_id)
            journal_path, journal = _existing_journal(paths, index, records)
            if journal is not None and journal["backup"] is not None:
                _verify_journal_backup(paths, journal)
            installer = AddonPackageInstaller(paths)
            try:
                for record in records:
                    with record.archive_path.open("rb") as stream:
                        package = installer.stage(
                            stream,
                            original_filename=record.archive_filename,
                            operator_approved=True,
                            source_provenance=_private_overlay_provenance(
                                index, record
                            ),
                            expected_archive_sha256=record.archive_sha256,
                            expected_archive_size=record.archive_size,
                            expected_manifest_sha256=record.manifest_sha256,
                            expected_contract_sha256=record.contract_sha256,
                        )
                    staged.append(package)

                plans = tuple(
                    _preflight_plan(
                        index,
                        record,
                        package,
                        paths=paths,
                        manager=manager,
                        explicit_review_ids=explicit_review_ids,
                        journal_overlay=(
                            journal["overlays"][record.addon_id]
                            if journal is not None
                            else None
                        ),
                    )
                    for record, package in zip(records, staged, strict=True)
                )
                required_review_ids = {
                    notice_id
                    for plan in plans
                    for notice_id in plan.reviewed_notice_ids
                }
                if explicit_review_ids - required_review_ids:
                    raise PrivateOverlayTakeoverError(
                        "Every reviewed dependency notice ID must be required by a selected overlay."
                    )
                mutable_plans = [plan for plan in plans if not plan.already_completed]
                if journal is None:
                    journal = _new_journal(index, plans)
                    _write_journal(journal_path, journal)
                if mutable_plans and journal["backup"] is None:
                    mutable_phases = {
                        journal["overlays"][plan.record.addon_id]["phase"]
                        for plan in mutable_plans
                    }
                    if journal["status"] != "preflighted" or mutable_phases != {
                        "preflighted"
                    }:
                        raise PrivateOverlayTakeoverError(
                            "An unfinished private overlay journal lacks a verified safety backup."
                        )
                    try:
                        backup = create_backup(
                            paths,
                            key_store=InstallationKeyStore(paths.master_keys_file),
                        )
                    except (BackupError, OSError, ValueError) as exc:
                        raise PrivateOverlayTakeoverError(
                            "A fresh verified application safety backup is required."
                        ) from exc
                    journal["backup"] = _backup_journal_entry(paths, backup)
                    _write_journal(journal_path, journal)

                results: dict[str, PrivateOverlayTakeoverResult] = {}
                for plan in plans:
                    if plan.already_completed:
                        journal_entry = journal["overlays"][plan.record.addon_id]
                        if journal_entry["phase"] not in {
                            "activated",
                            "already_completed",
                        }:
                            journal_entry.update(
                                phase="activated",
                                recovery_action="none",
                            )
                            _write_journal(journal_path, journal)
                        results[plan.record.addon_id] = _takeover_result(
                            plan.record,
                            index,
                            state="already_activated",
                            source_kind=AddonSourceKind.INSTALLED.value,
                            requires_restart=True,
                            recovery_action="none",
                        )
                coordinator = AddonInstallationCoordinator(
                    session=db.session,
                    manager=manager,
                    installer=installer,
                    actor_user_id=actor_user_id,
                )
                with manager.lifecycle_maintenance_authority():
                    for position, plan in enumerate(plans):
                        if plan.already_completed:
                            continue
                        row = db.session.get(Addon, plan.record.addon_id)
                        if row is None or plan.addon_snapshot is None:
                            raise PrivateOverlayTakeoverError(
                                "A preflighted add-on row disappeared before takeover."
                            )
                        if plan.builtin_contract is None:
                            raise PrivateOverlayTakeoverError(
                                "A mutable takeover lost its verified built-in contract."
                            )
                        journal_entry = journal["overlays"][plan.record.addon_id]
                        phase = journal_entry["phase"]
                        if row.source_kind == AddonSourceKind.BUILT_IN.value:
                            if phase not in {
                                "preflighted",
                                "takeover_started",
                                "not_attempted",
                            }:
                                raise PrivateOverlayTakeoverError(
                                    "The takeover journal cannot reconcile the built-in row state."
                                )
                            if phase in {"preflighted", "not_attempted"}:
                                journal_entry["phase"] = "takeover_started"
                                _write_journal(journal_path, journal)
                                manager.lifecycle.record_event(
                                    row,
                                    event="private_overlay_takeover_started",
                                    metadata={
                                        "source_revision": plan.record.source_revision,
                                        "index_sha256": index.sha256,
                                        "archive_sha256": plan.record.archive_sha256,
                                        "builtin_contract_sha256": plan.builtin_contract.contract_sha256,
                                        "installed_contract_sha256": plan.record.contract_sha256,
                                        "lifecycle_state": plan.addon_snapshot.values[
                                            "lifecycle_state"
                                        ],
                                        "lifecycle_generation": plan.addon_snapshot.values[
                                            "lifecycle_generation"
                                        ],
                                    },
                                    actor_user_id=actor_user_id,
                                )
                            row.source_kind = AddonSourceKind.INSTALLED.value
                            row.is_active = False
                            db.session.commit()
                            _after_source_kind_committed(plan)
                            journal_entry["phase"] = "source_kind_committed"
                            _write_journal(journal_path, journal)
                        elif row.source_kind == AddonSourceKind.INSTALLED.value:
                            if phase not in {
                                "takeover_started",
                                "source_kind_committed",
                                "recovery_required",
                            }:
                                raise PrivateOverlayTakeoverError(
                                    "The takeover journal cannot reconcile the installed row state."
                                )
                            if row.is_active:
                                row.is_active = False
                                db.session.commit()
                                manager.lifecycle.record_event(
                                    row,
                                    event="private_overlay_takeover_resume_disabled",
                                    success=False,
                                    detail="Resuming a stopped private overlay takeover.",
                                    actor_user_id=actor_user_id,
                                )
                            if phase == "takeover_started":
                                journal_entry["phase"] = "source_kind_committed"
                                _write_journal(journal_path, journal)
                        else:
                            raise PrivateOverlayTakeoverError(
                                "The takeover row has an unsupported ownership state."
                            )
                        try:
                            outcome = coordinator.install(
                                plan.staged,
                                reviewed_notice_ids=plan.reviewed_notice_ids,
                                retain_published_on_failure=True,
                            )
                            if (
                                outcome.addon_id != plan.record.addon_id
                                or outcome.version != plan.record.version
                            ):
                                raise PrivateOverlayTakeoverError(
                                    "The installation coordinator returned an unexpected outcome."
                                )
                            completed = _takeover_result(
                                plan.record,
                                index,
                                state="activated",
                                source_kind=AddonSourceKind.INSTALLED.value,
                                requires_restart=bool(outcome.requires_restart),
                                recovery_action="none",
                            )
                            results[plan.record.addon_id] = completed
                            journal["overlays"][plan.record.addon_id].update(
                                phase="activated",
                                recovery_action="none",
                            )
                            completed_row = db.session.get(Addon, plan.record.addon_id)
                            if completed_row is not None:
                                manager.lifecycle.record_event(
                                    completed_row,
                                    event="private_overlay_takeover_completed",
                                    metadata={
                                        "index_sha256": index.sha256,
                                        "archive_sha256": plan.record.archive_sha256,
                                        "contract_sha256": plan.record.contract_sha256,
                                    },
                                    actor_user_id=actor_user_id,
                                )
                            _write_journal(journal_path, journal)
                        except Exception as exc:
                            row = db.session.get(Addon, plan.record.addon_id)
                            if row is None:
                                raise PrivateOverlayTakeoverError(
                                    "The takeover row disappeared during recovery."
                                ) from exc
                            _retain_disabled_after_failure(
                                manager,
                                row,
                                plan,
                                actor_user_id,
                            )
                            failed = _safe_error_result(
                                index,
                                plan,
                                state="recovery_required",
                                source_kind=AddonSourceKind.INSTALLED.value,
                                recovery_action="retain_installed_disabled",
                            )
                            results[plan.record.addon_id] = failed
                            journal["overlays"][plan.record.addon_id].update(
                                phase=failed.state,
                                recovery_action=failed.recovery_action,
                            )
                            for deferred in plans[position + 1 :]:
                                if deferred.record.addon_id in results:
                                    continue
                                skipped = (
                                    _takeover_result(
                                        deferred.record,
                                        index,
                                        state="already_activated",
                                        source_kind=AddonSourceKind.INSTALLED.value,
                                        requires_restart=True,
                                        recovery_action="none",
                                    )
                                    if deferred.already_completed
                                    else _safe_error_result(
                                        index,
                                        deferred,
                                        state="not_attempted",
                                        source_kind=AddonSourceKind.BUILT_IN.value,
                                        recovery_action="no_mutation",
                                    )
                                )
                                results[deferred.record.addon_id] = skipped
                                journal["overlays"][deferred.record.addon_id].update(
                                    phase=skipped.state,
                                    recovery_action=skipped.recovery_action,
                                )
                            journal["status"] = "partial_failure"
                            _write_journal(journal_path, journal)
                            raise PrivateOverlayTakeoverPartialError(
                                tuple(results[record.addon_id] for record in records)
                            ) from exc
                journal["status"] = "complete"
                _write_journal(journal_path, journal)
                return tuple(results[record.addon_id] for record in records)
            finally:
                for package in staged:
                    installer.cleanup_stage(package)
    except PrivateOverlayTakeoverError:
        raise
    except Exception as exc:
        raise PrivateOverlayTakeoverError("Private overlay takeover failed.") from exc
    finally:
        if previous_role is None:
            os.environ.pop("CARLINA_RUNTIME_ROLE", None)
        else:
            os.environ["CARLINA_RUNTIME_ROLE"] = previous_role


def takeover_result_payload(
    *,
    success: bool,
    state: str,
    outcomes: Sequence[PrivateOverlayTakeoverResult],
) -> dict[str, Any]:
    """Return the redacted operator-facing result shape used by the CLI."""

    return {
        "success": success,
        "state": state,
        "outcomes": [outcome.to_dict() for outcome in outcomes],
    }


__all__ = [
    "PrivateOverlayIndex",
    "PrivateOverlayRecord",
    "PrivateOverlayTakeoverError",
    "PrivateOverlayTakeoverPartialError",
    "PrivateOverlayTakeoverResult",
    "load_private_overlay_index",
    "select_private_overlay_records",
    "takeover_private_addon_overlays",
    "takeover_result_payload",
]
