"""Added api_version to ApiKey for Azure OpenAI

Revision ID: ed60d16b91d6
Revises: add_tts_audio_path_001
Create Date: 2026-03-22 13:50:57.062620

"""
from alembic import op
import sqlalchemy as sa
import iarf_assistant


# revision identifiers, used by Alembic.
revision = 'ed60d16b91d6'
down_revision = 'add_tts_audio_path_001'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('api_key', schema=None) as batch_op:
        batch_op.add_column(sa.Column('api_version', sa.String(length=100), nullable=True))


def downgrade():
    with op.batch_alter_table('api_key', schema=None) as batch_op:
        batch_op.drop_column('api_version')
