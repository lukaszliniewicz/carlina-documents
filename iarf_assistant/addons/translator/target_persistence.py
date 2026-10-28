"""Durable append-only target revisions for the experimental block engine."""

from __future__ import annotations

import hmac
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, cast

from sqlalchemy import inspect, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload, selectinload

from carlina.core.semantic_html import SAFE_LANGUAGE_RE
from carlina.core.utc import utc_iso, utc_now_db
from iarf_assistant.extensions import db

from .analysis_contract import private_witness, private_witness_candidates
from .block_contract import (
    BlockContractError,
    TranslationSourcePlan,
    canonical_json,
    sha256_text,
)
from .block_notes import (
    ANNOTATION_PROFILE_RESTRAINED,
    ANNOTATION_PROFILE_STUDY,
    BlockNoteCandidate,
    ReaderNoteCandidate,
    make_note_receipt,
    notes_allowed_for_block,
    persisted_note_projection,
    validate_note_candidates,
)
from .block_persistence import rehydrate_source_plan
from .integrity_receipts import make_receipt, verify_receipt
from .models import (
    TranslationAlignmentRevision,
    TranslationReaderNote,
    TranslationShareGrant,
    TranslationTargetAlignmentActive,
    TranslationTargetBlockActive,
    TranslationTargetBlockNote,
    TranslationTargetBlockRevision,
    TranslationTargetRevisionShareAttribution,
    TranslationTargetSpanSet,
)
from .models import (
    TranslationSourcePlan as TranslationSourcePlanRecord,
)
from .rehydration_cache import TranslationRehydrationCache
from .target_contract import (
    TARGET_BLOCK_RESULT_CONTRACT_VERSION,
    TargetBlockCandidate,
    normalize_target_candidate,
    structural_fingerprint,
    target_text,
)
from .target_rendering import render_target_block_fragment, render_target_document
from .target_validation import (
    TARGET_BLOCK_VALIDATOR_VERSION,
    require_valid_target_candidate,
)

_AUTHOR_TYPES = frozenset({"human", "reviewer", "model", "system"})
_MAX_TARGET_REVISION_LINEAGE_DEPTH = 64
_SHARE_ATTRIBUTION_RECEIPT_PURPOSE = (
    "translator-target-revision-share-attribution-receipt-v1"
)

_TARGET_CANDIDATE_WITNESS_PURPOSE = "translator-target-candidate-v1"
_TARGET_NORMALIZED_WITNESS_PURPOSE = "translator-target-normalized-v1"
_TARGET_FRAGMENT_WITNESS_PURPOSE = "translator-target-fragment-v1"
_TARGET_EVIDENCE_WITNESS_PURPOSE = "translator-target-evidence-v1"


def _target_witness(value: object, *, purpose: str) -> str:
    """Return a retained-key witness for source-bearing target material."""

    return private_witness(value, purpose=purpose)


def _target_witness_valid(stored: object, value: object, *, purpose: str) -> bool:
    return isinstance(stored, str) and stored in private_witness_candidates(
        value, purpose=purpose
    )


def _pair_receipt(
    pointer: TranslationTargetBlockActive,
    alignment_pointer: TranslationTargetAlignmentActive,
    *,
    session: Any | None = None,
    revision: TranslationTargetBlockRevision | None = None,
    alignment: TranslationAlignmentRevision | None = None,
) -> str:
    active_session = session or db.session
    if (revision is None) != (alignment is None):
        raise BlockContractError("target-active-pair")
    if revision is None or alignment is None:
        alignment = active_session.get(
            TranslationAlignmentRevision, alignment_pointer.alignment_revision_id
        )
        revision = active_session.get(
            TranslationTargetBlockRevision, pointer.revision_id
        )
    if alignment is None or revision is None:
        raise BlockContractError("target-active-pair")
    return make_receipt(
        purpose="translator-target-active-pair-v1",
        values={
            "plan": pointer.source_plan_id,
            "block": pointer.block_id,
            "revision_id": pointer.revision_id,
            "revision_receipt": revision.revision_record_hash,
            "alignment_id": alignment.id,
            "alignment_receipt": alignment.record_hash,
            "version": pointer.version,
            "target_updated_at": utc_iso(pointer.updated_at, timespec="microseconds"),
            "alignment_updated_at": utc_iso(
                alignment_pointer.updated_at, timespec="microseconds"
            ),
        },
    )


def verify_active_pair(
    pointer: TranslationTargetBlockActive,
    alignment_pointer: TranslationTargetAlignmentActive,
    *,
    session: Any | None = None,
    revision: TranslationTargetBlockRevision | None = None,
    alignment: TranslationAlignmentRevision | None = None,
) -> None:
    if (
        pointer.source_plan_id != alignment_pointer.source_plan_id
        or pointer.block_id != alignment_pointer.block_id
        or pointer.revision_id != alignment_pointer.revision_id
        or pointer.version != alignment_pointer.version
    ):
        raise BlockContractError("target-active-pair")
    if (revision is None) != (alignment is None):
        raise BlockContractError("target-active-pair")
    if revision is None or alignment is None:
        active_session = session or db.session
        revision = active_session.get(
            TranslationTargetBlockRevision,
            pointer.revision_id,
            populate_existing=True,
        )
        alignment = active_session.get(
            TranslationAlignmentRevision,
            alignment_pointer.alignment_revision_id,
            populate_existing=True,
        )
    if revision is None or alignment is None:
        raise BlockContractError("target-active-pair")
    if (
        revision.id != pointer.revision_id
        or revision.source_plan_id != pointer.source_plan_id
        or revision.block_id != pointer.block_id
        or alignment.id != alignment_pointer.alignment_revision_id
        or alignment.source_plan_id != pointer.source_plan_id
        or alignment.block_id != pointer.block_id
        or alignment.revision_id != pointer.revision_id
    ):
        raise BlockContractError("target-active-pair")
    verify_receipt(
        stored=pointer.pair_receipt,
        purpose="translator-target-active-pair-v1",
        values={
            "plan": pointer.source_plan_id,
            "block": pointer.block_id,
            "revision_id": pointer.revision_id,
            "revision_receipt": revision.revision_record_hash,
            "alignment_id": alignment_pointer.alignment_revision_id,
            "alignment_receipt": alignment.record_hash,
            "version": pointer.version,
            "target_updated_at": utc_iso(pointer.updated_at, timespec="microseconds"),
            "alignment_updated_at": utc_iso(
                alignment_pointer.updated_at, timespec="microseconds"
            ),
        },
        code="target-active-pair",
    )


def _ensure_database_transaction() -> None:
    """Give SQLite a real outer transaction before a nested savepoint."""

    session = db.session()
    connection = session.connection()
    if connection.dialect.name != "sqlite":
        return
    proxied = connection.connection
    driver = getattr(proxied, "driver_connection", proxied)
    if not bool(getattr(driver, "in_transaction", False)):
        connection.exec_driver_sql("BEGIN")


def _plan_and_block(
    record: TranslationSourcePlanRecord,
    block_id: str,
    *,
    verified_plan: TranslationSourcePlan | None = None,
) -> tuple[TranslationSourcePlan, Any]:
    if not isinstance(record, TranslationSourcePlanRecord):
        raise BlockContractError("target-source-plan-type")
    state = inspect(record)
    if state is None or not state.persistent:
        raise BlockContractError("target-source-plan-persistent")
    plan = verified_plan or rehydrate_source_plan(record)
    if (
        plan.project_id != record.project_id
        or plan.plan_hash != record.plan_hash
        or plan.snapshot.document_id != record.source_document_id
    ):
        raise BlockContractError("target-source-plan-pin")
    block = next((item for item in plan.blocks if item.id == block_id), None)
    if block is None:
        raise BlockContractError("target-block-owner")
    return plan, block


def _author(
    author_type: object,
    author_user_id: object,
    source_plan: TranslationSourcePlanRecord,
) -> tuple[str, int | None]:
    if not isinstance(author_type, str) or author_type not in _AUTHOR_TYPES:
        raise BlockContractError("target-author-type")
    project = cast(Any, source_plan.project)
    if (
        project is None
        or isinstance(project.user_id, bool)
        or not isinstance(project.user_id, int)
    ):
        raise BlockContractError("target-author-owner")
    if author_type == "reviewer" and author_user_id is not None:
        raise BlockContractError("target-author-user")
    if author_user_id is None:
        if author_type == "human":
            raise BlockContractError("target-author-user")
        return author_type, None
    if (
        isinstance(author_user_id, bool)
        or not isinstance(author_user_id, int)
        or author_user_id < 1
    ):
        raise BlockContractError("target-author-user")
    if author_user_id != project.user_id:
        raise BlockContractError("target-author-owner")
    return author_type, author_user_id


def _target_language(
    target_language: object, source_plan: TranslationSourcePlanRecord
) -> str:
    project = cast(Any, source_plan.project)
    if (
        project is None
        or not isinstance(project.target_lang, str)
        or not SAFE_LANGUAGE_RE.fullmatch(project.target_lang)
        or not isinstance(target_language, str)
        or not SAFE_LANGUAGE_RE.fullmatch(target_language)
        or target_language != project.target_lang
    ):
        raise BlockContractError("target-language-project-mismatch")
    return target_language


def _revision_record_payload(
    *,
    revision_id: str,
    project_id: str,
    owner_user_id: int,
    source_plan_id: str,
    block_id: str,
    source_plan_hash: str,
    structural_fingerprint: str,
    contract_version: str,
    validator_version: str,
    parent_revision_id: str | None,
    parent_revision_hash: str | None,
    author_type: str,
    author_user_id: int | None,
    validation_state: str,
    target_language: str,
    candidate_hash: str,
    normalized_hash: str,
    fragment_hash: str,
    evidence_hash: str,
    created_at: str,
    normalized_candidate: object,
    rendered_fragment: object,
    validation_evidence: object,
    source_derived_target_text: object,
) -> dict[str, object]:
    return {
        "revision_id": revision_id,
        "project_id": project_id,
        "owner_user_id": owner_user_id,
        "source_plan_id": source_plan_id,
        "block_id": block_id,
        "source_plan_hash": source_plan_hash,
        "structural_fingerprint": structural_fingerprint,
        "contract_version": contract_version,
        "validator_version": validator_version,
        "parent_revision_id": parent_revision_id,
        "parent_revision_hash": parent_revision_hash,
        "author_type": author_type,
        "author_user_id": author_user_id,
        "validation_state": validation_state,
        "target_language": target_language,
        "candidate_hash": candidate_hash,
        "normalized_hash": normalized_hash,
        "fragment_hash": fragment_hash,
        "evidence_hash": evidence_hash,
        "created_at": created_at,
        "normalized_candidate": normalized_candidate,
        "rendered_fragment": rendered_fragment,
        "validation_evidence": validation_evidence,
        "source_derived_target_text": source_derived_target_text,
    }


def _revision_record_hash(**values: Any) -> str:
    return make_receipt(
        purpose="translator-target-revision-receipt-v1",
        values={"receipt_version": 1, **_revision_record_payload(**values)},
    )


def _revision_record_valid(stored: object, **values: Any) -> bool:
    try:
        verify_receipt(
            stored=stored,
            purpose="translator-target-revision-receipt-v1",
            values={"receipt_version": 1, **_revision_record_payload(**values)},
            code="target-revision-hash",
        )
    except BlockContractError:
        return False
    return True


def _revision_hash(value: object, code: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise BlockContractError(code)
    return value


def _created_at_identity(value: object) -> str:
    if not isinstance(value, datetime):
        raise BlockContractError("target-revision-created-at")
    return utc_iso(value, timespec="microseconds")


def _parent(
    parent_revision_id: str | None,
    record: TranslationSourcePlanRecord,
    block_id: str,
) -> tuple[str | None, str | None]:
    if parent_revision_id is None:
        return None, None
    if not isinstance(parent_revision_id, str) or not parent_revision_id:
        raise BlockContractError("target-parent-id")
    parent = db.session.get(TranslationTargetBlockRevision, parent_revision_id)
    if parent is None:
        raise BlockContractError("target-parent-missing")
    if parent.source_plan_id != record.id or parent.block_id != block_id:
        raise BlockContractError("target-parent-owner")
    rehydrate_target_revision(parent, record)
    return parent_revision_id, _revision_hash(
        parent.revision_record_hash, "target-parent-hash"
    )


def _revision_payload(
    plan: TranslationSourcePlan,
    record: TranslationSourcePlanRecord,
    block: Any,
    candidate: TargetBlockCandidate,
    *,
    target_language: str,
) -> tuple[dict[str, object], str, dict[str, object], str]:
    validation = require_valid_target_candidate(plan, record.id, block, candidate)
    fragment = render_target_block_fragment(
        plan, record.id, block, candidate, target_language=target_language
    )
    evidence = validation.evidence()
    return candidate.to_dict(), fragment, evidence, target_text(candidate)


def _coerce_note(value: object, block_id: str, order_index: int) -> BlockNoteCandidate:
    if isinstance(value, BlockNoteCandidate):
        if value.block_id != block_id or value.order_index != order_index:
            raise BlockContractError("block-note-owner")
        return value
    if isinstance(value, Mapping) and set(value) == {"anchor", "body"}:
        return BlockNoteCandidate(
            block_id=block_id,
            anchor_text=value["anchor"],
            body=value["body"],
            order_index=order_index,
        )
    raise BlockContractError("block-note-shape")


def _persist_target_notes(
    source_plan: TranslationSourcePlanRecord,
    revision: TranslationTargetBlockRevision,
    block: Any,
    notes: Sequence[BlockNoteCandidate | Mapping[str, Any]],
    *,
    annotation_profile: object = ANNOTATION_PROFILE_RESTRAINED,
) -> None:
    if not notes:
        return
    if not notes_allowed_for_block(block):
        raise BlockContractError("block-note-attribute")
    if revision.author_type != "model":
        raise BlockContractError("block-note-author")
    normalized = tuple(
        _coerce_note(value, block.id, index) for index, value in enumerate(notes)
    )
    validate_note_candidates(
        normalized,
        {block.id: revision.rendered_fragment},
        annotation_profile=annotation_profile,
    )
    for note in normalized:
        created_at = utc_now_db()
        db.session.add(
            TranslationTargetBlockNote(
                project_id=source_plan.project_id,
                source_plan_id=source_plan.id,
                block_id=block.id,
                revision_id=revision.id,
                order_index=note.order_index,
                anchor_text=note.anchor_text,
                body=note.body,
                author_type="model",
                created_at=created_at,
                record_hash=make_note_receipt(
                    project_id=source_plan.project_id,
                    source_plan_id=source_plan.id,
                    block_id=block.id,
                    revision_id=revision.id,
                    order_index=note.order_index,
                    anchor_text=note.anchor_text,
                    body=note.body,
                    author_type="model",
                    created_at=created_at,
                ),
            )
        )


def persist_target_revision(
    source_plan: TranslationSourcePlanRecord,
    candidate: Mapping[str, Any] | TargetBlockCandidate,
    *,
    target_language: str,
    author_type: str,
    author_user_id: int | None = None,
    parent_revision_id: str | None = None,
    verified_plan: TranslationSourcePlan | None = None,
    notes: Sequence[BlockNoteCandidate | Mapping[str, Any]] = (),
    annotation_profile: object = ANNOTATION_PROFILE_RESTRAINED,
) -> TranslationTargetBlockRevision:
    """Append a valid target revision without committing the caller transaction."""

    normalized = normalize_target_candidate(candidate)
    plan, block = _plan_and_block(
        source_plan,
        normalized.block_id,
        verified_plan=verified_plan,
    )
    author_type, author_user_id = _author(author_type, author_user_id, source_plan)
    target_language = _target_language(target_language, source_plan)
    parent_revision_id, parent_revision_hash = _parent(
        parent_revision_id, source_plan, block.id
    )
    payload, fragment, evidence, derived_text = _revision_payload(
        plan, source_plan, block, normalized, target_language=target_language
    )
    _ensure_database_transaction()
    revision_id = str(uuid.uuid4())
    source_plan_hash = plan.plan_hash
    fingerprint = structural_fingerprint(plan, block)
    candidate_witness = _target_witness(
        payload, purpose=_TARGET_CANDIDATE_WITNESS_PURPOSE
    )
    normalized_witness = _target_witness(
        payload, purpose=_TARGET_NORMALIZED_WITNESS_PURPOSE
    )
    fragment_witness = _target_witness(
        fragment, purpose=_TARGET_FRAGMENT_WITNESS_PURPOSE
    )
    evidence_witness = _target_witness(
        evidence, purpose=_TARGET_EVIDENCE_WITNESS_PURPOSE
    )
    created_at = utc_now_db()
    created_at_identity = _created_at_identity(created_at)
    revision = cast(Any, TranslationTargetBlockRevision)(
        id=revision_id,
        source_plan_id=source_plan.id,
        block_id=block.id,
        source_plan_hash=source_plan_hash,
        structural_fingerprint=fingerprint,
        contract_version=TARGET_BLOCK_RESULT_CONTRACT_VERSION,
        validator_version=TARGET_BLOCK_VALIDATOR_VERSION,
        parent_revision_id=parent_revision_id,
        parent_revision_hash=parent_revision_hash,
        author_type=author_type,
        author_user_id=author_user_id,
        validation_state="valid",
        target_language=target_language,
        normalized_candidate=payload,
        rendered_fragment=fragment,
        validation_evidence=evidence,
        source_derived_target_text=derived_text,
        candidate_hash=candidate_witness,
        normalized_hash=normalized_witness,
        fragment_hash=fragment_witness,
        evidence_hash=evidence_witness,
        created_at=created_at,
        revision_record_hash=_revision_record_hash(
            revision_id=revision_id,
            project_id=source_plan.project_id,
            owner_user_id=cast(Any, source_plan.project).user_id,
            source_plan_id=source_plan.id,
            block_id=block.id,
            source_plan_hash=source_plan_hash,
            structural_fingerprint=fingerprint,
            contract_version=TARGET_BLOCK_RESULT_CONTRACT_VERSION,
            validator_version=TARGET_BLOCK_VALIDATOR_VERSION,
            parent_revision_id=parent_revision_id,
            parent_revision_hash=parent_revision_hash,
            author_type=author_type,
            author_user_id=author_user_id,
            validation_state="valid",
            target_language=target_language,
            candidate_hash=candidate_witness,
            normalized_hash=normalized_witness,
            fragment_hash=fragment_witness,
            evidence_hash=evidence_witness,
            created_at=created_at_identity,
            normalized_candidate=payload,
            rendered_fragment=fragment,
            validation_evidence=evidence,
            source_derived_target_text=derived_text,
        ),
    )
    with db.session.begin_nested():
        db.session.add(revision)
        db.session.flush()
        _persist_target_notes(
            source_plan,
            revision,
            block,
            notes,
            annotation_profile=annotation_profile,
        )
        db.session.flush()
    return revision


def persist_target_revision_share_attribution(
    source_plan: TranslationSourcePlanRecord,
    revision: TranslationTargetBlockRevision,
    *,
    share_grant_id: str,
) -> TranslationTargetRevisionShareAttribution:
    """Append immutable provenance for one accountless reviewer revision."""

    if (
        not isinstance(share_grant_id, str)
        or not share_grant_id
        or revision.source_plan_id != source_plan.id
        or not isinstance(revision.block_id, str)
        or not revision.block_id
        or revision.author_type != "reviewer"
        or revision.author_user_id is not None
    ):
        raise BlockContractError("candidate-authority")
    grant = db.session.get(TranslationShareGrant, share_grant_id)
    project = cast(Any, source_plan.project)
    now = utc_now_db()
    try:
        expired = (
            grant is not None
            and grant.expires_at is not None
            and grant.expires_at <= now
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise BlockContractError("candidate-authority") from exc
    if (
        grant is None
        or grant.id != share_grant_id
        or grant.project_id != source_plan.project_id
        or project is None
        or grant.owner_user_id != project.user_id
        or grant.role != "reviewer"
        or grant.status != "active"
        or grant.revoked_at is not None
        or expired
    ):
        raise BlockContractError("candidate-authority")
    created_at = utc_now_db()
    attribution_id = str(uuid.uuid4())
    public_values = {
        "project_id": source_plan.project_id,
        "source_plan_id": source_plan.id,
        "block_id": revision.block_id,
        "target_revision_id": revision.id,
        "share_grant_id": share_grant_id,
        "created_at": _created_at_identity(created_at),
    }
    record_hash = sha256_text(canonical_json(public_values))
    attribution = cast(Any, TranslationTargetRevisionShareAttribution)(
        id=attribution_id,
        project_id=source_plan.project_id,
        source_plan_id=source_plan.id,
        block_id=revision.block_id,
        target_revision_id=revision.id,
        share_grant_id=share_grant_id,
        created_at=created_at,
        record_hash=record_hash,
        integrity_receipt=make_receipt(
            purpose=_SHARE_ATTRIBUTION_RECEIPT_PURPOSE,
            values={
                "receipt_version": 1,
                "id": attribution_id,
                **public_values,
                "record_hash": record_hash,
            },
        ),
    )
    db.session.add(attribution)
    db.session.flush()
    verify_target_revision_share_attribution(attribution)
    return attribution


def verify_target_revision_share_attribution(
    attribution: TranslationTargetRevisionShareAttribution,
) -> TranslationTargetRevisionShareAttribution:
    """Verify immutable reviewer attribution against its retained-key receipt."""

    public_values = {
        "project_id": attribution.project_id,
        "source_plan_id": attribution.source_plan_id,
        "block_id": attribution.block_id,
        "target_revision_id": attribution.target_revision_id,
        "share_grant_id": attribution.share_grant_id,
        "created_at": _created_at_identity(attribution.created_at),
    }
    record_hash = sha256_text(canonical_json(public_values))
    if not hmac.compare_digest(attribution.record_hash, record_hash):
        raise BlockContractError("candidate-share-attribution")
    verify_receipt(
        stored=attribution.integrity_receipt,
        purpose=_SHARE_ATTRIBUTION_RECEIPT_PURPOSE,
        values={
            "receipt_version": 1,
            "id": attribution.id,
            **public_values,
            "record_hash": record_hash,
        },
        code="candidate-share-attribution",
    )
    return attribution


def rehydrate_target_revision(
    revision: TranslationTargetBlockRevision,
    source_plan: TranslationSourcePlanRecord | None = None,
    *,
    session: Any | None = None,
    verified_plan: TranslationSourcePlan | None = None,
    verification_cache: TranslationRehydrationCache | None = None,
) -> TargetBlockCandidate:
    """Decrypt and fully revalidate a durable revision before it is trusted."""

    return _rehydrate_target_revision(
        revision,
        source_plan,
        lineage=frozenset(),
        depth=0,
        session=session,
        verified_plan=verified_plan,
        verification_cache=verification_cache,
    )


def _rehydrate_target_revision(
    revision: TranslationTargetBlockRevision,
    source_plan: TranslationSourcePlanRecord | None,
    *,
    lineage: frozenset[str],
    depth: int,
    session: Any | None,
    verified_plan: TranslationSourcePlan | None,
    verification_cache: TranslationRehydrationCache | None,
) -> TargetBlockCandidate:
    """Verify one append-only revision and its witnessed parent lineage."""

    try:
        if depth >= _MAX_TARGET_REVISION_LINEAGE_DEPTH:
            raise BlockContractError("target-revision-lineage-depth")
        if not isinstance(revision, TranslationTargetBlockRevision):
            raise BlockContractError("target-revision-type")
        if not isinstance(revision.id, str) or not revision.id:
            raise BlockContractError("target-revision-id")
        if revision.id in lineage:
            raise BlockContractError("target-revision-lineage-cycle")
        record = cast(TranslationSourcePlanRecord, source_plan or revision.source_plan)
        if record is None or record.id != revision.source_plan_id:
            raise BlockContractError("target-revision-plan")
        cache_key = (record.id, revision.id)
        if verification_cache is not None:
            cached = verification_cache.target_candidates.get(cache_key)
            if cached is not None:
                return cast(TargetBlockCandidate, cached)
        plan, block = _plan_and_block(
            record,
            revision.block_id,
            verified_plan=verified_plan,
        )
        parent_revision_id = revision.parent_revision_id
        parent_revision_hash = revision.parent_revision_hash
        if (parent_revision_id is None) != (parent_revision_hash is None):
            raise BlockContractError("target-revision-parent-witness")
        if parent_revision_id is not None:
            if not isinstance(parent_revision_id, str) or not parent_revision_id:
                raise BlockContractError("target-revision-parent")
            witnessed_parent_hash = _revision_hash(
                parent_revision_hash, "target-revision-parent-hash"
            )
            active_session = session or db.session
            parent = active_session.get(
                TranslationTargetBlockRevision, parent_revision_id
            )
            if (
                parent is None
                or parent.source_plan_id != record.id
                or parent.block_id != block.id
            ):
                raise BlockContractError("target-revision-parent")
            _rehydrate_target_revision(
                parent,
                record,
                lineage=lineage | {revision.id},
                depth=depth + 1,
                session=active_session,
                verified_plan=plan,
                verification_cache=verification_cache,
            )
            if parent.revision_record_hash != witnessed_parent_hash:
                raise BlockContractError("target-revision-parent-witness")
        candidate = normalize_target_candidate(revision.normalized_candidate)
        _author(revision.author_type, revision.author_user_id, record)
        target_language = _target_language(revision.target_language, record)
        created_at_identity = _created_at_identity(revision.created_at)
        if revision.validation_state != "valid":
            raise BlockContractError("target-revision-state")
        if (
            revision.contract_version != TARGET_BLOCK_RESULT_CONTRACT_VERSION
            or revision.validator_version != TARGET_BLOCK_VALIDATOR_VERSION
            or revision.source_plan_hash != plan.plan_hash
            or revision.structural_fingerprint != structural_fingerprint(plan, block)
            or not _target_witness_valid(
                revision.candidate_hash,
                candidate.to_dict(),
                purpose=_TARGET_CANDIDATE_WITNESS_PURPOSE,
            )
            or not _target_witness_valid(
                revision.normalized_hash,
                candidate.to_dict(),
                purpose=_TARGET_NORMALIZED_WITNESS_PURPOSE,
            )
            or not _revision_record_valid(
                revision.revision_record_hash,
                revision_id=revision.id,
                project_id=record.project_id,
                owner_user_id=cast(Any, record.project).user_id,
                source_plan_id=revision.source_plan_id,
                block_id=revision.block_id,
                source_plan_hash=revision.source_plan_hash,
                structural_fingerprint=revision.structural_fingerprint,
                contract_version=revision.contract_version,
                validator_version=revision.validator_version,
                parent_revision_id=revision.parent_revision_id,
                parent_revision_hash=revision.parent_revision_hash,
                author_type=revision.author_type,
                author_user_id=revision.author_user_id,
                validation_state=revision.validation_state,
                target_language=target_language,
                candidate_hash=revision.candidate_hash,
                normalized_hash=revision.normalized_hash,
                fragment_hash=revision.fragment_hash,
                evidence_hash=revision.evidence_hash,
                created_at=created_at_identity,
                normalized_candidate=revision.normalized_candidate,
                rendered_fragment=revision.rendered_fragment,
                validation_evidence=revision.validation_evidence,
                source_derived_target_text=revision.source_derived_target_text,
            )
        ):
            raise BlockContractError("target-revision-hash")
        validation = require_valid_target_candidate(plan, record.id, block, candidate)
        if (
            not isinstance(revision.validation_evidence, Mapping)
            or canonical_json(revision.validation_evidence)
            != canonical_json(validation.evidence())
            or not _target_witness_valid(
                revision.evidence_hash,
                validation.evidence(),
                purpose=_TARGET_EVIDENCE_WITNESS_PURPOSE,
            )
        ):
            raise BlockContractError("target-revision-evidence")
        fragment = render_target_block_fragment(
            plan, record.id, block, candidate, target_language=target_language
        )
        if (
            revision.rendered_fragment != fragment
            or not _target_witness_valid(
                revision.fragment_hash,
                fragment,
                purpose=_TARGET_FRAGMENT_WITNESS_PURPOSE,
            )
            or revision.source_derived_target_text != target_text(candidate)
        ):
            raise BlockContractError("target-revision-render")
        if verification_cache is not None:
            verification_cache.target_candidates[cache_key] = candidate
        return candidate
    except BlockContractError:
        raise
    except Exception as exc:
        raise BlockContractError("target-revision-corrupt") from exc


def rehydrate_target_block_notes(
    revision: TranslationTargetBlockRevision,
    source_plan: TranslationSourcePlanRecord,
    *,
    rendered_fragment: str | None = None,
    rows: Sequence[TranslationTargetBlockNote] | None = None,
    verified_plan: TranslationSourcePlan | None = None,
    verification_cache: TranslationRehydrationCache | None = None,
) -> tuple[BlockNoteCandidate, ...]:
    """Verify active-revision note rows before allowing projection/rendering."""

    rehydrate_target_revision(
        revision,
        source_plan,
        verified_plan=verified_plan,
        verification_cache=verification_cache,
    )
    fragment = (
        revision.rendered_fragment if rendered_fragment is None else rendered_fragment
    )
    if rows is None:
        rows = (
            TranslationTargetBlockNote.query.filter_by(
                project_id=source_plan.project_id,
                source_plan_id=source_plan.id,
                block_id=revision.block_id,
                revision_id=revision.id,
            )
            .order_by(
                TranslationTargetBlockNote.order_index,
                TranslationTargetBlockNote.id,
            )
            .all()
        )
    else:
        if any(
            row.project_id != source_plan.project_id
            or row.source_plan_id != source_plan.id
            or row.block_id != revision.block_id
            or row.revision_id != revision.id
            for row in rows
        ):
            raise BlockContractError("block-note-owner")
        rows = tuple(sorted(rows, key=lambda row: (row.order_index, str(row.id))))
    if rows:
        plan = verified_plan or rehydrate_source_plan(source_plan)
        block = next(
            (item for item in plan.blocks if item.id == revision.block_id), None
        )
        if block is None:
            raise BlockContractError("block-note-owner")
        if not notes_allowed_for_block(block):
            raise BlockContractError("block-note-attribute")
    # Generation policy was enforced from the frozen request. Rehydration uses
    # the structural Study ceiling so later mutable project settings cannot
    # invalidate authentic historical notes.
    persisted_note_projection(
        rows,
        project_id=source_plan.project_id,
        source_plan_id=source_plan.id,
        block_id=revision.block_id,
        revision_id=revision.id,
        rendered_fragment=fragment,
        annotation_profile=ANNOTATION_PROFILE_STUDY,
    )
    return tuple(
        BlockNoteCandidate(
            block_id=revision.block_id,
            anchor_text=row.anchor_text,
            body=row.body,
            order_index=row.order_index,
        )
        for row in rows
    )


def _maintain_reader_notes_after_activation(
    source_plan: TranslationSourcePlanRecord,
    plan: TranslationSourcePlan,
    block: Any,
    revision: TranslationTargetBlockRevision,
    candidate: TargetBlockCandidate,
    pointer: TranslationTargetBlockActive,
) -> None:
    """Materialize and revalidate reader notes for a newly active target.

    Activation is the persistence boundary for the target and its derived
    scholarly-review projection.  Keep the already verified source/target
    context on the same savepoint so a review projection failure cannot leave
    an active pointer without its corresponding note maintenance.
    """

    # Local import avoids making target-revision persistence a module-level
    # dependency of the scholarly-review service, which imports this module for
    # target verification.
    from . import scholarly_review

    project = cast(Any, source_plan.project)
    current_target = (pointer, revision, candidate)
    try:
        scholarly_review.materialize_model_reader_notes(
            project,
            revision,
            _source_plan=source_plan,
            _blocks=plan.blocks,
            _current_target=current_target,
            _verified_plan=plan,
        )
        scholarly_review.revalidate_reader_notes_for_target(
            project,
            block_id=str(block.id),
            target_revision_id=str(revision.id),
            target_version=int(pointer.version),
            _source_plan=source_plan,
            _blocks=plan.blocks,
            _current_target=current_target,
        )
    except scholarly_review.ScholarlyReviewError as exc:
        raise BlockContractError("target-reader-note-maintenance") from exc


def activate_target_revision(
    source_plan: TranslationSourcePlanRecord,
    *,
    block_id: str,
    revision_id: str,
    alignment_revision_id: str,
    expected_version: int,
    verified_plan: TranslationSourcePlan | None = None,
) -> TranslationTargetBlockActive:
    """Atomically CAS a target pointer and its complete alignment witness."""

    if (
        isinstance(expected_version, bool)
        or not isinstance(expected_version, int)
        or expected_version < 0
    ):
        raise BlockContractError("target-active-version")
    plan, block = _plan_and_block(
        source_plan,
        block_id,
        verified_plan=verified_plan,
    )
    revision = db.session.get(TranslationTargetBlockRevision, revision_id)
    if (
        revision is None
        or revision.source_plan_id != source_plan.id
        or revision.block_id != block.id
    ):
        raise BlockContractError("target-active-revision-owner")
    candidate = rehydrate_target_revision(
        revision, source_plan, verified_plan=plan
    )
    # Local import keeps target-revision persistence independent of the
    # Phase-4 substrate until activation; the latter intentionally depends on
    # the former, never vice versa.
    from .alignment_persistence import (
        rehydrate_alignment_revision,
    )
    from .models import TranslationAlignmentRevision

    alignment = db.session.get(TranslationAlignmentRevision, alignment_revision_id)
    if (
        alignment is None
        or alignment.source_plan_id != source_plan.id
        or alignment.block_id != block.id
        or alignment.revision_id != revision.id
    ):
        raise BlockContractError("target-active-alignment-owner")
    rehydrate_alignment_revision(alignment, source_plan, verified_plan=plan)
    lock_target_dependency_scope(source_plan.id)
    _ensure_database_transaction()
    pointer = db.session.get(
        TranslationTargetBlockActive,
        {"source_plan_id": source_plan.id, "block_id": block.id},
    )
    alignment_pointer = db.session.get(
        TranslationTargetAlignmentActive,
        {"source_plan_id": source_plan.id, "block_id": block.id},
    )
    if pointer is None:
        if expected_version != 0 or alignment_pointer is not None:
            raise BlockContractError("target-active-conflict")
        now = utc_now_db()
        pointer = cast(Any, TranslationTargetBlockActive)(
            source_plan_id=source_plan.id,
            block_id=block.id,
            revision_id=revision.id,
            version=1,
            pair_receipt="0" * 64,
            updated_at=now,
        )
        try:
            with db.session.begin_nested():
                db.session.add(pointer)
                db.session.add(
                    cast(Any, TranslationTargetAlignmentActive)(
                        source_plan_id=source_plan.id,
                        block_id=block.id,
                        revision_id=revision.id,
                        alignment_revision_id=alignment.id,
                        version=1,
                        updated_at=now,
                    )
                )
                db.session.flush()
                current_alignment = db.session.get(
                    TranslationTargetAlignmentActive,
                    {"source_plan_id": source_plan.id, "block_id": block.id},
                )
                if current_alignment is None:
                    raise BlockContractError("target-active-conflict")
                db.session.refresh(pointer)
                db.session.refresh(current_alignment)
                receipt = _pair_receipt(pointer, current_alignment)
                sealed = db.session.execute(
                    update(TranslationTargetBlockActive)
                    .where(
                        TranslationTargetBlockActive.source_plan_id == source_plan.id,
                        TranslationTargetBlockActive.block_id == block.id,
                        TranslationTargetBlockActive.revision_id == revision.id,
                        TranslationTargetBlockActive.version == 1,
                    )
                    .values(pair_receipt=receipt, updated_at=now)
                )
                if getattr(sealed, "rowcount", None) != 1:
                    raise BlockContractError("target-active-conflict")
                db.session.flush()
                db.session.refresh(pointer)
                db.session.refresh(current_alignment)
                verify_active_pair(pointer, current_alignment)
                _maintain_reader_notes_after_activation(
                    source_plan,
                    plan,
                    block,
                    revision,
                    candidate,
                    pointer,
                )
        except IntegrityError as exc:
            raise BlockContractError("target-active-conflict") from exc
        return pointer
    if (
        pointer.version != expected_version
        or alignment_pointer is None
        or alignment_pointer.version != expected_version
        or alignment_pointer.revision_id != pointer.revision_id
    ):
        raise BlockContractError("target-active-conflict")
    previous_revision_id = pointer.revision_id
    now = utc_now_db()
    with db.session.begin_nested():
        changed = db.session.execute(
            update(TranslationTargetBlockActive)
            .where(
                TranslationTargetBlockActive.source_plan_id == source_plan.id,
                TranslationTargetBlockActive.block_id == block.id,
                TranslationTargetBlockActive.version == expected_version,
            )
            .values(
                revision_id=revision.id, version=expected_version + 1, updated_at=now
            )
        )
        if getattr(changed, "rowcount", None) != 1:
            raise BlockContractError("target-active-conflict")
        aligned = db.session.execute(
            update(TranslationTargetAlignmentActive)
            .where(
                TranslationTargetAlignmentActive.source_plan_id == source_plan.id,
                TranslationTargetAlignmentActive.block_id == block.id,
                TranslationTargetAlignmentActive.version == expected_version,
                TranslationTargetAlignmentActive.revision_id == previous_revision_id,
            )
            .values(
                revision_id=revision.id,
                alignment_revision_id=alignment.id,
                version=expected_version + 1,
                updated_at=now,
            )
        )
        if getattr(aligned, "rowcount", None) != 1:
            raise BlockContractError("target-active-conflict")
        db.session.flush()
        refreshed_pointer = db.session.get(
            TranslationTargetBlockActive,
            {"source_plan_id": source_plan.id, "block_id": block.id},
        )
        refreshed_alignment = db.session.get(
            TranslationTargetAlignmentActive,
            {"source_plan_id": source_plan.id, "block_id": block.id},
        )
        if refreshed_pointer is None or refreshed_alignment is None:
            raise BlockContractError("target-active-conflict")
        db.session.refresh(refreshed_pointer)
        db.session.refresh(refreshed_alignment)
        receipt = _pair_receipt(refreshed_pointer, refreshed_alignment)
        sealed = db.session.execute(
            update(TranslationTargetBlockActive)
            .where(
                TranslationTargetBlockActive.source_plan_id == source_plan.id,
                TranslationTargetBlockActive.block_id == block.id,
                TranslationTargetBlockActive.revision_id == revision.id,
                TranslationTargetBlockActive.version == expected_version + 1,
            )
            .values(pair_receipt=receipt, updated_at=now)
        )
        if getattr(sealed, "rowcount", None) != 1:
            raise BlockContractError("target-active-conflict")
        db.session.flush()
        db.session.refresh(refreshed_pointer)
        db.session.refresh(refreshed_alignment)
        verify_active_pair(refreshed_pointer, refreshed_alignment)
        _maintain_reader_notes_after_activation(
            source_plan,
            plan,
            block,
            revision,
            candidate,
            refreshed_pointer,
        )
    db.session.refresh(pointer)
    return pointer


def activate_initial_target_revisions(
    source_plan: TranslationSourcePlanRecord,
    activations: Sequence[
        tuple[TranslationTargetBlockRevision, TranslationAlignmentRevision]
    ],
    *,
    verified_plan: TranslationSourcePlan | None = None,
    verification_cache: TranslationRehydrationCache | None = None,
) -> tuple[TranslationTargetBlockActive, ...]:
    """Seal several version-one target/alignment pairs in one savepoint.

    This is intentionally narrower than the ordinary CAS path: it accepts only
    new pointers for one already verified source plan.  It keeps every immutable
    revision and paired receipt independent while avoiding a dependency lock,
    savepoint, flush, refresh, and receipt re-read per carried target.
    """

    material = tuple(activations)
    if not material:
        return ()
    from .alignment_persistence import rehydrate_alignment_revision

    plan = verified_plan or rehydrate_source_plan(source_plan)
    block_ids = [revision.block_id for revision, _alignment in material]
    note_contexts: dict[str, tuple[Any, TargetBlockCandidate]] = {}
    if len(set(block_ids)) != len(block_ids):
        raise BlockContractError("target-active-conflict")
    for revision, alignment in material:
        _plan, block = _plan_and_block(
            source_plan,
            revision.block_id,
            verified_plan=plan,
        )
        if (
            revision.source_plan_id != source_plan.id
            or revision.block_id != block.id
            or alignment.source_plan_id != source_plan.id
            or alignment.block_id != block.id
            or alignment.revision_id != revision.id
        ):
            raise BlockContractError("target-active-pair")
        candidate = rehydrate_target_revision(
            revision,
            source_plan,
            verified_plan=plan,
            verification_cache=verification_cache,
        )
        note_contexts[revision.id] = (block, candidate)
        rehydrate_alignment_revision(
            alignment,
            source_plan,
            verified_plan=plan,
            verification_cache=verification_cache,
        )

    existing_targets = TranslationTargetBlockActive.query.filter(
        TranslationTargetBlockActive.source_plan_id == source_plan.id,
        TranslationTargetBlockActive.block_id.in_(block_ids),
    ).first()
    existing_alignments = TranslationTargetAlignmentActive.query.filter(
        TranslationTargetAlignmentActive.source_plan_id == source_plan.id,
        TranslationTargetAlignmentActive.block_id.in_(block_ids),
    ).first()
    if existing_targets is not None or existing_alignments is not None:
        raise BlockContractError("target-active-conflict")

    lock_target_dependency_scope(source_plan.id)
    now = utc_now_db()
    pairs: list[
        tuple[
            TranslationTargetBlockActive,
            TranslationTargetAlignmentActive,
            TranslationTargetBlockRevision,
            TranslationAlignmentRevision,
        ]
    ] = []
    for revision, alignment in material:
        pointer = cast(Any, TranslationTargetBlockActive)(
            source_plan_id=source_plan.id,
            block_id=revision.block_id,
            revision_id=revision.id,
            version=1,
            pair_receipt="0" * 64,
            updated_at=now,
        )
        alignment_pointer = cast(Any, TranslationTargetAlignmentActive)(
            source_plan_id=source_plan.id,
            block_id=revision.block_id,
            revision_id=revision.id,
            alignment_revision_id=alignment.id,
            version=1,
            updated_at=now,
        )
        pointer.pair_receipt = _pair_receipt(
            pointer,
            alignment_pointer,
            revision=revision,
            alignment=alignment,
        )
        pairs.append((pointer, alignment_pointer, revision, alignment))

    try:
        with db.session.begin_nested():
            db.session.add_all([item for pair in pairs for item in (pair[0], pair[1])])
            db.session.flush()
            for pointer, alignment_pointer, revision, alignment in pairs:
                verify_active_pair(
                    pointer,
                    alignment_pointer,
                    revision=revision,
                    alignment=alignment,
                )
                block, candidate = note_contexts[revision.id]
                _maintain_reader_notes_after_activation(
                    source_plan, plan, block, revision, candidate, pointer,
                )
    except IntegrityError as exc:
        raise BlockContractError("target-active-conflict") from exc
    return tuple(pair[0] for pair in pairs)


def lock_target_dependency_scope(source_plan_id: str) -> None:
    """Serialize target-pointer changes and window staging for one source plan.

    A no-op row update is intentional.  It acquires the source-plan write lock
    on SQLite and a row lock on databases with row-level locking.  Every active
    pointer mutation and every final window staging transaction uses this same
    lock, including the pointer-absent case that cannot itself be row-locked.
    """

    if not isinstance(source_plan_id, str) or not source_plan_id:
        raise BlockContractError("target-dependency-scope")
    _ensure_database_transaction()
    changed = db.session.execute(
        update(TranslationSourcePlanRecord)
        .where(TranslationSourcePlanRecord.id == source_plan_id)
        .values(plan_hash=TranslationSourcePlanRecord.plan_hash)
    )
    if getattr(changed, "rowcount", None) != 1:
        raise BlockContractError("target-dependency-scope")
    db.session.flush()


def _active_target_candidates_with_plan(
    source_plan: TranslationSourcePlanRecord,
) -> tuple[TranslationSourcePlan, dict[str, TargetBlockCandidate]]:
    """Load a fresh verified plan and its active target projection."""

    active_session = db.session
    with active_session.no_autoflush:
        canonical_record = active_session.get(
            TranslationSourcePlanRecord,
            source_plan.id,
            populate_existing=True,
            options=(selectinload(TranslationSourcePlanRecord.anchors),),
        )
        if canonical_record is None:
            raise BlockContractError("target-active-source-plan")
        plan = rehydrate_source_plan(canonical_record)
        # Do not trust a potentially stale relationship collection after
        # another session/worker won an activation CAS.  The active projection
        # is the database state, not an ORM identity-map convenience cache.
        pointers = list(
            active_session.query(TranslationTargetBlockActive)
            .populate_existing()
            .filter(TranslationTargetBlockActive.source_plan_id == canonical_record.id)
            .order_by(TranslationTargetBlockActive.block_id)
            .all()
        )
        pointer_by_block = _active_pointer_map(plan, canonical_record.id, pointers)
        alignment_pointers = {
            pointer.block_id: pointer
            for pointer in active_session.query(TranslationTargetAlignmentActive)
            .populate_existing()
            .filter(
                TranslationTargetAlignmentActive.source_plan_id == canonical_record.id
            )
            .all()
        }
        if set(alignment_pointers) != set(pointer_by_block):
            raise BlockContractError("target-active-alignment-cardinality")

        # Keep preview/export verification proportional to the document rather
        # than repeatedly rehydrating the complete immutable source plan for
        # every active target and every nested alignment witness.  Parent
        # target revisions remain available in the identity map for lineage
        # verification, while only active alignment trees are loaded.
        revisions = list(
            active_session.query(TranslationTargetBlockRevision)
            .populate_existing()
            .filter(
                TranslationTargetBlockRevision.source_plan_id == canonical_record.id
            )
            .order_by(TranslationTargetBlockRevision.created_at)
            .all()
        )
        revisions_by_id = {revision.id: revision for revision in revisions}
        alignment_ids = {
            pointer.alignment_revision_id for pointer in alignment_pointers.values()
        }
        if alignment_ids:
            alignments = list(
                active_session.query(TranslationAlignmentRevision)
                .populate_existing()
                .filter(
                    TranslationAlignmentRevision.source_plan_id == canonical_record.id,
                    TranslationAlignmentRevision.id.in_(alignment_ids),
                )
                .options(
                    selectinload(TranslationAlignmentRevision.groups),
                    joinedload(TranslationAlignmentRevision.span_set).selectinload(
                        TranslationTargetSpanSet.spans
                    ),
                )
                .all()
            )
        else:
            alignments = []
        alignments_by_id = {alignment.id: alignment for alignment in alignments}
        if set(alignments_by_id) != alignment_ids:
            raise BlockContractError("target-active-alignment-pointer")

        verification_cache = TranslationRehydrationCache()
        result: dict[str, TargetBlockCandidate] = {}
        for block in plan.blocks:
            block_id = block.id
            pointer = pointer_by_block.get(block_id)
            if (
                pointer is None
                or pointer.source_plan_id != canonical_record.id
                or pointer.version < 1
            ):
                raise BlockContractError("target-active-pointer")
            revision = revisions_by_id.get(pointer.revision_id)
            if (
                revision is None
                or revision.id != pointer.revision_id
                or revision.source_plan_id != canonical_record.id
                or revision.block_id != block_id
            ):
                raise BlockContractError("target-active-pointer")
            alignment_pointer = alignment_pointers.get(block_id)
            if (
                alignment_pointer is None
                or alignment_pointer.version != pointer.version
                or alignment_pointer.revision_id != pointer.revision_id
            ):
                raise BlockContractError("target-active-alignment-pointer")
            alignment = alignments_by_id.get(alignment_pointer.alignment_revision_id)
            if alignment is None or alignment.revision_id != revision.id:
                raise BlockContractError("target-active-alignment-pointer")
            verify_active_pair(
                pointer,
                alignment_pointer,
                session=active_session,
                revision=revision,
                alignment=alignment,
            )
            from .alignment_persistence import rehydrate_alignment_revision

            candidate = rehydrate_target_revision(
                revision,
                canonical_record,
                session=active_session,
                verified_plan=plan,
                verification_cache=verification_cache,
            )
            rehydrate_alignment_revision(
                alignment,
                canonical_record,
                session=active_session,
                verified_plan=plan,
                verification_cache=verification_cache,
            )
            result[block_id] = candidate
        return plan, result


def active_target_candidates(
    source_plan: TranslationSourcePlanRecord,
) -> dict[str, TargetBlockCandidate]:
    """Load one verified active result per canonical source block, or fail."""

    _plan, candidates = _active_target_candidates_with_plan(source_plan)
    return candidates


def _active_pointer_map(
    plan: TranslationSourcePlan,
    source_plan_id: str,
    pointers: list[TranslationTargetBlockActive],
) -> dict[str, TranslationTargetBlockActive]:
    """Require a one-to-one, plan-owned active pointer projection."""

    expected = [block.id for block in plan.blocks]
    if (
        len(pointers) != len(expected)
        or {pointer.block_id for pointer in pointers} != set(expected)
        or any(pointer.source_plan_id != source_plan_id for pointer in pointers)
    ):
        raise BlockContractError("target-active-cardinality")
    result = {pointer.block_id: pointer for pointer in pointers}
    if len(result) != len(pointers):
        raise BlockContractError("target-active-cardinality")
    return result


def _reader_note_rows_for_render(
    source_plan: TranslationSourcePlanRecord,
    plan: TranslationSourcePlan,
) -> tuple[list[dict[str, Any]], set[str]]:
    """Load current review heads and prove their project/plan ownership."""

    # Keep this import local: scholarly_review imports target persistence for
    # target verification, while rendering is the one-way integration seam.
    from . import scholarly_review

    project = source_plan.project
    if project is None:
        raise BlockContractError("target-render-project")
    try:
        with db.session.no_autoflush:
            rows = scholarly_review.list_reader_notes(
                project,
                include_proposed=True,
                include_retired=True,
                actor=None,
                _source_plan=source_plan,
                _blocks=plan.blocks,
            )
    except Exception as exc:
        raise BlockContractError("target-document-note-projection") from exc
    if not isinstance(rows, (list, tuple)):
        raise BlockContractError("target-document-note-projection")

    block_ids = {block.id for block in plan.blocks}
    seen_ids: set[str] = set()
    validated: list[dict[str, Any]] = []
    sentinel_citations: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            raise BlockContractError("target-document-note-shape")
        note_id = row.get("id", row.get("note_id"))
        if not isinstance(note_id, str) or not note_id or note_id in seen_ids:
            raise BlockContractError("target-document-note-id")
        block_id = row.get("block_id")
        if not isinstance(block_id, str) or block_id not in block_ids:
            raise BlockContractError("target-document-note-owner")
        state = row.get("state", "active")
        if state not in {"proposed", "active", "needs_review", "retired"}:
            raise BlockContractError("target-document-note-state")
        citations = row.get("citations", row.get("citation_ids", ()))
        if citations is None:
            citations = ()
        if isinstance(citations, list):
            citations = tuple(citations)
        if not isinstance(citations, tuple) or any(
            not isinstance(citation, str) for citation in citations
        ):
            raise BlockContractError("target-document-note-citations")
        sentinel_citations.update(
            citation
            for citation in citations
            if citation.startswith("legacy-target-note-v1:")
        )
        seen_ids.add(note_id)
        public_citations = tuple(
            citation
            for citation in citations
            if not citation.startswith(
                ("dossier-note-v1:", "legacy-target-note-v1:")
            )
        )
        validated.append(
            {
                **row,
                "id": note_id,
                "block_id": block_id,
                "citations": public_citations,
            }
        )

    # list_reader_notes intentionally exposes a review-safe projection and
    # therefore does not include source_plan_id.  Resolve each returned durable
    # identity against the ORM row so a successor plan cannot leak a same-id
    # block note into this export.
    if seen_ids:
        with db.session.no_autoflush:
            durable_rows = (
                TranslationReaderNote.query.filter(
                    TranslationReaderNote.project_id == project.id,
                    TranslationReaderNote.source_plan_id == source_plan.id,
                    TranslationReaderNote.id.in_(seen_ids),
                ).all()
            )
        durable_by_id = {str(row.id): row for row in durable_rows}
        if set(durable_by_id) != seen_ids or any(
            row.block_id != projection["block_id"]
            or row.source_plan_id != source_plan.id
            or row.project_id != project.id
            for projection in validated
            for row in [durable_by_id.get(projection["id"])]
            if row is not None
        ):
            raise BlockContractError("target-document-note-owner")

    return validated, sentinel_citations


def render_persisted_target_document(source_plan: TranslationSourcePlanRecord) -> str:
    plan, candidates = _active_target_candidates_with_plan(source_plan)
    project = source_plan.project
    if project is None:
        raise BlockContractError("target-render-project")
    with db.session.no_autoflush:
        pointers = TranslationTargetBlockActive.query.filter_by(
            source_plan_id=source_plan.id
        ).all()
    revision_ids = [pointer.revision_id for pointer in pointers]
    with db.session.no_autoflush:
        revisions = (
            TranslationTargetBlockRevision.query.filter(
                TranslationTargetBlockRevision.id.in_(revision_ids)
            ).all()
            if revision_ids
            else []
        )
    revisions_by_id = {revision.id: revision for revision in revisions}
    with db.session.no_autoflush:
        note_rows = (
            TranslationTargetBlockNote.query.filter_by(source_plan_id=source_plan.id)
            .order_by(
                TranslationTargetBlockNote.block_id,
                TranslationTargetBlockNote.order_index,
                TranslationTargetBlockNote.id,
            )
            .all()
        )
    note_rows_by_revision: dict[str, list[TranslationTargetBlockNote]] = {}
    for row in note_rows:
        note_rows_by_revision.setdefault(row.revision_id, []).append(row)

    reader_rows, sentinel_citations = _reader_note_rows_for_render(source_plan, plan)
    reader_by_block: dict[str, list[dict[str, Any]]] = {}
    for row in reader_rows:
        if row.get("state") == "active":
            reader_by_block.setdefault(row["block_id"], []).append(row)

    verification_cache = TranslationRehydrationCache()
    notes_by_block: dict[
        str, tuple[BlockNoteCandidate | ReaderNoteCandidate | Mapping[str, object], ...]
    ] = {}
    for pointer in pointers:
        revision = revisions_by_id.get(pointer.revision_id)
        if revision is None:
            raise BlockContractError("target-active-revision-owner")
        with db.session.no_autoflush:
            notes = rehydrate_target_block_notes(
                revision,
                source_plan,
                rows=note_rows_by_revision.get(revision.id, ()),
                verified_plan=plan,
                verification_cache=verification_cache,
            )
        active_rows = note_rows_by_revision.get(revision.id, ())
        legacy_notes = tuple(
            note
            for row, note in zip(active_rows, notes, strict=True)
            if f"legacy-target-note-v1:{row.id}" not in sentinel_citations
        )
        readers = tuple(reader_by_block.get(pointer.block_id, ()))
        for reader in readers:
            if (
                reader.get("anchor_kind") == "target"
                and reader.get("target_revision_id") is not None
                and str(reader["target_revision_id"]) != str(revision.id)
            ):
                raise BlockContractError("target-document-note-target")
        if legacy_notes or readers:
            notes_by_block[pointer.block_id] = (*legacy_notes, *readers)
    rendered = render_target_document(
        plan,
        source_plan.id,
        candidates,
        target_language=project.target_lang,
        notes=notes_by_block,
    )
    # The original target is fully verified before the non-destructive
    # numbering projection is applied.  A project without an overlay returns
    # the renderer's exact bytes.
    from carlina.core.semantic_numbering import apply_numbering_overlay

    from .numbering_sync import numbering_state

    try:
        state = numbering_state(source_plan)
        if not state["entries"]:
            return rendered
        return apply_numbering_overlay(
            plan.snapshot.output_html, rendered, state["entries"]
        )
    except ValueError as exc:
        raise BlockContractError("target-numbering-overlay-invalid") from exc


__all__ = [
    "activate_initial_target_revisions",
    "activate_target_revision",
    "active_target_candidates",
    "persist_target_revision",
    "persist_target_revision_share_attribution",
    "rehydrate_target_block_notes",
    "rehydrate_target_revision",
    "render_persisted_target_document",
    "verify_active_pair",
    "verify_target_revision_share_attribution",
]
