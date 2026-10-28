"""Strict JSON contract for passive multipage document responses."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import pairwise

from .extraction_issues import issue_reports_schema, validate_issue_reports


class PageBatchValidationError(RuntimeError):
    """A failed atomic batch with bounded, content-free recovery evidence."""

    def __init__(self, feedback: dict):
        self.feedback = dict(feedback)
        batch_id = str(self.feedback.get("batch_id") or "unknown")
        attempt = self.feedback.get("attempt")
        attempt_label = attempt if isinstance(attempt, int) and attempt > 0 else "unknown"
        page = self.feedback.get("page") or "unknown"
        stage = str(self.feedback.get("stage") or "validation")
        reason = str(self.feedback.get("reason") or "validation failed")
        super().__init__(
            f"PDF batch {batch_id} failed after {attempt_label} attempts: "
            f"page {page}, {stage}: {reason}"
        )


class PageBatchResponseError(ValueError):
    """Content-free page-batch parse error with a stable diagnostic code."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        page_index: int | None = None,
        field: str | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.page_index = page_index
        self.field = field


@dataclass(frozen=True)
class ParsedPageBatchPage:
    html: str
    blank: bool = False
    issues: tuple[dict, ...] = ()


def batch_validation_reason(
    stage: str,
    diagnostics: Sequence[str],
    *,
    allow_blank: bool = False,
    allow_issues: bool = False,
) -> str:
    """Project validators to fixed advice without echoing document IDs/text."""

    reasons = (
        ("duplicate id=", "Duplicate HTML IDs; assign distinct IDs to distinct elements."),
        ("source extraction appears incomplete", "Incomplete transcription; check the end of the page and omitted passages."),
        ("source grounding mismatch", "Transcription does not match the page text; verify the correct page and all visible text."),
        ("footnote reference", "Invalid footnote target; link each reference to its local footnote."),
        ("required roots", "Return one ordered main_content/footnotes root pair for each page."),
        ("unstructured text", "Place text inside semantic elements within the page roots."),
        ("unsafe link scheme", "Remove active link schemes from the extracted markup."),
        ("source_toc_", "Invalid source TOC markup; follow the source-TOC omission contract."),
        ("blank declaration", "A blank declaration requires empty main_content and footnotes roots."),
        ("page-blank", "Include a boolean blank field; set it true only for a visually blank page."),
    )
    for prefix, reason in reasons:
        if any(detail.startswith(prefix) for detail in diagnostics):
            return reason
    return {
        "page_identity": (
            "Return exactly the requested pages, once each in order, with page_number, html, boolean blank, and an issues array only. Use an empty issues array when no report is warranted."
            if allow_issues
            else (
                "Return exactly the requested pages, once each in order, with page_number, html, and boolean blank only."
                if allow_blank
                else "Return exactly the requested pages, once each in order, with page_number and html only."
            )
        ),
        "model_response": "The page output is empty, blocked or truncated; return complete page HTML.",
        "semantic_contract": "The page HTML violates the semantic contract; check roots, IDs and footnote links.",
        "source_grounding": "Check the transcription against the complete source page.",
    }.get(stage, "The page response failed validation.")


def _validated_page_numbers(page_numbers: Sequence[int]) -> tuple[int, ...]:
    if not isinstance(page_numbers, Sequence):
        raise ValueError(  # noqa: TRY004 - the public contract uses ValueError.
            "page_numbers must be a non-empty sequence of integers."
        )

    numbers = tuple(page_numbers)
    if not numbers:
        raise ValueError("page_numbers must be a non-empty sequence of integers.")
    if any(
        isinstance(number, bool) or not isinstance(number, int) or number <= 0
        for number in numbers
    ):
        raise ValueError("page_numbers must contain positive integers only.")
    if any(previous >= current for previous, current in pairwise(numbers)):
        raise ValueError("page_numbers must be strictly increasing.")
    return numbers


def page_batch_response_schema(
    page_numbers: Sequence[int],
    *,
    allow_blank: bool = False,
    allow_issues: bool = False,
) -> dict:
    """Build the exact response schema for an ordered page-number batch.

    The legacy schema is byte-for-byte structurally unchanged by default.
    Version 2 requires an explicit boolean declaration for every page.
    """

    if type(allow_blank) is not bool:
        raise ValueError("allow_blank must be a boolean.")
    if type(allow_issues) is not bool:
        raise ValueError("allow_issues must be a boolean.")
    expected_numbers = _validated_page_numbers(page_numbers)
    item_properties = {
        "page_number": {
            "type": "integer",
            "enum": list(expected_numbers),
        },
        "html": {"type": "string", "minLength": 1},
    }
    required = ["page_number", "html"]
    if allow_blank:
        item_properties["blank"] = {
            "type": "boolean",
            "description": (
                "Set false for pages with visible content and for intentionally "
                "omitted TOC content. Set true only after inspecting the supplied "
                "page image and finding no substantive visible content. Return empty "
                "main_content and footnotes roots when true. This is an explicit "
                "visual judgment, not an automatic image classification. Use the "
                "source-TOC omission marker for intentionally omitted TOC content."
            ),
        }
        required.append("blank")
    if allow_issues:
        item_properties["issues"] = issue_reports_schema()
        required.append("issues")
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "pages": {
                "type": "array",
                "minItems": len(expected_numbers),
                "maxItems": len(expected_numbers),
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": item_properties,
                    "required": required,
                },
            }
        },
        "required": ["pages"],
    }


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("JSON objects must not contain duplicate fields.")
        result[key] = value
    return result


def parse_page_batch_response_details(
    content: str,
    page_numbers: Sequence[int],
    *,
    allow_blank: bool = False,
    allow_issues: bool = False,
) -> dict[int, ParsedPageBatchPage]:
    """Parse one ordered response, enforcing the selected schema version."""

    if type(allow_blank) is not bool:
        raise ValueError("allow_blank must be a boolean.")
    if type(allow_issues) is not bool:
        raise ValueError("allow_issues must be a boolean.")
    expected_numbers = _validated_page_numbers(page_numbers)
    if not isinstance(content, str):
        raise PageBatchResponseError(
            "content must be a JSON string.", code="response-content-type"
        )

    try:
        payload = json.loads(content, object_pairs_hook=_reject_duplicate_keys)
    except json.JSONDecodeError as exc:
        raise PageBatchResponseError(
            "content is not valid JSON.", code="response-invalid-json"
        ) from exc
    except ValueError as exc:
        raise PageBatchResponseError(
            "JSON objects must not contain duplicate fields.",
            code="response-duplicate-field",
        ) from exc
    except RecursionError as exc:
        raise PageBatchResponseError(
            "content could not be decoded as JSON.", code="response-invalid-json"
        ) from exc

    if type(payload) is not dict or set(payload) != {"pages"}:
        raise PageBatchResponseError(
            "response must contain exactly one pages field.", code="response-shape"
        )

    pages = payload["pages"]
    if type(pages) is not list:
        raise PageBatchResponseError("pages must be a JSON array.", code="pages-type")
    if len(pages) != len(expected_numbers):
        raise PageBatchResponseError(
            "pages must contain exactly the expected page count.",
            code="pages-cardinality",
        )

    parsed: dict[int, ParsedPageBatchPage] = {}
    allowed_fields = {"page_number", "html"}
    if allow_blank:
        allowed_fields.add("blank")
    if allow_issues:
        allowed_fields.add("issues")
    for index, item in enumerate(pages):
        if type(item) is not dict:
            raise PageBatchResponseError(
                "each pages item must be a JSON object.",
                code="page-shape",
                page_index=index,
            )
        if set(item) != allowed_fields:
            if allow_blank and "blank" not in item:
                code, field = "page-blank-required", "blank"
            elif allow_issues and "issues" not in item:
                code, field = "page-issues-required", "issues"
            else:
                code, field = "page-shape", None
            raise PageBatchResponseError(
                "each pages item must contain exactly the fields for its schema version.",
                code=code,
                page_index=index,
                field=field,
            )

        page_number = item["page_number"]
        if type(page_number) is not int:
            raise PageBatchResponseError(
                "page_number must be an integer.",
                code="page-number-type",
                page_index=index,
                field="page_number",
            )
        if page_number != expected_numbers[index]:
            raise PageBatchResponseError(
                "page_number values must match the expected order.",
                code="page-order",
                page_index=index,
                field="page_number",
            )

        html = item["html"]
        if type(html) is not str:
            raise PageBatchResponseError(
                "html must be a string.",
                code="page-html-type",
                page_index=index,
                field="html",
            )
        if not html.strip():
            raise PageBatchResponseError(
                "html must not be blank.",
                code="page-html-empty",
                page_index=index,
                field="html",
            )

        blank = item["blank"] if allow_blank else False
        if allow_blank and type(blank) is not bool:
            raise PageBatchResponseError(
                "blank must be a boolean.",
                code="page-blank-type",
                page_index=index,
                field="blank",
            )
        issues: tuple[dict, ...] = ()
        if allow_issues:
            try:
                normalized_issues = validate_issue_reports(item["issues"])
            except (TypeError, ValueError):
                raise PageBatchResponseError(
                    "issues must satisfy the extraction issue report contract.",
                    code="page-issues-invalid",
                    page_index=index,
                    field="issues",
                ) from None
            if blank and normalized_issues:
                raise PageBatchResponseError(
                    "blank pages must not contain extraction issue reports.",
                    code="page-blank-issues",
                    page_index=index,
                    field="issues",
                )
            issues = tuple(normalized_issues)
        parsed[page_number] = ParsedPageBatchPage(
            html=html, blank=blank, issues=issues
        )

    return parsed


def parse_page_batch_response(
    content: str,
    page_numbers: Sequence[int],
    *,
    allow_blank: bool = False,
    allow_issues: bool = False,
) -> dict[int, str]:
    """Parse one strict, ordered multipage response without changing its HTML."""

    detailed = parse_page_batch_response_details(
        content,
        page_numbers,
        allow_blank=allow_blank,
        allow_issues=allow_issues,
    )
    return {number: page.html for number, page in detailed.items()}
