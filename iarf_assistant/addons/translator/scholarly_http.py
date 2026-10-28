"""HTTP adapter for the Translator scholarly-review service.

This module owns transport concerns only.  Grants, sessions, optimistic
concurrency, and durable review state remain in :mod:`scholarly_review`; the
document-review functions remain the principal-neutral read service.
"""

from __future__ import annotations

import copy
import hmac
import json
import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from functools import wraps
from typing import Any, TypeVar, cast
from urllib.parse import urlsplit

from flask import Blueprint, Response, g, jsonify, request
from flask_login import current_user
from sqlalchemy.exc import SQLAlchemyError

from ...database_utils import run_transaction_with_retry
from ...extensions import db
from . import document_review, scholarly_review
from .block_workspace_api import BlockWorkspaceIntegrityError, load_block_source_plan
from .models import TranslationProject, TranslationShareGrant
from .scholarly_wire import (
    block_review,
    dossier,
    dossier_summary,
    glossary_suggestion,
    grant,
    reader_note,
    retranslation_request,
)

logger = logging.getLogger(__name__)

SHARE_TOKEN_HEADER = "X-Translator-Share-Token"
CSRF_HEADER = "X-Translator-CSRF"
LEGACY_SHARE_TOKEN_HEADER = "X-Shared-Token"
IDEMPOTENCY_HEADER = "Idempotency-Key"

SESSION_COOKIE_PREFIX = "translator_review_session_"
SESSION_COOKIE_PATH = "/addons/translator/"
SESSION_COOKIE_HTTP_ONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_SECURE = "request.is_secure"
SESSION_COOKIE_HTTPONLY = SESSION_COOKIE_HTTP_ONLY

# Descriptive aliases are part of the adapter contract for callers that want
# to configure a client without copying policy literals.
TRANSLATOR_SHARE_TOKEN_HEADER = SHARE_TOKEN_HEADER
TRANSLATOR_CSRF_HEADER = CSRF_HEADER
SHARE_TOKEN_HEADER_NAME = SHARE_TOKEN_HEADER
CSRF_HEADER_NAME = CSRF_HEADER
SESSION_COOKIE_NAME_PREFIX = SESSION_COOKIE_PREFIX
COOKIE_PATH = SESSION_COOKIE_PATH
COOKIE_HTTP_ONLY = SESSION_COOKIE_HTTP_ONLY
COOKIE_SAMESITE = SESSION_COOKIE_SAMESITE
REVIEW_SESSION_COOKIE_PATH = SESSION_COOKIE_PATH
REVIEW_SESSION_COOKIE_HTTPONLY = SESSION_COOKIE_HTTP_ONLY
REVIEW_SESSION_COOKIE_SAMESITE = SESSION_COOKIE_SAMESITE

_MAX_GRANT_JSON_BYTES = 2 * 1024
_MAX_SESSION_JSON_BYTES = 1 * 1024
_MAX_IDEMPOTENCY_KEY = 128
_T = TypeVar("_T")


def review_session_cookie_name(project_id: str) -> str:
    """Return the project-isolated cookie name for a validated UUID id."""

    if not isinstance(project_id, str):
        raise TypeError("project id must be a UUID")
    try:
        project_uuid = uuid.UUID(project_id)
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError("project id must be a UUID") from exc
    return f"{SESSION_COOKIE_PREFIX}{project_uuid.hex}"


def _canonical_project_id(project_id: Any) -> str | None:
    if not isinstance(project_id, str):
        return None
    try:
        return str(uuid.UUID(project_id))
    except (ValueError, AttributeError, TypeError):
        return None


def _project(project_id: Any) -> TranslationProject | None:
    # Project identity is resolved by the model boundary.  UUID validation is
    # intentionally limited to cookie-name derivation; this keeps the auth
    # decorator compatible with historical synthetic/imported project IDs while
    # never creating a cookie with an unvalidated name component.
    if not isinstance(project_id, str) or not project_id or "\x00" in project_id:
        return None
    return TranslationProject.query.filter_by(id=project_id).first()


def _generic_not_found() -> tuple[Response, int]:
    return jsonify({"error": "Project not found or access denied"}), 404


def _error_response(code: str, status: int) -> tuple[Response, int]:
    # Codes originate from the service's bounded error type or local stable
    # validators.  Never include exception text or a serialized ORM object.
    return jsonify({"error": code, "code": code}), status


def _safe_code(value: Any, default: str = "scholarly-review-invalid") -> str:
    code = str(value) if isinstance(value, str) else default
    if not code or len(code) > 96 or any(
        character not in "abcdefghijklmnopqrstuvwxyz0123456789._-"
        for character in code
    ):
        return default
    return code


def _same_origin() -> bool:
    """Require exact scheme+netloc equality with the request host."""

    host = urlsplit(request.host_url)
    if not host.scheme or not host.netloc:
        return False
    origin = request.headers.get("Origin")
    if origin is not None:
        candidate = urlsplit(origin)
        return (
            candidate.scheme == host.scheme
            and candidate.netloc == host.netloc
        )
    referer = request.headers.get("Referer")
    if referer is None:
        return False
    candidate = urlsplit(referer)
    return candidate.scheme == host.scheme and candidate.netloc == host.netloc


def _grant_label(project: Any, actor: Any) -> str | None:
    grant_id = getattr(actor, "share_grant_id", None)
    if not isinstance(grant_id, str) or not grant_id:
        return None
    try:
        row = db.session.get(TranslationShareGrant, grant_id)
    except Exception:  # noqa: BLE001 - auth fails closed on storage errors.
        return None
    if row is None or str(getattr(row, "project_id", "")) != str(project.id):
        return None
    label = getattr(row, "label", None)
    return label[:128] if isinstance(label, str) and "\x00" not in label else None


def _resolve_request_actor(project: Any) -> tuple[Any, str, str, bool, str] | None:
    """Resolve the packet's ordered owner/grant/session/legacy auth chain."""

    if bool(getattr(current_user, "is_authenticated", False)) and str(
        getattr(current_user, "id", "")
    ) == str(getattr(project, "user_id", "")):
        return (
            scholarly_review.ReviewActor("owner", current_user.id),
            "owner",
            "Owner",
            False,
            "owner",
        )

    raw_grant = request.headers.get(SHARE_TOKEN_HEADER)
    if raw_grant:
        try:
            actor = scholarly_review.resolve_share_grant(project, raw_grant)
            label = _grant_label(project, actor)
            if label is not None:
                return actor, actor.role, label, True, "grant_header"
        except Exception:  # noqa: BLE001 - capability resolution is fail closed.
            db.session.rollback()

    try:
        cookie_name = review_session_cookie_name(str(project.id))
    except ValueError:
        return None
    raw_session = request.cookies.get(cookie_name)
    if raw_session:
        try:
            actor = scholarly_review.resolve_session(project, raw_session)
            label = _grant_label(project, actor)
            if label is not None:
                return actor, actor.role, label, True, raw_session
        except Exception:  # noqa: BLE001 - capability resolution is fail closed.
            db.session.rollback()

    # This is intentionally the deprecated header only.  Query strings are
    # never accepted for either legacy or new share credentials.
    legacy = request.headers.get(LEGACY_SHARE_TOKEN_HEADER)
    stored = getattr(project, "share_token", None)
    if (
        isinstance(legacy, str)
        and isinstance(stored, str)
        and hmac.compare_digest(legacy, stored)
    ):
        return (
            scholarly_review.ReviewActor("viewer", share_grant_id=f"legacy:{project.id}"),
            "viewer",
            "Legacy shared link",
            True,
            "legacy_header",
        )
    return None


def _read_actor() -> Any | None:
    """Legacy links are adapter-authenticated, but have no grant row."""

    return None if getattr(g, "share_auth_kind", None) == "legacy_header" else g.review_actor


def _session_cookie_csrf_ok(project: Any) -> bool:
    if not _same_origin():
        return False
    raw_session = getattr(g, "share_session_secret", None)
    csrf = request.headers.get(CSRF_HEADER)
    if not isinstance(raw_session, str) or not isinstance(csrf, str) or not csrf:
        return False
    try:
        return bool(scholarly_review.validate_session_csrf(project, raw_session, csrf))
    except Exception:  # noqa: BLE001 - malformed/expired sessions fail closed.
        db.session.rollback()
        return False


def project_access_required(  # noqa: UP047 - optional decorator invocation.
    function: Callable[..., _T] | None = None,
):
    """Authenticate an exact project owner or one of its review capabilities."""

    def decorator(view: Callable[..., _T]):
        @wraps(view)
        def decorated_function(project_id: str, *args: Any, **kwargs: Any):
            try:
                project = _project(project_id)
            except Exception:  # noqa: BLE001 - auth failures are generic 404s.
                db.session.rollback()
                return _generic_not_found()
            if project is None:
                return _generic_not_found()
            resolved = _resolve_request_actor(project)
            if resolved is None:
                return _generic_not_found()
            actor, role, label, guest, auth_kind = resolved
            g.project = project
            g.review_actor = actor
            g.access_role = role
            g.access_label = label[:128]
            g.is_guest = guest
            if auth_kind == "grant_header":
                g.share_auth_kind = "grant_header"
            elif auth_kind == "legacy_header":
                g.share_auth_kind = "legacy_header"
            elif auth_kind == "owner":
                g.share_auth_kind = "owner"
            else:
                g.share_auth_kind = "session"
                g.share_session_secret = auth_kind
            if g.share_auth_kind == "session" and request.method not in {
                "GET",
                "HEAD",
                "OPTIONS",
            } and not _session_cookie_csrf_ok(project):
                return _error_response("csrf-required", 403)
            return view(project_id, *args, **kwargs)

        return decorated_function

    return decorator(function) if function is not None else decorator


def _json_body(*, keys: set[str], maximum_bytes: int) -> dict[str, Any] | tuple[Response, int]:
    content_length = request.content_length
    if content_length is not None and content_length > maximum_bytes:
        return _error_response("request-too-large", 400)
    raw = request.get_data(cache=True)
    if len(raw) > maximum_bytes:
        return _error_response("request-too-large", 400)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _error_response("invalid-json", 400)
    if not isinstance(value, dict) or set(value) != keys:
        return _error_response("invalid-request-shape", 400)
    return value


def _json_body_optional(
    *, required: set[str], optional: set[str], maximum_bytes: int
) -> dict[str, Any] | tuple[Response, int]:
    content_length = request.content_length
    if content_length is not None and content_length > maximum_bytes:
        return _error_response("request-too-large", 400)
    raw = request.get_data(cache=True)
    if len(raw) > maximum_bytes:
        return _error_response("request-too-large", 400)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _error_response("invalid-json", 400)
    if (
        not isinstance(value, dict)
        or not required.issubset(value)
        or set(value) - required - optional
    ):
        return _error_response("invalid-request-shape", 400)
    return value


def _role_required(*roles: str) -> tuple[Response, int] | None:
    if getattr(g, "access_role", None) not in roles:
        return _error_response("review-authority-required", 403)
    return None


def _status_for_service_code(code: str) -> int:
    if "conflict" in code or "idempotency" in code or code == "review-target-version":
        return 409
    if code in {
        "review-owner-required",
        "review-mutation-forbidden",
        "review-read-forbidden",
        "glossary-suggestion-reviewer-required",
        "retranslation-owner-required",
    }:
        return 403
    if any(part in code for part in ("missing", "not-found")):
        return 404
    return 400


def _service_error(
    exc: Exception, *, latest_projector: Callable[[Any], Any] | None = None
) -> tuple[Response, int]:
    code = _safe_code(getattr(exc, "code", "scholarly-review-failed"))
    status = _status_for_service_code(code)
    if status == 404:
        return _generic_not_found()
    payload: dict[str, Any] = {"error": code, "code": code}
    latest = getattr(exc, "latest", None)
    if status == 409 and latest is not None:
        try:
            payload["latest"] = latest_projector(latest) if latest_projector else latest
        except Exception:  # noqa: BLE001 - a conflict must remain safe.
            payload.pop("latest", None)
    return jsonify(payload), status


def _document_error(exc: document_review.DocumentReviewError) -> tuple[Response, int]:
    code = _safe_code(getattr(exc, "code", "document-review-failed"))
    status = 404 if any(part in code for part in ("missing", "not-found")) else 400
    if status == 404:
        return _generic_not_found()
    return _error_response(code, status)


def _call_service(  # noqa: UP047 - shared Flask adapter keeps a TypeVar signature.
    operation: Callable[[], _T],
    *,
    mutation: bool = False,
    latest_projector: Callable[[Any], Any] | None = None,
    retry_operation_name: str | None = None,
) -> tuple[_T | None, tuple[Response, int] | None]:
    try:
        if retry_operation_name is not None:
            if not mutation:
                raise ValueError("Retryable service operations must be mutations.")
            result = run_transaction_with_retry(
                db.session,
                lambda _session: operation(),
                operation_name=retry_operation_name,
            )
        else:
            result = operation()
        if mutation and retry_operation_name is None:
            db.session.commit()
        return result, None
    except scholarly_review.ScholarlyReviewError as exc:
        if mutation:
            db.session.rollback()
        return None, _service_error(exc, latest_projector=latest_projector)
    except document_review.DocumentReviewError as exc:
        if mutation:
            db.session.rollback()
        return None, _document_error(exc)
    except BlockWorkspaceIntegrityError as exc:
        if mutation:
            db.session.rollback()
        return None, _error_response(_safe_code(exc.code, "workspace-invalid"), 409)
    except SQLAlchemyError:
        db.session.rollback()
        logger.error("Translator scholarly HTTP database failure")
        return None, _error_response("service-unavailable", 503)
    except Exception:  # noqa: BLE001 - HTTP boundary redacts unexpected failures.
        db.session.rollback()
        logger.error("Translator scholarly HTTP runtime failure")
        return None, _error_response("service-unavailable", 503)


def _parse_expiry(value: Any) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("expiry")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("expiry")
    # The scholarly service accepts the repository's naive-UTC database
    # boundary value (``utc_now_db``), not an aware datetime.
    return parsed.astimezone(UTC).replace(tzinfo=None)


def _cookie_expiry(value: Any) -> tuple[datetime | None, int | None]:
    if not isinstance(value, str):
        return None, None
    try:
        expires = datetime.fromisoformat(value)
        if expires.tzinfo is None:
            # SQLAlchemy's UTC database policy intentionally stores naive UTC
            # datetimes on SQLite; HTTP cookie dates still need an explicit
            # interpretation.
            expires = expires.replace(tzinfo=UTC)
        expires = expires.astimezone(UTC)
        seconds = max(0, int((expires - datetime.now(UTC)).total_seconds()))
        return expires, seconds
    except (TypeError, ValueError, OverflowError):
        return None, None


def _set_session_cookie(response: Response, project_id: str, raw_session: str, expires_at: Any) -> None:
    expires, max_age = _cookie_expiry(expires_at)
    response.set_cookie(
        review_session_cookie_name(project_id),
        raw_session,
        path=SESSION_COOKIE_PATH,
        httponly=SESSION_COOKIE_HTTP_ONLY,
        samesite=SESSION_COOKIE_SAMESITE,
        secure=request.is_secure,
        expires=expires,
        max_age=max_age,
    )


def _session_wire(project: Any, actor: Any, csrf: str, expires_at: Any) -> dict[str, Any]:
    label = _grant_label(project, actor) or "Shared review"
    return {
        "project_id": str(project.id),
        "role": actor.role,
        "label": label[:128],
        "csrf": csrf,
        "expires_at": expires_at,
    }


def scholarly_redeem_share() -> tuple[Response, int] | Response:
    if not _same_origin():
        return _error_response("same-origin-required", 403)
    body = _json_body(keys={"project_id", "secret"}, maximum_bytes=_MAX_GRANT_JSON_BYTES)
    if not isinstance(body, dict):
        return body
    if not isinstance(body.get("project_id"), str) or not isinstance(
        body.get("secret"), str
    ):
        return _error_response("invalid-request-shape", 400)
    project = None
    try:
        project = _project(body.get("project_id"))
    except SQLAlchemyError:
        db.session.rollback()
        logger.error("Translator scholarly HTTP database failure resolving project")
        return _error_response("service-unavailable", 503)
    if project is None:
        return _generic_not_found()
    result, error = _call_service(
        lambda: scholarly_review.redeem_share_grant(project, body["secret"]),
        mutation=True,
    )
    if error is not None:
        if isinstance(result, tuple):
            return error
        # Invalid capability material is intentionally indistinguishable from
        # an invalid project at this public endpoint.
        if error[1] == 404:
            return _generic_not_found()
        return error
    assert result is not None
    session_projection, raw_session, raw_csrf = result
    actor, actor_error = _resolve_session_actor(project, raw_session)
    if actor_error is not None:
        return actor_error
    payload = _session_wire(project, actor, raw_csrf, session_projection.get("expires_at"))
    response = jsonify(payload)
    _set_session_cookie(response, str(project.id), raw_session, payload["expires_at"])
    return response


def _resolve_session_actor(project: Any, raw_session: str) -> tuple[Any, tuple[Response, int] | None]:
    try:
        actor = scholarly_review.resolve_session(project, raw_session)
    except scholarly_review.ScholarlyReviewError:
        db.session.rollback()
        return None, _generic_not_found()
    except Exception:  # noqa: BLE001 - session resolution fails closed.
        db.session.rollback()
        logger.error("Translator scholarly HTTP session resolution failure")
        return None, _error_response("service-unavailable", 503)
    if _grant_label(project, actor) is None:
        return None, _generic_not_found()
    return actor, None


def scholarly_bootstrap_share_session() -> tuple[Response, int] | Response:
    if not _same_origin():
        return _error_response("same-origin-required", 403)
    body = _json_body(keys={"project_id"}, maximum_bytes=_MAX_SESSION_JSON_BYTES)
    if not isinstance(body, dict):
        return body
    if not isinstance(body["project_id"], str):
        return _error_response("invalid-request-shape", 400)
    try:
        project = _project(body.get("project_id"))
    except SQLAlchemyError:
        db.session.rollback()
        logger.error("Translator scholarly HTTP database failure resolving project")
        return _error_response("service-unavailable", 503)
    if project is None:
        return _generic_not_found()
    try:
        raw_session = request.cookies.get(review_session_cookie_name(str(project.id)))
    except ValueError:
        raw_session = None
    if not raw_session:
        return _generic_not_found()
    actor, error = _resolve_session_actor(project, raw_session)
    if error is not None:
        return error
    result, error = _call_service(
        lambda: scholarly_review.rotate_session_csrf(project, raw_session),
        mutation=True,
    )
    if error is not None:
        if error[1] == 404:
            return _generic_not_found()
        return error
    assert result is not None
    session_projection, csrf = result
    return jsonify(_session_wire(project, actor, csrf, session_projection.get("expires_at")))


def scholarly_list_share_grants(project_id: str):
    role_error = _role_required("owner")
    if role_error is not None:
        return role_error
    result, error = _call_service(
        lambda: scholarly_review.list_share_grants(g.project, actor=g.review_actor)
    )
    if error is not None:
        return error
    return jsonify({"grants": [grant(row) for row in result or []]})


def scholarly_issue_share_grant(project_id: str):
    role_error = _role_required("owner")
    if role_error is not None:
        return role_error
    body = _json_body(
        keys={"label", "role", "expires_at"}, maximum_bytes=_MAX_GRANT_JSON_BYTES
    )
    if not isinstance(body, dict):
        return body
    try:
        expires_at = _parse_expiry(body["expires_at"])
    except (TypeError, ValueError):
        return _error_response("grant-expiry", 400)
    result, error = _call_service(
        lambda: scholarly_review.issue_share_grant(
            g.project,
            label=body["label"],
            role=body["role"],
            actor=g.review_actor,
            expires_at=expires_at,
        ),
        mutation=True,
    )
    if error is not None:
        return error
    assert result is not None
    projection, secret = result
    payload = {
        "grant": grant(projection),
        "secret": secret,
        "review_path": f"/addons/translator/review/{g.project.id}",
    }
    return jsonify(payload), 201


def scholarly_revoke_share_grant(project_id: str, grant_id: str):
    role_error = _role_required("owner")
    if role_error is not None:
        return role_error
    result, error = _call_service(
        lambda: scholarly_review.revoke_share_grant(
            g.project, grant_id, actor=g.review_actor
        ),
        mutation=True,
    )
    if error is not None:
        return error
    return jsonify({"grant": grant(result)})


def scholarly_list_research_dossiers(project_id: str):
    result, error = _call_service(
        lambda: scholarly_review.list_research_dossiers(g.project, include_payload=False)
    )
    if error is not None:
        return error
    return jsonify({"dossiers": [dossier_summary(row) for row in result or []]})


def scholarly_submit_research_dossier(project_id: str):
    role_error = _role_required("owner")
    if role_error is not None:
        return role_error
    body = _json_body(
        keys={
            "client_submission_id",
            "payload",
            "provenance",
            "scope",
            "dossier_id",
            "expected_revision",
        },
        maximum_bytes=2 * 1024 * 1024,
    )
    if not isinstance(body, dict):
        return body

    # The retry callback owns project reload, authorization reconstruction, and
    # the complete service unit.  Copy request values before capturing them so
    # a retry cannot observe caller mutations between attempts.
    request_project_id = copy.deepcopy(project_id)
    request_client_submission_id = copy.deepcopy(body["client_submission_id"])
    request_payload = copy.deepcopy(body["payload"])
    request_provenance = copy.deepcopy(body["provenance"])
    request_scope = copy.deepcopy(body["scope"])
    request_dossier_id = copy.deepcopy(body["dossier_id"])
    request_expected_revision = copy.deepcopy(body["expected_revision"])
    actor_role = copy.deepcopy(g.review_actor.role)
    actor_user_id = copy.deepcopy(g.review_actor.user_id)
    actor_grant_id = copy.deepcopy(g.review_actor.share_grant_id)

    def submit_once():
        project = _project(copy.deepcopy(request_project_id))
        if project is None:
            raise scholarly_review.ScholarlyReviewError("review-project-missing")
        actor = scholarly_review.ReviewActor(
            actor_role,
            user_id=actor_user_id,
            share_grant_id=actor_grant_id,
        )
        return scholarly_review.submit_research_dossier(
            project,
            client_submission_id=copy.deepcopy(request_client_submission_id),
            payload=copy.deepcopy(request_payload),
            provenance=copy.deepcopy(request_provenance),
            scope=copy.deepcopy(request_scope),
            actor=actor,
            dossier_id=copy.deepcopy(request_dossier_id),
            expected_revision=copy.deepcopy(request_expected_revision),
        )

    result, error = _call_service(
        submit_once,
        mutation=True,
        retry_operation_name="translator_research_dossier_submit",
        latest_projector=lambda row: dossier(
            row, include_payload=True, include_proposals=True
        ),
    )
    if error is not None:
        return error
    return jsonify(
        {
            "dossier": dossier(
                result, include_payload=True, include_proposals=True
            )
        }
    ), 201


def scholarly_get_research_dossier(project_id: str, dossier_id: str):
    include_payload = request.args.get("include_payload") == "1"
    revision_id = request.args.get("revision_id")
    result, error = _call_service(
        lambda: scholarly_review.get_research_dossier(
            g.project,
            dossier_id,
            revision_id=revision_id,
            include_payload=include_payload,
        )
    )
    if error is not None:
        return error
    return jsonify(
        dossier(
            result,
            include_payload=include_payload,
            include_proposals=getattr(g, "access_role", None)
            in {"owner", "reviewer"},
        )
    )


def scholarly_create_reader_note(project_id: str, block_id: str):
    role_error = _role_required("owner", "reviewer")
    if role_error is not None:
        return role_error
    key = request.headers.get(IDEMPOTENCY_HEADER)
    if not isinstance(key, str) or not 1 <= len(key) <= _MAX_IDEMPOTENCY_KEY:
        return _error_response("idempotency-key-required", 400)
    body = _json_body_optional(
        required={"body", "anchor", "citation_ids"},
        optional={"state"},
        maximum_bytes=16 * 1024,
    )
    if not isinstance(body, dict):
        return body
    result, error = _call_service(
        lambda: scholarly_review.create_reader_note(
            g.project,
            block_id=block_id,
            body=body["body"],
            anchor=body["anchor"],
            citations=body["citation_ids"],
            state=body.get("state", "active"),
            actor=g.review_actor,
            idempotency_key=key,
        ),
        mutation=True,
        latest_projector=reader_note,
    )
    if error is not None:
        return error
    return jsonify({"note": reader_note(result)}), 201


def scholarly_revise_reader_note(project_id: str, note_id: str):
    role_error = _role_required("owner", "reviewer")
    if role_error is not None:
        return role_error
    key = request.headers.get(IDEMPOTENCY_HEADER)
    if not isinstance(key, str) or not 1 <= len(key) <= _MAX_IDEMPOTENCY_KEY:
        return _error_response("idempotency-key-required", 400)
    body = _json_body_optional(
        required={"expected_version"},
        optional={"body", "anchor", "citation_ids", "state"},
        maximum_bytes=16 * 1024,
    )
    if not isinstance(body, dict):
        return body
    result, error = _call_service(
        lambda: scholarly_review.revise_reader_note(
            g.project,
            note_id,
            expected_head_version=body["expected_version"],
            body=body.get("body"),
            anchor=body.get("anchor"),
            citations=body.get("citation_ids"),
            state=body.get("state"),
            actor=g.review_actor,
            idempotency_key=key,
        ),
        mutation=True,
        latest_projector=reader_note,
    )
    if error is not None:
        return error
    return jsonify({"note": reader_note(result)})


def scholarly_transition_reader_note(project_id: str, note_id: str):
    role_error = _role_required("owner", "reviewer")
    if role_error is not None:
        return role_error
    key = request.headers.get(IDEMPOTENCY_HEADER)
    if not isinstance(key, str) or not 1 <= len(key) <= _MAX_IDEMPOTENCY_KEY:
        return _error_response("idempotency-key-required", 400)
    body = _json_body(keys={"state", "expected_version"}, maximum_bytes=8 * 1024)
    if not isinstance(body, dict):
        return body
    result, error = _call_service(
        lambda: scholarly_review.transition_reader_note(
            g.project,
            note_id,
            expected_head_version=body["expected_version"],
            next_state=body["state"],
            actor=g.review_actor,
            idempotency_key=key,
        ),
        mutation=True,
        latest_projector=reader_note,
    )
    if error is not None:
        return error
    return jsonify({"note": reader_note(result)})


def scholarly_record_block_review(project_id: str, block_id: str):
    role_error = _role_required("owner", "reviewer")
    if role_error is not None:
        return role_error
    key = request.headers.get(IDEMPOTENCY_HEADER)
    if not isinstance(key, str) or not 1 <= len(key) <= _MAX_IDEMPOTENCY_KEY:
        return _error_response("idempotency-key-required", 400)
    body = _json_body(
        keys={"decision", "rationale", "expected_version", "expected_target_version"},
        maximum_bytes=16 * 1024,
    )
    if not isinstance(body, dict):
        return body
    result, error = _call_service(
        lambda: scholarly_review.record_block_review(
            g.project,
            block_id,
            body["decision"],
            body["rationale"],
            body["expected_version"],
            body["expected_target_version"],
            g.review_actor,
            idempotency_key=key,
        ),
        mutation=True,
        latest_projector=block_review,
    )
    if error is not None:
        return error
    return jsonify({"review": block_review(result)})


def scholarly_record_review_coverage(project_id: str, block_id: str):
    role_error = _role_required("owner", "reviewer")
    if role_error is not None:
        return role_error
    key = request.headers.get(IDEMPOTENCY_HEADER)
    if not isinstance(key, str) or not 1 <= len(key) <= _MAX_IDEMPOTENCY_KEY:
        return _error_response("idempotency-key-required", 400)
    body = _json_body_optional(
        required={"kind", "expected_target_version"},
        optional={"scope", "source_anchor_ids", "comment"}, maximum_bytes=16 * 1024,
    )
    if not isinstance(body, dict):
        return body
    from .review_coverage import record_review_coverage

    result, error = _call_service(
        lambda: record_review_coverage(
            g.project, entries=[{
                "block_id": block_id, "expected_target_version": body["expected_target_version"],
                "scope": body.get("scope", "block"), "source_anchor_ids": body.get("source_anchor_ids", []),
            }], kind=body["kind"], reviewer_kind="human", comment=body.get("comment", ""),
            actor=g.review_actor, idempotency_key=key,
        ), mutation=True,
    )
    if error is not None:
        return error
    from .block_workspace_api import current_block_projection

    return jsonify({"coverage": result, "block": current_block_projection(g.project, block_id)})


def scholarly_list_glossary_suggestions(project_id: str):
    role_error = _role_required("owner", "reviewer")
    if role_error is not None:
        return role_error
    include_resolved = request.args.get("include_resolved") == "1"
    if include_resolved and getattr(g, "access_role", None) != "owner":
        return _error_response("review-authority-required", 403)
    result, error = _call_service(
        lambda: scholarly_review.list_glossary_suggestions(
            g.project, include_resolved=include_resolved
        )
    )
    if error is not None:
        return error
    return jsonify({"suggestions": [glossary_suggestion(row) for row in result or []]})


def scholarly_create_glossary_suggestion(project_id: str):
    role_error = _role_required("reviewer")
    if role_error is not None:
        return role_error
    key = request.headers.get(IDEMPOTENCY_HEADER)
    if not isinstance(key, str) or not 1 <= len(key) <= _MAX_IDEMPOTENCY_KEY:
        return _error_response("idempotency-key-required", 400)
    body = _json_body(keys={"source_term", "target_term", "notes"}, maximum_bytes=16 * 1024)
    if not isinstance(body, dict):
        return body
    result, error = _call_service(
        lambda: scholarly_review.create_glossary_suggestion(
            g.project,
            body["source_term"],
            body["target_term"],
            body["notes"],
            g.review_actor,
            key,
        ),
        mutation=True,
    )
    if error is not None:
        return error
    return jsonify({"suggestion": glossary_suggestion(result)}), 201


def scholarly_resolve_glossary_suggestion(project_id: str, suggestion_id: str):
    role_error = _role_required("owner")
    if role_error is not None:
        return role_error
    body = _json_body_optional(
        required={"action", "expected_version"},
        optional={"source_term", "target_term", "notes"},
        maximum_bytes=16 * 1024,
    )
    if not isinstance(body, dict):
        return body
    if body["action"] not in {"approve", "reject"}:
        return _error_response("glossary-suggestion-decision", 400)
    approved = None
    if any(key in body for key in ("source_term", "target_term", "notes")):
        approved = {key: body.get(key) for key in ("source_term", "target_term", "notes")}
    result, error = _call_service(
        lambda: scholarly_review.resolve_glossary_suggestion(
            g.project,
            suggestion_id,
            body["expected_version"],
            "accepted" if body["action"] == "approve" else "rejected",
            approved_payload=approved,
            actor=g.review_actor,
        ),
        mutation=True,
        latest_projector=glossary_suggestion,
    )
    if error is not None:
        return error
    return jsonify({"suggestion": glossary_suggestion(result)})


def scholarly_list_retranslation_requests(project_id: str):
    role_error = _role_required("owner", "reviewer")
    if role_error is not None:
        return role_error
    result, error = _call_service(
        lambda: scholarly_review.list_retranslation_requests(g.project, actor=_read_actor())
    )
    if error is not None:
        return error
    return jsonify({"requests": [retranslation_request(row) for row in result or []]})


def scholarly_create_retranslation_request(project_id: str):
    role_error = _role_required("owner", "reviewer")
    if role_error is not None:
        return role_error
    key = request.headers.get(IDEMPOTENCY_HEADER)
    if not isinstance(key, str) or not 1 <= len(key) <= _MAX_IDEMPOTENCY_KEY:
        return _error_response("idempotency-key-required", 400)
    body = _json_body(
        keys={
            "scope",
            "custom_prompt",
            "annotation_profile",
            "research_choice",
            "dossier_revision_id",
            "rationale",
            "expected_source_plan_id",
            "expected_source_plan_version",
        },
        maximum_bytes=32 * 1024,
    )
    if not isinstance(body, dict):
        return body
    if not isinstance(body["expected_source_plan_id"], str) or (
        isinstance(body["expected_source_plan_version"], bool)
        or not isinstance(body["expected_source_plan_version"], int)
        or body["expected_source_plan_version"] < 1
    ):
        return _error_response("source-plan-shape", 400)
    try:
        plan = load_block_source_plan(g.project)
        if (
            body["expected_source_plan_id"] != plan.record.id
            or body["expected_source_plan_version"] != plan.active_version
        ):
            return _error_response("source-plan-conflict", 409)
    except BlockWorkspaceIntegrityError as exc:
        return _error_response(_safe_code(exc.code, "workspace-invalid"), 409)
    except Exception:  # noqa: BLE001 - source-plan failures are redacted.
        db.session.rollback()
        logger.error("Translator scholarly HTTP source-plan validation failure")
        return _error_response("service-unavailable", 503)
    result, error = _call_service(
        lambda: scholarly_review.create_retranslation_request(
            g.project,
            scope=body["scope"],
            custom_prompt=body["custom_prompt"],
            annotation_profile=body["annotation_profile"],
            research_choice=body["research_choice"],
            dossier_revision_id=body["dossier_revision_id"],
            rationale=body["rationale"],
            idempotency_key=key,
            actor=g.review_actor,
        ),
        mutation=True,
    )
    if error is not None:
        return error
    return jsonify({"request": retranslation_request(result)}), 201


def scholarly_attach_retranslation_dossier(project_id: str, request_id: str):
    """Attach the exact external dossier revision before an owner executes it."""

    role_error = _role_required("owner")
    if role_error is not None:
        return role_error
    body = _json_body(
        keys={"dossier_revision_id", "expected_version"}, maximum_bytes=2 * 1024
    )
    if not isinstance(body, dict):
        return body
    if (
        not isinstance(body["dossier_revision_id"], str)
        or isinstance(body["expected_version"], bool)
        or not isinstance(body["expected_version"], int)
        or body["expected_version"] < 1
    ):
        return _error_response("retranslation-request-shape", 400)
    result, error = _call_service(
        lambda: scholarly_review.attach_retranslation_dossier(
            g.project,
            request_id,
            dossier_revision_id=body["dossier_revision_id"],
            expected_version=body["expected_version"],
            actor=g.review_actor,
        ),
        mutation=True,
        latest_projector=retranslation_request,
    )
    if error is not None:
        return error
    return jsonify({"request": retranslation_request(result)})


def scholarly_document_review_outline(project_id: str):
    role_error = _role_required("owner", "reviewer", "viewer")
    if role_error is not None:
        return role_error
    cursor = request.args.get("cursor")
    raw_limit = request.args.get("limit", "100")
    try:
        limit = int(raw_limit)
    except (TypeError, ValueError):
        return _error_response("review-outline-limit", 400)
    result, error = _call_service(lambda: document_review.review_outline(g.project, cursor=cursor, limit=limit))
    if error is not None:
        return error
    return jsonify(result)


def scholarly_document_review_fetch(project_id: str):
    role_error = _role_required("owner", "reviewer", "viewer")
    if role_error is not None:
        return role_error
    body = _json_body_optional(
        required={
            "block_ids",
            "order_start",
            "order_end",
            "cursor",
            "limit",
            "include_research",
            "include_proposals",
        },
        optional={"include_alignment"},
        maximum_bytes=16 * 1024,
    )
    if not isinstance(body, dict):
        return body
    if body["include_proposals"] and getattr(g, "access_role", None) == "viewer":
        return _error_response("review-proposals-forbidden", 403)
    result, error = _call_service(
        lambda: document_review.review_fetch(
            g.project,
            block_ids=body["block_ids"],
            order_start=body["order_start"],
            order_end=body["order_end"],
            cursor=body["cursor"],
            limit=body["limit"],
            include_research=body["include_research"],
            include_proposals=body["include_proposals"],
            include_alignment=body.get("include_alignment", False),
        )
    )
    if error is not None:
        return error
    return jsonify(result)


def scholarly_document_review_search(project_id: str):
    body = _json_body(
        keys={"query", "fields", "roles", "before", "after", "cursor", "limit", "include_proposals"},
        maximum_bytes=16 * 1024,
    )
    if not isinstance(body, dict):
        return body
    if body["include_proposals"] and getattr(g, "access_role", None) == "viewer":
        return _error_response("review-proposals-forbidden", 403)
    result, error = _call_service(
        lambda: document_review.review_search(
            g.project,
            query=body["query"],
            fields=body["fields"],
            roles=body["roles"],
            before=body["before"],
            after=body["after"],
            cursor=body["cursor"],
            limit=body["limit"],
            include_proposals=body["include_proposals"],
        )
    )
    if error is not None:
        return error
    return jsonify(result)


def scholarly_document_review_explain(project_id: str):
    body = _json_body(keys={"block_ids", "question"}, maximum_bytes=16 * 1024)
    if not isinstance(body, dict):
        return body
    result, error = _call_service(
        lambda: document_review.explanation_evidence(
            g.project, block_ids=body["block_ids"], question=body["question"]
        )
    )
    if error is not None:
        return error
    return jsonify(result)


def register_scholarly_routes(blueprint: Blueprint) -> Blueprint:
    """Register the complete scholarly HTTP surface on a Translator blueprint."""

    routes: tuple[tuple[str, str, Callable[..., Any], tuple[str, ...]], ...] = (
        ("/api/share/redeem", "scholarly_redeem_share", scholarly_redeem_share, ("POST",)),
        ("/api/share/session", "scholarly_bootstrap_share_session", scholarly_bootstrap_share_session, ("POST",)),
        ("/api/projects/<project_id>/share-grants", "scholarly_list_share_grants", project_access_required(scholarly_list_share_grants), ("GET",)),
        ("/api/projects/<project_id>/share-grants", "scholarly_issue_share_grant", project_access_required(scholarly_issue_share_grant), ("POST",)),
        ("/api/projects/<project_id>/share-grants/<grant_id>", "scholarly_revoke_share_grant", project_access_required(scholarly_revoke_share_grant), ("DELETE",)),
        ("/api/projects/<project_id>/research-dossiers", "scholarly_list_research_dossiers", project_access_required(scholarly_list_research_dossiers), ("GET",)),
        ("/api/projects/<project_id>/research-dossiers", "scholarly_submit_research_dossier", project_access_required(scholarly_submit_research_dossier), ("POST",)),
        ("/api/projects/<project_id>/research-dossiers/<dossier_id>", "scholarly_get_research_dossier", project_access_required(scholarly_get_research_dossier), ("GET",)),
        ("/api/projects/<project_id>/blocks/<block_id>/reader-notes", "scholarly_create_reader_note", project_access_required(scholarly_create_reader_note), ("POST",)),
        ("/api/projects/<project_id>/reader-notes/<note_id>", "scholarly_revise_reader_note", project_access_required(scholarly_revise_reader_note), ("PUT",)),
        ("/api/projects/<project_id>/reader-notes/<note_id>/state", "scholarly_transition_reader_note", project_access_required(scholarly_transition_reader_note), ("POST",)),
        ("/api/projects/<project_id>/blocks/<block_id>/review", "scholarly_record_block_review", project_access_required(scholarly_record_block_review), ("PUT",)),
        ("/api/projects/<project_id>/blocks/<block_id>/review-coverage", "scholarly_record_review_coverage", project_access_required(scholarly_record_review_coverage), ("POST",)),
        ("/api/projects/<project_id>/glossary-suggestions", "scholarly_list_glossary_suggestions", project_access_required(scholarly_list_glossary_suggestions), ("GET",)),
        ("/api/projects/<project_id>/glossary-suggestions", "scholarly_create_glossary_suggestion", project_access_required(scholarly_create_glossary_suggestion), ("POST",)),
        ("/api/projects/<project_id>/glossary-suggestions/<suggestion_id>/resolve", "scholarly_resolve_glossary_suggestion", project_access_required(scholarly_resolve_glossary_suggestion), ("POST",)),
        ("/api/projects/<project_id>/retranslation-requests", "scholarly_list_retranslation_requests", project_access_required(scholarly_list_retranslation_requests), ("GET",)),
        ("/api/projects/<project_id>/retranslation-requests", "scholarly_create_retranslation_request", project_access_required(scholarly_create_retranslation_request), ("POST",)),
        ("/api/projects/<project_id>/retranslation-requests/<request_id>/dossier", "scholarly_attach_retranslation_dossier", project_access_required(scholarly_attach_retranslation_dossier), ("POST",)),
        ("/api/projects/<project_id>/document-review/outline", "scholarly_document_review_outline", project_access_required(scholarly_document_review_outline), ("GET",)),
        ("/api/projects/<project_id>/document-review/fetch", "scholarly_document_review_fetch", project_access_required(scholarly_document_review_fetch), ("POST",)),
        ("/api/projects/<project_id>/document-review/search", "scholarly_document_review_search", project_access_required(scholarly_document_review_search), ("POST",)),
        ("/api/projects/<project_id>/document-review/explain", "scholarly_document_review_explain", project_access_required(scholarly_document_review_explain), ("POST",)),
    )
    for rule, endpoint, view, methods in routes:
        blueprint.add_url_rule(
            rule,
            endpoint=endpoint,
            view_func=cast(Any, view),
            methods=list(methods),
        )
    return blueprint


__all__ = [
    "COOKIE_HTTP_ONLY",
    "COOKIE_PATH",
    "COOKIE_SAMESITE",
    "CSRF_HEADER",
    "CSRF_HEADER_NAME",
    "IDEMPOTENCY_HEADER",
    "LEGACY_SHARE_TOKEN_HEADER",
    "REVIEW_SESSION_COOKIE_HTTPONLY",
    "REVIEW_SESSION_COOKIE_PATH",
    "REVIEW_SESSION_COOKIE_SAMESITE",
    "SESSION_COOKIE_HTTPONLY",
    "SESSION_COOKIE_HTTP_ONLY",
    "SESSION_COOKIE_NAME_PREFIX",
    "SESSION_COOKIE_PATH",
    "SESSION_COOKIE_SAMESITE",
    "SHARE_TOKEN_HEADER",
    "SHARE_TOKEN_HEADER_NAME",
    "TRANSLATOR_CSRF_HEADER",
    "TRANSLATOR_SHARE_TOKEN_HEADER",
    "project_access_required",
    "register_scholarly_routes",
    "review_session_cookie_name",
]
