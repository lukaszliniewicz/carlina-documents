"""Add backend hot-path indexes and keyed collection lookups.

Revision ID: 20260731_backend_performance
Revises: 20260731_addon_api_v1
"""

from alembic import op
import sqlalchemy as sa


revision = "20260731_backend_performance"
down_revision = "20260731_addon_api_v1"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("runtime_approval_request") as batch_op:
        batch_op.add_column(
            sa.Column("chat_session_id", sa.String(length=36), nullable=True)
        )

    with op.batch_alter_table("runtime_collection_item") as batch_op:
        batch_op.add_column(
            sa.Column("source_lookup_hash", sa.String(length=96), nullable=True)
        )
        batch_op.add_column(
            sa.Column("external_lookup_hash", sa.String(length=96), nullable=True)
        )

    op.create_index(
        "ix_workflow_run_owner_updated_id",
        "workflow_run",
        ["created_by_user_id", "updated_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_workflow_step_run_created_id",
        "workflow_step",
        ["workflow_run_id", "created_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_workflow_step_status_sequence_created_id",
        "workflow_step",
        ["status", "sequence", "created_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_runtime_approval_user_status_created_id",
        "runtime_approval_request",
        ["requester_user_id", "status", "created_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_runtime_approval_chat_scope",
        "runtime_approval_request",
        [
            "requester_user_id",
            "status",
            "subject_type",
            "chat_session_id",
            "created_at",
            "id",
        ],
        unique=False,
    )
    op.create_index(
        "ix_runtime_artifact_run_created_id",
        "runtime_artifact",
        ["workflow_run_id", "created_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_runtime_collection_run_created_id",
        "runtime_collection",
        ["workflow_run_id", "created_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_collection_item_collection_status_created_key",
        "runtime_collection_item",
        ["collection_id", "status", "created_at", "item_key"],
        unique=False,
    )
    op.create_index(
        "ix_collection_item_collection_source_lookup",
        "runtime_collection_item",
        ["collection_id", "source_lookup_hash"],
        unique=False,
    )
    op.create_index(
        "ix_collection_item_collection_external_lookup",
        "runtime_collection_item",
        ["collection_id", "external_lookup_hash"],
        unique=False,
    )
    op.create_index(
        "ix_collection_item_expired_lease",
        "runtime_collection_item",
        ["collection_id", "lease_expires_at"],
        unique=False,
        sqlite_where=sa.text("status = 'leased' AND lease_expires_at IS NOT NULL"),
    )


def downgrade():
    op.drop_index(
        "ix_collection_item_expired_lease",
        table_name="runtime_collection_item",
    )
    op.drop_index(
        "ix_collection_item_collection_external_lookup",
        table_name="runtime_collection_item",
    )
    op.drop_index(
        "ix_collection_item_collection_source_lookup",
        table_name="runtime_collection_item",
    )
    op.drop_index(
        "ix_collection_item_collection_status_created_key",
        table_name="runtime_collection_item",
    )
    op.drop_index(
        "ix_runtime_collection_run_created_id",
        table_name="runtime_collection",
    )
    op.drop_index(
        "ix_runtime_artifact_run_created_id",
        table_name="runtime_artifact",
    )
    op.drop_index(
        "ix_runtime_approval_chat_scope",
        table_name="runtime_approval_request",
    )
    op.drop_index(
        "ix_runtime_approval_user_status_created_id",
        table_name="runtime_approval_request",
    )
    op.drop_index(
        "ix_workflow_step_status_sequence_created_id",
        table_name="workflow_step",
    )
    op.drop_index(
        "ix_workflow_step_run_created_id",
        table_name="workflow_step",
    )
    op.drop_index(
        "ix_workflow_run_owner_updated_id",
        table_name="workflow_run",
    )
    with op.batch_alter_table("runtime_collection_item") as batch_op:
        batch_op.drop_column("external_lookup_hash")
        batch_op.drop_column("source_lookup_hash")
    with op.batch_alter_table("runtime_approval_request") as batch_op:
        batch_op.drop_column("chat_session_id")
