from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from iarf_assistant.core.release_manifest import (
    ReleaseManifestError,
    ReleaseSignatureError,
    TrustedReleaseKey,
    canonical_release_payload,
    parse_release_index,
    release_contract_sha256,
    sign_release_index,
    trusted_key_record,
    verify_release_index,
)
from scripts.build_managed_pack import (
    ManagedPackBuildError,
    build_managed_pack,
    inspect_wheel,
    pack_filename,
    pixi_pack_command,
)
from scripts.build_release_index import (
    ReleaseIndexBuildError,
    build_release_index,
)

pytestmark = [pytest.mark.fast, pytest.mark.contract]

_DOCUMENTS_DESCRIPTOR = {
    "format": "carlina.application-distribution",
    "schema_version": 1,
    "application_profile": "documents",
    "source_revision": "a" * 40,
    "source_date_epoch": 1791050400,
    "addon_ids": ["doc_processor", "sentence_segmenter", "translator"],
}


def _legacy_payload(schema_version: int) -> dict[str, object]:
    release: dict[str, object] = {
        "version": "1.2.0",
        "published_at": "2026-07-31T12:00:00Z",
        "addon_api_version": "1.0",
        "database": {
            "known_revisions": ["root", "head"],
            "heads": ["head"],
            "rollback": "compatible",
        },
        "artifacts": [
            {
                "platform": "win-64",
                "kind": "pixi-pack-bundle",
                "filename": "carlina-1.2.0-win-64.zip",
                "url": "carlina-1.2.0-win-64.zip",
                "sha256": "a" * 64,
                "size": 100,
            }
        ],
    }
    if schema_version == 2:
        release["execution"] = {
            "data_impact": "none",
            "migration": {"mode": "none"},
            "bootstrap": {"mode": "none"},
            "rollback": {"mode": "pointer_only"},
            "recovery": {"automatic_coverage": "none"},
            "probe": {
                "protocol": "carlina.managed-release-probe",
                "schema_version": 1,
                "isolation": "host_filesystem_and_network_denied_v1",
            },
        }
    return {
        "format": "carlina.release-index",
        "schema_version": schema_version,
        "channel": "stable",
        "generated_at": "2026-07-31T12:00:00Z",
        "releases": [release],
        "signatures": [],
    }


def _trusted(private_key: Ed25519PrivateKey):
    record = trusted_key_record(private_key)
    return TrustedReleaseKey(
        key_id=str(record["key_id"]),
        algorithm=str(record["algorithm"]),
        public_key=str(record["public_key"]),
        channels=tuple(str(channel) for channel in record["channels"]),
    )


@pytest.mark.parametrize(
    ("schema_version", "signature"),
    [
        (
            1,
            "krH341Li6UimlekqOQ9oTIOGHUMPn/LGZehOfceGaGkAGrc012AhZri7zp+9/RLPlwfvTZDVKfBiKaJTWMEPAw==",
        ),
        (
            2,
            "I8bwXt4TVhP0KT2tnK78g99QzVdfxoKjLOWtE4P6Vt5pGtqLjEVi/DB9eJJrFlVlmzLlxIfbiWP/hBvxDhdHBw==",
        ),
    ],
)
def test_legacy_v1_and_v2_signature_bytes_remain_unchanged(
    schema_version: int,
    signature: str,
):
    private_key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    legacy = parse_release_index(_legacy_payload(schema_version))

    assert legacy.application_profile is None
    assert legacy.effective_application_profile == "workbench"
    assert "application_profile" not in legacy.to_dict()

    signed = sign_release_index(legacy, private_key)
    reparsed = parse_release_index(signed.to_dict())
    assert signed.signatures[0].value == signature
    assert canonical_release_payload(reparsed) == canonical_release_payload(signed)
    assert verify_release_index(reparsed, [_trusted(private_key)])
    assert "application_profile" not in reparsed.to_dict()


def test_signed_documents_profile_is_bound_to_index_and_selected_release_receipt():
    private_key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    workbench = parse_release_index(_legacy_payload(1))
    documents_payload = _legacy_payload(1)
    documents_payload["application_profile"] = "documents"
    documents = sign_release_index(parse_release_index(documents_payload), private_key)

    assert documents.application_profile == "documents"
    assert documents.effective_application_profile == "documents"
    assert "application_profile" in documents.to_dict()
    assert release_contract_sha256(
        workbench, workbench.releases[0]
    ) != release_contract_sha256(documents, documents.releases[0])
    assert verify_release_index(documents, [_trusted(private_key)])

    changed = documents.to_dict()
    changed["application_profile"] = "workbench"
    tampered = parse_release_index(changed)
    with pytest.raises(ReleaseSignatureError):
        verify_release_index(tampered, [_trusted(private_key)])


@pytest.mark.parametrize("schema_version", [1, 2])
@pytest.mark.parametrize("profile", ["other", "", 1, None])
def test_release_index_rejects_invalid_application_profiles(
    schema_version: int,
    profile: object,
):
    payload = _legacy_payload(schema_version)
    payload["application_profile"] = profile
    with pytest.raises(ReleaseManifestError, match="application_profile"):
        parse_release_index(payload)


def _write_artifact_metadata(
    path: Path,
    *,
    platform: str = "linux-64",
    application_profile: str | None = None,
) -> Path:
    metadata: dict[str, object] = {
        "format": "carlina.managed-artifact",
        "schema_version": 1,
        "version": "1.2.3",
        "platform": platform,
        "kind": "pixi-pack-executable",
        "filename": f"carlina-1.2.3-{platform}.sh",
        "sha256": "b" * 64,
        "size": 123,
    }
    if application_profile is not None:
        metadata["application_profile"] = application_profile
    path.write_text(json.dumps(metadata), encoding="utf-8")
    return path


def _build_index(metadata_paths: list[Path], **kwargs):
    return build_release_index(
        [str(path) for path in metadata_paths],
        channel="preview",
        base_url="",
        rollback="restore_required",
        addon_api_version="1.0",
        published_at="2026-07-31T12:00:00Z",
        **kwargs,
    )[0]


def test_builder_infers_documents_and_keeps_legacy_workbench_shape(tmp_path: Path):
    documents_metadata = _write_artifact_metadata(
        tmp_path / "documents.artifact.json",
        application_profile="documents",
    )
    workbench_metadata = _write_artifact_metadata(
        tmp_path / "workbench.artifact.json",
    )

    documents = _build_index([documents_metadata])
    workbench = _build_index([workbench_metadata])

    assert documents["application_profile"] == "documents"
    assert "application_profile" not in workbench

    explicit_workbench = _build_index(
        [workbench_metadata], application_profile="workbench"
    )
    assert explicit_workbench["application_profile"] == "workbench"


def test_builder_rejects_invalid_mixed_or_mismatched_profiles(tmp_path: Path):
    documents = _write_artifact_metadata(
        tmp_path / "documents.artifact.json",
        application_profile="documents",
    )
    legacy_workbench = _write_artifact_metadata(
        tmp_path / "legacy-workbench.artifact.json",
        platform="osx-64",
    )
    invalid = _write_artifact_metadata(
        tmp_path / "invalid.artifact.json",
        platform="win-64",
        application_profile="server",
    )

    with pytest.raises(ReleaseIndexBuildError, match="mixed application profiles"):
        _build_index([documents, legacy_workbench])
    with pytest.raises(ReleaseIndexBuildError, match="Requested application profile"):
        _build_index([documents], application_profile="workbench")
    with pytest.raises(ReleaseIndexBuildError, match="Application profile"):
        _build_index([invalid])


def _write_wheel(
    path: Path,
    *,
    descriptor: dict[str, object] | bytes | None = None,
) -> Path:
    required_members = (
        "iarf_assistant/static/output.css",
        "iarf_assistant/static/frontend/manifest.json",
        "iarf_assistant/core/release_manifest_schema.json",
        "iarf_assistant/core/release_manifest_schema_v2.json",
        "iarf_assistant/core/release_trust.json",
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "carlina-1.2.3.dist-info/METADATA",
            "Name: Carlina\nVersion: 1.2.3\n",
        )
        for member in required_members:
            archive.writestr(member, "{}" if member.endswith(".json") else "")
        if descriptor is not None:
            descriptor_bytes = (
                json.dumps(descriptor).encode("utf-8")
                if isinstance(descriptor, dict)
                else descriptor
            )
            archive.writestr("carlina/distribution.json", descriptor_bytes)
    return path


def _write_pixi_manifest_and_lock(
    tmp_path: Path,
    *,
    manifest_environments: tuple[str, ...],
    lock_environments: tuple[str, ...],
) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    manifest = tmp_path / "pixi.toml"
    manifest_lines = ["[workspace]", 'name = "test"', "", "[environments]"]
    manifest_lines.extend(
        f'{name} = {{ features = ["{name}"] }}' for name in manifest_environments
    )
    manifest.write_text("\n".join(manifest_lines) + "\n", encoding="utf-8")
    lock = tmp_path / "pixi.lock"
    lock_lines = ["version: 6", "environments:"]
    lock_lines.extend(f"  {name}: {{}}" for name in lock_environments)
    lock.write_text("\n".join(lock_lines) + "\n", encoding="utf-8")
    return manifest


def test_pack_profile_selects_only_supported_environment_and_filename():
    assert pack_filename("1.2.3", "linux-64") == "carlina-1.2.3-linux-64.sh"
    assert (
        pack_filename("1.2.3", "linux-64", application_profile="documents")
        == "carlina-documents-1.2.3-linux-64.sh"
    )

    runtime = pixi_pack_command(
        pixi="pixi",
        manifest=Path("pixi.toml"),
        wheel=Path("carlina.whl"),
        platform="linux-64",
        output=Path("output.sh"),
    )
    documents = pixi_pack_command(
        pixi="pixi",
        manifest=Path("pixi.toml"),
        wheel=Path("carlina.whl"),
        platform="linux-64",
        output=Path("output.sh"),
        environment="documents",
    )
    assert runtime[runtime.index("--environment") + 1] == "runtime"
    assert documents[documents.index("--environment") + 1] == "documents"
    with pytest.raises(ManagedPackBuildError, match="Unsupported Pixi environment"):
        pixi_pack_command(
            pixi="pixi",
            manifest=Path("pixi.toml"),
            wheel=Path("carlina.whl"),
            platform="linux-64",
            output=Path("output.sh"),
            environment="unbounded",
        )


def test_descriptor_free_wheel_defaults_to_workbench(tmp_path: Path):
    wheel = _write_wheel(tmp_path / "carlina-workbench.whl")

    report = inspect_wheel(wheel)

    assert report["application_profile"] == "workbench"
    assert report["source_revision"] is None


def test_documents_wheel_descriptor_controls_pack_receipt_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    wheel = _write_wheel(
        tmp_path / "carlina-documents.whl", descriptor=_DOCUMENTS_DESCRIPTOR
    )
    report = inspect_wheel(wheel)
    assert report["application_profile"] == "documents"
    assert report["source_revision"] == "a" * 40

    manifest = _write_pixi_manifest_and_lock(
        tmp_path,
        manifest_environments=("documents",),
        lock_environments=("documents",),
    )
    invocations: list[list[str]] = []

    def fake_run(command, **_kwargs):
        invocations.append(list(command))
        output = Path(command[command.index("--output-file") + 1])
        output.write_bytes(b"bounded synthetic pack")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("scripts.build_managed_pack.subprocess.run", fake_run)
    monkeypatch.setattr(
        "scripts.build_managed_pack._source_revision",
        lambda: (_ for _ in ()).throw(
            AssertionError("checkout revision must not be used")
        ),
    )
    output, metadata_path = build_managed_pack(
        wheel,
        platform="linux-64",
        output_dir=tmp_path / "documents-pack",
        manifest=manifest,
        pixi="pixi",
    )
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))

    assert len(invocations) == 1
    assert invocations[0][invocations[0].index("--environment") + 1] == "documents"
    assert output.name == "carlina-documents-1.2.3-linux-64.sh"
    assert metadata["application_profile"] == "documents"
    assert metadata["source_revision"] == "a" * 40
    assert metadata["sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()


@pytest.mark.parametrize(
    ("manifest_environments", "lock_environments"),
    [
        (("documents",), ("runtime",)),
        (("runtime",), ("documents",)),
    ],
)
def test_documents_pack_rejects_wheel_mismatch_and_missing_locked_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    manifest_environments: tuple[str, ...],
    lock_environments: tuple[str, ...],
):
    wheel = _write_wheel(
        tmp_path / "carlina-documents.whl", descriptor=_DOCUMENTS_DESCRIPTOR
    )
    documents_manifest = _write_pixi_manifest_and_lock(
        tmp_path / "matching",
        manifest_environments=("documents",),
        lock_environments=("documents",),
    )
    calls: list[list[str]] = []
    monkeypatch.setattr(
        "scripts.build_managed_pack.subprocess.run",
        lambda command, **_kwargs: calls.append(list(command)),
    )

    with pytest.raises(ManagedPackBuildError, match="does not match wheel"):
        build_managed_pack(
            wheel,
            platform="linux-64",
            output_dir=tmp_path / "mismatch",
            manifest=documents_manifest,
            application_profile="workbench",
        )
    assert calls == []

    missing_environment_manifest = _write_pixi_manifest_and_lock(
        tmp_path / "missing",
        manifest_environments=manifest_environments,
        lock_environments=lock_environments,
    )
    with pytest.raises(
        ManagedPackBuildError,
        match=r"documents.*manifest and lock",
    ):
        build_managed_pack(
            wheel,
            platform="linux-64",
            output_dir=tmp_path / "missing-pack",
            manifest=missing_environment_manifest,
        )
    assert calls == []


@pytest.mark.parametrize(
    "descriptor",
    [
        {**_DOCUMENTS_DESCRIPTOR, "application_profile": "unexpected"},
        {**_DOCUMENTS_DESCRIPTOR, "source_revision": "short"},
        {**_DOCUMENTS_DESCRIPTOR, "addon_ids": ["translator", "doc_processor"]},
    ],
)
def test_wheel_inspection_rejects_malformed_profile_descriptor(
    tmp_path: Path,
    descriptor: dict[str, object],
):
    wheel = _write_wheel(tmp_path / "malformed.whl", descriptor=descriptor)
    with pytest.raises(ManagedPackBuildError, match="distribution descriptor"):
        inspect_wheel(wheel)


def test_wheel_inspection_bounds_optional_descriptor_reads(tmp_path: Path):
    wheel = _write_wheel(
        tmp_path / "oversized.whl",
        descriptor=b" " * 1_000_000,
    )

    with pytest.raises(ManagedPackBuildError, match="distribution descriptor"):
        inspect_wheel(wheel)
