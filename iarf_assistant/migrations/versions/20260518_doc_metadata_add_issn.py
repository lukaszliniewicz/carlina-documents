"""add issn column to document_metadata

Revision ID: 20260518_doc_metadata_add_issn
Revises: 20260515_workflow_run_chat_session_fk
Create Date: 2026-05-18 00:00:01.000000

"""

from alembic import op
import sqlalchemy as sa


revision = "20260518_doc_metadata_add_issn"
down_revision = "20260515_workflow_run_chat_session_fk"
branch_labels = None
depends_on = None


def _table_has_column(bind, table_name: str, column_name: str) -> bool:
    inspector = sa.inspect(bind)
    if table_name not in inspector.get_table_names():
        return False
    return any(column.get("name") == column_name for column in inspector.get_columns(table_name))


def upgrade():
    bind = op.get_bind()
    if _table_has_column(bind, "document_metadata", "issn"):
        return

    if "document_metadata" in sa.inspect(bind).get_table_names():
        with op.batch_alter_table("document_metadata", schema=None) as batch_op:
            batch_op.add_column(sa.Column("issn", sa.String(length=50), nullable=True))


def downgrade():
    bind = op.get_bind()
    if not _table_has_column(bind, "document_metadata", "issn"):
        return

    with op.batch_alter_table("document_metadata", schema=None) as batch_op:
        batch_op.drop_column("issn")
