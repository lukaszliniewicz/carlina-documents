"""Strict, principal-safe projections for the scholarly review HTTP API.

The scholarly review service deliberately returns persistence-oriented
projections because its callers include internal workers.  This module is the
small HTTP wire boundary: it copies only fields which are useful to a review
client and never forwards durable identifiers, integrity material, or
capability secrets by accident.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from .block_notes import render_note_body

_NOTE_SENTINELS = ("dossier-note-v1:", "legacy-target-note-v1:")
_NOTE_ANCHORS = {"block": "whole_block", "whole_block": "whole_block", "source": "source", "target": "target"}
_SUGGESTION_STATES = {
    "proposed": "pending",
    "accepted": "approved",
    "rejected": "rejected",
}
_AUTHOR_LABELS = {
    "owner": "Owner",
    "reviewer": "Reviewer",
    "viewer": "Viewer",
    "model": "Model",
    "system": "System",
    "external": "External",
}


def _value(row: Any, *keys: str, default: Any = None) -> Any:
    if isinstance(row, Mapping):
        for key in keys:
            if key in row and row[key] is not None:
                return row[key]
        return default
    for key in keys:
        try:
            value = getattr(row, key)
        except Exception:  # noqa: BLE001, S112 - projections tolerate ORM fakes.
            continue
        if value is not None:
            return value
    return default


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _safe_text(value: Any, *, default: str = "", maximum: int = 128) -> str:
    if not isinstance(value, str):
        return default
    if "\x00" in value:
        return default
    return value[:maximum]


_FORBIDDEN_PARTS = (
    "hash",
    "fingerprint",
    "receipt",
    "secret",
    "witness",
    "owner_user_id",
    "author_user_id",
    "user_id",
    "owner_id",
    "proposer_user_id",
    "requester_user_id",
    "created_by_user_id",
    "resolved_by_user_id",
    "grant_id",
    "share_grant_id",
    "operation_id",
)


def _public_value(value: Any) -> Any:
    """Recursively remove persistence/integrity fields from free-form data."""

    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            normalized = key_text.lower()
            if any(part in normalized for part in _FORBIDDEN_PARTS):
                continue
            result[key_text] = _public_value(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_public_value(item) for item in value]
    return value


def _author(row: Any, *, type_keys: tuple[str, ...], label_keys: tuple[str, ...] = ()) -> dict[str, str]:
    supplied = _value(row, "author", "actor", default=None)
    if isinstance(supplied, Mapping):
        kind = _safe_text(supplied.get("type"), maximum=32)
        label = _safe_text(supplied.get("label"), maximum=128)
    else:
        kind = _safe_text(_value(row, *type_keys), maximum=32)
        label = _safe_text(_value(row, *label_keys), maximum=128) if label_keys else ""
    if not kind:
        kind = "unknown"
    if not label:
        label = _AUTHOR_LABELS.get(kind, kind.replace("_", " ").title())
    return {"type": kind, "label": label}


def access(actor: Any, label: str) -> dict[str, str]:
    """Project an authenticated actor without any user or grant identifier."""

    role = _safe_text(_value(actor, "role", default=actor if isinstance(actor, str) else ""), maximum=32)
    if role not in {"owner", "reviewer", "viewer"}:
        role = "viewer"
    return {"role": role, "label": _safe_text(label, maximum=128)}


def reader_note(row: Any) -> dict[str, Any]:
    """Project a reader note with only public review content and anchor data."""

    body = _safe_text(_value(row, "body", default=""), maximum=4_000)
    citations = _value(row, "citation_ids", "citations", default=[])
    if not isinstance(citations, (list, tuple)):
        citations = []
    citation_ids = [
        _safe_text(item, maximum=128)
        for item in citations
        if isinstance(item, str)
        and not any(item.startswith(prefix) for prefix in _NOTE_SENTINELS)
    ]
    kind = _NOTE_ANCHORS.get(
        _safe_text(_value(row, "anchor_kind", "kind", default="block"), maximum=32),
        "whole_block",
    )
    occurrence = _value(row, "occurrence_index", default=None)
    if isinstance(occurrence, bool) or not isinstance(occurrence, int):
        occurrence = None
    version = _value(row, "version", default=0)
    if isinstance(version, bool) or not isinstance(version, int) or version < 0:
        version = 0
    anchor_text = _value(row, "anchor_text", "text", default=None)
    if not isinstance(anchor_text, str):
        anchor_text = None
    else:
        anchor_text = _safe_text(anchor_text, maximum=160)
    state = _safe_text(_value(row, "state", "status", default="active"), maximum=32)
    if state not in {"proposed", "active", "needs_review", "retired"}:
        state = "active"
    return {
        "id": _safe_text(_value(row, "id", "note_id", default=""), maximum=256),
        "version": version,
        "state": state,
        "body": body,
        "body_html": render_note_body(body),
        "citation_ids": citation_ids,
        "anchor": {
            "kind": kind,
            "text": anchor_text,
            "occurrence_index": occurrence,
        },
        "author": _author(row, type_keys=("author_type", "actor_role"), label_keys=("author_label",)),
    }


def block_review(row: Any | None) -> dict[str, Any]:
    """Project the current block review, including a stable empty state."""

    version_hint = _value(row, "version", default=0) if row is not None else 0
    if row is None or (
        _value(row, "decision", default=None) is None
        and (version_hint is None or version_hint == 0)
    ):
        return {
            "decision": "pending",
            "version": 0,
            "rationale": None,
            "updated_at": None,
            "actor": None,
        }
    decision = _safe_text(_value(row, "decision", default="pending"), maximum=32)
    version = _value(row, "version", default=0)
    if isinstance(version, bool) or not isinstance(version, int):
        version = 0
    if decision not in {"pending", "approved", "needs_changes"}:
        decision = "pending"
    rationale = _value(row, "rationale", "comment", default=None)
    if not isinstance(rationale, str) and rationale is not None:
        rationale = None
    elif isinstance(rationale, str):
        rationale = _safe_text(rationale, maximum=4_000)
    return {
        "decision": decision or "pending",
        "effective_decision": _value(row, "effective_decision", default=decision or "pending"),
        "stale": bool(_value(row, "stale", default=False)),
        "target_revision_id": _value(row, "target_revision_id", default=None),
        "current_target_revision_id": _value(row, "current_target_revision_id", default=None),
        "current_target_version": _value(row, "current_target_version", default=None),
        "version": max(0, version),
        "rationale": rationale,
        "updated_at": _iso(_value(row, "updated_at", "created_at", default=None)),
        "actor": _author(row, type_keys=("author_type", "actor_role"), label_keys=("author_label",)),
    }


def glossary_suggestion(row: Any) -> dict[str, Any]:
    """Project a glossary proposal and map its internal lifecycle to wire state."""

    state = _SUGGESTION_STATES.get(
        _safe_text(_value(row, "state", default="proposed"), maximum=32),
        "pending",
    )
    version = _value(row, "version", default=1)
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        version = 1
    proposer = _value(row, "proposer_kind", default=None)
    return {
        "id": _safe_text(_value(row, "id", default=""), maximum=256),
        "source_term": _value(row, "source_term", default=""),
        "target_term": _value(row, "target_term", default=""),
        "notes": _value(row, "notes", default=None),
        "state": state,
        "version": version,
        "created_at": _iso(_value(row, "created_at", default=None)),
        "author": _author(
            row,
            type_keys=("author_type", "proposer_kind"),
            label_keys=("author_label",),
        )
        if proposer is not None or _value(row, "author", default=None) is not None
        else {"type": "unknown", "label": "Unknown"},
    }


def grant(row: Any) -> dict[str, Any]:
    """Project a grant without its digest, owner, or token hint."""

    status = "revoked" if _value(row, "revoked_at", default=None) is not None else "active"
    role = _safe_text(_value(row, "role", default="viewer"), maximum=32)
    if role not in {"viewer", "reviewer"}:
        role = "viewer"
    return {
        "id": _safe_text(_value(row, "id", default=""), maximum=256),
        "label": _safe_text(_value(row, "label", default=""), maximum=128),
        "role": role,
        "status": status,
        "expires_at": _iso(_value(row, "expires_at", default=None)),
        "last_used_at": _iso(_value(row, "last_used_at", default=None)),
        "created_at": _iso(_value(row, "created_at", default=None)),
    }


def dossier(
    row: Any,
    *,
    include_payload: bool = False,
    include_proposals: bool = False,
) -> dict[str, Any]:
    """Project a dossier while retaining only selection/fencing identifiers."""

    version = _value(row, "version", default=0)
    if isinstance(version, bool) or not isinstance(version, int) or version < 0:
        version = 0
    result: dict[str, Any] = {
        "id": _safe_text(_value(row, "id", "dossier_id", default=""), maximum=256),
        "revision_id": _value(row, "revision_id", default=None),
        "version": version,
        "scope": _public_value(_value(row, "scope", default={})),
        "provenance": _public_value(_value(row, "provenance", default={})),
        "conclusion_provenance": _safe_text(
            _value(row, "conclusion_provenance", default="external_unverified"), maximum=64
        ),
        "evidence_verification_state": _safe_text(
            _value(row, "evidence_verification_state", default="unverified"), maximum=32
        ),
        "submitted_at": _iso(_value(row, "submitted_at", "created_at", default=None)),
    }
    if include_payload and isinstance(_value(row, "payload", default=None), Mapping):
        payload = _value(row, "payload")
        if not include_proposals:
            payload = {
                key: value
                for key, value in payload.items()
                if key not in {"annotation_proposals", "terminology"}
            }
        result["payload"] = _public_value(payload)
    return result


def dossier_summary(row: Any) -> dict[str, Any]:
    """Return the summary form used for list and viewer responses."""

    return dossier(row, include_payload=False)


def retranslation_request(row: Any) -> dict[str, Any]:
    """Project a request without operation identity, actor IDs, or integrity fields."""

    version = _value(row, "version", default=1)
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        version = 1
    state = _safe_text(_value(row, "state", default="requested"), maximum=32)
    if state not in {"requested", "started", "rejected", "cancelled", "completed"}:
        state = "requested"
    return {
        "id": _safe_text(_value(row, "id", default=""), maximum=256),
        "scope": _public_value(_value(row, "scope", default={})),
        "resolved_block_ids": _public_value(
            _value(row, "resolved_block_ids", default=[])
        ),
        "custom_prompt": _value(row, "custom_prompt", default=""),
        "annotation_profile": _value(row, "annotation_profile", default=None),
        "research_choice": _value(row, "research_choice", default=None),
        "dossier_revision_id": _value(row, "dossier_revision_id", default=None),
        "rationale": _value(row, "rationale", default=""),
        "state": state,
        "version": version,
        "created_at": _iso(_value(row, "created_at", default=None)),
        "updated_at": _iso(_value(row, "updated_at", default=None)),
    }


# Descriptive aliases make the projection names easy to discover for adapters
# without creating a second implementation or accepting arbitrary dictionaries.
project_access = access
project_reader_note = reader_note
project_block_review = block_review
project_glossary_suggestion = glossary_suggestion
project_grant = grant
project_dossier = dossier
project_retranslation_request = retranslation_request
project_dossier_summary = dossier_summary
access_projection = access
reader_note_projection = reader_note
block_review_projection = block_review
glossary_suggestion_projection = glossary_suggestion
grant_projection = grant
dossier_projection = dossier
retranslation_request_projection = retranslation_request


__all__ = [
    "access",
    "access_projection",
    "block_review",
    "block_review_projection",
    "dossier",
    "dossier_projection",
    "dossier_summary",
    "glossary_suggestion",
    "glossary_suggestion_projection",
    "grant",
    "grant_projection",
    "project_access",
    "project_block_review",
    "project_dossier",
    "project_dossier_summary",
    "project_glossary_suggestion",
    "project_grant",
    "project_reader_note",
    "project_retranslation_request",
    "reader_note",
    "reader_note_projection",
    "retranslation_request",
    "retranslation_request_projection",
]
