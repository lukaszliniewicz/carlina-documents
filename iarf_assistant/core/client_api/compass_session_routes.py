"""Browser-session consent and device management for Compass native clients."""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast

from flask import Blueprint, Response, jsonify, redirect, render_template, request
from flask_login import current_user, login_required

from .device_auth import (
    CompassClientAuthError,
    authorization_redirect,
    current_compass_client_auth_service,
)
from .problems import compass_problem_response, no_store

COMPASS_CLIENT_SESSION_PREFIX = "/compass/client"
COMPASS_CLIENT_DEVICE_API_PREFIX = "/api/compass/client/v1"

compass_client_session_blueprint = Blueprint(
    "compass_client_session", __name__, url_prefix=COMPASS_CLIENT_SESSION_PREFIX
)
compass_client_device_blueprint = Blueprint(
    "compass_client_device_v1", __name__, url_prefix=COMPASS_CLIENT_DEVICE_API_PREFIX
)


@compass_client_session_blueprint.errorhandler(CompassClientAuthError)
@compass_client_device_blueprint.errorhandler(CompassClientAuthError)
def handle_auth_problem(error: CompassClientAuthError) -> Any:
    return compass_problem_response(
        status_code=error.status_code,
        code=error.code,
        detail=error.detail,
        retryable=error.retryable,
    )


@compass_client_session_blueprint.route("/authorize", methods=["GET", "POST"])
@login_required
def authorize() -> Any:
    """Render consent or atomically issue a code after explicit POST approval."""

    service = current_compass_client_auth_service()
    raw_values = request.args if request.method == "GET" else request.form
    if any(len(entries) != 1 for _key, entries in raw_values.lists()):
        raise CompassClientAuthError(
            400,
            "invalid_authorization_request",
            "The Compass client authorization request is invalid.",
        )
    values = raw_values.copy()
    if request.method == "POST":
        values.pop("decision", None)
        values.pop("csrf_token", None)
    authorization = service.parse_authorization_request(values)
    if request.method == "GET":
        return no_store(
            Response(
                render_template(
                    "compass_client_authorize.html", authorization=authorization
                ),
                content_type="text/html; charset=utf-8",
            )
        )
    decision = request.form.get("decision")
    if decision == "deny":
        return no_store(
            cast(
                Response,
                redirect(
                    authorization_redirect(
                        authorization.redirect_uri,
                        {"error": "access_denied", "state": authorization.state},
                    ),
                    code=303,
                ),
            )
        )
    if decision != "approve":
        raise CompassClientAuthError(
            400,
            "invalid_authorization_request",
            "The Compass client authorization decision is invalid.",
        )
    code = service.approve(int(current_user.id), authorization)
    return no_store(
        cast(
            Response,
            redirect(
                authorization_redirect(
                    authorization.redirect_uri,
                    {"code": code, "state": authorization.state},
                ),
                code=303,
            ),
        )
    )


@compass_client_device_blueprint.get("/devices")
@login_required
def list_devices() -> Any:
    service = current_compass_client_auth_service()
    devices = service.list_devices(int(current_user.id))
    return _device_response(
        {
            "devices": [
                {
                    "deviceId": device.device_id,
                    "displayName": device.display_name,
                    "platform": device.platform,
                    "scope": device.scope,
                    "status": device.status,
                    "createdAt": _iso(device.created_at),
                    "lastUsedAt": _iso(device.last_used_at),
                    "revokedAt": _iso(device.revoked_at),
                }
                for device in devices
            ]
        }
    )


@compass_client_device_blueprint.delete("/devices/<device_id>")
@login_required
def revoke_device(device_id: str) -> Any:
    service = current_compass_client_auth_service()
    if not service.revoke_device(int(current_user.id), device_id):
        raise CompassClientAuthError(
            404,
            "device_not_found",
            "The Compass client device was not found.",
        )
    return _device_response({"revoked": True}, 200)


def register_compass_client_session_blueprints(app: Any) -> None:
    """Register browser-session endpoints once; deliberately never CSRF exempt."""

    app.register_blueprint(compass_client_session_blueprint)
    app.register_blueprint(compass_client_device_blueprint)


def _device_response(body: dict[str, object], status: int = 200) -> Response:
    response = jsonify(body)
    response.status_code = status
    return no_store(response)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


__all__ = [
    "COMPASS_CLIENT_DEVICE_API_PREFIX",
    "COMPASS_CLIENT_SESSION_PREFIX",
    "compass_client_device_blueprint",
    "compass_client_session_blueprint",
    "register_compass_client_session_blueprints",
]
