"""Add encrypted, owner-scoped translator numbering overlays.

Revision ID: 026_numbering_overlay
Revises: 025_target_edit_idempotency
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "026_numbering_overlay"
down_revision = "025_target_edit_idempotency"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "translation_numbering_overlay",
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("owner_user_id", sa.Integer(), nullable=False),
        sa.Column("source_plan_id", sa.String(length=36), nullable=False),
        sa.Column("source_plan_hash", sa.String(length=64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        # EncryptedType is physically TEXT; the ORM owns envelope handling.
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("project_id", name="pk_translation_numbering_overlay"),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["translation_project.id"],
            ondelete="CASCADE",
            name="fk_translation_numbering_overlay_project",
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["user.id"],
            ondelete="CASCADE",
            name="fk_translation_numbering_overlay_owner",
        ),
        sa.ForeignKeyConstraint(
            ["source_plan_id", "project_id"],
            ["translation_source_plan.id", "translation_source_plan.project_id"],
            ondelete="CASCADE",
            name="fk_translation_numbering_overlay_plan_project",
        ),
        sa.CheckConstraint(
            "revision >= 1", name="ck_translation_numbering_overlay_revision"
        ),
        sa.CheckConstraint(
            "length(source_plan_hash) = 64",
            name="ck_translation_numbering_overlay_plan_hash",
        ),
    )


def downgrade() -> None:
    if (
        op.get_bind()
        .execute(sa.text("SELECT 1 FROM translation_numbering_overlay LIMIT 1"))
        .first()
        is not None
    ):
        raise RuntimeError(
            "Translator migration 026 cannot downgrade after numbering overlays exist."
        )
    op.drop_table("translation_numbering_overlay")
