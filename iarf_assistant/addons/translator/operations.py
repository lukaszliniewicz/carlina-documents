"""Durable state transitions for translator workflows.

The add-on queue is a delivery mechanism and may retry, duplicate, or lose a
delivery after a process restart.  ``TranslationOperation`` is consequently
the authoritative owner/project-scoped ledger.  Callers enqueue a queue task
after creating a row, then use this module for all state and cancellation
changes.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast

from flask import has_app_context
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.attributes import flag_modified

from carlina.core.utc import utc_iso, utc_now_db

from ...extensions import db
from .block_contract import BlockContractError
from .block_persistence import (
    _ensure_database_transaction,
    rehydrate_source_plan,
    require_active_source_plan,
)
from .models import TranslationOperation, TranslationProject, TranslationSourcePlan

OPERATION_STATES = frozenset(
    {
        "queued",
        "running",
        "cancel_requested",
        "completed",
        "partial",
        "failed",
        "cancelled",
    }
)
ACTIVE_OPERATION_STATES = frozenset({"queued", "running", "cancel_requested"})
TERMINAL_OPERATION_STATES = frozenset({"completed", "partial", "failed", "cancelled"})

_ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "queued": frozenset(
        {"queued", "running", "cancel_requested", "cancelled", "failed"}
    ),
    "running": frozenset(
        {"running", "cancel_requested", "completed", "partial", "failed", "cancelled"}
    ),
    "cancel_requested": frozenset({"cancel_requested", "cancelled"}),
    "completed": frozenset(),
    "partial": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
}
_ERROR_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
OPERATION_LEASE_DURATION = timedelta(minutes=30)
QUEUED_DELIVERY_GRACE = timedelta(minutes=5)
# A published delivery may legitimately wait behind other work, so give a
# bound job substantially longer than the narrow publish/bind crash window.
# It still needs a finite deadline: queue loss must not hold the project's
# single-flight fence forever.
QUEUED_BOUND_DELIVERY_GRACE = timedelta(hours=2)
# Bounded maintenance scan: one scheduler tick reaps at most this many
# expired leases.  Per-item work uses short independent transactions and
# stops on the first busy signal instead of retrying 50 times.
OPERATION_MAINTENANCE_CANDIDATE_LIMIT = 50
# Cooperative wall-clock budget for one maintenance tick.  The loop stops
# between rows once this elapses and shrinks each row's retry allowance to
# the time that remains.  One underlying busy-wait may still overrun the
# soft budget; only the stop-between-rows is guaranteed.  Global DB
# durability and timeouts are unchanged.
OPERATION_MAINTENANCE_SOFT_BUDGET_SECONDS = 20.0

logger = logging.getLogger(__name__)


class TranslationOperationError(RuntimeError):
    """Base class for invalid translator operation lifecycle actions."""


class TranslationOperationConflict(TranslationOperationError):
    """A request conflicts with a durable operation's current identity/state."""


class TranslationOperationIdempotencyConflict(TranslationOperationConflict):
    """An idempotency key was reused for a different request payload."""

    def __init__(self, operation: TranslationOperation, expected: str, actual: str):
        super().__init__(
            "The idempotency key is already bound to a different translator request."
        )
        self.operation = operation
        self.expected_fingerprint = expected
        self.actual_fingerprint = actual


class TranslationOperationActiveConflict(TranslationOperationConflict):
    """A second operation would violate the project's single-flight fence."""

    def __init__(self, operation: TranslationOperation):
        super().__init__(
            f"Project already has translator operation '{operation.id}' in progress."
        )
        self.operation = operation


class TranslationProjectIdempotencyConflict(TranslationOperationConflict):
    """A project creation key is already bound to different request data."""

    def __init__(
        self,
        project: TranslationProject,
        expected: str,
        actual: str | None,
    ):
        super().__init__(
            "The project creation idempotency key is already bound to a different request."
        )
        self.project = project
        self.expected_fingerprint = expected
        self.actual_fingerprint = actual


class TranslationOperationTransitionConflict(TranslationOperationConflict):
    """A compare-and-set transition lost a race or used a stale version."""

    def __init__(self, operation_id: str, expected_version: int | None):
        suffix = (
            f" at version {expected_version}" if expected_version is not None else ""
        )
        super().__init__(
            f"Translator operation '{operation_id}' changed before this update{suffix}."
        )
        self.operation_id = operation_id
        self.expected_version = expected_version


class TranslationOperationClaimConflict(TranslationOperationConflict):
    """A queue delivery does not match the operation's attached job."""


class TranslationOperationNotRunnable(TranslationOperationConflict):
    """A delivery arrived after another worker claimed or finished the row."""

    def __init__(self, operation: TranslationOperation, reason: str):
        super().__init__(reason)
        self.operation = operation
        self.reason = reason


class TranslationOperationCancellation(TranslationOperationError):
    """Raised by a worker when cooperative cancellation has been observed."""


def canonical_request_fingerprint(payload: Any) -> str:
    """Return a deterministic SHA-256 fingerprint for a JSON-compatible request.

    Sorting object keys and using compact UTF-8 JSON makes equivalent request
    bodies idempotent across retries while rejecting non-JSON values instead of
    silently stringifying them into a different request.
    """

    def _default(value: Any) -> str:
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, uuid.UUID):
            return str(value)
        raise TypeError(f"Unsupported request value: {type(value).__name__}")

    encoded = json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
        default=_default,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _require_kind(kind: str) -> str:
    normalized = str(kind or "").strip()
    if not normalized or len(normalized) > 64:
        raise ValueError("Translator operation kind must be 1-64 characters.")
    return normalized


def _require_idempotency_key(value: str | None) -> str:
    if value is None or not str(value).strip():
        return f"auto-{uuid.uuid4().hex}"
    normalized = str(value).strip()
    if len(normalized) > 128:
        raise ValueError("Translator idempotency key must be at most 128 characters.")
    return normalized


def normalize_project_creation_idempotency_key(value: str | None) -> str | None:
    """Normalize an optional project-create key without inventing one.

    Unlike operation delivery keys, an absent project key intentionally keeps
    historical non-idempotent creation semantics.
    """

    if value is None:
        return None
    normalized = str(value).strip()
    if not normalized:
        return None
    if len(normalized) > 128:
        raise ValueError(
            "Project creation idempotency key must be at most 128 characters."
        )
    return normalized


def _normalize_creation_request_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            key: _normalize_creation_request_value(item) for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_normalize_creation_request_value(item) for item in value]
    if isinstance(value, str):
        return value.strip()
    return value


def project_creation_request_fingerprint(payload: Any) -> str:
    """Fingerprint the normalized request representation used for creation."""

    return canonical_request_fingerprint(_normalize_creation_request_value(payload))


def _safe_error_code(value: str | None) -> str | None:
    if value is None or not str(value).strip():
        return None
    normalized = str(value).strip().lower()
    if not _ERROR_CODE_RE.fullmatch(normalized):
        raise ValueError(
            "Translator error_code must contain lowercase letters, digits, and underscores."
        )
    return normalized


def _iso(value: datetime | None) -> str | None:
    return utc_iso(value) if value is not None else None


def _lease_deadline(now: datetime | None = None) -> datetime:
    """Return a naive UTC wall-clock deadline for SQLite-backed leases."""

    return (now or utc_now_db()) + OPERATION_LEASE_DURATION


def serialize_operation(operation: TranslationOperation) -> dict[str, Any]:
    """Return a safe browser/API projection without private payloads."""

    return {
        "id": operation.id,
        "project_id": operation.project_id,
        "kind": operation.kind,
        "queue_job_id": operation.queue_job_id,
        "state": operation.state,
        "version": operation.version,
        "progress": operation.progress,
        "message": operation.message,
        "error_code": operation.error_code,
        "source_content_hash": operation.source_content_hash,
        "cancel_requested": operation.cancel_requested_at is not None,
        "cancel_requested_at": _iso(operation.cancel_requested_at),
        "lease_expires_at": _iso(operation.lease_expires_at),
        "created_at": _iso(operation.created_at),
        "started_at": _iso(operation.started_at),
        "finished_at": _iso(operation.finished_at),
        "updated_at": _iso(operation.updated_at),
    }


def _status_for(
    project: TranslationProject, operation: TranslationOperation, state: str
) -> str:
    """Map an operation state onto the legacy project.status compatibility field."""

    if state in {"queued", "running"}:
        active_statuses = {
            "segment": "analyzing_structure",
            "plan_document_blocks": "analyzing_structure",
            "analyze": "analyzing_terminology",
            "analyze_terminology": "analyzing_terminology",
            "translate": "translating",
            "translate_segment": "translating",
            "document_block_translate": "translating",
            "document_block_export": "exporting",
        }
        return active_statuses.get(operation.kind.lower(), "translating")
    if state == "cancel_requested":
        return "cancelling"
    if state == "completed":
        return "ready"
    if state == "partial":
        return "partial"
    if state == "failed":
        return "failed"
    if state == "cancelled":
        return "cancelled"
    return project.status


def _operation_for_identity(
    *, owner_user_id: int, project_id: str, kind: str, idempotency_key: str
) -> TranslationOperation | None:
    return TranslationOperation.query.filter_by(
        owner_user_id=owner_user_id,
        project_id=project_id,
        kind=kind,
        idempotency_key=idempotency_key,
    ).one_or_none()


def _require_digest(value: str | None, label: str) -> None:
    if value is not None and (
        len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest.")


def _pin_fingerprint(
    *,
    source_content_hash: str | None,
    source_plan_id: str | None,
    source_plan_hash: str | None,
) -> str:
    return canonical_request_fingerprint(
        {
            "source_content_hash": source_content_hash,
            "source_plan_id": source_plan_id,
            "source_plan_hash": source_plan_hash,
        }
    )


def _pins_match(
    operation: TranslationOperation,
    *,
    source_content_hash: str | None,
    source_plan_id: str | None,
    source_plan_hash: str | None,
) -> bool:
    # Omitted pins preserve historical idempotency.  Any explicitly supplied
    # pin must match the durable row exactly; callers cannot silently upgrade
    # an unpinned retry into a different source identity.
    return all(
        supplied is None or getattr(operation, field) == supplied
        for field, supplied in (
            ("source_content_hash", source_content_hash),
            ("source_plan_id", source_plan_id),
            ("source_plan_hash", source_plan_hash),
        )
    )


def _raise_pin_conflict(
    operation: TranslationOperation,
    *,
    source_content_hash: str | None,
    source_plan_id: str | None,
    source_plan_hash: str | None,
) -> None:
    raise TranslationOperationIdempotencyConflict(
        operation,
        _pin_fingerprint(
            source_content_hash=operation.source_content_hash,
            source_plan_id=operation.source_plan_id,
            source_plan_hash=operation.source_plan_hash,
        ),
        _pin_fingerprint(
            source_content_hash=source_content_hash,
            source_plan_id=source_plan_id,
            source_plan_hash=source_plan_hash,
        ),
    )


def _project_for_creation_identity(
    *, owner_user_id: int, idempotency_key: str
) -> TranslationProject | None:
    return TranslationProject.query.filter_by(
        user_id=owner_user_id,
        creation_idempotency_key=idempotency_key,
    ).one_or_none()


def create_project_idempotently(
    project: TranslationProject,
    *,
    idempotency_key: str | None,
    request_payload: Any,
) -> tuple[TranslationProject, bool]:
    """Insert a project or reuse its owner/key equivalent in a savepoint.

    The candidate is added only after ``begin_nested`` establishes its
    SAVEPOINT.  This preserves unrelated pending outer work when a composite
    unique-index race occurs.  No commit is performed, allowing callers to
    add segments, snapshots, and the first operation in one transaction.
    Return ``(project, True)`` for a new row and ``(existing, False)`` for an
    exact fingerprint retry.  A missing key deliberately takes the historical
    non-idempotent path and always inserts a new row.
    """

    if not isinstance(project, TranslationProject):
        raise TypeError("A TranslationProject candidate is required.")
    if project.user_id is None:
        raise ValueError("A project owner is required before idempotent creation.")
    normalized_key = normalize_project_creation_idempotency_key(idempotency_key)
    if normalized_key is None:
        project.creation_idempotency_key = None
        project.creation_request_fingerprint = None
        db.session.add(project)
        db.session.flush()
        return project, True

    fingerprint = project_creation_request_fingerprint(request_payload)
    project.creation_idempotency_key = normalized_key
    project.creation_request_fingerprint = fingerprint
    existing = _project_for_creation_identity(
        owner_user_id=int(project.user_id),
        idempotency_key=normalized_key,
    )
    if existing is not None:
        if existing.creation_request_fingerprint != fingerprint:
            raise TranslationProjectIdempotencyConflict(
                existing,
                fingerprint,
                existing.creation_request_fingerprint,
            )
        return existing, False

    # A first SQLite SAVEPOINT is not an outer transaction in legacy driver
    # mode. Keep the project rollbackable if source preparation later fails.
    _ensure_database_transaction()
    try:
        with db.session.begin_nested():
            db.session.add(project)
            db.session.flush()
    except IntegrityError as exc:
        # The savepoint has already rolled back.  Keep the outer transaction
        # intact and resolve only the owner/key row that won the race.
        existing = _project_for_creation_identity(
            owner_user_id=int(project.user_id),
            idempotency_key=normalized_key,
        )
        if existing is None:
            raise
        if existing.creation_request_fingerprint != fingerprint:
            raise TranslationProjectIdempotencyConflict(
                existing,
                fingerprint,
                existing.creation_request_fingerprint,
            ) from exc
        return existing, False
    return project, True


def get_operation(
    operation_id: str, *, owner_user_id: int | None = None
) -> TranslationOperation | None:
    """Fetch an operation, optionally enforcing its owner boundary."""

    query = TranslationOperation.query.filter_by(id=str(operation_id))
    if owner_user_id is not None:
        query = query.filter_by(owner_user_id=int(owner_user_id))
    return query.one_or_none()


def find_active_operation(
    project_id: str, *, owner_user_id: int | None = None, kind: str | None = None
) -> TranslationOperation | None:
    """Return the newest active operation for a project, if any."""

    query = TranslationOperation.query.filter(
        TranslationOperation.project_id == str(project_id),
        TranslationOperation.state.in_(ACTIVE_OPERATION_STATES),
    )
    if owner_user_id is not None:
        query = query.filter(TranslationOperation.owner_user_id == int(owner_user_id))
    if kind is not None:
        query = query.filter(TranslationOperation.kind == _require_kind(kind))
    return query.order_by(
        TranslationOperation.created_at.desc(), TranslationOperation.id.desc()
    ).first()


def expire_stale_operation(
    operation: TranslationOperation | str,
    *,
    expected_version: int | None = None,
    now: datetime | None = None,
) -> TranslationOperation | None:
    """Reap one expired worker lease without committing the caller's unit.

    Running work becomes ``failed`` with the safe ``lease_expired`` code.  A
    cancellation request becomes ``cancelled`` because no worker remains
    alive to observe it; a linked started retranslation request is mirrored
    to ``cancelled`` in the same transaction through the existing
    ``synchronize_retranslation_request_terminal`` helper so subordinate
    work is not stranded.  A cancelled expiry additionally clears the exact
    project MCP execution binding (returning dispatch to managed) only when
    the binding is provably still the reaped delivery's: the owner/project
    identity matches, the execution id is unchanged since entry, the queue
    binding is unchanged, and no newer active operation remains.  A rebound
    or newer execution is never cleared.  Fresh, queued, and terminal rows
    are untouched and return ``None``.  The lease deadline is included in
    the CAS predicate so a concurrent renewal cannot be incorrectly reaped;
    a stale expected version raises
    ``TranslationOperationTransitionConflict`` when the row is still
    expired.
    """

    record = (
        operation
        if isinstance(operation, TranslationOperation)
        else get_operation(str(operation))
    )
    if record is None:
        raise KeyError(str(operation))
    current = str(record.state)
    current_now = now or utc_now_db()
    if (
        current not in {"running", "cancel_requested"}
        or record.lease_expires_at is None
        or record.lease_expires_at > current_now
    ):
        return None
    # Snapshot the provably-exact MCP binding before the version-fenced CAS.
    # The operation row carries no execution id; the project settings hold
    # the only binding.  Capture it now so a rebound/newer execution that
    # lands before our commit is never cleared.
    expected_queue_job_id = record.queue_job_id
    expected_execution_id = ""
    try:
        _snapshot_project = db.session.get(
            TranslationProject, record.project_id
        )
        if _snapshot_project is not None and int(
            _snapshot_project.user_id
        ) == int(record.owner_user_id):
            _snapshot_settings = dict(_snapshot_project.settings or {})
            expected_execution_id = str(
                _snapshot_settings.get("mcp_execution_id") or ""
            ).strip()
    except Exception:  # noqa: BLE001 - snapshot failure means no safe clear.
        expected_execution_id = ""
    current_version = (
        record.version if expected_version is None else int(expected_version)
    )
    target = "failed" if current == "running" else "cancelled"
    message = (
        "Worker lease expired before the translation operation completed."
        if target == "failed"
        else "Cancellation completed after the worker lease expired."
    )
    changed = TranslationOperation.query.filter(
        TranslationOperation.id == record.id,
        TranslationOperation.state == current,
        TranslationOperation.version == current_version,
        TranslationOperation.lease_expires_at <= current_now,
    ).update(
        {
            TranslationOperation.state: target,
            TranslationOperation.version: TranslationOperation.version + 1,
            TranslationOperation.message: message,
            TranslationOperation.error_code: "lease_expired"
            if target == "failed"
            else None,
            TranslationOperation.lease_expires_at: None,
            TranslationOperation.finished_at: current_now,
            TranslationOperation.updated_at: current_now,
        },
        synchronize_session=False,
    )
    if changed != 1:
        db.session.refresh(record)
        if (
            record.state in {"running", "cancel_requested"}
            and record.lease_expires_at is not None
            and record.lease_expires_at <= current_now
        ):
            raise TranslationOperationTransitionConflict(record.id, current_version)
        return None
    project = db.session.get(TranslationProject, record.project_id)
    if project is not None:
        project.status = _status_for(project, record, target)
        project.updated_at = current_now
    if target == "cancelled":
        # Deferred import: scholarly_review never imports this module, and
        # keeping the import local avoids any import-time cycle through the
        # block persistence chain.  The helper flushes inside the caller's
        # open transaction and is a no-op without a linked started request.
        from .scholarly_review import synchronize_retranslation_request_terminal

        db.session.refresh(record)
        synchronize_retranslation_request_terminal(record, "cancelled")
        _clear_reaped_mcp_binding(
            record,
            expected_execution_id=expected_execution_id,
            expected_queue_job_id=expected_queue_job_id,
            now=current_now,
        )
    db.session.flush()
    db.session.refresh(record)
    return record


def _clear_reaped_mcp_binding(
    record: TranslationOperation,
    *,
    expected_execution_id: str,
    expected_queue_job_id: str | None,
    now: datetime,
) -> bool:
    """Clear the project's MCP binding only for the provably exact delivery.

    Returns ``True`` when the binding was cleared.  Owner, project, queue,
    and execution fences must all hold, and no newer active operation may
    remain; otherwise the current binding (possibly a rebound/newer
    execution) is preserved untouched.
    """

    expected = str(expected_execution_id or "").strip()
    if not expected:
        return False
    # Queue fence: the version-fenced CAS already guarantees no concurrent
    # queue rebinding, but re-check the refreshed row before touching the
    # project binding.
    try:
        db.session.refresh(record)
    except Exception:  # noqa: BLE001 - without a fresh row never clear.
        return False
    if (record.queue_job_id or None) != (expected_queue_job_id or None):
        return False
    fresh_project = db.session.get(TranslationProject, record.project_id)
    if fresh_project is None:
        return False
    try:
        if int(fresh_project.user_id) != int(record.owner_user_id):
            return False
    except (TypeError, ValueError):
        return False
    current_settings = dict(fresh_project.settings or {})
    current_execution = str(current_settings.get("mcp_execution_id") or "").strip()
    if not current_execution or current_execution != expected:
        return False
    # A newer active operation (even one reusing the same execution id)
    # owns the binding now; the reaped delivery must not steal it.
    remaining = find_active_operation(
        record.project_id, owner_user_id=int(record.owner_user_id)
    )
    if remaining is not None:
        return False
    if "mcp_execution_id" not in current_settings:
        return False
    current_settings.pop("mcp_execution_id", None)
    current_settings["execution_mode"] = "managed"
    fresh_project.settings = current_settings
    flag_modified(fresh_project, "settings")
    fresh_project.updated_at = now
    return True


def reconcile_expired_active_operation(
    project_id: str,
    owner_user_id: int,
    *,
    now: datetime | None = None,
) -> TranslationOperation | None:
    """Reap one expired project operation before enforcing single-flight.

    A running/cancelling row is reaped only after its explicit worker lease.
    A queued row is failed only after its delivery deadline: the short publish
    grace applies before binding, while a bound job gets a much longer queue
    grace. This releases the single-flight fence after either an outbox crash
    or durable queue loss. Fresh active rows and terminal rows remain untouched.
    """

    active = find_active_operation(project_id, owner_user_id=owner_user_id)
    if active is None:
        return None
    current_now = now or utc_now_db()
    if active.state == "queued":
        delivery_grace = (
            QUEUED_BOUND_DELIVERY_GRACE
            if active.queue_job_id
            else QUEUED_DELIVERY_GRACE
        )
        delivery_expired = active.updated_at <= current_now - delivery_grace
    else:
        delivery_expired = False
    if delivery_expired:
        bound = bool(active.queue_job_id)
        transition_operation(
            active,
            "failed",
            expected_version=active.version,
            message=(
                "Bound queue delivery did not start before its delivery deadline."
                if bound
                else "Queue delivery was not bound after the publish grace period."
            ),
            error_code="queue_delivery_lost",
        )
        return find_active_operation(project_id, owner_user_id=owner_user_id)
    if active.lease_expires_at is None or active.lease_expires_at > current_now:
        return active
    expire_stale_operation(active, now=current_now)
    return find_active_operation(project_id, owner_user_id=owner_user_id)


def create_operation(
    project: TranslationProject,
    *,
    kind: str,
    request_payload: Mapping[str, Any] | list[Any] | str | float | bool | None,
    idempotency_key: str | None = None,
    source_content_hash: str | None = None,
    source_plan_id: str | None = None,
    source_plan_hash: str | None = None,
    enforce_single_flight: bool = True,
    checkpoint: Any = None,
) -> tuple[TranslationOperation, bool]:
    """Create or reuse one operation without committing the caller's transaction.

    The returned boolean is ``True`` for a newly inserted row and ``False``
    when the same owner/project/kind/key/fingerprint already existed.  A
    duplicate key with a different fingerprint is a typed conflict.  A second
    active operation for this project/kind is rejected unless it is the exact
    idempotent retry.  The project status projection is updated before flush;
    callers must commit the surrounding unit of work or roll it back.
    """

    if not isinstance(project, TranslationProject) or not project.id:
        raise ValueError("A persisted TranslationProject is required.")
    owner_user_id = int(project.user_id)
    plan_pin_presence = source_plan_id is not None, source_plan_hash is not None
    if any(plan_pin_presence) and not all(
        (source_content_hash is not None, *plan_pin_presence)
    ):
        raise ValueError(
            "source_content_hash is required when source_plan_id and "
            "source_plan_hash are supplied."
        )
    _require_digest(source_content_hash, "source_content_hash")
    _require_digest(source_plan_hash, "source_plan_hash")
    if source_plan_id is not None:
        source_plan = db.session.get(TranslationSourcePlan, source_plan_id)
        if source_plan is None:
            raise TranslationOperationConflict(
                "The supplied translator source plan does not exist."
            )
        if source_plan.project_id != project.id:
            raise TranslationOperationConflict(
                "The supplied translator source plan belongs to another project."
            )
        if source_plan.plan_hash != source_plan_hash:
            raise TranslationOperationConflict(
                "The supplied translator source plan hash does not match the plan."
            )
        plan = rehydrate_source_plan(source_plan)
        if (
            source_content_hash is not None
            and source_content_hash != plan.snapshot.content_hash
        ):
            raise TranslationOperationConflict(
                "The supplied source content hash does not match the source plan."
            )
    normalized_kind = _require_kind(kind)
    normalized_key = _require_idempotency_key(idempotency_key)
    fingerprint = canonical_request_fingerprint(request_payload)
    existing = _operation_for_identity(
        owner_user_id=owner_user_id,
        project_id=project.id,
        kind=normalized_kind,
        idempotency_key=normalized_key,
    )
    if existing is not None:
        if existing.request_fingerprint != fingerprint:
            raise TranslationOperationIdempotencyConflict(
                existing, fingerprint, existing.request_fingerprint
            )
        if not _pins_match(
            existing,
            source_content_hash=source_content_hash,
            source_plan_id=source_plan_id,
            source_plan_hash=source_plan_hash,
        ):
            _raise_pin_conflict(
                existing,
                source_content_hash=source_content_hash,
                source_plan_id=source_plan_id,
                source_plan_hash=source_plan_hash,
            )
        return existing, False
    if source_plan_id is not None:
        try:
            require_active_source_plan(
                project.id,
                source_plan_id,
                lock=True,
            )
        except BlockContractError as exc:
            raise TranslationOperationConflict(
                "The supplied translator source plan is no longer active."
            ) from exc
    if enforce_single_flight:
        active = reconcile_expired_active_operation(
            project.id,
            owner_user_id,
        )
        if active is not None:
            raise TranslationOperationActiveConflict(active)

    operation = cast(Any, TranslationOperation)(
        project_id=project.id,
        owner_user_id=owner_user_id,
        kind=normalized_kind,
        idempotency_key=normalized_key,
        request_fingerprint=fingerprint,
        source_content_hash=source_content_hash,
        source_plan_id=source_plan_id,
        source_plan_hash=source_plan_hash,
        checkpoint_json=checkpoint,
        progress=0.0,
        message="Queued.",
    )
    try:
        # A savepoint keeps an IntegrityError from poisoning a caller's outer
        # transaction.  This is the SQLite-compatible uniqueness fence for
        # concurrent requests; the unique constraint remains authoritative.
        with db.session.begin_nested():
            # ``begin_nested`` flushes pending state before establishing its
            # SAVEPOINT.  Add the candidate only after the nested transaction
            # exists, otherwise an INSERT error can occur outside the
            # savepoint and poison unrelated caller work.
            db.session.add(operation)
            db.session.flush()
    except IntegrityError as exc:
        # Only recover a uniqueness race.  Other errors must retain their
        # original exception and leave the outer transaction usable.
        existing = _operation_for_identity(
            owner_user_id=owner_user_id,
            project_id=project.id,
            kind=normalized_kind,
            idempotency_key=normalized_key,
        )
        if existing is None:
            active = find_active_operation(project.id, owner_user_id=owner_user_id)
            if active is not None:
                raise TranslationOperationActiveConflict(active) from exc
            raise
        if existing.request_fingerprint != fingerprint:
            raise TranslationOperationIdempotencyConflict(
                existing, fingerprint, existing.request_fingerprint
            ) from exc
        if not _pins_match(
            existing,
            source_content_hash=source_content_hash,
            source_plan_id=source_plan_id,
            source_plan_hash=source_plan_hash,
        ):
            _raise_pin_conflict(
                existing,
                source_content_hash=source_content_hash,
                source_plan_id=source_plan_id,
                source_plan_hash=source_plan_hash,
            )
        return existing, False
    project.status = _status_for(project, operation, operation.state)
    project.updated_at = utc_now_db()
    db.session.flush()
    return operation, True


def attach_queue_job(
    operation: TranslationOperation | str,
    queue_job_id: str,
    *,
    expected_version: int | None = None,
) -> TranslationOperation:
    """Bind the delivery identifier without allowing it to replace another job."""

    record = (
        operation
        if isinstance(operation, TranslationOperation)
        else get_operation(str(operation))
    )
    if record is None:
        raise KeyError(str(operation))
    normalized_job_id = str(queue_job_id or "").strip()
    if not normalized_job_id or len(normalized_job_id) > 128:
        raise ValueError(
            "A queue job id is required and must be at most 128 characters."
        )
    if record.queue_job_id:
        if record.queue_job_id == normalized_job_id:
            return record
        raise TranslationOperationConflict(
            "Translator operation is already attached to another queue job."
        )
    current_version = (
        record.version if expected_version is None else int(expected_version)
    )
    changed = TranslationOperation.query.filter(
        TranslationOperation.id == record.id,
        TranslationOperation.version == current_version,
        TranslationOperation.queue_job_id.is_(None),
    ).update(
        {
            TranslationOperation.queue_job_id: normalized_job_id,
            TranslationOperation.version: TranslationOperation.version + 1,
            TranslationOperation.updated_at: utc_now_db(),
        },
        synchronize_session=False,
    )
    if changed != 1:
        raise TranslationOperationTransitionConflict(record.id, current_version)
    db.session.flush()
    db.session.refresh(record)
    return record


def passive_operation_can_wait(
    operation_id: str,
    queue_job_id: str,
    *,
    owner_user_id: int,
    project_id: str,
) -> bool:
    """Read only lease metadata before a sequential external wait is parked.

    Near-expiry, cancellation, terminal and mismatched rows return to ordinary
    worker entry so its authoritative claim/renewal/cancellation path runs.
    No encrypted checkpoint, project settings or source content is loaded.
    """
    if (
        not has_app_context()
        or not all(isinstance(value, str) and value for value in (
            operation_id, queue_job_id, project_id,
        ))
        or isinstance(owner_user_id, bool)
        or not isinstance(owner_user_id, int)
        or owner_user_id < 1
    ):
        return False
    query = select(TranslationOperation.id).where(
        TranslationOperation.id == operation_id,
        TranslationOperation.queue_job_id == queue_job_id,
        TranslationOperation.owner_user_id == owner_user_id,
        TranslationOperation.project_id == project_id,
        TranslationOperation.kind == "document_block_translate",
        TranslationOperation.state == "running",
        TranslationOperation.cancel_requested_at.is_(None),
        TranslationOperation.lease_expires_at > utc_now_db() + timedelta(minutes=5),
    )
    with db.engine.connect() as connection:
        return connection.execute(query).first() is not None


def claim_operation(
    operation_id: str,
    queue_job_id: str,
    *,
    owner_user_id: int | None = None,
    project_id: str | None = None,
    expected_version: int | None = None,
) -> TranslationOperation:
    """Atomically claim one queued delivery and transition it to ``running``.

    Queue implementations may return a job id before the route has had a
    chance to attach it to this ledger row.  A queued row with no attached job
    is therefore bound to the supplied id in the same compare-and-set update.
    If a different id is already attached, the delivery is rejected.  A
    duplicate claim is deferred while the current worker lease is live,
    except that the exact same queue delivery may re-enter through a
    version-fenced lease-renewal CAS.  The outer :class:`TaskQueue` delivery
    claim is the exclusive fence for that re-entry.  The same delivery may
    also recover an expired lease, but a different delivery can never take
    over the operation.
    This function does not commit, leaving queue acknowledgement and the
    caller's surrounding transaction under the caller's control.
    """

    normalized_job_id = str(queue_job_id or "").strip()
    if not normalized_job_id or len(normalized_job_id) > 128:
        raise ValueError(
            "A queue job id is required and must be at most 128 characters."
        )
    record = get_operation(operation_id, owner_user_id=owner_user_id)
    if record is None:
        raise KeyError(str(operation_id))
    if project_id is not None and record.project_id != str(project_id):
        raise TranslationOperationClaimConflict(
            "Translator operation does not belong to the supplied project."
        )
    if record.queue_job_id and record.queue_job_id != normalized_job_id:
        raise TranslationOperationClaimConflict(
            "Queue delivery does not match the operation's attached job."
        )
    current_version = (
        record.version if expected_version is None else int(expected_version)
    )
    now = utc_now_db()
    if (
        record.state == "running"
        and record.queue_job_id == normalized_job_id
        and record.lease_expires_at is not None
        and record.lease_expires_at <= now
    ):
        changed = TranslationOperation.query.filter(
            TranslationOperation.id == record.id,
            TranslationOperation.state == "running",
            TranslationOperation.version == current_version,
            TranslationOperation.queue_job_id == normalized_job_id,
            TranslationOperation.lease_expires_at <= now,
        ).update(
            {
                TranslationOperation.version: TranslationOperation.version + 1,
                TranslationOperation.lease_expires_at: _lease_deadline(now),
                TranslationOperation.message: "Recovered after a worker lease loss.",
                TranslationOperation.updated_at: now,
            },
            synchronize_session=False,
        )
        if changed != 1:
            db.session.refresh(record)
            raise TranslationOperationNotRunnable(
                record,
                "Another worker recovered the expired translation operation.",
            )
        project = db.session.get(TranslationProject, record.project_id)
        if project is not None:
            project.status = _status_for(project, record, "running")
            project.updated_at = now
        db.session.flush()
        db.session.refresh(record)
        return record
    # A queue delivery can be re-entered after a worker-side retry boundary.
    # The queue's exclusive delivery claim prevents concurrent execution; this
    # CAS renews the durable product lease without resetting state or
    # started_at.  Keep the expired branch above separate so its recovery
    # message and semantics remain unchanged.
    if (
        record.state == "running"
        and record.queue_job_id == normalized_job_id
        and record.lease_expires_at is not None
        and record.lease_expires_at > now
    ):
        changed = TranslationOperation.query.filter(
            TranslationOperation.id == record.id,
            TranslationOperation.state == "running",
            TranslationOperation.version == current_version,
            TranslationOperation.queue_job_id == normalized_job_id,
            TranslationOperation.lease_expires_at > now,
        ).update(
            {
                TranslationOperation.version: TranslationOperation.version + 1,
                TranslationOperation.lease_expires_at: _lease_deadline(now),
                TranslationOperation.updated_at: now,
            },
            synchronize_session=False,
        )
        if changed != 1:
            raise TranslationOperationTransitionConflict(record.id, current_version)
        project = db.session.get(TranslationProject, record.project_id)
        if project is not None:
            project.status = _status_for(project, record, "running")
            project.updated_at = now
        db.session.flush()
        db.session.refresh(record)
        return record
    if record.state != "queued":
        raise TranslationOperationNotRunnable(
            record,
            f"Translator operation is already {record.state} and cannot be claimed.",
        )
    changed = TranslationOperation.query.filter(
        TranslationOperation.id == record.id,
        TranslationOperation.state == "queued",
        TranslationOperation.version == current_version,
        or_(
            TranslationOperation.queue_job_id.is_(None),
            TranslationOperation.queue_job_id == normalized_job_id,
        ),
    ).update(
        {
            TranslationOperation.queue_job_id: normalized_job_id,
            TranslationOperation.state: "running",
            TranslationOperation.version: TranslationOperation.version + 1,
            TranslationOperation.lease_expires_at: _lease_deadline(now),
            TranslationOperation.started_at: now,
            TranslationOperation.updated_at: now,
        },
        synchronize_session=False,
    )
    if changed != 1:
        db.session.refresh(record)
        if record.state != "queued":
            raise TranslationOperationNotRunnable(
                record,
                f"Translator operation is already {record.state} and cannot be claimed.",
            )
        raise TranslationOperationTransitionConflict(record.id, current_version)
    project = db.session.get(TranslationProject, record.project_id)
    if project is not None:
        project.status = _status_for(project, record, "running")
        project.updated_at = now
    db.session.flush()
    db.session.refresh(record)
    return record


def transition_operation(
    operation: TranslationOperation | str,
    new_state: str,
    *,
    expected_version: int | None = None,
    progress: float | None = None,
    message: str | None = None,
    error_code: str | None = None,
    error_detail: str | None = None,
    checkpoint: Any = None,
    result: Any = None,
) -> TranslationOperation:
    """CAS-transition an operation and its project status in one transaction.

    Terminal states cannot transition again.  ``cancel_requested`` can only
    become ``cancelled``; ``finish_operation`` applies this cancellation fence
    when a worker races a cancellation request.  No commit is performed here.
    """

    target = str(new_state or "").strip()
    if target not in OPERATION_STATES:
        raise ValueError(f"Unsupported translator operation state: {target}")
    record = (
        operation
        if isinstance(operation, TranslationOperation)
        else get_operation(str(operation))
    )
    if record is None:
        raise KeyError(str(operation))
    current = str(record.state)
    if target not in _ALLOWED_TRANSITIONS.get(current, frozenset()):
        raise TranslationOperationError(
            f"Translator operation cannot transition from '{current}' to '{target}'."
        )
    if progress is not None and not 0 <= float(progress) <= 100:
        raise ValueError("Translator operation progress must be between 0 and 100.")
    safe_code = _safe_error_code(error_code)
    if message is not None and len(str(message)) > 255:
        raise ValueError("Translator operation message must be at most 255 characters.")
    current_version = (
        record.version if expected_version is None else int(expected_version)
    )
    now = utc_now_db()
    values: dict[Any, Any] = {
        TranslationOperation.state: target,
        TranslationOperation.version: TranslationOperation.version + 1,
        TranslationOperation.updated_at: now,
    }
    if progress is not None:
        values[TranslationOperation.progress] = float(progress)
    if message is not None:
        values[TranslationOperation.message] = str(message)
    if error_code is not None:
        values[TranslationOperation.error_code] = safe_code
    if error_detail is not None:
        values[TranslationOperation.error_detail] = str(error_detail)
    if checkpoint is not None:
        values[TranslationOperation.checkpoint_json] = checkpoint
    if result is not None:
        values[TranslationOperation.result_json] = result
    if target == "running" and record.started_at is None:
        values[TranslationOperation.started_at] = now
    if target == "running":
        values[TranslationOperation.lease_expires_at] = _lease_deadline(now)
    if target == "cancel_requested" and record.cancel_requested_at is None:
        values[TranslationOperation.cancel_requested_at] = now
    if target == "cancelled" and record.cancel_requested_at is None:
        values[TranslationOperation.cancel_requested_at] = now
    if target in TERMINAL_OPERATION_STATES:
        values[TranslationOperation.finished_at] = now
        values[TranslationOperation.lease_expires_at] = None
        if target == "completed":
            values[TranslationOperation.progress] = 100.0
    changed = TranslationOperation.query.filter(
        TranslationOperation.id == record.id,
        TranslationOperation.state == current,
        TranslationOperation.version == current_version,
    ).update(values, synchronize_session=False)
    if changed != 1:
        raise TranslationOperationTransitionConflict(record.id, current_version)
    project = db.session.get(TranslationProject, record.project_id)
    if project is not None:
        project.status = _status_for(project, record, target)
        project.updated_at = now
    db.session.flush()
    db.session.refresh(record)
    return record


def renew_operation_lease(
    operation: TranslationOperation | str,
    *,
    expected_version: int | None = None,
) -> TranslationOperation:
    """CAS-renew a running operation's 30-minute worker lease."""

    record = (
        operation
        if isinstance(operation, TranslationOperation)
        else get_operation(str(operation))
    )
    if record is None:
        raise KeyError(str(operation))
    if record.state != "running":
        raise TranslationOperationNotRunnable(
            record,
            f"Translator operation is {record.state}; only running work can renew a lease.",
        )
    current_version = (
        record.version if expected_version is None else int(expected_version)
    )
    now = utc_now_db()
    changed = TranslationOperation.query.filter(
        TranslationOperation.id == record.id,
        TranslationOperation.state == "running",
        TranslationOperation.version == current_version,
    ).update(
        {
            TranslationOperation.version: TranslationOperation.version + 1,
            TranslationOperation.lease_expires_at: _lease_deadline(now),
            TranslationOperation.updated_at: now,
        },
        synchronize_session=False,
    )
    if changed != 1:
        raise TranslationOperationTransitionConflict(record.id, current_version)
    db.session.flush()
    db.session.refresh(record)
    return record


def update_progress(
    operation: TranslationOperation | str,
    *,
    progress: float,
    message: str | None = None,
    checkpoint: Any = None,
    expected_version: int | None = None,
) -> TranslationOperation:
    """Persist progress/checkpoint while retaining the current nonterminal state."""

    record = (
        operation
        if isinstance(operation, TranslationOperation)
        else get_operation(str(operation))
    )
    if record is None:
        raise KeyError(str(operation))
    if record.state in TERMINAL_OPERATION_STATES:
        raise TranslationOperationError(
            "A terminal translator operation cannot receive progress updates."
        )
    return transition_operation(
        record,
        record.state,
        expected_version=expected_version,
        progress=progress,
        message=message,
        checkpoint=checkpoint,
    )


def request_cancellation(
    operation: TranslationOperation | str,
    *,
    expected_version: int | None = None,
) -> TranslationOperation:
    """Request cooperative cancellation; queued work is cancelled immediately."""

    record = (
        operation
        if isinstance(operation, TranslationOperation)
        else get_operation(str(operation))
    )
    if record is None:
        raise KeyError(str(operation))
    if record.state in TERMINAL_OPERATION_STATES:
        return record
    if record.state == "cancel_requested":
        return record
    target = "cancelled" if record.state == "queued" else "cancel_requested"
    result = transition_operation(
        record,
        target,
        expected_version=expected_version,
        message=(
            "Cancelled before execution."
            if target == "cancelled"
            else "Cancellation requested; waiting for a safe worker boundary."
        ),
    )
    if target == "cancel_requested":
        result.cancel_requested_at = utc_now_db()
        db.session.flush()
    return result


def is_cancellation_requested(operation: TranslationOperation | str) -> bool:
    """Read cancellation intent for a worker boundary check."""

    record = (
        operation
        if isinstance(operation, TranslationOperation)
        else get_operation(str(operation))
    )
    if record is None:
        raise KeyError(str(operation))
    db.session.refresh(record)
    return (
        record.state in {"cancel_requested", "cancelled"}
        or record.cancel_requested_at is not None
    )


def raise_if_cancellation_requested(operation: TranslationOperation | str) -> None:
    if is_cancellation_requested(operation):
        raise TranslationOperationCancellation(
            "Translator operation cancellation was observed at a safe boundary."
        )


def finish_operation(
    operation: TranslationOperation | str,
    state: str,
    *,
    progress: float | None = None,
    message: str | None = None,
    error_code: str | None = None,
    error_detail: str | None = None,
    result: Any = None,
    expected_version: int | None = None,
) -> TranslationOperation:
    """Finish a worker operation, making a pending cancellation win the race."""

    record = (
        operation
        if isinstance(operation, TranslationOperation)
        else get_operation(str(operation))
    )
    if record is None:
        raise KeyError(str(operation))
    requested = str(state or "").strip()
    if requested not in {"completed", "partial", "failed", "cancelled"}:
        raise ValueError("finish_operation requires a terminal translator state.")
    if requested == record.state:
        # Queue redelivery after a durable terminal write is harmless and
        # idempotent.  A different terminal state still fails validation.
        return record
    if record.state == "cancel_requested" and requested != "cancelled":
        requested = "cancelled"
        message = "Cancelled at a safe worker boundary."
        error_code = None
        error_detail = None
        # A late worker result must never become a successful/cancellable
        # operation result after the user has requested cancellation.
        result = None
    return transition_operation(
        record,
        requested,
        expected_version=expected_version,
        progress=progress,
        message=message,
        error_code=error_code,
        error_detail=error_detail,
        result=result,
    )


@dataclass(frozen=True)
class OperationLeaseReapReport:
    """Bounded result of one expired-lease maintenance tick."""

    inspected: int = 0
    reaped: int = 0
    failed: int = 0
    cancelled: int = 0
    skipped: int = 0
    busy_stopped: bool = False
    budget_exhausted: bool = False
    errors: int = 0


def acknowledge_same_delivery_cancellation(
    operation_id: str,
    queue_job_id: str,
    *,
    owner_user_id: int,
    project_id: str,
    expected_version: int | None = None,
) -> TranslationOperation | None:
    """Finish a ``cancel_requested`` row for its exact parked delivery.

    The caller has just observed ``TranslationOperationNotRunnable`` from
    :func:`claim_operation`.  A parked same-delivery resume must not run the
    handler or enter a passive execution context; it acknowledges cancellation
    through the normal ``cancelled`` ledger transition instead.  Identity is
    re-checked against a fresh owner/project/queue row and the transition is
    version-fenced, so a different delivery, a wrong owner/project, or a fresh
    unrelated lease is never finalized.  Terminal rows return ``None`` to
    preserve duplicate-delivery semantics.  No commit is performed; the caller
    mirrors a linked started retranslation request and clears the project's
    MCP binding in the same transaction before commit.
    """

    normalized_job_id = str(queue_job_id or "").strip()
    if (
        not normalized_job_id
        or len(normalized_job_id) > 128
        or not str(operation_id or "").strip()
        or not str(project_id or "").strip()
        or isinstance(owner_user_id, bool)
        or not isinstance(owner_user_id, int)
        or owner_user_id < 1
    ):
        return None
    record = get_operation(str(operation_id), owner_user_id=int(owner_user_id))
    if record is None:
        return None
    if record.project_id != str(project_id):
        return None
    if not record.queue_job_id or record.queue_job_id != normalized_job_id:
        return None
    if record.state != "cancel_requested":
        return None
    current_version = (
        record.version if expected_version is None else int(expected_version)
    )
    return finish_operation(
        record,
        "cancelled",
        expected_version=current_version,
        message="Cancelled at a safe worker boundary.",
    )


def _expired_lease_candidate_ids(*, now: datetime, limit: int) -> list[str]:
    """Return at most ``limit`` expired running/cancelling operation ids."""

    bounded = max(1, min(int(limit), OPERATION_MAINTENANCE_CANDIDATE_LIMIT))
    query = (
        select(TranslationOperation.id)
        .where(
            TranslationOperation.state.in_(("running", "cancel_requested")),
            TranslationOperation.lease_expires_at.is_not(None),
            TranslationOperation.lease_expires_at <= now,
        )
        .order_by(TranslationOperation.lease_expires_at.asc())
        .limit(bounded)
    )
    with db.engine.connect() as connection:
        rows = connection.execute(query).all()
    return [str(row[0]) for row in rows if row and row[0]]


def reap_expired_operation_leases(
    application: Any,
    *,
    limit: int = OPERATION_MAINTENANCE_CANDIDATE_LIMIT,
    per_item_timeout_seconds: float = 3.0,
    soft_budget_seconds: float = OPERATION_MAINTENANCE_SOFT_BUDGET_SECONDS,
) -> OperationLeaseReapReport:
    """Reap expired worker leases across all owners in bounded clean units.

    Candidate selection is metadata only (operation ids) and capped at 50.
    Each candidate is reaped in its own short transaction through
    :func:`expire_stale_operation`, whose ``lease_expires_at <= now`` CAS
    predicate guarantees a freshly renewed lease is never reaped.  Expired
    ``running`` becomes ``failed`` while expired ``cancel_requested`` becomes
    ``cancelled`` (with a linked started retranslation request mirrored in
    the same transaction).  The loop stops on the first busy signal instead
    of retrying every candidate, and a dirty caller session is never
    clobbered: a pending unit of work aborts the tick before any rollback.

    A cooperative monotonic wall-clock budget bounds total tick time from
    tick start (including the candidate scan): the loop stops between rows
    once ``soft_budget_seconds`` elapses and each row's retry allowance
    shrinks to the time that remains.  One underlying busy-wait may still
    overrun the soft budget; only the stop-between-rows is guaranteed.
    Committed rows are never rolled back by a later stop, so partial work
    resumes on the next tick.
    """

    # Local import keeps this maintenance entrypoint free of import-time
    # database-utility cycles; the retry helper remains the current utility.
    from ...database_utils import (
        DatabaseBusyError,
        TransactionScopeError,
        run_transaction_with_retry,
    )

    if application is None:
        return OperationLeaseReapReport()
    bounded_limit = max(1, min(int(limit), OPERATION_MAINTENANCE_CANDIDATE_LIMIT))
    per_item_timeout = max(0.5, min(float(per_item_timeout_seconds), 10.0))
    soft_budget = max(1.0, float(soft_budget_seconds))
    # Check the caller's session before pushing a new app context: a pending
    # unit of work must abort the tick instead of being rolled back away.
    try:
        caller_pending = (
            len(db.session.new),
            len(db.session.dirty),
            len(db.session.deleted),
        )
    except Exception:  # noqa: BLE001 - session inspection must never fail maintenance.
        caller_pending = (0, 0, 0)
    if any(caller_pending):
        logger.warning(
            "Translator lease maintenance skipped dirty session "
            "new=%d dirty=%d deleted=%d",
            *caller_pending,
        )
        return OperationLeaseReapReport(busy_stopped=False, errors=0)
    with application.app_context():
        tick_started = time.monotonic()
        try:
            candidate_ids = _expired_lease_candidate_ids(
                now=utc_now_db(), limit=bounded_limit
            )
        except Exception:
            logger.exception("Translator lease maintenance candidate scan failed")
            return OperationLeaseReapReport(errors=1)
        inspected = len(candidate_ids)
        reaped = failed = cancelled = skipped = errors = 0
        busy_stopped = False
        budget_exhausted = False
        for candidate_id in candidate_ids:
            remaining = soft_budget - (time.monotonic() - tick_started)
            if remaining < 0.5:
                budget_exhausted = True
                break
            item_timeout = min(per_item_timeout, max(0.5, remaining))

            def _reap_once(_session: Any, _candidate: str = candidate_id):
                return expire_stale_operation(_candidate)

            try:
                result = run_transaction_with_retry(
                    db.session,
                    _reap_once,
                    operation_name="translator_expire_stale_operation",
                    timeout_seconds=item_timeout,
                    require_clean_session=True,
                )
            except DatabaseBusyError:
                logger.warning(
                    "Translator lease maintenance stopped on busy candidate %s",
                    candidate_id,
                )
                busy_stopped = True
                break
            except TransactionScopeError:
                logger.warning("Translator lease maintenance stopped on dirty session")
                busy_stopped = True
                break
            except TranslationOperationTransitionConflict:
                # A concurrent renewal won the CAS race; the fresh lease must
                # survive.  Count it as skipped, not as a reap.
                skipped += 1
                continue
            except KeyError:
                skipped += 1
                continue
            except Exception:
                logger.exception(
                    "Translator lease maintenance failed for %s", candidate_id
                )
                errors += 1
                continue
            if result is None:
                skipped += 1
                continue
            reaped += 1
            if result.state == "failed":
                failed += 1
            elif result.state == "cancelled":
                cancelled += 1
            else:  # pragma: no cover - expire only yields failed/cancelled.
                skipped += 1
                reaped -= 1
        return OperationLeaseReapReport(
            inspected=inspected,
            reaped=reaped,
            failed=failed,
            cancelled=cancelled,
            skipped=skipped,
            busy_stopped=busy_stopped,
            budget_exhausted=budget_exhausted,
            errors=errors,
        )


__all__ = [
    "ACTIVE_OPERATION_STATES",
    "OPERATION_LEASE_DURATION",
    "OPERATION_MAINTENANCE_CANDIDATE_LIMIT",
    "OPERATION_MAINTENANCE_SOFT_BUDGET_SECONDS",
    "OPERATION_STATES",
    "TERMINAL_OPERATION_STATES",
    "OperationLeaseReapReport",
    "TranslationOperationActiveConflict",
    "TranslationOperationCancellation",
    "TranslationOperationClaimConflict",
    "TranslationOperationConflict",
    "TranslationOperationError",
    "TranslationOperationIdempotencyConflict",
    "TranslationOperationNotRunnable",
    "TranslationOperationTransitionConflict",
    "TranslationProjectIdempotencyConflict",
    "acknowledge_same_delivery_cancellation",
    "attach_queue_job",
    "canonical_request_fingerprint",
    "claim_operation",
    "create_operation",
    "create_project_idempotently",
    "expire_stale_operation",
    "find_active_operation",
    "finish_operation",
    "get_operation",
    "is_cancellation_requested",
    "normalize_project_creation_idempotency_key",
    "project_creation_request_fingerprint",
    "raise_if_cancellation_requested",
    "reap_expired_operation_leases",
    "reconcile_expired_active_operation",
    "renew_operation_lease",
    "request_cancellation",
    "serialize_operation",
    "transition_operation",
    "update_progress",
]
