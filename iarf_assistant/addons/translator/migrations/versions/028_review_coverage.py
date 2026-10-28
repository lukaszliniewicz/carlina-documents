"""Add encrypted, append-only Translator review coverage receipts.

Revision ID: 028_review_coverage
Revises: 027_shared_glossaries
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "028_review_coverage"
down_revision = "027_shared_glossaries"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "translation_review_coverage",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("source_plan_id", sa.String(length=36), nullable=False),
        sa.Column("owner_user_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=24), nullable=False),
        sa.Column("reviewer_kind", sa.String(length=16), nullable=False),
        sa.Column("actor_type", sa.String(length=16), nullable=False),
        sa.Column("actor_user_id", sa.Integer(), nullable=True),
        sa.Column("share_grant_id", sa.String(length=36), nullable=True),
        # EncryptedType is physically TEXT; the ORM owns envelope handling.
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("record_hash", sa.String(length=64), nullable=False),
        sa.Column("integrity_receipt", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_translation_review_coverage"),
        sa.CheckConstraint(
            "kind IN ('structural', 'target_reading', 'bilingual')",
            name="ck_translation_review_coverage_kind",
        ),
        sa.CheckConstraint(
            "reviewer_kind IN ('agent', 'human')",
            name="ck_translation_review_coverage_reviewer_kind",
        ),
        sa.CheckConstraint(
            "actor_type IN ('owner', 'reviewer')",
            name="ck_translation_review_coverage_actor_type",
        ),
        sa.CheckConstraint(
            "length(record_hash) = 64",
            name="ck_translation_review_coverage_record_hash",
        ),
        sa.CheckConstraint(
            "length(integrity_receipt) = 64",
            name="ck_translation_review_coverage_integrity_receipt",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["translation_project.id"],
            ondelete="CASCADE",
            name="fk_translation_review_coverage_project",
        ),
        sa.ForeignKeyConstraint(
            ["source_plan_id", "project_id"],
            ["translation_source_plan.id", "translation_source_plan.project_id"],
            ondelete="CASCADE",
            name="fk_translation_review_coverage_plan_scope",
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["user.id"],
            ondelete="CASCADE",
            name="fk_translation_review_coverage_owner",
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["user.id"],
            ondelete="SET NULL",
            name="fk_translation_review_coverage_actor",
        ),
        sa.ForeignKeyConstraint(
            ["share_grant_id"],
            ["translation_share_grant.id"],
            ondelete="SET NULL",
            name="fk_translation_review_coverage_share_grant",
        ),
    )
    op.create_index(
        "ix_translation_review_coverage_plan_created",
        "translation_review_coverage",
        ["source_plan_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    bind = op.get_bind()
    count = bind.execute(
        sa.text("SELECT COUNT(*) FROM translation_review_coverage")
    ).scalar_one()
    if count:
        raise RuntimeError(
            "cannot downgrade review coverage while coverage records exist"
        )
    op.drop_index(
        "ix_translation_review_coverage_plan_created",
        table_name="translation_review_coverage",
    )
    op.drop_table("translation_review_coverage")
