from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import pytest
import yaml

from scripts.generate_documents_inventory import (
    ReleaseInventoryError,
    cyclonedx,
    generate_inventory,
    main,
)

_REVISION = "a" * 40
_HASH = "b" * 64


def _write(root: Path, name: str, data: str) -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data)


def _pixi(root: Path, name: str, *, environment: str, local: bool = False) -> None:
    locator = f"https://conda.anaconda.org/conda-forge/linux-64/{name}-1.0-build.conda"
    references: list[dict[str, str]] = [{"conda": locator}]
    records: list[dict[str, object]] = [
        {
            "conda": locator,
            "name": name,
            "version": "1.0",
            "sha256": _HASH,
            "build": "build",
            "subdir": "linux-64",
            "license": "MIT",
        }
    ]
    if local:
        references.append({"pypi": "./"})
        records.append({"pypi": "./", "name": "carlina"})
    payload = {
        "version": 7,
        "platforms": [{"name": "linux-64"}],
        "environments": {environment: {"packages": {"linux-64": references}}},
        "packages": records,
    }
    _write(root, "pixi.lock", yaml.safe_dump(payload))


def _fixture(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "source Żółć"
    root.mkdir()
    descriptor = json.dumps(
        {
            "format": "carlina.application-distribution",
            "schema_version": 1,
            "application_profile": "documents",
            "source_revision": _REVISION,
            "source_date_epoch": 1,
            "addon_ids": ["doc_processor", "sentence_segmenter", "translator"],
        }
    )
    _write(root, "carlina/distribution.json", descriptor)
    _write(root, "pyproject.toml", '[project]\nname="carlina"\nversion="1.0"\n')
    _pixi(root, "base-native", environment="documents", local=True)
    for addon_id in ("doc_processor", "translator", "sentence_segmenter"):
        addon = root / "iarf_assistant" / "addons" / addon_id
        addon.mkdir(parents=True)
        environments: list[dict[str, str]] = []
        if addon_id != "sentence_segmenter":
            _write(
                addon, "requirements.lock", f"vendor-test==1.0 --hash=sha256:{_HASH}\n"
            )
            environments.append(
                {
                    "id": "host",
                    "environment": "in_process_vendor",
                    "lock_format": "pip_requirements",
                    "lockfile": "requirements.lock",
                }
            )
        if addon_id != "translator":
            _pixi(addon, f"{addon_id}-native", environment="default")
            environments.append(
                {
                    "id": "native",
                    "environment": "managed_process_pixi",
                    "lock_format": "pixi",
                    "lockfile": "pixi.lock",
                }
            )
        _write(
            addon,
            "manifest.json",
            json.dumps(
                {
                    "dependencies": {"python": {"environments": environments}},
                }
            ),
        )
    _write(
        root,
        "package-lock.json",
        json.dumps(
            {
                "lockfileVersion": 3,
                "packages": {
                    "": {"name": "synthetic"},
                    "node_modules/@example/tool": {
                        "version": "1.0.0",
                        "resolved": "https://registry.npmjs.org/tool/-/tool-1.0.0.tgz",
                        "integrity": "sha512-synthetic",
                        "license": "MIT",
                        "dev": True,
                    },
                    "node_modules/local-workspace": {
                        "link": True,
                        "resolved": "frontend/local",
                    },
                },
            }
        ),
    )
    _write(
        root,
        "carlina/segmentation/sat_assets.py",
        f'''
raise RuntimeError("Target application source must never execute")
_MODEL_REPOSITORY = "example/model"
_MODEL_REVISION = "{_REVISION}"
_ASSET_DESCRIPTORS = (
    _AssetDescriptor(repository=_MODEL_REPOSITORY, revision=_MODEL_REVISION,
                     filename="model.onnx", relative_path="model/model.onnx",
                     byte_length=42, sha256="{_HASH}"),
)
''',
    )
    wheel = tmp_path / "synthetic.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("carlina/distribution.json", descriptor)
        archive.writestr(
            "carlina-1.0.dist-info/METADATA", "Name: carlina\nVersion: 1.0\n"
        )
        for path in sorted(root.rglob("*")):
            relative = path.relative_to(root).as_posix()
            if (
                path.is_file()
                and relative.startswith(("carlina/", "iarf_assistant/"))
                and relative != "carlina/distribution.json"
            ):
                archive.writestr(relative, path.read_bytes())
    return root, wheel


def _witness(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file()
    }


def _add_source_offer(wheel: Path, **updates: object) -> dict[str, object]:
    source = b"synthetic Documents source"
    manifest = {
        "format": "carlina.application-source",
        "schema_version": 1,
        "application_profile": "documents",
        "application_version": "1.0",
        "source_revision": _REVISION,
        "source_date_epoch": 1,
        "archive": "application-source.tar.gz",
        "filename": "carlina-1.0.tar.gz",
        "byte_length": len(source),
        "sha256": hashlib.sha256(source).hexdigest(),
        **updates,
    }
    with zipfile.ZipFile(wheel, "a") as archive:
        archive.writestr("carlina/application-source.json", json.dumps(manifest))
        archive.writestr("carlina/application-source.tar.gz", source)
    return manifest


def test_inventory_binds_exact_embedded_application_source(tmp_path: Path) -> None:
    root, wheel = _fixture(tmp_path)
    manifest = _add_source_offer(wheel)
    before = _witness(tmp_path)
    inventory = generate_inventory(root, wheel, platform="linux-64")
    assert inventory["application_source"] == {"coverage": "application", **manifest}
    assert _witness(tmp_path) == before
    assert inventory["public_release_accepted"] is False


@pytest.mark.parametrize(
    "updates",
    [
        {"application_version": "1.1"},
        {"source_revision": "c" * 40},
        {"source_date_epoch": 2},
        {"byte_length": True},
        {"sha256": "c" * 64},
        {"filename": "../private.tar.gz"},
        {"extra": "private"},
    ],
)
def test_inventory_rejects_unbound_source_offer(
    tmp_path: Path, updates: dict[str, object]
) -> None:
    root, wheel = _fixture(tmp_path)
    _add_source_offer(wheel, **updates)
    with pytest.raises(
        ReleaseInventoryError, match="Documents inventory input is invalid"
    ):
        generate_inventory(root, wheel, platform="linux-64")


def test_exact_pair_is_read_only_deterministic_and_does_not_execute_source(
    tmp_path: Path,
) -> None:
    root, wheel = _fixture(tmp_path)
    before = _witness(tmp_path)
    first = generate_inventory(root, wheel, platform="linux-64")
    assert first == generate_inventory(root, wheel, platform="linux-64")
    assert before == _witness(tmp_path)
    assert first["notice_collection_complete"] is False
    assert first["public_release_accepted"] is False
    assert first["reconciled_local_products"] == [
        {
            "context": "documents",
            "name": "carlina",
            "declared_version": None,
            "wheel_version": "1.0",
        }
    ]
    assert str(tmp_path) not in json.dumps(first)
    inputs = {item["path"]: item["sha256"] for item in first["source_inputs"]}
    assert inputs["package-lock.json"] == before["source Żółć/package-lock.json"]
    assert inputs["pixi.lock"] == before["source Żółć/pixi.lock"]


def test_bom_keeps_distinct_environments_and_allowed_hashes_without_inventing_installed_hash(
    tmp_path: Path,
) -> None:
    root, wheel = _fixture(tmp_path)
    bom = cyclonedx(generate_inventory(root, wheel, platform="linux-64"))
    encoded = json.dumps(bom)
    assert bom["bomFormat"] == "CycloneDX"
    assert bom["compositions"] == [
        {
            "aggregate": "incomplete",
            "assemblies": [
                f"documents-wheel:{hashlib.sha256(wheel.read_bytes()).hexdigest()}"
            ],
        }
    ]
    vendors = [item for item in bom["components"] if item["name"] == "vendor-test"]
    assert len(vendors) == 2
    assert vendors[0]["bom-ref"] != vendors[1]["bom-ref"]
    assert all("hashes" not in item for item in vendors)
    assert "frontend-build-lock" in encoded
    assert "segmentation-model-assets" in encoded


def test_mismatched_wheel_descriptor_is_rejected(tmp_path: Path) -> None:
    root, wheel = _fixture(tmp_path)
    _write(
        root,
        "carlina/distribution.json",
        (root / "carlina/distribution.json").read_text().replace(_REVISION, "c" * 40),
    )
    with pytest.raises(ReleaseInventoryError, match="input is invalid"):
        generate_inventory(root, wheel, platform="linux-64")


@pytest.mark.parametrize(
    "relative",
    (
        "carlina/segmentation/sat_assets.py",
        "iarf_assistant/addons/doc_processor/pixi.lock",
        "iarf_assistant/addons/translator/requirements.lock",
        "iarf_assistant/addons/sentence_segmenter/manifest.json",
    ),
)
def test_packaged_inventory_inputs_must_match_wheel(
    tmp_path: Path, relative: str
) -> None:
    root, wheel = _fixture(tmp_path)
    path = root / relative
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(ReleaseInventoryError, match="input is invalid"):
        generate_inventory(root, wheel, platform="linux-64")


def test_duplicate_json_identity_fields_are_rejected(tmp_path: Path) -> None:
    root, wheel = _fixture(tmp_path)
    _write(root, "package-lock.json", '{"lockfileVersion":3,"lockfileVersion":2}')
    with pytest.raises(ReleaseInventoryError, match="input is invalid"):
        generate_inventory(root, wheel, platform="linux-64")


def test_npm_bundled_dependency_retains_parent_without_inventing_artifact(
    tmp_path: Path,
) -> None:
    root, wheel = _fixture(tmp_path)
    lock = root / "package-lock.json"
    data = json.loads(lock.read_text())
    child_path = "node_modules/@example/tool/node_modules/bundled-child"
    data["packages"][child_path] = {
        "version": "2.0",
        "license": "MIT",
        "inBundle": True,
    }
    lock.write_text(json.dumps(data))
    report = generate_inventory(root, wheel, platform="linux-64")
    child = next(
        item for item in report["components"] if item["name"] == "bundled-child"
    )
    assert "externalReferences" not in child
    assert "hashes" not in child
    assert {
        "name": "carlina:bundled-parent-lock-path",
        "value": "node_modules/@example/tool",
    } in child["properties"]
    assert not any(
        item["name"] == "carlina:npm-integrity" for item in child["properties"]
    )


def test_declared_dependency_path_cannot_escape_source(tmp_path: Path) -> None:
    root, wheel = _fixture(tmp_path)
    manifest = root / "iarf_assistant/addons/translator/manifest.json"
    manifest.write_text(
        manifest.read_text().replace("requirements.lock", "../../../../../secret.lock")
    )
    with pytest.raises(ReleaseInventoryError, match="input is invalid"):
        generate_inventory(root, wheel, platform="linux-64")


def test_symlinked_model_source_is_rejected_without_reading_target(
    tmp_path: Path,
) -> None:
    root, wheel = _fixture(tmp_path)
    path = root / "carlina/segmentation/sat_assets.py"
    path.unlink()
    path.symlink_to(tmp_path / "unrelated-private-file")
    with pytest.raises(ReleaseInventoryError, match="input is invalid"):
        generate_inventory(root, wheel, platform="linux-64")


def test_cli_redacts_bad_registry_url_and_creates_no_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root, wheel = _fixture(tmp_path)
    lock = root / "package-lock.json"
    lock.write_text(
        lock.read_text().replace(
            "https://registry.npmjs.org/", "https://secret-token@registry.npmjs.org/"
        )
    )
    output = tmp_path / "invalid-output"
    assert (
        main(
            [
                "--source-root",
                str(root),
                "--wheel",
                str(wheel),
                "--platform",
                "linux-64",
                "--output-dir",
                str(output),
            ]
        )
        == 2
    )
    assert not output.exists()
    assert "secret-token" not in capsys.readouterr().err


def test_cli_writes_new_reports_and_preserves_existing_output(tmp_path: Path) -> None:
    root, wheel = _fixture(tmp_path)
    output = tmp_path / "reports"
    argv = [
        "--source-root",
        str(root),
        "--wheel",
        str(wheel),
        "--platform",
        "linux-64",
        "--output-dir",
        str(output),
    ]
    assert main(argv) == 0
    first = _witness(output)
    assert first.keys() == {"inventory.json", "cyclonedx.json"}
    assert main(argv) == 2
    assert _witness(output) == first
