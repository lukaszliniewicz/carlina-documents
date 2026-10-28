from __future__ import annotations

import gzip
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import scripts.build_publisher_bootstrap as builder
from carlina.core import publisher_bootstrap as bootstrap_core
from carlina.core.publisher_bootstrap import (
    PublisherUnpacker,
    parse_publisher_bootstrap_plan,
    publisher_bootstrap_plan_bytes,
)

pytestmark = [pytest.mark.fast, pytest.mark.contract]

_VERSION = "1.2.3"
_REVISION = "a" * 40


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _record(filename: str, payload: bytes) -> dict[str, object]:
    return {"filename": filename, "size": len(payload), "sha256": _sha256(payload)}


def _fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    patch_unpacker: bool = True,
    include_companion: bool = True,
) -> tuple[Path, Path, Path, Path, dict[str, object], dict[str, object]]:
    platform = "linux-64"
    archive = bytearray(2048)
    package = b"wheel-payload"
    offset = 512
    archive[offset : offset + len(package)] = package
    archive_bytes = bytes(archive)
    skeleton_bytes = gzip.compress(bytes(2048), mtime=0)
    if patch_unpacker:
        unpacker_payload = b"synthetic pinned unpacker"
        unpacker = PublisherUnpacker(
            name="pixi-unpack-x86_64-unknown-linux-musl",
            url=(
                "https://github.com/Quantco/pixi-pack/releases/download/v0.7.10/"
                "pixi-unpack-x86_64-unknown-linux-musl"
            ),
            size=len(unpacker_payload),
            sha256=_sha256(unpacker_payload),
        )
        monkeypatch.setitem(bootstrap_core._UNPACKER_POLICY, platform, unpacker)
    else:
        unpacker = bootstrap_core._UNPACKER_POLICY[platform]

    plan_payload: dict[str, object] = {
        "format": "carlina.publisher-bootstrap.plan",
        "schema_version": 1,
        "platform": platform,
        "application_profile": "documents",
        "version": _VERSION,
        "archive": {"size": len(archive_bytes), "sha256": _sha256(archive_bytes)},
        "skeleton": {
            "filename": "skeleton.tar.gz",
            "size": len(skeleton_bytes),
            "sha256": _sha256(skeleton_bytes),
            "uncompressed_size": len(archive_bytes),
        },
        "downloads": [
            {
                "member_name": "pypi/demo-1.2.3-py3-none-any.whl",
                "offset_data": offset,
                "size": len(package),
                "sha256": _sha256(package),
                "url": (
                    "https://files.pythonhosted.org/packages/ab/cd/"
                    "demo-1.2.3-py3-none-any.whl"
                ),
                "component_ref": "pypi:demo=1.2.3",
            }
        ],
        "unpacker": {
            "name": unpacker.name,
            "url": unpacker.url,
            "size": unpacker.size,
            "sha256": unpacker.sha256,
        },
    }
    plan = parse_publisher_bootstrap_plan(plan_payload)
    plan_bytes = publisher_bootstrap_plan_bytes(plan)

    wheel_payload = b"application-wheel"
    companion_payload = b"companion-wheel"
    wheel = _record(f"carlina-{_VERSION}-py3-none-any.whl", wheel_payload)
    companion = _record(
        f"carlina_application_companions-{_VERSION}-py3-none-any.whl",
        companion_payload,
    )
    source_pack = _record(
        f"carlina-documents-{_VERSION}-{platform}.sh",
        b"original Pixi pack bytes",
    )
    metadata: dict[str, object] = {
        "format": "carlina.managed-artifact",
        "schema_version": 1,
        "version": _VERSION,
        "platform": platform,
        "kind": "pixi-pack-executable",
        **source_pack,
        "built_at": "2026-10-07T00:00:00Z",
        "source_revision": _REVISION,
        "pixi_lock_sha256": "b" * 64,
        "pixi_pack_version": "0.7.10",
        "application_profile": "documents",
        "wheel": wheel,
    }
    if include_companion:
        metadata["companion_wheel"] = companion
    metadata_path = tmp_path / "original-artifact.json"
    metadata_bytes = (json.dumps(metadata, sort_keys=True, indent=2) + "\n").encode()
    metadata_path.write_bytes(metadata_bytes)

    preparation_dir = tmp_path / "preparation"
    preparation_dir.mkdir()
    plan_path = preparation_dir / "plan.json"
    plan_path.write_bytes(plan_bytes)
    skeleton_path = preparation_dir / "skeleton.tar.gz"
    skeleton_path.write_bytes(skeleton_bytes)
    inventory_bytes = b"synthetic inventory evidence"
    application: dict[str, object] = {
        "platform": platform,
        "application_profile": "documents",
        "version": _VERSION,
        "source_revision": _REVISION,
        "wheel": wheel,
    }
    if include_companion:
        application["companion_wheel"] = companion
    preparation: dict[str, object] = {
        "format": "carlina.publisher-bootstrap.preparation",
        "schema_version": 1,
        "application": application,
        "inputs": {
            "artifact_metadata": _record(metadata_path.name, metadata_bytes),
            "inventory": _record("inventory.json", inventory_bytes),
            "source_pack": source_pack,
        },
        "original_archive": {
            "size": plan.archive.size,
            "sha256": plan.archive.sha256,
        },
        "outputs": {
            "plan": _record("plan.json", plan_bytes),
            "skeleton": {
                **_record("skeleton.tar.gz", skeleton_bytes),
                "uncompressed_size": len(archive_bytes),
            },
        },
    }
    preparation_path = preparation_dir / "preparation.json"
    preparation_path.write_text(
        json.dumps(preparation, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    output_dir = tmp_path / "published"
    return preparation_dir, metadata_path, output_dir, plan_path, preparation, metadata


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )


def test_builder_output_and_sidecar_are_deterministic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    preparation_dir, metadata_path, output_dir, _, _, _ = _fixture(
        tmp_path, monkeypatch
    )

    artifact, sidecar = builder.build_publisher_bootstrap(
        preparation_dir, metadata_path, output_dir
    )
    second_artifact, second_sidecar = builder.build_publisher_bootstrap(
        preparation_dir,
        metadata_path,
        tmp_path / "published-again",
    )

    assert artifact.name == "carlina-documents-1.2.3-linux-64.sh"
    assert artifact.read_bytes() == second_artifact.read_bytes()
    assert sidecar.read_bytes() == second_sidecar.read_bytes()
    result = json.loads(sidecar.read_bytes())
    assert result["format"] == "carlina.managed-artifact"
    assert result["kind"] == "publisher-bootstrap-executable"
    assert result["publisher_bootstrap"]["archive"]["size"] == 2048


def test_builder_generates_bootstrap_without_absent_companion_field(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    preparation_dir, metadata_path, output_dir, _, _, original = _fixture(
        tmp_path,
        monkeypatch,
        include_companion=False,
    )

    artifact, sidecar = builder.build_publisher_bootstrap(
        preparation_dir, metadata_path, output_dir
    )

    result = json.loads(sidecar.read_bytes())
    assert artifact.is_file()
    assert result["wheel"] == original["wheel"]
    assert "companion_wheel" not in result
    assert result["publisher_bootstrap"]["archive"]["size"] == 2048


def test_builder_rejects_explicit_null_for_absent_preparation_companion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    preparation_dir, metadata_path, output_dir, _, preparation, _ = _fixture(
        tmp_path,
        monkeypatch,
        include_companion=False,
    )
    changed_preparation = json.loads(json.dumps(preparation))
    changed_preparation["application"]["companion_wheel"] = None
    _write_json(preparation_dir / "preparation.json", changed_preparation)

    with pytest.raises(builder.PublisherBootstrapBuildError):
        builder.build_publisher_bootstrap(preparation_dir, metadata_path, output_dir)
    assert not output_dir.exists()


@pytest.mark.parametrize(
    "mutation",
    [
        "unknown_metadata_key",
        "missing_metadata_key",
        "source_pack_filename",
        "source_pack_size",
        "source_pack_hash",
        "wheel_extra_field",
        "wheel_noncanonical_filename",
        "companion_unsafe_filename",
        "companion_noncanonical_filename",
        "companion_explicit_null",
        "companion_malformed_record",
        "companion_metadata_mismatch",
        "companion_missing_from_preparation",
        "unknown_application_key",
        "profile_mismatch",
        "invalid_lock_digest",
        "timezone_missing",
    ],
)
def test_builder_rejects_open_or_mismatched_metadata_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    preparation_dir, metadata_path, output_dir, _, preparation, metadata = _fixture(
        tmp_path, monkeypatch
    )
    changed_preparation = json.loads(json.dumps(preparation))
    changed_metadata = json.loads(json.dumps(metadata))
    if mutation == "unknown_metadata_key":
        changed_metadata["unreviewed"] = True
    elif mutation == "missing_metadata_key":
        changed_metadata.pop("kind")
    elif mutation == "source_pack_filename":
        changed_preparation["inputs"]["source_pack"]["filename"] = "other-pack.sh"
    elif mutation == "source_pack_size":
        changed_preparation["inputs"]["source_pack"]["size"] += 1
    elif mutation == "source_pack_hash":
        changed_preparation["inputs"]["source_pack"]["sha256"] = "0" * 64
    elif mutation == "wheel_extra_field":
        changed_metadata["wheel"]["download_url"] = "https://example.invalid/wheel"
        changed_preparation["application"]["wheel"]["download_url"] = (
            "https://example.invalid/wheel"
        )
    elif mutation == "wheel_noncanonical_filename":
        changed_metadata["wheel"]["filename"] = "other-wheel.whl"
        changed_preparation["application"]["wheel"]["filename"] = "other-wheel.whl"
    elif mutation == "companion_unsafe_filename":
        changed_metadata["companion_wheel"]["filename"] = "../companion.whl"
        changed_preparation["application"]["companion_wheel"]["filename"] = (
            "../companion.whl"
        )
    elif mutation == "companion_noncanonical_filename":
        changed_metadata["companion_wheel"]["filename"] = "other-companion.whl"
        changed_preparation["application"]["companion_wheel"]["filename"] = (
            "other-companion.whl"
        )
    elif mutation == "companion_explicit_null":
        changed_metadata["companion_wheel"] = None
    elif mutation == "companion_malformed_record":
        malformed = dict(changed_metadata["companion_wheel"])
        malformed["sha256"] = "invalid"
        changed_metadata["companion_wheel"] = malformed
        changed_preparation["application"]["companion_wheel"] = malformed
    elif mutation == "companion_metadata_mismatch":
        changed_metadata["companion_wheel"]["sha256"] = "0" * 64
    elif mutation == "companion_missing_from_preparation":
        changed_preparation["application"].pop("companion_wheel")
    elif mutation == "unknown_application_key":
        changed_preparation["application"]["unreviewed"] = True
    elif mutation == "invalid_lock_digest":
        changed_metadata["pixi_lock_sha256"] = "z" * 64
    elif mutation == "timezone_missing":
        changed_metadata["built_at"] = "2026-10-07T00:00:00"
    else:
        changed_metadata["application_profile"] = "workbench"
        changed_preparation["application"]["application_profile"] = "workbench"

    _write_json(metadata_path, changed_metadata)
    _write_json(preparation_dir / "preparation.json", changed_preparation)

    with pytest.raises(builder.PublisherBootstrapBuildError):
        builder.build_publisher_bootstrap(preparation_dir, metadata_path, output_dir)
    assert not output_dir.exists()


@pytest.mark.parametrize("input_name", ["metadata", "preparation"])
def test_builder_rejects_duplicate_json_keys_and_non_finite_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    input_name: str,
) -> None:
    preparation_dir, metadata_path, output_dir, _, preparation, metadata = _fixture(
        tmp_path, monkeypatch
    )
    if input_name == "metadata":
        duplicate = json.dumps(metadata, sort_keys=True).replace(
            '"schema_version": 1',
            '"schema_version": 1, "schema_version": 1',
            1,
        )
        metadata_path.write_text(duplicate, encoding="utf-8")
        expected = "duplicate JSON"
    else:
        preparation_json = json.dumps(preparation, sort_keys=True)
        preparation_dir.joinpath("preparation.json").write_text(
            preparation_json[:-1] + ',"unexpected":NaN}',
            encoding="utf-8",
        )
        expected = "non-finite"

    with pytest.raises(builder.PublisherBootstrapBuildError, match=expected):
        builder.build_publisher_bootstrap(preparation_dir, metadata_path, output_dir)
    assert not output_dir.exists()


def test_builder_refuses_symlink_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    preparation_dir, metadata_path, output_dir, _, _, _ = _fixture(
        tmp_path, monkeypatch
    )
    skeleton = preparation_dir / "skeleton.tar.gz"
    regular = tmp_path / "regular-skeleton.tar.gz"
    skeleton.replace(regular)
    skeleton.symlink_to(regular)

    with pytest.raises(builder.PublisherBootstrapBuildError, match="unsafe"):
        builder.build_publisher_bootstrap(preparation_dir, metadata_path, output_dir)
    assert not output_dir.exists()


def test_builder_refuses_existing_output_directory_and_preserves_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    preparation_dir, metadata_path, output_dir, _, _, _ = _fixture(
        tmp_path, monkeypatch
    )
    output_dir.mkdir()
    sentinel = output_dir / "keep.txt"
    sentinel.write_text("concurrent output", encoding="utf-8")

    with pytest.raises(FileExistsError):
        builder.build_publisher_bootstrap(preparation_dir, metadata_path, output_dir)
    assert sentinel.read_text(encoding="utf-8") == "concurrent output"


@pytest.mark.parametrize("when", ["before_publication", "during_publication"])
def test_builder_revalidates_frozen_inputs_before_and_during_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    when: str,
) -> None:
    preparation_dir, metadata_path, output_dir, plan_path, _, _ = _fixture(
        tmp_path, monkeypatch
    )
    original_plan = plan_path.read_bytes()
    if when == "before_publication":
        render = builder.render_publisher_bootstrap

        def mutate_after_render(*args: object, **kwargs: object) -> bytes:
            artifact = render(*args, **kwargs)
            plan_path.write_bytes(original_plan + b" ")
            return artifact

        monkeypatch.setattr(builder, "render_publisher_bootstrap", mutate_after_render)
    else:
        link = os.link
        calls = 0

        def mutate_after_first_link(
            source: str | bytes | os.PathLike[str],
            destination: str | bytes | os.PathLike[str],
            **kwargs: object,
        ) -> None:
            nonlocal calls
            link(source, destination, **kwargs)
            calls += 1
            if calls == 1:
                plan_path.write_bytes(original_plan + b" ")

        monkeypatch.setattr(builder.os, "link", mutate_after_first_link)

    with pytest.raises(builder.PublisherBootstrapBuildError, match="changed"):
        builder.build_publisher_bootstrap(preparation_dir, metadata_path, output_dir)
    assert not output_dir.exists()


def test_builder_atomic_link_failure_cleans_owned_file_and_preserves_racing_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    preparation_dir, metadata_path, output_dir, _, _, _ = _fixture(
        tmp_path, monkeypatch
    )
    link = os.link
    raced_file = output_dir / "carlina-documents-1.2.3-linux-64.sh.artifact.json"
    foreign_bytes = b"file created by another process"

    def fail_sidecar_link(
        source: str | bytes | os.PathLike[str],
        destination: str | bytes | os.PathLike[str],
        **kwargs: object,
    ) -> None:
        if Path(destination) == raced_file:
            raced_file.write_bytes(foreign_bytes)
            raise FileExistsError("synthetic concurrent publisher")
        link(source, destination, **kwargs)

    monkeypatch.setattr(builder.os, "link", fail_sidecar_link)

    with pytest.raises(FileExistsError, match="concurrent publisher"):
        builder.build_publisher_bootstrap(preparation_dir, metadata_path, output_dir)

    assert not (output_dir / "carlina-documents-1.2.3-linux-64.sh").exists()
    assert raced_file.read_bytes() == foreign_bytes
    assert list(output_dir.iterdir()) == [raced_file]
    assert list(output_dir.glob(".publisher-build-*")) == []


def test_builder_cli_runs_outside_repository_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    preparation_dir, metadata_path, output_dir, _, _, _ = _fixture(
        tmp_path, monkeypatch, patch_unpacker=False
    )
    caller_directory = tmp_path / "caller"
    caller_directory.mkdir()

    result = subprocess.run(
        [
            sys.executable,
            "-I",
            str(Path(builder.__file__).resolve()),
            "--preparation-dir",
            str(preparation_dir),
            "--artifact-metadata",
            str(metadata_path),
            "--output-dir",
            str(output_dir),
        ],
        cwd=caller_directory,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    assert Path(summary["artifact"]).is_file()
    assert Path(summary["metadata"]).is_file()
