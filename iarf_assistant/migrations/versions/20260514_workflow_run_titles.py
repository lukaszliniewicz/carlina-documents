"""add human readable workflow run titles

Revision ID: 20260514_workflow_run_titles
Revises: 20260514_workflow_dag_schema
Create Date: 2026-05-14 00:00:05.000000

"""
from alembic import op
import sqlalchemy as sa


revision = '20260514_workflow_run_titles'
down_revision = '20260514_workflow_dag_schema'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('workflow_run', schema=None) as batch_op:
        batch_op.add_column(sa.Column('title', sa.String(length=255), nullable=True))


def downgrade():
    with op.batch_alter_table('workflow_run', schema=None) as batch_op:
        batch_op.drop_column('title')
