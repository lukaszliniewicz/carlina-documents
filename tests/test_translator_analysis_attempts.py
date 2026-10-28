"""Phase-5B generic analysis evidence tests (all provider I/O is injected)."""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import timedelta
from pathlib import Path

import pytest
from content_storage_support import bind_content_policy
from flask import Flask
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from carlina.core.semantic_html import SEMANTIC_HTML_CONTRACT_VERSION
from carlina.core.utc import utc_now_db
from iarf_assistant.addons.translator.analysis_attempts import (
    ANALYSIS_CONTRACT_VERSION_V2,
    _event_hash,
    _record_values,
    _time,
    append_analysis_attempt_event,
    canonical_analysis_assistant_message,
    persist_analysis_attempt,
    rehydrate_analysis_attempt,
    rehydrate_analysis_attempt_events,
    rehydrate_completed_analysis_output,
)
from iarf_assistant.addons.translator.analysis_execution import (
    _claim_dispatch,
    execute_analysis_attempt,
    reconcile_dispatched_analysis_attempt,
    resume_analysis_attempt,
    terminalize_stale_prepared_analysis_attempt,
)
from iarf_assistant.addons.translator.block_contract import (
    AttestedDocumentSnapshot,
    BlockContractError,
    sha256_text,
)
from iarf_assistant.addons.translator.block_persistence import (
    persist_source_plan,
    source_attestation_for_project,
)
from iarf_assistant.addons.translator.block_planner import plan_document_blocks
from iarf_assistant.addons.translator.models import (
    TranslationAnalysisAttempt,
    TranslationAnalysisAttemptEvent,
    TranslationOperation,
    TranslationProject,
)
from iarf_assistant.addons.translator.operations import TranslationOperationNotRunnable
from iarf_assistant.core.encryption import EncryptionService, InstallationKeyStore
from iarf_assistant.extensions import db
from iarf_assistant.models import User


@pytest.fixture
def app(tmp_path: Path):
    application = Flask(__name__, instance_path=str(tmp_path / "instance"))
    bind_content_policy(application)
    application.config.update(
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{tmp_path / 'analysis.db'}",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SECRET_KEY="analysis-test",
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


def _state(suffix: str = ""):
    user = User(email=f"analysis{suffix}@example.test", password_hash="x")
    db.session.add(user)
    db.session.flush()
    project = TranslationProject(
        user_id=user.id,
        name="Analysis",
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
    html = "<html><body><p>Exact private source text.</p></body></html>"
    digest = hashlib.sha256(html.encode()).hexdigest()
    plan = plan_document_blocks(
        AttestedDocumentSnapshot(
            id="doc",
            document_id="doc",
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
    record, _ = persist_source_plan(project, plan)
    operation = TranslationOperation(
        project_id=project.id,
        owner_user_id=user.id,
        kind="document_block_translate",
        idempotency_key="analysis",
        request_fingerprint="a" * 64,
        state="running",
        queue_job_id="analysis-worker",
        source_content_hash=plan.snapshot.content_hash,
        source_plan_id=record.id,
        source_plan_hash=plan.plan_hash,
        lease_expires_at=utc_now_db() + timedelta(minutes=5),
    )
    db.session.add(operation)
    db.session.commit()
    return record, operation


def _attempt(record, operation, **kwargs):
    values = {
        "operation": operation,
        "source_plan": record,
        "analysis_kind": "document_brief",
        "contract_version": "analysis-attempt-v1",
        "packet": {
            "messages": [{"role": "system", "content": "Keep private."}],
            "input": {"source": "Exact private source text."},
        },
        "model": {"model_id": "configured/model"},
        "profile": {"profile": "quality"},
        "settings": {"temperature": 0},
        "budget": {"output_tokens": 100},
    }
    values.update(kwargs)
    return persist_analysis_attempt(**values)


def test_analysis_attempt_roundtrip_is_encrypted_and_runs_once(app):
    record, operation = _state("-provider")
    attempt = _attempt(record, operation)
    db.session.commit()
    raw = db.session.execute(
        text("SELECT packet_json FROM translation_analysis_attempt")
    ).scalar_one()
    assert "Exact private source text." not in str(raw)
    restored = rehydrate_analysis_attempt(attempt, record)
    assert restored["input"]["source"] == "Exact private source text."
    calls = []
    result = execute_analysis_attempt(
        attempt,
        completion_call=lambda packet: calls.append(packet) or {"brief": "ready"},
    )
    assert result == {"brief": "ready"}
    assert len(calls) == 1
    assert [
        event.event_type for event in rehydrate_analysis_attempt_events(attempt)
    ] == ["request_prepared", "provider_dispatched", "provider_response", "completed"]
    with pytest.raises(BlockContractError, match="analysis-dispatch-replay"):
        execute_analysis_attempt(
            attempt, completion_call=lambda _packet: pytest.fail("replayed")
        )


def _research_messages() -> list[dict[str, object]]:
    return [
        {"role": "system", "content": "Use frozen read-only tools."},
        {"role": "user", "content": "Research the exact private topic."},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "provider-call-exact-1",
                    "type": "function",
                    "function": {
                        "name": "library.lookup",
                        "arguments": '{  "query": "Exact private topic", "limit": 2 }',
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "provider-call-exact-1",
            "name": "library.lookup",
            "content": '{"status":"ok","result":"Exact private result"}',
        },
    ]


def test_analysis_attempt_v2_preserves_replayable_tool_messages_exactly(app):
    record, operation = _state("-research-packet")
    messages = _research_messages()
    attempt = _attempt(
        record,
        operation,
        analysis_kind="research",
        contract_version=ANALYSIS_CONTRACT_VERSION_V2,
        packet={"messages": messages, "input": {"turn": 2}},
        tool_declarations=[
            {
                "type": "function",
                "function": {"name": "library.lookup", "parameters": {}},
            }
        ],
    )
    db.session.commit()

    raw = db.session.execute(
        text(
            "SELECT packet_json FROM translation_analysis_attempt WHERE id = :id"
        ),
        {"id": attempt.id},
    ).scalar_one()
    assert "Exact private topic" not in str(raw)
    restored = rehydrate_analysis_attempt(attempt, record)
    assert restored["messages"] == messages
    assert (
        restored["messages"][2]["tool_calls"][0]["function"]["arguments"]
        == '{  "query": "Exact private topic", "limit": 2 }'
    )
    calls: list[dict[str, object]] = []
    execute_analysis_attempt(
        attempt,
        completion_call=lambda packet: calls.append(packet)
        or {"role": "assistant", "content": "done"},
    )
    assert calls[0]["messages"] == messages


def test_analysis_attempt_v2_rejects_non_assistant_provider_output(app):
    record, operation = _state("-research-invalid-output")
    attempt = _attempt(
        record,
        operation,
        analysis_kind="research",
        contract_version=ANALYSIS_CONTRACT_VERSION_V2,
        packet={"messages": [{"role": "user", "content": "Research"}], "input": {}},
    )
    db.session.commit()
    with pytest.raises(BlockContractError, match="analysis-assistant-message"):
        execute_analysis_attempt(
            attempt,
            completion_call=lambda _packet: {"content": "missing role"},
        )
    db.session.expire_all()
    assert attempt.dispatch_state == "failed"
    assert [
        event.event_type for event in rehydrate_analysis_attempt_events(attempt)
    ] == ["request_prepared", "provider_dispatched", "failed"]


def test_analysis_attempt_v2_rejects_undeclared_tool_request(app):
    record, operation = _state("-research-undeclared-tool")
    attempt = _attempt(
        record,
        operation,
        analysis_kind="research",
        contract_version=ANALYSIS_CONTRACT_VERSION_V2,
        packet={"messages": [{"role": "user", "content": "Research"}], "input": {}},
        tool_declarations=[
            {
                "type": "function",
                "function": {"name": "library.lookup", "parameters": {}},
            }
        ],
    )
    db.session.commit()
    with pytest.raises(BlockContractError, match="analysis-response-tool-undeclared"):
        execute_analysis_attempt(
            attempt,
            completion_call=lambda _packet: {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "invented-call",
                        "type": "function",
                        "function": {"name": "shell.exec", "arguments": "{}"},
                    }
                ],
            },
        )
    db.session.expire_all()
    assert attempt.dispatch_state == "failed"
    assert [
        event.event_type for event in rehydrate_analysis_attempt_events(attempt)
    ] == ["request_prepared", "provider_dispatched", "failed"]


def test_analysis_attempt_v2_rejects_reused_tool_id_on_second_turn(app):
    """A later assistant message cannot reuse an earlier provider call id."""

    record, operation = _state("-research-duplicate-extension")
    attempt = _attempt(
        record,
        operation,
        analysis_kind="research",
        contract_version=ANALYSIS_CONTRACT_VERSION_V2,
        packet={"messages": _research_messages(), "input": {"turn": 2}},
        tool_declarations=[
            {
                "type": "function",
                "function": {"name": "library.lookup", "parameters": {}},
            }
        ],
    )
    db.session.commit()
    with pytest.raises(
        BlockContractError, match="analysis-response-tool-call-duplicate"
    ):
        execute_analysis_attempt(
            attempt,
            completion_call=lambda _packet: {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "provider-call-exact-1",
                        "type": "function",
                        "function": {
                            "name": "library.lookup",
                            "arguments": '{"query":"same id"}',
                        },
                    }
                ],
            },
        )
    db.session.expire_all()
    assert attempt.dispatch_state == "failed"
    events = rehydrate_analysis_attempt_events(attempt)
    assert [event.event_type for event in events] == [
        "request_prepared",
        "provider_dispatched",
        "failed",
    ]
    assert not any(event.event_type == "provider_response" for event in events)


def test_canonical_assistant_message_preserves_pending_tool_calls_exactly():
    assistant = _research_messages()[2]
    assert canonical_analysis_assistant_message(assistant) == assistant
    with pytest.raises(BlockContractError, match="analysis-assistant-message"):
        canonical_analysis_assistant_message(
            {"role": "user", "content": "invent a tool call"}
        )
    with pytest.raises(BlockContractError, match="analysis-packet-tool-call"):
        canonical_analysis_assistant_message(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "same-id",
                        "type": "function",
                        "function": {"name": "library.lookup", "arguments": "{}"},
                    },
                    {
                        "id": "same-id",
                        "type": "function",
                        "function": {"name": "library.lookup", "arguments": "{}"},
                    },
                ],
            }
        )


def test_analysis_attempt_v1_does_not_silently_accept_tool_call_metadata(app):
    record, operation = _state("-v1-tool-metadata")
    with pytest.raises(BlockContractError, match="analysis-packet-message"):
        _attempt(
            record,
            operation,
            packet={"messages": _research_messages(), "input": {}},
        )
    assert db.session.query(TranslationAnalysisAttempt).count() == 0


@pytest.mark.parametrize(
    "messages, code",
    [
        (
            [
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "pending",
                            "type": "function",
                            "function": {"name": "library.lookup", "arguments": "{}"},
                        }
                    ],
                }
            ],
            "analysis-packet-tool-result",
        ),
        (
            [
                {
                    "role": "tool",
                    "tool_call_id": "orphan",
                    "name": "library.lookup",
                    "content": "{}",
                }
            ],
            "analysis-packet-tool-result",
        ),
        (
            [
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "mismatch",
                            "type": "function",
                            "function": {"name": "library.lookup", "arguments": "{}"},
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "mismatch",
                    "name": "other.lookup",
                    "content": "{}",
                },
            ],
            "analysis-packet-tool-result",
        ),
        (
            [
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "duplicate",
                            "type": "function",
                            "function": {"name": "library.lookup", "arguments": "{}"},
                        },
                        {
                            "id": "duplicate",
                            "type": "function",
                            "function": {"name": "library.lookup", "arguments": "{}"},
                        },
                    ],
                }
            ],
            "analysis-packet-tool-call",
        ),
        (
            [
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "parsed",
                            "type": "function",
                            "function": {
                                "name": "library.lookup",
                                "arguments": {"query": "changed representation"},
                            },
                        }
                    ],
                }
            ],
            "analysis-packet-tool-call",
        ),
    ],
)
def test_analysis_attempt_v2_rejects_non_replayable_tool_transcripts(
    app, messages, code
):
    record, operation = _state(f"-{code}")
    with pytest.raises(BlockContractError, match=code):
        _attempt(
            record,
            operation,
            analysis_kind="research",
            contract_version=ANALYSIS_CONTRACT_VERSION_V2,
            packet={"messages": messages, "input": {}},
        )
    assert db.session.query(TranslationAnalysisAttempt).count() == 0


def test_analysis_attempt_rejects_retry_packet_mutation(app):
    record, operation = _state("-provider")
    parent = _attempt(record, operation)
    db.session.commit()
    with pytest.raises(BlockContractError, match="analysis-lineage-terminal"):
        _attempt(record, operation, retry_of_attempt_id=parent.id)
    execute_analysis_attempt(parent, completion_call=lambda _packet: {"brief": "ready"})
    _attempt(record, operation, retry_of_attempt_id=parent.id)
    with pytest.raises(BlockContractError, match="analysis-retry-packet"):
        _attempt(
            record,
            operation,
            retry_of_attempt_id=parent.id,
            settings={"temperature": 1},
        )


def test_analysis_attempt_rejects_invalid_json_and_tamper(app):
    record, operation = _state("-failure")
    with pytest.raises(BlockContractError, match="analysis-packet"):
        _attempt(record, operation, packet={"messages": [], "input": {1: "coerces"}})
    attempt = _attempt(record, operation)
    db.session.commit()
    db.session.execute(
        text(
            "UPDATE translation_analysis_attempt SET packet_hash = :value WHERE id = :id"
        ),
        {"value": "0" * 64, "id": attempt.id},
    )
    db.session.commit()
    db.session.expire_all()
    with pytest.raises(BlockContractError, match="analysis-attempt-evidence"):
        rehydrate_analysis_attempt(
            db.session.get(TranslationAnalysisAttempt, attempt.id), record
        )


def test_analysis_attempt_cancellation_and_provider_failure_are_terminal(app):
    record, operation = _state()
    cancelled = _attempt(record, operation)
    db.session.commit()
    db.session.execute(
        text(
            "UPDATE translation_operation SET state = 'cancel_requested', "
            "cancel_requested_at = CURRENT_TIMESTAMP WHERE id = :id"
        ),
        {"id": operation.id},
    )
    db.session.commit()
    with pytest.raises(Exception, match="cancellation"):
        execute_analysis_attempt(
            cancelled, completion_call=lambda _packet: pytest.fail("called")
        )
    db.session.expire_all()
    assert (
        db.session.get(TranslationAnalysisAttempt, cancelled.id).dispatch_state
        == "cancelled"
    )

    # A fresh operation proves that an adapter exception becomes durable
    # provider_failed evidence and cannot be replayed by delivery recovery.
    record, operation = _state("-failure")
    failed = _attempt(record, operation)
    db.session.commit()
    with pytest.raises(RuntimeError, match="adapter"):
        execute_analysis_attempt(
            failed,
            completion_call=lambda _packet: (_ for _ in ()).throw(
                RuntimeError("adapter")
            ),
        )
    db.session.expire_all()
    assert (
        db.session.get(TranslationAnalysisAttempt, failed.id).dispatch_state
        == "provider_failed"
    )
    with pytest.raises(BlockContractError, match="analysis-dispatch-replay"):
        execute_analysis_attempt(
            failed, completion_call=lambda _packet: pytest.fail("replayed")
        )


def test_preparation_is_atomic_and_requires_a_clean_live_operation(app, monkeypatch):
    record, operation = _state("-preparation")

    def fail_event(*_args, **_kwargs):
        raise BlockContractError("forced-event-failure")

    monkeypatch.setattr(
        "iarf_assistant.addons.translator.analysis_attempts.append_analysis_attempt_event",
        fail_event,
    )
    with pytest.raises(BlockContractError, match="analysis-attempt-conflict"):
        _attempt(record, operation)
    db.session.commit()
    assert db.session.query(TranslationAnalysisAttempt).count() == 0
    monkeypatch.undo()

    operation.message = "unflushed caller mutation"
    with pytest.raises(BlockContractError, match="analysis-operation-durable"):
        _attempt(record, operation)
    db.session.rollback()
    for column, value in (
        ("state", "queued"),
        ("queue_job_id", None),
        ("lease_expires_at", "2000-01-01 00:00:00"),
    ):
        db.session.execute(
            text(f"UPDATE translation_operation SET {column} = :value WHERE id = :id"),
            {"value": value, "id": operation.id},
        )
        db.session.commit()
        db.session.expire_all()
        with pytest.raises(BlockContractError, match="analysis-operation-fence"):
            _attempt(record, db.session.get(TranslationOperation, operation.id))
        db.session.execute(
            text(
                "UPDATE translation_operation SET state = 'running', "
                "queue_job_id = 'analysis-worker', "
                "lease_expires_at = :lease WHERE id = :id"
            ),
            {"lease": utc_now_db() + timedelta(minutes=5), "id": operation.id},
        )
        db.session.commit()
    with pytest.raises(BlockContractError, match="analysis-contract-version"):
        _attempt(
            record,
            db.session.get(TranslationOperation, operation.id),
            contract_version=" v1 ",
        )


def test_independent_runner_does_not_flush_dirty_caller_uow(app):
    record, operation = _state("-dirty-caller")
    attempt = _attempt(record, operation)
    db.session.commit()
    operation.message = "caller-only-dirty"
    calls = []
    assert execute_analysis_attempt(
        attempt,
        completion_call=lambda _packet: calls.append(1) or {"ok": True},
    ) == {"ok": True}
    assert calls == [1]
    with db.session.no_autoflush:
        stored_message = db.session.execute(
            text("SELECT message FROM translation_operation WHERE id = :id"),
            {"id": operation.id},
        ).scalar_one()
    assert stored_message is None
    assert operation.message == "caller-only-dirty"
    db.session.rollback()
    db.session.expire_all()
    assert db.session.get(TranslationOperation, operation.id).message is None


def test_owner_drift_between_precheck_and_claim_is_terminalized_without_provider(
    app,
):
    record, operation = _state("-owner-drift")
    attempt = _attempt(record, operation)
    other_user = User(
        email="analysis-owner-drift-other@example.test", password_hash="x"
    )
    db.session.add(other_user)
    db.session.commit()
    attempt_id = attempt.id
    operation_id = operation.id

    def drift_owner():
        other = sessionmaker(bind=db.engine, expire_on_commit=False)()
        try:
            other.execute(
                text(
                    "UPDATE translation_operation SET owner_user_id = :owner "
                    "WHERE id = :id"
                ),
                {"owner": other_user.id, "id": operation_id},
            )
            other.commit()
        finally:
            other.close()

    with pytest.raises(TranslationOperationNotRunnable):
        execute_analysis_attempt(
            TranslationAnalysisAttempt(id=attempt_id),
            completion_call=lambda _packet: pytest.fail("provider drifted owner"),
            before_dispatch_claim=drift_owner,
        )
    db.session.expire_all()
    durable = db.session.get(TranslationAnalysisAttempt, attempt_id)
    assert durable is not None and durable.dispatch_state == "fence_failed"
    assert [
        event.event_type for event in rehydrate_analysis_attempt_events(durable)
    ] == ["request_prepared", "post_response_fence_failed"]


def test_reserved_tool_events_are_rejected(app):
    record, operation = _state("-reserved-tools")
    attempt = _attempt(record, operation)
    db.session.commit()
    with pytest.raises(BlockContractError, match="analysis-event-type"):
        append_analysis_attempt_event(
            attempt, "tool_call_started", {"tool": "future-tool"}
        )


def test_dispatch_race_and_post_response_cancellation_are_witnessed(app):
    record, operation = _state("-dispatch-race")
    attempt = _attempt(record, operation)
    attempt_id = str(attempt.id)
    db.session.commit()
    barrier = threading.Barrier(2)
    counter = []
    result_lock = threading.Lock()
    outcomes = []
    errors = []

    def provider(_packet):
        with result_lock:
            counter.append(1)
        return {"response": "private durable response"}

    def run():
        with app.app_context():
            try:
                row = db.session.get(TranslationAnalysisAttempt, attempt_id)
                assert row is not None
                execute_analysis_attempt(
                    row,
                    completion_call=provider,
                    before_dispatch_claim=lambda: barrier.wait(timeout=10),
                )
                outcome = "ok"
            except BlockContractError as exc:
                outcome = exc.code
            except Exception as exc:  # noqa: BLE001 - surface thread failures
                with result_lock:
                    errors.append(exc)
                return
            finally:
                db.session.remove()
            with result_lock:
                outcomes.append(outcome)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    assert counter == [1]
    assert sorted(outcomes) == ["analysis-dispatch-replay", "ok"]
    db.session.expire_all()
    durable = db.session.get(TranslationAnalysisAttempt, attempt_id)
    assert durable.dispatch_state == "completed"
    raw = db.session.execute(
        text(
            "SELECT payload_json FROM translation_analysis_attempt_event WHERE attempt_id = :id AND event_type = 'provider_response'"
        ),
        {"id": attempt_id},
    ).scalar_one()
    assert "private durable response" not in str(raw)
    assert (
        rehydrate_analysis_attempt(durable, record)["input"]["source"]
        == "Exact private source text."
    )
    assert rehydrate_completed_analysis_output(durable, record) == {
        "response": "private durable response"
    }

    record, operation = _state("-post-cancel")
    cancelled = _attempt(record, operation)
    db.session.commit()

    def cancel_inside_provider(_packet):
        db.session.execute(
            text(
                "UPDATE translation_operation SET state = 'cancel_requested', cancel_requested_at = CURRENT_TIMESTAMP WHERE id = :id"
            ),
            {"id": operation.id},
        )
        db.session.commit()
        return {"response": "recorded-before-cancellation"}

    with pytest.raises(Exception, match="cancellation"):
        execute_analysis_attempt(
            cancelled,
            completion_call=cancel_inside_provider,
        )
    db.session.expire_all()
    durable = db.session.get(TranslationAnalysisAttempt, cancelled.id)
    assert durable.dispatch_state == "cancelled"
    with pytest.raises(BlockContractError, match="analysis-output-not-completed"):
        rehydrate_completed_analysis_output(durable, record)
    assert [
        event.event_type for event in rehydrate_analysis_attempt_events(durable)
    ] == ["request_prepared", "provider_dispatched", "provider_response", "cancelled"]


def test_transcript_tamper_rotation_and_pure_normalizer_rollback(app):
    record, operation = _state("-normalizer")
    attempt = _attempt(record, operation)
    db.session.commit()
    execute_analysis_attempt(
        attempt, completion_call=lambda _packet: {"response": "ok"}
    )
    app.extensions["carlina_encryption"].key_store.rotate()
    db.session.expire_all()
    durable = db.session.get(TranslationAnalysisAttempt, attempt.id)
    assert rehydrate_analysis_attempt(durable, record)["model"] == {
        "model_id": "configured/model"
    }
    terminal = rehydrate_analysis_attempt_events(durable)[-1]
    terminal.event_type = "provider_failure"
    terminal.event_hash = _event_hash(terminal)
    db.session.commit()
    db.session.expire_all()
    with pytest.raises(BlockContractError, match="analysis-dispatch-receipt"):
        rehydrate_analysis_attempt(
            db.session.get(TranslationAnalysisAttempt, attempt.id), record
        )


def test_attempt_retry_reuses_verified_witnesses_after_key_rotation(app):
    record, operation = _state("-attempt-rotation")
    parent = _attempt(record, operation)
    db.session.commit()
    execute_analysis_attempt(parent, completion_call=lambda _packet: {"ok": True})
    db.session.expire_all()
    parent = db.session.get(TranslationAnalysisAttempt, parent.id)
    assert parent is not None
    old_hashes = {
        name: getattr(parent, f"{name}_hash")
        for name in ("packet", "model", "profile", "settings", "budget", "tools")
    }
    app.extensions["carlina_encryption"].key_store.rotate()
    db.session.expire_all()
    assert rehydrate_analysis_attempt(parent, record)["model"] == {
        "model_id": "configured/model"
    }

    retry = _attempt(record, operation, retry_of_attempt_id=parent.id)
    assert retry.request_hash == parent.request_hash
    assert {
        name: getattr(retry, f"{name}_hash")
        for name in old_hashes
    } == old_hashes
    db.session.commit()

    with pytest.raises(BlockContractError, match="analysis-rebase-identity"):
        _attempt(record, operation, rebase_of_attempt_id=parent.id)
    changed = _attempt(
        record,
        operation,
        rebase_of_attempt_id=parent.id,
        settings={"temperature": 1},
    )
    assert changed.request_hash != parent.request_hash


def test_final_completion_fence_is_atomic_against_isolated_operation_change(
    app, monkeypatch
):
    record, operation = _state("-final-fence")
    attempt = _attempt(record, operation)
    db.session.commit()
    original = __import__(
        "iarf_assistant.addons.translator.analysis_execution",
        fromlist=["_fresh_fence"],
    )._fresh_fence
    changed = False

    def observe_then_change(attempt_id):
        nonlocal changed
        result = original(attempt_id)
        observer = sessionmaker(bind=db.engine, expire_on_commit=False)()
        try:
            durable = observer.get(TranslationAnalysisAttempt, attempt_id)
        finally:
            observer.close()
        if (
            durable is not None
            and durable.dispatch_state == "response_received"
            and not changed
        ):
            changed = True
            db.session.commit()
            other = sessionmaker(bind=db.engine, expire_on_commit=False)()
            try:
                other.execute(
                    text(
                        "UPDATE translation_operation SET state = 'cancel_requested', "
                        "version = version + 1, cancel_requested_at = CURRENT_TIMESTAMP "
                        "WHERE id = :id"
                    ),
                    {"id": operation.id},
                )
                other.commit()
            finally:
                other.close()
        return result

    monkeypatch.setattr(
        "iarf_assistant.addons.translator.analysis_execution._fresh_fence",
        observe_then_change,
    )
    with pytest.raises(Exception, match="cancellation"):
        execute_analysis_attempt(
            attempt, completion_call=lambda _packet: {"response": "preserve"}
        )
    db.session.expire_all()
    durable = db.session.get(TranslationAnalysisAttempt, attempt.id)
    assert durable is not None
    assert durable.dispatch_state == "cancelled"
    assert [
        event.event_type for event in rehydrate_analysis_attempt_events(durable)
    ] == ["request_prepared", "provider_dispatched", "provider_response", "cancelled"]


def test_response_received_resumes_without_provider_after_restart(app, monkeypatch):
    record, operation = _state("-resume")
    attempt = _attempt(record, operation)
    attempt_id = attempt.id
    db.session.commit()
    calls = []

    def crash_before_finalization(*_args, **_kwargs):
        raise RuntimeError("simulated worker restart")

    monkeypatch.setattr(
        "iarf_assistant.addons.translator.analysis_execution._finalize_response",
        crash_before_finalization,
    )
    with pytest.raises(RuntimeError, match="simulated worker restart"):
        execute_analysis_attempt(
            attempt,
            completion_call=lambda _packet: calls.append(1) or {"response": "saved"},
        )
    monkeypatch.undo()
    db.session.remove()
    detached = TranslationAnalysisAttempt(id=attempt_id)
    assert resume_analysis_attempt(detached) == {"response": "saved"}
    assert calls == [1]
    db.session.expire_all()
    durable = db.session.get(TranslationAnalysisAttempt, attempt_id)
    assert durable is not None and durable.dispatch_state == "completed"
    assert resume_analysis_attempt(TranslationAnalysisAttempt(id=attempt_id)) == {
        "response": "saved"
    }


def test_response_received_resume_fence_loss_terminalizes_without_provider(
    app, monkeypatch
):
    record, operation = _state("-resume-cancel")
    attempt = _attempt(record, operation)
    attempt_id = attempt.id
    db.session.commit()

    monkeypatch.setattr(
        "iarf_assistant.addons.translator.analysis_execution._finalize_response",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("simulated worker restart")
        ),
    )
    with pytest.raises(RuntimeError, match="simulated worker restart"):
        execute_analysis_attempt(
            attempt, completion_call=lambda _packet: {"response": "saved"}
        )
    monkeypatch.undo()
    db.session.execute(
        text(
            "UPDATE translation_operation SET state = 'cancel_requested', "
            "cancel_requested_at = CURRENT_TIMESTAMP WHERE id = :id"
        ),
        {"id": operation.id},
    )
    db.session.commit()
    db.session.remove()
    with pytest.raises(Exception, match="cancellation"):
        resume_analysis_attempt(TranslationAnalysisAttempt(id=attempt_id))
    db.session.expire_all()
    durable = db.session.get(TranslationAnalysisAttempt, attempt_id)
    assert durable is not None and durable.dispatch_state == "cancelled"


def test_resume_rehydration_uses_independent_session_with_dirty_caller(app, monkeypatch):
    record, operation = _state("-resume-session")
    attempt = _attempt(record, operation)
    attempt_id = attempt.id
    db.session.commit()
    monkeypatch.setattr(
        "iarf_assistant.addons.translator.analysis_execution._finalize_response",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("simulated worker restart")
        ),
    )
    with pytest.raises(RuntimeError, match="simulated worker restart"):
        execute_analysis_attempt(
            attempt, completion_call=lambda _packet: {"response": "saved"}
        )
    monkeypatch.undo()
    operation.message = "caller-only-dirty"
    assert resume_analysis_attempt(TranslationAnalysisAttempt(id=attempt_id)) == {
        "response": "saved"
    }
    with db.session.no_autoflush:
        assert db.session.execute(
            text("SELECT message FROM translation_operation WHERE id = :id"),
            {"id": operation.id},
        ).scalar_one() is None
    db.session.rollback()


def test_reconcile_rehydration_uses_independent_session_with_dirty_caller(app):
    record, operation = _state("-reconcile-session")
    attempt = _attempt(record, operation)
    operation_id = operation.id
    db.session.commit()
    _claim_dispatch(attempt.id)
    operation.message = "caller-only-dirty"
    other = sessionmaker(bind=db.engine, expire_on_commit=False)()
    try:
        other.execute(
            text(
                "UPDATE translation_operation SET state = 'queued', "
                "version = version + 1 WHERE id = :id"
            ),
            {"id": operation_id},
        )
        other.commit()
    finally:
        other.close()
    reconcile_dispatched_analysis_attempt(TranslationAnalysisAttempt(id=attempt.id))
    with db.session.no_autoflush:
        assert db.session.execute(
            text("SELECT message FROM translation_operation WHERE id = :id"),
            {"id": operation_id},
        ).scalar_one() is None
    db.session.rollback()


def test_dispatched_reconciliation_requires_lost_fence_and_never_calls_provider(app):
    record, operation = _state("-uncertain")
    attempt = _attempt(record, operation)
    db.session.commit()
    _claim_dispatch(attempt.id)
    db.session.expire_all()
    with pytest.raises(BlockContractError, match="analysis-operation-fence-live"):
        reconcile_dispatched_analysis_attempt(
            TranslationAnalysisAttempt(id=attempt.id)
        )
    db.session.execute(
        text(
            "UPDATE translation_operation SET state = 'queued', "
            "version = version + 1 WHERE id = :id"
        ),
        {"id": operation.id},
    )
    db.session.commit()
    reconcile_dispatched_analysis_attempt(TranslationAnalysisAttempt(id=attempt.id))
    db.session.expire_all()
    durable = db.session.get(TranslationAnalysisAttempt, attempt.id)
    assert durable is not None
    assert durable.dispatch_state == "provider_outcome_uncertain"
    assert durable.dispatch_finished_at is not None
    assert [
        event.event_type for event in rehydrate_analysis_attempt_events(durable)
    ] == ["request_prepared", "provider_dispatched", "provider_outcome_uncertain"]
    with pytest.raises(BlockContractError, match="analysis-dispatch-replay"):
        execute_analysis_attempt(
            durable, completion_call=lambda _packet: pytest.fail("provider replay")
        )


def test_stale_prepared_terminalization_is_no_io_idempotent_and_retryable(app):
    record, operation = _state("-prepared-reconcile")
    attempt = _attempt(record, operation)
    attempt_id = attempt.id
    db.session.commit()

    with pytest.raises(BlockContractError, match="analysis-operation-fence-live"):
        terminalize_stale_prepared_analysis_attempt(
            TranslationAnalysisAttempt(id=attempt_id)
        )

    db.session.execute(
        text(
            "UPDATE translation_operation SET version = version + 1 "
            "WHERE id = :id"
        ),
        {"id": operation.id},
    )
    db.session.commit()
    terminalize_stale_prepared_analysis_attempt(
        TranslationAnalysisAttempt(id=attempt_id)
    )
    # Exactly this pre-dispatch terminal transcript is idempotent.
    terminalize_stale_prepared_analysis_attempt(
        TranslationAnalysisAttempt(id=attempt_id)
    )
    db.session.expire_all()
    terminal = db.session.get(TranslationAnalysisAttempt, attempt_id)
    assert terminal is not None
    assert terminal.dispatch_state == "fence_failed"
    assert terminal.dispatch_finished_at is not None
    events = rehydrate_analysis_attempt_events(terminal)
    assert [event.event_type for event in events] == [
        "request_prepared",
        "post_response_fence_failed",
    ]
    assert events[-1].payload_json == {"code": "operation-fence-lost-before-dispatch"}

    current_operation = db.session.get(TranslationOperation, operation.id)
    assert current_operation is not None
    retry = _attempt(
        record,
        current_operation,
        retry_of_attempt_id=attempt_id,
    )
    assert retry.retry_of_attempt_id == attempt_id


def test_prepared_terminalization_rejects_dispatched_response_and_completed(app, monkeypatch):
    record, operation = _state("-prepared-invalid")
    dispatched = _attempt(record, operation, settings={"state": "dispatched"})
    db.session.commit()
    _claim_dispatch(dispatched.id)
    with pytest.raises(BlockContractError, match="analysis-prepared-not-reconcilable"):
        terminalize_stale_prepared_analysis_attempt(
            TranslationAnalysisAttempt(id=dispatched.id)
        )

    response = _attempt(record, operation, settings={"state": "response"})
    db.session.commit()
    with monkeypatch.context() as patched:
        patched.setattr(
            "iarf_assistant.addons.translator.analysis_execution._finalize_response",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("restart")),
        )
        with pytest.raises(RuntimeError, match="restart"):
            execute_analysis_attempt(
                response, completion_call=lambda _packet: {"response": "saved"}
            )
    with pytest.raises(BlockContractError, match="analysis-prepared-not-reconcilable"):
        terminalize_stale_prepared_analysis_attempt(
            TranslationAnalysisAttempt(id=response.id)
        )

    completed = _attempt(record, operation, settings={"state": "completed"})
    db.session.commit()
    execute_analysis_attempt(completed, completion_call=lambda _packet: {"ok": True})
    with pytest.raises(BlockContractError, match="analysis-prepared-not-reconcilable"):
        terminalize_stale_prepared_analysis_attempt(
            TranslationAnalysisAttempt(id=completed.id)
        )


def test_dispatch_receipt_rejects_terminal_event_suffix_truncation(app):
    record, operation = _state("-truncation")
    attempt = _attempt(record, operation)
    db.session.commit()
    execute_analysis_attempt(attempt, completion_call=lambda _packet: {"ok": True})
    db.session.execute(
        text(
            "DELETE FROM translation_analysis_attempt_event "
            "WHERE attempt_id = :id AND event_type = 'completed'"
        ),
        {"id": attempt.id},
    )
    db.session.execute(
        text(
            "UPDATE translation_analysis_attempt SET dispatch_state = 'response_received', "
            "dispatch_version = 3, dispatch_finished_at = NULL WHERE id = :id"
        ),
        {"id": attempt.id},
    )
    db.session.commit()
    db.session.expire_all()
    with pytest.raises(BlockContractError, match="analysis-dispatch-receipt"):
        rehydrate_analysis_attempt(
            db.session.get(TranslationAnalysisAttempt, attempt.id), record
        )


def test_attempt_record_and_event_receipts_bind_raw_row_transplants(app):
    record, operation = _state("-transplant")
    first = _attempt(record, operation, settings={"temperature": 0})
    second = _attempt(record, operation, settings={"temperature": 1})
    db.session.commit()
    setting_values = db.session.execute(
        text(
            "SELECT settings_json, settings_hash, request_hash "
            "FROM translation_analysis_attempt WHERE id = :id"
        ),
        {"id": second.id},
    ).mappings().one()
    event_values = db.session.execute(
        text(
            "SELECT payload_json, payload_hash FROM translation_analysis_attempt_event "
            "WHERE attempt_id = :id AND sequence = 1"
        ),
        {"id": second.id},
    ).mappings().one()
    db.session.execute(
        text(
            "UPDATE translation_analysis_attempt SET settings_json = :settings_json, "
            "settings_hash = :settings_hash, request_hash = :request_hash "
            "WHERE id = :id"
        ),
        {
            "id": first.id,
            "settings_json": setting_values["settings_json"],
            "settings_hash": setting_values["settings_hash"],
            "request_hash": setting_values["request_hash"],
        },
    )
    db.session.execute(
        text(
            "UPDATE translation_analysis_attempt_event SET payload_json = :payload_json, "
            "payload_hash = :payload_hash WHERE attempt_id = :id AND sequence = 1"
        ),
        {
            "id": first.id,
            "payload_json": event_values["payload_json"],
            "payload_hash": event_values["payload_hash"],
        },
    )
    db.session.commit()
    db.session.expire_all()
    transplanted = db.session.get(TranslationAnalysisAttempt, first.id)
    public_record_hash = sha256_text(
        json.dumps(
            _record_values(transplanted),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    db.session.execute(
        text(
            "UPDATE translation_analysis_attempt SET attempt_record_hash = :value "
            "WHERE id = :id"
        ),
        {"id": first.id, "value": public_record_hash},
    )
    transplanted_event = (
        db.session.query(TranslationAnalysisAttemptEvent)
        .filter_by(attempt_id=first.id, sequence=1)
        .one()
    )
    public_event_hash = sha256_text(
        json.dumps(
            {
                "id": transplanted_event.id,
                "attempt_id": transplanted_event.attempt_id,
                "sequence": transplanted_event.sequence,
                "event_type": transplanted_event.event_type,
                "payload_hash": transplanted_event.payload_hash,
                "predecessor_event_hash": transplanted_event.predecessor_event_hash,
                "created_at": _time(
                    transplanted_event.created_at, "analysis-event-created-at"
                ),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    db.session.execute(
        text(
            "UPDATE translation_analysis_attempt_event SET event_hash = :value "
            "WHERE attempt_id = :id AND sequence = 1"
        ),
        {"id": first.id, "value": public_event_hash},
    )
    db.session.commit()
    db.session.expire_all()
    with pytest.raises(BlockContractError, match="analysis-attempt-hash"):
        rehydrate_analysis_attempt(
            db.session.get(TranslationAnalysisAttempt, first.id), record
        )
