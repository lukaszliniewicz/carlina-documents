"""Durable Phase-4 target spans and source/target alignment witnesses."""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from typing import Any, cast

from sqlalchemy import inspect
from sqlalchemy.orm.attributes import NO_VALUE

from carlina.core.utc import utc_iso, utc_now_db
from iarf_assistant.extensions import db

from .alignment_contract import (
    ALIGNMENT_CONFIDENCE_PROFILE,
    ALIGNMENT_CONFIDENCE_THRESHOLD,
    TARGET_ALIGNMENT_CONTRACT_VERSION,
    AlignmentGroup,
    TargetSpan,
    alignment_can_generate_sentence_memory,
    groups_witness,
    target_alignment_projection,
    target_alignment_scope,
    validate_span_partition,
)
from .analysis_contract import private_witness, private_witness_candidates
from .block_contract import (
    BlockContractError,
    TranslationSourcePlan,
    canonical_json,
    sha256_text,
)
from .block_persistence import rehydrate_source_plan
from .integrity_receipts import make_receipt, verify_receipt
from .models import (
    TranslationAlignmentGroup,
    TranslationAlignmentRevision,
    TranslationSourceAnchor,
    TranslationTargetBlockRevision,
    TranslationTargetSpan,
    TranslationTargetSpanSet,
)
from .models import (
    TranslationSourcePlan as TranslationSourcePlanRecord,
)
from .rehydration_cache import TranslationRehydrationCache
from .sentence_alignment import SentenceAlignmentManifest
from .target_persistence import _ensure_database_transaction, rehydrate_target_revision
from .target_segmentation import split_target_projection, target_split_profile

_SPAN_TEXT_WITNESS_PURPOSE = "translator-target-span-text-v1"
_SPAN_HASH_WITNESS_PURPOSE = "translator-target-span-v1"
_SPAN_PROJECTION_WITNESS_PURPOSE = "translator-target-span-projection-v1"
_SOURCE_WITNESS_PURPOSE = "translator-alignment-source-witness-v1"
_TARGET_WITNESS_PURPOSE = "translator-alignment-target-witness-v1"
_GROUP_EVIDENCE_WITNESS_PURPOSE = "translator-alignment-group-evidence-v1"
_GROUP_HASH_WITNESS_PURPOSE = "translator-alignment-group-v1"
_GROUPS_WITNESS_PURPOSE = "translator-alignment-groups-v1"
_PROVIDER_MANIFEST_PROFILE_ID = "provider-manifest-v1"


def _private_hash(value: object, *, purpose: str) -> str:
    return private_witness(value, purpose=purpose)


def _private_hash_valid(stored: object, value: object, *, purpose: str) -> bool:
    return isinstance(stored, str) and stored in private_witness_candidates(
        value, purpose=purpose
    )


def _identity_hash(value: object, code: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise BlockContractError(code)
    return value


def _record(source_plan: TranslationSourcePlanRecord) -> TranslationSourcePlanRecord:
    state = (
        inspect(source_plan)
        if isinstance(source_plan, TranslationSourcePlanRecord)
        else None
    )
    if state is None or not state.persistent:
        raise BlockContractError("alignment-source-plan")
    return source_plan


def _block_for_revision(
    record: TranslationSourcePlanRecord,
    revision: TranslationTargetBlockRevision,
    *,
    verified_plan: TranslationSourcePlan | None = None,
) -> Any:
    if (
        not isinstance(revision, TranslationTargetBlockRevision)
        or revision.source_plan_id != record.id
    ):
        raise BlockContractError("alignment-revision-owner")
    plan = verified_plan or rehydrate_source_plan(record)
    if (
        plan.project_id != record.project_id
        or plan.plan_hash != record.plan_hash
        or plan.snapshot.document_id != record.source_document_id
    ):
        raise BlockContractError("alignment-source-plan-pin")
    block = next((item for item in plan.blocks if item.id == revision.block_id), None)
    if block is None:
        raise BlockContractError("alignment-revision-block")
    return block


def _span_hash(span_id: str, order: int, start: int, end: int, text_hash: str) -> str:
    return _private_hash(
        {
            "id": span_id,
            "order": order,
            "start": start,
            "end": end,
            "text_hash": text_hash,
        },
        purpose=_SPAN_HASH_WITNESS_PURPOSE,
    )


def _owner_scope(record: TranslationSourcePlanRecord) -> dict[str, object]:
    project = cast(Any, record.project)
    if project is None or not isinstance(project.user_id, int):
        raise BlockContractError("alignment-project-owner")
    return {"project_id": record.project_id, "owner_user_id": project.user_id}


def _owner_scope_for_plan(
    source_plan_id: str, *, session: Any | None = None
) -> dict[str, object]:
    active_session = session or db.session
    record = active_session.get(TranslationSourcePlanRecord, source_plan_id)
    if record is None:
        raise BlockContractError("alignment-receipt-plan")
    return _owner_scope(_record(record))


def _span_set_payload(
    row: TranslationTargetSpanSet,
    *,
    session: Any | None = None,
    spans: Iterable[TranslationTargetSpan] | None = None,
) -> dict[str, object]:
    child_rows = list(spans) if spans is not None else _spans(row, session=session)
    return {
        **_owner_scope_for_plan(row.source_plan_id, session=session),
        "id": row.id,
        "source_plan_id": row.source_plan_id,
        "block_id": row.block_id,
        "revision_id": row.revision_id,
        "target_revision_hash": row.target_revision_hash,
        "projection_scope": row.projection_scope,
        "profile_id": row.profile_id,
        "profile_version": row.profile_version,
        "projection_hash": row.projection_hash,
        "span_count": row.span_count,
        "created_at": utc_iso(row.created_at, timespec="microseconds"),
        "projection": row.projection,
        "spans": [_span_payload(item) for item in child_rows],
    }


def _span_payload(row: TranslationTargetSpan) -> dict[str, object]:
    return {
        "id": row.id,
        "span_set_id": row.span_set_id,
        "source_plan_id": row.source_plan_id,
        "block_id": row.block_id,
        "revision_id": row.revision_id,
        "order_index": row.order_index,
        "start_offset": row.start_offset,
        "end_offset": row.end_offset,
        "text_hash": row.text_hash,
        "span_hash": row.span_hash,
    }


def _span_set_hash(
    row: TranslationTargetSpanSet,
    *,
    session: Any | None = None,
    spans: Iterable[TranslationTargetSpan] | None = None,
) -> str:
    return make_receipt(
        purpose="translator-target-span-set-receipt-v1",
        values={
            "receipt_version": 1,
            **_span_set_payload(row, session=session, spans=spans),
        },
    )


def _span_set_valid(
    row: TranslationTargetSpanSet,
    *,
    session: Any | None = None,
    spans: Iterable[TranslationTargetSpan] | None = None,
) -> bool:
    try:
        verify_receipt(
            stored=row.record_hash,
            purpose="translator-target-span-set-receipt-v1",
            values={
                "receipt_version": 1,
                **_span_set_payload(row, session=session, spans=spans),
            },
            code="alignment-span-set-hash",
        )
    except BlockContractError:
        return False
    return True


def _alignment_payload(
    row: TranslationAlignmentRevision,
    *,
    session: Any | None = None,
    groups: Iterable[TranslationAlignmentGroup] | None = None,
) -> dict[str, object]:
    child_rows = list(groups) if groups is not None else _groups(row, session=session)
    return {
        **_owner_scope_for_plan(row.source_plan_id, session=session),
        "id": row.id,
        "source_plan_id": row.source_plan_id,
        "block_id": row.block_id,
        "revision_id": row.revision_id,
        "span_set_id": row.span_set_id,
        "target_revision_hash": row.target_revision_hash,
        "span_set_hash": row.span_set_hash,
        "method": row.method,
        "confidence_profile": row.confidence_profile,
        "review_state": row.review_state,
        "group_count": row.group_count,
        "groups_hash": row.groups_hash,
        "created_at": utc_iso(row.created_at, timespec="microseconds"),
        "payload": row.payload,
        "groups": [_group_payload(item) for item in child_rows],
    }


def _group_payload(row: TranslationAlignmentGroup) -> dict[str, object]:
    return {
        "id": row.id,
        "alignment_revision_id": row.alignment_revision_id,
        "source_plan_id": row.source_plan_id,
        "block_id": row.block_id,
        "revision_id": row.revision_id,
        "span_set_id": row.span_set_id,
        "order_index": row.order_index,
        "source_anchor_ids": row.source_anchor_ids,
        "target_span_ids": row.target_span_ids,
        "source_witness_hash": row.source_witness_hash,
        "target_witness_hash": row.target_witness_hash,
        "method": row.method,
        "confidence": row.confidence,
        "review_state": row.review_state,
        "evidence": row.evidence,
        "evidence_hash": row.evidence_hash,
        "group_hash": row.group_hash,
    }


def _alignment_hash(
    row: TranslationAlignmentRevision,
    *,
    session: Any | None = None,
    groups: Iterable[TranslationAlignmentGroup] | None = None,
) -> str:
    return make_receipt(
        purpose="translator-alignment-revision-receipt-v1",
        values={
            "receipt_version": 1,
            **_alignment_payload(row, session=session, groups=groups),
        },
    )


def _alignment_valid(
    row: TranslationAlignmentRevision,
    *,
    session: Any | None = None,
    groups: Iterable[TranslationAlignmentGroup] | None = None,
) -> bool:
    try:
        verify_receipt(
            stored=row.record_hash,
            purpose="translator-alignment-revision-receipt-v1",
            values={
                "receipt_version": 1,
                **_alignment_payload(row, session=session, groups=groups),
            },
            code="alignment-revision-hash",
        )
    except BlockContractError:
        return False
    return True


def _anchors(
    record: TranslationSourcePlanRecord, block_id: str, *, session: Any | None = None
) -> list[TranslationSourceAnchor]:
    loaded = inspect(record).attrs.anchors.loaded_value
    if loaded is not NO_VALUE:
        return sorted(
            (item for item in loaded if item.block_id == block_id),
            key=lambda item: item.block_order_index,
        )
    active_session = session or db.session
    return list(
        active_session.query(TranslationSourceAnchor)
        .populate_existing()
        .filter_by(source_plan_id=record.id, block_id=block_id)
        .order_by(TranslationSourceAnchor.block_order_index)
        .all()
    )


def _spans(
    span_set: TranslationTargetSpanSet, *, session: Any | None = None
) -> list[TranslationTargetSpan]:
    loaded = inspect(span_set).attrs.spans.loaded_value
    if loaded is not NO_VALUE:
        return sorted(loaded, key=lambda item: item.order_index)
    active_session = session or db.session
    return list(
        active_session.query(TranslationTargetSpan)
        .populate_existing()
        .filter_by(span_set_id=span_set.id)
        .order_by(TranslationTargetSpan.order_index)
        .all()
    )


def _groups(
    alignment: TranslationAlignmentRevision, *, session: Any | None = None
) -> list[TranslationAlignmentGroup]:
    loaded = inspect(alignment).attrs.groups.loaded_value
    if loaded is not NO_VALUE:
        return sorted(loaded, key=lambda item: item.order_index)
    active_session = session or db.session
    return list(
        active_session.query(TranslationAlignmentGroup)
        .populate_existing()
        .filter_by(alignment_revision_id=alignment.id)
        .order_by(TranslationAlignmentGroup.order_index)
        .all()
    )


def _source_witness(items: Iterable[TranslationSourceAnchor]) -> str:
    return _private_hash(
        _source_witness_material(items), purpose=_SOURCE_WITNESS_PURPOSE
    )


def _target_witness(items: Iterable[TranslationTargetSpan]) -> str:
    return _private_hash(
        _target_witness_material(items), purpose=_TARGET_WITNESS_PURPOSE
    )


def _source_witness_material(
    items: Iterable[TranslationSourceAnchor],
) -> list[dict[str, object]]:
    return [
        {
            "id": item.id,
            "source_plan_id": item.source_plan_id,
            "block_id": item.block_id,
            "order_index": item.order_index,
            "order": item.block_order_index,
            "start_offset": item.start_offset,
            "end_offset": item.end_offset,
            "splitter_profile": item.splitter_profile,
            "splitter_version": item.splitter_version,
            "source_text": item.source_text,
            "source_hash": item.source_hash,
            "created_at": utc_iso(item.created_at, timespec="microseconds"),
        }
        for item in items
    ]


def _target_witness_material(
    items: Iterable[TranslationTargetSpan],
) -> list[dict[str, object]]:
    return [
        {
            "id": item.id,
            "span_set_id": item.span_set_id,
            "source_plan_id": item.source_plan_id,
            "block_id": item.block_id,
            "revision_id": item.revision_id,
            "order": item.order_index,
            "start": item.start_offset,
            "end": item.end_offset,
            "text_hash": item.text_hash,
            "span_hash": item.span_hash,
        }
        for item in items
    ]


def _validate_groups(
    groups: tuple[AlignmentGroup, ...],
    anchors: list[TranslationSourceAnchor],
    spans: list[TranslationTargetSpan],
) -> None:
    if not groups:
        raise BlockContractError("alignment-groups-empty")
    anchors_by_id = {item.id: item for item in anchors}
    spans_by_id = {item.id: item for item in spans}
    source_seen: list[str] = []
    target_seen: list[str] = []
    source_cursor = 0
    target_cursor = 0
    source_order = [item.id for item in anchors]
    target_order = [item.id for item in spans]
    for group in groups:
        source_ids = group.source_anchor_ids
        target_ids = group.target_span_ids
        if any(item not in anchors_by_id for item in source_ids) or any(
            item not in spans_by_id for item in target_ids
        ):
            raise BlockContractError("alignment-group-foreign-range")
        if source_order[source_cursor : source_cursor + len(source_ids)] != list(
            source_ids
        ):
            raise BlockContractError("alignment-group-source-order")
        if target_order[target_cursor : target_cursor + len(target_ids)] != list(
            target_ids
        ):
            raise BlockContractError("alignment-group-target-order")
        source_seen.extend(source_ids)
        target_seen.extend(target_ids)
        source_cursor += len(source_ids)
        target_cursor += len(target_ids)
    if source_seen != source_order or target_seen != target_order:
        raise BlockContractError("alignment-group-coverage")


def deterministic_alignment_groups(
    anchors: list[TranslationSourceAnchor], spans: list[TranslationTargetSpan]
) -> tuple[AlignmentGroup, ...]:
    """A stable no-embedding baseline; it partitions monotonically by count."""

    if not anchors and not spans:
        raise BlockContractError("alignment-empty-partition")
    groups: list[AlignmentGroup] = []
    if not anchors or not spans:
        raise BlockContractError("alignment-empty-partition")
    if len(anchors) <= len(spans):
        for index, anchor in enumerate(anchors):
            start = index * len(spans) // len(anchors)
            end = (index + 1) * len(spans) // len(anchors)
            groups.append(
                AlignmentGroup(
                    (anchor.id,),
                    tuple(item.id for item in spans[start:end]),
                    "deterministic-count-v1",
                    0.45 if end - start == 1 else 0.3,
                    "needs_review",
                    {"source_count": 1, "target_count": end - start},
                )
            )
    else:
        for index, span in enumerate(spans):
            start = index * len(anchors) // len(spans)
            end = (index + 1) * len(anchors) // len(spans)
            groups.append(
                AlignmentGroup(
                    tuple(item.id for item in anchors[start:end]),
                    (span.id,),
                    "deterministic-count-v1",
                    0.45 if end - start == 1 else 0.3,
                    "needs_review",
                    {"source_count": end - start, "target_count": 1},
                )
            )
    return tuple(groups)


def _provider_manifest_profile_version(
    boundaries: Iterable[tuple[int, int, int]],
) -> str:
    """Hash only canonical ordered boundaries for provider span identity."""

    normalized = tuple(boundaries)
    if not normalized:
        raise BlockContractError("alignment-provider-manifest-spans")
    return sha256_text(canonical_json([list(item) for item in normalized]))


def persist_target_span_set(
    source_plan: TranslationSourcePlanRecord,
    revision: TranslationTargetBlockRevision,
    *,
    verified_plan: TranslationSourcePlan | None = None,
    verification_cache: TranslationRehydrationCache | None = None,
    manifest: SentenceAlignmentManifest | None = None,
) -> TranslationTargetSpanSet:
    """Persist one immutable exact span partition, reusing equal material."""

    record = _record(source_plan)
    block = _block_for_revision(record, revision, verified_plan=verified_plan)
    candidate = rehydrate_target_revision(
        revision,
        record,
        verified_plan=verified_plan,
        verification_cache=verification_cache,
    )
    projection = target_alignment_projection(block, candidate)
    projection_scope = target_alignment_scope(block)
    if manifest is not None and (
        not isinstance(manifest, SentenceAlignmentManifest)
        or manifest.block_id != revision.block_id
    ):
        raise BlockContractError("alignment-provider-manifest-owner")
    if manifest is not None and (
        len(manifest.target_sentences) != len(manifest.target_spans)
        or any(
            span.order != index
            or span.text != projection[span.start : span.end]
            or sentence != span.text.strip()
            for index, (sentence, span) in enumerate(
                zip(manifest.target_sentences, manifest.target_spans, strict=True),
                start=1,
            )
        )
    ):
        raise BlockContractError("alignment-provider-manifest-spans")
    if manifest is None:
        profile_id, profile_version = target_split_profile(revision.target_language)
    else:
        profile_id = _PROVIDER_MANIFEST_PROFILE_ID
        profile_version = _provider_manifest_profile_version(
            (span.order, span.start, span.end) for span in manifest.target_spans
        )
    projection_hash = _private_hash(
        projection, purpose=_SPAN_PROJECTION_WITNESS_PURPOSE
    )
    existing_candidates = (
        db.session.query(TranslationTargetSpanSet)
        .filter_by(
            source_plan_id=record.id,
            block_id=revision.block_id,
            revision_id=revision.id,
            projection_scope=projection_scope,
            profile_id=profile_id,
            profile_version=profile_version,
        )
        .all()
    )
    for existing in existing_candidates:
        if _private_hash_valid(
            existing.projection_hash,
            projection,
            purpose=_SPAN_PROJECTION_WITNESS_PURPOSE,
        ):
            rehydrate_target_span_set(
                existing,
                record,
                verified_plan=verified_plan,
                verification_cache=verification_cache,
            )
            return existing
    if manifest is None:
        spans = split_target_projection(projection, revision.target_language)
    else:
        spans = tuple(
            TargetSpan(
                id=f"span-{index}",
                order=index,
                start=span.start,
                end=span.end,
                text_hash=sha256_text(projection[span.start : span.end]),
            )
            for index, span in enumerate(manifest.target_spans, start=1)
        )
    validate_span_partition(projection, spans)
    _ensure_database_transaction()
    now = utc_now_db()
    row = cast(Any, TranslationTargetSpanSet)(
        id=str(uuid.uuid4()),
        source_plan_id=record.id,
        block_id=revision.block_id,
        revision_id=revision.id,
        target_revision_hash=revision.revision_record_hash,
        projection_scope=projection_scope,
        profile_id=profile_id,
        profile_version=profile_version,
        projection=projection,
        projection_hash=projection_hash,
        span_count=len(spans),
        record_hash="0" * 64,
        created_at=now,
    )
    child_rows: list[TranslationTargetSpan] = []
    for span in spans:
        span_id = str(uuid.uuid4())
        text = projection[span.start : span.end]
        child_rows.append(
            cast(Any, TranslationTargetSpan)(
                id=span_id,
                span_set_id=row.id,
                source_plan_id=record.id,
                block_id=revision.block_id,
                revision_id=revision.id,
                order_index=span.order,
                start_offset=span.start,
                end_offset=span.end,
                text_hash=_private_hash(text, purpose=_SPAN_TEXT_WITNESS_PURPOSE),
                span_hash=_span_hash(
                    span_id,
                    span.order,
                    span.start,
                    span.end,
                    span.text_hash,
                ),
            )
        )
    row.record_hash = _span_set_hash(row, spans=child_rows)
    db.session.add(row)
    db.session.add_all(child_rows)
    db.session.flush()
    return row


def rehydrate_target_span_set(
    span_set: TranslationTargetSpanSet,
    source_plan: TranslationSourcePlanRecord | None = None,
    *,
    session: Any | None = None,
    verified_plan: TranslationSourcePlan | None = None,
    verification_cache: TranslationRehydrationCache | None = None,
) -> tuple[TargetSpan, ...]:
    try:
        active_session = session or db.session
        record = _record(
            cast(
                TranslationSourcePlanRecord,
                source_plan
                or active_session.get(
                    TranslationSourcePlanRecord, span_set.source_plan_id
                ),
            )
        )
        cache_key = (record.id, span_set.id)
        if verification_cache is not None:
            cached = verification_cache.target_span_sets.get(cache_key)
            if cached is not None:
                return cast(tuple[TargetSpan, ...], cached)
        revision = active_session.get(
            TranslationTargetBlockRevision, span_set.revision_id
        )
        if (
            revision is None
            or revision.source_plan_id != record.id
            or revision.block_id != span_set.block_id
        ):
            raise BlockContractError("alignment-span-set-owner")
        block = _block_for_revision(
            record,
            revision,
            verified_plan=verified_plan,
        )
        candidate = rehydrate_target_revision(
            revision,
            record,
            session=active_session,
            verified_plan=verified_plan,
            verification_cache=verification_cache,
        )
        projection = target_alignment_projection(block, candidate)
        rows = _spans(span_set, session=active_session)
        if (
            span_set.target_revision_hash != revision.revision_record_hash
            or span_set.projection_scope != target_alignment_scope(block)
            or span_set.projection != projection
            or not _private_hash_valid(
                span_set.projection_hash,
                projection,
                purpose=_SPAN_PROJECTION_WITNESS_PURPOSE,
            )
            or not _span_set_valid(
                span_set,
                session=active_session,
                spans=rows,
            )
        ):
            raise BlockContractError("alignment-span-set-hash")
        if span_set.profile_id == _PROVIDER_MANIFEST_PROFILE_ID:
            expected_profile = (
                _PROVIDER_MANIFEST_PROFILE_ID,
                _provider_manifest_profile_version(
                    (item.order_index, item.start_offset, item.end_offset)
                    for item in rows
                ),
            )
        else:
            expected_profile = target_split_profile(revision.target_language)
        if (span_set.profile_id, span_set.profile_version) != expected_profile:
            raise BlockContractError("alignment-span-set-profile")
        if any(
            item.span_set_id != span_set.id
            or item.source_plan_id != record.id
            or item.block_id != span_set.block_id
            or item.revision_id != revision.id
            for item in rows
        ):
            raise BlockContractError("alignment-span-identity")
        spans = tuple(
            TargetSpan(
                id=item.id,
                order=item.order_index,
                start=item.start_offset,
                end=item.end_offset,
                text_hash=sha256_text(projection[item.start_offset : item.end_offset]),
            )
            for item in rows
        )
        validate_span_partition(projection, spans)
        if len(rows) != span_set.span_count:
            raise BlockContractError("alignment-span-set-count")
        for row, span in zip(rows, spans, strict=True):
            segment = projection[span.start : span.end]
            if not _private_hash_valid(
                row.text_hash, segment, purpose=_SPAN_TEXT_WITNESS_PURPOSE
            ):
                raise BlockContractError("alignment-span-witness")
            if not _private_hash_valid(
                row.span_hash,
                {
                    "id": row.id,
                    "order": row.order_index,
                    "start": row.start_offset,
                    "end": row.end_offset,
                    "text_hash": span.text_hash,
                },
                purpose=_SPAN_HASH_WITNESS_PURPOSE,
            ):
                raise BlockContractError("alignment-span-hash")
        if verification_cache is not None:
            verification_cache.target_span_sets[cache_key] = spans
        return spans
    except BlockContractError:
        raise
    except Exception as exc:
        raise BlockContractError("alignment-span-set-corrupt") from exc


def persist_alignment_revision(
    source_plan: TranslationSourcePlanRecord,
    span_set: TranslationTargetSpanSet,
    *,
    groups: Iterable[AlignmentGroup] | None = None,
    method: str = "deterministic-count-v1",
    review_state: str = "needs_review",
    verified_plan: TranslationSourcePlan | None = None,
    verification_cache: TranslationRehydrationCache | None = None,
) -> TranslationAlignmentRevision:
    """Persist a complete, ordered, noncrossing alignment partition."""

    record = _record(source_plan)
    if (
        not isinstance(span_set, TranslationTargetSpanSet)
        or span_set.source_plan_id != record.id
    ):
        raise BlockContractError("alignment-span-set-owner")
    rehydrate_target_span_set(
        span_set,
        record,
        verified_plan=verified_plan,
        verification_cache=verification_cache,
    )
    revision = db.session.get(TranslationTargetBlockRevision, span_set.revision_id)
    if revision is None:
        raise BlockContractError("alignment-target-missing")
    anchor_rows = _anchors(record, span_set.block_id)
    span_rows = _spans(span_set)
    normalized = (
        tuple(groups)
        if groups is not None
        else deterministic_alignment_groups(anchor_rows, span_rows)
    )
    if (
        not isinstance(method, str)
        or not method
        or review_state not in {"approved", "needs_review"}
    ):
        raise BlockContractError("alignment-revision-state")
    _validate_groups(normalized, anchor_rows, span_rows)
    if any(group.confidence < ALIGNMENT_CONFIDENCE_THRESHOLD for group in normalized):
        if (
            any(group.review_state == "approved" for group in normalized)
            or review_state == "approved"
        ):
            raise BlockContractError("alignment-confidence-approval")
        review_state = "needs_review"
    _ensure_database_transaction()
    now = utc_now_db()
    row = cast(Any, TranslationAlignmentRevision)(
        id=str(uuid.uuid4()),
        source_plan_id=record.id,
        block_id=span_set.block_id,
        revision_id=span_set.revision_id,
        span_set_id=span_set.id,
        target_revision_hash=revision.revision_record_hash,
        span_set_hash=span_set.record_hash,
        method=method,
        confidence_profile=ALIGNMENT_CONFIDENCE_PROFILE,
        review_state=review_state,
        group_count=len(normalized),
        payload={
            "contract_version": TARGET_ALIGNMENT_CONTRACT_VERSION,
            "groups": [group.to_dict() for group in normalized],
        },
        groups_hash=groups_witness(normalized),
        record_hash="0" * 64,
        created_at=now,
    )
    anchors_by_id = {item.id: item for item in anchor_rows}
    spans_by_id = {item.id: item for item in span_rows}
    group_rows: list[TranslationAlignmentGroup] = []
    for index, group in enumerate(normalized, start=1):
        source_items = [anchors_by_id[item] for item in group.source_anchor_ids]
        target_items = [spans_by_id[item] for item in group.target_span_ids]
        group_rows.append(
            cast(Any, TranslationAlignmentGroup)(
                id=str(uuid.uuid4()),
                alignment_revision_id=row.id,
                source_plan_id=record.id,
                block_id=row.block_id,
                revision_id=row.revision_id,
                span_set_id=row.span_set_id,
                order_index=index,
                source_anchor_ids=list(group.source_anchor_ids),
                target_span_ids=list(group.target_span_ids),
                source_witness_hash=_source_witness(source_items),
                target_witness_hash=_target_witness(target_items),
                method=group.method,
                confidence=float(group.confidence),
                review_state=group.review_state,
                evidence=dict(group.evidence),
                evidence_hash=_private_hash(
                    group.evidence, purpose=_GROUP_EVIDENCE_WITNESS_PURPOSE
                ),
                group_hash=_private_hash(
                    group.to_dict(), purpose=_GROUP_HASH_WITNESS_PURPOSE
                ),
            )
        )
    row.groups_hash = _private_hash(
        [group.to_dict() for group in normalized], purpose=_GROUPS_WITNESS_PURPOSE
    )
    row.record_hash = _alignment_hash(row, groups=group_rows)
    db.session.add(row)
    db.session.add_all(group_rows)
    db.session.flush()
    return row


def rehydrate_alignment_revision(
    alignment: TranslationAlignmentRevision,
    source_plan: TranslationSourcePlanRecord | None = None,
    *,
    session: Any | None = None,
    verified_plan: TranslationSourcePlan | None = None,
    verification_cache: TranslationRehydrationCache | None = None,
) -> tuple[AlignmentGroup, ...]:
    """Verify all immutable witnesses before an alignment can activate a target."""

    try:
        if not isinstance(alignment, TranslationAlignmentRevision):
            raise BlockContractError("alignment-revision-type")
        active_session = session or db.session
        record = _record(
            cast(
                TranslationSourcePlanRecord,
                source_plan
                or active_session.get(
                    TranslationSourcePlanRecord, alignment.source_plan_id
                ),
            )
        )
        if alignment.source_plan_id != record.id:
            raise BlockContractError("alignment-revision-owner")
        cache_key = (record.id, alignment.id)
        if verification_cache is not None:
            cached = verification_cache.alignments.get(cache_key)
            if cached is not None:
                return cast(tuple[AlignmentGroup, ...], cached)
        revision = active_session.get(
            TranslationTargetBlockRevision, alignment.revision_id
        )
        span_set = active_session.get(TranslationTargetSpanSet, alignment.span_set_id)
        if (
            revision is None
            or span_set is None
            or revision.block_id != alignment.block_id
            or span_set.source_plan_id != record.id
            or span_set.revision_id != revision.id
        ):
            raise BlockContractError("alignment-revision-identity")
        rehydrate_target_span_set(
            span_set,
            record,
            session=active_session,
            verified_plan=verified_plan,
            verification_cache=verification_cache,
        )
        rows = _groups(alignment, session=active_session)
        if (
            alignment.confidence_profile != ALIGNMENT_CONFIDENCE_PROFILE
            or alignment.target_revision_hash != revision.revision_record_hash
            or alignment.span_set_hash != span_set.record_hash
            or not _alignment_valid(
                alignment,
                session=active_session,
                groups=rows,
            )
        ):
            raise BlockContractError("alignment-revision-hash")
        if len(rows) != alignment.group_count or alignment.review_state not in {
            "approved",
            "needs_review",
        }:
            raise BlockContractError("alignment-revision-groups")
        groups: list[AlignmentGroup] = []
        for expected, row in enumerate(rows, start=1):
            if (
                row.order_index != expected
                or row.source_plan_id != record.id
                or row.block_id != alignment.block_id
                or row.revision_id != revision.id
                or row.span_set_id != span_set.id
            ):
                raise BlockContractError("alignment-group-identity")
            group = AlignmentGroup(
                tuple(row.source_anchor_ids),
                tuple(row.target_span_ids),
                row.method,
                row.confidence,
                row.review_state,
                cast(dict[str, object], row.evidence),
            )
            if not _private_hash_valid(
                row.evidence_hash,
                group.evidence,
                purpose=_GROUP_EVIDENCE_WITNESS_PURPOSE,
            ) or not _private_hash_valid(
                row.group_hash,
                group.to_dict(),
                purpose=_GROUP_HASH_WITNESS_PURPOSE,
            ):
                raise BlockContractError("alignment-group-hash")
            groups.append(group)
        anchor_rows = _anchors(record, alignment.block_id, session=active_session)
        span_rows = _spans(span_set, session=active_session)
        _validate_groups(tuple(groups), anchor_rows, span_rows)
        if any(
            group.confidence < ALIGNMENT_CONFIDENCE_THRESHOLD for group in groups
        ) and (
            alignment.review_state != "needs_review"
            or any(group.review_state != "needs_review" for group in groups)
        ):
            raise BlockContractError("alignment-confidence-review")
        anchors_by_id = {item.id: item for item in anchor_rows}
        spans_by_id = {item.id: item for item in span_rows}
        for row, group in zip(rows, groups, strict=True):
            if not _private_hash_valid(
                row.source_witness_hash,
                _source_witness_material(
                    [anchors_by_id[item] for item in group.source_anchor_ids]
                ),
                purpose=_SOURCE_WITNESS_PURPOSE,
            ) or not _private_hash_valid(
                row.target_witness_hash,
                _target_witness_material(
                    [spans_by_id[item] for item in group.target_span_ids]
                ),
                purpose=_TARGET_WITNESS_PURPOSE,
            ):
                raise BlockContractError("alignment-group-witness")
        if not _private_hash_valid(
            alignment.groups_hash,
            [group.to_dict() for group in groups],
            purpose=_GROUPS_WITNESS_PURPOSE,
        ):
            raise BlockContractError("alignment-revision-groups-hash")
        payload = {
            "contract_version": TARGET_ALIGNMENT_CONTRACT_VERSION,
            "groups": [group.to_dict() for group in groups],
        }
        if canonical_json(alignment.payload) != canonical_json(payload):
            raise BlockContractError("alignment-revision-payload")
        result = tuple(groups)
        if verification_cache is not None:
            verification_cache.alignments[cache_key] = result
        return result
    except BlockContractError:
        raise
    except Exception as exc:
        raise BlockContractError("alignment-revision-corrupt") from exc


def alignment_memory_eligible(alignment: TranslationAlignmentRevision) -> bool:
    return alignment_can_generate_sentence_memory(
        alignment.review_state, rehydrate_alignment_revision(alignment)
    )


__all__ = [
    "alignment_memory_eligible",
    "deterministic_alignment_groups",
    "persist_alignment_revision",
    "persist_target_span_set",
    "rehydrate_alignment_revision",
    "rehydrate_target_span_set",
]
