"""Add versioned encrypted Translator reader notes.

Revision ID: 021_translation_notes
Revises: 020_source_plan_corrections
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "021_translation_notes"
down_revision = "020_source_plan_corrections"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "translation_note",
        # EncryptedType is physically TEXT; the ORM owns envelope handling.
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("segment_id", sa.String(length=36), nullable=False),
        sa.Column("order_index", sa.Integer(), nullable=False),
        sa.Column("anchor_text", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "author_type", sa.String(length=20), nullable=False, server_default="model"
        ),
        sa.Column(
            "state", sa.String(length=20), nullable=False, server_default="active"
        ),
        sa.Column("segment_version", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_translation_note"),
        sa.UniqueConstraint(
            "segment_id",
            "segment_version",
            "order_index",
            name="uq_translation_note_segment_version_order",
        ),
        sa.CheckConstraint("order_index >= 0", name="ck_translation_note_order_index"),
        sa.CheckConstraint(
            "segment_version >= 1", name="ck_translation_note_segment_version"
        ),
        sa.CheckConstraint("version >= 1", name="ck_translation_note_version"),
        sa.CheckConstraint(
            "author_type IN ('model', 'human')",
            name="ck_translation_note_author_type",
        ),
        sa.CheckConstraint(
            "state IN ('active', 'superseded', 'suppressed')",
            name="ck_translation_note_state",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["translation_project.id"],
            ondelete="CASCADE",
            name="fk_translation_note_project",
        ),
        sa.ForeignKeyConstraint(
            ["segment_id"],
            ["translation_segment.id"],
            ondelete="CASCADE",
            name="fk_translation_note_segment",
        ),
    )
    op.create_index(
        "ix_translation_note_segment_state_version_order",
        "translation_note",
        ["segment_id", "state", "segment_version", "order_index"],
        unique=False,
    )
    op.create_index(
        "ix_translation_note_project_segment",
        "translation_note",
        ["project_id", "segment_id"],
        unique=False,
    )


def downgrade() -> None:
    bind = op.get_bind()
    if (
        bind.execute(sa.text("SELECT 1 FROM translation_note LIMIT 1")).first()
        is not None
    ):
        raise RuntimeError(
            "Translator migration 021 cannot downgrade after translation notes exist."
        )
    op.drop_index("ix_translation_note_project_segment", table_name="translation_note")
    op.drop_index(
        "ix_translation_note_segment_state_version_order",
        table_name="translation_note",
    )
    op.drop_table("translation_note")
