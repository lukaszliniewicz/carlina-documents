"""Versioned morphology-aware terminology metadata and bounded MCP inputs.

Canonical headwords identify concepts. Observed surface forms are model claims
about that concept, not machine-certified morphology. Exact occurrence positions
are derived and checked separately against the immutable source projection.
"""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


GlossarySourceForm = Annotated[str, Field(min_length=1, max_length=4096)]
GlossaryTargetForm = Annotated[str, Field(min_length=1, max_length=4096)]
GLOSSARY_KINDS = ("term", "name", "title", "formula", "do_not_translate")
GLOSSARY_VERIFICATION = ("verified", "unverified", "coined")
GlossaryKind = Literal["term", "name", "title", "formula", "do_not_translate"]
GlossaryVerification = Literal["verified", "unverified", "coined"]


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
    kind: GlossaryKind | None = Field(
        None,
        description=(
            "term, name (person, place, organisation), title (of a work), formula (a fixed "
            "liturgical, legal or set phrase) or do_not_translate (target_term equals "
            "source_term). Omit to keep the current kind; new entries default to term."
        ),
    )
    verification: GlossaryVerification | None = Field(
        None,
        description=(
            "verified: the rendering was checked against a reference edition, authority or "
            "established usage; unverified: not checked, listed before export; coined: no "
            "established rendering exists and this one is the translator's own. Omit to keep "
            "the current state; new entries default to not stated."
        ),
    )

    @model_validator(mode="after")
    def _untranslated_terms_keep_their_form(self) -> GlossaryTermInput:
        if self.kind == "do_not_translate" and self.target_term != self.source_term:
            raise ValueError("a do_not_translate entry must use source_term as target_term")
        return self


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


class GlossaryUsageV2(BaseModel):
    """Receipt-bound usage payload with accepted target variants. Immutable shape."""

    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal["translator-glossary-usage-v2"] = "translator-glossary-usage-v2"
    sense: str = Field("", max_length=1000)
    match_policy: Literal["preferred", "exact"] = "preferred"
    source_forms: list[GlossarySourceForm] = Field(default_factory=list, max_length=128)
    accepted_target_forms: list[GlossaryTargetForm] = Field(default_factory=list, max_length=128)
    observations: list[GlossaryObservation] = Field(default_factory=list, max_length=64)
    provenance: str = Field("passive-executor", min_length=1, max_length=1000)
    request_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")


class GlossaryUsage(BaseModel):
    """Current receipt-bound usage payload: v2 plus kind and verification."""

    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal["translator-glossary-usage-v3"] = "translator-glossary-usage-v3"
    sense: str = Field("", max_length=1000)
    match_policy: Literal["preferred", "exact"] = "preferred"
    source_forms: list[GlossarySourceForm] = Field(default_factory=list, max_length=128)
    accepted_target_forms: list[GlossaryTargetForm] = Field(default_factory=list, max_length=128)
    observations: list[GlossaryObservation] = Field(default_factory=list, max_length=64)
    provenance: str = Field("passive-executor", min_length=1, max_length=1000)
    request_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    kind: GlossaryKind = "term"
    verification: GlossaryVerification | None = None


def validate_glossary_usage(
    value: object,
) -> GlossaryUsageV1 | GlossaryUsageV2 | GlossaryUsage:
    if not isinstance(value, dict):
        raise ValueError("glossary usage must be an object")
    version = value.get("schema_version", "translator-glossary-usage-v1")
    if version == "translator-glossary-usage-v1":
        return GlossaryUsageV1.model_validate(value)
    if version == "translator-glossary-usage-v2":
        return GlossaryUsageV2.model_validate(value)
    if version == "translator-glossary-usage-v3":
        return GlossaryUsage.model_validate(value)
    raise ValueError("unsupported glossary usage schema")
