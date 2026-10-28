"""Initial doc processor schema

Revision ID: 001_doc_processor
Revises:
Create Date: 2024-05-24 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

from iarf_assistant.database.types import EncryptedType


# revision identifiers, used by Alembic.
revision = "001_doc_processor"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "processed_document" in inspector.get_table_names():
        return

    op.create_table(
        "processed_document",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("file_path", EncryptedType(sa.String(length=512)), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("settings", sa.JSON(), nullable=True),
        sa.Column("output_html", EncryptedType(sa.Text()), nullable=True),
        sa.Column("output_markdown", EncryptedType(sa.Text()), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "processed_document" in inspector.get_table_names():
        op.drop_table("processed_document")
