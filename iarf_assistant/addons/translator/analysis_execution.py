"""Injected, no-retry runner for durable Phase-5 analysis attempts."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from sqlalchemy import exists, inspect
from sqlalchemy.orm import sessionmaker

from carlina.core.utc import utc_now_db
from iarf_assistant.extensions import db

from .analysis_attempts import (
    append_analysis_attempt_event,
    canonical_analysis_value,
    persist_analysis_attempt,
    rehydrate_analysis_attempt,
    rehydrate_analysis_attempt_events,
    validate_analysis_response_extension,
)
from .block_contract import (
    BlockContractError,
)
from .block_contract import (
    TranslationSourcePlan as CanonicalSourcePlan,
)
from .models import TranslationAnalysisAttempt, TranslationOperation
from .operations import (
    TranslationOperationCancellation,
    TranslationOperationNotRunnable,
    TranslationOperationTransitionConflict,
)


def _independent_session():
    return sessionmaker(bind=db.engine, expire_on_commit=False)()


def _attempt_identifier(attempt: TranslationAnalysisAttempt) -> str:
    """Read an attempt identity without refreshing an expired caller object."""

    state = inspect(attempt)
    if state is None:
        raise BlockContractError("analysis-attempt-id")
    identity = state.identity
    if identity and isinstance(identity[0], str) and identity[0]:
        return identity[0]
    value = state.dict.get("id")
    if isinstance(value, str) and value:
        return value
    raise BlockContractError("analysis-attempt-id")


def prepare_and_commit_analysis_attempt(**kwargs: Any) -> TranslationAnalysisAttempt:
    """Persist the immutable packet before a caller obtains a runnable attempt."""

    attempt = persist_analysis_attempt(**kwargs)
    db.session.commit()
    return attempt


def _fence_reason(
    attempt: TranslationAnalysisAttempt, operation: TranslationOperation | None
) -> str | None:
    now = utc_now_db()
    if (
        operation is None
        or operation.id != attempt.operation_id
        or operation.project_id != attempt.project_id
        or operation.owner_user_id != attempt.owner_user_id
    ):
        return "operation"
    if (
        operation.state in {"cancel_requested", "cancelled"}
        or operation.cancel_requested_at is not None
    ):
        return "cancelled"
    if (
        operation.state != "running"
        or operation.version != attempt.operation_version
        or operation.queue_job_id != attempt.queue_job_id
        or operation.lease_expires_at is None
        or operation.lease_expires_at != attempt.operation_lease_expires_at
        or operation.lease_expires_at <= now
        or operation.source_content_hash != attempt.source_content_hash
        or operation.source_plan_id != attempt.source_plan_id
        or operation.source_plan_hash != attempt.source_plan_hash
    ):
        return "fence"
    return None


def _live_operation_fence(attempt: TranslationAnalysisAttempt, now: Any) -> Any:
    """Correlated EXISTS predicate for the final response fence.

    The predicate intentionally repeats every mutable operation claim field;
    a preflight read is not sufficient because cancellation, lease expiry, or
    source-pin replacement may occur before the terminal attempt CAS.
    """

    return exists().where(
        TranslationOperation.id == attempt.operation_id,
        TranslationOperation.project_id == attempt.project_id,
        TranslationOperation.owner_user_id == attempt.owner_user_id,
        TranslationOperation.state == "running",
        TranslationOperation.version == attempt.operation_version,
        TranslationOperation.queue_job_id == attempt.queue_job_id,
        TranslationOperation.cancel_requested_at.is_(None),
        TranslationOperation.lease_expires_at == attempt.operation_lease_expires_at,
        TranslationOperation.lease_expires_at > now,
        TranslationOperation.source_content_hash == attempt.source_content_hash,
        TranslationOperation.source_plan_id == attempt.source_plan_id,
        TranslationOperation.source_plan_hash == attempt.source_plan_hash,
    )


def _fresh_fence(attempt_id: str) -> None:
    session = _independent_session()
    try:
        attempt = session.get(TranslationAnalysisAttempt, attempt_id)
        operation = (
            session.get(TranslationOperation, attempt.operation_id) if attempt else None
        )
        if attempt is None:
            raise BlockContractError("analysis-attempt-uncommitted")
        reason = _fence_reason(attempt, operation)
        if reason == "cancelled":
            raise TranslationOperationCancellation(
                "Translator operation cancellation was observed at a safe boundary."
            )
        if reason:
            if operation is None:
                raise BlockContractError("analysis-attempt-operation")
            if operation is not None and operation.version != attempt.operation_version:
                raise TranslationOperationTransitionConflict(
                    operation.id, attempt.operation_version
                )
            raise TranslationOperationNotRunnable(
                operation, "Translation analysis no longer has a live worker claim."
            )
    finally:
        session.close()


def _transition(
    attempt_id: str,
    *,
    expected_state: str,
    next_state: str,
    event_type: str,
    payload: Mapping[str, Any],
    require_lost_fence: bool = False,
) -> None:
    session = _independent_session()
    try:
        attempt = session.get(TranslationAnalysisAttempt, attempt_id)
        if attempt is None or attempt.dispatch_state != expected_state:
            raise BlockContractError("analysis-dispatch-replay")
        values: dict[Any, Any] = {
            TranslationAnalysisAttempt.dispatch_state: next_state,
            TranslationAnalysisAttempt.dispatch_version: attempt.dispatch_version + 1,
        }
        if next_state in {
            "completed",
            "provider_failed",
            "provider_outcome_uncertain",
            "fence_failed",
            "cancelled",
            "failed",
        }:
            values[TranslationAnalysisAttempt.dispatch_finished_at] = utc_now_db()
        query = session.query(TranslationAnalysisAttempt).filter(
            TranslationAnalysisAttempt.id == attempt_id,
            TranslationAnalysisAttempt.dispatch_state == expected_state,
            TranslationAnalysisAttempt.dispatch_version == attempt.dispatch_version,
        )
        if expected_state == "response_received" and next_state == "completed":
            query = query.filter(_live_operation_fence(attempt, utc_now_db()))
        elif require_lost_fence:
            query = query.filter(~_live_operation_fence(attempt, utc_now_db()))
        changed = query.update(values, synchronize_session=False)
        if changed != 1:
            if (
                expected_state == "response_received" and next_state == "completed"
            ) or require_lost_fence:
                raise BlockContractError("analysis-dispatch-fence")
            raise BlockContractError("analysis-dispatch-replay")
        session.expire_all()
        current = session.get(TranslationAnalysisAttempt, attempt_id)
        if current is None:
            raise BlockContractError("analysis-dispatch-replay")
        append_analysis_attempt_event(current, event_type, payload, session=session)
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _terminal_after_fence(
    attempt_id: str, expected_state: str, error: Exception
) -> None:
    if isinstance(error, TranslationOperationCancellation):
        _transition(
            attempt_id,
            expected_state=expected_state,
            next_state="cancelled",
            event_type="cancelled",
            payload={"code": "cancelled"},
        )
    else:
        _transition(
            attempt_id,
            expected_state=expected_state,
            next_state="fence_failed",
            event_type="post_response_fence_failed",
            payload={"code": type(error).__name__},
        )


def _claim_dispatch(
    attempt_id: str,
    before_compare_and_set: Callable[[], None] | None = None,
) -> None:
    """Atomically claim exactly one prepared attempt while the operation fence holds."""

    session = _independent_session()
    try:
        attempt = session.get(TranslationAnalysisAttempt, attempt_id)
        if attempt is None:
            raise BlockContractError("analysis-attempt-uncommitted")
        if before_compare_and_set is not None:
            before_compare_and_set()
        now = utc_now_db()
        live_operation = exists().where(
            TranslationOperation.id == attempt.operation_id,
            TranslationOperation.project_id == attempt.project_id,
            TranslationOperation.owner_user_id == attempt.owner_user_id,
            TranslationOperation.state == "running",
            TranslationOperation.version == attempt.operation_version,
            TranslationOperation.queue_job_id == attempt.queue_job_id,
            TranslationOperation.cancel_requested_at.is_(None),
            TranslationOperation.lease_expires_at == attempt.operation_lease_expires_at,
            TranslationOperation.lease_expires_at > now,
            TranslationOperation.source_content_hash == attempt.source_content_hash,
            TranslationOperation.source_plan_id == attempt.source_plan_id,
            TranslationOperation.source_plan_hash == attempt.source_plan_hash,
        )
        changed = (
            session.query(TranslationAnalysisAttempt)
            .filter(
                TranslationAnalysisAttempt.id == attempt_id,
                TranslationAnalysisAttempt.dispatch_state == "prepared",
                TranslationAnalysisAttempt.dispatch_version == attempt.dispatch_version,
                live_operation,
            )
            .update(
                {
                    TranslationAnalysisAttempt.dispatch_state: "dispatched",
                    TranslationAnalysisAttempt.dispatch_version: attempt.dispatch_version
                    + 1,
                    TranslationAnalysisAttempt.dispatch_claimed_at: now,
                },
                synchronize_session=False,
            )
        )
        if changed != 1:
            session.rollback()
            _fresh_fence(attempt_id)
            raise BlockContractError("analysis-dispatch-replay")
        session.expire_all()
        current = session.get(TranslationAnalysisAttempt, attempt_id)
        if current is None:
            raise BlockContractError("analysis-dispatch-replay")
        append_analysis_attempt_event(
            current,
            "provider_dispatched",
            {"request_hash": current.request_hash},
            session=session,
        )
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _response_from_transcript(
    attempt: TranslationAnalysisAttempt,
    *,
    completed: bool = False,
    session: Any | None = None,
) -> Any:
    events = rehydrate_analysis_attempt_events(attempt, session=session)
    event_type = "completed" if completed else "provider_response"
    matches = tuple(event for event in events if event.event_type == event_type)
    if len(matches) != 1:
        raise BlockContractError("analysis-response-missing")
    payload = canonical_analysis_value(
        matches[0].payload_json, "analysis-event-payload"
    )
    if not isinstance(payload, Mapping):
        raise BlockContractError("analysis-response-payload")
    key = "output" if completed else "response"
    if set(payload) != {key}:
        raise BlockContractError("analysis-response-payload")
    return canonical_analysis_value(payload[key], "analysis-response")


def _finalize_response(attempt_id: str, response: Any) -> Any:
    """Complete a durable response without invoking provider I/O."""

    try:
        _fresh_fence(attempt_id)
    except (
        TranslationOperationCancellation,
        TranslationOperationNotRunnable,
        TranslationOperationTransitionConflict,
    ) as exc:
        _terminal_after_fence(attempt_id, "response_received", exc)
        raise
    try:
        _transition(
            attempt_id,
            expected_state="response_received",
            next_state="completed",
            event_type="completed",
            payload={"output": response},
        )
        return response
    except BlockContractError as exc:
        if exc.code == "analysis-dispatch-fence":
            try:
                _fresh_fence(attempt_id)
            except (
                TranslationOperationCancellation,
                TranslationOperationNotRunnable,
                TranslationOperationTransitionConflict,
            ) as fence_error:
                _terminal_after_fence(attempt_id, "response_received", fence_error)
                raise fence_error from exc
            _terminal_after_fence(attempt_id, "response_received", exc)
            raise
        _transition(
            attempt_id,
            expected_state="response_received",
            next_state="failed",
            event_type="failed",
            payload={"error_type": type(exc).__name__},
        )
        raise


def resume_analysis_attempt(
    attempt: TranslationAnalysisAttempt,
    *,
    verified_plan: CanonicalSourcePlan | None = None,
) -> Any:
    """Resume a committed response after a worker/session restart.

    The provider is never called.  A completed attempt safely returns its
    verified output; a response-received attempt is finalized through the
    same live-operation CAS used by the normal execution path.  A dispatched
    attempt remains explicitly unresolved because its provider outcome is
    unknowable from durable evidence alone.
    """

    attempt_id = _attempt_identifier(attempt)
    persisted = _independent_session()
    try:
        current = persisted.get(TranslationAnalysisAttempt, attempt_id)
        if current is None:
            raise BlockContractError("analysis-attempt-uncommitted")
        rehydrate_analysis_attempt(
            current,
            session=persisted,
            verified_plan=verified_plan,
        )
        if current.dispatch_state == "completed":
            return _response_from_transcript(current, completed=True, session=persisted)
        if current.dispatch_state == "response_received":
            response = _response_from_transcript(current, session=persisted)
        elif current.dispatch_state == "dispatched":
            raise BlockContractError("analysis-provider-outcome-unknown")
        else:
            raise BlockContractError("analysis-attempt-not-resumable")
    finally:
        persisted.close()
    return _finalize_response(attempt_id, response)


def reconcile_dispatched_analysis_attempt(
    attempt: TranslationAnalysisAttempt,
) -> None:
    """Terminalize an unresolved dispatch only after its fence is lost.

    This records uncertainty rather than inventing a provider failure.  It is
    deliberately separate from response resumption and never calls a
    provider.
    """

    attempt_id = _attempt_identifier(attempt)
    persisted = _independent_session()
    try:
        current = persisted.get(TranslationAnalysisAttempt, attempt_id)
        if current is None:
            raise BlockContractError("analysis-attempt-uncommitted")
        rehydrate_analysis_attempt(current, session=persisted)
        if current.dispatch_state == "provider_outcome_uncertain":
            return
        if current.dispatch_state != "dispatched":
            raise BlockContractError("analysis-dispatch-not-reconcilable")
    finally:
        persisted.close()
    try:
        _transition(
            attempt_id,
            expected_state="dispatched",
            next_state="provider_outcome_uncertain",
            event_type="provider_outcome_uncertain",
            payload={"code": "operation-fence-lost"},
            require_lost_fence=True,
        )
    except BlockContractError as exc:
        if exc.code == "analysis-dispatch-fence":
            raise BlockContractError("analysis-operation-fence-live") from exc
        raise


def _is_pre_dispatch_fence_failure(
    attempt: TranslationAnalysisAttempt, *, session: Any
) -> bool:
    """Identify the one terminal transcript this reconciliation owns."""

    return tuple(
        event.event_type
        for event in rehydrate_analysis_attempt_events(attempt, session=session)
    ) == ("request_prepared", "post_response_fence_failed")


def terminalize_stale_prepared_analysis_attempt(
    attempt: TranslationAnalysisAttempt,
) -> None:
    """Make a prepared request retryable after proving it never dispatched.

    This is intentionally narrower than normal execution recovery.  It only
    transitions ``prepared`` to ``fence_failed`` while the original operation
    fence is provably absent, records no provider-facing material, and is
    idempotent only for that exact pre-dispatch terminal transcript.
    """

    attempt_id = _attempt_identifier(attempt)
    persisted = _independent_session()
    try:
        current = persisted.get(TranslationAnalysisAttempt, attempt_id)
        if current is None:
            raise BlockContractError("analysis-attempt-uncommitted")
        rehydrate_analysis_attempt(current, session=persisted)
        if current.dispatch_state == "fence_failed":
            if _is_pre_dispatch_fence_failure(current, session=persisted):
                return
            raise BlockContractError("analysis-prepared-terminal-mismatch")
        if current.dispatch_state != "prepared":
            raise BlockContractError("analysis-prepared-not-reconcilable")
    finally:
        persisted.close()
    try:
        _transition(
            attempt_id,
            expected_state="prepared",
            next_state="fence_failed",
            event_type="post_response_fence_failed",
            payload={"code": "operation-fence-lost-before-dispatch"},
            require_lost_fence=True,
        )
    except BlockContractError as exc:
        if exc.code == "analysis-dispatch-fence":
            raise BlockContractError("analysis-operation-fence-live") from exc
        raise


def execute_analysis_attempt(
    attempt: TranslationAnalysisAttempt,
    *,
    completion_call: Callable[[Mapping[str, Any]], Mapping[str, Any] | str],
    before_dispatch_claim: Callable[[], None] | None = None,
    verified_plan: CanonicalSourcePlan | None = None,
) -> Any:
    """Call a supplied no-retry adapter once and durably record its response.

    The runner records a response only. A later typed finalization API owns
    normalization and persistence; arbitrary callbacks are deliberately
    excluded because a savepoint cannot contain a callback that commits.
    """

    attempt_id = _attempt_identifier(attempt)
    persisted = _independent_session()
    try:
        current = persisted.get(TranslationAnalysisAttempt, attempt_id)
        if current is None or current.dispatch_state != "prepared":
            raise BlockContractError("analysis-dispatch-replay")
        # Rehydrate the durable row rather than the caller's potentially stale
        # ORM object: workers resume after process/session boundaries.
        packet = rehydrate_analysis_attempt(
            current,
            session=persisted,
            verified_plan=verified_plan,
        )
        contract_version = current.contract_version
    finally:
        persisted.close()
    try:
        _fresh_fence(attempt_id)
        _claim_dispatch(attempt_id, before_dispatch_claim)
    except (
        TranslationOperationCancellation,
        TranslationOperationNotRunnable,
        TranslationOperationTransitionConflict,
    ) as exc:
        _terminal_after_fence(attempt_id, "prepared", exc)
        raise
    except BlockContractError as exc:
        if exc.code in {"analysis-attempt-operation", "analysis-operation-fence"}:
            _terminal_after_fence(attempt_id, "prepared", exc)
        raise
    try:
        # One and only one provider invocation.  Retrying is a new, witnessed
        # attempt created by the caller, never an invisible transport retry.
        response = completion_call(packet)
    except Exception as exc:
        _transition(
            attempt_id,
            expected_state="dispatched",
            next_state="provider_failed",
            event_type="provider_failure",
            payload={"error_type": type(exc).__name__},
        )
        raise
    if not isinstance(response, (str, Mapping)):
        _transition(
            attempt_id,
            expected_state="dispatched",
            next_state="failed",
            event_type="failed",
            payload={"code": "response_type"},
        )
        raise BlockContractError("analysis-response-type")
    try:
        response = canonical_analysis_value(response, "analysis-response")
        if contract_version == "analysis-attempt-v2":
            response = validate_analysis_response_extension(packet, response)
    except BlockContractError:
        _transition(
            attempt_id,
            expected_state="dispatched",
            next_state="failed",
            event_type="failed",
            payload={"code": "response_evidence"},
        )
        raise
    try:
        _transition(
            attempt_id,
            expected_state="dispatched",
            next_state="response_received",
            event_type="provider_response",
            payload={"response": response},
        )
    except BlockContractError:
        _transition(
            attempt_id,
            expected_state="dispatched",
            next_state="failed",
            event_type="failed",
            payload={"code": "response_evidence"},
        )
        raise
    return _finalize_response(attempt_id, response)


__all__ = [
    "execute_analysis_attempt",
    "prepare_and_commit_analysis_attempt",
    "reconcile_dispatched_analysis_attempt",
    "resume_analysis_attempt",
    "terminalize_stale_prepared_analysis_attempt",
]
