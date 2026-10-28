"""create document_metadata table when missing

Revision ID: 20260518_doc_metadata_create_if_missing
Revises: 20260518_doc_metadata_add_issn
Create Date: 2026-05-18 00:00:02.000000

"""

from alembic import op
import sqlalchemy as sa


revision = "20260518_doc_metadata_create_if_missing"
down_revision = "20260518_doc_metadata_add_issn"
branch_labels = None
depends_on = None


def _table_exists(bind, table_name: str) -> bool:
    inspector = sa.inspect(bind)
    return table_name in inspector.get_table_names()


def _column_exists(bind, table_name: str, column_name: str) -> bool:
    inspector = sa.inspect(bind)
    if table_name not in inspector.get_table_names():
        return False
    return any(column.get("name") == column_name for column in inspector.get_columns(table_name))


def upgrade():
    bind = op.get_bind()
    has_processed_document = _table_exists(bind, "processed_document")
    has_document_metadata = _table_exists(bind, "document_metadata")

    if not has_document_metadata and has_processed_document:
        op.create_table(
            "document_metadata",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("document_id", sa.String(length=36), nullable=False),
            sa.Column("title", sa.String(length=512), nullable=True),
            sa.Column("subtitle", sa.String(length=512), nullable=True),
            sa.Column("author", sa.String(length=512), nullable=True),
            sa.Column("publisher", sa.String(length=255), nullable=True),
            sa.Column("publication_date", sa.String(length=50), nullable=True),
            sa.Column("isbn", sa.String(length=50), nullable=True),
            sa.Column("issn", sa.String(length=50), nullable=True),
            sa.Column("language", sa.String(length=50), nullable=True),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("cover_image_id", sa.String(length=36), nullable=True),
            sa.Column("contributors", sa.JSON(), nullable=True),
            sa.Column("table_of_contents", sa.JSON(), nullable=True),
            sa.Column("pages_to_remove", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(
                ["document_id"],
                ["processed_document.id"],
                ondelete="CASCADE",
            ),
        )
        return

    if has_document_metadata and not _column_exists(bind, "document_metadata", "issn"):
        with op.batch_alter_table("document_metadata", schema=None) as batch_op:
            batch_op.add_column(sa.Column("issn", sa.String(length=50), nullable=True))


def downgrade():
    bind = op.get_bind()
    if _table_exists(bind, "document_metadata"):
        op.drop_table("document_metadata")
