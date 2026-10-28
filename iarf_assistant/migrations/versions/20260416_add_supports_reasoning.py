"""Add supports_reasoning to LLMModel

Revision ID: 202604160000
Revises: ed60d16b91d6
Create Date: 2026-04-16 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '202604160000'
down_revision = 'ed60d16b91d6'
branch_labels = None
depends_on = None

def upgrade():
    with op.batch_alter_table('llm_models', schema=None) as batch_op:
        batch_op.add_column(sa.Column('supports_reasoning', sa.Boolean(), server_default='0', nullable=False))

def downgrade():
    with op.batch_alter_table('llm_models', schema=None) as batch_op:
        batch_op.drop_column('supports_reasoning')
