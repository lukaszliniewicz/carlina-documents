"""Expose only the exact application source bundled with a Documents release."""

from __future__ import annotations

import io
import re
from importlib.metadata import PackageNotFoundError, version

from flask import Flask, Response, jsonify, send_file

from carlina.application_companions import (
    ApplicationCompanionsError,
    ApplicationCompanionsMetadata,
    read_application_companions,
)
from carlina.application_source import (
    ApplicationSourceBundle,
    ApplicationSourceError,
    read_application_source,
)
from carlina.distribution import read_application_distribution
from carlina.runtime_composition import RuntimeComposition
from carlina.runtime_profiles import RuntimeProfile

_NOT_FOUND = "Application source was not found."
APPLICATION_COMPANIONS_RESOURCE = "carlina://application/companions"


def _version() -> str:
    try:
        return version("carlina")
    except PackageNotFoundError:
        return "development"


def application_distribution_info(profile: RuntimeProfile) -> dict[str, object]:
    """Return public build identity and an exact, verified source offer."""
    distribution = read_application_distribution()
    info: dict[str, object] = {
        "application_profile": profile.value,
        "application_version": _version(),
        "source_revision": distribution.source_revision,
        "source": {"available": False, "reason": "not_bundled"},
        "companions": {"available": False, "reason": "not_bundled"},
    }
    if profile is not RuntimeProfile.DOCUMENTS:
        return info
    try:
        bundle = read_application_source()
    except ApplicationSourceError:
        info["source"] = {"available": False, "reason": "invalid_archive"}
        return info
    if bundle is not None:
        metadata = bundle.metadata
        info["source"] = {
            "available": True,
            "coverage": "application",
            "filename": metadata.filename,
            "byte_length": metadata.byte_length,
            "sha256": metadata.sha256,
            "media_type": "application/gzip",
            "download_path": f"/api/application-source/{metadata.sha256}",
            "resource_uri": f"carlina://application/source/{metadata.sha256}",
        }
        try:
            companions = read_application_companions(application_source=metadata)
        except ApplicationCompanionsError:
            info["companions"] = {"available": False, "reason": "invalid_manifest"}
        else:
            if companions is not None:
                info["companions"] = _companions_info(companions)
    return info


def _companions_info(metadata: ApplicationCompanionsMetadata) -> dict[str, object]:
    return {
        "available": True,
        "coverage": "selected",
        "application_version": metadata.application_version,
        "source_revision": metadata.source_revision,
        "application_source_sha256": metadata.application_source_sha256,
        "wheel_sha256": metadata.wheel_sha256,
        "resource_uri": APPLICATION_COMPANIONS_RESOURCE,
        "inventory_inputs": [
            {"platform": inventory.platform, "sha256": inventory.sha256}
            for inventory in metadata.inventory_inputs
        ],
        "downloads": [
            {
                "kind": download.kind,
                "filename": download.filename,
                "byte_length": download.byte_length,
                "sha256": download.sha256,
                "download_url": download.url,
                "coverage": download.coverage,
            }
            for download in metadata.companions
        ],
    }


def application_source_for_digest(
    profile: RuntimeProfile, digest: str
) -> ApplicationSourceBundle:
    """Read the fixed package archive; never interpret a request as a file path."""
    if (
        profile is not RuntimeProfile.DOCUMENTS
        or re.fullmatch(r"[0-9a-f]{64}", digest) is None
    ):
        raise LookupError(_NOT_FOUND)
    bundle = read_application_source()
    if bundle is None or bundle.metadata.sha256 != digest:
        raise LookupError(_NOT_FOUND)
    return bundle


def register_distribution_endpoints(
    app: Flask, composition: RuntimeComposition
) -> None:
    """Publish source without a login or private repository credential."""

    @app.get("/api/application-distribution")
    def carlina_distribution_info() -> Response:
        response = jsonify(application_distribution_info(composition.profile))
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/application-source/<digest>")
    def carlina_application_source(digest: str) -> Response:
        try:
            bundle = application_source_for_digest(composition.profile, digest)
        except LookupError:
            response = jsonify({"error": _NOT_FOUND})
            response.status_code = 404
            return response
        except ApplicationSourceError:
            response = jsonify({"error": "Application source is unavailable."})
            response.status_code = 503
            response.headers["Cache-Control"] = "no-store"
            return response
        return send_file(
            io.BytesIO(bundle.archive),
            mimetype="application/gzip",
            as_attachment=True,
            download_name=bundle.metadata.filename,
            etag=bundle.metadata.sha256,
            max_age=31536000,
        )


__all__ = [
    "application_distribution_info",
    "application_source_for_digest",
    "register_distribution_endpoints",
]
