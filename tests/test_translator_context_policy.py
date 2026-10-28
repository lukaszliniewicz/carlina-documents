from __future__ import annotations

import hashlib
from copy import deepcopy

import pytest
from content_storage_support import bind_content_policy
from flask import Flask, g
from flask_login import LoginManager
from pydantic import ValidationError

from carlina.core.semantic_html import SEMANTIC_HTML_CONTRACT_VERSION
from iarf_assistant import extensions, models
from iarf_assistant.addons.translator import api_client, operations, routes
from iarf_assistant.addons.translator.block_contract import (
    AttestedDocumentSnapshot,
    BlockContractError,
)
from iarf_assistant.addons.translator.block_persistence import (
    persist_source_plan,
    source_attestation_for_project,
)
from iarf_assistant.addons.translator.block_planner import plan_document_blocks
from iarf_assistant.addons.translator.block_workflow_config import (
    _validate_config,
    freeze_block_workflow_config,
    normalize_translation_context_mode,
    persist_block_workflow_config,
    translation_context_policy,
)
from iarf_assistant.addons.translator.block_workspace_api import (
    persist_manual_candidate,
)
from iarf_assistant.addons.translator.mcp_settings import TranslatorSettings
from iarf_assistant.addons.translator.models import (
    TranslationBlockWorkflowConfig,
    TranslationOperation,
    TranslationProject,
    TranslationTargetBlockActive,
    TranslationTargetBlockRevision,
)
from iarf_assistant.addons.translator.target_contract import (
    TargetBlockCandidate,
    TargetTextPart,
    structural_fingerprint,
)
from iarf_assistant.core.encryption import EncryptionService, InstallationKeyStore

db = extensions.db
User = models.User
ApiKey = models.ApiKey
LLMModel = models.LLMModel
LLMProvider = models.LLMProvider

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
        self.enqueued: list[tuple[str, dict]] = []

    def enqueue(self, handler, payload):
        job_id = f"job-{len(self.enqueued) + 1}"
        self.enqueued.append((handler, payload))
        return job_id


class _Context:
    def __init__(self):
        self.queue = _Queue()

    def get_task_queue(self, addon_id):
        assert addon_id == "translator"
        return self.queue


@pytest.fixture
def app(tmp_path):
    application = Flask(__name__, instance_path=str(tmp_path / "instance"))
    bind_content_policy(application)
    application.config.update(
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{tmp_path / 'translator-context.db'}",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SECRET_KEY="translator-context-test",
        TESTING=True,
        SQLALCHEMY_ENGINE_OPTIONS={"connect_args": {"timeout": 30}},
    )
    db.init_app(application)
    login_manager = LoginManager()
    login_manager.init_app(application)

    @login_manager.user_loader
    def load_user(user_id):
        return db.session.get(User, int(user_id))

    application.register_blueprint(routes.blueprint, url_prefix="/addons/translator")
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


def _settings(*, context_mode="self_contained", execution_mode="passive"):
    return {
        "source_type": "document",
        "source_reference": {
            "version": 1,
            "capability": "document.processing",
            "provider_addon_id": "doc-provider",
            "provider_capability_id": "document-processing",
            "resource_type": "document",
            "resource_id": "context-doc",
        },
        "execution_mode": execution_mode,
        "translator_model": "",
        "enable_pre_analysis": False,
        "enable_research": False,
        "translation_unit_mode": "block",
        "annotation_profile": "none",
        "enable_translator_notes": False,
        "translation_context_mode": context_mode,
        "translation_batch_input_tokens": 2_048,
        "translation_batch_max_blocks": 24,
    }


def _project_and_plan(user: User, *, settings=None):
    source_html = (
        "<html><body><p>First source sentence.</p>"
        "<p>Second source sentence.</p><p>Third source sentence.</p></body></html>"
    )
    source_document_id = f"context-doc-{user.id}"
    source_hash = hashlib.sha256(source_html.encode()).hexdigest()
    project_settings = _settings() if settings is None else dict(settings)
    project_settings["source_reference"]["resource_id"] = source_document_id
    project = TranslationProject(
        user_id=user.id,
        name="Context policy project",
        source_lang="en",
        target_lang="pl",
        status="setup",
        engine_contract="document-block-v1",
        document_id=source_document_id,
        source_content_hash=source_hash,
        settings=project_settings,
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
    return project, plan, record


def _ready_chat_model(user: User) -> str:
    provider = LLMProvider(
        user_id=user.id,
        name=f"context-provider-{user.id}",
        display_name="Context policy test provider",
    )
    db.session.add(provider)
    db.session.flush()
    model_id = f"context-provider-{user.id}/chat"
    db.session.add_all(
        [
            ApiKey(
                user_id=user.id,
                provider_id=provider.id,
                name="Test key",
                api_key="context-test-key",  # pragma: allowlist secret
                is_active=True,
            ),
            LLMModel(
                user_id=user.id,
                provider_id=provider.id,
                model_id=model_id,
                display_name="Context policy test chat",
                is_active=True,
                model_type="chat",
            ),
        ]
    )
    db.session.commit()
    return model_id


def _passive_client(user: User) -> tuple[api_client.TranslatorClient, _Context]:
    context = _Context()
    return (
        api_client.TranslatorClient(context, {"current_user_id": user.id}),
        context,
    )


def _seed_target(project, plan, source_plan, user):
    block = plan.blocks[0]
    candidate = TargetBlockCandidate(
        source_plan_id=source_plan.id,
        source_plan_hash=plan.plan_hash,
        block_id=block.id,
        structural_fingerprint=structural_fingerprint(plan, block),
        parts=(TargetTextPart(kind="text", text="Existing target."),),
        lexical_attributes=(),
    )
    persist_manual_candidate(
        project,
        block.id,
        candidate,
        0,
        author_user_id=user.id,
        idempotency_key="seed-context-target",
    )
    db.session.commit()
    return TranslationTargetBlockActive.query.filter_by(
        source_plan_id=source_plan.id, block_id=block.id
    ).one()


def test_context_mode_has_strict_default_and_is_persisted_by_create_and_settings_http(
    app,
):
    assert TranslatorSettings().translation_context_mode == "self_contained"
    assert (
        TranslatorSettings.model_validate(
            {"translation_context_mode": "retained"}
        ).translation_context_mode
        == "retained"
    )
    with pytest.raises(ValidationError):
        TranslatorSettings.model_validate({"translation_context_mode": "unknown"})
    with pytest.raises(ValidationError):
        TranslatorSettings.model_validate({"translation_context_mode": 1})
    for invalid in (None, True, "Retained", "unknown"):
        with pytest.raises(BlockContractError):
            normalize_translation_context_mode(invalid)

    owner = _user("context-settings@example.test")
    client, _context = _passive_client(owner)
    settings = client._settings(
        execution_mode="passive", translation_context_mode="retained"
    )
    assert settings["translation_context_mode"] == "retained"
    project = client._new_project(
        name="Created with retained context",
        source_lang="en",
        target_lang="pl",
        settings=settings,
        persist=False,
    )
    project, created = client._create_project(
        project,
        idempotency_key="context-create",
        request_payload={"translation_context_mode": "retained"},
    )
    assert created is True
    assert TranslationProject.query.filter_by(id=project.id).one().settings[
        "translation_context_mode"
    ] == "retained"

    http_settings = routes._base_project_settings(
        {"execution_mode": "passive", "translation_context_mode": "retained"}
    )
    assert http_settings["translation_context_mode"] == "retained"
    with pytest.raises(BlockContractError):
        routes._base_project_settings(
            {"execution_mode": "passive", "translation_context_mode": "unknown"}
        )

    managed_settings = _settings(execution_mode="passive")
    managed_settings.pop("translation_context_mode")
    managed_project, _plan, _record = _project_and_plan(owner, settings=managed_settings)
    test_client = app.test_client()
    with test_client.session_transaction() as session:
        session["_user_id"] = str(owner.id)
        session["_fresh"] = True
    url = f"/addons/translator/api/projects/{managed_project.id}/settings"
    current = test_client.get(url)
    assert current.status_code == 200
    assert current.get_json()["translation_context_mode"] == "self_contained"
    updated = test_client.put(url, json={"translation_context_mode": "retained"})
    assert updated.status_code == 200
    db.session.refresh(managed_project)
    assert managed_project.settings["translation_context_mode"] == "retained"
    assert test_client.get(url).get_json()["translation_context_mode"] == "retained"
    invalid = test_client.put(url, json={"translation_context_mode": "unknown"})
    assert invalid.status_code == 400
    db.session.refresh(managed_project)
    assert managed_project.settings["translation_context_mode"] == "retained"


def test_configure_batches_cas_versions_context_mode_and_never_replans(app):
    owner = _user("context-cas@example.test")
    project, _plan, source_plan = _project_and_plan(owner)
    client, _context = _passive_client(owner)
    original_plan_id = source_plan.id
    original = client._batch_settings(project)

    changed = client.configure_batches(
        project.id,
        input_tokens=4_096,
        max_blocks=16,
        expected_version=original["version"],
        context_mode="retained",
    )
    assert changed["batch_settings"]["context_mode"] == "retained"
    assert changed["batch_settings"]["version"] != original["version"]
    assert changed["source_plan_changed"] is False
    db.session.refresh(project)
    assert project.settings["translation_context_mode"] == "retained"
    assert client._batch_settings(project)["version"] == changed["batch_settings"]["version"]

    omitted = client.configure_batches(
        project.id,
        input_tokens=5_120,
        max_blocks=15,
        expected_version=changed["batch_settings"]["version"],
    )
    assert omitted["batch_settings"]["context_mode"] == "retained"
    with pytest.raises(BlockContractError, match="translation-batch-version-conflict"):
        client.configure_batches(
            project.id,
            input_tokens=5_120,
            max_blocks=15,
            expected_version=changed["batch_settings"]["version"],
            context_mode="self_contained",
        )
    assert api_client.active_source_plan_pointer(project.id).source_plan_id == original_plan_id

    operation, _created = operations.create_operation(
        project,
        kind="document_block_translate",
        request_payload={},
        idempotency_key="context-config-active",
    )
    db.session.commit()
    before = dict(project.settings)
    with pytest.raises(BlockContractError, match="translation-batch-operation-active"):
        client.configure_batches(
            project.id,
            input_tokens=6_144,
            max_blocks=14,
            expected_version=omitted["batch_settings"]["version"],
            context_mode="self_contained",
        )
    db.session.refresh(project)
    assert project.settings == before
    assert operation.state == "queued"
    assert api_client.active_source_plan_pointer(project.id).source_plan_id == original_plan_id


def test_passive_start_freezes_v2_identity_preserves_v1_and_targets_and_is_idempotent(
    app,
):
    owner = _user("context-start@example.test")
    settings = _settings(context_mode="self_contained")
    project, plan, source_plan = _project_and_plan(owner, settings=settings)
    target = _seed_target(project, plan, source_plan, owner)
    target_revision_id = target.revision_id
    window_profile = {
        "profile_id": "document-block-window",
        "profile_version": "2",
        "capacity_version": "context-policy-test-v1",
        "max_blocks": 24,
        "max_input_tokens": EXECUTOR_PROFILE["max_input_tokens"],
        "output_reserve_tokens": EXECUTOR_PROFILE["max_output_tokens"],
        "target_input_tokens": 2_048,
    }
    legacy, _ = operations.create_operation(
        project,
        kind="document_block_translate",
        request_payload={"legacy": True},
        idempotency_key=f"translate-{plan.plan_hash[:48]}",
        source_content_hash=source_plan.source_content_hash,
        source_plan_id=source_plan.id,
        source_plan_hash=source_plan.plan_hash,
    )
    operations.claim_operation(legacy.id, "legacy-job")
    legacy_config = freeze_block_workflow_config(
        legacy,
        source_plan,
        user=owner,
        settings=project.settings,
        window_profile=window_profile,
        protected_targets=None,
        executor_profile=EXECUTOR_PROFILE,
    )
    persist_block_workflow_config(legacy, source_plan, legacy_config)
    operations.finish_operation(legacy, "cancelled")
    db.session.commit()
    assert legacy_config["translation_context"] == {
        "contract_version": "translation-context-v1"
    }
    assert _validate_config(legacy_config) == legacy_config
    contextless = deepcopy(legacy_config)
    contextless.pop("foreign_language_policy")
    contextless.pop("translation_context")
    assert _validate_config(contextless) == contextless
    explicit_none = deepcopy(contextless)
    explicit_none["translation_context"] = None
    assert _validate_config(explicit_none) == explicit_none
    for invalid_context in (
        {"contract_version": "translation-context-v2", "continuity_mode": "inherit"},
        {
            "contract_version": "translation-context-v2",
            "continuity_mode": "retained",
            "unexpected": True,
        },
        {"contract_version": "translation-context-v2", "continuity_mode": 1},
    ):
        malformed = deepcopy(legacy_config)
        malformed["translation_context"] = invalid_context
        with pytest.raises(BlockContractError, match="workflow-config-translation-context"):
            _validate_config(malformed)
    malformed_top_level = deepcopy(legacy_config)
    malformed_top_level["context_mode"] = "retained"
    with pytest.raises(BlockContractError, match="workflow-config-keys"):
        _validate_config(malformed_top_level)

    client, context = _passive_client(owner)
    first = client.start_block_translation(
        project.id,
        executor_profile=EXECUTOR_PROFILE,
        context_mode="retained",
    )
    current = TranslationOperation.query.filter_by(
        project_id=project.id,
        kind="document_block_translate",
        state="queued",
    ).one()
    legacy_fence = hashlib.sha256(legacy.id.encode("utf-8")).hexdigest()[:16]
    frozen = TranslationBlockWorkflowConfig.query.filter_by(
        operation_id=current.id
    ).one()
    assert current.id != legacy.id
    assert current.idempotency_key == (
        f"translate-{plan.plan_hash[:48]}-ctxv2-retained-after-{legacy_fence}"
    )
    assert frozen.config_json["translation_context"] == translation_context_policy(
        "retained"
    )
    assert _validate_config(frozen.config_json) == frozen.config_json
    assert frozen.config_json["source_plan"] == legacy_config["source_plan"]
    assert TranslationTargetBlockActive.query.filter_by(
        source_plan_id=source_plan.id, block_id=target.block_id
    ).one().revision_id == target_revision_id
    assert TranslationTargetBlockRevision.query.filter_by(
        id=target_revision_id
    ).one() is not None
    db.session.refresh(project)
    assert project.settings["translation_context_mode"] == "self_contained"
    assert first["job_id"] == "job-1"
    assert context.queue.enqueued[0][1]["translation_context"] == translation_context_policy(
        "retained"
    )

    operations.claim_operation(current.id, first["job_id"])
    operations.finish_operation(current, "completed")
    db.session.commit()
    retry = client.start_block_translation(
        project.id,
        executor_profile=EXECUTOR_PROFILE,
        context_mode="retained",
    )
    assert retry["job_id"] == first["job_id"]
    assert TranslationOperation.query.filter_by(
        project_id=project.id, kind="document_block_translate"
    ).count() == 2
    assert len(context.queue.enqueued) == 1

    first_a = client.start_block_translation(
        project.id,
        executor_profile=EXECUTOR_PROFILE,
        context_mode="self_contained",
    )
    a_after_b1 = hashlib.sha256(current.id.encode("utf-8")).hexdigest()[:16]
    first_a_operation = TranslationOperation.query.filter_by(
        project_id=project.id,
        kind="document_block_translate",
        idempotency_key=(
            f"translate-{plan.plan_hash[:48]}-ctxv2-self_contained-after-{a_after_b1}"
        ),
    ).one()
    first_a_config = TranslationBlockWorkflowConfig.query.filter_by(
        operation_id=first_a_operation.id
    ).one()
    assert first_a_operation.id != current.id
    assert first_a["job_id"] == "job-2"
    assert first_a_config.config_json["translation_context"] == (
        translation_context_policy("self_contained")
    )
    operations.claim_operation(first_a_operation.id, first_a["job_id"])
    operations.finish_operation(first_a_operation, "completed")
    db.session.commit()

    second_b = client.start_block_translation(
        project.id,
        executor_profile=EXECUTOR_PROFILE,
        context_mode="retained",
    )
    b_after_a1 = hashlib.sha256(first_a_operation.id.encode("utf-8")).hexdigest()[:16]
    second_b_operation = TranslationOperation.query.filter_by(
        project_id=project.id,
        kind="document_block_translate",
        idempotency_key=(
            f"translate-{plan.plan_hash[:48]}-ctxv2-retained-after-{b_after_a1}"
        ),
    ).one()
    assert second_b_operation.id != current.id
    assert second_b["job_id"] == "job-3"
    operations.claim_operation(second_b_operation.id, second_b["job_id"])
    operations.finish_operation(second_b_operation, "completed")
    db.session.commit()

    second_a = client.start_block_translation(
        project.id,
        executor_profile=EXECUTOR_PROFILE,
        context_mode="self_contained",
    )
    a_after_b2 = hashlib.sha256(second_b_operation.id.encode("utf-8")).hexdigest()[:16]
    second_a_operation = TranslationOperation.query.filter_by(
        project_id=project.id,
        kind="document_block_translate",
        idempotency_key=(
            f"translate-{plan.plan_hash[:48]}-ctxv2-self_contained-after-{a_after_b2}"
        ),
    ).one()
    assert second_a_operation.id != first_a_operation.id
    assert second_a["job_id"] == "job-4"
    operations.claim_operation(second_a_operation.id, second_a["job_id"])
    operations.finish_operation(second_a_operation, "completed")
    db.session.commit()
    retry_a = client.start_block_translation(
        project.id,
        executor_profile=EXECUTOR_PROFILE,
        context_mode="self_contained",
    )
    assert retry_a["job_id"] == second_a["job_id"]
    assert TranslationOperation.query.filter_by(
        project_id=project.id, kind="document_block_translate"
    ).count() == 5
    assert len(context.queue.enqueued) == 4

    saved_retained_settings = _settings(context_mode="retained")
    saved_retained, _saved_plan, _saved_record = _project_and_plan(
        owner, settings=saved_retained_settings
    )
    saved_client, saved_context = _passive_client(owner)
    saved_start = saved_client.start_block_translation(
        saved_retained.id,
        executor_profile=EXECUTOR_PROFILE,
    )
    saved_operation = TranslationOperation.query.filter_by(
        project_id=saved_retained.id, kind="document_block_translate"
    ).one()
    saved_config = TranslationBlockWorkflowConfig.query.filter_by(
        operation_id=saved_operation.id
    ).one()
    assert saved_start["job_id"] == "job-1"
    assert saved_config.config_json["translation_context"] == translation_context_policy(
        "retained"
    )
    db.session.refresh(saved_retained)
    assert saved_retained.settings["translation_context_mode"] == "retained"
    assert saved_context.queue.enqueued[0][1]["translation_context"] == (
        translation_context_policy("retained")
    )


def test_managed_start_forces_self_contained_and_active_conflicting_passive_start_writes_nothing(
    app,
):
    owner = _user("context-managed@example.test")
    model_id = _ready_chat_model(owner)
    managed_settings = _settings(context_mode="retained", execution_mode="managed")
    managed_settings.update(
        translator_model=model_id,
        enable_pre_analysis=False,
        annotation_profile="none",
        enable_translator_notes=False,
    )
    managed, _plan, _record = _project_and_plan(owner, settings=managed_settings)
    before = dict(managed.settings)
    managed_client, managed_context = _passive_client(owner)
    managed_client.start_block_translation(
        managed.id,
        context_mode="retained",
    )
    managed_operation = TranslationOperation.query.filter_by(
        project_id=managed.id, kind="document_block_translate"
    ).one()
    managed_config = TranslationBlockWorkflowConfig.query.filter_by(
        operation_id=managed_operation.id
    ).one()
    assert managed_config.config_json["translation_context"] == translation_context_policy(
        "self_contained"
    )
    db.session.refresh(managed)
    assert managed.settings == before
    assert managed_context.queue.enqueued[0][1]["translation_context"] == translation_context_policy(
        "self_contained"
    )

    passive_settings = _settings(context_mode="self_contained")
    passive, _plan, _record = _project_and_plan(owner, settings=passive_settings)
    passive_client, passive_context = _passive_client(owner)
    passive_client.start_block_translation(
        passive.id,
        executor_profile=EXECUTOR_PROFILE,
    )
    active = TranslationOperation.query.filter_by(
        project_id=passive.id, kind="document_block_translate"
    ).one()
    old_settings = dict(passive.settings)
    with pytest.raises(RuntimeError, match="work in progress"):
        passive_client.start_block_translation(
            passive.id,
            executor_profile=EXECUTOR_PROFILE,
            context_mode="retained",
        )
    db.session.refresh(passive)
    assert passive.settings == old_settings
    assert active.state == "queued"
    assert len(passive_context.queue.enqueued) == 1
    assert TranslationOperation.query.filter_by(
        project_id=passive.id, kind="document_block_translate"
    ).count() == 1


def test_context_get_is_owner_and_plan_fenced_bounded_and_read_only_while_active(
    app, monkeypatch
):
    owner = _user("context-get-owner@example.test")
    other = _user("context-get-other@example.test")
    project, plan, source_plan = _project_and_plan(owner)
    target = _seed_target(project, plan, source_plan, owner)
    other_project, _other_plan, other_source_plan = _project_and_plan(other)
    operation, _ = operations.create_operation(
        project,
        kind="document_block_translate",
        request_payload={},
        idempotency_key="context-get-active",
        source_content_hash=source_plan.source_content_hash,
        source_plan_id=source_plan.id,
        source_plan_hash=source_plan.plan_hash,
    )
    db.session.commit()

    from iarf_assistant.addons.translator import segment_context

    real_context_before_block = segment_context.context_before_block
    forwarded = {}

    def capture(record, *, plan, before_block_id, max_segments):
        forwarded.update(
            source_plan_id=record.id,
            before_block_id=before_block_id,
            max_segments=max_segments,
        )
        return real_context_before_block(
            record,
            plan=plan,
            before_block_id=before_block_id,
            max_segments=max_segments,
        )

    monkeypatch.setattr(segment_context, "context_before_block", capture)
    client, _context = _passive_client(owner)
    before = {
        "operations": TranslationOperation.query.count(),
        "configs": TranslationBlockWorkflowConfig.query.count(),
        "targets": TranslationTargetBlockRevision.query.count(),
    }
    result = client.context_get(
        project.id,
        before_block_id=plan.blocks[2].id,
        max_segments=2,
        source_plan_id=source_plan.id,
    )
    assert result["project_id"] == project.id
    assert result["source_plan_id"] == source_plan.id
    assert forwarded == {
        "source_plan_id": source_plan.id,
        "before_block_id": plan.blocks[2].id,
        "max_segments": 2,
    }
    assert result["before_block_id"] == plan.blocks[2].id
    assert result["source_plan_hash"] == plan.plan_hash
    assert result["predecessor"]["revision_id"] == target.revision_id
    assert result["predecessor"]["alignment_revision_id"]
    with pytest.raises(BlockContractError, match="segment-context-source-plan"):
        client.context_get(
            project.id,
            before_block_id=plan.blocks[2].id,
            source_plan_id=other_source_plan.id,
        )
    with pytest.raises(LookupError, match="not found"):
        api_client.TranslatorClient(
            _Context(), {"current_user_id": other.id}
        ).context_get(
            project.id,
            before_block_id=plan.blocks[2].id,
        )
    for invalid_segments in (True, 0, 4, 1.5, "2"):
        with pytest.raises(BlockContractError, match="segment-context-request"):
            client.context_get(
                project.id,
                before_block_id=plan.blocks[2].id,
                max_segments=invalid_segments,
            )
    with pytest.raises(BlockContractError):
        client.context_get(project.id, before_block_id="unknown-block")
    db.session.refresh(operation)
    assert operation.state == "queued"
    assert before == {
        "operations": TranslationOperation.query.count(),
        "configs": TranslationBlockWorkflowConfig.query.count(),
        "targets": TranslationTargetBlockRevision.query.count(),
    }

    http_client = app.test_client()
    monkeypatch.setattr(routes.blueprint, "addon_context", _Context(), raising=False)
    with http_client.session_transaction() as session:
        session["_user_id"] = str(owner.id)
        session["_fresh"] = True
    response = http_client.get(
        f"/addons/translator/api/projects/{project.id}/translation-context",
        query_string={
            "before_block_id": plan.blocks[2].id,
            "max_segments": "2",
            "source_plan_id": source_plan.id,
        },
    )
    assert response.status_code == 200
    assert response.get_json()["source_plan_id"] == source_plan.id
    invalid_http = http_client.get(
        f"/addons/translator/api/projects/{project.id}/translation-context",
        query_string={
            "before_block_id": plan.blocks[2].id,
            "max_segments": "4",
        },
    )
    assert invalid_http.status_code == 400
    stale_http = http_client.get(
        f"/addons/translator/api/projects/{project.id}/translation-context",
        query_string={
            "before_block_id": plan.blocks[2].id,
            "source_plan_id": other_source_plan.id,
        },
    )
    assert stale_http.status_code in {400, 409}
    other_http_client = app.test_client()
    with other_http_client.session_transaction() as session:
        session["_user_id"] = str(other.id)
        session["_fresh"] = True
    g.pop("_login_user", None)
    forbidden = other_http_client.get(
        f"/addons/translator/api/projects/{project.id}/translation-context",
        query_string={"before_block_id": plan.blocks[2].id},
    )
    assert forbidden.status_code == 404
    assert other_project.id != project.id
