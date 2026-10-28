"""Encrypted, append-only glossary evidence for the block translator."""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, cast

from flask import has_request_context
from flask_login import current_user
from sqlalchemy import inspect, or_, update
from sqlalchemy.exc import IntegrityError, OperationalError

from carlina.core.utc import utc_iso, utc_now_db
from iarf_assistant.core.encryption import keyed_lookup_candidates, keyed_lookup_digest
from iarf_assistant.database_utils import is_database_locked
from iarf_assistant.extensions import db
from iarf_assistant.models import RuntimeApprovalRequest

from .analysis_contract import (
    GlossaryPayload,
    canonical_scope,
    glossary_lookup_digest,
    normalized_term,
    private_witness,
    private_witness_candidates,
)
from .block_contract import BlockContractError, canonical_json, sha256_text
from .block_persistence import (
    _ensure_database_transaction,
    rehydrate_source_plan,
    verify_source_plan_binding,
)
from .integrity_receipts import make_receipt, verify_receipt
from .models import (
    TranslationContextArtifactRevision,
    TranslationContextImpact,
    TranslationGlossaryActive,
    TranslationGlossaryConcept,
    TranslationGlossaryOccurrence,
    TranslationGlossaryRevision,
)
from .models import (
    TranslationSourcePlan as TranslationSourcePlanRecord,
)

_GLOSSARY_LOCK_SUBJECT = "translator.glossary-lock.v1"
_APPROVAL_RECEIPT_PURPOSE = "translator-glossary-approval-receipt-v1"


def _record(value: TranslationSourcePlanRecord | None) -> TranslationSourcePlanRecord:
    state = inspect(value) if isinstance(value, TranslationSourcePlanRecord) else None
    if state is None or not state.persistent:
        raise BlockContractError("glossary-source-plan")
    record = cast(TranslationSourcePlanRecord, value)
    # Glossary evidence is bound to the active plan identity, but it neither
    # reads nor renders source blocks.  Rehydrating every encrypted block and
    # anchor here made an empty glossary scale with the entire document.
    verify_source_plan_binding(record)
    return record


def _revision_hash(row: Any) -> str:
    parent = (
        db.session.get(TranslationGlossaryRevision, row.parent_revision_id)
        if row.parent_revision_id
        else None
    )
    return make_receipt(
        purpose="translator-glossary-revision-receipt-v1",
        values={
            "receipt_version": 1,
            "id": row.id,
            "concept_id": row.concept_id,
            "project_id": row.project_id,
            "source_plan_id": row.source_plan_id,
            "revision_number": row.revision_number,
            "parent_revision_id": row.parent_revision_id,
            "parent_record_hash": parent.record_hash if parent is not None else None,
            "approval_request_id": row.approval_request_id,
            "approved_by_user_id": row.approved_by_user_id,
            "approved_at": row.approved_at.isoformat() if row.approved_at else None,
            "approval_receipt": row.approval_receipt,
            "author_type": row.author_type,
            "revision_state": row.revision_state,
            "normalization_profile": row.normalization_profile,
            "normalization_version": row.normalization_version,
            "normalization_hash": row.normalization_hash,
            "keyed_lookup_digest": row.keyed_lookup_digest,
            "source_digest": row.source_digest,
            "target_digest": row.target_digest,
            "payload_hash": row.payload_hash,
            "payload_json": row.payload_json,
            "created_at": row.created_at.isoformat(),
            },
    )


def _revision_valid(row: Any) -> bool:
    try:
        parent = (
            db.session.get(TranslationGlossaryRevision, row.parent_revision_id)
            if row.parent_revision_id
            else None
        )
        verify_receipt(
            stored=row.record_hash,
            purpose="translator-glossary-revision-receipt-v1",
            values={
                "receipt_version": 1,
                "id": row.id,
                "concept_id": row.concept_id,
                "project_id": row.project_id,
                "source_plan_id": row.source_plan_id,
                "revision_number": row.revision_number,
                "parent_revision_id": row.parent_revision_id,
                "parent_record_hash": parent.record_hash if parent else None,
                "approval_request_id": row.approval_request_id,
                "approved_by_user_id": row.approved_by_user_id,
                "approved_at": row.approved_at.isoformat() if row.approved_at else None,
                "approval_receipt": row.approval_receipt,
                "author_type": row.author_type,
                "revision_state": row.revision_state,
                "normalization_profile": row.normalization_profile,
                "normalization_version": row.normalization_version,
                "normalization_hash": row.normalization_hash,
                "keyed_lookup_digest": row.keyed_lookup_digest,
                "source_digest": row.source_digest,
                "target_digest": row.target_digest,
                "payload_hash": row.payload_hash,
                "payload_json": row.payload_json,
                "created_at": row.created_at.isoformat(),
            },
            code="glossary-revision-owner",
        )
    except BlockContractError:
        return False
    return True


def _occurrence_receipt(row: Any) -> str:
    revision = db.session.get(TranslationGlossaryRevision, row.revision_id)
    return make_receipt(
        purpose="translator-glossary-occurrence-receipt-v1",
        values=_occurrence_receipt_values(row, revision),
    )


def _occurrence_receipt_values(
    row: Any, revision: TranslationGlossaryRevision | None
) -> dict[str, object]:
    return {
        "receipt_version": 1,
        "id": row.id,
        "revision_id": row.revision_id,
        "revision_record_hash": revision.record_hash if revision else None,
        "concept_id": row.concept_id,
        "project_id": row.project_id,
        "source_plan_id": row.source_plan_id,
        "block_id": row.block_id,
        "anchor_id": row.anchor_id,
        "start_offset": row.start_offset,
        "end_offset": row.end_offset,
        "occurrence_hash": row.occurrence_hash,
    }


def _impact_reference_receipts(row: Any) -> dict[str, str | None]:
    """Load and rehydrate every immutable row referenced by an impact."""

    from .analysis_persistence import rehydrate_context_artifact_revision

    values: dict[str, str | None] = {}

    def bind_glossary(field: str) -> None:
        reference_id = getattr(row, field)
        receipt_key = field.removesuffix("_id") + "_record_hash"
        if reference_id is None:
            values[receipt_key] = None
            return
        reference = db.session.get(TranslationGlossaryRevision, reference_id)
        if reference is None:
            raise BlockContractError("glossary-impact-receipt")
        rehydrate_glossary_revision(reference)
        if (
            reference.project_id != row.project_id
            or reference.source_plan_id != row.source_plan_id
        ):
            raise BlockContractError("glossary-impact-receipt")
        values[receipt_key] = reference.record_hash

    def bind_artifact(field: str) -> None:
        reference_id = getattr(row, field)
        receipt_key = field.removesuffix("_id") + "_record_hash"
        if reference_id is None:
            values[receipt_key] = None
            return
        reference = db.session.get(TranslationContextArtifactRevision, reference_id)
        if reference is None:
            raise BlockContractError("glossary-impact-receipt")
        rehydrate_context_artifact_revision(reference)
        if (
            reference.project_id != row.project_id
            or reference.source_plan_id != row.source_plan_id
        ):
            raise BlockContractError("glossary-impact-receipt")
        values[receipt_key] = reference.record_hash

    bind_glossary("old_glossary_revision_id")
    bind_glossary("new_glossary_revision_id")
    bind_artifact("old_artifact_revision_id")
    bind_artifact("new_artifact_revision_id")
    return values


def _impact_receipt_values(row: Any) -> dict[str, object]:
    return {
        "receipt_version": 1,
        "id": row.id,
        "project_id": row.project_id,
        "source_plan_id": row.source_plan_id,
        "block_id": row.block_id,
        "old_glossary_revision_id": row.old_glossary_revision_id,
        "new_glossary_revision_id": row.new_glossary_revision_id,
        "old_artifact_revision_id": row.old_artifact_revision_id,
        "new_artifact_revision_id": row.new_artifact_revision_id,
        **_impact_reference_receipts(row),
        "reason_json": row.reason_json,
        "impact_state": row.impact_state,
        "impact_key": row.impact_key,
        "created_at": utc_iso(row.created_at, timespec="microseconds"),
    }


def _impact_receipt(row: Any) -> str:
    return make_receipt(
        purpose="translator-context-impact-receipt-v1",
        values=_impact_receipt_values(row),
    )


def _active_receipt_values(
    active: TranslationGlossaryActive, revision: TranslationGlossaryRevision
) -> dict[str, object]:
    return {
        "receipt_version": 1,
        "concept_id": active.concept_id,
        "revision_id": active.revision_id,
        "revision_record_hash": revision.record_hash,
        "version": active.version,
        "updated_at": utc_iso(active.updated_at, timespec="microseconds"),
    }


def verify_glossary_active(active: TranslationGlossaryActive) -> TranslationGlossaryRevision:
    """Verify the durable active pointer, never a stale caller-side snapshot."""

    if not isinstance(active, TranslationGlossaryActive) or not active.concept_id:
        raise BlockContractError("glossary-active-receipt")
    # A verification read must not make unrelated dirty caller state durable.
    # ``populate_existing`` is also essential here: another worker may have
    # advanced the pointer since this scoped session cached the row.
    with db.session.no_autoflush:
        durable = db.session.get(
            TranslationGlossaryActive,
            active.concept_id,
            populate_existing=True,
        )
        if durable is None:
            raise BlockContractError("glossary-active-receipt")
        revision = db.session.get(
            TranslationGlossaryRevision,
            durable.revision_id,
            populate_existing=True,
        )
        if revision is None or revision.concept_id != durable.concept_id:
            raise BlockContractError("glossary-active-receipt")
        rehydrate_glossary_revision(revision)
        verify_receipt(
            stored=durable.active_receipt,
            purpose="translator-glossary-active-receipt-v1",
            values=_active_receipt_values(durable, revision),
            code="glossary-active-receipt",
        )
        return revision


def rehydrate_active_glossary_revision(
    concept_id: str,
) -> TranslationGlossaryRevision:
    """Return the locked revision only after verifying its mutable pointer."""

    if not isinstance(concept_id, str) or not concept_id:
        raise BlockContractError("glossary-active-receipt")
    with db.session.no_autoflush:
        active = db.session.get(
            TranslationGlossaryActive, concept_id, populate_existing=True
        )
    if active is None:
        raise BlockContractError("glossary-active-receipt")
    return verify_glossary_active(active)


def _verify_occurrence(row: Any) -> None:
    with db.session.no_autoflush:
        revision = db.session.get(TranslationGlossaryRevision, row.revision_id)
        if revision is None:
            raise BlockContractError("glossary-occurrence-receipt")
        rehydrate_glossary_revision(revision)
        if (
            row.concept_id != revision.concept_id
            or row.project_id != revision.project_id
            or row.source_plan_id != revision.source_plan_id
        ):
            raise BlockContractError("glossary-occurrence-receipt")
        verify_receipt(
            stored=row.record_receipt,
            purpose="translator-glossary-occurrence-receipt-v1",
            values=_occurrence_receipt_values(row, revision),
            code="glossary-occurrence-receipt",
        )


def _verify_impact(row: Any) -> None:
    with db.session.no_autoflush:
        verify_receipt(
            stored=row.record_receipt,
            purpose="translator-context-impact-receipt-v1",
            values=_impact_receipt_values(row),
            code="glossary-impact-receipt",
        )


def verify_glossary_occurrence(
    row: TranslationGlossaryOccurrence,
) -> TranslationGlossaryOccurrence:
    _verify_occurrence(row)
    return row


def verify_context_impact(row: TranslationContextImpact) -> TranslationContextImpact:
    _verify_impact(row)
    return row


def _private_term_digest(value: str, *, purpose: str) -> str:
    """Stable only for the active key; verification accepts retained candidates."""

    return sha256_text(keyed_lookup_digest(value, purpose=purpose))


def _private_term_digest_candidates(value: str, *, purpose: str) -> tuple[str, ...]:
    return tuple(
        sha256_text(candidate)
        for candidate in keyed_lookup_candidates(value, purpose=purpose)
    )


def _canonical_scope(value: object) -> str:
    return canonical_scope(value)


def glossary_lock_preview(
    record: TranslationSourcePlanRecord,
    concept: TranslationGlossaryConcept,
    proposal: TranslationGlossaryRevision,
    payload: GlossaryPayload,
) -> dict[str, str]:
    return {
        "contract_version": _GLOSSARY_LOCK_SUBJECT,
        "project_id": record.project_id,
        "source_plan_id": record.id,
        "concept_id": concept.id,
        "proposal_id": proposal.id,
        "proposal_record_hash": proposal.record_hash,
        "approved_payload_hash": payload.payload_hash,
        "scope": _canonical_scope(payload.scope),
    }


def _approval_receipt_identity(
    *, record: TranslationSourcePlanRecord, concept: TranslationGlossaryConcept,
    proposal: TranslationGlossaryRevision, locked: TranslationGlossaryRevision,
    payload: GlossaryPayload, approval: RuntimeApprovalRequest,
) -> dict[str, object]:
    """Immutable approval-consumption envelope; mutable request columns alone
    are deliberately insufficient to create a trusted human lock."""

    return {
        "contract_version": "translator-glossary-approval-receipt-v1",
        "request": {
            "id": approval.id, "requester": approval.requester_user_id,
            "approver": approval.approver_user_id, "subject_type": approval.subject_type,
            "subject_id": approval.subject_id, "status": approval.status,
            "resolved_at": approval.resolved_at.isoformat() if approval.resolved_at else None,
            "expires_at": approval.expires_at.isoformat() if approval.expires_at else None,
            "preview": approval.preview_payload,
        },
        "proposal": {
            "id": proposal.id, "record_hash": proposal.record_hash,
            "project_id": proposal.project_id, "source_plan_id": proposal.source_plan_id,
            "concept_id": proposal.concept_id, "revision_number": proposal.revision_number,
        },
        "locked": {
            "id": locked.id, "revision_number": locked.revision_number,
            "parent_revision_id": locked.parent_revision_id,
            "project_id": locked.project_id, "source_plan_id": locked.source_plan_id,
            "concept_id": locked.concept_id, "payload": payload.to_dict(),
        },
        "project_owner": record.project.user_id,
        "concept_owner": concept.owner_user_id,
    }


def create_glossary_concept(
    source_plan: TranslationSourcePlanRecord, *, scope: str = "document"
) -> TranslationGlossaryConcept:
    record = _record(source_plan)
    scope = _canonical_scope(scope)
    if not scope:
        raise BlockContractError("glossary-concept-scope")
    _ensure_database_transaction()
    row = cast(Any, TranslationGlossaryConcept)(
        id=str(uuid.uuid4()),
        project_id=record.project_id,
        source_plan_id=record.id,
        owner_user_id=record.project.user_id,
        concept_scope=scope,
    )
    db.session.add(row)
    db.session.flush()
    return row


def persist_glossary_revision(
    source_plan: TranslationSourcePlanRecord,
    concept: TranslationGlossaryConcept,
    *,
    payload: GlossaryPayload,
    author_type: str,
    revision_state: str,
    normalization_profile: str = "unicode-casefold-v1",
    normalization_version: str = "1",
    revision_id: str | None = None,
) -> TranslationGlossaryRevision:
    record = _record(source_plan)
    if (
        concept.project_id != record.project_id
        or concept.source_plan_id != record.id
        or concept.owner_user_id != record.project.user_id
        or concept.concept_scope != _canonical_scope(payload.scope)
    ):
        raise BlockContractError("glossary-concept-owner")
    if (
        author_type not in {"model", "system"}
        or revision_state not in {"proposal", "reviewed"}
    ):
        raise BlockContractError("glossary-revision-authority")
    normalized = normalized_term(
        payload.source_term,
        profile=normalization_profile,
        version=normalization_version,
    )
    prior = (
        TranslationGlossaryRevision.query.filter_by(concept_id=concept.id)
        .order_by(TranslationGlossaryRevision.revision_number.desc())
        .first()
    )
    if prior is not None:
        rehydrate_glossary_revision(prior, record)
    _ensure_database_transaction()
    now = utc_now_db()
    row = cast(Any, TranslationGlossaryRevision)(
        id=revision_id or str(uuid.uuid4()),
        concept_id=concept.id,
        project_id=record.project_id,
        source_plan_id=record.id,
        revision_number=(prior.revision_number + 1 if prior else 1),
        parent_revision_id=(prior.id if prior else None),
        approval_request_id=None,
        approved_by_user_id=None,
        approved_at=None,
        author_type=author_type,
        revision_state=revision_state,
        normalization_profile=normalization_profile,
        normalization_version=normalization_version,
        normalization_hash=_private_term_digest(
            normalized, purpose="translator-glossary-normalization-v1"
        ),
        keyed_lookup_digest=glossary_lookup_digest(
            payload.source_term,
            profile=normalization_profile,
            version=normalization_version,
        ),
        source_digest=_private_term_digest(
            payload.source_term, purpose="translator-glossary-source-v1"
        ),
        target_digest=_private_term_digest(
            payload.target_term, purpose="translator-glossary-target-v1"
        ),
        payload_hash=payload.payload_hash,
        record_hash="0" * 64,
        payload_json=payload.to_dict(),
        created_at=now,
    )
    row.record_hash = _revision_hash(row)
    db.session.add(row)
    db.session.flush()
    return row


def approve_glossary_proposal(
    source_plan: TranslationSourcePlanRecord,
    proposal: TranslationGlossaryRevision,
    *,
    approved_payload: GlossaryPayload,
    approval: RuntimeApprovalRequest | str,
) -> TranslationGlossaryRevision:
    """Consume one durable, authenticated pending approval into a human lock.

    ``RuntimeApprovalRequest.status`` is deliberately not a capability.  The
    action that changes it from pending to approved is this function, after it
    has reloaded the immutable proposal/request identity and checked the
    current Flask actor.  This makes a raw status flip, a stale ORM object, and
    a caller-mutated ``approved`` request equally non-authoritative.
    """
    proposal_id = getattr(proposal, "id", None)
    if not isinstance(proposal_id, str) or not proposal_id:
        raise BlockContractError("glossary-approval-proposal")
    approval_id = approval.id if isinstance(approval, RuntimeApprovalRequest) else approval
    if not isinstance(approval_id, str) or not approval_id:
        raise BlockContractError("glossary-approval-request")
    # Do not trust an object supplied by a caller.  In particular, this must
    # not autoflush a caller's forged status/preview mutation before we reload
    # the rows used for authorization.
    with db.session.no_autoflush:
        record = _record(source_plan)
        for supplied in (proposal, approval if isinstance(approval, RuntimeApprovalRequest) else None):
            state = inspect(supplied) if supplied is not None else None
            if state is not None and state.persistent:
                # Do not leave a dirty, caller-owned object in the identity
                # map.  ``populate_existing`` can otherwise retain an
                # encrypted attribute's in-memory value.  We already captured
                # the only accepted input (the opaque ID), so expunging is the
                # least ambiguous way to force a canonical DB load.
                db.session.expunge(supplied)
        canonical_proposal = db.session.get(
            TranslationGlossaryRevision, proposal_id, populate_existing=True
        )
        request = db.session.get(
            RuntimeApprovalRequest, approval_id, populate_existing=True
        )
    if not has_request_context() or not getattr(current_user, "is_authenticated", False):
        raise BlockContractError("glossary-approval-actor")
    if getattr(current_user, "id", None) != record.project.user_id:
        raise BlockContractError("glossary-approval-actor")
    if (
        canonical_proposal is None
        or canonical_proposal.revision_state != "proposal"
        or canonical_proposal.author_type not in {"model", "system"}
    ):
        raise BlockContractError("glossary-approval-state")
    proposal = canonical_proposal
    concept = db.session.get(TranslationGlossaryConcept, proposal.concept_id)
    if concept is None:
        raise BlockContractError("glossary-approval-concept")
    if (
        concept is None or concept.source_plan_id != record.id
        or concept.project_id != record.project_id
        or concept.owner_user_id != record.project.user_id
        or proposal.concept_id != concept.id or proposal.project_id != record.project_id
        or proposal.source_plan_id != record.id
    ):
        raise BlockContractError("glossary-approval-concept")
    if approved_payload.scope != concept.concept_scope:
        raise BlockContractError("glossary-approval-scope")
    if request is None:
        raise BlockContractError("glossary-approval-request")
    request_state = inspect(request)
    if request_state is None or not request_state.persistent:
        raise BlockContractError("glossary-approval-request")
    preview = glossary_lock_preview(record, concept, proposal, approved_payload)
    now = utc_now_db()
    if (
        request.status != "pending"
        or request.subject_type != _GLOSSARY_LOCK_SUBJECT
        or request.subject_id != proposal.id
        or request.requester_user_id != record.project.user_id
        or request.approver_user_id is not None
        or request.resolved_at is not None
        or (request.expires_at is not None and request.expires_at <= now)
        or not isinstance(request.preview_payload, dict)
        or set(request.preview_payload) != set(preview)
        or any(
            request.preview_payload.get(key) != value
            for key, value in preview.items()
            if key != "approved_payload_hash"
        )
        or request.preview_payload.get("approved_payload_hash")
        not in private_witness_candidates(
            approved_payload.to_dict(), purpose="translator-glossary-payload-v1"
        )
    ):
        raise BlockContractError("glossary-approval-request")
    # Canonical proposal must be independently valid before it becomes the
    # direct predecessor of a human lock.  This also rejects forged lineage.
    rehydrate_glossary_revision(proposal, record)
    _ensure_database_transaction()
    try:
        # The compare-and-swap consumes exactly the durable pending state which
        # we inspected above.  A raw flip to approved, a second request, or a
        # competing session therefore cannot mint a second human lock.
        with db.session.begin_nested():
            transition = db.session.execute(
                update(RuntimeApprovalRequest)
                .where(
                    RuntimeApprovalRequest.id == request.id,
                    RuntimeApprovalRequest.status == "pending",
                    RuntimeApprovalRequest.requester_user_id == record.project.user_id,
                    RuntimeApprovalRequest.approver_user_id.is_(None),
                    RuntimeApprovalRequest.resolved_at.is_(None),
                    or_(
                        RuntimeApprovalRequest.expires_at.is_(None),
                        RuntimeApprovalRequest.expires_at > now,
                    ),
                )
                .values(
                    status="approved",
                    approver_user_id=record.project.user_id,
                    resolved_at=now,
                )
            )
            if cast(Any, transition).rowcount != 1:
                raise BlockContractError("glossary-approval-used")
            db.session.expire(request)
            request = db.session.get(
                RuntimeApprovalRequest, request.id, populate_existing=True
            )
            if request is None or request.status != "approved":
                raise BlockContractError("glossary-approval-used")
            # Keep lock row construction inside the authenticated CAS scope.
            # There is intentionally no callable persistence helper: a raw
            # status flip or a caller-imported private function cannot mint a
            # human lock without winning this pending->approved transition.
            normalized = normalized_term(approved_payload.source_term)
            row = cast(Any, TranslationGlossaryRevision)(
                id=str(uuid.uuid4()), concept_id=concept.id,
                project_id=record.project_id, source_plan_id=record.id,
                revision_number=proposal.revision_number + 1,
                parent_revision_id=proposal.id, approval_request_id=request.id,
                approved_by_user_id=request.approver_user_id,
                approved_at=request.resolved_at, approval_receipt=None,
                author_type="human", revision_state="locked",
                normalization_profile="unicode-casefold-v1",
                normalization_version="1",
                normalization_hash=_private_term_digest(
                    normalized, purpose="translator-glossary-normalization-v1"
                ),
                keyed_lookup_digest=glossary_lookup_digest(
                    approved_payload.source_term
                ),
                source_digest=_private_term_digest(
                    approved_payload.source_term,
                    purpose="translator-glossary-source-v1",
                ),
                target_digest=_private_term_digest(
                    approved_payload.target_term,
                    purpose="translator-glossary-target-v1",
                ),
                payload_hash=approved_payload.payload_hash,
                record_hash="0" * 64,
                payload_json=approved_payload.to_dict(),
                created_at=utc_now_db(),
            )
            row.approval_receipt = private_witness(
                _approval_receipt_identity(
                    record=record, concept=concept, proposal=proposal,
                    locked=row, payload=approved_payload, approval=request,
                ),
                purpose=_APPROVAL_RECEIPT_PURPOSE,
            )
            row.record_hash = _revision_hash(row)
            db.session.add(row)
            db.session.flush()
    except IntegrityError as exc:
        raise BlockContractError("glossary-approval-used") from exc
    except OperationalError as exc:
        if is_database_locked(exc):
            raise BlockContractError("glossary-approval-used") from exc
        raise
    return row


def activate_glossary_revision(
    revision: TranslationGlossaryRevision, *, expected_version: int | None = None
) -> TranslationGlossaryActive:
    record = _record(db.session.get(TranslationSourcePlanRecord, revision.source_plan_id))
    rehydrate_glossary_revision(revision, record)
    if revision.revision_state != "locked" or revision.author_type != "human":
        raise BlockContractError("glossary-active-authority")
    _ensure_database_transaction()
    active = db.session.get(TranslationGlossaryActive, revision.concept_id)
    if active is None:
        if expected_version not in (None, 0):
            raise BlockContractError("glossary-active-conflict")
        try:
            with db.session.begin_nested():
                now = utc_now_db()
                active = cast(Any, TranslationGlossaryActive)(
                    concept_id=revision.concept_id,
                    revision_id=revision.id,
                    version=1,
                    active_receipt="0" * 64,
                    updated_at=now,
                )
                db.session.add(active)
                db.session.flush()
                db.session.refresh(active)
                receipt = make_receipt(
                    purpose="translator-glossary-active-receipt-v1",
                    values=_active_receipt_values(active, revision),
                )
                result = db.session.execute(
                    update(TranslationGlossaryActive)
                    .where(
                        TranslationGlossaryActive.concept_id == active.concept_id,
                        TranslationGlossaryActive.revision_id == active.revision_id,
                        TranslationGlossaryActive.version == active.version,
                        TranslationGlossaryActive.active_receipt == "0" * 64,
                    )
                    .values(active_receipt=receipt, updated_at=now)
                )
                if cast(Any, result).rowcount != 1:
                    raise BlockContractError("glossary-active-conflict")
                db.session.flush()
                db.session.refresh(active)
        except (IntegrityError, OperationalError) as exc:
            if isinstance(exc, IntegrityError) or is_database_locked(exc):
                raise BlockContractError("glossary-active-conflict") from exc
            raise
        verify_glossary_active(active)
        return active
    if expected_version is None:
        raise BlockContractError("glossary-active-conflict")
    try:
        now = utc_now_db()
        result = db.session.execute(
            update(TranslationGlossaryActive)
            .where(
                TranslationGlossaryActive.concept_id == revision.concept_id,
                TranslationGlossaryActive.version == expected_version,
            )
            .values(
                revision_id=revision.id,
                version=expected_version + 1,
                updated_at=now,
                active_receipt="0" * 64,
            )
        )
    except OperationalError as exc:
        if is_database_locked(exc):
            raise BlockContractError("glossary-active-conflict") from exc
        raise
    if cast(Any, result).rowcount != 1:
        raise BlockContractError("glossary-active-conflict")
    db.session.flush()
    refreshed = db.session.get(TranslationGlossaryActive, revision.concept_id)
    if refreshed is None:
        raise BlockContractError("glossary-active-conflict")
    receipt = make_receipt(
        purpose="translator-glossary-active-receipt-v1",
        values=_active_receipt_values(refreshed, revision),
    )
    result = db.session.execute(
        update(TranslationGlossaryActive)
        .where(
            TranslationGlossaryActive.concept_id == refreshed.concept_id,
            TranslationGlossaryActive.revision_id == refreshed.revision_id,
            TranslationGlossaryActive.version == refreshed.version,
            TranslationGlossaryActive.active_receipt == "0" * 64,
        )
        .values(active_receipt=receipt, updated_at=now)
    )
    if cast(Any, result).rowcount != 1:
        raise BlockContractError("glossary-active-conflict")
    db.session.flush()
    db.session.refresh(refreshed)
    verify_glossary_active(refreshed)
    return refreshed


def rehydrate_glossary_revision(
    revision: TranslationGlossaryRevision,
    source_plan: TranslationSourcePlanRecord | None = None,
    _seen: set[str] | None = None,
) -> GlossaryPayload:
    try:
        if not isinstance(revision, TranslationGlossaryRevision) or not revision.id:
            raise BlockContractError("glossary-revision-owner")
        seen = _seen if _seen is not None else set()
        if revision.id in seen:
            raise BlockContractError("glossary-revision-lineage")
        seen.add(revision.id)
        record = _record(
            source_plan
            or db.session.get(TranslationSourcePlanRecord, revision.source_plan_id)
        )
        concept = db.session.get(TranslationGlossaryConcept, revision.concept_id)
        if (
            concept is None
            or concept.project_id != record.project_id
            or concept.source_plan_id != record.id
            or concept.owner_user_id != record.project.user_id
            or revision.project_id != record.project_id
            or revision.source_plan_id != record.id
            or not _revision_valid(revision)
        ):
            raise BlockContractError("glossary-revision-owner")
        payload = GlossaryPayload(**revision.payload_json)
        if (
            _canonical_scope(payload.scope) != concept.concept_scope
            or revision.payload_hash
            not in private_witness_candidates(
                payload.to_dict(), purpose="translator-glossary-payload-v1"
            )
            or revision.source_digest
            not in _private_term_digest_candidates(
                payload.source_term, purpose="translator-glossary-source-v1"
            )
            or revision.target_digest
            not in _private_term_digest_candidates(
                payload.target_term, purpose="translator-glossary-target-v1"
            )
            or revision.normalization_hash
            not in _private_term_digest_candidates(
                normalized_term(
                    payload.source_term,
                    profile=revision.normalization_profile,
                    version=revision.normalization_version,
                ),
                purpose="translator-glossary-normalization-v1",
            )
            or revision.keyed_lookup_digest
            not in keyed_lookup_candidates(
                normalized_term(
                    payload.source_term,
                    profile=revision.normalization_profile,
                    version=revision.normalization_version,
                ),
                purpose=(
                    "translator-glossary:"
                    f"{revision.normalization_profile}:{revision.normalization_version}"
                ),
            )
        ):
            raise BlockContractError("glossary-revision-witness")
        parent = (
            db.session.get(TranslationGlossaryRevision, revision.parent_revision_id)
            if revision.parent_revision_id is not None
            else None
        )
        if revision.revision_number == 1:
            if parent is not None:
                raise BlockContractError("glossary-revision-lineage")
        elif (
            parent is None
            or parent.id == revision.id
            or parent.concept_id != concept.id
            or parent.project_id != record.project_id
            or parent.source_plan_id != record.id
            or parent.revision_number + 1 != revision.revision_number
        ):
            raise BlockContractError("glossary-revision-lineage")
        if parent is not None:
            rehydrate_glossary_revision(parent, record, seen)
        if revision.revision_state == "locked":
            approval = db.session.get(RuntimeApprovalRequest, revision.approval_request_id)
            if (
                revision.author_type != "human"
                or approval is None
                or parent is None
                or parent.revision_state != "proposal"
                or parent.author_type not in {"model", "system"}
                or parent.concept_id != concept.id
                or parent.project_id != record.project_id
                or parent.source_plan_id != record.id
                or parent.revision_number + 1 != revision.revision_number
                or revision.approved_by_user_id != record.project.user_id
                or revision.approved_at != approval.resolved_at
                or approval.status != "approved"
                or approval.subject_type != _GLOSSARY_LOCK_SUBJECT
                or approval.subject_id != parent.id
                or approval.requester_user_id != record.project.user_id
                or approval.approver_user_id != record.project.user_id
                or approval.resolved_at is None
                or (
                    approval.expires_at is not None
                    and approval.resolved_at >= approval.expires_at
                )
                or not isinstance(approval.preview_payload, dict)
                or set(approval.preview_payload)
                != {"contract_version", "project_id", "source_plan_id", "concept_id", "proposal_id", "proposal_record_hash", "approved_payload_hash", "scope"}
                or approval.preview_payload.get("contract_version") != _GLOSSARY_LOCK_SUBJECT
                or approval.preview_payload.get("project_id") != record.project_id
                or approval.preview_payload.get("source_plan_id") != record.id
                or approval.preview_payload.get("concept_id") != concept.id
                or approval.preview_payload.get("proposal_id") != parent.id
                or approval.preview_payload.get("proposal_record_hash") != parent.record_hash
                or approval.preview_payload.get("scope") != _canonical_scope(payload.scope)
                or approval.preview_payload.get("approved_payload_hash")
                not in private_witness_candidates(
                    payload.to_dict(), purpose="translator-glossary-payload-v1"
                )
            ):
                raise BlockContractError("glossary-revision-authority")
            identity = _approval_receipt_identity(
                record=record, concept=concept, proposal=parent, locked=revision,
                payload=payload, approval=approval,
            )
            if revision.approval_receipt not in private_witness_candidates(
                identity, purpose=_APPROVAL_RECEIPT_PURPOSE
            ):
                raise BlockContractError("glossary-revision-authority")
        elif (
            revision.approval_request_id is not None
            or revision.approved_by_user_id is not None
            or revision.approved_at is not None
            or revision.approval_receipt is not None
            or revision.author_type == "human"
        ):
            raise BlockContractError("glossary-revision-authority")
        return payload
    except BlockContractError:
        raise
    except Exception as exc:
        raise BlockContractError("glossary-revision-corrupt") from exc


def find_glossary_revisions_for_term(
    source_plan: TranslationSourcePlanRecord, term: str
) -> tuple[TranslationGlossaryRevision, ...]:
    """Rotation-safe private lookup; every returned row is rehydrated first."""

    record = _record(source_plan)
    # The native candidate helper includes retained keys and is the sole query
    # authority; this never derives an unkeyed plaintext term index.
    normalized = normalized_term(term)
    candidates = keyed_lookup_candidates(
        normalized, purpose="translator-glossary:unicode-casefold-v1:1"
    )
    rows = tuple(
        TranslationGlossaryRevision.query.filter(
            TranslationGlossaryRevision.source_plan_id == record.id,
            TranslationGlossaryRevision.keyed_lookup_digest.in_(candidates),
        )
        .order_by(TranslationGlossaryRevision.created_at, TranslationGlossaryRevision.id)
        .all()
    )
    for row in rows:
        rehydrate_glossary_revision(row, record)
    return rows


def persist_analysis_glossary_proposals(
    source_plan: TranslationSourcePlanRecord,
    proposals: Sequence[Mapping[str, object]],
) -> tuple[TranslationGlossaryRevision, ...]:
    """Persist actionable analysis terms as unapproved model proposals.

    Analysis remains advisory: this function never creates an approval request
    or an active glossary pointer.  Exact retries reuse the existing proposal,
    while a human-locked concept always wins over later model output.
    Targetless findings stay in the encrypted analysis evidence until a model
    or owner supplies an actual translation.
    """

    record = _record(source_plan)
    persisted: list[TranslationGlossaryRevision] = []
    expected_keys = {
        "source_term",
        "target_term",
        "notes",
        "scope",
        "source_block_ids",
        "confidence",
        "status",
    }
    for value in proposals:
        if (
            not isinstance(value, Mapping)
            or set(value) != expected_keys
            or value.get("scope") != "document"
            or value.get("status") != "proposed"
        ):
            raise BlockContractError("analysis-glossary-proposal")
        target = value.get("target_term")
        if target is None:
            continue
        source = value.get("source_term")
        notes = value.get("notes")
        if not isinstance(source, str) or not isinstance(target, str):
            raise BlockContractError("analysis-glossary-proposal")
        if notes is not None and not isinstance(notes, str):
            raise BlockContractError("analysis-glossary-proposal")
        payload = GlossaryPayload(
            source_term=source.strip(),
            target_term=target.strip(),
            notes=notes if notes else None,
            scope="document",
        )
        revisions = find_glossary_revisions_for_term(record, payload.source_term)
        concept_ids = {revision.concept_id for revision in revisions}
        if len(concept_ids) > 1:
            raise BlockContractError("glossary-proposal-ambiguous")

        # A verified active human decision is authoritative.  Do not append a
        # competing model suggestion to that concept.
        active = (
            db.session.get(TranslationGlossaryActive, next(iter(concept_ids)))
            if concept_ids
            else None
        )
        if active is not None:
            verify_glossary_active(active)
            continue

        matching = [
            revision
            for revision in revisions
            if rehydrate_glossary_revision(revision, record) == payload
            and revision.author_type == "model"
            and revision.revision_state == "proposal"
        ]
        if matching:
            persisted.append(matching[-1])
            continue

        concept = (
            db.session.get(TranslationGlossaryConcept, next(iter(concept_ids)))
            if concept_ids
            else create_glossary_concept(record, scope=payload.scope)
        )
        if concept is None:
            raise BlockContractError("glossary-proposal-concept")
        persisted.append(
            persist_glossary_revision(
                record,
                concept,
                payload=payload,
                author_type="model",
                revision_state="proposal",
            )
        )
    db.session.flush()
    return tuple(persisted)


def persist_glossary_occurrences(
    revision: TranslationGlossaryRevision,
    occurrences: Iterable[dict[str, object]],
    *,
    verified_plan: Any = None,
) -> tuple[TranslationGlossaryOccurrence, ...]:
    """Persist only exact terminology-eligible source-projection spans.

    Offsets are explicitly measured in the owning block's ``source_projection``;
    accepting raw HTML, template, literal, protected, or identifier positions would
    make a glossary claim about text the translator is not allowed to rewrite.
    """
    record = _record(
        db.session.get(TranslationSourcePlanRecord, revision.source_plan_id)
    )
    rehydrate_glossary_revision(revision, record)
    plan = verified_plan if verified_plan is not None else rehydrate_source_plan(record)
    if plan.project_id != record.project_id or plan.plan_hash != record.plan_hash:
        raise BlockContractError("glossary-occurrence-plan")
    payload = rehydrate_glossary_revision(revision, record)
    blocks = {block.id: block for block in plan.blocks}
    anchors = {anchor.id: anchor for anchor in cast(Any, record.anchors)}
    validated: list[tuple[str, str | None, int, int, str]] = []
    seen: set[str] = set()
    for occurrence in occurrences:
        if not isinstance(occurrence, dict) or set(occurrence) != {
            "coordinate_system", "block_id", "anchor_id", "start_offset", "end_offset"
        }:
            raise BlockContractError("glossary-occurrence-coordinate")
        coordinate_system = occurrence["coordinate_system"]
        block_id = occurrence["block_id"]
        anchor_id = occurrence["anchor_id"]
        start = occurrence["start_offset"]
        end = occurrence["end_offset"]
        if anchor_id is not None and not isinstance(anchor_id, str):
            raise BlockContractError("glossary-occurrence-owner")
        if (
            coordinate_system != "source_projection"
            or not isinstance(block_id, str)
            or block_id not in blocks
            or anchor_id is not None
            and (anchor_id not in anchors or anchors[anchor_id].block_id != block_id)
            or not isinstance(start, int)
            or isinstance(start, bool)
            or not isinstance(end, int)
            or isinstance(end, bool)
            or start < 0
            or end <= start
        ):
            raise BlockContractError("glossary-occurrence-owner")
        block = blocks[block_id]
        if end > len(block.projection):
            raise BlockContractError("glossary-occurrence-range")
        eligible = [
            slot
            for slot in block.slots
            if slot.disposition == "translatable"
            and slot.kind not in {"template", "identifier"}
            and slot.projection_start is not None
            and slot.projection_end is not None
            and slot.projection_start < end
            and start < slot.projection_end
        ]
        covered_until = start
        for slot in sorted(eligible, key=lambda item: item.projection_start):
            if slot.projection_start > covered_until:
                break
            covered_until = max(covered_until, slot.projection_end)
        allowed_forms = [payload.source_term]
        if payload.usage is not None:
            allowed_forms.extend(payload.usage["source_forms"])
        if covered_until < end or normalized_term(block.projection[start:end]) not in {
            normalized_term(form, profile=revision.normalization_profile,
                            version=revision.normalization_version)
            for form in allowed_forms
        }:
            raise BlockContractError("glossary-occurrence-term")
        if anchor_id is not None:
            anchor = next(item for item in block.anchors if item.id == anchor_id)
            if not (anchor.start <= start and end <= anchor.end):
                raise BlockContractError("glossary-occurrence-anchor")
        occurrence_hash = sha256_text(
            canonical_json({"revision_id": revision.id, "block_id": block_id, "anchor_id": anchor_id, "start": start, "end": end})
        )
        if occurrence_hash in seen:
            continue
        seen.add(occurrence_hash)
        validated.append((block_id, anchor_id, start, end, occurrence_hash))

    # Validation must finish before the first insert/savepoint.  This also
    # makes a caller's rollback authoritative when a later element is bad.
    _ensure_database_transaction()
    rows = []
    for block_id, anchor_id, start, end, occurrence_hash in validated:
        row = cast(Any, TranslationGlossaryOccurrence)(
            id=str(uuid.uuid4()),
            revision_id=revision.id,
            concept_id=revision.concept_id,
            project_id=revision.project_id,
            source_plan_id=revision.source_plan_id,
            block_id=block_id,
            anchor_id=anchor_id,
            start_offset=start,
            end_offset=end,
            occurrence_hash=occurrence_hash,
            record_receipt="0" * 64,
        )
        row.record_receipt = _occurrence_receipt(row)
        try:
            # The non-null opaque identity index, not the nullable anchor
            # tuple, is the authoritative idempotency fence.  A savepoint
            # keeps a concurrent unique race from poisoning the caller UoW.
            with db.session.begin_nested():
                db.session.add(row)
                db.session.flush()
        except (IntegrityError, OperationalError) as exc:
            if isinstance(exc, OperationalError) and not is_database_locked(exc):
                raise
            existing = TranslationGlossaryOccurrence.query.filter_by(
                revision_id=revision.id, occurrence_hash=occurrence_hash
            ).one_or_none()
            if existing is None:
                if isinstance(exc, OperationalError) and is_database_locked(exc):
                    raise BlockContractError("glossary-occurrence-conflict") from exc
                raise
            row = existing
        _verify_occurrence(row)
        rows.append(row)
    db.session.flush()
    return tuple(rows)


def record_glossary_impact(
    *,
    old_revision: TranslationGlossaryRevision | None,
    new_revision: TranslationGlossaryRevision,
    block_ids: Iterable[str],
    reason: dict[str, object],
    state: str = "needs_review",
) -> tuple[TranslationContextImpact, ...]:
    record = _record(
        db.session.get(TranslationSourcePlanRecord, new_revision.source_plan_id)
    )
    rehydrate_glossary_revision(new_revision, record)
    if old_revision is not None and (
        old_revision.source_plan_id != record.id
        or old_revision.concept_id != new_revision.concept_id
    ):
        raise BlockContractError("glossary-impact-lineage")
    if old_revision is not None:
        rehydrate_glossary_revision(old_revision, record)
        if (
            old_revision.id == new_revision.id
            or old_revision.revision_number >= new_revision.revision_number
        ):
            raise BlockContractError("glossary-impact-lineage")
    if state not in {"needs_review", "retranslation_candidate"} or not isinstance(
        reason, dict
    ):
        raise BlockContractError("glossary-impact-state")
    # Materialize and JSON-canonicalize all caller input before any nested
    # savepoint.  A malformed later block ID or reason therefore leaves zero
    # impact rows for this call.
    known = {block.id for block in cast(Any, record.blocks)}
    try:
        canonical_reason = json.loads(canonical_json(reason))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise BlockContractError("glossary-impact-reason") from exc
    if not isinstance(canonical_reason, dict):
        raise BlockContractError("glossary-impact-reason")
    canonical_block_ids: set[str] = set()
    for block_id in block_ids:
        if not isinstance(block_id, str) or block_id not in known:
            raise BlockContractError("glossary-impact-block")
        canonical_block_ids.add(block_id)
    ordered_block_ids = sorted(canonical_block_ids)
    _ensure_database_transaction()
    result = []
    for block_id in ordered_block_ids:
        impact_key = sha256_text(canonical_json({
            "block_id": block_id,
            "old_glossary_revision_id": old_revision.id if old_revision else None,
            "new_glossary_revision_id": new_revision.id,
            "old_artifact_revision_id": None,
            "new_artifact_revision_id": None,
            "impact_state": state,
        }))
        row = cast(Any, TranslationContextImpact)(
            id=str(uuid.uuid4()),
            project_id=record.project_id,
            source_plan_id=record.id,
            block_id=block_id,
            old_glossary_revision_id=(old_revision.id if old_revision else None),
            new_glossary_revision_id=new_revision.id,
            reason_json=canonical_reason,
            impact_state=state,
            impact_key=impact_key,
            created_at=utc_now_db(),
            record_receipt="0" * 64,
        )
        row.record_receipt = _impact_receipt(row)
        try:
            with db.session.begin_nested():
                db.session.add(row)
                db.session.flush()
        except (IntegrityError, OperationalError) as exc:
            if isinstance(exc, OperationalError) and not is_database_locked(exc):
                raise
            existing = TranslationContextImpact.query.filter_by(
                source_plan_id=record.id, impact_key=impact_key
            ).one_or_none()
            if existing is None:
                if isinstance(exc, OperationalError) and is_database_locked(exc):
                    raise BlockContractError("glossary-impact-conflict") from exc
                raise
            if (
                existing.project_id != record.project_id
                or existing.source_plan_id != record.id
                or existing.block_id != block_id
                or existing.old_glossary_revision_id
                != (old_revision.id if old_revision else None)
                or existing.new_glossary_revision_id != new_revision.id
                or existing.impact_state != state
                or existing.reason_json != canonical_reason
            ):
                raise BlockContractError("glossary-impact-conflict")
            row = existing
        _verify_impact(row)
        result.append(row)
    db.session.flush()
    return tuple(result)


__all__ = [
    "activate_glossary_revision",
    "approve_glossary_proposal",
    "create_glossary_concept",
    "find_glossary_revisions_for_term",
    "glossary_lock_preview",
    "persist_analysis_glossary_proposals",
    "persist_glossary_occurrences",
    "persist_glossary_revision",
    "record_glossary_impact",
    "rehydrate_active_glossary_revision",
    "rehydrate_glossary_revision",
    "verify_context_impact",
    "verify_glossary_active",
    "verify_glossary_occurrence",
]
