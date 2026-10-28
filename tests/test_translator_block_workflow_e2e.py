"""Encrypted-SQLite acceptance coverage for the block workflow conductor.

The provider is deliberately fake, but all Translator persistence boundaries
remain real: source-plan/config receipts, analysis attempts and artifacts,
context baseline, window attempts, active target pointers, and alignment.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import timedelta
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from content_storage_support import bind_content_policy
from flask import Flask

from carlina.core.semantic_html import SEMANTIC_HTML_CONTRACT_VERSION
from carlina.core.utc import utc_now_db
from iarf_assistant.core.mcp_completion import passive_execution
from iarf_assistant.core.mcp_gateway import _work_claim_result
from iarf_assistant.core.mcp_jobs import (
    claim_next_work,
    create_execution,
    issue_access_key,
    resolve_access_key,
    submit_work,
)
from iarf_assistant.core.task_queue import ExternalWorkPending
from iarf_assistant.llm_service import LLMService

_ROOT = "iarf" + "_assistant"
_translator = f"{_ROOT}.addons.translator"
_contract = import_module(f"{_translator}.block_contract")
_persistence = import_module(f"{_translator}.block_persistence")
_planner = import_module(f"{_translator}.block_planner")
_continuation = import_module(f"{_translator}.block_continuation")
_workflow_config = import_module(f"{_translator}.block_workflow_config")
_workflow_runtime = import_module(f"{_translator}.block_workflow_runtime")
_evidence_reuse = import_module(f"{_translator}.evidence_reuse")
_analysis_attempts = import_module(f"{_translator}.analysis_attempts")
_context_baselines = import_module(f"{_translator}.context_baselines")
_research_persistence = import_module(f"{_translator}.research_persistence")
_scholarly_review = import_module(f"{_translator}.scholarly_review")
_analysis_scope_contract = import_module(f"{_translator}.analysis_scope_contract")
_workspace_api = import_module(f"{_translator}.block_workspace_api")
_target_contract = import_module(f"{_translator}.target_contract")
_target_persistence = import_module(f"{_translator}.target_persistence")
_window_execution = import_module(f"{_translator}.window_execution")
_window_persistence = import_module(f"{_translator}.window_persistence")
_operations = import_module(f"{_translator}.operations")
_models = import_module(f"{_translator}.models")
_core_models = import_module(f"{_ROOT}.models")
_encryption = import_module(f"{_ROOT}.core.encryption")
_extensions = import_module(f"{_ROOT}.extensions")

AttestedDocumentSnapshot = _contract.AttestedDocumentSnapshot
DOCUMENT_BLOCK_ENGINE_CONTRACT = _contract.DOCUMENT_BLOCK_ENGINE_CONTRACT
TranslationOperation = _models.TranslationOperation
TranslationAnalysisAttempt = _models.TranslationAnalysisAttempt
TranslationOperationEvidenceReuse = _models.TranslationOperationEvidenceReuse
TranslationProject = _models.TranslationProject
TranslationShareGrant = _models.TranslationShareGrant
TranslationTargetAlignmentActive = _models.TranslationTargetAlignmentActive
TranslationTargetBlockActive = _models.TranslationTargetBlockActive
TranslationWindowAttempt = _models.TranslationWindowAttempt
ApiKey = _core_models.ApiKey
LLMModel = _core_models.LLMModel
LLMProvider = _core_models.LLMProvider
User = _core_models.User
db = _extensions.db

_PASSIVE_EXECUTOR_PROFILE = {
    "schema_version": "carlina.external-executor.v1",
    "max_input_tokens": 32_000,
    "max_output_tokens": 4_000,
    "input_modalities": ["text"],
    "supports_tools": False,
    "supports_structured_output": True,
    "supports_reasoning": True,
    "user_confirmed": True,
}


@pytest.fixture
def app(tmp_path: Path):
    application = Flask(__name__, instance_path=str(tmp_path / "instance"))
    bind_content_policy(application)
    application.config.update(
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{tmp_path / 'workflow-e2e.db'}",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SECRET_KEY="workflow-e2e-test",
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


def _candidate(
    plan: Any,
    record: Any,
    block: Any,
    translated_text: str = "Przekład",
) -> dict[str, object]:
    """Return valid provider output, including all protected inline tokens."""

    parts: list[Any] = []
    slots = {slot.id: slot for slot in block.slots}
    for wire_part in block.wire:
        if wire_part.kind == "token":
            parts.append(
                _target_contract.TargetTextPart(kind="token", token_id=wire_part.ref_id)
            )
            continue
        slot = slots[wire_part.ref_id]
        if slot.disposition == "translatable" and slot.kind != "attribute":
            # Keep text inside protected inline pairs: empty <em>/<a>/<sup>
            # is structurally valid but not semantically acceptable output.
            parts.append(
                _target_contract.TargetTextPart(kind="text", text=translated_text)
            )
        elif slot.disposition == "literal-preserved":
            parts.append(
                _target_contract.TargetTextPart(kind="literal", literal_slot_id=slot.id)
            )
    lexical = tuple(
        _target_contract.TargetLexicalAttribute(slot_id=slot.id, value=translated_text)
        for slot in block.slots
        if slot.kind == "attribute" and slot.disposition == "translatable"
    )
    return _target_contract.TargetBlockCandidate(
        source_plan_id=record.id,
        source_plan_hash=plan.plan_hash,
        block_id=block.id,
        structural_fingerprint=_target_contract.structural_fingerprint(plan, block),
        parts=tuple(parts),
        lexical_attributes=lexical,
    ).to_dict()


class _DeterministicProvider:
    """Strict provider boundary fake; no persistence or workflow calls are mocked."""

    def __init__(self, plan: Any, record: Any, *, with_research: bool = False):
        self.plan = plan
        self.record = record
        self.with_research = with_research
        self.calls: list[str] = []
        self._translation_index = 0

    def completion(self, **kwargs: Any) -> dict[str, object]:
        source = kwargs["source"]
        self.calls.append(source)
        messages = kwargs["messages"]
        if source == "translator_block_analysis":
            payload = json.loads(messages[-1]["content"])
            if "map_results" in payload:
                response = {
                    "schema_version": "analysis-reduce-v1",
                    "source_plan_hash": payload["source_plan_hash"],
                    "document_brief": {
                        "summary": "A witnessed fixture document.",
                        "domains": ["testing"],
                        "audience": "test readers",
                    },
                    "style_guide": {
                        "register": "neutral",
                        "guidance": ["Preserve protected content."],
                        "prohibitions": ["Do not invent facts."],
                    },
                    "entity_register": {"entities": []},
                    "glossary_proposals": [],
                    "unresolved": (
                        [
                            {
                                "question": "Which established rendering best fits this witnessed term?",
                                "reason": "The source alone does not settle the convention.",
                                "impact": "medium",
                                "source_block_ids": [payload["mapped_block_ids"][0]],
                            }
                        ]
                        if self.with_research
                        else []
                    ),
                    "scope_hashes": payload["scope_hashes"],
                }
            else:
                scope = payload["scope"]
                blocks = payload["source_blocks"]
                block_ids = [block["block_id"] for block in blocks]
                response = {
                    "schema_version": "analysis-map-v1",
                    "source_plan_hash": scope["source_plan_hash"],
                    "part_index": scope["part_index"],
                    "block_ids": block_ids,
                    "summary": {
                        "text": "Witnessed analysis scope.",
                        "domains": ["testing"],
                        "audience": "test readers",
                        "source_block_ids": [block_ids[0]],
                    },
                    "style": {
                        "register": "neutral",
                        "guidance": ["Preserve meaning."],
                        "prohibitions": ["Do not add facts."],
                        "source_block_ids": [block_ids[0]],
                    },
                    "entities": [],
                    "terminology_candidates": [],
                    "unresolved": [],
                }
            return {
                "choices": [
                    {"message": {"role": "assistant", "content": json.dumps(response)}}
                ]
            }
        if source == "translator_block_research":
            assert self.with_research
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "Use the established Polish rendering witnessed by the research record.",
                        }
                    }
                ]
            }
        if source == "translator_block_translation_window":
            content = messages[-1]["content"]
            marker = "\n[writable_source target_language="
            marker_start = content.rfind(marker)
            assert marker_start >= 0
            json_start = content.find("\n", marker_start + 1) + 1
            payload = json.loads(content[json_start:])
            assert payload["contract_version"] == "translation-block-writable-source-v4"
            assert set(payload) == {"contract_version", "blocks"}
            # max_blocks=1 below deliberately makes this correspondence exact
            # without exposing durable block identities to the provider.
            assert len(payload["blocks"]) == 1
            writable = payload["blocks"][0]
            assert writable["n"] == 1
            assert self._translation_index < len(self.plan.blocks)
            self._translation_index += 1
            translated_content = writable["content"].replace("protected", "Przekład")
            if translated_content == writable["content"] and translated_content:
                translated_content = f"Przekład: {translated_content}"
            response = {
                "results": [
                    {
                        "n": writable["n"],
                        "content": translated_content,
                        "fields": [
                            {**field, "value": f"Przekład: {field['value']}"}
                            for field in writable["fields"]
                        ],
                    }
                ]
            }
            return {
                "choices": [
                    {"message": {"role": "assistant", "content": json.dumps(response)}}
                ]
            }
        raise AssertionError(f"Unexpected provider source: {source}")


def _state(
    *,
    enable_research: bool = False,
    legacy_analysis_planner: bool = False,
    passive: bool = False,
) -> tuple[Any, Any, Any, Any, Any]:
    user = User(email="block-workflow-e2e@example.test", password_hash="x")
    db.session.add(user)
    db.session.flush()
    provider = LLMProvider(user_id=None, name="e2e-provider", provider_kind="native")
    db.session.add(provider)
    db.session.flush()
    db.session.add_all(
        [
            ApiKey(
                user_id=None,
                provider_id=provider.id,
                name="primary",
                api_key="test-only-provider-secret",
                is_active=True,
            ),
            LLMModel(
                user_id=None,
                provider_id=provider.id,
                model_id="e2e-provider/chat",
                display_name="E2E Chat",
                is_active=True,
                manually_added=True,
                model_type="chat",
                context_window=1_000_000,
                max_output_tokens=128_000,
            ),
        ]
    )
    project = TranslationProject(
        user_id=user.id,
        name="Block workflow E2E",
        source_lang="en",
        target_lang="pl",
        engine_contract=DOCUMENT_BLOCK_ENGINE_CONTRACT,
        settings={
            "translator_model": "e2e-provider/chat",
            **(
                {
                    "execution_mode": "passive",
                    "executor_profile": _PASSIVE_EXECUTOR_PROFILE,
                }
                if passive
                else {}
            ),
            "source_reference": {
                "version": 1,
                "capability": "document.processing",
                "provider_addon_id": "doc-provider",
                "provider_capability_id": "process",
                "resource_type": "document",
                "resource_id": "workflow-e2e-document",
            },
        },
    )
    db.session.add(project)
    db.session.flush()
    first_paragraph = (
        "<p>First <a href=\"https://example.test\"><em>protected</em>"
        "</a> paragraph.</p>"
        if passive
        else "<p>First <em>protected</em> paragraph.</p>"
    )
    html = (
        "<html><body><h1>Title</h1>"
        f"{first_paragraph}"
        "<p>Second paragraph.</p></body></html>"
    )
    digest = hashlib.sha256(html.encode("utf-8")).hexdigest()
    plan = _planner.plan_document_blocks(
        AttestedDocumentSnapshot(
            id="workflow-e2e-document",
            document_id="workflow-e2e-document",
            output_html=html,
            content_hash=digest,
            semantic_content_hash=digest,
            metadata={},
            provenance={},
            captured_at="2026-08-26T00:00:00+00:00",
            semantic_contract_version=SEMANTIC_HTML_CONTRACT_VERSION,
        ),
        project_id=project.id,
        source_attestation=_persistence.source_attestation_for_project(project),
        source_language="en",
    )
    record, _created = _persistence.persist_source_plan(project, plan)
    operation = TranslationOperation(
        project_id=project.id,
        owner_user_id=user.id,
        kind="document_block_translate",
        idempotency_key="workflow-e2e-operation",
        request_fingerprint="e" * 64,
        state="running",
        queue_job_id="workflow-e2e-worker",
        source_content_hash=record.source_content_hash,
        source_plan_id=record.id,
        source_plan_hash=record.plan_hash,
        lease_expires_at=utc_now_db() + timedelta(minutes=10),
    )
    db.session.add(operation)
    db.session.commit()
    profile = (
        _window_execution.execution_profile_from_frozen_phase(
            {
                "model": _workflow_config._external_executor_identity(
                    _PASSIVE_EXECUTOR_PROFILE
                )
            }
        )
        if passive
        else _window_execution.resolve_execution_profile(project)
    )
    workflow_settings: dict[str, Any] = {
        "translator_model": "e2e-provider/chat",
        "enable_pre_analysis": not passive,
        "enable_research": enable_research and not passive,
        "owner_policy": "Preserve document meaning and structure.",
    }
    if passive:
        workflow_settings["execution_mode"] = "passive"
    else:
        workflow_settings["analyzer_model"] = "e2e-provider/chat"
    if enable_research and not passive:
        workflow_settings["researcher_model"] = "e2e-provider/chat"
    translation_scope = (
        {
            "contract_version": _continuation.TRANSLATION_SCOPE_CONTRACT_VERSION,
            "mode": "explicit",
            "block_ids": [plan.blocks[1].id],
        }
        if passive
        else None
    )
    material = _workflow_config.freeze_block_workflow_config(
        operation,
        record,
        user=user,
        settings=workflow_settings,
        window_profile=_translator_window_profile(profile),
        executor_profile=_PASSIVE_EXECUTOR_PROFILE if passive else None,
        translation_scope=translation_scope,
        research=(
            {
                "budget": {
                    "max_model_calls": 2,
                    "max_tool_calls": 0,
                    "output_tokens": 128_000,
                },
                "tool_declarations": [],
                "selected_provider_ids": [],
                "selected_addon_ids": [],
            }
            if enable_research
            else None
        ),
    )
    if legacy_analysis_planner:
        material["pre_analysis"].pop("scope_planner_contract")
    _workflow_config.persist_block_workflow_config(operation, record, material)
    db.session.commit()
    return user, project, record, operation, plan


def _translator_window_profile(profile: Any) -> dict[str, object]:
    return _planner_window_profile(profile).to_dict()


def _planner_window_profile(profile: Any) -> Any:
    return import_module(f"{_translator}.block_translation").derive_window_profile(
        profile, maximum_blocks=1
    )


def _safe_boundary(
    operation: Any, progress: float, message: str, checkpoint: Any
) -> Any:
    updated = _operations.update_progress(
        operation, progress=progress, message=message, checkpoint=dict(checkpoint)
    )
    db.session.commit()
    return updated


def _submit_dossier(project: Any, user: Any) -> tuple[Any, dict[str, str]]:
    result = _scholarly_review.submit_research_dossier(
        project,
        client_submission_id="workflow-dossier-submission",
        payload={"document_brief": "An externally supplied research brief."},
        provenance={"method": "fixture"},
        scope={"kind": "document", "block_ids": []},
        actor=_scholarly_review.ReviewActor("owner", user.id),
    )
    revision = db.session.get(
        _models.TranslationResearchDossierRevision, result["revision_id"]
    )
    assert revision is not None
    return revision, {
        "revision_id": revision.id,
        "record_hash": revision.record_hash,
        "content_digest": revision.content_digest,
        "conclusion_provenance": revision.conclusion_provenance,
        "evidence_verification_state": revision.evidence_verification_state,
    }


def test_pinned_research_dossier_is_passed_verbatim_to_baseline_before_translation(
    app, monkeypatch
):
    user, project, record, operation, plan = _state()
    revision, pin = _submit_dossier(project, user)
    existing = _models.TranslationBlockWorkflowConfig.query.filter_by(
        operation_id=operation.id
    ).one()
    db.session.delete(existing)
    db.session.flush()
    profile = _window_execution.resolve_execution_profile(project)
    material = _workflow_config.freeze_block_workflow_config(
        operation,
        record,
        user=user,
        settings={
            "translator_model": "e2e-provider/chat",
            "enable_pre_analysis": False,
            "enable_research": False,
        },
        window_profile=_translator_window_profile(profile),
        research_dossier=pin,
    )
    _workflow_config.persist_block_workflow_config(operation, record, material)
    db.session.commit()

    original_freeze = _workflow_runtime.freeze_context_baseline
    received: dict[str, Any] = {}

    def capture_baseline(*args: Any, **kwargs: Any) -> Any:
        received["revision"] = kwargs.get("research_dossier_revision")
        return original_freeze(*args, **kwargs)

    monkeypatch.setattr(
        _workflow_runtime, "freeze_context_baseline", capture_baseline
    )
    provider = _DeterministicProvider(plan, record)
    _completed, result = _workflow_runtime.run_document_block_workflow(
        project=project,
        operation=operation,
        service=provider,
        addon_manager=object(),
        safe_boundary=_safe_boundary,
    )

    assert received["revision"] is revision
    assert provider.calls.count("translator_block_analysis") == 0
    assert provider.calls.count("translator_block_research") == 0
    assert provider.calls.count("translator_block_translation_window") == len(
        plan.blocks
    )
    assert result["translated_block_count"] == len(plan.blocks)


def test_missing_pinned_research_dossier_fails_before_provider_calls(app):
    user, project, record, operation, plan = _state()
    existing = _models.TranslationBlockWorkflowConfig.query.filter_by(
        operation_id=operation.id
    ).one()
    db.session.delete(existing)
    db.session.flush()
    profile = _window_execution.resolve_execution_profile(project)
    material = _workflow_config.freeze_block_workflow_config(
        operation,
        record,
        user=user,
        settings={
            "translator_model": "e2e-provider/chat",
            "enable_pre_analysis": False,
            "enable_research": False,
        },
        window_profile=_translator_window_profile(profile),
        research_dossier={
            "revision_id": "missing-dossier-revision",
            "record_hash": "a" * 64,
            "content_digest": "b" * 64,
            "conclusion_provenance": "external_unverified",
            "evidence_verification_state": "unverified",
        },
    )
    _workflow_config.persist_block_workflow_config(operation, record, material)
    db.session.commit()
    provider = _DeterministicProvider(plan, record)
    with pytest.raises(_contract.BlockContractError, match="block-workflow-dossier"):
        _workflow_runtime.run_document_block_workflow(
            project=project,
            operation=operation,
            service=provider,
            addon_manager=object(),
            safe_boundary=_safe_boundary,
        )
    assert provider.calls == []


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("record_hash", "c" * 64),
        ("content_digest", "d" * 64),
        ("project_id", "cross-project"),
        ("source_plan_id", "cross-plan"),
    ),
)
def test_tampered_or_cross_scope_pinned_dossier_fails_before_provider_calls(
    app, field, value
):
    user, project, record, operation, plan = _state()
    revision, pin = _submit_dossier(project, user)
    existing = _models.TranslationBlockWorkflowConfig.query.filter_by(
        operation_id=operation.id
    ).one()
    db.session.delete(existing)
    db.session.flush()
    profile = _window_execution.resolve_execution_profile(project)
    material = _workflow_config.freeze_block_workflow_config(
        operation,
        record,
        user=user,
        settings={
            "translator_model": "e2e-provider/chat",
            "enable_pre_analysis": False,
            "enable_research": False,
        },
        window_profile=_translator_window_profile(profile),
        research_dossier=pin,
    )
    _workflow_config.persist_block_workflow_config(operation, record, material)
    db.session.commit()
    setattr(revision, field, value)
    provider = _DeterministicProvider(plan, record)
    with db.session.no_autoflush:
        with pytest.raises(
            _contract.BlockContractError, match="block-workflow-dossier"
        ):
            _workflow_runtime.run_document_block_workflow(
                project=project,
                operation=operation,
                service=provider,
                addon_manager=object(),
                safe_boundary=_safe_boundary,
            )
    assert provider.calls == []


def test_analysis_prefers_one_full_document_scope_when_exact_packet_fits(app):
    _user, _project, record, operation, plan = _state()
    frozen = _models.TranslationBlockWorkflowConfig.query.filter_by(
        operation_id=operation.id
    ).one()
    workflow = _workflow_config.rehydrate_block_workflow_config(
        frozen, operation=operation, source_plan=record
    )
    phase = workflow.config_json["pre_analysis"]

    scopes = _workflow_runtime._analysis_scopes_for_operation(
        plan=plan,
        operation=operation,
        source_plan=record,
        phase=phase,
        budget=_workflow_runtime._analysis_budget(phase),
        token_counter=lambda **_kwargs: 10,
    )

    assert len(scopes) == 1
    assert scopes[0].block_ids == tuple(block.id for block in plan.blocks)


def test_analysis_fallback_packs_complete_blocks_with_the_model_counter(app):
    _user, _project, record, operation, plan = _state()
    frozen = _models.TranslationBlockWorkflowConfig.query.filter_by(
        operation_id=operation.id
    ).one()
    workflow = _workflow_config.rehydrate_block_workflow_config(
        frozen, operation=operation, source_plan=record
    )
    phase = workflow.config_json["pre_analysis"]

    def counter(**kwargs):
        messages = kwargs.get("messages")
        if messages:
            payload = json.loads(messages[-1]["content"])
            return 10 if len(payload["source_blocks"]) == 1 else 1_000_000
        return 1

    scopes = _workflow_runtime._analysis_scopes_for_operation(
        plan=plan,
        operation=operation,
        source_plan=record,
        phase=phase,
        budget=_workflow_runtime._analysis_budget(phase),
        token_counter=counter,
    )

    assert [scope.block_ids for scope in scopes] == [
        (block.id,) for block in plan.blocks
    ]


def test_conductor_runs_full_evaluation_off_workflow_with_real_encrypted_state(app):
    _user, project, record, operation, plan = _state()
    provider = _DeterministicProvider(plan, record)

    completed, result = _workflow_runtime.run_document_block_workflow(
        project=project,
        operation=operation,
        service=provider,
        addon_manager=object(),
        safe_boundary=_safe_boundary,
    )

    assert completed.id == operation.id
    assert result["translated_block_count"] == len(plan.blocks)
    assert len(result["window_attempt_ids"]) == len(plan.blocks) >= 2
    assert len(result["active_revision_ids"]) == len(plan.blocks)
    assert len(result["alignment_revision_ids"]) == len(plan.blocks)
    assert result["research_run_id"] is None
    assert provider.calls.count("translator_block_analysis") == 2
    assert provider.calls.count("translator_block_translation_window") == len(
        plan.blocks
    )
    assert db.session.query(TranslationWindowAttempt).count() == len(plan.blocks)
    assert db.session.query(TranslationTargetBlockActive).count() == len(plan.blocks)
    assert db.session.query(TranslationTargetAlignmentActive).count() == len(
        plan.blocks
    )

    # Rendering reads the persisted plan and active alignment, not fake output.
    rendered = _target_persistence.render_persisted_target_document(record)
    assert "Przekład" in rendered
    assert "<em" in rendered and ">Przekład</em>" in rendered

    # The exact source/config evidence remains independently rehydratable.
    assert _persistence.rehydrate_source_plan(record).plan_hash == record.plan_hash
    frozen = _models.TranslationBlockWorkflowConfig.query.filter_by(
        operation_id=operation.id
    ).one()
    assert (
        _workflow_config.rehydrate_block_workflow_config(
            frozen, operation=operation, source_plan=record
        ).config_hash
        == result["workflow_config_hash"]
    )
    assert completed.checkpoint_json["phase"] == "translation"

    calls_before_recovery = list(provider.calls)
    recovered, recovered_result = _workflow_runtime.run_document_block_workflow(
        project=project,
        operation=completed,
        service=provider,
        addon_manager=object(),
        safe_boundary=_safe_boundary,
    )
    assert recovered.id == completed.id
    assert recovered_result == result
    assert provider.calls == calls_before_recovery


def test_explicit_scope_retranslates_one_active_block_without_touching_others(app):
    user, project, record, operation, plan = _state()
    first_provider = _DeterministicProvider(plan, record)
    completed, first_result = _workflow_runtime.run_document_block_workflow(
        project=project,
        operation=operation,
        service=first_provider,
        addon_manager=object(),
        safe_boundary=_safe_boundary,
    )
    _operations.finish_operation(completed, "completed", result=first_result)
    db.session.commit()
    before = {
        pointer.block_id: (pointer.revision_id, pointer.version)
        for pointer in TranslationTargetBlockActive.query.filter_by(
            source_plan_id=record.id
        ).all()
    }
    selected_block_id = plan.blocks[1].id

    continuation = TranslationOperation(
        project_id=project.id,
        owner_user_id=user.id,
        kind="document_block_translate",
        idempotency_key="workflow-e2e-retranslate-one",
        request_fingerprint="7" * 64,
        state="running",
        queue_job_id="workflow-e2e-retranslate-one-worker",
        source_content_hash=record.source_content_hash,
        source_plan_id=record.id,
        source_plan_hash=record.plan_hash,
        lease_expires_at=utc_now_db() + timedelta(minutes=10),
    )
    db.session.add(continuation)
    db.session.flush()
    profile = _window_execution.resolve_execution_profile(project)
    material = _workflow_config.freeze_block_workflow_config(
        continuation,
        record,
        user=user,
        settings={
            "translator_model": "e2e-provider/chat",
            "enable_pre_analysis": False,
            "enable_research": False,
        },
        window_profile=_translator_window_profile(profile),
        protected_targets=_continuation.snapshot_protected_targets(record),
        translation_scope={
            "contract_version": "translator-translation-scope-v1",
            "mode": "explicit",
            "block_ids": [selected_block_id],
        },
    )
    _workflow_config.persist_block_workflow_config(continuation, record, material)
    db.session.commit()

    second_provider = _DeterministicProvider(plan, record)
    _continued, result = _workflow_runtime.run_document_block_workflow(
        project=project,
        operation=continuation,
        service=second_provider,
        addon_manager=object(),
        safe_boundary=_safe_boundary,
    )
    after = {
        pointer.block_id: (pointer.revision_id, pointer.version)
        for pointer in TranslationTargetBlockActive.query.populate_existing()
        .filter_by(source_plan_id=record.id)
        .all()
    }

    assert result["translated_block_count"] == 1
    assert result["protected_block_count"] == len(plan.blocks) - 1
    assert second_provider.calls.count("translator_block_translation_window") == 1
    assert after[selected_block_id][0] != before[selected_block_id][0]
    assert after[selected_block_id][1] == before[selected_block_id][1] + 1
    assert {
        block_id: value
        for block_id, value in after.items()
        if block_id != selected_block_id
    } == {
        block_id: value
        for block_id, value in before.items()
        if block_id != selected_block_id
    }


def test_new_operation_reuses_exact_analysis_without_provider_calls(app, monkeypatch):
    user, project, record, operation, plan = _state()
    first_provider = _DeterministicProvider(plan, record)
    original_translation = _workflow_runtime.call_translation_window
    monkeypatch.setattr(
        _workflow_runtime,
        "call_translation_window",
        lambda *_args, **_kwargs: "not-json",
    )
    with pytest.raises(_workflow_runtime.BlockWorkflowPartialOutcome) as raised:
        _workflow_runtime.run_document_block_workflow(
            project=project,
            operation=operation,
            service=first_provider,
            addon_manager=object(),
            safe_boundary=_safe_boundary,
        )
    first_result = raised.value.result
    _operations.finish_operation(operation, "partial", result=first_result)
    db.session.commit()

    # Call authority is deliberately rotated. The endpoint and effective model
    # are unchanged, so immutable evidence remains reusable.
    key = db.session.query(ApiKey).one()
    key.api_key = "rotated-test-only-provider-secret"
    db.session.commit()

    continuation = TranslationOperation(
        project_id=project.id,
        owner_user_id=user.id,
        kind="document_block_translate",
        idempotency_key="workflow-e2e-reuse",
        request_fingerprint="9" * 64,
        state="running",
        queue_job_id="workflow-e2e-reuse-worker",
        source_content_hash=record.source_content_hash,
        source_plan_id=record.id,
        source_plan_hash=record.plan_hash,
        lease_expires_at=utc_now_db() + timedelta(minutes=10),
    )
    db.session.add(continuation)
    db.session.flush()
    profile = _window_execution.resolve_execution_profile(project)
    material = _workflow_config.freeze_block_workflow_config(
        continuation,
        record,
        user=user,
        settings={
            "translator_model": "e2e-provider/chat",
            "enable_pre_analysis": True,
            "analyzer_model": "e2e-provider/chat",
            "enable_research": False,
            # Translation policy is deliberately different and must not make
            # immutable document analysis ineligible for reuse.
            "owner_policy": "Prefer concise Polish where the source permits it.",
        },
        window_profile=_translator_window_profile(profile),
    )
    _workflow_config.persist_block_workflow_config(continuation, record, material)
    db.session.commit()

    monkeypatch.setattr(
        _workflow_runtime, "call_translation_window", original_translation
    )
    second_provider = _DeterministicProvider(plan, record)
    _continued, second_result = _workflow_runtime.run_document_block_workflow(
        project=project,
        operation=continuation,
        service=second_provider,
        addon_manager=object(),
        safe_boundary=_safe_boundary,
    )

    reuse = TranslationOperationEvidenceReuse.query.filter_by(
        operation_id=continuation.id
    ).one()
    assert reuse.id == second_result["evidence_reuse_id"]
    assert reuse.origin_operation_id == operation.id
    assert (
        reuse.selection_json["analysis"]["scope_planner_contract"]
        == _analysis_scope_contract.ANALYSIS_SCOPE_PLANNER_CONTRACT_VERSION
    )
    assert (
        reuse.selection_json["analysis"]["scope_plan"]["contract_version"]
        == _analysis_scope_contract.ANALYSIS_SCOPE_PLANNER_CONTRACT_VERSION
    )
    assert (
        second_result["analysis_artifact_ids"] == first_result["analysis_artifact_ids"]
    )
    assert second_result["research_run_id"] is None
    assert second_provider.calls.count("translator_block_analysis") == 0
    assert second_provider.calls.count("translator_block_translation_window") == len(
        plan.blocks
    )

    assert (
        TranslationAnalysisAttempt.query.filter_by(
            operation_id=continuation.id,
            analysis_kind="document_brief",
        ).count()
        == 0
    )

    calls_before_recovery = list(second_provider.calls)
    _recovered, recovered_result = _workflow_runtime.run_document_block_workflow(
        project=project,
        operation=continuation,
        service=second_provider,
        addon_manager=object(),
        safe_boundary=_safe_boundary,
    )
    assert recovered_result == second_result
    assert second_provider.calls == calls_before_recovery

    reuse.selection_hash = "0" * 64
    db.session.commit()
    with pytest.raises(_contract.BlockContractError, match="evidence-reuse-selection"):
        _evidence_reuse.rehydrate_operation_evidence_reuse(reuse)


def test_analysis_reuse_rejects_legacy_origin_without_planner_witness(app, monkeypatch):
    user, project, record, operation, plan = _state(legacy_analysis_planner=True)
    first_provider = _DeterministicProvider(plan, record)
    original_translation = _workflow_runtime.call_translation_window
    monkeypatch.setattr(
        _workflow_runtime,
        "call_translation_window",
        lambda *_args, **_kwargs: "not-json",
    )
    with pytest.raises(_workflow_runtime.BlockWorkflowPartialOutcome) as raised:
        _workflow_runtime.run_document_block_workflow(
            project=project,
            operation=operation,
            service=first_provider,
            addon_manager=object(),
            safe_boundary=_safe_boundary,
        )
    _operations.finish_operation(operation, "partial", result=raised.value.result)
    db.session.commit()

    continuation = TranslationOperation(
        project_id=project.id,
        owner_user_id=user.id,
        kind="document_block_translate",
        idempotency_key="workflow-e2e-legacy-planner-origin",
        request_fingerprint="6" * 64,
        state="running",
        queue_job_id="workflow-e2e-legacy-planner-origin-worker",
        source_content_hash=record.source_content_hash,
        source_plan_id=record.id,
        source_plan_hash=record.plan_hash,
        lease_expires_at=utc_now_db() + timedelta(minutes=10),
    )
    db.session.add(continuation)
    db.session.flush()
    profile = _window_execution.resolve_execution_profile(project)
    material = _workflow_config.freeze_block_workflow_config(
        continuation,
        record,
        user=user,
        settings={
            "translator_model": "e2e-provider/chat",
            "enable_pre_analysis": True,
            "analyzer_model": "e2e-provider/chat",
            "enable_research": False,
        },
        window_profile=_translator_window_profile(profile),
    )
    _workflow_config.persist_block_workflow_config(continuation, record, material)
    db.session.commit()

    monkeypatch.setattr(
        _workflow_runtime, "call_translation_window", original_translation
    )
    second_provider = _DeterministicProvider(plan, record)
    _continued, second_result = _workflow_runtime.run_document_block_workflow(
        project=project,
        operation=continuation,
        service=second_provider,
        addon_manager=object(),
        safe_boundary=_safe_boundary,
    )

    assert second_result["evidence_reuse_id"] is None
    assert (
        TranslationOperationEvidenceReuse.query.filter_by(
            operation_id=continuation.id
        ).one_or_none()
        is None
    )
    assert second_provider.calls.count("translator_block_analysis") > 0


def test_analysis_reuse_rejects_endpoint_or_scope_plan_drift(app, monkeypatch):
    user, project, record, operation, plan = _state()
    first_provider = _DeterministicProvider(plan, record)
    monkeypatch.setattr(
        _workflow_runtime,
        "call_translation_window",
        lambda *_args, **_kwargs: "not-json",
    )
    with pytest.raises(_workflow_runtime.BlockWorkflowPartialOutcome) as raised:
        _workflow_runtime.run_document_block_workflow(
            project=project,
            operation=operation,
            service=first_provider,
            addon_manager=object(),
            safe_boundary=_safe_boundary,
        )
    _operations.finish_operation(operation, "partial", result=raised.value.result)
    db.session.commit()

    profile = _window_execution.resolve_execution_profile(project)

    def frozen_continuation(identifier: str):
        continuation = TranslationOperation(
            project_id=project.id,
            owner_user_id=user.id,
            kind="document_block_translate",
            idempotency_key=identifier,
            request_fingerprint=hashlib.sha256(identifier.encode()).hexdigest(),
            state="running",
            queue_job_id=f"{identifier}-worker",
            source_content_hash=record.source_content_hash,
            source_plan_id=record.id,
            source_plan_hash=record.plan_hash,
            lease_expires_at=utc_now_db() + timedelta(minutes=10),
        )
        db.session.add(continuation)
        db.session.flush()
        material = _workflow_config.freeze_block_workflow_config(
            continuation,
            record,
            user=user,
            settings={
                "translator_model": "e2e-provider/chat",
                "enable_pre_analysis": True,
                "analyzer_model": "e2e-provider/chat",
                "enable_research": False,
            },
            window_profile=_translator_window_profile(profile),
        )
        workflow = _workflow_config.persist_block_workflow_config(
            continuation, record, material
        )
        db.session.commit()
        return continuation, workflow

    key = db.session.query(ApiKey).one()
    key.api_base = "https://different-model-endpoint.example/v1"
    db.session.commit()
    endpoint_operation, endpoint_workflow = frozen_continuation(
        "workflow-e2e-endpoint-drift"
    )
    endpoint_phase = endpoint_workflow.config_json["pre_analysis"]
    endpoint_scopes = _workflow_runtime._analysis_scopes_for_operation(
        plan=plan,
        operation=endpoint_operation,
        source_plan=record,
        phase=endpoint_phase,
        budget=_workflow_runtime._analysis_budget(endpoint_phase),
        token_counter=None,
    )
    assert (
        _evidence_reuse.discover_or_persist_operation_evidence_reuse(
            operation=endpoint_operation,
            source_plan=record,
            workflow=endpoint_workflow,
            analysis_scope_hashes=tuple(scope.scope_hash for scope in endpoint_scopes),
        )
        is None
    )
    _operations.finish_operation(
        endpoint_operation,
        "failed",
        error_code="test_endpoint_drift",
        error_detail="Synthetic terminal state for the next independent fence.",
    )

    key.api_base = None
    db.session.commit()
    scope_operation, scope_workflow = frozen_continuation("workflow-e2e-scope-drift")
    scope_phase = scope_workflow.config_json["pre_analysis"]

    def singleton_counter(**kwargs):
        messages = kwargs.get("messages")
        if messages:
            payload = json.loads(messages[-1]["content"])
            return 10 if len(payload["source_blocks"]) == 1 else 1_000_000
        return 1

    changed_scopes = _workflow_runtime._analysis_scopes_for_operation(
        plan=plan,
        operation=scope_operation,
        source_plan=record,
        phase=scope_phase,
        budget=_workflow_runtime._analysis_budget(scope_phase),
        token_counter=singleton_counter,
    )
    assert len(changed_scopes) == len(plan.blocks)
    assert (
        _evidence_reuse.discover_or_persist_operation_evidence_reuse(
            operation=scope_operation,
            source_plan=record,
            workflow=scope_workflow,
            analysis_scope_hashes=tuple(scope.scope_hash for scope in changed_scopes),
        )
        is None
    )


def test_new_operation_reuses_exact_completed_research(app, monkeypatch):
    user, project, record, operation, plan = _state(enable_research=True)
    first_provider = _DeterministicProvider(plan, record, with_research=True)
    original_translation = _workflow_runtime.call_translation_window
    monkeypatch.setattr(
        _workflow_runtime,
        "call_translation_window",
        lambda *_args, **_kwargs: "not-json",
    )
    with pytest.raises(_workflow_runtime.BlockWorkflowPartialOutcome) as raised:
        _workflow_runtime.run_document_block_workflow(
            project=project,
            operation=operation,
            service=first_provider,
            addon_manager=object(),
            safe_boundary=_safe_boundary,
        )
    first_result = raised.value.result
    assert first_provider.calls.count("translator_block_research") == 1
    _operations.finish_operation(operation, "partial", result=first_result)
    db.session.commit()

    continuation = TranslationOperation(
        project_id=project.id,
        owner_user_id=user.id,
        kind="document_block_translate",
        idempotency_key="workflow-e2e-research-reuse",
        request_fingerprint="8" * 64,
        state="running",
        queue_job_id="workflow-e2e-research-reuse-worker",
        source_content_hash=record.source_content_hash,
        source_plan_id=record.id,
        source_plan_hash=record.plan_hash,
        lease_expires_at=utc_now_db() + timedelta(minutes=10),
    )
    db.session.add(continuation)
    db.session.flush()
    profile = _window_execution.resolve_execution_profile(project)
    material = _workflow_config.freeze_block_workflow_config(
        continuation,
        record,
        user=user,
        settings={
            "translator_model": "e2e-provider/chat",
            "enable_pre_analysis": True,
            "analyzer_model": "e2e-provider/chat",
            "enable_research": True,
            "researcher_model": "e2e-provider/chat",
        },
        window_profile=_translator_window_profile(profile),
        research={
            "budget": {
                "max_model_calls": 2,
                "max_tool_calls": 0,
                "output_tokens": 128_000,
            },
            "tool_declarations": [],
            "selected_provider_ids": [],
            "selected_addon_ids": [],
        },
    )
    _workflow_config.persist_block_workflow_config(continuation, record, material)
    db.session.commit()

    def reject_replanning(_record):
        raise AssertionError("the workflow's verified immutable plan must be reused")

    original_graph_verification = _research_persistence._verify_run_graph_shape
    graph_verifications = 0

    def count_graph_verification(*args, **kwargs):
        nonlocal graph_verifications
        graph_verifications += 1
        return original_graph_verification(*args, **kwargs)

    monkeypatch.setattr(_evidence_reuse, "rehydrate_source_plan", reject_replanning)
    monkeypatch.setattr(_analysis_attempts, "rehydrate_source_plan", reject_replanning)
    monkeypatch.setattr(_context_baselines, "rehydrate_source_plan", reject_replanning)
    monkeypatch.setattr(
        _research_persistence,
        "rehydrate_source_plan",
        reject_replanning,
    )
    monkeypatch.setattr(
        _research_persistence,
        "_verify_run_graph_shape",
        count_graph_verification,
    )

    monkeypatch.setattr(
        _workflow_runtime, "call_translation_window", original_translation
    )
    second_provider = _DeterministicProvider(plan, record, with_research=True)
    _continued, second_result = _workflow_runtime.run_document_block_workflow(
        project=project,
        operation=continuation,
        service=second_provider,
        addon_manager=object(),
        safe_boundary=_safe_boundary,
    )

    reuse = TranslationOperationEvidenceReuse.query.filter_by(
        operation_id=continuation.id
    ).one()
    assert reuse.id == second_result["evidence_reuse_id"]
    assert reuse.research_run_id == first_result["research_run_id"]
    research_reference = reuse.selection_json["research_reference"]
    assert research_reference["source_scope"]["mode"] == "full_document"
    assert len(research_reference["initial_packet_hash"]) == 64
    assert len(research_reference["source_document_hash"]) == 64
    assert research_reference["effective_output_tokens"] == 128_000
    assert second_result["research_run_id"] == first_result["research_run_id"]
    assert second_provider.calls.count("translator_block_analysis") == 0
    assert second_provider.calls.count("translator_block_research") == 0
    assert second_provider.calls.count("translator_block_translation_window") == len(
        plan.blocks
    )
    graph_verifications_before_workspace = graph_verifications
    workspace = _workspace_api.build_workspace_payload(
        project,
        offset=0,
        limit=20,
        latest_operation=continuation,
    )
    assert workspace["target_summary"]["active_targets"] == len(plan.blocks)
    assert workspace["target_summary"]["document_complete"] is True
    # Workspace paint verifies active target/alignment truth, not dormant
    # attempt/research history. The reuse path above already consumed and
    # verified that graph; scrolling must not replay it.
    assert graph_verifications - graph_verifications_before_workspace == 0
    # One complete graph rehydration checks shape both at entry and inside its
    # terminal-child proof. Baseline freezing/rehydration and slot adaptation
    # must reuse that result instead of adding more complete passes.
    assert graph_verifications == 2

    _operations.finish_operation(continuation, "completed", result=second_result)
    db.session.commit()
    changed_research = TranslationOperation(
        project_id=project.id,
        owner_user_id=user.id,
        kind="document_block_translate",
        idempotency_key="workflow-e2e-changed-research",
        request_fingerprint="7" * 64,
        state="running",
        queue_job_id="workflow-e2e-changed-research-worker",
        source_content_hash=record.source_content_hash,
        source_plan_id=record.id,
        source_plan_hash=record.plan_hash,
        lease_expires_at=utc_now_db() + timedelta(minutes=10),
    )
    db.session.add(changed_research)
    db.session.flush()
    changed_material = _workflow_config.freeze_block_workflow_config(
        changed_research,
        record,
        user=user,
        settings={
            "translator_model": "e2e-provider/chat",
            "enable_pre_analysis": True,
            "analyzer_model": "e2e-provider/chat",
            "enable_research": True,
            "researcher_model": "e2e-provider/chat",
        },
        window_profile=_translator_window_profile(profile),
        research={
            "budget": {
                "max_model_calls": 2,
                "max_tool_calls": 0,
                "output_tokens": 127_000,
            },
            "tool_declarations": [],
            "selected_provider_ids": [],
            "selected_addon_ids": [],
        },
        protected_targets=_continuation.snapshot_protected_targets(record),
    )
    _workflow_config.persist_block_workflow_config(
        changed_research, record, changed_material
    )
    db.session.commit()

    changed_provider = _DeterministicProvider(plan, record, with_research=True)
    _changed, changed_result = _workflow_runtime.run_document_block_workflow(
        project=project,
        operation=changed_research,
        service=changed_provider,
        addon_manager=object(),
        safe_boundary=_safe_boundary,
    )
    changed_reuse = TranslationOperationEvidenceReuse.query.filter_by(
        operation_id=changed_research.id
    ).one()
    assert changed_reuse.research_run_id is None
    assert (
        changed_result["analysis_artifact_ids"] == first_result["analysis_artifact_ids"]
    )
    assert changed_result["research_run_id"] != first_result["research_run_id"]
    assert changed_provider.calls.count("translator_block_analysis") == 0
    assert changed_provider.calls.count("translator_block_research") == 1
    assert changed_provider.calls.count("translator_block_translation_window") == 0


def test_conductor_retries_one_malformed_window_response_on_same_operation(
    app, monkeypatch
):
    _user, project, record, operation, plan = _state()
    provider = _DeterministicProvider(plan, record)
    original = _workflow_runtime.call_translation_window
    calls = 0

    def malformed_once(service, *, phase, packet, action_id):
        nonlocal calls
        calls += 1
        if calls == 1:
            return "not-json"
        return original(service, phase=phase, packet=packet, action_id=action_id)

    monkeypatch.setattr(_workflow_runtime, "call_translation_window", malformed_once)

    completed, result = _workflow_runtime.run_document_block_workflow(
        project=project,
        operation=operation,
        service=provider,
        addon_manager=object(),
        safe_boundary=_safe_boundary,
    )

    attempts = (
        db.session.query(TranslationWindowAttempt)
        .order_by(TranslationWindowAttempt.created_at)
        .all()
    )
    assert completed.id == operation.id
    assert calls == len(plan.blocks) + 1
    assert result["translated_block_count"] == len(plan.blocks)
    assert len(attempts) == len(plan.blocks) + 1
    assert attempts[1].retry_of_attempt_id == attempts[0].id
    assert attempts[0].dispatch_state == "normalization_failed"
    assert all(attempt.dispatch_state == "staged" for attempt in attempts[1:])


@pytest.mark.parametrize(
    "legacy_retry_marker",
    [False, True],
    ids=["versioned-retry", "historical-unmarked-retry"],
)
def test_passive_normalization_retry_fingerprints_are_versioned_compatibly(
    app, monkeypatch, legacy_retry_marker: bool
):
    user, project, record, operation, plan = _state(passive=True)
    translator_addon = object.__new__(import_module(_translator).TranslatorAddon)
    app.addon_manager = SimpleNamespace(
        get_loaded_addon=lambda _addon_id: translator_addon
    )
    execution = create_execution(
        user.id,
        "translator",
        "project",
        str(project.id),
        {"mode": "passive", "executor_profile": _PASSIVE_EXECUTOR_PROFILE},
        mode="passive",
    )
    project_settings = dict(project.settings)
    project_settings["mcp_execution_id"] = execution.id
    project.settings = project_settings
    db.session.commit()

    issued = issue_access_key(
        user.id,
        "passive retry fixture",
        ["mcp.work", "mcp.translator"],
        [],
    )
    principal = resolve_access_key(issued.raw_token)
    db.session.commit()
    context = {
        "execution_id": execution.id,
        "owner_user_id": user.id,
        "addon_id": "translator",
        "resource_kind": "project",
        "resource_id": str(project.id),
    }
    service = LLMService(user_id=user.id, session_id="passive-window-retry-e2e")
    response_fixture = _DeterministicProvider(plan, record)
    bad_response_content: list[str] = []

    if legacy_retry_marker:
        append_attempt_event = _window_persistence.append_attempt_event

        def append_historical_request_event(attempt, event_type, payload, **kwargs):
            if (
                event_type == "request_prepared"
                and attempt.retry_of_attempt_id is not None
            ):
                legacy_payload = dict(payload)
                legacy_payload.pop("passive_retry_dispatch_version", None)
                payload = legacy_payload
            return append_attempt_event(attempt, event_type, payload, **kwargs)

        # Simulate an older writer before the event payload is hashed. The
        # production hash verifier and existing event rows remain untouched.
        monkeypatch.setattr(
            _window_persistence,
            "append_attempt_event",
            append_historical_request_event,
        )

    def run_passive():
        with passive_execution(**context):
            return _workflow_runtime.run_document_block_workflow(
                project=project,
                operation=operation,
                service=service,
                addon_manager=object(),
                safe_boundary=_safe_boundary,
            )

    def submit_work_response(work_item_id: str, *, accepted_bad_response: bool) -> None:
        claim = claim_next_work(
            principal,
            "passive-retry-test-client",
            addon_id="translator",
            execution_id=execution.id,
        )
        assert claim is not None
        assert claim.work_item_id == work_item_id
        wire_request = _work_claim_result(claim).structured_content["request"]
        retry = claim.request.get("dispatch", {}).get("translator_window_retry")
        if retry is not None:
            assert wire_request["dispatch"]["translator_window_retry"] == retry
            assert retry["validation_codes"] == [
                "target-validation-failed", "target-fixed-token-scope",
            ]
        else:
            assert "translator_window_retry" not in wire_request.get("dispatch", {})
        if accepted_bad_response:
            generated = response_fixture.completion(
                source="translator_block_translation_window",
                messages=wire_request["messages"],
            )
            response = json.loads(generated["choices"][0]["message"]["content"])
            markup = response["results"][0]["content"]
            moved_markup = re.sub(
                r'(<a\b[^>]*>)<em>(.*?)</em></a>',
                r"<em>\1\2</a></em>",
                markup,
                count=1,
            )
            assert moved_markup != markup
            response["results"][0]["content"] = moved_markup
            content = json.dumps(response, ensure_ascii=False)
            preflight = import_module(
                f"{_translator}.passive_preflight"
            ).preflight_completion(
                claim.request,
                {"content": content},
            )
            assert preflight is None or not preflight["validation_errors"]
            bad_response_content.append(content)
        else:
            generated = response_fixture.completion(
                source="translator_block_translation_window",
                messages=wire_request["messages"],
            )
            content = generated["choices"][0]["message"]["content"]
        submitted = submit_work(
            principal,
            claim.work_item_id,
            claim.lease_token,
            {"content": content, "finish_reason": "stop"},
        )
        assert submitted["status"] == "accepted", {
            key: submitted.get(key)
            for key in (
                "error_code",
                "error_detail",
                "validation_errors",
                "validation_stage",
            )
        }
        assert submitted["validation_stage"] == "response_contract"

    with pytest.raises(ExternalWorkPending) as first_pending:
        run_passive()
    first_work_id = first_pending.value.work_item_id
    assert _core_models.McpWorkItem.query.count() == 1
    first_work = db.session.get(_core_models.McpWorkItem, first_work_id)
    assert first_work is not None
    assert "translator_window_retry" not in first_work.request_json["dispatch"]
    submit_work_response(first_work_id, accepted_bad_response=True)

    if legacy_retry_marker:
        with pytest.raises(_workflow_runtime.BlockWorkflowPartialOutcome):
            run_passive()
        db.session.expire_all()
        attempts = (
            db.session.query(TranslationWindowAttempt)
            .filter_by(operation_id=operation.id)
            .order_by(TranslationWindowAttempt.created_at)
            .all()
        )
        assert len(attempts) == 2
        failed_attempt, retry_attempt = attempts
        assert failed_attempt.dispatch_state == "normalization_failed"
        assert retry_attempt.retry_of_attempt_id == failed_attempt.id
        assert retry_attempt.dispatch_state == "normalization_failed"
        retry_events = _window_persistence.rehydrate_attempt_events(retry_attempt)
        retry_request_event = next(
            event for event in retry_events if event.event_type == "request_prepared"
        )
        assert "passive_retry_dispatch_version" not in retry_request_event.payload_json
        assert _core_models.McpWorkItem.query.count() == 1
        original_work = db.session.get(_core_models.McpWorkItem, first_work_id)
        assert original_work is not None
        assert original_work.state == "completed"
        assert "translator_window_retry" not in (
            original_work.request_json.get("dispatch") or {}
        )
        failed_events = _window_persistence.rehydrate_attempt_events(failed_attempt)
        original_response_event = next(
            event for event in failed_events if event.event_type == "provider_response"
        )
        assert original_response_event.payload_json["response"] == bad_response_content[0]
        return

    class SimulatedDispatchCrash(BaseException):
        pass

    transition_dispatch = _window_execution._transition_dispatch

    def crash_before_pending_transition(*args, **kwargs):
        if kwargs.get("event_type") == "external_work_pending":
            raise SimulatedDispatchCrash
        return transition_dispatch(*args, **kwargs)

    monkeypatch.setattr(
        _window_execution, "_transition_dispatch", crash_before_pending_transition
    )
    with pytest.raises(SimulatedDispatchCrash):
        run_passive()

    db.session.expire_all()
    attempts = (
        db.session.query(TranslationWindowAttempt)
        .filter_by(operation_id=operation.id)
        .order_by(TranslationWindowAttempt.created_at)
        .all()
    )
    assert len(attempts) == 2
    failed_attempt, retry_attempt = attempts
    assert failed_attempt.dispatch_state == "normalization_failed"
    assert retry_attempt.dispatch_state == "dispatched"
    assert retry_attempt.retry_of_attempt_id == failed_attempt.id
    retry_events = _window_persistence.rehydrate_attempt_events(retry_attempt)
    retry_request_event = next(
        event for event in retry_events if event.event_type == "request_prepared"
    )
    assert retry_request_event.payload_json["passive_retry_dispatch_version"] == 1
    pending_work = _core_models.McpWorkItem.query.filter_by(
        execution_id=execution.id, state="pending"
    ).one()
    retry_work_id = pending_work.id
    assert "translator_window_retry" in (
        pending_work.request_json.get("dispatch") or {}
    )
    assert _core_models.McpWorkItem.query.count() == 2

    monkeypatch.setattr(_window_execution, "_transition_dispatch", transition_dispatch)
    with pytest.raises(ExternalWorkPending) as retry_pending:
        run_passive()
    assert retry_pending.value.work_item_id == retry_work_id
    assert retry_work_id != first_work_id
    assert _core_models.McpWorkItem.query.count() == 2

    db.session.expire_all()
    attempts = (
        db.session.query(TranslationWindowAttempt)
        .filter_by(operation_id=operation.id)
        .order_by(TranslationWindowAttempt.created_at)
        .all()
    )
    assert len(attempts) == 2
    failed_attempt, retry_attempt = attempts
    assert failed_attempt.dispatch_state == "normalization_failed"
    assert retry_attempt.retry_of_attempt_id == failed_attempt.id
    failed_events = _window_persistence.rehydrate_attempt_events(failed_attempt)
    retry_events = _window_persistence.rehydrate_attempt_events(retry_attempt)
    retry_request_event = next(
        event for event in retry_events if event.event_type == "request_prepared"
    )
    assert retry_request_event.payload_json["passive_retry_dispatch_version"] == 1
    assert "passive_retry_dispatch_version" not in retry_attempt.packet_json
    response_event = next(
        event for event in failed_events if event.event_type == "provider_response"
    )
    assert response_event.payload_json["response"] == bad_response_content[0]
    failure_event = next(
        event
        for event in failed_events
        if event.event_type == "normalization_failed"
    )
    assert failure_event.payload_json["code"] == "target-validation-failed"
    assert failure_event.payload_json["finding_codes"] == ["target-fixed-token-scope"]

    retry_work = db.session.get(_core_models.McpWorkItem, retry_work_id)
    assert retry_work is not None
    assert retry_work.state == "pending"
    assert retry_work.request_json["dispatch"]["translator_window_retry"] == {
        "contract_version": 1,
        "attempt_id": retry_attempt.id,
        "retry_of_attempt_id": failed_attempt.id,
        "dispatch_state": "normalization_failed",
        "validation_codes": [
            "target-validation-failed",
            "target-fixed-token-scope",
        ],
    }

    with pytest.raises(ExternalWorkPending) as retry_reentry:
        run_passive()
    assert retry_reentry.value.work_item_id == retry_work_id
    assert _core_models.McpWorkItem.query.count() == 2
    assert (
        db.session.query(TranslationWindowAttempt)
        .filter_by(operation_id=operation.id)
        .count()
        == 2
    )

    submit_work_response(retry_work_id, accepted_bad_response=False)
    for _ in range(len(plan.blocks) + 1):
        try:
            completed, result = run_passive()
        except ExternalWorkPending as pending:
            assert pending.work_item_id != retry_work_id
            submit_work_response(
                pending.work_item_id, accepted_bad_response=False
            )
        else:
            break
    else:
        pytest.fail("Passive workflow did not complete within its bounded windows.")

    assert completed.id == operation.id
    assert result["translated_block_count"] == 1
    retry_attempt = db.session.get(TranslationWindowAttempt, retry_attempt.id)
    assert retry_attempt is not None
    assert retry_attempt.dispatch_state == "staged"
    staged_revisions = _models.TranslationWindowAttemptRevision.query.filter_by(
        attempt_id=retry_attempt.id
    ).count()
    assert staged_revisions == len(retry_attempt.block_ids_json)
    assert _core_models.McpWorkItem.query.count() == 2


def test_conductor_returns_safe_partial_after_two_window_normalization_failures(
    app, monkeypatch
):
    _user, project, record, operation, plan = _state()
    provider = _DeterministicProvider(plan, record)
    original = _workflow_runtime.call_translation_window
    calls = 0

    def malformed_after_first(service, *, phase, packet, action_id):
        nonlocal calls
        calls += 1
        if calls > 1:
            return "not-json"
        return original(service, phase=phase, packet=packet, action_id=action_id)

    monkeypatch.setattr(
        _workflow_runtime, "call_translation_window", malformed_after_first
    )

    with pytest.raises(_workflow_runtime.BlockWorkflowPartialOutcome) as raised:
        _workflow_runtime.run_document_block_workflow(
            project=project,
            operation=operation,
            service=provider,
            addon_manager=object(),
            safe_boundary=_safe_boundary,
        )

    partial = raised.value
    failure = partial.result["block_failure"]
    assert calls == 3
    assert partial.operation.id == operation.id
    assert partial.result["translated_block_count"] == 1
    assert failure == {
        "attempt_id": failure["attempt_id"],
        "window_order": 2,
        "affected_block_ids": [plan.blocks[1].id],
        "dispatch_state": "normalization_failed",
        "failure_kind": "normalization_failed",
        "recovery_action": "translate_remaining",
        "remaining_dispatches": 0,
        "validation_codes": ["window-response-json"],
        "validator": "target_response_normalization",
    }
    assert db.session.query(TranslationTargetBlockActive).count() == 1
    assert db.session.query(TranslationWindowAttempt).count() == 3


def test_new_operation_preserves_human_edits_and_translates_only_remaining_blocks(
    app, monkeypatch
):
    user, project, record, operation, plan = _state()
    first_provider = _DeterministicProvider(plan, record)
    original = _workflow_runtime.call_translation_window
    calls = 0

    def fail_second_window(service, *, phase, packet, action_id):
        nonlocal calls
        calls += 1
        if calls > 1:
            return "not-json"
        return original(service, phase=phase, packet=packet, action_id=action_id)

    monkeypatch.setattr(
        _workflow_runtime, "call_translation_window", fail_second_window
    )
    with pytest.raises(_workflow_runtime.BlockWorkflowPartialOutcome) as raised:
        _workflow_runtime.run_document_block_workflow(
            project=project,
            operation=operation,
            service=first_provider,
            addon_manager=object(),
            safe_boundary=_safe_boundary,
        )
    _operations.finish_operation(operation, "partial", result=raised.value.result)
    db.session.commit()

    first_block = plan.blocks[0]
    _workspace_api.persist_manual_candidate(
        project,
        first_block.id,
        _candidate(plan, record, first_block, "Ręczna redakcja"),
        1,
        author_user_id=user.id,
    )
    protected_pointer = db.session.get(
        TranslationTargetBlockActive,
        {"source_plan_id": record.id, "block_id": first_block.id},
    )
    assert protected_pointer is not None
    protected_revision_id = protected_pointer.revision_id

    continuation = TranslationOperation(
        project_id=project.id,
        owner_user_id=user.id,
        kind="document_block_translate",
        idempotency_key="workflow-e2e-continuation",
        request_fingerprint="f" * 64,
        state="running",
        queue_job_id="workflow-e2e-continuation-worker",
        source_content_hash=record.source_content_hash,
        source_plan_id=record.id,
        source_plan_hash=record.plan_hash,
        lease_expires_at=utc_now_db() + timedelta(minutes=10),
    )
    db.session.add(continuation)
    db.session.flush()
    profile = _window_execution.resolve_execution_profile(project)
    config = _workflow_config.freeze_block_workflow_config(
        continuation,
        record,
        user=user,
        settings={
            "translator_model": "e2e-provider/chat",
            "enable_pre_analysis": False,
            "enable_research": False,
        },
        window_profile=_translator_window_profile(profile),
        protected_targets=_continuation.snapshot_protected_targets(record),
    )
    _workflow_config.persist_block_workflow_config(continuation, record, config)
    db.session.commit()

    monkeypatch.setattr(_workflow_runtime, "call_translation_window", original)
    remaining_provider = _DeterministicProvider(plan, record)
    remaining_provider._translation_index = 1
    _completed, result = _workflow_runtime.run_document_block_workflow(
        project=project,
        operation=continuation,
        service=remaining_provider,
        addon_manager=object(),
        safe_boundary=_safe_boundary,
    )

    db.session.expire_all()
    still_protected = db.session.get(
        TranslationTargetBlockActive,
        {"source_plan_id": record.id, "block_id": first_block.id},
    )
    assert still_protected is not None
    assert still_protected.revision_id == protected_revision_id
    assert result["protected_block_count"] == 1
    assert result["translated_block_count"] == len(plan.blocks) - 1
    assert remaining_provider.calls.count("translator_block_analysis") == 0
    assert remaining_provider.calls.count("translator_block_translation_window") == (
        len(plan.blocks) - 1
    )
    assert "Ręczna redakcja" in _target_persistence.render_persisted_target_document(
        record
    )


def test_new_operation_preserves_reviewer_edits_and_translates_only_remaining_blocks(
    app,
):
    user, project, record, operation, plan = _state()
    # Reviewer edits belong between operations, after the previous lease ends.
    operation.state = "cancelled"
    operation.cancel_requested_at = utc_now_db()
    operation.lease_expires_at = None
    grant = TranslationShareGrant(
        id="reviewer-workflow-grant",
        project_id=project.id,
        owner_user_id=user.id,
        label="Reviewer workflow grant",
        role="reviewer",
        token_digest=hashlib.sha512(b"reviewer-workflow-grant").hexdigest(),
        token_hint="hint",
        status="active",
        expires_at=None,
        revoked_at=None,
        created_at=utc_now_db(),
    )
    db.session.add(grant)
    db.session.commit()
    first_block = plan.blocks[0]
    _workspace_api.persist_manual_candidate(
        project,
        first_block.id,
        _candidate(plan, record, first_block, "Reviewer redaction"),
        0,
        author_user_id=None,
        share_grant_id=grant.id,
    )
    protected_pointer = db.session.get(
        TranslationTargetBlockActive,
        {"source_plan_id": record.id, "block_id": first_block.id},
    )
    assert protected_pointer is not None
    protected_revision_id = protected_pointer.revision_id

    # A fresh continuation freezes the reviewer revision as a protected target.
    operation = TranslationOperation(
        project_id=project.id,
        owner_user_id=user.id,
        kind="document_block_translate",
        idempotency_key="workflow-e2e-after-review",
        request_fingerprint="f" * 64,
        state="running",
        queue_job_id="workflow-e2e-after-review-worker",
        source_content_hash=record.source_content_hash,
        source_plan_id=record.id,
        source_plan_hash=record.plan_hash,
        lease_expires_at=utc_now_db() + timedelta(minutes=10),
    )
    db.session.add(operation)
    db.session.flush()
    profile = _window_execution.resolve_execution_profile(project)
    config = _workflow_config.freeze_block_workflow_config(
        operation,
        record,
        user=user,
        settings={
            "translator_model": "e2e-provider/chat",
            "enable_pre_analysis": False,
            "enable_research": False,
            "owner_policy": "Preserve document meaning and structure.",
        },
        window_profile=_translator_window_profile(profile),
        protected_targets=_continuation.snapshot_protected_targets(record),
    )
    _workflow_config.persist_block_workflow_config(operation, record, config)
    db.session.commit()

    provider = _DeterministicProvider(plan, record)
    _completed, result = _workflow_runtime.run_document_block_workflow(
        project=project,
        operation=operation,
        service=provider,
        addon_manager=object(),
        safe_boundary=_safe_boundary,
    )

    db.session.expire_all()
    still_protected = db.session.get(
        TranslationTargetBlockActive,
        {"source_plan_id": record.id, "block_id": first_block.id},
    )
    assert still_protected is not None
    assert still_protected.revision_id == protected_revision_id
    assert result["protected_block_count"] == 1
    assert result["translated_block_count"] == len(plan.blocks) - 1
    assert provider.calls.count("translator_block_analysis") == 0
    assert provider.calls.count("translator_block_translation_window") == (
        len(plan.blocks) - 1
    )


def test_stale_prepared_window_gets_one_safe_retry_after_worker_reclaim(
    app, monkeypatch
):
    _user, project, record, operation, plan = _state()
    provider = _DeterministicProvider(plan, record)

    class SimulatedCrash(BaseException):
        pass

    def crash_before_dispatch(*_args, **_kwargs):
        raise SimulatedCrash

    original_execute = _workflow_runtime.execute_window_attempt
    monkeypatch.setattr(
        _workflow_runtime, "execute_window_attempt", crash_before_dispatch
    )
    with pytest.raises(SimulatedCrash):
        _workflow_runtime.run_document_block_workflow(
            project=project,
            operation=operation,
            service=provider,
            addon_manager=object(),
            safe_boundary=_safe_boundary,
        )
    prepared = db.session.query(TranslationWindowAttempt).one()
    assert prepared.dispatch_state == "prepared"
    assert provider.calls.count("translator_block_translation_window") == 0

    operation.version += 1
    operation.queue_job_id = "workflow-e2e-reclaimed-worker"
    operation.lease_expires_at = utc_now_db() + timedelta(minutes=10)
    db.session.commit()
    monkeypatch.setattr(_workflow_runtime, "execute_window_attempt", original_execute)
    _completed, result = _workflow_runtime.run_document_block_workflow(
        project=project,
        operation=operation,
        service=provider,
        addon_manager=object(),
        safe_boundary=_safe_boundary,
    )

    attempts = (
        db.session.query(TranslationWindowAttempt)
        .order_by(TranslationWindowAttempt.created_at)
        .all()
    )
    assert result["translated_block_count"] == len(plan.blocks)
    assert attempts[0].dispatch_state == "prepared"
    assert attempts[1].retry_of_attempt_id == attempts[0].id
    assert provider.calls.count("translator_block_translation_window") == len(
        plan.blocks
    )


def test_historical_unmarked_retry_after_prepared_reclaim_reuses_passive_work(
    app, monkeypatch
):
    user, project, record, operation, plan = _state(passive=True)
    translator_addon = object.__new__(import_module(_translator).TranslatorAddon)
    app.addon_manager = SimpleNamespace(
        get_loaded_addon=lambda _addon_id: translator_addon
    )
    execution = create_execution(
        user.id,
        "translator",
        "project",
        str(project.id),
        {"mode": "passive", "executor_profile": _PASSIVE_EXECUTOR_PROFILE},
        mode="passive",
    )
    project_settings = dict(project.settings)
    project_settings["mcp_execution_id"] = execution.id
    project.settings = project_settings
    db.session.commit()

    issued = issue_access_key(
        user.id,
        "prepared retry compatibility",
        ["mcp.work", "mcp.translator"],
        [],
    )
    principal = resolve_access_key(issued.raw_token)
    db.session.commit()
    context = {
        "execution_id": execution.id,
        "owner_user_id": user.id,
        "addon_id": "translator",
        "resource_kind": "project",
        "resource_id": str(project.id),
    }
    service = LLMService(user_id=user.id, session_id="prepared-retry-compatibility")
    response_fixture = _DeterministicProvider(plan, record)

    def run_passive():
        with passive_execution(**context):
            return _workflow_runtime.run_document_block_workflow(
                project=project,
                operation=operation,
                service=service,
                addon_manager=object(),
                safe_boundary=_safe_boundary,
            )

    class SimulatedCrash(BaseException):
        pass

    def crash_before_dispatch(*_args, **_kwargs):
        raise SimulatedCrash

    original_execute = _workflow_runtime.execute_window_attempt
    monkeypatch.setattr(
        _workflow_runtime, "execute_window_attempt", crash_before_dispatch
    )
    with pytest.raises(SimulatedCrash):
        run_passive()
    db.session.expire_all()
    prepared = db.session.query(TranslationWindowAttempt).one()
    assert prepared.dispatch_state == "prepared"

    operation.version += 1
    operation.queue_job_id = "workflow-e2e-passive-reclaimed-worker"
    operation.lease_expires_at = utc_now_db() + timedelta(minutes=10)
    db.session.commit()
    monkeypatch.setattr(_workflow_runtime, "execute_window_attempt", original_execute)

    append_attempt_event = _window_persistence.append_attempt_event

    def append_historical_request_event(attempt, event_type, payload, **kwargs):
        if event_type == "request_prepared" and attempt.retry_of_attempt_id is not None:
            legacy_payload = dict(payload)
            legacy_payload.pop("passive_retry_dispatch_version", None)
            payload = legacy_payload
        return append_attempt_event(attempt, event_type, payload, **kwargs)

    # Omit the marker before hashing to simulate an existing historical chain.
    monkeypatch.setattr(
        _window_persistence, "append_attempt_event", append_historical_request_event
    )
    with pytest.raises(ExternalWorkPending) as first_pending:
        run_passive()
    retry_work_id = first_pending.value.work_item_id

    db.session.expire_all()
    attempts = (
        db.session.query(TranslationWindowAttempt)
        .filter_by(operation_id=operation.id)
        .order_by(TranslationWindowAttempt.created_at)
        .all()
    )
    assert len(attempts) == 2
    prepared, retry_attempt = attempts
    assert prepared.dispatch_state == "prepared"
    assert retry_attempt.retry_of_attempt_id == prepared.id
    assert retry_attempt.dispatch_state == "external_pending"
    retry_events = _window_persistence.rehydrate_attempt_events(retry_attempt)
    retry_request_event = next(
        event for event in retry_events if event.event_type == "request_prepared"
    )
    assert "passive_retry_dispatch_version" not in retry_request_event.payload_json
    work = db.session.get(_core_models.McpWorkItem, retry_work_id)
    assert work is not None
    assert work.state == "pending"
    assert "translator_window_retry" not in (work.request_json.get("dispatch") or {})
    assert _core_models.McpWorkItem.query.count() == 1

    with pytest.raises(ExternalWorkPending) as pending_reentry:
        run_passive()
    assert pending_reentry.value.work_item_id == retry_work_id
    assert _core_models.McpWorkItem.query.count() == 1

    claim = claim_next_work(
        principal,
        "prepared-retry-test-client",
        addon_id="translator",
        execution_id=execution.id,
    )
    assert claim is not None
    assert claim.work_item_id == retry_work_id
    leased_work = db.session.get(_core_models.McpWorkItem, retry_work_id)
    assert leased_work is not None
    assert leased_work.state == "leased"

    with pytest.raises(ExternalWorkPending) as leased_reentry:
        run_passive()
    assert leased_reentry.value.work_item_id == retry_work_id
    assert _core_models.McpWorkItem.query.count() == 1

    generated = response_fixture.completion(
        source="translator_block_translation_window",
        messages=claim.request["messages"],
    )
    accepted_content = generated["choices"][0]["message"]["content"]
    submitted = submit_work(
        principal,
        retry_work_id,
        claim.lease_token,
        {"content": accepted_content, "finish_reason": "stop"},
    )
    assert submitted["status"] == "accepted"

    for _ in range(len(plan.blocks) + 1):
        try:
            completed, result = run_passive()
        except ExternalWorkPending as pending:
            assert pending.work_item_id != retry_work_id
            next_claim = claim_next_work(
                principal,
                "prepared-retry-test-client",
                addon_id="translator",
                execution_id=execution.id,
            )
            assert next_claim is not None
            assert next_claim.work_item_id == pending.work_item_id
            next_response = response_fixture.completion(
                source="translator_block_translation_window",
                messages=next_claim.request["messages"],
            )
            next_submission = submit_work(
                principal,
                next_claim.work_item_id,
                next_claim.lease_token,
                {
                    "content": next_response["choices"][0]["message"]["content"],
                    "finish_reason": "stop",
                },
            )
            assert next_submission["status"] == "accepted"
        else:
            break
    else:
        pytest.fail("Passive workflow did not stage the prepared retry.")

    assert completed.id == operation.id
    db.session.expire_all()
    retry_attempt = db.session.get(TranslationWindowAttempt, retry_attempt.id)
    assert retry_attempt is not None
    assert retry_attempt.dispatch_state == "staged"
    assert result["translated_block_count"] == len(retry_attempt.block_ids_json)
    assert (
        _models.TranslationWindowAttemptRevision.query.filter_by(
            attempt_id=retry_attempt.id
        ).count()
        == len(retry_attempt.block_ids_json)
    )
    work = db.session.get(_core_models.McpWorkItem, retry_work_id)
    assert work is not None
    assert work.state == "completed"
    assert _core_models.McpWorkItem.query.count() == 1


def test_stale_received_response_is_not_replayed_after_worker_reclaim(app, monkeypatch):
    _user, project, record, operation, plan = _state()
    provider = _DeterministicProvider(plan, record)

    class SimulatedCrash(BaseException):
        pass

    original_stage = _window_execution._stage_window_response

    def crash_after_response(*_args, **_kwargs):
        raise SimulatedCrash

    monkeypatch.setattr(
        _window_execution, "_stage_window_response", crash_after_response
    )
    with pytest.raises(SimulatedCrash):
        _workflow_runtime.run_document_block_workflow(
            project=project,
            operation=operation,
            service=provider,
            addon_manager=object(),
            safe_boundary=_safe_boundary,
        )
    received = db.session.query(TranslationWindowAttempt).one()
    assert received.dispatch_state == "response_received"
    translation_calls = provider.calls.count("translator_block_translation_window")
    assert translation_calls == 1

    operation.version += 1
    operation.queue_job_id = "workflow-e2e-reclaimed-worker"
    operation.lease_expires_at = utc_now_db() + timedelta(minutes=10)
    db.session.commit()
    monkeypatch.setattr(_window_execution, "_stage_window_response", original_stage)
    with pytest.raises(_workflow_runtime.BlockWorkflowPartialOutcome) as raised:
        _workflow_runtime.run_document_block_workflow(
            project=project,
            operation=operation,
            service=provider,
            addon_manager=object(),
            safe_boundary=_safe_boundary,
        )

    failure = raised.value.result["block_failure"]
    assert failure["dispatch_state"] == "response_received"
    assert failure["failure_kind"] == "fence_failed"
    assert failure["recovery_action"] == "rebase_required"
    assert provider.calls.count("translator_block_translation_window") == 1


def test_stale_staging_failure_is_not_replayed_after_worker_reclaim(app, monkeypatch):
    _user, project, record, operation, plan = _state()
    provider = _DeterministicProvider(plan, record)
    original_persist = _window_execution.persist_target_revision

    def fail_staging(*_args, **_kwargs):
        raise _contract.BlockContractError("forced-staging-failure")

    monkeypatch.setattr(_window_execution, "persist_target_revision", fail_staging)
    with pytest.raises(_workflow_runtime.BlockWorkflowPartialOutcome):
        _workflow_runtime.run_document_block_workflow(
            project=project,
            operation=operation,
            service=provider,
            addon_manager=object(),
            safe_boundary=_safe_boundary,
        )
    failed = db.session.query(TranslationWindowAttempt).one()
    assert failed.dispatch_state == "staging_failed"
    assert provider.calls.count("translator_block_translation_window") == 1

    operation.version += 1
    operation.queue_job_id = "workflow-e2e-reclaimed-worker"
    operation.lease_expires_at = utc_now_db() + timedelta(minutes=10)
    db.session.commit()
    monkeypatch.setattr(_window_execution, "persist_target_revision", original_persist)
    with pytest.raises(_workflow_runtime.BlockWorkflowPartialOutcome) as raised:
        _workflow_runtime.run_document_block_workflow(
            project=project,
            operation=operation,
            service=provider,
            addon_manager=object(),
            safe_boundary=_safe_boundary,
        )

    failure = raised.value.result["block_failure"]
    assert failure["dispatch_state"] == "staging_failed"
    assert failure["failure_kind"] == "staging_failed"
    assert failure["recovery_action"] == "support_required"
    assert provider.calls.count("translator_block_translation_window") == 1


def test_conductor_research_is_frozen_once_across_window_retry(app, monkeypatch):
    _user, project, record, operation, plan = _state()
    provider = _DeterministicProvider(plan, record)
    original_rehydrate = _workflow_runtime.rehydrate_block_workflow_config
    persisted = _models.TranslationBlockWorkflowConfig.query.filter_by(
        operation_id=operation.id
    ).one()
    original_workflow = original_rehydrate(
        persisted, operation=operation, source_plan=record
    )
    research_calls = 0

    def research_once(**_kwargs):
        nonlocal research_calls
        research_calls += 1

    # Reuse the signed translation/window material while toggling only the
    # already-frozen phase selector for this fake research-path experiment.
    workflow = SimpleNamespace(
        id=original_workflow.id,
        config_hash=original_workflow.config_hash,
        config_receipt=original_workflow.config_receipt,
        config_json={**original_workflow.config_json, "enable_research": True},
    )
    monkeypatch.setattr(
        _workflow_runtime,
        "rehydrate_block_workflow_config",
        lambda *_args, **_kwargs: workflow,
    )
    monkeypatch.setattr(
        _workflow_runtime,
        "discover_or_persist_operation_evidence_reuse",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(_workflow_runtime, "_run_research", research_once)
    original = _workflow_runtime.call_translation_window
    calls = 0

    def malformed_once(service, *, phase, packet, action_id):
        nonlocal calls
        calls += 1
        if calls == 1:
            return "not-json"
        return original(service, phase=phase, packet=packet, action_id=action_id)

    monkeypatch.setattr(_workflow_runtime, "call_translation_window", malformed_once)
    _workflow_runtime.run_document_block_workflow(
        project=project,
        operation=operation,
        service=provider,
        addon_manager=object(),
        safe_boundary=_safe_boundary,
    )

    assert research_calls == 1
    assert calls == len(plan.blocks) + 1
