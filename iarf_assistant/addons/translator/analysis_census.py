"""Deterministic semantic census over a rehydrated immutable source plan."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .analysis_contract import (
    ANALYSIS_CENSUS_PROFILE,
    ANALYSIS_CENSUS_VERSION,
    private_witness,
    private_witness_candidates,
)
from .block_contract import (
    BlockContractError,
    CoverageEntry,
    TranslationSourcePlan,
    canonical_json,
    sha256_text,
)

_COVERAGE_IDENTITY_PURPOSE = "translator-semantic-coverage-identity-v1"


def coverage_identity_material(entry: CoverageEntry) -> dict[str, object]:
    """Return the complete, non-repairable identity material for one witness.

    ``source_hash`` is source-derived and therefore must never be placed in a
    plaintext occurrence identifier.  It remains part of the authenticated
    keyed witness so the census can distinguish equal structural spans whose
    source fragments differ without exposing an offline-guessable digest.
    """

    return {
        "locator": entry.locator,
        "kind": entry.kind,
        "disposition": entry.disposition,
        "policy_code": entry.policy_code,
        "raw_start": entry.raw_start,
        "raw_end": entry.raw_end,
        "source_hash": entry.source_hash,
        "block_id": entry.block_id,
    }


def coverage_occurrence_id(entry: CoverageEntry) -> str:
    """Create the opaque persisted identity for a coverage-only occurrence."""

    return "coverage:" + private_witness(
        coverage_identity_material(entry), purpose=_COVERAGE_IDENTITY_PURPOSE
    )


def coverage_occurrence_id_candidates(entry: CoverageEntry) -> tuple[str, ...]:
    """Return active- and retained-key identities for one coverage witness."""

    return tuple(
        "coverage:" + candidate
        for candidate in private_witness_candidates(
            coverage_identity_material(entry), purpose=_COVERAGE_IDENTITY_PURPOSE
        )
    )


@dataclass(frozen=True, slots=True)
class CensusOccurrence:
    block_id: str | None
    anchor_id: str | None
    occurrence_kind: str
    slot_id: str | None
    slot_kind: str | None
    disposition: str | None
    start_offset: int | None
    end_offset: int | None
    value: str
    structural: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        return {
            "block_id": self.block_id,
            "anchor_id": self.anchor_id,
            "kind": self.occurrence_kind,
            "slot_id": self.slot_id,
            "slot_kind": self.slot_kind,
            "disposition": self.disposition,
            "start_offset": self.start_offset,
            "end_offset": self.end_offset,
            "value_hash": private_witness(
                self.value, purpose="translator-census-value-v1"
            ),
            "structural": self.structural,
        }


@dataclass(frozen=True, slots=True)
class SemanticCensus:
    source_plan_hash: str
    profile_id: str
    profile_version: str
    occurrences: tuple[CensusOccurrence, ...]

    @staticmethod
    def _logical_structure(value: object) -> object:
        """Retain structure while excluding source-derived hash witnesses."""

        if isinstance(value, Mapping):
            return {
                key: SemanticCensus._logical_structure(item)
                for key, item in value.items()
                if key not in {"source_hash", "coverage_hash", "content_hash"}
            }
        if isinstance(value, (list, tuple)):
            return [SemanticCensus._logical_structure(item) for item in value]
        return value

    @classmethod
    def _logical_occurrence_material(
        cls, item: CensusOccurrence, order_index: int
    ) -> dict[str, object]:
        # The keyed coverage slot ID and source-bearing value are deliberately
        # absent.  Plan identity and ordered structural coordinates already
        # establish the logical census identity; encrypted witnesses below
        # establish exact source truth.
        return {
            "order_index": order_index,
            "block_id": item.block_id,
            "anchor_id": item.anchor_id,
            "kind": item.occurrence_kind,
            "slot_kind": item.slot_kind,
            "disposition": item.disposition,
            "start_offset": item.start_offset,
            "end_offset": item.end_offset,
            "structural": cls._logical_structure(item.structural),
        }

    @property
    def logical_coverage_hash(self) -> str:
        return sha256_text(
            canonical_json(
                [
                    self._logical_occurrence_material(item, index)
                    for index, item in enumerate(self.occurrences, 1)
                ]
            )
        )

    @property
    def coverage_hash(self) -> str:
        # Do not persist a plain digest of source-derived values.  The outer
        # SHA only fixes storage width; its input is a keyed opaque witness.
        return private_witness(
            [
                {
                    "block_id": item.block_id,
                    "anchor_id": item.anchor_id,
                    "kind": item.occurrence_kind,
                    "slot_id": item.slot_id,
                    "slot_kind": item.slot_kind,
                    "disposition": item.disposition,
                    "start_offset": item.start_offset,
                    "end_offset": item.end_offset,
                    "structural": item.structural,
                    "value": item.value,
                }
                for item in self.occurrences
            ],
            purpose="translator-census-coverage-v1",
        )

    @property
    def census_hash(self) -> str:
        return sha256_text(
            canonical_json(
                {
                    "source_plan_hash": self.source_plan_hash,
                    "profile_id": self.profile_id,
                    "profile_version": self.profile_version,
                    "logical_coverage_hash": self.logical_coverage_hash,
                }
            )
        )


def build_semantic_census(
    plan: TranslationSourcePlan,
    *,
    profile_id: str = ANALYSIS_CENSUS_PROFILE,
    profile_version: str = ANALYSIS_CENSUS_VERSION,
) -> SemanticCensus:
    """Cover every source slot, token, and anchor without flattening evidence.

    A census is an indexable *description* of plan truth. It never chooses a
    replacement source string or emits legacy segment/chunk rows.
    """

    if (
        not isinstance(plan, TranslationSourcePlan)
        or profile_id != ANALYSIS_CENSUS_PROFILE
        or profile_version != ANALYSIS_CENSUS_VERSION
    ):
        raise BlockContractError("semantic-census-profile")
    rows: list[CensusOccurrence] = []
    for block in plan.blocks:
        relationship = {
            "role": block.role,
            "locator": block.locator,
            "block_order": block.order,
        }
        for slot in block.slots:
            rows.append(
                CensusOccurrence(
                    block.id,
                    None,
                    "slot",
                    slot.id,
                    slot.kind,
                    slot.disposition,
                    slot.projection_start,
                    slot.projection_end,
                    slot.source_value,
                    {
                        **relationship,
                        "policy_code": slot.policy_code,
                        "raw_start": slot.raw_start,
                        "raw_end": slot.raw_end,
                        "slot_locator": slot.locator,
                    },
                )
            )
        for token in block.protected_inline:
            if token.movement_policy == "source-only":
                continue
            rows.append(
                CensusOccurrence(
                    block.id,
                    None,
                    "protected_inline",
                    token.id,
                    "protected_inline",
                    token.movement_policy,
                    None,
                    None,
                    token.source_value,
                    {
                        **relationship,
                        "tag": token.tag,
                        "token_kind": token.kind,
                        "element_id": token.element_id,
                        "raw_start": token.raw_start,
                        "raw_end": token.raw_end,
                        "token_locator": token.locator,
                    },
                )
            )
        for anchor in block.anchors:
            rows.append(
                CensusOccurrence(
                    block.id,
                    anchor.id,
                    "anchor",
                    None,
                    None,
                    None,
                    anchor.start,
                    anchor.end,
                    block.projection[anchor.start : anchor.end],
                    {
                        **relationship,
                        "anchor_order": anchor.order,
                        "splitter_profile": anchor.splitter_profile,
                        "splitter_version": anchor.splitter_version,
                    },
                )
            )
    # Blocks intentionally omit structural and excluded regions.  The verified
    # coverage witness does not: preserving it here makes the census an exact
    # account of the source plan without pretending those facts are terms.
    for entry in plan.coverage.entries:
        rows.append(
            CensusOccurrence(
                entry.block_id,
                None,
                "coverage",
                coverage_occurrence_id(entry),
                entry.kind,
                entry.disposition,
                entry.raw_start,
                entry.raw_end,
                plan.snapshot.output_html[entry.raw_start : entry.raw_end],
                {
                    "locator": entry.locator,
                    "policy_code": entry.policy_code,
                    "raw_start": entry.raw_start,
                    "raw_end": entry.raw_end,
                    "source_hash": entry.source_hash,
                    "coverage_hash": plan.coverage.content_hash,
                    "terminology_eligible": False,
                },
            )
        )
    if not rows:
        raise BlockContractError("semantic-census-empty")
    return SemanticCensus(plan.plan_hash, profile_id, profile_version, tuple(rows))


__all__ = [
    "CensusOccurrence",
    "SemanticCensus",
    "build_semantic_census",
    "coverage_identity_material",
    "coverage_occurrence_id",
    "coverage_occurrence_id_candidates",
]
