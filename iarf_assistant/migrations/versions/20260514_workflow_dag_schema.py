"""add workflow DAG dependency and schema fields

Revision ID: 20260514_workflow_dag_schema
Revises: 1549076a28cb
Create Date: 2026-05-14 00:00:04.000000

"""
from alembic import op
import sqlalchemy as sa


revision = '20260514_workflow_dag_schema'
down_revision = '1549076a28cb'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('workflow_step', schema=None) as batch_op:
        batch_op.add_column(sa.Column('depends_on_step_ids', sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column('instructions', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('skill_ids', sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column('input_schema', sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column('output_schema', sa.JSON(), nullable=True))


def downgrade():
    with op.batch_alter_table('workflow_step', schema=None) as batch_op:
        batch_op.drop_column('output_schema')
        batch_op.drop_column('input_schema')
        batch_op.drop_column('skill_ids')
        batch_op.drop_column('instructions')
        batch_op.drop_column('depends_on_step_ids')
