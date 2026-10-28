"""Add durable indexed MCP batch-dispatch projections.

Revision ID: 20260904_mcp_batch_dispatch
Revises: 20260901_mcp_external_jobs
"""

import sqlalchemy as sa
from alembic import op

from iarf_assistant.database.types import EncryptedType

revision = "20260904_mcp_batch_dispatch"
down_revision = "20260901_mcp_external_jobs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add nullable dispatch indexes without rewriting authoritative requests."""

    with op.batch_alter_table("mcp_work_item") as batch_op:
        batch_op.add_column(sa.Column("batch_id", sa.String(length=64), nullable=True))
        batch_op.add_column(
            sa.Column("dispatch_phase", sa.String(length=32), nullable=True)
        )
        batch_op.add_column(sa.Column("unit_index", sa.Integer(), nullable=True))
        batch_op.add_column(
            sa.Column(
                "is_batch_terminal_unit",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch_op.add_column(
            sa.Column("context_artifact_hash", sa.String(length=64), nullable=True)
        )
        batch_op.add_column(
            sa.Column(
                "boundary_input_json",
                EncryptedType(sa.JSON(), strategy="user"),
                nullable=True,
            )
        )
        batch_op.create_check_constraint(
            "ck_mcp_work_item_batch_id_length",
            "batch_id IS NULL OR length(batch_id) BETWEEN 1 AND 64",
        )
        batch_op.create_check_constraint(
            "ck_mcp_work_item_dispatch_phase_length",
            "dispatch_phase IS NULL OR length(dispatch_phase) BETWEEN 1 AND 32",
        )
        batch_op.create_check_constraint(
            "ck_mcp_work_item_unit_index_positive",
            "unit_index IS NULL OR unit_index > 0",
        )
        batch_op.create_check_constraint(
            "ck_mcp_work_item_context_artifact_hash",
            "context_artifact_hash IS NULL OR "
            "(length(context_artifact_hash) = 64 AND "
            "context_artifact_hash NOT GLOB '*[^0-9a-f]*')",
        )
        batch_op.create_check_constraint(
            "ck_mcp_work_item_terminal_dispatch",
            "is_batch_terminal_unit = false OR "
            "(batch_id IS NOT NULL AND dispatch_phase = 'page_extraction' "
            "AND unit_index IS NOT NULL)",
        )
        batch_op.create_index(
            "ix_mcp_work_item_batch_id", ["batch_id"], unique=False
        )
        batch_op.create_index(
            "ix_mcp_work_item_dispatch_phase", ["dispatch_phase"], unique=False
        )


def downgrade() -> None:
    """Remove batch projections while preserving the original work ledger."""

    with op.batch_alter_table("mcp_work_item") as batch_op:
        batch_op.drop_index("ix_mcp_work_item_dispatch_phase")
        batch_op.drop_index("ix_mcp_work_item_batch_id")
        batch_op.drop_constraint(
            "ck_mcp_work_item_terminal_dispatch", type_="check"
        )
        batch_op.drop_constraint(
            "ck_mcp_work_item_context_artifact_hash", type_="check"
        )
        batch_op.drop_constraint(
            "ck_mcp_work_item_unit_index_positive", type_="check"
        )
        batch_op.drop_constraint(
            "ck_mcp_work_item_dispatch_phase_length", type_="check"
        )
        batch_op.drop_constraint(
            "ck_mcp_work_item_batch_id_length", type_="check"
        )
        batch_op.drop_column("boundary_input_json")
        batch_op.drop_column("context_artifact_hash")
        batch_op.drop_column("is_batch_terminal_unit")
        batch_op.drop_column("unit_index")
        batch_op.drop_column("dispatch_phase")
        batch_op.drop_column("batch_id")
