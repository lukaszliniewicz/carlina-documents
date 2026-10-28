"""File-backed recovery contracts for passive PDF executions."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace

import pytest
from content_storage_support import bind_content_policy
from flask import Flask

from iarf_assistant.addons.doc_processor import execution_recovery, routes, worker
from iarf_assistant.addons.doc_processor.execution_recovery import (
    freeze_recovery_contract,
    reconcile_resume_dispatch,
    recovery_projection,
    resume_execution,
    settings_fingerprint,
    suspend_execution,
    validate_delivery,
)
from iarf_assistant.addons.doc_processor.mcp_service import DocProcessorMcpService
from iarf_assistant.addons.doc_processor.models import ProcessedDocument
from iarf_assistant.core.encryption import EncryptionService, InstallationKeyStore
from iarf_assistant.core.mcp_jobs import McpWorkError
from iarf_assistant.core.task_queue import TaskQueue
from iarf_assistant.extensions import db
from iarf_assistant.models import McpExecution, McpWorkItem, User


class _Context:
    def __init__(self, app: Flask, queue: TaskQueue):
        self.application = app
        self.queue = queue

    def ensure_worker_pool(self, _script: str, *, num_workers: int = 1) -> None:
        assert num_workers == 1

    def get_task_queue(self, addon_id: str) -> TaskQueue:
        assert addon_id == "doc_processor"
        return self.queue


@pytest.fixture
def setup(tmp_path: Path):
    app = Flask(__name__, instance_path=str(tmp_path / "instance"))
    bind_content_policy(app)
    app.config.update(
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{tmp_path / 'recovery.db'}",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SECRET_KEY="recovery-test",
        TESTING=True,
        CARLINA_UPLOADS_DIR=str(tmp_path / "uploads"),
    )
    db.init_app(app)
    app.extensions["carlina_encryption"] = EncryptionService(
        InstallationKeyStore.ephemeral()
    )
    with app.app_context():
        db.create_all()
        db.session.add_all(
            [
                User(
                    id=1,
                    email="recovery-one@example.test",
                    password_hash="x",
                    addon_settings={"doc_processor": {"page_prompt": "frozen"}},
                ),
                User(id=2, email="recovery-two@example.test", password_hash="x"),
            ]
        )
        db.session.commit()
        source = tmp_path / "uploads" / "1" / "source.pdf"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"%PDF synthetic original")
        settings = {
            "engine_id": "llm_vision",
            "parsing_model": "configured-model",
            "metadata_model": "configured-model",
            "pdf_options": {"skip_cropping": True},
            "mcp_execution_mode": "passive",
        }
        doc = ProcessedDocument(
            user_id=1,
            filename="source.pdf",
            file_path=str(source),
            status="processing",
            settings=settings,
        )
        db.session.add(doc)
        db.session.flush()
        execution = McpExecution(
            owner_user_id=1,
            addon_id="doc_processor",
            resource_kind="document",
            resource_id=doc.id,
            mode="passive",
            status="running",
            queue_job_id="initial-job",
            request_json={"recovery": freeze_recovery_contract(doc, source, settings)},
        )
        db.session.add(execution)
        db.session.flush()
        doc.settings = {
            **settings,
            "mcp_execution_id": execution.id,
            "active_job_id": "initial-job",
        }
        db.session.commit()
        queue = TaskQueue(str(tmp_path / "instance"), "doc_processor")
        try:
            yield SimpleNamespace(
                app=app,
                source=source,
                document_id=doc.id,
                execution_id=execution.id,
                queue=queue,
            )
        finally:
            queue.close()
            db.session.remove()
            db.drop_all()
            db.engine.dispose()


def _service(setup, owner: int = 1):
    return DocProcessorMcpService(_Context(setup.app, setup.queue), owner)


def test_browser_pause_uses_same_recoverable_execution(setup, monkeypatch):
    document = db.session.get(ProcessedDocument, setup.document_id)
    monkeypatch.setattr(routes, "_get_user_document", lambda _: document)
    monkeypatch.setattr(routes, "_get_task_queue", lambda: setup.queue)
    with setup.app.test_request_context("/control", method="POST", json={"action": "pause"}):
        response = routes.control_document_processing.__wrapped__(document.id)
        assert response.get_json()["success"]
        response = routes.control_document_processing.__wrapped__(document.id)
        assert response.get_json()["idempotent"]
    db.session.expire_all()
    assert document.status == "suspended"
    assert document.settings["mcp_recovery"]["reason"]["error_code"] == "user_paused"
    resumed = resume_execution(_service(setup), setup.execution_id, "resume-paused")
    assert resumed["execution"]["id"] == setup.execution_id
    assert resumed["queue_job_id"] != "initial-job"


def _suspend(setup, reason=None):
    assert suspend_execution(
        setup.document_id,
        setup.execution_id,
        "initial-job",
        reason
        or {"error_code": "WorkerExited", "phase": "page_extraction", "returncode": 42},
    )
    db.session.expire_all()
    return db.session.get(McpExecution, setup.execution_id), db.session.get(
        ProcessedDocument, setup.document_id
    )


def test_frozen_source_prompt_and_settings_exclude_transient_state(setup):
    execution = db.session.get(McpExecution, setup.execution_id)
    recovery = execution.request_json["recovery"]
    assert recovery["source"]["size"] == len(setup.source.read_bytes())
    assert recovery["prompt_settings"] == {"page_prompt": "frozen"}
    assert "source.pdf" not in str(recovery)
    base = {
        "pdf_options": {"skip_cropping": True},
        "mcp_document_context": {"title": "A"},
    }
    altered = {
        **base,
        "metrics": {"latency": 3},
        "pdf_state": {"html_parts": ["secret"]},
        "progress": 75,
    }
    assert settings_fingerprint(base) == settings_fingerprint(altered)
    assert settings_fingerprint(base) != settings_fingerprint(
        {**base, "mcp_document_context": {"title": "B"}}
    )


def test_settings_fingerprint_tracks_note_arrangement_policy_only():
    legacy = {"mcp_execution_mode": "passive"}
    legacy_fingerprint = settings_fingerprint(legacy)

    assert (
        settings_fingerprint(
            {**legacy, "mcp_native_vision_authoring_policy": "direct-v1"}
        )
        == legacy_fingerprint
    )
    assert (
        settings_fingerprint(
            {**legacy, "mcp_note_arrangement_policy": "source-arrangement-v1"}
        )
        != legacy_fingerprint
    )


def test_settings_fingerprint_tracks_image_accessibility_policy_without_changing_legacy():
    legacy = {"mcp_execution_mode": "passive"}
    legacy_fingerprint = settings_fingerprint(legacy)

    assert legacy_fingerprint == (
        "99e71188f29c5a79374f0527dd10f25d3c7838650764b398d5b775ed5e5c979f"
    )
    assert (
        settings_fingerprint(
            {**legacy, "mcp_image_accessibility_policy": "image-descriptions-v1"}
        )
        != legacy_fingerprint
    )
    assert settings_fingerprint(
        {**legacy, "mcp_image_accessibility_policy": "image-descriptions-v1"}
    ) != settings_fingerprint(
        {**legacy, "mcp_image_accessibility_policy": "legacy-image-accessibility-v0"}
    )


def test_list_assembly_suspension_points_to_repair_instead_of_blind_resume(setup):
    error = worker.ListContinuationError(
        "list-continuation-style-conflict", previous_page=343,
        page_number=344, section="main_content",
    )
    assert worker._suspend_recoverable_failure(
        setup.document_id, {"mcp_execution_id": setup.execution_id},
        "initial-job", error,
    )
    execution = db.session.get(McpExecution, setup.execution_id)
    document = db.session.get(ProcessedDocument, setup.document_id)
    projection = recovery_projection(document, execution)
    assert not projection["resumable"]
    assert projection["reason"] == "assembly_repair_required"
    assert projection["call"] == {
        "tool": "carlina_document_recover",
        "arguments": {"execution_id": execution.id, "operation": "inspect"},
    }
    assert projection["suspension_reason"]["previous_page"] == 343
    with pytest.raises(McpWorkError) as rejected:
        resume_execution(_service(setup), execution.id, "do-not-replay-broken-list")
    assert rejected.value.code == "assembly_repair_required"
    assert execution.status == document.status == "suspended"


def test_suspend_preserves_completed_response_and_resume_replays_same_queue_id(setup):
    work = McpWorkItem(
        execution_id=setup.execution_id,
        request_fingerprint="a" * 64,
        request_json={"page": 1},
        accepted_response_json={"text": "private result"},
        state="completed",
    )
    leased = McpWorkItem(
        execution_id=setup.execution_id,
        request_fingerprint="b" * 64,
        request_json={"page": 2},
        state="leased",
        lease_token="00000000-0000-0000-0000-000000000001",
        lease_owner="worker",
        lease_expires_at=datetime.now(UTC),
    )
    db.session.add_all([work, leased])
    db.session.commit()
    execution, doc = _suspend(setup)
    assert execution.status == doc.status == "suspended"
    assert db.session.get(McpWorkItem, leased.id).state == "pending"
    assert db.session.get(McpWorkItem, leased.id).lease_token is None
    assert doc.settings["mcp_recovery"] == {
        "generation": 1,
        "reason": {
            "error_code": "worker_exited",
            "phase": "page_extraction",
            "returncode": 42,
        },
    }
    projection = recovery_projection(doc, execution)
    assert projection["resumable"] and projection["completed_work_items"] == 1
    assert projection["call"]["tool"] == "carlina_job_resume"
    service = _service(setup)
    first = resume_execution(service, execution.id, "resume-one")
    assert first["resumed"] and not first["idempotent"]
    assert setup.queue.get_status(first["queue_job_id"])["status"] == "queued"
    second = resume_execution(service, execution.id, "resume-one")
    assert second["idempotent"] and second["queue_job_id"] == first["queue_job_id"]
    assert reconcile_resume_dispatch(
        service, db.session.get(McpExecution, execution.id)
    )
    db.session.expire_all()
    assert db.session.get(McpWorkItem, work.id).accepted_response_json == {
        "text": "private result"
    }


def test_dispatch_failure_commits_intent_and_new_session_reconciles(setup, monkeypatch):
    execution, _ = _suspend(setup)
    service = _service(setup)
    original = setup.queue.enqueue_once
    monkeypatch.setattr(
        setup.queue,
        "enqueue_once",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("offline")),
    )
    with pytest.raises(McpWorkError) as error:
        resume_execution(service, execution.id, "retry-key")
    assert error.value.code == "resume_dispatch_pending" and error.value.retryable
    db.session.remove()
    execution = db.session.get(McpExecution, setup.execution_id)
    job_id = execution.queue_job_id
    assert (
        execution.status == "queued"
        and execution.request_json["recovery"]["attempts"][-1]["job_id"] == job_id
    )
    monkeypatch.setattr(setup.queue, "enqueue_once", original)
    assert reconcile_resume_dispatch(service, execution)
    assert setup.queue.get_status(job_id)["status"] == "queued"
    assert (
        resume_execution(service, execution.id, "retry-key")["queue_job_id"] == job_id
    )


def test_source_settings_owner_and_delivery_fences(setup):
    initial_doc = db.session.get(ProcessedDocument, setup.document_id)
    initial_payload = {
        "document_id": initial_doc.id,
        "user_id": 1,
        "mcp_execution_id": setup.execution_id,
    }
    assert validate_delivery(initial_doc, initial_payload, "initial-job", setup.source)
    setup.source.write_bytes(b"changed")
    with pytest.raises(McpWorkError) as delivery_changed:
        validate_delivery(initial_doc, initial_payload, "initial-job", setup.source)
    assert delivery_changed.value.code == "recovery_source_changed"
    setup.source.write_bytes(b"%PDF synthetic original")
    execution, document = _suspend(setup)
    assert not validate_delivery(
        document,
        {"document_id": document.id, "user_id": 1, "mcp_execution_id": execution.id},
        "initial-job",
        None,
    )
    with pytest.raises(McpWorkError) as foreign:
        resume_execution(_service(setup, 2), execution.id, "foreign")
    assert foreign.value.code == "execution_not_found"
    setup.source.write_bytes(b"changed")
    with pytest.raises(McpWorkError) as changed:
        resume_execution(_service(setup), execution.id, "changed-source")
    assert changed.value.code == "recovery_source_changed"
    setup.source.unlink()
    with pytest.raises(McpWorkError) as missing:
        resume_execution(_service(setup), execution.id, "missing-source")
    assert missing.value.code == "source_unavailable"
    setup.source.write_bytes(b"%PDF synthetic original")
    document.settings = {**document.settings, "parsing_model": "another-model"}
    db.session.commit()
    with pytest.raises(McpWorkError) as changed_settings:
        resume_execution(_service(setup), execution.id, "changed-settings")
    assert changed_settings.value.code == "recovery_settings_changed"


def test_historical_key_does_not_reopen_newer_suspension(setup):
    execution, _ = _suspend(setup)
    service = _service(setup)
    first = resume_execution(service, execution.id, "first-generation")
    assert suspend_execution(
        setup.document_id,
        execution.id,
        first["queue_job_id"],
        {"error_code": "recovery_replay_conflict", "batch_id": "batch-1", "page": 3},
    )
    db.session.expire_all()
    execution = db.session.get(McpExecution, setup.execution_id)
    document = db.session.get(ProcessedDocument, setup.document_id)
    assert not recovery_projection(document, execution)["resumable"]
    old = resume_execution(service, execution.id, "first-generation")
    assert old["idempotent"] and old["queue_job_id"] == first["queue_job_id"]
    assert db.session.get(McpExecution, execution.id).status == "suspended"
    with pytest.raises(McpWorkError) as blocked:
        resume_execution(service, execution.id, "new-generation")
    assert blocked.value.code == "recovery_replay_conflict"


def test_deleted_document_and_newer_active_execution_refuse_resume(setup):
    execution, document = _suspend(setup)
    newer = McpExecution(
        owner_user_id=1,
        addon_id="doc_processor",
        resource_kind="document",
        resource_id=document.id,
        mode="passive",
        status="running",
        request_json={},
        created_at=execution.created_at + timedelta(seconds=1),
    )
    db.session.add(newer)
    db.session.commit()
    with pytest.raises(McpWorkError) as conflict:
        resume_execution(_service(setup), execution.id, "newer-active")
    assert conflict.value.code == "newer_execution_active"
    db.session.delete(document)
    db.session.commit()
    with pytest.raises(McpWorkError) as deleted:
        resume_execution(_service(setup), execution.id, "deleted")
    assert deleted.value.code == "document_not_found"


@pytest.mark.parametrize("after", ["completed", "cancelled", "rebound", "deleted"])
def test_lost_response_historical_key_never_dispatches_after_run_changes(
    setup, monkeypatch, after
):
    execution, document = _suspend(setup)
    service = _service(setup)
    first = resume_execution(service, execution.id, "lost-response")
    db.session.refresh(execution)
    db.session.refresh(document)
    if after in {"completed", "cancelled"}:
        execution.status = after
        document.status = after
        document.settings = {
            key: value
            for key, value in document.settings.items()
            if key not in {"mcp_execution_id", "active_job_id"}
        }
    elif after == "rebound":
        successor = McpExecution(
            owner_user_id=1,
            addon_id="doc_processor",
            resource_kind="document",
            resource_id=document.id,
            mode="passive",
            status="queued",
            request_json={},
        )
        db.session.add(successor)
        db.session.flush()
        document.settings = {
            **document.settings,
            "mcp_execution_id": successor.id,
            "active_job_id": "successor-job",
        }
    else:
        db.session.delete(document)
    db.session.commit()
    calls = []
    monkeypatch.setattr(
        setup.queue, "enqueue_once", lambda *_args, **_kwargs: calls.append(True)
    )
    retry = resume_execution(service, execution.id, "lost-response")
    assert retry["idempotent"] and retry["queue_job_id"] == first["queue_job_id"]
    assert calls == []


def test_initial_queued_intent_reconciles_after_producer_crash(setup):
    execution = db.session.get(McpExecution, setup.execution_id)
    document = db.session.get(ProcessedDocument, setup.document_id)
    payload = {
        "document_id": document.id,
        "user_id": 1,
        "mcp_execution_id": execution.id,
        "mcp_execution_mode": "passive",
        "engine_id": "llm_vision",
    }
    recovery = dict(execution.request_json["recovery"])
    recovery["initial_attempt"] = {"job_id": "initial-job", "payload": payload}
    recovery["initial_settings_sha256"] = settings_fingerprint(document.settings)
    execution.request_json = {"recovery": recovery}
    execution.status = "queued"
    db.session.commit()
    assert setup.queue.get_status("initial-job")["status"] == "unknown"
    assert reconcile_resume_dispatch(_service(setup), execution)
    assert setup.queue.get_status("initial-job")["status"] == "queued"
    setup.source.unlink()
    assert not reconcile_resume_dispatch(_service(setup), execution)


def test_missing_resolved_source_is_safe_projection_blocker(setup, monkeypatch):
    execution, document = _suspend(setup)
    monkeypatch.setattr(
        execution_recovery, "resolve_upload_reference", lambda *_args: None
    )
    projection = recovery_projection(document, execution)
    assert not projection["resumable"] and projection["reason"] == "source_unavailable"


def test_renderer_reason_retains_only_bounded_safe_diagnostics(setup):
    _, document = _suspend(
        setup,
        {
            "error_code": "WorkerExited",
            "phase": "page_render",
            "returncode": 2,
            "stderr_bytes": 2_000_000_000,
            "stderr_exception": "FzErrorFormat",
            "attempts": 3,
            "errno": 28,
            "stderr_tail": "private stderr must not persist",
        },
    )
    marker = document.settings["mcp_recovery"]["reason"]
    assert marker["stderr_bytes"] == 1_000_000_000
    assert marker["stderr_exception"] == "FzErrorFormat"
    assert marker["attempts"] == 3
    assert marker["errno"] == 28
    assert "stderr_tail" not in marker


@pytest.mark.parametrize("same_key", [True, False])
def test_independent_context_resume_race(setup, same_key):
    execution, _ = _suspend(setup)
    execution_id = execution.id
    db.session.remove()
    barrier = Barrier(2)

    def attempt(key: str):
        with setup.app.app_context():
            barrier.wait(timeout=10)
            try:
                result = resume_execution(_service(setup), execution_id, key)
                return ("ok", result)
            except McpWorkError as exc:
                return ("error", exc.code)
            finally:
                db.session.remove()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(attempt, "same-key" if same_key else f"key-{index}")
            for index in range(2)
        ]
        results = [future.result(timeout=20) for future in futures]
    if same_key:
        assert [kind for kind, _ in results] == ["ok", "ok"], results
        assert results[0][1]["queue_job_id"] == results[1][1]["queue_job_id"]
    else:
        assert sorted(kind for kind, _ in results) == ["error", "ok"]
        assert next(value for kind, value in results if kind == "error") in {
            "execution_conflict",
            "execution_not_suspended",
        }
