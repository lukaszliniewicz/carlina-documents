"""Add the durable host-owned MCP access-key and external-work ledger.

Revision ID: 20260901_mcp_external_jobs
Revises: 20260812_compass_client_auth
"""

import sqlalchemy as sa
from alembic import op

from iarf_assistant.database.types import EncryptedType

revision = "20260901_mcp_external_jobs"
down_revision = "20260812_compass_client_auth"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create protocol-independent MCP domain and work records."""

    op.create_table(
        "mcp_access_key",
        sa.Column("key_id", sa.String(length=36), nullable=False),
        sa.Column("owner_user_id", sa.Integer(), nullable=False),
        sa.Column(
            "label", EncryptedType(sa.String(length=255), strategy="user"), nullable=False
        ),
        sa.Column("token_digest", sa.String(length=128), nullable=False),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column(
            "allowed_roots", EncryptedType(sa.JSON(), strategy="user"), nullable=False
        ),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('active', 'revoked')", name="ck_mcp_access_key_status"
        ),
        sa.CheckConstraint(
            "(status = 'active' AND revoked_at IS NULL) "
            "OR (status = 'revoked' AND revoked_at IS NOT NULL)",
            name="ck_mcp_access_key_revocation",
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"], ["user.id"], ondelete="CASCADE",
            name="fk_mcp_access_key_owner",
        ),
        sa.PrimaryKeyConstraint("key_id"),
        sa.UniqueConstraint("token_digest"),
    )
    op.create_index(
        "ix_mcp_access_key_owner_created", "mcp_access_key", ["owner_user_id", "created_at"]
    )
    op.create_index("ix_mcp_access_key_token_digest", "mcp_access_key", ["token_digest"])
    op.create_index("ix_mcp_access_key_status", "mcp_access_key", ["status"])

    op.create_table(
        "mcp_execution",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("owner_user_id", sa.Integer(), nullable=False),
        sa.Column("addon_id", sa.String(length=32), nullable=False),
        sa.Column("resource_kind", sa.String(length=64), nullable=False),
        sa.Column("resource_id", sa.String(length=255), nullable=False),
        sa.Column("queue_job_id", sa.String(length=64), nullable=True),
        sa.Column("mode", sa.String(length=16), nullable=False, server_default="managed"),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="queued"),
        sa.Column(
            "request_json", EncryptedType(sa.JSON(), strategy="user"), nullable=False
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "addon_id IN ('doc_processor', 'translator')", name="ck_mcp_execution_addon"
        ),
        sa.CheckConstraint(
            "mode IN ('managed', 'passive')", name="ck_mcp_execution_mode"
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'waiting_external', 'waiting_review', "
            "'completed', 'failed', 'cancelled')",
            name="ck_mcp_execution_status",
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"], ["user.id"], ondelete="CASCADE",
            name="fk_mcp_execution_owner",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "owner_user_id", "addon_id", "queue_job_id",
            name="uq_mcp_execution_owner_addon_queue_job",
        ),
    )
    op.create_index(
        "ix_mcp_execution_owner_status_created",
        "mcp_execution",
        ["owner_user_id", "status", "created_at"],
    )
    op.create_index("ix_mcp_execution_addon_id", "mcp_execution", ["addon_id"])
    op.create_index("ix_mcp_execution_queue_job_id", "mcp_execution", ["queue_job_id"])
    op.create_index("ix_mcp_execution_mode", "mcp_execution", ["mode"])

    op.create_table(
        "mcp_work_item",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("execution_id", sa.String(length=36), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column(
            "request_json", EncryptedType(sa.JSON(), strategy="user"), nullable=False
        ),
        sa.Column(
            "accepted_response_json", EncryptedType(sa.JSON(), strategy="user"), nullable=True
        ),
        sa.Column("state", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("lease_token", sa.String(length=36), nullable=True),
        sa.Column("lease_owner", sa.String(length=255), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("leased_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "state IN ('pending', 'leased', 'completed', 'cancelled')",
            name="ck_mcp_work_item_state",
        ),
        sa.CheckConstraint(
            "version >= 0 AND attempt_count >= 0", name="ck_mcp_work_item_counters"
        ),
        sa.CheckConstraint(
            "(state = 'leased' AND lease_token IS NOT NULL AND lease_owner IS NOT NULL "
            "AND lease_expires_at IS NOT NULL) OR state != 'leased'",
            name="ck_mcp_work_item_lease_fields",
        ),
        sa.ForeignKeyConstraint(
            ["execution_id"], ["mcp_execution.id"], ondelete="CASCADE",
            name="fk_mcp_work_item_execution",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "execution_id", "request_fingerprint", name="uq_mcp_work_item_request"
        ),
    )
    op.create_index(
        "ix_mcp_work_item_state_created", "mcp_work_item", ["state", "created_at", "id"]
    )
    op.create_index("ix_mcp_work_item_execution_id", "mcp_work_item", ["execution_id"])
    op.create_index(
        "ix_mcp_work_item_request_fingerprint", "mcp_work_item", ["request_fingerprint"]
    )
    op.create_index("ix_mcp_work_item_lease_token", "mcp_work_item", ["lease_token"])
    op.create_index("ix_mcp_work_item_lease_owner", "mcp_work_item", ["lease_owner"])
    op.create_index(
        "ix_mcp_work_item_expired_lease",
        "mcp_work_item",
        ["lease_expires_at", "id"],
        sqlite_where=sa.text("state = 'leased' AND lease_expires_at IS NOT NULL"),
    )

    op.create_table(
        "mcp_work_submission",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("work_item_id", sa.String(length=36), nullable=False),
        sa.Column(
            "response_json", EncryptedType(sa.JSON(), strategy="user"), nullable=False
        ),
        sa.Column("response_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column(
            "error_detail", EncryptedType(sa.Text(), strategy="user"), nullable=True
        ),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('accepted', 'rejected', 'duplicate')",
            name="ck_mcp_work_submission_status",
        ),
        sa.CheckConstraint(
            "error_code IS NULL OR length(error_code) BETWEEN 1 AND 64",
            name="ck_mcp_work_submission_error_code",
        ),
        sa.ForeignKeyConstraint(
            ["work_item_id"], ["mcp_work_item.id"], ondelete="CASCADE",
            name="fk_mcp_work_submission_work_item",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_mcp_work_submission_item_submitted",
        "mcp_work_submission",
        ["work_item_id", "submitted_at", "id"],
    )
    op.create_index(
        "ix_mcp_work_submission_response_fingerprint",
        "mcp_work_submission",
        ["response_fingerprint"],
    )
    op.create_index(
        "ix_mcp_work_submission_status", "mcp_work_submission", ["status"]
    )


def downgrade() -> None:
    """Drop the MCP ledger in dependency order."""

    op.drop_index("ix_mcp_work_submission_status", table_name="mcp_work_submission")
    op.drop_index(
        "ix_mcp_work_submission_response_fingerprint", table_name="mcp_work_submission"
    )
    op.drop_index(
        "ix_mcp_work_submission_item_submitted", table_name="mcp_work_submission"
    )
    op.drop_table("mcp_work_submission")

    op.drop_index("ix_mcp_work_item_expired_lease", table_name="mcp_work_item")
    op.drop_index("ix_mcp_work_item_lease_owner", table_name="mcp_work_item")
    op.drop_index("ix_mcp_work_item_lease_token", table_name="mcp_work_item")
    op.drop_index(
        "ix_mcp_work_item_request_fingerprint", table_name="mcp_work_item"
    )
    op.drop_index("ix_mcp_work_item_execution_id", table_name="mcp_work_item")
    op.drop_index("ix_mcp_work_item_state_created", table_name="mcp_work_item")
    op.drop_table("mcp_work_item")

    op.drop_index("ix_mcp_execution_mode", table_name="mcp_execution")
    op.drop_index("ix_mcp_execution_queue_job_id", table_name="mcp_execution")
    op.drop_index("ix_mcp_execution_addon_id", table_name="mcp_execution")
    op.drop_index(
        "ix_mcp_execution_owner_status_created", table_name="mcp_execution"
    )
    op.drop_table("mcp_execution")

    op.drop_index("ix_mcp_access_key_status", table_name="mcp_access_key")
    op.drop_index("ix_mcp_access_key_token_digest", table_name="mcp_access_key")
    op.drop_index("ix_mcp_access_key_owner_created", table_name="mcp_access_key")
    op.drop_table("mcp_access_key")
