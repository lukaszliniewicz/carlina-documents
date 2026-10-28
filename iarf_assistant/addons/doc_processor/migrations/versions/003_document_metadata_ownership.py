"""Add extended document metadata fields

Revision ID: 003_doc_metadata_ownership
Revises: 002_doc_projects
Create Date: 2026-06-20 12:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "003_doc_metadata_ownership"
down_revision = "002_doc_projects"
branch_labels = None
depends_on = None


def _table_names() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _column_names(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade():
    existing = _table_names()
    if "document_metadata" not in existing:
        op.create_table(
            "document_metadata",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("document_id", sa.String(length=36), nullable=False),
            sa.Column("title", sa.String(length=512), nullable=True),
            sa.Column("subtitle", sa.String(length=512), nullable=True),
            sa.Column("author", sa.String(length=512), nullable=True),
            sa.Column("publisher", sa.String(length=255), nullable=True),
            sa.Column("publication_date", sa.String(length=50), nullable=True),
            sa.Column("isbn", sa.String(length=50), nullable=True),
            sa.Column("issn", sa.String(length=50), nullable=True),
            sa.Column("language", sa.String(length=50), nullable=True),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("cover_image_id", sa.String(length=36), nullable=True),
            sa.Column("contributors", sa.JSON(), nullable=True),
            sa.Column("table_of_contents", sa.JSON(), nullable=True),
            sa.Column("pages_to_remove", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.Column("publication_type", sa.String(length=50), nullable=True),
            sa.Column("rights", sa.Text(), nullable=True),
            sa.Column("source", sa.String(length=512), nullable=True),
            sa.Column("journal_or_series", sa.String(length=255), nullable=True),
            sa.Column("volume", sa.String(length=50), nullable=True),
            sa.Column("issue", sa.String(length=50), nullable=True),
            sa.Column("subjects", sa.JSON(), nullable=True),
            sa.Column("identifiers", sa.JSON(), nullable=True),
            sa.Column("source_pages", sa.JSON(), nullable=True),
            sa.Column("extraction_notes", sa.JSON(), nullable=True),
            sa.ForeignKeyConstraint(["document_id"], ["processed_document.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("document_id"),
        )
        op.create_index("ix_document_metadata_document_id", "document_metadata", ["document_id"])
        return

    columns = _column_names("document_metadata")
    additions = {
        "publication_type": sa.Column("publication_type", sa.String(length=50), nullable=True),
        "rights": sa.Column("rights", sa.Text(), nullable=True),
        "source": sa.Column("source", sa.String(length=512), nullable=True),
        "journal_or_series": sa.Column("journal_or_series", sa.String(length=255), nullable=True),
        "volume": sa.Column("volume", sa.String(length=50), nullable=True),
        "issue": sa.Column("issue", sa.String(length=50), nullable=True),
        "subjects": sa.Column("subjects", sa.JSON(), nullable=True),
        "identifiers": sa.Column("identifiers", sa.JSON(), nullable=True),
        "source_pages": sa.Column("source_pages", sa.JSON(), nullable=True),
        "extraction_notes": sa.Column("extraction_notes", sa.JSON(), nullable=True),
    }
    with op.batch_alter_table("document_metadata") as batch_op:
        for name, column in additions.items():
            if name not in columns:
                batch_op.add_column(column)


def downgrade():
    if "document_metadata" not in _table_names():
        return
    columns = _column_names("document_metadata")
    removable = [
        "extraction_notes",
        "source_pages",
        "identifiers",
        "subjects",
        "issue",
        "volume",
        "journal_or_series",
        "source",
        "rights",
        "publication_type",
    ]
    with op.batch_alter_table("document_metadata") as batch_op:
        for name in removable:
            if name in columns:
                batch_op.drop_column(name)
