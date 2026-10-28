"""Authenticated Documents API for managing local MCP connections."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import cast

from flask import Blueprint, Flask, Response, current_app, jsonify, request
from sqlalchemy import select

from iarf_assistant.core.frontend_contracts import set_frontend_api_version_headers
from iarf_assistant.core.mcp_gateway_runtime import gateway_status
from iarf_assistant.core.mcp_jobs import (
    McpError,
    issue_access_key,
    normalize_allowed_roots,
    revoke_access_key,
)
from iarf_assistant.extensions import db
from iarf_assistant.models import McpAccessKey
from iarf_assistant.security import admin_required, current_user_id, login_required

logger = logging.getLogger(__name__)

MCP_DOCUMENT_SCOPES = (
    "mcp.access",
    "mcp.doc_processor",
    "mcp.translator",
    "mcp.work",
)
_VALIDATION_ERROR = "Check the connection label and import folders."

mcp_access_blueprint = Blueprint("mcp_access_api", __name__)


@mcp_access_blueprint.after_request
def _add_api_headers(response: Response) -> Response:
    """Keep credentials and owner-scoped metadata out of shared caches."""

    response.headers["Cache-Control"] = "no-store"
    response.vary.add("Cookie")
    set_frontend_api_version_headers(response)
    return response


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    rendered = value.isoformat()
    if value.tzinfo is None:
        return f"{rendered}Z"
    return rendered


def _error(message: str, status_code: int) -> tuple[Response, int]:
    return jsonify({"error": message}), status_code


def _domain_error(
    error: McpError,
    private_values: tuple[str, ...] = (),
) -> tuple[Response, int]:
    """Return the bounded domain message unless it contains submitted data."""

    detail = error.detail
    if any(value and value in detail for value in private_values):
        detail = _VALIDATION_ERROR
    return _error(detail, error.status_code)


@mcp_access_blueprint.get("/api/mcp/access")
@login_required
@admin_required
def get_mcp_access() -> Response:
    """Return this administrator's connection keys and listener state."""

    owner_user_id = current_user_id()
    keys: Sequence[McpAccessKey] = db.session.scalars(
        select(McpAccessKey)
        .where(McpAccessKey.owner_user_id == owner_user_id)
        .order_by(McpAccessKey.created_at.desc(), McpAccessKey.key_id.desc())
    ).all()
    return jsonify(
        {
            "gateway": gateway_status(current_app),
            "scopes": list(MCP_DOCUMENT_SCOPES),
            "keys": [
                {
                    "key_id": record.key_id,
                    "label": record.label,
                    "status": record.status,
                    "scopes": list(record.scopes or []),
                    "allowed_roots": list(record.allowed_roots or []),
                    "created_at": _iso(record.created_at),
                    "last_used_at": _iso(record.last_used_at),
                    "revoked_at": _iso(record.revoked_at),
                }
                for record in keys
            ],
        }
    )


@mcp_access_blueprint.post("/api/mcp/access")
@login_required
@admin_required
def create_mcp_access() -> Response | tuple[Response, int]:
    """Issue one Documents-scoped bearer key for explicitly selected folders."""

    raw_payload = request.get_json(silent=True)
    if not isinstance(raw_payload, dict):
        return _error(_VALIDATION_ERROR, 400)

    payload = cast(dict[str, object], raw_payload)
    if set(payload) != {"label", "allowed_roots"}:
        return _error(_VALIDATION_ERROR, 400)
    raw_label = payload.get("label")
    raw_roots = payload.get("allowed_roots")
    if not isinstance(raw_label, str) or not isinstance(raw_roots, list):
        return _error(_VALIDATION_ERROR, 400)
    label: str = raw_label
    candidate_roots: list[object] = cast(list[object], raw_roots)
    submitted_roots: list[str] = []
    for root in candidate_roots:
        if not isinstance(root, str):
            return _error(_VALIDATION_ERROR, 400)
        submitted_roots.append(root)

    try:
        normalized_roots = normalize_allowed_roots(submitted_roots)
    except UnicodeEncodeError:
        return _error(_VALIDATION_ERROR, 400)
    except McpError as exc:
        return _error(_VALIDATION_ERROR, exc.status_code)

    canonical_roots: list[str] = []
    for candidate in normalized_roots:
        path = Path(candidate)
        try:
            if not path.is_absolute() or not path.is_dir():
                return _error(_VALIDATION_ERROR, 400)
            canonical = path.resolve(strict=True)
        except OSError, RuntimeError, ValueError:
            return _error(_VALIDATION_ERROR, 400)
        if not canonical.is_dir():
            return _error(_VALIDATION_ERROR, 400)
        canonical_text = str(canonical)
        if canonical_text not in canonical_roots:
            canonical_roots.append(canonical_text)

    status = gateway_status(current_app)
    if not status["ready"] or status["url"] is None:
        return _error("The local MCP listener is not ready.", 503)

    owner_user_id = current_user_id()
    try:
        issued = issue_access_key(
            owner_user_id,
            label,
            MCP_DOCUMENT_SCOPES,
            canonical_roots,
        )
    except McpError as exc:
        db.session.rollback()
        return _domain_error(exc, (label, *submitted_roots, *canonical_roots))
    except Exception:
        db.session.rollback()
        logger.error("Unexpected MCP connection creation failure.")
        return _error("Unable to create the MCP connection.", 500)

    return jsonify(
        {
            "key_id": issued.key_id,
            "raw_token": issued.raw_token,
            "scopes": list(MCP_DOCUMENT_SCOPES),
            "allowed_roots": canonical_roots,
            "url": status["url"],
        }
    ), 201


@mcp_access_blueprint.delete("/api/mcp/access/<key_id>")
@login_required
@admin_required
def delete_mcp_access(key_id: str) -> Response | tuple[Response, int]:
    """Revoke one key only when the authenticated administrator owns it."""

    try:
        record = revoke_access_key(
            key_id,
            owner_user_id=current_user_id(),
        )
        db.session.commit()
    except McpError as exc:
        db.session.rollback()
        return _domain_error(exc)
    except Exception:
        db.session.rollback()
        logger.error("Unexpected MCP connection revocation failure.")
        return _error("Unable to revoke the MCP connection.", 500)

    return jsonify({"key_id": record.key_id, "status": "revoked"})


def register_mcp_access_blueprint(app: Flask) -> None:
    """Register connection management in the Documents profile only."""

    app.register_blueprint(mcp_access_blueprint)


__all__ = [
    "MCP_DOCUMENT_SCOPES",
    "mcp_access_blueprint",
    "register_mcp_access_blueprint",
]
