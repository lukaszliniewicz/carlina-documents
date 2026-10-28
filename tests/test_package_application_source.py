"""Acceptance checks for attaching exact Documents source to its wheel."""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import tarfile
import warnings
import zipfile
from pathlib import Path

import pytest

from carlina.application_source import (
    APPLICATION_SOURCE_ARCHIVE_MEMBER,
    APPLICATION_SOURCE_MANIFEST_MEMBER,
    parse_application_source,
)
from carlina.distribution import parse_application_distribution
from scripts import package_application_source as package_source

_REVISION = "a" * 40
_EPOCH = 1_791_000_000
_VERSION = "2.3.4"
_ROOT = f"carlina-{_VERSION}"
_WHEEL_MEMBER = "carlina/distribution.json"
_PACKAGE_CODE = b"SYNTHETIC_SOURCE_VALUE = 'same artifact'\n"
_METADATA_MEMBER = f"carlina-{_VERSION}.dist-info/METADATA"
_INVALID = "Application source and wheel do not form a valid Documents release."


def _descriptor(
    *, profile: str = "documents", revision: str = _REVISION, epoch: int = _EPOCH
) -> bytes:
    return (
        json.dumps(
            {
                "format": "carlina.application-distribution",
                "schema_version": 1,
                "application_profile": profile,
                "source_revision": revision,
                "source_date_epoch": epoch,
                "addon_ids": ["doc_processor", "sentence_segmenter", "translator"],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _package_info(version: str = _VERSION) -> bytes:
    return (f"Metadata-Version: 2.1\nName: carlina\nVersion: {version}\n\n").encode(
        "ascii"
    )


def _write_tar(
    entries: list[tuple[str, str, bytes]],
) -> bytes:
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w:gz") as archive:
        for name, kind, data in entries:
            member = tarfile.TarInfo(name)
            member.mtime = _EPOCH
            member.mode = 0o755 if kind == "directory" else 0o644
            if kind == "file":
                member.size = len(data)
                archive.addfile(member, io.BytesIO(data))
            elif kind == "directory":
                member.type = tarfile.DIRTYPE
                archive.addfile(member)
            elif kind == "symlink":
                member.type = tarfile.SYMTYPE
                member.linkname = data.decode("utf-8")
                archive.addfile(member)
            else:
                raise AssertionError(f"unsupported synthetic tar member kind: {kind}")
    return payload.getvalue()


def _source_archive(
    *,
    descriptor: bytes | None = None,
    version: str = _VERSION,
    extra_members: tuple[tuple[str, str, bytes], ...] = (),
) -> bytes:
    entries = [
        (_ROOT, "directory", b""),
        (f"{_ROOT}/PKG-INFO", "file", _package_info(version)),
        (
            f"{_ROOT}/carlina/distribution.json",
            "file",
            _descriptor() if descriptor is None else descriptor,
        ),
        (f"{_ROOT}/carlina/__init__.py", "file", b'"""Synthetic package."""\n'),
        (f"{_ROOT}/carlina/sample.py", "file", _PACKAGE_CODE),
        *extra_members,
    ]
    return _write_tar(entries)


def _wheel_bytes(
    *,
    descriptor: bytes | None = None,
    version: str = _VERSION,
    extra_members: tuple[tuple[str, bytes], ...] = (),
    duplicate_member: str | None = None,
) -> bytes:
    metadata = (f"Metadata-Version: 2.1\nName: carlina\nVersion: {version}\n\n").encode(
        "ascii"
    )
    members = [
        (_WHEEL_MEMBER, _descriptor() if descriptor is None else descriptor),
        ("carlina/__init__.py", b'"""Synthetic package."""\n'),
        ("carlina/sample.py", _PACKAGE_CODE),
        (_METADATA_MEMBER, metadata),
        (
            f"carlina-{_VERSION}.dist-info/WHEEL",
            b"Wheel-Version: 1.0\nGenerator: synthetic\nRoot-Is-Purelib: true\n",
        ),
        (f"carlina-{_VERSION}.dist-info/RECORD", b""),
    ]
    replacements = {name for name, _ in extra_members}
    members = [member for member in members if member[0] not in replacements]
    members.extend(extra_members)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, data in members:
            archive.writestr(name, data)
            if name == duplicate_member:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", UserWarning)
                    archive.writestr(name, data)
    return output.getvalue()


def _write_pair(
    tmp_path: Path,
    *,
    descriptor: bytes | None = None,
    source_descriptor: bytes | None = None,
    wheel_version: str = _VERSION,
    source_version: str = _VERSION,
    wheel_members: tuple[tuple[str, bytes], ...] = (),
    source_members: tuple[tuple[str, str, bytes], ...] = (),
    duplicate_wheel_member: str | None = None,
) -> tuple[Path, Path]:
    wheel = tmp_path / f"carlina-{_VERSION}-py3-none-any.whl"
    source = tmp_path / "carlina-documents-source.tar.gz"
    wheel.write_bytes(
        _wheel_bytes(
            descriptor=descriptor,
            version=wheel_version,
            extra_members=wheel_members,
            duplicate_member=duplicate_wheel_member,
        )
    )
    source.write_bytes(
        _source_archive(
            descriptor=source_descriptor or descriptor,
            version=source_version,
            extra_members=source_members,
        )
    )
    return wheel, source


def _read_wheel(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as archive:
        return {member.filename: archive.read(member) for member in archive.infolist()}


def _record_digest(payload: bytes) -> str:
    return "sha256=" + base64.urlsafe_b64encode(
        hashlib.sha256(payload).digest()
    ).rstrip(b"=").decode("ascii")


def test_packages_exact_source_manifest_and_consistent_record(tmp_path: Path) -> None:
    wheel, source = _write_pair(tmp_path)
    source_bytes = source.read_bytes()
    descriptor = _descriptor()

    package_source.package_application_source(wheel, source)

    members = _read_wheel(wheel)
    manifest = json.loads(members[APPLICATION_SOURCE_MANIFEST_MEMBER])
    parsed_distribution = parse_application_distribution(descriptor)
    parsed_manifest = parse_application_source(
        members[APPLICATION_SOURCE_MANIFEST_MEMBER],
        distribution=parsed_distribution,
        application_version=_VERSION,
    )
    assert members[APPLICATION_SOURCE_ARCHIVE_MEMBER] == source_bytes
    assert members[_WHEEL_MEMBER] == descriptor
    assert manifest == {
        "format": "carlina.application-source",
        "schema_version": 1,
        "application_profile": "documents",
        "application_version": _VERSION,
        "source_revision": _REVISION,
        "source_date_epoch": _EPOCH,
        "archive": "application-source.tar.gz",
        "filename": source.name,
        "byte_length": len(source_bytes),
        "sha256": hashlib.sha256(source_bytes).hexdigest(),
    }
    assert parsed_manifest.sha256 == hashlib.sha256(source_bytes).hexdigest()

    record_name = f"carlina-{_VERSION}.dist-info/RECORD"
    rows = list(csv.reader(io.StringIO(members[record_name].decode("utf-8"))))
    record = {row[0]: row[1:] for row in rows}
    assert set(record) == set(members)
    for name, payload in members.items():
        digest, size = record[name]
        if name == record_name:
            assert (digest, size) == ("", "")
        else:
            assert digest == _record_digest(payload)
            assert size == str(len(payload))


def test_packaging_is_deterministic_for_identical_inputs(tmp_path: Path) -> None:
    initial_wheel = _wheel_bytes()
    source_bytes = _source_archive()
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    first_dir.mkdir()
    second_dir.mkdir()
    first_wheel = first_dir / "carlina-2.3.4-py3-none-any.whl"
    second_wheel = second_dir / "carlina-2.3.4-py3-none-any.whl"
    first_source = first_dir / "carlina-documents-source.tar.gz"
    second_source = second_dir / "carlina-documents-source.tar.gz"
    first_wheel.write_bytes(initial_wheel)
    second_wheel.write_bytes(initial_wheel)
    first_source.write_bytes(source_bytes)
    second_source.write_bytes(source_bytes)

    package_source.package_application_source(first_wheel, first_source)
    package_source.package_application_source(second_wheel, second_source)

    assert first_wheel.read_bytes() == second_wheel.read_bytes()


@pytest.mark.parametrize("mismatch", ["descriptor", "version"])
def test_identity_mismatch_leaves_original_wheel_unchanged(
    tmp_path: Path, mismatch: str
) -> None:
    options: dict[str, object] = {}
    if mismatch == "descriptor":
        options["source_descriptor"] = _descriptor(revision="b" * 40)
    else:
        options["source_version"] = "2.3.5"
    wheel, source = _write_pair(tmp_path, **options)
    original = wheel.read_bytes()

    with pytest.raises(package_source.ApplicationSourcePackagingError) as error:
        package_source.package_application_source(wheel, source)

    assert str(error.value) == _INVALID
    assert wheel.read_bytes() == original


def test_package_code_mismatch_leaves_original_wheel_unchanged(
    tmp_path: Path,
) -> None:
    wheel, source = _write_pair(
        tmp_path,
        wheel_members=(("carlina/sample.py", b"SYNTHETIC_SOURCE_VALUE = 'rebuilt'\n"),),
    )
    original = wheel.read_bytes()

    with pytest.raises(package_source.ApplicationSourcePackagingError) as error:
        package_source.package_application_source(wheel, source)

    assert str(error.value) == _INVALID
    assert wheel.read_bytes() == original


def test_wrong_profile_is_rejected_without_changing_wheel(tmp_path: Path) -> None:
    descriptor = _descriptor(profile="workbench")
    wheel, source = _write_pair(tmp_path, descriptor=descriptor)
    original = wheel.read_bytes()

    with pytest.raises(package_source.ApplicationSourcePackagingError) as error:
        package_source.package_application_source(wheel, source)

    assert str(error.value) == _INVALID
    assert wheel.read_bytes() == original


@pytest.mark.parametrize(
    "member",
    [
        (f"{_ROOT}/../outside", "file", b"unsafe"),
        ("other-root/extra.txt", "file", b"second root"),
        (f"{_ROOT}/carlina/alias", "symlink", b"__init__.py"),
    ],
    ids=["path-traversal", "multiple-roots", "symlink"],
)
def test_unsafe_or_nonregular_source_member_is_rejected(
    tmp_path: Path, member: tuple[str, str, bytes]
) -> None:
    wheel, source = _write_pair(tmp_path, source_members=(member,))
    original = wheel.read_bytes()

    with pytest.raises(package_source.ApplicationSourcePackagingError) as error:
        package_source.package_application_source(wheel, source)

    assert str(error.value) == _INVALID
    assert wheel.read_bytes() == original


def test_duplicate_source_member_is_rejected(tmp_path: Path) -> None:
    duplicate = (f"{_ROOT}/carlina/__init__.py", "file", b"duplicate")
    wheel, source = _write_pair(tmp_path, source_members=(duplicate,))
    original = wheel.read_bytes()

    with pytest.raises(package_source.ApplicationSourcePackagingError) as error:
        package_source.package_application_source(wheel, source)

    assert str(error.value) == _INVALID
    assert wheel.read_bytes() == original


@pytest.mark.parametrize("duplicate", [False, True], ids=["traversal", "duplicate"])
def test_unsafe_or_duplicate_wheel_member_is_rejected_without_rewrite(
    tmp_path: Path, duplicate: bool
) -> None:
    wheel, source = _write_pair(
        tmp_path,
        wheel_members=() if duplicate else (("../outside", b"unsafe"),),
        duplicate_wheel_member="carlina/__init__.py" if duplicate else None,
    )
    original = wheel.read_bytes()

    with pytest.raises(package_source.ApplicationSourcePackagingError) as error:
        package_source.package_application_source(wheel, source)

    assert str(error.value) == _INVALID
    assert wheel.read_bytes() == original


@pytest.mark.parametrize(
    "collision",
    [APPLICATION_SOURCE_ARCHIVE_MEMBER, APPLICATION_SOURCE_MANIFEST_MEMBER],
)
def test_existing_source_bundle_member_is_rejected_atomically(
    tmp_path: Path, collision: str
) -> None:
    wheel, source = _write_pair(
        tmp_path,
        wheel_members=((collision, b"already present"),),
    )
    original = wheel.read_bytes()

    with pytest.raises(package_source.ApplicationSourcePackagingError) as error:
        package_source.package_application_source(wheel, source)

    assert str(error.value) == _INVALID
    assert wheel.read_bytes() == original


@pytest.mark.parametrize("bound", ["source_bytes", "wheel_bytes"])
def test_compressed_artifact_byte_limits_are_enforced_with_small_bounds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bound: str
) -> None:
    wheel, source = _write_pair(tmp_path)
    original = wheel.read_bytes()
    if bound == "source_bytes":
        monkeypatch.setattr(package_source, "MAX_APPLICATION_SOURCE_BYTES", 16)
    else:
        monkeypatch.setattr(package_source, "_MAX_WHEEL_BYTES", 16)

    with pytest.raises(package_source.ApplicationSourcePackagingError) as error:
        package_source.package_application_source(wheel, source)

    assert str(error.value) == _INVALID
    assert wheel.read_bytes() == original


@pytest.mark.parametrize("archive_kind", ["source", "wheel"])
def test_member_count_limit_is_enforced_with_small_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, archive_kind: str
) -> None:
    wheel_members = (
        (("carlina/extra.bin", b"wheel member"),) if archive_kind == "wheel" else ()
    )
    source_members = (
        ((f"{_ROOT}/extra.bin", "file", b"source member"),)
        if archive_kind == "source"
        else ()
    )
    wheel, source = _write_pair(
        tmp_path,
        wheel_members=wheel_members,
        source_members=source_members,
    )
    original = wheel.read_bytes()
    monkeypatch.setattr(package_source, "_MAX_SOURCE_MEMBERS", 2)

    with pytest.raises(package_source.ApplicationSourcePackagingError) as error:
        package_source.package_application_source(wheel, source)

    assert str(error.value) == _INVALID
    assert wheel.read_bytes() == original


@pytest.mark.parametrize("archive_kind", ["source", "wheel"])
def test_expanded_size_limit_is_enforced_with_small_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, archive_kind: str
) -> None:
    source_members = (
        ((f"{_ROOT}/large.bin", "file", b"s" * 512),)
        if archive_kind == "source"
        else ()
    )
    wheel_members = (
        (("carlina/large.bin", b"w" * 512),) if archive_kind == "wheel" else ()
    )
    wheel, source = _write_pair(
        tmp_path,
        wheel_members=wheel_members,
        source_members=source_members,
    )
    original = wheel.read_bytes()
    if archive_kind == "source":
        limit = 128
    else:
        with tarfile.open(source, "r:gz") as archive:
            limit = sum(member.size for member in archive.getmembers()) + 1
    monkeypatch.setattr(package_source, "_MAX_SOURCE_EXPANDED_BYTES", limit)

    with pytest.raises(package_source.ApplicationSourcePackagingError) as error:
        package_source.package_application_source(wheel, source)

    assert str(error.value) == _INVALID
    assert wheel.read_bytes() == original


def test_wheel_metadata_limit_is_enforced_with_small_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wheel, source = _write_pair(
        tmp_path,
        wheel_members=(
            (
                _METADATA_MEMBER,
                _package_info().replace(b"\n\n", b"\nSummary: " + b"x" * 256 + b"\n\n"),
            ),
        ),
    )
    original = wheel.read_bytes()
    monkeypatch.setattr(package_source, "_MAX_PACKAGE_METADATA_BYTES", 128)

    with pytest.raises(package_source.ApplicationSourcePackagingError) as error:
        package_source.package_application_source(wheel, source)

    assert str(error.value) == _INVALID
    assert wheel.read_bytes() == original


def test_descriptor_size_limit_is_enforced_without_large_fixture(
    tmp_path: Path,
) -> None:
    descriptor = _descriptor() + b" " * 4097
    wheel, source = _write_pair(tmp_path, descriptor=descriptor)
    original = wheel.read_bytes()

    with pytest.raises(package_source.ApplicationSourcePackagingError) as error:
        package_source.package_application_source(wheel, source)

    assert str(error.value) == _INVALID
    assert wheel.read_bytes() == original


@pytest.mark.parametrize("missing_record", [True, False], ids=["missing", "multiple"])
def test_normalizer_rejects_invalid_record_layout_atomically(
    tmp_path: Path, missing_record: bool
) -> None:
    wheel, source = _write_pair(tmp_path)
    members = _read_wheel(wheel)
    if missing_record:
        del members[f"carlina-{_VERSION}.dist-info/RECORD"]
    else:
        members["other.dist-info/RECORD"] = b""
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)
    original = wheel.read_bytes()

    with pytest.raises(package_source.ApplicationSourcePackagingError) as error:
        package_source.package_application_source(wheel, source)

    assert str(error.value) == _INVALID
    assert wheel.read_bytes() == original
    assert sorted(path.name for path in tmp_path.iterdir()) == sorted(
        [wheel.name, source.name]
    )


@pytest.mark.parametrize("epoch", [1, 10**30], ids=["outside-zip-range", "overflow"])
def test_normalizer_rejects_unrepresentable_date_atomically(
    tmp_path: Path, epoch: int
) -> None:
    wheel, source = _write_pair(tmp_path, descriptor=_descriptor(epoch=epoch))
    original = wheel.read_bytes()

    with pytest.raises(package_source.ApplicationSourcePackagingError) as error:
        package_source.package_application_source(wheel, source)

    assert str(error.value) == _INVALID
    assert wheel.read_bytes() == original
    assert sorted(path.name for path in tmp_path.iterdir()) == sorted(
        [wheel.name, source.name]
    )
