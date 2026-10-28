"""Pure deterministic validation for protected target block candidates."""

from __future__ import annotations

from dataclasses import dataclass

from .block_contract import (
    BlockContractError,
    ProtectedInlineToken,
    TranslationBlock,
    TranslationSourcePlan,
    canonical_json,
    sha256_text,
)
from .target_contract import (
    TARGET_BLOCK_VALIDATOR_VERSION,
    TargetBlockCandidate,
    lexical_slots,
    literal_reference_slots,
    structural_fingerprint,
    target_text,
    text_slots,
    token_map,
)


@dataclass(frozen=True, slots=True)
class TargetValidationFinding:
    """A bounded, source-free failure code suitable for durable evidence."""

    code: str
    severity: str = "fatal"

    def __post_init__(self) -> None:
        if self.severity not in {"fatal", "report"}:
            raise BlockContractError("target-finding-severity")

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "severity": self.severity}


@dataclass(frozen=True, slots=True)
class TargetValidationResult:
    candidate: TargetBlockCandidate
    findings: tuple[TargetValidationFinding, ...]
    validator_version: str = TARGET_BLOCK_VALIDATOR_VERSION

    @property
    def valid(self) -> bool:
        return not self.findings

    @property
    def normalized_hash(self) -> str:
        return sha256_text(canonical_json(self.candidate.to_dict()))

    def evidence(self) -> dict[str, object]:
        return {
            "validator_version": self.validator_version,
            "valid": self.valid,
            "findings": [finding.to_dict() for finding in self.findings],
            "normalized_hash": self.normalized_hash,
        }


class TargetCandidateValidationError(BlockContractError):
    """Carry bounded, source-free findings across the normalization boundary."""

    def __init__(self, findings: tuple[TargetValidationFinding, ...]):
        self.finding_codes = tuple(finding.code for finding in findings)
        super().__init__("target-validation-failed")


def _finding(codes: set[str], code: str) -> None:
    # Do not expose a provider-supplied ID or any source fragment in evidence.
    codes.add(code)


def _source_token_order(block: TranslationBlock) -> list[str]:
    target_tokens = token_map(block)
    return [
        token.id
        for token in sorted(
            block.protected_inline,
            key=lambda item: (item.raw_start, item.raw_end, item.id),
        )
        if token.id in target_tokens
    ]


def _validate_token_nesting(
    block: TranslationBlock, candidate: TargetBlockCandidate, codes: set[str]
) -> None:
    tokens = token_map(block)
    observed = [part.token_id for part in candidate.parts if part.kind == "token"]
    if (
        len(observed) != len(tokens)
        or set(observed) != set(tokens)
        or len(observed) != len(set(observed))
    ):
        _finding(codes, "target-token-cardinality")
        return
    fixed_expected = [
        token_id
        for token_id in _source_token_order(block)
        if tokens[token_id].movement_policy == "fixed"
    ]
    fixed_observed = [
        token_id
        for token_id in observed
        if token_id is not None and tokens[token_id].movement_policy == "fixed"
    ]
    if fixed_observed != fixed_expected:
        _finding(codes, "target-fixed-token-order")

    source_scopes = _fixed_token_scopes(tokens, _source_token_order(block))
    stack: list[str] = []
    for token_id in observed:
        if token_id is None:
            _finding(codes, "target-token-cardinality")
            return
        token = tokens[token_id]
        if token.kind == "open":
            stack.append(token.element_id)
        elif token.kind == "close":
            if not stack or stack.pop() != token.element_id:
                _finding(codes, "target-token-nesting")
                return
        elif token.kind != "void":  # defensive: DTO construction already checks.
            _finding(codes, "target-token-kind")
            return
    if stack:
        _finding(codes, "target-token-nesting")
        return
    candidate_scopes = _fixed_token_scopes(
        tokens, [token_id for token_id in observed if token_id is not None]
    )
    if any(
        token.movement_policy == "movable"
        and candidate_scopes[token.id] != source_scopes[token.id]
        for token in tokens.values()
    ):
        # Movable emphasis may change position inside a reference, but it
        # cannot escape a fixed link/noteref/line-break scope.
        _finding(codes, "target-fixed-token-scope")


def _validate_combined_nesting(
    block: TranslationBlock, candidate: TargetBlockCandidate, codes: set[str]
) -> None:
    """Require source-owned elements and target-local formatting to nest cleanly."""

    tokens = token_map(block)
    literals = {slot.id: slot for slot in literal_reference_slots(block)}
    stack: list[tuple[str, str, int]] = []
    visible_offset = 0
    for part in candidate.parts:
        if part.kind == "text":
            visible_offset += len(part.text or "")
            continue
        if part.kind == "literal":
            literal = literals.get(part.literal_slot_id or "")
            if literal is not None:
                visible_offset += len(literal.source_value)
            continue
        if part.kind == "format":
            tag = part.format_tag or ""
            if part.format_boundary == "open":
                stack.append(("format", tag, visible_offset))
            elif not stack or stack[-1][0] != "format" or stack[-1][1] != tag:
                _finding(codes, "target-format-nesting")
                return
            else:
                _kind, _tag, start = stack.pop()
                if visible_offset <= start:
                    _finding(codes, "target-format-empty")
            continue
        token = tokens.get(part.token_id or "")
        if token is None:
            continue
        if token.kind == "open":
            stack.append(("source", token.element_id, visible_offset))
        elif token.kind == "close":
            if (
                not stack
                or stack[-1][0] != "source"
                or stack[-1][1] != token.element_id
            ):
                _finding(codes, "target-format-nesting")
                return
            stack.pop()
    if stack:
        _finding(codes, "target-format-nesting")


def _fixed_token_scopes(
    tokens: dict[str, ProtectedInlineToken], token_ids: list[str]
) -> dict[str, tuple[str, ...]]:
    """Return each token's fixed-element ancestry for a valid token stream."""

    stack: list[ProtectedInlineToken] = []
    scopes: dict[str, tuple[str, ...]] = {}
    for token_id in token_ids:
        token = tokens[token_id]
        scopes[token_id] = tuple(
            item.element_id for item in stack if item.movement_policy == "fixed"
        )
        if token.kind == "open":
            stack.append(token)
        elif token.kind == "close":
            stack.pop()
    return scopes


def _validate_literal_policy(
    block: TranslationBlock, candidate: TargetBlockCandidate, codes: set[str]
) -> None:
    """Require an exact, non-model-writable literal-slot reference once."""

    expected = [
        slot.id
        for slot in sorted(
            literal_reference_slots(block),
            key=lambda item: (item.raw_start, item.raw_end, item.id),
        )
    ]
    observed = [
        part.literal_slot_id for part in candidate.parts if part.kind == "literal"
    ]
    if observed != expected:
        _finding(codes, "target-literal-preservation")


def _validate_fixed_reference_order(
    block: TranslationBlock,
    candidate: TargetBlockCandidate,
    codes: set[str],
) -> None:
    """Preserve literal refs relative to fixed inline references.

    Checking fixed tokens and literal slots in separate streams lets a literal
    escape a fixed link/noteref while both individual checks still pass.  The
    source wire is the canonical raw-order witness for the combined stream.
    """

    slots = {slot.id: slot for slot in block.slots}
    tokens = token_map(block)
    literal_reference_ids = {slot.id for slot in literal_reference_slots(block)}
    expected: list[tuple[str, str]] = []
    for wire_part in block.wire:
        if wire_part.kind == "token":
            token = tokens.get(wire_part.ref_id)
            if token is None:
                continue
            if token.movement_policy == "fixed":
                expected.append(("token", token.id))
        else:
            slot = slots[wire_part.ref_id]
            if slot.id in literal_reference_ids:
                expected.append(("literal", slot.id))
    observed = [
        ("token", part.token_id or "")
        if part.kind == "token"
        and tokens.get(part.token_id or "")
        and tokens[part.token_id or ""].movement_policy == "fixed"
        else ("literal", part.literal_slot_id or "")
        for part in candidate.parts
        if part.kind == "literal"
        or (
            part.kind == "token"
            and tokens.get(part.token_id or "")
            and tokens[part.token_id or ""].movement_policy == "fixed"
        )
    ]
    if observed != expected:
        _finding(codes, "target-fixed-reference-order")


def validate_target_candidate(
    plan: TranslationSourcePlan,
    plan_id: str,
    block: TranslationBlock,
    candidate: TargetBlockCandidate,
) -> TargetValidationResult:
    """Validate without parsing, repairing, or otherwise changing candidate data."""

    codes: set[str] = set()
    if block not in plan.blocks:
        _finding(codes, "target-block-not-owned")
    if candidate.source_plan_id != plan_id:
        _finding(codes, "target-plan-id")
    if candidate.source_plan_hash != plan.plan_hash:
        _finding(codes, "target-plan-hash")
    if candidate.block_id != block.id:
        _finding(codes, "target-block-id")
    if candidate.structural_fingerprint != structural_fingerprint(plan, block):
        _finding(codes, "target-structural-fingerprint")

    declared_lexical_slots = lexical_slots(block)
    declared_lexical = {slot.id for slot in declared_lexical_slots}
    supplied_lexical = [item.slot_id for item in candidate.lexical_attributes]
    if len(supplied_lexical) != len(set(supplied_lexical)):
        _finding(codes, "target-lexical-duplicate")
    if set(supplied_lexical) != declared_lexical:
        _finding(codes, "target-lexical-cardinality")
    elif supplied_lexical != [slot.id for slot in declared_lexical_slots]:
        # Attributes are not prose ordering, but canonical ordering makes a
        # normalized candidate hash stable and prevents an adapter from using
        # a second, undocumented ordering convention.
        _finding(codes, "target-lexical-order")

    if text_slots(block):
        plain = target_text(candidate)
        if not plain.strip():
            _finding(codes, "target-text-empty")
    elif any(
        part.kind == "text" and (part.text or "").strip() for part in candidate.parts
    ):
        _finding(codes, "target-text-unowned")

    _validate_token_nesting(block, candidate, codes)
    _validate_combined_nesting(block, candidate, codes)
    _validate_literal_policy(block, candidate, codes)
    _validate_fixed_reference_order(block, candidate, codes)
    return TargetValidationResult(
        candidate=candidate,
        findings=tuple(TargetValidationFinding(code) for code in sorted(codes)),
    )


def require_valid_target_candidate(
    plan: TranslationSourcePlan,
    plan_id: str,
    block: TranslationBlock,
    candidate: TargetBlockCandidate,
) -> TargetValidationResult:
    result = validate_target_candidate(plan, plan_id, block, candidate)
    if not result.valid:
        raise TargetCandidateValidationError(result.findings)
    return result


__all__ = [
    "TARGET_BLOCK_VALIDATOR_VERSION",
    "TargetCandidateValidationError",
    "TargetValidationFinding",
    "TargetValidationResult",
    "require_valid_target_candidate",
    "validate_target_candidate",
]
