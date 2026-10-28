"""Closed metadata contract for note placement and numbering evidence."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import cast

NOTE_ARRANGEMENT_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "placement": {
            "type": "string",
            "enum": [
                "page_footnotes",
                "chapter_endnotes",
                "document_endnotes",
                "mixed",
                "none",
                "unknown",
            ],
        },
        "numbering": {
            "type": "string",
            "enum": ["document", "chapter", "section", "page", "mixed", "unknown"],
        },
        "scope_heading_level": {
            "type": ["integer", "null"],
            "minimum": 1,
            "maximum": 6,
        },
        "source_pages": {
            "type": "array",
            "items": {"type": "integer", "minimum": 1},
            "maxItems": 32,
        },
        "evidence": {"type": "string", "maxLength": 800},
    },
    "required": [
        "placement",
        "numbering",
        "scope_heading_level",
        "source_pages",
        "evidence",
    ],
}

_PLACEMENT_VALUES = (
    "page_footnotes",
    "chapter_endnotes",
    "document_endnotes",
    "mixed",
    "none",
    "unknown",
)
_NUMBERING_VALUES = ("document", "chapter", "section", "page", "mixed", "unknown")


def normalize_note_arrangement(value: object) -> dict[str, object]:
    """Normalize a note arrangement while dropping undeclared input fields."""

    if not isinstance(value, Mapping):
        return {}

    arrangement = cast(Mapping[str, object], value)
    if not arrangement:
        return {}

    placement = arrangement.get("placement")
    if not isinstance(placement, str) or placement not in _PLACEMENT_VALUES:
        placement = "unknown"

    numbering = arrangement.get("numbering")
    if not isinstance(numbering, str) or numbering not in _NUMBERING_VALUES:
        numbering = "unknown"

    heading_level = arrangement.get("scope_heading_level")
    if (
        not isinstance(heading_level, int)
        or isinstance(heading_level, bool)
        or not 1 <= heading_level <= 6
    ):
        heading_level = None

    raw_pages = arrangement.get("source_pages")
    pages: list[int] = []
    if isinstance(raw_pages, list):
        raw_page_values = cast(list[object], raw_pages)
        pages = sorted(
            {
                page
                for page in raw_page_values
                if isinstance(page, int) and not isinstance(page, bool) and page > 0
            }
        )[:32]

    evidence = arrangement.get("evidence")
    evidence = evidence.strip()[:800] if isinstance(evidence, str) else ""

    return {
        "placement": placement,
        "numbering": numbering,
        "scope_heading_level": heading_level,
        "source_pages": pages,
        "evidence": evidence,
    }


def note_arrangement_enabled(
    settings: Mapping[str, object] | None = None,
) -> bool:
    """Enable this contract for managed work and marked passive executions."""

    if settings is None or settings.get("mcp_execution_mode") != "passive":
        return True
    return settings.get("mcp_note_arrangement_policy") == "source-arrangement-v1"


def metadata_schema_with_note_arrangement(
    schema: dict[str, object], settings: Mapping[str, object] | None = None
) -> dict[str, object]:
    """Return the base schema unchanged unless the execution enables this field."""

    if not note_arrangement_enabled(settings):
        return schema

    selected = deepcopy(schema)
    properties = selected.get("properties")
    if not isinstance(properties, dict):
        properties = {}
        selected["properties"] = properties
    properties["note_arrangement"] = deepcopy(NOTE_ARRANGEMENT_SCHEMA)

    required = selected.get("required")
    if not isinstance(required, list):
        required = []
        selected["required"] = required
    required_items = cast(list[object], required)
    if "note_arrangement" not in required_items:
        required_items.append("note_arrangement")

    return selected
