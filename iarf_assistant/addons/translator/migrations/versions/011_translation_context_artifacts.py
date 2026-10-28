"""Add encrypted Phase-5A analysis, glossary, and frozen baseline evidence.

Revision ID: 011_translation_context_artifacts
Revises: 010_translation_target_alignment
"""

import sqlalchemy as sa
from alembic import op

revision = "011_context_artifacts"
down_revision = "010_translation_target_alignment"
branch_labels = None
depends_on = None


def _hash_checks(table, fields):
    return [sa.CheckConstraint(f"length({field}) = 64", name=f"ck_{table}_{name}") for field, name in fields]


def _operation_before_plan_pin() -> sa.Table:
    """Avoid reflecting optional host runtime FKs during SQLite batch copy."""
    metadata = sa.MetaData()
    table = sa.Table(
        "translation_operation", metadata,
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("project_id", sa.String(36), nullable=False),
        sa.Column("owner_user_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("queue_job_id", sa.String(128)),
        sa.Column("state", sa.String(32), nullable=False, server_default="queued"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("progress", sa.Float()), sa.Column("message", sa.String(255)),
        sa.Column("error_code", sa.String(64)), sa.Column("error_detail", sa.Text()),
        sa.Column("checkpoint_json", sa.Text()), sa.Column("result_json", sa.Text()),
        sa.Column("source_content_hash", sa.String(64)),
        sa.Column("cancel_requested_at", sa.DateTime()), sa.Column("lease_expires_at", sa.DateTime()),
        sa.Column("started_at", sa.DateTime()), sa.Column("finished_at", sa.DateTime()),
        sa.Column("created_at", sa.DateTime(), nullable=False), sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_translation_operation"),
        sa.ForeignKeyConstraint(["project_id"], ["translation_project.id"], ondelete="CASCADE", name="fk_translation_operation_project"),
        sa.ForeignKeyConstraint(["owner_user_id"], ["user.id"], ondelete="CASCADE", name="fk_translation_operation_owner"),
        sa.UniqueConstraint("owner_user_id", "project_id", "kind", "idempotency_key", name="uq_translation_operation_owner_project_kind_key"),
        sa.CheckConstraint("state IN ('queued', 'running', 'cancel_requested', 'completed', 'partial', 'failed', 'cancelled')", name="ck_translation_operation_state"),
        sa.CheckConstraint("version >= 1", name="ck_translation_operation_version"),
        sa.CheckConstraint("progress IS NULL OR (progress >= 0 AND progress <= 100)", name="ck_translation_operation_progress"),
        sa.CheckConstraint("length(request_fingerprint) = 64", name="ck_translation_operation_request_fingerprint"),
        sa.CheckConstraint("source_content_hash IS NULL OR length(source_content_hash) = 64", name="ck_translation_operation_source_hash"),
        sa.CheckConstraint("error_code IS NULL OR (length(error_code) BETWEEN 1 AND 64 AND substr(error_code, 1, 1) GLOB '[a-z]' AND error_code NOT GLOB '*[^a-z0-9_]*')", name="ck_translation_operation_error_code"),
        sa.CheckConstraint("state NOT IN ('cancel_requested', 'cancelled') OR cancel_requested_at IS NOT NULL", name="ck_translation_operation_cancellation_timestamp"),
    )
    sa.Index("ix_translation_operation_project_state_updated", table.c.project_id, table.c.state, table.c.updated_at)
    sa.Index("ix_translation_operation_owner_kind_created", table.c.owner_user_id, table.c.kind, table.c.created_at)
    sa.Index("ix_translation_operation_queue_job", table.c.queue_job_id)
    sa.Index("ix_translation_operation_owner_state", table.c.owner_user_id, table.c.state)
    sa.Index("uq_translation_operation_active_project", table.c.project_id, unique=True, sqlite_where=sa.text("state IN ('queued', 'running', 'cancel_requested')"))
    return table


def _operation_with_plan_pin() -> sa.Table:
    table = _operation_before_plan_pin()
    table.append_column(sa.Column("source_plan_id", sa.String(36), nullable=True))
    table.append_column(sa.Column("source_plan_hash", sa.String(64), nullable=True))
    table.append_constraint(sa.ForeignKeyConstraint(
        ["source_plan_id", "project_id"],
        ["translation_source_plan.id", "translation_source_plan.project_id"],
        ondelete="RESTRICT",
        name="fk_translation_operation_source_plan_project",
    ))
    table.append_constraint(sa.CheckConstraint(
        "(source_plan_id IS NULL AND source_plan_hash IS NULL) OR "
        "(source_content_hash IS NOT NULL AND source_plan_id IS NOT NULL AND "
        "source_plan_hash IS NOT NULL AND length(source_content_hash) = 64 AND "
        "length(source_plan_hash) = 64)",
        name="ck_translation_operation_source_plan_pin",
    ))
    sa.Index("ix_translation_operation_source_plan", table.c.source_plan_id)
    return table


def upgrade():
    # The Phase-1 model predates a composite anchor identity.  This unique
    # index makes the new cross-plan occurrence FK enforceable on SQLite too.
    op.create_index("uq_translation_source_anchor_id_plan", "translation_source_anchor", ["id", "source_plan_id"], unique=True)
    op.create_index("uq_translation_source_plan_id_project", "translation_source_plan", ["id", "project_id"], unique=True)
    op.create_table("translation_semantic_census",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("project_id", sa.String(36), nullable=False), sa.Column("source_plan_id", sa.String(36), nullable=False), sa.Column("source_plan_hash", sa.String(64), nullable=False), sa.Column("profile_id", sa.String(64), nullable=False), sa.Column("profile_version", sa.String(64), nullable=False), sa.Column("occurrence_count", sa.Integer(), nullable=False), sa.Column("census_hash", sa.String(64), nullable=False), sa.Column("coverage_hash", sa.String(64), nullable=False), sa.Column("record_hash", sa.String(64), nullable=False), sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("id", "source_plan_id", name="uq_translation_semantic_census_id_plan"), sa.UniqueConstraint("source_plan_id", "profile_id", "profile_version", "census_hash", name="uq_translation_semantic_census_material"), sa.CheckConstraint("occurrence_count >= 0", name="ck_translation_semantic_census_count"), *_hash_checks("translation_semantic_census", (("source_plan_hash", "plan_hash"), ("census_hash", "hash"), ("coverage_hash", "coverage_hash"), ("record_hash", "record_hash"))), sa.ForeignKeyConstraint(["project_id"], ["translation_project.id"], ondelete="CASCADE", name="fk_translation_semantic_census_project"), sa.ForeignKeyConstraint(["source_plan_id"], ["translation_source_plan.id"], ondelete="CASCADE", name="fk_translation_semantic_census_plan"))
    op.create_index("ix_translation_semantic_census_plan_created", "translation_semantic_census", ["source_plan_id", "created_at"])
    op.create_table("translation_semantic_occurrence",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("census_id", sa.String(36), nullable=False), sa.Column("source_plan_id", sa.String(36), nullable=False), sa.Column("block_id", sa.String(128)), sa.Column("anchor_id", sa.String(128)), sa.Column("order_index", sa.Integer(), nullable=False), sa.Column("occurrence_kind", sa.String(64), nullable=False), sa.Column("slot_id", sa.String(128)), sa.Column("slot_kind", sa.String(32)), sa.Column("disposition", sa.String(64)), sa.Column("start_offset", sa.Integer()), sa.Column("end_offset", sa.Integer()), sa.Column("structural_json", sa.Text(), nullable=False), sa.Column("value_text", sa.Text(), nullable=False), sa.Column("value_hash", sa.String(64), nullable=False), sa.Column("occurrence_hash", sa.String(64), nullable=False),
        sa.UniqueConstraint("census_id", "order_index", name="uq_translation_semantic_occurrence_order"), sa.CheckConstraint("order_index >= 1", name="ck_translation_semantic_occurrence_order"), sa.CheckConstraint("start_offset IS NULL OR start_offset >= 0", name="ck_translation_semantic_occurrence_start"), sa.CheckConstraint("end_offset IS NULL OR (start_offset IS NOT NULL AND end_offset >= start_offset)", name="ck_translation_semantic_occurrence_end"), *_hash_checks("translation_semantic_occurrence", (("value_hash", "value_hash"), ("occurrence_hash", "hash"))), sa.ForeignKeyConstraint(["census_id", "source_plan_id"], ["translation_semantic_census.id", "translation_semantic_census.source_plan_id"], ondelete="CASCADE", name="fk_translation_semantic_occurrence_census_plan"), sa.ForeignKeyConstraint(["block_id", "source_plan_id"], ["translation_block.id", "translation_block.source_plan_id"], ondelete="CASCADE", name="fk_translation_semantic_occurrence_block_plan"), sa.ForeignKeyConstraint(["anchor_id", "source_plan_id"], ["translation_source_anchor.id", "translation_source_anchor.source_plan_id"], ondelete="CASCADE", name="fk_translation_semantic_occurrence_anchor_plan"))
    op.create_index("ix_translation_semantic_occurrence_plan_block", "translation_semantic_occurrence", ["source_plan_id", "block_id"])
    op.create_table("translation_context_artifact",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("project_id", sa.String(36), nullable=False), sa.Column("source_plan_id", sa.String(36), nullable=False), sa.Column("owner_user_id", sa.Integer(), nullable=False), sa.Column("artifact_kind", sa.String(32), nullable=False), sa.Column("created_at", sa.DateTime(), nullable=False), sa.UniqueConstraint("id", "project_id", "source_plan_id", name="uq_translation_context_artifact_scope"), sa.CheckConstraint("artifact_kind IN ('document_brief', 'style_guide', 'entity_register')", name="ck_translation_context_artifact_kind"), sa.ForeignKeyConstraint(["project_id"], ["translation_project.id"], ondelete="CASCADE", name="fk_translation_context_artifact_project"), sa.ForeignKeyConstraint(["source_plan_id"], ["translation_source_plan.id"], ondelete="CASCADE", name="fk_translation_context_artifact_plan"), sa.ForeignKeyConstraint(["owner_user_id"], ["user.id"], ondelete="CASCADE", name="fk_translation_context_artifact_owner"))
    op.create_index("ix_translation_context_artifact_plan_kind", "translation_context_artifact", ["source_plan_id", "artifact_kind"])
    op.create_table("translation_context_artifact_revision",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("artifact_id", sa.String(36), nullable=False), sa.Column("project_id", sa.String(36), nullable=False), sa.Column("source_plan_id", sa.String(36), nullable=False), sa.Column("revision_number", sa.Integer(), nullable=False), sa.Column("parent_revision_id", sa.String(36)), sa.Column("analysis_state", sa.String(16), nullable=False), sa.Column("schema_version", sa.String(64), nullable=False), sa.Column("profile_id", sa.String(64), nullable=False), sa.Column("profile_version", sa.String(64), nullable=False), sa.Column("source_plan_hash", sa.String(64), nullable=False), sa.Column("schema_hash", sa.String(64), nullable=False), sa.Column("profile_hash", sa.String(64), nullable=False), sa.Column("model_hash", sa.String(64), nullable=False), sa.Column("prompt_hash", sa.String(64), nullable=False), sa.Column("settings_hash", sa.String(64), nullable=False), sa.Column("input_hash", sa.String(64), nullable=False), sa.Column("output_hash", sa.String(64), nullable=False), sa.Column("evidence_hash", sa.String(64), nullable=False), sa.Column("record_hash", sa.String(64), nullable=False), sa.Column("model_json", sa.Text(), nullable=False), sa.Column("prompt_json", sa.Text(), nullable=False), sa.Column("settings_json", sa.Text(), nullable=False), sa.Column("input_json", sa.Text(), nullable=False), sa.Column("output_json", sa.Text(), nullable=False), sa.Column("evidence_json", sa.Text(), nullable=False), sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("artifact_id", "revision_number", name="uq_translation_context_artifact_revision_number"), sa.UniqueConstraint("id", "source_plan_id", name="uq_translation_context_artifact_revision_id_plan"), sa.UniqueConstraint("id", "project_id", "source_plan_id", name="uq_translation_context_artifact_revision_id_project_plan"), sa.UniqueConstraint("id", "artifact_id", "source_plan_id", name="uq_translation_context_artifact_revision_scope"), sa.CheckConstraint("revision_number >= 1", name="ck_translation_context_artifact_revision_number"), sa.CheckConstraint("analysis_state IN ('human', 'model', 'system')", name="ck_translation_context_artifact_revision_state"), *_hash_checks("translation_context_artifact_revision", (("source_plan_hash", "plan_hash"), ("schema_hash", "schema_hash"), ("profile_hash", "profile_hash"), ("model_hash", "model_hash"), ("prompt_hash", "prompt_hash"), ("settings_hash", "settings_hash"), ("input_hash", "input_hash"), ("output_hash", "output_hash"), ("evidence_hash", "evidence_hash"), ("record_hash", "record_hash"))), sa.ForeignKeyConstraint(["artifact_id", "project_id", "source_plan_id"], ["translation_context_artifact.id", "translation_context_artifact.project_id", "translation_context_artifact.source_plan_id"], ondelete="CASCADE", name="fk_translation_context_artifact_revision_artifact_scope"), sa.ForeignKeyConstraint(["parent_revision_id"], ["translation_context_artifact_revision.id"], ondelete="RESTRICT", name="fk_translation_context_artifact_revision_parent"))
    op.create_index("ix_translation_context_artifact_revision_plan_kind", "translation_context_artifact_revision", ["source_plan_id", "created_at"])
    op.create_table("translation_block_glossary_concept",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("project_id", sa.String(36), nullable=False), sa.Column("source_plan_id", sa.String(36), nullable=False), sa.Column("owner_user_id", sa.Integer(), nullable=False), sa.Column("concept_scope", sa.String(128), nullable=False), sa.Column("created_at", sa.DateTime(), nullable=False), sa.UniqueConstraint("id", "project_id", "source_plan_id", name="uq_translation_block_glossary_concept_scope"), sa.ForeignKeyConstraint(["project_id"], ["translation_project.id"], ondelete="CASCADE", name="fk_translation_block_glossary_concept_project"), sa.ForeignKeyConstraint(["source_plan_id"], ["translation_source_plan.id"], ondelete="CASCADE", name="fk_translation_block_glossary_concept_plan"), sa.ForeignKeyConstraint(["owner_user_id"], ["user.id"], ondelete="CASCADE", name="fk_translation_block_glossary_concept_owner"))
    op.create_index("ix_translation_block_glossary_concept_plan", "translation_block_glossary_concept", ["source_plan_id", "created_at"])
    op.create_table("translation_block_glossary_revision",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("concept_id", sa.String(36), nullable=False), sa.Column("project_id", sa.String(36), nullable=False), sa.Column("source_plan_id", sa.String(36), nullable=False), sa.Column("revision_number", sa.Integer(), nullable=False), sa.Column("parent_revision_id", sa.String(36)), sa.Column("approval_request_id", sa.String(36)), sa.Column("approved_by_user_id", sa.Integer()), sa.Column("approved_at", sa.DateTime()), sa.Column("approval_receipt", sa.String(64)), sa.Column("author_type", sa.String(16), nullable=False), sa.Column("revision_state", sa.String(16), nullable=False), sa.Column("normalization_profile", sa.String(64), nullable=False), sa.Column("normalization_version", sa.String(64), nullable=False), sa.Column("normalization_hash", sa.String(64), nullable=False), sa.Column("keyed_lookup_digest", sa.String(128), nullable=False), sa.Column("source_digest", sa.String(64), nullable=False), sa.Column("target_digest", sa.String(64), nullable=False), sa.Column("payload_hash", sa.String(64), nullable=False), sa.Column("record_hash", sa.String(64), nullable=False), sa.Column("payload_json", sa.Text(), nullable=False), sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("concept_id", "revision_number", name="uq_translation_block_glossary_revision_number"), sa.UniqueConstraint("concept_id", "id", name="uq_translation_block_glossary_revision_concept_id"), sa.UniqueConstraint("id", "source_plan_id", name="uq_translation_block_glossary_revision_id_plan"), sa.UniqueConstraint("id", "project_id", "source_plan_id", name="uq_translation_block_glossary_revision_id_project_plan"), sa.UniqueConstraint("id", "concept_id", "project_id", "source_plan_id", name="uq_translation_block_glossary_revision_scope"), sa.UniqueConstraint("approval_request_id", name="uq_translation_block_glossary_revision_approval"), sa.CheckConstraint("revision_number >= 1", name="ck_translation_block_glossary_revision_number"), sa.CheckConstraint("revision_state IN ('proposal', 'reviewed', 'locked')", name="ck_translation_block_glossary_revision_state"), sa.CheckConstraint("author_type IN ('human', 'model', 'system')", name="ck_translation_block_glossary_revision_author"), sa.CheckConstraint("NOT (revision_state = 'locked' AND author_type <> 'human')", name="ck_translation_block_glossary_revision_human_lock"), sa.CheckConstraint("(revision_state = 'locked' AND author_type = 'human' AND parent_revision_id IS NOT NULL AND approval_request_id IS NOT NULL AND approved_by_user_id IS NOT NULL AND approved_at IS NOT NULL AND approval_receipt IS NOT NULL) OR (revision_state <> 'locked' AND approval_request_id IS NULL AND approved_by_user_id IS NULL AND approved_at IS NULL AND approval_receipt IS NULL)", name="ck_translation_block_glossary_revision_authority_shape"), sa.CheckConstraint("approval_receipt IS NULL OR length(approval_receipt) = 64", name="ck_translation_block_glossary_revision_receipt"), *_hash_checks("translation_block_glossary_revision", tuple((name, name) for name in ("normalization_hash", "source_digest", "target_digest", "payload_hash", "record_hash"))), sa.ForeignKeyConstraint(["concept_id", "project_id", "source_plan_id"], ["translation_block_glossary_concept.id", "translation_block_glossary_concept.project_id", "translation_block_glossary_concept.source_plan_id"], ondelete="CASCADE", name="fk_translation_block_glossary_revision_concept_scope"), sa.ForeignKeyConstraint(["parent_revision_id"], ["translation_block_glossary_revision.id"], ondelete="RESTRICT", name="fk_translation_block_glossary_revision_parent"), sa.ForeignKeyConstraint(["approved_by_user_id"], ["user.id"], ondelete="RESTRICT", name="fk_translation_block_glossary_revision_approver"), sa.ForeignKeyConstraint(["approval_request_id"], ["runtime_approval_request.id"], ondelete="RESTRICT", name="fk_translation_block_glossary_revision_approval"))
    op.create_index("ix_translation_block_glossary_revision_lookup", "translation_block_glossary_revision", ["source_plan_id", "keyed_lookup_digest"])
    op.create_table("translation_block_glossary_active", sa.Column("concept_id", sa.String(36), primary_key=True), sa.Column("revision_id", sa.String(36), nullable=False), sa.Column("version", sa.Integer(), nullable=False, server_default="1"), sa.Column("updated_at", sa.DateTime(), nullable=False), sa.CheckConstraint("version >= 1", name="ck_translation_block_glossary_active_version"), sa.ForeignKeyConstraint(["concept_id", "revision_id"], ["translation_block_glossary_revision.concept_id", "translation_block_glossary_revision.id"], ondelete="RESTRICT", name="fk_translation_block_glossary_active_revision"))
    op.create_table("translation_block_glossary_occurrence", sa.Column("id", sa.String(36), primary_key=True), sa.Column("revision_id", sa.String(36), nullable=False), sa.Column("concept_id", sa.String(36), nullable=False), sa.Column("project_id", sa.String(36), nullable=False), sa.Column("source_plan_id", sa.String(36), nullable=False), sa.Column("block_id", sa.String(128), nullable=False), sa.Column("anchor_id", sa.String(128)), sa.Column("start_offset", sa.Integer(), nullable=False), sa.Column("end_offset", sa.Integer(), nullable=False), sa.Column("occurrence_hash", sa.String(64), nullable=False), sa.UniqueConstraint("revision_id", "block_id", "anchor_id", "start_offset", "end_offset", name="uq_translation_block_glossary_occurrence_exact"), sa.CheckConstraint("start_offset >= 0 AND end_offset > start_offset", name="ck_translation_block_glossary_occurrence_span"), sa.ForeignKeyConstraint(["revision_id", "concept_id", "project_id", "source_plan_id"], ["translation_block_glossary_revision.id", "translation_block_glossary_revision.concept_id", "translation_block_glossary_revision.project_id", "translation_block_glossary_revision.source_plan_id"], ondelete="CASCADE", name="fk_translation_block_glossary_occurrence_revision_scope"), sa.ForeignKeyConstraint(["block_id", "source_plan_id"], ["translation_block.id", "translation_block.source_plan_id"], ondelete="CASCADE", name="fk_translation_block_glossary_occurrence_block_plan"), sa.ForeignKeyConstraint(["anchor_id", "source_plan_id"], ["translation_source_anchor.id", "translation_source_anchor.source_plan_id"], ondelete="CASCADE", name="fk_translation_block_glossary_occurrence_anchor_plan"))
    op.create_index("ix_translation_block_glossary_occurrence_plan_block", "translation_block_glossary_occurrence", ["source_plan_id", "block_id"])
    op.create_index("uq_translation_block_glossary_occurrence_hash", "translation_block_glossary_occurrence", ["revision_id", "occurrence_hash"], unique=True)
    op.create_table("translation_context_baseline", sa.Column("id", sa.String(36), primary_key=True), sa.Column("operation_id", sa.String(36), nullable=False), sa.Column("project_id", sa.String(36), nullable=False), sa.Column("owner_user_id", sa.Integer(), nullable=False), sa.Column("source_plan_id", sa.String(36), nullable=False), sa.Column("source_plan_hash", sa.String(64), nullable=False), sa.Column("manifest_hash", sa.String(64), nullable=False), sa.Column("record_hash", sa.String(64), nullable=False), sa.Column("binding_attestation", sa.String(128), nullable=False), sa.Column("manifest_json", sa.Text(), nullable=False), sa.Column("created_at", sa.DateTime(), nullable=False), sa.UniqueConstraint("operation_id", name="uq_translation_context_baseline_operation"), sa.UniqueConstraint("id", "operation_id", "source_plan_id", name="uq_translation_context_baseline_scope"), *_hash_checks("translation_context_baseline", (("source_plan_hash", "plan_hash"), ("manifest_hash", "manifest_hash"), ("record_hash", "record_hash"))), sa.ForeignKeyConstraint(["operation_id"], ["translation_operation.id"], ondelete="CASCADE", name="fk_translation_context_baseline_operation"), sa.ForeignKeyConstraint(["source_plan_id"], ["translation_source_plan.id"], ondelete="CASCADE", name="fk_translation_context_baseline_plan"), sa.ForeignKeyConstraint(["project_id"], ["translation_project.id"], ondelete="CASCADE", name="fk_translation_context_baseline_project"), sa.ForeignKeyConstraint(["owner_user_id"], ["user.id"], ondelete="CASCADE", name="fk_translation_context_baseline_owner"))
    op.create_table("translation_context_baseline_item", sa.Column("id", sa.String(36), primary_key=True), sa.Column("baseline_id", sa.String(36), nullable=False), sa.Column("operation_id", sa.String(36), nullable=False), sa.Column("project_id", sa.String(36), nullable=False), sa.Column("source_plan_id", sa.String(36), nullable=False), sa.Column("order_index", sa.Integer(), nullable=False), sa.Column("item_kind", sa.String(32), nullable=False), sa.Column("artifact_revision_id", sa.String(36)), sa.Column("glossary_revision_id", sa.String(36)), sa.Column("revision_hash", sa.String(64), nullable=False), sa.Column("slot_state", sa.String(32), nullable=False, server_default="enabled"), sa.UniqueConstraint("baseline_id", "order_index", name="uq_translation_context_baseline_item_order"), sa.CheckConstraint("order_index >= 1", name="ck_translation_context_baseline_item_order"), sa.CheckConstraint("item_kind IN ('document_brief', 'style_guide', 'entity_register', 'glossary_locked', 'research_evidence', 'translation_memory')", name="ck_translation_context_baseline_item_kind"), sa.CheckConstraint("(item_kind IN ('document_brief', 'style_guide', 'entity_register') AND artifact_revision_id IS NOT NULL AND glossary_revision_id IS NULL) OR (item_kind = 'glossary_locked' AND artifact_revision_id IS NULL AND glossary_revision_id IS NOT NULL)", name="ck_translation_context_baseline_item_reference_kind"), *_hash_checks("translation_context_baseline_item", (("revision_hash", "revision_hash"),)), sa.ForeignKeyConstraint(["baseline_id", "operation_id", "source_plan_id"], ["translation_context_baseline.id", "translation_context_baseline.operation_id", "translation_context_baseline.source_plan_id"], ondelete="CASCADE", name="fk_translation_context_baseline_item_baseline_scope"), sa.ForeignKeyConstraint(["artifact_revision_id", "project_id", "source_plan_id"], ["translation_context_artifact_revision.id", "translation_context_artifact_revision.project_id", "translation_context_artifact_revision.source_plan_id"], ondelete="RESTRICT", name="fk_translation_context_baseline_item_artifact_scope"), sa.ForeignKeyConstraint(["glossary_revision_id", "project_id", "source_plan_id"], ["translation_block_glossary_revision.id", "translation_block_glossary_revision.project_id", "translation_block_glossary_revision.source_plan_id"], ondelete="RESTRICT", name="fk_translation_context_baseline_item_glossary_scope"))
    op.create_table("translation_context_impact", sa.Column("id", sa.String(36), primary_key=True), sa.Column("project_id", sa.String(36), nullable=False), sa.Column("source_plan_id", sa.String(36), nullable=False), sa.Column("block_id", sa.String(128), nullable=False), sa.Column("old_glossary_revision_id", sa.String(36)), sa.Column("new_glossary_revision_id", sa.String(36)), sa.Column("old_artifact_revision_id", sa.String(36)), sa.Column("new_artifact_revision_id", sa.String(36)), sa.Column("reason_json", sa.Text(), nullable=False), sa.Column("impact_state", sa.String(32), nullable=False), sa.Column("impact_key", sa.String(64), nullable=False), sa.Column("created_at", sa.DateTime(), nullable=False), sa.UniqueConstraint("source_plan_id", "impact_key", name="uq_translation_context_impact_key"), sa.CheckConstraint("impact_state IN ('needs_review', 'retranslation_candidate')", name="ck_translation_context_impact_state"), sa.ForeignKeyConstraint(["project_id"], ["translation_project.id"], ondelete="CASCADE", name="fk_translation_context_impact_project"), sa.ForeignKeyConstraint(["source_plan_id"], ["translation_source_plan.id"], ondelete="CASCADE", name="fk_translation_context_impact_plan"), sa.ForeignKeyConstraint(["block_id", "source_plan_id"], ["translation_block.id", "translation_block.source_plan_id"], ondelete="CASCADE", name="fk_translation_context_impact_block_plan"), sa.ForeignKeyConstraint(["old_glossary_revision_id", "project_id", "source_plan_id"], ["translation_block_glossary_revision.id", "translation_block_glossary_revision.project_id", "translation_block_glossary_revision.source_plan_id"], ondelete="RESTRICT", name="fk_translation_context_impact_old_glossary_scope"), sa.ForeignKeyConstraint(["new_glossary_revision_id", "project_id", "source_plan_id"], ["translation_block_glossary_revision.id", "translation_block_glossary_revision.project_id", "translation_block_glossary_revision.source_plan_id"], ondelete="RESTRICT", name="fk_translation_context_impact_new_glossary_scope"), sa.ForeignKeyConstraint(["old_artifact_revision_id", "project_id", "source_plan_id"], ["translation_context_artifact_revision.id", "translation_context_artifact_revision.project_id", "translation_context_artifact_revision.source_plan_id"], ondelete="RESTRICT", name="fk_translation_context_impact_old_artifact_scope"), sa.ForeignKeyConstraint(["new_artifact_revision_id", "project_id", "source_plan_id"], ["translation_context_artifact_revision.id", "translation_context_artifact_revision.project_id", "translation_context_artifact_revision.source_plan_id"], ondelete="RESTRICT", name="fk_translation_context_impact_new_artifact_scope"))
    op.create_index("ix_translation_context_impact_plan_block", "translation_context_impact", ["source_plan_id", "block_id"])

    # A controlled copy preserves exact DB enforcement without reflecting the
    # host's optional workflow tables through runtime-approval foreign keys.
    with op.batch_alter_table(
        "translation_operation", recreate="always", copy_from=_operation_before_plan_pin()
    ) as batch:
        batch.add_column(sa.Column("source_plan_id", sa.String(36), nullable=True))
        batch.add_column(sa.Column("source_plan_hash", sa.String(64), nullable=True))
        batch.create_foreign_key(
            "fk_translation_operation_source_plan_project", "translation_source_plan",
            ["source_plan_id", "project_id"], ["id", "project_id"], ondelete="RESTRICT",
        )
        batch.create_check_constraint(
            "ck_translation_operation_source_plan_pin",
            "(source_plan_id IS NULL AND source_plan_hash IS NULL) OR "
            "(source_content_hash IS NOT NULL AND source_plan_id IS NOT NULL AND "
            "source_plan_hash IS NOT NULL AND length(source_content_hash) = 64 AND "
            "length(source_plan_hash) = 64)",
        )
    op.create_index("ix_translation_operation_source_plan", "translation_operation", ["source_plan_id"])
    if op.get_bind().dialect.name == "sqlite":
        op.execute(
            """
            CREATE TRIGGER trg_translation_operation_source_pins_immutable
            BEFORE UPDATE OF source_content_hash, source_plan_id, source_plan_hash
            ON translation_operation
            FOR EACH ROW
            WHEN NEW.source_content_hash IS NOT OLD.source_content_hash
              OR NEW.source_plan_id IS NOT OLD.source_plan_id
              OR NEW.source_plan_hash IS NOT OLD.source_plan_hash
            BEGIN
                SELECT RAISE(ABORT, 'translation_operation source pins are immutable');
            END
            """
        )

    # These tables are new at 011.  Batch alteration nevertheless gives the
    # SQLite schema the same named authority/lineage constraints as metadata.


def downgrade():
    bind = op.get_bind()
    if bind.execute(
        sa.text(
            "SELECT 1 FROM translation_operation "
            "WHERE source_plan_id IS NOT NULL "
            "OR source_plan_hash IS NOT NULL LIMIT 1"
        )
    ).first() is not None:
        raise RuntimeError(
            "Translator migration 011 is forward-only while operation source "
            "pin evidence exists."
        )
    tables = ("translation_semantic_census", "translation_semantic_occurrence", "translation_context_artifact", "translation_context_artifact_revision", "translation_block_glossary_concept", "translation_block_glossary_revision", "translation_block_glossary_active", "translation_block_glossary_occurrence", "translation_context_baseline", "translation_context_baseline_item", "translation_context_impact")
    if any(bind.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first() is not None for table in tables):
        raise RuntimeError("Translator migration 011 is forward-only after context artifact evidence exists.")
    for table in reversed(tables):
        op.drop_table(table)
    with op.batch_alter_table(
        "translation_operation", recreate="always", copy_from=_operation_with_plan_pin()
    ) as batch:
        batch.drop_constraint("ck_translation_operation_source_plan_pin", type_="check")
        batch.drop_constraint("fk_translation_operation_source_plan_project", type_="foreignkey")
        batch.drop_index("ix_translation_operation_source_plan")
        batch.drop_column("source_plan_hash")
        batch.drop_column("source_plan_id")
    if bind.dialect.name == "sqlite":
        op.execute(
            "DROP TRIGGER IF EXISTS trg_translation_operation_source_pins_immutable"
        )
    op.drop_index("uq_translation_source_anchor_id_plan", table_name="translation_source_anchor")
    op.drop_index("uq_translation_source_plan_id_project", table_name="translation_source_plan")
