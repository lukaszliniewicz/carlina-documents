from __future__ import annotations

from typing import Any

from sqlalchemy.orm.attributes import flag_modified

from iarf_assistant.core.document_knowledge import (
    create_pending_document_source,
    document_source_modified_at,
    fail_document_source,
    finalize_document_source,
)
from iarf_assistant.core.knowledge_fabric import KnowledgeOwnershipError
from iarf_assistant.core.upload_paths import resolve_upload_reference
from carlina.core.utc import utc_iso
from iarf_assistant.extensions import db
from iarf_assistant.models import KnowledgeSource

from .format_utils import html_to_markdown_on_the_fly
from .metadata_service import get_canonical_document_metadata
from .models import ProcessedDocument

DOC_PROCESSOR_EXTRACTOR = "iarf-doc-processor"
DOC_PROCESSOR_EXTRACTOR_VERSION = "1"


def _source_path(document: ProcessedDocument) -> str | None:
    try:
        path = resolve_upload_reference(document.file_path)
        return str(path) if path is not None else None
    except Exception:
        return None


def _extractor_config(document: ProcessedDocument) -> dict[str, Any]:
    settings = dict(document.settings or {})
    return {
        key: settings[key]
        for key in (
            "engine_id",
            "resolved_engine_id",
            "parsing_model",
            "metadata_model",
            "pdf_options",
            "engine_options",
            "metrics",
        )
        if key in settings
    }


def ensure_processed_document_source(
    document: ProcessedDocument,
    *,
    commit: bool = False,
) -> KnowledgeSource:
    settings = dict(document.settings or {})
    source_id = str(settings.get("knowledge_source_id") or "").strip()
    if source_id:
        source = db.session.get(KnowledgeSource, source_id)
        if source is not None and source.user_id == document.user_id:
            return source

    metadata = get_canonical_document_metadata(document)
    source = create_pending_document_source(
        user_id=document.user_id,
        external_id=f"doc_processor:{document.id}",
        filename=document.filename,
        file_path=document.file_path,
        extractor_name=DOC_PROCESSOR_EXTRACTOR,
        extractor_config=_extractor_config(document),
        provenance={
            "database_table": "processed_document",
            "database_id": document.id,
        },
    )
    source.title = str(metadata.get("title") or document.filename)
    source_metadata = dict(source.metadata_json or {})
    source_metadata.update({
        "processed_document_id": document.id,
        "document_metadata": metadata,
    })
    source.metadata_json = source_metadata
    settings["knowledge_source_id"] = source.id
    settings["knowledge_sync_status"] = "processing"
    document.settings = settings
    flag_modified(document, "settings")
    db.session.flush()
    if commit:
        db.session.commit()
    return source


def sync_processed_document_source(
    document: ProcessedDocument,
    *,
    commit: bool = False,
):
    content = str(
        document.output_markdown
        or (
            html_to_markdown_on_the_fly(document.output_html)
            if document.output_html
            else ""
        )
        or ""
    )
    if not content.strip():
        raise ValueError("Processed document has no canonical text output.")
    source = ensure_processed_document_source(document)
    metadata = get_canonical_document_metadata(document)
    source.title = str(metadata.get("title") or document.filename)
    source_path = _source_path(document)
    source, version, created = finalize_document_source(
        user_id=document.user_id,
        source_id=source.id,
        content=content,
        extractor_name=DOC_PROCESSOR_EXTRACTOR,
        extractor_version=DOC_PROCESSOR_EXTRACTOR_VERSION,
        extractor_config=_extractor_config(document),
        metadata={
            "filename": document.filename,
            "file_name": document.filename,
            "file_path": document.file_path,
            "processed_document_id": document.id,
            "processed_document_status": document.status,
            "document_metadata": metadata,
        },
        provenance={
            "database_table": "processed_document",
            "database_id": document.id,
            "source_path": document.file_path,
        },
        source_modified_at=document_source_modified_at(source_path),
    )
    settings = dict(document.settings or {})
    settings.update({
        "knowledge_source_id": source.id,
        "knowledge_source_version_id": version.id,
        "knowledge_source_version_number": version.version_number,
        "knowledge_source_content_hash": version.content_hash,
        "knowledge_sync_status": "ready",
        "knowledge_synced_at": utc_iso(),
    })
    settings.pop("knowledge_sync_error", None)
    document.settings = settings
    flag_modified(document, "settings")
    db.session.flush()
    if commit:
        db.session.commit()
    return source, version, created


def mark_processed_document_source_failed(
    document: ProcessedDocument,
    error: Any,
    *,
    commit: bool = False,
) -> None:
    settings = dict(document.settings or {})
    settings["knowledge_sync_status"] = "failed"
    settings["knowledge_sync_error"] = str(error)
    document.settings = settings
    flag_modified(document, "settings")
    source_id = str(settings.get("knowledge_source_id") or "").strip()
    if source_id:
        try:
            fail_document_source(
                user_id=document.user_id,
                source_id=source_id,
                error=error,
            )
        except KnowledgeOwnershipError:
            pass
    db.session.flush()
    if commit:
        db.session.commit()


def delete_processed_document_source(
    document: ProcessedDocument,
) -> None:
    source_id = str(
        (document.settings or {}).get("knowledge_source_id") or ""
    ).strip()
    if not source_id:
        return
    source = db.session.get(KnowledgeSource, source_id)
    if source is not None and source.user_id == document.user_id:
        db.session.delete(source)
