from __future__ import annotations

from copy import deepcopy

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from iarf_assistant.core import release_manifest
from iarf_assistant.core.release_manifest import (
    ReleaseManifestError,
    ReleaseSignatureError,
    TrustedReleaseKey,
    parse_release_index,
    sign_release_index,
    trusted_key_record,
    verify_release_index,
)

pytestmark = [pytest.mark.fast, pytest.mark.contract]


def _execution() -> dict:
    return {
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


def _payload(
    *,
    schema_version: int = 3,
    platform: str = "linux-64",
    kind: str = "publisher-bootstrap-executable",
    suffix: str = ".sh",
    application_profile: str | None = "documents",
    include_profile: bool = True,
    include_execution: bool = True,
) -> dict:
    artifact = {
        "platform": platform,
        "kind": kind,
        "filename": f"carlina-1.2.3-{platform}{suffix}",
        "url": f"carlina-1.2.3-{platform}{suffix}",
        "sha256": "a" * 64,
        "size": 100,
    }
    release = {
        "version": "1.2.3",
        "published_at": "2026-07-31T12:00:00Z",
        "addon_api_version": "1.0",
        "database": {
            "known_revisions": ["head"],
            "heads": ["head"],
            "rollback": "compatible",
        },
        "artifacts": [artifact],
    }
    if schema_version >= 2 and include_execution:
        release["execution"] = _execution()
    payload = {
        "format": "carlina.release-index",
        "schema_version": schema_version,
        "channel": "stable",
        "generated_at": "2026-07-31T12:00:00Z",
        "releases": [release],
        "signatures": [],
    }
    if include_profile:
        payload["application_profile"] = application_profile
    return payload


def _trusted(private_key: Ed25519PrivateKey) -> TrustedReleaseKey:
    record = trusted_key_record(private_key)
    return TrustedReleaseKey(
        key_id=record["key_id"],
        algorithm=record["algorithm"],
        public_key=record["public_key"],
        channels=tuple(record["channels"]),
    )


@pytest.mark.parametrize(
    ("platform", "suffix"),
    [("linux-64", ".sh"), ("win-64", ".ps1")],
)
def test_v3_publisher_artifact_round_trip_and_signature(platform: str, suffix: str):
    private_key = Ed25519PrivateKey.generate()
    signed = sign_release_index(
        parse_release_index(_payload(platform=platform, suffix=suffix)),
        private_key,
    )

    reparsed = parse_release_index(signed.to_dict())

    assert reparsed.schema_version == 3
    assert reparsed.application_profile == "documents"
    assert reparsed.releases[0].execution is not None
    assert reparsed.releases[0].artifacts[0].kind == "publisher-bootstrap-executable"
    assert reparsed.to_dict() == signed.to_dict()
    assert (
        verify_release_index(reparsed, [_trusted(private_key)])
        == _trusted(private_key).key_id
    )


@pytest.mark.parametrize("schema_version", [1, 2])
def test_publisher_artifact_is_rejected_by_v1_and_v2_schemas(schema_version: int):
    with pytest.raises(ReleaseManifestError):
        parse_release_index(_payload(schema_version=schema_version))


@pytest.mark.parametrize(
    ("application_profile", "include_profile"),
    [("workbench", True), (None, False)],
)
def test_publisher_artifact_requires_explicit_documents_profile(
    application_profile: str | None,
    include_profile: bool,
):
    with pytest.raises(ReleaseManifestError, match="Publisher bootstrap artifacts"):
        parse_release_index(
            _payload(
                application_profile=application_profile,
                include_profile=include_profile,
            )
        )


@pytest.mark.parametrize("platform", ["linux-aarch64", "osx-64"])
def test_publisher_artifact_rejects_unsupported_native_platform(platform: str):
    with pytest.raises(ReleaseManifestError, match="Publisher bootstrap artifacts"):
        parse_release_index(_payload(platform=platform))


@pytest.mark.parametrize(
    ("platform", "suffix", "expected_suffix"),
    [("linux-64", ".ps1", r"\.sh filename"), ("win-64", ".sh", r"\.ps1 filename")],
)
def test_publisher_artifact_requires_platform_specific_suffix(
    platform: str,
    suffix: str,
    expected_suffix: str,
):
    with pytest.raises(ReleaseManifestError, match=expected_suffix):
        parse_release_index(_payload(platform=platform, suffix=suffix))


@pytest.mark.parametrize(
    ("platform", "suffix"), [("linux-64", ".SH"), ("win-64", ".PS1")]
)
def test_publisher_artifact_suffix_is_case_insensitive(platform: str, suffix: str):
    assert (
        parse_release_index(_payload(platform=platform, suffix=suffix)).schema_version
        == 3
    )


def test_v3_rejects_unknown_artifact_kind():
    with pytest.raises(ReleaseManifestError, match="kind"):
        parse_release_index(_payload(kind="unknown"))


def test_v3_requires_execution_contract():
    with pytest.raises(ReleaseManifestError, match="execution"):
        parse_release_index(_payload(include_execution=False))


@pytest.mark.parametrize(
    ("platform", "kind", "suffix"),
    [
        ("linux-64", "pixi-pack-executable", ".sh"),
        ("win-64", "pixi-pack-bundle", ".zip"),
    ],
)
def test_v3_preserves_existing_artifact_pairings(
    platform: str,
    kind: str,
    suffix: str,
):
    assert (
        parse_release_index(
            _payload(platform=platform, kind=kind, suffix=suffix)
        ).schema_version
        == 3
    )


def test_v3_applies_existing_execution_consistency_checks():
    payload = _payload()
    payload["releases"][0]["execution"]["recovery"]["automatic_coverage"] = "full"

    with pytest.raises(ReleaseManifestError, match="inconsistent"):
        parse_release_index(payload)


@pytest.mark.parametrize("schema_version", [4, True])
def test_unsupported_and_boolean_versions_fail_before_schema_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    schema_version: int | bool,
):
    def unexpected_dispatch(_schema_version: int) -> dict:
        raise AssertionError("schema dispatch must not run for an invalid version")

    monkeypatch.setattr(release_manifest, "_schema", unexpected_dispatch)
    payload = _payload()
    payload["schema_version"] = schema_version

    with pytest.raises(ReleaseManifestError, match="Unsupported"):
        parse_release_index(payload)


def test_signed_v3_artifact_digest_mutation_rejects_signature():
    private_key = Ed25519PrivateKey.generate()
    signed = sign_release_index(parse_release_index(_payload()), private_key)
    tampered_payload = deepcopy(signed.to_dict())
    tampered_payload["releases"][0]["artifacts"][0]["sha256"] = "b" * 64

    tampered = parse_release_index(tampered_payload)
    with pytest.raises(ReleaseSignatureError):
        verify_release_index(tampered, [_trusted(private_key)])


def test_signed_v2_round_trip_preserves_v2_execution_contract():
    payload = _payload(schema_version=2, kind="pixi-pack-executable")
    payload["releases"][0]["artifacts"][0].update(
        filename="carlina-1.2.3-linux-64.sh",
        url="carlina-1.2.3-linux-64.sh",
    )
    private_key = Ed25519PrivateKey.generate()
    signed = sign_release_index(parse_release_index(payload), private_key)

    reparsed = parse_release_index(signed.to_dict())

    assert reparsed.schema_version == 2
    assert reparsed.to_dict() == signed.to_dict()
    assert (
        verify_release_index(reparsed, [_trusted(private_key)])
        == _trusted(private_key).key_id
    )
