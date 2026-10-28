"""Closed metadata contract for a bounded PDF cover-page witness."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import cast

COVER_CANDIDATE_SCHEMA: dict[str, object] = {
    "type": ["object", "null"],
    "additionalProperties": False,
    "properties": {
        "page": {"type": "integer", "minimum": 1, "maximum": 1_000_000},
        "kind": {"type": "string", "enum": ["cover", "title_page"]},
        "evidence": {"type": "string", "minLength": 1, "maxLength": 500},
    },
    "required": ["page", "kind", "evidence"],
}


def normalize_cover_candidate(value: object) -> dict[str, object]:
    """Return only a valid, bounded cover or title-page witness."""

    if not isinstance(value, Mapping):
        return {}

    candidate = cast(Mapping[str, object], value)
    page = candidate.get("page")
    if (
        not isinstance(page, int)
        or isinstance(page, bool)
        or not 1 <= page <= 1_000_000
    ):
        return {}

    kind = candidate.get("kind")
    if not isinstance(kind, str) or kind not in {"cover", "title_page"}:
        return {}

    evidence = candidate.get("evidence")
    if not isinstance(evidence, str):
        return {}
    evidence = evidence.strip()[:500]
    if not evidence:
        return {}

    return {"page": page, "kind": kind, "evidence": evidence}


def pdf_cover_candidate_enabled(
    settings: Mapping[str, object] | None = None,
) -> bool:
    """Enable the field for managed work and explicitly marked passive runs."""

    if settings is None or settings.get("mcp_execution_mode") != "passive":
        return True
    return settings.get("mcp_cover_candidate_policy") == "pdf-page-v1"


def metadata_schema_with_cover_candidate(
    schema: dict[str, object], settings: Mapping[str, object] | None = None
) -> dict[str, object]:
    """Add the field to a copied schema when its execution policy enables it."""

    if not pdf_cover_candidate_enabled(settings):
        return schema

    selected = deepcopy(schema)
    properties = selected.get("properties")
    if not isinstance(properties, dict):
        properties = {}
        selected["properties"] = properties
    cast(dict[str, object], properties)["cover_candidate"] = deepcopy(
        COVER_CANDIDATE_SCHEMA
    )

    required = selected.get("required")
    if not isinstance(required, list):
        required = []
        selected["required"] = required
    required_items = cast(list[object], required)
    if "cover_candidate" not in required_items:
        required_items.append("cover_candidate")

    return selected
