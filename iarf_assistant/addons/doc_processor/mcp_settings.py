"""Typed, public Document Processor settings for the MCP boundary."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class DocumentNotePromotion(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    action: Literal["promote_note"]
    element_id: str = Field(pattern=r"^n[0-9]{6,}$")
    label: str = Field(min_length=1, max_length=64)
    scope_id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,128}$")


class DocumentNoteScope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    action: Literal["set_note_scope"]
    element_id: str = Field(pattern=r"^n[0-9]{6,}$")
    scope_id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,128}$")


class DocumentNoteLink(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    action: Literal["link_reference"]
    element_id: str = Field(pattern=r"^n[0-9]{6,}$")
    marker_index: int = Field(ge=0)
    note_element_id: str = Field(pattern=r"^n[0-9]{6,}$")
    expected_label: str = Field(min_length=1, max_length=64)
    expected_href: str | None = Field(max_length=500)


DocumentNoteRepair = Annotated[
    DocumentNotePromotion | DocumentNoteScope | DocumentNoteLink,
    Field(discriminator="action"),
]


class DocumentReviewTextReplacement(BaseModel):
    """Case-sensitive literal replacement against the reviewed source snapshot."""

    model_config = ConfigDict(extra="forbid", strict=True)

    find: str = Field(min_length=1, max_length=1000)
    replacement: str = Field(max_length=10000)
    expected_matches: int = Field(ge=1, le=1000)
    whole_word: bool = Field(
        False,
        description=(
            "Require Unicode word boundaries around the literal match. Letters, "
            "numbers, combining marks and connector punctuation remain part of a word. "
            "False retains substring matching; no spelling normalization is performed."
        ),
    )


class DocumentReviewEdit(BaseModel):
    """Exactly one revision-scoped document correction."""

    model_config = ConfigDict(extra="forbid", strict=True)

    element_id: str = Field(pattern=r"^n[0-9]{6,}$")
    end_element_id: str | None = Field(None, pattern=r"^n[0-9]{6,}$")
    replacement_html: str | None = Field(None, min_length=1, max_length=262144)
    heading_level: int | None = Field(None, ge=1, le=6)
    demote_heading: bool | None = Field(
        None,
        description=(
            "Set true to convert a heading to an emphasized paragraph, preserving "
            "its identifier, inline markup and protected content. Does not merge blocks."
        ),
    )
    replace_text: DocumentReviewTextReplacement | None = None
    replace_texts: list[DocumentReviewTextReplacement] | None = Field(
        None,
        min_length=1,
        max_length=50,
        description=(
            "Apply 1-50 non-overlapping replacements to this element atomically. "
            "Every match and expected count refers to the original reviewed text, "
            "not the output of a preceding replacement."
        ),
    )
    delete_element: bool | None = None
    expected_text: str | None = Field(None, max_length=64000)

    @model_validator(mode="after")
    def exactly_one_operation(self):
        if (
            sum(
                value is not None
                for value in (
                    self.replacement_html,
                    self.heading_level,
                    self.demote_heading,
                    self.replace_text,
                    self.replace_texts,
                    self.delete_element,
                )
            )
            != 1
        ):
            raise ValueError("Provide exactly one edit operation.")
        if self.replacement_html is None and self.end_element_id is not None:
            raise ValueError("Only HTML replacement can address a sibling range.")
        if self.demote_heading is not None and self.demote_heading is not True:
            raise ValueError("Heading demotion requires demote_heading=true.")
        if self.delete_element is not None:
            if self.delete_element is not True or self.expected_text is None:
                raise ValueError(
                    "Deletion requires delete_element=true and expected_text."
                )
        elif self.expected_text is not None:
            raise ValueError("expected_text is only used with deletion.")
        return self


class McpPdfOptions(BaseModel):
    """Caller-configurable PDF processing options."""

    model_config = ConfigDict(extra="forbid", strict=True)

    removehf: bool | None = Field(
        None, description="Remove headers and footers from extracted PDF content."
    )
    evaluate: bool | None = Field(
        None, description="Run the PDF evaluation workflow when supported."
    )
    append_previous: bool | None = Field(
        None, description="Carry the previous page's context into the next page."
    )
    ignore_footnotes: bool | None = Field(
        None, description="Ignore detected footnotes during extraction."
    )
    simple_footnotes: bool | None = Field(
        None, description="Use simplified footnote handling."
    )
    llm_boundary_merge: bool | None = Field(
        None, description="Ask the language model to merge page-boundary fragments."
    )
    correct_images: bool | None = Field(
        None, description="Apply image correction during PDF extraction."
    )
    extract_images: bool | None = Field(
        None, description="Extract images from PDF pages."
    )
    parallel_pages: int | None = Field(
        None, description="Number of PDF pages that may be processed in parallel."
    )
    page_range: str | None = Field(
        None, description="Optional inclusive PDF page-range expression."
    )
    excluded_pages: list[int] | str | None = Field(
        None,
        description=(
            "PDF pages to exclude, as page numbers or a comma-separated expression."
        ),
    )
    metadata_prepass: bool | None = Field(
        None, description="Run metadata analysis before page extraction."
    )
    metadata_pages: int | None = Field(
        None, description="Number of pages sampled at the beginning for metadata."
    )
    metadata_end_pages: int | None = Field(
        None, description="Number of pages sampled at the end for metadata."
    )
    metadata_delete_flagged_pages: bool | None = Field(
        None,
        description="Remove pages confidently identified as non-content metadata.",
    )
    toc_handling: str | None = Field(
        None,
        description=(
            "How printed tables of contents are handled; the effective intake "
            "contract uses omit."
        ),
    )
    prompt_cache: bool | None = Field(
        None, description="Allow reusable prompt-cache data during extraction."
    )
    skip_cropping: bool | None = Field(
        None, description="Skip PDF crop detection and initial crop review."
    )
    store_debug_artifacts: bool | None = Field(
        None, description="Retain bounded parser debug artifacts for diagnostics."
    )


class McpEngineOptions(BaseModel):
    """Caller-configurable extraction-engine options."""

    model_config = ConfigDict(extra="forbid", strict=True)

    force_ocr: bool | None = Field(
        None, description="Force OCR even when embedded text is available."
    )
    paginate_output: bool | None = Field(
        None, description="Preserve page boundaries in extracted output."
    )
    use_doc_unwarping: bool | None = Field(
        None, description="Enable document unwarping for supported OCR engines."
    )
    use_doc_orientation_classify: bool | None = Field(
        None,
        description=(
            "Enable document-orientation classification for supported OCR engines."
        ),
    )
    layout_shape_mode: str | None = Field(
        None, description="Layout shape strategy for the PaddleOCR engine."
    )
    merge_tables: bool | None = Field(
        None, description="Merge compatible table fragments across page regions."
    )
    relevel_titles: bool | None = Field(
        None, description="Normalize title levels in OCR output."
    )
    concatenate_pages: bool | None = Field(
        None, description="Concatenate page output for supported OCR engines."
    )
    paddle_remove_hf: bool | None = Field(
        None, description="Remove headers and footers in PaddleOCR output."
    )
    paddle_preserve_footnotes: bool | None = Field(
        None, description="Preserve footnotes in PaddleOCR output."
    )
    paddle_chart_rec: bool | None = Field(
        None, description="Enable chart recognition in PaddleOCR."
    )
    paddle_seal_rec: bool | None = Field(
        None, description="Enable seal recognition in PaddleOCR."
    )
    paddle_batch_size: int | None = Field(
        None, description="Batch size for PaddleOCR page processing."
    )
    use_ocr: bool | None = Field(None, description="Use OCR in the PyMuPDF engine.")
    exclude_footers: bool | None = Field(
        None, description="Exclude detected footers in the PyMuPDF engine."
    )
    embed_images: bool | None = Field(
        None, description="Embed extracted images in the output."
    )


McpCropCoordinate = Annotated[float, Field(ge=0, le=1)]
McpCropBox = Annotated[
    list[McpCropCoordinate],
    Field(
        min_length=4,
        max_length=4,
        description="Normalized crop rectangle [left, top, right, bottom], each coordinate from 0 to 1.",
    ),
]


class McpCropBoxes(BaseModel):
    """Optional alternating-page crop rectangles."""

    model_config = ConfigDict(extra="forbid", strict=True)

    mode: Literal["single", "left_right"] | None = Field(
        None, description="Whether one crop or alternating left/right crops are used."
    )
    left: McpCropBox | None = Field(
        None, description="Normalized crop rectangle for left pages."
    )
    right: McpCropBox | None = Field(
        None, description="Normalized crop rectangle for right pages."
    )
    first_page_side: Literal["left", "right"] | None = Field(
        None, description="Side assigned to the first page in alternating mode."
    )


class DocumentSettings(BaseModel):
    """Closed public settings object shared by import and configure tools."""

    model_config = ConfigDict(extra="forbid", strict=True)

    engine: str | None = Field(
        None, description="Extraction engine identifier, or auto for host selection."
    )
    parsing_model: str | None = Field(
        None,
        description="Model used for document parsing when the engine requires one.",
    )
    metadata_model: str | None = Field(
        None, description="Model used for metadata analysis when enabled."
    )
    processing_profile: str | None = Field(
        None, description="Named processing profile, such as book_quality or custom."
    )
    pdf_options: McpPdfOptions | None = Field(
        None, description="PDF-specific processing options."
    )
    crop_box: McpCropBox | None = Field(
        None,
        description="One normalized PDF crop rectangle [left, top, right, bottom].",
    )
    crop_boxes: McpCropBoxes | None = Field(
        None, description="Optional single or alternating-page PDF crop rectangles."
    )
    engine_options: McpEngineOptions | None = Field(
        None, description="Options specific to the selected extraction engine."
    )


def document_settings_to_dict(
    value: DocumentSettings | Mapping[str, object] | None,
) -> dict[str, object]:
    """Return only fields explicitly supplied by the caller."""

    if value is None:
        return {}
    model = (
        value
        if isinstance(value, DocumentSettings)
        else DocumentSettings.model_validate(value)
    )
    return model.model_dump(mode="python", exclude_unset=True)


__all__ = ["DocumentSettings", "document_settings_to_dict"]
