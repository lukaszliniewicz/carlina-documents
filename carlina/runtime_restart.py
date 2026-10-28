# pyright: reportMissingTypeStubs=false

"""Shared restart routes for runtimes that own a canonical data root."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import cast
from urllib.parse import urlsplit

import flask_login  # pyright: ignore[reportMissingTypeStubs]
from flask import Blueprint, Flask, Response, current_app, jsonify, request

from carlina.product_identity import product_environment_value
from iarf_assistant.core.app_paths import AppPaths
from iarf_assistant.core.restart_state import (
    pending_restart as _pending_restart,  # pyright: ignore[reportUnknownVariableType]
)
from iarf_assistant.core.restart_state import (
    request_restart as _request_restart,  # pyright: ignore[reportUnknownVariableType]
)
from iarf_assistant.security import (
    admin_required as _admin_required,  # pyright: ignore[reportUnknownVariableType]
)

_LOGGER = logging.getLogger(__name__)
runtime_restart_api = Blueprint("runtime_restart_api", __name__)
_RouteCallable = Callable[..., Response | tuple[Response, int]]
_RouteDecorator = Callable[[_RouteCallable], _RouteCallable]
_login_required_typed: _RouteDecorator = cast(
    _RouteDecorator, flask_login.login_required
)
_admin_required_typed: _RouteDecorator = cast(_RouteDecorator, _admin_required)
_pending_restart_typed: Callable[[AppPaths], dict[str, object] | None] = cast(
    Callable[[AppPaths], dict[str, object] | None], _pending_restart
)
_request_restart_typed: Callable[[AppPaths], str] = cast(
    Callable[[AppPaths], str], _request_restart
)


def _infer_restart_target_from_request() -> tuple[str | None, int | None]:
    config = cast(Mapping[str, object], current_app.config)
    configured_host = (
        str(config.get("CARLINA_HOST", "") or config.get("IARF_HOST", "")).strip()
        or product_environment_value("HOST", default="")
        or ""
    ).strip()
    configured_port_raw = (
        config.get("CARLINA_PORT")
        or config.get("IARF_PORT")
        or product_environment_value("PORT")
    )
    port_input = cast(str | int | float | bool | None, configured_port_raw)
    try:
        configured_port = int(port_input) if port_input is not None else None
    except TypeError, ValueError:
        configured_port = None

    host = configured_host or None
    port = configured_port
    if host and port:
        return host, port

    request_host = (request.host or "").strip()
    if not request_host:
        return host, port

    try:
        parsed = urlsplit(f"//{request_host}")
    except ValueError:
        return host, port

    inferred_host = parsed.hostname
    inferred_port = parsed.port
    if not host and inferred_host:
        host = inferred_host
    if port is None and inferred_port:
        port = inferred_port
    return host, port


def _current_app_paths() -> AppPaths | None:
    extensions = cast(Mapping[str, object], current_app.extensions)
    paths: object = extensions.get("carlina_paths")
    return cast(AppPaths, paths) if paths is not None else None


@runtime_restart_api.post("/api/system/restart")
@_login_required_typed
@_admin_required_typed
def restart_server() -> Response | tuple[Response, int]:
    """Request a durable handoff before scheduling the process restart."""
    from iarf_assistant.restart import (
        configure_restart_environment,
        schedule_restart,
    )

    paths = _current_app_paths()
    if paths is None:
        return jsonify({"error": "Restart handoff storage is unavailable."}), 503

    _LOGGER.info("Server restart triggered.")
    restart_host, restart_port = _infer_restart_target_from_request()
    if restart_host and restart_port:
        configure_restart_environment(restart_host, restart_port)
        _LOGGER.info(
            "Restart command configured for %s:%s before handoff.",
            restart_host,
            restart_port,
        )

    handoff_id = _request_restart_typed(paths)
    schedule_restart(delay=1.0)
    return jsonify(
        {
            "success": True,
            "message": "Server is restarting...",
            "handoff_id": handoff_id,
        }
    )


@runtime_restart_api.get("/api/system/restart-status")
@_login_required_typed
@_admin_required_typed
def restart_status() -> Response:
    paths = _current_app_paths()
    state = _pending_restart_typed(paths) if paths is not None else None
    response = jsonify(
        state
        or {
            "pending": False,
            "status": "ready",
            "reasons": [],
            "completed_handoff_id": None,
        }
    )
    response.headers["Cache-Control"] = "private, no-store"
    return response


def register_restart_blueprint(app: Flask) -> None:
    """Register the shared restart API on an application."""
    app.register_blueprint(runtime_restart_api)
