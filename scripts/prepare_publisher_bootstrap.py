"""Prepare a closed local bootstrap plan from a verified Pixi Pack artifact."""

from __future__ import annotations

import argparse
import base64
import binascii
import gzip
import hashlib
import json
import os
import re
import stat
import sys
import tarfile
import tempfile
import unicodedata
import zipfile
from collections import Counter
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Protocol, TextIO, cast
from urllib.parse import unquote, urlsplit

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from carlina.core.publisher_bootstrap import (  # noqa: E402
    BOOTSTRAP_SKELETON_FILENAME,
    PUBLISHER_BOOTSTRAP_FORMAT,
    PUBLISHER_BOOTSTRAP_SCHEMA_VERSION,
    BootstrapSkeleton,
    HashedPayload,
    PublisherBootstrapError,
    PublisherBootstrapPlan,
    PublisherPackage,
    PublisherUnpacker,
    parse_publisher_bootstrap_plan,
    publisher_bootstrap_plan_bytes,
)
from iarf_assistant.core.managed_bundle import (  # noqa: E402
    WINDOWS_BUNDLE_MANIFEST,
    extract_windows_bundle,
    parse_windows_bundle_manifest,
)

PUBLISHER_BOOTSTRAP_PREPARATION_FORMAT = "carlina.publisher-bootstrap.preparation"
PUBLISHER_BOOTSTRAP_PREPARATION_SCHEMA_VERSION = 1

_ARTIFACT_FORMAT = "carlina.managed-artifact"
_INVENTORY_FORMAT = "carlina.documents-release-inventory"
_APPLICATION_PROFILE = "documents"
_PIXIPACK_VERSION = "0.7.10"
_MAX_METADATA_BYTES = 64 * 1024
_MAX_INVENTORY_BYTES = 16 * 1024 * 1024
_MAX_SOURCE_PACK_BYTES = 8 * 1024 * 1024 * 1024
_MAX_ARCHIVE_BYTES = 4 * 1024 * 1024 * 1024
_MAX_TAR_MEMBERS = 4096
_MAX_LONG_NAME_BYTES = 4096
_MAX_DECLARED_PAYLOAD_BYTES = 4 * 1024 * 1024 * 1024
_MAX_HEADER_SCAN_BYTES = 64 * 1024
_MAX_TRAILING_LINE_BREAKS = 64 * 1024
_CHUNK_SIZE = 1024 * 1024
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_REVISION = re.compile(r"[0-9a-f]{40}\Z")
_LINUX_HEADER_MARKER = b"@@END_HEADER@@"
_LINUX_ARCHIVE_MARKER = b"\n@@END_ARCHIVE@@\n"
_WINDOWS_BUNDLE_MEMBERS = frozenset({"archive.tar", "pixi-unpack.exe", "bundle.json"})
_WINDOWS_MANIFEST_LIMIT = 64 * 1024


class PublisherBootstrapPreparationError(ValueError):
    """Raised when the frozen artifact or its inventory is not safe to prepare."""


@dataclass(frozen=True, slots=True)
class _StatIdentity:
    device: int
    inode: int
    mode: int
    size: int
    modified_ns: int
    changed_ns: int


class _BinaryReader(Protocol):
    def read(self, size: int = -1, /) -> bytes: ...


class _BinaryWriter(Protocol):
    def write(self, data: bytes, /) -> int: ...


@dataclass(frozen=True, slots=True)
class _FileIdentity:
    size: int
    sha256: str
    stat: _StatIdentity


@dataclass(frozen=True, slots=True)
class _WheelIdentity:
    filename: str
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class _ArtifactMetadata:
    version: str
    platform: str
    filename: str
    sha256: str
    size: int
    source_revision: str
    wheel: _WheelIdentity
    companion_wheel: _WheelIdentity | None


@dataclass(frozen=True, slots=True)
class _ComponentLink:
    sha256: str
    component_ref: str
    url: str
    basename: str
    ecosystem: str
    subdir: str | None


@dataclass(frozen=True, slots=True)
class _TarMember:
    name: str
    member_type: str
    classification: str
    size: int
    offset_data: int | None
    sha256: str | None


@dataclass(frozen=True, slots=True)
class _TarInspection:
    members: tuple[_TarMember, ...]
    downloads: tuple[PublisherPackage, ...]
    original_archive: _FileIdentity
    removed_payload_bytes: int
    classification_counts: Mapping[str, int]


@dataclass(frozen=True, slots=True)
class _PinnedUnpacker:
    name: str
    url: str
    size: int
    sha256: str


_PINNED_UNPACKERS: dict[str, _PinnedUnpacker] = {
    "linux-64": _PinnedUnpacker(
        name="pixi-unpack-x86_64-unknown-linux-musl",
        url=(
            "https://github.com/Quantco/pixi-pack/releases/download/v0.7.10/"
            "pixi-unpack-x86_64-unknown-linux-musl"
        ),
        size=15668176,
        sha256="1e610ea67baae58119de8aa52cf238f6efea7e0c68ea3a8a295c8a2630a5df04",
    ),
    "win-64": _PinnedUnpacker(
        name="pixi-unpack-x86_64-pc-windows-msvc.exe",
        url=(
            "https://github.com/Quantco/pixi-pack/releases/download/v0.7.10/"
            "pixi-unpack-x86_64-pc-windows-msvc.exe"
        ),
        size=15029248,
        sha256="f0409bf7ba71ade130bc2f74aca2c7a923af207f7cfa40bad82b3bff2d20bd8b",
    ),
}

_ARTIFACT_FIELDS = frozenset(
    {
        "format",
        "schema_version",
        "version",
        "platform",
        "kind",
        "filename",
        "sha256",
        "size",
        "wheel",
        "pixi_pack_version",
        "pixi_lock_sha256",
        "built_at",
        "source_revision",
        "application_profile",
        "companion_wheel",
    }
)
_REQUIRED_ARTIFACT_FIELDS = _ARTIFACT_FIELDS - {"companion_wheel"}
_INVENTORY_FIELDS = frozenset(
    {
        "format",
        "schema_version",
        "application_profile",
        "source_revision",
        "application_version",
        "wheel_sha256",
        "application_source",
        "platform",
        "basis",
        "frontend_scope",
        "notice_collection_complete",
        "public_release_accepted",
        "excluded_optional_runtime",
        "source_locks",
        "source_inputs",
        "reconciled_local_products",
        "components",
        "required_follow_up",
    }
)
_WHEEL_FIELDS = frozenset({"filename", "sha256", "size"})
_METADATA_MEMBERS = frozenset(
    {
        "channel/noarch/repodata.json",
        "channel/{platform}/repodata.json",
        "environment.yml",
        "pixi-pack.json",
    }
)
_ALLOWED_DIRECTORIES = frozenset(
    {"channel", "channel/{platform}", "channel/noarch", "pypi"}
)


def _fail(message: str) -> PublisherBootstrapPreparationError:
    return PublisherBootstrapPreparationError(message)


def _is_regular_non_reparse(info: os.stat_result) -> bool:
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    attributes = getattr(info, "st_file_attributes", 0)
    return (
        stat.S_ISREG(info.st_mode)
        and not stat.S_ISLNK(info.st_mode)
        and not (attributes & reparse_flag)
    )


def _stat_identity(info: os.stat_result) -> _StatIdentity:
    return _StatIdentity(
        device=info.st_dev,
        inode=info.st_ino,
        mode=info.st_mode,
        size=info.st_size,
        modified_ns=info.st_mtime_ns,
        changed_ns=info.st_ctime_ns,
    )


@contextmanager
def _open_regular_file(
    path: Path, label: str
) -> Generator[tuple[BinaryIO, _StatIdentity]]:
    try:
        before_open = path.lstat()
    except OSError as exc:
        raise _fail(f"{label} input could not be opened.") from exc
    if not _is_regular_non_reparse(before_open):
        raise _fail(f"{label} input must be a regular file.")

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise _fail(f"{label} input could not be opened safely.") from exc

    handle: BinaryIO | None = None
    try:
        opened = os.fstat(descriptor)
        opened_identity = _stat_identity(opened)
        if not _is_regular_non_reparse(opened) or opened_identity != _stat_identity(
            before_open
        ):
            raise _fail(f"{label} input changed while it was opened.")
        try:
            after_open = path.lstat()
        except OSError as exc:
            raise _fail(f"{label} input path changed while it was opened.") from exc
        if (
            not _is_regular_non_reparse(after_open)
            or _stat_identity(after_open) != opened_identity
        ):
            raise _fail(f"{label} input path changed while it was opened.")
        handle = os.fdopen(descriptor, "rb")
        descriptor = -1
        yield handle, opened_identity
    except PublisherBootstrapPreparationError:
        raise
    except OSError as exc:
        raise _fail(f"{label} input could not be read safely.") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if handle is not None:
            handle.close()


def _read_bounded_file(
    path: Path, limit: int, label: str
) -> tuple[bytes, _FileIdentity]:
    content = bytearray()
    digest = hashlib.sha256()
    with _open_regular_file(path, label) as (handle, original_stat):
        if original_stat.size > limit:
            raise _fail(f"{label} input exceeds its size limit.")
        while True:
            remaining = limit + 1 - len(content)
            chunk = handle.read(min(_CHUNK_SIZE, remaining))
            if not chunk:
                break
            content.extend(chunk)
            digest.update(chunk)
            if len(content) > limit:
                raise _fail(f"{label} input exceeds its size limit.")
        if len(content) != original_stat.size:
            raise _fail(f"{label} input changed while it was read.")
        if _stat_identity(os.fstat(handle.fileno())) != original_stat:
            raise _fail(f"{label} input changed while it was read.")
    return (
        bytes(content),
        _FileIdentity(len(content), digest.hexdigest(), original_stat),
    )


def _hash_regular_file(path: Path, limit: int, label: str) -> _FileIdentity:
    digest = hashlib.sha256()
    total = 0
    with _open_regular_file(path, label) as (handle, original_stat):
        if original_stat.size > limit:
            raise _fail(f"{label} input exceeds its size limit.")
        while chunk := handle.read(_CHUNK_SIZE):
            total += len(chunk)
            if total > limit:
                raise _fail(f"{label} input exceeds its size limit.")
            digest.update(chunk)
        final_stat = _stat_identity(os.fstat(handle.fileno()))
        if total != original_stat.size or final_stat != original_stat:
            raise _fail(f"{label} input changed while it was hashed.")
    return _FileIdentity(total, digest.hexdigest(), original_stat)


def _same_file_identity(expected: _FileIdentity, actual: _FileIdentity) -> bool:
    return (
        expected.size == actual.size
        and expected.sha256 == actual.sha256
        and expected.stat == actual.stat
    )


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"invalid JSON constant: {value}")


def _parse_json(payload: bytes, label: str) -> object:
    try:
        return json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_json_constant,
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        RecursionError,
        ValueError,
    ) as exc:
        raise _fail(f"{label} input is not valid duplicate-free UTF-8 JSON.") from exc


def _object(
    value: object, label: str, fields: frozenset[str] | None = None
) -> dict[str, object]:
    if not isinstance(value, dict):
        raise _fail(f"{label} must be a JSON object.")
    untyped_result = cast(dict[object, object], value)
    if not all(type(key) is str for key in untyped_result):
        raise _fail(f"{label} must be a JSON object.")
    result = cast(dict[str, object], untyped_result)
    if fields is not None and frozenset(result) != fields:
        raise _fail(f"{label} has unknown or missing fields.")
    return result


def _string(value: object, label: str) -> str:
    if type(value) is not str or not value:
        raise _fail(f"{label} must be a non-empty string.")
    return value


def _integer(value: object, label: str, *, minimum: int = 1) -> int:
    if type(value) is not int or value < minimum:
        raise _fail(f"{label} must be an integer in range.")
    return value


def _sha256(value: object, label: str) -> str:
    result = _string(value, label)
    if _SHA256.fullmatch(result) is None:
        raise _fail(f"{label} must be a lowercase SHA-256 digest.")
    return result


def _safe_basename(value: object, label: str, suffix: str) -> str:
    result = _string(value, label)
    if (
        result in {".", ".."}
        or "/" in result
        or "\\" in result
        or "\x00" in result
        or any(ord(character) < 32 or ord(character) == 127 for character in result)
        or not result.endswith(suffix)
    ):
        raise _fail(f"{label} must be a safe basename ending in {suffix}.")
    return result


def _parse_wheel_identity(value: object, label: str) -> _WheelIdentity:
    record = _object(value, label, _WHEEL_FIELDS)
    filename = _safe_basename(record["filename"], f"{label}.filename", ".whl")
    size = _integer(record["size"], f"{label}.size")
    if size > 1024 * 1024 * 1024:
        raise _fail(f"{label}.size exceeds the wheel size limit.")
    return _WheelIdentity(
        filename=filename,
        size=size,
        sha256=_sha256(record["sha256"], f"{label}.sha256"),
    )


def _parse_artifact_metadata(path: Path) -> tuple[_ArtifactMetadata, _FileIdentity]:
    encoded, identity = _read_bounded_file(
        path, _MAX_METADATA_BYTES, "artifact metadata"
    )
    raw = _object(_parse_json(encoded, "artifact metadata"), "artifact metadata")
    present_fields = frozenset(raw)
    if (
        not present_fields >= _REQUIRED_ARTIFACT_FIELDS
        or present_fields - _ARTIFACT_FIELDS
    ):
        raise _fail("artifact metadata has unknown or missing fields.")
    if (
        raw["format"] != _ARTIFACT_FORMAT
        or type(raw["schema_version"]) is not int
        or raw["schema_version"] != 1
        or raw["application_profile"] != _APPLICATION_PROFILE
        or raw["pixi_pack_version"] != _PIXIPACK_VERSION
    ):
        raise _fail(
            "artifact metadata format, profile, or Pixi Pack version is unsupported."
        )

    platform = _string(raw["platform"], "artifact platform")
    if platform not in _PINNED_UNPACKERS:
        raise _fail("artifact platform is unsupported.")
    version = _string(raw["version"], "artifact version")
    filename = _string(raw["filename"], "artifact filename")
    if platform == "linux-64":
        expected_kind, expected_suffix = "pixi-pack-executable", ".sh"
    else:
        expected_kind, expected_suffix = "pixi-pack-bundle", ".zip"
    filename = _safe_basename(filename, "artifact filename", expected_suffix)
    if not filename.endswith(f"-{platform}{expected_suffix}"):
        raise _fail("artifact filename does not match its platform.")
    if raw["kind"] != expected_kind or path.name != f"{filename}.artifact.json":
        raise _fail("artifact metadata kind or sibling filename is inconsistent.")
    size = _integer(raw["size"], "artifact size")
    if size > _MAX_SOURCE_PACK_BYTES:
        raise _fail("artifact exceeds the source pack size limit.")
    source_revision = _string(raw["source_revision"], "source revision")
    if _REVISION.fullmatch(source_revision) is None:
        raise _fail("source revision must be a lowercase forty-character Git SHA.")
    _sha256(raw["pixi_lock_sha256"], "Pixi lock SHA-256")
    _sha256(raw["sha256"], "artifact SHA-256")
    built_at = _string(raw["built_at"], "artifact build timestamp")
    if not built_at:
        raise _fail("artifact build timestamp is missing.")
    wheel = _parse_wheel_identity(raw["wheel"], "wheel")
    companion_wheel = (
        _parse_wheel_identity(raw["companion_wheel"], "companion wheel")
        if "companion_wheel" in raw
        else None
    )
    if companion_wheel is not None and wheel.filename == companion_wheel.filename:
        raise _fail("application and companion wheel filenames must be distinct.")
    return (
        _ArtifactMetadata(
            version=version,
            platform=platform,
            filename=filename,
            sha256=_sha256(raw["sha256"], "artifact SHA-256"),
            size=size,
            source_revision=source_revision,
            wheel=wheel,
            companion_wheel=companion_wheel,
        ),
        identity,
    )


def _inventory_identity(
    path: Path,
    metadata: _ArtifactMetadata,
) -> tuple[dict[str, object], _FileIdentity, tuple[_ComponentLink, ...]]:
    encoded, identity = _read_bounded_file(path, _MAX_INVENTORY_BYTES, "inventory")
    raw = _object(_parse_json(encoded, "inventory"), "inventory", _INVENTORY_FIELDS)
    if (
        raw["format"] != _INVENTORY_FORMAT
        or type(raw["schema_version"]) is not int
        or raw["schema_version"] != 1
        or raw["application_profile"] != _APPLICATION_PROFILE
        or raw["application_version"] != metadata.version
        or raw["platform"] != metadata.platform
        or raw["source_revision"] != metadata.source_revision
        or raw["wheel_sha256"] != metadata.wheel.sha256
    ):
        raise _fail("inventory identity does not match the artifact metadata.")
    components = raw["components"]
    if not isinstance(components, list):
        raise _fail("inventory components must be a JSON array.")
    links = _component_links(cast(list[object], components), metadata.platform)
    return raw, identity, links


def _property_values(properties: object, name: str, label: str) -> list[object]:
    if not isinstance(properties, list):
        raise _fail(f"{label} properties must be a JSON array.")
    values: list[object] = []
    for raw_property in cast(list[object], properties):
        prop = _object(raw_property, f"{label} property")
        if frozenset(prop) != frozenset({"name", "value"}):
            raise _fail(f"{label} property has unknown or missing fields.")
        if prop["name"] == name:
            values.append(prop["value"])
    return values


def _url_component_identity(
    url_value: object, label: str
) -> tuple[str, str | None, str]:
    url = _string(url_value, f"{label} distribution URL")
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise _fail(f"{label} distribution URL is malformed.") from exc
    if (
        parsed.scheme != "https"
        or hostname not in {"conda.anaconda.org", "files.pythonhosted.org"}
        or parsed.netloc != hostname
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith("/")
        or "\\" in parsed.path
    ):
        raise _fail(f"{label} distribution URL is outside the publisher allowlist.")
    raw_parts = parsed.path[1:].split("/")
    if not raw_parts or any(not part for part in raw_parts):
        raise _fail(f"{label} distribution URL has an unsafe path.")
    decoded_parts: list[str] = []
    for part in raw_parts:
        lowered = part.lower()
        if "%2f" in lowered or "%5c" in lowered:
            raise _fail(f"{label} distribution URL has an encoded separator.")
        try:
            decoded = unquote(part, errors="strict")
        except UnicodeDecodeError as exc:
            raise _fail(f"{label} distribution URL has invalid encoding.") from exc
        if decoded in {".", ".."} or "/" in decoded or "\\" in decoded:
            raise _fail(f"{label} distribution URL has an unsafe path segment.")
        if any(ord(character) < 32 or ord(character) == 127 for character in decoded):
            raise _fail(f"{label} distribution URL has a control character.")
        decoded_parts.append(decoded)

    basename = decoded_parts[-1]
    if hostname == "files.pythonhosted.org":
        if len(decoded_parts) < 2 or decoded_parts[0] != "packages":
            raise _fail(f"{label} wheel URL has an unsupported publisher path.")
        return "pypi", None, basename
    if (
        len(decoded_parts) != 3
        or decoded_parts[0] != "conda-forge"
        or decoded_parts[1] not in {"linux-64", "win-64", "noarch"}
    ):
        raise _fail(f"{label} Conda URL has an unsupported publisher path.")
    return "conda", decoded_parts[1], basename


def _component_links(
    components: list[object], platform: str
) -> tuple[_ComponentLink, ...]:
    seen_refs: set[str] = set()
    links: list[_ComponentLink] = []
    for index, component_raw in enumerate(components):
        label = f"inventory component {index}"
        component = _object(component_raw, label)
        component_ref = _string(component.get("bom-ref"), f"{label} bom-ref")
        if component_ref in seen_refs:
            raise _fail("inventory contains duplicate component references.")
        seen_refs.add(component_ref)
        properties = component.get("properties")
        environment_values = _property_values(
            properties,
            "carlina:environment",
            label,
        )
        if _APPLICATION_PROFILE not in environment_values:
            continue
        if environment_values.count(_APPLICATION_PROFILE) != 1:
            raise _fail(f"{label} has duplicate Documents environment properties.")

        hashes = component.get("hashes")
        if not isinstance(hashes, list):
            raise _fail(f"{label} must contain exactly one package hash.")
        hash_records = cast(list[object], hashes)
        if len(hash_records) != 1:
            raise _fail(f"{label} must contain exactly one package hash.")
        hash_record = _object(hash_records[0], f"{label} hash")
        if hash_record.get("alg") != "SHA-256":
            raise _fail(f"{label} hash must use SHA-256.")
        digest = _sha256(hash_record.get("content"), f"{label} hash content")

        external_references = component.get("externalReferences")
        if not isinstance(external_references, list):
            raise _fail(f"{label} externalReferences must be a JSON array.")
        distributions: list[dict[str, object]] = []
        for external_raw in cast(list[object], external_references):
            external = _object(external_raw, f"{label} external reference")
            if external.get("type") == "distribution":
                distributions.append(external)
        if len(distributions) != 1:
            raise _fail(f"{label} must have exactly one distribution reference.")
        ecosystem, url_subdir, basename = _url_component_identity(
            distributions[0].get("url"),
            label,
        )
        subdir_values = _property_values(properties, "carlina:subdir", label)
        if ecosystem == "pypi":
            if subdir_values:
                raise _fail(f"{label} wheel must not declare a Conda subdirectory.")
            subdir = None
            if not basename.endswith(".whl"):
                raise _fail(f"{label} wheel URL must name a wheel.")
        else:
            if len(subdir_values) != 1 or subdir_values[0] != url_subdir:
                raise _fail(f"{label} Conda subdirectory does not match its URL.")
            if url_subdir not in {platform, "noarch"}:
                raise _fail(f"{label} Conda package is for a different platform.")
            subdir = url_subdir
            if not basename.endswith((".conda", ".tar.bz2")):
                raise _fail(f"{label} Conda URL has an unsupported package suffix.")
        links.append(
            _ComponentLink(
                sha256=digest,
                component_ref=component_ref,
                url=_string(distributions[0].get("url"), f"{label} distribution URL"),
                basename=basename,
                ecosystem=ecosystem,
                subdir=subdir,
            )
        )
    return tuple(links)


def _safe_member_name(name: str, *, directory: bool) -> str:
    if not name or "\\" in name or "\x00" in name or name.startswith("/"):
        raise _fail("archive contains an unsafe POSIX member path.")
    normalized = name
    if directory and normalized.endswith("/"):
        normalized = normalized[:-1]
    if not normalized or normalized.startswith("/"):
        raise _fail("archive contains an unsafe POSIX member path.")
    parts = normalized.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise _fail("archive contains an unsafe POSIX member path.")
    if any(unicodedata.category(character) == "Cc" for character in normalized):
        raise _fail("archive contains a control character in a member path.")
    try:
        normalized.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise _fail("archive member path is not valid Unicode.") from exc
    return normalized


def _raw_tar_text(field: bytes) -> str:
    terminator = field.find(b"\x00")
    if terminator < 0:
        payload = field
    else:
        payload = field[:terminator]
        if any(field[terminator:]):
            raise _fail("archive header has non-canonical name padding.")
    try:
        return payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise _fail("archive header name is not valid UTF-8.") from exc


def _regular_tar_header(
    handle: BinaryIO, member: tarfile.TarInfo
) -> tuple[bytes, bool]:
    handle.seek(member.offset)
    header = handle.read(512)
    if len(header) != 512:
        raise _fail("archive member header is truncated.")
    if member.offset_data == member.offset + 512:
        return header, False
    if header[156:157] != tarfile.GNUTYPE_LONGNAME:
        raise _fail("archive member has an unsupported header layout.")
    extension = tarfile.TarInfo.frombuf(header, encoding="utf-8", errors="strict")
    encoded_name = member.name.encode("utf-8", errors="strict")
    if (
        extension.name != "././@LongLink"
        or member.type not in {tarfile.REGTYPE, tarfile.AREGTYPE}
        or not 100 < len(encoded_name) < _MAX_LONG_NAME_BYTES
        or extension.size != len(encoded_name) + 1
        or header[257:265] != tarfile.GNU_MAGIC
    ):
        raise _fail("archive has an unsupported GNU long-name record.")
    padded_size = ((extension.size + 511) // 512) * 512
    if member.offset_data != member.offset + 1024 + padded_size:
        raise _fail("archive GNU long-name record has an unsupported layout.")
    payload = handle.read(extension.size)
    padding = handle.read(padded_size - extension.size)
    if (
        payload != encoded_name + b"\x00"
        or len(padding) != padded_size - extension.size
        or any(padding)
    ):
        raise _fail("archive GNU long-name payload or padding is invalid.")
    regular_header = handle.read(512)
    if (
        len(regular_header) != 512
        or regular_header[257:265] != tarfile.GNU_MAGIC
        or regular_header[:100] != encoded_name[:100]
        or any(regular_header[345:500])
    ):
        raise _fail("archive GNU long-name header does not match its payload.")
    return regular_header, True


def _verify_raw_tar_header(
    handle: BinaryIO,
    member: tarfile.TarInfo,
) -> None:
    header, has_long_name = _regular_tar_header(handle, member)
    if has_long_name:
        _safe_member_name(member.name, directory=False)
        if header[156:157] != member.type:
            raise _fail("archive header type does not match its parsed member type.")
        return
    raw_name = _raw_tar_text(header[:100])
    raw_prefix = _raw_tar_text(header[345:500])
    combined = f"{raw_prefix}/{raw_name}" if raw_prefix else raw_name
    directory = member.type == tarfile.DIRTYPE
    raw_normalized = _safe_member_name(combined, directory=directory)
    parsed_normalized = _safe_member_name(member.name, directory=directory)
    if not raw_name or raw_normalized != parsed_normalized:
        raise _fail("archive header name does not match its parsed member path.")
    if header[156:157] != member.type:
        raise _fail("archive header type does not match its parsed member type.")


def _package_member_kind(name: str, platform: str) -> tuple[str, str, str] | None:
    parts = name.split("/")
    if len(parts) == 2 and parts[0] == "pypi" and parts[1].endswith(".whl"):
        return "pypi", parts[1], "pypi"
    if (
        len(parts) == 3
        and parts[0] == "channel"
        and parts[1] in {platform, "noarch"}
        and parts[2].endswith((".conda", ".tar.bz2"))
    ):
        return "conda", parts[2], parts[1]
    return None


def _match_component(
    links: Sequence[_ComponentLink],
    *,
    digest: str,
    basename: str,
    ecosystem: str,
    subdir: str | None,
) -> _ComponentLink:
    candidates = [
        link
        for link in links
        if link.sha256 == digest
        and link.basename == basename
        and link.ecosystem == ecosystem
        and link.subdir == subdir
    ]
    if len(candidates) != 1:
        raise _fail(
            "publisher package does not map to exactly one inventory component."
        )
    return candidates[0]


def _hash_tar_member(handle: _BinaryReader, size: int) -> tuple[int, str]:
    digest = hashlib.sha256()
    total = 0
    while total < size:
        chunk = handle.read(min(_CHUNK_SIZE, size - total))
        if not chunk:
            raise _fail("archive member ended before its declared size.")
        total += len(chunk)
        digest.update(chunk)
    return total, digest.hexdigest()


def _inspect_tar(
    archive_path: Path,
    metadata: _ArtifactMetadata,
    links: Sequence[_ComponentLink],
) -> _TarInspection:
    archive_identity = _hash_regular_file(
        archive_path, _MAX_ARCHIVE_BYTES, "decoded archive"
    )
    if archive_identity.size <= 0 or archive_identity.size % 512:
        raise _fail("decoded archive size must be a positive 512-byte multiple.")

    metadata_names = frozenset(
        member.format(platform=metadata.platform) for member in _METADATA_MEMBERS
    )
    allowed_directories = frozenset(
        member.format(platform=metadata.platform) for member in _ALLOWED_DIRECTORIES
    )
    own_wheels = {
        f"pypi/{metadata.wheel.filename}": ("application_wheel", metadata.wheel)
    }
    expected_own_counts: Counter[str] = Counter({"application_wheel": 1})
    if metadata.companion_wheel is not None:
        own_wheels[f"pypi/{metadata.companion_wheel.filename}"] = (
            "companion_wheel",
            metadata.companion_wheel,
        )
        expected_own_counts["companion_wheel"] = 1
    own_counts: Counter[str] = Counter()
    metadata_counts: Counter[str] = Counter()
    classification_counts: Counter[str] = Counter()
    seen_names: set[str] = set()
    members: list[_TarMember] = []
    downloads: list[PublisherPackage] = []
    declared_payload = 0
    expected_offset = 0
    total_members = 0

    try:
        with (
            archive_path.open("rb") as raw_archive,
            archive_path.open("rb") as raw_headers,
            tarfile.open(fileobj=raw_archive, mode="r|", errorlevel=2) as archive,
        ):
            for member in archive:
                total_members += 1
                if total_members > _MAX_TAR_MEMBERS:
                    raise _fail("archive exceeds the member count limit.")
                if member.offset != expected_offset or member.pax_headers:
                    raise _fail("archive contains an unsupported extended tar header.")
                if member.offset_data % 512:
                    raise _fail("archive member payload is not 512-byte aligned.")
                _verify_raw_tar_header(raw_headers, member)
                is_directory = member.type == tarfile.DIRTYPE
                if member.type not in {
                    tarfile.REGTYPE,
                    tarfile.AREGTYPE,
                    tarfile.DIRTYPE,
                }:
                    raise _fail("archive contains a link or special member.")
                normalized = _safe_member_name(member.name, directory=is_directory)
                folded = normalized.casefold()
                if folded in seen_names:
                    raise _fail(
                        "archive contains duplicate case-insensitive member paths."
                    )
                seen_names.add(folded)
                size = _integer(member.size, "archive member size", minimum=0)
                padded_size = ((size + 511) // 512) * 512
                member_end = member.offset_data + padded_size
                if member_end > archive_identity.size:
                    raise _fail("archive member extends beyond the decoded archive.")
                expected_offset = member_end
                raw_headers.seek(member.offset_data + size)
                padding = raw_headers.read(padded_size - size)
                if len(padding) != padded_size - size or any(padding):
                    raise _fail("archive member has non-canonical payload padding.")

                if is_directory:
                    if size != 0 or normalized not in allowed_directories:
                        raise _fail("archive contains an unsupported directory member.")
                    classification = "directory"
                    members.append(
                        _TarMember(
                            normalized,
                            "directory",
                            classification,
                            0,
                            member.offset_data,
                            None,
                        )
                    )
                    classification_counts[classification] += 1
                    continue

                declared_payload += size
                if declared_payload > _MAX_DECLARED_PAYLOAD_BYTES:
                    raise _fail("archive declared payload exceeds the size limit.")
                extracted = archive.extractfile(member)
                if extracted is None:
                    raise _fail("archive regular member could not be read.")
                with extracted:
                    actual_size, digest = _hash_tar_member(extracted, size)
                    if actual_size != size or extracted.read(1):
                        raise _fail("archive member size does not match its header.")

                if normalized in metadata_names:
                    classification = "metadata"
                    metadata_counts[normalized] += 1
                elif normalized in own_wheels:
                    classification, expected_wheel = own_wheels[normalized]
                    if (
                        actual_size != expected_wheel.size
                        or digest != expected_wheel.sha256
                    ):
                        raise _fail(
                            "an owned wheel payload does not match artifact metadata."
                        )
                    own_counts[classification] += 1
                elif (
                    metadata.companion_wheel is None
                    and normalized.casefold().startswith(
                        "pypi/carlina_application_companions-"
                    )
                    and normalized.casefold().endswith(".whl")
                ):
                    raise _fail("archive contains an undeclared companion wheel.")
                else:
                    member_kind = _package_member_kind(normalized, metadata.platform)
                    if member_kind is None:
                        raise _fail("archive contains an unknown regular member.")
                    ecosystem, basename, subdir = member_kind
                    link = _match_component(
                        links,
                        digest=digest,
                        basename=basename,
                        ecosystem=ecosystem,
                        subdir=subdir if ecosystem == "conda" else None,
                    )
                    classification = (
                        f"publisher_conda_{subdir}"
                        if ecosystem == "conda"
                        else "publisher_pypi"
                    )
                    downloads.append(
                        PublisherPackage(
                            member_name=normalized,
                            offset_data=member.offset_data,
                            size=actual_size,
                            sha256=digest,
                            url=link.url,
                            component_ref=link.component_ref,
                        )
                    )

                members.append(
                    _TarMember(
                        normalized,
                        "regular",
                        classification,
                        actual_size,
                        member.offset_data,
                        digest,
                    )
                )
                classification_counts[classification] += 1
            raw_headers.seek(expected_offset)
            remaining = archive_identity.size - expected_offset
            if remaining < 1024:
                raise _fail("archive is missing its end-of-archive blocks.")
            while remaining:
                trailing = raw_headers.read(min(_CHUNK_SIZE, remaining))
                if not trailing or any(trailing):
                    raise _fail("archive contains an unsupported trailing tar record.")
                remaining -= len(trailing)

    except PublisherBootstrapPreparationError:
        raise
    except (tarfile.TarError, OSError, EOFError, ValueError) as exc:
        raise _fail(
            "decoded archive is not a supported streaming tar archive."
        ) from exc

    if own_counts != expected_own_counts:
        raise _fail("archive must contain exactly its metadata-owned wheel(s).")
    if metadata_counts != Counter(dict.fromkeys(metadata_names, 1)):
        raise _fail("archive must contain each required metadata member exactly once.")
    if not downloads:
        raise _fail("archive contains no publisher package payloads to replace.")
    downloads.sort(key=lambda package: package.offset_data)
    removed_payload_bytes = sum(package.size for package in downloads)
    return _TarInspection(
        members=tuple(members),
        downloads=tuple(downloads),
        original_archive=archive_identity,
        removed_payload_bytes=removed_payload_bytes,
        classification_counts=dict(sorted(classification_counts.items())),
    )


def _skip_line_breaks(handle: BinaryIO, offset: int, end: int) -> int:
    handle.seek(offset)
    skipped = 0
    while offset < end:
        byte = handle.read(1)
        if byte not in {b"\r", b"\n"}:
            handle.seek(-1, os.SEEK_CUR)
            return offset
        offset += 1
        skipped += 1
        if skipped > _MAX_HEADER_SCAN_BYTES:
            raise _fail("Pixi Pack framing contains too many line breaks.")
    return offset


def _find_bytes(handle: BinaryIO, needle: bytes, start: int, end: int) -> int:
    handle.seek(start)
    position = start
    overlap = b""
    while position < end:
        chunk = handle.read(min(_CHUNK_SIZE, end - position))
        if not chunk:
            break
        data = overlap + chunk
        found = data.find(needle)
        if found >= 0:
            return position - len(overlap) + found
        position += len(chunk)
        overlap = data[-(len(needle) - 1) :]
    raise _fail("Linux Pixi Pack artifact is missing its archive terminator.")


def _linux_payload_offsets(
    handle: BinaryIO, total_size: int
) -> tuple[int, int, int, int]:
    handle.seek(0)
    header = handle.read(min(total_size, _MAX_HEADER_SCAN_BYTES))
    marker_end: int | None = None
    consumed = 0
    for line in header.splitlines(keepends=True):
        if line.rstrip(b"\r\n") == _LINUX_HEADER_MARKER:
            if not line.endswith(b"\n"):
                raise _fail("Linux Pixi Pack header marker is not a complete line.")
            marker_end = consumed + len(line)
            break
        consumed += len(line)
    if marker_end is None:
        raise _fail("Linux Pixi Pack artifact is missing its header marker.")

    archive_start = _skip_line_breaks(handle, marker_end, total_size)
    archive_marker = _find_bytes(
        handle, _LINUX_ARCHIVE_MARKER, archive_start, total_size
    )
    archive_length = archive_marker - archive_start
    if archive_length <= 0 or archive_length % 4:
        raise _fail("Linux Pixi Pack archive Base64 is not aligned.")

    suffix_start = archive_marker + len(_LINUX_ARCHIVE_MARKER)
    suffix_start = _skip_line_breaks(handle, suffix_start, total_size)
    suffix_end = total_size
    trailing = 0
    while suffix_end > suffix_start and trailing <= _MAX_TRAILING_LINE_BREAKS:
        handle.seek(suffix_end - 1)
        last = handle.read(1)
        if last not in {b"\r", b"\n"}:
            break
        suffix_end -= 1
        trailing += 1
    if trailing > _MAX_TRAILING_LINE_BREAKS:
        raise _fail("Linux Pixi Pack suffix has too many trailing line breaks.")
    suffix_length = suffix_end - suffix_start
    if suffix_length <= 0 or suffix_length % 4:
        raise _fail("Linux Pixi Pack unpacker Base64 is not aligned.")
    return archive_start, archive_length, suffix_start, suffix_length


def _decode_base64_range(
    source: BinaryIO,
    offset: int,
    length: int,
    *,
    destination: BinaryIO | None,
    maximum_decoded_size: int,
    label: str,
) -> tuple[int, str]:
    if length <= 0 or length % 4:
        raise _fail(f"{label} Base64 range is not aligned.")
    source.seek(offset)
    remaining = length
    total = 0
    digest = hashlib.sha256()
    while remaining:
        requested = min(_CHUNK_SIZE, remaining)
        # The fixed chunk size and encoded range are both multiples of four.
        requested -= requested % 4
        if requested == 0:
            raise _fail(f"{label} Base64 range ended out of alignment.")
        encoded = source.read(requested)
        if len(encoded) != requested:
            raise _fail(f"{label} Base64 range ended unexpectedly.")
        remaining -= requested
        if remaining and b"=" in encoded:
            raise _fail(f"{label} Base64 has padding before its final chunk.")
        try:
            decoded = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise _fail(f"{label} Base64 is invalid.") from exc
        total += len(decoded)
        if total > maximum_decoded_size:
            raise _fail(f"{label} decoded payload exceeds its size limit.")
        digest.update(decoded)
        if destination is not None:
            destination.write(decoded)
    return total, digest.hexdigest()


def _decode_linux_pack(artifact_path: Path, scratch: Path, platform: str) -> Path:
    unpacker = _PINNED_UNPACKERS[platform]
    archive_path = scratch / "archive.tar"
    with _open_regular_file(artifact_path, "source pack") as (source, original_stat):
        offsets = _linux_payload_offsets(source, original_stat.size)
        archive_offset, archive_length, unpacker_offset, unpacker_length = offsets
        with archive_path.open("xb") as decoded_archive:
            archive_size, _archive_sha = _decode_base64_range(
                source,
                archive_offset,
                archive_length,
                destination=decoded_archive,
                maximum_decoded_size=_MAX_ARCHIVE_BYTES,
                label="Linux archive",
            )
            decoded_archive.flush()
            os.fsync(decoded_archive.fileno())
        unpacker_size, unpacker_sha = _decode_base64_range(
            source,
            unpacker_offset,
            unpacker_length,
            destination=None,
            maximum_decoded_size=unpacker.size,
            label="Linux unpacker",
        )
        if unpacker_size != unpacker.size or unpacker_sha != unpacker.sha256:
            raise _fail(
                "Linux Pixi unpacker does not match the pinned publisher bytes."
            )
        if _stat_identity(os.fstat(source.fileno())) != original_stat:
            raise _fail("source pack changed while its payloads were decoded.")
    if archive_size <= 0 or archive_size % 512:
        raise _fail("decoded Linux archive size is not a positive tar block multiple.")
    return archive_path


def _preflight_windows_bundle(path: Path, platform: str) -> None:
    unpacker = _PINNED_UNPACKERS[platform]
    try:
        with zipfile.ZipFile(path) as bundle:
            infos = bundle.infolist()
    except (OSError, zipfile.BadZipFile) as exc:
        raise _fail("Windows source pack is not a valid ZIP bundle.") from exc
    names = [info.filename for info in infos]
    if len(infos) != 3 or frozenset(names) != _WINDOWS_BUNDLE_MEMBERS:
        raise _fail(
            "Windows source pack must contain exactly the three bundle members."
        )
    by_name = {info.filename: info for info in infos}
    if any(
        info.compress_type != zipfile.ZIP_STORED or info.file_size != info.compress_size
        for info in infos
    ):
        raise _fail("Windows bundle members must be stored without compression.")
    if (
        by_name["archive.tar"].file_size > _MAX_ARCHIVE_BYTES
        or by_name["pixi-unpack.exe"].file_size != unpacker.size
        or by_name["bundle.json"].file_size > _WINDOWS_MANIFEST_LIMIT
    ):
        raise _fail(
            "Windows bundle declares a payload outside the supported size limits."
        )
    try:
        with zipfile.ZipFile(path) as bundle:
            manifest_bytes = bundle.read(WINDOWS_BUNDLE_MANIFEST)
        _parse_json(manifest_bytes, "Windows bundle manifest")
        parse_windows_bundle_manifest(manifest_bytes)
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        raise _fail("Windows bundle manifest is invalid.") from exc


def _decode_windows_pack(artifact_path: Path, scratch: Path, platform: str) -> Path:
    unpacker = _PINNED_UNPACKERS[platform]
    _preflight_windows_bundle(artifact_path, platform)
    extract_dir = scratch / "windows-bundle"
    extract_dir.mkdir()
    try:
        outputs = extract_windows_bundle(artifact_path, extract_dir)
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        raise _fail("Windows bundle validation failed.") from exc
    archive_path = outputs.get("archive.tar")
    unpacker_path = outputs.get("pixi-unpack.exe")
    if archive_path is None or unpacker_path is None:
        raise _fail("Windows bundle did not provide its required runtime payloads.")
    unpacker_identity = _hash_regular_file(
        unpacker_path,
        unpacker.size,
        "Windows unpacker",
    )
    if (
        unpacker_identity.size != unpacker.size
        or unpacker_identity.sha256 != unpacker.sha256
    ):
        raise _fail("Windows Pixi unpacker does not match the pinned publisher bytes.")
    archive_identity = _hash_regular_file(
        archive_path, _MAX_ARCHIVE_BYTES, "decoded archive"
    )
    if archive_identity.size <= 0 or archive_identity.size % 512:
        raise _fail(
            "decoded Windows archive size is not a positive tar block multiple."
        )
    return archive_path


def _create_skeleton(
    archive_path: Path,
    downloads: Sequence[PublisherPackage],
    output_dir: Path,
) -> tuple[Path, int, str]:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".skeleton.tar.gz.",
        suffix=".tmp",
        dir=output_dir,
    )
    temporary_path = Path(temporary_name)
    original_size = archive_path.stat().st_size
    try:
        with (
            os.fdopen(descriptor, "wb") as raw_output,
            archive_path.open("rb") as source,
        ):
            with gzip.GzipFile(
                filename="",
                mode="wb",
                compresslevel=6,
                fileobj=raw_output,
                mtime=0,
            ) as compressed:
                position = 0
                zeroes = bytes(_CHUNK_SIZE)
                for package in downloads:
                    if package.offset_data < position:
                        raise _fail(
                            "publisher package payload ranges overlap or are unordered."
                        )
                    _copy_range(
                        source, compressed, position, package.offset_data - position
                    )
                    source.seek(package.offset_data)
                    remaining = package.size
                    while remaining:
                        count = min(remaining, len(zeroes))
                        compressed.write(zeroes[:count])
                        remaining -= count
                    position = package.offset_data + package.size
                _copy_range(source, compressed, position, original_size - position)
            raw_output.flush()
            os.fsync(raw_output.fileno())
        identity = _hash_regular_file(temporary_path, 512 * 1024 * 1024, "skeleton")
        return temporary_path, identity.size, identity.sha256
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def _copy_range(
    source: BinaryIO, destination: _BinaryWriter, offset: int, length: int
) -> None:
    if length < 0:
        raise _fail("archive copy range is invalid.")
    source.seek(offset)
    remaining = length
    while remaining:
        chunk = source.read(min(_CHUNK_SIZE, remaining))
        if not chunk:
            raise _fail("decoded archive changed while the skeleton was written.")
        destination.write(chunk)
        remaining -= len(chunk)


def _json_bytes(value: object) -> bytes:
    try:
        return (
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise _fail("preparation summary could not be encoded as JSON.") from exc


def _write_temporary(output_dir: Path, prefix: str, payload: bytes) -> Path:
    descriptor, name = tempfile.mkstemp(prefix=prefix, suffix=".tmp", dir=output_dir)
    path = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        return path
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _publish_new_file(source: Path, destination: Path) -> _StatIdentity:
    try:
        os.link(source, destination)
    except FileExistsError as exc:
        raise _fail(
            "an output file appeared during preparation; it was preserved."
        ) from exc
    except OSError as exc:
        raise _fail("an output file could not be published atomically.") from exc
    info = destination.lstat()
    return _stat_identity(info)


def _unlink_if_ours(path: Path, identity: _StatIdentity) -> None:
    try:
        current = path.lstat()
    except OSError:
        return
    if _stat_identity(current) == identity:
        path.unlink(missing_ok=True)


def _write_prep_manifest(
    *,
    metadata: _ArtifactMetadata,
    metadata_filename: str,
    metadata_file: _FileIdentity,
    inventory_filename: str,
    inventory_file: _FileIdentity,
    pack_file: _FileIdentity,
    inspection: _TarInspection,
    plan_bytes: bytes,
    skeleton_size: int,
    skeleton_sha256: str,
) -> bytes:
    application: dict[str, object] = {
        "application_profile": _APPLICATION_PROFILE,
        "version": metadata.version,
        "platform": metadata.platform,
        "source_revision": metadata.source_revision,
        "wheel": {
            "filename": metadata.wheel.filename,
            "size": metadata.wheel.size,
            "sha256": metadata.wheel.sha256,
        },
    }
    if metadata.companion_wheel is not None:
        application["companion_wheel"] = {
            "filename": metadata.companion_wheel.filename,
            "size": metadata.companion_wheel.size,
            "sha256": metadata.companion_wheel.sha256,
        }
    members = [
        {
            "member_name": item.name,
            "type": item.member_type,
            "classification": item.classification,
            "size": item.size,
            "offset_data": item.offset_data,
            "sha256": item.sha256,
        }
        for item in inspection.members
    ]
    counts = Counter(item.classification for item in inspection.members)
    counts.update(
        {
            "tar_members": len(inspection.members),
            "regular_members": sum(
                item.member_type == "regular" for item in inspection.members
            ),
            "directory_members": sum(
                item.member_type == "directory" for item in inspection.members
            ),
            "publisher_packages": len(inspection.downloads),
            "removed_publisher_payload_bytes": inspection.removed_payload_bytes,
        }
    )
    payload = {
        "format": PUBLISHER_BOOTSTRAP_PREPARATION_FORMAT,
        "schema_version": PUBLISHER_BOOTSTRAP_PREPARATION_SCHEMA_VERSION,
        "application": application,
        "inputs": {
            "artifact_metadata": {
                "filename": metadata_filename,
                "size": metadata_file.size,
                "sha256": metadata_file.sha256,
            },
            "inventory": {
                "filename": inventory_filename,
                "size": inventory_file.size,
                "sha256": inventory_file.sha256,
            },
            "source_pack": {
                "filename": metadata.filename,
                "size": pack_file.size,
                "sha256": pack_file.sha256,
            },
        },
        "original_archive": {
            "size": inspection.original_archive.size,
            "sha256": inspection.original_archive.sha256,
        },
        "outputs": {
            "plan": {
                "filename": "plan.json",
                "size": len(plan_bytes),
                "sha256": hashlib.sha256(plan_bytes).hexdigest(),
            },
            "skeleton": {
                "filename": BOOTSTRAP_SKELETON_FILENAME,
                "size": skeleton_size,
                "sha256": skeleton_sha256,
                "uncompressed_size": inspection.original_archive.size,
            },
        },
        "member_counts": dict(sorted(counts.items())),
        "classification_counts": dict(inspection.classification_counts),
        "removed_publisher_payload_bytes": inspection.removed_payload_bytes,
        "members": members,
    }
    return _json_bytes(payload)


def _verify_input_unchanged(
    path: Path,
    original: _FileIdentity,
    limit: int,
    label: str,
) -> None:
    current = _hash_regular_file(path, limit, label)
    if not _same_file_identity(original, current):
        raise _fail(f"{label} input changed during preparation.")


def _verify_all_inputs(
    metadata_path: Path,
    metadata_file: _FileIdentity,
    inventory_path: Path,
    inventory_file: _FileIdentity,
    artifact_path: Path,
    artifact_file: _FileIdentity,
) -> None:
    _verify_input_unchanged(
        metadata_path,
        metadata_file,
        _MAX_METADATA_BYTES,
        "artifact metadata",
    )
    _verify_input_unchanged(
        inventory_path,
        inventory_file,
        _MAX_INVENTORY_BYTES,
        "inventory",
    )
    _verify_input_unchanged(
        artifact_path,
        artifact_file,
        _MAX_SOURCE_PACK_BYTES,
        "source pack",
    )


def prepare_publisher_bootstrap(
    artifact_metadata: Path,
    inventory: Path,
    output_dir: Path,
) -> tuple[Path, Path, Path]:
    """Prepare plan, deterministic skeleton and an identity-bound receipt."""

    metadata_path = Path(artifact_metadata)
    inventory_path = Path(inventory)
    destination_dir = Path(output_dir)

    created_output_dir = False
    temporary_paths: set[Path] = set()
    published_files: dict[Path, _StatIdentity] = {}
    try:
        try:
            destination_dir.mkdir()
        except FileExistsError as exc:
            raise _fail(
                "output directory already exists; it will not be overwritten."
            ) from exc
        except OSError as exc:
            raise _fail("new output directory could not be created.") from exc
        created_output_dir = True

        metadata, metadata_file = _parse_artifact_metadata(metadata_path)
        inventory_data, inventory_file, component_links = _inventory_identity(
            inventory_path,
            metadata,
        )
        del inventory_data
        artifact_path = metadata_path.parent / metadata.filename
        artifact_file = _hash_regular_file(
            artifact_path,
            _MAX_SOURCE_PACK_BYTES,
            "source pack",
        )
        if (
            artifact_file.size != metadata.size
            or artifact_file.sha256 != metadata.sha256
        ):
            raise _fail("source pack size or SHA-256 does not match artifact metadata.")

        with tempfile.TemporaryDirectory(
            prefix=".carlina-publisher-bootstrap-", dir=destination_dir
        ) as scratch_name:
            scratch = Path(scratch_name)
            if metadata.platform == "linux-64":
                archive_path = _decode_linux_pack(
                    artifact_path, scratch, metadata.platform
                )
            else:
                archive_path = _decode_windows_pack(
                    artifact_path, scratch, metadata.platform
                )

            inspection = _inspect_tar(archive_path, metadata, component_links)
            unpacker_pin = _PINNED_UNPACKERS[metadata.platform]
            skeleton_temp, skeleton_size, skeleton_sha256 = _create_skeleton(
                archive_path,
                inspection.downloads,
                destination_dir,
            )
            temporary_paths.add(skeleton_temp)
            plan = PublisherBootstrapPlan(
                platform=metadata.platform,
                application_profile=_APPLICATION_PROFILE,
                version=metadata.version,
                archive=HashedPayload(
                    size=inspection.original_archive.size,
                    sha256=inspection.original_archive.sha256,
                ),
                skeleton=BootstrapSkeleton(
                    filename=BOOTSTRAP_SKELETON_FILENAME,
                    size=skeleton_size,
                    sha256=skeleton_sha256,
                    uncompressed_size=inspection.original_archive.size,
                ),
                downloads=inspection.downloads,
                unpacker=PublisherUnpacker(
                    name=unpacker_pin.name,
                    url=unpacker_pin.url,
                    size=unpacker_pin.size,
                    sha256=unpacker_pin.sha256,
                ),
            )
            try:
                validated_plan = parse_publisher_bootstrap_plan(
                    {
                        "format": PUBLISHER_BOOTSTRAP_FORMAT,
                        "schema_version": PUBLISHER_BOOTSTRAP_SCHEMA_VERSION,
                        "platform": plan.platform,
                        "application_profile": plan.application_profile,
                        "version": plan.version,
                        "archive": {
                            "size": plan.archive.size,
                            "sha256": plan.archive.sha256,
                        },
                        "skeleton": {
                            "filename": plan.skeleton.filename,
                            "size": plan.skeleton.size,
                            "sha256": plan.skeleton.sha256,
                            "uncompressed_size": plan.skeleton.uncompressed_size,
                        },
                        "downloads": [
                            {
                                "member_name": item.member_name,
                                "offset_data": item.offset_data,
                                "size": item.size,
                                "sha256": item.sha256,
                                "url": item.url,
                                "component_ref": item.component_ref,
                            }
                            for item in plan.downloads
                        ],
                        "unpacker": {
                            "name": plan.unpacker.name,
                            "url": plan.unpacker.url,
                            "size": plan.unpacker.size,
                            "sha256": plan.unpacker.sha256,
                        },
                    }
                )
                plan_bytes = publisher_bootstrap_plan_bytes(validated_plan)
                parse_publisher_bootstrap_plan(plan_bytes)
            except PublisherBootstrapError as exc:
                raise _fail("generated plan failed core contract validation.") from exc

            plan_temp = _write_temporary(destination_dir, ".plan.json.", plan_bytes)
            temporary_paths.add(plan_temp)
            preparation_bytes = _write_prep_manifest(
                metadata=metadata,
                metadata_filename=metadata_path.name,
                metadata_file=metadata_file,
                inventory_filename=inventory_path.name,
                inventory_file=inventory_file,
                pack_file=artifact_file,
                inspection=inspection,
                plan_bytes=plan_bytes,
                skeleton_size=skeleton_size,
                skeleton_sha256=skeleton_sha256,
            )
            prep_temp = _write_temporary(
                destination_dir, ".preparation.json.", preparation_bytes
            )
            temporary_paths.add(prep_temp)

            _verify_all_inputs(
                metadata_path,
                metadata_file,
                inventory_path,
                inventory_file,
                artifact_path,
                artifact_file,
            )

            plan_path = destination_dir / "plan.json"
            skeleton_path = destination_dir / BOOTSTRAP_SKELETON_FILENAME
            preparation_path = destination_dir / "preparation.json"
            for source, destination in (
                (skeleton_temp, skeleton_path),
                (plan_temp, plan_path),
                (prep_temp, preparation_path),
            ):
                published_files[destination] = _publish_new_file(source, destination)
                source.unlink()
                current_identity = _stat_identity(destination.lstat())
                published_identity = published_files[destination]
                if (
                    current_identity.device != published_identity.device
                    or current_identity.inode != published_identity.inode
                    or current_identity.mode != published_identity.mode
                    or current_identity.size != published_identity.size
                    or current_identity.modified_ns != published_identity.modified_ns
                ):
                    raise _fail("an output file changed during publication.")
                published_files[destination] = current_identity
                temporary_paths.discard(source)

            _verify_all_inputs(
                metadata_path,
                metadata_file,
                inventory_path,
                inventory_file,
                artifact_path,
                artifact_file,
            )
            return plan_path, skeleton_path, preparation_path
    except BaseException:
        for path in temporary_paths:
            path.unlink(missing_ok=True)
        for path, identity in published_files.items():
            _unlink_if_ours(path, identity)
        if created_output_dir:
            with suppress(OSError):
                destination_dir.rmdir()
        raise


def _summary(paths: tuple[Path, Path, Path]) -> dict[str, str]:
    plan, skeleton, preparation = paths
    return {
        "plan": str(plan),
        "skeleton": str(skeleton),
        "preparation": str(preparation),
    }


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-metadata", required=True, type=Path)
    parser.add_argument("--inventory", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None, *, stdout: TextIO | None = None) -> int:
    args = _argument_parser().parse_args(argv)
    output = stdout if stdout is not None else sys.stdout
    try:
        paths = prepare_publisher_bootstrap(
            args.artifact_metadata,
            args.inventory,
            args.output_dir,
        )
    except (PublisherBootstrapPreparationError, PublisherBootstrapError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except OSError:
        print(
            "publisher bootstrap preparation failed because a file operation failed.",
            file=sys.stderr,
        )
        return 2
    print(json.dumps(_summary(paths), sort_keys=True), file=output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
