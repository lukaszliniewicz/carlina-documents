"""Read bounded notice and metadata members from a verified Conda archive."""

from __future__ import annotations

import bz2
import hashlib
import io
import json
import os
import re
import stat
import tarfile
import unicodedata
import zipfile
from collections.abc import Callable
from compression import zstd
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Literal, Protocol, cast

NoticeKind = Literal["license", "metadata", "recipe"]

_MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
_MAX_METADATA_JSON_BYTES = 64 * 1024
_MAX_INFO_TAR_BYTES = 32 * 1024 * 1024
_MAX_TAR_BYTES = 2 * 1024 * 1024 * 1024
_MAX_MEMBERS = 100_000
_MAX_SELECTED_MEMBERS = 4_096
_MAX_SELECTED_MEMBER_BYTES = 2 * 1024 * 1024
_MAX_SELECTED_LICENSE_BYTES = 16 * 1024 * 1024
_MAX_SELECTED_TOTAL_BYTES = 32 * 1024 * 1024
_MAX_RELATIVE_PATH_CHARS = 4_096
_MAX_TAR_EXTENSION_BYTES = 64 * 1024
_READ_CHUNK_BYTES = 64 * 1024
_TAR_BLOCK_BYTES = 512
_TAR_END_BYTES = 2 * _TAR_BLOCK_BYTES
_EXPECTED_SHA256 = re.compile(r"[0-9a-fA-F]{64}\Z")
_DRIVE_PREFIX = re.compile(r"[A-Za-z]:")
_INNER_ZSTD_NAME = re.compile(r"(?:info|pkg)-[A-Za-z0-9._-]+\.tar\.zst\Z")
_LICENSE_PREFIXES = ("license", "licence", "copying", "notice")
_LICENSE_EXACT = {*_LICENSE_PREFIXES, "copyright"}
_METADATA_PATHS = {
    "info/index.json",
    "info/about.json",
    "info/hash_input.json",
}
_SAFE_INPUT_ERROR = "invalid Conda archive input"
_ARCHIVE_ERROR = "invalid Conda archive"


class CondaNoticeError(ValueError):
    """Raised for invalid archive inputs without disclosing their contents."""


@dataclass(frozen=True, slots=True)
class CondaNoticeMember:
    relative_path: str
    kind: NoticeKind
    content: bytes
    sha256: str


@dataclass(frozen=True, slots=True)
class CondaNoticeBundle:
    archive_sha256: str
    name: str
    version: str
    build: str
    subdir: str | None
    members: tuple[CondaNoticeMember, ...]


@dataclass(slots=True)
class _ScanState:
    members: dict[str, CondaNoticeMember]
    entry_count: int = 0
    selected_size: int = 0
    info_index: dict[str, object] | None = None


class _ByteReader(Protocol):
    def read(self, size: int = -1, /) -> bytes: ...


def _close_descriptor(descriptor: int) -> None:
    with suppress(OSError):
        os.close(descriptor)


def _status_signature(status: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        status.st_dev,
        status.st_ino,
        status.st_mode,
        status.st_size,
        status.st_mtime_ns,
        status.st_ctime_ns,
    )


def _open_archive(path: Path) -> tuple[int, os.stat_result]:
    try:
        path_status = path.lstat()
    except OSError, ValueError:
        raise CondaNoticeError(_SAFE_INPUT_ERROR) from None
    if not stat.S_ISREG(path_status.st_mode):
        raise CondaNoticeError(_SAFE_INPUT_ERROR)

    flags = os.O_RDONLY
    flags |= cast(int, getattr(os, "O_CLOEXEC", 0))
    flags |= cast(int, getattr(os, "O_NOFOLLOW", 0))
    try:
        descriptor = os.open(path, flags)
    except OSError, ValueError:
        raise CondaNoticeError(_SAFE_INPUT_ERROR) from None

    try:
        opened_status = os.fstat(descriptor)
    except OSError:
        _close_descriptor(descriptor)
        raise CondaNoticeError(_SAFE_INPUT_ERROR) from None
    if (
        not stat.S_ISREG(opened_status.st_mode)
        or (path_status.st_dev, path_status.st_ino)
        != (opened_status.st_dev, opened_status.st_ino)
        or opened_status.st_size > _MAX_ARCHIVE_BYTES
    ):
        _close_descriptor(descriptor)
        raise CondaNoticeError(_SAFE_INPUT_ERROR)
    return descriptor, opened_status


def _hash_archive(descriptor: int) -> str:
    digest = hashlib.sha256()
    size = 0
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        while True:
            remaining = _MAX_ARCHIVE_BYTES + 1 - size
            chunk = os.read(descriptor, min(_READ_CHUNK_BYTES, remaining))
            if not chunk:
                break
            size += len(chunk)
            if size > _MAX_ARCHIVE_BYTES:
                raise CondaNoticeError(_SAFE_INPUT_ERROR)
            digest.update(chunk)
    except OSError:
        raise CondaNoticeError(_SAFE_INPUT_ERROR) from None
    return digest.hexdigest()


def _verify_stable_path(
    path: Path, descriptor: int, initial_status: os.stat_result
) -> None:
    try:
        descriptor_status = os.fstat(descriptor)
        path_status = path.lstat()
    except OSError, ValueError:
        raise CondaNoticeError(_SAFE_INPUT_ERROR) from None
    if (
        not stat.S_ISREG(path_status.st_mode)
        or _status_signature(descriptor_status) != _status_signature(initial_status)
        or _status_signature(path_status) != _status_signature(initial_status)
    ):
        raise CondaNoticeError(_SAFE_INPUT_ERROR)


class _LimitedReader(io.RawIOBase):
    """Count decompressed tar bytes while keeping reads incremental."""

    def __init__(self, source: _ByteReader, limit: int) -> None:
        super().__init__()
        self._source = source
        self._limit = limit
        self.total = 0
        self.tail = b""

    def readable(self) -> bool:
        return True

    def read(self, size: int = -1) -> bytes:
        if self.closed:
            raise ValueError("I/O operation on closed stream")
        if size == 0:
            return b""
        request_size = _READ_CHUNK_BYTES if size < 0 else size
        if request_size < 0:
            raise CondaNoticeError(_ARCHIVE_ERROR)
        request_size = min(request_size, max(1, self._limit - self.total + 1))
        try:
            chunk = self._source.read(request_size)
        except Exception:
            raise CondaNoticeError(_ARCHIVE_ERROR) from None
        if not chunk:
            return b""
        if self.total + len(chunk) > self._limit:
            raise CondaNoticeError(_ARCHIVE_ERROR)
        self.total += len(chunk)
        self.tail = (self.tail + chunk)[-_TAR_END_BYTES:]
        return chunk


class _BoundedTarInfo(tarfile.TarInfo):
    """Cap tar extension records before tarfile buffers their contents."""

    def _call_parent_processor(
        self, name: str, archive: tarfile.TarFile
    ) -> tarfile.TarInfo:
        processor_value: object = getattr(super(), name, None)
        if not callable(processor_value):
            raise CondaNoticeError(_ARCHIVE_ERROR)
        processor = cast(Callable[[tarfile.TarFile], tarfile.TarInfo], processor_value)
        return processor(archive)

    def _proc_pax(self, archive: tarfile.TarFile) -> tarfile.TarInfo:
        if self.size > _MAX_TAR_EXTENSION_BYTES:
            raise CondaNoticeError(_ARCHIVE_ERROR)
        return self._call_parent_processor("_proc_pax", archive)

    def _proc_gnulong(self, archive: tarfile.TarFile) -> tarfile.TarInfo:
        if self.size > _MAX_TAR_EXTENSION_BYTES:
            raise CondaNoticeError(_ARCHIVE_ERROR)
        return self._call_parent_processor("_proc_gnulong", archive)


def _has_control(value: str) -> bool:
    return any(unicodedata.category(character) in {"Cc", "Cs"} for character in value)


def _normalized_candidate_path(raw_path: str) -> str:
    return raw_path[2:] if raw_path.startswith("./") else raw_path


def _is_payload_notice_basename(basename: str) -> bool:
    lowered = basename.casefold()
    return lowered in _LICENSE_EXACT or any(
        lowered.startswith(f"{prefix}.") for prefix in _LICENSE_PREFIXES
    )


def _is_payload_notice_path(parts: list[str]) -> bool:
    return bool(parts) and (
        _is_payload_notice_basename(parts[-1])
        or any(part in {"licenses", "licences"} for part in parts[:-1])
    )


def _is_recipe_license_basename(basename: str) -> bool:
    lowered = basename.casefold()
    return lowered in _LICENSE_EXACT or lowered.startswith(_LICENSE_PREFIXES)


def _candidate_kind(
    raw_path: str, *, include_payload_notices: bool
) -> NoticeKind | None:
    path = _normalized_candidate_path(raw_path)
    match_path = path[:-1] if path.endswith("/") else path
    parts = match_path.split("/")
    lowered_parts = [part.casefold() for part in parts]
    basename = lowered_parts[-1] if lowered_parts else ""

    if len(lowered_parts) >= 3 and lowered_parts[:2] == ["info", "licenses"]:
        return "license"
    if (
        len(lowered_parts) == 2
        and lowered_parts[0] == "info"
        and lowered_parts[1].startswith(_LICENSE_PREFIXES)
    ):
        return "license"
    if match_path.casefold() in _METADATA_PATHS:
        return "metadata"
    if len(lowered_parts) >= 3 and lowered_parts[:2] == ["info", "recipe"]:
        return "license" if _is_recipe_license_basename(basename) else "recipe"
    if include_payload_notices and _is_payload_notice_path(lowered_parts):
        return "license"

    has_unsafe_shape = (
        path.startswith("/")
        or _DRIVE_PREFIX.match(path) is not None
        or "\\" in path
        or _has_control(path)
        or any(part in {"", ".", ".."} for part in path.replace("\\", "/").split("/"))
    )
    if has_unsafe_shape:
        loose_parts = [
            part.casefold()
            for part in path.replace("\\", "/").split("/")
            if part not in {"", ".", ".."}
        ]
        metadata_names = {
            metadata_path.split("/")[1] for metadata_path in _METADATA_PATHS
        }
        if include_payload_notices and _is_payload_notice_path(loose_parts):
            return "license"
        if any(
            loose_parts[index : index + 2] == ["info", "licenses"]
            for index in range(max(0, len(loose_parts) - 1))
        ):
            return "license"
        if any(
            loose_parts[index : index + 2] == ["info", "recipe"]
            for index in range(max(0, len(loose_parts) - 1))
        ):
            return "license" if _is_recipe_license_basename(basename) else "recipe"
        if any(
            loose_parts[index] == "info"
            and index + 1 < len(loose_parts)
            and loose_parts[index + 1] in metadata_names
            for index in range(max(0, len(loose_parts) - 1))
        ):
            return "metadata"
        if any(
            loose_parts[index] == "info"
            and index + 1 < len(loose_parts)
            and loose_parts[index + 1].startswith(_LICENSE_PREFIXES)
            for index in range(len(loose_parts))
        ):
            return "license"
    return None


def _is_notice_directory(path: str, kind: NoticeKind) -> bool:
    parts = path.casefold().split("/")
    return kind in {"license", "recipe"} and (
        parts[:2] == ["info", "licenses"] or parts[:2] == ["info", "recipe"]
    )


def _validate_relative_path(path: str) -> str:
    if (
        not path
        or len(path) > _MAX_RELATIVE_PATH_CHARS
        or path.startswith("/")
        or _DRIVE_PREFIX.match(path) is not None
        or "\\" in path
        or _has_control(path)
    ):
        raise CondaNoticeError(_ARCHIVE_ERROR)
    components = path.split("/")
    if any(not component or component in {".", ".."} for component in components):
        raise CondaNoticeError(_ARCHIVE_ERROR)
    return path


def _selected_member_limit(kind: NoticeKind) -> int:
    return (
        _MAX_SELECTED_LICENSE_BYTES if kind == "license" else _MAX_SELECTED_MEMBER_BYTES
    )


def _read_selected_member(
    archive: tarfile.TarFile, member: tarfile.TarInfo, *, kind: NoticeKind
) -> bytes:
    if member.size < 0 or member.size > _selected_member_limit(kind):
        raise CondaNoticeError(_ARCHIVE_ERROR)
    try:
        stream = archive.extractfile(member)
    except Exception:
        raise CondaNoticeError(_ARCHIVE_ERROR) from None
    if stream is None:
        raise CondaNoticeError(_ARCHIVE_ERROR)
    content = bytearray()
    try:
        with stream:
            while len(content) < member.size:
                chunk = stream.read(min(_READ_CHUNK_BYTES, member.size - len(content)))
                if not chunk:
                    raise CondaNoticeError(_ARCHIVE_ERROR)
                content.extend(chunk)
            if stream.read(1):
                raise CondaNoticeError(_ARCHIVE_ERROR)
    except CondaNoticeError:
        raise
    except Exception:
        raise CondaNoticeError(_ARCHIVE_ERROR) from None
    return bytes(content)


def _parse_json_object(content: bytes) -> dict[str, object]:
    def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise CondaNoticeError(_ARCHIVE_ERROR)
            result[key] = value
        return result

    try:
        text = content.decode("utf-8")
        value: object = json.loads(text, object_pairs_hook=reject_duplicate_keys)
    except UnicodeDecodeError, json.JSONDecodeError, RecursionError:
        raise CondaNoticeError(_ARCHIVE_ERROR) from None
    if not isinstance(value, dict) or not all(
        isinstance(key, str) for key in cast(dict[object, object], value)
    ):
        raise CondaNoticeError(_ARCHIVE_ERROR)
    return cast(dict[str, object], value)


def _add_member(
    state: _ScanState,
    path: str,
    kind: NoticeKind,
    content: bytes,
    *,
    is_info_tar: bool,
) -> None:
    if path in state.members:
        raise CondaNoticeError(_ARCHIVE_ERROR)
    if len(state.members) >= _MAX_SELECTED_MEMBERS:
        raise CondaNoticeError(_ARCHIVE_ERROR)
    if len(content) > _selected_member_limit(kind):
        raise CondaNoticeError(_ARCHIVE_ERROR)
    if state.selected_size + len(content) > _MAX_SELECTED_TOTAL_BYTES:
        raise CondaNoticeError(_ARCHIVE_ERROR)

    if kind == "metadata":
        metadata = _parse_json_object(content)
        if path == "info/index.json" and is_info_tar:
            state.info_index = metadata

    member = CondaNoticeMember(
        relative_path=path,
        kind=kind,
        content=content,
        sha256=hashlib.sha256(content).hexdigest(),
    )
    state.members[path] = member
    state.selected_size += len(content)


def _drain_tar_padding(archive: tarfile.TarFile, reader: _LimitedReader) -> None:
    while True:
        try:
            chunk = archive.fileobj.read(_READ_CHUNK_BYTES)
        except Exception:
            raise CondaNoticeError(_ARCHIVE_ERROR) from None
        if not chunk:
            break
        if any(chunk):
            raise CondaNoticeError(_ARCHIVE_ERROR)
    try:
        terminal_position = archive.fileobj.tell()
    except Exception:
        raise CondaNoticeError(_ARCHIVE_ERROR) from None
    if (
        reader.total < _TAR_END_BYTES
        or terminal_position - archive.offset < _TAR_END_BYTES
        or reader.total % _TAR_BLOCK_BYTES != 0
        or len(reader.tail) < _TAR_END_BYTES
        or any(reader.tail)
    ):
        raise CondaNoticeError(_ARCHIVE_ERROR)


def _scan_tar_stream(
    source: _ByteReader,
    *,
    state: _ScanState,
    decompressed_limit: int,
    is_info_tar: bool,
    include_payload_notices: bool,
) -> None:
    reader = _LimitedReader(source, decompressed_limit)
    try:
        with tarfile.open(
            fileobj=reader,
            mode="r|",
            bufsize=_READ_CHUNK_BYTES,
            errorlevel=2,
            tarinfo=_BoundedTarInfo,
        ) as archive:
            for member in archive:
                state.entry_count += 1
                if state.entry_count > _MAX_MEMBERS:
                    raise CondaNoticeError(_ARCHIVE_ERROR)

                normalized = _normalized_candidate_path(member.name)
                kind = _candidate_kind(
                    member.name,
                    include_payload_notices=include_payload_notices,
                )
                if kind is None:
                    continue
                relative_path = _validate_relative_path(normalized)
                if member.isdir() and _is_notice_directory(relative_path, kind):
                    continue
                if not member.isreg():
                    raise CondaNoticeError(_ARCHIVE_ERROR)
                content = _read_selected_member(archive, member, kind=kind)
                _add_member(
                    state,
                    relative_path,
                    kind,
                    content,
                    is_info_tar=is_info_tar,
                )
            _drain_tar_padding(archive, reader)
    except CondaNoticeError:
        raise
    except Exception:
        raise CondaNoticeError(_ARCHIVE_ERROR) from None


def _validate_index(
    state: _ScanState, *, name: str, version: str, build: str
) -> str | None:
    if state.info_index is None:
        raise CondaNoticeError(_ARCHIVE_ERROR)
    index = state.info_index
    if (
        index.get("name") != name
        or index.get("version") != version
        or index.get("build") != build
    ):
        raise CondaNoticeError(_ARCHIVE_ERROR)
    if "subdir" not in index:
        return None
    subdir = index.get("subdir")
    if not isinstance(subdir, str) or not subdir or _has_control(subdir):
        raise CondaNoticeError(_ARCHIVE_ERROR)
    return subdir


def _zip_member_is_regular(info: zipfile.ZipInfo) -> bool:
    mode = info.external_attr >> 16
    file_type = stat.S_IFMT(mode)
    return (
        not info.is_dir()
        and file_type in {0, stat.S_IFREG}
        and not (info.external_attr & 0x10)
        and not (info.flag_bits & 0x41)
        and info.compress_type in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
    )


def _validate_conda_zip(archive: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    infos = archive.infolist()
    if len(infos) != 3:
        raise CondaNoticeError(_ARCHIVE_ERROR)
    by_name: dict[str, zipfile.ZipInfo] = {}
    for info in infos:
        name = info.filename
        if (
            not name
            or "/" in name
            or "\\" in name
            or _has_control(name)
            or (_INNER_ZSTD_NAME.fullmatch(name) is None and name != "metadata.json")
            or name in by_name
            or not _zip_member_is_regular(info)
        ):
            raise CondaNoticeError(_ARCHIVE_ERROR)
        by_name[name] = info

    info_names = [
        name
        for name in by_name
        if name.startswith("info-") and name.endswith(".tar.zst")
    ]
    package_names = [
        name
        for name in by_name
        if name.startswith("pkg-") and name.endswith(".tar.zst")
    ]
    if (
        "metadata.json" not in by_name
        or len(info_names) != 1
        or len(package_names) != 1
    ):
        raise CondaNoticeError(_ARCHIVE_ERROR)
    return by_name


def _read_zip_member(
    archive: zipfile.ZipFile, info: zipfile.ZipInfo, *, limit: int
) -> bytes:
    if (
        info.file_size > limit
        or info.compress_size > limit
        or info.file_size < 0
        or info.compress_size < 0
    ):
        raise CondaNoticeError(_ARCHIVE_ERROR)
    content = bytearray()
    try:
        with archive.open(info, mode="r") as source:
            while True:
                remaining = limit + 1 - len(content)
                chunk = source.read(min(_READ_CHUNK_BYTES, remaining))
                if not chunk:
                    break
                content.extend(chunk)
                if len(content) > limit:
                    raise CondaNoticeError(_ARCHIVE_ERROR)
    except CondaNoticeError:
        raise
    except Exception:
        raise CondaNoticeError(_ARCHIVE_ERROR) from None
    if len(content) != info.file_size:
        raise CondaNoticeError(_ARCHIVE_ERROR)
    return bytes(content)


def _read_conda_zip(fileobj: BinaryIO, *, state: _ScanState) -> None:
    try:
        with zipfile.ZipFile(fileobj, mode="r") as archive:
            members = _validate_conda_zip(archive)
            metadata_bytes = _read_zip_member(
                archive,
                members["metadata.json"],
                limit=_MAX_METADATA_JSON_BYTES,
            )
            metadata = _parse_json_object(metadata_bytes)
            if (
                type(metadata.get("conda_pkg_format_version")) is not int
                or metadata.get("conda_pkg_format_version") != 2
            ):
                raise CondaNoticeError(_ARCHIVE_ERROR)

            info_name = next(
                name
                for name in members
                if name.startswith("info-") and name.endswith(".tar.zst")
            )
            package_name = next(
                name
                for name in members
                if name.startswith("pkg-") and name.endswith(".tar.zst")
            )
            for inner_name, limit in (
                (info_name, _MAX_INFO_TAR_BYTES),
                (package_name, _MAX_TAR_BYTES),
            ):
                info = members[inner_name]
                compressed_input_limit = (
                    _MAX_INFO_TAR_BYTES
                    if inner_name == info_name
                    else _MAX_ARCHIVE_BYTES
                )
                if info.file_size > compressed_input_limit or info.compress_size > (
                    _MAX_INFO_TAR_BYTES
                    if inner_name == info_name
                    else _MAX_ARCHIVE_BYTES
                ):
                    raise CondaNoticeError(_ARCHIVE_ERROR)
                try:
                    with archive.open(info, mode="r") as raw_zstd:
                        with zstd.ZstdFile(raw_zstd, mode="rb") as decompressed:
                            _scan_tar_stream(
                                decompressed,
                                state=state,
                                decompressed_limit=limit,
                                is_info_tar=inner_name == info_name,
                                include_payload_notices=inner_name == package_name,
                            )
                        if raw_zstd.read(1):
                            raise CondaNoticeError(_ARCHIVE_ERROR)
                except CondaNoticeError:
                    raise
                except Exception:
                    raise CondaNoticeError(_ARCHIVE_ERROR) from None
    except CondaNoticeError:
        raise
    except Exception:
        raise CondaNoticeError(_ARCHIVE_ERROR) from None


def _read_legacy_tar(fileobj: BinaryIO, *, state: _ScanState) -> None:
    try:
        with bz2.BZ2File(fileobj, mode="rb") as decompressed:
            _scan_tar_stream(
                decompressed,
                state=state,
                decompressed_limit=_MAX_TAR_BYTES,
                is_info_tar=True,
                include_payload_notices=True,
            )
    except CondaNoticeError:
        raise
    except Exception:
        raise CondaNoticeError(_ARCHIVE_ERROR) from None


def _parse_archive(
    path: Path, descriptor: int, digest: str, *, name: str, version: str, build: str
) -> CondaNoticeBundle:
    state = _ScanState(members={})
    try:
        with os.fdopen(descriptor, "rb", closefd=False) as fileobj:
            fileobj.seek(0)
            if path.name.endswith(".conda"):
                _read_conda_zip(fileobj, state=state)
            elif path.name.endswith(".tar.bz2"):
                _read_legacy_tar(fileobj, state=state)
            else:
                raise CondaNoticeError(_SAFE_INPUT_ERROR)
    except CondaNoticeError:
        raise
    except Exception:
        raise CondaNoticeError(_ARCHIVE_ERROR) from None

    subdir = _validate_index(state, name=name, version=version, build=build)
    return CondaNoticeBundle(
        archive_sha256=digest,
        name=name,
        version=version,
        build=build,
        subdir=subdir,
        members=tuple(state.members[key] for key in sorted(state.members)),
    )


def read_conda_notices(
    path: Path,
    *,
    expected_sha256: str,
    name: str,
    version: str,
    build: str,
) -> CondaNoticeBundle:
    """Read selected notice and provenance files after verifying the archive hash."""
    try:
        hash_is_valid = _EXPECTED_SHA256.fullmatch(expected_sha256) is not None
    except TypeError:
        hash_is_valid = False
    if not hash_is_valid or not name or not version or not build:
        raise CondaNoticeError(_SAFE_INPUT_ERROR)

    descriptor, initial_status = _open_archive(path)
    try:
        digest = _hash_archive(descriptor)
        if digest != expected_sha256.lower():
            raise CondaNoticeError(_SAFE_INPUT_ERROR)
        bundle = _parse_archive(
            path,
            descriptor,
            digest,
            name=name,
            version=version,
            build=build,
        )
        _verify_stable_path(path, descriptor, initial_status)
        return bundle
    except CondaNoticeError:
        raise
    except Exception:
        raise CondaNoticeError(_ARCHIVE_ERROR) from None
    finally:
        _close_descriptor(descriptor)


__all__ = [
    "CondaNoticeBundle",
    "CondaNoticeError",
    "CondaNoticeMember",
    "NoticeKind",
    "read_conda_notices",
]
