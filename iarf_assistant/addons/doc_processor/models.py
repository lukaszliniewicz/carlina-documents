from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import datetime

from carlina.core.utc import utc_now_db
from iarf_assistant.core.upload_paths import resolve_upload_reference
from iarf_assistant.database.types import ContentType
from iarf_assistant.extensions import db

from .cover_candidate import normalize_cover_candidate
from .note_arrangement import normalize_note_arrangement


def _uuid() -> str:
    return str(uuid.uuid4())


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _source_available(file_path: str | None) -> bool:
    if not file_path:
        return False
    try:
        resolved = resolve_upload_reference(file_path)
        return bool(resolved and resolved.is_file())
    except Exception:
        return False


class ProcessedDocument(db.Model):
    __tablename__ = "processed_document"

    id = db.Column(db.String(36), primary_key=True, default=_uuid)
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    filename = db.Column(db.String(255), nullable=False)
    file_path = db.Column(ContentType(db.String(512)), nullable=False)
    status = db.Column(db.String(50), default="created", nullable=False)
    settings = db.Column(db.JSON, nullable=True)
    output_html = db.Column(ContentType(db.Text), nullable=True)
    output_markdown = db.Column(ContentType(db.Text), nullable=True)
    error_message = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now_db)
    updated_at = db.Column(db.DateTime, default=utc_now_db, onupdate=utc_now_db)

    user = db.relationship(
        "User",
        backref=db.backref("processed_documents", lazy=True, cascade="all, delete-orphan"),
    )
    metadata_record = db.relationship(
        "DocumentMetadata",
        back_populates="document",
        uselist=False,
        cascade="all, delete-orphan",
    )
    project_entries = db.relationship(
        "ProjectDocument",
        back_populates="document",
        cascade="all, delete-orphan",
    )
    run_entries = db.relationship(
        "ProjectRunDocument",
        back_populates="document",
        cascade="all, delete-orphan",
    )
    epub_edition = db.relationship(
        "DocumentEpubEdition",
        back_populates="document",
        uselist=False,
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    def to_dict(self) -> dict:
        metadata = self.metadata_record.to_dict() if self.metadata_record is not None else {}
        settings_metadata = (self.settings or {}).get("document_metadata")
        if isinstance(settings_metadata, Mapping):
            if self.metadata_record is None:
                metadata = dict(settings_metadata)
            arrangement = settings_metadata.get("note_arrangement")
            if isinstance(arrangement, Mapping):
                metadata["note_arrangement"] = normalize_note_arrangement(arrangement)
            if "cover_candidate" in settings_metadata:
                metadata.pop("cover_candidate", None)
                cover_candidate = normalize_cover_candidate(
                    settings_metadata.get("cover_candidate")
                )
                if cover_candidate:
                    metadata["cover_candidate"] = cover_candidate

        return {
            "id": self.id,
            "user_id": self.user_id,
            "filename": self.filename,
            "file_path": self.file_path,
            "status": self.status,
            # Undo and image-source provenance stay in private persistence.
            "settings": {
                key: value for key, value in (self.settings or {}).items()
                if key not in {
                    "mcp_review_history",
                    "extraction_issues_checkpoint",
                    "mcp_assembly_recovery_audit",
                    "image_crops",
                }
            },
            "error_message": self.error_message,
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
            "metadata": metadata,
            "document_metadata": metadata,
            "source_available": _source_available(self.file_path),
        }


class DocumentMetadata(db.Model):
    __tablename__ = "document_metadata"

    id = db.Column(db.Integer, primary_key=True)
    document_id = db.Column(db.String(36), db.ForeignKey("processed_document.id", ondelete="CASCADE"), nullable=False, unique=True)
    title = db.Column(db.String(512), nullable=True)
    subtitle = db.Column(db.String(512), nullable=True)
    author = db.Column(db.String(512), nullable=True)
    publisher = db.Column(db.String(255), nullable=True)
    publication_date = db.Column(db.String(50), nullable=True)
    isbn = db.Column(db.String(50), nullable=True)
    issn = db.Column(db.String(50), nullable=True)
    language = db.Column(db.String(50), nullable=True)
    description = db.Column(db.Text, nullable=True)
    cover_image_id = db.Column(db.String(36), nullable=True)
    contributors = db.Column(db.JSON, nullable=True)
    table_of_contents = db.Column(db.JSON, nullable=True)
    pages_to_remove = db.Column(db.JSON, nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now_db)
    updated_at = db.Column(db.DateTime, default=utc_now_db, onupdate=utc_now_db)
    publication_type = db.Column(db.String(50), nullable=True)
    rights = db.Column(db.Text, nullable=True)
    source = db.Column(db.String(512), nullable=True)
    journal_or_series = db.Column(db.String(255), nullable=True)
    volume = db.Column(db.String(50), nullable=True)
    issue = db.Column(db.String(50), nullable=True)
    subjects = db.Column(db.JSON, nullable=True)
    identifiers = db.Column(db.JSON, nullable=True)
    source_pages = db.Column(db.JSON, nullable=True)
    extraction_notes = db.Column(db.JSON, nullable=True)

    document = db.relationship("ProcessedDocument", back_populates="metadata_record")

    __table_args__ = (db.Index("ix_document_metadata_document_id", "document_id"),)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "document_id": self.document_id,
            "title": self.title,
            "subtitle": self.subtitle,
            "author": self.author,
            "publication_type": self.publication_type,
            "publisher": self.publisher,
            "publication_date": self.publication_date,
            "isbn": self.isbn,
            "issn": self.issn,
            "language": self.language,
            "rights": self.rights,
            "source": self.source,
            "description": self.description,
            "journal_or_series": self.journal_or_series,
            "volume": self.volume,
            "issue": self.issue,
            "cover_image_id": self.cover_image_id,
            "contributors": self.contributors,
            "subjects": self.subjects,
            "identifiers": self.identifiers,
            "table_of_contents": self.table_of_contents,
            "pages_to_remove": self.pages_to_remove,
            "source_pages": self.source_pages,
            "extraction_notes": self.extraction_notes,
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
        }


class DocumentProject(db.Model):
    __tablename__ = "document_project"

    id = db.Column(db.String(36), primary_key=True, default=_uuid)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True)
    name = db.Column(db.String(255), nullable=False)
    settings = db.Column(db.JSON, nullable=True)
    created_at = db.Column(db.DateTime, default=utc_now_db)
    updated_at = db.Column(db.DateTime, default=utc_now_db, onupdate=utc_now_db)

    user = db.relationship(
        "User",
        backref=db.backref("document_projects", lazy=True, cascade="all, delete-orphan"),
    )
    documents = db.relationship(
        "ProjectDocument",
        back_populates="project",
        cascade="all, delete-orphan",
        order_by="ProjectDocument.position",
    )
    runs = db.relationship(
        "ProjectRun",
        back_populates="project",
        cascade="all, delete-orphan",
        order_by="ProjectRun.created_at.desc()",
    )

    def to_dict(self) -> dict:
        latest_run = self.runs[0].to_dict(include_documents=True) if self.runs else None
        return {
            "id": self.id,
            "user_id": self.user_id,
            "name": self.name,
            "settings": self.settings or {},
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
            "document_count": len(self.documents),
            "documents": [entry.to_dict() for entry in self.documents],
            "latest_run": latest_run,
        }


class ProjectDocument(db.Model):
    __tablename__ = "project_document"

    id = db.Column(db.String(36), primary_key=True, default=_uuid)
    project_id = db.Column(db.String(36), db.ForeignKey("document_project.id", ondelete="CASCADE"), nullable=False)
    document_id = db.Column(db.String(36), db.ForeignKey("processed_document.id", ondelete="CASCADE"), nullable=False)
    position = db.Column(db.Integer, nullable=False, default=0)
    settings_overrides = db.Column(db.JSON, nullable=True)
    added_at = db.Column(db.DateTime, default=utc_now_db)

    project = db.relationship("DocumentProject", back_populates="documents")
    document = db.relationship("ProcessedDocument", back_populates="project_entries")

    __table_args__ = (
        db.UniqueConstraint("project_id", "document_id", name="uq_project_document"),
        db.Index("ix_project_document_position", "project_id", "position"),
        db.Index("ix_project_document_document_id", "document_id"),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "document_id": self.document_id,
            "position": self.position,
            "settings_overrides": self.settings_overrides or {},
            "added_at": _iso(self.added_at),
            "document": self.document.to_dict() if self.document is not None else None,
        }


class ProjectRun(db.Model):
    __tablename__ = "project_run"

    id = db.Column(db.String(36), primary_key=True, default=_uuid)
    project_id = db.Column(
        db.String(36),
        db.ForeignKey("document_project.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True)
    status = db.Column(db.String(50), default="queued", nullable=False)
    settings_snapshot = db.Column(db.JSON, nullable=True)
    concurrency_limit = db.Column(db.Integer, default=1, nullable=False)
    pause_requested = db.Column(db.Boolean, default=False, nullable=False)
    stop_requested = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=utc_now_db)
    updated_at = db.Column(db.DateTime, default=utc_now_db, onupdate=utc_now_db)
    started_at = db.Column(db.DateTime, nullable=True)
    completed_at = db.Column(db.DateTime, nullable=True)

    project = db.relationship("DocumentProject", back_populates="runs")
    user = db.relationship("User", backref=db.backref("document_project_runs", lazy=True, cascade="all, delete-orphan"))
    documents = db.relationship(
        "ProjectRunDocument",
        back_populates="run",
        cascade="all, delete-orphan",
        order_by="ProjectRunDocument.position",
    )

    def to_dict(self, include_documents: bool = True) -> dict:
        counts: dict[str, int] = {"total": len(self.documents)}
        for entry in self.documents:
            counts[entry.status] = counts.get(entry.status, 0) + 1
        completed_count = sum(
            counts.get(status, 0)
            for status in ("completed", "error", "cancelled")
        )
        payload = {
            "id": self.id,
            "project_id": self.project_id,
            "user_id": self.user_id,
            "status": self.status,
            "settings_snapshot": self.settings_snapshot or {},
            "concurrency_limit": self.concurrency_limit,
            "pause_requested": self.pause_requested,
            "stop_requested": self.stop_requested,
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
            "started_at": _iso(self.started_at),
            "completed_at": _iso(self.completed_at),
            "counts": counts,
            "progress": round((completed_count / counts["total"]) * 100) if counts["total"] else 0,
        }
        if include_documents:
            payload["documents"] = [entry.to_dict() for entry in self.documents]
        return payload


class ProjectRunDocument(db.Model):
    __tablename__ = "project_run_document"

    id = db.Column(db.String(36), primary_key=True, default=_uuid)
    run_id = db.Column(db.String(36), db.ForeignKey("project_run.id", ondelete="CASCADE"), nullable=False)
    document_id = db.Column(db.String(36), db.ForeignKey("processed_document.id", ondelete="CASCADE"), nullable=False)
    position = db.Column(db.Integer, nullable=False, default=0)
    status = db.Column(db.String(50), default="queued", nullable=False)
    effective_settings = db.Column(db.JSON, nullable=True)
    job_id = db.Column(db.String(36), nullable=True)
    error_message = db.Column(db.Text, nullable=True)
    started_at = db.Column(db.DateTime, nullable=True)
    completed_at = db.Column(db.DateTime, nullable=True)

    run = db.relationship("ProjectRun", back_populates="documents")
    document = db.relationship("ProcessedDocument", back_populates="run_entries")

    __table_args__ = (
        db.UniqueConstraint("run_id", "document_id", name="uq_project_run_document"),
        db.Index("ix_project_run_document_status", "run_id", "status", "position"),
        db.Index("ix_project_run_document_document_id", "document_id"),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "run_id": self.run_id,
            "document_id": self.document_id,
            "position": self.position,
            "status": self.status,
            "effective_settings": self.effective_settings or {},
            "job_id": self.job_id,
            "error_message": self.error_message,
            "started_at": _iso(self.started_at),
            "completed_at": _iso(self.completed_at),
            "document": self.document.to_dict() if self.document is not None else None,
        }


class DocProcessorIssue(db.Model):
    """Owner-scoped extraction feedback with encrypted report and history."""

    __tablename__ = "doc_processor_issue"

    id = db.Column(db.String(36), primary_key=True, default=_uuid)
    document_id = db.Column(
        db.String(36),
        db.ForeignKey("processed_document.id", ondelete="CASCADE"),
        nullable=False,
    )
    owner_user_id = db.Column(
        db.Integer,
        db.ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
    )
    page_number = db.Column(db.Integer, nullable=True)
    source_key = db.Column(db.String(255), nullable=False)
    source_revision = db.Column(db.String(64), nullable=False)
    status = db.Column(db.String(16), nullable=False, default="open")
    version = db.Column(db.Integer, nullable=False, default=1)
    payload = db.Column(
        ContentType(db.JSON, strategy="system"),
        nullable=False,
    )
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now_db)
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=utc_now_db,
        onupdate=utc_now_db,
    )

    __table_args__ = (
        db.UniqueConstraint(
            "document_id", "source_key", name="uq_doc_processor_issue_source"
        ),
        db.CheckConstraint(
            "page_number IS NULL OR page_number >= 1",
            name="ck_doc_processor_issue_page_number",
        ),
        db.CheckConstraint(
            "status IN ('open', 'confirmed', 'resolved', 'dismissed')",
            name="ck_doc_processor_issue_status",
        ),
        db.CheckConstraint("version >= 1", name="ck_doc_processor_issue_version"),
        db.Index(
            "ix_doc_processor_issue_owner_document_page",
            "owner_user_id",
            "document_id",
            "page_number",
        ),
        db.Index(
            "ix_doc_processor_issue_owner_document_status",
            "owner_user_id",
            "document_id",
            "status",
        ),
    )


class DocumentEpubEdition(db.Model):
    """Owner-scoped EPUB recipe bound to one processed-document revision."""

    __tablename__ = "document_epub_edition"
    __table_args__ = (
        db.CheckConstraint("version >= 1", name="ck_document_epub_edition_version"),
        db.CheckConstraint(
            "source_revision IS NULL OR length(source_revision) = 64",
            name="ck_document_epub_edition_source_revision",
        ),
    )

    document_id = db.Column(
        db.String(36),
        db.ForeignKey("processed_document.id", ondelete="CASCADE"),
        primary_key=True,
    )
    owner_user_id = db.Column(
        db.Integer,
        db.ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version = db.Column(db.Integer, nullable=False)
    source_revision = db.Column(db.String(64), nullable=True)
    recipe = db.Column(ContentType(db.JSON, strategy="system"), nullable=True)
    updated_at = db.Column(db.DateTime, nullable=False, default=utc_now_db)

    document = db.relationship("ProcessedDocument", back_populates="epub_edition")
