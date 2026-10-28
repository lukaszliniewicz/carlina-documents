"""Pure semantic checks for new leased PDF page-batch packets."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from itertools import pairwise
from typing import Any

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

from .page_batch_response import (
    PageBatchResponseError,
    page_batch_response_schema,
    parse_page_batch_response_details,
)
from .parsers.pdf_utils import (
    ListContinuationError,
    clean_markdown_artifacts,
    concatenate_html_pages,
    has_source_toc_omission_marker,
    normalize_extracted_page_html,
)
from .parsers.semantic_validation import (
    EXTRACTION_NOTE_INPUT_POLICY_VERSION,
    validate_extraction_note_input,
    validate_model_response_content,
    validate_normalized_page_output,
)

PASSIVE_PAGE_BATCH_V2_SCHEMA = "document_page_batch_v2"
PASSIVE_PAGE_BATCH_V3_SCHEMA = "document_page_batch_v3"
PASSIVE_PAGE_BATCH_SOURCE = "doc_processor_pdf_batch"
PASSIVE_PAGE_PREFLIGHT_CONTRACT = "document-page-preflight-v1"
_MAX_PACKET_PAGES = 256
_MAX_DIAGNOSTICS = 20


@dataclass(frozen=True)
class PageDeclarationValidation:
    valid: bool
    normalized_html: str | None = None
    source_toc_omitted: bool = False
    diagnostics: tuple[str, ...] = ()


def _significant(node: object) -> bool:
    if isinstance(node, Comment):
        return False
    if isinstance(node, NavigableString):
        return bool(str(node).strip())
    return True


def _has_empty_root_pair(content: str) -> bool:
    """Require exactly two roots with only whitespace/comments inside them."""

    soup = BeautifulSoup(clean_markdown_artifacts(content), "html.parser")
    significant = [node for node in soup.contents if _significant(node)]
    if len(significant) != 2 or not all(isinstance(node, Tag) for node in significant):
        return False
    main, footnotes = significant
    if [main.name, footnotes.name] != ["main_content", "footnotes"]:
        return False
    return all(
        not _significant(child) or isinstance(child, Comment)
        for root in (main, footnotes)
        for child in root.contents
    )


def _normalized_page_has_no_visible_content(normalized_html: str) -> bool:
    """Detect empty normalized output while preserving image-only pages."""

    soup = BeautifulSoup(normalized_html, "html.parser")
    roots = [node for node in soup.contents if isinstance(node, Tag)]
    if [root.name for root in roots] != ["main_content", "footnotes"]:
        return False
    if any(root.get_text().strip() for root in roots):
        return False
    return not any(root.find(["img", "figure"]) for root in roots)


def validate_page_declaration(
    content: str,
    *,
    declared_blank: bool,
    finish_reason: object = "",
    strict_note_input: bool = False,
) -> PageDeclarationValidation:
    """Validate a page's HTML and explicit visual blank declaration.

    This pure helper is shared by the worker and leased-packet preflight. It
    checks the declaration's relationship to the HTML only; it does not inspect
    page pixels or make a claim about source grounding.
    """

    if type(declared_blank) is not bool:
        return PageDeclarationValidation(False, diagnostics=("blank must be a boolean",))

    response = validate_model_response_content(content, finish_reason)
    if not response.valid:
        return PageDeclarationValidation(False, diagnostics=response.diagnostics)

    if strict_note_input:
        note_input = validate_extraction_note_input(content)
        if not note_input.valid:
            return PageDeclarationValidation(
                False, diagnostics=note_input.diagnostics
            )

    source_toc_omitted = has_source_toc_omission_marker(content)
    normalized = normalize_extracted_page_html(content, toc_handling="omit")
    validation_soup = BeautifulSoup(normalized, "html.parser")
    for comment in validation_soup.find_all(
        string=lambda value: isinstance(value, Comment)
    ):
        comment.extract()
    semantic = validate_normalized_page_output(str(validation_soup))
    if not semantic.valid:
        return PageDeclarationValidation(
            False,
            normalized_html=normalized,
            source_toc_omitted=source_toc_omitted,
            diagnostics=semantic.diagnostics,
        )

    if declared_blank and not _has_empty_root_pair(content):
        return PageDeclarationValidation(
            False,
            normalized_html=normalized,
            source_toc_omitted=source_toc_omitted,
            diagnostics=("blank declaration requires empty main_content and footnotes roots",),
        )
    if (
        not declared_blank
        and not source_toc_omitted
        and _normalized_page_has_no_visible_content(normalized)
    ):
        return PageDeclarationValidation(
            False,
            normalized_html=normalized,
            diagnostics=("blank declaration required for empty page output",),
        )

    return PageDeclarationValidation(
        True,
        normalized_html=normalized,
        source_toc_omitted=source_toc_omitted,
    )


def _schema_page_numbers(request: Mapping[str, Any]) -> tuple[int, ...] | None:
    response_format = request.get("response_format")
    json_schema = (
        response_format.get("json_schema")
        if isinstance(response_format, Mapping)
        else None
    )
    if not isinstance(json_schema, Mapping) or json_schema.get("name") not in {
        PASSIVE_PAGE_BATCH_V2_SCHEMA,
        PASSIVE_PAGE_BATCH_V3_SCHEMA,
    }:
        return None
    is_v3 = json_schema.get("name") == PASSIVE_PAGE_BATCH_V3_SCHEMA

    schema = json_schema.get("schema")
    if not isinstance(schema, Mapping):
        raise TypeError("invalid schema")
    properties = schema.get("properties")
    pages = properties.get("pages") if isinstance(properties, Mapping) else None
    item = pages.get("items") if isinstance(pages, Mapping) else None
    item_properties = item.get("properties") if isinstance(item, Mapping) else None
    number_property = (
        item_properties.get("page_number")
        if isinstance(item_properties, Mapping)
        else None
    )
    if not isinstance(pages, Mapping) or not isinstance(number_property, Mapping):
        raise TypeError("invalid schema")

    raw_numbers = number_property.get("enum")
    minimum = pages.get("minItems")
    maximum = pages.get("maxItems")
    item_required = item.get("required") if isinstance(item, Mapping) else None
    blank_property = item_properties.get("blank") if isinstance(item_properties, Mapping) else None
    if (
        not isinstance(raw_numbers, list)
        or not raw_numbers
        or len(raw_numbers) > _MAX_PACKET_PAGES
        or type(minimum) is not int
        or minimum != len(raw_numbers)
        or type(maximum) is not int
        or maximum != len(raw_numbers)
        or not isinstance(item_required, list)
        or not {"page_number", "html", "blank"}.issubset(item_required)
        or not isinstance(blank_property, Mapping)
        or blank_property.get("type") != "boolean"
    ):
        raise ValueError("invalid schema")
    if any(type(number) is not int or number <= 0 for number in raw_numbers):
        raise ValueError("invalid schema")
    if any(left >= right for left, right in pairwise(raw_numbers)):
        raise ValueError("invalid schema")
    if is_v3 and schema != page_batch_response_schema(
        raw_numbers, allow_blank=True, allow_issues=True
    ):
        raise ValueError("invalid schema")
    return tuple(raw_numbers)


def _request_schema_error() -> dict[str, Any]:
    return {
        "contract": PASSIVE_PAGE_PREFLIGHT_CONTRACT,
        "scope": "leased_packet_only",
        "validated_blocks": 0,
        "validation_errors": [
            {"path": "/request/response_format", "code": "page-batch-request-schema"}
        ],
    }


def _response_error(path: str, code: str) -> dict[str, Any]:
    return {
        "contract": PASSIVE_PAGE_PREFLIGHT_CONTRACT,
        "scope": "leased_packet_only",
        "validated_blocks": 0,
        "validation_errors": [{"path": path, "code": code}],
    }


def _diagnostic_code(diagnostics: tuple[str, ...]) -> tuple[str, str]:
    detail = diagnostics[0].casefold() if diagnostics else ""
    if detail.startswith("footnote input shape:"):
        return "/html", "footnote-input-shape"
    if detail.startswith("blank declaration requires"):
        return "/blank", "blank-requires-empty-html"
    if detail.startswith("blank declaration required"):
        return "/blank", "blank-declaration-required"
    if detail.startswith("required roots"):
        return "/html", "semantic-roots"
    if detail.startswith("unstructured text"):
        return "/html", "unstructured-text"
    if detail.startswith("footnote reference"):
        return "/html", "footnote-reference"
    if detail.startswith("unsafe link"):
        return "/html", "unsafe-link"
    if detail.startswith("source_toc_"):
        return "/html", "source-toc-contract"
    if detail.startswith("duplicate id="):
        return "/html", "duplicate-id"
    if detail.startswith("terminal failure"):
        return "/html", "terminal-model-response"
    return "/html", "page-semantic-contract"


def preflight_completion(
    request: Mapping[str, Any], response: Mapping[str, Any]
) -> dict[str, Any] | None:
    """Validate one exact v2/v3 leased packet without loading source or state."""

    if (
        not isinstance(request, Mapping)
        or request.get("source") != PASSIVE_PAGE_BATCH_SOURCE
    ):
        return None
    try:
        expected_numbers = _schema_page_numbers(request)
    except (TypeError, ValueError, RecursionError):
        return _request_schema_error()
    if expected_numbers is None:
        return None
    json_schema = request.get("response_format", {}).get("json_schema", {})
    allow_issues = (
        isinstance(json_schema, Mapping)
        and json_schema.get("name") == PASSIVE_PAGE_BATCH_V3_SCHEMA
    )
    dispatch = request.get("dispatch")
    extraction_contract = (
        dispatch.get("extraction_contract")
        if isinstance(dispatch, Mapping)
        else None
    )
    strict_note_input = (
        isinstance(extraction_contract, Mapping)
        and extraction_contract.get("version")
        == EXTRACTION_NOTE_INPUT_POLICY_VERSION
    )
    if not isinstance(response, Mapping):
        return _response_error("/content/pages", "page-response-contract")
    content = response.get("content")
    if not isinstance(content, str):
        return _response_error("/content/pages", "page-response-contract")
    try:
        pages = parse_page_batch_response_details(
            content,
            expected_numbers,
            allow_blank=True,
            allow_issues=allow_issues,
        )
    except PageBatchResponseError as exc:
        if exc.page_index is not None:
            base = f"/content/pages/{exc.page_index}"
            field = exc.field or "html"
            path = f"{base}/{field}"
            if exc.code == "page-order":
                path = "/content/pages/order"
        elif exc.code == "page-order":
            path = "/content/pages/order"
        else:
            path = "/content/pages"
        return _response_error(path, exc.code)
    except (TypeError, ValueError, RecursionError):
        return _response_error("/content/pages", "page-response-contract")

    errors = []
    validated_pages: list[tuple[int, int, str]] = []
    for index, page_number in enumerate(expected_numbers):
        page = pages[page_number]
        validation = validate_page_declaration(
            page.html,
            declared_blank=page.blank,
            strict_note_input=strict_note_input,
        )
        if not validation.valid:
            suffix, code = _diagnostic_code(validation.diagnostics)
            errors.append({"path": f"/content/pages/{index}{suffix}", "code": code})
        elif validation.normalized_html is not None:
            validated_pages.append((index, page_number, validation.normalized_html))
        if len(errors) >= _MAX_DIAGNOSTICS:
            break

    if len(errors) < _MAX_DIAGNOSTICS:
        merge_pages: list[tuple[int, str]] = []
        dispatch = request.get("dispatch")
        boundary_input = (
            dispatch.get("boundary_input") if isinstance(dispatch, Mapping) else None
        )
        if isinstance(boundary_input, Mapping):
            last_page = boundary_input.get("last_page")
            last_page_html = boundary_input.get("last_page_html")
            if (
                type(last_page) is int
                and last_page > 0
                and isinstance(last_page_html, str)
                and expected_numbers
                and last_page + 1 == expected_numbers[0]
            ):
                predecessor = validate_page_declaration(
                    last_page_html, declared_blank=False
                )
                if predecessor.valid and predecessor.normalized_html is not None:
                    merge_pages.append((last_page, predecessor.normalized_html))
        merge_pages.extend(
            (page_number, html) for _index, page_number, html in validated_pages
        )

        if len(merge_pages) > 1:
            try:
                concatenate_html_pages(
                    [html for _page_number, html in merge_pages],
                    page_numbers=[page_number for page_number, _html in merge_pages],
                )
            except ListContinuationError as exc:
                incoming_index = next(
                    (
                        index
                        for index, page_number, _html in validated_pages
                        if page_number == exc.page_number
                    ),
                    None,
                )
                if incoming_index is not None:
                    errors.append(
                        {
                            "path": f"/content/pages/{incoming_index}/html",
                            "code": exc.reason_code,
                        }
                    )

    return {
        "contract": PASSIVE_PAGE_PREFLIGHT_CONTRACT,
        "scope": "leased_packet_only",
        "validated_blocks": len(expected_numbers) if not errors else 0,
        "validation_errors": errors,
    }
