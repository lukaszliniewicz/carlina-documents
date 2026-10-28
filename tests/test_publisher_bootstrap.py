from __future__ import annotations

import copy
import gzip
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from carlina.core import publisher_bootstrap
from carlina.core.publisher_bootstrap import (
    BOOTSTRAP_SKELETON_FILENAME,
    PublisherBootstrapError,
    parse_publisher_bootstrap_plan,
    publisher_bootstrap_plan_bytes,
    reconstruct_publisher_archive,
)


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _unpacker(platform: str) -> dict[str, object]:
    if platform == "linux-64":
        name = "pixi-unpack-x86_64-unknown-linux-musl"
        size = 15668176
        digest = "1e610ea67baae58119de8aa52cf238f6efea7e0c68ea3a8a295c8a2630a5df04"
    else:
        name = "pixi-unpack-x86_64-pc-windows-msvc.exe"
        size = 15029248
        digest = "f0409bf7ba71ade130bc2f74aca2c7a923af207f7cfa40bad82b3bff2d20bd8b"
    return {
        "name": name,
        "url": f"https://github.com/Quantco/pixi-pack/releases/download/v0.7.10/{name}",
        "size": size,
        "sha256": digest,
    }


def _fixture(
    *,
    compressed_skeleton: bytes | None = None,
    archive_sha256: str | None = None,
) -> tuple[dict[str, Any], bytes, bytes, dict[str, bytes]]:
    archive = bytearray(2048)
    wheel = b"synthetic-wheel"
    conda = b"synthetic-conda-package"
    wheel_offset = 512
    conda_offset = 1024
    archive[wheel_offset : wheel_offset + len(wheel)] = wheel
    archive[conda_offset : conda_offset + len(conda)] = conda
    if compressed_skeleton is None:
        compressed_skeleton = gzip.compress(bytes(2048), mtime=0)
    wheel_hash = _sha256(wheel)
    conda_hash = _sha256(conda)
    plan: dict[str, Any] = {
        "format": "carlina.publisher-bootstrap.plan",
        "schema_version": 1,
        "platform": "linux-64",
        "application_profile": "documents",
        "version": "1.2.3",
        "archive": {
            "size": len(archive),
            "sha256": archive_sha256 or _sha256(bytes(archive)),
        },
        "skeleton": {
            "filename": BOOTSTRAP_SKELETON_FILENAME,
            "size": len(compressed_skeleton),
            "sha256": _sha256(compressed_skeleton),
            "uncompressed_size": len(archive),
        },
        "downloads": [
            {
                "member_name": "pypi/demo-1.2.3-py3-none-any.whl",
                "offset_data": wheel_offset,
                "size": len(wheel),
                "sha256": wheel_hash,
                "url": (
                    "https://files.pythonhosted.org/packages/ab/cd/"
                    "demo-1.2.3-py3-none-any.whl"
                ),
                "component_ref": "pypi:demo=1.2.3",
            },
            {
                "member_name": "channel/linux-64/libdemo-1.2.3-0.conda",
                "offset_data": conda_offset,
                "size": len(conda),
                "sha256": conda_hash,
                "url": (
                    "https://conda.anaconda.org/conda-forge/linux-64/"
                    "libdemo-1.2.3-0.conda"
                ),
                "component_ref": "conda-forge::libdemo=1.2.3",
            },
        ],
        "unpacker": _unpacker("linux-64"),
    }
    return (
        plan,
        bytes(archive),
        compressed_skeleton,
        {
            wheel_hash: wheel,
            conda_hash: conda,
        },
    )


def _write_inputs(
    tmp_path: Path,
    skeleton_bytes: bytes,
    package_bytes: dict[str, bytes],
) -> tuple[Path, dict[str, Path]]:
    skeleton_path = tmp_path / "skeleton.tar.gz"
    skeleton_path.write_bytes(skeleton_bytes)
    package_paths: dict[str, Path] = {}
    for digest, package in package_bytes.items():
        package_path = tmp_path / f"{digest}.pkg"
        package_path.write_bytes(package)
        package_paths[digest] = package_path
    return skeleton_path, package_paths


def _reconstruct(
    tmp_path: Path,
    plan_payload: dict[str, Any],
    skeleton_bytes: bytes,
    package_bytes: dict[str, bytes],
    *,
    destination_name: str = "pixi.tar",
) -> Path:
    plan = parse_publisher_bootstrap_plan(plan_payload)
    skeleton_path, package_paths = _write_inputs(
        tmp_path,
        skeleton_bytes,
        package_bytes,
    )
    return reconstruct_publisher_archive(
        plan,
        skeleton_path,
        package_paths,
        tmp_path / destination_name,
    )


def test_parse_and_canonical_serialization_round_trip() -> None:
    payload, _, _, _ = _fixture()

    plan = parse_publisher_bootstrap_plan(payload)
    encoded = publisher_bootstrap_plan_bytes(plan)

    assert encoded.endswith(b"\n")
    assert encoded == publisher_bootstrap_plan_bytes(
        parse_publisher_bootstrap_plan(encoded)
    )
    assert parse_publisher_bootstrap_plan(encoded.decode("utf-8")) == plan
    assert json.loads(encoded) == payload


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda value: value.update(extra=True), "unknown or missing fields"),
        (lambda value: value.pop("version"), "unknown or missing fields"),
        (lambda value: value.update(schema_version=True), "integer"),
        (lambda value: value.update(platform="osx-64"), "platform"),
        (lambda value: value.update(application_profile="workbench"), "profile"),
        (lambda value: value.update(version="01.2.3"), "canonical PEP 440"),
        (
            lambda value: value["archive"].update(sha256="A" * 64),
            "lowercase SHA-256",
        ),
        (lambda value: value["archive"].update(size=True), "integer"),
        (lambda value: value["skeleton"].update(filename="other.tar.gz"), "filename"),
        (
            lambda value: value["downloads"][0].update(member_name="../demo.whl"),
            "member_name",
        ),
        (
            lambda value: value["downloads"][0].update(member_name="pypi/../demo.whl"),
            "path segment",
        ),
        (
            lambda value: value["downloads"][0].update(
                member_name="channel/osx-64/libdemo-1.2.3-0.conda"
            ),
            "supported package paths",
        ),
        (
            lambda value: value["downloads"][0].update(offset_data=True),
            "integer",
        ),
        (
            lambda value: value["downloads"][0].update(offset_data=513),
            "512-byte boundary",
        ),
        (
            lambda value: value["downloads"][0].update(component_ref="line\nbreak"),
            "component_ref",
        ),
        (
            lambda value: value["downloads"][0].update(component_ref="x" * 513),
            "component_ref",
        ),
        (
            lambda value: value["unpacker"].update(size=1),
            "pinned platform policy",
        ),
    ],
)
def test_parser_rejects_closed_schema_and_scalar_violations(
    mutate,
    message: str,
) -> None:
    payload, _, _, _ = _fixture()
    changed = copy.deepcopy(payload)
    mutate(changed)

    with pytest.raises(PublisherBootstrapError, match=message):
        parse_publisher_bootstrap_plan(changed)


def test_parser_rejects_unknown_nested_keys_and_non_array_downloads() -> None:
    payload, _, _, _ = _fixture()
    changed = copy.deepcopy(payload)
    changed["skeleton"]["extra"] = "closed"  # type: ignore[index]
    with pytest.raises(PublisherBootstrapError, match="unknown or missing fields"):
        parse_publisher_bootstrap_plan(changed)

    changed = copy.deepcopy(payload)
    changed["downloads"] = tuple(changed["downloads"])
    with pytest.raises(PublisherBootstrapError, match="JSON array"):
        parse_publisher_bootstrap_plan(changed)


@pytest.mark.parametrize("nested", [False, True])
def test_parser_rejects_duplicate_json_keys_at_any_depth(nested: bool) -> None:
    payload, _, _, _ = _fixture()
    encoded = json.dumps(payload)
    if nested:
        encoded = encoded.replace(
            '"filename": "skeleton.tar.gz"',
            '"filename": "skeleton.tar.gz", "filename": "skeleton.tar.gz"',
            1,
        )
    else:
        encoded = encoded.replace(
            '"schema_version": 1',
            '"schema_version": 1, "schema_version": 1',
            1,
        )

    with pytest.raises(PublisherBootstrapError, match="duplicate object keys"):
        parse_publisher_bootstrap_plan(encoded)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("size", 4 * 1024 * 1024 * 1024 + 512, "archive.size"),
        ("size", 2049, "archive.size"),
    ],
)
def test_parser_rejects_archive_size_limits(
    field: str, value: int, message: str
) -> None:
    payload, _, _, _ = _fixture()
    payload["archive"][field] = value  # type: ignore[index]

    with pytest.raises(PublisherBootstrapError, match=message):
        parse_publisher_bootstrap_plan(payload)


def test_parser_rejects_download_count_limit() -> None:
    payload, _, _, _ = _fixture()
    payload["downloads"] = payload["downloads"] * 257  # type: ignore[operator]

    with pytest.raises(PublisherBootstrapError, match="downloads count"):
        parse_publisher_bootstrap_plan(payload)


def test_parser_rejects_skeleton_and_package_size_limits() -> None:
    payload, _, _, _ = _fixture()
    payload["skeleton"]["size"] = 512 * 1024 * 1024 + 1
    with pytest.raises(PublisherBootstrapError, match=r"skeleton\.size"):
        parse_publisher_bootstrap_plan(payload)

    payload, _, _, _ = _fixture()
    payload["downloads"][0]["size"] = 1024 * 1024 * 1024 + 1
    with pytest.raises(PublisherBootstrapError, match="size is outside"):
        parse_publisher_bootstrap_plan(payload)


def test_parser_rejects_encoded_json_limit() -> None:
    with pytest.raises(PublisherBootstrapError, match="1 MiB"):
        parse_publisher_bootstrap_plan(b" " * (1024 * 1024 + 1))


@pytest.mark.parametrize("problem", ["overlap", "unordered", "past-end"])
def test_parser_rejects_overlapping_unordered_or_out_of_bounds_ranges(
    problem: str,
) -> None:
    payload, _, _, _ = _fixture()
    if problem == "overlap":
        payload["downloads"][1]["offset_data"] = 512  # type: ignore[index]
        message = "overlap"
    elif problem == "unordered":
        payload["downloads"][0]["offset_data"] = 1024  # type: ignore[index]
        payload["downloads"][1]["offset_data"] = 512  # type: ignore[index]
        message = "ordered"
    else:
        payload["downloads"][1]["size"] = 1025  # type: ignore[index]
        message = "archive.size"

    with pytest.raises(PublisherBootstrapError, match=message):
        parse_publisher_bootstrap_plan(payload)


@pytest.mark.parametrize(
    "url",
    [
        "http://files.pythonhosted.org/packages/demo-1.2.3-py3-none-any.whl",
        "https://evil.example/packages/demo-1.2.3-py3-none-any.whl",
        "https://user@files.pythonhosted.org/packages/demo-1.2.3-py3-none-any.whl",
        "https://files.pythonhosted.org:443/packages/demo-1.2.3-py3-none-any.whl",
        "https://files.pythonhosted.org/packages/../demo-1.2.3-py3-none-any.whl",
        "https://files.pythonhosted.org/packages/%2e%2e/demo-1.2.3-py3-none-any.whl",
        "https://files.pythonhosted.org/packages/%252e%252e/demo-1.2.3-py3-none-any.whl",
        "https://files.pythonhosted.org/packages/%2f/demo-1.2.3-py3-none-any.whl",
        "https://files.pythonhosted.org/packages/demo-1.2.3-py3-none-any.whl?token=x",
        "https://files.pythonhosted.org/packages/demo-1.2.3-py3-none-any.whl#fragment",
        "https://files.pythonhosted.org/packages/demo-1.2.3-py3-none-any.whl\\x",
        "https://files.pythonhosted.org/packages/other.whl",
    ],
)
def test_parser_rejects_untrusted_or_mismatched_urls(url: str) -> None:
    payload, _, _, _ = _fixture()
    payload["downloads"][0]["url"] = url  # type: ignore[index]

    with pytest.raises(PublisherBootstrapError):
        parse_publisher_bootstrap_plan(payload)


def test_parser_rejects_conda_url_subdirectory_mismatch() -> None:
    payload, _, _, _ = _fixture()
    payload["downloads"][1]["url"] = (
        "https://conda.anaconda.org/conda-forge/noarch/libdemo-1.2.3-0.conda"  # type: ignore[index]
    )

    with pytest.raises(PublisherBootstrapError, match="subdirectory"):
        parse_publisher_bootstrap_plan(payload)


def test_parser_accepts_noarch_conda_member_and_matching_url() -> None:
    payload, _, _, _ = _fixture()
    payload["downloads"][1]["member_name"] = "channel/noarch/libdemo-1.2.3-0.conda"
    payload["downloads"][1]["url"] = (
        "https://conda.anaconda.org/conda-forge/noarch/libdemo-1.2.3-0.conda"
    )

    plan = parse_publisher_bootstrap_plan(payload)

    assert plan.downloads[1].member_name.startswith("channel/noarch/")


def test_parser_rejects_casefold_duplicate_member_and_duplicate_component_ref() -> None:
    payload, _, _, _ = _fixture()
    payload["downloads"][1]["member_name"] = "pypi/DEMO-1.2.3-py3-none-any.whl"  # type: ignore[index]
    payload["downloads"][1]["url"] = (
        "https://files.pythonhosted.org/packages/ab/cd/DEMO-1.2.3-py3-none-any.whl"  # type: ignore[index]
    )
    with pytest.raises(PublisherBootstrapError, match="case-insensitively"):
        parse_publisher_bootstrap_plan(payload)

    payload, _, _, _ = _fixture()
    payload["downloads"][1]["component_ref"] = "pypi:demo=1.2.3"  # type: ignore[index]
    with pytest.raises(PublisherBootstrapError, match="component_ref values"):
        parse_publisher_bootstrap_plan(payload)


def test_fixed_unpacker_policy_accepts_both_supported_platforms() -> None:
    payload, _, _, _ = _fixture()
    assert parse_publisher_bootstrap_plan(payload).unpacker.name == (
        "pixi-unpack-x86_64-unknown-linux-musl"
    )

    payload["platform"] = "win-64"
    payload["unpacker"] = _unpacker("win-64")
    payload["downloads"][1]["member_name"] = "channel/win-64/libdemo-1.2.3-0.conda"  # type: ignore[index]
    payload["downloads"][1]["url"] = (
        "https://conda.anaconda.org/conda-forge/win-64/libdemo-1.2.3-0.conda"  # type: ignore[index]
    )
    assert parse_publisher_bootstrap_plan(payload).unpacker.name == (
        "pixi-unpack-x86_64-pc-windows-msvc.exe"
    )


def test_plan_dataclass_construction_is_revalidated_on_serialization() -> None:
    payload, _, _, _ = _fixture()
    plan = parse_publisher_bootstrap_plan(payload)
    invalid = type(plan)(
        platform=plan.platform,
        application_profile=plan.application_profile,
        version=plan.version,
        archive=plan.archive,
        skeleton=plan.skeleton,
        downloads=plan.downloads,
        unpacker=type(plan.unpacker)(
            name=plan.unpacker.name,
            url=plan.unpacker.url,
            size=plan.unpacker.size + 1,
            sha256=plan.unpacker.sha256,
        ),
    )

    with pytest.raises(PublisherBootstrapError, match="pinned platform policy"):
        publisher_bootstrap_plan_bytes(invalid)


def test_reconstructs_exact_two_range_archive_offline(tmp_path: Path) -> None:
    payload, expected_archive, compressed_skeleton, package_bytes = _fixture()

    destination = _reconstruct(tmp_path, payload, compressed_skeleton, package_bytes)

    assert destination.read_bytes() == expected_archive
    assert _sha256(destination.read_bytes()) == payload["archive"]["sha256"]  # type: ignore[index]
    assert list(tmp_path.glob(".carlina-publisher-bootstrap-*.tmp")) == []


@pytest.mark.parametrize("mismatch", ["size", "hash"])
def test_reconstruct_rejects_skeleton_size_or_hash_mismatch(
    tmp_path: Path,
    mismatch: str,
) -> None:
    payload, _, compressed_skeleton, package_bytes = _fixture()
    if mismatch == "size":
        skeleton_bytes = compressed_skeleton + b"x"
    else:
        skeleton_bytes = bytearray(compressed_skeleton)
        skeleton_bytes[-1] ^= 1
        skeleton_bytes = bytes(skeleton_bytes)
    skeleton_path, package_paths = _write_inputs(
        tmp_path,
        skeleton_bytes,
        package_bytes,
    )

    with pytest.raises(PublisherBootstrapError, match="skeleton input"):
        reconstruct_publisher_archive(
            parse_publisher_bootstrap_plan(payload),
            skeleton_path,
            package_paths,
            tmp_path / "result.tar",
        )


@pytest.mark.parametrize("problem", ["corrupt", "truncated", "overlong"])
def test_reconstruct_rejects_corrupt_truncated_or_overlong_gzip(
    tmp_path: Path,
    problem: str,
) -> None:
    payload, _, compressed_skeleton, package_bytes = _fixture()
    if problem == "corrupt":
        candidate = bytearray(compressed_skeleton)
        candidate[-1] ^= 0x80
        compressed_skeleton = bytes(candidate)
    elif problem == "truncated":
        compressed_skeleton = compressed_skeleton[:-3]
    else:
        compressed_skeleton = gzip.compress(b"x" * 2049, mtime=0)
    payload["skeleton"]["size"] = len(compressed_skeleton)  # type: ignore[index]
    payload["skeleton"]["sha256"] = _sha256(compressed_skeleton)  # type: ignore[index]
    skeleton_path, package_paths = _write_inputs(
        tmp_path,
        compressed_skeleton,
        package_bytes,
    )
    destination = tmp_path / "result.tar"

    with pytest.raises(PublisherBootstrapError):
        reconstruct_publisher_archive(
            parse_publisher_bootstrap_plan(payload),
            skeleton_path,
            package_paths,
            destination,
        )
    assert not destination.exists()
    assert list(tmp_path.glob(".carlina-publisher-bootstrap-*.tmp")) == []


def test_reconstruct_rejects_nonzero_skeleton_placeholder(tmp_path: Path) -> None:
    payload, _, _, package_bytes = _fixture()
    skeleton = bytearray(2048)
    skeleton[512] = 1
    compressed_skeleton = gzip.compress(bytes(skeleton), mtime=0)
    payload["skeleton"]["size"] = len(compressed_skeleton)  # type: ignore[index]
    payload["skeleton"]["sha256"] = _sha256(compressed_skeleton)  # type: ignore[index]
    skeleton_path, package_paths = _write_inputs(
        tmp_path,
        compressed_skeleton,
        package_bytes,
    )

    with pytest.raises(PublisherBootstrapError, match="nonzero placeholder"):
        reconstruct_publisher_archive(
            parse_publisher_bootstrap_plan(payload),
            skeleton_path,
            package_paths,
            tmp_path / "result.tar",
        )


@pytest.mark.parametrize("mismatch", ["size", "hash"])
def test_reconstruct_rejects_package_size_or_hash_mismatch(
    tmp_path: Path,
    mismatch: str,
) -> None:
    payload, _, compressed_skeleton, package_bytes = _fixture()
    first_hash = next(iter(package_bytes))
    if mismatch == "size":
        package_bytes[first_hash] = package_bytes[first_hash][:-1]
    else:
        package_bytes[first_hash] = b"x" * len(package_bytes[first_hash])
    skeleton_path, package_paths = _write_inputs(
        tmp_path,
        compressed_skeleton,
        package_bytes,
    )
    destination = tmp_path / "result.tar"

    with pytest.raises(PublisherBootstrapError, match="package input"):
        reconstruct_publisher_archive(
            parse_publisher_bootstrap_plan(payload),
            skeleton_path,
            package_paths,
            destination,
        )
    assert not destination.exists()
    assert list(tmp_path.glob(".carlina-publisher-bootstrap-*.tmp")) == []


def test_reconstruct_rejects_wrong_final_archive_hash(tmp_path: Path) -> None:
    payload, _, compressed_skeleton, package_bytes = _fixture(
        archive_sha256="0" * 64,
    )

    with pytest.raises(PublisherBootstrapError, match="archive SHA-256"):
        _reconstruct(tmp_path, payload, compressed_skeleton, package_bytes)

    assert not (tmp_path / "pixi.tar").exists()
    assert list(tmp_path.glob(".carlina-publisher-bootstrap-*.tmp")) == []


@pytest.mark.parametrize("mapping_kind", ["missing", "extra"])
def test_reconstruct_requires_exact_package_hash_mapping(
    tmp_path: Path,
    mapping_kind: str,
) -> None:
    payload, _, compressed_skeleton, package_bytes = _fixture()
    plan = parse_publisher_bootstrap_plan(payload)
    skeleton_path, package_paths = _write_inputs(
        tmp_path,
        compressed_skeleton,
        package_bytes,
    )
    if mapping_kind == "missing":
        package_paths.pop(next(iter(package_paths)))
    else:
        package_paths["f" * 64] = tmp_path / "extra.pkg"

    with pytest.raises(PublisherBootstrapError, match="exactly match"):
        reconstruct_publisher_archive(
            plan,
            skeleton_path,
            package_paths,
            tmp_path / "result.tar",
        )


def test_reconstruct_preserves_existing_destination(tmp_path: Path) -> None:
    payload, _, compressed_skeleton, package_bytes = _fixture()
    destination = tmp_path / "existing.tar"
    destination.write_bytes(b"keep-me")
    skeleton_path, package_paths = _write_inputs(
        tmp_path,
        compressed_skeleton,
        package_bytes,
    )

    with pytest.raises(PublisherBootstrapError, match="already exists"):
        reconstruct_publisher_archive(
            parse_publisher_bootstrap_plan(payload),
            skeleton_path,
            package_paths,
            destination,
        )

    assert destination.read_bytes() == b"keep-me"


def test_reconstruct_preserves_destination_created_before_atomic_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload, _, compressed_skeleton, package_bytes = _fixture()
    skeleton_path, package_paths = _write_inputs(
        tmp_path,
        compressed_skeleton,
        package_bytes,
    )
    destination = tmp_path / "raced.tar"
    real_link = publisher_bootstrap.os.link

    def create_destination_then_link(source: Path, target: Path) -> None:
        target.write_bytes(b"created-by-another-writer")
        real_link(source, target)

    monkeypatch.setattr(publisher_bootstrap.os, "link", create_destination_then_link)

    with pytest.raises(PublisherBootstrapError, match="appeared during reconstruction"):
        reconstruct_publisher_archive(
            parse_publisher_bootstrap_plan(payload),
            skeleton_path,
            package_paths,
            destination,
        )

    assert destination.read_bytes() == b"created-by-another-writer"
    assert list(tmp_path.glob(".carlina-publisher-bootstrap-*.tmp")) == []


@pytest.mark.parametrize("input_kind", ["skeleton", "package"])
def test_reconstruct_rejects_symlink_inputs_and_cleans_temporary_file(
    tmp_path: Path,
    input_kind: str,
) -> None:
    payload, _, compressed_skeleton, package_bytes = _fixture()
    skeleton_path, package_paths = _write_inputs(
        tmp_path,
        compressed_skeleton,
        package_bytes,
    )
    if input_kind == "skeleton":
        real_path = tmp_path / "real-skeleton"
        skeleton_path.rename(real_path)
        skeleton_path.symlink_to(real_path)
    else:
        digest = next(iter(package_paths))
        real_path = tmp_path / "real-package"
        package_paths[digest].rename(real_path)
        package_paths[digest].symlink_to(real_path)

    with pytest.raises(PublisherBootstrapError, match="regular non-reparse"):
        reconstruct_publisher_archive(
            parse_publisher_bootstrap_plan(payload),
            skeleton_path,
            package_paths,
            tmp_path / "result.tar",
        )

    assert not (tmp_path / "result.tar").exists()
    assert list(tmp_path.glob(".carlina-publisher-bootstrap-*.tmp")) == []
