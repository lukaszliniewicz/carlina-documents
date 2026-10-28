"""Explicit source-to-target numbering projection for pinned block translations."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Mapping
from typing import Any

from sqlalchemy import update
from sqlalchemy.orm import selectinload

from carlina.core.semantic_numbering import (
    apply_numbering_overlay,
    merge_numbering_entries,
    numbering_candidates,
)
from carlina.core.utc import utc_now_db
from iarf_assistant.extensions import db

from .block_contract import (
    DOCUMENT_BLOCK_ENGINE_CONTRACT,
    AttestedDocumentSnapshot,
    BlockContractError,
    canonical_json,
)
from .block_persistence import (
    active_source_plan_pointer,
    rehydrate_source_plan,
    require_active_source_plan,
)
from .models import (
    TranslationNumberingOverlay,
    TranslationProject,
    TranslationSourcePlan,
    TranslationTargetAlignmentActive,
    TranslationTargetBlockActive,
)
from .operations import find_active_operation, reconcile_expired_active_operation

CONTRACT = "translator-numbering-sync-v1"
MAX_HISTORY = 100
MAX_RECEIPTS = 128
MAX_ENTRIES = 10_000
MAX_PAYLOAD_BYTES = 20 * 1024 * 1024
_KEY_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


class NumberingSyncError(ValueError):
    """A bounded failure that contains no source or target text."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _payload_bytes(value: Any) -> int:
    try:
        return len(canonical_json(value).encode("utf-8"))
    except (TypeError, ValueError, RecursionError) as exc:
        raise NumberingSyncError("numbering-overlay-corrupt") from exc


def _project(project: TranslationProject, owner_user_id: int) -> TranslationProject:
    if (
        not isinstance(project, TranslationProject)
        or not isinstance(owner_user_id, int)
        or isinstance(owner_user_id, bool)
    ):
        raise NumberingSyncError("numbering-project-invalid")
    with db.session.no_autoflush:
        current = db.session.get(TranslationProject, project.id, populate_existing=True)
    if current is None or current.user_id != owner_user_id:
        raise NumberingSyncError("numbering-owner-conflict")
    if current.engine_contract != DOCUMENT_BLOCK_ENGINE_CONTRACT:
        raise NumberingSyncError("numbering-engine-unsupported")
    return current


def _active_plan(
    project: TranslationProject, *, lock: bool
) -> tuple[TranslationSourcePlan, Any, int]:
    # The lock is acquired before reading any mutable witness in the mutation path.
    with db.session.no_autoflush:
        active = active_source_plan_pointer(project.id, lock=lock)
        if active is None:
            raise NumberingSyncError("numbering-source-plan-missing")
        record = db.session.get(
            TranslationSourcePlan,
            active.source_plan_id,
            populate_existing=True,
            options=(selectinload(TranslationSourcePlan.blocks),),
        )
    if record is None or record.project_id != project.id:
        raise NumberingSyncError("numbering-source-plan-conflict")
    try:
        plan = rehydrate_source_plan(record)
    except BlockContractError as exc:
        raise NumberingSyncError("numbering-source-plan-invalid") from exc
    return record, plan, active.version


def _payload(
    row: TranslationNumberingOverlay | None,
    record: TranslationSourcePlan,
    owner_user_id: int,
) -> dict[str, Any]:
    if row is None:
        return {"entries": [], "history": [], "receipts": []}
    if (
        row.project_id != record.project_id
        or row.owner_user_id != owner_user_id
        or type(row.revision) is not int
        or row.revision < 1
    ):
        raise NumberingSyncError("numbering-source-plan-conflict")
    try:
        value = row.payload
    except Exception as exc:
        raise NumberingSyncError("numbering-overlay-corrupt") from exc
    if (
        not isinstance(value, dict)
        or set(value) != {"entries", "history", "receipts"}
        or not isinstance(value["entries"], list)
        or not isinstance(value["history"], list)
        or not isinstance(value["receipts"], list)
        or len(value["history"]) > MAX_HISTORY
        or len(value["receipts"]) > MAX_RECEIPTS
        or len(value["entries"]) > MAX_ENTRIES
        or any(not isinstance(item, dict) for item in value["entries"])
        or any(not isinstance(item, dict) for item in value["history"])
        or any(not isinstance(item, dict) for item in value["receipts"])
        or any(
            not isinstance(item.get("key"), str)
            for item in value["receipts"]
            if isinstance(item, dict)
        )
        or len({item.get("key") for item in value["receipts"]})
        != len(value["receipts"])
    ):
        raise NumberingSyncError("numbering-overlay-corrupt")
    if _payload_bytes(value) > MAX_PAYLOAD_BYTES:
        raise NumberingSyncError("numbering-overlay-corrupt")
    if (
        row.source_plan_id != record.id or row.source_plan_hash != record.plan_hash
    ) and value["entries"]:
        raise NumberingSyncError("numbering-source-plan-conflict")
    return value


def _row(project_id: str) -> TranslationNumberingOverlay | None:
    with db.session.no_autoflush:
        return db.session.get(
            TranslationNumberingOverlay, project_id, populate_existing=True
        )


def _undo_stack(
    payload: dict[str, Any], record: TranslationSourcePlan
) -> list[dict[str, Any]]:
    """Reconstruct the current plan's LIFO applies from its contiguous history."""
    history = payload["history"]
    start = len(history)
    while start and history[start - 1].get("source_plan_id") == record.id:
        start -= 1
    stack: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []
    for event in history[start:]:
        if (
            event.get("source_plan_hash") != record.plan_hash
            or type(event.get("revision")) is not int
            or event["revision"] < 1
        ):
            raise NumberingSyncError("numbering-overlay-corrupt")
        action = event.get("action")
        if action == "apply":
            previous = event.get("previous_entries")
            resulting = event.get("new_entries")
            if (
                not isinstance(previous, list)
                or not isinstance(resulting, list)
                or previous != current
                or event.get("undo_revision") is not None
                or resulting == previous
            ):
                raise NumberingSyncError("numbering-overlay-corrupt")
            stack.append(event)
            current = resulting
        elif action == "undo":
            if (
                not stack
                or event.get("undo_revision") != stack[-1]["revision"]
                or event.get("previous_entries_hash") != _digest(current)
                or event.get("new_entries_hash")
                != _digest(stack[-1]["previous_entries"])
            ):
                raise NumberingSyncError("numbering-overlay-corrupt")
            current = stack.pop()["previous_entries"]
        else:
            raise NumberingSyncError("numbering-overlay-corrupt")
    if current != payload["entries"]:
        raise NumberingSyncError("numbering-overlay-corrupt")
    return stack


def _undo_history_event(
    *,
    record: TranslationSourcePlan,
    revision: int,
    undo_revision: int,
    previous_entries: list[dict[str, Any]],
    new_entries: list[dict[str, Any]],
    key: str,
    fingerprint: str,
    result: dict[str, Any],
) -> dict[str, Any]:
    return {
        "action": "undo",
        "source_plan_id": record.id,
        "source_plan_hash": record.plan_hash,
        "revision": revision,
        "undo_revision": undo_revision,
        "previous_entries_hash": _digest(previous_entries),
        "new_entries_hash": _digest(new_entries),
        "idempotency_key": key,
        "request_fingerprint": fingerprint,
        "result": result,
    }


def _reserve_undo_capacity(
    payload: dict[str, Any], record: TranslationSourcePlan
) -> None:
    """Prove every current apply can be undone within all durable bounds."""
    simulated = {
        "entries": list(payload["entries"]),
        "history": list(payload["history"]),
        "receipts": list(payload["receipts"]),
    }
    stack = _undo_stack(payload, record)
    for apply_event in reversed(stack):
        previous = simulated["entries"]
        restored = apply_event["previous_entries"]
        # 19 decimal digits upper-bound a signed SQLite integer. The digest
        # placeholders and longest legal key bound an eventual receipt even
        # when the real caller uses shorter values.
        revision = 9_223_372_036_854_775_807
        key = "x" * 128
        result = {
            "contract": CONTRACT,
            "revision": revision,
            "source_plan_id": record.id,
            "source_revision": "f" * 64,
            "target_revision": "f" * 64,
            "can_undo": False,
        }
        event = _undo_history_event(
            record=record,
            revision=revision,
            undo_revision=apply_event["revision"],
            previous_entries=previous,
            new_entries=restored,
            key=key,
            fingerprint="f" * 64,
            result=result,
        )
        simulated["entries"] = restored
        simulated["history"].append(event)
        simulated["receipts"].append(
            {"key": key, "fingerprint": "f" * 64, "result": result}
        )
        if (
            len(simulated["history"]) > MAX_HISTORY
            or len(simulated["receipts"]) > MAX_RECEIPTS
            or _payload_bytes(simulated) > MAX_PAYLOAD_BYTES
        ):
            raise NumberingSyncError("numbering-undo-capacity")


def numbering_state(source_plan_record: TranslationSourcePlan) -> dict[str, Any]:
    project = source_plan_record.project
    if project is None or source_plan_record.project_id != project.id:
        raise NumberingSyncError("numbering-project-invalid")
    row = _row(project.id)
    payload = _payload(row, source_plan_record, project.user_id)
    entries = (
        list(payload["entries"])
        if row is None or row.source_plan_id == source_plan_record.id
        else []
    )
    return {"revision": row.revision if row else 0, "entries": entries}


def numbering_revision(project_id: str) -> int:
    """Read the monotonic projection revision (zero before the first edit)."""
    row = _row(project_id)
    return row.revision if row is not None else 0


def require_no_numbering_overlay(project_id: str) -> None:
    """Fence a source-plan transition while a nonempty numbering projection exists."""
    row = _row(project_id)
    if row is not None and isinstance(row.payload, dict) and row.payload.get("entries"):
        raise NumberingSyncError("numbering-source-plan-conflict")
    if row is not None and not isinstance(row.payload, dict):
        raise NumberingSyncError("numbering-overlay-corrupt")


def project_source_numbering(source_plan_record: TranslationSourcePlan) -> str:
    plan = rehydrate_source_plan(source_plan_record)
    state = numbering_state(source_plan_record)
    html = plan.snapshot.output_html
    if not state["entries"]:
        return html
    return apply_numbering_overlay(html, html, state["entries"])


def _upstream(
    load_current_source: Callable[[], dict[str, Any]], document_id: str
) -> AttestedDocumentSnapshot:
    if not callable(load_current_source):
        raise NumberingSyncError("numbering-source-loader")
    try:
        snapshot = AttestedDocumentSnapshot.from_dict(load_current_source())
    except (BlockContractError, TypeError, ValueError, KeyError) as exc:
        raise NumberingSyncError("numbering-upstream-invalid") from exc
    if snapshot.document_id != document_id or snapshot.id != document_id:
        raise NumberingSyncError("numbering-upstream-conflict")
    return snapshot


def _target_digest(record: TranslationSourcePlan) -> str:
    with db.session.no_autoflush:
        targets = (
            db.session.query(TranslationTargetBlockActive)
            .populate_existing()
            .filter_by(source_plan_id=record.id)
            .order_by(TranslationTargetBlockActive.block_id)
            .all()
        )
        alignments = (
            db.session.query(TranslationTargetAlignmentActive)
            .populate_existing()
            .filter_by(source_plan_id=record.id)
            .order_by(TranslationTargetAlignmentActive.block_id)
            .all()
        )
    target_rows = [
        [p.block_id, p.revision_id, p.version, p.pair_receipt] for p in targets
    ]
    alignment_rows = [
        [p.block_id, p.revision_id, p.alignment_revision_id, p.version]
        for p in alignments
    ]
    if [row[0] for row in target_rows] != [row[0] for row in alignment_rows]:
        raise NumberingSyncError("numbering-target-conflict")
    for target, alignment in zip(targets, alignments, strict=True):
        if (
            target.revision_id != alignment.revision_id
            or target.version != alignment.version
        ):
            raise NumberingSyncError("numbering-target-conflict")
    return _digest([target_rows, alignment_rows])


def _change_blocks(change: Mapping[str, Any], plan: Any) -> list[str]:
    start, end = change.get("root_start"), change.get("root_end")
    if type(start) is not int or type(end) is not int or start >= end:
        return []
    return [
        block.id
        for block in plan.blocks
        if block.source_start < end and block.source_end > start
    ]


def _preview(
    project: TranslationProject,
    record: TranslationSourcePlan,
    plan: Any,
    active_version: int,
    payload: dict[str, Any],
    revision: int,
    load_current_source: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    upstream = _upstream(load_current_source, plan.snapshot.document_id)
    if payload["entries"]:
        try:
            apply_numbering_overlay(
                plan.snapshot.output_html,
                plan.snapshot.output_html,
                payload["entries"],
            )
        except ValueError as exc:
            raise NumberingSyncError("numbering-overlay-corrupt") from exc
    target_digest = _target_digest(record)
    candidate_report = numbering_candidates(
        plan.snapshot.output_html, upstream.output_html, payload["entries"]
    )
    changes = []
    for change in candidate_report["changes"]:
        item = dict(change)
        item["affected_block_ids"] = _change_blocks(item, plan)
        changes.append(item)
    conflicts = list(candidate_report["conflicts"])
    token = _digest(
        {
            "contract": CONTRACT,
            "owner": project.user_id,
            "project": project.id,
            "source_plan_id": record.id,
            "source_plan_hash": record.plan_hash,
            "active_version": active_version,
            "upstream_hash": upstream.content_hash,
            "target_digest": target_digest,
            "overlay_revision": revision,
            "changes": changes,
            "conflicts": conflicts,
        }
    )
    return {
        "contract": CONTRACT,
        "revision": revision,
        "preview_token": token,
        "source_plan_id": record.id,
        "source_revision": record.source_content_hash,
        "target_revision": target_digest,
        "changes": changes,
        "conflicts": conflicts,
        "can_undo": bool(_undo_stack(payload, record)),
    }


def preview_numbering_sync(
    project: TranslationProject,
    *,
    owner_user_id: int,
    load_current_source: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    current = _project(project, owner_user_id)
    record, plan, version = _active_plan(current, lock=False)
    row = _row(current.id)
    payload = _payload(row, record, owner_user_id)
    return _preview(
        current,
        record,
        plan,
        version,
        payload,
        row.revision if row else 0,
        load_current_source,
    )


def _key(value: str) -> str:
    if not isinstance(value, str) or not _KEY_RE.fullmatch(value):
        raise NumberingSyncError("numbering-idempotency-key")
    return value


def _receipt(
    payload: dict[str, Any], key: str, fingerprint: str
) -> dict[str, Any] | None:
    for receipt in payload["receipts"]:
        if not isinstance(receipt, dict) or receipt.get("key") != key:
            continue
        if receipt.get("fingerprint") != fingerprint:
            raise NumberingSyncError("numbering-idempotency-conflict")
        result = receipt.get("result")
        if not isinstance(result, dict):
            raise NumberingSyncError("numbering-overlay-corrupt")
        return dict(result)
    return None


def _mutate(
    project: TranslationProject,
    *,
    owner_user_id: int,
    load_current_source: Callable[[], dict[str, Any]],
    preview_token: str,
    idempotency_key: str,
    action: str,
    selected_ids: list[str] | None,
) -> dict[str, Any]:
    if not isinstance(preview_token, str) or not re.fullmatch(
        r"[0-9a-f]{64}", preview_token
    ):
        raise NumberingSyncError("numbering-preview-token")
    key = _key(idempotency_key)
    if action == "apply" and (
        not isinstance(selected_ids, list)
        or not selected_ids
        or any(not isinstance(item, str) or not item for item in selected_ids)
        or len(set(selected_ids)) != len(selected_ids)
    ):
        raise NumberingSyncError("numbering-selection-invalid")
    normalized_ids = sorted(selected_ids) if selected_ids is not None else None
    fingerprint = _digest([CONTRACT, action, preview_token, normalized_ids])
    try:
        # Acquiring this SQLite writer fence before mutable reads serializes
        # independent request sessions with source-plan and target writers.
        if not isinstance(project, TranslationProject) or not isinstance(
            project.id, str
        ):
            raise NumberingSyncError("numbering-project-invalid")
        pointer = active_source_plan_pointer(project.id, lock=True)
        if pointer is None:
            raise NumberingSyncError("numbering-source-plan-missing")
        require_active_source_plan(project.id, pointer.source_plan_id, lock=True)
        current = _project(project, owner_user_id)
        record, plan, version = _active_plan(current, lock=False)
        row = _row(current.id)
        payload = _payload(row, record, owner_user_id)
        replay = _receipt(payload, key, fingerprint)
        if replay is not None:
            db.session.rollback()
            return replay
        if (
            len(payload["history"]) >= MAX_HISTORY
            or len(payload["receipts"]) >= MAX_RECEIPTS
        ):
            raise NumberingSyncError("numbering-history-full")
        if (
            reconcile_expired_active_operation(current.id, owner_user_id) is not None
            or find_active_operation(current.id) is not None
        ):
            raise NumberingSyncError("numbering-operation-active")
        old_revision = row.revision if row else 0
        report = _preview(
            current, record, plan, version, payload, old_revision, load_current_source
        )
        if report["preview_token"] != preview_token:
            raise NumberingSyncError("numbering-preview-stale")
        previous = list(payload["entries"])
        stack = _undo_stack(payload, record)
        undo_revision: int | None = None
        if action == "apply":
            by_id = {change["id"]: change for change in report["changes"]}
            if len(by_id) != len(report["changes"]) or any(
                item not in by_id for item in selected_ids or []
            ):
                raise NumberingSyncError("numbering-selection-conflict")
            selected = [
                {
                    key: value
                    for key, value in by_id[item].items()
                    if key != "affected_block_ids"
                }
                for item in normalized_ids or []
            ]
            entries = merge_numbering_entries(previous, selected)
        else:
            if not stack:
                raise NumberingSyncError("numbering-undo-unavailable")
            last = stack[-1]
            entries = list(last["previous_entries"])
            undo_revision = last["revision"]
        if not isinstance(entries, list) or entries == previous:
            raise NumberingSyncError("numbering-no-change")
        new_revision = old_revision + 1
        result = {
            "contract": CONTRACT,
            "revision": new_revision,
            "source_plan_id": record.id,
            "source_revision": record.source_content_hash,
            "target_revision": report["target_revision"],
            "can_undo": action == "apply" or len(stack) > 1,
        }
        if action == "undo":
            assert undo_revision is not None
            event = _undo_history_event(
                record=record,
                revision=new_revision,
                undo_revision=undo_revision,
                previous_entries=previous,
                new_entries=entries,
                key=key,
                fingerprint=fingerprint,
                result=result,
            )
        else:
            event = {
                "action": "apply",
                "source_plan_id": record.id,
                "source_plan_hash": record.plan_hash,
                "previous_entries": previous,
                "new_entries": entries,
                "revision": new_revision,
                "undo_revision": None,
                "idempotency_key": key,
                "request_fingerprint": fingerprint,
                "result": result,
            }
        next_payload = {
            "entries": entries,
            "history": [*payload["history"], event],
            "receipts": [
                *payload["receipts"],
                {"key": key, "fingerprint": fingerprint, "result": result},
            ],
        }
        if (
            len(entries) > MAX_ENTRIES
            or _payload_bytes(next_payload) > MAX_PAYLOAD_BYTES
        ):
            raise NumberingSyncError("numbering-overlay-full")
        if action == "apply":
            _reserve_undo_capacity(next_payload, record)
        if row is None:
            db.session.add(
                TranslationNumberingOverlay(
                    project_id=current.id,
                    owner_user_id=owner_user_id,
                    source_plan_id=record.id,
                    source_plan_hash=record.plan_hash,
                    revision=new_revision,
                    payload=next_payload,
                    updated_at=utc_now_db(),
                )
            )
        else:
            changed = db.session.execute(
                update(TranslationNumberingOverlay)
                .where(
                    TranslationNumberingOverlay.project_id == current.id,
                    TranslationNumberingOverlay.revision == old_revision,
                )
                .values(
                    source_plan_id=record.id,
                    source_plan_hash=record.plan_hash,
                    revision=new_revision,
                    payload=next_payload,
                    updated_at=utc_now_db(),
                )
            )
            if changed.rowcount != 1:
                raise NumberingSyncError("numbering-overlay-stale")
        db.session.commit()
        return result
    except Exception:
        db.session.rollback()
        raise


def apply_numbering_sync(
    project: TranslationProject,
    *,
    owner_user_id: int,
    load_current_source: Callable[[], dict[str, Any]],
    preview_token: str,
    selected_ids: list[str],
    idempotency_key: str,
) -> dict[str, Any]:
    return _mutate(
        project,
        owner_user_id=owner_user_id,
        load_current_source=load_current_source,
        preview_token=preview_token,
        selected_ids=selected_ids,
        idempotency_key=idempotency_key,
        action="apply",
    )


def undo_numbering_sync(
    project: TranslationProject,
    *,
    owner_user_id: int,
    load_current_source: Callable[[], dict[str, Any]],
    preview_token: str,
    idempotency_key: str,
) -> dict[str, Any]:
    return _mutate(
        project,
        owner_user_id=owner_user_id,
        load_current_source=load_current_source,
        preview_token=preview_token,
        selected_ids=None,
        idempotency_key=idempotency_key,
        action="undo",
    )
