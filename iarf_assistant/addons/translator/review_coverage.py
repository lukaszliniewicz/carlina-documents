"""Append-only, owner-scoped coverage evidence for translated targets.

Coverage records preserve what a reviewer examined. They remain independent
from block approval decisions and are only current while their target revision
and active version still match.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy.exc import IntegrityError

from carlina.core.utc import utc_now_db

from ...extensions import db
from . import scholarly_review
from .block_persistence import _ensure_database_transaction
from .block_workspace_api import (
    BlockWorkspaceIntegrityError,
    _rehydrate_workspace_objects,
)
from .models import TranslationReviewCoverage, TranslationTargetBlockActive

_CONTRACT = "translator-review-coverage-v1"
_EXPORT_CONTRACT = "translator-export-review-snapshot-v1"
_KINDS = ("structural", "target_reading", "bilingual")
_COVERAGE_PK = "pk_translation_review_coverage"


def _error(code: str) -> scholarly_review.ScholarlyReviewError:
    return scholarly_review.ScholarlyReviewError(code)


def _anchor_ids(block: Any) -> list[str]:
    anchors = getattr(block, "anchors", ())
    if isinstance(anchors, (str, bytes)) or not isinstance(anchors, Sequence):
        raise _error("review-coverage-source-anchors")
    values: list[str] = []
    for anchor in anchors:
        value = getattr(anchor, "id", None)
        if not isinstance(value, str) or not value:
            raise _error("review-coverage-source-anchors")
        values.append(value)
    if len(values) != len(set(values)):
        raise _error("review-coverage-source-anchors")
    return values


def _normalize_entries(entries: object, blocks: Sequence[Any]) -> list[dict[str, Any]]:
    if (
        isinstance(entries, (str, bytes))
        or not isinstance(entries, Sequence)
        or not 1 <= len(entries) <= 100
    ):
        raise _error("review-coverage-entries")
    blocks_by_id = scholarly_review._block_map(blocks)
    seen_blocks: set[str] = set()
    normalized: list[dict[str, Any]] = []
    required_keys = {
        "block_id",
        "expected_target_version",
        "scope",
        "source_anchor_ids",
    }
    for raw in entries:
        if not isinstance(raw, Mapping) or set(raw) != required_keys:
            raise _error("review-coverage-entry")
        block_id = scholarly_review._safe_id(
            raw.get("block_id"), "review-coverage-block"
        )
        if block_id not in blocks_by_id or block_id in seen_blocks:
            raise _error("review-coverage-block")
        seen_blocks.add(block_id)
        expected_version = raw.get("expected_target_version")
        if (
            isinstance(expected_version, bool)
            or not isinstance(expected_version, int)
            or expected_version < 1
        ):
            raise _error("review-coverage-target-version")
        scope = raw.get("scope")
        if not isinstance(scope, str) or scope not in {"block", "anchors"}:
            raise _error("review-coverage-scope")
        raw_anchor_ids = raw.get("source_anchor_ids")
        if isinstance(raw_anchor_ids, (str, bytes)) or not isinstance(
            raw_anchor_ids, Sequence
        ):
            raise _error("review-coverage-source-anchors")
        anchor_ids = [
            scholarly_review._safe_id(item, "review-coverage-source-anchor")
            for item in raw_anchor_ids
        ]
        if len(anchor_ids) != len(set(anchor_ids)):
            raise _error("review-coverage-source-anchors")
        block_anchor_ids = _anchor_ids(blocks_by_id[block_id])
        if scope == "block":
            if anchor_ids:
                raise _error("review-coverage-source-anchors")
        elif not anchor_ids or not set(anchor_ids).issubset(block_anchor_ids):
            raise _error("review-coverage-source-anchors")
        normalized.append(
            {
                "block_id": block_id,
                "expected_target_version": expected_version,
                "scope": scope,
                "source_anchor_ids": anchor_ids,
            }
        )
    return normalized


def _actor_values(actor: scholarly_review.ReviewActor) -> dict[str, Any]:
    return {
        "actor_type": actor.role,
        "actor_user_id": actor.user_id,
        "share_grant_id": actor.share_grant_id,
    }


def _row_values(row: Any) -> dict[str, Any]:
    fields = (
        "id",
        "project_id",
        "source_plan_id",
        "owner_user_id",
        "kind",
        "reviewer_kind",
        "actor_type",
        "actor_user_id",
        "share_grant_id",
        "payload_json",
        "created_at",
    )
    return {field: scholarly_review._get(row, field) for field in fields}


def _verify_row(row: Any) -> Mapping[str, Any]:
    values = _row_values(row)
    if values["payload_json"] is None:
        raise _error("review-coverage-integrity")
    expected_hash = scholarly_review._sha(values)
    if scholarly_review._get(row, "record_hash") != expected_hash:
        raise _error("review-coverage-integrity")
    scholarly_review._verify_revision_receipt(
        row,
        purpose="translator-review-coverage-batch-v1",
        fields=(*values.keys(), "record_hash"),
        code="review-coverage-integrity",
    )
    payload = values["payload_json"]
    if not isinstance(payload, Mapping) or payload.get("contract") != _CONTRACT:
        raise _error("review-coverage-integrity")
    return payload


def _receipt(row: Any, payload: Mapping[str, Any]) -> dict[str, Any]:
    entries = payload.get("entries")
    if not isinstance(entries, list):
        raise _error("review-coverage-integrity")
    projected: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise _error("review-coverage-integrity")
        projected.append(
            {
                "block_id": entry.get("block_id"),
                "target_revision_id": entry.get("target_revision_id"),
                "target_version": entry.get("target_version"),
                "scope": entry.get("scope"),
                "source_anchor_ids": list(entry.get("source_anchor_ids", [])),
            }
        )
    return {
        "coverage_id": str(scholarly_review._get(row, "id", default="")),
        "kind": scholarly_review._get(row, "kind"),
        "reviewer_kind": scholarly_review._get(row, "reviewer_kind"),
        "entries": projected,
    }


def _matches_request(
    row: Any,
    payload: Mapping[str, Any],
    *,
    project: Any,
    source_plan: Any,
    kind: str,
    reviewer_kind: str,
    comment: str,
    entries: Sequence[Mapping[str, Any]],
    actor: scholarly_review.ReviewActor,
) -> bool:
    if (
        str(scholarly_review._get(row, "project_id")) != str(project.id)
        or str(scholarly_review._get(row, "source_plan_id")) != str(source_plan.id)
        or str(scholarly_review._get(row, "owner_user_id")) != str(project.user_id)
        or scholarly_review._get(row, "kind") != kind
        or scholarly_review._get(row, "reviewer_kind") != reviewer_kind
        or scholarly_review._get(row, "actor_type") != actor.role
        or not scholarly_review._same_optional_identity(
            scholarly_review._get(row, "actor_user_id"), actor.user_id
        )
        or not scholarly_review._same_optional_identity(
            scholarly_review._get(row, "share_grant_id"), actor.share_grant_id
        )
        or payload.get("comment") != comment
    ):
        return False
    stored = payload.get("entries")
    if not isinstance(stored, list) or len(stored) != len(entries):
        return False
    for saved, requested in zip(stored, entries, strict=True):
        if not isinstance(saved, Mapping) or any(
            saved.get(field) != requested[field]
            for field in (
                "block_id",
                "expected_target_version",
                "scope",
                "source_anchor_ids",
            )
        ):
            return False
    return True


def _persist_new_row(row: Any) -> None:
    """Insert under a savepoint so a unique-key race leaves the caller usable."""

    _ensure_database_transaction()
    with db.session.begin_nested():
        db.session.add(row)
        db.session.flush()


def _is_coverage_primary_key_conflict(exc: IntegrityError) -> bool:
    original = getattr(exc, "orig", None)
    constraint = getattr(getattr(original, "diag", None), "constraint_name", None)
    if constraint == _COVERAGE_PK:
        return True
    message = str(original or exc).casefold()
    return "translation_review_coverage.id" in message and (
        "unique constraint" in message or "primary key constraint" in message
    )


def record_review_coverage(
    project: Any,
    *,
    entries: object,
    kind: str,
    reviewer_kind: str = "agent",
    comment: str = "",
    actor: scholarly_review.ReviewActor,
    idempotency_key: str,
) -> dict[str, Any]:
    """Record one encrypted batch of target-bound coverage evidence."""

    scholarly_review._require_mutator(project, actor)
    if not isinstance(kind, str) or kind not in _KINDS:
        raise _error("review-coverage-kind")
    if not isinstance(reviewer_kind, str) or reviewer_kind not in {"agent", "human"}:
        raise _error("review-coverage-reviewer-kind")
    comment = (
        scholarly_review._safe_text(comment, "review-coverage-comment", 2000) or ""
    )
    validated_idempotency_key = scholarly_review._write_idempotency_key(
        idempotency_key, "review-coverage-idempotency"
    )
    if validated_idempotency_key is None:
        raise _error("review-coverage-idempotency")
    idempotency_key = validated_idempotency_key

    _workspace, source_plan, blocks = scholarly_review._workspace(project)
    normalized = _normalize_entries(entries, blocks)
    coverage_id = scholarly_review._idempotent_write_id(
        "review-coverage-batch",
        project.id,
        source_plan.id,
        key=idempotency_key,
    )
    replay = db.session.get(TranslationReviewCoverage, coverage_id)
    if replay is not None:
        payload = _verify_row(replay)
        if not _matches_request(
            replay,
            payload,
            project=project,
            source_plan=source_plan,
            kind=kind,
            reviewer_kind=reviewer_kind,
            comment=comment,
            entries=normalized,
            actor=actor,
        ):
            raise _error("review-coverage-idempotency-conflict")
        return _receipt(replay, payload)

    blocks_by_id = scholarly_review._block_map(blocks)
    snapshots: list[dict[str, Any]] = []
    for entry in normalized:
        block_id = entry["block_id"]
        pointer, target_revision, _target = scholarly_review._load_current_target(
            project, source_plan, block_id
        )
        if pointer is None or target_revision is None:
            raise _error("review-coverage-target-missing")
        if (
            int(scholarly_review._get(pointer, "version", default=0))
            != entry["expected_target_version"]
        ):
            raise _error("review-coverage-target-version")
        if (
            str(scholarly_review._get(pointer, "source_plan_id")) != str(source_plan.id)
            or str(scholarly_review._get(pointer, "block_id")) != block_id
            or str(scholarly_review._get(pointer, "revision_id"))
            != str(scholarly_review._get(target_revision, "id"))
        ):
            raise _error("review-coverage-target-owner")
        snapshots.append(
            {
                **entry,
                "target_revision_id": str(scholarly_review._get(target_revision, "id")),
                "target_version": int(scholarly_review._get(pointer, "version")),
                "source_anchor_snapshot": _anchor_ids(blocks_by_id[block_id]),
            }
        )

    payload = {
        "contract": _CONTRACT,
        "comment": comment,
        "entries": snapshots,
    }
    now = utc_now_db()
    values: dict[str, Any] = {
        "id": coverage_id,
        "project_id": project.id,
        "source_plan_id": source_plan.id,
        "owner_user_id": project.user_id,
        "kind": kind,
        "reviewer_kind": reviewer_kind,
        **_actor_values(actor),
        "payload_json": payload,
        "created_at": now,
    }
    scholarly_review._set_integrity_fields(
        TranslationReviewCoverage,
        values,
        hash_values=dict(values),
        receipt_purpose="translator-review-coverage-batch-v1",
        receipt_values=values,
    )
    row = scholarly_review._new(TranslationReviewCoverage, values)
    try:
        _persist_new_row(row)
    except IntegrityError as exc:
        if not _is_coverage_primary_key_conflict(exc):
            raise
        replay = db.session.get(TranslationReviewCoverage, coverage_id)
        if replay is None:
            raise _error("review-coverage-idempotency-retry") from exc
        replay_payload = _verify_row(replay)
        if not _matches_request(
            replay,
            replay_payload,
            project=project,
            source_plan=source_plan,
            kind=kind,
            reviewer_kind=reviewer_kind,
            comment=comment,
            entries=normalized,
            actor=actor,
        ):
            raise _error("review-coverage-idempotency-conflict") from exc
        return _receipt(replay, replay_payload)
    return _receipt(row, payload)


def _validate_payload(
    row: Any,
    payload: Mapping[str, Any],
    *,
    source_plan_id: str,
    owner_user_id: object,
    blocks_by_id: Mapping[str, Any],
) -> list[dict[str, Any]]:
    if (
        str(scholarly_review._get(row, "source_plan_id")) != source_plan_id
        or str(scholarly_review._get(row, "owner_user_id")) != str(owner_user_id)
        or scholarly_review._get(row, "kind") not in _KINDS
        or scholarly_review._get(row, "reviewer_kind") not in {"agent", "human"}
    ):
        raise _error("review-coverage-owner")
    entries = payload.get("entries")
    if not isinstance(entries, list) or not 1 <= len(entries) <= 100:
        raise _error("review-coverage-integrity")
    validated: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise _error("review-coverage-integrity")
        block_id = entry.get("block_id")
        if not isinstance(block_id, str) or block_id not in blocks_by_id:
            raise _error("review-coverage-integrity")
        if block_id in seen:
            raise _error("review-coverage-integrity")
        seen.add(block_id)
        expected_version = entry.get("expected_target_version")
        target_version = entry.get("target_version")
        target_revision_id = entry.get("target_revision_id")
        scope = entry.get("scope")
        selected_ids = entry.get("source_anchor_ids")
        anchor_snapshot = entry.get("source_anchor_snapshot")
        if (
            isinstance(expected_version, bool)
            or not isinstance(expected_version, int)
            or expected_version < 1
            or expected_version != target_version
            or isinstance(target_version, bool)
            or not isinstance(target_version, int)
            or target_version < 1
            or not isinstance(target_revision_id, str)
            or not target_revision_id
            or not isinstance(scope, str)
            or scope not in {"block", "anchors"}
            or not isinstance(selected_ids, list)
            or not isinstance(anchor_snapshot, list)
        ):
            raise _error("review-coverage-integrity")
        current_anchor_ids = _anchor_ids(blocks_by_id[block_id])
        if anchor_snapshot != current_anchor_ids:
            raise _error("review-coverage-integrity")
        if (
            any(not isinstance(item, str) for item in selected_ids)
            or len(selected_ids) != len(set(selected_ids))
            or (scope == "block" and selected_ids)
            or (
                scope == "anchors"
                and (
                    not selected_ids
                    or not set(selected_ids).issubset(current_anchor_ids)
                )
            )
        ):
            raise _error("review-coverage-integrity")
        validated.append(
            {
                "block_id": block_id,
                "target_revision_id": target_revision_id,
                "target_version": target_version,
                "scope": scope,
                "source_anchor_ids": list(selected_ids),
                "source_anchor_snapshot": list(anchor_snapshot),
            }
        )
    return validated


def _block_kind_coverage(
    block: Any,
    coverage_records: Sequence[Mapping[str, Any]],
    *,
    active: bool,
) -> dict[str, dict[str, Any]]:
    source_anchor_ids = _anchor_ids(block)
    by_kind: dict[str, dict[str, Any]] = {}
    for kind in _KINDS:
        relevant = [row for row in coverage_records if row["kind"] == kind]
        block_covered = any(row["scope"] == "block" for row in relevant)
        reviewed_ids = {
            anchor_id
            for row in relevant
            if row["scope"] == "anchors"
            for anchor_id in row["source_anchor_ids"]
        }
        has_anchor_scope = any(row["scope"] == "anchors" for row in relevant)
        complete = bool(
            active
            and (
                block_covered
                or (has_anchor_scope and set(source_anchor_ids).issubset(reviewed_ids))
            )
        )
        if block_covered:
            reviewed_ids = set(source_anchor_ids)
        reviewed_ordered = [
            anchor_id for anchor_id in source_anchor_ids if anchor_id in reviewed_ids
        ]
        by_kind[kind] = {
            "complete": complete,
            "coverage_ids": [row["coverage_id"] for row in relevant],
            "reviewed_source_anchor_ids": reviewed_ordered,
            "reviewed_source_anchor_count": len(reviewed_ordered),
        }
    return by_kind


def review_coverage_manifest(
    project: Any,
    *,
    block_ids: Sequence[str] | None = None,
    _workspace_plan: Any | None = None,
    _active_targets: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return verified current-target coverage, retaining stale rows in storage.

    Internal workspace callers may pass their freshly verified source-plan and
    active-target objects to avoid decrypting a document plan a second time.
    """

    if _workspace_plan is None:
        workspace, source_plan, blocks = scholarly_review._workspace(project)
    else:
        workspace = _workspace_plan
        source_plan = getattr(workspace, "record", None)
        plan = getattr(workspace, "plan", None)
        if (
            source_plan is None
            or plan is None
            or str(scholarly_review._get(source_plan, "project_id")) != str(project.id)
        ):
            raise _error("source-plan-context")
        blocks = list(getattr(plan, "blocks", ()))
    if block_ids is None:
        selected_blocks = list(blocks)
    else:
        selected_ids = scholarly_review._normalize_block_ids(block_ids, blocks)
        selected_set = set(selected_ids)
        selected_blocks = [block for block in blocks if str(block.id) in selected_set]
    if _active_targets is None:
        try:
            active_targets, _target_projections = _rehydrate_workspace_objects(
                workspace
            )
        except BlockWorkspaceIntegrityError as exc:
            raise _error(
                getattr(exc, "code", "review-coverage-target-invalid")
            ) from exc
    elif not isinstance(_active_targets, Mapping):
        raise _error("review-coverage-target-owner")
    else:
        active_targets = _active_targets
    blocks_by_id = scholarly_review._block_map(blocks)
    selected_ids = {str(block.id) for block in selected_blocks}
    active_by_block: dict[str, tuple[str, int]] = {}
    for block_id in selected_ids:
        pointer = active_targets.get(block_id)
        if pointer is not None:
            target_plan_id = scholarly_review._get(pointer, "source_plan_id")
            target_block_id = scholarly_review._get(pointer, "block_id")
            target_revision_id = scholarly_review._get(pointer, "revision_id")
            target_version = scholarly_review._get(pointer, "version")
            if (
                str(target_plan_id) != str(source_plan.id)
                or str(target_block_id) != block_id
                or not isinstance(target_revision_id, str)
                or not target_revision_id
                or isinstance(target_version, bool)
                or not isinstance(target_version, int)
                or target_version < 1
            ):
                raise _error("review-coverage-target-owner")
            active_by_block[block_id] = (
                target_revision_id,
                target_version,
            )

    rows = (
        TranslationReviewCoverage.query.filter_by(
            project_id=project.id,
            source_plan_id=source_plan.id,
        )
        .order_by(TranslationReviewCoverage.created_at, TranslationReviewCoverage.id)
        .all()
    )
    records_by_block: dict[str, list[dict[str, Any]]] = {
        block_id: [] for block_id in selected_ids
    }
    for row in rows:
        if str(row.owner_user_id) != str(project.user_id):
            raise _error("review-coverage-owner")
        payload = _verify_row(row)
        validated = _validate_payload(
            row,
            payload,
            source_plan_id=str(source_plan.id),
            owner_user_id=project.user_id,
            blocks_by_id=blocks_by_id,
        )
        for entry in validated:
            block_id = entry["block_id"]
            if block_id not in selected_ids:
                continue
            current = active_by_block.get(block_id)
            if current != (
                entry["target_revision_id"],
                entry["target_version"],
            ):
                continue
            records_by_block[block_id].append(
                {
                    "coverage_id": str(row.id),
                    "kind": str(row.kind),
                    "reviewer_kind": str(row.reviewer_kind),
                    "scope": entry["scope"],
                    "source_anchor_ids": list(entry["source_anchor_ids"]),
                }
            )

    manifest_blocks: list[dict[str, Any]] = []
    summary = {kind: {"complete": 0, "partial": 0, "missing": 0} for kind in _KINDS}
    for block in selected_blocks:
        block_id = str(block.id)
        active = active_by_block.get(block_id)
        coverage_records = records_by_block[block_id]
        by_kind = _block_kind_coverage(
            block, coverage_records, active=active is not None
        )
        for kind in _KINDS:
            value = by_kind[kind]
            state = (
                "complete"
                if value["complete"]
                else "partial"
                if value["coverage_ids"]
                else "missing"
            )
            summary[kind][state] += 1
        manifest_blocks.append(
            {
                "block_id": block_id,
                "target_revision_id": active[0] if active else None,
                "target_version": active[1] if active else None,
                "coverage": coverage_records,
                "kinds": by_kind,
            }
        )
    return {
        "contract": _CONTRACT,
        "project_id": str(project.id),
        "source_plan_id": str(source_plan.id),
        "source_plan_hash": str(workspace.plan.plan_hash),
        "blocks": manifest_blocks,
        "summary": summary,
    }


def snapshot_export_review(project: Any, requirement: str = "none") -> dict[str, Any]:
    """Capture target identities and verified coverage witnesses for export."""

    if not isinstance(requirement, str) or requirement not in {"none", *_KINDS}:
        raise _error("review-coverage-requirement")
    manifest = review_coverage_manifest(project)
    if requirement != "none" and any(
        not block["kinds"][requirement]["complete"] for block in manifest["blocks"]
    ):
        raise _error("review-coverage-required")
    return _export_snapshot(manifest, requirement)


def _export_snapshot(manifest: Mapping[str, Any], requirement: str) -> dict[str, Any]:
    snapshot = {
        "contract": _EXPORT_CONTRACT,
        "project_id": manifest["project_id"],
        "source_plan_id": manifest["source_plan_id"],
        "source_plan_hash": manifest["source_plan_hash"],
        "requirement": requirement,
        "targets": [
            {
                "block_id": block["block_id"],
                "target_revision_id": block["target_revision_id"],
                "target_version": block["target_version"],
            }
            for block in manifest["blocks"]
        ],
        "coverage_manifest": manifest,
    }
    snapshot["sha256"] = hashlib.sha256(
        scholarly_review._canonical_bytes(snapshot)
    ).hexdigest()
    return snapshot


def validate_export_review_snapshot(project: Any, snapshot: object) -> bool:
    """Revalidate frozen witnesses from a trusted server-created export checkpoint."""

    if (
        not isinstance(snapshot, Mapping)
        or snapshot.get("contract") != _EXPORT_CONTRACT
        or snapshot.get("project_id") != str(getattr(project, "id", ""))
    ):
        raise _error("review-coverage-export-snapshot")
    requirement = snapshot.get("requirement")
    if not isinstance(requirement, str) or requirement not in {"none", *_KINDS}:
        raise _error("review-coverage-export-snapshot")
    supplied_hash = snapshot.get("sha256")
    unsigned_snapshot = dict(snapshot)
    unsigned_snapshot.pop("sha256", None)
    try:
        computed_hash = hashlib.sha256(
            scholarly_review._canonical_bytes(unsigned_snapshot)
        ).hexdigest()
    except Exception as exc:
        raise _error("review-coverage-export-snapshot") from exc
    if not isinstance(supplied_hash, str) or supplied_hash != computed_hash:
        raise _error("review-coverage-export-snapshot")

    workspace, _source_plan, blocks = scholarly_review._workspace(project)
    current_manifest = review_coverage_manifest(project, _workspace_plan=workspace)
    frozen_manifest = _freeze_coverage_witnesses(
        snapshot.get("coverage_manifest"), current_manifest, blocks
    )
    current = _export_snapshot(frozen_manifest, requirement)
    if dict(snapshot) != current:
        raise _error("review-coverage-export-stale")
    if requirement != "none" and any(
        not block["kinds"][requirement]["complete"]
        for block in frozen_manifest["blocks"]
    ):
        raise _error("review-coverage-required")
    return True


def _freeze_coverage_witnesses(
    captured: object,
    current: Mapping[str, Any],
    blocks: Sequence[Any],
) -> dict[str, Any]:
    """Rebuild a snapshot manifest from only its captured verified witnesses."""

    if not isinstance(captured, Mapping) or not isinstance(
        captured.get("blocks"), list
    ):
        raise _error("review-coverage-export-snapshot")
    captured_by_id: dict[str, Mapping[str, Any]] = {}
    for block in captured["blocks"]:
        if not isinstance(block, Mapping):
            raise _error("review-coverage-export-snapshot")
        block_id = block.get("block_id")
        if not isinstance(block_id, str) or block_id in captured_by_id:
            raise _error("review-coverage-export-snapshot")
        captured_by_id[block_id] = block
    current_blocks = current.get("blocks")
    if not isinstance(current_blocks, list) or set(captured_by_id) != {
        block.get("block_id") for block in current_blocks if isinstance(block, Mapping)
    }:
        raise _error("review-coverage-export-stale")

    source_blocks = scholarly_review._block_map(blocks)
    summary = {kind: {"complete": 0, "partial": 0, "missing": 0} for kind in _KINDS}
    frozen_blocks: list[dict[str, Any]] = []
    for current_block in current_blocks:
        block_id = current_block.get("block_id")
        captured_block = captured_by_id[str(block_id)]
        source_block = source_blocks.get(str(block_id))
        if source_block is None:
            raise _error("review-coverage-export-stale")
        captured_records = captured_block.get("coverage")
        if not isinstance(captured_records, list):
            raise _error("review-coverage-export-snapshot")
        captured_ids: list[str] = []
        for record in captured_records:
            if not isinstance(record, Mapping):
                raise _error("review-coverage-export-snapshot")
            coverage_id = record.get("coverage_id")
            if not isinstance(coverage_id, str) or coverage_id in captured_ids:
                raise _error("review-coverage-export-snapshot")
            captured_ids.append(coverage_id)

        live_records = current_block.get("coverage")
        if not isinstance(live_records, list):
            raise _error("review-coverage-export-integrity")
        live_by_id = {
            record.get("coverage_id"): record
            for record in live_records
            if isinstance(record, Mapping)
        }
        if any(coverage_id not in live_by_id for coverage_id in captured_ids):
            raise _error("review-coverage-export-stale")
        frozen_records = [live_by_id[coverage_id] for coverage_id in captured_ids]
        if frozen_records != captured_records:
            raise _error("review-coverage-export-stale")

        kinds = _block_kind_coverage(
            source_block,
            frozen_records,
            active=current_block.get("target_revision_id") is not None,
        )
        for kind in _KINDS:
            value = kinds[kind]
            state = (
                "complete"
                if value["complete"]
                else "partial"
                if value["coverage_ids"]
                else "missing"
            )
            summary[kind][state] += 1
        frozen_blocks.append(
            {
                "block_id": block_id,
                "target_revision_id": current_block.get("target_revision_id"),
                "target_version": current_block.get("target_version"),
                "coverage": frozen_records,
                "kinds": kinds,
            }
        )

    return {
        "contract": current.get("contract"),
        "project_id": current.get("project_id"),
        "source_plan_id": current.get("source_plan_id"),
        "source_plan_hash": current.get("source_plan_hash"),
        "blocks": frozen_blocks,
        "summary": summary,
    }


def lock_export_review_snapshot(project: Any, snapshot: object) -> bool:
    """Fence all snapshotted active targets until the caller commits or rolls back.

    The caller owns the surrounding transaction and must hold it through export
    publication and operation completion. This function never commits.
    """

    if not isinstance(snapshot, Mapping) or snapshot.get("project_id") != str(
        getattr(project, "id", "")
    ):
        raise _error("review-coverage-export-snapshot")
    _workspace, source_plan, blocks = scholarly_review._workspace(project)
    if snapshot.get("source_plan_id") != str(source_plan.id):
        raise _error("review-export-stale")
    target_rows = snapshot.get("targets")
    if not isinstance(target_rows, list):
        raise _error("review-coverage-export-snapshot")
    targets_by_block: dict[str, Mapping[str, Any]] = {}
    source_block_ids = {str(block.id) for block in blocks}
    for target in target_rows:
        if not isinstance(target, Mapping):
            raise _error("review-coverage-export-snapshot")
        block_id = target.get("block_id")
        if (
            not isinstance(block_id, str)
            or block_id in targets_by_block
            or block_id not in source_block_ids
        ):
            raise _error("review-coverage-export-snapshot")
        targets_by_block[block_id] = target
    if set(targets_by_block) != {str(block.id) for block in blocks}:
        raise _error("review-coverage-export-snapshot")

    _ensure_database_transaction()
    for block_id, target in targets_by_block.items():
        revision_id = target.get("target_revision_id")
        version = target.get("target_version")
        if revision_id is None and version is None:
            continue
        if (
            not isinstance(revision_id, str)
            or not revision_id
            or isinstance(version, bool)
            or not isinstance(version, int)
            or version < 1
        ):
            raise _error("review-coverage-export-snapshot")
        rowcount = TranslationTargetBlockActive.query.filter_by(
            source_plan_id=source_plan.id,
            block_id=block_id,
            revision_id=revision_id,
            version=version,
        ).update(
            {
                TranslationTargetBlockActive.version: TranslationTargetBlockActive.version,
                TranslationTargetBlockActive.updated_at: TranslationTargetBlockActive.updated_at,
            },
            synchronize_session=False,
        )
        if rowcount != 1:
            raise _error("review-export-stale")

    return validate_export_review_snapshot(project, snapshot)


__all__ = [
    "lock_export_review_snapshot",
    "record_review_coverage",
    "review_coverage_manifest",
    "snapshot_export_review",
    "validate_export_review_snapshot",
]
