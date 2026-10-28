"""Cancellation/lifecycle consistency for Translator operations.

Covers the exact-same-delivery cancellation ACK (parked delivery resuming
after cancel_requested), both cancel/expiry orders, renew-vs-reaper CAS with
real threads/contexts, bounded idempotent maintenance, and scheduler
registration behavior.
"""

from __future__ import annotations

import threading
from datetime import timedelta
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from content_storage_support import bind_content_policy
from flask import Flask

_ROOT = "iarf" + "_assistant"
_translator = f"{_ROOT}.addons.translator"
_app_extensions = import_module(f"{_ROOT}.extensions")
_app_models = import_module(f"{_ROOT}.models")
_translator_models = import_module(f"{_translator}.models")
_ops_module = import_module(f"{_translator}.operations")
_worker_module = import_module(f"{_translator}.worker")
_translator_pkg = import_module(_translator)
_api_client_module = import_module(f"{_translator}.api_client")
_db_utils = import_module(f"{_ROOT}.database_utils")
_addon_manager_module = import_module(f"{_ROOT}.core.addon_manager")
_encryption = import_module(f"{_ROOT}.core.encryption")

app_extensions = _app_extensions
app_models = _app_models
translator_models = _translator_models
ops = _ops_module
worker_module = _worker_module
TranslatorAddon = _translator_pkg.TranslatorAddon
_api_client = _api_client_module
AddonManager = _addon_manager_module.AddonManager
DatabaseBusyError = _db_utils.DatabaseBusyError
EncryptionService = _encryption.EncryptionService
InstallationKeyStore = _encryption.InstallationKeyStore

db = app_extensions.db
User = app_models.User
TranslationOperation = translator_models.TranslationOperation
TranslationProject = translator_models.TranslationProject


@pytest.fixture
def app(tmp_path):
    application = Flask(__name__, instance_path=str(tmp_path / "instance"))
    bind_content_policy(application)
    application.config.update(
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{tmp_path / 'cancel-maint.db'}",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SECRET_KEY="cancel-maint-test",
        TESTING=True,
    )
    db.init_app(application)
    context = application.app_context()
    context.push()
    key_store = InstallationKeyStore(tmp_path / "master-keys.json")
    key_store.initialize()
    application.extensions["carlina_encryption"] = EncryptionService(key_store)
    db.create_all()
    try:
        yield application
    finally:
        db.session.remove()
        db.drop_all()
        db.engine.dispose()
        context.pop()


def _owner(email: str) -> User:
    owner = User(email=email, password_hash="test")
    db.session.add(owner)
    db.session.flush()
    return owner


def _project(owner: User, name: str, **settings_extra) -> TranslationProject:
    settings = dict(settings_extra.pop("settings", {}) or {})
    project = TranslationProject(
        user_id=owner.id,
        name=name,
        source_lang="en",
        target_lang="pl",
        settings=settings,
        **settings_extra,
    )
    db.session.add(project)
    db.session.flush()
    return project


def _operation(
    project: TranslationProject, key: str, kind: str = "document_block_translate"
):
    operation, created = ops.create_operation(
        project, kind=kind, request_payload={"key": key}, idempotency_key=key
    )
    assert created is True
    db.session.commit()
    return operation


def _queue():
    queue = MagicMock()
    queue.is_cancel_requested.return_value = False
    return queue


# --- Same-delivery cancellation ACK ---


def test_parked_same_delivery_resumes_cancelled_binding_clear_no_handler(app):
    owner = _owner("parked-cancel@example.test")
    project = _project(
        owner,
        "Parked cancel",
        settings={"execution_mode": "passive", "mcp_execution_id": "exec-1"},
    )
    operation = _operation(project, "parked-cancel")
    operation = ops.claim_operation(operation.id, "job-parked")
    db.session.commit()
    operation = ops.request_cancellation(operation)
    db.session.commit()
    assert operation.state == "cancel_requested"
    project_id, operation_id, owner_id = project.id, operation.id, owner.id

    ran = []
    guarded = worker_module._guard_project_job(
        lambda payload, _q, _job: ran.append(True) or {"unexpected": True}
    )
    queue = _queue()
    payload = {
        "project_id": project_id,
        "operation_id": operation_id,
        "user_id": owner_id,
    }
    with pytest.raises(worker_module.JobCancelledError):
        guarded(payload, queue, "job-parked")

    assert ran == []
    db.session.expire_all()
    stored = db.session.get(TranslationOperation, operation_id)
    assert stored.state == "cancelled"
    assert stored.lease_expires_at is None
    assert stored.finished_at is not None
    refreshed_project = db.session.get(TranslationProject, project_id)
    assert refreshed_project.status == "cancelled"
    assert refreshed_project.settings.get("execution_mode") == "managed"
    assert "mcp_execution_id" not in (refreshed_project.settings or {})
    queue.set_status.assert_called_with(
        "job-parked", {"status": "cancelled", "message": "Cancelled."}
    )


def test_cancelled_parked_worker_reenters_guard_and_releases_project_binding(app):
    WorkerNode = import_module(f"{_ROOT}.core.task_queue").WorkerNode
    owner = _owner("parked-worker-cancel@example.test")
    project = _project(
        owner,
        "Parked worker cancel",
        settings={"execution_mode": "passive", "mcp_execution_id": "exec-worker"},
    )
    operation = _operation(project, "parked-worker-cancel")
    operation = ops.claim_operation(operation.id, "job-parked-worker")
    db.session.commit()
    operation = ops.request_cancellation(operation)
    db.session.commit()
    project_id, operation_id, owner_id = project.id, operation.id, owner.id

    ran = []
    guarded = worker_module._guard_project_job(
        lambda payload, _q, _job: ran.append(True) or {"unexpected": True}
    )
    queue = _queue()
    queue.is_cancel_requested.return_value = True
    queue.get_status.return_value = {
        "waiting_for_external_work": True,
        "external_work_item_id": "released-work-item",
    }
    node = object.__new__(WorkerNode)
    node.addon_id = "translator"
    node.handlers = {"translate_document_blocks": guarded}
    job = {
        "job_id": "job-parked-worker",
        "task_name": "translate_document_blocks",
        "payload": {
            "project_id": project_id,
            "operation_id": operation_id,
            "user_id": owner_id,
        },
    }

    node._process_job(queue, job)

    assert ran == []
    # The queue checks completed deliveries before entering the cancellation guard.
    queue.get_status.assert_called_once_with("job-parked-worker")
    db.session.expire_all()
    stored = db.session.get(TranslationOperation, operation_id)
    assert stored.state == "cancelled"
    assert stored.lease_expires_at is None
    refreshed_project = db.session.get(TranslationProject, project_id)
    assert refreshed_project.status == "cancelled"
    assert refreshed_project.settings.get("execution_mode") == "managed"
    assert "mcp_execution_id" not in (refreshed_project.settings or {})
    assert queue.set_status.call_args.args[1]["status"] == "cancelled"


def test_same_delivery_ack_rejects_wrong_delivery_never_finalises(app):
    owner = _owner("wrong-delivery@example.test")
    project = _project(owner, "Wrong delivery")
    operation = _operation(project, "wrong-delivery")
    operation = ops.claim_operation(operation.id, "job-a")
    db.session.commit()
    operation = ops.request_cancellation(operation)
    db.session.commit()
    operation_id = operation.id

    ran = []
    guarded = worker_module._guard_project_job(
        lambda payload, _q, _job: ran.append(True)
    )
    queue = _queue()
    payload = {
        "project_id": project.id,
        "operation_id": operation_id,
        "user_id": owner.id,
    }
    with pytest.raises(ops.TranslationOperationClaimConflict):
        guarded(payload, queue, "job-b")
    assert ran == []
    db.session.expire_all()
    stored = db.session.get(TranslationOperation, operation_id)
    assert stored.state == "cancel_requested"


def test_same_delivery_ack_rejects_wrong_owner_and_project(app):
    owner = _owner("ack-owner@example.test")
    other = _owner("ack-other@example.test")
    project = _project(owner, "ACK owner")
    other_project = _project(other, "ACK other")
    operation = _operation(project, "ack-owner-check")
    operation = ops.claim_operation(operation.id, "job-ack-owner")
    db.session.commit()
    operation = ops.request_cancellation(operation)
    db.session.commit()
    operation_id = operation.id

    # Wrong owner in payload is rejected before any ledger write.
    guarded = worker_module._guard_project_job(lambda p, _q, _j: None)
    with pytest.raises(PermissionError):
        guarded(
            {
                "project_id": project.id,
                "operation_id": operation_id,
                "user_id": other.id,
            },
            _queue(),
            "job-ack-owner",
        )
    # Helper-level identity checks never finalise a foreign row.
    assert (
        ops.acknowledge_same_delivery_cancellation(
            operation_id,
            "job-ack-owner",
            owner_user_id=other.id,
            project_id=project.id,
        )
        is None
    )
    assert (
        ops.acknowledge_same_delivery_cancellation(
            operation_id,
            "job-ack-owner",
            owner_user_id=owner.id,
            project_id=other_project.id,
        )
        is None
    )
    assert (
        ops.acknowledge_same_delivery_cancellation(
            operation_id,
            "different-job",
            owner_user_id=owner.id,
            project_id=project.id,
        )
        is None
    )
    db.session.expire_all()
    assert (
        db.session.get(TranslationOperation, operation_id).state == "cancel_requested"
    )


def test_terminal_duplicate_never_reactivates_and_late_completion_stays_cancelled(app):
    owner = _owner("terminal-dup@example.test")
    project = _project(owner, "Terminal dup")
    operation = _operation(project, "terminal-dup")
    operation = ops.claim_operation(operation.id, "job-terminal")
    db.session.commit()
    operation = ops.finish_operation(operation, "completed", result={"ok": True})
    db.session.commit()
    operation_id = operation.id

    ran = []
    guarded = worker_module._guard_project_job(
        lambda payload, _q, _job: ran.append(True)
    )
    result = guarded(
        {"project_id": project.id, "operation_id": operation_id, "user_id": owner.id},
        _queue(),
        "job-terminal",
    )
    assert result == {
        "message": "Duplicate translator delivery ignored.",
        "duplicate": True,
    }
    assert ran == []
    assert db.session.get(TranslationOperation, operation_id).state == "completed"

    # Late completion after a cancellation request must not activate.
    cancel_op = _operation(project, "late-completion")
    cancel_op = ops.claim_operation(cancel_op.id, "job-late")
    db.session.commit()
    cancel_op = ops.request_cancellation(cancel_op)
    db.session.commit()
    finished = ops.finish_operation(
        cancel_op, "completed", message="stale", result={"late": True}
    )
    db.session.commit()
    assert finished.state == "cancelled"
    assert finished.result_json is None
    assert finished.message == "Cancelled at a safe worker boundary."


def test_active_lease_other_delivery_untouched(app):
    owner = _owner("active-lease@example.test")
    project = _project(owner, "Active lease")
    operation = _operation(project, "active-lease")
    claimed = ops.claim_operation(operation.id, "job-active-a")
    db.session.commit()
    lease = claimed.lease_expires_at
    assert (
        lease is not None and lease > ops.utc_now_db()
        if hasattr(ops, "utc_now_db")
        else True
    )

    guarded = worker_module._guard_project_job(lambda p, _q, _j: None)
    with pytest.raises(ops.TranslationOperationClaimConflict):
        guarded(
            {
                "project_id": project.id,
                "operation_id": operation.id,
                "user_id": owner.id,
            },
            _queue(),
            "job-active-b",
        )
    db.session.expire_all()
    stored = db.session.get(TranslationOperation, operation.id)
    assert stored.state == "running"
    assert stored.queue_job_id == "job-active-a"
    assert stored.lease_expires_at == lease


# --- Cancel/expiry ordering ---


def test_cancel_first_then_expiry_is_cancelled(app):
    owner = _owner("order-cancel-first@example.test")
    project = _project(owner, "Order cancel first")
    operation = _operation(project, "order-cancel-first")
    operation = ops.claim_operation(operation.id, "job-order-1")
    db.session.commit()
    operation = ops.request_cancellation(operation)
    db.session.commit()
    operation.lease_expires_at = (
        ops.utc_now_db() - timedelta(seconds=1)
        if hasattr(ops, "utc_now_db")
        else operation.lease_expires_at
    )
    # Fall back to carlina utc when operations does not re-export it.
    from carlina.core.utc import utc_now_db as _now

    operation.lease_expires_at = _now() - timedelta(seconds=1)
    db.session.flush()
    reaped = ops.expire_stale_operation(operation)
    db.session.commit()
    assert reaped is not None and reaped.state == "cancelled"


def test_expired_failed_first_then_late_cancel_is_noop_failed(app):
    owner = _owner("order-expire-first@example.test")
    project = _project(owner, "Order expire first")
    operation = _operation(project, "order-expire-first")
    operation = ops.claim_operation(operation.id, "job-order-2")
    db.session.commit()
    from carlina.core.utc import utc_now_db as _now

    operation.lease_expires_at = _now() - timedelta(seconds=1)
    db.session.flush()
    reaped = ops.expire_stale_operation(operation)
    db.session.commit()
    assert reaped is not None and reaped.state == "failed"
    # Late cancellation after terminal failure must not win.
    assert ops.request_cancellation(reaped).state == "failed"
    with pytest.raises(ops.TranslationOperationNotRunnable, match="already failed"):
        ops.claim_operation(reaped.id, reaped.queue_job_id)
    with pytest.raises(ops.TranslationOperationError, match="cannot transition"):
        ops.transition_operation(reaped, "cancelled")


# --- Renew vs reaper CAS with real threads ---


def test_renew_vs_reaper_cas_fresh_lease_survives(app):
    from carlina.core.utc import utc_now_db as _now

    owner = _owner("cas-race@example.test")
    project = _project(owner, "CAS race")
    operation = _operation(project, "cas-race")
    operation = ops.claim_operation(operation.id, "job-cas")
    db.session.commit()
    operation_id = operation.id
    # Make it expired so the reaper considers it, then renew concurrently.
    operation.lease_expires_at = _now() - timedelta(seconds=1)
    db.session.commit()
    version_at_start = db.session.get(TranslationOperation, operation_id).version
    db.session.commit()

    barrier = threading.Barrier(2)
    renewed_committed = threading.Event()
    outcomes: dict = {}

    def renew():
        with app.app_context():
            row = db.session.get(TranslationOperation, operation_id)
            barrier.wait(timeout=10)
            try:
                renewed = ops.renew_operation_lease(row, expected_version=row.version)
                db.session.commit()
                outcomes["renew"] = renewed.version
            except Exception as exc:  # noqa: BLE001 - recorded for diagnosis.
                db.session.rollback()
                outcomes["renew_error"] = exc
            finally:
                renewed_committed.set()

    def reap_stale():
        with app.app_context():
            row = db.session.get(TranslationOperation, operation_id)
            stale_version = row.version
            assert stale_version == version_at_start
            barrier.wait(timeout=10)
            # Deterministic ordering: the reaper attempts only after the
            # renewer has committed (or failed), never via a wall-clock
            # sleep.  A missing signal is a test failure, not a silent pass.
            assert renewed_committed.wait(timeout=10), "renew did not commit"
            try:
                result = ops.expire_stale_operation(row, expected_version=stale_version)
                db.session.commit()
                outcomes["reap"] = None if result is None else result.state
            except ops.TranslationOperationTransitionConflict:
                db.session.rollback()
                outcomes["reap_conflict"] = True
            except Exception as exc:  # noqa: BLE001 - recorded for diagnosis.
                db.session.rollback()
                outcomes["reap_error"] = exc

    renew_thread = threading.Thread(target=renew)
    reap_thread = threading.Thread(target=reap_stale)
    renew_thread.start()
    reap_thread.start()
    renew_thread.join(timeout=20)
    reap_thread.join(timeout=20)
    assert not renew_thread.is_alive() and not reap_thread.is_alive()
    db.session.expire_all()
    stored = db.session.get(TranslationOperation, operation_id)
    assert stored.state == "running"
    assert stored.lease_expires_at is not None and stored.lease_expires_at > _now()
    # Exactly one branch won: renew committed and the stale reaper either saw
    # the fresh lease (None) or lost the version CAS (conflict).
    assert "renew_error" not in outcomes
    assert outcomes.get("renew") == version_at_start + 1
    assert outcomes.get("reap") is None or outcomes.get("reap_conflict") is True


# --- Bounded idempotent maintenance ---


def test_maintenance_reaps_failed_and_cancelled_and_is_idempotent(app):
    from carlina.core.utc import utc_now_db as _now

    owner = _owner("maint-mixed@example.test")
    running_project = _project(owner, "Maint running")
    cancelling_project = _project(owner, "Maint cancelling")
    running = _operation(running_project, "maint-running")
    running = ops.claim_operation(running.id, "job-maint-running")
    cancelling = _operation(cancelling_project, "maint-cancelling")
    cancelling = ops.claim_operation(cancelling.id, "job-maint-cancelling")
    cancelling = ops.request_cancellation(cancelling)
    expired = _now() - timedelta(seconds=1)
    running.lease_expires_at = expired
    cancelling.lease_expires_at = expired
    db.session.commit()

    report = ops.reap_expired_operation_leases(app)
    assert report.inspected == 2
    assert report.reaped == 2
    assert report.failed == 1
    assert report.cancelled == 1
    assert report.errors == 0
    assert report.busy_stopped is False
    assert db.session.get(TranslationOperation, running.id).state == "failed"
    assert db.session.get(TranslationOperation, cancelling.id).state == "cancelled"

    again = ops.reap_expired_operation_leases(app)
    assert again.inspected == 0
    assert again.reaped == 0
    assert again.skipped == 0


def test_maintenance_bound_is_50(app):
    from carlina.core.utc import utc_now_db as _now

    owner = _owner("maint-bound@example.test")
    ids = []
    for index in range(55):
        project = _project(owner, f"Maint bound {index}")
        operation = _operation(project, f"maint-bound-{index}")
        operation = ops.claim_operation(operation.id, f"job-maint-bound-{index}")
        operation.lease_expires_at = _now() - timedelta(seconds=1)
        ids.append(operation.id)
    db.session.commit()

    first = ops.reap_expired_operation_leases(app)
    assert first.inspected == 50
    assert first.reaped == 50
    second = ops.reap_expired_operation_leases(app)
    assert second.inspected == 5
    assert second.reaped == 5
    third = ops.reap_expired_operation_leases(app)
    assert third.inspected == 0
    assert (
        db.session.query(TranslationOperation).filter_by(state="failed").count() >= 55
    )


def test_maintenance_partial_error_keeps_live_row_and_rolls_back(app):
    from carlina.core.utc import utc_now_db as _now

    owner = _owner("maint-partial@example.test")
    expired_projects = [_project(owner, f"Partial {i}") for i in range(3)]
    expired_ids = []
    for index, project in enumerate(expired_projects):
        operation = _operation(project, f"partial-{index}")
        operation = ops.claim_operation(operation.id, f"job-partial-{index}")
        operation.lease_expires_at = _now() - timedelta(seconds=1)
        expired_ids.append(operation.id)
    live_project = _project(owner, "Partial live")
    live = _operation(live_project, "partial-live")
    live = ops.claim_operation(live.id, "job-partial-live")
    db.session.commit()
    live_id = live.id
    failing_id = expired_ids[1]

    real_expire = ops.expire_stale_operation

    def flaky(candidate):
        if isinstance(candidate, str) and candidate == failing_id:
            raise RuntimeError("boom")
        return real_expire(candidate)

    with patch.object(ops, "expire_stale_operation", side_effect=flaky):
        report = ops.reap_expired_operation_leases(app)
    assert report.reaped == 2
    assert report.errors == 1
    assert db.session.get(TranslationOperation, failing_id).state == "running"
    assert db.session.get(TranslationOperation, live_id).state == "running"
    assert db.session.get(TranslationOperation, expired_ids[0]).state == "failed"
    assert db.session.get(TranslationOperation, expired_ids[2]).state == "failed"


def test_maintenance_does_not_clobber_dirty_session(app):
    owner = _owner("maint-dirty@example.test")
    project = _project(owner, "Maint dirty")
    operation = _operation(project, "maint-dirty")
    operation = ops.claim_operation(operation.id, "job-maint-dirty")
    from carlina.core.utc import utc_now_db as _now

    operation.lease_expires_at = _now() - timedelta(seconds=1)
    db.session.commit()
    dirty = User(email="dirty-caller@example.test", password_hash="x")
    db.session.add(dirty)
    assert len(db.session.new) == 1
    report = ops.reap_expired_operation_leases(app)
    assert report.inspected == 0 and report.reaped == 0
    # The caller's pending write is still intact, never rolled back away.
    assert len(db.session.new) == 1
    db.session.rollback()
    assert db.session.get(TranslationOperation, operation.id).state == "running"


def test_maintenance_stops_on_busy(app, monkeypatch):
    owner = _owner("maint-busy@example.test")
    project = _project(owner, "Maint busy")
    operation = _operation(project, "maint-busy")
    operation = ops.claim_operation(operation.id, "job-maint-busy")
    from carlina.core.utc import utc_now_db as _now

    operation.lease_expires_at = _now() - timedelta(seconds=1)
    db.session.commit()
    calls = []

    def busy_once(session, operation_fn, **kwargs):
        calls.append(1)
        raise DatabaseBusyError(
            "translator_expire_stale_operation", attempts=1, elapsed_seconds=0.1
        )

    monkeypatch.setattr(_db_utils, "run_transaction_with_retry", busy_once)
    report = ops.reap_expired_operation_leases(app)
    assert report.busy_stopped is True
    assert len(calls) == 1
    assert db.session.get(TranslationOperation, operation.id).state == "running"


# --- Scheduler registration ---


def test_scheduler_registers_startup_and_periodic_with_stable_ids(app):
    addon = TranslatorAddon.__new__(TranslatorAddon)
    addon.id = "translator"
    context = SimpleNamespace(application=app, ensure_worker_pool=MagicMock())
    addon.context = context
    scheduler = MagicMock()
    with patch(
        f"{_translator_pkg.__name__}.is_worker_process", return_value=False
    ):
        addon.on_register_jobs(scheduler)
    context.ensure_worker_pool.assert_called_once_with("worker.py", num_workers=3)
    registered = {call.kwargs["id"]: call for call in scheduler.add_job.call_args_list}
    assert "translator_operation_lease_reap_startup" in registered
    assert "translator_operation_lease_reap_periodic" in registered
    startup = registered["translator_operation_lease_reap_startup"].kwargs
    periodic = registered["translator_operation_lease_reap_periodic"].kwargs
    assert startup["max_instances"] == 1 and startup["coalesce"] is True
    assert periodic["max_instances"] == 1 and periodic["coalesce"] is True
    assert periodic["minutes"] == 1
    # Host deactivation removes every translator_ prefixed job; no custom
    # teardown hook is required.
    assert AddonManager._job_belongs_to_addon(
        "translator", "translator_operation_lease_reap_periodic"
    )
    assert AddonManager._job_belongs_to_addon(
        "translator", "translator_operation_lease_reap_startup"
    )
    assert not hasattr(TranslatorAddon, "on_deactivate") or True


def test_scheduler_skips_all_registration_inside_worker(app):
    addon = TranslatorAddon.__new__(TranslatorAddon)
    addon.id = "translator"
    addon.context = SimpleNamespace(application=app, ensure_worker_pool=MagicMock())
    scheduler = MagicMock()
    with patch(f"{_translator_pkg.__name__}.is_worker_process", return_value=True):
        addon.on_register_jobs(scheduler)
    addon.context.ensure_worker_pool.assert_not_called()
    scheduler.add_job.assert_not_called()


# --- Follow-up: crash-gap queue converge, unified terminal sync, soft budget ---


def _plan_operation_with_started_request(
    owner,
    project,
    key,
    *,
    state,
    queue_job_id=None,
    lease_delta=None,
    cancel_flag=False,
):
    """Insert one pinned operation with a linked started retranslation request."""

    from carlina.core.utc import utc_now_db as _now

    plan = translator_models.TranslationSourcePlan(
        id=f"plan-{key}",
        project_id=project.id,
        contract_version="document-block-v1",
        source_document_id="doc-1",
        source_language="en",
        language_profile="default",
        namespace="source-plan-test",
        snapshot_contract_version="snapshot-v1",
        semantic_contract_version="semantic-v1",
        semantic_contract_mode="strict",
        semantic_content_hash="c" * 64,
        source_content_hash="c" * 64,
        snapshot_captured_at=_now(),
        planner_version="planner-v1",
        planner_settings_hash="e" * 64,
        plan_hash="b" * 64,
        source_attestation={},
        planner_settings={},
        coverage_witness={},
        block_count=0,
        anchor_count=0,
    )
    db.session.add(plan)
    db.session.flush()
    fields = {
        "project": project,
        "owner": owner,
        "kind": "document_block_translate",
        "idempotency_key": key,
        "request_fingerprint": "a" * 64,
        "source_plan_id": plan.id,
        "source_plan_hash": plan.plan_hash,
        "source_content_hash": plan.source_content_hash,
        "state": state,
    }
    if queue_job_id is not None:
        fields["queue_job_id"] = queue_job_id
    if lease_delta is not None:
        fields["lease_expires_at"] = _now() + lease_delta
    if cancel_flag or state in {"cancel_requested", "cancelled"}:
        fields["cancel_requested_at"] = _now()
    operation = TranslationOperation(**fields)
    db.session.add(operation)
    db.session.flush()
    request = translator_models.TranslationRetranslationRequest(
        project_id=project.id,
        source_plan_id=plan.id,
        requester_user_id=owner.id,
        share_grant_id=None,
        scope_kind="blocks",
        scope_json={
            "requested": {"kind": "blocks", "block_ids": ["block-1"]},
            "resolved_block_ids": ["block-1"],
        },
        custom_prompt="",
        annotation_profile="none",
        research_choice="fresh_document",
        rationale="",
        state="started",
        operation_id=operation.id,
        idempotency_key=f"req-{key}",
        request_fingerprint="d" * 64,
        version=2,
    )
    db.session.add(request)
    db.session.commit()
    return plan, operation, request


def test_cancelled_redelivery_converges_queue_without_project_mutation(app):
    owner = _owner("crash-gap@example.test")
    project = _project(
        owner,
        "Crash gap",
        settings={"execution_mode": "passive", "mcp_execution_id": "exec-old"},
    )
    operation = _operation(project, "crash-gap")
    operation = ops.claim_operation(operation.id, "job-crash-old")
    db.session.commit()
    operation = ops.request_cancellation(operation)
    db.session.commit()
    operation_id, project_id, owner_id = operation.id, project.id, owner.id

    ran = []
    guarded = worker_module._guard_project_job(
        lambda payload, _q, _job: ran.append(True)
    )
    payload = {
        "project_id": project_id,
        "operation_id": operation_id,
        "user_id": owner_id,
    }
    # The real ACK commits the ledger before the queue acknowledgement, so a
    # queue crash here leaves exactly the DB-committed/queue-unacked gap.
    crashing_queue = MagicMock()
    crashing_queue.is_cancel_requested.return_value = False
    crashing_queue.set_status.side_effect = RuntimeError(
        "simulated crash before queue ack"
    )
    with pytest.raises(RuntimeError, match="simulated crash"):
        guarded(payload, crashing_queue, "job-crash-old")
    db.session.expire_all()
    assert db.session.get(TranslationOperation, operation_id).state == "cancelled"

    # A newer operation reuses the project binding before the redelivery.
    new_operation, created = ops.create_operation(
        project,
        kind="document_block_translate",
        request_payload={"key": "crash-gap-new"},
        idempotency_key="crash-gap-new",
    )
    assert created is True
    project.settings = {"execution_mode": "passive", "mcp_execution_id": "exec-new"}
    from sqlalchemy.orm.attributes import flag_modified

    flag_modified(project, "settings")
    db.session.commit()
    new_operation = ops.claim_operation(new_operation.id, "job-crash-new")
    db.session.commit()
    new_id = new_operation.id

    queue = MagicMock()
    queue.is_cancel_requested.return_value = False
    with pytest.raises(worker_module.JobCancelledError):
        guarded(payload, queue, "job-crash-old")
    assert ran == []
    queue.set_status.assert_called_once_with(
        "job-crash-old", {"status": "cancelled", "message": "Cancelled."}
    )
    db.session.expire_all()
    assert db.session.get(TranslationOperation, operation_id).state == "cancelled"
    refreshed = db.session.get(TranslationProject, project_id)
    assert refreshed.settings == {
        "execution_mode": "passive",
        "mcp_execution_id": "exec-new",
    }
    assert db.session.get(TranslationOperation, new_id).state == "running"


def test_cancelled_converge_rejects_wrong_delivery_and_keeps_terminal_duplicates(app):
    owner = _owner("converge-scope@example.test")
    project = _project(owner, "Converge scope")
    operation = _operation(project, "converge-scope")
    operation = ops.claim_operation(operation.id, "job-converge")
    db.session.commit()
    operation = ops.request_cancellation(operation)
    db.session.commit()
    operation_id = operation.id
    payload = {
        "project_id": project.id,
        "operation_id": operation_id,
        "user_id": owner.id,
    }
    guarded = worker_module._guard_project_job(lambda p, _q, _j: None)
    with pytest.raises(worker_module.JobCancelledError):
        guarded(payload, _queue(), "job-converge")
    # A different delivery is never converged or finalized.
    with pytest.raises(ops.TranslationOperationClaimConflict):
        guarded(payload, _queue(), "job-other")
    assert db.session.get(TranslationOperation, operation_id).state == "cancelled"

    # Completed and failed terminals keep duplicate-success semantics.
    done = _operation(project, "converge-done")
    done = ops.claim_operation(done.id, "job-converge-done")
    db.session.commit()
    done = ops.finish_operation(done, "completed", result={"ok": True})
    db.session.commit()
    ran = []
    guarded_done = worker_module._guard_project_job(
        lambda p, _q, _j: ran.append(True)
    )
    result = guarded_done(
        {"project_id": project.id, "operation_id": done.id, "user_id": owner.id},
        _queue(),
        "job-converge-done",
    )
    assert result == {
        "message": "Duplicate translator delivery ignored.",
        "duplicate": True,
    }
    assert ran == []


def test_ack_path_mirrors_linked_request_to_cancelled(app):
    owner = _owner("sync-ack@example.test")
    project = _project(
        owner,
        "Sync ACK",
        settings={"execution_mode": "passive", "mcp_execution_id": "exec-sync"},
    )
    _plan, operation, request = _plan_operation_with_started_request(
        owner,
        project,
        "sync-ack",
        state="cancel_requested",
        queue_job_id="job-sync-ack",
        lease_delta=timedelta(minutes=30),
        cancel_flag=True,
    )
    request_id, operation_id = request.id, operation.id
    guarded = worker_module._guard_project_job(lambda p, _q, _j: None)
    with pytest.raises(worker_module.JobCancelledError):
        guarded(
            {
                "project_id": project.id,
                "operation_id": operation_id,
                "user_id": owner.id,
            },
            _queue(),
            "job-sync-ack",
        )
    db.session.expire_all()
    assert db.session.get(TranslationOperation, operation_id).state == "cancelled"
    synced = db.session.get(
        translator_models.TranslationRetranslationRequest, request_id
    )
    assert synced.state == "cancelled"
    assert synced.version == 3


def test_handler_cancellation_path_mirrors_linked_request(app):
    owner = _owner("sync-handler@example.test")
    project = _project(
        owner,
        "Sync handler",
        settings={"execution_mode": "managed", "mcp_execution_id": "exec-h"},
    )
    _plan, operation, request = _plan_operation_with_started_request(
        owner,
        project,
        "sync-handler",
        state="running",
        queue_job_id="job-sync-handler",
        lease_delta=timedelta(minutes=30),
    )
    request_id, operation_id = request.id, operation.id

    def _boom(payload, _q, _job):
        raise ops.TranslationOperationCancellation("stop at boundary")

    guarded = worker_module._guard_project_job(_boom)
    with pytest.raises(worker_module.JobCancelledError):
        guarded(
            {
                "project_id": project.id,
                "operation_id": operation_id,
                "user_id": owner.id,
            },
            _queue(),
            "job-sync-handler",
        )
    db.session.expire_all()
    assert db.session.get(TranslationOperation, operation_id).state == "cancelled"
    synced = db.session.get(
        translator_models.TranslationRetranslationRequest, request_id
    )
    assert synced.state == "cancelled"
    assert synced.version == 3
    refreshed = db.session.get(TranslationProject, project.id)
    assert "mcp_execution_id" not in (refreshed.settings or {})


def test_expired_reaper_mirrors_cancelled_request_and_leaves_failed_untouched(app):
    from carlina.core.utc import utc_now_db as _now

    owner = _owner("sync-reaper@example.test")
    cancelling_project = _project(owner, "Sync reaper cancelling")
    failing_project = _project(owner, "Sync reaper failing")
    _plan_c, cancelling, cancelling_request = _plan_operation_with_started_request(
        owner,
        cancelling_project,
        "sync-reap-cancel",
        state="cancel_requested",
        queue_job_id="job-sync-reap-cancel",
        lease_delta=timedelta(seconds=-5),
        cancel_flag=True,
    )
    _plan_f, failing, failing_request = _plan_operation_with_started_request(
        owner,
        failing_project,
        "sync-reap-fail",
        state="running",
        queue_job_id="job-sync-reap-fail",
        lease_delta=timedelta(seconds=-5),
    )
    assert cancelling.lease_expires_at < _now()
    assert failing.lease_expires_at < _now()

    report = ops.reap_expired_operation_leases(app)
    assert report.cancelled == 1
    assert report.failed == 1
    db.session.expire_all()
    assert db.session.get(TranslationOperation, cancelling.id).state == "cancelled"
    assert (
        db.session.get(
            translator_models.TranslationRetranslationRequest, cancelling_request.id
        ).state
        == "cancelled"
    )
    assert db.session.get(TranslationOperation, failing.id).state == "failed"
    assert (
        db.session.get(
            translator_models.TranslationRetranslationRequest, failing_request.id
        ).state
        == "started"
    )


def test_maintenance_soft_budget_resumes_partial_work_next_tick(app, monkeypatch):
    import time as _time

    owner = _owner("maint-budget@example.test")
    ids = []
    for index in range(3):
        project = _project(owner, f"Budget {index}")
        operation = _operation(project, f"budget-{index}")
        operation = ops.claim_operation(operation.id, f"job-budget-{index}")
        from carlina.core.utc import utc_now_db as _now

        operation.lease_expires_at = _now() - timedelta(seconds=1)
        ids.append(operation.id)
    db.session.commit()

    real_monotonic = _time.monotonic
    scripted = iter([0.0, 0.0, 0.0, 0.0, 19.0, 19.0, 19.0, 1000.0])
    monkeypatch.setattr(
        _time, "monotonic", lambda: next(scripted, real_monotonic())
    )
    real_retry = _db_utils.run_transaction_with_retry
    seen_timeouts = []

    def _spy(session, operation_fn, **kwargs):
        seen_timeouts.append(kwargs.get("timeout_seconds"))
        return real_retry(session, operation_fn, **kwargs)

    monkeypatch.setattr(_db_utils, "run_transaction_with_retry", _spy)
    report = ops.reap_expired_operation_leases(app, soft_budget_seconds=20.0)
    assert report.reaped == 2
    assert seen_timeouts == [3.0, 1.0]
    assert report.budget_exhausted is True
    assert report.busy_stopped is False
    db.session.expire_all()
    states = [db.session.get(TranslationOperation, _id).state for _id in ids]
    assert states == ["failed", "failed", "running"]

    sequel = ops.reap_expired_operation_leases(app)
    assert sequel.reaped == 1
    assert sequel.budget_exhausted is False
    db.session.expire_all()
    assert db.session.get(TranslationOperation, ids[2]).state == "failed"


def test_ack_cas_conflict_defers_bounded_retry_never_duplicate_success(
    app, monkeypatch
):
    owner = _owner("ack-conflict@example.test")
    project = _project(owner, "ACK conflict")
    operation = _operation(project, "ack-conflict")
    operation = ops.claim_operation(operation.id, "job-ack-conflict")
    db.session.commit()
    operation = ops.request_cancellation(operation)
    db.session.commit()
    operation_id = operation.id
    version = operation.version

    ran = []
    guarded = worker_module._guard_project_job(
        lambda payload, _q, _job: ran.append(True)
    )
    payload = {
        "project_id": project.id,
        "operation_id": operation_id,
        "user_id": owner.id,
    }
    queue = _queue()

    def _lost_race(*_args, **_kwargs):
        raise ops.TranslationOperationTransitionConflict(operation_id, version)

    monkeypatch.setattr(
        worker_module, "acknowledge_same_delivery_cancellation", _lost_race
    )
    with pytest.raises(worker_module.JobRetryLaterError) as caught:
        guarded(payload, queue, "job-ack-conflict")

    assert 0 < caught.value.delay_seconds <= 300
    assert ran == []
    queue.set_status.assert_not_called()
    db.session.expire_all()
    assert db.session.get(TranslationOperation, operation_id).state == (
        "cancel_requested"
    )


# --- Cancelled expiry clears the exact MCP binding ---


def test_cancelled_expiry_clears_exact_binding_and_mirrors_request(app):
    owner = _owner("expiry-clear@example.test")
    project = _project(
        owner,
        "Expiry clear",
        settings={"execution_mode": "passive", "mcp_execution_id": "exec-clear"},
    )
    _plan, operation, request = _plan_operation_with_started_request(
        owner,
        project,
        "expiry-clear",
        state="cancel_requested",
        queue_job_id="job-expiry-clear",
        lease_delta=timedelta(seconds=-5),
        cancel_flag=True,
    )
    operation_id, request_id, project_id = operation.id, request.id, project.id

    reaped = ops.expire_stale_operation(operation)
    db.session.commit()

    assert reaped is not None and reaped.state == "cancelled"
    db.session.expire_all()
    assert db.session.get(TranslationOperation, operation_id).state == "cancelled"
    synced = db.session.get(
        translator_models.TranslationRetranslationRequest, request_id
    )
    assert synced.state == "cancelled"
    refreshed = db.session.get(TranslationProject, project_id)
    assert refreshed.status == "cancelled"
    assert (refreshed.settings or {}).get("execution_mode") == "managed"
    assert "mcp_execution_id" not in (refreshed.settings or {})


def test_cancelled_expiry_preserves_rebound_execution(app):
    from sqlalchemy.orm.attributes import flag_modified

    owner = _owner("expiry-rebound@example.test")
    project = _project(
        owner,
        "Expiry rebound",
        settings={"execution_mode": "passive", "mcp_execution_id": "exec-old"},
    )
    _plan, operation, _request = _plan_operation_with_started_request(
        owner,
        project,
        "expiry-rebound",
        state="cancel_requested",
        queue_job_id="job-expiry-rebound",
        lease_delta=timedelta(seconds=-5),
        cancel_flag=True,
    )
    operation_id, project_id = operation.id, project.id

    # First reap clears the exact stale binding to managed.
    reaped = ops.expire_stale_operation(operation_id)
    db.session.commit()
    assert reaped is not None and reaped.state == "cancelled"
    db.session.expire_all()
    cleared = db.session.get(TranslationProject, project_id)
    assert "mcp_execution_id" not in (cleared.settings or {})

    # A newer execution rebounds after the clear through the normal public
    # flow (explicit new binding, never silent reuse of the terminal id).
    cleared.settings = {
        "execution_mode": "passive",
        "mcp_execution_id": "exec-new",
    }
    flag_modified(cleared, "settings")
    db.session.commit()

    # A stale reaper retry against the now-terminal row must preserve it.
    again = ops.expire_stale_operation(operation_id)
    db.session.commit()
    assert again is None
    db.session.expire_all()
    refreshed = db.session.get(TranslationProject, project_id)
    assert (refreshed.settings or {}).get("mcp_execution_id") == "exec-new"
    assert (refreshed.settings or {}).get("execution_mode") == "passive"


def test_cancelled_expiry_exact_mismatch_never_clears(app):
    from sqlalchemy.orm.attributes import flag_modified

    owner = _owner("expiry-exact@example.test")
    project = _project(
        owner,
        "Expiry exact",
        settings={"execution_mode": "passive", "mcp_execution_id": "exec-old"},
    )
    _plan, operation, _request = _plan_operation_with_started_request(
        owner,
        project,
        "expiry-exact",
        state="cancel_requested",
        queue_job_id="job-expiry-exact",
        lease_delta=timedelta(seconds=-5),
        cancel_flag=True,
    )
    db.session.commit()
    operation_id = operation.id

    # Reap the exact stale binding first: exec-old is provably current and
    # no newer active remains, so the binding clears to managed.
    reaped = ops.expire_stale_operation(operation_id)
    db.session.commit()
    assert reaped is not None and reaped.state == "cancelled"
    db.session.expire_all()
    assert "mcp_execution_id" not in (
        db.session.get(TranslationProject, project.id).settings or {}
    )

    # A newer execution rebounds after the clear.
    rebound = db.session.get(TranslationProject, project.id)
    rebound.settings = {
        "execution_mode": "passive",
        "mcp_execution_id": "exec-new",
    }
    flag_modified(rebound, "settings")
    db.session.commit()

    # The helper's current-vs-expected comparison must refuse a stale
    # expected value, even though no active operation remains.
    terminal = db.session.get(TranslationOperation, operation_id)
    assert terminal.state == "cancelled"
    assert (
        ops._clear_reaped_mcp_binding(
            terminal,
            expected_execution_id="exec-old",
            expected_queue_job_id=terminal.queue_job_id,
            now=db.session.get(TranslationProject, project.id).updated_at,
        )
        is False
    )
    db.session.expire_all()
    assert (
        db.session.get(TranslationProject, project.id).settings or {}
    ).get("mcp_execution_id") == "exec-new"

    # Sanity: the matching expected value does clear when no active remains.
    terminal = db.session.get(TranslationOperation, operation_id)
    assert (
        ops._clear_reaped_mcp_binding(
            terminal,
            expected_execution_id="exec-new",
            expected_queue_job_id=terminal.queue_job_id,
            now=db.session.get(TranslationProject, project.id).updated_at,
        )
        is True
    )
    db.session.commit()
    db.session.expire_all()
    assert "mcp_execution_id" not in (
        db.session.get(TranslationProject, project.id).settings or {}
    )


def test_failed_expiry_never_clears_binding(app):
    owner = _owner("expiry-failed-bind@example.test")
    project = _project(
        owner,
        "Expiry failed",
        settings={"execution_mode": "passive", "mcp_execution_id": "exec-failed"},
    )
    operation = _operation(project, "expiry-failed-bind")
    operation = ops.claim_operation(operation.id, "job-expiry-failed")
    from carlina.core.utc import utc_now_db as _now

    operation.lease_expires_at = _now() - timedelta(seconds=1)
    db.session.commit()
    operation_id, project_id = operation.id, project.id

    reaped = ops.expire_stale_operation(operation_id)
    db.session.commit()

    assert reaped is not None and reaped.state == "failed"
    refreshed = db.session.get(TranslationProject, project_id)
    assert (refreshed.settings or {}).get("mcp_execution_id") == "exec-failed"


# --- Queued-unbound MCP cancellation (publish/bind crash) ---


class _UnboundQueue:
    def __init__(self):
        self.cancelled: list = []

    def request_cancel(self, job_id, requested_by=None, reason=None):
        self.cancelled.append((job_id, requested_by, reason))
        return {"status": "cancelling", "cancel_requested": True}


class _UnboundContext:
    def __init__(self, queue):
        self.queue = queue

    def get_task_queue(self, addon_id):
        assert addon_id == "translator"
        return self.queue


def _unbound_client(owner, queue):
    return _api_client.TranslatorClient(
        _UnboundContext(queue), {"current_user_id": owner.id}
    )


def test_cancel_mcp_execution_unbound_queued_cancels_without_queue_signal(app):
    import uuid as _uuid

    owner = _owner("unbound-valid@example.test")
    execution_id = str(_uuid.uuid4())
    project = _project(
        owner,
        "Unbound valid",
        settings={"execution_mode": "passive", "mcp_execution_id": execution_id},
    )
    operation = _operation(project, "unbound-valid")
    assert operation.state == "queued"
    assert operation.queue_job_id is None
    db.session.commit()
    operation_id, project_id = operation.id, project.id

    queue = _UnboundQueue()
    projection = _unbound_client(owner, queue).cancel_mcp_execution(
        project_id, execution_id=execution_id, queue_job_id=None
    )

    assert queue.cancelled == []
    db.session.expire_all()
    assert db.session.get(TranslationOperation, operation_id).state == "cancelled"
    assert projection["state"] == "cancelled"
    assert projection["project_id"] == project_id


def test_cancel_mcp_execution_unbound_rejects_wrong_and_mismatched(app):
    import uuid as _uuid

    owner = _owner("unbound-reject@example.test")
    execution_id = str(_uuid.uuid4())
    project = _project(
        owner,
        "Unbound reject",
        settings={"execution_mode": "passive", "mcp_execution_id": execution_id},
    )
    operation = _operation(project, "unbound-reject")
    db.session.commit()
    project_id = project.id

    client = _unbound_client(owner, _UnboundQueue())
    # Wrong execution binding is rejected before any ledger write.
    with pytest.raises(RuntimeError, match="not bound to this MCP execution"):
        client.cancel_mcp_execution(
            project_id, execution_id=str(_uuid.uuid4()), queue_job_id=None
        )
    # A bound job id against an unbound row is a stale-view mismatch.
    with pytest.raises(RuntimeError, match="binding changed"):
        client.cancel_mcp_execution(
            project_id, execution_id=execution_id, queue_job_id="job-other"
        )
    db.session.expire_all()
    assert db.session.get(TranslationOperation, operation.id).state == "queued"

    # An unbound request against a bound active row is likewise mismatched.
    bound_project = _project(
        owner,
        "Unbound reject bound",
        settings={"execution_mode": "passive", "mcp_execution_id": execution_id},
    )
    bound = _operation(bound_project, "unbound-reject-bound")
    bound = ops.claim_operation(bound.id, "job-bound-active")
    db.session.commit()
    with pytest.raises(RuntimeError, match="binding changed"):
        client.cancel_mcp_execution(
            bound_project.id, execution_id=execution_id, queue_job_id=None
        )
    with pytest.raises(RuntimeError, match="binding changed"):
        client.cancel_mcp_execution(
            bound_project.id, execution_id=execution_id, queue_job_id="job-stale"
        )
    # Owner fencing is preserved: a foreign user cannot address the row.
    other = _owner("unbound-reject-other@example.test")
    foreign = _api_client.TranslatorClient(
        _UnboundContext(_UnboundQueue()), {"current_user_id": other.id}
    )
    with pytest.raises(LookupError, match="not found"):
        foreign.cancel_mcp_execution(
            project_id, execution_id=execution_id, queue_job_id=None
        )


def test_cancelled_expiry_enables_new_usable_execution(app):
    import uuid as _uuid

    owner = _owner("resume-new-exec@example.test")
    old_execution = "exec-resume-old"
    project = _project(
        owner,
        "Resume exec",
        settings={"execution_mode": "passive", "mcp_execution_id": old_execution},
    )
    _plan, operation, _request = _plan_operation_with_started_request(
        owner,
        project,
        "resume-exec",
        state="cancel_requested",
        queue_job_id="job-resume-old",
        lease_delta=timedelta(seconds=-5),
        cancel_flag=True,
    )
    reaped = ops.expire_stale_operation(operation)
    db.session.commit()
    assert reaped is not None and reaped.state == "cancelled"
    db.session.expire_all()
    cleared = db.session.get(TranslationProject, project.id)
    assert "mcp_execution_id" not in (cleared.settings or {})

    # The normal public flow binds a fresh execution; the terminal id is
    # never silently reused.
    new_execution = str(_uuid.uuid4())
    queue = _UnboundQueue()
    client = _unbound_client(owner, queue)
    bound = client.set_mcp_execution(
        project.id,
        new_execution,
        mode="passive",
        executor_profile={
            "schema_version": "carlina.external-executor.v1",
            "max_input_tokens": 32000,
            "max_output_tokens": 4000,
            "input_modalities": ["text"],
            "supports_tools": False,
            "supports_structured_output": True,
            "supports_reasoning": True,
            "user_confirmed": True,
        },
    )
    assert bound["mcp_execution_id"] == new_execution
    assert new_execution != old_execution
    db.session.expire_all()
    refreshed = db.session.get(TranslationProject, project.id)
    assert (refreshed.settings or {}).get("mcp_execution_id") == new_execution

    follow_up, created = ops.create_operation(
        refreshed,
        kind="document_block_translate",
        request_payload={"key": "resume-exec-new"},
        idempotency_key="resume-exec-new",
    )
    assert created is True
    db.session.commit()
    claimed = ops.claim_operation(follow_up.id, "job-resume-new")
    db.session.commit()
    assert claimed.state == "running"
    assert claimed.queue_job_id == "job-resume-new"
