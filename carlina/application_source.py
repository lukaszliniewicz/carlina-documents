"""Verify the immutable source archive embedded in a Documents package."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from importlib.resources import files
from typing import Final, cast

from carlina.distribution import ApplicationDistribution, read_application_distribution
from carlina.runtime_profiles import RuntimeProfile

APPLICATION_SOURCE_MANIFEST_MEMBER: Final = "carlina/application-source.json"
APPLICATION_SOURCE_ARCHIVE_MEMBER: Final = "carlina/application-source.tar.gz"
MAX_APPLICATION_SOURCE_BYTES: Final = 64 * 1024 * 1024

_MAX_MANIFEST_BYTES: Final = 4096
_MAX_FILENAME_LENGTH: Final = 200
_MAX_VERSION_LENGTH: Final = 200
_MANIFEST_KEYS: Final = frozenset(
    {
        "format",
        "schema_version",
        "application_profile",
        "application_version",
        "source_revision",
        "source_date_epoch",
        "archive",
        "filename",
        "byte_length",
        "sha256",
    }
)
_APPLICATION_VERSION: Final = re.compile(r"[0-9][0-9A-Za-z._+!-]{0,199}")
_SOURCE_REVISION: Final = re.compile(r"[0-9a-f]{40}")
_FILENAME: Final = re.compile(r"[A-Za-z0-9._+!-]{1,200}")
_SHA256: Final = re.compile(r"[0-9a-f]{64}")
_INVALID_MANIFEST_ERROR: Final = "Application source manifest is malformed."
_READ_ERROR: Final = "Application source package could not be read."
_INVALID_ARCHIVE_ERROR: Final = "Application source archive is invalid."


class ApplicationSourceError(ValueError):
    """The installed application source metadata or archive is invalid."""


@dataclass(frozen=True, slots=True)
class ApplicationSourceMetadata:
    """Validated, version-bound identity for an embedded source archive."""

    application_version: str
    source_revision: str
    source_date_epoch: int
    filename: str
    byte_length: int
    sha256: str


@dataclass(frozen=True, slots=True)
class ApplicationSourceBundle:
    """Validated metadata and the exact verified archive bytes."""

    metadata: ApplicationSourceMetadata
    archive: bytes


def _unique_object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ApplicationSourceError(_INVALID_MANIFEST_ERROR)
        result[key] = value
    return result


def _safe_package_version(value: object) -> bool:
    if type(value) is not str:
        return False
    return (
        len(value) <= _MAX_VERSION_LENGTH
        and _APPLICATION_VERSION.fullmatch(value) is not None
    )


def _safe_filename(value: object) -> bool:
    if type(value) is not str:
        return False
    return (
        len(value) <= _MAX_FILENAME_LENGTH
        and _FILENAME.fullmatch(value) is not None
        and ".." not in value
        and value.endswith(".tar.gz")
    )


def parse_application_source(
    payload: bytes,
    *,
    distribution: ApplicationDistribution,
    application_version: str,
) -> ApplicationSourceMetadata:
    """Parse a closed source manifest and bind it to installed identity."""
    if (
        type(payload) is not bytes
        or len(payload) > _MAX_MANIFEST_BYTES
        or not _safe_package_version(application_version)
        or type(distribution) is not ApplicationDistribution
        or distribution.profile is not RuntimeProfile.DOCUMENTS
        or type(distribution.source_revision) is not str
        or _SOURCE_REVISION.fullmatch(distribution.source_revision) is None
        or type(distribution.source_date_epoch) is not int
        or distribution.source_date_epoch <= 0
    ):
        raise ApplicationSourceError(_INVALID_MANIFEST_ERROR)

    try:
        parsed_object: object = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_unique_object_pairs,
        )
    except ApplicationSourceError:
        raise
    except TypeError, ValueError, RecursionError:
        raise ApplicationSourceError(_INVALID_MANIFEST_ERROR) from None

    if type(parsed_object) is not dict:
        raise ApplicationSourceError(_INVALID_MANIFEST_ERROR)
    parsed = cast(dict[str, object], parsed_object)
    if frozenset(parsed) != _MANIFEST_KEYS:
        raise ApplicationSourceError(_INVALID_MANIFEST_ERROR)

    if (
        parsed["format"] != "carlina.application-source"
        or type(parsed["schema_version"]) is not int
        or parsed["schema_version"] != 1
        or parsed["application_profile"] != RuntimeProfile.DOCUMENTS.value
        or parsed["archive"] != "application-source.tar.gz"
    ):
        raise ApplicationSourceError(_INVALID_MANIFEST_ERROR)

    if (
        parsed["application_version"] != application_version
        or parsed["source_revision"] != distribution.source_revision
        or type(parsed["source_date_epoch"]) is not int
        or parsed["source_date_epoch"] != distribution.source_date_epoch
    ):
        raise ApplicationSourceError(_INVALID_MANIFEST_ERROR)

    filename = parsed["filename"]
    byte_length = parsed["byte_length"]
    sha256 = parsed["sha256"]
    if (
        not _safe_filename(filename)
        or type(byte_length) is not int
        or not 1 <= byte_length <= MAX_APPLICATION_SOURCE_BYTES
        or type(sha256) is not str
        or _SHA256.fullmatch(sha256) is None
    ):
        raise ApplicationSourceError(_INVALID_MANIFEST_ERROR)

    return ApplicationSourceMetadata(
        application_version=application_version,
        source_revision=distribution.source_revision,
        source_date_epoch=distribution.source_date_epoch,
        filename=cast(str, filename),
        byte_length=byte_length,
        sha256=sha256,
    )


def _read_package_member(
    member: str,
    limit: int,
    *,
    missing_is_none: bool = False,
) -> bytes | None:
    try:
        package_member = member.removeprefix("carlina/")
        resource = files("carlina").joinpath(package_member)
        with resource.open("rb") as stream:
            payload = stream.read(limit)
    except FileNotFoundError:
        if missing_is_none:
            return None
        raise ApplicationSourceError(_READ_ERROR) from None
    except ModuleNotFoundError, OSError, TypeError, ValueError:
        raise ApplicationSourceError(_READ_ERROR) from None
    if type(payload) is not bytes:
        raise ApplicationSourceError(_READ_ERROR)
    return payload


def read_application_source() -> ApplicationSourceBundle | None:
    """Read and verify the fixed source archive from a Documents package."""
    installed_distribution = read_application_distribution()
    if installed_distribution.profile is not RuntimeProfile.DOCUMENTS:
        return None

    manifest = _read_package_member(
        APPLICATION_SOURCE_MANIFEST_MEMBER,
        _MAX_MANIFEST_BYTES + 1,
        missing_is_none=True,
    )
    if manifest is None:
        return None
    if not manifest:
        raise ApplicationSourceError(_INVALID_MANIFEST_ERROR)
    if len(manifest) > _MAX_MANIFEST_BYTES:
        raise ApplicationSourceError(_INVALID_MANIFEST_ERROR)

    try:
        installed_version = version("carlina")
    except PackageNotFoundError, OSError, TypeError, ValueError:
        raise ApplicationSourceError(_READ_ERROR) from None
    metadata = parse_application_source(
        manifest,
        distribution=installed_distribution,
        application_version=installed_version,
    )

    archive = _read_package_member(
        APPLICATION_SOURCE_ARCHIVE_MEMBER,
        min(metadata.byte_length + 1, MAX_APPLICATION_SOURCE_BYTES + 1),
    )
    if archive is None:
        raise ApplicationSourceError(_READ_ERROR)
    if (
        len(archive) != metadata.byte_length
        or len(archive) > MAX_APPLICATION_SOURCE_BYTES
        or hashlib.sha256(archive).hexdigest() != metadata.sha256
    ):
        raise ApplicationSourceError(_INVALID_ARCHIVE_ERROR)

    return ApplicationSourceBundle(metadata=metadata, archive=archive)


__all__ = [
    "APPLICATION_SOURCE_ARCHIVE_MEMBER",
    "APPLICATION_SOURCE_MANIFEST_MEMBER",
    "MAX_APPLICATION_SOURCE_BYTES",
    "ApplicationSourceBundle",
    "ApplicationSourceError",
    "ApplicationSourceMetadata",
    "parse_application_source",
    "read_application_source",
]
