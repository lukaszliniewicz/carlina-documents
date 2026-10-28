"""Persistent, privacy-preserving login throttling for the local runtime."""

from __future__ import annotations

import hashlib
import hmac
import logging
import math
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from iarf_assistant.core.persistence.sqlite import (
    configure_sqlite_connection,
    sqlite_busy_timeout_ms,
)


@dataclass(frozen=True, slots=True)
class LoginDecision:
    allowed: bool
    retry_after: int = 0


class LoginAttemptLimiter:
    """Rate-limit account/source pairs without persisting raw identifiers."""

    def __init__(
        self,
        database_path: str | Path,
        secret: str | bytes,
        *,
        window_seconds: int = 15 * 60,
        free_failures: int = 4,
        maximum_delay: int = 15 * 60,
    ):
        self.database_path = Path(database_path).expanduser().resolve()
        self.secret = secret.encode("utf-8") if isinstance(secret, str) else secret
        if len(self.secret) < 32:
            raise ValueError("Login limiter secret must contain at least 32 bytes.")
        self.window_seconds = int(window_seconds)
        self.free_failures = int(free_failures)
        self.maximum_delay = int(maximum_delay)
        self._schema_lock = threading.Lock()
        self._initialized = False

    def _digest(self, namespace: str, value: str | None) -> str:
        normalized = str(value or "unknown").strip().casefold()
        return hmac.new(
            self.secret,
            f"{namespace}\0{normalized}".encode("utf-8"),
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

    def _ensure_schema(self) -> None:
        if self._initialized:
            return
        with self._schema_lock:
            if self._initialized:
                return
            with self._connect() as connection:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS login_attempt (
                        subject_hash TEXT NOT NULL,
                        source_hash TEXT NOT NULL,
                        failure_count INTEGER NOT NULL,
                        window_started REAL NOT NULL,
                        last_failure REAL NOT NULL,
                        locked_until REAL NOT NULL,
                        PRIMARY KEY (subject_hash, source_hash)
                    );
                    CREATE TABLE IF NOT EXISTS login_audit (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        occurred_at REAL NOT NULL,
                        event TEXT NOT NULL,
                        subject_hash TEXT NOT NULL,
                        source_hash TEXT NOT NULL,
                        failure_count INTEGER NOT NULL,
                        delay_seconds INTEGER NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS ix_login_audit_occurred_at
                    ON login_audit (occurred_at);
                    """
                )
            self._initialized = True

    def _identifiers(self, subject: str | None, source: str | None) -> tuple[str, str]:
        return self._digest("account", subject), self._digest("source", source)

    def check(
        self,
        subject: str | None,
        source: str | None,
        *,
        now: float | None = None,
    ) -> LoginDecision:
        self._ensure_schema()
        timestamp = time.time() if now is None else float(now)
        subject_hash, source_hash = self._identifiers(subject, source)
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT locked_until, last_failure
                FROM login_attempt
                WHERE subject_hash = ? AND source_hash = ?
                """,
                (subject_hash, source_hash),
            ).fetchone()
            if row is None:
                return LoginDecision(True)
            if timestamp - float(row["last_failure"]) >= self.window_seconds:
                connection.execute(
                    "DELETE FROM login_attempt WHERE subject_hash = ? AND source_hash = ?",
                    (subject_hash, source_hash),
                )
                return LoginDecision(True)
            retry_after = max(0, math.ceil(float(row["locked_until"]) - timestamp))
            return LoginDecision(retry_after == 0, retry_after)

    def record_failure(
        self,
        subject: str | None,
        source: str | None,
        *,
        now: float | None = None,
    ) -> LoginDecision:
        self._ensure_schema()
        timestamp = time.time() if now is None else float(now)
        subject_hash, source_hash = self._identifiers(subject, source)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT failure_count, window_started, last_failure
                FROM login_attempt
                WHERE subject_hash = ? AND source_hash = ?
                """,
                (subject_hash, source_hash),
            ).fetchone()
            if row is None or timestamp - float(row["last_failure"]) >= self.window_seconds:
                failure_count = 1
                window_started = timestamp
            else:
                failure_count = int(row["failure_count"]) + 1
                window_started = float(row["window_started"])
            exponent = max(0, failure_count - self.free_failures - 1)
            delay = 0 if failure_count <= self.free_failures else min(
                self.maximum_delay,
                2**exponent,
            )
            locked_until = timestamp + delay
            connection.execute(
                """
                INSERT INTO login_attempt (
                    subject_hash, source_hash, failure_count, window_started,
                    last_failure, locked_until
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(subject_hash, source_hash) DO UPDATE SET
                    failure_count = excluded.failure_count,
                    window_started = excluded.window_started,
                    last_failure = excluded.last_failure,
                    locked_until = excluded.locked_until
                """,
                (
                    subject_hash,
                    source_hash,
                    failure_count,
                    window_started,
                    timestamp,
                    locked_until,
                ),
            )
            self._audit(
                connection,
                timestamp,
                "failure",
                subject_hash,
                source_hash,
                failure_count,
                delay,
            )
            self._prune(connection, timestamp)
            connection.commit()
        logging.warning(
            "Authentication failed account=%s source=%s failures=%d delay=%ds",
            subject_hash[:12],
            source_hash[:12],
            failure_count,
            delay,
        )
        return LoginDecision(delay == 0, delay)

    def record_success(
        self,
        subject: str | None,
        source: str | None,
        *,
        now: float | None = None,
    ) -> None:
        self._ensure_schema()
        timestamp = time.time() if now is None else float(now)
        subject_hash, source_hash = self._identifiers(subject, source)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM login_attempt WHERE subject_hash = ? AND source_hash = ?",
                (subject_hash, source_hash),
            )
            self._audit(
                connection,
                timestamp,
                "success",
                subject_hash,
                source_hash,
                0,
                0,
            )
            self._prune(connection, timestamp)
            connection.commit()

    @staticmethod
    def _audit(
        connection: sqlite3.Connection,
        timestamp: float,
        event: str,
        subject_hash: str,
        source_hash: str,
        failure_count: int,
        delay: int,
    ) -> None:
        connection.execute(
            """
            INSERT INTO login_audit (
                occurred_at, event, subject_hash, source_hash,
                failure_count, delay_seconds
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                timestamp,
                event,
                subject_hash,
                source_hash,
                failure_count,
                delay,
            ),
        )

    def _prune(self, connection: sqlite3.Connection, timestamp: float) -> None:
        connection.execute(
            "DELETE FROM login_audit WHERE occurred_at < ?",
            (timestamp - 30 * 24 * 60 * 60,),
        )


def configure_login_security(app, paths) -> LoginAttemptLimiter:
    limiter = LoginAttemptLimiter(paths.state_dir / "security" / "login-attempts.db", app.secret_key)
    app.extensions["carlina_login_limiter"] = limiter
    return limiter


__all__ = [
    "LoginAttemptLimiter",
    "LoginDecision",
    "configure_login_security",
]
