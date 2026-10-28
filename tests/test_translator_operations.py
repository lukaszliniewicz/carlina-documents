from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from content_storage_support import bind_content_policy
from flask import Flask
from sqlalchemy.exc import IntegrityError

# Keep the old package-name references consolidated for the exact rename ratchet.
# isort: off
from iarf_assistant import extensions as app_extensions, models as app_models
from iarf_assistant.addons.translator import (
    models as translator_models,
    operations as operation_service,
)
from iarf_assistant.core import encryption as encryption_module, utc as utc_module
# isort: on

TranslationOperation = translator_models.TranslationOperation
TranslationProject = translator_models.TranslationProject
TranslationSegment = translator_models.TranslationSegment
TranslationSourcePlan = translator_models.TranslationSourcePlan
TranslationSourcePlanActive = translator_models.TranslationSourcePlanActive
TranslationOperationActiveConflict = (
    operation_service.TranslationOperationActiveConflict
)
TranslationOperationClaimConflict = operation_service.TranslationOperationClaimConflict
TranslationOperationError = operation_service.TranslationOperationError
TranslationOperationIdempotencyConflict = (
    operation_service.TranslationOperationIdempotencyConflict
)
TranslationOperationNotRunnable = operation_service.TranslationOperationNotRunnable
TranslationOperationTransitionConflict = (
    operation_service.TranslationOperationTransitionConflict
)
TranslationProjectIdempotencyConflict = (
    operation_service.TranslationProjectIdempotencyConflict
)
attach_queue_job = operation_service.attach_queue_job
canonical_request_fingerprint = operation_service.canonical_request_fingerprint
claim_operation = operation_service.claim_operation
create_operation = operation_service.create_operation
create_project_idempotently = operation_service.create_project_idempotently
expire_stale_operation = operation_service.expire_stale_operation
finish_operation = operation_service.finish_operation
renew_operation_lease = operation_service.renew_operation_lease
request_cancellation = operation_service.request_cancellation
serialize_operation = operation_service.serialize_operation
transition_operation = operation_service.transition_operation
update_progress = operation_service.update_progress
EncryptionService = encryption_module.EncryptionService
InstallationKeyStore = encryption_module.InstallationKeyStore
utc_now_db = utc_module.utc_now_db
db = app_extensions.db
User = app_models.User


@pytest.fixture
def app(tmp_path):
    application = Flask(__name__, instance_path=str(tmp_path / "instance"))
    bind_content_policy(application)
    application.config.update(
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{tmp_path / 'translator-operations.db'}",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SECRET_KEY="translator-operation-test",
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
        db.drop_all()
        db.engine.dispose()
        context.pop()


def _project(name: str = "Draft") -> TranslationProject:
    owner = User(email=f"{name.lower()}@example.test", password_hash="test")
    db.session.add(owner)
    db.session.flush()
    project = TranslationProject(
        user_id=owner.id,
        name=name,
        source_lang="en",
        target_lang="pl",
        source_snapshot="<p>Frozen source</p>",
        source_content_hash="a" * 64,
    )
    db.session.add(project)
    db.session.flush()
    return project


def _source_plan(project: TranslationProject) -> TranslationSourcePlan:
    """Persist the smallest valid plan row needed for operation pin tests."""

    source_hash = "c" * 64
    plan_hash = "d" * 64
    plan = TranslationSourcePlan(
        id=f"plan-{project.id}",
        project_id=project.id,
        contract_version="document-block-v1",
        source_document_id="document",
        source_language="en",
        language_profile="default",
        namespace="namespace",
        snapshot_contract_version="snapshot-v1",
        semantic_contract_version="semantic-html-v1",
        semantic_contract_mode="strict",
        semantic_content_hash=source_hash,
        source_content_hash=source_hash,
        snapshot_captured_at=utc_now_db(),
        planner_version="planner-v1",
        planner_settings_hash="e" * 64,
        plan_hash=plan_hash,
        source_attestation={},
        planner_settings={},
        coverage_witness={},
        block_count=0,
        anchor_count=0,
    )
    db.session.add(plan)
    db.session.flush()
    db.session.add(
        TranslationSourcePlanActive(
            project_id=project.id,
            source_plan_id=plan.id,
            version=1,
        )
    )
    db.session.flush()
    return plan


def _creation_candidate(owner_id: int, name: str = "Created") -> TranslationProject:
    return TranslationProject(
        user_id=owner_id,
        name=name,
        source_lang="en",
        target_lang="pl",
        status="setup",
    )


def test_canonical_fingerprint_is_order_independent():
    first = canonical_request_fingerprint({"b": 2, "a": ["x", 1]})
    second = canonical_request_fingerprint({"a": ["x", 1], "b": 2})
    assert first == second
    assert len(first) == 64


def test_project_creation_idempotency_reuses_exact_body_and_conflicts_on_change(app):
    owner = User(email="project-create@example.test", password_hash="test")
    db.session.add(owner)
    db.session.flush()
    payload = {
        "name": "  Created project  ",
        "source_lang": " en ",
        "target_lang": " pl ",
        "settings": {"enable_pre_analysis": True},
    }
    first, created = create_project_idempotently(
        _creation_candidate(owner.id, name="Created project"),
        idempotency_key=" create-key ",
        request_payload=payload,
    )
    assert created is True
    db.session.commit()
    second, reused = create_project_idempotently(
        _creation_candidate(owner.id, name="Created project"),
        idempotency_key="create-key",
        request_payload={
            "target_lang": "pl",
            "settings": {"enable_pre_analysis": True},
            "source_lang": "en",
            "name": "Created project",
        },
    )
    assert reused is False
    assert second.id == first.id
    with pytest.raises(TranslationProjectIdempotencyConflict) as conflict:
        create_project_idempotently(
            _creation_candidate(owner.id, name="Created project"),
            idempotency_key="create-key",
            request_payload={**payload, "target_lang": "de"},
        )
    assert conflict.value.project.id == first.id


def test_project_creation_without_key_remains_non_idempotent(app):
    owner = User(email="project-no-key@example.test", password_hash="test")
    db.session.add(owner)
    db.session.flush()
    first, first_created = create_project_idempotently(
        _creation_candidate(owner.id, name="No key"),
        idempotency_key=None,
        request_payload={"name": "No key"},
    )
    second, second_created = create_project_idempotently(
        _creation_candidate(owner.id, name="No key"),
        idempotency_key="",
        request_payload={"name": "No key"},
    )
    assert first_created is True
    assert second_created is True
    assert first.id != second.id
    assert first.creation_idempotency_key is None
    assert second.creation_request_fingerprint is None


def test_first_idempotent_project_savepoint_remains_rollbackable(app):
    owner = User(email="project-rollback@example.test", password_hash="test")
    db.session.add(owner)
    db.session.commit()
    owner_id = owner.id
    first, created = create_project_idempotently(
        _creation_candidate(owner_id),
        idempotency_key="rollback-key",
        request_payload={"name": "Created"},
    )
    assert created is True
    assert first.id
    db.session.rollback()
    db.session.remove()
    assert TranslationProject.query.filter_by(user_id=owner_id).count() == 0
    retried, created = create_project_idempotently(
        _creation_candidate(owner_id),
        idempotency_key="rollback-key",
        request_payload={"name": "Created"},
    )
    assert created is True
    db.session.commit()
    assert TranslationProject.query.filter_by(user_id=owner_id).one().id == retried.id


def test_project_creation_integrity_race_keeps_outer_work_and_reuses_existing(
    app, monkeypatch
):
    owner = User(email="project-race@example.test", password_hash="test")
    db.session.add(owner)
    db.session.flush()
    payload = {"name": "Race", "target_lang": "pl"}
    first, _ = create_project_idempotently(
        _creation_candidate(owner.id, name="Race"),
        idempotency_key="race-key",
        request_payload=payload,
    )
    db.session.commit()
    unrelated = User(email="project-race-unrelated@example.test", password_hash="test")
    db.session.add(unrelated)
    real_lookup = operation_service._project_for_creation_identity
    calls = 0

    def delayed_lookup(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return None
        return real_lookup(**kwargs)

    monkeypatch.setattr(
        operation_service, "_project_for_creation_identity", delayed_lookup
    )
    reused, created = create_project_idempotently(
        _creation_candidate(owner.id, name="Race"),
        idempotency_key="race-key",
        request_payload=payload,
    )
    assert created is False
    assert reused.id == first.id
    assert unrelated.id is not None
    db.session.commit()
    assert (
        User.query.filter_by(email="project-race-unrelated@example.test").one().id
        == unrelated.id
    )


def test_create_reuses_same_request_and_rejects_key_reuse(app):
    project = _project()
    operation, created = create_operation(
        project,
        kind="translate",
        request_payload={"model": "configured-model", "segment_ids": ["one"]},
        idempotency_key="request-1",
        source_content_hash="b" * 64,
        checkpoint={"next": 0, "private": "source text"},
    )
    assert created is True
    reused, reused_created = create_operation(
        project,
        kind="translate",
        request_payload={"segment_ids": ["one"], "model": "configured-model"},
        idempotency_key="request-1",
    )
    assert reused_created is False
    assert reused.id == operation.id
    with pytest.raises(TranslationOperationIdempotencyConflict):
        create_operation(
            project,
            kind="translate",
            request_payload={"model": "different-model"},
            idempotency_key="request-1",
        )


@pytest.mark.parametrize(
    ("label", "pins"),
    (
        ("plan-id-only", {"source_plan_id": "plan"}),
        ("plan-hash-only", {"source_plan_hash": "c" * 64}),
        (
            "content-and-plan-id",
            {"source_content_hash": "b" * 64, "source_plan_id": "plan"},
        ),
        (
            "content-and-plan-hash",
            {"source_content_hash": "b" * 64, "source_plan_hash": "c" * 64},
        ),
        (
            "plan-id-and-plan-hash",
            {"source_plan_id": "plan", "source_plan_hash": "c" * 64},
        ),
    ),
)
def test_create_rejects_every_partial_source_pin_shape(app, label, pins):
    project = _project(name=f"Partial-{label}")
    with pytest.raises(ValueError, match="source_content_hash is required"):
        create_operation(
            project,
            kind="document_block_translate",
            request_payload={"shape": label},
            idempotency_key=f"partial-{label}",
            **pins,
        )


def test_create_accepts_complete_source_pin_after_rehydrating_plan(app, monkeypatch):
    project = _project(name="Complete-pin")
    plan = _source_plan(project)
    calls: list[str] = []

    def rehydrate(record):
        calls.append(record.id)
        return SimpleNamespace(snapshot=SimpleNamespace(content_hash="c" * 64))

    monkeypatch.setattr(operation_service, "rehydrate_source_plan", rehydrate)
    operation, created = create_operation(
        project,
        kind="document_block_translate",
        request_payload={"source": "snapshot"},
        idempotency_key="complete-pin",
        source_content_hash="c" * 64,
        source_plan_id=plan.id,
        source_plan_hash=plan.plan_hash,
    )
    assert created is True
    assert calls == [plan.id]
    assert (
        operation.source_content_hash,
        operation.source_plan_id,
        operation.source_plan_hash,
    ) == ("c" * 64, plan.id, plan.plan_hash)


def test_operation_pin_shape_check_matches_legacy_and_complete_shapes(app):
    project = _project(name="Orm-pin-shape")
    plan = _source_plan(project)

    invalid_shapes = (
        {"source_plan_id": plan.id},
        {"source_plan_hash": plan.plan_hash},
        {"source_content_hash": "b" * 64, "source_plan_id": plan.id},
        {"source_content_hash": "b" * 64, "source_plan_hash": plan.plan_hash},
        {"source_plan_id": plan.id, "source_plan_hash": plan.plan_hash},
    )
    for index, pins in enumerate(invalid_shapes):
        with pytest.raises(IntegrityError), db.session.begin_nested():
            db.session.add(
                TranslationOperation(
                    project_id=project.id,
                    owner_user_id=project.user_id,
                    kind="document_block_translate",
                    idempotency_key=f"orm-partial-{index}",
                    request_fingerprint="f" * 64,
                    state="completed",
                    **pins,
                )
            )
            db.session.flush()

    legacy = TranslationOperation(
        project_id=project.id,
        owner_user_id=project.user_id,
        kind="document_block_translate",
        idempotency_key="orm-legacy",
        request_fingerprint="f" * 64,
        state="completed",
        source_content_hash="b" * 64,
    )
    db.session.add(legacy)
    db.session.flush()
    assert legacy.source_content_hash == "b" * 64

    complete = TranslationOperation(
        project_id=project.id,
        owner_user_id=project.user_id,
        kind="document_block_translate",
        idempotency_key="orm-complete",
        request_fingerprint="f" * 64,
        state="completed",
        source_content_hash="c" * 64,
        source_plan_id=plan.id,
        source_plan_hash=plan.plan_hash,
    )
    db.session.add(complete)
    db.session.flush()
    assert complete.source_plan_id == plan.id


def test_duplicate_integrity_error_isolated_to_savepoint_and_returns_idempotency_conflict(
    app, monkeypatch
):
    project = _project(name="Duplicate-race")
    existing, _ = create_operation(
        project,
        kind="translate",
        request_payload={"original": True},
        idempotency_key="race-key",
    )
    db.session.commit()
    unrelated = User(email="unrelated@example.test", password_hash="test")
    db.session.add(unrelated)

    real_lookup = operation_service._operation_for_identity
    calls = 0

    def delayed_lookup(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return None
        return real_lookup(**kwargs)

    monkeypatch.setattr(operation_service, "_operation_for_identity", delayed_lookup)
    monkeypatch.setattr(
        operation_service, "find_active_operation", lambda *args, **kwargs: None
    )
    with pytest.raises(TranslationOperationIdempotencyConflict) as caught:
        create_operation(
            project,
            kind="translate",
            request_payload={"original": False},
            idempotency_key="race-key",
        )
    assert caught.value.operation.id == existing.id
    assert unrelated.id is not None
    db.session.commit()
    assert User.query.filter_by(email="unrelated@example.test").one().id == unrelated.id


def test_active_integrity_race_isolated_to_savepoint_and_returns_active_conflict(
    app, monkeypatch
):
    project = _project(name="Active-race")
    existing, _ = create_operation(
        project,
        kind="translate",
        request_payload={"original": True},
        idempotency_key="active-key",
    )
    db.session.commit()
    unrelated = User(email="unrelated-active@example.test", password_hash="test")
    db.session.add(unrelated)

    real_lookup = operation_service._operation_for_identity
    lookup_calls = 0

    def delayed_lookup(**kwargs):
        nonlocal lookup_calls
        lookup_calls += 1
        if lookup_calls == 1:
            return None
        return real_lookup(**kwargs)

    real_active = operation_service.find_active_operation
    active_calls = 0

    def delayed_active(*args, **kwargs):
        nonlocal active_calls
        active_calls += 1
        if active_calls == 1:
            return None
        return real_active(*args, **kwargs)

    monkeypatch.setattr(operation_service, "_operation_for_identity", delayed_lookup)
    monkeypatch.setattr(operation_service, "find_active_operation", delayed_active)
    with pytest.raises(TranslationOperationActiveConflict) as caught:
        create_operation(
            project,
            kind="document_block_translate",
            request_payload={"original": False},
            idempotency_key="other-active-key",
        )
    assert caught.value.operation.id == existing.id
    assert unrelated.id is not None
    db.session.commit()
    assert (
        User.query.filter_by(email="unrelated-active@example.test").one().id
        == unrelated.id
    )


def test_single_flight_and_project_projection(app):
    project = _project()
    operation, _ = create_operation(
        project,
        kind="translate",
        request_payload={"source": project.source_content_hash},
        idempotency_key="first",
    )
    assert project.status == "translating"
    with pytest.raises(TranslationOperationActiveConflict):
        create_operation(
            project,
            kind="analyze_terminology",
            request_payload={"source": "another"},
            idempotency_key="different-kind",
        )
    operation = transition_operation(operation, "running", expected_version=1)
    assert operation.version == 2
    assert project.status == "translating"
    operation = finish_operation(operation, "completed", result={"translated": 1})
    assert operation.state == "completed"
    assert operation.result_json == {"translated": 1}
    assert project.status == "ready"
    second, created = create_operation(
        project,
        kind="translate",
        request_payload={"source": "another"},
        idempotency_key="second",
    )
    assert created is True
    assert second.id != operation.id


def test_active_project_partial_unique_index_rejects_distinct_kind_directly(app):
    project = _project()
    operation, _ = create_operation(
        project,
        kind="translate",
        request_payload={},
        idempotency_key="index-fence",
    )
    duplicate = TranslationOperation(
        project_id=project.id,
        owner_user_id=project.user_id,
        kind="document_block_translate",
        idempotency_key="other-kind",
        request_fingerprint="b" * 64,
    )
    db.session.add(duplicate)
    with pytest.raises(IntegrityError):
        db.session.flush()
    db.session.rollback()
    assert operation.state == "queued"


def test_claim_binds_fast_queue_delivery_and_reenters_same_delivery(app):
    project = _project()
    operation, _ = create_operation(
        project,
        kind="translate",
        request_payload={},
        idempotency_key="claim-fast",
    )
    claimed = claim_operation(
        operation.id,
        "job-fast",
        owner_user_id=project.user_id,
        project_id=project.id,
    )
    assert claimed.state == "running"
    assert claimed.queue_job_id == "job-fast"
    assert claimed.started_at is not None
    started_at = claimed.started_at
    version = claimed.version
    renewed = claim_operation(operation.id, "job-fast")
    assert renewed.state == "running"
    assert renewed.queue_job_id == "job-fast"
    assert renewed.started_at == started_at
    assert renewed.version == version + 1
    assert renewed.lease_expires_at > utc_now_db()


def test_claim_rejects_wrong_delivery_and_terminal_redelivery(app):
    project = _project()
    operation, _ = create_operation(
        project,
        kind="translate",
        request_payload={},
        idempotency_key="claim-bound",
    )
    operation = attach_queue_job(operation, "job-bound")
    with pytest.raises(TranslationOperationClaimConflict):
        claim_operation(operation.id, "wrong-job")
    claim_operation(operation.id, "job-bound")
    operation = finish_operation(operation, "completed")
    with pytest.raises(TranslationOperationNotRunnable):
        claim_operation(operation.id, "job-bound")


def test_claim_renew_and_terminal_transition_manage_worker_lease(app):
    project = _project(name="Lease-lifecycle")
    operation, _ = create_operation(
        project,
        kind="translate",
        request_payload={},
        idempotency_key="lease-lifecycle",
    )
    operation = claim_operation(operation.id, "lease-job")
    first_deadline = operation.lease_expires_at
    assert first_deadline is not None
    assert first_deadline > utc_now_db()
    operation = renew_operation_lease(operation, expected_version=operation.version)
    assert operation.lease_expires_at >= first_deadline
    renewed_version = operation.version
    operation = update_progress(
        operation,
        progress=25,
        expected_version=renewed_version,
    )
    assert operation.lease_expires_at is not None
    operation = finish_operation(operation, "completed")
    assert operation.lease_expires_at is None


def test_same_delivery_reclaims_an_expired_running_operation(app):
    project = _project(name="Lease-redelivery")
    operation, _ = create_operation(
        project,
        kind="translate",
        request_payload={},
        idempotency_key="lease-redelivery",
    )
    operation = claim_operation(operation.id, "redelivered-job")
    claimed_version = operation.version
    operation.lease_expires_at = utc_now_db() - timedelta(seconds=1)
    db.session.flush()

    recovered = claim_operation(operation.id, "redelivered-job")

    assert recovered.state == "running"
    assert recovered.version == claimed_version + 1
    assert recovered.lease_expires_at > utc_now_db()
    assert recovered.message == "Recovered after a worker lease loss."


def test_expired_running_lease_is_reaped_and_replaced(app):
    project = _project(name="Expired-running")
    operation, _ = create_operation(
        project,
        kind="translate",
        request_payload={"attempt": 1},
        idempotency_key="expired-running",
    )
    operation = claim_operation(operation.id, "expired-job")
    operation.lease_expires_at = utc_now_db() - timedelta(seconds=1)
    db.session.flush()
    expired = expire_stale_operation(operation)
    assert expired is not None
    assert expired.state == "failed"
    assert expired.error_code == "lease_expired"
    assert expired.lease_expires_at is None
    replacement, created = create_operation(
        project,
        kind="translate",
        request_payload={"attempt": 2},
        idempotency_key="replacement",
    )
    assert created is True
    assert replacement.state == "queued"


def test_stale_unbound_queue_publish_releases_singleflight_for_retry(app):
    project = _project(name="Lost-publish")
    operation, _ = create_operation(
        project,
        kind="translate",
        request_payload={"attempt": 1},
        idempotency_key="lost-publish",
    )
    operation.updated_at = (
        utc_now_db() - operation_service.QUEUED_DELIVERY_GRACE - timedelta(seconds=1)
    )
    db.session.flush()

    active = operation_service.reconcile_expired_active_operation(
        project.id, project.user_id
    )

    assert active is None
    assert operation.state == "failed"
    assert operation.error_code == "queue_delivery_lost"
    replacement, created = create_operation(
        project,
        kind="translate",
        request_payload={"attempt": 2},
        idempotency_key="new-browser-retry-key",
    )
    assert created is True
    assert replacement.state == "queued"


def test_fresh_bound_queue_delivery_keeps_singleflight_fence(app):
    project = _project(name="Fresh-bound-delivery")
    operation, _ = create_operation(
        project,
        kind="document_block_translate",
        request_payload={"attempt": 1},
        idempotency_key="fresh-bound-delivery",
    )
    operation = attach_queue_job(operation, "bound-job")

    active = operation_service.reconcile_expired_active_operation(
        project.id,
        project.user_id,
        now=operation.updated_at + operation_service.QUEUED_DELIVERY_GRACE,
    )

    assert active is not None
    assert active.id == operation.id
    assert active.state == "queued"


def test_stale_bound_queue_delivery_is_reaped_and_late_claim_is_harmless(app):
    project = _project(name="Lost-bound-delivery")
    operation, _ = create_operation(
        project,
        kind="document_block_translate",
        request_payload={"attempt": 1},
        idempotency_key="lost-bound-delivery",
    )
    operation = attach_queue_job(operation, "lost-job")
    operation.updated_at = (
        utc_now_db()
        - operation_service.QUEUED_BOUND_DELIVERY_GRACE
        - timedelta(seconds=1)
    )
    db.session.flush()

    active = operation_service.reconcile_expired_active_operation(
        project.id, project.user_id
    )

    assert active is None
    assert operation.state == "failed"
    assert operation.error_code == "queue_delivery_lost"
    replacement, created = create_operation(
        project,
        kind="document_block_translate",
        request_payload={"attempt": 2},
        idempotency_key="recovered-bound-delivery",
    )
    assert created is True
    assert replacement.state == "queued"
    with pytest.raises(TranslationOperationNotRunnable, match="already failed"):
        claim_operation(operation.id, "lost-job")


def test_expired_cancellation_becomes_cancelled_without_releasing_intent(app):
    project = _project(name="Expired-cancel")
    operation, _ = create_operation(
        project,
        kind="translate",
        request_payload={},
        idempotency_key="expired-cancel",
    )
    operation = claim_operation(operation.id, "cancel-job")
    operation = request_cancellation(operation)
    cancellation_time = operation.cancel_requested_at
    operation.lease_expires_at = utc_now_db() - timedelta(seconds=1)
    db.session.flush()
    expired = expire_stale_operation(operation)
    assert expired is not None
    assert expired.state == "cancelled"
    assert expired.cancel_requested_at == cancellation_time
    assert expired.lease_expires_at is None


def test_stale_expiry_version_loses_without_mutating_newer_lease_state(app):
    project = _project(name="Stale-expiry")
    operation, _ = create_operation(
        project,
        kind="translate",
        request_payload={},
        idempotency_key="stale-expiry",
    )
    operation = claim_operation(operation.id, "stale-job")
    stale_version = operation.version
    expired_at = utc_now_db() - timedelta(seconds=1)
    db.session.query(TranslationOperation).filter_by(id=operation.id).update(
        {
            TranslationOperation.version: stale_version + 1,
            TranslationOperation.lease_expires_at: expired_at,
        },
        synchronize_session=False,
    )
    db.session.flush()
    db.session.refresh(operation)
    with pytest.raises(TranslationOperationTransitionConflict):
        expire_stale_operation(operation, expected_version=stale_version)
    assert operation.state == "running"
    assert operation.lease_expires_at == expired_at


def test_transition_uses_version_fence_and_rejects_invalid_edges(app):
    project = _project()
    operation, _ = create_operation(
        project,
        kind="translate",
        request_payload={},
        idempotency_key="versioned",
    )
    with pytest.raises(TranslationOperationError, match="cannot transition"):
        transition_operation(operation, "completed")
    operation = transition_operation(operation, "running", expected_version=1)
    with pytest.raises(TranslationOperationTransitionConflict):
        update_progress(operation, progress=30, expected_version=1)
    operation = update_progress(operation, progress=30, message="Working")
    assert operation.progress == 30
    assert operation.version == 3


@pytest.mark.parametrize(
    ("kind", "expected_status"),
    [
        ("segment", "analyzing_structure"),
        ("analyze", "analyzing_terminology"),
        ("analyze_terminology", "analyzing_terminology"),
        ("translate", "translating"),
        ("translate_segment", "translating"),
        ("document_block_translate", "translating"),
    ],
)
def test_project_status_projection_is_kind_specific(app, kind, expected_status):
    project = _project(name=f"{kind}-project")
    operation, _ = create_operation(
        project,
        kind=kind,
        request_payload={"kind": kind},
        idempotency_key=f"{kind}-request",
    )
    assert project.status == expected_status
    operation = transition_operation(operation, "running")
    operation = finish_operation(operation, "completed")
    assert operation.state == "completed"
    assert project.status == "ready"


def test_queued_cancellation_records_intent_and_terminal_finish_is_idempotent(app):
    project = _project()
    operation, _ = create_operation(
        project,
        kind="translate",
        request_payload={},
        idempotency_key="queued-cancel",
    )
    operation = request_cancellation(operation)
    assert operation.state == "cancelled"
    assert operation.cancel_requested_at is not None
    assert operation.to_dict()["cancel_requested"] is True
    assert operation.to_dict()["cancel_requested_at"] is not None
    assert finish_operation(operation, "cancelled").id == operation.id


def test_cancellation_wins_against_late_worker_completion(app):
    project = _project()
    operation, _ = create_operation(
        project,
        kind="translate",
        request_payload={},
        idempotency_key="cancel-me",
    )
    operation = transition_operation(operation, "running")
    operation = request_cancellation(operation)
    assert operation.state == "cancel_requested"
    assert operation.cancel_requested_at is not None
    operation = finish_operation(
        operation,
        "completed",
        message="Completed stale work.",
        result={"should": "not publish"},
    )
    assert operation.state == "cancelled"
    assert operation.message == "Cancelled at a safe worker boundary."
    assert operation.result_json is None
    assert project.status == "cancelled"
    assert request_cancellation(operation).state == "cancelled"


def test_public_serializer_excludes_private_payloads(app):
    project = _project()
    operation, _ = create_operation(
        project,
        kind="translate",
        request_payload={"secret": "request"},
        idempotency_key="safe",
        checkpoint={"secret": "checkpoint"},
    )
    operation.error_detail = "provider response with source text"
    db.session.flush()
    public = serialize_operation(operation)
    assert public["id"] == operation.id
    assert "owner_user_id" not in public
    assert "request_fingerprint" not in public
    assert "idempotency_key" not in public
    assert "checkpoint_json" not in public
    assert "result_json" not in public
    assert "error_detail" not in public


def test_segment_version_defaults_and_is_serialized(app):
    project = _project()
    segment = TranslationSegment(
        project_id=project.id,
        order_index=0,
        block_index=0,
        html_tag="p",
        original_text="Hello",
    )
    db.session.add(segment)
    db.session.flush()
    assert segment.version == 1
    assert segment.to_dict()["version"] == 1
    invalid = TranslationSegment(
        project_id=project.id,
        order_index=1,
        block_index=0,
        html_tag="p",
        original_text="Invalid",
        version=0,
    )
    db.session.add(invalid)
    with pytest.raises(IntegrityError):
        db.session.flush()
    db.session.rollback()
