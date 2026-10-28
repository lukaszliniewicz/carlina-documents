"""Add normalized add-on Connections, secrets, grants, and bindings.

Revision ID: 20260801_addon_connections
Revises: 20260731_backend_performance
"""

import sqlalchemy as sa
from alembic import op

revision = "20260801_addon_connections"
down_revision = "20260731_backend_performance"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "addon_connection",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("owner_user_id", sa.Integer(), nullable=False),
        sa.Column("connector_addon_id", sa.String(length=100), nullable=False),
        sa.Column("connector_definition_id", sa.String(length=128), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("state", sa.String(length=32), server_default="ready", nullable=False),
        sa.Column("is_default", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("config_json", sa.Text(), nullable=True),
        sa.Column("metadata_json", sa.Text(), nullable=True),
        sa.Column("health_status", sa.String(length=32), nullable=True),
        sa.Column("health_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("legacy_credential_id", sa.Integer(), nullable=True),
        sa.Column("legacy_provider_name", sa.String(length=50), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.current_timestamp(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.current_timestamp(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["user.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["legacy_credential_id"],
            ["plugin_credential.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "legacy_credential_id",
            name="uq_addon_connection_legacy_credential_id",
        ),
    )
    op.create_index(
        "ix_addon_connection_owner_user_id",
        "addon_connection",
        ["owner_user_id"],
        unique=False,
    )
    op.create_index(
        "ix_addon_connection_connector_addon_id",
        "addon_connection",
        ["connector_addon_id"],
        unique=False,
    )
    op.create_index(
        "ix_addon_connection_state",
        "addon_connection",
        ["state"],
        unique=False,
    )
    op.create_index(
        "ix_addon_connection_health_status",
        "addon_connection",
        ["health_status"],
        unique=False,
    )
    op.create_index(
        "ix_addon_connection_legacy_provider_name",
        "addon_connection",
        ["legacy_provider_name"],
        unique=False,
    )
    op.create_index(
        "ix_addon_connection_owner_connector_state",
        "addon_connection",
        ["owner_user_id", "connector_addon_id", "state"],
        unique=False,
    )

    op.create_table(
        "connection_secret",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("connection_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("secret_data", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.current_timestamp(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.current_timestamp(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["connection_id"],
            ["addon_connection.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "connection_id",
            "name",
            name="uq_connection_secret_connection_name",
        ),
    )
    op.create_index(
        "ix_connection_secret_connection_id",
        "connection_secret",
        ["connection_id"],
        unique=False,
    )

    op.create_table(
        "connection_grant",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("connection_id", sa.String(length=36), nullable=False),
        sa.Column("grantee_user_id", sa.Integer(), nullable=False),
        sa.Column("granted_by_user_id", sa.Integer(), nullable=False),
        sa.Column("operations_json", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.current_timestamp(),
            nullable=False,
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["connection_id"],
            ["addon_connection.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["grantee_user_id"],
            ["user.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["granted_by_user_id"],
            ["user.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "connection_id",
            "grantee_user_id",
            name="uq_connection_grant_connection_grantee",
        ),
    )
    op.create_index(
        "ix_connection_grant_connection_id",
        "connection_grant",
        ["connection_id"],
        unique=False,
    )
    op.create_index(
        "ix_connection_grant_grantee_user_id",
        "connection_grant",
        ["grantee_user_id"],
        unique=False,
    )
    op.create_index(
        "ix_connection_grant_revoked_at",
        "connection_grant",
        ["revoked_at"],
        unique=False,
    )

    op.create_table(
        "capability_binding",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("binding_key", sa.String(length=64), nullable=False),
        sa.Column("consumer_addon_id", sa.String(length=100), nullable=False),
        sa.Column("requirement_id", sa.String(length=128), nullable=False),
        sa.Column("provider_addon_id", sa.String(length=100), nullable=False),
        sa.Column("provider_capability_id", sa.String(length=128), nullable=False),
        sa.Column("connection_id", sa.String(length=36), nullable=True),
        sa.Column(
            "scope",
            sa.String(length=32),
            server_default="installation",
            nullable=False,
        ),
        sa.Column("scope_id", sa.String(length=128), server_default="", nullable=False),
        sa.Column("priority", sa.Integer(), server_default="0", nullable=False),
        sa.Column("status", sa.String(length=32), server_default="enabled", nullable=False),
        sa.Column("contract_version", sa.String(length=50), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_by_user_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.current_timestamp(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.current_timestamp(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["connection_id"],
            ["addon_connection.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["user.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("binding_key", name="uq_capability_binding_binding_key"),
    )
    op.create_index(
        "ix_capability_binding_consumer_addon_id",
        "capability_binding",
        ["consumer_addon_id"],
        unique=False,
    )
    op.create_index(
        "ix_capability_binding_provider_addon_id",
        "capability_binding",
        ["provider_addon_id"],
        unique=False,
    )
    op.create_index(
        "ix_capability_binding_connection_id",
        "capability_binding",
        ["connection_id"],
        unique=False,
    )
    op.create_index(
        "ix_capability_binding_status",
        "capability_binding",
        ["status"],
        unique=False,
    )
    op.create_index(
        "ix_capability_binding_consumer_requirement_scope",
        "capability_binding",
        ["consumer_addon_id", "requirement_id", "scope", "scope_id", "status"],
        unique=False,
    )
    op.create_index(
        "ix_capability_binding_provider_capability",
        "capability_binding",
        ["provider_addon_id", "provider_capability_id"],
        unique=False,
    )

    op.create_table(
        "addon_preference",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("addon_id", sa.String(length=100), nullable=False),
        sa.Column("preferences_json", sa.Text(), nullable=False),
        sa.Column(
            "migrated_from_user_settings",
            sa.Boolean(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.current_timestamp(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "addon_id"),
    )

    op.create_table(
        "addon_data_migration_run",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("migration_id", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("checkpoint", sa.String(length=100), nullable=True),
        sa.Column("resumed_from_run_id", sa.String(length=36), nullable=True),
        sa.Column("report_json", sa.JSON(), nullable=True),
        sa.Column("error_type", sa.String(length=200), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.current_timestamp(),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["resumed_from_run_id"],
            ["addon_data_migration_run.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_addon_data_migration_run_migration_id",
        "addon_data_migration_run",
        ["migration_id"],
        unique=False,
    )
    op.create_index(
        "ix_addon_data_migration_run_status",
        "addon_data_migration_run",
        ["status"],
        unique=False,
    )
    op.create_table(
        "addon_data_migration_event",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("stage", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("detail_json", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.current_timestamp(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["addon_data_migration_run.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "run_id",
            "sequence",
            name="uq_addon_data_migration_event_run_sequence",
        ),
    )
    op.create_index(
        "ix_addon_data_migration_event_run_id",
        "addon_data_migration_event",
        ["run_id"],
        unique=False,
    )


def downgrade():
    op.drop_index(
        "ix_addon_data_migration_event_run_id",
        table_name="addon_data_migration_event",
    )
    op.drop_table("addon_data_migration_event")
    op.drop_index(
        "ix_addon_data_migration_run_status",
        table_name="addon_data_migration_run",
    )
    op.drop_index(
        "ix_addon_data_migration_run_migration_id",
        table_name="addon_data_migration_run",
    )
    op.drop_table("addon_data_migration_run")

    op.drop_table("addon_preference")

    op.drop_index(
        "ix_capability_binding_provider_capability",
        table_name="capability_binding",
    )
    op.drop_index(
        "ix_capability_binding_consumer_requirement_scope",
        table_name="capability_binding",
    )
    op.drop_index("ix_capability_binding_status", table_name="capability_binding")
    op.drop_index(
        "ix_capability_binding_connection_id",
        table_name="capability_binding",
    )
    op.drop_index(
        "ix_capability_binding_provider_addon_id",
        table_name="capability_binding",
    )
    op.drop_index(
        "ix_capability_binding_consumer_addon_id",
        table_name="capability_binding",
    )
    op.drop_table("capability_binding")

    op.drop_index("ix_connection_grant_revoked_at", table_name="connection_grant")
    op.drop_index(
        "ix_connection_grant_grantee_user_id",
        table_name="connection_grant",
    )
    op.drop_index(
        "ix_connection_grant_connection_id",
        table_name="connection_grant",
    )
    op.drop_table("connection_grant")

    op.drop_index(
        "ix_connection_secret_connection_id",
        table_name="connection_secret",
    )
    op.drop_table("connection_secret")

    op.drop_index(
        "ix_addon_connection_owner_connector_state",
        table_name="addon_connection",
    )
    op.drop_index(
        "ix_addon_connection_legacy_provider_name",
        table_name="addon_connection",
    )
    op.drop_index(
        "ix_addon_connection_health_status",
        table_name="addon_connection",
    )
    op.drop_index("ix_addon_connection_state", table_name="addon_connection")
    op.drop_index(
        "ix_addon_connection_connector_addon_id",
        table_name="addon_connection",
    )
    op.drop_index(
        "ix_addon_connection_owner_user_id",
        table_name="addon_connection",
    )
    op.drop_table("addon_connection")
