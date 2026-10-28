"""Bounded, advisory context for source notes linked to a translation window.

The source relationship index is the authority for note identity.  This module
only projects those direct relationships into a small context slot; it never
walks note graphs, follows displayed labels, or mutates the source plan.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, cast

from sqlalchemy.orm import joinedload, selectinload

from ...extensions import db
from .alignment_contract import target_alignment_projection
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
from .models import TranslationSourcePlan as TranslationSourcePlanRecord
from .note_relations import (
    NOTE_RELATION_REASONS,
    NoteRelations,
    build_note_relations,
    window_blocks_are_linked,
)
from .rehydration_cache import TranslationRehydrationCache
from .target_contract import structural_fingerprint
from .target_persistence import rehydrate_target_revision, verify_active_pair
from .window_contract import ContextSlot, TranslationWindow

LINKED_NOTES_CONTRACT_VERSION = "translation-linked-notes-v1"
_MAX_RELATIONS = 12
_MAX_OUTSIDE_BLOCKS = 12
_MAX_UNRESOLVED = 6
_MAX_SOURCE_CHARS = 1_200
_MAX_TARGET_CHARS = 1_200
_MAX_SLOT_CONTENT_BYTES = 8_000


def _window_blocks(
    plan: TranslationSourcePlan, window: TranslationWindow
) -> tuple[TranslationBlock, ...]:
    if not isinstance(plan, TranslationSourcePlan) or not isinstance(
        window, TranslationWindow
    ):
        raise BlockContractError("linked-notes-window")
    blocks_by_id = {block.id: block for block in plan.blocks}
    try:
        selected = tuple(blocks_by_id[block_id] for block_id in window.block_ids)
    except KeyError as exc:
        raise BlockContractError("linked-notes-window-owner") from exc
    if not window_blocks_are_linked(plan, window.block_ids):
        raise BlockContractError("linked-notes-window-adjacency")
    expected = tuple(structural_fingerprint(plan, block) for block in selected)
    if expected != tuple(window.block_fingerprints):
        raise BlockContractError("linked-notes-window-fingerprint")
    return selected


def _verify_source_plan(
    source_plan_id: str,
    source_plan: TranslationSourcePlanRecord | None,
    plan: TranslationSourcePlan,
) -> None:
    if not isinstance(source_plan_id, str) or not source_plan_id.strip():
        raise BlockContractError("linked-notes-source-plan")
    if source_plan is None:
        return
    if (
        not isinstance(source_plan, TranslationSourcePlanRecord)
        or source_plan.id != source_plan_id
        or source_plan.project_id != plan.project_id
        or source_plan.plan_hash != plan.plan_hash
        or source_plan.source_document_id != plan.snapshot.document_id
        or source_plan.source_content_hash != plan.snapshot.content_hash
    ):
        raise BlockContractError("linked-notes-source-plan")


def _block_key(block: TranslationBlock) -> tuple[int, int, str]:
    return (block.order, block.source_start, block.id)


def _note_key(target_id: str, blocks: Sequence[TranslationBlock]) -> tuple[Any, ...]:
    first = min(blocks, key=_block_key)
    return (first.order, first.source_start, target_id)


def _clip(value: str, limit: int) -> tuple[str, bool]:
    if len(value) <= limit:
        return value, False
    return value[:limit], True


def _active_context(
    source_plan: TranslationSourcePlanRecord,
    plan: TranslationSourcePlan,
    block_ids: Sequence[str],
) -> tuple[dict[str, str], dict[str, dict[str, object]]]:
    """Batch verify and rehydrate current target text for outside blocks."""

    if not block_ids:
        return {}, {}
    session = db.session
    with session.no_autoflush:
        pointers = (
            session.query(TranslationTargetBlockActive)
            .populate_existing()
            .filter(
                TranslationTargetBlockActive.source_plan_id == source_plan.id,
                TranslationTargetBlockActive.block_id.in_(tuple(block_ids)),
            )
            .all()
        )
        alignment_pointers = (
            session.query(TranslationTargetAlignmentActive)
            .populate_existing()
            .filter(
                TranslationTargetAlignmentActive.source_plan_id == source_plan.id,
                TranslationTargetAlignmentActive.block_id.in_(tuple(block_ids)),
            )
            .all()
        )
        pointers_by_block = {item.block_id: item for item in pointers}
        alignment_by_block = {item.block_id: item for item in alignment_pointers}
        revision_ids = {item.revision_id for item in pointers}
        revisions = (
            session.query(TranslationTargetBlockRevision)
            .populate_existing()
            .filter(TranslationTargetBlockRevision.id.in_(revision_ids))
            .all()
            if revision_ids
            else []
        )
        revisions_by_id = {item.id: item for item in revisions}
        alignment_ids = {item.alignment_revision_id for item in alignment_pointers}
        alignments = (
            session.query(TranslationAlignmentRevision)
            .populate_existing()
            .options(
                selectinload(cast(Any, TranslationAlignmentRevision.groups)),
                joinedload(cast(Any, TranslationAlignmentRevision.span_set)).selectinload(
                    cast(Any, TranslationTargetSpanSet.spans)
                ),
            )
            .filter(TranslationAlignmentRevision.id.in_(alignment_ids))
            .all()
            if alignment_ids
            else []
        )
    alignments_by_id = {item.id: item for item in alignments}

    target_text: dict[str, str] = {}
    dependencies: dict[str, dict[str, object]] = {}
    cache = TranslationRehydrationCache()
    for block_id in block_ids:
        pointer = pointers_by_block.get(block_id)
        alignment_pointer = alignment_by_block.get(block_id)
        if pointer is None and alignment_pointer is None:
            continue
        if pointer is None or alignment_pointer is None:
            raise BlockContractError("linked-notes-active-pair")
        revision = revisions_by_id.get(pointer.revision_id)
        alignment = alignments_by_id.get(alignment_pointer.alignment_revision_id)
        if revision is None or alignment is None:
            raise BlockContractError("linked-notes-active-pair")
        block = next((item for item in plan.blocks if item.id == block_id), None)
        if block is None:
            raise BlockContractError("linked-notes-window-owner")
        with session.no_autoflush:
            verify_active_pair(
                pointer,
                alignment_pointer,
                session=session,
                revision=revision,
                alignment=alignment,
            )
            candidate = rehydrate_target_revision(
                revision,
                source_plan,
                session=session,
                verified_plan=plan,
                verification_cache=cache,
            )
        projection = target_alignment_projection(block, candidate)
        if not projection:
            continue
        target_text[block_id] = projection
        dependencies[block_id] = {
            "block_id": block_id,
            "revision_id": pointer.revision_id,
            "revision_record_hash": revision.revision_record_hash,
            "active_pointer_version": pointer.version,
        }
    return target_text, dependencies


def _reference_blocks(
    target_id: str,
    relations: NoteRelations,
    selected_ids: set[str],
    blocks_by_id: Mapping[str, TranslationBlock],
    selected_note_blocks: Sequence[TranslationBlock],
    selected_reference_blocks: Sequence[TranslationBlock],
    target_note_blocks: Sequence[TranslationBlock],
) -> list[TranslationBlock]:
    owners = [
        blocks_by_id[block_id]
        for block_id, targets in relations.references.items()
        if block_id in blocks_by_id
        and block_id not in selected_ids
        and target_id in targets
    ]
    if not owners:
        return []
    nearest = tuple(selected_note_blocks) or tuple(selected_reference_blocks)
    if not nearest:
        nearest = tuple(target_note_blocks)
    if nearest:
        owners.sort(
            key=lambda block: (
                min(abs(block.order - item.order) for item in nearest),
                *_block_key(block),
            )
        )
    else:
        owners.sort(key=_block_key)
    return owners[:2]


def _source_context(
    block: TranslationBlock, target_text: str | None
) -> tuple[dict[str, object], bool]:
    source_text, source_truncated = _clip(block.projection, _MAX_SOURCE_CHARS)
    item: dict[str, object] = {
        "role": block.role,
        "source_order": block.order,
        "source_text": source_text,
        "truncated": source_truncated,
    }
    clipped = source_truncated
    if target_text is not None:
        target, target_truncated = _clip(target_text, _MAX_TARGET_CHARS)
        item["target_text"] = target
        clipped = clipped or target_truncated
    item["truncated"] = clipped
    return item, clipped


def _payload(
    rows: Sequence[Mapping[str, object]],
    diagnostics: Sequence[Mapping[str, object]],
    truncated: bool,
) -> str:
    return canonical_json(
        {
            "contract_version": LINKED_NOTES_CONTRACT_VERSION,
            "guidance": (
                "Advisory linked source-note context. Use relationship numbers and "
                "source excerpts to understand the current writable blocks. Related "
                "source excerpts are read-only context: never copy them into output "
                "or duplicate a note inline, and never infer identity from labels or "
                "similar text."
            ),
            "relationships": list(rows),
            "unresolved": list(diagnostics),
            "truncated": truncated,
        }
    )


def linked_notes_slot(
    plan: TranslationSourcePlan,
    window: TranslationWindow,
    *,
    source_plan_id: str,
    source_plan: TranslationSourcePlanRecord | None = None,
    relations: NoteRelations | None = None,
) -> ContextSlot | None:
    """Build direct, bounded note relationships for one writable window."""

    _verify_source_plan(source_plan_id, source_plan, plan)
    selected = _window_blocks(plan, window)
    selected_ids = {block.id for block in selected}
    selected_numbers = {block.id: number for number, block in enumerate(selected, 1)}
    blocks_by_id = {block.id: block for block in plan.blocks}
    note_relations = relations if relations is not None else build_note_relations(plan)

    note_entries = [
        (target_id, tuple(blocks))
        for target_id, blocks in note_relations.notes.items()
        if blocks
    ]
    note_entries.sort(key=lambda item: _note_key(item[0], item[1]))
    note_numbers = {
        target_id: number for number, (target_id, _blocks) in enumerate(note_entries, 1)
    }
    related_ids: set[str] = set()
    for target_id, note_blocks in note_entries:
        if any(block.id in selected_ids for block in note_blocks) or any(
            target_id in note_relations.references.get(block.id, ())
            for block in selected
        ):
            related_ids.add(target_id)
    ordered_related = [
        target_id for target_id, _blocks in note_entries if target_id in related_ids
    ]
    truncated = len(ordered_related) > _MAX_RELATIONS
    ordered_related = ordered_related[:_MAX_RELATIONS]

    outside_candidates: list[tuple[str, TranslationBlock]] = []
    outside_candidate_ids: set[str] = set()
    provisional_rows: list[dict[str, Any]] = []
    for target_id in ordered_related:
        note_blocks = tuple(note_relations.notes.get(target_id, ()))
        writable_notes = [block for block in note_blocks if block.id in selected_ids]
        writable_references = [
            block
            for block in selected
            if target_id in note_relations.references.get(block.id, ())
        ]
        outside_notes = [
            block for block in note_blocks if block.id not in selected_ids
        ]
        outside_refs = _reference_blocks(
            target_id,
            note_relations,
            selected_ids,
            blocks_by_id,
            writable_notes,
            writable_references,
            note_blocks,
        )
        provisional_contexts: list[tuple[str, TranslationBlock]] = []
        seen_context_ids: set[str] = set()
        for block in (*outside_notes, *outside_refs):
            if block.id in seen_context_ids:
                continue
            seen_context_ids.add(block.id)
            if (
                block.id not in outside_candidate_ids
                and len(outside_candidate_ids) >= _MAX_OUTSIDE_BLOCKS
            ):
                truncated = True
                break
            provisional_contexts.append((target_id, block))
            if block.id not in outside_candidate_ids:
                outside_candidate_ids.add(block.id)
                outside_candidates.append((target_id, block))
        provisional_rows.append(
            {
                "note_number": note_numbers[target_id],
                "reference_ns": [
                    selected_numbers[block.id] for block in writable_references
                ],
                "note_ns": [selected_numbers[block.id] for block in writable_notes],
                "related_source": provisional_contexts,
            }
        )

    target_text: dict[str, str] = {}
    dependencies_by_block: dict[str, dict[str, object]] = {}
    if source_plan is not None and outside_candidates:
        target_text, dependencies_by_block = _active_context(
            source_plan,
            plan,
            [block.id for _target_id, block in outside_candidates],
        )

    rows: list[dict[str, object]] = []
    row_context_ids: list[list[str]] = []
    for row in provisional_rows:
        context_rows: list[dict[str, object]] = []
        context_ids: list[str] = []
        provisional_contexts = cast(
            list[tuple[str, TranslationBlock]], row["related_source"]
        )
        for target_id, block in provisional_contexts:
            context, clipped = _source_context(block, target_text.get(block.id))
            truncated = truncated or clipped
            context_rows.append(context)
            context_ids.append(block.id)
        row_copy = dict(row)
        row_copy["related_source"] = context_rows
        rows.append(row_copy)
        row_context_ids.append(context_ids)

    diagnostics: list[dict[str, object]] = []
    for block in selected:
        for diagnostic in note_relations.unresolved.get(block.id, ()):
            if len(diagnostics) >= _MAX_UNRESOLVED:
                truncated = True
                break
            label = getattr(diagnostic, "label", "")
            reason = getattr(diagnostic, "reason", "")
            if (
                not isinstance(label, str)
                or not isinstance(reason, str)
                or reason not in NOTE_RELATION_REASONS
            ):
                continue
            diagnostics.append(
                {
                    "n": selected_numbers[block.id],
                    "label": label[:32],
                    "reason": reason,
                }
            )

    # Remove outside contexts deterministically until the canonical slot fits,
    # preserving every relationship number and writable membership array.
    content = _payload(rows, diagnostics, truncated)
    while len(content.encode("utf-8")) > _MAX_SLOT_CONTENT_BYTES:
        removed = False
        for row_index in range(len(rows) - 1, -1, -1):
            row = rows[row_index]
            context_rows = cast(list[dict[str, object]], row["related_source"])
            if context_rows:
                context_rows.pop()
                row_context_ids[row_index].pop()
                truncated = True
                removed = True
                break
        if not removed:
            break
        content = _payload(rows, diagnostics, truncated)
    if len(content.encode("utf-8")) > _MAX_SLOT_CONTENT_BYTES:
        raise BlockContractError("linked-notes-content")
    if not rows and not diagnostics:
        return None

    dependency_ids: set[str] = set()
    active_targets: list[dict[str, object]] = []
    for context_ids in row_context_ids:
        for block_id in context_ids:
            if block_id in dependency_ids or block_id not in dependencies_by_block:
                continue
            dependency_ids.add(block_id)
            active_targets.append(dependencies_by_block[block_id])
    dependency = {
        "source_plan_id": source_plan_id,
        "active_targets": active_targets,
    }
    return ContextSlot(
        "linked_notes",
        content,
        {
            "contract_version": LINKED_NOTES_CONTRACT_VERSION,
            "source_plan_hash": plan.plan_hash,
            "window_order": window.order,
        },
        f"translation-window-linked-notes:{window.order}",
        LINKED_NOTES_CONTRACT_VERSION,
        sha256_text(content),
        dependency,
        False,
    )


__all__ = ["LINKED_NOTES_CONTRACT_VERSION", "linked_notes_slot"]
