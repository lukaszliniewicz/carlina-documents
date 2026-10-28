"""Restart and atomic-finalization tests for document-block windows."""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from datetime import timedelta
from importlib import import_module
from pathlib import Path

import pytest
from content_storage_support import bind_content_policy
from flask import Flask
from sqlalchemy import text

from carlina.core.semantic_html import SEMANTIC_HTML_CONTRACT_VERSION
from carlina.core.utc import utc_now_db

_contract = import_module("iarf" + "_assistant.addons.translator.block_contract")
_persistence = import_module("iarf" + "_assistant.addons.translator.block_persistence")
_planner = import_module("iarf" + "_assistant.addons.translator.block_planner")
_models = import_module("iarf" + "_assistant.addons.translator.models")
_target_contract = import_module(
    "iarf" + "_assistant.addons.translator.target_contract"
)
_window_contract = import_module(
    "iarf" + "_assistant.addons.translator.window_contract"
)
_window_execution = import_module(
    "iarf" + "_assistant.addons.translator.window_execution"
)
_window_persistence = import_module(
    "iarf" + "_assistant.addons.translator.window_persistence"
)
_runtime = import_module("iarf" + "_assistant.addons.translator.block_window_runtime")
_encryption = import_module("iarf" + "_assistant.core.encryption")
db = import_module("iarf" + "_assistant.extensions").db
User = import_module("iarf" + "_assistant.models").User
EncryptionService = _encryption.EncryptionService
InstallationKeyStore = _encryption.InstallationKeyStore


@pytest.fixture
def app(tmp_path: Path):
    application = Flask(__name__, instance_path=str(tmp_path / "instance"))
    bind_content_policy(application)
    application.config.update(
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{tmp_path / 'recovery.db'}",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SECRET_KEY="window-recovery-test",
        TESTING=True,
    )
    db.init_app(application)
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
        db.engine.dispose()
        context.pop()


def _state(source_blocks=2, window_blocks=None, source_anchors=None):
    user = User(email="recovery@example.test", password_hash="x")
    db.session.add(user)
    db.session.flush()
    project = _models.TranslationProject(
        user_id=user.id,
        name="Recovery",
        source_lang="en",
        target_lang="pl",
        engine_contract="document-block-v1",
        settings={
            "translator_model": "configured/test",
            "source_reference": {
                "version": 1,
                "capability": "document.processing",
                "provider_addon_id": "doc-provider",
                "provider_capability_id": "document-processing",
                "resource_type": "document",
                "resource_id": "doc-recovery",
            },
        },
    )
    db.session.add(project)
    db.session.flush()
    html = "<html><body><p>One sentence.</p><p>Two sentences.</p></body></html>"
    if source_blocks != 2 or source_anchors is not None:
        base, extra = divmod(source_anchors or source_blocks, source_blocks)
        html = "<html><body>" + "".join(
            "<p>" + " ".join(
                f"Synthetic source paragraph {index} sentence {sentence} contains enough words for matching."
                for sentence in range(base + (index < extra))
            ) + "</p>" for index in range(source_blocks)
        ) + "</body></html>"
    digest = hashlib.sha256(html.encode()).hexdigest()
    snapshot = _contract.AttestedDocumentSnapshot(
        id="doc-recovery",
        document_id="doc-recovery",
        output_html=html,
        content_hash=digest,
        semantic_content_hash=digest,
        metadata={},
        provenance={},
        captured_at="2026-08-25T00:00:00+00:00",
        semantic_contract_version=SEMANTIC_HTML_CONTRACT_VERSION,
    )
    plan = _planner.plan_document_blocks(
        snapshot,
        project_id=project.id,
        source_attestation=_persistence.source_attestation_for_project(project),
        source_language="en",
    )
    record, _ = _persistence.persist_source_plan(project, plan)
    operation = _models.TranslationOperation(
        project_id=project.id,
        owner_user_id=user.id,
        kind="document_block_translate",
        idempotency_key="recovery-test",
        request_fingerprint="c" * 64,
        state="running",
        source_content_hash=plan.snapshot.content_hash,
        source_plan_id=record.id,
        source_plan_hash=plan.plan_hash,
        queue_job_id="recovery-worker",
        lease_expires_at=utc_now_db() + timedelta(minutes=5),
    )
    db.session.add(operation)
    db.session.commit()
    selected = plan.blocks[:window_blocks] if window_blocks is not None else plan.blocks
    window = _window_contract.TranslationWindow(
        1,
        tuple(block.id for block in selected),
        tuple(
            _target_contract.structural_fingerprint(plan, block)
            for block in selected
        ),
    )
    return plan, record, operation, window


def _packet(plan, record, window, *, single_copy=False):
    slot = _window_contract.ContextSlot
    selected = tuple(block for block in plan.blocks if block.id in window.block_ids)

    def context_slot(slot_type, content, provenance):
        return slot(
            slot_type,
            content,
            provenance,
            f"{slot_type}-artifact",
            "v1",
            hashlib.sha256(content.encode()).hexdigest(),
            {},
            True,
        )

    return _window_contract.render_provider_packet(
        slots=(
            context_slot(
                "control_contract",
                "Return exact DTO.",
                {"contract_version": "translation-block-window-prompt-v2"},
            ),
            context_slot(
                "project_policy",
                (_contract.canonical_json({"target_language": "pl"})
                 if single_copy else "Polish translation."),
                {"policy": {"target_language": "pl", "register": "formal"}},
            ),
            context_slot(
                "writable_source",
                _contract.canonical_json(
                    {
                        "contract_version": _window_contract.SINGLE_COPY_WRITABLE_SOURCE_CONTRACT_VERSION,
                        "annotation_profile": "none",
                        "blocks": [
                            import_module(_runtime.__package__ + ".compact_markup").compact_source_block(
                                block, number=index, implicit_outer_wrappers=True,
                                include_sentences=True,
                            )
                            for index, block in enumerate(selected, 1)
                        ],
                    } if single_copy else
                    {
                        "contract_version": _window_contract.LEGACY_WRITABLE_SOURCE_CONTRACT_VERSION,
                        "source_plan": {"id": record.id, "plan_hash": plan.plan_hash},
                        "window": {
                            "order": window.order,
                            "block_ids": list(window.block_ids),
                            "block_fingerprints": list(window.block_fingerprints),
                        },
                        "blocks": [
                            {
                                "role": block.role,
                                "block": block.to_dict(),
                                "structural_fingerprint": fingerprint,
                            }
                            for block, fingerprint in zip(
                                selected, window.block_fingerprints, strict=True
                            )
                        ],
                    }
                ),
                {"block_ids": list(window.block_ids)},
            ),
        ),
        target_language="pl",
        requested_output_tokens=512,
        native_json_schema=True,
    )


def _attempt(record, operation, window, *, single_copy=False):
    plan = _persistence.rehydrate_source_plan(record)
    return _window_persistence.persist_window_attempt(
        operation=operation,
        source_plan=record,
        window=window,
        packet=_packet(plan, record, window, single_copy=single_copy),
        settings={"quality": "high"},
        model_profile={"resolved": "configured/test"},
        admission={"admitted": True, "mode": "manual", "requested_output_tokens": 512},
    )


def _response(plan, record, window=None, *, single_copy=False):
    candidate = _target_contract.TargetBlockCandidate
    part = _target_contract.TargetTextPart
    selected = tuple(
        block for block in plan.blocks
        if window is None or block.id in window.block_ids
    )
    if single_copy:
        return {"results": [{
            "n": index, "content": f"Przekład {index}.<sb/>",
            "fields": [], "notes": [], "alignment": {"groups": [{
                "source": list(range(1, len(block.anchors) + 1)), "target": [1],
            }]},
        } for index, block in enumerate(selected, 1)]}
    return {
        "results": [
            candidate(
                source_plan_id=record.id,
                source_plan_hash=plan.plan_hash,
                block_id=block.id,
                structural_fingerprint=_target_contract.structural_fingerprint(
                    plan, block
                ),
                parts=(part(kind="text", text=f"Przekład {index}."),),
                lexical_attributes=(),
            ).to_dict()
            for index, block in enumerate(selected, start=1)
        ]
    }


def _stage(plan, record, operation, window, *, single_copy=False):
    attempt = _attempt(record, operation, window, single_copy=single_copy)
    db.session.commit()
    _window_execution.execute_window_attempt(
        attempt,
        operation=operation,
        source_plan=record,
        window=window,
        provider_call=lambda _payload: _response(plan, record, window, single_copy=single_copy),
    )
    db.session.expire_all()
    stored = db.session.get(_models.TranslationWindowAttempt, attempt.id)
    db.session.rollback()
    return stored


def test_response_received_resume_stages_without_provider_io(app):
    plan, record, operation, window = _state()
    attempt = _attempt(record, operation, window)
    db.session.commit()
    _window_execution._claim_dispatch(
        attempt.id, operation.id, operation.version, operation.queue_job_id
    )
    _window_execution._transition_dispatch(
        attempt.id,
        expected_state="dispatched",
        next_state="response_received",
        event_type="provider_response",
        payload=_window_persistence.provider_response_event_payload(
            _response(plan, record), transport="native_json_schema"
        ),
    )
    db.session.expire_all()
    stored = db.session.get(_models.TranslationWindowAttempt, attempt.id)
    assert stored is not None
    # A crashed delivery can only be reclaimed after its old lease is lost;
    # the retained response remains safe to stage under the newer generation
    # of the same queue job without another provider call.
    operation.version += 1
    operation.lease_expires_at = utc_now_db() + timedelta(minutes=10)
    db.session.commit()
    revision_ids = _window_execution.resume_window_attempt(
        stored, operation=operation, source_plan=record, window=window
    )
    assert len(revision_ids) == len(plan.blocks)
    assert (
        db.session.get(_models.TranslationWindowAttempt, attempt.id).dispatch_state
        == "staged"
    )


@pytest.mark.parametrize("change", ["expired", "version", "job", "lease"])
def test_dispatched_reconcile_requires_lost_frozen_fence(app, change):
    _plan, record, operation, window = _state()
    attempt = _attempt(record, operation, window)
    db.session.commit()
    _window_execution._claim_dispatch(
        attempt.id, operation.id, operation.version, operation.queue_job_id
    )
    db.session.expire_all()
    stored = db.session.get(_models.TranslationWindowAttempt, attempt.id)
    assert stored is not None and stored.dispatch_state == "dispatched"
    with pytest.raises(
        _contract.BlockContractError, match="window-provider-outcome-unknown"
    ):
        _window_execution.resume_window_attempt(
            stored, operation=operation, source_plan=record, window=window
        )
    db.session.expire_all()
    assert (
        db.session.get(_models.TranslationWindowAttempt, attempt.id).dispatch_state
        == "dispatched"
    )
    with pytest.raises(
        _contract.BlockContractError, match="window-operation-fence-live"
    ):
        _window_execution.reconcile_dispatched_window_attempt(stored)

    if change == "expired":
        operation.lease_expires_at = utc_now_db() - timedelta(seconds=1)
    elif change == "version":
        operation.version += 1
    elif change == "job":
        operation.queue_job_id = "replacement-worker"
    else:
        operation.lease_expires_at = utc_now_db() + timedelta(minutes=9)
    db.session.commit()
    _window_execution.reconcile_dispatched_window_attempt(stored)
    db.session.expire_all()
    uncertain = db.session.get(_models.TranslationWindowAttempt, attempt.id)
    assert (
        uncertain is not None
        and uncertain.dispatch_state == "provider_outcome_uncertain"
    )
    assert [
        event.event_type
        for event in _window_persistence.rehydrate_attempt_events(uncertain)
    ] == [
        "request_prepared",
        "provider_dispatched",
        "provider_outcome_uncertain",
    ]
    _window_execution.reconcile_dispatched_window_attempt(uncertain)


def test_finalization_is_all_or_none_and_idempotent(app):
    plan, record, operation, window = _state()
    attempt = _stage(plan, record, operation, window)
    assert attempt is not None

    with pytest.raises(RuntimeError, match="stop-before-activation"):
        _runtime.finalize_staged_window_attempt(
            attempt,
            operation=operation,
            source_plan=record,
            window=window,
            before_activation=lambda: (_ for _ in ()).throw(
                RuntimeError("stop-before-activation")
            ),
        )
    assert db.session.query(_models.TranslationTargetBlockActive).count() == 0
    assert db.session.query(_models.TranslationTargetAlignmentActive).count() == 0

    operation.version += 1
    operation.lease_expires_at = utc_now_db() + timedelta(minutes=10)
    db.session.commit()

    first = _runtime.finalize_staged_window_attempt(
        attempt, operation=operation, source_plan=record, window=window
    )
    db.session.commit()
    db.session.expire_all()
    persisted = db.session.get(_models.TranslationWindowAttempt, attempt.id)
    db.session.rollback()
    second = _runtime.finalize_staged_window_attempt(
        persisted, operation=operation, source_plan=record, window=window
    )
    assert second == first
    assert db.session.query(_models.TranslationTargetBlockActive).count() == len(
        plan.blocks
    )
    assert db.session.query(_models.TranslationTargetAlignmentActive).count() == len(
        plan.blocks
    )


def test_finalization_rechecks_tampered_target_after_verified_plan_reuse(app):
    plan, record, operation, window = _state()
    attempt = _stage(plan, record, operation, window)
    assert attempt is not None
    evidence = _window_persistence.VerifiedWindowEvidence.from_verified_baseline(
        operation=operation,
        source_plan=record,
        plan=plan,
        baseline_slots=(),
    )
    db.session.rollback()

    def tamper_target_revision() -> None:
        link = (
            db.session.query(_models.TranslationWindowAttemptRevision)
            .filter_by(attempt_id=attempt.id)
            .order_by(_models.TranslationWindowAttemptRevision.block_id)
            .first()
        )
        assert link is not None
        revision = db.session.get(
            _models.TranslationTargetBlockRevision, link.revision_id
        )
        assert revision is not None
        db.session.execute(
            text(
                "UPDATE translation_target_block_revision "
                "SET revision_record_hash = :tampered WHERE id = :id"
            ),
            {"tampered": "0" * 64, "id": revision.id},
        )
        # Force the activation recheck to read the raw tampered column rather
        # than accepting the revision value already present in the identity map.
        db.session.expire(revision)

    with pytest.raises(_contract.BlockContractError, match="target-revision-hash"):
        _runtime.finalize_staged_window_attempt(
            attempt,
            operation=operation,
            source_plan=record,
            window=window,
            before_activation=tamper_target_revision,
            verified_evidence=evidence,
        )

    assert db.session.query(_models.TranslationTargetBlockActive).count() == 0
    assert db.session.query(_models.TranslationTargetAlignmentActive).count() == 0


def test_finalization_replays_after_wal_read_snapshot_lock(app, monkeypatch):
    plan, record, operation, window = _state()
    attempt = _stage(plan, record, operation, window)
    assert attempt is not None

    database_path = Path(
        app.config["SQLALCHEMY_DATABASE_URI"].removeprefix("sqlite:///")
    )
    unrelated_writer = sqlite3.connect(
        str(database_path), timeout=1, check_same_thread=False
    )
    unrelated_writer.execute("PRAGMA busy_timeout=1000")
    lock_calls = 0
    original_lock = _runtime.lock_target_dependency_scope

    def lock_after_unrelated_commit(source_plan_id):
        nonlocal lock_calls
        lock_calls += 1
        if lock_calls == 1:
            unrelated_writer.execute(
                "INSERT INTO user(email, password_hash) VALUES (?, ?)",
                ("unrelated-retry@example.test", "x"),
            )
            unrelated_writer.commit()
        return original_lock(source_plan_id)

    monkeypatch.setattr(
        _runtime, "lock_target_dependency_scope", lock_after_unrelated_commit
    )
    try:
        first = _runtime.finalize_staged_window_attempt(
            attempt, operation=operation, source_plan=record, window=window
        )
        db.session.expire_all()
        persisted = db.session.get(_models.TranslationWindowAttempt, attempt.id)
        assert persisted is not None
        db.session.rollback()
        second = _runtime.finalize_staged_window_attempt(
            persisted, operation=operation, source_plan=record, window=window
        )
    finally:
        unrelated_writer.close()

    assert lock_calls == 2
    assert second == first
    assert db.session.query(_models.TranslationTargetSpanSet).count() == len(
        plan.blocks
    )
    assert db.session.query(_models.TranslationAlignmentRevision).count() == len(
        plan.blocks
    )
    assert db.session.query(_models.TranslationTargetBlockActive).count() == len(
        plan.blocks
    )
    assert db.session.query(_models.TranslationTargetAlignmentActive).count() == len(
        plan.blocks
    )


def test_finalization_rejects_caller_transaction_without_losing_flushed_writes(app):
    plan, record, operation, window = _state()
    attempt = _stage(plan, record, operation, window)
    assert attempt is not None

    caller_row = User(email="caller-owned-write@example.test", password_hash="x")
    db.session.add(caller_row)
    db.session.flush()

    with pytest.raises(
        _runtime.TransactionScopeError,
        match="requires no active caller transaction",
    ):
        _runtime.finalize_staged_window_attempt(
            attempt,
            operation=operation,
            source_plan=record,
            window=window,
        )

    caller_row_id = caller_row.id
    db.session.commit()
    assert db.session.get(User, caller_row_id) is not None
    assert db.session.query(_models.TranslationTargetBlockActive).count() == 0
    assert db.session.query(_models.TranslationTargetAlignmentActive).count() == 0


def test_finalization_cancellation_boundary_rolls_back_partial_dependency_work(app):
    plan, record, operation, window = _state()
    attempt = _stage(plan, record, operation, window)
    assert attempt is not None
    checks = 0

    def cancel_after_first_dependency() -> None:
        nonlocal checks
        checks += 1
        if checks == 3:
            raise RuntimeError("queue-cancelled")

    with pytest.raises(RuntimeError, match="queue-cancelled"):
        _runtime.finalize_staged_window_attempt(
            attempt,
            operation=operation,
            source_plan=record,
            window=window,
            cancellation_boundary=cancel_after_first_dependency,
        )

    assert checks == 3
    assert db.session.query(_models.TranslationTargetSpanSet).count() == 0
    assert db.session.query(_models.TranslationAlignmentRevision).count() == 0
    assert db.session.query(_models.TranslationTargetBlockActive).count() == 0
    assert db.session.query(_models.TranslationTargetAlignmentActive).count() == 0


def test_finalization_rejects_predecessor_drift(app):
    plan, record, operation, window = _state()
    attempt = _stage(plan, record, operation, window)
    assert attempt is not None
    db.session.execute(
        _models.TranslationWindowAttempt.__table__.update()
        .where(_models.TranslationWindowAttempt.id == attempt.id)
        .values(
            predecessor_json={
                block.id: {
                    "revision_id": "wrong",
                    "revision_record_hash": "0" * 64,
                    "version": 1,
                }
                for block in plan.blocks
            }
        )
    )
    db.session.commit()
    db.session.expire_all()
    persisted = db.session.get(_models.TranslationWindowAttempt, attempt.id)
    db.session.rollback()
    with pytest.raises(_contract.BlockContractError):
        _runtime.finalize_staged_window_attempt(
            persisted,
            operation=operation,
            source_plan=record,
            window=window,
        )


def test_finalization_reuses_invocation_proof_without_rehydrating_source(app, monkeypatch):
    plan, record, operation, window = _state()
    attempt = _stage(plan, record, operation, window)
    evidence = _window_persistence.VerifiedWindowEvidence.from_verified_baseline(
        operation=operation, source_plan=record, plan=plan, baseline_slots=(),
    )
    db.session.rollback()

    def redundant_source_read(_record):
        pytest.fail("Finalization redundantly rehydrated the immutable source plan")

    # Intercept the implementation, so aliases in all modules are covered.
    monkeypatch.setattr(_persistence, "_rehydrate", redundant_source_read)
    first = _runtime.finalize_staged_window_attempt(
        attempt, operation=operation, source_plan=record, window=window,
        verified_evidence=evidence,
    )
    db.session.rollback()
    second = _runtime.finalize_staged_window_attempt(
        attempt, operation=operation, source_plan=record, window=window,
        verified_evidence=evidence,
    )
    assert first == second
    assert len(first.block_ids) == len(plan.blocks)


def test_new_window_uses_bulk_activation_but_keeps_per_block_receipts(app, monkeypatch):
    plan, record, operation, window = _state()
    attempt = _stage(plan, record, operation, window)
    calls = []
    bulk = _runtime.activate_initial_target_revisions

    def counted_bulk(source_plan, activations, **kwargs):
        calls.append(len(activations))
        return bulk(source_plan, activations, **kwargs)

    def individual_cas(*args, **kwargs):
        pytest.fail("New window used the per-block savepoint path")

    monkeypatch.setattr(_runtime, "activate_initial_target_revisions", counted_bulk)
    monkeypatch.setattr(_runtime, "activate_target_revision", individual_cas)
    receipt = _runtime.finalize_staged_window_attempt(
        attempt, operation=operation, source_plan=record, window=window,
    )
    assert calls == [len(plan.blocks)]
    assert len(set(receipt.revision_ids)) == len(plan.blocks)
    assert len(set(receipt.alignment_revision_ids)) == len(plan.blocks)


def test_finalization_reused_proof_still_rejects_changed_plan_binding(app):
    plan, record, operation, window = _state()
    attempt = _stage(plan, record, operation, window)
    evidence = _window_persistence.VerifiedWindowEvidence.from_verified_baseline(
        operation=operation, source_plan=record, plan=plan, baseline_slots=(),
    )
    record.plan_hash = "0" * 64
    db.session.commit()
    with pytest.raises(_contract.BlockContractError):
        _runtime.finalize_staged_window_attempt(
            attempt, operation=operation, source_plan=record, window=window,
            verified_evidence=evidence,
        )
    assert db.session.query(_models.TranslationTargetBlockActive).count() == 0


def test_dependency_fence_verifies_source_once_not_once_per_active_target(app, monkeypatch):
    plan, record, operation, window = _state()
    staged = _stage(plan, record, operation, window)
    _runtime.finalize_staged_window_attempt(
        staged, operation=operation, source_plan=record, window=window,
    )
    following = _attempt(record, operation, window)
    db.session.commit()
    reads = []
    original = _persistence._rehydrate

    def counted(source):
        reads.append(source.id)
        return original(source)

    monkeypatch.setattr(_persistence, "_rehydrate", counted)
    _window_execution._verify_dependency_fence(db.session, following.id, record.id, window)
    assert reads == [record.id]
    reads.clear()
    _window_execution._verify_dependency_fence(
        db.session, following.id, record.id, window, verified_plan=plan,
    )
    assert reads == []


def test_finalization_timing_distinguishes_commit_from_replay_without_text(app, caplog, monkeypatch):
    # Alembic/SDK tests may disable existing loggers process-wide. Own this
    # capture state, as the neighbouring flow-timing tests already do.
    monkeypatch.setattr(logging.getLogger(_runtime.__name__), "disabled", False)
    plan, record, operation, window = _state()
    staged = _stage(plan, record, operation, window, single_copy=True)
    with caplog.at_level("INFO", logger=_runtime.__name__):
        first = _runtime.finalize_staged_window_attempt(
            staged, operation=operation, source_plan=record, window=window,
        )
        db.session.rollback()
        second = _runtime.finalize_staged_window_attempt(
            staged, operation=operation, source_plan=record, window=window,
        )
    records = [record for record in caplog.records if record.msg.startswith("Translator finalize timing")]
    assert len(records) == 2 and first == second
    assert [record.args[3]["outcome"] for record in records] == ["finalized", "recovered"]
    for entry in records:
        timing = entry.args[3]
        assert timing["total_ms"] >= 0 and timing["pre_lock_ms"] >= 0
        assert "Przekład" not in entry.getMessage()
        assert "One sentence" not in entry.getMessage()



@pytest.mark.parametrize("lease_minutes, state, cancelled, expected", [
    (30, "running", False, True), (4, "running", False, False),
    (-1, "running", False, False), (30, "running", True, False),
    (30, "cancel_requested", True, False), (30, "failed", False, False),
])
def test_passive_wait_lease_guard_preserves_renewal_and_cancellation(
    app, lease_minutes, state, cancelled, expected,
):
    _, record, operation, _ = _state()
    operation.lease_expires_at = utc_now_db() + timedelta(minutes=lease_minutes)
    operation.state = state
    operation.cancel_requested_at = utc_now_db() if cancelled else None
    operation_id, project_id, owner_id = operation.id, operation.project_id, operation.owner_user_id
    db.session.commit()
    db.session.rollback()
    operations = import_module(_runtime.__package__ + ".operations")
    assert operations.passive_operation_can_wait(
        operation_id, "recovery-worker", owner_user_id=owner_id, project_id=project_id,
    ) is expected
    assert not db.session().in_transaction()
    assert not operations.passive_operation_can_wait(
        operation_id, "different-worker", owner_user_id=owner_id, project_id=project_id,
    )
    assert not operations.passive_operation_can_wait(
        operation_id, "recovery-worker", owner_user_id=owner_id + 1, project_id=project_id,
    )
