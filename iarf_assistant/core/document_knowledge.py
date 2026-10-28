from __future__ import annotations

import mimetypes
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from iarf_assistant.core.knowledge_fabric import (
    KnowledgeOwnershipError,
    add_collection_membership,
    document_units_from_text,
    ensure_source_identity,
    upsert_source,
)
from carlina.core.utc import utc_now_db
from iarf_assistant.extensions import db
from iarf_assistant.models import (
    KnowledgeIndexEntry,
    KnowledgeSource,
    KnowledgeSourceUnit,
)


DOCUMENT_SOURCE_TYPE = "document"
DOCUMENT_EXTRACTOR_VERSION = "1"


def create_pending_document_source(
    *,
    user_id: int,
    external_id: str,
    filename: str,
    file_path: str,
    collection_id: int | None = None,
    mime_type: str | None = None,
    extractor_name: str | None = None,
    extractor_config: dict[str, Any] | None = None,
    provenance: dict[str, Any] | None = None,
    commit: bool = False,
) -> KnowledgeSource:
    detected_mime = (
        mime_type
        or mimetypes.guess_type(str(filename or file_path))[0]
        or "application/octet-stream"
    )
    source, _created = ensure_source_identity(
        user_id=user_id,
        source_type=DOCUMENT_SOURCE_TYPE,
        external_id=external_id,
        title=filename,
        mime_type=detected_mime,
        metadata={
            "filename": filename,
            "file_name": filename,
            "file_path": file_path,
            "ingestion_status": "processing",
            "extractor_name": extractor_name,
            "extractor_config": dict(extractor_config or {}),
            "provenance": dict(provenance or {}),
        },
        access_policy={"owner_user_id": user_id},
        source_created_at=utc_now_db(),
    )
    if collection_id is not None:
        add_collection_membership(
            user_id=user_id,
            collection_id=collection_id,
            source_id=source.id,
            metadata={"ingestion": "document_upload"},
        )
    db.session.flush()
    if commit:
        db.session.commit()
    return source


def finalize_document_source(
    *,
    user_id: int,
    source_id: str,
    content: str,
    extractor_name: str,
    extractor_version: str = DOCUMENT_EXTRACTOR_VERSION,
    extractor_config: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    provenance: dict[str, Any] | None = None,
    source_modified_at: Any = None,
    commit: bool = False,
):
    source = db.session.get(KnowledgeSource, source_id)
    if source is None or source.user_id != user_id:
        raise KnowledgeOwnershipError("Document source is unavailable.")
    merged_metadata = dict(source.metadata_json or {})
    merged_metadata.update(dict(metadata or {}))
    merged_metadata["ingestion_status"] = "ready"
    merged_metadata.pop("ingestion_error", None)
    merged_provenance = dict(merged_metadata.get("provenance") or {})
    merged_provenance.update(dict(provenance or {}))
    merged_metadata["provenance"] = merged_provenance
    source, version, created = upsert_source(
        user_id=user_id,
        source_type=source.source_type,
        external_id=source.external_id,
        title=source.title,
        mime_type=source.mime_type,
        content=str(content or ""),
        units=document_units_from_text(str(content or "")),
        metadata=merged_metadata,
        access_policy=source.access_policy_json or {},
        source_created_at=source.source_created_at,
        source_modified_at=source_modified_at or utc_now_db(),
        extractor_name=extractor_name,
        extractor_version=extractor_version,
        extractor_config=dict(extractor_config or {}),
        provenance=merged_provenance,
        commit=commit,
    )
    return source, version, created


def fail_document_source(
    *,
    user_id: int,
    source_id: str,
    error: Any,
    commit: bool = False,
) -> KnowledgeSource:
    source = db.session.get(KnowledgeSource, source_id)
    if source is None or source.user_id != user_id:
        raise KnowledgeOwnershipError("Document source is unavailable.")
    metadata = dict(source.metadata_json or {})
    metadata["ingestion_status"] = "failed"
    metadata["ingestion_error"] = {
        "type": type(error).__name__,
        "message": str(error),
    }
    source.metadata_json = metadata
    db.session.flush()
    if commit:
        db.session.commit()
    return source


def canonical_source_item(source: KnowledgeSource) -> dict[str, Any]:
    version = source.current_version
    metadata = dict(source.metadata_json or {})
    has_embedding = False
    if version is not None:
        unit_ids = [unit.id for unit in version.units]
        has_embedding = bool(
            unit_ids
            and KnowledgeIndexEntry.query.filter(
                KnowledgeIndexEntry.source_unit_id.in_(unit_ids)
            ).first()
        )
    content = version.content if version is not None else ""
    return {
        "id": source.id,
        "item_kind": "canonical",
        "canonical_source_id": source.id,
        "user_id": source.user_id,
        "source_type": source.source_type,
        "source_id": metadata.get("file_path") or source.external_id,
        "external_id": source.external_id,
        "content_snippet": (
            content[:100] + "..." if len(content) > 100 else content
        ) or None,
        "metadata": metadata,
        "has_embedding": has_embedding,
        "status": metadata.get("ingestion_status", "ready"),
        "current_version_number": source.current_version_number,
        "created_at": (
            source.created_at.isoformat() if source.created_at else None
        ),
        "updated_at": (
            source.updated_at.isoformat() if source.updated_at else None
        ),
    }


def canonical_source_xray(
    *,
    user_id: int,
    source_id: str,
    page: int,
    per_page: int,
) -> dict[str, Any]:
    source = db.session.get(KnowledgeSource, source_id)
    if source is None or source.user_id != user_id:
        raise KnowledgeOwnershipError("Knowledge source is unavailable.")
    version = source.current_version
    page = max(1, int(page or 1))
    per_page = max(1, int(per_page or 30))
    if version is None:
        units = []
        total = 0
    else:
        pagination = (
            KnowledgeSourceUnit.query
            .filter_by(source_version_id=version.id)
            .order_by(KnowledgeSourceUnit.ordinal)
            .paginate(page=page, per_page=per_page, error_out=False)
        )
        units = pagination.items
        total = pagination.total
    pages = (total + per_page - 1) // per_page if total else 0
    return {
        "id": source.id,
        "item_kind": "canonical",
        "canonical_source_id": source.id,
        "source_type": source.source_type,
        "source_id": (source.metadata_json or {}).get("file_path")
        or source.external_id,
        "external_id": source.external_id,
        "metadata": source.metadata_json or {},
        "raw_content": version.content if version is not None else "",
        "source_version_id": version.id if version is not None else None,
        "content_hash": version.content_hash if version is not None else None,
        "chunks": [
            {
                "id": unit.id,
                "text": unit.content,
                "meta": {
                    **(unit.metadata_json or {}),
                    "ordinal": unit.ordinal,
                    "unit_type": unit.unit_type,
                    "external_id": unit.external_id,
                    "anchor": unit.anchor,
                    "content_hash": unit.content_hash,
                },
            }
            for unit in units
        ],
        "chunk_total": total,
        "chunk_pages": pages,
        "chunk_page": page,
        "has_next": page < pages,
        "has_prev": page > 1 and pages > 0,
    }


def document_source_modified_at(file_path: str | None) -> datetime | None:
    if not file_path:
        return None
    try:
        return datetime.fromtimestamp(
            Path(file_path).stat().st_mtime,
            UTC,
        ).replace(tzinfo=None)
    except (OSError, ValueError):
        return None
