"""attach workflow runs to chat sessions

Revision ID: 20260515_workflow_run_chat_session
Revises: 20260514_workflow_run_titles
Create Date: 2026-05-15 00:00:01.000000

"""
from alembic import op
import sqlalchemy as sa


revision = '20260515_workflow_run_chat_session'
down_revision = '20260514_workflow_run_titles'
branch_labels = None
depends_on = None


def upgrade():
    # SQLite dev databases cannot safely rebuild workflow_run while workflow_step
    # and artifact tables hold foreign keys into it. Add the nullable indexed
    # column incrementally; fresh installs still get the FK from SQLAlchemy metadata.
    with op.batch_alter_table('workflow_run', schema=None) as batch_op:
        batch_op.add_column(sa.Column('chat_session_id', sa.String(length=36), nullable=True))
        batch_op.create_index(batch_op.f('ix_workflow_run_chat_session_id'), ['chat_session_id'], unique=False)


def downgrade():
    with op.batch_alter_table('workflow_run', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_workflow_run_chat_session_id'))
        batch_op.drop_column('chat_session_id')
