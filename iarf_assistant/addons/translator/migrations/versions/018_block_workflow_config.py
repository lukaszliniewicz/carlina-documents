"""Persist immutable document-block workflow configuration snapshots.

Revision ID: 018_block_workflow_config
Revises: 017_context_references
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision = "018_block_workflow_config"
down_revision = "017_context_references"
branch_labels = None
depends_on = None


def _legacy_lease_fence(value: object) -> str:
    """Match ``window_persistence.window_lease_fence`` for a legacy timestamp."""

    if isinstance(value, datetime):
        timestamp = value
    else:
        raw = str(value).strip().replace(" ", "T")
        timestamp = datetime.fromisoformat(raw)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    else:
        timestamp = timestamp.astimezone(UTC)
    iso = timestamp.isoformat(timespec="microseconds").replace("+00:00", "Z")
    payload = json.dumps(
        {"lease_expires_at": iso},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _populate_window_fence_columns(bind: sa.Connection) -> None:
    rows = bind.execute(
        sa.text(
            "SELECT w.id, w.operation_id, w.source_plan_id, "
            "w.source_plan_hash, "
            "o.source_content_hash AS operation_source_content_hash, "
            "o.version AS operation_version, o.queue_job_id, "
            "o.lease_expires_at, p.source_content_hash AS plan_source_content_hash "
            "FROM translation_window_attempt AS w "
            "LEFT JOIN translation_operation AS o ON o.id = w.operation_id "
            "LEFT JOIN translation_source_plan AS p ON p.id = w.source_plan_id"
        )
    ).mappings()
    for row in rows:
        operation_hash = row.get("operation_source_content_hash")
        plan_hash = row.get("plan_source_content_hash")
        retained_hashes = [
            value
            for value in (operation_hash, plan_hash)
            if isinstance(value, str)
            and len(value) == 64
            and all(char in "0123456789abcdef" for char in value)
        ]
        operation_version = row.get("operation_version")
        queue_job_id = row.get("queue_job_id")
        lease_expires_at = row.get("lease_expires_at")
        if (
            not retained_hashes
            or len(set(retained_hashes)) != 1
            or not isinstance(operation_version, int)
            or operation_version < 1
            or not isinstance(queue_job_id, str)
            or not queue_job_id.strip()
            or queue_job_id != queue_job_id.strip()
            or len(queue_job_id) > 128
            or lease_expires_at is None
        ):
            raise RuntimeError(
                "Translator migration 018 cannot reconstruct an exact legacy "
                f"window-operation fence for attempt {row['id']!r}."
            )
        source_content_hash = retained_hashes[0]
        bind.execute(
            sa.text(
                "UPDATE translation_window_attempt SET "
                "source_content_hash = :source_content_hash, "
                "operation_version = :operation_version, "
                "queue_job_id = :queue_job_id, "
                "operation_lease_expires_at = :operation_lease_expires_at, "
                "lease_fence = :lease_fence WHERE id = :id"
            ),
            {
                "id": row["id"],
                "source_content_hash": source_content_hash,
                "operation_version": operation_version,
                "queue_job_id": queue_job_id,
                "operation_lease_expires_at": lease_expires_at,
                "lease_fence": _legacy_lease_fence(lease_expires_at),
            },
        )


def upgrade() -> None:
    # SQLite cannot add a NOT NULL column to a populated table without a
    # default.  Stage the columns, backfill only from retained evidence, then
    # enforce the final non-null/check-constrained contract in one rebuild.
    for column in (
        sa.Column("source_content_hash", sa.String(length=64), nullable=True),
        sa.Column("operation_version", sa.Integer(), nullable=True),
        sa.Column("queue_job_id", sa.String(length=128), nullable=True),
        sa.Column("operation_lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("lease_fence", sa.String(length=64), nullable=True),
    ):
        op.add_column("translation_window_attempt", column)
    _populate_window_fence_columns(op.get_bind())

    # SQLite cannot alter a CHECK constraint in place.  Alembic's batch
    # recreator preserves every column, foreign key, index, and row while
    # replacing the dispatch-state predicate and enforcing the fence snapshot.
    with op.batch_alter_table("translation_window_attempt", recreate="always") as batch:
        batch.drop_constraint(
            "ck_translation_window_attempt_dispatch_state", type_="check"
        )
        batch.alter_column("source_content_hash", nullable=False)
        batch.alter_column("operation_version", nullable=False)
        batch.alter_column("queue_job_id", nullable=False)
        batch.alter_column("operation_lease_expires_at", nullable=False)
        batch.alter_column("lease_fence", nullable=False)
        batch.create_check_constraint(
            "ck_translation_window_attempt_source_hash",
            "length(source_content_hash) = 64",
        )
        batch.create_check_constraint(
            "ck_translation_window_attempt_operation_version",
            "operation_version >= 1",
        )
        batch.create_check_constraint(
            "ck_translation_window_attempt_queue_job",
            "length(queue_job_id) BETWEEN 1 AND 128 AND queue_job_id = trim(queue_job_id)",
        )
        batch.create_check_constraint(
            "ck_translation_window_attempt_lease_fence",
            "length(lease_fence) = 64",
        )
        batch.create_check_constraint(
            "ck_translation_window_attempt_dispatch_state",
            "dispatch_state IN ('prepared', 'dispatched', 'response_received', "
            "'provider_failed', 'normalization_failed', 'fence_failed', "
            "'staging_failed', 'staged', 'provider_outcome_uncertain')",
        )

    op.create_table(
        "translation_block_workflow_config",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("operation_id", sa.String(length=36), nullable=False),
        sa.Column("owner_user_id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("source_plan_id", sa.String(length=36), nullable=False),
        sa.Column("source_plan_hash", sa.String(length=64), nullable=False),
        sa.Column("source_content_hash", sa.String(length=64), nullable=False),
        sa.Column("config_hash", sa.String(length=64), nullable=False),
        # EncryptedType is physically TEXT; the ORM owns envelope handling.
        sa.Column("config_json", sa.Text(), nullable=False),
        sa.Column("config_receipt", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_translation_block_workflow_config"),
        sa.UniqueConstraint(
            "operation_id", name="uq_translation_block_workflow_config_operation"
        ),
        sa.UniqueConstraint(
            "id",
            "project_id",
            name="uq_translation_block_workflow_config_id_project",
        ),
        sa.CheckConstraint(
            "length(source_plan_hash) = 64",
            name="ck_translation_block_workflow_config_plan_hash",
        ),
        sa.CheckConstraint(
            "length(source_content_hash) = 64",
            name="ck_translation_block_workflow_config_content_hash",
        ),
        sa.CheckConstraint(
            "length(config_hash) = 64",
            name="ck_translation_block_workflow_config_hash",
        ),
        sa.CheckConstraint(
            "length(config_receipt) = 64",
            name="ck_translation_block_workflow_config_receipt",
        ),
        sa.ForeignKeyConstraint(
            ["operation_id", "project_id"],
            ["translation_operation.id", "translation_operation.project_id"],
            ondelete="CASCADE",
            name="fk_translation_block_workflow_config_operation_scope",
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["user.id"],
            ondelete="CASCADE",
            name="fk_translation_block_workflow_config_owner",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["translation_project.id"],
            ondelete="CASCADE",
            name="fk_translation_block_workflow_config_project",
        ),
        sa.ForeignKeyConstraint(
            ["source_plan_id", "project_id"],
            ["translation_source_plan.id", "translation_source_plan.project_id"],
            ondelete="RESTRICT",
            name="fk_translation_block_workflow_config_plan_scope",
        ),
    )
    op.create_index(
        "ix_translation_block_workflow_config_scope",
        "translation_block_workflow_config",
        ["owner_user_id", "project_id", "source_plan_id"],
        unique=False,
    )


def downgrade() -> None:
    bind = op.get_bind()
    if (
        bind.execute(
            sa.text("SELECT 1 FROM translation_block_workflow_config LIMIT 1")
        ).first()
        is not None
    ):
        raise RuntimeError(
            "Translator migration 018 cannot downgrade after workflow-config evidence exists."
        )
    if (
        bind.execute(
            sa.text("SELECT 1 FROM translation_window_attempt LIMIT 1")
        ).first()
        is not None
    ):
        raise RuntimeError(
            "Translator migration 018 cannot downgrade after window-fence evidence exists."
        )
    op.drop_index(
        "ix_translation_block_workflow_config_scope",
        table_name="translation_block_workflow_config",
    )
    op.drop_table("translation_block_workflow_config")
    with op.batch_alter_table("translation_window_attempt", recreate="always") as batch:
        batch.drop_constraint(
            "ck_translation_window_attempt_dispatch_state", type_="check"
        )
        for constraint in (
            "ck_translation_window_attempt_source_hash",
            "ck_translation_window_attempt_operation_version",
            "ck_translation_window_attempt_queue_job",
            "ck_translation_window_attempt_lease_fence",
        ):
            batch.drop_constraint(constraint, type_="check")
        for column in (
            "source_content_hash",
            "operation_version",
            "queue_job_id",
            "operation_lease_expires_at",
            "lease_fence",
        ):
            batch.drop_column(column)
        batch.create_check_constraint(
            "ck_translation_window_attempt_dispatch_state",
            "dispatch_state IN ('prepared', 'dispatched', 'response_received', "
            "'provider_failed', 'normalization_failed', 'fence_failed', "
            "'staging_failed', 'staged')",
        )
