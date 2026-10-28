"""Focused contracts for immutable document-block workflow configuration."""

from __future__ import annotations

import hashlib
from copy import deepcopy
from dataclasses import replace
from importlib import import_module
from pathlib import Path

import pytest
from content_storage_support import bind_content_policy
from flask import Flask
from sqlalchemy import text

from carlina.core.semantic_html import SEMANTIC_HTML_CONTRACT_VERSION

_TRANSLATOR_ROOT = "iarf" + "_assistant.addons.translator"
_config = import_module(f"{_TRANSLATOR_ROOT}.block_workflow_config")
_contract = import_module(f"{_TRANSLATOR_ROOT}.block_contract")
_persistence = import_module(f"{_TRANSLATOR_ROOT}.block_persistence")
_planner = import_module(f"{_TRANSLATOR_ROOT}.block_planner")
_models = import_module(f"{_TRANSLATOR_ROOT}.models")
_core_models = import_module("iarf" + "_assistant.models")
_core_encryption = import_module("iarf" + "_assistant.core.encryption")
_extensions = import_module("iarf" + "_assistant.extensions")
_window_execution = import_module(f"{_TRANSLATOR_ROOT}.window_execution")
_evidence_reuse = import_module(f"{_TRANSLATOR_ROOT}.evidence_reuse")

AttestedDocumentSnapshot = _contract.AttestedDocumentSnapshot
TranslationBlockWorkflowConfig = _models.TranslationBlockWorkflowConfig
TranslationOperation = _models.TranslationOperation
TranslationProject = _models.TranslationProject
TranslationSourcePlan = _models.TranslationSourcePlan
TranslationBlock = _models.TranslationBlock
freeze = _config.freeze_block_workflow_config
persist = _config.persist_block_workflow_config
rehydrate = _config.rehydrate_block_workflow_config
rehydrate_historical = _config.rehydrate_historical_block_workflow_config
execution_profile_from_frozen_phase = (
    _window_execution.execution_profile_from_frozen_phase
)
BlockContractError = _contract.BlockContractError
persist_source_plan = _persistence.persist_source_plan
source_attestation_for_project = _persistence.source_attestation_for_project
plan_document_blocks = _planner.plan_document_blocks
ApiKey = _core_models.ApiKey
LLMModel = _core_models.LLMModel
LLMProvider = _core_models.LLMProvider
Setting = _core_models.Setting
User = _core_models.User
EncryptionService = _core_encryption.EncryptionService
InstallationKeyStore = _core_encryption.InstallationKeyStore
db = _extensions.db


@pytest.fixture
def app(tmp_path: Path):
    application = Flask(__name__, instance_path=str(tmp_path / "instance"))
    bind_content_policy(application)
    application.config.update(
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{tmp_path / 'workflow-config.db'}",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SECRET_KEY="workflow-config-test",
        TESTING=True,
        SQLALCHEMY_ENGINE_OPTIONS={"connect_args": {"timeout": 30}},
    )
    db.init_app(application)
    context = application.app_context()
    context.push()
    keys = InstallationKeyStore(tmp_path / "keys.json")
    keys.initialize()
    application.extensions["carlina_encryption"] = EncryptionService(keys)
    db.create_all()
    try:
        yield application
    finally:
        db.session.remove()
        db.engine.dispose()
        context.pop()


def _state():
    user = User(email="config@example.test", password_hash="x")
    db.session.add(user)
    db.session.flush()
    provider = LLMProvider(
        user_id=None,
        name="config-provider",
        provider_kind="native",
    )
    db.session.add(provider)
    db.session.flush()
    key = ApiKey(
        user_id=None,
        provider_id=provider.id,
        name="primary",
        api_key="secret-value",
        is_active=True,
    )
    model = LLMModel(
        user_id=None,
        provider_id=provider.id,
        model_id="config-provider/chat",
        display_name="Configured Chat",
        is_active=True,
        manually_added=True,
        model_type="chat",
        supports_reasoning=True,
        supported_reasoning_levels="off,high",
        updated_at=None,
    )
    db.session.add_all([key, model])
    db.session.flush()
    project = TranslationProject(
        user_id=user.id,
        name="Config document",
        source_lang="en",
        target_lang="pl",
        engine_contract="document-block-v1",
        settings={
            "source_reference": {
                "version": 1,
                "capability": "document.processing",
                "provider_addon_id": "doc",
                "provider_capability_id": "process",
                "resource_type": "document",
                "resource_id": "config-doc",
            }
        },
    )
    db.session.add(project)
    db.session.flush()
    html = "<html><body><h1>Title</h1><p>Source paragraph.</p></body></html>"
    digest = hashlib.sha256(html.encode()).hexdigest()
    snapshot = AttestedDocumentSnapshot(
        id="config-doc",
        document_id="config-doc",
        output_html=html,
        content_hash=digest,
        semantic_content_hash=digest,
        metadata={},
        provenance={},
        captured_at="2026-08-26T00:00:00+00:00",
        semantic_contract_version=SEMANTIC_HTML_CONTRACT_VERSION,
    )
    plan = plan_document_blocks(
        snapshot,
        project_id=project.id,
        source_attestation=source_attestation_for_project(project),
        source_language="en",
    )
    record, _ = persist_source_plan(project, plan)
    operation = TranslationOperation(
        project_id=project.id,
        owner_user_id=user.id,
        kind="document_block_translate",
        idempotency_key="config-op",
        request_fingerprint="a" * 64,
        state="running",
        source_content_hash=record.source_content_hash,
        source_plan_id=record.id,
        source_plan_hash=record.plan_hash,
    )
    db.session.add(operation)
    db.session.commit()
    return user, project, record, operation


def _settings(**overrides):
    result = {
        "translator_model": "config-provider/chat",
        "enable_pre_analysis": False,
        "enable_research": False,
    }
    result.update(overrides)
    return result


def _window_profile():
    return {
        "profile_id": "document-block-window",
        "profile_version": "1",
        "capacity_version": "model-capacity-v1",
        "max_blocks": 4,
        "max_input_tokens": 32_000,
        "output_reserve_tokens": 2_048,
    }


def _research_dossier_pin():
    return {
        "revision_id": "dossier-revision-1",
        "record_hash": "a" * 64,
        "content_digest": "b" * 64,
        "conclusion_provenance": "external_unverified",
        "evidence_verification_state": "unverified",
    }


def _executor_profile():
    return {
        "schema_version": "carlina.external-executor.v1",
        "max_input_tokens": 64_000,
        "max_output_tokens": 16_000,
        "input_modalities": ["text"],
        "supports_tools": True,
        "supports_structured_output": True,
        "supports_reasoning": True,
        "user_confirmed": True,
    }


def test_disabled_phases_and_blank_policy_are_absent(app):
    user, _project, record, operation = _state()
    material = freeze(
        operation,
        record,
        user=user,
        settings=_settings(custom_prompt="  "),
        window_profile=_window_profile(),
    )
    assert material["enable_pre_analysis"] is False
    assert material["enable_research"] is False
    assert "pre_analysis" not in material
    assert "research" not in material
    assert "embedding_projection" not in material
    assert "owner_policy" not in material
    assert material["document_structure_context"] == {
        "contract_version": "document-structure-context-v1"
    }
    assert material["foreign_language_policy"] == "preserve-passages-v2"
    assert (
        material["writable_source_contract_version"]
        == "translation-block-writable-source-v4"
    )
    row = persist(operation, record, material)
    db.session.commit()
    assert rehydrate(row, operation=operation, source_plan=record) is row
    assert row.config_json["translation"]["reasoning"]["slot"] == "translation"


def test_foreign_language_policy_v2_freezes_and_legacy_configs_remain_valid(app):
    user, _project, record, operation = _state()
    material = freeze(
        operation,
        record,
        user=user,
        settings=_settings(),
        window_profile=_window_profile(),
    )

    assert material["foreign_language_policy"] == "preserve-passages-v2"
    legacy = deepcopy(material)
    legacy.pop("foreign_language_policy")
    assert _config._validate_config(legacy) == legacy
    legacy_contextless = deepcopy(legacy)
    legacy_contextless.pop("translation_context")
    assert _config._validate_config(legacy_contextless) == legacy_contextless

    for invalid_policy in (None, "preserve-passages-v1", "unknown-policy"):
        malformed = deepcopy(material)
        malformed["foreign_language_policy"] = invalid_policy
        with pytest.raises(
            BlockContractError, match="workflow-config-foreign-language-policy"
        ):
            _config._validate_config(malformed)

    missing_context = deepcopy(material)
    missing_context.pop("translation_context")
    with pytest.raises(
        BlockContractError,
        match="workflow-config-foreign-language-policy-context",
    ):
        _config._validate_config(missing_context)


def test_research_dossier_pin_round_trips_and_replaces_managed_phases(app):
    user, _project, record, operation = _state()
    material = freeze(
        operation,
        record,
        user=user,
        settings=_settings(),
        window_profile=_window_profile(),
        research_dossier=_research_dossier_pin(),
    )
    assert material["research_dossier"] == _research_dossier_pin()
    assert material["enable_pre_analysis"] is False
    assert material["enable_research"] is False
    assert "pre_analysis" not in material
    assert "research" not in material
    row = persist(operation, record, material)
    db.session.commit()
    assert rehydrate(row, operation=operation, source_plan=record) is row

    for malformed in (
        {**_research_dossier_pin(), "extra": "rejected"},
        {
            key: value
            for key, value in _research_dossier_pin().items()
            if key != "record_hash"
        },
        {**_research_dossier_pin(), "record_hash": "A" * 64},
        {**_research_dossier_pin(), "conclusion_provenance": []},
        {**_research_dossier_pin(), "conclusion_provenance": "managed_verified"},
        {**_research_dossier_pin(), "evidence_verification_state": "partial"},
    ):
        tampered = deepcopy(material)
        tampered["research_dossier"] = malformed
        with pytest.raises(
            BlockContractError, match="workflow-config-research-dossier"
        ):
            _config._validate_config(tampered)

    with pytest.raises(BlockContractError, match="workflow-config-research-dossier"):
        freeze(
            operation,
            record,
            user=user,
            settings=_settings(enable_pre_analysis=True),
            window_profile=_window_profile(),
            research_dossier=_research_dossier_pin(),
        )
    tampered = deepcopy(material)
    tampered["pre_analysis"] = {}
    with pytest.raises(BlockContractError, match="workflow-config-research-dossier"):
        _config._validate_config(tampered)


def test_passive_executor_identity_survives_json_persistence_and_rehydration(app):
    user, _project, record, operation = _state()
    material = freeze(
        operation,
        record,
        user=user,
        settings=_settings(
            execution_mode="passive",
            translator_model="",
        ),
        window_profile=_window_profile(),
        executor_profile=_executor_profile(),
    )

    identity = material["translation"]["model"]
    assert identity["identity_kind"] == "external_executor"
    assert identity["capability_profile"]["missing_metadata"] == []
    row = persist(operation, record, material)
    db.session.commit()

    assert rehydrate(row, operation=operation, source_plan=record) is row


def test_enabled_block_notes_select_the_note_aware_writable_contract(app):
    user, _project, record, operation = _state()
    material = freeze(
        operation,
        record,
        user=user,
        settings=_settings(enable_translator_notes=True),
        window_profile=_window_profile(),
    )

    assert material["enable_translator_notes"] is True
    assert (
        material["writable_source_contract_version"]
        == "translation-block-writable-source-v5"
    )


def test_annotation_profile_selection_migrates_legacy_bool_and_prefers_explicit(app):
    user, _project, record, operation = _state()

    legacy_disabled = freeze(
        operation,
        record,
        user=user,
        settings=_settings(enable_translator_notes=False),
        window_profile=_window_profile(),
    )
    assert legacy_disabled["annotation_profile"] == "none"
    assert legacy_disabled["enable_translator_notes"] is False
    assert (
        legacy_disabled["writable_source_contract_version"]
        == "translation-block-writable-source-v4"
    )

    legacy_enabled = freeze(
        operation,
        record,
        user=user,
        settings=_settings(enable_translator_notes=True),
        window_profile=_window_profile(),
    )
    assert legacy_enabled["annotation_profile"] == "restrained"
    assert legacy_enabled["enable_translator_notes"] is True
    assert (
        legacy_enabled["writable_source_contract_version"]
        == "translation-block-writable-source-v5"
    )

    explicit_study = freeze(
        operation,
        record,
        user=user,
        settings=_settings(annotation_profile="study", enable_translator_notes=False),
        window_profile=_window_profile(),
    )
    assert explicit_study["annotation_profile"] == "study"
    assert explicit_study["enable_translator_notes"] is True
    assert (
        explicit_study["writable_source_contract_version"]
        == "translation-block-writable-source-v6"
    )

    with pytest.raises(BlockContractError, match="workflow-config-annotation-profile"):
        freeze(
            operation,
            record,
            user=user,
            settings=_settings(
                annotation_profile="unrecognized", enable_translator_notes=True
            ),
            window_profile=_window_profile(),
        )


def test_aligned_sentences_freezes_v9_and_accepts_legacy_v7_v8_for_every_profile(app):
    user, _project, record, operation = _state()
    for profile in ("none", "restrained", "study"):
        settings = _settings(
            translation_unit_mode="aligned_sentences",
            annotation_profile=profile,
            enable_translator_notes=profile != "none",
        )
        material = freeze(
            operation,
            record,
            user=user,
            settings=settings,
            window_profile=_window_profile(),
        )
        assert material["translation_unit_mode"] == "aligned_sentences"
        assert material["writable_source_contract_version"] == (
            "translation-block-writable-source-v9"
        )
        for version in (
            "translation-block-writable-source-v7",
            "translation-block-writable-source-v8",
        ):
            legacy = deepcopy(material)
            legacy["writable_source_contract_version"] = version
            _config._validate_config(legacy)
            assert legacy["writable_source_contract_version"] == version

        tampered = deepcopy(material)
        tampered["writable_source_contract_version"] = (
            "translation-block-writable-source-v4"
        )
        with pytest.raises(
            BlockContractError, match="workflow-config-writable-source-contract"
        ):
            _config._validate_config(tampered)

        missing_profile = deepcopy(material)
        missing_profile.pop("annotation_profile")
        with pytest.raises(
            BlockContractError, match="workflow-config-writable-source-contract"
        ):
            _config._validate_config(missing_profile)

    block_with_v7 = freeze(
        operation,
        record,
        user=user,
        settings=_settings(),
        window_profile=_window_profile(),
    )
    block_with_v7["writable_source_contract_version"] = (
        "translation-block-writable-source-v7"
    )
    with pytest.raises(
        BlockContractError, match="workflow-config-writable-source-contract"
    ):
        _config._validate_config(block_with_v7)


def test_historical_config_without_translation_unit_mode_rehydrates_as_block(app):
    user, _project, record, operation = _state()
    material = freeze(
        operation,
        record,
        user=user,
        settings=_settings(),
        window_profile=_window_profile(),
    )
    historical = deepcopy(material)
    historical.pop("translation_unit_mode")
    assert _config._validate_config(historical) == historical


def test_explicit_scope_and_reasoning_are_frozen_for_the_operation(app):
    user, _project, record, operation = _state()
    material = freeze(
        operation,
        record,
        user=user,
        settings=_settings(),
        window_profile=_window_profile(),
        translation_scope={
            "contract_version": "translator-translation-scope-v1",
            "mode": "explicit",
            "block_ids": ["block-a", "block-b"],
        },
        translation_reasoning="high",
    )

    assert material["translation_scope"] == {
        "contract_version": "translator-translation-scope-v1",
        "mode": "explicit",
        "block_ids": ["block-a", "block-b"],
    }
    assert material["translation"]["reasoning"]["config"] == {"level": "high"}
    row = persist(operation, record, material)
    db.session.commit()
    assert rehydrate(row, operation=operation, source_plan=record) is row


def test_explicit_model_default_reasoning_ignores_saved_preference(app):
    user, _project, record, operation = _state()
    user.addon_settings = {
        "translator": {
            "reasoning": {
                "slots": {
                    "translation": {
                        "model_id": "config-provider/chat",
                        "level": "high",
                    }
                }
            }
        }
    }
    db.session.commit()

    material = freeze(
        operation,
        record,
        user=user,
        settings=_settings(),
        window_profile=_window_profile(),
        translation_reasoning="default",
    )

    assert material["translation"]["reasoning"]["config"] is None


def test_historical_config_without_writable_contract_defaults_to_v1(app):
    user, _project, record, operation = _state()
    material = freeze(
        operation,
        record,
        user=user,
        settings=_settings(),
        window_profile=_window_profile(),
    )
    historical = deepcopy(material)
    historical.pop("writable_source_contract_version")
    assert _config._validate_config(historical) == historical


def test_reasoning_owner_must_match_operation_owner(app):
    _user, _project, record, operation = _state()
    other = User(email="other-config@example.test", password_hash="x")
    db.session.add(other)
    db.session.commit()

    with pytest.raises(BlockContractError, match="workflow-config-user"):
        freeze(
            operation,
            record,
            user=other,
            settings=_settings(),
            window_profile=_window_profile(),
        )


def test_legacy_and_explicit_owner_policy_must_not_conflict(app):
    user, _project, record, operation = _state()
    material = freeze(
        operation,
        record,
        user=user,
        settings=_settings(
            custom_prompt="Preserve the author's register.",
            owner_policy="Preserve the author's register.",
        ),
        window_profile=_window_profile(),
    )
    assert material["owner_policy"] == "Preserve the author's register."

    with pytest.raises(BlockContractError, match="workflow-config-owner-policy"):
        freeze(
            operation,
            record,
            user=user,
            settings=_settings(custom_prompt="Policy A", owner_policy="Policy B"),
            window_profile=_window_profile(),
        )


def test_nonblank_policy_and_exact_retry_are_immutable(app):
    user, _project, record, operation = _state()
    material = freeze(
        operation,
        record,
        user=user,
        settings=_settings(owner_policy="Preserve the author's register."),
        window_profile=_window_profile(),
    )
    row = persist(operation, record, material)
    db.session.commit()
    assert row.config_json["owner_policy"] == "Preserve the author's register."
    same = persist(operation, record, material)
    assert same.id == row.id
    changed = dict(material)
    changed["window_profile"] = {**material["window_profile"], "max_blocks": 5}
    with pytest.raises(BlockContractError, match="workflow-config-conflict"):
        persist(operation, record, changed)


def test_unknown_profile_keys_and_bool_capacity_fail_before_mutation(app):
    user, _project, record, operation = _state()
    with pytest.raises(BlockContractError):
        freeze(
            operation,
            record,
            user=user,
            settings=_settings(),
            window_profile={**_window_profile(), "chunk_size_chars": 4000},
        )
    with pytest.raises(BlockContractError):
        freeze(
            operation,
            record,
            user=user,
            settings=_settings(),
            window_profile={**_window_profile(), "max_blocks": True},
        )
    assert db.session.query(TranslationBlockWorkflowConfig).count() == 0


def test_v2_window_profile_freezes_separate_batch_input_target(app):
    user, _project, record, operation = _state()
    profile = {
        **_window_profile(),
        "profile_version": "2",
        "target_input_tokens": 16_000,
    }
    material = freeze(
        operation,
        record,
        user=user,
        settings=_settings(),
        window_profile=profile,
    )
    assert material["window_profile"] == profile

    with pytest.raises(BlockContractError, match="workflow-config-window-profile"):
        freeze(
            operation,
            record,
            user=user,
            settings=_settings(),
            window_profile={
                key: value
                for key, value in profile.items()
                if key != "target_input_tokens"
            },
        )


def test_v3_window_profile_freezes_bounded_output_request_contract(app):
    user, _project, record, operation = _state()
    profile = {
        **_window_profile(),
        "profile_version": "3",
        "target_input_tokens": 16_000,
    }
    material = freeze(
        operation,
        record,
        user=user,
        settings=_settings(),
        window_profile=profile,
    )
    assert material["window_profile"] == profile


def test_v5_window_profile_freezes_unbounded_provider_output_contract(app):
    user, _project, record, operation = _state()
    profile = {
        **_window_profile(),
        "profile_version": "5",
        "target_input_tokens": 16_000,
    }
    material = freeze(
        operation,
        record,
        user=user,
        settings=_settings(),
        window_profile=profile,
    )
    assert material["window_profile"] == profile


def test_model_key_drift_fails_rehydration_without_secret_exposure(app):
    user, _project, record, operation = _state()
    material = freeze(
        operation,
        record,
        user=user,
        settings=_settings(),
        window_profile=_window_profile(),
    )
    row = persist(operation, record, material)
    db.session.commit()
    assert material["translation"]["model"]["transport_identity"] == {
        "contract_version": "translator-model-transport-v1",
        "provider_name": "config-provider",
        "provider_kind": "native",
        "api_base": None,
        "api_version": None,
        "vertex_project": None,
        "vertex_location": None,
    }
    key = db.session.query(ApiKey).one()
    key.api_key = "rotated-secret"
    db.session.commit()
    with pytest.raises(BlockContractError, match="workflow-config-model-drift"):
        rehydrate(row, operation=operation, source_plan=record)
    assert rehydrate_historical(row, operation=operation, source_plan=record) is row
    assert "secret-value" not in repr(material)
    assert "rotated-secret" not in repr(material)


def test_frozen_capability_profile_survives_catalog_drift(app, monkeypatch):
    user, _project, record, operation = _state()
    material = freeze(
        operation,
        record,
        user=user,
        settings=_settings(),
        window_profile=_window_profile(),
    )
    frozen = dict(material["translation"]["model"]["capability_profile"])
    row = persist(operation, record, material)
    db.session.commit()
    original = _config.resolve_model_capabilities

    def changed_catalog(*args, **kwargs):
        profile = original(*args, **kwargs)
        return replace(
            profile,
            max_input_tokens=profile.max_input_tokens + 10_000,
            usable_input_tokens=profile.usable_input_tokens + 10_000,
        )

    monkeypatch.setattr(_config, "resolve_model_capabilities", changed_catalog)

    assert rehydrate(row, operation=operation, source_plan=record) is row
    execution = execution_profile_from_frozen_phase(row.config_json["translation"])
    assert execution.capability_profile["capability_profile"] == frozen
    assert (
        execution.capability_profile["capability_profile_hash"]
        == row.config_json["translation"]["model"]["capability_profile_hash"]
    )


def test_embedding_projection_is_optional_but_pinned_when_ready(app):
    user, _project, record, operation = _state()
    embedding = LLMModel(
        user_id=None,
        provider_id=db.session.query(LLMProvider).one().id,
        model_id="config-provider/embed",
        display_name="Configured Embeddings",
        is_active=True,
        manually_added=True,
        model_type="embedding",
        default_dimensions=768,
        supports_mrl=False,
    )
    db.session.add_all(
        [embedding, Setting(key="knowledge_embedding_model", value=embedding.model_id)]
    )
    db.session.commit()
    material = freeze(
        operation,
        record,
        user=user,
        settings=_settings(),
        window_profile=_window_profile(),
    )
    assert material["embedding_projection"]["enabled"] is True
    row = persist(operation, record, material)
    db.session.commit()
    assert rehydrate(row, operation=operation, source_plan=record) is row
    setting = db.session.get(Setting, "knowledge_embedding_model")
    assert setting is not None
    setting.value = "some-other-embedding-model"
    db.session.commit()
    assert rehydrate(row, operation=operation, source_plan=record) is row
    embedding.is_active = False
    db.session.commit()
    with pytest.raises(BlockContractError, match="workflow-config-embedding-drift"):
        rehydrate(row, operation=operation, source_plan=record)


def test_research_phase_is_frozen_as_selected_read_only_evidence(app):
    user, _project, record, operation = _state()
    research = {
        "budget": {"max_model_calls": 2, "max_tool_calls": 1, "output_tokens": 2000},
        "tool_declarations": [
            {
                "tool_name": "search",
                "tool_id": "tool.search",
                "provider_id": "web-provider",
                "provider_domain": "research",
                "addon_id": "web-addon",
                "connection_mode": "saved",
                "connection_id": "web-connection",
                "read_only": True,
                "declaration": {
                    "type": "function",
                    "function": {
                        "name": "search",
                        "description": "Search",
                        "parameters": {"type": "object"},
                    },
                },
            }
        ],
        "selected_provider_ids": ["web-provider"],
        "selected_addon_ids": ["web-addon"],
    }
    material = freeze(
        operation,
        record,
        user=user,
        settings=_settings(
            enable_research=True,
            researcher_model="config-provider/chat",
        ),
        window_profile=_window_profile(),
        research=research,
    )
    assert material["research"]["selected"]["tool_declarations"][0]["read_only"] is True
    assert (
        material["research"]["input_planner_contract"]
        == "document-block-research-input-planner-v3"
    )
    legacy = deepcopy(material)
    legacy["research"].pop("input_planner_contract")
    assert _evidence_reuse._research_descriptor(legacy) != (
        _evidence_reuse._research_descriptor(material)
    )
    unsupported_budget = deepcopy(material)
    unsupported_budget["research"]["selected"]["budget"]["output_tokens"] = (
        material["research"]["model"]["capability_profile"]["max_output_tokens"] + 1
    )
    with pytest.raises(BlockContractError, match="workflow-config-research-budget"):
        persist(operation, record, unsupported_budget)
    row = persist(operation, record, material)
    db.session.commit()
    assert persist(operation, record, material).id == row.id
    assert rehydrate(row, operation=operation, source_plan=record) is row


def test_operation_project_plan_scope_is_composite_and_uncertain_state_is_allowed(app):
    _user, _project, _record, _operation = _state()
    columns = {
        row[1]
        for row in db.session.execute(
            text("PRAGMA table_info('translation_block_workflow_config')")
        )
    }
    assert {"operation_id", "owner_user_id", "project_id", "source_plan_id"} <= columns
    window = db.session.execute(
        text("SELECT sql FROM sqlite_master WHERE name='translation_window_attempt'")
    ).scalar_one()
    assert "provider_outcome_uncertain" in window
    assert {
        "source_content_hash",
        "operation_version",
        "queue_job_id",
        "operation_lease_expires_at",
        "lease_fence",
    } <= {
        row[1]
        for row in db.session.execute(
            text("PRAGMA table_info('translation_window_attempt')")
        )
    }
