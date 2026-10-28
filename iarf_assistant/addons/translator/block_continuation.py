"""Frozen active-target coverage for literal translate-remaining operations."""

from __future__ import annotations

from collections.abc import Mapping

from .block_contract import BlockContractError
from .block_persistence import rehydrate_source_plan
from .models import (
    TranslationAlignmentRevision,
    TranslationSourcePlan,
    TranslationTargetAlignmentActive,
    TranslationTargetBlockActive,
    TranslationTargetBlockRevision,
)
from .rehydration_cache import TranslationRehydrationCache
from .target_persistence import rehydrate_target_revision, verify_active_pair

PROTECTED_TARGETS_CONTRACT_VERSION = "translator-protected-targets-v1"
MAX_PROTECTED_TARGETS = 50_000
TRANSLATION_SCOPE_CONTRACT_VERSION = "translator-translation-scope-v1"
TRANSLATION_SCOPE_MODES = frozenset({"pending", "explicit"})
_HASH_CHARS = frozenset("0123456789abcdef")
_TARGET_KEYS = frozenset(
    {"block_id", "revision_id", "revision_record_hash", "version", "pair_receipt"}
)


def _fail(code: str = "block-continuation-invalid") -> BlockContractError:
    return BlockContractError(code)


def _identifier(value: object, *, maximum: int = 128) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or not value.isprintable()
    ):
        raise _fail()
    return value


def _digest(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in _HASH_CHARS for character in value)
    ):
        raise _fail()
    return value


def normalize_protected_targets(value: object) -> dict[str, object]:
    """Return the sole canonical JSON shape accepted by the workflow config."""

    if not isinstance(value, Mapping) or set(value) != {
        "contract_version",
        "targets",
    }:
        raise _fail()
    if value.get("contract_version") != PROTECTED_TARGETS_CONTRACT_VERSION:
        raise _fail()
    raw_targets = value.get("targets")
    if not isinstance(raw_targets, list) or len(raw_targets) > MAX_PROTECTED_TARGETS:
        raise _fail()
    targets: list[dict[str, object]] = []
    seen: set[str] = set()
    for raw in raw_targets:
        if not isinstance(raw, Mapping) or set(raw) != _TARGET_KEYS:
            raise _fail()
        block_id = _identifier(raw.get("block_id"))
        if block_id in seen:
            raise _fail()
        seen.add(block_id)
        revision_id = _identifier(raw.get("revision_id"), maximum=36)
        version = raw.get("version")
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise _fail()
        targets.append(
            {
                "block_id": block_id,
                "revision_id": revision_id,
                "revision_record_hash": _digest(raw.get("revision_record_hash")),
                "version": version,
                "pair_receipt": _digest(raw.get("pair_receipt")),
            }
        )
    return {
        "contract_version": PROTECTED_TARGETS_CONTRACT_VERSION,
        "targets": targets,
    }


def normalize_translation_scope(value: object) -> dict[str, object]:
    """Return the exact block scope frozen for one translation operation."""

    if not isinstance(value, Mapping) or set(value) != {
        "contract_version",
        "mode",
        "block_ids",
    }:
        raise _fail("block-translation-scope")
    if value.get("contract_version") != TRANSLATION_SCOPE_CONTRACT_VERSION:
        raise _fail("block-translation-scope")
    mode = value.get("mode")
    raw_block_ids = value.get("block_ids")
    if mode not in TRANSLATION_SCOPE_MODES or not isinstance(raw_block_ids, list):
        raise _fail("block-translation-scope")
    if len(raw_block_ids) > MAX_PROTECTED_TARGETS:
        raise _fail("block-translation-scope")
    block_ids = [_identifier(item) for item in raw_block_ids]
    if len(set(block_ids)) != len(block_ids):
        raise _fail("block-translation-scope")
    if (mode == "pending" and block_ids) or (mode == "explicit" and not block_ids):
        raise _fail("block-translation-scope")
    return {
        "contract_version": TRANSLATION_SCOPE_CONTRACT_VERSION,
        "mode": mode,
        "block_ids": block_ids,
    }


def snapshot_protected_targets(source_plan: TranslationSourcePlan) -> dict[str, object]:
    """Freeze every currently active, verified target in source-block order."""

    plan = rehydrate_source_plan(source_plan)
    pointers = {
        pointer.block_id: pointer
        for pointer in TranslationTargetBlockActive.query.populate_existing()
        .filter_by(source_plan_id=source_plan.id)
        .all()
    }
    alignment_pointers = {
        pointer.block_id: pointer
        for pointer in TranslationTargetAlignmentActive.query.populate_existing()
        .filter_by(source_plan_id=source_plan.id)
        .all()
    }
    revisions = (
        {
            row.id: row
            for row in TranslationTargetBlockRevision.query.populate_existing()
            .filter(
                TranslationTargetBlockRevision.id.in_(
                    [pointer.revision_id for pointer in pointers.values()]
                )
            )
            .all()
        }
        if pointers
        else {}
    )
    alignments = (
        {
            row.id: row
            for row in TranslationAlignmentRevision.query.populate_existing()
            .filter(
                TranslationAlignmentRevision.id.in_(
                    [
                        pointer.alignment_revision_id
                        for pointer in alignment_pointers.values()
                    ]
                )
            )
            .all()
        }
        if alignment_pointers
        else {}
    )
    verification_cache = TranslationRehydrationCache()
    block_ids = {block.id for block in plan.blocks}
    if not set(pointers).issubset(block_ids):
        raise _fail("block-continuation-owner")
    targets: list[dict[str, object]] = []
    for block in plan.blocks:
        pointer = pointers.get(block.id)
        if pointer is None:
            continue
        alignment_pointer = alignment_pointers.get(block.id)
        revision = revisions.get(pointer.revision_id)
        alignment = (
            None
            if alignment_pointer is None
            else alignments.get(alignment_pointer.alignment_revision_id)
        )
        if alignment_pointer is None or revision is None or alignment is None:
            raise _fail("block-continuation-pair")
        verify_active_pair(
            pointer,
            alignment_pointer,
            revision=revision,
            alignment=alignment,
        )
        rehydrate_target_revision(
            revision,
            source_plan,
            verified_plan=plan,
            verification_cache=verification_cache,
        )
        if revision.block_id != block.id or revision.source_plan_id != source_plan.id:
            raise _fail("block-continuation-owner")
        targets.append(
            {
                "block_id": block.id,
                "revision_id": revision.id,
                "revision_record_hash": revision.revision_record_hash,
                "version": pointer.version,
                "pair_receipt": pointer.pair_receipt,
            }
        )
    return normalize_protected_targets(
        {
            "contract_version": PROTECTED_TARGETS_CONTRACT_VERSION,
            "targets": targets,
        }
    )


def verify_protected_targets(
    source_plan: TranslationSourcePlan,
    value: object,
) -> frozenset[str]:
    """Verify frozen pairs and return block IDs excluded from provider dispatch."""

    plan = rehydrate_source_plan(source_plan)
    material = normalize_protected_targets(value)
    plan_ids = {block.id for block in plan.blocks}
    protected: set[str] = set()
    raw_targets = material["targets"]
    assert isinstance(raw_targets, list)
    requested_revision_ids = [str(item["revision_id"]) for item in raw_targets]
    pointers = (
        {
            row.block_id: row
            for row in TranslationTargetBlockActive.query.populate_existing()
            .filter(
                TranslationTargetBlockActive.source_plan_id == source_plan.id,
                TranslationTargetBlockActive.block_id.in_(
                    [str(item["block_id"]) for item in raw_targets]
                ),
            )
            .all()
        }
        if raw_targets
        else {}
    )
    alignment_pointers = (
        {
            row.block_id: row
            for row in TranslationTargetAlignmentActive.query.populate_existing()
            .filter(
                TranslationTargetAlignmentActive.source_plan_id == source_plan.id,
                TranslationTargetAlignmentActive.block_id.in_(list(pointers)),
            )
            .all()
        }
        if pointers
        else {}
    )
    revisions = (
        {
            row.id: row
            for row in TranslationTargetBlockRevision.query.populate_existing()
            .filter(TranslationTargetBlockRevision.id.in_(requested_revision_ids))
            .all()
        }
        if requested_revision_ids
        else {}
    )
    alignments = (
        {
            row.id: row
            for row in TranslationAlignmentRevision.query.populate_existing()
            .filter(
                TranslationAlignmentRevision.id.in_(
                    [
                        pointer.alignment_revision_id
                        for pointer in alignment_pointers.values()
                    ]
                )
            )
            .all()
        }
        if alignment_pointers
        else {}
    )
    verification_cache = TranslationRehydrationCache()
    for item in raw_targets:
        assert isinstance(item, dict)
        block_id = str(item["block_id"])
        if block_id not in plan_ids:
            raise _fail("block-continuation-owner")
        pointer = pointers.get(block_id)
        alignment_pointer = alignment_pointers.get(block_id)
        revision = revisions.get(str(item["revision_id"]))
        alignment = (
            None
            if alignment_pointer is None
            else alignments.get(alignment_pointer.alignment_revision_id)
        )
        if (
            pointer is None
            or alignment_pointer is None
            or revision is None
            or alignment is None
        ):
            raise _fail("block-continuation-drift")
        verify_active_pair(
            pointer,
            alignment_pointer,
            revision=revision,
            alignment=alignment,
        )
        rehydrate_target_revision(
            revision,
            source_plan,
            verified_plan=plan,
            verification_cache=verification_cache,
        )
        if (
            pointer.revision_id != item["revision_id"]
            or pointer.version != item["version"]
            or pointer.pair_receipt != item["pair_receipt"]
            or revision.source_plan_id != source_plan.id
            or revision.block_id != block_id
            or revision.revision_record_hash != item["revision_record_hash"]
        ):
            raise _fail("block-continuation-drift")
        protected.add(block_id)
    return frozenset(protected)


__all__ = [
    "PROTECTED_TARGETS_CONTRACT_VERSION",
    "TRANSLATION_SCOPE_CONTRACT_VERSION",
    "normalize_protected_targets",
    "normalize_translation_scope",
    "snapshot_protected_targets",
    "verify_protected_targets",
]
