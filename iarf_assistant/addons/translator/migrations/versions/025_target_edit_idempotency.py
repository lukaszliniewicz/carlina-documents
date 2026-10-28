"""Persist actor-scoped idempotent manual target-edit receipts.

Revision ID: 025_target_edit_idempotency
Revises: 024_scholarly_review
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "025_target_edit_idempotency"
down_revision = "024_scholarly_review"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "translation_target_edit_idempotency",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("source_plan_id", sa.String(length=36), nullable=False),
        sa.Column("block_id", sa.String(length=128), nullable=False),
        sa.Column("target_revision_id", sa.String(length=36), nullable=False),
        sa.Column("actor_type", sa.String(length=16), nullable=False),
        sa.Column("actor_id", sa.String(length=128), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("expected_active_version", sa.Integer(), nullable=False),
        sa.Column("active_version", sa.Integer(), nullable=False),
        # EncryptedType is physically TEXT; the ORM owns envelope handling.
        sa.Column("safe_result_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint(
            "id", name="pk_translation_target_edit_idempotency"
        ),
        sa.UniqueConstraint(
            "project_id",
            "actor_type",
            "actor_id",
            "idempotency_key",
            name="uq_translation_target_edit_idempotency_actor_key",
        ),
        sa.UniqueConstraint(
            "id",
            "project_id",
            "source_plan_id",
            "block_id",
            "target_revision_id",
            name="uq_translation_target_edit_idempotency_scope",
        ),
        sa.CheckConstraint(
            "actor_type IN ('owner', 'reviewer')",
            name="ck_translation_target_edit_idempotency_actor_type",
        ),
        sa.CheckConstraint(
            "length(actor_id) BETWEEN 1 AND 128",
            name="ck_translation_target_edit_idempotency_actor_id",
        ),
        sa.CheckConstraint(
            "length(idempotency_key) BETWEEN 1 AND 128",
            name="ck_translation_target_edit_idempotency_key",
        ),
        sa.CheckConstraint(
            "length(request_fingerprint) = 64",
            name="ck_translation_target_edit_idempotency_fingerprint",
        ),
        sa.CheckConstraint(
            "expected_active_version >= 0",
            name="ck_translation_target_edit_idempotency_expected_version",
        ),
        sa.CheckConstraint(
            "active_version >= 1",
            name="ck_translation_target_edit_idempotency_active_version",
        ),
        sa.CheckConstraint(
            "actor_type = 'owner' OR actor_id LIKE 'grant:%'",
            name="ck_translation_target_edit_idempotency_reviewer_actor",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["translation_project.id"],
            ondelete="CASCADE",
            name="fk_translation_target_edit_idempotency_project",
        ),
        sa.ForeignKeyConstraint(
            ["source_plan_id", "project_id"],
            ["translation_source_plan.id", "translation_source_plan.project_id"],
            ondelete="CASCADE",
            name="fk_translation_target_edit_idempotency_plan_scope",
        ),
        sa.ForeignKeyConstraint(
            ["block_id", "source_plan_id"],
            ["translation_block.id", "translation_block.source_plan_id"],
            ondelete="CASCADE",
            name="fk_translation_target_edit_idempotency_block_scope",
        ),
        sa.ForeignKeyConstraint(
            ["target_revision_id", "source_plan_id", "block_id"],
            [
                "translation_target_block_revision.id",
                "translation_target_block_revision.source_plan_id",
                "translation_target_block_revision.block_id",
            ],
            ondelete="CASCADE",
            name="fk_translation_target_edit_idempotency_target_revision",
        ),
    )
    op.create_index(
        "ix_translation_target_edit_idempotency_project_created",
        "translation_target_edit_idempotency",
        ["project_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.execute(
        sa.text(
            "SELECT 1 FROM translation_target_edit_idempotency LIMIT 1"
        )
    ).first() is not None:
        raise RuntimeError(
            "Translator migration 025 cannot downgrade after target-edit receipts exist."
        )
    op.drop_index(
        "ix_translation_target_edit_idempotency_project_created",
        table_name="translation_target_edit_idempotency",
    )
    op.drop_table("translation_target_edit_idempotency")
