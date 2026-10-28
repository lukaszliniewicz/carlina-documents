"""Repair the omitted shared model audio-input capability column.

The historical revisions bearing this column's name were empty. Keep those
revisions immutable and repair both fresh migrations and previously stamped
databases here. The column is compatible with the older ORM as well.
"""

import sqlalchemy as sa
from alembic import op

revision = "20261004_llm_audio_input"
down_revision = "20260930_mcp_suspension"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    columns = {
        column["name"] for column in sa.inspect(connection).get_columns("llm_models")
    }
    if "supports_audio_in" not in columns:
        op.add_column(
            "llm_models",
            sa.Column(
                "supports_audio_in",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("0"),
            ),
        )


def downgrade() -> None:
    # Older application versions already declare this field. Retaining the
    # additive column keeps them usable and preserves collected capability data.
    pass
