"""Add per-key MCP response transport budgets."""

import sqlalchemy as sa
from alembic import op

revision = "20260926_mcp_transport"
down_revision = "20260920_news_mcp"
branch_labels = None
depends_on = None

DEFAULT_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
MIN_RESPONSE_BYTES = 64 * 1024
DEFAULT_COLUMN = "response_default_bytes"
MAX_COLUMN = "response_max_bytes"
DEFAULT_CHECK = "ck_mcp_access_key_response_default_bounds"
MAX_CHECK = "ck_mcp_access_key_response_max_bounds"
DEFAULT_CHECK_SQL = f"{DEFAULT_COLUMN} BETWEEN {MIN_RESPONSE_BYTES} AND {MAX_RESPONSE_BYTES}"
MAX_CHECK_SQL = (
    f"{MAX_COLUMN} BETWEEN {MIN_RESPONSE_BYTES} AND {MAX_RESPONSE_BYTES} "
    f"AND {DEFAULT_COLUMN} <= {MAX_COLUMN}"
)


def _normalized_sql(value: str) -> str:
    return "".join(
        value.lower()
        .replace('"', "")
        .replace("`", "")
        .replace("[", "")
        .replace("]", "")
        .replace("(", "")
        .replace(")", "")
        .split()
    )


def _normalized_default(value) -> str | None:
    if value is None:
        return None
    return "".join(
        str(value)
        .replace("'", "")
        .replace('"', "")
        .replace("(", "")
        .replace(")", "")
        .split()
    )


def _transport_columns() -> set[str]:
    """Return valid transport columns, rejecting partial or incompatible DDL."""

    connection = op.get_bind()
    inspector = sa.inspect(connection)
    if not inspector.has_table("mcp_access_key"):
        raise RuntimeError("Cannot migrate MCP transport budgets without mcp_access_key.")
    columns = {
        column["name"]: column
        for column in inspector.get_columns("mcp_access_key")
    }
    checks = {
        check["name"]: check["sqltext"]
        for check in inspector.get_check_constraints("mcp_access_key")
    }

    def validate_column(
        name: str,
        constraint_name: str,
        expected_sql: str,
        expected_default: int,
    ) -> bool:
        column = columns.get(name)
        check_sql = checks.get(constraint_name)
        if column is None:
            if check_sql is not None:
                raise RuntimeError(
                    f"Cannot retry MCP transport migration: orphaned {constraint_name}."
                )
            return False
        if (
            not isinstance(column["type"], sa.Integer)
            or column["nullable"] is not False
            or _normalized_default(column.get("default")) != str(expected_default)
            or check_sql is None
            or _normalized_sql(check_sql) != _normalized_sql(expected_sql)
        ):
            raise RuntimeError(
                f"Cannot retry MCP transport migration: incompatible {name} column."
            )
        return True

    has_default = validate_column(
        DEFAULT_COLUMN,
        DEFAULT_CHECK,
        DEFAULT_CHECK_SQL,
        DEFAULT_RESPONSE_BYTES,
    )
    has_max = validate_column(
        MAX_COLUMN,
        MAX_CHECK,
        MAX_CHECK_SQL,
        MAX_RESPONSE_BYTES,
    )
    if has_max and not has_default:
        raise RuntimeError(
            "Cannot retry MCP transport migration: response_max_bytes exists "
            "without response_default_bytes."
        )
    return {
        name
        for name, present in (
            (DEFAULT_COLUMN, has_default),
            (MAX_COLUMN, has_max),
        )
        if present
    }


def upgrade() -> None:
    """Add bounded response defaults without rebuilding encrypted key rows.

    SQLite DDL may persist the first addition even if migration execution is
    interrupted before Alembic stamps the revision. Inspecting each column
    makes that state safe to resume and makes a repeated pre-stamp invocation
    a no-op.
    """

    existing = _transport_columns()
    if DEFAULT_COLUMN not in existing:
        op.add_column(
            "mcp_access_key",
            sa.Column(
                DEFAULT_COLUMN,
                sa.Integer(),
                sa.CheckConstraint(
                    DEFAULT_CHECK_SQL,
                    name=DEFAULT_CHECK,
                ),
                nullable=False,
                server_default=str(DEFAULT_RESPONSE_BYTES),
            ),
        )
        existing.add(DEFAULT_COLUMN)
    if MAX_COLUMN not in existing:
        op.add_column(
            "mcp_access_key",
            sa.Column(
                MAX_COLUMN,
                sa.Integer(),
                sa.CheckConstraint(
                    MAX_CHECK_SQL,
                    name=MAX_CHECK,
                ),
                nullable=False,
                server_default=str(MAX_RESPONSE_BYTES),
            ),
        )


def downgrade() -> None:
    """Remove only the two transport-budget columns and their checks."""

    existing = _transport_columns()
    if MAX_COLUMN in existing:
        op.drop_column("mcp_access_key", MAX_COLUMN)
    if DEFAULT_COLUMN in existing:
        op.drop_column("mcp_access_key", DEFAULT_COLUMN)
