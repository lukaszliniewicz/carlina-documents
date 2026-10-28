"""Persistence boundary for the experimental document-block translator.

This module is deliberately small and boring.  The planner and the identity
renderer own all source semantics; this boundary only maps their immutable
DTOs to encrypted ORM rows and proves that a row can be rehydrated without
normalising the source document.

No function in this module commits.  Callers may therefore add the plan to a
larger transaction (for example, one that creates the project and queues a
workflow operation) and retain ownership of the final commit.
"""

from __future__ import annotations

import threading
import uuid
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import inspect, update
from sqlalchemy.exc import IntegrityError

from carlina.core.semantic_export import (
    SemanticExportContractError,
    SemanticExportResource,
)
from carlina.core.utc import as_utc
from iarf_assistant.extensions import db

from .block_contract import (
    DOCUMENT_BLOCK_ENGINE_CONTRACT,
    LEGACY_ENGINE_CONTRACT,
    PROTECTED_INLINE_CONTRACT_VERSION,
    SOURCE_PLAN_CONTRACT_VERSION,
    AttestedDocumentSnapshot,
    BlockContractError,
    BlockWirePart,
    CoverageWitness,
    ProtectedInlineToken,
    SourceAnchor,
    SourceProviderAttestation,
    SourceSlot,
    TranslationBlock,
    TranslationSourcePlan,
    canonical_json,
    sha256_text,
)
from .block_planner import plan_document_blocks
from .block_rendering import render_source_identity
from .models import (
    TranslationBlock as TranslationBlockRecord,
)
from .models import (
    TranslationProject,
    TranslationSourceAnchor,
    TranslationSourcePlanActive,
)
from .models import (
    TranslationSourcePlan as TranslationSourcePlanRecord,
)

_PROVENANCE_FIELDS: dict[str, int] = {
    "knowledge_source_id": 128,
    "knowledge_source_version_id": 128,
    "knowledge_source_version_number": 32,
    "knowledge_source_content_hash": 64,
}
_PROVENANCE_BYTES = 512
_LOCATOR_KEYS = {"dom", "source_start", "source_end"}
_SLOTS_KEYS = {"contract_version", "slots", "wire"}
_INLINE_KEYS = {"contract_version", "tokens"}
_RESOURCE_METADATA_KEY = "__semantic_export_resources_v1"
_WRAPPED_METADATA_KEY = "__semantic_export_metadata_v1"
_SOURCE_IDENTITY_CACHE_MAX_SIZE = 128
_SOURCE_IDENTITY_CACHE: OrderedDict[str, None] = OrderedDict()
_SOURCE_IDENTITY_CACHE_LOCK = threading.Lock()


def source_attestation_for_project(
    project: TranslationProject,
) -> SourceProviderAttestation:
    """Derive the immutable document-provider pin from one project.

    ``source_reference`` is a project setting rather than an ORM relationship,
    so this boundary validates every field before it can influence a plan.
    The provider reference deliberately does not carry owner identity; that is
    bound here to the persistent project's owner.
    """

    if not isinstance(project, TranslationProject):
        raise BlockContractError("source-attestation-project")
    settings = project.settings
    if not isinstance(settings, Mapping):
        raise BlockContractError("source-attestation-reference")
    reference = settings.get("source_reference")
    if not isinstance(reference, Mapping):
        raise BlockContractError("source-attestation-reference")
    required = {
        "version",
        "capability",
        "provider_addon_id",
        "provider_capability_id",
        "resource_type",
        "resource_id",
    }
    if set(reference) != required:
        raise BlockContractError("source-attestation-reference")
    if (
        isinstance(reference["version"], bool)
        or not isinstance(reference["version"], int)
        or reference["version"] != 1
        or reference["capability"] != "document.processing"
        or reference["resource_type"] != "document"
    ):
        raise BlockContractError("source-attestation-reference")
    for key in (
        "provider_addon_id",
        "provider_capability_id",
        "resource_id",
    ):
        value = reference[key]
        if (
            not isinstance(value, str)
            or not value.strip()
            or value != value.strip()
            or len(value.encode("utf-8")) > 256
        ):
            raise BlockContractError("source-attestation-reference")
    if isinstance(project.user_id, bool) or not isinstance(project.user_id, int):
        raise BlockContractError("source-attestation-owner")
    try:
        attestation = SourceProviderAttestation(
            version=reference["version"],
            capability=reference["capability"],
            provider_addon_id=reference["provider_addon_id"],
            provider_capability_id=reference["provider_capability_id"],
            resource_type=reference["resource_type"],
            resource_id=reference["resource_id"],
            owner_id=str(project.user_id),
        )
    except (BlockContractError, TypeError, ValueError) as exc:
        raise BlockContractError("source-attestation-reference") from exc
    return attestation


def effective_engine_contract(value: object) -> str:
    """Validate the persisted engine selector without inferring new behavior."""

    if value is None:
        raise BlockContractError("engine-contract-missing")
    if not isinstance(value, str):
        raise BlockContractError("engine-contract-type")
    normalized = value.strip()
    if normalized in {LEGACY_ENGINE_CONTRACT, DOCUMENT_BLOCK_ENGINE_CONTRACT}:
        return normalized
    raise BlockContractError("engine-contract-unknown")


def _require_block_engine(value: object) -> None:
    if effective_engine_contract(value) != DOCUMENT_BLOCK_ENGINE_CONTRACT:
        raise BlockContractError("engine-contract-required")


def _parse_aware_timestamp(value: object, code: str) -> datetime:
    """Parse an RFC3339-ish timestamp and return naive UTC DB time."""

    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise BlockContractError(code) from exc
    else:
        raise BlockContractError(code)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BlockContractError(code)
    return as_utc(parsed).replace(tzinfo=None)


def _optional_aware_timestamp(value: object, code: str) -> datetime | None:
    if value is None:
        return None
    return _parse_aware_timestamp(value, code)


def _iso_utc(value: object, code: str) -> str:
    if not isinstance(value, datetime):
        raise BlockContractError(code)
    # SQLAlchemy's DateTime column is intentionally a naive UTC database
    # representation.  At this boundary a naive value is therefore valid and
    # is interpreted as UTC; inbound DTO timestamps are checked separately by
    # ``_parse_aware_timestamp`` before they reach the database.
    return as_utc(value).isoformat().replace("+00:00", "Z")


def _safe_provenance(value: Mapping[str, Any]) -> dict[str, str]:
    """Retain only the source-free provider identity allowlist."""

    if not isinstance(value, Mapping):
        raise BlockContractError("provenance-type")
    result: dict[str, str] = {}
    for key, maximum in _PROVENANCE_FIELDS.items():
        item = value.get(key)
        if item is None:
            continue
        if isinstance(item, bool) or not isinstance(item, (str, int)):
            raise BlockContractError("provenance-field")
        normalized = str(item).strip()
        if not normalized or len(normalized) > maximum:
            raise BlockContractError("provenance-field")
        result[key] = normalized
    if len(canonical_json(result).encode("utf-8")) > _PROVENANCE_BYTES:
        raise BlockContractError("provenance-size")
    return result


def _json_mapping(value: object, code: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise BlockContractError(code)
    return value


def _plan_record_id(plan: TranslationSourcePlan) -> str:
    try:
        namespace = uuid.UUID(plan.namespace)
    except (ValueError, AttributeError, TypeError) as exc:
        raise BlockContractError("plan-namespace") from exc
    return str(uuid.uuid5(namespace, f"source-plan:{plan.plan_hash}"))


def _block_locator(block: TranslationBlock) -> dict[str, object]:
    return {
        "dom": block.locator,
        "source_start": block.source_start,
        "source_end": block.source_end,
    }


def _classification_summary(block: TranslationBlock) -> dict[str, object]:
    dispositions: dict[str, int] = {}
    policies: set[str] = set()
    for slot in block.slots:
        dispositions[slot.disposition] = dispositions.get(slot.disposition, 0) + 1
        policies.add(slot.policy_code)
    kinds: dict[str, int] = {}
    for slot in block.slots:
        kinds[slot.kind] = kinds.get(slot.kind, 0) + 1
    return {
        "role": block.role,
        "slot_count": len(block.slots),
        "token_count": len(block.protected_inline),
        "anchor_count": len(block.anchors),
        "slot_kinds": dict(sorted(kinds.items())),
        "dispositions": dict(sorted(dispositions.items())),
        "policy_codes": sorted(policies),
    }


def _slots_payload(block: TranslationBlock) -> dict[str, object]:
    return {
        "contract_version": SOURCE_PLAN_CONTRACT_VERSION,
        "slots": [slot.to_dict() for slot in block.slots],
        "wire": [part.to_dict() for part in block.wire],
    }


def _inline_payload(block: TranslationBlock) -> dict[str, object]:
    return {
        "contract_version": PROTECTED_INLINE_CONTRACT_VERSION,
        "tokens": [token.to_dict() for token in block.protected_inline],
    }


def _validate_payload_keys(value: object, keys: set[str], code: str) -> Mapping[str, Any]:
    payload = _json_mapping(value, code)
    if set(payload) != keys:
        raise BlockContractError(code + "-keys")
    return payload


def _planning_equal(left: TranslationSourcePlan, right: TranslationSourcePlan) -> bool:
    """Compare only fields included in deterministic plan identity."""

    return left._hash_payload() == right._hash_payload()


def _verify_source_identity(plan: TranslationSourcePlan) -> None:
    """Validate one exact plan DTO, memoizing successful renders by full DTO.

    Soundness notes for reviewers. The cache stores digest markers only
    (``None`` values): it never returns a DTO, mutable or otherwise, so no
    cached object can bypass verification. Every caller (``_rehydrate``)
    rechecks the authoritative persisted inputs first on every load:
    project pins, fragment hashes, anchor hashes, attestation pins, plan
    hash, and scalar fields. Only then does this render run, and only a
    success is memoized. The key is the complete plan DTO digest, never
    the plan hash alone: two plans sharing a hash but differing in
    metadata or source text get distinct entries. Failures are never
    cached, the map is bounded LRU (128), and a tampered record rebuilds
    a different DTO that misses and revalidates from reread rows.
    """

    key = sha256_text(canonical_json(plan.to_dict()))
    with _SOURCE_IDENTITY_CACHE_LOCK:
        if key in _SOURCE_IDENTITY_CACHE:
            _SOURCE_IDENTITY_CACHE.move_to_end(key)
            return
    render_source_identity(plan)
    with _SOURCE_IDENTITY_CACHE_LOCK:
        _SOURCE_IDENTITY_CACHE[key] = None
        _SOURCE_IDENTITY_CACHE.move_to_end(key)
        while len(_SOURCE_IDENTITY_CACHE) > _SOURCE_IDENTITY_CACHE_MAX_SIZE:
            _SOURCE_IDENTITY_CACHE.popitem(last=False)


def _project_pin_compatible(project: TranslationProject, plan: TranslationSourcePlan) -> None:
    snapshot = plan.snapshot
    if project.document_id is not None and project.document_id != snapshot.document_id:
        raise BlockContractError("project-document-mismatch")
    if (
        project.source_content_hash is not None
        and project.source_content_hash != snapshot.content_hash
    ):
        raise BlockContractError("project-source-hash-mismatch")
    if project.source_snapshot is not None and project.source_snapshot != snapshot.output_html:
        raise BlockContractError("project-source-snapshot-mismatch")


def _pin_project(project: TranslationProject, plan: TranslationSourcePlan, captured_at: datetime) -> None:
    snapshot = plan.snapshot
    if project.document_id is None:
        project.document_id = snapshot.document_id
    if project.source_snapshot is None:
        project.source_snapshot = snapshot.output_html
    if project.source_content_hash is None:
        project.source_content_hash = snapshot.content_hash
    if project.source_captured_at is None:
        project.source_captured_at = captured_at


def _make_plan_record(project: TranslationProject, plan: TranslationSourcePlan, captured_at: datetime, source_updated_at: datetime | None) -> TranslationSourcePlanRecord:
    snapshot = plan.snapshot
    snapshot_dto = snapshot.to_dict()
    return TranslationSourcePlanRecord(
        id=_plan_record_id(plan),
        project_id=project.id,
        contract_version=plan.contract_version,
        source_document_id=snapshot.document_id,
        source_filename=snapshot.filename,
        source_language=plan.source_language,
        language_profile=plan.language_profile,
        namespace=plan.namespace,
        snapshot_contract_version=snapshot.snapshot_contract_version,
        semantic_contract_version=snapshot.semantic_contract_version,
        semantic_contract_mode=snapshot.semantic_contract_mode,
        semantic_content_hash=snapshot.semantic_content_hash,
        source_content_hash=snapshot.content_hash,
        snapshot_captured_at=captured_at,
        source_updated_at=source_updated_at,
        planner_version=plan.planner_version,
        planner_settings_hash=plan.settings_fingerprint,
        plan_hash=plan.plan_hash,
        # ``AttestedDocumentSnapshot`` freezes nested metadata with tuples and
        # mapping proxies.  The JSON encrypted type intentionally accepts only
        # ordinary JSON containers, so use the DTO's plain representation.
        source_metadata=(
            {
                _WRAPPED_METADATA_KEY: snapshot_dto["metadata"],
                _RESOURCE_METADATA_KEY: snapshot_dto["resources"],
            }
            if snapshot.resources
            else snapshot_dto["metadata"]
        ),
        source_attestation=plan.source_attestation.to_dict(),
        planner_settings=plan.to_dict()["planner_settings"],
        source_provenance=_safe_provenance(snapshot.provenance),
        coverage_witness=plan.coverage.to_dict(),
        block_count=len(plan.blocks),
        anchor_count=sum(len(block.anchors) for block in plan.blocks),
    )


def _make_block_record(record: TranslationSourcePlanRecord, plan: TranslationSourcePlan, block: TranslationBlock) -> TranslationBlockRecord:
    source = plan.snapshot.output_html[block.source_start : block.source_end]
    return TranslationBlockRecord(
        id=block.id,
        source_plan_id=record.id,
        order_index=block.order,
        role=block.role,
        locator=_block_locator(block),
        classification_summary=_classification_summary(block),
        source_fragment=source,
        source_projection=block.projection,
        source_slots=_slots_payload(block),
        protected_inline=_inline_payload(block),
        fragment_hash=sha256_text(source),
        projection_hash=block.projection_hash,
    )


def _make_anchor_records(record: TranslationSourcePlanRecord, block_record: TranslationBlockRecord, block: TranslationBlock, global_offset: int) -> list[TranslationSourceAnchor]:
    return [
        TranslationSourceAnchor(
            id=anchor.id,
            source_plan_id=record.id,
            block_id=block_record.id,
            order_index=global_offset + anchor.order,
            block_order_index=anchor.order,
            start_offset=anchor.start,
            end_offset=anchor.end,
            splitter_profile=anchor.splitter_profile,
            splitter_version=anchor.splitter_version,
            source_text=block.projection[anchor.start : anchor.end],
            source_hash=anchor.source_hash,
        )
        for anchor in block.anchors
    ]


def _validate_record_scalar(record: TranslationSourcePlanRecord, plan: TranslationSourcePlan) -> None:
    snapshot = plan.snapshot
    expected_id = _plan_record_id(plan)
    if record.id != expected_id or record.project_id != plan.project_id:
        raise BlockContractError("persisted-plan-identity")
    expected = {
        "contract_version": plan.contract_version,
        "source_document_id": snapshot.document_id,
        "source_language": plan.source_language,
        "language_profile": plan.language_profile,
        "namespace": plan.namespace,
        "snapshot_contract_version": snapshot.snapshot_contract_version,
        "semantic_contract_version": snapshot.semantic_contract_version,
        "semantic_contract_mode": snapshot.semantic_contract_mode,
        "semantic_content_hash": snapshot.semantic_content_hash,
        "source_content_hash": snapshot.content_hash,
        "planner_version": plan.planner_version,
        "planner_settings_hash": plan.settings_fingerprint,
        "plan_hash": plan.plan_hash,
    }
    for key, expected_value in expected.items():
        if getattr(record, key) != expected_value:
            raise BlockContractError("persisted-plan-scalar")
    if record.block_count != len(plan.blocks) or record.anchor_count != sum(len(block.anchors) for block in plan.blocks):
        raise BlockContractError("persisted-plan-count")
    if record.source_filename != snapshot.filename:
        raise BlockContractError("persisted-plan-filename")
    if record.source_attestation != plan.source_attestation.to_dict():
        raise BlockContractError("persisted-plan-attestation")
    if record.planner_settings is None or canonical_json(record.planner_settings) != canonical_json(
        plan.to_dict()["planner_settings"]
    ):
        raise BlockContractError("persisted-plan-settings")
    if _safe_provenance(snapshot.provenance) != (record.source_provenance or {}):
        raise BlockContractError("persisted-plan-provenance")
    expected_metadata = (
        {
            _WRAPPED_METADATA_KEY: snapshot.to_dict()["metadata"],
            _RESOURCE_METADATA_KEY: snapshot.to_dict()["resources"],
        }
        if snapshot.resources
        else snapshot.to_dict()["metadata"]
    )
    if canonical_json(record.source_metadata or {}) != canonical_json(
        expected_metadata
    ):
        raise BlockContractError("persisted-plan-metadata")


def _rehydrate(record: TranslationSourcePlanRecord) -> TranslationSourcePlan:
    project = record.project
    if project is None or project.source_snapshot is None:
        raise BlockContractError("persisted-project-snapshot")
    if project.id != record.project_id:
        raise BlockContractError("persisted-project-identity")
    _require_block_engine(project.engine_contract)
    if (
        project.document_id != record.source_document_id
        or project.source_content_hash != record.source_content_hash
    ):
        raise BlockContractError("persisted-project-source-pin")
    if record.source_metadata is None:
        raise BlockContractError("persisted-metadata")
    source_attestation = SourceProviderAttestation.from_dict(
        _json_mapping(record.source_attestation, "persisted-attestation")
    )
    if source_attestation != source_attestation_for_project(project):
        raise BlockContractError("persisted-attestation-pin")
    if source_attestation.resource_id != record.source_document_id:
        raise BlockContractError("persisted-attestation-resource")
    planner_settings = _json_mapping(record.planner_settings, "persisted-settings")
    if not isinstance(record.source_content_hash, str) or sha256_text(project.source_snapshot) != record.source_content_hash:
        raise BlockContractError("persisted-project-source-hash")
    captured = _iso_utc(record.snapshot_captured_at, "persisted-captured-at")
    updated = _iso_utc(record.source_updated_at, "persisted-source-updated-at") if record.source_updated_at is not None else None
    stored_metadata = _json_mapping(record.source_metadata or {}, "persisted-metadata")
    resources = ()
    if _RESOURCE_METADATA_KEY in stored_metadata or _WRAPPED_METADATA_KEY in stored_metadata:
        if set(stored_metadata) != {_RESOURCE_METADATA_KEY, _WRAPPED_METADATA_KEY}:
            raise BlockContractError("persisted-metadata")
        stored_resources = stored_metadata.get(_RESOURCE_METADATA_KEY)
        if not isinstance(stored_resources, list):
            raise BlockContractError("persisted-metadata")
        try:
            resources = tuple(SemanticExportResource.from_dict(item) for item in stored_resources)
        except (SemanticExportContractError, TypeError, ValueError) as exc:
            raise BlockContractError("persisted-metadata") from exc
        stored_metadata = _json_mapping(stored_metadata.get(_WRAPPED_METADATA_KEY), "persisted-metadata")
    snapshot = AttestedDocumentSnapshot(
        id=record.source_document_id,
        document_id=record.source_document_id,
        output_html=project.source_snapshot,
        content_hash=record.source_content_hash,
        semantic_content_hash=record.semantic_content_hash,
        metadata=stored_metadata,
        provenance=_safe_provenance(record.source_provenance or {}),
        filename=record.source_filename,
        captured_at=captured,
        source_updated_at=updated,
        snapshot_contract_version=record.snapshot_contract_version,
        semantic_contract_version=record.semantic_contract_version,
        semantic_contract_mode=record.semantic_contract_mode,
        status="completed",
        resources=resources,
    )
    blocks: list[TranslationBlock] = []
    anchors_by_block: dict[str, list[TranslationSourceAnchor]] = {}
    all_anchors = sorted(record.anchors, key=lambda item: item.order_index)
    if [item.order_index for item in all_anchors] != list(range(1, len(all_anchors) + 1)):
        raise BlockContractError("persisted-anchor-order")
    anchor_ids: set[str] = set()
    for anchor in all_anchors:
        if anchor.source_plan_id != record.id or anchor.id in anchor_ids:
            raise BlockContractError("persisted-anchor-identity")
        anchor_ids.add(anchor.id)
        anchors_by_block.setdefault(anchor.block_id, []).append(anchor)
    for block_record in sorted(record.blocks, key=lambda item: item.order_index):
        slots_payload = _validate_payload_keys(block_record.source_slots, _SLOTS_KEYS, "persisted-slots")
        if slots_payload["contract_version"] != SOURCE_PLAN_CONTRACT_VERSION or not isinstance(slots_payload["slots"], list) or not isinstance(slots_payload["wire"], list):
            raise BlockContractError("persisted-slots-shape")
        inline_payload = _validate_payload_keys(block_record.protected_inline, _INLINE_KEYS, "persisted-inline")
        if inline_payload["contract_version"] != PROTECTED_INLINE_CONTRACT_VERSION or not isinstance(inline_payload["tokens"], list):
            raise BlockContractError("persisted-inline-shape")
        locator = _json_mapping(block_record.locator, "persisted-locator")
        if set(locator) != _LOCATOR_KEYS:
            raise BlockContractError("persisted-locator-keys")
        source_start, source_end = locator.get("source_start"), locator.get("source_end")
        if not isinstance(source_start, int) or isinstance(source_start, bool) or not isinstance(source_end, int) or isinstance(source_end, bool):
            raise BlockContractError("persisted-locator-span")
        _require_fragment(project.source_snapshot, source_start, source_end, block_record.source_fragment, block_record.fragment_hash)
        slots = tuple(SourceSlot.from_dict(item) for item in slots_payload["slots"])
        wire = tuple(BlockWirePart.from_dict(item) for item in slots_payload["wire"])
        tokens = tuple(ProtectedInlineToken.from_dict(item) for item in inline_payload["tokens"])
        anchor_records = anchors_by_block.get(block_record.id, [])
        if [item.block_order_index for item in anchor_records] != list(range(1, len(anchor_records) + 1)):
            raise BlockContractError("persisted-block-anchor-order")
        anchors: list[SourceAnchor] = []
        for item in anchor_records:
            if item.block_id != block_record.id or item.source_plan_id != record.id:
                raise BlockContractError("persisted-anchor-owner")
            if item.start_offset < 0 or item.end_offset > len(block_record.source_projection) or item.end_offset <= item.start_offset:
                raise BlockContractError("persisted-anchor-span")
            source_text = block_record.source_projection[item.start_offset : item.end_offset]
            if item.source_text != source_text or sha256_text(source_text) != item.source_hash:
                raise BlockContractError("persisted-anchor-text")
            anchors.append(SourceAnchor(id=item.id, order=item.block_order_index, start=item.start_offset, end=item.end_offset, source_hash=item.source_hash, splitter_profile=item.splitter_profile, splitter_version=item.splitter_version))
        block = TranslationBlock(id=block_record.id, order=block_record.order_index, role=block_record.role, locator=str(locator["dom"]), source_start=source_start, source_end=source_end, projection=block_record.source_projection, projection_hash=block_record.projection_hash, slots=slots, protected_inline=tokens, wire=wire, anchors=tuple(anchors))
        if block_record.classification_summary != _classification_summary(block):
            raise BlockContractError("persisted-classification")
        blocks.append(block)
    if record.block_count != len(blocks) or record.anchor_count != sum(len(block.anchors) for block in blocks):
        raise BlockContractError("persisted-plan-count")
    coverage_value = _json_mapping(record.coverage_witness, "persisted-coverage")
    coverage = CoverageWitness.from_dict(coverage_value)
    plan = TranslationSourcePlan.create(snapshot=snapshot, source_attestation=source_attestation, project_id=record.project_id, planner_version=record.planner_version, source_language=record.source_language, language_profile=record.language_profile, planner_settings=planner_settings, settings_fingerprint=record.planner_settings_hash, namespace=record.namespace, blocks=tuple(blocks), coverage=coverage)
    if plan.plan_hash != record.plan_hash:
        raise BlockContractError("persisted-plan-hash")
    _validate_record_scalar(record, plan)
    _verify_source_identity(plan)
    return plan


def _require_fragment(source: str, start: int, end: int, stored: object, digest: object) -> str:
    if start < 0 or end <= start or end > len(source) or not isinstance(stored, str) or stored != source[start:end] or sha256_text(stored) != digest:
        raise BlockContractError("persisted-fragment")
    return stored


def _ensure_database_transaction(session: Any | None = None) -> None:
    """Make SQLite's outer transaction explicit before creating a savepoint.

    SQLite's legacy driver mode starts a transaction for DML but not for a
    savepoint. Releasing the first savepoint could otherwise make rows durable
    even when the caller later rolls back the SQLAlchemy session.
    """

    active_session = session or db.session()
    connection = active_session.connection()
    if connection.dialect.name != "sqlite":
        return
    proxied = connection.connection
    driver = getattr(proxied, "driver_connection", proxied)
    if not bool(getattr(driver, "in_transaction", False)):
        connection.exec_driver_sql("BEGIN")


def rehydrate_source_plan(record: TranslationSourcePlanRecord) -> TranslationSourcePlan:
    """Load and validate an immutable source plan without exposing source text in errors."""

    try:
        if not isinstance(record, TranslationSourcePlanRecord):
            raise BlockContractError("persisted-plan-type")
        return _rehydrate(record)
    except BlockContractError:
        raise
    except Exception as exc:  # encrypted corruption and malformed JSON are bounded
        raise BlockContractError("persisted-plan-corrupt") from exc


def verify_source_plan_binding(
    record: TranslationSourcePlanRecord,
) -> TranslationSourcePlanRecord:
    """Verify a plan's project binding without loading its block collection.

    This narrower boundary is for records, such as glossary revisions, whose
    authority depends on the active plan identity but whose payload does not
    consume source blocks or anchors.  Consumers of source content must still
    call :func:`rehydrate_source_plan` and receive the complete aggregate
    verification.
    """

    try:
        if not isinstance(record, TranslationSourcePlanRecord):
            raise BlockContractError("persisted-plan-type")
        state = inspect(record)
        if state is None or not state.persistent:
            raise BlockContractError("persisted-plan-persistent")
        project = record.project
        if project is None or project.source_snapshot is None:
            raise BlockContractError("persisted-project-snapshot")
        if project.id != record.project_id:
            raise BlockContractError("persisted-project-identity")
        _require_block_engine(project.engine_contract)
        if (
            project.document_id != record.source_document_id
            or project.source_content_hash != record.source_content_hash
            or not isinstance(record.source_content_hash, str)
            or sha256_text(project.source_snapshot) != record.source_content_hash
        ):
            raise BlockContractError("persisted-project-source-pin")
        if record.contract_version != SOURCE_PLAN_CONTRACT_VERSION:
            raise BlockContractError("persisted-plan-contract")
        if (
            not isinstance(record.plan_hash, str)
            or len(record.plan_hash) != 64
            or any(character not in "0123456789abcdef" for character in record.plan_hash)
        ):
            raise BlockContractError("persisted-plan-hash")
        try:
            namespace = uuid.UUID(record.namespace)
        except (AttributeError, TypeError, ValueError) as exc:
            raise BlockContractError("persisted-plan-namespace") from exc
        expected_id = str(
            uuid.uuid5(namespace, f"source-plan:{record.plan_hash}")
        )
        if record.id != expected_id:
            raise BlockContractError("persisted-plan-identity")
        source_attestation = SourceProviderAttestation.from_dict(
            _json_mapping(record.source_attestation, "persisted-attestation")
        )
        if (
            source_attestation != source_attestation_for_project(project)
            or source_attestation.resource_id != record.source_document_id
        ):
            raise BlockContractError("persisted-attestation-pin")
        planner_settings = _json_mapping(
            record.planner_settings, "persisted-settings"
        )
        if sha256_text(canonical_json(planner_settings)) != record.planner_settings_hash:
            raise BlockContractError("persisted-plan-settings")
        if (
            isinstance(record.block_count, bool)
            or not isinstance(record.block_count, int)
            or record.block_count < 0
            or isinstance(record.anchor_count, bool)
            or not isinstance(record.anchor_count, int)
            or record.anchor_count < 0
        ):
            raise BlockContractError("persisted-plan-count")
        return record
    except BlockContractError:
        raise
    except Exception as exc:
        raise BlockContractError("persisted-plan-corrupt") from exc


def _existing_plan(project: TranslationProject, plan: TranslationSourcePlan) -> TranslationSourcePlanRecord | None:
    return TranslationSourcePlanRecord.query.filter_by(project_id=project.id, plan_hash=plan.plan_hash).one_or_none()


def active_source_plan_pointer(
    project_id: str, *, lock: bool = False
) -> TranslationSourcePlanActive | None:
    """Return the project's explicit active-plan pointer, optionally row-locked."""

    if lock:
        _ensure_database_transaction()
        db.session.execute(
            update(TranslationSourcePlanActive)
            .where(TranslationSourcePlanActive.project_id == str(project_id))
            .values(version=TranslationSourcePlanActive.version)
        )
        db.session.flush()
    query = TranslationSourcePlanActive.query.filter_by(project_id=str(project_id))
    if lock:
        # ``load_block_source_plan`` may already have placed this pointer in the
        # identity map.  Refresh it after acquiring the write/row lock so a
        # concurrent source-plan transition cannot be mistaken for the stale
        # in-session value.
        query = query.populate_existing().with_for_update()
    return query.one_or_none()


def require_active_source_plan(
    project_id: str,
    source_plan_id: str,
    *,
    lock: bool = False,
) -> TranslationSourcePlanActive:
    pointer = active_source_plan_pointer(project_id, lock=lock)
    if pointer is None:
        raise BlockContractError("source-plan-active-missing")
    if pointer.source_plan_id != source_plan_id:
        raise BlockContractError("source-plan-active-conflict")
    return pointer


def _ensure_initial_active_pointer(
    project: TranslationProject, record: TranslationSourcePlanRecord
) -> None:
    pointer = active_source_plan_pointer(project.id)
    if pointer is not None:
        if pointer.source_plan_id == record.id:
            return
        return
    competing = TranslationSourcePlanRecord.query.filter(
        TranslationSourcePlanRecord.project_id == project.id,
        TranslationSourcePlanRecord.id != record.id,
    ).first()
    if competing is not None:
        raise BlockContractError("source-plan-active-missing")
    db.session.add(
        TranslationSourcePlanActive(
            project_id=project.id,
            source_plan_id=record.id,
            version=1,
        )
    )
    db.session.flush()


def persist_source_plan(project: TranslationProject, plan: TranslationSourcePlan) -> tuple[TranslationSourcePlanRecord, bool]:
    """Atomically persist one source plan, preserving caller transaction ownership."""

    if not isinstance(project, TranslationProject):
        raise BlockContractError("project-type")
    state = inspect(project)
    if not state.persistent:
        raise BlockContractError("project-not-persistent")
    if not isinstance(plan, TranslationSourcePlan):
        raise BlockContractError("plan-type")
    # Reparse the DTO at the persistence boundary.  A frozen dataclass can
    # still be deliberately tampered with through ``object.__setattr__`` in a
    # failing worker or test; reparsing re-runs the settings fingerprint,
    # attestation, block, and plan-hash invariants before any database write.
    try:
        plan = TranslationSourcePlan.from_dict(plan.to_dict())
    except BlockContractError:
        raise
    except Exception as exc:
        raise BlockContractError("plan-dto-invalid") from exc
    if plan.project_id != project.id:
        raise BlockContractError("plan-project-mismatch")
    _require_block_engine(project.engine_contract)
    project_attestation = source_attestation_for_project(project)
    if (
        plan.source_attestation != project_attestation
        or plan.source_attestation.resource_id != plan.snapshot.document_id
    ):
        raise BlockContractError("source-attestation-mismatch")
    # Do this before any row or project mutation.  The renderer is the final
    # source identity gate, not a post-persistence diagnostic.
    _verify_source_identity(plan)
    captured_at = _parse_aware_timestamp(plan.snapshot.captured_at, "snapshot-captured-at")
    source_updated_at = _optional_aware_timestamp(plan.snapshot.source_updated_at, "snapshot-source-updated-at")
    _project_pin_compatible(project, plan)
    existing = _existing_plan(project, plan)
    if existing is not None:
        loaded = rehydrate_source_plan(existing)
        if not _planning_equal(loaded, plan):
            raise BlockContractError("persisted-plan-conflict")
        _ensure_initial_active_pointer(project, existing)
        return existing, False
    from .numbering_sync import NumberingSyncError, require_no_numbering_overlay

    try:
        require_no_numbering_overlay(str(project.id))
    except NumberingSyncError as exc:
        raise BlockContractError(exc.code) from exc
    record = _make_plan_record(project, plan, captured_at, source_updated_at)
    _ensure_database_transaction()
    try:
        with db.session.begin_nested():
            _pin_project(project, plan, captured_at)
            db.session.add(record)
            db.session.flush()
            global_offset = 0
            for block in plan.blocks:
                block_record = _make_block_record(record, plan, block)
                db.session.add(block_record)
                db.session.flush()
                db.session.add_all(_make_anchor_records(record, block_record, block, global_offset))
                global_offset += len(block.anchors)
            db.session.flush()
            _ensure_initial_active_pointer(project, record)
    except IntegrityError as exc:
        existing = _existing_plan(project, plan)
        if existing is None:
            raise
        loaded = rehydrate_source_plan(existing)
        if not _planning_equal(loaded, plan):
            raise BlockContractError("persisted-plan-conflict") from exc
        _ensure_initial_active_pointer(project, existing)
        return existing, False
    return record, True


def plan_and_persist_source_snapshot(
    project: TranslationProject,
    snapshot_dto: Mapping[str, Any] | AttestedDocumentSnapshot,
    *,
    source_language: str,
    language_profile: str | None = None,
    settings: Mapping[str, object] | None = None,
    sentence_segmenter: Callable[[str], Sequence[str]] | None = None,
    sentence_batch_segmenter: Callable[[Sequence[str]], Sequence[Sequence[str]]] | None = None,
) -> tuple[TranslationSourcePlanRecord, bool]:
    plan = plan_document_blocks(
        snapshot_dto,
        project_id=project.id,
        source_attestation=source_attestation_for_project(project),
        source_language=source_language,
        language_profile=language_profile,
        settings=settings,
        sentence_segmenter=sentence_segmenter,
        sentence_batch_segmenter=sentence_batch_segmenter,
    )
    return persist_source_plan(project, plan)


__all__ = [
    "active_source_plan_pointer",
    "effective_engine_contract",
    "persist_source_plan",
    "plan_and_persist_source_snapshot",
    "rehydrate_source_plan",
    "require_active_source_plan",
    "source_attestation_for_project",
]
