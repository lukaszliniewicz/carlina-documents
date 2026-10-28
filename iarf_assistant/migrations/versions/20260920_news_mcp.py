"""Allow owner-scoped news executions in the shared MCP ledger."""

import sqlalchemy as sa
from alembic import op

revision = "20260920_news_mcp"
down_revision = "20260920_mcp_claim_receipts"
branch_labels = None
depends_on = None


def _change(include_news):
    connection = op.get_bind()
    if (
        not include_news
        and connection.execute(
            sa.text(
                "SELECT 1 FROM mcp_execution WHERE addon_id = 'news_pipeline' LIMIT 1"
            )
        ).first()
    ):
        raise RuntimeError("Cannot downgrade while retained news MCP executions exist.")
    constraints = sa.inspect(connection).get_check_constraints("mcp_execution")
    current = next(
        row["sqltext"] for row in constraints if row["name"] == "ck_mcp_execution_addon"
    )
    if ("news_pipeline" in current) == include_news:
        return  # Safe retry if the process stopped before Alembic stamped the revision.
    with op.batch_alter_table("mcp_execution") as batch:
        batch.drop_constraint("ck_mcp_execution_addon", type_="check")
        batch.create_check_constraint(
            "ck_mcp_execution_addon",
            "addon_id IN ('doc_processor', 'translator', 'news_pipeline')"
            if include_news
            else "addon_id IN ('doc_processor', 'translator')",
        )


def _migrate(include_news):
    if op.get_bind().dialect.name != "sqlite":
        _change(include_news)
        return
    # SQLite's batch rebuild drops the parent table. Leaving foreign keys on
    # would cascade-delete retained work. Toggle outside a transaction, then
    # explicitly transact the complete rebuild (including its DDL) atomically.
    # No decoded encrypted values pass through Python during the table copy.
    with op.get_context().autocommit_block():
        connection = op.get_bind()
        enabled = connection.exec_driver_sql("PRAGMA foreign_keys").scalar()
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        started = False
        try:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            started = True
            _change(include_news)
            if connection.exec_driver_sql("PRAGMA foreign_key_check").first():
                raise RuntimeError(
                    "Foreign-key validation failed during news MCP migration."
                )
            connection.exec_driver_sql("COMMIT")
        except BaseException:
            if started:
                connection.exec_driver_sql("ROLLBACK")
            raise
        finally:
            connection.exec_driver_sql(f"PRAGMA foreign_keys={1 if enabled else 0}")


def upgrade():
    _migrate(True)


def downgrade():
    _migrate(False)
