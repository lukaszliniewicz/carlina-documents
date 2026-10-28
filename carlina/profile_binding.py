"""Bind a data root to one runtime profile before durable application writes."""

from __future__ import annotations

import json
import os
import re
import stat
import uuid
from pathlib import Path
from typing import cast

from carlina.runtime_profiles import (
    ContentPolicy,
    RootProfileBinding,
    RuntimeProfile,
    RuntimeProfileError,
)

_SCHEMA_VERSION = 1
_MAX_MARKER_BYTES = 4096
_MARKER_KEYS = frozenset({"schema_version", "profile", "content_policy"})
_TEMP_FILENAME = re.compile(r"\.profile\.[0-9a-f]{32}\.tmp\Z")
_MARKER_RELATIVE_PATH = Path("state") / "runtime" / "profile.json"


def _resolved_root(data_root: Path) -> Path:
    try:
        return data_root.resolve()
    except OSError, RuntimeError:
        raise RuntimeProfileError("Could not resolve the data root.") from None


def _marker_path_for_root(root: Path) -> Path:
    marker_path = root / _MARKER_RELATIVE_PATH
    try:
        marker_path.parent.relative_to(root)
        marker_path.relative_to(root)
    except ValueError:
        raise RuntimeProfileError(
            "Runtime profile marker path leaves the data root."
        ) from None
    return marker_path


def profile_marker_path(data_root: Path) -> Path:
    """Return the marker path beneath the once-resolved data root."""

    return _marker_path_for_root(_resolved_root(data_root))


def _lstat_or_none(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        raise RuntimeProfileError("Could not inspect the data root.") from None


def _require_directory(path: Path, info: os.stat_result, *, label: str) -> None:
    if stat.S_ISLNK(info.st_mode):
        raise RuntimeProfileError(f"The {label} directory cannot be a symlink.")
    if not stat.S_ISDIR(info.st_mode):
        raise RuntimeProfileError(f"The {label} path must be a directory.")


def _marker_if_present(root: Path) -> Path | None:
    marker_path = _marker_path_for_root(root)
    root_info = _lstat_or_none(root)
    if root_info is None:
        return None
    _require_directory(root, root_info, label="data root")

    state_path = root / "state"
    state_info = _lstat_or_none(state_path)
    if state_info is None:
        return None
    _require_directory(state_path, state_info, label="state")

    runtime_path = state_path / "runtime"
    runtime_info = _lstat_or_none(runtime_path)
    if runtime_info is None:
        return None
    _require_directory(runtime_path, runtime_info, label="runtime")

    marker_info = _lstat_or_none(marker_path)
    if marker_info is None:
        return None
    if stat.S_ISLNK(marker_info.st_mode):
        raise RuntimeProfileError("Runtime profile marker cannot be a symlink.")
    if not stat.S_ISREG(marker_info.st_mode):
        raise RuntimeProfileError("Runtime profile marker must be a regular file.")
    return marker_path


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _decode_marker(payload: bytes) -> RootProfileBinding:
    try:
        parsed: object = json.loads(
            payload.decode("utf-8"), object_pairs_hook=_unique_json_object
        )
    except UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError:
        raise RuntimeProfileError("Runtime profile marker is malformed.") from None

    if not isinstance(parsed, dict):
        raise RuntimeProfileError("Runtime profile marker must be a JSON object.")
    fields = cast(dict[str, object], parsed)
    if fields.keys() != _MARKER_KEYS:
        raise RuntimeProfileError("Runtime profile marker contains unsupported fields.")

    version = fields["schema_version"]
    if type(version) is not int or version != _SCHEMA_VERSION:
        raise RuntimeProfileError("Runtime profile marker version is unsupported.")

    raw_profile = fields["profile"]
    if not isinstance(raw_profile, str):
        raise RuntimeProfileError("Runtime profile marker profile is invalid.")
    try:
        profile = RuntimeProfile(raw_profile)
    except ValueError:
        raise RuntimeProfileError(
            "Runtime profile marker profile is unsupported."
        ) from None

    raw_policy = fields["content_policy"]
    if not isinstance(raw_policy, str):
        raise RuntimeProfileError("Runtime profile marker content policy is invalid.")
    try:
        content_policy = ContentPolicy(raw_policy)
    except ValueError:
        raise RuntimeProfileError(
            "Runtime profile marker content policy is unsupported."
        ) from None

    if (
        content_policy is ContentPolicy.PLAINTEXT_V1
        and profile is not RuntimeProfile.DOCUMENTS
    ):
        raise RuntimeProfileError(
            "Runtime profile marker content policy is incompatible."
        )

    return RootProfileBinding(
        profile=profile,
        content_policy=content_policy,
        legacy=False,
    )


def _read_marker_file(marker_path: Path) -> RootProfileBinding:
    try:
        descriptor = os.open(
            marker_path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
        )
    except OSError:
        raise RuntimeProfileError(
            "Could not read the runtime profile marker."
        ) from None

    try:
        marker_info = os.fstat(descriptor)
        if not stat.S_ISREG(marker_info.st_mode):
            raise RuntimeProfileError("Runtime profile marker must be a regular file.")
        if marker_info.st_size > _MAX_MARKER_BYTES:
            raise RuntimeProfileError("Runtime profile marker exceeds the size limit.")
        with os.fdopen(descriptor, "rb") as marker_file:
            descriptor = -1
            payload = marker_file.read(_MAX_MARKER_BYTES + 1)
        if len(payload) > _MAX_MARKER_BYTES:
            raise RuntimeProfileError("Runtime profile marker exceeds the size limit.")
        return _decode_marker(payload)
    except OSError:
        raise RuntimeProfileError(
            "Could not read the runtime profile marker."
        ) from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _read_at_root(root: Path) -> RootProfileBinding | None:
    marker_path = _marker_if_present(root)
    if marker_path is None:
        return None
    return _read_marker_file(marker_path)


def read_runtime_profile(data_root: Path) -> RootProfileBinding | None:
    """Read a validated root marker without creating or changing files."""

    return _read_at_root(_resolved_root(data_root))


def _matching_temporary_file(name: str) -> bool:
    return _TEMP_FILENAME.fullmatch(name) is not None


def _is_fresh_root(root: Path, marker_parent: Path) -> bool:
    root_info = _lstat_or_none(root)
    if root_info is None:
        return True
    _require_directory(root, root_info, label="data root")

    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    if entry.is_symlink():
                        return False
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(Path(entry.path))
                        continue
                    if (
                        directory == marker_parent
                        and _matching_temporary_file(entry.name)
                        and entry.is_file(follow_symlinks=False)
                    ):
                        continue
                    return False
        except OSError:
            raise RuntimeProfileError(
                "Could not inspect the data root for existing content."
            ) from None
    return True


def _ensure_marker_parent(root: Path) -> Path:
    marker_path = _marker_path_for_root(root)
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError:
        raise RuntimeProfileError("Could not prepare the data root.") from None

    root_info = _lstat_or_none(root)
    if root_info is None:
        raise RuntimeProfileError("Data root disappeared during setup.")
    _require_directory(root, root_info, label="data root")

    parent = root
    for name, label in (("state", "state"), ("runtime", "runtime")):
        parent = parent / name
        try:
            parent.mkdir(mode=0o700)
        except FileExistsError:
            pass
        except OSError:
            raise RuntimeProfileError(
                "Could not prepare the runtime profile directory."
            ) from None
        info = _lstat_or_none(parent)
        if info is None:
            raise RuntimeProfileError(
                "Runtime profile directory disappeared during setup."
            )
        _require_directory(parent, info, label=label)

    try:
        marker_path.parent.relative_to(root)
    except ValueError:
        raise RuntimeProfileError(
            "Runtime profile marker path leaves the data root."
        ) from None
    return marker_path


def _fresh_content_policy(profile: RuntimeProfile) -> ContentPolicy:
    if profile is RuntimeProfile.DOCUMENTS:
        return ContentPolicy.PLAINTEXT_V1
    return ContentPolicy.INSTALLATION_ENVELOPE_V2


def _serialized_marker(profile: RuntimeProfile) -> bytes:
    marker = {
        "schema_version": _SCHEMA_VERSION,
        "profile": profile.value,
        "content_policy": _fresh_content_policy(profile).value,
    }
    return json.dumps(marker, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def _matching_binding(
    binding: RootProfileBinding, requested: RuntimeProfile
) -> RootProfileBinding:
    if binding.profile is not requested:
        raise RuntimeProfileError(
            "Data root is already bound to another runtime profile."
        )
    return binding


def _publish_marker(
    root: Path, marker_path: Path, profile: RuntimeProfile
) -> RootProfileBinding | None:
    temporary_path = marker_path.parent / f".profile.{uuid.uuid4().hex}.tmp"
    descriptor: int | None = None
    temporary_created = False
    try:
        try:
            descriptor = os.open(
                temporary_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            temporary_created = True
        except OSError:
            raise RuntimeProfileError(
                "Could not create a unique runtime profile temporary file."
            ) from None

        with os.fdopen(descriptor, "wb") as temporary_file:
            descriptor = None
            temporary_file.write(_serialized_marker(profile))
            temporary_file.flush()
            os.fsync(temporary_file.fileno())

        try:
            os.link(temporary_path, marker_path)
        except FileExistsError:
            existing = _read_at_root(root)
            if existing is None:
                raise RuntimeProfileError(
                    "Runtime profile marker changed during publication."
                ) from None
            return _matching_binding(existing, profile)
        except OSError:
            raise RuntimeProfileError(
                "Atomic runtime profile publication is unsupported on this filesystem."
            ) from None
        return None
    except RuntimeProfileError:
        raise
    except OSError:
        raise RuntimeProfileError(
            "Could not prepare the runtime profile marker."
        ) from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary_created:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                raise RuntimeProfileError(
                    "Could not remove the runtime profile temporary file."
                ) from None


def _validated_binding(
    root: Path, profile: RuntimeProfile
) -> RootProfileBinding | None:
    marker_path = _marker_path_for_root(root)
    existing = _read_at_root(root)
    if existing is not None:
        return _matching_binding(existing, profile)

    fresh = _is_fresh_root(root, marker_path.parent)

    # Another initializer may have published its marker while this process
    # inspected a markerless root. A valid marker always wins classification.
    existing = _read_at_root(root)
    if existing is not None:
        return _matching_binding(existing, profile)

    if not fresh:
        if profile is RuntimeProfile.WORKBENCH:
            return RootProfileBinding(
                profile=RuntimeProfile.WORKBENCH,
                content_policy=ContentPolicy.INSTALLATION_ENVELOPE_V2,
                legacy=True,
            )
        raise RuntimeProfileError(
            "Markerless existing data roots are incompatible with the documents profile."
        )
    return None


def validate_runtime_profile(
    data_root: Path, profile: RuntimeProfile
) -> RootProfileBinding | None:
    """Validate a requested composition without writing even a fresh root marker."""

    return _validated_binding(_resolved_root(data_root), profile)


def bind_runtime_profile(
    data_root: Path, profile: RuntimeProfile
) -> RootProfileBinding:
    """Create a fresh root's marker or safely identify a markerless legacy root."""
    root = _resolved_root(data_root)
    binding = _validated_binding(root, profile)
    if binding is not None:
        return binding

    marker_path = _ensure_marker_parent(root)
    existing = _read_at_root(root)
    if existing is not None:
        return _matching_binding(existing, profile)

    raced_binding = _publish_marker(root, marker_path, profile)
    if raced_binding is not None:
        return raced_binding
    return RootProfileBinding(
        profile=profile,
        content_policy=_fresh_content_policy(profile),
        legacy=False,
    )


__all__ = [
    "bind_runtime_profile",
    "profile_marker_path",
    "read_runtime_profile",
    "validate_runtime_profile",
]
