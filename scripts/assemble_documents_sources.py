"""Assemble distributor-selected Documents source blobs into a release ZIP."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import unicodedata
import uuid
from collections.abc import Generator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Final, NoReturn, cast
from urllib.parse import urlsplit
from zipfile import ZIP_STORED, ZipFile, ZipInfo

_MANIFEST_FORMAT: Final = "carlina.documents-source-companion-input"
_OUTPUT_FORMAT: Final = "carlina.documents-source-companion"
_SCHEMA_VERSION: Final = 1
_INDEX_LIMIT: Final = 16 * 1024 * 1024
_TOTAL_PAYLOAD_LIMIT: Final = 4 * 1024 * 1024 * 1024
_SOURCE_LIMIT: Final = 2 * 1024 * 1024 * 1024
_OTHER_FILE_LIMIT: Final = 16 * 1024 * 1024
_COMPONENT_LIMIT: Final = 4096
_FILE_LIMIT: Final = 16384
_PLATFORM_LIMIT: Final = 2
_MEMBER_PATH_LIMIT: Final = 240
_MEMBER_SEGMENT_LIMIT: Final = 80
_STAGE_ATTEMPT_LIMIT: Final = 5
_READ_CHUNK: Final = 1024 * 1024
_SHA256: Final = re.compile(r"[0-9a-f]{64}\Z")
_REVISION: Final = re.compile(r"[0-9a-f]{40}\Z")
_TOKEN: Final = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.+\-]*\Z")
_SAFE_SEGMENT: Final = re.compile(r"[A-Za-z0-9_.+\-]+\Z", re.ASCII)
_WINDOWS_DEVICE: Final = re.compile(
    r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])\Z", re.IGNORECASE
)
_BASIS: Final = frozenset(
    {
        "recipe_declared_source",
        "publisher_release_source",
        "source_version_dependency",
    }
)
_MANIFEST_KEYS: Final = frozenset(
    {
        "format",
        "schema_version",
        "application_profile",
        "application_version",
        "source_revision",
        "wheel_sha256",
        "inventory_inputs",
        "components",
        "files",
    }
)
_INVENTORY_INPUT_KEYS: Final = frozenset({"platform", "sha256"})
_COMPONENT_KEYS: Final = frozenset(
    {
        "platform",
        "component_ref",
        "binary_sha256",
        "source_members",
        "recipe_members",
        "notice_members",
        "basis",
    }
)
_FILE_KEYS: Final = frozenset(
    {
        "member",
        "kind",
        "sha256",
        "bytes",
        "origin_url",
        "origin_archive_sha256",
        "origin_member",
    }
)
_KIND_PREFIX: Final = {
    "source": "sources",
    "recipe": "recipes",
    "notice": "notices",
    "instructions": "instructions",
}
_ERROR: Final = "Documents source companion assembly failed; no acceptance is implied."


class DocumentsSourceAssemblyError(ValueError):
    """A source companion failure that does not disclose local input details."""


@dataclass(frozen=True, slots=True)
class _FileRecord:
    member: str
    kind: str
    sha256: str
    size: int
    origin_url: str | None
    origin_archive_sha256: str | None
    origin_member: str | None

    def as_json(self) -> dict[str, object]:
        return {
            "member": self.member,
            "kind": self.kind,
            "sha256": self.sha256,
            "bytes": self.size,
            "origin_url": self.origin_url,
            "origin_archive_sha256": self.origin_archive_sha256,
            "origin_member": self.origin_member,
        }


@dataclass(frozen=True, slots=True)
class _SelectedComponent:
    platform: str
    component_ref: str
    binary_sha256: str
    basis: str
    name: str
    version: str
    purl: str | None
    licenses: tuple[object, ...]
    source_members: tuple[str, ...]
    recipe_members: tuple[str, ...]
    notice_members: tuple[str, ...]

    def as_json(self) -> dict[str, object]:
        return {
            "platform": self.platform,
            "component_ref": self.component_ref,
            "binary_sha256": self.binary_sha256,
            "basis": self.basis,
            "name": self.name,
            "version": self.version,
            "purl": self.purl,
            "declared_licenses": list(self.licenses),
            "source_members": list(self.source_members),
            "recipe_members": list(self.recipe_members),
            "notice_members": list(self.notice_members),
        }


@dataclass(frozen=True, slots=True)
class _InventoryIdentity:
    platform: str
    sha256: str


@dataclass(frozen=True, slots=True)
class _FileIdentity:
    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int


@dataclass(frozen=True, slots=True)
class _DirectoryIdentity:
    device: int
    inode: int


def _fail() -> NoReturn:
    raise DocumentsSourceAssemblyError(_ERROR)


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        _fail()
    raw = cast(dict[object, object], value)
    if any(not isinstance(key, str) for key in raw):
        _fail()
    return cast(dict[str, object], raw)


def _list(value: object) -> list[object]:
    if not isinstance(value, list):
        _fail()
    return cast(list[object], value)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            _fail()
        result[key] = value
    return result


def _reject_constant(_value: str) -> NoReturn:
    _fail()


def _parse_json(payload: bytes) -> dict[str, object]:
    try:
        value: object = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except UnicodeError, json.JSONDecodeError:
        _fail()
    return _object(value)


def _exact_keys(value: dict[str, object], expected: frozenset[str]) -> None:
    if frozenset(value) != expected:
        _fail()


def _text(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or any(unicodedata.category(character) in {"Cc", "Cs"} for character in value)
    ):
        _fail()
    return value


def _sha256(value: object) -> str:
    result = _text(value)
    if _SHA256.fullmatch(result) is None:
        _fail()
    return result


def _version(value: object) -> str:
    result = _text(value)
    if _TOKEN.fullmatch(result) is None:
        _fail()
    return result


def _positive_or_zero(value: object) -> int:
    if type(value) is not int or value < 0:
        _fail()
    return value


def _file_identity(value: os.stat_result) -> _FileIdentity:
    return _FileIdentity(
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _directory_identity(value: os.stat_result) -> _DirectoryIdentity:
    return _DirectoryIdentity(value.st_dev, value.st_ino)


def _read_regular(path: Path, limit: int) -> bytes:
    try:
        initial = path.lstat()
        if not stat.S_ISREG(initial.st_mode) or initial.st_size > limit:
            _fail()
        descriptor = os.open(
            path,
            os.O_RDONLY
            | cast(int, getattr(os, "O_NOFOLLOW", 0))
            | cast(int, getattr(os, "O_CLOEXEC", 0)),
        )
    except DocumentsSourceAssemblyError:
        raise
    except OSError:
        _fail()
    with os.fdopen(descriptor, "rb") as handle:
        try:
            opened = os.fstat(handle.fileno())
            if not stat.S_ISREG(opened.st_mode) or _file_identity(
                opened
            ) != _file_identity(initial):
                _fail()
            payload = handle.read(limit + 1)
            after_fd = os.fstat(handle.fileno())
            after_path = path.lstat()
        except OSError:
            _fail()
    if (
        len(payload) > limit
        or _file_identity(after_fd) != _file_identity(opened)
        or _file_identity(after_path) != _file_identity(opened)
    ):
        _fail()
    return payload


def _check_posix_support() -> None:
    required_dir_fd = {os.open, os.stat, os.link, os.unlink}
    if (
        not required_dir_fd.issubset(os.supports_dir_fd)
        or os.stat not in os.supports_follow_symlinks
        or not hasattr(os, "O_NOFOLLOW")
        or not hasattr(os, "O_DIRECTORY")
    ):
        _fail()


def _safe_archive_member(value: object) -> str:
    result = _text(value)
    if (
        len(result) > _MEMBER_PATH_LIMIT
        or "\\" in result
        or ":" in result
        or result.startswith("/")
    ):
        _fail()
    parts = result.split("/")
    if not parts:
        _fail()
    for part in parts:
        if (
            not part
            or part in {".", ".."}
            or len(part) > _MEMBER_SEGMENT_LIMIT
            or _SAFE_SEGMENT.fullmatch(part) is None
            or part.endswith((".", " "))
            or _WINDOWS_DEVICE.fullmatch(part.split(".", 1)[0]) is not None
        ):
            _fail()
    return result


def _member(value: object, kind: str | None = None) -> str:
    result = _safe_archive_member(value)
    if kind is not None and result.split("/", 1)[0] != _KIND_PREFIX[kind]:
        _fail()
    if result.casefold() == "index.json":
        _fail()
    return result


@dataclass(slots=True)
class _PathNode:
    spelling: str
    children: dict[str, _PathNode]
    is_file: bool = False


def _add_member_path(root: dict[str, _PathNode], member: str) -> None:
    current: dict[str, _PathNode] = root
    parts = member.split("/")
    for index, part in enumerate(parts):
        folded = part.casefold()
        node = current.get(folded)
        if node is None:
            node = _PathNode(part, {})
            current[folded] = node
        elif node.spelling != part:
            _fail()
        if node.is_file:
            _fail()
        if index == len(parts) - 1:
            if node.children or node.is_file:
                _fail()
            node.is_file = True
        current = node.children


def _origin_url(value: object) -> str | None:
    if value is None:
        return None
    result = _text(value)
    if any(character.isspace() for character in result):
        _fail()
    try:
        parsed = urlsplit(result)
        hostname = parsed.hostname
        _ = parsed.port
    except ValueError:
        _fail()
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or "?" in result
        or "#" in result
    ):
        _fail()
    return result


def _origin_member(value: object) -> str | None:
    if value is None:
        return None
    return _safe_archive_member(value)


def _validate_manifest_identity(manifest: dict[str, object]) -> tuple[str, str, str]:
    _exact_keys(manifest, _MANIFEST_KEYS)
    if (
        manifest.get("format") != _MANIFEST_FORMAT
        or type(manifest.get("schema_version")) is not int
        or manifest.get("schema_version") != _SCHEMA_VERSION
        or manifest.get("application_profile") != "documents"
    ):
        _fail()
    application_version = _version(manifest.get("application_version"))
    source_revision = _text(manifest.get("source_revision"))
    if _REVISION.fullmatch(source_revision) is None:
        _fail()
    wheel_sha256 = _sha256(manifest.get("wheel_sha256"))
    return application_version, source_revision, wheel_sha256


def _manifest_inventories(value: object) -> tuple[_InventoryIdentity, ...]:
    raw_inputs = _list(value)
    if not raw_inputs or len(raw_inputs) > _PLATFORM_LIMIT:
        _fail()
    result: list[_InventoryIdentity] = []
    seen_platforms: set[str] = set()
    seen_hashes: set[str] = set()
    for raw in raw_inputs:
        item = _object(raw)
        _exact_keys(item, _INVENTORY_INPUT_KEYS)
        platform = _text(item.get("platform"))
        digest = _sha256(item.get("sha256"))
        if (
            platform not in {"linux-64", "win-64"}
            or platform in seen_platforms
            or digest in seen_hashes
        ):
            _fail()
        seen_platforms.add(platform)
        seen_hashes.add(digest)
        result.append(_InventoryIdentity(platform, digest))
    return tuple(sorted(result, key=lambda item: item.platform))


def _hash_records(component: dict[str, object]) -> frozenset[str]:
    raw_hashes = component.get("hashes", [])
    if raw_hashes is None:
        _fail()
    records = _list(raw_hashes)
    permitted: set[str] = set()
    seen_algorithms: set[str] = set()
    for raw in records:
        record = _object(raw)
        if frozenset(record) != frozenset({"alg", "content"}):
            _fail()
        algorithm = _text(record.get("alg"))
        content = _text(record.get("content"))
        if algorithm == "SHA-256":
            if algorithm in seen_algorithms:
                _fail()
            seen_algorithms.add(algorithm)
            permitted.add(_sha256(content))
    return frozenset(permitted)


def _properties(component: dict[str, object]) -> frozenset[str]:
    raw_properties = component.get("properties", [])
    if raw_properties is None:
        _fail()
    result: set[str] = set()
    seen_names: set[str] = set()
    allowed_count = 0
    for raw in _list(raw_properties):
        prop = _object(raw)
        if frozenset(prop) != frozenset({"name", "value"}):
            _fail()
        name = _text(prop.get("name"))
        value = _text(prop.get("value"))
        if name in seen_names:
            _fail()
        seen_names.add(name)
        if name == "carlina:allowed-artifact-sha256":
            allowed_count += 1
            try:
                allowed: object = json.loads(value, parse_constant=_reject_constant)
            except json.JSONDecodeError:
                _fail()
            allowed_hashes = [_sha256(item) for item in _list(allowed)]
            if len(allowed_hashes) != len(set(allowed_hashes)):
                _fail()
            result.update(allowed_hashes)
    if allowed_count > 1:
        _fail()
    return frozenset(result)


def _inventory_components(
    report: dict[str, object],
    *,
    platform: str,
    application_version: str,
    source_revision: str,
    wheel_sha256: str,
) -> dict[str, dict[str, object]]:
    if (
        report.get("format") != "carlina.documents-release-inventory"
        or type(report.get("schema_version")) is not int
        or report.get("schema_version") != 1
        or report.get("application_profile") != "documents"
        or report.get("platform") != platform
        or report.get("application_version") != application_version
        or report.get("source_revision") != source_revision
        or report.get("wheel_sha256") != wheel_sha256
    ):
        _fail()
    raw_components = _list(report.get("components"))
    result: dict[str, dict[str, object]] = {}
    for raw in raw_components:
        component = _object(raw)
        ref = _text(component.get("bom-ref"))
        if ref in result:
            _fail()
        actual_hashes = _hash_records(component)
        allowed_hashes = _properties(component)
        if actual_hashes and allowed_hashes and actual_hashes != allowed_hashes:
            _fail()
        component["_carlina_permitted_sha256"] = sorted(actual_hashes | allowed_hashes)
        result[ref] = component
    return result


def _read_inventories(
    paths: Sequence[Path],
    declared: tuple[_InventoryIdentity, ...],
    *,
    application_version: str,
    source_revision: str,
    wheel_sha256: str,
) -> dict[str, dict[str, dict[str, object]]]:
    if len(paths) != len(declared):
        _fail()
    declared_by_sha = {item.sha256: item for item in declared}
    result: dict[str, dict[str, dict[str, object]]] = {}
    seen_sha: set[str] = set()
    for path in paths:
        payload = _read_regular(Path(path), _INDEX_LIMIT)
        digest = hashlib.sha256(payload).hexdigest()
        identity = declared_by_sha.get(digest)
        if identity is None or digest in seen_sha:
            _fail()
        seen_sha.add(digest)
        report = _parse_json(payload)
        result[identity.platform] = _inventory_components(
            report,
            platform=identity.platform,
            application_version=application_version,
            source_revision=source_revision,
            wheel_sha256=wheel_sha256,
        )
    if seen_sha != set(declared_by_sha) or set(result) != {
        item.platform for item in declared
    }:
        _fail()
    return result


def _member_list(value: object) -> tuple[str, ...]:
    members = tuple(_text(item) for item in _list(value))
    if len(members) != len(set(members)):
        _fail()
    return members


def _component_references(
    source_members: tuple[str, ...],
    recipe_members: tuple[str, ...],
    notice_members: tuple[str, ...],
    file_records: dict[str, _FileRecord],
) -> set[str]:
    all_members = (*source_members, *recipe_members, *notice_members)
    if not source_members or len(all_members) != len(set(all_members)):
        _fail()
    referenced: set[str] = set()
    for members, expected_kind in (
        (source_members, "source"),
        (recipe_members, "recipe"),
        (notice_members, "notice"),
    ):
        for member in members:
            record = file_records.get(member)
            if record is None or record.kind != expected_kind:
                _fail()
            referenced.add(member)
    return referenced


def _select_components(
    raw_components: object,
    inventory_components: dict[str, dict[str, dict[str, object]]],
    file_records: dict[str, _FileRecord],
) -> tuple[_SelectedComponent, ...]:
    values = _list(raw_components)
    if not values or len(values) > _COMPONENT_LIMIT:
        _fail()
    result: list[_SelectedComponent] = []
    seen: set[tuple[str, str, str]] = set()
    referenced: set[str] = set()
    for raw in values:
        selection = _object(raw)
        _exact_keys(selection, _COMPONENT_KEYS)
        platform = _text(selection.get("platform"))
        component_ref = _text(selection.get("component_ref"))
        binary_sha = _sha256(selection.get("binary_sha256"))
        basis = _text(selection.get("basis"))
        source_members = _member_list(selection.get("source_members"))
        recipe_members = _member_list(selection.get("recipe_members"))
        notice_members = _member_list(selection.get("notice_members"))
        if platform not in inventory_components or basis not in _BASIS:
            _fail()
        component = inventory_components[platform].get(component_ref)
        if component is None:
            _fail()
        key = (platform, component_ref, binary_sha)
        if key in seen:
            _fail()
        seen.add(key)
        permitted = _list(component.get("_carlina_permitted_sha256"))
        if binary_sha not in permitted:
            _fail()
        referenced.update(
            _component_references(
                source_members,
                recipe_members,
                notice_members,
                file_records,
            )
        )
        name = _text(component.get("name"))
        version = _text(component.get("version"))
        raw_purl = component.get("purl")
        purl = None if raw_purl is None else _text(raw_purl)
        raw_licenses = component.get("licenses", [])
        if raw_licenses is None:
            _fail()
        licenses = tuple(_list(raw_licenses))
        result.append(
            _SelectedComponent(
                platform,
                component_ref,
                binary_sha,
                basis,
                name,
                version,
                purl,
                licenses,
                tuple(sorted(source_members)),
                tuple(sorted(recipe_members)),
                tuple(sorted(notice_members)),
            )
        )
    required_references = {
        record.member
        for record in file_records.values()
        if record.kind in {"source", "recipe", "notice"}
    }
    if referenced != required_references:
        _fail()
    return tuple(
        sorted(
            result,
            key=lambda item: (item.platform, item.component_ref, item.binary_sha256),
        )
    )


def _parse_files(value: object) -> tuple[_FileRecord, ...]:
    raw_files = _list(value)
    if not raw_files or len(raw_files) > _FILE_LIMIT:
        _fail()
    result: list[_FileRecord] = []
    by_member: dict[str, _FileRecord] = {}
    path_tree: dict[str, _PathNode] = {}
    total_payload = 0
    for raw in raw_files:
        item = _object(raw)
        _exact_keys(item, _FILE_KEYS)
        kind = _text(item.get("kind"))
        if kind not in _KIND_PREFIX:
            _fail()
        member = _member(item.get("member"), kind)
        digest = _sha256(item.get("sha256"))
        size = _positive_or_zero(item.get("bytes"))
        limit = _SOURCE_LIMIT if kind == "source" else _OTHER_FILE_LIMIT
        if size > limit:
            _fail()
        origin_url = _origin_url(item.get("origin_url"))
        archive_sha = item.get("origin_archive_sha256")
        origin_archive_sha = None if archive_sha is None else _sha256(archive_sha)
        origin_member = _origin_member(item.get("origin_member"))
        if (origin_archive_sha is None) != (origin_member is None):
            _fail()
        if kind == "source" and origin_url is None:
            _fail()
        if kind == "recipe" and (origin_archive_sha is None or origin_member is None):
            _fail()
        _add_member_path(path_tree, member)
        record = _FileRecord(
            member,
            kind,
            digest,
            size,
            origin_url,
            origin_archive_sha,
            origin_member,
        )
        by_member[member] = record
        result.append(record)
        total_payload += size
        if total_payload > _TOTAL_PAYLOAD_LIMIT:
            _fail()
    if not any(record.kind == "instructions" for record in result):
        _fail()
    return tuple(sorted(result, key=lambda item: item.member))


@dataclass(frozen=True, slots=True)
class _InputDirectory:
    path: Path
    descriptor: int
    identity: _DirectoryIdentity

    def verify(self) -> None:
        try:
            path_status = self.path.lstat()
            descriptor_status = os.fstat(self.descriptor)
        except OSError:
            _fail()
        if (
            not stat.S_ISDIR(path_status.st_mode)
            or not stat.S_ISDIR(descriptor_status.st_mode)
            or _directory_identity(path_status) != self.identity
            or _directory_identity(descriptor_status) != self.identity
        ):
            _fail()

    def open_blob(self, digest: str, expected_size: int) -> tuple[int, _FileIdentity]:
        self.verify()
        try:
            before = os.stat(digest, dir_fd=self.descriptor, follow_symlinks=False)
            path_before = (self.path / digest).lstat()
            if (
                not stat.S_ISREG(before.st_mode)
                or _file_identity(before) != _file_identity(path_before)
                or before.st_size != expected_size
            ):
                _fail()
            descriptor = os.open(
                digest,
                os.O_RDONLY
                | cast(int, getattr(os, "O_NOFOLLOW", 0))
                | cast(int, getattr(os, "O_CLOEXEC", 0)),
                dir_fd=self.descriptor,
            )
            try:
                opened = os.fstat(descriptor)
            except OSError:
                os.close(descriptor)
                _fail()
        except DocumentsSourceAssemblyError:
            raise
        except OSError:
            _fail()
        if not stat.S_ISREG(opened.st_mode) or _file_identity(opened) != _file_identity(
            before
        ):
            os.close(descriptor)
            _fail()
        return descriptor, _file_identity(opened)

    def verify_blob(
        self, digest: str, descriptor: int, identity: _FileIdentity
    ) -> None:
        self.verify()
        try:
            after_fd = os.fstat(descriptor)
            after_leaf = os.stat(digest, dir_fd=self.descriptor, follow_symlinks=False)
            after_path = (self.path / digest).lstat()
        except OSError:
            _fail()
        if any(
            not stat.S_ISREG(status.st_mode) or _file_identity(status) != identity
            for status in (after_fd, after_leaf, after_path)
        ):
            _fail()


@contextmanager
def _open_input_directory(path: Path) -> Generator[_InputDirectory]:
    descriptor = -1
    try:
        before = path.lstat()
        if not stat.S_ISDIR(before.st_mode):
            _fail()
        descriptor = os.open(
            path,
            os.O_RDONLY
            | cast(int, getattr(os, "O_DIRECTORY", 0))
            | cast(int, getattr(os, "O_NOFOLLOW", 0))
            | cast(int, getattr(os, "O_CLOEXEC", 0)),
        )
        opened = os.fstat(descriptor)
    except DocumentsSourceAssemblyError:
        if descriptor >= 0:
            os.close(descriptor)
        raise
    except OSError:
        if descriptor >= 0:
            os.close(descriptor)
        _fail()
    identity = _directory_identity(opened)
    tree = _InputDirectory(path, descriptor, identity)
    try:
        if not stat.S_ISDIR(opened.st_mode) or _directory_identity(before) != identity:
            _fail()
        tree.verify()
        yield tree
        tree.verify()
    finally:
        os.close(descriptor)


def _validate_inputs(
    manifest_path: Path,
    inventory_paths: Sequence[Path],
) -> tuple[
    dict[str, object],
    tuple[_InventoryIdentity, ...],
    tuple[_SelectedComponent, ...],
    tuple[_FileRecord, ...],
]:
    manifest_payload = _read_regular(manifest_path, _INDEX_LIMIT)
    manifest = _parse_json(manifest_payload)
    application_version, source_revision, wheel_sha = _validate_manifest_identity(
        manifest
    )
    inventory_inputs = _manifest_inventories(manifest.get("inventory_inputs"))
    inventories = _read_inventories(
        inventory_paths,
        inventory_inputs,
        application_version=application_version,
        source_revision=source_revision,
        wheel_sha256=wheel_sha,
    )
    files = _parse_files(manifest.get("files"))
    file_by_member = {item.member: item for item in files}
    components = _select_components(
        manifest.get("components"), inventories, file_by_member
    )
    return manifest, inventory_inputs, components, files


def _zip_info(member: str) -> ZipInfo:
    info = ZipInfo(member, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = ZIP_STORED
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | 0o644) << 16
    info.internal_attr = 0
    info.extra = b""
    info.comment = b""
    return info


def _index_payload(
    *,
    manifest: dict[str, object],
    inventory_inputs: tuple[_InventoryIdentity, ...],
    components: tuple[_SelectedComponent, ...],
    files: tuple[_FileRecord, ...],
) -> bytes:
    value: dict[str, object] = {
        "format": _OUTPUT_FORMAT,
        "schema_version": _SCHEMA_VERSION,
        "application_profile": "documents",
        "application_version": manifest["application_version"],
        "source_revision": manifest["source_revision"],
        "wheel_sha256": manifest["wheel_sha256"],
        "inventory_inputs": [
            {"platform": item.platform, "sha256": item.sha256}
            for item in inventory_inputs
        ],
        "components": [item.as_json() for item in components],
        "files": [item.as_json() for item in files],
        "coverage": "selected_components",
        "source_closure_complete": False,
        "public_release_accepted": False,
    }
    try:
        return (
            json.dumps(
                value, ensure_ascii=True, sort_keys=True, indent=2, allow_nan=False
            )
            + "\n"
        ).encode("utf-8")
    except TypeError, ValueError:
        _fail()


def _output_parent(path: Path) -> tuple[int, _DirectoryIdentity, str]:
    parent = path.parent
    leaf = path.name
    if not leaf or leaf in {".", ".."}:
        _fail()
    descriptor = -1
    try:
        before = parent.lstat()
        if not stat.S_ISDIR(before.st_mode):
            _fail()
        descriptor = os.open(
            parent,
            os.O_RDONLY
            | cast(int, getattr(os, "O_DIRECTORY", 0))
            | cast(int, getattr(os, "O_NOFOLLOW", 0))
            | cast(int, getattr(os, "O_CLOEXEC", 0)),
        )
        opened = os.fstat(descriptor)
    except DocumentsSourceAssemblyError:
        if descriptor >= 0:
            os.close(descriptor)
        raise
    except OSError:
        if descriptor >= 0:
            os.close(descriptor)
        _fail()
    identity = _directory_identity(opened)
    if not stat.S_ISDIR(opened.st_mode) or _directory_identity(before) != identity:
        os.close(descriptor)
        _fail()
    try:
        os.stat(leaf, dir_fd=descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return descriptor, identity, leaf
    except OSError:
        os.close(descriptor)
        _fail()
    os.close(descriptor)
    _fail()


def _verify_output_parent(
    path: Path, descriptor: int, identity: _DirectoryIdentity
) -> None:
    try:
        path_status = path.parent.lstat()
        descriptor_status = os.fstat(descriptor)
    except OSError:
        _fail()
    if (
        not stat.S_ISDIR(path_status.st_mode)
        or not stat.S_ISDIR(descriptor_status.st_mode)
        or _directory_identity(path_status) != identity
        or _directory_identity(descriptor_status) != identity
    ):
        _fail()


def _stream_blob(
    output: object,
    tree: _InputDirectory,
    record: _FileRecord,
) -> None:
    descriptor, identity = tree.open_blob(record.sha256, record.size)
    digest = hashlib.sha256()
    count = 0
    try:
        while True:
            chunk = os.read(descriptor, _READ_CHUNK)
            if not chunk:
                break
            count += len(chunk)
            if count > record.size:
                _fail()
            digest.update(chunk)
            cast(BinaryIO, output).write(chunk)
        tree.verify_blob(record.sha256, descriptor, identity)
    finally:
        os.close(descriptor)
    if count != record.size or digest.hexdigest() != record.sha256:
        _fail()


def _write_archive(
    *,
    output_fd: int,
    tree: _InputDirectory,
    index_payload: bytes,
    files: tuple[_FileRecord, ...],
) -> tuple[str, int]:
    with os.fdopen(output_fd, "w+b") as raw:
        with ZipFile(raw, mode="w", compression=ZIP_STORED, allowZip64=True) as archive:
            archive.comment = b""
            members: list[tuple[str, _FileRecord | None]] = [
                (record.member, record) for record in files
            ]
            members.append(("index.json", None))
            for member, record in sorted(members, key=lambda item: item[0]):
                info = _zip_info(member)
                if record is None:
                    archive.writestr(info, index_payload, compress_type=ZIP_STORED)
                else:
                    info.file_size = record.size
                    with archive.open(info, mode="w") as blob:
                        _stream_blob(blob, tree, record)
        raw.flush()
        os.fsync(raw.fileno())
        status = os.fstat(raw.fileno())
        if not stat.S_ISREG(status.st_mode):
            _fail()
        raw.seek(0)
        digest = hashlib.sha256()
        count = 0
        while True:
            chunk = raw.read(_READ_CHUNK)
            if not chunk:
                break
            count += len(chunk)
            digest.update(chunk)
        after = os.fstat(raw.fileno())
        if _file_identity(after) != _file_identity(status) or count != status.st_size:
            _fail()
    return digest.hexdigest(), count


def _stage_name() -> str:
    return f".carlina-source-companion-{uuid.uuid4().hex}.tmp"


def _cleanup_stage(
    parent_fd: int,
    stage_name: str | None,
    stage_identity: _FileIdentity | None,
) -> None:
    if stage_name is None:
        return
    try:
        current = os.stat(stage_name, dir_fd=parent_fd, follow_symlinks=False)
        if stage_identity is None or (
            current.st_dev == stage_identity.device
            and current.st_ino == stage_identity.inode
        ):
            os.unlink(stage_name, dir_fd=parent_fd)
    except OSError:
        pass


def _cleanup_failed_output(
    parent_fd: int,
    output_name: str,
    stage_identity: _FileIdentity | None,
) -> None:
    if stage_identity is None:
        return
    try:
        current = os.stat(output_name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            current.st_dev == stage_identity.device
            and current.st_ino == stage_identity.inode
        ):
            os.unlink(output_name, dir_fd=parent_fd)
    except OSError:
        pass


def _create_stage(parent_fd: int) -> tuple[str, _FileIdentity, int]:
    for _attempt in range(_STAGE_ATTEMPT_LIMIT):
        candidate = _stage_name()
        try:
            stage_fd = os.open(
                candidate,
                os.O_RDWR
                | os.O_CREAT
                | os.O_EXCL
                | cast(int, getattr(os, "O_NOFOLLOW", 0))
                | cast(int, getattr(os, "O_CLOEXEC", 0)),
                0o600,
                dir_fd=parent_fd,
            )
        except FileExistsError:
            continue
        except OSError:
            _fail()
        try:
            initial_stage = os.fstat(stage_fd)
            if not stat.S_ISREG(initial_stage.st_mode):
                _fail()
            os.fchmod(stage_fd, 0o644)
            stage_identity = _file_identity(os.fstat(stage_fd))
        except DocumentsSourceAssemblyError:
            with suppress(OSError):
                os.close(stage_fd)
            _cleanup_stage(parent_fd, candidate, None)
            raise
        except OSError:
            with suppress(OSError):
                os.close(stage_fd)
            _cleanup_stage(parent_fd, candidate, None)
            _fail()
        return candidate, stage_identity, stage_fd
    _fail()


def _publish(
    *,
    output_path: Path,
    output_parent_fd: int,
    output_parent_identity: _DirectoryIdentity,
    stage_name: str,
    stage_identity: _FileIdentity,
) -> None:
    output_name = output_path.name
    _verify_output_parent(output_path, output_parent_fd, output_parent_identity)
    try:
        os.link(
            stage_name,
            output_name,
            src_dir_fd=output_parent_fd,
            dst_dir_fd=output_parent_fd,
            follow_symlinks=False,
        )
    except OSError:
        _fail()
    try:
        output_status = os.stat(
            output_name, dir_fd=output_parent_fd, follow_symlinks=False
        )
        stage_status = os.stat(
            stage_name, dir_fd=output_parent_fd, follow_symlinks=False
        )
    except OSError:
        _fail()
    if (
        not stat.S_ISREG(output_status.st_mode)
        or _file_identity(output_status).device != stage_identity.device
        or _file_identity(output_status).inode != stage_identity.inode
        or _file_identity(stage_status).device != stage_identity.device
        or _file_identity(stage_status).inode != stage_identity.inode
    ):
        _fail()
    try:
        os.unlink(stage_name, dir_fd=output_parent_fd)
    except OSError:
        _fail()
    _verify_output_parent(output_path, output_parent_fd, output_parent_identity)


def _assemble(
    manifest_path: Path,
    files_dir: Path,
    inventory_paths: Sequence[Path],
    output_path: Path,
) -> dict[str, object]:
    _check_posix_support()
    (
        manifest,
        inventory_inputs,
        components,
        files,
    ) = _validate_inputs(Path(manifest_path), inventory_paths)
    index_payload = _index_payload(
        manifest=manifest,
        inventory_inputs=inventory_inputs,
        components=components,
        files=files,
    )
    if len(index_payload) > _INDEX_LIMIT:
        _fail()
    output_parent_fd, output_parent_identity, output_name = _output_parent(
        Path(output_path)
    )
    stage_name: str | None = None
    stage_identity: _FileIdentity | None = None
    published = False
    try:
        with _open_input_directory(Path(files_dir)) as files_tree:
            stage_name, stage_identity, stage_fd = _create_stage(output_parent_fd)
            archive_sha, archive_bytes = _write_archive(
                output_fd=stage_fd,
                tree=files_tree,
                index_payload=index_payload,
                files=files,
            )
            files_tree.verify()
        stage_status = os.stat(
            stage_name, dir_fd=output_parent_fd, follow_symlinks=False
        )
        if (
            not stat.S_ISREG(stage_status.st_mode)
            or stage_status.st_dev != stage_identity.device
            or stage_status.st_ino != stage_identity.inode
            or stage_status.st_size != archive_bytes
        ):
            _fail()
        _publish(
            output_path=Path(output_path),
            output_parent_fd=output_parent_fd,
            output_parent_identity=output_parent_identity,
            stage_name=stage_name,
            stage_identity=_file_identity(stage_status),
        )
        published = True
        return {
            "format": _OUTPUT_FORMAT,
            "index_sha256": hashlib.sha256(index_payload).hexdigest(),
            "archive_sha256": archive_sha,
            "archive_bytes": archive_bytes,
            "component_count": len(components),
            "file_count": len(files),
            "payload_bytes": sum(item.size for item in files),
            "source_closure_complete": False,
            "public_release_accepted": False,
        }
    finally:
        _cleanup_stage(output_parent_fd, stage_name, stage_identity)
        if not published:
            _cleanup_failed_output(output_parent_fd, output_name, stage_identity)
        os.close(output_parent_fd)


def assemble_sources(
    manifest_path: Path,
    files_dir: Path,
    inventory_paths: Sequence[Path],
    output_path: Path,
) -> dict[str, object]:
    """Write a deterministic offline source companion from exact input bytes."""
    try:
        return _assemble(manifest_path, files_dir, inventory_paths, output_path)
    except DocumentsSourceAssemblyError:
        raise
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        IndexError,
        AttributeError,
        RuntimeError,
        OverflowError,
        EOFError,
    ):
        raise DocumentsSourceAssemblyError(_ERROR) from None


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        del message
        raise DocumentsSourceAssemblyError(_ERROR)


def main(argv: Sequence[str] | None = None) -> int:
    parser = _ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--files-dir", type=Path, required=True)
    parser.add_argument("--inventory", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    try:
        arguments = parser.parse_args(argv)
        report = assemble_sources(
            arguments.manifest,
            arguments.files_dir,
            arguments.inventory,
            arguments.output,
        )
    except DocumentsSourceAssemblyError:
        print(_ERROR, file=sys.stderr)
        return 2
    print(
        " ".join(
            (
                f"components={report['component_count']}",
                f"files={report['file_count']}",
                f"payload_bytes={report['payload_bytes']}",
                f"archive_bytes={report['archive_bytes']}",
            )
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
