"""Profile-aware release-index URL selection and override compatibility."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from carlina import distribution as distribution_module
from carlina.product_identity import PRODUCT_IDENTITY, release_repository_url
from carlina.runtime_composition import resolve_runtime_composition
from carlina.runtime_profiles import RuntimeProfile
from iarf_assistant import distribution_cli
from iarf_assistant.core.release_manifest import ReleaseManifestError

pytestmark = [pytest.mark.fast, pytest.mark.contract]

_DOCUMENTS_REPOSITORY = "https://github.com/lukaszliniewicz/carlina-documents"
_REPOSITORIES = {
    RuntimeProfile.DOCUMENTS: _DOCUMENTS_REPOSITORY,
    RuntimeProfile.WORKBENCH: PRODUCT_IDENTITY.repository_url,
}


@pytest.fixture(autouse=True)
def clear_release_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "CARLINA_PROFILE",
        "CARLINA_RELEASE_INDEX_STABLE_URL",
        "CARLINA_RELEASE_INDEX_PREVIEW_URL",
        "IARF_RELEASE_INDEX_STABLE_URL",
        "IARF_RELEASE_INDEX_PREVIEW_URL",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def documents_distribution_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    descriptor = {
        "format": "carlina.application-distribution",
        "schema_version": 1,
        "application_profile": "documents",
        "source_revision": "a" * 40,
        "source_date_epoch": 1_791_050_400,
        "addon_ids": ["doc_processor", "sentence_segmenter", "translator"],
    }
    (tmp_path / "distribution.json").write_text(
        json.dumps(descriptor), encoding="utf-8"
    )
    monkeypatch.setattr(distribution_module, "files", lambda _package: tmp_path)


@pytest.mark.parametrize(
    ("profile", "expected"),
    [
        (RuntimeProfile.DOCUMENTS, _DOCUMENTS_REPOSITORY),
        ("documents", _DOCUMENTS_REPOSITORY),
        (RuntimeProfile.WORKBENCH, PRODUCT_IDENTITY.repository_url),
        ("workbench", PRODUCT_IDENTITY.repository_url),
    ],
)
def test_release_repository_url_maps_profiles(
    profile: RuntimeProfile | str,
    expected: str,
) -> None:
    assert release_repository_url(profile) == expected


def test_release_repository_url_rejects_unknown_profile() -> None:
    with pytest.raises(ValueError):
        release_repository_url("unsupported")


@pytest.mark.parametrize(
    "profile", [RuntimeProfile.DOCUMENTS, RuntimeProfile.WORKBENCH]
)
def test_stable_fallback_uses_real_runtime_composition(
    monkeypatch: pytest.MonkeyPatch,
    profile: RuntimeProfile,
) -> None:
    monkeypatch.setenv("CARLINA_PROFILE", profile.value)

    composition = resolve_runtime_composition()

    assert composition.profile is profile
    assert distribution_cli.default_release_index_url("stable") == (
        f"{_REPOSITORIES[profile]}/releases/latest/download/"
        "carlina-release-index-stable.json"
    )


def test_documents_descriptor_selects_documents_stable_fallback(
    documents_distribution_descriptor: None,
) -> None:
    assert resolve_runtime_composition().profile is RuntimeProfile.DOCUMENTS
    assert distribution_cli.default_release_index_url("stable") == (
        f"{_DOCUMENTS_REPOSITORY}/releases/latest/download/"
        "carlina-release-index-stable.json"
    )


@pytest.mark.parametrize(
    "profile", [RuntimeProfile.DOCUMENTS, RuntimeProfile.WORKBENCH]
)
@pytest.mark.parametrize("channel", ["stable", "preview"])
@pytest.mark.parametrize(
    "override", ["canonical", "legacy", "canonical_over_legacy", "empty_canonical"]
)
def test_release_index_overrides_keep_profile_and_alias_precedence(
    monkeypatch: pytest.MonkeyPatch,
    profile: RuntimeProfile,
    channel: str,
    override: str,
) -> None:
    monkeypatch.setenv("CARLINA_PROFILE", profile.value)
    canonical_name = f"CARLINA_RELEASE_INDEX_{channel.upper()}_URL"
    legacy_name = f"IARF_RELEASE_INDEX_{channel.upper()}_URL"
    canonical_url = f"https://updates.invalid/{profile.value}/{channel}/canonical.json"
    legacy_url = f"https://updates.invalid/{profile.value}/{channel}/legacy.json"

    if override == "canonical":
        monkeypatch.setenv(canonical_name, canonical_url)
        assert distribution_cli.default_release_index_url(channel) == canonical_url
        return

    if override == "legacy":
        monkeypatch.setenv(legacy_name, legacy_url)
        with pytest.warns(FutureWarning, match=legacy_name):
            assert distribution_cli.default_release_index_url(channel) == legacy_url
        return

    if override == "canonical_over_legacy":
        monkeypatch.setenv(canonical_name, canonical_url)
        monkeypatch.setenv(legacy_name, legacy_url)
        assert distribution_cli.default_release_index_url(channel) == canonical_url
        return

    monkeypatch.setenv(canonical_name, "")
    monkeypatch.setenv(legacy_name, legacy_url)
    if channel == "preview":
        with pytest.raises(
            ReleaseManifestError, match="preview channel has no mutable"
        ):
            distribution_cli.default_release_index_url(channel)
        return

    assert distribution_cli.default_release_index_url(channel) == (
        f"{_REPOSITORIES[profile]}/releases/latest/download/"
        "carlina-release-index-stable.json"
    )


@pytest.mark.parametrize(
    "profile", [RuntimeProfile.DOCUMENTS, RuntimeProfile.WORKBENCH]
)
def test_preview_without_override_remains_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
    profile: RuntimeProfile,
) -> None:
    monkeypatch.setenv("CARLINA_PROFILE", profile.value)

    with pytest.raises(ReleaseManifestError, match="preview channel has no mutable"):
        distribution_cli.default_release_index_url("preview")


def test_explicit_manifest_bypasses_all_default_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    distribution_cli.add_distribution_parsers(subparsers)
    args = parser.parse_args(
        [
            "update",
            "--manifest",
            "https://updates.invalid/explicit.json",
            "--channel",
            "preview",
        ]
    )
    default_lookup = Mock(side_effect=AssertionError("default lookup must be bypassed"))
    monkeypatch.setattr(distribution_cli, "default_release_index_url", default_lookup)

    assert distribution_cli._source(args) == "https://updates.invalid/explicit.json"
    default_lookup.assert_not_called()


def test_blank_manifest_keeps_default_lookup_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fallback_url = "https://updates.invalid/default.json"
    default_lookup = Mock(return_value=fallback_url)
    monkeypatch.setattr(distribution_cli, "default_release_index_url", default_lookup)
    args = argparse.Namespace(manifest="", channel="stable")

    assert distribution_cli._source(args) == fallback_url
    default_lookup.assert_called_once_with("stable")
