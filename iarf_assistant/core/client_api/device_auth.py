"""Core-owned OAuth-style authorization for Compass native capture clients.

The implementation deliberately keeps the browser-session consent surface and
the CSRF-exempt token surface separate.  Native clients receive only opaque
short-lived access and rotating refresh tokens; the database contains keyed
digests, never the raw values returned to a loopback callback or token caller.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import math
import re
import secrets
import sqlite3
import threading
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final
from urllib.parse import urlencode, urlsplit, urlunsplit

import sqlalchemy as sa
from flask import Flask, current_app
from sqlalchemy.exc import SQLAlchemyError

from iarf_assistant.core.persistence.sqlite import (
    configure_sqlite_connection,
    sqlite_busy_timeout_ms,
)
from iarf_assistant.models import (
    CompassClientAccessToken,
    CompassClientAuthEvent,
    CompassClientAuthorizationCode,
    CompassClientDevice,
    CompassClientRefreshToken,
)

from .principal import (
    COMPASS_CAPTURE_SCOPE,
    COMPASS_CLIENT_PRINCIPAL_RESOLVER_EXTENSION,
    CompassClientPrincipal,
    CompassClientPrincipalDenied,
)

COMPASS_CLIENT_AUTH_SERVICE_EXTENSION: Final = "carlina.compass_client_auth_service"
COMPASS_CLIENT_TOKEN_LIMITER_EXTENSION: Final = "carlina.compass_client_token_limiter"
COMPASS_CLIENT_ID: Final = "carlina-compass-native"
AUTHORIZATION_CODE_TTL: Final = timedelta(minutes=5)
ACCESS_TOKEN_TTL: Final = timedelta(minutes=15)
REFRESH_TOKEN_TTL: Final = timedelta(days=30)
LAST_USED_WRITE_INTERVAL: Final = timedelta(minutes=5)
MAX_STATE_LENGTH: Final = 512
MAX_REDIRECT_URI_LENGTH: Final = 512
MAX_DEVICE_NAME_LENGTH: Final = 96
MAX_PLATFORM_LENGTH: Final = 32
MAX_TOKEN_FORM_VALUE_LENGTH: Final = 4096
TOKEN_RATE_LIMIT: Final = 30
TOKEN_RATE_WINDOW_SECONDS: Final = 60
TOKEN_RATE_RETENTION_SECONDS: Final = 24 * 60 * 60
TOKEN_RATE_MAX_SOURCES: Final = 4_096

_STATE = re.compile(r"^[A-Za-z0-9._~-]{1,512}$")
_PLATFORM = re.compile(r"^[a-z][a-z0-9._-]{0,31}$")
_PKCE_CHALLENGE = re.compile(r"^[A-Za-z0-9_-]{43,128}$")
_PKCE_VERIFIER = re.compile(r"^[A-Za-z0-9._~-]{43,128}$")
_OPAQUE_SECRET = re.compile(r"^[A-Za-z0-9_-]{43,256}$")


@dataclass(frozen=True, slots=True)
class CompassClientAuthorizationRequest:
    """Validated browser-consent request; all values are safe to echo in HTML."""

    redirect_uri: str
    state: str
    code_challenge: str
    device_name: str
    platform: str


@dataclass(frozen=True, slots=True)
class CompassClientTokenPair:
    access_token: str
    refresh_token: str
    expires_in: int


@dataclass(frozen=True, slots=True)
class _CompassClientPrincipalSnapshot:
    """Minimal values required to revalidate a bearer without ORM cache state."""

    owner_user_id: int
    device_id: str
    expires_at: datetime
    last_used_at: datetime | None


class CompassClientAuthError(Exception):
    """Safe device-auth failure rendered by the route boundary."""

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


class CompassTokenExchangeLimiter:
    """A cross-process SQLite fixed-window limiter with no raw client IDs."""

    def __init__(
        self,
        database_path: str | Path,
        secret: bytes,
        *,
        limit: int = TOKEN_RATE_LIMIT,
        window_seconds: int = TOKEN_RATE_WINDOW_SECONDS,
        retention_seconds: int = TOKEN_RATE_RETENTION_SECONDS,
        max_sources: int = TOKEN_RATE_MAX_SOURCES,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if len(secret) < 16:
            raise ValueError("Compass token limiter secret must contain 16 bytes.")
        self.database_path = Path(database_path).expanduser().resolve()
        self.secret = bytes(secret)
        self.limit = int(limit)
        self.window_seconds = int(window_seconds)
        self.retention_seconds = int(retention_seconds)
        self.max_sources = int(max_sources)
        self.clock = clock or __import__("time").time
        self._schema_lock = threading.Lock()
        self._initialized = False

    def consume(self, source: str | None, *, now: float | None = None) -> int | None:
        """Record one exchange and return retry seconds when the bucket is full."""

        if (
            self.limit < 1
            or self.window_seconds < 1
            or self.retention_seconds < self.window_seconds
            or self.max_sources < 1
        ):
            raise RuntimeError("Compass token limiter has unsafe configuration.")
        self._ensure_schema()
        timestamp = float(self.clock() if now is None else now)
        source_hash = self._digest(str(source or "unknown"))
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._prune(connection, timestamp)
                row = connection.execute(
                    "SELECT window_started, attempt_count FROM compass_token_rate_limit "
                    "WHERE source_hash = ?",
                    (source_hash,),
                ).fetchone()
                if (
                    row is None
                    or timestamp >= float(row["window_started"]) + self.window_seconds
                ):
                    connection.execute(
                        "INSERT INTO compass_token_rate_limit "
                        "(source_hash, window_started, attempt_count) VALUES (?, ?, 1) "
                        "ON CONFLICT(source_hash) DO UPDATE SET "
                        "window_started = excluded.window_started, attempt_count = 1",
                        (source_hash, timestamp),
                    )
                    self._enforce_source_cap(connection, source_hash)
                    connection.commit()
                    return None
                attempt_count = int(row["attempt_count"])
                retry_after = max(
                    1,
                    math.ceil(
                        float(row["window_started"]) + self.window_seconds - timestamp
                    ),
                )
                if attempt_count >= self.limit:
                    connection.commit()
                    return retry_after
                connection.execute(
                    "UPDATE compass_token_rate_limit SET attempt_count = ? "
                    "WHERE source_hash = ?",
                    (attempt_count + 1, source_hash),
                )
                self._enforce_source_cap(connection, source_hash)
                connection.commit()
                return None
        except sqlite3.Error as error:
            raise RuntimeError("Compass token rate limiter is unavailable.") from error

    def _digest(self, source: str) -> str:
        return hmac.new(
            self.secret,
            b"compass-token-rate-limit\0" + source.encode("utf-8", "replace"),
            hashlib.sha256,
        ).hexdigest()

    def _connect(self) -> sqlite3.Connection:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            self.database_path,
            timeout=sqlite_busy_timeout_ms() / 1_000,
        )
        configure_sqlite_connection(connection)
        connection.row_factory = sqlite3.Row
        return connection

    def _prune(self, connection: sqlite3.Connection, timestamp: float) -> None:
        """Prune expired windows and bound retained source digests in this write."""

        connection.execute(
            "DELETE FROM compass_token_rate_limit WHERE window_started < ?",
            (timestamp - self.retention_seconds,),
        )

    def _enforce_source_cap(
        self, connection: sqlite3.Connection, protected_hash: str
    ) -> None:
        count = int(
            connection.execute(
                "SELECT count(*) FROM compass_token_rate_limit"
            ).fetchone()[0]
        )
        excess = count - self.max_sources
        if excess <= 0:
            return
        connection.execute(
            "DELETE FROM compass_token_rate_limit WHERE source_hash IN ("
            "SELECT source_hash FROM compass_token_rate_limit "
            "WHERE source_hash != ? ORDER BY window_started ASC, source_hash ASC LIMIT ?"
            ")",
            (protected_hash, excess),
        )

    def _ensure_schema(self) -> None:
        if self._initialized:
            return
        with self._schema_lock:
            if self._initialized:
                return
            with self._connect() as connection:
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS compass_token_rate_limit ("
                    "source_hash TEXT PRIMARY KEY, "
                    "window_started REAL NOT NULL, "
                    "attempt_count INTEGER NOT NULL "
                    "CHECK (attempt_count BETWEEN 1 AND 2147483647)"
                    ")"
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS ix_compass_token_rate_limit_window "
                    "ON compass_token_rate_limit (window_started)"
                )
            self._initialized = True


class CompassClientAuthService:
    """Issue, rotate, resolve, and revoke opaque Compass device credentials."""

    def __init__(
        self,
        secret: str | bytes,
        *,
        clock: Callable[[], datetime] | None = None,
        secret_factory: Callable[[int], str] | None = None,
        after_initial_principal_read: Callable[[], None] | None = None,
    ) -> None:
        material = secret.encode("utf-8") if isinstance(secret, str) else bytes(secret)
        if len(material) < 16:
            raise ValueError(
                "Compass client authorization secret must contain 16 bytes."
            )
        self._secret = material
        self._clock = clock or (lambda: datetime.now(UTC))
        self._secret_factory = secret_factory or secrets.token_urlsafe
        self._after_initial_principal_read = after_initial_principal_read

    def parse_authorization_request(
        self, values: Mapping[str, object]
    ) -> CompassClientAuthorizationRequest:
        """Validate the only supported browser-consent request shape."""

        expected = {
            "response_type",
            "client_id",
            "redirect_uri",
            "state",
            "code_challenge",
            "code_challenge_method",
            "scope",
            "device_name",
            "platform",
        }
        if set(values) != expected:
            raise _invalid_authorization_request()
        if (
            values.get("response_type") != "code"
            or values.get("client_id") != COMPASS_CLIENT_ID
        ):
            raise _invalid_authorization_request()
        if values.get("scope") != COMPASS_CAPTURE_SCOPE:
            raise _invalid_authorization_request()
        if values.get("code_challenge_method") != "S256":
            raise _invalid_authorization_request()

        redirect_uri = _validate_loopback_redirect(values.get("redirect_uri"))
        state = values.get("state")
        challenge = values.get("code_challenge")
        device_name = values.get("device_name")
        platform = values.get("platform")
        if not isinstance(state, str) or _STATE.fullmatch(state) is None:
            raise _invalid_authorization_request()
        if (
            not isinstance(challenge, str)
            or _PKCE_CHALLENGE.fullmatch(challenge) is None
        ):
            raise _invalid_authorization_request()
        if (
            not isinstance(device_name, str)
            or not 1 <= len(device_name.strip()) <= MAX_DEVICE_NAME_LENGTH
            or any(
                ord(character) < 32 or ord(character) == 127
                for character in device_name
            )
        ):
            raise _invalid_authorization_request()
        if not isinstance(platform, str) or _PLATFORM.fullmatch(platform) is None:
            raise _invalid_authorization_request()
        return CompassClientAuthorizationRequest(
            redirect_uri=redirect_uri,
            state=state,
            code_challenge=challenge,
            device_name=device_name.strip(),
            platform=platform,
        )

    def approve(
        self, owner_user_id: int, request: CompassClientAuthorizationRequest
    ) -> str:
        """Create an active device and one short-lived, one-time PKCE code."""

        now = self.now()
        device_id = self._new_device_id()
        code = self._new_opaque_secret()
        device = CompassClientDevice(
            device_id=device_id,
            owner_user_id=_positive_owner(owner_user_id),
            display_name=request.device_name,
            platform=request.platform,
            scope=COMPASS_CAPTURE_SCOPE,
            status="active",
            created_at=now,
        )
        authorization_code = CompassClientAuthorizationCode(
            code_hash=self.digest("authorization-code", code),
            device_id=device_id,
            redirect_uri=request.redirect_uri,
            code_challenge=request.code_challenge,
            scope=COMPASS_CAPTURE_SCOPE,
            issued_at=now,
            expires_at=now + AUTHORIZATION_CODE_TTL,
        )
        try:
            from iarf_assistant.extensions import db

            db.session.add(device)
            # The models deliberately avoid an ORM relationship because the
            # authorization record never needs decrypted device metadata.  A
            # flush still makes their FK ordering explicit within this one
            # transaction on SQLite and every other supported backend.
            db.session.flush()
            db.session.add(authorization_code)
            self._record_event(device.owner_user_id, device.device_id, "approved", now)
            db.session.commit()
        except SQLAlchemyError as error:
            from iarf_assistant.extensions import db

            db.session.rollback()
            raise _auth_unavailable() from error
        return code

    def exchange_authorization_code(
        self, *, code: object, redirect_uri: object, code_verifier: object
    ) -> CompassClientTokenPair:
        """Consume an authorization code once after exact redirect and PKCE checks."""

        if not isinstance(code, str) or _OPAQUE_SECRET.fullmatch(code) is None:
            raise _invalid_grant()
        if (
            not isinstance(code_verifier, str)
            or _PKCE_VERIFIER.fullmatch(code_verifier) is None
        ):
            raise _invalid_grant()
        try:
            canonical_redirect = _validate_loopback_redirect(redirect_uri)
        except CompassClientAuthError as error:
            raise _invalid_grant() from error
        from iarf_assistant.extensions import db

        now = self.now()
        try:
            record = db.session.scalar(
                sa.select(CompassClientAuthorizationCode).where(
                    CompassClientAuthorizationCode.code_hash
                    == self.digest("authorization-code", code)
                )
            )
            if record is None:
                raise _invalid_grant()
            if record.consumed_at is not None:
                raise _invalid_grant()
            if _before_or_equal(record.expires_at, now):
                raise _invalid_grant()
            if not hmac.compare_digest(record.redirect_uri, canonical_redirect):
                raise _invalid_grant()
            if not hmac.compare_digest(
                _pkce_s256(code_verifier), record.code_challenge
            ):
                raise _invalid_grant()
            device = db.session.get(CompassClientDevice, record.device_id)
            if device is None or not _device_active(device, now):
                raise _invalid_grant()
            # SQLite stores aware SQLAlchemy datetimes as text.  A bound aware
            # value can compare lexically differently there, so the prior
            # Python expiry check is authoritative and the conditional update
            # only races the one-time ``consumed_at`` transition.
            consumed = db.session.execute(
                sa.update(CompassClientAuthorizationCode)
                .where(
                    CompassClientAuthorizationCode.id == record.id,
                    CompassClientAuthorizationCode.consumed_at.is_(None),
                )
                .values(consumed_at=now)
            )
            if int(getattr(consumed, "rowcount", -1)) != 1:
                db.session.rollback()
                raise _invalid_grant()
            pair = self._issue_token_pair(device.device_id, now)
            self._record_event(
                device.owner_user_id, device.device_id, "code_exchanged", now
            )
            db.session.commit()
            return pair
        except CompassClientAuthError:
            db.session.rollback()
            raise
        except SQLAlchemyError as error:
            db.session.rollback()
            raise _auth_unavailable() from error

    def exchange_refresh_token(
        self, *, refresh_token: object
    ) -> CompassClientTokenPair:
        """Rotate one refresh token atomically; duplicate use revokes the device."""

        if (
            not isinstance(refresh_token, str)
            or _OPAQUE_SECRET.fullmatch(refresh_token) is None
        ):
            raise _invalid_grant()
        from iarf_assistant.extensions import db

        now = self.now()
        token_hash = self.digest("refresh-token", refresh_token)
        try:
            original = db.session.get(CompassClientRefreshToken, token_hash)
            if original is None:
                raise _invalid_grant()
            # A previously rotated predecessor is a replay even after its
            # original expiry.  Check this before the ordinary expiry refusal:
            # a still-valid descendant must not survive evidence of theft.
            if original.used_at is not None or original.replaced_by_hash is not None:
                device_id = original.device_id
                db.session.rollback()
                self._revoke_device_after_refresh_reuse(device_id, now)
                raise _invalid_grant()
            device = db.session.get(CompassClientDevice, original.device_id)
            if (
                device is None
                or not _device_active(device, now)
                or original.revoked_at is not None
                or _before_or_equal(original.expires_at, now)
            ):
                raise _invalid_grant()
            replacement_refresh = self._new_opaque_secret()
            replacement_hash = self.digest("refresh-token", replacement_refresh)
            rotated = db.session.execute(
                sa.update(CompassClientRefreshToken)
                .where(
                    CompassClientRefreshToken.token_hash == token_hash,
                    CompassClientRefreshToken.used_at.is_(None),
                    CompassClientRefreshToken.revoked_at.is_(None),
                )
                .values(used_at=now, replaced_by_hash=replacement_hash)
            )
            if int(getattr(rotated, "rowcount", -1)) != 1:
                db.session.rollback()
                self._revoke_device_after_refresh_reuse(original.device_id, now)
                raise _invalid_grant()
            pair = self._issue_token_pair(
                device.device_id,
                now,
                refresh_token=replacement_refresh,
                refresh_hash=replacement_hash,
            )
            self._record_event(
                device.owner_user_id, device.device_id, "refresh_rotated", now
            )
            db.session.commit()
            return pair
        except CompassClientAuthError:
            db.session.rollback()
            raise
        except SQLAlchemyError as error:
            db.session.rollback()
            raise _auth_unavailable() from error

    def resolve_principal(
        self, raw_access_token: str
    ) -> CompassClientPrincipal | CompassClientPrincipalDenied:
        """Resolve a principal and revalidate it immediately before return.

        The first read decides whether a bounded ``last_used_at`` write is due.
        It is never trusted as the final authorization decision: a revoke can
        commit between that read and return, including on the no-write path.
        """

        from iarf_assistant.extensions import db

        now = self.now()
        token_hash = self.digest("access-token", raw_access_token)
        try:
            initial = self._principal_snapshot(token_hash)
            if initial is None or _before_or_equal(initial.expires_at, now):
                db.session.rollback()
                return CompassClientPrincipalDenied(401)
            # Release SQLite's read snapshot before the deterministic
            # interleaving seam and before a potential concurrent revocation.
            db.session.rollback()
            if self._after_initial_principal_read is not None:
                self._after_initial_principal_read()
            if _last_used_is_stale(initial.last_used_at, now):
                updated = db.session.execute(
                    sa.update(CompassClientDevice)
                    .where(
                        CompassClientDevice.device_id == initial.device_id,
                        CompassClientDevice.status == "active",
                        CompassClientDevice.revoked_at.is_(None),
                    )
                    .values(last_used_at=now)
                )
                if int(getattr(updated, "rowcount", -1)) != 1:
                    db.session.rollback()
                    return CompassClientPrincipalDenied(401)
                db.session.commit()
            # This joined query is the final authorization decision on both
            # paths. It sees committed device/token revocation state without
            # relying on an ORM identity-map object from the earlier read.
            final = self._principal_snapshot(token_hash)
            if final is None or _before_or_equal(final.expires_at, now):
                db.session.rollback()
                return CompassClientPrincipalDenied(401)
            db.session.rollback()
            return CompassClientPrincipal(
                final.owner_user_id,
                final.device_id,
                frozenset({COMPASS_CAPTURE_SCOPE}),
            )
        except SQLAlchemyError:
            db.session.rollback()
            return CompassClientPrincipalDenied(503)

    def list_devices(self, owner_user_id: int) -> list[CompassClientDevice]:
        """Return an owner's own device records, newest first."""

        from iarf_assistant.extensions import db

        return list(
            db.session.scalars(
                sa.select(CompassClientDevice)
                .where(
                    CompassClientDevice.owner_user_id == _positive_owner(owner_user_id)
                )
                .order_by(
                    CompassClientDevice.created_at.desc(), CompassClientDevice.device_id
                )
            )
        )

    def revoke_device(self, owner_user_id: int, device_id: object) -> bool:
        """Revoke a device for its owner, invalidating all existing credentials."""

        if not isinstance(device_id, str) or not _is_uuid7(device_id):
            return False
        from iarf_assistant.extensions import db

        now = self.now()
        try:
            device = db.session.get(CompassClientDevice, device_id)
            if device is None or int(device.owner_user_id) != _positive_owner(
                owner_user_id
            ):
                return False
            self._revoke_device_tokens(device.device_id, now, event="revoked")
            db.session.commit()
            return True
        except SQLAlchemyError as error:
            db.session.rollback()
            raise _auth_unavailable() from error

    def now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def digest(self, purpose: str, value: str) -> str:
        return hmac.new(
            self._secret,
            f"compass-client/{purpose}\0{value}".encode(),
            hashlib.sha256,
        ).hexdigest()

    def _new_opaque_secret(self) -> str:
        value = self._secret_factory(48)
        if not isinstance(value, str) or _OPAQUE_SECRET.fullmatch(value) is None:
            raise RuntimeError(
                "Compass opaque-token generator returned invalid material."
            )
        return value

    @staticmethod
    def _new_device_id() -> str:
        return str(uuid.uuid7())

    def _issue_token_pair(
        self,
        device_id: str,
        now: datetime,
        *,
        refresh_token: str | None = None,
        refresh_hash: str | None = None,
    ) -> CompassClientTokenPair:
        """Stage a new pair in the current transaction; caller commits atomically."""

        access_token = self._new_opaque_secret()
        refresh_value = refresh_token or self._new_opaque_secret()
        access_hash = self.digest("access-token", access_token)
        refresh_digest = refresh_hash or self.digest("refresh-token", refresh_value)
        from iarf_assistant.extensions import db

        db.session.add_all(
            (
                CompassClientAccessToken(
                    token_hash=access_hash,
                    device_id=device_id,
                    issued_at=now,
                    expires_at=now + ACCESS_TOKEN_TTL,
                ),
                CompassClientRefreshToken(
                    token_hash=refresh_digest,
                    device_id=device_id,
                    issued_at=now,
                    expires_at=now + REFRESH_TOKEN_TTL,
                ),
            )
        )
        return CompassClientTokenPair(
            access_token=access_token,
            refresh_token=refresh_value,
            expires_in=int(ACCESS_TOKEN_TTL.total_seconds()),
        )

    @staticmethod
    def _principal_snapshot(token_hash: str) -> _CompassClientPrincipalSnapshot | None:
        """Read active token/device state together, bypassing ORM cache objects."""

        from iarf_assistant.extensions import db

        row = db.session.execute(
            sa.select(
                CompassClientDevice.owner_user_id,
                CompassClientDevice.device_id,
                CompassClientAccessToken.expires_at,
                CompassClientDevice.last_used_at,
            )
            .join(
                CompassClientDevice,
                CompassClientDevice.device_id == CompassClientAccessToken.device_id,
            )
            .where(
                CompassClientAccessToken.token_hash == token_hash,
                CompassClientAccessToken.revoked_at.is_(None),
                CompassClientDevice.status == "active",
                CompassClientDevice.revoked_at.is_(None),
                CompassClientDevice.scope == COMPASS_CAPTURE_SCOPE,
            )
        ).one_or_none()
        if row is None:
            return None
        return _CompassClientPrincipalSnapshot(
            owner_user_id=int(row.owner_user_id),
            device_id=str(row.device_id),
            expires_at=row.expires_at,
            last_used_at=row.last_used_at,
        )

    def _revoke_device_after_refresh_reuse(self, device_id: str, now: datetime) -> None:
        """Fail closed after a consumed refresh token is presented again."""

        from iarf_assistant.extensions import db

        try:
            self._revoke_device_tokens(device_id, now, event="refresh_reuse_revoked")
            db.session.commit()
        except SQLAlchemyError as error:
            db.session.rollback()
            raise _auth_unavailable() from error

    @staticmethod
    def _revoke_device_tokens(device_id: str, now: datetime, *, event: str) -> bool:
        """Revoke one active family once and record only the winning transition."""

        from iarf_assistant.extensions import db

        device = db.session.get(CompassClientDevice, device_id)
        transitioned = db.session.execute(
            sa.update(CompassClientDevice)
            .where(
                CompassClientDevice.device_id == device_id,
                CompassClientDevice.status == "active",
                CompassClientDevice.revoked_at.is_(None),
            )
            .values(status="revoked", revoked_at=now)
        )
        if int(getattr(transitioned, "rowcount", -1)) != 1:
            return False
        db.session.execute(
            sa.update(CompassClientAccessToken)
            .where(
                CompassClientAccessToken.device_id == device_id,
                CompassClientAccessToken.revoked_at.is_(None),
            )
            .values(revoked_at=now)
        )
        db.session.execute(
            sa.update(CompassClientRefreshToken)
            .where(
                CompassClientRefreshToken.device_id == device_id,
                CompassClientRefreshToken.revoked_at.is_(None),
            )
            .values(revoked_at=now)
        )
        if device is not None:
            db.session.add(
                CompassClientAuthEvent(
                    owner_user_id=device.owner_user_id,
                    device_id=device.device_id,
                    event=event,
                    occurred_at=now,
                )
            )
        return True

    @staticmethod
    def _record_event(
        owner_user_id: int, device_id: str, event: str, occurred_at: datetime
    ) -> None:
        from iarf_assistant.extensions import db

        db.session.add(
            CompassClientAuthEvent(
                owner_user_id=owner_user_id,
                device_id=device_id,
                event=event,
                occurred_at=occurred_at,
            )
        )


def configure_compass_client_auth(app: Flask) -> CompassClientAuthService:
    """Install the production resolver and persisted exchange limiter once."""

    existing = app.extensions.get(COMPASS_CLIENT_AUTH_SERVICE_EXTENSION)
    if isinstance(existing, CompassClientAuthService):
        return existing
    secret = app.secret_key
    if not isinstance(secret, (str, bytes)):
        raise TypeError("Compass client authorization requires a Flask secret key.")
    service = CompassClientAuthService(secret)
    app.extensions[COMPASS_CLIENT_AUTH_SERVICE_EXTENSION] = service
    app.extensions.setdefault(
        COMPASS_CLIENT_PRINCIPAL_RESOLVER_EXTENSION, service.resolve_principal
    )
    paths = app.extensions.get("carlina_paths")
    state_dir = getattr(paths, "state_dir", Path(app.instance_path) / "state")
    app.extensions.setdefault(
        COMPASS_CLIENT_TOKEN_LIMITER_EXTENSION,
        CompassTokenExchangeLimiter(
            Path(state_dir) / "compass-client-token-rate-limit.sqlite3",
            secret.encode() if isinstance(secret, str) else secret,
        ),
    )
    return service


def current_compass_client_auth_service() -> CompassClientAuthService:
    service = current_app.extensions.get(COMPASS_CLIENT_AUTH_SERVICE_EXTENSION)
    if not isinstance(service, CompassClientAuthService):
        service = configure_compass_client_auth(current_app)
    return service


def consume_token_exchange_rate_limit(source: str | None) -> int | None:
    limiter = current_app.extensions.get(COMPASS_CLIENT_TOKEN_LIMITER_EXTENSION)
    if not isinstance(limiter, CompassTokenExchangeLimiter):
        current_compass_client_auth_service()
        limiter = current_app.extensions.get(COMPASS_CLIENT_TOKEN_LIMITER_EXTENSION)
    if not isinstance(limiter, CompassTokenExchangeLimiter):
        raise _auth_unavailable()
    try:
        return limiter.consume(source)
    except RuntimeError as error:
        raise _auth_unavailable() from error


def authorization_redirect(redirect_uri: str, parameters: Mapping[str, str]) -> str:
    """Append the bounded authorization result to a validated bare loopback URI."""

    canonical = _validate_loopback_redirect(redirect_uri)
    parsed = urlsplit(canonical)
    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urlencode(parameters), "")
    )


def _validate_loopback_redirect(value: object) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= MAX_REDIRECT_URI_LENGTH:
        raise _invalid_authorization_request()
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise _invalid_authorization_request()
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise _invalid_authorization_request() from error
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "::1"}
        or port is None
        or not 1 <= port <= 65535
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith("/")
        or len(parsed.path) > 256
        or parsed.path.startswith("//")
    ):
        raise _invalid_authorization_request()
    host = "127.0.0.1" if parsed.hostname == "127.0.0.1" else "[::1]"
    return urlunsplit(("http", f"{host}:{port}", parsed.path, "", ""))


def _pkce_s256(verifier: str) -> str:
    return (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )


def _positive_owner(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise _auth_unavailable()
    return value


def _device_active(device: CompassClientDevice, now: datetime) -> bool:
    del now
    return (
        device.status == "active"
        and device.revoked_at is None
        and device.scope == COMPASS_CAPTURE_SCOPE
    )


def _before_or_equal(value: datetime, now: datetime) -> bool:
    candidate = (
        value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    )
    return candidate <= now


def _last_used_is_stale(value: datetime | None, now: datetime) -> bool:
    return value is None or _before_or_equal(value + LAST_USED_WRITE_INTERVAL, now)


def _is_uuid7(value: str) -> bool:
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        return False
    return parsed.version == 7 and str(parsed) == value


def _invalid_authorization_request() -> CompassClientAuthError:
    return CompassClientAuthError(
        400,
        "invalid_authorization_request",
        "The Compass client authorization request is invalid.",
    )


def _invalid_grant() -> CompassClientAuthError:
    return CompassClientAuthError(
        400,
        "invalid_grant",
        "The authorization grant is invalid or expired.",
    )


def _auth_unavailable() -> CompassClientAuthError:
    return CompassClientAuthError(
        503,
        "client_auth_unavailable",
        "Compass client authorization is temporarily unavailable.",
        retryable=True,
    )


__all__ = [
    "ACCESS_TOKEN_TTL",
    "AUTHORIZATION_CODE_TTL",
    "COMPASS_CLIENT_AUTH_SERVICE_EXTENSION",
    "COMPASS_CLIENT_ID",
    "COMPASS_CLIENT_TOKEN_LIMITER_EXTENSION",
    "CompassClientAuthError",
    "CompassClientAuthService",
    "CompassClientAuthorizationRequest",
    "CompassClientTokenPair",
    "CompassTokenExchangeLimiter",
    "authorization_redirect",
    "configure_compass_client_auth",
    "consume_token_exchange_rate_limit",
    "current_compass_client_auth_service",
]
