"""Bounded, source-aligned context from verified Translator target pairs.

The helpers in this module read one selected active target/alignment pair at a
time. They never assemble target text for an entire source plan.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ...extensions import db
from .alignment_contract import AlignmentGroup, TargetSpan, target_alignment_projection
from .alignment_persistence import (
    rehydrate_alignment_revision,
    rehydrate_target_span_set,
)
from .block_contract import (
    BlockContractError,
    TranslationBlock,
    TranslationSourcePlan,
    canonical_json,
    sha256_text,
)
from .models import (
    TranslationAlignmentGroup,
    TranslationAlignmentRevision,
    TranslationTargetAlignmentActive,
    TranslationTargetBlockActive,
    TranslationTargetBlockRevision,
    TranslationTargetSpan,
    TranslationTargetSpanSet,
)
from .models import (
    TranslationSourcePlan as SourcePlanRecord,
)
from .note_relations import window_blocks_are_linked
from .rehydration_cache import TranslationRehydrationCache
from .target_contract import structural_fingerprint
from .target_persistence import rehydrate_target_revision, verify_active_pair
from .window_contract import ContextSlot, TranslationWindow

SEGMENT_CONTEXT_CONTRACT_VERSION = "translation-segment-context-v2"
SEGMENT_CONTINUITY_CONTRACT_VERSION = "translation-segment-continuity-v2"
_MAX_CONTEXT_BYTES = 6_000
_MAX_CONTEXT_GROUPS = 3
_PROSE_ROLES = frozenset(
    {
        "paragraph",
        "direct_prose",
        "list_item",
        "blockquote",
        "definition_body",
        "table_caption",
        "figure_caption",
    }
)


@dataclass(frozen=True, slots=True)
class VerifiedAlignedBlock:
    """One rehydrated target/alignment pair and its exact plan projection."""

    block: TranslationBlock
    pointer: TranslationTargetBlockActive
    alignment_pointer: TranslationTargetAlignmentActive
    revision: TranslationTargetBlockRevision
    alignment: TranslationAlignmentRevision
    candidate: Any
    span_set: TranslationTargetSpanSet
    spans: tuple[TargetSpan, ...]
    span_rows: tuple[TranslationTargetSpan, ...]
    groups: tuple[AlignmentGroup, ...]
    group_rows: tuple[TranslationAlignmentGroup, ...]
    target_projection: str


def normalize_context_text(value: str) -> str:
    """Normalize editorial text for the shared precedent eligibility rule."""

    if not isinstance(value, str):
        raise BlockContractError("segment-context-source-text")
    folded = unicodedata.normalize("NFC", value).casefold()
    retained = "".join(
        character for character in folded if character.isalnum() or character.isspace()
    )
    return " ".join(retained.split())


def eligible_context_text(normalized: str) -> bool:
    """Apply the sentence-precedent minimum-text rule to normalized text."""

    if not isinstance(normalized, str) or not normalized:
        return False
    alphanumeric = [character for character in normalized if character.isalnum()]
    if not any(not character.isdigit() for character in alphanumeric):
        return False
    if len(alphanumeric) >= 16:
        return True
    return len(alphanumeric) >= 8 and any(_is_cjk(character) for character in alphanumeric)


def _is_cjk(character: str) -> bool:
    codepoint = ord(character)
    return (
        0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0xF900 <= codepoint <= 0xFAFF
        or 0x20000 <= codepoint <= 0x2FA1F
    )


def _verify_plan_record(
    source_plan_record: SourcePlanRecord,
    plan: TranslationSourcePlan,
) -> None:
    if (
        not isinstance(source_plan_record, SourcePlanRecord)
        or not isinstance(plan, TranslationSourcePlan)
        or source_plan_record.plan_hash != plan.plan_hash
        or source_plan_record.project_id != plan.project_id
        or source_plan_record.source_document_id != plan.snapshot.document_id
        or source_plan_record.source_content_hash != plan.snapshot.content_hash
    ):
        raise BlockContractError("segment-context-source-plan")


def require_window_blocks(
    plan: TranslationSourcePlan,
    window: TranslationWindow,
) -> tuple[TranslationBlock, ...]:
    """Validate the exact selected window against its immutable source plan."""

    if not isinstance(plan, TranslationSourcePlan) or not isinstance(
        window, TranslationWindow
    ):
        raise BlockContractError("segment-context-window")
    blocks_by_id = {block.id: block for block in plan.blocks}
    try:
        selected = tuple(blocks_by_id[block_id] for block_id in window.block_ids)
    except KeyError as exc:
        raise BlockContractError("segment-context-window-owner") from exc
    if not window_blocks_are_linked(plan, window.block_ids):
        raise BlockContractError("segment-context-window-adjacency")
    if tuple(structural_fingerprint(plan, block) for block in selected) != tuple(
        window.block_fingerprints
    ):
        raise BlockContractError("segment-context-window-fingerprint")
    return selected


def verified_aligned_block(
    source_plan_record: SourcePlanRecord,
    plan: TranslationSourcePlan,
    block: TranslationBlock,
    *,
    pointer: TranslationTargetBlockActive | None = None,
    alignment_pointer: TranslationTargetAlignmentActive | None = None,
    verification_cache: TranslationRehydrationCache | None = None,
) -> VerifiedAlignedBlock:
    """Rehydrate and verify exactly one active target/alignment pair."""

    _verify_plan_record(source_plan_record, plan)
    if block not in plan.blocks:
        raise BlockContractError("segment-context-block-owner")
    active_session = db.session
    if pointer is None:
        pointer = active_session.get(
            TranslationTargetBlockActive,
            {"source_plan_id": source_plan_record.id, "block_id": block.id},
            populate_existing=True,
        )
    if alignment_pointer is None:
        alignment_pointer = active_session.get(
            TranslationTargetAlignmentActive,
            {"source_plan_id": source_plan_record.id, "block_id": block.id},
            populate_existing=True,
        )
    if pointer is None or alignment_pointer is None:
        raise BlockContractError("segment-context-active-pair")
    revision = active_session.get(
        TranslationTargetBlockRevision,
        pointer.revision_id,
        populate_existing=True,
    )
    alignment = active_session.get(
        TranslationAlignmentRevision,
        alignment_pointer.alignment_revision_id,
        populate_existing=True,
    )
    if revision is None or alignment is None:
        raise BlockContractError("segment-context-active-pair")
    verify_active_pair(
        pointer,
        alignment_pointer,
        session=active_session,
        revision=revision,
        alignment=alignment,
    )
    cache = verification_cache or TranslationRehydrationCache()
    candidate = rehydrate_target_revision(
        revision,
        source_plan_record,
        session=active_session,
        verified_plan=plan,
        verification_cache=cache,
    )
    span_set = active_session.get(
        TranslationTargetSpanSet,
        alignment.span_set_id,
        populate_existing=True,
    )
    if span_set is None:
        raise BlockContractError("segment-context-span-set")
    spans = rehydrate_target_span_set(
        span_set,
        source_plan_record,
        session=active_session,
        verified_plan=plan,
        verification_cache=cache,
    )
    groups = rehydrate_alignment_revision(
        alignment,
        source_plan_record,
        session=active_session,
        verified_plan=plan,
        verification_cache=cache,
    )
    group_rows = tuple(
        active_session.query(TranslationAlignmentGroup)
        .populate_existing()
        .filter(TranslationAlignmentGroup.alignment_revision_id == alignment.id)
        .order_by(TranslationAlignmentGroup.order_index)
        .all()
    )
    span_rows = tuple(
        active_session.query(TranslationTargetSpan)
        .populate_existing()
        .filter(TranslationTargetSpan.span_set_id == span_set.id)
        .order_by(TranslationTargetSpan.order_index)
        .all()
    )
    if len(groups) != len(group_rows) or len(spans) != len(span_rows):
        raise BlockContractError("segment-context-alignment-groups")
    return VerifiedAlignedBlock(
        block=block,
        pointer=pointer,
        alignment_pointer=alignment_pointer,
        revision=revision,
        alignment=alignment,
        candidate=candidate,
        span_set=span_set,
        spans=spans,
        span_rows=span_rows,
        groups=groups,
        group_rows=group_rows,
        target_projection=target_alignment_projection(block, candidate),
    )


def _predecessor_candidates(
    plan: TranslationSourcePlan,
    before_block: TranslationBlock,
) -> tuple[TranslationBlock, ...]:
    note_mode = before_block.role == "note_body"
    eligible_roles = _PROSE_ROLES | ({"note_body"} if note_mode else set())
    return tuple(
        block
        for block in plan.blocks
        if block.order < before_block.order
        and block.role in eligible_roles
        and eligible_context_text(normalize_context_text(block.projection))
    )


def _anchor_payload(block: TranslationBlock, anchor_id: str) -> dict[str, object]:
    anchor = next((item for item in block.anchors if item.id == anchor_id), None)
    if anchor is None:
        raise BlockContractError("segment-context-source-anchor")
    return {
        "id": anchor.id,
        "order": anchor.order,
        "start": anchor.start,
        "end": anchor.end,
        "source_hash": anchor.source_hash,
        "splitter_profile": anchor.splitter_profile,
        "splitter_version": anchor.splitter_version,
        "text": block.projection[anchor.start : anchor.end],
    }


def verified_group_payload(
    verified: VerifiedAlignedBlock,
    group_index: int,
) -> dict[str, object]:
    """Return one complete verified group without inventing pairwise links."""

    if (
        not isinstance(group_index, int)
        or isinstance(group_index, bool)
        or group_index < 0
        or group_index >= len(verified.groups)
    ):
        raise BlockContractError("segment-context-alignment-group")
    group = verified.groups[group_index]
    group_row = verified.group_rows[group_index]
    spans_by_id = {span.id: span for span in verified.spans}
    span_rows_by_id = {row.id: row for row in verified.span_rows}
    anchors_by_id = {anchor.id: anchor for anchor in verified.block.anchors}
    source_sentences = [
        _anchor_payload(verified.block, anchor_id)
        for anchor_id in group.source_anchor_ids
    ]
    target_spans: list[dict[str, object]] = []
    for span_id in group.target_span_ids:
        span = spans_by_id.get(span_id)
        span_row = span_rows_by_id.get(span_id)
        if span is None or span_row is None:
            raise BlockContractError("segment-context-target-span")
        target_spans.append(
            {
                "id": span.id,
                "order": span.order,
                "start": span.start,
                "end": span.end,
                "text_hash": span.text_hash,
                "span_hash": span_row.span_hash,
                "text": verified.target_projection[span.start : span.end],
            }
        )
    return {
        "id": group_row.id,
        "order": group_index + 1,
        "group_hash": group_row.group_hash,
        "method": group.method,
        "confidence": float(group.confidence),
        "review_state": group.review_state,
        "source_sentence_orders": [
            anchors_by_id[anchor_id].order for anchor_id in group.source_anchor_ids
        ],
        "target_span_orders": [
            spans_by_id[span_id].order for span_id in group.target_span_ids
        ],
        "source_sentences": source_sentences,
        "target_spans": target_spans,
    }


def _predecessor_payload(verified: VerifiedAlignedBlock) -> dict[str, object]:
    return {
        "block_id": verified.block.id,
        "source_order": verified.block.order,
        "source_role": verified.block.role,
        "revision_id": verified.revision.id,
        "revision_record_hash": verified.revision.revision_record_hash,
        "active_pointer_version": verified.pointer.version,
        "alignment_revision_id": verified.alignment.id,
        "alignment_record_hash": verified.alignment.record_hash,
        "alignment_pointer_version": verified.alignment_pointer.version,
        "span_set_id": verified.span_set.id,
        "span_set_record_hash": verified.span_set.record_hash,
    }


def _base_payload(
    source_plan_record: SourcePlanRecord,
    plan: TranslationSourcePlan,
    before_block: TranslationBlock,
) -> dict[str, object]:
    return {
        "contract_version": SEGMENT_CONTEXT_CONTRACT_VERSION,
        "source_plan_id": source_plan_record.id,
        "source_plan_hash": plan.plan_hash,
        "before_block_id": before_block.id,
        "available": False,
        "reason": None,
        "predecessor": None,
        "groups": [],
        "truncated": False,
        "omitted_group_count": 0,
    }


def _unavailable(
    payload: dict[str, object], reason: str, *, truncated: bool = False
) -> dict[str, object]:
    result = dict(payload)
    result["reason"] = reason
    result["truncated"] = truncated
    if len(canonical_json(result).encode("utf-8")) > _MAX_CONTEXT_BYTES:
        raise BlockContractError("segment-context-result-size")
    return result


def context_before_block(
    source_plan_record: SourcePlanRecord,
    *,
    plan: TranslationSourcePlan,
    before_block_id: str,
    max_segments: int = 3,
) -> dict[str, object]:
    """Return a bounded tail from the latest earlier translated prose block."""

    _verify_plan_record(source_plan_record, plan)
    if (
        not isinstance(before_block_id, str)
        or not before_block_id
        or not isinstance(max_segments, int)
        or isinstance(max_segments, bool)
        or not 1 <= max_segments <= _MAX_CONTEXT_GROUPS
    ):
        raise BlockContractError("segment-context-request")
    before_block = next(
        (block for block in plan.blocks if block.id == before_block_id), None
    )
    if before_block is None:
        raise BlockContractError("segment-context-before-block")
    payload = _base_payload(source_plan_record, plan, before_block)
    candidates = _predecessor_candidates(plan, before_block)
    if not candidates:
        return _unavailable(payload, "no_relevant_predecessor")

    candidate_ids = tuple(block.id for block in candidates)
    active_session = db.session
    with active_session.no_autoflush:
        target_rows = (
            active_session.query(TranslationTargetBlockActive)
            .populate_existing()
            .filter(
                TranslationTargetBlockActive.source_plan_id == source_plan_record.id,
                TranslationTargetBlockActive.block_id.in_(candidate_ids),
            )
            .all()
        )
        alignment_rows = (
            active_session.query(TranslationTargetAlignmentActive)
            .populate_existing()
            .filter(
                TranslationTargetAlignmentActive.source_plan_id == source_plan_record.id,
                TranslationTargetAlignmentActive.block_id.in_(candidate_ids),
            )
            .all()
        )
    targets = {row.block_id: row for row in target_rows}
    alignments = {row.block_id: row for row in alignment_rows}
    selected_pair: tuple[TranslationBlock, TranslationTargetBlockActive, TranslationTargetAlignmentActive] | None = None
    for candidate in reversed(candidates):
        pointer = targets.get(candidate.id)
        alignment_pointer = alignments.get(candidate.id)
        if pointer is None and alignment_pointer is None:
            continue
        if pointer is None or alignment_pointer is None:
            raise BlockContractError("segment-context-active-pair")
        selected_pair = (candidate, pointer, alignment_pointer)
        break
    if selected_pair is None:
        return _unavailable(payload, "no_translated_relevant_predecessor")

    candidate, pointer, alignment_pointer = selected_pair
    verified = verified_aligned_block(
        source_plan_record,
        plan,
        candidate,
        pointer=pointer,
        alignment_pointer=alignment_pointer,
        verification_cache=TranslationRehydrationCache(),
    )
    if len(verified.groups) != len(verified.group_rows) or not verified.groups:
        raise BlockContractError("segment-context-alignment-groups")

    start = max(0, len(verified.groups) - max_segments)
    selected_indexes = list(range(start, len(verified.groups)))
    omitted = start
    base = dict(payload)
    base["predecessor"] = _predecessor_payload(verified)
    base["truncated"] = omitted > 0
    base["omitted_group_count"] = omitted
    while selected_indexes:
        result = dict(base)
        result["available"] = True
        result["reason"] = None
        result["groups"] = [
            verified_group_payload(verified, index)
            for index in selected_indexes
        ]
        if len(canonical_json(result).encode("utf-8")) <= _MAX_CONTEXT_BYTES:
            return result
        if len(selected_indexes) == 1:
            break
        selected_indexes.pop(0)
        omitted += 1
        base["truncated"] = True
        base["omitted_group_count"] = omitted

    return _unavailable(
        {
            **base,
            "predecessor": _predecessor_payload(verified),
            "groups": [],
            "omitted_group_count": len(verified.groups),
        },
        "last_alignment_group_exceeds_budget",
        truncated=True,
    )


def segment_continuity_slot(
    source_plan_record: SourcePlanRecord,
    *,
    plan: TranslationSourcePlan,
    window: TranslationWindow,
    mode: str = "self_contained",
) -> ContextSlot | None:
    """Build an optional bounded continuity slot or omit it for retained mode."""

    _verify_plan_record(source_plan_record, plan)
    selected = require_window_blocks(plan, window)
    if mode not in {"self_contained", "retained"}:
        raise BlockContractError("segment-continuity-mode")
    if mode == "retained":
        return None
    payload = context_before_block(
        source_plan_record,
        plan=plan,
        before_block_id=selected[0].id,
    )
    predecessor = payload.get("predecessor")
    if not isinstance(predecessor, Mapping):
        return None
    content = canonical_json(payload)
    if len(content.encode("utf-8")) > _MAX_CONTEXT_BYTES:
        raise BlockContractError("segment-context-result-size")
    return ContextSlot(
        slot_type="continuity",
        content=content,
        provenance={"contract_version": SEGMENT_CONTINUITY_CONTRACT_VERSION},
        artifact_id=f"translation-window-continuity:{window.order}",
        artifact_version=SEGMENT_CONTINUITY_CONTRACT_VERSION,
        artifact_hash=sha256_text(content),
        dependency={
            "predecessor_block_id": str(predecessor["block_id"]),
            "revision_id": str(predecessor["revision_id"]),
            "revision_record_hash": str(predecessor["revision_record_hash"]),
            "active_pointer_version": int(predecessor["active_pointer_version"]),
        },
        required=False,
    )


__all__ = [
    "SEGMENT_CONTEXT_CONTRACT_VERSION",
    "SEGMENT_CONTINUITY_CONTRACT_VERSION",
    "context_before_block",
    "eligible_context_text",
    "normalize_context_text",
    "require_window_blocks",
    "segment_continuity_slot",
    "verified_aligned_block",
    "verified_group_payload",
]
