"""Acceptance checks for Documents source metadata and HTTP delivery."""

from __future__ import annotations

import hashlib

import pytest
from flask import Flask

from carlina.application_companions import (
    ApplicationCompanionsError,
    ApplicationCompanionsMetadata,
    CompanionDownload,
    CompanionInventory,
)
from carlina.application_source import (
    ApplicationSourceBundle,
    ApplicationSourceError,
    ApplicationSourceMetadata,
)
from carlina.core import application_distribution
from carlina.distribution import ApplicationDistribution
from carlina.runtime_composition import RuntimeComposition
from carlina.runtime_profiles import RuntimeProfile

_REVISION = "a" * 40
_EPOCH = 1_791_000_000
_VERSION = "2.3.4"
_ARCHIVE = b"synthetic exact Documents source archive\x00\x01"
_PRIVATE_MARKER = "synthetic-private-marker"


def _distribution(profile: RuntimeProfile) -> ApplicationDistribution:
    if profile is RuntimeProfile.DOCUMENTS:
        return ApplicationDistribution(
            profile=profile,
            source_revision=_REVISION,
            source_date_epoch=_EPOCH,
            addon_ids=("doc_processor", "sentence_segmenter", "translator"),
        )
    return ApplicationDistribution(
        profile=profile,
        source_revision=None,
        source_date_epoch=None,
        addon_ids=(),
    )


def _bundle() -> ApplicationSourceBundle:
    metadata = ApplicationSourceMetadata(
        application_version=_VERSION,
        source_revision=_REVISION,
        source_date_epoch=_EPOCH,
        filename="carlina-documents-source.tar.gz",
        byte_length=len(_ARCHIVE),
        sha256=hashlib.sha256(_ARCHIVE).hexdigest(),
    )
    return ApplicationSourceBundle(metadata=metadata, archive=_ARCHIVE)


def _client(profile: RuntimeProfile) -> object:
    app = Flask("application-distribution-test")
    composition = RuntimeComposition(
        profile=profile,
        addon_ids=(
            frozenset({"doc_processor", "translator", "sentence_segmenter"})
            if profile is RuntimeProfile.DOCUMENTS
            else None
        ),
        assistant_services=profile is RuntimeProfile.WORKBENCH,
        container_services=profile is RuntimeProfile.WORKBENCH,
    )
    application_distribution.register_distribution_endpoints(app, composition)
    return app.test_client()


def _patch_identity(monkeypatch: pytest.MonkeyPatch, profile: RuntimeProfile) -> None:
    monkeypatch.setattr(
        application_distribution,
        "read_application_distribution",
        lambda: _distribution(profile),
    )
    monkeypatch.setattr(
        application_distribution,
        "version",
        lambda package: _VERSION if package == "carlina" else pytest.fail(package),
    )


def test_documents_info_reports_exact_source_offer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _bundle()
    _patch_identity(monkeypatch, RuntimeProfile.DOCUMENTS)
    monkeypatch.setattr(
        application_distribution, "read_application_source", lambda: bundle
    )

    info = application_distribution.application_distribution_info(
        RuntimeProfile.DOCUMENTS
    )

    assert info["application_profile"] == "documents"
    assert info["application_version"] == _VERSION
    assert info["source_revision"] == _REVISION
    assert info["source"] == {
        "available": True,
        "coverage": "application",
        "filename": bundle.metadata.filename,
        "byte_length": len(_ARCHIVE),
        "sha256": hashlib.sha256(_ARCHIVE).hexdigest(),
        "media_type": "application/gzip",
        "download_path": f"/api/application-source/{bundle.metadata.sha256}",
        "resource_uri": f"carlina://application/source/{bundle.metadata.sha256}",
    }


def test_workbench_info_never_probes_for_documents_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_identity(monkeypatch, RuntimeProfile.WORKBENCH)
    monkeypatch.setattr(
        application_distribution,
        "read_application_source",
        lambda: pytest.fail("Workbench must not probe for application source"),
    )
    monkeypatch.setattr(
        application_distribution,
        "read_application_companions",
        lambda **_kw: pytest.fail("Workbench must not probe for companions"),
    )

    info = application_distribution.application_distribution_info(
        RuntimeProfile.WORKBENCH
    )

    assert info["source"] == {"available": False, "reason": "not_bundled"}
    assert info["companions"] == {"available": False, "reason": "not_bundled"}


def test_older_documents_build_without_source_reports_not_bundled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_identity(monkeypatch, RuntimeProfile.DOCUMENTS)
    monkeypatch.setattr(
        application_distribution, "read_application_source", lambda: None
    )
    monkeypatch.setattr(
        application_distribution,
        "read_application_companions",
        lambda **_kw: pytest.fail(
            "Missing application source must not probe companions"
        ),
    )

    info = application_distribution.application_distribution_info(
        RuntimeProfile.DOCUMENTS
    )

    assert info["source"] == {"available": False, "reason": "not_bundled"}
    assert info["companions"] == {"available": False, "reason": "not_bundled"}


def test_invalid_source_archive_is_reported_as_unavailable_without_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_identity(monkeypatch, RuntimeProfile.DOCUMENTS)

    def invalid_archive() -> ApplicationSourceBundle:
        raise ApplicationSourceError(f"{_PRIVATE_MARKER} /private/build/path")

    monkeypatch.setattr(
        application_distribution, "read_application_source", invalid_archive
    )

    info = application_distribution.application_distribution_info(
        RuntimeProfile.DOCUMENTS
    )

    assert info["source"] == {"available": False, "reason": "invalid_archive"}
    assert _PRIVATE_MARKER not in repr(info)
    assert "/private/build/path" not in repr(info)


def test_anonymous_get_and_head_serve_only_fixed_archive_with_digest_etag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _bundle()
    _patch_identity(monkeypatch, RuntimeProfile.DOCUMENTS)
    monkeypatch.setattr(
        application_distribution, "read_application_source", lambda: bundle
    )
    client = _client(RuntimeProfile.DOCUMENTS)
    path = f"/api/application-source/{bundle.metadata.sha256}"

    response = client.get(path)

    assert response.status_code == 200
    assert response.data == _ARCHIVE
    assert response.mimetype == "application/gzip"
    assert response.headers["Content-Disposition"] == (
        f"attachment; filename={bundle.metadata.filename}"
    )
    assert response.headers["ETag"] == f'"{bundle.metadata.sha256}"'
    assert "Authorization" not in response.request.headers

    head = client.head(path)
    assert head.status_code == 200
    assert head.data == b""
    assert head.headers["Content-Length"] == str(len(_ARCHIVE))
    assert head.headers["ETag"] == f'"{bundle.metadata.sha256}"'

    conditional = client.get(path, headers={"If-None-Match": response.headers["ETag"]})
    assert conditional.status_code == 304
    assert conditional.data == b""


def test_wrong_digest_and_missing_source_return_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _bundle()
    _patch_identity(monkeypatch, RuntimeProfile.DOCUMENTS)
    monkeypatch.setattr(
        application_distribution, "read_application_source", lambda: bundle
    )
    client = _client(RuntimeProfile.DOCUMENTS)

    wrong = client.get(f"/api/application-source/{'0' * 64}")
    assert wrong.status_code == 404
    assert _PRIVATE_MARKER not in wrong.get_data(as_text=True)

    monkeypatch.setattr(
        application_distribution, "read_application_source", lambda: None
    )
    missing = client.get(f"/api/application-source/{bundle.metadata.sha256}")
    assert missing.status_code == 404


def test_tampered_source_returns_redacted_uncacheable_service_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_identity(monkeypatch, RuntimeProfile.DOCUMENTS)

    def tampered_archive() -> ApplicationSourceBundle:
        raise ApplicationSourceError(_PRIVATE_MARKER)

    monkeypatch.setattr(
        application_distribution, "read_application_source", tampered_archive
    )
    response = _client(RuntimeProfile.DOCUMENTS).get(
        f"/api/application-source/{hashlib.sha256(_ARCHIVE).hexdigest()}"
    )

    assert response.status_code == 503
    assert _PRIVATE_MARKER not in response.get_data(as_text=True)
    assert response.headers["Cache-Control"] == "no-store"


def test_workbench_source_route_is_not_found_without_reading_package(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        application_distribution,
        "read_application_source",
        lambda: pytest.fail("Workbench must not read bundled source"),
    )

    response = _client(RuntimeProfile.WORKBENCH).get(
        f"/api/application-source/{hashlib.sha256(_ARCHIVE).hexdigest()}"
    )

    assert response.status_code == 404


def test_public_metadata_projects_bound_companion_downloads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _bundle()
    _patch_identity(monkeypatch, RuntimeProfile.DOCUMENTS)
    monkeypatch.setattr(
        application_distribution, "read_application_source", lambda: bundle
    )
    metadata = ApplicationCompanionsMetadata(
        application_version=_VERSION,
        source_revision=_REVISION,
        application_source_sha256=bundle.metadata.sha256,
        wheel_sha256="b" * 64,
        inventory_inputs=(CompanionInventory(platform="linux-64", sha256="c" * 64),),
        companions=(
            CompanionDownload(
                kind="dependency_sources",
                filename="selected-sources.zip",
                byte_length=1234,
                sha256="d" * 64,
                url="https://downloads.example.test/selected-sources.zip",
                coverage="selected",
            ),
        ),
    )

    def read_companions(
        *, application_source: ApplicationSourceMetadata
    ) -> ApplicationCompanionsMetadata:
        assert application_source == bundle.metadata
        return metadata

    monkeypatch.setattr(
        application_distribution, "read_application_companions", read_companions
    )
    response = _client(RuntimeProfile.DOCUMENTS).get("/api/application-distribution")
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.get_json()["companions"] == {
        "available": True,
        "coverage": "selected",
        "application_version": _VERSION,
        "source_revision": _REVISION,
        "application_source_sha256": bundle.metadata.sha256,
        "wheel_sha256": "b" * 64,
        "resource_uri": "carlina://application/companions",
        "inventory_inputs": [{"platform": "linux-64", "sha256": "c" * 64}],
        "downloads": [
            {
                "kind": "dependency_sources",
                "filename": "selected-sources.zip",
                "byte_length": 1234,
                "sha256": "d" * 64,
                "download_url": "https://downloads.example.test/selected-sources.zip",
                "coverage": "selected",
            }
        ],
    }


def test_invalid_companions_preserve_valid_application_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _bundle()
    _patch_identity(monkeypatch, RuntimeProfile.DOCUMENTS)
    monkeypatch.setattr(
        application_distribution, "read_application_source", lambda: bundle
    )

    def invalid_companions(
        *, application_source: ApplicationSourceMetadata
    ) -> ApplicationCompanionsMetadata:
        raise ApplicationCompanionsError(f"{_PRIVATE_MARKER} /private/build/path")

    monkeypatch.setattr(
        application_distribution, "read_application_companions", invalid_companions
    )
    info = application_distribution.application_distribution_info(
        RuntimeProfile.DOCUMENTS
    )
    assert info["companions"] == {"available": False, "reason": "invalid_manifest"}
    assert info["source"]["available"] is True
    assert _PRIVATE_MARKER not in repr(info)
    assert "/private/build/path" not in repr(info)
