"""Add working memory to ChatSession

Revision ID: 7d39a8bd45c5
Revises: f89bb6ee5cd6
Create Date: 2026-03-10 04:12:45.285097

"""
from alembic import op
import sqlalchemy as sa
import iarf_assistant


# revision identifiers, used by Alembic.
revision = '7d39a8bd45c5'
down_revision = 'f89bb6ee5cd6'
branch_labels = None
depends_on = None


def upgrade():
    # Only add the working_memory column
    with op.batch_alter_table('chat_session', schema=None) as batch_op:
        batch_op.add_column(sa.Column('working_memory', sa.Text(), nullable=True))

def downgrade():
    # Only remove the working_memory column
    with op.batch_alter_table('chat_session', schema=None) as batch_op:
        batch_op.drop_column('working_memory')