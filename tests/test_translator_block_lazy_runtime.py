"""Lazy tail-planning workflow regression: actual multi-window runs.

Exercises run_document_block_workflow (not just the planner helper):
completion over several windows, passive re-entry after ExternalWorkPending,
late oversized-block repack, duplicate issued scopes failing closed, and
per-pack glossary refresh. Isolated encrypted SQLite; deterministic fake
provider; no live data.
"""
from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from importlib import import_module
from pathlib import Path

import pytest
from content_storage_support import bind_content_policy
from flask import Flask

from carlina.core.semantic_html import SEMANTIC_HTML_CONTRACT_VERSION
from carlina.core.utc import utc_now_db

_ROOT = "iarf" + "_assistant"
_translator = f"{_ROOT}.addons.translator"
_contract = import_module(f"{_translator}.block_contract")
_persistence = import_module(f"{_translator}.block_persistence")
_planner = import_module(f"{_translator}.block_planner")
_workflow_config = import_module(f"{_translator}.block_workflow_config")
_workflow_runtime = import_module(f"{_translator}.block_workflow_runtime")
_window_execution = import_module(f"{_translator}.window_execution")
_working_glossary = import_module(f"{_translator}.working_glossary")
_block_translation = import_module(f"{_translator}.block_translation")
_target_contract = import_module(f"{_translator}.target_contract")
_window_contract = import_module(f"{_translator}.window_contract")
_operations = import_module(f"{_translator}.operations")
_models = import_module(f"{_translator}.models")
_core_models = import_module(f"{_ROOT}.models")
_encryption = import_module(f"{_ROOT}.core.encryption")
_extensions = import_module(f"{_ROOT}.extensions")
_task_queue = import_module(f"{_ROOT}.core.task_queue")

AttestedDocumentSnapshot = _contract.AttestedDocumentSnapshot
DOCUMENT_BLOCK_ENGINE_CONTRACT = _contract.DOCUMENT_BLOCK_ENGINE_CONTRACT
TranslationOperation = _models.TranslationOperation
TranslationProject = _models.TranslationProject
TranslationWindowAttempt = _models.TranslationWindowAttempt
ApiKey = _core_models.ApiKey
LLMModel = _core_models.LLMModel
LLMProvider = _core_models.LLMProvider
User = _core_models.User
db = _extensions.db
BlockContractError = _contract.BlockContractError
ExternalWorkPending = _task_queue.ExternalWorkPending
derive_window_profile = _block_translation.derive_window_profile
structural_fingerprint = _target_contract.structural_fingerprint
TranslationWindow = _window_contract.TranslationWindow


@pytest.fixture
def app(tmp_path: Path):
    application = Flask(__name__, instance_path=str(tmp_path / "instance"))
    bind_content_policy(application)
    application.config.update(
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{tmp_path / 'lazy-runtime.db'}",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SECRET_KEY="lazy-runtime-test",
        TESTING=True,
        SQLALCHEMY_ENGINE_OPTIONS={"connect_args": {"timeout": 30}},
    )
    db.init_app(application)
    context = application.app_context()
    context.push()
    keys = _encryption.InstallationKeyStore(tmp_path / "installation-keys.json")
    keys.initialize()
    application.extensions["carlina_encryption"] = _encryption.EncryptionService(keys)
    db.create_all()
    try:
        yield application
    finally:
        db.session.remove()
        db.engine.dispose()
        context.pop()


class _MultiProvider:
    """Fake provider translating every writable block in the window."""

    def __init__(self):
        self.calls: list[str] = []
        self.translation_windows = 0

    def completion(self, **kwargs):
        source = kwargs["source"]
        self.calls.append(source)
        messages = kwargs["messages"]
        assert source == "translator_block_translation_window"
        content = messages[-1]["content"]
        marker = "\n[writable_source target_language="
        start = content.rfind(marker)
        assert start >= 0
        payload = json.loads(content[content.find("\n", start + 1) + 1:])
        results = []
        for writable in payload["blocks"]:
            text = writable["content"]
            translated = text.replace("protected", "Przekład")
            if translated == text and translated:
                translated = f"Przekład: {text}"
            results.append({
                "n": writable["n"],
                "content": translated,
                "fields": [{**f, "value": f"Przekład: {f['value']}"} for f in writable.get("fields", [])],
            })
        self.translation_windows += 1
        return {"choices": [{"message": {"role": "assistant", "content": json.dumps({"results": results})}}]}


def _state(nparas: int = 6):
    user = User(email="lazy-runtime@example.test", password_hash="x")
    db.session.add(user)
    db.session.flush()
    provider = LLMProvider(user_id=None, name="e2e-provider", provider_kind="native")
    db.session.add(provider)
    db.session.flush()
    db.session.add_all([
        ApiKey(user_id=None, provider_id=provider.id, name="primary",
               api_key="test-only-provider-secret", is_active=True),
        LLMModel(user_id=None, provider_id=provider.id, model_id="e2e-provider/chat",
                 display_name="E2E Chat", is_active=True, manually_added=True,
                 model_type="chat", context_window=1_000_000, max_output_tokens=128_000),
    ])
    project = TranslationProject(
        user_id=user.id, name="Lazy runtime", source_lang="en", target_lang="pl",
        engine_contract=DOCUMENT_BLOCK_ENGINE_CONTRACT,
        settings={"translator_model": "e2e-provider/chat", "source_reference": {
            "version": 1, "capability": "document.processing",
            "provider_addon_id": "doc-provider", "provider_capability_id": "document-processing",
            "resource_type": "document", "resource_id": "lazy-doc"}})
    db.session.add(project)
    db.session.flush()
    html = "<html><body>" + "".join(
        f"<p>Paragraph {i} with packing content.</p>" for i in range(nparas)) + "</body></html>"
    digest = hashlib.sha256(html.encode()).hexdigest()
    plan = _planner.plan_document_blocks(
        AttestedDocumentSnapshot(id="lazy-doc", document_id="lazy-doc", output_html=html,
            content_hash=digest, semantic_content_hash=digest, metadata={}, provenance={},
            captured_at="2026-08-26T00:00:00+00:00",
            semantic_contract_version=SEMANTIC_HTML_CONTRACT_VERSION),
        project_id=project.id,
        source_attestation=_persistence.source_attestation_for_project(project),
        source_language="en")
    record, _ = _persistence.persist_source_plan(project, plan)
    operation = TranslationOperation(
        project_id=project.id, owner_user_id=user.id, kind="document_block_translate",
        idempotency_key="lazy-op", request_fingerprint="e" * 64, state="running",
        queue_job_id="lazy-worker", source_content_hash=record.source_content_hash,
        source_plan_id=record.id, source_plan_hash=record.plan_hash,
        lease_expires_at=utc_now_db() + timedelta(minutes=10))
    db.session.add(operation)
    db.session.commit()
    profile = _window_execution.resolve_execution_profile(project)
    material = _workflow_config.freeze_block_workflow_config(
        operation, record, user=user,
        settings={"translator_model": "e2e-provider/chat", "enable_pre_analysis": False,
                  "enable_research": False, "owner_policy": "Preserve meaning."},
        window_profile=derive_window_profile(profile, maximum_blocks=2).to_dict())
    _workflow_config.persist_block_workflow_config(operation, record, material)
    db.session.commit()
    return user, project, record, operation, plan


def _boundary_recorder():
    calls: list[dict] = []

    def safe_boundary(operation, progress, message, checkpoint):
        calls.append({"progress": progress, "checkpoint": dict(checkpoint)})
        return _operations.update_progress(
            operation, progress=progress, message=message, checkpoint=dict(checkpoint))
    return safe_boundary, calls


def test_lazy_multi_window_completion(app):
    _u, project, record, operation, plan = _state(6)
    provider = _MultiProvider()
    safe_boundary, calls = _boundary_recorder()
    _op, result = _workflow_runtime.run_document_block_workflow(
        project=project, operation=operation, service=provider,
        addon_manager=object(), safe_boundary=safe_boundary)
    assert "block_failure" not in result
    assert result["translated_block_count"] == len(plan.blocks)
    assert provider.translation_windows == 3
    rows = TranslationWindowAttempt.query.filter_by(
        operation_id=operation.id, source_plan_id=record.id).all()
    assert sorted(r.window_order for r in rows) == [1, 2, 3]
    assert calls, "expected per-window checkpoints"
    assert all("window_count" not in c["checkpoint"] for c in calls)
    progresses = [c["progress"] for c in calls]
    assert all(b > a for a, b in zip(progresses, progresses[1:]))
    assert calls[-1]["checkpoint"]["remaining_blocks"] == 0
    assert calls[-1]["checkpoint"]["coverage_complete"] is True


def test_lazy_passive_reentry_completes(app, monkeypatch):
    _u, project, record, operation, plan = _state(6)
    # Explicitly emulate the real passive-execution context predicate the
    # production re-entry fence reads. Production code is unchanged: outside
    # passive mode the same parked attempt must still fail closed with
    # window-dispatch-replay (see the closed-fence assertion below).
    monkeypatch.setattr(_workflow_runtime, "passive_execution_active", lambda: True)
    provider = _MultiProvider()
    state = {"parked": False}
    orig_completion = provider.completion

    def parking_completion(**kw):
        if kw.get("source") == "translator_block_translation_window" and not state["parked"]:
            state["parked"] = True
            raise ExternalWorkPending("park", delay_seconds=1, work_item_id="w1")
        return orig_completion(**kw)

    provider.completion = parking_completion
    safe_boundary, _ = _boundary_recorder()
    with pytest.raises(ExternalWorkPending):
        _workflow_runtime.run_document_block_workflow(
            project=project, operation=operation, service=provider,
            addon_manager=object(), safe_boundary=safe_boundary)
    db.session.rollback()
    db.session.expire_all()
    operation = db.session.get(TranslationOperation, operation.id)
    safe_boundary2, _ = _boundary_recorder()
    _op2, result = _workflow_runtime.run_document_block_workflow(
        project=project, operation=operation, service=provider,
        addon_manager=object(), safe_boundary=safe_boundary2)
    assert "block_failure" not in result
    assert result["translated_block_count"] == len(plan.blocks)
    rows = TranslationWindowAttempt.query.filter_by(
        operation_id=operation.id, source_plan_id=record.id).all()
    assert sorted(r.window_order for r in rows) == [1, 2, 3]


def test_lazy_reentry_stays_closed_outside_passive_mode(app):
    """The production fence is not loosened for the fixture above."""
    _u, project, record, operation, plan = _state(2)
    provider = _MultiProvider()
    state = {"parked": False}
    orig_completion = provider.completion

    def parking_completion(**kw):
        if kw.get("source") == "translator_block_translation_window" and not state["parked"]:
            state["parked"] = True
            raise ExternalWorkPending("park", delay_seconds=1, work_item_id="w1")
        return orig_completion(**kw)

    provider.completion = parking_completion
    safe_boundary, _ = _boundary_recorder()
    with pytest.raises(ExternalWorkPending):
        _workflow_runtime.run_document_block_workflow(
            project=project, operation=operation, service=provider,
            addon_manager=object(), safe_boundary=safe_boundary)
    db.session.rollback()
    db.session.expire_all()
    operation = db.session.get(TranslationOperation, operation.id)
    # Real predicate (unpatched): re-entry of the parked attempt must fail closed.
    with pytest.raises(BlockContractError) as exc:
        _workflow_runtime.run_document_block_workflow(
            project=project, operation=operation, service=provider,
            addon_manager=object(), safe_boundary=safe_boundary)
    assert exc.value.code == "window-dispatch-replay"


def test_lazy_late_oversize_repack(app, monkeypatch):
    _u, project, record, operation, plan = _state(6)
    provider = _MultiProvider()
    real_build = _workflow_runtime.build_window_material
    real_repack = _workflow_runtime.plan_unissued_tail_materials
    calls = {"fails": 0, "repacks": 0}

    def flaky_build(**kw):
        window = kw.get("window")
        if (kw.get("include_continuity") and window is not None
                and window.order == 2 and len(window.block_ids) > 1 and calls["fails"] == 0):
            calls["fails"] += 1
            raise BlockContractError("window-oversized-block")
        return real_build(**kw)

    def shrinking_repack(**kw):
        calls["repacks"] += 1
        if kw.get("start_order") == 2:
            execution = _window_execution.resolve_execution_profile(project)
            narrow = derive_window_profile(execution, maximum_blocks=1)
            return real_repack(**{**kw, "window_profile": narrow})
        return real_repack(**kw)

    monkeypatch.setattr(_workflow_runtime, "build_window_material", flaky_build)
    monkeypatch.setattr(_workflow_runtime, "plan_unissued_tail_materials", shrinking_repack)
    safe_boundary, _ = _boundary_recorder()
    _op, result = _workflow_runtime.run_document_block_workflow(
        project=project, operation=operation, service=provider,
        addon_manager=object(), safe_boundary=safe_boundary)
    assert calls["fails"] == 1 and calls["repacks"] == 1
    assert "block_failure" not in result
    assert result["translated_block_count"] == len(plan.blocks)


def test_lazy_duplicate_issued_scope_fails_closed(app, monkeypatch):
    _u, project, record, operation, plan = _state(4)
    provider = _MultiProvider()
    b = [block.id for block in plan.blocks]
    fp = {block.id: structural_fingerprint(plan, block) for block in plan.blocks}
    w1 = TranslationWindow(1, (b[0], b[1]), (fp[b[0]], fp[b[1]]))
    w2 = TranslationWindow(2, (b[1], b[2]), (fp[b[1]], fp[b[2]]))  # overlaps w1 on b[1]
    monkeypatch.setattr(
        _workflow_runtime, "_durable_issued_windows",
        lambda *, operation, source_plan: ([w1, w2], frozenset({b[0], b[1], b[2]}), 2))
    safe_boundary, _ = _boundary_recorder()
    with pytest.raises(BlockContractError) as exc:
        _workflow_runtime.run_document_block_workflow(
            project=project, operation=operation, service=provider,
            addon_manager=object(), safe_boundary=safe_boundary)
    assert exc.value.code == "block-window-issued-scope"
    assert provider.calls == []


def test_lazy_glossary_refresh_per_pack(app, monkeypatch):
    _u, project, record, operation, plan = _state(6)
    provider = _MultiProvider()
    real_snapshot = _working_glossary.snapshot_current_glossary_entries
    completed_windows_at_read = []

    def counting_snapshot(source_plan):
        completed_windows_at_read.append(provider.translation_windows)
        return real_snapshot(source_plan)

    monkeypatch.setattr(_working_glossary, "snapshot_current_glossary_entries", counting_snapshot)
    safe_boundary, _ = _boundary_recorder()
    _op, result = _workflow_runtime.run_document_block_workflow(
        project=project, operation=operation, service=provider,
        addon_manager=object(), safe_boundary=safe_boundary)
    assert result["translated_block_count"] == len(plan.blocks)
    # One initial snapshot, then one fresh read before each window. The
    # completed-window sequence proves reads happen after prior submissions,
    # not as a burst before translating. Fit and material share that read.
    assert provider.translation_windows == 3
    assert completed_windows_at_read == [0, 0, 1, 2]
