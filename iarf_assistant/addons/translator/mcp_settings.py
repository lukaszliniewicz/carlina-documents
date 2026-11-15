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


class EditReason(BaseModel):
    """The defect a correction removes; "it reads better" is not a defect."""

    model_config = ConfigDict(extra="forbid", strict=True)
    defect: Literal[
        "meaning",
        "omission",
        "addition",
        "language",
        "terminology",
        "consistency",
        "brief",
        "misreading",
        "formatting",
    ]
    note: str = Field(
        min_length=1,
        max_length=500,
        description="One line naming what was wrong, e.g. the quoted span and why.",
    )


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


class TranslationBrief(BaseModel):
    """The translation brief. Choices enter the trusted policy; text does not."""

    model_config = ConfigDict(extra="forbid", strict=True)

    register: Literal["formal", "neutral", "informal"] | None = None
    target_variety: str | None = Field(
        None,
        max_length=35,
        pattern=r"^[a-z]{2,3}(-[A-Za-z0-9]{2,8}){1,3}$",
        description="BCP 47 tag of the target variety, such as en-GB or pt-BR.",
    )
    translation_type: Literal["overt", "covert"] | None = Field(
        None,
        description=(
            "overt: readers know it is a translation and source-culture references "
            "stay; covert: it should read as written in the target language."
        ),
    )
    form_of_address: Literal["formal", "informal", "impersonal"] | None = Field(
        None, description="How the text addresses its reader; omit to follow the source."
    )
    purpose: str | None = Field(
        None, max_length=300, description="What the translation is for; reference only."
    )
    readers: str | None = Field(
        None, max_length=300, description="Who will read it; reference only."
    )


class QaPattern(BaseModel):
    """One owner house-style check that QA reports as an advisory finding."""

    model_config = ConfigDict(extra="forbid", strict=True)

    pattern: str = Field(
        min_length=1,
        max_length=200,
        description="Regular expression (Python regex syntax) matched against target text.",
    )
    message: str = Field(
        min_length=1, max_length=200, description="What QA tells the reviewer on a match."
    )
    ignore_case: bool = False


class TranslatorSettings(BaseModel):
    """Creation options; omitted values retain the product's defaults."""

    model_config = ConfigDict(extra="forbid", strict=True)

    translator_model: str | None = Field(None, max_length=255)
    researcher_model: str | None = Field(None, max_length=255)
    analyzer_model: str | None = Field(None, max_length=255)
    chunk_size_chars: int | None = Field(None, ge=500, le=20_000)
    custom_prompt: str | None = Field(None, max_length=20_000)
    translation_brief: TranslationBrief | None = Field(
        None,
        description=(
            "Register, target variety, overt or covert translation and form of address "
            "become project policy for every window; purpose and readers are passed as "
            "reference data."
        ),
    )
    qa_patterns: list[QaPattern] | None = Field(
        None,
        max_length=30,
        description=(
            "House-style checks: each match in the target is an advisory QA finding "
            "in the style category. A pattern must not match the empty string."
        ),
    )
    enable_pre_analysis: bool | None = Field(
        None,
        description=(
            "Document brief, style guide and entity analysis before translation. "
            "Passive creation defaults to false; when true, the analysis is leased "
            "as work items before the first window."
        ),
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
    translation_context_mode: Literal["self_contained", "retained"] = "self_contained"
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
