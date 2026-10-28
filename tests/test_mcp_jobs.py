from __future__ import annotations

import importlib.util
import inspect
import json
import sqlite3
import sys
import time as time_module
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier, Event, Thread
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from content_storage_support import bind_content_policy
from flask import Flask
from sqlalchemy import text

from iarf_assistant import database_utils
from iarf_assistant.core.encryption import EncryptionService, InstallationKeyStore
from iarf_assistant.core.mcp_jobs import (
    MCP_ACTIVE_EXECUTION_STATUSES,
    MCP_REQUEST_TOMBSTONE,
    McpAuthError,
    McpPrincipal,
    McpWorkError,
    authorize_source_path,
    bind_execution_queue_job,
    cancel_execution,
    claim_next_work,
    configure_access_key_transport,
    create_execution,
    get_execution,
    get_or_create_work,
    issue_access_key,
    list_executions,
    project_execution_batches,
    redact_terminal_work,
    release_work,
    renew_work,
    resolve_access_key,
    revoke_access_key,
    serialize_execution,
    submit_work,
    transition_execution,
    validate_response,
)
from iarf_assistant.database_utils import TransactionScopeError
from iarf_assistant.extensions import db
from iarf_assistant.models import (
    McpAccessKey,
    McpExecution,
    McpWorkItem,
    McpWorkSubmission,
    User,
)

MCP_BATCH_MIGRATION_PATH = (
    Path(sys.modules[McpWorkItem.__module__].__file__).resolve().parent
    / "migrations"
    / "versions"
    / "20260904_mcp_batch_dispatch.py"
)
MCP_RETENTION_MIGRATION_PATH = MCP_BATCH_MIGRATION_PATH.with_name(
    "20260904_mcp_request_retention.py"
)


def _load_batch_dispatch_migration():
    spec = importlib.util.spec_from_file_location(
        "mcp_batch_dispatch_migration", MCP_BATCH_MIGRATION_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_request_retention_migration():
    spec = importlib.util.spec_from_file_location(
        "mcp_request_retention_migration", MCP_RETENTION_MIGRATION_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def app(tmp_path: Path):
    application = Flask(__name__, instance_path=str(tmp_path))
    bind_content_policy(application)
    application.config.update(
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{tmp_path / 'mcp.db'}",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SECRET_KEY="mcp-test-secret",
        TESTING=True,
    )
    db.init_app(application)
    application.extensions["carlina_encryption"] = EncryptionService(
        InstallationKeyStore.ephemeral()
    )
    context = application.app_context()
    context.push()
    db.create_all()
    owner = User(id=1, email="owner@example.test", password_hash="x")
    other = User(id=2, email="other@example.test", password_hash="x")
    db.session.add_all([owner, other])
    db.session.commit()
    try:
        yield application
    finally:
        db.session.remove()
        db.drop_all()
        db.engine.dispose()
        context.pop()


def _principal(app, roots: tuple[str, ...] = ()):
    with app.app_context():
        issued = issue_access_key(
            1,
            "test worker",
            ["mcp.work", "mcp.doc_processor"],
            list(roots),
        )
        return issued, resolve_access_key(issued.raw_token)


def _execution():
    return create_execution(
        1,
        "doc_processor",
        "document",
        "document-1",
        {"prompt": "process this"},
        mode="passive",
    )


def _database_path() -> Path:
    database = db.engine.url.database
    assert database
    return Path(database)


def _set_sqlite_busy_timeout(milliseconds: int = 25) -> None:
    """Set a short timeout on the current pooled connection for lock tests."""

    db.session.connection().exec_driver_sql(
        f"PRAGMA busy_timeout={int(milliseconds)}"
    )
    db.session.rollback()


@contextmanager
def _held_sqlite_writer(path: Path):
    """Hold a real SQLite writer lock until the test releases it."""

    ready = Event()
    release = Event()
    finished = Event()
    failures: list[Exception] = []

    def hold_lock() -> None:
        connection = sqlite3.connect(
            path,
            timeout=0.025,
            check_same_thread=False,
        )
        try:
            connection.execute("PRAGMA busy_timeout=25")
            connection.execute("BEGIN IMMEDIATE")
            ready.set()
            if not release.wait(timeout=10):
                raise AssertionError("timed out waiting to release SQLite writer lock")
        except (AssertionError, sqlite3.Error) as error:
            failures.append(error)
            ready.set()
        finally:
            try:
                connection.rollback()
            finally:
                connection.close()
                finished.set()

    thread = Thread(target=hold_lock, name="mcp-sqlite-lock")
    thread.start()
    assert ready.wait(timeout=5), "SQLite lock thread did not start"
    try:
        if failures:
            raise failures[0]
        yield release, finished
    finally:
        release.set()
        assert finished.wait(timeout=5), "SQLite lock thread did not stop"
        thread.join(timeout=5)
        if failures:
            raise failures[0]


def _release_lock_on_first_retry(
    monkeypatch: pytest.MonkeyPatch,
    release: Event,
    finished: Event,
    *,
    after_release: Callable[[], None] | None = None,
) -> list[float]:
    """Release a real blocker from the retry sleep hook, without sleeping."""

    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        release.set()
        assert finished.wait(timeout=5), "SQLite lock thread did not release"
        if after_release is not None:
            after_release()

    monkeypatch.setattr(
        database_utils,
        "time",
        SimpleNamespace(monotonic=time_module.monotonic, sleep=sleep),
    )
    return sleeps


def test_access_key_is_one_time_raw_and_revocable(app, tmp_path: Path):
    source_root = tmp_path / "sources"
    source_root.mkdir()
    source = source_root / "input.txt"
    source.write_text("synthetic", encoding="utf-8")
    issued, principal = _principal(app, (str(source_root),))
    assert issued.raw_token.startswith("mcp_")
    with app.app_context():
        raw_digest, raw_label, raw_roots = db.session.execute(
            text(
                "SELECT token_digest, label, allowed_roots FROM mcp_access_key "
                "WHERE key_id = :id"
            ),
            {"id": issued.key_id},
        ).one()
        assert issued.raw_token not in raw_digest
        assert raw_digest.startswith("lh1:")
        assert raw_label.startswith("enc:v2:")
        assert raw_roots.startswith("enc:v2:")
        assert authorize_source_path(principal, source) == source.resolve()
        revoke_access_key(issued.key_id)
        db.session.commit()
        with pytest.raises(McpAuthError) as error:
            resolve_access_key(issued.raw_token)
        assert error.value.code == "revoked_key"


@pytest.mark.parametrize(
    ("response_default_bytes", "response_max_bytes"),
    [
        (True, 65536),
        (65535, 65535),
        (67108865, 67108865),
        (65536.0, 65536),
        (2 * 1024 * 1024, 1024 * 1024),
        (65536, False),
    ],
)
def test_issue_access_key_rejects_invalid_response_budgets(
    app,
    response_default_bytes,
    response_max_bytes,
):
    with app.app_context():
        count_before = McpAccessKey.query.count()
        with pytest.raises(McpAuthError) as error:
            issue_access_key(
                1,
                "invalid response budget",
                ["mcp.access"],
                [],
                response_default_bytes=response_default_bytes,
                response_max_bytes=response_max_bytes,
            )
        assert error.value.code == "invalid_response_budget"
        assert McpAccessKey.query.count() == count_before


def test_access_key_response_budgets_keep_defaults_and_accept_extremes(app):
    with app.app_context():
        default_key = issue_access_key(1, "default", ["mcp.access"], [])
        minimum_key = issue_access_key(
            1,
            "minimum",
            ["mcp.access"],
            [],
            response_default_bytes=65536,
            response_max_bytes=65536,
        )
        maximum_key = issue_access_key(
            1,
            "maximum",
            ["mcp.access"],
            [],
            response_default_bytes=67108864,
            response_max_bytes=67108864,
        )

        default_principal = resolve_access_key(default_key.raw_token)
        minimum_principal = resolve_access_key(minimum_key.raw_token)
        maximum_principal = resolve_access_key(maximum_key.raw_token)

        assert (
            default_principal.response_default_bytes,
            default_principal.response_max_bytes,
        ) == (8 * 1024 * 1024, 64 * 1024 * 1024)
        assert (
            minimum_principal.response_default_bytes,
            minimum_principal.response_max_bytes,
        ) == (65536, 65536)
        assert (
            maximum_principal.response_default_bytes,
            maximum_principal.response_max_bytes,
        ) == (67108864, 67108864)


def test_mcp_principal_legacy_constructor_uses_server_transport_defaults():
    principal = McpPrincipal("synthetic-key", 1, frozenset(), ())

    assert (principal.response_default_bytes, principal.response_max_bytes) == (
        8 * 1024 * 1024,
        64 * 1024 * 1024,
    )


def test_malformed_persisted_response_budget_fails_access_key_authentication(app):
    with app.app_context():
        issued = issue_access_key(1, "malformed", ["mcp.access"], [])
        connection = db.session.connection()
        connection.exec_driver_sql("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            text(
                "UPDATE mcp_access_key SET response_max_bytes = :value "
                "WHERE key_id = :key_id"
            ),
            {"value": 67108865, "key_id": issued.key_id},
        )
        db.session.commit()
        db.session.connection().exec_driver_sql(
            "PRAGMA ignore_check_constraints = OFF"
        )

        with pytest.raises(McpAuthError) as error:
            resolve_access_key(issued.raw_token)
        assert error.value.code == "malformed_key"
        assert error.value.status_code == 401


def test_configure_access_key_transport_preserves_key_metadata_and_caller_commit(app):
    with app.app_context():
        issued = issue_access_key(
            1,
            "transport",
            ["mcp.access", "mcp.work"],
            [],
        )
        metadata_sql = text(
            "SELECT owner_user_id, label, token_digest, scopes, allowed_roots, "
            "status, created_at, last_used_at, revoked_at "
            "FROM mcp_access_key WHERE key_id = :key_id"
        )
        before_metadata = db.session.execute(
            metadata_sql, {"key_id": issued.key_id}
        ).one()

        configured = configure_access_key_transport(
            issued.key_id,
            owner_user_id=1,
            response_default_bytes=1024 * 1024,
            response_max_bytes=4 * 1024 * 1024,
        )
        assert configured.response_default_bytes == 1024 * 1024
        assert configured.response_max_bytes == 4 * 1024 * 1024
        assert db.session.execute(
            metadata_sql, {"key_id": issued.key_id}
        ).one() == before_metadata

        db.session.rollback()
        rolled_back = db.session.get(McpAccessKey, issued.key_id)
        assert rolled_back is not None
        assert (
            rolled_back.response_default_bytes,
            rolled_back.response_max_bytes,
        ) == (8 * 1024 * 1024, 64 * 1024 * 1024)

        configure_access_key_transport(
            issued.key_id,
            owner_user_id=1,
            response_default_bytes=1024 * 1024,
            response_max_bytes=4 * 1024 * 1024,
        )
        db.session.commit()
        assert db.session.execute(
            metadata_sql, {"key_id": issued.key_id}
        ).one() == before_metadata
        principal = resolve_access_key(issued.raw_token)
        assert (
            principal.response_default_bytes,
            principal.response_max_bytes,
        ) == (1024 * 1024, 4 * 1024 * 1024)


def test_configure_access_key_transport_enforces_owner_and_active_state(app):
    with app.app_context():
        issued = issue_access_key(1, "owner", ["mcp.access"], [])
        with pytest.raises(McpAuthError) as wrong_owner:
            configure_access_key_transport(
                issued.key_id,
                owner_user_id=2,
                response_default_bytes=1024 * 1024,
                response_max_bytes=4 * 1024 * 1024,
            )
        assert wrong_owner.value.code == "key_not_found"

        revoke_access_key(issued.key_id, owner_user_id=1)
        db.session.commit()
        with pytest.raises(McpAuthError) as revoked:
            configure_access_key_transport(
                issued.key_id,
                owner_user_id=1,
                response_default_bytes=1024 * 1024,
                response_max_bytes=4 * 1024 * 1024,
            )
        assert revoked.value.code == "revoked_key"


def test_configure_access_key_transport_rejects_stale_budget_compare_and_set(
    app,
    monkeypatch: pytest.MonkeyPatch,
):
    with app.app_context():
        issued = issue_access_key(1, "stale transport", ["mcp.access"], [])
        session = db.session()
        assert session.get(McpAccessKey, issued.key_id) is not None
        original_execute = session.execute
        state = {"interleaved": False}

        def execute(statement, *args, **kwargs):
            if (
                not state["interleaved"]
                and getattr(getattr(statement, "table", None), "name", None)
                == "mcp_access_key"
            ):
                state["interleaved"] = True
                session.rollback()
                with db.engine.begin() as connection:
                    connection.execute(
                        text(
                            "UPDATE mcp_access_key "
                            "SET response_default_bytes = :response_default_bytes, "
                            "response_max_bytes = :response_max_bytes "
                            "WHERE key_id = :key_id"
                        ),
                        {
                            "key_id": issued.key_id,
                            "response_default_bytes": 2 * 1024 * 1024,
                            "response_max_bytes": 8 * 1024 * 1024,
                        },
                    )
            return original_execute(statement, *args, **kwargs)

        monkeypatch.setattr(session, "execute", execute)
        with pytest.raises(McpAuthError) as error:
            configure_access_key_transport(
                issued.key_id,
                owner_user_id=1,
                response_default_bytes=1024 * 1024,
                response_max_bytes=4 * 1024 * 1024,
            )
        assert state["interleaved"]
        assert error.value.code == "key_changed"
        assert error.value.status_code == 409
        session.rollback()
        current = session.get(McpAccessKey, issued.key_id)
        assert current is not None
        assert (
            current.response_default_bytes,
            current.response_max_bytes,
        ) == (2 * 1024 * 1024, 8 * 1024 * 1024)


def test_allowed_root_rejects_symlink_escape(app, tmp_path: Path):
    root = tmp_path / "allowed"
    outside = tmp_path / "outside.txt"
    root.mkdir()
    outside.write_text("outside", encoding="utf-8")
    link = root / "link.txt"
    link.symlink_to(outside)
    _, principal = _principal(app, (str(root),))
    with app.app_context(), pytest.raises(McpAuthError) as error:
        authorize_source_path(principal, link)
    assert error.value.code == "source_not_allowed"


def test_execution_binding_is_one_time_and_serialization_is_safe(app):
    with app.app_context():
        execution = _execution()
        assert "request" not in serialize_execution(execution)
        assert serialize_execution(execution, include_request=True)["request"] == {
            "prompt": "process this"
        }
        bind_execution_queue_job(execution, "queue-1")
        db.session.commit()
        bind_execution_queue_job(execution, "queue-1")
        with pytest.raises(McpWorkError) as error:
            bind_execution_queue_job(execution, "queue-2")
        assert error.value.code == "queue_job_conflict"


def test_execution_listing_can_exclude_terminal_history(app):
    _, principal = _principal(app)
    with app.app_context():
        active = _execution()
        terminal = create_execution(
            1,
            "doc_processor",
            "document",
            "terminal-document",
            {"mode": "passive"},
            mode="passive",
        )
        transition_execution(terminal, "completed")
        db.session.commit()

        active_only = list_executions(principal, include_terminal=False)
        completed_only = list_executions(
            principal,
            status="completed",
            include_terminal=False,
        )

    assert [item["execution_id"] for item in active_only] == [active.id]
    assert [item["execution_id"] for item in completed_only] == [terminal.id]


def test_work_creation_is_idempotent_and_requires_clean_session(app):
    with app.app_context():
        execution = _execution()
        first = get_or_create_work(execution.id, {"b": 2, "a": 1})
        second = get_or_create_work(execution.id, {"a": 1, "b": 2})
        assert first.created is True
        assert second.created is False
        assert first.work_item_id == second.work_item_id
        execution_id = execution.id
        pending = User(email="pending@example.test", password_hash="x")
        db.session.add(pending)
        assert db.session.new
        with pytest.raises(McpWorkError) as error:
            get_or_create_work(execution_id, {"new": True})
        assert error.value.code == "dirty_session"
        db.session.rollback()


def test_work_packet_accepts_page_image_payload_larger_than_execution_envelope(app):
    with app.app_context():
        execution = _execution()
        image_payload = "A" * (768 * 1024)
        packet = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/png;base64,{image_payload}"
                            },
                        }
                    ],
                }
            ]
        }

        resolution = get_or_create_work(execution.id, packet)

        assert resolution.created is True
        assert db.session.get(McpWorkItem, resolution.work_item_id).request_json == packet


def test_work_dispatch_indexes_are_validated_and_populated(app):
    context_hash = "a" * 64
    boundary_input = {"last_page": 4, "heading": "Synthetic"}
    with app.app_context():
        execution = _execution()
        indexed = get_or_create_work(
            execution.id,
            {
                "source": "doc_processor_pdf_parser",
                "dispatch": {
                    "batch_id": "batch-1",
                    "phase": "page_extraction",
                    "unit_index": 5,
                    "is_batch_terminal_unit": True,
                    "context_artifact_hash": context_hash,
                    "boundary_input": boundary_input,
                },
            },
        )
        plain = get_or_create_work(execution.id, {"prompt": "unbatched"})

        indexed_item = db.session.get(McpWorkItem, indexed.work_item_id)
        plain_item = db.session.get(McpWorkItem, plain.work_item_id)
        assert indexed_item is not None
        assert indexed_item.batch_id == "batch-1"
        assert indexed_item.dispatch_phase == "page_extraction"
        assert indexed_item.unit_index == 5
        assert indexed_item.is_batch_terminal_unit is True
        assert indexed_item.context_artifact_hash == context_hash
        assert indexed_item.boundary_input_json == boundary_input
        assert plain_item is not None
        assert plain_item.batch_id is None
        assert plain_item.dispatch_phase is None
        assert plain_item.unit_index is None
        assert plain_item.is_batch_terminal_unit is False
        assert plain_item.context_artifact_hash is None
        assert plain_item.boundary_input_json is None
        raw_boundary = db.session.execute(
            text("SELECT boundary_input_json FROM mcp_work_item WHERE id = :id"),
            {"id": indexed.work_item_id},
        ).scalar_one()
        assert raw_boundary.startswith("enc:v2:")


@pytest.mark.parametrize(
    "packet",
    [
        {"dispatch": []},
        {"dispatch": {"batch_id": "not valid"}},
        {"dispatch": {"phase": " "}},
        {"dispatch": {"phase": "x" * 33}},
        {"dispatch": {"unit_index": True}},
        {"dispatch": {"unit_index": 0}},
        {"dispatch": {"is_batch_terminal_unit": "yes"}},
        {"dispatch": {"context_artifact_hash": "A" * 64}},
        {"dispatch": {"boundary_input": {"value": "x" * (256 * 1024)}}},
        {
            "source": "doc_processor_pdf_boundary",
            "dispatch": {
                "batch_id": "batch-1",
                "phase": "page_extraction",
                "unit_index": 1,
                "is_batch_terminal_unit": True,
            },
        },
        {
            "source": "doc_processor_pdf_parser",
            "dispatch": {
                "batch_id": "batch-1",
                "phase": "page_extraction",
                "is_batch_terminal_unit": True,
            },
        },
    ],
)
def test_malformed_work_dispatch_is_rejected_before_insert(app, packet):
    with app.app_context():
        execution = _execution()

        with pytest.raises(McpWorkError) as error:
            get_or_create_work(execution.id, packet)

        assert error.value.code == "invalid_dispatch"
        assert McpWorkItem.query.filter_by(execution_id=execution.id).count() == 0


def test_evaluator_may_be_the_terminal_work_for_an_evaluated_batch_page(app):
    with app.app_context():
        execution = _execution()

        work = get_or_create_work(
            execution.id,
            {
                "source": "doc_processor_pdf_evaluation",
                "dispatch": {
                    "batch_id": "batch-1",
                    "phase": "page_extraction",
                    "unit_index": 7,
                    "is_batch_terminal_unit": True,
                },
            },
        )

        record = db.session.get(McpWorkItem, work.work_item_id)
        assert record is not None
        assert record.batch_id == "batch-1"
        assert record.unit_index == 7
        assert record.is_batch_terminal_unit is True


def test_claim_is_owner_scoped_and_cas_claims_once(app):
    with app.app_context():
        execution = _execution()
        work = get_or_create_work(execution.id, {"prompt": "one"})
        issued, principal = _principal(app)
        outsider = McpPrincipal(
            key_id="other",
            owner_user_id=2,
            scopes=frozenset({"mcp.work", "mcp.doc_processor"}),
            allowed_roots=(),
        )
        with pytest.raises(McpWorkError) as outsider_error:
            claim_next_work(
                outsider,
                "worker",
                addon_id="doc_processor",
                execution_id=execution.id,
            )
        assert outsider_error.value.code == "execution_not_found"
        claim = claim_next_work(
            principal,
            "worker",
            addon_id="doc_processor",
            execution_id=execution.id,
        )
        assert claim is not None
        assert claim.work_item_id == work.work_item_id
        assert claim.request == {"prompt": "one"}
        assert claim.lease_token
        assert claim.attempt_count == 1
        assert (
            claim_next_work(
                principal,
                "other-worker",
                addon_id="doc_processor",
                execution_id=execution.id,
            )
            is None
        )
        del issued


def test_claim_requires_and_is_scoped_to_one_exact_execution(app):
    with app.app_context():
        first_execution = _execution()
        get_or_create_work(first_execution.id, {"prompt": "oldest"})
        second_execution = create_execution(
            1,
            "doc_processor",
            "document",
            "document-2",
            {"prompt": "process another"},
            mode="passive",
        )
        second_work = get_or_create_work(second_execution.id, {"prompt": "selected"})
        _, principal = _principal(app)

        claim = claim_next_work(
            principal,
            "scoped-worker",
            addon_id="doc_processor",
            execution_id=second_execution.id,
        )

        assert claim is not None
        assert claim.execution_id == second_execution.id
        assert claim.work_item_id == second_work.work_item_id
        with pytest.raises(TypeError):
            claim_next_work(  # type: ignore[call-arg]
                principal,
                "unscoped-worker",
                addon_id="doc_processor",
            )
        with pytest.raises(McpWorkError) as blank:
            claim_next_work(
                principal,
                "blank-worker",
                addon_id="doc_processor",
                execution_id=" ",
            )
        assert blank.value.code == "invalid_execution"


def test_role_filter_scans_beyond_first_candidate_page(app):
    with app.app_context():
        execution = _execution()
        for index in range(129):
            get_or_create_work(
                execution.id,
                {
                    "source": "doc_processor_pdf_boundary",
                    "workload_role": "boundary_merge",
                    "messages": [{"role": "user", "content": f"boundary {index}"}],
                },
            )
        selected = get_or_create_work(
            execution.id,
            {
                "source": "doc_processor_pdf_parser",
                "workload_role": "document_page",
                "messages": [{"role": "user", "content": "page"}],
            },
        )
        _, principal = _principal(app)

        claim = claim_next_work(
            principal,
            "page-worker",
            addon_id="doc_processor",
            execution_id=execution.id,
            workload_role="document_page",
        )

        assert claim is not None
        assert claim.work_item_id == selected.work_item_id


def test_batch_filter_is_exact_validated_and_scans_beyond_first_page(app):
    with app.app_context():
        execution = _execution()
        for index in range(129):
            get_or_create_work(
                execution.id,
                {
                    "source": "doc_processor_pdf_parser",
                    "dispatch": {"batch_id": "other", "phase": "page_extraction"},
                    "messages": [{"role": "user", "content": f"other {index}"}],
                },
            )
        selected = get_or_create_work(
            execution.id,
            {
                "source": "doc_processor_pdf_parser",
                "dispatch": {"batch_id": "target", "phase": "page_extraction"},
                "messages": [{"role": "user", "content": "target"}],
            },
        )
        db.session.execute(
            text(
                "UPDATE mcp_work_item SET request_json = :broken "
                "WHERE execution_id = :execution_id AND batch_id = 'other'"
            ),
            {"broken": "enc:v2:broken", "execution_id": execution.id},
        )
        db.session.commit()
        db.session.expire_all()
        _, principal = _principal(app)

        claim = claim_next_work(
            principal,
            "batch-worker",
            addon_id="doc_processor",
            execution_id=execution.id,
            batch_id="target",
        )

        assert claim is not None
        assert claim.work_item_id == selected.work_item_id
        assert claim.batch_id == "target"
        assert claim.to_dict()["batch_id"] == "target"
        with pytest.raises(McpWorkError) as missing_execution:
            claim_next_work(
                principal,
                "batch-worker-2",
                addon_id="doc_processor",
                execution_id="",
                batch_id="target",
            )
        assert missing_execution.value.code == "invalid_execution"
        with pytest.raises(McpWorkError) as invalid_identifier:
            claim_next_work(
                principal,
                "batch-worker-3",
                addon_id="doc_processor",
                execution_id=execution.id,
                batch_id="not valid",
            )
        assert invalid_identifier.value.code == "invalid_batch_id"


def test_batch_projection_is_redacted_and_respects_dependency_and_terminal_rules(app):
    context_hash = "b" * 64
    plan = {
        "schema_version": "carlina.document-batches.v1",
        "scheduling_mode": "sequential",
        "batches": [
            {
                "batch_id": "first",
                "sequence": 0,
                "start_page": 1,
                "end_page": 1,
                "depends_on": [],
            },
            {
                "batch_id": "second",
                "sequence": 1,
                "start_page": 2,
                "end_page": 2,
                "depends_on": ["first"],
            },
        ],
    }
    with app.app_context():
        execution = create_execution(
            1,
            "doc_processor",
            "document",
            "batched-document",
            {"batch_plan": plan, "prompt": "must never be projected"},
            mode="passive",
        )
        first = get_or_create_work(
            execution.id,
            {
                "source": "doc_processor_pdf_evaluation",
                "messages": [{"role": "user", "content": "private prompt"}],
                "dispatch": {
                    "phase": "page_extraction",
                    "batch_id": "first",
                    "unit_index": 1,
                    "context_artifact_hash": context_hash,
                    "boundary_input": {"last_page": 0},
                },
            },
        )
        first_item = db.session.get(McpWorkItem, first.work_item_id)
        assert first_item is not None
        first_item.state = "completed"
        first_item.accepted_response_json = {"content": "secret accepted response"}
        db.session.commit()
        db.session.execute(
            text(
                "UPDATE mcp_work_item SET request_json = :broken, "
                "accepted_response_json = :broken WHERE id = :id"
            ),
            {"broken": "enc:v2:broken", "id": first.work_item_id},
        )
        db.session.commit()
        db.session.expire_all()

        projection = project_execution_batches(execution.id)

        assert projection[0]["state"] == "running"
        assert projection[0]["counts"]["completed"] == 1
        assert projection[0]["context_artifact_hash"] == context_hash
        assert projection[0]["boundary_input"] == {"last_page": 0}
        assert projection[1]["state"] == "blocked"
        assert "messages" not in projection[0]
        assert "prompt" not in projection[0]
        assert "secret accepted response" not in str(projection)

        parser_page = get_or_create_work(
            execution.id,
            {
                "source": "doc_processor_pdf_parser",
                "messages": [{"role": "user", "content": "private parser prompt"}],
                "dispatch": {
                    "phase": "page_extraction",
                    "batch_id": "first",
                    "unit_index": 1,
                    "is_batch_terminal_unit": True,
                    "context_artifact_hash": context_hash,
                },
            },
        )
        parser_item = db.session.get(McpWorkItem, parser_page.work_item_id)
        assert parser_item is not None
        parser_item.state = "completed"
        db.session.commit()

        projection = project_execution_batches(execution.id)

        assert projection[0]["state"] == "completed"
        assert projection[1]["state"] == "planned"

        transition_execution(execution.id, "completed")
        db.session.commit()
        projection = project_execution_batches(execution.id)

        assert [batch["state"] for batch in projection] == [
            "completed",
            "completed",
        ]


@pytest.mark.parametrize("completed_terminal", [False, True])
def test_batch_projection_uses_lease_and_reclaim_precedence(app, completed_terminal):
    plan = {
        "schema_version": "carlina.document-batches.v1",
        "scheduling_mode": "concurrent",
        "batches": [
            {
                "batch_id": "active",
                "sequence": 0,
                "start_page": 1,
                "end_page": 1,
                "depends_on": [],
            },
            {
                "batch_id": "waiting",
                "sequence": 1,
                "start_page": 2,
                "end_page": 2,
                "depends_on": [],
            },
        ],
    }
    with app.app_context():
        execution = create_execution(
            1,
            "doc_processor",
            "document",
            "lease-document",
            {"batch_plan": plan},
            mode="passive",
        )
        get_or_create_work(
            execution.id,
            {
                "source": "doc_processor_pdf_parser",
                "dispatch": {
                    "phase": "page_extraction",
                    "batch_id": "active",
                    "unit_index": 1,
                },
            },
        )
        waiting = get_or_create_work(
            execution.id,
            {
                "source": "doc_processor_pdf_parser",
                "dispatch": {
                    "phase": "page_extraction",
                    "batch_id": "waiting",
                    "unit_index": 2,
                },
            },
        )
        _, principal = _principal(app)
        active_claim = claim_next_work(
            principal,
            "active-worker",
            addon_id="doc_processor",
            execution_id=execution.id,
            batch_id="active",
        )
        assert active_claim is not None
        waiting_item = db.session.get(McpWorkItem, waiting.work_item_id)
        assert waiting_item is not None
        waiting_item.lease_expires_at = datetime(2000, 1, 1, tzinfo=UTC)
        waiting_item.state = "leased"
        waiting_item.lease_owner = "expired-worker"
        waiting_item.lease_token = "expired-token"
        db.session.commit()

        if completed_terminal:
            for batch_id, unit_index in (("active", 1), ("waiting", 2)):
                accepted = get_or_create_work(
                    execution.id,
                    {
                        "source": "doc_processor_pdf_batch",
                        "dispatch": {
                            "phase": "page_extraction",
                            "batch_id": batch_id,
                            "unit_index": unit_index,
                            "is_batch_terminal_unit": True,
                        },
                    },
                )
                accepted_item = db.session.get(McpWorkItem, accepted.work_item_id)
                accepted_item.state = "completed"
            db.session.commit()

        projection = project_execution_batches(execution)

        assert projection[0]["state"] == "leased"
        assert projection[1]["state"] == "waiting_external"
        assert projection[1]["counts"]["reclaimable"] == 1


def test_batch_projection_accepts_durable_completion_for_an_empty_range(app):
    plan = {
        "schema_version": "carlina.document-batches.v1",
        "scheduling_mode": "sequential",
        "batches": [
            {
                "batch_id": "empty-front",
                "sequence": 0,
                "start_page": 1,
                "end_page": 3,
                "depends_on": [],
            },
            {
                "batch_id": "selected-body",
                "sequence": 1,
                "start_page": 4,
                "end_page": 5,
                "depends_on": ["empty-front"],
            },
        ],
    }
    with app.app_context():
        execution = create_execution(
            1,
            "doc_processor",
            "document",
            "sparse-document",
            {"batch_plan": plan},
            mode="passive",
        )
        get_or_create_work(
            execution.id,
            {
                "source": "doc_processor_pdf_parser",
                "dispatch": {
                    "phase": "page_extraction",
                    "batch_id": "selected-body",
                    "unit_index": 4,
                },
            },
        )

        projection = project_execution_batches(
            execution,
            completed_batch_ids={"empty-front"},
        )

        assert projection[0]["state"] == "completed"
        assert projection[0]["counts"]["total"] == 0
        assert projection[1]["state"] == "waiting_external"


def test_batch_dispatch_migration_preserves_legacy_rows_and_is_reversible(tmp_path: Path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'mcp-batch-migration.db'}")
    metadata = sa.MetaData()
    work_item = sa.Table(
        "mcp_work_item",
        metadata,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("execution_id", sa.String(36), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("request_json", sa.Text(), nullable=False),
        sa.Column("accepted_response_json", sa.Text(), nullable=True),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
    )
    metadata.create_all(engine)
    try:
        with engine.begin() as connection:
            connection.execute(
                work_item.insert().values(
                    id="legacy-work",
                    execution_id="legacy-execution",
                    request_fingerprint="f" * 64,
                    request_json="enc:v2:legacy-request",
                    accepted_response_json="enc:v2:legacy-response",
                    state="completed",
                )
            )
            migration = _load_batch_dispatch_migration()
            migration.op = Operations(
                MigrationContext.configure(
                    connection,
                    opts={"render_as_batch": True},
                )
            )

            migration.upgrade()

            inspector = sa.inspect(connection)
            columns = {
                column["name"]: column
                for column in inspector.get_columns("mcp_work_item")
            }
            assert {
                "batch_id",
                "dispatch_phase",
                "unit_index",
                "is_batch_terminal_unit",
                "context_artifact_hash",
                "boundary_input_json",
            } <= columns.keys()
            assert columns["is_batch_terminal_unit"]["nullable"] is False
            indexes = {
                index["name"] for index in inspector.get_indexes("mcp_work_item")
            }
            assert {
                "ix_mcp_work_item_batch_id",
                "ix_mcp_work_item_dispatch_phase",
            } <= indexes
            checks = {
                constraint["name"]
                for constraint in inspector.get_check_constraints("mcp_work_item")
            }
            assert {
                "ck_mcp_work_item_batch_id_length",
                "ck_mcp_work_item_context_artifact_hash",
                "ck_mcp_work_item_dispatch_phase_length",
                "ck_mcp_work_item_terminal_dispatch",
                "ck_mcp_work_item_unit_index_positive",
            } <= checks
            upgraded = connection.execute(
                sa.text(
                    "SELECT request_json, accepted_response_json, batch_id, "
                    "dispatch_phase, unit_index, is_batch_terminal_unit, "
                    "context_artifact_hash, boundary_input_json "
                    "FROM mcp_work_item WHERE id = 'legacy-work'"
                )
            ).one()
            assert upgraded == (
                "enc:v2:legacy-request",
                "enc:v2:legacy-response",
                None,
                None,
                None,
                0,
                None,
                None,
            )

            migration.downgrade()

            downgraded_columns = {
                column["name"]
                for column in sa.inspect(connection).get_columns("mcp_work_item")
            }
            assert not {
                "batch_id",
                "dispatch_phase",
                "unit_index",
                "is_batch_terminal_unit",
                "context_artifact_hash",
                "boundary_input_json",
            } & downgraded_columns
            preserved = connection.execute(
                sa.text(
                    "SELECT request_json, accepted_response_json, state "
                    "FROM mcp_work_item WHERE id = 'legacy-work'"
                )
            ).one()
            assert preserved == (
                "enc:v2:legacy-request",
                "enc:v2:legacy-response",
                "completed",
            )
    finally:
        engine.dispose()


def test_request_retention_migration_adds_and_drops_only_nullable_column(tmp_path: Path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'mcp-request-retention.db'}")
    metadata = sa.MetaData()
    work_item = sa.Table(
        "mcp_work_item",
        metadata,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("execution_id", sa.String(36), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("request_json", sa.Text(), nullable=False),
        sa.Column("accepted_response_json", sa.Text(), nullable=True),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
    )
    metadata.create_all(engine)
    try:
        with engine.begin() as connection:
            connection.execute(
                work_item.insert().values(
                    id="legacy-work",
                    execution_id="legacy-execution",
                    request_fingerprint="f" * 64,
                    request_json="enc:v2:legacy-request",
                    accepted_response_json="enc:v2:legacy-response",
                    state="completed",
                    version=4,
                    attempt_count=2,
                )
            )
            before_rootpage = connection.execute(
                sa.text("SELECT rootpage FROM sqlite_master WHERE name = 'mcp_work_item'")
            ).scalar_one()
            before_row = connection.execute(
                sa.text(
                    "SELECT request_json, accepted_response_json, state, version, attempt_count "
                    "FROM mcp_work_item WHERE id = 'legacy-work'"
                )
            ).one()
            migration = _load_request_retention_migration()
            migration.op = Operations(MigrationContext.configure(connection))
            assert "batch_alter_table" not in inspect.getsource(migration.upgrade)
            assert migration.down_revision == "20260904_mcp_batch_dispatch"

            migration.upgrade()

            columns = {
                column["name"]: column
                for column in sa.inspect(connection).get_columns("mcp_work_item")
            }
            assert columns["request_pruned_at"]["nullable"] is True
            assert connection.execute(
                sa.text("SELECT request_pruned_at FROM mcp_work_item WHERE id = 'legacy-work'")
            ).scalar_one() is None
            assert connection.execute(
                sa.text("SELECT rootpage FROM sqlite_master WHERE name = 'mcp_work_item'")
            ).scalar_one() == before_rootpage
            assert connection.execute(
                sa.text(
                    "SELECT request_json, accepted_response_json, state, version, attempt_count "
                    "FROM mcp_work_item WHERE id = 'legacy-work'"
                )
            ).one() == before_row

            migration.downgrade()

            assert "request_pruned_at" not in {
                column["name"]
                for column in sa.inspect(connection).get_columns("mcp_work_item")
            }
            assert connection.execute(
                sa.text(
                    "SELECT request_json, accepted_response_json, state, version, attempt_count "
                    "FROM mcp_work_item WHERE id = 'legacy-work'"
                )
            ).one() == before_row
    finally:
        engine.dispose()


def test_concurrent_claims_have_one_winner(app):
    with app.app_context():
        execution = _execution()
        get_or_create_work(execution.id, {"prompt": "one"})
        issued, principal = _principal(app)
        execution_id = execution.id
        del issued

    def claim():
        with app.app_context():
            return claim_next_work(
                principal,
                "parallel-worker",
                addon_id="doc_processor",
                execution_id=execution_id,
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: claim(), range(2)))
    assert sum(result is not None for result in results) == 1


def test_submit_validation_duplicate_and_stale_lease(app):
    with app.app_context():
        execution = _execution()
        work = get_or_create_work(execution.id, {"prompt": "one"})
        _, principal = _principal(app)
        claim = claim_next_work(
            principal,
            "worker",
            addon_id="doc_processor",
            execution_id=execution.id,
        )
        assert claim is not None
        accepted = {"content": "done"}
        result = submit_work(principal, work.work_item_id, claim.lease_token, accepted)
        assert result["status"] == "accepted"
        encrypted_request, encrypted_response = db.session.execute(
            text(
                "SELECT request_json, accepted_response_json "
                "FROM mcp_work_item WHERE id = :id"
            ),
            {"id": work.work_item_id},
        ).one()
        assert encrypted_request.startswith("enc:v2:")
        assert encrypted_response.startswith("enc:v2:")
        duplicate = submit_work(principal, work.work_item_id, claim.lease_token, accepted)
        assert duplicate["status"] == "duplicate"
        rejected = submit_work(
            principal,
            work.work_item_id,
            claim.lease_token,
            {"content": "different"},
        )
        assert rejected["status"] == "rejected"
        assert rejected["error_code"] == "already_completed"
        assert McpWorkSubmission.query.filter_by(work_item_id=work.work_item_id).count() == 3
        raw_submission = db.session.execute(
            text(
                "SELECT response_json, error_detail FROM mcp_work_submission "
                "WHERE id = :id"
            ),
            {"id": result["submission_id"]},
        ).one()
        assert raw_submission.response_json.startswith("enc:v2:")


def test_expired_lease_is_fenced_and_can_be_reclaimed(app):
    with app.app_context():
        execution = _execution()
        work = get_or_create_work(execution.id, {"prompt": "one"})
        _, principal = _principal(app)
        first = claim_next_work(
            principal,
            "worker-a",
            addon_id="doc_processor",
            execution_id=execution.id,
        )
        assert first is not None
        db.session.query(McpWorkItem).filter_by(id=work.work_item_id).update(
            {"lease_expires_at": datetime(2000, 1, 1, tzinfo=UTC)}, synchronize_session=False
        )
        db.session.commit()
        with pytest.raises(McpWorkError) as release_error:
            release_work(principal, work.work_item_id, first.lease_token)
        assert release_error.value.code == "stale_lease"
        second = claim_next_work(
            principal,
            "worker-b",
            addon_id="doc_processor",
            execution_id=execution.id,
        )
        assert second is not None
        assert second.lease_token != first.lease_token
        with pytest.raises(McpWorkError) as error:
            renew_work(principal, work.work_item_id, first.lease_token)
        assert error.value.code == "stale_lease"


def test_cancel_execution_fences_pending_and_leased_work(app):
    with app.app_context():
        execution = _execution()
        leased_work = get_or_create_work(execution.id, {"prompt": "leased"})
        pending_work = get_or_create_work(execution.id, {"prompt": "pending"})
        _, principal = _principal(app)
        claim = claim_next_work(
            principal,
            "worker",
            addon_id="doc_processor",
            execution_id=execution.id,
        )
        assert claim is not None
        assert claim.work_item_id == leased_work.work_item_id

        cancelled = cancel_execution(principal, execution.id)

        assert cancelled["status"] == "cancelled"
        assert cancelled["cancelled_work_count"] == 2
        assert db.session.get(McpWorkItem, leased_work.work_item_id).state == "cancelled"
        assert db.session.get(McpWorkItem, pending_work.work_item_id).state == "cancelled"
        rejected = submit_work(
            principal,
            leased_work.work_item_id,
            claim.lease_token,
            {"content": "too late"},
        )
        assert rejected["status"] == "rejected"
        assert rejected["error_code"] == "work_cancelled"
        assert (
            claim_next_work(
                principal,
                "worker-2",
                addon_id="doc_processor",
                execution_id=execution.id,
            )
            is None
        )
        assert cancel_execution(principal, execution.id)["status"] == "cancelled"
        with pytest.raises(McpWorkError) as terminal:
            transition_execution(execution.id, "running")
        assert terminal.value.code == "execution_terminal"
        assert get_execution(execution.id, principal=principal).status == "cancelled"


def test_active_completed_work_redacts_request_and_replays_before_terminal(app):
    with app.app_context():
        execution = _execution()
        packet = {"prompt": "retain until terminal", "private": "synthetic"}
        work = get_or_create_work(execution.id, packet)
        _, principal = _principal(app)
        claim = claim_next_work(
            principal,
            "worker",
            addon_id="doc_processor",
            execution_id=execution.id,
        )
        assert claim is not None
        accepted = submit_work(principal, work.work_item_id, claim.lease_token, {"content": "ok"})

        item = db.session.get(McpWorkItem, work.work_item_id)
        assert item is not None
        assert item.request_json == MCP_REQUEST_TOMBSTONE
        assert item.request_pruned_at is not None

        replay = get_or_create_work(execution.id, packet)
        assert replay.created is False
        assert replay.status == "completed"
        assert replay.response == accepted["response"]

        duplicate = submit_work(principal, work.work_item_id, claim.lease_token, {"content": "ok"})
        assert duplicate["status"] == "duplicate"
        assert duplicate["response"] == accepted["response"]
        assert (
            db.session.get(McpWorkItem, work.work_item_id).request_json
            == MCP_REQUEST_TOMBSTONE
        )


@pytest.mark.parametrize("terminal_status", sorted(("completed", "failed", "cancelled")))
def test_terminal_transition_redacts_request_and_preserves_work_audit(app, terminal_status):
    with app.app_context():
        execution = _execution()
        packet = {
            "source": "doc_processor_pdf_parser",
            "messages": [{"role": "user", "content": "private synthetic request"}],
            "dispatch": {
                "batch_id": "batch-1",
                "phase": "page_extraction",
                "unit_index": 1,
            },
        }
        work = get_or_create_work(execution.id, packet)
        _, principal = _principal(app)
        claim = claim_next_work(
            principal,
            "worker",
            addon_id="doc_processor",
            execution_id=execution.id,
        )
        assert claim is not None
        submit_work(principal, work.work_item_id, claim.lease_token, {"content": "accepted"})
        item = db.session.get(McpWorkItem, work.work_item_id)
        assert item is not None
        fingerprint = item.request_fingerprint
        submission_count = McpWorkSubmission.query.filter_by(work_item_id=item.id).count()

        transition_execution(execution.id, terminal_status)
        db.session.commit()
        db.session.expire_all()

        item = db.session.get(McpWorkItem, work.work_item_id)
        assert item is not None
        assert item.request_json == MCP_REQUEST_TOMBSTONE
        assert item.request_pruned_at is not None
        assert item.accepted_response_json == {"content": "accepted"}
        assert item.request_fingerprint == fingerprint
        assert item.batch_id == "batch-1"
        assert item.dispatch_phase == "page_extraction"
        assert item.unit_index == 1
        assert McpWorkSubmission.query.filter_by(work_item_id=item.id).count() == submission_count
        raw_request, raw_response = db.session.execute(
            text(
                "SELECT request_json, accepted_response_json "
                "FROM mcp_work_item WHERE id = :id"
            ),
            {"id": work.work_item_id},
        ).one()
        assert raw_request.startswith("enc:v2:")
        assert raw_response.startswith("enc:v2:")


def test_terminal_redaction_is_idempotent_and_cancellation_count_stays_fenced_only(app):
    with app.app_context():
        execution = _execution()
        completed = get_or_create_work(execution.id, {"prompt": "completed"})
        leased = get_or_create_work(execution.id, {"prompt": "leased"})
        pending = get_or_create_work(execution.id, {"prompt": "pending"})
        _, principal = _principal(app)
        completed_claim = claim_next_work(
            principal,
            "completed-worker",
            addon_id="doc_processor",
            execution_id=execution.id,
        )
        assert completed_claim is not None
        assert completed_claim.work_item_id == completed.work_item_id
        submit_work(
            principal,
            completed.work_item_id,
            completed_claim.lease_token,
            {"content": "accepted"},
        )
        claim = claim_next_work(
            principal,
            "worker",
            addon_id="doc_processor",
            execution_id=execution.id,
        )
        assert claim is not None
        assert claim.work_item_id == leased.work_item_id
        pending_claim = claim_next_work(
            principal,
            "worker-2",
            addon_id="doc_processor",
            execution_id=execution.id,
        )
        assert pending_claim is not None
        assert pending_claim.work_item_id == pending.work_item_id

        cancelled = cancel_execution(principal, execution.id)
        assert cancelled["cancelled_work_count"] == 2
        db.session.expire_all()
        first_snapshot = {
            item.id: (item.request_json, item.request_pruned_at, item.version)
            for item in McpWorkItem.query.filter_by(execution_id=execution.id).all()
        }
        assert all(snapshot[0] == MCP_REQUEST_TOMBSTONE for snapshot in first_snapshot.values())

        second = cancel_execution(principal, execution.id)
        assert second["cancelled_work_count"] == 0
        db.session.expire_all()
        second_snapshot = {
            item.id: (item.request_json, item.request_pruned_at, item.version)
            for item in McpWorkItem.query.filter_by(execution_id=execution.id).all()
        }
        assert second_snapshot == first_snapshot


def test_historical_terminal_redaction_is_bounded_owner_scoped_and_repeatable(app):
    with app.app_context():
        terminal = _execution()
        terminal_items = [
            get_or_create_work(terminal.id, {"prompt": f"historical-{index}"})
            for index in range(5)
        ]
        terminal.status = "completed"
        terminal.completed_at = datetime.now(UTC)
        other_terminal = create_execution(
            2,
            "doc_processor",
            "document",
            "other-document",
            {"prompt": "other owner"},
            mode="passive",
        )
        other_item = get_or_create_work(other_terminal.id, {"prompt": "other owner work"})
        other_terminal.status = "completed"
        other_terminal.completed_at = datetime.now(UTC)
        active = _execution()
        active_item = get_or_create_work(active.id, {"prompt": "must stay active"})
        db.session.commit()

        preview = redact_terminal_work(owner_user_id=1, batch_size=2, dry_run=True)
        assert preview["rows_eligible"] == 5
        assert preview["rows_redacted"] == 0
        assert preview["batches"] == 3
        assert preview["encrypted_request_bytes_before_redaction"] > 0
        assert db.session.get(McpWorkItem, active_item.work_item_id).request_pruned_at is None

        report = redact_terminal_work(owner_user_id=1, batch_size=2)
        assert report["rows_eligible"] == 5
        assert report["rows_redacted"] == 5
        assert report["batches"] == 3
        assert report["encrypted_request_bytes_before_redaction"] >= preview[
            "encrypted_request_bytes_before_redaction"
        ]
        db.session.expire_all()
        assert all(
            db.session.get(McpWorkItem, item.work_item_id).request_json
            == MCP_REQUEST_TOMBSTONE
            for item in terminal_items
        )
        assert all(
            db.session.get(McpWorkItem, item.work_item_id).state == "cancelled"
            for item in terminal_items
        )
        assert db.session.get(McpWorkItem, active_item.work_item_id).request_json == {
            "prompt": "must stay active"
        }
        assert db.session.get(McpWorkItem, other_item.work_item_id).request_pruned_at is None

        repeat = redact_terminal_work(owner_user_id=1, batch_size=2)
        assert repeat == {
            "rows_eligible": 0,
            "rows_redacted": 0,
            "encrypted_request_bytes_before_redaction": 0,
            "batches": 0,
        }


def test_terminal_execution_rejects_lease_mutations_before_cleanup(app):
    with app.app_context():
        execution = _execution()
        work = get_or_create_work(execution.id, {"prompt": "leased before terminal"})
        _, principal = _principal(app)
        claim = claim_next_work(
            principal,
            "worker",
            addon_id="doc_processor",
            execution_id=execution.id,
        )
        assert claim is not None
        execution.status = "completed"
        execution.completed_at = datetime.now(UTC)
        db.session.commit()

        with pytest.raises(McpWorkError) as renewal:
            renew_work(principal, work.work_item_id, claim.lease_token)
        assert renewal.value.code == "execution_terminal"
        with pytest.raises(McpWorkError) as release:
            release_work(principal, work.work_item_id, claim.lease_token)
        assert release.value.code == "execution_terminal"
        submission = submit_work(
            principal,
            work.work_item_id,
            claim.lease_token,
            {"content": "late"},
        )
        assert submission["status"] == "rejected"
        assert submission["error_code"] == "execution_terminal"

        report = redact_terminal_work(batch_size=1)
        assert report["rows_redacted"] == 1
        db.session.expire_all()
        item = db.session.get(McpWorkItem, work.work_item_id)
        assert item is not None
        assert item.state == "cancelled"
        assert item.lease_token is None
        assert item.lease_owner is None
        assert item.lease_expires_at is None
        assert item.request_json == MCP_REQUEST_TOMBSTONE


def test_work_creation_write_fence_serializes_terminalization(app, monkeypatch):
    with app.app_context():
        execution = _execution()
        execution_id = execution.id

    guard_entered = Event()
    release_guard = Event()
    terminal_started = Event()
    module = sys.modules[get_or_create_work.__module__]
    original_guard = module._fence_work_creation_to_active_execution

    def held_guard(selected_execution_id):
        original_guard(selected_execution_id)
        guard_entered.set()
        assert release_guard.wait(timeout=5)

    monkeypatch.setattr(module, "_fence_work_creation_to_active_execution", held_guard)

    def create_work():
        with app.app_context():
            return get_or_create_work(execution_id, {"prompt": "race-safe"})

    def terminalize():
        terminal_started.set()
        with app.app_context():
            db.session.execute(
                sa.update(McpExecution)
                .where(
                    McpExecution.id == execution_id,
                    McpExecution.status.in_(tuple(MCP_ACTIVE_EXECUTION_STATUSES)),
                )
                .values(status="cancelled", completed_at=datetime.now(UTC))
            )
            db.session.commit()
            transition_execution(execution_id, "cancelled")
            db.session.commit()

    with ThreadPoolExecutor(max_workers=2) as pool:
        creation = pool.submit(create_work)
        assert guard_entered.wait(timeout=5)
        terminal = pool.submit(terminalize)
        assert terminal_started.wait(timeout=5)
        release_guard.set()
        created = creation.result(timeout=10)
        terminal.result(timeout=10)

    with app.app_context():
        item = db.session.get(McpWorkItem, created.work_item_id)
        assert item is not None
        assert item.state == "cancelled"
        assert item.request_json == MCP_REQUEST_TOMBSTONE
        assert item.request_pruned_at is not None


def test_terminal_execution_transition_fences_unfinished_work(app):
    with app.app_context():
        execution = _execution()
        work = get_or_create_work(execution.id, {"prompt": "still pending"})

        transitioned = transition_execution(execution.id, "completed")
        db.session.commit()

        assert transitioned.status == "completed"
        assert db.session.get(McpWorkItem, work.work_item_id).state == "cancelled"


def test_response_contract_and_json_schema_validation(app):
    assert validate_response({"content": "ok", "finish_reason": "stop"}) == {
        "content": "ok",
        "finish_reason": "stop",
    }
    assert validate_response(
        {
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "lookup", "arguments": "{}"},
                }
            ]
        }
    )["tool_calls"][0]["type"] == "function"
    assert validate_response(
        {"content": "ok", "tool_calls": [], "finish_reason": "stop"}
    ) == {"content": "ok", "finish_reason": "stop"}
    assert validate_response(
        {"content": "ok", "tool_calls": None, "finish_reason": "stop"}
    ) == {"content": "ok", "finish_reason": "stop"}
    with pytest.raises(McpWorkError) as empty_error:
        validate_response({"tool_calls": []})
    assert empty_error.value.code == "invalid_response"
    with pytest.raises(McpWorkError) as error:
        validate_response(
            {"content": '{"wrong": "no"}'},
            {
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "schema": {
                            "type": "object",
                            "required": ["answer"],
                            "properties": {"answer": {"type": "string"}},
                        }
                    },
                }
            },
        )
    assert error.value.code == "invalid_json_schema_response"
    with pytest.raises(McpWorkError):
        validate_response({"content": "ok", "extra": True})


def test_response_schema_feedback_is_bounded_and_does_not_echo_completion(app):
    schema = {
        "type": "object",
        "properties": {
            "heading_structure": {
                "type": "object",
                "properties": {
                    "running_matter": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["position"],
                            "properties": {
                                "position": {
                                    "type": "string",
                                    "enum": ["top", "bottom"],
                                }
                            },
                        },
                    }
                },
                "required": ["running_matter"],
            }
        },
        "required": ["heading_structure"],
    }
    request = {
        "response_format": {"type": "json_schema", "json_schema": {"schema": schema}}
    }

    with pytest.raises(McpWorkError) as missing:
        validate_response(
            {"content": '{"heading_structure":{"running_matter":[{}]}}'}, request
        )
    assert missing.value.validation_errors == [
        {
            "path": "/heading_structure/running_matter/0/position",
            "keyword": "required",
            "message": "Missing required property.",
        }
    ]

    with pytest.raises(McpWorkError) as wrong_type:
        validate_response(
            {
                "content": '{"heading_structure":{"running_matter":[{"position":123}]}}'
            },
            request,
        )
    assert wrong_type.value.validation_errors[0]["expected_type"] == "string"
    assert all("123" not in str(error) for error in wrong_type.value.validation_errors)

    with pytest.raises(McpWorkError) as wrong_enum:
        validate_response(
            {
                "content": '{"heading_structure":{"running_matter":[{"position":"secret"}]}}'
            },
            request,
        )
    assert wrong_enum.value.validation_errors[0]["expected_enum"] == ["top", "bottom"]
    assert all("secret" not in str(error) for error in wrong_enum.value.validation_errors)

    with pytest.raises(McpWorkError) as parse_error:
        validate_response({"content": '{"heading_structure":}'}, request)
    parse_diagnostic = parse_error.value.validation_errors[0]
    assert parse_diagnostic["keyword"] == "json_parse"
    assert isinstance(parse_diagnostic["line"], int)
    assert isinstance(parse_diagnostic["column"], int)
    assert "heading_structure" not in parse_diagnostic["message"]

    with pytest.raises(McpWorkError) as wrapper_error:
        validate_response({"content": {"secret": "value"}}, request)
    assert wrapper_error.value.validation_errors[0]["path"] == "/heading_structure"
    assert "secret" not in str(wrapper_error.value.validation_errors)

    with pytest.raises(McpWorkError) as extra_field:
        validate_response({"content": "ok", "secret": "value"})
    assert extra_field.value.validation_errors == [
        {
            "path": "/<key>",
            "keyword": "schema",
            "message": "The value does not satisfy the response schema.",
        }
    ]
    assert "secret" not in str(extra_field.value.validation_errors)

    with pytest.raises(McpWorkError) as missing_content:
        validate_response({"finish_reason": "stop"})
    assert missing_content.value.validation_errors[0]["path"] == "/content"
    assert missing_content.value.validation_errors[0]["keyword"] == "required"

    bounded_schema = {
        "type": "object",
        "required": [f"field-{index}" for index in range(12)],
        "properties": {f"field-{index}": {"type": "string"} for index in range(12)},
    }
    with pytest.raises(McpWorkError) as bounded:
        validate_response(
            {"content": '{"attacker-key":"attacker-value"}'},
            {
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {"schema": bounded_schema},
                }
            },
        )
    assert len(bounded.value.validation_errors) <= 8
    assert all(len(error["path"]) <= 256 for error in bounded.value.validation_errors)
    assert all("attacker-key" not in str(error) for error in bounded.value.validation_errors)
    assert all("attacker-value" not in str(error) for error in bounded.value.validation_errors)


def test_native_json_content_is_canonical_and_schema_validated(app):
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["answer"],
        "properties": {"answer": {"type": "string"}},
    }
    request = {
        "response_format": {"type": "json_schema", "json_schema": {"schema": schema}}
    }

    normalized = validate_response({"content": {"answer": "ok"}}, request)
    assert normalized["content"] == '{"answer":"ok"}'

    integer_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["groups"],
        "properties": {
            "groups": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["source", "target"],
                    "properties": {
                        "source": {"type": "array", "items": {"type": "integer"}},
                        "target": {"type": "array", "items": {"type": "integer"}},
                    },
                },
            }
        },
    }
    integer_request = {
        "response_format": {
            "type": "json_schema",
            "json_schema": {"schema": integer_schema},
        }
    }
    integral_float = validate_response(
        {
            "content": {
                "groups": [{"source": [1.0, 2.0], "target": [1.0]}]
            }
        },
        integer_request,
    )
    assert integral_float["content"] == (
        '{"groups":[{"source":[1,2],"target":[1]}]}'
    )
    assert isinstance(json.loads(integral_float["content"])["groups"][0]["source"][0], int)

    with pytest.raises(McpWorkError) as nested:
        validate_response({"content": {"answer": 123}}, request)
    assert nested.value.code == "invalid_json_schema_response"
    assert nested.value.validation_errors[0]["path"] == "/answer"
    assert "123" not in str(nested.value.validation_errors)

    with pytest.raises(McpWorkError) as native_text:
        validate_response({"content": {"secret": "value"}})
    assert native_text.value.validation_errors[0]["path"] == "/content"
    assert native_text.value.validation_errors[0]["expected_type"] == "string"
    assert "secret" not in str(native_text.value.validation_errors)


def test_json_object_response_parses_safely_and_requires_object_root(app):
    request = {"response_format": {"type": "json_object"}}

    assert validate_response({"content": {"b": 2, "a": 1}}, request) == {
        "content": '{"a":1,"b":2}'
    }
    string_content = ' {"a": 1} \n'
    assert validate_response({"content": string_content}, request)["content"] == string_content

    with pytest.raises(McpWorkError) as syntax:
        validate_response({"content": '{"a":}'}, request)
    assert syntax.value.code == "invalid_json_object_response"
    assert syntax.value.validation_errors[0]["keyword"] == "json_parse"
    assert "{" not in syntax.value.validation_errors[0]["message"]

    for content in ([], "[]", '"{}"'):
        with pytest.raises(McpWorkError) as root:
            validate_response({"content": content}, request)
        assert root.value.code == "invalid_json_object_response"
        assert root.value.validation_errors == [
            {
                "path": "/content",
                "keyword": "type",
                "message": "The value has an invalid type.",
                "expected_type": "object",
            }
        ]


def test_json_object_response_type_feedback_and_tool_call_compatibility(app):
    request = {"response_format": {"type": "json_object"}}

    with pytest.raises(McpWorkError) as invalid_native:
        validate_response({"content": 123}, request)
    assert invalid_native.value.code == "invalid_response"
    assert invalid_native.value.validation_errors[0]["path"] == "/content"
    assert invalid_native.value.validation_errors[0]["expected_type"] == [
        "object",
        "string",
    ]

    result = validate_response(
        {
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "lookup", "arguments": "{}"},
                }
            ]
        },
        request,
    )
    assert result["tool_calls"][0]["function"]["name"] == "lookup"


def test_json_schema_strings_are_not_double_decoded_and_native_content_is_bounded(app):
    request = {
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "schema": {
                    "type": "object",
                    "required": ["answer"],
                    "properties": {"answer": {"type": "string"}},
                }
            },
        }
    }
    with pytest.raises(McpWorkError) as double_encoded:
        validate_response({"content": '"{\\"answer\\":\\"ok\\"}"'}, request)
    assert double_encoded.value.code == "invalid_json_schema_response"
    assert double_encoded.value.validation_errors[0]["expected_type"] == "object"

    module = sys.modules[validate_response.__module__]
    too_large = "x" * (module.MAX_CONTENT_BYTES + 1)
    with pytest.raises(McpWorkError) as oversized:
        validate_response({"content": {"answer": too_large}}, request)
    assert oversized.value.code == "invalid_response"
    assert oversized.value.validation_errors[0]["path"] == "/content"


def test_native_schema_rejection_keeps_same_lease_usable(app):
    schema = {
        "type": "object",
        "required": ["result"],
        "properties": {
            "result": {
                "type": "object",
                "required": ["answer"],
                "properties": {"answer": {"type": "string"}},
            }
        },
    }
    with app.app_context():
        execution = _execution()
        work = get_or_create_work(
            execution.id,
            {
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {"schema": schema},
                }
            },
        )
        _, principal = _principal(app)
        claim = claim_next_work(principal, "worker", execution_id=execution.id)
        assert claim is not None

        rejected = submit_work(
            principal,
            work.work_item_id,
            claim.lease_token,
            {"content": {"result": {"answer": 123}}},
        )
        assert rejected["status"] == "rejected"
        assert rejected["error_code"] == "invalid_json_schema_response"
        assert rejected["lease_active"] is True
        assert rejected["validation_errors"][0]["path"] == "/result/answer"
        assert "123" not in str(rejected["validation_errors"])

        accepted = submit_work(
            principal,
            work.work_item_id,
            claim.lease_token,
            {"content": {"result": {"answer": "ok"}}},
        )
        assert accepted["status"] == "accepted"
        assert accepted["validation_stage"] == "response_contract"
        assert accepted["product_validation"] == "not_checked"
        assert accepted["response"]["content"] == '{"result":{"answer":"ok"}}'


def test_invalid_native_content_rejects_with_feedback_and_journals_safely(app):
    request = {
        "response_format": {
            "type": "json_schema",
            "json_schema": {"schema": {"type": "object"}},
        }
    }
    with app.app_context():
        execution = _execution()
        work = get_or_create_work(execution.id, request)
        _, principal = _principal(app)
        claim = claim_next_work(principal, "worker", execution_id=execution.id)
        assert claim is not None

        rejected = submit_work(
            principal,
            work.work_item_id,
            claim.lease_token,
            {"content": {"bad": {1, 2}}},
        )
        assert rejected["status"] == "rejected"
        assert rejected["error_code"] == "invalid_response"
        assert rejected["lease_active"] is True
        assert rejected["validation_errors"][0]["path"] == "/content"

        submission = McpWorkSubmission.query.filter_by(
            work_item_id=work.work_item_id
        ).one()
        assert submission.status == "rejected"
        assert set(submission.response_json) == {"invalid_response"}
        assert len(submission.response_fingerprint) == 64

        accepted = submit_work(
            principal,
            work.work_item_id,
            claim.lease_token,
            {"content": {"ok": True}},
        )
        assert accepted["status"] == "accepted"


def test_native_duplicate_replay_after_tombstone_and_dry_run_has_no_dry_write(app):
    request = {
        "response_format": {
            "type": "json_schema",
            "json_schema": {"schema": {"type": "object"}},
        }
    }
    with app.app_context():
        execution = _execution()
        work = get_or_create_work(execution.id, request)
        _, principal = _principal(app)
        claim = claim_next_work(principal, "worker", execution_id=execution.id)
        assert claim is not None
        accepted = submit_work(
            principal,
            work.work_item_id,
            claim.lease_token,
            {"content": {"b": 2, "a": 1}},
        )
        assert accepted["status"] == "accepted"
        assert accepted["response"]["content"] == '{"a":1,"b":2}'
        accepted_submission = McpWorkSubmission.query.filter_by(
            work_item_id=work.work_item_id, status="accepted"
        ).one()
        assert accepted_submission.response_json == accepted["response"]
        item = db.session.get(McpWorkItem, work.work_item_id)
        assert item is not None and item.request_json == MCP_REQUEST_TOMBSTONE
        count = McpWorkSubmission.query.filter_by(work_item_id=item.id).count()

        dry_duplicate = submit_work(
            principal,
            work.work_item_id,
            claim.lease_token,
            {"content": {"a": 1, "b": 2}},
            dry_run=True,
        )
        assert dry_duplicate == {
            "work_item_id": work.work_item_id,
            "status": "duplicate",
            "validation_stage": "response_contract",
            "product_validation": "not_checked",
            "receipt_state": "schema_validated",
            "product_commit_confirmed": False,
            "dry_run": True,
        }
        db.session.expire_all()
        item = db.session.get(McpWorkItem, work.work_item_id)
        assert item is not None and item.request_json == MCP_REQUEST_TOMBSTONE
        assert McpWorkSubmission.query.filter_by(work_item_id=item.id).count() == count

        duplicate = submit_work(
            principal,
            work.work_item_id,
            claim.lease_token,
            {"content": {"a": 1, "b": 2}},
        )
        assert duplicate["status"] == "duplicate"
        assert duplicate["validation_stage"] == "response_contract"
        assert duplicate["product_validation"] == "not_checked"
        assert duplicate["response"] == accepted["response"]
        duplicate_submission = (
            McpWorkSubmission.query.filter_by(
                work_item_id=item.id, status="duplicate"
            )
            .order_by(McpWorkSubmission.submitted_at.desc())
            .first()
        )
        assert duplicate_submission is not None
        assert duplicate_submission.response_json == accepted["response"]
        assert (
            duplicate_submission.response_fingerprint
            == accepted_submission.response_fingerprint
        )
        assert McpWorkSubmission.query.filter_by(work_item_id=item.id).count() == count + 1


def test_submit_work_dry_run_is_no_write_and_can_correct_same_lease(app):
    schema = {
        "type": "object",
        "required": ["answer"],
        "properties": {"answer": {"type": "string"}},
    }
    with app.app_context():
        execution = _execution()
        work = get_or_create_work(
            execution.id,
            {
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {"schema": schema},
                }
            },
        )
        _, principal = _principal(app)
        claim = claim_next_work(
            principal,
            "worker",
            addon_id="doc_processor",
            execution_id=execution.id,
        )
        assert claim is not None
        item = db.session.get(McpWorkItem, work.work_item_id)
        assert item is not None
        before = {
            "state": item.state,
            "version": item.version,
            "request": item.request_json,
            "lease_token": item.lease_token,
            "lease_expires_at": item.lease_expires_at,
            "submissions": McpWorkSubmission.query.filter_by(
                work_item_id=item.id
            ).count(),
        }

        valid = submit_work(
            principal,
            work.work_item_id,
            claim.lease_token,
            {"content": '{"answer":"ok"}'},
            dry_run=True,
        )
        assert valid == {
            "work_item_id": work.work_item_id,
            "status": "valid",
            "validation_stage": "response_contract",
            "product_validation": "not_checked",
            "receipt_state": "schema_validated",
            "product_commit_confirmed": False,
            "dry_run": True,
        }
        invalid = submit_work(
            principal,
            work.work_item_id,
            claim.lease_token,
            {"content": "{}"},
            dry_run=True,
        )
        assert invalid["status"] == "rejected"
        assert invalid["dry_run"] is True
        assert "response" not in invalid
        assert invalid["validation_errors"][0]["path"] == "/answer"
        assert invalid["lease_active"] is True
        assert invalid["lease_expires_at"]

        db.session.expire_all()
        item = db.session.get(McpWorkItem, work.work_item_id)
        assert item is not None
        assert {
            "state": item.state,
            "version": item.version,
            "request": item.request_json,
            "lease_token": item.lease_token,
            "lease_expires_at": item.lease_expires_at,
            "submissions": McpWorkSubmission.query.filter_by(
                work_item_id=item.id
            ).count(),
        } == before

        accepted = submit_work(
            principal,
            work.work_item_id,
            claim.lease_token,
            {"content": '{"answer":"ok"}'},
        )
        assert accepted["status"] == "accepted"


def test_submit_work_dry_run_duplicate_has_no_journal_or_response(app):
    with app.app_context():
        execution = _execution()
        work = get_or_create_work(execution.id, {"prompt": "one"})
        _, principal = _principal(app)
        claim = claim_next_work(
            principal,
            "worker",
            addon_id="doc_processor",
            execution_id=execution.id,
        )
        assert claim is not None
        accepted = submit_work(
            principal, work.work_item_id, claim.lease_token, {"content": "done"}
        )
        count = McpWorkSubmission.query.filter_by(work_item_id=work.work_item_id).count()

        duplicate = submit_work(
            principal,
            work.work_item_id,
            claim.lease_token,
            {"content": "done"},
            dry_run=True,
        )
        assert duplicate == {
            "work_item_id": work.work_item_id,
            "status": "duplicate",
            "validation_stage": "response_contract",
            "product_validation": "not_checked",
            "receipt_state": "schema_validated",
            "product_commit_confirmed": False,
            "dry_run": True,
        }
        assert McpWorkSubmission.query.filter_by(work_item_id=work.work_item_id).count() == count
        assert "response" not in duplicate
        assert accepted["response"] == {"content": "done"}


@pytest.mark.parametrize("dry_run", [False, True])
def test_submission_feedback_does_not_advertise_another_workers_lease(app, dry_run):
    with app.app_context():
        execution = _execution()
        work = get_or_create_work(execution.id, {"prompt": "one"})
        _, principal = _principal(app)
        old = claim_next_work(principal, "first", execution_id=execution.id)
        release_work(principal, work.work_item_id, old.lease_token)
        current = claim_next_work(principal, "second", execution_id=execution.id)
        rejected = submit_work(
            principal, work.work_item_id, old.lease_token, {"content": "late"}, dry_run=dry_run,
        )
        assert rejected["error_code"] == "stale_lease"
        assert rejected["lease_active"] is False
        assert "lease_expires_at" not in rejected
        assert db.session.get(McpWorkItem, work.work_item_id).lease_token == current.lease_token
        assert McpWorkSubmission.query.count() == (0 if dry_run else 1)


@pytest.mark.parametrize("dry_run", [False, True])
def test_lease_expiring_during_validation_cannot_accept_output(app, monkeypatch, dry_run):
    with app.app_context():
        execution = _execution()
        work = get_or_create_work(execution.id, {"prompt": "one"})
        _, principal = _principal(app)
        claim = claim_next_work(principal, "worker", execution_id=execution.id)
        module = sys.modules[submit_work.__module__]
        original = module.validate_response

        def validate_then_expire(*args):
            result = original(*args)
            monkeypatch.setattr(module, "utc_now_db", lambda: claim.lease_expires_at + timedelta(seconds=1))
            return result

        monkeypatch.setattr(module, "validate_response", validate_then_expire)
        rejected = submit_work(principal, work.work_item_id, claim.lease_token, {"content": "late"}, dry_run=dry_run)
        assert rejected["error_code"] == "stale_lease"
        assert rejected["lease_active"] is False
        item = db.session.get(McpWorkItem, work.work_item_id)
        assert item.state == "leased"
        assert item.accepted_response_json is None
        assert item.request_json == {"prompt": "one"}


@pytest.mark.parametrize("invalid", [False, True])
def test_dry_run_rechecks_cancellation_from_an_independent_context(app, monkeypatch, invalid):
    with app.app_context():
        execution = _execution()
        execution_id = execution.id
        work = get_or_create_work(execution_id, {"prompt": "one"})
        work_id = work.work_item_id
        _, principal = _principal(app)
        claim = claim_next_work(principal, "worker", execution_id=execution_id)
        token = claim.lease_token
    entered = Event()
    proceed = Event()
    module = sys.modules[submit_work.__module__]
    original = module.validate_response

    def blocked_validation(*args):
        entered.set()
        assert proceed.wait(5)
        if invalid:
            raise McpWorkError("invalid_response", "Synthetic invalid completion.")
        return original(*args)

    monkeypatch.setattr(module, "validate_response", blocked_validation)

    def preview():
        with app.app_context():
            return submit_work(principal, work_id, token, {"content": "draft"}, dry_run=True)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(preview)
        try:
            assert entered.wait(5)
            with app.app_context():
                transition_execution(execution_id, "cancelled")
                db.session.commit()
        finally:
            proceed.set()
        result = future.result(timeout=5)
    assert result["status"] == "rejected"
    assert result["lease_active"] is False
    with app.app_context():
        assert McpWorkSubmission.query.count() == 0
        assert db.session.get(McpExecution, execution_id).status == "cancelled"


@pytest.mark.parametrize("operation", ["claim", "renew", "release", "submit"])
def test_work_write_operations_replay_after_real_sqlite_writer_contention(
    app,
    monkeypatch,
    operation,
):
    """Each DB-only work API rebuilds its write after a transient file lock."""

    with app.app_context():
        execution = _execution()
        execution_id = str(execution.id)
        work = get_or_create_work(
            execution_id,
            {"packet": "contention", "operation": operation},
        )
        _, principal = _principal(app)
        claim = None
        if operation != "claim":
            claim = claim_next_work(
                principal,
                f"{operation}-setup-worker",
                addon_id="doc_processor",
                execution_id=execution_id,
            )
            assert claim is not None
        item_id = work.work_item_id
        initial_version = db.session.get(McpWorkItem, item_id).version
        _set_sqlite_busy_timeout()

        with _held_sqlite_writer(_database_path()) as (release, finished):
            sleeps = _release_lock_on_first_retry(
                monkeypatch,
                release,
                finished,
            )
            if operation == "claim":
                result = claim_next_work(
                    principal,
                    "contention-worker",
                    addon_id="doc_processor",
                    execution_id=execution_id,
                )
            elif operation == "renew":
                assert claim is not None
                result = renew_work(
                    principal,
                    item_id,
                    claim.lease_token,
                    lease_seconds=60,
                )
            elif operation == "release":
                assert claim is not None
                result = release_work(
                    principal,
                    item_id,
                    claim.lease_token,
                    reason="contention-test",
                )
            else:
                assert claim is not None
                result = submit_work(
                    principal,
                    item_id,
                    claim.lease_token,
                    {"content": "contention accepted"},
                )

        assert sleeps, "the operation did not observe the transient lock"
        db.session.expire_all()
        item = db.session.get(McpWorkItem, item_id)
        assert item is not None
        assert item.execution_id == execution_id
        assert item.version == initial_version + 1

        if operation == "claim":
            assert result is not None
            assert result.work_item_id == item_id
            assert result.execution_id == execution_id
            assert result.state == "leased"
            assert result.version == 1
            assert item.state == "leased"
            assert item.lease_owner == "contention-worker"
            assert item.attempt_count == 1
        elif operation == "renew":
            assert result["work_item_id"] == item_id
            assert result["state"] == "leased"
            assert result["version"] == initial_version + 1
            assert item.state == "leased"
            assert item.lease_owner == claim.lease_owner
            assert item.lease_token == claim.lease_token
        elif operation == "release":
            assert result == {
                "work_item_id": item_id,
                "execution_id": execution_id,
                "version": initial_version + 1,
                "state": "pending",
                "released": True,
                "reason": "contention-test",
            }
            assert item.state == "pending"
            assert item.lease_token is None
            assert item.lease_owner is None
        else:
            assert result["status"] == "accepted"
            assert result["work_item_id"] == item_id
            assert item.state == "completed"
            assert item.accepted_response_json == {"content": "contention accepted"}
            accepted = McpWorkSubmission.query.filter_by(
                work_item_id=item_id,
                status="accepted",
            ).all()
            assert len(accepted) == 1


@pytest.mark.parametrize("write_kind", ["sql", "orm"])
def test_claim_preserves_active_caller_transaction_and_skips_retry(
    app,
    monkeypatch,
    write_kind,
):
    """Caller-owned Core/SQL writes commit with the claim, without replay."""

    with app.app_context():
        execution = _execution()
        execution_id = str(execution.id)
        work = get_or_create_work(execution_id, {"packet": "caller-owned"})
        work_id = work.work_item_id
        _, principal = _principal(app)
        db.session.rollback()

        if write_kind == "sql":
            db.session.execute(
                text(
                    "UPDATE mcp_execution "
                    "SET queue_job_id = :queue_job_id "
                    "WHERE id = :execution_id"
                ),
                {"queue_job_id": "caller-job", "execution_id": execution_id},
            )
        else:
            caller_execution = db.session.get(McpExecution, execution_id)
            assert caller_execution is not None
            caller_execution.queue_job_id = "caller-job"
            db.session.flush()

        module = sys.modules[claim_next_work.__module__]
        retry_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

        def unexpected_retry(*args, **kwargs):
            retry_calls.append((args, kwargs))
            raise AssertionError("caller-owned transaction must not be replayed")

        monkeypatch.setattr(module, "run_transaction_with_retry", unexpected_retry)
        claim = claim_next_work(
            principal,
            "caller-transaction-worker",
            addon_id="doc_processor",
            execution_id=execution_id,
        )

        assert claim is not None
        assert claim.work_item_id == work_id
        assert claim.execution_id == execution_id
        assert claim.queue_job_id == "caller-job"
        assert retry_calls == []

        db.session.expire_all()
        committed_execution = db.session.get(McpExecution, execution_id)
        committed_work = db.session.get(McpWorkItem, work_id)
        assert committed_execution is not None
        assert committed_execution.queue_job_id == "caller-job"
        assert committed_work is not None
        assert committed_work.state == "leased"
        assert committed_work.version == 1


def test_claim_projects_large_packet_before_acquiring_writer_lock(app, monkeypatch):
    with app.app_context():
        execution = _execution()
        execution_id = str(execution.id)
        work = get_or_create_work(
            execution_id,
            {"prompt": "P" * (1024 * 1024)},
        )
        work_id = work.work_item_id
        _, principal = _principal(app)
        db.session.rollback()

        module = sys.modules[claim_next_work.__module__]
        original_claim_payload = module._claim_payload
        probe_calls = 0

        def probe_before_projection(work_record, execution_record, **kwargs):
            nonlocal probe_calls
            probe_calls += 1
            assert {"worker_id", "lease_token", "lease_seconds"} <= set(kwargs)
            probe = sqlite3.connect(
                _database_path(),
                timeout=0.025,
                check_same_thread=False,
            )
            try:
                probe.execute("BEGIN IMMEDIATE")
                probe.rollback()
            finally:
                probe.close()
            return original_claim_payload(
                work_record,
                execution_record,
                **kwargs,
            )

        monkeypatch.setattr(module, "_claim_payload", probe_before_projection)
        claim = claim_next_work(
            principal,
            "large-packet-worker",
            addon_id="doc_processor",
            execution_id=execution_id,
        )

        assert probe_calls == 1
        assert claim is not None
        assert claim.work_item_id == work_id
        assert claim.execution_id == execution_id
        assert len(claim.request["prompt"]) == 1024 * 1024
        item = db.session.get(McpWorkItem, work_id)
        assert item is not None
        assert item.state == "leased"
        assert item.version == 1


def test_renew_invalid_token_preserves_active_caller_transaction(app):
    with app.app_context():
        execution = _execution()
        execution_id = str(execution.id)
        work = get_or_create_work(execution_id, {"packet": "caller-validation"})
        work_id = work.work_item_id
        _, principal = _principal(app)
        claim = claim_next_work(
            principal,
            "caller-validation-worker",
            addon_id="doc_processor",
            execution_id=execution_id,
        )
        assert claim is not None
        lease_token = claim.lease_token
        db.session.rollback()

        db.session.execute(
            text(
                "UPDATE mcp_execution "
                "SET queue_job_id = :queue_job_id "
                "WHERE id = :execution_id"
            ),
            {"queue_job_id": "caller-job", "execution_id": execution_id},
        )

        with pytest.raises(McpWorkError) as error:
            renew_work(principal, work_id, "not-a-lease-token")

        assert error.value.code == "invalid_lease_token"
        assert db.session().in_transaction()
        assert db.session.execute(
            text(
                "SELECT queue_job_id FROM mcp_execution "
                "WHERE id = :execution_id"
            ),
            {"execution_id": execution_id},
        ).scalar_one() == "caller-job"
        state, version, persisted_token = db.session.execute(
            text(
                "SELECT state, version, lease_token FROM mcp_work_item "
                "WHERE id = :work_id"
            ),
            {"work_id": work_id},
        ).one()
        assert (state, version, persisted_token) == ("leased", 1, lease_token)

        db.session.commit()
        committed_execution = db.session.get(McpExecution, execution_id)
        committed_work = db.session.get(McpWorkItem, work_id)
        assert committed_execution is not None
        assert committed_execution.queue_job_id == "caller-job"
        assert committed_work is not None
        assert committed_work.state == "leased"
        assert committed_work.version == 1
        assert committed_work.lease_token == lease_token


def test_submit_lock_exhaustion_is_safe_and_session_is_reusable(app, monkeypatch):
    with app.app_context():
        execution = _execution()
        execution_id = str(execution.id)
        work = get_or_create_work(execution_id, {"packet": "exhaustion"})
        _, principal = _principal(app)
        claim = claim_next_work(
            principal,
            "exhaustion-worker",
            addon_id="doc_processor",
            execution_id=execution_id,
        )
        assert claim is not None
        _set_sqlite_busy_timeout()
        monkeypatch.setenv("CARLINA_DB_WRITE_RETRY_SECONDS", "0.1")

        with _held_sqlite_writer(_database_path()), pytest.raises(McpWorkError) as error:
            submit_work(
                principal,
                work.work_item_id,
                claim.lease_token,
                {"content": "retry-after-lock"},
            )

        assert error.value.code == "database_busy"
        assert error.value.status_code == 503
        assert error.value.retryable is True
        assert error.value.detail == (
            "The database is temporarily busy. Please retry the operation in a moment."
        )
        assert "retry-after-lock" not in error.value.detail
        assert "UPDATE" not in error.value.detail
        assert "INSERT" not in error.value.detail

        db.session.expire_all()
        item = db.session.get(McpWorkItem, work.work_item_id)
        assert item is not None
        assert item.state == "leased"
        assert item.accepted_response_json is None
        assert McpWorkSubmission.query.filter_by(
            work_item_id=item.id,
            status="accepted",
        ).count() == 0

        accepted = submit_work(
            principal,
            work.work_item_id,
            claim.lease_token,
            {"content": "retry-after-lock"},
        )
        assert accepted["status"] == "accepted"
        assert McpWorkSubmission.query.filter_by(
            work_item_id=item.id,
            status="accepted",
        ).count() == 1


def test_submit_contention_reloads_lease_and_rejects_independent_expiry(
    app,
    monkeypatch,
):
    with app.app_context():
        execution = _execution()
        execution_id = str(execution.id)
        work = get_or_create_work(execution_id, {"packet": "stale-after-replay"})
        _, principal = _principal(app)
        claim = claim_next_work(
            principal,
            "stale-replay-worker",
            addon_id="doc_processor",
            execution_id=execution_id,
        )
        assert claim is not None
        item_id = work.work_item_id
        _set_sqlite_busy_timeout()

    mutate = Event()
    mutated = Event()
    writer_errors: list[Exception] = []

    def expire_lease() -> None:
        try:
            assert mutate.wait(timeout=5), "stale-lease writer was not released"
            with app.app_context():
                _set_sqlite_busy_timeout()
                db.session.execute(
                    sa.update(McpWorkItem)
                    .where(McpWorkItem.id == item_id)
                    .values(
                        lease_expires_at=datetime.now(UTC) - timedelta(seconds=1),
                        updated_at=datetime.now(UTC),
                        version=McpWorkItem.version + 1,
                    )
                )
                db.session.commit()
        except (AssertionError, RuntimeError, sa.exc.SQLAlchemyError) as error:
            writer_errors.append(error)
        finally:
            mutated.set()

    writer = Thread(target=expire_lease, name="mcp-stale-lease-writer")
    writer.start()

    def release_and_expire() -> None:
        mutate.set()
        assert mutated.wait(timeout=5), "stale-lease writer did not commit"

    try:
        with app.app_context(), _held_sqlite_writer(_database_path()) as (
            release,
            finished,
        ):
            _set_sqlite_busy_timeout()
            sleeps = _release_lock_on_first_retry(
                monkeypatch,
                release,
                finished,
                after_release=release_and_expire,
            )
            result = submit_work(
                principal,
                item_id,
                claim.lease_token,
                {"content": "must-be-rejected"},
            )
        assert sleeps
    finally:
        mutate.set()
        assert mutated.wait(timeout=5)
        writer.join(timeout=5)
    if writer_errors:
        raise writer_errors[0]

    assert result["status"] == "rejected"
    assert result["error_code"] == "stale_lease"
    with app.app_context():
        item = db.session.get(McpWorkItem, item_id)
        assert item is not None
        assert item.execution_id == execution_id
        assert item.state == "leased"
        assert item.accepted_response_json is None
        assert McpWorkSubmission.query.filter_by(
            work_item_id=item_id,
            status="accepted",
        ).count() == 0
        assert McpWorkSubmission.query.filter_by(
            work_item_id=item_id,
            status="rejected",
        ).count() == 1


def test_five_file_backed_workers_preserve_execution_and_packet_identity(app):
    worker_count = 5
    with app.app_context():
        db.session.execute(text("PRAGMA journal_mode=WAL"))
        db.session.commit()
        assert db.session.execute(text("PRAGMA journal_mode")).scalar_one().lower() == "wal"
        executions: list[tuple[str, str, str]] = []
        principals: list[McpPrincipal] = []
        for index in range(worker_count):
            resource_id = f"document-resource-{index}"
            execution = create_execution(
                1,
                "doc_processor",
                "document",
                resource_id,
                {"resource_id": resource_id},
                mode="passive",
            )
            execution_id = str(execution.id)
            work = get_or_create_work(
                execution_id,
                {
                    "packet": f"packet-{index}",
                    "resource_id": resource_id,
                },
            )
            _, principal = _principal(app)
            executions.append((execution_id, work.work_item_id, resource_id))
            principals.append(principal)

    claim_barrier = Barrier(worker_count)
    renew_barrier = Barrier(worker_count)
    submit_barrier = Barrier(worker_count)
    barriers = (claim_barrier, renew_barrier, submit_barrier)

    def worker(index: int):
        execution_id, work_id, resource_id = executions[index]
        principal = principals[index]
        try:
            with app.app_context():
                _set_sqlite_busy_timeout(250)
                claim = claim_next_work(
                    principal,
                    f"identity-worker-{index}",
                    addon_id="doc_processor",
                    execution_id=execution_id,
                )
            assert claim is not None
            assert claim.execution_id == execution_id
            assert claim.resource_id == resource_id
            assert claim.request == {
                "packet": f"packet-{index}",
                "resource_id": resource_id,
            }
            claim_barrier.wait(timeout=15)

            with app.app_context():
                _set_sqlite_busy_timeout(250)
                renewal = renew_work(
                    principal,
                    work_id,
                    claim.lease_token,
                    lease_seconds=60,
                )
            assert renewal["work_item_id"] == work_id
            assert renewal["execution_id"] == execution_id
            renew_barrier.wait(timeout=15)

            with app.app_context():
                _set_sqlite_busy_timeout(250)
                submission = submit_work(
                    principal,
                    work_id,
                    claim.lease_token,
                    {"content": f"completion-{index}"},
                )
            assert submission["status"] == "accepted"
            assert submission["work_item_id"] == work_id
            submit_barrier.wait(timeout=15)
            return index, submission
        except BaseException:
            for barrier in barriers:
                barrier.abort()
            raise

    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        results = [pool.submit(worker, index) for index in range(worker_count)]
        completed = [future.result(timeout=45) for future in results]

    assert {index for index, _ in completed} == set(range(worker_count))
    with app.app_context():
        for index, (execution_id, work_id, resource_id) in enumerate(executions):
            item = db.session.get(McpWorkItem, work_id)
            assert item is not None
            assert item.execution_id == execution_id
            assert item.state == "completed"
            assert item.version == 3
            assert item.attempt_count == 1
            assert item.accepted_response_json == {
                "content": f"completion-{index}"
            }
            assert McpWorkSubmission.query.filter_by(
                work_item_id=work_id,
                status="accepted",
            ).count() == 1
            execution = db.session.get(McpExecution, execution_id)
            assert execution is not None
            assert execution.resource_id == resource_id


def test_retryable_work_write_refuses_unrelated_pending_mutation(app):
    with app.app_context():
        execution = _execution()
        work = get_or_create_work(execution.id, {"packet": "dirty-write"})
        _, principal = _principal(app)
        claim = claim_next_work(
            principal,
            "dirty-write-worker",
            addon_id="doc_processor",
            execution_id=execution.id,
        )
        assert claim is not None
        pending = User(email="uncommitted-write@example.test", password_hash="x")
        db.session.add(pending)
        with pytest.raises(TransactionScopeError):
            renew_work(principal, work.work_item_id, claim.lease_token)
        assert pending in db.session.new
        db.session.rollback()
        assert User.query.filter_by(email=pending.email).count() == 0


def test_submit_dry_run_preserves_unrelated_pending_mutation(app):
    with app.app_context():
        execution = _execution()
        work = get_or_create_work(execution.id, {"packet": "dry-run-dirty"})
        _, principal = _principal(app)
        claim = claim_next_work(
            principal,
            "dry-run-dirty-worker",
            addon_id="doc_processor",
            execution_id=execution.id,
        )
        assert claim is not None
        pending = User(email="dry-run-uncommitted@example.test", password_hash="x")
        db.session.add(pending)
        result = submit_work(
            principal,
            work.work_item_id,
            claim.lease_token,
            {"content": "preview"},
            dry_run=True,
        )
        assert result["status"] == "valid"
        assert pending in db.session.new
        db.session.rollback()
        assert User.query.filter_by(email=pending.email).count() == 0
        item = db.session.get(McpWorkItem, work.work_item_id)
        assert item is not None
        assert item.state == "leased"
        assert McpWorkSubmission.query.filter_by(work_item_id=item.id).count() == 0


def test_transition_execution_supports_review_state(app):
    with app.app_context():
        execution = _execution()
        transition_execution(execution, "waiting_review")
        db.session.commit()
        assert execution.status == "waiting_review"


def test_same_lease_read_preserves_attempt_version_expiry_and_owner(app):
    from iarf_assistant.core.mcp_jobs import get_leased_work

    with app.app_context():
        execution = _execution()
        packet = {"messages": [{"role": "user", "content": "Synthetic request."}]}
        work = get_or_create_work(execution.id, packet)
        _, principal = _principal(app)
        claim = claim_next_work(principal, "reader", execution_id=execution.id)
        before = claim.to_dict()
        read = get_leased_work(principal, work.work_item_id, claim.lease_token).to_dict()
        assert read == before
        assert get_leased_work(principal, work.work_item_id, claim.lease_token).to_dict() == before
        other_key = issue_access_key(2, "other", ["mcp.work", "mcp.doc_processor"], [])
        other = resolve_access_key(other_key.raw_token)
        with pytest.raises(McpWorkError, match="not found"):
            get_leased_work(other, work.work_item_id, claim.lease_token)
        release_work(principal, work.work_item_id, claim.lease_token)
        with pytest.raises(McpWorkError, match="stale"):
            get_leased_work(principal, work.work_item_id, claim.lease_token)
        fresh = claim_next_work(principal, "reader", execution_id=execution.id, lease_seconds=30)
        record = db.session.get(McpWorkItem, work.work_item_id)
        record.lease_expires_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=1)
        db.session.commit()
        with pytest.raises(McpWorkError, match="stale"):
            get_leased_work(principal, work.work_item_id, fresh.lease_token)


@pytest.mark.parametrize("output_tokens,explicit,expected", [(4096, None, 1800), (24000, None, 3300), (64000, None, 3600), (64000, 300, 300)])
def test_translation_packet_lease_defaults_and_explicit_override(app, output_tokens, explicit, expected):
    issued = issue_access_key(1, "translator-worker", ["mcp.work", "mcp.translator"], [])
    principal = resolve_access_key(issued.raw_token)
    execution = create_execution(1, "translator", "project", "project-1", {}, mode="passive")
    work = get_or_create_work(execution.id, {"parameters": {"max_tokens": output_tokens}})
    claim = claim_next_work(principal, "worker", execution_id=execution.id, lease_seconds=explicit)
    item = db.session.get(McpWorkItem, work.work_item_id)
    assert (claim.lease_expires_at - item.leased_at).total_seconds() == expected
    old_deadline = item.lease_expires_at
    renew_work(principal, item.id, claim.lease_token)
    db.session.expire_all()
    assert db.session.get(McpWorkItem, item.id).lease_expires_at >= old_deadline


def test_translation_lease_defaults_reject_invalid_explicit_values(app):
    issued = issue_access_key(1, "translator-worker", ["mcp.work", "mcp.translator"], [])
    principal = resolve_access_key(issued.raw_token)
    execution = create_execution(1, "translator", "project", "project-1", {}, mode="passive")
    for value in (True, 29, 3601, "3600"):
        with pytest.raises(McpWorkError):
            claim_next_work(principal, "worker", execution_id=execution.id, lease_seconds=value)



def test_queue_execution_receipts_are_race_safe_and_owner_scoped(app):
    barrier = Barrier(2)
    def create_same():
        with app.app_context():
            barrier.wait(timeout=10)
            execution = create_execution(
                1, "translator", "project", "project-1", {"kind": "source_ingestion"},
                queue_job_id="one-queued-plan", mode="passive", deduplicate_queue=True,
            )
            return execution.id
    db.session.remove()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: create_same(), range(2)))
    assert results[0] == results[1]
    assert McpExecution.query.filter_by(owner_user_id=1, queue_job_id="one-queued-plan").count() == 1
    other = create_execution(
        2, "translator", "project", "project-2", {"kind": "source_ingestion"},
        queue_job_id="one-queued-plan", mode="passive", deduplicate_queue=True,
    )
    assert other.id != results[0]
    with pytest.raises(McpWorkError, match="different execution intent"):
        create_execution(
            1, "translator", "project", "foreign-project", {"kind": "source_ingestion"},
            queue_job_id="one-queued-plan", mode="passive", deduplicate_queue=True,
        )


@pytest.mark.parametrize("wire", ["native", "json", "escaped_json"])
def test_completion_unicode_fidelity_for_nested_document_html(wire):
    original = {
        "pages": [
            {
                "page_number": 1,
                "html": "<p>über weiß schön a\u0308 Łódź 日本語 עברית 😀</p>",
            }
        ]
    }
    content = (
        original
        if wire == "native"
        else json.dumps(original, ensure_ascii=wire == "escaped_json")
    )
    result = validate_response(
        {"content": content}, {"response_format": {"type": "json_object"}}
    )
    assert json.loads(result["content"]) == original
    if wire != "native":
        assert (
            result["content"] == content
        )  # Valid existing string completions keep their bytes.


@pytest.mark.parametrize(
    "content", [{"text": "\ud800"}, '{"text":"\\ud800"}', '{"\\udfff":"value"}']
)
def test_invalid_unicode_never_reaches_document_extraction(content):
    with pytest.raises(McpWorkError) as caught:
        validate_response(
            {"content": content}, {"response_format": {"type": "json_object"}}
        )
    assert caught.value.validation_errors[0]["code"] == "invalid_unicode_content"
    assert "never ASCII-fold" in str(caught.value)
    assert "\ud800" not in str(caught.value)


@pytest.mark.parametrize("root", ["object", "array"])
def test_double_encoding_error_explains_native_json_without_repairing_payload(root):
    request = {
        "response_format": {
            "type": "json_schema",
            "json_schema": {"schema": {"type": root}},
        }
    }
    original = {"text": "über"} if root == "object" else ["über"]
    double = json.dumps(json.dumps(original))
    with pytest.raises(McpWorkError) as caught:
        validate_response({"content": double}, request)
    assert caught.value.code == "invalid_json_schema_response"
    assert caught.value.validation_errors[0]["expected_type"] == root
    assert "double-encoded" in str(caught.value)
    assert "über" not in str(caught.value)


def test_schema_string_content_that_looks_like_json_is_legitimate():
    text = '{"quote":"über"}'
    request = {
        "response_format": {
            "type": "json_schema",
            "json_schema": {"schema": {"type": "string"}},
        }
    }
    result = validate_response({"content": json.dumps(text)}, request)
    assert json.loads(result["content"]) == text
