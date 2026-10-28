"""add context fabric v2 durability and model capability metadata

Revision ID: 20260728_context_fabric_v2
Revises: 20260726_workflow_template
Create Date: 2026-07-28 00:00:00.000000

EncryptedType fields are represented as TEXT in the migration. Encryption and
the user/system key envelope are applied by the application type decorator.
"""

from alembic import op
import sqlalchemy as sa


revision = "20260728_context_fabric_v2"
down_revision = "20260726_workflow_template"
branch_labels = None
depends_on = None


def _column_names(table_name):
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table(table_name):
        return set()
    return {column["name"] for column in inspector.get_columns(table_name)}


def _index_names(table_name):
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table(table_name):
        return set()
    return {index.get("name") for index in inspector.get_indexes(table_name)}


def _create_indexes(table_name, columns):
    existing = _index_names(table_name)
    for column in columns:
        name = f"ix_{table_name}_{column}"
        if name not in existing:
            op.create_index(name, table_name, [column], unique=False)


def upgrade():
    llm_columns = _column_names("llm_models")
    with op.batch_alter_table("llm_models", schema=None) as batch_op:
        if "max_output_tokens" not in llm_columns:
            batch_op.add_column(sa.Column("max_output_tokens", sa.Integer(), nullable=True))
        if "capability_overrides" not in llm_columns:
            batch_op.add_column(sa.Column("capability_overrides", sa.JSON(), nullable=True))

    session_columns = _column_names("chat_session")
    if "context_stewardship" not in session_columns:
        with op.batch_alter_table("chat_session", schema=None) as batch_op:
            batch_op.add_column(sa.Column(
                "context_stewardship",
                sa.String(length=24),
                server_default="assisted",
                nullable=False,
            ))

    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("chat_turn"):
        op.create_table(
            "chat_turn",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("session_id", sa.String(length=36), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(length=24), server_default="pending", nullable=False),
            sa.Column("context_stewardship", sa.String(length=24), server_default="assisted", nullable=False),
            sa.Column("model_id", sa.String(length=255), nullable=True),
            sa.Column("assistant_message_id", sa.Integer(), nullable=True),
            sa.Column("base_memory_revision", sa.Integer(), nullable=True),
            sa.Column("admission_json", sa.Text(), nullable=True),
            sa.Column("finalization_json", sa.Text(), nullable=True),
            sa.Column("error_json", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.Column("finalized_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(
                ["assistant_message_id"],
                ["chat_message.id"],
                name="fk_chat_turn_assistant_message_id_chat_message",
            ),
            sa.ForeignKeyConstraint(
                ["session_id"],
                ["chat_session.id"],
                name="fk_chat_turn_session_id_chat_session",
            ),
            sa.ForeignKeyConstraint(
                ["user_id"],
                ["user.id"],
                name="fk_chat_turn_user_id_user",
            ),
            sa.PrimaryKeyConstraint("id"),
        )
        _create_indexes("chat_turn", ("session_id", "status", "user_id"))

    inspector = sa.inspect(bind)
    if not inspector.has_table("chat_evidence"):
        op.create_table(
            "chat_evidence",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("session_id", sa.String(length=36), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("turn_id", sa.String(length=36), nullable=True),
            sa.Column("kind", sa.String(length=64), nullable=False),
            sa.Column("role", sa.String(length=32), nullable=True),
            sa.Column("mime_type", sa.String(length=255), nullable=True),
            sa.Column("source_type", sa.String(length=64), nullable=True),
            sa.Column("source_id", sa.String(length=512), nullable=True),
            sa.Column("source_version", sa.String(length=128), nullable=True),
            sa.Column("content_hash", sa.String(length=64), nullable=False),
            sa.Column("content", sa.Text(), nullable=True),
            sa.Column("content_encoding", sa.String(length=32), server_default="utf-8", nullable=False),
            sa.Column("char_count", sa.Integer(), server_default="0", nullable=False),
            sa.Column("byte_count", sa.Integer(), server_default="0", nullable=False),
            sa.Column("token_estimate", sa.Integer(), nullable=True),
            sa.Column("metadata_json", sa.Text(), nullable=True),
            sa.Column("retention_state", sa.String(length=24), server_default="retained", nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("deleted_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(
                ["session_id"],
                ["chat_session.id"],
                name="fk_chat_evidence_session_id_chat_session",
            ),
            sa.ForeignKeyConstraint(
                ["turn_id"],
                ["chat_turn.id"],
                name="fk_chat_evidence_turn_id_chat_turn",
            ),
            sa.ForeignKeyConstraint(
                ["user_id"],
                ["user.id"],
                name="fk_chat_evidence_user_id_user",
            ),
            sa.PrimaryKeyConstraint("id"),
        )
        _create_indexes(
            "chat_evidence",
            (
                "content_hash",
                "kind",
                "retention_state",
                "session_id",
                "source_id",
                "source_type",
                "turn_id",
                "user_id",
            ),
        )

    inspector = sa.inspect(bind)
    if not inspector.has_table("chat_evidence_chunk"):
        op.create_table(
            "chat_evidence_chunk",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("evidence_id", sa.String(length=36), nullable=False),
            sa.Column("ordinal", sa.Integer(), nullable=False),
            sa.Column("start_offset", sa.Integer(), nullable=False),
            sa.Column("end_offset", sa.Integer(), nullable=False),
            sa.Column("content_hash", sa.String(length=64), nullable=False),
            sa.Column("content", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.CheckConstraint(
                "end_offset >= start_offset",
                name="ck_chat_evidence_chunk_offsets",
            ),
            sa.CheckConstraint(
                "start_offset >= 0",
                name="ck_chat_evidence_chunk_start_nonnegative",
            ),
            sa.ForeignKeyConstraint(
                ["evidence_id"],
                ["chat_evidence.id"],
                name="fk_chat_evidence_chunk_evidence_id_chat_evidence",
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "evidence_id",
                "ordinal",
                name="uq_chat_evidence_chunk_evidence_ordinal",
            ),
        )
        _create_indexes("chat_evidence_chunk", ("evidence_id",))

    inspector = sa.inspect(bind)
    if not inspector.has_table("chat_context_item"):
        op.create_table(
            "chat_context_item",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("session_id", sa.String(length=36), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("evidence_id", sa.String(length=36), nullable=False),
            sa.Column("first_turn_id", sa.String(length=36), nullable=True),
            sa.Column("last_turn_id", sa.String(length=36), nullable=True),
            sa.Column("kind", sa.String(length=64), nullable=False),
            sa.Column("title", sa.Text(), nullable=True),
            sa.Column("lifecycle_state", sa.String(length=24), server_default="active", nullable=False),
            sa.Column("materialization", sa.String(length=24), server_default="full", nullable=False),
            sa.Column("materialized_content", sa.Text(), nullable=True),
            sa.Column("priority", sa.Integer(), server_default="50", nullable=False),
            sa.Column("is_pinned", sa.Boolean(), server_default=sa.false(), nullable=False),
            sa.Column("token_estimate", sa.Integer(), nullable=True),
            sa.Column("content_hash", sa.String(length=64), nullable=False),
            sa.Column("source_ref_json", sa.Text(), nullable=True),
            sa.Column("metadata_json", sa.Text(), nullable=True),
            sa.Column("version", sa.Integer(), server_default="1", nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.CheckConstraint(
                "lifecycle_state in ('active','archived','excluded','deleted')",
                name="ck_chat_context_item_lifecycle_state",
            ),
            sa.CheckConstraint(
                "materialization in ('full','excerpt','summary','reference','overflow')",
                name="ck_chat_context_item_materialization",
            ),
            sa.CheckConstraint(
                "priority >= 0 and priority <= 100",
                name="ck_chat_context_item_priority",
            ),
            sa.CheckConstraint(
                "version >= 1",
                name="ck_chat_context_item_version",
            ),
            sa.ForeignKeyConstraint(
                ["evidence_id"],
                ["chat_evidence.id"],
                name="fk_chat_context_item_evidence_id_chat_evidence",
            ),
            sa.ForeignKeyConstraint(
                ["first_turn_id"],
                ["chat_turn.id"],
                name="fk_chat_context_item_first_turn_id_chat_turn",
            ),
            sa.ForeignKeyConstraint(
                ["last_turn_id"],
                ["chat_turn.id"],
                name="fk_chat_context_item_last_turn_id_chat_turn",
            ),
            sa.ForeignKeyConstraint(
                ["session_id"],
                ["chat_session.id"],
                name="fk_chat_context_item_session_id_chat_session",
            ),
            sa.ForeignKeyConstraint(
                ["user_id"],
                ["user.id"],
                name="fk_chat_context_item_user_id_user",
            ),
            sa.PrimaryKeyConstraint("id"),
        )
        _create_indexes(
            "chat_context_item",
            (
                "evidence_id",
                "is_pinned",
                "kind",
                "lifecycle_state",
                "session_id",
                "user_id",
            ),
        )

    inspector = sa.inspect(bind)
    if not inspector.has_table("chat_context_event"):
        op.create_table(
            "chat_context_event",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("context_item_id", sa.String(length=36), nullable=False),
            sa.Column("session_id", sa.String(length=36), nullable=False),
            sa.Column("turn_id", sa.String(length=36), nullable=True),
            sa.Column("actor_type", sa.String(length=24), nullable=False),
            sa.Column("actor_id", sa.String(length=255), nullable=True),
            sa.Column("event_type", sa.String(length=32), nullable=False),
            sa.Column("from_state", sa.String(length=24), nullable=True),
            sa.Column("to_state", sa.String(length=24), nullable=True),
            sa.Column("from_materialization", sa.String(length=24), nullable=True),
            sa.Column("to_materialization", sa.String(length=24), nullable=True),
            sa.Column("expected_version", sa.Integer(), nullable=True),
            sa.Column("result_version", sa.Integer(), nullable=False),
            sa.Column("reason", sa.Text(), nullable=True),
            sa.Column("metadata_json", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(
                ["context_item_id"],
                ["chat_context_item.id"],
                name="fk_chat_context_event_item_id_chat_context_item",
            ),
            sa.ForeignKeyConstraint(
                ["session_id"],
                ["chat_session.id"],
                name="fk_chat_context_event_session_id_chat_session",
            ),
            sa.ForeignKeyConstraint(
                ["turn_id"],
                ["chat_turn.id"],
                name="fk_chat_context_event_turn_id_chat_turn",
            ),
            sa.PrimaryKeyConstraint("id"),
        )
        _create_indexes(
            "chat_context_event",
            ("context_item_id", "event_type", "session_id", "turn_id"),
        )

    inspector = sa.inspect(bind)
    if not inspector.has_table("chat_memory_revision"):
        op.create_table(
            "chat_memory_revision",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("session_id", sa.String(length=36), nullable=False),
            sa.Column("turn_id", sa.String(length=36), nullable=True),
            sa.Column("revision", sa.Integer(), nullable=False),
            sa.Column("parent_revision", sa.Integer(), nullable=True),
            sa.Column("model_id", sa.String(length=255), nullable=True),
            sa.Column("patch_json", sa.Text(), nullable=False),
            sa.Column("snapshot_json", sa.Text(), nullable=False),
            sa.Column("evidence_refs", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.CheckConstraint(
                "revision >= 1",
                name="ck_chat_memory_revision_positive",
            ),
            sa.ForeignKeyConstraint(
                ["session_id"],
                ["chat_session.id"],
                name="fk_chat_memory_revision_session_id_chat_session",
            ),
            sa.ForeignKeyConstraint(
                ["turn_id"],
                ["chat_turn.id"],
                name="fk_chat_memory_revision_turn_id_chat_turn",
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "session_id",
                "revision",
                name="uq_chat_memory_revision_session_revision",
            ),
            sa.UniqueConstraint(
                "turn_id",
                name="uq_chat_memory_revision_turn_id",
            ),
        )
        _create_indexes("chat_memory_revision", ("session_id", "turn_id"))


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    for table_name in (
        "chat_memory_revision",
        "chat_context_event",
        "chat_context_item",
        "chat_evidence_chunk",
        "chat_evidence",
        "chat_turn",
    ):
        inspector = sa.inspect(bind)
        if inspector.has_table(table_name):
            op.drop_table(table_name)

    session_columns = _column_names("chat_session")
    if "context_stewardship" in session_columns:
        with op.batch_alter_table("chat_session", schema=None) as batch_op:
            batch_op.drop_column("context_stewardship")

    llm_columns = _column_names("llm_models")
    with op.batch_alter_table("llm_models", schema=None) as batch_op:
        if "capability_overrides" in llm_columns:
            batch_op.drop_column("capability_overrides")
        if "max_output_tokens" in llm_columns:
            batch_op.drop_column("max_output_tokens")
