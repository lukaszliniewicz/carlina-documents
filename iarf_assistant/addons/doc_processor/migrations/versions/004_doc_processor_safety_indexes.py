"""Add Document Processor lookup indexes and remove plaintext output backups.

Revision ID: 004_doc_processor_safety_indexes
Revises: 003_document_metadata_ownership
Create Date: 2026-07-31 12:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "004_doc_processor_safety_indexes"
down_revision = "003_document_metadata_ownership"
branch_labels = None
depends_on = None


INDEXES = (
    ("processed_document", "ix_processed_document_user_id", ("user_id",)),
    ("project_run", "ix_project_run_project_id", ("project_id",)),
    ("project_document", "ix_project_document_document_id", ("document_id",)),
    ("project_run_document", "ix_project_run_document_document_id", ("document_id",)),
)


def _table_names() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _index_names(table_name: str) -> set[str]:
    return {
        index["name"]
        for index in sa.inspect(op.get_bind()).get_indexes(table_name)
        if index.get("name")
    }


def _purge_json_key(table_name: str, primary_key: str, json_column: str) -> None:
    if table_name not in _table_names():
        return
    table = sa.table(
        table_name,
        sa.column(primary_key, sa.String(length=36)),
        sa.column(json_column, sa.JSON()),
    )
    connection = op.get_bind()
    id_column = table.c[primary_key]
    value_column = table.c[json_column]
    for record_id, settings in connection.execute(
        sa.select(id_column, value_column)
    ):
        if not isinstance(settings, dict) or "original_output_backup" not in settings:
            continue
        cleaned_settings = dict(settings)
        cleaned_settings.pop("original_output_backup", None)
        connection.execute(
            table.update()
            .where(id_column == record_id)
            .values({json_column: cleaned_settings})
        )


def upgrade():
    tables = _table_names()
    for table_name, index_name, columns in INDEXES:
        if table_name in tables and index_name not in _index_names(table_name):
            op.create_index(index_name, table_name, list(columns))
    _purge_json_key("processed_document", "id", "settings")
    _purge_json_key("project_run_document", "id", "effective_settings")


def downgrade():
    tables = _table_names()
    for table_name, index_name, _columns in reversed(INDEXES):
        if table_name in tables and index_name in _index_names(table_name):
            op.drop_index(index_name, table_name=table_name)
