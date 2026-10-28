"""Persistence contracts for scholarly review of block translations.

This module is deliberately a service boundary rather than a route adapter.  It
validates all user/model supplied material before writing, stores source-bearing
material in encrypted model columns, and only flushes the caller's transaction.
The new review models are kept behind small mapper helpers so their migration
can evolve independently from this non-UI contract.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import importlib
import re
import secrets
import unicodedata
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from urllib.parse import urlsplit

from sqlalchemy.exc import OperationalError

from carlina.core.utc import utc_now_db

from ...core.encryption import keyed_lookup_candidates, keyed_lookup_digest
from ...extensions import db
from .block_contract import BlockContractError, TranslationSourcePlan, canonical_json
from .block_workspace_api import load_block_source_plan
from .glossary_persistence import (
    GlossaryPayload,
    activate_glossary_revision,
    approve_glossary_proposal,
    create_glossary_concept,
    find_glossary_revisions_for_term,
    glossary_lock_preview,
    persist_analysis_glossary_proposals,
    persist_glossary_revision,
)
from .integrity_receipts import make_receipt, verify_receipt
from .target_contract import literal_reference_slots, target_text
from .target_persistence import rehydrate_target_block_notes, rehydrate_target_revision

_MODEL_MODULE = f"{__package__}.models"
_MAX_JSON_BYTES = 2 * 1024 * 1024
_MAX_SCOPE_BLOCKS = 512
_MAX_CLIENT_ID = 128
_MAX_CITATIONS = 32
_MAX_CITATION_ID = 128
_MAX_ANCHOR_CHARS = 160
_MAX_BODY_CHARS = 4000
_MAX_GRANT_LABEL = 128
_MAX_SESSION_MINUTES = 30
_MAX_SESSION_DAYS = 1
_GRANT_PREFIX = "trg_"
_SESSION_PREFIX = "trs_"
_GRANT_SECRET_PURPOSE = "translator-review-grant-secret-v1"
_SESSION_SECRET_PURPOSE = "translator-review-session-secret-v1"
_CSRF_SECRET_PURPOSE = "translator-review-session-csrf-v1"
_DOSSIER_REVISION_RECEIPT_PURPOSE = "translator-research-dossier-revision-receipt-v1"
_NOTE_RECEIPT_PURPOSE = "translator-reader-note-revision-receipt-v1"
_REVIEW_RECEIPT_PURPOSE = "translator-block-review-revision-receipt-v1"
_REQUEST_RECEIPT_PURPOSE = "translator-retranslation-request-receipt-v1"
_NOTE_DOSSIER_SENTINEL = "dossier-note-v1:"
_NOTE_LEGACY_SENTINEL = "legacy-target-note-v1:"
_NOTE_INTERNAL_SENTINELS = (_NOTE_DOSSIER_SENTINEL, _NOTE_LEGACY_SENTINEL)
_DOSSIER_REVISION_RECEIPT_FIELDS = (
    "id",
    "dossier_id",
    "project_id",
    "source_plan_id",
    "revision_number",
    "parent_revision_id",
    "client_submission_id",
    "content_digest",
    "conclusion_provenance",
    "evidence_verification_state",
    "payload_json",
    "provenance_json",
    "created_at",
)
_NOTE_REVISION_RECEIPT_FIELDS = (
    "id",
    "note_id",
    "project_id",
    "source_plan_id",
    "block_id",
    "revision_number",
    "parent_revision_id",
    "state",
    "anchor_kind",
    "target_revision_id",
    "anchor_text",
    "occurrence_index",
    "witness_before",
    "witness_after",
    "body",
    "citations_json",
    "author_type",
    "author_user_id",
    "share_grant_id",
    "created_at",
)
_REVIEW_REVISION_RECEIPT_FIELDS = (
    "id",
    "project_id",
    "source_plan_id",
    "block_id",
    "revision_number",
    "parent_revision_id",
    "target_revision_id",
    "decision",
    "comment",
    "author_type",
    "author_user_id",
    "share_grant_id",
    "created_at",
)


class ScholarlyReviewError(ValueError):
    """Safe, stable failure at the scholarly-review persistence boundary."""

    def __init__(self, code: str, message: str | None = None, *, latest: Any = None):
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,95}", code):
            code = "scholarly-review-invalid"
        self.code = code
        self.latest = latest
        super().__init__(message or code.replace("-", " "))


@dataclass(frozen=True)
class ReviewActor:
    """Explicit review authority; no Flask/current-user lookup is performed."""

    role: str
    user_id: int | str | None = None
    share_grant_id: str | None = None

    def __post_init__(self) -> None:
        if self.role not in {"owner", "reviewer", "viewer"}:
            raise ScholarlyReviewError("review-actor-role")
        if self.role == "owner" and self.user_id is None:
            raise ScholarlyReviewError("review-actor-owner")
        if self.role in {"reviewer", "viewer"} and not self.share_grant_id:
            raise ScholarlyReviewError("review-actor-grant")


def actor_is_owner(project: Any, actor: ReviewActor) -> bool:
    return (
        actor.role == "owner"
        and actor.user_id is not None
        and str(actor.user_id) == str(project.user_id)
    )


def _actor_grant_is_active(project: Any, actor: ReviewActor) -> bool:
    if actor.role not in {"reviewer", "viewer"} or not actor.share_grant_id:
        return False
    try:
        row = db.session.get(_model("TranslationShareGrant"), actor.share_grant_id)
    except ScholarlyReviewError:
        return False
    return bool(
        row is not None
        and _get(row, "project_id") == project.id
        and _get(row, "role") == actor.role
        and _get(row, "status", default="active") == "active"
        and _get(row, "revoked_at") is None
        and _expires_after_now(_get(row, "expires_at"))
    )


def actor_can_mutate(project: Any, actor: ReviewActor) -> bool:
    return actor_is_owner(project, actor) or (
        actor.role == "reviewer" and _actor_grant_is_active(project, actor)
    )


def actor_can_read(
    actor: ReviewActor, *, include_proposed: bool = False, project: Any | None = None
) -> bool:
    if include_proposed and actor.role == "viewer":
        return False
    if actor.role == "owner":
        return project is None or actor_is_owner(project, actor)
    return project is None or _actor_grant_is_active(project, actor)


def actor_can_execute(project: Any, actor: ReviewActor) -> bool:
    return actor_is_owner(project, actor)


def _require_owner(project: Any, actor: ReviewActor) -> None:
    if not actor_is_owner(project, actor):
        raise ScholarlyReviewError("review-owner-required")


def _require_mutator(project: Any, actor: ReviewActor) -> None:
    if not actor_can_mutate(project, actor):
        raise ScholarlyReviewError("review-mutation-forbidden")


def _require_reader(actor: ReviewActor, project: Any | None = None) -> None:
    if not actor_can_read(actor, project=project):
        raise ScholarlyReviewError("review-read-forbidden")


def _models() -> Any:
    return importlib.import_module(_MODEL_MODULE)


def _model(name: str, *, optional: bool = False) -> Any:
    value = getattr(_models(), name, None)
    if value is None and not optional:
        raise ScholarlyReviewError("review-model-unavailable")
    return value


def _mapper_keys(model: Any) -> set[str]:
    try:
        return set(model.__mapper__.attrs.keys())
    except Exception:  # noqa: BLE001 - compatibility mapper probing.
        try:
            return {column.key for column in model.__table__.columns}
        except Exception:  # noqa: BLE001 - optional/fake models have no mapper.
            return set()


_ALIASES: dict[str, tuple[str, ...]] = {
    "record_hash": ("record_hash", "revision_record_hash"),
    "integrity_receipt": ("integrity_receipt", "receipt", "record_receipt"),
    "payload_json": ("payload_json", "payload", "evidence_json"),
    "provenance_json": ("provenance_json", "provenance"),
    "scope_json": ("scope_json", "scope"),
    "source_plan_id": ("source_plan_id", "plan_id"),
    "owner_user_id": ("owner_user_id", "user_id"),
    "author_user_id": ("author_user_id", "user_id"),
    "actor_user_id": ("actor_user_id", "user_id"),
    "grant_id": ("grant_id", "share_grant_id"),
    "secret_digest": ("secret_digest", "secret_hash", "lookup_digest"),
    "token_digest": ("token_digest", "secret_digest", "secret_hash", "lookup_digest"),
    "session_digest": ("session_digest", "secret_digest", "secret_hash"),
    "csrf_digest": ("csrf_digest", "csrf_hash"),
    "target_version": ("target_version", "target_active_version"),
    "revision_id": ("revision_id", "head_revision_id"),
    "version": ("version", "head_version"),
}


def _actual_key(model: Any, key: str) -> str | None:
    available = _mapper_keys(model)
    for candidate in (key, *_ALIASES.get(key, ())):
        if candidate in available:
            return candidate
    return None


def _set_fields(obj: Any, values: Mapping[str, Any]) -> None:
    model = type(obj)
    for key, value in values.items():
        actual = _actual_key(model, key)
        if actual is not None:
            setattr(obj, actual, value)


def _new(model: Any, values: Mapping[str, Any]) -> Any:
    obj = model()
    _set_fields(obj, values)
    return obj


def _set_integrity_fields(
    model: Any,
    values: dict[str, Any],
    *,
    hash_values: Mapping[str, Any],
    receipt_purpose: str,
    receipt_values: Mapping[str, Any],
) -> None:
    """Attach only integrity fields actually mapped by the target model."""

    if _actual_key(model, "record_hash") is not None:
        values["record_hash"] = _sha(hash_values)
    receipt_key = _actual_key(model, "integrity_receipt")
    if receipt_key is not None:
        receipt_material = _receipt_json_value(
            {"receipt_version": 1, **dict(receipt_values)}
        )
        if "record_hash" in values:
            receipt_material["record_hash"] = values["record_hash"]
        values[receipt_key] = make_receipt(
            purpose=receipt_purpose,
            values=receipt_material,
        )


def _receipt_json_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _receipt_json_value(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_receipt_json_value(item) for item in value]
    return value


def _verify_revision_receipt(
    row: Any,
    *,
    purpose: str,
    fields: Sequence[str],
    code: str,
    extra: Mapping[str, Any] | None = None,
) -> None:
    """Authenticate a whole decrypted immutable revision before projection."""

    if _actual_key(type(row), "integrity_receipt") is None:
        return
    material = _receipt_json_value(
        {
            "receipt_version": 1,
            **{
                field: row.get(field)
                if isinstance(row, Mapping)
                else _get(row, field)
                for field in fields
            },
            **dict(extra or {}),
            "record_hash": _get(row, "record_hash"),
        }
    )
    try:
        verify_receipt(
            stored=_get(row, "integrity_receipt"),
            purpose=purpose,
            values=material,
            code=code,
        )
    except BlockContractError as exc:
        raise ScholarlyReviewError(code) from exc


def _get(obj: Any, *keys: str, default: Any = None) -> Any:
    for key in keys:
        try:
            value = getattr(obj, key)
        except Exception:  # noqa: BLE001, S112 - alias probing tolerates ORM fakes.
            continue
        if value is not None:
            return value
    return default


def _query(model: Any, filters: Mapping[str, Any] | None = None) -> Any:
    query = model.query
    for key, value in (filters or {}).items():
        actual = _actual_key(model, key)
        if actual is not None:
            query = query.filter(getattr(model, actual) == value)
    return query


def _query_all(model: Any, filters: Mapping[str, Any] = ()) -> list[Any]:
    return list(_query(model, filters).all())


def _flush() -> None:
    db.session.flush()


def _safe_text(
    value: Any, code: str, maximum: int, *, nullable: bool = False
) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or len(value) > maximum or "\x00" in value:
        raise ScholarlyReviewError(code)
    return value


def _safe_id(value: Any, code: str = "review-id") -> str:
    if not isinstance(value, str) or not value or len(value) > 256 or "\x00" in value:
        raise ScholarlyReviewError(code)
    return value


def _canonical_bytes(value: Any, code: str = "review-json") -> bytes:
    try:
        result = canonical_json(_json_compatible(value)).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as exc:
        raise ScholarlyReviewError(code) from exc
    if len(result) > _MAX_JSON_BYTES:
        raise ScholarlyReviewError("dossier-payload-too-large")
    return result


def _json_compatible(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_compatible(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_compatible(item) for item in value]
    return value


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _db_datetime(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is not None:
        return value.astimezone(UTC).replace(tzinfo=None)
    return value


def _expires_after_now(value: Any) -> bool:
    if value is None:
        return True
    try:
        return bool(value > utc_now_db())
    except TypeError:
        return bool(
            isinstance(value, datetime)
            and value.tzinfo is not None
            and value > datetime.now(UTC)
        )


def normalize_annotation_profile(value: Any, legacy_enabled: bool | None = None) -> str:
    if value is None and legacy_enabled is not None:
        value = legacy_enabled
    if value is False:
        return "none"
    if value is True:
        return "restrained"
    if value not in {"none", "restrained", "study"}:
        raise ScholarlyReviewError("annotation-profile")
    return cast(str, value)


def annotation_profile_notes_enabled(
    value: Any, legacy_enabled: bool | None = None
) -> bool:
    return normalize_annotation_profile(value, legacy_enabled) != "none"


def _workspace(project: Any) -> tuple[Any, Any, list[Any]]:
    try:
        workspace = load_block_source_plan(project)
    except Exception as exc:
        code = getattr(exc, "code", "source-plan-invalid")
        raise ScholarlyReviewError(code) from exc
    blocks = list(workspace.plan.blocks)
    return workspace, workspace.record, blocks


def _block_map(blocks: Sequence[Any]) -> dict[str, Any]:
    return {str(block.id): block for block in blocks}


def _context_blocks(value: Any) -> list[Any]:
    """Normalize an activation DTO block list or block-ID mapping."""

    if isinstance(value, Mapping):
        return list(value.values())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return list(value)
    raise ScholarlyReviewError("source-plan-context")


def _normalize_block_ids(
    value: Any, blocks: Sequence[Any], *, code: str = "scope-blocks"
) -> list[str]:
    if not isinstance(value, (list, tuple)) or len(value) > _MAX_SCOPE_BLOCKS:
        raise ScholarlyReviewError(code)
    known = _block_map(blocks)
    supplied: set[str] = set()
    for block_id in value:
        block_id = _safe_id(block_id, code)
        if block_id not in known or block_id in supplied:
            raise ScholarlyReviewError(code)
        supplied.add(block_id)
    return [str(block.id) for block in blocks if str(block.id) in supplied]


def _normalize_scope(
    scope: Any, blocks: Sequence[Any], *, allow_range: bool = False
) -> tuple[dict[str, Any], list[str]]:
    if not isinstance(scope, Mapping):
        raise ScholarlyReviewError("scope-shape")
    if set(scope) == {"kind", "block_ids"} and scope.get("kind") in {
        "document",
        "blocks",
    }:
        ids = _normalize_block_ids(scope.get("block_ids"), blocks)
        if scope["kind"] == "document" and ids:
            raise ScholarlyReviewError("scope-shape")
        if scope["kind"] == "blocks" and not ids:
            raise ScholarlyReviewError("scope-empty")
        return {"kind": scope["kind"], "block_ids": ids}, ids
    if (
        allow_range
        and set(scope)
        in ({"kind", "start_order", "end_order"}, {"kind", "start", "end"})
        and scope.get("kind") in {"range", "order_range"}
    ):
        start = scope.get("start_order", scope.get("start"))
        end = scope.get("end_order", scope.get("end"))
        if (
            isinstance(start, bool)
            or not isinstance(start, int)
            or isinstance(end, bool)
            or not isinstance(end, int)
            or start < 1
            or end < start
        ):
            raise ScholarlyReviewError("scope-range")
        if end > len(blocks) or end - start + 1 > _MAX_SCOPE_BLOCKS:
            raise ScholarlyReviewError("scope-range")
        ids = [str(block.id) for block in blocks[start - 1 : end]]
        return {"kind": "range", "start_order": start, "end_order": end}, ids
    raise ScholarlyReviewError("scope-shape")


def _source_projection(block: Any) -> str:
    value = _get(block, "projection", "source_projection", default="")
    if not isinstance(value, str):
        raise ScholarlyReviewError("source-projection")
    return value


def _literal_occurrences(source: str, needle: str) -> list[tuple[int, int]]:
    source_n = unicodedata.normalize("NFKC", source)
    needle_n = unicodedata.normalize("NFKC", needle)
    if not needle_n:
        return []
    positions: list[tuple[int, int]] = []
    start = 0
    while True:
        found = source_n.find(needle_n, start)
        if found < 0:
            return positions
        positions.append((found, found + len(needle_n)))
        start = found + 1


def _anchor_witnesses(source: str, start: int, end: int) -> tuple[str, str]:
    normalized = unicodedata.normalize("NFKC", source)
    before = normalized[max(0, start - 40) : start][-40:]
    after = normalized[end : min(len(normalized), end + 40)][:40]
    return before, after


def _validate_anchor(
    anchor: Mapping[str, Any] | None,
    block: Any,
    *,
    target: str | None = None,
    target_revision_id: str | None = None,
    target_version: int | None = None,
) -> dict[str, Any]:
    value = dict(anchor or {})
    kind = value.get("kind", value.get("anchor_kind", "block"))
    allowed = {
        "kind",
        "anchor_kind",
        "anchor_text",
        "occurrence_index",
        "target_revision_id",
        "target_version",
    }
    if (
        set(value) - allowed
        or kind not in {"block", "source", "target"}
        or (
            "kind" in value
            and "anchor_kind" in value
            and value["kind"] != value["anchor_kind"]
        )
    ):
        raise ScholarlyReviewError("note-anchor-shape")
    if kind == "block":
        if (
            any(
                value.get(key) not in (None, "")
                for key in (
                    "anchor_text",
                    "target_revision_id",
                    "target_version",
                )
            )
            or value.get("occurrence_index") is not None
        ):
            raise ScholarlyReviewError("note-anchor-shape")
        return {
            "kind": "block",
            "anchor_text": None,
            "occurrence_index": None,
            "witness": None,
            "witness_before": None,
            "witness_after": None,
            "target_revision_id": None,
            "target_version": None,
        }
    text = _safe_text(value.get("anchor_text"), "note-anchor-text", _MAX_ANCHOR_CHARS)
    if not text or not text.strip():
        raise ScholarlyReviewError("note-anchor-text")
    occurrence = value.get("occurrence_index")
    if (
        isinstance(occurrence, bool)
        or not isinstance(occurrence, int)
        or occurrence < 0
    ):
        raise ScholarlyReviewError("note-anchor-occurrence")
    source_text = _source_projection(block) if kind == "source" else (target or "")
    matches = _literal_occurrences(source_text, text)
    if occurrence >= len(matches):
        raise ScholarlyReviewError("note-anchor-missing")
    revision = (
        value.get("target_revision_id", target_revision_id)
        if kind == "target"
        else None
    )
    version = value.get("target_version", target_version) if kind == "target" else None
    if kind == "target" and (
        (
            target_revision_id is not None
            and value.get("target_revision_id") not in (None, target_revision_id)
        )
        or (
            target_version is not None
            and value.get("target_version") not in (None, target_version)
        )
        or not revision
        or isinstance(version, bool)
        or not isinstance(version, int)
        or version < 1
    ):
        raise ScholarlyReviewError("note-target-fence")
    start, end = matches[occurrence]
    witness_before, witness_after = _anchor_witnesses(source_text, start, end)
    return {
        "kind": kind,
        "anchor_text": text,
        "occurrence_index": occurrence,
        # Keep the combined value for callers written against the initial
        # service draft; persistence uses the mapped before/after columns.
        "witness": witness_before + witness_after,
        "witness_before": witness_before,
        "witness_after": witness_after,
        "target_revision_id": revision,
        "target_version": version,
    }


def _validate_citations(
    citations: Any, *, allow_internal: bool = False
) -> list[str]:
    if citations is None:
        return []
    if not isinstance(citations, (list, tuple)) or len(citations) > _MAX_CITATIONS:
        raise ScholarlyReviewError("note-citations")
    result: list[str] = []
    for citation in citations:
        citation = _safe_text(citation, "note-citation", _MAX_CITATION_ID)
        if (
            not citation
            or not citation.strip()
            or citation in result
            or (
                not allow_internal
                and citation.startswith(_NOTE_INTERNAL_SENTINELS)
            )
        ):
            raise ScholarlyReviewError("note-citations")
        result.append(citation)
    return result


def _write_idempotency_key(value: Any, code: str) -> str | None:
    """Normalize an optional external write key without weakening internal calls."""

    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= _MAX_CLIENT_ID
        or value != value.strip()
    ):
        raise ScholarlyReviewError(code)
    return value


def _idempotent_write_id(kind: str, *scope: object, key: str) -> str:
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            ":".join(
                ["carlina", "translator", kind, *(str(value) for value in scope), key]
            ),
        )
    )


def _same_optional_identity(left: Any, right: Any) -> bool:
    if left is None or right is None:
        return left is None and right is None
    return str(left) == str(right)


def _normalize_dossier_payload(
    payload: Any, blocks: Sequence[Any], scope_ids: set[str]
) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise ScholarlyReviewError("dossier-payload-shape")
    keys = {
        "document_brief",
        "style_guidance",
        "entities",
        "terminology",
        "references",
        "allusions",
        "unresolved_questions",
        "annotation_proposals",
    }
    if set(payload) - keys:
        raise ScholarlyReviewError("dossier-payload-keys")
    block_map = _block_map(blocks)

    def ids(value: Any) -> list[str]:
        normalized = _normalize_block_ids(value, blocks, code="dossier-source-blocks")
        if not set(normalized).issubset(scope_ids):
            raise ScholarlyReviewError("dossier-scope-block")
        return normalized

    brief = _safe_text(payload.get("document_brief", ""), "dossier-brief", 50_000)
    style = payload.get("style_guidance", [])
    if not isinstance(style, list) or len(style) > 256:
        raise ScholarlyReviewError("dossier-style")
    style_out = [_safe_text(item, "dossier-style", 2_000) for item in style]
    entities: list[dict[str, Any]] = []
    raw_entities = payload.get("entities", [])
    if not isinstance(raw_entities, list) or len(raw_entities) > 512:
        raise ScholarlyReviewError("dossier-entities")
    for item in raw_entities:
        if not isinstance(item, Mapping) or set(item) != {
            "name",
            "kind",
            "description",
            "source_block_ids",
        }:
            raise ScholarlyReviewError("dossier-entity-shape")
        entities.append(
            {
                "name": _safe_text(item["name"], "dossier-entity", 1_000),
                "kind": _safe_text(item["kind"], "dossier-entity", 1_000),
                "description": _safe_text(item["description"], "dossier-entity", 8_000),
                "source_block_ids": ids(item["source_block_ids"]),
            }
        )
    terminology: list[dict[str, Any]] = []
    raw_terms = payload.get("terminology", [])
    if not isinstance(raw_terms, list) or len(raw_terms) > 2_000:
        raise ScholarlyReviewError("dossier-terminology")
    for item in raw_terms:
        if not isinstance(item, Mapping) or set(item) != {
            "source_term",
            "target_term",
            "notes",
            "source_block_ids",
            "confidence",
        }:
            raise ScholarlyReviewError("dossier-term-shape")
        confidence = item["confidence"]
        if confidence is not None and (
            isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not 0 <= confidence <= 1
        ):
            raise ScholarlyReviewError("dossier-confidence")
        terminology.append(
            {
                "source_term": _safe_text(item["source_term"], "dossier-term", 4_096),
                "target_term": _safe_text(
                    item["target_term"], "dossier-term", 4_096, nullable=True
                ),
                "notes": _safe_text(
                    item["notes"], "dossier-term-notes", 32_768, nullable=True
                ),
                "source_block_ids": ids(item["source_block_ids"]),
                "confidence": confidence,
            }
        )
        if not terminology[-1]["source_term"].strip() or (
            terminology[-1]["target_term"] is not None
            and not terminology[-1]["target_term"].strip()
        ):
            raise ScholarlyReviewError("dossier-term")
    references: list[dict[str, Any]] = []
    raw_refs = payload.get("references", [])
    if not isinstance(raw_refs, list) or len(raw_refs) > 1_000:
        raise ScholarlyReviewError("dossier-references")
    ref_ids: set[str] = set()
    for item in raw_refs:
        if not isinstance(item, Mapping) or set(item) != {
            "reference_id",
            "title",
            "authors",
            "url",
            "locator",
            "note",
        }:
            raise ScholarlyReviewError("dossier-reference-shape")
        rid = _safe_text(item["reference_id"], "dossier-reference-id", 128)
        if not rid or rid in ref_ids:
            raise ScholarlyReviewError("dossier-reference-id")
        ref_ids.add(rid)
        url = _reference_url(item["url"])
        references.append(
            {
                "reference_id": rid,
                "title": _safe_text(item["title"], "dossier-reference", 4_096),
                "authors": _safe_text(item["authors"], "dossier-reference", 4_096),
                "url": url,
                "locator": _safe_text(
                    item["locator"], "dossier-reference", 4_096, nullable=True
                ),
                "note": _safe_text(
                    item["note"], "dossier-reference", 32_768, nullable=True
                ),
            }
        )
    allusions: list[dict[str, Any]] = []
    raw_allusions = payload.get("allusions", [])
    if not isinstance(raw_allusions, list) or len(raw_allusions) > 1_000:
        raise ScholarlyReviewError("dossier-allusions")
    for item in raw_allusions:
        if not isinstance(item, Mapping) or set(item) != {
            "label",
            "explanation",
            "source_block_ids",
            "reference_ids",
        }:
            raise ScholarlyReviewError("dossier-allusion-shape")
        rids = item["reference_ids"]
        if not isinstance(rids, list) or any(
            not isinstance(rid, str) or rid not in ref_ids for rid in rids
        ):
            raise ScholarlyReviewError("dossier-reference-id")
        allusions.append(
            {
                "label": _safe_text(item["label"], "dossier-allusion", 4_096),
                "explanation": _safe_text(
                    item["explanation"], "dossier-allusion", 32_768
                ),
                "source_block_ids": ids(item["source_block_ids"]),
                "reference_ids": list(dict.fromkeys(rids)),
            }
        )
    questions: list[dict[str, Any]] = []
    raw_questions = payload.get("unresolved_questions", [])
    if not isinstance(raw_questions, list) or len(raw_questions) > 1_000:
        raise ScholarlyReviewError("dossier-questions")
    for item in raw_questions:
        if not isinstance(item, Mapping) or set(item) != {
            "question",
            "source_block_ids",
        }:
            raise ScholarlyReviewError("dossier-question-shape")
        questions.append(
            {
                "question": _safe_text(item["question"], "dossier-question", 8_000),
                "source_block_ids": ids(item["source_block_ids"]),
            }
        )
    annotations: list[dict[str, Any]] = []
    raw_annotations = payload.get("annotation_proposals", [])
    if not isinstance(raw_annotations, list) or len(raw_annotations) > 2_000:
        raise ScholarlyReviewError("dossier-annotations")
    for item in raw_annotations:
        if not isinstance(item, Mapping) or set(item) != {
            "block_id",
            "anchor_kind",
            "anchor_text",
            "occurrence_index",
            "body",
            "reference_ids",
        }:
            raise ScholarlyReviewError("dossier-annotation-shape")
        bid = _safe_id(item["block_id"], "dossier-annotation-block")
        if (
            bid not in block_map
            or bid not in scope_ids
            or item["anchor_kind"] not in {"block", "source"}
        ):
            raise ScholarlyReviewError("dossier-annotation-anchor")
        anchor = {
            "kind": item["anchor_kind"],
            "anchor_text": item["anchor_text"],
            "occurrence_index": (
                None
                if item["anchor_kind"] == "block"
                and item["occurrence_index"] in (None, 0)
                else item["occurrence_index"]
            ),
        }
        if item["anchor_kind"] == "block":
            if (
                item["anchor_text"] not in (None, "")
                or isinstance(item["occurrence_index"], bool)
                or item["occurrence_index"] not in (None, 0)
            ):
                raise ScholarlyReviewError("dossier-annotation-anchor")
            item_anchor_text = None
            item_occurrence = None
        else:
            item_anchor_text = None
            item_occurrence = None
        validated_anchor = _validate_anchor(anchor, block_map[bid])
        rids = item["reference_ids"]
        if not isinstance(rids, list) or any(
            not isinstance(rid, str) or rid not in ref_ids for rid in rids
        ):
            raise ScholarlyReviewError("dossier-reference-id")
        annotation_body = _safe_text(
            item["body"], "dossier-annotation-body", _MAX_BODY_CHARS
        )
        if not annotation_body.strip():
            raise ScholarlyReviewError("dossier-annotation-body")
        annotations.append(
            {
                "block_id": bid,
                "anchor_kind": validated_anchor["kind"],
                "anchor_text": item_anchor_text
                if item["anchor_kind"] == "block"
                else validated_anchor["anchor_text"],
                "occurrence_index": item_occurrence
                if item["anchor_kind"] == "block"
                else validated_anchor["occurrence_index"],
                "body": annotation_body,
                "reference_ids": list(dict.fromkeys(rids)),
            }
        )
    result = {
        "document_brief": brief,
        "style_guidance": style_out,
        "entities": entities,
        "terminology": terminology,
        "references": references,
        "allusions": allusions,
        "unresolved_questions": questions,
        "annotation_proposals": annotations,
    }
    _canonical_bytes(result)
    return result


def _normalize_provenance(provenance: Any) -> dict[str, Any]:
    if provenance is None:
        provenance = {}
    if not isinstance(provenance, Mapping) or set(provenance) - {
        "method",
        "models",
        "agents",
        "notes",
    }:
        raise ScholarlyReviewError("dossier-provenance")
    method = _safe_text(provenance.get("method", ""), "dossier-provenance", 512)
    notes = _safe_text(provenance.get("notes", ""), "dossier-provenance", 8_000)
    models = provenance.get("models", [])
    agents = provenance.get("agents", [])
    if (
        not isinstance(models, list)
        or len(models) > 64
        or not isinstance(agents, list)
        or len(agents) > 64
    ):
        raise ScholarlyReviewError("dossier-provenance")
    return {
        "method": method,
        "models": [_safe_text(item, "dossier-provenance", 512) for item in models],
        "agents": [_safe_text(item, "dossier-provenance", 512) for item in agents],
        "notes": notes,
    }


def _reference_url(value: Any) -> str | None:
    url = _safe_text(value, "dossier-reference-url", 4_096, nullable=True)
    if url is None:
        return None
    if not url or url != url.strip() or any(
        character.isspace() or ord(character) < 0x20 for character in url
    ):
        raise ScholarlyReviewError("dossier-reference-url")
    try:
        parsed = urlsplit(url)
        # Accessing ``port`` also rejects malformed or out-of-range ports.
        _ = parsed.port
    except ValueError as exc:
        raise ScholarlyReviewError("dossier-reference-url") from exc
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ScholarlyReviewError("dossier-reference-url")
    return url


def _dossier_projection(
    dossier: Any, revision: Any | None = None, *, include_payload: bool = False
) -> dict[str, Any]:
    revision = revision or _latest_dossier_revision(dossier)
    if revision is not None:
        _verify_revision_receipt(
            revision,
            purpose=_DOSSIER_REVISION_RECEIPT_PURPOSE,
            fields=_DOSSIER_REVISION_RECEIPT_FIELDS,
            extra={"scope": _stored_scope(dossier, revision)},
            code="dossier-integrity",
        )
    provenance = (
        _get(revision, "provenance_json", "provenance", default={}) if revision else {}
    )
    if isinstance(provenance, Mapping):
        provenance = {
            key: value
            for key, value in provenance.items()
            if not str(key).startswith("_")
        }
    result = {
        "id": str(_get(dossier, "id", default="")),
        "dossier_id": str(_get(dossier, "id", default="")),
        # Internal consumers must be able to prove that an immutable dossier
        # belongs to the active workspace before using its conclusions.  The
        # public scholarly wire deliberately allowlists and drops both fields.
        "project_id": _get(dossier, "project_id"),
        "source_plan_id": _get(dossier, "source_plan_id"),
        "revision_id": str(_get(revision, "id", default="")) if revision else None,
        "version": _get(revision, "revision_number", "version", default=0)
        if revision
        else 0,
        "scope": _stored_scope(dossier, revision),
        "client_submission_id": _get(
            dossier,
            "client_submission_id",
            default=_get(revision, "client_submission_id"),
        )
        if revision
        else _get(dossier, "client_submission_id", default=None),
        "submitted_at": _iso(_get(revision, "created_at", "submitted_at")),
        "provenance": provenance,
        "conclusion_provenance": "external_unverified",
        "evidence_verification_state": "unverified",
    }
    if include_payload and revision is not None:
        result["payload"] = _get(
            revision, "payload_json", "payload", "evidence_json", default={}
        )
    return result


def _stored_scope(dossier: Any, revision: Any | None = None) -> dict[str, Any]:
    value = _get(revision, "scope_json", "scope", default=None) if revision else None
    if isinstance(value, Mapping):
        return dict(value)
    value = _get(dossier, "scope_json", "scope", default=None)
    if isinstance(value, Mapping):
        return dict(value)
    return {
        "kind": _get(dossier, "scope_kind", default="document"),
        "block_ids": list(_get(dossier, "scope_block_ids", default=[])),
    }


def _require_dossier_scope(
    revision: Any,
    *,
    project_id: str,
    source_plan_id: str,
    requested_block_ids: set[str],
) -> None:
    """Require one dossier revision to cover every requested source block."""

    if (
        _get(revision, "project_id") != project_id
        or _get(revision, "source_plan_id") != source_plan_id
    ):
        raise ScholarlyReviewError("retranslation-dossier")
    dossier = db.session.get(
        _model("TranslationResearchDossier"), _get(revision, "dossier_id")
    )
    if (
        dossier is None
        or _get(dossier, "project_id") != project_id
        or _get(dossier, "source_plan_id") != source_plan_id
    ):
        raise ScholarlyReviewError("retranslation-dossier")
    scope = _stored_scope(dossier, revision)
    if scope.get("kind") == "document":
        return
    scope_ids = scope.get("block_ids")
    if (
        scope.get("kind") != "blocks"
        or not isinstance(scope_ids, list)
        or not requested_block_ids.issubset({str(item) for item in scope_ids})
    ):
        raise ScholarlyReviewError("retranslation-dossier-scope")


def _latest_dossier_revision(dossier: Any) -> Any | None:
    model = _model("TranslationResearchDossierRevision")
    rows = _query_all(model, {"dossier_id": _get(dossier, "id")})
    rows.sort(
        key=lambda row: (
            _get(row, "revision_number", "version", default=0),
            str(_get(row, "id", default="")),
        )
    )
    return rows[-1] if rows else None


def _dossier_revision_by_id(revision_id: str) -> Any:
    model = _model("TranslationResearchDossierRevision")
    revision = db.session.get(model, revision_id)
    if revision is None:
        raise ScholarlyReviewError("dossier-revision-missing")
    return revision


def block_translation_dossier_pin(
    project: Any,
    source_plan: Any,
    revision_id: str,
    *,
    blocks: Sequence[Any] | None = None,
) -> dict[str, str]:
    """Return the verified full-plan pin for one explicitly selected dossier."""

    if not isinstance(revision_id, str) or not revision_id.strip():
        raise ScholarlyReviewError("dossier-revision-missing")
    source_plan_id = _get(source_plan, "id")
    blocks = _get(source_plan, "blocks", default=()) if blocks is None else blocks
    if (
        not isinstance(source_plan_id, str)
        or _get(source_plan, "project_id") != project.id
        or not isinstance(blocks, Sequence)
    ):
        raise ScholarlyReviewError("retranslation-source-plan")
    revision = _dossier_revision_by_id(revision_id)
    dossier = db.session.get(
        _model("TranslationResearchDossier"), _get(revision, "dossier_id")
    )
    if (
        dossier is None
        or _get(dossier, "project_id") != project.id
        or _get(dossier, "source_plan_id") != source_plan_id
        or _get(dossier, "owner_user_id") != project.user_id
    ):
        raise ScholarlyReviewError("retranslation-dossier")
    _require_dossier_scope(
        revision,
        project_id=project.id,
        source_plan_id=source_plan_id,
        requested_block_ids={str(_get(block, "id")) for block in blocks},
    )
    _verify_revision_receipt(
        revision,
        purpose=_DOSSIER_REVISION_RECEIPT_PURPOSE,
        fields=_DOSSIER_REVISION_RECEIPT_FIELDS,
        extra={"scope": _stored_scope(dossier, revision)},
        code="dossier-integrity",
    )
    values = {
        "revision_id": _get(revision, "id"),
        "record_hash": _get(revision, "record_hash"),
        "content_digest": _get(revision, "content_digest"),
        "conclusion_provenance": _get(revision, "conclusion_provenance"),
        "evidence_verification_state": _get(revision, "evidence_verification_state"),
    }
    if (
        any(not isinstance(value, str) or not value for value in values.values())
        or values["revision_id"] != revision_id
        or values["conclusion_provenance"] != "external_unverified"
        or values["evidence_verification_state"] != "unverified"
    ):
        raise ScholarlyReviewError("retranslation-dossier")
    return cast(dict[str, str], values)


def submit_research_dossier(
    project: Any,
    *,
    client_submission_id: str,
    payload: Mapping[str, Any],
    provenance: Mapping[str, Any] | None,
    scope: Mapping[str, Any],
    actor: ReviewActor,
    dossier_id: str | None = None,
    expected_revision: int = 0,
) -> dict[str, Any]:
    _require_owner(project, actor)
    client_submission_id = (
        _safe_text(client_submission_id, "dossier-client-id", _MAX_CLIENT_ID) or ""
    )
    if not client_submission_id or not client_submission_id.strip():
        raise ScholarlyReviewError("dossier-client-id")
    if (
        isinstance(expected_revision, bool)
        or not isinstance(expected_revision, int)
        or expected_revision < 0
    ):
        raise ScholarlyReviewError("dossier-version")
    if dossier_id is not None:
        dossier_id = _safe_id(dossier_id, "dossier-id")
    _workspace_obj, source_plan, blocks = _workspace(project)
    normalized_scope, scope_ids = _normalize_scope(scope, blocks)
    normalized_payload = _normalize_dossier_payload(
        payload,
        blocks,
        set(scope_ids)
        if normalized_scope["kind"] == "blocks"
        else {str(block.id) for block in blocks},
    )
    normalized_provenance = _normalize_provenance(provenance)
    request = {
        "payload": normalized_payload,
        "provenance": normalized_provenance,
        "scope": normalized_scope,
        "dossier_id": dossier_id,
    }
    fingerprint = _sha(request)
    dossier_model = _model("TranslationResearchDossier")
    existing = (
        _query(dossier_model, {"project_id": project.id})
        .filter(
            getattr(dossier_model, _actual_key(dossier_model, "client_submission_id"))
            == client_submission_id
        )
        .first()
        if _actual_key(dossier_model, "client_submission_id")
        else None
    )
    existing_revision = None
    if existing is None:
        revision_model = _model("TranslationResearchDossierRevision")
        existing_revision = _query(
            revision_model,
            {"project_id": project.id, "client_submission_id": client_submission_id},
        ).first()
        if existing_revision is not None:
            existing = db.session.get(
                dossier_model, _get(existing_revision, "dossier_id")
            )
    if existing is not None:
        existing_revision = existing_revision or _latest_dossier_revision(existing)
        prior_provenance = _get(
            existing_revision, "provenance_json", "provenance", default={}
        )
        prior_fingerprint = _get(
            existing,
            "request_fingerprint",
            "submission_fingerprint",
            default=_get(
                existing_revision,
                "request_fingerprint",
                "submission_fingerprint",
                default=prior_provenance.get("_request_fingerprint")
                if isinstance(prior_provenance, Mapping)
                else None,
            ),
        )
        if prior_fingerprint != fingerprint:
            raise ScholarlyReviewError("dossier-idempotency-conflict")
        return _dossier_projection(existing, existing_revision, include_payload=True)
    dossier = db.session.get(dossier_model, dossier_id) if dossier_id else None
    previous_revision = None
    if dossier is not None:
        latest = _latest_dossier_revision(dossier)
        previous_revision = latest
        if (
            _get(dossier, "project_id") != project.id
            or _get(dossier, "source_plan_id") != source_plan.id
            or latest is None
            or expected_revision
            != _get(latest, "revision_number", "version", default=0)
            or canonical_json(_stored_scope(dossier, latest))
            != canonical_json(normalized_scope)
        ):
            raise ScholarlyReviewError(
                "dossier-version-conflict",
                latest=_dossier_projection(dossier, latest, include_payload=True),
            )
    elif expected_revision != 0:
        raise ScholarlyReviewError("dossier-version-conflict")
    now = utc_now_db()
    if dossier is None:
        dossier = _new(
            dossier_model,
            {
                "id": dossier_id or str(uuid.uuid4()),
                "project_id": project.id,
                "source_plan_id": source_plan.id,
                "owner_user_id": project.user_id,
                "scope_kind": normalized_scope["kind"],
                "scope_block_ids": normalized_scope["block_ids"],
                "created_at": now,
            },
        )
        db.session.add(dossier)
        _flush()
    revision_model = _model("TranslationResearchDossierRevision")
    revision_number = expected_revision + 1
    stored_provenance = {**normalized_provenance, "_request_fingerprint": fingerprint}
    content_digest = _sha(normalized_payload)
    revision_values = {
        "id": str(uuid.uuid4()),
        "dossier_id": dossier.id,
        "project_id": project.id,
        "source_plan_id": source_plan.id,
        "client_submission_id": client_submission_id,
        "revision_number": revision_number,
        "parent_revision_id": _get(previous_revision, "id"),
        "payload_json": normalized_payload,
        "provenance_json": stored_provenance,
        "content_digest": content_digest,
        "conclusion_provenance": "external_unverified",
        "evidence_verification_state": "unverified",
        "created_at": now,
    }
    _set_integrity_fields(
        revision_model,
        revision_values,
        hash_values={
            "dossier_id": dossier.id,
            "revision_number": revision_number,
            "content_digest": content_digest,
            "scope": normalized_scope,
        },
        receipt_purpose=_DOSSIER_REVISION_RECEIPT_PURPOSE,
        receipt_values={**revision_values, "scope": normalized_scope},
    )
    revision = _new(revision_model, revision_values)
    db.session.add(revision)
    _flush()
    glossary_rows: tuple[Any, ...] = ()
    try:
        glossary_rows = persist_analysis_glossary_proposals(
            source_plan,
            tuple(
                {
                    "source_term": item["source_term"],
                    "target_term": item["target_term"],
                    "notes": item["notes"],
                    "scope": "document",
                    "source_block_ids": item["source_block_ids"],
                    "confidence": item["confidence"],
                    "status": "proposed",
                }
                for item in normalized_payload["terminology"]
                if item["target_term"] is not None
            ),
        )
    except Exception as exc:
        if isinstance(exc, ScholarlyReviewError):
            raise
        if isinstance(exc, OperationalError):
            raise
        # Dossier evidence must remain usable when no terminology is target-bearing.
        if normalized_payload["terminology"]:
            raise ScholarlyReviewError("dossier-glossary-persistence") from exc
    link_model = _model("TranslationResearchDossierGlossaryProposal", optional=True)
    if link_model is not None:
        for order_index, glossary in enumerate(glossary_rows):
            link = _new(
                link_model,
                {
                    "id": str(uuid.uuid4()),
                    "project_id": project.id,
                    "source_plan_id": source_plan.id,
                    "dossier_id": dossier.id,
                    "dossier_revision_id": revision.id,
                    "glossary_revision_id": glossary.id,
                    "order_index": order_index,
                    "created_at": now,
                },
            )
            db.session.add(link)
    _materialize_dossier_annotations(
        project,
        source_plan,
        blocks,
        revision,
        normalized_payload["annotation_proposals"],
    )
    _materialize_dossier_suggestions(
        project, source_plan, revision, normalized_payload["terminology"], actor
    )
    _flush()
    return _dossier_projection(dossier, revision, include_payload=True)


def _materialize_dossier_suggestions(
    project: Any,
    source_plan: Any,
    revision: Any,
    terms: Sequence[Mapping[str, Any]],
    actor: ReviewActor,
) -> None:
    model = _model("TranslationGlossarySuggestion", optional=True)
    if model is None:
        return
    for index, term in enumerate(terms):
        if term.get("target_term") is None:
            continue
        suggestion_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"carlina:translator-external-glossary:{project.id}:{revision.id}:{index}",
            )
        )
        existing = db.session.get(model, suggestion_id)
        if existing is not None:
            continue
        values = {
            "id": suggestion_id,
            "project_id": project.id,
            "source_plan_id": source_plan.id,
            "dossier_revision_id": revision.id,
            "proposer_kind": "external",
            "proposer_user_id": None,
            "share_grant_id": None,
            "source_term": term["source_term"],
            "target_term": term["target_term"],
            "notes": term["notes"],
            "state": "proposed",
            "version": 1,
            "created_at": utc_now_db(),
            "updated_at": utc_now_db(),
        }
        db.session.add(_new(model, values))


def _materialize_dossier_annotations(
    project: Any,
    source_plan: Any,
    blocks: Sequence[Any],
    revision: Any,
    proposals: Sequence[Mapping[str, Any]],
) -> None:
    for index, proposal in enumerate(proposals):
        citations = [
            f"{_NOTE_DOSSIER_SENTINEL}{revision.id}:{index}",
            *proposal["reference_ids"],
        ]
        existing = _find_note_by_citation(
            project.id,
            source_plan.id,
            proposal["block_id"],
            citations[0],
        )
        if existing is not None:
            continue
        actor = ReviewActor("owner", project.user_id)
        create_reader_note(
            project,
            block_id=proposal["block_id"],
            body=proposal["body"],
            citations=citations,
            actor=actor,
            anchor={
                "kind": proposal["anchor_kind"],
                "anchor_text": proposal["anchor_text"],
                "occurrence_index": proposal["occurrence_index"],
            },
            state="proposed",
            _author_type="system",
            _allow_internal_citations=True,
            _source_plan=source_plan,
            _blocks=blocks,
        )


def _find_note_by_citation(
    project_id: str,
    source_plan_id: str,
    block_id: str,
    sentinel: str,
) -> Any | None:
    note_model = _model("TranslationReaderNote")
    revision_model = _model("TranslationReaderNoteRevision")
    rows = _query_all(
        note_model,
        {
            "project_id": project_id,
            "source_plan_id": source_plan_id,
            "block_id": block_id,
        },
    )
    rows_by_id = {str(_get(row, "id")): row for row in rows}
    note_ids = tuple(rows_by_id)
    if not note_ids:
        return None
    revisions = revision_model.query.filter(revision_model.note_id.in_(note_ids)).all()
    for revision in revisions:
        citations = _get(revision, "citations_json", "citations", default=[])
        if isinstance(citations, list) and sentinel in citations:
            return rows_by_id.get(str(_get(revision, "note_id")))
    return None


def list_research_dossiers(
    project: Any, *, include_payload: bool = False
) -> list[dict[str, Any]]:
    model = _model("TranslationResearchDossier")
    rows = _query_all(model, {"project_id": project.id})
    rows.sort(
        key=lambda row: (
            _iso(_get(row, "created_at")) or "",
            str(_get(row, "id", default="")),
        )
    )
    return [_dossier_projection(row, include_payload=include_payload) for row in rows]


def get_research_dossier(
    project: Any,
    dossier_id: str,
    *,
    revision_id: str | None = None,
    include_payload: bool = False,
) -> dict[str, Any]:
    dossier = db.session.get(_model("TranslationResearchDossier"), dossier_id)
    if dossier is None or _get(dossier, "project_id") != project.id:
        raise ScholarlyReviewError("dossier-missing")
    revision = (
        _dossier_revision_by_id(revision_id)
        if revision_id
        else _latest_dossier_revision(dossier)
    )
    if revision is not None and _get(revision, "dossier_id") != dossier.id:
        raise ScholarlyReviewError("dossier-revision-owner")
    return _dossier_projection(dossier, revision, include_payload=include_payload)


def _note_projection(
    note: Any, revision: Any, head: Any | None = None
) -> dict[str, Any]:
    _verify_revision_receipt(
        revision,
        purpose=_NOTE_RECEIPT_PURPOSE,
        fields=_NOTE_REVISION_RECEIPT_FIELDS,
        code="note-integrity",
    )
    return {
        "id": str(_get(note, "id", default="")),
        "project_id": _get(note, "project_id"),
        "source_plan_id": _get(note, "source_plan_id"),
        "revision_id": str(_get(revision, "id", default="")),
        "version": _get(
            head,
            "version",
            default=_get(revision, "revision_number", "version", default=1),
        ),
        "block_id": _get(note, "block_id"),
        "state": _get(revision, "state", "status", default="active"),
        "body": _get(revision, "body", default=""),
        "citations": _get(revision, "citations_json", "citations", default=[]),
        "anchor_kind": _get(revision, "anchor_kind", "kind", default="block"),
        "anchor_text": _get(revision, "anchor_text"),
        "occurrence_index": _get(revision, "occurrence_index"),
        "target_revision_id": _get(revision, "target_revision_id"),
        "target_version": _get(revision, "target_version"),
        "author_type": _get(revision, "author_type", default="human"),
        "author_user_id": _get(revision, "author_user_id", "user_id"),
        "created_at": _iso(_get(revision, "created_at")),
    }


def _note_rows(
    project: Any,
    source_plan: Any,
    blocks: Sequence[Any],
    *,
    block_ids: Sequence[str] | None = None,
    include_proposed: bool = False,
    include_retired: bool = False,
) -> list[dict[str, Any]]:
    note_model = _model("TranslationReaderNote")
    revision_model = _model("TranslationReaderNoteRevision")
    head_model = _model("TranslationReaderNoteHead")
    selected = set(block_ids or [str(block.id) for block in blocks])
    notes = [
        row
        for row in _query_all(
            note_model,
            {"project_id": project.id, "source_plan_id": _get(source_plan, "id")},
        )
        if str(_get(row, "block_id")) in selected
    ]
    results: list[dict[str, Any]] = []
    for note in notes:
        head = _query(head_model, {"note_id": _get(note, "id")}).first()
        revision = (
            db.session.get(revision_model, _get(head, "revision_id"))
            if head is not None
            else None
        )
        if revision is None:
            continue
        state = _get(revision, "state", "status", default="active")
        if state == "retired" and not include_retired:
            continue
        if state == "proposed" and not include_proposed:
            continue
        results.append(_note_projection(note, revision, head))
    results.sort(
        key=lambda value: (
            str(value.get("block_id")),
            str(value.get("created_at")),
            str(value.get("id")),
        )
    )
    return results


def list_reader_notes(
    project: Any,
    *,
    block_ids: Sequence[str] | None = None,
    include_proposed: bool = False,
    include_retired: bool = False,
    actor: ReviewActor | None = None,
    _source_plan: Any | None = None,
    _blocks: Sequence[Any] | Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    if actor is not None:
        _require_reader(actor, project)
        if actor.role == "viewer" and (include_proposed or include_retired):
            raise ScholarlyReviewError("review-read-forbidden")
    if _source_plan is None:
        _workspace_obj, source_plan, blocks = _workspace(project)
    else:
        if _blocks is None or str(_get(_source_plan, "project_id")) != str(project.id):
            raise ScholarlyReviewError("source-plan-context")
        source_plan = _source_plan
        blocks = _context_blocks(_blocks)
    ids = _normalize_block_ids(block_ids, blocks) if block_ids is not None else None
    return _note_rows(
        project,
        source_plan,
        blocks,
        block_ids=ids,
        include_proposed=include_proposed,
        include_retired=include_retired,
    )


def _load_current_target(
    project: Any, source_plan: Any, block_id: str
) -> tuple[Any | None, Any | None, str | None]:
    models = _models()
    active_model = getattr(models, "TranslationTargetBlockActive", None)
    revision_model = getattr(models, "TranslationTargetBlockRevision", None)
    if active_model is None or revision_model is None:
        return None, None, None
    pointer = _query(
        active_model, {"source_plan_id": source_plan.id, "block_id": block_id}
    ).first()
    if pointer is None:
        return None, None, None
    revision = db.session.get(revision_model, _get(pointer, "revision_id"))
    if revision is None:
        raise ScholarlyReviewError("target-missing")
    try:
        candidate = rehydrate_target_revision(revision, source_plan)
        value = target_text(candidate)
    except Exception as exc:
        raise ScholarlyReviewError("target-invalid") from exc
    return pointer, revision, value


def _reader_target_projection(candidate: Any, block: Any) -> str:
    """Project the prose text that the compact editor exposes as ``textContent``.

    Target integrity and legacy model-note validation intentionally continue to
    use :func:`target_text`, which only includes provider text nodes.  Reader
    notes are contextual editor actions, however, so source-owned literal
    references are visible anchor text too.  Formatting and source token
    boundaries are zero-width in DOM ``textContent`` (including ``br``).
    """

    literals = {slot.id: slot for slot in literal_reference_slots(block)}
    visible: list[str] = []
    for part in candidate.parts:
        if part.kind == "text":
            visible.append(part.text or "")
        elif part.kind == "literal":
            slot = literals.get(part.literal_slot_id or "")
            if slot is None or not isinstance(slot.source_value, str):
                raise ScholarlyReviewError("target-projection")
            # Source slots retain entity spelling durably; the editor renders
            # their decoded character values in the protected literal element.
            visible.append(html.unescape(slot.source_value))
    return "".join(visible)


def _current_reader_target(
    project: Any, source_plan: Any, block: Any
) -> tuple[Any | None, Any | None, str | None]:
    pointer, revision, _target = _load_current_target(
        project, source_plan, str(block.id)
    )
    if pointer is None or revision is None:
        return pointer, revision, None
    try:
        candidate = rehydrate_target_revision(revision, source_plan)
        target = _reader_target_projection(candidate, block)
    except ScholarlyReviewError:
        raise
    except Exception as exc:
        raise ScholarlyReviewError("target-projection") from exc
    return pointer, revision, target


def _verified_target_context(
    project: Any,
    source_plan: Any,
    block: Any,
    context: Any | None,
    *,
    reader_projection: bool,
) -> tuple[Any | None, Any | None, str | None]:
    """Use an activation caller's verified target without reloading its plan.

    The private context may be a ``(pointer, revision[, candidate])`` tuple or
    a mapping keyed by block ID to that tuple.  When the candidate is omitted,
    only its revision is rehydrated against the already-supplied source plan.
    Public callers leave this unset and retain the ordinary database lookup.
    """

    if context is None:
        return (
            _current_reader_target(project, source_plan, block)
            if reader_projection
            else _load_current_target(project, source_plan, str(block.id))
        )
    if isinstance(context, Mapping):
        if str(block.id) in context:
            context = context[str(block.id)]
        elif any(key in context for key in ("pointer", "revision", "candidate")):
            context = (
                context.get("pointer"),
                context.get("revision"),
                context.get("candidate"),
            )
        else:
            raise ScholarlyReviewError("target-projection")
    if not isinstance(context, (tuple, list)) or len(context) not in {2, 3}:
        raise ScholarlyReviewError("target-projection")
    pointer, revision = context[0], context[1]
    candidate = context[2] if len(context) == 3 else None
    if pointer is None or revision is None:
        raise ScholarlyReviewError("target-projection")

    for value in (pointer, revision, candidate):
        if value is None:
            continue
        value_plan = _get(value, "source_plan_id", default=None)
        value_block = _get(value, "block_id", default=None)
        if value_plan is not None and str(value_plan) != str(source_plan.id):
            raise ScholarlyReviewError("target-projection")
        if value_block is not None and str(value_block) != str(block.id):
            raise ScholarlyReviewError("target-projection")
    pointer_revision = _get(pointer, "revision_id", default=None)
    if (
        pointer_revision is not None
        and str(pointer_revision) != str(_get(revision, "id"))
    ):
        raise ScholarlyReviewError("target-projection")
    try:
        if candidate is None:
            candidate = rehydrate_target_revision(revision, source_plan)
        target = (
            _reader_target_projection(candidate, block)
            if reader_projection
            else target_text(candidate)
        )
    except ScholarlyReviewError:
        raise
    except Exception as exc:
        raise ScholarlyReviewError("target-projection") from exc
    return pointer, revision, target


def _note_head(note_id: str) -> tuple[Any | None, Any | None]:
    head_model = _model("TranslationReaderNoteHead")
    head = _query(head_model, {"note_id": note_id}).first()
    revision = (
        db.session.get(
            _model("TranslationReaderNoteRevision"), _get(head, "revision_id")
        )
        if head
        else None
    )
    return head, revision


def _persist_note_revision(
    project: Any,
    source_plan: Any,
    note: Any,
    *,
    body: str,
    citations: list[str],
    anchor: Mapping[str, Any],
    state: str,
    actor: ReviewActor,
    expected_head_version: int,
    author_type: str | None = None,
    revision_id: str | None = None,
) -> tuple[Any, Any]:
    if state not in {"proposed", "active", "needs_review", "retired"}:
        raise ScholarlyReviewError("note-state")
    head, previous = _note_head(str(note.id))
    current_version = _get(head, "version", default=0)
    effective_author_type = author_type or actor.role
    revision_model = _model("TranslationReaderNoteRevision")
    if revision_id is not None:
        replay = db.session.get(revision_model, revision_id)
        if replay is not None:
            replay_matches = (
                _same_optional_identity(_get(replay, "note_id"), note.id)
                and _same_optional_identity(_get(replay, "project_id"), project.id)
                and _same_optional_identity(
                    _get(replay, "source_plan_id"), source_plan.id
                )
                and _same_optional_identity(_get(replay, "block_id"), note.block_id)
                and _get(replay, "revision_number", "version")
                == expected_head_version + 1
                and _get(replay, "state", "status") == state
                and _get(replay, "body", default="") == body
                and canonical_json(
                    _get(replay, "citations_json", "citations", default=[])
                )
                == canonical_json(citations)
                and _get(replay, "anchor_kind", "kind") == anchor["kind"]
                and _get(replay, "anchor_text") == anchor["anchor_text"]
                and _get(replay, "occurrence_index") == anchor["occurrence_index"]
                and _get(replay, "witness_before") == anchor["witness_before"]
                and _get(replay, "witness_after") == anchor["witness_after"]
                and _same_optional_identity(
                    _get(replay, "target_revision_id"), anchor["target_revision_id"]
                )
                and _get(replay, "author_type") == effective_author_type
                and _same_optional_identity(
                    _get(replay, "author_user_id", "user_id"), actor.user_id
                )
                and _same_optional_identity(
                    _get(replay, "share_grant_id"), actor.share_grant_id
                )
            )
            if not replay_matches:
                latest = _note_projection(note, previous, head) if previous else None
                raise ScholarlyReviewError(
                    "note-idempotency-conflict", latest=latest
                )
            if head is None or previous is None:
                raise ScholarlyReviewError("note-corrupt")
            return note, previous
    if current_version != expected_head_version:
        latest = _note_projection(note, previous, head) if previous else None
        raise ScholarlyReviewError("note-version-conflict", latest=latest)
    transitions = {
        "proposed": {"active", "retired"},
        "active": {"retired", "needs_review"},
        "needs_review": {"active", "retired"},
        "retired": {"active"},
    }
    previous_state = _get(previous, "state", "status") if previous else None
    if (
        previous_state is not None
        and state != previous_state
        and state not in transitions.get(previous_state, set())
    ):
        raise ScholarlyReviewError("note-state-transition")
    revision_number = current_version + 1
    now = utc_now_db()
    values = {
        "id": revision_id or str(uuid.uuid4()),
        "note_id": note.id,
        "project_id": project.id,
        "source_plan_id": source_plan.id,
        "block_id": note.block_id,
        "revision_number": revision_number,
        "parent_revision_id": _get(previous, "id"),
        "state": state,
        "body": body,
        "citations_json": citations,
        "anchor_kind": anchor["kind"],
        "anchor_text": anchor["anchor_text"],
        "occurrence_index": anchor["occurrence_index"],
        "witness_before": anchor["witness_before"],
        "witness_after": anchor["witness_after"],
        "target_revision_id": anchor["target_revision_id"],
        "author_type": effective_author_type,
        "author_user_id": actor.user_id,
        "share_grant_id": actor.share_grant_id,
        "created_at": now,
    }
    _set_integrity_fields(
        revision_model,
        values,
        hash_values={k: v for k, v in values.items() if k != "record_hash"},
        receipt_purpose=_NOTE_RECEIPT_PURPOSE,
        receipt_values=values,
    )
    revision = _new(revision_model, values)
    db.session.add(revision)
    _flush()
    if head is None:
        head = _new(
            _model("TranslationReaderNoteHead"),
            {
                "note_id": note.id,
                "project_id": project.id,
                "revision_id": revision.id,
                "version": 1,
                "updated_at": now,
            },
        )
        db.session.add(head)
    else:
        _set_fields(
            head,
            {"revision_id": revision.id, "version": revision_number, "updated_at": now},
        )
    _flush()
    return note, revision


def create_reader_note(
    project: Any,
    *,
    block_id: str,
    body: str,
    citations: Sequence[str] | None = None,
    actor: ReviewActor,
    anchor: Mapping[str, Any] | None = None,
    state: str = "active",
    idempotency_key: str | None = None,
    _author_type: str | None = None,
    _allow_internal_citations: bool = False,
    _source_plan: Any | None = None,
    _blocks: Sequence[Any] | Mapping[str, Any] | None = None,
    _current_target: Any | None = None,
) -> dict[str, Any]:
    _require_mutator(project, actor)
    idempotency_key = _write_idempotency_key(
        idempotency_key, "note-idempotency"
    )
    body = _safe_text(body, "note-body", _MAX_BODY_CHARS) or ""
    if not body.strip():
        raise ScholarlyReviewError("note-body")
    citations_out = _validate_citations(
        citations, allow_internal=_allow_internal_citations
    )
    if _source_plan is None:
        _workspace_obj, source_plan, blocks = _workspace(project)
    else:
        source_project_id = _get(_source_plan, "project_id", default=None)
        if (
            source_project_id is not None
            and str(source_project_id) != str(project.id)
        ):
            raise ScholarlyReviewError("source-plan-context")
        source_plan = _source_plan
        blocks = (
            _context_blocks(_blocks)
            if _blocks is not None
            else list(_get(_source_plan, "blocks", default=[]))
        )
    block = _block_map(blocks).get(block_id)
    if block is None:
        raise ScholarlyReviewError("note-block")
    target_pointer, target_revision, target_value = _verified_target_context(
        project,
        source_plan,
        block,
        _current_target,
        reader_projection=True,
    )
    anchor_value = dict(anchor or {"kind": "block"})
    if anchor_value.get("kind", anchor_value.get("anchor_kind", "block")) == "target":
        anchor_value.setdefault(
            "target_revision_id",
            _get(target_revision, "id") if target_revision else None,
        )
        anchor_value.setdefault(
            "target_version",
            _get(target_pointer, "version") if target_pointer else None,
        )
        if target_pointer is None or target_revision is None:
            raise ScholarlyReviewError("note-target-missing")
        anchor_normalized = _validate_anchor(
            anchor_value,
            block,
            target=target_value,
            target_revision_id=str(target_revision.id),
            target_version=int(target_pointer.version),
        )
    else:
        anchor_normalized = _validate_anchor(anchor_value, block)
    note_model = _model("TranslationReaderNote")
    note_id = (
        _idempotent_write_id(
            "reader-note-create",
            project.id,
            source_plan.id,
            key=idempotency_key,
        )
        if idempotency_key is not None
        else str(uuid.uuid4())
    )
    note = db.session.get(note_model, note_id)
    if note is None:
        note = _new(
            note_model,
            {
                "id": note_id,
                "project_id": project.id,
                "source_plan_id": source_plan.id,
                "block_id": block_id,
                "created_at": utc_now_db(),
            },
        )
        db.session.add(note)
        _flush()
    elif (
        not _same_optional_identity(_get(note, "project_id"), project.id)
        or not _same_optional_identity(_get(note, "source_plan_id"), source_plan.id)
        or not _same_optional_identity(_get(note, "block_id"), block_id)
    ):
        raise ScholarlyReviewError("note-idempotency-conflict")
    revision_id = (
        _idempotent_write_id(
            "reader-note-revision", project.id, note.id, key=idempotency_key
        )
        if idempotency_key is not None
        else None
    )
    _note, revision = _persist_note_revision(
        project,
        source_plan,
        note,
        body=body,
        citations=citations_out,
        anchor=anchor_normalized,
        state=state,
        actor=actor,
        expected_head_version=0,
        author_type=_author_type,
        revision_id=revision_id,
    )
    head, _ = _note_head(str(note.id))
    return _note_projection(note, revision, head)


def _find_note(project: Any, note_id: str) -> Any:
    note = db.session.get(_model("TranslationReaderNote"), note_id)
    if note is None or _get(note, "project_id") != project.id:
        raise ScholarlyReviewError("note-missing")
    return note


def revise_reader_note(
    project: Any,
    note_id: str,
    *,
    expected_head_version: int,
    actor: ReviewActor,
    body: str | None = None,
    citations: Sequence[str] | None = None,
    anchor: Mapping[str, Any] | None = None,
    state: str | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    _require_mutator(project, actor)
    idempotency_key = _write_idempotency_key(
        idempotency_key, "note-idempotency"
    )
    if (
        isinstance(expected_head_version, bool)
        or not isinstance(expected_head_version, int)
        or expected_head_version < 1
    ):
        raise ScholarlyReviewError("note-version")
    note = _find_note(project, note_id)
    _workspace_obj, source_plan, blocks = _workspace(project)
    block = _block_map(blocks).get(str(note.block_id))
    if block is None:
        raise ScholarlyReviewError("note-block")
    head, previous = _note_head(str(note.id))
    if head is None or previous is None:
        raise ScholarlyReviewError("note-corrupt")
    body_out = (
        _safe_text(body, "note-body", _MAX_BODY_CHARS)
        if body is not None
        else _get(previous, "body", default="")
    )
    if not body_out or not body_out.strip():
        raise ScholarlyReviewError("note-body")
    previous_citations = list(
        _get(previous, "citations_json", "citations", default=[])
    )
    if citations is None:
        citations_out = previous_citations
    else:
        internal_citations = [
            citation
            for citation in previous_citations
            if isinstance(citation, str)
            and citation.startswith(_NOTE_INTERNAL_SENTINELS)
        ]
        citations_out = [
            *internal_citations,
            *(
                citation
                for citation in _validate_citations(citations)
                if citation not in internal_citations
            ),
        ]
    pointer, target_revision, target_value = _current_reader_target(
        project, source_plan, block
    )
    previous_anchor = {
        "kind": _get(previous, "anchor_kind", "kind", default="block"),
        "anchor_text": _get(previous, "anchor_text"),
        "occurrence_index": _get(previous, "occurrence_index"),
        "target_revision_id": _get(previous, "target_revision_id"),
    }
    # Note revisions persist target identity, not the active-pointer version.
    # Let the current pointer supply that input fence below; a retained target
    # revision mismatch must still be rejected by _validate_anchor.
    anchor_input = dict(anchor) if anchor is not None else previous_anchor
    if anchor_input.get("kind", anchor_input.get("anchor_kind", "block")) == "target":
        anchor_input.setdefault(
            "target_revision_id",
            _get(target_revision, "id") if target_revision else None,
        )
        anchor_input.setdefault(
            "target_version", _get(pointer, "version") if pointer else None
        )
        normalized_anchor = _validate_anchor(
            anchor_input,
            block,
            target=target_value,
            target_revision_id=str(_get(target_revision, "id"))
            if target_revision
            else None,
            target_version=_get(pointer, "version"),
        )
    else:
        normalized_anchor = _validate_anchor(anchor_input, block)
    state_out = state or _get(previous, "state", "status", default="active")
    _note, revision = _persist_note_revision(
        project,
        source_plan,
        note,
        body=cast(str, body_out),
        citations=citations_out,
        anchor=normalized_anchor,
        state=state_out,
        actor=actor,
        expected_head_version=expected_head_version,
        revision_id=(
            _idempotent_write_id(
                "reader-note-revision",
                project.id,
                note.id,
                key=idempotency_key,
            )
            if idempotency_key is not None
            else None
        ),
    )
    new_head, _ = _note_head(str(note.id))
    return _note_projection(note, revision, new_head)


def transition_reader_note(
    project: Any,
    note_id: str,
    *,
    expected_head_version: int,
    next_state: str,
    actor: ReviewActor,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    return revise_reader_note(
        project,
        note_id,
        expected_head_version=expected_head_version,
        actor=actor,
        state=next_state,
        idempotency_key=idempotency_key,
    )


def _anchor_witnesses_match(previous: Any, current: Mapping[str, Any]) -> bool:
    """Require the persisted local context to survive target retranslation."""

    prior_before = _get(previous, "witness_before", default=None)
    prior_after = _get(previous, "witness_after", default=None)
    if prior_before is None and prior_after is None:
        # Older/fake rows may not have witnesses.  Occurrence validation still
        # gives them a safe fallback, while all new rows carry both witnesses.
        return True
    return bool(
        prior_before == current.get("witness_before")
        and prior_after == current.get("witness_after")
    )


def revalidate_reader_notes_for_target(
    project: Any,
    *,
    block_id: str | None = None,
    target_revision_id: str | None = None,
    target_version: int | None = None,
    _source_plan: Any | None = None,
    _blocks: Sequence[Any] | Mapping[str, Any] | None = None,
    _current_target: Any | None = None,
) -> list[dict[str, Any]]:
    if _source_plan is None:
        _workspace_obj, source_plan, blocks = _workspace(project)
    else:
        source_project_id = _get(_source_plan, "project_id", default=None)
        if (
            _blocks is None
            or (
                source_project_id is not None
                and str(source_project_id) != str(project.id)
            )
        ):
            raise ScholarlyReviewError("source-plan-context")
        source_plan, blocks = _source_plan, _context_blocks(_blocks)
    if block_id is not None and block_id not in _block_map(blocks):
        raise ScholarlyReviewError("note-block")
    selected = [block_id] if block_id else [str(block.id) for block in blocks]
    results: list[dict[str, Any]] = []
    note_model = _model("TranslationReaderNote")
    for bid in selected:
        pointer, target_revision, target_value = _verified_target_context(
            project,
            source_plan,
            _block_map(blocks)[bid],
            _current_target,
            reader_projection=True,
        )
        if pointer is None or target_revision is None:
            continue
        if (
            target_revision_id is not None
            and str(target_revision.id) != target_revision_id
        ):
            raise ScholarlyReviewError("target-version-conflict")
        if target_version is not None and int(pointer.version) != target_version:
            raise ScholarlyReviewError("target-version-conflict")
        notes = _query_all(note_model, {"project_id": project.id, "block_id": bid})
        for note in notes:
            head, previous = _note_head(str(note.id))
            if (
                head is None
                or previous is None
                or _get(previous, "anchor_kind", "kind") != "target"
            ):
                continue
            if _get(previous, "state", "status") == "retired":
                # Retired notes are intentionally out of the review surface;
                # the lifecycle contract does not permit a direct
                # retired->needs_review transition.
                results.append(_note_projection(note, previous, head))
                continue
            # ReaderNoteRevision deliberately stores the immutable target
            # revision identity, but not the active-pointer version.  The
            # revision id is therefore the durable idempotency fence here;
            # ``target_version`` remains an input fence for callers and is
            # never required to be a persisted column.
            if str(_get(previous, "target_revision_id")) == str(target_revision.id):
                results.append(_note_projection(note, previous, head))
                continue
            anchor = {
                "kind": "target",
                "anchor_text": _get(previous, "anchor_text"),
                "occurrence_index": _get(previous, "occurrence_index"),
                "target_revision_id": target_revision.id,
                "target_version": pointer.version,
            }
            try:
                normalized = _validate_anchor(
                    anchor,
                    _block_map(blocks)[bid],
                    target=target_value,
                    target_revision_id=str(target_revision.id),
                    target_version=int(pointer.version),
                )
                state = (
                    _get(previous, "state", "status", default="active")
                    if _anchor_witnesses_match(previous, normalized)
                    else "needs_review"
                )
            except ScholarlyReviewError:
                normalized = {
                    "kind": "target",
                    "anchor_text": _get(previous, "anchor_text"),
                    "occurrence_index": _get(previous, "occurrence_index"),
                    "witness": None,
                    "witness_before": None,
                    "witness_after": None,
                    "target_revision_id": str(target_revision.id),
                    "target_version": int(pointer.version),
                }
                state = "needs_review"
            actor = ReviewActor("owner", project.user_id)
            _note, revision = _persist_note_revision(
                project,
                source_plan,
                note,
                body=_get(previous, "body", default=""),
                citations=list(
                    _get(previous, "citations_json", "citations", default=[])
                ),
                anchor=normalized,
                state=state,
                actor=actor,
                expected_head_version=int(_get(head, "version", default=0)),
                author_type="system",
            )
            new_head, _ = _note_head(str(note.id))
            results.append(_note_projection(note, revision, new_head))
    _flush()
    return results


def materialize_model_reader_notes(
    project: Any,
    target_revision: Any,
    rows: Sequence[Any] | None = None,
    *,
    _source_plan: Any | None = None,
    _blocks: Sequence[Any] | Mapping[str, Any] | None = None,
    _current_target: Any | None = None,
    _verified_plan: TranslationSourcePlan | None = None,
) -> list[dict[str, Any]]:
    if _source_plan is None:
        _workspace_obj, source_plan, blocks = _workspace(project)
    else:
        source_project_id = _get(_source_plan, "project_id", default=None)
        if (
            _blocks is None
            or (
                source_project_id is not None
                and str(source_project_id) != str(project.id)
            )
        ):
            raise ScholarlyReviewError("source-plan-context")
        source_plan, blocks = _source_plan, _context_blocks(_blocks)
    block_id = str(_get(target_revision, "block_id"))
    block = _block_map(blocks).get(block_id)
    if block is None:
        raise ScholarlyReviewError("note-block")
    pointer, current_revision, target_value = _verified_target_context(
        project,
        source_plan,
        block,
        _current_target,
        reader_projection=False,
    )
    if (
        pointer is None
        or current_revision is None
        or str(current_revision.id) != str(_get(target_revision, "id"))
    ):
        raise ScholarlyReviewError("target-version-conflict")
    if rows is None:
        legacy_model = _model("TranslationTargetBlockNote", optional=True)
        rows = (
            _query_all(
                legacy_model,
                {
                    "source_plan_id": source_plan.id,
                    "block_id": block_id,
                    "revision_id": target_revision.id,
                },
            )
            if legacy_model
            else []
        )
    valid_rows = [
        row
        for row in rows
        if _get(row, "author_type") == "model"
        and _get(row, "source_plan_id") == source_plan.id
        and _get(row, "block_id") == block_id
        and _get(row, "revision_id") == target_revision.id
    ]
    if len(valid_rows) != len(rows):
        raise ScholarlyReviewError("legacy-note-invalid")
    valid_rows.sort(
        key=lambda row: (
            _get(row, "order_index", default=0),
            str(_get(row, "id", default="")),
        )
    )
    try:
        verified_notes = rehydrate_target_block_notes(
            target_revision,
            source_plan,
            rows=valid_rows,
            verified_plan=_verified_plan,
        )
    except Exception as exc:
        raise ScholarlyReviewError("legacy-note-invalid") from exc
    actor = ReviewActor("owner", project.user_id)
    output: list[dict[str, Any]] = []
    for row, verified_note in zip(valid_rows, verified_notes, strict=True):
        sentinel = f"{_NOTE_LEGACY_SENTINEL}{row.id}"
        existing = _find_note_by_citation(
            project.id,
            source_plan.id,
            block_id,
            sentinel,
        )
        if existing is not None:
            head, revision = _note_head(str(existing.id))
            if revision is not None:
                output.append(_note_projection(existing, revision, head))
            continue
        anchor = _validate_anchor(
            {
                "kind": "target",
                "anchor_text": verified_note.anchor_text,
                "occurrence_index": 0,
                "target_revision_id": target_revision.id,
                "target_version": pointer.version,
            },
            block,
            target=target_value,
            target_revision_id=str(target_revision.id),
            target_version=int(pointer.version),
        )
        # The first validation returns private witness fields for later
        # revalidation.  Strip those derived values before passing the anchor
        # back through create_reader_note's strict input boundary.
        anchor_input = {
            key: anchor[key]
            for key in (
                "kind",
                "anchor_text",
                "occurrence_index",
                "target_revision_id",
                "target_version",
            )
        }
        output.append(
            create_reader_note(
                project,
                block_id=block_id,
                body=verified_note.body,
                citations=[sentinel],
                actor=actor,
                anchor=anchor_input,
                state="active",
                _author_type="model",
                _allow_internal_citations=True,
                _source_plan=source_plan,
                _blocks=blocks,
                _current_target=_current_target,
            )
        )
    return output


def _grant_projection(row: Any) -> dict[str, Any]:
    return {
        "id": str(_get(row, "id", default="")),
        "project_id": _get(row, "project_id"),
        "role": _get(row, "role"),
        "label": _get(row, "label"),
        "status": _get(row, "status", default="active"),
        "expires_at": _iso(_get(row, "expires_at")),
        "revoked_at": _iso(_get(row, "revoked_at")),
        "created_at": _iso(_get(row, "created_at")),
        "last_used_at": _iso(_get(row, "last_used_at")),
    }


def _parse_secret(raw: Any, prefix: str, code: str) -> tuple[str, str]:
    if not isinstance(raw, str) or not raw.startswith(prefix) or "." not in raw:
        raise ScholarlyReviewError(code)
    identifier, secret = raw[len(prefix) :].split(".", 1)
    try:
        uuid.UUID(identifier)
    except ValueError, AttributeError:
        raise ScholarlyReviewError(code) from None
    if not secret or len(secret) > 256:
        raise ScholarlyReviewError(code)
    return identifier, secret


def _digest_matches(secret: str, stored: Any, *, purpose: str) -> bool:
    if not isinstance(stored, str):
        return False
    return any(
        hmac.compare_digest(
            hashlib.sha512(candidate.encode("utf-8")).hexdigest(), stored
        )
        for candidate in keyed_lookup_candidates(secret, purpose=purpose)
    )


def _stored_secret_digest(secret: str, *, purpose: str) -> str:
    """Store a fixed-width digest of the installation-keyed lookup value."""

    return hashlib.sha512(
        keyed_lookup_digest(secret, purpose=purpose).encode("utf-8")
    ).hexdigest()


def issue_share_grant(
    project: Any,
    *,
    label: str,
    role: str,
    actor: ReviewActor,
    expires_at: datetime | None = None,
) -> tuple[dict[str, Any], str]:
    _require_owner(project, actor)
    label = _safe_text(label, "grant-label", _MAX_GRANT_LABEL) or ""
    if not label.strip() or role not in {"viewer", "reviewer"}:
        raise ScholarlyReviewError("grant-shape")
    if expires_at is not None and not isinstance(expires_at, datetime):
        raise ScholarlyReviewError("grant-expiry")
    expires_at = _db_datetime(expires_at)
    if expires_at is not None and not _expires_after_now(expires_at):
        raise ScholarlyReviewError("grant-expiry")
    grant_id = str(uuid.uuid4())
    secret = secrets.token_urlsafe(32)
    now = utc_now_db()
    grant_model = _model("TranslationShareGrant")
    row = _new(
        grant_model,
        {
            "id": grant_id,
            "project_id": project.id,
            "owner_user_id": project.user_id,
            "role": role,
            "label": label,
            "token_digest": _stored_secret_digest(
                secret, purpose=_GRANT_SECRET_PURPOSE
            ),
            "token_hint": f"{_GRANT_PREFIX}{grant_id[:4]}",
            "status": "active",
            "created_at": now,
            "expires_at": expires_at,
            "revoked_at": None,
            "last_used_at": None,
        },
    )
    db.session.add(row)
    _flush()
    return _grant_projection(row), f"{_GRANT_PREFIX}{grant_id}.{secret}"


def list_share_grants(project: Any, *, actor: ReviewActor) -> list[dict[str, Any]]:
    _require_owner(project, actor)
    rows = _query_all(_model("TranslationShareGrant"), {"project_id": project.id})
    rows.sort(
        key=lambda row: (
            _iso(_get(row, "created_at")) or "",
            str(_get(row, "id", default="")),
        )
    )
    return [_grant_projection(row) for row in rows]


def revoke_share_grant(
    project: Any, grant_id: str, *, actor: ReviewActor
) -> dict[str, Any]:
    _require_owner(project, actor)
    model = _model("TranslationShareGrant")
    row = db.session.get(model, grant_id)
    if row is None or _get(row, "project_id") != project.id:
        raise ScholarlyReviewError("grant-missing")
    if _get(row, "revoked_at") is None:
        now = utc_now_db()
        _set_fields(row, {"revoked_at": now, "status": "revoked"})
        session_model = _model("TranslationShareSession", optional=True)
        if session_model is not None:
            for session in _query_all(session_model, {"grant_id": row.id}):
                _set_fields(session, {"revoked_at": now, "status": "revoked"})
        _flush()
    return _grant_projection(row)


def resolve_share_grant(project: Any, raw_secret: str) -> ReviewActor:
    grant_id, secret = _parse_secret(raw_secret, _GRANT_PREFIX, "grant-secret")
    row = db.session.get(_model("TranslationShareGrant"), grant_id)
    now = utc_now_db()
    if (
        row is None
        or _get(row, "project_id") != project.id
        or _get(row, "revoked_at") is not None
        or _get(row, "status", default="active") != "active"
        or not _expires_after_now(_get(row, "expires_at"))
        or not _digest_matches(
            secret,
            _get(row, "token_digest", "secret_digest", "secret_hash", "lookup_digest"),
            purpose=_GRANT_SECRET_PURPOSE,
        )
    ):
        raise ScholarlyReviewError("grant-invalid")
    _set_fields(row, {"last_used_at": now})
    _flush()
    return ReviewActor(cast(str, _get(row, "role")), share_grant_id=str(row.id))


def redeem_share_grant(
    project: Any, raw_secret: str
) -> tuple[dict[str, Any], str, str]:
    resolve_share_grant(project, raw_secret)
    grant_id, _secret = _parse_secret(raw_secret, _GRANT_PREFIX, "grant-secret")
    now = utc_now_db()
    session_id = str(uuid.uuid4())
    session_secret = secrets.token_urlsafe(32)
    csrf_secret = secrets.token_urlsafe(32)
    expires = now + timedelta(minutes=_MAX_SESSION_MINUTES)
    grant = db.session.get(_model("TranslationShareGrant"), grant_id)
    if _get(grant, "expires_at") is not None:
        expires = min(expires, _db_datetime(_get(grant, "expires_at")))
    session = _new(
        _model("TranslationShareSession"),
        {
            "id": session_id,
            "grant_id": grant_id,
            "session_digest": _stored_secret_digest(
                session_secret, purpose=_SESSION_SECRET_PURPOSE
            ),
            "csrf_digest": _stored_secret_digest(
                csrf_secret, purpose=_CSRF_SECRET_PURPOSE
            ),
            "status": "active",
            "created_at": now,
            "expires_at": expires,
            "revoked_at": None,
            "last_used_at": None,
        },
    )
    db.session.add(session)
    _flush()
    return (
        _session_projection(session),
        f"{_SESSION_PREFIX}{session_id}.{session_secret}",
        csrf_secret,
    )


def _session_projection(row: Any) -> dict[str, Any]:
    return {
        "id": str(_get(row, "id", default="")),
        "grant_id": _get(row, "grant_id", "share_grant_id"),
        "status": _get(row, "status", default="active"),
        "expires_at": _iso(_get(row, "expires_at")),
        "revoked_at": _iso(_get(row, "revoked_at")),
        "created_at": _iso(_get(row, "created_at")),
        "last_used_at": _iso(_get(row, "last_used_at")),
    }


def _resolve_session_row(project: Any, raw_session: str) -> tuple[Any, Any]:
    session_id, secret = _parse_secret(raw_session, _SESSION_PREFIX, "session-secret")
    row = db.session.get(_model("TranslationShareSession"), session_id)
    grant = (
        db.session.get(
            _model("TranslationShareGrant"), _get(row, "grant_id", "share_grant_id")
        )
        if row is not None
        else None
    )
    now = utc_now_db()
    if (
        row is None
        or grant is None
        or _get(grant, "project_id") != project.id
        or _get(row, "status", default="active") != "active"
        or _get(row, "revoked_at") is not None
        or _get(grant, "revoked_at") is not None
        or _get(grant, "status", default="active") != "active"
        or not _expires_after_now(_get(row, "expires_at"))
        or not _expires_after_now(_get(grant, "expires_at"))
        or not _digest_matches(
            secret,
            _get(row, "session_digest", "secret_digest", "secret_hash"),
            purpose=_SESSION_SECRET_PURPOSE,
        )
    ):
        raise ScholarlyReviewError("session-invalid")
    _set_fields(row, {"last_used_at": now})
    _flush()
    return row, grant


def resolve_session(project: Any, raw_session: str) -> ReviewActor:
    _row, grant = _resolve_session_row(project, raw_session)
    return ReviewActor(
        cast(str, _get(grant, "role")), share_grant_id=str(_get(grant, "id"))
    )


def validate_session_csrf(project: Any, raw_session: str, raw_csrf: str) -> bool:
    row, _grant = _resolve_session_row(project, raw_session)
    return _digest_matches(
        raw_csrf, _get(row, "csrf_digest", "csrf_hash"), purpose=_CSRF_SECRET_PURPOSE
    )


def rotate_session_csrf(project: Any, raw_session: str) -> tuple[dict[str, Any], str]:
    row, _grant = _resolve_session_row(project, raw_session)
    csrf_secret = secrets.token_urlsafe(32)
    _set_fields(
        row,
        {
            "csrf_digest": _stored_secret_digest(
                csrf_secret, purpose=_CSRF_SECRET_PURPOSE
            ),
        },
    )
    _flush()
    return _session_projection(row), csrf_secret


_CURRENT_TARGET_UNSET = object()


def _review_projection(
    revision: Any,
    head: Any | None = None,
    *,
    current_target_revision_id: Any = _CURRENT_TARGET_UNSET,
    current_target_version: Any = _CURRENT_TARGET_UNSET,
) -> dict[str, Any]:
    _verify_revision_receipt(
        revision,
        purpose=_REVIEW_RECEIPT_PURPOSE,
        fields=_REVIEW_REVISION_RECEIPT_FIELDS,
        code="review-integrity",
    )
    target_revision_id = _get(revision, "target_revision_id")
    if current_target_revision_id is _CURRENT_TARGET_UNSET:
        current_target_revision_id = target_revision_id
    if current_target_version is _CURRENT_TARGET_UNSET:
        current_target_version = None
    current_target_id = (
        str(current_target_revision_id)
        if current_target_revision_id is not None
        else None
    )
    stale = current_target_id is None or str(target_revision_id) != current_target_id
    decision = _get(revision, "decision")
    return {
        "id": str(_get(revision, "id", default="")),
        "revision_id": str(_get(revision, "id", default="")),
        "project_id": str(_get(revision, "project_id", default="")),
        "source_plan_id": str(_get(revision, "source_plan_id", default="")),
        "block_id": _get(revision, "block_id"),
        "version": _get(
            head,
            "version",
            default=_get(revision, "revision_number", "version", default=1),
        ),
        "decision": decision,
        "effective_decision": "pending" if stale else decision,
        "stale": stale,
        "comment": _get(revision, "comment", default=""),
        "target_revision_id": target_revision_id,
        "current_target_revision_id": current_target_id,
        "current_target_version": current_target_version,
        "target_version": _get(revision, "target_version"),
        "author_type": _get(revision, "author_type"),
        "author_user_id": _get(revision, "author_user_id", "user_id"),
        "share_grant_id": _get(revision, "share_grant_id"),
        "created_at": _iso(_get(revision, "created_at")),
    }


def list_block_reviews(
    project: Any,
    *,
    block_ids: Sequence[str] | None = None,
    actor: ReviewActor | None = None,
    _source_plan: Any | None = None,
    _blocks: Sequence[Any] | None = None,
) -> list[dict[str, Any]]:
    if actor is not None:
        _require_reader(actor, project)
    if _source_plan is None:
        _workspace_obj, source_plan, blocks = _workspace(project)
    else:
        if (
            _blocks is None
            or str(_get(_source_plan, "project_id")) != str(project.id)
        ):
            raise ScholarlyReviewError("source-plan-context")
        source_plan, blocks = _source_plan, list(_blocks)
    selected = (
        set(_normalize_block_ids(block_ids, blocks)) if block_ids is not None else None
    )
    revision_model = _model("TranslationBlockReviewRevision")
    head_model = _model("TranslationBlockReviewHead")
    rows = [
        row
        for row in _query_all(
            revision_model, {"project_id": project.id, "source_plan_id": source_plan.id}
        )
        if selected is None or _get(row, "block_id") in selected
    ]
    rows.sort(
        key=lambda row: (
            _iso(_get(row, "created_at")) or "",
            str(_get(row, "id", default="")),
        )
    )
    active_model = _model("TranslationTargetBlockActive")
    active_targets: dict[str, Any] = {}
    for active in _query(
        active_model, {"source_plan_id": source_plan.id}
    ).all():
        block_id = str(_get(active, "block_id"))
        if selected is None or block_id in selected:
            active_targets[block_id] = active
    result: list[dict[str, Any]] = []
    for row in rows:
        head = _query(
            head_model,
            {
                "project_id": project.id,
                "source_plan_id": source_plan.id,
                "block_id": _get(row, "block_id"),
            },
        ).first()
        if head is None or str(_get(head, "revision_id")) == str(_get(row, "id")):
            active = active_targets.get(str(_get(row, "block_id")))
            result.append(
                _review_projection(
                    row,
                    head,
                    current_target_revision_id=(
                        _get(active, "revision_id") if active is not None else None
                    ),
                    current_target_version=(
                        _get(active, "version") if active is not None else None
                    ),
                )
            )
    return result


def record_block_review(
    project: Any,
    block_id: str,
    decision: str,
    comment: str,
    expected_version: int,
    expected_target_version: int | None,
    actor: ReviewActor,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    _require_mutator(project, actor)
    idempotency_key = _write_idempotency_key(
        idempotency_key, "review-idempotency"
    )
    if decision not in {"pending", "approved", "needs_changes"}:
        raise ScholarlyReviewError("review-decision")
    comment = _safe_text(comment, "review-comment", _MAX_BODY_CHARS) or ""
    if (
        isinstance(expected_version, bool)
        or not isinstance(expected_version, int)
        or expected_version < 0
    ):
        raise ScholarlyReviewError("review-version")
    _workspace_obj, source_plan, blocks = _workspace(project)
    if block_id not in _block_map(blocks):
        raise ScholarlyReviewError("review-block")
    pointer, target_revision, _target = _load_current_target(
        project, source_plan, block_id
    )
    if pointer is not None and expected_target_version != int(pointer.version):
        raise ScholarlyReviewError("review-target-version")
    if pointer is None and expected_target_version not in (None, 0):
        raise ScholarlyReviewError("review-target-version")
    head_model = _model("TranslationBlockReviewHead")
    revision_model = _model("TranslationBlockReviewRevision")
    head = _query(
        head_model,
        {
            "project_id": project.id,
            "source_plan_id": source_plan.id,
            "block_id": block_id,
        },
    ).first()
    current = _get(head, "version", default=0)
    revision_id = (
        _idempotent_write_id(
            "block-review-revision",
            project.id,
            source_plan.id,
            block_id,
            key=idempotency_key,
        )
        if idempotency_key is not None
        else None
    )
    if revision_id is not None:
        replay = db.session.get(revision_model, revision_id)
        if replay is not None:
            replay_matches = (
                _same_optional_identity(_get(replay, "project_id"), project.id)
                and _same_optional_identity(
                    _get(replay, "source_plan_id"), source_plan.id
                )
                and _same_optional_identity(_get(replay, "block_id"), block_id)
                and _get(replay, "revision_number", "version")
                == expected_version + 1
                and _get(replay, "decision") == decision
                and _get(replay, "comment", default="") == comment
                and _get(replay, "author_type") == actor.role
                and _same_optional_identity(
                    _get(replay, "author_user_id", "user_id"), actor.user_id
                )
                and _same_optional_identity(
                    _get(replay, "share_grant_id"), actor.share_grant_id
                )
            )
            if not replay_matches:
                latest_revision = (
                    db.session.get(revision_model, _get(head, "revision_id"))
                    if head
                    else None
                )
                raise ScholarlyReviewError(
                    "review-idempotency-conflict",
                    latest=_review_projection(
                        latest_revision,
                        head,
                        current_target_revision_id=_get(target_revision, "id"),
                        current_target_version=_get(pointer, "version"),
                    )
                    if latest_revision
                    else None,
                )
            latest_revision = (
                db.session.get(revision_model, _get(head, "revision_id"))
                if head
                else None
            )
            if latest_revision is None:
                raise ScholarlyReviewError("review-corrupt")
            return _review_projection(
                latest_revision,
                head,
                current_target_revision_id=_get(target_revision, "id"),
                current_target_version=_get(pointer, "version"),
            )
    if current != expected_version:
        latest_revision = (
            db.session.get(revision_model, _get(head, "revision_id")) if head else None
        )
        raise ScholarlyReviewError(
            "review-version-conflict",
            latest=_review_projection(
                latest_revision,
                head,
                current_target_revision_id=_get(target_revision, "id"),
                current_target_version=_get(pointer, "version"),
            )
            if latest_revision
            else None,
        )
    now = utc_now_db()
    revision_number = expected_version + 1
    values = {
        "id": revision_id or str(uuid.uuid4()),
        "project_id": project.id,
        "source_plan_id": source_plan.id,
        "block_id": block_id,
        "revision_number": revision_number,
        "parent_revision_id": _get(head, "revision_id"),
        "decision": decision,
        "comment": comment,
        "target_revision_id": _get(target_revision, "id"),
        "author_type": actor.role,
        "author_user_id": actor.user_id,
        "share_grant_id": actor.share_grant_id,
        "created_at": now,
    }
    _set_integrity_fields(
        revision_model,
        values,
        hash_values={k: v for k, v in values.items() if k != "record_hash"},
        receipt_purpose=_REVIEW_RECEIPT_PURPOSE,
        receipt_values=values,
    )
    revision = _new(revision_model, values)
    db.session.add(revision)
    _flush()
    if head is None:
        head = _new(
            head_model,
            {
                "project_id": project.id,
                "source_plan_id": source_plan.id,
                "block_id": block_id,
                "revision_id": revision.id,
                "version": 1,
                "updated_at": now,
            },
        )
        db.session.add(head)
    else:
        _set_fields(
            head,
            {"revision_id": revision.id, "version": revision_number, "updated_at": now},
        )
    _flush()
    return _review_projection(
        revision,
        head,
        current_target_revision_id=_get(target_revision, "id"),
        current_target_version=_get(pointer, "version"),
    )


def _suggestion_projection(row: Any) -> dict[str, Any]:
    state = _get(row, "state", default="proposed")
    return {
        "id": str(_get(row, "id", default="")),
        "source_term": _get(row, "source_term"),
        "target_term": _get(row, "target_term"),
        "notes": _get(row, "notes"),
        "proposer_kind": _get(row, "proposer_kind"),
        "proposer_grant_id": _get(
            row, "proposer_grant_id", "share_grant_id", "grant_id"
        ),
        "dossier_revision_id": _get(row, "dossier_revision_id"),
        "state": state,
        "version": _get(row, "version", default=1),
        "accepted_revision_id": _get(row, "accepted_revision_id"),
        "created_at": _iso(_get(row, "created_at")),
        "resolved_at": _iso(
            _get(
                row,
                "resolved_at",
                default=_get(row, "updated_at")
                if state in {"accepted", "rejected"}
                else None,
            )
        ),
    }


def list_glossary_suggestions(
    project: Any, *, include_resolved: bool = False
) -> list[dict[str, Any]]:
    model = _model("TranslationGlossarySuggestion")
    rows = _query_all(model, {"project_id": project.id})
    if not include_resolved:
        rows = [
            row
            for row in rows
            if _get(row, "state", default="proposed") not in {"accepted", "rejected"}
        ]
    rows.sort(
        key=lambda row: (
            _iso(_get(row, "created_at")) or "",
            str(_get(row, "id", default="")),
        )
    )
    return [_suggestion_projection(row) for row in rows]


def create_glossary_suggestion(
    project: Any,
    source_term: str,
    target_term: str | None,
    notes: str | None,
    actor: ReviewActor,
    idempotency_key: str,
) -> dict[str, Any]:
    _require_mutator(project, actor)
    if actor.role != "reviewer":
        raise ScholarlyReviewError("glossary-suggestion-reviewer-required")
    source_term = _safe_text(source_term, "glossary-source-term", 4_096) or ""
    target_term = _safe_text(target_term, "glossary-target-term", 4_096, nullable=True)
    notes = _safe_text(notes, "glossary-notes", 32_768, nullable=True)
    if (
        not source_term.strip()
        or target_term is None
        or not target_term.strip()
        or not idempotency_key
        or not idempotency_key.strip()
        or len(idempotency_key) > _MAX_CLIENT_ID
    ):
        raise ScholarlyReviewError("glossary-suggestion-shape")
    _workspace_obj, source_plan, _blocks = _workspace(project)
    model = _model("TranslationGlossarySuggestion")
    fingerprint = _sha(
        {
            "source_term": source_term,
            "target_term": target_term,
            "notes": notes,
            "source_plan_id": source_plan.id,
        }
    )
    suggestion_id = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"carlina:translator-glossary-suggestion:{project.id}:{idempotency_key}",
        )
    )
    existing = db.session.get(model, suggestion_id)
    if existing is not None:
        prior_fingerprint = _get(
            existing,
            "request_fingerprint",
            default=_sha(
                {
                    "source_term": _get(existing, "source_term"),
                    "target_term": _get(existing, "target_term"),
                    "notes": _get(existing, "notes"),
                    "source_plan_id": _get(existing, "source_plan_id"),
                }
            ),
        )
        if prior_fingerprint != fingerprint:
            raise ScholarlyReviewError("glossary-suggestion-idempotency-conflict")
        return _suggestion_projection(existing)
    now = utc_now_db()
    values = {
        "id": suggestion_id,
        "project_id": project.id,
        "source_plan_id": source_plan.id,
        "proposer_kind": "reviewer",
        "proposer_user_id": actor.user_id,
        "share_grant_id": actor.share_grant_id,
        "source_term": source_term,
        "target_term": target_term,
        "notes": notes,
        "state": "proposed",
        "version": 1,
        "created_at": now,
        "updated_at": now,
    }
    row = _new(model, values)
    db.session.add(row)
    _flush()
    return _suggestion_projection(row)


def resolve_glossary_suggestion(
    project: Any,
    suggestion_id: str,
    expected_version: int,
    decision: str,
    approved_payload: Mapping[str, Any] | None = None,
    actor: ReviewActor | None = None,
) -> dict[str, Any]:
    if actor is None:
        raise ScholarlyReviewError("review-owner-required")
    _require_owner(project, actor)
    decision = {"accept": "accepted", "reject": "rejected"}.get(decision, decision)
    if decision not in {"accepted", "rejected"}:
        raise ScholarlyReviewError("glossary-suggestion-decision")
    model = _model("TranslationGlossarySuggestion")
    row = db.session.get(model, suggestion_id)
    if row is None or _get(row, "project_id") != project.id:
        raise ScholarlyReviewError("glossary-suggestion-missing")
    if _get(row, "version", default=1) != expected_version:
        raise ScholarlyReviewError(
            "glossary-suggestion-version-conflict", latest=_suggestion_projection(row)
        )
    accepted_revision_id = None
    if decision == "accepted":
        payload = dict(
            approved_payload
            or {
                "source_term": _get(row, "source_term"),
                "target_term": _get(row, "target_term"),
                "notes": _get(row, "notes"),
            }
        )
        if set(payload) != {"source_term", "target_term", "notes"}:
            raise ScholarlyReviewError("glossary-suggestion-payload")
        source_plan = db.session.get(
            _model("TranslationSourcePlan"), _get(row, "source_plan_id")
        )
        try:
            glossary_payload = GlossaryPayload(
                source_term=payload["source_term"],
                target_term=payload["target_term"],
                notes=payload["notes"],
                scope="document",
            )
            revisions = find_glossary_revisions_for_term(
                source_plan, glossary_payload.source_term
            )
            concept = (
                db.session.get(
                    _model("TranslationGlossaryConcept"), revisions[-1].concept_id
                )
                if revisions
                else create_glossary_concept(source_plan)
            )
            proposal = persist_glossary_revision(
                source_plan,
                concept,
                payload=glossary_payload,
                author_type="model",
                revision_state="proposal",
            )
            approval_model = getattr(
                importlib.import_module("...models", package=__package__),
                "RuntimeApprovalRequest",
                None,
            )
            if approval_model is None:
                raise ScholarlyReviewError("glossary-approval-unavailable")
            approval = approval_model(
                requester_user_id=project.user_id,
                # The authenticated approval helper performs the owner CAS and
                # writes approver_user_id itself.  Pre-populating this field
                # would make the durable request look already consumed and is
                # correctly rejected by that helper.
                approver_user_id=None,
                subject_type="translator.glossary-lock.v1",
                subject_id=proposal.id,
                preview_payload=glossary_lock_preview(
                    source_plan, concept, proposal, glossary_payload
                ),
                status="pending",
                resolved_at=None,
            )
            db.session.add(approval)
            _flush()
            locked = approve_glossary_proposal(
                source_plan,
                proposal,
                approved_payload=glossary_payload,
                approval=approval.id,
            )
            active_model = _model("TranslationGlossaryActive")
            active = db.session.get(active_model, concept.id)
            activate_glossary_revision(
                locked, expected_version=int(_get(active, "version", default=0))
            )
            accepted_revision_id = locked.id
        except ScholarlyReviewError:
            raise
        except Exception as exc:
            raise ScholarlyReviewError("glossary-activation-failed") from exc
    now = utc_now_db()
    _set_fields(
        row,
        {
            "state": decision,
            "version": expected_version + 1,
            "accepted_revision_id": accepted_revision_id,
            "resolved_at": now,
            "updated_at": now,
            "resolved_by_user_id": actor.user_id,
        },
    )
    _flush()
    return _suggestion_projection(row)


def _request_projection(row: Any) -> dict[str, Any]:
    stored_scope = _get(row, "scope_json", default={})
    requested_scope = _get(row, "requested_scope_json", "requested_scope", default=None)
    resolved_ids = _get(
        row, "resolved_block_ids_json", "resolved_block_ids", default=None
    )
    if isinstance(stored_scope, Mapping) and "requested" in stored_scope:
        requested_scope = stored_scope.get("requested")
        resolved_ids = stored_scope.get("resolved_block_ids", [])
    return {
        "id": str(_get(row, "id", default="")),
        "scope": requested_scope
        if requested_scope is not None
        else {
            "kind": _get(row, "scope_kind", default="document"),
            "block_ids": list(resolved_ids or []),
        },
        "resolved_block_ids": list(resolved_ids or []),
        "custom_prompt": _get(row, "custom_prompt", default=""),
        "annotation_profile": _get(row, "annotation_profile"),
        "research_choice": _get(row, "research_choice"),
        "dossier_revision_id": _get(row, "dossier_revision_id"),
        "rationale": _get(row, "rationale", default=""),
        "state": _get(row, "state", default="requested"),
        "version": _get(row, "version", default=1),
        "operation_id": _get(row, "operation_id"),
        "created_at": _iso(_get(row, "created_at")),
        "updated_at": _iso(_get(row, "updated_at")),
    }


def create_retranslation_request(
    project: Any,
    *,
    scope: Mapping[str, Any],
    custom_prompt: str,
    annotation_profile: Any,
    research_choice: str,
    dossier_revision_id: str | None,
    rationale: str,
    idempotency_key: str,
    actor: ReviewActor,
) -> dict[str, Any]:
    _require_mutator(project, actor)
    _workspace_obj, source_plan, blocks = _workspace(project)
    requested_scope = dict(scope)
    resolved_scope, block_ids = _normalize_scope(
        requested_scope, blocks, allow_range=True
    )
    prompt = _safe_text(custom_prompt, "retranslation-prompt", 20_000) or ""
    rationale = _safe_text(rationale, "retranslation-rationale", 4_000) or ""
    profile = normalize_annotation_profile(annotation_profile)
    if research_choice not in {
        "reuse_dossier",
        "await_external",
        "fresh_document",
    }:
        raise ScholarlyReviewError("retranslation-research-choice")
    if research_choice == "reuse_dossier":
        if not dossier_revision_id:
            raise ScholarlyReviewError("retranslation-dossier")
        revision = _dossier_revision_by_id(dossier_revision_id)
        _require_dossier_scope(
            revision,
            project_id=project.id,
            source_plan_id=source_plan.id,
            requested_block_ids=set(block_ids),
        )
    elif dossier_revision_id is not None:
        raise ScholarlyReviewError("retranslation-dossier")
    if resolved_scope.get("kind") == "document":
        raise ScholarlyReviewError("retranslation-scope")
    if (
        not isinstance(idempotency_key, str)
        or not 1 <= len(idempotency_key) <= _MAX_CLIENT_ID
        or not idempotency_key.strip()
    ):
        raise ScholarlyReviewError("retranslation-idempotency")
    fingerprint = _sha(
        {
            "scope": requested_scope,
            "resolved_block_ids": block_ids,
            "custom_prompt": prompt,
            "annotation_profile": profile,
            "research_choice": research_choice,
            "dossier_revision_id": dossier_revision_id,
            "rationale": rationale,
        }
    )
    model = _model("TranslationRetranslationRequest")
    existing = _query(
        model, {"project_id": project.id, "idempotency_key": idempotency_key}
    ).first()
    if existing is not None:
        if _get(existing, "request_fingerprint") != fingerprint:
            raise ScholarlyReviewError("retranslation-idempotency-conflict")
        return _request_projection(existing)
    now = utc_now_db()
    values = {
        "id": str(uuid.uuid4()),
        "project_id": project.id,
        "source_plan_id": source_plan.id,
        "requester_user_id": actor.user_id,
        "share_grant_id": actor.share_grant_id,
        "scope_kind": "order_range" if resolved_scope["kind"] == "range" else "blocks",
        "scope_json": {
            "requested": requested_scope,
            "resolved_block_ids": block_ids,
            "resolved_scope": resolved_scope,
        },
        "custom_prompt": prompt,
        "annotation_profile": profile,
        "research_choice": research_choice,
        "dossier_revision_id": dossier_revision_id,
        "rationale": rationale,
        "idempotency_key": idempotency_key,
        "request_fingerprint": fingerprint,
        "state": "requested",
        "version": 1,
        "created_at": now,
        "updated_at": now,
    }
    _set_integrity_fields(
        model,
        values,
        hash_values={"id": values["id"], "request_fingerprint": fingerprint},
        receipt_purpose=_REQUEST_RECEIPT_PURPOSE,
        receipt_values={"id": values["id"], "request_fingerprint": fingerprint},
    )
    row = _new(model, values)
    db.session.add(row)
    _flush()
    return _request_projection(row)


def list_retranslation_requests(
    project: Any, *, actor: ReviewActor | None = None
) -> list[dict[str, Any]]:
    if actor is not None:
        _require_reader(actor, project)
    rows = _query_all(
        _model("TranslationRetranslationRequest"), {"project_id": project.id}
    )
    rows.sort(
        key=lambda row: (
            _iso(_get(row, "created_at")) or "",
            str(_get(row, "id", default="")),
        )
    )
    return [_request_projection(row) for row in rows]


def get_retranslation_request(
    project: Any, request_id: str, *, actor: ReviewActor | None = None
) -> dict[str, Any]:
    if actor is not None:
        _require_reader(actor, project)
    row = db.session.get(_model("TranslationRetranslationRequest"), request_id)
    if row is None or _get(row, "project_id") != project.id:
        raise ScholarlyReviewError("retranslation-missing")
    return _request_projection(row)


def retranslation_request_projection(row: Any) -> dict[str, Any]:
    """Return the internal projection used by trusted execution callers."""

    return _request_projection(row)


def retranslation_request_record(
    project: Any, request_id: str, *, actor: ReviewActor
) -> Any:
    """Load one owner-executable request without exposing its durable binding."""

    _require_owner(project, actor)
    row = db.session.get(_model("TranslationRetranslationRequest"), request_id)
    if row is None or _get(row, "project_id") != project.id:
        raise ScholarlyReviewError("retranslation-missing")
    return row


def attach_retranslation_dossier(
    project: Any,
    request_id: str,
    *,
    dossier_revision_id: str,
    expected_version: int,
    actor: ReviewActor,
) -> dict[str, Any]:
    """CAS-bind a complete external dossier to one awaiting request.

    This service deliberately does not commit.  The HTTP adapter owns the
    transport transaction and execute performs its bind inside operation
    publication's transaction.
    """

    request = retranslation_request_record(project, request_id, actor=actor)
    _workspace_obj, source_plan, _blocks = _workspace(project)
    if _get(request, "source_plan_id") != source_plan.id:
        raise ScholarlyReviewError("retranslation-source-plan-conflict")
    if (
        isinstance(expected_version, bool)
        or not isinstance(expected_version, int)
        or expected_version < 1
    ):
        raise ScholarlyReviewError("retranslation-version")
    if _get(request, "version", default=1) != expected_version:
        raise ScholarlyReviewError(
            "retranslation-version-conflict", latest=_request_projection(request)
        )
    if _get(request, "state", default="requested") != "requested":
        raise ScholarlyReviewError("retranslation-transition")
    if _get(request, "research_choice") != "await_external":
        raise ScholarlyReviewError("retranslation-dossier")
    revision = _dossier_revision_by_id(dossier_revision_id)
    if (
        _get(revision, "project_id") != project.id
        or _get(revision, "source_plan_id") != source_plan.id
    ):
        raise ScholarlyReviewError("retranslation-dossier")
    requested_ids = set(_request_projection(request)["resolved_block_ids"])
    _require_dossier_scope(
        revision,
        project_id=project.id,
        source_plan_id=source_plan.id,
        requested_block_ids=requested_ids,
    )
    now = utc_now_db()
    _set_fields(
        request,
        {
            "research_choice": "reuse_dossier",
            "dossier_revision_id": _get(revision, "id"),
            "version": expected_version + 1,
            "updated_at": now,
        },
    )
    _flush()
    return _request_projection(request)


def start_retranslation_request(
    request: Any,
    *,
    expected_version: int,
    operation: Any,
    actor: ReviewActor,
) -> dict[str, Any]:
    """Bind one newly queued operation and atomically enter ``started``."""

    project = db.session.get(_model("TranslationProject"), _get(request, "project_id"))
    if project is None or not actor_is_owner(project, actor):
        raise ScholarlyReviewError("retranslation-owner-required")
    if (
        _get(request, "state", default="requested") != "requested"
        or _get(request, "version", default=1) != expected_version
    ):
        raise ScholarlyReviewError(
            "retranslation-version-conflict", latest=_request_projection(request)
        )
    if (
        _get(operation, "project_id") != project.id
        or _get(operation, "kind") != "document_block_translate"
        or _get(operation, "source_plan_id") != _get(request, "source_plan_id")
    ):
        raise ScholarlyReviewError("retranslation-operation")
    now = utc_now_db()
    _set_fields(
        request,
        {
            "state": "started",
            "operation_id": _get(operation, "id"),
            "version": expected_version + 1,
            "started_at": now,
            "updated_at": now,
        },
    )
    _flush()
    return _request_projection(request)


def recover_retranslation_request(
    request: Any,
    *,
    expected_version: int,
    failed_operation: Any,
    recovery_operation: Any,
    actor: ReviewActor,
) -> dict[str, Any]:
    """CAS-rebind a started request from its failed operation to a recovery."""

    project = db.session.get(_model("TranslationProject"), _get(request, "project_id"))
    if project is None or not actor_is_owner(project, actor):
        raise ScholarlyReviewError("retranslation-owner-required")
    if (
        _get(request, "state") != "started"
        or _get(request, "version", default=1) != expected_version
        or _get(request, "operation_id") != _get(failed_operation, "id")
        or _get(failed_operation, "state") != "failed"
    ):
        raise ScholarlyReviewError(
            "retranslation-version-conflict", latest=_request_projection(request)
        )
    for operation in (failed_operation, recovery_operation):
        if (
            _get(operation, "project_id") != project.id
            or _get(operation, "kind") != "document_block_translate"
            or _get(operation, "source_plan_id") != _get(request, "source_plan_id")
        ):
            raise ScholarlyReviewError("retranslation-operation")
    _set_fields(
        request,
        {
            "operation_id": _get(recovery_operation, "id"),
            "version": expected_version + 1,
            "updated_at": utc_now_db(),
        },
    )
    _flush()
    return _request_projection(request)


def retranslation_dossier_pin(request: Any) -> dict[str, str]:
    """Return the immutable dossier identity required by a reuse operation."""

    revision_id = _get(request, "dossier_revision_id")
    if _get(request, "research_choice") != "reuse_dossier" or not revision_id:
        raise ScholarlyReviewError("retranslation-dossier")
    revision = _dossier_revision_by_id(str(revision_id))
    _require_dossier_scope(
        revision,
        project_id=str(_get(request, "project_id")),
        source_plan_id=str(_get(request, "source_plan_id")),
        requested_block_ids=set(_request_projection(request)["resolved_block_ids"]),
    )
    values = {
        "revision_id": _get(revision, "id"),
        "record_hash": _get(revision, "record_hash"),
        "content_digest": _get(revision, "content_digest"),
        "conclusion_provenance": _get(revision, "conclusion_provenance"),
        "evidence_verification_state": _get(revision, "evidence_verification_state"),
    }
    if any(not isinstance(value, str) or not value for value in values.values()):
        raise ScholarlyReviewError("retranslation-dossier")
    return cast(dict[str, str], values)


def synchronize_retranslation_request_terminal(operation: Any, state: str) -> None:
    """Mirror a linked terminal operation inside the caller's open transaction."""

    if state not in {"completed", "cancelled"}:
        return
    if (
        _get(operation, "kind") != "document_block_translate"
        or _get(operation, "state") != state
    ):
        return
    request = _query(
        _model("TranslationRetranslationRequest"),
        {"operation_id": _get(operation, "id")},
    ).one_or_none()
    if request is None:
        return
    if (
        _get(request, "project_id") != _get(operation, "project_id")
        or _get(request, "source_plan_id") != _get(operation, "source_plan_id")
        or _get(request, "state") != "started"
    ):
        raise ScholarlyReviewError("retranslation-operation")
    now = utc_now_db()
    _set_fields(
        request,
        {
            "state": state,
            "version": _get(request, "version", default=1) + 1,
            "updated_at": now,
            "completed_at": now if state == "completed" else _get(request, "completed_at"),
        },
    )
    _flush()


def transition_retranslation_request(
    request: Any,
    expected_version: int,
    next_state: str,
    operation_id: str | None = None,
    actor: ReviewActor | None = None,
) -> dict[str, Any]:
    project = getattr(request, "project", None)
    if project is None:
        project = db.session.get(
            _model("TranslationProject"), _get(request, "project_id")
        )
    if actor is None or project is None or not actor_is_owner(project, actor):
        raise ScholarlyReviewError("retranslation-owner-required")
    if (
        next_state not in {"started", "rejected", "cancelled", "completed"}
        or isinstance(expected_version, bool)
        or not isinstance(expected_version, int)
        or expected_version < 1
    ):
        raise ScholarlyReviewError("retranslation-transition")
    current = _get(request, "state", default="requested")
    allowed = {
        "requested": {"started", "rejected", "cancelled"},
        "started": {"completed", "cancelled"},
    }
    if next_state not in allowed.get(current, set()):
        raise ScholarlyReviewError("retranslation-transition")
    version = _get(request, "version", default=1)
    if version != expected_version:
        raise ScholarlyReviewError(
            "retranslation-version-conflict", latest=_request_projection(request)
        )
    if operation_id is not None:
        operation_id = _safe_text(operation_id, "retranslation-operation", 256)
        if not operation_id or not operation_id.strip():
            raise ScholarlyReviewError("retranslation-operation")
    now = utc_now_db()
    updates = {
        "state": next_state,
        "version": version + 1,
        "updated_at": now,
        "started_at": now if next_state == "started" else _get(request, "started_at"),
        "completed_at": now
        if next_state == "completed"
        else _get(request, "completed_at"),
    }
    if operation_id is not None:
        updates["operation_id"] = operation_id
    _set_fields(request, updates)
    _flush()
    return _request_projection(request)


__all__ = [
    "ReviewActor",
    "ScholarlyReviewError",
    "actor_can_execute",
    "actor_can_mutate",
    "actor_can_read",
    "annotation_profile_notes_enabled",
    "attach_retranslation_dossier",
    "block_translation_dossier_pin",
    "create_glossary_suggestion",
    "create_reader_note",
    "create_retranslation_request",
    "get_research_dossier",
    "get_retranslation_request",
    "issue_share_grant",
    "list_block_reviews",
    "list_glossary_suggestions",
    "list_reader_notes",
    "list_research_dossiers",
    "list_retranslation_requests",
    "list_share_grants",
    "materialize_model_reader_notes",
    "normalize_annotation_profile",
    "record_block_review",
    "recover_retranslation_request",
    "redeem_share_grant",
    "resolve_glossary_suggestion",
    "resolve_session",
    "resolve_share_grant",
    "retranslation_dossier_pin",
    "retranslation_request_projection",
    "retranslation_request_record",
    "revalidate_reader_notes_for_target",
    "revise_reader_note",
    "revoke_share_grant",
    "rotate_session_csrf",
    "start_retranslation_request",
    "submit_research_dossier",
    "synchronize_retranslation_request_terminal",
    "transition_reader_note",
    "transition_retranslation_request",
    "validate_session_csrf",
]
