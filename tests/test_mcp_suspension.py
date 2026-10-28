from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from content_storage_support import bind_content_policy
from flask import Flask

from iarf_assistant.core.encryption import EncryptionService, InstallationKeyStore
from iarf_assistant.core.mcp_jobs import (
    MCP_ACTIVE_EXECUTION_STATUSES,
    MCP_EXECUTION_STATUSES,
    MCP_NONTERMINAL_EXECUTION_STATUSES,
    MCP_TERMINAL_EXECUTION_STATUSES,
    claim_next_work,
    create_execution,
    get_or_create_work,
    issue_access_key,
    list_executions,
    resolve_access_key,
    submit_work,
    transition_execution,
)
from iarf_assistant.extensions import db
from iarf_assistant.models import (
    McpWorkItem,
    McpWorkSubmission,
    User,
)

MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "iarf_assistant/migrations/versions/20260930_mcp_suspension.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location(
        "mcp_suspension_migration", MIGRATION_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_migration(connection: sa.Connection, migration, direction: str) -> None:
    connection.commit()
    context = MigrationContext.configure(
        connection,
        opts={"render_as_batch": True, "transaction_per_migration": True},
    )
    migration.op = Operations(context)
    with context.begin_transaction(_per_migration=True):
        getattr(migration, direction)()


def test_suspension_migration_retains_linear_chain():
    config = Config()
    config.set_main_option("script_location", str(MIGRATION_PATH.parent.parent))
    scripts = ScriptDirectory.from_config(config)

    assert scripts.get_heads() == ["20261004_llm_audio_input"]
    assert scripts.get_revision("20261004_llm_audio_input").down_revision == (
        "20260930_mcp_suspension"
    )
    assert scripts.get_revision("20260930_mcp_suspension").down_revision == (
        "20260926_mcp_transport"
    )


@pytest.fixture
def app(tmp_path: Path):
    application = Flask(__name__, instance_path=str(tmp_path))
    bind_content_policy(application)
    application.config.update(
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{tmp_path / 'mcp-suspension.db'}",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SECRET_KEY="mcp-suspension-test-secret",
        TESTING=True,
    )
    db.init_app(application)
    application.extensions["carlina_encryption"] = EncryptionService(
        InstallationKeyStore.ephemeral()
    )
    context = application.app_context()
    context.push()
    db.create_all()
    db.session.add(User(id=1, email="owner@example.test", password_hash="x"))
    db.session.commit()
    try:
        yield application
    finally:
        db.session.remove()
        db.drop_all()
        db.engine.dispose()
        context.pop()


def test_suspension_releases_leases_and_preserves_work_history(app):
    with app.app_context():
        issued = issue_access_key(
            1,
            "suspension test",
            ["mcp.work", "mcp.doc_processor"],
            [],
        )
        principal = resolve_access_key(issued.raw_token)
        execution = create_execution(
            1,
            "doc_processor",
            "document",
            "suspension-document",
            {"prompt": "synthetic parent request"},
            mode="passive",
        )

        leased_resolution = get_or_create_work(
            execution.id, {"prompt": "retain leased request"}
        )
        leased_claim = claim_next_work(
            principal,
            "suspension-worker",
            addon_id="doc_processor",
            execution_id=execution.id,
        )
        assert leased_claim is not None
        assert leased_claim.work_item_id == leased_resolution.work_item_id

        completed_resolution = get_or_create_work(
            execution.id, {"prompt": "retain completed response"}
        )
        completed_claim = claim_next_work(
            principal,
            "completion-worker",
            addon_id="doc_processor",
            execution_id=execution.id,
        )
        assert completed_claim is not None
        assert completed_claim.work_item_id == completed_resolution.work_item_id
        accepted = submit_work(
            principal,
            completed_resolution.work_item_id,
            completed_claim.lease_token,
            {"content": "accepted synthetic response"},
        )
        assert accepted["status"] == "accepted"

        pending_resolution = get_or_create_work(
            execution.id, {"prompt": "retain pending request"}
        )
        leased_item = db.session.get(McpWorkItem, leased_resolution.work_item_id)
        completed_item = db.session.get(
            McpWorkItem, completed_resolution.work_item_id
        )
        pending_item = db.session.get(McpWorkItem, pending_resolution.work_item_id)
        assert leased_item is not None and completed_item is not None
        assert pending_item is not None
        leased_version = leased_item.version
        completed_response = completed_item.accepted_response_json
        submission_count = McpWorkSubmission.query.count()
        before_rows = db.session.connection().exec_driver_sql(
            """
            SELECT id, request_json, accepted_response_json, boundary_input_json
            FROM mcp_work_item WHERE execution_id = ? ORDER BY id
            """,
            (execution.id,),
        ).fetchall()

        suspended = transition_execution(execution, "suspended")
        db.session.commit()

        assert suspended.status == "suspended"
        assert "suspended" in MCP_EXECUTION_STATUSES
        assert "suspended" not in MCP_ACTIVE_EXECUTION_STATUSES
        assert "suspended" not in MCP_TERMINAL_EXECUTION_STATUSES
        assert "suspended" in MCP_NONTERMINAL_EXECUTION_STATUSES
        assert list_executions(principal, include_terminal=False)[0]["status"] == (
            "suspended"
        )

        db.session.expire_all()
        fenced_lease = db.session.get(McpWorkItem, leased_resolution.work_item_id)
        preserved_completed = db.session.get(
            McpWorkItem, completed_resolution.work_item_id
        )
        preserved_pending = db.session.get(
            McpWorkItem, pending_resolution.work_item_id
        )
        assert fenced_lease is not None
        assert fenced_lease.state == "pending"
        assert fenced_lease.version == leased_version + 1
        assert fenced_lease.lease_token is None
        assert fenced_lease.lease_owner is None
        assert fenced_lease.lease_expires_at is None
        assert fenced_lease.leased_at is None
        assert preserved_completed is not None
        assert preserved_completed.state == "completed"
        assert preserved_completed.accepted_response_json == completed_response
        assert preserved_pending is not None
        assert preserved_pending.state == "pending"
        assert McpWorkSubmission.query.count() == submission_count
        after_rows = db.session.connection().exec_driver_sql(
            """
            SELECT id, request_json, accepted_response_json, boundary_input_json
            FROM mcp_work_item WHERE execution_id = ? ORDER BY id
            """,
            (execution.id,),
        ).fetchall()
        assert after_rows == before_rows

        assert (
            claim_next_work(
                principal,
                "suspended-worker",
                addon_id="doc_processor",
                execution_id=execution.id,
            )
            is None
        )
        rejected = submit_work(
            principal,
            leased_resolution.work_item_id,
            leased_claim.lease_token,
            {"content": "late synthetic response"},
        )
        assert rejected["status"] == "rejected"
        assert rejected["error_code"] == "execution_terminal"
        assert db.session.get(
            McpWorkItem, completed_resolution.work_item_id
        ).accepted_response_json == completed_response


def _create_migration_fixture(engine: sa.Engine) -> None:
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE user (id INTEGER PRIMARY KEY)")
        connection.exec_driver_sql("INSERT INTO user (id) VALUES (1)")
        connection.exec_driver_sql(
            """
            CREATE TABLE mcp_execution (
                id VARCHAR(36) PRIMARY KEY,
                owner_user_id INTEGER NOT NULL
                    REFERENCES user(id) ON DELETE CASCADE,
                status VARCHAR(32) NOT NULL,
                request_json TEXT NOT NULL,
                CONSTRAINT ck_mcp_execution_status CHECK (
                    status IN ('queued', 'running', 'waiting_external',
                               'waiting_review', 'completed', 'failed', 'cancelled')
                )
            )
            """
        )
        connection.exec_driver_sql(
            """
            CREATE TABLE mcp_work_item (
                id VARCHAR(36) PRIMARY KEY,
                execution_id VARCHAR(36) NOT NULL
                    REFERENCES mcp_execution(id) ON DELETE CASCADE,
                request_json TEXT NOT NULL,
                accepted_response_json TEXT,
                boundary_input_json TEXT,
                state VARCHAR(16) NOT NULL
            )
            """
        )
        connection.exec_driver_sql(
            """
            CREATE TABLE mcp_work_submission (
                id VARCHAR(36) PRIMARY KEY,
                work_item_id VARCHAR(36) NOT NULL
                    REFERENCES mcp_work_item(id) ON DELETE CASCADE,
                response_json TEXT NOT NULL,
                error_detail TEXT
            )
            """
        )
        connection.exec_driver_sql(
            """
            INSERT INTO mcp_execution(id, owner_user_id, status, request_json)
            VALUES ('execution-1', 1, 'queued', 'enc:v2:parent-request-bytes')
            """
        )
        connection.exec_driver_sql(
            """
            INSERT INTO mcp_work_item(
                id, execution_id, request_json, accepted_response_json,
                boundary_input_json, state
            ) VALUES
                ('leased-work', 'execution-1', 'enc:v2:leased-request', NULL,
                 'enc:v2:leased-boundary', 'leased'),
                ('completed-work', 'execution-1', 'enc:v2:completed-request',
                 'enc:v2:accepted-response', 'enc:v2:completed-boundary',
                 'completed')
            """
        )
        connection.exec_driver_sql(
            """
            INSERT INTO mcp_work_submission(
                id, work_item_id, response_json, error_detail
            ) VALUES (
                'submission-1', 'completed-work', 'enc:v2:submission-response',
                'enc:v2:submission-detail'
            )
            """
        )


def test_suspension_migration_rebuild_preserves_encrypted_children_and_retries(
    tmp_path: Path,
):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'mcp-suspension-migration.db'}")

    @sa.event.listens_for(engine, "connect")
    def _enable_foreign_keys(connection, _record) -> None:
        connection.execute("PRAGMA foreign_keys=ON")

    _create_migration_fixture(engine)
    migration = _load_migration()
    assert migration.revision == "20260930_mcp_suspension"
    assert migration.down_revision == "20260926_mcp_transport"

    with engine.connect() as connection:
        parent_before = connection.exec_driver_sql(
            "SELECT id, status, request_json FROM mcp_execution"
        ).fetchall()
        items_before = connection.exec_driver_sql(
            """
            SELECT id, request_json, accepted_response_json, boundary_input_json
            FROM mcp_work_item ORDER BY id
            """
        ).fetchall()
        submissions_before = connection.exec_driver_sql(
            "SELECT id, work_item_id, response_json, error_detail "
            "FROM mcp_work_submission"
        ).fetchall()

        _run_migration(connection, migration, "upgrade")
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall() == []
        assert connection.exec_driver_sql(
            "SELECT id, status, request_json FROM mcp_execution"
        ).fetchall() == parent_before
        assert connection.exec_driver_sql(
            """
            SELECT id, request_json, accepted_response_json, boundary_input_json
            FROM mcp_work_item ORDER BY id
            """
        ).fetchall() == items_before
        assert connection.exec_driver_sql(
            "SELECT id, work_item_id, response_json, error_detail "
            "FROM mcp_work_submission"
        ).fetchall() == submissions_before

        upgraded_rootpage = connection.exec_driver_sql(
            "SELECT rootpage FROM sqlite_master WHERE type='table' "
            "AND name='mcp_execution'"
        ).scalar_one()
        _run_migration(connection, migration, "upgrade")
        assert connection.exec_driver_sql(
            "SELECT rootpage FROM sqlite_master WHERE type='table' "
            "AND name='mcp_execution'"
        ).scalar_one() == upgraded_rootpage

        connection.exec_driver_sql(
            "UPDATE mcp_execution SET status='suspended' WHERE id='execution-1'"
        )
        connection.commit()
        with pytest.raises(RuntimeError, match="suspended MCP executions exist"):
            _run_migration(connection, migration, "downgrade")
        assert connection.exec_driver_sql(
            "SELECT status FROM mcp_execution WHERE id='execution-1'"
        ).scalar_one() == "suspended"

        connection.exec_driver_sql(
            "UPDATE mcp_execution SET status='queued' WHERE id='execution-1'"
        )
        connection.commit()
        _run_migration(connection, migration, "downgrade")
        _run_migration(connection, migration, "downgrade")
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall() == []
        assert connection.exec_driver_sql(
            "SELECT id, status, request_json FROM mcp_execution"
        ).fetchall() == parent_before
        with pytest.raises(sa.exc.IntegrityError), connection.begin_nested():
            connection.exec_driver_sql(
                "UPDATE mcp_execution SET status='suspended' WHERE id='execution-1'"
            )

    engine.dispose()
