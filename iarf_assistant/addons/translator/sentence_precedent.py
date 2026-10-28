"""Verified sentence-level precedent evidence for Translator windows.

The sentence precedent projection is deliberately small and advisory.  Source
sentence text comes from the already verified source plan, while target text is
rehydrated from the current active target/alignment pair.  No retrieval model,
provider, or persistent index is involved.
"""

from __future__ import annotations

import unicodedata
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import joinedload, selectinload

from ...extensions import db
from .alignment_contract import target_alignment_projection
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
    TranslationAlignmentRevision,
    TranslationTargetAlignmentActive,
    TranslationTargetBlockActive,
    TranslationTargetBlockRevision,
    TranslationTargetSpanSet,
)
from .models import TranslationSourcePlan as SourcePlanRecord
from .note_relations import window_blocks_are_linked
from .rehydration_cache import TranslationRehydrationCache
from .segment_context import (
    eligible_context_text,
    verified_aligned_block,
    verified_group_payload,
)
from .source_indexes import source_index
from .target_contract import structural_fingerprint
from .target_persistence import rehydrate_target_revision, verify_active_pair
from .window_contract import ContextSlot, TranslationWindow

SENTENCE_PRECEDENT_CONTRACT_VERSION = "translation-sentence-precedent-v1"
SENTENCE_PRECEDENT_V2_CONTRACT_VERSION = "translation-sentence-precedent-v2"

_MAX_QUERY_UNITS = 32
_MAX_APPROXIMATE_CANDIDATES = 64
_MAX_VERIFICATION_BLOCKS = 64
_MAX_MATCHES_PER_QUERY = 2
_MAX_MATCHES = 6
_MAX_SOURCE_SENTENCE_CHARS = 750
_MAX_TARGET_SENTENCE_CHARS = 1_200
_MAX_SLOT_CONTENT_BYTES = 8_000
_APPROXIMATE_THRESHOLD = 0.85


@dataclass(frozen=True, slots=True)
class _SentenceUnit:
    """One plan-owned sentence occurrence used by the local matcher."""

    block: TranslationBlock
    anchor: Any
    text: str
    exact_key: str
    normalized: str
    trigrams: frozenset[str]

    @property
    def identity(self) -> tuple[str, str]:
        return (self.block.id, self.anchor.id)


@dataclass(frozen=True, slots=True)
class _VerifiedMatch:
    unit: _SentenceUnit
    match_kind: str
    similarity: float
    origin: str
    alignment_review_state: str
    target_text: str


def _is_cjk(character: str) -> bool:
    codepoint = ord(character)
    return (
        0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0xF900 <= codepoint <= 0xFAFF
        or 0x20000 <= codepoint <= 0x2FA1F
    )


def normalize_sentence_match_text(value: str) -> str:
    """NFC/casefold source text for approximate matching."""

    if not isinstance(value, str):
        raise BlockContractError("sentence-precedent-source-text")
    folded = unicodedata.normalize("NFC", value).casefold()
    retained = "".join(
        character for character in folded if character.isalnum() or character.isspace()
    )
    return " ".join(retained.split())


def _exact_match_key(value: str) -> str:
    """NFC/casefold source text while preserving punctuation."""

    if not isinstance(value, str):
        raise BlockContractError("sentence-precedent-source-text")
    folded = unicodedata.normalize("NFC", value).casefold()
    return " ".join(folded.split())


def _eligible_match_text(value: str) -> bool:
    return eligible_context_text(value)


def _unique_character_trigrams(value: str) -> frozenset[str]:
    return frozenset(value[index : index + 3] for index in range(len(value) - 2))


def dice_similarity(left: str, right: str) -> float:
    """Return Dice similarity over unique character trigrams."""

    left_trigrams = _unique_character_trigrams(left)
    right_trigrams = _unique_character_trigrams(right)
    if not left_trigrams or not right_trigrams:
        return 0.0
    return (2.0 * len(left_trigrams & right_trigrams)) / (
        len(left_trigrams) + len(right_trigrams)
    )


def _window_blocks(
    plan: TranslationSourcePlan, window: TranslationWindow
) -> tuple[TranslationBlock, ...]:
    if not isinstance(plan, TranslationSourcePlan) or not isinstance(
        window, TranslationWindow
    ):
        raise BlockContractError("sentence-precedent-window")
    blocks = {block.id: block for block in plan.blocks}
    try:
        selected = tuple(blocks[block_id] for block_id in window.block_ids)
    except KeyError as exc:
        raise BlockContractError("sentence-precedent-window-owner") from exc
    if not window_blocks_are_linked(plan, window.block_ids):
        raise BlockContractError("sentence-precedent-window-adjacency")
    if tuple(structural_fingerprint(plan, block) for block in selected) != tuple(
        window.block_fingerprints
    ):
        raise BlockContractError("sentence-precedent-window-fingerprint")
    return selected


def _verified_plan(
    source_plan: SourcePlanRecord, plan: TranslationSourcePlan
) -> None:
    if (
        not isinstance(source_plan, SourcePlanRecord)
        or not isinstance(plan, TranslationSourcePlan)
        or source_plan.plan_hash != plan.plan_hash
        or source_plan.project_id != plan.project_id
        or source_plan.source_document_id != plan.snapshot.document_id
        or source_plan.source_content_hash != plan.snapshot.content_hash
    ):
        raise BlockContractError("sentence-precedent-source-plan")


def _unit(block: TranslationBlock, anchor: Any) -> _SentenceUnit | None:
    try:
        text = block.projection[anchor.start : anchor.end]
    except (AttributeError, TypeError):
        raise BlockContractError("sentence-precedent-source-anchor") from None
    if len(text) > _MAX_SOURCE_SENTENCE_CHARS:
        return None
    exact_key = _exact_match_key(text)
    normalized = normalize_sentence_match_text(text)
    if not _eligible_match_text(normalized):
        return None
    return _SentenceUnit(
        block=block,
        anchor=anchor,
        text=text,
        exact_key=exact_key,
        normalized=normalized,
        trigrams=_unique_character_trigrams(normalized),
    )


def _units(blocks: tuple[TranslationBlock, ...]) -> list[_SentenceUnit]:
    result: list[_SentenceUnit] = []
    seen: set[tuple[str, str]] = set()
    for block in blocks:
        for anchor in block.anchors:
            item = _unit(block, anchor)
            if item is None:
                continue
            if item.identity in seen:
                raise BlockContractError("sentence-precedent-source-anchor")
            seen.add(item.identity)
            result.append(item)
    return result


def _active_candidate_block_ids(source_plan: SourcePlanRecord) -> set[str]:
    """Read candidate block IDs from both active projections in one batch."""

    active_session = db.session
    target_ids = select(TranslationTargetBlockActive.block_id).where(
        TranslationTargetBlockActive.source_plan_id == source_plan.id
    )
    alignment_ids = select(TranslationTargetAlignmentActive.block_id).where(
        TranslationTargetAlignmentActive.source_plan_id == source_plan.id
    )
    with active_session.no_autoflush:
        rows = active_session.execute(target_ids.union(alignment_ids)).all()
    return {str(row[0]) for row in rows}


def _precedence_key(query: _SentenceUnit, candidate: _SentenceUnit) -> tuple[object, ...]:
    """Prefer a preceding source block, then nearest block and ordinal."""

    return (
        0 if candidate.block.order < query.block.order else 1,
        abs(candidate.block.order - query.block.order),
        candidate.block.order,
        candidate.anchor.order,
        candidate.block.id,
        candidate.anchor.id,
    )


def _candidate_matches(
    query: _SentenceUnit,
    candidates: list[_SentenceUnit],
    candidates_by_identity: Mapping[tuple[str, str], _SentenceUnit],
    trigram_index: Mapping[str, list[_SentenceUnit]],
) -> list[tuple[_SentenceUnit, str, float]]:
    exact = [
        (candidate, "exact", 1.0)
        for candidate in candidates
        if candidate.exact_key == query.exact_key
    ]
    exact_ids = {candidate.identity for candidate, _kind, _score in exact}

    shared: dict[tuple[str, str], int] = defaultdict(int)
    for trigram in query.trigrams:
        for candidate in trigram_index.get(trigram, ()):
            if candidate.identity not in exact_ids:
                shared[candidate.identity] += 1
    shortlist = [
        (
            candidates_by_identity[identity],
            count,
        )
        for identity, count in shared.items()
    ]
    shortlist.sort(key=lambda item: (-item[1], *_precedence_key(query, item[0])))
    approximate = [
        (candidate, "approximate", dice_similarity(query.normalized, candidate.normalized))
        for candidate, _shared_count in shortlist[:_MAX_APPROXIMATE_CANDIDATES]
    ]
    approximate = [
        item for item in approximate if item[2] >= _APPROXIMATE_THRESHOLD
    ]
    exact.sort(key=lambda item: _precedence_key(query, item[0]))
    approximate.sort(key=lambda item: (-item[2], *_precedence_key(query, item[0])))
    return exact + approximate


def _load_active_candidates(
    source_plan: SourcePlanRecord,
    block_ids: list[str],
) -> tuple[
    dict[str, TranslationTargetBlockActive],
    dict[str, TranslationTargetAlignmentActive],
    dict[str, TranslationTargetBlockRevision],
    dict[str, TranslationAlignmentRevision],
]:
    if not block_ids:
        return {}, {}, {}, {}
    active_session = db.session
    with active_session.no_autoflush:
        pointers = (
            active_session.query(TranslationTargetBlockActive)
            .populate_existing()
            .filter(
                TranslationTargetBlockActive.source_plan_id == source_plan.id,
                TranslationTargetBlockActive.block_id.in_(block_ids),
            )
            .all()
        )
        alignment_pointers = (
            active_session.query(TranslationTargetAlignmentActive)
            .populate_existing()
            .filter(
                TranslationTargetAlignmentActive.source_plan_id == source_plan.id,
                TranslationTargetAlignmentActive.block_id.in_(block_ids),
            )
            .all()
        )
        pointers_by_block = {item.block_id: item for item in pointers}
        alignment_pointers_by_block = {
            item.block_id: item for item in alignment_pointers
        }
        revision_ids = {item.revision_id for item in pointers}
        revisions = (
            active_session.query(TranslationTargetBlockRevision)
            .populate_existing()
            .filter(TranslationTargetBlockRevision.id.in_(revision_ids))
            .all()
            if revision_ids
            else []
        )
        revisions_by_id = {item.id: item for item in revisions}
        alignment_ids = {item.alignment_revision_id for item in alignment_pointers}
        alignments = (
            active_session.query(TranslationAlignmentRevision)
            .populate_existing()
            .options(
                selectinload(TranslationAlignmentRevision.groups),
                joinedload(TranslationAlignmentRevision.span_set).selectinload(
                    TranslationTargetSpanSet.spans
                ),
            )
            .filter(TranslationAlignmentRevision.id.in_(alignment_ids))
            .all()
            if alignment_ids
            else []
        )
    alignments_by_id = {item.id: item for item in alignments}
    return (
        pointers_by_block,
        alignment_pointers_by_block,
        revisions_by_id,
        alignments_by_id,
    )


def _verified_target_text(
    *,
    source_plan: SourcePlanRecord,
    plan: TranslationSourcePlan,
    candidate: _SentenceUnit,
    pointer: TranslationTargetBlockActive,
    alignment_pointer: TranslationTargetAlignmentActive,
    revision: TranslationTargetBlockRevision,
    alignment: TranslationAlignmentRevision,
    cache: TranslationRehydrationCache,
) -> tuple[str, str, str] | None:
    verify_active_pair(
        pointer,
        alignment_pointer,
        session=db.session,
        revision=revision,
        alignment=alignment,
    )
    target_candidate = rehydrate_target_revision(
        revision,
        source_plan,
        session=db.session,
        verified_plan=plan,
        verification_cache=cache,
    )
    span_set = alignment.span_set
    if span_set is None:
        raise BlockContractError("sentence-precedent-span-set")
    spans = rehydrate_target_span_set(
        span_set,
        source_plan,
        session=db.session,
        verified_plan=plan,
        verification_cache=cache,
    )
    groups = rehydrate_alignment_revision(
        alignment,
        source_plan,
        session=db.session,
        verified_plan=plan,
        verification_cache=cache,
    )
    source_groups = [
        group for group in groups if tuple(group.source_anchor_ids) == (candidate.anchor.id,)
    ]
    if len(source_groups) != 1:
        # More than one group for an anchor is corrupt after alignment
        # verification; a missing group means this sentence belongs to an
        # intentional many-source group or is otherwise not claimable.
        if len(source_groups) > 1:
            raise BlockContractError("sentence-precedent-alignment-group")
        return None
    group = source_groups[0]
    spans_by_id = {span.id: span for span in spans}
    projection = target_alignment_projection(candidate.block, target_candidate)
    try:
        target_text = "".join(
            projection[spans_by_id[span_id].start : spans_by_id[span_id].end]
            for span_id in group.target_span_ids
        )
    except KeyError as exc:
        raise BlockContractError("sentence-precedent-target-span") from exc
    if not target_text or len(target_text) > _MAX_TARGET_SENTENCE_CHARS:
        return None
    return target_text, revision.author_type, alignment.review_state


def _match_payload(
    query: _SentenceUnit, match: _VerifiedMatch
) -> dict[str, object]:
    candidate = match.unit
    return {
        "match_kind": match.match_kind,
        "similarity": round(match.similarity, 3),
        "origin": match.origin,
        "alignment_review_state": match.alignment_review_state,
        "source_order": candidate.block.order,
        "source_sentence_order": candidate.anchor.order,
        "source_role": candidate.block.role,
        "source_text": candidate.text,
        "target_text": match.target_text,
    }


def sentence_precedent_slot(
    source_plan: SourcePlanRecord,
    *,
    plan: TranslationSourcePlan,
    window: TranslationWindow,
) -> ContextSlot | None:
    """Build bounded, advisory sentence precedents for one writable window."""

    _verified_plan(source_plan, plan)
    selected = _window_blocks(plan, window)
    selected_ids = {block.id for block in selected}
    query_numbers = {block.id: number for number, block in enumerate(selected, 1)}
    selected_units = _units(selected)[:_MAX_QUERY_UNITS]
    if not selected_units:
        return None

    active_candidate_block_ids = _active_candidate_block_ids(source_plan)
    candidate_units = [
        item
        for item in source_index(
            plan, "sentence-units-v1", lambda: tuple(_units(plan.blocks)),
        )
        if item.block.id not in selected_ids
        and item.block.id in active_candidate_block_ids
    ]
    if not candidate_units:
        return None
    candidates_by_identity = {item.identity: item for item in candidate_units}
    trigram_index: dict[str, list[_SentenceUnit]] = defaultdict(list)
    for candidate in candidate_units:
        for trigram in candidate.trigrams:
            trigram_index[trigram].append(candidate)

    candidate_matches: dict[tuple[str, str], list[tuple[_SentenceUnit, str, float]]] = {}
    verification_block_ids: list[str] = []
    for query in selected_units:
        matches = _candidate_matches(
            query,
            candidate_units,
            candidates_by_identity,
            trigram_index,
        )
        candidate_matches[query.identity] = matches
        for candidate, _kind, _similarity in matches:
            if candidate.block.id not in verification_block_ids:
                verification_block_ids.append(candidate.block.id)
            if len(verification_block_ids) >= _MAX_VERIFICATION_BLOCKS:
                break
        if len(verification_block_ids) >= _MAX_VERIFICATION_BLOCKS:
            break
    # Keep only selected verification blocks in the already deterministic
    # order; never allow later queries to expand the database batch.
    verification_block_ids = verification_block_ids[:_MAX_VERIFICATION_BLOCKS]
    (
        pointers,
        alignment_pointers,
        revisions,
        alignments,
    ) = _load_active_candidates(source_plan, verification_block_ids)
    cache = TranslationRehydrationCache()
    verified: dict[tuple[str, str], _VerifiedMatch] = {}
    for query in selected_units:
        for candidate, kind, similarity in candidate_matches.get(query.identity, ()):
            if candidate.block.id not in verification_block_ids:
                continue
            pointer = pointers.get(candidate.block.id)
            alignment_pointer = alignment_pointers.get(candidate.block.id)
            if pointer is None and alignment_pointer is None:
                continue
            if pointer is None or alignment_pointer is None:
                raise BlockContractError("sentence-precedent-active-pair")
            revision = revisions.get(pointer.revision_id)
            alignment = alignments.get(alignment_pointer.alignment_revision_id)
            if revision is None or alignment is None:
                raise BlockContractError("sentence-precedent-active-pair")
            with db.session.no_autoflush:
                target = _verified_target_text(
                    source_plan=source_plan,
                    plan=plan,
                    candidate=candidate,
                    pointer=pointer,
                    alignment_pointer=alignment_pointer,
                    revision=revision,
                    alignment=alignment,
                    cache=cache,
                )
            if target is None:
                continue
            verified[(query.identity, candidate.identity)] = _VerifiedMatch(
                candidate,
                kind,
                similarity,
                revision.author_type,
                alignment.review_state,
                target[0],
            )

    rows: list[dict[str, object]] = []
    total_matches = 0
    used_dependency_blocks: set[str] = set()
    budget_exhausted = False
    for query in selected_units:
        possible = [
            verified[(query.identity, candidate.identity)]
            for candidate, _kind, _similarity in candidate_matches.get(query.identity, ())
            if (query.identity, candidate.identity) in verified
        ]
        possible.sort(
            key=lambda item: (
                0 if item.match_kind == "exact" else 1,
                0 if item.origin in {"human", "reviewer"} else 1,
                *_precedence_key(query, item.unit),
            )
        )
        for match in possible[:_MAX_MATCHES_PER_QUERY]:
            if total_matches >= _MAX_MATCHES:
                break
            candidate_row = _match_payload(query, match)
            proposed_rows = [
                {
                    **row,
                    "matches": list(row["matches"]),
                }
                for row in rows
            ]
            existing = next(
                (
                    row
                    for row in proposed_rows
                    if row["n"] == query_numbers[query.block.id]
                    and row["source_sentence_order"] == query.anchor.order
                ),
                None,
            )
            if existing is None:
                proposed_rows.append(
                    {
                        "n": query_numbers[query.block.id],
                        "source_sentence_order": query.anchor.order,
                        "source_role": query.block.role,
                        "source_text": query.text,
                        "matches": [candidate_row],
                    }
                )
            else:
                cast_matches = existing["matches"]
                if not isinstance(cast_matches, list):
                    raise BlockContractError("sentence-precedent-content")
                cast_matches.append(candidate_row)
            proposed_content = canonical_json(
                {
                    "contract_version": SENTENCE_PRECEDENT_CONTRACT_VERSION,
                    "guidance": (
                        "Advisory current wording only: validate context and negation; "
                        "preserve passages in other source languages; never infer note "
                        "identity from similarity or a marker."
                    ),
                    "queries": proposed_rows,
                }
            )
            if len(proposed_content.encode("utf-8")) > _MAX_SLOT_CONTENT_BYTES:
                budget_exhausted = True
                break
            rows = proposed_rows
            total_matches += 1
            used_dependency_blocks.add(match.unit.block.id)
        if total_matches >= _MAX_MATCHES:
            break
        if budget_exhausted:
            break

    if not rows:
        return None
    content = canonical_json(
        {
            "contract_version": SENTENCE_PRECEDENT_CONTRACT_VERSION,
            "guidance": (
                "Advisory current wording only: validate context and negation; "
                "preserve passages in other source languages; never infer note "
                "identity from similarity or a marker."
            ),
            "queries": rows,
        }
    )
    if len(content.encode("utf-8")) > _MAX_SLOT_CONTENT_BYTES:
        raise BlockContractError("sentence-precedent-content")
    active_targets = [
        {
            "block_id": block_id,
            "revision_id": pointers[block_id].revision_id,
            "revision_record_hash": revisions[pointers[block_id].revision_id].revision_record_hash,
            "active_pointer_version": pointers[block_id].version,
        }
        for block_id in verification_block_ids
        if block_id in pointers and block_id in alignment_pointers
    ]
    active_targets = [
        item for item in active_targets if item["block_id"] in used_dependency_blocks
    ]
    if not active_targets:
        return None
    dependency = {
        "source_plan_id": source_plan.id,
        "active_targets": active_targets,
    }
    return ContextSlot(
        "sentence_precedent",
        content,
        {
            "contract_version": SENTENCE_PRECEDENT_CONTRACT_VERSION,
            "method": "exact-or-dice-unique-character-trigrams-v1",
            "normalization": (
                "unicode-nfc-casefold-whitespace-exact-"
                "alphanumeric-trigram-v1"
            ),
        },
        f"translation-window-sentence-precedent:{window.order}",
        SENTENCE_PRECEDENT_CONTRACT_VERSION,
        sha256_text(content),
        dependency,
        False,
    )


def sentence_precedent_v2_slot(
    source_plan: SourcePlanRecord,
    *,
    plan: TranslationSourcePlan,
    window: TranslationWindow,
) -> ContextSlot | None:
    """Return complete verified alignment groups for bounded sentence matches.

    Query rows preserve each current sentence occurrence. Repeated candidates
    that resolve to the same active alignment group share one group witness,
    while their query references retain the matched source sentence and score.
    """

    _verified_plan(source_plan, plan)
    selected = _window_blocks(plan, window)
    selected_ids = {block.id for block in selected}
    query_numbers = {block.id: number for number, block in enumerate(selected, 1)}
    selected_units = _units(selected)[:_MAX_QUERY_UNITS]
    if not selected_units:
        return None

    active_candidate_block_ids = _active_candidate_block_ids(source_plan)
    candidate_units = [
        item
        for item in source_index(
            plan,
            "sentence-units-v1",
            lambda: tuple(_units(plan.blocks)),
        )
        if item.block.id not in selected_ids
        and item.block.id in active_candidate_block_ids
    ]
    if not candidate_units:
        return None
    candidates_by_identity = {item.identity: item for item in candidate_units}
    trigram_index: dict[str, list[_SentenceUnit]] = defaultdict(list)
    for candidate in candidate_units:
        for trigram in candidate.trigrams:
            trigram_index[trigram].append(candidate)

    candidate_matches: dict[
        tuple[str, str], list[tuple[_SentenceUnit, str, float]]
    ] = {}
    verification_block_ids: list[str] = []
    verification_truncated = False
    for query in selected_units:
        matches = _candidate_matches(
            query,
            candidate_units,
            candidates_by_identity,
            trigram_index,
        )
        candidate_matches[query.identity] = matches
        for candidate, _kind, _similarity in matches:
            if candidate.block.id not in verification_block_ids:
                if len(verification_block_ids) >= _MAX_VERIFICATION_BLOCKS:
                    verification_truncated = True
                    break
                verification_block_ids.append(candidate.block.id)
        if verification_truncated:
            break
    verification_block_ids = verification_block_ids[:_MAX_VERIFICATION_BLOCKS]
    pointers, alignment_pointers, _revisions, _alignments = _load_active_candidates(
        source_plan, verification_block_ids
    )

    payload: dict[str, object] = {
        "contract_version": SENTENCE_PRECEDENT_V2_CONTRACT_VERSION,
        "guidance": (
            "Advisory wording only. A match score applies to matched_source_text; "
            "the complete source and target arrays describe one alignment group, "
            "not sentence-by-sentence pairings. Validate context and negation."
        ),
        "queries": [],
        "matches": [],
        "truncated": len(_units(selected)) > len(selected_units)
        or verification_truncated,
    }
    query_rows: list[dict[str, object]] = []
    group_rows: list[dict[str, object]] = []
    group_ids: dict[tuple[str, str, str, str], str] = {}
    query_row_indexes: dict[tuple[str, str], int] = {}
    query_seen_groups: dict[tuple[str, str], set[tuple[str, str, str, str]]] = {}
    verified_blocks: dict[str, Any] = {}
    verification_cache = TranslationRehydrationCache()
    used_dependency_blocks: set[str] = set()
    total_matches = 0
    budget_exhausted = False
    match_limit_reached = False

    for query in selected_units:
        query_key = query.identity
        query_seen_groups[query_key] = set()
        possible_candidates = candidate_matches.get(query_key, ())
        query_limit_truncated = False
        for candidate_index, (candidate, raw_kind, similarity) in enumerate(
            possible_candidates
        ):
            if total_matches >= _MAX_MATCHES:
                match_limit_reached = True
                break
            if candidate.block.id not in verification_block_ids:
                continue
            pointer = pointers.get(candidate.block.id)
            alignment_pointer = alignment_pointers.get(candidate.block.id)
            if pointer is None and alignment_pointer is None:
                continue
            if pointer is None or alignment_pointer is None:
                raise BlockContractError("sentence-precedent-active-pair")
            verified = verified_blocks.get(candidate.block.id)
            if verified is None:
                with db.session.no_autoflush:
                    verified = verified_aligned_block(
                        source_plan,
                        plan,
                        candidate.block,
                        pointer=pointer,
                        alignment_pointer=alignment_pointer,
                        verification_cache=verification_cache,
                    )
                verified_blocks[candidate.block.id] = verified
            source_group_indexes = [
                index
                for index, group in enumerate(verified.groups)
                if candidate.anchor.id in group.source_anchor_ids
            ]
            if len(source_group_indexes) > 1:
                raise BlockContractError("sentence-precedent-alignment-group")
            if not source_group_indexes:
                continue
            group_index = source_group_indexes[0]
            group_row = verified.group_rows[group_index]
            group_key = (
                candidate.block.id,
                verified.revision.id,
                verified.alignment.id,
                group_row.id,
            )
            if group_key in query_seen_groups[query_key]:
                continue
            query_row_index = query_row_indexes.get(query_key)
            existing_group_id = group_ids.get(group_key)
            new_group = existing_group_id is None
            if new_group and len(group_rows) >= _MAX_MATCHES:
                match_limit_reached = True
                break
            group_id = existing_group_id or group_row.id
            kind = "exact" if raw_kind == "exact" else "lexical_fuzzy"
            match_reference: dict[str, object] = {
                "group_id": group_id,
                "match_kind": kind,
                "similarity": round(similarity, 3),
                "matched_source_text": candidate.text,
                "matched_source_order": candidate.anchor.order,
                "matched_source_anchor_id": candidate.anchor.id,
            }
            proposed_queries = [
                {**row, "matches": list(row["matches"])} for row in query_rows
            ]
            proposed_query_index = query_row_index
            if proposed_query_index is None:
                proposed_query_index = len(proposed_queries)
                proposed_queries.append(
                    {
                        "n": query_numbers[query.block.id],
                        "source_order": query.block.order,
                        "source_sentence_order": query.anchor.order,
                        "source_role": query.block.role,
                        "source_text": query.text,
                        "matches": [match_reference],
                    }
                )
            else:
                proposed_queries[proposed_query_index]["matches"].append(
                    match_reference
                )
            proposed_groups = list(group_rows)
            group_payload: dict[str, object] | None = None
            if new_group:
                group_payload = {
                    **verified_group_payload(verified, group_index),
                    "block_id": candidate.block.id,
                    "source_order": candidate.block.order,
                    "origin": verified.revision.author_type,
                    "alignment_review_state": verified.alignment.review_state,
                    "revision_id": verified.revision.id,
                    "revision_record_hash": verified.revision.revision_record_hash,
                    "active_pointer_version": verified.pointer.version,
                    "alignment_revision_id": verified.alignment.id,
                    "alignment_record_hash": verified.alignment.record_hash,
                    "alignment_pointer_version": verified.alignment_pointer.version,
                    "span_set_id": verified.span_set.id,
                    "span_set_record_hash": verified.span_set.record_hash,
                }
                proposed_groups.append(group_payload)
            proposed_payload = {
                **payload,
                "queries": proposed_queries,
                "matches": proposed_groups,
            }
            proposed_content = canonical_json(proposed_payload)
            if len(proposed_content.encode("utf-8")) > _MAX_SLOT_CONTENT_BYTES - 1:
                budget_exhausted = True
                break
            query_rows = proposed_queries
            group_rows = proposed_groups
            query_row_indexes[query_key] = proposed_query_index
            query_seen_groups[query_key].add(group_key)
            if new_group:
                group_ids[group_key] = group_id
            used_dependency_blocks.add(candidate.block.id)
            total_matches += 1
            if len(query_rows[proposed_query_index]["matches"]) >= _MAX_MATCHES_PER_QUERY:
                query_limit_truncated = candidate_index < len(possible_candidates) - 1
                break
        if query_limit_truncated:
            payload["truncated"] = True
        if budget_exhausted or match_limit_reached:
            break

    if not query_rows:
        return None
    if budget_exhausted or match_limit_reached:
        payload["truncated"] = True
    final_payload = {
        **payload,
        "queries": query_rows,
        "matches": group_rows,
    }
    content = canonical_json(final_payload)
    if len(content.encode("utf-8")) > _MAX_SLOT_CONTENT_BYTES:
        raise BlockContractError("sentence-precedent-v2-content")
    if not used_dependency_blocks or total_matches > _MAX_MATCHES:
        raise BlockContractError("sentence-precedent-v2-content")
    active_targets = [
        {
            "block_id": block_id,
            "revision_id": pointers[block_id].revision_id,
            "revision_record_hash": verified_blocks[
                block_id
            ].revision.revision_record_hash,
            "active_pointer_version": pointers[block_id].version,
        }
        for block_id in sorted(used_dependency_blocks)
    ]
    return ContextSlot(
        "sentence_precedent",
        content,
        {
            "contract_version": SENTENCE_PRECEDENT_V2_CONTRACT_VERSION,
            "method": "exact-or-dice-unique-character-trigrams-v2",
            "normalization": (
                "unicode-nfc-casefold-whitespace-exact-"
                "alphanumeric-trigram-v1"
            ),
        },
        f"translation-window-sentence-precedent:{window.order}",
        SENTENCE_PRECEDENT_V2_CONTRACT_VERSION,
        sha256_text(content),
        {"source_plan_id": source_plan.id, "active_targets": active_targets},
        False,
    )


__all__ = [
    "SENTENCE_PRECEDENT_CONTRACT_VERSION",
    "SENTENCE_PRECEDENT_V2_CONTRACT_VERSION",
    "dice_similarity",
    "normalize_sentence_match_text",
    "sentence_precedent_slot",
    "sentence_precedent_v2_slot",
]
