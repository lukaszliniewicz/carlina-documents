"""Focused persistence tests; every provider/tool call is injected or absent."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from content_storage_support import bind_content_policy
from flask import Flask
from sqlalchemy import event, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from carlina.core.semantic_html import SEMANTIC_HTML_CONTRACT_VERSION
from carlina.core.utc import utc_now_db
from iarf_assistant.addons.translator import (
    analysis_attempts,
    context_baselines,
    research_persistence,
)
from iarf_assistant.addons.translator.analysis_attempts import (
    rehydrate_analysis_attempt,
    rehydrate_completed_analysis_output,
)
from iarf_assistant.addons.translator.analysis_execution import execute_analysis_attempt
from iarf_assistant.addons.translator.block_contract import (
    AttestedDocumentSnapshot,
    BlockContractError,
)
from iarf_assistant.addons.translator.block_persistence import (
    persist_source_plan,
    rehydrate_source_plan,
    source_attestation_for_project,
)
from iarf_assistant.addons.translator.block_planner import plan_document_blocks
from iarf_assistant.addons.translator.models import (
    TranslationAnalysisAttempt,
    TranslationOperation,
    TranslationProject,
    TranslationResearchToolCall,
    TranslationResearchToolCallEvent,
    TranslationResearchToolResultBlob,
    TranslationResearchTurn,
)
from iarf_assistant.addons.translator.operations import (
    request_cancellation,
    transition_operation,
)
from iarf_assistant.addons.translator.research_execution import (
    execute_research_tool_call,
)
from iarf_assistant.addons.translator.research_persistence import (
    cancel_research_tool_call,
    capture_research_operation_fence,
    claim_research_tool_call,
    complete_research_tool_call,
    persist_research_run,
    prepare_research_tool_calls,
    rebase_research_operation_lease,
    reconcile_research_tool_call,
    rehydrate_research_run,
    rehydrate_research_tool_call,
    rehydrate_research_tool_result_blob,
    reserve_research_turn,
    transition_research_run,
)
from iarf_assistant.core.addon_manager import AddonManager
from iarf_assistant.core.addons.connections import ConnectionService
from iarf_assistant.core.encryption import EncryptionService, InstallationKeyStore
from iarf_assistant.core.execution_kernel import AgentEventSink
from iarf_assistant.extensions import db
from iarf_assistant.models import User


@contextmanager
def _measure_queries():
    metrics = SimpleNamespace(statement_count=0)

    def count_statement(*_args):
        metrics.statement_count += 1

    event.listen(db.engine, "after_cursor_execute", count_statement)
    try:
        yield metrics
    finally:
        event.remove(db.engine, "after_cursor_execute", count_statement)


@pytest.fixture
def app(tmp_path: Path):
    application = Flask(__name__, instance_path=str(tmp_path / "instance"))
    bind_content_policy(application)
    application.config.update(
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{tmp_path / 'research.db'}",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SECRET_KEY="research-test",
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
    user = User(email="research@example.test", password_hash="x")
    db.session.add(user)
    db.session.flush()
    project = TranslationProject(
        user_id=user.id,
        name="Research",
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
    html = "<html><body><p>Private research source.</p></body></html>"
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
        idempotency_key="research",
        request_fingerprint="a" * 64,
        state="running",
        queue_job_id="research-worker",
        source_content_hash=plan.snapshot.content_hash,
        source_plan_id=record.id,
        source_plan_hash=plan.plan_hash,
        lease_expires_at=utc_now_db() + timedelta(minutes=5),
    )
    db.session.add(operation)
    db.session.commit()
    return record, operation


def _reserve(run):
    return reserve_research_turn(
        run,
        packet={"messages": [{"role": "user", "content": "Research"}], "input": {}},
    )


def _declaration():
    return {
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


def _strict_declaration():
    declaration = _declaration()
    declaration["declaration"]["function"]["parameters"] = {
        "type": "object",
        "properties": {"q": {"type": "string"}},
        "required": ["q"],
        "additionalProperties": False,
    }
    return declaration


def _budget(*, model_calls: int = 1, tools: int = 3):
    return {
        "max_model_calls": model_calls,
        "max_tool_calls": tools,
        "output_tokens": 100,
    }


def _complete_attempt(
    attempt, *, call_id="provider-call-1", raw='{"q":"exact spacing"}'
):
    execute_analysis_attempt(
        attempt,
        completion_call=lambda _packet: {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": "search", "arguments": raw},
                }
            ],
        },
    )


def _continuation_packet(record, attempt, calls):
    restored = rehydrate_analysis_attempt(attempt, record)
    assistant = rehydrate_completed_analysis_output(attempt, record)
    messages = [*restored["messages"], assistant]
    for call in calls:
        tool = rehydrate_research_tool_call(call)
        messages.append(
            {
                "role": "tool",
                "tool_call_id": call.provider_call_id,
                "name": call.tool_name,
                "content": tool["tool_message"],
            }
        )
    return {"messages": messages, "input": restored["input"]}


def _settled_run(record, operation):
    run = persist_research_run(
        operation=operation,
        source_plan=record,
        model={"model_id": "test/research"},
        settings={"temperature": 0},
        profile={"profile": "quality"},
        budget=_budget(model_calls=2),
    )
    turn, attempt = _reserve(run)
    db.session.commit()
    execute_analysis_attempt(
        attempt,
        completion_call=lambda _packet: {
            "role": "assistant",
            "content": "No tools were needed.",
        },
    )
    db.session.commit()
    return run, turn, attempt


def _three_turn_tool_run(record, operation):
    run = persist_research_run(
        operation=operation,
        source_plan=record,
        model={"model_id": "test/research"},
        settings={"temperature": 0},
        profile={"profile": "quality"},
        budget=_budget(model_calls=3, tools=2),
        selected_provider_ids=["web-provider"],
        selected_addon_ids=["web-addon"],
        tool_declarations=[_declaration()],
    )
    prior_attempt = None
    prior_calls = []
    for turn_number in (1, 2, 3):
        packet = (
            {"messages": [{"role": "user", "content": "Research"}], "input": {}}
            if prior_attempt is None
            else _continuation_packet(record, prior_attempt, prior_calls)
        )
        turn, attempt = reserve_research_turn(run, packet=packet)
        db.session.commit()
        if turn_number == 3:
            execute_analysis_attempt(
                attempt,
                completion_call=lambda _packet: {
                    "role": "assistant",
                    "content": "Concluded.",
                },
            )
            prior_calls = []
        else:
            _complete_attempt(
                attempt,
                call_id=f"provider-call-{turn_number}",
                raw=json.dumps({"q": f"question {turn_number}"}),
            )
            prior_calls = list(prepare_research_tool_calls(run=run, turn=turn))
            for call in prior_calls:
                claim_research_tool_call(call)
                complete_research_tool_call(
                    call,
                    {"items": [f"answer {turn_number}"]},
                )
            db.session.commit()
        prior_attempt = attempt
    return run


def test_rebase_renews_only_after_settled_turn_and_next_attempt_uses_new_fence(app):
    record, operation = _state()
    run, turn, attempt = _settled_run(record, operation)

    original = capture_research_operation_fence(run)
    renewed = rebase_research_operation_lease(run, expected_fence=original)
    db.session.commit()

    assert renewed["operation_version"] == original["operation_version"] + 1
    db.session.refresh(operation)
    assert operation.version == renewed["operation_version"]
    next_turn, next_attempt = reserve_research_turn(
        run,
        packet=_continuation_packet(record, attempt, []),
    )
    assert next_turn.sequence == turn.sequence + 1
    assert next_attempt.operation_version == renewed["operation_version"]
    assert next_attempt.rebase_of_attempt_id == attempt.id


def test_rebase_rejects_active_or_dispatched_child(app):
    record, operation = _state()
    run = persist_research_run(
        operation=operation,
        source_plan=record,
        model={"model_id": "test/research"},
        settings={"temperature": 0},
        profile={"profile": "quality"},
        budget=_budget(model_calls=2),
    )
    _reserve(run)
    db.session.commit()

    with pytest.raises(BlockContractError, match="research-rebase-active-attempt"):
        rebase_research_operation_lease(
            run,
            expected_fence=capture_research_operation_fence(run),
        )

    class _SyntheticWorkerCrash(BaseException):
        pass

    def crash_after_durable_dispatch(_packet):
        raise _SyntheticWorkerCrash()

    # ``execute_analysis_attempt`` intentionally lets non-Exception failures
    # escape after the dispatch claim, modelling process loss without writing
    # a fictional provider outcome.
    with pytest.raises(_SyntheticWorkerCrash):
        execute_analysis_attempt(
            TranslationAnalysisAttempt.query.one(),
            completion_call=crash_after_durable_dispatch,
        )
    with pytest.raises(BlockContractError, match="research-rebase-active-attempt"):
        rebase_research_operation_lease(
            run,
            expected_fence=capture_research_operation_fence(run),
        )


def test_rebase_rejects_stale_or_forged_operation_fence(app):
    record, operation = _state()
    run, _turn, _attempt = _settled_run(record, operation)
    original = capture_research_operation_fence(run)
    forged = dict(original)
    forged["lease_fence"] = "0" * 64
    with pytest.raises(BlockContractError, match="research-rebase-fence"):
        rebase_research_operation_lease(run, expected_fence=forged)

    transition_operation(operation, "running", expected_version=operation.version)
    db.session.commit()
    with pytest.raises(BlockContractError, match="research-rebase-fence"):
        rebase_research_operation_lease(run, expected_fence=original)


def test_research_evidence_is_encrypted_and_has_ordered_lifecycle(app):
    record, operation = _state()
    run = persist_research_run(
        operation=operation,
        source_plan=record,
        model={"model_id": "test/research"},
        settings={"temperature": 0},
        budget=_budget(),
        profile={"profile": "quality"},
        selected_provider_ids=["web-provider"],
        selected_addon_ids=["web-addon"],
        tool_declarations=[_declaration()],
    )
    turn, attempt = _reserve(run)
    db.session.commit()
    _complete_attempt(attempt)
    (call,) = prepare_research_tool_calls(
        run=run,
        turn=turn,
    )
    db.session.commit()
    raw = db.session.execute(
        text(
            "SELECT r.model_json, c.raw_arguments_json "
            "FROM translation_research_run AS r "
            "JOIN translation_research_tool_call AS c ON c.run_id = r.id"
        )
    ).one()
    assert "exact spacing" not in str(raw)
    assert (
        hashlib.sha256(b'{"q":"exact spacing"}').hexdigest() != call.raw_arguments_hash
    )
    assert claim_research_tool_call(call).state == "executing"
    complete_research_tool_call(call, {"items": ["one"]})
    db.session.commit()
    app.extensions["carlina_encryption"].key_store.rotate()
    restored_call = rehydrate_research_tool_call(call)
    assert restored_call["raw_arguments"] == '{"q":"exact spacing"}'
    assert restored_call["arguments"] == {"q": "exact spacing"}
    assert restored_call["result"] == {"items": ["one"]}
    assert [event.event_type for event in restored_call["events"]] == [
        "prepared",
        "executing",
        "succeeded",
    ]
    restored_run = rehydrate_research_run(run)
    assert restored_run["turns"][0].analysis_attempt_id == attempt.id


def test_active_research_rehydration_has_a_bounded_query_budget(app):
    record, operation = _state()
    run = _three_turn_tool_run(record, operation)
    verified_plan = rehydrate_source_plan(record)
    verification_context = research_persistence.ResearchVerificationContext()
    db.session.expire_all()

    with _measure_queries() as metrics:
        restored = rehydrate_research_run(
            run,
            verified_plan=verified_plan,
            verification_context=verification_context,
        )

    db.session.expire_all()
    with _measure_queries() as repeated_metrics:
        repeated = rehydrate_research_run(
            run,
            verified_plan=verified_plan,
            verification_context=verification_context,
        )

    assert len(restored["turns"]) == 3
    assert len(restored["tool_calls"]) == 2
    assert [turn.id for turn in repeated["turns"]] == [
        turn.id for turn in restored["turns"]
    ]
    assert metrics.statement_count <= 32
    assert repeated_metrics.statement_count <= 12

    changed_attempt = db.session.get(
        TranslationAnalysisAttempt,
        repeated["turns"][-1].analysis_attempt_id,
    )
    assert changed_attempt is not None
    changed_attempt.dispatch_receipt_hash = "0" * 64
    db.session.commit()
    with pytest.raises(BlockContractError, match="analysis-dispatch-receipt"):
        rehydrate_research_run(
            run,
            verified_plan=verified_plan,
            verification_context=verification_context,
        )


def test_research_call_replay_is_rejected(app):
    record, operation = _state()
    run = persist_research_run(
        operation=operation,
        source_plan=record,
        model={"model_id": "test/research"},
        settings={"temperature": 0},
        budget=_budget(),
        profile={"profile": "quality"},
        selected_provider_ids=["web-provider"],
        selected_addon_ids=["web-addon"],
        tool_declarations=[_declaration()],
    )
    turn, attempt = _reserve(run)
    db.session.commit()
    _complete_attempt(attempt, call_id="provider-call-2", raw="{}")
    (call,) = prepare_research_tool_calls(
        run=run,
        turn=turn,
    )
    claim_research_tool_call(call)
    with pytest.raises(BlockContractError, match="not-prepared"):
        claim_research_tool_call(call)


def test_research_tool_reservation_replays_complete_transaction_after_lock(
    app,
    monkeypatch,
):
    record, operation = _state()
    run = persist_research_run(
        operation=operation,
        source_plan=record,
        model={"model_id": "test/research"},
        settings={"temperature": 0},
        budget=_budget(),
        profile={"profile": "quality"},
        selected_provider_ids=["web-provider"],
        selected_addon_ids=["web-addon"],
        tool_declarations=[_declaration()],
    )
    turn, attempt = _reserve(run)
    db.session.commit()
    _complete_attempt(attempt, call_id="provider-call-lock-retry", raw="{}")

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

    monkeypatch.setattr(active, "flush", flush_with_one_transient_lock)

    (call,) = prepare_research_tool_calls(run=run, turn=turn, session=active)

    assert reservation_flushes == 2
    assert call.state == "prepared"
    assert (
        active.query(TranslationResearchToolCall).filter_by(run_id=run.id).count() == 1
    )
    assert (
        active.query(TranslationResearchToolCallEvent)
        .filter_by(tool_call_id=call.id)
        .count()
        == 1
    )


def test_uncertain_reconciliation_requires_fence_loss(app):
    record, operation = _state()
    run = persist_research_run(
        operation=operation,
        source_plan=record,
        model={"model_id": "test/research"},
        settings={"temperature": 0},
        budget=_budget(),
        profile={"profile": "quality"},
        selected_provider_ids=["web-provider"],
        selected_addon_ids=["web-addon"],
        tool_declarations=[_declaration()],
    )
    turn, attempt = _reserve(run)
    db.session.commit()
    _complete_attempt(attempt, call_id="provider-call-3", raw="{}")
    (call,) = prepare_research_tool_calls(
        run=run,
        turn=turn,
    )
    claim_research_tool_call(call)
    with pytest.raises(BlockContractError, match="fence-not-lost"):
        reconcile_research_tool_call(call)
    operation.state = "failed"
    operation.version += 1
    db.session.commit()
    reconciled = reconcile_research_tool_call(call, evidence={"lease": "lost"})
    assert reconciled.state == "uncertain"
    assert (
        rehydrate_research_tool_call(call)["error"]["reason"] == "operation_fence_lost"
    )


def test_prepared_call_renews_before_claim_but_claimed_old_fence_cannot_finish(app):
    record, operation = _state()
    run = persist_research_run(
        operation=operation,
        source_plan=record,
        model={"model_id": "test/research"},
        settings={"temperature": 0},
        profile={"profile": "quality"},
        budget=_budget(),
        selected_provider_ids=["web-provider"],
        selected_addon_ids=["web-addon"],
        tool_declarations=[_declaration()],
    )
    turn, attempt = _reserve(run)
    db.session.commit()
    _complete_attempt(attempt, call_id="provider-call-renew", raw="{}")
    (call,) = prepare_research_tool_calls(
        run=run,
        turn=turn,
    )
    db.session.commit()
    operation.version += 1
    operation.queue_job_id = "research-worker-renewed"
    operation.lease_expires_at = utc_now_db() + timedelta(minutes=5)
    db.session.commit()
    claimed = claim_research_tool_call(call)
    assert claimed.state == "executing"
    assert claimed.operation_version == operation.version
    operation.version += 1
    operation.queue_job_id = "research-worker-renewed-again"
    operation.lease_expires_at = utc_now_db() + timedelta(minutes=5)
    db.session.commit()
    with pytest.raises(BlockContractError, match="call-record"):
        complete_research_tool_call(claimed, {"items": ["never-dispatched-again"]})
    assert rehydrate_research_tool_call(claimed)["events"][-1].event_type == "executing"


def test_pre_dispatch_block_and_cancel_are_rehydratable(app):
    record, operation = _state()
    run = persist_research_run(
        operation=operation,
        source_plan=record,
        model={"model_id": "test/research"},
        settings={"temperature": 0},
        budget=_budget(model_calls=2, tools=2),
        profile={"profile": "quality"},
        selected_provider_ids=["web-provider"],
        selected_addon_ids=["web-addon"],
        tool_declarations=[_declaration()],
    )
    first_turn, first_attempt = _reserve(run)
    db.session.commit()
    _complete_attempt(first_attempt, call_id="invalid-raw", raw="{")
    (blocked,) = prepare_research_tool_calls(
        run=run,
        turn=first_turn,
    )
    assert blocked.argument_state == "invalid"
    assert blocked.state == "blocked"
    blocked_data = rehydrate_research_tool_call(blocked)
    assert [event.event_type for event in blocked_data["events"]] == [
        "prepared",
        "blocked",
    ]
    assert blocked.claimed_at is None

    second_turn, second_attempt = reserve_research_turn(
        run,
        packet=_continuation_packet(record, first_attempt, [blocked]),
    )
    assert second_turn.sequence == 2
    assert second_attempt.rebase_of_attempt_id == first_attempt.id
    db.session.rollback()

    # A separate run exercises cancellation before dispatch; it has no claim
    # fence and remains valid terminal evidence.
    run = persist_research_run(
        operation=operation,
        source_plan=record,
        model={"model_id": "test/research"},
        settings={"temperature": 0},
        budget=_budget(),
        profile={"profile": "quality"},
        selected_provider_ids=["web-provider"],
        selected_addon_ids=["web-addon"],
        tool_declarations=[_declaration()],
    )
    turn, attempt = _reserve(run)
    db.session.commit()
    _complete_attempt(attempt, call_id="cancel-before-io", raw="{}")
    (cancelled,) = prepare_research_tool_calls(
        run=run,
        turn=turn,
    )
    cancelled = cancel_research_tool_call(cancelled)
    restored = rehydrate_research_tool_call(cancelled)
    assert [event.event_type for event in restored["events"]] == [
        "prepared",
        "cancelled",
    ]
    assert transition_research_run(run, "cancelled").state == "cancelled"


def test_uncertain_child_allows_only_uncertain_run_terminal(app):
    record, operation = _state()
    run = persist_research_run(
        operation=operation,
        source_plan=record,
        model={"model_id": "test/research"},
        settings={"temperature": 0},
        budget=_budget(),
        profile={"profile": "quality"},
        selected_provider_ids=["web-provider"],
        selected_addon_ids=["web-addon"],
        tool_declarations=[_declaration()],
    )
    turn, attempt = _reserve(run)
    db.session.commit()
    _complete_attempt(attempt, call_id="uncertain-call", raw="{}")
    (call,) = prepare_research_tool_calls(
        run=run,
        turn=turn,
    )
    claim_research_tool_call(call)
    operation.version += 1
    operation.state = "failed"
    db.session.commit()
    reconcile_research_tool_call(call)
    with pytest.raises(BlockContractError, match="uncertain-children"):
        transition_research_run(run, "failed")
    assert transition_research_run(run, "uncertain").state == "uncertain"
    assert rehydrate_research_run(run)["tool_calls"][0].state == "uncertain"


def test_terminal_run_rejects_active_attempt_and_final_tool_request(app):
    record, operation = _state()
    run = persist_research_run(
        operation=operation,
        source_plan=record,
        model={"model_id": "test/research"},
        settings={"temperature": 0},
        budget=_budget(),
        profile={"profile": "quality"},
        selected_provider_ids=["web-provider"],
        selected_addon_ids=["web-addon"],
        tool_declarations=[_declaration()],
    )
    _turn, attempt = _reserve(run)
    db.session.commit()
    with pytest.raises(BlockContractError, match="active-attempts"):
        transition_research_run(run, "completed")
    execute_analysis_attempt(
        attempt,
        completion_call=lambda _packet: {"role": "assistant", "content": "Done."},
    )
    assert transition_research_run(run, "completed").state == "completed"


def test_completed_tool_request_is_recoverable_before_atomic_reservation(app):
    record, operation = _state()
    run = persist_research_run(
        operation=operation,
        source_plan=record,
        model={"model_id": "test/research"},
        settings={"temperature": 0},
        budget=_budget(),
        profile={"profile": "quality"},
        selected_provider_ids=["web-provider"],
        selected_addon_ids=["web-addon"],
        tool_declarations=[_declaration()],
    )
    _turn, attempt = _reserve(run)
    db.session.commit()
    _complete_attempt(attempt, call_id="recoverable", raw="{}")
    restored = rehydrate_research_run(run)
    assert restored["tool_calls"] == ()
    with pytest.raises(BlockContractError, match="unreserved-tool-calls"):
        transition_research_run(run, "failed")


def test_tool_call_batch_is_all_or_nothing_and_ordered(app, monkeypatch):
    record, operation = _state()
    run = persist_research_run(
        operation=operation,
        source_plan=record,
        model={"model_id": "test/research"},
        settings={"temperature": 0},
        budget=_budget(tools=2),
        profile={"profile": "quality"},
        selected_provider_ids=["web-provider"],
        selected_addon_ids=["web-addon"],
        tool_declarations=[_declaration()],
    )
    turn, attempt = _reserve(run)
    db.session.commit()
    execute_analysis_attempt(
        attempt,
        completion_call=lambda _packet: {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "batch-1",
                    "type": "function",
                    "function": {"name": "search", "arguments": "{}"},
                },
                {
                    "id": "batch-2",
                    "type": "function",
                    "function": {"name": "search", "arguments": "{}"},
                },
            ],
        },
    )
    original = research_persistence._prepare_research_tool_call
    call_count = 0

    def fail_second(**kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise BlockContractError("injected-second-call-failure")
        return original(**kwargs)

    monkeypatch.setattr(
        research_persistence, "_prepare_research_tool_call", fail_second
    )
    with pytest.raises(BlockContractError, match="injected-second-call-failure"):
        prepare_research_tool_calls(
            run=run,
            turn=turn,
        )
    assert (
        db.session.query(TranslationResearchToolCall).filter_by(run_id=run.id).count()
        == 0
    )
    monkeypatch.setattr(research_persistence, "_prepare_research_tool_call", original)
    calls = prepare_research_tool_calls(run=run, turn=turn)
    assert [call.turn_call_index for call in calls] == [1, 2]
    assert [call.provider_call_id for call in calls] == ["batch-1", "batch-2"]


@pytest.mark.parametrize(
    ("raw", "validator", "path"),
    [
        ("{}", "required", []),
        ('{"q":2}', "type", ["q"]),
        ('{"q":"ok","extra":true}', "additionalProperties", []),
    ],
)
def test_schema_invalid_arguments_are_durably_blocked_before_io(
    app, raw, validator, path
):
    record, operation = _state()
    run = persist_research_run(
        operation=operation,
        source_plan=record,
        model={"model_id": "test/research"},
        settings={"temperature": 0},
        budget=_budget(),
        profile={"profile": "quality"},
        selected_provider_ids=["web-provider"],
        selected_addon_ids=["web-addon"],
        tool_declarations=[_strict_declaration()],
    )
    turn, attempt = _reserve(run)
    db.session.commit()
    _complete_attempt(attempt, call_id=f"schema-{validator}", raw=raw)
    (call,) = prepare_research_tool_calls(run=run, turn=turn)
    assert call.state == "blocked"
    assert call.argument_state == "invalid"
    assert call.claimed_at is None
    restored = rehydrate_research_tool_call(call)
    assert restored["argument_error"] == {
        "reason": "research-arguments-schema",
        "path": path,
        "validator": validator,
    }
    assert restored["error"] == restored["argument_error"]
    with pytest.raises(BlockContractError, match="not-prepared"):
        claim_research_tool_call(call)


def test_external_tool_schema_reference_is_rejected(app):
    record, operation = _state()
    declaration = _declaration()
    declaration["declaration"]["function"]["parameters"] = {
        "type": "object",
        "properties": {"q": {"$ref": "https://example.test/schema.json"}},
    }
    with pytest.raises(BlockContractError, match="declaration-schema"):
        persist_research_run(
            operation=operation,
            source_plan=record,
            model={"model_id": "test/research"},
            settings={"temperature": 0},
            budget=_budget(),
            profile={"profile": "quality"},
            selected_provider_ids=["web-provider"],
            selected_addon_ids=["web-addon"],
            tool_declarations=[declaration],
        )


@pytest.mark.parametrize(
    ("connection_mode", "connection_id"),
    [
        ("saved", ""),
        ("connectionless", "web-connection"),
        ("implicit-default", "web-connection"),
    ],
)
def test_research_tool_connection_identity_must_be_explicit(
    app, connection_mode, connection_id
):
    record, operation = _state()
    declaration = _declaration()
    declaration["connection_mode"] = connection_mode
    declaration["connection_id"] = connection_id

    with pytest.raises(BlockContractError, match="declaration-identity"):
        persist_research_run(
            operation=operation,
            source_plan=record,
            model={"model_id": "test/research"},
            settings={"temperature": 0},
            budget=_budget(),
            profile={"profile": "quality"},
            selected_provider_ids=["web-provider"],
            selected_addon_ids=["web-addon"],
            tool_declarations=[declaration],
        )


def test_research_tool_can_explicitly_freeze_connectionless_provider(app):
    record, operation = _state()
    declaration = _declaration()
    declaration["connection_mode"] = "connectionless"
    declaration["connection_id"] = ""

    run = persist_research_run(
        operation=operation,
        source_plan=record,
        model={"model_id": "test/research"},
        settings={"temperature": 0},
        budget=_budget(),
        profile={"profile": "quality"},
        selected_provider_ids=["web-provider"],
        selected_addon_ids=["web-addon"],
        tool_declarations=[declaration],
    )

    assert (
        rehydrate_research_run(run)["tool_declarations"][0]["connection_mode"]
        == "connectionless"
    )


def test_oversized_tool_batch_is_blocked_and_can_continue(app):
    record, operation = _state()
    run = persist_research_run(
        operation=operation,
        source_plan=record,
        model={"model_id": "test/research"},
        settings={"temperature": 0},
        budget=_budget(model_calls=2, tools=1),
        profile={"profile": "quality"},
        selected_provider_ids=["web-provider"],
        selected_addon_ids=["web-addon"],
        tool_declarations=[_declaration()],
    )
    first_turn, first_attempt = _reserve(run)
    db.session.commit()
    execute_analysis_attempt(
        first_attempt,
        completion_call=lambda _packet: {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "over-budget-1",
                    "type": "function",
                    "function": {"name": "search", "arguments": "{}"},
                },
                {
                    "id": "over-budget-2",
                    "type": "function",
                    "function": {"name": "search", "arguments": "{}"},
                },
            ],
        },
    )
    calls = prepare_research_tool_calls(run=run, turn=first_turn)
    assert [call.state for call in calls] == ["blocked", "blocked"]
    assert all(call.claimed_at is None for call in calls)
    assert [rehydrate_research_tool_call(call)["error"] for call in calls] == [
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
    second_turn, second_attempt = reserve_research_turn(
        run,
        packet=_continuation_packet(record, first_attempt, calls),
    )
    assert second_turn.sequence == 2
    db.session.commit()
    execute_analysis_attempt(
        second_attempt,
        completion_call=lambda _packet: {"role": "assistant", "content": "Done."},
    )
    assert transition_research_run(run, "completed").state == "completed"


def test_prepared_event_payload_must_be_exact_even_with_valid_receipts(app):
    record, operation = _state()
    run = persist_research_run(
        operation=operation,
        source_plan=record,
        model={"model_id": "test/research"},
        settings={"temperature": 0},
        budget=_budget(),
        profile={"profile": "quality"},
        selected_provider_ids=["web-provider"],
        selected_addon_ids=["web-addon"],
        tool_declarations=[_declaration()],
    )
    turn, attempt = _reserve(run)
    db.session.commit()
    _complete_attempt(attempt, call_id="event-extra", raw="{}")
    (call,) = prepare_research_tool_calls(run=run, turn=turn)
    event = rehydrate_research_tool_call(call)["events"][0]
    event.payload_json = {
        "state": "prepared",
        "call_record_hash": call.call_record_hash,
        "extra": True,
    }
    event.payload_hash = research_persistence._witness(
        event.payload_json, "event_payload"
    )
    event.event_hash = research_persistence._event_hash(event)
    research_persistence._set_call_state_head(call, event, db.session)
    research_persistence._refresh_run_state_head(run, db.session)
    db.session.commit()
    with pytest.raises(BlockContractError, match="event-payload-shape"):
        rehydrate_research_tool_call(call)


def test_executing_event_version_must_be_exact_even_with_valid_receipts(app):
    record, operation = _state()
    run = persist_research_run(
        operation=operation,
        source_plan=record,
        model={"model_id": "test/research"},
        settings={"temperature": 0},
        budget=_budget(),
        profile={"profile": "quality"},
        selected_provider_ids=["web-provider"],
        selected_addon_ids=["web-addon"],
        tool_declarations=[_declaration()],
    )
    turn, attempt = _reserve(run)
    db.session.commit()
    _complete_attempt(attempt, call_id="event-version", raw="{}")
    (call,) = prepare_research_tool_calls(run=run, turn=turn)
    call = claim_research_tool_call(call)
    event = rehydrate_research_tool_call(call)["events"][1]
    event.payload_json = {"state": "executing", "state_version": 99}
    event.payload_hash = research_persistence._witness(
        event.payload_json, "event_payload"
    )
    event.event_hash = research_persistence._event_hash(event)
    research_persistence._set_call_state_head(call, event, db.session)
    research_persistence._refresh_run_state_head(run, db.session)
    db.session.commit()
    with pytest.raises(BlockContractError, match="event-payload-shape"):
        rehydrate_research_tool_call(call)


def test_turn_reservation_rolls_back_attempt_and_link_together(app):
    record, operation = _state()
    run = persist_research_run(
        operation=operation,
        source_plan=record,
        model={"model_id": "test/research"},
        settings={"temperature": 0},
        budget=_budget(),
        profile={"profile": "quality"},
        selected_provider_ids=["web-provider"],
        selected_addon_ids=["web-addon"],
        tool_declarations=[_declaration()],
    )
    db.session.commit()
    turn, attempt = _reserve(run)
    turn_id = turn.id
    attempt_id = attempt.id
    assert rehydrate_analysis_attempt(attempt, record)["budget"] == {
        "output_tokens": 100
    }
    db.session.rollback()
    assert db.session.get(TranslationResearchTurn, turn_id) is None
    assert db.session.get(TranslationAnalysisAttempt, attempt_id) is None
    assert rehydrate_research_run(run)["turns"] == ()


def test_provider_failure_retry_is_exact_and_durably_linked(app):
    record, operation = _state()
    run = persist_research_run(
        operation=operation,
        source_plan=record,
        model={"model_id": "test/research"},
        settings={"temperature": 0},
        budget=_budget(model_calls=2),
        profile={"profile": "quality"},
        selected_provider_ids=["web-provider"],
        selected_addon_ids=["web-addon"],
        tool_declarations=[_declaration()],
    )
    _turn, first = _reserve(run)
    db.session.commit()

    def fail(_packet):
        raise RuntimeError("provider unavailable")

    with pytest.raises(RuntimeError, match="provider unavailable"):
        execute_analysis_attempt(first, completion_call=fail)
    second_turn, second = _reserve(run)
    assert second_turn.sequence == 2
    assert second.retry_of_attempt_id == first.id
    assert rehydrate_research_run(run)["turns"][-1].analysis_attempt_id == second.id


def test_verified_plan_avoids_replanning_across_research_graph_and_tool_call(
    app, monkeypatch
):
    record, operation = _state()
    run = persist_research_run(
        operation=operation,
        source_plan=record,
        model={"model_id": "test/research"},
        settings={"temperature": 0},
        budget=_budget(model_calls=2),
        profile={"profile": "quality"},
        selected_provider_ids=["web-provider"],
        selected_addon_ids=["web-addon"],
        tool_declarations=[_strict_declaration()],
    )
    turn, attempt = _reserve(run)
    db.session.commit()
    _complete_attempt(attempt)
    verified_plan = rehydrate_source_plan(record)

    def reject_replanning(_record):
        raise AssertionError("the verified immutable plan must be reused")

    monkeypatch.setattr(
        research_persistence, "rehydrate_source_plan", reject_replanning
    )
    monkeypatch.setattr(analysis_attempts, "rehydrate_source_plan", reject_replanning)

    graph = rehydrate_research_run(run, verified_plan=verified_plan)
    assert [turn.analysis_attempt_id for turn in graph["turns"]] == [attempt.id]
    (call,) = prepare_research_tool_calls(
        run=run,
        turn=turn,
        verified_plan=verified_plan,
    )
    terminal = execute_research_tool_call(
        call,
        addon_manager=_ResearchToolManager(
            lambda _name, arguments: {"items": [arguments["q"]]}
        ),
        verified_plan=verified_plan,
    )
    assert terminal.state == "succeeded"

    restored_attempt = rehydrate_analysis_attempt(
        attempt,
        record,
        verified_plan=verified_plan,
    )
    assistant = rehydrate_completed_analysis_output(
        attempt,
        record,
        verified_plan=verified_plan,
    )
    restored_call = rehydrate_research_tool_call(
        terminal,
        verified_plan=verified_plan,
    )
    second_turn, _second_attempt = reserve_research_turn(
        run,
        packet={
            "messages": [
                *restored_attempt["messages"],
                assistant,
                {
                    "role": "tool",
                    "tool_call_id": terminal.provider_call_id,
                    "name": terminal.tool_name,
                    "content": restored_call["tool_message"],
                },
            ],
            "input": {},
        },
        verified_plan=verified_plan,
    )
    assert second_turn.sequence == 2


def test_context_baseline_reuses_one_verified_plan_across_research_graph(
    app, monkeypatch
):
    record, operation = _state()
    run, _turn, _attempt = _settled_run(record, operation)
    transition_research_run(run, "completed")
    db.session.commit()

    def reject_replanning(_record):
        raise AssertionError("nested research verification must reuse the source plan")

    monkeypatch.setattr(
        research_persistence, "rehydrate_source_plan", reject_replanning
    )
    monkeypatch.setattr(analysis_attempts, "rehydrate_source_plan", reject_replanning)

    baseline = context_baselines.freeze_context_baseline(
        operation,
        record,
        completed_research_runs=[run],
    )
    db.session.commit()

    assert context_baselines.rehydrate_context_baseline(baseline, record) is baseline
    slots = context_baselines.context_slots_from_baseline(baseline, record)
    assert [slot.slot_type for slot in slots] == ["research_evidence"]


def test_dirty_research_uow_is_rejected_without_overwrite(app):
    record, operation = _state()
    run = persist_research_run(
        operation=operation,
        source_plan=record,
        model={"model_id": "test/research"},
        settings={"temperature": 0},
        budget=_budget(),
        profile={"profile": "quality"},
    )
    db.session.commit()
    run_id = run.id
    run.state_version = 77
    with pytest.raises(BlockContractError, match="session-dirty"):
        rehydrate_research_run(run_id)
    assert run.state_version == 77


@pytest.mark.parametrize(
    "raw",
    [
        '{"x":1e10000}',
        '{"x":' * 140 + "{}" + "}" * 140,
    ],
)
def test_pathological_json_is_durably_blocked_and_run_can_terminalize(app, raw):
    record, operation = _state()
    run = persist_research_run(
        operation=operation,
        source_plan=record,
        model={"model_id": "test/research"},
        settings={"temperature": 0},
        budget=_budget(),
        profile={"profile": "quality"},
        selected_provider_ids=["web-provider"],
        selected_addon_ids=["web-addon"],
        tool_declarations=[_declaration()],
    )
    turn, attempt = _reserve(run)
    db.session.commit()
    _complete_attempt(attempt, call_id="pathological-json", raw=raw)
    (call,) = prepare_research_tool_calls(run=run, turn=turn)
    assert call.argument_state == "invalid"
    assert call.state == "blocked"
    assert call.claimed_at is None
    assert transition_research_run(run, "failed").state == "failed"


def test_dangling_local_tool_schema_reference_is_rejected_at_freeze(app):
    record, operation = _state()
    declaration = _declaration()
    declaration["declaration"]["function"]["parameters"] = {
        "$ref": "#/$defs/missing",
        "$defs": {},
    }
    with pytest.raises(BlockContractError, match="declaration-schema"):
        persist_research_run(
            operation=operation,
            source_plan=record,
            model={"model_id": "test/research"},
            settings={"temperature": 0},
            budget=_budget(),
            profile={"profile": "quality"},
            selected_provider_ids=["web-provider"],
            selected_addon_ids=["web-addon"],
            tool_declarations=[declaration],
        )


def test_historical_rehydration_survives_terminal_operation_lease_clear(app):
    record, operation = _state()
    run = persist_research_run(
        operation=operation,
        source_plan=record,
        model={"model_id": "test/research"},
        settings={"temperature": 0},
        budget=_budget(),
        profile={"profile": "quality"},
    )
    _turn, attempt = _reserve(run)
    db.session.commit()
    execute_analysis_attempt(
        attempt,
        completion_call=lambda _packet: {"role": "assistant", "content": "Done."},
    )
    transition_research_run(run, "completed")
    transition_operation(operation, "completed")
    db.session.commit()
    assert operation.lease_expires_at is None
    assert rehydrate_research_run(run)["turns"][0].analysis_attempt_id == attempt.id


def test_executing_call_reconciles_after_terminal_operation_clears_lease(app):
    record, operation = _state()
    run = persist_research_run(
        operation=operation,
        source_plan=record,
        model={"model_id": "test/research"},
        settings={"temperature": 0},
        budget=_budget(),
        profile={"profile": "quality"},
        selected_provider_ids=["web-provider"],
        selected_addon_ids=["web-addon"],
        tool_declarations=[_declaration()],
    )
    turn, attempt = _reserve(run)
    db.session.commit()
    _complete_attempt(attempt, call_id="terminal-fence", raw="{}")
    (call,) = prepare_research_tool_calls(run=run, turn=turn)
    call = claim_research_tool_call(call)
    transition_operation(operation, "failed")
    db.session.commit()
    assert operation.lease_expires_at is None
    assert reconcile_research_tool_call(call).state == "uncertain"
    assert rehydrate_research_tool_call(call)["events"][-1].event_type == "uncertain"


def test_cancellation_after_model_completion_creates_exact_non_io_children(app):
    record, operation = _state()
    run = persist_research_run(
        operation=operation,
        source_plan=record,
        model={"model_id": "test/research"},
        settings={"temperature": 0},
        budget=_budget(),
        profile={"profile": "quality"},
        selected_provider_ids=["web-provider"],
        selected_addon_ids=["web-addon"],
        tool_declarations=[_declaration()],
    )
    turn, attempt = _reserve(run)
    db.session.commit()
    _complete_attempt(attempt, call_id="cancel-window", raw="{}")
    request_cancellation(operation)
    db.session.commit()
    (call,) = prepare_research_tool_calls(run=run, turn=turn)
    assert call.state == "cancelled"
    assert call.claimed_at is None
    assert rehydrate_research_tool_call(call)["error"] == {
        "reason": "operation_not_runnable_before_dispatch",
        "operation_state": "cancel_requested",
    }
    assert transition_research_run(run, "cancelled").state == "cancelled"


def test_expiry_after_model_completion_creates_exact_non_io_children(app):
    record, operation = _state()
    run = persist_research_run(
        operation=operation,
        source_plan=record,
        model={"model_id": "test/research"},
        settings={"temperature": 0},
        budget=_budget(),
        profile={"profile": "quality"},
        selected_provider_ids=["web-provider"],
        selected_addon_ids=["web-addon"],
        tool_declarations=[_declaration()],
    )
    turn, attempt = _reserve(run)
    db.session.commit()
    _complete_attempt(attempt, call_id="expiry-window", raw="{}")
    operation.lease_expires_at = utc_now_db() - timedelta(seconds=1)
    db.session.commit()
    (call,) = prepare_research_tool_calls(run=run, turn=turn)
    assert call.state == "cancelled"
    assert call.claimed_at is None
    assert transition_research_run(run, "failed").state == "failed"


def test_pending_caller_work_is_rejected_without_autoflush(app):
    record, operation = _state()
    run = persist_research_run(
        operation=operation,
        source_plan=record,
        model={"model_id": "test/research"},
        settings={"temperature": 0},
        budget=_budget(),
        profile={"profile": "quality"},
    )
    db.session.commit()
    pending = User(email="pending@example.test", password_hash="x")
    db.session.add(pending)
    with pytest.raises(BlockContractError, match="session-dirty"):
        rehydrate_research_run(run)
    assert pending.id is None
    db.session.rollback()
    assert User.query.filter_by(email="pending@example.test").count() == 0


def test_terminal_run_rehydration_rejects_executing_child_with_valid_receipts(app):
    record, operation = _state()
    run = persist_research_run(
        operation=operation,
        source_plan=record,
        model={"model_id": "test/research"},
        settings={"temperature": 0},
        budget=_budget(),
        profile={"profile": "quality"},
        selected_provider_ids=["web-provider"],
        selected_addon_ids=["web-addon"],
        tool_declarations=[_declaration()],
    )
    turn, attempt = _reserve(run)
    db.session.commit()
    _complete_attempt(attempt, call_id="forged-terminal", raw="{}")
    (call,) = prepare_research_tool_calls(run=run, turn=turn)
    claim_research_tool_call(call)
    run.state = "failed"
    run.state_version += 1
    run.finished_at = utc_now_db()
    research_persistence._refresh_run_state_head(run, db.session)
    db.session.commit()
    with pytest.raises(BlockContractError, match="active-calls"):
        rehydrate_research_run(run)


def test_run_rehydration_rejects_noncontiguous_turns_with_valid_receipts(app):
    record, operation = _state()
    run = persist_research_run(
        operation=operation,
        source_plan=record,
        model={"model_id": "test/research"},
        settings={"temperature": 0},
        budget=_budget(),
        profile={"profile": "quality"},
    )
    turn, _attempt = _reserve(run)
    turn.sequence = 2
    turn.turn_record_hash = research_persistence._witness(
        research_persistence._turn_record_values(turn), "turn_record"
    )
    db.session.flush()
    research_persistence._refresh_run_state_head(run, db.session)
    db.session.commit()
    with pytest.raises(BlockContractError, match="turn-sequence"):
        rehydrate_research_run(run)


class _ResearchToolManager:
    def __init__(self, dispatch):
        declaration = _strict_declaration()["declaration"]
        self.record = SimpleNamespace(
            id="tool.search",
            addon_id="web-addon",
            legacy_name="search",
            function_schema=declaration,
            input_schema=declaration["function"]["parameters"],
            side_effects=["external_read"],
            approval_required=False,
            resource_requirements=[],
            status=SimpleNamespace(state="active"),
        )
        self.provider = SimpleNamespace(
            id="research.web-provider",
            provider_id="web-provider",
            domain="research",
            addon_id="web-addon",
            aliases=[],
            status=SimpleNamespace(state="active"),
        )
        self.loaded_addons = {"web-addon": object()}
        self.plugins = {}
        self._dispatch_enabled_addons = {"web-addon"}
        self._dispatch = dispatch
        self.dispatch_count = 0
        self.configs = []
        self.connection_state = "ready"
        self.connection_exists = True
        self.connection_owner_user_id = None
        self.connection_addon_id = "web-addon"

    def get_tool_record(self, name):
        if name in {"search", "tool.search"}:
            return self.record
        return None

    def validate_registered_tool_call(self, _name, arguments):
        return dict(arguments)

    def get_provider_record(self, domain, provider_id):
        if domain == "research" and provider_id in {
            self.provider.id,
            self.provider.provider_id,
        }:
            return self.provider
        return None

    def validate_visible_connection_binding(
        self, *, connection_id, owner_user_id, connector_addon_id
    ):
        if not self.connection_exists or connection_id != "web-connection":
            raise LookupError("unknown connection")
        if self.connection_owner_user_id not in (None, owner_user_id):
            raise PermissionError("foreign connection")
        if connector_addon_id != self.connection_addon_id:
            raise PermissionError("connector mismatch")
        if self.connection_state != "ready":
            raise RuntimeError("connection unavailable")
        return SimpleNamespace(id=connection_id, state="ready")

    def _execute_registered_tool_record_raw(
        self, _record, name, arguments, config, *, instance=None
    ):
        assert instance is self.loaded_addons["web-addon"]
        self.dispatch_count += 1
        self.configs.append(dict(config or {}))
        return self._dispatch(name, arguments)


def _prepared_execution_call(*, call_id="executor-call", raw='{"q":"evidence"}'):
    record, operation = _state()
    run = persist_research_run(
        operation=operation,
        source_plan=record,
        model={"model_id": "test/research"},
        settings={"temperature": 0},
        budget=_budget(),
        profile={"profile": "quality"},
        selected_provider_ids=["web-provider"],
        selected_addon_ids=["web-addon"],
        tool_declarations=[_strict_declaration()],
    )
    turn, attempt = _reserve(run)
    db.session.commit()
    _complete_attempt(attempt, call_id=call_id, raw=raw)
    (call,) = prepare_research_tool_calls(run=run, turn=turn)
    db.session.commit()
    return call, operation


def test_research_executor_commits_claim_before_one_shot_dispatch(app):
    call, _operation = _prepared_execution_call()
    states_seen_during_dispatch = []

    def dispatch(_name, arguments):
        checking_session = sessionmaker(bind=db.engine)()
        try:
            states_seen_during_dispatch.append(
                checking_session.get(TranslationResearchToolCall, call.id).state
            )
        finally:
            checking_session.close()
        return {"status": "ok", "items": [arguments["q"]]}

    events = []
    manager = _ResearchToolManager(dispatch)
    terminal = execute_research_tool_call(
        call,
        addon_manager=manager,
        event_sink=AgentEventSink(persist=events.append),
    )

    assert terminal.state == "succeeded"
    assert states_seen_during_dispatch == ["executing"]
    assert manager.dispatch_count == 1
    assert manager.configs == [
        {
            "current_user_id": call.owner_user_id,
            "connection_id": "web-connection",
        }
    ]
    restored = rehydrate_research_tool_call(call)
    assert restored["result"]["inline_result"] == {
        "status": "ok",
        "items": ["evidence"],
    }
    assert [event.event_type for event in restored["events"]] == [
        "prepared",
        "executing",
        "succeeded",
    ]
    assert "result_envelope" not in events[-1]["event"]["payload"]

    with pytest.raises(BlockContractError, match="not-prepared"):
        execute_research_tool_call(call, addon_manager=manager)
    assert manager.dispatch_count == 1


@pytest.mark.parametrize("drift", ["write", "identity", "schema"])
def test_research_executor_blocks_live_tool_drift_without_raw_dispatch(app, drift):
    call, _operation = _prepared_execution_call(call_id=f"drift-{drift}")
    manager = _ResearchToolManager(lambda *_args: {"status": "never"})
    if drift == "write":
        manager.record.side_effects = ["external_write"]
    elif drift == "identity":
        manager.record.id = "tool.replacement"
    else:
        manager.record.function_schema["function"]["description"] = "Changed"

    terminal = execute_research_tool_call(call, addon_manager=manager)

    assert terminal.state == "blocked"
    assert manager.dispatch_count == 0
    restored = rehydrate_research_tool_call(call)
    assert restored["error"]["reason"] == "tool_executor_blocked"
    assert restored["error"]["result_envelope"]["action"] == "blocked"


def test_research_executor_closes_registry_swap_at_final_dispatch(app):
    call, _operation = _prepared_execution_call(call_id="registry-swap")
    manager = _ResearchToolManager(lambda *_args: {"status": "never"})
    replacement = SimpleNamespace(**vars(manager.record))
    replacement.side_effects = ["external_write"]
    resolutions = 0

    def resolve(name):
        nonlocal resolutions
        resolutions += 1
        assert name in {"search", "tool.search"}
        return manager.record if resolutions < 3 else replacement

    manager.get_tool_record = resolve

    terminal = execute_research_tool_call(call, addon_manager=manager)

    assert terminal.state == "blocked"
    assert resolutions >= 3
    assert manager.dispatch_count == 0


def test_research_executor_revalidates_provider_at_final_dispatch(app):
    call, _operation = _prepared_execution_call(call_id="provider-swap")
    manager = _ResearchToolManager(lambda *_args: {"status": "never"})
    replacement = SimpleNamespace(**vars(manager.provider))
    replacement.status = SimpleNamespace(state="inactive")
    resolutions = 0

    def resolve(domain, provider_id):
        nonlocal resolutions
        resolutions += 1
        assert (domain, provider_id) == ("research", "web-provider")
        return manager.provider if resolutions == 1 else replacement

    manager.get_provider_record = resolve

    terminal = execute_research_tool_call(call, addon_manager=manager)

    assert terminal.state == "blocked"
    assert resolutions >= 2
    assert manager.dispatch_count == 0


def test_research_executor_does_not_claim_after_operation_fence_loss(app):
    call, operation = _prepared_execution_call(call_id="lost-before-claim")
    operation.state = "failed"
    operation.version += 1
    db.session.commit()
    manager = _ResearchToolManager(lambda *_args: {"status": "never"})

    with pytest.raises(BlockContractError):
        execute_research_tool_call(call, addon_manager=manager)

    assert manager.dispatch_count == 0
    assert rehydrate_research_tool_call(call)["events"][-1].event_type == "prepared"


def test_research_executor_witnesses_dispatch_exception_as_failure(app):
    call, _operation = _prepared_execution_call(call_id="dispatch-error")

    def dispatch(_name, _arguments):
        raise RuntimeError("connector unavailable")

    manager = _ResearchToolManager(dispatch)
    terminal = execute_research_tool_call(call, addon_manager=manager)

    assert terminal.state == "failed"
    assert manager.dispatch_count == 1
    error = rehydrate_research_tool_call(call)["error"]
    assert error["reason"] == "tool_executor_failed"
    assert error["result_envelope"]["status"] == "tool_execution_failed"


@pytest.mark.parametrize(
    ("result", "secret"),
    [
        (
            {
                "status": "waiting_for_user",
                "interaction_type": "structured_input",
                "question": "PRIVATE_QUESTION_81b6",
            },
            "PRIVATE_QUESTION_81b6",
        ),
        (
            {
                "status": "waiting_for_approval",
                "approval_id": "approval-1",
                "message": "PRIVATE_APPROVAL_2c44",
            },
            "PRIVATE_APPROVAL_2c44",
        ),
        (
            {
                "status": "scheduled",
                "monitor_type": "self_nudge",
                "monitor_id": "monitor-1",
                "due_at": "2026-08-26T12:00:00+00:00",
                "reason": "PRIVATE_MONITOR_0aa7",
            },
            "PRIVATE_MONITOR_0aa7",
        ),
    ],
)
def test_research_executor_projects_tool_controlled_event_summaries(
    app, result, secret
):
    call, _operation = _prepared_execution_call(call_id=f"private-summary-{secret}")
    events = []

    execute_research_tool_call(
        call,
        addon_manager=_ResearchToolManager(lambda *_args: result),
        event_sink=AgentEventSink(persist=events.append),
    )

    serialized = json.dumps(events, ensure_ascii=False, sort_keys=True)
    assert secret not in serialized
    assert all(
        event["event"]["title"] == "Translator research tool lifecycle"
        and event["event"]["summary"]
        == "Content retained in encrypted Translator research evidence."
        and set(event["event"]["payload"]) == {"content_retention"}
        for event in events
    )


@pytest.mark.parametrize("outcome", ["success", "error"])
def test_research_executor_host_events_never_copy_private_content(app, outcome):
    argument_secret = "ARGUMENT_SECRET_9f3d"
    result_secret = "RESULT_SECRET_7a2b"
    error_secret = "ERROR_SECRET_4c1e"
    call, _operation = _prepared_execution_call(
        call_id=f"private-event-{outcome}",
        raw=json.dumps({"q": argument_secret}),
    )

    def dispatch(_name, _arguments):
        if outcome == "error":
            raise RuntimeError(error_secret)
        return {"status": "ok", "text": result_secret}

    events = []
    execute_research_tool_call(
        call,
        addon_manager=_ResearchToolManager(dispatch),
        event_sink=AgentEventSink(persist=events.append),
    )

    serialized = json.dumps(events, ensure_ascii=False, sort_keys=True)
    assert argument_secret not in serialized
    assert result_secret not in serialized
    assert error_secret not in serialized
    assert all(
        event["event"]["payload"]["content_retention"]
        == "encrypted_translator_research"
        for event in events
    )


@pytest.mark.parametrize(
    ("drift", "value"),
    [
        ("missing", False),
        ("state", "disabled"),
        ("owner", -1),
        ("addon", "other-addon"),
    ],
)
def test_research_executor_blocks_frozen_connection_drift(app, drift, value):
    call, _operation = _prepared_execution_call(call_id=f"connection-{drift}")
    manager = _ResearchToolManager(lambda *_args: {"status": "never"})
    if drift == "missing":
        manager.connection_exists = value
    elif drift == "state":
        manager.connection_state = value
    elif drift == "owner":
        manager.connection_owner_user_id = value
    else:
        manager.connection_addon_id = value

    terminal = execute_research_tool_call(call, addon_manager=manager)

    assert terminal.state == "blocked"
    assert manager.dispatch_count == 0
    assert (
        rehydrate_research_tool_call(call)["error"]["result_envelope"]["status"]
        == "blocked"
    )


@pytest.mark.parametrize("drift", ["missing", "disabled", "foreign", "addon"])
def test_addon_manager_validates_exact_connection_binding_in_independent_session(
    app, drift
):
    _record, operation = _state()
    service = ConnectionService(db.session)
    owner_user_id = operation.owner_user_id
    connection = None
    if drift != "missing":
        connection_owner = owner_user_id
        if drift == "foreign":
            foreign = User(email="foreign@example.test", password_hash="x")
            db.session.add(foreign)
            db.session.flush()
            connection_owner = foreign.id
        connection = service.create(
            owner_user_id=connection_owner,
            connector_addon_id=("other-addon" if drift == "addon" else "web-addon"),
            connector_definition_id="web-addon.account",
            label="Research account",
            state=("disabled" if drift == "disabled" else "ready"),
        )
        db.session.flush()
        connection_id = connection.id
    else:
        connection_id = "missing-connection"
    db.session.commit()
    manager = AddonManager.__new__(AddonManager)

    with pytest.raises((LookupError, PermissionError, RuntimeError)):
        manager.validate_visible_connection_binding(
            connection_id=connection_id,
            owner_user_id=owner_user_id,
            connector_addon_id="web-addon",
        )


def test_research_executor_discards_untrusted_result_references(app):
    call, _operation = _prepared_execution_call(call_id="untrusted-refs")
    manager = _ResearchToolManager(
        lambda *_args: {
            "status": "ok",
            "artifact_id": "foreign-artifact",
            "context_ref_id": "foreign-context",
            "source_ids": ["foreign-source"],
            "evidence_ids": ["foreign-evidence"],
        }
    )

    terminal = execute_research_tool_call(call, addon_manager=manager)

    assert terminal.state == "succeeded"
    envelope = rehydrate_research_tool_call(call)["result"]
    for key in ("data_refs", "artifact_refs", "source_refs", "evidence_refs"):
        assert key not in envelope
    assert envelope["inline_result"]["artifact_id"] == "foreign-artifact"


def test_research_executor_externalizes_large_result_before_terminal_evidence(app):
    call, _operation = _prepared_execution_call(call_id="large-result")
    content = "large-private-result-" + ("x" * 20_000)
    manager = _ResearchToolManager(lambda *_args: {"status": "ok", "content": content})

    terminal = execute_research_tool_call(call, addon_manager=manager)

    assert terminal.state == "succeeded"
    result = rehydrate_research_tool_call(call)["result"]
    assert result["truncated"] is True
    assert "inline_result" not in result
    assert result["data_refs"]
    assert result["data_refs"][0]["kind"] == "translator_research_result"
    assert rehydrate_research_tool_result_blob(call) == {
        "status": "ok",
        "content": content,
    }
    raw_result = db.session.execute(
        text(
            "SELECT c.result_json, b.payload_json, b.payload_hash "
            "FROM translation_research_tool_call AS c "
            "JOIN translation_research_tool_result_blob AS b "
            "ON b.tool_call_id = c.id WHERE c.id = :id"
        ),
        {"id": call.id},
    ).one()
    assert "large-private-result" not in str(raw_result.result_json)
    assert "large-private-result" not in str(raw_result.payload_json)
    assert (
        raw_result.payload_hash
        != hashlib.sha256(
            json.dumps(
                {"status": "ok", "content": content},
                ensure_ascii=False,
            sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
    )
    for path in Path(app.instance_path).rglob("*"):
        if path.is_file():
            assert b"large-private-result" not in path.read_bytes()


@pytest.mark.parametrize("tamper", ["payload", "missing"])
def test_research_result_blob_tampering_fails_closed(app, tamper):
    call, _operation = _prepared_execution_call(call_id=f"blob-{tamper}")
    manager = _ResearchToolManager(
        lambda *_args: {"status": "ok", "content": "x" * 20_000}
    )
    assert execute_research_tool_call(call, addon_manager=manager).state == "succeeded"
    blob = db.session.get(TranslationResearchToolResultBlob, call.id)
    assert blob is not None
    if tamper == "payload":
        blob.payload_json = {"status": "ok", "content": "tampered"}
    else:
        db.session.delete(blob)
    db.session.commit()

    with pytest.raises(
        BlockContractError,
        match="result-blob-(record|missing)",
    ):
        rehydrate_research_tool_call(call)


def test_large_result_with_independent_session_never_commits_global_uow(app):
    call, _operation = _prepared_execution_call(call_id="independent-blob")
    call_id = call.id
    db.session.rollback()
    pending = User(email="global-pending@example.test", password_hash="x")
    db.session.add(pending)
    independent = sessionmaker(bind=db.engine)()
    try:
        terminal = execute_research_tool_call(
            call_id,
            addon_manager=_ResearchToolManager(
                lambda *_args: {"status": "ok", "content": "x" * 20_000}
            ),
            session=independent,
        )
        assert terminal.state == "succeeded"
        assert pending.id is None
    finally:
        independent.close()
    db.session.rollback()
    assert User.query.filter_by(email="global-pending@example.test").count() == 0
    assert rehydrate_research_tool_result_blob(call_id)["content"] == "x" * 20_000


def test_research_executor_fails_noncanonical_result_without_replay(app):
    call, _operation = _prepared_execution_call(call_id="noncanonical-result")
    manager = _ResearchToolManager(
        lambda *_args: {"status": "ok", "score": float("inf")}
    )

    terminal = execute_research_tool_call(call, addon_manager=manager)

    assert terminal.state == "failed"
    assert manager.dispatch_count == 1
    assert rehydrate_research_tool_call(call)["error"] == {
        "reason": "tool_result_evidence_invalid",
        "contract_code": "research-executor-result",
    }
    assert TranslationResearchToolResultBlob.query.count() == 0
    with pytest.raises(BlockContractError, match="not-prepared"):
        execute_research_tool_call(call, addon_manager=manager)
    assert manager.dispatch_count == 1


def test_research_executor_rejects_coercive_truncated_result(app):
    call, _operation = _prepared_execution_call(call_id="coercive-large-result")
    manager = _ResearchToolManager(
        lambda *_args: {
            "status": "ok",
            "non_json": object(),
            "padding": "x" * 20_000,
        }
    )

    terminal = execute_research_tool_call(call, addon_manager=manager)

    assert terminal.state == "failed"
    assert manager.dispatch_count == 1
    assert rehydrate_research_tool_call(call)["error"] == {
        "reason": "tool_result_evidence_invalid",
        "contract_code": "research-executor-result",
    }
    assert TranslationResearchToolResultBlob.query.count() == 0


def test_post_dispatch_event_failure_stays_executing_until_fence_reconciliation(app):
    call, operation = _prepared_execution_call(call_id="post-dispatch-sink-failure")
    manager = _ResearchToolManager(lambda *_args: {"status": "ok"})
    emitted = []

    def persist_event(packet):
        emitted.append(packet)
        if packet["event"]["kind"] == "tool_call.completed":
            raise RuntimeError("event destination unavailable")

    with pytest.raises(RuntimeError, match="event destination"):
        execute_research_tool_call(
            call,
            addon_manager=manager,
            event_sink=AgentEventSink(persist=persist_event),
        )

    assert manager.dispatch_count == 1
    assert rehydrate_research_tool_call(call)["events"][-1].event_type == "executing"
    with pytest.raises(BlockContractError, match="not-prepared"):
        execute_research_tool_call(call, addon_manager=manager)
    assert manager.dispatch_count == 1

    operation.state = "failed"
    operation.version += 1
    db.session.commit()
    reconciled = reconcile_research_tool_call(
        call,
        evidence={"stage": "post_dispatch_event"},
    )
    assert reconciled.state == "uncertain"
