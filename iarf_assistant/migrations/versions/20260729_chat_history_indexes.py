"""add composite indexes for chat listing and history hydration

Revision ID: 20260729_chat_history_indexes
Revises: 20260728_knowledge_fabric_v2
Create Date: 2026-07-29 12:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "20260729_chat_history_indexes"
down_revision = "20260728_knowledge_fabric_v2"
branch_labels = None
depends_on = None


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(table_name):
    return _inspector().has_table(table_name)


def _index_names(table_name):
    if not _has_table(table_name):
        return set()
    return {
        index.get("name")
        for index in _inspector().get_indexes(table_name)
    }


def _create_index(table_name, index_name, columns):
    if _has_table(table_name) and index_name not in _index_names(table_name):
        op.create_index(
            index_name,
            table_name,
            columns,
            unique=False,
        )


def _drop_index(table_name, index_name):
    if _has_table(table_name) and index_name in _index_names(table_name):
        op.drop_index(index_name, table_name=table_name)


def upgrade():
    _create_index(
        "chat_session",
        "ix_chat_session_user_updated_id",
        ["user_id", "updated_at", "id"],
    )
    _create_index(
        "chat_message",
        "ix_chat_message_session_id_id",
        ["session_id", "id"],
    )
    _create_index(
        "chat_message",
        "ix_chat_message_session_created_id",
        ["session_id", "created_at", "id"],
    )


def downgrade():
    _drop_index(
        "chat_message",
        "ix_chat_message_session_created_id",
    )
    _drop_index(
        "chat_message",
        "ix_chat_message_session_id_id",
    )
    _drop_index(
        "chat_session",
        "ix_chat_session_user_updated_id",
    )
