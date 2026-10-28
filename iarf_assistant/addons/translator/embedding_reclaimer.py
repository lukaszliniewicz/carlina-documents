"""Safe reclamation of replaceable Translator embedding vector tables.

The sqlite-vec tables are only a rebuildable projection.  The encrypted
relational generation rows are the authority for deciding whether a namespace
is still useful.  This module consequently uses a separate engine connection
and a small, deliberately strict identifier allow-list: a malformed or
foreign table name is never interpolated into a ``DROP TABLE`` statement.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import inspect, text

from ...extensions import db
from .models import TranslationEmbeddingGeneration

logger = logging.getLogger(__name__)

_NAMESPACE_RE = re.compile(r"^translator_emb_[a-z0-9_]{1,112}$")
_ROOT_TABLE_RE = re.compile(
    r"^vec_(translator_emb_[a-z0-9_]{1,112})_([1-9][0-9]*)$"
)
_LIVE_STATUSES = frozenset({"building", "ready", "active"})
type EmbeddingVectorIdentity = tuple[str, int]


@dataclass(frozen=True)
class EmbeddingReclamationReport:
    """Bounded result of one reconciliation or exact cleanup operation."""

    authority_available: bool
    inspected: int = 0
    kept: tuple[EmbeddingVectorIdentity, ...] = ()
    dropped: tuple[EmbeddingVectorIdentity, ...] = ()


def _validated_identity(
    namespace: object, dimensions: object
) -> EmbeddingVectorIdentity | None:
    """Return a safe identity or ``None`` for untrusted input.

    ``bool`` is intentionally rejected even though it is an ``int`` subclass;
    accepting ``True`` here would make an accidental flag select a vector
    table named ``..._1``.
    """

    if not isinstance(namespace, str) or not _NAMESPACE_RE.fullmatch(namespace):
        return None
    if isinstance(dimensions, bool):
        return None
    if isinstance(dimensions, int):
        value = dimensions
        raw_dimensions = str(dimensions)
    elif isinstance(dimensions, str):
        raw_dimensions = dimensions.strip()
        try:
            value = int(raw_dimensions)
        except (TypeError, ValueError, OverflowError):
            return None
    else:
        return None
    if value <= 0 or str(value) != raw_dimensions:
        return None
    table_name = f"vec_{namespace}_{value}"
    if _ROOT_TABLE_RE.fullmatch(table_name) is None:
        return None
    return namespace, value


def _identity_from_table_name(table_name: object) -> EmbeddingVectorIdentity | None:
    if not isinstance(table_name, str):
        return None
    match = _ROOT_TABLE_RE.fullmatch(table_name)
    if match is None:
        return None
    return _validated_identity(match.group(1), match.group(2))


def _table_name(identity: EmbeddingVectorIdentity) -> str:
    """Construct a name only after validating its complete identity."""

    validated = _validated_identity(*identity)
    if validated is None:  # pragma: no cover - callers validate at the edge.
        raise ValueError("invalid Translator embedding vector identity")
    namespace, dimensions = validated
    return f"vec_{namespace}_{dimensions}"


def _quote_table(conn: Any, identity: EmbeddingVectorIdentity) -> str:
    return conn.dialect.identifier_preparer.quote(_table_name(identity))


def _sqlite_table_names(conn: Any) -> tuple[str, ...]:
    """Read all SQLite table names, including virtual tables.

    sqlite-vec roots and their shadow tables are both exposed as table-like
    entries.  The strict root-name parser below admits only the root, so a
    shadow suffix can never be dropped independently.
    """

    rows = conn.execute(
        text("SELECT name FROM sqlite_master WHERE type = 'table'")
    ).scalars()
    return tuple(value for value in rows if isinstance(value, str))


def _authority_identities(conn: Any) -> tuple[set[EmbeddingVectorIdentity], bool]:
    """Return live generation identities and whether authority was readable."""

    table_name = TranslationEmbeddingGeneration.__tablename__
    try:
        if not inspect(conn).has_table(table_name):
            return set(), False
        rows = conn.execute(
            text(
                f"SELECT namespace, dimensions, status FROM {table_name}"
            )
        ).all()
    except Exception:
        logger.exception("Translator embedding authority could not be read")
        return set(), False

    keep: set[EmbeddingVectorIdentity] = set()
    for namespace, dimensions, status in rows:
        if status not in _LIVE_STATUSES:
            continue
        identity = _validated_identity(namespace, dimensions)
        if identity is not None:
            keep.add(identity)
    # An active/runnable operation may still need to perform new window-time
    # retrieval from a baseline-pinned retired generation.  Keep that exact
    # namespace until the operation is terminal; packet replay itself does
    # not need vector state, so completed/failed/cancelled work can reclaim.
    if (
        inspect(conn).has_table("translation_context_baseline_item")
        and inspect(conn).has_table("translation_context_baseline")
        and inspect(conn).has_table("translation_operation")
    ):
        try:
            referenced = conn.execute(
                text(
                    "SELECT g.namespace, g.dimensions "
                    "FROM translation_context_baseline_item bi "
                    "JOIN translation_context_baseline b ON b.id = bi.baseline_id "
                    "JOIN translation_operation o ON o.id = b.operation_id "
                    "JOIN translation_embedding_generation g "
                    "ON g.id = bi.embedding_generation_id "
                    "WHERE bi.embedding_generation_id IS NOT NULL "
                    "AND o.state IN ('queued', 'running', 'cancel_requested')"
                )
            ).all()
        except Exception:
            logger.exception("Translator embedding baseline references could not be read")
            return set(), False
        for namespace, dimensions in referenced:
            identity = _validated_identity(namespace, dimensions)
            if identity is not None:
                keep.add(identity)
    return keep, True


def _reconcile_in_connection(conn: Any) -> EmbeddingReclamationReport:
    # The implementation is SQLite-specific by design: the shipped vector
    # projection is sqlite-vec and the table enumeration query must include its
    # virtual roots.  Other engines fail closed without attempting DDL.
    if conn.dialect.name != "sqlite":
        return EmbeddingReclamationReport(authority_available=False)

    keep, authority_available = _authority_identities(conn)
    if not authority_available:
        return EmbeddingReclamationReport(authority_available=False)

    roots = {
        identity
        for name in _sqlite_table_names(conn)
        if (identity := _identity_from_table_name(name)) is not None
    }
    dropped: list[EmbeddingVectorIdentity] = []
    for identity in sorted(roots - keep):
        conn.execute(text(f"DROP TABLE IF EXISTS {_quote_table(conn, identity)}"))
        dropped.append(identity)
    return EmbeddingReclamationReport(
        authority_available=True,
        inspected=len(roots),
        kept=tuple(sorted(roots & keep)),
        dropped=tuple(dropped),
    )


def reconcile_embedding_vector_tables(application: Any) -> EmbeddingReclamationReport:
    """Reconcile all recognized Translator vector roots for ``application``.

    The scheduler passes the trusted Flask application object.  A fresh app
    context and engine transaction ensure this job never shares the request
    session or a worker process's stale transaction.
    """

    with application.app_context(), db.engine.begin() as conn:
        report = _reconcile_in_connection(conn)
    logger.info(
        "Translator embedding reconciliation inspected=%d kept=%d dropped=%d",
        report.inspected,
        len(report.kept),
        len(report.dropped),
    )
    return report


def cleanup_embedding_vector_identities(
    application: Any, identities: Iterable[tuple[object, object]]
) -> EmbeddingReclamationReport:
    """Drop exact, pre-collected project identities after DB deletion commits.

    Callers use it after a project's generation rows have been cascaded away,
    but it still re-reads relational authority.  A stale or mis-scoped caller
    must never be able to delete a table that another live generation retains.
    """

    validated = {
        identity
        for raw in identities
        if isinstance(raw, tuple)
        and len(raw) == 2
        and (identity := _validated_identity(raw[0], raw[1])) is not None
    }
    if not validated:
        return EmbeddingReclamationReport(authority_available=True)

    with application.app_context(), db.engine.begin() as conn:
        if conn.dialect.name != "sqlite":
            return EmbeddingReclamationReport(authority_available=False)
        keep, authority_available = _authority_identities(conn)
        if not authority_available:
            return EmbeddingReclamationReport(authority_available=False)
        dropped: list[EmbeddingVectorIdentity] = []
        for identity in sorted(validated - keep):
            exists = conn.execute(
                text(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                    "AND name = :name"
                ),
                {"name": _table_name(identity)},
            ).first()
            if exists is None:
                continue
            conn.execute(text(f"DROP TABLE IF EXISTS {_quote_table(conn, identity)}"))
            dropped.append(identity)
    report = EmbeddingReclamationReport(
        authority_available=True,
        inspected=len(validated),
        kept=tuple(sorted(validated & keep)),
        dropped=tuple(dropped),
    )
    logger.info(
        "Translator embedding exact cleanup inspected=%d dropped=%d",
        report.inspected,
        len(report.dropped),
    )
    return report


__all__ = [
    "EmbeddingReclamationReport",
    "EmbeddingVectorIdentity",
    "cleanup_embedding_vector_identities",
    "reconcile_embedding_vector_tables",
]
