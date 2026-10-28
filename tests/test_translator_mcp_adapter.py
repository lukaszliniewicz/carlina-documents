from __future__ import annotations

import hashlib
import uuid
from contextlib import contextmanager, nullcontext
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from content_storage_support import bind_content_policy
from flask import Flask
from flask_login import LoginManager, login_user, logout_user

from carlina.core.semantic_html import SEMANTIC_HTML_CONTRACT_VERSION
from iarf_assistant import extensions, models
from iarf_assistant.addons.translator import api_client, operations, worker
from iarf_assistant.addons.translator import models as translator_models
from iarf_assistant.addons.translator.block_contract import AttestedDocumentSnapshot
from iarf_assistant.addons.translator.block_persistence import (
    persist_source_plan,
    source_attestation_for_project,
)
from iarf_assistant.addons.translator.block_planner import plan_document_blocks
from iarf_assistant.addons.translator.block_workspace_api import (
    BlockWorkspaceIntegrityError,
    persist_human_glossary_entry,
)
from iarf_assistant.addons.translator.scholarly_review import (
    ReviewActor,
    submit_research_dossier,
)
from iarf_assistant.core import task_queue
from iarf_assistant.core.encryption import EncryptionService, InstallationKeyStore
from iarf_assistant.core.mcp_completion import build_passive_packet
from iarf_assistant.core.mcp_jobs import (
    bind_execution_queue_job,
    claim_next_work,
    create_execution,
    get_or_create_work,
    issue_access_key,
    resolve_access_key,
    submit_work,
    transition_execution,
)
from iarf_assistant.core.utc import utc_now_db

db = extensions.db
User = models.User
ApiKey = models.ApiKey
LLMModel = models.LLMModel
LLMProvider = models.LLMProvider
TranslationOperation = translator_models.TranslationOperation
TranslationGlossaryActive = translator_models.TranslationGlossaryActive
TranslationProject = translator_models.TranslationProject

EXECUTOR_PROFILE = {
    "schema_version": "carlina.external-executor.v1",
    "max_input_tokens": 32_000,
    "max_output_tokens": 4_000,
    "input_modalities": ["text"],
    "supports_tools": False,
    "supports_structured_output": True,
    "supports_reasoning": True,
    "user_confirmed": True,
}


class _Queue:
    def __init__(self):
        self.enqueued = []
        self.cancelled = []

    def enqueue(self, handler, payload):
        job_id = f"job-{len(self.enqueued) + 1}"
        self.enqueued.append((handler, payload))
        return job_id

    def request_cancel(self, job_id, requested_by=None, reason=None):
        self.cancelled.append((job_id, requested_by, reason))
        return {"status": "cancelling", "cancel_requested": True}


class _Context:
    def __init__(self, queue=None):
        self.queue = queue or _Queue()

    def get_task_queue(self, addon_id):
        assert addon_id == "translator"
        return self.queue


@pytest.fixture
def app(tmp_path):
    application = Flask(__name__, instance_path=str(tmp_path / "instance"))
    bind_content_policy(application)
    application.config.update(
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{tmp_path / 'translator-mcp.db'}",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SECRET_KEY="translator-mcp-test",
        TESTING=True,
    )
    db.init_app(application)
    login_manager = LoginManager()
    login_manager.init_app(application)

    @login_manager.user_loader
    def load_user(user_id):
        return db.session.get(User, int(user_id))

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
        with db.engine.begin() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            db.metadata.drop_all(connection)
        db.engine.dispose()
        context.pop()


def _user(email: str) -> User:
    user = User(email=email, password_hash="test")
    db.session.add(user)
    db.session.commit()
    return user


def _ready_chat_model(user: User) -> str:
    provider = LLMProvider(
        user_id=user.id,
        name=f"translator_mcp_{user.id}",
        display_name="Translator MCP test provider",
    )
    db.session.add(provider)
    db.session.flush()
    model_id = f"translator_mcp_{user.id}/chat"
    db.session.add_all(
        [
            ApiKey(
                user_id=user.id,
                provider_id=provider.id,
                name="Test",
                api_key="test-secret",  # pragma: allowlist secret
                is_active=True,
            ),
            LLMModel(
                user_id=user.id,
                provider_id=provider.id,
                model_id=model_id,
                display_name="Translator MCP test chat",
                is_active=True,
                model_type="chat",
            ),
        ]
    )
    db.session.commit()
    return model_id


def _project(user: User, *, settings=None, status="setup") -> TranslationProject:
    project = TranslationProject(
        user_id=user.id,
        name="MCP project",
        source_lang="en",
        target_lang="pl",
        status=status,
        settings=settings,
    )
    db.session.add(project)
    db.session.flush()
    return project


def _block_project_and_plan(
    user: User,
    *,
    source_html: str = "<html><body><p>Dao is a term.</p><p>Second source.</p></body></html>",
) -> tuple[TranslationProject, object, list[object]]:
    source_document_id = f"doc-{user.id}"
    source_hash = hashlib.sha256(source_html.encode()).hexdigest()
    project = TranslationProject(
        user_id=user.id,
        name="MCP block project",
        source_lang="en",
        target_lang="pl",
        status="setup",
        engine_contract="document-block-v1",
        document_id=source_document_id,
        source_content_hash=source_hash,
        settings={
            "source_type": "document",
            "source_reference": {
                "version": 1,
                "capability": "document.processing",
                "provider_addon_id": "doc-provider",
                "provider_capability_id": "document-processing",
                "resource_type": "document",
                "resource_id": source_document_id,
            },
        },
    )
    db.session.add(project)
    db.session.flush()
    snapshot = AttestedDocumentSnapshot(
        id=source_document_id,
        document_id=source_document_id,
        output_html=source_html,
        content_hash=source_hash,
        semantic_content_hash=source_hash,
        metadata={},
        provenance={},
        captured_at="2026-09-07T12:00:00+00:00",
        semantic_contract_version=SEMANTIC_HTML_CONTRACT_VERSION,
    )
    plan = plan_document_blocks(
        snapshot,
        project_id=project.id,
        source_attestation=source_attestation_for_project(project),
        source_language="en",
    )
    record, _ = persist_source_plan(project, plan)
    db.session.commit()
    return project, record, list(plan.blocks)


def _block_operation(
    project: TranslationProject,
    record: Any,
    *,
    key: str,
    state: str = "running",
) -> TranslationOperation:
    operation, created = operations.create_operation(
        project,
        kind="document_block_translate",
        request_payload={"operation": key},
        idempotency_key=key,
        source_content_hash=record.source_content_hash,
        source_plan_id=record.id,
        source_plan_hash=record.plan_hash,
    )
    assert created is True
    operations.claim_operation(operation.id, f"job-{key}")
    if state != "running":
        operations.finish_operation(operation, state)
    db.session.commit()
    return operation


def _persist_owner_glossary(
    app: Flask,
    project: TranslationProject,
    owner: User,
    *,
    source_term: str,
    target_term: str,
    notes: str | None,
) -> dict:
    with app.test_request_context("/block-glossary"):
        login_user(owner)
        try:
            return persist_human_glossary_entry(
                project,
                source_term=source_term,
                target_term=target_term,
                notes=notes,
                author_user_id=owner.id,
            )
        finally:
            logout_user()


def _block_export_operation(
    project: TranslationProject, *, key: str, state: str = "completed"
) -> TranslationOperation:
    operation, created = operations.create_operation(
        project,
        kind="document_block_export",
        request_payload={"operation": key},
        idempotency_key=key,
    )
    assert created is True
    operations.claim_operation(operation.id, f"job-{key}")
    if state != "running":
        operations.finish_operation(operation, state)
    db.session.commit()
    return operation


def test_execution_mode_defaults_and_passive_settings_are_provider_neutral(app):
    owner = _user("mode-owner@example.test")
    model_id = _ready_chat_model(owner)
    client = api_client.TranslatorClient(_Context(), {"current_user_id": owner.id})

    managed = client._settings(
        translator_model=model_id,
        enable_pre_analysis=False,
    )
    passive = client._settings(
        enable_pre_analysis=False,
        execution_mode="passive",
    )

    assert managed["execution_mode"] == "managed"
    assert managed["translator_model"] == model_id
    assert passive["execution_mode"] == "passive"
    assert passive["translator_model"] == ""
    with pytest.raises(ValueError, match="execution_mode"):
        client._settings(
            translator_model=model_id,
            enable_pre_analysis=False,
            execution_mode="external_worker",
        )


def test_annotation_profile_persists_explicit_study_and_maps_legacy_notes(app):
    owner = _user("annotation-profile-owner@example.test")
    client = api_client.TranslatorClient(_Context(), {"current_user_id": owner.id})

    explicit = client._settings(execution_mode="passive", annotation_profile="study")
    assert explicit["annotation_profile"] == "study"
    assert explicit["enable_translator_notes"] is True

    legacy_enabled = client._settings(
        execution_mode="passive", enable_translator_notes=True
    )
    assert legacy_enabled["annotation_profile"] == "restrained"
    assert legacy_enabled["enable_translator_notes"] is True

    legacy_disabled = client._settings(
        execution_mode="passive", enable_translator_notes=False
    )
    assert legacy_disabled["annotation_profile"] == "none"
    assert legacy_disabled["enable_translator_notes"] is False

    with pytest.raises(ValueError):
        client._settings(execution_mode="passive", annotation_profile="invalid")


def test_project_creation_normalizes_language_names_before_idempotency(app):
    owner = _user("language-normalization-owner@example.test")
    client = api_client.TranslatorClient(_Context(), {"current_user_id": owner.id})

    first = client._new_project(
        name="Named languages",
        source_lang="Hebrew",
        target_lang="English",
        settings={},
        persist=False,
    )
    first, created = client._create_project(
        first,
        idempotency_key="named-language-project",
        request_payload={
            "name": first.name,
            "source_lang": first.source_lang,
            "target_lang": first.target_lang,
        },
    )
    assert created is True
    assert first.source_lang == "he"
    assert first.target_lang == "en"

    replay = client._new_project(
        name="Named languages",
        source_lang="he",
        target_lang="en",
        settings={},
        persist=False,
    )
    replay, created = client._create_project(
        replay,
        idempotency_key="named-language-project",
        request_payload={
            "name": replay.name,
            "source_lang": replay.source_lang,
            "target_lang": replay.target_lang,
        },
    )
    assert created is False
    assert replay.id == first.id

    for field, values in {
        "source_lang": ("not a language", "en"),
        "target_lang": ("he", "not a language"),
    }.items():
        with pytest.raises(ValueError, match=field):
            client._new_project(
                name="Invalid language",
                source_lang=values[0],
                target_lang=values[1],
                settings={},
                persist=False,
            )


def test_project_mcp_projection_never_returns_legacy_bearer_token(app):
    owner = _user("legacy-token-projection@example.test")
    project = _project(owner)
    project.share_token = "legacy-bearer-secret"
    db.session.commit()

    projection = api_client.TranslatorClient._project_payload(project)

    assert "share_token" not in projection
    assert "legacy-bearer-secret" not in repr(projection)


def test_passive_mode_needs_no_model_profile_but_managed_still_does(app):
    owner = _user("external-model-owner@example.test")
    client = api_client.TranslatorClient(_Context(), {"current_user_id": owner.id})

    settings = client._settings(
        execution_mode="passive",
    )
    assert settings["execution_mode"] == "passive"
    assert settings["enable_pre_analysis"] is False
    assert all(
        settings[key] == ""
        for key in (
            "translator_model",
            "analyzer_model",
            "researcher_model",
        )
    )
    assert db.session.query(LLMModel).count() == 0

    with pytest.raises(ValueError, match="translator model"):
        client._settings(
            enable_pre_analysis=False,
            execution_mode="managed",
        )
    with pytest.raises(ValueError, match="separately supported execution"):
        client._settings(
            enable_pre_analysis=True,
            execution_mode="passive",
        )


def test_set_mcp_execution_is_owner_scoped_and_refuses_active_work(app):
    owner = _user("binding-owner@example.test")
    other = _user("binding-other@example.test")
    project = _project(owner)
    db.session.commit()
    execution_id = str(uuid.uuid4())
    client = api_client.TranslatorClient(_Context(), {"current_user_id": owner.id})

    result = client.set_mcp_execution(
        project.id,
        execution_id,
        mode="passive",
        executor_profile=EXECUTOR_PROFILE,
    )
    assert result == {
        "project_id": project.id,
        "mcp_execution_id": execution_id,
        "execution_mode": "passive",
        "executor_profile": EXECUTOR_PROFILE,
    }
    assert db.session.get(TranslationProject, project.id).settings == {
        "mcp_execution_id": execution_id,
        "execution_mode": "passive",
        "executor_profile": EXECUTOR_PROFILE,
    }

    with pytest.raises(LookupError, match="not found"):
        api_client.TranslatorClient(
            _Context(), {"current_user_id": other.id}
        ).set_mcp_execution(project.id, str(uuid.uuid4()), mode="managed")

    operation, _ = operations.create_operation(
        project,
        kind="translate",
        request_payload={},
        idempotency_key="active-binding",
    )
    db.session.commit()
    with pytest.raises(RuntimeError, match="work in progress"):
        client.set_mcp_execution(project.id, str(uuid.uuid4()), mode="managed")
    db.session.refresh(project)
    assert project.settings["execution_mode"] == "passive"
    assert operation.state == "queued"


@pytest.mark.parametrize(
    ("operation_state", "mode", "expected"),
    [
        ("queued", "managed", "queued"),
        ("running", "managed", "running"),
        ("running", "passive", "running"),
        ("completed", "passive", "completed"),
        ("partial", "passive", "failed"),
        ("partial", "managed", "failed"),
        ("failed", "passive", "failed"),
        ("cancelled", "passive", "cancelled"),
        ("cancel_requested", "passive", "cancelled"),
    ],
)
def test_mcp_projection_maps_states_and_redacts_private_fields(
    app, operation_state, mode, expected
):
    owner = _user(f"projection-{operation_state}-{mode}@example.test")
    execution_id = str(uuid.uuid4())
    project = _project(
        owner,
        settings={
            "execution_mode": mode,
            "mcp_execution_id": execution_id,
            "custom_prompt": "private prompt",
        },
    )
    operation = TranslationOperation(
        project_id=project.id,
        owner_user_id=owner.id,
        kind="translate",
        idempotency_key=f"projection-{operation_state}-{mode}",
        request_fingerprint="a" * 64,
        queue_job_id="job-projection",
        state=operation_state,
        message="safe status",
        source_content_hash="b" * 64,
        error_detail="private source and /private/path",
        cancel_requested_at=utc_now_db()
        if operation_state in {"cancel_requested", "cancelled"}
        else None,
        started_at=utc_now_db() if operation_state == "running" else None,
        finished_at=utc_now_db()
        if operation_state in {"completed", "partial", "failed", "cancelled"}
        else None,
    )
    db.session.add(operation)
    db.session.commit()

    projection = api_client.TranslatorClient(
        _Context(), {"current_user_id": owner.id}
    ).mcp_job_projection(project.id)

    assert projection["status"] == expected
    assert projection["queue_job_id"] == "job-projection"
    assert projection["latest_operation"]["state"] == (
        "partial" if operation_state == "partial" else expected
    )
    serialized = repr(projection)
    for private in ("source_snapshot", "source_content_hash", "custom_prompt"):
        assert private not in serialized
    assert "private source" not in serialized
    assert "/private/path" not in serialized
    assert "error_detail" not in serialized


def test_partial_project_without_operation_is_not_mcp_success(app):
    owner = _user("partial-project@example.test")
    project = _project(owner, status="partial")
    db.session.commit()

    projection = api_client.TranslatorClient(
        _Context(), {"current_user_id": owner.id}
    ).mcp_job_projection(project.id, include_output=True)

    assert projection["state"] == projection["status"] == "failed"
    assert projection["latest_operation"] is None
    assert projection["output"] == {
        "state": "unavailable",
        "reason": "translation_not_complete",
    }


def _pending_translation_handoff():
    owner = _user("finalizing-passive@example.test")
    project = _project(owner, status="translating")
    execution = create_execution(
        owner.id,
        "translator",
        "project",
        project.id,
        {"mode": "passive", "executor_profile": EXECUTOR_PROFILE},
        mode="passive",
    )
    bind_execution_queue_job(execution, "projection-job")
    project.settings = {
        "execution_mode": "passive",
        "mcp_execution_id": execution.id,
    }
    operation = TranslationOperation(
        project_id=project.id,
        owner_user_id=owner.id,
        kind="document_block_translate",
        idempotency_key="finalizing-passive",
        request_fingerprint="a" * 64,
        queue_job_id="projection-job",
        state="running",
        source_content_hash="b" * 64,
    )
    db.session.add(operation)
    packet = build_passive_packet(
        executor_profile=EXECUTOR_PROFILE,
        workload_role="translation",
        messages=[{"role": "user", "content": "Synthetic finalization probe."}],
        tools=None,
        response_format=None,
        parameters={},
        source="finalization-projection-probe",
    )
    db.session.commit()
    resolution = get_or_create_work(execution.id, packet)
    transition_execution(execution.id, "waiting_external")
    db.session.commit()
    client = api_client.TranslatorClient(_Context(), {"current_user_id": owner.id})
    return owner, project, operation, execution, resolution.work_item_id, client


def test_passive_projection_waits_only_until_external_response_is_accepted(app):
    owner, project, operation, execution, work_id, client = _pending_translation_handoff()
    assert client.mcp_job_projection(project.id)["state"] == "waiting_external"
    issued = issue_access_key(owner.id, "projection probe", ["mcp.work", "mcp.translator"], [])
    principal = resolve_access_key(issued.raw_token)
    claim = claim_next_work(principal, "projection-probe", execution_id=execution.id)
    assert claim is not None and claim.work_item_id == work_id
    assert client.mcp_job_projection(project.id)["state"] == "waiting_external"
    submitted = submit_work(
        principal,
        work_id,
        claim.lease_token,
        {"content": "Synthetic target.", "finish_reason": "stop"},
    )
    assert submitted["status"] == "accepted"
    db.session.commit()

    # The queue has not yet consumed the handoff. External work is already done;
    # staging/finalization belongs to Carlina rather than another model worker.
    db.session.refresh(execution)
    assert execution.status == "waiting_external"
    assert client.mcp_job_projection(project.id)["state"] == "running"
    operation.state = "failed"
    operation.error_detail = "private finalization diagnostic"
    db.session.commit()
    failed = client.mcp_job_projection(project.id)
    assert failed["state"] == "failed"
    assert "private finalization" not in repr(failed)
    assert db.session.get(models.McpWorkItem, work_id).accepted_response_json["content"] == "Synthetic target."


@pytest.mark.parametrize("historical_state", ["waiting_external", "completed", "failed", "cancelled"])
def test_execution_projection_preserves_earlier_run_after_project_reuse(app, historical_state):
    owner, project, operation, execution, _work_id, client = _pending_translation_handoff()
    if historical_state != "waiting_external":
        transition_execution(execution.id, historical_state)
    newer = create_execution(
        owner.id, "translator", "project", project.id,
        {"mode": "managed"}, mode="managed",
    )
    bind_execution_queue_job(newer, "newer-projection-job")
    project.settings = {"execution_mode": "managed", "mcp_execution_id": newer.id}
    project.status = "ready"
    latest = TranslationOperation(
        project_id=project.id, owner_user_id=owner.id, kind="document_block_translate",
        idempotency_key="newer-projection-operation", request_fingerprint="c" * 64,
        queue_job_id="newer-projection-job", state="completed", source_content_hash="d" * 64,
        created_at=utc_now_db(),
    )
    db.session.add(latest)
    db.session.commit()

    projection = client.mcp_job_projection(
        project.id, execution_id=execution.id, include_output=True,
        include_glossary=True, include_export_metadata=True,
    )
    assert projection["execution_id"] == execution.id
    assert projection["state"] == projection["status"] == historical_state
    assert projection["latest_operation"]["id"] == operation.id
    assert projection["queue_job_id"] == "projection-job"
    assert projection["output"]["state"] == "unavailable"
    assert projection["output"]["reason"] == "execution_not_current"
    assert {"translation_progress", "batch_settings", "glossary", "export_metadata"}.isdisjoint(projection)
    assert client.mcp_job_projection(project.id, execution_id=newer.id)["latest_operation"]["id"] == latest.id
    assert client.mcp_job_projection(project.id)["state"] == "completed"


def test_unbound_execution_does_not_adopt_current_project_completion(app):
    owner, project, _operation, _execution, _work_id, client = _pending_translation_handoff()
    unbound = create_execution(
        owner.id, "translator", "project", project.id, {"mode": "managed"}, mode="managed",
    )
    project.status = "ready"
    db.session.commit()
    projection = client.mcp_job_projection(project.id, execution_id=unbound.id)
    assert projection["state"] == unbound.status == "queued"
    assert projection["latest_operation"] is None
    assert projection["queue_job_id"] is None


def test_execution_projection_rejects_another_owner(app):
    owner, project, _operation, execution, _work_id, _client = _pending_translation_handoff()
    foreign_owner = _user("foreign-projection@example.test")
    foreign_project = _project(foreign_owner)
    db.session.commit()
    client = api_client.TranslatorClient(_Context(), {"current_user_id": foreign_owner.id})
    with pytest.raises(LookupError, match="MCP execution was not found"):
        client.mcp_job_projection(foreign_project.id, execution_id=execution.id)
    assert execution.owner_user_id == owner.id


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("owner_user_id", None),
        ("queue_job_id", "earlier-queue-job"),
        ("resource_id", "another-project"),
        ("resource_kind", "another-resource-kind"),
        ("addon_id", "doc_processor"),
        ("mode", "managed"),
        ("status", "failed"),
    ],
)
def test_passive_projection_does_not_adopt_foreign_or_stale_pending_work(app, field, value):
    _owner, project, _operation, execution, _work_id, client = _pending_translation_handoff()
    if field == "owner_user_id":
        value = _user("foreign-handoff@example.test").id
    setattr(execution, field, value)
    db.session.commit()
    assert client.mcp_job_projection(project.id)["state"] == "running"


def test_mcp_projection_output_uses_completed_translation_after_export(
    app, monkeypatch
):
    owner = _user("output-after-export@example.test")
    project, record, _blocks = _block_project_and_plan(owner)
    translation = _block_operation(
        project, record, key="output-translation", state="completed"
    )
    _block_export_operation(project, key="output-export")
    rendered = "<html><body><p>Verified output.</p></body></html>"
    renderer = MagicMock(return_value=rendered)
    monkeypatch.setattr(api_client, "render_persisted_target_document", renderer)

    projection = api_client.TranslatorClient(
        _Context(), {"current_user_id": owner.id}
    ).mcp_job_projection(project.id, include_output=True)

    assert projection["latest_operation"]["kind"] == "document_block_export"
    assert projection["output"] == {
        "state": "available",
        "format": "html",
        "mimetype": "text/html",
        "content": rendered,
    }
    renderer.assert_called_once_with(record)
    assert translation.state == "completed"


def test_mcp_projection_output_rejects_later_incomplete_translation(app, monkeypatch):
    owner = _user("output-incomplete-later@example.test")
    project, record, _blocks = _block_project_and_plan(owner)
    _block_operation(project, record, key="output-first", state="completed")
    _block_export_operation(project, key="output-export")
    renderer = MagicMock(return_value="<html><body>verified</body></html>")
    monkeypatch.setattr(api_client, "render_persisted_target_document", renderer)
    _block_operation(project, record, key="output-later", state="running")

    projection = api_client.TranslatorClient(
        _Context(), {"current_user_id": owner.id}
    ).mcp_job_projection(project.id, include_output=True)

    assert projection["output"] == {
        "state": "unavailable",
        "reason": "translation_not_complete",
    }
    renderer.assert_not_called()


def test_mcp_projection_output_rejects_translation_from_stale_plan(
    app, monkeypatch
):
    owner = _user("output-stale-plan@example.test")
    project, record, _blocks = _block_project_and_plan(owner)
    _block_operation(project, record, key="output-stale", state="completed")
    _block_export_operation(project, key="output-stale-export")
    renderer = MagicMock(return_value="<html><body>stale</body></html>")
    monkeypatch.setattr(api_client, "render_persisted_target_document", renderer)
    monkeypatch.setattr(
        api_client,
        "load_block_source_plan",
        lambda _project: SimpleNamespace(record=SimpleNamespace(id="successor-plan")),
    )

    projection = api_client.TranslatorClient(
        _Context(), {"current_user_id": owner.id}
    ).mcp_job_projection(project.id, include_output=True)

    assert projection["output"] == {
        "state": "unavailable",
        "reason": "verified_target_unavailable",
    }
    renderer.assert_not_called()


def test_mcp_projection_glossary_is_opt_in_and_owner_scoped(app):
    owner = _user("glossary-projection-owner@example.test")
    project, _record, blocks = _block_project_and_plan(owner)
    _persist_owner_glossary(
        app,
        project,
        owner,
        source_term="Dao",
        target_term="Dao PL",
        notes="Owner decision.",
    )
    submit_research_dossier(
        project,
        client_submission_id="glossary-dossier-1",
        payload={
            "terminology": [
                {
                    "source_term": "signal",
                    "target_term": "sygnał",
                    "notes": "Keep distinct from sign.",
                    "source_block_ids": [blocks[0].id],
                    "confidence": 0.86,
                }
            ]
        },
        provenance={"method": "external-review"},
        scope={"kind": "blocks", "block_ids": [blocks[0].id, blocks[1].id]},
        actor=ReviewActor("owner", owner.id),
    )

    client = api_client.TranslatorClient(_Context(), {"current_user_id": owner.id})
    default_projection = client.mcp_job_projection(project.id)
    assert "glossary" not in default_projection

    projected = client.mcp_job_projection(project.id, include_glossary=True)
    assert projected["glossary"]["entries"][0]["source_term"] == "Dao"
    assert projected["glossary"]["entries"][0]["target_term"] == "Dao PL"
    assert [item["source_term"] for item in projected["glossary"]["suggestions"]] == [
        "signal"
    ]
    assert projected["glossary"]["suggestions"][0]["state"] == "pending"
    assert TranslationGlossaryActive.query.count() == 1

    other = _user("glossary-projection-other@example.test")
    with pytest.raises(LookupError, match="Translation project not found"):
        api_client.TranslatorClient(
            _Context(), {"current_user_id": other.id}
        ).mcp_job_projection(project.id, include_glossary=True)


def test_mcp_projection_glossary_retains_delta_dossiers_idempotently(app):
    owner = _user("glossary-delta-owner@example.test")
    project, _record, blocks = _block_project_and_plan(owner)
    client = api_client.TranslatorClient(_Context(), {"current_user_id": owner.id})

    first = submit_research_dossier(
        project,
        client_submission_id="delta-1",
        payload={
            "terminology": [
                {
                    "source_term": "signal",
                    "target_term": "sygnał",
                    "notes": None,
                    "source_block_ids": [blocks[0].id],
                    "confidence": None,
                }
            ]
        },
        provenance={},
        scope={"kind": "blocks", "block_ids": [blocks[0].id, blocks[1].id]},
        actor=ReviewActor("owner", owner.id),
    )
    second_payload = {
        "terminology": [
            {
                "source_term": "concept",
                "target_term": "pojęcie",
                "notes": "Use the technical register.",
                "source_block_ids": [blocks[1].id],
                "confidence": 0.7,
            }
        ]
    }
    second = submit_research_dossier(
        project,
        client_submission_id="delta-2",
        payload=second_payload,
        provenance={},
        scope={"kind": "blocks", "block_ids": [blocks[0].id, blocks[1].id]},
        actor=ReviewActor("owner", owner.id),
        dossier_id=first["dossier_id"],
        expected_revision=1,
    )
    retried = submit_research_dossier(
        project,
        client_submission_id="delta-2",
        payload=second_payload,
        provenance={},
        scope={"kind": "blocks", "block_ids": [blocks[0].id, blocks[1].id]},
        actor=ReviewActor("owner", owner.id),
        dossier_id=first["dossier_id"],
        expected_revision=1,
    )

    assert retried["revision_id"] == second["revision_id"]
    glossary = client.mcp_job_projection(project.id, include_glossary=True)["glossary"]
    assert sorted(item["source_term"] for item in glossary["suggestions"]) == [
        "concept",
        "signal",
    ]
    assert len(glossary["suggestions"]) == 2


@pytest.mark.parametrize("terminal_state", ["completed", "partial", "cancelled"])
def test_mcp_projection_glossary_survives_operation_transitions_and_retry(
    app, terminal_state
):
    owner = _user(f"glossary-lifecycle-{terminal_state}@example.test")
    project, record, _blocks = _block_project_and_plan(owner)
    _persist_owner_glossary(
        app,
        project,
        owner,
        source_term="Dao",
        target_term="Dao PL",
        notes="Stable owner lock.",
    )
    client = api_client.TranslatorClient(_Context(), {"current_user_id": owner.id})
    submit_research_dossier(
        project,
        client_submission_id="lifecycle-suggestion",
        payload={"terminology": [{
            "source_term": "signal",
            "target_term": "sygnał",
            "notes": "Advisory choice retained with the project.",
            "source_block_ids": [_blocks[0].id],
            "confidence": None,
        }]},
        provenance={},
        scope={"kind": "blocks", "block_ids": [_blocks[0].id]},
        actor=ReviewActor("owner", owner.id),
    )
    before = client.mcp_job_projection(project.id, include_glossary=True)["glossary"]
    assert len(before["entries"]) == 1
    assert len(before["suggestions"]) == 1

    operation = _block_operation(
        project,
        record,
        key=f"lifecycle-{terminal_state}",
        state=terminal_state,
    )
    transitioned = client.mcp_job_projection(project.id, include_glossary=True)
    assert transitioned["latest_operation"]["id"] == operation.id
    assert transitioned["glossary"] == before

    _block_operation(
        project,
        record,
        key=f"lifecycle-{terminal_state}-retry",
        state="completed",
    )
    retried = client.mcp_job_projection(project.id, include_glossary=True)
    assert retried["glossary"] == before


@pytest.mark.parametrize("tamper", ["source_plan", "receipt"])
def test_mcp_projection_glossary_fails_closed_for_plan_or_receipt_tamper(
    app, tamper
):
    owner = _user(f"glossary-tamper-{tamper}@example.test")
    project, record, _blocks = _block_project_and_plan(owner)
    _persist_owner_glossary(
        app,
        project,
        owner,
        source_term="Dao",
        target_term="Dao PL",
        notes=None,
    )
    if tamper == "source_plan":
        record.plan_hash = "0" * 64
    else:
        active = TranslationGlossaryActive.query.one()
        active.active_receipt = "0" * 64

    with pytest.raises(BlockWorkspaceIntegrityError):
        api_client.TranslatorClient(
            _Context(), {"current_user_id": owner.id}
        ).mcp_job_projection(project.id, include_glossary=True)


def test_cancel_mcp_execution_signals_queue_and_requests_durable_cancel(app):
    owner = _user("cancel-mcp@example.test")
    execution_id = str(uuid.uuid4())
    project = _project(
        owner,
        settings={
            "execution_mode": "passive",
            "mcp_execution_id": execution_id,
        },
        status="translating",
    )
    operation = TranslationOperation(
        project_id=project.id,
        owner_user_id=owner.id,
        kind="document_block_translate",
        idempotency_key="cancel-mcp",
        request_fingerprint="a" * 64,
        queue_job_id="job-cancel",
        state="running",
        message="working",
        source_content_hash="b" * 64,
        started_at=utc_now_db(),
    )
    db.session.add(operation)
    db.session.commit()
    queue = _Queue()
    client = api_client.TranslatorClient(
        _Context(queue), {"current_user_id": owner.id}
    )

    projection = client.cancel_mcp_execution(
        project.id,
        execution_id=execution_id,
        queue_job_id="job-cancel",
    )

    assert projection["state"] == "cancelled"
    assert queue.cancelled == [
        ("job-cancel", owner.id, "Cancelled through the MCP execution.")
    ]
    db.session.refresh(operation)
    assert operation.state == "cancel_requested"


@pytest.mark.parametrize(
    ("operation_state", "expected_state"),
    [
        ("completed", "completed"),
        ("partial", "failed"),
        ("failed", "failed"),
        ("cancelled", "cancelled"),
    ],
)
def test_cancel_mcp_execution_preserves_terminal_product_truth(
    app, operation_state, expected_state
):
    owner = _user(f"terminal-cancel-{operation_state}@example.test")
    execution_id = str(uuid.uuid4())
    project = _project(
        owner,
        settings={
            "execution_mode": "passive",
            "mcp_execution_id": execution_id,
        },
        status="translating",
    )
    operation = TranslationOperation(
        project_id=project.id,
        owner_user_id=owner.id,
        kind="document_block_translate",
        idempotency_key=f"terminal-cancel-{operation_state}",
        request_fingerprint="a" * 64,
        queue_job_id="job-terminal",
        state=operation_state,
        message="terminal",
        source_content_hash="b" * 64,
        cancel_requested_at=(
            utc_now_db() if operation_state == "cancelled" else None
        ),
        started_at=utc_now_db(),
        finished_at=utc_now_db(),
    )
    db.session.add(operation)
    db.session.commit()
    queue = _Queue()
    client = api_client.TranslatorClient(
        _Context(queue), {"current_user_id": owner.id}
    )

    projection = client.cancel_mcp_execution(
        project.id,
        execution_id=execution_id,
        queue_job_id="job-terminal",
    )

    assert projection["state"] == expected_state
    assert queue.cancelled == []
    db.session.refresh(operation)
    assert operation.state == operation_state


def test_cancel_mcp_execution_reconciles_completion_that_wins_cas_race(
    app, monkeypatch
):
    owner = _user("cancel-race@example.test")
    execution_id = str(uuid.uuid4())
    project = _project(
        owner,
        settings={
            "execution_mode": "passive",
            "mcp_execution_id": execution_id,
        },
        status="translating",
    )
    operation = TranslationOperation(
        project_id=project.id,
        owner_user_id=owner.id,
        kind="document_block_translate",
        idempotency_key="cancel-race",
        request_fingerprint="a" * 64,
        queue_job_id="job-race",
        state="running",
        message="working",
        source_content_hash="b" * 64,
        started_at=utc_now_db(),
    )
    db.session.add(operation)
    db.session.commit()
    queue = _Queue()
    client = api_client.TranslatorClient(
        _Context(queue), {"current_user_id": owner.id}
    )

    def completion_wins(stale_operation):
        TranslationOperation.query.filter_by(id=stale_operation.id).update(
            {
                "state": "completed",
                "version": TranslationOperation.version + 1,
                "finished_at": utc_now_db(),
            },
            synchronize_session=False,
        )
        db.session.commit()
        raise operations.TranslationOperationTransitionConflict(
            stale_operation.id, stale_operation.version
        )

    monkeypatch.setattr(api_client, "request_cancellation", completion_wins)

    projection = client.cancel_mcp_execution(
        project.id,
        execution_id=execution_id,
        queue_job_id="job-race",
    )

    assert projection["state"] == "completed"
    assert queue.cancelled == [
        ("job-race", owner.id, "Cancelled through the MCP execution.")
    ]


def test_guard_wraps_passive_execution_and_managed_path_is_ordinary(app):
    owner = _user("guard-owner@example.test")
    passive_id = str(uuid.uuid4())
    passive_project = _project(
        owner,
        settings={"execution_mode": "passive", "mcp_execution_id": passive_id},
    )
    managed_project = _project(owner, settings={"execution_mode": "managed"})
    passive_operation, _ = operations.create_operation(
        passive_project,
        kind="translate",
        request_payload={},
        idempotency_key="passive-guard",
    )
    managed_operation, _ = operations.create_operation(
        managed_project,
        kind="translate",
        request_payload={},
        idempotency_key="managed-guard",
    )
    db.session.commit()
    entered = []

    @contextmanager
    def fake_passive_execution(**kwargs):
        entered.append(kwargs)
        yield

    passive_handler = worker._guard_project_job(
        lambda payload, _queue, _job_id: payload["_operation"].state
    )
    managed_handler = worker._guard_project_job(
        lambda payload, _queue, _job_id: payload["_operation"].state
    )
    queue = SimpleNamespace()
    with patch.object(worker, "passive_execution", fake_passive_execution):
        assert (
            passive_handler(
                {
                    "project_id": passive_project.id,
                    "operation_id": passive_operation.id,
                    "user_id": owner.id,
                },
                queue,
                "job-passive",
            )
            == "running"
        )
        assert (
            managed_handler(
                {
                    "project_id": managed_project.id,
                    "operation_id": managed_operation.id,
                    "user_id": owner.id,
                },
                queue,
                "job-managed",
            )
            == "running"
        )
    assert entered == [
        {
            "execution_id": passive_id,
            "owner_user_id": owner.id,
            "addon_id": "translator",
            "resource_kind": "project",
            "resource_id": passive_project.id,
        }
    ]


def test_pending_base_exception_bypasses_translator_failure_publication(app):
    owner = _user("pending-owner@example.test")
    project = _project(
        owner,
        settings={"execution_mode": "passive", "mcp_execution_id": str(uuid.uuid4())},
    )
    operation, _ = operations.create_operation(
        project,
        kind="translate",
        request_payload={},
        idempotency_key="pending-guard",
    )
    db.session.commit()
    published_failure = MagicMock()
    pending = task_queue.ExternalWorkPending(
        "Waiting for external model output.",
        delay_seconds=30,
        work_item_id=str(uuid.uuid4()),
    )
    guarded = worker._guard_project_job(
        lambda _payload, _queue, _job_id: (_ for _ in ()).throw(pending)
    )

    with (
        patch.object(worker, "passive_execution", lambda **_kwargs: nullcontext()),
        patch.object(worker, "_publish_operation_failure", published_failure),
        pytest.raises(task_queue.ExternalWorkPending),
    ):
        guarded(
            {
                "project_id": project.id,
                "operation_id": operation.id,
                "user_id": owner.id,
            },
            SimpleNamespace(),
            "job-pending",
        )
    published_failure.assert_not_called()


def test_same_delivery_reentry_is_version_fenced_and_other_delivery_rejected(app):
    owner = _user("lease-owner@example.test")
    project = _project(owner)
    operation, _ = operations.create_operation(
        project,
        kind="translate",
        request_payload={},
        idempotency_key="lease-reentry",
    )
    first = operations.claim_operation(operation.id, "job-same")
    stale_version = first.version
    renewed = operations.claim_operation(operation.id, "job-same")
    assert renewed.version == stale_version + 1
    assert renewed.started_at == first.started_at
    assert renewed.lease_expires_at > utc_now_db()

    with pytest.raises(operations.TranslationOperationTransitionConflict):
        operations.claim_operation(
            operation.id,
            "job-same",
            expected_version=stale_version,
        )
    with pytest.raises(operations.TranslationOperationClaimConflict):
        operations.claim_operation(operation.id, "job-other")
    operations.finish_operation(operation, "completed")
    with pytest.raises(operations.TranslationOperationNotRunnable):
        operations.claim_operation(operation.id, "job-same")



def test_mcp_partial_projection_exposes_bounded_validator_codes_not_response_text(app):
    owner = _user("partial-diagnostics@example.test")
    project = _project(owner)
    operation = TranslationOperation(
        project_id=project.id, owner_user_id=owner.id, kind="translate",
        idempotency_key="diagnostic", request_fingerprint="a" * 64,
        state="partial", error_code="block_window_incomplete",
        result_json={"block_failure": {
            "attempt_id": "attempt-1", "window_order": 2,
            "affected_block_ids": ["block-19", "<private source>"],
            "failure_kind": "normalization_failed", "dispatch_state": "normalization_failed",
            "recovery_action": "translate_remaining", "remaining_dispatches": 0,
            "validator": "target_response_normalization",
            "validation_codes": ["target-protected-reference", "private prose and /path"],
            "response": "Never expose this source text.",
        }},
    )
    db.session.add(operation)
    db.session.commit()
    projection = api_client.TranslatorClient(_Context(), {"current_user_id": owner.id}).mcp_job_projection(project.id)
    failure = projection["block_failure"]
    assert failure["window_order"] == 2
    assert failure["affected_block_ids"] == ["block-19"]
    assert failure["validation_codes"] == ["target-protected-reference"]
    assert failure["validator"] == "target_response_normalization"
    assert "private" not in repr(projection) and "Never expose" not in repr(projection)
