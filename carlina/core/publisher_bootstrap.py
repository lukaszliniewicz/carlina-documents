"""Closed, offline reconstruction of publisher-pinned Pixi archives."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import stat
import tempfile
import unicodedata
import zlib
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Protocol, cast
from urllib.parse import unquote, urlsplit

from packaging.version import InvalidVersion, Version

PUBLISHER_BOOTSTRAP_FORMAT = "carlina.publisher-bootstrap.plan"
PUBLISHER_BOOTSTRAP_SCHEMA_VERSION = 1
DOCUMENTS_APPLICATION_PROFILE = "documents"
BOOTSTRAP_SKELETON_FILENAME = "skeleton.tar.gz"

_CHUNK_SIZE = 1024 * 1024
_MAX_JSON_BYTES = 1024 * 1024
_MAX_DOWNLOADS = 512
_MAX_ARCHIVE_SIZE = 4 * 1024 * 1024 * 1024
_MAX_SKELETON_SIZE = 512 * 1024 * 1024
_MAX_PACKAGE_SIZE = 1024 * 1024 * 1024
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_ENCODED_SEPARATOR = re.compile(r"%(?:25)*(?:2f|5c)", re.IGNORECASE)
_ENCODED_DOT_COMPONENT = re.compile(
    r"(?:\.|%(?:25)*2e){1,2}\Z",
    re.IGNORECASE,
)
_URL_HOSTS = frozenset({"conda.anaconda.org", "files.pythonhosted.org"})


class PublisherBootstrapError(ValueError):
    """A publisher bootstrap plan or one of its inputs is invalid."""


@dataclass(frozen=True, slots=True)
class HashedPayload:
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class BootstrapSkeleton:
    filename: str
    size: int
    sha256: str
    uncompressed_size: int


@dataclass(frozen=True, slots=True)
class PublisherPackage:
    member_name: str
    offset_data: int
    size: int
    sha256: str
    url: str
    component_ref: str


@dataclass(frozen=True, slots=True)
class PublisherUnpacker:
    name: str
    url: str
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class PublisherBootstrapPlan:
    platform: str
    application_profile: str
    version: str
    archive: HashedPayload
    skeleton: BootstrapSkeleton
    downloads: tuple[PublisherPackage, ...]
    unpacker: PublisherUnpacker


class _GzipDecoder(Protocol):
    @property
    def eof(self) -> bool: ...

    @property
    def unused_data(self) -> bytes: ...

    @property
    def unconsumed_tail(self) -> bytes: ...

    def decompress(self, data: bytes, /, max_length: int = 0) -> bytes: ...


_UNPACKER_POLICY: dict[str, PublisherUnpacker] = {
    "linux-64": PublisherUnpacker(
        name="pixi-unpack-x86_64-unknown-linux-musl",
        url=(
            "https://github.com/Quantco/pixi-pack/releases/download/v0.7.10/"
            "pixi-unpack-x86_64-unknown-linux-musl"
        ),
        size=15668176,
        sha256="1e610ea67baae58119de8aa52cf238f6efea7e0c68ea3a8a295c8a2630a5df04",
    ),
    "win-64": PublisherUnpacker(
        name="pixi-unpack-x86_64-pc-windows-msvc.exe",
        url=(
            "https://github.com/Quantco/pixi-pack/releases/download/v0.7.10/"
            "pixi-unpack-x86_64-pc-windows-msvc.exe"
        ),
        size=15029248,
        sha256="f0409bf7ba71ade130bc2f74aca2c7a923af207f7cfa40bad82b3bff2d20bd8b",
    ),
}


def _fail(message: str) -> PublisherBootstrapError:
    return PublisherBootstrapError(message)


def _require_mapping(
    value: object,
    expected: frozenset[str],
    label: str,
) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise _fail(f"{label} must be an object.")
    typed_value = cast(Mapping[object, object], value)
    try:
        items: list[tuple[object, object]] = list(typed_value.items())
    except Exception as exc:
        raise _fail(f"{label} could not be read as an object.") from exc
    result: dict[str, object] = {}
    for key, item in items:
        if type(key) is not str:
            raise _fail(f"{label} keys must be strings.")
        if key in result:
            raise _fail(f"{label} contains duplicate keys.")
        result[key] = item
    actual = frozenset(result)
    if actual != expected:
        raise _fail(f"{label} has unknown or missing fields.")
    return result


def _require_string(value: object, label: str) -> str:
    if type(value) is not str:
        raise _fail(f"{label} must be a string.")
    return value


def _require_integer(value: object, label: str) -> int:
    if type(value) is not int:
        raise _fail(f"{label} must be an integer.")
    return value


def _require_list(value: object, label: str) -> list[object]:
    if type(value) is not list:
        raise _fail(f"{label} must be a JSON array.")
    return cast(list[object], value)


def _require_path(value: object, label: str) -> Path:
    if not isinstance(value, Path):
        raise _fail(f"{label} path must be a pathlib.Path.")
    return value


def _require_payload_mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise _fail("payload must be JSON bytes, text, or a mapping.")
    return cast(Mapping[str, object], value)


def _require_sha256(value: object, label: str) -> str:
    result = _require_string(value, label)
    if _SHA256.fullmatch(result) is None:
        raise _fail(f"{label} must be a lowercase SHA-256 digest.")
    return result


def _has_control(value: str) -> bool:
    return any(unicodedata.category(character) == "Cc" for character in value)


def _validated_member_kind(
    member_name: str,
    platform: str,
) -> tuple[str, str]:
    if (
        not member_name
        or member_name.startswith(("/", "\\"))
        or "\\" in member_name
        or "\x00" in member_name
        or _has_control(member_name)
    ):
        raise _fail("download member_name is not a safe POSIX path.")
    parts = member_name.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise _fail("download member_name contains an unsafe path segment.")
    if len(parts) == 2 and parts[0] == "pypi" and parts[1].endswith(".whl"):
        basename = parts[1]
        if basename == ".whl":
            raise _fail("download member_name must name a package file.")
        return "wheel", basename
    if (
        len(parts) == 3
        and parts[0] == "channel"
        and parts[1] in {platform, "noarch"}
        and parts[2].endswith((".conda", ".tar.bz2"))
    ):
        basename = parts[2]
        if basename in {".conda", ".tar.bz2"}:
            raise _fail("download member_name must name a package file.")
        return parts[1], basename
    raise _fail("download member_name is outside the supported package paths.")


def _decoded_url_path(path: str) -> tuple[str, ...]:
    if not path.startswith("/"):
        raise _fail("download URL must contain an absolute publisher path.")
    raw_parts = path[1:].split("/")
    if not raw_parts or any(part == "" for part in raw_parts):
        raise _fail("download URL contains an empty path segment.")
    decoded_parts: list[str] = []
    for part in raw_parts:
        if _ENCODED_SEPARATOR.search(part) is not None:
            raise _fail("download URL contains an encoded path separator.")
        if _ENCODED_DOT_COMPONENT.fullmatch(part) is not None:
            raise _fail("download URL contains an encoded traversal segment.")
        try:
            decoded = unquote(part, errors="strict")
        except UnicodeDecodeError as exc:
            raise _fail("download URL contains invalid encoded text.") from exc
        if decoded in {".", ".."} or "/" in decoded or "\\" in decoded:
            raise _fail("download URL contains an unsafe path segment.")
        if any(character.isspace() for character in decoded) or _has_control(decoded):
            raise _fail("download URL contains whitespace or control characters.")
        decoded_parts.append(decoded)
    return tuple(decoded_parts)


def _validate_package_url(
    url: str,
    platform: str,
    member_name: str,
    member_kind: str,
    basename: str,
) -> None:
    if (
        not url
        or "\\" in url
        or "?" in url
        or "#" in url
        or any(character.isspace() for character in url)
        or _has_control(url)
    ):
        raise _fail("download URL contains a forbidden character or component.")
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        explicit_port = parsed.port
    except ValueError as exc:
        raise _fail("download URL is malformed.") from exc
    if (
        parsed.scheme != "https"
        or hostname not in _URL_HOSTS
        or parsed.netloc != hostname
        or parsed.username is not None
        or parsed.password is not None
        or explicit_port is not None
        or parsed.query
        or parsed.fragment
    ):
        raise _fail("download URL must use an allowed HTTPS publisher host.")
    decoded_parts = _decoded_url_path(parsed.path)
    if decoded_parts[-1] != basename:
        raise _fail("download URL basename does not match member_name.")
    if member_kind == "wheel":
        if hostname != "files.pythonhosted.org" or decoded_parts[0] != "packages":
            raise _fail("wheel URLs must use the Python package publisher path.")
        if len(decoded_parts) < 2:
            raise _fail("wheel URL is missing its package basename.")
        return
    if hostname != "conda.anaconda.org":
        raise _fail("conda package URLs must use the conda publisher host.")
    if (
        len(decoded_parts) != 3
        or decoded_parts[0] != "conda-forge"
        or decoded_parts[1] != member_kind
        or member_kind not in {platform, "noarch"}
    ):
        raise _fail("conda URL path must match the package member subdirectory.")
    if member_name != f"channel/{member_kind}/{basename}":
        raise _fail("conda URL path does not match member_name.")


def _validate_plan(plan: object) -> PublisherBootstrapPlan:
    if type(plan) is not PublisherBootstrapPlan:
        raise _fail("plan must be a PublisherBootstrapPlan.")
    platform = _require_string(plan.platform, "platform")
    if platform not in _UNPACKER_POLICY:
        raise _fail("platform is unsupported.")
    profile = _require_string(plan.application_profile, "application_profile")
    if profile != DOCUMENTS_APPLICATION_PROFILE:
        raise _fail("application_profile is unsupported.")
    version = _require_string(plan.version, "version")
    try:
        parsed_version = Version(version)
    except (InvalidVersion, ValueError) as exc:
        raise _fail("version must be a canonical PEP 440 version.") from exc
    if str(parsed_version) != version:
        raise _fail("version must be a canonical PEP 440 version.")

    if type(plan.archive) is not HashedPayload:
        raise _fail("archive must be a HashedPayload.")
    archive_size = _require_integer(plan.archive.size, "archive.size")
    if archive_size <= 0 or archive_size > _MAX_ARCHIVE_SIZE or archive_size % 512:
        raise _fail("archive.size is outside the supported archive bounds.")
    _require_sha256(plan.archive.sha256, "archive.sha256")

    if type(plan.skeleton) is not BootstrapSkeleton:
        raise _fail("skeleton must be a BootstrapSkeleton.")
    filename = _require_string(plan.skeleton.filename, "skeleton.filename")
    if filename != BOOTSTRAP_SKELETON_FILENAME:
        raise _fail("skeleton.filename is unsupported.")
    skeleton_size = _require_integer(plan.skeleton.size, "skeleton.size")
    if skeleton_size <= 0 or skeleton_size > _MAX_SKELETON_SIZE:
        raise _fail("skeleton.size is outside the supported bounds.")
    _require_sha256(plan.skeleton.sha256, "skeleton.sha256")
    uncompressed_size = _require_integer(
        plan.skeleton.uncompressed_size,
        "skeleton.uncompressed_size",
    )
    if uncompressed_size != archive_size:
        raise _fail("skeleton.uncompressed_size must equal archive.size.")

    if type(plan.downloads) is not tuple:
        raise _fail("downloads must be a tuple of PublisherPackage values.")
    if not plan.downloads or len(plan.downloads) > _MAX_DOWNLOADS:
        raise _fail("downloads count is outside the supported bounds.")
    seen_members: set[str] = set()
    seen_components: set[str] = set()
    previous_offset = 0
    previous_end = 0
    for index, package in enumerate(plan.downloads):
        label = f"downloads[{index}]"
        if type(package) is not PublisherPackage:
            raise _fail(f"{label} must be a PublisherPackage.")
        member_name = _require_string(package.member_name, f"{label}.member_name")
        member_kind, basename = _validated_member_kind(member_name, platform)
        folded_member = member_name.casefold()
        if folded_member in seen_members:
            raise _fail(
                "download member_name values must be unique case-insensitively."
            )
        seen_members.add(folded_member)
        offset = _require_integer(package.offset_data, f"{label}.offset_data")
        if offset <= 0 or offset % 512:
            raise _fail(f"{label}.offset_data must be a positive 512-byte boundary.")
        size = _require_integer(package.size, f"{label}.size")
        if size <= 0 or size > _MAX_PACKAGE_SIZE:
            raise _fail(f"{label}.size is outside the supported bounds.")
        end = offset + size
        if end > archive_size:
            raise _fail(f"{label} range extends beyond archive.size.")
        if offset < previous_offset:
            raise _fail("download ranges must be ordered by offset_data.")
        if offset < previous_end:
            raise _fail("download ranges must not overlap.")
        previous_offset = offset
        previous_end = end
        _require_sha256(package.sha256, f"{label}.sha256")
        url = _require_string(package.url, f"{label}.url")
        component_ref = _require_string(
            package.component_ref,
            f"{label}.component_ref",
        )
        if not component_ref or len(component_ref) > 512 or _has_control(component_ref):
            raise _fail(f"{label}.component_ref is invalid.")
        if component_ref in seen_components:
            raise _fail("download component_ref values must be unique.")
        seen_components.add(component_ref)
        _validate_package_url(url, platform, member_name, member_kind, basename)

    expected_unpacker = _UNPACKER_POLICY[platform]
    if type(plan.unpacker) is not PublisherUnpacker:
        raise _fail("unpacker must be a PublisherUnpacker.")
    if plan.unpacker != expected_unpacker:
        raise _fail("unpacker metadata does not match the pinned platform policy.")
    return plan


def _mapping_to_plan(payload: object) -> PublisherBootstrapPlan:
    top = _require_mapping(
        payload,
        frozenset(
            {
                "format",
                "schema_version",
                "platform",
                "application_profile",
                "version",
                "archive",
                "skeleton",
                "downloads",
                "unpacker",
            }
        ),
        "plan",
    )
    if _require_string(top["format"], "format") != PUBLISHER_BOOTSTRAP_FORMAT:
        raise _fail("plan format is unsupported.")
    if (
        _require_integer(top["schema_version"], "schema_version")
        != PUBLISHER_BOOTSTRAP_SCHEMA_VERSION
    ):
        raise _fail("plan schema_version is unsupported.")

    archive_raw = _require_mapping(
        top["archive"],
        frozenset({"size", "sha256"}),
        "archive",
    )
    skeleton_raw = _require_mapping(
        top["skeleton"],
        frozenset({"filename", "size", "sha256", "uncompressed_size"}),
        "skeleton",
    )
    downloads_raw = _require_list(top["downloads"], "downloads")
    if not downloads_raw or len(downloads_raw) > _MAX_DOWNLOADS:
        raise _fail("downloads count is outside the supported bounds.")
    downloads: list[PublisherPackage] = []
    package_fields = frozenset(
        {"member_name", "offset_data", "size", "sha256", "url", "component_ref"}
    )
    for index, item in enumerate(downloads_raw):
        package = _require_mapping(item, package_fields, f"downloads[{index}]")
        downloads.append(
            PublisherPackage(
                member_name=_require_string(
                    package["member_name"],
                    f"downloads[{index}].member_name",
                ),
                offset_data=_require_integer(
                    package["offset_data"],
                    f"downloads[{index}].offset_data",
                ),
                size=_require_integer(package["size"], f"downloads[{index}].size"),
                sha256=_require_sha256(
                    package["sha256"],
                    f"downloads[{index}].sha256",
                ),
                url=_require_string(package["url"], f"downloads[{index}].url"),
                component_ref=_require_string(
                    package["component_ref"],
                    f"downloads[{index}].component_ref",
                ),
            )
        )
    unpacker_raw = _require_mapping(
        top["unpacker"],
        frozenset({"name", "url", "size", "sha256"}),
        "unpacker",
    )
    plan = PublisherBootstrapPlan(
        platform=_require_string(top["platform"], "platform"),
        application_profile=_require_string(
            top["application_profile"],
            "application_profile",
        ),
        version=_require_string(top["version"], "version"),
        archive=HashedPayload(
            size=_require_integer(archive_raw["size"], "archive.size"),
            sha256=_require_sha256(archive_raw["sha256"], "archive.sha256"),
        ),
        skeleton=BootstrapSkeleton(
            filename=_require_string(skeleton_raw["filename"], "skeleton.filename"),
            size=_require_integer(skeleton_raw["size"], "skeleton.size"),
            sha256=_require_sha256(skeleton_raw["sha256"], "skeleton.sha256"),
            uncompressed_size=_require_integer(
                skeleton_raw["uncompressed_size"],
                "skeleton.uncompressed_size",
            ),
        ),
        downloads=tuple(downloads),
        unpacker=PublisherUnpacker(
            name=_require_string(unpacker_raw["name"], "unpacker.name"),
            url=_require_string(unpacker_raw["url"], "unpacker.url"),
            size=_require_integer(unpacker_raw["size"], "unpacker.size"),
            sha256=_require_sha256(unpacker_raw["sha256"], "unpacker.sha256"),
        ),
    )
    return _validate_plan(plan)


def _plan_mapping(plan: PublisherBootstrapPlan) -> dict[str, object]:
    return {
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
                "member_name": package.member_name,
                "offset_data": package.offset_data,
                "size": package.size,
                "sha256": package.sha256,
                "url": package.url,
                "component_ref": package.component_ref,
            }
            for package in plan.downloads
        ],
        "unpacker": {
            "name": plan.unpacker.name,
            "url": plan.unpacker.url,
            "size": plan.unpacker.size,
            "sha256": plan.unpacker.sha256,
        },
    }


def _canonical_json_bytes(value: object) -> bytes:
    encoder = json.JSONEncoder(
        allow_nan=False,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    pieces: list[str] = []
    encoded_size = 0
    try:
        for piece in encoder.iterencode(value):
            if len(piece) > _MAX_JSON_BYTES:
                raise _fail("encoded plan JSON exceeds the 1 MiB limit.")
            encoded_piece = piece.encode("utf-8")
            encoded_size += len(encoded_piece)
            if encoded_size > _MAX_JSON_BYTES:
                raise _fail("encoded plan JSON exceeds the 1 MiB limit.")
            pieces.append(piece)
    except PublisherBootstrapError:
        raise
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError) as exc:
        raise _fail("plan could not be encoded as canonical JSON.") from exc
    encoded_size += 1
    if encoded_size > _MAX_JSON_BYTES:
        raise _fail("encoded plan JSON exceeds the 1 MiB limit.")
    return ("".join(pieces) + "\n").encode("utf-8")


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _fail("plan JSON contains duplicate object keys.")
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> object:
    raise _fail("plan JSON contains a non-finite number.")


def parse_publisher_bootstrap_plan(
    payload: bytes | str | Mapping[str, object],
) -> PublisherBootstrapPlan:
    """Parse and fully validate a closed publisher bootstrap plan."""

    payload_value: object = payload
    mapping_input = False
    if isinstance(payload_value, bytes):
        if len(payload_value) > _MAX_JSON_BYTES:
            raise _fail("encoded plan JSON exceeds the 1 MiB limit.")
        try:
            raw_text = payload_value.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise _fail("plan JSON must be UTF-8.") from exc
        try:
            raw = json.loads(
                raw_text,
                object_pairs_hook=_reject_duplicate_json_keys,
                parse_constant=_reject_json_constant,
            )
        except PublisherBootstrapError:
            raise
        except (json.JSONDecodeError, RecursionError, ValueError) as exc:
            raise _fail("plan JSON is invalid.") from exc
    elif isinstance(payload_value, str):
        try:
            encoded = payload_value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise _fail("plan JSON must be valid UTF-8 text.") from exc
        if len(encoded) > _MAX_JSON_BYTES:
            raise _fail("encoded plan JSON exceeds the 1 MiB limit.")
        try:
            raw = json.loads(
                payload_value,
                object_pairs_hook=_reject_duplicate_json_keys,
                parse_constant=_reject_json_constant,
            )
        except PublisherBootstrapError:
            raise
        except (json.JSONDecodeError, RecursionError, ValueError) as exc:
            raise _fail("plan JSON is invalid.") from exc
    else:
        raw = _require_payload_mapping(payload_value)
        mapping_input = True

    plan = _mapping_to_plan(raw)
    if mapping_input:
        _canonical_json_bytes(_plan_mapping(plan))
    return plan


def publisher_bootstrap_plan_bytes(plan: PublisherBootstrapPlan) -> bytes:
    """Serialize a validated plan as canonical sorted, indented JSON plus LF."""

    validated = _validate_plan(plan)
    return _canonical_json_bytes(_plan_mapping(validated))


def _is_regular_non_reparse(metadata: os.stat_result) -> bool:
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    file_attributes = getattr(metadata, "st_file_attributes", 0)
    return (
        stat.S_ISREG(metadata.st_mode)
        and not stat.S_ISLNK(metadata.st_mode)
        and not (file_attributes & reparse_flag)
    )


def _same_file_snapshot(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        os.path.samestat(left, right)
        and left.st_mode == right.st_mode
        and left.st_size == right.st_size
        and left.st_mtime_ns == right.st_mtime_ns
        and left.st_ctime_ns == right.st_ctime_ns
    )


@contextmanager
def _open_regular_input(
    path: Path,
    label: str,
) -> Generator[tuple[BinaryIO, os.stat_result]]:
    checked_path = _require_path(path, label)
    try:
        before_open = checked_path.lstat()
    except FileNotFoundError as exc:
        raise _fail(f"{label} input file is missing.") from exc
    if not _is_regular_non_reparse(before_open):
        raise _fail(f"{label} input must be a regular non-reparse file.")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(checked_path, flags)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise _fail(f"{label} input must not be a symbolic link.") from exc
        raise
    try:
        opened = os.fstat(descriptor)
        if not _is_regular_non_reparse(opened) or not _same_file_snapshot(
            before_open,
            opened,
        ):
            raise _fail(f"{label} input changed while it was opened.")
        try:
            after_open = checked_path.lstat()
        except FileNotFoundError as exc:
            raise _fail(f"{label} input path changed while it was opened.") from exc
        if not _is_regular_non_reparse(after_open) or not _same_file_snapshot(
            opened,
            after_open,
        ):
            raise _fail(f"{label} input path changed while it was opened.")
        handle = os.fdopen(descriptor, "rb", buffering=0)
        descriptor = -1
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    try:
        yield handle, opened
    finally:
        handle.close()


def _verify_input_unchanged(
    path: Path,
    handle: BinaryIO,
    original: os.stat_result,
    label: str,
) -> None:
    current = os.fstat(handle.fileno())
    if not _is_regular_non_reparse(current) or not _same_file_snapshot(
        original,
        current,
    ):
        raise _fail(f"{label} input changed while it was read.")
    try:
        current_path = path.lstat()
    except FileNotFoundError as exc:
        raise _fail(f"{label} input path changed while it was read.") from exc
    if not _is_regular_non_reparse(current_path) or not _same_file_snapshot(
        original,
        current_path,
    ):
        raise _fail(f"{label} input path changed while it was read.")


def _hash_exact_input(
    path: Path,
    expected_size: int,
    expected_sha256: str,
    label: str,
) -> os.stat_result:
    with _open_regular_input(path, label) as (source, original):
        if original.st_size != expected_size:
            raise _fail(f"{label} input size does not match the plan.")
        digest = hashlib.sha256()
        total = 0
        while total < expected_size:
            chunk = source.read(min(_CHUNK_SIZE, expected_size - total))
            if not chunk:
                raise _fail(f"{label} input ended before its planned size.")
            digest.update(chunk)
            total += len(chunk)
        if source.read(1):
            raise _fail(f"{label} input exceeds its planned size.")
        if digest.hexdigest() != expected_sha256:
            raise _fail(f"{label} input SHA-256 does not match the plan.")
        _verify_input_unchanged(path, source, original, label)
        return original


def _write_all(destination: BinaryIO, data: bytes, label: str) -> None:
    written = destination.write(data)
    if written != len(data):
        raise OSError(f"Could not write the complete {label} data.")


def _decompress_skeleton(
    source: BinaryIO,
    destination: BinaryIO,
    skeleton: BootstrapSkeleton,
) -> None:
    source.seek(0)
    digest = hashlib.sha256()
    compressed_size = 0
    expanded_size = 0
    decoder: _GzipDecoder | None = None
    saw_member = False

    while chunk := source.read(_CHUNK_SIZE):
        compressed_size += len(chunk)
        if compressed_size > skeleton.size:
            raise _fail("skeleton input exceeds its planned size.")
        digest.update(chunk)
        pending = chunk
        while pending:
            if decoder is None:
                try:
                    decoder = zlib.decompressobj(wbits=16 + zlib.MAX_WBITS)
                except zlib.error as exc:
                    raise _fail("skeleton gzip stream is invalid.") from exc
            active_decoder = decoder
            output_limit = min(
                _CHUNK_SIZE,
                skeleton.uncompressed_size - expanded_size + 1,
            )
            try:
                expanded = active_decoder.decompress(pending, output_limit)
            except zlib.error as exc:
                raise _fail("skeleton gzip stream is corrupt.") from exc
            if expanded:
                expanded_size += len(expanded)
                if expanded_size > skeleton.uncompressed_size:
                    raise _fail("skeleton expands beyond archive.size.")
                _write_all(destination, expanded, "skeleton")
            if active_decoder.eof:
                saw_member = True
                pending = active_decoder.unused_data
                decoder = None
                continue
            unconsumed = active_decoder.unconsumed_tail
            if unconsumed:
                if unconsumed == pending and not expanded:
                    raise _fail("skeleton gzip stream made no progress.")
                pending = unconsumed
                continue
            pending = b""

    if decoder is not None:
        raise _fail("skeleton gzip stream is truncated.")
    if not saw_member:
        raise _fail("skeleton gzip stream is empty.")
    if compressed_size != skeleton.size:
        raise _fail("skeleton input size does not match the plan.")
    if digest.hexdigest() != skeleton.sha256:
        raise _fail("skeleton input changed or failed its SHA-256 check.")
    if expanded_size != skeleton.uncompressed_size:
        raise _fail("skeleton gzip output size does not match archive.size.")


def _verify_zero_ranges(
    archive: BinaryIO,
    downloads: tuple[PublisherPackage, ...],
) -> None:
    for index, package in enumerate(downloads):
        archive.seek(package.offset_data)
        remaining = package.size
        while remaining:
            chunk = archive.read(min(_CHUNK_SIZE, remaining))
            if not chunk:
                raise _fail(f"skeleton range {index} ended before its planned size.")
            if any(chunk):
                raise _fail(
                    f"skeleton range {index} contains nonzero placeholder data."
                )
            remaining -= len(chunk)


def _validate_package_paths(
    plan: PublisherBootstrapPlan,
    packages: object,
) -> dict[str, Path]:
    hashes = {package.sha256 for package in plan.downloads}
    if not isinstance(packages, Mapping):
        raise _fail("packages must map SHA-256 digests to input paths.")
    typed_packages = cast(Mapping[object, object], packages)
    try:
        items: list[tuple[object, object]] = list(typed_packages.items())
    except Exception as exc:
        raise _fail("packages mapping could not be read.") from exc
    result: dict[str, Path] = {}
    for key, path in items:
        package_hash = _require_string(key, "packages mapping key")
        if package_hash in result:
            raise _fail("packages mapping contains duplicate hashes.")
        result[package_hash] = _require_path(path, "packages mapping value")
    if frozenset(result) != frozenset(hashes):
        raise _fail("packages mapping hashes must exactly match plan downloads.")
    return result


def _copy_package_range(
    archive: BinaryIO,
    package: PublisherPackage,
    source_path: Path,
    index: int,
) -> None:
    label = f"package input {index}"
    with _open_regular_input(source_path, label) as (source, original):
        if original.st_size != package.size:
            raise _fail(f"{label} size does not match the plan.")
        archive.seek(package.offset_data)
        digest = hashlib.sha256()
        remaining = package.size
        while remaining:
            chunk = source.read(min(_CHUNK_SIZE, remaining))
            if not chunk:
                raise _fail(f"{label} ended before its planned size.")
            digest.update(chunk)
            _write_all(archive, chunk, label)
            remaining -= len(chunk)
        if source.read(1):
            raise _fail(f"{label} exceeds its planned size.")
        if digest.hexdigest() != package.sha256:
            raise _fail(f"{label} SHA-256 does not match the plan.")
        _verify_input_unchanged(source_path, source, original, label)


def _verify_final_archive(archive: BinaryIO, plan: PublisherBootstrapPlan) -> None:
    archive.seek(0, os.SEEK_END)
    if archive.tell() != plan.archive.size:
        raise _fail("reconstructed archive size does not match the plan.")
    archive.seek(0)
    digest = hashlib.sha256()
    total = 0
    while chunk := archive.read(_CHUNK_SIZE):
        digest.update(chunk)
        total += len(chunk)
    if total != plan.archive.size:
        raise _fail("reconstructed archive size changed while verifying.")
    if digest.hexdigest() != plan.archive.sha256:
        raise _fail("reconstructed archive SHA-256 does not match the plan.")


def reconstruct_publisher_archive(
    plan: PublisherBootstrapPlan,
    skeleton: Path,
    packages: Mapping[str, Path],
    destination: Path,
) -> Path:
    """Rebuild and atomically publish an exact archive using only local files."""

    validated = _validate_plan(plan)
    package_paths = _validate_package_paths(validated, packages)
    destination_path = _require_path(destination, "destination")
    try:
        destination_path.lstat()
    except FileNotFoundError:
        pass
    else:
        raise _fail("destination already exists; it will not be overwritten.")

    _hash_exact_input(
        skeleton,
        validated.skeleton.size,
        validated.skeleton.sha256,
        "skeleton",
    )
    temp_descriptor = -1
    temporary_path: Path | None = None
    try:
        temp_descriptor, temporary_name = tempfile.mkstemp(
            prefix=".carlina-publisher-bootstrap-",
            suffix=".tmp",
            dir=destination_path.parent,
        )
        temporary_path = Path(temporary_name)
        with os.fdopen(temp_descriptor, "w+b", buffering=0) as archive:
            temp_descriptor = -1
            with _open_regular_input(skeleton, "skeleton") as (source, original):
                if original.st_size != validated.skeleton.size:
                    raise _fail("skeleton input size changed before decompression.")
                _decompress_skeleton(source, archive, validated.skeleton)
                _verify_input_unchanged(skeleton, source, original, "skeleton")
            _verify_zero_ranges(archive, validated.downloads)
            for index, package in enumerate(validated.downloads):
                _copy_package_range(
                    archive,
                    package,
                    package_paths[package.sha256],
                    index,
                )
            _verify_final_archive(archive, validated)
            archive.flush()
            os.fsync(archive.fileno())
        try:
            os.link(temporary_path, destination_path)
        except FileExistsError as exc:
            raise _fail(
                "destination appeared during reconstruction; it was preserved."
            ) from exc
        return destination_path
    finally:
        if temp_descriptor >= 0:
            os.close(temp_descriptor)
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
