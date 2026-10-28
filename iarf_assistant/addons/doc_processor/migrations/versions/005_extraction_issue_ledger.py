"""Add the encrypted extraction issue ledger.

Revision ID: 005_extraction_issue_ledger
Revises: 004_doc_processor_safety_indexes
Create Date: 2026-09-27 00:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from iarf_assistant.database.types import EncryptedType

revision = "005_extraction_issue_ledger"
down_revision = "004_doc_processor_safety_indexes"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "doc_processor_issue",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("document_id", sa.String(length=36), nullable=False),
        sa.Column("owner_user_id", sa.Integer(), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=True),
        sa.Column("source_key", sa.String(length=255), nullable=False),
        sa.Column("source_revision", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("payload", EncryptedType(sa.JSON(), strategy="system"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "page_number IS NULL OR page_number >= 1",
            name="ck_doc_processor_issue_page_number",
        ),
        sa.CheckConstraint(
            "status IN ('open', 'confirmed', 'resolved', 'dismissed')",
            name="ck_doc_processor_issue_status",
        ),
        sa.CheckConstraint("version >= 1", name="ck_doc_processor_issue_version"),
        sa.ForeignKeyConstraint(
            ["document_id"], ["processed_document.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["owner_user_id"], ["user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "document_id", "source_key", name="uq_doc_processor_issue_source"
        ),
    )
    op.create_index(
        "ix_doc_processor_issue_owner_document_page",
        "doc_processor_issue",
        ["owner_user_id", "document_id", "page_number"],
    )
    op.create_index(
        "ix_doc_processor_issue_owner_document_status",
        "doc_processor_issue",
        ["owner_user_id", "document_id", "status"],
    )


def downgrade():
    op.drop_index(
        "ix_doc_processor_issue_owner_document_status",
        table_name="doc_processor_issue",
    )
    op.drop_index(
        "ix_doc_processor_issue_owner_document_page",
        table_name="doc_processor_issue",
    )
    op.drop_table("doc_processor_issue")
