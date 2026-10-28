"""Require retained-key integrity receipts for Translator block evidence.

Revision ID: 013_integrity_receipts
Revises: 012_analysis_attempts

This migration intentionally has no evidence conversion path.  Reinterpreting
the pre-013 public hashes as keyed receipts would otherwise make existing rows
look protected without having authenticated their decrypted contents.  The
block engine has not yet produced live evidence, so fail closed unless every
related table and every operation source-plan pin is empty.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "013_integrity_receipts"
down_revision = "012_analysis_attempts"
branch_labels = None
depends_on = None


_EVIDENCE_TABLES = (
    "translation_source_plan",
    "translation_block",
    "translation_source_anchor",
    "translation_target_block_revision",
    "translation_target_block_active",
    "translation_target_span_set",
    "translation_target_span",
    "translation_alignment_revision",
    "translation_alignment_group",
    "translation_target_alignment_active",
    "translation_window_attempt",
    "translation_window_attempt_event",
    "translation_window_attempt_revision",
    "translation_semantic_census",
    "translation_semantic_occurrence",
    "translation_context_artifact",
    "translation_context_artifact_revision",
    "translation_block_glossary_concept",
    "translation_block_glossary_revision",
    "translation_block_glossary_active",
    "translation_block_glossary_occurrence",
    "translation_context_baseline",
    "translation_context_baseline_item",
    "translation_context_impact",
    "translation_analysis_attempt",
    "translation_analysis_attempt_event",
)


def _assert_empty_evidence(*, direction: str) -> None:
    bind = op.get_bind()
    pinned = bind.execute(
        sa.text(
            "SELECT 1 FROM translation_operation "
            "WHERE source_plan_id IS NOT NULL OR source_plan_hash IS NOT NULL "
            "LIMIT 1"
        )
    ).first()
    if pinned is not None:
        raise RuntimeError(
            "Translator migration 013 cannot "
            f"{direction}: operation source pin evidence exists "
            "(a source-plan pin is still retained)."
        )
    for table_name in _EVIDENCE_TABLES:
        present = bind.execute(
            sa.text(f'SELECT 1 FROM "{table_name}" LIMIT 1')
        ).first()
        if present is not None:
            raise RuntimeError(
                "Translator migration 013 cannot "
                f"{direction} after block-engine evidence exists "
                f"in {table_name}."
            )


def upgrade() -> None:
    _assert_empty_evidence(direction="upgrade")

    with op.batch_alter_table(
        "translation_target_block_active",
        recreate="always",
        reflect_kwargs={"resolve_fks": False},
    ) as batch:
        batch.add_column(
            sa.Column("pair_receipt", sa.String(64), nullable=False),
            insert_after="version",
        )
        batch.create_check_constraint(
            "ck_translation_target_active_pair_receipt",
            "length(pair_receipt) = 64",
        )

    with op.batch_alter_table(
        "translation_window_attempt",
        recreate="always",
        reflect_kwargs={"resolve_fks": False},
    ) as batch:
        batch.add_column(
            sa.Column("settings_json", sa.Text(), nullable=False),
            insert_after="packet_json",
        )
        batch.add_column(
            sa.Column("state_head_receipt", sa.String(64), nullable=False),
            insert_after="attempt_record_hash",
        )
        batch.create_check_constraint(
            "ck_translation_window_attempt_state_head_receipt",
            "length(state_head_receipt) = 64",
        )

    with op.batch_alter_table(
        "translation_block_glossary_active",
        recreate="always",
        reflect_kwargs={"resolve_fks": False},
    ) as batch:
        batch.add_column(
            sa.Column("active_receipt", sa.String(64), nullable=False),
            insert_after="version",
        )
        batch.create_check_constraint(
            "ck_translation_block_glossary_active_receipt",
            "length(active_receipt) = 64",
        )

    with op.batch_alter_table(
        "translation_block_glossary_occurrence",
        recreate="always",
        reflect_kwargs={"resolve_fks": False},
    ) as batch:
        batch.add_column(
            sa.Column("record_receipt", sa.String(64), nullable=False),
            insert_after="occurrence_hash",
        )
        batch.create_check_constraint(
            "ck_translation_block_glossary_occurrence_receipt",
            "length(record_receipt) = 64",
        )

    with op.batch_alter_table(
        "translation_context_impact",
        recreate="always",
        reflect_kwargs={"resolve_fks": False},
    ) as batch:
        batch.add_column(
            sa.Column("record_receipt", sa.String(64), nullable=False),
            insert_after="impact_key",
        )
        batch.create_check_constraint(
            "ck_translation_context_impact_receipt",
            "length(record_receipt) = 64",
        )


def downgrade() -> None:
    _assert_empty_evidence(direction="downgrade")

    with op.batch_alter_table(
        "translation_context_impact",
        recreate="always",
        reflect_kwargs={"resolve_fks": False},
    ) as batch:
        batch.drop_constraint(
            "ck_translation_context_impact_receipt", type_="check"
        )
        batch.drop_column("record_receipt")

    with op.batch_alter_table(
        "translation_block_glossary_occurrence",
        recreate="always",
        reflect_kwargs={"resolve_fks": False},
    ) as batch:
        batch.drop_constraint(
            "ck_translation_block_glossary_occurrence_receipt", type_="check"
        )
        batch.drop_column("record_receipt")

    with op.batch_alter_table(
        "translation_block_glossary_active",
        recreate="always",
        reflect_kwargs={"resolve_fks": False},
    ) as batch:
        batch.drop_constraint(
            "ck_translation_block_glossary_active_receipt", type_="check"
        )
        batch.drop_column("active_receipt")

    with op.batch_alter_table(
        "translation_window_attempt",
        recreate="always",
        reflect_kwargs={"resolve_fks": False},
    ) as batch:
        batch.drop_constraint(
            "ck_translation_window_attempt_state_head_receipt", type_="check"
        )
        batch.drop_column("state_head_receipt")
        batch.drop_column("settings_json")

    with op.batch_alter_table(
        "translation_target_block_active",
        recreate="always",
        reflect_kwargs={"resolve_fks": False},
    ) as batch:
        batch.drop_constraint(
            "ck_translation_target_active_pair_receipt", type_="check"
        )
        batch.drop_column("pair_receipt")
