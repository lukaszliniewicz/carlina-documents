"""Opaque Bearer-token principal resolution for Compass client routes."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final, TypeGuard

from flask import current_app, request

from .problems import CompassClientProblem

COMPASS_CLIENT_PRINCIPAL_RESOLVER_EXTENSION: Final = (
    "carlina.compass_client_principal_resolver"
)
"""Application-extension key for the Compass opaque-token resolver callable."""

COMPASS_CAPTURE_SCOPE: Final = "compass.capture"
MAX_BEARER_TOKEN_LENGTH: Final = 4096

_BEARER_TOKEN = re.compile(r"^[A-Za-z0-9\-._~+/]+=*$")
_DEVICE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SCOPE = re.compile(r"^[A-Za-z][A-Za-z0-9._:-]{0,127}$")


@dataclass(frozen=True, slots=True)
class CompassClientPrincipal:
    """Trusted owner and device scope returned by the token resolver."""

    owner_user_id: int
    device_id: str
    scopes: frozenset[str]


class CompassClientPrincipalDenied(Exception):
    """A resolver's safe, typed refusal of a Compass client token."""

    def __init__(self, status_code: int):
        super().__init__(status_code)
        self.status_code = status_code


def resolve_compass_client_principal() -> CompassClientPrincipal:
    """Resolve a valid opaque Bearer token without consulting browser sessions."""

    token = _bearer_token()
    resolver = current_app.extensions.get(COMPASS_CLIENT_PRINCIPAL_RESOLVER_EXTENSION)
    if not callable(resolver):
        raise _authentication_unavailable()

    try:
        principal = resolver(token)
    except CompassClientPrincipalDenied as error:
        raise _denial_problem(error) from None
    except Exception as error:  # noqa: BLE001 - third-party resolver boundary
        current_app.logger.warning(
            "Compass client principal resolver failed: %s", type(error).__name__
        )
        raise _authentication_unavailable() from None

    if isinstance(principal, CompassClientPrincipalDenied):
        raise _denial_problem(principal)
    if not _is_valid_principal(principal):
        current_app.logger.warning(
            "Compass client principal resolver returned invalid output"
        )
        raise _authentication_unavailable()
    if COMPASS_CAPTURE_SCOPE not in principal.scopes:
        raise CompassClientProblem(
            403,
            "insufficient_scope",
            "The token does not grant Compass capture access.",
        )
    return principal


def _bearer_token() -> str:
    authorization = request.headers.get("Authorization")
    if not isinstance(authorization, str):
        raise _invalid_token()
    match = re.fullmatch(r"(?i:Bearer) ([A-Za-z0-9\-._~+/]+=*)", authorization)
    if match is None:
        raise _invalid_token()
    token = match.group(1)
    if len(token) > MAX_BEARER_TOKEN_LENGTH or not _BEARER_TOKEN.fullmatch(token):
        raise _invalid_token()
    return token


def _is_valid_principal(value: object) -> TypeGuard[CompassClientPrincipal]:
    if not isinstance(value, CompassClientPrincipal):
        return False
    if (
        not isinstance(value.owner_user_id, int)
        or isinstance(value.owner_user_id, bool)
        or value.owner_user_id < 1
    ):
        return False
    if not isinstance(value.device_id, str) or not _DEVICE_ID.fullmatch(
        value.device_id
    ):
        return False
    if not isinstance(value.scopes, frozenset) or not value.scopes:
        return False
    return all(
        isinstance(scope, str) and _SCOPE.fullmatch(scope) for scope in value.scopes
    )


def _denial_problem(error: CompassClientPrincipalDenied) -> CompassClientProblem:
    if error.status_code == 401:
        return _invalid_token()
    if error.status_code == 403:
        return CompassClientProblem(
            403,
            "client_access_denied",
            "The token is not authorized for Compass client access.",
        )
    if error.status_code == 503:
        return _authentication_unavailable()
    current_app.logger.warning(
        "Compass client principal resolver returned unsupported denial status: %s",
        type(error.status_code).__name__,
    )
    return _authentication_unavailable()


def _invalid_token() -> CompassClientProblem:
    return CompassClientProblem(
        401,
        "invalid_client_token",
        "A valid Compass client Bearer token is required.",
    )


def _authentication_unavailable() -> CompassClientProblem:
    return CompassClientProblem(
        503,
        "client_auth_unavailable",
        "Compass client authentication is temporarily unavailable.",
        retryable=True,
    )


__all__ = [
    "COMPASS_CAPTURE_SCOPE",
    "COMPASS_CLIENT_PRINCIPAL_RESOLVER_EXTENSION",
    "MAX_BEARER_TOKEN_LENGTH",
    "CompassClientPrincipal",
    "CompassClientPrincipalDenied",
    "resolve_compass_client_principal",
]
