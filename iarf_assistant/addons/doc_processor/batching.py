"""Provider-neutral batching contracts for passive PDF extraction."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

DOCUMENT_BATCH_PLAN_VERSION: Final = "carlina.document-batches.v1"
DOCUMENT_CONTEXT_VERSION: Final = "carlina.document-context.v1"
DOCUMENT_BOUNDARY_VERSION: Final = "carlina.document-boundary.v1"
MAX_DOCUMENT_BATCHES: Final = 128
MAX_PAGE_NUMBER: Final = 1_000_000
MAX_BOUNDARY_HTML_CHARS: Final = 12_000
MAX_HALO_TEXT_CHARS: Final = 8_000

_BATCH_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
_CONTEXT_OPTION_KEYS = (
    "append_previous",
    "correct_images",
    "evaluate",
    "extract_images",
    "ignore_footnotes",
    "llm_boundary_merge",
    "metadata_prepass",
    "page_range",
    "removehf",
    "simple_footnotes",
    "toc_handling",
)


class DocumentBatchInput(BaseModel):
    """Strict public transport shape for one inclusive PDF page range."""

    model_config = ConfigDict(extra="forbid", strict=True)

    batch_id: str = Field(
        ...,
        min_length=1,
        max_length=64,
        pattern=_BATCH_ID_RE.pattern,
        description=(
            "Stable batch identifier using 1-64 letters, digits, '.', '_', ':', "
            "or '-'."
        ),
    )
    start_page: int = Field(
        ...,
        strict=True,
        ge=1,
        le=MAX_PAGE_NUMBER,
        description="Inclusive one-based first page in this batch.",
    )
    end_page: int = Field(
        ...,
        strict=True,
        ge=1,
        le=MAX_PAGE_NUMBER,
        description="Inclusive one-based last page in this batch.",
    )

    @model_validator(mode="after")
    def validate_page_range(self) -> DocumentBatchInput:
        if self.end_page < self.start_page:
            raise ValueError("end_page must be greater than or equal to start_page.")
        return self


class DocumentBatchPlanInput(BaseModel):
    """Strict public transport shape for an ordered passive PDF batch plan."""

    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal["carlina.document-batches.v1"] = Field(
        DOCUMENT_BATCH_PLAN_VERSION,
        description="Batch-plan schema version supported by Carlina.",
    )
    scheduling_mode: Literal["sequential", "concurrent"] = Field(
        ...,
        description="Whether batches run in order or independently.",
    )
    max_in_flight: int | None = Field(
        None, strict=True, ge=1, le=16,
        description="Optional concurrent work-packet window (1-16). Default runtime window is 4; sequential mode remains one.",
    )
    batches: list[DocumentBatchInput] = Field(
        ...,
        min_length=1,
        max_length=MAX_DOCUMENT_BATCHES,
        description="Ordered, non-overlapping inclusive page ranges.",
    )

    @model_validator(mode="after")
    def validate_batches(self) -> DocumentBatchPlanInput:
        seen_ids: set[str] = set()
        previous_end = 0
        for batch in self.batches:
            if batch.batch_id in seen_ids:
                raise ValueError("batch_id values must be unique within an execution.")
            if batch.start_page <= previous_end:
                raise ValueError("Batch ranges must be ordered and non-overlapping.")
            seen_ids.add(batch.batch_id)
            previous_end = batch.end_page
        return self


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def normalize_document_batch_plan(
    value: Mapping[str, Any] | DocumentBatchPlanInput | None,
) -> dict[str, Any] | None:
    """Validate and canonicalize one orchestrator-authored page-range plan."""

    if value is None:
        return None
    if isinstance(value, DocumentBatchPlanInput):
        value = value.model_dump(mode="python")
    if not isinstance(value, Mapping):
        raise ValueError("batch_plan must be a JSON object.")
    supplied_version = str(value.get("schema_version") or DOCUMENT_BATCH_PLAN_VERSION)
    if supplied_version != DOCUMENT_BATCH_PLAN_VERSION:
        raise ValueError("batch_plan schema_version is not supported.")
    scheduling_mode = str(value.get("scheduling_mode") or "").strip().lower()
    if scheduling_mode not in {"sequential", "concurrent"}:
        raise ValueError("batch_plan scheduling_mode must be sequential or concurrent.")
    max_in_flight = value.get("max_in_flight")
    if max_in_flight is not None and (type(max_in_flight) is not int or not 1 <= max_in_flight <= 16):
        raise ValueError("max_in_flight must be an integer from 1 to 16.")
    raw_batches = value.get("batches")
    if not isinstance(raw_batches, list) or not raw_batches:
        raise ValueError("batch_plan must contain at least one batch.")
    if len(raw_batches) > MAX_DOCUMENT_BATCHES:
        raise ValueError(f"batch_plan may contain at most {MAX_DOCUMENT_BATCHES} batches.")

    normalized_batches: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    previous_end = 0
    for sequence, raw_batch in enumerate(raw_batches):
        if not isinstance(raw_batch, Mapping):
            raise ValueError("Every document batch must be a JSON object.")
        batch_id = str(raw_batch.get("batch_id") or "").strip()
        if not _BATCH_ID_RE.fullmatch(batch_id):
            raise ValueError(
                "batch_id must be 1-64 characters and use letters, digits, '.', '_', ':', or '-'."
            )
        if batch_id in seen_ids:
            raise ValueError("batch_id values must be unique within an execution.")
        seen_ids.add(batch_id)
        start_page = raw_batch.get("start_page")
        end_page = raw_batch.get("end_page")
        if (
            isinstance(start_page, bool)
            or not isinstance(start_page, int)
            or isinstance(end_page, bool)
            or not isinstance(end_page, int)
        ):
            raise ValueError("Batch page bounds must be integers.")
        if not 1 <= start_page <= end_page <= MAX_PAGE_NUMBER:
            raise ValueError("Batch page bounds are invalid.")
        if start_page <= previous_end:
            raise ValueError("Batch ranges must be ordered and non-overlapping.")
        previous_end = end_page
        depends_on = (
            [normalized_batches[-1]["batch_id"]]
            if scheduling_mode == "sequential" and normalized_batches
            else []
        )
        normalized_batches.append(
            {
                "batch_id": batch_id,
                "sequence": sequence,
                "start_page": start_page,
                "end_page": end_page,
                "depends_on": depends_on,
            }
        )

    return {
        "schema_version": DOCUMENT_BATCH_PLAN_VERSION,
        "scheduling_mode": scheduling_mode,
        "batches": normalized_batches,
        **({"max_in_flight": max_in_flight} if max_in_flight is not None else {}),
    }


def bind_document_batches(
    plan: Mapping[str, Any], included_page_indexes: Sequence[int]
) -> list[dict[str, Any]]:
    """Bind a normalized plan to the parser's effective zero-based page set."""

    normalized = normalize_document_batch_plan(plan)
    if normalized is None:
        return []
    included = sorted({int(page) for page in included_page_indexes})
    assignments: list[dict[str, Any]] = []
    assigned: set[int] = set()
    for batch in normalized["batches"]:
        pages = [
            page
            for page in included
            if batch["start_page"] <= page + 1 <= batch["end_page"]
        ]
        assigned.update(pages)
        assignments.append({**copy.deepcopy(batch), "page_indexes": pages})
    missing = [page + 1 for page in included if page not in assigned]
    if missing:
        preview = ", ".join(str(page) for page in missing[:8])
        raise ValueError(
            "batch_plan does not cover every selected PDF page"
            + (f" (first missing: {preview})." if preview else ".")
        )
    return assignments


def build_document_context_artifact(
    *,
    source_path: str | Path,
    total_pages: int,
    included_page_indexes: Sequence[int],
    excluded_page_indexes: Sequence[int],
    page_side_map: Mapping[int, str],
    default_crop_box: Any,
    metadata: Mapping[str, Any] | None,
    pdf_options: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Freeze the shared metadata/topology/policy snapshot used by every batch."""

    source = Path(source_path)
    source_digest = hashlib.sha256()
    with source.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            source_digest.update(chunk)
    options = dict(pdf_options or {})
    payload = {
        "schema_version": DOCUMENT_CONTEXT_VERSION,
        "source": {
            "sha256": source_digest.hexdigest(),
            "total_pages": int(total_pages),
        },
        "page_topology": {
            "included_pages": [int(page) + 1 for page in included_page_indexes],
            "excluded_pages": [int(page) + 1 for page in excluded_page_indexes],
            "page_sides": {
                str(int(page) + 1): str(side)
                for page, side in sorted(page_side_map.items())
            },
            "default_crop_box": copy.deepcopy(default_crop_box),
        },
        "metadata": copy.deepcopy(dict(metadata or {})),
        "extraction_policy": {
            key: copy.deepcopy(options[key])
            for key in _CONTEXT_OPTION_KEYS
            if key in options
        },
    }
    return {
        "schema_version": DOCUMENT_CONTEXT_VERSION,
        "content_hash": _digest(payload),
        "content": payload,
    }


def build_boundary_handoff(
    *,
    batch_id: str,
    page_indexes: Sequence[int],
    html_parts: Sequence[str | None],
    current_headings: Mapping[str, Any] | None,
    current_author: str | None,
) -> dict[str, Any]:
    """Create a bounded deterministic handoff for the next sequential batch."""

    completed = [page for page in page_indexes if html_parts[page] is not None]
    last_page = completed[-1] if completed else None
    last_html = str(html_parts[last_page] or "") if last_page is not None else ""
    content = {
        "schema_version": DOCUMENT_BOUNDARY_VERSION,
        "batch_id": str(batch_id),
        "last_page": last_page + 1 if last_page is not None else None,
        "last_page_html": last_html[-MAX_BOUNDARY_HTML_CHARS:],
        "active_headings": copy.deepcopy(dict(current_headings or {})),
        "current_author": str(current_author or "") or None,
    }
    return {**content, "content_hash": _digest(content)}


def build_source_halo(
    *,
    first_page_index: int,
    included_page_indexes: Sequence[int],
    native_text_by_page: Mapping[int, str],
) -> dict[str, Any] | None:
    """Return bounded preceding source evidence for a concurrent batch edge."""

    included = set(included_page_indexes)
    preceding = int(first_page_index) - 1
    if preceding not in included:
        return None
    text = str(native_text_by_page.get(preceding) or "").strip()
    return {
        "schema_version": DOCUMENT_BOUNDARY_VERSION,
        "kind": "source_halo",
        "source_page": preceding + 1,
        "text": text[-MAX_HALO_TEXT_CHARS:],
    }


__all__ = [
    "DOCUMENT_BATCH_PLAN_VERSION",
    "DOCUMENT_BOUNDARY_VERSION",
    "DOCUMENT_CONTEXT_VERSION",
    "DocumentBatchInput",
    "DocumentBatchPlanInput",
    "bind_document_batches",
    "build_boundary_handoff",
    "build_document_context_artifact",
    "build_source_halo",
    "normalize_document_batch_plan",
]
