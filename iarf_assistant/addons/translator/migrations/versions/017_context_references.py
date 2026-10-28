"""Pin verified research and memory projections in context baselines.

Revision ID: 017_context_references
Revises: 016_embedding_generations

The columns are nullable so existing artifact/glossary baselines upgrade
losslessly.  Reference material remains encrypted; only bounded identities and
receipts are duplicated in the encrypted baseline manifest/row witnesses.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "017_context_references"
down_revision = "016_embedding_generations"
branch_labels = None
depends_on = None


_CHECK_NAME = "ck_translation_context_baseline_item_reference_kind"


def _reference_check() -> sa.CheckConstraint:
    return sa.CheckConstraint(
        "(item_kind IN ('document_brief', 'style_guide', 'entity_register') "
        "AND artifact_revision_id IS NOT NULL AND glossary_revision_id IS NULL "
        "AND research_run_id IS NULL AND embedding_generation_id IS NULL "
        "AND reference_json IS NULL) OR "
        "(item_kind = 'glossary_locked' "
        "AND artifact_revision_id IS NULL AND glossary_revision_id IS NOT NULL "
        "AND research_run_id IS NULL AND embedding_generation_id IS NULL "
        "AND reference_json IS NULL) OR "
        "(item_kind = 'research_evidence' "
        "AND artifact_revision_id IS NULL AND glossary_revision_id IS NULL "
        "AND research_run_id IS NOT NULL AND embedding_generation_id IS NULL "
        "AND reference_json IS NOT NULL) OR "
        "(item_kind = 'translation_memory' "
        "AND artifact_revision_id IS NULL AND glossary_revision_id IS NULL "
        "AND research_run_id IS NULL AND embedding_generation_id IS NOT NULL "
        "AND reference_json IS NOT NULL)",
        name=_CHECK_NAME,
    )


def _assert_no_references() -> None:
    bind = op.get_bind()
    if bind.execute(
        sa.text(
            "SELECT 1 FROM translation_context_baseline_item "
            "WHERE research_run_id IS NOT NULL OR embedding_generation_id IS NOT NULL "
            "LIMIT 1"
        )
    ).first() is not None:
        raise RuntimeError(
            "Translator migration 017 cannot downgrade after context references exist."
        )


def _create_item_table(*, with_references: bool) -> None:
    columns = [
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("baseline_id", sa.String(36), nullable=False),
        sa.Column("operation_id", sa.String(36), nullable=False),
        sa.Column("project_id", sa.String(36), nullable=False),
        sa.Column("source_plan_id", sa.String(36), nullable=False),
        sa.Column("order_index", sa.Integer(), nullable=False),
        sa.Column("item_kind", sa.String(32), nullable=False),
        sa.Column("artifact_revision_id", sa.String(36), nullable=True),
        sa.Column("glossary_revision_id", sa.String(36), nullable=True),
    ]
    if with_references:
        columns.extend(
            [
                sa.Column("research_run_id", sa.String(36), nullable=True),
                sa.Column("embedding_generation_id", sa.String(36), nullable=True),
                sa.Column("reference_json", sa.Text(), nullable=True),
            ]
        )
    columns.extend(
        [
            sa.Column("revision_hash", sa.String(64), nullable=False),
            sa.Column("slot_state", sa.String(32), nullable=False, server_default="enabled"),
        ]
    )
    constraints: list[sa.Constraint] = [
        sa.UniqueConstraint("baseline_id", "order_index", name="uq_translation_context_baseline_item_order"),
        sa.CheckConstraint("order_index >= 1", name="ck_translation_context_baseline_item_order"),
        sa.CheckConstraint("item_kind IN ('document_brief', 'style_guide', 'entity_register', 'glossary_locked', 'research_evidence', 'translation_memory')", name="ck_translation_context_baseline_item_kind"),
        sa.CheckConstraint("length(revision_hash) = 64", name="ck_translation_context_baseline_item_revision_hash"),
        sa.ForeignKeyConstraint(["baseline_id", "operation_id", "source_plan_id"], ["translation_context_baseline.id", "translation_context_baseline.operation_id", "translation_context_baseline.source_plan_id"], ondelete="CASCADE", name="fk_translation_context_baseline_item_baseline_scope"),
        sa.ForeignKeyConstraint(["artifact_revision_id", "project_id", "source_plan_id"], ["translation_context_artifact_revision.id", "translation_context_artifact_revision.project_id", "translation_context_artifact_revision.source_plan_id"], ondelete="RESTRICT", name="fk_translation_context_baseline_item_artifact_scope"),
        sa.ForeignKeyConstraint(["glossary_revision_id", "project_id", "source_plan_id"], ["translation_block_glossary_revision.id", "translation_block_glossary_revision.project_id", "translation_block_glossary_revision.source_plan_id"], ondelete="RESTRICT", name="fk_translation_context_baseline_item_glossary_scope"),
    ]
    if with_references:
        constraints.extend(
            [
                _reference_check(),
                sa.ForeignKeyConstraint(["research_run_id", "source_plan_id"], ["translation_research_run.id", "translation_research_run.source_plan_id"], ondelete="RESTRICT", name="fk_translation_context_baseline_item_research_run_scope"),
                sa.ForeignKeyConstraint(["embedding_generation_id", "project_id", "source_plan_id"], ["translation_embedding_generation.id", "translation_embedding_generation.project_id", "translation_embedding_generation.source_plan_id"], ondelete="RESTRICT", name="fk_translation_context_baseline_item_embedding_generation_scope"),
            ]
        )
    else:
        constraints.append(
            sa.CheckConstraint(
                "(item_kind IN ('document_brief', 'style_guide', 'entity_register') AND artifact_revision_id IS NOT NULL AND glossary_revision_id IS NULL) OR (item_kind = 'glossary_locked' AND artifact_revision_id IS NULL AND glossary_revision_id IS NOT NULL)",
                name=_CHECK_NAME,
            )
        )
    op.create_table("translation_context_baseline_item", *columns, *constraints)


def upgrade() -> None:
    # Rebuild explicitly instead of reflecting all transitive foreign-key
    # parents; isolated legacy migration harnesses need not create workflow_run.
    op.rename_table("translation_context_baseline_item", "_translation_context_baseline_item_old")
    _create_item_table(with_references=True)
    op.execute(
        "INSERT INTO translation_context_baseline_item "
        "(id, baseline_id, operation_id, project_id, source_plan_id, order_index, "
        "item_kind, artifact_revision_id, glossary_revision_id, research_run_id, "
        "embedding_generation_id, reference_json, revision_hash, slot_state) "
        "SELECT id, baseline_id, operation_id, project_id, source_plan_id, order_index, "
        "item_kind, artifact_revision_id, glossary_revision_id, NULL, NULL, NULL, "
        "revision_hash, slot_state FROM _translation_context_baseline_item_old"
    )
    op.drop_table("_translation_context_baseline_item_old")


def downgrade() -> None:
    _assert_no_references()
    op.rename_table("translation_context_baseline_item", "_translation_context_baseline_item_new")
    _create_item_table(with_references=False)
    op.execute(
        "INSERT INTO translation_context_baseline_item "
        "(id, baseline_id, operation_id, project_id, source_plan_id, order_index, "
        "item_kind, artifact_revision_id, glossary_revision_id, revision_hash, slot_state) "
        "SELECT id, baseline_id, operation_id, project_id, source_plan_id, order_index, "
        "item_kind, artifact_revision_id, glossary_revision_id, revision_hash, slot_state "
        "FROM _translation_context_baseline_item_new"
    )
    op.drop_table("_translation_context_baseline_item_new")
