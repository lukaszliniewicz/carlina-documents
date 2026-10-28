"""Synthetic acceptance fixtures for the Documents companion metadata wheel."""

from __future__ import annotations

import base64
import csv
import gzip
import hashlib
import io
import json
import os
import stat
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from email.parser import BytesParser
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from carlina.application_companions import (
    ApplicationCompanionsMetadata,
    parse_application_companions,
)
from carlina.application_source import (
    APPLICATION_SOURCE_ARCHIVE_MEMBER,
    APPLICATION_SOURCE_MANIFEST_MEMBER,
    parse_application_source,
)
from carlina.distribution import parse_application_distribution
from scripts import package_application_companions as packaging

_REVISION = "a" * 40
_EPOCH = 1_791_000_000
_VERSION = "2.3.4"
_SYNTHETIC_SOURCE = gzip.compress(b"synthetic source bytes\n", mtime=0)
_DISTRIBUTION_MEMBER = "carlina/distribution.json"


@dataclass(frozen=True, slots=True)
class SyntheticCompanionsInputs:
    wheel: Path
    manifest: Path
    output_dir: Path
    companion_wheel: Path


def _descriptor() -> bytes:
    value = {
        "format": "carlina.application-distribution",
        "schema_version": 1,
        "application_profile": "documents",
        "source_revision": _REVISION,
        "source_date_epoch": _EPOCH,
        "addon_ids": ["doc_processor", "sentence_segmenter", "translator"],
    }
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _source_manifest() -> bytes:
    value = {
        "format": "carlina.application-source",
        "schema_version": 1,
        "application_profile": "documents",
        "application_version": _VERSION,
        "source_revision": _REVISION,
        "source_date_epoch": _EPOCH,
        "archive": "application-source.tar.gz",
        "filename": "synthetic-documents-source.tar.gz",
        "byte_length": len(_SYNTHETIC_SOURCE),
        "sha256": hashlib.sha256(_SYNTHETIC_SOURCE).hexdigest(),
    }
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _record_digest(payload: bytes) -> str:
    encoded = base64.urlsafe_b64encode(hashlib.sha256(payload).digest())
    return "sha256=" + encoded.rstrip(b"=").decode("ascii")


def _record_payload(files: dict[str, bytes], record_name: str) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    for name, payload in sorted(files.items()):
        writer.writerow((name, _record_digest(payload), len(payload)))
    writer.writerow((record_name, "", ""))
    return output.getvalue().encode("utf-8")


def _zip_info(name: str) -> zipfile.ZipInfo:
    timestamp = datetime.fromtimestamp(_EPOCH, UTC)
    info = zipfile.ZipInfo(
        name,
        date_time=(
            timestamp.year,
            timestamp.month,
            timestamp.day,
            timestamp.hour,
            timestamp.minute,
            timestamp.second - timestamp.second % 2,
        ),
    )
    info.compress_type = zipfile.ZIP_STORED
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | 0o644) << 16
    info.internal_attr = 0
    info.extra = b""
    info.comment = b""
    return info


def _application_wheel_bytes(
    *,
    metadata_version: str = _VERSION,
    source_archive: bytes = _SYNTHETIC_SOURCE,
    source_manifest_name: str = APPLICATION_SOURCE_MANIFEST_MEMBER,
    extra_members: tuple[tuple[str, bytes], ...] = (),
) -> bytes:
    descriptor = _descriptor()
    source_manifest = _source_manifest()
    dist_info = f"carlina-{metadata_version}.dist-info"
    files = {
        _DISTRIBUTION_MEMBER: descriptor,
        APPLICATION_SOURCE_MANIFEST_MEMBER: source_manifest,
        APPLICATION_SOURCE_ARCHIVE_MEMBER: source_archive,
        "carlina/__init__.py": b'"""Synthetic Documents wheel fixture."""\n',
        f"{dist_info}/METADATA": (
            f"Metadata-Version: 2.4\nName: carlina\nVersion: {metadata_version}\n\n"
        ).encode("ascii"),
        f"{dist_info}/WHEEL": (
            "Wheel-Version: 1.0\nGenerator: synthetic\n"
            "Root-Is-Purelib: true\nTag: py3-none-any\n"
        ).encode("ascii"),
    }
    if source_manifest_name != APPLICATION_SOURCE_MANIFEST_MEMBER:
        del files[APPLICATION_SOURCE_MANIFEST_MEMBER]
        files[source_manifest_name] = source_manifest
    files.update(extra_members)
    record_name = f"{dist_info}/RECORD"
    files[record_name] = _record_payload(files, record_name)

    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, payload in sorted(files.items()):
            archive.writestr(_zip_info(name), payload)
    return output.getvalue()


def _companion_manifest(wheel_bytes: bytes) -> dict[str, Any]:
    source_sha256 = hashlib.sha256(_SYNTHETIC_SOURCE).hexdigest()
    names = (
        ("dependency_notices", "documents-notices.zip", "d"),
        ("dependency_sources", "documents-sources.zip", "e"),
    )
    return {
        "format": "carlina.application-companions",
        "schema_version": 1,
        "application_profile": "documents",
        "application_version": _VERSION,
        "source_revision": _REVISION,
        "application_source_sha256": source_sha256,
        "wheel_sha256": hashlib.sha256(wheel_bytes).hexdigest(),
        "inventory_inputs": [
            {"platform": "linux-64", "sha256": "b" * 64},
            {"platform": "win-64", "sha256": "c" * 64},
        ],
        "companions": [
            {
                "kind": kind,
                "filename": filename,
                "byte_length": 321 if kind == "dependency_notices" else 654,
                "sha256": digest_char * 64,
                "url": f"https://downloads.example.invalid/releases/{filename}",
                "coverage": "selected",
            }
            for kind, filename, digest_char in names
        ],
    }


def _write_json(path: Path, value: object, *, sort_keys: bool = True) -> None:
    path.write_text(
        json.dumps(value, sort_keys=sort_keys, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def make_valid_application_companions_fixture(
    tmp_path: Path,
) -> SyntheticCompanionsInputs:
    """Create a tiny reusable wheel/manifest pair with no real source payload."""
    wheel_bytes = _application_wheel_bytes()
    wheel = tmp_path / f"carlina-{_VERSION}-py3-none-any.whl"
    wheel.write_bytes(wheel_bytes)
    manifest = tmp_path / "companions.json"
    _write_json(manifest, _companion_manifest(wheel_bytes))
    output_dir = tmp_path / "companions"
    output_dir.mkdir()
    filename = f"carlina_application_companions-{_VERSION}-py3-none-any.whl"
    return SyntheticCompanionsInputs(
        wheel=wheel,
        manifest=manifest,
        output_dir=output_dir,
        companion_wheel=output_dir / filename,
    )


def _stat_with_ctime_offset(
    status: os.stat_result,
    offset: int,
) -> SimpleNamespace:
    return _stat_with_ctime(status, status.st_ctime_ns + offset)


def _stat_with_ctime(
    status: os.stat_result,
    ctime_ns: int,
) -> SimpleNamespace:
    return SimpleNamespace(
        st_dev=status.st_dev,
        st_ino=status.st_ino,
        st_mode=status.st_mode,
        st_size=status.st_size,
        st_mtime_ns=status.st_mtime_ns,
        st_ctime_ns=ctime_ns,
    )


def _offset_input_descriptor_ctime(
    monkeypatch: pytest.MonkeyPatch,
    paths: set[Path],
    *,
    freeze_ctime: bool = False,
) -> None:
    original_open = os.open
    original_fstat = os.fstat
    original_lstat = Path.lstat
    input_descriptors: set[int] = set()
    baseline_ctimes: dict[tuple[int, int], int] = {}
    for path in paths:
        status = original_lstat(path)
        baseline_ctimes[(status.st_dev, status.st_ino)] = status.st_ctime_ns

    if freeze_ctime:

        def frozen_lstat(path: Path) -> SimpleNamespace | os.stat_result:
            status = original_lstat(path)
            baseline_ctime = baseline_ctimes.get((status.st_dev, status.st_ino))
            if baseline_ctime is None:
                return status
            return _stat_with_ctime(status, baseline_ctime)

        monkeypatch.setattr(Path, "lstat", frozen_lstat)

    def tracked_open(file: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        descriptor = original_open(file, flags, *args, **kwargs)
        input_descriptors.discard(descriptor)
        try:
            is_input = Path(file) in paths
        except TypeError:
            is_input = False
        if is_input:
            input_descriptors.add(descriptor)
        return descriptor

    def offset_fstat(descriptor: int) -> SimpleNamespace | os.stat_result:
        status = original_fstat(descriptor)
        if descriptor in input_descriptors:
            if freeze_ctime:
                baseline_ctime = baseline_ctimes.get((status.st_dev, status.st_ino))
                if baseline_ctime is not None:
                    return _stat_with_ctime(
                        status,
                        baseline_ctime + 100_000_000_000,
                    )
            return _stat_with_ctime_offset(status, 100_000_000_000)
        return status

    monkeypatch.setattr(os, "open", tracked_open)
    monkeypatch.setattr(os, "fstat", offset_fstat)


def _read_companion_wheel(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as archive:
        return {info.filename: archive.read(info) for info in archive.infolist()}


def _write_companion_wheel(path: Path, files: dict[str, bytes]) -> None:
    temporary = path.with_suffix(".rewrite.whl")
    with zipfile.ZipFile(
        temporary,
        "w",
        compression=zipfile.ZIP_STORED,
        allowZip64=False,
    ) as archive:
        for name, payload in sorted(files.items()):
            archive.writestr(_zip_info(name), payload)
    temporary.replace(path)


def _mutate_file_byte_restore_mtime(
    path: Path,
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    before = path.stat()
    before_identity = packaging._identity(path.lstat())
    original_payload = path.read_bytes()
    mutation_index = len(original_payload) // 2
    with path.open("r+b") as stream:
        stream.seek(mutation_index)
        stream.write(bytes((original_payload[mutation_index] ^ 1,)))
        stream.flush()
    assert path.read_bytes() != original_payload
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = path.stat()
    after_identity = packaging._identity(path.lstat())
    assert after.st_size == before.st_size
    assert after.st_mtime_ns == before.st_mtime_ns
    return before_identity, after_identity


def test_builds_exact_metadata_wheel_and_record(tmp_path: Path) -> None:
    fixture = make_valid_application_companions_fixture(tmp_path)

    result = packaging.build_application_companions_wheel(
        fixture.wheel,
        fixture.manifest,
        fixture.output_dir,
    )
    metadata = packaging.validate_application_companions_wheel(
        fixture.wheel,
        result,
        platform="linux-64",
    )

    stem = f"carlina_application_companions-{_VERSION}"
    names = {
        f"{stem}.data/data/share/carlina/application-companions.json",
        f"{stem}.dist-info/METADATA",
        f"{stem}.dist-info/WHEEL",
        f"{stem}.dist-info/RECORD",
        f"{stem}.dist-info/licenses/LICENSE",
    }
    with zipfile.ZipFile(result) as archive:
        infos = archive.infolist()
        assert [info.filename for info in infos] == sorted(names)
        assert all(info.compress_type == zipfile.ZIP_STORED for info in infos)
        assert all(info.create_system == 3 for info in infos)
        assert all(info.external_attr == (stat.S_IFREG | 0o644) << 16 for info in infos)
        assert all(not info.extra and not info.comment for info in infos)
        assert len(infos) == 5

    members = _read_companion_wheel(result)
    manifest_name = f"{stem}.data/data/share/carlina/application-companions.json"
    parsed_source = parse_application_source(
        _read_main_member(fixture.wheel, APPLICATION_SOURCE_MANIFEST_MEMBER),
        distribution=parse_application_distribution(
            _read_main_member(fixture.wheel, _DISTRIBUTION_MEMBER)
        ),
        application_version=_VERSION,
    )
    parsed = parse_application_companions(
        members[manifest_name],
        application_source=parsed_source,
    )
    assert isinstance(parsed, ApplicationCompanionsMetadata)
    assert metadata == parsed
    assert metadata.application_version == _VERSION
    assert {item.platform for item in metadata.inventory_inputs} == {
        "linux-64",
        "win-64",
    }

    metadata_bytes = members[f"{stem}.dist-info/METADATA"]
    package_metadata = BytesParser().parsebytes(metadata_bytes)
    assert package_metadata.get_all("Name") == ["carlina-application-companions"]
    assert package_metadata.get_all("Version") == [_VERSION]
    assert package_metadata.get_all("License-Expression") == ["MIT"]
    assert package_metadata.get_all("License-File") == ["licenses/LICENSE"]
    assert package_metadata.get_all("Requires-Dist") is None
    assert package_metadata.get_all("Entry-Points") is None
    assert (
        members[f"{stem}.dist-info/licenses/LICENSE"]
        == (packaging.PROJECT_ROOT / "LICENSE").read_bytes()
    )

    record_name = f"{stem}.dist-info/RECORD"
    rows = list(csv.reader(io.StringIO(members[record_name].decode("utf-8"))))
    record = {row[0]: row[1:] for row in rows}
    assert set(record) == names
    for name, payload in members.items():
        digest, size = record[name]
        if name == record_name:
            assert (digest, size) == ("", "")
        else:
            assert digest == _record_digest(payload)
            assert size == str(len(payload))
    assert result.stat().st_size <= 512 * 1024


def _read_main_member(path: Path, name: str) -> bytes:
    with zipfile.ZipFile(path) as archive:
        return archive.read(name)


def test_reordered_input_manifest_builds_identical_wheel(tmp_path: Path) -> None:
    fixture = make_valid_application_companions_fixture(tmp_path)
    first = packaging.build_application_companions_wheel(
        fixture.wheel,
        fixture.manifest,
        fixture.output_dir,
    )
    first_bytes = first.read_bytes()

    source_manifest = json.loads(fixture.manifest.read_text(encoding="utf-8"))
    source_manifest["inventory_inputs"].reverse()
    source_manifest["companions"].reverse()
    source_manifest = dict(reversed(tuple(source_manifest.items())))
    reordered_manifest = tmp_path / "reordered.json"
    _write_json(reordered_manifest, source_manifest, sort_keys=False)
    second_output = tmp_path / "second"
    second_output.mkdir()

    second = packaging.build_application_companions_wheel(
        fixture.wheel,
        reordered_manifest,
        second_output,
    )

    assert second.read_bytes() == first_bytes


def test_cross_api_ctime_difference_builds_identical_wheel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = make_valid_application_companions_fixture(tmp_path)
    baseline = packaging.build_application_companions_wheel(
        fixture.wheel,
        fixture.manifest,
        fixture.output_dir,
    )
    baseline_bytes = baseline.read_bytes()
    packaging.validate_application_companions_wheel(
        fixture.wheel,
        baseline,
        platform="win-64",
    )

    offset_output = tmp_path / "offset-output"
    offset_output.mkdir()
    _offset_input_descriptor_ctime(
        monkeypatch,
        {fixture.wheel, fixture.manifest},
    )
    result = packaging.build_application_companions_wheel(
        fixture.wheel,
        fixture.manifest,
        offset_output,
    )
    packaging.validate_application_companions_wheel(
        fixture.wheel,
        result,
        platform="win-64",
    )

    assert result.read_bytes() == baseline_bytes


@pytest.mark.parametrize(
    ("mutated_input", "freeze_ctime"),
    [
        ("wheel", False),
        ("manifest", False),
        ("wheel", True),
        ("manifest", True),
    ],
)
def test_rejects_input_mutation_during_wheel_write_with_ctime_offset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutated_input: str,
    freeze_ctime: bool,
) -> None:
    fixture = make_valid_application_companions_fixture(tmp_path)
    target = fixture.wheel if mutated_input == "wheel" else fixture.manifest
    original_writer = packaging._write_companion_wheel

    def write_then_mutate(*args: Any, **kwargs: Any) -> None:
        original_writer(*args, **kwargs)
        before_identity, after_identity = _mutate_file_byte_restore_mtime(target)
        if freeze_ctime:
            assert after_identity == before_identity

    monkeypatch.setattr(packaging, "_write_companion_wheel", write_then_mutate)
    _offset_input_descriptor_ctime(
        monkeypatch,
        {fixture.wheel, fixture.manifest},
        freeze_ctime=freeze_ctime,
    )

    with pytest.raises(packaging.ApplicationCompanionsPackagingError):
        packaging.build_application_companions_wheel(
            fixture.wheel,
            fixture.manifest,
            fixture.output_dir,
        )

    assert list(fixture.output_dir.iterdir()) == []


def test_rejects_late_wheel_mutation_after_staged_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = make_valid_application_companions_fixture(tmp_path)
    original_validator = packaging.validate_application_companions_wheel

    def validate_then_mutate(
        *args: Any, **kwargs: Any
    ) -> ApplicationCompanionsMetadata:
        result = original_validator(*args, **kwargs)
        before_identity, after_identity = _mutate_file_byte_restore_mtime(fixture.wheel)
        assert after_identity == before_identity
        return result

    monkeypatch.setattr(
        packaging,
        "validate_application_companions_wheel",
        validate_then_mutate,
    )
    _offset_input_descriptor_ctime(
        monkeypatch,
        {fixture.wheel, fixture.manifest},
        freeze_ctime=True,
    )

    with pytest.raises(packaging.ApplicationCompanionsPackagingError):
        packaging.build_application_companions_wheel(
            fixture.wheel,
            fixture.manifest,
            fixture.output_dir,
        )

    assert list(fixture.output_dir.iterdir()) == []


def test_verify_input_checks_descriptor_ctime_independently(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = make_valid_application_companions_fixture(tmp_path)
    path_identity = packaging._identity(fixture.wheel.lstat())
    stream, expected = packaging._open_regular_input(fixture.wheel)
    original_fstat = os.fstat
    descriptor = stream.fileno()

    def offset_descriptor_ctime(
        file_descriptor: int,
    ) -> SimpleNamespace | os.stat_result:
        status = original_fstat(file_descriptor)
        if file_descriptor == descriptor:
            return _stat_with_ctime_offset(status, 100_000_000_000)
        return status

    monkeypatch.setattr(os, "fstat", offset_descriptor_ctime)
    try:
        with pytest.raises(packaging.ApplicationCompanionsPackagingError):
            packaging._verify_input(fixture.wheel, stream, expected)
    finally:
        stream.close()

    assert packaging._identity(fixture.wheel.lstat()) == path_identity


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("application_version", "2.3.5"),
        ("source_revision", "f" * 40),
        ("application_source_sha256", "0" * 64),
        ("wheel_sha256", "1" * 64),
    ],
)
def test_rejects_manifest_identity_mismatches(
    tmp_path: Path,
    field: str,
    replacement: str,
) -> None:
    fixture = make_valid_application_companions_fixture(tmp_path)
    value = json.loads(fixture.manifest.read_text(encoding="utf-8"))
    value[field] = replacement
    _write_json(fixture.manifest, value)

    with pytest.raises(packaging.ApplicationCompanionsPackagingError):
        packaging.build_application_companions_wheel(
            fixture.wheel,
            fixture.manifest,
            fixture.output_dir,
        )

    assert list(fixture.output_dir.iterdir()) == []


def test_rejects_main_wheel_version_mismatch(tmp_path: Path) -> None:
    fixture = make_valid_application_companions_fixture(tmp_path)
    fixture.wheel.write_bytes(_application_wheel_bytes(metadata_version="2.3.5"))

    with pytest.raises(packaging.ApplicationCompanionsPackagingError):
        packaging.build_application_companions_wheel(
            fixture.wheel,
            fixture.manifest,
            fixture.output_dir,
        )


def test_rejects_embedded_source_byte_mismatch(tmp_path: Path) -> None:
    fixture = make_valid_application_companions_fixture(tmp_path)
    fixture.wheel.write_bytes(
        _application_wheel_bytes(source_archive=_SYNTHETIC_SOURCE + b"changed")
    )

    with pytest.raises(packaging.ApplicationCompanionsPackagingError):
        packaging.build_application_companions_wheel(
            fixture.wheel,
            fixture.manifest,
            fixture.output_dir,
        )


def test_rejects_case_insensitive_duplicate_main_wheel_names(tmp_path: Path) -> None:
    fixture = make_valid_application_companions_fixture(tmp_path)
    fixture.wheel.write_bytes(
        _application_wheel_bytes(
            extra_members=(("Carlina/application-source.json", _source_manifest()),)
        )
    )

    with pytest.raises(packaging.ApplicationCompanionsPackagingError):
        packaging.build_application_companions_wheel(
            fixture.wheel,
            fixture.manifest,
            fixture.output_dir,
        )


def test_validator_checks_requested_platform_and_known_platforms(
    tmp_path: Path,
) -> None:
    fixture = make_valid_application_companions_fixture(tmp_path)
    companion = packaging.build_application_companions_wheel(
        fixture.wheel,
        fixture.manifest,
        fixture.output_dir,
    )

    packaging.validate_application_companions_wheel(
        fixture.wheel,
        companion,
        platform="win-64",
    )
    for platform in ("linux-aarch64", "macos-64"):
        with pytest.raises(packaging.ApplicationCompanionsPackagingError):
            packaging.validate_application_companions_wheel(
                fixture.wheel,
                companion,
                platform=platform,
            )


@pytest.mark.parametrize("tamper", ["extra-file", "record", "crc", "oversize"])
def test_validator_rejects_tampered_or_open_companion_wheels(
    tmp_path: Path,
    tamper: str,
) -> None:
    fixture = make_valid_application_companions_fixture(tmp_path)
    companion = packaging.build_application_companions_wheel(
        fixture.wheel,
        fixture.manifest,
        fixture.output_dir,
    )
    members = _read_companion_wheel(companion)
    stem = f"carlina_application_companions-{_VERSION}"
    record_name = f"{stem}.dist-info/RECORD"
    license_name = f"{stem}.dist-info/licenses/LICENSE"

    if tamper == "extra-file":
        members["unexpected.py"] = b"raise RuntimeError('extra code')\n"
        _write_companion_wheel(companion, members)
    elif tamper == "record":
        members[record_name] += b"unexpected,,\n"
        _write_companion_wheel(companion, members)
    elif tamper == "oversize":
        members[license_name] += b"x" * (512 * 1024)
        _write_companion_wheel(companion, members)
    else:
        with zipfile.ZipFile(companion) as archive:
            info = archive.getinfo(license_name)
            data_offset = (
                info.header_offset
                + 30
                + len(info.filename.encode("utf-8"))
                + len(info.extra)
            )
        with companion.open("r+b") as stream:
            stream.seek(data_offset)
            original = stream.read(1)
            stream.seek(data_offset)
            stream.write(bytes((original[0] ^ 1,)))

    with pytest.raises(packaging.ApplicationCompanionsPackagingError):
        packaging.validate_application_companions_wheel(fixture.wheel, companion)


def test_existing_output_is_preserved_and_failure_cleans_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = make_valid_application_companions_fixture(tmp_path)
    fixture.companion_wheel.write_bytes(b"preexisting output")

    with pytest.raises(packaging.ApplicationCompanionsPackagingError):
        packaging.build_application_companions_wheel(
            fixture.wheel,
            fixture.manifest,
            fixture.output_dir,
        )
    assert fixture.companion_wheel.read_bytes() == b"preexisting output"
    fixture.companion_wheel.unlink()

    def reject_staged_wheel(
        *_args: object, **_kwargs: object
    ) -> ApplicationCompanionsMetadata:
        raise packaging.ApplicationCompanionsPackagingError("synthetic rejection")

    monkeypatch.setattr(
        packaging, "validate_application_companions_wheel", reject_staged_wheel
    )
    with pytest.raises(packaging.ApplicationCompanionsPackagingError):
        packaging.build_application_companions_wheel(
            fixture.wheel,
            fixture.manifest,
            fixture.output_dir,
        )

    assert list(fixture.output_dir.iterdir()) == []


def test_rejects_symlink_and_oversized_manifest_inputs(tmp_path: Path) -> None:
    fixture = make_valid_application_companions_fixture(tmp_path)
    symlink = tmp_path / "companions-link.json"
    try:
        symlink.symlink_to(fixture.manifest)
    except OSError:
        pytest.skip("symlinks are unavailable in this test environment")
    with pytest.raises(packaging.ApplicationCompanionsPackagingError):
        packaging.build_application_companions_wheel(
            fixture.wheel,
            symlink,
            fixture.output_dir,
        )

    fixture.manifest.write_bytes(b" " * (16 * 1024 + 1))
    with pytest.raises(packaging.ApplicationCompanionsPackagingError):
        packaging.build_application_companions_wheel(
            fixture.wheel,
            fixture.manifest,
            fixture.output_dir,
        )
