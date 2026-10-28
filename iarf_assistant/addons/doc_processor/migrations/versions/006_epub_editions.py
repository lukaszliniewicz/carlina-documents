"""Persist owner-scoped Document Processor EPUB edition recipes.

Revision ID: 006_epub_editions
Revises: 005_extraction_issue_ledger
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "006_epub_editions"
down_revision = "005_extraction_issue_ledger"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "document_epub_edition",
        sa.Column("document_id", sa.String(length=36), nullable=False),
        sa.Column("owner_user_id", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("source_revision", sa.String(length=64), nullable=True),
        # ContentType uses TEXT for plaintext JSON and encrypted JSON alike.
        sa.Column("recipe", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("version >= 1", name="ck_document_epub_edition_version"),
        sa.CheckConstraint(
            "source_revision IS NULL OR length(source_revision) = 64",
            name="ck_document_epub_edition_source_revision",
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["processed_document.id"],
            ondelete="CASCADE",
            name="fk_document_epub_edition_document",
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["user.id"],
            ondelete="CASCADE",
            name="fk_document_epub_edition_owner",
        ),
        sa.PrimaryKeyConstraint("document_id", name="pk_document_epub_edition"),
    )
    op.create_index(
        "ix_document_epub_edition_owner_user_id",
        "document_epub_edition",
        ["owner_user_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_document_epub_edition_owner_user_id",
        table_name="document_epub_edition",
    )
    op.drop_table("document_epub_edition")
