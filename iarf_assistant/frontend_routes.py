from __future__ import annotations

import logging
from pathlib import Path

from flask import Blueprint, current_app, jsonify, request, url_for
from flask_login import current_user, login_required
from sqlalchemy.orm.attributes import flag_modified
from werkzeug.routing import BuildError

from iarf_assistant.core.encryption import encryption_service_from_context
from iarf_assistant.core.frontend_contracts import (
    build_frontend_bootstrap,
    set_frontend_api_version_headers,
    set_frontend_status_cache_headers,
)
from iarf_assistant.core.frontend_orchestration import (
    runtime_frontend_status_cache_info,
)
from iarf_assistant.core.startup_readiness import (
    startup_readiness_snapshot,
)
from iarf_assistant.extensions import db
from iarf_assistant.models import User

frontend_blueprint = Blueprint("frontend_api", __name__)
PROFILE_TEXT_LIMIT = 20_000


def _resolve_view_route(view_func: str) -> str | None:
    try:
        return url_for(view_func)
    except (BuildError, RuntimeError):
        return None


def _frontend_profile_payload(user) -> dict:
    addon_settings = (
        user.addon_settings if isinstance(user.addon_settings, dict) else {}
    )
    profile = addon_settings.get("profile")
    if not isinstance(profile, dict):
        profile = {}

    style_reference = profile.get("style_reference")
    if not isinstance(style_reference, str):
        legacy_parts = [
            str(profile.get(key) or "").strip()
            for key in ("style_guidelines", "writing_samples")
        ]
        style_reference = "\n\n".join(
            value for value in legacy_parts if value
        )
    preferences = profile.get("preferences")
    encryption_metadata = (
        encryption_service_from_context().key_store.public_metadata()
    )
    return {
        "email": str(getattr(user, "email", "") or ""),
        "is_admin": bool(getattr(user, "is_admin", False)),
        "preferences": preferences if isinstance(preferences, str) else "",
        "style_reference": style_reference,
        "security": {
            "envelope_version": 2,
            "active_key_id": encryption_metadata["active_key_id"],
            "key_backend": encryption_metadata["backend"],
            "persistent": True,
            "automation_ready": True,
            "recovery_export_supported": True,
            "note": (
                "New protected values use the persistent installation key. "
                "Password changes and background work do not change that key."
            ),
        },
    }


@frontend_blueprint.route("/api/frontend/bootstrap", methods=["GET"])
@login_required
def get_frontend_bootstrap():
    """Returns the authenticated, versioned shell and addon registry contract."""
    project_root = Path(current_app.root_path).resolve().parent
    timings: dict[str, float] = {}
    cache_info_before = runtime_frontend_status_cache_info(project_root)
    payload = build_frontend_bootstrap(
        user=current_user,
        manager=getattr(current_app, "addon_manager", None),
        current_path=request.args.get("path") or request.referrer or "/",
        project_root=project_root,
        route_resolver=_resolve_view_route,
        timings=timings,
        composition=current_app.extensions.get("carlina_runtime_composition"),
    )
    response = jsonify(payload)
    set_frontend_api_version_headers(response)
    response.headers["Cache-Control"] = "private, no-cache"
    response.headers["Server-Timing"] = ", ".join(
        (
            f"bootstrap;dur={timings.get('total_ms', 0.0):.2f}",
            f"registry;dur={timings.get('registry_ms', 0.0):.2f}",
            f"addons;dur={timings.get('addons_ms', 0.0):.2f}",
            (
                "feature_flags;dur="
                f"{timings.get('feature_flags_ms', 0.0):.2f}"
            ),
            (
                "frontend_status;dur="
                f"{timings.get('core_status_ms', 0.0):.2f}"
            ),
        )
    )
    cache_status = (
        "hit" if cache_info_before["hit"] else "miss"
    )
    set_frontend_status_cache_headers(response, cache_status)
    if timings.get("total_ms", 0.0) >= 500:
        logging.warning(
            "Slow frontend bootstrap: %.2fms (registry=%.2fms, "
            "addons=%.2fms, flags=%.2fms, status=%.2fms)",
            timings["total_ms"],
            timings.get("registry_ms", 0.0),
            timings.get("addons_ms", 0.0),
            timings.get("feature_flags_ms", 0.0),
            timings.get("core_status_ms", 0.0),
        )
    return response


@frontend_blueprint.route(
    "/api/frontend/readiness",
    methods=["GET"],
)
@login_required
def get_frontend_readiness():
    response = jsonify(startup_readiness_snapshot(current_app))
    response.headers["Cache-Control"] = "private, no-cache"
    return response


@frontend_blueprint.route("/api/frontend/profile", methods=["GET"])
@login_required
def get_frontend_profile():
    """Return the intentionally small profile contract used by the shell."""
    response = jsonify(_frontend_profile_payload(current_user))
    response.headers["Cache-Control"] = "private, no-cache"
    return response


@frontend_blueprint.route("/api/frontend/profile", methods=["PUT"])
@login_required
def update_frontend_profile():
    """Update only the profile fields owned by the compact Svelte form."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "A JSON object is required."}), 400

    values: dict[str, str] = {}
    for key in ("preferences", "style_reference"):
        if key not in data:
            continue
        if not isinstance(data[key], str):
            return jsonify({"error": f"{key} must be a string."}), 400
        value = data[key].replace("\x00", "")
        if len(value) > PROFILE_TEXT_LIMIT:
            return jsonify(
                {
                    "error": (
                        f"{key} must be no longer than "
                        f"{PROFILE_TEXT_LIMIT:,} characters."
                    )
                }
            ), 400
        values[key] = value

    user = db.session.get(User, current_user.id)
    if user is None:
        return jsonify({"error": "Current user was not found."}), 404
    if not isinstance(user.addon_settings, dict):
        user.addon_settings = {}
    profile = user.addon_settings.get("profile")
    if not isinstance(profile, dict):
        profile = {}
        user.addon_settings["profile"] = profile
    profile.update(values)
    flag_modified(user, "addon_settings")
    db.session.commit()

    response = jsonify(
        {
            "success": True,
            "profile": _frontend_profile_payload(user),
        }
    )
    response.headers["Cache-Control"] = "private, no-cache"
    return response


def register_frontend_blueprint(app) -> None:
    app.register_blueprint(frontend_blueprint)


__all__ = ["frontend_blueprint", "register_frontend_blueprint"]
