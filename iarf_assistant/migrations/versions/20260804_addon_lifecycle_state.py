"""Normalize persisted add-on activity and lifecycle state.

Revision ID: 20260804_addon_lifecycle_state
Revises: 20260801_addon_connections
"""

import sqlalchemy as sa
from alembic import op

revision = "20260804_addon_lifecycle_state"
down_revision = "20260801_addon_connections"
branch_labels = None
depends_on = None

_MISMATCH = """
    (is_active = 1 AND lifecycle_state != 'activated')
    OR (is_active = 0 AND lifecycle_state = 'activated')
"""


def upgrade() -> None:
    """Repair states written by the former startup safe-mode shortcut."""

    op.execute(
        sa.text(
            f"""
            INSERT INTO addon_lifecycle_events (
                addon_id,
                from_state,
                to_state,
                event,
                success,
                detail,
                metadata_json,
                actor_user_id,
                generation,
                created_at
            )
            SELECT
                id,
                lifecycle_state,
                CASE WHEN is_active = 1 THEN 'activated' ELSE 'disabled' END,
                'lifecycle_state_reconciled',
                1,
                'Normalized activity and lifecycle state during upgrade.',
                '{{"migration":"{revision}"}}',
                NULL,
                COALESCE(lifecycle_generation, 0) + 1,
                CURRENT_TIMESTAMP
            FROM addons
            WHERE {_MISMATCH}
            """
        )
    )
    op.execute(
        sa.text(
            f"""
            UPDATE addons
            SET lifecycle_state = CASE
                    WHEN is_active = 1 THEN 'activated'
                    ELSE 'disabled'
                END,
                lifecycle_generation = COALESCE(lifecycle_generation, 0) + 1,
                lifecycle_updated_at = CURRENT_TIMESTAMP,
                lifecycle_error = CASE
                    WHEN is_active = 1 THEN NULL
                    ELSE lifecycle_error
                END
            WHERE {_MISMATCH}
            """
        )
    )


def downgrade() -> None:
    """Keep repaired data consistent when the schema revision is downgraded."""
