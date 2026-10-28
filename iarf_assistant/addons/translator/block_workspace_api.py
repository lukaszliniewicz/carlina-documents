"""Safe read and human-edit boundaries for the document-block workspace.

The block workspace is intentionally a thin API projection over the strict
Translator persistence contracts.  It never infers an engine from row shape.
Continuous editor markup is reparsed through the compact allowlist and restored
to the source-bound target contract before validation, span splitting,
alignment, and paired CAS activation.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload, selectinload

from carlina.core.utc import utc_iso, utc_now_db

from ...extensions import db
from ...models import RuntimeApprovalRequest
from .alignment_contract import AlignmentGroup
from .alignment_persistence import (
    persist_alignment_revision,
    persist_target_span_set,
    rehydrate_alignment_revision,
    rehydrate_target_span_set,
)
from .analysis_contract import GlossaryPayload, glossary_usage_view, normalized_term, private_witness
from .glossary_contract import GlossaryUsage
from .block_contract import (
    DOCUMENT_BLOCK_ENGINE_CONTRACT,
    LEGACY_ENGINE_CONTRACT,
    BlockContractError,
    canonical_json,
    sha256_text,
)
from .block_persistence import (
    _ensure_database_transaction,
    active_source_plan_pointer,
    effective_engine_contract,
    rehydrate_source_plan,
    require_active_source_plan,
    verify_source_plan_binding,
)
from .compact_markup import (
    COMPACT_EDITOR_CONTRACT_VERSION,
    compact_candidate_projection,
    compact_source_projection,
    normalize_editor_candidate,
)
from .glossary_persistence import (
    activate_glossary_revision,
    approve_glossary_proposal,
    create_glossary_concept,
    find_glossary_revisions_for_term,
    glossary_lock_preview,
    persist_glossary_revision,
    rehydrate_glossary_revision,
    verify_glossary_active,
)
from .models import (
    TranslationAlignmentRevision,
    TranslationGlossaryActive,
    TranslationGlossaryConcept,
    TranslationGlossaryRevision,
    TranslationGlossarySuggestion,
    TranslationResearchDossierGlossaryProposal,
    TranslationShareGrant,
    TranslationSourcePlan,
    TranslationTargetAlignmentActive,
    TranslationTargetBlockActive,
    TranslationTargetBlockNote,
    TranslationTargetBlockRevision,
    TranslationTargetEditIdempotency,
    TranslationTargetSpanSet,
)
from .operations import find_active_operation
from .rehydration_cache import TranslationRehydrationCache
from .sentence_alignment import (
    SentenceAlignmentManifest,
    apply_sentence_edit,
    rebase_sentence_alignment,
)
from .target_contract import TargetBlockCandidate, normalize_target_candidate
from .target_persistence import (
    activate_target_revision,
    persist_target_revision,
    persist_target_revision_share_attribution,
    rehydrate_target_block_notes,
    rehydrate_target_revision,
    verify_active_pair,
)
from .translation_units import (
    TRANSLATION_UNIT_MODE_ALIGNED_SENTENCES,
    normalize_translation_unit_mode,
)

WORKSPACE_CONTRACT = "translator-block-workspace-v5"
ALIGNED_SENTENCES_WORKSPACE_CONTRACT = "translator-block-workspace-v6"
MAX_WORKSPACE_LIMIT = 200
MAX_WORKSPACE_OFFSET = 10_000_000
MAX_CANDIDATE_JSON_BYTES = 2 * 1024 * 1024
MAX_GLOSSARY_JSON_BYTES = 64 * 1024
_PARTIAL_RESULT_CONTRACT = "translator-block-partial-v1"
_PARTIAL_FAILURE_KEYS = frozenset(
    {
        "attempt_id",
        "window_order",
        "affected_block_ids",
        "dispatch_state",
        "failure_kind",
        "recovery_action",
        "remaining_dispatches",
    }
)
_PARTIAL_FAILURE_STATES = frozenset(
    {
        "provider_failed",
        "normalization_failed",
        "fence_failed",
        "response_received",
        "provider_outcome_uncertain",
        "staging_failed",
    }
)
_PARTIAL_RECOVERY_ACTIONS = {
    ("provider_failed", "provider_failed"): "translate_remaining",
    ("normalization_failed", "normalization_failed"): "translate_remaining",
    ("fence_failed", "fence_failed"): "rebase_required",
    ("response_received", "fence_failed"): "rebase_required",
    ("provider_outcome_uncertain", "provider_outcome_uncertain"): "manual_review",
    ("staging_failed", "staging_failed"): "support_required",
}


class BlockWorkspaceIntegrityError(ValueError):
    """A safe, typed integrity failure at the workspace boundary."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class WorkspacePlan:
    record: TranslationSourcePlan
    plan: Any
    active_version: int


def require_document_block_engine(project: Any) -> None:
    """Require the explicit block engine selector for block-only operations."""

    try:
        engine = effective_engine_contract(getattr(project, "engine_contract", None))
    except BlockContractError as exc:
        raise BlockWorkspaceIntegrityError("engine-contract-invalid") from exc
    if engine != DOCUMENT_BLOCK_ENGINE_CONTRACT:
        raise BlockWorkspaceIntegrityError("engine-contract-required")


def require_segment_engine(project: Any) -> None:
    """Fail closed when a subtitle segment surface receives a block project."""

    try:
        engine = effective_engine_contract(getattr(project, "engine_contract", None))
    except BlockContractError as exc:
        raise BlockWorkspaceIntegrityError("engine-contract-invalid") from exc
    settings = getattr(project, "settings", None)
    source_type = settings.get("source_type") if isinstance(settings, Mapping) else None
    if engine != LEGACY_ENGINE_CONTRACT or source_type != "subtitle_studio":
        raise BlockWorkspaceIntegrityError("segment-engine-surface")


def is_document_block_engine(project: Any) -> bool:
    """Return the explicit selector result without inspecting child rows."""

    try:
        return (
            effective_engine_contract(getattr(project, "engine_contract", None))
            == DOCUMENT_BLOCK_ENGINE_CONTRACT
        )
    except BlockContractError:
        return False


def load_block_source_plan(project: Any) -> WorkspacePlan:
    """Load the explicitly active verified plan for a block-engine surface."""

    require_document_block_engine(project)
    pointer = active_source_plan_pointer(project.id)
    if pointer is None:
        raise BlockWorkspaceIntegrityError("source-plan-active-missing")
    record = db.session.get(TranslationSourcePlan, pointer.source_plan_id)
    if record is None:
        raise BlockWorkspaceIntegrityError("source-plan-active-missing")
    try:
        plan = rehydrate_source_plan(record)
    except Exception as exc:
        raise BlockWorkspaceIntegrityError("source-plan-corrupt") from exc
    if (
        record.project_id != project.id
        or plan.project_id != project.id
        or plan.snapshot.document_id != project.document_id
        or plan.snapshot.content_hash != project.source_content_hash
    ):
        raise BlockWorkspaceIntegrityError("source-plan-project-pin")
    return WorkspacePlan(record, plan, pointer.version)


def _load_glossary_source_plan(project: Any) -> tuple[TranslationSourcePlan, int]:
    """Load only the verified active-plan binding needed by glossary rows."""

    require_document_block_engine(project)
    pointer = active_source_plan_pointer(project.id)
    if pointer is None:
        raise BlockWorkspaceIntegrityError("source-plan-active-missing")
    record = db.session.get(TranslationSourcePlan, pointer.source_plan_id)
    if record is None:
        raise BlockWorkspaceIntegrityError("source-plan-active-missing")
    try:
        verify_source_plan_binding(record)
    except Exception as exc:
        raise BlockWorkspaceIntegrityError("source-plan-corrupt") from exc
    if record.project_id != project.id:
        raise BlockWorkspaceIntegrityError("source-plan-project-pin")
    return record, pointer.version


def _safe_timestamp(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return utc_iso(value)
    except TypeError, ValueError, OverflowError:
        raise BlockWorkspaceIntegrityError("workspace-timestamp")


def _safe_partial_failure(operation: Any) -> dict[str, Any] | None:
    """Project only the bounded recovery hint from a partial block result."""

    if (
        getattr(operation, "kind", None) != "document_block_translate"
        or getattr(operation, "state", None) != "partial"
    ):
        return None
    try:
        result = operation.result_json
    except Exception as exc:
        raise BlockWorkspaceIntegrityError("operation-result-corrupt") from exc
    if (
        not isinstance(result, Mapping)
        or result.get("contract_version") != _PARTIAL_RESULT_CONTRACT
    ):
        return None
    failure = result.get("block_failure")
    if not isinstance(failure, Mapping) or set(failure) != _PARTIAL_FAILURE_KEYS:
        raise BlockWorkspaceIntegrityError("operation-failure-projection")
    attempt_id = failure.get("attempt_id")
    if not isinstance(attempt_id, str) or not attempt_id or len(attempt_id) > 128:
        raise BlockWorkspaceIntegrityError("operation-failure-projection")
    window_order = failure.get("window_order")
    if (
        not isinstance(window_order, int)
        or isinstance(window_order, bool)
        or not 1 <= window_order <= MAX_WORKSPACE_OFFSET
    ):
        raise BlockWorkspaceIntegrityError("operation-failure-projection")
    block_ids = failure.get("affected_block_ids")
    if (
        not isinstance(block_ids, list)
        or not block_ids
        or len(block_ids) > MAX_WORKSPACE_LIMIT * 250
        or any(
            not isinstance(block_id, str) or not block_id or len(block_id) > 128
            for block_id in block_ids
        )
        or len(block_ids) != len(set(block_ids))
    ):
        raise BlockWorkspaceIntegrityError("operation-failure-projection")
    state = failure.get("dispatch_state")
    failure_kind = failure.get("failure_kind")
    action = failure.get("recovery_action")
    if (
        not isinstance(state, str)
        or state not in _PARTIAL_FAILURE_STATES
        or not isinstance(failure_kind, str)
        or _PARTIAL_RECOVERY_ACTIONS.get((state, failure_kind)) != action
    ):
        raise BlockWorkspaceIntegrityError("operation-failure-projection")
    remaining = failure.get("remaining_dispatches")
    if (
        not isinstance(remaining, int)
        or isinstance(remaining, bool)
        or not 0 <= remaining <= 2
    ):
        raise BlockWorkspaceIntegrityError("operation-failure-projection")
    return {
        "attempt_id": attempt_id,
        "window_order": window_order,
        "affected_block_ids": list(block_ids),
        "dispatch_state": state,
        "failure_kind": failure_kind,
        "recovery_action": action,
        "remaining_dispatches": remaining,
    }


def _safe_operation(operation: Any) -> dict[str, Any]:
    """Allowlist operation state; queue and owner/provider details stay private."""

    if operation is None:
        return {}
    payload = {
        "id": str(operation.id),
        "kind": str(operation.kind),
        "state": str(operation.state),
        "progress": operation.progress,
        "message": str(operation.message or ""),
        "error_code": operation.error_code,
        "created_at": _safe_timestamp(operation.created_at),
        "started_at": _safe_timestamp(operation.started_at),
        "finished_at": _safe_timestamp(operation.finished_at),
        "updated_at": _safe_timestamp(operation.updated_at),
    }
    failure = _safe_partial_failure(operation)
    if failure is not None:
        payload["block_failure"] = failure
    return payload


def _safe_project(project: Any) -> dict[str, Any]:
    return {
        "id": project.id,
        "name": project.name,
        "source_lang": project.source_lang,
        "target_lang": project.target_lang,
        "engine_contract": DOCUMENT_BLOCK_ENGINE_CONTRACT,
    }


def _translation_unit_mode(project: Any) -> str:
    settings = getattr(project, "settings", None)
    raw = (
        settings.get("translation_unit_mode") if isinstance(settings, Mapping) else None
    )
    try:
        return normalize_translation_unit_mode(raw)
    except BlockContractError as exc:
        raise BlockWorkspaceIntegrityError("translation-unit-mode-invalid") from exc


_VALIDATION_MESSAGES = {
    "target-text-empty": "Translation text is empty.",
    "target-text-unowned": "Translation text escaped its editable source field.",
    "target-lexical-cardinality": "Translated descriptions no longer match the source fields.",
    "target-lexical-duplicate": "A translated description was duplicated.",
    "target-lexical-order": "Translated descriptions no longer match the source fields.",
    "target-literal-preservation": "Fixed source text was changed or removed.",
    "target-fixed-reference-order": "A fixed source reference moved outside its allowed position.",
    "target-fixed-token-order": "Fixed source formatting changed order.",
    "target-fixed-token-scope": "Formatting moved outside a fixed source reference.",
    "target-token-cardinality": "Source formatting was added or removed.",
    "target-token-kind": "Source formatting became invalid.",
    "target-token-nesting": "Source formatting became improperly nested.",
}


def _safe_validation(evidence: Any) -> dict[str, Any]:
    if not isinstance(evidence, Mapping):
        return {"valid": False, "findings": []}
    findings: list[dict[str, Any]] = []
    raw_findings = evidence.get("findings")
    if isinstance(raw_findings, list):
        for finding in raw_findings:
            if not isinstance(finding, Mapping):
                continue
            code = finding.get("code")
            severity = finding.get("severity")
            if not isinstance(code, str) or len(code) > 128:
                continue
            findings.append(
                {
                    "message": _VALIDATION_MESSAGES.get(
                        code,
                        "This translation no longer matches its protected source structure.",
                    ),
                    "severity": severity
                    if isinstance(severity, str) and len(severity) <= 128
                    else "fatal",
                }
            )
    return {
        "valid": bool(evidence.get("valid")),
        "findings": findings,
    }


def _safe_alignment_projection(
    block: Any,
    alignment: TranslationAlignmentRevision,
    span_set: TranslationTargetSpanSet,
    *,
    record: TranslationSourcePlan,
    verified_plan: Any,
    verification_cache: TranslationRehydrationCache,
) -> dict[str, Any]:
    """Project verified sentence memberships without durable internals."""

    try:
        groups = rehydrate_alignment_revision(
            alignment,
            record,
            verified_plan=verified_plan,
            verification_cache=verification_cache,
        )
        spans = rehydrate_target_span_set(
            span_set,
            record,
            verified_plan=verified_plan,
            verification_cache=verification_cache,
        )
    except Exception as exc:
        raise BlockWorkspaceIntegrityError("alignment-corrupt") from exc
    if not isinstance(span_set.projection, str):
        raise BlockWorkspaceIntegrityError("alignment-corrupt")

    anchors_by_id = {anchor.id: anchor for anchor in block.anchors}
    spans_by_id = {span.id: span for span in spans}
    projected_groups: list[dict[str, Any]] = []
    for order, group in enumerate(groups, start=1):
        source_anchors = [anchors_by_id.get(item) for item in group.source_anchor_ids]
        target_spans = [spans_by_id.get(item) for item in group.target_span_ids]
        if any(item is None for item in (*source_anchors, *target_spans)):
            raise BlockWorkspaceIntegrityError("alignment-group-owner")
        source_items = [
            {
                "order": anchor.order,
                "text": block.projection[anchor.start : anchor.end],
            }
            for anchor in source_anchors
        ]
        target_items = [
            {
                "order": span.order,
                "text": span_set.projection[span.start : span.end],
            }
            for span in target_spans
        ]
        source_count = len(source_items)
        target_count = len(target_items)
        if source_count == 1 and target_count == 1:
            kind = "one_to_one"
        elif source_count == 1:
            kind = "split"
        elif target_count == 1:
            kind = "merge"
        else:
            kind = "many_to_many"
        projected_groups.append(
            {
                "order": order,
                "kind": kind,
                "source_sentences": source_items,
                "target_sentences": target_items,
            }
        )
    if alignment.method == "provider-manifest-v1":
        origin = "model_reported"
    elif alignment.method == "deterministic-count-v1":
        origin = "automatic_fallback"
    elif alignment.method == "editor-carried-v1":
        origin = "carried_forward"
    elif alignment.method == "editor-segment-v1":
        origin = "user_edited"
    else:
        raise BlockWorkspaceIntegrityError("alignment-method-unsupported")
    return {
        "origin": origin,
        "review_state": alignment.review_state,
        "groups": projected_groups,
    }


def _target_projection(
    record: TranslationSourcePlan,
    block: Any,
    pointer: TranslationTargetBlockActive | None,
    alignment_pointer: TranslationTargetAlignmentActive | None,
    *,
    verified_plan: Any | None = None,
    revisions_by_id: Mapping[str, TranslationTargetBlockRevision],
    alignments_by_id: Mapping[str, TranslationAlignmentRevision],
    verification_cache: TranslationRehydrationCache,
    notes_by_revision: Mapping[str, tuple[Any, ...]],
    include_alignment: bool = False,
) -> dict[str, Any] | None:
    if pointer is None or alignment_pointer is None:
        if pointer is not None or alignment_pointer is not None:
            raise BlockWorkspaceIntegrityError("target-active-cardinality")
        return None
    if (
        pointer.source_plan_id != record.id
        or pointer.block_id != block.id
        or alignment_pointer.source_plan_id != record.id
        or alignment_pointer.block_id != block.id
        or pointer.version != alignment_pointer.version
        or pointer.revision_id != alignment_pointer.revision_id
    ):
        raise BlockWorkspaceIntegrityError("target-active-owner")
    revision = revisions_by_id.get(pointer.revision_id)
    alignment = alignments_by_id.get(alignment_pointer.alignment_revision_id)
    if revision is None or alignment is None:
        raise BlockWorkspaceIntegrityError("target-active-missing")
    try:
        candidate = rehydrate_target_revision(
            revision,
            record,
            verified_plan=verified_plan,
            verification_cache=verification_cache,
        )
    except Exception as exc:
        raise BlockWorkspaceIntegrityError("target-revision-corrupt") from exc
    try:
        rehydrate_alignment_revision(
            alignment,
            record,
            verified_plan=verified_plan,
            verification_cache=verification_cache,
        )
    except Exception as exc:
        raise BlockWorkspaceIntegrityError("alignment-corrupt") from exc
    if include_alignment:
        span_set = alignment.span_set
        if span_set is None:
            raise BlockWorkspaceIntegrityError("alignment-span-set-missing")
    try:
        verify_active_pair(
            pointer,
            alignment_pointer,
            revision=revision,
            alignment=alignment,
        )
    except Exception as exc:
        raise BlockWorkspaceIntegrityError("target-active-corrupt") from exc
    try:
        verified_notes = rehydrate_target_block_notes(
            revision,
            record,
            rows=notes_by_revision.get(revision.id, ()),
            verified_plan=verified_plan,
            verification_cache=verification_cache,
        )
    except Exception as exc:
        raise BlockWorkspaceIntegrityError("target-notes-corrupt") from exc
    projection = {
        "active_version": pointer.version,
        "editor": compact_candidate_projection(block, candidate),
        "validation": _safe_validation(revision.validation_evidence),
        "translator_notes": [],
    }
    if verified_notes:
        projection["translator_notes"] = [
            {
                "id": str(note_row.id),
                "anchor_text": note_row.anchor_text,
                "body": note_row.body,
                "author_type": note_row.author_type,
            }
            for note_row in sorted(
                notes_by_revision.get(revision.id, ()),
                key=lambda item: (item.order_index, str(item.id)),
            )
        ]
    if include_alignment:
        projection["alignment"] = _safe_alignment_projection(
            block,
            alignment,
            span_set,
            record=record,
            verified_plan=verified_plan,
            verification_cache=verification_cache,
        )
    return projection


def _rehydrate_workspace_objects(
    workspace_plan: WorkspacePlan,
    *,
    include_alignment: bool = False,
) -> tuple[
    dict[str, TranslationTargetBlockActive],
    dict[str, dict[str, Any]],
]:
    record, plan = workspace_plan.record, workspace_plan.plan
    block_ids = {block.id for block in plan.blocks}
    target_pointers = {
        pointer.block_id: pointer
        for pointer in TranslationTargetBlockActive.query.filter_by(
            source_plan_id=record.id
        ).all()
    }
    alignment_pointers = {
        pointer.block_id: pointer
        for pointer in TranslationTargetAlignmentActive.query.filter_by(
            source_plan_id=record.id
        ).all()
    }
    if set(target_pointers) != set(alignment_pointers) or not set(
        target_pointers
    ).issubset(block_ids):
        raise BlockWorkspaceIntegrityError("target-active-cardinality")

    # Load immutable dependencies in bounded batches.  All revisions stay in
    # the identity map so active parent lineages can be checked without an N+1
    # walk; only active alignment/span/group material is loaded and verified.
    revision_rows = (
        TranslationTargetBlockRevision.query.filter_by(source_plan_id=record.id)
        .order_by(TranslationTargetBlockRevision.created_at)
        .all()
    )
    revisions_by_id = {revision.id: revision for revision in revision_rows}
    alignment_ids = {
        pointer.alignment_revision_id for pointer in alignment_pointers.values()
    }
    revision_ids = {pointer.revision_id for pointer in target_pointers.values()}
    if revision_ids:
        note_rows = (
            TranslationTargetBlockNote.query.filter(
                TranslationTargetBlockNote.source_plan_id == record.id,
                TranslationTargetBlockNote.revision_id.in_(revision_ids),
            )
            .order_by(
                TranslationTargetBlockNote.revision_id,
                TranslationTargetBlockNote.order_index,
            )
            .all()
        )
    else:
        note_rows = []
    notes_by_revision: dict[str, tuple[Any, ...]] = {}
    for row in note_rows:
        notes_by_revision.setdefault(row.revision_id, ())
        notes_by_revision[row.revision_id] += (row,)
    if alignment_ids:
        alignment_rows = (
            TranslationAlignmentRevision.query.filter(
                TranslationAlignmentRevision.source_plan_id == record.id,
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
        alignment_rows = []
    alignments_by_id = {alignment.id: alignment for alignment in alignment_rows}
    if set(alignments_by_id) != alignment_ids:
        raise BlockWorkspaceIntegrityError("target-active-missing")

    blocks_by_id = {block.id: block for block in plan.blocks}
    target_projections: dict[str, dict[str, Any]] = {}
    verification_cache = TranslationRehydrationCache()
    for block_id, target_pointer in target_pointers.items():
        block = blocks_by_id.get(block_id)
        if block is None:
            raise BlockWorkspaceIntegrityError("target-active-owner")
        target_projection = _target_projection(
            record,
            block,
            target_pointer,
            alignment_pointers[block_id],
            verified_plan=plan,
            revisions_by_id=revisions_by_id,
            alignments_by_id=alignments_by_id,
            verification_cache=verification_cache,
            notes_by_revision=notes_by_revision,
            include_alignment=include_alignment,
        )
        if target_projection is None:
            raise BlockWorkspaceIntegrityError("target-active-cardinality")
        target_projections[block_id] = target_projection
    return target_pointers, target_projections


def _target_summary(
    plan: Any,
    target_pointers: Mapping[str, TranslationTargetBlockActive],
) -> dict[str, int | bool]:
    """Summarize target coverage after exact workspace rehydration.

    ``_rehydrate_workspace_objects`` validates every active target/alignment
    pair and its parent lineage before this helper is called.  Consequently a
    pointer counted here is both active and structurally valid; inactive
    append-only history is audited by the operation that consumes it rather
    than re-audited on every paginated workspace read.
    """

    total_blocks = len(plan.blocks)
    active_targets = len(target_pointers)
    structurally_valid_active_targets = active_targets
    return {
        "total_blocks": total_blocks,
        "active_targets": active_targets,
        "structurally_valid_active_targets": structurally_valid_active_targets,
        "document_complete": structurally_valid_active_targets == total_blocks,
    }


def serialize_block_item(
    block: Any,
    target: Mapping[str, Any] | None,
    *,
    reader_notes: list[dict[str, Any]] | None = None,
    review: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from .scholarly_wire import block_review

    return {
        "id": block.id,
        "order": block.order,
        "role": block.role,
        "source": {"editor": compact_source_projection(block)},
        "target": target,
        "reader_notes": list(reader_notes or []),
        "review": review if review is not None else block_review(None),
    }


def _page_review_projections(
    project: Any,
    workspace_plan: WorkspacePlan,
    page: list[Any] | tuple[Any, ...],
    *,
    actor: Any | None,
    include_proposed: bool,
    target_pointers: Mapping[str, Any] | None = None,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, dict[str, Any]]]:
    """Project current reader notes and review heads for one visible page."""

    # Local imports avoid turning the scholarly service's existing workspace
    # dependency into an import cycle.
    from . import scholarly_review
    from .scholarly_wire import block_review, reader_note

    block_ids = [str(block.id) for block in page]
    try:
        notes = scholarly_review.list_reader_notes(
            project,
            block_ids=block_ids,
            include_proposed=include_proposed,
            include_retired=False,
            actor=actor,
            _source_plan=workspace_plan.record,
            _blocks=workspace_plan.plan.blocks,
        )
        reviews = scholarly_review.list_block_reviews(
            project,
            block_ids=block_ids,
            actor=actor,
            _source_plan=workspace_plan.record,
            _blocks=workspace_plan.plan.blocks,
        )
    except Exception as exc:
        raise BlockWorkspaceIntegrityError("review-projection-corrupt") from exc
    notes_by_block: dict[str, list[dict[str, Any]]] = {
        block_id: [] for block_id in block_ids
    }
    for note in notes:
        block_id = str(note.get("block_id") or "")
        if block_id not in notes_by_block:
            raise BlockWorkspaceIntegrityError("review-projection-owner")
        notes_by_block[block_id].append(reader_note(note))
    reviews_by_block: dict[str, dict[str, Any]] = {}
    for review_row in reviews:
        block_id = str(review_row.get("block_id") or "")
        if block_id not in notes_by_block or block_id in reviews_by_block:
            raise BlockWorkspaceIntegrityError("review-projection-owner")
        reviews_by_block[block_id] = block_review(review_row)
    from .review_coverage import review_coverage_manifest

    coverage = review_coverage_manifest(
        project, block_ids=block_ids, _workspace_plan=workspace_plan, _active_targets=target_pointers,
    )
    for item in coverage.get("blocks", []):
        block_id = item["block_id"]
        review = reviews_by_block.setdefault(block_id, block_review(None))
        review["coverage"] = item.get("kinds", {})
    return notes_by_block, reviews_by_block


def build_workspace_payload(
    project: Any,
    *,
    offset: int,
    limit: int,
    latest_operation: Any | None = None,
    access_role: str = "owner",
    access_label: str | None = None,
    actor: Any | None = None,
) -> dict[str, Any]:
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise BlockWorkspaceIntegrityError("workspace-offset")
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= MAX_WORKSPACE_LIMIT
    ):
        raise BlockWorkspaceIntegrityError("workspace-limit")
    if offset > MAX_WORKSPACE_OFFSET:
        raise BlockWorkspaceIntegrityError("workspace-offset")
    if access_role not in {"owner", "reviewer", "viewer"}:
        raise BlockWorkspaceIntegrityError("workspace-access")
    if actor is not None and getattr(actor, "role", None) != access_role:
        raise BlockWorkspaceIntegrityError("workspace-access")
    translation_unit_mode = _translation_unit_mode(project)
    aligned_sentences = translation_unit_mode == TRANSLATION_UNIT_MODE_ALIGNED_SENTENCES
    workspace_plan = load_block_source_plan(project)
    target_pointers, target_projections = _rehydrate_workspace_objects(
        workspace_plan,
        include_alignment=aligned_sentences,
    )
    blocks = workspace_plan.plan.blocks
    page = blocks[offset : offset + limit]
    notes_by_block, reviews_by_block = _page_review_projections(
        project,
        workspace_plan,
        page,
        actor=actor,
        include_proposed=access_role != "viewer",
        target_pointers=target_pointers,
    )
    payload = {
        "contract": (
            ALIGNED_SENTENCES_WORKSPACE_CONTRACT
            if aligned_sentences
            else WORKSPACE_CONTRACT
        ),
        "access": {"role": access_role, "label": access_label},
        "project": _safe_project(project),
        "source_plan": {
            "id": workspace_plan.record.id,
            "version": workspace_plan.active_version,
        },
        "target_summary": _target_summary(workspace_plan.plan, target_pointers),
        "latest_operation": _safe_operation(latest_operation),
        "blocks": [
            serialize_block_item(
                block,
                target_projections.get(block.id),
                reader_notes=notes_by_block.get(block.id),
                review=reviews_by_block.get(block.id),
            )
            for block in page
        ],
        "pagination": {
            "offset": offset,
            "limit": limit,
            "total": len(blocks),
            "has_more": offset + len(page) < len(blocks),
        },
    }
    if aligned_sentences:
        payload["review_mode"] = TRANSLATION_UNIT_MODE_ALIGNED_SENTENCES
    return payload


def current_block_projection(project: Any, block_id: str) -> dict[str, Any]:
    """Return one fresh block projection for an optimistic-concurrency error."""

    translation_unit_mode = _translation_unit_mode(project)
    workspace_plan = load_block_source_plan(project)
    target_pointers, target_projections = _rehydrate_workspace_objects(
        workspace_plan,
        include_alignment=(
            translation_unit_mode == TRANSLATION_UNIT_MODE_ALIGNED_SENTENCES
        ),
    )
    block = next(
        (item for item in workspace_plan.plan.blocks if item.id == block_id), None
    )
    if block is None:
        raise BlockWorkspaceIntegrityError("block-owner")
    notes_by_block, reviews_by_block = _page_review_projections(
        project,
        workspace_plan,
        [block],
        actor=None,
        include_proposed=True,
        target_pointers=target_pointers,
    )
    return serialize_block_item(
        block,
        target_projections.get(block.id),
        reader_notes=notes_by_block.get(block.id),
        review=reviews_by_block.get(block.id),
    )


def _active_glossary_entries(record: TranslationSourcePlan) -> list[dict[str, Any]]:
    """Return verified active terminology without leaking approval evidence."""

    concepts = (
        TranslationGlossaryConcept.query.filter_by(
            project_id=record.project_id,
            source_plan_id=record.id,
            owner_user_id=record.project.user_id,
        )
        .order_by(TranslationGlossaryConcept.created_at, TranslationGlossaryConcept.id)
        .all()
    )
    entries: list[dict[str, Any]] = []
    for concept in concepts:
        active = db.session.get(TranslationGlossaryActive, concept.id)
        if active is None:
            continue
        try:
            revision = verify_glossary_active(active)
            payload = rehydrate_glossary_revision(revision, record)
        except Exception as exc:
            raise BlockWorkspaceIntegrityError("glossary-corrupt") from exc
        if (
            revision.concept_id != concept.id
            or revision.project_id != record.project_id
            or revision.source_plan_id != record.id
            or revision.author_type != "human"
            or revision.revision_state != "locked"
        ):
            raise BlockWorkspaceIntegrityError("glossary-owner")
        entries.append(
            {
                "concept_id": concept.id,
                "active_version": active.version,
                "source_term": payload.source_term,
                "target_term": payload.target_term,
                "notes": payload.notes,
                "scope": payload.scope,
                "updated_at": _safe_timestamp(active.updated_at),
                **({"usage": glossary_usage_view(payload)} if payload.usage is not None else {}),
            }
        )
    entries.sort(key=lambda item: normalized_term(str(item["source_term"])))
    return entries


def _pending_glossary_proposals(
    record: TranslationSourcePlan,
    *,
    active_concept_ids: set[str],
    excluded_revision_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Return latest verified model proposals which have no active human lock."""

    concepts = (
        TranslationGlossaryConcept.query.filter_by(
            project_id=record.project_id,
            source_plan_id=record.id,
            owner_user_id=record.project.user_id,
        )
        .order_by(TranslationGlossaryConcept.created_at, TranslationGlossaryConcept.id)
        .all()
    )
    revisions = (
        TranslationGlossaryRevision.query.filter_by(
            project_id=record.project_id,
            source_plan_id=record.id,
        )
        .order_by(
            TranslationGlossaryRevision.concept_id,
            TranslationGlossaryRevision.revision_number.desc(),
            TranslationGlossaryRevision.id.desc(),
        )
        .all()
    )
    latest_by_concept: dict[str, TranslationGlossaryRevision] = {}
    for revision in revisions:
        latest_by_concept.setdefault(revision.concept_id, revision)
    excluded_revision_ids = excluded_revision_ids or set()
    proposals: list[dict[str, Any]] = []
    for concept in concepts:
        if concept.id in active_concept_ids:
            continue
        revision = latest_by_concept.get(concept.id)
        if revision is None:
            raise BlockWorkspaceIntegrityError("glossary-corrupt")
        if revision.id in excluded_revision_ids:
            # Externally submitted dossier terminology has its own attributed
            # suggestion row.  Keep the linked glossary revision as durable
            # evidence, but expose only the suggestion so reviewers do not see
            # (or resolve) the same conclusion twice.
            continue
        try:
            payload = rehydrate_glossary_revision(revision, record)
        except Exception as exc:
            raise BlockWorkspaceIntegrityError("glossary-corrupt") from exc
        if revision.author_type != "model" or revision.revision_state != "proposal":
            raise BlockWorkspaceIntegrityError("glossary-owner")
        proposals.append(
            {
                "proposal_id": revision.id,
                "concept_id": concept.id,
                "source_term": payload.source_term,
                "target_term": payload.target_term,
                "notes": payload.notes,
                "scope": payload.scope,
                "proposed_at": _safe_timestamp(revision.created_at),
                **({"usage": glossary_usage_view(payload)} if payload.usage is not None else {}),
            }
        )
    proposals.sort(key=lambda item: normalized_term(str(item["source_term"])))
    return proposals


def build_block_glossary_payload(
    project: Any, *, include_proposals: bool = True
) -> dict[str, Any]:
    """Project active constraints and reviewable analysis suggestions."""

    record, _active_version = _load_glossary_source_plan(project)
    entries = _active_glossary_entries(record)
    suggestions: list[dict[str, Any]] = []
    dossier_proposal_revision_ids: set[str] = set()
    if include_proposals:
        from .scholarly_wire import glossary_suggestion

        rows = (
            TranslationGlossarySuggestion.query.filter_by(
                project_id=record.project_id,
                source_plan_id=record.id,
                state="proposed",
            )
            .order_by(
                TranslationGlossarySuggestion.created_at,
                TranslationGlossarySuggestion.id,
            )
            .all()
        )
        suggestions = [glossary_suggestion(row) for row in rows]
        dossier_proposal_revision_ids = {
            str(row.glossary_revision_id)
            for row in TranslationResearchDossierGlossaryProposal.query.filter_by(
                project_id=record.project_id,
                source_plan_id=record.id,
            ).all()
        }
    return {
        "contract": "translator-block-glossary-v3",
        "project_id": record.project_id,
        "source_plan_id": record.id,
        "entries": entries,
        "proposals": (
            _pending_glossary_proposals(
                record,
                active_concept_ids={str(entry["concept_id"]) for entry in entries},
                excluded_revision_ids=dossier_proposal_revision_ids,
            )
            if include_proposals
            else []
        ),
        "suggestions": suggestions,
    }


def persist_human_glossary_entry(
    project: Any,
    *,
    source_term: str,
    target_term: str,
    notes: str | None,
    author_user_id: int,
    concept_id: str | None = None,
    expected_active_version: int = 0,
    expected_proposal_id: str | None = None,
    accepted_target_forms: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Append, approve, and activate one explicit owner-authored glossary edit."""

    if (
        isinstance(expected_active_version, bool)
        or not isinstance(expected_active_version, int)
        or expected_active_version < 0
    ):
        raise BlockWorkspaceIntegrityError("glossary-version")
    if expected_proposal_id is not None and (
        not isinstance(expected_proposal_id, str) or not expected_proposal_id
    ):
        raise BlockWorkspaceIntegrityError("glossary-proposal-conflict")
    workspace_plan = load_block_source_plan(project)
    record = workspace_plan.record
    if author_user_id != record.project.user_id:
        raise BlockWorkspaceIntegrityError("glossary-owner")
    variants: list[str] | None = None
    if accepted_target_forms is not None:
        if (
            isinstance(accepted_target_forms, (str, bytes))
            or not isinstance(accepted_target_forms, Sequence)
            or len(accepted_target_forms) > 128
        ):
            raise BlockWorkspaceIntegrityError("glossary-target-forms")
        variants = []
        seen_variants: set[str] = set()
        preferred_key = " ".join(target_term.casefold().split())
        for raw_variant in accepted_target_forms:
            if not isinstance(raw_variant, str):
                raise BlockWorkspaceIntegrityError("glossary-target-forms")
            variant = raw_variant.strip()
            if not variant or len(variant) > 4096:
                raise BlockWorkspaceIntegrityError("glossary-target-forms")
            key = normalized_term(variant)
            if key == preferred_key or key in seen_variants:
                continue
            seen_variants.add(key)
            variants.append(variant)

    try:
        usage = None
        if concept_id is not None:
            current_active = db.session.get(TranslationGlossaryActive, concept_id)
            origin_id = expected_proposal_id or (current_active.revision_id if current_active else None)
            if origin_id:
                origin = db.session.get(TranslationGlossaryRevision, origin_id)
                if origin is None or origin.concept_id != concept_id:
                    raise BlockContractError("glossary-proposal-conflict")
                usage = rehydrate_glossary_revision(origin, record).usage
        if variants is not None:
            prior_usage = usage or {}
            usage = GlossaryUsage(
                sense=str(prior_usage.get("sense", "")),
                match_policy=prior_usage.get("match_policy", "preferred"),
                source_forms=list(prior_usage.get("source_forms") or [source_term]),
                accepted_target_forms=variants,
                observations=list(prior_usage.get("observations") or []),
                provenance="human-owner",
                request_fingerprint=private_witness(
                    {
                        "project_id": record.project_id,
                        "source_plan_id": record.id,
                        "concept_id": concept_id,
                        "source_term": source_term,
                        "target_term": target_term,
                        "accepted_target_forms": variants,
                        "expected_active_version": expected_active_version,
                        "expected_proposal_id": expected_proposal_id,
                    },
                    purpose="translator-human-glossary-usage-v1",
                ),
            ).model_dump()
        payload = GlossaryPayload(
            source_term=source_term,
            target_term=target_term,
            notes=notes,
            scope="document",
            usage=usage,
        )
    except (BlockContractError, ValueError) as exc:
        code = getattr(exc, "code", "glossary-usage")
        raise BlockWorkspaceIntegrityError(code) from exc

    active_entries = _active_glossary_entries(record)
    requested_key = normalized_term(payload.source_term)
    duplicate = next(
        (
            entry
            for entry in active_entries
            if normalized_term(str(entry["source_term"])) == requested_key
            and entry["concept_id"] != concept_id
            and " ".join(str((entry.get("usage") or {}).get("sense", "")).casefold().split())
            == " ".join(str((payload.usage or {}).get("sense", "")).casefold().split())
        ),
        None,
    )
    if duplicate is not None:
        raise BlockWorkspaceIntegrityError("glossary-duplicate")

    if concept_id is None:
        if expected_active_version != 0 or expected_proposal_id is not None:
            raise BlockWorkspaceIntegrityError("glossary-active-conflict")
        requested_sense = " ".join(str((payload.usage or {}).get("sense", "")).casefold().split())
        for revision in find_glossary_revisions_for_term(record, payload.source_term):
            try:
                existing_payload = rehydrate_glossary_revision(revision, record)
            except Exception as exc:
                raise BlockWorkspaceIntegrityError("glossary-corrupt") from exc
            if " ".join(str((existing_payload.usage or {}).get("sense", "")).casefold().split()) == requested_sense:
                raise BlockWorkspaceIntegrityError("glossary-duplicate")
        concept = None
        proposal = None
    else:
        concept = db.session.get(TranslationGlossaryConcept, concept_id)
        if (
            concept is None
            or concept.project_id != record.project_id
            or concept.source_plan_id != record.id
            or concept.owner_user_id != record.project.user_id
            or concept.concept_scope != "document"
        ):
            raise BlockWorkspaceIntegrityError("glossary-owner")
        active = db.session.get(TranslationGlossaryActive, concept.id)
        if active is None:
            if expected_active_version != 0 or expected_proposal_id is None:
                raise BlockWorkspaceIntegrityError("glossary-proposal-conflict")
            proposal = (
                TranslationGlossaryRevision.query.filter_by(concept_id=concept.id)
                .order_by(
                    TranslationGlossaryRevision.revision_number.desc(),
                    TranslationGlossaryRevision.id.desc(),
                )
                .first()
            )
            if (
                proposal is None
                or proposal.id != expected_proposal_id
                or proposal.author_type != "model"
                or proposal.revision_state != "proposal"
            ):
                raise BlockWorkspaceIntegrityError("glossary-proposal-conflict")
            try:
                rehydrate_glossary_revision(proposal, record)
            except Exception as exc:
                raise BlockWorkspaceIntegrityError("glossary-corrupt") from exc
        else:
            if (
                active.version != expected_active_version
                or expected_proposal_id is not None
            ):
                raise BlockWorkspaceIntegrityError("glossary-active-conflict")
            try:
                verify_glossary_active(active)
            except Exception as exc:
                raise BlockWorkspaceIntegrityError("glossary-corrupt") from exc
            proposal = None

    try:
        with db.session.begin_nested():
            if concept is None:
                concept = create_glossary_concept(record)
            if proposal is None:
                proposal = persist_glossary_revision(
                    record,
                    concept,
                    payload=payload,
                    author_type="system",
                    revision_state="proposal",
                )
            approval = RuntimeApprovalRequest(
                requester_user_id=author_user_id,
                approver_user_id=None,
                subject_type="translator.glossary-lock.v1",
                subject_id=proposal.id,
                preview_payload=glossary_lock_preview(
                    record, concept, proposal, payload
                ),
                status="pending",
                resolved_at=None,
            )
            db.session.add(approval)
            db.session.flush()
            locked = approve_glossary_proposal(
                record,
                proposal,
                approved_payload=payload,
                approval=approval.id,
            )
            activate_glossary_revision(
                locked,
                expected_version=expected_active_version,
            )
        db.session.commit()
    except BlockWorkspaceIntegrityError:
        db.session.rollback()
        raise
    except BlockContractError as exc:
        db.session.rollback()
        if exc.code in {"glossary-active-conflict", "glossary-approval-used"}:
            raise BlockWorkspaceIntegrityError("glossary-active-conflict") from exc
        raise BlockWorkspaceIntegrityError(exc.code) from exc
    except Exception as exc:
        db.session.rollback()
        raise BlockWorkspaceIntegrityError("glossary-persist-failed") from exc
    return build_block_glossary_payload(project)


def _persist_manual_alignment(
    record: TranslationSourcePlan,
    plan: Any,
    block: Any,
    revision: TranslationTargetBlockRevision,
    active: TranslationTargetBlockActive | None,
    alignment_active: TranslationTargetAlignmentActive | None,
    *,
    restore_parent_alignment: bool,
    sentence_manifest: SentenceAlignmentManifest | None = None,
    sentence_edit_action: str | None = None,
) -> TranslationAlignmentRevision:
    """Carry verified sentence membership through local edits, never by counts."""

    reference = None
    manifest = None
    if active is not None and alignment_active is not None:
        verify_active_pair(active, alignment_active)
        previous = db.session.get(TranslationTargetBlockRevision, active.revision_id)
        if previous is None:
            raise BlockWorkspaceIntegrityError("alignment-target-missing")
        rehydrate_target_revision(previous, record, verified_plan=plan)
        if restore_parent_alignment:
            references = (
                TranslationAlignmentRevision.query.filter_by(
                    source_plan_id=record.id,
                    block_id=block.id,
                    revision_id=previous.parent_revision_id,
                )
                .limit(2)
                .all()
                if previous.parent_revision_id is not None
                else []
            )
            if len(references) != 1:
                raise BlockWorkspaceIntegrityError("alignment-parent-unavailable")
            reference = references[0]
        else:
            reference = db.session.get(
                TranslationAlignmentRevision, alignment_active.alignment_revision_id
            )
        if reference is None:
            raise BlockWorkspaceIntegrityError("alignment-target-missing")
        if (
            reference.source_plan_id != record.id
            or reference.block_id != block.id
            or (not restore_parent_alignment and reference.revision_id != previous.id)
        ):
            raise BlockWorkspaceIntegrityError("alignment-group-owner")
        groups = rehydrate_alignment_revision(reference, record, verified_plan=plan)
        previous_spans = db.session.get(TranslationTargetSpanSet, reference.span_set_id)
        if previous_spans is None:
            raise BlockWorkspaceIntegrityError("alignment-span-set-missing")
        spans = rehydrate_target_span_set(previous_spans, record, verified_plan=plan)
        candidate = rehydrate_target_revision(revision, record, verified_plan=plan)
        if sentence_manifest is not None:
            manifest = sentence_manifest
        elif reference.method in {
            "provider-manifest-v1",
            "editor-carried-v1",
            "editor-segment-v1",
        }:
            manifest = rebase_sentence_alignment(
                block,
                candidate,
                previous_projection=previous_spans.projection,
                previous_spans=spans,
                previous_groups=groups,
            )
    if restore_parent_alignment and manifest is None:
        raise BlockWorkspaceIntegrityError("alignment-parent-carry-unsafe")
    span_set = persist_target_span_set(
        record, revision, verified_plan=plan, manifest=manifest
    )
    if manifest is None:
        return persist_alignment_revision(record, span_set, verified_plan=plan)
    if reference is None:
        raise BlockWorkspaceIntegrityError("alignment-target-missing")
    spans = rehydrate_target_span_set(span_set, record, verified_plan=plan)
    alignment_method = (
        "editor-segment-v1" if sentence_manifest is not None else "editor-carried-v1"
    )
    carried = tuple(
        AlignmentGroup(
            tuple(block.anchors[index - 1].id for index in group.source_ordinals),
            tuple(spans[index - 1].id for index in group.target_ordinals),
            alignment_method,
            0.5,
            "needs_review",
            {
                "reference_alignment_id": reference.id,
                "reference_method": reference.method,
                **(
                    {"edit_action": sentence_edit_action}
                    if sentence_edit_action is not None
                    else {}
                ),
            },
        )
        for group in manifest.groups
    )
    return persist_alignment_revision(
        record,
        span_set,
        groups=carried,
        method=alignment_method,
        review_state="needs_review",
        verified_plan=plan,
    )


def persist_manual_candidate(
    project: Any,
    block_id: str,
    candidate: Mapping[str, Any] | TargetBlockCandidate | None,
    expected_active_version: int,
    *,
    author_user_id: int | None = None,
    share_grant_id: str | None = None,
    idempotency_key: str | None = None,
    restore_parent_alignment: bool = False,
    sentence_edit: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist and pair one owner or accountless reviewer candidate."""

    sentence_edit_mode = sentence_edit is not None
    if sentence_edit_mode:
        if candidate is not None or restore_parent_alignment:
            raise BlockWorkspaceIntegrityError("candidate-alignment-option")
        if not isinstance(sentence_edit, Mapping):
            raise BlockWorkspaceIntegrityError("sentence-edit-shape")
        try:
            sentence_edit_payload = dict(sentence_edit)
            # Validate that the raw DTO can participate in the exact receipt
            # fingerprint before any current target state is consulted.
            canonical_json(sentence_edit_payload)
        except (TypeError, ValueError, OverflowError) as exc:
            raise BlockWorkspaceIntegrityError("sentence-edit-shape") from exc
    elif not isinstance(candidate, (Mapping, TargetBlockCandidate)):
        raise BlockWorkspaceIntegrityError("candidate-object")
    if not isinstance(restore_parent_alignment, bool):
        raise BlockWorkspaceIntegrityError("candidate-alignment-option")
    if (
        isinstance(expected_active_version, bool)
        or not isinstance(expected_active_version, int)
        or expected_active_version < 0
    ):
        raise BlockWorkspaceIntegrityError("candidate-version")
    if idempotency_key is not None:
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise BlockWorkspaceIntegrityError("candidate-idempotency")
        idempotency_key = idempotency_key.strip()
        if len(idempotency_key) > 128:
            raise BlockWorkspaceIntegrityError("candidate-idempotency")
    workspace_plan = load_block_source_plan(project)
    record, plan = workspace_plan.record, workspace_plan.plan
    block = next((item for item in plan.blocks if item.id == block_id), None)
    if block is None:
        raise BlockWorkspaceIntegrityError("block-owner")

    # Resolve and validate the actor before consulting a receipt.  A revoked
    # reviewer must not be able to replay an otherwise valid historical result.
    reviewer_mode = author_user_id is None and isinstance(share_grant_id, str)
    owner_mode = (
        isinstance(author_user_id, int)
        and not isinstance(author_user_id, bool)
        and author_user_id == record.project.user_id
        and share_grant_id is None
    )
    if not owner_mode and not reviewer_mode:
        raise BlockWorkspaceIntegrityError("candidate-authority")
    if restore_parent_alignment and not owner_mode:
        raise BlockWorkspaceIntegrityError("candidate-authority")
    if reviewer_mode:
        if not share_grant_id:
            raise BlockWorkspaceIntegrityError("candidate-authority")
        grant = db.session.get(TranslationShareGrant, share_grant_id)
        now = utc_now_db()
        try:
            expired = (
                grant is not None
                and grant.expires_at is not None
                and grant.expires_at <= now
            )
        except (TypeError, ValueError, OverflowError) as exc:
            raise BlockWorkspaceIntegrityError("candidate-authority") from exc
        if (
            grant is None
            or grant.id != share_grant_id
            or grant.project_id != record.project_id
            or grant.owner_user_id != record.project.user_id
            or grant.role != "reviewer"
            or grant.status != "active"
            or grant.revoked_at is not None
            or expired
        ):
            raise BlockWorkspaceIntegrityError("candidate-authority")
        actor_type = "reviewer"
        actor_id = f"grant:{grant.id}"
        author_type = "reviewer"
        effective_author_user_id = None
    else:
        actor_type = "owner"
        actor_id = f"user:{author_user_id}"
        author_type = "human"
        effective_author_user_id = author_user_id

    if sentence_edit_mode:
        normalized = None
    else:
        try:
            if (
                isinstance(candidate, Mapping)
                and candidate.get("contract_version") == COMPACT_EDITOR_CONTRACT_VERSION
            ):
                normalized = normalize_editor_candidate(
                    plan=plan,
                    plan_id=record.id,
                    block=block,
                    value=candidate,
                )
            else:
                normalized = normalize_target_candidate(candidate)
        except BlockContractError as exc:
            raise BlockWorkspaceIntegrityError(exc.code) from exc
    if normalized is not None and (
        normalized.block_id != block.id or normalized.source_plan_id != record.id
    ):
        raise BlockWorkspaceIntegrityError("candidate-owner")

    if sentence_edit_mode:
        request_fingerprint = sha256_text(
            canonical_json(
                {
                    "project_id": record.project_id,
                    "source_plan_id": record.id,
                    "block_id": block.id,
                    "sentence_edit": sentence_edit_payload,
                    "expected_active_version": expected_active_version,
                    "actor_type": actor_type,
                    "actor_id": actor_id,
                }
            )
        )
    else:
        request_fingerprint = sha256_text(
            canonical_json(
                {
                    "project_id": record.project_id,
                    "source_plan_id": record.id,
                    "block_id": block.id,
                    "candidate": normalized.to_dict(),
                    "expected_active_version": expected_active_version,
                    "actor_type": actor_type,
                    "actor_id": actor_id,
                    **(
                        {"restore_parent_alignment": True}
                        if restore_parent_alignment
                        else {}
                    ),
                }
            )
        )

    def replay_idempotent_result() -> dict[str, Any] | None:
        if idempotency_key is None:
            return None
        receipt = (
            TranslationTargetEditIdempotency.query.populate_existing()
            .filter_by(
                project_id=record.project_id,
                actor_type=actor_type,
                actor_id=actor_id,
                idempotency_key=idempotency_key,
            )
            .one_or_none()
        )
        if receipt is None:
            return None
        if receipt.request_fingerprint != request_fingerprint:
            raise BlockWorkspaceIntegrityError("candidate-idempotency-conflict")
        safe_result = receipt.safe_result_json
        if not isinstance(safe_result, Mapping):
            raise BlockWorkspaceIntegrityError("candidate-idempotency-corrupt")
        return dict(safe_result)

    if idempotency_key is not None:
        replay = replay_idempotent_result()
        if replay is not None:
            return replay

    active = db.session.get(
        TranslationTargetBlockActive,
        {"source_plan_id": record.id, "block_id": block.id},
    )
    alignment_active = db.session.get(
        TranslationTargetAlignmentActive,
        {"source_plan_id": record.id, "block_id": block.id},
    )
    current_version = 0 if active is None else active.version
    if (
        (active is None and alignment_active is not None)
        or (active is not None and alignment_active is None)
        or (
            active is not None
            and alignment_active is not None
            and (
                active.version != alignment_active.version
                or active.revision_id != alignment_active.revision_id
            )
        )
    ):
        raise BlockWorkspaceIntegrityError("target-active-cardinality")
    if current_version != expected_active_version:
        raise BlockWorkspaceIntegrityError("target-active-conflict")
    if sentence_edit_mode and (active is None or alignment_active is None):
        raise BlockWorkspaceIntegrityError("target-active-conflict")
    try:
        # Keep the active-plan fence, edit, and receipt in one explicit
        # transaction.  Source-plan correction takes the same pointer lock
        # before carrying targets, so the edit either precedes that carry or
        # fails instead of becoming stranded on a superseded plan.
        _ensure_database_transaction()
        require_active_source_plan(record.project_id, record.id, lock=True)
        if (
            find_active_operation(
                record.project_id,
                owner_user_id=record.project.user_id,
            )
            is not None
        ):
            raise BlockWorkspaceIntegrityError("target-active-operation")
        with db.session.begin_nested():
            parent_revision_id = active.revision_id if active is not None else None
            sentence_manifest = None
            if sentence_edit_mode:
                if active is None or alignment_active is None:
                    raise BlockWorkspaceIntegrityError("target-active-conflict")
                previous_revision = db.session.get(
                    TranslationTargetBlockRevision, active.revision_id
                )
                previous_alignment = db.session.get(
                    TranslationAlignmentRevision,
                    alignment_active.alignment_revision_id,
                )
                try:
                    verify_active_pair(
                        active,
                        alignment_active,
                        revision=previous_revision,
                        alignment=previous_alignment,
                    )
                    if previous_revision is None or previous_alignment is None:
                        raise BlockContractError("target-active-pair")
                    previous_candidate = rehydrate_target_revision(
                        previous_revision,
                        record,
                        verified_plan=plan,
                    )
                    previous_groups = rehydrate_alignment_revision(
                        previous_alignment,
                        record,
                        verified_plan=plan,
                    )
                    previous_span_set = db.session.get(
                        TranslationTargetSpanSet, previous_alignment.span_set_id
                    )
                    if previous_span_set is None:
                        raise BlockContractError("alignment-span-set-missing")
                    previous_spans = rehydrate_target_span_set(
                        previous_span_set,
                        record,
                        verified_plan=plan,
                    )
                    normalized, sentence_manifest = apply_sentence_edit(
                        block,
                        previous_candidate,
                        previous_spans,
                        previous_groups,
                        sentence_edit_payload,
                    )
                except BlockContractError as exc:
                    raise BlockWorkspaceIntegrityError(exc.code) from exc
            if normalized is None:
                raise BlockWorkspaceIntegrityError("candidate-object")
            revision = persist_target_revision(
                record,
                normalized,
                target_language=project.target_lang,
                author_type=author_type,
                author_user_id=effective_author_user_id,
                parent_revision_id=parent_revision_id,
            )
            if reviewer_mode:
                persist_target_revision_share_attribution(
                    record,
                    revision,
                    share_grant_id=share_grant_id,
                )
            alignment = _persist_manual_alignment(
                record,
                plan,
                block,
                revision,
                active,
                alignment_active,
                restore_parent_alignment=restore_parent_alignment,
                sentence_manifest=sentence_manifest,
                sentence_edit_action=(
                    sentence_edit_payload.get("action")
                    if sentence_edit_mode
                    else None
                ),
            )
            activate_target_revision(
                record,
                block_id=block.id,
                revision_id=revision.id,
                alignment_revision_id=alignment.id,
                expected_version=expected_active_version,
            )
            db.session.flush()
            safe_result = current_block_projection(project, block.id)
            if idempotency_key is not None:
                active_after = db.session.get(
                    TranslationTargetBlockActive,
                    {"source_plan_id": record.id, "block_id": block.id},
                )
                if active_after is None:
                    raise BlockWorkspaceIntegrityError("target-active-cardinality")
                db.session.add(
                    TranslationTargetEditIdempotency(
                        project_id=record.project_id,
                        source_plan_id=record.id,
                        block_id=block.id,
                        target_revision_id=revision.id,
                        actor_type=actor_type,
                        actor_id=actor_id,
                        idempotency_key=idempotency_key,
                        request_fingerprint=request_fingerprint,
                        expected_active_version=expected_active_version,
                        active_version=active_after.version,
                        safe_result_json=safe_result,
                    )
                )
                db.session.flush()
        db.session.commit()
    except BlockWorkspaceIntegrityError:
        db.session.rollback()
        raise
    except BlockContractError as exc:
        db.session.rollback()
        if exc.code in {"target-active-conflict", "target-active-alignment-conflict"}:
            replay = replay_idempotent_result()
            if replay is not None:
                return replay
            raise BlockWorkspaceIntegrityError("target-active-conflict") from exc
        raise BlockWorkspaceIntegrityError(exc.code) from exc
    except IntegrityError as exc:
        db.session.rollback()
        if idempotency_key is not None:
            replay = replay_idempotent_result()
            if replay is not None:
                return replay
            # A concurrent key collision is visible as a committed receipt
            # after rollback.  Without that receipt this was some other
            # persistence failure and must not be misreported as a client key
            # conflict.
            raise BlockWorkspaceIntegrityError("candidate-persist-failed") from exc
        raise BlockWorkspaceIntegrityError("candidate-persist-failed") from exc
    except Exception as exc:
        db.session.rollback()
        raise BlockWorkspaceIntegrityError("candidate-persist-failed") from exc
    return safe_result


__all__ = [
    "ALIGNED_SENTENCES_WORKSPACE_CONTRACT",
    "MAX_CANDIDATE_JSON_BYTES",
    "MAX_GLOSSARY_JSON_BYTES",
    "MAX_WORKSPACE_LIMIT",
    "WORKSPACE_CONTRACT",
    "BlockWorkspaceIntegrityError",
    "build_block_glossary_payload",
    "build_workspace_payload",
    "current_block_projection",
    "is_document_block_engine",
    "load_block_source_plan",
    "persist_human_glossary_entry",
    "persist_manual_candidate",
    "require_document_block_engine",
    "require_segment_engine",
]
