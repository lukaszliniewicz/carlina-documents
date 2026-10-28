"""Add durable Translator research runs and tool-call transcripts.

Revision ID: 014_research_runs
Revises: 013_integrity_receipts

The migration also widens the analysis-attempt contract check for the
versioned message packet that will be implemented by the execution layer.
Existing v1 evidence remains valid; no v2 payload is fabricated here.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "014_research_runs"
down_revision = "013_integrity_receipts"
branch_labels = None
depends_on = None


def _hash(field: str, name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(
        f"length({field}) = 64",
        name=f"ck_translation_research_{name}",
    )


def _assert_no_evidence(*, direction: str, allow_v2: bool = True) -> None:
    bind = op.get_bind()
    tables = {
        row[0]
        for row in bind.execute(
            sa.text("SELECT name FROM sqlite_master WHERE type = 'table'")
        )
    }
    for table_name in (
        "translation_research_tool_call_event",
        "translation_research_tool_result_blob",
        "translation_research_tool_call",
        "translation_research_turn",
        "translation_research_run",
    ):
        if table_name not in tables:
            continue
        if bind.execute(sa.text(f'SELECT 1 FROM "{table_name}" LIMIT 1')).first() is not None:
            raise RuntimeError(
                "Translator migration 014 cannot "
                f"{direction} after research evidence exists in {table_name}."
            )
    if not allow_v2 and "translation_analysis_attempt" in tables and bind.execute(
        sa.text(
            "SELECT 1 FROM translation_analysis_attempt "
            "WHERE contract_version = 'analysis-attempt-v2' LIMIT 1"
        )
    ).first() is not None:
        raise RuntimeError(
            "Translator migration 014 cannot downgrade after analysis-attempt-v2 evidence exists."
        )


_V1_CONTRACT_CHECK = "CHECK (contract_version = 'analysis-attempt-v1')"
_V2_CONTRACT_CHECK = (
    "CHECK (contract_version IN ('analysis-attempt-v1', 'analysis-attempt-v2'))"
)


def _replace_analysis_contract_check(
    bind: sa.Connection,
    *,
    source: str,
    target: str,
) -> None:
    """Widen/narrow one SQLite CHECK without rebuilding evidence tables.

    SQLite implements Alembic batch CHECK changes by dropping the old table.
    ``DROP TABLE`` performs an implicit delete, so valid retry/rebase self-FKs
    with ``ON DELETE RESTRICT`` make that strategy both lossy and impossible.
    This migration changes only the verified CHECK fragment in ``sqlite_master``
    and bumps the schema cookie so SQLite reparses it.  Rows, indexes, foreign
    keys, encrypted child events, and lineage remain byte-for-byte untouched.
    """

    row = bind.execute(
        sa.text(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'table' AND name = 'translation_analysis_attempt'"
        )
    ).scalar_one_or_none()
    if (
        not isinstance(row, str)
        or "ck_translation_analysis_attempt_contract_version" not in row
        or row.count(source) != 1
        or target in row
    ):
        raise RuntimeError(
            "Translator migration 014 found an unexpected analysis-attempt schema."
        )
    replacement = row.replace(source, target, 1)
    try:
        bind.exec_driver_sql("PRAGMA writable_schema = ON")
        result = bind.execute(
            sa.text(
                "UPDATE sqlite_master SET sql = :sql "
                "WHERE type = 'table' AND name = 'translation_analysis_attempt' "
                "AND sql = :expected"
            ),
            {"sql": replacement, "expected": row},
        )
        if result.rowcount != 1:
            raise RuntimeError(
                "Translator migration 014 could not update the analysis-attempt schema."
            )
    finally:
        bind.exec_driver_sql("PRAGMA writable_schema = OFF")
    schema_version = int(bind.exec_driver_sql("PRAGMA schema_version").scalar_one())
    bind.exec_driver_sql(f"PRAGMA schema_version = {schema_version + 1}")
    updated = bind.execute(
        sa.text(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'table' AND name = 'translation_analysis_attempt'"
        )
    ).scalar_one()
    if updated != replacement:
        raise RuntimeError(
            "Translator migration 014 could not verify the analysis-attempt schema."
        )
    integrity = bind.exec_driver_sql("PRAGMA integrity_check").scalars().all()
    foreign_keys = [
        *bind.exec_driver_sql(
            "PRAGMA foreign_key_check('translation_analysis_attempt')"
        ).all(),
        *bind.exec_driver_sql(
            "PRAGMA foreign_key_check('translation_analysis_attempt_event')"
        ).all(),
    ]
    if integrity != ["ok"] or foreign_keys:
        raise RuntimeError(
            "Translator migration 014 analysis-attempt schema validation failed."
        )


def upgrade() -> None:
    _assert_no_evidence(direction="upgrade")
    bind = op.get_bind()
    _replace_analysis_contract_check(
        bind,
        source=_V1_CONTRACT_CHECK,
        target=_V2_CONTRACT_CHECK,
    )

    op.create_table(
        "translation_research_run",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), nullable=False),
        sa.Column("owner_user_id", sa.Integer(), nullable=False),
        sa.Column("operation_id", sa.String(36), nullable=False),
        sa.Column("source_plan_id", sa.String(36), nullable=False),
        sa.Column("source_plan_hash", sa.String(64), nullable=False),
        sa.Column("source_content_hash", sa.String(64), nullable=False),
        sa.Column("contract_version", sa.String(64), nullable=False),
        sa.Column("operation_version", sa.Integer(), nullable=False),
        sa.Column("queue_job_id", sa.String(128), nullable=False),
        sa.Column("operation_lease_expires_at", sa.DateTime(), nullable=False),
        sa.Column("lease_fence", sa.String(64), nullable=False),
        sa.Column("model_json", sa.Text(), nullable=False),
        sa.Column("settings_json", sa.Text(), nullable=False),
        sa.Column("budget_json", sa.Text(), nullable=False),
        sa.Column("profile_json", sa.Text(), nullable=False),
        sa.Column("selected_provider_ids_json", sa.Text(), nullable=False),
        sa.Column("selected_addon_ids_json", sa.Text(), nullable=False),
        sa.Column("tool_declarations_json", sa.Text(), nullable=False),
        sa.Column("model_hash", sa.String(64), nullable=False),
        sa.Column("settings_hash", sa.String(64), nullable=False),
        sa.Column("budget_hash", sa.String(64), nullable=False),
        sa.Column("profile_hash", sa.String(64), nullable=False),
        sa.Column("providers_hash", sa.String(64), nullable=False),
        sa.Column("addons_hash", sa.String(64), nullable=False),
        sa.Column("tools_hash", sa.String(64), nullable=False),
        sa.Column("run_record_hash", sa.String(64), nullable=False),
        sa.Column("state", sa.String(32), nullable=False, server_default="prepared"),
        sa.Column("state_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("state_head_receipt", sa.String(64), nullable=False),
        sa.Column("started_at", sa.DateTime()),
        sa.Column("finished_at", sa.DateTime()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("id", "source_plan_id", name="uq_translation_research_run_id_plan"),
        sa.CheckConstraint("contract_version = 'research-run-v1'", name="ck_translation_research_run_contract_version"),
        sa.CheckConstraint("state IN ('prepared', 'running', 'completed', 'failed', 'cancelled', 'uncertain')", name="ck_translation_research_run_state"),
        sa.CheckConstraint("state_version >= 1", name="ck_translation_research_run_state_version"),
        sa.CheckConstraint("operation_version >= 1", name="ck_translation_research_run_operation_version"),
        sa.CheckConstraint(
            "(state = 'prepared' AND started_at IS NULL AND finished_at IS NULL) OR "
            "(state = 'running' AND started_at IS NOT NULL AND finished_at IS NULL) OR "
            "(state IN ('completed', 'uncertain') AND started_at IS NOT NULL AND finished_at IS NOT NULL) OR "
            "(state IN ('failed', 'cancelled') AND finished_at IS NOT NULL)",
            name="ck_translation_research_run_timestamps",
        ),
        _hash("source_plan_hash", "run_plan_hash"),
        _hash("source_content_hash", "run_source_hash"),
        _hash("lease_fence", "run_lease_fence"),
        _hash("model_hash", "run_model_hash"),
        _hash("settings_hash", "run_settings_hash"),
        _hash("budget_hash", "run_budget_hash"),
        _hash("profile_hash", "run_profile_hash"),
        _hash("providers_hash", "run_providers_hash"),
        _hash("addons_hash", "run_addons_hash"),
        _hash("tools_hash", "run_tools_hash"),
        _hash("run_record_hash", "run_record_hash"),
        _hash("state_head_receipt", "run_state_head_receipt"),
        sa.ForeignKeyConstraint(["project_id"], ["translation_project.id"], ondelete="CASCADE", name="fk_translation_research_run_project"),
        sa.ForeignKeyConstraint(["owner_user_id"], ["user.id"], ondelete="CASCADE", name="fk_translation_research_run_owner"),
        sa.ForeignKeyConstraint(["operation_id", "project_id"], ["translation_operation.id", "translation_operation.project_id"], ondelete="CASCADE", name="fk_translation_research_run_operation_scope"),
        sa.ForeignKeyConstraint(["source_plan_id", "project_id"], ["translation_source_plan.id", "translation_source_plan.project_id"], ondelete="CASCADE", name="fk_translation_research_run_plan_scope"),
    )
    op.create_index("ix_translation_research_run_operation_created", "translation_research_run", ["operation_id", "created_at"])
    op.create_index("ix_translation_research_run_plan_state", "translation_research_run", ["source_plan_id", "state", "created_at"])

    op.create_table(
        "translation_research_turn",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("project_id", sa.String(36), nullable=False),
        sa.Column("owner_user_id", sa.Integer(), nullable=False),
        sa.Column("operation_id", sa.String(36), nullable=False),
        sa.Column("source_plan_id", sa.String(36), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("analysis_attempt_id", sa.String(36), nullable=False),
        sa.Column("attempt_record_hash", sa.String(64), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("turn_record_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("run_id", "sequence", name="uq_translation_research_turn_sequence"),
        sa.UniqueConstraint("analysis_attempt_id", name="uq_translation_research_turn_attempt"),
        sa.UniqueConstraint("id", "run_id", "source_plan_id", name="uq_translation_research_turn_id_scope"),
        sa.CheckConstraint("sequence >= 1", name="ck_translation_research_turn_sequence"),
        _hash("attempt_record_hash", "turn_attempt_record_hash"),
        _hash("request_hash", "turn_request_hash"),
        _hash("turn_record_hash", "turn_record_hash"),
        sa.ForeignKeyConstraint(["run_id", "source_plan_id"], ["translation_research_run.id", "translation_research_run.source_plan_id"], ondelete="CASCADE", name="fk_translation_research_turn_run_plan"),
        sa.ForeignKeyConstraint(["analysis_attempt_id", "source_plan_id"], ["translation_analysis_attempt.id", "translation_analysis_attempt.source_plan_id"], ondelete="RESTRICT", name="fk_translation_research_turn_attempt_plan"),
        sa.ForeignKeyConstraint(["project_id"], ["translation_project.id"], ondelete="CASCADE", name="fk_translation_research_turn_project"),
        sa.ForeignKeyConstraint(["owner_user_id"], ["user.id"], ondelete="CASCADE", name="fk_translation_research_turn_owner"),
    )
    op.create_index("ix_translation_research_turn_run_sequence", "translation_research_turn", ["run_id", "sequence"])

    op.create_table(
        "translation_research_tool_call",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("turn_id", sa.String(36), nullable=False),
        sa.Column("project_id", sa.String(36), nullable=False),
        sa.Column("owner_user_id", sa.Integer(), nullable=False),
        sa.Column("operation_id", sa.String(36), nullable=False),
        sa.Column("source_plan_id", sa.String(36), nullable=False),
        sa.Column("source_plan_hash", sa.String(64), nullable=False),
        sa.Column("call_index", sa.Integer(), nullable=False),
        sa.Column("turn_call_index", sa.Integer(), nullable=False),
        sa.Column("provider_call_id_json", sa.Text(), nullable=False),
        sa.Column("provider_call_key", sa.String(64), nullable=False),
        sa.Column("tool_name", sa.String(512), nullable=False),
        sa.Column("tool_id", sa.String(128), nullable=False),
        sa.Column("provider_id", sa.String(128), nullable=False),
        sa.Column("addon_id", sa.String(128), nullable=False),
        sa.Column("operation_version", sa.Integer(), nullable=True),
        sa.Column("queue_job_id", sa.String(128), nullable=True),
        sa.Column("operation_lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("lease_fence", sa.String(64), nullable=True),
        sa.Column("declaration_json", sa.Text(), nullable=False),
        sa.Column("raw_arguments_json", sa.Text(), nullable=False),
        sa.Column("arguments_json", sa.Text(), nullable=False),
        sa.Column("argument_state", sa.String(16), nullable=False, server_default="parsed"),
        sa.Column("argument_error_json", sa.Text()),
        sa.Column("declaration_hash", sa.String(64), nullable=False),
        sa.Column("raw_arguments_hash", sa.String(64), nullable=False),
        sa.Column("arguments_hash", sa.String(64), nullable=False),
        sa.Column("call_record_hash", sa.String(64), nullable=False),
        sa.Column("state", sa.String(32), nullable=False, server_default="prepared"),
        sa.Column("state_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("state_head_receipt", sa.String(64), nullable=False),
        sa.Column("result_json", sa.Text()),
        sa.Column("error_json", sa.Text()),
        sa.Column("result_hash", sa.String(64)),
        sa.Column("error_hash", sa.String(64)),
        sa.Column("tool_message_content_json", sa.Text()),
        sa.Column("tool_message_hash", sa.String(64)),
        sa.Column("claimed_at", sa.DateTime()),
        sa.Column("finished_at", sa.DateTime()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("run_id", "call_index", name="uq_translation_research_tool_call_index"),
        sa.UniqueConstraint("turn_id", "turn_call_index", name="uq_translation_research_tool_call_turn_index"),
        sa.UniqueConstraint("run_id", "provider_call_key", name="uq_translation_research_tool_call_provider_key"),
        sa.CheckConstraint("call_index >= 1", name="ck_translation_research_tool_call_index"),
        sa.CheckConstraint("turn_call_index >= 1", name="ck_translation_research_tool_call_turn_index"),
        sa.CheckConstraint("state IN ('prepared', 'executing', 'succeeded', 'blocked', 'failed', 'uncertain', 'cancelled')", name="ck_translation_research_tool_call_state"),
        sa.CheckConstraint("state_version >= 1", name="ck_translation_research_tool_call_state_version"),
        sa.CheckConstraint("argument_state IN ('parsed', 'invalid')", name="ck_translation_research_tool_call_argument_state"),
        sa.CheckConstraint("operation_version IS NULL OR operation_version >= 1", name="ck_translation_research_tool_call_operation_version"),
        sa.CheckConstraint("length(tool_name) <= 512", name="ck_translation_research_tool_call_tool_name_length"),
        sa.CheckConstraint("length(tool_id) <= 128", name="ck_translation_research_tool_call_tool_id_length"),
        sa.CheckConstraint("length(provider_id) <= 128", name="ck_translation_research_tool_call_provider_id_length"),
        sa.CheckConstraint("length(addon_id) <= 128", name="ck_translation_research_tool_call_addon_id_length"),
        sa.CheckConstraint(
            "(operation_version IS NULL AND queue_job_id IS NULL AND "
            "operation_lease_expires_at IS NULL AND lease_fence IS NULL) OR "
            "(operation_version IS NOT NULL AND queue_job_id IS NOT NULL AND "
            "operation_lease_expires_at IS NOT NULL AND lease_fence IS NOT NULL)",
            name="ck_translation_research_tool_call_fence_shape",
        ),
        sa.CheckConstraint(
            "(claimed_at IS NULL AND operation_version IS NULL) OR "
            "(claimed_at IS NOT NULL AND operation_version IS NOT NULL)",
            name="ck_translation_research_tool_call_claimed_fence",
        ),
        sa.CheckConstraint(
            "(argument_state = 'parsed' AND argument_error_json IS NULL) OR "
            "(argument_state = 'invalid' AND argument_error_json IS NOT NULL)",
            name="ck_translation_research_tool_call_argument_evidence",
        ),
        sa.CheckConstraint(
            "(state = 'prepared' AND claimed_at IS NULL AND finished_at IS NULL) OR "
            "(state = 'executing' AND claimed_at IS NOT NULL AND finished_at IS NULL) OR "
            "(state IN ('succeeded', 'failed', 'uncertain') AND claimed_at IS NOT NULL AND finished_at IS NOT NULL) OR "
            "(state = 'blocked' AND finished_at IS NOT NULL) OR "
            "(state = 'cancelled' AND claimed_at IS NULL AND finished_at IS NOT NULL)",
            name="ck_translation_research_tool_call_timestamps",
        ),
        sa.CheckConstraint(
            "(state IN ('prepared', 'executing') AND result_json IS NULL AND "
            "error_json IS NULL AND result_hash IS NULL AND error_hash IS NULL AND "
            "tool_message_content_json IS NULL AND tool_message_hash IS NULL) OR "
            "(state = 'succeeded' AND result_json IS NOT NULL AND error_json IS NULL AND "
            "result_hash IS NOT NULL AND error_hash IS NULL AND "
            "tool_message_content_json IS NOT NULL AND tool_message_hash IS NOT NULL) OR "
            "(state IN ('blocked', 'failed', 'uncertain', 'cancelled') AND "
            "result_json IS NULL AND error_json IS NOT NULL AND result_hash IS NULL AND "
            "error_hash IS NOT NULL AND tool_message_content_json IS NOT NULL AND "
            "tool_message_hash IS NOT NULL)",
            name="ck_translation_research_tool_call_terminal_evidence",
        ),
        _hash("source_plan_hash", "tool_call_plan_hash"),
        _hash("lease_fence", "tool_call_lease_fence"),
        sa.CheckConstraint(
            "length(provider_call_key) = 64",
            name="ck_translation_research_tool_call_provider_key",
        ),
        _hash("declaration_hash", "tool_call_declaration_hash"),
        _hash("raw_arguments_hash", "tool_call_raw_arguments_hash"),
        _hash("arguments_hash", "tool_call_arguments_hash"),
        _hash("call_record_hash", "tool_call_record_hash"),
        _hash("state_head_receipt", "tool_call_state_head_receipt"),
        sa.CheckConstraint("result_hash IS NULL OR length(result_hash) = 64", name="ck_translation_research_tool_call_result_hash"),
        sa.CheckConstraint("error_hash IS NULL OR length(error_hash) = 64", name="ck_translation_research_tool_call_error_hash"),
        sa.CheckConstraint("tool_message_hash IS NULL OR length(tool_message_hash) = 64", name="ck_translation_research_tool_call_message_hash"),
        sa.ForeignKeyConstraint(["run_id", "source_plan_id"], ["translation_research_run.id", "translation_research_run.source_plan_id"], ondelete="CASCADE", name="fk_translation_research_tool_call_run_plan"),
        sa.ForeignKeyConstraint(["turn_id", "run_id", "source_plan_id"], ["translation_research_turn.id", "translation_research_turn.run_id", "translation_research_turn.source_plan_id"], ondelete="CASCADE", name="fk_translation_research_tool_call_turn_scope"),
        sa.ForeignKeyConstraint(["project_id"], ["translation_project.id"], ondelete="CASCADE", name="fk_translation_research_tool_call_project"),
        sa.ForeignKeyConstraint(["owner_user_id"], ["user.id"], ondelete="CASCADE", name="fk_translation_research_tool_call_owner"),
    )
    op.create_index("ix_translation_research_tool_call_turn_index", "translation_research_tool_call", ["turn_id", "call_index"])

    op.create_table(
        "translation_research_tool_result_blob",
        sa.Column("tool_call_id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("turn_id", sa.String(36), nullable=False),
        sa.Column("project_id", sa.String(36), nullable=False),
        sa.Column("owner_user_id", sa.Integer(), nullable=False),
        sa.Column("operation_id", sa.String(36), nullable=False),
        sa.Column("source_plan_id", sa.String(36), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("payload_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("record_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "payload_size_bytes >= 0",
            name="ck_translation_research_tool_result_blob_size",
        ),
        _hash("payload_hash", "tool_result_blob_payload_hash"),
        _hash("record_hash", "tool_result_blob_record_hash"),
        sa.ForeignKeyConstraint(
            ["tool_call_id"],
            ["translation_research_tool_call.id"],
            ondelete="CASCADE",
            name="fk_translation_research_tool_result_blob_call",
        ),
        sa.ForeignKeyConstraint(
            ["run_id", "source_plan_id"],
            ["translation_research_run.id", "translation_research_run.source_plan_id"],
            ondelete="CASCADE",
            name="fk_translation_research_tool_result_blob_run_plan",
        ),
    )
    op.create_index(
        "ix_translation_research_tool_result_blob_run_created",
        "translation_research_tool_result_blob",
        ["run_id", "created_at"],
    )

    op.create_table(
        "translation_research_tool_call_event",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tool_call_id", sa.String(36), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("predecessor_event_hash", sa.String(64)),
        sa.Column("event_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("tool_call_id", "sequence", name="uq_translation_research_tool_call_event_sequence"),
        sa.CheckConstraint("sequence >= 1", name="ck_translation_research_tool_call_event_sequence"),
        sa.CheckConstraint("event_type IN ('prepared', 'executing', 'succeeded', 'blocked', 'failed', 'uncertain', 'cancelled')", name="ck_translation_research_tool_call_event_type"),
        sa.CheckConstraint("length(payload_hash) = 64", name="ck_translation_research_tool_call_event_payload_hash"),
        sa.CheckConstraint("length(event_hash) = 64", name="ck_translation_research_tool_call_event_hash"),
        sa.CheckConstraint("predecessor_event_hash IS NULL OR length(predecessor_event_hash) = 64", name="ck_translation_research_tool_call_event_predecessor_hash"),
        sa.ForeignKeyConstraint(["tool_call_id"], ["translation_research_tool_call.id"], ondelete="CASCADE", name="fk_translation_research_tool_call_event_call"),
    )
    op.create_index("ix_translation_research_tool_call_event_call_created", "translation_research_tool_call_event", ["tool_call_id", "created_at"])


def downgrade() -> None:
    _assert_no_evidence(direction="downgrade", allow_v2=False)
    op.drop_index("ix_translation_research_tool_call_event_call_created", table_name="translation_research_tool_call_event")
    op.drop_table("translation_research_tool_call_event")
    op.drop_index(
        "ix_translation_research_tool_result_blob_run_created",
        table_name="translation_research_tool_result_blob",
    )
    op.drop_table("translation_research_tool_result_blob")
    op.drop_index("ix_translation_research_tool_call_turn_index", table_name="translation_research_tool_call")
    op.drop_table("translation_research_tool_call")
    op.drop_index("ix_translation_research_turn_run_sequence", table_name="translation_research_turn")
    op.drop_table("translation_research_turn")
    op.drop_index("ix_translation_research_run_plan_state", table_name="translation_research_run")
    op.drop_index("ix_translation_research_run_operation_created", table_name="translation_research_run")
    op.drop_table("translation_research_run")

    _replace_analysis_contract_check(
        op.get_bind(),
        source=_V2_CONTRACT_CHECK,
        target=_V1_CONTRACT_CHECK,
    )
