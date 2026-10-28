"""Add core-owned Compass native-client authorization records.

Revision ID: 20260812_compass_client_auth
Revises: 20260804_addon_lifecycle_state
"""

import sqlalchemy as sa
from alembic import op

from iarf_assistant.database.types import EncryptedType

revision = "20260812_compass_client_auth"
down_revision = "20260804_addon_lifecycle_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create only keyed-digest credentials; raw bearer material is never DDL data."""

    op.create_table(
        "compass_client_device",
        sa.Column("device_id", sa.String(length=36), nullable=False),
        sa.Column("owner_user_id", sa.Integer(), nullable=False),
        sa.Column(
            "display_name",
            EncryptedType(sa.String(length=96), strategy="user"),
            nullable=False,
        ),
        sa.Column("platform", sa.String(length=32), nullable=False),
        sa.Column("scope", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "scope = 'compass.capture'", name="ck_compass_client_device_scope"
        ),
        sa.CheckConstraint(
            "status IN ('active', 'revoked')", name="ck_compass_client_device_status"
        ),
        sa.CheckConstraint(
            "(status = 'active' AND revoked_at IS NULL) "
            "OR (status = 'revoked' AND revoked_at IS NOT NULL)",
            name="ck_compass_client_device_revocation",
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["user.id"],
            ondelete="CASCADE",
            name="fk_compass_client_device_owner",
        ),
        sa.PrimaryKeyConstraint("device_id"),
    )
    op.create_index(
        "ix_compass_client_device_owner_created",
        "compass_client_device",
        ["owner_user_id", "created_at"],
    )

    op.create_table(
        "compass_client_authorization_code",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column("device_id", sa.String(length=36), nullable=False),
        sa.Column("redirect_uri", sa.String(length=512), nullable=False),
        sa.Column("code_challenge", sa.String(length=128), nullable=False),
        sa.Column("scope", sa.String(length=32), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "scope = 'compass.capture'",
            name="ck_compass_client_authorization_code_scope",
        ),
        sa.CheckConstraint(
            "expires_at > issued_at",
            name="ck_compass_client_authorization_code_expiry",
        ),
        sa.ForeignKeyConstraint(
            ["device_id"],
            ["compass_client_device.device_id"],
            ondelete="CASCADE",
            name="fk_compass_client_authorization_code_device",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code_hash"),
    )
    op.create_index(
        "ix_compass_client_authorization_code_expiry",
        "compass_client_authorization_code",
        ["expires_at"],
    )

    op.create_table(
        "compass_client_access_token",
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("device_id", sa.String(length=36), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "expires_at > issued_at", name="ck_compass_client_access_token_expiry"
        ),
        sa.ForeignKeyConstraint(
            ["device_id"],
            ["compass_client_device.device_id"],
            ondelete="CASCADE",
            name="fk_compass_client_access_token_device",
        ),
        sa.PrimaryKeyConstraint("token_hash"),
    )
    op.create_index(
        "ix_compass_client_access_token_expiry",
        "compass_client_access_token",
        ["expires_at"],
    )
    op.create_index(
        "ix_compass_client_access_token_device",
        "compass_client_access_token",
        ["device_id"],
    )

    op.create_table(
        "compass_client_refresh_token",
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("device_id", sa.String(length=36), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("replaced_by_hash", sa.String(length=64), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "expires_at > issued_at", name="ck_compass_client_refresh_token_expiry"
        ),
        sa.CheckConstraint(
            "(used_at IS NULL AND replaced_by_hash IS NULL) "
            "OR (used_at IS NOT NULL AND replaced_by_hash IS NOT NULL)",
            name="ck_compass_client_refresh_token_rotation",
        ),
        sa.ForeignKeyConstraint(
            ["device_id"],
            ["compass_client_device.device_id"],
            ondelete="CASCADE",
            name="fk_compass_client_refresh_token_device",
        ),
        sa.PrimaryKeyConstraint("token_hash"),
    )
    op.create_index(
        "ix_compass_client_refresh_token_device",
        "compass_client_refresh_token",
        ["device_id"],
    )
    op.create_index(
        "ix_compass_client_refresh_token_expiry",
        "compass_client_refresh_token",
        ["expires_at"],
    )
    op.create_table(
        "compass_client_auth_event",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("owner_user_id", sa.Integer(), nullable=False),
        sa.Column("device_id", sa.String(length=36), nullable=False),
        sa.Column("event", sa.String(length=32), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "event IN ('approved', 'code_exchanged', 'refresh_rotated', "
            "'refresh_reuse_revoked', 'revoked')",
            name="ck_compass_client_auth_event_event",
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["user.id"],
            ondelete="CASCADE",
            name="fk_compass_client_auth_event_owner",
        ),
        sa.ForeignKeyConstraint(
            ["device_id"],
            ["compass_client_device.device_id"],
            ondelete="CASCADE",
            name="fk_compass_client_auth_event_device",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_compass_client_auth_event_owner_occurred",
        "compass_client_auth_event",
        ["owner_user_id", "occurred_at"],
    )


def downgrade() -> None:
    """Remove the standalone device-auth state without touching Compass content."""

    op.drop_index(
        "ix_compass_client_auth_event_owner_occurred",
        table_name="compass_client_auth_event",
    )
    op.drop_table("compass_client_auth_event")
    op.drop_index(
        "ix_compass_client_refresh_token_expiry",
        table_name="compass_client_refresh_token",
    )
    op.drop_index(
        "ix_compass_client_refresh_token_device",
        table_name="compass_client_refresh_token",
    )
    op.drop_table("compass_client_refresh_token")
    op.drop_index(
        "ix_compass_client_access_token_device",
        table_name="compass_client_access_token",
    )
    op.drop_index(
        "ix_compass_client_access_token_expiry",
        table_name="compass_client_access_token",
    )
    op.drop_table("compass_client_access_token")
    op.drop_index(
        "ix_compass_client_authorization_code_expiry",
        table_name="compass_client_authorization_code",
    )
    op.drop_table("compass_client_authorization_code")
    op.drop_index(
        "ix_compass_client_device_owner_created",
        table_name="compass_client_device",
    )
    op.drop_table("compass_client_device")
