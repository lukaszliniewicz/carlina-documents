"""Add replaceable, dense-only Translator embedding generations.

Revision ID: 016_embedding_generations
Revises: 015_translation_memory

The projection is deliberately additive.  Its encrypted relational rows are
the authority for scope and source identity; sqlite-vec rows are rebuildable
retrieval state and are never accompanied by a plaintext FTS table.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "016_embedding_generations"
down_revision = "015_translation_memory"
branch_labels = None
depends_on = None


def _assert_no_evidence(*, direction: str) -> None:
    bind = op.get_bind()
    for table_name in (
        "translation_embedding_entry",
        "translation_embedding_active",
        "translation_embedding_generation",
    ):
        exists = bind.execute(
            sa.text(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = :name"
            ),
            {"name": table_name},
        ).first()
        if exists is None:
            continue
        if bind.execute(sa.text(f'SELECT 1 FROM "{table_name}" LIMIT 1')).first() is not None:
            raise RuntimeError(
                "Translator migration 016 cannot "
                f"{direction} after embedding projection evidence exists in {table_name}."
            )


def _hash(field: str, name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(f"length({field}) = 64", name=f"ck_translation_embedding_{name}")


def upgrade() -> None:
    _assert_no_evidence(direction="upgrade")
    op.create_table(
        "translation_embedding_generation",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_user_id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.String(36), nullable=False),
        sa.Column("source_plan_id", sa.String(36), nullable=False),
        sa.Column("source_plan_hash", sa.String(64), nullable=False),
        sa.Column("projection_kind", sa.String(16), nullable=False),
        sa.Column("model_record_id", sa.Integer(), nullable=False),
        sa.Column("model_owner_user_id", sa.Integer(), nullable=True),
        sa.Column("provider_id", sa.Integer(), nullable=False),
        sa.Column("provider_name", sa.String(100), nullable=False),
        sa.Column("provider_kind", sa.String(50), nullable=False),
        sa.Column("api_key_id", sa.Integer(), nullable=False),
        sa.Column("api_key_owner_user_id", sa.Integer(), nullable=True),
        sa.Column("api_key_created_at", sa.DateTime(), nullable=False),
        sa.Column("credential_witness", sa.String(64), nullable=False),
        sa.Column("model_id", sa.String(255), nullable=False),
        sa.Column("model_revision", sa.String(255), nullable=False),
        sa.Column("model_updated_at", sa.DateTime(), nullable=True),
        sa.Column("dimensions", sa.Integer(), nullable=False),
        sa.Column("task_type", sa.String(64), nullable=False),
        sa.Column("normalization", sa.String(64), nullable=False),
        sa.Column("distance_metric", sa.String(64), nullable=False),
        sa.Column("prefix_style", sa.String(64), nullable=False),
        sa.Column("query_prefix", sa.String(4096), nullable=True),
        sa.Column("passage_prefix", sa.String(4096), nullable=True),
        sa.Column("extractor_version", sa.String(128), nullable=False),
        sa.Column("profile_json", sa.Text(), nullable=False),
        sa.Column("signature_json", sa.Text(), nullable=False),
        sa.Column("signature_hash", sa.String(64), nullable=False),
        sa.Column("namespace", sa.String(128), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="building"),
        sa.Column("entry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("health_json", sa.Text(), nullable=True),
        sa.Column("error_json", sa.Text(), nullable=True),
        sa.Column("previous_generation_id", sa.String(36), nullable=True),
        sa.Column("record_hash", sa.String(64), nullable=False),
        sa.Column("state_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("state_receipt", sa.String(64), nullable=False),
        sa.Column("vector_witness", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("prepared_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("activated_at", sa.DateTime(), nullable=True),
        sa.Column("retired_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("namespace", name="uq_translation_embedding_generation_namespace"),
        sa.UniqueConstraint("id", "project_id", "source_plan_id", name="uq_translation_embedding_generation_id_scope"),
        sa.CheckConstraint("projection_kind IN ('block', 'anchor', 'memory')", name="ck_translation_embedding_generation_kind"),
        sa.CheckConstraint("status IN ('building', 'ready', 'active', 'failed', 'retired')", name="ck_translation_embedding_generation_status"),
        sa.CheckConstraint("dimensions > 0", name="ck_translation_embedding_generation_dimensions"),
        sa.CheckConstraint("entry_count >= 0", name="ck_translation_embedding_generation_entry_count"),
        _hash("source_plan_hash", "generation_plan_hash"),
        _hash("signature_hash", "generation_signature_hash"),
        _hash("record_hash", "generation_record_hash"),
        _hash("credential_witness", "generation_credential_witness"),
        _hash("state_receipt", "generation_state_receipt"),
        _hash("vector_witness", "generation_vector_witness"),
        sa.CheckConstraint("state_version >= 1", name="ck_translation_embedding_generation_state_version"),
        sa.ForeignKeyConstraint(["project_id"], ["translation_project.id"], ondelete="CASCADE", name="fk_translation_embedding_generation_project"),
        sa.ForeignKeyConstraint(["owner_user_id"], ["user.id"], ondelete="CASCADE", name="fk_translation_embedding_generation_owner"),
        sa.ForeignKeyConstraint(["source_plan_id", "project_id"], ["translation_source_plan.id", "translation_source_plan.project_id"], ondelete="CASCADE", name="fk_translation_embedding_generation_plan_scope"),
        sa.ForeignKeyConstraint(["model_record_id"], ["llm_models.id"], ondelete="RESTRICT", name="fk_translation_embedding_generation_model"),
        sa.ForeignKeyConstraint(["provider_id"], ["llm_providers.id"], ondelete="RESTRICT", name="fk_translation_embedding_generation_provider"),
        sa.ForeignKeyConstraint(["api_key_id"], ["api_key.id"], ondelete="RESTRICT", name="fk_translation_embedding_generation_api_key"),
        sa.ForeignKeyConstraint(["previous_generation_id"], ["translation_embedding_generation.id"], ondelete="SET NULL", name="fk_translation_embedding_generation_previous"),
    )
    op.create_index(
        "ix_translation_embedding_generation_scope",
        "translation_embedding_generation",
        ["owner_user_id", "project_id", "source_plan_id", "projection_kind", "status"],
    )

    op.create_table(
        "translation_embedding_entry",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("generation_id", sa.String(36), nullable=False),
        sa.Column("owner_user_id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.String(36), nullable=False),
        sa.Column("source_plan_id", sa.String(36), nullable=False),
        sa.Column("subject_type", sa.String(16), nullable=False),
        sa.Column("subject_id", sa.String(128), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("vector_row_id", sa.Integer(), nullable=False),
        sa.Column("source_text", sa.Text(), nullable=False),
        sa.Column("source_witness_hash", sa.String(64), nullable=False),
        sa.Column("subject_receipt", sa.String(64), nullable=False),
        sa.Column("record_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("generation_id", "ordinal", name="uq_translation_embedding_entry_ordinal"),
        sa.UniqueConstraint("generation_id", "subject_type", "subject_id", name="uq_translation_embedding_entry_subject"),
        sa.UniqueConstraint("generation_id", "vector_row_id", name="uq_translation_embedding_entry_vector_row"),
        sa.CheckConstraint("subject_type IN ('block', 'anchor', 'memory')", name="ck_translation_embedding_entry_subject_type"),
        sa.CheckConstraint("ordinal >= 0", name="ck_translation_embedding_entry_ordinal"),
        sa.CheckConstraint("vector_row_id > 0", name="ck_translation_embedding_entry_vector_row_positive"),
        _hash("source_witness_hash", "entry_source_witness"),
        _hash("subject_receipt", "entry_subject_receipt"),
        _hash("record_hash", "entry_record_hash"),
        sa.ForeignKeyConstraint(["generation_id"], ["translation_embedding_generation.id"], ondelete="CASCADE", name="fk_translation_embedding_entry_generation"),
        sa.ForeignKeyConstraint(["owner_user_id"], ["user.id"], ondelete="CASCADE", name="fk_translation_embedding_entry_owner"),
        sa.ForeignKeyConstraint(["project_id"], ["translation_project.id"], ondelete="CASCADE", name="fk_translation_embedding_entry_project"),
        sa.ForeignKeyConstraint(["source_plan_id", "project_id"], ["translation_source_plan.id", "translation_source_plan.project_id"], ondelete="CASCADE", name="fk_translation_embedding_entry_plan_scope"),
    )
    op.create_index(
        "ix_translation_embedding_entry_scope",
        "translation_embedding_entry",
        ["owner_user_id", "project_id", "source_plan_id", "subject_type"],
    )

    op.create_table(
        "translation_embedding_active",
        sa.Column("owner_user_id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.String(36), nullable=False),
        sa.Column("source_plan_id", sa.String(36), nullable=False),
        sa.Column("projection_kind", sa.String(16), nullable=False),
        sa.Column("generation_id", sa.String(36), nullable=False),
        sa.Column("generation_record_hash", sa.String(64), nullable=False),
        sa.Column("generation_state_receipt", sa.String(64), nullable=False),
        sa.Column("pointer_receipt", sa.String(64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("owner_user_id", "project_id", "source_plan_id", "projection_kind", name="pk_translation_embedding_active"),
        sa.CheckConstraint("projection_kind IN ('block', 'anchor', 'memory')", name="ck_translation_embedding_active_kind"),
        _hash("generation_record_hash", "active_generation_hash"),
        _hash("generation_state_receipt", "active_generation_state_receipt"),
        _hash("pointer_receipt", "active_pointer_receipt"),
        sa.CheckConstraint("version >= 1", name="ck_translation_embedding_active_version"),
        sa.ForeignKeyConstraint(["owner_user_id"], ["user.id"], ondelete="CASCADE", name="fk_translation_embedding_active_owner"),
        sa.ForeignKeyConstraint(["project_id"], ["translation_project.id"], ondelete="CASCADE", name="fk_translation_embedding_active_project"),
        sa.ForeignKeyConstraint(["source_plan_id", "project_id"], ["translation_source_plan.id", "translation_source_plan.project_id"], ondelete="CASCADE", name="fk_translation_embedding_active_plan_scope"),
        sa.ForeignKeyConstraint(["generation_id", "project_id", "source_plan_id"], ["translation_embedding_generation.id", "translation_embedding_generation.project_id", "translation_embedding_generation.source_plan_id"], ondelete="RESTRICT", name="fk_translation_embedding_active_generation_scope"),
    )


def downgrade() -> None:
    _assert_no_evidence(direction="downgrade")
    op.drop_table("translation_embedding_active")
    op.drop_index("ix_translation_embedding_entry_scope", table_name="translation_embedding_entry")
    op.drop_table("translation_embedding_entry")
    op.drop_index("ix_translation_embedding_generation_scope", table_name="translation_embedding_generation")
    op.drop_table("translation_embedding_generation")
