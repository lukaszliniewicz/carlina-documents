from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
from collections import Counter
from datetime import datetime, timedelta
from typing import Any, Callable, Iterable

from flask import current_app, has_app_context
from sqlalchemy import func

from iarf_assistant.core.model_capabilities import (
    admit_provider_payload,
    resolve_model_capabilities,
)
from iarf_assistant.core.runtime_contracts import estimate_tokens
from carlina.core.utc import utc_now_db
from iarf_assistant.extensions import db
from iarf_assistant.models import (
    DataContext,
    DataContextCollection,
    KnowledgeCollectionMembership,
    KnowledgeCoverageRun,
    KnowledgeCoverageSlice,
    KnowledgeIndexEntry,
    KnowledgeIndexGeneration,
    KnowledgeSavedView,
    KnowledgeSource,
    KnowledgeSourceUnit,
    KnowledgeSourceVersion,
)


KNOWLEDGE_SCHEMA_VERSION = 2
QUERY_STRATEGIES = {
    "auto",
    "structured",
    "lexical",
    "semantic",
    "hybrid",
    "full",
    "coverage",
    "programmatic",
}
QUERY_ORDERINGS = {
    "relevance",
    "occurred_at_asc",
    "occurred_at_desc",
    "source_order",
}
UNIT_RESULT_FIELDS = {
    "rank",
    "score",
    "component_scores",
    "matched_fields",
    "source_id",
    "source_type",
    "external_id",
    "title",
    "source_version_id",
    "version_number",
    "source_content_hash",
    "unit_id",
    "unit_external_id",
    "unit_ordinal",
    "unit_type",
    "unit_anchor",
    "unit_content_hash",
    "occurred_at",
    "metadata",
    "snippet",
    "content",
}
PROVENANCE_RESULT_FIELDS = {
    "source_id",
    "source_version_id",
    "unit_id",
    "unit_content_hash",
}
INDEX_GENERATION_TERMINAL_STATES = {"ready", "active", "failed", "retired"}
WORD_RE = re.compile(r"[^\W_]+(?:[-'][^\W_]+)*", re.UNICODE)
QUOTED_RE = re.compile(r'"([^"]+)"|“([^”]+)”')
ISO_DATE_RE = re.compile(r"\b(?:19|20)\d{2}[-/.]\d{1,2}[-/.]\d{1,2}\b")
PAGE_MARKER_RE = re.compile(
    r"(?im)^\s*<!--\s*Page\s+(\d+)\s*-->\s*$",
)


class KnowledgeFabricError(ValueError):
    pass


class KnowledgeOwnershipError(KnowledgeFabricError):
    pass


class KnowledgeVersionError(KnowledgeFabricError):
    pass


class IndexActivationConflict(KnowledgeFabricError):
    pass


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    values = value if isinstance(value, (list, tuple, set)) else [value]
    result: list[str] = []
    seen: set[str] = set()
    for item in values:
        text = str(item or "").strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def _int_list(value: Any) -> list[int]:
    result: list[int] = []
    for item in value if isinstance(value, (list, tuple, set)) else [value]:
        if item in (None, "", "all"):
            continue
        try:
            number = int(item)
        except (TypeError, ValueError):
            raise KnowledgeFabricError(f"Invalid integer identifier: {item}.") from None
        if number not in result:
            result.append(number)
    return result


def _content_hash(content: str) -> str:
    return hashlib.sha256(str(content or "").encode("utf-8")).hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _json_hash(value: Any) -> str:
    return _content_hash(_canonical_json(value))


def _as_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError as exc:
        raise KnowledgeFabricError(f"Invalid ISO-8601 datetime: {value}.") from exc


def _tokens(text: Any) -> list[str]:
    return [
        match.group(0).casefold()
        for match in WORD_RE.finditer(str(text or ""))
        if match.group(0)
    ]


def _safe_excerpt(text: str, *, query: str = "", max_chars: int = 1200) -> str:
    text = str(text or "")
    max_chars = max(80, int(max_chars or 1200))
    if len(text) <= max_chars:
        return text
    query_tokens = _tokens(query)
    lowered = text.casefold()
    positions = [
        lowered.find(token)
        for token in query_tokens
        if len(token) > 2 and lowered.find(token) >= 0
    ]
    center = min(positions) if positions else 0
    start = max(0, center - max_chars // 3)
    end = min(len(text), start + max_chars)
    start = max(0, end - max_chars)
    prefix = "…" if start else ""
    suffix = "…" if end < len(text) else ""
    return prefix + text[start:end] + suffix


def _source_or_404(source_id: str, user_id: int) -> KnowledgeSource:
    source = db.session.get(KnowledgeSource, str(source_id or ""))
    if source is None or source.user_id != user_id:
        raise KnowledgeOwnershipError("Knowledge source is unavailable.")
    return source


def _collection_or_404(
    collection_id: int,
    user_id: int,
) -> DataContextCollection:
    collection = db.session.get(DataContextCollection, int(collection_id))
    if collection is None or collection.user_id != user_id:
        raise KnowledgeOwnershipError("Knowledge collection is unavailable.")
    return collection


def _resolve_version(
    source: KnowledgeSource,
    *,
    version_id: str | None = None,
    version_number: int | None = None,
) -> KnowledgeSourceVersion:
    if version_id:
        version = db.session.get(KnowledgeSourceVersion, str(version_id))
        if version is None or version.source_id != source.id:
            raise KnowledgeVersionError("Source version is unavailable.")
        return version
    number = (
        int(version_number)
        if version_number is not None
        else int(source.current_version_number or 0)
    )
    version = KnowledgeSourceVersion.query.filter_by(
        source_id=source.id,
        version_number=number,
    ).one_or_none()
    if version is None:
        raise KnowledgeVersionError("Source has no readable current version.")
    return version


def document_units_from_text(
    content: str,
    *,
    max_unit_chars: int = 20_000,
) -> list[dict[str, Any]]:
    """Create ordered page/section units without changing the canonical text."""
    content = str(content or "")
    if not content:
        return []
    max_unit_chars = max(500, int(max_unit_chars or 20_000))
    markers = list(PAGE_MARKER_RE.finditer(content))
    page_parts: list[tuple[int | None, int, int, str]] = []
    if markers:
        if markers[0].start() > 0 and content[:markers[0].start()].strip():
            page_parts.append((None, 0, markers[0].start(), content[:markers[0].start()]))
        for index, marker in enumerate(markers):
            start = marker.end()
            end = markers[index + 1].start() if index + 1 < len(markers) else len(content)
            page_parts.append((int(marker.group(1)), start, end, content[start:end]))
    else:
        page_parts.append((None, 0, len(content), content))

    units: list[dict[str, Any]] = []
    for page_number, base_start, _base_end, page_text in page_parts:
        if not page_text:
            continue
        offset = 0
        while offset < len(page_text):
            hard_end = min(len(page_text), offset + max_unit_chars)
            end = hard_end
            if hard_end < len(page_text):
                boundary = max(
                    page_text.rfind("\n\n", offset, hard_end),
                    page_text.rfind("\n", offset, hard_end),
                    page_text.rfind(". ", offset, hard_end),
                )
                if boundary > offset + max_unit_chars // 2:
                    end = boundary + (2 if page_text[boundary:boundary + 2] == ". " else 1)
            fragment = page_text[offset:end]
            if fragment.strip():
                units.append({
                    "unit_type": "page" if page_number is not None else "segment",
                    "external_id": (
                        f"page:{page_number}:part:{1 + sum(1 for row in units if row.get('metadata', {}).get('page') == page_number)}"
                        if page_number is not None
                        else None
                    ),
                    "anchor": f"Page {page_number}" if page_number is not None else None,
                    "content": fragment,
                    "start_offset": base_start + offset,
                    "end_offset": base_start + end,
                    "metadata": {"page": page_number} if page_number is not None else {},
                })
            offset = end
    return units


def ensure_source_identity(
    *,
    user_id: int,
    source_type: str,
    external_id: Any,
    title: str | None = None,
    mime_type: str | None = None,
    metadata: dict[str, Any] | None = None,
    access_policy: dict[str, Any] | None = None,
    source_created_at: Any = None,
    source_modified_at: Any = None,
    legacy_data_context_id: int | None = None,
    commit: bool = False,
) -> tuple[KnowledgeSource, bool]:
    """Create or refresh a source identity before extracted content exists."""
    normalized_type = str(source_type or "").strip().lower()
    normalized_external_id = str(external_id or "").strip()
    if not normalized_type or not normalized_external_id:
        raise KnowledgeFabricError("source_type and external_id are required.")
    source = KnowledgeSource.query.filter_by(
        user_id=user_id,
        source_type=normalized_type,
        external_id=normalized_external_id,
    ).one_or_none()
    created = source is None
    if source is None:
        source = KnowledgeSource(
            user_id=user_id,
            source_type=normalized_type,
            external_id=normalized_external_id,
        )
        db.session.add(source)
    source.title = title
    source.mime_type = mime_type
    source.metadata_json = dict(metadata or {})
    source.access_policy_json = dict(access_policy or {})
    source.source_created_at = _as_datetime(source_created_at)
    source.source_modified_at = _as_datetime(source_modified_at)
    if legacy_data_context_id is not None:
        source.legacy_data_context_id = int(legacy_data_context_id)
    db.session.flush()
    if commit:
        db.session.commit()
    return source, created


def upsert_source(
    *,
    user_id: int,
    source_type: str,
    external_id: Any,
    content: str,
    title: str | None = None,
    mime_type: str | None = None,
    units: list[dict[str, Any]] | None = None,
    metadata: dict[str, Any] | None = None,
    access_policy: dict[str, Any] | None = None,
    source_created_at: Any = None,
    source_modified_at: Any = None,
    extractor_name: str | None = None,
    extractor_version: str | None = None,
    extractor_config: dict[str, Any] | None = None,
    provenance: dict[str, Any] | None = None,
    legacy_data_context_id: int | None = None,
    commit: bool = False,
) -> tuple[KnowledgeSource, KnowledgeSourceVersion, bool]:
    """Upsert logical identity and append an immutable version when content changed."""
    content = str(content or "")
    digest = _content_hash(content)
    source, _created = ensure_source_identity(
        user_id=user_id,
        source_type=source_type,
        external_id=external_id,
        title=title,
        mime_type=mime_type,
        metadata=metadata,
        access_policy=access_policy,
        source_created_at=source_created_at,
        source_modified_at=source_modified_at,
        legacy_data_context_id=legacy_data_context_id,
    )

    existing = KnowledgeSourceVersion.query.filter_by(
        source_id=source.id,
        content_hash=digest,
    ).one_or_none()
    if existing is not None:
        source.current_version_number = existing.version_number
        db.session.flush()
        if commit:
            db.session.commit()
        return source, existing, False

    latest_number = (
        db.session.query(func.max(KnowledgeSourceVersion.version_number))
        .filter(KnowledgeSourceVersion.source_id == source.id)
        .scalar()
        or 0
    )
    version = KnowledgeSourceVersion(
        source_id=source.id,
        version_number=int(latest_number) + 1,
        content_hash=digest,
        content=content,
        extraction_status="ready",
        extractor_name=extractor_name,
        extractor_version=extractor_version,
        extractor_config_json=dict(extractor_config or {}),
        provenance_json=dict(provenance or {}),
        char_count=len(content),
        byte_count=len(content.encode("utf-8")),
        token_estimate=estimate_tokens(content),
        source_modified_at=source.source_modified_at,
    )
    db.session.add(version)
    db.session.flush()

    unit_payloads = (
        document_units_from_text(content)
        if units is None
        else list(units)
    )
    for ordinal, unit_payload in enumerate(unit_payloads):
        if not isinstance(unit_payload, dict):
            raise KnowledgeVersionError(f"Source unit {ordinal} must be an object.")
        unit_content = str(unit_payload.get("content") or "")
        if not unit_content and content:
            raise KnowledgeVersionError(f"Source unit {ordinal} is empty.")
        start_offset = unit_payload.get("start_offset")
        end_offset = unit_payload.get("end_offset")
        db.session.add(KnowledgeSourceUnit(
            source_version_id=version.id,
            ordinal=ordinal,
            unit_type=str(unit_payload.get("unit_type") or "segment"),
            external_id=(
                str(unit_payload.get("external_id"))
                if unit_payload.get("external_id") is not None
                else None
            ),
            anchor=unit_payload.get("anchor"),
            content=unit_content,
            content_hash=_content_hash(unit_content),
            start_offset=int(start_offset) if start_offset is not None else None,
            end_offset=int(end_offset) if end_offset is not None else None,
            occurred_at=_as_datetime(unit_payload.get("occurred_at")),
            char_count=len(unit_content),
            token_estimate=estimate_tokens(unit_content),
            metadata_json=dict(unit_payload.get("metadata") or {}),
        ))
    source.current_version_number = version.version_number
    db.session.flush()
    if commit:
        db.session.commit()
    return source, version, True


def add_collection_membership(
    *,
    user_id: int,
    collection_id: int,
    source_id: str,
    frozen_version_id: str | None = None,
    position: int | None = None,
    metadata: dict[str, Any] | None = None,
    commit: bool = False,
) -> KnowledgeCollectionMembership:
    collection = _collection_or_404(collection_id, user_id)
    source = _source_or_404(source_id, user_id)
    version = None
    if frozen_version_id:
        version = db.session.get(KnowledgeSourceVersion, frozen_version_id)
        if version is None or version.source_id != source.id:
            raise KnowledgeVersionError("Frozen membership version is unavailable.")
    membership = KnowledgeCollectionMembership.query.filter_by(
        collection_id=collection.id,
        source_id=source.id,
    ).one_or_none()
    if membership is None:
        membership = KnowledgeCollectionMembership(
            collection_id=collection.id,
            source_id=source.id,
        )
        db.session.add(membership)
    membership.frozen_version_id = version.id if version is not None else None
    membership.position = int(position) if position is not None else None
    membership.metadata_json = dict(metadata or {})
    db.session.flush()
    if commit:
        db.session.commit()
    return membership


def replace_collection_memberships(
    *,
    user_id: int,
    collection_id: int,
    sources: Iterable[dict[str, Any] | tuple[str, str | None] | str],
    commit: bool = False,
) -> list[KnowledgeCollectionMembership]:
    _collection_or_404(collection_id, user_id)
    desired: list[tuple[str, str | None, dict[str, Any]]] = []
    for row in sources:
        if isinstance(row, str):
            desired.append((row, None, {}))
        elif isinstance(row, tuple):
            desired.append((str(row[0]), row[1], {}))
        elif isinstance(row, dict):
            desired.append((
                str(row.get("source_id") or ""),
                row.get("frozen_version_id"),
                dict(row.get("metadata") or {}),
            ))
        else:
            raise KnowledgeFabricError("Invalid collection membership payload.")
    desired_ids = {source_id for source_id, _version, _metadata in desired if source_id}
    existing = KnowledgeCollectionMembership.query.filter_by(
        collection_id=collection_id,
    ).all()
    for membership in existing:
        if membership.source_id not in desired_ids:
            db.session.delete(membership)
    result = []
    for position, (source_id, frozen_version_id, metadata) in enumerate(desired):
        result.append(add_collection_membership(
            user_id=user_id,
            collection_id=collection_id,
            source_id=source_id,
            frozen_version_id=frozen_version_id,
            position=position,
            metadata=metadata,
            commit=False,
        ))
    db.session.flush()
    if commit:
        db.session.commit()
    return result


def upsert_saved_view(
    *,
    user_id: int,
    collection_id: int,
    source_type: str,
    query: dict[str, Any],
    result_count: int | None = None,
    refresh_status: str = "ready",
    error: dict[str, Any] | None = None,
    commit: bool = False,
) -> KnowledgeSavedView:
    _collection_or_404(collection_id, user_id)
    saved_view = KnowledgeSavedView.query.filter_by(
        collection_id=collection_id,
    ).one_or_none()
    if saved_view is None:
        saved_view = KnowledgeSavedView(
            collection_id=collection_id,
            user_id=user_id,
            source_type=str(source_type or "").strip().lower(),
            query_json=dict(query or {}),
        )
        db.session.add(saved_view)
    else:
        if saved_view.user_id != user_id:
            raise KnowledgeOwnershipError("Saved view is unavailable.")
        saved_view.source_type = str(source_type or saved_view.source_type).strip().lower()
        if saved_view.query_json != dict(query or {}):
            saved_view.query_version += 1
        saved_view.query_json = dict(query or {})
    if refresh_status not in {"pending", "refreshing", "ready", "failed"}:
        raise KnowledgeFabricError("Invalid saved-view refresh status.")
    saved_view.refresh_status = refresh_status
    saved_view.result_count = max(0, int(result_count or 0))
    saved_view.last_refreshed_at = utc_now_db()
    saved_view.error_json = dict(error) if isinstance(error, dict) else None
    db.session.flush()
    if commit:
        db.session.commit()
    return saved_view


def _membership_version(
    membership: KnowledgeCollectionMembership,
) -> KnowledgeSourceVersion:
    if membership.frozen_version_id:
        version = db.session.get(
            KnowledgeSourceVersion,
            membership.frozen_version_id,
        )
        if version is not None:
            return version
    return _resolve_version(membership.source)


def materialize_source(
    *,
    user_id: int,
    source_id: str,
    version_id: str | None = None,
    version_number: int | None = None,
    start_ordinal: int | None = None,
    end_ordinal: int | None = None,
    unit_ids: list[str] | None = None,
    include_content: bool = True,
) -> dict[str, Any]:
    source = _source_or_404(source_id, user_id)
    version = _resolve_version(
        source,
        version_id=version_id,
        version_number=version_number,
    )
    selected_ids = set(_string_list(unit_ids))
    units = list(version.units)
    if selected_ids:
        units = [unit for unit in units if unit.id in selected_ids]
    if start_ordinal is not None:
        units = [unit for unit in units if unit.ordinal >= int(start_ordinal)]
    if end_ordinal is not None:
        units = [unit for unit in units if unit.ordinal <= int(end_ordinal)]
    full_selection = (
        not selected_ids
        and start_ordinal is None
        and end_ordinal is None
    )
    return {
        "schema_version": KNOWLEDGE_SCHEMA_VERSION,
        "source": source.to_dict(),
        "version": version.to_dict(),
        "selection": {
            "full": full_selection,
            "start_ordinal": start_ordinal,
            "end_ordinal": end_ordinal,
            "unit_ids": list(selected_ids),
            "selected_unit_count": len(units),
            "total_unit_count": len(version.units),
        },
        "content": (
            version.content
            if include_content and full_selection
            else (
                "\n\n".join(unit.content for unit in units)
                if include_content
                else None
            )
        ),
        "units": [
            unit.to_dict(include_content=include_content and not full_selection)
            for unit in units
        ],
        "coverage": {
            "covered_ordinals": [unit.ordinal for unit in units],
            "first_ordinal": units[0].ordinal if units else None,
            "last_ordinal": units[-1].ordinal if units else None,
            "complete": len(units) == len(version.units),
            "content_hash": version.content_hash,
        },
    }


def source_message(
    materialization: dict[str, Any],
    *,
    instruction: str | None = None,
) -> dict[str, Any]:
    source = materialization["source"]
    version = materialization["version"]
    selection = materialization["selection"]
    unit_manifest = [
        {
            "unit_id": unit["id"],
            "ordinal": unit["ordinal"],
            "type": unit["unit_type"],
            "anchor": unit.get("anchor"),
            "occurred_at": unit.get("occurred_at"),
        }
        for unit in materialization.get("units") or []
    ]
    header = {
        "source_id": source["id"],
        "source_type": source["source_type"],
        "external_id": source["external_id"],
        "title": source.get("title"),
        "source_version_id": version["id"],
        "version_number": version["version_number"],
        "content_hash": version["content_hash"],
        "selection": selection,
        "unit_manifest": unit_manifest,
    }
    return {
        "role": "user",
        "content": (
            (str(instruction).strip() + "\n\n" if instruction else "")
            + "<canonical-source>\n"
            + _canonical_json(header)
            + "\n</canonical-source>\n\n"
            + str(materialization.get("content") or "")
        ),
    }


def admit_full_source(
    *,
    user_id: int,
    source_id: str,
    model_id: str,
    base_messages: list[dict[str, Any]] | None = None,
    instruction: str | None = None,
    mode: str = "assisted",
    tools: list[dict[str, Any]] | None = None,
    response_format: dict[str, Any] | None = None,
    requested_output_tokens: int | None = None,
    token_counter: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    materialization = materialize_source(
        user_id=user_id,
        source_id=source_id,
        include_content=True,
    )
    message = source_message(materialization, instruction=instruction)
    profile = resolve_model_capabilities(model_id, user_id=user_id)
    messages = [*(base_messages or []), message]
    admission = admit_provider_payload(
        profile=profile,
        messages=messages,
        tools=tools,
        response_format=response_format,
        requested_output_tokens=requested_output_tokens,
        mode=mode,
        token_counter=token_counter,
    )
    return {
        "status": "admitted" if admission.admitted else "overflow",
        "strategy": "full",
        "source_id": source_id,
        "source_version_id": materialization["version"]["id"],
        "messages": messages if admission.admitted else None,
        "admission": admission.to_dict(),
        "coverage": materialization["coverage"],
        "recommended_strategy": None if admission.admitted else "coverage",
        "manual_preserved": str(mode or "").lower() == "manual",
    }


def infer_query_strategy(
    query: str,
    *,
    requested: str | None = None,
    filters: dict[str, Any] | None = None,
    source_ids: list[str] | None = None,
) -> tuple[str, list[str]]:
    requested = str(requested or "auto").strip().lower()
    if requested not in QUERY_STRATEGIES:
        raise KnowledgeFabricError(f"Unsupported knowledge query strategy: {requested}.")
    if requested != "auto":
        return requested, ["explicit_strategy"]
    query_text = str(query or "")
    lowered = query_text.casefold()
    filters = filters if isinstance(filters, dict) else {}
    reasons: list[str] = []
    if source_ids and len(_string_list(source_ids)) == 1 and any(
        phrase in lowered
        for phrase in (
            "exhaustive",
            "every source unit",
            "every page",
            "every message",
            "all pages",
            "all messages",
            "coverage run",
        )
    ):
        reasons.append("explicit_exhaustive_coverage_request")
        return "coverage", reasons
    if source_ids and any(
        phrase in lowered
        for phrase in (
            "whole",
            "entire",
            "full document",
            "full file",
            "full thread",
            "read it all",
            "read all",
            "complete source",
        )
    ):
        reasons.append("explicit_complete_source_request")
        return "full", reasons
    if any(
        key in filters
        for key in (
            "date_from",
            "date_to",
            "after",
            "before",
            "sender",
            "recipient",
            "external_id",
            "source_type",
            "unit_type",
        )
    ):
        reasons.append("structured_filters")
        return "structured", reasons
    if ISO_DATE_RE.search(query_text) or re.search(
        r"\b(?:before|after|between|on|from|to)\s+(?:19|20)\d{2}\b",
        lowered,
    ):
        reasons.append("date_or_range_language")
        return "structured", reasons
    if QUOTED_RE.search(query_text):
        reasons.append("quoted_phrase")
        return "lexical", reasons
    identifier_language = re.search(
        r"\b(?:message-id|exact\s+id|identifier|invoice\s+(?:number|no\.?)|"
        r"case\s+(?:number|no\.?)|ticket\s+(?:number|no\.?)|"
        r"reference\s+(?:number|no\.?)|code\s*[:=#])\b",
        lowered,
    )
    identifier_shape = re.search(
        r"\b(?=[A-Z0-9_:/.-]*\d{2,})"
        r"[A-Z0-9]{2,}(?:[-_:/.][A-Z0-9]{2,})+\b",
        query_text,
        flags=re.IGNORECASE,
    )
    correction_language = re.search(
        r"\b(?:corrected|correction|superseded|revised|operative|"
        r"authoritative|final|latest|current|approved|rejected|"
        r"proposal|deadline|go-live|amended|replacement)\b",
        lowered,
    )
    if identifier_language or identifier_shape:
        reasons.append("identifier_or_rare_term")
        return "hybrid", reasons
    if correction_language:
        reasons.append("correction_or_chronology")
        return "hybrid", reasons
    reasons.append("default_semantic_retrieval")
    return "semantic", reasons


def _candidate_units(
    *,
    user_id: int,
    collection_ids: list[int] | None,
    source_ids: list[str] | None,
    filters: dict[str, Any],
) -> list[tuple[KnowledgeSource, KnowledgeSourceVersion, KnowledgeSourceUnit]]:
    source_id_filter = set(_string_list(source_ids))
    collection_id_filter = _int_list(collection_ids or [])
    source_query = KnowledgeSource.query.filter_by(user_id=user_id)
    if source_id_filter:
        source_query = source_query.filter(KnowledgeSource.id.in_(source_id_filter))
    if filters.get("source_type"):
        source_query = source_query.filter(
            KnowledgeSource.source_type.in_(_string_list(filters["source_type"]))
        )
    if filters.get("external_id"):
        source_query = source_query.filter(
            KnowledgeSource.external_id.in_(_string_list(filters["external_id"]))
        )
    sources = source_query.all()

    memberships_by_source: dict[
        str,
        list[KnowledgeCollectionMembership],
    ] = {}
    if collection_id_filter:
        owned = DataContextCollection.query.filter(
            DataContextCollection.id.in_(collection_id_filter),
            DataContextCollection.user_id == user_id,
        ).all()
        owned_ids = {collection.id for collection in owned}
        if owned_ids != set(collection_id_filter):
            raise KnowledgeOwnershipError("One or more knowledge collections are unavailable.")
        memberships = KnowledgeCollectionMembership.query.filter(
            KnowledgeCollectionMembership.collection_id.in_(collection_id_filter),
        ).all()
        for membership in memberships:
            memberships_by_source.setdefault(membership.source_id, []).append(
                membership
            )
        sources = [
            source for source in sources
            if source.id in memberships_by_source
        ]

    after = _as_datetime(filters.get("date_from") or filters.get("after"))
    before = _as_datetime(filters.get("date_to") or filters.get("before"))
    unit_types = {
        value.casefold()
        for value in _string_list(filters.get("unit_type"))
    }
    results = []
    seen_unit_ids: set[str] = set()
    for source in sources:
        scoped_memberships = memberships_by_source.get(source.id)
        if not scoped_memberships:
            if int(source.current_version_number or 0) <= 0:
                continue
            scoped_memberships = [None]
        for membership in scoped_memberships:
            frozen_id = (
                membership.frozen_version_id
                if membership is not None
                else None
            )
            if frozen_id is None and int(source.current_version_number or 0) <= 0:
                continue
            version = _resolve_version(source, version_id=frozen_id)
            membership_metadata = (
                dict(membership.metadata_json or {})
                if membership is not None
                else {}
            )
            nested_selector = membership_metadata.get("unit_selector")
            if not isinstance(nested_selector, dict):
                nested_selector = {}
            selected_external_ids = set(_string_list(
                membership_metadata.get("unit_external_ids")
                or nested_selector.get("external_ids")
            ))
            selected_ordinals = set(_int_list(
                membership_metadata.get("unit_ordinals")
                or nested_selector.get("ordinals")
                or []
            ))
            has_selector = bool(selected_external_ids or selected_ordinals)
            for unit in version.units:
                if has_selector and (
                    str(unit.external_id or "") not in selected_external_ids
                    and int(unit.ordinal) not in selected_ordinals
                ):
                    continue
                if after and (unit.occurred_at is None or unit.occurred_at < after):
                    continue
                if before and (unit.occurred_at is None or unit.occurred_at > before):
                    continue
                if unit_types and unit.unit_type.casefold() not in unit_types:
                    continue
                if unit.id in seen_unit_ids:
                    continue
                seen_unit_ids.add(unit.id)
                results.append((source, version, unit))
    return results


def _metadata_matches(
    source: KnowledgeSource,
    unit: KnowledgeSourceUnit,
    filters: dict[str, Any],
) -> tuple[bool, list[str]]:
    source_meta = source.metadata_json or {}
    unit_meta = unit.metadata_json or {}
    combined = {**source_meta, **unit_meta}
    matched_fields: list[str] = []
    aliases = {
        "sender": ("sender", "sender_email", "from"),
        "recipient": ("recipient", "recipients", "to", "cc", "bcc"),
        "subject": ("subject", "title"),
        "label": ("label", "labels", "folder", "folders"),
        "state": ("state", "status", "read_state"),
    }
    for filter_name, metadata_keys in aliases.items():
        requested = _string_list(filters.get(filter_name))
        if not requested:
            continue
        values: list[str] = []
        for key in metadata_keys:
            value = combined.get(key)
            if isinstance(value, dict):
                values.extend(str(item) for item in value.values())
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        values.extend(str(part) for part in item.values())
                    else:
                        values.append(str(item))
            elif value is not None:
                values.append(str(value))
        haystack = "\n".join(values).casefold()
        if not any(item.casefold() in haystack for item in requested):
            return False, matched_fields
        matched_fields.append(filter_name)
    return True, matched_fields


def _lexical_scores(
    query: str,
    candidates: list[tuple[KnowledgeSource, KnowledgeSourceVersion, KnowledgeSourceUnit]],
) -> dict[str, tuple[float, list[str]]]:
    query_tokens = _tokens(query)
    phrases = [
        next(part for part in match.groups() if part is not None).casefold()
        for match in QUOTED_RE.finditer(query)
    ]
    if not query_tokens and not phrases:
        return {}
    document_frequency: Counter[str] = Counter()
    tokenized: dict[str, list[str]] = {}
    for source, _version, unit in candidates:
        row_tokens = _tokens(f"{source.title or ''}\n{unit.content}")
        tokenized[unit.id] = row_tokens
        document_frequency.update(set(row_tokens))
    total_docs = max(1, len(candidates))
    average_document_length = (
        sum(len(tokens) for tokens in tokenized.values()) / total_docs
        if tokenized
        else 1.0
    )
    average_document_length = max(1.0, average_document_length)
    k1 = 1.5
    b = 0.75
    query_term_frequency = Counter(query_tokens)
    scores: dict[str, tuple[float, list[str]]] = {}
    for source, _version, unit in candidates:
        row_tokens = tokenized[unit.id]
        counts = Counter(row_tokens)
        score = 0.0
        matched_fields: list[str] = []
        document_length_normalization = (
            1.0 - b
            + b * (len(row_tokens) / average_document_length)
        )
        for token, query_frequency in query_term_frequency.items():
            frequency = counts.get(token, 0)
            if frequency:
                inverse = math.log(
                    1.0
                    + (
                        (total_docs - document_frequency[token] + 0.5)
                        / (document_frequency[token] + 0.5)
                    )
                )
                numerator = frequency * (k1 + 1.0)
                denominator = (
                    frequency + k1 * document_length_normalization
                )
                if denominator:
                    score += (
                        float(query_frequency)
                        * inverse
                        * (numerator / denominator)
                    )
        haystack = unit.content.casefold()
        title = str(source.title or "").casefold()
        for phrase in phrases:
            if phrase in haystack:
                score += 8.0
                matched_fields.append("content_exact_phrase")
            if phrase in title:
                score += 12.0
                matched_fields.append("title_exact_phrase")
        for token in set(query_tokens):
            if len(token) > 2 and token in title:
                score += 1.5
                if "title" not in matched_fields:
                    matched_fields.append("title")
        if score > 0:
            if "content" not in matched_fields:
                matched_fields.append("content")
            scores[unit.id] = (score, matched_fields)
    return scores


def _rank_map(
    scores: dict[str, tuple[float, list[str]]],
    *,
    limit: int | None = None,
) -> dict[str, int]:
    ordered = sorted(
        scores.items(),
        key=lambda item: (-item[1][0], item[0]),
    )
    if limit is not None:
        ordered = ordered[:max(0, int(limit))]
    return {
        unit_id: rank
        for rank, (unit_id, _payload) in enumerate(
            ordered,
            start=1,
        )
    }


def _hybrid_fusion_weights(
    strategy_reasons: Iterable[str],
) -> dict[str, float]:
    reasons = {str(reason or "") for reason in strategy_reasons}
    if "identifier_or_rare_term" in reasons:
        return {"lexical": 1.0, "semantic": 0.85}
    if "correction_or_chronology" in reasons:
        return {"lexical": 0.55, "semantic": 1.0}
    return {"lexical": 0.75, "semantic": 1.0}


def _result_row(
    source: KnowledgeSource,
    version: KnowledgeSourceVersion,
    unit: KnowledgeSourceUnit,
    *,
    rank: int,
    score: float,
    component_scores: dict[str, float],
    matched_fields: list[str],
    query: str,
    include_content: bool,
    snippet_chars: int,
) -> dict[str, Any]:
    return {
        "rank": rank,
        "score": score,
        "component_scores": component_scores,
        "matched_fields": sorted(set(matched_fields)),
        "source_id": source.id,
        "source_type": source.source_type,
        "external_id": source.external_id,
        "title": source.title,
        "source_version_id": version.id,
        "version_number": version.version_number,
        "source_content_hash": version.content_hash,
        "unit_id": unit.id,
        "unit_external_id": unit.external_id,
        "unit_ordinal": unit.ordinal,
        "unit_type": unit.unit_type,
        "unit_anchor": unit.anchor,
        "unit_content_hash": unit.content_hash,
        "occurred_at": unit.occurred_at.isoformat() if unit.occurred_at else None,
        "metadata": unit.metadata_json or {},
        "snippet": _safe_excerpt(unit.content, query=query, max_chars=snippet_chars),
        "content": unit.content if include_content else None,
    }


def _project_unit_result_fields(
    results: list[dict[str, Any]],
    fields: list[str] | None,
) -> tuple[list[dict[str, Any]], list[str]]:
    requested = _string_list(fields)
    if not requested:
        return results, []
    invalid = sorted(set(requested) - UNIT_RESULT_FIELDS)
    if invalid:
        raise KnowledgeFabricError(
            "Unsupported knowledge result fields: " + ", ".join(invalid)
        )
    selected = set(requested) | PROVENANCE_RESULT_FIELDS
    return [
        {
            key: value
            for key, value in result.items()
            if key in selected
        }
        for result in results
    ], sorted(selected)


def query_knowledge(
    *,
    user_id: int,
    query: str = "",
    strategy: str = "auto",
    collection_ids: list[int] | None = None,
    source_ids: list[str] | None = None,
    filters: dict[str, Any] | None = None,
    limit: int = 10,
    include_content: bool = False,
    snippet_chars: int = 1200,
    fields: list[str] | None = None,
    order_by: str = "relevance",
    semantic_search: Callable[..., list[dict[str, Any]]] | None = None,
    program: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Typed, provenance-preserving query interface over canonical knowledge."""
    result_field_projection = fields
    filters = dict(filters or {})
    # Dates are better handled as deterministic range predicates than as
    # semantic hints. Infer the most common ISO date forms when the caller did
    # not already provide an explicit range.
    date_match = ISO_DATE_RE.search(str(query or ""))
    if date_match and not any(
        key in filters for key in ("date_from", "date_to", "after", "before")
    ):
        date_text = date_match.group(0).replace("/", "-").replace(".", "-")
        date_value = datetime.fromisoformat(date_text)
        lowered_query = str(query or "").casefold()
        if re.search(r"\bafter\b", lowered_query):
            filters["after"] = date_value
        elif re.search(r"\bbefore\b", lowered_query):
            filters["before"] = date_value
        else:
            filters["date_from"] = date_value
            filters["date_to"] = date_value + timedelta(days=1) - timedelta(microseconds=1)
    resolved, reasons = infer_query_strategy(
        query,
        requested=strategy,
        filters=filters,
        source_ids=source_ids,
    )
    planned_resolved = resolved
    order_by = str(order_by or "relevance").strip().lower()
    if order_by not in QUERY_ORDERINGS:
        raise KnowledgeFabricError(
            f"Unsupported knowledge result ordering: {order_by}."
        )
    limit = max(1, min(int(limit or 10), 500))
    candidates = _candidate_units(
        user_id=user_id,
        collection_ids=collection_ids,
        source_ids=source_ids,
        filters=filters,
    )
    filtered = []
    structured_matches: dict[str, list[str]] = {}
    for source, version, unit in candidates:
        matches, fields = _metadata_matches(source, unit, filters)
        if matches:
            filtered.append((source, version, unit))
            structured_matches[unit.id] = fields
    candidates = filtered

    if resolved == "full":
        resolved_source_ids = _string_list(source_ids)
        if not resolved_source_ids:
            raise KnowledgeFabricError("Full materialization requires explicit source_ids.")
        full_results = [
            materialize_source(
                user_id=user_id,
                source_id=source_id,
                include_content=True,
            )
            for source_id in resolved_source_ids[:limit]
        ]
        return {
            "schema_version": KNOWLEDGE_SCHEMA_VERSION,
            "strategy_requested": strategy,
            "strategy_resolved": resolved,
            "strategy_reasons": reasons,
            "results": full_results,
            "result_count": len(full_results),
            "diagnostics": {
                "candidate_unit_count": len(candidates),
                "retrieval": "complete_source_materialization",
            },
        }

    if resolved == "coverage":
        resolved_source_ids = _string_list(source_ids)
        if len(resolved_source_ids) != 1:
            raise KnowledgeFabricError("Coverage requires exactly one source_id.")
        run = create_coverage_run(
            user_id=user_id,
            source_id=resolved_source_ids[0],
            commit=False,
        )
        return {
            "schema_version": KNOWLEDGE_SCHEMA_VERSION,
            "strategy_requested": strategy,
            "strategy_resolved": resolved,
            "strategy_reasons": reasons,
            "results": [{
                "coverage_run_id": run.id,
                "source_id": run.source_id,
                "source_version_id": run.source_version_id,
                "status": run.status,
                "total_units": run.total_units,
                "slice_count": len(run.slices),
                "manifest": run.manifest_json or {},
            }],
            "result_count": 1,
            "diagnostics": {"retrieval": "exhaustive_coverage_manifest"},
        }

    if resolved == "programmatic":
        program = dict(program or {})
        operation = str(program.get("operation") or "count").lower()
        if operation == "count":
            result = {"count": len(candidates)}
        elif operation == "group_count":
            field = str(program.get("field") or "source_type")
            counts: Counter[str] = Counter()
            for source, _version, unit in candidates:
                if field == "source_type":
                    value = source.source_type
                elif field == "unit_type":
                    value = unit.unit_type
                else:
                    value = str((unit.metadata_json or {}).get(field) or "<missing>")
                counts[str(value)] += 1
            result = {"field": field, "counts": dict(counts)}
        else:
            raise KnowledgeFabricError(f"Unsupported programmatic operation: {operation}.")
        return {
            "schema_version": KNOWLEDGE_SCHEMA_VERSION,
            "strategy_requested": strategy,
            "strategy_resolved": resolved,
            "strategy_reasons": reasons,
            "results": [result],
            "result_count": 1,
            "diagnostics": {
                "candidate_unit_count": len(candidates),
                "retrieval": "deterministic_program",
            },
        }

    fusion_candidate_limit = max(limit * 3, 30)
    lexical = (
        _lexical_scores(query, candidates)
        if resolved in {"lexical", "hybrid"}
        else {}
    )
    semantic_payloads = (
        semantic_search(
            query=query,
            unit_ids=[unit.id for _source, _version, unit in candidates],
            limit=fusion_candidate_limit,
            user_id=user_id,
        )
        if resolved in {"semantic", "hybrid"} and callable(semantic_search)
        else []
    )
    semantic: dict[str, tuple[float, list[str]]] = {}
    for payload in semantic_payloads or []:
        if not isinstance(payload, dict):
            continue
        unit_id = str(payload.get("unit_id") or "")
        try:
            score = float(payload.get("score") or 0)
        except (TypeError, ValueError):
            score = 0.0
        if unit_id and score:
            semantic[unit_id] = (score, ["semantic"])

    semantic_attempted = (
        planned_resolved in {"semantic", "hybrid"}
        and callable(semantic_search)
    )
    if planned_resolved in {"semantic", "hybrid"} and not semantic:
        if not lexical:
            lexical = _lexical_scores(query, candidates)
        resolved = "lexical"
        reasons.append("semantic_component_unavailable_fallback_lexical")
    elif planned_resolved == "hybrid" and not lexical:
        resolved = "semantic"
        reasons.append("lexical_component_empty_fallback_semantic")

    fusion_weights: dict[str, float] | None = None
    if resolved == "structured":
        ordered_ids = [unit.id for _source, _version, unit in candidates]
        final_scores = {
            unit_id: (1.0, structured_matches.get(unit_id, []))
            for unit_id in ordered_ids
        }
    elif resolved == "lexical":
        final_scores = lexical
    elif resolved == "semantic":
        final_scores = semantic
    else:
        fusion_weights = _hybrid_fusion_weights(
            reasons,
        )
        lexical_ranks = _rank_map(
            lexical,
            limit=fusion_candidate_limit,
        )
        semantic_ranks = _rank_map(
            semantic,
            limit=fusion_candidate_limit,
        )
        final_scores: dict[str, tuple[float, list[str]]] = {}
        for unit_id in set(lexical_ranks) | set(semantic_ranks):
            score = 0.0
            fields: list[str] = []
            if unit_id in lexical_ranks:
                score += (
                    fusion_weights["lexical"]
                    / (60 + lexical_ranks[unit_id])
                )
                fields.extend(lexical[unit_id][1])
            if unit_id in semantic_ranks:
                score += (
                    fusion_weights["semantic"]
                    / (60 + semantic_ranks[unit_id])
                )
                fields.extend(semantic[unit_id][1])
            final_scores[unit_id] = (score, fields)

    candidate_map = {
        unit.id: (source, version, unit)
        for source, version, unit in candidates
    }
    rank_candidates = [
        (unit_id, score, matched)
        for unit_id, (score, matched) in final_scores.items()
        if unit_id in candidate_map
    ]
    if order_by == "occurred_at_asc":
        rank_key = lambda item: (
            candidate_map[item[0]][2].occurred_at or datetime.min,
            candidate_map[item[0]][0].id,
            candidate_map[item[0]][2].ordinal,
        )
    elif order_by == "occurred_at_desc":
        def rank_key(item):
            occurred_at = (
                candidate_map[item[0]][2].occurred_at
                or datetime.min
            )
            return (
                -occurred_at.toordinal(),
                -occurred_at.hour,
                -occurred_at.minute,
                -occurred_at.second,
                -occurred_at.microsecond,
                candidate_map[item[0]][0].id,
                candidate_map[item[0]][2].ordinal,
            )
    elif order_by == "source_order":
        rank_key = lambda item: (
            candidate_map[item[0]][0].id,
            candidate_map[item[0]][1].version_number,
            candidate_map[item[0]][2].ordinal,
        )
    else:
        rank_key = lambda item: (
            -item[1],
            candidate_map[item[0]][2].occurred_at or datetime.min,
            candidate_map[item[0]][0].id,
            candidate_map[item[0]][2].ordinal,
            item[0],
        )
    ranked = sorted(rank_candidates, key=rank_key)[:limit]
    results = []
    for rank, (unit_id, score, fields) in enumerate(ranked, start=1):
        source, version, unit = candidate_map[unit_id]
        results.append(_result_row(
            source,
            version,
            unit,
            rank=rank,
            score=score,
            component_scores={
                "lexical": lexical.get(unit_id, (0.0, []))[0],
                "semantic": semantic.get(unit_id, (0.0, []))[0],
                "structured": 1.0 if structured_matches.get(unit_id) else 0.0,
            },
            matched_fields=[
                *fields,
                *structured_matches.get(unit_id, []),
            ],
            query=query,
            include_content=include_content,
            snippet_chars=snippet_chars,
        ))
    results, projected_fields = _project_unit_result_fields(
        results,
        result_field_projection,
    )
    return {
        "schema_version": KNOWLEDGE_SCHEMA_VERSION,
        "strategy_requested": strategy,
        "strategy_resolved": resolved,
        "strategy_reasons": reasons,
        "results": results,
        "result_count": len(results),
        "diagnostics": {
            "strategy_planned": planned_resolved,
            "candidate_unit_count": len(candidates),
            "lexical_candidate_count": len(lexical),
            "semantic_candidate_count": len(semantic),
            "semantic_backend_used": semantic_attempted,
            "fusion": (
                {
                    "algorithm": "weighted_reciprocal_rank_fusion",
                    "rrf_k": 60,
                    "candidate_limit_per_component": fusion_candidate_limit,
                    "weights": fusion_weights,
                }
                if fusion_weights is not None
                else None
            ),
            "lexical": {
                "algorithm": "bm25",
                "schema_version": 2,
                "k1": 1.5,
                "b": 0.75,
            },
            "collection_ids": _int_list(collection_ids or []),
            "source_ids": _string_list(source_ids),
            "filters_applied": sorted(filters.keys()),
            "order_by": order_by,
            "field_projection": projected_fields or None,
        },
    }


def _fingerprint_key(user_id: int) -> bytes:
    secret = (
        current_app.secret_key
        if has_app_context() and current_app.secret_key
        else "knowledge-fabric-local-key"
    )
    return hashlib.sha256(
        f"{secret}:knowledge-index:{user_id}".encode("utf-8")
    ).digest()


def lexical_fingerprints(text: str, *, user_id: int) -> list[str]:
    key = _fingerprint_key(user_id)
    return sorted({
        hmac.new(key, token.encode("utf-8"), hashlib.sha256).hexdigest()[:32]
        for token in _tokens(text)
    })


def create_index_generation(
    *,
    user_id: int,
    signature: dict[str, Any],
    collection_id: int | None = None,
    projection_type: str = "hybrid",
    commit: bool = False,
) -> KnowledgeIndexGeneration:
    if collection_id is not None:
        _collection_or_404(collection_id, user_id)
    signature = dict(signature or {})
    required = {
        "extractor",
        "extractor_version",
        "chunker",
        "chunker_version",
        "lexical_schema_version",
    }
    missing = sorted(required - set(signature))
    if missing:
        raise KnowledgeFabricError(
            "Index signature is missing: " + ", ".join(missing)
        )
    generation = KnowledgeIndexGeneration(
        user_id=user_id,
        collection_id=collection_id,
        projection_type=str(projection_type or "hybrid"),
        status="building",
        signature_hash=_json_hash(signature),
        signature_json=signature,
    )
    db.session.add(generation)
    db.session.flush()
    if commit:
        db.session.commit()
    return generation


def build_index_generation(
    generation_id: str,
    *,
    unit_ids: list[str] | None = None,
    mark_ready: bool = True,
    commit: bool = False,
) -> KnowledgeIndexGeneration:
    generation = db.session.get(KnowledgeIndexGeneration, generation_id)
    if generation is None:
        raise KnowledgeFabricError("Index generation is unavailable.")
    if generation.status != "building":
        raise KnowledgeFabricError("Only a building index generation can be populated.")
    query = KnowledgeSourceUnit.query.join(KnowledgeSourceVersion).join(KnowledgeSource)
    query = query.filter(KnowledgeSource.user_id == generation.user_id)
    selected_ids = _string_list(unit_ids)
    if selected_ids:
        query = query.filter(KnowledgeSourceUnit.id.in_(selected_ids))
    elif generation.collection_id is not None:
        selected_ids = [
            unit.id
            for _source, _version, unit in _candidate_units(
                user_id=generation.user_id,
                collection_ids=[generation.collection_id],
                source_ids=None,
                filters={},
            )
        ]
        if selected_ids:
            query = query.filter(
                KnowledgeSourceUnit.id.in_(selected_ids)
            )
        else:
            query = query.filter(KnowledgeSourceUnit.id == "__no_units__")
    units = query.order_by(
        KnowledgeSourceUnit.source_version_id,
        KnowledgeSourceUnit.ordinal,
    ).all()
    KnowledgeIndexEntry.query.filter_by(
        generation_id=generation.id,
    ).delete(synchronize_session=False)
    db.session.flush()
    for ordinal, unit in enumerate(units):
        db.session.add(KnowledgeIndexEntry(
            generation_id=generation.id,
            source_unit_id=unit.id,
            ordinal=ordinal,
            vector_key=f"{generation.id}:{unit.id}",
            lexical_fingerprints=lexical_fingerprints(
                unit.content,
                user_id=generation.user_id,
            ),
            projection_metadata_json={
                "content_hash": unit.content_hash,
                "token_estimate": unit.token_estimate,
            },
        ))
    generation.entry_count = len(units)
    generation.health_json = {
        "validated": True,
        "source_unit_count": len(units),
        "duplicate_unit_count": 0,
        "content_hashes_present": all(bool(unit.content_hash) for unit in units),
        "semantic_projection_ready": False,
    }
    if mark_ready:
        generation.status = "ready"
        generation.completed_at = utc_now_db()
    db.session.flush()
    if commit:
        db.session.commit()
    return generation


def mark_index_generation_ready(
    generation_id: str,
    *,
    health: dict[str, Any] | None = None,
    commit: bool = False,
) -> KnowledgeIndexGeneration:
    generation = db.session.get(KnowledgeIndexGeneration, generation_id)
    if generation is None:
        raise KnowledgeFabricError("Index generation is unavailable.")
    if generation.status != "building":
        raise KnowledgeFabricError("Only a building generation can become ready.")
    generation.health_json = {
        **dict(generation.health_json or {}),
        **dict(health or {}),
        "validated": True,
    }
    generation.status = "ready"
    generation.completed_at = utc_now_db()
    generation.error_json = None
    db.session.flush()
    if commit:
        db.session.commit()
    return generation


def mark_index_generation_failed(
    generation_id: str,
    error: BaseException | str,
    *,
    details: dict[str, Any] | None = None,
    commit: bool = False,
) -> KnowledgeIndexGeneration:
    generation = db.session.get(KnowledgeIndexGeneration, generation_id)
    if generation is None:
        raise KnowledgeFabricError("Index generation is unavailable.")
    if generation.status == "active":
        raise KnowledgeFabricError("An active generation cannot be marked failed.")
    generation.status = "failed"
    generation.error_json = {
        "error_type": (
            type(error).__name__
            if isinstance(error, BaseException)
            else "error"
        ),
        "message": str(error),
        "details": dict(details or {}),
    }
    generation.completed_at = utc_now_db()
    db.session.flush()
    if commit:
        db.session.commit()
    return generation


def active_index_generation(
    *,
    user_id: int,
    collection_id: int | None,
    projection_type: str,
) -> KnowledgeIndexGeneration | None:
    query = KnowledgeIndexGeneration.query.filter_by(
        user_id=user_id,
        collection_id=collection_id,
        projection_type=projection_type,
        status="active",
    )
    return query.order_by(KnowledgeIndexGeneration.activated_at.desc()).first()


def activate_index_generation(
    generation_id: str,
    *,
    expected_active_generation_id: str | None = None,
    commit: bool = False,
) -> KnowledgeIndexGeneration:
    generation = db.session.get(KnowledgeIndexGeneration, generation_id)
    if generation is None:
        raise KnowledgeFabricError("Index generation is unavailable.")
    if generation.status not in {"ready", "active"}:
        raise KnowledgeFabricError("Only a validated ready generation can be activated.")
    active = active_index_generation(
        user_id=generation.user_id,
        collection_id=generation.collection_id,
        projection_type=generation.projection_type,
    )
    active_id = active.id if active is not None else None
    if expected_active_generation_id is not None and active_id != expected_active_generation_id:
        raise IndexActivationConflict(
            f"Active generation is {active_id or '<none>'}, not {expected_active_generation_id}."
        )
    if active is not None and active.id == generation.id:
        return generation
    if active is not None:
        active.status = "ready"
    generation.previous_generation_id = active_id
    generation.status = "active"
    generation.activated_at = utc_now_db()
    generation.retired_at = None
    db.session.flush()
    if commit:
        db.session.commit()
    return generation


def rollback_index_generation(
    *,
    user_id: int,
    target_generation_id: str,
    commit: bool = False,
) -> KnowledgeIndexGeneration:
    target = db.session.get(KnowledgeIndexGeneration, target_generation_id)
    if target is None or target.user_id != user_id:
        raise KnowledgeOwnershipError("Rollback index generation is unavailable.")
    active = active_index_generation(
        user_id=user_id,
        collection_id=target.collection_id,
        projection_type=target.projection_type,
    )
    return activate_index_generation(
        target.id,
        expected_active_generation_id=active.id if active else None,
        commit=commit,
    )


def retire_index_generation(
    *,
    user_id: int,
    generation_id: str,
    commit: bool = False,
) -> KnowledgeIndexGeneration:
    generation = db.session.get(KnowledgeIndexGeneration, generation_id)
    if generation is None or generation.user_id != user_id:
        raise KnowledgeOwnershipError("Index generation is unavailable.")
    if generation.status == "active":
        raise KnowledgeFabricError("Activate another generation before retiring the active index.")
    generation.status = "retired"
    generation.retired_at = utc_now_db()
    db.session.flush()
    if commit:
        db.session.commit()
    return generation


def create_coverage_run(
    *,
    user_id: int,
    source_id: str,
    source_version_id: str | None = None,
    session_id: str | None = None,
    turn_id: str | None = None,
    model_id: str | None = None,
    units_per_slice: int = 20,
    overlap_units: int = 1,
    commit: bool = False,
) -> KnowledgeCoverageRun:
    source = _source_or_404(source_id, user_id)
    version = _resolve_version(source, version_id=source_version_id)
    total = len(version.units)
    units_per_slice = max(1, min(int(units_per_slice or 20), 500))
    overlap_units = max(0, min(int(overlap_units or 0), units_per_slice - 1))
    coverage_run = KnowledgeCoverageRun(
        user_id=user_id,
        source_id=source.id,
        source_version_id=version.id,
        session_id=session_id,
        turn_id=turn_id,
        model_id=model_id,
        status="pending",
        total_units=total,
        manifest_json={
            "schema_version": KNOWLEDGE_SCHEMA_VERSION,
            "source_content_hash": version.content_hash,
            "unit_ids": [unit.id for unit in version.units],
            "unit_hashes": [unit.content_hash for unit in version.units],
            "units_per_slice": units_per_slice,
            "overlap_units": overlap_units,
        },
    )
    db.session.add(coverage_run)
    db.session.flush()
    for slice_index, start in enumerate(range(0, total, units_per_slice)):
        end = min(total - 1, start + units_per_slice - 1)
        db.session.add(KnowledgeCoverageSlice(
            coverage_run_id=coverage_run.id,
            slice_index=slice_index,
            start_ordinal=start,
            end_ordinal=end,
            overlap_before=min(overlap_units, start),
            overlap_after=min(overlap_units, max(0, total - 1 - end)),
            status="pending",
        ))
    db.session.flush()
    if commit:
        db.session.commit()
    return coverage_run


def coverage_slice_materialization(
    *,
    user_id: int,
    coverage_slice_id: str,
) -> dict[str, Any]:
    coverage_slice = db.session.get(KnowledgeCoverageSlice, coverage_slice_id)
    if (
        coverage_slice is None
        or coverage_slice.coverage_run.user_id != user_id
    ):
        raise KnowledgeOwnershipError("Coverage slice is unavailable.")
    run = coverage_slice.coverage_run
    start = max(0, coverage_slice.start_ordinal - coverage_slice.overlap_before)
    end = coverage_slice.end_ordinal + coverage_slice.overlap_after
    materialized = materialize_source(
        user_id=user_id,
        source_id=run.source_id,
        version_id=run.source_version_id,
        start_ordinal=start,
        end_ordinal=end,
        include_content=True,
    )
    materialized["coverage_slice"] = {
        "id": coverage_slice.id,
        "slice_index": coverage_slice.slice_index,
        "primary_start_ordinal": coverage_slice.start_ordinal,
        "primary_end_ordinal": coverage_slice.end_ordinal,
        "materialized_start_ordinal": start,
        "materialized_end_ordinal": end,
    }
    return materialized


def record_coverage_slice(
    *,
    user_id: int,
    coverage_slice_id: str,
    output: dict[str, Any] | None = None,
    evidence_refs: list[str] | None = None,
    error: dict[str, Any] | None = None,
    commit: bool = False,
) -> KnowledgeCoverageRun:
    coverage_slice = db.session.get(KnowledgeCoverageSlice, coverage_slice_id)
    if (
        coverage_slice is None
        or coverage_slice.coverage_run.user_id != user_id
    ):
        raise KnowledgeOwnershipError("Coverage slice is unavailable.")
    if coverage_slice.status == "completed" and error:
        raise KnowledgeFabricError("A completed coverage slice cannot be replaced by an error.")
    coverage_slice.output_json = dict(output or {}) if not error else None
    coverage_slice.evidence_refs = _string_list(evidence_refs)
    coverage_slice.error_json = dict(error) if error else None
    coverage_slice.status = "failed" if error else "completed"
    coverage_slice.completed_at = utc_now_db()
    run = coverage_slice.coverage_run
    completed = sum(1 for item in run.slices if item.status == "completed")
    failed = sum(1 for item in run.slices if item.status == "failed")
    covered = sum(
        item.end_ordinal - item.start_ordinal + 1
        for item in run.slices
        if item.status == "completed"
    )
    run.covered_units = min(run.total_units, covered)
    run.failed_units = sum(
        item.end_ordinal - item.start_ordinal + 1
        for item in run.slices
        if item.status == "failed"
    )
    if completed + failed == len(run.slices):
        run.status = "completed" if failed == 0 else ("partial" if completed else "failed")
        run.completed_at = utc_now_db()
    else:
        run.status = "running"
    db.session.flush()
    if commit:
        db.session.commit()
    return run


def validate_coverage_run(
    *,
    user_id: int,
    coverage_run_id: str,
) -> dict[str, Any]:
    run = db.session.get(KnowledgeCoverageRun, coverage_run_id)
    if run is None or run.user_id != user_id:
        raise KnowledgeOwnershipError("Coverage run is unavailable.")
    covered_ordinals: set[int] = set()
    duplicate_ordinals: set[int] = set()
    failed_ranges = []
    for coverage_slice in run.slices:
        primary = set(range(
            coverage_slice.start_ordinal,
            coverage_slice.end_ordinal + 1,
        ))
        if coverage_slice.status == "completed":
            duplicate_ordinals.update(covered_ordinals & primary)
            covered_ordinals.update(primary)
        elif coverage_slice.status == "failed":
            failed_ranges.append({
                "slice_id": coverage_slice.id,
                "start_ordinal": coverage_slice.start_ordinal,
                "end_ordinal": coverage_slice.end_ordinal,
                "error": coverage_slice.error_json,
            })
    expected = set(range(run.total_units))
    missing = sorted(expected - covered_ordinals)
    return {
        "coverage_run_id": run.id,
        "source_id": run.source_id,
        "source_version_id": run.source_version_id,
        "status": run.status,
        "complete": not missing and not failed_ranges,
        "total_units": run.total_units,
        "covered_units": len(covered_ordinals),
        "missing_ordinals": missing,
        "duplicate_primary_ordinals": sorted(duplicate_ordinals),
        "failed_ranges": failed_ranges,
        "source_content_hash": (run.manifest_json or {}).get("source_content_hash"),
    }


def execute_coverage_run(
    *,
    user_id: int,
    coverage_run_id: str,
    process_slice: Callable[[dict[str, Any]], dict[str, Any]],
    synthesize: Callable[[list[dict[str, Any]], dict[str, Any]], dict[str, Any]] | None = None,
    commit_each_slice: bool = True,
) -> dict[str, Any]:
    """Execute a coverage manifest with injectable worker/synthesis functions."""
    run = db.session.get(KnowledgeCoverageRun, coverage_run_id)
    if run is None or run.user_id != user_id:
        raise KnowledgeOwnershipError("Coverage run is unavailable.")
    outputs = []
    for coverage_slice in run.slices:
        if coverage_slice.status == "completed":
            outputs.append(dict(coverage_slice.output_json or {}))
            continue
        materialized = coverage_slice_materialization(
            user_id=user_id,
            coverage_slice_id=coverage_slice.id,
        )
        try:
            output = process_slice(materialized)
            if not isinstance(output, dict):
                output = {"result": output}
            outputs.append(output)
            record_coverage_slice(
                user_id=user_id,
                coverage_slice_id=coverage_slice.id,
                output=output,
                commit=commit_each_slice,
            )
        except Exception as exc:
            record_coverage_slice(
                user_id=user_id,
                coverage_slice_id=coverage_slice.id,
                error={
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                },
                commit=commit_each_slice,
            )
    validation = validate_coverage_run(
        user_id=user_id,
        coverage_run_id=run.id,
    )
    if callable(synthesize):
        run.synthesis_json = synthesize(outputs, validation)
        db.session.flush()
        if commit_each_slice:
            db.session.commit()
    return {
        "coverage": validation,
        "outputs": outputs,
        "synthesis": run.synthesis_json,
    }


def import_legacy_data_context(
    *,
    user_id: int,
    data_context_id: int,
    commit: bool = False,
) -> tuple[KnowledgeSource, KnowledgeSourceVersion, bool]:
    context = db.session.get(DataContext, int(data_context_id))
    if context is None or context.user_id != user_id:
        raise KnowledgeOwnershipError("Legacy knowledge item is unavailable.")
    metadata = dict(context.json_metadata or {})
    external_id = str(
        context.source_id
        if context.source_id not in (None, "")
        else f"legacy-data-context:{context.id}"
    )
    source, version, created = upsert_source(
        user_id=user_id,
        source_type=context.source_type or "legacy",
        external_id=external_id,
        title=(
            metadata.get("title")
            or metadata.get("file_name")
            or metadata.get("subject")
        ),
        mime_type=metadata.get("mime_type"),
        content=context.content or "",
        metadata=metadata,
        source_created_at=context.created_at,
        source_modified_at=context.updated_at,
        extractor_name="legacy_data_context",
        extractor_version="1",
        provenance={"data_context_id": context.id},
        legacy_data_context_id=context.id,
        commit=False,
    )
    for collection in context.collections:
        if collection.user_id == user_id:
            add_collection_membership(
                user_id=user_id,
                collection_id=collection.id,
                source_id=source.id,
                commit=False,
            )
    db.session.flush()
    if commit:
        db.session.commit()
    return source, version, created


def legacy_migration_inventory(*, user_id: int) -> dict[str, Any]:
    contexts = DataContext.query.filter_by(user_id=user_id).all()
    imported_ids = {
        source.legacy_data_context_id
        for source in KnowledgeSource.query.filter_by(user_id=user_id).all()
        if source.legacy_data_context_id is not None
    }
    pending = [context for context in contexts if context.id not in imported_ids]
    return {
        "user_id": user_id,
        "legacy_item_count": len(contexts),
        "already_imported_count": len(contexts) - len(pending),
        "pending_count": len(pending),
        "pending_ids": [context.id for context in pending],
        "destructive_cleanup_performed": False,
    }
