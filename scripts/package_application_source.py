"""Bundle an exact Documents source archive in its matching application wheel."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import sys
import tarfile
import tempfile
import zipfile
from collections.abc import Iterator
from email.parser import BytesParser
from pathlib import Path, PurePosixPath
from typing import IO, TYPE_CHECKING, Final

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if not __package__ and os.fspath(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, os.fspath(PROJECT_ROOT))

if TYPE_CHECKING or not __package__:
    from carlina.application_source import (
        APPLICATION_SOURCE_ARCHIVE_MEMBER,
        APPLICATION_SOURCE_MANIFEST_MEMBER,
        MAX_APPLICATION_SOURCE_BYTES,
        parse_application_source,
    )
    from carlina.distribution import (
        ApplicationDistribution,
        parse_application_distribution,
    )
    from carlina.runtime_profiles import RuntimeProfileError
    from scripts import build_clean_release as _release
else:
    from carlina.application_source import (
        APPLICATION_SOURCE_ARCHIVE_MEMBER,
        APPLICATION_SOURCE_MANIFEST_MEMBER,
        MAX_APPLICATION_SOURCE_BYTES,
        parse_application_source,
    )
    from carlina.distribution import parse_application_distribution
    from carlina.runtime_profiles import RuntimeProfileError

    from . import build_clean_release as _release

_INVALID: Final = "Application source and wheel do not form a valid Documents release."
_PACKAGE_ROOTS: Final = frozenset({"carlina", "iarf_assistant"})
_MAX_DESCRIPTOR_BYTES: Final = 4096
_MAX_WHEEL_BYTES: Final = 256 * 1024 * 1024
_MAX_SOURCE_MEMBERS: Final = 20_000
_MAX_SOURCE_EXPANDED_BYTES: Final = 512 * 1024 * 1024
_MAX_PACKAGE_METADATA_BYTES: Final = 64 * 1024


class ApplicationSourcePackagingError(ValueError):
    """Reject an unbound, ambiguous or malformed source artifact."""


def _package_version(payload: bytes) -> str:
    metadata = BytesParser().parsebytes(payload)
    versions = metadata.get_all("Version") or []
    if metadata.get_all("Name") != ["carlina"] or len(versions) != 1:
        raise ApplicationSourcePackagingError(_INVALID)
    return versions[0]


def _stream_digest(stream: IO[bytes]) -> str:
    digest = hashlib.sha256()
    while chunk := stream.read(1024 * 1024):
        digest.update(chunk)
    return digest.hexdigest()


def _read_bounded(path: Path, limit: int) -> bytes:
    try:
        with path.open("rb") as stream:
            payload = stream.read(limit + 1)
    except OSError:
        raise ApplicationSourcePackagingError(_INVALID) from None
    if not 0 < len(payload) <= limit:
        raise ApplicationSourcePackagingError(_INVALID)
    return payload


def _safe_archive_path(member_name: str) -> PurePosixPath:
    name = PurePosixPath(member_name)
    if (
        not name.parts
        or name.is_absolute()
        or ".." in name.parts
        or "\\" in member_name
    ):
        raise ApplicationSourcePackagingError(_INVALID)
    return name


def _source_members(
    archive: tarfile.TarFile,
) -> Iterator[tuple[tarfile.TarInfo, PurePosixPath]]:
    expanded = 0
    names: set[str] = set()
    root: str | None = None
    for index, member in enumerate(archive):
        name = _safe_archive_path(member.name)
        normalized_name = name.as_posix().casefold()
        if (
            index >= _MAX_SOURCE_MEMBERS
            or normalized_name in names
            or not (member.isfile() or member.isdir())
            or member.size < 0
        ):
            raise ApplicationSourcePackagingError(_INVALID)
        if root is None:
            root = name.parts[0]
        elif root != name.parts[0]:
            raise ApplicationSourcePackagingError(_INVALID)
        names.add(normalized_name)
        expanded += member.size
        if expanded > _MAX_SOURCE_EXPANDED_BYTES:
            raise ApplicationSourcePackagingError(_INVALID)
        yield member, name


def _read_tar_member(
    archive: tarfile.TarFile, member: tarfile.TarInfo, limit: int
) -> bytes:
    if not member.isfile() or member.size > limit:
        raise ApplicationSourcePackagingError(_INVALID)
    stream = archive.extractfile(member)
    if stream is None:
        raise ApplicationSourcePackagingError(_INVALID)
    with stream:
        payload = stream.read(limit + 1)
    if len(payload) > limit:
        raise ApplicationSourcePackagingError(_INVALID)
    return payload


def _archive_identity(payload: bytes) -> tuple[bytes, str, dict[str, str]]:
    descriptor: bytes | None = None
    package_metadata: bytes | None = None
    package_hashes: dict[str, str] = {}
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        for member, name in _source_members(archive):
            relative_name = PurePosixPath(*name.parts[1:])
            if relative_name == PurePosixPath("carlina/distribution.json"):
                descriptor = _read_tar_member(archive, member, _MAX_DESCRIPTOR_BYTES)
            elif relative_name == PurePosixPath("PKG-INFO"):
                package_metadata = _read_tar_member(
                    archive, member, _MAX_PACKAGE_METADATA_BYTES
                )
            if (
                member.isfile()
                and len(relative_name.parts) > 1
                and relative_name.parts[0] in _PACKAGE_ROOTS
            ):
                stream = archive.extractfile(member)
                if stream is None:
                    raise ApplicationSourcePackagingError(_INVALID)
                with stream:
                    package_hashes[relative_name.as_posix()] = _stream_digest(stream)
    if descriptor is None or package_metadata is None:
        raise ApplicationSourcePackagingError(_INVALID)
    source_version = _package_version(package_metadata)
    return descriptor, source_version, package_hashes


def _wheel_members(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    members = archive.infolist()
    if len(members) > _MAX_SOURCE_MEMBERS:
        raise ApplicationSourcePackagingError(_INVALID)
    names: set[str] = set()
    expanded = 0
    for member in members:
        name = _safe_archive_path(member.filename)
        normalized_name = name.as_posix().casefold()
        if normalized_name in names or member.file_size < 0:
            raise ApplicationSourcePackagingError(_INVALID)
        names.add(normalized_name)
        expanded += member.file_size
        if expanded > _MAX_SOURCE_EXPANDED_BYTES:
            raise ApplicationSourcePackagingError(_INVALID)
    if any(
        name.casefold() in names
        for name in (
            APPLICATION_SOURCE_ARCHIVE_MEMBER,
            APPLICATION_SOURCE_MANIFEST_MEMBER,
        )
    ):
        raise ApplicationSourcePackagingError(_INVALID)
    return members


def _read_wheel_member(archive: zipfile.ZipFile, name: str, limit: int) -> bytes:
    member = archive.getinfo(name)
    if member.file_size < 0 or member.file_size > limit:
        raise ApplicationSourcePackagingError(_INVALID)
    with archive.open(member) as stream:
        payload = stream.read(limit + 1)
    if len(payload) > limit:
        raise ApplicationSourcePackagingError(_INVALID)
    return payload


def _wheel_identity(
    archive: zipfile.ZipFile,
    source_descriptor: bytes,
    source_version: str,
    source_hashes: dict[str, str],
) -> tuple[ApplicationDistribution, str]:
    members = _wheel_members(archive)
    for member in members:
        name = PurePosixPath(member.filename)
        if (
            member.is_dir()
            or len(name.parts) <= 1
            or name.parts[0] not in _PACKAGE_ROOTS
        ):
            continue
        with archive.open(member) as stream:
            digest = _stream_digest(stream)
        if source_hashes.get(name.as_posix()) != digest:
            raise ApplicationSourcePackagingError(_INVALID)

    metadata_names = [
        member.filename
        for member in members
        if member.filename.endswith(".dist-info/METADATA")
    ]
    if len(metadata_names) != 1:
        raise ApplicationSourcePackagingError(_INVALID)
    descriptor_bytes = _read_wheel_member(
        archive, "carlina/distribution.json", _MAX_DESCRIPTOR_BYTES
    )
    if descriptor_bytes != source_descriptor:
        raise ApplicationSourcePackagingError(_INVALID)
    metadata = _read_wheel_member(
        archive, metadata_names[0], _MAX_PACKAGE_METADATA_BYTES
    )
    package_version = _package_version(metadata)
    if package_version != source_version:
        raise ApplicationSourcePackagingError(_INVALID)
    distribution = parse_application_distribution(descriptor_bytes)
    return distribution, package_version


def package_application_source(wheel: Path, source: Path) -> None:
    """Attach source only after proving matching immutable Documents identities.

    This edits an unpublished build output in place. The ordinary wheel
    normalizer regenerates every RECORD digest and deterministic ZIP field.
    """
    try:
        wheel_payload = _read_bounded(wheel, _MAX_WHEEL_BYTES)
        payload = _read_bounded(source, MAX_APPLICATION_SOURCE_BYTES)
        source_descriptor, source_version, source_hashes = _archive_identity(payload)
        with zipfile.ZipFile(io.BytesIO(wheel_payload)) as archive:
            distribution, package_version = _wheel_identity(
                archive, source_descriptor, source_version, source_hashes
            )
            manifest = {
                "format": "carlina.application-source",
                "schema_version": 1,
                "application_profile": "documents",
                "application_version": package_version,
                "source_revision": distribution.source_revision,
                "source_date_epoch": distribution.source_date_epoch,
                "archive": "application-source.tar.gz",
                "filename": source.name,
                "byte_length": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
            encoded = (
                json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n"
            ).encode()
            parse_application_source(
                encoded, distribution=distribution, application_version=package_version
            )
        with tempfile.TemporaryDirectory(
            prefix="application-source-", dir=wheel.parent
        ) as temporary:
            candidate = Path(temporary) / wheel.name
            candidate.write_bytes(wheel_payload)
            with zipfile.ZipFile(candidate, "a") as archive:
                archive.writestr(APPLICATION_SOURCE_MANIFEST_MEMBER, encoded)
                archive.writestr(APPLICATION_SOURCE_ARCHIVE_MEMBER, payload)
            _release._normalize_wheel(  # pyright: ignore[reportPrivateUsage]
                candidate, source_date_epoch=str(distribution.source_date_epoch)
            )
            os.replace(candidate, wheel)
    except ApplicationSourcePackagingError:
        raise
    except (
        OSError,
        ValueError,
        KeyError,
        RuntimeProfileError,
        tarfile.TarError,
        zipfile.BadZipFile,
        _release.CleanReleaseBuildError,
        OverflowError,
    ):
        raise ApplicationSourcePackagingError(_INVALID) from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", required=True, type=Path)
    parser.add_argument("--source-archive", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        package_application_source(args.wheel, args.source_archive)
    except ApplicationSourcePackagingError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
