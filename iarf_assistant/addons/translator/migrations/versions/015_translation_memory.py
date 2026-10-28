"""Add alignment-witnessed, encrypted translation-memory evidence.

Revision ID: 015_translation_memory
Revises: 014_research_runs

The table is an additive projection.  It is deliberately independent from
the legacy segment/vector tables and its migration is reversible only while
the new evidence table is empty.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "015_translation_memory"
down_revision = "014_research_runs"
branch_labels = None
depends_on = None


def _assert_no_evidence(*, direction: str) -> None:
    bind = op.get_bind()
    tables = {
        row[0]
        for row in bind.execute(
            sa.text("SELECT name FROM sqlite_master WHERE type = 'table'")
        )
    }
    if (
        "translation_memory_entry" in tables
        and bind.execute(
            sa.text('SELECT 1 FROM "translation_memory_entry" LIMIT 1')
        ).first()
        is not None
    ):
        raise RuntimeError(
            "Translator migration 015 cannot "
            f"{direction} after translation-memory evidence exists."
        )


def _hash(field: str, name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(
        f"length({field}) = 64",
        name=f"ck_translation_memory_entry_{name}",
    )


def upgrade() -> None:
    _assert_no_evidence(direction="upgrade")
    op.create_table(
        "translation_memory_entry",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_user_id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.String(36), nullable=False),
        sa.Column("source_plan_id", sa.String(36), nullable=False),
        sa.Column("block_id", sa.String(128), nullable=False),
        sa.Column("target_revision_id", sa.String(36), nullable=False),
        sa.Column("target_span_set_id", sa.String(36), nullable=False),
        sa.Column("alignment_revision_id", sa.String(36), nullable=False),
        sa.Column("source_plan_hash", sa.String(64), nullable=False),
        sa.Column("target_revision_hash", sa.String(64), nullable=False),
        sa.Column("target_span_set_hash", sa.String(64), nullable=False),
        sa.Column("alignment_revision_hash", sa.String(64), nullable=False),
        sa.Column("active_version", sa.Integer(), nullable=False),
        sa.Column("active_pair_receipt", sa.String(64), nullable=False),
        sa.Column("target_activation_at", sa.DateTime(), nullable=False),
        sa.Column("alignment_activation_at", sa.DateTime(), nullable=False),
        sa.Column("source_language", sa.String(64), nullable=False),
        sa.Column("target_language", sa.String(64), nullable=False),
        sa.Column("semantic_role", sa.String(64), nullable=False),
        sa.Column("structure_fingerprint", sa.String(64), nullable=False),
        sa.Column("classification", sa.String(32), nullable=False),
        sa.Column("granularity", sa.String(16), nullable=False),
        sa.Column(
            "scope_kind", sa.String(16), nullable=False, server_default="project"
        ),
        sa.Column("scope_id", sa.String(36), nullable=True),
        sa.Column("source_lookup_digest", sa.String(160), nullable=False),
        sa.Column("target_lookup_digest", sa.String(160), nullable=False),
        sa.Column("derivation_key", sa.String(64), nullable=False),
        sa.Column("source_witness_hash", sa.String(64), nullable=False),
        sa.Column("target_witness_hash", sa.String(64), nullable=False),
        sa.Column("source_evidence_hash", sa.String(64), nullable=False),
        sa.Column("target_evidence_hash", sa.String(64), nullable=False),
        sa.Column("group_ids_hash", sa.String(64), nullable=False),
        sa.Column("provenance_hash", sa.String(64), nullable=False),
        sa.Column("record_hash", sa.String(64), nullable=False),
        sa.Column("source_text", sa.Text(), nullable=False),
        sa.Column("target_text", sa.Text(), nullable=False),
        sa.Column("source_evidence_json", sa.Text(), nullable=False),
        sa.Column("target_evidence_json", sa.Text(), nullable=False),
        sa.Column("alignment_group_ids_json", sa.Text(), nullable=False),
        sa.Column("provenance_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "owner_user_id",
            "project_id",
            "derivation_key",
            name="uq_translation_memory_entry_derivation",
        ),
        sa.CheckConstraint(
            "classification IN ('contextual_precedent', 'deliberate_variant')",
            name="ck_translation_memory_entry_classification",
        ),
        sa.CheckConstraint(
            "granularity IN ('sentence', 'block')",
            name="ck_translation_memory_entry_granularity",
        ),
        sa.CheckConstraint(
            "scope_kind = 'project'",
            name="ck_translation_memory_entry_project_scope",
        ),
        sa.CheckConstraint(
            "scope_id IS NULL OR scope_id = project_id",
            name="ck_translation_memory_entry_scope_id",
        ),
        *[
            _hash(field, name)
            for field, name in (
                ("source_plan_hash", "plan_hash"),
                ("target_revision_hash", "target_revision_hash"),
                ("target_span_set_hash", "span_set_hash"),
                ("alignment_revision_hash", "alignment_hash"),
                ("structure_fingerprint", "structure_fingerprint"),
                ("source_witness_hash", "source_witness"),
                ("target_witness_hash", "target_witness"),
                ("source_evidence_hash", "source_evidence"),
                ("target_evidence_hash", "target_evidence"),
                ("group_ids_hash", "group_ids"),
                ("provenance_hash", "provenance"),
                ("record_hash", "record"),
                ("derivation_key", "derivation_key"),
            )
        ],
        sa.CheckConstraint(
            "length(source_lookup_digest) BETWEEN 64 AND 160",
            name="ck_translation_memory_entry_source_lookup",
        ),
        sa.CheckConstraint(
            "length(target_lookup_digest) BETWEEN 64 AND 160",
            name="ck_translation_memory_entry_target_lookup",
        ),
        sa.CheckConstraint(
            "active_version >= 1",
            name="ck_translation_memory_entry_active_version",
        ),
        sa.CheckConstraint(
            "length(active_pair_receipt) = 64",
            name="ck_translation_memory_entry_active_pair_receipt",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["translation_project.id"],
            ondelete="CASCADE",
            name="fk_translation_memory_entry_project",
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["user.id"],
            ondelete="CASCADE",
            name="fk_translation_memory_entry_owner",
        ),
        sa.ForeignKeyConstraint(
            ["source_plan_id", "project_id"],
            ["translation_source_plan.id", "translation_source_plan.project_id"],
            ondelete="CASCADE",
            name="fk_translation_memory_entry_plan_scope",
        ),
        sa.ForeignKeyConstraint(
            ["block_id", "source_plan_id"],
            ["translation_block.id", "translation_block.source_plan_id"],
            ondelete="CASCADE",
            name="fk_translation_memory_entry_block_scope",
        ),
        sa.ForeignKeyConstraint(
            ["target_revision_id", "source_plan_id", "block_id"],
            [
                "translation_target_block_revision.id",
                "translation_target_block_revision.source_plan_id",
                "translation_target_block_revision.block_id",
            ],
            ondelete="RESTRICT",
            name="fk_translation_memory_entry_target_revision",
        ),
        sa.ForeignKeyConstraint(
            ["target_span_set_id", "source_plan_id", "block_id", "target_revision_id"],
            [
                "translation_target_span_set.id",
                "translation_target_span_set.source_plan_id",
                "translation_target_span_set.block_id",
                "translation_target_span_set.revision_id",
            ],
            ondelete="RESTRICT",
            name="fk_translation_memory_entry_span_set",
        ),
        sa.ForeignKeyConstraint(
            [
                "alignment_revision_id",
                "source_plan_id",
                "block_id",
                "target_revision_id",
                "target_span_set_id",
            ],
            [
                "translation_alignment_revision.id",
                "translation_alignment_revision.source_plan_id",
                "translation_alignment_revision.block_id",
                "translation_alignment_revision.revision_id",
                "translation_alignment_revision.span_set_id",
            ],
            ondelete="RESTRICT",
            name="fk_translation_memory_entry_alignment",
        ),
    )
    op.create_index(
        "ix_translation_memory_entry_lookup",
        "translation_memory_entry",
        [
            "owner_user_id",
            "project_id",
            "source_lookup_digest",
            "source_language",
            "target_language",
        ],
    )
    op.create_index(
        "ix_translation_memory_entry_parent",
        "translation_memory_entry",
        ["source_plan_id", "block_id", "alignment_revision_id"],
    )


def downgrade() -> None:
    _assert_no_evidence(direction="downgrade")
    op.drop_index(
        "ix_translation_memory_entry_parent", table_name="translation_memory_entry"
    )
    op.drop_index(
        "ix_translation_memory_entry_lookup", table_name="translation_memory_entry"
    )
    op.drop_table("translation_memory_entry")
