from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict
from pathlib import Path
from typing import cast

import pytest

from carlina.core.bootstrap_ownership import (
    BOOTSTRAP_BINDING_NAME,
    BOOTSTRAP_CLAIM_HEADER,
    BOOTSTRAP_CLAIM_NAME,
    BOOTSTRAP_PREPARATION_BINDING_NAME,
    BootstrapIdentity,
    BootstrapOwnershipError,
    bind_bootstrap_generation,
    bootstrap_claim_bytes,
    bootstrap_markers_present,
    read_bootstrap_claim,
    validate_bootstrap_generation,
    validate_bootstrap_preparation,
)
from carlina.core.native_preparation import (
    PREPARATION_NAME,
    PreparationCompletion,
    encode_preparation_completion,
)

_CLAIM_ID = "0123456789abcdef0123456789abcdef"
_HASH_A = "a" * 64
_HASH_B = "b" * 64


@pytest.mark.parametrize(
    ("profile", "kind"),
    [
        ("documents", "pixi-pack-executable"),
        ("workbench", "pixi-pack-executable"),
        ("documents", "publisher-bootstrap-executable"),
    ],
)
def test_native_completion_is_not_ready_until_root_bound(
    tmp_path: Path, profile: str, kind: str
) -> None:
    identity = _identity(profile, changes={"artifact_kind": kind})
    directory, program, data = _generation(tmp_path, identity)
    _write_claim(directory, identity)
    _write_preparation(directory, identity, program)
    assert (
        validate_bootstrap_preparation(
            directory, identity, claim_id=_CLAIM_ID, program_root=program
        )
        == _CLAIM_ID
    )
    with pytest.raises(BootstrapOwnershipError):
        validate_bootstrap_generation(
            directory, identity, program_root=program, data_root=data
        )
    assert not data.exists()
    assert not (directory / BOOTSTRAP_BINDING_NAME).exists()
    assert _bind(directory, identity, program, data) == _CLAIM_ID
    intent = directory / BOOTSTRAP_PREPARATION_BINDING_NAME
    before = intent.read_bytes()
    assert json.loads(before)["data_root"] == str(data.resolve())
    assert (
        validate_bootstrap_generation(
            directory, identity, program_root=program, data_root=data
        )
        == _CLAIM_ID
    )
    assert intent.read_bytes() == before and not data.exists()


def test_publisher_preparation_cannot_bind_workbench(tmp_path: Path) -> None:
    identity = _identity(
        "workbench", changes={"artifact_kind": "publisher-bootstrap-executable"}
    )
    directory, program, data = _generation(tmp_path, identity)
    _write_claim(directory, identity)
    _write_preparation(directory, identity, program)
    with pytest.raises(BootstrapOwnershipError):
        _bind(directory, identity, program, data)
    assert not directory.joinpath(BOOTSTRAP_BINDING_NAME).exists()
    assert not directory.joinpath(BOOTSTRAP_PREPARATION_BINDING_NAME).exists()
    assert not data.exists()


@pytest.mark.parametrize(
    "problem",
    [
        "stopped",
        "foreign-root",
        "foreign-claim",
        "foreign-identity",
        "malformed",
        "symlink",
    ],
)
def test_invalid_native_completion_cannot_publish_ready_binding(
    tmp_path: Path, problem: str
) -> None:
    identity = _identity()
    directory, program, data = _generation(tmp_path, identity)
    _write_claim(directory, identity)
    proof = _write_preparation(
        directory, identity, program, 7 if problem == "stopped" else 0
    )
    if problem == "foreign-root":
        _write_preparation(directory, identity, tmp_path / "foreign program")
    elif problem == "foreign-claim":
        proof.write_bytes(proof.read_bytes().replace(_CLAIM_ID.encode(), b"f" * 32))
    elif problem == "foreign-identity":
        proof.write_bytes(
            proof.read_bytes().replace(identity.digest.encode(), b"0" * 64)
        )
    elif problem == "malformed":
        proof.write_bytes(b"not a completion")
    elif problem == "symlink":
        target = tmp_path / "caller metadata"
        proof.rename(target)
        proof.symlink_to(target)
    before_claim = (directory / BOOTSTRAP_CLAIM_NAME).read_bytes()
    with pytest.raises(BootstrapOwnershipError):
        _bind(directory, identity, program, data)
    assert (directory / BOOTSTRAP_CLAIM_NAME).read_bytes() == before_claim
    assert not (directory / BOOTSTRAP_BINDING_NAME).exists()
    assert not (directory / BOOTSTRAP_PREPARATION_BINDING_NAME).exists()
    assert not data.exists()


def test_prepared_binding_retry_retains_first_root_after_interruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import carlina.core.bootstrap_ownership as ownership

    identity = _identity("workbench")
    directory, program, data = _generation(tmp_path, identity)
    _write_claim(directory, identity)
    _write_preparation(directory, identity, program)
    write = ownership._exclusive_write_binding

    def interrupt(
        version_dir: Path, payload: bytes, *, marker_name: str = BOOTSTRAP_BINDING_NAME
    ) -> None:
        if marker_name == BOOTSTRAP_BINDING_NAME:
            raise BootstrapOwnershipError()
        write(version_dir, payload, marker_name=marker_name)

    monkeypatch.setattr(ownership, "_exclusive_write_binding", interrupt)
    with pytest.raises(BootstrapOwnershipError):
        _bind(directory, identity, program, data)
    intent = directory / BOOTSTRAP_PREPARATION_BINDING_NAME
    original = intent.read_bytes()
    assert not (directory / BOOTSTRAP_BINDING_NAME).exists()
    monkeypatch.setattr(ownership, "_exclusive_write_binding", write)
    foreign_data = tmp_path / "foreign data"
    with pytest.raises(BootstrapOwnershipError):
        _bind(directory, identity, program, foreign_data)
    assert intent.read_bytes() == original and not foreign_data.exists()
    assert _bind(directory, identity, program, data) == _CLAIM_ID
    (directory / BOOTSTRAP_BINDING_NAME).unlink()
    with pytest.raises(BootstrapOwnershipError):
        _bind(directory, identity, program, foreign_data)
    assert _bind(directory, identity, program, data) == _CLAIM_ID
    assert intent.read_bytes() == original and not data.exists()


def test_preparation_metadata_cannot_reclassify_a_legacy_ready_binding(
    tmp_path: Path,
) -> None:
    identity = _identity()
    directory, program, data = _generation(tmp_path, identity)
    _write_claim(directory, identity)
    _bind(directory, identity, program, data)
    original = (directory / BOOTSTRAP_BINDING_NAME).read_bytes()
    _write_preparation(directory, identity, program)
    with pytest.raises(BootstrapOwnershipError):
        _bind(directory, identity, program, data)
    with pytest.raises(BootstrapOwnershipError):
        validate_bootstrap_generation(
            directory, identity, program_root=program, data_root=data
        )
    assert (directory / BOOTSTRAP_BINDING_NAME).read_bytes() == original
    assert not (directory / BOOTSTRAP_PREPARATION_BINDING_NAME).exists()


@pytest.mark.parametrize(
    "marker", [PREPARATION_NAME, BOOTSTRAP_PREPARATION_BINDING_NAME]
)
def test_preparation_markers_without_claim_are_never_unmarked_legacy(
    tmp_path: Path, marker: str
) -> None:
    identity = _identity()
    directory, program, data = _generation(tmp_path, identity)
    (directory / marker).write_bytes(b"unattributed host metadata")
    assert bootstrap_markers_present(directory)
    with pytest.raises(BootstrapOwnershipError):
        read_bootstrap_claim(directory, identity)
    with pytest.raises(BootstrapOwnershipError):
        validate_bootstrap_generation(
            directory, identity, program_root=program, data_root=data
        )


def _identity(
    profile: str = "documents", changes: dict[str, object] | None = None
) -> BootstrapIdentity:
    values: dict[str, object] = {
        "application_profile": profile,
        "release_version": "1.2.3",
        "channel": "stable",
        "index_sha256": _HASH_A,
        "artifact_platform": "linux-64",
        "artifact_kind": "pixi-pack-executable",
        "artifact_filename": "carlina-1.2.3-linux.tar.zst",
        "artifact_sha256": _HASH_B,
        "artifact_size": 1234,
    }
    if changes is not None:
        values.update(changes)
    return BootstrapIdentity(
        application_profile=cast(str, values["application_profile"]),
        release_version=cast(str, values["release_version"]),
        channel=cast(str, values["channel"]),
        index_sha256=cast(str, values["index_sha256"]),
        artifact_platform=cast(str, values["artifact_platform"]),
        artifact_kind=cast(str, values["artifact_kind"]),
        artifact_filename=cast(str, values["artifact_filename"]),
        artifact_sha256=cast(str, values["artifact_sha256"]),
        artifact_size=cast(int, values["artifact_size"]),
    )


def _generation(tmp_path: Path, identity: BootstrapIdentity) -> tuple[Path, Path, Path]:
    program_root = tmp_path / "program"
    version_dir = program_root / "versions" / identity.release_version
    version_dir.mkdir(parents=True)
    data_root = tmp_path / "data-not-created"
    return version_dir, program_root, data_root


def _write_claim(version_dir: Path, identity: BootstrapIdentity) -> Path:
    path = version_dir / BOOTSTRAP_CLAIM_NAME
    path.write_bytes(bootstrap_claim_bytes(identity, _CLAIM_ID))
    return path


def _write_preparation(
    version_dir: Path,
    identity: BootstrapIdentity,
    program_root: Path,
    exit_code: int = 0,
) -> Path:
    path = version_dir / PREPARATION_NAME
    path.write_bytes(
        encode_preparation_completion(
            PreparationCompletion(
                _CLAIM_ID, identity.digest, str(program_root.resolve()), exit_code
            )
        )
    )
    return path


def _bind(
    version_dir: Path,
    identity: BootstrapIdentity,
    program_root: Path,
    data_root: Path,
) -> str:
    return bind_bootstrap_generation(
        version_dir,
        identity,
        claim_id=_CLAIM_ID,
        program_root=program_root,
        data_root=data_root,
    )


@pytest.mark.parametrize("profile", ["documents", "workbench"])
def test_identity_digest_claim_and_ready_binding_are_canonical(
    tmp_path: Path, profile: str
) -> None:
    identity = _identity(profile)
    expected_digest = hashlib.sha256(
        json.dumps(
            asdict(identity), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("utf-8")
    ).hexdigest()
    assert identity.digest == expected_digest
    assert bootstrap_claim_bytes(identity, _CLAIM_ID) == (
        f"{BOOTSTRAP_CLAIM_HEADER}\n{_CLAIM_ID}\n{expected_digest}\n".encode("ascii")
    )

    version_dir, program_root, data_root = _generation(tmp_path, identity)
    _write_claim(version_dir, identity)
    assert _bind(version_dir, identity, program_root, data_root) == _CLAIM_ID
    assert (
        validate_bootstrap_generation(
            version_dir,
            identity,
            program_root=program_root,
            data_root=data_root,
            claim_id=_CLAIM_ID,
        )
        == _CLAIM_ID
    )
    assert not data_root.exists()
    assert list(tmp_path.iterdir()) == [program_root]
    if os.name == "posix":
        assert (version_dir / BOOTSTRAP_BINDING_NAME).stat().st_mode & 0o777 == 0o600


def test_program_and_data_root_aliases_are_canonicalized(tmp_path: Path) -> None:
    identity = _identity()
    version_dir, program_root, _ = _generation(tmp_path, identity)
    _write_claim(version_dir, identity)
    program_alias = tmp_path / "program-alias"
    data_root = tmp_path / "data-root"
    data_alias = tmp_path / "data-alias"
    data_root.mkdir()
    try:
        program_alias.symlink_to(program_root, target_is_directory=True)
        data_alias.symlink_to(data_root, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"Symlink creation unavailable: {error}")

    assert _bind(version_dir, identity, program_alias, data_alias) == _CLAIM_ID
    assert (
        validate_bootstrap_generation(
            version_dir, identity, program_root=program_root, data_root=data_root
        )
        == _CLAIM_ID
    )


def test_binding_is_idempotent_and_immutable(tmp_path: Path) -> None:
    identity = _identity()
    version_dir, program_root, data_root = _generation(tmp_path, identity)
    _write_claim(version_dir, identity)
    _bind(version_dir, identity, program_root, data_root)
    binding_path = version_dir / BOOTSTRAP_BINDING_NAME
    original_bytes = binding_path.read_bytes()
    original_stat = binding_path.stat()

    assert _bind(version_dir, identity, program_root, data_root) == _CLAIM_ID
    unchanged_stat = binding_path.stat()
    assert binding_path.read_bytes() == original_bytes
    assert (unchanged_stat.st_dev, unchanged_stat.st_ino) == (
        original_stat.st_dev,
        original_stat.st_ino,
    )
    assert unchanged_stat.st_mtime_ns == original_stat.st_mtime_ns


def test_conflicting_roots_or_identity_refuse_existing_binding(
    tmp_path: Path,
) -> None:
    identity = _identity()
    version_dir, program_root, data_root = _generation(tmp_path, identity)
    _write_claim(version_dir, identity)
    _bind(version_dir, identity, program_root, data_root)
    binding_path = version_dir / BOOTSTRAP_BINDING_NAME
    original = binding_path.read_bytes()

    with pytest.raises(
        BootstrapOwnershipError,
        match=r"^Bootstrap generation ownership is invalid\.$",
    ):
        _bind(version_dir, identity, program_root, tmp_path / "different-data")
    with pytest.raises(BootstrapOwnershipError):
        _bind(
            version_dir,
            _identity(changes={"index_sha256": "c" * 64}),
            program_root,
            data_root,
        )
    with pytest.raises(BootstrapOwnershipError):
        validate_bootstrap_generation(
            version_dir,
            identity,
            program_root=program_root,
            data_root=tmp_path / "different-data",
        )
    assert binding_path.read_bytes() == original


def test_unmarked_legacy_generation_returns_none(tmp_path: Path) -> None:
    identity = _identity()
    version_dir, program_root, data_root = _generation(tmp_path, identity)

    assert not bootstrap_markers_present(version_dir)
    assert read_bootstrap_claim(version_dir, identity) is None
    assert (
        validate_bootstrap_generation(
            version_dir, identity, program_root=program_root, data_root=data_root
        )
        is None
    )
    assert not data_root.exists()


def test_claim_alone_is_preparing_but_not_ready(tmp_path: Path) -> None:
    identity = _identity()
    version_dir, program_root, data_root = _generation(tmp_path, identity)
    _write_claim(version_dir, identity)

    assert read_bootstrap_claim(version_dir, identity) == _CLAIM_ID
    with pytest.raises(BootstrapOwnershipError):
        validate_bootstrap_generation(
            version_dir, identity, program_root=program_root, data_root=data_root
        )


def test_claim_nonce_must_match_when_supplied(tmp_path: Path) -> None:
    identity = _identity()
    version_dir, _, _ = _generation(tmp_path, identity)
    _write_claim(version_dir, identity)

    with pytest.raises(BootstrapOwnershipError):
        read_bootstrap_claim(version_dir, identity, claim_id="f" * 32)


def test_binding_without_claim_is_invalid(tmp_path: Path) -> None:
    identity = _identity()
    version_dir, _, _ = _generation(tmp_path, identity)
    (version_dir / BOOTSTRAP_BINDING_NAME).write_bytes(b"foreign")

    assert bootstrap_markers_present(version_dir)
    with pytest.raises(BootstrapOwnershipError):
        read_bootstrap_claim(version_dir, identity)


@pytest.mark.parametrize(
    "frame",
    [
        b"unknown/1\n" + _CLAIM_ID.encode() + b"\n" + _HASH_A.encode() + b"\n",
        (
            BOOTSTRAP_CLAIM_HEADER + "\r\n" + _CLAIM_ID + "\r\n" + _HASH_A + "\r\n"
        ).encode(),
        (BOOTSTRAP_CLAIM_HEADER + "\n" + _CLAIM_ID + "\n" + _HASH_A).encode(),
        (BOOTSTRAP_CLAIM_HEADER + "\n" + "A" * 32 + "\n" + _HASH_A + "\n").encode(),
        (BOOTSTRAP_CLAIM_HEADER + "\n" + _CLAIM_ID + "\n" + _HASH_B + "\n").encode(),
        (
            BOOTSTRAP_CLAIM_HEADER + "\n" + _CLAIM_ID + "\n" + _HASH_A + "\nextra\n"
        ).encode(),
    ],
)
def test_claim_frame_errors_are_closed(tmp_path: Path, frame: bytes) -> None:
    identity = _identity()
    version_dir, _, _ = _generation(tmp_path, identity)
    (version_dir / BOOTSTRAP_CLAIM_NAME).write_bytes(frame)

    with pytest.raises(BootstrapOwnershipError):
        read_bootstrap_claim(version_dir, identity)


@pytest.mark.parametrize(
    "changes",
    [
        {"application_profile": 5},
        {"application_profile": "other"},
        {"release_version": 5},
        {"release_version": "v1.2.3"},
        {"release_version": "../1.2.3"},
        {"channel": False},
        {"channel": "nightly"},
        {"index_sha256": "A" * 64},
        {"artifact_platform": "other-64"},
        {"artifact_kind": "other"},
        {"artifact_filename": "../payload"},
        {"artifact_filename": 42},
        {"artifact_filename": "x" * 256},
        {"artifact_sha256": "z" * 64},
        {"artifact_size": "1234"},
        {"artifact_size": True},
    ],
)
def test_invalid_identity_fields_are_rejected(changes: dict[str, object]) -> None:
    with pytest.raises(BootstrapOwnershipError):
        _identity(changes=changes)


def _ready_binding(tmp_path: Path) -> tuple[Path, BootstrapIdentity, Path, Path, Path]:
    identity = _identity()
    version_dir, program_root, data_root = _generation(tmp_path, identity)
    _write_claim(version_dir, identity)
    _bind(version_dir, identity, program_root, data_root)
    return (
        version_dir,
        identity,
        program_root,
        data_root,
        version_dir / BOOTSTRAP_BINDING_NAME,
    )


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("unknown", "field"),
        ("schema_version", True),
        ("claim_id", "f" * 32),
        ("program_root", "/elsewhere"),
    ],
)
def test_binding_unknown_boolean_and_conflicting_fields_fail_closed(
    tmp_path: Path, key: str, value: object
) -> None:
    version_dir, identity, program_root, data_root, binding_path = _ready_binding(
        tmp_path
    )
    record = cast(
        dict[str, object], json.loads(binding_path.read_text(encoding="utf-8"))
    )
    record[key] = value
    binding_path.write_text(json.dumps(record), encoding="utf-8")

    with pytest.raises(BootstrapOwnershipError):
        validate_bootstrap_generation(
            version_dir, identity, program_root=program_root, data_root=data_root
        )


@pytest.mark.parametrize(
    "raw",
    [
        b"{not-json",
        b"\xff",
        b"{" + b"[" * 7000 + b"]" * 7000 + b"}",
        b"{" + b" " * 65_536 + b"}",
    ],
)
def test_malformed_duplicate_invalid_utf8_deep_and_oversized_bindings_fail(
    tmp_path: Path, raw: bytes
) -> None:
    version_dir, identity, program_root, data_root, binding_path = _ready_binding(
        tmp_path
    )
    binding_path.write_bytes(raw)

    with pytest.raises(BootstrapOwnershipError):
        validate_bootstrap_generation(
            version_dir, identity, program_root=program_root, data_root=data_root
        )


def test_duplicate_binding_keys_are_rejected_even_when_values_match(
    tmp_path: Path,
) -> None:
    version_dir, identity, program_root, data_root, binding_path = _ready_binding(
        tmp_path
    )
    record = cast(
        dict[str, object], json.loads(binding_path.read_text(encoding="utf-8"))
    )
    duplicate = json.dumps(record, sort_keys=True, separators=(",", ":"))
    duplicate = duplicate.replace(
        '"claim_id":"' + _CLAIM_ID + '"',
        '"claim_id":"' + _CLAIM_ID + '","claim_id":"' + _CLAIM_ID + '"',
    )
    binding_path.write_text(duplicate + "\n", encoding="utf-8")

    with pytest.raises(BootstrapOwnershipError):
        validate_bootstrap_generation(
            version_dir, identity, program_root=program_root, data_root=data_root
        )


def test_missing_binding_key_is_rejected(tmp_path: Path) -> None:
    version_dir, identity, program_root, data_root, binding_path = _ready_binding(
        tmp_path
    )
    record = cast(
        dict[str, object], json.loads(binding_path.read_text(encoding="utf-8"))
    )
    record.pop("data_root")
    binding_path.write_text(json.dumps(record), encoding="utf-8")

    with pytest.raises(BootstrapOwnershipError):
        validate_bootstrap_generation(
            version_dir, identity, program_root=program_root, data_root=data_root
        )


def test_duplicate_nested_binding_keys_are_rejected(tmp_path: Path) -> None:
    version_dir, identity, program_root, data_root, binding_path = _ready_binding(
        tmp_path
    )
    binding_path.write_text(
        '{"format":"carlina.bootstrap-binding","schema_version":1,'
        '"claim_id":"'
        + _CLAIM_ID
        + '","identity_sha256":"'
        + identity.digest
        + '","program_root":"'
        + str(program_root.resolve())
        + '","data_root":{"nested":{"x":1,"x":2}}}\n',
        encoding="utf-8",
    )

    with pytest.raises(BootstrapOwnershipError):
        validate_bootstrap_generation(
            version_dir, identity, program_root=program_root, data_root=data_root
        )


@pytest.mark.parametrize("dangling", [False, True])
def test_claim_symlinks_are_detected_and_rejected(
    tmp_path: Path, dangling: bool
) -> None:
    identity = _identity()
    version_dir, _, _ = _generation(tmp_path, identity)
    target = tmp_path / "claim-target"
    if not dangling:
        target.write_bytes(bootstrap_claim_bytes(identity, _CLAIM_ID))
    claim_path = version_dir / BOOTSTRAP_CLAIM_NAME
    try:
        claim_path.symlink_to(target)
    except OSError as error:
        pytest.skip(f"Symlink creation unavailable: {error}")

    assert bootstrap_markers_present(version_dir)
    with pytest.raises(BootstrapOwnershipError):
        read_bootstrap_claim(version_dir, identity)


def test_binding_symlink_is_rejected(tmp_path: Path) -> None:
    identity = _identity()
    version_dir, program_root, data_root = _generation(tmp_path, identity)
    _write_claim(version_dir, identity)
    target = tmp_path / "binding-target"
    target.write_text("{}", encoding="utf-8")
    binding_path = version_dir / BOOTSTRAP_BINDING_NAME
    try:
        binding_path.symlink_to(target)
    except OSError as error:
        pytest.skip(f"Symlink creation unavailable: {error}")

    with pytest.raises(BootstrapOwnershipError):
        _bind(version_dir, identity, program_root, data_root)


@pytest.mark.parametrize("target_kind", ["version", "versions"])
def test_junction_directories_are_rejected_when_api_is_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target_kind: str
) -> None:
    identity = _identity()
    version_dir, _, _ = _generation(tmp_path, identity)
    is_junction = getattr(version_dir, "is_junction", None)
    if is_junction is None:
        pytest.skip("Path.is_junction is unavailable")
    junction_path = version_dir if target_kind == "version" else version_dir.parent

    def fake_is_junction(path: Path) -> bool:
        return path == junction_path

    monkeypatch.setattr(type(version_dir), "is_junction", fake_is_junction)

    with pytest.raises(BootstrapOwnershipError):
        read_bootstrap_claim(version_dir, identity)


def test_lexical_and_resolved_generation_targets_must_match(
    tmp_path: Path,
) -> None:
    identity = _identity()
    version_dir, program_root, data_root = _generation(tmp_path, identity)
    _write_claim(version_dir, identity)
    wrong_lexical = program_root / "versions" / "1.2.4"
    wrong_lexical.mkdir()
    with pytest.raises(BootstrapOwnershipError):
        read_bootstrap_claim(wrong_lexical, identity)

    wrong_parent = program_root / "other" / identity.release_version
    wrong_parent.mkdir(parents=True)
    with pytest.raises(BootstrapOwnershipError):
        read_bootstrap_claim(wrong_parent, identity)

    copied_program_root = tmp_path / "other-program"
    copied_program_root.mkdir()
    with pytest.raises(BootstrapOwnershipError):
        _bind(version_dir, identity, copied_program_root, data_root)


@pytest.mark.parametrize("component", ["version", "versions"])
def test_symlinked_generation_directories_are_rejected(
    tmp_path: Path, component: str
) -> None:
    identity = _identity()
    program_root = tmp_path / "program"
    versions_path = program_root / "versions"
    versions_path.mkdir(parents=True)
    version_path = versions_path / identity.release_version
    if component == "version":
        real_version = tmp_path / "real-version"
        real_version.mkdir()
        try:
            version_path.symlink_to(real_version, target_is_directory=True)
        except OSError as error:
            pytest.skip(f"Symlink creation unavailable: {error}")
        version_dir = version_path
    else:
        real_versions = tmp_path / "real-versions"
        real_versions.mkdir()
        try:
            versions_path.rmdir()
            versions_path.symlink_to(real_versions, target_is_directory=True)
        except OSError as error:
            pytest.skip(f"Symlink creation unavailable: {error}")
        version_dir = versions_path / identity.release_version
        version_dir.mkdir()

    with pytest.raises(BootstrapOwnershipError):
        read_bootstrap_claim(version_dir, identity)


def test_exclusive_create_collision_never_clobbers_foreign_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity = _identity()
    version_dir, program_root, data_root = _generation(tmp_path, identity)
    _write_claim(version_dir, identity)
    binding_path = version_dir / BOOTSTRAP_BINDING_NAME
    foreign = b"foreign bytes"
    real_open = os.open
    injected = False

    def collide(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
    ) -> int:
        nonlocal injected
        if (
            os.fsdecode(path) == str(binding_path)
            and flags & os.O_EXCL
            and not injected
        ):
            binding_path.write_bytes(foreign)
            injected = True
        if flags & os.O_CREAT:
            return real_open(path, flags, mode)
        return real_open(path, flags)

    monkeypatch.setattr(os, "open", collide)
    with pytest.raises(BootstrapOwnershipError):
        _bind(version_dir, identity, program_root, data_root)
    assert injected
    assert binding_path.read_bytes() == foreign


def test_short_binding_write_refuses_and_closes_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity = _identity()
    version_dir, program_root, data_root = _generation(tmp_path, identity)
    _write_claim(version_dir, identity)
    real_write = os.write
    descriptor_holder: list[int] = []
    write_calls = 0

    def partial_then_fail(descriptor: int, payload: bytes) -> int:
        nonlocal write_calls
        if not descriptor_holder:
            descriptor_holder.append(descriptor)
        if write_calls == 0:
            write_calls += 1
            return real_write(descriptor, payload[:4])
        write_calls += 1
        raise OSError("synthetic short-write follow-up failure")

    monkeypatch.setattr(os, "write", partial_then_fail)
    with pytest.raises(BootstrapOwnershipError):
        _bind(version_dir, identity, program_root, data_root)
    assert write_calls == 2
    assert len(descriptor_holder) == 1
    with pytest.raises(OSError):
        os.fstat(descriptor_holder[0])
    with pytest.raises(BootstrapOwnershipError):
        validate_bootstrap_generation(
            version_dir, identity, program_root=program_root, data_root=data_root
        )
