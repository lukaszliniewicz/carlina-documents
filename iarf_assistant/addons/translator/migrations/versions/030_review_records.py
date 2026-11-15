"""Add edit reasons, translator flags and QA finding dispositions.

Revision ID: 030_review_records
Revises: 029_epub_editions

All three tables are append-only, encrypted and integrity-hashed in the same
way as review coverage. Edit reasons sit beside the integrity-hashed target
revision instead of altering it. Flags and dispositions are event logs: the
current state of a flag or finding is its highest-sequence event, and the
unique sequence is the optimistic fence.
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.schema import SchemaItem

revision = "030_review_records"
down_revision = "029_epub_editions"
branch_labels = None
depends_on = None

EDIT_DEFECTS = (
    "'meaning', 'omission', 'addition', 'language', 'terminology', "
    "'consistency', 'brief', 'misreading', 'formatting'"
)
FLAG_KINDS = "'assumption', 'source_defect', 'unverified', 'departure', 'revisit'"
_TABLES = (
    "translation_qa_disposition",
    "translation_review_flag_event",
    "translation_target_edit_reason",
)


def _scope_columns() -> list[sa.Column[Any]]:
    return [
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("source_plan_id", sa.String(length=36), nullable=False),
        sa.Column("block_id", sa.String(length=128), nullable=False),
        sa.Column("owner_user_id", sa.Integer(), nullable=False),
    ]


def _actor_columns() -> list[sa.Column[Any]]:
    return [
        sa.Column("actor_type", sa.String(length=16), nullable=False),
        sa.Column("actor_user_id", sa.Integer(), nullable=True),
        sa.Column("share_grant_id", sa.String(length=36), nullable=True),
    ]


def _integrity_columns() -> list[sa.Column[Any]]:
    return [
        # ContentType is physically TEXT; the ORM owns envelope handling.
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("record_hash", sa.String(length=64), nullable=False),
        sa.Column("integrity_receipt", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    ]


def _scope_constraints(table: str) -> list[SchemaItem]:
    return [
        sa.CheckConstraint("length(record_hash) = 64", name=f"ck_{table}_record_hash"),
        sa.CheckConstraint(
            "length(integrity_receipt) = 64", name=f"ck_{table}_integrity_receipt"
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["translation_project.id"],
            ondelete="CASCADE",
            name=f"fk_{table}_project",
        ),
        sa.ForeignKeyConstraint(
            ["source_plan_id", "project_id"],
            ["translation_source_plan.id", "translation_source_plan.project_id"],
            ondelete="CASCADE",
            name=f"fk_{table}_plan_scope",
        ),
        sa.ForeignKeyConstraint(
            ["block_id", "source_plan_id"],
            ["translation_block.id", "translation_block.source_plan_id"],
            ondelete="CASCADE",
            name=f"fk_{table}_block_plan",
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["user.id"],
            ondelete="CASCADE",
            name=f"fk_{table}_owner",
        ),
    ]


def _actor_constraints(table: str) -> list[SchemaItem]:
    return [
        sa.CheckConstraint(
            "actor_type IN ('owner', 'reviewer')", name=f"ck_{table}_actor_type"
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["user.id"],
            ondelete="SET NULL",
            name=f"fk_{table}_actor",
        ),
        sa.ForeignKeyConstraint(
            ["share_grant_id"],
            ["translation_share_grant.id"],
            ondelete="SET NULL",
            name=f"fk_{table}_share_grant",
        ),
    ]


def upgrade() -> None:
    table = "translation_target_edit_reason"
    op.create_table(
        "translation_target_edit_reason",
        sa.Column("target_revision_id", sa.String(length=36), nullable=False),
        *_scope_columns(),
        sa.Column("defect", sa.String(length=24), nullable=False),
        *_integrity_columns(),
        sa.PrimaryKeyConstraint("target_revision_id", name=f"pk_{table}"),
        sa.CheckConstraint(f"defect IN ({EDIT_DEFECTS})", name=f"ck_{table}_defect"),
        sa.ForeignKeyConstraint(
            ["target_revision_id"],
            ["translation_target_block_revision.id"],
            ondelete="CASCADE",
            name=f"fk_{table}_target_revision",
        ),
        *_scope_constraints(table),
    )

    table = "translation_review_flag_event"
    op.create_table(
        "translation_review_flag_event",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("flag_id", sa.String(length=36), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        *_scope_columns(),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("kind", sa.String(length=24), nullable=False),
        sa.Column("origin", sa.String(length=16), nullable=False),
        *_actor_columns(),
        *_integrity_columns(),
        sa.PrimaryKeyConstraint("id", name=f"pk_{table}"),
        sa.UniqueConstraint("flag_id", "sequence", name=f"uq_{table}_sequence"),
        sa.CheckConstraint("sequence >= 1", name=f"ck_{table}_sequence"),
        sa.CheckConstraint(
            "action IN ('open', 'resolve', 'dismiss', 'reopen')",
            name=f"ck_{table}_action",
        ),
        sa.CheckConstraint(
            "(sequence = 1) = (action = 'open')", name=f"ck_{table}_open_first"
        ),
        sa.CheckConstraint(f"kind IN ({FLAG_KINDS})", name=f"ck_{table}_kind"),
        sa.CheckConstraint(
            "origin IN ('agent', 'human', 'translation')", name=f"ck_{table}_origin"
        ),
        *_scope_constraints(table),
        *_actor_constraints(table),
    )
    op.create_index(
        "ix_translation_review_flag_event_plan_block",
        "translation_review_flag_event",
        ["source_plan_id", "block_id"],
        unique=False,
    )

    table = "translation_qa_disposition"
    op.create_table(
        "translation_qa_disposition",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("finding_id", sa.String(length=24), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        *_scope_columns(),
        sa.Column("target_revision_id", sa.String(length=36), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("basis", sa.String(length=16), nullable=True),
        *_actor_columns(),
        *_integrity_columns(),
        sa.PrimaryKeyConstraint("id", name=f"pk_{table}"),
        sa.UniqueConstraint(
            "source_plan_id", "finding_id", "sequence", name=f"uq_{table}_sequence"
        ),
        sa.CheckConstraint("sequence >= 1", name=f"ck_{table}_sequence"),
        sa.CheckConstraint(
            "action IN ('dismiss', 'reopen')", name=f"ck_{table}_action"
        ),
        sa.CheckConstraint(
            "(action = 'dismiss' AND basis IN ('false_positive', 'intended')) OR "
            "(action = 'reopen' AND basis IS NULL)",
            name=f"ck_{table}_basis",
        ),
        sa.ForeignKeyConstraint(
            ["target_revision_id"],
            ["translation_target_block_revision.id"],
            ondelete="CASCADE",
            name=f"fk_{table}_target_revision",
        ),
        *_scope_constraints(table),
        *_actor_constraints(table),
    )
    op.create_index(
        "ix_translation_qa_disposition_plan_block",
        "translation_qa_disposition",
        ["source_plan_id", "block_id"],
        unique=False,
    )


def downgrade() -> None:
    bind = op.get_bind()
    for table in _TABLES:
        count = bind.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
        if count:
            raise RuntimeError(
                f"cannot downgrade review records while {table} has rows"
            )
    op.drop_index(
        "ix_translation_qa_disposition_plan_block",
        table_name="translation_qa_disposition",
    )
    op.drop_table("translation_qa_disposition")
    op.drop_index(
        "ix_translation_review_flag_event_plan_block",
        table_name="translation_review_flag_event",
    )
    op.drop_table("translation_review_flag_event")
    op.drop_table("translation_target_edit_reason")
