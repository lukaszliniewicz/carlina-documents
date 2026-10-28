"""Add immutable document-block translation window evidence.

Revision ID: 009_translation_window_attempts
Revises: 008_translation_target_revisions
"""

import sqlalchemy as sa
from alembic import op

revision = "009_translation_window_attempts"
down_revision = "008_translation_target_revisions"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "translation_window_attempt",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("owner_user_id", sa.Integer(), nullable=False),
        sa.Column("operation_id", sa.String(length=36), nullable=False),
        sa.Column("source_plan_id", sa.String(length=36), nullable=False),
        sa.Column("source_plan_hash", sa.String(length=64), nullable=False),
        sa.Column("window_order", sa.Integer(), nullable=False),
        sa.Column("packet_json", sa.Text(), nullable=False),
        sa.Column("block_ids_json", sa.Text(), nullable=False),
        sa.Column("block_fingerprints_json", sa.Text(), nullable=False),
        sa.Column("predecessor_json", sa.Text(), nullable=False),
        sa.Column("model_profile_json", sa.Text(), nullable=False),
        sa.Column("admission_json", sa.Text(), nullable=False),
        sa.Column("context_manifest_json", sa.Text(), nullable=False),
        sa.Column("packet_hash", sa.String(length=64), nullable=False),
        sa.Column("prompt_hash", sa.String(length=64), nullable=False),
        sa.Column("settings_hash", sa.String(length=64), nullable=False),
        sa.Column("context_hash", sa.String(length=64), nullable=False),
        sa.Column("context_manifest_hash", sa.String(length=64), nullable=False),
        sa.Column("model_profile_hash", sa.String(length=64), nullable=False),
        sa.Column("predecessor_hash", sa.String(length=64), nullable=False),
        sa.Column("retry_of_attempt_id", sa.String(length=36), nullable=True),
        sa.Column("retry_of_attempt_hash", sa.String(length=64), nullable=True),
        sa.Column("rebase_of_attempt_id", sa.String(length=36), nullable=True),
        sa.Column("rebase_of_attempt_hash", sa.String(length=64), nullable=True),
        sa.Column("attempt_record_hash", sa.String(length=64), nullable=False),
        sa.Column("dispatch_state", sa.String(length=32), nullable=False, server_default="prepared"),
        sa.Column("dispatch_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("dispatch_claimed_at", sa.DateTime(), nullable=True),
        sa.Column("dispatch_finished_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_translation_window_attempt"),
        sa.UniqueConstraint(
            "id",
            "source_plan_id",
            name="uq_translation_window_attempt_id_plan",
        ),
        sa.ForeignKeyConstraint(["project_id"], ["translation_project.id"], ondelete="CASCADE", name="fk_translation_window_attempt_project"),
        sa.ForeignKeyConstraint(["owner_user_id"], ["user.id"], ondelete="CASCADE", name="fk_translation_window_attempt_owner"),
        sa.ForeignKeyConstraint(["operation_id"], ["translation_operation.id"], ondelete="CASCADE", name="fk_translation_window_attempt_operation"),
        sa.ForeignKeyConstraint(["source_plan_id"], ["translation_source_plan.id"], ondelete="CASCADE", name="fk_translation_window_attempt_plan"),
        sa.ForeignKeyConstraint(["retry_of_attempt_id"], ["translation_window_attempt.id"], ondelete="RESTRICT", name="fk_translation_window_attempt_retry"),
        sa.ForeignKeyConstraint(["rebase_of_attempt_id"], ["translation_window_attempt.id"], ondelete="RESTRICT", name="fk_translation_window_attempt_rebase"),
        sa.CheckConstraint("window_order >= 1", name="ck_translation_window_attempt_order"),
        sa.CheckConstraint("length(source_plan_hash) = 64", name="ck_translation_window_attempt_plan_hash"),
        *[sa.CheckConstraint(f"length({column}) = 64", name=f"ck_translation_window_attempt_{name}") for column, name in (
            ("packet_hash", "packet_hash"), ("prompt_hash", "prompt_hash"), ("settings_hash", "settings_hash"),
            ("context_hash", "context_hash"), ("model_profile_hash", "model_hash"), ("predecessor_hash", "predecessor_hash"),
            ("context_manifest_hash", "context_manifest_hash"),
            ("attempt_record_hash", "record_hash"),
        )],
        sa.CheckConstraint("retry_of_attempt_hash IS NULL OR length(retry_of_attempt_hash) = 64", name="ck_translation_window_attempt_retry_hash"),
        sa.CheckConstraint("rebase_of_attempt_hash IS NULL OR length(rebase_of_attempt_hash) = 64", name="ck_translation_window_attempt_rebase_hash"),
        sa.CheckConstraint("(retry_of_attempt_id IS NULL AND retry_of_attempt_hash IS NULL) OR (retry_of_attempt_id IS NOT NULL AND retry_of_attempt_hash IS NOT NULL)", name="ck_translation_window_attempt_retry_witness"),
        sa.CheckConstraint("(rebase_of_attempt_id IS NULL AND rebase_of_attempt_hash IS NULL) OR (rebase_of_attempt_id IS NOT NULL AND rebase_of_attempt_hash IS NOT NULL)", name="ck_translation_window_attempt_rebase_witness"),
        sa.CheckConstraint("retry_of_attempt_id IS NULL OR rebase_of_attempt_id IS NULL", name="ck_translation_window_attempt_single_lineage"),
        sa.CheckConstraint("dispatch_state IN ('prepared', 'dispatched', 'response_received', 'provider_failed', 'normalization_failed', 'fence_failed', 'staging_failed', 'staged')", name="ck_translation_window_attempt_dispatch_state"),
        sa.CheckConstraint("dispatch_version >= 1", name="ck_translation_window_attempt_dispatch_version"),
    )
    op.create_index("ix_translation_window_attempt_operation_created", "translation_window_attempt", ["operation_id", "created_at"])
    op.create_index("ix_translation_window_attempt_plan_created", "translation_window_attempt", ["source_plan_id", "created_at"])
    op.create_table(
        "translation_window_attempt_event",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("attempt_id", sa.String(length=36), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("predecessor_event_hash", sa.String(length=64), nullable=True),
        sa.Column("event_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_translation_window_attempt_event"),
        sa.UniqueConstraint("attempt_id", "sequence", name="uq_translation_window_attempt_event_sequence"),
        sa.ForeignKeyConstraint(["attempt_id"], ["translation_window_attempt.id"], ondelete="CASCADE", name="fk_translation_window_attempt_event_attempt"),
        sa.CheckConstraint("sequence >= 1", name="ck_translation_window_attempt_event_sequence"),
        sa.CheckConstraint("length(payload_hash) = 64", name="ck_translation_window_attempt_event_payload_hash"),
        sa.CheckConstraint("length(event_hash) = 64", name="ck_translation_window_attempt_event_hash"),
        sa.CheckConstraint("predecessor_event_hash IS NULL OR length(predecessor_event_hash) = 64", name="ck_translation_window_attempt_event_predecessor_hash"),
    )
    op.create_index("ix_translation_window_attempt_event_attempt_created", "translation_window_attempt_event", ["attempt_id", "created_at"])
    op.create_table(
        "translation_window_attempt_revision",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("attempt_id", sa.String(length=36), nullable=False),
        sa.Column("source_plan_id", sa.String(length=36), nullable=False),
        sa.Column("block_id", sa.String(length=128), nullable=False),
        sa.Column("revision_id", sa.String(length=36), nullable=False),
        sa.Column("candidate_hash", sa.String(length=64), nullable=False),
        sa.Column("revision_record_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_translation_window_attempt_revision"),
        sa.UniqueConstraint("attempt_id", "block_id", name="uq_translation_window_attempt_revision_block"),
        sa.UniqueConstraint("revision_id", name="uq_translation_window_attempt_revision_revision"),
        sa.ForeignKeyConstraint(
            ["attempt_id", "source_plan_id"],
            ["translation_window_attempt.id", "translation_window_attempt.source_plan_id"],
            ondelete="CASCADE",
            name="fk_translation_window_attempt_revision_attempt_plan",
        ),
        sa.ForeignKeyConstraint(["block_id", "source_plan_id"], ["translation_block.id", "translation_block.source_plan_id"], ondelete="CASCADE", name="fk_translation_window_attempt_revision_block_plan"),
        sa.ForeignKeyConstraint(
            ["revision_id", "source_plan_id", "block_id"],
            [
                "translation_target_block_revision.id",
                "translation_target_block_revision.source_plan_id",
                "translation_target_block_revision.block_id",
            ],
            ondelete="RESTRICT",
            name="fk_translation_window_attempt_revision_revision_plan_block",
        ),
        sa.CheckConstraint("length(candidate_hash) = 64", name="ck_translation_window_attempt_revision_candidate_hash"),
        sa.CheckConstraint("length(revision_record_hash) = 64", name="ck_translation_window_attempt_revision_record_hash"),
    )
    op.create_index("ix_translation_window_attempt_revision_plan", "translation_window_attempt_revision", ["source_plan_id", "block_id"])


def downgrade():
    bind = op.get_bind()
    if any(bind.execute(sa.text(statement)).first() is not None for statement in (
        "SELECT 1 FROM translation_window_attempt LIMIT 1",
        "SELECT 1 FROM translation_window_attempt_event LIMIT 1",
        "SELECT 1 FROM translation_window_attempt_revision LIMIT 1",
    )):
        raise RuntimeError("Translator migration 009 is forward-only after window attempt evidence exists.")
    op.drop_index("ix_translation_window_attempt_revision_plan", table_name="translation_window_attempt_revision")
    op.drop_table("translation_window_attempt_revision")
    op.drop_index("ix_translation_window_attempt_event_attempt_created", table_name="translation_window_attempt_event")
    op.drop_table("translation_window_attempt_event")
    op.drop_index("ix_translation_window_attempt_plan_created", table_name="translation_window_attempt")
    op.drop_index("ix_translation_window_attempt_operation_created", table_name="translation_window_attempt")
    op.drop_table("translation_window_attempt")
