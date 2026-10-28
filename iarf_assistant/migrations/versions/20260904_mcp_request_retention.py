"""Add terminal MCP request-retention markers.

Revision ID: 20260904_mcp_request_retention
Revises: 20260904_mcp_batch_dispatch

Terminal payload redaction is forward-only data behavior even though schema
downgrade is supported.
"""

import sqlalchemy as sa
from alembic import op

revision = "20260904_mcp_request_retention"
down_revision = "20260904_mcp_batch_dispatch"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add a nullable timestamp without rebuilding the work-item table."""

    op.add_column(
        "mcp_work_item",
        sa.Column("request_pruned_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    """Remove only the retention marker column."""

    op.drop_column("mcp_work_item", "request_pruned_at")
