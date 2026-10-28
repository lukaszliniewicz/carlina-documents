"""Add document projects and project runs

Revision ID: 002_doc_projects
Revises: 001_doc_processor
Create Date: 2026-06-15 12:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = "002_doc_projects"
down_revision = "001_doc_processor"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    existing = set(inspector.get_table_names())

    if "document_project" not in existing:
        op.create_table(
            "document_project",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("name", sa.String(length=255), nullable=False),
            sa.Column("settings", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_document_project_user_id", "document_project", ["user_id"])

    if "project_document" not in existing:
        op.create_table(
            "project_document",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("project_id", sa.String(length=36), nullable=False),
            sa.Column("document_id", sa.String(length=36), nullable=False),
            sa.Column("position", sa.Integer(), nullable=False),
            sa.Column("settings_overrides", sa.JSON(), nullable=True),
            sa.Column("added_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(["document_id"], ["processed_document.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["project_id"], ["document_project.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("project_id", "document_id", name="uq_project_document"),
        )
        op.create_index("ix_project_document_position", "project_document", ["project_id", "position"])

    if "project_run" not in existing:
        op.create_table(
            "project_run",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("project_id", sa.String(length=36), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(length=50), nullable=False),
            sa.Column("settings_snapshot", sa.JSON(), nullable=True),
            sa.Column("concurrency_limit", sa.Integer(), nullable=False),
            sa.Column("pause_requested", sa.Boolean(), nullable=False),
            sa.Column("stop_requested", sa.Boolean(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.Column("started_at", sa.DateTime(), nullable=True),
            sa.Column("completed_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(["project_id"], ["document_project.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_project_run_user_id", "project_run", ["user_id"])

    if "project_run_document" not in existing:
        op.create_table(
            "project_run_document",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("run_id", sa.String(length=36), nullable=False),
            sa.Column("document_id", sa.String(length=36), nullable=False),
            sa.Column("position", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(length=50), nullable=False),
            sa.Column("effective_settings", sa.JSON(), nullable=True),
            sa.Column("job_id", sa.String(length=36), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("started_at", sa.DateTime(), nullable=True),
            sa.Column("completed_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(["document_id"], ["processed_document.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["run_id"], ["project_run.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("run_id", "document_id", name="uq_project_run_document"),
        )
        op.create_index("ix_project_run_document_status", "project_run_document", ["run_id", "status", "position"])


def downgrade():
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    for table in ("project_run_document", "project_run", "project_document", "document_project"):
        if table in existing:
            op.drop_table(table)
