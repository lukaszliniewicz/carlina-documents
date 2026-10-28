"""Deterministic, evidence-preserving source-plan corrections.

The document snapshot never changes here.  A correction derives a new semantic
grouping from the same attested bytes, retains the predecessor plan as durable
evidence, and records enough canonical material to reproduce the successor.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, replace

from .block_contract import (
    BLOCK_PLANNER_VERSION,
    BLOCK_PLANNER_V2,
    CORRECTED_BLOCK_PLANNER_VERSION,
    LEGACY_BLOCK_PLANNER_VERSION,
    BlockContractError,
    BlockWirePart,
    CoverageEntry,
    CoverageWitness,
    ProtectedInlineToken,
    SourceAnchor,
    SourceSlot,
    TranslationBlock,
    TranslationSourcePlan,
    _plain_value,
    canonical_json,
    sha256_text,
)

CORRECTION_SETTINGS_CONTRACT = "translation-source-plan-corrections-v1"
MERGE_BOUNDARY_TEXT = " "
SUPPORTED_CORRECTION_ACTIONS = frozenset({"merge_previous", "merge_next", "exclude"})
MAX_CORRECTION_STEPS = 10_000
_WRAPPER_EXCLUSION_ROLES = frozenset(
    {
        "blockquote",
        "definition_body",
        "definition_term",
        "document_title",
        "figure_caption",
        "heading",
        "list_item",
        "note_body",
        "paragraph",
        "source_toc_entry",
        "table_caption",
    }
)
_CORRECTION_KEYS = frozenset(
    {
        "action",
        "block_id",
        "adjacent_block_id",
        "direction",
        "reason_code",
        "source_start",
        "source_end",
        "source_hash",
    }
)
_SETTINGS_KEYS = frozenset(
    {
        "contract_version",
        "base_planner_version",
        "base_planner_settings",
        "base_plan_hash",
        "corrections",
    }
)
_DETERMINISTIC_FORMAT_TAGS = frozenset({"b", "em", "i", "strong", "sub", "sup", "u"})


@dataclass(frozen=True, slots=True)
class CorrectionPlanResult:
    plan: TranslationSourcePlan
    block_map: Mapping[str, str | None]
    unchanged_block_map: Mapping[str, str]
    affected_block_ids: tuple[str, ...]
    result_block_id: str | None


def _hash(value: object, code: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise BlockContractError(code)
    return value


def _identifier(value: object, code: str, *, maximum: int = 128) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value.encode("utf-8")) > maximum
    ):
        raise BlockContractError(code)
    return value


def _optional_identifier(value: object, code: str) -> str | None:
    if value is None:
        return None
    return _identifier(value, code)


def _integer(value: object, code: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise BlockContractError(code)
    return value


def _normalize_entry(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != _CORRECTION_KEYS:
        raise BlockContractError("correction-entry")
    action = value.get("action")
    if action not in SUPPORTED_CORRECTION_ACTIONS:
        raise BlockContractError("correction-action")
    block_id = _identifier(value.get("block_id"), "correction-block")
    adjacent = _optional_identifier(
        value.get("adjacent_block_id"), "correction-adjacent"
    )
    direction = value.get("direction")
    reason_code = _identifier(value.get("reason_code"), "correction-reason", maximum=96)
    source_start = _integer(value.get("source_start"), "correction-source-span")
    source_end = _integer(value.get("source_end"), "correction-source-span")
    source_hash = value.get("source_hash")
    if action == "exclude":
        if (
            adjacent is not None
            or direction is not None
            or source_start is None
            or source_end is None
            or source_end <= source_start
        ):
            raise BlockContractError("correction-exclude-entry")
        source_hash = _hash(source_hash, "correction-source-hash")
    else:
        expected_direction = "previous" if action == "merge_previous" else "next"
        if (
            adjacent is None
            or direction != expected_direction
            or source_start is not None
            or source_end is not None
            or source_hash is not None
        ):
            raise BlockContractError("correction-merge-entry")
    return {
        "action": action,
        "block_id": block_id,
        "adjacent_block_id": adjacent,
        "direction": direction,
        "reason_code": reason_code,
        "source_start": source_start,
        "source_end": source_end,
        "source_hash": source_hash,
    }


def normalize_correction_settings(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != _SETTINGS_KEYS:
        raise BlockContractError("correction-settings")
    if value.get("contract_version") != CORRECTION_SETTINGS_CONTRACT:
        raise BlockContractError("correction-settings-contract")
    base_version = value.get("base_planner_version")
    if base_version not in {
        LEGACY_BLOCK_PLANNER_VERSION, BLOCK_PLANNER_V2, BLOCK_PLANNER_VERSION,
    }:
        raise BlockContractError("correction-base-planner")
    base_settings = value.get("base_planner_settings")
    if not isinstance(base_settings, Mapping):
        raise BlockContractError("correction-base-settings")
    corrections = value.get("corrections")
    if (
        not isinstance(corrections, (list, tuple))
        or not corrections
        or len(corrections) > MAX_CORRECTION_STEPS
    ):
        raise BlockContractError("correction-entries")
    return {
        "contract_version": CORRECTION_SETTINGS_CONTRACT,
        "base_planner_version": base_version,
        # Retained sentence manifests contain recursively frozen mappings.
        # Correction receipts need their plain JSON form, not a shallow copy.
        "base_planner_settings": _plain_value(base_settings),
        "base_plan_hash": _hash(value.get("base_plan_hash"), "correction-base-hash"),
        "corrections": [_normalize_entry(item) for item in corrections],
    }


def _step_fingerprint(corrections: list[dict[str, object]]) -> str:
    return sha256_text(canonical_json(corrections))


def _derived_id(namespace: str, *parts: object) -> str:
    try:
        parsed = uuid.UUID(namespace)
    except (TypeError, ValueError, AttributeError) as exc:
        raise BlockContractError("correction-namespace") from exc
    return str(uuid.uuid5(parsed, ":".join(str(part) for part in parts)))


def _rekey_anchors(
    block: TranslationBlock,
    *,
    new_block_id: str,
    step_hash: str,
    namespace: str,
) -> tuple[SourceAnchor, ...]:
    return tuple(
        replace(
            anchor,
            id=_derived_id(
                namespace,
                "correction",
                step_hash,
                "anchor",
                new_block_id,
                anchor.id,
            ),
        )
        for anchor in block.anchors
    )


def _rekey_block(
    block: TranslationBlock,
    *,
    order: int,
    step_hash: str,
    namespace: str,
) -> TranslationBlock:
    block_id = _derived_id(namespace, "correction", step_hash, "block", block.id)
    return replace(
        block,
        id=block_id,
        order=order,
        anchors=_rekey_anchors(
            block,
            new_block_id=block_id,
            step_hash=step_hash,
            namespace=namespace,
        ),
    )


def _outer_wrapper_pairs(
    block: TranslationBlock,
) -> tuple[tuple[ProtectedInlineToken, ProtectedInlineToken], ...]:
    tokens = {token.id: token for token in block.protected_inline}
    if any(token.movement_policy == "source-only" for token in tokens.values()):
        openings: list[ProtectedInlineToken] = []
        closings: list[ProtectedInlineToken] = []
        for part in block.wire:
            token = tokens.get(part.ref_id) if part.kind == "token" else None
            if (
                token is None
                or token.kind != "open"
                or token.movement_policy != "source-only"
            ):
                break
            openings.append(token)
        for part in reversed(block.wire):
            token = tokens.get(part.ref_id) if part.kind == "token" else None
            if (
                token is None
                or token.kind != "close"
                or token.movement_policy != "source-only"
            ):
                break
            closings.append(token)
        if len(openings) != len(closings):
            raise BlockContractError("correction-merge-structure")
        return tuple(zip(openings, closings, strict=True))
    left = 0
    right = len(block.wire) - 1
    result: list[tuple[ProtectedInlineToken, ProtectedInlineToken]] = []
    while left < right:
        opening_part = block.wire[left]
        closing_part = block.wire[right]
        if opening_part.kind != "token" or closing_part.kind != "token":
            break
        opening = tokens.get(opening_part.ref_id)
        closing = tokens.get(closing_part.ref_id)
        if (
            opening is None
            or closing is None
            or opening.kind != "open"
            or closing.kind != "close"
            or opening.element_id != closing.element_id
            or opening.tag != closing.tag
        ):
            break
        result.append((opening, closing))
        left += 1
        right -= 1
    return tuple(result)


def _shift_slot(slot: SourceSlot, offset: int) -> SourceSlot:
    return replace(
        slot,
        projection_start=(
            None if slot.projection_start is None else slot.projection_start + offset
        ),
        projection_end=(
            None if slot.projection_end is None else slot.projection_end + offset
        ),
    )


def _merged_anchors(
    left: TranslationBlock,
    right: TranslationBlock,
    *,
    merged_projection: str,
    new_block_id: str,
    step_hash: str,
    namespace: str,
) -> tuple[SourceAnchor, ...]:
    if not left.anchors or not right.anchors:
        raise BlockContractError("correction-merge-anchors")
    shift = len(left.projection) + len(MERGE_BOUNDARY_TEXT)
    combined: list[tuple[int, int, str, str]] = []
    for anchor in left.anchors:
        end = anchor.end
        if anchor is left.anchors[-1]:
            end += len(MERGE_BOUNDARY_TEXT)
        combined.append(
            (anchor.start, end, anchor.splitter_profile, anchor.splitter_version)
        )
    combined.extend(
        (
            anchor.start + shift,
            anchor.end + shift,
            anchor.splitter_profile,
            anchor.splitter_version,
        )
        for anchor in right.anchors
    )
    return tuple(
        SourceAnchor(
            id=_derived_id(
                namespace,
                "correction",
                step_hash,
                "anchor",
                new_block_id,
                index,
            ),
            order=index,
            start=start,
            end=end,
            source_hash=sha256_text(merged_projection[start:end]),
            splitter_profile=profile,
            splitter_version=version,
        )
        for index, (start, end, profile, version) in enumerate(combined, 1)
    )


def _source_only_boundary_wrappers(
    left: TranslationBlock,
    right: TranslationBlock,
) -> tuple[tuple[ProtectedInlineToken, ...], tuple[ProtectedInlineToken, ...]]:
    left_wrappers = _outer_wrapper_pairs(left)
    right_wrappers = _outer_wrapper_pairs(right)
    left_boundary_ids: set[str] = set()
    right_boundary_ids: set[str] = set()
    for (left_open, left_close), (right_open, right_close) in zip(
        left_wrappers, right_wrappers
    ):
        if (
            left_open.tag not in _DETERMINISTIC_FORMAT_TAGS
            or left_open.tag != right_open.tag
            or left_close.tag != right_close.tag
            or left_open.source_value != right_open.source_value
            or left_close.source_value != right_close.source_value
            or left_open.movement_policy != right_open.movement_policy
            or left_close.movement_policy != right_close.movement_policy
            or re.fullmatch(
                rf"<\s*{re.escape(left_open.tag)}\s*>",
                left_open.source_value,
                re.IGNORECASE,
            )
            is None
            or re.fullmatch(
                rf"</\s*{re.escape(left_close.tag)}\s*>",
                left_close.source_value,
                re.IGNORECASE,
            )
            is None
        ):
            break
        left_boundary_ids.update((left_open.id, left_close.id))
        right_boundary_ids.update((right_open.id, right_close.id))
    return (
        tuple(
            replace(token, movement_policy="source-only")
            if token.id in left_boundary_ids
            else token
            for token in left.protected_inline
        ),
        tuple(
            replace(token, movement_policy="source-only")
            if token.id in right_boundary_ids
            else token
            for token in right.protected_inline
        ),
    )


def _merge_gap_is_safe(
    left: TranslationBlock, right: TranslationBlock, source: str
) -> bool:
    """Accept whitespace or one exact matching semantic wrapper boundary."""

    gap = source[left.source_end : right.source_start]
    if not gap.strip():
        return True
    opening = re.search(
        r"(?P<opening><\s*(?P<tag>[A-Za-z][A-Za-z0-9:_-]*)(?:\s[^<>]*)?>)\s*$",
        source[: left.source_start],
    )
    if opening is None:
        return False
    tag = re.escape(opening.group("tag"))
    boundary = re.fullmatch(
        rf"\s*</\s*{tag}\s*>\s*(?P<opening><\s*{tag}(?:\s[^<>]*)?>)\s*",
        gap,
        re.IGNORECASE,
    )
    if boundary is None or boundary.group("opening") != opening.group("opening"):
        return False
    return (
        re.match(
            rf"\s*</\s*{tag}\s*>",
            source[right.source_end :],
            re.IGNORECASE,
        )
        is not None
    )


def _exclusion_span(block: TranslationBlock, source: str) -> tuple[int, int]:
    """Expand a standalone semantic block to its exact enclosing wrapper."""

    if block.role not in _WRAPPER_EXCLUSION_ROLES:
        return block.source_start, block.source_end
    opening = re.search(
        r"<\s*(?P<tag>[A-Za-z][A-Za-z0-9:_-]*)(?:\s[^<>]*)?>\s*$",
        source[: block.source_start],
    )
    if opening is None:
        return block.source_start, block.source_end
    tag = re.escape(opening.group("tag"))
    closing = re.match(
        rf"\s*</\s*{tag}\s*>",
        source[block.source_end :],
        re.IGNORECASE,
    )
    if closing is None:
        return block.source_start, block.source_end
    return opening.start(), block.source_end + closing.end()


def _merge_blocks(
    left: TranslationBlock,
    right: TranslationBlock,
    *,
    source: str,
    order: int,
    step_hash: str,
    namespace: str,
) -> TranslationBlock:
    if (
        left.role != right.role
        or left.role == "accessibility_text"
        or left.source_end > right.source_start
        or not _merge_gap_is_safe(left, right, source)
    ):
        raise BlockContractError("correction-merge-incompatible")
    left_tokens, right_tokens = _source_only_boundary_wrappers(left, right)
    boundary_position = left.source_end
    boundary = SourceSlot(
        id=_derived_id(namespace, "correction", step_hash, "boundary"),
        locator=f"derived:merge-boundary:{step_hash[:16]}",
        kind="boundary",
        disposition="translatable",
        policy_code="owner-merge-boundary",
        raw_start=boundary_position,
        raw_end=boundary_position,
        projection_start=len(left.projection),
        projection_end=len(left.projection) + len(MERGE_BOUNDARY_TEXT),
        source_value="",
        source_hash=sha256_text(""),
    )
    shift = len(left.projection) + len(MERGE_BOUNDARY_TEXT)
    slots = (
        *left.slots,
        boundary,
        *(_shift_slot(slot, shift) for slot in right.slots),
    )
    if len({slot.id for slot in slots}) != len(slots):
        raise BlockContractError("correction-merge-slot-identity")
    tokens = (*left_tokens, *right_tokens)
    if len({token.id for token in tokens}) != len(tokens):
        raise BlockContractError("correction-merge-token-identity")
    projection = left.projection + MERGE_BOUNDARY_TEXT + right.projection
    block_id = _derived_id(
        namespace,
        "correction",
        step_hash,
        "merge",
        left.id,
        right.id,
    )
    return TranslationBlock(
        id=block_id,
        order=order,
        role=left.role,
        locator=f"derived:merge:{step_hash[:24]}",
        source_start=left.source_start,
        source_end=right.source_end,
        projection=projection,
        projection_hash=sha256_text(projection),
        slots=tuple(slots),
        protected_inline=tuple(tokens),
        wire=(
            *left.wire,
            BlockWirePart(kind="slot", ref_id=boundary.id),
            *right.wire,
        ),
        anchors=_merged_anchors(
            left,
            right,
            merged_projection=projection,
            new_block_id=block_id,
            step_hash=step_hash,
            namespace=namespace,
        ),
    )


def _apply_step(
    current: TranslationSourcePlan,
    entry: dict[str, object],
    *,
    settings: dict[str, object],
) -> CorrectionPlanResult:
    blocks = list(current.blocks)
    by_id = {block.id: block for block in blocks}
    selected = by_id.get(str(entry["block_id"]))
    if selected is None:
        raise BlockContractError("correction-block-owner")
    action = str(entry["action"])
    selected_index = blocks.index(selected)
    adjacent: TranslationBlock | None = None
    if action == "merge_previous":
        if selected_index == 0:
            raise BlockContractError("correction-adjacent-missing")
        adjacent = blocks[selected_index - 1]
    elif action == "merge_next":
        if selected_index + 1 >= len(blocks):
            raise BlockContractError("correction-adjacent-missing")
        adjacent = blocks[selected_index + 1]
    if adjacent is not None and adjacent.id != entry["adjacent_block_id"]:
        raise BlockContractError("correction-adjacent-owner")
    exclusion_start, exclusion_end = _exclusion_span(
        selected, current.snapshot.output_html
    )
    if action == "exclude" and (
        entry["source_start"] != exclusion_start
        or entry["source_end"] != exclusion_end
        or entry["source_hash"]
        != sha256_text(current.snapshot.output_html[exclusion_start:exclusion_end])
    ):
        raise BlockContractError("correction-exclude-source")

    corrections = list(settings["corrections"])
    step_hash = _step_fingerprint(corrections)
    if action == "exclude" and any(
        block.id != selected.id
        and block.source_start <= selected.source_start
        and selected.source_end <= block.source_end
        for block in blocks
    ):
        raise BlockContractError("correction-exclude-overlap")
    removed_ids = (
        {
            block.id
            for block in blocks
            if selected.source_start <= block.source_start
            and block.source_end <= selected.source_end
        }
        if action == "exclude"
        else {selected.id}
    )
    merge_left: TranslationBlock | None = None
    merge_right: TranslationBlock | None = None
    if adjacent is not None:
        removed_ids.add(adjacent.id)
        merge_left, merge_right = sorted(
            (selected, adjacent), key=lambda block: block.order
        )

    transformed: list[TranslationBlock] = []
    block_map: dict[str, str | None] = {}
    unchanged: dict[str, str] = {}
    merged_id: str | None = None
    for block in blocks:
        if block.id in removed_ids:
            if merge_left is not None and block.id == merge_left.id:
                merged = _merge_blocks(
                    merge_left,
                    merge_right,
                    source=current.snapshot.output_html,
                    order=len(transformed) + 1,
                    step_hash=step_hash,
                    namespace=current.namespace,
                )
                transformed.append(merged)
                merged_id = merged.id
                block_map[merge_left.id] = merged.id
                block_map[merge_right.id] = merged.id
            elif merge_left is None:
                block_map[block.id] = None
            continue
        copied = _rekey_block(
            block,
            order=len(transformed) + 1,
            step_hash=step_hash,
            namespace=current.namespace,
        )
        transformed.append(copied)
        block_map[block.id] = copied.id
        unchanged[block.id] = copied.id

    coverage_entries: list[CoverageEntry] = []
    for coverage in current.coverage.entries:
        if coverage.block_id is None:
            coverage_entries.append(coverage)
            continue
        mapped = block_map.get(coverage.block_id)
        if coverage.block_id in removed_ids and action == "exclude":
            coverage_entries.append(
                replace(
                    coverage,
                    disposition="excluded-with-reason",
                    policy_code=str(entry["reason_code"]),
                    block_id=None,
                )
            )
        elif mapped is None:
            raise BlockContractError("correction-coverage-owner")
        else:
            coverage_entries.append(replace(coverage, block_id=mapped))

    settings_fingerprint = sha256_text(canonical_json(settings))
    plan = TranslationSourcePlan.create(
        snapshot=current.snapshot,
        source_attestation=current.source_attestation,
        project_id=current.project_id,
        planner_version=CORRECTED_BLOCK_PLANNER_VERSION,
        source_language=current.source_language,
        language_profile=current.language_profile,
        planner_settings=settings,
        settings_fingerprint=settings_fingerprint,
        namespace=current.namespace,
        blocks=tuple(transformed),
        coverage=CoverageWitness.create(tuple(coverage_entries)),
    )
    return CorrectionPlanResult(
        plan=plan,
        block_map=block_map,
        unchanged_block_map=unchanged,
        affected_block_ids=tuple(sorted(removed_ids)),
        result_block_id=merged_id,
    )


def _base_settings(current: TranslationSourcePlan) -> dict[str, object]:
    if current.planner_version == CORRECTED_BLOCK_PLANNER_VERSION:
        return normalize_correction_settings(current.planner_settings)
    if current.planner_version not in {
        LEGACY_BLOCK_PLANNER_VERSION,
        BLOCK_PLANNER_V2,
        BLOCK_PLANNER_VERSION,
    }:
        raise BlockContractError("correction-base-planner")
    return {
        "contract_version": CORRECTION_SETTINGS_CONTRACT,
        "base_planner_version": current.planner_version,
        "base_planner_settings": current.to_dict()["planner_settings"],
        "base_plan_hash": current.plan_hash,
        "corrections": [],
    }


def build_successor_plan(
    current: TranslationSourcePlan,
    *,
    block_id: str,
    action: str,
) -> CorrectionPlanResult:
    if action not in SUPPORTED_CORRECTION_ACTIONS:
        raise BlockContractError("correction-action")
    selected = next((block for block in current.blocks if block.id == block_id), None)
    if selected is None:
        raise BlockContractError("correction-block-owner")
    index = current.blocks.index(selected)
    adjacent: TranslationBlock | None = None
    direction: str | None = None
    if action == "merge_previous":
        if index == 0:
            raise BlockContractError("correction-adjacent-missing")
        adjacent = current.blocks[index - 1]
        direction = "previous"
    elif action == "merge_next":
        if index + 1 >= len(current.blocks):
            raise BlockContractError("correction-adjacent-missing")
        adjacent = current.blocks[index + 1]
        direction = "next"
    exclusion_start, exclusion_end = _exclusion_span(
        selected, current.snapshot.output_html
    )
    entry = {
        "action": action,
        "block_id": selected.id,
        "adjacent_block_id": adjacent.id if adjacent is not None else None,
        "direction": direction,
        "reason_code": (
            "owner-excluded-marginalia" if action == "exclude" else "owner-merged"
        ),
        "source_start": exclusion_start if action == "exclude" else None,
        "source_end": exclusion_end if action == "exclude" else None,
        "source_hash": (
            sha256_text(current.snapshot.output_html[exclusion_start:exclusion_end])
            if action == "exclude"
            else None
        ),
    }
    normalized_entry = _normalize_entry(entry)
    settings = _base_settings(current)
    settings["corrections"] = [*list(settings["corrections"]), normalized_entry]
    normalized_settings = normalize_correction_settings(settings)
    return _apply_step(current, normalized_entry, settings=normalized_settings)


def rebuild_corrected_plan(expected: TranslationSourcePlan) -> TranslationSourcePlan:
    if expected.planner_version != CORRECTED_BLOCK_PLANNER_VERSION:
        raise BlockContractError("correction-plan-version")
    settings = normalize_correction_settings(expected.planner_settings)
    from .block_planner import plan_document_blocks

    current = plan_document_blocks(
        expected.snapshot,
        project_id=expected.project_id,
        source_attestation=expected.source_attestation,
        source_language=expected.source_language,
        language_profile=expected.language_profile,
        settings=settings["base_planner_settings"],
        planner_version=str(settings["base_planner_version"]),
    )
    if current.plan_hash != settings["base_plan_hash"]:
        raise BlockContractError("correction-base-hash")
    prefix: list[dict[str, object]] = []
    for raw_entry in settings["corrections"]:
        entry = _normalize_entry(raw_entry)
        prefix.append(entry)
        step_settings = {
            **settings,
            "corrections": list(prefix),
        }
        current = _apply_step(current, entry, settings=step_settings).plan
    return current


def corrected_exclusion_spans(
    plan: TranslationSourcePlan,
) -> tuple[tuple[int, int], ...]:
    if plan.planner_version != CORRECTED_BLOCK_PLANNER_VERSION:
        return ()
    settings = normalize_correction_settings(plan.planner_settings)
    return tuple(
        (int(entry["source_start"]), int(entry["source_end"]))
        for entry in settings["corrections"]
        if entry["action"] == "exclude"
    )


__all__ = [
    "CORRECTION_SETTINGS_CONTRACT",
    "SUPPORTED_CORRECTION_ACTIONS",
    "CorrectionPlanResult",
    "build_successor_plan",
    "corrected_exclusion_spans",
    "normalize_correction_settings",
    "rebuild_corrected_plan",
]
