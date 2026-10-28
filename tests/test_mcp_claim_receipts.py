from __future__ import annotations

import importlib.util
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Event

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from content_storage_support import bind_content_policy
from flask import Flask

from iarf_assistant.core.encryption import EncryptionService, InstallationKeyStore
from iarf_assistant.core.mcp_jobs import (
    McpAuthError,
    McpPrincipal,
    McpWorkError,
    claim_next_work,
    create_execution,
    get_or_create_work,
    issue_access_key,
    release_work,
    resolve_access_key,
    submit_work,
    transition_execution,
)
from iarf_assistant.extensions import db
from iarf_assistant.models import McpWorkClaimReceipt, McpWorkItem, User


@pytest.fixture
def app(tmp_path: Path):
    application = Flask(__name__, instance_path=str(tmp_path))
    bind_content_policy(application)
    application.config.update(
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{tmp_path / 'mcp-receipts.db'}",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SECRET_KEY="mcp-receipts-test-secret",
        TESTING=True,
    )
    db.init_app(application)
    application.extensions["carlina_encryption"] = EncryptionService(
        InstallationKeyStore.ephemeral()
    )
    context = application.app_context()
    context.push()
    db.create_all()
    db.session.add_all(
        [
            User(id=1, email="receipt-owner@example.test", password_hash="x"),
            User(id=2, email="receipt-other@example.test", password_hash="x"),
        ]
    )
    db.session.commit()
    try:
        yield application
    finally:
        db.session.remove()
        db.drop_all()
        db.engine.dispose()
        context.pop()


def _principal(owner_id: int = 1):
    issued = issue_access_key(
        owner_id,
        f"receipt-key-{owner_id}",
        ["mcp.work", "mcp.doc_processor"],
        [],
    )
    return issued, resolve_access_key(issued.raw_token)


def _execution_and_work():
    execution = create_execution(
        1,
        "doc_processor",
        "document",
        "receipt-document",
        {"prompt": "synthetic receipt test"},
        mode="passive",
    )
    work = get_or_create_work(
        execution.id,
        {
            "source": "doc_processor_pdf_parser",
            "messages": [{"role": "user", "content": "synthetic packet"}],
        },
    )
    return execution, work


def _claim(principal, execution_id: str, *, request_id: str = "claim-1", **kwargs):
    return claim_next_work(
        principal,
        kwargs.pop("worker_id", "receipt-worker"),
        addon_id=kwargs.pop("addon_id", "doc_processor"),
        execution_id=execution_id,
        claim_request_id=request_id,
        **kwargs,
    )


def _stage_caller_writes() -> None:
    """Leave both Core SQL and ORM work flushed before the claim call."""

    db.session.execute(
        sa.text("UPDATE user SET email = :email WHERE id = 1"),
        {"email": "sql-edit@example.test"},
    )
    owner = db.session.get(User, 1)
    assert owner is not None
    owner.password_hash = "orm-edit"
    db.session.flush()


def _force_first_work_update_to_zero(monkeypatch):
    session = db.session()
    original_execute = session.execute
    state = {"forced": False}

    def execute(statement, *args, **kwargs):
        statement_text = str(statement).upper()
        if not state["forced"] and statement_text.startswith("UPDATE MCP_WORK_ITEM"):
            state["forced"] = True
            return type("ZeroRowcount", (), {"rowcount": 0})()
        return original_execute(statement, *args, **kwargs)

    monkeypatch.setattr(session, "execute", execute)
    return state


def test_same_key_replay_returns_current_lease_without_new_attempt(app):
    with app.app_context():
        _, principal = _principal()
        execution, work = _execution_and_work()

        first = _claim(principal, execution.id)
        assert first is not None
        second = _claim(principal, execution.id)

        assert second is not None
        assert second.work_item_id == first.work_item_id == work.work_item_id
        assert second.lease_token == first.lease_token
        assert second.lease_expires_at == first.lease_expires_at
        assert second.version == first.version == 1
        assert second.attempt_count == first.attempt_count == 1
        assert db.session.query(McpWorkClaimReceipt).count() == 1

        receipt = db.session.query(McpWorkClaimReceipt).one()
        assert receipt.attempt_count == 1
        assert receipt.request_id == "claim-1"
        assert receipt.request_fingerprint != "synthetic packet"
        assert not {"lease_token", "request_json", "prompt"} & {
            column.name for column in McpWorkClaimReceipt.__table__.columns
        }


def test_replay_reads_a_renewed_current_expiry_without_new_attempt(app):
    with app.app_context():
        _, principal = _principal()
        execution, _ = _execution_and_work()
        first = _claim(principal, execution.id, lease_seconds=60)
        assert first is not None

        from iarf_assistant.core.mcp_jobs import renew_work

        renewed = renew_work(
            principal,
            first.work_item_id,
            first.lease_token,
            lease_seconds=120,
        )
        replay = _claim(principal, execution.id, lease_seconds=60)

        assert replay is not None
        assert replay.lease_token == first.lease_token
        assert (
            replay.lease_expires_at.replace(microsecond=0).isoformat() + "Z"
            == renewed["lease_expires_at"]
        )
        assert replay.version == renewed["version"]
        assert replay.attempt_count == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"worker_id": "different-worker"},
        {"batch_id": "different-batch"},
        {"lease_seconds": 120},
        {"workload_role": "different-role"},
    ],
)
def test_replay_with_changed_claim_inputs_conflicts(app, changes):
    with app.app_context():
        _, principal = _principal()
        execution, _ = _execution_and_work()
        first = _claim(principal, execution.id, lease_seconds=60)
        assert first is not None

        claim_kwargs = dict(changes)
        claim_kwargs.setdefault("lease_seconds", 60)
        with pytest.raises(McpWorkError) as exc_info:
            _claim(principal, execution.id, **claim_kwargs)
        assert exc_info.value.code == "claim_request_conflict"
        assert exc_info.value.status_code == 409


def test_empty_poll_reuses_request_id_without_a_receipt(app):
    with app.app_context():
        _, principal = _principal()
        execution = create_execution(
            1,
            "doc_processor",
            "document",
            "empty-receipt-document",
            {"prompt": "empty"},
            mode="passive",
        )

        assert _claim(principal, execution.id) is None
        assert _claim(principal, execution.id) is None
        assert db.session.query(McpWorkClaimReceipt).count() == 0


def test_release_reclaim_closes_old_request_and_new_request_claims_new_attempt(app):
    with app.app_context():
        _, principal = _principal()
        execution, _ = _execution_and_work()
        first = _claim(principal, execution.id)
        assert first is not None
        release_work(principal, first.work_item_id, first.lease_token)

        with pytest.raises(McpWorkError) as closed:
            _claim(principal, execution.id)
        assert closed.value.code == "claim_request_closed"

        second = _claim(principal, execution.id, request_id="claim-2")
        assert second is not None
        assert second.lease_token != first.lease_token
        assert second.attempt_count == 2
        assert db.session.query(McpWorkClaimReceipt).count() == 2


def test_expired_lease_closes_old_request_and_new_request_reclaims(app):
    with app.app_context():
        _, principal = _principal()
        execution, work = _execution_and_work()
        first = _claim(principal, execution.id)
        assert first is not None

        item = db.session.get(McpWorkItem, work.work_item_id)
        assert item is not None
        item.lease_expires_at = item.lease_expires_at.replace(year=2000)
        db.session.commit()

        with pytest.raises(McpWorkError) as closed:
            _claim(principal, execution.id)
        assert closed.value.code == "claim_request_closed"

        second = _claim(principal, execution.id, request_id="claim-2")
        assert second is not None
        assert second.attempt_count == 2


def test_terminal_execution_closes_receipt_without_deleting_it(app):
    with app.app_context():
        _, principal = _principal()
        execution, _ = _execution_and_work()
        first = _claim(principal, execution.id)
        assert first is not None

        transition_execution(execution.id, "cancelled")
        db.session.commit()

        with pytest.raises(McpWorkError) as closed:
            _claim(principal, execution.id)
        assert closed.value.code == "claim_request_closed"
        assert db.session.query(McpWorkClaimReceipt).count() == 1


def test_invalid_request_ids_are_rejected_before_claiming(app):
    with app.app_context():
        _, principal = _principal()
        execution, _ = _execution_and_work()
        for request_id in ("", "has space", "ümlaut", "x" * 129):
            with pytest.raises(McpWorkError) as exc_info:
                _claim(principal, execution.id, request_id=request_id)
            assert exc_info.value.code == "invalid_claim_request_id"


def test_other_key_cannot_recover_an_active_receipt(app):
    with app.app_context():
        _, principal = _principal(1)
        _, other_principal = _principal(1)
        execution, _ = _execution_and_work()
        first = _claim(principal, execution.id)
        assert first is not None

        other = _claim(other_principal, execution.id)
        assert other is None
        assert db.session.query(McpWorkClaimReceipt).count() == 1


def test_independent_simultaneous_same_id_requests_share_one_lease(app):
    with app.app_context():
        _, principal = _principal()
        execution, _ = _execution_and_work()
        execution_id = execution.id

    barrier = Barrier(2)

    def claim():
        with app.app_context():
            barrier.wait()
            return _claim(principal, execution_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: claim(), range(2)))

    assert all(result is not None for result in results)
    assert results[0].lease_token == results[1].lease_token
    assert results[0].attempt_count == results[1].attempt_count == 1
    with app.app_context():
        assert db.session.query(McpWorkClaimReceipt).count() == 1
        item = db.session.get(McpWorkItem, results[0].work_item_id)
        assert item is not None
        assert item.attempt_count == 1


def test_late_receipt_recovery_after_initial_miss_returns_peer_lease(app, monkeypatch):
    with app.app_context():
        _, principal = _principal()
        execution, _ = _execution_and_work()
        execution_id = execution.id
        db.session.commit()

    module = sys.modules[claim_next_work.__module__]
    original_recover = module._recover_claim_receipt
    initial_miss = Event()
    allow_b_to_continue = Event()
    state = {"first": True}

    def recover_with_initial_miss(*args, **kwargs):
        if state["first"]:
            state["first"] = False
            result = original_recover(*args, **kwargs)
            assert result is None
            initial_miss.set()
            assert allow_b_to_continue.wait(10)
            return None
        return original_recover(*args, **kwargs)

    monkeypatch.setattr(module, "_recover_claim_receipt", recover_with_initial_miss)
    results = {}

    def claim_b():
        with app.app_context():
            return _claim(principal, execution_id)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(claim_b)
        assert initial_miss.wait(10)
        try:
            with app.app_context():
                results["a"] = _claim(principal, execution_id)
        finally:
            allow_b_to_continue.set()
        results["b"] = future.result(timeout=10)

    first = results["a"]
    second = results["b"]
    assert first is not None
    assert second is not None
    assert second.work_item_id == first.work_item_id
    assert second.lease_token == first.lease_token
    assert second.attempt_count == first.attempt_count == 1
    with app.app_context():
        assert db.session.query(McpWorkClaimReceipt).count() == 1


def test_lost_cas_removes_only_own_receipt_and_preserves_flushed_caller_writes(
    app, monkeypatch
):
    with app.app_context():
        _, principal = _principal()
        execution, work = _execution_and_work()
        _stage_caller_writes()
        state = _force_first_work_update_to_zero(monkeypatch)

        result = _claim(principal, execution.id)
        assert result is None
        assert state["forced"] is True
        db.session.expire_all()
        owner = db.session.get(User, 1)
        item = db.session.get(McpWorkItem, work.work_item_id)
        assert owner is not None
        assert item is not None
        assert owner.email == "sql-edit@example.test"
        assert owner.password_hash == "orm-edit"
        assert item.state == "pending"
        assert item.attempt_count == 0
        assert db.session.query(McpWorkClaimReceipt).count() == 0

        retry = _claim(principal, execution.id, request_id="cas-next")
        assert retry is not None
        assert retry.work_item_id == work.work_item_id
        assert retry.attempt_count == 1


def test_receipt_conflict_recovery_preserves_caller_writes_and_does_not_claim_next(
    app, monkeypatch
):
    with app.app_context():
        _, principal = _principal()
        execution, first_work = _execution_and_work()
        second_work = get_or_create_work(
            execution.id,
            {"prompt": "second pending packet"},
        )
        first = _claim(principal, execution.id)
        assert first is not None
        _stage_caller_writes()

        module = sys.modules[claim_next_work.__module__]
        original_recover = module._recover_claim_receipt
        state = {"first": True}

        def recover_with_initial_miss(*args, **kwargs):
            if state["first"]:
                state["first"] = False
                return None
            return original_recover(*args, **kwargs)

        monkeypatch.setattr(module, "_recover_claim_receipt", recover_with_initial_miss)
        replay = _claim(principal, execution.id)

        assert replay is not None
        assert replay.work_item_id == first_work.work_item_id
        assert replay.lease_token == first.lease_token
        assert replay.attempt_count == 1
        db.session.expire_all()
        owner = db.session.get(User, 1)
        pending = db.session.get(McpWorkItem, second_work.work_item_id)
        assert owner is not None
        assert pending is not None
        assert owner.email == "sql-edit@example.test"
        assert owner.password_hash == "orm-edit"
        assert pending.state == "pending"
        assert pending.attempt_count == 0
        assert db.session.query(McpWorkClaimReceipt).count() == 1


def test_owner_scope_and_completed_submission_close_receipt(app):
    with app.app_context():
        _, principal = _principal(1)
        _, other_owner = _principal(2)
        execution, _ = _execution_and_work()
        first = _claim(principal, execution.id)
        assert first is not None

        restricted = McpPrincipal(
            key_id=principal.key_id,
            owner_user_id=principal.owner_user_id,
            scopes=frozenset({"mcp.work"}),
            allowed_roots=(),
        )
        with pytest.raises(McpAuthError) as scope_error:
            _claim(restricted, execution.id)
        assert scope_error.value.code == "scope_denied"

        with pytest.raises(McpWorkError) as owner_error:
            _claim(other_owner, execution.id)
        assert owner_error.value.code == "execution_not_found"

        completed = create_execution(
            1,
            "doc_processor",
            "document",
            "receipt-completed",
            {"prompt": "accepted submission"},
            mode="passive",
        )
        completed_work = get_or_create_work(completed.id, {"prompt": "answer"})
        accepted = _claim(principal, completed.id, request_id="accepted-1")
        assert accepted is not None
        submitted = submit_work(
            principal,
            completed_work.work_item_id,
            accepted.lease_token,
            {"content": "accepted"},
        )
        assert submitted["status"] == "accepted"

        with pytest.raises(McpWorkError) as closed:
            _claim(principal, completed.id, request_id="accepted-1")
        assert closed.value.code == "claim_request_closed"


def test_claim_receipt_migration_is_additive_and_reversible(tmp_path: Path):
    database = sa.create_engine(f"sqlite:///{tmp_path / 'claim-receipts-migration.db'}")
    metadata = sa.MetaData()
    sa.Table(
        "user",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("email", sa.String(255), nullable=False),
    )
    sa.Table(
        "mcp_access_key",
        metadata,
        sa.Column("key_id", sa.String(36), primary_key=True),
        sa.Column("label", sa.String(255), nullable=False),
    )
    sa.Table(
        "mcp_execution",
        metadata,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("status", sa.String(32), nullable=False),
    )
    sa.Table(
        "mcp_work_item",
        metadata,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("state", sa.String(16), nullable=False),
    )
    metadata.create_all(database)
    migration_path = (
        Path(sys.modules[McpWorkItem.__module__].__file__).resolve().parent
        / "migrations"
        / "versions"
        / "20260920_mcp_claim_receipts.py"
    )
    spec = importlib.util.spec_from_file_location("mcp_claim_receipts_migration", migration_path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    try:
        with database.begin() as connection:
            connection.execute(sa.text("PRAGMA foreign_keys=ON"))
            connection.execute(
                sa.text("INSERT INTO user (id, email) VALUES (1, 'legacy@example.test')")
            )
            connection.execute(
                sa.text("INSERT INTO mcp_access_key (key_id, label) VALUES ('key', 'legacy-key')")
            )
            connection.execute(
                sa.text("INSERT INTO mcp_execution (id, status) VALUES ('execution', 'running')")
            )
            connection.execute(
                sa.text("INSERT INTO mcp_work_item (id, state) VALUES ('work', 'pending')")
            )
            before = connection.execute(
                sa.text(
                    "SELECT (SELECT email FROM user WHERE id = 1), "
                    "(SELECT label FROM mcp_access_key WHERE key_id = 'key'), "
                    "(SELECT status FROM mcp_execution WHERE id = 'execution'), "
                    "(SELECT state FROM mcp_work_item WHERE id = 'work')"
                )
            ).one()
            migration.op = Operations(MigrationContext.configure(connection))
            migration.upgrade()
            connection.execute(
                sa.text(
                    "INSERT INTO mcp_work_claim_receipt "
                    "(id, execution_id, key_id, request_id, request_fingerprint, work_item_id, attempt_count, created_at) "
                    "VALUES ('receipt', 'execution', 'key', 'claim-1', :fingerprint, 'work', 1, CURRENT_TIMESTAMP)"
                ),
                {"fingerprint": "f" * 64},
            )
            assert sa.inspect(connection).has_table("mcp_work_claim_receipt")
            migration.downgrade()
            assert not sa.inspect(connection).has_table("mcp_work_claim_receipt")
            assert sa.inspect(connection).has_table("mcp_work_item")
            after = connection.execute(
                sa.text(
                    "SELECT (SELECT email FROM user WHERE id = 1), "
                    "(SELECT label FROM mcp_access_key WHERE key_id = 'key'), "
                    "(SELECT status FROM mcp_execution WHERE id = 'execution'), "
                    "(SELECT state FROM mcp_work_item WHERE id = 'work')"
                )
            ).one()
            assert after == before
    finally:
        database.dispose()
