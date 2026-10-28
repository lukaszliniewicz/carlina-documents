"""Read the immutable, version-bound dependency companion inventory."""

from __future__ import annotations

import ipaddress
import json
import os
import re
import stat
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, cast
from urllib.parse import unquote, urlsplit

from carlina.application_source import ApplicationSourceMetadata

APPLICATION_COMPANIONS_RELATIVE_PATH: Final = (
    "share/carlina/application-companions.json"
)
MAX_APPLICATION_COMPANIONS_BYTES: Final = 16_384

_FORMAT: Final = "carlina.application-companions"
_PROFILE: Final = "documents"
_MAX_NESTING_DEPTH: Final = 16
_MAX_FILENAME_LENGTH: Final = 200
_MAX_URL_LENGTH: Final = 2048
_MAX_COMPANION_BYTES: Final = 4 * 1024 * 1024 * 1024
_ROOT_KEYS: Final = frozenset(
    {
        "format",
        "schema_version",
        "application_profile",
        "application_version",
        "source_revision",
        "application_source_sha256",
        "wheel_sha256",
        "inventory_inputs",
        "companions",
    }
)
_INVENTORY_KEYS: Final = frozenset({"platform", "sha256"})
_COMPANION_KEYS: Final = frozenset(
    {"kind", "filename", "byte_length", "sha256", "url", "coverage"}
)
_APPLICATION_VERSION: Final = re.compile(r"[0-9][0-9A-Za-z._+!-]{0,199}")
_REVISION: Final = re.compile(r"[0-9a-f]{40}")
_SHA256: Final = re.compile(r"[0-9a-f]{64}")
_FILENAME: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]*\.zip")
_DNS_LABEL: Final = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")
_INVALID_MANIFEST_ERROR: Final = "Application companions manifest is malformed."
_READ_ERROR: Final = "Application companions manifest could not be read."
_WINDOWS_RESERVED_NAMES: Final = frozenset(
    {
        "AUX",
        "CON",
        "NUL",
        "PRN",
        *(f"COM{number}" for number in range(1, 10)),
        *(f"LPT{number}" for number in range(1, 10)),
    }
)

CompanionKind = Literal["dependency_sources", "dependency_notices"]


class ApplicationCompanionsError(ValueError):
    """The fixed application companion manifest is invalid or unreadable."""


@dataclass(frozen=True, slots=True)
class CompanionInventory:
    """Digest of the resolved dependency inventory for one platform."""

    platform: str
    sha256: str


@dataclass(frozen=True, slots=True)
class CompanionDownload:
    """A selected, version-bound dependency companion download."""

    kind: CompanionKind
    filename: str
    byte_length: int
    sha256: str
    url: str
    coverage: Literal["selected"]


@dataclass(frozen=True, slots=True)
class ApplicationCompanionsMetadata:
    """Validated companion metadata bound to a Documents application build."""

    application_version: str
    source_revision: str
    application_source_sha256: str
    wheel_sha256: str
    inventory_inputs: tuple[CompanionInventory, ...]
    companions: tuple[CompanionDownload, ...]


def _unique_object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ApplicationCompanionsError(_INVALID_MANIFEST_ERROR)
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise ApplicationCompanionsError(_INVALID_MANIFEST_ERROR)


def _depth_is_bounded(payload: str) -> bool:
    depth = 0
    in_string = False
    escaped = False
    for character in payload:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character in "[{":
            depth += 1
            if depth > _MAX_NESTING_DEPTH:
                return False
        elif character in "]}":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0 and not in_string


def _safe_application_version(value: object) -> bool:
    return type(value) is str and _APPLICATION_VERSION.fullmatch(value) is not None


def _safe_sha256(value: object) -> bool:
    return type(value) is str and _SHA256.fullmatch(value) is not None


def _safe_filename(value: object) -> bool:
    if (
        type(value) is not str
        or len(value) > _MAX_FILENAME_LENGTH
        or not value.isascii()
        or _FILENAME.fullmatch(value) is None
        or ".." in value
    ):
        return False
    basename = value.split(".", 1)[0].upper()
    return basename not in _WINDOWS_RESERVED_NAMES


def _valid_hostname(hostname: str) -> bool:
    if not hostname or len(hostname) > 253 or "%" in hostname:
        return False
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        if re.fullmatch(r"[0-9.]+", hostname) is not None:
            return False
        dns_name = hostname.removesuffix(".")
        labels = dns_name.split(".")
        return bool(dns_name) and all(
            len(label) <= 63 and _DNS_LABEL.fullmatch(label) is not None
            for label in labels
        )
    return True


def _safe_download_url(value: object, filename: str) -> bool:
    if (
        type(value) is not str
        or not value
        or len(value) > _MAX_URL_LENGTH
        or not value.isascii()
        or any(ord(character) <= 0x20 or ord(character) == 0x7F for character in value)
        or "\\" in value
        or "?" in value
        or "#" in value
        or re.search(r"%(?![0-9A-Fa-f]{2})", value) is not None
    ):
        return False
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.netloc.endswith(":")
        or (port is not None and not 1 <= port <= 65_535)
    ):
        return False
    hostname = parsed.hostname
    if hostname is None or not _valid_hostname(hostname):
        return False
    if parsed.scheme.lower() == "http":
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            return False
        if not address.is_loopback:
            return False
    try:
        decoded_path = unquote(parsed.path, encoding="utf-8", errors="strict")
    except UnicodeDecodeError:
        return False
    if (
        "\\" in decoded_path
        or any(unicodedata.category(character) == "Cc" for character in decoded_path)
        or any(segment in {".", ".."} for segment in decoded_path.split("/"))
    ):
        return False
    return decoded_path.rsplit("/", 1)[-1] == filename


def _parse_object(value: object, expected_keys: frozenset[str]) -> dict[str, object]:
    if type(value) is not dict:
        raise ApplicationCompanionsError(_INVALID_MANIFEST_ERROR)
    parsed = cast(dict[str, object], value)
    if frozenset(parsed) != expected_keys:
        raise ApplicationCompanionsError(_INVALID_MANIFEST_ERROR)
    return parsed


def _source_identity_is_valid(application_source: object) -> bool:
    if type(application_source) is not ApplicationSourceMetadata:
        return False
    return (
        _safe_application_version(application_source.application_version)
        and type(application_source.source_revision) is str
        and _REVISION.fullmatch(application_source.source_revision) is not None
        and _safe_sha256(application_source.sha256)
    )


def _parse_inventory_inputs(value: object) -> tuple[CompanionInventory, ...]:
    if type(value) is not list:
        raise ApplicationCompanionsError(_INVALID_MANIFEST_ERROR)
    entries = cast(list[object], value)
    if not 1 <= len(entries) <= 2:
        raise ApplicationCompanionsError(_INVALID_MANIFEST_ERROR)
    results: list[CompanionInventory] = []
    seen_platforms: set[str] = set()
    for entry in entries:
        parsed = _parse_object(entry, _INVENTORY_KEYS)
        platform = parsed["platform"]
        sha256 = parsed["sha256"]
        if (
            type(platform) is not str
            or platform not in {"linux-64", "win-64"}
            or platform in seen_platforms
            or not _safe_sha256(sha256)
        ):
            raise ApplicationCompanionsError(_INVALID_MANIFEST_ERROR)
        seen_platforms.add(platform)
        results.append(CompanionInventory(platform=platform, sha256=cast(str, sha256)))
    return tuple(results)


def _parse_companions(value: object) -> tuple[CompanionDownload, ...]:
    if type(value) is not list:
        raise ApplicationCompanionsError(_INVALID_MANIFEST_ERROR)
    entries = cast(list[object], value)
    if not 1 <= len(entries) <= 2:
        raise ApplicationCompanionsError(_INVALID_MANIFEST_ERROR)
    results: list[CompanionDownload] = []
    seen_kinds: set[str] = set()
    seen_filenames: set[str] = set()
    for entry in entries:
        parsed = _parse_object(entry, _COMPANION_KEYS)
        kind = parsed["kind"]
        filename = parsed["filename"]
        byte_length = parsed["byte_length"]
        sha256 = parsed["sha256"]
        url = parsed["url"]
        coverage = parsed["coverage"]
        if (
            type(kind) is not str
            or kind not in {"dependency_sources", "dependency_notices"}
            or kind in seen_kinds
            or not _safe_filename(filename)
            or cast(str, filename) in seen_filenames
            or type(byte_length) is not int
            or not 1 <= byte_length <= _MAX_COMPANION_BYTES
            or not _safe_sha256(sha256)
            or type(coverage) is not str
            or coverage != "selected"
            or not _safe_download_url(url, cast(str, filename))
        ):
            raise ApplicationCompanionsError(_INVALID_MANIFEST_ERROR)
        seen_kinds.add(kind)
        seen_filenames.add(cast(str, filename))
        results.append(
            CompanionDownload(
                kind=cast(CompanionKind, kind),
                filename=cast(str, filename),
                byte_length=byte_length,
                sha256=cast(str, sha256),
                url=cast(str, url),
                coverage="selected",
            )
        )
    return tuple(results)


def parse_application_companions(
    payload: bytes,
    *,
    application_source: ApplicationSourceMetadata,
) -> ApplicationCompanionsMetadata:
    """Parse a closed manifest and bind it to verified source metadata."""
    if (
        type(payload) is not bytes
        or len(payload) > MAX_APPLICATION_COMPANIONS_BYTES
        or not _source_identity_is_valid(application_source)
    ):
        raise ApplicationCompanionsError(_INVALID_MANIFEST_ERROR)
    try:
        decoded = payload.decode("utf-8")
        if not _depth_is_bounded(decoded):
            raise ApplicationCompanionsError(_INVALID_MANIFEST_ERROR)
        parsed_object: object = json.loads(
            decoded,
            object_pairs_hook=_unique_object_pairs,
            parse_constant=_reject_constant,
        )
    except ApplicationCompanionsError:
        raise
    except UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError:
        raise ApplicationCompanionsError(_INVALID_MANIFEST_ERROR) from None

    parsed = _parse_object(parsed_object, _ROOT_KEYS)
    if (
        type(parsed["format"]) is not str
        or parsed["format"] != _FORMAT
        or type(parsed["schema_version"]) is not int
        or parsed["schema_version"] != 1
        or type(parsed["application_profile"]) is not str
        or parsed["application_profile"] != _PROFILE
    ):
        raise ApplicationCompanionsError(_INVALID_MANIFEST_ERROR)

    application_version = parsed["application_version"]
    source_revision = parsed["source_revision"]
    application_source_sha256 = parsed["application_source_sha256"]
    wheel_sha256 = parsed["wheel_sha256"]
    if (
        not _safe_application_version(application_version)
        or application_version != application_source.application_version
        or type(source_revision) is not str
        or source_revision != application_source.source_revision
        or _REVISION.fullmatch(source_revision) is None
        or not _safe_sha256(application_source_sha256)
        or application_source_sha256 != application_source.sha256
        or not _safe_sha256(wheel_sha256)
    ):
        raise ApplicationCompanionsError(_INVALID_MANIFEST_ERROR)

    return ApplicationCompanionsMetadata(
        application_version=cast(str, application_version),
        source_revision=source_revision,
        application_source_sha256=cast(str, application_source_sha256),
        wheel_sha256=cast(str, wheel_sha256),
        inventory_inputs=_parse_inventory_inputs(parsed["inventory_inputs"]),
        companions=_parse_companions(parsed["companions"]),
    )


def _same_file_identity(left: os.stat_result, right: os.stat_result) -> bool:
    devices_match = left.st_dev == right.st_dev or not (left.st_dev or right.st_dev)
    inodes_match = left.st_ino == right.st_ino or not (left.st_ino or right.st_ino)
    return devices_match and inodes_match


def _is_redirected(path_stat: os.stat_result) -> bool:
    reparse_point = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    file_attributes = getattr(path_stat, "st_file_attributes", 0)
    return stat.S_ISLNK(path_stat.st_mode) or bool(file_attributes & reparse_point)


def _read_fixed_manifest(path: Path) -> bytes:
    try:
        path_stat = path.lstat()
        if _is_redirected(path_stat) or not stat.S_ISREG(path_stat.st_mode):
            raise ApplicationCompanionsError(_READ_ERROR)
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            descriptor_stat = os.fstat(descriptor)
            if not stat.S_ISREG(descriptor_stat.st_mode) or not _same_file_identity(
                path_stat, descriptor_stat
            ):
                raise ApplicationCompanionsError(_READ_ERROR)
            with os.fdopen(descriptor, "rb") as stream:
                descriptor = -1
                payload = stream.read(MAX_APPLICATION_COMPANIONS_BYTES + 1)
                after_read = os.fstat(stream.fileno())
            final_path_stat = path.lstat()
        finally:
            if descriptor >= 0:
                os.close(descriptor)
    except ApplicationCompanionsError:
        raise
    except OSError, TypeError, ValueError:
        raise ApplicationCompanionsError(_READ_ERROR) from None
    if (
        _is_redirected(final_path_stat)
        or not stat.S_ISREG(final_path_stat.st_mode)
        or not stat.S_ISREG(after_read.st_mode)
        or not _same_file_identity(final_path_stat, after_read)
        or not _same_file_identity(path_stat, after_read)
        or type(payload) is not bytes
        or len(payload) > MAX_APPLICATION_COMPANIONS_BYTES
    ):
        raise ApplicationCompanionsError(_READ_ERROR)
    return payload


def read_application_companions(
    *, application_source: ApplicationSourceMetadata
) -> ApplicationCompanionsMetadata | None:
    """Read only the fixed manifest under the resolved active Python prefix."""
    try:
        prefix = Path(sys.prefix).resolve(strict=True)
        if not prefix.is_dir():
            raise ApplicationCompanionsError(_READ_ERROR)
        current = prefix
        path_components = APPLICATION_COMPANIONS_RELATIVE_PATH.split("/")
        for component in path_components[:-1]:
            current = current / component
            try:
                component_stat = current.lstat()
            except FileNotFoundError:
                return None
            if _is_redirected(component_stat) or not stat.S_ISDIR(
                component_stat.st_mode
            ):
                raise ApplicationCompanionsError(_READ_ERROR)
        manifest_path = current / path_components[-1]
        try:
            manifest_path.lstat()
        except FileNotFoundError:
            return None
        payload = _read_fixed_manifest(manifest_path)
    except ApplicationCompanionsError:
        raise
    except OSError, RuntimeError, TypeError, ValueError:
        raise ApplicationCompanionsError(_READ_ERROR) from None
    return parse_application_companions(
        payload,
        application_source=application_source,
    )


__all__ = [
    "APPLICATION_COMPANIONS_RELATIVE_PATH",
    "MAX_APPLICATION_COMPANIONS_BYTES",
    "ApplicationCompanionsError",
    "ApplicationCompanionsMetadata",
    "CompanionDownload",
    "CompanionInventory",
    "parse_application_companions",
    "read_application_companions",
]
