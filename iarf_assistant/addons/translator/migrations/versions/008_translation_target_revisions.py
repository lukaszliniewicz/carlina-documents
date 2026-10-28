"""Add append-only block-engine target revisions and active pointers.

Revision ID: 008_translation_target_revisions
Revises: 007_translation_source_plan
"""

import sqlalchemy as sa
from alembic import op

revision = "008_translation_target_revisions"
down_revision = "007_translation_source_plan"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "translation_target_block_revision",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("source_plan_id", sa.String(length=36), nullable=False),
        sa.Column("block_id", sa.String(length=128), nullable=False),
        sa.Column("source_plan_hash", sa.String(length=64), nullable=False),
        sa.Column("structural_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("contract_version", sa.String(length=64), nullable=False),
        sa.Column("validator_version", sa.String(length=64), nullable=False),
        sa.Column("parent_revision_id", sa.String(length=36), nullable=True),
        sa.Column("parent_revision_hash", sa.String(length=64), nullable=True),
        sa.Column("author_type", sa.String(length=32), nullable=False),
        sa.Column("author_user_id", sa.Integer(), nullable=True),
        sa.Column("validation_state", sa.String(length=16), nullable=False),
        sa.Column("target_language", sa.String(length=64), nullable=False),
        # EncryptedType is physically TEXT; envelope handling belongs to ORM.
        sa.Column("normalized_candidate", sa.Text(), nullable=False),
        sa.Column("rendered_fragment", sa.Text(), nullable=False),
        sa.Column("validation_evidence", sa.Text(), nullable=False),
        sa.Column("source_derived_target_text", sa.Text(), nullable=False),
        sa.Column("candidate_hash", sa.String(length=64), nullable=False),
        sa.Column("normalized_hash", sa.String(length=64), nullable=False),
        sa.Column("fragment_hash", sa.String(length=64), nullable=False),
        sa.Column("evidence_hash", sa.String(length=64), nullable=False),
        sa.Column("revision_record_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_translation_target_block_revision"),
        sa.UniqueConstraint(
            "id", "source_plan_id", "block_id",
            name="uq_translation_target_revision_id_plan_block",
        ),
        sa.ForeignKeyConstraint(
            ["source_plan_id"], ["translation_source_plan.id"], ondelete="CASCADE",
            name="fk_translation_target_revision_source_plan",
        ),
        sa.ForeignKeyConstraint(
            ["block_id", "source_plan_id"],
            ["translation_block.id", "translation_block.source_plan_id"],
            ondelete="CASCADE", name="fk_translation_target_revision_block_plan",
        ),
        sa.ForeignKeyConstraint(
            ["parent_revision_id"], ["translation_target_block_revision.id"],
            ondelete="RESTRICT", name="fk_translation_target_revision_parent",
        ),
        sa.ForeignKeyConstraint(
            ["author_user_id"], ["user.id"], ondelete="SET NULL",
            name="fk_translation_target_revision_author",
        ),
        sa.CheckConstraint(
            "validation_state IN ('valid', 'rejected')",
            name="ck_translation_target_revision_validation_state",
        ),
        sa.CheckConstraint(
            "parent_revision_hash IS NULL OR length(parent_revision_hash) = 64",
            name="ck_translation_target_revision_parent_hash",
        ),
        sa.CheckConstraint(
            "(parent_revision_id IS NULL AND parent_revision_hash IS NULL) OR "
            "(parent_revision_id IS NOT NULL AND parent_revision_hash IS NOT NULL)",
            name="ck_translation_target_revision_parent_witness",
        ),
        *[
            sa.CheckConstraint(
                f"length({column}) = 64", name=f"ck_translation_target_revision_{name}"
            )
            for column, name in (
                ("source_plan_hash", "plan_hash"),
                ("structural_fingerprint", "structural_fingerprint"),
                ("candidate_hash", "candidate_hash"),
                ("normalized_hash", "normalized_hash"),
                ("fragment_hash", "fragment_hash"),
                ("evidence_hash", "evidence_hash"),
                ("revision_record_hash", "record_hash"),
            )
        ],
    )
    op.create_index(
        "ix_translation_target_revision_plan_block_created",
        "translation_target_block_revision",
        ["source_plan_id", "block_id", "created_at"], unique=False,
    )
    op.create_table(
        "translation_target_block_active",
        sa.Column("source_plan_id", sa.String(length=36), nullable=False),
        sa.Column("block_id", sa.String(length=128), nullable=False),
        sa.Column("revision_id", sa.String(length=36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("source_plan_id", "block_id", name="pk_translation_target_active"),
        sa.ForeignKeyConstraint(
            ["source_plan_id"], ["translation_source_plan.id"], ondelete="CASCADE",
            name="fk_translation_target_active_source_plan",
        ),
        sa.ForeignKeyConstraint(
            ["block_id", "source_plan_id"],
            ["translation_block.id", "translation_block.source_plan_id"],
            ondelete="CASCADE", name="fk_translation_target_active_block_plan",
        ),
        sa.ForeignKeyConstraint(
            ["revision_id", "source_plan_id", "block_id"],
            [
                "translation_target_block_revision.id",
                "translation_target_block_revision.source_plan_id",
                "translation_target_block_revision.block_id",
            ],
            ondelete="RESTRICT", name="fk_translation_target_active_revision",
        ),
        sa.CheckConstraint("version >= 1", name="ck_translation_target_active_version"),
    )


def downgrade():
    bind = op.get_bind()
    if any(
        bind.execute(sa.text(statement)).first() is not None
        for statement in (
            "SELECT 1 FROM translation_target_block_revision LIMIT 1",
            "SELECT 1 FROM translation_target_block_active LIMIT 1",
        )
    ):
        raise RuntimeError(
            "Translator migration 008 is forward-only after target revision evidence exists."
        )
    op.drop_table("translation_target_block_active")
    op.drop_index(
        "ix_translation_target_revision_plan_block_created",
        table_name="translation_target_block_revision",
    )
    op.drop_table("translation_target_block_revision")
