"""Add OrgMemory and UserMemory

Revision ID: 2712fe83eb9d
Revises: 20260515_workflow_run_chat_session
Create Date: 2026-05-15 02:54:56.008021

"""
from alembic import op
import sqlalchemy as sa
import iarf_assistant


# revision identifiers, used by Alembic.
revision = '2712fe83eb9d'
down_revision = '20260515_workflow_run_chat_session'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'org_memory',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('fact', sa.Text(), nullable=False),
        sa.Column('embedding', sa.LargeBinary(), nullable=True),
        sa.Column('json_metadata', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_table(
        'user_memory',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('fact', sa.Text(), nullable=False),
        sa.Column('embedding', sa.LargeBinary(), nullable=True),
        sa.Column('json_metadata', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['user.id'], name='fk_user_memory_user_id_user'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_user_memory_user_id'), 'user_memory', ['user_id'], unique=False)


def downgrade():
    op.drop_index(op.f('ix_user_memory_user_id'), table_name='user_memory')
    op.drop_table('user_memory')
    op.drop_table('org_memory')
