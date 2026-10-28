"""Persist Add-on API v1 contracts and lifecycle journal.

Revision ID: 20260731_addon_api_v1
Revises: 20260729_chat_history_indexes
"""

from alembic import op
import sqlalchemy as sa


revision = "20260731_addon_api_v1"
down_revision = "20260729_chat_history_indexes"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("addons") as batch_op:
        batch_op.add_column(
            sa.Column("source_kind", sa.String(length=32), server_default="built_in", nullable=False)
        )
        batch_op.add_column(
            sa.Column(
                "trust_tier",
                sa.String(length=40),
                server_default="built_in_trusted",
                nullable=False,
            )
        )
        batch_op.add_column(
            sa.Column(
                "lifecycle_state",
                sa.String(length=32),
                server_default="discovered",
                nullable=False,
            )
        )
        batch_op.add_column(
            sa.Column("lifecycle_generation", sa.Integer(), server_default="0", nullable=False)
        )
        batch_op.add_column(sa.Column("lifecycle_error", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("manifest_sha256", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("contract_sha256", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("resolved_contract_json", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("provenance_json", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("code_path", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("installed_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(
            sa.Column(
                "lifecycle_updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.current_timestamp(),
                nullable=False,
            )
        )
        batch_op.create_index("ix_addons_source_kind", ["source_kind"], unique=False)
        batch_op.create_index("ix_addons_trust_tier", ["trust_tier"], unique=False)
        batch_op.create_index("ix_addons_lifecycle_state", ["lifecycle_state"], unique=False)

    op.execute(
        "UPDATE addons SET lifecycle_state = CASE WHEN is_active = 1 "
        "THEN 'activated' ELSE 'disabled' END"
    )
    op.create_table(
        "addon_lifecycle_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("addon_id", sa.String(length=100), nullable=False),
        sa.Column("from_state", sa.String(length=32), nullable=False),
        sa.Column("to_state", sa.String(length=32), nullable=False),
        sa.Column("event", sa.String(length=80), nullable=False),
        sa.Column("success", sa.Boolean(), server_default="1", nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column("actor_user_id", sa.Integer(), nullable=True),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.current_timestamp(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["addon_id"], ["addons.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["actor_user_id"], ["user.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_addon_lifecycle_events_addon_id_id",
        "addon_lifecycle_events",
        ["addon_id", "id"],
        unique=False,
    )


def downgrade():
    op.drop_index("ix_addon_lifecycle_events_addon_id_id", table_name="addon_lifecycle_events")
    op.drop_table("addon_lifecycle_events")
    with op.batch_alter_table("addons") as batch_op:
        batch_op.drop_index("ix_addons_lifecycle_state")
        batch_op.drop_index("ix_addons_trust_tier")
        batch_op.drop_index("ix_addons_source_kind")
        batch_op.drop_column("lifecycle_updated_at")
        batch_op.drop_column("installed_at")
        batch_op.drop_column("code_path")
        batch_op.drop_column("provenance_json")
        batch_op.drop_column("resolved_contract_json")
        batch_op.drop_column("contract_sha256")
        batch_op.drop_column("manifest_sha256")
        batch_op.drop_column("lifecycle_error")
        batch_op.drop_column("lifecycle_generation")
        batch_op.drop_column("lifecycle_state")
        batch_op.drop_column("trust_tier")
        batch_op.drop_column("source_kind")
