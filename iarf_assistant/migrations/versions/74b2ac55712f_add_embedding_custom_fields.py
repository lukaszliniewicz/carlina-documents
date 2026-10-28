"""add_embedding_custom_fields

Revision ID: 74b2ac55712f
Revises: 1e63bec4585b
Create Date: 2026-06-17 00:23:44.055477

"""
from alembic import op
import sqlalchemy as sa
import iarf_assistant


# revision identifiers, used by Alembic.
revision = '74b2ac55712f'
down_revision = '1e63bec4585b'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('llm_models', schema=None) as batch_op:
        batch_op.add_column(sa.Column('model_type', sa.String(length=50), nullable=False, server_default='chat'))
        batch_op.add_column(sa.Column('default_dimensions', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('supports_mrl', sa.Boolean(), nullable=False, server_default='0'))
        batch_op.add_column(sa.Column('prefix_style', sa.String(length=50), nullable=False, server_default='none'))
        batch_op.add_column(sa.Column('custom_query_prefix', sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column('custom_passage_prefix', sa.String(length=255), nullable=True))


def downgrade():
    with op.batch_alter_table('llm_models', schema=None) as batch_op:
        batch_op.drop_column('custom_passage_prefix')
        batch_op.drop_column('custom_query_prefix')
        batch_op.drop_column('prefix_style')
        batch_op.drop_column('supports_mrl')
        batch_op.drop_column('default_dimensions')
        batch_op.drop_column('model_type')
