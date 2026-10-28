"""Read locked package inventories for public Documents release review."""

from __future__ import annotations

import hashlib
import os
import re
import shlex
import stat
import unicodedata
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast
from urllib.parse import urlsplit

import yaml
from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import InvalidName, canonicalize_name
from packaging.version import InvalidVersion, Version

Ecosystem = Literal["conda", "pypi"]

_MAX_LOCK_BYTES = 16 * 1024 * 1024
_PIXI_LOCK_VERSION = 7
_HEX_SHA256 = re.compile(r"[0-9a-fA-F]{64}\Z")
_PIP_SHA256 = re.compile(r"sha256:([0-9a-fA-F]{64})\Z")
_CONDA_NAME = re.compile(r"[a-z0-9_][a-z0-9_.-]*\Z")
_READ_ERROR = "inventory input could not be read"
_REGULAR_FILE_ERROR = "inventory input must be a regular file"
_SIZE_ERROR = "inventory input exceeds the size limit"
_UTF8_ERROR = "inventory input is not UTF-8"
_PIXILOCK_ERROR = "invalid Pixi lock"
_ENVIRONMENT_ERROR = "Pixi environment is unavailable"
_PLATFORM_ERROR = "Pixi platform is unavailable"
_REFERENCE_ERROR = "invalid selected package reference"
_PACKAGE_ERROR = "invalid selected package record"
_REQUIREMENTS_ERROR = "invalid hash-locked requirements input"


class InventoryError(ValueError):
    """Raised for invalid inventory inputs without disclosing their contents."""


@dataclass(frozen=True, slots=True)
class LockedPackage:
    ecosystem: Ecosystem
    name: str
    version: str | None
    locator: str | None
    sha256: tuple[str, ...]
    license_expression: str | None
    build: str | None
    subdir: str | None
    local: bool = False


@dataclass(frozen=True, slots=True)
class LockInventory:
    lock_sha256: str
    packages: tuple[LockedPackage, ...]


def _close_descriptor(descriptor: int) -> None:
    with suppress(OSError):
        os.close(descriptor)


def _open_regular_descriptor(path: Path) -> tuple[int, os.stat_result]:
    try:
        initial_status = path.lstat()
    except OSError, ValueError:
        raise InventoryError(_READ_ERROR) from None
    if not stat.S_ISREG(initial_status.st_mode):
        raise InventoryError(_REGULAR_FILE_ERROR)

    flags = os.O_RDONLY
    flags |= cast(int, getattr(os, "O_CLOEXEC", 0))
    flags |= cast(int, getattr(os, "O_NOFOLLOW", 0))
    try:
        descriptor = os.open(path, flags)
    except OSError, ValueError:
        raise InventoryError(_READ_ERROR) from None

    try:
        opened_status = os.fstat(descriptor)
    except OSError:
        _close_descriptor(descriptor)
        raise InventoryError(_READ_ERROR) from None
    if not stat.S_ISREG(opened_status.st_mode) or (
        opened_status.st_dev,
        opened_status.st_ino,
    ) != (initial_status.st_dev, initial_status.st_ino):
        _close_descriptor(descriptor)
        raise InventoryError(_REGULAR_FILE_ERROR)
    return descriptor, opened_status


def _read_bounded_bytes(descriptor: int, initial_size: int) -> bytes:
    if initial_size > _MAX_LOCK_BYTES:
        raise InventoryError(_SIZE_ERROR)
    content = bytearray()
    while True:
        remaining = _MAX_LOCK_BYTES + 1 - len(content)
        chunk = os.read(descriptor, min(64 * 1024, remaining))
        if not chunk:
            return bytes(content)
        content.extend(chunk)
        if len(content) > _MAX_LOCK_BYTES:
            raise InventoryError(_SIZE_ERROR)


def _verify_open_path(path: Path, opened_status: os.stat_result) -> None:
    try:
        final_status = path.lstat()
    except OSError, ValueError:
        raise InventoryError(_READ_ERROR) from None
    if not stat.S_ISREG(final_status.st_mode) or (
        final_status.st_dev,
        final_status.st_ino,
    ) != (opened_status.st_dev, opened_status.st_ino):
        raise InventoryError(_REGULAR_FILE_ERROR)


def _read_utf8(path: Path) -> tuple[bytes, str]:
    """Read one bounded, regular, non-symlink file without following its leaf."""
    descriptor, opened_status = _open_regular_descriptor(path)
    try:
        try:
            data = _read_bounded_bytes(descriptor, opened_status.st_size)
            _verify_open_path(path, opened_status)
        except OSError:
            raise InventoryError(_READ_ERROR) from None
    finally:
        _close_descriptor(descriptor)

    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise InventoryError(_UTF8_ERROR) from None
    return data, text


def _object_mapping(value: object, message: str) -> dict[object, object]:
    if not isinstance(value, dict):
        raise InventoryError(message)
    return cast(dict[object, object], value)


def _nonempty_string(value: object, message: str) -> str:
    if not isinstance(value, str) or not value or not value.strip():
        raise InventoryError(message)
    return value


def _optional_string(
    record: dict[object, object], field: str, message: str
) -> str | None:
    value = record.get(field)
    if value is None:
        return None
    if not isinstance(value, str):
        raise InventoryError(message)
    return value


def _package_name(value: object, ecosystem: Ecosystem) -> str:
    name = _nonempty_string(value, _PACKAGE_ERROR)
    if any(character.isspace() for character in name):
        raise InventoryError(_PACKAGE_ERROR)
    if ecosystem == "pypi":
        try:
            return canonicalize_name(name, validate=True)
        except InvalidName:
            raise InventoryError(_PACKAGE_ERROR) from None
    return name


def _package_version(
    record: dict[object, object], ecosystem: Ecosystem, *, local: bool
) -> str | None:
    if "version" not in record or record.get("version") is None:
        if local:
            return None
        raise InventoryError(_PACKAGE_ERROR)

    version = record.get("version")
    if (
        not isinstance(version, str)
        or not version
        or any(character.isspace() for character in version)
    ):
        raise InventoryError(_PACKAGE_ERROR)
    if ecosystem == "pypi":
        try:
            Version(version)
        except InvalidVersion:
            raise InventoryError(_PACKAGE_ERROR) from None
    return version


def _package_hashes(record: dict[object, object]) -> tuple[str, ...]:
    value = record.get("sha256")
    if value is None:
        return ()
    if not isinstance(value, str) or _HEX_SHA256.fullmatch(value) is None:
        raise InventoryError(_PACKAGE_ERROR)
    return (value.lower(),)


def _has_control_character(value: str) -> bool:
    return any(unicodedata.category(character) == "Cc" for character in value)


def _conda_identity(path: str, platform: str) -> tuple[str, str, str, str]:
    if (
        not path
        or "%" in path
        or "\\" in path
        or any(character.isspace() for character in path)
        or _has_control_character(path)
    ):
        raise InventoryError(_REFERENCE_ERROR)

    path_parts = path.split("/")
    if len(path_parts) < 2:
        raise InventoryError(_REFERENCE_ERROR)
    subdir = path_parts[-2]
    if subdir not in {"noarch", platform}:
        raise InventoryError(_REFERENCE_ERROR)

    filename = path_parts[-1]
    if filename.endswith(".tar.bz2"):
        stem = filename[: -len(".tar.bz2")]
    elif filename.endswith(".conda"):
        stem = filename[: -len(".conda")]
    else:
        raise InventoryError(_REFERENCE_ERROR)
    identity = stem.rsplit("-", 2)
    if len(identity) != 3:
        raise InventoryError(_REFERENCE_ERROR)
    name, version, build = identity
    for component in identity:
        if (
            not component
            or any(character.isspace() for character in component)
            or _has_control_character(component)
            or any(character in "%/\\" for character in component)
        ):
            raise InventoryError(_REFERENCE_ERROR)
    if _CONDA_NAME.fullmatch(name) is None:
        raise InventoryError(_REFERENCE_ERROR)
    return name, version, build, subdir


def _validate_remote_locator(locator: str, ecosystem: Ecosystem, platform: str) -> None:
    if (
        any(character.isspace() for character in locator)
        or _has_control_character(locator)
        or "\\" in locator
    ):
        raise InventoryError(_REFERENCE_ERROR)
    try:
        parsed = urlsplit(locator)
        hostname = parsed.hostname
        has_credentials = parsed.username is not None or parsed.password is not None
    except ValueError:
        raise InventoryError(_REFERENCE_ERROR) from None
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or hostname is None
        or has_credentials
        or not parsed.path
        or parsed.path.endswith("/")
        or "?" in locator
        or "#" in locator
    ):
        raise InventoryError(_REFERENCE_ERROR)
    if ecosystem == "conda":
        _conda_identity(parsed.path, platform)


def _read_pixi_payload(text: str) -> object:
    try:
        payload: object = yaml.safe_load(text)
    except yaml.YAMLError, ValueError, RecursionError:
        raise InventoryError(_PIXILOCK_ERROR) from None
    return payload


def _selected_platform_is_declared(value: object, platform: str) -> bool:
    if not isinstance(value, list):
        raise InventoryError(_PIXILOCK_ERROR)
    for raw_platform in cast(list[object], value):
        if isinstance(raw_platform, dict):
            platform_record = cast(dict[object, object], raw_platform)
            if platform_record.get("name") == platform:
                return True
    return False


def _selected_references(
    value: object, platform: str
) -> tuple[tuple[Ecosystem, str, bool], ...]:
    if not isinstance(value, list):
        raise InventoryError(_REFERENCE_ERROR)

    selected: list[tuple[Ecosystem, str, bool]] = []
    seen: set[tuple[Ecosystem, str]] = set()
    for raw_reference in cast(list[object], value):
        reference = _object_mapping(raw_reference, _REFERENCE_ERROR)
        if len(reference) != 1:
            raise InventoryError(_REFERENCE_ERROR)
        kind = next(iter(reference))
        if not isinstance(kind, str) or kind not in {"conda", "pypi"}:
            raise InventoryError(_REFERENCE_ERROR)
        ecosystem = cast(Ecosystem, kind)
        locator = _nonempty_string(reference[kind], _REFERENCE_ERROR)
        local = ecosystem == "pypi" and locator in {".", "./"}
        if not local:
            _validate_remote_locator(locator, ecosystem, platform)
        identity = (ecosystem, locator)
        if identity in seen:
            raise InventoryError(_REFERENCE_ERROR)
        seen.add(identity)
        selected.append((ecosystem, locator, local))
    return tuple(selected)


def _matching_record(
    packages: list[object], ecosystem: Ecosystem, locator: str
) -> dict[object, object]:
    matches: list[dict[object, object]] = []
    for raw_record in packages:
        if not isinstance(raw_record, dict):
            continue
        record = cast(dict[object, object], raw_record)
        if record.get(ecosystem) == locator:
            matches.append(record)
    if len(matches) != 1:
        raise InventoryError(_PACKAGE_ERROR)
    return matches[0]


def _package_from_record(
    record: dict[object, object],
    ecosystem: Ecosystem,
    locator: str,
    platform: str,
    *,
    local: bool,
) -> LockedPackage:
    license_expression = _optional_string(record, "license", _PACKAGE_ERROR)
    hashes = _package_hashes(record)
    if ecosystem == "conda":
        name, version, build, subdir = _conda_identity(urlsplit(locator).path, platform)
        for field, expected in (
            ("name", name),
            ("version", version),
            ("build", build),
            ("subdir", subdir),
        ):
            if field in record:
                explicit_value = record[field]
                if not isinstance(explicit_value, str) or not explicit_value:
                    raise InventoryError(_PACKAGE_ERROR)
                if explicit_value != expected:
                    raise InventoryError(_PACKAGE_ERROR)
    else:
        name = _package_name(record.get("name"), ecosystem)
        version = _package_version(record, ecosystem, local=local)
        build = _optional_string(record, "build", _PACKAGE_ERROR)
        subdir = _optional_string(record, "subdir", _PACKAGE_ERROR)
    return LockedPackage(
        ecosystem=ecosystem,
        name=name,
        version=version,
        locator=None if local else locator,
        sha256=hashes,
        license_expression=license_expression,
        build=build,
        subdir=subdir,
        local=local,
    )


def load_pixi_lock(path: Path, *, environment: str, platform: str) -> LockInventory:
    """Load one selected closure from a Pixi v7 lock without resolving it."""
    if not environment or not platform:
        raise InventoryError(_PIXILOCK_ERROR)
    data, text = _read_utf8(path)
    payload = _object_mapping(_read_pixi_payload(text), _PIXILOCK_ERROR)
    if (
        type(payload.get("version")) is not int
        or payload.get("version") != _PIXI_LOCK_VERSION
    ):
        raise InventoryError(_PIXILOCK_ERROR)
    if not _selected_platform_is_declared(payload.get("platforms"), platform):
        raise InventoryError(_PLATFORM_ERROR)

    environments = _object_mapping(payload.get("environments"), _PIXILOCK_ERROR)
    environment_value = environments.get(environment)
    if environment_value is None:
        raise InventoryError(_ENVIRONMENT_ERROR)
    selected_environment = _object_mapping(environment_value, _ENVIRONMENT_ERROR)
    platform_packages = _object_mapping(
        selected_environment.get("packages"), _ENVIRONMENT_ERROR
    )
    if platform not in platform_packages:
        raise InventoryError(_PLATFORM_ERROR)

    references = _selected_references(platform_packages[platform], platform)
    global_packages_value = payload.get("packages")
    if not isinstance(global_packages_value, list):
        raise InventoryError(_PIXILOCK_ERROR)
    global_packages = cast(list[object], global_packages_value)

    packages: list[LockedPackage] = []
    for ecosystem, locator, local in references:
        record = _matching_record(global_packages, ecosystem, locator)
        packages.append(
            _package_from_record(record, ecosystem, locator, platform, local=local)
        )

    packages.sort(
        key=lambda package: (
            package.ecosystem,
            package.name,
            package.version or "",
            package.locator or "",
            package.build or "",
        )
    )
    return LockInventory(
        lock_sha256=hashlib.sha256(data).hexdigest(), packages=tuple(packages)
    )


def _logical_requirement_lines(text: str) -> tuple[str, ...]:
    logical_lines: list[str] = []
    pending: str | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        continued = line.endswith("\\")
        part = line[:-1].strip() if continued else line
        if not part:
            raise InventoryError(_REQUIREMENTS_ERROR)
        combined = f"{pending} {part}" if pending is not None else part
        combined = " ".join(combined.split())
        if continued:
            pending = combined
        else:
            logical_lines.append(combined)
            pending = None
    if pending is not None:
        raise InventoryError(_REQUIREMENTS_ERROR)
    return tuple(logical_lines)


def _append_requirement_hash(hashes: list[str], value: str) -> None:
    match = _PIP_SHA256.fullmatch(value)
    if match is None:
        raise InventoryError(_REQUIREMENTS_ERROR)
    hashes.append(match.group(1).lower())


def _parse_locked_requirement(line: str) -> LockedPackage:
    try:
        tokens = shlex.split(line, comments=True, posix=True)
    except ValueError:
        raise InventoryError(_REQUIREMENTS_ERROR) from None
    if not tokens:
        raise InventoryError(_REQUIREMENTS_ERROR)

    requirement_parts: list[str] = []
    hashes: list[str] = []
    index = 0
    while index < len(tokens):
        argument = tokens[index]
        if argument == "--hash":
            index += 1
            if index >= len(tokens):
                raise InventoryError(_REQUIREMENTS_ERROR)
            _append_requirement_hash(hashes, tokens[index])
        elif argument.startswith("--hash="):
            _append_requirement_hash(hashes, argument.removeprefix("--hash="))
        elif argument.startswith("-"):
            raise InventoryError(_REQUIREMENTS_ERROR)
        else:
            requirement_parts.append(argument)
        index += 1

    if not requirement_parts:
        raise InventoryError(_REQUIREMENTS_ERROR)
    requirement_text = " ".join(requirement_parts)
    try:
        requirement = Requirement(requirement_text)
    except InvalidRequirement:
        raise InventoryError(_REQUIREMENTS_ERROR) from None
    if requirement.url is not None or requirement.marker is not None:
        raise InventoryError(_REQUIREMENTS_ERROR)

    specifiers = tuple(requirement.specifier)
    if (
        requirement_text.count("==") != 1
        or len(specifiers) != 1
        or specifiers[0].operator != "=="
        or "*" in specifiers[0].version
    ):
        raise InventoryError(_REQUIREMENTS_ERROR)
    version = specifiers[0].version
    try:
        Version(version)
        name = canonicalize_name(requirement.name, validate=True)
    except InvalidName, InvalidVersion:
        raise InventoryError(_REQUIREMENTS_ERROR) from None
    if not hashes:
        raise InventoryError(_REQUIREMENTS_ERROR)

    return LockedPackage(
        ecosystem="pypi",
        name=name,
        version=version,
        locator=None,
        sha256=tuple(hashes),
        license_expression=None,
        build=None,
        subdir=None,
    )


def load_requirements_lock(path: Path) -> LockInventory:
    """Load exact, SHA-256-locked pip requirements without installing them."""
    data, text = _read_utf8(path)
    packages: list[LockedPackage] = []
    seen_names: set[str] = set()
    for line in _logical_requirement_lines(text):
        package = _parse_locked_requirement(line)
        if package.name in seen_names:
            raise InventoryError(_REQUIREMENTS_ERROR)
        seen_names.add(package.name)
        packages.append(package)

    packages.sort(
        key=lambda package: (
            package.ecosystem,
            package.name,
            package.version or "",
            package.locator or "",
            package.build or "",
        )
    )
    return LockInventory(
        lock_sha256=hashlib.sha256(data).hexdigest(), packages=tuple(packages)
    )


__all__ = [
    "InventoryError",
    "LockInventory",
    "LockedPackage",
    "load_pixi_lock",
    "load_requirements_lock",
]
