"""Build and verify a small wheel that discovers Documents source companions."""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import json
import os
import stat
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from email.parser import BytesParser
from pathlib import Path
from typing import BinaryIO, Final, NoReturn, cast

from packaging.utils import InvalidWheelFilename, parse_wheel_filename
from packaging.version import InvalidVersion, Version

if not __package__:
    sys.path.insert(0, os.fspath(Path(__file__).resolve().parents[1]))

from carlina.application_companions import (
    APPLICATION_COMPANIONS_RELATIVE_PATH,
    MAX_APPLICATION_COMPANIONS_BYTES,
    ApplicationCompanionsMetadata,
    CompanionDownload,
    CompanionInventory,
    parse_application_companions,
)
from carlina.application_source import (
    APPLICATION_SOURCE_ARCHIVE_MEMBER,
    APPLICATION_SOURCE_MANIFEST_MEMBER,
    MAX_APPLICATION_SOURCE_BYTES,
    ApplicationSourceMetadata,
    parse_application_source,
)
from carlina.distribution import (
    ApplicationDistribution,
    parse_application_distribution,
)
from carlina.runtime_profiles import RuntimeProfile

PROJECT_ROOT = Path(__file__).resolve().parents[1]
_INVALID: Final = (
    "Application companions and wheel do not form a valid Documents release."
)
_APPLICATION_DISTRIBUTION_MEMBER: Final = "carlina/distribution.json"
_APPLICATION_MANIFEST_MAX_BYTES: Final = MAX_APPLICATION_COMPANIONS_BYTES
_APPLICATION_SOURCE_MANIFEST_MAX_BYTES: Final = 4096
_COMPANION_WHEEL_MAX_BYTES: Final = 512 * 1024
_LICENSE_MAX_BYTES: Final = 64 * 1024
_RECORD_DIGEST_ALGORITHM: Final = "sha256"
_RECORD_NAME: Final = "RECORD"
_COMPANION_MANIFEST_PATH: Final = APPLICATION_COMPANIONS_RELATIVE_PATH
_COMPANION_DISTRIBUTION: Final = "carlina-application-companions"
_ZIP_STORED_MODE: Final = (stat.S_IFREG | 0o644) << 16
_READ_CHUNK_BYTES: Final = 1024 * 1024


class ApplicationCompanionsPackagingError(ValueError):
    """Reject an unbound, ambiguous or malformed companion wheel."""


@dataclass(frozen=True, slots=True)
class _InputIdentity:
    path: tuple[int, int, int, int, int, int]
    descriptor: tuple[int, int, int, int, int, int]


@dataclass(frozen=True, slots=True)
class _ApplicationWheelIdentity:
    application_version: str
    source: ApplicationSourceMetadata
    source_date_epoch: int
    sha256: str
    file_identity: tuple[int, int, int, int, int, int]


def _fail() -> NoReturn:
    raise ApplicationCompanionsPackagingError(_INVALID)


def _identity(status: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        status.st_dev,
        status.st_ino,
        status.st_mode,
        status.st_size,
        status.st_mtime_ns,
        status.st_ctime_ns,
    )


def _open_regular_input(
    path: Path,
) -> tuple[BinaryIO, _InputIdentity]:
    try:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode):
            _fail()
        descriptor = os.open(
            path,
            os.O_RDONLY
            | cast(int, getattr(os, "O_NOFOLLOW", 0))
            | cast(int, getattr(os, "O_CLOEXEC", 0)),
        )
        try:
            opened = os.fstat(descriptor)
            path_identity = _identity(before)
            descriptor_identity = _identity(opened)
            # Windows ctime can differ between APIs; fence changes within each.
            if (
                not stat.S_ISREG(opened.st_mode)
                or path_identity[:5] != descriptor_identity[:5]
            ):
                _fail()
            stream = os.fdopen(descriptor, "rb")
        except BaseException:
            os.close(descriptor)
            raise
    except ApplicationCompanionsPackagingError:
        raise
    except OSError:
        _fail()
    return stream, _InputIdentity(
        path=path_identity,
        descriptor=descriptor_identity,
    )


def _verify_input(
    path: Path,
    stream: BinaryIO,
    expected: _InputIdentity,
) -> None:
    try:
        opened = os.fstat(stream.fileno())
        current_path = path.lstat()
    except OSError:
        _fail()
    if (
        not stat.S_ISREG(opened.st_mode)
        or not stat.S_ISREG(current_path.st_mode)
        or _identity(opened) != expected.descriptor
        or _identity(current_path) != expected.path
    ):
        _fail()


def _read_bounded_file(
    path: Path, *, limit: int
) -> tuple[bytes, tuple[int, int, int, int, int, int]]:
    stream, identity = _open_regular_input(path)
    try:
        if identity.path[3] > limit:
            _fail()
        payload = stream.read(limit + 1)
        if len(payload) != identity.path[3] or len(payload) > limit:
            _fail()
        _verify_input(path, stream, identity)
        return payload, identity.path
    finally:
        stream.close()


def _stream_sha256(stream: BinaryIO) -> tuple[str, int]:
    digest = hashlib.sha256()
    count = 0
    while chunk := stream.read(_READ_CHUNK_BYTES):
        count += len(chunk)
        digest.update(chunk)
    return digest.hexdigest(), count


def _application_wheel_members(
    archive: zipfile.ZipFile,
) -> dict[str, zipfile.ZipInfo]:
    entries = archive.infolist()
    if not entries:
        _fail()
    by_name: dict[str, zipfile.ZipInfo] = {}
    by_folded_name: set[str] = set()
    for info in entries:
        name = info.filename
        if not name or info.file_size < 0:
            _fail()
        folded = name.casefold()
        if folded in by_folded_name:
            _fail()
        by_folded_name.add(folded)
        by_name[name] = info
    return by_name


def _required_member(members: dict[str, zipfile.ZipInfo], name: str) -> zipfile.ZipInfo:
    for existing in members:
        if existing.casefold() == name.casefold() and existing != name:
            _fail()
    result = members.get(name)
    if result is None or result.is_dir() or stat.S_ISLNK(result.external_attr >> 16):
        _fail()
    return result


def _read_zip_member(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    *,
    limit: int,
) -> bytes:
    if info.file_size > limit:
        _fail()
    try:
        with archive.open(info, "r") as stream:
            payload = stream.read(limit + 1)
    except OSError, RuntimeError, ValueError, zipfile.BadZipFile:
        _fail()
    if len(payload) != info.file_size or len(payload) > limit:
        _fail()
    return payload


def _wheel_version(path: Path) -> Version:
    try:
        distribution, version, _build, _tags = parse_wheel_filename(path.name)
    except InvalidWheelFilename, InvalidVersion, ValueError:
        _fail()
    parts = path.name[:-4].split("-") if path.name.endswith(".whl") else []
    if (
        distribution != "carlina"
        or len(parts) not in (5, 6)
        or parts[0] != "carlina"
        or parts[1] != str(version)
    ):
        _fail()
    return version


def _application_version(metadata_payload: bytes) -> str:
    try:
        metadata = BytesParser().parsebytes(metadata_payload)
        names = metadata.get_all("Name") or []
        versions = metadata.get_all("Version") or []
    except TypeError, ValueError:
        _fail()
    if names != ["carlina"] or len(versions) != 1 or type(versions[0]) is not str:
        _fail()
    try:
        version = Version(versions[0])
    except InvalidVersion:
        _fail()
    if str(version) != versions[0]:
        _fail()
    return versions[0]


def _application_wheel_identity(path: Path) -> _ApplicationWheelIdentity:
    file_version = _wheel_version(path)
    stream, file_identity = _open_regular_input(path)
    try:
        wheel_sha256, byte_count = _stream_sha256(stream)
        if byte_count != file_identity.path[3]:
            _fail()
        stream.seek(0)
        try:
            with zipfile.ZipFile(stream, "r") as archive:
                members = _application_wheel_members(archive)
                expected_metadata = f"carlina-{file_version}.dist-info/METADATA"
                metadata_candidates = [
                    name
                    for name in members
                    if name.casefold().endswith(".dist-info/metadata")
                ]
                if metadata_candidates != [expected_metadata]:
                    _fail()
                descriptor_bytes = _read_zip_member(
                    archive,
                    _required_member(members, _APPLICATION_DISTRIBUTION_MEMBER),
                    limit=4096,
                )
                distribution: ApplicationDistribution = parse_application_distribution(
                    descriptor_bytes
                )
                if distribution.profile is not RuntimeProfile.DOCUMENTS:
                    _fail()
                version = _application_version(
                    _read_zip_member(
                        archive,
                        _required_member(members, expected_metadata),
                        limit=64 * 1024,
                    )
                )
                if Version(version) != file_version:
                    _fail()
                source_manifest = _read_zip_member(
                    archive,
                    _required_member(members, APPLICATION_SOURCE_MANIFEST_MEMBER),
                    limit=_APPLICATION_SOURCE_MANIFEST_MAX_BYTES,
                )
                source = parse_application_source(
                    source_manifest,
                    distribution=distribution,
                    application_version=version,
                )
                source_archive = _read_zip_member(
                    archive,
                    _required_member(members, APPLICATION_SOURCE_ARCHIVE_MEMBER),
                    limit=MAX_APPLICATION_SOURCE_BYTES,
                )
        except ApplicationCompanionsPackagingError:
            raise
        except OSError, RuntimeError, ValueError, KeyError, zipfile.BadZipFile:
            _fail()
        if (
            len(source_archive) != source.byte_length
            or hashlib.sha256(source_archive).hexdigest() != source.sha256
            or distribution.source_date_epoch is None
        ):
            _fail()
        _verify_input(path, stream, file_identity)
        return _ApplicationWheelIdentity(
            application_version=version,
            source=source,
            source_date_epoch=distribution.source_date_epoch,
            sha256=wheel_sha256,
            file_identity=file_identity.path,
        )
    finally:
        stream.close()


def _inventory_json(item: CompanionInventory) -> dict[str, str]:
    return {"platform": item.platform, "sha256": item.sha256}


def _companion_json(item: CompanionDownload) -> dict[str, str | int]:
    return {
        "kind": item.kind,
        "filename": item.filename,
        "byte_length": item.byte_length,
        "sha256": item.sha256,
        "url": item.url,
        "coverage": item.coverage,
    }


def _canonical_manifest(metadata: ApplicationCompanionsMetadata) -> bytes:
    value = {
        "format": "carlina.application-companions",
        "schema_version": 1,
        "application_profile": "documents",
        "application_version": metadata.application_version,
        "source_revision": metadata.source_revision,
        "application_source_sha256": metadata.application_source_sha256,
        "wheel_sha256": metadata.wheel_sha256,
        "inventory_inputs": [
            _inventory_json(item)
            for item in sorted(
                metadata.inventory_inputs, key=lambda item: item.platform
            )
        ],
        "companions": [
            _companion_json(item)
            for item in sorted(metadata.companions, key=lambda item: item.kind)
        ],
    }
    try:
        result = (
            json.dumps(
                value,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except TypeError, ValueError:
        _fail()
    if len(result) > _APPLICATION_MANIFEST_MAX_BYTES:
        _fail()
    return result


def _record_digest(payload: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(payload).digest())
    return f"{_RECORD_DIGEST_ALGORITHM}=" + digest.rstrip(b"=").decode("ascii")


def _record_payload(files: dict[str, bytes], record_name: str) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    for name in sorted(files):
        payload = files[name]
        writer.writerow((name, _record_digest(payload), str(len(payload))))
    writer.writerow((record_name, "", ""))
    return output.getvalue().encode("utf-8")


def _companion_stem(application_version: str) -> str:
    try:
        version = Version(application_version)
    except InvalidVersion:
        _fail()
    if str(version) != application_version:
        _fail()
    return f"carlina_application_companions-{application_version}"


def _wheel_paths(application_version: str) -> tuple[str, str, str, str, str]:
    stem = _companion_stem(application_version)
    return (
        f"{stem}.data/data/{_COMPANION_MANIFEST_PATH}",
        f"{stem}.dist-info/METADATA",
        f"{stem}.dist-info/WHEEL",
        f"{stem}.dist-info/RECORD",
        f"{stem}.dist-info/licenses/LICENSE",
    )


def _metadata_payload(application_version: str) -> bytes:
    return (
        "Metadata-Version: 2.4\n"
        f"Name: {_COMPANION_DISTRIBUTION}\n"
        f"Version: {application_version}\n"
        "License-Expression: MIT\n"
        "License-File: licenses/LICENSE\n"
        "\n"
    ).encode("ascii")


def _wheel_payload(application_version: str) -> bytes:
    return (
        "Wheel-Version: 1.0\n"
        "Generator: carlina\n"
        "Root-Is-Purelib: true\n"
        "Tag: py3-none-any\n"
    ).encode("ascii")


def _zip_timestamp(source_date_epoch: int) -> tuple[int, int, int, int, int, int]:
    try:
        value = datetime.fromtimestamp(source_date_epoch, UTC)
    except OverflowError, OSError, ValueError:
        _fail()
    if not 1980 <= value.year <= 2107:
        _fail()
    return (
        value.year,
        value.month,
        value.day,
        value.hour,
        value.minute,
        value.second - (value.second % 2),
    )


def _zip_info(
    name: str, timestamp: tuple[int, int, int, int, int, int]
) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=timestamp)
    info.compress_type = zipfile.ZIP_STORED
    info.create_system = 3
    info.external_attr = _ZIP_STORED_MODE
    info.internal_attr = 0
    info.extra = b""
    info.comment = b""
    return info


def _read_project_license() -> bytes:
    license_path = PROJECT_ROOT / "LICENSE"
    payload, _identity_value = _read_bounded_file(
        license_path,
        limit=_LICENSE_MAX_BYTES,
    )
    if payload.splitlines()[:1] != [b"MIT License"]:
        _fail()
    return payload


def _write_companion_wheel(
    output: BinaryIO,
    *,
    application_version: str,
    manifest: bytes,
    license_payload: bytes,
    source_date_epoch: int,
) -> None:
    manifest_name, metadata_name, wheel_name, record_name, license_name = _wheel_paths(
        application_version
    )
    files = {
        manifest_name: manifest,
        metadata_name: _metadata_payload(application_version),
        wheel_name: _wheel_payload(application_version),
        license_name: license_payload,
    }
    files[record_name] = _record_payload(files, record_name)
    if sum(len(payload) for payload in files.values()) > _COMPANION_WHEEL_MAX_BYTES:
        _fail()
    timestamp = _zip_timestamp(source_date_epoch)
    try:
        with zipfile.ZipFile(
            output,
            mode="w",
            compression=zipfile.ZIP_STORED,
            allowZip64=False,
        ) as archive:
            archive.comment = b""
            for name in sorted(files):
                archive.writestr(
                    _zip_info(name, timestamp),
                    files[name],
                    compress_type=zipfile.ZIP_STORED,
                )
    except OSError, ValueError, zipfile.BadZipFile, zipfile.LargeZipFile:
        _fail()


def _companion_wheel_members(
    path: Path,
    *,
    expected_names: tuple[str, str, str, str, str],
    timestamp: tuple[int, int, int, int, int, int],
) -> dict[str, bytes]:
    stream, identity = _open_regular_input(path)
    try:
        if identity.path[3] > _COMPANION_WHEEL_MAX_BYTES:
            _fail()
        try:
            with zipfile.ZipFile(stream, "r") as archive:
                infos = archive.infolist()
                names = [info.filename for info in infos]
                if (
                    len(infos) != 5
                    or names != sorted(names)
                    or set(names) != set(expected_names)
                    or archive.comment
                ):
                    _fail()
                declared_total = 0
                bytes_read = 0
                result: dict[str, bytes] = {}
                for info in infos:
                    name = info.filename
                    if (
                        info.is_dir()
                        or info.compress_type != zipfile.ZIP_STORED
                        or info.create_system != 3
                        or info.external_attr != _ZIP_STORED_MODE
                        or info.internal_attr != 0
                        or info.extra
                        or info.comment
                        or info.flag_bits != 0
                        or info.date_time != timestamp
                    ):
                        _fail()
                    declared_total += info.file_size
                    if declared_total > _COMPANION_WHEEL_MAX_BYTES:
                        _fail()
                    payload = _read_zip_member(
                        archive,
                        info,
                        limit=_COMPANION_WHEEL_MAX_BYTES - bytes_read,
                    )
                    bytes_read += len(payload)
                    if bytes_read > _COMPANION_WHEEL_MAX_BYTES:
                        _fail()
                    result[name] = payload
        except ApplicationCompanionsPackagingError:
            raise
        except OSError, RuntimeError, ValueError, KeyError, zipfile.BadZipFile:
            _fail()
        _verify_input(path, stream, identity)
        return result
    finally:
        stream.close()


def _validate_companion_payloads(
    payloads: dict[str, bytes],
    *,
    application_version: str,
    application_source: ApplicationSourceMetadata,
    wheel_sha256: str,
) -> ApplicationCompanionsMetadata:
    manifest_name, metadata_name, wheel_name, record_name, license_name = _wheel_paths(
        application_version
    )
    manifest_payload = payloads[manifest_name]
    if len(manifest_payload) > _APPLICATION_MANIFEST_MAX_BYTES:
        _fail()
    try:
        metadata = parse_application_companions(
            manifest_payload,
            application_source=application_source,
        )
    except TypeError, ValueError:
        _fail()
    canonical_manifest = _canonical_manifest(metadata)
    if (
        manifest_payload != canonical_manifest
        or metadata.application_version != application_version
        or metadata.wheel_sha256 != wheel_sha256
        or metadata.application_source_sha256 != application_source.sha256
        or metadata.source_revision != application_source.source_revision
    ):
        _fail()
    if payloads[metadata_name] != _metadata_payload(application_version):
        _fail()
    if payloads[wheel_name] != _wheel_payload(application_version):
        _fail()
    if payloads[license_name] != _read_project_license():
        _fail()
    expected_record = _record_payload(
        {name: payload for name, payload in payloads.items() if name != record_name},
        record_name,
    )
    if payloads[record_name] != expected_record:
        _fail()
    return metadata


def _validate_platform(
    metadata: ApplicationCompanionsMetadata, platform: str | None
) -> None:
    if platform is None:
        return
    if platform not in {"linux-64", "win-64"}:
        _fail()
    if not any(item.platform == platform for item in metadata.inventory_inputs):
        _fail()


def validate_application_companions_wheel(
    wheel: Path,
    companion_wheel: Path,
    *,
    platform: str | None = None,
) -> ApplicationCompanionsMetadata:
    """Validate a metadata-only wheel against the exact Documents wheel."""
    try:
        application = _application_wheel_identity(wheel)
        expected_stem = _companion_stem(application.application_version)
        expected_filename = f"{expected_stem}-py3-none-any.whl"
        if companion_wheel.name != expected_filename:
            _fail()
        expected_names = _wheel_paths(application.application_version)
        payloads = _companion_wheel_members(
            companion_wheel,
            expected_names=expected_names,
            timestamp=_zip_timestamp(application.source_date_epoch),
        )
        metadata = _validate_companion_payloads(
            payloads,
            application_version=application.application_version,
            application_source=application.source,
            wheel_sha256=application.sha256,
        )
        _validate_platform(metadata, platform)
        return metadata
    except ApplicationCompanionsPackagingError:
        raise
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        IndexError,
        OverflowError,
        RuntimeError,
        EOFError,
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
    ):
        raise ApplicationCompanionsPackagingError(_INVALID) from None


def _output_directory_identity(path: Path) -> tuple[int, int, int]:
    try:
        status = path.lstat()
    except OSError:
        _fail()
    if not stat.S_ISDIR(status.st_mode):
        _fail()
    return status.st_dev, status.st_ino, status.st_mode


def _verify_output_directory(path: Path, expected: tuple[int, int, int]) -> None:
    if _output_directory_identity(path) != expected:
        _fail()


def _output_absent(path: Path) -> None:
    try:
        path.lstat()
    except FileNotFoundError:
        return
    except OSError:
        _fail()
    _fail()


def _publish_noclobber(stage: Path, output: Path) -> tuple[int, int]:
    stage_status: os.stat_result | None = None
    linked = False
    try:
        stage_status = stage.lstat()
        if not stat.S_ISREG(stage_status.st_mode):
            _fail()
        os.link(stage, output)
        linked = True
        published = output.lstat()
    except FileExistsError:
        _fail()
    except ApplicationCompanionsPackagingError:
        if linked and stage_status is not None:
            _unlink_if_same_file(output, (stage_status.st_dev, stage_status.st_ino))
        raise
    except OSError:
        if linked and stage_status is not None:
            _unlink_if_same_file(output, (stage_status.st_dev, stage_status.st_ino))
        _fail()
    if not stat.S_ISREG(published.st_mode) or (published.st_dev, published.st_ino) != (
        stage_status.st_dev,
        stage_status.st_ino,
    ):
        _fail()
    return published.st_dev, published.st_ino


def _unlink_if_same_file(path: Path, identity: tuple[int, int]) -> None:
    try:
        current = path.lstat()
        if (
            stat.S_ISREG(current.st_mode)
            and (current.st_dev, current.st_ino) == identity
        ):
            path.unlink()
    except OSError:
        pass


def build_application_companions_wheel(
    wheel: Path,
    manifest: Path,
    output_dir: Path,
) -> Path:
    """Build a tiny wheel bound to one immutable Documents wheel."""
    output: Path | None = None
    published_identity: tuple[int, int] | None = None
    try:
        application = _application_wheel_identity(wheel)
        manifest_payload, manifest_identity = _read_bounded_file(
            manifest,
            limit=_APPLICATION_MANIFEST_MAX_BYTES,
        )
        manifest_sha256 = hashlib.sha256(manifest_payload).hexdigest()
        try:
            metadata = parse_application_companions(
                manifest_payload,
                application_source=application.source,
            )
        except TypeError, ValueError:
            _fail()
        if (
            metadata.application_version != application.application_version
            or metadata.source_revision != application.source.source_revision
            or metadata.application_source_sha256 != application.source.sha256
            or metadata.wheel_sha256 != application.sha256
        ):
            _fail()
        canonical_manifest = _canonical_manifest(metadata)
        filename = (
            f"{_companion_stem(application.application_version)}-py3-none-any.whl"
        )
        output = output_dir / filename
        license_payload = _read_project_license()
        output_dir.mkdir(parents=True, exist_ok=True)
        output_directory_identity = _output_directory_identity(output_dir)
        _output_absent(output)
        with tempfile.TemporaryDirectory(
            prefix=".application-companions-",
            dir=output_dir,
        ) as temporary:
            staging_directory = Path(temporary)
            staging_identity = _output_directory_identity(staging_directory)
            staged_wheel = staging_directory / filename
            descriptor = os.open(
                staged_wheel,
                os.O_RDWR
                | os.O_CREAT
                | os.O_EXCL
                | cast(int, getattr(os, "O_NOFOLLOW", 0))
                | cast(int, getattr(os, "O_CLOEXEC", 0)),
                0o600,
            )
            try:
                with os.fdopen(descriptor, "w+b", closefd=False) as stream:
                    _write_companion_wheel(
                        stream,
                        application_version=application.application_version,
                        manifest=canonical_manifest,
                        license_payload=license_payload,
                        source_date_epoch=application.source_date_epoch,
                    )
                    stream.flush()
                    os.fsync(stream.fileno())
            finally:
                os.close(descriptor)
            os.chmod(staged_wheel, 0o644)
            staged_status = staged_wheel.lstat()
            if not stat.S_ISREG(staged_status.st_mode) or (
                staged_status.st_size > _COMPANION_WHEEL_MAX_BYTES
            ):
                _fail()
            _verify_output_directory(output_dir, output_directory_identity)
            _verify_output_directory(staging_directory, staging_identity)
            _verify_path_identity(
                wheel,
                application.file_identity,
                expected_sha256=application.sha256,
            )
            _verify_path_identity(
                manifest,
                manifest_identity,
                expected_sha256=manifest_sha256,
            )
            validate_application_companions_wheel(wheel, staged_wheel)
            _verify_output_directory(output_dir, output_directory_identity)
            _output_absent(output)
            _verify_path_identity(
                manifest,
                manifest_identity,
                expected_sha256=manifest_sha256,
            )
            _verify_path_identity(
                wheel,
                application.file_identity,
                expected_sha256=application.sha256,
            )
            published_identity = _publish_noclobber(staged_wheel, output)
            return output
    except ApplicationCompanionsPackagingError:
        if output is not None and published_identity is not None:
            _unlink_if_same_file(output, published_identity)
        raise
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        IndexError,
        OverflowError,
        RuntimeError,
        EOFError,
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
    ):
        if output is not None and published_identity is not None:
            _unlink_if_same_file(output, published_identity)
        raise ApplicationCompanionsPackagingError(_INVALID) from None


def _verify_path_identity(
    path: Path,
    expected: tuple[int, int, int, int, int, int],
    *,
    expected_sha256: str,
) -> None:
    stream, identity = _open_regular_input(path)
    try:
        if identity.path != expected:
            _fail()
        digest, byte_count = _stream_sha256(stream)
        _verify_input(path, stream, identity)
        if digest != expected_sha256 or byte_count != expected[3]:
            _fail()
    finally:
        stream.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        result = build_application_companions_wheel(
            args.wheel,
            args.manifest,
            args.output_dir,
        )
    except ApplicationCompanionsPackagingError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
