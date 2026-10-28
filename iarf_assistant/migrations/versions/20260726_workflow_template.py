"""repair the missing workflow template table

Revision ID: 20260726_workflow_template
Revises: 20260725_runtime_collections
Create Date: 2026-07-26 00:00:00.000000

The original dd2d3e720c4b migration was generated as an empty no-op. This
forward migration is deliberately idempotent so it repairs upgraded databases
while leaving databases created from current metadata untouched.
"""

from alembic import op
import sqlalchemy as sa


revision = "20260726_workflow_template"
down_revision = "20260725_runtime_collections"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    if sa.inspect(bind).has_table("workflow_template"):
        return

    op.create_table(
        "workflow_template",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("definition_json", sa.JSON(), nullable=False),
        sa.Column("owner_id", sa.Integer(), nullable=True),
        sa.Column(
            "is_active",
            sa.Boolean(),
            server_default=sa.true(),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.func.current_timestamp(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.func.current_timestamp(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["user.id"],
            name="fk_workflow_template_owner_id_user",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_workflow_template_owner_id",
        "workflow_template",
        ["owner_id"],
        unique=False,
    )


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("workflow_template"):
        return

    indexes = {
        index.get("name")
        for index in inspector.get_indexes("workflow_template")
    }
    if "ix_workflow_template_owner_id" in indexes:
        op.drop_index(
            "ix_workflow_template_owner_id",
            table_name="workflow_template",
        )
    op.drop_table("workflow_template")
