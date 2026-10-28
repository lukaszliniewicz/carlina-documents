"""Alignment-witnessed translation-memory evidence.

The functions in this module create a replaceable, encrypted projection of
verified Phase-4 target/alignment pairs.  They deliberately do not touch the
legacy segment memory or vector tables, and they never commit the caller's
transaction.
"""

from __future__ import annotations

import json
import unicodedata
import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import Any, cast

from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from carlina.core.utc import utc_iso, utc_now_db
from iarf_assistant.core.encryption import keyed_lookup_candidates, keyed_lookup_digest
from iarf_assistant.extensions import db

from .alignment_contract import (
    ALIGNMENT_CONFIDENCE_THRESHOLD,
    target_alignment_projection,
)
from .alignment_persistence import (
    rehydrate_alignment_revision,
    rehydrate_target_span_set,
)
from .analysis_contract import private_witness, private_witness_candidates
from .block_contract import BlockContractError, canonical_json, sha256_text
from .block_persistence import _ensure_database_transaction, rehydrate_source_plan
from .integrity_receipts import make_receipt, verify_receipt
from .models import (
    TranslationAlignmentGroup,
    TranslationAlignmentRevision,
    TranslationMemoryEntry,
    TranslationProject,
    TranslationSourcePlan,
    TranslationTargetAlignmentActive,
    TranslationTargetBlockActive,
    TranslationTargetBlockRevision,
    TranslationTargetSpanSet,
)
from .target_persistence import rehydrate_target_revision, verify_active_pair

MEMORY_CONTRACT_VERSION = "translation-memory-v1"
MEMORY_CLASSIFICATIONS = frozenset({"contextual_precedent", "deliberate_variant"})
MEMORY_GRANULARITIES = frozenset({"sentence", "block"})
MEMORY_SCOPE = "project"

_SOURCE_WITNESS_PURPOSE = "translator-memory-source-v1"
_TARGET_WITNESS_PURPOSE = "translator-memory-target-v1"
_SOURCE_EVIDENCE_PURPOSE = "translator-memory-source-evidence-v1"
_TARGET_EVIDENCE_PURPOSE = "translator-memory-target-evidence-v1"
_GROUP_IDS_PURPOSE = "translator-memory-group-ids-v1"
_PROVENANCE_PURPOSE = "translator-memory-provenance-v1"
_RECORD_PURPOSE = "translator-memory-record-v1"
_LOOKUP_PURPOSE = "translator-memory-lookup-v1"
_MAX_TEXT_BYTES = 512 * 1024
_MAX_JSON_BYTES = 256 * 1024


def _active_session(session: Any | None) -> Any:
    return session if session is not None else db.session()


def _require_record_session(
    record: TranslationSourcePlan,
    *,
    session: Any,
) -> None:
    state = inspect(record)
    if state is None or not state.persistent:
        raise BlockContractError("memory-source-plan-persistent")
    if state.session is not session:
        raise BlockContractError("memory-source-plan-session")


def _bounded_text(value: object, code: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BlockContractError(code)
    if len(value.encode("utf-8")) > _MAX_TEXT_BYTES:
        raise BlockContractError(code)
    return value


def _json_object(value: object, code: str, *, maximum: int = _MAX_JSON_BYTES) -> Any:
    try:
        encoded = canonical_json(value)
        if len(encoded.encode("utf-8")) > maximum:
            raise BlockContractError(code)
        isolated = json.loads(encoded)
    except BlockContractError:
        raise
    except (RecursionError, TypeError, ValueError) as exc:
        raise BlockContractError(code) from exc
    return isolated


def _lookup_text(value: str) -> str:
    normalized = " ".join(unicodedata.normalize("NFKC", value).casefold().split())
    if not normalized:
        raise BlockContractError("memory-lookup-empty")
    if len(normalized.encode("utf-8")) > _MAX_TEXT_BYTES:
        raise BlockContractError("memory-lookup-size")
    return normalized


def _lookup_purpose(
    source_language: str,
    target_language: str,
    *,
    owner_user_id: int,
    project_id: str,
    role: str,
) -> str:
    return (
        f"{_LOOKUP_PURPOSE}:{owner_user_id}:{project_id}:"
        f"{source_language}:{target_language}:{role}"
    )


def _lookup_digest(
    value: str,
    *,
    source_language: str,
    target_language: str,
    owner_user_id: int,
    project_id: str,
    role: str,
) -> str:
    return keyed_lookup_digest(
        _lookup_text(value),
        purpose=_lookup_purpose(
            source_language,
            target_language,
            owner_user_id=owner_user_id,
            project_id=project_id,
            role=role,
        ),
    )


def _lookup_candidates(
    value: str,
    *,
    source_language: str,
    target_language: str,
    owner_user_id: int,
    project_id: str,
    role: str,
) -> tuple[str, ...]:
    return keyed_lookup_candidates(
        _lookup_text(value),
        purpose=_lookup_purpose(
            source_language,
            target_language,
            owner_user_id=owner_user_id,
            project_id=project_id,
            role=role,
        ),
    )


def _hash(value: object, code: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise BlockContractError(code)
    return value


def _owner_project(
    record: TranslationSourcePlan,
    *,
    session: Any,
) -> tuple[TranslationProject, int]:
    if not isinstance(record, TranslationSourcePlan):
        raise BlockContractError("memory-source-plan-type")
    _require_record_session(record, session=session)
    project = session.get(TranslationProject, record.project_id)
    if (
        project is None
        or project.id != record.project_id
        or not isinstance(project.user_id, int)
    ):
        raise BlockContractError("memory-project-owner")
    return project, project.user_id


def _plan_and_block(
    record: TranslationSourcePlan,
    block_id: str,
    *,
    session: Any,
) -> tuple[Any, Any, TranslationProject, int]:
    project, owner_user_id = _owner_project(record, session=session)
    plan = rehydrate_source_plan(record)
    block = next((item for item in plan.blocks if item.id == block_id), None)
    if block is None:
        raise BlockContractError("memory-block-owner")
    return plan, block, project, owner_user_id


def _group_rows(
    alignment: TranslationAlignmentRevision, *, session: Any
) -> list[TranslationAlignmentGroup]:
    return list(
        session.query(TranslationAlignmentGroup)
        .populate_existing()
        .filter_by(alignment_revision_id=alignment.id)
        .order_by(TranslationAlignmentGroup.order_index)
        .all()
    )


def _verified_pair(
    record: TranslationSourcePlan,
    block_id: str,
    *,
    session: Any,
) -> tuple[Any, Any, Any, Any, Any, Any, Any, TranslationProject, int]:
    """Load one active target/alignment pair and verify every parent witness."""

    plan, block, project, owner_user_id = _plan_and_block(
        record, block_id, session=session
    )
    pointer = session.get(
        TranslationTargetBlockActive,
        {"source_plan_id": record.id, "block_id": block_id},
        populate_existing=True,
    )
    alignment_pointer = session.get(
        TranslationTargetAlignmentActive,
        {"source_plan_id": record.id, "block_id": block_id},
        populate_existing=True,
    )
    if pointer is None or alignment_pointer is None:
        raise BlockContractError("memory-active-pair")
    if (
        pointer.source_plan_id != record.id
        or pointer.block_id != block_id
        or alignment_pointer.source_plan_id != record.id
        or alignment_pointer.block_id != block_id
        or alignment_pointer.revision_id != pointer.revision_id
    ):
        raise BlockContractError("memory-active-pair")
    verify_active_pair(pointer, alignment_pointer, session=session)
    revision = session.get(
        TranslationTargetBlockRevision,
        pointer.revision_id,
        populate_existing=True,
    )
    alignment = session.get(
        TranslationAlignmentRevision,
        alignment_pointer.alignment_revision_id,
        populate_existing=True,
    )
    if (
        revision is None
        or alignment is None
        or revision.source_plan_id != record.id
        or revision.block_id != block_id
        or alignment.source_plan_id != record.id
        or alignment.block_id != block_id
        or alignment.revision_id != revision.id
    ):
        raise BlockContractError("memory-active-pair")
    rehydrate_target_revision(revision, record, session=session)
    span_set = session.get(
        TranslationTargetSpanSet,
        alignment.span_set_id,
        populate_existing=True,
    )
    if (
        span_set is None
        or span_set.source_plan_id != record.id
        or span_set.block_id != block_id
        or span_set.revision_id != revision.id
    ):
        raise BlockContractError("memory-span-set-owner")
    rehydrate_target_span_set(span_set, record, session=session)
    groups = rehydrate_alignment_revision(alignment, record, session=session)
    group_rows = _group_rows(alignment, session=session)
    if len(groups) != len(group_rows) or len(groups) != alignment.group_count:
        raise BlockContractError("memory-group-cardinality")
    return (
        plan,
        block,
        pointer,
        alignment_pointer,
        revision,
        span_set,
        alignment,
        project,
        owner_user_id,
    )


def _group_evidence(
    *,
    block: Any,
    target_projection: str,
    groups: tuple[Any, ...],
    group_rows: list[TranslationAlignmentGroup],
    spans: tuple[Any, ...],
) -> list[dict[str, Any]]:
    anchors_by_id = {anchor.id: anchor for anchor in block.anchors}
    spans_by_id = {span.id: span for span in spans}
    if len(group_rows) != len(groups):
        raise BlockContractError("memory-group-cardinality")
    evidence: list[dict[str, Any]] = []
    for group_row, group in zip(group_rows, groups, strict=True):
        source_items = [anchors_by_id.get(item) for item in group.source_anchor_ids]
        target_items = [spans_by_id.get(item) for item in group.target_span_ids]
        if any(item is None for item in (*source_items, *target_items)):
            raise BlockContractError("memory-group-membership")
        source_values = [cast(Any, item) for item in source_items]
        target_values = [cast(Any, item) for item in target_items]
        source_start = source_values[0].start
        source_end = source_values[-1].end
        target_start = target_values[0].start
        target_end = target_values[-1].end
        if source_start < 0 or source_end > len(block.projection):
            raise BlockContractError("memory-source-span")
        if target_start < 0 or target_end > len(target_projection):
            raise BlockContractError("memory-target-span")
        source_entries = [
            {
                "anchor_id": item.id,
                "order": item.order,
                "start": item.start,
                "end": item.end,
                "source_hash": item.source_hash,
                "text": block.projection[item.start : item.end],
            }
            for item in source_values
        ]
        target_entries = [
            {
                "span_id": item.id,
                "order": item.order,
                "start": item.start,
                "end": item.end,
                "text_hash": item.text_hash,
                "text": target_projection[item.start : item.end],
            }
            for item in target_values
        ]
        evidence.append(
            {
                "group_id": group_row.id,
                "order": group_row.order_index,
                "source_anchor_ids": list(group.source_anchor_ids),
                "target_span_ids": list(group.target_span_ids),
                "source_start": source_start,
                "source_end": source_end,
                "target_start": target_start,
                "target_end": target_end,
                "source_text": block.projection[source_start:source_end],
                "target_text": target_projection[target_start:target_end],
                "source": source_entries,
                "target": target_entries,
                "method": group.method,
                "confidence": float(group.confidence),
                "review_state": group.review_state,
            }
        )
    return evidence


def _selected_group_evidence(
    row: TranslationMemoryEntry,
    expected_evidence: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[str]]:
    """Resolve exactly the one group witnessed by a v1 memory row."""

    persisted = row.alignment_group_ids_json
    if (
        not isinstance(persisted, list)
        or len(persisted) != 1
        or any(not isinstance(item, str) or not item for item in persisted)
        or len(set(persisted)) != len(persisted)
    ):
        raise BlockContractError("memory-group-ids")
    expected_ids = [item["group_id"] for item in expected_evidence]
    if len(set(expected_ids)) != len(expected_ids):
        raise BlockContractError("memory-group-order")
    selected_id = persisted[0]
    if selected_id not in expected_ids:
        raise BlockContractError("memory-group-ids")
    selected = expected_evidence[expected_ids.index(selected_id)]
    one_to_one = (
        len(selected["source_anchor_ids"]) == 1
        and len(selected["target_span_ids"]) == 1
    )
    expected_granularity = "sentence" if one_to_one else "block"
    if row.granularity != expected_granularity:
        raise BlockContractError("memory-group-granularity")
    return selected, [selected_id]


def _memory_timestamp(value: object, code: str) -> str:
    if not isinstance(value, datetime):
        raise BlockContractError(code)
    return utc_iso(value, timespec="microseconds")


def _row_values(row: TranslationMemoryEntry) -> dict[str, Any]:
    return {
        "contract_version": MEMORY_CONTRACT_VERSION,
        "id": row.id,
        "owner_user_id": row.owner_user_id,
        "project_id": row.project_id,
        "source_plan_id": row.source_plan_id,
        "block_id": row.block_id,
        "target_revision_id": row.target_revision_id,
        "target_span_set_id": row.target_span_set_id,
        "alignment_revision_id": row.alignment_revision_id,
        "source_plan_hash": row.source_plan_hash,
        "target_revision_hash": row.target_revision_hash,
        "target_span_set_hash": row.target_span_set_hash,
        "alignment_revision_hash": row.alignment_revision_hash,
        "active_version": row.active_version,
        "active_pair_receipt": row.active_pair_receipt,
        "target_activation_at": _memory_timestamp(
            row.target_activation_at, "memory-target-activation-at"
        ),
        "alignment_activation_at": _memory_timestamp(
            row.alignment_activation_at, "memory-alignment-activation-at"
        ),
        "source_language": row.source_language,
        "target_language": row.target_language,
        "semantic_role": row.semantic_role,
        "structure_fingerprint": row.structure_fingerprint,
        "classification": row.classification,
        "granularity": row.granularity,
        "scope_kind": row.scope_kind,
        "scope_id": row.scope_id,
        "source_lookup_digest": row.source_lookup_digest,
        "target_lookup_digest": row.target_lookup_digest,
        "derivation_key": row.derivation_key,
        "source_text": row.source_text,
        "target_text": row.target_text,
        "source_evidence": row.source_evidence_json,
        "target_evidence": row.target_evidence_json,
        "group_ids": row.alignment_group_ids_json,
        "provenance": row.provenance_json,
        "source_witness_hash": row.source_witness_hash,
        "target_witness_hash": row.target_witness_hash,
        "source_evidence_hash": row.source_evidence_hash,
        "target_evidence_hash": row.target_evidence_hash,
        "group_ids_hash": row.group_ids_hash,
        "provenance_hash": row.provenance_hash,
        "created_at": _memory_timestamp(row.created_at, "memory-created-at"),
    }


def _row_receipt(row: TranslationMemoryEntry) -> str:
    return make_receipt(purpose=_RECORD_PURPOSE, values=_row_values(row))


def _row_scope_matches(
    row: TranslationMemoryEntry,
    *,
    project: TranslationProject,
    owner_user_id: int,
    record: TranslationSourcePlan,
) -> bool:
    return (
        row.owner_user_id == owner_user_id
        and row.project_id == project.id == record.project_id
        and row.source_plan_id == record.id
        and row.scope_kind == MEMORY_SCOPE
        and (row.scope_id is None or row.scope_id == project.id)
    )


def _historical_parents(
    row: TranslationMemoryEntry,
    record: TranslationSourcePlan,
    *,
    session: Any,
) -> tuple[Any, Any, TranslationTargetBlockRevision, TranslationTargetSpanSet, TranslationAlignmentRevision, tuple[Any, ...], list[TranslationAlignmentGroup]]:
    """Rehydrate the immutable pair witnessed when the memory row was made."""

    plan, block, _project, _owner = _plan_and_block(
        record, row.block_id, session=session
    )
    revision = session.get(
        TranslationTargetBlockRevision,
        row.target_revision_id,
        populate_existing=True,
    )
    span_set = session.get(
        TranslationTargetSpanSet,
        row.target_span_set_id,
        populate_existing=True,
    )
    alignment = session.get(
        TranslationAlignmentRevision,
        row.alignment_revision_id,
        populate_existing=True,
    )
    if (
        revision is None
        or span_set is None
        or alignment is None
        or revision.source_plan_id != record.id
        or revision.block_id != row.block_id
        or revision.revision_record_hash != row.target_revision_hash
        or span_set.source_plan_id != record.id
        or span_set.block_id != row.block_id
        or span_set.revision_id != revision.id
        or span_set.record_hash != row.target_span_set_hash
        or alignment.source_plan_id != record.id
        or alignment.block_id != row.block_id
        or alignment.revision_id != revision.id
        or alignment.span_set_id != span_set.id
        or alignment.record_hash != row.alignment_revision_hash
    ):
        raise BlockContractError("memory-parent")
    if not isinstance(row.active_version, int) or row.active_version < 1:
        raise BlockContractError("memory-active-version")
    target_activation_at = _memory_timestamp(
        row.target_activation_at, "memory-target-activation-at"
    )
    alignment_activation_at = _memory_timestamp(
        row.alignment_activation_at, "memory-alignment-activation-at"
    )
    verify_receipt(
        stored=row.active_pair_receipt,
        purpose="translator-target-active-pair-v1",
        values={
            "plan": record.id,
            "block": row.block_id,
            "revision_id": revision.id,
            "revision_receipt": revision.revision_record_hash,
            "alignment_id": alignment.id,
            "alignment_receipt": alignment.record_hash,
            "version": row.active_version,
            "target_updated_at": target_activation_at,
            "alignment_updated_at": alignment_activation_at,
        },
        code="memory-active-pair-receipt",
    )
    rehydrate_target_revision(revision, record, session=session)
    spans = rehydrate_target_span_set(span_set, record, session=session)
    groups = rehydrate_alignment_revision(alignment, record, session=session)
    group_rows = _group_rows(alignment, session=session)
    if len(groups) != len(group_rows) or len(groups) != alignment.group_count:
        raise BlockContractError("memory-group-cardinality")
    return plan, block, revision, span_set, alignment, spans, group_rows


def rehydrate_translation_memory_entry(
    row: TranslationMemoryEntry,
    source_plan: TranslationSourcePlan | None = None,
    *,
    session: Any | None = None,
) -> dict[str, Any]:
    """Decrypt and fully verify one memory row and all witnessed parents."""

    active_session = _active_session(session)
    try:
        if not isinstance(row, TranslationMemoryEntry) or not row.id:
            raise BlockContractError("memory-record")
        record = source_plan or active_session.get(
            TranslationSourcePlan, row.source_plan_id
        )
        if record is None:
            raise BlockContractError("memory-plan")
        plan, _block, project, owner_user_id = _plan_and_block(
            record, row.block_id, session=active_session
        )
        if not _row_scope_matches(
            row, project=project, owner_user_id=owner_user_id, record=record
        ):
            raise BlockContractError("memory-scope")
        if row.classification not in MEMORY_CLASSIFICATIONS:
            raise BlockContractError("memory-classification")
        if (
            row.granularity not in MEMORY_GRANULARITIES
            or row.scope_kind != MEMORY_SCOPE
        ):
            raise BlockContractError("memory-scope")
        _hash(row.source_plan_hash, "memory-plan-hash")
        _hash(row.target_revision_hash, "memory-target-hash")
        _hash(row.target_span_set_hash, "memory-span-set-hash")
        _hash(row.alignment_revision_hash, "memory-alignment-hash")
        _hash(row.structure_fingerprint, "memory-structure-hash")
        if row.source_plan_hash != plan.plan_hash:
            raise BlockContractError("memory-plan-hash")
        (
            _plan,
            verified_block,
            revision,
            _span_set,
            alignment,
            spans,
            group_rows,
        ) = _historical_parents(record=record, row=row, session=active_session)
        if (
            revision.structural_fingerprint != row.structure_fingerprint
            or row.source_language != plan.source_language
            or row.target_language != revision.target_language
            or row.semantic_role != verified_block.role
        ):
            raise BlockContractError("memory-parent")
        candidate = rehydrate_target_revision(revision, record, session=active_session)
        target_projection = target_alignment_projection(verified_block, candidate)
        groups = rehydrate_alignment_revision(alignment, record, session=active_session)
        expected_evidence = _group_evidence(
            block=verified_block,
            target_projection=target_projection,
            groups=groups,
            group_rows=group_rows,
            spans=spans,
        )
        selected, group_ids = _selected_group_evidence(row, expected_evidence)
        source_evidence = (
            selected["source"]
            if row.granularity == "sentence"
            else [selected]
        )
        target_evidence = (
            selected["target"]
            if row.granularity == "sentence"
            else [selected]
        )
        expected_source_text = selected["source_text"]
        expected_target_text = selected["target_text"]
        if (
            row.source_text != expected_source_text
            or row.target_text != expected_target_text
            or canonical_json(row.source_evidence_json)
            != canonical_json(source_evidence)
            or canonical_json(row.target_evidence_json)
            != canonical_json(target_evidence)
            or canonical_json(row.alignment_group_ids_json) != canonical_json(group_ids)
            or row.source_lookup_digest
            not in _lookup_candidates(
                row.source_text,
                source_language=row.source_language,
                target_language=row.target_language,
                owner_user_id=row.owner_user_id,
                project_id=row.project_id,
                role="source",
            )
            or row.target_lookup_digest
            not in _lookup_candidates(
                row.target_text,
                source_language=row.source_language,
                target_language=row.target_language,
                owner_user_id=row.owner_user_id,
                project_id=row.project_id,
                role="target",
            )
            or row.source_witness_hash
            not in private_witness_candidates(
                row.source_text, purpose=_SOURCE_WITNESS_PURPOSE
            )
            or row.target_witness_hash
            not in private_witness_candidates(
                row.target_text, purpose=_TARGET_WITNESS_PURPOSE
            )
            or row.source_evidence_hash
            not in private_witness_candidates(
                source_evidence, purpose=_SOURCE_EVIDENCE_PURPOSE
            )
            or row.target_evidence_hash
            not in private_witness_candidates(
                target_evidence, purpose=_TARGET_EVIDENCE_PURPOSE
            )
            or row.group_ids_hash
            not in private_witness_candidates(group_ids, purpose=_GROUP_IDS_PURPOSE)
            or not isinstance(row.provenance_json, Mapping)
            or row.provenance_hash
            not in private_witness_candidates(
                row.provenance_json, purpose=_PROVENANCE_PURPOSE
            )
            or row.record_hash
            not in private_witness_candidates(_row_values(row), purpose=_RECORD_PURPOSE)
        ):
            raise BlockContractError("memory-witness")
        return {
            "id": row.id,
            "source_text": row.source_text,
            "target_text": row.target_text,
            "source_evidence": source_evidence,
            "target_evidence": target_evidence,
            "alignment_group_ids": group_ids,
            "provenance": row.provenance_json,
            "classification": row.classification,
            "granularity": row.granularity,
            "scope_kind": row.scope_kind,
            "project_id": row.project_id,
            "owner_user_id": row.owner_user_id,
            "source_language": row.source_language,
            "target_language": row.target_language,
            "semantic_role": row.semantic_role,
            "structure_fingerprint": row.structure_fingerprint,
            "source_plan_id": row.source_plan_id,
            "target_revision_id": row.target_revision_id,
            "target_span_set_id": row.target_span_set_id,
            "alignment_revision_id": row.alignment_revision_id,
        }
    except BlockContractError:
        raise
    except Exception as exc:
        raise BlockContractError("memory-corrupt") from exc


def _entry_from_group(
    *,
    record: TranslationSourcePlan,
    plan: Any,
    block: Any,
    project: TranslationProject,
    owner_user_id: int,
    pointer: TranslationTargetBlockActive,
    alignment_pointer: TranslationTargetAlignmentActive,
    revision: TranslationTargetBlockRevision,
    span_set: TranslationTargetSpanSet,
    alignment: TranslationAlignmentRevision,
    group: Mapping[str, Any],
    classification: str,
    provenance: Mapping[str, Any] | None,
) -> TranslationMemoryEntry:
    source_text = _bounded_text(group["source_text"], "memory-source-text")
    target_text = _bounded_text(group["target_text"], "memory-target-text")
    source_evidence = (
        group["source"]
        if len(group["source_anchor_ids"]) == 1 and len(group["target_span_ids"]) == 1
        else [group]
    )
    target_evidence = (
        group["target"]
        if len(group["source_anchor_ids"]) == 1 and len(group["target_span_ids"]) == 1
        else [group]
    )
    source_evidence = _json_object(source_evidence, "memory-source-evidence")
    target_evidence = _json_object(target_evidence, "memory-target-evidence")
    group_ids = [group["group_id"]]
    caller_provenance = _json_object(dict(provenance or {}), "memory-provenance")
    if not isinstance(caller_provenance, dict):
        raise BlockContractError("memory-provenance")
    derived_provenance = {
        **caller_provenance,
        "contract_version": MEMORY_CONTRACT_VERSION,
        "derivation": "verified-active-target-alignment-pair",
        "source_plan_id": record.id,
        "source_plan_hash": plan.plan_hash,
        "block_id": block.id,
        "target_revision_id": revision.id,
        "target_span_set_id": span_set.id,
        "alignment_revision_id": alignment.id,
        "alignment_group_ids": group_ids,
        "alignment_method": group["method"],
        "alignment_confidence": group["confidence"],
        "alignment_review_state": group["review_state"],
    }
    derived_provenance = _json_object(derived_provenance, "memory-provenance")
    granularity = (
        "sentence"
        if len(group["source_anchor_ids"]) == 1 and len(group["target_span_ids"]) == 1
        else "block"
    )
    source_lookup = _lookup_digest(
        source_text,
        source_language=plan.source_language,
        target_language=revision.target_language,
        owner_user_id=owner_user_id,
        project_id=project.id,
        role="source",
    )
    target_lookup = _lookup_digest(
        target_text,
        source_language=plan.source_language,
        target_language=revision.target_language,
        owner_user_id=owner_user_id,
        project_id=project.id,
        role="target",
    )
    derivation_key = sha256_text(
        canonical_json(
            {
                "contract_version": MEMORY_CONTRACT_VERSION,
                "owner_user_id": owner_user_id,
                "project_id": project.id,
                "source_plan_id": record.id,
                "target_revision_id": revision.id,
                "target_span_set_id": span_set.id,
                "alignment_revision_id": alignment.id,
                "group_id": group_ids[0],
                "classification": classification,
                "granularity": granularity,
            }
        )
    )
    now = utc_now_db()
    row = cast(Any, TranslationMemoryEntry)(
        id=str(uuid.uuid4()),
        owner_user_id=owner_user_id,
        project_id=project.id,
        source_plan_id=record.id,
        block_id=block.id,
        target_revision_id=revision.id,
        target_span_set_id=span_set.id,
        alignment_revision_id=alignment.id,
        source_plan_hash=plan.plan_hash,
        target_revision_hash=revision.revision_record_hash,
        target_span_set_hash=span_set.record_hash,
        alignment_revision_hash=alignment.record_hash,
        active_version=pointer.version,
        active_pair_receipt=pointer.pair_receipt,
        target_activation_at=pointer.updated_at,
        alignment_activation_at=alignment_pointer.updated_at,
        source_language=plan.source_language,
        target_language=revision.target_language,
        semantic_role=block.role,
        structure_fingerprint=revision.structural_fingerprint,
        classification=classification,
        granularity=granularity,
        scope_kind=MEMORY_SCOPE,
        scope_id=project.id,
        source_lookup_digest=source_lookup,
        target_lookup_digest=target_lookup,
        derivation_key=derivation_key,
        source_witness_hash=private_witness(
            source_text, purpose=_SOURCE_WITNESS_PURPOSE
        ),
        target_witness_hash=private_witness(
            target_text, purpose=_TARGET_WITNESS_PURPOSE
        ),
        source_evidence_hash=private_witness(
            source_evidence, purpose=_SOURCE_EVIDENCE_PURPOSE
        ),
        target_evidence_hash=private_witness(
            target_evidence, purpose=_TARGET_EVIDENCE_PURPOSE
        ),
        group_ids_hash=private_witness(group_ids, purpose=_GROUP_IDS_PURPOSE),
        provenance_hash=private_witness(
            derived_provenance, purpose=_PROVENANCE_PURPOSE
        ),
        record_hash="0" * 64,
        source_text=source_text,
        target_text=target_text,
        source_evidence_json=source_evidence,
        target_evidence_json=target_evidence,
        alignment_group_ids_json=group_ids,
        provenance_json=derived_provenance,
        created_at=now,
    )
    row.record_hash = _row_receipt(row)
    return row


def derive_translation_memory(
    source_plan: TranslationSourcePlan,
    *,
    block_id: str | None = None,
    classification: str = "contextual_precedent",
    provenance: Mapping[str, Any] | None = None,
    session: Any | None = None,
) -> list[TranslationMemoryEntry]:
    """Derive immutable memory rows only from verified active Phase-4 pairs."""

    active_session = _active_session(session)
    if classification == "deliberate_variant":
        raise BlockContractError("memory-human-acceptance-required")
    if classification == "locked_exact":
        raise BlockContractError("memory-locked-exact")
    if classification != "contextual_precedent":
        raise BlockContractError("memory-classification")
    if not isinstance(source_plan, TranslationSourcePlan):
        raise BlockContractError("memory-source-plan-type")
    record = source_plan
    _require_record_session(record, session=active_session)
    plan = rehydrate_source_plan(record)
    block_ids = (
        [block_id] if block_id is not None else [block.id for block in plan.blocks]
    )
    if not block_ids or any(
        not isinstance(item, str) or not item for item in block_ids
    ):
        raise BlockContractError("memory-block-id")
    result: list[TranslationMemoryEntry] = []
    for requested_block_id in block_ids:
        pair = _verified_pair(record, requested_block_id, session=active_session)
        (
            pair_plan,
            block,
            pointer,
            alignment_pointer,
            revision,
            span_set,
            alignment,
            project,
            owner_user_id,
        ) = pair
        groups = rehydrate_alignment_revision(alignment, record, session=active_session)
        group_rows = _group_rows(alignment, session=active_session)
        if (
            alignment.review_state != "approved"
            or not groups
            or any(
                group.review_state != "approved"
                or float(group.confidence) < ALIGNMENT_CONFIDENCE_THRESHOLD
                for group in groups
            )
        ):
            continue
        candidate = rehydrate_target_revision(revision, record, session=active_session)
        target_projection = target_alignment_projection(block, candidate)
        spans = rehydrate_target_span_set(span_set, record, session=active_session)
        evidence = _group_evidence(
            block=block,
            target_projection=target_projection,
            groups=groups,
            group_rows=group_rows,
            spans=spans,
        )
        for group in evidence:
            derivation = _entry_from_group(
                record=record,
                plan=pair_plan,
                block=block,
                project=project,
                owner_user_id=owner_user_id,
                pointer=pointer,
                alignment_pointer=alignment_pointer,
                revision=revision,
                span_set=span_set,
                alignment=alignment,
                group=group,
                classification=classification,
                provenance=provenance,
            )
            existing = (
                active_session.query(TranslationMemoryEntry)
                .filter_by(
                    owner_user_id=owner_user_id,
                    project_id=project.id,
                    derivation_key=derivation.derivation_key,
                )
                .one_or_none()
            )
            if existing is not None:
                rehydrate_translation_memory_entry(
                    existing, record, session=active_session
                )
                result.append(existing)
                continue
            _ensure_database_transaction(active_session)
            try:
                with active_session.begin_nested():
                    active_session.add(derivation)
                    active_session.flush()
            except IntegrityError:
                existing = (
                    active_session.query(TranslationMemoryEntry)
                    .filter_by(
                        owner_user_id=owner_user_id,
                        project_id=project.id,
                        derivation_key=derivation.derivation_key,
                    )
                    .one_or_none()
                )
                if existing is None:
                    raise BlockContractError("memory-derivation-conflict")
                rehydrate_translation_memory_entry(
                    existing, record, session=active_session
                )
                derivation = existing
            result.append(derivation)
    return result


def lookup_translation_memory(
    *,
    owner_user_id: int,
    project_id: str,
    source_text: str,
    source_language: str,
    target_language: str,
    classification: str | None = None,
    granularity: str | None = None,
    session: Any | None = None,
) -> list[TranslationMemoryEntry]:
    """Perform owner/project/language/class constrained exact lookup."""

    active_session = _active_session(session)
    if isinstance(owner_user_id, bool) or not isinstance(owner_user_id, int):
        raise BlockContractError("memory-owner")
    if not isinstance(project_id, str) or not project_id.strip():
        raise BlockContractError("memory-project")
    if classification is not None and classification not in MEMORY_CLASSIFICATIONS:
        raise BlockContractError("memory-classification")
    if granularity is not None and granularity not in MEMORY_GRANULARITIES:
        raise BlockContractError("memory-granularity")
    project = active_session.get(TranslationProject, project_id)
    if project is None or project.user_id != owner_user_id:
        return []
    digests = _lookup_candidates(
        source_text,
        source_language=source_language,
        target_language=target_language,
        owner_user_id=owner_user_id,
        project_id=project_id,
        role="source",
    )
    query = active_session.query(TranslationMemoryEntry).filter(
        TranslationMemoryEntry.owner_user_id == owner_user_id,
        TranslationMemoryEntry.project_id == project_id,
        TranslationMemoryEntry.scope_kind == MEMORY_SCOPE,
        TranslationMemoryEntry.source_lookup_digest.in_(digests),
        TranslationMemoryEntry.source_language == source_language,
        TranslationMemoryEntry.target_language == target_language,
    )
    if classification is not None:
        query = query.filter(TranslationMemoryEntry.classification == classification)
    if granularity is not None:
        query = query.filter(TranslationMemoryEntry.granularity == granularity)
    rows = query.order_by(
        TranslationMemoryEntry.created_at, TranslationMemoryEntry.id
    ).all()
    result: list[TranslationMemoryEntry] = []
    for row in rows:
        record = active_session.get(TranslationSourcePlan, row.source_plan_id)
        if record is None:
            raise BlockContractError("memory-plan")
        rehydrate_translation_memory_entry(row, record, session=active_session)
        result.append(row)
    return result


# Explicit aliases keep the add-on boundary readable to future workers while
# retaining one implementation and one verification path.
derive_memory_entries = derive_translation_memory
rehydrate_memory_entry = rehydrate_translation_memory_entry
lookup_memory = lookup_translation_memory


__all__ = [
    "MEMORY_CLASSIFICATIONS",
    "MEMORY_CONTRACT_VERSION",
    "MEMORY_GRANULARITIES",
    "derive_memory_entries",
    "derive_translation_memory",
    "lookup_memory",
    "lookup_translation_memory",
    "rehydrate_memory_entry",
    "rehydrate_translation_memory_entry",
]
