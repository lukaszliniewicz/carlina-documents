from __future__ import annotations

import bz2
import compression.zstd as zstd
import hashlib
import io
import json
import os
import stat
import sys
import tarfile
import warnings
import zipfile
from pathlib import Path

import pytest

import scripts.documents_conda_notices as notices
from scripts.documents_conda_notices import (
    CondaNoticeBundle,
    CondaNoticeError,
    read_conda_notices,
)

_NAME = "sample-package"
_VERSION = "1.2.3"
_BUILD = "h0_0"
_SUBDIR = "linux-64"
_NOTICE = b"notice bytes\xff\xfe\n"
_INDEX = {
    "name": _NAME,
    "version": _VERSION,
    "build": _BUILD,
    "subdir": _SUBDIR,
}


def _tar_bytes(
    entries: list[tuple[str, bytes | None, bytes | None]],
    *,
    index: dict[str, object] | None = None,
    raw_index: bytes | None = None,
    index_first: bool = True,
    tar_format: int = tarfile.PAX_FORMAT,
) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tar_format) as archive:
        index_entry: tuple[str, bytes | None, bytes | None] | None = None
        if raw_index is not None:
            index_entry = ("info/index.json", raw_index, None)
        elif index is not None:
            index_entry = ("info/index.json", json.dumps(index).encode("utf-8"), None)
        if index_entry is not None:
            entries = (
                [index_entry, *entries] if index_first else [*entries, index_entry]
            )
        for path, content, member_type in entries:
            member = tarfile.TarInfo(path)
            if member_type is not None:
                member.type = member_type
                member.mode = 0o777
                if member_type in {tarfile.SYMTYPE, tarfile.LNKTYPE}:
                    member.linkname = "../../untrusted-target"
                if member_type == tarfile.DIRTYPE:
                    member.mode = 0o755
                archive.addfile(member)
            else:
                assert content is not None
                member.size = len(content)
                member.mode = 0o644
                archive.addfile(member, io.BytesIO(content))
    return buffer.getvalue()


def _write_conda(
    path: Path,
    *,
    info_entries: list[tuple[str, bytes | None, bytes | None]] | None = None,
    package_entries: list[tuple[str, bytes | None, bytes | None]] | None = None,
    index: dict[str, object] | None = None,
    raw_index: bytes | None = None,
    metadata: bytes = b'{"conda_pkg_format_version":2}',
    zip_entries: list[tuple[str, bytes, int]] | None = None,
) -> Path:
    index_record = _INDEX if index is None else index
    info_data = zstd.compress(
        _tar_bytes(
            list(info_entries or []),
            index=index_record,
            raw_index=raw_index,
        )
    )
    package_data = zstd.compress(_tar_bytes(list(package_entries or [])))
    entries = zip_entries or [
        ("metadata.json", metadata, zipfile.ZIP_STORED),
        ("info-synthetic.tar.zst", info_data, zipfile.ZIP_STORED),
        ("pkg-synthetic.tar.zst", package_data, zipfile.ZIP_STORED),
    ]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(path, mode="w") as archive:
            for name, content, compression in entries:
                archive.writestr(name, content, compress_type=compression)
    return path


def _write_legacy(
    path: Path,
    entries: list[tuple[str, bytes | None, bytes | None]] | None = None,
    *,
    index: dict[str, object] | None = None,
) -> Path:
    path.write_bytes(
        bz2.compress(_tar_bytes(list(entries or []), index=index or _INDEX))
    )
    return path


def _invoke(
    path: Path, *, name: str = _NAME, version: str = _VERSION, build: str = _BUILD
) -> CondaNoticeBundle:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return read_conda_notices(
        path,
        expected_sha256=digest,
        name=name,
        version=version,
        build=build,
    )


def _by_path(bundle: CondaNoticeBundle, path: str):
    return next(member for member in bundle.members if member.relative_path == path)


def test_conda_v2_scans_both_inner_tars_and_preserves_notice_bytes(
    tmp_path: Path,
) -> None:
    archive = _write_conda(
        tmp_path / "sample.conda",
        info_entries=[
            ("info/about.json", b'{"summary":"sample"}', None),
            ("info/hash_input.json", b'{"source":"locked"}', None),
            ("info/recipe/meta.yaml", b"recipe: retained as data\n", None),
            ("info/recipe/COPYING.txt", b"recipe license\x00", None),
            ("info/COPYRIGHT", b"not a legacy root candidate", None),
        ],
        package_entries=[
            ("info/licenses/LICENCE.txt", _NOTICE, None),
            ("usr/share/doc/unrelated-link", None, tarfile.SYMTYPE),
            ("usr/share/doc/unrelated-hardlink", None, tarfile.LNKTYPE),
            ("usr/share/font.bin", b"ignored payload", None),
        ],
    )

    bundle = _invoke(archive)

    assert bundle.archive_sha256 == hashlib.sha256(archive.read_bytes()).hexdigest()
    assert bundle.subdir == _SUBDIR
    assert _by_path(bundle, "info/licenses/LICENCE.txt").content == _NOTICE
    assert _by_path(bundle, "info/licenses/LICENCE.txt").kind == "license"
    assert _by_path(bundle, "info/recipe/meta.yaml").kind == "recipe"
    assert _by_path(bundle, "info/recipe/COPYING.txt").kind == "license"
    assert all(member.relative_path != "info/COPYRIGHT" for member in bundle.members)
    assert all(
        member.relative_path != "usr/share/font.bin" for member in bundle.members
    )
    assert all(
        member.relative_path != "usr/share/doc/unrelated-link"
        for member in bundle.members
    )
    assert all(
        member.relative_path != "usr/share/doc/unrelated-hardlink"
        for member in bundle.members
    )
    for member in bundle.members:
        assert member.sha256 == hashlib.sha256(member.content).hexdigest()


@pytest.mark.parametrize("archive_format", ["conda", "legacy"])
def test_payload_license_directories_retain_nonstandard_notice_names(
    tmp_path: Path, archive_format: str
) -> None:
    paths = [
        "share/licenses/gcc-libs/RUNTIME.LIBRARY.EXCEPTION",
        "Library/share/licenses/runtime/redistribution.rtf",
        "lib/python3.14/site-packages/sample.dist-info/licenses/AUTHORS",
    ]
    entries = [(path, _NOTICE, None) for path in paths]
    unrelated = "share/doc/sample/RUNTIME.LIBRARY.EXCEPTION"
    entries.append((unrelated, b"not in a licence directory", None))
    archive = (
        _write_conda(tmp_path / "sample.conda", package_entries=entries)
        if archive_format == "conda"
        else _write_legacy(tmp_path / "sample.tar.bz2", entries)
    )

    bundle = _invoke(archive)

    for path in paths:
        member = _by_path(bundle, path)
        assert member.kind == "license"
        assert member.content == _NOTICE
        assert member.sha256 == hashlib.sha256(_NOTICE).hexdigest()
    assert all(member.relative_path != unrelated for member in bundle.members)


@pytest.mark.parametrize("archive_format", ["conda", "legacy"])
@pytest.mark.parametrize("member_type", [tarfile.SYMTYPE, tarfile.LNKTYPE])
def test_payload_notice_directory_links_are_rejected(
    tmp_path: Path, archive_format: str, member_type: bytes
) -> None:
    entries = [("share/licenses/runtime/exception", None, member_type)]
    archive = (
        _write_conda(tmp_path / "sample.conda", package_entries=entries)
        if archive_format == "conda"
        else _write_legacy(tmp_path / "sample.tar.bz2", entries)
    )

    with pytest.raises(CondaNoticeError, match="invalid Conda archive"):
        _invoke(archive)


def test_legacy_tar_bz2_selects_root_notice_and_allows_no_licenses(
    tmp_path: Path,
) -> None:
    archive = _write_legacy(
        tmp_path / "sample.tar.bz2",
        [
            ("info/LICENCE.txt", b"legacy license", None),
            ("info/about.json", b'{"summary":"metadata only"}', None),
            ("info/recipe/build.sh", b"echo not executed\n", None),
            ("usr/share/doc/COPYRIGHT", b"installed payload notice", None),
        ],
    )

    bundle = _invoke(archive)

    assert _by_path(bundle, "info/LICENCE.txt").kind == "license"
    assert _by_path(bundle, "info/recipe/build.sh").kind == "recipe"
    assert _by_path(bundle, "usr/share/doc/COPYRIGHT").kind == "license"

    empty_notice = _write_legacy(
        tmp_path / "without-license.tar.bz2",
        [("info/about.json", b'{"summary":"none"}', None)],
    )
    bundle_without_license = _invoke(empty_notice)
    assert (
        sum(member.kind == "license" for member in bundle_without_license.members) == 0
    )
    assert (
        sum(member.kind == "metadata" for member in bundle_without_license.members) == 2
    )


@pytest.mark.parametrize(
    ("field", "wrong_value"),
    [("name", "another-package"), ("version", "9.9"), ("build", "other_build")],
)
def test_archive_index_identity_must_match_expected(
    tmp_path: Path, field: str, wrong_value: str
) -> None:
    index = dict(_INDEX)
    index[field] = wrong_value
    archive = _write_conda(tmp_path / "bad.conda", index=index)

    with pytest.raises(CondaNoticeError) as caught:
        _invoke(archive)

    assert wrong_value not in str(caught.value)


def test_conflicting_duplicate_index_identity_keys_are_rejected(
    tmp_path: Path,
) -> None:
    raw_index = (
        b'{"name":"another-package","name":"sample-package",'
        b'"version":"1.2.3","build":"h0_0","subdir":"linux-64"}'
    )
    archive = _write_conda(tmp_path / "duplicate-index-key.conda", raw_index=raw_index)

    with pytest.raises(CondaNoticeError) as caught:
        _invoke(archive)

    assert "another-package" not in str(caught.value)


def test_duplicate_conda_pkg_format_version_keys_are_rejected(
    tmp_path: Path,
) -> None:
    metadata = b'{"conda_pkg_format_version":1,"conda_pkg_format_version":2}'
    archive = _write_conda(
        tmp_path / "duplicate-format-version.conda", metadata=metadata
    )

    with pytest.raises(CondaNoticeError) as caught:
        _invoke(archive)

    assert "conda_pkg_format_version" not in str(caught.value)


def test_optional_subdir_is_preserved_and_malformed_subdir_is_rejected(
    tmp_path: Path,
) -> None:
    no_subdir = dict(_INDEX)
    no_subdir.pop("subdir")
    archive = _write_conda(tmp_path / "without-subdir.conda", index=no_subdir)
    assert _invoke(archive).subdir is None

    malformed = dict(_INDEX)
    malformed["subdir"] = []
    archive = _write_conda(tmp_path / "bad-subdir.conda", index=malformed)
    with pytest.raises(CondaNoticeError):
        _invoke(archive)


def test_single_dot_prefix_is_normalized_for_selected_relative_paths(
    tmp_path: Path,
) -> None:
    archive = _write_legacy(
        tmp_path / "dot-prefix.tar.bz2",
        [("./info/licenses/NOTICE", b"notice", None)],
    )

    bundle = _invoke(archive)

    assert _by_path(bundle, "info/licenses/NOTICE").content == b"notice"


@pytest.mark.parametrize(
    "unsafe_path",
    [
        "info/licenses/../NOTICE",
        "../info/licenses/NOTICE",
        "info/licenses//NOTICE",
        "info/licenses/./NOTICE",
        "../info/licenses/legal-notice.txt",
        "/info/index.json",
        "info//index.json",
        "/info/licenses/NOTICE",
        "C:/info/licenses/NOTICE",
        "info/licenses/NOTICE\\private",
        "info/licenses/NOT\x01ICE",
        "share/licenses/../RUNTIME.LIBRARY.EXCEPTION",
        "/share/licenses/runtime/exception",
        "Library/share/licenses/runtime/exception\\private",
        "share\\licenses\\runtime\\exception",
    ],
)
def test_selected_unsafe_paths_are_rejected(tmp_path: Path, unsafe_path: str) -> None:
    archive = _write_legacy(
        tmp_path / "unsafe.tar.bz2",
        [(unsafe_path, b"private notice", None)],
    )

    with pytest.raises(CondaNoticeError) as caught:
        _invoke(archive)

    assert unsafe_path not in str(caught.value)
    assert "private notice" not in str(caught.value)


@pytest.mark.parametrize(
    "member_type", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.CHRTYPE]
)
def test_selected_nonregular_notice_members_are_rejected(
    tmp_path: Path, member_type: bytes
) -> None:
    archive = _write_legacy(
        tmp_path / "selected-link.tar.bz2",
        [("info/licenses/NOTICE", None, member_type)],
    )

    with pytest.raises(CondaNoticeError):
        _invoke(archive)


def test_duplicate_selected_paths_across_inner_tars_are_rejected(
    tmp_path: Path,
) -> None:
    archive = _write_conda(
        tmp_path / "duplicate.conda",
        info_entries=[("info/licenses/NOTICE", b"first", None)],
        package_entries=[("info/licenses/NOTICE", b"second", None)],
    )

    with pytest.raises(CondaNoticeError):
        _invoke(archive)


@pytest.mark.parametrize(
    "metadata",
    [
        b"not-json",
        b"[]",
        b'{"conda_pkg_format_version":true}',
        b'{"conda_pkg_format_version":1}',
    ],
)
def test_conda_zip_metadata_must_be_a_v2_json_object(
    tmp_path: Path, metadata: bytes
) -> None:
    archive = _write_conda(tmp_path / "bad-metadata.conda", metadata=metadata)

    with pytest.raises(CondaNoticeError):
        _invoke(archive)


def test_conda_zip_rejects_extra_duplicate_unsafe_and_symlink_members(
    tmp_path: Path,
) -> None:
    valid_info = zstd.compress(_tar_bytes([], index=_INDEX))
    valid_package = zstd.compress(_tar_bytes([]))
    cases: list[list[tuple[str, bytes, int]]] = [
        [
            ("metadata.json", b'{"conda_pkg_format_version":2}', zipfile.ZIP_STORED),
            ("info-a.tar.zst", valid_info, zipfile.ZIP_STORED),
            ("pkg-a.tar.zst", valid_package, zipfile.ZIP_STORED),
            ("extra", b"x", zipfile.ZIP_STORED),
        ],
        [
            ("metadata.json", b'{"conda_pkg_format_version":2}', zipfile.ZIP_STORED),
            ("metadata.json", b'{"conda_pkg_format_version":2}', zipfile.ZIP_STORED),
            ("info-a.tar.zst", valid_info, zipfile.ZIP_STORED),
        ],
        [
            ("../metadata.json", b'{"conda_pkg_format_version":2}', zipfile.ZIP_STORED),
            ("info-a.tar.zst", valid_info, zipfile.ZIP_STORED),
            ("pkg-a.tar.zst", valid_package, zipfile.ZIP_STORED),
        ],
    ]
    symlink = zipfile.ZipInfo("metadata.json")
    symlink.create_system = 3
    symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
    for index, entries in enumerate(cases):
        archive = _write_conda(tmp_path / f"bad-zip-{index}.conda", zip_entries=entries)
        with pytest.raises(CondaNoticeError):
            _invoke(archive)

    # Replace the normal metadata entry's type bits with a ZIP symlink entry.
    symlink_archive = tmp_path / "symlink-member.conda"
    with zipfile.ZipFile(symlink_archive, mode="w") as archive_file:
        archive_file.writestr(symlink, b'{"conda_pkg_format_version":2}')
        archive_file.writestr("info-a.tar.zst", valid_info)
        archive_file.writestr("pkg-a.tar.zst", valid_package)
    with pytest.raises(CondaNoticeError):
        _invoke(symlink_archive)


def test_deflated_conda_zip_is_bounded_and_supported(tmp_path: Path) -> None:
    archive = _write_conda(
        tmp_path / "deflated.conda",
        zip_entries=[
            ("metadata.json", b'{"conda_pkg_format_version":2}', zipfile.ZIP_DEFLATED),
            (
                "info-a.tar.zst",
                zstd.compress(_tar_bytes([], index=_INDEX)),
                zipfile.ZIP_DEFLATED,
            ),
            ("pkg-a.tar.zst", zstd.compress(_tar_bytes([])), zipfile.ZIP_DEFLATED),
        ],
    )

    assert _invoke(archive).subdir == _SUBDIR


def test_conda_zip_rejects_unsupported_compression(tmp_path: Path) -> None:
    archive = _write_conda(
        tmp_path / "bzip2-outer.conda",
        zip_entries=[
            ("metadata.json", b'{"conda_pkg_format_version":2}', zipfile.ZIP_BZIP2),
            (
                "info-a.tar.zst",
                zstd.compress(_tar_bytes([], index=_INDEX)),
                zipfile.ZIP_STORED,
            ),
            ("pkg-a.tar.zst", zstd.compress(_tar_bytes([])), zipfile.ZIP_STORED),
        ],
    )

    with pytest.raises(CondaNoticeError):
        _invoke(archive)


@pytest.mark.parametrize(
    "invalid_info_name",
    ["../info-a.tar.zst", "./info-a.tar.zst", "info-a.tar.bz2", "info-.tar.zst"],
)
def test_conda_zip_requires_safe_expected_inner_names(
    tmp_path: Path, invalid_info_name: str
) -> None:
    archive = _write_conda(
        tmp_path / "bad-inner-name.conda",
        zip_entries=[
            ("metadata.json", b'{"conda_pkg_format_version":2}', zipfile.ZIP_STORED),
            (
                invalid_info_name,
                zstd.compress(_tar_bytes([], index=_INDEX)),
                zipfile.ZIP_STORED,
            ),
            ("pkg-a.tar.zst", zstd.compress(_tar_bytes([])), zipfile.ZIP_STORED),
        ],
    )

    with pytest.raises(CondaNoticeError):
        _invoke(archive)


def test_hash_mismatch_and_unexpected_extension_fail_without_echo(
    tmp_path: Path,
) -> None:
    archive = _write_legacy(tmp_path / "private-name.tar.bz2")
    with pytest.raises(CondaNoticeError) as caught:
        read_conda_notices(
            archive,
            expected_sha256="0" * 64,
            name="private-product",
            version=_VERSION,
            build=_BUILD,
        )
    assert "private-name" not in str(caught.value)
    assert "private-product" not in str(caught.value)

    other = tmp_path / "opaque.archive"
    other.write_bytes(archive.read_bytes())
    with pytest.raises(CondaNoticeError):
        _invoke(other)


def test_selected_json_files_must_be_objects(tmp_path: Path) -> None:
    archive = _write_conda(
        tmp_path / "bad-about.conda",
        info_entries=[("info/about.json", b"[]", None)],
    )

    with pytest.raises(CondaNoticeError):
        _invoke(archive)


def test_corrupt_and_truncated_tar_or_zstd_streams_are_rejected(
    tmp_path: Path,
) -> None:
    good_tar = _tar_bytes([], index=_INDEX)
    truncated_tar = good_tar[:1024]
    archive = _write_legacy_bytes(
        tmp_path / "truncated.tar.bz2", bz2.compress(truncated_tar)
    )
    with pytest.raises(CondaNoticeError):
        _invoke(archive)

    corrupt_tar = bytearray(good_tar)
    corrupt_tar[0] ^= 0x01
    archive = _write_legacy_bytes(
        tmp_path / "corrupt.tar.bz2", bz2.compress(corrupt_tar)
    )
    with pytest.raises(CondaNoticeError):
        _invoke(archive)

    archive = _write_conda(tmp_path / "truncated-zstd.conda")
    archive_data = archive.read_bytes()
    with zipfile.ZipFile(io.BytesIO(archive_data)) as source_zip:
        metadata = source_zip.read("metadata.json")
        info_data = source_zip.read("info-synthetic.tar.zst")[:-3]
        package_data = source_zip.read("pkg-synthetic.tar.zst")
    broken = _write_conda(
        tmp_path / "broken-zstd.conda",
        zip_entries=[
            ("metadata.json", metadata, zipfile.ZIP_STORED),
            ("info-a.tar.zst", info_data, zipfile.ZIP_STORED),
            ("pkg-a.tar.zst", package_data, zipfile.ZIP_STORED),
        ],
    )
    with pytest.raises(CondaNoticeError):
        _invoke(broken)


def test_tar_requires_two_zero_end_blocks_after_last_member_body(
    tmp_path: Path,
) -> None:
    notice_path = "info/licenses/zero-filled-notice.bin"
    raw_tar = _tar_bytes(
        [(notice_path, bytes(512), None)],
        index=_INDEX,
    )
    with tarfile.open(fileobj=io.BytesIO(raw_tar), mode="r:") as archive:
        members = archive.getmembers()
    last_member = members[-1]
    assert last_member.name == notice_path
    assert last_member.size == 512
    body_end = (
        last_member.offset_data
        + (
            (last_member.size + notices._TAR_BLOCK_BYTES - 1)
            // notices._TAR_BLOCK_BYTES
        )
        * notices._TAR_BLOCK_BYTES
    )

    one_end_block = _write_legacy_bytes(
        tmp_path / "one-end-block.tar.bz2",
        bz2.compress(raw_tar[: body_end + notices._TAR_BLOCK_BYTES]),
    )
    with pytest.raises(CondaNoticeError):
        _invoke(one_end_block)

    two_end_blocks = _write_legacy_bytes(
        tmp_path / "two-end-blocks.tar.bz2",
        bz2.compress(raw_tar[: body_end + 2 * notices._TAR_BLOCK_BYTES]),
    )
    bundle = _invoke(two_end_blocks)
    assert _by_path(bundle, notice_path).content == bytes(512)


def _write_legacy_bytes(path: Path, content: bytes) -> Path:
    path.write_bytes(content)
    return path


def test_license_member_can_exceed_old_two_mib_cap(tmp_path: Path) -> None:
    content = bytes(range(256)) * (notices._MAX_SELECTED_MEMBER_BYTES // 256 + 1)
    assert len(content) > notices._MAX_SELECTED_MEMBER_BYTES
    archive = _write_conda(
        tmp_path / "large-license.conda",
        package_entries=[("info/licenses/THIRDPARTY", content, None)],
    )

    bundle = _invoke(archive)

    member = _by_path(bundle, "info/licenses/THIRDPARTY")
    assert member.content == content
    assert member.sha256 == hashlib.sha256(content).hexdigest()


def test_license_member_obeys_its_specific_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _write_conda(
        tmp_path / "license-cap.conda",
        package_entries=[("info/licenses/NOTICE", b"123456789", None)],
    )
    monkeypatch.setattr(notices, "_MAX_SELECTED_LICENSE_BYTES", 8)

    with pytest.raises(CondaNoticeError):
        _invoke(archive)


@pytest.mark.parametrize(
    "selected_path",
    ["info/about.json", "info/recipe/build.sh"],
)
def test_metadata_and_recipe_members_keep_the_two_mib_limit(
    tmp_path: Path, selected_path: str
) -> None:
    content = bytes(notices._MAX_SELECTED_MEMBER_BYTES + 1)
    archive = _write_conda(
        tmp_path / f"large-small-kind-{selected_path.rsplit('/', 1)[-1]}.conda",
        info_entries=[(selected_path, content, None)],
    )

    with pytest.raises(CondaNoticeError):
        _invoke(archive)


@pytest.mark.parametrize(
    ("tar_format", "header_type", "hook_name"),
    [
        (tarfile.PAX_FORMAT, tarfile.XHDTYPE, "_proc_pax"),
        (tarfile.GNU_FORMAT, tarfile.GNUTYPE_LONGNAME, "_proc_gnulong"),
    ],
)
def test_tar_extension_caps_run_before_parent_buffering(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tar_format: int,
    header_type: bytes,
    hook_name: str,
) -> None:
    long_path = f"info/licenses/{'notice-' * 30}THIRDPARTY"
    raw_tar = _tar_bytes(
        [(long_path, b"license", None)],
        index=_INDEX,
        index_first=False,
        tar_format=tar_format,
    )
    assert raw_tar[156:157] == header_type
    archive = _write_legacy_bytes(
        tmp_path / f"{hook_name}.tar.bz2",
        bz2.compress(raw_tar),
    )
    monkeypatch.setattr(notices, "_MAX_TAR_EXTENSION_BYTES", 32)
    hook_calls: list[str] = []
    parent_calls: list[str] = []
    original_hook = getattr(notices._BoundedTarInfo, hook_name)
    original_parent_call = notices._BoundedTarInfo._call_parent_processor

    def observe_hook(
        tar_info: notices._BoundedTarInfo, current_archive: tarfile.TarFile
    ) -> tarfile.TarInfo:
        hook_calls.append(hook_name)
        return original_hook(tar_info, current_archive)

    def observe_parent_call(
        tar_info: notices._BoundedTarInfo,
        name: str,
        current_archive: tarfile.TarFile,
    ) -> tarfile.TarInfo:
        parent_calls.append(name)
        return original_parent_call(tar_info, name, current_archive)

    monkeypatch.setattr(notices._BoundedTarInfo, hook_name, observe_hook)
    monkeypatch.setattr(
        notices._BoundedTarInfo,
        "_call_parent_processor",
        observe_parent_call,
    )

    with pytest.raises(CondaNoticeError):
        _invoke(archive)

    assert hook_calls == [hook_name]
    assert parent_calls == []


@pytest.mark.parametrize(
    ("constant_name", "limit"),
    [
        ("_MAX_ARCHIVE_BYTES", 1),
        ("_MAX_METADATA_JSON_BYTES", 2),
        ("_MAX_INFO_TAR_BYTES", 2_000),
        ("_MAX_TAR_BYTES", 2_000),
        ("_MAX_SELECTED_MEMBER_BYTES", 2),
        ("_MAX_SELECTED_TOTAL_BYTES", 2),
        ("_MAX_MEMBERS", 1),
        ("_MAX_SELECTED_MEMBERS", 1),
        ("_MAX_RELATIVE_PATH_CHARS", 8),
    ],
)
def test_reader_enforces_documented_caps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    constant_name: str,
    limit: int,
) -> None:
    archive = _write_conda(
        tmp_path / "caps.conda",
        info_entries=[("info/licenses/NOTICE", b"notice-content", None)],
    )
    monkeypatch.setattr(notices, constant_name, limit)

    with pytest.raises(CondaNoticeError):
        _invoke(archive)


def test_archive_and_selected_member_limits_apply_to_legacy_streams(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _write_legacy(
        tmp_path / "legacy-caps.tar.bz2",
        [("info/licenses/NOTICE", b"notice-content", None)],
    )
    monkeypatch.setattr(notices, "_MAX_TAR_BYTES", 2_000)
    with pytest.raises(CondaNoticeError):
        _invoke(archive)


def test_reader_rejects_symlink_and_nonregular_archive_without_path_leak(
    tmp_path: Path,
) -> None:
    target = _write_legacy(tmp_path / "private-target.tar.bz2")
    link = tmp_path / "private-link.tar.bz2"
    link.symlink_to(target)
    with pytest.raises(CondaNoticeError) as caught:
        _invoke(link)
    assert str(link) not in str(caught.value)
    assert str(target) not in str(caught.value)

    with pytest.raises(CondaNoticeError):
        read_conda_notices(
            tmp_path,
            expected_sha256="0" * 64,
            name=_NAME,
            version=_VERSION,
            build=_BUILD,
        )


def test_reader_detects_path_replacement_after_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _write_legacy(tmp_path / "stable.tar.bz2")
    replacement = _write_legacy(tmp_path / "replacement.tar.bz2")
    expected = hashlib.sha256(archive.read_bytes()).hexdigest()
    original_read = os.read
    replaced = False

    def replace_after_first_hash_chunk(descriptor: int, size: int) -> bytes:
        nonlocal replaced
        result = original_read(descriptor, size)
        if not replaced and result:
            os.replace(replacement, archive)
            replaced = True
        return result

    monkeypatch.setattr(notices.os, "read", replace_after_first_hash_chunk)
    with pytest.raises(CondaNoticeError):
        read_conda_notices(
            archive,
            expected_sha256=expected,
            name=_NAME,
            version=_VERSION,
            build=_BUILD,
        )


def test_reader_does_not_write_or_import_application_code(tmp_path: Path) -> None:
    archive = _write_conda(
        tmp_path / "readonly.conda",
        package_entries=[("usr/share/doc/LICENSE.txt", b"notice", None)],
    )
    names_before = {path.name for path in tmp_path.iterdir()}
    app_modules_before = {
        module_name
        for module_name in sys.modules
        if module_name == "iarf_assistant" or module_name.startswith("iarf_assistant.")
    }
    bundle = _invoke(archive)
    names_after = {path.name for path in tmp_path.iterdir()}
    app_modules_after = {
        module_name
        for module_name in sys.modules
        if module_name == "iarf_assistant" or module_name.startswith("iarf_assistant.")
    }

    assert names_after == names_before
    assert _by_path(bundle, "usr/share/doc/LICENSE.txt").content == b"notice"
    assert app_modules_after == app_modules_before
