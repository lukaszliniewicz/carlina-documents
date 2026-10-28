"""add run kind for ad hoc and template runs

Revision ID: 20260512_run_kind
Revises: 20260512_phase4
Create Date: 2026-05-12 00:00:01.000000

"""
from alembic import op
import sqlalchemy as sa


revision = '20260512_run_kind'
down_revision = '20260512_phase4'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('workflow_run', schema=None) as batch_op:
        batch_op.add_column(sa.Column('kind', sa.String(length=32), server_default='template', nullable=False))
        batch_op.alter_column('workflow_id', existing_type=sa.String(length=255), nullable=True)
        batch_op.create_index(batch_op.f('ix_workflow_run_kind'), ['kind'], unique=False)


def downgrade():
    with op.batch_alter_table('workflow_run', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_workflow_run_kind'))
        batch_op.alter_column('workflow_id', existing_type=sa.String(length=255), nullable=False)
        batch_op.drop_column('kind')
