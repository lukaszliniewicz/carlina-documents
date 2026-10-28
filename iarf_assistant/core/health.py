"""Minimal unauthenticated liveness and readiness probes."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from flask import jsonify

from iarf_assistant.core.startup_readiness import startup_readiness_snapshot
from carlina.product_identity import PRODUCT_IDENTITY


def _distribution_version() -> str:
    try:
        return version(PRODUCT_IDENTITY.distribution_name)
    except PackageNotFoundError:
        return "development"


def register_health_endpoints(app) -> None:
    @app.get("/health/live")
    def carlina_liveness():
        response = jsonify(
            {
                "status": "alive",
                "product": PRODUCT_IDENTITY.short_name,
                "version": _distribution_version(),
            }
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/health/ready")
    def carlina_readiness():
        snapshot = startup_readiness_snapshot(app)
        state = snapshot["state"]
        ready = state == "ready"
        response = jsonify(
            {
                "status": "ready" if ready else "not_ready",
                "state": state,
                "phase": snapshot["phase"],
                "error_count": len(snapshot["errors"]),
                "version": _distribution_version(),
            }
        )
        response.status_code = 200 if ready else 503
        response.headers["Cache-Control"] = "no-store"
        return response


__all__ = ["register_health_endpoints"]
