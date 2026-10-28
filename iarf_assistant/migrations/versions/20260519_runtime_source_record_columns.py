"""add missing runtime_source_record columns

Revision ID: 20260519_runtime_source_record_columns
Revises: 20260518_doc_metadata_create_if_missing
Create Date: 2026-05-19 00:00:03.000000

"""

from alembic import op
import sqlalchemy as sa


revision = "20260519_runtime_source_record_columns"
down_revision = "20260518_doc_metadata_create_if_missing"
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


def _index_exists(bind, table_name: str, index_name: str) -> bool:
    inspector = sa.inspect(bind)
    if table_name not in inspector.get_table_names():
        return False
    return any(index.get("name") == index_name for index in inspector.get_indexes(table_name))


def upgrade():
    bind = op.get_bind()
    table_name = "runtime_source_record"
    if not _table_exists(bind, table_name):
        return

    add_canonical = not _column_exists(bind, table_name, "canonical_uri")
    add_accessed = not _column_exists(bind, table_name, "accessed_at")
    if add_canonical or add_accessed:
        with op.batch_alter_table(table_name, schema=None) as batch_op:
            if add_canonical:
                batch_op.add_column(sa.Column("canonical_uri", sa.String(length=2048), nullable=True))
            if add_accessed:
                batch_op.add_column(sa.Column("accessed_at", sa.DateTime(), nullable=True))

    if add_accessed:
        op.execute(
            sa.text(
                "UPDATE runtime_source_record "
                "SET accessed_at = COALESCE(accessed_at, created_at, CURRENT_TIMESTAMP)"
            )
        )

    index_name = "ix_runtime_source_record_canonical_uri"
    if _column_exists(bind, table_name, "canonical_uri") and not _index_exists(bind, table_name, index_name):
        with op.batch_alter_table(table_name, schema=None) as batch_op:
            batch_op.create_index(batch_op.f(index_name), ["canonical_uri"], unique=False)


def downgrade():
    bind = op.get_bind()
    table_name = "runtime_source_record"
    if not _table_exists(bind, table_name):
        return

    drop_index = _index_exists(bind, table_name, "ix_runtime_source_record_canonical_uri")
    drop_canonical = _column_exists(bind, table_name, "canonical_uri")
    drop_accessed = _column_exists(bind, table_name, "accessed_at")
    if drop_index or drop_canonical or drop_accessed:
        with op.batch_alter_table(table_name, schema=None) as batch_op:
            if drop_index:
                batch_op.drop_index(batch_op.f("ix_runtime_source_record_canonical_uri"))
            if drop_canonical:
                batch_op.drop_column("canonical_uri")
            if drop_accessed:
                batch_op.drop_column("accessed_at")
