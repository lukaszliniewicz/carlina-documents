"""Small JSON-only SQLite primitives for cross-process runtime state.

The store deliberately supports only JSON values. It must never grow a pickle
fallback: queue and cache files live in mutable application storage and are
therefore untrusted inputs when opened after a restart.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any

MAX_JSON_BYTES = 64 * 1024 * 1024
MAX_KEY_CHARS = 512
MAX_QUEUE_NAME_CHARS = 128
MAX_ENQUEUE_JOB_ID_CHARS = 128
MAX_LEASE_EXPIRY_SECONDS = 24 * 60 * 60
_SAFE_ENQUEUE_JOB_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")


class SafeSQLiteStoreError(ValueError):
    """Stored data violates the JSON-only persistence contract."""


class QueueCapacityError(RuntimeError):
    """Raised when a bounded queue cannot admit another new item."""


def _reject_json_constant(value: str) -> None:
    raise SafeSQLiteStoreError(f"Non-finite JSON number is not permitted: {value}")


def _encode_json(value: Any) -> str:
    try:
        payload = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise SafeSQLiteStoreError(
            "Queue/cache values must contain only JSON-compatible data."
        ) from exc
    if len(payload.encode("utf-8")) > MAX_JSON_BYTES:
        raise SafeSQLiteStoreError(
            f"Queue/cache value exceeds the {MAX_JSON_BYTES}-byte limit."
        )
    return payload


def _decode_json(payload: str) -> Any:
    if not isinstance(payload, str) or len(payload.encode("utf-8")) > MAX_JSON_BYTES:
        raise SafeSQLiteStoreError("Stored JSON payload is invalid or oversized.")
    try:
        return json.loads(payload, parse_constant=_reject_json_constant)
    except (json.JSONDecodeError, TypeError) as exc:
        raise SafeSQLiteStoreError(
            "Stored queue/cache payload is not valid JSON."
        ) from exc


def _bounded_text(value: str, *, label: str, maximum: int) -> str:
    normalized = str(value)
    if not normalized or len(normalized) > maximum or "\x00" in normalized:
        raise SafeSQLiteStoreError(f"{label} is empty or exceeds its format limit.")
    return normalized


def _expiry_seconds(expire: float | None) -> float | None:
    if expire is None:
        return None
    if isinstance(expire, bool):
        raise SafeSQLiteStoreError("Expiry must be a finite non-negative number.")
    try:
        seconds = float(expire)
    except (TypeError, ValueError) as exc:
        raise SafeSQLiteStoreError(
            "Expiry must be a finite non-negative number."
        ) from exc
    if not math.isfinite(seconds) or seconds < 0:
        raise SafeSQLiteStoreError("Expiry must be a finite non-negative number.")
    return seconds


def _lease_expiry_seconds(expire: float | None) -> float:
    seconds = _expiry_seconds(expire)
    if seconds is None or seconds <= 0 or seconds > MAX_LEASE_EXPIRY_SECONDS:
        raise SafeSQLiteStoreError(
            "Lease expiry must be finite, positive, and bounded."
        )
    return seconds


def _queue_delay_seconds(delay: float) -> float:
    if isinstance(delay, bool):
        raise SafeSQLiteStoreError(
            "Queue defer delay must be a finite, positive, bounded number."
        )
    try:
        seconds = float(delay)
    except (TypeError, ValueError) as exc:
        raise SafeSQLiteStoreError(
            "Queue defer delay must be a finite, positive, bounded number."
        ) from exc
    if not math.isfinite(seconds) or seconds <= 0 or seconds > MAX_LEASE_EXPIRY_SECONDS:
        raise SafeSQLiteStoreError(
            "Queue defer delay must be a finite, positive, bounded number."
        )
    return seconds


def _rate_limit_clock(
    limit: int,
    window_seconds: float,
    now: float | None,
) -> tuple[float, float]:
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise SafeSQLiteStoreError("Rate limit must be a positive integer.")
    if isinstance(window_seconds, bool) or isinstance(now, bool):
        raise SafeSQLiteStoreError(
            "Rate-limit window and timestamp must be finite numbers."
        )
    try:
        window = float(window_seconds)
        timestamp = time.time() if now is None else float(now)
    except (TypeError, ValueError) as exc:
        raise SafeSQLiteStoreError(
            "Rate-limit window and timestamp must be finite numbers."
        ) from exc
    if not math.isfinite(window) or window <= 0:
        raise SafeSQLiteStoreError("Rate-limit window must be a finite positive value.")
    if not math.isfinite(timestamp):
        raise SafeSQLiteStoreError("Rate-limit timestamp must be finite.")
    return window, timestamp


def _active_window_timestamps(
    payload: str | None,
    *,
    cutoff: float,
    timestamp: float,
    limit: int,
) -> list[float]:
    if payload is None:
        return []
    try:
        decoded = _decode_json(payload)
    except SafeSQLiteStoreError:
        return []
    if not isinstance(decoded, list):
        return []

    observed: list[float] = []
    for value in decoded:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        candidate = float(value)
        if math.isfinite(candidate) and cutoff < candidate <= timestamp:
            observed.append(candidate)
    observed.sort()
    return observed[-limit:]


class JsonSQLiteStore:
    """Process-safe JSON key/value storage with optional expiry and queues."""

    def __init__(self, path: str | os.PathLike[str], *, timeout: float = 5.0) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._restrict_permissions(self.path.parent, 0o700)
        self._lock = threading.RLock()
        self._closed = False
        self._connection = sqlite3.connect(
            self.path,
            timeout=timeout,
            isolation_level=None,
            check_same_thread=False,
        )
        try:
            self._configure(timeout)
        except Exception:
            self._connection.close()
            self._closed = True
            raise
        self._restrict_permissions(self.path, 0o600)

    @staticmethod
    def _restrict_permissions(path: Path, mode: int) -> None:
        # Windows ACLs and some network filesystems cannot represent POSIX
        # modes. AppPaths/doctor surface their ownership limitations.
        with suppress(OSError):
            path.chmod(mode)

    def _configure(self, timeout: float) -> None:
        busy_timeout_ms = max(1, int(timeout * 1000))
        self._connection.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
        self._connection.execute("PRAGMA trusted_schema=OFF")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.execute("PRAGMA temp_store=MEMORY")
        connection = self._connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS json_values (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    expires_at REAL,
                    updated_at REAL NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS json_queue_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    queue_name TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    available_at REAL NOT NULL DEFAULT 0
                )
                """
            )
            queue_columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(json_queue_entries)"
                ).fetchall()
            }
            if "available_at" not in queue_columns:
                # queue-v2 files created before durable eligibility existed have
                # no scheduling state. Make every retained entry immediately
                # eligible while leaving entry ids and claims untouched.
                connection.execute(
                    """
                    ALTER TABLE json_queue_entries
                    ADD COLUMN available_at REAL NOT NULL DEFAULT 0
                    """
                )
            else:
                # Repair only an incomplete prior schema change; existing
                # scheduled timestamps remain authoritative.
                connection.execute(
                    """
                    UPDATE json_queue_entries
                    SET available_at = 0
                    WHERE available_at IS NULL
                    """
                )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS json_queue_claims (
                    queue_name TEXT NOT NULL,
                    entry_id INTEGER NOT NULL,
                    claim_token TEXT NOT NULL UNIQUE,
                    claimed_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    PRIMARY KEY(queue_name, entry_id),
                    FOREIGN KEY(entry_id) REFERENCES json_queue_entries(id)
                        ON DELETE CASCADE
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS json_enqueue_receipts (
                    queue_name TEXT NOT NULL,
                    job_id TEXT NOT NULL,
                    task_name TEXT NOT NULL,
                    payload_fingerprint TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY(queue_name, job_id),
                    CHECK(length(job_id) BETWEEN 1 AND 128),
                    CHECK(length(payload_fingerprint) = 64)
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS ix_json_values_expires_at
                    ON json_values(expires_at)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS ix_json_queue_name_id
                    ON json_queue_entries(queue_name, id)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS ix_json_queue_name_available_id
                    ON json_queue_entries(queue_name, available_at, id)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS ix_json_queue_claim_expiry
                    ON json_queue_claims(queue_name, expires_at)
                """
            )
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()

    @staticmethod
    def validate(value: Any) -> None:
        """Raise when ``value`` cannot be persisted by this JSON-only store."""

        _encode_json(value)

    def _require_open(self) -> sqlite3.Connection:
        if self._closed:
            raise RuntimeError("JSON SQLite store is closed.")
        return self._connection

    @contextmanager
    def _write_transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            connection = self._require_open()
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()

    def get(self, key: str, default: Any = None) -> Any:
        normalized = _bounded_text(key, label="Store key", maximum=MAX_KEY_CHARS)
        with self._lock:
            connection = self._require_open()
            row = connection.execute(
                "SELECT value_json, expires_at FROM json_values WHERE key = ?",
                (normalized,),
            ).fetchone()
            if row is None:
                return default
            if row[1] is not None and float(row[1]) <= time.time():
                connection.execute(
                    "DELETE FROM json_values WHERE key = ?",
                    (normalized,),
                )
                return default
            try:
                return _decode_json(row[0])
            except SafeSQLiteStoreError:
                # Do not let one attacker-controlled/corrupt row become a
                # permanent denial of service. It is never deserialized as code.
                connection.execute(
                    "DELETE FROM json_values WHERE key = ?",
                    (normalized,),
                )
                raise

    def set(self, key: str, value: Any, *, expire: float | None = None) -> None:
        normalized = _bounded_text(key, label="Store key", maximum=MAX_KEY_CHARS)
        payload = _encode_json(value)
        now = time.time()
        expiry_seconds = _expiry_seconds(expire)
        expires_at = None if expiry_seconds is None else now + expiry_seconds
        with self._write_transaction() as connection:
            connection.execute(
                """
                INSERT INTO json_values(key, value_json, expires_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value_json = excluded.value_json,
                    expires_at = excluded.expires_at,
                    updated_at = excluded.updated_at
                """,
                (normalized, payload, expires_at, now),
            )

    def add(self, key: str, value: Any, *, expire: float | None = None) -> bool:
        """Atomically add ``value`` only when ``key`` is absent or expired."""

        normalized = _bounded_text(key, label="Store key", maximum=MAX_KEY_CHARS)
        payload = _encode_json(value)
        expiry_seconds = _expiry_seconds(expire)
        now = time.time()
        expires_at = None if expiry_seconds is None else now + expiry_seconds
        with self._write_transaction() as connection:
            row = connection.execute(
                "SELECT expires_at FROM json_values WHERE key = ?",
                (normalized,),
            ).fetchone()
            if row is not None and row[0] is not None:
                try:
                    expired = float(row[0]) <= now
                except (TypeError, ValueError) as exc:
                    raise SafeSQLiteStoreError("Stored expiry is invalid.") from exc
                if not expired:
                    return False
            elif row is not None:
                return False
            connection.execute(
                """
                INSERT INTO json_values(key, value_json, expires_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value_json = excluded.value_json,
                    expires_at = excluded.expires_at,
                    updated_at = excluded.updated_at
                """,
                (normalized, payload, expires_at, now),
            )
        return True

    def delete(self, key: str) -> bool:
        normalized = _bounded_text(key, label="Store key", maximum=MAX_KEY_CHARS)
        with self._write_transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM json_values WHERE key = ?",
                (normalized,),
            )
        return cursor.rowcount > 0

    def delete_if_value(self, key: str, value: Any) -> bool:
        """Atomically delete ``key`` only when its JSON value matches."""

        normalized = _bounded_text(key, label="Store key", maximum=MAX_KEY_CHARS)
        payload = _encode_json(value)
        with self._write_transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM json_values WHERE key = ? AND value_json = ?",
                (normalized, payload),
            )
        return cursor.rowcount > 0

    def acquire_or_signal(
        self,
        key: str,
        token: str,
        *,
        expire: float,
        signal_key: str,
        signal_expire: float | None = None,
    ) -> str:
        """Acquire a lease or coalesce demand behind its current owner.

        Returns ``"acquired"`` for a new owner and ``"signaled"`` when a
        valid owner already exists.  Lease and wake rows are changed in one
        immediate transaction so a contender cannot observe an in-between
        state.
        """

        normalized_key = _bounded_text(key, label="Lease key", maximum=MAX_KEY_CHARS)
        normalized_signal_key = _bounded_text(
            signal_key,
            label="Lease signal key",
            maximum=MAX_KEY_CHARS,
        )
        if normalized_key == normalized_signal_key:
            raise SafeSQLiteStoreError("Lease and signal keys must differ.")
        normalized_token = _bounded_text(
            token, label="Lease token", maximum=MAX_KEY_CHARS
        )
        token_payload = _encode_json(normalized_token)
        lease_seconds = _lease_expiry_seconds(expire)
        signal_seconds = _lease_expiry_seconds(
            lease_seconds if signal_expire is None else signal_expire
        )
        now = time.time()
        lease_expires_at = now + lease_seconds
        signal_expires_at = now + signal_seconds

        with self._write_transaction() as connection:
            lease_row = connection.execute(
                "SELECT value_json, expires_at FROM json_values WHERE key = ?",
                (normalized_key,),
            ).fetchone()
            lease_valid = False
            if lease_row is not None:
                if lease_row[1] is None:
                    lease_valid = True
                else:
                    try:
                        lease_valid = float(lease_row[1]) > now
                    except (TypeError, ValueError) as exc:
                        raise SafeSQLiteStoreError(
                            "Stored lease expiry is invalid."
                        ) from exc

            if not lease_valid:
                connection.execute(
                    """
                    INSERT INTO json_values(key, value_json, expires_at, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        value_json = excluded.value_json,
                        expires_at = excluded.expires_at,
                        updated_at = excluded.updated_at
                    """,
                    (normalized_key, token_payload, lease_expires_at, now),
                )
                # A signal left by an expired/lost owner belongs to no one.
                connection.execute(
                    "DELETE FROM json_values WHERE key = ?",
                    (normalized_signal_key,),
                )
                return "acquired"

            connection.execute(
                """
                INSERT INTO json_values(key, value_json, expires_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    expires_at = excluded.expires_at,
                    updated_at = excluded.updated_at
                """,
                (normalized_signal_key, _encode_json(True), signal_expires_at, now),
            )
            return "signaled"

    def renew_or_release(
        self,
        key: str,
        token: str,
        *,
        expire: float,
        signal_key: str,
        signal_expire: float | None = None,
    ) -> str:
        """Drain a valid wake signal or release a scheduler lease.

        Returns ``"signaled"`` when the caller retains the lease for another
        scheduling pass, ``"released"`` when it safely gives up ownership,
        and ``"lost"`` when the token is no longer the valid owner.
        """

        normalized_key = _bounded_text(key, label="Lease key", maximum=MAX_KEY_CHARS)
        normalized_signal_key = _bounded_text(
            signal_key,
            label="Lease signal key",
            maximum=MAX_KEY_CHARS,
        )
        if normalized_key == normalized_signal_key:
            raise SafeSQLiteStoreError("Lease and signal keys must differ.")
        normalized_token = _bounded_text(
            token, label="Lease token", maximum=MAX_KEY_CHARS
        )
        token_payload = _encode_json(normalized_token)
        lease_seconds = _lease_expiry_seconds(expire)
        # Validate the signal's bounded lifetime even though a release path
        # only needs to inspect the already-persisted timestamp.
        _signal_seconds = _lease_expiry_seconds(
            lease_seconds if signal_expire is None else signal_expire
        )
        now = time.time()
        with self._write_transaction() as connection:
            lease_row = connection.execute(
                "SELECT value_json, expires_at FROM json_values WHERE key = ?",
                (normalized_key,),
            ).fetchone()
            if lease_row is None or lease_row[0] != token_payload:
                return "lost"
            if lease_row[1] is not None:
                try:
                    if float(lease_row[1]) <= now:
                        return "lost"
                except (TypeError, ValueError) as exc:
                    raise SafeSQLiteStoreError(
                        "Stored lease expiry is invalid."
                    ) from exc

            signal_row = connection.execute(
                "SELECT expires_at FROM json_values WHERE key = ?",
                (normalized_signal_key,),
            ).fetchone()
            signal_valid = False
            if signal_row is not None:
                if signal_row[0] is None:
                    signal_valid = True
                else:
                    try:
                        signal_valid = float(signal_row[0]) > now
                    except TypeError, ValueError:
                        signal_valid = False

            if signal_valid:
                connection.execute(
                    "DELETE FROM json_values WHERE key = ?",
                    (normalized_signal_key,),
                )
                connection.execute(
                    "UPDATE json_values SET expires_at = ?, updated_at = ? WHERE key = ? AND value_json = ?",
                    (now + lease_seconds, now, normalized_key, token_payload),
                )
                return "signaled"

            if signal_row is not None:
                # Do not leave malformed/expired demand behind for a later
                # owner to misinterpret as a live wake request.
                connection.execute(
                    "DELETE FROM json_values WHERE key = ?",
                    (normalized_signal_key,),
                )
            connection.execute(
                "DELETE FROM json_values WHERE key = ? AND value_json = ?",
                (normalized_key, token_payload),
            )
            return "released"

    def heartbeat(
        self,
        key: str,
        token: str,
        *,
        expire: float,
        signal_key: str,
        signal_expire: float | None = None,
    ) -> tuple[str, bool]:
        """Renew a live lease and atomically consume one wake signal.

        The result is ``("owned", demand_consumed)`` while ``token`` still
        owns an unexpired lease, or ``("lost", False)`` when ownership has
        expired or been replaced.  The check, renewal, and signal consume are
        one immediate transaction so a scheduler never renews a lease it no
        longer owns or consumes demand for another owner.
        """

        normalized_key = _bounded_text(key, label="Lease key", maximum=MAX_KEY_CHARS)
        normalized_signal_key = _bounded_text(
            signal_key,
            label="Lease signal key",
            maximum=MAX_KEY_CHARS,
        )
        if normalized_key == normalized_signal_key:
            raise SafeSQLiteStoreError("Lease and signal keys must differ.")
        normalized_token = _bounded_text(
            token, label="Lease token", maximum=MAX_KEY_CHARS
        )
        token_payload = _encode_json(normalized_token)
        lease_seconds = _lease_expiry_seconds(expire)
        _signal_seconds = _lease_expiry_seconds(
            lease_seconds if signal_expire is None else signal_expire
        )
        now = time.time()

        with self._write_transaction() as connection:
            lease_row = connection.execute(
                "SELECT value_json, expires_at FROM json_values WHERE key = ?",
                (normalized_key,),
            ).fetchone()
            if lease_row is None or lease_row[0] != token_payload:
                return "lost", False
            if lease_row[1] is not None:
                try:
                    if float(lease_row[1]) <= now:
                        return "lost", False
                except (TypeError, ValueError) as exc:
                    raise SafeSQLiteStoreError(
                        "Stored lease expiry is invalid."
                    ) from exc

            signal_row = connection.execute(
                "SELECT expires_at FROM json_values WHERE key = ?",
                (normalized_signal_key,),
            ).fetchone()
            signal_valid = False
            if signal_row is not None:
                if signal_row[0] is None:
                    signal_valid = True
                else:
                    try:
                        signal_valid = float(signal_row[0]) > now
                    except TypeError, ValueError:
                        signal_valid = False

            renewed = connection.execute(
                """
                UPDATE json_values
                SET expires_at = ?, updated_at = ?
                WHERE key = ? AND value_json = ?
                """,
                (now + lease_seconds, now, normalized_key, token_payload),
            )
            if renewed.rowcount != 1:
                return "lost", False
            if signal_row is not None:
                connection.execute(
                    "DELETE FROM json_values WHERE key = ?",
                    (normalized_signal_key,),
                )
            return "owned", signal_valid

    def increment(
        self,
        key: str,
        amount: int = 1,
        *,
        default: int = 0,
        expire: float | None = None,
    ) -> int:
        normalized = _bounded_text(key, label="Store key", maximum=MAX_KEY_CHARS)
        if isinstance(amount, bool) or not isinstance(amount, int):
            raise SafeSQLiteStoreError("Counter increments must be integers.")
        if isinstance(default, bool) or not isinstance(default, int):
            raise SafeSQLiteStoreError("Counter defaults must be integers.")
        now = time.time()
        with self._write_transaction() as connection:
            row = connection.execute(
                "SELECT value_json, expires_at FROM json_values WHERE key = ?",
                (normalized,),
            ).fetchone()
            expired = row is not None and row[1] is not None and float(row[1]) <= now
            if row is None or expired:
                current = default
            else:
                try:
                    current = _decode_json(row[0])
                except SafeSQLiteStoreError:
                    # A malformed row is data loss, but it must not become a
                    # permanent denial of service for every later increment.
                    current = default
            if isinstance(current, bool) or not isinstance(current, int):
                current = default
            result = current + amount
            expires_at = (
                now + max(0.0, float(expire))
                if expire is not None
                else (None if row is None or expired else row[1])
            )
            connection.execute(
                """
                INSERT INTO json_values(key, value_json, expires_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value_json = excluded.value_json,
                    expires_at = excluded.expires_at,
                    updated_at = excluded.updated_at
                """,
                (normalized, _encode_json(result), expires_at, now),
            )
        return result

    def consume_sliding_window(
        self,
        key: str,
        *,
        limit: int,
        window_seconds: float,
        now: float | None = None,
    ) -> tuple[bool, int]:
        """Atomically consume one allowance from a cross-process sliding window."""

        normalized = _bounded_text(key, label="Store key", maximum=MAX_KEY_CHARS)
        window, timestamp = _rate_limit_clock(limit, window_seconds, now)
        cutoff = timestamp - window
        with self._write_transaction() as connection:
            connection.execute(
                "DELETE FROM json_values "
                "WHERE expires_at IS NOT NULL AND expires_at <= ?",
                (timestamp,),
            )
            row = connection.execute(
                "SELECT value_json FROM json_values WHERE key = ?",
                (normalized,),
            ).fetchone()
            observed = _active_window_timestamps(
                row[0] if row is not None else None,
                cutoff=cutoff,
                timestamp=timestamp,
                limit=limit,
            )

            allowed = len(observed) < limit
            if allowed:
                observed.append(timestamp)
            expires_at = observed[-1] + window
            connection.execute(
                """
                INSERT INTO json_values(key, value_json, expires_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value_json = excluded.value_json,
                    expires_at = excluded.expires_at,
                    updated_at = excluded.updated_at
                """,
                (normalized, _encode_json(observed), expires_at, timestamp),
            )

        if allowed:
            return True, 0
        retry_after = math.ceil(observed[0] + window - timestamp)
        return False, max(1, retry_after)

    def merge_dict(
        self,
        key: str,
        updates: dict[str, Any],
        *,
        expire: float | None = None,
    ) -> tuple[dict, dict]:
        """Atomically merge a JSON object and return ``(previous, merged)``."""

        normalized = _bounded_text(key, label="Store key", maximum=MAX_KEY_CHARS)
        if not isinstance(updates, dict):
            raise SafeSQLiteStoreError("Mapping updates must be a dictionary.")
        # Validate before acquiring the SQLite write lock. The merged payload
        # is validated again because its combined size may exceed the limit.
        _encode_json(updates)
        expiry_seconds = _expiry_seconds(expire)
        now = time.time()
        expires_at = None if expiry_seconds is None else now + expiry_seconds
        with self._write_transaction() as connection:
            row = connection.execute(
                "SELECT value_json, expires_at FROM json_values WHERE key = ?",
                (normalized,),
            ).fetchone()
            expired = row is not None and row[1] is not None and float(row[1]) <= now
            previous: dict[str, Any] = {}
            if row is not None and not expired:
                try:
                    decoded = _decode_json(row[0])
                except SafeSQLiteStoreError:
                    decoded = {}
                if isinstance(decoded, dict):
                    previous = decoded
            merged = {**previous, **updates}
            payload = _encode_json(merged)
            connection.execute(
                """
                INSERT INTO json_values(key, value_json, expires_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value_json = excluded.value_json,
                    expires_at = excluded.expires_at,
                    updated_at = excluded.updated_at
                """,
                (normalized, payload, expires_at, now),
            )
        return previous, merged

    def expire(self) -> int:
        """Delete expired JSON values and return the number of removed rows."""

        now = time.time()
        with self._write_transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM json_values WHERE expires_at IS NOT NULL AND expires_at <= ?",
                (now,),
            )
        return cursor.rowcount

    def queue(self, name: str) -> JsonSQLiteQueue:
        return JsonSQLiteQueue(self, name)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._connection.close()
            self._closed = True


class JsonSQLiteQueue:
    """FIFO queue with leased claim/ack delivery semantics.

    ``popleft`` remains as a destructive compatibility primitive for direct
    administrative/test consumers. Workers should use ``claim`` and ``ack`` so
    a process death between dequeue and handler entry cannot lose the job.
    """

    def __init__(self, cache: JsonSQLiteStore, name: str) -> None:
        self.cache = cache
        self.name = _bounded_text(
            name,
            label="Queue name",
            maximum=MAX_QUEUE_NAME_CHARS,
        )

    def append(self, value: Any) -> None:
        payload = _encode_json(value)
        now = time.time()
        with self.cache._write_transaction() as connection:
            connection.execute(
                """
                INSERT INTO json_queue_entries(
                    queue_name, payload_json, created_at, available_at
                ) VALUES (?, ?, ?, ?)
                """,
                (self.name, payload, now, now),
            )

    def enqueue_once(
        self,
        *,
        job_id: str,
        task_name: str,
        payload: dict[str, Any],
        status_data: dict[str, Any],
        max_queue_items: int | None = None,
        status_expire_seconds: float | None = None,
    ) -> bool:
        """Atomically store a keyed enqueue receipt, initial status and job.

        The receipt retains only a fingerprint of the canonical JSON payload.
        An identical replay is a no-op even when its original queue entry has
        already been acknowledged; reusing the id for different work fails.
        When set, ``max_queue_items`` counts every retained queue entry, and
        receipt replay/conflict checks happen before capacity admission.
        """

        if max_queue_items is not None and (
            type(max_queue_items) is not int or max_queue_items <= 0
        ):
            raise SafeSQLiteStoreError(
                "Queue capacity must be a positive integer or None."
            )
        if (
            not isinstance(job_id, str)
            or len(job_id) > MAX_ENQUEUE_JOB_ID_CHARS
            or not _SAFE_ENQUEUE_JOB_ID.fullmatch(job_id)
        ):
            raise SafeSQLiteStoreError(
                "Queue job id must be a bounded safe identifier."
            )
        if (
            not isinstance(task_name, str)
            or not task_name.strip()
            or len(task_name) > MAX_QUEUE_NAME_CHARS
            or any(ord(character) < 32 or ord(character) == 127 for character in task_name)
        ):
            raise SafeSQLiteStoreError(
                "Queue task name must be a bounded non-empty string."
            )
        if not isinstance(payload, dict) or not isinstance(status_data, dict):
            raise SafeSQLiteStoreError(
                "Queue jobs and their initial status must be JSON objects."
            )

        canonical_payload = _encode_json(payload)
        payload_fingerprint = hashlib.sha256(
            canonical_payload.encode("utf-8")
        ).hexdigest()
        job_data = _encode_json(
            {"job_id": job_id, "task_name": task_name, "payload": payload}
        )
        status_key = _bounded_text(
            f"job_status:{job_id}",
            label="Store key",
            maximum=MAX_KEY_CHARS,
        )
        status_json = _encode_json(status_data)
        expiry_seconds = _expiry_seconds(status_expire_seconds)
        now = time.time()
        status_expires_at = (
            None if expiry_seconds is None else now + expiry_seconds
        )

        with self.cache._write_transaction() as connection:
            receipt = connection.execute(
                """
                SELECT task_name, payload_fingerprint
                FROM json_enqueue_receipts
                WHERE queue_name = ? AND job_id = ?
                """,
                (self.name, job_id),
            ).fetchone()
            if receipt is not None:
                if receipt[0] != task_name or receipt[1] != payload_fingerprint:
                    raise SafeSQLiteStoreError(
                        "Queue job id is already bound to different work."
                    )
                return False

            if max_queue_items is not None:
                queue_item_count = connection.execute(
                    "SELECT COUNT(*) FROM json_queue_entries WHERE queue_name = ?",
                    (self.name,),
                ).fetchone()[0]
                if queue_item_count >= max_queue_items:
                    raise QueueCapacityError("Queue capacity has been reached.")

            connection.execute(
                """
                INSERT INTO json_enqueue_receipts(
                    queue_name, job_id, task_name, payload_fingerprint, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (self.name, job_id, task_name, payload_fingerprint, now),
            )
            connection.execute(
                """
                INSERT INTO json_values(key, value_json, expires_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value_json = excluded.value_json,
                    expires_at = excluded.expires_at,
                    updated_at = excluded.updated_at
                """,
                (status_key, status_json, status_expires_at, now),
            )
            connection.execute(
                """
                INSERT INTO json_queue_entries(
                    queue_name, payload_json, created_at, available_at
                ) VALUES (?, ?, ?, ?)
                """,
                (self.name, job_data, now, now),
            )
        return True

    def active_claims(self) -> tuple[tuple[int, str, float, float], ...]:
        """Return identity and timing for current live claims.

        This is an advisory, read-only observation for replacement workers. It
        does not decode payloads or mutate lease state.
        """

        now = time.time()
        with self.cache._lock:
            connection = self.cache._require_open()
            rows = connection.execute(
                """
                SELECT claim.entry_id, claim.claim_token,
                       claim.claimed_at, claim.expires_at
                FROM json_queue_claims AS claim
                JOIN json_queue_entries AS entry ON entry.id = claim.entry_id
                WHERE claim.queue_name = ? AND claim.expires_at > ?
                ORDER BY claim.entry_id ASC
                """,
                (self.name, now),
            ).fetchall()
        return tuple(
            (int(row[0]), str(row[1]), float(row[2]), float(row[3]))
            for row in rows
        )

    def next_available_delay(self) -> float | None:
        """Return the bounded delay until the next unclaimed entry is eligible.

        ``None`` means there are no unclaimed entries. A zero result means an
        entry is ready now; a positive result means only delayed work is
        available. This observation never cleans up claims or changes entries.
        """

        now = time.time()
        with self.cache._lock:
            connection = self.cache._require_open()
            row = connection.execute(
                """
                SELECT MIN(entry.available_at)
                FROM json_queue_entries AS entry
                LEFT JOIN json_queue_claims AS claim
                    ON claim.queue_name = entry.queue_name
                    AND claim.entry_id = entry.id
                WHERE entry.queue_name = ? AND claim.entry_id IS NULL
                """,
                (self.name,),
            ).fetchone()
        if row is None or row[0] is None:
            return None
        try:
            delay = float(row[0]) - now
        except (TypeError, ValueError):
            return MAX_LEASE_EXPIRY_SECONDS
        if not math.isfinite(delay):
            return MAX_LEASE_EXPIRY_SECONDS
        return min(max(0.0, delay), MAX_LEASE_EXPIRY_SECONDS)

    def popleft(self) -> Any:
        with self.cache._write_transaction() as connection:
            now = time.time()
            connection.execute(
                "DELETE FROM json_queue_claims WHERE expires_at <= ?",
                (now,),
            )
            row = connection.execute(
                """
                SELECT entry.id, entry.payload_json
                FROM json_queue_entries AS entry
                LEFT JOIN json_queue_claims AS claim
                    ON claim.queue_name = entry.queue_name
                    AND claim.entry_id = entry.id
                WHERE entry.queue_name = ?
                    AND claim.entry_id IS NULL
                    AND entry.available_at <= ?
                ORDER BY entry.available_at ASC, entry.id ASC
                LIMIT 1
                """,
                (self.name, now),
            ).fetchone()
            if row is None:
                raise IndexError("pop from an empty queue")
            connection.execute(
                "DELETE FROM json_queue_entries WHERE id = ?",
                (row[0],),
            )
        # Decode only after deleting the row. Corrupt JSON can cause a bounded
        # job failure, but can never execute code or wedge every later job.
        return _decode_json(row[1])

    def claim(self, *, lease_seconds: float) -> tuple[str, Any]:
        """Lease and return the oldest available entry without deleting it."""

        lease = _lease_expiry_seconds(lease_seconds)
        now = time.time()
        token = str(uuid.uuid4())
        with self.cache._write_transaction() as connection:
            connection.execute(
                "DELETE FROM json_queue_claims WHERE expires_at <= ?",
                (now,),
            )
            while True:
                row = connection.execute(
                    """
                    SELECT entry.id, entry.payload_json
                    FROM json_queue_entries AS entry
                    LEFT JOIN json_queue_claims AS claim
                        ON claim.queue_name = entry.queue_name
                        AND claim.entry_id = entry.id
                    WHERE entry.queue_name = ?
                        AND claim.entry_id IS NULL
                        AND entry.available_at <= ?
                    ORDER BY entry.available_at ASC, entry.id ASC
                    LIMIT 1
                    """,
                    (self.name, now),
                ).fetchone()
                if row is None:
                    raise IndexError("claim from an empty queue")
                try:
                    value = _decode_json(row[1])
                except SafeSQLiteStoreError:
                    # Corrupt JSON is data, never executable code. Remove only
                    # that row so later legitimate jobs remain available.
                    connection.execute(
                        "DELETE FROM json_queue_entries WHERE id = ?",
                        (row[0],),
                    )
                    continue
                connection.execute(
                    """
                    INSERT INTO json_queue_claims(
                        queue_name, entry_id, claim_token, claimed_at, expires_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (self.name, row[0], token, now, now + lease),
                )
                return token, value

    def defer(self, claim_token: str, delay_seconds: float) -> bool:
        """Atomically schedule an owned delivery for a later retry."""

        token = _bounded_text(
            claim_token,
            label="Queue claim token",
            maximum=MAX_KEY_CHARS,
        )
        delay = _queue_delay_seconds(delay_seconds)
        now = time.time()
        with self.cache._write_transaction() as connection:
            row = connection.execute(
                """
                SELECT entry_id
                FROM json_queue_claims
                WHERE queue_name = ? AND claim_token = ? AND expires_at > ?
                """,
                (self.name, token, now),
            ).fetchone()
            if row is None:
                return False
            cursor = connection.execute(
                """
                UPDATE json_queue_entries
                SET available_at = ?
                WHERE id = ? AND queue_name = ?
                """,
                (now + delay, row[0], self.name),
            )
            if cursor.rowcount != 1:
                return False
            connection.execute(
                """
                DELETE FROM json_queue_claims
                WHERE queue_name = ? AND claim_token = ?
                """,
                (self.name, token),
            )
        return True

    def renew_claim(self, claim_token: str, *, lease_seconds: float) -> bool:
        """Extend a still-owned, unexpired claim."""

        token = _bounded_text(
            claim_token,
            label="Queue claim token",
            maximum=MAX_KEY_CHARS,
        )
        lease = _lease_expiry_seconds(lease_seconds)
        now = time.time()
        with self.cache._write_transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE json_queue_claims
                SET expires_at = ?
                WHERE queue_name = ? AND claim_token = ? AND expires_at > ?
                """,
                (now + lease, self.name, token, now),
            )
        return cursor.rowcount == 1

    def ack(self, claim_token: str) -> bool:
        """Delete exactly the entry still owned by ``claim_token``."""

        token = _bounded_text(
            claim_token,
            label="Queue claim token",
            maximum=MAX_KEY_CHARS,
        )
        with self.cache._write_transaction() as connection:
            now = time.time()
            row = connection.execute(
                """
                SELECT entry_id
                FROM json_queue_claims
                WHERE queue_name = ? AND claim_token = ? AND expires_at > ?
                """,
                (self.name, token, now),
            ).fetchone()
            if row is None:
                return False
            cursor = connection.execute(
                """
                DELETE FROM json_queue_entries
                WHERE id = ? AND queue_name = ?
                """,
                (row[0], self.name),
            )
            # Retire the receipt in this transaction instead of depending only
            # on connection-local foreign-key cascade configuration.
            connection.execute(
                """
                DELETE FROM json_queue_claims
                WHERE queue_name = ? AND claim_token = ?
                """,
                (self.name, token),
            )
        return cursor.rowcount == 1

    def release(self, claim_token: str) -> bool:
        """Relinquish a claim immediately while retaining its queue entry."""

        token = _bounded_text(
            claim_token,
            label="Queue claim token",
            maximum=MAX_KEY_CHARS,
        )
        with self.cache._write_transaction() as connection:
            cursor = connection.execute(
                """
                DELETE FROM json_queue_claims
                WHERE queue_name = ? AND claim_token = ?
                """,
                (self.name, token),
            )
        return cursor.rowcount == 1


__all__ = [
    "MAX_JSON_BYTES",
    "JsonSQLiteQueue",
    "JsonSQLiteStore",
    "QueueCapacityError",
    "SafeSQLiteStoreError",
]
