"""Add the immutable block-engine source plan schema.

Revision ID: 007_translation_source_plan
Revises: 006_translation_operations
"""

import sqlalchemy as sa
from alembic import op

revision = "007_translation_source_plan"
down_revision = "006_translation_operations"
branch_labels = None
depends_on = None


def upgrade():
    # Keep this a native ALTER.  A SQLite batch rebuild of the project table
    # can interact badly with its existing cascading child foreign keys.
    op.add_column(
        "translation_project",
        sa.Column("engine_contract", sa.String(length=64), nullable=True),
    )

    op.create_table(
        "translation_source_plan",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("contract_version", sa.String(length=64), nullable=False),
        sa.Column("source_document_id", sa.String(length=256), nullable=False),
        # EncryptedType is physically TEXT; the ORM owns envelope handling.
        sa.Column("source_filename", sa.Text(), nullable=True),
        sa.Column("source_language", sa.String(length=64), nullable=False),
        sa.Column("language_profile", sa.String(length=128), nullable=False),
        sa.Column("namespace", sa.String(length=36), nullable=False),
        sa.Column(
            "snapshot_contract_version", sa.String(length=64), nullable=False
        ),
        sa.Column(
            "semantic_contract_version", sa.String(length=64), nullable=False
        ),
        sa.Column("semantic_contract_mode", sa.String(length=32), nullable=False),
        sa.Column("semantic_content_hash", sa.String(length=64), nullable=False),
        sa.Column("source_content_hash", sa.String(length=64), nullable=False),
        sa.Column("snapshot_captured_at", sa.DateTime(), nullable=False),
        sa.Column("source_updated_at", sa.DateTime(), nullable=True),
        sa.Column("planner_version", sa.String(length=64), nullable=False),
        sa.Column("planner_settings_hash", sa.String(length=64), nullable=False),
        sa.Column("plan_hash", sa.String(length=64), nullable=False),
        # EncryptedType is physically TEXT.  The ORM owns envelope
        # encryption/decryption; migrations only describe storage shape.
        sa.Column("source_metadata", sa.Text(), nullable=True),
        sa.Column("source_attestation", sa.Text(), nullable=False),
        sa.Column("planner_settings", sa.Text(), nullable=False),
        sa.Column("source_provenance", sa.JSON(), nullable=True),
        sa.Column("coverage_witness", sa.JSON(), nullable=False),
        sa.Column("block_count", sa.Integer(), nullable=False),
        sa.Column("anchor_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_translation_source_plan"),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["translation_project.id"],
            ondelete="CASCADE",
            name="fk_translation_source_plan_project",
        ),
        sa.UniqueConstraint(
            "project_id",
            "plan_hash",
            name="uq_translation_source_plan_project_plan_hash",
        ),
        sa.CheckConstraint(
            "length(semantic_content_hash) = 64",
            name="ck_translation_source_plan_semantic_content_hash",
        ),
        sa.CheckConstraint(
            "length(source_content_hash) = 64",
            name="ck_translation_source_plan_source_content_hash",
        ),
        sa.CheckConstraint(
            "length(plan_hash) = 64",
            name="ck_translation_source_plan_plan_hash",
        ),
        sa.CheckConstraint(
            "length(planner_settings_hash) = 64",
            name="ck_translation_source_plan_planner_settings_hash",
        ),
        sa.CheckConstraint(
            "block_count >= 0",
            name="ck_translation_source_plan_block_count",
        ),
        sa.CheckConstraint(
            "anchor_count >= 0",
            name="ck_translation_source_plan_anchor_count",
        ),
    )
    op.create_index(
        "ix_translation_source_plan_project_created",
        "translation_source_plan",
        ["project_id", "created_at"],
        unique=False,
    )

    op.create_table(
        "translation_block",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("source_plan_id", sa.String(length=36), nullable=False),
        sa.Column("order_index", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=64), nullable=False),
        sa.Column("locator", sa.JSON(), nullable=False),
        sa.Column("classification_summary", sa.JSON(), nullable=False),
        sa.Column("source_fragment", sa.Text(), nullable=False),
        sa.Column("source_projection", sa.Text(), nullable=False),
        sa.Column("source_slots", sa.Text(), nullable=False),
        sa.Column("protected_inline", sa.Text(), nullable=False),
        sa.Column("fragment_hash", sa.String(length=64), nullable=False),
        sa.Column("projection_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_translation_block"),
        sa.ForeignKeyConstraint(
            ["source_plan_id"],
            ["translation_source_plan.id"],
            ondelete="CASCADE",
            name="fk_translation_block_source_plan",
        ),
        sa.UniqueConstraint(
            "source_plan_id",
            "order_index",
            name="uq_translation_block_plan_order",
        ),
        sa.UniqueConstraint(
            "id",
            "source_plan_id",
            name="uq_translation_block_id_source_plan",
        ),
        sa.CheckConstraint(
            "order_index >= 1",
            name="ck_translation_block_order_index",
        ),
        sa.CheckConstraint(
            "length(fragment_hash) = 64",
            name="ck_translation_block_fragment_hash",
        ),
        sa.CheckConstraint(
            "length(projection_hash) = 64",
            name="ck_translation_block_projection_hash",
        ),
    )
    op.create_index(
        "ix_translation_block_plan_order",
        "translation_block",
        ["source_plan_id", "order_index"],
        unique=False,
    )

    op.create_table(
        "translation_source_anchor",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("source_plan_id", sa.String(length=36), nullable=False),
        sa.Column("block_id", sa.String(length=128), nullable=False),
        sa.Column("order_index", sa.Integer(), nullable=False),
        sa.Column("block_order_index", sa.Integer(), nullable=False),
        sa.Column("start_offset", sa.Integer(), nullable=False),
        sa.Column("end_offset", sa.Integer(), nullable=False),
        sa.Column("splitter_profile", sa.String(length=64), nullable=False),
        sa.Column("splitter_version", sa.String(length=64), nullable=False),
        sa.Column("source_text", sa.Text(), nullable=False),
        sa.Column("source_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_translation_source_anchor"),
        sa.ForeignKeyConstraint(
            ["source_plan_id"],
            ["translation_source_plan.id"],
            ondelete="CASCADE",
            name="fk_translation_source_anchor_source_plan",
        ),
        sa.ForeignKeyConstraint(
            ["block_id", "source_plan_id"],
            ["translation_block.id", "translation_block.source_plan_id"],
            ondelete="CASCADE",
            name="fk_translation_source_anchor_block_plan",
        ),
        sa.UniqueConstraint(
            "source_plan_id",
            "order_index",
            name="uq_translation_source_anchor_plan_order",
        ),
        sa.UniqueConstraint(
            "block_id",
            "block_order_index",
            name="uq_translation_source_anchor_block_order",
        ),
        sa.CheckConstraint(
            "order_index >= 1",
            name="ck_translation_source_anchor_order_index",
        ),
        sa.CheckConstraint(
            "block_order_index >= 1",
            name="ck_translation_source_anchor_block_order_index",
        ),
        sa.CheckConstraint(
            "start_offset >= 0",
            name="ck_translation_source_anchor_start_offset",
        ),
        sa.CheckConstraint(
            "end_offset > start_offset",
            name="ck_translation_source_anchor_end_offset",
        ),
        sa.CheckConstraint(
            "length(source_hash) = 64",
            name="ck_translation_source_anchor_source_hash",
        ),
    )
    op.create_index(
        "ix_translation_source_anchor_plan_order",
        "translation_source_anchor",
        ["source_plan_id", "order_index"],
        unique=False,
    )
    op.create_index(
        "ix_translation_source_anchor_block_order",
        "translation_source_anchor",
        ["block_id", "block_order_index"],
        unique=False,
    )


def downgrade():
    bind = op.get_bind()
    durable_evidence = any(
        bind.execute(sa.text(statement)).first() is not None
        for statement in (
            "SELECT 1 FROM translation_source_plan LIMIT 1",
            (
                "SELECT 1 FROM translation_project "
                "WHERE engine_contract IS NOT NULL "
                "AND engine_contract <> 'segment-v1' LIMIT 1"
            ),
        )
    )
    if durable_evidence:
        raise RuntimeError(
            "Translator migration 007 is forward-only after a block-engine "
            "contract or source-plan evidence exists."
        )

    op.drop_index(
        "ix_translation_source_anchor_block_order",
        table_name="translation_source_anchor",
    )
    op.drop_index(
        "ix_translation_source_anchor_plan_order",
        table_name="translation_source_anchor",
    )
    op.drop_table("translation_source_anchor")

    op.drop_index("ix_translation_block_plan_order", table_name="translation_block")
    op.drop_table("translation_block")

    op.drop_index(
        "ix_translation_source_plan_project_created",
        table_name="translation_source_plan",
    )
    op.drop_table("translation_source_plan")
    op.drop_column("translation_project", "engine_contract")
