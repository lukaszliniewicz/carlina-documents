"""One-time activation of the bundled Carlina Documents add-ons."""

from __future__ import annotations

import json
import os
import stat
import tempfile
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager, suppress
from pathlib import Path
from typing import Literal, Protocol, cast

from filelock import Timeout as FileLockTimeout

from carlina.runtime_profiles import RuntimeProfile
from iarf_assistant.core import runtime_ownership

SetupState = Literal["absent", "pending", "complete"]
_MarkerState = Literal["pending", "complete"]

_MARKER_FORMAT = "carlina.documents-setup"
_SCHEMA_VERSION = 1
_NOTICE_REVIEW_SOURCE = "bundled_documents_distribution"
_MARKER_LIMIT_BYTES = 16 * 1024
_ADDON_IDS = ("sentence_segmenter", "doc_processor", "translator")
_REQUIRED_NOTICES: dict[str, frozenset[str]] = {
    "sentence_segmenter": frozenset(),
    "doc_processor": frozenset({"pymupdf-agpl", "extract-msg-gpl"}),
    "translator": frozenset(),
}
_ERROR_CODES = frozenset(
    {
        "documents_setup_state_invalid",
        "documents_setup_state_io_failed",
        "documents_setup_busy",
        "documents_setup_unreviewed_notices",
        "documents_setup_activation_failed",
    }
)
_RuntimeLockFactory = Callable[..., AbstractContextManager[object]]
_runtime_lock_factory = cast(_RuntimeLockFactory, runtime_ownership.runtime_leader_lock)


class DocumentsSetupError(RuntimeError):
    """A fixed, non-sensitive Documents setup failure."""

    def __init__(self, code: str, addon_id: str | None = None) -> None:
        if code not in _ERROR_CODES:
            code = "documents_setup_state_invalid"
        if addon_id not in _ADDON_IDS:
            addon_id = None
        self.code = code
        self.addon_id = addon_id
        super().__init__(code if addon_id is None else f"{code}:{addon_id}")


class DocumentsSetupPaths(Protocol):
    """Read-only subset of the application paths needed by setup."""

    @property
    def data_root(self) -> Path: ...

    @property
    def state_dir(self) -> Path: ...


class DocumentsSetupManager(Protocol):
    """Native lifecycle surface used to activate the bundled add-ons."""

    @property
    def loaded_addons(self) -> Mapping[str, object]: ...

    def get_addon_dependency_notices(self, addon_id: str) -> object: ...

    def lifecycle_maintenance_authority(self) -> AbstractContextManager[None]: ...

    def activate_addon(
        self, addon_id: str, *, dependency_review: list[str] | None = None
    ) -> None: ...


def inspect_documents_setup(paths: DocumentsSetupPaths) -> SetupState:
    """Read and validate the setup marker without creating files or acquiring locks."""

    try:
        return _read_marker_state(paths)
    except DocumentsSetupError:
        raise
    except OSError:
        raise DocumentsSetupError("documents_setup_state_io_failed") from None


def begin_documents_setup(
    paths: DocumentsSetupPaths,
    *,
    profile: RuntimeProfile,
    has_existing_accounts: bool,
) -> bool:
    """Create the pending marker for a new Documents root with no accounts."""

    if profile is not RuntimeProfile.DOCUMENTS or has_existing_accounts:
        return False

    try:
        with _runtime_lock_factory(paths, timeout=0):
            if _read_marker_state(paths) != "absent":
                return False
            _write_marker(paths, _pending_payload(), expected_state="absent")
            return True
    except FileLockTimeout:
        raise DocumentsSetupError("documents_setup_busy") from None
    except DocumentsSetupError:
        raise
    except OSError:
        raise DocumentsSetupError("documents_setup_state_io_failed") from None


def complete_documents_setup(
    paths: DocumentsSetupPaths,
    *,
    profile: RuntimeProfile,
    has_administrator: bool,
    manager: DocumentsSetupManager,
    on_product: Callable[[str], None] | None = None,
) -> bool:
    """Activate every bundled product once a pending Documents setup is approved."""

    if profile is not RuntimeProfile.DOCUMENTS or not has_administrator:
        return False

    try:
        # Older roots and finished setup must remain usable even while their
        # runtime owns the leader lock. Only pending setup needs mutation rights.
        if _read_marker_state(paths) != "pending":
            return False
        with _runtime_lock_factory(paths, timeout=0):
            state = _read_marker_state(paths)
            if state != "pending":
                return False
            reviewed_notices = _validated_notice_sets(manager)
            active_addon_id = _ADDON_IDS[0]
            try:
                if not _is_mapping(manager.loaded_addons):
                    raise DocumentsSetupError(
                        "documents_setup_activation_failed", active_addon_id
                    )
                with manager.lifecycle_maintenance_authority():
                    for active_addon_id in _ADDON_IDS:
                        if on_product is not None:
                            on_product(active_addon_id)
                        manager.activate_addon(
                            active_addon_id,
                            dependency_review=sorted(
                                _REQUIRED_NOTICES[active_addon_id]
                            ),
                        )
                    loaded_addons: object = manager.loaded_addons
                    for active_addon_id in _ADDON_IDS:
                        if not _mapping_contains(loaded_addons, active_addon_id):
                            raise DocumentsSetupError(
                                "documents_setup_activation_failed", active_addon_id
                            )
            except DocumentsSetupError:
                raise
            except Exception:
                # Native callbacks may contain private exception details.
                raise DocumentsSetupError(
                    "documents_setup_activation_failed", active_addon_id
                ) from None

            payload = _complete_payload(reviewed_notices)
            _write_marker(paths, payload, expected_state="pending")
            return True
    except FileLockTimeout:
        raise DocumentsSetupError("documents_setup_busy") from None
    except DocumentsSetupError:
        raise
    except OSError:
        raise DocumentsSetupError("documents_setup_state_io_failed") from None


def _marker_path(paths: DocumentsSetupPaths) -> Path:
    return paths.state_dir / "bootstrap" / "documents-products.json"


def _read_marker_state(paths: DocumentsSetupPaths) -> SetupState:
    marker_path = _marker_path(paths)
    state_dir = paths.state_dir

    state_dir_info = _lstat_or_none(state_dir)
    if state_dir_info is None:
        return "absent"
    if stat.S_ISLNK(state_dir_info.st_mode) or not stat.S_ISDIR(state_dir_info.st_mode):
        raise DocumentsSetupError("documents_setup_state_invalid")

    bootstrap_dir = marker_path.parent
    bootstrap_info = _lstat_or_none(bootstrap_dir)
    if bootstrap_info is None:
        return "absent"
    if stat.S_ISLNK(bootstrap_info.st_mode) or not stat.S_ISDIR(bootstrap_info.st_mode):
        raise DocumentsSetupError("documents_setup_state_invalid")

    marker_info = _lstat_or_none(marker_path)
    if marker_info is None:
        return "absent"
    if stat.S_ISLNK(marker_info.st_mode) or not stat.S_ISREG(marker_info.st_mode):
        raise DocumentsSetupError("documents_setup_state_invalid")

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(marker_path, flags)
    except OSError:
        raise DocumentsSetupError("documents_setup_state_io_failed") from None
    try:
        opened_info = os.fstat(descriptor)
        if not stat.S_ISREG(opened_info.st_mode) or (
            opened_info.st_dev,
            opened_info.st_ino,
        ) != (marker_info.st_dev, marker_info.st_ino):
            raise DocumentsSetupError("documents_setup_state_invalid")
        marker_bytes = _read_bounded(descriptor)
    finally:
        os.close(descriptor)

    return _parse_marker(marker_bytes)


def _read_bounded(descriptor: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while total <= _MARKER_LIMIT_BYTES:
        chunk = os.read(descriptor, min(4096, _MARKER_LIMIT_BYTES + 1 - total))
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        total += len(chunk)
    raise DocumentsSetupError("documents_setup_state_invalid")


def _parse_marker(marker_bytes: bytes) -> SetupState:
    try:
        value = json.loads(
            marker_bytes.decode("utf-8"),
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except UnicodeError, ValueError, json.JSONDecodeError, RecursionError:
        raise DocumentsSetupError("documents_setup_state_invalid") from None

    if type(value) is not dict:
        raise DocumentsSetupError("documents_setup_state_invalid")
    marker = cast(dict[str, object], value)
    if (
        marker.get("format") != _MARKER_FORMAT
        or type(marker.get("schema_version")) is not int
        or marker.get("schema_version") != _SCHEMA_VERSION
        or marker.get("addon_ids") != list(_ADDON_IDS)
    ):
        raise DocumentsSetupError("documents_setup_state_invalid")

    state = marker.get("state")
    if state == "pending":
        if set(marker) != {"format", "schema_version", "state", "addon_ids"}:
            raise DocumentsSetupError("documents_setup_state_invalid")
        return "pending"
    if state == "complete":
        if set(marker) != {
            "format",
            "schema_version",
            "state",
            "addon_ids",
            "notice_review_source",
            "reviewed_dependency_notices",
        }:
            raise DocumentsSetupError("documents_setup_state_invalid")
        if marker.get("notice_review_source") != _NOTICE_REVIEW_SOURCE:
            raise DocumentsSetupError("documents_setup_state_invalid")
        _validate_completed_notice_map(marker.get("reviewed_dependency_notices"))
        return "complete"
    raise DocumentsSetupError("documents_setup_state_invalid")


def _validate_completed_notice_map(value: object) -> None:
    if type(value) is not dict:
        raise DocumentsSetupError("documents_setup_state_invalid")
    notice_map = cast(dict[str, object], value)
    if set(notice_map) != set(_ADDON_IDS):
        raise DocumentsSetupError("documents_setup_state_invalid")
    for addon_id in _ADDON_IDS:
        notices = notice_map.get(addon_id)
        expected = sorted(_REQUIRED_NOTICES[addon_id])
        if type(notices) is not list:
            raise DocumentsSetupError("documents_setup_state_invalid")
        notice_ids = cast(list[object], notices)
        if any(type(notice_id) is not str for notice_id in notice_ids):
            raise DocumentsSetupError("documents_setup_state_invalid")
        if notice_ids != expected:
            raise DocumentsSetupError("documents_setup_state_invalid")


def _validated_notice_sets(
    manager: DocumentsSetupManager,
) -> dict[str, list[str]]:
    reviewed: dict[str, list[str]] = {}
    for addon_id in _ADDON_IDS:
        try:
            raw_notices = manager.get_addon_dependency_notices(addon_id)
        except Exception:
            raise DocumentsSetupError("documents_setup_unreviewed_notices") from None
        if type(raw_notices) is not list:
            raise DocumentsSetupError("documents_setup_unreviewed_notices")
        notices = cast(list[object], raw_notices)

        seen_ids: set[str] = set()
        required_ids: set[str] = set()
        for raw_notice in notices:
            if type(raw_notice) is not dict:
                raise DocumentsSetupError("documents_setup_unreviewed_notices")
            notice = cast(dict[str, object], raw_notice)
            notice_id = notice.get("id")
            review_required = notice.get("review_required")
            if (
                type(notice_id) is not str
                or not notice_id
                or type(review_required) is not bool
                or notice_id in seen_ids
            ):
                raise DocumentsSetupError("documents_setup_unreviewed_notices")
            seen_ids.add(notice_id)
            if review_required:
                required_ids.add(notice_id)

        if required_ids != set(_REQUIRED_NOTICES[addon_id]):
            raise DocumentsSetupError("documents_setup_unreviewed_notices")
        reviewed[addon_id] = sorted(required_ids)
    return reviewed


def _is_mapping(value: object) -> bool:
    return isinstance(value, Mapping)


def _mapping_contains(value: object, key: str) -> bool:
    if not isinstance(value, Mapping):
        return False
    mapping = cast(Mapping[object, object], value)
    return key in mapping


def _pending_payload() -> dict[str, object]:
    return {
        "format": _MARKER_FORMAT,
        "schema_version": _SCHEMA_VERSION,
        "state": "pending",
        "addon_ids": list(_ADDON_IDS),
    }


def _complete_payload(reviewed_notices: dict[str, list[str]]) -> dict[str, object]:
    return {
        "format": _MARKER_FORMAT,
        "schema_version": _SCHEMA_VERSION,
        "state": "complete",
        "addon_ids": list(_ADDON_IDS),
        "notice_review_source": _NOTICE_REVIEW_SOURCE,
        "reviewed_dependency_notices": reviewed_notices,
    }


def _write_marker(
    paths: DocumentsSetupPaths,
    payload: dict[str, object],
    *,
    expected_state: SetupState,
) -> None:
    marker_path = _marker_path(paths)
    _ensure_directory_tree(marker_path.parent)
    if _read_marker_state(paths) != expected_state:
        raise DocumentsSetupError("documents_setup_state_invalid")

    serialized = (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{marker_path.name}.", suffix=".tmp", dir=marker_path.parent
    )
    temporary_path = Path(temporary_name)
    temporary_info: os.stat_result | None = None
    try:
        temporary_info = os.fstat(descriptor)
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            descriptor = -1
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        if _read_marker_state(paths) != expected_state:
            raise DocumentsSetupError("documents_setup_state_invalid")
        os.replace(temporary_path, marker_path)
    except DocumentsSetupError:
        raise
    except OSError:
        raise DocumentsSetupError("documents_setup_state_io_failed") from None
    finally:
        if descriptor >= 0:
            with suppress(OSError):
                os.close(descriptor)
        if temporary_info is not None:
            _remove_temporary_if_owned(temporary_path, temporary_info)


def _ensure_directory_tree(path: Path) -> None:
    absolute_path = Path(os.path.abspath(path))
    ancestors = list(reversed((absolute_path, *absolute_path.parents)))
    for directory in ancestors:
        try:
            info = directory.lstat()
        except FileNotFoundError:
            with suppress(FileExistsError):
                directory.mkdir(mode=0o700)
            info = directory.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise DocumentsSetupError("documents_setup_state_invalid")


def _lstat_or_none(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


def _remove_temporary_if_owned(path: Path, owned_info: os.stat_result) -> None:
    try:
        current_info = path.lstat()
    except FileNotFoundError:
        return
    except OSError:
        return
    if (current_info.st_dev, current_info.st_ino) == (
        owned_info.st_dev,
        owned_info.st_ino,
    ):
        with suppress(OSError):
            path.unlink()


def _object_without_duplicate_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    del value
    raise ValueError("invalid JSON constant")


__all__ = [
    "DocumentsSetupError",
    "DocumentsSetupManager",
    "DocumentsSetupPaths",
    "SetupState",
    "begin_documents_setup",
    "complete_documents_setup",
    "inspect_documents_setup",
]
