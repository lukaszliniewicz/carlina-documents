"""Add explicit active source plans and append-only correction receipts.

Revision ID: 020_source_plan_corrections
Revises: 019_operation_evidence_reuse
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "020_source_plan_corrections"
down_revision = "019_operation_evidence_reuse"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    ambiguous = bind.execute(
        sa.text(
            "SELECT p.id AS project_id, COUNT(s.id) AS plan_count "
            "FROM translation_project AS p LEFT JOIN translation_source_plan AS s "
            "ON s.project_id = p.id "
            "WHERE p.engine_contract = 'document-block-v1' "
            "GROUP BY p.id HAVING COUNT(s.id) > 1"
        )
    ).mappings().all()
    if ambiguous:
        raise RuntimeError(
            "Translator migration 020 cannot infer an active source plan for "
            "a project that already retains multiple source plans."
        )

    op.create_table(
        "translation_source_plan_active",
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("source_plan_id", sa.String(length=36), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint(
            "project_id", name="pk_translation_source_plan_active"
        ),
        sa.UniqueConstraint(
            "source_plan_id", name="uq_translation_source_plan_active_plan"
        ),
        sa.CheckConstraint(
            "version >= 1", name="ck_translation_source_plan_active_version"
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["translation_project.id"],
            ondelete="CASCADE",
            name="fk_translation_source_plan_active_project",
        ),
        sa.ForeignKeyConstraint(
            ["source_plan_id", "project_id"],
            ["translation_source_plan.id", "translation_source_plan.project_id"],
            ondelete="RESTRICT",
            name="fk_translation_source_plan_active_plan",
        ),
    )
    bind.execute(
        sa.text(
            "INSERT INTO translation_source_plan_active "
            "(project_id, source_plan_id, version, updated_at) "
            "SELECT s.project_id, s.id, 1, s.created_at "
            "FROM translation_source_plan AS s "
            "JOIN translation_project AS p ON p.id = s.project_id "
            "WHERE p.engine_contract = 'document-block-v1'"
        )
    )

    op.create_table(
        "translation_source_plan_correction",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("contract_version", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("owner_user_id", sa.Integer(), nullable=False),
        sa.Column("predecessor_source_plan_id", sa.String(length=36), nullable=False),
        sa.Column("successor_source_plan_id", sa.String(length=36), nullable=False),
        sa.Column("active_version", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        # EncryptedType is physically TEXT; the ORM owns envelope handling.
        sa.Column("change_json", sa.Text(), nullable=False),
        sa.Column("target_reuse_json", sa.Text(), nullable=False),
        sa.Column("record_receipt", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint(
            "id", name="pk_translation_source_plan_correction"
        ),
        sa.UniqueConstraint(
            "project_id",
            "idempotency_key",
            name="uq_translation_source_plan_correction_idempotency",
        ),
        sa.CheckConstraint(
            "contract_version = 'translation-source-plan-correction-v1'",
            name="ck_translation_source_plan_correction_contract",
        ),
        sa.CheckConstraint(
            "action IN ('merge_previous', 'merge_next', 'exclude')",
            name="ck_translation_source_plan_correction_action",
        ),
        sa.CheckConstraint(
            "active_version >= 2",
            name="ck_translation_source_plan_correction_version",
        ),
        sa.CheckConstraint(
            "predecessor_source_plan_id != successor_source_plan_id",
            name="ck_translation_source_plan_correction_distinct",
        ),
        sa.CheckConstraint(
            "length(request_fingerprint) = 64",
            name="ck_translation_source_plan_correction_request_hash",
        ),
        sa.CheckConstraint(
            "length(record_receipt) = 64",
            name="ck_translation_source_plan_correction_receipt",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["translation_project.id"],
            ondelete="CASCADE",
            name="fk_translation_source_plan_correction_project",
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["user.id"],
            ondelete="CASCADE",
            name="fk_translation_source_plan_correction_owner",
        ),
        sa.ForeignKeyConstraint(
            ["predecessor_source_plan_id", "project_id"],
            ["translation_source_plan.id", "translation_source_plan.project_id"],
            ondelete="RESTRICT",
            name="fk_translation_source_plan_correction_predecessor",
        ),
        sa.ForeignKeyConstraint(
            ["successor_source_plan_id", "project_id"],
            ["translation_source_plan.id", "translation_source_plan.project_id"],
            ondelete="RESTRICT",
            name="fk_translation_source_plan_correction_successor",
        ),
    )
    op.create_index(
        "ix_translation_source_plan_correction_project_created",
        "translation_source_plan_correction",
        ["project_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.execute(
        sa.text("SELECT 1 FROM translation_source_plan_correction LIMIT 1")
    ).first() is not None:
        raise RuntimeError(
            "Translator migration 020 cannot downgrade after source-plan "
            "correction receipts exist."
        )
    if bind.execute(
        sa.text(
            "SELECT 1 FROM translation_source_plan GROUP BY project_id "
            "HAVING COUNT(*) > 1 LIMIT 1"
        )
    ).first() is not None:
        raise RuntimeError(
            "Translator migration 020 cannot downgrade after a project retains "
            "multiple source plans."
        )
    op.drop_index(
        "ix_translation_source_plan_correction_project_created",
        table_name="translation_source_plan_correction",
    )
    op.drop_table("translation_source_plan_correction")
    op.drop_table("translation_source_plan_active")
