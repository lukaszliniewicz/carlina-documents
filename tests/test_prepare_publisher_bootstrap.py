from __future__ import annotations

import base64
import gzip
import hashlib
import io
import json
import subprocess
import sys
import tarfile
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import carlina.core.publisher_bootstrap as bootstrap_core
import scripts.prepare_publisher_bootstrap as producer
from iarf_assistant.core.managed_bundle import (
    WINDOWS_BUNDLE_PAYLOADS,
    WindowsBundleMember,
    windows_bundle_manifest_bytes,
)

_VERSION = "1.2.3"
_REVISION = "a" * 40


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _component(
    *,
    name: str,
    payload: bytes,
    url: str,
    subdir: str | None = None,
    ref: str | None = None,
) -> dict[str, object]:
    properties: list[dict[str, str]] = [
        {"name": "carlina:environment", "value": "documents"},
        {
            "name": "carlina:allowed-artifact-sha256",
            "value": json.dumps([_sha256(payload)]),
        },
    ]
    if subdir is not None:
        properties.extend(
            [
                {"name": "carlina:subdir", "value": subdir},
                {"name": "carlina:build", "value": "fixture_0"},
            ]
        )
    return {
        "bom-ref": ref or f"documents:{_sha256(payload)}:{name}",
        "externalReferences": [{"type": "distribution", "url": url}],
        "hashes": [{"alg": "SHA-256", "content": _sha256(payload)}],
        "licenses": [],
        "name": name,
        "properties": properties,
        "purl": f"pkg:fixture/{name}",
        "type": "library",
        "version": "1.0",
    }


def _tar_bytes(
    platform: str,
    *,
    include_companion: bool = True,
    extra_members: tuple[tuple[str, bytes, int], ...] = (),
    missing_metadata: str | None = None,
    duplicate_member: str | None = None,
    tar_format: int = tarfile.USTAR_FORMAT,
    helper_basename: str = "helper-fixture-1.0-py3-none-any.whl",
) -> tuple[bytes, dict[str, bytes], list[dict[str, object]]]:
    own_wheel = f"carlina-{_VERSION}-py3-none-any.whl"
    companion_wheel = f"carlina_application_companions-{_VERSION}-py3-none-any.whl"
    payloads = {
        "pypi/" + own_wheel: b"owned application wheel bytes",
        f"channel/{platform}/libfixture-1.0-hfixture_0.conda": b"platform package bytes",
        "channel/noarch/shared-fixture-1.0-hfixture_0.tar.bz2": b"noarch package bytes",
        "pypi/" + helper_basename: b"third-party wheel bytes",
        "channel/noarch/repodata.json": b'{"packages":{}}',
        f"channel/{platform}/repodata.json": b'{"packages":{}}',
        "environment.yml": b"name: fixture\n",
        "pixi-pack.json": b'{"environment":"documents"}',
    }
    if include_companion:
        payloads["pypi/" + companion_wheel] = b"owned companion wheel bytes"
    if missing_metadata is not None:
        payloads.pop(missing_metadata)

    components = [
        _component(
            name="libfixture",
            payload=payloads[f"channel/{platform}/libfixture-1.0-hfixture_0.conda"],
            url=(
                "https://conda.anaconda.org/conda-forge/"
                f"{platform}/libfixture-1.0-hfixture_0.conda"
            ),
            subdir=platform,
        ),
        _component(
            name="shared-fixture",
            payload=payloads["channel/noarch/shared-fixture-1.0-hfixture_0.tar.bz2"],
            url=(
                "https://conda.anaconda.org/conda-forge/noarch/"
                "shared-fixture-1.0-hfixture_0.tar.bz2"
            ),
            subdir="noarch",
        ),
        _component(
            name="helper-fixture",
            payload=payloads["pypi/" + helper_basename],
            url=(
                "https://files.pythonhosted.org/packages/00/fixture/" + helper_basename
            ),
        ),
    ]

    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w", format=tar_format) as archive:
        for directory in (
            "channel/",
            f"channel/{platform}/",
            "channel/noarch/",
            "pypi/",
        ):
            member = tarfile.TarInfo(directory)
            member.type = tarfile.DIRTYPE
            member.mode = 0o755
            archive.addfile(member)
        entries = list(payloads.items())
        if duplicate_member is not None and duplicate_member in payloads:
            entries.append((duplicate_member, payloads[duplicate_member]))
        for name, payload in entries:
            member = tarfile.TarInfo(name)
            member.size = len(payload)
            member.mode = 0o644
            archive.addfile(member, io.BytesIO(payload))
        for name, payload, typeflag in extra_members:
            member = tarfile.TarInfo(name)
            member.type = typeflag
            if typeflag in {tarfile.REGTYPE, tarfile.AREGTYPE}:
                member.size = len(payload)
                archive.addfile(member, io.BytesIO(payload))
            else:
                member.linkname = "pypi/target.whl"
                archive.addfile(member)
    package_files = {
        _sha256(payload): payload
        for name, payload in payloads.items()
        if name.startswith(("channel/", "pypi/"))
        and name not in {"pypi/" + own_wheel, "pypi/" + companion_wheel}
    }
    return stream.getvalue(), package_files, components


def _inventory(platform: str, components: list[dict[str, object]]) -> dict[str, object]:
    return {
        "format": "carlina.documents-release-inventory",
        "schema_version": 1,
        "application_profile": "documents",
        "source_revision": _REVISION,
        "application_version": _VERSION,
        "wheel_sha256": _sha256(b"owned application wheel bytes"),
        "application_source": {},
        "platform": platform,
        "basis": "resolved locks and packaged asset descriptors",
        "frontend_scope": "fixture",
        "notice_collection_complete": False,
        "public_release_accepted": False,
        "excluded_optional_runtime": [],
        "source_locks": [],
        "source_inputs": [],
        "reconciled_local_products": [],
        "components": components,
        "required_follow_up": [],
    }


def _patch_unpacker(
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    binary: bytes,
) -> producer._PinnedUnpacker:
    original = producer._PINNED_UNPACKERS[platform]
    pinned = producer._PinnedUnpacker(
        name=original.name,
        url=original.url,
        size=len(binary),
        sha256=_sha256(binary),
    )
    monkeypatch.setitem(producer._PINNED_UNPACKERS, platform, pinned)
    monkeypatch.setitem(
        bootstrap_core._UNPACKER_POLICY,
        platform,
        bootstrap_core.PublisherUnpacker(
            name=pinned.name,
            url=pinned.url,
            size=pinned.size,
            sha256=pinned.sha256,
        ),
    )
    return pinned


def _make_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    platform: str = "linux-64",
    include_companion: bool = True,
    extra_members: tuple[tuple[str, bytes, int], ...] = (),
    missing_metadata: str | None = None,
    duplicate_member: str | None = None,
    unpacker_bytes: bytes = b"synthetic pinned unpacker",
    inventory_mutator: Callable[[dict[str, object]], None] | None = None,
    archive_mutator: Callable[[bytes], bytes] | None = None,
    tar_format: int = tarfile.USTAR_FORMAT,
    helper_basename: str = "helper-fixture-1.0-py3-none-any.whl",
) -> tuple[Path, Path, Path, bytes, dict[str, bytes], producer._PinnedUnpacker]:
    archive, package_bytes, components = _tar_bytes(
        platform,
        include_companion=include_companion,
        extra_members=extra_members,
        missing_metadata=missing_metadata,
        duplicate_member=duplicate_member,
        tar_format=tar_format,
        helper_basename=helper_basename,
    )
    if archive_mutator is not None:
        archive = archive_mutator(archive)
    pinned = _patch_unpacker(monkeypatch, platform, unpacker_bytes)
    pack_name = f"carlina-documents-{_VERSION}-{platform}"
    pack_path = tmp_path / (pack_name + (".sh" if platform == "linux-64" else ".zip"))
    metadata_path = Path(f"{pack_path}.artifact.json")
    inventory_path = tmp_path / "inventory.json"
    output_dir = tmp_path / "prepared"

    if platform == "linux-64":
        pack_payload = (
            b"#!/bin/sh\n# synthetic fixture\n@@END_HEADER@@\n"
            + base64.b64encode(archive)
            + b"\n@@END_ARCHIVE@@\n"
            + base64.b64encode(unpacker_bytes)
            + b"\n"
        )
        pack_path.write_bytes(pack_payload)
    else:
        manifest = windows_bundle_manifest_bytes(
            {
                "archive.tar": WindowsBundleMember(_sha256(archive), len(archive)),
                "pixi-unpack.exe": WindowsBundleMember(
                    _sha256(unpacker_bytes), len(unpacker_bytes)
                ),
            }
        )
        with zipfile.ZipFile(pack_path, "w", compression=zipfile.ZIP_STORED) as bundle:
            bundle.writestr("archive.tar", archive)
            bundle.writestr("pixi-unpack.exe", unpacker_bytes)
            bundle.writestr("bundle.json", manifest)
        assert set(WINDOWS_BUNDLE_PAYLOADS) == {"archive.tar", "pixi-unpack.exe"}

    own_wheel = b"owned application wheel bytes"
    companion_wheel = b"owned companion wheel bytes"
    metadata: dict[str, object] = {
        "format": "carlina.managed-artifact",
        "schema_version": 1,
        "version": _VERSION,
        "platform": platform,
        "kind": "pixi-pack-executable"
        if platform == "linux-64"
        else "pixi-pack-bundle",
        "filename": pack_path.name,
        "sha256": _sha256(pack_path.read_bytes()),
        "size": pack_path.stat().st_size,
        "wheel": {
            "filename": f"carlina-{_VERSION}-py3-none-any.whl",
            "sha256": _sha256(own_wheel),
            "size": len(own_wheel),
        },
        "pixi_pack_version": "0.7.10",
        "pixi_lock_sha256": "b" * 64,
        "built_at": "2026-10-07T00:00:00Z",
        "source_revision": _REVISION,
        "application_profile": "documents",
    }
    if include_companion:
        metadata["companion_wheel"] = {
            "filename": f"carlina_application_companions-{_VERSION}-py3-none-any.whl",
            "sha256": _sha256(companion_wheel),
            "size": len(companion_wheel),
        }
    inventory = _inventory(platform, components)
    if inventory_mutator is not None:
        inventory_mutator(inventory)
    metadata_path.write_text(json.dumps(metadata, sort_keys=True), encoding="utf-8")
    inventory_path.write_text(json.dumps(inventory, sort_keys=True), encoding="utf-8")
    return metadata_path, inventory_path, output_dir, archive, package_bytes, pinned


@pytest.mark.parametrize("platform", ["linux-64", "win-64"])
@pytest.mark.parametrize("include_companion", [True, False])
def test_prepare_round_trips_synthetic_platform_archive_and_retains_nonpackage_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    include_companion: bool,
):
    metadata, inventory, output_dir, original_tar, package_payloads, _pin = (
        _make_fixture(
            tmp_path,
            monkeypatch,
            platform=platform,
            include_companion=include_companion,
        )
    )

    plan_path, skeleton_path, preparation_path = producer.prepare_publisher_bootstrap(
        metadata,
        inventory,
        output_dir,
    )
    plan = bootstrap_core.parse_publisher_bootstrap_plan(plan_path.read_bytes())
    skeleton_tar = gzip.decompress(skeleton_path.read_bytes())
    expected_skeleton = bytearray(original_tar)
    for package in plan.downloads:
        assert package.member_name in {
            f"channel/{platform}/libfixture-1.0-hfixture_0.conda",
            "channel/noarch/shared-fixture-1.0-hfixture_0.tar.bz2",
            "pypi/helper-fixture-1.0-py3-none-any.whl",
        }
        assert skeleton_tar[
            package.offset_data : package.offset_data + package.size
        ] == bytes(package.size)
        expected_skeleton[package.offset_data : package.offset_data + package.size] = (
            bytes(package.size)
        )
    assert skeleton_tar == expected_skeleton

    package_files: dict[str, Path] = {}
    download_hashes = {package.sha256 for package in plan.downloads}
    for digest, payload in package_payloads.items():
        if digest not in download_hashes:
            continue
        package_path = tmp_path / f"package-{digest}"
        package_path.write_bytes(payload)
        package_files[digest] = package_path
    reconstructed = bootstrap_core.reconstruct_publisher_archive(
        plan,
        skeleton_path,
        package_files,
        tmp_path / "reconstructed.tar",
    )
    assert reconstructed.read_bytes() == original_tar

    prep = json.loads(preparation_path.read_bytes())
    assert prep["application"]["source_revision"] == _REVISION
    assert prep["original_archive"]["sha256"] == _sha256(original_tar)
    assert prep["removed_publisher_payload_bytes"] == sum(
        package.size for package in plan.downloads
    )
    assert (
        prep["inputs"]["source_pack"]["sha256"]
        == json.loads(metadata.read_bytes())["sha256"]
    )
    assert prep["outputs"]["skeleton"]["sha256"] == _sha256(skeleton_path.read_bytes())
    classifications = {item["classification"] for item in prep["members"]}
    expected_classifications = {
        "metadata",
        "application_wheel",
        f"publisher_conda_{platform}",
        "publisher_conda_noarch",
        "publisher_pypi",
    }
    if include_companion:
        expected_classifications.add("companion_wheel")
    assert classifications >= expected_classifications
    expected_application_fields = {
        "application_profile",
        "version",
        "platform",
        "source_revision",
        "wheel",
    }
    if include_companion:
        expected_application_fields.add("companion_wheel")
    assert set(prep["application"]) == expected_application_fields
    assert (
        sum(item["classification"] == "application_wheel" for item in prep["members"])
        == 1
    )
    assert sum(
        item["classification"] == "companion_wheel" for item in prep["members"]
    ) == int(include_companion)
    assert {item.url for item in plan.downloads} == {
        (
            "https://conda.anaconda.org/conda-forge/"
            f"{platform}/libfixture-1.0-hfixture_0.conda"
        ),
        "https://conda.anaconda.org/conda-forge/noarch/shared-fixture-1.0-hfixture_0.tar.bz2",
        "https://files.pythonhosted.org/packages/00/fixture/helper-fixture-1.0-py3-none-any.whl",
    }


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("null_companion", "companion wheel"),
        ("malformed_companion", "companion wheel"),
        ("unknown_field", "unknown or missing fields"),
        ("missing_application_wheel", "unknown or missing fields"),
    ],
)
def test_optional_companion_absence_does_not_weaken_artifact_metadata_shape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    message: str,
) -> None:
    metadata_path, inventory_path, output_dir, *_ = _make_fixture(
        tmp_path,
        monkeypatch,
        include_companion=False,
    )
    metadata = json.loads(metadata_path.read_bytes())
    if mutation == "null_companion":
        metadata["companion_wheel"] = None
    elif mutation == "malformed_companion":
        metadata["companion_wheel"] = {
            "filename": f"carlina_application_companions-{_VERSION}-py3-none-any.whl",
            "size": 1,
            "sha256": "invalid",
        }
    elif mutation == "unknown_field":
        metadata["unreviewed"] = True
    else:
        metadata.pop("wheel")
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    with pytest.raises(producer.PublisherBootstrapPreparationError, match=message):
        producer.prepare_publisher_bootstrap(metadata_path, inventory_path, output_dir)
    assert not output_dir.exists()


@pytest.mark.parametrize("mismatch", ["sha256", "size"])
def test_declared_companion_wheel_must_match_its_archive_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mismatch: str,
) -> None:
    metadata_path, inventory_path, output_dir, *_ = _make_fixture(tmp_path, monkeypatch)
    metadata = json.loads(metadata_path.read_bytes())
    if mismatch == "sha256":
        metadata["companion_wheel"]["sha256"] = "0" * 64
    else:
        metadata["companion_wheel"]["size"] += 1
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    with pytest.raises(
        producer.PublisherBootstrapPreparationError,
        match="owned wheel payload does not match",
    ):
        producer.prepare_publisher_bootstrap(metadata_path, inventory_path, output_dir)
    assert not output_dir.exists()


@pytest.mark.parametrize("mismatch", ["sha256", "size"])
def test_declared_application_wheel_must_match_its_archive_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mismatch: str,
) -> None:
    metadata_path, inventory_path, output_dir, *_ = _make_fixture(tmp_path, monkeypatch)
    metadata = json.loads(metadata_path.read_bytes())
    if mismatch == "sha256":
        metadata["wheel"]["sha256"] = "0" * 64
        inventory = json.loads(inventory_path.read_bytes())
        inventory["wheel_sha256"] = "0" * 64
        inventory_path.write_text(json.dumps(inventory), encoding="utf-8")
    else:
        metadata["wheel"]["size"] += 1
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    with pytest.raises(
        producer.PublisherBootstrapPreparationError,
        match="owned wheel payload does not match",
    ):
        producer.prepare_publisher_bootstrap(metadata_path, inventory_path, output_dir)
    assert not output_dir.exists()


def test_application_wheel_is_required_in_archive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metadata_path, inventory_path, output_dir, *_ = _make_fixture(
        tmp_path,
        monkeypatch,
        missing_metadata=f"pypi/carlina-{_VERSION}-py3-none-any.whl",
    )

    with pytest.raises(
        producer.PublisherBootstrapPreparationError,
        match="exactly its metadata-owned wheel",
    ):
        producer.prepare_publisher_bootstrap(metadata_path, inventory_path, output_dir)
    assert not output_dir.exists()


def test_undeclared_companion_wheel_in_archive_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    filename = f"carlina_application_companions-{_VERSION}-py3-none-any.whl"
    payload = b"unclaimed companion wheel bytes"

    def declare_extra_inventory_component(data: dict[str, object]) -> None:
        components = data["components"]
        assert isinstance(components, list)
        components.append(
            _component(
                name=filename.removesuffix(".whl"),
                payload=payload,
                url=("https://files.pythonhosted.org/packages/00/fixture/" + filename),
            )
        )

    metadata_path, inventory_path, output_dir, *_ = _make_fixture(
        tmp_path,
        monkeypatch,
        include_companion=False,
        extra_members=(("pypi/" + filename, payload, tarfile.REGTYPE),),
        inventory_mutator=declare_extra_inventory_component,
    )

    with pytest.raises(
        producer.PublisherBootstrapPreparationError,
        match="undeclared companion wheel",
    ):
        producer.prepare_publisher_bootstrap(metadata_path, inventory_path, output_dir)
    assert not output_dir.exists()


def test_skeleton_is_deterministic_for_identical_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    metadata, inventory, first_output, *_ = _make_fixture(tmp_path, monkeypatch)
    first_paths = producer.prepare_publisher_bootstrap(
        metadata, inventory, first_output
    )
    second_output = tmp_path / "prepared-again"
    second_paths = producer.prepare_publisher_bootstrap(
        metadata, inventory, second_output
    )
    assert first_paths[1].read_bytes() == second_paths[1].read_bytes()


def test_existing_output_directory_is_preserved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    metadata, inventory, output_dir, *_ = _make_fixture(tmp_path, monkeypatch)
    output_dir.mkdir()
    sentinel = output_dir / "keep.txt"
    sentinel.write_text("concurrent data", encoding="utf-8")

    with pytest.raises(
        producer.PublisherBootstrapPreparationError, match="already exists"
    ):
        producer.prepare_publisher_bootstrap(metadata, inventory, output_dir)

    assert sentinel.read_text(encoding="utf-8") == "concurrent data"


@pytest.mark.parametrize(
    ("platform", "mutation"),
    [
        ("linux-64", "pack_sha"),
        ("win-64", "kind"),
        ("linux-64", "profile"),
        ("linux-64", "version"),
        ("win-64", "platform"),
        ("linux-64", "wheel_sha"),
        ("linux-64", "revision"),
    ],
)
def test_metadata_and_inventory_identity_mismatches_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    mutation: str,
):
    metadata_path, inventory_path, output_dir, *_ = _make_fixture(
        tmp_path,
        monkeypatch,
        platform=platform,
    )
    if mutation in {"pack_sha", "kind"}:
        metadata = json.loads(metadata_path.read_bytes())
        if mutation == "pack_sha":
            metadata["sha256"] = "0" * 64
        else:
            metadata["kind"] = "pixi-pack-executable"
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    else:
        inventory = json.loads(inventory_path.read_bytes())
        fields = {
            "profile": "application_profile",
            "version": "application_version",
            "platform": "platform",
            "wheel_sha": "wheel_sha256",
            "revision": "source_revision",
        }
        field = fields[mutation]
        inventory[field] = "0" * (40 if mutation == "revision" else 64)
        inventory_path.write_text(json.dumps(inventory), encoding="utf-8")

    with pytest.raises(producer.PublisherBootstrapPreparationError):
        producer.prepare_publisher_bootstrap(metadata_path, inventory_path, output_dir)
    assert not output_dir.exists()


@pytest.mark.parametrize(
    "case",
    [
        "missing_metadata",
        "unknown_regular",
        "path_traversal",
        "symlink",
        "duplicate_member",
        "zero_mapping",
        "multiple_mapping",
        "wrong_unpacker",
    ],
)
def test_archive_and_inventory_mapping_rejections(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
):
    extra_members: tuple[tuple[str, bytes, bytes], ...] = ()
    missing_metadata = None
    duplicate_member = None
    wrong_unpacker = False
    inventory_mutator: Callable[[dict[str, object]], None] | None = None
    if case == "missing_metadata":
        missing_metadata = "environment.yml"
    elif case == "unknown_regular":
        extra_members = (("unexpected/data.bin", b"unknown", tarfile.REGTYPE),)
    elif case == "path_traversal":
        extra_members = (("pypi/../escape.whl", b"escape", tarfile.REGTYPE),)
    elif case == "symlink":
        extra_members = (("pypi/linked.whl", b"", tarfile.SYMTYPE),)
    elif case == "duplicate_member":
        duplicate_member = "environment.yml"
    elif case == "zero_mapping":

        def change_url(data: dict[str, object]) -> None:
            components = data["components"]
            assert isinstance(components, list)
            first = components[0]
            assert isinstance(first, dict)
            external = first["externalReferences"]
            assert isinstance(external, list)
            external[0]["url"] = (
                "https://conda.anaconda.org/conda-forge/linux-64/wrong.conda"
            )

        inventory_mutator = change_url
    elif case == "multiple_mapping":

        def duplicate_component(data: dict[str, object]) -> None:
            components = data["components"]
            assert isinstance(components, list)
            duplicate = json.loads(json.dumps(components[0]))
            duplicate["bom-ref"] = "documents:ambiguous-duplicate"
            components.append(duplicate)

        inventory_mutator = duplicate_component
    elif case == "wrong_unpacker":
        wrong_unpacker = True

    fixture_unpacker = (
        b"wrong unpacker bytes" if wrong_unpacker else b"synthetic pinned unpacker"
    )
    metadata, inventory, output_dir, *_ = _make_fixture(
        tmp_path,
        monkeypatch,
        extra_members=extra_members,
        missing_metadata=missing_metadata,
        duplicate_member=duplicate_member,
        unpacker_bytes=fixture_unpacker,
        inventory_mutator=inventory_mutator,
    )
    if wrong_unpacker:
        producer._PINNED_UNPACKERS["linux-64"] = producer._PinnedUnpacker(
            name=producer._PINNED_UNPACKERS["linux-64"].name,
            url=producer._PINNED_UNPACKERS["linux-64"].url,
            size=len(b"expected different unpacker"),
            sha256=_sha256(b"expected different unpacker"),
        )
        bootstrap_core._UNPACKER_POLICY["linux-64"] = bootstrap_core.PublisherUnpacker(
            name=producer._PINNED_UNPACKERS["linux-64"].name,
            url=producer._PINNED_UNPACKERS["linux-64"].url,
            size=producer._PINNED_UNPACKERS["linux-64"].size,
            sha256=producer._PINNED_UNPACKERS["linux-64"].sha256,
        )

    with pytest.raises(producer.PublisherBootstrapPreparationError):
        producer.prepare_publisher_bootstrap(metadata, inventory, output_dir)
    assert not output_dir.exists()


def test_wrong_windows_publisher_unpacker_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    metadata, inventory, output_dir, *_ = _make_fixture(
        tmp_path,
        monkeypatch,
        platform="win-64",
        unpacker_bytes=b"unexpected Windows unpacker bytes",
    )
    current_pin = producer._PINNED_UNPACKERS["win-64"]
    expected_bytes = b"x" * len(b"unexpected Windows unpacker bytes")
    expected_pin = producer._PinnedUnpacker(
        name=current_pin.name,
        url=current_pin.url,
        size=len(expected_bytes),
        sha256=_sha256(expected_bytes),
    )
    monkeypatch.setitem(producer._PINNED_UNPACKERS, "win-64", expected_pin)
    monkeypatch.setitem(
        bootstrap_core._UNPACKER_POLICY,
        "win-64",
        bootstrap_core.PublisherUnpacker(
            name=expected_pin.name,
            url=expected_pin.url,
            size=expected_pin.size,
            sha256=expected_pin.sha256,
        ),
    )

    with pytest.raises(
        producer.PublisherBootstrapPreparationError, match="pinned publisher bytes"
    ):
        producer.prepare_publisher_bootstrap(metadata, inventory, output_dir)
    assert not output_dir.exists()


def test_duplicate_json_keys_are_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    metadata, inventory, output_dir, *_ = _make_fixture(tmp_path, monkeypatch)
    metadata.write_text(
        '{"format":"wrong","format":"carlina.managed-artifact"}', encoding="utf-8"
    )
    with pytest.raises(
        producer.PublisherBootstrapPreparationError, match="duplicate-free"
    ):
        producer.prepare_publisher_bootstrap(metadata, inventory, output_dir)


def test_duplicate_inventory_json_keys_are_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    metadata, inventory, output_dir, *_ = _make_fixture(tmp_path, monkeypatch)
    inventory.write_text(
        '{"format":"wrong","format":"carlina.documents-release-inventory"}',
        encoding="utf-8",
    )
    with pytest.raises(
        producer.PublisherBootstrapPreparationError, match="duplicate-free"
    ):
        producer.prepare_publisher_bootstrap(metadata, inventory, output_dir)


def test_artifact_filename_traversal_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    metadata_path, inventory, output_dir, *_ = _make_fixture(tmp_path, monkeypatch)
    metadata = json.loads(metadata_path.read_bytes())
    metadata["filename"] = "../escape-linux-64.sh"
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    with pytest.raises(
        producer.PublisherBootstrapPreparationError, match="safe basename"
    ):
        producer.prepare_publisher_bootstrap(metadata_path, inventory, output_dir)


def test_source_pack_bytes_must_match_original_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    metadata_path, inventory, output_dir, *_ = _make_fixture(tmp_path, monkeypatch)
    pack_name = json.loads(metadata_path.read_bytes())["filename"]
    pack_path = metadata_path.parent / pack_name
    pack_path.write_bytes(pack_path.read_bytes() + b"changed")
    with pytest.raises(
        producer.PublisherBootstrapPreparationError, match="does not match"
    ):
        producer.prepare_publisher_bootstrap(metadata_path, inventory, output_dir)


def test_duplicate_inventory_component_refs_are_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    def duplicate_ref(data: dict[str, object]) -> None:
        components = data["components"]
        assert isinstance(components, list)
        duplicate = json.loads(json.dumps(components[0]))
        duplicate["bom-ref"] = components[0]["bom-ref"]
        components.append(duplicate)

    metadata, inventory, output_dir, *_ = _make_fixture(
        tmp_path,
        monkeypatch,
        inventory_mutator=duplicate_ref,
    )
    with pytest.raises(
        producer.PublisherBootstrapPreparationError, match="duplicate component"
    ):
        producer.prepare_publisher_bootstrap(metadata, inventory, output_dir)


@pytest.mark.parametrize("input_name", ["inventory", "source_pack"])
def test_input_mutation_during_preparation_is_rejected_and_outputs_are_removed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    input_name: str,
):
    metadata, inventory, output_dir, *_ = _make_fixture(tmp_path, monkeypatch)
    if input_name == "inventory":
        mutated_path = inventory
    else:
        source_pack_name = json.loads(metadata.read_bytes())["filename"]
        mutated_path = metadata.parent / source_pack_name
    original_bytes = mutated_path.read_bytes()
    original_inspect = producer._inspect_tar

    def mutate_inventory(*args: Any, **kwargs: Any) -> producer._TarInspection:
        result = original_inspect(*args, **kwargs)
        mutated_path.write_bytes(mutated_path.read_bytes() + b" ")
        return result

    monkeypatch.setattr(producer, "_inspect_tar", mutate_inventory)
    with pytest.raises(producer.PublisherBootstrapPreparationError, match="changed"):
        producer.prepare_publisher_bootstrap(metadata, inventory, output_dir)
    assert mutated_path.read_bytes() == original_bytes + b" "
    assert not output_dir.exists()


def test_partial_output_publish_failure_preserves_racing_file_and_cleans_ours(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    metadata, inventory, output_dir, *_ = _make_fixture(tmp_path, monkeypatch)
    original_publish = producer._publish_new_file
    plan_path = output_dir / "plan.json"

    def race_on_plan(source: Path, destination: Path) -> producer._StatIdentity:
        if destination == plan_path:
            destination.write_bytes(b"concurrent output")
        return original_publish(source, destination)

    monkeypatch.setattr(producer, "_publish_new_file", race_on_plan)
    with pytest.raises(
        producer.PublisherBootstrapPreparationError, match="appeared during preparation"
    ):
        producer.prepare_publisher_bootstrap(metadata, inventory, output_dir)

    assert plan_path.read_bytes() == b"concurrent output"
    assert [path.name for path in output_dir.iterdir()] == ["plan.json"]


def test_cli_emits_one_json_summary_line(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    metadata, inventory, output_dir, *_ = _make_fixture(tmp_path, monkeypatch)
    stdout = io.StringIO()
    result = producer.main(
        [
            "--artifact-metadata",
            str(metadata),
            "--inventory",
            str(inventory),
            "--output-dir",
            str(output_dir),
        ],
        stdout=stdout,
    )
    assert result == 0
    summary = json.loads(stdout.getvalue())
    assert set(summary) == {"plan", "skeleton", "preparation"}
    assert len(stdout.getvalue().splitlines()) == 1


@pytest.mark.parametrize("case", ["padding", "after_end", "missing_end"])
def test_unlisted_tar_bytes_and_missing_end_blocks_are_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
):
    def change_archive(payload: bytes) -> bytes:
        with tarfile.open(fileobj=io.BytesIO(payload)) as archive:
            members = archive.getmembers()
        last = members[-1]
        end = last.offset_data + ((last.size + 511) // 512) * 512
        if case == "missing_end":
            return payload[:end]
        changed = bytearray(payload)
        if case == "padding":
            first_file = next(member for member in members if member.isfile())
            assert first_file.size % 512
            changed[first_file.offset_data + first_file.size] = 1
        else:
            assert end + 1024 < len(changed)
            changed[end + 1024] = 1
        return bytes(changed)

    metadata, inventory, output_dir, *_ = _make_fixture(
        tmp_path, monkeypatch, archive_mutator=change_archive
    )
    with pytest.raises(producer.PublisherBootstrapPreparationError):
        producer.prepare_publisher_bootstrap(metadata, inventory, output_dir)
    assert not output_dir.exists()


@pytest.mark.parametrize("stage", ["skeleton", "publish"])
def test_interrupted_preparation_cleans_owned_scratch_and_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
):
    metadata, inventory, output_dir, *_ = _make_fixture(tmp_path, monkeypatch)

    if stage == "skeleton":

        def interrupt_copy(*_args: Any, **_kwargs: Any) -> None:
            raise KeyboardInterrupt("synthetic interruption")

        monkeypatch.setattr(producer, "_copy_range", interrupt_copy)
    else:
        original_publish = producer._publish_new_file

        def interrupt_publish(
            source: Path, destination: Path
        ) -> producer._StatIdentity:
            if destination.name == "plan.json":
                raise KeyboardInterrupt("synthetic interruption")
            return original_publish(source, destination)

        monkeypatch.setattr(producer, "_publish_new_file", interrupt_publish)

    with pytest.raises(KeyboardInterrupt, match="synthetic interruption"):
        producer.prepare_publisher_bootstrap(metadata, inventory, output_dir)
    assert not output_dir.exists()


def test_direct_cli_help_works_outside_the_source_directory(tmp_path: Path):
    result = subprocess.run(
        [sys.executable, "-I", str(Path(producer.__file__)), "--help"],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert "--artifact-metadata" in result.stdout


_LONG_WHEEL_NAME = (
    "charset_normalizer-3.4.9-cp314-cp314-manylinux2014_x86_64."
    "manylinux_2_17_x86_64.manylinux_2_28_x86_64.whl"
)


def test_gnu_long_name_record_survives_exact_reconstruction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    metadata, inventory, output_dir, original_tar, payloads, _ = _make_fixture(
        tmp_path,
        monkeypatch,
        tar_format=tarfile.GNU_FORMAT,
        helper_basename=_LONG_WHEEL_NAME,
    )
    plan_path, skeleton_path, _ = producer.prepare_publisher_bootstrap(
        metadata, inventory, output_dir
    )
    plan = bootstrap_core.parse_publisher_bootstrap_plan(plan_path.read_bytes())
    with tarfile.open(fileobj=io.BytesIO(original_tar)) as archive:
        member = archive.getmember("pypi/" + _LONG_WHEEL_NAME)
    assert member.offset_data == member.offset + 1536
    skeleton = gzip.decompress(skeleton_path.read_bytes())
    assert (
        skeleton[member.offset : member.offset_data]
        == original_tar[member.offset : member.offset_data]
    )
    assert skeleton[member.offset_data : member.offset_data + member.size] == bytes(
        member.size
    )
    package_files: dict[str, Path] = {}
    for package in plan.downloads:
        path = tmp_path / package.sha256
        path.write_bytes(payloads[package.sha256])
        package_files[package.sha256] = path
    rebuilt = bootstrap_core.reconstruct_publisher_archive(
        plan, skeleton_path, package_files, tmp_path / "rebuilt.tar"
    )
    assert rebuilt.read_bytes() == original_tar


@pytest.mark.parametrize("case", ["long_name_padding", "truncated_name", "pax"])
def test_malformed_gnu_long_name_and_unaccepted_pax_records_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
):
    def change_archive(payload: bytes) -> bytes:
        if case == "pax":
            return payload
        with tarfile.open(fileobj=io.BytesIO(payload)) as archive:
            member = archive.getmember("pypi/" + _LONG_WHEEL_NAME)
        changed = bytearray(payload)
        if case == "long_name_padding":
            changed[member.offset + 512 + len(member.name.encode()) + 1] = 1
        else:
            real_header = member.offset_data - 512
            changed[real_header] = ord("X")
            changed[real_header + 148 : real_header + 156] = b" " * 8
            checksum = sum(changed[real_header : real_header + 512])
            changed[real_header + 148 : real_header + 156] = (
                f"{checksum:06o}\x00 ".encode()
            )
        return bytes(changed)

    metadata, inventory, output_dir, *_ = _make_fixture(
        tmp_path,
        monkeypatch,
        tar_format=tarfile.PAX_FORMAT if case == "pax" else tarfile.GNU_FORMAT,
        helper_basename=_LONG_WHEEL_NAME,
        archive_mutator=change_archive,
    )
    with pytest.raises(producer.PublisherBootstrapPreparationError):
        producer.prepare_publisher_bootstrap(metadata, inventory, output_dir)
    assert not output_dir.exists()
