from __future__ import annotations

import stat
import zipfile
from pathlib import PurePosixPath
from typing import Final

from defusedxml import ElementTree as SafeElementTree
from defusedxml.common import DefusedXmlException

MAX_ARCHIVE_MEMBERS: Final = 2_000
MAX_ARCHIVE_TOTAL_BYTES: Final = 512 * 1024 * 1024
MAX_ARCHIVE_MEMBER_BYTES: Final = 64 * 1024 * 1024
MAX_ARCHIVE_XML_BYTES: Final = 4 * 1024 * 1024
MAX_ARCHIVE_HTML_BYTES: Final = 16 * 1024 * 1024
MAX_COMPRESSION_RATIO: Final = 200


class ArchiveSafetyError(ValueError):
    """Raised when an uploaded archive violates the document safety policy."""


def _normalized_member_name(name: str) -> str:
    if "\x00" in name or "\\" in name:
        raise ArchiveSafetyError("Archive contains a member with an invalid name.")

    path = PurePosixPath(name)
    if (
        not name
        or name.startswith("/")
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or (path.parts and ":" in path.parts[0])
    ):
        raise ArchiveSafetyError("Archive contains an unsafe member path.")
    return path.as_posix()


def _validate_member_type(info: zipfile.ZipInfo) -> None:
    unix_mode = (info.external_attr >> 16) & 0o177777
    file_type = stat.S_IFMT(unix_mode)
    if file_type and file_type not in {stat.S_IFREG, stat.S_IFDIR}:
        raise ArchiveSafetyError("Archive contains a link or special file.")


def validate_zip_archive(archive: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    """Validate a ZIP's directory before any member data is decompressed."""

    members = archive.infolist()
    if len(members) > MAX_ARCHIVE_MEMBERS:
        raise ArchiveSafetyError("Archive contains too many files.")

    validated: dict[str, zipfile.ZipInfo] = {}
    total_size = 0
    for info in members:
        normalized_name = _normalized_member_name(info.filename)
        if normalized_name in validated:
            raise ArchiveSafetyError("Archive contains duplicate member paths.")
        _validate_member_type(info)
        if info.flag_bits & 0x1:
            raise ArchiveSafetyError("Encrypted archive members are not supported.")
        if info.file_size < 0 or info.compress_size < 0:
            raise ArchiveSafetyError("Archive contains an invalid member size.")
        if info.file_size > MAX_ARCHIVE_MEMBER_BYTES:
            raise ArchiveSafetyError("Archive member exceeds the allowed size.")

        total_size += info.file_size
        if total_size > MAX_ARCHIVE_TOTAL_BYTES:
            raise ArchiveSafetyError("Archive expands beyond the allowed total size.")
        if info.file_size:
            if info.compress_size == 0:
                raise ArchiveSafetyError("Archive member has an unsafe compression ratio.")
            if info.file_size / info.compress_size > MAX_COMPRESSION_RATIO:
                raise ArchiveSafetyError("Archive member has an unsafe compression ratio.")
        validated[normalized_name] = info
    return validated


def read_zip_member(
    archive: zipfile.ZipFile,
    member_name: str,
    *,
    max_bytes: int = MAX_ARCHIVE_MEMBER_BYTES,
) -> bytes:
    """Read one previously validated member with a second streaming size guard."""

    normalized_name = _normalized_member_name(member_name)
    info = archive.getinfo(normalized_name)
    if info.file_size > max_bytes:
        raise ArchiveSafetyError("Archive member exceeds the allowed read size.")
    with archive.open(info) as handle:
        data = handle.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ArchiveSafetyError("Archive member exceeds the allowed read size.")
    return data


def parse_safe_xml(data: bytes | str):
    """Parse untrusted package XML with DTDs and entity expansion disabled."""

    encoded = data.encode("utf-8") if isinstance(data, str) else data
    if len(encoded) > MAX_ARCHIVE_XML_BYTES:
        raise ArchiveSafetyError("Archive XML exceeds the allowed size.")
    try:
        return SafeElementTree.fromstring(encoded)
    except DefusedXmlException as exc:
        raise ArchiveSafetyError("Archive XML contains a prohibited declaration.") from exc
