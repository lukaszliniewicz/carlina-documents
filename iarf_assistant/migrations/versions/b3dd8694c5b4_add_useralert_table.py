"""Add UserAlert table

Revision ID: b3dd8694c5b4
Revises: 2712fe83eb9d
Create Date: 2026-05-15 03:17:33.539494

"""
from alembic import op
import sqlalchemy as sa
import iarf_assistant


# revision identifiers, used by Alembic.
revision = 'b3dd8694c5b4'
down_revision = '2712fe83eb9d'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'user_alert',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('title', sa.String(length=255), nullable=False),
        sa.Column('message', sa.Text(), nullable=False),
        sa.Column('action_url', sa.String(length=1024), nullable=True),
        sa.Column('is_read', sa.Boolean(), nullable=False, server_default=sa.text('0')),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['user.id'], name='fk_user_alert_user_id_user'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_user_alert_user_id'), 'user_alert', ['user_id'], unique=False)


def downgrade():
    op.drop_index(op.f('ix_user_alert_user_id'), table_name='user_alert')
    op.drop_table('user_alert')
