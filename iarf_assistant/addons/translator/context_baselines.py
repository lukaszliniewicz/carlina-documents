"""Frozen Phase-5 context selections and verified ContextSlot adapters."""

from __future__ import annotations

import hmac
import uuid
from collections.abc import Iterable, Mapping
from typing import Any, cast

from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError, OperationalError

from carlina.core.utc import utc_now_db
from iarf_assistant.core.encryption import keyed_lookup_candidates, keyed_lookup_digest
from iarf_assistant.database_utils import is_database_locked
from iarf_assistant.extensions import db

from .analysis_persistence import rehydrate_context_artifact_revision
from .block_contract import (
    BlockContractError,
    canonical_json,
    sha256_text,
)
from .block_contract import (
    TranslationSourcePlan as CanonicalSourcePlan,
)
from .block_persistence import _ensure_database_transaction, rehydrate_source_plan
from .embedding_projection import (
    verified_active_embedding_reference,
    verify_pinned_embedding_reference,
)
from .glossary_persistence import rehydrate_glossary_revision
from .analysis_contract import glossary_prompt_mapping
from .integrity_receipts import verify_receipt
from .models import (
    TranslationContextArtifactRevision,
    TranslationContextBaseline,
    TranslationContextBaselineItem,
    TranslationEmbeddingGeneration,
    TranslationGlossaryRevision,
    TranslationOperation,
    TranslationResearchDossier,
    TranslationResearchDossierRevision,
    TranslationResearchRun,
)
from .models import (
    TranslationSourcePlan as TranslationSourcePlanRecord,
)
from .research_persistence import (
    ResearchVerificationContext,
    rehydrate_completed_research_conclusion,
)
from .window_contract import ContextSlot

_ARTIFACT_SLOT = {
    "document_brief": "document_metadata",
    "style_guide": "document_style_brief",
    "entity_register": "structural_context",
}
_GLOSSARY_BINDING_PURPOSE = "translator-glossary-binding-v1"
_OPERATION_PIN_COLUMNS = (
    "source_content_hash",
    "source_plan_id",
    "source_plan_hash",
)
_ANALYSIS_REDUCE_PROFILE_ID = "analysis-reduce-v1"
_ANALYSIS_TERMINOLOGY_CONTRACT_VERSION = "analysis-terminology-guidance-v1"


def _analysis_terminology_slot(
    revision: TranslationContextArtifactRevision,
    baseline: TranslationContextBaseline,
) -> ContextSlot | None:
    """Expose witnessed model proposals as advisory prompt evidence.

    The proposals deliberately do not become glossary revisions: only the
    separate human approval path may create binding glossary constraints.
    """

    if (
        revision.analysis_state != "model"
        or revision.profile_id != _ANALYSIS_REDUCE_PROFILE_ID
        or revision.profile_version != "1"
    ):
        return None
    evidence = revision.evidence_json
    if not isinstance(evidence, Mapping):
        raise BlockContractError("baseline-analysis-terminology")
    proposals = evidence.get("glossary_proposals")
    if not isinstance(proposals, list):
        raise BlockContractError("baseline-analysis-terminology")
    terms: list[dict[str, object]] = []
    required = {
        "source_term",
        "target_term",
        "notes",
        "scope",
        "source_block_ids",
        "confidence",
        "status",
    }
    for raw in proposals:
        if not isinstance(raw, Mapping) or set(raw) != required:
            raise BlockContractError("baseline-analysis-terminology")
        source = raw["source_term"]
        target = raw["target_term"]
        notes = raw["notes"]
        links = raw["source_block_ids"]
        confidence = raw["confidence"]
        if (
            not isinstance(source, str)
            or not source.strip()
            or len(source.encode("utf-8")) > 4_096
            or (
                target is not None
                and (
                    not isinstance(target, str)
                    or not target.strip()
                    or len(target.encode("utf-8")) > 4_096
                )
            )
            or not isinstance(notes, str)
            or len(notes.encode("utf-8")) > 16_384
            or raw["scope"] != "document"
            or raw["status"] != "proposed"
            or not isinstance(links, list)
            or len(links) > 128
            or any(
                not isinstance(link, str)
                or not link.strip()
                or len(link.encode("utf-8")) > 16_384
                for link in links
            )
            or not isinstance(confidence, (int, float))
            or isinstance(confidence, bool)
            or not 0 <= confidence <= 1
        ):
            raise BlockContractError("baseline-analysis-terminology")
        terms.append(dict(raw))
    if not terms:
        return None
    content = canonical_json(
        {
            "contract_version": _ANALYSIS_TERMINOLOGY_CONTRACT_VERSION,
            "terms": terms,
        }
    )
    return ContextSlot(
        "terminology_guidance",
        content,
        {
            "resolver": "phase5-analysis-terminology-v1",
            "baseline_id": baseline.id,
            "baseline_hash": baseline.record_hash,
            "artifact_revision_id": revision.id,
            "artifact_revision_hash": revision.record_hash,
            "evidence_hash": revision.evidence_hash,
            "reduction_mode": evidence.get("reduction_mode"),
        },
        f"analysis-terminology:{revision.id}",
        str(revision.revision_number),
        sha256_text(content),
        {},
    )


def _plan(
    record: TranslationSourcePlanRecord | None,
    *,
    verified_plan: CanonicalSourcePlan | None = None,
) -> tuple[TranslationSourcePlanRecord, CanonicalSourcePlan]:
    state = inspect(record) if isinstance(record, TranslationSourcePlanRecord) else None
    if state is None or not state.persistent:
        raise BlockContractError("baseline-source-plan")
    plan = cast(TranslationSourcePlanRecord, record)
    if verified_plan is None:
        canonical = rehydrate_source_plan(plan)
    elif (
        verified_plan.project_id != plan.project_id
        or verified_plan.plan_hash != plan.plan_hash
        or verified_plan.snapshot.document_id != plan.source_document_id
        or verified_plan.snapshot.content_hash != plan.source_content_hash
    ):
        raise BlockContractError("baseline-source-plan")
    else:
        canonical = verified_plan
    return plan, canonical


def _baseline_hash(row: Any) -> str:
    return sha256_text(
        canonical_json(
            {
                "id": row.id,
                "operation_id": row.operation_id,
                "project_id": row.project_id,
                "owner_user_id": row.owner_user_id,
                "source_plan_id": row.source_plan_id,
                "source_plan_hash": row.source_plan_hash,
                "manifest_hash": row.manifest_hash,
                "created_at": row.created_at.isoformat(),
            }
        )
    )


def _glossary_binding_identity(
    baseline: TranslationContextBaseline, entries: list[dict[str, object]], *,
    artifact_id: str, artifact_version: str, artifact_hash: str, content: str,
) -> str:
    """Canonical signed input, including private slot material but no prompt trust."""

    locked = [
        {"revision_id": entry["glossary_revision_id"], "record_hash": entry["revision_hash"]}
        for entry in entries
        if entry["kind"] == "glossary_locked"
    ]
    return canonical_json(
        {
            "baseline_id": baseline.id,
            "baseline_record_hash": baseline.record_hash,
            "manifest_hash": baseline.manifest_hash,
            "glossary_revisions": locked,
            "artifact_id": artifact_id,
            "artifact_version": artifact_version,
            "artifact_hash": artifact_hash,
            "content": content,
        }
    )


def _research_reference(
    run: TranslationResearchRun,
    *,
    operation: TranslationOperation,
    source_plan: TranslationSourcePlanRecord,
    verified_plan: CanonicalSourcePlan | None = None,
    verification_context: ResearchVerificationContext | None = None,
) -> tuple[dict[str, object], dict[str, object]]:
    if isinstance(run, TranslationResearchRun) and run.operation_id != operation.id:
        from .evidence_reuse import verify_reused_research_reference

        verify_reused_research_reference(
            operation=operation,
            source_plan=source_plan,
            run=run,
            verified_plan=verified_plan,
            verification_context=verification_context,
        )
    if (
        not isinstance(run, TranslationResearchRun)
        or run.project_id != source_plan.project_id
        or run.source_plan_id != source_plan.id
        or run.owner_user_id != source_plan.project.user_id
    ):
        raise BlockContractError("baseline-research-scope")
    conclusion = rehydrate_completed_research_conclusion(
        run,
        verified_plan=verified_plan,
        verification_context=verification_context,
    )
    reference = {
        "kind": "research_evidence",
        "research_run_id": conclusion["run_id"],
        "run_record_hash": conclusion["run_record_hash"],
        "run_state": conclusion["run_state"],
        "state_head_receipt": conclusion["state_head_receipt"],
        "final_turn_id": conclusion["final_turn_id"],
        "final_turn_record_hash": conclusion["final_turn_record_hash"],
        "final_attempt_id": conclusion["final_attempt_id"],
        "final_attempt_record_hash": conclusion["final_attempt_record_hash"],
        "final_attempt_request_hash": conclusion["final_attempt_request_hash"],
        "conclusion_hash": conclusion["conclusion_hash"],
    }
    return reference, conclusion


def _dossier_scope(
    dossier: TranslationResearchDossier,
    plan: CanonicalSourcePlan,
) -> dict[str, object]:
    """Validate and normalize the owner-persisted dossier scope.

    Dossier scope is deliberately read from the stable dossier row.  A
    revision carries the dossier identity and immutable content, while the
    parent carries the scope shared by its append-only revision lineage.
    """

    if not isinstance(dossier, TranslationResearchDossier):
        raise BlockContractError("baseline-dossier-scope")
    kind = dossier.scope_kind
    raw_ids = dossier.scope_block_ids
    if (
        kind not in {"document", "blocks"}
        or not isinstance(raw_ids, list)
        or len(raw_ids) > 512
    ):
        raise BlockContractError("baseline-dossier-scope")
    canonical_ids = [str(block.id) for block in plan.blocks]
    known_ids = set(canonical_ids)
    supplied: list[str] = []
    for block_id in raw_ids:
        if (
            not isinstance(block_id, str)
            or not block_id
            or block_id not in known_ids
            or block_id in supplied
        ):
            raise BlockContractError("baseline-dossier-scope")
        supplied.append(block_id)
    if kind == "document" and supplied:
        raise BlockContractError("baseline-dossier-scope")
    if kind == "blocks" and not supplied:
        raise BlockContractError("baseline-dossier-scope")
    selected = set(supplied)
    return {
        "kind": kind,
        "block_ids": [block_id for block_id in canonical_ids if block_id in selected],
    }


def _dossier_hash(
    *,
    dossier_id: str,
    revision_number: int,
    content_digest: str,
    scope: Mapping[str, object],
) -> str:
    return sha256_text(
        canonical_json(
            {
                "dossier_id": dossier_id,
                "revision_number": revision_number,
                "content_digest": content_digest,
                "scope": dict(scope),
            }
        )
    )


def _verify_dossier_receipt(
    revision: TranslationResearchDossierRevision,
    *,
    scope: Mapping[str, object],
) -> None:
    verify_receipt(
        stored=revision.integrity_receipt,
        purpose="translator-research-dossier-revision-receipt-v1",
        values={
            "receipt_version": 1,
            "id": revision.id,
            "dossier_id": revision.dossier_id,
            "project_id": revision.project_id,
            "source_plan_id": revision.source_plan_id,
            "revision_number": revision.revision_number,
            "parent_revision_id": revision.parent_revision_id,
            "client_submission_id": revision.client_submission_id,
            "content_digest": revision.content_digest,
            "conclusion_provenance": revision.conclusion_provenance,
            "evidence_verification_state": revision.evidence_verification_state,
            "payload_json": revision.payload_json,
            "provenance_json": revision.provenance_json,
            "created_at": revision.created_at.isoformat(),
            "scope": dict(scope),
            "record_hash": revision.record_hash,
        },
        code="baseline-dossier-receipt",
    )


def _dossier_reference(
    revision: TranslationResearchDossierRevision,
    *,
    operation: TranslationOperation,
    source_plan: TranslationSourcePlanRecord,
    verified_plan: CanonicalSourcePlan,
) -> tuple[dict[str, object], dict[str, object]]:
    try:
        return _dossier_reference_unchecked(
            revision,
            operation=operation,
            source_plan=source_plan,
            verified_plan=verified_plan,
        )
    except BlockContractError:
        raise
    except Exception as exc:
        raise BlockContractError("baseline-dossier-corrupt") from exc


def _dossier_reference_unchecked(
    revision: TranslationResearchDossierRevision,
    *,
    operation: TranslationOperation,
    source_plan: TranslationSourcePlanRecord,
    verified_plan: CanonicalSourcePlan,
) -> tuple[dict[str, object], dict[str, object]]:
    """Fully verify one external dossier revision and return its safe DTO."""

    if not isinstance(revision, TranslationResearchDossierRevision):
        raise BlockContractError("baseline-dossier-revision")
    revision_state = inspect(revision)
    if (
        revision_state is None
        or not revision_state.persistent
        or revision_state.deleted
        or revision_state.modified
        or revision_state.session is not db.session()
    ):
        raise BlockContractError("baseline-dossier-persistence")
    with db.session.no_autoflush:
        dossier = db.session.get(TranslationResearchDossier, revision.dossier_id)
    dossier_state = inspect(dossier) if dossier is not None else None
    if (
        dossier is None
        or dossier_state is None
        or not dossier_state.persistent
        or dossier_state.deleted
        or dossier_state.modified
        or dossier_state.session is not db.session()
        or revision.dossier_id != dossier.id
        or revision.project_id != dossier.project_id
        or revision.source_plan_id != dossier.source_plan_id
        or dossier.project_id != source_plan.project_id
        or dossier.source_plan_id != source_plan.id
        or dossier.owner_user_id != source_plan.project.user_id
        or revision.project_id != operation.project_id
        or revision.source_plan_id != operation.source_plan_id
        or source_plan.project.user_id != operation.owner_user_id
    ):
        raise BlockContractError("baseline-dossier-scope")
    scope = _dossier_scope(dossier, verified_plan)
    if (
        isinstance(revision.revision_number, bool)
        or not isinstance(revision.revision_number, int)
        or revision.revision_number < 1
        or revision.conclusion_provenance != "external_unverified"
        or revision.evidence_verification_state != "unverified"
        or not isinstance(revision.payload_json, Mapping)
        or not isinstance(revision.provenance_json, Mapping)
    ):
        raise BlockContractError("baseline-dossier-revision")
    try:
        content_digest = sha256_text(canonical_json(dict(revision.payload_json)))
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise BlockContractError("baseline-dossier-payload") from exc
    if revision.content_digest != content_digest:
        raise BlockContractError("baseline-dossier-content")
    if revision.record_hash != _dossier_hash(
        dossier_id=dossier.id,
        revision_number=revision.revision_number,
        content_digest=content_digest,
        scope=scope,
    ):
        raise BlockContractError("baseline-dossier-record")
    _verify_dossier_receipt(revision, scope=scope)

    # Verify the complete parent chain sufficiently to reject a cross-dossier
    # or malformed lineage, while bounding work on hostile/corrupt graphs.
    current = revision
    visited: set[str] = set()
    traversed = 0
    while True:
        traversed += 1
        if traversed > 64 or current.id in visited:
            raise BlockContractError("baseline-dossier-lineage")
        visited.add(current.id)
        parent_id = current.parent_revision_id
        if current.revision_number == 1:
            if parent_id is not None:
                raise BlockContractError("baseline-dossier-lineage")
            break
        if not isinstance(parent_id, str) or not parent_id:
            raise BlockContractError("baseline-dossier-lineage")
        with db.session.no_autoflush:
            parent = db.session.get(TranslationResearchDossierRevision, parent_id)
        parent_state = inspect(parent) if parent is not None else None
        if (
            parent is None
            or parent_state is None
            or not parent_state.persistent
            or parent_state.deleted
            or parent_state.modified
            or parent_state.session is not db.session()
            or parent.dossier_id != dossier.id
            or parent.project_id != dossier.project_id
            or parent.source_plan_id != dossier.source_plan_id
            or isinstance(parent.revision_number, bool)
            or not isinstance(parent.revision_number, int)
            or parent.revision_number != current.revision_number - 1
        ):
            raise BlockContractError("baseline-dossier-lineage")
        if (
            not isinstance(parent.payload_json, Mapping)
            or not isinstance(parent.provenance_json, Mapping)
            or parent.conclusion_provenance != "external_unverified"
            or parent.evidence_verification_state != "unverified"
        ):
            raise BlockContractError("baseline-dossier-lineage")
        try:
            parent_digest = sha256_text(canonical_json(dict(parent.payload_json)))
        except (TypeError, ValueError, OverflowError, RecursionError) as exc:
            raise BlockContractError("baseline-dossier-lineage") from exc
        if (
            parent.content_digest != parent_digest
            or parent.record_hash
            != _dossier_hash(
                dossier_id=dossier.id,
                revision_number=parent.revision_number,
                content_digest=parent_digest,
                scope=scope,
            )
        ):
            raise BlockContractError("baseline-dossier-lineage")
        _verify_dossier_receipt(parent, scope=scope)
        current = parent

    reference = {
        "kind": "research_dossier",
        "dossier_id": dossier.id,
        "dossier_revision_id": revision.id,
        "dossier_revision_number": revision.revision_number,
        "dossier_record_hash": revision.record_hash,
        "content_digest": content_digest,
        "conclusion_provenance": "external_unverified",
        "evidence_verification_state": "unverified",
        "scope": scope,
    }
    return reference, {"scope": scope, "payload": dict(revision.payload_json)}


def _memory_reference(
    generation: TranslationEmbeddingGeneration,
    pointer: dict[str, object],
) -> dict[str, object]:
    if generation.projection_kind != "memory":
        raise BlockContractError("baseline-memory-generation")
    if (
        pointer.get("generation_id") != generation.id
        or pointer.get("generation_record_hash") != generation.record_hash
        or pointer.get("projection_kind") != "memory"
        or pointer.get("generation_state_receipt") is None
    ):
        raise BlockContractError("baseline-memory-reference")
    return {
        "kind": "translation_memory",
        "generation_id": generation.id,
        "generation_record_hash": generation.record_hash,
        # This is the state receipt witnessed by the historical active
        # pointer.  It must remain stable when the generation later becomes
        # retired and receives a new lifecycle receipt.
        "generation_state": "active",
        "generation_state_receipt": pointer["generation_state_receipt"],
        "signature_hash": generation.signature_hash,
        "model_record_id": generation.model_record_id,
        "model_id": generation.model_id,
        "provider_id": generation.provider_id,
        "pointer": dict(pointer),
    }


def _reference_hash(reference: Mapping[str, object]) -> str:
    return sha256_text(canonical_json(dict(reference)))


def _durable_operation(operation: TranslationOperation) -> TranslationOperation:
    """Reload a clean operation without allowing caller state to autoflush."""

    # Attribute history may need to load an expired value.  Keep that load,
    # and the durable re-query below, inside the same no-autoflush boundary so
    # a dirty caller object can never be flushed merely by being inspected.
    with db.session.no_autoflush:
        if not isinstance(operation, TranslationOperation) or not operation.id:
            raise BlockContractError("baseline-operation-owner")
        state = inspect(operation)
        if state is None or not state.persistent or state.deleted:
            raise BlockContractError("baseline-operation-owner")
        if state.session is not db.session():
            raise BlockContractError("baseline-operation-owner")
        dirty_pins = any(
            state.attrs[name].history.has_changes() for name in _OPERATION_PIN_COLUMNS
        )
        if dirty_pins:
            raise BlockContractError("baseline-operation-source")
        if state.modified:
            raise BlockContractError("baseline-operation-owner")
        durable = db.session.get(
            TranslationOperation, operation.id, populate_existing=True
        )
    if durable is None:
        raise BlockContractError("baseline-operation-owner")
    return durable


def freeze_context_baseline(
    operation: TranslationOperation,
    source_plan: TranslationSourcePlanRecord,
    *,
    artifact_revisions: Iterable[TranslationContextArtifactRevision] = (),
    glossary_revisions: Iterable[TranslationGlossaryRevision] = (),
    completed_research_runs: Iterable[TranslationResearchRun] = (),
    active_memory_generation: TranslationEmbeddingGeneration | None = None,
    research_dossier_revision: TranslationResearchDossierRevision | None = None,
    verified_plan: CanonicalSourcePlan | None = None,
    verification_context: ResearchVerificationContext | None = None,
) -> TranslationContextBaseline:
    selected_research_runs = tuple(completed_research_runs)
    if len(selected_research_runs) > 1 or (
        selected_research_runs and research_dossier_revision is not None
    ):
        raise BlockContractError("baseline-research-selection")
    operation = _durable_operation(operation)
    record, plan = _plan(source_plan, verified_plan=verified_plan)
    if (
        operation.project_id != record.project_id
        or operation.owner_user_id != record.project.user_id
    ):
        raise BlockContractError("baseline-operation-owner")
    if (
        not isinstance(operation.source_content_hash, str)
        or operation.source_content_hash != plan.snapshot.content_hash
        or operation.source_plan_id != record.id
        or operation.source_plan_hash != plan.plan_hash
    ):
        raise BlockContractError("baseline-operation-source")
    artifacts = tuple(artifact_revisions)
    glossaries = tuple(glossary_revisions)
    entries: list[dict[str, object]] = []
    locked_bindings: dict[tuple[str, str, str], str] = {}
    seen_artifacts: set[str] = set()
    seen_glossaries: set[str] = set()
    for revision in artifacts:
        if not isinstance(revision, TranslationContextArtifactRevision) or revision.id in seen_artifacts:
            raise BlockContractError("baseline-artifact-selection")
        seen_artifacts.add(revision.id)
        payload = rehydrate_context_artifact_revision(revision, record)
        entries.append(
            {
                "kind": payload.kind,
                "artifact_revision_id": revision.id,
                "glossary_revision_id": None,
                "revision_hash": revision.record_hash,
            }
        )
    for revision in glossaries:
        if not isinstance(revision, TranslationGlossaryRevision) or revision.id in seen_glossaries:
            raise BlockContractError("baseline-glossary-selection")
        seen_glossaries.add(revision.id)
        glossary = rehydrate_glossary_revision(revision, record)
        if revision.revision_state != "locked" or revision.author_type != "human":
            raise BlockContractError("baseline-glossary-authority")
        key = (" ".join(glossary.source_term.casefold().split()), glossary.scope,
               " ".join(str((glossary.usage or {}).get("sense", "")).casefold().split()))
        existing_target = locked_bindings.get(key)
        if existing_target is not None and existing_target != glossary.target_term:
            raise BlockContractError("baseline-glossary-conflict")
        locked_bindings[key] = glossary.target_term
        entries.append(
            {
                "kind": "glossary_locked",
                "artifact_revision_id": None,
                "glossary_revision_id": revision.id,
                "revision_hash": revision.record_hash,
            }
        )
    seen_research: set[str] = set()
    for run in selected_research_runs:
        if not isinstance(run, TranslationResearchRun) or run.id in seen_research:
            raise BlockContractError("baseline-research-selection")
        seen_research.add(run.id)
        reference, _conclusion = _research_reference(
            run,
            operation=operation,
            source_plan=record,
            verified_plan=plan,
            verification_context=verification_context,
        )
        entries.append(
            {
                "kind": "research_evidence",
                "artifact_revision_id": None,
                "glossary_revision_id": None,
                "research_run_id": run.id,
                "embedding_generation_id": None,
                "revision_hash": reference["run_record_hash"],
                "reference_hash": _reference_hash(reference),
                "reference_json": reference,
            }
        )
    if research_dossier_revision is not None:
        reference, _dossier = _dossier_reference(
            research_dossier_revision,
            operation=operation,
            source_plan=record,
            verified_plan=plan,
        )
        entries.append(
            {
                "kind": "research_dossier",
                "artifact_revision_id": None,
                "glossary_revision_id": None,
                "research_run_id": None,
                "embedding_generation_id": None,
                "research_dossier_revision_id": research_dossier_revision.id,
                "revision_hash": research_dossier_revision.record_hash,
                "reference_hash": _reference_hash(reference),
                "reference_json": reference,
            }
        )
    if active_memory_generation is not None:
        if not isinstance(active_memory_generation, TranslationEmbeddingGeneration):
            raise BlockContractError("baseline-memory-selection")
        if (
            active_memory_generation.project_id != record.project_id
            or active_memory_generation.source_plan_id != record.id
            or active_memory_generation.owner_user_id != record.project.user_id
        ):
            raise BlockContractError("baseline-memory-scope")
        generation, pointer = verified_active_embedding_reference(
            owner_user_id=record.project.user_id,
            project_id=record.project_id,
            source_plan_id=record.id,
            projection_kind="memory",
        )
        if generation.id != active_memory_generation.id:
            raise BlockContractError("baseline-memory-selection")
        reference = _memory_reference(generation, pointer)
        entries.append(
            {
                "kind": "translation_memory",
                "artifact_revision_id": None,
                "glossary_revision_id": None,
                "research_run_id": None,
                "embedding_generation_id": generation.id,
                "revision_hash": generation.record_hash,
                "reference_hash": _reference_hash(reference),
                "reference_json": reference,
            }
        )
    # A baseline may pin one completed research conclusion and one memory
    # generation.  Research retries remain separate durable runs; callers must
    # explicitly select the one admissible run rather than letting ordering or
    # "latest" inference change the baseline.
    if sum(entry["kind"] == "translation_memory" for entry in entries) > 1:
        raise BlockContractError("baseline-memory-selection")
    entries.sort(
        key=lambda entry: (
            str(entry["kind"]),
            str(
                entry["artifact_revision_id"]
                or entry["glossary_revision_id"]
                or entry.get("research_dossier_revision_id")
            ),
        )
    )
    manifest = {
        "contract_version": "translation-context-baseline-v1",
        "source_plan_id": record.id,
        "source_plan_hash": plan.plan_hash,
        "items": entries,
    }
    manifest_hash = sha256_text(canonical_json(manifest))
    existing = TranslationContextBaseline.query.filter_by(
        operation_id=operation.id
    ).one_or_none()
    if existing is not None:
        rehydrate_context_baseline(existing, record, verified_plan=plan)
        if (
            existing.manifest_hash != manifest_hash
            or existing.manifest_json != manifest
        ):
            raise BlockContractError("baseline-selection-conflict")
        return existing
    _ensure_database_transaction()
    try:
        with db.session.begin_nested():
            now = utc_now_db()
            row = cast(Any, TranslationContextBaseline)(
                id=str(uuid.uuid4()),
                operation_id=operation.id,
                project_id=record.project_id,
                owner_user_id=record.project.user_id,
                source_plan_id=record.id,
                source_plan_hash=plan.plan_hash,
                manifest_hash=manifest_hash,
                record_hash="0" * 64,
                binding_attestation="pending",
                manifest_json=manifest,
                created_at=now,
            )
            row.record_hash = _baseline_hash(row)
            db.session.add(row)
            db.session.flush()
            locked_values = []
            for entry in entries:
                if entry["kind"] == "glossary_locked":
                    revision = db.session.get(
                        TranslationGlossaryRevision, entry["glossary_revision_id"]
                    )
                    if revision is None:
                        raise BlockContractError("baseline-glossary")
                    payload = rehydrate_glossary_revision(revision, record)
                    locked_values.append(
                        glossary_prompt_mapping(payload)
                    )
            content = canonical_json({"locked_terms": locked_values})
            row.binding_attestation = keyed_lookup_digest(
                _glossary_binding_identity(
                    row,
                    entries,
                    artifact_id=f"phase5-baseline:{row.id}",
                    artifact_version="1",
                    artifact_hash=sha256_text(content),
                    content=content,
                ),
                purpose=_GLOSSARY_BINDING_PURPOSE,
            )
            db.session.flush()
            for index, entry in enumerate(entries, 1):
                db.session.add(
                    cast(Any, TranslationContextBaselineItem)(
                        id=str(uuid.uuid4()),
                        baseline_id=row.id,
                        operation_id=operation.id,
                        project_id=record.project_id,
                        source_plan_id=record.id,
                        order_index=index,
                        item_kind=entry["kind"],
                        artifact_revision_id=entry["artifact_revision_id"],
                        glossary_revision_id=entry["glossary_revision_id"],
                        research_run_id=entry.get("research_run_id"),
                        embedding_generation_id=entry.get("embedding_generation_id"),
                        research_dossier_revision_id=entry.get(
                            "research_dossier_revision_id"
                        ),
                        reference_json=entry.get("reference_json"),
                        revision_hash=entry["revision_hash"],
                        slot_state="enabled",
                    )
                )
            db.session.flush()
    except (IntegrityError, OperationalError) as exc:
        if isinstance(exc, OperationalError) and not is_database_locked(exc):
            raise
        winner = TranslationContextBaseline.query.filter_by(
            operation_id=operation.id
        ).one_or_none()
        if winner is None:
            if isinstance(exc, OperationalError):
                raise BlockContractError("baseline-concurrency") from exc
            raise
        rehydrate_context_baseline(
            winner,
            record,
            verified_plan=plan,
            verification_context=verification_context,
        )
        if winner.manifest_hash != manifest_hash or winner.manifest_json != manifest:
            raise BlockContractError("baseline-selection-conflict") from exc
        return winner
    return row


def rehydrate_context_baseline(
    baseline: TranslationContextBaseline,
    source_plan: TranslationSourcePlanRecord | None = None,
    *,
    verified_plan: CanonicalSourcePlan | None = None,
    verification_context: ResearchVerificationContext | None = None,
) -> TranslationContextBaseline:
    try:
        record, plan = _plan(
            source_plan
            or db.session.get(TranslationSourcePlanRecord, baseline.source_plan_id),
            verified_plan=verified_plan,
        )
        operation = db.session.get(TranslationOperation, baseline.operation_id)
        if (
            operation is None
            or baseline.project_id != record.project_id
            or baseline.owner_user_id != record.project.user_id
            or operation.project_id != record.project_id
            or operation.owner_user_id != record.project.user_id
            or not isinstance(operation.source_content_hash, str)
            or operation.source_content_hash != plan.snapshot.content_hash
            or operation.source_plan_id != record.id
            or operation.source_plan_hash != plan.plan_hash
            or baseline.source_plan_hash != plan.plan_hash
            or baseline.record_hash != _baseline_hash(baseline)
        ):
            raise BlockContractError("baseline-record")
        items = sorted(cast(Any, baseline.items), key=lambda item: item.order_index)
        entries = []
        for index, item in enumerate(items, 1):
            if item.order_index != index or item.slot_state != "enabled":
                raise BlockContractError("baseline-item")
            if item.item_kind in _ARTIFACT_SLOT:
                if (
                    item.artifact_revision_id is None
                    or item.glossary_revision_id is not None
                    or item.research_run_id is not None
                    or item.embedding_generation_id is not None
                    or item.research_dossier_revision_id is not None
                    or item.reference_json is not None
                ):
                    raise BlockContractError("baseline-item-kind")
                revision = db.session.get(
                    TranslationContextArtifactRevision, item.artifact_revision_id
                )
                if (
                    revision is None
                    or revision.record_hash != item.revision_hash
                    or rehydrate_context_artifact_revision(revision, record).kind
                    != item.item_kind
                ):
                    raise BlockContractError("baseline-artifact")
            elif item.item_kind == "glossary_locked":
                if (
                    item.glossary_revision_id is None
                    or item.artifact_revision_id is not None
                    or item.research_run_id is not None
                    or item.embedding_generation_id is not None
                    or item.research_dossier_revision_id is not None
                    or item.reference_json is not None
                ):
                    raise BlockContractError("baseline-item-kind")
                revision = db.session.get(
                    TranslationGlossaryRevision, item.glossary_revision_id
                )
                if (
                    revision is None
                    or revision.record_hash != item.revision_hash
                    or revision.revision_state != "locked"
                    or revision.author_type != "human"
                ):
                    raise BlockContractError("baseline-glossary")
                rehydrate_glossary_revision(revision, record)
            elif item.item_kind == "research_evidence":
                if (
                    item.artifact_revision_id is not None
                    or item.glossary_revision_id is not None
                    or item.research_run_id is None
                    or item.embedding_generation_id is not None
                    or item.research_dossier_revision_id is not None
                    or not isinstance(item.reference_json, Mapping)
                ):
                    raise BlockContractError("baseline-item-kind")
                run = db.session.get(TranslationResearchRun, item.research_run_id)
                if run is None or run.project_id != record.project_id or run.source_plan_id != record.id:
                    raise BlockContractError("baseline-research-scope")
                reference, _conclusion = _research_reference(
                    run,
                    operation=operation,
                    source_plan=record,
                    verified_plan=plan,
                    verification_context=verification_context,
                )
                if (
                    item.revision_hash != reference["run_record_hash"]
                    or dict(item.reference_json) != reference
                    or _reference_hash(reference)
                    != _reference_hash(dict(item.reference_json))
                ):
                    raise BlockContractError("baseline-research-reference")
            elif item.item_kind == "research_dossier":
                if (
                    item.baseline_id != baseline.id
                    or item.operation_id != operation.id
                    or item.project_id != record.project_id
                    or item.source_plan_id != record.id
                    or item.artifact_revision_id is not None
                    or item.glossary_revision_id is not None
                    or item.research_run_id is not None
                    or item.embedding_generation_id is not None
                    or item.research_dossier_revision_id is None
                    or not isinstance(item.reference_json, Mapping)
                ):
                    raise BlockContractError("baseline-item-kind")
                revision = db.session.get(
                    TranslationResearchDossierRevision,
                    item.research_dossier_revision_id,
                )
                if revision is None:
                    raise BlockContractError("baseline-dossier-revision")
                reference, _dossier = _dossier_reference(
                    revision,
                    operation=operation,
                    source_plan=record,
                    verified_plan=plan,
                )
                if (
                    item.revision_hash != revision.record_hash
                    or dict(item.reference_json) != reference
                    or _reference_hash(dict(item.reference_json))
                    != _reference_hash(reference)
                ):
                    raise BlockContractError("baseline-dossier-reference")
            elif item.item_kind == "translation_memory":
                if (
                    item.artifact_revision_id is not None
                    or item.glossary_revision_id is not None
                    or item.research_run_id is not None
                    or item.research_dossier_revision_id is not None
                    or item.embedding_generation_id is None
                    or not isinstance(item.reference_json, Mapping)
                ):
                    raise BlockContractError("baseline-item-kind")
                reference = dict(item.reference_json)
                pointer = reference.get("pointer")
                if not isinstance(pointer, Mapping):
                    raise BlockContractError("baseline-memory-reference")
                generation, _pointer = verify_pinned_embedding_reference(pointer)
                expected_reference = _memory_reference(generation, dict(pointer))
                if (
                    generation.id != item.embedding_generation_id
                    or item.revision_hash != generation.record_hash
                    or reference != expected_reference
                    or _reference_hash(reference) != _reference_hash(expected_reference)
                ):
                    raise BlockContractError("baseline-memory-reference")
            else:
                raise BlockContractError("baseline-item-kind")
            entries.append(
                {
                    "kind": item.item_kind,
                    "artifact_revision_id": item.artifact_revision_id,
                    "glossary_revision_id": item.glossary_revision_id,
                    **(
                        {
                            "research_run_id": item.research_run_id,
                            "embedding_generation_id": item.embedding_generation_id,
                            "reference_hash": _reference_hash(
                                dict(
                                    cast(
                                        Mapping[str, object], item.reference_json
                                    )
                                )
                            ),
                            "reference_json": dict(
                                cast(Mapping[str, object], item.reference_json)
                            ),
                        }
                        if item.item_kind in {"research_evidence", "translation_memory"}
                        else {}
                    ),
                    **(
                        {
                            "research_run_id": None,
                            "embedding_generation_id": None,
                            "research_dossier_revision_id": item.research_dossier_revision_id,
                            "reference_hash": _reference_hash(
                                dict(cast(Mapping[str, object], item.reference_json))
                            ),
                            "reference_json": dict(
                                cast(Mapping[str, object], item.reference_json)
                            ),
                        }
                        if item.item_kind == "research_dossier"
                        else {}
                    ),
                    "revision_hash": item.revision_hash,
                }
            )
        expected = {
            "contract_version": "translation-context-baseline-v1",
            "source_plan_id": record.id,
            "source_plan_hash": plan.plan_hash,
            "items": entries,
        }
        if baseline.manifest_json != expected or baseline.manifest_hash != sha256_text(
            canonical_json(expected)
        ):
            raise BlockContractError("baseline-manifest")
        locked = []
        for entry in entries:
            if entry["kind"] == "glossary_locked":
                revision = db.session.get(TranslationGlossaryRevision, entry["glossary_revision_id"])
                if revision is None:
                    raise BlockContractError("baseline-glossary")
                payload = rehydrate_glossary_revision(revision, record)
                locked.append(glossary_prompt_mapping(payload))
        content = canonical_json({"locked_terms": locked})
        identity = _glossary_binding_identity(
            baseline, entries, artifact_id=f"phase5-baseline:{baseline.id}", artifact_version="1",
            artifact_hash=sha256_text(content), content=content,
        )
        if not any(hmac.compare_digest(baseline.binding_attestation, candidate) for candidate in keyed_lookup_candidates(identity, purpose=_GLOSSARY_BINDING_PURPOSE)):
            raise BlockContractError("baseline-attestation")
        return baseline
    except BlockContractError:
        raise
    except Exception as exc:
        raise BlockContractError("baseline-corrupt") from exc


def context_slots_from_baseline(
    baseline: TranslationContextBaseline,
    source_plan: TranslationSourcePlanRecord | None = None,
    *,
    verified_plan: CanonicalSourcePlan | None = None,
    verification_context: ResearchVerificationContext | None = None,
) -> tuple[ContextSlot, ...]:
    """Adapt only verified enabled selections; values stay untrusted reference data."""
    record, plan = _plan(
        source_plan
        or db.session.get(TranslationSourcePlanRecord, baseline.source_plan_id),
        verified_plan=verified_plan,
    )
    baseline = rehydrate_context_baseline(
        baseline,
        record,
        verified_plan=plan,
        verification_context=verification_context,
    )
    operation = db.session.get(TranslationOperation, baseline.operation_id)
    if operation is None:
        raise BlockContractError("baseline-operation-owner")
    slots: list[ContextSlot] = []
    locked: list[dict[str, str]] = []
    locked_ids: list[str] = []
    for item in cast(Any, baseline.items):
        if item.item_kind in _ARTIFACT_SLOT:
            revision = db.session.get(
                TranslationContextArtifactRevision, item.artifact_revision_id
            )
            if revision is None:
                raise BlockContractError("baseline-artifact")
            payload = rehydrate_context_artifact_revision(revision, record)
            content = canonical_json(payload.to_dict())
            slots.append(
                ContextSlot(
                    _ARTIFACT_SLOT[item.item_kind],
                    content,
                    {
                        "resolver": "phase5-baseline-v1",
                        "baseline_id": baseline.id,
                        "baseline_hash": baseline.record_hash,
                        "artifact_revision_id": revision.id,
                        "kind": payload.kind,
                    },
                    revision.id,
                    str(revision.revision_number),
                    sha256_text(content),
                    {},
                )
            )
            if item.item_kind == "document_brief":
                terminology = _analysis_terminology_slot(revision, baseline)
                if terminology is not None:
                    slots.append(terminology)
        elif item.item_kind == "glossary_locked":
            revision = db.session.get(
                TranslationGlossaryRevision, item.glossary_revision_id
            )
            if revision is None:
                raise BlockContractError("baseline-glossary")
            payload = rehydrate_glossary_revision(revision, record)
            locked.append(
                glossary_prompt_mapping(payload)
            )
            locked_ids.append(revision.id)
        elif item.item_kind == "research_evidence":
            run = db.session.get(TranslationResearchRun, item.research_run_id)
            if run is None:
                raise BlockContractError("baseline-research-scope")
            reference, conclusion = _research_reference(
                run,
                operation=operation,
                source_plan=record,
                verified_plan=plan,
                verification_context=verification_context,
            )
            if (
                not isinstance(item.reference_json, Mapping)
                or dict(item.reference_json) != reference
                or item.revision_hash != reference["run_record_hash"]
            ):
                raise BlockContractError("baseline-research-reference")
            content = canonical_json(conclusion["conclusion"])
            slots.append(
                ContextSlot(
                    "research_evidence",
                    content,
                    {
                        "resolver": "phase5-baseline-v1",
                        "baseline_id": baseline.id,
                        "baseline_hash": baseline.record_hash,
                        "reference_hash": _reference_hash(reference),
                        "research_run_id": reference["research_run_id"],
                    },
                    str(reference["research_run_id"]),
                    str(reference["final_turn_id"]),
                    sha256_text(content),
                    {
                        "run_id": reference["research_run_id"],
                        "run_record_hash": reference["run_record_hash"],
                        "state_head_receipt": reference["state_head_receipt"],
                        "turn_id": reference["final_turn_id"],
                        "turn_record_hash": reference["final_turn_record_hash"],
                        "attempt_id": reference["final_attempt_id"],
                        "attempt_record_hash": reference["final_attempt_record_hash"],
                        "attempt_request_hash": reference["final_attempt_request_hash"],
                        "conclusion_hash": reference["conclusion_hash"],
                    },
                )
            )
        elif item.item_kind == "research_dossier":
            revision = db.session.get(
                TranslationResearchDossierRevision,
                item.research_dossier_revision_id,
            )
            if revision is None or not isinstance(item.reference_json, Mapping):
                raise BlockContractError("baseline-dossier-reference")
            reference, dossier = _dossier_reference(
                revision,
                operation=operation,
                source_plan=record,
                verified_plan=plan,
            )
            if (
                item.revision_hash != revision.record_hash
                or dict(item.reference_json) != reference
                or _reference_hash(dict(item.reference_json))
                != _reference_hash(reference)
            ):
                raise BlockContractError("baseline-dossier-reference")
            content = canonical_json(
                {
                    "contract_version": "translation-external-research-dossier-v1",
                    "conclusion_provenance": "external_unverified",
                    "evidence_verification_state": "unverified",
                    "scope": dossier["scope"],
                    "payload": dossier["payload"],
                }
            )
            slots.append(
                ContextSlot(
                    "research_evidence",
                    content,
                    {
                        "resolver": "phase5-external-dossier-v1",
                        "baseline_id": baseline.id,
                        "baseline_hash": baseline.record_hash,
                        "reference_hash": _reference_hash(reference),
                        "dossier_id": reference["dossier_id"],
                        "dossier_revision_id": reference["dossier_revision_id"],
                        "dossier_record_hash": reference["dossier_record_hash"],
                        "conclusion_provenance": "external_unverified",
                        "evidence_verification_state": "unverified",
                    },
                    f"external-dossier:{revision.id}",
                    str(revision.revision_number),
                    sha256_text(content),
                    {},
                )
            )
        elif item.item_kind == "translation_memory":
            # The baseline pins a generation/pointer, but memory content is
            # selected per window by context_adapters.py.  Never emit an
            # empty placeholder slot here.
            continue
    if locked:
        content = canonical_json({"locked_terms": locked})
        entries = [
            {"kind": item.item_kind, "glossary_revision_id": item.glossary_revision_id, "revision_hash": item.revision_hash}
            for item in cast(Any, baseline.items)
        ]
        artifact_id = f"phase5-baseline:{baseline.id}"
        artifact_hash = sha256_text(content)
        identity = _glossary_binding_identity(
            baseline, entries, artifact_id=artifact_id, artifact_version="1",
            artifact_hash=artifact_hash, content=content,
        )
        if not any(hmac.compare_digest(baseline.binding_attestation, candidate) for candidate in keyed_lookup_candidates(identity, purpose=_GLOSSARY_BINDING_PURPOSE)):
            raise BlockContractError("baseline-attestation")
        slots.append(
            ContextSlot(
                "glossary_constraints", content,
                {"resolver": "phase5-baseline-v1", "baseline_id": baseline.id,
                 "baseline_hash": baseline.record_hash, "baseline_manifest_hash": baseline.manifest_hash,
                 "glossary_revision_ids": locked_ids,
                 "glossary_revision_hashes": [item["revision_hash"] for item in entries if item["kind"] == "glossary_locked"],
                 "binding_attestation": baseline.binding_attestation},
                artifact_id, "1", artifact_hash, {},
            )
        )
    return tuple(slots)


__all__ = [
    "context_slots_from_baseline",
    "freeze_context_baseline",
    "rehydrate_context_baseline",
]
