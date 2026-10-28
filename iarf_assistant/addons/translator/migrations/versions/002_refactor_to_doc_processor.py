"""Refactor to use doc processor

Revision ID: 002_refactor_translator
Revises: bfa3a1d29067
Create Date: 2024-05-24 10:05:00.000000

"""
from alembic import op
import sqlalchemy as sa

from iarf_assistant.database.types import EncryptedType


# revision identifiers, used by Alembic.
revision = "002_refactor_translator"
down_revision = "bfa3a1d29067"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_columns = {column["name"] for column in inspector.get_columns("translation_project")}

    with op.batch_alter_table("translation_project", schema=None) as batch_op:
        if "document_id" not in existing_columns:
            batch_op.add_column(sa.Column("document_id", sa.String(length=36), nullable=True))
        if "source_file_path" in existing_columns:
            batch_op.drop_column("source_file_path")
        if "parsing_rules" in existing_columns:
            batch_op.drop_column("parsing_rules")


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_columns = {column["name"] for column in inspector.get_columns("translation_project")}

    with op.batch_alter_table("translation_project", schema=None) as batch_op:
        if "parsing_rules" not in existing_columns:
            batch_op.add_column(sa.Column("parsing_rules", sa.JSON(), nullable=True))
        if "source_file_path" not in existing_columns:
            batch_op.add_column(sa.Column("source_file_path", EncryptedType(sa.String(length=512)), nullable=True))
        if "document_id" in existing_columns:
            batch_op.drop_column("document_id")
