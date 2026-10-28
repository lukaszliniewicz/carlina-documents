"""Durable, owner-fenced recovery for passive Document Processor PDF jobs."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any

from sqlalchemy import func, update

from carlina.core.utc import utc_now_db

from ...core.mcp_jobs import (
    McpWorkError,
    _release_suspended_execution_work,
    serialize_execution,
)
from ...core.upload_paths import resolve_upload_reference
from ...extensions import db
from ...models import McpExecution, McpWorkItem, User
from .models import ProcessedDocument
from .parsers.pdf_render_pool import _SAFE_CHILD_EXCEPTION_NAMES

_CONFIG_KEYS = frozenset(
    {
        "engine_id",
        "requested_engine_id",
        "processing_profile",
        "parsing_model",
        "metadata_model",
        "pdf_options",
        "engine_options",
        "crop_box",
        "crop_boxes",
        "reasoning_snapshot",
        "mcp_batch_plan",
        "mcp_page_batch_protocol",
        "mcp_page_blank_policy",
        "mcp_image_transport",
        "mcp_batch_feedback",
        "mcp_metadata_policy",
        "mcp_note_arrangement_policy",
        "mcp_cover_candidate_policy",
        "mcp_semantic_structure_policy",
        "mcp_image_accessibility_policy",
        "mcp_extraction_issues_policy",
        "mcp_execution_mode",
        "mcp_document_context",
        "document_metadata",
    }
)
_ACTIVE = frozenset({"queued", "running", "waiting_external"})
_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_DIAGNOSTIC = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_MAX_RECEIPTS = 128
_CHUNK = 1024 * 1024


def settings_fingerprint(settings: dict[str, Any]) -> str:
    """Hash only frozen effective configuration, excluding parser-owned state."""

    selected = {key: settings[key] for key in sorted(_CONFIG_KEYS) if key in settings}
    encoded = json.dumps(
        selected,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _source_proof(source: Path) -> dict[str, Any]:
    try:
        if source.is_symlink() or not source.is_file():
            raise OSError("Source is not a regular file")
        before = source.stat()
        digest = hashlib.sha256()
        size = 0
        with source.open("rb") as handle:
            while chunk := handle.read(_CHUNK):
                digest.update(chunk)
                size += len(chunk)
        after = source.stat()
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ) or size != after.st_size:
            raise OSError("Source changed during inspection")
    except (OSError, ValueError) as exc:
        raise McpWorkError(
            "source_unavailable", "The document source is unavailable.", status_code=409
        ) from exc
    return {"sha256": digest.hexdigest(), "size": size}


def freeze_recovery_contract(
    document: ProcessedDocument, source: Path, settings: dict[str, Any]
) -> dict[str, Any]:
    """Freeze the original source and initialize one bounded recovery ledger."""

    if not document.id or not isinstance(settings, dict):
        raise ValueError("A persisted document and JSON settings are required.")
    owner = db.session.get(User, document.user_id)
    if owner is None:
        raise McpWorkError(
            "owner_not_found", "The document owner was not found.", status_code=404
        )
    addon_settings = owner.addon_settings or {}
    prompt_settings = (
        addon_settings.get("doc_processor", {})
        if isinstance(addon_settings, Mapping)
        else {}
    )
    if not isinstance(prompt_settings, dict):
        prompt_settings = {}
    return {
        "schema_version": 1,
        "source": _source_proof(source),
        "prompt_settings": deepcopy(prompt_settings),
        "generation": 0,
        "attempts": [],
    }


def _contract(execution: McpExecution) -> dict[str, Any] | None:
    request = execution.request_json
    recovery = request.get("recovery") if isinstance(request, Mapping) else None
    if not isinstance(recovery, dict) or recovery.get("schema_version") != 1:
        return None
    source = recovery.get("source")
    if (
        not isinstance(source, dict)
        or not isinstance(source.get("sha256"), str)
        or type(source.get("size")) is not int
    ):
        return None
    return recovery


def _matches_source(contract: Mapping[str, Any], source: Path) -> None:
    if not isinstance(source, Path):
        raise McpWorkError(
            "source_unavailable", "The document source is unavailable.", status_code=409
        )
    if _source_proof(source) != contract["source"]:
        raise McpWorkError(
            "recovery_source_changed",
            "The document source changed after this execution began.",
            status_code=409,
        )


def _safe_reason(reason: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key in ("error_code", "phase"):
        value = reason.get(key)
        if isinstance(value, str):
            normalized = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", value).lower()
            if _DIAGNOSTIC.fullmatch(normalized):
                result[key] = normalized
    value = reason.get("returncode")
    if type(value) is int and -99999 <= value <= 99999:
        result["returncode"] = value
    attempts = reason.get("attempts")
    if type(attempts) is int and 1 <= attempts <= 3:
        result["attempts"] = attempts
    error_number = reason.get("errno")
    if type(error_number) is int and 1 <= error_number <= 4095:
        result["errno"] = error_number
    batch = reason.get("batch_id")
    if isinstance(batch, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", batch):
        result["batch_id"] = batch
    page = reason.get("page")
    if type(page) is int and 1 <= page <= 1_000_000:
        result["page"] = page
    previous_page = reason.get("previous_page")
    if type(previous_page) is int and 1 <= previous_page <= 1_000_000:
        result["previous_page"] = previous_page
    if reason.get("section") in {"main_content", "footnotes"}:
        result["section"] = reason["section"]
    stderr_bytes = reason.get("stderr_bytes")
    if type(stderr_bytes) is int and stderr_bytes >= 0:
        result["stderr_bytes"] = min(stderr_bytes, 1_000_000_000)
    stderr_exception = reason.get("stderr_exception")
    if (
        isinstance(stderr_exception, str)
        and stderr_exception in _SAFE_CHILD_EXCEPTION_NAMES
    ):
        result["stderr_exception"] = stderr_exception
    if "error_code" not in result:
        result["error_code"] = "processing_interrupted"
    return result


def _bound(execution: McpExecution, document: ProcessedDocument) -> bool:
    settings = document.settings or {}
    return (
        execution.owner_user_id == document.user_id
        and execution.addon_id == "doc_processor"
        and execution.resource_kind == "document"
        and execution.resource_id == document.id
        and execution.mode == "passive"
        and document.filename.lower().endswith(".pdf")
        and settings.get("mcp_execution_id") == execution.id
    )


def validate_delivery(
    document: ProcessedDocument,
    payload: Mapping[str, Any],
    job_id: str,
    source: Path | None,
) -> bool:
    """Reject a stale queue delivery before it can publish progress or output."""

    execution_id = str(payload.get("mcp_execution_id") or "")
    if not execution_id:
        return True
    execution = db.session.get(McpExecution, execution_id)
    if execution is None:
        return False
    if _contract(execution) is None:
        return True  # Historical, unfrozen executions retain their former path.
    settings = document.settings or {}
    if not (
        _bound(execution, document)
        and payload.get("document_id") == document.id
        and payload.get("user_id") == document.user_id
        and document.status == "processing"
        and execution.status in _ACTIVE
        and execution.queue_job_id == str(job_id)
        and settings.get("active_job_id") == str(job_id)
    ):
        return False
    if source is not None:
        _matches_source(_contract(execution), source)
    return True


def suspend_execution(
    document_id: str, execution_id: str, job_id: str, reason: dict[str, Any]
) -> bool:
    """Atomically suspend the current passive delivery after caller rollback."""

    execution = db.session.get(McpExecution, str(execution_id), populate_existing=True)
    document = db.session.get(
        ProcessedDocument, str(document_id), populate_existing=True
    )
    if execution is None or document is None or not _bound(execution, document):
        return False
    contract = _contract(execution)
    settings = dict(document.settings or {})
    if (
        contract is None
        or execution.status not in _ACTIVE
        or execution.queue_job_id != str(job_id)
        or document.status != "processing"
        or settings.get("active_job_id") != str(job_id)
    ):
        return False
    generation = int(contract.get("generation", 0)) + 1
    recovery = {
        **contract,
        "generation": generation,
        "suspended_settings_sha256": settings_fingerprint(settings),
    }
    marker = {"generation": generation, "reason": _safe_reason(reason)}
    settings.pop("active_job_id", None)
    settings["mcp_recovery"] = marker
    now = utc_now_db()
    try:
        with db.session.no_autoflush:
            changed_execution = db.session.execute(
                update(McpExecution)
                .where(
                    McpExecution.id == execution.id,
                    McpExecution.owner_user_id == document.user_id,
                    McpExecution.status == execution.status,
                    McpExecution.updated_at == execution.updated_at,
                    McpExecution.queue_job_id == str(job_id),
                )
                .values(
                    status="suspended",
                    request_json={**execution.request_json, "recovery": recovery},
                    updated_at=now,
                )
                .execution_options(synchronize_session=False)
            ).rowcount
            if changed_execution != 1:
                db.session.rollback()
                return False
            changed_document = db.session.execute(
                update(ProcessedDocument)
                .where(
                    ProcessedDocument.id == document.id,
                    ProcessedDocument.user_id == execution.owner_user_id,
                    ProcessedDocument.status == "processing",
                    ProcessedDocument.updated_at == document.updated_at,
                    ProcessedDocument.settings["active_job_id"].as_string()
                    == str(job_id),
                    ProcessedDocument.settings["mcp_execution_id"].as_string()
                    == execution.id,
                )
                .values(status="suspended", settings=settings, updated_at=now)
                .execution_options(synchronize_session=False)
            ).rowcount
            if changed_document != 1:
                db.session.rollback()
                return False
            _release_suspended_execution_work(execution.id, now)
        db.session.commit()
        return True
    except Exception:
        db.session.rollback()
        raise


def _resume_error(code: str, detail: str) -> McpWorkError:
    return McpWorkError(code, detail, status_code=409)


def _receipt_result(
    execution: McpExecution, receipt: Mapping[str, Any], *, resumed: bool
) -> dict[str, Any]:
    return {
        "execution": serialize_execution(execution),
        "queue_job_id": receipt["job_id"],
        "resumed": resumed,
        "idempotent": not resumed,
    }


def _historical_result(
    service: Any, execution: McpExecution, receipt: Mapping[str, Any]
) -> dict[str, Any]:
    result = _receipt_result(execution, receipt, resumed=False)
    if execution.status == "queued" and execution.queue_job_id == receipt["job_id"]:
        reconcile_resume_dispatch(service, execution)
    return result


def resume_execution(
    service: Any, execution_id: str, idempotency_key: str
) -> dict[str, Any]:
    """Reserve a new attempt durably, then idempotently dispatch its queue job."""

    if not isinstance(idempotency_key, str) or not _KEY.fullmatch(idempotency_key):
        raise McpWorkError(
            "invalid_idempotency_key", "A bounded idempotency key is required."
        )
    execution = db.session.get(McpExecution, str(execution_id), populate_existing=True)
    if (
        execution is None
        or execution.owner_user_id != service.owner_user_id
        or execution.addon_id != "doc_processor"
        or execution.resource_kind != "document"
        or execution.mode != "passive"
    ):
        raise McpWorkError(
            "execution_not_found", "The execution was not found.", status_code=404
        )
    contract = _contract(execution)
    if contract is None:
        raise _resume_error(
            "recovery_unavailable", "This execution has no frozen recovery contract."
        )
    key_hash = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
    receipts = contract.get("attempts") or []
    prior = next((item for item in receipts if item.get("key_hash") == key_hash), None)
    if prior is not None:
        return _historical_result(service, execution, prior)
    try:
        document = service._document(execution.resource_id, fresh=True)
    except RuntimeError as exc:
        raise McpWorkError(
            "document_not_found", "The document was not found.", status_code=404
        ) from exc
    if not _bound(execution, document):
        raise _resume_error(
            "execution_conflict", "The document is bound to another execution."
        )
    if execution.status != "suspended" or document.status != "suspended":
        db.session.refresh(execution)
        refreshed_contract = _contract(execution)
        refreshed_prior = (
            next(
                (
                    item
                    for item in refreshed_contract.get("attempts", [])
                    if item.get("key_hash") == key_hash
                ),
                None,
            )
            if refreshed_contract
            else None
        )
        if refreshed_prior is not None:
            return _historical_result(service, execution, refreshed_prior)
        raise _resume_error(
            "execution_not_suspended", "Only a suspended execution can be resumed."
        )
    newer_active = (
        McpExecution.query.filter(
            McpExecution.owner_user_id == service.owner_user_id,
            McpExecution.addon_id == "doc_processor",
            McpExecution.resource_kind == "document",
            McpExecution.resource_id == document.id,
            McpExecution.id != execution.id,
            McpExecution.created_at > execution.created_at,
            McpExecution.status.in_(_ACTIVE),
        )
        .with_entities(McpExecution.id)
        .first()
    )
    if newer_active is not None:
        raise _resume_error(
            "newer_execution_active", "A newer execution is active for this document."
        )
    settings = dict(document.settings or {})
    marker = settings.get("mcp_recovery")
    generation = contract.get("generation")
    if not isinstance(marker, dict) or marker.get("generation") != generation:
        raise _resume_error("recovery_conflict", "The suspension marker changed.")
    diagnostic = marker.get("reason")
    if (
        isinstance(diagnostic, Mapping)
        and diagnostic.get("error_code") == "recovery_replay_conflict"
    ):
        raise _resume_error(
            "recovery_replay_conflict", "The frozen packet replay requires inspection."
        )
    if isinstance(diagnostic, Mapping) and diagnostic.get("error_code") in {
        "list_continuation_style_conflict", "list_continuation_ordinal_conflict"
    }:
        raise _resume_error(
            "assembly_repair_required",
            "Inspect retained pages with carlina_document_recover before repairing this assembly failure.",
        )
    if settings_fingerprint(settings) != contract.get("suspended_settings_sha256"):
        raise _resume_error(
            "recovery_settings_changed", "Processing settings changed after suspension."
        )
    try:
        source = service._stored_source(document)
    except Exception as exc:
        raise _resume_error(
            "source_unavailable", "The document source is unavailable."
        ) from exc
    _matches_source(contract, source)
    if len(receipts) >= _MAX_RECEIPTS:
        raise _resume_error(
            "recovery_receipts_full", "The recovery receipt limit was reached."
        )
    job_id = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"carlina:doc-recovery:{execution.id}:{generation}:{key_hash}",
        )
    )
    payload = {
        "document_id": document.id,
        "user_id": service.owner_user_id,
        "mcp_execution_id": execution.id,
        "mcp_execution_mode": "passive",
        "engine_id": settings.get("engine_id"),
    }
    receipt = {
        "key_hash": key_hash,
        "generation": generation,
        "job_id": job_id,
        "payload": payload,
    }
    next_contract = {**contract, "attempts": [*receipts, receipt]}
    settings["active_job_id"] = job_id
    settings.pop("mcp_recovery", None)
    now = utc_now_db()
    try:
        with db.session.no_autoflush:
            changed_execution = db.session.execute(
                update(McpExecution)
                .where(
                    McpExecution.id == execution.id,
                    McpExecution.owner_user_id == service.owner_user_id,
                    McpExecution.status == "suspended",
                    McpExecution.updated_at == execution.updated_at,
                )
                .values(
                    status="queued",
                    queue_job_id=job_id,
                    request_json={**execution.request_json, "recovery": next_contract},
                    updated_at=now,
                )
                .execution_options(synchronize_session=False)
            ).rowcount
            if changed_execution != 1:
                raise _resume_error(
                    "execution_conflict", "The execution changed concurrently."
                )
            changed_document = db.session.execute(
                update(ProcessedDocument)
                .where(
                    ProcessedDocument.id == document.id,
                    ProcessedDocument.user_id == service.owner_user_id,
                    ProcessedDocument.status == "suspended",
                    ProcessedDocument.updated_at == document.updated_at,
                    ProcessedDocument.settings["mcp_execution_id"].as_string()
                    == execution.id,
                    ProcessedDocument.settings["mcp_recovery"][
                        "generation"
                    ].as_integer()
                    == generation,
                )
                .values(status="processing", settings=settings, updated_at=now)
                .execution_options(synchronize_session=False)
            ).rowcount
            if changed_document != 1:
                raise _resume_error(
                    "document_conflict", "The document changed concurrently."
                )
        db.session.commit()
    except McpWorkError as exc:
        db.session.rollback()
        if exc.code in {"execution_conflict", "document_conflict"}:
            winner = db.session.get(McpExecution, execution.id, populate_existing=True)
            winner_contract = _contract(winner) if winner is not None else None
            winning_receipt = (
                next(
                    (
                        item
                        for item in winner_contract.get("attempts", [])
                        if item.get("key_hash") == key_hash
                    ),
                    None,
                )
                if winner_contract
                else None
            )
            if winning_receipt is not None:
                return _historical_result(service, winner, winning_receipt)
        raise
    except Exception:
        db.session.rollback()
        raise
    db.session.refresh(execution)
    result = _receipt_result(execution, receipt, resumed=True)
    _dispatch(service, receipt)
    return result


def _dispatch(service: Any, receipt: Mapping[str, Any]) -> None:
    try:
        result = service._queue().enqueue_once(
            "process_document", receipt["payload"], job_id=receipt["job_id"]
        )
        if result != receipt["job_id"]:
            raise RuntimeError("Queue returned another job id")
    except Exception as exc:
        raise McpWorkError(
            "resume_dispatch_pending",
            "The resume is reserved; retry with the same idempotency key.",
            status_code=503,
            retryable=True,
        ) from exc


def reconcile_resume_dispatch(service: Any, execution: McpExecution) -> bool:
    """Redeliver only the latest current queued intent after a producer crash."""

    if (
        execution.owner_user_id != service.owner_user_id
        or execution.addon_id != "doc_processor"
        or execution.resource_kind != "document"
        or execution.status != "queued"
    ):
        return False
    contract = _contract(execution)
    if contract is None:
        return False
    attempts = contract.get("attempts") or []
    receipt = attempts[-1] if attempts else contract.get("initial_attempt")
    if not isinstance(receipt, Mapping):
        return False
    if receipt.get("job_id") != execution.queue_job_id:
        return False
    expected_settings = (
        contract.get("suspended_settings_sha256")
        if attempts
        else contract.get("initial_settings_sha256")
    )
    try:
        document = service._document(execution.resource_id, fresh=True)
        if (
            not _bound(execution, document)
            or document.status != "processing"
            or (document.settings or {}).get("active_job_id") != execution.queue_job_id
            or settings_fingerprint(dict(document.settings or {})) != expected_settings
        ):
            return False
        _matches_source(contract, service._stored_source(document))
        queue = service._queue()
        status = queue.get_status(execution.queue_job_id)
        if isinstance(status, Mapping) and status.get("status") in {
            "completed",
            "failed",
            "cancelled",
            "running",
            "processing",
        }:
            return False
        _dispatch(service, receipt)
        return True
    except McpWorkError, OSError, RuntimeError, TypeError, ValueError, AttributeError:
        return False


def recovery_projection(
    document: ProcessedDocument, execution: McpExecution
) -> dict[str, Any]:
    """Expose only safe eligibility and progress counts for one execution."""

    contract = _contract(execution)
    completed = (
        db.session.query(func.count(McpWorkItem.id))
        .filter(
            McpWorkItem.execution_id == execution.id, McpWorkItem.state == "completed"
        )
        .scalar()
        or 0
    )
    state = (document.settings or {}).get("pdf_state")
    pages = state.get("html_parts") if isinstance(state, Mapping) else None
    checkpoint_pages = (
        sum(bool(part) for part in pages) if isinstance(pages, list) else 0
    )
    reason = "ready"
    if contract is None:
        reason = "recovery_unavailable"
    elif not _bound(execution, document):
        reason = "execution_conflict"
    elif execution.status != "suspended" or document.status != "suspended":
        reason = "execution_not_suspended"
    elif (
        isinstance((document.settings or {}).get("mcp_recovery"), Mapping)
        and ((document.settings or {})["mcp_recovery"].get("reason") or {}).get(
            "error_code"
        )
        == "recovery_replay_conflict"
    ):
        reason = "recovery_replay_conflict"
    elif (
        isinstance((document.settings or {}).get("mcp_recovery"), Mapping)
        and ((document.settings or {})["mcp_recovery"].get("reason") or {}).get("error_code")
        in {"list_continuation_style_conflict", "list_continuation_ordinal_conflict"}
    ):
        reason = "assembly_repair_required"
    elif settings_fingerprint(dict(document.settings or {})) != contract.get(
        "suspended_settings_sha256"
    ):
        reason = "recovery_settings_changed"
    else:
        try:
            source = resolve_upload_reference(document.file_path)
            _matches_source(contract, source)
        except McpWorkError as exc:
            reason = exc.code
        except OSError, RuntimeError, TypeError, ValueError:
            reason = "source_unavailable"
    marker = (document.settings or {}).get("mcp_recovery")
    return {
        "resumable": reason == "ready",
        "reason": reason,
        "generation": contract.get("generation") if contract else None,
        "completed_work_items": int(completed),
        "checkpoint_pages": checkpoint_pages,
        "attempt_count": len(contract.get("attempts", [])) if contract else 0,
        "suspension_reason": marker.get("reason")
        if isinstance(marker, Mapping)
        else None,
        "next_action": "resume" if reason == "ready" else (
            "inspect_assembly_repair" if reason == "assembly_repair_required" else "resolve_blocker"
        ),
        "call": {
            "tool": "carlina_job_resume",
            "arguments": {
                "execution_id": execution.id,
                "idempotency_key": "<unique-key>",
            },
        }
        if reason == "ready"
        else ({"tool": "carlina_document_recover", "arguments": {
            "execution_id": execution.id, "operation": "inspect"
        }} if reason == "assembly_repair_required" else None),
    }
