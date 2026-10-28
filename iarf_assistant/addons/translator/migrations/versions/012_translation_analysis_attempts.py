"""Add durable, provider-independent Translator analysis attempt evidence.

Revision ID: 012_translation_analysis_attempts
Revises: 011_context_artifacts
"""

import sqlalchemy as sa
from alembic import op

revision = "012_analysis_attempts"
down_revision = "011_context_artifacts"
branch_labels = None
depends_on = None


def _hash(field: str, name: str | None = None) -> sa.CheckConstraint:
    return sa.CheckConstraint(
        f"length({field}) = 64",
        name=f"ck_translation_analysis_attempt_{name or field}",
    )


def upgrade():
    # Required by the scoped operation FK below.  The primary key remains the
    # identity; this index only makes the owner/project witness enforceable.
    op.create_index(
        "uq_translation_operation_id_project",
        "translation_operation",
        ["id", "project_id"],
        unique=True,
    )
    op.create_table(
        "translation_analysis_attempt",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), nullable=False),
        sa.Column("owner_user_id", sa.Integer(), nullable=False),
        sa.Column("operation_id", sa.String(36), nullable=False),
        sa.Column("source_plan_id", sa.String(36), nullable=False),
        sa.Column("analysis_kind", sa.String(32), nullable=False),
        sa.Column("contract_version", sa.String(64), nullable=False),
        sa.Column("source_plan_hash", sa.String(64), nullable=False),
        sa.Column("source_content_hash", sa.String(64), nullable=False),
        sa.Column("operation_version", sa.Integer(), nullable=False),
        sa.Column("queue_job_id", sa.String(128), nullable=False),
        sa.Column("operation_lease_expires_at", sa.DateTime(), nullable=False),
        sa.Column("lease_fence", sa.String(64), nullable=False),
        sa.Column("packet_json", sa.Text(), nullable=False),
        sa.Column("model_json", sa.Text(), nullable=False),
        sa.Column("profile_json", sa.Text(), nullable=False),
        sa.Column("settings_json", sa.Text(), nullable=False),
        sa.Column("budget_json", sa.Text(), nullable=False),
        sa.Column("tool_declarations_json", sa.Text(), nullable=False),
        sa.Column("packet_hash", sa.String(64), nullable=False),
        sa.Column("model_hash", sa.String(64), nullable=False),
        sa.Column("profile_hash", sa.String(64), nullable=False),
        sa.Column("settings_hash", sa.String(64), nullable=False),
        sa.Column("budget_hash", sa.String(64), nullable=False),
        sa.Column("tools_hash", sa.String(64), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("retry_of_attempt_id", sa.String(36)),
        sa.Column("retry_of_attempt_hash", sa.String(64)),
        sa.Column("rebase_of_attempt_id", sa.String(36)),
        sa.Column("rebase_of_attempt_hash", sa.String(64)),
        sa.Column("attempt_record_hash", sa.String(64), nullable=False),
        sa.Column("dispatch_receipt_hash", sa.String(64), nullable=False),
        sa.Column(
            "dispatch_state", sa.String(32), nullable=False, server_default="prepared"
        ),
        sa.Column("dispatch_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("dispatch_claimed_at", sa.DateTime()),
        sa.Column("dispatch_finished_at", sa.DateTime()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "id", "source_plan_id", name="uq_translation_analysis_attempt_id_plan"
        ),
        sa.CheckConstraint(
            "analysis_kind IN ('document_brief', 'style_guide', 'entity_register', 'terminology_consolidation', 'research', 'embedding_projection', 'memory_derivation')",
            name="ck_translation_analysis_attempt_kind",
        ),
        sa.CheckConstraint(
            "contract_version = 'analysis-attempt-v1'",
            name="ck_translation_analysis_attempt_contract_version",
        ),
        sa.CheckConstraint(
            "operation_version >= 1",
            name="ck_translation_analysis_attempt_operation_version",
        ),
        sa.CheckConstraint(
            "dispatch_version >= 1",
            name="ck_translation_analysis_attempt_dispatch_version",
        ),
        sa.CheckConstraint(
            "dispatch_state IN ('prepared', 'dispatched', 'response_received', 'completed', 'provider_failed', 'provider_outcome_uncertain', 'fence_failed', 'cancelled', 'failed')",
            name="ck_translation_analysis_attempt_dispatch_state",
        ),
        sa.CheckConstraint(
            "(retry_of_attempt_id IS NULL AND retry_of_attempt_hash IS NULL) OR (retry_of_attempt_id IS NOT NULL AND retry_of_attempt_hash IS NOT NULL)",
            name="ck_translation_analysis_attempt_retry_witness",
        ),
        sa.CheckConstraint(
            "(rebase_of_attempt_id IS NULL AND rebase_of_attempt_hash IS NULL) OR (rebase_of_attempt_id IS NOT NULL AND rebase_of_attempt_hash IS NOT NULL)",
            name="ck_translation_analysis_attempt_rebase_witness",
        ),
        sa.CheckConstraint(
            "retry_of_attempt_id IS NULL OR rebase_of_attempt_id IS NULL",
            name="ck_translation_analysis_attempt_single_lineage",
        ),
        *[
            _hash(field, name)
            for field, name in (
                ("source_plan_hash", "plan_hash"),
                ("source_content_hash", "source_hash"),
                ("lease_fence", "lease_fence"),
                ("packet_hash", "packet_hash"),
                ("model_hash", "model_hash"),
                ("profile_hash", "profile_hash"),
                ("settings_hash", "settings_hash"),
                ("budget_hash", "budget_hash"),
                ("tools_hash", "tools_hash"),
                ("request_hash", "request_hash"),
                ("attempt_record_hash", "record_hash"),
                ("dispatch_receipt_hash", "dispatch_receipt_hash"),
            )
        ],
        sa.CheckConstraint(
            "retry_of_attempt_hash IS NULL OR length(retry_of_attempt_hash) = 64",
            name="ck_translation_analysis_attempt_retry_hash",
        ),
        sa.CheckConstraint(
            "rebase_of_attempt_hash IS NULL OR length(rebase_of_attempt_hash) = 64",
            name="ck_translation_analysis_attempt_rebase_hash",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["translation_project.id"],
            ondelete="CASCADE",
            name="fk_translation_analysis_attempt_project",
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["user.id"],
            ondelete="CASCADE",
            name="fk_translation_analysis_attempt_owner",
        ),
        sa.ForeignKeyConstraint(
            ["operation_id", "project_id"],
            ["translation_operation.id", "translation_operation.project_id"],
            ondelete="CASCADE",
            name="fk_translation_analysis_attempt_operation_scope",
        ),
        sa.ForeignKeyConstraint(
            ["source_plan_id", "project_id"],
            ["translation_source_plan.id", "translation_source_plan.project_id"],
            ondelete="CASCADE",
            name="fk_translation_analysis_attempt_plan_scope",
        ),
        sa.ForeignKeyConstraint(
            ["retry_of_attempt_id"],
            ["translation_analysis_attempt.id"],
            ondelete="RESTRICT",
            name="fk_translation_analysis_attempt_retry",
        ),
        sa.ForeignKeyConstraint(
            ["rebase_of_attempt_id"],
            ["translation_analysis_attempt.id"],
            ondelete="RESTRICT",
            name="fk_translation_analysis_attempt_rebase",
        ),
    )
    op.create_index(
        "ix_translation_analysis_attempt_operation_created",
        "translation_analysis_attempt",
        ["operation_id", "created_at"],
    )
    op.create_index(
        "ix_translation_analysis_attempt_plan_kind_created",
        "translation_analysis_attempt",
        ["source_plan_id", "analysis_kind", "created_at"],
    )
    op.create_table(
        "translation_analysis_attempt_event",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("attempt_id", sa.String(36), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("predecessor_event_hash", sa.String(64)),
        sa.Column("event_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "attempt_id",
            "sequence",
            name="uq_translation_analysis_attempt_event_sequence",
        ),
        sa.CheckConstraint(
            "sequence >= 1", name="ck_translation_analysis_attempt_event_sequence"
        ),
        sa.CheckConstraint(
            "event_type IN ('request_prepared', 'provider_dispatched', 'provider_response', 'provider_failure', 'provider_outcome_uncertain', 'post_response_fence_failed', 'cancelled', 'completed', 'failed')",
            name="ck_translation_analysis_attempt_event_type",
        ),
        sa.CheckConstraint(
            "length(payload_hash) = 64",
            name="ck_translation_analysis_attempt_event_payload_hash",
        ),
        sa.CheckConstraint(
            "length(event_hash) = 64", name="ck_translation_analysis_attempt_event_hash"
        ),
        sa.CheckConstraint(
            "predecessor_event_hash IS NULL OR length(predecessor_event_hash) = 64",
            name="ck_translation_analysis_attempt_event_predecessor_hash",
        ),
        sa.ForeignKeyConstraint(
            ["attempt_id"],
            ["translation_analysis_attempt.id"],
            ondelete="CASCADE",
            name="fk_translation_analysis_attempt_event_attempt",
        ),
    )
    op.create_index(
        "ix_translation_analysis_attempt_event_attempt_created",
        "translation_analysis_attempt_event",
        ["attempt_id", "created_at"],
    )


def downgrade():
    bind = op.get_bind()
    for table in ("translation_analysis_attempt_event", "translation_analysis_attempt"):
        if bind.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first() is not None:
            raise RuntimeError(
                "Translator migration 012 is forward-only after analysis attempt evidence exists."
            )
    op.drop_table("translation_analysis_attempt_event")
    op.drop_table("translation_analysis_attempt")
    op.drop_index(
        "uq_translation_operation_id_project", table_name="translation_operation"
    )
