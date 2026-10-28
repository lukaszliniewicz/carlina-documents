"""Single SQLite connection policy for Carlina's SQLAlchemy engines."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import quote

from sqlalchemy import event
from sqlalchemy.engine import Connection, Engine

from carlina.product_identity import product_environment_value


def _sqlite_file_uri_from_text(path: str) -> str:
    """Encode an absolute native path without turning UNC hosts into authorities."""

    normalized = str(path).replace("\\", "/")
    folded = normalized.casefold()
    if folded.startswith("//?/unc/"):
        normalized = "//" + normalized[8:]
    elif folded.startswith("//?/"):
        normalized = normalized[4:]
    encoded = quote(normalized, safe="/:")
    if normalized.startswith("//"):
        # SQLite rejects non-empty URI authorities by default. Four slashes
        # preserve the leading UNC // as part of the path instead.
        return "file://" + encoded
    if len(normalized) >= 3 and normalized[1:3] == ":/":
        return "file:///" + encoded
    if normalized.startswith("/"):
        return "file://" + encoded
    raise ValueError(f"SQLite URI paths must be absolute: {path!r}")


def sqlite_read_only_uri(path: str | Path) -> str:
    """Return a read-only SQLite URI for POSIX, drive-letter, and UNC paths."""

    resolved = Path(path).expanduser().resolve()
    return f"{_sqlite_file_uri_from_text(str(resolved))}?mode=ro"


def sqlite_busy_timeout_ms() -> int:
    raw = product_environment_value("SQLITE_BUSY_TIMEOUT_MS", default="5000")
    try:
        return max(250, min(30_000, int(raw)))
    except (TypeError, ValueError):
        return 5_000


def _load_sqlite_vec(connection: sqlite3.Connection) -> None:
    try:
        import sqlite_vec
    except ImportError:
        return
    try:
        connection.enable_load_extension(True)
        sqlite_vec.load(connection)
    except (AttributeError, sqlite3.Error, RuntimeError):
        logging.getLogger(__name__).warning(
            "Could not load sqlite-vec for a SQLite connection.",
            exc_info=True,
        )
    finally:
        try:
            connection.enable_load_extension(False)
        except (AttributeError, sqlite3.Error):
            pass


def configure_sqlite_connection(
    connection: sqlite3.Connection,
    *,
    load_vector_extension: bool = False,
) -> sqlite3.Connection:
    """Apply Carlina's policy to a directly-owned sqlite3 connection."""

    original_autocommit = getattr(connection, "autocommit", None)
    restore_autocommit = original_autocommit is False
    if restore_autocommit:
        # Python's PEP 249 mode starts a transaction immediately. SQLite does
        # not allow journal_mode/synchronous to change inside one, so follow
        # SQLAlchemy's documented PRAGMA recipe and restore the driver mode.
        connection.autocommit = True
    cursor = connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute(f"PRAGMA busy_timeout={sqlite_busy_timeout_ms()}")
    finally:
        cursor.close()
        if restore_autocommit:
            connection.autocommit = original_autocommit
    if load_vector_extension:
        _load_sqlite_vec(connection)
    return connection


def _configure_connection(
    dbapi_connection: Any,
    _connection_record: Any,
) -> None:
    if not isinstance(dbapi_connection, sqlite3.Connection):
        return
    configure_sqlite_connection(
        dbapi_connection,
        load_vector_extension=True,
    )


def configure_sqlite_engine(engine: Engine) -> None:
    """Apply Carlina's policy only to the supplied application engine."""

    if engine.dialect.name != "sqlite":
        return
    if not event.contains(engine, "connect", _configure_connection):
        event.listen(engine, "connect", _configure_connection)


def ensure_sqlite_ddl_transaction(connection: Connection) -> None:
    """Start transactional SQLite DDL only when the driver has not done so."""

    if connection.dialect.name != "sqlite":
        return
    proxied = connection.connection
    driver_connection = getattr(proxied, "driver_connection", proxied)
    if not bool(getattr(driver_connection, "in_transaction", False)):
        connection.exec_driver_sql("BEGIN IMMEDIATE")


__all__ = [
    "configure_sqlite_connection",
    "configure_sqlite_engine",
    "ensure_sqlite_ddl_transaction",
    "sqlite_busy_timeout_ms",
    "sqlite_read_only_uri",
]
