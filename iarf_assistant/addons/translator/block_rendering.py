"""Fail-closed identity rendering for a planned block source document.

This is intentionally separate from the legacy Translator renderer.  Phase 1
does not render target language material; it proves that a source plan can be
reconstructed from its exact source slices without normalization.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from html import unescape
from itertools import pairwise

from .block_contract import BlockContractError, TranslationSourcePlan, sha256_text
from .block_planner import validate_canonical_plan


def _require_slice(
    source: str, start: int, end: int, expected_hash: str, code: str
) -> str:
    if start < 0 or end <= start or end > len(source):
        raise BlockContractError(code)
    value = source[start:end]
    if sha256_text(value) != expected_hash:
        raise BlockContractError(code)
    return value


def _non_overlapping(
    intervals: Iterable[tuple[int, int, str]], *, code: str
) -> list[tuple[int, int, str]]:
    ordered = sorted(intervals, key=lambda item: (item[0], item[1], item[2]))
    if any(left[1] > right[0] for left, right in pairwise(ordered)):
        raise BlockContractError(code)
    return ordered


def _validate_plan(
    plan: TranslationSourcePlan, source: str
) -> list[tuple[int, int, str]]:
    if (
        plan.snapshot.output_html != source
        or sha256_text(source) != plan.snapshot.content_hash
    ):
        raise BlockContractError("identity-source-hash")
    # Reconstructing from the canonical DTO is a plan-hash verification rather
    # than trusting a mutable caller-provided plan object.
    if plan._expected_hash() != plan.plan_hash:
        raise BlockContractError("identity-plan-hash")
    validate_canonical_plan(plan)
    slot_ids: set[str] = set()
    token_ids: set[str] = set()
    slot_spans: list[tuple[int, int, str]] = []
    token_spans: list[tuple[int, int, str]] = []
    block_ids = {block.id for block in plan.blocks}
    if len(block_ids) != len(plan.blocks):
        raise BlockContractError("identity-block-duplicate")
    ordered_coverage = sorted(
        plan.coverage.entries,
        key=lambda entry: (entry.raw_start, entry.raw_end, entry.locator),
    )
    if list(plan.coverage.entries) != ordered_coverage:
        raise BlockContractError("identity-coverage-order")
    _non_overlapping(
        [
            (entry.raw_start, entry.raw_end, entry.locator)
            for entry in plan.coverage.entries
        ],
        code="identity-coverage-overlap",
    )
    coverage_owned: set[tuple[object, ...]] = set()
    for entry in plan.coverage.entries:
        _require_slice(
            source,
            entry.raw_start,
            entry.raw_end,
            entry.source_hash,
            "identity-coverage",
        )
        if entry.block_id is not None:
            if (
                entry.block_id not in block_ids
                or entry.disposition == "excluded-with-reason"
            ):
                raise BlockContractError("identity-coverage-owner")
            key = (
                entry.block_id,
                entry.locator,
                entry.kind,
                entry.disposition,
                entry.policy_code,
                entry.raw_start,
                entry.raw_end,
                entry.source_hash,
            )
            if key in coverage_owned:
                raise BlockContractError("identity-coverage-duplicate")
            coverage_owned.add(key)
        elif entry.disposition == "translatable":
            raise BlockContractError("identity-coverage-owner")
    expected_owned = {
        (
            block.id,
            slot.locator,
            slot.kind,
            slot.disposition,
            slot.policy_code,
            slot.raw_start,
            slot.raw_end,
            slot.source_hash,
        )
        for block in plan.blocks
        for slot in block.slots
        if slot.kind != "boundary"
    }
    if coverage_owned != expected_owned:
        raise BlockContractError("identity-coverage-cardinality")
    for block in plan.blocks:
        if (
            block.source_start < 0
            or block.source_end <= block.source_start
            or block.source_end > len(source)
        ):
            raise BlockContractError("identity-block-span")
        if sha256_text(block.projection) != block.projection_hash:
            raise BlockContractError("identity-projection")
        reconstructed_projection: list[str] = []
        last_projection = 0
        for slot in sorted(
            block.slots, key=lambda value: (value.raw_start, value.raw_end, value.id)
        ):
            if slot.id in slot_ids:
                raise BlockContractError("identity-slot-duplicate")
            slot_ids.add(slot.id)
            if slot.kind == "boundary":
                value = ""
                if not (
                    block.source_start
                    <= slot.raw_start
                    == slot.raw_end
                    <= block.source_end
                ):
                    raise BlockContractError("identity-slot-scope")
            else:
                value = _require_slice(
                    source,
                    slot.raw_start,
                    slot.raw_end,
                    slot.source_hash,
                    "identity-slot",
                )
                if value != slot.source_value:
                    raise BlockContractError("identity-slot-value")
                if not (
                    block.source_start
                    <= slot.raw_start
                    < slot.raw_end
                    <= block.source_end
                ):
                    raise BlockContractError("identity-slot-scope")
            if (
                slot.projection_start is None
                or slot.projection_end is None
                or slot.projection_start != last_projection
            ):
                raise BlockContractError("identity-slot-projection")
            projected = (
                " "
                if slot.kind == "boundary"
                else "\n"
                if slot.kind == "break"
                else unescape(slot.source_value)
            )
            if slot.projection_end - slot.projection_start != len(projected):
                raise BlockContractError("identity-slot-projection")
            reconstructed_projection.append(projected)
            last_projection = slot.projection_end
            if slot.kind != "boundary":
                slot_spans.append((slot.raw_start, slot.raw_end, slot.source_value))
        if "".join(reconstructed_projection) != block.projection:
            raise BlockContractError("identity-projection")
        if block.anchors:
            if block.anchors[0].start != 0 or block.anchors[-1].end != len(
                block.projection
            ):
                raise BlockContractError("identity-anchor-coverage")
            for index, anchor in enumerate(block.anchors, 1):
                if (
                    anchor.order != index
                    or anchor.start >= anchor.end
                    or sha256_text(block.projection[anchor.start : anchor.end])
                    != anchor.source_hash
                ):
                    raise BlockContractError("identity-anchor")
                if index > 1 and block.anchors[index - 2].end != anchor.start:
                    raise BlockContractError("identity-anchor-coverage")
        references = {("slot", slot.id): slot for slot in block.slots} | {
            ("token", token.id): token for token in block.protected_inline
        }
        wire_starts: list[int] = []
        stack: list[tuple[str, str, str, str]] = []
        for part in block.wire:
            reference = references.get((part.kind, part.ref_id))
            if reference is None:
                raise BlockContractError("identity-wire-reference")
            wire_starts.append(reference.raw_start)
            if part.kind == "token":
                token = reference
                if token.kind == "open":
                    stack.append(
                        (
                            token.element_id,
                            token.tag,
                            token.locator,
                            token.movement_policy,
                        )
                    )
                elif token.kind == "close" and (
                    not stack
                    or stack.pop()
                    != (
                        token.element_id,
                        token.tag,
                        token.locator,
                        token.movement_policy,
                    )
                ):
                    raise BlockContractError("identity-inline-nesting")
        if wire_starts != sorted(wire_starts) or stack:
            raise BlockContractError("identity-wire-order")
        for token in sorted(
            block.protected_inline,
            key=lambda value: (value.raw_start, value.raw_end, value.id),
        ):
            if token.id in token_ids:
                raise BlockContractError("identity-inline-duplicate")
            token_ids.add(token.id)
            value = _require_slice(
                source,
                token.raw_start,
                token.raw_end,
                token.source_hash,
                "identity-inline",
            )
            if value != token.source_value:
                raise BlockContractError("identity-inline-value")
            escaped = re.escape(token.tag)
            pattern = (
                rf"^</\s*{escaped}\s*>$"
                if token.kind == "close"
                else rf"^<\s*{escaped}(?:\s|/|>)"
            )
            if not re.match(pattern, token.source_value, re.IGNORECASE):
                raise BlockContractError("identity-inline-tag")
            if not (
                block.source_start
                <= token.raw_start
                < token.raw_end
                <= block.source_end
            ):
                raise BlockContractError("identity-inline-scope")
            token_spans.append((token.raw_start, token.raw_end, token.source_value))
    # Attribute values live inside their exact opening-tag protected token, so
    # cross-kind containment is valid.  Peer slots/tokens must still never
    # overlap; reconstruction uses the enclosing token once.
    _non_overlapping(slot_spans, code="identity-slot-overlap")
    token_spans = _non_overlapping(token_spans, code="identity-inline-overlap")
    renderable_slots = [
        span
        for span in slot_spans
        if not any(
            token_start <= span[0] and span[1] <= token_end
            for token_start, token_end, _ in token_spans
        )
    ]
    return _non_overlapping(
        renderable_slots + token_spans, code="identity-owned-overlap"
    )


def render_source_identity(
    plan: TranslationSourcePlan, snapshot_html: str | None = None
) -> str:
    """Reconstruct the original bytes/string from owned plan slices and gaps.

    The unchanged structural gaps are copied only after all block-owned text,
    attributes, and protected inline tag slices have been individually
    revalidated.  This is intentionally *not* ``return snapshot_html``.
    """

    source = plan.snapshot.output_html if snapshot_html is None else snapshot_html
    if not isinstance(source, str):
        raise BlockContractError("identity-source")
    spans = _validate_plan(plan, source)
    pieces: list[str] = []
    cursor = 0
    for start, end, stored_value in spans:
        if start < cursor:
            raise BlockContractError("identity-order")
        pieces.append(source[cursor:start])
        # Owned bytes come from immutable plan values, not caller source slices.
        pieces.append(stored_value)
        cursor = end
    pieces.append(source[cursor:])
    rendered = "".join(pieces)
    if rendered != source or sha256_text(rendered) != plan.snapshot.content_hash:
        raise BlockContractError("identity-output-hash")
    return rendered


identity_render = render_source_identity
