"""Add needs_review to glossary

Revision ID: 004_glossary_needs_review
Revises: 003_translator_blocks
Create Date: 2024-11-20 13:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

revision = "004_glossary_needs_review"
down_revision = "003_translator_blocks"
branch_labels = None
depends_on = None

def upgrade():
    with op.batch_alter_table("translation_glossary", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("needs_review", sa.Boolean(), server_default="0", nullable=False)
        )

def downgrade():
    with op.batch_alter_table("translation_glossary", schema=None) as batch_op:
        batch_op.drop_column("needs_review")
