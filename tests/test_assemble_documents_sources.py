"""Offline contract tests for the Documents source companion assembler."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest

from scripts import assemble_documents_sources as assembler

_REVISION = "a" * 40
_WHEEL_SHA = "b" * 64


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _write_json(path: Path, value: object) -> Path:
    path.write_bytes(_json_bytes(value))
    return path


def _component(
    name: str,
    version: str,
    ref: str,
    binary_sha256: str,
    *,
    use_actual_hash: bool,
) -> dict[str, object]:
    component: dict[str, object] = {
        "name": name,
        "version": version,
        "bom-ref": ref,
        "purl": f"pkg:pypi/{name}@{version}",
        "licenses": [{"license": {"name": "MIT"}}],
        "properties": [
            {
                "name": "carlina:allowed-artifact-sha256",
                "value": json.dumps([binary_sha256]),
            }
        ],
    }
    if use_actual_hash:
        component["hashes"] = [{"alg": "SHA-256", "content": binary_sha256}]
    return component


@dataclass
class _Case:
    root: Path
    manifest_path: Path
    files_dir: Path
    inventory_paths: dict[str, Path]
    manifest: dict[str, object]
    inventories: dict[str, dict[str, object]]
    payloads: dict[str, bytes]

    def write_manifest(self, value: dict[str, object] | None = None) -> None:
        _write_json(self.manifest_path, self.manifest if value is None else value)

    def write_inventory(self, platform: str, value: dict[str, object]) -> None:
        _write_json(self.inventory_paths[platform], value)
        self.inventories[platform] = value

    def output(self, name: str = "source-companion.zip") -> Path:
        return self.root / name


def _make_case(tmp_path: Path) -> _Case:
    root = tmp_path / "case"
    root.mkdir(parents=True)
    files_dir = root / "blobs"
    files_dir.mkdir()
    payloads = {
        "sources/linux-conda.tar.zst": b"\x00opaque conda source\xff\n",
        "sources/pypi-empty.whl": b"",
        "sources/windows-src.zip": (
            b"from pathlib import Path\nPath('must-not-run').write_text('bad')\n"
        ),
        "recipes/linux-recipe.yaml": b"source:\n  url: https://example.invalid/src\n",
        "notices/linux-license.txt": b"Synthetic notice.\n",
        "instructions/README.txt": b"Apply the retained source provenance manually.\n",
    }
    file_records: list[dict[str, object]] = []
    for member, payload in payloads.items():
        digest = _sha(payload)
        blob = files_dir / digest
        if not blob.exists():
            blob.write_bytes(payload)
        kind = {
            "sources": "source",
            "recipes": "recipe",
            "notices": "notice",
            "instructions": "instructions",
        }[member.split("/", 1)[0]]
        row: dict[str, object] = {
            "member": member,
            "kind": kind,
            "sha256": digest,
            "bytes": len(payload),
            "origin_url": None,
            "origin_archive_sha256": None,
            "origin_member": None,
        }
        if kind == "source":
            row["origin_url"] = "https://example.invalid/source.tar.gz"
        elif kind == "recipe":
            row["origin_archive_sha256"] = "c" * 64
            row["origin_member"] = "info/recipe/meta.yaml"
        file_records.append(row)

    linux_hash = "1" * 64
    pypi_hash = "2" * 64
    windows_hash = "3" * 64
    linux = {
        "format": "carlina.documents-release-inventory",
        "schema_version": 1,
        "application_profile": "documents",
        "application_version": "1.2.3.dev4",
        "source_revision": _REVISION,
        "wheel_sha256": _WHEEL_SHA,
        "platform": "linux-64",
        "components": [
            _component(
                "native-lib",
                "1.0",
                "conda:native-linux",
                linux_hash,
                use_actual_hash=True,
            ),
            _component(
                "vendor-wheel",
                "2.0",
                "pypi:vendor-linux",
                pypi_hash,
                use_actual_hash=False,
            ),
        ],
    }
    windows = {
        "format": "carlina.documents-release-inventory",
        "schema_version": 1,
        "application_profile": "documents",
        "application_version": "1.2.3.dev4",
        "source_revision": _REVISION,
        "wheel_sha256": _WHEEL_SHA,
        "platform": "win-64",
        "components": [
            _component(
                "native-lib",
                "1.0",
                "conda:native-win",
                windows_hash,
                use_actual_hash=True,
            )
        ],
    }
    inventory_paths = {
        "linux-64": _write_json(root / "linux-inventory.json", linux),
        "win-64": _write_json(root / "windows-inventory.json", windows),
    }
    file_by_member = {str(item["member"]): item for item in file_records}
    selections: list[dict[str, object]] = [
        {
            "platform": "linux-64",
            "component_ref": "conda:native-linux",
            "binary_sha256": linux_hash,
            "source_members": ["sources/linux-conda.tar.zst"],
            "recipe_members": ["recipes/linux-recipe.yaml"],
            "notice_members": ["notices/linux-license.txt"],
            "basis": "recipe_declared_source",
        },
        {
            "platform": "linux-64",
            "component_ref": "pypi:vendor-linux",
            "binary_sha256": pypi_hash,
            "source_members": ["sources/pypi-empty.whl"],
            "recipe_members": [],
            "notice_members": [],
            "basis": "publisher_release_source",
        },
        {
            "platform": "win-64",
            "component_ref": "conda:native-win",
            "binary_sha256": windows_hash,
            "source_members": ["sources/windows-src.zip"],
            "recipe_members": [],
            "notice_members": [],
            "basis": "source_version_dependency",
        },
    ]
    assert set(file_by_member) == set(payloads)
    manifest: dict[str, object] = {
        "format": "carlina.documents-source-companion-input",
        "schema_version": 1,
        "application_profile": "documents",
        "application_version": "1.2.3.dev4",
        "source_revision": _REVISION,
        "wheel_sha256": _WHEEL_SHA,
        "inventory_inputs": [
            {"platform": platform, "sha256": _sha(path.read_bytes())}
            for platform, path in inventory_paths.items()
        ],
        "components": selections,
        "files": file_records,
    }
    case = _Case(
        root,
        root / "manifest.json",
        files_dir,
        inventory_paths,
        manifest,
        {"linux-64": linux, "win-64": windows},
        payloads,
    )
    case.write_manifest()
    return case


def _assemble(case: _Case, output: Path | None = None) -> dict[str, object]:
    return assembler.assemble_sources(
        case.manifest_path,
        case.files_dir,
        tuple(case.inventory_paths.values()),
        case.output() if output is None else output,
    )


def _assert_failure(case: _Case, output: Path | None = None) -> None:
    target = case.output() if output is None else output
    before = set(target.parent.iterdir())
    with pytest.raises(assembler.DocumentsSourceAssemblyError):
        _assemble(case, target)
    assert not target.exists()
    assert set(target.parent.iterdir()) == before


def test_two_platform_bundle_is_stored_deterministic_and_byte_exact(
    tmp_path: Path,
) -> None:
    case = _make_case(tmp_path)
    output = case.output()
    report = _assemble(case, output)

    assert report["source_closure_complete"] is False
    assert report["public_release_accepted"] is False
    assert report["component_count"] == 3
    assert report["file_count"] == 6
    assert report["payload_bytes"] == sum(map(len, case.payloads.values()))
    archive_bytes = output.read_bytes()
    assert report["archive_bytes"] == len(archive_bytes)
    assert report["archive_sha256"] == _sha(archive_bytes)

    with zipfile.ZipFile(output) as archive:
        expected_names = sorted(["index.json", *case.payloads])
        assert archive.namelist() == expected_names
        assert archive.comment == b""
        for info in archive.infolist():
            assert info.date_time == (1980, 1, 1, 0, 0, 0)
            assert info.compress_type == zipfile.ZIP_STORED
            assert info.extra == b""
            assert info.comment == b""
            assert info.external_attr >> 16 == stat.S_IFREG | 0o644
        for member, payload in case.payloads.items():
            assert archive.read(member) == payload
        index_bytes = archive.read("index.json")
        index = json.loads(index_bytes)
        assert hashlib.sha256(index_bytes).hexdigest() == report["index_sha256"]
        assert index["format"] == "carlina.documents-source-companion"
        assert index["coverage"] == "selected_components"
        assert index["source_closure_complete"] is False
        assert index["public_release_accepted"] is False
        assert [item["platform"] for item in index["inventory_inputs"]] == [
            "linux-64",
            "win-64",
        ]
        pypi = next(
            item
            for item in index["components"]
            if item["component_ref"] == "pypi:vendor-linux"
        )
        assert pypi["binary_sha256"] == "2" * 64
        assert pypi["purl"] == "pkg:pypi/vendor-wheel@2.0"
        assert pypi["declared_licenses"] == [{"license": {"name": "MIT"}}]
        assert str(tmp_path).encode() not in index_bytes

    assert not (case.root / "must-not-run").exists()

    reordered = dict(case.manifest)
    reordered["inventory_inputs"] = list(
        reversed(cast(list[object], reordered["inventory_inputs"]))
    )
    reordered["components"] = list(
        reversed(cast(list[object], reordered["components"]))
    )
    reordered["files"] = list(reversed(cast(list[object], reordered["files"])))
    alternate_manifest = case.root / "reordered.json"
    _write_json(alternate_manifest, reordered)
    second_output = case.output("second.zip")
    second = assembler.assemble_sources(
        alternate_manifest,
        case.files_dir,
        tuple(reversed(tuple(case.inventory_paths.values()))),
        second_output,
    )
    assert second["archive_sha256"] == report["archive_sha256"]
    assert second_output.read_bytes() == archive_bytes


def test_streamed_members_select_zip64_from_declared_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = _make_case(tmp_path)
    monkeypatch.setattr(zipfile, "ZIP64_LIMIT", 16)
    output = case.output()

    _assemble(case, output)

    with zipfile.ZipFile(output) as archive:
        assert archive.testzip() is None
        for member, payload in case.payloads.items():
            assert archive.read(member) == payload


@pytest.mark.parametrize(
    "field,value", [("source_revision", "d" * 40), ("wheel_sha256", "e" * 64)]
)
def test_manifest_must_bind_to_inventory_identity(
    tmp_path: Path, field: str, value: str
) -> None:
    case = _make_case(tmp_path)
    case.manifest[field] = value
    case.write_manifest()
    _assert_failure(case)


def test_inventory_digest_and_platform_are_bound(tmp_path: Path) -> None:
    case = _make_case(tmp_path)
    inputs = cast(list[dict[str, object]], case.manifest["inventory_inputs"])
    inputs[0]["sha256"] = "f" * 64
    case.write_manifest()
    _assert_failure(case)


@pytest.mark.parametrize("bad_field", ["component_ref", "binary_sha256"])
def test_component_selection_must_resolve_and_match_permitted_hash(
    tmp_path: Path, bad_field: str
) -> None:
    case = _make_case(tmp_path)
    selection = cast(list[dict[str, object]], case.manifest["components"])[0]
    selection[bad_field] = (
        "missing:component" if bad_field == "component_ref" else "f" * 64
    )
    case.write_manifest()
    _assert_failure(case)


def test_inventory_duplicate_refs_and_conflicting_hash_sources_fail_closed(
    tmp_path: Path,
) -> None:
    case = _make_case(tmp_path)
    report = case.inventories["linux-64"]
    components = cast(list[dict[str, object]], report["components"])
    components[1]["bom-ref"] = components[0]["bom-ref"]
    case.write_inventory("linux-64", report)
    inputs = cast(list[dict[str, object]], case.manifest["inventory_inputs"])
    linux_row = next(item for item in inputs if item["platform"] == "linux-64")
    linux_row["sha256"] = _sha(case.inventory_paths["linux-64"].read_bytes())
    case.write_manifest()
    _assert_failure(case)

    case = _make_case(tmp_path / "conflicting-hashes")
    report = case.inventories["linux-64"]
    first = cast(list[dict[str, object]], report["components"])[0]
    cast(list[dict[str, object]], first["properties"])[0]["value"] = json.dumps(
        ["f" * 64]
    )
    case.write_inventory("linux-64", report)
    inputs = cast(list[dict[str, object]], case.manifest["inventory_inputs"])
    linux_row = next(item for item in inputs if item["platform"] == "linux-64")
    linux_row["sha256"] = _sha(case.inventory_paths["linux-64"].read_bytes())
    case.write_manifest()
    _assert_failure(case)


@pytest.mark.parametrize(
    "raw",
    [
        b'{"format":"wrong","format":"duplicate"}',
        b'{"format":"carlina.documents-source-companion-input","schema_version":true}',
    ],
)
def test_duplicate_keys_and_boolean_schema_version_fail_closed(
    tmp_path: Path, raw: bytes
) -> None:
    case = _make_case(tmp_path)
    case.manifest_path.write_bytes(raw)
    _assert_failure(case)


def test_unknown_keys_and_inventory_duplicate_keys_fail_closed(tmp_path: Path) -> None:
    case = _make_case(tmp_path)
    case.manifest["unexpected"] = True
    case.write_manifest()
    _assert_failure(case)

    case = _make_case(tmp_path / "inventory-duplicate")
    case.inventory_paths["linux-64"].write_bytes(b'{"format":"x","format":"y"}')
    _assert_failure(case)


@pytest.mark.parametrize(
    "mutation",
    [
        "wrong-kind",
        "unreferenced",
        "empty-source-list",
        "no-instructions",
    ],
)
def test_component_file_closure_and_instructions_are_required(
    tmp_path: Path, mutation: str
) -> None:
    case = _make_case(tmp_path)
    files = cast(list[dict[str, object]], case.manifest["files"])
    selection = cast(list[dict[str, object]], case.manifest["components"])[0]
    if mutation == "wrong-kind":
        selection["source_members"] = ["recipes/linux-recipe.yaml"]
    elif mutation == "unreferenced":
        payload = b"unreferenced notice"
        digest = _sha(payload)
        (case.files_dir / digest).write_bytes(payload)
        files.append(
            {
                "member": "notices/unreferenced.txt",
                "kind": "notice",
                "sha256": digest,
                "bytes": len(payload),
                "origin_url": None,
                "origin_archive_sha256": None,
                "origin_member": None,
            }
        )
    elif mutation == "empty-source-list":
        selection["source_members"] = []
    else:
        case.manifest["files"] = [
            item for item in files if item["kind"] != "instructions"
        ]
    case.write_manifest()
    _assert_failure(case)


@pytest.mark.parametrize(
    "member",
    [
        "sources/../escape.tar",
        "/sources/absolute.tar",
        r"sources\backslash.tar",
        "sources/file:stream.tar",
        "sources/CON.txt",
        "sources/trailing./source",
        "sources//empty-segment.tar",
        "sources/./dot.tar",
        "sources/" + "x" * 81,
        "sources/" + "x" * 236,
    ],
)
def test_unsafe_member_paths_fail_before_output(tmp_path: Path, member: str) -> None:
    case = _make_case(tmp_path)
    records = cast(list[dict[str, object]], case.manifest["files"])
    row = dict(next(item for item in records if item["kind"] == "source"))
    row["member"] = member
    records.append(row)
    case.write_manifest()
    _assert_failure(case)


@pytest.mark.parametrize(
    "members",
    [
        ["sources/Same", "sources/same"],
        ["sources/a", "sources/a/child"],
        ["sources/dir/a", "sources/DIR/b"],
    ],
)
def test_casefold_and_file_directory_collisions_fail(
    tmp_path: Path, members: list[str]
) -> None:
    case = _make_case(tmp_path)
    rows = cast(list[dict[str, object]], case.manifest["files"])
    base = dict(next(item for item in rows if item["kind"] == "source"))
    for member in members:
        row = dict(base)
        row["member"] = member
        rows.append(row)
    case.write_manifest()
    _assert_failure(case)


@pytest.mark.parametrize(
    "field,value",
    [("bytes", True), ("bytes", -1), ("bytes", 999), ("sha256", "g" * 64)],
)
def test_invalid_sizes_hashes_and_file_bytes_fail(
    tmp_path: Path, field: str, value: object
) -> None:
    case = _make_case(tmp_path)
    row = next(
        item
        for item in cast(list[dict[str, object]], case.manifest["files"])
        if item["kind"] == "source"
    )
    row[field] = value
    case.write_manifest()
    _assert_failure(case)


def test_patched_payload_and_index_limits_are_enforced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = _make_case(tmp_path)
    monkeypatch.setattr(assembler, "_SOURCE_LIMIT", 1)
    _assert_failure(case)

    case = _make_case(tmp_path / "index-limit")
    monkeypatch.setattr(assembler, "_SOURCE_LIMIT", 2 * 1024 * 1024 * 1024)
    monkeypatch.setattr(assembler, "_TOTAL_PAYLOAD_LIMIT", 1)
    _assert_failure(case)

    case = _make_case(tmp_path / "manifest-limit")
    monkeypatch.setattr(assembler, "_TOTAL_PAYLOAD_LIMIT", 4 * 1024 * 1024 * 1024)
    monkeypatch.setattr(assembler, "_INDEX_LIMIT", 20)
    _assert_failure(case)


def test_component_file_and_other_payload_count_limits_are_enforced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = _make_case(tmp_path)
    monkeypatch.setattr(assembler, "_COMPONENT_LIMIT", 2)
    _assert_failure(case)

    case = _make_case(tmp_path / "file-count")
    monkeypatch.setattr(assembler, "_COMPONENT_LIMIT", 4096)
    monkeypatch.setattr(assembler, "_FILE_LIMIT", 5)
    _assert_failure(case)

    case = _make_case(tmp_path / "other-file-limit")
    monkeypatch.setattr(assembler, "_FILE_LIMIT", 16384)
    monkeypatch.setattr(assembler, "_OTHER_FILE_LIMIT", 1)
    _assert_failure(case)


@pytest.mark.parametrize(
    "origin_url",
    [
        "http://example.invalid/source.tar",
        "https://user:secret@example.invalid/source.tar",
        "https://example.invalid/source.tar?token=private",
        "https://example.invalid/source.tar?",
        "https://example.invalid/source.tar#fragment",
        "https://example.invalid/source.tar#",
    ],
)
def test_source_origin_url_must_be_clean_https(tmp_path: Path, origin_url: str) -> None:
    case = _make_case(tmp_path)
    row = next(
        item
        for item in cast(list[dict[str, object]], case.manifest["files"])
        if item["kind"] == "source"
    )
    row["origin_url"] = origin_url
    case.write_manifest()
    _assert_failure(case)


def test_recipe_provenance_fields_must_be_complete(tmp_path: Path) -> None:
    case = _make_case(tmp_path)
    recipe = next(
        item
        for item in cast(list[dict[str, object]], case.manifest["files"])
        if item["kind"] == "recipe"
    )
    recipe["origin_member"] = None
    case.write_manifest()
    _assert_failure(case)


def test_blob_hash_size_symlink_and_root_guards(tmp_path: Path) -> None:
    case = _make_case(tmp_path)
    source = next(
        item
        for item in cast(list[dict[str, object]], case.manifest["files"])
        if item["kind"] == "source"
    )
    blob = case.files_dir / str(source["sha256"])
    original = blob.read_bytes()
    blob.write_bytes(b"x" + original[1:])
    _assert_failure(case)

    case = _make_case(tmp_path / "size-mismatch")
    source = next(
        item
        for item in cast(list[dict[str, object]], case.manifest["files"])
        if item["kind"] == "source"
    )
    blob = case.files_dir / str(source["sha256"])
    blob.write_bytes(blob.read_bytes() + b"x")
    _assert_failure(case)

    case = _make_case(tmp_path / "symlink-leaf")
    source = next(
        item
        for item in cast(list[dict[str, object]], case.manifest["files"])
        if item["kind"] == "source"
    )
    blob = case.files_dir / str(source["sha256"])
    moved = blob.with_suffix(".held")
    blob.rename(moved)
    blob.symlink_to(moved.name)
    _assert_failure(case)

    case = _make_case(tmp_path / "symlink-root")
    actual = case.files_dir.with_name("actual-blobs")
    case.files_dir.rename(actual)
    case.files_dir.symlink_to(actual.name, target_is_directory=True)
    _assert_failure(case)


def test_manifest_inventory_and_output_symlinks_are_rejected(tmp_path: Path) -> None:
    case = _make_case(tmp_path)
    real_manifest = case.manifest_path.with_suffix(".real")
    case.manifest_path.rename(real_manifest)
    case.manifest_path.symlink_to(real_manifest.name)
    _assert_failure(case)

    case = _make_case(tmp_path / "inventory-link")
    inventory = case.inventory_paths["linux-64"]
    real_inventory = inventory.with_suffix(".real")
    inventory.rename(real_inventory)
    inventory.symlink_to(real_inventory.name)
    _assert_failure(case)

    case = _make_case(tmp_path / "output-link")
    target = case.output()
    sentinel = case.root / "sentinel"
    sentinel.write_bytes(b"do not replace")
    target.symlink_to(sentinel.name)
    before = set(case.root.iterdir())
    with pytest.raises(assembler.DocumentsSourceAssemblyError):
        _assemble(case, target)
    assert target.is_symlink()
    assert sentinel.read_bytes() == b"do not replace"
    assert set(case.root.iterdir()) == before


def test_output_parent_symlink_and_existing_output_are_preserved(
    tmp_path: Path,
) -> None:
    case = _make_case(tmp_path)
    real_parent = case.root / "real-parent"
    real_parent.mkdir()
    symlink_parent = case.root / "parent-link"
    symlink_parent.symlink_to(real_parent.name, target_is_directory=True)
    with pytest.raises(assembler.DocumentsSourceAssemblyError):
        _assemble(case, symlink_parent / "out.zip")
    assert list(real_parent.iterdir()) == []

    output = case.output()
    output.write_bytes(b"existing archive")
    with pytest.raises(assembler.DocumentsSourceAssemblyError):
        _assemble(case, output)
    assert output.read_bytes() == b"existing archive"


def test_mutated_blob_during_streaming_fails_and_cleans_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = _make_case(tmp_path)
    source = next(
        item
        for item in cast(list[dict[str, object]], case.manifest["files"])
        if item["kind"] == "source"
    )
    blob = case.files_dir / str(source["sha256"])
    original_read = os.read
    changed = False

    def mutate_after_read(descriptor: int, size: int) -> bytes:
        nonlocal changed
        result = original_read(descriptor, size)
        if not changed:
            changed = True
            blob.write_bytes(blob.read_bytes() + b"raced")
        return result

    monkeypatch.setattr(assembler.os, "read", mutate_after_read)
    before = set(case.root.iterdir())
    with pytest.raises(assembler.DocumentsSourceAssemblyError):
        _assemble(case)
    assert not case.output().exists()
    assert set(case.root.iterdir()) == before


def test_cli_redacts_failure_and_prints_only_bounded_counts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    case = _make_case(tmp_path)
    code = assembler.main(
        [
            "--manifest",
            str(case.manifest_path),
            "--files-dir",
            str(case.files_dir),
            "--inventory",
            str(case.inventory_paths["linux-64"]),
            "--inventory",
            str(case.inventory_paths["win-64"]),
            "--output",
            str(case.output()),
        ]
    )
    captured = capsys.readouterr()
    assert code == 0
    assert captured.err == ""
    assert "components=3" in captured.out
    assert "archive_bytes=" in captured.out
    assert str(tmp_path) not in captured.out

    broken = _make_case(tmp_path / "broken")
    broken.manifest["source_revision"] = "bad"
    broken.write_manifest()
    code = assembler.main(
        [
            "--manifest",
            str(broken.manifest_path),
            "--files-dir",
            str(broken.files_dir),
            "--inventory",
            str(broken.inventory_paths["linux-64"]),
            "--inventory",
            str(broken.inventory_paths["win-64"]),
            "--output",
            str(broken.output()),
        ]
    )
    captured = capsys.readouterr()
    assert code == 2
    assert captured.out == ""
    assert captured.err == assembler._ERROR + "\n"
    assert str(broken.root) not in captured.err
