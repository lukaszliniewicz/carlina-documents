"""Add keyed, content-free MCP work-claim receipts.

Revision ID: 20260920_mcp_claim_receipts
Revises: 20260904_mcp_request_retention
"""

import sqlalchemy as sa
from alembic import op

revision = "20260920_mcp_claim_receipts"
down_revision = "20260904_mcp_request_retention"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the additive claim-receipt ledger."""

    op.create_table(
        "mcp_work_claim_receipt",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("execution_id", sa.String(length=36), nullable=False),
        sa.Column("key_id", sa.String(length=36), nullable=False),
        sa.Column("request_id", sa.String(length=128), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("work_item_id", sa.String(length=36), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "attempt_count > 0",
            name="ck_mcp_work_claim_receipt_attempt_positive",
        ),
        sa.ForeignKeyConstraint(
            ["execution_id"], ["mcp_execution.id"], ondelete="CASCADE",
            name="fk_mcp_work_claim_receipt_execution",
        ),
        sa.ForeignKeyConstraint(
            ["key_id"], ["mcp_access_key.key_id"], ondelete="CASCADE",
            name="fk_mcp_work_claim_receipt_key",
        ),
        sa.ForeignKeyConstraint(
            ["work_item_id"], ["mcp_work_item.id"], ondelete="CASCADE",
            name="fk_mcp_work_claim_receipt_work_item",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "execution_id", "key_id", "request_id",
            name="uq_mcp_work_claim_receipt_request",
        ),
    )
    op.create_index(
        "ix_mcp_work_claim_receipt_execution_id",
        "mcp_work_claim_receipt",
        ["execution_id"],
    )
    op.create_index(
        "ix_mcp_work_claim_receipt_key_id",
        "mcp_work_claim_receipt",
        ["key_id"],
    )
    op.create_index(
        "ix_mcp_work_claim_receipt_work_item_id",
        "mcp_work_claim_receipt",
        ["work_item_id"],
    )


def downgrade() -> None:
    """Drop only the claim-receipt table."""

    op.drop_index(
        "ix_mcp_work_claim_receipt_work_item_id",
        table_name="mcp_work_claim_receipt",
    )
    op.drop_index(
        "ix_mcp_work_claim_receipt_key_id",
        table_name="mcp_work_claim_receipt",
    )
    op.drop_index(
        "ix_mcp_work_claim_receipt_execution_id",
        table_name="mcp_work_claim_receipt",
    )
    op.drop_table("mcp_work_claim_receipt")
