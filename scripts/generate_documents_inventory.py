"""Inventory an exact Documents source/wheel pair without executing its code."""

from __future__ import annotations

import argparse
import ast
import hashlib
import io
import json
import os
import re
import stat
import sys
import tomllib
import zipfile
from collections.abc import Sequence
from email.parser import BytesParser
from pathlib import Path
from typing import TYPE_CHECKING, Final, cast
from urllib.parse import quote, urlsplit

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if not __package__ and os.fspath(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, os.fspath(PROJECT_ROOT))

if TYPE_CHECKING or not __package__:
    from scripts.documents_dependency_inventory import (
        LockedPackage,
        load_pixi_lock,
        load_requirements_lock,
    )
else:
    from .documents_dependency_inventory import (
        LockedPackage,
        load_pixi_lock,
        load_requirements_lock,
    )

_ADDONS: Final = ("doc_processor", "sentence_segmenter", "translator")
_INPUT_LIMIT: Final = 16 * 1024 * 1024
_WHEEL_LIMIT: Final = 256 * 1024 * 1024
_SOURCE_LIMIT: Final = 64 * 1024 * 1024
_SOURCE_MANIFEST_LIMIT: Final = 4096
_INVALID_INPUT: Final = "Documents inventory input is invalid."
_INVALID_OUTPUT: Final = "Documents inventory output must be a new directory."
_SHA256: Final = re.compile(r"[0-9a-f]{64}\Z")
_REVISION: Final = re.compile(r"[0-9a-f]{40}\Z")
_LockContext = tuple[str, str, str, str | None]


class ReleaseInventoryError(ValueError):
    """An inventory failure that does not disclose input paths or contents."""


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ReleaseInventoryError(_INVALID_INPUT)
    raw = cast(dict[object, object], value)
    if any(not isinstance(key, str) for key in raw):
        raise ReleaseInventoryError(_INVALID_INPUT)
    return cast(dict[str, object], raw)


def _string(value: object) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ReleaseInventoryError(_INVALID_INPUT)
    return value


def _list(value: object) -> list[object]:
    if not isinstance(value, list):
        raise ReleaseInventoryError(_INVALID_INPUT)
    return cast(list[object], value)


def _read(path: Path, *, limit: int = _INPUT_LIMIT) -> bytes:
    status = path.lstat()
    if not stat.S_ISREG(status.st_mode) or status.st_size > limit:
        raise ReleaseInventoryError(_INVALID_INPUT)
    result = path.read_bytes()
    if len(result) > limit:
        raise ReleaseInventoryError(_INVALID_INPUT)
    return result


def _source_path(root: Path, relative: str) -> Path:
    current = root
    if not stat.S_ISDIR(root.lstat().st_mode):
        raise ReleaseInventoryError(_INVALID_INPUT)
    for part in relative.split("/"):
        if part in {"", ".", ".."} or "\\" in part or ":" in part:
            raise ReleaseInventoryError(_INVALID_INPUT)
        current /= part
        if stat.S_ISLNK(current.lstat().st_mode):
            raise ReleaseInventoryError(_INVALID_INPUT)
    return current


def _json(path: Path) -> dict[str, object]:
    parsed: object = json.loads(_read(path), object_pairs_hook=_unique_object)
    return _object(parsed)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ReleaseInventoryError(_INVALID_INPUT)
        result[key] = value
    return result


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _property(name: str, value: str) -> dict[str, str]:
    return {"name": f"carlina:{name}", "value": value}


def _descriptor(root: Path) -> tuple[dict[str, object], bytes]:
    raw = _read(_source_path(root, "carlina/distribution.json"))
    parsed: object = json.loads(raw, object_pairs_hook=_unique_object)
    descriptor = _object(parsed)
    if (
        descriptor.get("format") != "carlina.application-distribution"
        or type(descriptor.get("schema_version")) is not int
        or descriptor.get("schema_version") != 1
        or descriptor.get("application_profile") != "documents"
        or descriptor.get("addon_ids") != list(_ADDONS)
        or _REVISION.fullmatch(_string(descriptor.get("source_revision"))) is None
        or type(descriptor.get("source_date_epoch")) is not int
        or cast(int, descriptor.get("source_date_epoch")) < 0
    ):
        raise ReleaseInventoryError(_INVALID_INPUT)
    return descriptor, raw


def _wheel_identity(
    wheel: Path, descriptor: bytes, packaged_inputs: dict[str, str]
) -> tuple[str, str, dict[str, object] | None]:
    payload = _read(wheel, limit=_WHEEL_LIMIT)
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        names = archive.namelist()
        metadata = [name for name in names if name.endswith(".dist-info/METADATA")]
        if len(names) != len(set(names)) or len(metadata) != 1:
            raise ReleaseInventoryError(_INVALID_INPUT)
        for member in (*metadata, "carlina/distribution.json"):
            if archive.getinfo(member).file_size > _INPUT_LIMIT:
                raise ReleaseInventoryError(_INVALID_INPUT)
        if archive.read("carlina/distribution.json") != descriptor:
            raise ReleaseInventoryError(_INVALID_INPUT)
        for member, digest in packaged_inputs.items():
            if archive.getinfo(member).file_size > _INPUT_LIMIT:
                raise ReleaseInventoryError(_INVALID_INPUT)
            if _digest(archive.read(member)) != digest:
                raise ReleaseInventoryError(_INVALID_INPUT)
        message = BytesParser().parsebytes(archive.read(metadata[0]))
        if message.get_all("Name") != ["carlina"]:
            raise ReleaseInventoryError(_INVALID_INPUT)
        versions = message.get_all("Version") or []
        if len(versions) != 1:
            raise ReleaseInventoryError(_INVALID_INPUT)
        version = _string(versions[0])
        source_offer = _application_source(archive, descriptor, version)
    return version, _digest(payload), source_offer


def _application_source(
    archive: zipfile.ZipFile, descriptor: bytes, version: str
) -> dict[str, object] | None:
    """Bind source bytes without importing code from the inspected artifact."""
    manifest_member = "carlina/application-source.json"
    source_member = "carlina/application-source.tar.gz"
    present = {manifest_member, source_member}.intersection(archive.namelist())
    if not present:
        return None
    if present != {manifest_member, source_member}:
        raise ReleaseInventoryError(_INVALID_INPUT)
    if archive.getinfo(manifest_member).file_size > _SOURCE_MANIFEST_LIMIT:
        raise ReleaseInventoryError(_INVALID_INPUT)
    source_info = archive.getinfo(source_member)
    if not 0 < source_info.file_size <= _SOURCE_LIMIT:
        raise ReleaseInventoryError(_INVALID_INPUT)
    manifest = _object(
        json.loads(archive.read(manifest_member), object_pairs_hook=_unique_object)
    )
    distribution = _object(json.loads(descriptor))
    filename = _string(manifest.get("filename"))
    if (
        set(manifest)
        != {
            "format",
            "schema_version",
            "application_profile",
            "application_version",
            "source_revision",
            "source_date_epoch",
            "archive",
            "filename",
            "byte_length",
            "sha256",
        }
        or manifest["format"] != "carlina.application-source"
        or type(manifest["schema_version"]) is not int
        or manifest["schema_version"] != 1
        or manifest["application_profile"] != "documents"
        or manifest["application_version"] != version
        or manifest["source_revision"] != distribution["source_revision"]
        or type(manifest["source_date_epoch"]) is not int
        or manifest["source_date_epoch"] != distribution["source_date_epoch"]
        or manifest["archive"] != "application-source.tar.gz"
        or re.fullmatch(r"[A-Za-z0-9._+!-]{1,200}", filename) is None
        or ".." in filename
        or not filename.endswith(".tar.gz")
        or type(manifest["byte_length"]) is not int
        or manifest["byte_length"] != source_info.file_size
        or _SHA256.fullmatch(_string(manifest["sha256"])) is None
        or manifest["sha256"] != _digest(archive.read(source_member))
    ):
        raise ReleaseInventoryError(_INVALID_INPUT)
    return {"coverage": "application", **manifest}


def _remote_url(value: object) -> str:
    url = _string(value)
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or not parts.netloc
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
    ):
        raise ReleaseInventoryError(_INVALID_INPUT)
    return url


def _component_ref(context: str, identity: dict[str, object]) -> str:
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    return f"{context}:{_digest(encoded)}"


def _locked_component(context: str, package: LockedPackage) -> dict[str, object]:
    if package.local or package.version is None:
        raise ReleaseInventoryError(_INVALID_INPUT)
    properties = [
        _property("environment", context),
        _property("allowed-artifact-sha256", json.dumps(package.sha256)),
    ]
    for name, value in (("build", package.build), ("subdir", package.subdir)):
        if value is not None:
            properties.append(_property(name, value))
    identity: dict[str, object] = {
        "type": "library",
        "name": package.name,
        "version": package.version,
        "purl": f"pkg:{package.ecosystem}/{quote(package.name, safe='')}@"
        f"{quote(package.version, safe='')}",
        "properties": properties,
    }
    if package.locator is not None:
        identity["externalReferences"] = [
            {"type": "distribution", "url": package.locator}
        ]
        if len(package.sha256) == 1:
            identity["hashes"] = [{"alg": "SHA-256", "content": package.sha256[0]}]
    if package.license_expression is not None:
        identity["licenses"] = [{"license": {"name": package.license_expression}}]
    identity["bom-ref"] = _component_ref(context, identity)
    return identity


def documents_lock_contexts(root: Path) -> list[_LockContext]:
    """Return the validated, owner-labelled locks for the Documents closure."""
    contexts: list[_LockContext] = [("documents", "pixi.lock", "pixi", "documents")]
    for addon_id in _ADDONS:
        relative_root = f"iarf_assistant/addons/{addon_id}"
        manifest = _json(_source_path(root, f"{relative_root}/manifest.json"))
        dependencies = _object(manifest.get("dependencies"))
        python = _object(dependencies.get("python"))
        for raw in _list(python.get("environments")):
            environment = _object(raw)
            kind = environment.get("environment")
            expected_format = {
                "in_process_vendor": "pip_requirements",
                "managed_process_pixi": "pixi",
            }.get(_string(kind))
            if (
                expected_format is None
                or environment.get("lock_format") != expected_format
            ):
                raise ReleaseInventoryError(_INVALID_INPUT)
            context = f"{addon_id}:{_string(environment.get('id'))}"
            lock = f"{relative_root}/{_string(environment.get('lockfile'))}"
            _source_path(root, lock)
            contexts.append(
                (
                    context,
                    lock,
                    expected_format,
                    "default" if expected_format == "pixi" else None,
                )
            )
    if len({item[0] for item in contexts}) != len(contexts):
        raise ReleaseInventoryError(_INVALID_INPUT)
    return contexts


def _backend_inventory(
    root: Path, platform: str, version: str, contexts: list[_LockContext]
) -> tuple[list[dict[str, object]], list[dict[str, object]], list[dict[str, object]]]:
    components: list[dict[str, object]] = []
    sources: list[dict[str, object]] = []
    local_products: list[dict[str, object]] = []
    for context, relative, kind, environment in contexts:
        path = _source_path(root, relative)
        lock = (
            load_pixi_lock(path, environment=_string(environment), platform=platform)
            if kind == "pixi"
            else load_requirements_lock(path)
        )
        sources.append(
            {
                "context": context,
                "path": relative,
                "sha256": lock.lock_sha256,
                "selected_records": len(lock.packages),
                "format": kind,
            }
        )
        for package in lock.packages:
            if package.local:
                if context != "documents" or package.name != "carlina":
                    raise ReleaseInventoryError(_INVALID_INPUT)
                local_products.append(
                    {
                        "context": context,
                        "name": package.name,
                        "declared_version": package.version,
                        "wheel_version": version,
                    }
                )
            else:
                components.append(_locked_component(context, package))
    return components, sources, local_products


def _input_inventory(root: Path, contexts: list[_LockContext]) -> list[dict[str, str]]:
    members = {
        "carlina/distribution.json",
        "carlina/segmentation/sat_assets.py",
        "pyproject.toml",
        "package-lock.json",
        *(relative for _, relative, _, _ in contexts),
        *(f"iarf_assistant/addons/{addon}/manifest.json" for addon in _ADDONS),
    }
    return [
        {"path": member, "sha256": _digest(_read(_source_path(root, member)))}
        for member in sorted(members)
    ]


def _npm_inventory(root: Path) -> list[dict[str, object]]:
    lock = _json(_source_path(root, "package-lock.json"))
    if lock.get("lockfileVersion") != 3:
        raise ReleaseInventoryError(_INVALID_INPUT)
    packages = _object(lock.get("packages"))
    components: list[dict[str, object]] = []
    for path, raw in sorted(packages.items()):
        package = _object(raw)
        if "node_modules/" not in path or package.get("link") is True:
            continue
        name = _string(package.get("name", path.rsplit("node_modules/", 1)[1]))
        properties = [
            _property("environment", "frontend-build-lock"),
            _property("lock-path", path),
        ]
        identity: dict[str, object] = {
            "type": "library",
            "name": name,
            "version": _string(package.get("version")),
            "properties": properties,
        }
        if package.get("inBundle") is True:
            parent_path = path.rsplit("/node_modules/", 1)[0]
            parent = _object(packages.get(parent_path))
            _remote_url(parent.get("resolved"))
            _string(parent.get("integrity"))
            properties.append(_property("bundled-parent-lock-path", parent_path))
            properties.append(_property("npm-in-bundle", "true"))
        if package.get("resolved") is not None or package.get("inBundle") is not True:
            identity["externalReferences"] = [
                {"type": "distribution", "url": _remote_url(package.get("resolved"))}
            ]
            properties.append(
                _property("npm-integrity", _string(package.get("integrity")))
            )
        elif package.get("integrity") is not None:
            raise ReleaseInventoryError(_INVALID_INPUT)
        license_name = package.get("license")
        if license_name is not None:
            identity["licenses"] = [{"license": {"name": _string(license_name)}}]
        identity["bom-ref"] = _component_ref("frontend-build-lock", identity)
        components.append(identity)
    return components


def _asset_literal(value: ast.expr, constants: dict[str, str]) -> object:
    if isinstance(value, ast.Name):
        return constants[value.id]
    return cast(object, ast.literal_eval(value))


def _model_inventory(root: Path) -> list[dict[str, object]]:
    tree = ast.parse(_read(_source_path(root, "carlina/segmentation/sat_assets.py")))
    constants: dict[str, str] = {}
    descriptors: ast.Tuple | None = None
    for statement in tree.body:
        if not isinstance(statement, ast.Assign) or len(statement.targets) != 1:
            continue
        target = statement.targets[0]
        if not isinstance(target, ast.Name):
            continue
        if target.id in {
            "_MODEL_REPOSITORY",
            "_MODEL_REVISION",
            "_TOKENIZER_REPOSITORY",
            "_TOKENIZER_REVISION",
        }:
            constants[target.id] = _string(ast.literal_eval(statement.value))
        if target.id == "_ASSET_DESCRIPTORS" and isinstance(statement.value, ast.Tuple):
            descriptors = statement.value
    if descriptors is None or not descriptors.elts:
        raise ReleaseInventoryError(_INVALID_INPUT)
    components: list[dict[str, object]] = []
    for expression in descriptors.elts:
        if not isinstance(expression, ast.Call) or not isinstance(
            expression.func, ast.Name
        ):
            raise ReleaseInventoryError(_INVALID_INPUT)
        if expression.func.id != "_AssetDescriptor" or expression.args:
            raise ReleaseInventoryError(_INVALID_INPUT)
        fields = {
            kw.arg: _asset_literal(kw.value, constants) for kw in expression.keywords
        }
        if len(fields) != len(expression.keywords) or set(fields) != {
            "repository",
            "revision",
            "filename",
            "relative_path",
            "byte_length",
            "sha256",
        }:
            raise ReleaseInventoryError(_INVALID_INPUT)
        repository = _string(fields["repository"])
        revision = _string(fields["revision"])
        sha256 = _string(fields["sha256"])
        filename = _string(fields["filename"])
        relative_path = _string(fields["relative_path"])
        if (
            _REVISION.fullmatch(revision) is None
            or _SHA256.fullmatch(sha256) is None
            or type(fields["byte_length"]) is not int
            or fields["byte_length"] <= 0
            or len(repository.split("/")) != 2
            or any(
                re.fullmatch(r"[a-zA-Z0-9_.-]+", part) is None or part in {".", ".."}
                for part in (
                    *repository.split("/"),
                    *filename.split("/"),
                    *relative_path.split("/"),
                )
            )
        ):
            raise ReleaseInventoryError(_INVALID_INPUT)
        identity: dict[str, object] = {
            "type": "file",
            "name": relative_path,
            "version": revision,
            "hashes": [{"alg": "SHA-256", "content": sha256}],
            "properties": [
                _property("environment", "segmentation-model-assets"),
                _property("repository", repository),
                _property("byte-length", str(fields["byte_length"])),
            ],
            "externalReferences": [
                {
                    "type": "distribution",
                    "url": _remote_url(
                        f"https://huggingface.co/{repository}/resolve/{revision}/{filename}"
                    ),
                }
            ],
        }
        identity["bom-ref"] = _component_ref("segmentation-model-assets", identity)
        components.append(identity)
    return components


def generate_inventory(root: Path, wheel: Path, *, platform: str) -> dict[str, object]:
    """Capture resolved pins and provenance; notice acceptance remains separate."""
    if platform not in {"linux-64", "win-64"}:
        raise ReleaseInventoryError(_INVALID_INPUT)
    descriptor, descriptor_bytes = _descriptor(root)
    contexts = documents_lock_contexts(root)
    inputs = _input_inventory(root, contexts)
    packaged_inputs = {
        item["path"]: item["sha256"]
        for item in inputs
        if item["path"].startswith(("carlina/", "iarf_assistant/"))
    }
    version, wheel_sha256, source_offer = _wheel_identity(
        wheel, descriptor_bytes, packaged_inputs
    )
    pyproject = _object(
        tomllib.loads(_read(_source_path(root, "pyproject.toml")).decode())
    )
    project = _object(pyproject.get("project"))
    if project.get("name") != "carlina" or project.get("version") != version:
        raise ReleaseInventoryError(_INVALID_INPUT)
    backend, sources, local = _backend_inventory(root, platform, version, contexts)
    components = [*backend, *_npm_inventory(root), *_model_inventory(root)]
    component_refs = [_string(component.get("bom-ref")) for component in components]
    if len(component_refs) != len(set(component_refs)):
        raise ReleaseInventoryError(_INVALID_INPUT)
    return {
        "format": "carlina.documents-release-inventory",
        "schema_version": 1,
        "application_profile": "documents",
        "source_revision": descriptor["source_revision"],
        "application_version": version,
        "wheel_sha256": wheel_sha256,
        "application_source": source_offer,
        "platform": platform,
        "basis": "resolved locks and packaged asset descriptors",
        "frontend_scope": "entire build lock; bundled membership is not inferred",
        "notice_collection_complete": False,
        "public_release_accepted": False,
        "excluded_optional_runtime": ["sentence_segmenter:service/container"],
        "source_locks": sources,
        "source_inputs": inputs,
        "reconciled_local_products": local,
        "components": components,
        "required_follow_up": [
            "Collect exact installed package, font and model license texts/notices.",
            "Review frontend bundle membership and applicable notices.",
            "Record distribution decisions and any required corresponding source.",
        ],
    }


def cyclonedx(inventory: dict[str, object]) -> dict[str, object]:
    """Project the inventory into CycloneDX 1.6 with explicit incomplete coverage."""
    wheel_sha = _string(inventory.get("wheel_sha256"))
    root_ref = f"documents-wheel:{wheel_sha}"
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "version": 1,
        "metadata": {
            "component": {
                "type": "application",
                "name": "Carlina Documents",
                "version": _string(inventory.get("application_version")),
                "bom-ref": root_ref,
                "hashes": [{"alg": "SHA-256", "content": wheel_sha}],
                "properties": [
                    _property(
                        "source-revision", _string(inventory.get("source_revision"))
                    ),
                    _property("platform", _string(inventory.get("platform"))),
                    _property("notice-acceptance", "incomplete"),
                    _property(
                        "frontend-scope", _string(inventory.get("frontend_scope"))
                    ),
                ],
            }
        },
        "components": _list(inventory.get("components")),
        "compositions": [{"aggregate": "incomplete", "assemblies": [root_ref]}],
    }


def main(argv: Sequence[str] | None = None) -> int:
    """Write two portable reports into a new owned directory."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--platform", choices=("linux-64", "win-64"), required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        inventory = generate_inventory(
            cast(Path, args.source_root),
            cast(Path, args.wheel),
            platform=cast(str, args.platform),
        )
        bom = cyclonedx(inventory)
        output = cast(Path, args.output_dir)
        output.mkdir(mode=0o700, parents=True, exist_ok=False)
        for name, report in (("inventory.json", inventory), ("cyclonedx.json", bom)):
            with (output / name).open("x", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(report, ensure_ascii=True, sort_keys=True, indent=2)
                    + "\n"
                )
    except FileExistsError:
        print(_INVALID_OUTPUT, file=sys.stderr)
        return 2
    except (
        OSError,
        ValueError,
        KeyError,
        zipfile.BadZipFile,
        SyntaxError,
    ):
        print(_INVALID_INPUT, file=sys.stderr)
        return 2
    print(
        f"Documents inventory: {len(_list(inventory['components']))} components; notice review remains required."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
