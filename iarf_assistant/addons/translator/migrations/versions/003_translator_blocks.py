"""Add block_index and merge tracking

Revision ID: 003_translator_blocks
Revises: 002_refactor_translator
Create Date: 2024-11-20 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

revision = "003_translator_blocks"
down_revision = "002_refactor_translator"
branch_labels = None
depends_on = None

def upgrade():
    with op.batch_alter_table("translation_segment", schema=None) as batch_op:
        batch_op.add_column(sa.Column("block_index", sa.Integer(), nullable=False, server_default="0"))
        batch_op.add_column(sa.Column("merged_into_id", sa.String(length=36), nullable=True))
        
        batch_op.create_foreign_key(
            "fk_segment_merged_into", 
            "translation_segment", 
            ["merged_into_id"], 
            ["id"], 
            ondelete="SET NULL"
        )

def downgrade():
    with op.batch_alter_table("translation_segment", schema=None) as batch_op:
        batch_op.drop_constraint("fk_segment_merged_into", type_="foreignkey")
        batch_op.drop_column("merged_into_id")
        batch_op.drop_column("block_index")
