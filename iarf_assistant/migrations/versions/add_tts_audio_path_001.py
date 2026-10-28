"""Add tts_audio_path to chat_message

Revision ID: add_tts_audio_path_001
Revises: f89bb6ee5cd6
Create Date: 2026-03-20 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

revision = 'add_tts_audio_path_001'
down_revision = '7d39a8bd45c5'
branch_labels = None
depends_on = None

def upgrade():
    with op.batch_alter_table('chat_message', schema=None) as batch_op:
        batch_op.add_column(sa.Column('tts_audio_path', sa.String(length=512), nullable=True))

def downgrade():
    with op.batch_alter_table('chat_message', schema=None) as batch_op:
        batch_op.drop_column('tts_audio_path')
