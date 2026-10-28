"""Append-only persistence for deterministic census and typed analysis artifacts."""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from typing import Any, cast

from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError, OperationalError

from carlina.core.utc import utc_now_db
from iarf_assistant.database_utils import is_database_locked
from iarf_assistant.extensions import db

from .analysis_census import (
    SemanticCensus,
    build_semantic_census,
    coverage_occurrence_id_candidates,
)
from .analysis_contract import (
    ARTIFACT_SCHEMA_VERSION,
    ContextArtifactPayload,
    private_witness,
    private_witness_candidates,
)
from .block_contract import BlockContractError, canonical_json, sha256_text
from .block_persistence import _ensure_database_transaction, rehydrate_source_plan
from .integrity_receipts import make_receipt, verify_receipt
from .models import (
    TranslationContextArtifact,
    TranslationContextArtifactRevision,
    TranslationSemanticCensus,
    TranslationSemanticOccurrence,
)
from .models import (
    TranslationSourcePlan as TranslationSourcePlanRecord,
)


def _record(plan: TranslationSourcePlanRecord | None) -> TranslationSourcePlanRecord:
    state = inspect(plan) if isinstance(plan, TranslationSourcePlanRecord) else None
    if state is None or not state.persistent:
        raise BlockContractError("analysis-source-plan")
    record = cast(TranslationSourcePlanRecord, plan)
    rehydrate_source_plan(record)
    return record


def _hash(row: Any) -> str:
    return sha256_text(
        canonical_json(
            {
                "id": row.id,
                "source_plan_id": row.source_plan_id,
                "source_plan_hash": row.source_plan_hash,
                "profile_id": row.profile_id,
                "profile_version": row.profile_version,
                "occurrence_count": row.occurrence_count,
                "census_hash": row.census_hash,
                "coverage_hash": row.coverage_hash,
                "created_at": row.created_at.isoformat(),
            }
        )
    )


def _occurrence_witness_material(
    item: Any, *, slot_id_override: str | None = None
) -> dict[str, object]:
    return {
        "block_id": item.block_id,
        "anchor_id": item.anchor_id,
        "kind": item.occurrence_kind,
        "slot_id": item.slot_id if slot_id_override is None else slot_id_override,
        "slot_kind": item.slot_kind,
        "disposition": item.disposition,
        "start_offset": item.start_offset,
        "end_offset": item.end_offset,
        "structural": item.structural,
        "value": item.value,
    }


def _occurrence_hash_candidates(
    item: Any, slot_id_candidates: Mapping[str | None, tuple[str, ...]]
) -> set[str]:
    """Accept retained-key witnesses without accepting a forged occurrence."""

    if item.occurrence_kind != "coverage":
        return set(
            private_witness_candidates(
                _occurrence_witness_material(item),
                purpose="translator-census-occurrence-v1",
            )
        )
    return {
        candidate
        for slot_id in slot_id_candidates.get(item.slot_id, ())
        for candidate in private_witness_candidates(
            _occurrence_witness_material(item, slot_id_override=slot_id),
            purpose="translator-census-occurrence-v1",
        )
    }


def _coverage_hash_candidates(
    expected_items: tuple[Any, ...],
    slot_id_candidates: Mapping[str | None, tuple[str, ...]],
) -> set[str]:
    """Build whole-census witnesses for each retained key generation.

    Coverage occurrence IDs are keyed, so all IDs in one historical census
    were generated with the same key.  Varying them as a correlated set avoids
    the exponential cartesian product while still accepting key rotation.
    """

    coverage_counts = [
        len(slot_id_candidates.get(item.slot_id, ()))
        for item in expected_items
        if item.occurrence_kind == "coverage"
    ]
    generations = max(coverage_counts, default=1)
    result: set[str] = set()
    for generation in range(generations):
        material: list[dict[str, object]] = []
        for item in expected_items:
            if item.occurrence_kind == "coverage":
                candidates = slot_id_candidates.get(item.slot_id, ())
                slot_id = candidates[min(generation, len(candidates) - 1)]
                material.append(
                    _occurrence_witness_material(item, slot_id_override=slot_id)
                )
            else:
                material.append(_occurrence_witness_material(item))
        result.update(
            private_witness_candidates(
                material, purpose="translator-census-coverage-v1"
            )
        )
    return result


def persist_semantic_census(
    source_plan: TranslationSourcePlanRecord, census: SemanticCensus | None = None
) -> TranslationSemanticCensus:
    record = _record(source_plan)
    plan = rehydrate_source_plan(record)
    expected = build_semantic_census(plan)
    if census is not None and census != expected:
        raise BlockContractError("semantic-census-witness")
    census = expected
    existing = TranslationSemanticCensus.query.filter_by(
        source_plan_id=record.id,
        profile_id=census.profile_id,
        profile_version=census.profile_version,
        census_hash=census.census_hash,
    ).one_or_none()
    if existing is not None:
        rehydrate_semantic_census(existing, record)
        return existing
    _ensure_database_transaction()
    anchors = {anchor.id for anchor in cast(Any, record.anchors)}
    blocks = {block.id for block in cast(Any, record.blocks)}
    try:
        with db.session.begin_nested():
            now = utc_now_db()
            row = cast(Any, TranslationSemanticCensus)(
                id=str(uuid.uuid4()),
                project_id=record.project_id,
                source_plan_id=record.id,
                source_plan_hash=plan.plan_hash,
                profile_id=census.profile_id,
                profile_version=census.profile_version,
                occurrence_count=len(census.occurrences),
                census_hash=census.census_hash,
                coverage_hash=census.coverage_hash,
                record_hash="0" * 64,
                created_at=now,
            )
            row.record_hash = _hash(row)
            db.session.add(row)
            db.session.flush()
            for index, item in enumerate(census.occurrences, 1):
                if (item.block_id is not None and item.block_id not in blocks) or (
                    item.anchor_id is not None and item.anchor_id not in anchors
                ):
                    raise BlockContractError("semantic-census-occurrence-owner")
                db.session.add(
                    cast(Any, TranslationSemanticOccurrence)(
                        id=str(uuid.uuid4()),
                        census_id=row.id,
                        source_plan_id=record.id,
                        block_id=item.block_id,
                        anchor_id=item.anchor_id,
                        order_index=index,
                        occurrence_kind=item.occurrence_kind,
                        slot_id=item.slot_id,
                        slot_kind=item.slot_kind,
                        disposition=item.disposition,
                        start_offset=item.start_offset,
                        end_offset=item.end_offset,
                        structural_json=item.structural,
                        value_text=item.value,
                        value_hash=private_witness(
                            item.value, purpose="translator-census-value-v1"
                        ),
                        occurrence_hash=private_witness(
                            _occurrence_witness_material(item),
                            purpose="translator-census-occurrence-v1",
                        ),
                    )
                )
            db.session.flush()
    except (IntegrityError, OperationalError) as exc:
        if isinstance(exc, OperationalError) and not is_database_locked(exc):
            raise
        winner = TranslationSemanticCensus.query.filter_by(
            source_plan_id=record.id,
            profile_id=census.profile_id,
            profile_version=census.profile_version,
            census_hash=census.census_hash,
        ).one_or_none()
        if winner is None:
            if isinstance(exc, OperationalError):
                raise BlockContractError("semantic-census-conflict") from exc
            raise
        # Rehydration verifies the complete occurrence sequence, encrypted
        # witnesses, and stable census identity before returning a race winner.
        rehydrate_semantic_census(winner, record)
        return winner
    return row


def rehydrate_semantic_census(
    census: TranslationSemanticCensus,
    source_plan: TranslationSourcePlanRecord | None = None,
) -> SemanticCensus:
    try:
        record = _record(
            source_plan
            or db.session.get(TranslationSourcePlanRecord, census.source_plan_id)
        )
        plan = rehydrate_source_plan(record)
        if (
            census.project_id != record.project_id
            or census.source_plan_hash != plan.plan_hash
            or census.record_hash != _hash(census)
        ):
            raise BlockContractError("semantic-census-record")
        expected = build_semantic_census(
            plan, profile_id=census.profile_id, profile_version=census.profile_version
        )
        rows = sorted(cast(Any, census.occurrences), key=lambda item: item.order_index)
        if len(rows) != census.occurrence_count:
            raise BlockContractError("semantic-census-count")
        if len(rows) != len(expected.occurrences):
            raise BlockContractError("semantic-census-count")
        coverage_items = tuple(
            item
            for item in expected.occurrences
            if item.occurrence_kind == "coverage"
        )
        coverage_id_candidates = {
            expected_item.slot_id: coverage_occurrence_id_candidates(entry)
            for entry, expected_item in zip(plan.coverage.entries, coverage_items)
        }
        for index, (row, expected_item) in enumerate(zip(rows, expected.occurrences), 1):
            if (
                row.order_index != index
                or row.block_id != expected_item.block_id
                or row.anchor_id != expected_item.anchor_id
                or row.occurrence_kind != expected_item.occurrence_kind
                or (
                    row.slot_id
                    != expected_item.slot_id
                    and (
                        expected_item.occurrence_kind != "coverage"
                        or row.slot_id
                        not in coverage_id_candidates.get(expected_item.slot_id, ())
                    )
                )
                or row.slot_kind != expected_item.slot_kind
                or row.disposition != expected_item.disposition
                or row.start_offset != expected_item.start_offset
                or row.end_offset != expected_item.end_offset
                or row.structural_json != expected_item.structural
                or row.value_text != expected_item.value
                or row.value_hash not in private_witness_candidates(
                    expected_item.value, purpose="translator-census-value-v1"
                )
                or row.occurrence_hash
                not in _occurrence_hash_candidates(expected_item, coverage_id_candidates)
            ):
                raise BlockContractError("semantic-census-occurrence")
        if (
            census.coverage_hash
            not in _coverage_hash_candidates(
                expected.occurrences, coverage_id_candidates
            )
            or census.census_hash != expected.census_hash
        ):
            raise BlockContractError("semantic-census-witness")
        return expected
    except BlockContractError:
        raise
    except Exception as exc:
        raise BlockContractError("semantic-census-corrupt") from exc


def _artifact_hash(row: Any) -> str:
    parent = (
        db.session.get(TranslationContextArtifactRevision, row.parent_revision_id)
        if row.parent_revision_id is not None
        else None
    )
    return make_receipt(
        purpose="translator-context-artifact-receipt-v1",
        values={
                "receipt_version": 1,
                "id": row.id,
                "artifact_id": row.artifact_id,
                "project_id": row.project_id,
                "source_plan_id": row.source_plan_id,
                "revision_number": row.revision_number,
                "parent_revision_id": row.parent_revision_id,
                "parent_record_hash": parent.record_hash if parent is not None else None,
                "analysis_state": row.analysis_state,
                "schema_version": row.schema_version,
                "profile_id": row.profile_id,
                "profile_version": row.profile_version,
                "source_plan_hash": row.source_plan_hash,
                "schema_hash": row.schema_hash,
                "profile_hash": row.profile_hash,
                "model_hash": row.model_hash,
                "prompt_hash": row.prompt_hash,
                "settings_hash": row.settings_hash,
                "input_hash": row.input_hash,
                "output_hash": row.output_hash,
                "evidence_hash": row.evidence_hash,
                "model_json": row.model_json,
                "prompt_json": row.prompt_json,
                "settings_json": row.settings_json,
                "input_json": row.input_json,
                "output_json": row.output_json,
                "evidence_json": row.evidence_json,
                "created_at": row.created_at.isoformat(),
            },
    )


def _artifact_valid(row: Any) -> bool:
    try:
        parent = (
            db.session.get(TranslationContextArtifactRevision, row.parent_revision_id)
            if row.parent_revision_id is not None
            else None
        )
        verify_receipt(
            stored=row.record_hash,
            purpose="translator-context-artifact-receipt-v1",
            values={
                "receipt_version": 1,
                "id": row.id, "artifact_id": row.artifact_id, "project_id": row.project_id,
                "source_plan_id": row.source_plan_id, "revision_number": row.revision_number,
                "parent_revision_id": row.parent_revision_id,
                "parent_record_hash": parent.record_hash if parent is not None else None,
                "analysis_state": row.analysis_state, "schema_version": row.schema_version,
                "profile_id": row.profile_id, "profile_version": row.profile_version,
                "source_plan_hash": row.source_plan_hash, "schema_hash": row.schema_hash,
                "profile_hash": row.profile_hash, "model_hash": row.model_hash,
                "prompt_hash": row.prompt_hash, "settings_hash": row.settings_hash,
                "input_hash": row.input_hash, "output_hash": row.output_hash,
                "evidence_hash": row.evidence_hash, "model_json": row.model_json,
                "prompt_json": row.prompt_json, "settings_json": row.settings_json,
                "input_json": row.input_json, "output_json": row.output_json,
                "evidence_json": row.evidence_json, "created_at": row.created_at.isoformat(),
            },
            code="analysis-artifact-record",
        )
    except BlockContractError:
        return False
    return True


def _artifact_mapping(value: Mapping[str, object] | None, code: str) -> dict[str, object]:
    """Materialize one exact JSON mapping before any artifact write."""

    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise BlockContractError(code)
    material = dict(value)
    try:
        encoded = json.dumps(
            material,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        canonical = json.loads(encoded)
    except (RecursionError, TypeError, ValueError) as exc:
        raise BlockContractError(code) from exc
    if not isinstance(canonical, dict) or canonical != material:
        raise BlockContractError(code)
    return canonical


def _canonical_payload(payload: ContextArtifactPayload) -> ContextArtifactPayload:
    """Revalidate a detached JSON tree before it can participate in writes."""

    if not isinstance(payload, ContextArtifactPayload):
        raise BlockContractError("analysis-artifact-payload")
    try:
        encoded = json.dumps(
            payload.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        material = json.loads(encoded)
        if not isinstance(material, dict):
            raise BlockContractError("analysis-artifact-payload")
        return ContextArtifactPayload(
            kind=material["kind"],
            language=material["language"],
            content=material["content"],
        )
    except BlockContractError:
        raise
    except (KeyError, RecursionError, TypeError, ValueError) as exc:
        raise BlockContractError("analysis-artifact-payload") from exc


def _artifact_profile_identity(value: object) -> str:
    """Validate a profile identity before hashing or SQLite TEXT coercion."""

    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value.encode("utf-8")) > 64
    ):
        raise BlockContractError("analysis-artifact-identity")
    return value


def persist_context_artifact_revision(
    source_plan: TranslationSourcePlanRecord,
    *,
    kind: str,
    payload: ContextArtifactPayload,
    analysis_state: str,
    profile_id: str,
    profile_version: str,
    model: Mapping[str, object] | None = None,
    prompt: Mapping[str, object] | None = None,
    settings: Mapping[str, object] | None = None,
    input_evidence: Mapping[str, object] | None = None,
    evidence: Mapping[str, object] | None = None,
    artifact: TranslationContextArtifact | None = None,
) -> TranslationContextArtifactRevision:
    record = _record(source_plan)
    plan = rehydrate_source_plan(record)
    payload = _canonical_payload(payload)
    profile_id = _artifact_profile_identity(profile_id)
    profile_version = _artifact_profile_identity(profile_version)
    if (
        payload.kind != kind
        or analysis_state not in {"human", "model", "system"}
    ):
        raise BlockContractError("analysis-artifact-identity")
    model_value = _artifact_mapping(model, "analysis-artifact-model")
    prompt_value = _artifact_mapping(prompt, "analysis-artifact-prompt")
    settings_value = _artifact_mapping(settings, "analysis-artifact-settings")
    input_value = _artifact_mapping(input_evidence, "analysis-artifact-input")
    evidence_value = _artifact_mapping(evidence, "analysis-artifact-evidence")
    creating_artifact = artifact is None
    if creating_artifact:
        artifact = cast(Any, TranslationContextArtifact)(
            project_id=record.project_id,
            source_plan_id=record.id,
            owner_user_id=record.project.user_id,
            artifact_kind=kind,
        )
    artifact = cast(TranslationContextArtifact, artifact)
    if (
        artifact.project_id != record.project_id
        or artifact.source_plan_id != record.id
        or artifact.owner_user_id != record.project.user_id
        or artifact.artifact_kind != kind
    ):
        raise BlockContractError("analysis-artifact-owner")
    prior = None
    if not creating_artifact:
        prior = (
            TranslationContextArtifactRevision.query.filter_by(artifact_id=artifact.id)
            .order_by(TranslationContextArtifactRevision.revision_number.desc())
            .first()
        )
    if prior is not None:
        rehydrate_context_artifact_revision(prior, record)
    output_value = payload.to_dict()
    hashes = {
        "schema_hash": sha256_text(ARTIFACT_SCHEMA_VERSION),
        "profile_hash": sha256_text(
            canonical_json({"id": profile_id, "version": profile_version})
        ),
        "model_hash": private_witness(model_value, purpose="translator-artifact-model-v1"),
        "prompt_hash": private_witness(prompt_value, purpose="translator-artifact-prompt-v1"),
        "settings_hash": private_witness(settings_value, purpose="translator-artifact-settings-v1"),
        "input_hash": private_witness(input_value, purpose="translator-artifact-input-v1"),
        "output_hash": private_witness(output_value, purpose="translator-artifact-output-v1"),
        "evidence_hash": private_witness(evidence_value, purpose="translator-artifact-evidence-v1"),
    }
    _ensure_database_transaction()
    with db.session.begin_nested():
        if creating_artifact:
            db.session.add(artifact)
            db.session.flush()
        now = utc_now_db()
        row = cast(Any, TranslationContextArtifactRevision)(
            id=str(uuid.uuid4()),
            artifact_id=artifact.id,
            project_id=record.project_id,
            source_plan_id=record.id,
            revision_number=(prior.revision_number + 1 if prior else 1),
            parent_revision_id=(prior.id if prior else None),
            analysis_state=analysis_state,
            schema_version=ARTIFACT_SCHEMA_VERSION,
            profile_id=profile_id,
            profile_version=profile_version,
            source_plan_hash=plan.plan_hash,
            record_hash="0" * 64,
            model_json=model_value,
            prompt_json=prompt_value,
            settings_json=settings_value,
            input_json=input_value,
            output_json=output_value,
            evidence_json=evidence_value,
            created_at=now,
            **hashes,
        )
        row.record_hash = _artifact_hash(row)
        db.session.add(row)
        db.session.flush()
    return row


def rehydrate_context_artifact_revision(
    revision: TranslationContextArtifactRevision,
    source_plan: TranslationSourcePlanRecord | None = None,
    _seen: set[str] | None = None,
) -> ContextArtifactPayload:
    try:
        if not isinstance(revision, TranslationContextArtifactRevision) or not revision.id:
            raise BlockContractError("analysis-artifact-record")
        seen = _seen if _seen is not None else set()
        if revision.id in seen:
            raise BlockContractError("analysis-artifact-lineage")
        seen.add(revision.id)
        record = _record(
            source_plan
            or db.session.get(TranslationSourcePlanRecord, revision.source_plan_id)
        )
        artifact = db.session.get(TranslationContextArtifact, revision.artifact_id)
        if (
            artifact is None
            or artifact.project_id != record.project_id
            or artifact.source_plan_id != record.id
            or artifact.owner_user_id != record.project.user_id
            or revision.project_id != record.project_id
            or revision.source_plan_id != record.id
            or revision.source_plan_hash != rehydrate_source_plan(record).plan_hash
            or not _artifact_valid(revision)
        ):
            raise BlockContractError("analysis-artifact-record")
        parent = (
            db.session.get(TranslationContextArtifactRevision, revision.parent_revision_id)
            if revision.parent_revision_id is not None else None
        )
        if revision.revision_number == 1:
            if parent is not None:
                raise BlockContractError("analysis-artifact-lineage")
        elif (
            parent is None
            or parent.id == revision.id
            or parent.artifact_id != revision.artifact_id
            or parent.project_id != record.project_id
            or parent.source_plan_id != record.id
            or parent.revision_number + 1 != revision.revision_number
        ):
            raise BlockContractError("analysis-artifact-lineage")
        if parent is not None:
            rehydrate_context_artifact_revision(parent, record, seen)
        payload = ContextArtifactPayload(
            kind=revision.output_json.get("kind"),
            language=revision.output_json.get("language"),
            content=revision.output_json.get("content"),
        )
        if (
            revision.output_json != payload.to_dict()
            or payload.kind != artifact.artifact_kind
            or revision.schema_version != ARTIFACT_SCHEMA_VERSION
            or revision.schema_hash != sha256_text(ARTIFACT_SCHEMA_VERSION)
            or revision.profile_hash
            != sha256_text(
                canonical_json(
                    {"id": revision.profile_id, "version": revision.profile_version}
                )
            )
            or not isinstance(revision.model_json, Mapping)
            or not isinstance(revision.prompt_json, Mapping)
            or not isinstance(revision.settings_json, Mapping)
            or revision.model_hash not in private_witness_candidates(
                dict(revision.model_json), purpose="translator-artifact-model-v1"
            )
            or revision.prompt_hash not in private_witness_candidates(
                dict(revision.prompt_json), purpose="translator-artifact-prompt-v1"
            )
            or revision.settings_hash not in private_witness_candidates(
                dict(revision.settings_json), purpose="translator-artifact-settings-v1"
            )
            or revision.output_hash not in private_witness_candidates(
                revision.output_json, purpose="translator-artifact-output-v1"
            )
            or revision.input_hash not in private_witness_candidates(
                revision.input_json, purpose="translator-artifact-input-v1"
            )
            or revision.evidence_hash not in private_witness_candidates(
                revision.evidence_json, purpose="translator-artifact-evidence-v1"
            )
        ):
            raise BlockContractError("analysis-artifact-witness")
        return payload
    except BlockContractError:
        raise
    except Exception as exc:
        raise BlockContractError("analysis-artifact-corrupt") from exc


__all__ = [
    "persist_context_artifact_revision",
    "persist_semantic_census",
    "rehydrate_context_artifact_revision",
    "rehydrate_semantic_census",
]
