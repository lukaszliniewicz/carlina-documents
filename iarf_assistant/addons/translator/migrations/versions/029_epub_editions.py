"""Persist owner-scoped Translator EPUB edition recipes.

Revision ID: 029_epub_editions
Revises: 028_review_coverage
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "029_epub_editions"
down_revision = "028_review_coverage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "translation_epub_edition",
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("owner_user_id", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("source_revision", sa.String(length=64), nullable=True),
        # ContentType uses TEXT for plaintext JSON and encrypted JSON alike.
        sa.Column("recipe", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("version >= 1", name="ck_translation_epub_edition_version"),
        sa.CheckConstraint(
            "source_revision IS NULL OR length(source_revision) = 64",
            name="ck_translation_epub_edition_source_revision",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["translation_project.id"],
            ondelete="CASCADE",
            name="fk_translation_epub_edition_project",
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["user.id"],
            ondelete="CASCADE",
            name="fk_translation_epub_edition_owner",
        ),
        sa.PrimaryKeyConstraint("project_id", name="pk_translation_epub_edition"),
    )
    op.create_index(
        "ix_translation_epub_edition_owner_user_id",
        "translation_epub_edition",
        ["owner_user_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_translation_epub_edition_owner_user_id",
        table_name="translation_epub_edition",
    )
    op.drop_table("translation_epub_edition")
