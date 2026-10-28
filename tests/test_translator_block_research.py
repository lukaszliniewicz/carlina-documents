"""Focused block-research orchestration tests; all external boundaries are faked."""

from __future__ import annotations

import hashlib
import importlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from content_storage_support import bind_content_policy
from flask import Flask
from sqlalchemy import event
from sqlalchemy.exc import OperationalError

_contract = importlib.import_module(
    "iarf" + "_assistant.addons.translator.block_contract"
)
_persistence = importlib.import_module(
    "iarf" + "_assistant.addons.translator.block_persistence"
)
_planner = importlib.import_module(
    "iarf" + "_assistant.addons.translator.block_planner"
)
_research = importlib.import_module(
    "iarf" + "_assistant.addons.translator.block_research"
)
_research_input = importlib.import_module(
    "iarf" + "_assistant.addons.translator.research_input_contract"
)
_models = importlib.import_module("iarf" + "_assistant.addons.translator.models")
_extensions = importlib.import_module("iarf" + "_assistant.extensions")
_operations = importlib.import_module(
    "iarf" + "_assistant.addons.translator.operations"
)
_user_models = importlib.import_module("iarf" + "_assistant.models")
_encryption = importlib.import_module("iarf" + "_assistant.core.encryption")
_semantic_html = importlib.import_module("carlina.core.semantic_html")
_utc = importlib.import_module("carlina.core.utc")

BlockContractError = _contract.BlockContractError
AttestedDocumentSnapshot = _contract.AttestedDocumentSnapshot
TranslationProject = _models.TranslationProject
TranslationOperation = _models.TranslationOperation
TranslationResearchRun = _models.TranslationResearchRun
TranslationResearchToolCall = _models.TranslationResearchToolCall
db = _extensions.db
User = _user_models.User


@contextmanager
def _measure_queries():
    metrics = SimpleNamespace(statement_count=0)

    def count_statement(_connection, _cursor, _statement, *_args):
        metrics.statement_count += 1

    event.listen(db.engine, "after_cursor_execute", count_statement)
    try:
        yield metrics
    finally:
        event.remove(db.engine, "after_cursor_execute", count_statement)


def test_default_research_result_envelope_supports_128k_completion_headroom():
    settings, input_limit, result_limit = _research._frozen_limits({})

    assert input_limit == 320 * 1024
    assert result_limit == 2 * 1024 * 1024
    assert settings["research_result_bytes"] == result_limit


@pytest.fixture
def app(tmp_path: Path):
    application = Flask(__name__, instance_path=str(tmp_path / "instance"))
    bind_content_policy(application)
    application.config.update(
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{tmp_path / 'block-research.db'}",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SECRET_KEY="block-research-test",
        TESTING=True,
        SQLALCHEMY_ENGINE_OPTIONS={"connect_args": {"timeout": 30}},
    )
    db.init_app(application)
    context = application.app_context()
    context.push()
    keys = _encryption.InstallationKeyStore(tmp_path / "keys.json")
    keys.initialize()
    application.extensions["carlina_encryption"] = _encryption.EncryptionService(keys)
    db.create_all()
    try:
        yield application
    finally:
        db.session.remove()
        db.engine.dispose()
        context.pop()


def _state(*, source_text: str = "Private research source."):
    user = User(email="block-research@example.test", password_hash="x")
    db.session.add(user)
    db.session.flush()
    project = TranslationProject(
        user_id=user.id,
        name="Block research",
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
                "resource_id": "doc",
            }
        },
    )
    db.session.add(project)
    db.session.flush()
    html = f"<html><body><p>{source_text}</p></body></html>"
    digest = hashlib.sha256(html.encode()).hexdigest()
    snapshot = AttestedDocumentSnapshot(
        id="doc",
        document_id="doc",
        output_html=html,
        content_hash=digest,
        semantic_content_hash=digest,
        metadata={},
        provenance={},
        captured_at="2026-08-25T00:00:00+00:00",
        semantic_contract_version=_semantic_html.SEMANTIC_HTML_CONTRACT_VERSION,
    )
    plan = _planner.plan_document_blocks(
        snapshot,
        project_id=project.id,
        source_attestation=_persistence.source_attestation_for_project(project),
        source_language="en",
    )
    record, _ = _persistence.persist_source_plan(project, plan)
    operation = TranslationOperation(
        project_id=project.id,
        owner_user_id=user.id,
        kind="document_block_translate",
        idempotency_key="block-research",
        request_fingerprint="a" * 64,
        state="running",
        queue_job_id="research-worker",
        source_content_hash=plan.snapshot.content_hash,
        source_plan_id=record.id,
        source_plan_hash=plan.plan_hash,
        lease_expires_at=_utc.utc_now_db() + timedelta(minutes=5),
    )
    db.session.add(operation)
    db.session.commit()
    return record, operation


class _Manager:
    def __init__(self):
        schema = {
            "type": "function",
            "function": {
                "name": "search",
                "description": "Search",
                "parameters": {
                    "type": "object",
                    "properties": {"q": {"type": "string"}},
                    "required": ["q"],
                    "additionalProperties": False,
                },
            },
        }
        self.record = SimpleNamespace(
            id="tool.search",
            addon_id="web-addon",
            legacy_name="search",
            function_schema=schema,
            input_schema=schema["function"]["parameters"],
            side_effects=["external_read"],
            approval_required=False,
            resource_requirements=[],
            status=SimpleNamespace(state="active"),
        )
        self.provider = SimpleNamespace(
            id="research.web",
            provider_id="web",
            domain="research",
            addon_id="web-addon",
            aliases=[],
            config={"connector_definition_id": "research.account"},
            status=SimpleNamespace(state="active"),
        )
        self.lookup_count = 0
        self.loaded_addons = {"web-addon": object()}
        self.plugins = {}
        self._dispatch_enabled_addons = {"web-addon"}
        self.dispatch_count = 0
        self.configs = []

    def get_tool_record(self, name):
        self.lookup_count += 1
        return self.record if name in {"tool.search", "search"} else None

    def get_provider_record(self, domain, provider_id):
        return self.provider if domain == "research" and provider_id == "web" else None

    def validate_visible_connection_binding(self, **kwargs):
        assert kwargs["connection_id"] == "conn"
        return SimpleNamespace(
            id="conn",
            connector_addon_id="web-addon",
            connector_definition_id="research.account",
            state="ready",
        )

    def validate_registered_tool_call(self, _name, arguments):
        return dict(arguments)

    def _execute_registered_tool_record_raw(
        self, _record, _name, arguments, config, *, instance=None
    ):
        assert instance is self.loaded_addons["web-addon"]
        self.dispatch_count += 1
        self.configs.append(dict(config or {}))
        return {"status": "ok", "result": arguments["q"]}


def _selection():
    return {
        "tool_id": "tool.search",
        "provider_id": "web",
        "provider_domain": "research",
        "addon_id": "web-addon",
        "connection_mode": "saved",
        "connection_id": "conn",
    }


def _declaration():
    return {
        "tool_name": "search",
        **_selection(),
        "read_only": True,
        "declaration": {
            "type": "function",
            "function": {
                "name": "search",
                "description": "Search",
                "parameters": {
                    "type": "object",
                    "properties": {"q": {"type": "string"}},
                    "required": ["q"],
                    "additionalProperties": False,
                },
            },
        },
    }


def _kwargs(record, operation, manager, adapter):
    return {
        "enabled": True,
        "operation": operation,
        "source_plan": record,
        "model": {"model_id": "research-model"},
        "settings": {"thinking_config": {"level": "high"}},
        "profile": {"profile": "quality"},
        "budget": {"max_model_calls": 2, "max_tool_calls": 3, "output_tokens": 200},
        "semantic_brief": {"blocks": [{"id": "b1", "role": "paragraph"}]},
        "research_question": {"questions": ["What does the term mean?"]},
        "addon_manager": manager,
        "model_adapter": adapter,
        "tool_declarations": [_declaration()],
        "selected_provider_ids": ["web"],
        "selected_addon_ids": ["web-addon"],
    }


def test_disabled_research_never_discovers_or_calls(app):
    record, operation = _state()
    manager = _Manager()
    calls = 0

    def adapter(**_kwargs):
        nonlocal calls
        calls += 1
        return {"conclusion": "never", "tool_calls": []}

    assert (
        _research.run_block_research(
            **{**_kwargs(record, operation, manager, adapter), "enabled": False}
        )
        is None
    )
    assert manager.lookup_count == 0
    assert calls == 0


def test_one_turn_completion_freezes_reasoning_and_is_admissible(app):
    record, operation = _state()
    manager = _Manager()
    seen = []

    def adapter(**kwargs):
        seen.append(kwargs)
        return {
            "conclusion": "The term is a stable proper name; preserve it.",
            "tool_calls": [],
        }

    conclusion = _research.run_block_research(
        **_kwargs(record, operation, manager, adapter)
    )

    assert len(seen) == 1
    assert seen[0]["provider_max_attempts"] == 1
    attempt = _models.TranslationAnalysisAttempt.query.one()
    assert seen[0]["action_id"] == attempt.id
    assert seen[0]["settings"] == {
        "thinking_config": {"level": "high"},
        "research_input_bytes": 320 * 1024,
        "research_result_bytes": 2 * 1024 * 1024,
    }
    assert "prefer Jina" in seen[0]["messages"][0]["content"]
    assert "stop calling tools" in seen[0]["messages"][0]["content"]
    assert conclusion and conclusion["conclusion"]["content"].startswith("The term")
    assert conclusion["run_state"] == "completed"


def test_full_source_packet_is_persisted_as_the_exact_first_research_request(app):
    record, operation = _state(source_text="x" * (2 * 1024 * 1024 + 4_096))
    manager = _Manager()
    plan = _persistence.rehydrate_source_plan(record)
    source = _research_input.build_research_source_material(plan, plan.blocks)
    seen = []

    def adapter(**kwargs):
        seen.append(kwargs)
        return {"conclusion": "The complete source was inspected.", "tool_calls": []}

    kwargs = _kwargs(record, operation, manager, adapter)
    kwargs["settings"] = {
        "thinking_config": {"level": "high"},
        "input_planner_contract": _research_input.RESEARCH_INPUT_PLANNER_CONTRACT_VERSION,
        "research_input_bytes": _research_input.MAX_RESEARCH_INPUT_BYTES,
    }
    kwargs["research_source"] = source
    kwargs["provider_admission"] = {
        "admitted": True,
        "requested_output_tokens": 200,
        "overflow_tokens": 0,
        "reason": None,
    }

    _research.run_block_research(**kwargs)

    assert len(seen) == 1
    assert len(seen[0]["messages"][1]["content"].encode()) > 2 * 1024 * 1024
    user_payload = json.loads(seen[0]["messages"][1]["content"])
    assert user_payload["source_document"] == source
    attempt = _models.TranslationAnalysisAttempt.query.one()
    packet = _research.rehydrate_analysis_attempt(attempt, record)
    assert packet["input"]["phase"] == "block-research-v2"
    assert packet["input"]["source_scope"] == source["scope"]
    assert packet["input"]["provider_admission"] == kwargs["provider_admission"]
    assert "tool_declarations" not in packet["input"]
    assert packet["tool_declarations"] == [_declaration()["declaration"]]


def test_typed_model_contract_failure_is_not_retried(app):
    record, operation = _state()
    manager = _Manager()
    calls = 0

    def adapter(**_kwargs):
        nonlocal calls
        calls += 1
        return {"conclusion": None, "tool_calls": []}

    with pytest.raises(BlockContractError, match="research-not-admissible"):
        _research.run_block_research(
            **{
                **_kwargs(record, operation, manager, adapter),
                "budget": {
                    "max_model_calls": 128,
                    "max_tool_calls": 3,
                    "output_tokens": 200,
                },
            }
        )

    assert calls == 1
    run = TranslationResearchRun.query.one()
    assert run.state == "failed"
    attempt = _models.TranslationAnalysisAttempt.query.one()
    assert attempt.dispatch_state == "provider_failed"


def test_worker_loads_exact_frozen_research_runtime_before_schema_validation(
    app,
    monkeypatch,
):
    record, operation = _state()
    manager = _Manager()
    expected_schema = manager.record.function_schema
    manager.record.function_schema = {}
    manager.loaded_addons = {}
    manager._dispatch_enabled_addons = set()
    loaded = []

    def ensure_worker_runtime_addons(addon_ids):
        loaded.append(tuple(addon_ids))
        manager.record.function_schema = expected_schema
        manager.loaded_addons["web-addon"] = object()
        manager._dispatch_enabled_addons.add("web-addon")
        return frozenset({"web-addon"})

    manager.ensure_worker_runtime_addons = ensure_worker_runtime_addons
    monkeypatch.setenv("CARLINA_RUNTIME_ROLE", "worker")
    monkeypatch.setenv("CARLINA_WORKER_ADDON_ID", "translator")

    result = _research.run_block_research(
        **_kwargs(
            record,
            operation,
            manager,
            lambda **_kwargs: {
                "conclusion": "The dynamically loaded schema is exact.",
                "tool_calls": [],
            },
        )
    )

    assert loaded == [("web-addon",)]
    assert result and result["run_state"] == "completed"


def test_worker_rejects_addon_selection_mismatch_before_runtime_loading(
    app,
    monkeypatch,
):
    record, operation = _state()
    manager = _Manager()
    loaded = []
    manager.ensure_worker_runtime_addons = lambda addon_ids: loaded.append(addon_ids)
    monkeypatch.setenv("CARLINA_RUNTIME_ROLE", "worker")
    monkeypatch.setenv("CARLINA_WORKER_ADDON_ID", "translator")

    with pytest.raises(BlockContractError, match="research-tool-selection"):
        _research.run_block_research(
            **{
                **_kwargs(
                    record,
                    operation,
                    manager,
                    lambda **_kwargs: {"conclusion": "never", "tool_calls": []},
                ),
                "selected_addon_ids": ["different-addon"],
            }
        )

    assert loaded == []


def test_enabled_research_without_tools_can_complete_from_frozen_context(app):
    record, operation = _state()
    manager = _Manager()
    seen = []

    def adapter(**kwargs):
        seen.append(kwargs)
        return {"conclusion": "No external evidence was needed.", "tool_calls": []}

    result = _research.run_block_research(
        **{
            **_kwargs(record, operation, manager, adapter),
            "tool_declarations": [],
            "selected_provider_ids": [],
            "selected_addon_ids": [],
        }
    )

    assert result and result["conclusion"]["content"] == (
        "No external evidence was needed."
    )
    assert seen[0]["tools"] == []
    assert manager.lookup_count == 0


def test_required_tool_evidence_rejects_empty_selection_before_dispatch(app):
    record, operation = _state()
    manager = _Manager()
    calls = 0

    def adapter(**_kwargs):
        nonlocal calls
        calls += 1
        return {"conclusion": "never", "tool_calls": []}

    with pytest.raises(BlockContractError, match="research-tool-required"):
        _research.run_block_research(
            **{
                **_kwargs(record, operation, manager, adapter),
                "research_question": {"require_tool_evidence": True},
                "tool_declarations": [],
                "selected_provider_ids": [],
                "selected_addon_ids": [],
            }
        )

    assert calls == 0
    assert TranslationResearchRun.query.count() == 0


def test_required_tool_evidence_rejects_zero_tool_budget_before_dispatch(app):
    record, operation = _state()
    manager = _Manager()
    calls = 0

    def adapter(**_kwargs):
        nonlocal calls
        calls += 1
        return {"conclusion": "never", "tool_calls": []}

    with pytest.raises(BlockContractError, match="research-tool-required"):
        _research.run_block_research(
            **{
                **_kwargs(record, operation, manager, adapter),
                "research_question": {"require_tool_evidence": True},
                "budget": {
                    "max_model_calls": 2,
                    "max_tool_calls": 0,
                    "output_tokens": 200,
                },
            }
        )

    assert calls == 0
    assert TranslationResearchRun.query.count() == 0


def test_required_tool_evidence_rejects_a_model_only_conclusion(app):
    record, operation = _state()
    manager = _Manager()

    with pytest.raises(BlockContractError, match="research-not-admissible"):
        _research.run_block_research(
            **{
                **_kwargs(
                    record,
                    operation,
                    manager,
                    lambda **_kwargs: {
                        "conclusion": "No external evidence was needed.",
                        "tool_calls": [],
                    },
                ),
                "research_question": {"require_tool_evidence": True},
            }
        )

    assert TranslationResearchRun.query.one().state == "failed"
    assert manager.dispatch_count == 0


def test_malformed_response_fails_closed(app):
    record, operation = _state()
    manager = _Manager()
    with pytest.raises(BlockContractError, match="research-not-admissible"):
        _research.run_block_research(
            **_kwargs(record, operation, manager, lambda **_kwargs: {"bad": "shape"})
        )


def test_completed_run_reentry_returns_evidence_without_repeat_provider_call(app):
    record, operation = _state()
    manager = _Manager()
    calls = 0

    def adapter(**_kwargs):
        nonlocal calls
        calls += 1
        return {"conclusion": "Evidence is complete.", "tool_calls": []}

    first = _research.run_block_research(**_kwargs(record, operation, manager, adapter))
    run = TranslationResearchRun.query.one()
    second = _research.run_block_research(
        **{**_kwargs(record, operation, manager, adapter), "run": run}
    )

    assert calls == 1
    assert first == second


def test_freeze_rejects_non_read_only_tool(app):
    manager = _Manager()
    manager.record.side_effects = ["external_write"]
    with pytest.raises(BlockContractError, match="research-tool-selection"):
        _research.freeze_block_research_tool_declarations(
            addon_manager=manager, owner_user_id=1, selections=[_selection()]
        )


def test_freeze_rejects_function_name_drift(app):
    manager = _Manager()
    manager.record.function_schema["function"]["name"] = "other"
    with pytest.raises(BlockContractError, match="research-tool-declaration"):
        _research.freeze_block_research_tool_declarations(
            addon_manager=manager, owner_user_id=1, selections=[_selection()]
        )


def test_run_rejects_live_schema_drift_before_provider_call(app):
    record, operation = _state()
    manager = _Manager()
    manager.record.function_schema["function"]["description"] = "Changed"
    calls = 0

    def adapter(**_kwargs):
        nonlocal calls
        calls += 1
        return {"conclusion": "never", "tool_calls": []}

    with pytest.raises(BlockContractError, match="research-tool-declarations-schema"):
        _research.run_block_research(**_kwargs(record, operation, manager, adapter))
    assert calls == 0


def test_run_reports_live_tool_availability_drift_precisely(app):
    record, operation = _state()
    manager = _Manager()
    manager.record.status.state = "inactive"

    with pytest.raises(
        BlockContractError, match="research-tool-declarations-live-tool"
    ):
        _research.run_block_research(
            **_kwargs(
                record,
                operation,
                manager,
                lambda **_kwargs: {"conclusion": "never", "tool_calls": []},
            )
        )


def test_run_reports_live_provider_availability_drift_precisely(app):
    record, operation = _state()
    manager = _Manager()
    manager.provider.status.state = "inactive"

    with pytest.raises(
        BlockContractError, match="research-tool-declarations-live-provider"
    ):
        _research.run_block_research(
            **_kwargs(
                record,
                operation,
                manager,
                lambda **_kwargs: {"conclusion": "never", "tool_calls": []},
            )
        )


def test_run_reports_function_binding_drift_precisely(app):
    record, operation = _state()
    manager = _Manager()
    manager.record.legacy_name = "renamed"

    with pytest.raises(
        BlockContractError, match="research-tool-declarations-function-binding"
    ):
        _research.run_block_research(
            **_kwargs(
                record,
                operation,
                manager,
                lambda **_kwargs: {"conclusion": "never", "tool_calls": []},
            )
        )


def test_run_accepts_saved_connection_with_exact_provider_definition(app):
    record, operation = _state()
    manager = _Manager()
    calls = 0

    def adapter(**_kwargs):
        nonlocal calls
        calls += 1
        return {"conclusion": "The account binding is exact.", "tool_calls": []}

    result = _research.run_block_research(
        **_kwargs(record, operation, manager, adapter)
    )

    assert calls == 1
    assert result and result["run_state"] == "completed"


def test_run_rejects_saved_connection_definition_drift_before_provider_call(app):
    record, operation = _state()
    manager = _Manager()
    manager.validate_visible_connection_binding = lambda **_kwargs: SimpleNamespace(
        id="conn",
        connector_addon_id="web-addon",
        connector_definition_id="other.account",
        state="ready",
    )
    calls = 0

    def adapter(**_kwargs):
        nonlocal calls
        calls += 1
        return {"conclusion": "never", "tool_calls": []}

    with pytest.raises(BlockContractError, match="research-tool-connection"):
        _research.run_block_research(**_kwargs(record, operation, manager, adapter))
    assert calls == 0


def test_run_rejects_saved_connection_without_provider_definition_proof(app):
    record, operation = _state()
    manager = _Manager()
    manager.provider.config = {}
    calls = 0

    def adapter(**_kwargs):
        nonlocal calls
        calls += 1
        return {"conclusion": "never", "tool_calls": []}

    with pytest.raises(BlockContractError, match="research-tool-connection"):
        _research.run_block_research(**_kwargs(record, operation, manager, adapter))
    assert calls == 0


def test_run_allows_connectionless_tool_without_connector_definition_proof(app):
    record, operation = _state()
    manager = _Manager()
    manager.provider.config = {}
    declaration = {
        **_declaration(),
        "connection_mode": "connectionless",
        "connection_id": "",
    }
    calls = 0

    def adapter(**_kwargs):
        nonlocal calls
        calls += 1
        return {
            "conclusion": "The public tool needs no saved account.",
            "tool_calls": [],
        }

    result = _research.run_block_research(
        **{
            **_kwargs(record, operation, manager, adapter),
            "tool_declarations": [declaration],
        }
    )

    assert calls == 1
    assert result and result["run_state"] == "completed"


def test_run_accepts_frozen_tool_declarations_above_the_legacy_half_mib_cap(app):
    record, operation = _state()
    manager = _Manager()
    records = {}
    declarations = []
    for index in range(5):
        name = f"search_{index}"
        schema = {
            "type": "function",
            "function": {
                "name": name,
                "description": "d" * 110_000,
                "parameters": {
                    "type": "object",
                    "properties": {"q": {"type": "string"}},
                    "required": ["q"],
                    "additionalProperties": False,
                },
            },
        }
        tool_id = f"tool.{name}"
        tool = SimpleNamespace(
            id=tool_id,
            addon_id="web-addon",
            legacy_name=name,
            function_schema=schema,
            input_schema=schema["function"]["parameters"],
            side_effects=["external_read"],
            approval_required=False,
            resource_requirements=[],
            status=SimpleNamespace(state="active"),
        )
        records[tool_id] = tool
        records[name] = tool
        declarations.append(
            {
                "tool_name": name,
                "tool_id": tool_id,
                "provider_id": "web",
                "provider_domain": "research",
                "addon_id": "web-addon",
                "connection_mode": "saved",
                "connection_id": "conn",
                "read_only": True,
                "declaration": schema,
            }
        )
    manager.get_tool_record = records.get
    encoded = json.dumps(declarations, separators=(",", ":")).encode()
    assert len(encoded) > 512 * 1024

    result = _research.run_block_research(
        **{
            **_kwargs(
                record,
                operation,
                manager,
                lambda **_kwargs: {"conclusion": "complete", "tool_calls": []},
            ),
            "tool_declarations": declarations,
        }
    )

    assert result and result["run_state"] == "completed"


def test_initial_run_and_first_turn_rollback_together(app, monkeypatch):
    record, operation = _state()
    manager = _Manager()

    def fail_reservation(**_kwargs):
        raise BlockContractError("research-turn-test")

    monkeypatch.setattr(_research, "reserve_research_turn", fail_reservation)
    with pytest.raises(BlockContractError, match="research-turn-test"):
        _research.run_block_research(
            **_kwargs(
                record,
                operation,
                manager,
                lambda **_kwargs: {"conclusion": "never", "tool_calls": []},
            )
        )
    assert TranslationResearchRun.query.count() == 0


def test_resume_rejects_substituted_selected_provider_configuration(app):
    record, operation = _state()
    manager = _Manager()
    _research.run_block_research(
        **_kwargs(
            record,
            operation,
            manager,
            lambda **_kwargs: {"conclusion": "complete", "tool_calls": []},
        )
    )
    run = TranslationResearchRun.query.one()
    changed = _kwargs(
        record,
        operation,
        manager,
        lambda **_kwargs: {"conclusion": "never", "tool_calls": []},
    )
    changed["selected_provider_ids"] = []
    with pytest.raises(BlockContractError, match="research-tool-selection"):
        _research.run_block_research(**{**changed, "run": run})


def test_active_run_without_a_first_turn_is_not_recomputed(app):
    record, operation = _state()
    manager = _Manager()
    run = _research.persist_research_run(
        operation=operation,
        source_plan=record,
        model={"model_id": "research-model"},
        settings={
            "thinking_config": {"level": "high"},
            "research_input_bytes": 320 * 1024,
            "research_result_bytes": 256 * 1024,
        },
        profile={"profile": "quality"},
        budget={"max_model_calls": 2, "max_tool_calls": 3, "output_tokens": 200},
        selected_provider_ids=["web"],
        selected_addon_ids=["web-addon"],
        tool_declarations=[_declaration()],
    )
    db.session.commit()
    calls = 0

    def adapter(**_kwargs):
        nonlocal calls
        calls += 1
        return {"conclusion": "never", "tool_calls": []}

    with pytest.raises(BlockContractError, match="research-not-admissible"):
        _research.run_block_research(
            **{**_kwargs(record, operation, manager, adapter), "run": run}
        )
    assert calls == 0


def test_reentry_claims_a_prepared_turn_once_without_new_reservation(app):
    record, operation = _state()
    manager = _Manager()
    declarations = _research.freeze_block_research_tool_declarations(
        addon_manager=manager,
        owner_user_id=operation.owner_user_id,
        selections=[_selection()],
    )
    settings = {
        "thinking_config": {"level": "high"},
        "research_input_bytes": 320 * 1024,
        "research_result_bytes": 256 * 1024,
    }
    run = _research.persist_research_run(
        operation=operation,
        source_plan=record,
        model={"model_id": "research-model"},
        settings=settings,
        profile={"profile": "quality"},
        budget={"max_model_calls": 2, "max_tool_calls": 3, "output_tokens": 200},
        selected_provider_ids=["web"],
        selected_addon_ids=["web-addon"],
        tool_declarations=declarations,
    )
    packet = _research._research_packet(
        semantic_brief={"blocks": [{"id": "b1", "role": "paragraph"}]},
        question={"questions": ["What does the term mean?"]},
        declarations=declarations,
        input_limit=320 * 1024,
    )
    _turn, _attempt = _research.reserve_research_turn(run=run, packet=packet)
    db.session.commit()
    calls = 0

    def adapter(**_kwargs):
        nonlocal calls
        calls += 1
        return {"conclusion": "Recovered prepared request.", "tool_calls": []}

    result = _research.run_block_research(
        **{**_kwargs(record, operation, manager, adapter), "run": run}
    )

    assert calls == 1
    assert result and result["run_state"] == "completed"


def test_stale_prepared_and_provider_failure_use_two_dispatch_budget(app):
    record, operation = _state()
    manager = _Manager()
    declarations = _research.freeze_block_research_tool_declarations(
        addon_manager=manager,
        owner_user_id=operation.owner_user_id,
        selections=[_selection()],
    )
    settings = {
        "thinking_config": {"level": "high"},
        "research_input_bytes": 320 * 1024,
        "research_result_bytes": 256 * 1024,
    }
    run = _research.persist_research_run(
        operation=operation,
        source_plan=record,
        model={"model_id": "research-model"},
        settings=settings,
        profile={"profile": "quality"},
        budget={"max_model_calls": 2, "max_tool_calls": 3, "output_tokens": 200},
        selected_provider_ids=["web"],
        selected_addon_ids=["web-addon"],
        tool_declarations=declarations,
    )
    packet = _research._research_packet(
        semantic_brief={"blocks": [{"id": "b1", "role": "paragraph"}]},
        question={"questions": ["What does the term mean?"]},
        declarations=declarations,
        input_limit=320 * 1024,
    )
    _turn, stale = _research.reserve_research_turn(run=run, packet=packet)
    db.session.commit()

    operation = _operations.renew_operation_lease(
        operation, expected_version=operation.version
    )
    db.session.commit()
    calls = 0

    def adapter(**_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("transient provider failure")
        return {"conclusion": "Recovered with exact evidence.", "tool_calls": []}

    result = _research.run_block_research(
        **{**_kwargs(record, operation, manager, adapter), "run": run}
    )

    attempts = _models.TranslationAnalysisAttempt.query.order_by(
        _models.TranslationAnalysisAttempt.created_at
    ).all()
    assert calls == 2
    assert result and result["run_state"] == "completed"
    assert len(attempts) == 3
    assert attempts[0].id == stale.id
    assert attempts[0].dispatch_state == "fence_failed"
    assert attempts[1].dispatch_state == "provider_failed"
    assert attempts[1].retry_of_attempt_id == attempts[0].id
    assert attempts[2].dispatch_state == "completed"
    assert attempts[2].retry_of_attempt_id == attempts[1].id


def test_provider_failure_retry_can_request_tools_and_rebase(app):
    record, operation = _state()
    manager = _Manager()
    model_calls = 0

    def adapter(**_kwargs):
        nonlocal model_calls
        model_calls += 1
        if model_calls == 1:
            raise RuntimeError("transient provider failure")
        if model_calls == 2:
            return {
                "conclusion": None,
                "tool_calls": [
                    {
                        "id": "lookup-after-retry",
                        "type": "function",
                        "function": {
                            "name": "search",
                            "arguments": '{"q":"term after retry"}',
                        },
                    }
                ],
            }
        return {
            "conclusion": "The retried lookup confirms the term's usage.",
            "tool_calls": [],
        }

    result = _research.run_block_research(
        **{
            **_kwargs(record, operation, manager, adapter),
            "budget": {
                "max_model_calls": 3,
                "max_tool_calls": 1,
                "output_tokens": 200,
            },
            "research_question": {"require_tool_evidence": True},
        }
    )

    attempts = _models.TranslationAnalysisAttempt.query.order_by(
        _models.TranslationAnalysisAttempt.created_at
    ).all()
    assert model_calls == 3
    assert manager.dispatch_count == 1
    assert result and result["run_state"] == "completed"
    assert [attempt.dispatch_state for attempt in attempts] == [
        "provider_failed",
        "completed",
        "completed",
    ]
    assert attempts[1].retry_of_attempt_id == attempts[0].id
    assert attempts[2].rebase_of_attempt_id == attempts[1].id


def test_definite_tool_failure_is_feedback_and_research_continues(app):
    record, operation = _state()
    manager = _Manager()
    model_calls = 0
    follow_up_messages = []

    def dispatch(_record, _name, arguments, _config, *, instance=None):
        assert instance is manager.loaded_addons["web-addon"]
        manager.dispatch_count += 1
        if arguments["q"] == "unavailable source":
            raise RuntimeError("connector unavailable")
        return {"status": "ok", "result": arguments["q"]}

    manager._execute_registered_tool_record_raw = dispatch

    def adapter(**kwargs):
        nonlocal model_calls
        model_calls += 1
        if model_calls == 1:
            return {
                "conclusion": None,
                "tool_calls": [
                    {
                        "id": "available",
                        "type": "function",
                        "function": {
                            "name": "search",
                            "arguments": '{"q":"available source"}',
                        },
                    },
                    {
                        "id": "unavailable",
                        "type": "function",
                        "function": {
                            "name": "search",
                            "arguments": '{"q":"unavailable source"}',
                        },
                    },
                ],
            }
        follow_up_messages.extend(kwargs["messages"])
        return {
            "conclusion": "The available source is sufficient.",
            "tool_calls": [],
        }

    result = _research.run_block_research(
        **{
            **_kwargs(record, operation, manager, adapter),
            "budget": {
                "max_model_calls": 2,
                "max_tool_calls": 2,
                "output_tokens": 200,
            },
            "research_question": {"require_tool_evidence": True},
        }
    )

    calls = TranslationResearchToolCall.query.order_by(
        TranslationResearchToolCall.call_index
    ).all()
    assert model_calls == 2
    assert manager.dispatch_count == 2
    assert result and result["run_state"] == "completed"
    assert [call.state for call in calls] == ["succeeded", "failed"]
    assert any(
        message.get("role") == "tool"
        and '"state":"failed"' in message.get("content", "")
        for message in follow_up_messages
    )


def test_null_content_tool_turn_is_witnessed_once_before_follow_up_completion(app):
    record, operation = _state()
    manager = _Manager()
    calls = 0

    def adapter(**_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {
                "conclusion": None,
                "tool_calls": [
                    {
                        "id": "lookup-1",
                        "type": "function",
                        "function": {"name": "search", "arguments": '{"q":"term"}'},
                    }
                ],
            }
        return {"conclusion": "The lookup confirms the term's usage.", "tool_calls": []}

    conclusion = _research.run_block_research(
        **{
            **_kwargs(record, operation, manager, adapter),
            "research_question": {"require_tool_evidence": True},
        }
    )

    assert conclusion and conclusion["run_state"] == "completed"
    assert calls == 2
    assert manager.dispatch_count == 1

    run = TranslationResearchRun.query.one()
    resumed = _research.run_block_research(
        **{
            **_kwargs(record, operation, manager, adapter),
            "research_question": {"require_tool_evidence": True},
            "run": run,
        }
    )
    assert resumed == conclusion
    assert calls == 2
    assert manager.dispatch_count == 1


def test_budget_blocked_batch_returns_feedback_and_allows_a_conclusion(app):
    record, operation = _state()
    manager = _Manager()
    model_calls = 0

    def adapter(**_kwargs):
        nonlocal model_calls
        model_calls += 1
        if model_calls == 1:
            return {
                "conclusion": None,
                "tool_calls": [
                    {
                        "id": f"over-budget-{ordinal}",
                        "type": "function",
                        "function": {
                            "name": "search",
                            "arguments": json.dumps({"q": f"term {ordinal}"}),
                        },
                    }
                    for ordinal in range(1, 3)
                ],
            }
        return {
            "conclusion": "The available evidence is sufficient.",
            "tool_calls": [],
        }

    kwargs = _kwargs(record, operation, manager, adapter)
    kwargs["budget"] = {
        "max_model_calls": 2,
        "max_tool_calls": 1,
        "output_tokens": 200,
    }

    conclusion = _research.run_block_research(**kwargs)

    assert conclusion and conclusion["run_state"] == "completed"
    assert model_calls == 2
    assert manager.dispatch_count == 0
    calls = TranslationResearchToolCall.query.order_by(
        TranslationResearchToolCall.call_index
    ).all()
    assert [call.state for call in calls] == ["blocked", "blocked"]
    assert all(call.argument_state == "parsed" for call in calls)
    assert [call.error_json for call in calls] == [
        {
            "reason": "tool_call_budget_exceeded",
            "requested": 2,
            "remaining": 1,
        },
        {
            "reason": "tool_call_budget_exceeded",
            "requested": 2,
            "remaining": 1,
        },
    ]


def test_multi_turn_research_handoff_has_a_bounded_query_budget(app):
    record, operation = _state()
    manager = _Manager()
    model_calls = 0

    def adapter(**_kwargs):
        nonlocal model_calls
        model_calls += 1
        if model_calls == 6:
            return {"conclusion": "The evidence is sufficient.", "tool_calls": []}
        return {
            "conclusion": None,
            "tool_calls": [
                {
                    "id": f"lookup-{model_calls}-{ordinal}",
                    "type": "function",
                    "function": {
                        "name": "search",
                        "arguments": json.dumps(
                            {"q": f"term {model_calls} {ordinal}"},
                            separators=(",", ":"),
                        ),
                    },
                }
                for ordinal in range(1, 4)
            ],
        }

    kwargs = _kwargs(record, operation, manager, adapter)
    kwargs["budget"] = {
        "max_model_calls": 6,
        "max_tool_calls": 15,
        "output_tokens": 200,
    }
    kwargs["verified_plan"] = _persistence.rehydrate_source_plan(record)

    with _measure_queries() as metrics:
        conclusion = _research.run_block_research(**kwargs)

    assert conclusion and conclusion["run_state"] == "completed"
    assert model_calls == 6
    assert manager.dispatch_count == 15
    assert metrics.statement_count <= 2_000


def test_transient_reservation_lock_retries_without_duplicate_tool_dispatch(
    app,
    monkeypatch,
):
    record, operation = _state()
    manager = _Manager()
    model_calls = 0
    active = db.session()
    real_flush = active.flush
    reservation_flushes = 0

    def flush_with_one_transient_lock(*args, **kwargs):
        nonlocal reservation_flushes
        reserving_call = any(
            isinstance(item, TranslationResearchToolCall) for item in active.new
        )
        if reserving_call:
            reservation_flushes += 1
            if reservation_flushes == 1:
                raise OperationalError(
                    "INSERT INTO translation_research_tool_call",
                    {},
                    sqlite3.OperationalError("database is locked"),
                )
        return real_flush(*args, **kwargs)

    def adapter(**_kwargs):
        nonlocal model_calls
        model_calls += 1
        if model_calls == 1:
            return {
                "conclusion": None,
                "tool_calls": [
                    {
                        "id": "lookup-after-lock",
                        "type": "function",
                        "function": {
                            "name": "search",
                            "arguments": '{"q":"term"}',
                        },
                    }
                ],
            }
        return {"conclusion": "The lookup survived contention.", "tool_calls": []}

    monkeypatch.setattr(active, "flush", flush_with_one_transient_lock)

    conclusion = _research.run_block_research(
        **{
            **_kwargs(record, operation, manager, adapter),
            "research_question": {"require_tool_evidence": True},
        }
    )

    assert conclusion and conclusion["run_state"] == "completed"
    assert reservation_flushes == 2
    assert model_calls == 2
    assert manager.dispatch_count == 1
    assert TranslationResearchToolCall.query.count() == 1


def test_invalid_tool_arguments_are_witnessed_and_valid_siblings_continue(app):
    record, operation = _state()
    manager = _Manager()
    model_calls = 0
    continuation_messages = []

    def adapter(**kwargs):
        nonlocal model_calls
        model_calls += 1
        if model_calls == 1:
            return {
                "conclusion": None,
                "tool_calls": [
                    {
                        "id": "lookup-invalid",
                        "type": "function",
                        "function": {"name": "search", "arguments": "{}"},
                    },
                    {
                        "id": "lookup-valid",
                        "type": "function",
                        "function": {
                            "name": "search",
                            "arguments": '{"q":"term"}',
                        },
                    },
                ],
            }
        continuation_messages.extend(kwargs["messages"])
        return {
            "conclusion": "The valid lookup supplies the required evidence.",
            "tool_calls": [],
        }

    conclusion = _research.run_block_research(
        **{
            **_kwargs(record, operation, manager, adapter),
            "research_question": {"require_tool_evidence": True},
        }
    )

    assert conclusion and conclusion["run_state"] == "completed"
    assert model_calls == 2
    assert manager.dispatch_count == 1
    calls = _models.TranslationResearchToolCall.query.order_by(
        _models.TranslationResearchToolCall.call_index
    ).all()
    assert [(call.state, call.argument_state) for call in calls] == [
        ("blocked", "invalid"),
        ("succeeded", "parsed"),
    ]
    tool_messages = [
        message for message in continuation_messages if message["role"] == "tool"
    ]
    assert len(tool_messages) == 2
    assert json.loads(tool_messages[0]["content"])["state"] == "blocked"
    assert json.loads(tool_messages[1]["content"])["state"] == "succeeded"
