"""Provider-independent, protected target-block result contracts.

The wire format deliberately has no HTML field.  Text is plain text and the
only syntax a provider may refer to is an existing protected-inline token ID.
That gives a model freedom to split and merge sentences without giving it a
second, unvalidated markup language.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .block_contract import (
    BlockContractError,
    ProtectedInlineToken,
    SourceSlot,
    TranslationBlock,
    TranslationSourcePlan,
    canonical_json,
    sha256_text,
)

TARGET_BLOCK_RESULT_CONTRACT_VERSION = "target-block-result-v1"
TARGET_BLOCK_VALIDATOR_VERSION = "target-block-validator-v1"
MAX_TARGET_PARTS = 20_000
MAX_TARGET_TEXT_BYTES = 512 * 1024
MAX_TARGET_LEXICAL_ATTRIBUTES = 512
MAX_LEXICAL_VALUE_BYTES = 32 * 1024
MAX_TARGET_LEXICAL_BYTES = 256 * 1024
TARGET_LOCAL_FORMAT_TAGS = frozenset(
    {"b", "em", "i", "strong", "sub", "sup", "u"}
)


def _mapping(value: object, code: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise BlockContractError(code)
    return value


def _keys(value: Mapping[str, Any], expected: set[str], code: str) -> None:
    if set(value) != expected:
        raise BlockContractError(code)


def _identifier(value: object, code: str, maximum: int = 128) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise BlockContractError(code)
    if len(value.encode("utf-8")) > maximum:
        raise BlockContractError(code)
    return value


def _plain_text(value: object, code: str, maximum: int) -> str:
    if not isinstance(value, str) or len(value.encode("utf-8")) > maximum:
        raise BlockContractError(code)
    # Newlines and tabs are legitimate prose/attribute whitespace. Everything
    # else in C0/C1 is an injection vector or a corrupt provider response.
    if any(
        (ord(char) < 32 and char not in "\t\n\r") or 127 <= ord(char) <= 159
        for char in value
    ):
        raise BlockContractError(code)
    return value


@dataclass(frozen=True, slots=True)
class TargetTextPart:
    """One text run, immutable source reference, or target-local format edge."""

    kind: str
    text: str | None = None
    token_id: str | None = None
    literal_slot_id: str | None = None
    format_tag: str | None = None
    format_boundary: str | None = None

    def __post_init__(self) -> None:
        if self.kind == "text":
            if (
                self.token_id is not None
                or self.literal_slot_id is not None
                or self.format_tag is not None
                or self.format_boundary is not None
            ):
                raise BlockContractError("target-part-text-shape")
            _plain_text(self.text, "target-part-text", MAX_TARGET_TEXT_BYTES)
        elif self.kind == "token":
            if (
                self.text is not None
                or self.literal_slot_id is not None
                or self.format_tag is not None
                or self.format_boundary is not None
            ):
                raise BlockContractError("target-part-token-shape")
            _identifier(self.token_id, "target-part-token")
        elif self.kind == "literal":
            if (
                self.text is not None
                or self.token_id is not None
                or self.format_tag is not None
                or self.format_boundary is not None
            ):
                raise BlockContractError("target-part-literal-shape")
            _identifier(self.literal_slot_id, "target-part-literal")
        elif self.kind == "format":
            if (
                self.text is not None
                or self.token_id is not None
                or self.literal_slot_id is not None
                or self.format_tag not in TARGET_LOCAL_FORMAT_TAGS
                or self.format_boundary not in {"open", "close"}
            ):
                raise BlockContractError("target-part-format-shape")
        else:
            raise BlockContractError("target-part-kind")

    def to_dict(self) -> dict[str, str]:
        if self.kind == "text":
            return {"kind": "text", "text": self.text or ""}
        if self.kind == "token":
            return {"kind": "token", "token_id": self.token_id or ""}
        if self.kind == "format":
            return {
                "kind": "format",
                "tag": self.format_tag or "",
                "boundary": self.format_boundary or "",
            }
        return {"kind": "literal", "literal_slot_id": self.literal_slot_id or ""}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> TargetTextPart:
        value = _mapping(value, "target-part-dto")
        kind = value.get("kind")
        if kind == "text":
            _keys(value, {"kind", "text"}, "target-part-dto-keys")
            return cls(kind="text", text=value["text"])
        if kind == "token":
            _keys(value, {"kind", "token_id"}, "target-part-dto-keys")
            return cls(kind="token", token_id=value["token_id"])
        if kind == "literal":
            _keys(value, {"kind", "literal_slot_id"}, "target-part-dto-keys")
            return cls(kind="literal", literal_slot_id=value["literal_slot_id"])
        if kind == "format":
            _keys(value, {"kind", "tag", "boundary"}, "target-part-dto-keys")
            return cls(
                kind="format",
                format_tag=value["tag"],
                format_boundary=value["boundary"],
            )
        raise BlockContractError("target-part-kind")


@dataclass(frozen=True, slots=True)
class TargetLexicalAttribute:
    """Plain-text value for exactly one declared lexical source slot."""

    slot_id: str
    value: str

    def __post_init__(self) -> None:
        _identifier(self.slot_id, "target-lexical-slot")
        _plain_text(self.value, "target-lexical-value", MAX_LEXICAL_VALUE_BYTES)
        if not self.value.strip():
            raise BlockContractError("target-lexical-empty")

    def to_dict(self) -> dict[str, str]:
        return {"slot_id": self.slot_id, "value": self.value}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> TargetLexicalAttribute:
        value = _mapping(value, "target-lexical-dto")
        _keys(value, {"slot_id", "value"}, "target-lexical-dto-keys")
        return cls(slot_id=value["slot_id"], value=value["value"])


_BLOCK_MAP_INDEX_NAME = "block-map-v1"
_FINGERPRINT_MEMO_INDEX_NAME = "target-structural-fingerprint-memo-v1"


def _canonical_plan_block(
    plan: TranslationSourcePlan, block: TranslationBlock
) -> TranslationBlock:
    """Return the canonical plan-owned block equal to ``block``.

    This preserves the exact ``block in plan.blocks`` equality semantics
    without the linear tuple scan: the ID index narrows to at most one
    candidate, then one dataclass equality check decides. Same-ID altered
    content therefore rejects, while an equal dataclass clone accepts, exactly
    as before. Any non-member, unknown ID, or cross-plan content rejects with
    the same ``target-fingerprint-block`` code.
    """

    from .source_indexes import source_index

    def _build_block_map() -> dict[str, TranslationBlock]:
        return {candidate.id: candidate for candidate in plan.blocks}

    block_map = source_index(plan, _BLOCK_MAP_INDEX_NAME, _build_block_map)
    block_id = getattr(block, "id", None)
    if not isinstance(block_id, str):
        raise BlockContractError("target-fingerprint-block")
    try:
        canonical = block_map[block_id]
    except KeyError:
        raise BlockContractError("target-fingerprint-block") from None
    except TypeError:
        raise BlockContractError("target-fingerprint-block") from None
    if canonical is not block and canonical != block:
        raise BlockContractError("target-fingerprint-block")
    return canonical


def _compute_structural_fingerprint(
    plan: TranslationSourcePlan, canonical: TranslationBlock
) -> str:
    """Compute one fingerprint without touching any memo."""

    return sha256_text(
        canonical_json(
            {
                "plan_id": plan.plan_hash,
                "plan_contract": plan.contract_version,
                "snapshot_contract": plan.snapshot.semantic_contract_version,
                "snapshot_hash": plan.snapshot.content_hash,
                "planner_version": plan.planner_version,
                "planner_settings": plan.settings_fingerprint,
                "block_id": canonical.id,
                "role": canonical.role,
                "locator": canonical.locator,
                "slots": [
                    {
                        "id": slot.id,
                        "kind": slot.kind,
                        "disposition": slot.disposition,
                        "policy_code": slot.policy_code,
                        "source_hash": slot.source_hash,
                    }
                    for slot in canonical.slots
                ],
                "tokens": [
                    {
                        "id": token.id,
                        "kind": token.kind,
                        "tag": token.tag,
                        "locator": token.locator,
                        "movement_policy": token.movement_policy,
                        "element_id": token.element_id,
                        "source_hash": token.source_hash,
                    }
                    for token in canonical.protected_inline
                ],
                "wire": [part.to_dict() for part in canonical.wire],
            }
        )
    )


def structural_fingerprint(plan: TranslationSourcePlan, block: TranslationBlock) -> str:
    """Hash every structural rule a candidate is allowed to depend on.

    Source strings are represented by their existing hashes; neither candidate
    DTOs nor finding objects need to carry source text.

    Validation is indexed: the same-plan invocation-local ``block-map-v1``
    narrows membership to one ID lookup plus one dataclass equality check,
    preserving the exact ``block in plan.blocks`` accept/reject semantics.
    Digests are memoized lazily per block ID inside the same-plan
    invocation-local scope only. No fingerprint is precomputed for blocks that
    are never requested, warm entries never mask same-ID altered content
    (equality is checked before any memo hit), and nothing is retained beyond
    the outer assembly invocation or shared across plans.
    """

    from .source_indexes import source_index

    canonical = _canonical_plan_block(plan, block)

    def _new_memo() -> dict[str, str]:
        return {}

    memo: dict[str, str] = source_index(plan, _FINGERPRINT_MEMO_INDEX_NAME, _new_memo)
    cached = memo.get(canonical.id)
    if cached is not None:
        return cached
    fingerprint = _compute_structural_fingerprint(plan, canonical)
    memo[canonical.id] = fingerprint
    return fingerprint


@dataclass(frozen=True, slots=True)
class TargetBlockCandidate:
    """Strict normalized provider output for one requested source block."""

    source_plan_id: str
    source_plan_hash: str
    block_id: str
    structural_fingerprint: str
    parts: tuple[TargetTextPart, ...]
    lexical_attributes: tuple[TargetLexicalAttribute, ...]
    contract_version: str = TARGET_BLOCK_RESULT_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != TARGET_BLOCK_RESULT_CONTRACT_VERSION:
            raise BlockContractError("target-contract-version")
        _identifier(self.source_plan_id, "target-plan-id", 36)
        _identifier(self.block_id, "target-block-id")
        for value, code in (
            (self.source_plan_hash, "target-plan-hash"),
            (self.structural_fingerprint, "target-structural-fingerprint"),
        ):
            if (
                not isinstance(value, str)
                or len(value) != 64
                or any(char not in "0123456789abcdef" for char in value)
            ):
                raise BlockContractError(code)
        if not isinstance(self.parts, tuple) or len(self.parts) > MAX_TARGET_PARTS:
            raise BlockContractError("target-parts")
        if not all(isinstance(part, TargetTextPart) for part in self.parts):
            raise BlockContractError("target-parts")
        if (
            sum(
                len((part.text or "").encode("utf-8"))
                for part in self.parts
                if part.kind == "text"
            )
            > MAX_TARGET_TEXT_BYTES
        ):
            raise BlockContractError("target-text-size")
        if (
            not isinstance(self.lexical_attributes, tuple)
            or len(self.lexical_attributes) > MAX_TARGET_LEXICAL_ATTRIBUTES
        ):
            raise BlockContractError("target-lexical")
        if not all(
            isinstance(item, TargetLexicalAttribute) for item in self.lexical_attributes
        ):
            raise BlockContractError("target-lexical")
        if (
            sum(len(item.value.encode("utf-8")) for item in self.lexical_attributes)
            > MAX_TARGET_LEXICAL_BYTES
        ):
            raise BlockContractError("target-lexical-size")

    def to_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "source_plan_id": self.source_plan_id,
            "source_plan_hash": self.source_plan_hash,
            "block_id": self.block_id,
            "structural_fingerprint": self.structural_fingerprint,
            "parts": [part.to_dict() for part in self.parts],
            "lexical_attributes": [item.to_dict() for item in self.lexical_attributes],
        }

    @property
    def candidate_hash(self) -> str:
        return sha256_text(canonical_json(self.to_dict()))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> TargetBlockCandidate:
        value = _mapping(value, "target-candidate-dto")
        _keys(
            value,
            {
                "contract_version",
                "source_plan_id",
                "source_plan_hash",
                "block_id",
                "structural_fingerprint",
                "parts",
                "lexical_attributes",
            },
            "target-candidate-dto-keys",
        )
        if not isinstance(value["parts"], list) or not isinstance(
            value["lexical_attributes"], list
        ):
            raise BlockContractError("target-candidate-children")
        return cls(
            contract_version=value["contract_version"],
            source_plan_id=value["source_plan_id"],
            source_plan_hash=value["source_plan_hash"],
            block_id=value["block_id"],
            structural_fingerprint=value["structural_fingerprint"],
            parts=tuple(TargetTextPart.from_dict(item) for item in value["parts"]),
            lexical_attributes=tuple(
                TargetLexicalAttribute.from_dict(item)
                for item in value["lexical_attributes"]
            ),
        )


def normalize_target_candidate(
    value: Mapping[str, Any] | TargetBlockCandidate,
) -> TargetBlockCandidate:
    """Reparse any provider object through the exact-key durable boundary."""

    if isinstance(value, TargetBlockCandidate):
        value = value.to_dict()
    return TargetBlockCandidate.from_dict(value)


def target_text(candidate: TargetBlockCandidate) -> str:
    return "".join(part.text or "" for part in candidate.parts if part.kind == "text")


def text_slots(block: TranslationBlock) -> tuple[SourceSlot, ...]:
    return tuple(
        slot
        for slot in block.slots
        if slot.kind != "attribute" and slot.disposition == "translatable"
    )


def lexical_slots(block: TranslationBlock) -> tuple[SourceSlot, ...]:
    return tuple(
        slot
        for slot in block.slots
        if slot.kind == "attribute" and slot.disposition == "translatable"
    )


def literal_reference_slots(block: TranslationBlock) -> tuple[SourceSlot, ...]:
    """Return exact source-owned text spans represented by target references.

    Literal-preserved prose and structural-only whitespace are both immutable
    reconstruction data. The provider may position only their existing IDs;
    the renderer restores their source values from the canonical plan.
    """

    wire_slot_ids = {part.ref_id for part in block.wire if part.kind == "slot"}
    return tuple(
        slot
        for slot in block.slots
        if slot.id in wire_slot_ids
        and slot.kind != "attribute"
        and slot.disposition in {"literal-preserved", "structural-only"}
    )


def token_map(block: TranslationBlock) -> dict[str, ProtectedInlineToken]:
    return {
        token.id: token
        for token in block.protected_inline
        if token.movement_policy != "source-only"
    }


__all__ = [
    "MAX_LEXICAL_VALUE_BYTES",
    "MAX_TARGET_LEXICAL_ATTRIBUTES",
    "MAX_TARGET_LEXICAL_BYTES",
    "MAX_TARGET_PARTS",
    "MAX_TARGET_TEXT_BYTES",
    "TARGET_BLOCK_RESULT_CONTRACT_VERSION",
    "TARGET_BLOCK_VALIDATOR_VERSION",
    "TARGET_LOCAL_FORMAT_TAGS",
    "TargetBlockCandidate",
    "TargetLexicalAttribute",
    "TargetTextPart",
    "lexical_slots",
    "literal_reference_slots",
    "normalize_target_candidate",
    "structural_fingerprint",
    "target_text",
    "text_slots",
    "token_map",
]
