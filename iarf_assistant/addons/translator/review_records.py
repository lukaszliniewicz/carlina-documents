"""Edit reasons, translator flags and QA finding dispositions.

Each record is append-only, encrypted and integrity-hashed: a SHA-256 record
hash over the canonical row values and a keyed integrity receipt over the same
values. Records are review evidence about a translation; none of them is ever
rendered into an export.

Flags and dispositions are event logs. The current state of a flag or finding
is its highest-sequence event, sequences are contiguous from 1, and the unique
sequence is the optimistic fence a caller names as ``expected_sequence``.
"""

from __future__ import annotations

import hashlib
import uuid
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

from sqlalchemy.exc import IntegrityError

from carlina.core.utc import utc_now_db

from ...extensions import db
from .block_contract import BlockContractError, canonical_json
from .block_persistence import ensure_database_transaction
from .block_workspace_api import load_block_source_plan
from .integrity_receipts import make_receipt, verify_receipt
from .models import (
    REVIEW_FLAG_KINDS,
    TARGET_EDIT_DEFECTS,
    TranslationQaDisposition,
    TranslationReviewFlagEvent,
    TranslationTargetBlockActive,
    TranslationTargetEditReason,
)
from .scholarly_review import (
    ReviewActor,
    ScholarlyReviewError,
    actor_can_mutate,
    actor_can_read,
)

_EDIT_REASON_MODEL: Any = TranslationTargetEditReason
_FLAG_MODEL: Any = TranslationReviewFlagEvent
_DISPOSITION_MODEL: Any = TranslationQaDisposition
_ACTIVE_MODEL: Any = TranslationTargetBlockActive

_EDIT_REASON_CONTRACT = "translator-edit-reason-v1"
_EDIT_REASON_RECEIPT = "translator-target-edit-reason-v1"
_FLAG_CONTRACT = "translator-review-flag-v1"
_FLAG_RECEIPT = "translator-review-flag-event-v1"
_FLAG_LIST_CONTRACT = "translator-review-flags-v1"
_DISPOSITION_CONTRACT = "translator-qa-disposition-v1"
_DISPOSITION_RECEIPT = "translator-qa-disposition-event-v1"

MAX_EDIT_NOTE_CHARS = 500
MAX_FLAG_BODY_CHARS = 1000
MAX_FLAG_EXCERPT_CHARS = 300
MAX_FLAG_NOTE_CHARS = 1000
MAX_DISPOSITION_REASON_CHARS = 500
MAX_FLAG_PAGE = 100
_MAX_KEY_CHARS = 128

FLAG_ORIGINS = ("agent", "human", "translation")
FLAG_STATES = ("open", "resolved", "dismissed")
FLAG_ACTIONS = ("resolve", "dismiss", "reopen")
DISPOSITION_BASES = ("false_positive", "intended")
_STATE_AFTER = {
    "open": "open",
    "reopen": "open",
    "resolve": "resolved",
    "dismiss": "dismissed",
}
_FLAG_ALLOWED_FROM = {
    "resolve": frozenset({"open"}),
    "dismiss": frozenset({"open"}),
    "reopen": frozenset({"resolved", "dismissed"}),
}

_EDIT_REASON_FIELDS = (
    "target_revision_id",
    "project_id",
    "source_plan_id",
    "block_id",
    "owner_user_id",
    "defect",
    "payload_json",
    "created_at",
)
_FLAG_FIELDS = (
    "id",
    "flag_id",
    "sequence",
    "project_id",
    "source_plan_id",
    "block_id",
    "owner_user_id",
    "action",
    "kind",
    "origin",
    "actor_type",
    "actor_user_id",
    "share_grant_id",
    "payload_json",
    "created_at",
)
_DISPOSITION_FIELDS = (
    "id",
    "finding_id",
    "sequence",
    "project_id",
    "source_plan_id",
    "block_id",
    "owner_user_id",
    "target_revision_id",
    "code",
    "action",
    "basis",
    "actor_type",
    "actor_user_id",
    "share_grant_id",
    "payload_json",
    "created_at",
)


def _error(code: str) -> ScholarlyReviewError:
    return ScholarlyReviewError(code)


def _session() -> Any:
    return cast(Any, db).session


# --- Shared integrity and validation helpers ---------------------------------


def _json_compatible(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        mapping = cast(Mapping[object, object], value)
        return {str(key): _json_compatible(item) for key, item in mapping.items()}
    if isinstance(value, (list, tuple)):
        items = cast(Sequence[object], value)
        return [_json_compatible(item) for item in items]
    return value


def _digest(values: Mapping[str, object]) -> str:
    encoded = canonical_json(_json_compatible(values)).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _receipt_material(values: Mapping[str, object]) -> dict[str, Any]:
    material = _json_compatible({"receipt_version": 1, **values})
    return cast(dict[str, Any], material)


def _seal(model: Any, values: dict[str, Any], purpose: str) -> Any:
    values["record_hash"] = _digest(values)
    values["integrity_receipt"] = make_receipt(
        purpose=purpose, values=_receipt_material(values)
    )
    return model(**values)


def _verify(
    row: Any, fields: Sequence[str], *, purpose: str, contract: str, code: str
) -> dict[str, Any]:
    values: dict[str, object] = {field: getattr(row, field) for field in fields}
    payload = values.get("payload_json")
    if not isinstance(payload, Mapping):
        raise _error(code)
    payload_map = cast(Mapping[str, Any], payload)
    if payload_map.get("contract") != contract:
        raise _error(code)
    record_hash = getattr(row, "record_hash")  # noqa: B009 - ORM row is Any.
    if record_hash != _digest(values):
        raise _error(code)
    try:
        verify_receipt(
            stored=getattr(row, "integrity_receipt"),  # noqa: B009
            purpose=purpose,
            values=_receipt_material({**values, "record_hash": record_hash}),
            code=code,
        )
    except BlockContractError as exc:
        raise _error(code) from exc
    return dict(payload_map)


def _clean_text(
    value: object, code: str, maximum: int, *, required: bool = True
) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str):
        raise _error(code)
    text = value.strip()
    if (required and not text) or len(text) > maximum or "\x00" in text:
        raise _error(code)
    return text


def _idempotency_key(value: object, code: str) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= _MAX_KEY_CHARS
        or value != value.strip()
    ):
        raise _error(code)
    return value


def _write_id(kind: str, *scope: object, key: str) -> str:
    material = ":".join(["carlina", "translator", kind, *(str(v) for v in scope), key])
    return str(uuid.uuid5(uuid.NAMESPACE_URL, material))


def _expected_sequence(value: object, code: str, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise _error(code)
    return value


def _require_mutator(project: Any, actor: ReviewActor) -> None:
    if not actor_can_mutate(project, actor):
        raise _error("review-mutation-forbidden")


def _require_reader(project: Any, actor: ReviewActor) -> None:
    if not actor_can_read(actor, project=project):
        raise _error("review-read-forbidden")


def _actor_values(actor: ReviewActor) -> dict[str, Any]:
    return {
        "actor_type": actor.role,
        "actor_user_id": actor.user_id if actor.role == "owner" else None,
        "share_grant_id": actor.share_grant_id,
    }


def _same_actor(row: Any, actor: ReviewActor) -> bool:
    expected = _actor_values(actor)
    return all(
        str(getattr(row, field)) == str(value) for field, value in expected.items()
    )


def _source_plan(project: Any) -> tuple[Any, list[Any]]:
    try:
        workspace = load_block_source_plan(project)
    except Exception as exc:
        raise _error(str(getattr(exc, "code", "source-plan-invalid"))) from exc
    record: Any = workspace.record
    blocks: list[Any] = list(cast(Any, workspace).plan.blocks)
    return record, blocks


def _block_order(blocks: Sequence[Any]) -> dict[str, int]:
    return {str(block.id): int(block.order) for block in blocks}


def _active_target(source_plan_id: str, block_id: str) -> tuple[str | None, int | None]:
    pointer = _session().get(
        _ACTIVE_MODEL, {"source_plan_id": source_plan_id, "block_id": block_id}
    )
    if pointer is None:
        return None, None
    return str(pointer.revision_id), int(pointer.version)


def _insert(row: Any) -> bool:
    """Insert under a savepoint; ``False`` means a unique key already won."""

    ensure_database_transaction()
    session = _session()
    try:
        with session.begin_nested():
            session.add(row)
            session.flush()
    except IntegrityError:
        return False
    return True


def _iso(value: object) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None


# --- Edit reasons -----------------------------------------------------------


def normalize_edit_reason(value: object) -> dict[str, str] | None:
    """Validate ``{"defect", "note"}``: a named defect and one line about it."""

    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise _error("edit-reason-shape")
    reason = cast(Mapping[str, object], value)
    if set(reason) != {"defect", "note"}:
        raise _error("edit-reason-shape")
    defect = reason.get("defect")
    if not isinstance(defect, str) or defect not in TARGET_EDIT_DEFECTS:
        raise _error("edit-reason-defect")
    note = _clean_text(reason.get("note"), "edit-reason-note", MAX_EDIT_NOTE_CHARS)
    return {"defect": defect, "note": note}


def persist_edit_reason(
    source_plan: Any,
    *,
    owner_user_id: int,
    block_id: str,
    target_revision_id: str,
    reason: Mapping[str, str],
) -> None:
    """Stage the reason row in the caller's transaction, beside its revision."""

    values: dict[str, Any] = {
        "target_revision_id": target_revision_id,
        "project_id": source_plan.project_id,
        "source_plan_id": source_plan.id,
        "block_id": block_id,
        "owner_user_id": owner_user_id,
        "defect": reason["defect"],
        "payload_json": {"contract": _EDIT_REASON_CONTRACT, "note": reason["note"]},
        "created_at": utc_now_db(),
    }
    _session().add(_seal(_EDIT_REASON_MODEL, values, _EDIT_REASON_RECEIPT))


def edit_reasons(
    source_plan_id: str, target_revision_ids: Sequence[str]
) -> dict[str, dict[str, str]]:
    """Return verified reasons keyed by target revision id."""

    wanted = [str(value) for value in target_revision_ids if value]
    if not wanted:
        return {}
    rows: list[Any] = list(
        _EDIT_REASON_MODEL.query.filter(
            _EDIT_REASON_MODEL.source_plan_id == source_plan_id,
            _EDIT_REASON_MODEL.target_revision_id.in_(wanted),
        ).all()
    )
    reasons: dict[str, dict[str, str]] = {}
    for row in rows:
        payload = _verify(
            row,
            _EDIT_REASON_FIELDS,
            purpose=_EDIT_REASON_RECEIPT,
            contract=_EDIT_REASON_CONTRACT,
            code="edit-reason-integrity",
        )
        reasons[str(row.target_revision_id)] = {
            "defect": str(row.defect),
            "note": str(payload.get("note", "")),
        }
    return reasons


# --- Translator flags -------------------------------------------------------


@dataclass(frozen=True)
class _FlagEvent:
    id: str
    flag_id: str
    sequence: int
    block_id: str
    action: str
    kind: str
    origin: str
    actor_type: str
    created_at: str | None
    body: str
    excerpt: str
    note: str
    target_version: int | None
    row: Any


def _flag_event(row: Any) -> _FlagEvent:
    payload = _verify(
        row,
        _FLAG_FIELDS,
        purpose=_FLAG_RECEIPT,
        contract=_FLAG_CONTRACT,
        code="review-flag-integrity",
    )
    target_version = payload.get("target_version")
    return _FlagEvent(
        id=str(row.id),
        flag_id=str(row.flag_id),
        sequence=int(row.sequence),
        block_id=str(row.block_id),
        action=str(row.action),
        kind=str(row.kind),
        origin=str(row.origin),
        actor_type=str(row.actor_type),
        created_at=_iso(row.created_at),
        body=str(payload.get("body", "")),
        excerpt=str(payload.get("excerpt", "")),
        note=str(payload.get("note", "")),
        target_version=target_version if isinstance(target_version, int) else None,
        row=row,
    )


def _flag_histories(
    project: Any, source_plan_id: str, flag_ids: Iterable[str] | None = None
) -> dict[str, list[_FlagEvent]]:
    query: Any = _FLAG_MODEL.query.filter(
        _FLAG_MODEL.project_id == str(project.id),
        _FLAG_MODEL.source_plan_id == source_plan_id,
    )
    if flag_ids is not None:
        query = query.filter(_FLAG_MODEL.flag_id.in_(list(flag_ids)))
    histories: dict[str, list[_FlagEvent]] = defaultdict(list)
    for row in cast(list[Any], query.all()):
        event = _flag_event(row)
        histories[event.flag_id].append(event)
    for flag_id, events in histories.items():
        events.sort(key=lambda event: event.sequence)
        first = events[0]
        if (
            [event.sequence for event in events] != list(range(1, len(events) + 1))
            or first.id != flag_id
            or first.action != "open"
            or any(
                (event.block_id, event.kind, event.origin)
                != (first.block_id, first.kind, first.origin)
                for event in events
            )
        ):
            raise _error("review-flag-integrity")
    return dict(histories)


def _flag_projection(
    events: Sequence[_FlagEvent], order: Mapping[str, int]
) -> dict[str, Any]:
    first, head = events[0], events[-1]
    result: dict[str, Any] = {
        "id": first.flag_id,
        "block_id": first.block_id,
        "order": order.get(first.block_id),
        "kind": first.kind,
        "origin": first.origin,
        "state": _STATE_AFTER[head.action],
        "sequence": head.sequence,
        "body": first.body,
        "target_version": first.target_version,
        "opened_at": first.created_at,
        "updated_at": head.created_at,
        "history": [
            {
                "sequence": event.sequence,
                "action": event.action,
                "actor_type": event.actor_type,
                "at": event.created_at,
                **({"note": event.note} if event.note else {}),
            }
            for event in events[1:]
        ],
    }
    if first.excerpt:
        result["excerpt"] = first.excerpt
    return result


def create_flag(
    project: Any,
    *,
    block_id: str,
    kind: str,
    body: str,
    actor: ReviewActor,
    idempotency_key: str,
    excerpt: str | None = None,
    origin: str = "agent",
) -> dict[str, Any]:
    """Open one block-anchored flag: an assumption, defect or doubt to revisit."""

    _require_mutator(project, actor)
    if kind not in REVIEW_FLAG_KINDS:
        raise _error("review-flag-kind")
    if origin not in FLAG_ORIGINS:
        raise _error("review-flag-origin")
    body = _clean_text(body, "review-flag-body", MAX_FLAG_BODY_CHARS)
    excerpt_text = _clean_text(
        excerpt, "review-flag-excerpt", MAX_FLAG_EXCERPT_CHARS, required=False
    )
    key = _idempotency_key(idempotency_key, "review-flag-idempotency")
    source_plan, blocks = _source_plan(project)
    order = _block_order(blocks)
    if block_id not in order:
        raise _error("review-flag-block")
    flag_id = _write_id("review-flag-open", project.id, source_plan.id, key=key)

    def replay() -> dict[str, Any] | None:
        events = _flag_histories(project, str(source_plan.id), [flag_id]).get(flag_id)
        if events is None:
            return None
        first = events[0]
        if (
            first.block_id != block_id
            or first.kind != kind
            or first.origin != origin
            or first.body != body
            or first.excerpt != excerpt_text
            or not _same_actor(first.row, actor)
        ):
            raise _error("review-flag-idempotency-conflict")
        return _flag_projection(events, order)

    existing = replay()
    if existing is not None:
        return existing
    target_revision_id, target_version = _active_target(str(source_plan.id), block_id)
    values: dict[str, Any] = {
        "id": flag_id,
        "flag_id": flag_id,
        "sequence": 1,
        "project_id": str(project.id),
        "source_plan_id": str(source_plan.id),
        "block_id": block_id,
        "owner_user_id": project.user_id,
        "action": "open",
        "kind": kind,
        "origin": origin,
        **_actor_values(actor),
        "payload_json": {
            "contract": _FLAG_CONTRACT,
            "body": body,
            "excerpt": excerpt_text,
            "note": "",
            "target_revision_id": target_revision_id,
            "target_version": target_version,
        },
        "created_at": utc_now_db(),
    }
    if not _insert(_seal(_FLAG_MODEL, values, _FLAG_RECEIPT)):
        existing = replay()
        if existing is None:
            raise _error("review-flag-persist-failed")
        return existing
    events = _flag_histories(project, str(source_plan.id), [flag_id])[flag_id]
    return _flag_projection(events, order)


def transition_flag(
    project: Any,
    *,
    flag_id: str,
    action: str,
    expected_sequence: int,
    actor: ReviewActor,
    idempotency_key: str,
    note: str | None = None,
) -> dict[str, Any]:
    """Resolve, dismiss or reopen a flag, fenced on its current sequence."""

    _require_mutator(project, actor)
    if action not in FLAG_ACTIONS:
        raise _error("review-flag-action")
    expected = _expected_sequence(expected_sequence, "review-flag-version", minimum=1)
    note_text = _clean_text(
        note, "review-flag-note", MAX_FLAG_NOTE_CHARS, required=action != "reopen"
    )
    key = _idempotency_key(idempotency_key, "review-flag-idempotency")
    source_plan, blocks = _source_plan(project)
    order = _block_order(blocks)
    event_id = _write_id("review-flag-event", project.id, flag_id, key=key)

    def history() -> list[_FlagEvent]:
        events = _flag_histories(project, str(source_plan.id), [flag_id]).get(flag_id)
        if events is None:
            raise _error("review-flag-missing")
        return events

    def replay(events: Sequence[_FlagEvent]) -> dict[str, Any] | None:
        match = next((event for event in events if event.id == event_id), None)
        if match is None:
            return None
        if (
            match.action != action
            or match.sequence != expected + 1
            or match.note != note_text
            or not _same_actor(match.row, actor)
        ):
            raise _error("review-flag-idempotency-conflict")
        return _flag_projection(events, order)

    events = history()
    existing = replay(events)
    if existing is not None:
        return existing
    head = events[-1]
    if head.sequence != expected:
        raise _error("review-flag-version-conflict")
    if _STATE_AFTER[head.action] not in _FLAG_ALLOWED_FROM[action]:
        raise _error("review-flag-transition")
    first = events[0]
    values: dict[str, Any] = {
        "id": event_id,
        "flag_id": flag_id,
        "sequence": head.sequence + 1,
        "project_id": str(project.id),
        "source_plan_id": str(source_plan.id),
        "block_id": first.block_id,
        "owner_user_id": project.user_id,
        "action": action,
        "kind": first.kind,
        "origin": first.origin,
        **_actor_values(actor),
        "payload_json": {
            "contract": _FLAG_CONTRACT,
            "body": "",
            "excerpt": "",
            "note": note_text,
            "target_revision_id": None,
            "target_version": None,
        },
        "created_at": utc_now_db(),
    }
    if not _insert(_seal(_FLAG_MODEL, values, _FLAG_RECEIPT)):
        events = history()
        existing = replay(events)
        if existing is None:
            raise _error("review-flag-version-conflict")
        return existing
    return _flag_projection(history(), order)


def _flag_filters(
    states: Sequence[str] | None, kinds: Sequence[str] | None
) -> tuple[frozenset[str], frozenset[str]]:
    state_set = frozenset(states or ("open",))
    if not state_set <= frozenset(FLAG_STATES):
        raise _error("review-flag-state")
    kind_set = frozenset(kinds or REVIEW_FLAG_KINDS)
    if not kind_set <= frozenset(REVIEW_FLAG_KINDS):
        raise _error("review-flag-kind")
    return state_set, kind_set


def list_flags(
    project: Any,
    *,
    actor: ReviewActor,
    states: Sequence[str] | None = None,
    kinds: Sequence[str] | None = None,
    block_ids: Sequence[str] | None = None,
    offset: object = 0,
    limit: object = 50,
) -> dict[str, Any]:
    """List flags in document order, open ones by default, with state counts."""

    _require_reader(project, actor)
    state_set, kind_set = _flag_filters(states, kinds)
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise _error("review-flag-offset")
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= MAX_FLAG_PAGE
    ):
        raise _error("review-flag-limit")
    source_plan, blocks = _source_plan(project)
    order = _block_order(blocks)
    wanted_blocks = None if block_ids is None else frozenset(block_ids)
    projections = [
        _flag_projection(events, order)
        for events in _flag_histories(project, str(source_plan.id)).values()
    ]
    counts = dict.fromkeys(FLAG_STATES, 0)
    open_by_kind = dict.fromkeys(REVIEW_FLAG_KINDS, 0)
    for item in projections:
        counts[item["state"]] += 1
        if item["state"] == "open":
            open_by_kind[item["kind"]] += 1
    selected = sorted(
        (
            item
            for item in projections
            if item["state"] in state_set
            and item["kind"] in kind_set
            and (wanted_blocks is None or item["block_id"] in wanted_blocks)
        ),
        key=lambda item: (item["order"] or 0, item["opened_at"] or "", item["id"]),
    )
    page = selected[offset : offset + limit]
    return {
        "contract": _FLAG_LIST_CONTRACT,
        "source_plan": {"id": str(source_plan.id)},
        "items": page,
        "summary": {**counts, "open_by_kind": open_by_kind},
        "pagination": {
            "offset": offset,
            "limit": limit,
            "total": len(selected),
            "has_more": offset + len(page) < len(selected),
        },
    }


def open_flags_by_block(
    project: Any, source_plan_id: str, blocks: Sequence[Any]
) -> dict[str, list[dict[str, Any]]]:
    """Open flags for review projections, keyed by block id."""

    order = _block_order(blocks)
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for events in _flag_histories(project, source_plan_id).values():
        item = _flag_projection(events, order)
        if item["state"] == "open" and item["block_id"] in order:
            grouped[item["block_id"]].append(item)
    return dict(grouped)


# --- QA finding dispositions -------------------------------------------------


@dataclass(frozen=True)
class _Disposition:
    id: str
    finding_id: str
    sequence: int
    block_id: str
    code: str
    action: str
    basis: str | None
    reason: str
    created_at: str | None
    row: Any


def _disposition(row: Any) -> _Disposition:
    payload = _verify(
        row,
        _DISPOSITION_FIELDS,
        purpose=_DISPOSITION_RECEIPT,
        contract=_DISPOSITION_CONTRACT,
        code="qa-disposition-integrity",
    )
    basis = getattr(row, "basis")  # noqa: B009 - ORM row is Any.
    return _Disposition(
        id=str(row.id),
        finding_id=str(row.finding_id),
        sequence=int(row.sequence),
        block_id=str(row.block_id),
        code=str(row.code),
        action=str(row.action),
        basis=str(basis) if basis is not None else None,
        reason=str(payload.get("reason", "")),
        created_at=_iso(row.created_at),
        row=row,
    )


def _disposition_histories(
    project: Any, source_plan_id: str, finding_id: str | None = None
) -> dict[str, list[_Disposition]]:
    query: Any = _DISPOSITION_MODEL.query.filter(
        _DISPOSITION_MODEL.project_id == str(project.id),
        _DISPOSITION_MODEL.source_plan_id == source_plan_id,
    )
    if finding_id is not None:
        query = query.filter(_DISPOSITION_MODEL.finding_id == finding_id)
    histories: dict[str, list[_Disposition]] = defaultdict(list)
    for row in cast(list[Any], query.all()):
        event = _disposition(row)
        histories[event.finding_id].append(event)
    for events in histories.values():
        events.sort(key=lambda event: event.sequence)
        if [event.sequence for event in events] != list(range(1, len(events) + 1)):
            raise _error("qa-disposition-integrity")
    return dict(histories)


def _disposition_projection(events: Sequence[_Disposition]) -> dict[str, Any]:
    head = events[-1]
    dismissed = head.action == "dismiss"
    result: dict[str, Any] = {
        "finding_id": head.finding_id,
        "state": "dismissed" if dismissed else "open",
        "sequence": head.sequence,
        "decided_at": head.created_at,
    }
    if dismissed:
        result["basis"] = head.basis
        result["reason"] = head.reason
    return result


def qa_dispositions(project: Any, source_plan_id: str) -> dict[str, dict[str, Any]]:
    """Latest decision per finding id for one source plan."""

    return {
        finding_id: _disposition_projection(events)
        for finding_id, events in _disposition_histories(
            project, source_plan_id
        ).items()
    }


def record_qa_disposition(
    project: Any,
    *,
    source_plan_id: str,
    finding: Mapping[str, Any],
    target_revision_id: str,
    action: str,
    expected_sequence: int,
    actor: ReviewActor,
    idempotency_key: str,
    basis: str | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    """Dismiss or reopen one current finding; the caller proves it is current."""

    _require_mutator(project, actor)
    if action not in {"dismiss", "reopen"}:
        raise _error("qa-disposition-action")
    if action == "dismiss" and basis not in DISPOSITION_BASES:
        raise _error("qa-disposition-basis")
    if action == "reopen" and basis is not None:
        raise _error("qa-disposition-basis")
    reason_text = _clean_text(
        reason,
        "qa-disposition-reason",
        MAX_DISPOSITION_REASON_CHARS,
        required=action == "dismiss",
    )
    expected = _expected_sequence(
        expected_sequence, "qa-disposition-version", minimum=0
    )
    key = _idempotency_key(idempotency_key, "qa-disposition-idempotency")
    finding_id = str(finding["id"])
    block_id = str(finding["block_id"])
    event_id = _write_id(
        "qa-disposition", project.id, source_plan_id, finding_id, key=key
    )

    def history() -> list[_Disposition]:
        return _disposition_histories(project, source_plan_id, finding_id).get(
            finding_id, []
        )

    def replay(events: Sequence[_Disposition]) -> dict[str, Any] | None:
        match = next((event for event in events if event.id == event_id), None)
        if match is None:
            return None
        if (
            match.action != action
            or match.basis != basis
            or match.reason != reason_text
            or match.sequence != expected + 1
            or not _same_actor(match.row, actor)
        ):
            raise _error("qa-disposition-idempotency-conflict")
        return _disposition_projection(events)

    events = history()
    existing = replay(events)
    if existing is not None:
        return existing
    head_sequence = events[-1].sequence if events else 0
    if head_sequence != expected:
        raise _error("qa-disposition-version-conflict")
    dismissed = bool(events) and events[-1].action == "dismiss"
    if (action == "dismiss") == dismissed:
        raise _error("qa-disposition-transition")
    values: dict[str, Any] = {
        "id": event_id,
        "finding_id": finding_id,
        "sequence": head_sequence + 1,
        "project_id": str(project.id),
        "source_plan_id": source_plan_id,
        "block_id": block_id,
        "owner_user_id": project.user_id,
        "target_revision_id": target_revision_id,
        "code": str(finding["code"]),
        "action": action,
        "basis": basis,
        **_actor_values(actor),
        "payload_json": {"contract": _DISPOSITION_CONTRACT, "reason": reason_text},
        "created_at": utc_now_db(),
    }
    if not _insert(_seal(_DISPOSITION_MODEL, values, _DISPOSITION_RECEIPT)):
        existing = replay(history())
        if existing is None:
            raise _error("qa-disposition-version-conflict")
        return existing
    return _disposition_projection(history())


__all__ = [
    "DISPOSITION_BASES",
    "FLAG_ACTIONS",
    "FLAG_ORIGINS",
    "FLAG_STATES",
    "create_flag",
    "edit_reasons",
    "list_flags",
    "normalize_edit_reason",
    "open_flags_by_block",
    "persist_edit_reason",
    "qa_dispositions",
    "record_qa_disposition",
    "transition_flag",
]
