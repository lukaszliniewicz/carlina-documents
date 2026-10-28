"""Persist provider-neutral passive window suspension and reset projects.

Revision ID: 023_passive_window_pending
Revises: 022_block_translator_notes
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "023_passive_window_pending"
down_revision = "022_block_translator_notes"
branch_labels = None
depends_on = None


def _quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _delete_table_rows(
    bind: sa.Connection,
    inspector: sa.Inspector,
    table: str,
) -> None:
    """Delete one table child-first even when its rows form a lineage."""

    quoted_table = _quote_identifier(table)
    self_references = [
        foreign_key
        for foreign_key in inspector.get_foreign_keys(table)
        if foreign_key.get("referred_table") == table
    ]
    if not self_references:
        if bind.execute(
            sa.text(f"SELECT 1 FROM {quoted_table} LIMIT 1")  # noqa: S608
        ).first():
            bind.execute(sa.text(f"DELETE FROM {quoted_table}"))  # noqa: S608
        return

    predicates: list[str] = []
    for foreign_key in self_references:
        constrained = foreign_key.get("constrained_columns") or []
        referred = foreign_key.get("referred_columns") or []
        if not constrained or len(constrained) != len(referred):
            raise RuntimeError(
                "Translator migration 023 cannot inspect self-referential FKs "
                f"for {table!r}."
            )
        pairs = " AND ".join(
            f"child.{_quote_identifier(child_column)} = "
            f"{quoted_table}.{_quote_identifier(parent_column)}"
            for child_column, parent_column in zip(constrained, referred, strict=True)
        )
        predicates.append(f"({pairs})")

    referenced_by_child = " OR ".join(predicates)
    while bind.execute(
        sa.text(f"SELECT 1 FROM {quoted_table} LIMIT 1")  # noqa: S608
    ).first():
        deleted = bind.execute(
            sa.text(
                f"DELETE FROM {quoted_table} WHERE NOT EXISTS ("  # noqa: S608
                f"SELECT 1 FROM {quoted_table} AS child WHERE "
                f"{referenced_by_child})"
            )
        )
        if deleted.rowcount == 0:
            raise RuntimeError(
                "Translator migration 023 found a cyclic self-reference in "
                f"{table!r}."
            )


def _delete_translator_rows(bind: sa.Connection) -> None:
    """Delete every Translator-owned aggregate row child before parent."""

    inspector = sa.inspect(bind)
    tables = {
        table
        for table in inspector.get_table_names()
        if table.startswith("translation_") and table != "translation_project"
    }
    dependencies: dict[str, set[str]] = {table: set() for table in tables}
    for table in tables:
        for foreign_key in inspector.get_foreign_keys(table):
            referred = foreign_key.get("referred_table")
            if referred in tables and referred != table:
                dependencies[table].add(referred)

    remaining = set(tables)
    while remaining:
        ready = sorted(
            table
            for table in remaining
            if not any(
                table in dependencies[other]
                for other in remaining
                if other != table
            )
        )
        if not ready:
            raise RuntimeError(
                "Translator migration 023 cannot order aggregate cleanup FKs."
            )
        for table in ready:
            _delete_table_rows(bind, inspector, table)
            remaining.remove(table)


def _delete_translator_mcp_executions(bind: sa.Connection) -> None:
    """Delete only host MCP aggregates scoped to Translator projects."""

    tables = set(sa.inspect(bind).get_table_names())
    if "mcp_execution" not in tables:
        return
    scope_parameters = {"addon_id": "translator", "resource_kind": "project"}
    if {"mcp_execution", "mcp_work_item", "mcp_work_submission"} <= tables:
        bind.execute(
            sa.text(
                "DELETE FROM mcp_work_submission WHERE work_item_id IN ("
                "SELECT item.id FROM mcp_work_item AS item "
                "JOIN mcp_execution AS execution "
                "ON execution.id = item.execution_id "
                "WHERE execution.addon_id = :addon_id "
                "AND execution.resource_kind = :resource_kind)"
            ),
            scope_parameters,
        )
    if {"mcp_execution", "mcp_work_item"} <= tables:
        bind.execute(
            sa.text(
                "DELETE FROM mcp_work_item WHERE execution_id IN ("
                "SELECT id FROM mcp_execution WHERE addon_id = :addon_id "
                "AND resource_kind = :resource_kind)"
            ),
            scope_parameters,
        )
    bind.execute(
        sa.text(
            "DELETE FROM mcp_execution WHERE addon_id = :addon_id "
            "AND resource_kind = :resource_kind"
        ),
        scope_parameters,
    )


def upgrade() -> None:
    bind = op.get_bind()
    _delete_translator_mcp_executions(bind)
    _delete_translator_rows(bind)
    if bind.execute(sa.text('SELECT 1 FROM "translation_project" LIMIT 1')).first():
        bind.execute(sa.text('DELETE FROM "translation_project"'))

    with op.batch_alter_table("translation_window_attempt") as batch_op:
        batch_op.drop_constraint(
            "ck_translation_window_attempt_dispatch_state",
            type_="check",
        )
        batch_op.create_check_constraint(
            "ck_translation_window_attempt_dispatch_state",
            "dispatch_state IN ('prepared', 'dispatched', 'external_pending', "
            "'response_received', 'provider_failed', 'normalization_failed', "
            "'fence_failed', 'staging_failed', 'staged', "
            "'provider_outcome_uncertain')",
        )


def downgrade() -> None:
    raise RuntimeError(
        "Translator migration 023 is forward-only because project data was deleted."
    )
