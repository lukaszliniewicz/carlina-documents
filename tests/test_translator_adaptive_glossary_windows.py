"""Adaptive tail planning: glossary growth shrinks packing, restarts reuse scopes.

Concise end-to-end coverage through run_document_block_workflow reusing the
workflow-e2e harness style: no new provider contract, all persistence real.
Covers glossary-growth repacking, same-operation restart scope stability
(including a linked footnote translated exactly once), oversize-singleton
stability, the no-glossary baseline, issued-scope invariants, and the
per-pass glossary snapshot query bound.
"""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest

pytest_plugins = ("test_translator_block_workflow_e2e",)

from iarf_assistant import extensions as _extensions
from iarf_assistant import models as _core_models
from iarf_assistant.addons.translator import block_contract as _contract
from iarf_assistant.addons.translator import block_persistence as _persistence
from iarf_assistant.addons.translator import block_planner as _planner
from iarf_assistant.addons.translator import block_translation as _translation
from iarf_assistant.addons.translator import block_workflow_config as _workflow_config
from iarf_assistant.addons.translator import block_workflow_runtime as _workflow_runtime
from iarf_assistant.addons.translator import models as _models
from iarf_assistant.addons.translator import operations as _operations
from iarf_assistant.addons.translator import window_execution as _window_execution
from iarf_assistant.addons.translator import working_glossary as _working_glossary

from carlina.core.semantic_html import SEMANTIC_HTML_CONTRACT_VERSION
from carlina.core.utc import utc_now_db

AttestedDocumentSnapshot = _contract.AttestedDocumentSnapshot
DOCUMENT_BLOCK_ENGINE_CONTRACT = _contract.DOCUMENT_BLOCK_ENGINE_CONTRACT
BlockContractError = _contract.BlockContractError
TranslationOperation = _models.TranslationOperation
TranslationProject = _models.TranslationProject
TranslationTargetBlockActive = _models.TranslationTargetBlockActive
TranslationWindowAttempt = _models.TranslationWindowAttempt
User = _core_models.User
LLMProvider = _core_models.LLMProvider
ApiKey = _core_models.ApiKey
LLMModel = _core_models.LLMModel
db = _extensions.db


class _Provider:
    """Translation-only fake; mirrors the e2e transform for N blocks."""

    def __init__(self, *, fail_on_calls: frozenset[int] = frozenset()):
        self.calls = 0
        self.fail_on_calls = fail_on_calls

    def completion(self, **kwargs: Any) -> dict[str, object]:
        assert kwargs["source"] == "translator_block_translation_window"
        self.calls += 1
        if self.calls in self.fail_on_calls:
            return {"choices": [{"message": {"role": "assistant", "content": "not-json"}}]}
        content = kwargs["messages"][-1]["content"]
        raw = content.rsplit("\n[writable_source target_language=", 1)[1].partition("]\n")[2]
        payload = json.loads(raw)
        results = []
        for writable in payload["blocks"]:
            text = writable["content"].replace("protected", "Przekład")
            if text == writable["content"] and text:
                text = f"Przekład: {text}"
            results.append({
                "n": writable["n"],
                "content": text,
                "fields": [{**field, "value": f"Przekład: {field['value']}"}
                           for field in writable["fields"]],
            })
        return {"choices": [{"message": {"role": "assistant", "content": json.dumps({"results": results})}}]}


from test_translator_block_workflow_e2e import _safe_boundary


def _state(html: str, *, max_blocks: int = 4, target_tokens: int | None = None,
           key: str = "adaptive") -> tuple[Any, ...]:
    user = User(email=f"adaptive-{key}@example.test", password_hash="x")
    db.session.add(user)
    db.session.flush()
    provider = LLMProvider(user_id=None, name=f"adaptive-{key}", provider_kind="native")
    db.session.add(provider)
    db.session.flush()
    db.session.add_all([
        ApiKey(user_id=None, provider_id=provider.id, name="primary",
               api_key="test-only-secret", is_active=True),
        LLMModel(user_id=None, provider_id=provider.id, model_id=f"adaptive-{key}/chat",
                 display_name="Adaptive", is_active=True, manually_added=True,
                 model_type="chat", context_window=1_000_000, max_output_tokens=128_000),
    ])
    project = TranslationProject(
        user_id=user.id, name=f"Adaptive {key}", source_lang="en", target_lang="pl",
        engine_contract=DOCUMENT_BLOCK_ENGINE_CONTRACT,
        settings={
            "translator_model": f"adaptive-{key}/chat",
            "source_reference": {"version": 1, "capability": "document.processing",
                "provider_addon_id": "doc-provider", "provider_capability_id": "process",
                "resource_type": "document", "resource_id": f"adaptive-{key}-doc"},
        },
    )
    db.session.add(project)
    db.session.flush()
    digest = hashlib.sha256(html.encode("utf-8")).hexdigest()
    plan = _planner.plan_document_blocks(
        AttestedDocumentSnapshot(id=f"adaptive-{key}-doc", document_id=f"adaptive-{key}-doc",
            output_html=html, content_hash=digest, semantic_content_hash=digest,
            metadata={}, provenance={}, captured_at="2026-08-26T00:00:00+00:00",
            semantic_contract_version=SEMANTIC_HTML_CONTRACT_VERSION),
        project_id=project.id,
        source_attestation=_persistence.source_attestation_for_project(project),
        source_language="en",
    )
    record, _created = _persistence.persist_source_plan(project, plan)
    operation = TranslationOperation(
        project_id=project.id, owner_user_id=user.id, kind="document_block_translate",
        idempotency_key=f"adaptive-{key}-op", request_fingerprint="d" * 64,
        state="running", queue_job_id=f"adaptive-{key}-worker",
        source_content_hash=record.source_content_hash, source_plan_id=record.id,
        source_plan_hash=record.plan_hash,
        lease_expires_at=utc_now_db() + timedelta(minutes=10),
    )
    db.session.add(operation)
    db.session.commit()
    profile = _window_execution.resolve_execution_profile(project)
    derive_kwargs: dict[str, Any] = {"maximum_blocks": max_blocks}
    if target_tokens is not None:
        derive_kwargs["target_input_tokens"] = target_tokens
    window_profile = _translation.derive_window_profile(profile, **derive_kwargs).to_dict()
    material = _workflow_config.freeze_block_workflow_config(
        operation, record, user=user,
        settings={"translator_model": f"adaptive-{key}/chat", "enable_pre_analysis": False,
                  "enable_research": False, "owner_policy": "Preserve meaning."},
        window_profile=window_profile,
    )
    _workflow_config.persist_block_workflow_config(operation, record, material)
    db.session.commit()
    return user, project, record, operation, plan


def _propose(project: Any, record: Any, entries: list[dict[str, Any]], key: str) -> None:
    _working_glossary.propose_glossary(
        project, source_plan_id=record.id, entries=entries, idempotency_key=key)
    db.session.commit()


def _attempts() -> list[Any]:
    return db.session.query(TranslationWindowAttempt).order_by(
        TranslationWindowAttempt.window_order, TranslationWindowAttempt.created_at).all()


def _reclaim(operation: Any) -> None:
    operation.version += 1
    operation.queue_job_id += "-reclaimed"
    operation.lease_expires_at = utc_now_db() + timedelta(minutes=10)
    db.session.commit()


def _run(project: Any, operation: Any, provider: Any, boundary: Any = None) -> tuple[Any, dict[str, Any]]:
    return _workflow_runtime.run_document_block_workflow(
        project=project, operation=operation, service=provider,
        addon_manager=object(), safe_boundary=boundary or _safe_boundary,
    )


_GROWTH_WORDS = ["Alpha", "Bravo", "Charlie", "Delta", "Echo", "Foxtrot"]


def _growth_html() -> str:
    paras = "".join(
        f"<p>{word} {'filler ' * 120}end.</p>" for word in _GROWTH_WORDS)
    return f"<html><body>{paras}</body></html>"


def test_glossary_growth_shrinks_tail_within_same_operation(app, monkeypatch):
    """Mid-run growth repacks the unissued tail; the issued head is frozen."""
    user, project, record, operation, plan = _state(
        _growth_html(), max_blocks=3, target_tokens=3600, key="growth")
    assert len(plan.blocks) == 6
    injected = {"done": False}
    retry_records = []
    replan = _workflow_runtime.plan_unissued_tail_materials

    def observe_replan(**kwargs):
        retry_records.append(kwargs.get("source_plan_record"))
        return replan(**kwargs)

    monkeypatch.setattr(_workflow_runtime, "plan_unissued_tail_materials", observe_replan)

    def injecting_boundary(op: Any, progress: float, message: str, checkpoint: Any) -> Any:
        updated = _safe_boundary(op, progress, message, checkpoint)
        if (not injected["done"] and isinstance(checkpoint, dict)
                and checkpoint.get("phase") == "translation"
                and checkpoint.get("completed_windows") == 1):
            injected["done"] = True
            print("INJECTED growth terms after window 1")
            _propose(project, record, [
                {"source_term": word, "target_term": f"{word}-pl", "sense": "",
                 "notes": "n" * 2000} for word in _GROWTH_WORDS],
                key="mid-run-growth")
        return updated

    provider = _Provider()
    _completed, result = _run(project, operation, provider, injecting_boundary)
    assert injected["done"] is True
    # The expanded policy leaves room for a smaller batch, but only when retry
    # admission measures the continuity/precedent used by final assembly too.
    assert retry_records and all(item is record for item in retry_records)
    assert result["translated_block_count"] == 6
    rows = _attempts()
    assert [row.operation_id for row in rows] == [operation.id] * len(rows)
    # The tail was repacked smaller after growth: more windows than the
    # initial packing, one provider call per final window, every block once.
    assert len(rows) >= 3
    assert provider.calls == len(rows)
    active = db.session.query(TranslationTargetBlockActive).all()
    assert sorted({row.block_id for row in active}) == sorted(block.id for block in plan.blocks)
    assert len(active) == 6
    first_packet = next(row.packet_json for row in rows if row.window_order == 1)
    assert "terminology_guidance" not in json.dumps(first_packet, ensure_ascii=False)
    later_packets = json.dumps(
        [row.packet_json for row in rows if row.window_order > 1], ensure_ascii=False)
    assert "Foxtrot-pl" in later_packets


def test_restart_reuses_issued_scope_after_glossary_growth(app, monkeypatch):
    """Interruption after durable preparation freezes scope; tail stays live."""
    html = (
        "<html><body><h1>Title</h1>"
        "<p>First protected paragraph.</p>"
        "<p>Second paragraph mentions Heidelberg.</p>"
        '<p>Third paragraph cites a note<a href="#fn1">[1]</a>.</p>'
        '<aside id="fn1" role="doc-footnote"><p>Footnote text.</p></aside>'
        "</body></html>"
    )
    user, project, record, operation, plan = _state(html, max_blocks=1, key="restart")
    total = len(plan.blocks)
    assert total >= 4

    class _Crash(BaseException):
        pass

    captured: dict[str, Any] = {}
    preparations = {"count": 0}
    real_prepare = _workflow_runtime.prepare_and_commit_window_attempt

    def crash_after_second_prepare(**kwargs: Any) -> Any:
        attempt = real_prepare(**kwargs)
        preparations["count"] += 1
        if preparations["count"] == 2:
            captured["order"] = attempt.window_order
            captured["packet"] = json.loads(json.dumps(attempt.packet_json))
            raise _Crash
        return attempt

    monkeypatch.setattr(
        _workflow_runtime, "prepare_and_commit_window_attempt", crash_after_second_prepare)
    provider = _Provider()
    with pytest.raises(_Crash):
        _run(project, operation, provider)
    assert preparations["count"] == 2
    assert captured["order"] == 2
    assert provider.calls == 1
    first_packet = db.session.query(TranslationWindowAttempt).filter_by(
        operation_id=operation.id, window_order=1).one().packet_json
    monkeypatch.undo()
    _propose(project, record, [
        {"source_term": "Heidelberg", "target_term": "Heidelberg-pl", "sense": ""}],
        key="restart-term")
    _reclaim(operation)
    provider2 = _Provider()
    _completed, result = _run(project, operation, provider2)
    assert result["translated_block_count"] == total
    # Issued windows keep byte-identical packets across the restart: the
    # staged window 1 reuses its row; the prepared window 2 resumes through
    # one retry row carrying the identical frozen packet.
    order1 = db.session.query(TranslationWindowAttempt).filter_by(
        operation_id=operation.id, window_order=1).all()
    assert len(order1) == 1
    assert order1[0].packet_json == first_packet
    order2 = db.session.query(TranslationWindowAttempt).filter_by(
        operation_id=operation.id, window_order=2).all()
    assert len(order2) == 2
    assert order2[0].dispatch_state == "prepared"
    assert order2[1].retry_of_attempt_id == order2[0].id
    assert all(json.loads(json.dumps(row.packet_json)) == captured["packet"]
               for row in order2)
    assert order2[0].block_ids_json == order2[1].block_ids_json
    assert "Heidelberg-pl" not in json.dumps(first_packet, ensure_ascii=False)
    assert "Heidelberg-pl" not in json.dumps(order2[1].packet_json, ensure_ascii=False)
    # Every block (body + footnote) translated exactly once.
    active = db.session.query(TranslationTargetBlockActive).all()
    assert sorted({row.block_id for row in active}) == sorted(block.id for block in plan.blocks)
    assert len(active) == total
    # Recovery did not redispatch window 1; later windows carry the new term.
    assert provider2.calls == total - 1
    later_packets = json.dumps([
        row.packet_json for row in _attempts()
        if row.operation_id == operation.id and row.window_order > 1], ensure_ascii=False)
    assert "Heidelberg-pl" in later_packets


def test_no_glossary_baseline_carries_no_terminology(app):
    _user, project, record, operation, plan = _state(
        "<html><body><p>First paragraph.</p><p>Second paragraph.</p></body></html>",
        key="baseline")
    _completed, result = _run(project, operation, _Provider())
    assert result["translated_block_count"] == len(plan.blocks)
    packets = json.dumps([row.packet_json for row in _attempts()], ensure_ascii=False)
    assert "terminology_guidance" not in packets


def test_oversize_singleton_fails_stable_without_loop(app):
    html = f"<html><body><p>{'z' * 400_000}</p></body></html>"
    user = User(email="adaptive-singleton@example.test", password_hash="x")
    db.session.add(user)
    db.session.flush()
    provider = LLMProvider(user_id=None, name="adaptive-singleton", provider_kind="native")
    db.session.add(provider)
    db.session.flush()
    db.session.add_all([
        ApiKey(user_id=None, provider_id=provider.id, name="primary",
               api_key="test-only-secret", is_active=True),
        LLMModel(user_id=None, provider_id=provider.id, model_id="adaptive-singleton/chat",
                 display_name="Singleton", is_active=True, manually_added=True,
                 model_type="chat", context_window=32768, max_output_tokens=4096),
    ])
    project = TranslationProject(
        user_id=user.id, name="Adaptive singleton", source_lang="en", target_lang="pl",
        engine_contract=DOCUMENT_BLOCK_ENGINE_CONTRACT,
        settings={"translator_model": "adaptive-singleton/chat",
                  "source_reference": {"version": 1, "capability": "document.processing",
                      "provider_addon_id": "doc-provider", "provider_capability_id": "process",
                      "resource_type": "document", "resource_id": "adaptive-singleton-doc"}},
    )
    db.session.add(project)
    db.session.flush()
    digest = hashlib.sha256(html.encode("utf-8")).hexdigest()
    plan = _planner.plan_document_blocks(
        AttestedDocumentSnapshot(id="adaptive-singleton-doc", document_id="adaptive-singleton-doc",
            output_html=html, content_hash=digest, semantic_content_hash=digest,
            metadata={}, provenance={}, captured_at="2026-08-26T00:00:00+00:00",
            semantic_contract_version=SEMANTIC_HTML_CONTRACT_VERSION),
        project_id=project.id,
        source_attestation=_persistence.source_attestation_for_project(project),
        source_language="en",
    )
    assert len(plan.blocks) == 1
    record, _created = _persistence.persist_source_plan(project, plan)
    operation = TranslationOperation(
        project_id=project.id, owner_user_id=user.id, kind="document_block_translate",
        idempotency_key="adaptive-singleton-op", request_fingerprint="f" * 64,
        state="running", queue_job_id="adaptive-singleton-worker",
        source_content_hash=record.source_content_hash, source_plan_id=record.id,
        source_plan_hash=record.plan_hash,
        lease_expires_at=utc_now_db() + timedelta(minutes=10),
    )
    db.session.add(operation)
    db.session.commit()
    profile = _window_execution.resolve_execution_profile(project)
    material = _workflow_config.freeze_block_workflow_config(
        operation, record, user=user,
        settings={"translator_model": "adaptive-singleton/chat", "enable_pre_analysis": False,
                  "enable_research": False, "owner_policy": "Preserve meaning."},
        window_profile=_translation.derive_window_profile(profile, maximum_blocks=4).to_dict(),
    )
    _workflow_config.persist_block_workflow_config(operation, record, material)
    db.session.commit()
    provider_fake = _Provider()
    for _ in range(2):
        with pytest.raises(BlockContractError, match="window-oversized-block"):
            _run(project, operation, provider_fake)
    assert provider_fake.calls == 0
    assert db.session.query(TranslationWindowAttempt).count() == 0


class _FakeQuery:
    def __init__(self, rows: list[Any]):
        self._rows = rows

    def filter_by(self, **_kwargs: Any) -> "_FakeQuery":
        return self

    def all(self) -> list[Any]:
        return list(self._rows)


def _issued_row(order: int, bids: list[str], fingerprint: str = "a" * 64) -> Any:
    return SimpleNamespace(window_order=order, block_ids_json=list(bids),
                           block_fingerprints_json=[fingerprint] * len(bids))


def _durable(rows: list[Any]) -> Any:
    operation = SimpleNamespace(id="op-1")
    source_plan = SimpleNamespace(id="sp-1")
    return _workflow_runtime._durable_issued_windows(
        operation=operation, source_plan=source_plan)


def test_issued_scopes_reject_order_gap(app, monkeypatch):
    rows = [_issued_row(1, ["b1"]), _issued_row(3, ["b2"])]
    monkeypatch.setattr(TranslationWindowAttempt, "query", _FakeQuery(rows))
    with pytest.raises(BlockContractError, match="block-window-issued-scope"):
        _durable(rows)


def test_issued_scopes_reject_overlap(app, monkeypatch):
    rows = [_issued_row(1, ["b1", "b2"]), _issued_row(2, ["b2", "b3"])]
    monkeypatch.setattr(TranslationWindowAttempt, "query", _FakeQuery(rows))
    with pytest.raises(BlockContractError, match="block-window-issued-scope"):
        _durable(rows)


def test_issued_scope_retries_must_be_identical(app, monkeypatch):
    same = [_issued_row(1, ["b1"]), _issued_row(1, ["b1"]), _issued_row(2, ["b2"])]
    monkeypatch.setattr(TranslationWindowAttempt, "query", _FakeQuery(same))
    windows, covered, max_order = _durable(same)
    assert [(item.order, item.block_ids) for item in windows] == [
        (1, ("b1",)), (2, ("b2",))]
    assert covered == frozenset({"b1", "b2"})
    assert max_order == 2
    conflict_order = [_issued_row(1, ["b1"]), _issued_row(2, ["b1"])]
    monkeypatch.setattr(TranslationWindowAttempt, "query", _FakeQuery(conflict_order))
    with pytest.raises(BlockContractError, match="block-window-issued-scope"):
        _durable(conflict_order)
    conflict_fp = [_issued_row(1, ["b1"], "a" * 64), _issued_row(1, ["b1"], "b" * 64)]
    monkeypatch.setattr(TranslationWindowAttempt, "query", _FakeQuery(conflict_fp))
    with pytest.raises(BlockContractError, match="block-window-issued-scope"):
        _durable(conflict_fp)


def test_packing_reuses_one_glossary_snapshot(app, monkeypatch):
    user, project, record, operation, plan = _state(
        "<html><body><p>Alpha one.</p><p>Bravo two.</p>"
        "<p>Charlie three.</p><p>Delta four.</p></body></html>",
        max_blocks=1, key="querycount")
    _propose(project, record, [
        {"source_term": "Alpha", "target_term": "Alfa", "sense": ""}], key="qc-term")
    entries_calls = 0
    real_entries = _working_glossary._entries

    def counting_entries(snapshot_record: Any) -> Any:
        nonlocal entries_calls
        entries_calls += 1
        return real_entries(snapshot_record)

    slot_calls = 0
    real_slot = _working_glossary.glossary_slot_for_window

    def counting_slot(*args: Any, **kwargs: Any) -> Any:
        nonlocal slot_calls
        slot_calls += 1
        return real_slot(*args, **kwargs)

    monkeypatch.setattr(_working_glossary, "_entries", counting_entries)
    monkeypatch.setattr(_working_glossary, "glossary_slot_for_window", counting_slot)
    _completed, result = _run(project, operation, _Provider())
    assert result["translated_block_count"] == len(plan.blocks)
    windows = db.session.query(TranslationWindowAttempt).filter_by(
        operation_id=operation.id).count()
    assert windows == len(plan.blocks)
    # Fresh operation, nothing issued: no stored-snapshot path at all, and
    # terminology reads stay O(windows): one pass snapshot + one refresh
    # per newly admitted window.
    assert slot_calls == 0
    assert entries_calls <= windows + 2
