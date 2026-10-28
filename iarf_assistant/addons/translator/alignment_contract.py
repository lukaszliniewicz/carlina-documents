"""Exact, provider-independent contracts for target spans and alignments.

The alignment projection is deliberately not rendered HTML.  It is the target
candidate's text plus source-owned literal/structural slots, in candidate order. A
protected-inline token is structural only and consumes no projection offset.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from .block_contract import (
    BlockContractError,
    TranslationBlock,
    canonical_json,
    sha256_text,
)
from .target_contract import TargetBlockCandidate, literal_reference_slots

TARGET_ALIGNMENT_CONTRACT_VERSION = "target-alignment-v1"
TARGET_SPLITTER_VERSION = "target-splitter-v1"
ALIGNMENT_CONFIDENCE_PROFILE = "alignment-confidence-v1"
ALIGNMENT_CONFIDENCE_THRESHOLD = 0.85


def _hash(value: object, code: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(c not in "0123456789abcdef" for c in value)
    ):
        raise BlockContractError(code)
    return value


def _ids(value: Iterable[object], code: str) -> tuple[str, ...]:
    result = tuple(value)
    if (
        not result
        or any(not isinstance(item, str) or not item for item in result)
        or len(set(result)) != len(result)
    ):
        raise BlockContractError(code)
    return result  # type: ignore[return-value]


def target_alignment_scope(block: TranslationBlock) -> str:
    """Name the one semantic target scope represented by a span set."""

    attribute_slots = tuple(
        slot
        for slot in block.slots
        if slot.kind == "attribute" and slot.disposition == "translatable"
    )
    non_attribute = tuple(slot for slot in block.slots if slot.kind != "attribute")
    if not non_attribute and attribute_slots:
        if len(attribute_slots) != 1 or len(block.slots) != 1:
            raise BlockContractError("alignment-attribute-scope")
        return f"attribute:{attribute_slots[0].id}"
    return "prose"


def target_alignment_projection(
    block: TranslationBlock, candidate: TargetBlockCandidate
) -> str:
    """Return the exact offset-bearing target text for alignment.

    Text runs are model authored.  Literal references expand to the immutable
    source slot value.  Tokens intentionally expand to ``""``: treating their
    markup bytes as prose would make every subsequent target span depend on a
    renderer implementation rather than on the stable candidate contract.
    """

    scope = target_alignment_scope(block)
    if scope.startswith("attribute:"):
        slot_id = scope.removeprefix("attribute:")
        lexical = {item.slot_id: item.value for item in candidate.lexical_attributes}
        if set(lexical) != {slot_id}:
            raise BlockContractError("alignment-attribute-lexical")
        return lexical[slot_id]
    literals = {slot.id: slot.source_value for slot in literal_reference_slots(block)}
    pieces: list[str] = []
    for part in candidate.parts:
        if part.kind == "text":
            pieces.append(part.text or "")
        elif part.kind == "literal":
            literal = literals.get(part.literal_slot_id or "")
            if literal is None:
                raise BlockContractError("alignment-literal-reference")
            pieces.append(literal)
        elif part.kind not in {"token", "format"}:
            raise BlockContractError("alignment-target-part")
    return "".join(pieces)


@dataclass(frozen=True, slots=True)
class TargetSpan:
    id: str
    order: int
    start: int
    end: int
    text_hash: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.id, str)
            or not self.id
            or not isinstance(self.order, int)
            or self.order < 1
        ):
            raise BlockContractError("alignment-span-identity")
        if (
            not isinstance(self.start, int)
            or not isinstance(self.end, int)
            or self.start < 0
            or self.end <= self.start
        ):
            raise BlockContractError("alignment-span-offset")
        _hash(self.text_hash, "alignment-span-text-hash")

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "order": self.order,
            "start": self.start,
            "end": self.end,
            "text_hash": self.text_hash,
        }


@dataclass(frozen=True, slots=True)
class AlignmentGroup:
    source_anchor_ids: tuple[str, ...]
    target_span_ids: tuple[str, ...]
    method: str
    confidence: float
    review_state: str
    evidence: Mapping[str, object]

    def __post_init__(self) -> None:
        source = tuple(self.source_anchor_ids)
        target = tuple(self.target_span_ids)
        if (
            not source
            or not target
            or len(set(source)) != len(source)
            or len(set(target)) != len(target)
        ):
            raise BlockContractError("alignment-group-cardinality")
        if any(not isinstance(item, str) or not item for item in (*source, *target)):
            raise BlockContractError("alignment-group-membership")
        if (
            not isinstance(self.method, str)
            or not self.method
            or self.review_state not in {"approved", "needs_review"}
        ):
            raise BlockContractError("alignment-group-state")
        if (
            isinstance(self.confidence, bool)
            or not isinstance(self.confidence, (int, float))
            or not 0 <= float(self.confidence) <= 1
        ):
            raise BlockContractError("alignment-group-confidence")
        if not isinstance(self.evidence, Mapping):
            raise BlockContractError("alignment-group-evidence")

    def to_dict(self) -> dict[str, object]:
        return {
            "source_anchor_ids": list(self.source_anchor_ids),
            "target_span_ids": list(self.target_span_ids),
            "method": self.method,
            "confidence": float(self.confidence),
            "review_state": self.review_state,
            "evidence": dict(self.evidence),
        }

    @property
    def group_hash(self) -> str:
        return sha256_text(canonical_json(self.to_dict()))


def spans_witness(projection: str, spans: Iterable[TargetSpan]) -> str:
    return sha256_text(
        canonical_json(
            {
                "projection_hash": sha256_text(projection),
                "spans": [span.to_dict() for span in spans],
            }
        )
    )


def groups_witness(groups: Iterable[AlignmentGroup]) -> str:
    return sha256_text(canonical_json([group.to_dict() for group in groups]))


def validate_span_partition(
    projection: str, spans: Iterable[TargetSpan]
) -> tuple[TargetSpan, ...]:
    ordered = tuple(spans)
    if not projection:
        if ordered:
            raise BlockContractError("alignment-empty-projection-spans")
        return ordered
    if not ordered:
        raise BlockContractError("alignment-projection-spans")
    cursor = 0
    for expected_order, span in enumerate(ordered, start=1):
        if (
            span.order != expected_order
            or span.start != cursor
            or span.end > len(projection)
        ):
            raise BlockContractError("alignment-span-partition")
        if sha256_text(projection[span.start : span.end]) != span.text_hash:
            raise BlockContractError("alignment-span-witness")
        cursor = span.end
    if cursor != len(projection):
        raise BlockContractError("alignment-span-partition")
    return ordered


def alignment_can_generate_sentence_memory(
    review_state: str, groups: Iterable[AlignmentGroup]
) -> bool:
    """Phase 4 policy only; Phase 5 owns actual sentence-memory derivation."""

    return review_state == "approved" and all(
        group.review_state == "approved"
        and group.confidence >= ALIGNMENT_CONFIDENCE_THRESHOLD
        for group in groups
    )


__all__ = [
    "ALIGNMENT_CONFIDENCE_PROFILE",
    "ALIGNMENT_CONFIDENCE_THRESHOLD",
    "TARGET_ALIGNMENT_CONTRACT_VERSION",
    "TARGET_SPLITTER_VERSION",
    "AlignmentGroup",
    "TargetSpan",
    "alignment_can_generate_sentence_memory",
    "groups_witness",
    "spans_witness",
    "target_alignment_projection",
    "target_alignment_scope",
    "validate_span_partition",
]
