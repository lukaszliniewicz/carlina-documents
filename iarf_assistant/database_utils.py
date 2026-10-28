from __future__ import annotations

import logging
import os
import random
import sqlite3
import threading
import time
from collections.abc import Callable
from typing import Any, TypeVar

from sqlalchemy.exc import OperationalError

from carlina.product_identity import product_environment_value

T = TypeVar("T")
DEFAULT_WRITE_RETRY_SECONDS = 20.0


class DatabaseBusyError(RuntimeError):
    """A bounded SQLite write retry exhausted its deadline."""

    def __init__(
        self,
        operation_name: str,
        *,
        attempts: int,
        elapsed_seconds: float,
    ):
        self.operation_name = str(operation_name or "database_write")
        self.attempts = max(1, int(attempts))
        self.elapsed_seconds = max(0.0, float(elapsed_seconds))
        super().__init__(
            "The database is temporarily busy. "
            "Please retry the operation in a moment."
        )


class TransactionScopeError(RuntimeError):
    """A retryable operation was asked to adopt unrelated pending writes."""


def is_database_locked(error: BaseException | str) -> bool:
    lock_messages = (
        "database is locked",
        "database table is locked",
        "database schema is locked",
    )
    if isinstance(error, str):
        return any(message in error.lower() for message in lock_messages)
    current: BaseException | None = (
        error if isinstance(error, BaseException) else None
    )
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        if isinstance(current, sqlite3.Error):
            code = getattr(current, "sqlite_errorcode", None)
            if isinstance(code, int):
                return (code & 0xFF) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
        # SQLAlchemy's outer message includes SQL and parameters, which are not
        # evidence of lock contention. Inspect the underlying driver error.
        nested = (
            getattr(current, "orig", None)
            or current.__cause__
            or current.__context__
        )
        if isinstance(nested, BaseException):
            current = nested
            continue
        return any(message in str(current).lower() for message in lock_messages)
    return False


def public_error_message(error: BaseException | str) -> str:
    """Return a user/model-safe message without SQL or bound parameters."""
    if isinstance(error, DatabaseBusyError) or is_database_locked(error):
        return (
            "The database is temporarily busy. "
            "Please retry the operation in a moment."
        )
    if isinstance(error, OperationalError):
        return "A database operation failed. Please retry the operation."
    return str(error)


def _configured_retry_seconds() -> float:
    raw = product_environment_value(
        "DB_WRITE_RETRY_SECONDS",
        default=str(DEFAULT_WRITE_RETRY_SECONDS),
    )
    try:
        return max(0.1, float(raw))
    except (TypeError, ValueError):
        return DEFAULT_WRITE_RETRY_SECONDS


def _unit_of_work_counts(session) -> dict[str, int]:
    return {
        "new": len(session.new),
        "dirty": len(session.dirty),
        "deleted": len(session.deleted),
    }


def _session_in_transaction(session) -> bool:
    in_transaction = getattr(session, "in_transaction", None)
    if callable(in_transaction):
        return bool(in_transaction())
    # Flask-SQLAlchemy exposes a scoped_session proxy. Calling it returns the
    # actual SQLAlchemy Session for the current application/request context.
    if callable(session):
        current = session()
        in_transaction = getattr(current, "in_transaction", None)
        if callable(in_transaction):
            return bool(in_transaction())
    return False


def _rollback_quietly(session) -> None:
    try:
        session.rollback()
    except Exception:
        logging.exception("Database rollback failed")


def run_transaction_with_retry(
    session,
    operation: Callable[[Any], T],
    *,
    operation_name: str,
    timeout_seconds: float | None = None,
    initial_delay_seconds: float = 0.1,
    max_delay_seconds: float = 1.5,
    require_clean_session: bool = True,
) -> T:
    """Run and replay a complete unit of work after transient SQLite locks.

    ``operation`` must construct every write it needs on each invocation. This
    is deliberately callback-based: after SQLAlchemy rolls a transaction back,
    retrying only ``session.commit()`` cannot restore the discarded unit of
    work.
    """
    if not callable(operation):
        raise TypeError("operation must be callable")

    pending = _unit_of_work_counts(session)
    if require_clean_session and any(pending.values()):
        raise TransactionScopeError(
            f"{operation_name} requires a clean session; pending={pending}"
        )

    # End an implicit read transaction before beginning the isolated write.
    if _session_in_transaction(session):
        session.rollback()

    retry_window = (
        _configured_retry_seconds()
        if timeout_seconds is None
        else max(0.1, float(timeout_seconds))
    )
    started_at = time.monotonic()
    deadline = started_at + retry_window
    attempt = 0
    delay = max(0.01, float(initial_delay_seconds))

    while True:
        attempt += 1
        try:
            result = operation(session)
            session.commit()
            elapsed = time.monotonic() - started_at
            if elapsed >= 1.0:
                logging.warning(
                    "Slow database write operation=%s elapsed_ms=%d "
                    "attempts=%d pid=%d thread_id=%d",
                    operation_name,
                    round(elapsed * 1000),
                    attempt,
                    os.getpid(),
                    threading.get_ident(),
                )
            return result
        except OperationalError as error:
            _rollback_quietly(session)
            if not is_database_locked(error):
                raise

            now = time.monotonic()
            elapsed = now - started_at
            remaining = deadline - now
            if remaining <= 0:
                logging.error(
                    "Database write deadline exhausted operation=%s "
                    "elapsed_ms=%d attempts=%d pid=%d thread_id=%d",
                    operation_name,
                    round(elapsed * 1000),
                    attempt,
                    os.getpid(),
                    threading.get_ident(),
                )
                raise DatabaseBusyError(
                    operation_name,
                    attempts=attempt,
                    elapsed_seconds=elapsed,
                ) from error

            sleep_seconds = min(
                remaining,
                delay + random.uniform(0.025, 0.075),
            )
            logging.warning(
                "Database busy; replaying transaction operation=%s "
                "attempt=%d sleep_ms=%d remaining_ms=%d pid=%d thread_id=%d",
                operation_name,
                attempt,
                round(sleep_seconds * 1000),
                round(remaining * 1000),
                os.getpid(),
                threading.get_ident(),
            )
            time.sleep(sleep_seconds)
            delay = min(max_delay_seconds, delay * 2)
        except Exception:
            _rollback_quietly(session)
            raise


def commit_with_retry(
    session,
    max_retries: int = 6,
    *,
    operation_name: str = "session_commit",
) -> bool:
    """Commit once and fail safely if SQLite is locked.

    This compatibility helper intentionally does not retry a rolled-back
    session: SQLAlchemy has already discarded its unit of work. Callers that
    need lock retries must use :func:`run_transaction_with_retry` and provide a
    callback that reconstructs the complete transaction.
    """
    del max_retries  # Retained for source compatibility with older callers.
    started_at = time.monotonic()
    try:
        session.commit()
        elapsed = time.monotonic() - started_at
        if elapsed >= 1.0:
            logging.warning(
                "Slow database commit operation=%s elapsed_ms=%d pid=%d "
                "thread_id=%d",
                operation_name,
                round(elapsed * 1000),
                os.getpid(),
                threading.get_ident(),
            )
        return True
    except OperationalError as error:
        _rollback_quietly(session)
        if is_database_locked(error):
            elapsed = time.monotonic() - started_at
            logging.error(
                "Database commit could not be safely replayed operation=%s "
                "elapsed_ms=%d pid=%d thread_id=%d",
                operation_name,
                round(elapsed * 1000),
                os.getpid(),
                threading.get_ident(),
            )
            raise DatabaseBusyError(
                operation_name,
                attempts=1,
                elapsed_seconds=elapsed,
            ) from error
        raise
    except Exception:
        _rollback_quietly(session)
        raise
