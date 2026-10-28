"""Add authenticated cross-operation analysis/research evidence reuse.

Revision ID: 019_operation_evidence_reuse
Revises: 018_block_workflow_config
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "019_operation_evidence_reuse"
down_revision = "018_block_workflow_config"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "translation_operation_evidence_reuse",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("contract_version", sa.String(length=64), nullable=False),
        sa.Column("operation_id", sa.String(length=36), nullable=False),
        sa.Column("origin_operation_id", sa.String(length=36), nullable=False),
        sa.Column("owner_user_id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("source_plan_id", sa.String(length=36), nullable=False),
        sa.Column("source_plan_hash", sa.String(length=64), nullable=False),
        sa.Column("source_content_hash", sa.String(length=64), nullable=False),
        sa.Column("workflow_config_id", sa.String(length=36), nullable=False),
        sa.Column("workflow_config_hash", sa.String(length=64), nullable=False),
        sa.Column("origin_workflow_config_id", sa.String(length=36), nullable=False),
        sa.Column("origin_workflow_config_hash", sa.String(length=64), nullable=False),
        sa.Column("document_brief_revision_id", sa.String(length=36), nullable=False),
        sa.Column("document_brief_revision_hash", sa.String(length=64), nullable=False),
        sa.Column("style_guide_revision_id", sa.String(length=36), nullable=False),
        sa.Column("style_guide_revision_hash", sa.String(length=64), nullable=False),
        sa.Column("entity_register_revision_id", sa.String(length=36), nullable=False),
        sa.Column(
            "entity_register_revision_hash", sa.String(length=64), nullable=False
        ),
        sa.Column("research_run_id", sa.String(length=36), nullable=True),
        sa.Column("research_run_record_hash", sa.String(length=64), nullable=True),
        # EncryptedType is physically TEXT; the ORM owns envelope handling.
        sa.Column("selection_json", sa.Text(), nullable=False),
        sa.Column("selection_hash", sa.String(length=64), nullable=False),
        sa.Column("selection_receipt", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_translation_operation_evidence_reuse"),
        sa.UniqueConstraint(
            "operation_id", name="uq_translation_operation_evidence_reuse_operation"
        ),
        sa.CheckConstraint(
            "contract_version = 'translator-operation-evidence-reuse-v1'",
            name="ck_translation_operation_evidence_reuse_contract",
        ),
        *[
            sa.CheckConstraint(f"length({column}) = 64", name=name)
            for column, name in (
                (
                    "source_plan_hash",
                    "ck_translation_operation_evidence_reuse_plan_hash",
                ),
                (
                    "source_content_hash",
                    "ck_translation_operation_evidence_reuse_content_hash",
                ),
                (
                    "workflow_config_hash",
                    "ck_translation_operation_evidence_reuse_workflow_hash",
                ),
                (
                    "origin_workflow_config_hash",
                    "ck_translation_operation_evidence_reuse_origin_workflow_hash",
                ),
                (
                    "document_brief_revision_hash",
                    "ck_translation_operation_evidence_reuse_brief_hash",
                ),
                (
                    "style_guide_revision_hash",
                    "ck_translation_operation_evidence_reuse_style_hash",
                ),
                (
                    "entity_register_revision_hash",
                    "ck_translation_operation_evidence_reuse_entity_hash",
                ),
                (
                    "selection_hash",
                    "ck_translation_operation_evidence_reuse_selection_hash",
                ),
                (
                    "selection_receipt",
                    "ck_translation_operation_evidence_reuse_selection_receipt",
                ),
            )
        ],
        sa.CheckConstraint(
            "(research_run_id IS NULL AND research_run_record_hash IS NULL) OR "
            "(research_run_id IS NOT NULL AND length(research_run_record_hash) = 64)",
            name="ck_translation_operation_evidence_reuse_research",
        ),
        sa.ForeignKeyConstraint(
            ["operation_id", "project_id"],
            ["translation_operation.id", "translation_operation.project_id"],
            ondelete="CASCADE",
            name="fk_translation_operation_evidence_reuse_operation",
        ),
        sa.ForeignKeyConstraint(
            ["origin_operation_id", "project_id"],
            ["translation_operation.id", "translation_operation.project_id"],
            ondelete="RESTRICT",
            name="fk_translation_operation_evidence_reuse_origin_operation",
        ),
        sa.ForeignKeyConstraint(
            ["workflow_config_id", "project_id"],
            [
                "translation_block_workflow_config.id",
                "translation_block_workflow_config.project_id",
            ],
            ondelete="RESTRICT",
            name="fk_translation_operation_evidence_reuse_workflow",
        ),
        sa.ForeignKeyConstraint(
            ["origin_workflow_config_id", "project_id"],
            [
                "translation_block_workflow_config.id",
                "translation_block_workflow_config.project_id",
            ],
            ondelete="RESTRICT",
            name="fk_translation_operation_evidence_reuse_origin_workflow",
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["user.id"],
            ondelete="CASCADE",
            name="fk_translation_operation_evidence_reuse_owner",
        ),
        sa.ForeignKeyConstraint(
            ["source_plan_id", "project_id"],
            ["translation_source_plan.id", "translation_source_plan.project_id"],
            ondelete="RESTRICT",
            name="fk_translation_operation_evidence_reuse_plan",
        ),
        *[
            sa.ForeignKeyConstraint(
                [column, "project_id", "source_plan_id"],
                [
                    "translation_context_artifact_revision.id",
                    "translation_context_artifact_revision.project_id",
                    "translation_context_artifact_revision.source_plan_id",
                ],
                ondelete="RESTRICT",
                name=name,
            )
            for column, name in (
                (
                    "document_brief_revision_id",
                    "fk_translation_operation_evidence_reuse_brief",
                ),
                (
                    "style_guide_revision_id",
                    "fk_translation_operation_evidence_reuse_style",
                ),
                (
                    "entity_register_revision_id",
                    "fk_translation_operation_evidence_reuse_entity",
                ),
            )
        ],
        sa.ForeignKeyConstraint(
            ["research_run_id", "source_plan_id"],
            ["translation_research_run.id", "translation_research_run.source_plan_id"],
            ondelete="RESTRICT",
            name="fk_translation_operation_evidence_reuse_research",
        ),
    )
    op.create_index(
        "ix_translation_operation_evidence_reuse_scope",
        "translation_operation_evidence_reuse",
        ["owner_user_id", "project_id", "source_plan_id"],
        unique=False,
    )


def downgrade() -> None:
    bind = op.get_bind()
    if (
        bind.execute(
            sa.text("SELECT 1 FROM translation_operation_evidence_reuse LIMIT 1")
        ).first()
        is not None
    ):
        raise RuntimeError(
            "Translator migration 019 cannot downgrade after evidence-reuse "
            "receipts exist."
        )
    op.drop_index(
        "ix_translation_operation_evidence_reuse_scope",
        table_name="translation_operation_evidence_reuse",
    )
    op.drop_table("translation_operation_evidence_reuse")
