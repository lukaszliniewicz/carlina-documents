"""UTC boundary helpers.

Carlina's existing SQLite schema stores naive ``DATETIME`` values. The
application uses aware UTC values at process/API boundaries and converts to
naive UTC only at the persistence boundary.
"""

from __future__ import annotations

from datetime import UTC, datetime


def utc_now() -> datetime:
    """Return an aware current UTC timestamp."""

    return datetime.now(UTC)


def utc_now_db() -> datetime:
    """Return current UTC normalized for the legacy naive SQLite schema."""

    return utc_now().replace(tzinfo=None)


def as_utc(value: datetime) -> datetime:
    """Interpret a legacy naive value as UTC and return an aware timestamp."""

    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def utc_iso(value: datetime | None = None, *, timespec: str = "seconds") -> str:
    """Serialize a timestamp as an RFC 3339 UTC value ending in ``Z``."""

    normalized = as_utc(value) if value is not None else utc_now()
    return normalized.isoformat(timespec=timespec).replace("+00:00", "Z")


__all__ = ["as_utc", "utc_iso", "utc_now", "utc_now_db"]
