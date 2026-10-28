from __future__ import annotations

import hashlib
import stat
from pathlib import Path
from typing import cast

import pytest
import yaml

from scripts.documents_dependency_inventory import (
    InventoryError,
    load_pixi_lock,
    load_requirements_lock,
)

_HASH_A = "a" * 64
_HASH_B = "b" * 64
_HASH_C = "c" * 64
_CONDA_LOCATOR = "https://conda.example.invalid/linux-64/shared-1.0-h0.conda"
_PYPI_LOCATOR = "https://files.example.invalid/shared-1.0-py3-none-any.whl"


def _record(ecosystem: str, locator: str, **fields: object) -> dict[str, object]:
    return {ecosystem: locator, **fields}


def _pixi_payload(
    references: list[object],
    packages: list[object],
    *,
    environment: str = "documents",
    platform: str = "linux-64",
) -> dict[str, object]:
    return {
        "version": 7,
        "platforms": [{"name": platform}],
        "environments": {
            environment: {"packages": {platform: references}},
        },
        "packages": packages,
    }


def _write_pixi(tmp_path: Path, payload: object) -> Path:
    path = tmp_path / "pixi.lock"
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


def _write_requirements(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "requirements.lock"
    path.write_text(content, encoding="utf-8")
    return path


def _pip_line(name: str, version: str, *hashes: str) -> str:
    options = " ".join(f"--hash=sha256:{value}" for value in hashes)
    return f"{name}=={version} {options}".rstrip()


def test_pixi_lock_uses_only_selected_closure_and_preserves_fields(
    tmp_path: Path,
) -> None:
    unrelated = _record(
        "conda",
        "https://conda.example.invalid/linux-64/unselected.conda",
        name=17,
        version=[],
        sha256={"not": "a hash"},
    )
    conda_record = _record(
        "conda",
        _CONDA_LOCATOR,
        name="shared",
        version="1.0",
        sha256=_HASH_A,
        license="BSD-3-Clause",
        build="h0",
        subdir="linux-64",
    )
    pypi_record = _record(
        "pypi",
        _PYPI_LOCATOR,
        name="shared",
        version="1.0",
        sha256=_HASH_B,
    )
    path = _write_pixi(
        tmp_path,
        _pixi_payload(
            [{"pypi": _PYPI_LOCATOR}, {"conda": _CONDA_LOCATOR}],
            [unrelated, pypi_record, conda_record],
        ),
    )
    source_bytes = path.read_bytes()

    inventory = load_pixi_lock(path, environment="documents", platform="linux-64")

    assert inventory.lock_sha256 == hashlib.sha256(source_bytes).hexdigest()
    assert [
        (package.ecosystem, package.name, package.version)
        for package in inventory.packages
    ] == [
        ("conda", "shared", "1.0"),
        ("pypi", "shared", "1.0"),
    ]
    conda_package, pypi_package = inventory.packages
    assert conda_package.sha256 == (_HASH_A,)
    assert conda_package.license_expression == "BSD-3-Clause"
    assert conda_package.build == "h0"
    assert conda_package.subdir == "linux-64"
    assert pypi_package.sha256 == (_HASH_B,)
    assert not conda_package.local
    assert not pypi_package.local


@pytest.mark.parametrize(
    ("locator", "platform", "expected"),
    [
        (
            "https://conda.example.invalid/conda/linux-64/shared-1.0-h0.conda",
            "linux-64",
            ("shared", "1.0", "h0", "linux-64"),
        ),
        (
            "https://conda.example.invalid/conda/noarch/my-conda-package-2.4.1-h7_1.tar.bz2",
            "linux-64",
            ("my-conda-package", "2.4.1", "h7_1", "noarch"),
        ),
        (
            "https://conda.example.invalid/conda/win-64/_leading-package-3.0-b4.conda",
            "win-64",
            ("_leading-package", "3.0", "b4", "win-64"),
        ),
    ],
)
def test_conda_archive_locator_supplies_compact_identity(
    tmp_path: Path,
    locator: str,
    platform: str,
    expected: tuple[str, str, str, str],
) -> None:
    payload = _pixi_payload(
        [{"conda": locator}], [_record("conda", locator)], platform=platform
    )
    path = _write_pixi(tmp_path, payload)

    package = load_pixi_lock(path, environment="documents", platform=platform).packages[
        0
    ]

    assert (package.name, package.version, package.build, package.subdir) == expected


@pytest.mark.parametrize(
    ("field", "conflicting_value"),
    [
        ("name", "different"),
        ("version", "2.0"),
        ("build", "h9"),
        ("subdir", "noarch"),
    ],
)
def test_conda_record_fields_must_match_archive_identity(
    tmp_path: Path, field: str, conflicting_value: str
) -> None:
    record = _record("conda", _CONDA_LOCATOR)
    record[field] = conflicting_value
    path = _write_pixi(
        tmp_path,
        _pixi_payload([{"conda": _CONDA_LOCATOR}], [record]),
    )

    with pytest.raises(InventoryError):
        load_pixi_lock(path, environment="documents", platform="linux-64")


@pytest.mark.parametrize(
    ("locator", "platform"),
    [
        (
            "https://conda.example.invalid/conda/linux-64/pkg-1.0-h0.whl",
            "linux-64",
        ),
        (
            "https://conda.example.invalid/conda/linux-64/pkg-1.0.conda",
            "linux-64",
        ),
        (
            "https://conda.example.invalid/conda/linux-64/pkg--h0.conda",
            "linux-64",
        ),
        (
            "https://conda.example.invalid/conda/linux-64/pkg-1.0%2Fother-h0.conda",
            "linux-64",
        ),
        (
            "https://conda.example.invalid/conda/win-64/pkg-1.0-h0.conda",
            "linux-64",
        ),
        (
            "https://conda.example.invalid/conda/linux-64/Pkg-1.0-h0.conda",
            "linux-64",
        ),
        (
            "https://conda.example.invalid/conda/linux-64/-pkg-1.0-h0.conda",
            "linux-64",
        ),
        (
            "https://conda.example.invalid/conda/linux-64/.pkg-1.0-h0.conda",
            "linux-64",
        ),
        (
            "https://conda.example.invalid/conda/linux-64/pkg-1.0-h0.conda",
            "win-64",
        ),
    ],
)
def test_conda_locator_rejects_malformed_identity_and_subdir(
    tmp_path: Path, locator: str, platform: str
) -> None:
    path = _write_pixi(
        tmp_path,
        _pixi_payload(
            [{"conda": locator}], [_record("conda", locator)], platform=platform
        ),
    )

    with pytest.raises(InventoryError) as caught:
        load_pixi_lock(path, environment="documents", platform=platform)

    assert locator not in str(caught.value)


@pytest.mark.parametrize(
    "records",
    [
        [],
        [
            _record("conda", _CONDA_LOCATOR, name="x", version="1.0"),
            _record("conda", _CONDA_LOCATOR, name="x", version="1.0"),
        ],
    ],
)
def test_pixi_lock_rejects_missing_or_ambiguous_global_match(
    tmp_path: Path, records: list[object]
) -> None:
    path = _write_pixi(
        tmp_path,
        _pixi_payload([{"conda": _CONDA_LOCATOR}], records),
    )

    with pytest.raises(InventoryError) as caught:
        load_pixi_lock(path, environment="documents", platform="linux-64")

    assert _CONDA_LOCATOR not in str(caught.value)


def test_pixi_lock_rejects_duplicate_selected_references(tmp_path: Path) -> None:
    record = _record(
        "conda", _CONDA_LOCATOR, name="shared", version="1.0", sha256=_HASH_A
    )
    path = _write_pixi(
        tmp_path,
        _pixi_payload([{"conda": _CONDA_LOCATOR}, {"conda": _CONDA_LOCATOR}], [record]),
    )

    with pytest.raises(InventoryError):
        load_pixi_lock(path, environment="documents", platform="linux-64")


def test_pixi_lock_rejects_unavailable_environment_and_platform(
    tmp_path: Path,
) -> None:
    record = _record(
        "conda", _CONDA_LOCATOR, name="shared", version="1.0", sha256=_HASH_A
    )
    path = _write_pixi(
        tmp_path,
        _pixi_payload([{"conda": _CONDA_LOCATOR}], [record]),
    )

    with pytest.raises(InventoryError):
        load_pixi_lock(path, environment="missing", platform="linux-64")
    with pytest.raises(InventoryError):
        load_pixi_lock(path, environment="documents", platform="win-64")


def test_pixi_lock_rejects_platform_missing_from_selected_environment(
    tmp_path: Path,
) -> None:
    payload = _pixi_payload([], [])
    environments = cast(dict[str, object], payload["environments"])
    selected_environment = cast(dict[str, object], environments["documents"])
    selected_environment["packages"] = {}
    path = _write_pixi(tmp_path, payload)

    with pytest.raises(InventoryError):
        load_pixi_lock(path, environment="documents", platform="linux-64")


@pytest.mark.parametrize(
    ("locator", "version_state", "expected_version"),
    [
        (".", "declared", "0.1.0"),
        ("./", "absent", None),
        ("./", "null", None),
    ],
)
def test_local_pixi_pypi_ref_retains_reconciliation_metadata_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    locator: str,
    version_state: str,
    expected_version: str | None,
) -> None:
    monkeypatch.chdir(tmp_path)
    record: dict[str, object] = {"pypi": locator, "name": "carlina"}
    if version_state == "declared":
        record["version"] = "0.1.0"
    elif version_state == "null":
        record["version"] = None
    path = _write_pixi(
        tmp_path,
        _pixi_payload([{"pypi": locator}], [record]),
    )

    package = load_pixi_lock(
        path, environment="documents", platform="linux-64"
    ).packages[0]

    assert package.name == "carlina"
    assert package.version == expected_version
    assert package.local
    assert package.locator is None


def test_local_pixi_record_requires_name_and_never_infers_version(
    tmp_path: Path,
) -> None:
    path = _write_pixi(
        tmp_path,
        _pixi_payload([{"pypi": "./"}], [{"pypi": "./", "version": "9.9"}]),
    )

    with pytest.raises(InventoryError):
        load_pixi_lock(path, environment="documents", platform="linux-64")


def test_https_pixi_record_requires_version(tmp_path: Path) -> None:
    path = _write_pixi(
        tmp_path,
        _pixi_payload(
            [{"pypi": _PYPI_LOCATOR}],
            [_record("pypi", _PYPI_LOCATOR, name="shared", sha256=_HASH_A)],
        ),
    )

    with pytest.raises(InventoryError):
        load_pixi_lock(path, environment="documents", platform="linux-64")


def test_pixi_pypi_names_use_the_same_identity_as_requirements(tmp_path: Path) -> None:
    path = _write_pixi(
        tmp_path,
        _pixi_payload(
            [{"pypi": _PYPI_LOCATOR}],
            [_record("pypi", _PYPI_LOCATOR, name="Shared_Pkg", version="1.0")],
        ),
    )
    requirement = _write_requirements(tmp_path, _pip_line("shared.pkg", "1.0", _HASH_A))
    pixi_package = load_pixi_lock(
        path, environment="documents", platform="linux-64"
    ).packages[0]
    assert pixi_package.name == load_requirements_lock(requirement).packages[0].name
    assert pixi_package.name == "shared-pkg"


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("name", 12),
        ("name", ""),
        ("version", 12),
        ("version", ""),
        ("sha256", []),
        ("sha256", "not-a-sha256"),
        ("license", []),
        ("build", 3),
        ("subdir", False),
    ],
)
def test_pixi_lock_rejects_malformed_selected_record_fields(
    tmp_path: Path, field: str, bad_value: object
) -> None:
    record = _record(
        "conda",
        _CONDA_LOCATOR,
        name="shared",
        version="1.0",
        sha256=_HASH_A,
        license="MIT",
        build="h0",
        subdir="linux-64",
    )
    record[field] = bad_value
    path = _write_pixi(
        tmp_path,
        _pixi_payload([{"conda": _CONDA_LOCATOR}], [record]),
    )

    with pytest.raises(InventoryError):
        load_pixi_lock(path, environment="documents", platform="linux-64")


@pytest.mark.parametrize(
    "reference",
    [
        {"git": "https://example.invalid/repository"},
        {"pypi": "http://files.example.invalid/package.whl"},
        {"pypi": "https://files.example.invalid"},
        {"pypi": "../local-project"},
        {"pypi": "file:///private/local-project"},
    ],
)
def test_pixi_lock_rejects_unknown_kinds_and_non_https_locators_without_echo(
    tmp_path: Path, reference: object
) -> None:
    path = _write_pixi(tmp_path, _pixi_payload([reference], []))

    with pytest.raises(InventoryError) as caught:
        load_pixi_lock(path, environment="documents", platform="linux-64")

    assert "example.invalid" not in str(caught.value)
    assert "local-project" not in str(caught.value)


@pytest.mark.parametrize(
    ("locator", "secret"),
    [
        (
            "https://user:credential-secret@files.example.invalid/package.whl",
            "credential-secret",
        ),
        (
            "https://files.example.invalid/package.whl?token=query-secret",
            "query-secret",
        ),
        (
            "https://files.example.invalid/package.whl#fragment-secret",
            "fragment-secret",
        ),
    ],
)
def test_pixi_lock_redacts_credentials_query_and_fragment(
    tmp_path: Path, locator: str, secret: str
) -> None:
    record = _record("pypi", locator, name="shared", version="1.0", sha256=_HASH_A)
    path = _write_pixi(
        tmp_path,
        _pixi_payload([{"pypi": locator}], [record]),
    )

    with pytest.raises(InventoryError) as caught:
        load_pixi_lock(path, environment="documents", platform="linux-64")

    assert secret not in str(caught.value)
    assert locator not in str(caught.value)


def test_pixi_lock_does_not_invent_missing_archive_sha256(tmp_path: Path) -> None:
    path = _write_pixi(
        tmp_path,
        _pixi_payload(
            [{"conda": _CONDA_LOCATOR}],
            [_record("conda", _CONDA_LOCATOR, name="shared", version="1.0")],
        ),
    )

    package = load_pixi_lock(
        path, environment="documents", platform="linux-64"
    ).packages[0]

    assert package.sha256 == ()


def test_requirements_lock_folds_continuations_and_preserves_hash_alternatives(
    tmp_path: Path,
) -> None:
    content = (
        "# generated lock\n"
        "\n"
        f"Zed_Pkg == 2.0 \\\n    --hash=sha256:{_HASH_A} \\\n    --hash sha256:{_HASH_B}\n"
        f"Foo.Bar==1.0 --hash=sha256:{_HASH_C}\n"
    )
    path = _write_requirements(tmp_path, content)
    source_bytes = path.read_bytes()

    inventory = load_requirements_lock(path)

    assert inventory.lock_sha256 == hashlib.sha256(source_bytes).hexdigest()
    assert [(package.name, package.version) for package in inventory.packages] == [
        ("foo-bar", "1.0"),
        ("zed-pkg", "2.0"),
    ]
    assert inventory.packages[1].sha256 == (_HASH_A, _HASH_B)
    assert all(package.ecosystem == "pypi" for package in inventory.packages)
    assert all(not package.local for package in inventory.packages)


@pytest.mark.parametrize(
    "content",
    [
        _pip_line("pkg", "1.0"),
        f"pkg==1.0 --hash=sha1:{_HASH_A}\n",
        f"pkg==1.0 --hash=sha256:{'a' * 63}\n",
        f"pkg==1.0.* --hash=sha256:{_HASH_A}\n",
        f"pkg>=1.0 --hash=sha256:{_HASH_A}\n",
        f"pkg==1.0,!=1.1 --hash=sha256:{_HASH_A}\n",
        f"pkg==1.0,==1.0 --hash=sha256:{_HASH_A}\n",
        f"pkg===1.0 --hash=sha256:{_HASH_A}\n",
        f"pkg==1.0; python_version < '3.14' --hash=sha256:{_HASH_A}\n",
        f"pkg @ https://files.example.invalid/pkg.whl --hash=sha256:{_HASH_A}\n",
        "pkg==1.0 --index-url=https://index.example.invalid/simple\n",
        "-e .\n",
        "-r nested-requirements.txt\n",
        f"not a requirement --hash=sha256:{_HASH_A}\n",
    ],
)
def test_requirements_lock_rejects_unpinned_unhashed_or_unsupported_lines(
    tmp_path: Path, content: str
) -> None:
    path = _write_requirements(tmp_path, content)

    with pytest.raises(InventoryError):
        load_requirements_lock(path)


def test_requirements_lock_rejects_duplicate_canonical_names(tmp_path: Path) -> None:
    content = (
        f"{_pip_line('Foo_Bar', '1.0', _HASH_A)}\n"
        f"{_pip_line('foo-bar', '2.0', _HASH_B)}\n"
    )
    path = _write_requirements(tmp_path, content)

    with pytest.raises(InventoryError):
        load_requirements_lock(path)


def test_requirements_lock_rejects_incomplete_continuation(tmp_path: Path) -> None:
    path = _write_requirements(tmp_path, f"pkg==1.0 --hash=sha256:{_HASH_A} \\\n")

    with pytest.raises(InventoryError):
        load_requirements_lock(path)


def test_input_reader_rejects_symlinks_and_does_not_echo_paths(
    tmp_path: Path,
) -> None:
    target = tmp_path / "private-target.lock"
    target.write_text(f"{_pip_line('pkg', '1.0', _HASH_A)}\n", encoding="utf-8")
    link = tmp_path / "private-link.lock"
    link.symlink_to(target)

    with pytest.raises(InventoryError) as caught:
        load_requirements_lock(link)

    assert str(link) not in str(caught.value)
    assert str(target) not in str(caught.value)


def test_input_reader_enforces_sixteen_mib_limit(tmp_path: Path) -> None:
    exact_limit = tmp_path / "exact-limit.lock"
    exact_limit.write_bytes(b"#" + b"x" * (16 * 1024 * 1024 - 1))
    inventory = load_requirements_lock(exact_limit)
    assert inventory.packages == ()

    oversized = tmp_path / "oversized.lock"
    oversized.write_bytes(b"#" + b"x" * (16 * 1024 * 1024))
    with pytest.raises(InventoryError):
        load_requirements_lock(oversized)


def test_input_reader_is_read_only_and_hashes_exact_source_bytes(
    tmp_path: Path,
) -> None:
    path = _write_requirements(
        tmp_path, f"# immutable witness\n{_pip_line('pkg', '1.0', _HASH_A)}\n"
    )
    original_bytes = path.read_bytes()
    before = path.stat()

    inventory = load_requirements_lock(path)

    after = path.stat()
    assert inventory.lock_sha256 == hashlib.sha256(original_bytes).hexdigest()
    assert path.read_bytes() == original_bytes
    assert stat.S_IMODE(after.st_mode) == stat.S_IMODE(before.st_mode)
    assert after.st_mtime_ns == before.st_mtime_ns
    assert after.st_ino == before.st_ino


def test_input_reader_rejects_invalid_utf8_without_echoing_content(
    tmp_path: Path,
) -> None:
    path = tmp_path / "utf8.lock"
    path.write_bytes(b"private-invalid-byte-\xff")

    with pytest.raises(InventoryError) as caught:
        load_requirements_lock(path)

    assert "private-invalid-byte" not in str(caught.value)
