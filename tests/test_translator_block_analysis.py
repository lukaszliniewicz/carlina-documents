"""Focused, provider-free tests for semantic block analysis map/reduce."""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

import pytest
from content_storage_support import bind_content_policy
from flask import Flask

from carlina.core.semantic_html import SEMANTIC_HTML_CONTRACT_VERSION
from carlina.core.utc import utc_now_db
from iarf_assistant.addons.translator import block_analysis
from iarf_assistant.addons.translator.analysis_contract import private_witness
from iarf_assistant.addons.translator.analysis_execution import (
    terminalize_stale_prepared_analysis_attempt,
)
from iarf_assistant.addons.translator.analysis_persistence import _artifact_hash
from iarf_assistant.addons.translator.block_analysis import (
    ANALYSIS_KIND,
    ANALYSIS_MAP_SCHEMA_VERSION,
    ANALYSIS_REDUCE_KIND,
    AnalysisMapResult,
    AnalysisScope,
    build_analysis_packet,
    build_analysis_scopes,
    build_reduce_packet,
    discover_exact_map_attempt,
    discover_exact_reduce_attempt,
    execute_block_analysis_attempt,
    parse_analysis_map_result,
    persist_reduced_analysis_artifacts,
    prepare_block_analysis_attempt,
    prepare_reduce_analysis_attempt,
    reduce_analysis_results,
    source_blocks_for_scope,
    whole_document_analysis_scope,
)
from iarf_assistant.addons.translator.block_contract import (
    AttestedDocumentSnapshot,
    BlockContractError,
    SourceProviderAttestation,
)
from iarf_assistant.addons.translator.block_persistence import (
    persist_source_plan,
    source_attestation_for_project,
)
from iarf_assistant.addons.translator.block_planner import plan_document_blocks
from iarf_assistant.addons.translator.models import (
    TranslationContextArtifactRevision,
    TranslationGlossaryActive,
    TranslationGlossaryConcept,
    TranslationGlossaryRevision,
    TranslationOperation,
    TranslationProject,
)
from iarf_assistant.core.encryption import EncryptionService, InstallationKeyStore
from iarf_assistant.extensions import db
from iarf_assistant.models import User


def _plan(html: str):
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
        semantic_contract_version=SEMANTIC_HTML_CONTRACT_VERSION,
    )
    return plan_document_blocks(
        snapshot,
        project_id="project",
        source_attestation=SourceProviderAttestation(
            provider_addon_id="doc",
            provider_capability_id="process",
            owner_id="project",
            resource_id="doc",
        ),
        source_language="en",
    )


@pytest.fixture
def app(tmp_path: Path):
    application = Flask(__name__, instance_path=str(tmp_path / "instance"))
    bind_content_policy(application)
    application.config.update(
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{tmp_path / 'block-analysis.db'}",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SECRET_KEY="block-analysis-test",
        TESTING=True,
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


def _durable_plan_record():
    user = cast(Any, User)(email="block-analysis@example.test", password_hash="x")
    db.session.add(user)
    db.session.flush()
    project = cast(Any, TranslationProject)(
        user_id=user.id,
        name="Block analysis",
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
                "resource_id": "document",
            }
        },
    )
    db.session.add(project)
    db.session.flush()
    html = "<html><body><p>Durable analysis source.</p></body></html>"
    digest = hashlib.sha256(html.encode()).hexdigest()
    planned = plan_document_blocks(
        AttestedDocumentSnapshot(
            id="document",
            document_id="document",
            output_html=html,
            content_hash=digest,
            semantic_content_hash=digest,
            metadata={},
            provenance={},
            captured_at="2026-08-25T00:00:00+00:00",
            semantic_contract_version=SEMANTIC_HTML_CONTRACT_VERSION,
        ),
        project_id=project.id,
        source_attestation=source_attestation_for_project(project),
        source_language="en",
    )
    record, _ = persist_source_plan(project, planned)
    db.session.commit()
    return record, planned


def _running_operation(record, plan, *, key: str):
    operation = cast(Any, TranslationOperation)(
        project_id=record.project_id,
        owner_user_id=record.project.user_id,
        kind="document_block_translate",
        idempotency_key=key,
        request_fingerprint=hashlib.sha256(key.encode()).hexdigest(),
        state="running",
        queue_job_id=f"{key}-worker",
        source_content_hash=plan.snapshot.content_hash,
        source_plan_id=record.id,
        source_plan_hash=plan.plan_hash,
        lease_expires_at=utc_now_db() + timedelta(minutes=5),
    )
    db.session.add(operation)
    db.session.commit()
    return operation


def _map(scope: AnalysisScope, *, suffix: str = "") -> AnalysisMapResult:
    first = scope.block_ids[0]
    return AnalysisMapResult(
        source_plan_hash=scope.source_plan_hash,
        part_index=scope.part_index,
        block_ids=scope.block_ids,
        summary={
            "text": f"Summary {scope.part_index}{suffix}.",
            "domains": ["history", "history"],
            "audience": "researchers",
            "source_block_ids": [first],
        },
        style={
            "register": "formal",
            "guidance": ["Use precise terms."],
            "prohibitions": ["Do not invent facts."],
            "source_block_ids": [first],
        },
        entities=(
            {
                "name": "Ada",
                "type": "person",
                "aliases": ["A."],
                "source_block_ids": [first],
            },
        ),
        terminology_candidates=(
            {
                "source_term": "signal",
                "variants": ["signals"],
                "proposed_target": "sygnał",
                "definition": "A meaningful indication.",
                "source_block_ids": [first],
                "confidence": 0.8,
            },
        ),
        unresolved=(
            {
                "question": "Which edition is intended?",
                "reason": "The source does not identify one.",
                "impact": "medium",
                "source_block_ids": [first],
            },
        ),
    )


def test_scopes_are_source_ordered_and_break_at_headings():
    plan = _plan(
        "<html><body><h1>One</h1><p>First paragraph.</p>"
        "<h2>Two</h2><p>Second paragraph.</p></body></html>"
    )
    scopes = build_analysis_scopes(plan, max_input_bytes=2_000, context_bytes=0)

    assert [scope.part_index for scope in scopes] == list(range(1, len(scopes) + 1))
    assert [block_id for scope in scopes for block_id in scope.block_ids] == [
        block.id for block in plan.blocks
    ]
    assert len(scopes) == 2
    assert scopes[0].heading_path[-1].text == "One"
    assert scopes[1].heading_path[-1].text == "Two"
    assert all(item.derived for scope in scopes for item in scope.heading_path)
    assert all(
        item.source == "derived-ordered-heading-boundaries-v1"
        for scope in scopes
        for item in scope.heading_path
    )


def test_whole_document_scope_crosses_heading_boundaries_without_splitting_blocks():
    plan = _plan(
        "<html><body><h1>One</h1><p>First.</p>"
        "<h2>Two</h2><p>Second.</p><h2>Three</h2><p>Third.</p></body></html>"
    )

    scope = whole_document_analysis_scope(plan)

    assert scope.part_index == 1
    assert scope.heading_path == ()
    assert scope.block_ids == tuple(block.id for block in plan.blocks)
    assert len(scope.block_ids) > 4


def test_scopes_greedily_subdivide_only_at_complete_blocks():
    plan = _plan(
        "<html><body><h1>One</h1><p>First paragraph with enough words.</p>"
        "<p>Second paragraph with enough words.</p></body></html>"
    )
    scopes = build_analysis_scopes(plan, max_input_bytes=700, context_bytes=0)

    assert [block_id for scope in scopes for block_id in scope.block_ids] == [
        block.id for block in plan.blocks
    ]
    assert all(scope.block_ids for scope in scopes)
    assert all(
        set(scope.block_ids).isdisjoint(other.block_ids)
        for index, scope in enumerate(scopes)
        for other in scopes[index + 1 :]
    )


def test_oversized_singleton_fails_with_stable_code():
    plan = _plan("<html><body><p>" + ("x" * 3_000) + "</p></body></html>")
    with pytest.raises(BlockContractError, match="analysis-oversized-block"):
        build_analysis_scopes(plan, max_input_bytes=512, context_bytes=0)


def test_scope_packet_contains_only_whole_source_blocks_and_exact_schema():
    plan = _plan(
        "<html><body><h1>Heading</h1><p>Body <em>text</em> and "
        '<a href="https://example.test/source">link</a>.</p></body></html>'
    )
    scope = build_analysis_scopes(plan, max_input_bytes=4_000, context_bytes=0)[0]
    packet = build_analysis_packet(
        plan,
        scope,
        phase={"model": {"model_id": "saved/model"}, "reasoning": {"level": "off"}},
    )

    assert set(packet) == {"messages", "input"}
    packet_input = cast(dict[str, object], packet["input"])
    assert packet_input["scope"] == scope.to_dict()
    source_blocks = cast(list[dict[str, object]], packet_input["source_blocks"])
    assert [item["block_id"] for item in source_blocks] == list(scope.block_ids)
    assert all(
        set(item) == {"block_id", "role", "content", "fields"} for item in source_blocks
    )
    assert source_blocks[1]["content"] == 'Body <em>text</em> and <a r="1">link</a>.'
    # The host owns protected link targets and restores them after translation;
    # language analysis receives the meaningful inline relationship, not URLs.
    assert source_blocks[1]["fields"] == []
    assert "https://example.test/source" not in json.dumps(source_blocks)
    assert not any(
        key in item
        for item in source_blocks
        for key in ("anchors", "locator", "order", "protected_inline", "slots")
    )
    assert packet_input["output_schema"] == ANALYSIS_MAP_SCHEMA_VERSION
    messages = cast(list[dict[str, str]], packet["messages"])
    assert "source_blocks" in messages[1]["content"]
    user_payload = json.loads(messages[1]["content"])
    response_contract = user_payload["response_contract"]
    assert response_contract["json_schema"]["additionalProperties"] is False
    assert response_contract["exact_values"] == {
        "schema_version": ANALYSIS_MAP_SCHEMA_VERSION,
        "source_plan_hash": scope.source_plan_hash,
        "part_index": scope.part_index,
        "block_ids": list(scope.block_ids),
    }


def test_analysis_context_accepts_a_source_block_above_target_output_limits():
    source_text = "x" * (600 * 1024)
    plan = _plan(f"<html><body><p>{source_text}</p></body></html>")
    scope = whole_document_analysis_scope(plan)

    source_blocks = source_blocks_for_scope(plan, scope)

    assert len(source_blocks) == 1
    assert source_blocks[0]["content"] == source_text
    assert source_blocks[0]["fields"] == []


def test_map_result_requires_exact_source_links_and_strict_json():
    plan = _plan("<html><body><p>Body text.</p></body></html>")
    scope = build_analysis_scopes(plan, max_input_bytes=2_000, context_bytes=0)[0]
    result = _map(scope)
    restored = parse_analysis_map_result(result.to_dict(), scope)
    assert restored.to_dict() == result.to_dict()

    invalid = result.to_dict()
    invalid["entities"] = [
        {
            "name": "Ada",
            "type": "person",
            "aliases": [],
            "source_block_ids": ["not-in-scope"],
        }
    ]
    with pytest.raises(BlockContractError, match="analysis-map-entity"):
        parse_analysis_map_result(invalid, scope)
    with pytest.raises(BlockContractError, match="analysis-map-json"):
        parse_analysis_map_result("```json\n{}\n```", scope)


def test_map_result_canonicalizes_a_bounded_audience_list_from_text_models():
    plan = _plan("<html><body><p>Body text.</p></body></html>")
    scope = build_analysis_scopes(plan, max_input_bytes=2_000, context_bytes=0)[0]
    payload = _map(scope).to_dict()
    payload["summary"]["audience"] = [
        "academic researchers",
        "technical readers",
        "academic researchers",
    ]

    restored = parse_analysis_map_result(payload, scope)

    assert restored.summary["audience"] == (
        "academic researchers; technical readers"
    )


@pytest.mark.parametrize("audience", [[], {"primary": "researchers"}, [""]])
def test_map_result_rejects_noncanonicalizable_audience_shapes(audience):
    plan = _plan("<html><body><p>Body text.</p></body></html>")
    scope = build_analysis_scopes(plan, max_input_bytes=2_000, context_bytes=0)[0]
    payload = _map(scope).to_dict()
    payload["summary"]["audience"] = audience

    with pytest.raises(BlockContractError, match="analysis-map-summary"):
        parse_analysis_map_result(payload, scope)


def test_reducer_emits_exact_context_payload_shapes_and_proposals_only():
    plan = _plan(
        "<html><body><h1>One</h1><p>First.</p><h2>Two</h2><p>Second.</p></body></html>"
    )
    scopes = build_analysis_scopes(plan, max_input_bytes=2_000, context_bytes=0)
    reduced = reduce_analysis_results([_map(scope) for scope in scopes])

    assert set(reduced.document_brief) == {"summary", "domains", "audience"}
    assert set(reduced.style_guide) == {"register", "guidance", "prohibitions"}
    assert set(reduced.entity_register) == {"entities"}
    assert reduced.document_brief["domains"] == ["history"]
    assert reduced.entity_register["entities"] == [
        {"name": "Ada", "type": "person", "aliases": ["A."]}
    ]
    assert reduced.glossary_proposals[0]["status"] == "proposed"
    assert reduced.glossary_proposals[0]["target_term"] == "sygnał"
    assert reduced.to_dict()["schema_version"] == "analysis-reduce-v1"


def test_source_block_materialization_rejects_other_plan():
    plan = _plan("<html><body><p>Body text.</p></body></html>")
    scope = build_analysis_scopes(plan, max_input_bytes=2_000, context_bytes=0)[0]
    other = _plan("<html><body><p>Other text.</p></body></html>")
    with pytest.raises(BlockContractError, match="analysis-scope-plan"):
        source_blocks_for_scope(other, scope)


def _phase(*, native: bool) -> dict[str, object]:
    return {
        "model": {
            "model_id": "saved/model",
            "capability_profile": {
                "supports_structured_output": native,
                "structured_output_mode": "json_schema" if native else "none",
            },
        },
        "reasoning": {"config": {"level": "off"}},
    }


def test_reduce_packet_uses_native_schema_only_when_frozen_capability_allows_it():
    plan = _plan("<html><body><p>Body text.</p></body></html>")
    scope = build_analysis_scopes(plan, max_input_bytes=2_000, context_bytes=0)[0]
    result = _map(scope)

    native = build_reduce_packet([result], phase=_phase(native=True))
    native_input = cast(dict[str, object], native["input"])
    assert native_input["transport"] == "native_json_schema"
    native_format = cast(dict[str, object], native_input["response_format"])
    assert native_format["type"] == "json_schema"
    assert cast(dict[str, object], native_format["json_schema"])["strict"] is True

    text = build_reduce_packet([result], phase=_phase(native=False))
    text_input = cast(dict[str, object], text["input"])
    assert text_input["transport"] == "strict_text_json"
    assert text_input["response_format"] is None
    assert (
        "no Markdown fences"
        in cast(list[dict[str, str]], text["messages"])[0]["content"]
    )
    text_payload = json.loads(
        cast(list[dict[str, str]], text["messages"])[1]["content"]
    )
    assert text_payload["response_contract"]["exact_values"] == {
        "schema_version": "analysis-reduce-v1",
        "source_plan_hash": result.source_plan_hash,
        "scope_hashes": [result.scope_hash],
    }

    native_payload = json.loads(
        cast(list[dict[str, str]], native["messages"])[1]["content"]
    )
    assert "response_contract" not in native_payload


def test_map_packet_and_execution_use_the_same_capability_derived_transport():
    plan = _plan("<html><body><p>Body text.</p></body></html>")
    scope = build_analysis_scopes(plan, max_input_bytes=2_000, context_bytes=0)[0]
    attempt = SimpleNamespace(id="map-attempt")

    for native in (False, True):
        packet = build_analysis_packet(plan, scope, phase=_phase(native=native))
        packet["budget"] = {"output_tokens": 2_000}
        packet["profile"] = {
            "profile_version": block_analysis.ANALYSIS_PROFILE_VERSION,
            "provider_output_policy": block_analysis.PROVIDER_DEFAULT_OUTPUT_POLICY,
        }
        packet_input = cast(dict[str, object], packet["input"])
        expected = packet_input["response_format"]
        captured = {}

        def call_provider(_service, *, _captured=captured, **kwargs):
            _captured.update(kwargs)
            return {
                "role": "assistant",
                "content": json.dumps(_map(scope).to_dict()),
            }

        with (
            patch.object(
                block_analysis,
                "execute_analysis_attempt",
                side_effect=lambda _attempt, *, completion_call, _packet=packet: (
                    completion_call(_packet)
                ),
            ),
            patch.object(
                block_analysis,
                "rehydrate_analysis_attempt",
                return_value={"input": packet_input},
            ),
            patch.object(block_analysis, "call_analysis", side_effect=call_provider),
        ):
            result = execute_block_analysis_attempt(
                attempt,
                provider_service=object(),
            )

        assert result.to_dict() == _map(scope).to_dict()
        assert (
            cast(dict[str, object], captured["packet"])["response_format"] == expected
        )
        assert cast(dict[str, object], captured["packet"])[
            "max_completion_tokens"
        ] is None
        if native:
            assert cast(dict[str, object], expected)["type"] == "json_schema"
        else:
            assert expected is None


def test_analysis_output_policy_preserves_legacy_replay_only():
    budget = {"output_tokens": 2_000}

    assert block_analysis._provider_output_limit(
        {
            "budget": budget,
            "profile": {"profile_version": "1"},
        }
    ) == 2_000
    for profile_version in ("2", block_analysis.ANALYSIS_PROFILE_VERSION):
        assert (
            block_analysis._provider_output_limit(
                {
                    "budget": budget,
                    "profile": {
                        "profile_version": profile_version,
                        "provider_output_policy": (
                            block_analysis.PROVIDER_DEFAULT_OUTPUT_POLICY
                        ),
                    },
                }
            )
            is None
        )


def test_provider_reduce_result_requires_exact_scope_lineage_and_source_links():
    plan = _plan("<html><body><h1>One</h1><p>Body text.</p></body></html>")
    scope = build_analysis_scopes(plan, max_input_bytes=2_000, context_bytes=0)[0]
    map_result = _map(scope)
    reduced = reduce_analysis_results([map_result])
    mapped_ids = list(scope.block_ids)
    restored = type(reduced).from_response(
        reduced.to_dict(),
        source_plan_hash=plan.plan_hash,
        scope_hashes=list(reduced.scope_hashes),
        mapped_block_ids=mapped_ids,
    )
    assert restored.to_dict() == reduced.to_dict()

    wrong_scope = reduced.to_dict()
    wrong_scope["scope_hashes"] = ["f" * 64]
    with pytest.raises(BlockContractError, match="analysis-reduce-scopes"):
        type(reduced).from_response(
            wrong_scope,
            source_plan_hash=plan.plan_hash,
            scope_hashes=list(reduced.scope_hashes),
            mapped_block_ids=mapped_ids,
        )

    wrong_link = reduced.to_dict()
    wrong_link["unresolved"] = [
        {
            "question": "Which edition?",
            "reason": "Not stated.",
            "impact": "medium",
            "source_block_ids": ["not-mapped"],
        }
    ]
    with pytest.raises(BlockContractError, match="analysis-reduce-unresolved"):
        type(reduced).from_response(
            wrong_link,
            source_plan_hash=plan.plan_hash,
            scope_hashes=list(reduced.scope_hashes),
            mapped_block_ids=mapped_ids,
        )


def test_artifact_persistence_requires_provider_reduce_attempt_unless_explicit_fallback():
    plan = _plan("<html><body><p>Body text.</p></body></html>")
    scope = build_analysis_scopes(plan, max_input_bytes=2_000, context_bytes=0)[0]
    reduced = reduce_analysis_results([_map(scope)])
    map_attempt = {
        "id": "map-attempt",
        "attempt_record_hash": "a" * 64,
        "request_hash": "b" * 64,
    }
    with pytest.raises(BlockContractError, match="analysis-reduce-attempt-required"):
        persist_reduced_analysis_artifacts(
            object(),
            reduced,
            language="en",
            attempts=[map_attempt],
        )
    assert ANALYSIS_REDUCE_KIND == "terminology_consolidation"


def test_legacy_fixture_shape_can_be_created_without_runtime_state():
    """Keep the DTO usable in a unit-only process with no database session."""

    scope = AnalysisScope(
        source_plan_hash="a" * 64,
        part_index=1,
        block_ids=("block-1",),
        source_bytes=1,
    )
    result = _map(scope)
    assert SimpleNamespace(value=result).value.scope_hash


class _AttemptQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter_by(self, **_kwargs):
        return self

    def all(self):
        return list(self._rows)


class _AttemptSession:
    def __init__(self, rows):
        self._rows = rows

    def query(self, _model):
        return _AttemptQuery(self._rows)


def _sealed_attempt(*, state="prepared", version=7, parent=None):
    return SimpleNamespace(
        id=f"attempt-{state}-{version}-{parent or 'root'}",
        analysis_kind=ANALYSIS_KIND,
        contract_version="analysis-attempt-v1",
        source_plan_hash="a" * 64,
        operation_version=version,
        dispatch_state=state,
        retry_of_attempt_id=parent,
        rebase_of_attempt_id=None,
    )


def _discover_map(rows, *, operation_version=7):
    plan = SimpleNamespace(id="plan", plan_hash="a" * 64)
    operation = SimpleNamespace(id="operation", version=operation_version)
    phase = {"model": {"model_id": "saved/model"}}
    scope = AnalysisScope("a" * 64, 1, ("block",), source_bytes=1)
    packet = {"messages": [{"role": "system", "content": "exact"}], "input": {}}
    sealed = {
        "messages": packet["messages"],
        "input": packet["input"],
        "model": phase["model"],
        "profile": {
            "profile_id": block_analysis.ANALYSIS_PROFILE_ID,
            "profile_version": block_analysis.ANALYSIS_PROFILE_VERSION,
            "provider_output_policy": block_analysis.PROVIDER_DEFAULT_OUTPUT_POLICY,
            "phase": phase,
        },
        "settings": {"quality": "high"},
        "budget": {"output_tokens": 100},
        "tool_declarations": [],
    }
    with (
        patch.object(block_analysis, "rehydrate_source_plan", return_value=plan),
        patch.object(block_analysis, "build_analysis_packet", return_value=packet),
        patch.object(block_analysis, "rehydrate_analysis_attempt", return_value=sealed),
    ):
        return discover_exact_map_attempt(
            operation=operation,
            source_plan=plan,
            scope=scope,
            phase=phase,
            settings={"quality": "high"},
            budget={"output_tokens": 100},
            session=_AttemptSession(rows),
        )


def test_exact_map_attempt_discovery_distinguishes_absent_current_and_old_prepared():
    absent = _discover_map([])
    assert absent.attempt is None
    assert not absent.executable_under_current_generation

    prepared = _sealed_attempt()
    current = _discover_map([prepared])
    assert current.attempt is prepared
    assert current.executable_under_current_generation
    assert not current.resumable

    old = _sealed_attempt(version=6)
    recovered = _discover_map([old], operation_version=7)
    assert recovered.attempt is old
    # Generic analysis persistence accepts retry parents only after they are
    # terminal. A stale prepared row must first be terminalized without I/O.
    assert recovered.retry_lineage_attempt_id is None
    assert not recovered.executable_under_current_generation


def test_exact_attempt_discovery_selects_witnessed_response_never_dispatch_replay():
    response_received = _sealed_attempt(state="response_received")
    recovered = _discover_map([response_received])
    assert recovered.attempt is response_received
    assert recovered.resumable
    assert not recovered.executable_under_current_generation

    completed = _sealed_attempt(state="completed", version=6)
    completed_reuse = _discover_map([completed], operation_version=7)
    assert completed_reuse.attempt is completed
    assert completed_reuse.resumable

    stale_response = _sealed_attempt(state="response_received", version=6)
    stale_recovery = _discover_map([stale_response], operation_version=7)
    assert not stale_recovery.resumable

    failed = _sealed_attempt(state="provider_outcome_uncertain", version=6)
    retry = _discover_map([failed], operation_version=7)
    assert retry.retry_lineage_attempt_id == failed.id
    assert not retry.executable_under_current_generation
    assert not retry.resumable

    reconciled = _sealed_attempt(state="fence_failed", version=6)
    reconciled_retry = _discover_map([reconciled], operation_version=7)
    assert reconciled_retry.retry_lineage_attempt_id == reconciled.id

    dispatched = _sealed_attempt(state="dispatched")
    unresolved = _discover_map([dispatched])
    assert unresolved.attempt is dispatched
    assert unresolved.reconciliation_required
    assert not unresolved.resumable
    assert not unresolved.executable_under_current_generation


def test_exact_attempt_discovery_rejects_unrelated_matching_roots():
    with pytest.raises(BlockContractError, match="analysis-discovery-ambiguous"):
        _discover_map([_sealed_attempt(), _sealed_attempt(version=7, parent="other")])


def test_exact_reduce_attempt_discovery_uses_ordered_map_packet_and_frozen_config():
    scope = AnalysisScope("a" * 64, 1, ("block",), source_bytes=1)
    result = _map(scope)
    plan = SimpleNamespace(id="plan", plan_hash="a" * 64)
    operation = SimpleNamespace(id="operation", version=7)
    phase = {"model": {"model_id": "saved/model"}}
    packet = {"messages": [{"role": "system", "content": "reduce"}], "input": {}}
    attempt_data = vars(_sealed_attempt()).copy()
    attempt_data["analysis_kind"] = ANALYSIS_REDUCE_KIND
    attempt = SimpleNamespace(**attempt_data)
    sealed = {
        "messages": packet["messages"],
        "input": packet["input"],
        "model": phase["model"],
        "profile": {
            "profile_id": ANALYSIS_REDUCE_KIND,
            "profile_version": block_analysis.ANALYSIS_PROFILE_VERSION,
            "provider_output_policy": block_analysis.PROVIDER_DEFAULT_OUTPUT_POLICY,
            "transport": "strict_text_json",
            "response_format": None,
            "phase": phase,
        },
        "settings": {"quality": "high"},
        "budget": {"output_tokens": 100},
        "tool_declarations": [],
    }
    with (
        patch.object(block_analysis, "rehydrate_source_plan", return_value=plan),
        patch.object(block_analysis, "build_reduce_packet", return_value=packet),
        patch.object(block_analysis, "rehydrate_analysis_attempt", return_value=sealed),
    ):
        discovered = discover_exact_reduce_attempt(
            operation=operation,
            source_plan=plan,
            map_results=[result],
            phase=phase,
            settings={"quality": "high"},
            budget={"output_tokens": 100},
            session=_AttemptSession([attempt]),
        )
    assert discovered.attempt is attempt
    assert discovered.executable_under_current_generation

    completed_data = vars(_sealed_attempt(state="completed", version=6)).copy()
    completed_data["analysis_kind"] = ANALYSIS_REDUCE_KIND
    completed = SimpleNamespace(**completed_data)
    with (
        patch.object(block_analysis, "rehydrate_source_plan", return_value=plan),
        patch.object(block_analysis, "build_reduce_packet", return_value=packet),
        patch.object(block_analysis, "rehydrate_analysis_attempt", return_value=sealed),
    ):
        completed_discovery = discover_exact_reduce_attempt(
            operation=SimpleNamespace(id="operation", version=7),
            source_plan=plan,
            map_results=[result],
            phase=phase,
            settings={"quality": "high"},
            budget={"output_tokens": 100},
            session=_AttemptSession([completed]),
        )
    assert completed_discovery.resumable
    assert not completed_discovery.executable_under_current_generation


def _artifact_inputs(record, plan):
    scope = AnalysisScope(plan.plan_hash, 1, (plan.blocks[0].id,), source_bytes=1)
    reduced = reduce_analysis_results([_map(scope)])
    return {
        "source_plan": record,
        "reduced": reduced,
        "language": "en",
        "attempts": [
            {
                "id": "map-attempt",
                "attempt_record_hash": "a" * 64,
                "request_hash": "b" * 64,
            }
        ],
        "reduce_attempt": {
            "id": "reduce-attempt",
            "analysis_kind": ANALYSIS_REDUCE_KIND,
            "attempt_record_hash": "c" * 64,
            "request_hash": "d" * 64,
        },
        "phase": {"model": {"model_id": "saved/model"}},
        "settings": {"quality": "high"},
    }


def test_reduced_artifact_persistence_reuses_an_exact_complete_trio(app):
    record, plan = _durable_plan_record()
    values = _artifact_inputs(record, plan)
    first = persist_reduced_analysis_artifacts(**values)
    db.session.commit()
    second = persist_reduced_analysis_artifacts(**values)
    assert {kind: row.id for kind, row in second.items()} == {
        kind: row.id for kind, row in first.items()
    }
    assert TranslationContextArtifactRevision.query.count() == 3
    assert TranslationGlossaryConcept.query.count() == 1
    assert TranslationGlossaryRevision.query.count() == 1
    assert TranslationGlossaryActive.query.count() == 0
    proposal = TranslationGlossaryRevision.query.one()
    assert proposal.author_type == "model"
    assert proposal.revision_state == "proposal"
    assert proposal.payload_json == {
        "source_term": "signal",
        "target_term": "sygnał",
        "notes": "A meaningful indication.",
        "scope": "document",
    }


def test_reduced_artifact_persistence_rejects_partial_lineage_without_appending(app):
    record, plan = _durable_plan_record()
    values = _artifact_inputs(record, plan)
    persisted = persist_reduced_analysis_artifacts(**values)
    db.session.delete(persisted["style_guide"])
    db.session.commit()
    with pytest.raises(BlockContractError, match="analysis-artifact-reuse-partial"):
        persist_reduced_analysis_artifacts(**values)
    assert TranslationContextArtifactRevision.query.count() == 2


def test_reduced_artifact_persistence_rejects_lineage_drift_without_duplicate(app):
    record, plan = _durable_plan_record()
    values = _artifact_inputs(record, plan)
    persisted = persist_reduced_analysis_artifacts(**values)
    revision = persisted["document_brief"]
    evidence = dict(revision.evidence_json)
    evidence["glossary_proposals"] = []
    revision.evidence_json = evidence
    revision.evidence_hash = private_witness(
        evidence, purpose="translator-artifact-evidence-v1"
    )
    revision.record_hash = _artifact_hash(revision)
    db.session.commit()
    with pytest.raises(BlockContractError, match="analysis-artifact-reuse-drift"):
        persist_reduced_analysis_artifacts(**values)
    assert TranslationContextArtifactRevision.query.count() == 3


def test_map_and_reduce_preparation_forward_only_exact_terminal_retry_lineage(app):
    record, plan = _durable_plan_record()
    phase = _phase(native=False)
    settings = {"quality": "high"}
    budget = {"output_tokens": 100}
    scope = build_analysis_scopes(plan, max_input_bytes=2_000, context_bytes=0)[0]

    map_operation = _running_operation(record, plan, key="map-retry")
    map_parent = prepare_block_analysis_attempt(
        operation=map_operation,
        source_plan=record,
        scope=scope,
        phase=phase,
        settings=settings,
        budget=budget,
    )
    map_operation.version += 1
    db.session.commit()
    terminalize_stale_prepared_analysis_attempt(map_parent)
    db.session.expire_all()
    current_map_operation = db.session.get(TranslationOperation, map_operation.id)
    assert current_map_operation is not None
    map_child = prepare_block_analysis_attempt(
        operation=current_map_operation,
        source_plan=record,
        scope=scope,
        phase=phase,
        settings=settings,
        budget=budget,
        retry_of_attempt_id=map_parent.id,
    )
    assert map_child.retry_of_attempt_id == map_parent.id

    changed_phase = _phase(native=False)
    changed_phase["model"] = {"model_id": "saved/other"}
    with pytest.raises(BlockContractError, match="analysis-retry-packet"):
        prepare_block_analysis_attempt(
            operation=current_map_operation,
            source_plan=record,
            scope=scope,
            phase=changed_phase,
            settings=settings,
            budget=budget,
            retry_of_attempt_id=map_parent.id,
        )

    reduce_operation = db.session.get(TranslationOperation, map_operation.id)
    assert reduce_operation is not None
    reduce_parent = prepare_reduce_analysis_attempt(
        operation=reduce_operation,
        source_plan=record,
        map_results=[_map(scope)],
        phase=phase,
        settings=settings,
        budget=budget,
    )
    reduce_operation.version += 1
    db.session.commit()
    terminalize_stale_prepared_analysis_attempt(reduce_parent)
    db.session.expire_all()
    current_reduce_operation = db.session.get(TranslationOperation, reduce_operation.id)
    assert current_reduce_operation is not None
    reduce_child = prepare_reduce_analysis_attempt(
        operation=current_reduce_operation,
        source_plan=record,
        map_results=[_map(scope)],
        phase=phase,
        settings=settings,
        budget=budget,
        retry_of_attempt_id=reduce_parent.id,
    )
    assert reduce_child.retry_of_attempt_id == reduce_parent.id
