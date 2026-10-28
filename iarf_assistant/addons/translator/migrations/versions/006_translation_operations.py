"""Add immutable source metadata, segment versions, and the operation ledger.

Revision ID: 006_translation_operations
Revises: 005_query_indexes
"""

import sqlalchemy as sa
from alembic import op

revision = "006_translation_operations"
down_revision = "005_query_indexes"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    is_sqlite = bind.dialect.name == "sqlite"

    # These are deliberately native ALTER TABLE operations.  Alembic's batch
    # implementation rebuilds the parent table on SQLite; with ON DELETE
    # CASCADE that can delete translation children while copying rows.
    op.add_column(
        "translation_project",
        sa.Column(
            "source_snapshot",
            # EncryptedType is physically TEXT.  Migrations declare storage
            # shape only; the ORM owns envelope encryption/decryption.
            sa.Text(),
            nullable=True,
        ),
    )
    op.add_column(
        "translation_project",
        sa.Column("source_content_hash", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "translation_project",
        sa.Column("source_captured_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "translation_project",
        sa.Column("creation_idempotency_key", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "translation_project",
        sa.Column(
            "creation_request_fingerprint",
            sa.String(length=64),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_translation_project_source_content_hash",
        "translation_project",
        ["source_content_hash"],
        unique=False,
    )
    op.create_index(
        "uq_translation_project_user_creation_key",
        "translation_project",
        ["user_id", "creation_idempotency_key"],
        unique=True,
    )

    op.add_column(
        "translation_segment",
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
    )
    if is_sqlite:
        # SQLite cannot add a named CHECK constraint without a table rebuild.
        # Triggers provide the same invariant without touching the FK table.
        op.execute(
            """
            CREATE TRIGGER trg_translation_segment_version_insert
            BEFORE INSERT ON translation_segment
            FOR EACH ROW WHEN NEW.version < 1
            BEGIN
                SELECT RAISE(ABORT, 'translation_segment.version must be >= 1');
            END
            """
        )
        op.execute(
            """
            CREATE TRIGGER trg_translation_segment_version_update
            BEFORE UPDATE OF version ON translation_segment
            FOR EACH ROW WHEN NEW.version < 1
            BEGIN
                SELECT RAISE(ABORT, 'translation_segment.version must be >= 1');
            END
            """
        )
    else:
        op.create_check_constraint(
            "ck_translation_segment_version",
            "translation_segment",
            "version >= 1",
        )

    op.create_table(
        "translation_operation",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("owner_user_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("queue_job_id", sa.String(length=128), nullable=True),
        sa.Column(
            "state", sa.String(length=32), nullable=False, server_default="queued"
        ),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("progress", sa.Float(), nullable=True),
        sa.Column("message", sa.String(length=255), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column(
            "error_detail",
            sa.Text(),
            nullable=True,
        ),
        sa.Column(
            "checkpoint_json",
            sa.Text(),
            nullable=True,
        ),
        sa.Column(
            "result_json",
            sa.Text(),
            nullable=True,
        ),
        sa.Column("source_content_hash", sa.String(length=64), nullable=True),
        sa.Column("cancel_requested_at", sa.DateTime(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_translation_operation"),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["translation_project.id"],
            ondelete="CASCADE",
            name="fk_translation_operation_project",
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["user.id"],
            ondelete="CASCADE",
            name="fk_translation_operation_owner",
        ),
        sa.UniqueConstraint(
            "owner_user_id",
            "project_id",
            "kind",
            "idempotency_key",
            name="uq_translation_operation_owner_project_kind_key",
        ),
        sa.CheckConstraint(
            "state IN ('queued', 'running', 'cancel_requested', 'completed', "
            "'partial', 'failed', 'cancelled')",
            name="ck_translation_operation_state",
        ),
        sa.CheckConstraint("version >= 1", name="ck_translation_operation_version"),
        sa.CheckConstraint(
            "progress IS NULL OR (progress >= 0 AND progress <= 100)",
            name="ck_translation_operation_progress",
        ),
        sa.CheckConstraint(
            "length(request_fingerprint) = 64",
            name="ck_translation_operation_request_fingerprint",
        ),
        sa.CheckConstraint(
            "source_content_hash IS NULL OR length(source_content_hash) = 64",
            name="ck_translation_operation_source_hash",
        ),
        sa.CheckConstraint(
            "error_code IS NULL OR (length(error_code) BETWEEN 1 AND 64 AND "
            "substr(error_code, 1, 1) GLOB '[a-z]' AND "
            "error_code NOT GLOB '*[^a-z0-9_]*')",
            name="ck_translation_operation_error_code",
        ),
        sa.CheckConstraint(
            "state NOT IN ('cancel_requested', 'cancelled') "
            "OR cancel_requested_at IS NOT NULL",
            name="ck_translation_operation_cancellation_timestamp",
        ),
    )
    op.create_index(
        "ix_translation_operation_project_state_updated",
        "translation_operation",
        ["project_id", "state", "updated_at"],
        unique=False,
    )
    op.create_index(
        "ix_translation_operation_owner_kind_created",
        "translation_operation",
        ["owner_user_id", "kind", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_translation_operation_queue_job",
        "translation_operation",
        ["queue_job_id"],
        unique=False,
    )
    op.create_index(
        "ix_translation_operation_owner_state",
        "translation_operation",
        ["owner_user_id", "state"],
        unique=False,
    )
    op.create_index(
        "uq_translation_operation_active_project",
        "translation_operation",
        ["project_id"],
        unique=True,
        sqlite_where=sa.text("state IN ('queued', 'running', 'cancel_requested')"),
    )


def downgrade():
    bind = op.get_bind()
    durable_evidence = any(
        bind.execute(sa.text(statement)).first() is not None
        for statement in (
            "SELECT 1 FROM translation_operation LIMIT 1",
            "SELECT 1 FROM translation_project "
            "WHERE source_snapshot IS NOT NULL "
            "OR source_content_hash IS NOT NULL "
            "OR source_captured_at IS NOT NULL "
            "OR creation_idempotency_key IS NOT NULL "
            "OR creation_request_fingerprint IS NOT NULL LIMIT 1",
            "SELECT 1 FROM translation_segment WHERE version <> 1 LIMIT 1",
        )
    )
    if durable_evidence:
        raise RuntimeError(
            "Translator migration 006 is forward-only after immutable source, "
            "operation, idempotency, or edited segment-version evidence exists."
        )

    op.drop_index(
        "uq_translation_operation_active_project",
        table_name="translation_operation",
    )
    op.drop_index(
        "ix_translation_operation_owner_state", table_name="translation_operation"
    )
    op.drop_index(
        "ix_translation_operation_queue_job", table_name="translation_operation"
    )
    op.drop_index(
        "ix_translation_operation_owner_kind_created",
        table_name="translation_operation",
    )
    op.drop_index(
        "ix_translation_operation_project_state_updated",
        table_name="translation_operation",
    )
    op.drop_table("translation_operation")

    is_sqlite = bind.dialect.name == "sqlite"
    if is_sqlite:
        op.execute("DROP TRIGGER IF EXISTS trg_translation_segment_version_update")
        op.execute("DROP TRIGGER IF EXISTS trg_translation_segment_version_insert")
    else:
        op.drop_constraint(
            "ck_translation_segment_version",
            "translation_segment",
            type_="check",
        )
    op.drop_column("translation_segment", "version")
    op.drop_index(
        "uq_translation_project_user_creation_key",
        table_name="translation_project",
    )
    op.drop_index(
        "ix_translation_project_source_content_hash",
        table_name="translation_project",
    )
    op.drop_column("translation_project", "creation_request_fingerprint")
    op.drop_column("translation_project", "creation_idempotency_key")
    op.drop_column("translation_project", "source_captured_at")
    op.drop_column("translation_project", "source_content_hash")
    op.drop_column("translation_project", "source_snapshot")
