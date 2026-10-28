"""Public, typed settings for document translation through MCP."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class SentenceReplacement(BaseModel):
    """Change wording without asking a splitter to recreate boundaries."""

    model_config = ConfigDict(extra="forbid", strict=True)
    action: Literal["replace"]
    sentence_order: int = Field(ge=1)
    text: str = Field(min_length=1, max_length=32_768)


class SentenceSplit(BaseModel):
    """Split after the specified Unicode character in the trimmed text."""

    model_config = ConfigDict(extra="forbid", strict=True)
    action: Literal["split"]
    sentence_order: int = Field(ge=1)
    text: str = Field(min_length=1, max_length=32_768)
    offset: int = Field(ge=1, description="Unicode character offset in trimmed text.")


class SentenceMerge(BaseModel):
    """Join two adjacent target segments without changing their text."""

    model_config = ConfigDict(extra="forbid", strict=True)
    action: Literal["merge_next"]
    sentence_order: int = Field(ge=1)


SentenceEdit = Annotated[
    SentenceReplacement | SentenceSplit | SentenceMerge,
    Field(discriminator="action"),
]

ExportMetadataName = Annotated[
    str,
    Field(
        min_length=1,
        max_length=300,
        description="One credited person's display name.",
    ),
]


class ExportMetadataOtherContributor(BaseModel):
    """One non-author/editor/translator contributor with a MARC role code."""

    model_config = ConfigDict(extra="forbid", strict=True)

    name: ExportMetadataName
    role: str = Field(
        min_length=3,
        max_length=3,
        pattern=r"^[a-z]{3}$",
        description="Three-letter lowercase MARC relator code.",
    )


class ExportMetadataIdentifier(BaseModel):
    """One bounded publication identifier."""

    model_config = ConfigDict(extra="forbid", strict=True)

    scheme: Literal["ISBN", "DOI", "URI", "OTHER"]
    value: str = Field(
        min_length=1,
        max_length=500,
        description="The identifier value.",
    )


class ExportMetadataSettings(BaseModel):
    """Optional full-replacement metadata defaults applied to future exports."""

    model_config = ConfigDict(extra="forbid", strict=True)

    title: str | None = Field(
        None,
        min_length=1,
        max_length=500,
        description="Optional export title; full-replacement updates clear omitted fields.",
    )
    subtitle: str | None = Field(None, max_length=500)
    description: str | None = Field(
        None,
        max_length=4_000,
        description="Bounded book details for the export.",
    )
    publisher: str | None = Field(None, max_length=500)
    publication_date: str | None = Field(None, max_length=10)
    rights: str | None = Field(
        None,
        max_length=4_000,
        description="Bounded rights text for the export.",
    )
    journal_or_series: str | None = Field(None, max_length=500)
    volume: str | None = Field(None, max_length=100)
    authors: list[ExportMetadataName] | None = Field(
        None,
        max_length=50,
        description="Replacement author names for future exports.",
    )
    editors: list[ExportMetadataName] | None = Field(
        None,
        max_length=50,
        description="Replacement editor names for future exports.",
    )
    translators: list[ExportMetadataName] | None = Field(
        None,
        max_length=50,
        description="Replacement translator names for future exports.",
    )
    other_contributors: list[ExportMetadataOtherContributor] | None = Field(
        None,
        max_length=50,
        description="Replacement additional contributors with MARC roles.",
    )
    subjects: list[Annotated[str, Field(min_length=1, max_length=300)]] | None = Field(
        None,
        max_length=64,
        description="Replacement subject terms for future exports.",
    )
    identifiers: list[ExportMetadataIdentifier] | None = Field(
        None,
        max_length=20,
        description="Replacement publication identifiers for future exports.",
    )
    cover_upload_path: str | None = Field(
        None,
        max_length=1_000,
        description="Canonical cover asset path returned by cover import.",
    )


class TranslatorSettings(BaseModel):
    """Creation options; omitted values retain the product's defaults."""

    model_config = ConfigDict(extra="forbid", strict=True)

    translator_model: str | None = Field(None, max_length=255)
    researcher_model: str | None = Field(None, max_length=255)
    analyzer_model: str | None = Field(None, max_length=255)
    chunk_size_chars: int | None = Field(None, ge=500, le=20_000)
    custom_prompt: str | None = Field(None, max_length=20_000)
    enable_pre_analysis: bool | None = Field(
        None, description="Managed mode only; passive creation defaults to false."
    )
    enable_research: bool | None = Field(
        None, description="Managed mode only; passive research uses submitted dossiers."
    )
    enable_translator_notes: bool | None = None
    annotation_profile: Literal["none", "restrained", "study"] | None = Field(
        None, description="Reader-note density; overrides enable_translator_notes."
    )
    research_plugin_ids: list[str] | None = Field(None, max_length=20)
    translation_batch_input_tokens: int | None = Field(None, ge=2_048, le=1_000_000)
    translation_batch_max_blocks: int | None = Field(None, ge=1, le=256)
    translation_context_mode: Literal["self_contained", "retained"] = (
        "self_contained"
    )
    translation_unit_mode: Literal["block", "aligned_sentences"] | None = Field(
        None,
        description="Complete semantic blocks, optionally with sentence alignment.",
    )
    translation_max_block_chars: int | None = Field(
        None,
        ge=500,
        le=20_000,
        description=(
            "Aligned-sentence mode only: split oversized body paragraphs at safe sentence boundaries."
        ),
    )
    export_metadata: ExportMetadataSettings | None = Field(
        None,
        description=("Optional full-replacement metadata defaults for future exports."),
    )
