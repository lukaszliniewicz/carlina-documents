"""add canonical knowledge sources, projections, and coverage

Revision ID: 20260728_knowledge_fabric_v2
Revises: 20260728_context_fabric_v2
Create Date: 2026-07-28 12:00:00.000000

EncryptedType application fields are represented as TEXT here. This migration
is additive: legacy DataContext rows and collection associations remain intact
until a separately audited retention operation removes them.
"""

from alembic import op
import sqlalchemy as sa


revision = "20260728_knowledge_fabric_v2"
down_revision = "20260728_context_fabric_v2"
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


def _create_indexes(table_name, columns):
    existing = _index_names(table_name)
    for column in columns:
        name = f"ix_{table_name}_{column}"
        if name not in existing:
            op.create_index(name, table_name, [column], unique=False)


def upgrade():
    if not _has_table("knowledge_source"):
        op.create_table(
            "knowledge_source",
            sa.Column("id", sa.String(length=40), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("source_type", sa.String(length=64), nullable=False),
            sa.Column("external_id", sa.String(length=512), nullable=False),
            sa.Column("title", sa.Text(), nullable=True),
            sa.Column("mime_type", sa.String(length=255), nullable=True),
            sa.Column(
                "current_version_number",
                sa.Integer(),
                server_default="0",
                nullable=False,
            ),
            sa.Column("source_created_at", sa.DateTime(), nullable=True),
            sa.Column("source_modified_at", sa.DateTime(), nullable=True),
            sa.Column("access_policy_json", sa.Text(), nullable=True),
            sa.Column("metadata_json", sa.Text(), nullable=True),
            sa.Column("legacy_data_context_id", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.CheckConstraint(
                "current_version_number >= 0",
                name="ck_knowledge_source_current_version_nonnegative",
            ),
            sa.ForeignKeyConstraint(
                ["legacy_data_context_id"],
                ["data_context.id"],
                name="fk_knowledge_source_legacy_context_id_data_context",
            ),
            sa.ForeignKeyConstraint(
                ["user_id"],
                ["user.id"],
                name="fk_knowledge_source_user_id_user",
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "user_id",
                "source_type",
                "external_id",
                name="uq_knowledge_source_owner_type_external",
            ),
        )
        _create_indexes(
            "knowledge_source",
            (
                "legacy_data_context_id",
                "source_created_at",
                "source_modified_at",
                "source_type",
                "user_id",
            ),
        )

    if not _has_table("knowledge_source_version"):
        op.create_table(
            "knowledge_source_version",
            sa.Column("id", sa.String(length=41), nullable=False),
            sa.Column("source_id", sa.String(length=40), nullable=False),
            sa.Column("version_number", sa.Integer(), nullable=False),
            sa.Column("content_hash", sa.String(length=64), nullable=False),
            sa.Column("content", sa.Text(), nullable=False),
            sa.Column(
                "extraction_status",
                sa.String(length=24),
                server_default="ready",
                nullable=False,
            ),
            sa.Column("extractor_name", sa.String(length=128), nullable=True),
            sa.Column("extractor_version", sa.String(length=64), nullable=True),
            sa.Column("extractor_config_json", sa.Text(), nullable=True),
            sa.Column("provenance_json", sa.Text(), nullable=True),
            sa.Column("char_count", sa.Integer(), server_default="0", nullable=False),
            sa.Column("byte_count", sa.Integer(), server_default="0", nullable=False),
            sa.Column("token_estimate", sa.Integer(), nullable=True),
            sa.Column("source_modified_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.CheckConstraint(
                "version_number >= 1",
                name="ck_knowledge_source_version_positive",
            ),
            sa.CheckConstraint(
                "extraction_status in ('pending','ready','failed','superseded')",
                name="ck_knowledge_source_version_status",
            ),
            sa.ForeignKeyConstraint(
                ["source_id"],
                ["knowledge_source.id"],
                name="fk_knowledge_source_version_source_id_source",
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "source_id",
                "version_number",
                name="uq_knowledge_source_version_number",
            ),
            sa.UniqueConstraint(
                "source_id",
                "content_hash",
                name="uq_knowledge_source_version_hash",
            ),
        )
        _create_indexes(
            "knowledge_source_version",
            ("content_hash", "extraction_status", "source_id", "source_modified_at"),
        )

    if not _has_table("knowledge_source_unit"):
        op.create_table(
            "knowledge_source_unit",
            sa.Column("id", sa.String(length=41), nullable=False),
            sa.Column("source_version_id", sa.String(length=41), nullable=False),
            sa.Column("ordinal", sa.Integer(), nullable=False),
            sa.Column(
                "unit_type",
                sa.String(length=48),
                server_default="segment",
                nullable=False,
            ),
            sa.Column("external_id", sa.String(length=512), nullable=True),
            sa.Column("anchor", sa.Text(), nullable=True),
            sa.Column("content", sa.Text(), nullable=False),
            sa.Column("content_hash", sa.String(length=64), nullable=False),
            sa.Column("start_offset", sa.Integer(), nullable=True),
            sa.Column("end_offset", sa.Integer(), nullable=True),
            sa.Column("occurred_at", sa.DateTime(), nullable=True),
            sa.Column("char_count", sa.Integer(), server_default="0", nullable=False),
            sa.Column("token_estimate", sa.Integer(), nullable=True),
            sa.Column("metadata_json", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.CheckConstraint(
                "ordinal >= 0",
                name="ck_knowledge_source_unit_ordinal_nonnegative",
            ),
            sa.CheckConstraint(
                "start_offset is null or start_offset >= 0",
                name="ck_knowledge_source_unit_start_nonnegative",
            ),
            sa.CheckConstraint(
                "end_offset is null or start_offset is null or end_offset >= start_offset",
                name="ck_knowledge_source_unit_offsets",
            ),
            sa.ForeignKeyConstraint(
                ["source_version_id"],
                ["knowledge_source_version.id"],
                name="fk_knowledge_source_unit_version_id_version",
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "source_version_id",
                "ordinal",
                name="uq_knowledge_source_unit_version_ordinal",
            ),
        )
        _create_indexes(
            "knowledge_source_unit",
            (
                "content_hash",
                "external_id",
                "occurred_at",
                "source_version_id",
                "unit_type",
            ),
        )

    if not _has_table("knowledge_collection_membership"):
        op.create_table(
            "knowledge_collection_membership",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("collection_id", sa.Integer(), nullable=False),
            sa.Column("source_id", sa.String(length=40), nullable=False),
            sa.Column("frozen_version_id", sa.String(length=41), nullable=True),
            sa.Column("position", sa.Integer(), nullable=True),
            sa.Column("metadata_json", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(
                ["collection_id"],
                ["data_context_collection.id"],
                name="fk_knowledge_membership_collection_id_collection",
            ),
            sa.ForeignKeyConstraint(
                ["frozen_version_id"],
                ["knowledge_source_version.id"],
                name="fk_knowledge_membership_version_id_version",
            ),
            sa.ForeignKeyConstraint(
                ["source_id"],
                ["knowledge_source.id"],
                name="fk_knowledge_membership_source_id_source",
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "collection_id",
                "source_id",
                name="uq_knowledge_membership_collection_source",
            ),
        )
        _create_indexes(
            "knowledge_collection_membership",
            ("collection_id", "frozen_version_id", "source_id"),
        )

    if not _has_table("knowledge_saved_view"):
        op.create_table(
            "knowledge_saved_view",
            sa.Column("id", sa.String(length=40), nullable=False),
            sa.Column("collection_id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("source_type", sa.String(length=64), nullable=False),
            sa.Column("query_json", sa.Text(), nullable=False),
            sa.Column("query_version", sa.Integer(), server_default="1", nullable=False),
            sa.Column(
                "refresh_status",
                sa.String(length=24),
                server_default="ready",
                nullable=False,
            ),
            sa.Column("result_count", sa.Integer(), server_default="0", nullable=False),
            sa.Column("last_refreshed_at", sa.DateTime(), nullable=True),
            sa.Column("error_json", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.CheckConstraint(
                "query_version >= 1",
                name="ck_knowledge_saved_view_query_version_positive",
            ),
            sa.CheckConstraint(
                "refresh_status in ('pending','refreshing','ready','failed')",
                name="ck_knowledge_saved_view_refresh_status",
            ),
            sa.ForeignKeyConstraint(
                ["collection_id"],
                ["data_context_collection.id"],
                name="fk_knowledge_saved_view_collection_id_collection",
            ),
            sa.ForeignKeyConstraint(
                ["user_id"],
                ["user.id"],
                name="fk_knowledge_saved_view_user_id_user",
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("collection_id"),
        )
        _create_indexes(
            "knowledge_saved_view",
            ("collection_id", "source_type", "user_id"),
        )

    if not _has_table("knowledge_index_generation"):
        op.create_table(
            "knowledge_index_generation",
            sa.Column("id", sa.String(length=39), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("collection_id", sa.Integer(), nullable=True),
            sa.Column(
                "projection_type",
                sa.String(length=32),
                server_default="hybrid",
                nullable=False,
            ),
            sa.Column(
                "status",
                sa.String(length=24),
                server_default="building",
                nullable=False,
            ),
            sa.Column("signature_hash", sa.String(length=64), nullable=False),
            sa.Column("signature_json", sa.JSON(), nullable=False),
            sa.Column("previous_generation_id", sa.String(length=39), nullable=True),
            sa.Column("entry_count", sa.Integer(), server_default="0", nullable=False),
            sa.Column("health_json", sa.Text(), nullable=True),
            sa.Column("error_json", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("completed_at", sa.DateTime(), nullable=True),
            sa.Column("activated_at", sa.DateTime(), nullable=True),
            sa.Column("retired_at", sa.DateTime(), nullable=True),
            sa.CheckConstraint(
                "status in ('building','ready','active','failed','retired')",
                name="ck_knowledge_index_generation_status",
            ),
            sa.ForeignKeyConstraint(
                ["collection_id"],
                ["data_context_collection.id"],
                name="fk_knowledge_index_generation_collection_id_collection",
            ),
            sa.ForeignKeyConstraint(
                ["previous_generation_id"],
                ["knowledge_index_generation.id"],
                name="fk_knowledge_index_generation_previous_id_generation",
            ),
            sa.ForeignKeyConstraint(
                ["user_id"],
                ["user.id"],
                name="fk_knowledge_index_generation_user_id_user",
            ),
            sa.PrimaryKeyConstraint("id"),
        )
        _create_indexes(
            "knowledge_index_generation",
            ("collection_id", "signature_hash", "status", "user_id"),
        )

    if not _has_table("knowledge_index_entry"):
        op.create_table(
            "knowledge_index_entry",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("generation_id", sa.String(length=39), nullable=False),
            sa.Column("source_unit_id", sa.String(length=41), nullable=False),
            sa.Column("ordinal", sa.Integer(), server_default="0", nullable=False),
            sa.Column("vector_key", sa.String(length=128), nullable=True),
            sa.Column("lexical_fingerprints", sa.JSON(), nullable=True),
            sa.Column("projection_metadata_json", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(
                ["generation_id"],
                ["knowledge_index_generation.id"],
                name="fk_knowledge_index_entry_generation_id_generation",
            ),
            sa.ForeignKeyConstraint(
                ["source_unit_id"],
                ["knowledge_source_unit.id"],
                name="fk_knowledge_index_entry_source_unit_id_unit",
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "generation_id",
                "source_unit_id",
                name="uq_knowledge_index_entry_generation_unit",
            ),
        )
        _create_indexes(
            "knowledge_index_entry",
            ("generation_id", "source_unit_id", "vector_key"),
        )

    if not _has_table("knowledge_coverage_run"):
        op.create_table(
            "knowledge_coverage_run",
            sa.Column("id", sa.String(length=39), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("source_id", sa.String(length=40), nullable=False),
            sa.Column("source_version_id", sa.String(length=41), nullable=False),
            sa.Column("session_id", sa.String(length=36), nullable=True),
            sa.Column("turn_id", sa.String(length=36), nullable=True),
            sa.Column("model_id", sa.String(length=255), nullable=True),
            sa.Column(
                "status",
                sa.String(length=24),
                server_default="pending",
                nullable=False,
            ),
            sa.Column("total_units", sa.Integer(), server_default="0", nullable=False),
            sa.Column("covered_units", sa.Integer(), server_default="0", nullable=False),
            sa.Column("failed_units", sa.Integer(), server_default="0", nullable=False),
            sa.Column("manifest_json", sa.Text(), nullable=True),
            sa.Column("synthesis_json", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.Column("completed_at", sa.DateTime(), nullable=True),
            sa.CheckConstraint(
                "status in ('pending','running','completed','partial','failed')",
                name="ck_knowledge_coverage_run_status",
            ),
            sa.ForeignKeyConstraint(
                ["session_id"],
                ["chat_session.id"],
                name="fk_knowledge_coverage_run_session_id_session",
            ),
            sa.ForeignKeyConstraint(
                ["source_id"],
                ["knowledge_source.id"],
                name="fk_knowledge_coverage_run_source_id_source",
            ),
            sa.ForeignKeyConstraint(
                ["source_version_id"],
                ["knowledge_source_version.id"],
                name="fk_knowledge_coverage_run_version_id_version",
            ),
            sa.ForeignKeyConstraint(
                ["turn_id"],
                ["chat_turn.id"],
                name="fk_knowledge_coverage_run_turn_id_turn",
            ),
            sa.ForeignKeyConstraint(
                ["user_id"],
                ["user.id"],
                name="fk_knowledge_coverage_run_user_id_user",
            ),
            sa.PrimaryKeyConstraint("id"),
        )
        _create_indexes(
            "knowledge_coverage_run",
            ("session_id", "source_id", "source_version_id", "status", "turn_id", "user_id"),
        )

    if not _has_table("knowledge_coverage_slice"):
        op.create_table(
            "knowledge_coverage_slice",
            sa.Column("id", sa.String(length=42), nullable=False),
            sa.Column("coverage_run_id", sa.String(length=39), nullable=False),
            sa.Column("slice_index", sa.Integer(), nullable=False),
            sa.Column("start_ordinal", sa.Integer(), nullable=False),
            sa.Column("end_ordinal", sa.Integer(), nullable=False),
            sa.Column("overlap_before", sa.Integer(), server_default="0", nullable=False),
            sa.Column("overlap_after", sa.Integer(), server_default="0", nullable=False),
            sa.Column(
                "status",
                sa.String(length=24),
                server_default="pending",
                nullable=False,
            ),
            sa.Column("output_json", sa.Text(), nullable=True),
            sa.Column("evidence_refs", sa.JSON(), nullable=True),
            sa.Column("error_json", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("completed_at", sa.DateTime(), nullable=True),
            sa.CheckConstraint(
                "slice_index >= 0",
                name="ck_knowledge_coverage_slice_index_nonnegative",
            ),
            sa.CheckConstraint(
                "start_ordinal >= 0 and end_ordinal >= start_ordinal",
                name="ck_knowledge_coverage_slice_ordinals",
            ),
            sa.CheckConstraint(
                "status in ('pending','running','completed','failed')",
                name="ck_knowledge_coverage_slice_status",
            ),
            sa.ForeignKeyConstraint(
                ["coverage_run_id"],
                ["knowledge_coverage_run.id"],
                name="fk_knowledge_coverage_slice_run_id_run",
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "coverage_run_id",
                "slice_index",
                name="uq_knowledge_coverage_slice_run_index",
            ),
        )
        _create_indexes(
            "knowledge_coverage_slice",
            ("coverage_run_id", "status"),
        )


def downgrade():
    for table_name in (
        "knowledge_coverage_slice",
        "knowledge_coverage_run",
        "knowledge_index_entry",
        "knowledge_index_generation",
        "knowledge_saved_view",
        "knowledge_collection_membership",
        "knowledge_source_unit",
        "knowledge_source_version",
        "knowledge_source",
    ):
        if _has_table(table_name):
            op.drop_table(table_name)
