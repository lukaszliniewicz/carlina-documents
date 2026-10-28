"""Strict tests for the immutable Documents source bundle verifier."""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
from typing import Never, Self

import pytest

from carlina import application_source
from carlina.distribution import ApplicationDistribution
from carlina.runtime_profiles import RuntimeProfile, RuntimeProfileError

_REVISION = "a" * 40
_EPOCH = 1_791_000_000
_VERSION = "2.3.4"
_ARCHIVE = b"synthetic application source archive"
_MANIFEST_ERROR = "Application source manifest is malformed."
_READ_ERROR = "Application source package could not be read."
_ARCHIVE_ERROR = "Application source archive is invalid."


def _distribution(
    *,
    profile: RuntimeProfile = RuntimeProfile.DOCUMENTS,
    source_revision: str | None = _REVISION,
    source_date_epoch: int | None = _EPOCH,
) -> ApplicationDistribution:
    return ApplicationDistribution(
        profile=profile,
        source_revision=source_revision,
        source_date_epoch=source_date_epoch,
        addon_ids=("doc_processor", "sentence_segmenter", "translator"),
    )


def _manifest(
    archive: bytes = _ARCHIVE,
    **updates: object,
) -> dict[str, object]:
    return {
        "format": "carlina.application-source",
        "schema_version": 1,
        "application_profile": "documents",
        "application_version": _VERSION,
        "source_revision": _REVISION,
        "source_date_epoch": _EPOCH,
        "archive": "application-source.tar.gz",
        "filename": "carlina-documents.tar.gz",
        "byte_length": len(archive),
        "sha256": hashlib.sha256(archive).hexdigest(),
        **updates,
    }


def _encode(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode("utf-8")


def _parse(
    payload: bytes,
    *,
    distribution: ApplicationDistribution | None = None,
    application_version: str = _VERSION,
) -> application_source.ApplicationSourceMetadata:
    return application_source.parse_application_source(
        payload,
        distribution=distribution or _distribution(),
        application_version=application_version,
    )


class _TrackedStream:
    def __init__(self, path: Path, reads: list[tuple[str, int]]) -> None:
        self.path = path
        self.reads = reads
        self.stream: io.BufferedReader | None = None

    def __enter__(self) -> Self:
        self.stream = self.path.open("rb")
        return self

    def __exit__(self, *_args: object) -> None:
        if self.stream is not None:
            self.stream.close()

    def read(self, size: int = -1) -> bytes:
        assert self.stream is not None
        self.reads.append((self.path.name, size))
        return self.stream.read(size)


class _TrackedResource:
    def __init__(self, path: Path, reads: list[tuple[str, int]]) -> None:
        self.path = path
        self.reads = reads

    def open(self, mode: str) -> _TrackedStream:
        assert mode == "rb"
        return _TrackedStream(self.path, self.reads)


class _TrackedPackage:
    def __init__(self, root: Path, reads: list[tuple[str, int]]) -> None:
        self.root = root
        self.reads = reads

    def joinpath(self, name: str) -> _TrackedResource:
        return _TrackedResource(self.root / name, self.reads)


def _install_documents_package(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    manifest: bytes | None,
    archive: bytes | None,
) -> list[tuple[str, int]]:
    package_root = tmp_path / "carlina"
    package_root.mkdir()
    if manifest is not None:
        (package_root / "application-source.json").write_bytes(manifest)
    if archive is not None:
        (package_root / "application-source.tar.gz").write_bytes(archive)

    reads: list[tuple[str, int]] = []
    monkeypatch.setattr(
        application_source,
        "files",
        lambda package: (
            _TrackedPackage(package_root, reads)
            if package == "carlina"
            else pytest.fail("unexpected package resource")
        ),
    )
    monkeypatch.setattr(
        application_source,
        "read_application_distribution",
        lambda: _distribution(),
    )
    monkeypatch.setattr(application_source, "version", lambda package: _VERSION)
    return reads


def test_parse_valid_manifest_returns_frozen_metadata() -> None:
    result = _parse(_encode(_manifest()))

    assert result == application_source.ApplicationSourceMetadata(
        application_version=_VERSION,
        source_revision=_REVISION,
        source_date_epoch=_EPOCH,
        filename="carlina-documents.tar.gz",
        byte_length=len(_ARCHIVE),
        sha256=hashlib.sha256(_ARCHIVE).hexdigest(),
    )
    with pytest.raises(AttributeError):
        result.filename = "changed.tar.gz"  # type: ignore[misc]


@pytest.mark.parametrize(
    "payload",
    [
        b"\xff",
        b"not json",
        b"null",
        b"[]",
        b'{"format":"carlina.application-source","format":"private"}',
        _encode({**_manifest(), "extra_private": "account data"}),
        _encode({key: value for key, value in _manifest().items() if key != "format"}),
        _encode({**_manifest(), "format": "other"}),
        _encode({**_manifest(), "schema_version": 2}),
        _encode({**_manifest(), "application_profile": "workbench"}),
        _encode({**_manifest(), "archive": "other.tar.gz"}),
    ],
)
def test_rejects_malformed_closed_manifests_without_echoing_values(
    payload: bytes,
) -> None:
    with pytest.raises(application_source.ApplicationSourceError) as error:
        _parse(payload)

    assert str(error.value) == _MANIFEST_ERROR
    assert "account data" not in str(error.value)
    assert "private" not in str(error.value)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("schema_version", True),
        ("source_date_epoch", True),
        ("byte_length", True),
    ],
)
def test_boolean_values_are_not_accepted_as_integers(key: str, value: bool) -> None:
    with pytest.raises(application_source.ApplicationSourceError) as error:
        _parse(_encode({**_manifest(), key: value}))

    assert str(error.value) == _MANIFEST_ERROR


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("application_version", "2.3.5"),
        ("source_revision", "b" * 40),
        ("source_date_epoch", _EPOCH + 1),
    ],
)
def test_manifest_identity_must_match_installed_distribution(
    key: str,
    value: object,
) -> None:
    with pytest.raises(application_source.ApplicationSourceError) as error:
        _parse(_encode({**_manifest(), key: value}))

    assert str(error.value) == _MANIFEST_ERROR


@pytest.mark.parametrize(
    "filename",
    [
        "../source.tar.gz",
        "source..tar.gz",
        "folder/source.tar.gz",
        "folder\\source.tar.gz",
        "bad\nname.tar.gz",
        "bad name.tar.gz",
        "é.tar.gz",
        f"{'a' * 194}.tar.gz",
    ],
)
def test_filename_must_be_a_bounded_safe_ascii_basename(filename: str) -> None:
    with pytest.raises(application_source.ApplicationSourceError) as error:
        _parse(_encode({**_manifest(), "filename": filename}))

    assert str(error.value) == _MANIFEST_ERROR


@pytest.mark.parametrize("sha256", ["A" * 64, "a" * 63, "g" * 64])
def test_sha256_must_be_lowercase_hex(sha256: str) -> None:
    with pytest.raises(application_source.ApplicationSourceError) as error:
        _parse(_encode({**_manifest(), "sha256": sha256}))

    assert str(error.value) == _MANIFEST_ERROR


@pytest.mark.parametrize(
    "byte_length", [0, -1, application_source.MAX_APPLICATION_SOURCE_BYTES + 1]
)
def test_archive_length_must_be_positive_and_bounded(byte_length: int) -> None:
    with pytest.raises(application_source.ApplicationSourceError) as error:
        _parse(_encode({**_manifest(), "byte_length": byte_length}))

    assert str(error.value) == _MANIFEST_ERROR


@pytest.mark.parametrize("version", ["", "v2.3.4", "2.3.4/evil", "2" + "a" * 200])
def test_package_version_must_be_bounded_safe_and_exact(version: str) -> None:
    with pytest.raises(application_source.ApplicationSourceError) as error:
        _parse(_encode(_manifest()), application_version=version)

    assert str(error.value) == _MANIFEST_ERROR


def test_manifest_is_bounded_to_4096_bytes() -> None:
    payload = _encode(_manifest()) + b" " * 4096

    with pytest.raises(application_source.ApplicationSourceError) as error:
        _parse(payload)

    assert str(error.value) == _MANIFEST_ERROR


def test_reader_returns_exact_verified_archive_bytes_and_bounded_reads(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    reads = _install_documents_package(
        monkeypatch,
        tmp_path,
        manifest=_encode(_manifest()),
        archive=_ARCHIVE,
    )

    result = application_source.read_application_source()

    assert result is not None
    assert result.archive == _ARCHIVE
    assert result.metadata.sha256 == hashlib.sha256(_ARCHIVE).hexdigest()
    assert reads == [
        ("application-source.json", 4097),
        ("application-source.tar.gz", len(_ARCHIVE) + 1),
    ]


def test_workbench_does_not_probe_source_resources_or_package_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        application_source,
        "read_application_distribution",
        lambda: _distribution(
            profile=RuntimeProfile.WORKBENCH,
            source_revision=None,
            source_date_epoch=None,
        ),
    )
    monkeypatch.setattr(
        application_source,
        "files",
        lambda _package: pytest.fail("Workbench probed package resources"),
    )
    monkeypatch.setattr(
        application_source,
        "version",
        lambda _package: pytest.fail("Workbench probed package version"),
    )

    assert application_source.read_application_source() is None


def test_older_documents_package_without_manifest_returns_none(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_documents_package(
        monkeypatch,
        tmp_path,
        manifest=None,
        archive=None,
    )
    monkeypatch.setattr(
        application_source,
        "version",
        lambda _package: pytest.fail("version read without a source manifest"),
    )

    assert application_source.read_application_source() is None


def test_unreadable_package_resource_errors_are_redacted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UnreadablePackage:
        def joinpath(self, _name: str) -> Never:
            raise OSError("private package resource path")

    monkeypatch.setattr(
        application_source,
        "read_application_distribution",
        lambda: _distribution(),
    )
    monkeypatch.setattr(
        application_source, "files", lambda _package: UnreadablePackage()
    )
    monkeypatch.setattr(
        application_source,
        "version",
        lambda _package: pytest.fail("version read after resource error"),
    )

    with pytest.raises(application_source.ApplicationSourceError) as error:
        application_source.read_application_source()

    assert str(error.value) == _READ_ERROR
    assert "private package resource path" not in str(error.value)


def test_runtime_profile_error_from_distribution_validation_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invalid_distribution = RuntimeProfileError("descriptor detail")
    monkeypatch.setattr(
        application_source,
        "read_application_distribution",
        lambda: (_ for _ in ()).throw(invalid_distribution),
    )
    monkeypatch.setattr(
        application_source,
        "files",
        lambda _package: pytest.fail("source read after invalid distribution"),
    )

    with pytest.raises(RuntimeProfileError) as error:
        application_source.read_application_source()

    assert error.value is invalid_distribution


@pytest.mark.parametrize(
    ("stored_archive", "expected_archive", "error_text"),
    [
        (None, _ARCHIVE, _READ_ERROR),
        (_ARCHIVE[:-1], _ARCHIVE, _ARCHIVE_ERROR),
        (_ARCHIVE + b"x", _ARCHIVE, _ARCHIVE_ERROR),
        (b"x" * len(_ARCHIVE), _ARCHIVE, _ARCHIVE_ERROR),
    ],
)
def test_reader_rejects_missing_truncated_oversized_or_tampered_archives(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    stored_archive: bytes | None,
    expected_archive: bytes,
    error_text: str,
) -> None:
    _install_documents_package(
        monkeypatch,
        tmp_path,
        manifest=_encode(_manifest(expected_archive)),
        archive=stored_archive,
    )

    with pytest.raises(application_source.ApplicationSourceError) as error:
        application_source.read_application_source()

    assert str(error.value) == error_text
    assert "tampered" not in str(error.value)


def test_reader_caps_archive_read_at_global_limit_plus_one(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    payload = _manifest(_ARCHIVE)
    payload["byte_length"] = application_source.MAX_APPLICATION_SOURCE_BYTES
    reads = _install_documents_package(
        monkeypatch,
        tmp_path,
        manifest=_encode(payload),
        archive=_ARCHIVE,
    )

    with pytest.raises(application_source.ApplicationSourceError) as error:
        application_source.read_application_source()

    assert str(error.value) == _ARCHIVE_ERROR
    assert reads[-1] == (
        "application-source.tar.gz",
        application_source.MAX_APPLICATION_SOURCE_BYTES + 1,
    )


def test_resource_and_missing_installed_version_errors_are_redacted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manifest = _encode(_manifest())
    _install_documents_package(
        monkeypatch,
        tmp_path,
        manifest=manifest,
        archive=_ARCHIVE,
    )
    monkeypatch.setattr(
        application_source,
        "version",
        lambda _package: (_ for _ in ()).throw(OSError("private version path")),
    )

    with pytest.raises(application_source.ApplicationSourceError) as error:
        application_source.read_application_source()

    assert str(error.value) == _READ_ERROR
    assert "private version path" not in str(error.value)
