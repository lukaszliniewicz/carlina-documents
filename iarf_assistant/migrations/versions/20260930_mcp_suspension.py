"""Allow nonterminal suspended MCP executions."""

import sqlalchemy as sa
from alembic import op

revision = "20260930_mcp_suspension"
down_revision = "20260926_mcp_transport"
branch_labels = None
depends_on = None

STATUS_CHECK = "ck_mcp_execution_status"
STATUS_WITHOUT_SUSPENSION = (
    "status IN ('queued', 'running', 'waiting_external', 'waiting_review', "
    "'completed', 'failed', 'cancelled')"
)
STATUS_WITH_SUSPENSION = (
    "status IN ('queued', 'running', 'waiting_external', 'waiting_review', "
    "'suspended', 'completed', 'failed', 'cancelled')"
)


def _normalize_sql(value: str) -> str:
    return " ".join(
        value.lower()
        .replace('"', "")
        .replace("`", "")
        .replace("[", "")
        .replace("]", "")
        .split()
    )


def _change(include_suspension: bool) -> None:
    connection = op.get_bind()
    if not sa.inspect(connection).has_table("mcp_execution"):
        raise RuntimeError("Cannot migrate MCP suspension without mcp_execution.")
    if not include_suspension and connection.execute(
        sa.text("SELECT 1 FROM mcp_execution WHERE status = 'suspended' LIMIT 1")
    ).first():
        raise RuntimeError(
            "Cannot downgrade while suspended MCP executions exist."
        )

    constraints = {
        item["name"]: item["sqltext"]
        for item in sa.inspect(connection).get_check_constraints("mcp_execution")
    }
    current = constraints.get(STATUS_CHECK)
    if current is None:
        raise RuntimeError(
            "Cannot migrate MCP suspension without the execution status check."
        )

    desired_sql = (
        STATUS_WITH_SUSPENSION if include_suspension else STATUS_WITHOUT_SUSPENSION
    )
    if _normalize_sql(current) == _normalize_sql(desired_sql):
        return  # Safe if DDL committed before Alembic stamped the revision.
    expected_sql = (
        STATUS_WITHOUT_SUSPENSION
        if include_suspension
        else STATUS_WITH_SUSPENSION
    )
    if _normalize_sql(current) != _normalize_sql(expected_sql):
        raise RuntimeError(
            "Cannot migrate MCP suspension: execution status check is incompatible."
        )

    with op.batch_alter_table("mcp_execution") as batch:
        batch.drop_constraint(STATUS_CHECK, type_="check")
        batch.create_check_constraint(STATUS_CHECK, desired_sql)


def _migrate(include_suspension: bool) -> None:
    if op.get_bind().dialect.name != "sqlite":
        _change(include_suspension)
        return

    # SQLite's batch rebuild drops the parent table. Keep child ledger rows and
    # their encrypted bytes intact by disabling foreign keys outside a single
    # explicit transaction, as in 20260920_news_mcp.
    with op.get_context().autocommit_block():
        connection = op.get_bind()
        enabled = connection.exec_driver_sql("PRAGMA foreign_keys").scalar()
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        started = False
        try:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            started = True
            _change(include_suspension)
            if connection.exec_driver_sql("PRAGMA foreign_key_check").first():
                raise RuntimeError(
                    "Foreign-key validation failed during MCP suspension migration."
                )
            connection.exec_driver_sql("COMMIT")
        except BaseException:
            if started:
                connection.exec_driver_sql("ROLLBACK")
            raise
        finally:
            connection.exec_driver_sql(
                f"PRAGMA foreign_keys={1 if enabled else 0}"
            )


def upgrade() -> None:
    _migrate(True)


def downgrade() -> None:
    _migrate(False)
