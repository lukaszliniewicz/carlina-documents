"""add shared execution cost attribution

Revision ID: 20260724_execution_kernel
Revises: 20260724_runtime_interactions
Create Date: 2026-07-24 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "20260724_execution_kernel"
down_revision = "20260724_runtime_interactions"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("llm_cost", schema=None) as batch_op:
        batch_op.add_column(sa.Column("workflow_run_id", sa.String(length=36), nullable=True))
        batch_op.add_column(sa.Column("workflow_step_id", sa.String(length=36), nullable=True))
        batch_op.add_column(sa.Column("runtime_job_id", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("turn_id", sa.String(length=36), nullable=True))
        batch_op.add_column(sa.Column("action_id", sa.String(length=64), nullable=True))
        batch_op.create_foreign_key(
            "fk_llm_cost_workflow_run_id_workflow_run",
            "workflow_run",
            ["workflow_run_id"],
            ["id"],
        )
        batch_op.create_foreign_key(
            "fk_llm_cost_workflow_step_id_workflow_step",
            "workflow_step",
            ["workflow_step_id"],
            ["id"],
        )
        batch_op.create_foreign_key(
            "fk_llm_cost_runtime_job_id_runtime_job",
            "runtime_job",
            ["runtime_job_id"],
            ["id"],
        )
        batch_op.create_index(
            batch_op.f("ix_llm_cost_workflow_run_id"),
            ["workflow_run_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_llm_cost_workflow_step_id"),
            ["workflow_step_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_llm_cost_runtime_job_id"),
            ["runtime_job_id"],
            unique=False,
        )
        batch_op.create_index(batch_op.f("ix_llm_cost_turn_id"), ["turn_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_llm_cost_action_id"), ["action_id"], unique=False)


def downgrade():
    with op.batch_alter_table("llm_cost", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_llm_cost_action_id"))
        batch_op.drop_index(batch_op.f("ix_llm_cost_turn_id"))
        batch_op.drop_index(batch_op.f("ix_llm_cost_runtime_job_id"))
        batch_op.drop_index(batch_op.f("ix_llm_cost_workflow_step_id"))
        batch_op.drop_index(batch_op.f("ix_llm_cost_workflow_run_id"))
        batch_op.drop_constraint(
            "fk_llm_cost_runtime_job_id_runtime_job",
            type_="foreignkey",
        )
        batch_op.drop_constraint(
            "fk_llm_cost_workflow_step_id_workflow_step",
            type_="foreignkey",
        )
        batch_op.drop_constraint(
            "fk_llm_cost_workflow_run_id_workflow_run",
            type_="foreignkey",
        )
        batch_op.drop_column("action_id")
        batch_op.drop_column("turn_id")
        batch_op.drop_column("runtime_job_id")
        batch_op.drop_column("workflow_step_id")
        batch_op.drop_column("workflow_run_id")
