"""Strict parser and fixed-prefix reader tests for application companions."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import BinaryIO, Self, cast

import pytest

from carlina import application_companions
from carlina.application_source import ApplicationSourceMetadata

_REVISION = "a" * 40
_VERSION = "2.3.4"
_SOURCE_BYTES = b"synthetic Documents application source"
_SOURCE_SHA256 = hashlib.sha256(_SOURCE_BYTES).hexdigest()
_WHEEL_SHA256 = "b" * 64
_INVALID_ERROR = "Application companions manifest is malformed."
_READ_ERROR = "Application companions manifest could not be read."


def _application_source() -> ApplicationSourceMetadata:
    return ApplicationSourceMetadata(
        application_version=_VERSION,
        source_revision=_REVISION,
        source_date_epoch=1_791_000_000,
        filename="carlina-documents.tar.gz",
        byte_length=len(_SOURCE_BYTES),
        sha256=_SOURCE_SHA256,
    )


def _manifest(**updates: object) -> dict[str, object]:
    return {
        "format": "carlina.application-companions",
        "schema_version": 1,
        "application_profile": "documents",
        "application_version": _VERSION,
        "source_revision": _REVISION,
        "application_source_sha256": _SOURCE_SHA256,
        "wheel_sha256": _WHEEL_SHA256,
        "inventory_inputs": [
            {"platform": "linux-64", "sha256": "c" * 64},
            {"platform": "win-64", "sha256": "d" * 64},
        ],
        "companions": [
            {
                "kind": "dependency_sources",
                "filename": "carlina-sources.zip",
                "byte_length": 1234,
                "sha256": "e" * 64,
                "url": "https://example.org/releases/carlina-sources.zip",
                "coverage": "selected",
            },
            {
                "kind": "dependency_notices",
                "filename": "carlina-notices.zip",
                "byte_length": 567,
                "sha256": "f" * 64,
                "url": "https://example.org/releases/carlina-notices.zip",
                "coverage": "selected",
            },
        ],
        **updates,
    }


def _companion(index: int = 0) -> dict[str, object]:
    entries = _manifest()["companions"]
    assert isinstance(entries, list)
    entry = entries[index]
    assert isinstance(entry, dict)
    return cast(dict[str, object], entry)


def _encode(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode("utf-8")


def _parse(
    payload: bytes,
    *,
    application_source: ApplicationSourceMetadata | None = None,
) -> application_companions.ApplicationCompanionsMetadata:
    return application_companions.parse_application_companions(
        payload,
        application_source=application_source or _application_source(),
    )


def _install_prefix(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    payload: bytes | None,
) -> tuple[Path, Path]:
    prefix = tmp_path / "prefix"
    manifest_path = prefix / "share" / "carlina" / "application-companions.json"
    manifest_path.parent.mkdir(parents=True)
    if payload is not None:
        manifest_path.write_bytes(payload)
    monkeypatch.setattr(application_companions.sys, "prefix", str(prefix))
    return prefix, manifest_path


def test_parse_returns_immutable_metadata_bound_to_application_source() -> None:
    result = _parse(_encode(_manifest()))

    assert result == application_companions.ApplicationCompanionsMetadata(
        application_version=_VERSION,
        source_revision=_REVISION,
        application_source_sha256=_SOURCE_SHA256,
        wheel_sha256=_WHEEL_SHA256,
        inventory_inputs=(
            application_companions.CompanionInventory("linux-64", "c" * 64),
            application_companions.CompanionInventory("win-64", "d" * 64),
        ),
        companions=(
            application_companions.CompanionDownload(
                "dependency_sources",
                "carlina-sources.zip",
                1234,
                "e" * 64,
                "https://example.org/releases/carlina-sources.zip",
                "selected",
            ),
            application_companions.CompanionDownload(
                "dependency_notices",
                "carlina-notices.zip",
                567,
                "f" * 64,
                "https://example.org/releases/carlina-notices.zip",
                "selected",
            ),
        ),
    )
    with pytest.raises(AttributeError):
        result.wheel_sha256 = "0" * 64  # type: ignore[misc]
    with pytest.raises(AttributeError):
        result.extra = "value"  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("application_version", "2.3.5"),
        ("source_revision", "b" * 40),
        ("application_source_sha256", "0" * 64),
    ],
)
def test_manifest_identity_must_match_verified_application_source(
    field: str,
    value: str,
) -> None:
    with pytest.raises(application_companions.ApplicationCompanionsError) as error:
        _parse(_encode(_manifest(**{field: value})))

    assert str(error.value) == _INVALID_ERROR


@pytest.mark.parametrize(
    "payload",
    [
        b"\xff",
        b"not json",
        b"null",
        b"[]",
        b"NaN",
        b'{"format":"carlina.application-companions","format":"private"}',
        b"[[[[[[[[[[[[[[[0]]]]]]]]]]]]]]]",
        _encode({**_manifest(), "private_key": "do not echo"}),
        _encode({key: value for key, value in _manifest().items() if key != "format"}),
        _encode({**_manifest(), "format": "other"}),
        _encode({**_manifest(), "schema_version": 2}),
        _encode({**_manifest(), "application_profile": "workbench"}),
        _encode(
            {
                **_manifest(),
                "inventory_inputs": [
                    {"platform": "linux-64", "sha256": "c" * 64, "private": "x"}
                ],
            }
        ),
        _encode(
            {
                **_manifest(),
                "companions": [
                    {
                        **_companion(),
                        "private": "x",
                    }
                ],
            }
        ),
    ],
)
def test_rejects_malformed_closed_manifests_without_echoing_values(
    payload: bytes,
) -> None:
    with pytest.raises(application_companions.ApplicationCompanionsError) as error:
        _parse(payload)

    assert str(error.value) == _INVALID_ERROR
    assert "private" not in str(error.value)
    assert "do not echo" not in str(error.value)


def test_rejects_duplicate_nested_keys() -> None:
    payload = _encode(_manifest()).replace(
        b'"platform":"linux-64"',
        b'"platform":"linux-64","platform":"win-64"',
        1,
    )

    with pytest.raises(application_companions.ApplicationCompanionsError) as error:
        _parse(payload)

    assert str(error.value) == _INVALID_ERROR


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", True),
        ("application_version", 2),
        ("source_revision", 1),
        ("application_source_sha256", "A" * 64),
        ("wheel_sha256", "g" * 64),
        ("inventory_inputs", {}),
        ("companions", {}),
    ],
)
def test_rejects_wrong_root_types_and_digest_shapes(field: str, value: object) -> None:
    with pytest.raises(application_companions.ApplicationCompanionsError) as error:
        _parse(_encode(_manifest(**{field: value})))

    assert str(error.value) == _INVALID_ERROR


def test_rejects_invalid_or_unbounded_source_metadata() -> None:
    invalid_source = replace(_application_source(), source_revision="not-a-revision")

    with pytest.raises(application_companions.ApplicationCompanionsError) as error:
        _parse(_encode(_manifest()), application_source=invalid_source)

    assert str(error.value) == _INVALID_ERROR


@pytest.mark.parametrize(
    "inventory_inputs",
    [
        [],
        [{"platform": "linux-64", "sha256": "c" * 64}] * 3,
        [
            {"platform": "linux-64", "sha256": "c" * 64},
            {"platform": "linux-64", "sha256": "d" * 64},
        ],
        [{"platform": "macos-64", "sha256": "c" * 64}],
        [{"platform": "linux-64", "sha256": "C" * 64}],
        [{"platform": True, "sha256": "c" * 64}],
    ],
)
def test_rejects_invalid_inventory_inputs(inventory_inputs: object) -> None:
    with pytest.raises(application_companions.ApplicationCompanionsError) as error:
        _parse(_encode(_manifest(inventory_inputs=inventory_inputs)))

    assert str(error.value) == _INVALID_ERROR


@pytest.mark.parametrize(
    "filename",
    [
        "../source.zip",
        "source..zip",
        "folder/source.zip",
        "folder\\source.zip",
        "bad name.zip",
        "bad\nname.zip",
        "é.zip",
        "CON.zip",
        "Lpt9.sources.zip",
        "not-a-zip.tar.gz",
        f"{'a' * 197}.zip",
    ],
)
def test_companion_filename_is_a_safe_ascii_basename(filename: str) -> None:
    companion = {
        **_companion(),
        "url": f"https://example.org/releases/{filename}",
    }
    companion = {**companion, "filename": filename}

    with pytest.raises(application_companions.ApplicationCompanionsError) as error:
        _parse(_encode(_manifest(companions=[companion])))

    assert str(error.value) == _INVALID_ERROR


def test_accepts_maximum_length_companion_filename() -> None:
    filename = f"{'a' * 196}.zip"
    companion = {
        **_companion(),
        "filename": filename,
        "url": f"https://example.org/releases/{filename}",
    }

    result = _parse(_encode(_manifest(companions=[companion])))

    assert result.companions[0].filename == filename


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("kind", "other"),
        ("kind", "dependency_notices"),
        ("byte_length", True),
        ("byte_length", 0),
        ("byte_length", -1),
        ("byte_length", 4 * 1024 * 1024 * 1024 + 1),
        ("sha256", "A" * 64),
        ("coverage", "complete"),
    ],
)
def test_rejects_invalid_companion_values(field: str, value: object) -> None:
    first = _companion()
    second = _companion(1)
    companions = [{**first, field: value}, second]

    with pytest.raises(application_companions.ApplicationCompanionsError) as error:
        _parse(_encode(_manifest(companions=companions)))

    assert str(error.value) == _INVALID_ERROR


@pytest.mark.parametrize(
    "url",
    [
        "http://example.org/releases/carlina-sources.zip",
        "http://localhost/releases/carlina-sources.zip",
        "http://127.0.0.1:0/releases/carlina-sources.zip",
        "http://[::2]/releases/carlina-sources.zip",
        "https://user:secret@example.org/releases/carlina-sources.zip",
        "https://example.org:99999/releases/carlina-sources.zip",
        "https://example.org:/releases/carlina-sources.zip",
        "https://example.org/releases/carlina-sources.zip?token=x",
        "https://example.org/releases/carlina-sources.zip#fragment",
        "https://example.org/releases/other.zip",
        "https://example.org/releases/%2e%2e/carlina-sources.zip",
        "https://example.org/releases/%5c/carlina-sources.zip",
        "https://example.org/releases/carlina-sources.zip%00",
        "https://example.org\\@evil.test/releases/carlina-sources.zip",
        "https://bad_host.example/releases/carlina-sources.zip",
        "https://example.org/releases/carlina-sources.zip%ZZ",
        "https://example.org/releases/carlina-sources.zip\n",
        "https://example.org/releases/carlina-sources.zipé",
        "ftp://example.org/releases/carlina-sources.zip",
        f"https://example.org/{'a' * 2048}/carlina-sources.zip",
    ],
)
def test_rejects_unsafe_companion_urls(url: str) -> None:
    first = _companion()
    companions = [{**first, "url": url}]

    with pytest.raises(application_companions.ApplicationCompanionsError) as error:
        _parse(_encode(_manifest(companions=companions)))

    assert str(error.value) == _INVALID_ERROR


@pytest.mark.parametrize(
    "url",
    [
        "https://example.org/releases/carlina-sources.zip",
        "http://127.0.0.1:8080/releases/carlina-sources.zip",
        "http://127.255.3.4/releases/carlina-sources.zip",
        "http://[::1]:8080/releases/carlina-sources.zip",
    ],
)
def test_accepts_https_and_numeric_loopback_http_urls(url: str) -> None:
    first = _companion()
    result = _parse(_encode(_manifest(companions=[{**first, "url": url}])))

    assert result.companions[0].url == url


@pytest.mark.parametrize(
    "companions",
    [
        [],
        [{"kind": "dependency_sources"}],
        [
            {
                "kind": "dependency_sources",
                "filename": "same.zip",
                "byte_length": 1,
                "sha256": "a" * 64,
                "url": "https://example.org/same.zip",
                "coverage": "selected",
            },
            {
                "kind": "dependency_notices",
                "filename": "same.zip",
                "byte_length": 1,
                "sha256": "b" * 64,
                "url": "https://example.org/same.zip",
                "coverage": "selected",
            },
        ],
    ],
)
def test_rejects_empty_malformed_or_duplicate_companions(companions: object) -> None:
    with pytest.raises(application_companions.ApplicationCompanionsError) as error:
        _parse(_encode(_manifest(companions=companions)))

    assert str(error.value) == _INVALID_ERROR


def test_manifest_and_companion_size_bounds() -> None:
    first = _companion()
    result = _parse(
        _encode(
            _manifest(
                companions=[
                    {
                        **first,
                        "byte_length": 4 * 1024 * 1024 * 1024,
                    }
                ]
            )
        )
    )
    assert result.companions[0].byte_length == 4 * 1024 * 1024 * 1024

    payload = _encode(_manifest())
    payload = payload.ljust(application_companions.MAX_APPLICATION_COMPANIONS_BYTES)
    assert len(payload) == application_companions.MAX_APPLICATION_COMPANIONS_BYTES
    assert _parse(payload).wheel_sha256 == _WHEEL_SHA256

    oversized = payload + b" "
    with pytest.raises(application_companions.ApplicationCompanionsError) as error:
        _parse(oversized)
    assert str(error.value) == _INVALID_ERROR


def test_reader_returns_none_when_fixed_manifest_is_absent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prefix = tmp_path / "prefix"
    prefix.mkdir()
    monkeypatch.setattr(application_companions.sys, "prefix", str(prefix))

    assert (
        application_companions.read_application_companions(
            application_source=_application_source()
        )
        is None
    )


def test_reader_resolves_a_symlinked_development_prefix(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    real_prefix, manifest_path = _install_prefix(
        monkeypatch,
        tmp_path,
        _encode(_manifest()),
    )
    symlink_prefix = tmp_path / "prefix-link"
    try:
        symlink_prefix.symlink_to(real_prefix, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"directory symlinks are unavailable: {error}")
    monkeypatch.setattr(application_companions.sys, "prefix", str(symlink_prefix))

    result = application_companions.read_application_companions(
        application_source=_application_source()
    )

    assert manifest_path.is_file()
    assert result is not None
    assert result.wheel_sha256 == _WHEEL_SHA256


def test_reparse_point_attributes_are_treated_as_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reparse_point = 0x400
    monkeypatch.setattr(
        application_companions.stat,
        "FILE_ATTRIBUTE_REPARSE_POINT",
        reparse_point,
        raising=False,
    )
    synthetic_directory_stat = SimpleNamespace(
        st_mode=application_companions.stat.S_IFDIR,
        st_file_attributes=reparse_point,
    )

    assert application_companions._is_redirected(
        cast(os.stat_result, synthetic_directory_stat)
    )


@pytest.mark.parametrize("payload", [b"", b"x" * 16_385])
def test_reader_rejects_empty_and_oversized_manifests(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    payload: bytes,
) -> None:
    _install_prefix(monkeypatch, tmp_path, payload)

    with pytest.raises(application_companions.ApplicationCompanionsError):
        application_companions.read_application_companions(
            application_source=_application_source()
        )


def test_reader_rejects_nonregular_manifest_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _prefix, manifest_path = _install_prefix(monkeypatch, tmp_path, None)
    manifest_path.mkdir()

    with pytest.raises(application_companions.ApplicationCompanionsError) as error:
        application_companions.read_application_companions(
            application_source=_application_source()
        )

    assert str(error.value) == _READ_ERROR


@pytest.mark.parametrize("symlink_component", ["share", "carlina", "manifest"])
def test_reader_rejects_symlinked_manifest_components(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    symlink_component: str,
) -> None:
    prefix = tmp_path / "prefix"
    external = tmp_path / "external"
    external.mkdir()
    if symlink_component == "share":
        prefix.mkdir()
        (prefix / "share").symlink_to(external, target_is_directory=True)
    elif symlink_component == "carlina":
        (prefix / "share").mkdir(parents=True)
        (prefix / "share" / "carlina").symlink_to(
            external,
            target_is_directory=True,
        )
    else:
        package_dir = prefix / "share" / "carlina"
        package_dir.mkdir(parents=True)
        target = external / "manifest.json"
        target.write_bytes(_encode(_manifest()))
        (package_dir / "application-companions.json").symlink_to(target)
    monkeypatch.setattr(application_companions.sys, "prefix", str(prefix))

    with pytest.raises(application_companions.ApplicationCompanionsError) as error:
        application_companions.read_application_companions(
            application_source=_application_source()
        )

    assert str(error.value) == _READ_ERROR


def test_reader_caps_the_single_file_read_at_limit_plus_one(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_prefix(monkeypatch, tmp_path, _encode(_manifest()))
    original_fdopen = os.fdopen
    read_sizes: list[int] = []

    class TrackedStream:
        def __init__(self, stream: BinaryIO) -> None:
            self.stream = stream

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_args: object) -> None:
            self.stream.close()

        def fileno(self) -> int:
            return self.stream.fileno()

        def read(self, size: int = -1) -> bytes:
            read_sizes.append(size)
            return self.stream.read(size)

    def tracked_fdopen(descriptor: int, mode: str = "r") -> TrackedStream:
        return TrackedStream(cast(BinaryIO, original_fdopen(descriptor, mode)))

    monkeypatch.setattr(application_companions.os, "fdopen", tracked_fdopen)

    result = application_companions.read_application_companions(
        application_source=_application_source()
    )

    assert result is not None
    assert read_sizes == [application_companions.MAX_APPLICATION_COMPANIONS_BYTES + 1]
