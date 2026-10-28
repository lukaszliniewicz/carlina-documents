"""Add indexes for user-scoped translation workflows.

Revision ID: 005_query_indexes
Revises: 004_glossary_needs_review
"""

from alembic import op

revision = "005_query_indexes"
down_revision = "004_glossary_needs_review"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index(
        "ix_translation_project_user_updated",
        "translation_project",
        ["user_id", "updated_at"],
        unique=False,
    )
    op.create_index(
        "ix_translation_revision_segment_created",
        "translation_revision",
        ["segment_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_translation_segment_project_order",
        "translation_segment",
        ["project_id", "order_index"],
        unique=False,
    )
    op.create_index(
        "ix_translation_segment_project_status_order",
        "translation_segment",
        ["project_id", "status", "order_index"],
        unique=False,
    )
    op.create_index(
        "ix_translation_glossary_project_source",
        "translation_glossary",
        ["project_id", "source_term"],
        unique=False,
    )


def downgrade():
    op.drop_index(
        "ix_translation_glossary_project_source",
        table_name="translation_glossary",
    )
    op.drop_index(
        "ix_translation_segment_project_status_order",
        table_name="translation_segment",
    )
    op.drop_index(
        "ix_translation_segment_project_order",
        table_name="translation_segment",
    )
    op.drop_index(
        "ix_translation_revision_segment_created",
        table_name="translation_revision",
    )
    op.drop_index(
        "ix_translation_project_user_updated",
        table_name="translation_project",
    )
