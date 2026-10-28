"""Add speech timing media file

Revision ID: 70bd868428a2
Revises: 0236dd4bd4ac
Create Date: 2026-05-22 23:24:31.081477

"""
from alembic import op
import sqlalchemy as sa
import iarf_assistant


# revision identifiers, used by Alembic.

revision = '70bd868428a2'
down_revision = '0236dd4bd4ac'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('speech_timing_media_file',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('project_id', sa.Integer(), nullable=False),
        sa.Column('file_index', sa.Integer(), nullable=False),
        sa.Column('filename', sa.String(length=255), nullable=False),
        sa.Column('media_path', sa.String(length=512), nullable=False),
        sa.Column('status', sa.String(length=32), nullable=True),
        sa.ForeignKeyConstraint(['project_id'], ['speech_timing_project.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('speech_timing_media_file', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_speech_timing_media_file_project_id'), ['project_id'], unique=False)
        
    op.create_table('speech_timing_chapter',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('project_id', sa.Integer(), nullable=False),
        sa.Column('chapter_index', sa.Integer(), nullable=False),
        sa.Column('title', sa.String(length=255), nullable=True),
        sa.Column('text_content', sa.Text(), nullable=False),
        sa.Column('media_file_id', sa.Integer(), nullable=True),
        sa.Column('status', sa.String(length=32), nullable=True),
        sa.ForeignKeyConstraint(['media_file_id'], ['speech_timing_media_file.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['project_id'], ['speech_timing_project.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('speech_timing_chapter', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_speech_timing_chapter_project_id'), ['project_id'], unique=False)


def downgrade():
    with op.batch_alter_table('speech_timing_chapter', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_speech_timing_chapter_project_id'))
    op.drop_table('speech_timing_chapter')

    with op.batch_alter_table('speech_timing_media_file', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_speech_timing_media_file_project_id'))
    op.drop_table('speech_timing_media_file')
