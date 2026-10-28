"""Persist scholarly research, review, sharing, and retranslation state.

Revision ID: 024_scholarly_review
Revises: 023_passive_window_pending
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "024_scholarly_review"
down_revision = "023_passive_window_pending"
branch_labels = None
depends_on = None


def _reference_check(*, include_dossier: bool) -> sa.CheckConstraint:
    dossier_column_clause = " AND research_dossier_revision_id IS NULL" if include_dossier else ""
    dossier_clause = (
        " OR (item_kind = 'research_dossier' "
        "AND artifact_revision_id IS NULL AND glossary_revision_id IS NULL "
        "AND research_run_id IS NULL AND embedding_generation_id IS NULL "
        "AND research_dossier_revision_id IS NOT NULL "
        "AND reference_json IS NOT NULL)"
        if include_dossier
        else ""
    )
    return sa.CheckConstraint(
        "(item_kind IN ('document_brief', 'style_guide', 'entity_register') "
        "AND artifact_revision_id IS NOT NULL AND glossary_revision_id IS NULL "
        "AND research_run_id IS NULL AND embedding_generation_id IS NULL "
        + dossier_column_clause
        + " AND reference_json IS NULL) OR "
        "(item_kind = 'glossary_locked' "
        "AND artifact_revision_id IS NULL AND glossary_revision_id IS NOT NULL "
        "AND research_run_id IS NULL AND embedding_generation_id IS NULL "
        + dossier_column_clause
        + " AND reference_json IS NULL) OR "
        "(item_kind = 'research_evidence' "
        "AND artifact_revision_id IS NULL AND glossary_revision_id IS NULL "
        "AND research_run_id IS NOT NULL AND embedding_generation_id IS NULL "
        + dossier_column_clause
        + " AND reference_json IS NOT NULL) OR "
        "(item_kind = 'translation_memory' "
        "AND artifact_revision_id IS NULL AND glossary_revision_id IS NULL "
        "AND research_run_id IS NULL AND embedding_generation_id IS NOT NULL "
        + dossier_column_clause
        + " AND reference_json IS NOT NULL)"
        + dossier_clause,
        name="ck_translation_context_baseline_item_reference_kind",
    )


def _item_kind_check(*, include_dossier: bool) -> sa.CheckConstraint:
    kinds = (
        "'document_brief', 'style_guide', 'entity_register', 'glossary_locked', "
        "'research_evidence', 'translation_memory', 'research_dossier'"
        if include_dossier
        else "'document_brief', 'style_guide', 'entity_register', 'glossary_locked', "
        "'research_evidence', 'translation_memory'"
    )
    return sa.CheckConstraint(
        f"item_kind IN ({kinds})",
        name="ck_translation_context_baseline_item_kind",
    )


def upgrade() -> None:
    op.create_table(
        "translation_research_dossier",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("source_plan_id", sa.String(length=36), nullable=False),
        sa.Column("owner_user_id", sa.Integer(), nullable=False),
        sa.Column("scope_kind", sa.String(length=16), nullable=False),
        # EncryptedType is physically TEXT; the ORM owns envelope handling.
        sa.Column("scope_block_ids", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_translation_research_dossier"),
        sa.UniqueConstraint(
            "id",
            "project_id",
            "source_plan_id",
            name="uq_translation_research_dossier_scope",
        ),
        sa.CheckConstraint(
            "scope_kind IN ('document', 'blocks')",
            name="ck_translation_research_dossier_scope_kind",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["translation_project.id"],
            ondelete="CASCADE",
            name="fk_translation_research_dossier_project",
        ),
        sa.ForeignKeyConstraint(
            ["source_plan_id", "project_id"],
            ["translation_source_plan.id", "translation_source_plan.project_id"],
            ondelete="CASCADE",
            name="fk_translation_research_dossier_plan_scope",
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["user.id"],
            ondelete="CASCADE",
            name="fk_translation_research_dossier_owner",
        ),
    )
    op.create_index(
        "ix_translation_research_dossier_plan_created",
        "translation_research_dossier",
        ["source_plan_id", "created_at"],
        unique=False,
    )

    op.create_table(
        "translation_share_grant",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("owner_user_id", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(length=128), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("token_digest", sa.String(length=128), nullable=False),
        sa.Column("token_hint", sa.String(length=8), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_translation_share_grant"),
        sa.CheckConstraint(
            "length(label) BETWEEN 1 AND 128",
            name="ck_translation_share_grant_label",
        ),
        sa.CheckConstraint(
            "role IN ('viewer', 'reviewer')",
            name="ck_translation_share_grant_role",
        ),
        sa.CheckConstraint(
            "length(token_digest) = 128",
            name="ck_translation_share_grant_token_digest",
        ),
        sa.CheckConstraint(
            "length(token_hint) BETWEEN 1 AND 8",
            name="ck_translation_share_grant_token_hint",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'revoked')",
            name="ck_translation_share_grant_status",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["translation_project.id"],
            ondelete="CASCADE",
            name="fk_translation_share_grant_project",
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["user.id"],
            ondelete="CASCADE",
            name="fk_translation_share_grant_owner",
        ),
    )
    op.create_index(
        "uq_translation_share_grant_token_digest",
        "translation_share_grant",
        ["token_digest"],
        unique=True,
    )
    op.create_index(
        "ix_translation_share_grant_project_status",
        "translation_share_grant",
        ["project_id", "status"],
        unique=False,
    )

    op.create_table(
        "translation_research_dossier_revision",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("dossier_id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("source_plan_id", sa.String(length=36), nullable=False),
        sa.Column("revision_number", sa.Integer(), nullable=False),
        sa.Column("parent_revision_id", sa.String(length=36), nullable=True),
        sa.Column("client_submission_id", sa.String(length=128), nullable=False),
        sa.Column("content_digest", sa.String(length=64), nullable=False),
        sa.Column("conclusion_provenance", sa.String(length=24), nullable=False),
        sa.Column("evidence_verification_state", sa.String(length=16), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("provenance_json", sa.Text(), nullable=False),
        sa.Column("record_hash", sa.String(length=64), nullable=False),
        sa.Column("integrity_receipt", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_translation_research_dossier_revision"),
        sa.UniqueConstraint(
            "dossier_id",
            "revision_number",
            name="uq_translation_research_dossier_revision_number",
        ),
        sa.UniqueConstraint(
            "project_id",
            "client_submission_id",
            name="uq_translation_research_dossier_revision_submission",
        ),
        sa.UniqueConstraint(
            "id",
            "project_id",
            "source_plan_id",
            name="uq_translation_research_dossier_revision_scope",
        ),
        sa.CheckConstraint(
            "revision_number >= 1",
            name="ck_translation_research_dossier_revision_number",
        ),
        sa.CheckConstraint(
            "length(client_submission_id) BETWEEN 1 AND 128",
            name="ck_translation_research_dossier_revision_submission",
        ),
        sa.CheckConstraint(
            "length(content_digest) = 64",
            name="ck_translation_research_dossier_revision_content_digest",
        ),
        sa.CheckConstraint(
            "conclusion_provenance IN ('external_unverified', 'managed_verified')",
            name="ck_translation_research_dossier_revision_conclusion_provenance",
        ),
        sa.CheckConstraint(
            "evidence_verification_state IN ('unverified', 'partial', 'verified')",
            name="ck_translation_research_dossier_revision_verification_state",
        ),
        sa.CheckConstraint(
            "length(record_hash) = 64",
            name="ck_translation_research_dossier_revision_record_hash",
        ),
        sa.CheckConstraint(
            "length(integrity_receipt) = 64",
            name="ck_translation_research_dossier_revision_integrity_receipt",
        ),
        sa.ForeignKeyConstraint(
            ["dossier_id", "project_id", "source_plan_id"],
            [
                "translation_research_dossier.id",
                "translation_research_dossier.project_id",
                "translation_research_dossier.source_plan_id",
            ],
            ondelete="CASCADE",
            name="fk_translation_research_dossier_revision_dossier_scope",
        ),
        sa.ForeignKeyConstraint(
            ["parent_revision_id"],
            ["translation_research_dossier_revision.id"],
            ondelete="RESTRICT",
            name="fk_translation_research_dossier_revision_parent",
        ),
    )
    op.create_index(
        "ix_translation_research_dossier_revision_dossier_number",
        "translation_research_dossier_revision",
        ["dossier_id", "revision_number"],
        unique=False,
    )
    op.create_index(
        "ix_translation_research_dossier_revision_plan_created",
        "translation_research_dossier_revision",
        ["source_plan_id", "created_at"],
        unique=False,
    )

    op.create_table(
        "translation_share_session",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("grant_id", sa.String(length=36), nullable=False),
        sa.Column("session_digest", sa.String(length=128), nullable=False),
        sa.Column("csrf_digest", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_translation_share_session"),
        sa.CheckConstraint(
            "length(session_digest) = 128",
            name="ck_translation_share_session_digest",
        ),
        sa.CheckConstraint(
            "length(csrf_digest) = 128",
            name="ck_translation_share_session_csrf_digest",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'revoked')",
            name="ck_translation_share_session_status",
        ),
        sa.ForeignKeyConstraint(
            ["grant_id"],
            ["translation_share_grant.id"],
            ondelete="CASCADE",
            name="fk_translation_share_session_grant",
        ),
    )
    op.create_index(
        "uq_translation_share_session_digest",
        "translation_share_session",
        ["session_digest"],
        unique=True,
    )
    op.create_index(
        "ix_translation_share_session_grant_status",
        "translation_share_session",
        ["grant_id", "status"],
        unique=False,
    )

    op.create_table(
        "translation_reader_note",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("source_plan_id", sa.String(length=36), nullable=False),
        sa.Column("block_id", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_translation_reader_note"),
        sa.UniqueConstraint(
            "id",
            "project_id",
            "source_plan_id",
            "block_id",
            name="uq_translation_reader_note_scope",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["translation_project.id"],
            ondelete="CASCADE",
            name="fk_translation_reader_note_project",
        ),
        sa.ForeignKeyConstraint(
            ["source_plan_id", "project_id"],
            ["translation_source_plan.id", "translation_source_plan.project_id"],
            ondelete="CASCADE",
            name="fk_translation_reader_note_plan_scope",
        ),
        sa.ForeignKeyConstraint(
            ["block_id", "source_plan_id"],
            ["translation_block.id", "translation_block.source_plan_id"],
            ondelete="CASCADE",
            name="fk_translation_reader_note_block_scope",
        ),
    )
    op.create_index(
        "ix_translation_reader_note_plan_block_created",
        "translation_reader_note",
        ["source_plan_id", "block_id", "created_at"],
        unique=False,
    )

    op.create_table(
        "translation_reader_note_revision",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("note_id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("source_plan_id", sa.String(length=36), nullable=False),
        sa.Column("block_id", sa.String(length=128), nullable=False),
        sa.Column("revision_number", sa.Integer(), nullable=False),
        sa.Column("parent_revision_id", sa.String(length=36), nullable=True),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("anchor_kind", sa.String(length=16), nullable=False),
        sa.Column("target_revision_id", sa.String(length=36), nullable=True),
        sa.Column("anchor_text", sa.Text(), nullable=True),
        sa.Column("occurrence_index", sa.Integer(), nullable=True),
        sa.Column("witness_before", sa.Text(), nullable=True),
        sa.Column("witness_after", sa.Text(), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("citations_json", sa.Text(), nullable=False),
        sa.Column("author_type", sa.String(length=16), nullable=False),
        sa.Column("author_user_id", sa.Integer(), nullable=True),
        sa.Column("share_grant_id", sa.String(length=36), nullable=True),
        sa.Column("record_hash", sa.String(length=64), nullable=False),
        sa.Column("integrity_receipt", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_translation_reader_note_revision"),
        sa.UniqueConstraint(
            "id", "note_id", name="uq_translation_reader_note_revision_note"
        ),
        sa.UniqueConstraint(
            "note_id",
            "revision_number",
            name="uq_translation_reader_note_revision_number",
        ),
        sa.CheckConstraint(
            "revision_number >= 1",
            name="ck_translation_reader_note_revision_number",
        ),
        sa.CheckConstraint(
            "state IN ('proposed', 'active', 'retired', 'needs_review')",
            name="ck_translation_reader_note_revision_state",
        ),
        sa.CheckConstraint(
            "anchor_kind IN ('block', 'source', 'target')",
            name="ck_translation_reader_note_revision_anchor_kind",
        ),
        sa.CheckConstraint(
            "occurrence_index IS NULL OR occurrence_index >= 0",
            name="ck_translation_reader_note_revision_occurrence",
        ),
        sa.CheckConstraint(
            "(anchor_kind = 'block' AND target_revision_id IS NULL AND occurrence_index IS NULL) OR "
            "(anchor_kind = 'source' AND target_revision_id IS NULL) OR "
            "(anchor_kind = 'target' AND target_revision_id IS NOT NULL "
            "AND anchor_text IS NOT NULL AND occurrence_index IS NOT NULL)",
            name="ck_translation_reader_note_revision_anchor_shape",
        ),
        sa.CheckConstraint(
            "author_type IN ('owner', 'reviewer', 'model', 'system')",
            name="ck_translation_reader_note_revision_author_type",
        ),
        sa.CheckConstraint(
            "length(record_hash) = 64",
            name="ck_translation_reader_note_revision_record_hash",
        ),
        sa.CheckConstraint(
            "length(integrity_receipt) = 64",
            name="ck_translation_reader_note_revision_integrity_receipt",
        ),
        sa.ForeignKeyConstraint(
            ["note_id", "project_id", "source_plan_id", "block_id"],
            [
                "translation_reader_note.id",
                "translation_reader_note.project_id",
                "translation_reader_note.source_plan_id",
                "translation_reader_note.block_id",
            ],
            ondelete="CASCADE",
            name="fk_translation_reader_note_revision_note_scope",
        ),
        sa.ForeignKeyConstraint(
            ["parent_revision_id", "note_id"],
            [
                "translation_reader_note_revision.id",
                "translation_reader_note_revision.note_id",
            ],
            ondelete="RESTRICT",
            name="fk_translation_reader_note_revision_parent",
        ),
        sa.ForeignKeyConstraint(
            ["target_revision_id", "source_plan_id", "block_id"],
            [
                "translation_target_block_revision.id",
                "translation_target_block_revision.source_plan_id",
                "translation_target_block_revision.block_id",
            ],
            ondelete="RESTRICT",
            name="fk_translation_reader_note_revision_target",
        ),
        sa.ForeignKeyConstraint(
            ["author_user_id"],
            ["user.id"],
            ondelete="SET NULL",
            name="fk_translation_reader_note_revision_author",
        ),
        sa.ForeignKeyConstraint(
            ["share_grant_id"],
            ["translation_share_grant.id"],
            ondelete="SET NULL",
            name="fk_translation_reader_note_revision_share_grant",
        ),
    )
    op.create_index(
        "ix_translation_reader_note_revision_plan_block_created",
        "translation_reader_note_revision",
        ["source_plan_id", "block_id", "created_at"],
        unique=False,
    )

    op.create_table(
        "translation_reader_note_head",
        sa.Column("note_id", sa.String(length=36), nullable=False),
        sa.Column("revision_id", sa.String(length=36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("note_id", name="pk_translation_reader_note_head"),
        sa.UniqueConstraint(
            "revision_id", name="uq_translation_reader_note_head_revision"
        ),
        sa.CheckConstraint(
            "version >= 1", name="ck_translation_reader_note_head_version"
        ),
        sa.ForeignKeyConstraint(
            ["note_id"],
            ["translation_reader_note.id"],
            ondelete="CASCADE",
            name="fk_translation_reader_note_head_note",
        ),
        sa.ForeignKeyConstraint(
            ["revision_id", "note_id"],
            [
                "translation_reader_note_revision.id",
                "translation_reader_note_revision.note_id",
            ],
            ondelete="RESTRICT",
            name="fk_translation_reader_note_head_revision",
        ),
    )

    op.create_table(
        "translation_research_dossier_glossary_proposal",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("source_plan_id", sa.String(length=36), nullable=False),
        sa.Column("dossier_revision_id", sa.String(length=36), nullable=False),
        sa.Column("glossary_revision_id", sa.String(length=36), nullable=False),
        sa.Column("order_index", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint(
            "id", name="pk_translation_research_dossier_glossary_proposal"
        ),
        sa.UniqueConstraint(
            "dossier_revision_id",
            "glossary_revision_id",
            name="uq_translation_research_dossier_glossary_proposal_pair",
        ),
        sa.CheckConstraint(
            "order_index >= 0",
            name="ck_translation_research_dossier_glossary_proposal_order",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["translation_project.id"],
            ondelete="CASCADE",
            name="fk_translation_research_dossier_glossary_proposal_project",
        ),
        sa.ForeignKeyConstraint(
            ["source_plan_id", "project_id"],
            ["translation_source_plan.id", "translation_source_plan.project_id"],
            ondelete="CASCADE",
            name="fk_translation_research_dossier_glossary_proposal_plan_scope",
        ),
        sa.ForeignKeyConstraint(
            ["dossier_revision_id", "project_id", "source_plan_id"],
            [
                "translation_research_dossier_revision.id",
                "translation_research_dossier_revision.project_id",
                "translation_research_dossier_revision.source_plan_id",
            ],
            ondelete="RESTRICT",
            name="fk_translation_research_dossier_glossary_proposal_dossier",
        ),
        sa.ForeignKeyConstraint(
            ["glossary_revision_id", "project_id", "source_plan_id"],
            [
                "translation_block_glossary_revision.id",
                "translation_block_glossary_revision.project_id",
                "translation_block_glossary_revision.source_plan_id",
            ],
            ondelete="RESTRICT",
            name="fk_translation_research_dossier_glossary_proposal_glossary",
        ),
    )
    op.create_index(
        "ix_translation_research_dossier_glossary_proposal_plan",
        "translation_research_dossier_glossary_proposal",
        ["source_plan_id", "dossier_revision_id", "order_index"],
        unique=False,
    )

    op.create_table(
        "translation_block_glossary_suggestion",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("source_plan_id", sa.String(length=36), nullable=False),
        sa.Column("source_term", sa.Text(), nullable=False),
        sa.Column("target_term", sa.Text(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("proposer_kind", sa.String(length=16), nullable=False),
        sa.Column("proposer_user_id", sa.Integer(), nullable=True),
        sa.Column("share_grant_id", sa.String(length=36), nullable=True),
        sa.Column("dossier_revision_id", sa.String(length=36), nullable=True),
        sa.Column("state", sa.String(length=16), nullable=False, server_default="proposed"),
        sa.Column("accepted_revision_id", sa.String(length=36), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_translation_block_glossary_suggestion"),
        sa.CheckConstraint(
            "proposer_kind IN ('reviewer', 'external')",
            name="ck_translation_block_glossary_suggestion_proposer_kind",
        ),
        sa.CheckConstraint(
            "state IN ('proposed', 'accepted', 'rejected')",
            name="ck_translation_block_glossary_suggestion_state",
        ),
        sa.CheckConstraint(
            "version >= 1",
            name="ck_translation_block_glossary_suggestion_version",
        ),
        sa.CheckConstraint(
            "(proposer_kind = 'reviewer' AND share_grant_id IS NOT NULL "
            "AND dossier_revision_id IS NULL) OR "
            "(proposer_kind = 'external' AND dossier_revision_id IS NOT NULL "
            "AND share_grant_id IS NULL)",
            name="ck_translation_block_glossary_suggestion_provenance",
        ),
        sa.CheckConstraint(
            "(state = 'accepted' AND accepted_revision_id IS NOT NULL) OR "
            "(state <> 'accepted' AND accepted_revision_id IS NULL)",
            name="ck_translation_block_glossary_suggestion_acceptance",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["translation_project.id"],
            ondelete="CASCADE",
            name="fk_translation_block_glossary_suggestion_project",
        ),
        sa.ForeignKeyConstraint(
            ["source_plan_id", "project_id"],
            ["translation_source_plan.id", "translation_source_plan.project_id"],
            ondelete="CASCADE",
            name="fk_translation_block_glossary_suggestion_plan_scope",
        ),
        sa.ForeignKeyConstraint(
            ["proposer_user_id"],
            ["user.id"],
            ondelete="SET NULL",
            name="fk_translation_block_glossary_suggestion_proposer",
        ),
        sa.ForeignKeyConstraint(
            ["share_grant_id"],
            ["translation_share_grant.id"],
            ondelete="SET NULL",
            name="fk_translation_block_glossary_suggestion_share_grant",
        ),
        sa.ForeignKeyConstraint(
            ["dossier_revision_id", "project_id", "source_plan_id"],
            [
                "translation_research_dossier_revision.id",
                "translation_research_dossier_revision.project_id",
                "translation_research_dossier_revision.source_plan_id",
            ],
            ondelete="RESTRICT",
            name="fk_translation_block_glossary_suggestion_dossier",
        ),
        sa.ForeignKeyConstraint(
            ["accepted_revision_id", "project_id", "source_plan_id"],
            [
                "translation_block_glossary_revision.id",
                "translation_block_glossary_revision.project_id",
                "translation_block_glossary_revision.source_plan_id",
            ],
            ondelete="RESTRICT",
            name="fk_translation_block_glossary_suggestion_accepted_revision",
        ),
    )
    op.create_index(
        "ix_translation_block_glossary_suggestion_plan_state_created",
        "translation_block_glossary_suggestion",
        ["source_plan_id", "state", "created_at"],
        unique=False,
    )

    op.create_table(
        "translation_block_review_revision",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("source_plan_id", sa.String(length=36), nullable=False),
        sa.Column("block_id", sa.String(length=128), nullable=False),
        sa.Column("revision_number", sa.Integer(), nullable=False),
        sa.Column("parent_revision_id", sa.String(length=36), nullable=True),
        sa.Column("target_revision_id", sa.String(length=36), nullable=True),
        sa.Column("decision", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("author_type", sa.String(length=16), nullable=False),
        sa.Column("author_user_id", sa.Integer(), nullable=True),
        sa.Column("share_grant_id", sa.String(length=36), nullable=True),
        sa.Column("record_hash", sa.String(length=64), nullable=False),
        sa.Column("integrity_receipt", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_translation_block_review_revision"),
        sa.UniqueConstraint(
            "project_id",
            "source_plan_id",
            "block_id",
            "revision_number",
            name="uq_translation_block_review_revision_number",
        ),
        sa.UniqueConstraint(
            "id",
            "project_id",
            "source_plan_id",
            "block_id",
            name="uq_translation_block_review_revision_scope",
        ),
        sa.CheckConstraint(
            "revision_number >= 1",
            name="ck_translation_block_review_revision_number",
        ),
        sa.CheckConstraint(
            "decision IN ('pending', 'approved', 'needs_changes')",
            name="ck_translation_block_review_revision_decision",
        ),
        sa.CheckConstraint(
            "author_type IN ('owner', 'reviewer')",
            name="ck_translation_block_review_revision_author_type",
        ),
        sa.CheckConstraint(
            "length(record_hash) = 64",
            name="ck_translation_block_review_revision_record_hash",
        ),
        sa.CheckConstraint(
            "length(integrity_receipt) = 64",
            name="ck_translation_block_review_revision_integrity_receipt",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["translation_project.id"],
            ondelete="CASCADE",
            name="fk_translation_block_review_revision_project",
        ),
        sa.ForeignKeyConstraint(
            ["source_plan_id", "project_id"],
            ["translation_source_plan.id", "translation_source_plan.project_id"],
            ondelete="CASCADE",
            name="fk_translation_block_review_revision_plan_scope",
        ),
        sa.ForeignKeyConstraint(
            ["block_id", "source_plan_id"],
            ["translation_block.id", "translation_block.source_plan_id"],
            ondelete="CASCADE",
            name="fk_translation_block_review_revision_block_scope",
        ),
        sa.ForeignKeyConstraint(
            ["parent_revision_id", "project_id", "source_plan_id", "block_id"],
            [
                "translation_block_review_revision.id",
                "translation_block_review_revision.project_id",
                "translation_block_review_revision.source_plan_id",
                "translation_block_review_revision.block_id",
            ],
            ondelete="RESTRICT",
            name="fk_translation_block_review_revision_parent",
        ),
        sa.ForeignKeyConstraint(
            ["target_revision_id", "source_plan_id", "block_id"],
            [
                "translation_target_block_revision.id",
                "translation_target_block_revision.source_plan_id",
                "translation_target_block_revision.block_id",
            ],
            ondelete="RESTRICT",
            name="fk_translation_block_review_revision_target",
        ),
        sa.ForeignKeyConstraint(
            ["author_user_id"],
            ["user.id"],
            ondelete="SET NULL",
            name="fk_translation_block_review_revision_author",
        ),
        sa.ForeignKeyConstraint(
            ["share_grant_id"],
            ["translation_share_grant.id"],
            ondelete="SET NULL",
            name="fk_translation_block_review_revision_share_grant",
        ),
    )
    op.create_index(
        "ix_translation_block_review_revision_plan_block_created",
        "translation_block_review_revision",
        ["source_plan_id", "block_id", "created_at"],
        unique=False,
    )

    op.create_table(
        "translation_block_review_head",
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("source_plan_id", sa.String(length=36), nullable=False),
        sa.Column("block_id", sa.String(length=128), nullable=False),
        sa.Column("revision_id", sa.String(length=36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint(
            "project_id",
            "source_plan_id",
            "block_id",
            name="pk_translation_block_review_head",
        ),
        sa.UniqueConstraint(
            "revision_id", name="uq_translation_block_review_head_revision"
        ),
        sa.CheckConstraint(
            "version >= 1", name="ck_translation_block_review_head_version"
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["translation_project.id"],
            ondelete="CASCADE",
            name="fk_translation_block_review_head_project",
        ),
        sa.ForeignKeyConstraint(
            ["source_plan_id", "project_id"],
            ["translation_source_plan.id", "translation_source_plan.project_id"],
            ondelete="CASCADE",
            name="fk_translation_block_review_head_plan_scope",
        ),
        sa.ForeignKeyConstraint(
            ["block_id", "source_plan_id"],
            ["translation_block.id", "translation_block.source_plan_id"],
            ondelete="CASCADE",
            name="fk_translation_block_review_head_block_scope",
        ),
        sa.ForeignKeyConstraint(
            ["revision_id", "project_id", "source_plan_id", "block_id"],
            [
                "translation_block_review_revision.id",
                "translation_block_review_revision.project_id",
                "translation_block_review_revision.source_plan_id",
                "translation_block_review_revision.block_id",
            ],
            ondelete="RESTRICT",
            name="fk_translation_block_review_head_revision",
        ),
    )

    op.create_table(
        "translation_retranslation_request",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("source_plan_id", sa.String(length=36), nullable=False),
        sa.Column("requester_user_id", sa.Integer(), nullable=True),
        sa.Column("share_grant_id", sa.String(length=36), nullable=True),
        sa.Column("scope_kind", sa.String(length=16), nullable=False),
        sa.Column("scope_json", sa.Text(), nullable=False),
        sa.Column("custom_prompt", sa.Text(), nullable=True),
        sa.Column("annotation_profile", sa.String(length=16), nullable=False),
        sa.Column("research_choice", sa.String(length=20), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("state", sa.String(length=16), nullable=False, server_default="requested"),
        sa.Column("dossier_revision_id", sa.String(length=36), nullable=True),
        sa.Column("operation_id", sa.String(length=36), nullable=True),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_translation_retranslation_request"),
        sa.UniqueConstraint(
            "project_id",
            "idempotency_key",
            name="uq_translation_retranslation_request_idempotency",
        ),
        sa.UniqueConstraint(
            "operation_id",
            name="uq_translation_retranslation_request_operation",
        ),
        sa.CheckConstraint(
            "scope_kind IN ('blocks', 'order_range')",
            name="ck_translation_retranslation_request_scope_kind",
        ),
        sa.CheckConstraint(
            "annotation_profile IN ('none', 'restrained', 'study')",
            name="ck_translation_retranslation_request_annotation_profile",
        ),
        sa.CheckConstraint(
            "research_choice IN ('reuse_dossier', 'fresh_document', 'await_external')",
            name="ck_translation_retranslation_request_research_choice",
        ),
        sa.CheckConstraint(
            "state IN ('requested', 'started', 'rejected', 'cancelled', 'completed')",
            name="ck_translation_retranslation_request_state",
        ),
        sa.CheckConstraint(
            "version >= 1",
            name="ck_translation_retranslation_request_version",
        ),
        sa.CheckConstraint(
            "(requester_user_id IS NOT NULL AND share_grant_id IS NULL) OR "
            "(requester_user_id IS NULL AND share_grant_id IS NOT NULL)",
            name="ck_translation_retranslation_request_actor",
        ),
        sa.CheckConstraint(
            "(research_choice = 'reuse_dossier' AND dossier_revision_id IS NOT NULL) OR "
            "(research_choice IN ('fresh_document', 'await_external') AND dossier_revision_id IS NULL)",
            name="ck_translation_retranslation_request_research",
        ),
        sa.CheckConstraint(
            "length(idempotency_key) BETWEEN 1 AND 128",
            name="ck_translation_retranslation_request_idempotency_key",
        ),
        sa.CheckConstraint(
            "length(request_fingerprint) = 64",
            name="ck_translation_retranslation_request_fingerprint",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["translation_project.id"],
            ondelete="CASCADE",
            name="fk_translation_retranslation_request_project",
        ),
        sa.ForeignKeyConstraint(
            ["source_plan_id", "project_id"],
            ["translation_source_plan.id", "translation_source_plan.project_id"],
            ondelete="CASCADE",
            name="fk_translation_retranslation_request_plan_scope",
        ),
        sa.ForeignKeyConstraint(
            ["requester_user_id"],
            ["user.id"],
            ondelete="SET NULL",
            name="fk_translation_retranslation_request_requester",
        ),
        sa.ForeignKeyConstraint(
            ["share_grant_id"],
            ["translation_share_grant.id"],
            ondelete="SET NULL",
            name="fk_translation_retranslation_request_share_grant",
        ),
        sa.ForeignKeyConstraint(
            ["dossier_revision_id", "project_id", "source_plan_id"],
            [
                "translation_research_dossier_revision.id",
                "translation_research_dossier_revision.project_id",
                "translation_research_dossier_revision.source_plan_id",
            ],
            ondelete="RESTRICT",
            name="fk_translation_retranslation_request_dossier_revision",
        ),
        sa.ForeignKeyConstraint(
            ["operation_id", "project_id"],
            ["translation_operation.id", "translation_operation.project_id"],
            ondelete="RESTRICT",
            name="fk_translation_retranslation_request_operation",
        ),
    )
    op.create_index(
        "ix_translation_retranslation_request_plan_state_created",
        "translation_retranslation_request",
        ["source_plan_id", "state", "created_at"],
        unique=False,
    )

    op.create_table(
        "translation_target_revision_share_attribution",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("source_plan_id", sa.String(length=36), nullable=False),
        sa.Column("block_id", sa.String(length=128), nullable=False),
        sa.Column("target_revision_id", sa.String(length=36), nullable=False),
        sa.Column("share_grant_id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("record_hash", sa.String(length=64), nullable=False),
        sa.Column("integrity_receipt", sa.String(length=64), nullable=False),
        sa.PrimaryKeyConstraint(
            "id", name="pk_translation_target_revision_share_attribution"
        ),
        sa.UniqueConstraint(
            "target_revision_id",
            name="uq_translation_target_revision_share_attribution_target",
        ),
        sa.CheckConstraint(
            "length(record_hash) = 64",
            name="ck_translation_target_revision_share_attribution_record_hash",
        ),
        sa.CheckConstraint(
            "length(integrity_receipt) = 64",
            name="ck_translation_target_revision_share_attribution_integrity_receipt",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["translation_project.id"],
            ondelete="CASCADE",
            name="fk_translation_target_revision_share_attribution_project",
        ),
        sa.ForeignKeyConstraint(
            ["source_plan_id", "project_id"],
            ["translation_source_plan.id", "translation_source_plan.project_id"],
            ondelete="CASCADE",
            name="fk_translation_target_revision_share_attribution_plan_scope",
        ),
        sa.ForeignKeyConstraint(
            ["block_id", "source_plan_id"],
            ["translation_block.id", "translation_block.source_plan_id"],
            ondelete="CASCADE",
            name="fk_translation_target_revision_share_attribution_block_scope",
        ),
        sa.ForeignKeyConstraint(
            ["target_revision_id", "source_plan_id", "block_id"],
            [
                "translation_target_block_revision.id",
                "translation_target_block_revision.source_plan_id",
                "translation_target_block_revision.block_id",
            ],
            ondelete="RESTRICT",
            name="fk_translation_target_revision_share_attribution_target",
        ),
        sa.ForeignKeyConstraint(
            ["share_grant_id"],
            ["translation_share_grant.id"],
            ondelete="RESTRICT",
            name="fk_translation_target_revision_share_attribution_grant",
        ),
    )
    op.create_index(
        "ix_translation_target_revision_share_attribution_grant_created",
        "translation_target_revision_share_attribution",
        ["share_grant_id", "created_at"],
        unique=False,
    )

    with op.batch_alter_table(
        "translation_context_baseline_item",
        recreate="always",
        reflect_kwargs={"resolve_fks": False},
    ) as batch_op:
        batch_op.add_column(sa.Column("research_dossier_revision_id", sa.String(36), nullable=True))
        batch_op.drop_constraint(
            "ck_translation_context_baseline_item_kind", type_="check"
        )
        batch_op.drop_constraint(
            "ck_translation_context_baseline_item_reference_kind", type_="check"
        )
        batch_op.create_check_constraint(
            "ck_translation_context_baseline_item_kind",
            _item_kind_check(include_dossier=True).sqltext,
        )
        batch_op.create_check_constraint(
            "ck_translation_context_baseline_item_reference_kind",
            _reference_check(include_dossier=True).sqltext,
        )
        batch_op.create_foreign_key(
            "fk_translation_context_baseline_item_research_dossier_scope",
            "translation_research_dossier_revision",
            ["research_dossier_revision_id", "project_id", "source_plan_id"],
            ["id", "project_id", "source_plan_id"],
            ondelete="RESTRICT",
        )


def downgrade() -> None:
    bind = op.get_bind()
    reviewer_target_exists = bind.execute(
        sa.text(
            "SELECT 1 FROM translation_target_block_revision "
            "WHERE author_type = 'reviewer' LIMIT 1"
        )
    ).first() is not None
    share_attribution_exists = bind.execute(
        sa.text(
            "SELECT 1 FROM translation_target_revision_share_attribution LIMIT 1"
        )
    ).first() is not None
    if reviewer_target_exists or share_attribution_exists:
        raise RuntimeError(
            "Translator migration 024 cannot downgrade after reviewer-authored "
            "target revisions or their share attributions exist."
        )
    if bind.execute(
        sa.text(
            "SELECT 1 FROM translation_context_baseline_item "
            "WHERE item_kind = 'research_dossier' OR research_dossier_revision_id IS NOT NULL "
            "LIMIT 1"
        )
    ).first() is not None:
        raise RuntimeError(
            "Translator migration 024 cannot downgrade after research dossier "
            "baseline items exist."
        )

    with op.batch_alter_table(
        "translation_context_baseline_item",
        recreate="always",
        reflect_kwargs={"resolve_fks": False},
    ) as batch_op:
        batch_op.drop_constraint(
            "fk_translation_context_baseline_item_research_dossier_scope",
            type_="foreignkey",
        )
        batch_op.drop_constraint(
            "ck_translation_context_baseline_item_kind", type_="check"
        )
        batch_op.drop_constraint(
            "ck_translation_context_baseline_item_reference_kind", type_="check"
        )
        batch_op.drop_column("research_dossier_revision_id")
        batch_op.create_check_constraint(
            "ck_translation_context_baseline_item_kind",
            _item_kind_check(include_dossier=False).sqltext,
        )
        batch_op.create_check_constraint(
            "ck_translation_context_baseline_item_reference_kind",
            _reference_check(include_dossier=False).sqltext,
        )

    for table in (
        "translation_target_revision_share_attribution",
        "translation_block_review_head",
        "translation_block_review_revision",
        "translation_reader_note_head",
        "translation_reader_note_revision",
        "translation_reader_note",
        "translation_block_glossary_suggestion",
        "translation_research_dossier_glossary_proposal",
        "translation_retranslation_request",
        "translation_share_session",
        "translation_research_dossier_revision",
        "translation_research_dossier",
        "translation_share_grant",
    ):
        op.drop_table(table)
