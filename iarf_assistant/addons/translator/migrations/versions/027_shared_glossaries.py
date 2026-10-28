"""Owner-scoped encrypted shared glossary assets and review receipts.

Revision ID: 027_shared_glossaries
Revises: 026_numbering_overlay
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "027_shared_glossaries"
down_revision = "026_numbering_overlay"
branch_labels = None
depends_on = None


def _id() -> sa.Column:
    return sa.Column("id", sa.String(36), primary_key=True)


def _owner() -> sa.Column:
    return sa.Column("owner_user_id", sa.Integer(), sa.ForeignKey("user.id", ondelete="CASCADE"), nullable=False)


def upgrade() -> None:
    op.create_table(
        "translation_shared_glossary", _id(), _owner(),
        sa.Column("current_revision", sa.Integer(), nullable=False),
        sa.Column("metadata_json", sa.Text(), nullable=False),
        sa.Column("metadata_receipt", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("current_revision >= 1", name="ck_shared_glossary_revision"),
    )
    op.create_index("ix_translation_shared_glossary_owner_user_id", "translation_shared_glossary", ["owner_user_id"])
    op.create_table(
        "translation_shared_glossary_revision", _id(), _owner(),
        sa.Column("asset_id", sa.String(36), sa.ForeignKey("translation_shared_glossary.id", ondelete="CASCADE"), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("receipt", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("asset_id", "revision", name="uq_translation_shared_glossary_revision"),
    )
    op.create_table(
        "translation_shared_glossary_proposal", _id(), _owner(),
        sa.Column("asset_id", sa.String(36), sa.ForeignKey("translation_shared_glossary.id", ondelete="CASCADE"), nullable=False),
        sa.Column("base_revision", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("receipt", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("state IN ('pending','accepted','rejected')", name="ck_shared_glossary_proposal_state"),
    )
    op.create_table(
        "translation_project_glossary_binding",
        sa.Column("project_id", sa.String(36), sa.ForeignKey("translation_project.id", ondelete="CASCADE"), primary_key=True),
        _owner(),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("source_plan_id", sa.String(36), nullable=False),
        sa.Column("asset_id", sa.String(36), sa.ForeignKey("translation_shared_glossary.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("adopted_revision", sa.Integer(), nullable=True),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("binding_receipt", sa.String(64), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("version >= 1", name="ck_project_glossary_binding_version"),
    )
    op.create_table(
        "translation_shared_glossary_receipt", _id(), _owner(),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("retry_key", sa.String(160), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("integrity_receipt", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("owner_user_id", "action", "retry_key", name="uq_translation_shared_glossary_retry"),
    )


def downgrade() -> None:
    tables = (
        "translation_shared_glossary_receipt", "translation_project_glossary_binding",
        "translation_shared_glossary_proposal", "translation_shared_glossary_revision",
        "translation_shared_glossary",
    )
    for table in tables:
        if op.get_bind().execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first() is not None:
            raise RuntimeError("Translator migration 027 cannot downgrade with shared glossary data.")
    for table in tables:
        op.drop_table(table)
