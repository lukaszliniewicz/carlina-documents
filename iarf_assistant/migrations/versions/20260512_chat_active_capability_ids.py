"""add chat active capability ids

Revision ID: 20260512_chat_caps
Revises: 20260512_run_kind
Create Date: 2026-05-12 00:00:02.000000

"""
from alembic import op
import sqlalchemy as sa


revision = '20260512_chat_caps'
down_revision = '20260512_run_kind'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('chat_session', schema=None) as batch_op:
        batch_op.add_column(sa.Column('active_capability_ids', sa.JSON(), nullable=True))

    op.execute(
        "UPDATE chat_session "
        "SET active_capability_ids = active_plugins "
        "WHERE active_capability_ids IS NULL AND active_plugins IS NOT NULL"
    )


def downgrade():
    with op.batch_alter_table('chat_session', schema=None) as batch_op:
        batch_op.drop_column('active_capability_ids')
