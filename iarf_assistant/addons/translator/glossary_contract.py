"""Versioned morphology-aware terminology metadata and bounded MCP inputs.

Canonical headwords identify concepts. Observed surface forms are model claims
about that concept, not machine-certified morphology. Exact occurrence positions
are derived and checked separately against the immutable source projection.
"""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


GlossarySourceForm = Annotated[str, Field(min_length=1, max_length=4096)]
GlossaryTargetForm = Annotated[str, Field(min_length=1, max_length=4096)]


class GlossaryOccurrenceInput(BaseModel):
    """Locate an exact source quote without asking an executor to count characters."""

    model_config = ConfigDict(extra="forbid", strict=True)
    block_id: str = Field(min_length=1, max_length=128)
    quote: str = Field(min_length=1, max_length=4096)
    occurrence_index: int | None = Field(
        None, ge=1, le=4096,
        description="One-based occurrence of the exact quote; required if it repeats in this block.",
    )
    anchor_id: str | None = Field(None, min_length=1, max_length=128)


class GlossaryTermInput(BaseModel):
    """A complete lexical revision, with newly observed forms/occurrences.

    On updates, supply the current concept_id and expected_revision. The lexical
    fields replace the previous revision. Observed forms accumulate unless an
    explicit replacement source_forms list is supplied; old occurrences remain
    historical evidence and are never rewritten.
    """

    model_config = ConfigDict(extra="forbid", strict=True)
    source_term: str = Field(min_length=1, max_length=4096, description="Canonical dictionary/citation form, not necessarily the form printed in the source.")
    target_term: str = Field(min_length=1, max_length=4096, description="Preferred lexical rendering; inflect grammatically unless match_policy is exact.")
    sense: str = Field("", max_length=1000, description="A short contextual sense, e.g. religious congregation versus municipality.")
    notes: str | None = Field(None, max_length=8192)
    match_policy: Literal["preferred", "exact"] = "preferred"
    concept_id: str | None = Field(None, min_length=1, max_length=128)
    expected_revision: int = Field(0, ge=0)
    source_forms: list[GlossarySourceForm] | None = Field(None, max_length=128, description="Known source spellings/inflections. Omit to retain existing forms; a supplied list replaces them. Anchored quotes are always added.")
    accepted_target_forms: list[GlossaryTargetForm] | None = Field(None, max_length=128, description="Accepted contextual target renderings in addition to target_term. Omit to retain existing variants; a supplied list replaces them.")
    occurrences: list[GlossaryOccurrenceInput] = Field(default_factory=list, max_length=64)


class GlossaryObservation(BaseModel):
    """Server-resolved source evidence retained with one lexical revision."""

    model_config = ConfigDict(extra="forbid", strict=True)
    block_id: str = Field(min_length=1, max_length=128)
    anchor_id: str | None = Field(None, min_length=1, max_length=128)
    start_offset: int = Field(ge=0)
    end_offset: int = Field(ge=1)
    surface_form: str = Field(min_length=1, max_length=4096)


class GlossaryUsageV1(BaseModel):
    """Legacy receipt-bound usage payload. Its serialized shape is immutable."""

    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal["translator-glossary-usage-v1"] = "translator-glossary-usage-v1"
    sense: str = Field("", max_length=1000)
    match_policy: Literal["preferred", "exact"] = "preferred"
    source_forms: list[GlossarySourceForm] = Field(default_factory=list, max_length=128)
    observations: list[GlossaryObservation] = Field(default_factory=list, max_length=64)
    provenance: str = Field("passive-executor", min_length=1, max_length=1000)
    request_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")


class GlossaryUsage(BaseModel):
    """Current receipt-bound usage payload with accepted target variants."""

    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal["translator-glossary-usage-v2"] = "translator-glossary-usage-v2"
    sense: str = Field("", max_length=1000)
    match_policy: Literal["preferred", "exact"] = "preferred"
    source_forms: list[GlossarySourceForm] = Field(default_factory=list, max_length=128)
    accepted_target_forms: list[GlossaryTargetForm] = Field(default_factory=list, max_length=128)
    observations: list[GlossaryObservation] = Field(default_factory=list, max_length=64)
    provenance: str = Field("passive-executor", min_length=1, max_length=1000)
    request_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")


def validate_glossary_usage(value: object) -> GlossaryUsageV1 | GlossaryUsage:
    if not isinstance(value, dict):
        raise ValueError("glossary usage must be an object")
    version = value.get("schema_version", "translator-glossary-usage-v1")
    if version == "translator-glossary-usage-v1":
        return GlossaryUsageV1.model_validate(value)
    if version == "translator-glossary-usage-v2":
        return GlossaryUsage.model_validate(value)
    raise ValueError("unsupported glossary usage schema")
