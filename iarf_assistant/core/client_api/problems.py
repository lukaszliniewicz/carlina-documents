"""Small RFC 9457 response helpers for the Compass client gateway."""

from __future__ import annotations

import re
import uuid
from typing import Final

from flask import Response, g, jsonify

_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_MAX_TITLE_LENGTH: Final = 256
_MAX_DETAIL_LENGTH: Final = 1024

_TITLES: Final = {
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
    409: "Conflict",
    413: "Payload Too Large",
    415: "Unsupported Media Type",
    422: "Unprocessable Content",
    429: "Too Many Requests",
    503: "Service Unavailable",
}
_BEARER_CHALLENGES: Final = {
    (401, "invalid_client_token"): 'Bearer realm="carlina-compass"',
    (403, "insufficient_scope"): (
        'Bearer realm="carlina-compass", error="insufficient_scope", '
        'scope="compass.capture"'
    ),
}


class CompassClientProblem(Exception):
    """A stable boundary failure rendered as an RFC 9457 problem response."""

    def __init__(
        self,
        status_code: int,
        code: str,
        detail: str,
        *,
        retryable: bool = False,
    ) -> None:
        super().__init__(code)
        self.status_code = status_code
        self.code = code
        self.detail = detail
        self.retryable = retryable


def compass_problem_response(
    *,
    status_code: int,
    code: str,
    detail: str,
    retryable: bool = False,
) -> Response:
    """Return a bounded, cache-safe Compass RFC 9457 problem response."""

    status = _safe_status(status_code)
    safe_code = (
        code if isinstance(code, str) and _CODE.fullmatch(code) else "gateway_error"
    )
    safe_detail = _safe_text(
        detail,
        limit=_MAX_DETAIL_LENGTH,
        fallback="The Compass client request could not be completed.",
    )
    response = jsonify(
        {
            "type": f"urn:carlina:problem:{safe_code}",
            "title": _TITLES.get(status, "Compass Client Error")[:_MAX_TITLE_LENGTH],
            "status": status,
            "detail": safe_detail,
            "code": safe_code,
            "retryable": bool(retryable),
            "requestId": compass_request_id(),
        }
    )
    response.status_code = status
    response.content_type = "application/problem+json"
    challenge = _BEARER_CHALLENGES.get((status, safe_code))
    if challenge is not None:
        response.headers["WWW-Authenticate"] = challenge
    return no_store(response)


def compass_request_id() -> str:
    """Return the app request id or a safe fallback for isolated route tests."""

    request_id = getattr(g, "request_id", None)
    if isinstance(request_id, str) and _REQUEST_ID.fullmatch(request_id):
        return request_id
    fallback = getattr(g, "_compass_client_request_id", None)
    if isinstance(fallback, str) and fallback:
        return fallback
    fallback = uuid.uuid4().hex
    g._compass_client_request_id = fallback
    return fallback


def no_store(response: Response) -> Response:
    """Prevent persisted copies of owner-scoped capture responses and errors."""

    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    response.headers["X-Request-ID"] = compass_request_id()
    return response


def _safe_status(value: int) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and 400 <= value <= 599:
        return value
    return 503


def _safe_text(value: object, *, limit: int, fallback: str) -> str:
    if not isinstance(value, str) or not value:
        return fallback
    return value[:limit]


__all__ = [
    "CompassClientProblem",
    "compass_problem_response",
    "compass_request_id",
    "no_store",
]
