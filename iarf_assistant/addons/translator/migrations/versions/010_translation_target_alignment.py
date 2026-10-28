"""Add immutable target-span and alignment witnesses.

Revision ID: 010_translation_target_alignment
Revises: 009_translation_window_attempts
"""

import sqlalchemy as sa
from alembic import op

revision = "010_translation_target_alignment"
down_revision = "009_translation_window_attempts"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT 1 FROM translation_target_block_active LIMIT 1")).first() is not None:
        raise RuntimeError("Translator migration 010 requires active target pointers to be reconciled with alignment evidence before upgrade.")
    op.create_table(
        "translation_target_span_set",
        sa.Column("id", sa.String(36), nullable=False), sa.Column("source_plan_id", sa.String(36), nullable=False), sa.Column("block_id", sa.String(128), nullable=False), sa.Column("revision_id", sa.String(36), nullable=False),
        sa.Column("target_revision_hash", sa.String(64), nullable=False), sa.Column("projection_scope", sa.String(160), nullable=False), sa.Column("profile_id", sa.String(64), nullable=False), sa.Column("profile_version", sa.String(64), nullable=False), sa.Column("projection", sa.Text(), nullable=False), sa.Column("projection_hash", sa.String(64), nullable=False), sa.Column("span_count", sa.Integer(), nullable=False), sa.Column("record_hash", sa.String(64), nullable=False), sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_translation_target_span_set"),
        sa.UniqueConstraint("id", "source_plan_id", "block_id", "revision_id", name="uq_translation_target_span_set_identity"),
        sa.UniqueConstraint("source_plan_id", "block_id", "revision_id", "projection_scope", "profile_id", "profile_version", "projection_hash", name="uq_translation_target_span_set_material"),
        sa.CheckConstraint("span_count >= 0", name="ck_translation_target_span_set_count"),
        *[sa.CheckConstraint(f"length({column}) = 64", name=name) for column, name in (("target_revision_hash", "ck_translation_target_span_set_revision_hash"), ("projection_hash", "ck_translation_target_span_set_projection_hash"), ("record_hash", "ck_translation_target_span_set_record_hash"))],
        sa.ForeignKeyConstraint(["source_plan_id"], ["translation_source_plan.id"], ondelete="CASCADE", name="fk_translation_target_span_set_plan"),
        sa.ForeignKeyConstraint(["block_id", "source_plan_id"], ["translation_block.id", "translation_block.source_plan_id"], ondelete="CASCADE", name="fk_translation_target_span_set_block_plan"),
        sa.ForeignKeyConstraint(["revision_id", "source_plan_id", "block_id"], ["translation_target_block_revision.id", "translation_target_block_revision.source_plan_id", "translation_target_block_revision.block_id"], ondelete="RESTRICT", name="fk_translation_target_span_set_revision_plan_block"),
    )
    op.create_index("ix_translation_target_span_set_revision", "translation_target_span_set", ["source_plan_id", "block_id", "revision_id", "created_at"])
    op.create_table(
        "translation_target_span",
        sa.Column("id", sa.String(36), nullable=False), sa.Column("span_set_id", sa.String(36), nullable=False), sa.Column("source_plan_id", sa.String(36), nullable=False), sa.Column("block_id", sa.String(128), nullable=False), sa.Column("revision_id", sa.String(36), nullable=False), sa.Column("order_index", sa.Integer(), nullable=False), sa.Column("start_offset", sa.Integer(), nullable=False), sa.Column("end_offset", sa.Integer(), nullable=False), sa.Column("text_hash", sa.String(64), nullable=False), sa.Column("span_hash", sa.String(64), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_translation_target_span"),
        sa.UniqueConstraint("span_set_id", "order_index", name="uq_translation_target_span_set_order"), sa.UniqueConstraint("id", "span_set_id", "source_plan_id", "block_id", "revision_id", name="uq_translation_target_span_identity"),
        sa.CheckConstraint("order_index >= 1", name="ck_translation_target_span_order"), sa.CheckConstraint("start_offset >= 0", name="ck_translation_target_span_start"), sa.CheckConstraint("end_offset > start_offset", name="ck_translation_target_span_end"), sa.CheckConstraint("length(text_hash) = 64", name="ck_translation_target_span_text_hash"), sa.CheckConstraint("length(span_hash) = 64", name="ck_translation_target_span_hash"),
        sa.ForeignKeyConstraint(["span_set_id", "source_plan_id", "block_id", "revision_id"], ["translation_target_span_set.id", "translation_target_span_set.source_plan_id", "translation_target_span_set.block_id", "translation_target_span_set.revision_id"], ondelete="CASCADE", name="fk_translation_target_span_set_identity"),
    )
    op.create_index("ix_translation_target_span_set", "translation_target_span", ["span_set_id", "order_index"])
    op.create_table(
        "translation_alignment_revision",
        sa.Column("id", sa.String(36), nullable=False), sa.Column("source_plan_id", sa.String(36), nullable=False), sa.Column("block_id", sa.String(128), nullable=False), sa.Column("revision_id", sa.String(36), nullable=False), sa.Column("span_set_id", sa.String(36), nullable=False), sa.Column("target_revision_hash", sa.String(64), nullable=False), sa.Column("span_set_hash", sa.String(64), nullable=False), sa.Column("method", sa.String(64), nullable=False), sa.Column("confidence_profile", sa.String(64), nullable=False), sa.Column("review_state", sa.String(32), nullable=False), sa.Column("group_count", sa.Integer(), nullable=False), sa.Column("payload", sa.Text(), nullable=False), sa.Column("groups_hash", sa.String(64), nullable=False), sa.Column("record_hash", sa.String(64), nullable=False), sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_translation_alignment_revision"), sa.UniqueConstraint("id", "source_plan_id", "block_id", "revision_id", name="uq_translation_alignment_revision_identity"), sa.UniqueConstraint("id", "source_plan_id", "block_id", "revision_id", "span_set_id", name="uq_translation_alignment_revision_span_set_identity"),
        sa.CheckConstraint("group_count >= 0", name="ck_translation_alignment_revision_group_count"), sa.CheckConstraint("review_state IN ('approved', 'needs_review')", name="ck_translation_alignment_revision_review_state"),
        *[sa.CheckConstraint(f"length({column}) = 64", name=name) for column, name in (("target_revision_hash", "ck_translation_alignment_revision_target_hash"), ("span_set_hash", "ck_translation_alignment_revision_span_set_hash"), ("groups_hash", "ck_translation_alignment_revision_groups_hash"), ("record_hash", "ck_translation_alignment_revision_record_hash"))],
        sa.ForeignKeyConstraint(["source_plan_id"], ["translation_source_plan.id"], ondelete="CASCADE", name="fk_translation_alignment_revision_plan"), sa.ForeignKeyConstraint(["block_id", "source_plan_id"], ["translation_block.id", "translation_block.source_plan_id"], ondelete="CASCADE", name="fk_translation_alignment_revision_block_plan"), sa.ForeignKeyConstraint(["revision_id", "source_plan_id", "block_id"], ["translation_target_block_revision.id", "translation_target_block_revision.source_plan_id", "translation_target_block_revision.block_id"], ondelete="RESTRICT", name="fk_translation_alignment_revision_target_plan_block"), sa.ForeignKeyConstraint(["span_set_id", "source_plan_id", "block_id", "revision_id"], ["translation_target_span_set.id", "translation_target_span_set.source_plan_id", "translation_target_span_set.block_id", "translation_target_span_set.revision_id"], ondelete="RESTRICT", name="fk_translation_alignment_revision_span_set_identity"),
    )
    op.create_index("ix_translation_alignment_revision_target", "translation_alignment_revision", ["source_plan_id", "block_id", "revision_id", "created_at"])
    op.create_table(
        "translation_alignment_group",
        sa.Column("id", sa.String(36), nullable=False), sa.Column("alignment_revision_id", sa.String(36), nullable=False), sa.Column("source_plan_id", sa.String(36), nullable=False), sa.Column("block_id", sa.String(128), nullable=False), sa.Column("revision_id", sa.String(36), nullable=False), sa.Column("span_set_id", sa.String(36), nullable=False), sa.Column("order_index", sa.Integer(), nullable=False), sa.Column("source_anchor_ids", sa.Text(), nullable=False), sa.Column("target_span_ids", sa.Text(), nullable=False), sa.Column("source_witness_hash", sa.String(64), nullable=False), sa.Column("target_witness_hash", sa.String(64), nullable=False), sa.Column("method", sa.String(64), nullable=False), sa.Column("confidence", sa.Float(), nullable=False), sa.Column("review_state", sa.String(32), nullable=False), sa.Column("evidence", sa.Text(), nullable=False), sa.Column("evidence_hash", sa.String(64), nullable=False), sa.Column("group_hash", sa.String(64), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_translation_alignment_group"), sa.UniqueConstraint("alignment_revision_id", "order_index", name="uq_translation_alignment_group_order"), sa.CheckConstraint("order_index >= 1", name="ck_translation_alignment_group_order"), sa.CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_translation_alignment_group_confidence"), sa.CheckConstraint("review_state IN ('approved', 'needs_review')", name="ck_translation_alignment_group_review_state"),
        *[sa.CheckConstraint(f"length({column}) = 64", name=name) for column, name in (("source_witness_hash", "ck_translation_alignment_group_source_witness"), ("target_witness_hash", "ck_translation_alignment_group_target_witness"), ("evidence_hash", "ck_translation_alignment_group_evidence_hash"), ("group_hash", "ck_translation_alignment_group_hash"))],
        sa.ForeignKeyConstraint(["alignment_revision_id", "source_plan_id", "block_id", "revision_id", "span_set_id"], ["translation_alignment_revision.id", "translation_alignment_revision.source_plan_id", "translation_alignment_revision.block_id", "translation_alignment_revision.revision_id", "translation_alignment_revision.span_set_id"], ondelete="CASCADE", name="fk_translation_alignment_group_revision_identity"),
    )
    op.create_index("ix_translation_alignment_group_revision", "translation_alignment_group", ["alignment_revision_id", "order_index"])
    op.create_table(
        "translation_target_alignment_active",
        sa.Column("source_plan_id", sa.String(36), nullable=False), sa.Column("block_id", sa.String(128), nullable=False), sa.Column("revision_id", sa.String(36), nullable=False), sa.Column("alignment_revision_id", sa.String(36), nullable=False), sa.Column("version", sa.Integer(), nullable=False, server_default="1"), sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("source_plan_id", "block_id", name="pk_translation_target_alignment_active"), sa.CheckConstraint("version >= 1", name="ck_translation_target_alignment_active_version"),
        sa.ForeignKeyConstraint(["source_plan_id"], ["translation_source_plan.id"], ondelete="CASCADE", name="fk_translation_target_alignment_active_plan"), sa.ForeignKeyConstraint(["block_id", "source_plan_id"], ["translation_block.id", "translation_block.source_plan_id"], ondelete="CASCADE", name="fk_translation_target_alignment_active_block_plan"), sa.ForeignKeyConstraint(["revision_id", "source_plan_id", "block_id"], ["translation_target_block_revision.id", "translation_target_block_revision.source_plan_id", "translation_target_block_revision.block_id"], ondelete="RESTRICT", name="fk_translation_target_alignment_active_target"), sa.ForeignKeyConstraint(["alignment_revision_id", "source_plan_id", "block_id", "revision_id"], ["translation_alignment_revision.id", "translation_alignment_revision.source_plan_id", "translation_alignment_revision.block_id", "translation_alignment_revision.revision_id"], ondelete="RESTRICT", name="fk_translation_target_alignment_active_alignment"),
    )


def downgrade():
    bind = op.get_bind()
    if any(bind.execute(sa.text(statement)).first() is not None for statement in (
        "SELECT 1 FROM translation_target_span_set LIMIT 1", "SELECT 1 FROM translation_target_span LIMIT 1", "SELECT 1 FROM translation_alignment_revision LIMIT 1", "SELECT 1 FROM translation_alignment_group LIMIT 1", "SELECT 1 FROM translation_target_alignment_active LIMIT 1",
    )):
        raise RuntimeError("Translator migration 010 is forward-only after alignment evidence exists.")
    op.drop_table("translation_target_alignment_active")
    op.drop_index("ix_translation_alignment_group_revision", table_name="translation_alignment_group")
    op.drop_table("translation_alignment_group")
    op.drop_index("ix_translation_alignment_revision_target", table_name="translation_alignment_revision")
    op.drop_table("translation_alignment_revision")
    op.drop_index("ix_translation_target_span_set", table_name="translation_target_span")
    op.drop_table("translation_target_span")
    op.drop_index("ix_translation_target_span_set_revision", table_name="translation_target_span_set")
    op.drop_table("translation_target_span_set")
