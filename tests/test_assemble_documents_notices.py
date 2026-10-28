from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import cast
from urllib.parse import quote

import pytest

from scripts import assemble_documents_notices as assembler


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=True, sort_keys=True, indent=2) + "\n"
    ).encode()


def _save_json(path: Path, value: object) -> None:
    path.write_bytes(_json_bytes(value))


def _setup(tmp_path: Path) -> tuple[Path, Path, dict[str, object], dict[str, object]]:
    collection = tmp_path / "collection"
    supplement = tmp_path / "supplement"
    (collection / "files").mkdir(parents=True)
    (supplement / "files").mkdir(parents=True)

    inventory_sha = _sha256(b"synthetic release inventory")
    inventory = {
        "sha256": inventory_sha,
        "platform": "linux-64",
        "source_revision": "a" * 40,
        "application_version": "0.1.0.post204.dev4",
        "wheel_sha256": "b" * 64,
        "component_count": 6,
        "conda_component_count": 6,
    }

    payloads = {
        "wrapper_recipe": b"recipe notice\x00\xff",
        "header": b"embedded header\x00\xfe",
        "target_metadata": b'{"name":"sibling_target"}\n',
        "sibling_license": b"sibling notice\x00\x81",
        "ordinary_license": b"ordinary license\x00\xff",
        "ordinary_recipe_license": b"recipe-only license\xfe",
    }

    def member(path: str, kind: str, content: bytes) -> dict[str, object]:
        digest = _sha256(content)
        blob_path = collection / "files" / digest
        if blob_path.exists():
            assert blob_path.read_bytes() == content
        else:
            blob_path.write_bytes(content)
        return {
            "archive_path": path,
            "kind": kind,
            "sha256": digest,
            "bytes": len(content),
            "file": f"files/{digest}",
        }

    package_specs = [
        (
            "metadata_wrapper",
            [
                member(
                    "info/recipe/LICENSE.txt", "license", payloads["wrapper_recipe"]
                ),
                member("info/recipe/post-link.bat", "recipe", b""),
            ],
            0,
            1,
        ),
        (
            "embedded_header_target",
            [member("include/sqlite3.h", "metadata", payloads["header"])],
            0,
            1,
        ),
        (
            "sibling_notice_target",
            [member("info/about.json", "metadata", payloads["target_metadata"])],
            0,
            1,
        ),
        (
            "sibling_notice_source",
            [member("LICENSE", "license", payloads["sibling_license"])],
            1,
            1,
        ),
        (
            "ordinary_package",
            [
                member("LICENSE", "license", payloads["ordinary_license"]),
                member(
                    "info/recipe/LICENSE",
                    "license",
                    payloads["ordinary_recipe_license"],
                ),
            ],
            1,
            2,
        ),
    ]

    packages: list[dict[str, object]] = []
    for name, members, candidate_count, uses_count in package_specs:
        version = "1.0.0"
        build = f"{name}_0"
        archive_sha = _sha256(f"archive:{name}".encode())
        uses = [
            {
                "inventory_sha256": inventory_sha,
                "platform": "linux-64",
                "environment": "documents",
                "component_ref": f"{name}:context{number}",
            }
            for number in range(uses_count)
        ]
        packages.append(
            {
                "name": name,
                "version": version,
                "build": build,
                "subdir": "noarch",
                "archive_sha256": archive_sha,
                "archive_url": (
                    "https://conda.anaconda.org/conda-forge/noarch/"
                    f"{quote(name, safe='')}-{quote(version, safe='')}-"
                    f"{quote(build, safe='')}.conda"
                ),
                "uses": uses,
                "members": members,
                "package_license_candidate_count": candidate_count,
            }
        )

    gaps = [
        {
            "archive_sha256": package["archive_sha256"],
            "reason": "No package licence candidate was found in the locked archive.",
        }
        for package in packages
        if package["package_license_candidate_count"] == 0
    ]
    collection_report: dict[str, object] = {
        "format": "carlina.documents-conda-notices",
        "schema_version": 1,
        "application_profile": "documents",
        "application_version": inventory["application_version"],
        "source_revision": inventory["source_revision"],
        "wheel_sha256": inventory["wheel_sha256"],
        "basis": "SHA-256 verified locked Conda archives; retained bytes are unmodified",
        "inventory_inputs": [inventory],
        "archive_collection_complete": True,
        "notice_acceptance_complete": False,
        "public_release_accepted": False,
        "downloaded_bytes": 0,
        "archive_count": len(packages),
        "conda_component_count": 6,
        "packages": packages,
        "discovery_gaps": gaps,
        "required_follow_up": ["Review candidates."],
    }
    _save_json(collection / "index.json", collection_report)

    collection_sha = _sha256((collection / "index.json").read_bytes())
    gap_packages = [
        package
        for package in packages
        if package["package_license_candidate_count"] == 0
    ]
    decisions: list[dict[str, object]] = []
    embedded = cast(dict[str, object], gap_packages[1])
    embedded_notice = payloads["header"]
    embedded_hash = _sha256(embedded_notice)
    (supplement / "files" / embedded_hash).write_bytes(embedded_notice)
    decisions.append(
        {
            "archive_sha256": embedded["archive_sha256"],
            "basis": "embedded_header_notice",
            "explanation": "Header bytes were retained from the target archive.",
            "notices": [
                {
                    "file": f"files/{embedded_hash}",
                    "sha256": embedded_hash,
                    "bytes": len(embedded_notice),
                    "archive_path": "include/sqlite3.h",
                    "origin_archive_sha256": embedded["archive_sha256"],
                }
            ],
        }
    )
    sibling_target = cast(dict[str, object], gap_packages[2])
    sibling_source = next(
        package for package in packages if package["name"] == "sibling_notice_source"
    )
    sibling_notice = payloads["sibling_license"]
    sibling_hash = _sha256(sibling_notice)
    (supplement / "files" / sibling_hash).write_bytes(sibling_notice)
    decisions.append(
        {
            "archive_sha256": sibling_target["archive_sha256"],
            "basis": "same_source_sibling_notice",
            "explanation": "The retained sibling notice has matching archive provenance.",
            "notices": [
                {
                    "file": f"files/{sibling_hash}",
                    "sha256": sibling_hash,
                    "bytes": len(sibling_notice),
                    "archive_path": "LICENSE",
                    "origin_archive_sha256": sibling_source["archive_sha256"],
                }
            ],
        }
    )
    wrapper = cast(dict[str, object], gap_packages[0])
    decisions.append(
        {
            "archive_sha256": wrapper["archive_sha256"],
            "basis": "metadata_only_wrapper",
            "explanation": "The reviewed wrapper has no retained regular payload files.",
            "notices": [],
        }
    )
    supplement_report: dict[str, object] = {
        "format": "carlina.documents-conda-notice-supplement",
        "schema_version": 1,
        "collection_index_sha256": collection_sha,
        "decisions": decisions,
    }
    _save_json(supplement / "manifest.json", supplement_report)
    return collection, supplement, collection_report, supplement_report


def _reload(path: Path) -> dict[str, object]:
    return cast(dict[str, object], json.loads(path.read_bytes()))


def _write_supplement(supplement: Path, value: dict[str, object]) -> None:
    _save_json(supplement / "manifest.json", value)


def _mutate_decisions(
    supplement: Path, mutate: Callable[[list[dict[str, object]]], None]
) -> None:
    manifest = _reload(supplement / "manifest.json")
    decisions = cast(list[dict[str, object]], manifest["decisions"])
    mutate(decisions)
    _write_supplement(supplement, manifest)


def _assert_no_output_failure(collection: Path, supplement: Path, output: Path) -> None:
    with pytest.raises(assembler.NoticeAssemblyError):
        assembler.assemble_notices(collection, supplement, output)
    assert not output.exists()


def test_happy_bundle_is_byte_preserving_deduplicated_and_deterministic(
    tmp_path: Path,
) -> None:
    collection, supplement, _collection_report, _supplement_report = _setup(tmp_path)
    first = tmp_path / "first"
    second = tmp_path / "second"

    result = assembler.assemble_notices(collection, supplement, first)
    repeated = assembler.assemble_notices(collection, supplement, second)

    assert result == repeated
    assert (first / "index.json").read_bytes() == (second / "index.json").read_bytes()
    assert result["format"] == "carlina.documents-conda-notice-bundle"
    assert result["archive_count"] == 5
    assert result["notice_file_count"] == 5
    assert result["discovery_gaps_accounted_for"] is True
    assert result["notice_acceptance_complete"] is False
    assert result["public_release_accepted"] is False

    first_index = _reload(first / "index.json")
    packages = cast(list[dict[str, object]], first_index["packages"])
    assert [item["archive_sha256"] for item in packages] == sorted(
        cast(str, item["archive_sha256"]) for item in packages
    )
    decisions = {
        cast(dict[str, object], package["gap_decision"])["basis"]
        for package in packages
        if package["gap_decision"] is not None
    }
    assert decisions == {
        "metadata_only_wrapper",
        "embedded_header_notice",
        "same_source_sibling_notice",
    }
    assert (
        len(
            cast(
                list[object],
                next(p for p in packages if p["name"] == "ordinary_package")["uses"],
            )
        )
        == 2
    )

    original_collection = _reload(collection / "index.json")
    all_member_blobs = {
        cast(str, member["sha256"]): (
            collection / cast(str, member["file"])
        ).read_bytes()
        for package in cast(list[dict[str, object]], original_collection["packages"])
        for member in cast(list[dict[str, object]], package["members"])
        if member["kind"] == "license"
    }
    supplement_manifest = _reload(supplement / "manifest.json")
    for decision in cast(list[dict[str, object]], supplement_manifest["decisions"]):
        for notice in cast(list[dict[str, object]], decision["notices"]):
            digest = cast(str, notice["sha256"])
            all_member_blobs[digest] = (
                supplement / cast(str, notice["file"])
            ).read_bytes()
    for digest, content in all_member_blobs.items():
        assert (first / "files" / digest).read_bytes() == content
        assert (second / "files" / digest).read_bytes() == content
    assert b"\x00" in (first / "files" / _sha256(b"recipe notice\x00\xff")).read_bytes()
    assert (
        b"\xff" in (first / "files" / _sha256(b"ordinary license\x00\xff")).read_bytes()
    )
    assert not (first / "files" / _sha256(b"")).exists()
    assert cast(int, result["total_notice_bytes"]) == sum(
        len(content) for content in all_member_blobs.values()
    )


def test_stale_collection_digest_fails_before_output(tmp_path: Path) -> None:
    collection, supplement, _collection_report, _supplement_report = _setup(tmp_path)
    manifest = _reload(supplement / "manifest.json")
    manifest["collection_index_sha256"] = "c" * 64
    _write_supplement(supplement, manifest)
    _assert_no_output_failure(collection, supplement, tmp_path / "output")


@pytest.mark.parametrize("tree_name", ["collection", "supplement"])
def test_tampered_referenced_bytes_fail_before_output(
    tmp_path: Path, tree_name: str
) -> None:
    collection, supplement, collection_report, supplement_report = _setup(tmp_path)
    if tree_name == "collection":
        package = next(
            item
            for item in cast(list[dict[str, object]], collection_report["packages"])
            if item["name"] == "embedded_header_target"
        )
        member = cast(dict[str, object], cast(list[object], package["members"])[0])
        leaf = collection / cast(str, member["file"])
    else:
        decision = next(
            item
            for item in cast(list[dict[str, object]], supplement_report["decisions"])
            if item["basis"] == "embedded_header_notice"
        )
        notice = cast(dict[str, object], cast(list[object], decision["notices"])[0])
        leaf = supplement / cast(str, notice["file"])
    original = leaf.read_bytes()
    leaf.write_bytes(bytes([original[0] ^ 1]) + original[1:])
    _assert_no_output_failure(collection, supplement, tmp_path / "output")


def test_zero_byte_recipe_member_is_accepted_and_verified(tmp_path: Path) -> None:
    collection, supplement, collection_report, _supplement_report = _setup(tmp_path)
    package = next(
        item
        for item in cast(list[dict[str, object]], collection_report["packages"])
        if item["name"] == "metadata_wrapper"
    )
    recipe = next(
        member
        for member in cast(list[dict[str, object]], package["members"])
        if member["kind"] == "recipe"
    )
    assert recipe["bytes"] == 0
    assert (collection / cast(str, recipe["file"])).read_bytes() == b""
    assembler.assemble_notices(collection, supplement, tmp_path / "output")


@pytest.mark.parametrize("mutation", ["unknown", "duplicate", "missing", "extra"])
def test_gap_decision_set_must_match_exactly(tmp_path: Path, mutation: str) -> None:
    collection, supplement, _collection_report, _supplement_report = _setup(tmp_path)

    def change(decisions: list[dict[str, object]]) -> None:
        if mutation == "unknown":
            decisions[0]["archive_sha256"] = "c" * 64
        elif mutation == "duplicate":
            decisions.append(dict(decisions[0]))
        elif mutation == "missing":
            decisions.pop()
        else:
            decisions.append(
                {
                    "archive_sha256": "c" * 64,
                    "basis": "metadata_only_wrapper",
                    "explanation": "Reviewed wrapper.",
                    "notices": [],
                }
            )

    _mutate_decisions(supplement, change)
    _assert_no_output_failure(collection, supplement, tmp_path / "output")


@pytest.mark.parametrize("mutation", ["absent_origin", "member_mismatch"])
def test_sibling_source_requires_exact_retained_license_member(
    tmp_path: Path, mutation: str
) -> None:
    collection, supplement, collection_report, _supplement_report = _setup(tmp_path)
    source = next(
        package
        for package in cast(list[dict[str, object]], collection_report["packages"])
        if package["name"] == "sibling_notice_source"
    )
    source_sha = cast(str, source["archive_sha256"])

    def change(decisions: list[dict[str, object]]) -> None:
        decision = next(
            item for item in decisions if item["basis"] == "same_source_sibling_notice"
        )
        notice = cast(dict[str, object], cast(list[object], decision["notices"])[0])
        if mutation == "absent_origin":
            notice["origin_archive_sha256"] = "c" * 64
        else:
            notice["archive_path"] = "COPYING"
        assert (
            notice["origin_archive_sha256"] == source_sha or mutation == "absent_origin"
        )

    _mutate_decisions(supplement, change)
    _assert_no_output_failure(collection, supplement, tmp_path / "output")


def test_wrapper_decision_cannot_attach_notices(tmp_path: Path) -> None:
    collection, supplement, _collection_report, _supplement_report = _setup(tmp_path)

    def change(decisions: list[dict[str, object]]) -> None:
        decision = next(
            item for item in decisions if item["basis"] == "metadata_only_wrapper"
        )
        first_notices = cast(list[object], decisions[0]["notices"])
        decision["notices"] = [first_notices[0]]

    _mutate_decisions(supplement, change)
    _assert_no_output_failure(collection, supplement, tmp_path / "output")


def test_embedded_header_origin_must_be_the_target_package(tmp_path: Path) -> None:
    collection, supplement, _collection_report, _supplement_report = _setup(tmp_path)

    def change(decisions: list[dict[str, object]]) -> None:
        decision = next(
            item for item in decisions if item["basis"] == "embedded_header_notice"
        )
        notice = cast(dict[str, object], cast(list[object], decision["notices"])[0])
        notice["origin_archive_sha256"] = "c" * 64

    _mutate_decisions(supplement, change)
    _assert_no_output_failure(collection, supplement, tmp_path / "output")


@pytest.mark.parametrize(
    ("source", "value"),
    [
        ("member", True),
        ("member", 0),
        ("member", -1),
        ("member", 16 * 1024 * 1024 + 1),
        ("supplement", True),
        ("supplement", 0),
        ("supplement", -1),
        ("supplement", 16 * 1024 * 1024 + 1),
    ],
)
def test_invalid_notice_sizes_fail_closed(
    tmp_path: Path, source: str, value: object
) -> None:
    collection, supplement, collection_report, _supplement_report = _setup(tmp_path)
    if source == "member":
        package = next(
            item
            for item in cast(list[dict[str, object]], collection_report["packages"])
            if item["name"] == "ordinary_package"
        )
        cast(dict[str, object], cast(list[object], package["members"])[0])["bytes"] = (
            value
        )
        _save_json(collection / "index.json", collection_report)
        # The bound supplement must follow the exact updated collection bytes.
        manifest = _reload(supplement / "manifest.json")
        manifest["collection_index_sha256"] = _sha256(
            (collection / "index.json").read_bytes()
        )
        _write_supplement(supplement, manifest)
    else:

        def change(decisions: list[dict[str, object]]) -> None:
            decision = next(
                item for item in decisions if item["basis"] == "embedded_header_notice"
            )
            cast(dict[str, object], cast(list[object], decision["notices"])[0])[
                "bytes"
            ] = value

        _mutate_decisions(supplement, change)
    _assert_no_output_failure(collection, supplement, tmp_path / "output")


@pytest.mark.parametrize("kind", ["metadata", "recipe"])
def test_metadata_and_recipe_members_have_two_megabyte_cap(
    tmp_path: Path, kind: str
) -> None:
    collection, supplement, collection_report, _supplement_report = _setup(tmp_path)
    target_package = (
        "embedded_header_target" if kind == "metadata" else "metadata_wrapper"
    )
    package = next(
        item
        for item in cast(list[dict[str, object]], collection_report["packages"])
        if item["name"] == target_package
    )
    member = next(
        item
        for item in cast(list[dict[str, object]], package["members"])
        if item["kind"] == kind
    )
    member["bytes"] = 2 * 1024 * 1024 + 1
    _save_json(collection / "index.json", collection_report)
    manifest = _reload(supplement / "manifest.json")
    manifest["collection_index_sha256"] = _sha256(
        (collection / "index.json").read_bytes()
    )
    _write_supplement(supplement, manifest)
    _assert_no_output_failure(collection, supplement, tmp_path / "output")


@pytest.mark.parametrize("tree_name", ["collection", "supplement"])
def test_declared_blob_size_must_match_bytes(tmp_path: Path, tree_name: str) -> None:
    collection, supplement, collection_report, _supplement_report = _setup(tmp_path)
    if tree_name == "collection":
        package = next(
            item
            for item in cast(list[dict[str, object]], collection_report["packages"])
            if item["name"] == "ordinary_package"
        )
        member = cast(dict[str, object], cast(list[object], package["members"])[0])
        member["bytes"] = cast(int, member["bytes"]) + 1
        _save_json(collection / "index.json", collection_report)
        manifest = _reload(supplement / "manifest.json")
        manifest["collection_index_sha256"] = _sha256(
            (collection / "index.json").read_bytes()
        )
        _write_supplement(supplement, manifest)
    else:

        def change(decisions: list[dict[str, object]]) -> None:
            decision = next(
                item for item in decisions if item["basis"] == "embedded_header_notice"
            )
            notice = cast(dict[str, object], cast(list[object], decision["notices"])[0])
            notice["bytes"] = cast(int, notice["bytes"]) + 1

        _mutate_decisions(supplement, change)
    _assert_no_output_failure(collection, supplement, tmp_path / "output")


@pytest.mark.parametrize(
    "mutation", ["collection_count", "inventory_count", "use_count", "duplicate_ref"]
)
def test_collection_use_and_inventory_counts_must_agree(
    tmp_path: Path, mutation: str
) -> None:
    collection, supplement, collection_report, _supplement_report = _setup(tmp_path)
    if mutation == "collection_count":
        collection_report["conda_component_count"] = 5
    elif mutation == "inventory_count":
        inventory = cast(
            dict[str, object],
            cast(list[object], collection_report["inventory_inputs"])[0],
        )
        inventory["conda_component_count"] = 5
    elif mutation == "use_count":
        package = next(
            item
            for item in cast(list[dict[str, object]], collection_report["packages"])
            if item["name"] == "ordinary_package"
        )
        cast(list[object], package["uses"]).pop()
    else:
        package = next(
            item
            for item in cast(list[dict[str, object]], collection_report["packages"])
            if item["name"] == "ordinary_package"
        )
        uses = cast(list[dict[str, object]], package["uses"])
        uses[1]["component_ref"] = uses[0]["component_ref"]
    _save_json(collection / "index.json", collection_report)
    manifest = _reload(supplement / "manifest.json")
    manifest["collection_index_sha256"] = _sha256(
        (collection / "index.json").read_bytes()
    )
    _write_supplement(supplement, manifest)
    _assert_no_output_failure(collection, supplement, tmp_path / "output")


@pytest.mark.parametrize(
    "mutation",
    [
        "archive_count",
        "candidate_count",
        "missing_gap",
        "extra_gap",
        "duplicate_archive_sha",
        "duplicate_identity",
    ],
)
def test_collection_package_counts_gaps_and_identities_are_recomputed(
    tmp_path: Path, mutation: str
) -> None:
    collection, supplement, collection_report, _supplement_report = _setup(tmp_path)
    packages = cast(list[dict[str, object]], collection_report["packages"])
    if mutation == "archive_count":
        collection_report["archive_count"] = 4
    elif mutation == "candidate_count":
        package = next(item for item in packages if item["name"] == "ordinary_package")
        package["package_license_candidate_count"] = 2
    elif mutation == "missing_gap":
        cast(list[object], collection_report["discovery_gaps"]).pop()
    elif mutation == "extra_gap":
        cast(list[object], collection_report["discovery_gaps"]).append(
            {"archive_sha256": "c" * 64, "reason": "extra gap"}
        )
    elif mutation == "duplicate_archive_sha":
        packages[1]["archive_sha256"] = packages[0]["archive_sha256"]
    else:
        packages[1]["name"] = packages[0]["name"]
        packages[1]["version"] = packages[0]["version"]
        packages[1]["build"] = packages[0]["build"]
        packages[1]["subdir"] = packages[0]["subdir"]
    _save_json(collection / "index.json", collection_report)
    manifest = _reload(supplement / "manifest.json")
    manifest["collection_index_sha256"] = _sha256(
        (collection / "index.json").read_bytes()
    )
    _write_supplement(supplement, manifest)
    _assert_no_output_failure(collection, supplement, tmp_path / "output")


def test_unique_referenced_bytes_are_capped_before_blob_reads(tmp_path: Path) -> None:
    collection, supplement, collection_report, _supplement_report = _setup(tmp_path)
    package = next(
        item
        for item in cast(list[dict[str, object]], collection_report["packages"])
        if item["name"] == "ordinary_package"
    )
    members = cast(list[dict[str, object]], package["members"])
    for number in range(17):
        digest = _sha256(f"not-present-{number}".encode())
        members.append(
            {
                "archive_path": f"info/recipe/LICENSE.extra{number}",
                "kind": "license",
                "sha256": digest,
                "bytes": 16 * 1024 * 1024,
                "file": f"files/{digest}",
            }
        )
    _save_json(collection / "index.json", collection_report)
    manifest = _reload(supplement / "manifest.json")
    manifest["collection_index_sha256"] = _sha256(
        (collection / "index.json").read_bytes()
    )
    _write_supplement(supplement, manifest)
    _assert_no_output_failure(collection, supplement, tmp_path / "output")


@pytest.mark.parametrize(
    "unsafe",
    [
        "../LICENSE",
        "/LICENSE",
        "C:/LICENSE",
        "a//LICENSE",
        "a/./LICENSE",
        "a/../LICENSE",
        "a\\LICENSE",
    ],
)
def test_unsafe_collection_member_path_is_rejected(tmp_path: Path, unsafe: str) -> None:
    collection, supplement, collection_report, _supplement_report = _setup(tmp_path)
    package = next(
        item
        for item in cast(list[dict[str, object]], collection_report["packages"])
        if item["name"] == "ordinary_package"
    )
    cast(dict[str, object], cast(list[object], package["members"])[0])[
        "archive_path"
    ] = unsafe
    _save_json(collection / "index.json", collection_report)
    manifest = _reload(supplement / "manifest.json")
    manifest["collection_index_sha256"] = _sha256(
        (collection / "index.json").read_bytes()
    )
    _write_supplement(supplement, manifest)
    _assert_no_output_failure(collection, supplement, tmp_path / "output")


@pytest.mark.parametrize("field", ["collection_file", "supplement_file"])
def test_blob_file_reference_must_be_hash_path(tmp_path: Path, field: str) -> None:
    collection, supplement, collection_report, _supplement_report = _setup(tmp_path)
    if field == "collection_file":
        package = next(
            item
            for item in cast(list[dict[str, object]], collection_report["packages"])
            if item["name"] == "ordinary_package"
        )
        cast(dict[str, object], cast(list[object], package["members"])[0])["file"] = (
            "files/../secret"
        )
        _save_json(collection / "index.json", collection_report)
        manifest = _reload(supplement / "manifest.json")
        manifest["collection_index_sha256"] = _sha256(
            (collection / "index.json").read_bytes()
        )
        _write_supplement(supplement, manifest)
    else:

        def change(decisions: list[dict[str, object]]) -> None:
            decision = next(
                item for item in decisions if item["basis"] == "embedded_header_notice"
            )
            cast(dict[str, object], cast(list[object], decision["notices"])[0])[
                "file"
            ] = "files/../secret"

        _mutate_decisions(supplement, change)
    _assert_no_output_failure(collection, supplement, tmp_path / "output")


def test_unsafe_supplement_archive_path_is_rejected(tmp_path: Path) -> None:
    collection, supplement, _collection_report, _supplement_report = _setup(tmp_path)

    def change(decisions: list[dict[str, object]]) -> None:
        decision = next(
            item for item in decisions if item["basis"] == "embedded_header_notice"
        )
        cast(dict[str, object], cast(list[object], decision["notices"])[0])[
            "archive_path"
        ] = "../header.h"

    _mutate_decisions(supplement, change)
    _assert_no_output_failure(collection, supplement, tmp_path / "output")


@pytest.mark.parametrize("tree_name", ["collection", "supplement"])
@pytest.mark.parametrize("target", ["blob", "files_directory"])
def test_input_symlink_leaves_and_files_directories_are_rejected(
    tmp_path: Path, tree_name: str, target: str
) -> None:
    collection, supplement, collection_report, supplement_report = _setup(tmp_path)
    tree = collection if tree_name == "collection" else supplement
    if target == "files_directory":
        files_dir = tree / "files"
        moved = tree / "real-files"
        files_dir.rename(moved)
        files_dir.symlink_to(moved, target_is_directory=True)
    elif tree_name == "collection":
        package = cast(list[dict[str, object]], collection_report["packages"])[0]
        member = cast(dict[str, object], cast(list[object], package["members"])[0])
        leaf = tree / cast(str, member["file"])
        external = tmp_path / f"external-{tree_name}"
        external.write_bytes(leaf.read_bytes())
        leaf.unlink()
        leaf.symlink_to(external)
    else:
        decision = next(
            item
            for item in cast(list[dict[str, object]], supplement_report["decisions"])
            if item["notices"]
        )
        notice = cast(dict[str, object], cast(list[object], decision["notices"])[0])
        leaf = tree / cast(str, notice["file"])
        external = tmp_path / f"external-{tree_name}"
        external.write_bytes(leaf.read_bytes())
        leaf.unlink()
        leaf.symlink_to(external)
    _assert_no_output_failure(collection, supplement, tmp_path / "output")


def test_existing_output_is_preserved(tmp_path: Path) -> None:
    collection, supplement, _collection_report, _supplement_report = _setup(tmp_path)
    output = tmp_path / "output"
    output.mkdir()
    witness = output / "keep.bin"
    witness.write_bytes(b"do not replace")
    with pytest.raises(assembler.NoticeAssemblyError):
        assembler.assemble_notices(collection, supplement, output)
    assert witness.read_bytes() == b"do not replace"
    assert sorted(path.name for path in output.iterdir()) == ["keep.bin"]


def test_unsupported_directory_io_fails_before_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    collection, supplement, _collection_report, _supplement_report = _setup(tmp_path)
    monkeypatch.setattr(assembler.os, "supports_dir_fd", set())
    _assert_no_output_failure(collection, supplement, tmp_path / "output")


@pytest.mark.parametrize("tree_name", ["collection", "supplement"])
def test_duplicate_json_keys_are_rejected(tmp_path: Path, tree_name: str) -> None:
    collection, supplement, _collection_report, _supplement_report = _setup(tmp_path)
    tree = collection if tree_name == "collection" else supplement
    manifest_name = "index.json" if tree_name == "collection" else "manifest.json"
    path = tree / manifest_name
    content = path.read_bytes()
    path.write_bytes(
        content.replace(b'"format":', b'"format":"duplicate","format":', 1)
    )
    _assert_no_output_failure(collection, supplement, tmp_path / "output")


def test_cli_redacts_failure_and_exits_two(tmp_path: Path) -> None:
    command = [
        sys.executable,
        str(Path(assembler.__file__)),
        "--collection-dir",
        "/private/collection/path",
        "--supplement-dir",
        "/private/supplement/path",
        "--output-dir",
        str(tmp_path / "output"),
    ]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == assembler._ERROR + "\n"
    assert "/private/" not in completed.stderr
