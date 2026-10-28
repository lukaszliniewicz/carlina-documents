import errno
import hashlib
import json
import logging
import os
import re
import stat
import tempfile
import uuid
from collections.abc import Mapping
from contextlib import nullcontext
from copy import deepcopy
from functools import wraps
from pathlib import Path
from typing import Any

from flask import current_app as app
from sqlalchemy import and_, case, func, inspect as sa_inspect, or_, update
from sqlalchemy.orm.attributes import flag_modified

# Managed add-on workers execute their declared script path directly.  Give
# package-relative imports the same anchor they receive under normal imports.
if not __package__:
    __package__ = ".".join(
        parent.name for parent in reversed(Path(__file__).resolve().parents[:3])
    )

from carlina.core.windows_artifacts import WindowsArtifactDirectory

from iarf_assistant.addons.doc_processor.format_utils import (
    html_to_markdown_on_the_fly,
    markdown_to_html_on_the_fly,
)
from iarf_assistant.addons.doc_processor.knowledge_adapter import (
    mark_processed_document_source_failed,
    sync_processed_document_source,
)
from iarf_assistant.addons.doc_processor.metadata_service import (
    get_canonical_document_metadata,
    get_document_display_title,
    get_primary_creator_names,
    sync_document_metadata,
)
from iarf_assistant.addons.doc_processor.models import ProcessedDocument
from iarf_assistant.addons.doc_processor.parsers import get_parser
from iarf_assistant.addons.doc_processor.parsers.pdf_utils import (
    ListContinuationError,
    concatenate_html_pages,
)
from iarf_assistant.addons.doc_processor.pdf_recovery import (
    is_complete_pdf_image_review_state,
)
from iarf_assistant.addons.doc_processor.reasoning_snapshot import (
    REASONING_SNAPSHOT_KEY,
    reasoning_debug_metadata,
    validate_reasoning_snapshot,
)
from iarf_assistant.addons.doc_processor.semantic_document import (
    SemanticDocumentError,
    canonicalize_document_html,
    is_current_semantic_document,
    namespace_semantic_fragment,
    semantic_result_for_stored_html,
    semantic_settings,
)
from iarf_assistant.core.upload_paths import resolve_upload_reference, uploads_root
from iarf_assistant.core.worker_runtime import build_worker_node, run_worker_node
from iarf_assistant.extensions import db
from iarf_assistant.llm_service import LLMService
from .page_batch_response import PageBatchValidationError
from .performance_metrics import safe_preparation_timings

logger = logging.getLogger("DocProcessorWorker")
logger.setLevel(logging.INFO)

_EXTRACTION_ISSUES_POLICY = "issues-v1"
_EXTRACTION_ISSUES_CHECKPOINT_KEY = "extraction_issues_checkpoint"
_EXTRACTION_ISSUES_CHECKPOINT_SCHEMA = "doc-processor-extraction-issues-v1"
_MAX_EXTRACTION_ISSUES_TOTAL = 10_000

worker = build_worker_node("doc_processor", idle_timeout=60)


def passive_execution(**kwargs):
    """Resolve the shared MCP completion context lazily.

    The lazy import keeps worker registration import-safe while the host is
    assembling its core MCP package.  Tests and host adapters may patch this
    local seam without changing the shared context contract.
    """

    from iarf_assistant.core.mcp_completion import passive_execution as context

    return context(**kwargs)


def _passive_execution_context(payload: dict, *, queue_job_id: str | None = None):
    document_id = str(payload.get("document_id") or "").strip()
    document = db.session.get(ProcessedDocument, document_id) if document_id else None
    persisted_settings = dict(getattr(document, "settings", None) or {})

    execution_id = payload.get("mcp_execution_id")
    if execution_id in (None, ""):
        execution_id = payload.get("execution_id")
    if execution_id in (None, ""):
        execution_id = persisted_settings.get("mcp_execution_id")
    if execution_id in (None, ""):
        execution_id = persisted_settings.get("execution_id")

    mode = payload.get("mcp_execution_mode")
    if mode in (None, ""):
        mode = payload.get("execution_mode")
    if mode in (None, ""):
        mode = persisted_settings.get("mcp_execution_mode")
    if mode in (None, ""):
        mode = persisted_settings.get("execution_mode")

    normalized_mode = str(mode or "").strip().lower()
    normalized_execution_id = str(execution_id or "").strip()
    owner_user_id = payload.get("user_id") or getattr(document, "user_id", None)
    if (
        normalized_mode != "passive"
        or not normalized_execution_id
        or owner_user_id
        in (
            None,
            "",
        )
    ):
        return nullcontext()

    attempt = {}
    if queue_job_id is not None:
        from ...models import McpExecution

        execution = db.session.get(McpExecution, normalized_execution_id)
        recovery = (execution.request_json or {}).get("recovery", {}) if execution else {}
        if recovery.get("schema_version") == 1:
            attempt["queue_job_id"] = queue_job_id
    return passive_execution(
        execution_id=normalized_execution_id,
        owner_user_id=int(owner_user_id),
        addon_id="doc_processor",
        resource_kind="document",
        resource_id=document_id,
        **attempt,
    )


def _with_passive_execution(handler):
    @wraps(handler)
    def wrapped(payload: dict, task_queue, job_id: str):
        from sqlalchemy.exc import OperationalError

        from ...core.mcp_completion import PassiveAttemptSuperseded, PassiveReplayConflict
        from ...core.task_queue import JobRetryLaterError
        from ...database_utils import is_database_locked

        try:
            with _passive_execution_context(
                payload, queue_job_id=job_id if handler.__name__ == "process_document" else None
            ):
                try:
                    return handler(payload, task_queue, job_id)
                except PassiveReplayConflict as exc:
                    from .execution_recovery import suspend_execution

                    db.session.rollback()
                    if suspend_execution(
                        payload.get("document_id"), str(payload.get("mcp_execution_id") or ""),
                        job_id, {"error_code": "recovery_replay_conflict", "batch_id": exc.batch_id, "page": exc.page},
                    ):
                        return {"suspended": True, "resumable": False}
                    raise
        except PassiveAttemptSuperseded:
            db.session.rollback()
            return {"superseded": True, "message": "The extraction attempt is no longer current."}
        except OperationalError as exc:
            if not is_database_locked(exc):
                raise
            db.session.rollback()
            raise JobRetryLaterError(
                "Database busy; extraction progress is retained.", delay_seconds=5
            ) from exc

    return wrapped


def _get_client(user_id: int):
    addon = app.addon_manager.get_loaded_addon("doc_processor")
    if not addon:
        raise RuntimeError("Document Processor addon is not active.")
    return addon.initialize_client({"current_user_id": user_id})


def _clear_mcp_execution_binding(doc: ProcessedDocument) -> None:
    """Remove a terminal run's dispatch marker without erasing its ledger."""

    settings = dict(doc.settings or {})
    document_status = getattr(doc, "status", None)
    terminal_status = {
        "completed": "completed",
        "error": "failed",
        "cancelled": "cancelled",
    }.get(document_status)
    if terminal_status is None:
        return

    execution_id = str(settings.get("mcp_execution_id") or "").strip()
    if execution_id and terminal_status:
        document_id = getattr(doc, "id", None)
        owner_user_id = getattr(doc, "user_id", None)
        if document_id is not None and owner_user_id is not None:
            from iarf_assistant.core.mcp_jobs import transition_execution
            from iarf_assistant.models import McpExecution

            execution = db.session.get(McpExecution, execution_id)
            if execution is not None:
                if (
                    execution.owner_user_id != owner_user_id
                    or execution.addon_id != "doc_processor"
                    or execution.resource_kind != "document"
                    or str(execution.resource_id) != str(document_id)
                ):
                    raise RuntimeError("MCP execution binding does not match document.")
                # transition_execution provides the terminal-state fence and
                # supports an idempotent same-state transition.  The caller
                # owns the enclosing commit.
                transition_execution(execution, terminal_status)

    changed = False
    if execution_id:
        settings["mcp_last_execution_id"] = execution_id
    for key in ("mcp_execution_id", "mcp_execution_mode"):
        if key in settings:
            settings.pop(key)
            changed = True
    if changed:
        doc.settings = settings
        flag_modified(doc, "settings")


def _mark_document_error(
    document_id: str,
    error: Exception,
    *,
    payload: dict | None = None,
    job_id: str | None = None,
    preflight_snapshot: tuple | None = None,
) -> bool:
    """Fence and persist a worker failure without clobbering a newer run."""

    db.session.rollback()
    failure_statuses = {
        "created",
        "processing",
        "preparing_crop_review",
        "finalizing",
    }
    clauses = [
        ProcessedDocument.id == document_id,
        ProcessedDocument.status.in_(failure_statuses),
    ]
    if job_id is not None:
        job_matches = ProcessedDocument.settings["active_job_id"].as_string() == str(
            job_id
        )
        if preflight_snapshot is not None:
            status, updated_at, settings = preflight_snapshot
            # Preflight examines current document state, before the producer
            # necessarily publishes its queue marker. Only that exact unbound
            # revision may fail; processing failures never use this exception.
            job_matches = or_(
                job_matches,
                and_(
                    ProcessedDocument.settings["active_job_id"].as_string().is_(None),
                    ProcessedDocument.status == status,
                    ProcessedDocument.updated_at == updated_at,
                    ProcessedDocument.settings == settings,
                ),
            )
        clauses.append(job_matches)
    execution_id = str((payload or {}).get("mcp_execution_id") or "").strip()
    if execution_id:
        clauses.append(
            ProcessedDocument.settings["mcp_execution_id"].as_string() == execution_id
        )

    with db.session.no_autoflush:
        changed = db.session.execute(
            update(ProcessedDocument)
            .where(*clauses)
            .values(status="error", error_message=str(error))
            .execution_options(synchronize_session=False)
        ).rowcount
    if changed != 1:
        db.session.rollback()
        return False

    # The SQL update is the write reservation.  Only now read settings and
    # apply bounded diagnostics/cleanup from the winning worker.
    db.session.expire_all()
    doc = db.session.get(ProcessedDocument, document_id)
    if not doc:
        db.session.rollback()
        return False
    _persist_document_error_details(doc, error)
    try:
        _clear_mcp_execution_binding(doc)
        _clear_document_activity(doc)
        mark_processed_document_source_failed(doc, error)
        db.session.commit()
    except BaseException:
        db.session.rollback()
        raise
    return True


def _suspend_recoverable_failure(document_id, payload, job_id, error) -> bool:
    from .execution_recovery import suspend_execution
    from .parsers.pdf_render_pool import RenderError
    from ...core.mcp_jobs import McpWorkError

    if isinstance(error, RenderError) and error.recoverable:
        reason = {**error.diagnostic, "error_code": error.code}
    elif isinstance(error, ListContinuationError):
        reason = {
            "error_code": error.reason_code.replace("-", "_"),
            "phase": "document_assembly",
            "page": error.page_number,
            "previous_page": error.previous_page,
            "section": error.section,
        }
    elif isinstance(error, FileNotFoundError):
        reason = {"error_code": "source_unavailable"}
    elif isinstance(error, McpWorkError) and error.code in {"recovery_source_changed", "source_unavailable"}:
        reason = {"error_code": error.code}
    elif isinstance(error, MemoryError) or (
        isinstance(error, OSError)
        and error.errno in {errno.ENOSPC, errno.EIO, errno.EMFILE, errno.ENFILE, errno.ENOMEM, errno.EAGAIN, errno.ETIMEDOUT}
    ):
        reason = {"error_code": "local_resource_unavailable"}
        if isinstance(error, OSError):
            reason["errno"] = error.errno
    else:
        return False
    db.session.rollback()
    return suspend_execution(
        document_id, str(payload.get("mcp_execution_id") or ""), job_id, reason
    )


def _persist_document_error_details(doc: ProcessedDocument, error: Exception) -> None:
    settings = dict(doc.settings or {})
    metrics = dict(settings.get("metrics") or {})
    changed = False
    if isinstance(error, PageBatchValidationError):
        metrics["batch_validation_error"] = dict(error.feedback)
        changed = True
    if isinstance(error, ListContinuationError):
        metrics["batch_validation_error"] = {
            "stage": "document_assembly", "reason": error.reason_code,
            "page": error.page_number, "previous_page": error.previous_page,
            "section": error.section,
        }
        changed = True
    diagnostics = getattr(error, "preparation_diagnostics", None)
    if isinstance(diagnostics, list):
        metrics["metadata_preparation"] = [
            dict(item) for item in diagnostics[-12:] if isinstance(item, dict)
        ]
        changed = True
    if changed:
        settings["metrics"] = metrics
        doc.settings = settings
        flag_modified(doc, "settings")


def _document_preflight_snapshot(doc: ProcessedDocument) -> tuple:
    return doc.status, doc.updated_at, deepcopy(doc.settings)


def _clear_document_activity(doc: ProcessedDocument) -> None:
    if doc.settings:
        settings = dict(doc.settings)
        settings.pop("active_job_id", None)
        settings.pop("enqueue_claim", None)
        settings.pop("progress", None)
        settings.pop("progress_msg", None)
        doc.settings = settings
        flag_modified(doc, "settings")


def _reserve_document_result_persistence(
    doc: ProcessedDocument,
    payload: dict,
    job_id: str,
) -> bool:
    """Atomically let either cancellation or this exact worker publish first."""

    clauses = [
        ProcessedDocument.id == doc.id,
        ProcessedDocument.status == "processing",
        ProcessedDocument.settings["active_job_id"].as_string() == str(job_id),
    ]
    execution_id = str(payload.get("mcp_execution_id") or "").strip()
    if execution_id:
        clauses.append(
            ProcessedDocument.settings["mcp_execution_id"].as_string()
            == execution_id
        )
    with db.session.no_autoflush:
        changed = db.session.execute(
            update(ProcessedDocument)
            .where(*clauses)
            .values(status="finalizing")
            .execution_options(synchronize_session=False)
        ).rowcount
    if changed != 1:
        db.session.rollback()
        return False
    doc.status = "finalizing"
    return True


def _sync_document_metadata(doc: ProcessedDocument):
    metadata_dict = (doc.settings or {}).get("document_metadata")
    sync_document_metadata(doc, metadata_dict, ensure_record=True)
    if doc.metadata_record is not None:
        db.session.add(doc.metadata_record)


def _reserve_passive_write(doc: ProcessedDocument, job_id: str, *, statuses=("processing",)) -> bool:
    """Reserve only this frozen delivery until its immediate write commits.

    Never hold this reservation across rendering or external model work. Legacy
    and image-review handlers retain their existing delivery contract.
    """
    from ...core.mcp_completion import PassiveAttemptSuperseded, passive_execution_context
    from ...models import McpExecution

    context = passive_execution_context()
    if context is None or context.queue_job_id is None:
        return False
    with db.session.no_autoflush:
        execution = db.session.get(McpExecution, context.execution_id)
        recovery = (execution.request_json or {}).get("recovery", {}) if execution else {}
        if recovery.get("schema_version") != 1:
            db.session.rollback()
            raise PassiveAttemptSuperseded("The frozen extraction contract is unavailable.")
        active = ("queued", "running", "waiting_external")
        if "reviewing_images" in statuses:
            active += ("waiting_review",)
        changed = db.session.execute(
            update(McpExecution).where(
                McpExecution.id == context.execution_id,
                McpExecution.owner_user_id == context.owner_user_id,
                McpExecution.queue_job_id == str(job_id),
                McpExecution.status.in_(active),
            ).values(updated_at=McpExecution.updated_at)
            .execution_options(synchronize_session=False)
        ).rowcount
        if changed == 1 and context.queue_job_id == str(job_id):
            changed = db.session.execute(
                update(ProcessedDocument).where(
                    ProcessedDocument.id == context.resource_id,
                    ProcessedDocument.id == doc.id,
                    ProcessedDocument.user_id == context.owner_user_id,
                    ProcessedDocument.status.in_(statuses),
                    ProcessedDocument.settings["active_job_id"].as_string() == str(job_id),
                    ProcessedDocument.settings["mcp_execution_id"].as_string() == context.execution_id,
                ).values(updated_at=ProcessedDocument.updated_at)
                .execution_options(synchronize_session=False)
            ).rowcount
        else:
            changed = 0
    if changed != 1:
        db.session.rollback()
        raise PassiveAttemptSuperseded("The extraction attempt is no longer current.")
    return True


def _make_document_progress_callback(doc: ProcessedDocument, task_queue, job_id: str):
    from ...core.mcp_completion import passive_execution_context

    invocation_context = passive_execution_context()

    def progress_callback(pct: int, msg: str):
        # This callback belongs to its creating invocation, never a refreshed
        # document binding or a later attempt.
        if invocation_context is not passive_execution_context():
            from ...core.mcp_completion import PassiveAttemptSuperseded

            raise PassiveAttemptSuperseded("The extraction context changed.")
        normalized_pct = max(0, min(100, int(pct or 0)))
        if task_queue and job_id:
            task_queue.set_status(
                job_id,
                {"status": "processing", "progress": normalized_pct, "message": msg},
            )
        if _reserve_passive_write(doc, job_id):
            db.session.refresh(doc)
        doc.settings = doc.settings or {}
        doc.settings["progress"] = normalized_pct
        doc.settings["progress_msg"] = msg
        flag_modified(doc, "settings")
        db.session.commit()

    return progress_callback


def _make_document_control_callback(task_queue, job_id: str):
    def control_callback():
        if not task_queue or not job_id:
            return None
        status = task_queue.get_status(job_id) or {}
        if (
            status.get("stop_requested")
            or status.get("cancel_requested")
            or status.get("status") == "cancelling"
        ):
            return "stop"
        if status.get("pause_requested"):
            return "pause"
        return None

    return control_callback


def _reasoning_run_metadata(settings: dict | None) -> dict | None:
    """Validate an immutable snapshot and expose only safe run metadata."""

    from ...core.mcp_completion import passive_execution_context

    if passive_execution_context() is not None:
        # Passive execution has no Carlina model/provider identity.  Do not
        # validate or persist the managed reasoning snapshot in its metrics.
        return None
    snapshot = (settings or {}).get(REASONING_SNAPSHOT_KEY)
    if snapshot is None:
        return None  # Legacy rows retain their dynamic compatibility behavior.
    validate_reasoning_snapshot(snapshot, settings=settings)
    return reasoning_debug_metadata(snapshot)


def _parser_settings(settings: dict | None) -> dict:
    """Use provider-neutral internal roles for passive parser calls."""

    values = dict(settings or {})
    from ...core.mcp_completion import passive_execution_context

    if passive_execution_context() is not None:
        values.pop(REASONING_SNAPSHOT_KEY, None)
        values["parsing_model"] = "carlina.passive.document"
        values["metadata_model"] = "carlina.passive.document.metadata"
    return values


def _load_extraction_issues_checkpoint(document) -> dict[int, list[dict]]:
    """Decode and identity-check the private page-issue resume checkpoint."""

    settings = dict(getattr(document, "settings", None) or {})
    if settings.get("mcp_extraction_issues_policy") != _EXTRACTION_ISSUES_POLICY:
        return {}
    encoded = settings.get(_EXTRACTION_ISSUES_CHECKPOINT_KEY)
    if encoded is None:
        return {}
    try:
        if not isinstance(encoded, str):
            raise ValueError("invalid checkpoint encoding")
        from carlina.content_storage import content_codec_from_context
        from .extraction_issues import validate_issue_reports

        decoded = json.loads(content_codec_from_context().decode_text(encoded))
        if (
            not isinstance(decoded, dict)
            or set(decoded)
            != {"schema", "document_id", "owner_user_id", "pages"}
            or decoded.get("schema") != _EXTRACTION_ISSUES_CHECKPOINT_SCHEMA
            or decoded.get("document_id") != str(document.id)
            or decoded.get("owner_user_id") != document.user_id
            or not isinstance(decoded.get("pages"), list)
        ):
            raise ValueError("invalid checkpoint binding")

        result: dict[int, list[dict]] = {}
        total_issues = 0
        previous_page = 0
        for page in decoded["pages"]:
            if (
                not isinstance(page, dict)
                or set(page) != {"page_number", "issues"}
                or type(page.get("page_number")) is not int
                or page["page_number"] <= previous_page
            ):
                raise ValueError("invalid checkpoint page")
            issues = validate_issue_reports(page["issues"])
            total_issues += len(issues)
            if total_issues > _MAX_EXTRACTION_ISSUES_TOTAL:
                raise ValueError("issue limit exceeded")
            result[page["page_number"]] = issues
            previous_page = page["page_number"]
        return result
    except Exception:
        raise RuntimeError(
            "Extraction issue checkpoint is unavailable or invalid."
        ) from None


def _extraction_issue_pages(parser) -> list[dict]:
    """Return a validated, physical-page-sorted view for checkpoint/import."""

    from .extraction_issues import validate_issue_reports

    issue_lock = getattr(parser, "metrics_lock", None)
    with issue_lock if issue_lock is not None else nullcontext():
        raw_pages = getattr(parser, "extraction_issues_by_page", {})
        if not isinstance(raw_pages, Mapping):
            raise RuntimeError("PDF extraction issue checkpoint is invalid.")
        raw_pages = dict(raw_pages)
    if any(type(page_number) is not int or page_number <= 0 for page_number in raw_pages):
        raise RuntimeError("PDF extraction issue checkpoint is invalid.")
    pages = []
    total_issues = 0
    for page_number in sorted(raw_pages):
        issues = validate_issue_reports(raw_pages[page_number])
        total_issues += len(issues)
        if total_issues > _MAX_EXTRACTION_ISSUES_TOTAL:
            raise RuntimeError("PDF extraction issue report limit exceeded.")
        pages.append({"page_number": page_number, "issues": issues})
    return pages


def _save_extraction_issues_checkpoint(document, parser) -> list[dict]:
    """Persist policy-encoded issue data and return its transient page view."""

    settings = dict(getattr(document, "settings", None) or {})
    if settings.get("mcp_extraction_issues_policy") != _EXTRACTION_ISSUES_POLICY:
        return []
    pages = _extraction_issue_pages(parser)
    if pages:
        settings[_EXTRACTION_ISSUES_CHECKPOINT_KEY] = (
            _encode_extraction_issues_checkpoint(document, pages)
        )
    else:
        settings.pop(_EXTRACTION_ISSUES_CHECKPOINT_KEY, None)
    document.settings = settings
    flag_modified(document, "settings")
    return pages


def _encode_extraction_issues_checkpoint(document, pages: list[dict]) -> str | None:
    if not pages:
        return None
    if getattr(document, "id", None) is None or getattr(document, "user_id", None) is None:
        raise RuntimeError("PDF extraction issue checkpoint cannot be identity-bound.")
    from carlina.content_storage import content_codec_from_context

    envelope = {
        "schema": _EXTRACTION_ISSUES_CHECKPOINT_SCHEMA,
        "document_id": str(document.id),
        "owner_user_id": document.user_id,
        "pages": pages,
    }
    return content_codec_from_context().encode_text(
        json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )


def _persist_extraction_issue_checkpoint_fenced(
    document,
    encoded: str | None,
    *,
    job_id: str | None,
    execution_id: str | None,
) -> bool:
    """Save encoded content through the active worker's row/execution fence."""

    document_id = getattr(document, "id", None)
    owner_user_id = getattr(document, "user_id", None)
    if document_id is None or owner_user_id is None or not job_id:
        return False
    table = ProcessedDocument.__table__
    settings = table.c.settings
    updated_settings = func.json_remove(
        func.coalesce(settings, func.json("{}")),
        f"$.{_EXTRACTION_ISSUES_CHECKPOINT_KEY}",
    )
    if encoded is not None:
        updated_settings = func.json_set(
            updated_settings,
            f"$.{_EXTRACTION_ISSUES_CHECKPOINT_KEY}",
            encoded,
        )
    conditions = [
        table.c.id == str(document_id),
        table.c.user_id == owner_user_id,
        table.c.status == "processing",
        settings["active_job_id"].as_string() == str(job_id),
    ]
    if execution_id:
        conditions.append(
            settings["mcp_execution_id"].as_string() == str(execution_id)
        )
    statement = (
        update(table)
        .where(*conditions)
        .values({settings: updated_settings, table.c.updated_at: table.c.updated_at})
    )
    with db.session.no_autoflush, db.engine.begin() as connection:
        changed = connection.execute(statement).rowcount
    return changed == 1


def _import_extraction_issues(document, pages: list[dict]) -> None:
    """Import frozen issue reports into the document ledger without committing."""

    settings = dict(getattr(document, "settings", None) or {})
    if settings.get("mcp_extraction_issues_policy") != _EXTRACTION_ISSUES_POLICY:
        return
    from .issue_ledger import import_extraction_issues

    import_extraction_issues(document, pages, source_revision=None)


def _sync_project_entry(payload: dict, status: str, task_queue=None, error=None):
    from iarf_assistant.addons.doc_processor.project_service import (
        sync_project_run_document,
    )

    sync_project_run_document(
        payload.get("project_run_document_id"), status, task_queue, error
    )


def _document_source_path(doc: ProcessedDocument) -> Path:
    try:
        source_path = resolve_upload_reference(
            doc.file_path, uploads_dir=uploads_root()
        )
    except OSError, RuntimeError, TypeError, ValueError:
        source_path = None
    if source_path is None or not source_path.is_file():
        logger.warning(
            "Source file missing for document %s.", getattr(doc, "id", "unknown")
        )
        raise FileNotFoundError("Source file missing.")
    return source_path


def _partial_output_from_state(partial_state: dict, doc: ProcessedDocument):
    from .note_arrangement import note_arrangement_enabled
    from iarf_assistant.addons.doc_processor.parsers.pdf_utils import (
        convert_html_to_clean_markdown,
    )

    try:
        partial_html = concatenate_html_pages(
            [(part or "") for part in partial_state.get("html_parts", [])],
            {},
            preserve_note_labels=note_arrangement_enabled(doc.settings),
        )
        semantic = canonicalize_document_html(
            partial_html,
            user_id=doc.user_id,
            source_file_path=doc.file_path,
            relaxed=True,
        )
        return semantic.html, convert_html_to_clean_markdown(semantic.html)
    except Exception as exc:
        logger.error(
            "Failed to generate preview HTML/Markdown for partial state: %s", exc
        )
        return "", ""


def _store_semantic_output(
    doc: ProcessedDocument,
    html_content: str,
    *,
    completed: bool,
    image_crop_sources: dict | None = None,
) -> None:
    """Establish canonical output before Markdown/knowledge synchronization."""

    result = canonicalize_document_html(
        html_content,
        user_id=doc.user_id,
        source_file_path=doc.file_path,
        relaxed=not completed,
    )
    if completed and result.errors:
        raise SemanticDocumentError(
            "Completed output has invalid semantic references; correct the source or resume extraction."
        )
    doc.output_html = result.html
    # Do not expose paused/error output as a Translator-ready current source.
    doc.settings = semantic_settings(
        doc.settings, result, mode="completed" if completed else "partial"
    )
    if completed and image_crop_sources is not None:
        doc.settings["image_crops"] = dict(image_crop_sources)
    try:
        doc.output_markdown = html_to_markdown_on_the_fly(result.html)
    except Exception:
        logger.exception("Failed to derive Markdown from canonical HTML.")
        doc.output_markdown = ""


def _wait_for_unchanged_passive_checkpoint(doc, task_queue, job_id: str) -> None:
    """Avoid PDF reopening, image encoding and HTML replay during empty polls.

    Wake on ANY completion in a concurrent window, or on control/state changes.
    Read only indexed identity/state columns, never encrypted request bodies.
    """
    from ...core.mcp_completion import passive_execution_context
    from ...core.mcp_jobs import MCP_ACTIVE_EXECUTION_STATUSES
    from ...core.task_queue import ExternalWorkPending
    from ...models import McpExecution, McpWorkItem

    context = passive_execution_context()
    settings = doc.settings or {}
    state = settings.get("pdf_state") or {}
    if (
        context is None or doc.status != "processing" or not isinstance(state, dict)
        or context.execution_id != settings.get("mcp_execution_id")
        or context.owner_user_id != doc.user_id or context.resource_id != doc.id
        or settings.get("active_job_id") != job_id
        or _make_document_control_callback(task_queue, job_id)()
    ):
        return

    if (
        not state
        and settings.get("mcp_page_batch_protocol") == "carlina.pdf-page-batch.v1"
        and settings.get("mcp_metadata_policy") == "budgeted-v1"
        and settings.get("mcp_metadata_complete") is not True
    ):
        metadata_work = (
            db.session.query(McpWorkItem.id)
            .join(McpExecution, McpExecution.id == McpWorkItem.execution_id)
            .filter(
                McpExecution.id == context.execution_id,
                McpExecution.owner_user_id == doc.user_id,
                McpExecution.addon_id == "doc_processor",
                McpExecution.resource_kind == "document",
                McpExecution.resource_id == doc.id,
                McpExecution.status.in_(MCP_ACTIVE_EXECUTION_STATUSES),
                McpWorkItem.execution_id == context.execution_id,
                McpWorkItem.batch_id.is_(None),
                McpWorkItem.state.in_(("pending", "leased")),
            )
            .limit(1)
            .first()
        )
        if metadata_work is not None:
            raise ExternalWorkPending(
                "Waiting for external metadata; checkpoint is unchanged.",
                delay_seconds=5.0,
                work_item_id=metadata_work.id,
            )

    if context.execution_id != state.get("execution_id"):
        return
    pending = state.get("waiting_work_item_ids")
    if not isinstance(pending, list) or not 1 <= len(pending) <= 128 or any(not isinstance(item, str) for item in pending):
        return
    identifiers = set(pending)
    rows = db.session.query(McpWorkItem.id, McpWorkItem.state).join(
        McpExecution, McpExecution.id == McpWorkItem.execution_id
    ).filter(
        McpWorkItem.id.in_(identifiers), McpExecution.id == context.execution_id,
        McpExecution.owner_user_id == doc.user_id, McpExecution.addon_id == "doc_processor",
        McpExecution.resource_id == doc.id,
        McpExecution.status.in_(MCP_ACTIVE_EXECUTION_STATUSES),
    ).all()
    if len(rows) == len(identifiers) and all(row.state in {"pending", "leased"} for row in rows):
        raise ExternalWorkPending(
            "Waiting for external extraction; checkpoint is unchanged.",
            delay_seconds=5.0, work_item_id=pending[0],
        )


def _persist_partial_preparation_metrics(
    parser: Any,
    document_id: str | None,
    owner_user_id: int | None,
    job_id: str | None,
    execution_id: str | None,
) -> bool:
    """Best-effort persist only bounded timings for the still-active invocation."""

    try:
        parser_metrics = getattr(parser, "metrics", None)
        if not isinstance(parser_metrics, dict):
            return False
        timings = safe_preparation_timings(
            parser_metrics.get("preparation_timings")
        )
        if (
            not timings
            or not document_id
            or owner_user_id is None
            or not job_id
        ):
            return False

        serialized = json.dumps(
            timings, allow_nan=False, ensure_ascii=True, separators=(",", ":")
        )
        table = ProcessedDocument.__table__
        settings = table.c.settings
        metrics_type = func.json_type(settings, "$.metrics")
        current_metrics = case(
            (metrics_type == "object", func.json_extract(settings, "$.metrics")),
            else_=func.json("{}"),
        )
        updated_metrics = func.json_set(
            current_metrics,
            "$.preparation_timings",
            func.json(serialized),
        )
        updated_settings = func.json_set(
            func.coalesce(settings, func.json("{}")),
            "$.metrics",
            func.json(updated_metrics),
        )

        conditions = [
            table.c.id == str(document_id),
            table.c.user_id == owner_user_id,
            table.c.status == "processing",
            settings["active_job_id"].as_string() == str(job_id),
            or_(metrics_type.is_(None), metrics_type == "object"),
        ]
        if execution_id:
            conditions.append(
                settings["mcp_execution_id"].as_string() == str(execution_id)
            )

        statement = (
            update(table)
            .where(*conditions)
            .values(
                {
                    settings: updated_settings,
                    table.c.updated_at: table.c.updated_at,
                }
            )
        )
        # Use a short independent Core transaction. This neither flushes nor
        # commits unrelated objects held by the worker's ORM session.
        with db.session.no_autoflush, db.engine.begin() as connection:
            changed = connection.execute(statement).rowcount
        return changed == 1
    except BaseException as exc:  # noqa: BLE001 - best-effort persistence cannot mask parser failures.
        try:
            logger.warning(
                "Could not persist partial document preparation metrics (%s)",
                type(exc).__name__,
            )
        except BaseException:  # noqa: BLE001, S110 - logging also cannot mask parser failures.
            pass
        return False


def _process_with_native_parser(
    doc: ProcessedDocument, task_queue=None, job_id: str | None = None
):
    import time

    from iarf_assistant.addons.doc_processor.parsers.pdf_utils import (
        convert_html_to_clean_markdown,
    )

    start_time = time.time()
    reasoning_metadata = _reasoning_run_metadata(doc.settings)
    invocation_settings = doc.settings if isinstance(doc.settings, dict) else {}
    invocation_execution_id = str(
        invocation_settings.get("mcp_execution_id") or ""
    ).strip()
    llm_service = LLMService(user_id=doc.user_id)
    source_path = _document_source_path(doc)
    parser = get_parser(
        str(source_path),
        doc.user_id,
        llm_service,
        settings=_parser_settings(doc.settings),
    )
    if (doc.settings or {}).get("mcp_extraction_issues_policy") == _EXTRACTION_ISSUES_POLICY:
        parser.extraction_issues_by_page = _load_extraction_issues_checkpoint(doc)
    progress_callback = (
        _make_document_progress_callback(doc, task_queue, job_id)
        if task_queue is not None and job_id
        else None
    )
    control_callback = (
        _make_document_control_callback(task_queue, job_id)
        if task_queue is not None and job_id
        else None
    )

    try:
        if str(doc.file_path).lower().endswith(".pdf"):
            status, html_content = parser.process(
                auto_mode=False,
                progress_callback=progress_callback,
                control_callback=control_callback,
            )
        else:
            status, html_content = parser.process()
    except BaseException:
        recovery_state = getattr(parser, "recovery_state", None)
        if isinstance(recovery_state, dict) and invocation_execution_id and job_id:
            # Roll back stale ORM state before claiming this exact delivery.
            # Partial output is only a checkpoint, never published output.
            document_id, owner_id = doc.id, doc.user_id
            db.session.rollback()
            _reserve_passive_write(doc, job_id)
            changed = db.session.execute(
                update(ProcessedDocument)
                .where(
                    ProcessedDocument.id == document_id,
                    ProcessedDocument.user_id == owner_id,
                    ProcessedDocument.status == "processing",
                    ProcessedDocument.settings["active_job_id"].as_string() == job_id,
                    ProcessedDocument.settings["mcp_execution_id"].as_string()
                    == invocation_execution_id,
                )
                .values(updated_at=ProcessedDocument.updated_at)
                .execution_options(synchronize_session=False)
            ).rowcount
            if changed == 1:
                db.session.expire(doc)
                settings = dict(doc.settings or {})
                recovery_state["execution_id"] = invocation_execution_id
                settings["pdf_state"] = recovery_state
                # These derived values are part of deterministic packet replay.
                for key in ("mcp_document_context", "mcp_metadata_complete", "document_metadata"):
                    if key in getattr(parser, "settings", {}):
                        settings[key] = parser.settings[key]
                doc.settings = settings
                flag_modified(doc, "settings")
                db.session.commit()
            else:
                db.session.rollback()
        if (doc.settings or {}).get("mcp_extraction_issues_policy") == _EXTRACTION_ISSUES_POLICY:
            try:
                issue_pages = _extraction_issue_pages(parser)
                encoded_checkpoint = _encode_extraction_issues_checkpoint(
                    doc, issue_pages
                )
                _persist_extraction_issue_checkpoint_fenced(
                    doc,
                    encoded_checkpoint,
                    job_id=job_id,
                    execution_id=invocation_execution_id or None,
                )
            except BaseException as checkpoint_error:  # noqa: BLE001 - preserve the parser failure.
                logger.warning(
                    "Could not persist partial extraction issue checkpoint (%s)",
                    type(checkpoint_error).__name__,
                )
        _persist_partial_preparation_metrics(
            parser,
            getattr(doc, "id", None),
            getattr(doc, "user_id", None),
            job_id,
            invocation_execution_id or None,
        )
        raise

    _reserve_passive_write(doc, job_id)
    parser_settings = getattr(parser, "settings", None)
    if isinstance(parser_settings, dict):
        doc.settings = parser_settings
        flag_modified(doc, "settings")
    extraction_issue_pages = []
    if (doc.settings or {}).get("mcp_extraction_issues_policy") == _EXTRACTION_ISSUES_POLICY:
        extraction_issue_pages = _save_extraction_issues_checkpoint(doc, parser)

    metrics = dict(getattr(parser, "metrics", {}) or {})
    if reasoning_metadata is not None:
        metrics["reasoning"] = reasoning_metadata
    previous_duration = float(metrics.get("duration_seconds", 0.0) or 0.0)
    metrics["duration_seconds"] = round(
        previous_duration + (time.time() - start_time), 2
    )

    _sync_document_metadata(doc)

    if status == "WAITING_EXTERNAL":
        from ...core.task_queue import ExternalWorkPending

        partial_state = dict(html_content or {})
        pending = dict(partial_state.pop("external_pending", {}) or {})
        partial_state["waiting_work_item_ids"] = pending.get("work_item_ids") or [pending.get("work_item_id")]
        work_item_id = str(pending.get("work_item_id") or "").strip()
        delay_seconds = float(pending.get("delay_seconds") or 5.0)
        if not work_item_id:
            raise RuntimeError("Passive PDF suspension lost its work-item identity.")
        doc.status = "processing"
        doc.settings = doc.settings or {}
        if doc.settings.get("mcp_execution_id"):
            partial_state["execution_id"] = doc.settings["mcp_execution_id"]
        doc.settings["pdf_state"] = partial_state
        doc.settings["metrics"] = metrics
        flag_modified(doc, "settings")
        db.session.commit()
        raise ExternalWorkPending(
            "Waiting for an MCP worker to return batched document output.",
            delay_seconds=delay_seconds,
            work_item_id=work_item_id,
        )

    if status == "PAUSED_IMAGE_REVIEW":
        doc.status = "reviewing_images"
        doc.settings = doc.settings or {}
        review_state = dict(html_content or {})
        review_state["phase"] = "PAUSED_IMAGE_REVIEW"
        doc.settings["pdf_state"] = review_state
        doc.settings["metrics"] = metrics
        flag_modified(doc, "settings")
        db.session.commit()
        return {
            "paused": True,
            "message": "Paused for human image bounding box review.",
        }

    doc.settings = doc.settings or {}
    doc.settings["metrics"] = metrics
    flag_modified(doc, "settings")
    db.session.commit()

    if status in {"PARTIAL_ERROR", "PAUSED_USER", "STOPPED_USER"}:
        _reserve_passive_write(doc, job_id)
        partial_state = html_content or {}
        doc.settings["pdf_state"] = partial_state
        flag_modified(doc, "settings")
        db.session.commit()

        partial_html, partial_markdown = _partial_output_from_state(partial_state, doc)

        return {
            "paused": False,
            "partial_error": status == "PARTIAL_ERROR",
            "interrupted": status in {"PAUSED_USER", "STOPPED_USER"},
            "control_action": "pause"
            if status == "PAUSED_USER"
            else "stop"
            if status == "STOPPED_USER"
            else None,
            "html": partial_html,
            "markdown": partial_markdown,
            "pdf_state": partial_state,
        }

    try:
        final_html = html_content or ""
        final_markdown = convert_html_to_clean_markdown(final_html)
    except Exception as e:
        logger.error("Failed to generate Markdown for final state: %s", e)
        final_html = str(html_content) if html_content else ""
        final_markdown = ""

    result = {
        "paused": False,
        "partial_error": False,
        "html": final_html,
        "markdown": final_markdown,
        "image_crop_sources": getattr(parser, "image_crop_sources", {}),
    }
    if (doc.settings or {}).get("mcp_extraction_issues_policy") == _EXTRACTION_ISSUES_POLICY:
        result["extraction_issues"] = extraction_issue_pages
    return result


def _process_with_external_engine(
    doc: ProcessedDocument, engine_id: str, task_queue, job_id: str
):
    import time

    start_time = time.time()
    client = _get_client(doc.user_id)
    engine_options = dict((doc.settings or {}).get("engine_options", {}))

    progress_callback = _make_document_progress_callback(doc, task_queue, job_id)

    source_path = _document_source_path(doc)
    resolved_engine = client.select_engine(
        str(source_path), engine=engine_id, **engine_options
    )

    # Built-in parsers return semantic HTML; keep it outside the Markdown-only client contract.
    if resolved_engine in {"llm_vision", "native"}:
        result = _process_with_native_parser(doc, task_queue, job_id)
        result["resolved_engine"] = resolved_engine
        return result

    parse_options = dict(engine_options)
    parse_options["progress_callback"] = progress_callback

    markdown_content = client.parse_document(
        filepath=str(source_path), engine=resolved_engine, **parse_options
    )
    if markdown_content and markdown_content.startswith("Error:"):
        raise RuntimeError(
            markdown_content[len("Error:") :].strip() or markdown_content
        )

    doc.settings = doc.settings or {}
    doc.settings["metrics"] = {
        "duration_seconds": round(time.time() - start_time, 2),
    }
    flag_modified(doc, "settings")

    import re

    is_html = bool(
        re.search(
            r"<(?:main|html|body|article|section)\b",
            markdown_content or "",
            re.IGNORECASE,
        )
    )
    if is_html:
        html_content = markdown_content or ""
    else:
        html_content = markdown_to_html_on_the_fly(markdown_content or "")

    return {
        "paused": False,
        "html": html_content,
        "markdown": None if is_html else markdown_content,
        "resolved_engine": resolved_engine,
    }


@worker.handler("convert_document")
def convert_document(payload: dict, task_queue, job_id: str):
    file_path = Path(payload.get("file_path") or "")
    filename = payload.get("filename") or file_path.name
    user_id = payload.get("user_id")
    engine_id = payload.get("engine_id") or "auto"
    options = dict(payload.get("options") or {})

    if not file_path.is_file():
        raise FileNotFoundError("Source document file not found.")

    task_queue.set_status(
        job_id,
        {"status": "processing", "progress": 5, "message": "Parsing document..."},
    )

    try:
        client = _get_client(user_id)
        metrics_dict = {}
        result_md = client.parse_document(
            filepath=str(file_path),
            engine=engine_id,
            metrics_dict=metrics_dict,
            **options,
        )

        if result_md and result_md.startswith("Error:"):
            raise RuntimeError(result_md)

        task_queue.set_status(
            job_id,
            {
                "status": "processing",
                "progress": 90,
                "message": "Rendering conversion output...",
            },
        )

        if result_md:
            import re

            if re.search(
                r"<(?:main|html|body|article|section)\b", result_md, re.IGNORECASE
            ):
                result_html = result_md
            else:
                result_html = markdown_to_html_on_the_fly(result_md)

            result_md = html_to_markdown_on_the_fly(result_html)
        else:
            result_html = ""

        return {
            "success": True,
            "markdown": result_md,
            "html": result_html,
            "filename": filename,
            "metrics": metrics_dict,
        }
    finally:
        try:
            file_path.unlink(missing_ok=True)
        except Exception:
            logger.warning(
                "Failed to remove temporary conversion upload '%s'.", file_path
            )


def ensure_default_reference_doc() -> str:
    from .print_export import docx_reference_path

    return docx_reference_path()


_MCP_EXPORT_FORMATS = frozenset({"epub", "docx", "html", "pdf", "md"})
_MCP_EXPORT_MIMETYPES = {
    "epub": "application/epub+zip",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "html": "text/html",
    "pdf": "application/pdf",
    "md": "text/markdown",
}


def _open_private_export_directory(parent_fd: int, name: str) -> int:
    try:
        os.mkdir(name, mode=0o700, dir_fd=parent_fd)
    except FileExistsError:
        pass
    descriptor = os.open(
        name,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        dir_fd=parent_fd,
    )
    opened = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(opened.st_mode)
        or opened.st_uid != os.geteuid()
        or stat.S_IMODE(opened.st_mode) & 0o077
    ):
        os.close(descriptor)
        raise RuntimeError("Document export storage is unavailable.")
    return descriptor


def _prepare_mcp_export_directory(
    job_id: str,
) -> tuple[Path, int] | WindowsArtifactDirectory:
    if re.fullmatch(r"[0-9a-f]{64}", job_id) is None:
        raise ValueError("The document export operation ID is invalid.")
    if os.name == "nt":
        return WindowsArtifactDirectory.prepare(
            Path(app.instance_path), "doc_processor_exports", job_id
        )
    if (
        not hasattr(os, "O_DIRECTORY")
        or not hasattr(os, "O_NOFOLLOW")
        or not {os.open, os.mkdir, os.stat, os.unlink, os.link}.issubset(
            os.supports_dir_fd
        )
        or os.link not in os.supports_follow_symlinks
    ):
        raise RuntimeError("Secure document export storage is unavailable.")

    instance_root = Path(app.instance_path)
    try:
        instance_root.mkdir(parents=True, exist_ok=True)
        root_stat = instance_root.lstat()
        if not stat.S_ISDIR(root_stat.st_mode) or stat.S_ISLNK(root_stat.st_mode):
            raise RuntimeError("Document export storage is unavailable.")
        instance_fd = os.open(
            instance_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        )
    except (OSError, ValueError) as exc:
        raise RuntimeError("Document export storage is unavailable.") from exc

    exports_fd = None
    operation_fd = None
    try:
        exports_fd = _open_private_export_directory(
            instance_fd, "doc_processor_exports"
        )
        operation_fd = _open_private_export_directory(exports_fd, job_id)
        operation_path = instance_root / "doc_processor_exports" / job_id
        return operation_path, operation_fd
    except BaseException:
        if operation_fd is not None:
            os.close(operation_fd)
        raise
    finally:
        if exports_fd is not None:
            os.close(exports_fd)
        os.close(instance_fd)


def _stage_mcp_export(
    operation_fd: int | WindowsArtifactDirectory,
    operation_path: Path,
    export_format: str,
    download_name: str,
) -> tuple[str, Path, str, Path]:
    final_name = download_name
    if isinstance(operation_fd, WindowsArtifactDirectory):
        try:
            stage_name, stage_path = operation_fd.reserve_stage(
                export_format, final_name
            )
        except FileExistsError as exc:
            raise RuntimeError("A completed document export already exists.") from exc
        return (
            stage_name,
            stage_path,
            final_name,
            operation_path / final_name,
        )
    try:
        os.stat(final_name, dir_fd=operation_fd, follow_symlinks=False)
    except FileNotFoundError:
        pass
    else:
        raise RuntimeError("A completed document export already exists.")

    for _attempt in range(3):
        stage_name = f".stage-{uuid.uuid4().hex}.{export_format}"
        try:
            descriptor = os.open(
                stage_name,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | os.O_NOFOLLOW,
                0o600,
                dir_fd=operation_fd,
            )
        except FileExistsError:
            continue
        else:
            os.close(descriptor)
            return (
                stage_name,
                operation_path / stage_name,
                final_name,
                operation_path / final_name,
            )
    raise RuntimeError("Document export staging is unavailable.")


def _publish_mcp_export(
    operation_fd: int | WindowsArtifactDirectory,
    stage_name: str,
    final_name: str,
) -> tuple[int, str]:
    if isinstance(operation_fd, WindowsArtifactDirectory):
        return operation_fd.publish(stage_name, final_name)
    descriptor = os.open(
        stage_name,
        os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0),
        dir_fd=operation_fd,
    )
    try:
        initial = os.fstat(descriptor)
        if not stat.S_ISREG(initial.st_mode):
            raise RuntimeError("The document export artifact is invalid.")
        os.fchmod(descriptor, 0o600)
        before = os.fstat(descriptor)
        digest = hashlib.sha256()
        byte_length = 0
        os.lseek(descriptor, 0, os.SEEK_SET)
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            byte_length += len(chunk)
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ) or byte_length != after.st_size:
            raise RuntimeError("The document export artifact changed while writing.")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)

    os.link(
        stage_name,
        final_name,
        src_dir_fd=operation_fd,
        dst_dir_fd=operation_fd,
        follow_symlinks=False,
    )
    os.unlink(stage_name, dir_fd=operation_fd)
    os.fsync(operation_fd)
    return byte_length, digest.hexdigest()


def _mcp_export_row_snapshot(row: Any) -> Any:
    mapper = sa_inspect(type(row))
    values = {
        attribute.key: deepcopy(getattr(row, attribute.key))
        for attribute in mapper.column_attrs
    }
    return type(row)(**values)


def _mcp_export_document_snapshot(
    document: ProcessedDocument,
) -> ProcessedDocument:
    snapshot = _mcp_export_row_snapshot(document)
    metadata_record = document.metadata_record
    if metadata_record is not None:
        snapshot.metadata_record = _mcp_export_row_snapshot(metadata_record)
    return snapshot


@worker.handler("mcp_export_document")
def mcp_export_document(payload: dict, task_queue, job_id: str):
    """Render an owner- and revision-fenced export into private storage."""

    from werkzeug.utils import secure_filename

    required_fields = {
        "contract",
        "user_id",
        "document_id",
        "format",
        "expected_revision",
        "start_h2_on_new_page",
    }
    if (
        not isinstance(payload, dict)
        or not required_fields.issubset(payload)
        or set(payload)
        - required_fields
        - {"epub_cover", "epub_options", "epub_review_hash", "epub_image_mode"}
    ):
        raise ValueError("The document export request is invalid.")
    export_format = payload.get("format")
    expected_revision = payload.get("expected_revision")
    document_id = payload.get("document_id")
    user_id = payload.get("user_id")
    start_h2_on_new_page = payload.get("start_h2_on_new_page")
    from carlina.core.epub_cover import normalize_epub_cover

    cover_options = normalize_epub_cover(payload.get("epub_cover"))
    from carlina.core.epub_export import normalize_epub_image_mode

    epub_image_mode = normalize_epub_image_mode(payload.get("epub_image_mode", "auto"))
    if epub_image_mode != "auto" and export_format != "epub":
        raise ValueError("The document export request is invalid.")
    if cover_options and export_format != "epub":
        raise ValueError("Cover selection requires EPUB.")
    if (
        payload.get("contract") != "document-mcp-export-v1"
        or type(user_id) is not int
        or not isinstance(document_id, str)
        or not document_id.strip()
        or not isinstance(export_format, str)
        or export_format not in _MCP_EXPORT_FORMATS
        or not isinstance(expected_revision, str)
        or re.fullmatch(r"[0-9a-f]{64}", expected_revision) is None
        or type(start_h2_on_new_page) is not bool
        or (start_h2_on_new_page and export_format not in {"docx", "pdf"})
        or re.fullmatch(r"[0-9a-f]{64}", str(job_id)) is None
    ):
        raise ValueError("The document export request is invalid.")

    from .mcp_review import _revision

    epub_options = None
    if "epub_options" in payload:
        from carlina.core.epub_export import normalize_epub_options

        try:
            epub_options = normalize_epub_options(payload["epub_options"])
        except ValueError:
            raise ValueError("The document export request is invalid.") from None
        if export_format != "epub":
            raise ValueError("The document export request is invalid.")
    epub_review_hash = payload.get("epub_review_hash")
    if epub_review_hash is not None and (
        not isinstance(epub_review_hash, str)
        or re.fullmatch(r"[0-9a-f]{64}", epub_review_hash) is None
        or export_format != "epub"
        or epub_options is None
    ):
        raise ValueError("The document export request is invalid.")

    document = (
        ProcessedDocument.query.populate_existing()
        .filter_by(id=document_id, user_id=user_id)
        .first()
    )
    if (
        document is None
        or document.status != "completed"
        or not isinstance(document.output_html, str)
        or not document.output_html
    ):
        raise RuntimeError("The document export is no longer available.")
    if _revision(document.output_html) != expected_revision:
        raise RuntimeError("The extraction changed before export rendering.")

    # Queue progress mirrors commit the worker's scoped SQLAlchemy session.
    # Keep the checked row values transient so those commits cannot refresh a
    # later edit into this already-admitted export.
    document_snapshot = _mcp_export_document_snapshot(document)
    if epub_review_hash is not None:
        from .epub_quality import direct_epub_review_sha256
        from .metadata_service import get_canonical_document_metadata

        if epub_options is None:
            raise ValueError("The document export request is invalid.")
        if direct_epub_review_sha256(
            expected_revision,
            get_canonical_document_metadata(document_snapshot),
            epub_options,
            cover=cover_options,
            image_mode=epub_image_mode,
        ) != epub_review_hash:
            task_queue.set_status(job_id, {"error_code": "epub-review-stale"})
            raise RuntimeError("The EPUB choices changed before rendering.")
    base_name = str(document_snapshot.filename or "processed_document").rsplit(
        ".", 1
    )[0]
    safe_name = secure_filename(base_name) or "processed_document"
    download_name = f"{safe_name}.{export_format}"
    prepared = _prepare_mcp_export_directory(str(job_id))
    if isinstance(prepared, WindowsArtifactDirectory):
        operation_path = prepared.path
        operation_fd: int | WindowsArtifactDirectory = prepared
    else:
        operation_path, operation_fd = prepared
    stage_name = None
    published = False
    try:
        stage_name, stage_path, final_name, final_path = _stage_mcp_export(
            operation_fd, operation_path, export_format, download_name
        )
        task_queue.set_status(
            job_id,
            {
                "status": "processing",
                "progress": 10,
                "message": "Preparing document export...",
            },
        )
        if export_format in {"md", "html"}:
            from .routes import (
                _render_text_document_export,
            )

            content, media_type = _render_text_document_export(
                document_snapshot, export_format
            )
            if (
                not isinstance(content, str)
                or media_type != _MCP_EXPORT_MIMETYPES[export_format]
            ):
                raise RuntimeError("The document export renderer returned invalid output.")
            stage_path.write_bytes(content.encode("utf-8"))
            rendered = {
                "kind": "file_download",
                "output_path": str(stage_path),
                "download_name": download_name,
                "mimetype": media_type,
                "user_id": user_id,
            }
        else:
            render_payload = dict(payload)
            if epub_options is not None:
                render_payload["epub_options"] = epub_options
            rendered = export_document(
                render_payload,
                task_queue,
                job_id,
                output_path=stage_path,
                document_snapshot=document_snapshot,
                cleanup_failed_output=not isinstance(
                    operation_fd, WindowsArtifactDirectory
                ),
            )
        if (
            not isinstance(rendered, dict)
            or rendered.get("kind") != "file_download"
            or rendered.get("output_path") != str(stage_path)
            or rendered.get("download_name") != download_name
            or rendered.get("mimetype") != _MCP_EXPORT_MIMETYPES[export_format]
            or type(rendered.get("user_id")) is not int
            or rendered.get("user_id") != user_id
            or (
                epub_review_hash is not None
                and rendered.get("epub_review_hash") != epub_review_hash
            )
        ):
            raise RuntimeError("The document export renderer returned invalid output.")
        task_queue.set_status(
            job_id,
            {
                "status": "processing",
                "progress": 95,
                "message": "Finalizing document export...",
            },
        )
        byte_length, sha256 = _publish_mcp_export(
            operation_fd, stage_name, final_name
        )
        published = True
        result = {
            "kind": "file_download",
            "user_id": document_snapshot.user_id,
            "document_id": document_snapshot.id,
            "operation_id": str(job_id),
            "format": export_format,
            "revision": expected_revision,
            "download_name": download_name,
            "mimetype": _MCP_EXPORT_MIMETYPES[export_format],
            "output_path": str(final_path),
            "byte_length": byte_length,
            "sha256": sha256,
        }
        if rendered.get("quality") is not None:
            result["quality"] = rendered["quality"]
        if epub_review_hash is not None:
            result["epub_review_hash"] = epub_review_hash
        return result
    finally:
        if not published and stage_name is not None:
            if isinstance(operation_fd, WindowsArtifactDirectory):
                try:
                    operation_fd.remove_stage(stage_name)
                except OSError:
                    logger.warning("Unable to clean the document export staging file.")
            else:
                try:
                    os.unlink(stage_name, dir_fd=operation_fd)
                except FileNotFoundError:
                    pass
                except OSError:
                    logger.warning("Unable to clean the document export staging file.")
        if isinstance(operation_fd, WindowsArtifactDirectory):
            operation_fd.close()
        else:
            os.close(operation_fd)


@worker.handler("export_document")
def export_document(
    payload: dict,
    task_queue,
    job_id: str,
    *,
    output_path: Path | None = None,
    document_snapshot: ProcessedDocument | None = None,
    cleanup_failed_output: bool = True,
):
    output_path_override = output_path
    if output_path_override is not None and (
        not isinstance(output_path_override, Path)
        or not output_path_override.is_absolute()
    ):
        raise ValueError("The internal export output path is invalid.")
    fmt = payload.get("format")
    if fmt not in ["epub", "docx", "pdf"]:
        raise ValueError("Unsupported export format.")
    start_h2_on_new_page = payload.get("start_h2_on_new_page", False)
    if type(start_h2_on_new_page) is not bool or (
        start_h2_on_new_page and fmt not in {"docx", "pdf"}
    ):
        raise ValueError("Invalid heading pagination option.")

    document_id = payload.get("document_id")
    if document_snapshot is None:
        doc = db.session.get(ProcessedDocument, document_id)
    elif (
        not isinstance(document_snapshot, ProcessedDocument)
        or not sa_inspect(document_snapshot).transient
        or document_snapshot.id != document_id
    ):
        raise ValueError("The internal document snapshot is invalid.")
    else:
        doc = document_snapshot
    if not doc:
        raise ValueError(f"Document {document_id} not found.")

    expected_user_id = payload.get("user_id")
    if expected_user_id is not None and int(expected_user_id) != int(doc.user_id):
        raise PermissionError("Document export is not available.")

    epub_options = None
    publishing_revision: str | None = None
    if "epub_options" in payload:
        from carlina.core.epub_export import normalize_epub_options

        try:
            epub_options = normalize_epub_options(payload["epub_options"])
        except ValueError:
            raise ValueError("Invalid EPUB publishing options.") from None
        if fmt != "epub":
            raise ValueError("Invalid EPUB publishing options.")
        publishing_revision = payload.get("expected_revision")
        from .mcp_review import _revision

        if (
            not isinstance(publishing_revision, str)
            or _revision(doc.output_html or "") != publishing_revision
        ):
            raise RuntimeError("The extraction changed before export rendering.")

    epub_review_hash = payload.get("epub_review_hash")
    if epub_review_hash is not None and (
        not isinstance(epub_review_hash, str)
        or re.fullmatch(r"[0-9a-f]{64}", epub_review_hash) is None
        or fmt != "epub"
        or epub_options is None
    ):
        raise ValueError("Invalid EPUB review witness.")

    from werkzeug.utils import secure_filename

    safe_name = secure_filename(doc.filename.rsplit(".", 1)[0]) or "processed_document"

    if fmt == "epub":
        from carlina.core.epub_export import normalize_epub_image_mode

        epub_image_mode = normalize_epub_image_mode(payload.get("epub_image_mode", "auto"))
        include_source_toc = payload.get("include_source_toc") is True
        if epub_review_hash is not None:
            from carlina.core.epub_cover import normalize_epub_cover

            from .epub_quality import direct_epub_review_sha256

            if not isinstance(publishing_revision, str) or epub_options is None:
                raise RuntimeError("The extraction revision is unavailable.")
            try:
                cover_options = normalize_epub_cover(payload.get("epub_cover"))
                current_review_hash = direct_epub_review_sha256(
                    publishing_revision,
                    get_canonical_document_metadata(doc),
                    epub_options,
                    cover=cover_options,
                    image_mode=epub_image_mode,
                )
            except ValueError:
                raise ValueError("Invalid EPUB review inputs.") from None
            if current_review_hash != epub_review_hash:
                task_queue.set_status(job_id, {"error_code": "epub-review-stale"})
                raise RuntimeError("The EPUB choices changed before rendering.")
        task_queue.set_status(
            job_id,
            {
                "status": "processing",
                "progress": 5,
                "message": "Preparing EPUB export...",
            },
        )
        try:
            from iarf_assistant.addons.doc_processor.epub_packager import EPUBPackager
            from iarf_assistant.addons.doc_processor.routes import _validate_epub_file
        except ImportError as exc:
            raise RuntimeError("EPUBPackager could not be imported.") from exc

        output_filename = f"{safe_name}.epub"
        output_path = output_path_override or (
            Path(tempfile.gettempdir()) / f"{safe_name}-{job_id}.epub"
        )

        try:
            task_queue.set_status(
                job_id,
                {
                    "status": "processing",
                    "progress": 35,
                    "message": "Building native EPUB 3 archive...",
                },
            )
            packager_options = {
                "image_mode": epub_image_mode,
                "include_source_toc": include_source_toc,
                "cover": payload.get("epub_cover"),
            }
            if epub_options is not None:
                packager_options["options"] = epub_options
            packager = EPUBPackager(
                doc,
                str(output_path),
                **packager_options,
            )
            packager.build()

            task_queue.set_status(
                job_id,
                {
                    "status": "processing",
                    "progress": 90,
                    "message": "Validating EPUB...",
                },
            )
            quality = None
            if epub_options is None:
                is_valid_epub, validation_error = _validate_epub_file(
                    str(output_path)
                )
                if not is_valid_epub:
                    raise RuntimeError(
                        f"EPUB validation failed: {validation_error}"
                    )
            else:
                from flask import current_app, has_app_context

                from .epub_validation import validate_epub_file

                validation = validate_epub_file(
                    str(output_path),
                    config=current_app.config if has_app_context() else None,
                )
                if not validation.passed:
                    raise RuntimeError("EPUB validation failed.")
                quality = packager.quality_report(
                    structure="passed",
                    epubcheck=validation.epubcheck,
                    epubcheck_version=validation.version,
                    ace=validation.ace,
                    ace_version=validation.ace_version,
                )
                if not isinstance(quality, dict):
                    raise RuntimeError("EPUB quality report is unavailable.")
                if not isinstance(publishing_revision, str):
                    raise RuntimeError("The extraction revision is unavailable.")
                quality["input_sha256"] = publishing_revision

            result = {
                "kind": "file_download",
                "output_path": str(output_path),
                "download_name": output_filename,
                "mimetype": "application/epub+zip",
                "user_id": doc.user_id,
            }
            if quality is not None:
                result["quality"] = quality
            if epub_review_hash is not None:
                result["epub_review_hash"] = epub_review_hash
            return result
        except Exception:
            logger.exception(
                "EPUB 3 native packager export failed for document %s", document_id
            )
            if cleanup_failed_output:
                try:
                    output_path.unlink(missing_ok=True)
                except Exception:
                    pass
            raise

    elif fmt == "pdf":
        from .print_export import render_pdf_export
        from .routes import _embed_local_images_in_html, _sanitize_document_html
        from bs4 import BeautifulSoup

        output_path = output_path_override or (
            Path(tempfile.gettempdir()) / f"{safe_name}-{job_id}.pdf"
        )
        try:
            task_queue.set_status(job_id, {
                "status": "processing", "progress": 30, "message": "Preparing PDF export...",
            })
            raw_html = doc.output_html or markdown_to_html_on_the_fly(doc.output_markdown or "")
            soup = BeautifulSoup(_sanitize_document_html(raw_html), "html.parser")
            _embed_local_images_in_html(soup, doc)
            metadata = get_canonical_document_metadata(doc)
            metadata["title"] = get_document_display_title(doc, metadata) or safe_name
            runner = getattr(_get_client(int(doc.user_id)).context, "run_managed_process", None)
            if not callable(runner):
                raise RuntimeError("Document PDF export runtime is unavailable.")
            output_path.write_bytes(render_pdf_export(
                str(soup), metadata=metadata, run_managed_process=runner,
                start_h2_on_new_page=start_h2_on_new_page,
            ))
            return {
                "kind": "file_download", "output_path": str(output_path),
                "download_name": f"{safe_name}.pdf", "mimetype": "application/pdf",
                "user_id": doc.user_id,
            }
        except Exception:
            if cleanup_failed_output:
                output_path.unlink(missing_ok=True)
            raise

    elif fmt == "docx":
        task_queue.set_status(
            job_id,
            {
                "status": "processing",
                "progress": 5,
                "message": "Preparing DOCX export...",
            },
        )
        try:
            import pypandoc

            from iarf_assistant.addons.doc_processor.docx_preprocessor import (
                preprocess_html_for_docx,
            )
            from iarf_assistant.addons.doc_processor.routes import _resolve_local_path
        except ImportError as exc:
            raise RuntimeError(
                "Required modules for DOCX export could not be imported."
            ) from exc

        output_filename = f"{safe_name}.docx"
        output_path = output_path_override or (
            Path(tempfile.gettempdir()) / f"{safe_name}-{job_id}.docx"
        )

        try:
            task_queue.set_status(
                job_id,
                {
                    "status": "processing",
                    "progress": 30,
                    "message": "Preprocessing HTML content...",
                },
            )

            raw_html = doc.output_html or markdown_to_html_on_the_fly(
                doc.output_markdown or ""
            )
            uploads_dir = uploads_root()
            preprocessed_html = preprocess_html_for_docx(
                raw_html,
                doc_file_path=doc.file_path,
                uploads_dir=uploads_dir,
                user_id=doc.user_id,
                settings=doc.settings,
            )

            task_queue.set_status(
                job_id,
                {
                    "status": "processing",
                    "progress": 50,
                    "message": "Converting to DOCX with Pandoc...",
                },
            )

            source_path = _resolve_local_path(doc.file_path)
            resource_paths = [tempfile.gettempdir()]
            if source_path:
                resource_paths.append(str(source_path.parent))

            extra_args = [
                "--quiet",
                f"--resource-path={os.pathsep.join(resource_paths)}",
            ]

            document_metadata = get_canonical_document_metadata(doc)
            doc_title = get_document_display_title(doc, document_metadata) or safe_name
            extra_args.append(f"--metadata=title:{doc_title}")

            authors = get_primary_creator_names(document_metadata)
            if authors:
                extra_args.append(
                    f"--metadata=author:{', '.join(str(author) for author in authors)}"
                )
            if document_metadata.get("publisher"):
                extra_args.append(
                    f"--metadata=publisher:{document_metadata.get('publisher')}"
                )
            if document_metadata.get("publication_date"):
                extra_args.append(
                    f"--metadata=date:{document_metadata.get('publication_date')}"
                )

            # Use the immutable styling reference shipped with the add-on.
            extra_args.append(f"--reference-doc={ensure_default_reference_doc()}")

            pypandoc.convert_text(
                preprocessed_html,
                to="docx",
                format="html",
                outputfile=str(output_path),
                extra_args=extra_args,
            )
            from .docx_notes import preserve_repeated_docx_notes
            from .print_export import finish_docx_export

            data = preserve_repeated_docx_notes(output_path.read_bytes(), preprocessed_html)
            output_path.write_bytes(finish_docx_export(
                data, preprocessed_html, title=doc_title,
                start_h2_on_new_page=start_h2_on_new_page,
            ))

            task_queue.set_status(
                job_id,
                {
                    "status": "processing",
                    "progress": 95,
                    "message": "Finalizing DOCX...",
                },
            )
            return {
                "kind": "file_download",
                "output_path": str(output_path),
                "download_name": output_filename,
                "mimetype": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "user_id": doc.user_id,
            }
        except Exception:
            logger.exception("DOCX export failed for document %s", document_id)
            if cleanup_failed_output:
                try:
                    output_path.unlink(missing_ok=True)
                except Exception:
                    pass
            raise


@worker.handler("prepare_crop_review")
@_with_passive_execution
def prepare_crop_review(payload: dict, task_queue, job_id: str):
    document_id = payload.get("document_id")
    doc = db.session.get(ProcessedDocument, document_id)
    if not doc:
        raise ValueError(f"Document {document_id} not found.")

    # Queued jobs can outlive the request's source check. Record a terminal
    # preflight failure before publishing processing progress.
    try:
        pdf_path = _document_source_path(doc)
    except FileNotFoundError as exc:
        logger.warning(
            "Queued crop review source is missing for document %s.", document_id
        )
        if _mark_document_error(
            document_id, exc, payload=payload, job_id=job_id,
            preflight_snapshot=_document_preflight_snapshot(doc),
        ):
            _sync_project_entry(payload, "error", task_queue, exc)
        raise

    task_queue.set_status(
        job_id,
        {"status": "processing", "progress": 0, "message": "Preparing crop review..."},
    )

    try:
        doc.status = "preparing_crop_review"
        doc.settings = doc.settings or {}
        doc.settings["progress"] = 0
        doc.settings["progress_msg"] = "Analyzing first pages for crop review..."
        doc.settings["active_job_id"] = job_id
        flag_modified(doc, "settings")
        db.session.commit()

        import fitz

        from iarf_assistant.addons.doc_processor.routes import (
            _ensure_crop_review_metadata_suggestions,
        )

        pdf_doc = None
        try:
            pdf_doc = fitz.open(pdf_path)
            _ensure_crop_review_metadata_suggestions(
                doc,
                pdf_doc,
                len(pdf_doc),
                progress_callback=_make_document_progress_callback(
                    doc, task_queue, job_id
                ),
            )
        finally:
            if pdf_doc is not None:
                pdf_doc.close()

        doc = db.session.get(ProcessedDocument, document_id)
        if not doc:
            raise ValueError(
                f"Document {document_id} not found after crop review preparation."
            )

        doc.status = "needs_cropping"
        doc.error_message = None
        doc.settings = doc.settings or {}
        doc.settings.pop("active_job_id", None)
        doc.settings.pop("progress", None)
        doc.settings.pop("progress_msg", None)
        flag_modified(doc, "settings")
        db.session.commit()

        _sync_project_entry(payload, "needs_cropping", task_queue)
        return {"message": "Crop review is ready."}
    except Exception as exc:
        logger.exception(
            "Error while preparing crop review for document %s", document_id
        )
        if _mark_document_error(document_id, exc, payload=payload, job_id=job_id):
            _sync_project_entry(payload, "error", task_queue, exc)
        raise


@worker.handler("process_document")
@_with_passive_execution
def process_document(payload: dict, task_queue, job_id: str):
    document_id = payload.get("document_id")
    doc = db.session.get(ProcessedDocument, document_id)
    if not doc:
        raise ValueError(f"Document {document_id} not found.")

    from .execution_recovery import validate_delivery

    # A retained queue envelope may outlive cancellation or a newer attempt.
    # Check it before any progress callback can commit document settings.
    if not validate_delivery(doc, payload, job_id, None):
        return {"superseded": True, "message": "The extraction attempt is no longer current."}

    # Resolve before queue status or document state changes so stale records
    # fail closed without becoming falsely active.
    try:
        source = _document_source_path(doc)
        if not validate_delivery(doc, payload, job_id, source):
            return {"superseded": True, "message": "The extraction attempt is no longer current."}
    except Exception as exc:
        if _suspend_recoverable_failure(document_id, payload, job_id, exc):
            return {"suspended": True, "resumable": True}
        logger.warning(
            "Queued processing source is missing for document %s.", document_id
        )
        if _mark_document_error(
            document_id, exc, payload=payload, job_id=job_id,
            preflight_snapshot=_document_preflight_snapshot(doc),
        ):
            _sync_project_entry(payload, "error", task_queue, exc)
        raise

    _wait_for_unchanged_passive_checkpoint(doc, task_queue, job_id)

    requested_engine = payload.get("engine_id") or (doc.settings or {}).get(
        "engine_id", "auto"
    )
    task_queue.set_status(
        job_id,
        {"status": "processing", "progress": 0, "message": "Parsing document..."},
    )

    try:
        if _reserve_passive_write(doc, job_id):
            db.session.refresh(doc)
        doc.status = "processing"
        doc.settings = doc.settings or {}
        doc.settings["engine_id"] = requested_engine
        doc.settings["active_job_id"] = job_id
        doc.settings["progress"] = 0
        doc.settings["progress_msg"] = "Parsing document..."
        flag_modified(doc, "settings")
        db.session.commit()

        result = (
            _process_with_native_parser(doc, task_queue, job_id)
            if requested_engine == "llm_vision"
            else _process_with_external_engine(
                doc, requested_engine, task_queue, job_id
            )
        )

        action = result.get("control_action")
        review_paused = bool(result.get("paused")) or (
            bool(result.get("interrupted")) and action == "pause"
        )
        frozen = _reserve_passive_write(
            doc, job_id, statuses=("reviewing_images",) if result.get("paused") else ("processing",)
        )
        if frozen and result.get("interrupted") and action == "pause":
            from .execution_recovery import suspend_execution

            if suspend_execution(document_id, payload.get("mcp_execution_id"), job_id, {"error_code": "user_paused"}):
                return {"suspended": True, "resumable": True}
        if not review_paused and not _reserve_document_result_persistence(
            doc, payload, job_id
        ):
            current = db.session.get(ProcessedDocument, document_id)
            current_status = str(getattr(current, "status", "") or "")
            if current_status == "cancelled":
                _sync_project_entry(payload, "cancelled", task_queue)
            return {
                "message": "Document result was not published because the active run changed.",
                "superseded": True,
                "state": current_status or "unknown",
            }

        if result.get("paused"):
            doc.settings = doc.settings or {}
            doc.settings.pop("active_job_id", None)
            doc.settings.pop("progress", None)
            doc.settings.pop("progress_msg", None)
            flag_modified(doc, "settings")
            db.session.commit()
            _sync_project_entry(payload, "reviewing_images", task_queue)
            return {"message": result["message"]}

        if result.get("interrupted"):
            action = result.get("control_action")
            _store_semantic_output(doc, result.get("html", ""), completed=False)
            doc.status = "paused" if action == "pause" else "cancelled"
            doc.error_message = None
            doc.settings = doc.settings or {}
            doc.settings["resolved_engine_id"] = requested_engine
            doc.settings["pdf_state"] = result.get("pdf_state", {})
            doc.settings.pop("active_job_id", None)
            doc.settings.pop("progress", None)
            doc.settings.pop("progress_msg", None)
            if action != "pause":
                _clear_mcp_execution_binding(doc)
            flag_modified(doc, "settings")
            db.session.commit()
            _sync_project_entry(payload, doc.status, task_queue)
            return {
                "message": "Document extraction paused."
                if action == "pause"
                else "Document extraction stopped.",
                "resumable": True,
            }

        if result.get("partial_error"):
            failed_pages = ((doc.settings or {}).get("metrics", {}) or {}).get(
                "failed_pages", []
            )
            failed_pages_text = ", ".join(str(page) for page in failed_pages[:10])
            if len(failed_pages) > 10:
                failed_pages_text += ", ..."

            _store_semantic_output(doc, result.get("html", ""), completed=False)
            doc.status = "error"
            doc.error_message = (
                "Some pages failed to process."
                + (f" Failed pages: {failed_pages_text}." if failed_pages_text else "")
                + " Adjust settings or switch the vision model, then resume processing."
            )
            doc.settings = doc.settings or {}
            doc.settings["resolved_engine_id"] = requested_engine
            doc.settings["pdf_state"] = result.get("pdf_state", {})
            doc.settings.pop("active_job_id", None)
            doc.settings.pop("progress", None)
            doc.settings.pop("progress_msg", None)
            _clear_mcp_execution_binding(doc)
            flag_modified(doc, "settings")
            db.session.commit()
            _sync_project_entry(payload, "error", task_queue, doc.error_message)
            return {"message": "Document processing halted with partial errors."}

        _store_semantic_output(
            doc,
            result.get("html", ""),
            completed=True,
            image_crop_sources=result.get("image_crop_sources", {}),
        )
        if (doc.settings or {}).get("mcp_extraction_issues_policy") == _EXTRACTION_ISSUES_POLICY:
            _import_extraction_issues(doc, result.get("extraction_issues", []))
            doc.settings.pop(_EXTRACTION_ISSUES_CHECKPOINT_KEY, None)
        doc.status = "completed"
        doc.error_message = None
        doc.settings = doc.settings or {}
        doc.settings["resolved_engine_id"] = result.get(
            "resolved_engine", requested_engine
        )
        doc.settings.pop("pdf_state", None)
        doc.settings.pop("active_job_id", None)
        doc.settings.pop("progress", None)
        doc.settings.pop("progress_msg", None)
        _clear_mcp_execution_binding(doc)
        flag_modified(doc, "settings")
        sync_processed_document_source(doc)
        db.session.commit()

        _sync_project_entry(payload, "completed", task_queue)
        return {"message": "Document parsed successfully."}
    except Exception as exc:
        logger.exception("Error while processing document %s", document_id)
        from sqlalchemy.exc import OperationalError
        from ...database_utils import is_database_locked
        from ...core.task_queue import JobRetryLaterError

        if isinstance(exc, OperationalError) and is_database_locked(exc):
            db.session.rollback()
            raise JobRetryLaterError("Database busy; extraction progress is retained.", delay_seconds=5) from exc
        if _suspend_recoverable_failure(document_id, payload, job_id, exc):
            _sync_project_entry(payload, "paused", task_queue)
            # The old delivery can finish; the execution stays suspended.
            # Resume creates a separately fenced queue attempt.
            return {"suspended": True, "resumable": True}
        if _mark_document_error(document_id, exc, payload=payload, job_id=job_id):
            _sync_project_entry(payload, "error", task_queue, exc)
        raise


@worker.handler("resume_pdf_parsing")
@_with_passive_execution
def resume_pdf_parsing(payload: dict, task_queue, job_id: str):
    document_id = payload.get("document_id")
    doc = db.session.get(ProcessedDocument, document_id)
    if not doc:
        raise ValueError(f"Document {document_id} not found.")

    review_state = (doc.settings or {}).get("pdf_state")
    if not is_complete_pdf_image_review_state(review_state):
        exc = ValueError(
            "PDF image-review resume rejected because one or more selected pages are missing."
        )
        if _mark_document_error(
            document_id, exc, payload=payload, job_id=job_id,
            preflight_snapshot=_document_preflight_snapshot(doc),
        ):
            _sync_project_entry(payload, "error", task_queue, exc)
        raise exc

    try:
        source_path = _document_source_path(doc)
    except FileNotFoundError as exc:
        logger.warning(
            "Queued PDF resume source is missing for document %s.", document_id
        )
        if _mark_document_error(
            document_id, exc, payload=payload, job_id=job_id,
            preflight_snapshot=_document_preflight_snapshot(doc),
        ):
            _sync_project_entry(payload, "error", task_queue, exc)
        raise

    task_queue.set_status(
        job_id, {"status": "processing", "message": "Resuming PDF parsing..."}
    )
    doc.status = "processing"
    doc.settings = dict(doc.settings or {})
    doc.settings["active_job_id"] = job_id
    doc.settings["progress_msg"] = "Resuming PDF parsing..."
    flag_modified(doc, "settings")
    db.session.commit()

    llm_service = LLMService(user_id=doc.user_id)
    _reasoning_run_metadata(doc.settings)
    parser = get_parser(
        str(source_path),
        doc.user_id,
        llm_service,
        settings=_parser_settings(doc.settings),
        state=(doc.settings or {}).get("pdf_state"),
    )
    if (doc.settings or {}).get("mcp_extraction_issues_policy") == _EXTRACTION_ISSUES_POLICY:
        parser.extraction_issues_by_page = _load_extraction_issues_checkpoint(doc)

    try:
        import time

        start_time = time.time()
        status, html_content = parser.resume_processing()
        extraction_issue_pages = []
        if (doc.settings or {}).get("mcp_extraction_issues_policy") == _EXTRACTION_ISSUES_POLICY:
            extraction_issue_pages = _save_extraction_issues_checkpoint(doc, parser)
        metrics = dict(getattr(parser, "metrics", {}) or {})
        previous_duration = float(metrics.get("duration_seconds", 0.0) or 0.0)
        metrics["duration_seconds"] = round(
            previous_duration + (time.time() - start_time), 2
        )

        if not _reserve_document_result_persistence(doc, payload, job_id):
            current = db.session.get(ProcessedDocument, document_id)
            current_status = str(getattr(current, "status", "") or "")
            if current_status == "cancelled":
                _sync_project_entry(payload, "cancelled", task_queue)
            return {
                "message": "PDF result was not published because the active run changed.",
                "superseded": True,
                "state": current_status or "unknown",
            }

        _store_semantic_output(
            doc,
            html_content or "",
            completed=True,
            image_crop_sources=getattr(parser, "image_crop_sources", {}),
        )
        if (doc.settings or {}).get("mcp_extraction_issues_policy") == _EXTRACTION_ISSUES_POLICY:
            _import_extraction_issues(doc, extraction_issue_pages)
            doc.settings.pop(_EXTRACTION_ISSUES_CHECKPOINT_KEY, None)
        doc.status = "completed"
        doc.error_message = None

        if not doc.settings:
            doc.settings = {}
        doc.settings["metrics"] = metrics
        doc.settings["resolved_engine_id"] = "llm_vision"
        doc.settings.pop("pdf_state", None)
        doc.settings.pop("active_job_id", None)
        doc.settings.pop("progress", None)
        doc.settings.pop("progress_msg", None)
        _clear_mcp_execution_binding(doc)
        flag_modified(doc, "settings")

        sync_processed_document_source(doc)
        db.session.commit()
        _sync_project_entry(payload, "completed", task_queue)
        return {"message": "PDF parsing resumed and completed."}
    except Exception as exc:
        logger.exception(
            "Error while resuming PDF parsing for document %s", document_id
        )
        if _mark_document_error(document_id, exc, payload=payload, job_id=job_id):
            _sync_project_entry(payload, "error", task_queue, exc)
        raise


@worker.handler("export_project")
def export_project(payload: dict, task_queue, job_id: str):
    export_format = payload.get("format")
    if export_format not in ["epub", "docx", "pdf"]:
        raise ValueError("Unsupported export format.")
    start_h2_on_new_page = payload.get("start_h2_on_new_page", False)
    if type(start_h2_on_new_page) is not bool or (
        start_h2_on_new_page and export_format not in {"docx", "pdf"}
    ):
        raise ValueError("Invalid heading pagination option.")
    from carlina.core.epub_export import normalize_epub_image_mode

    epub_image_mode = normalize_epub_image_mode(payload.get("epub_image_mode", "auto"))
    include_source_toc = payload.get("include_source_toc") is True

    project_id = payload.get("project_id")
    from .models import DocumentProject

    project = db.session.get(DocumentProject, project_id)
    if not project:
        raise ValueError(f"Project {project_id} not found.")

    expected_user_id = payload.get("user_id")
    if expected_user_id is not None and int(expected_user_id) != int(project.user_id):
        raise PermissionError("Project export is not available.")

    task_queue.set_status(
        job_id,
        {
            "status": "processing",
            "progress": 5,
            "message": f"Preparing {export_format.upper()} export for project...",
        },
    )

    pypandoc = None
    if export_format == "docx":
        try:
            import pypandoc
        except ImportError as exc:
            raise RuntimeError("pypandoc is not installed on the worker.") from exc

    import html as html_lib
    import os
    import tempfile
    import zipfile

    from bs4 import BeautifulSoup

    from iarf_assistant.addons.doc_processor.format_utils import (
        markdown_to_html_on_the_fly,
    )
    from iarf_assistant.addons.doc_processor.routes import (
        _build_standalone_html,
        _embed_local_images_in_html,
        _normalize_footnote_refs_for_epub,
        _prepare_html_for_epub_export,
        _resolve_export_image_src,
        _resolve_local_path,
        _sanitize_document_html,
        _validate_epub_file,
    )
    from .print_export import (
        finish_docx_export, has_leading_source_title, render_pdf_export,
    )

    completed_entries = [
        entry
        for entry in project.documents
        if entry.document
        and entry.document.status in ["completed", "completed_with_errors"]
    ]
    if not completed_entries:
        raise ValueError("No completed documents in project to export.")

    from werkzeug.utils import secure_filename

    safe_name = secure_filename(project.name) or "project_export"
    export_mode = payload.get("mode", "concatenated")

    # 1. Concatenated Mode
    if export_mode == "concatenated":
        combined_body_html = []
        combined_metadata = []
        resource_paths = [tempfile.gettempdir()]
        allowed_image_roots = []

        for document_index, entry in enumerate(completed_entries, start=1):
            doc = entry.document
            raw_html = doc.output_html or markdown_to_html_on_the_fly(
                doc.output_markdown or ""
            )
            if not raw_html:
                continue

            if is_current_semantic_document(getattr(doc, "settings", None)):
                sanitized_html = semantic_result_for_stored_html(
                    raw_html,
                    doc.settings,
                ).html
            else:
                sanitized_html = canonicalize_document_html(
                    raw_html,
                    user_id=getattr(doc, "user_id", project.user_id),
                    source_file_path=doc.file_path,
                    relaxed=True,
                ).html
            soup = BeautifulSoup(sanitized_html, "html.parser")
            if str(doc.file_path or "").lower().endswith(".pdf"):
                from iarf_assistant.addons.doc_processor.parsers.pdf_utils import (
                    remove_source_toc_blocks,
                )

                remove_source_toc_blocks(soup)

            source_path = _resolve_local_path(doc.file_path)
            if source_path:
                resource_paths.append(str(source_path.parent))
                allowed_image_roots.append(source_path.parent)

            if export_format == "pdf":
                _embed_local_images_in_html(soup, doc)
            else:
                for img in soup.find_all("img"):
                    if img.get("src"):
                        img["src"] = _resolve_export_image_src(img.get("src"), source_path)

            if export_format == "epub":
                _normalize_footnote_refs_for_epub(soup)

            namespace_semantic_fragment(
                soup,
                namespace=f"project-doc-{document_index}",
            )

            document_metadata = get_canonical_document_metadata(doc)
            combined_metadata.append(document_metadata)
            doc_title = get_document_display_title(doc, document_metadata)
            source_has_title = has_leading_source_title(str(soup), doc_title)
            if not source_has_title:
                for level in range(5, 0, -1):
                    old_tag = f"h{level}"
                    new_tag = f"h{level + 1}"
                    for elem in soup.find_all(old_tag):
                        elem.name = new_tag

            section_html = (
                f'<section class="project-document" data-filename="{html_lib.escape(doc.filename)}">\n'
                + ("" if source_has_title else f"  <h1>{html_lib.escape(doc_title)}</h1>\n")
                + f"  {str(soup)}\n"
                + "</section>"
            )
            combined_body_html.append(section_html)

        body_content = '\n<hr class="project-separator" />\n'.join(combined_body_html)
        if export_format in {"epub", "pdf"}:
            from types import SimpleNamespace

            def unique_values(values):
                seen = set()
                result = []
                for value in values:
                    cleaned = str(value or "").strip()
                    if cleaned and cleaned.casefold() not in seen:
                        seen.add(cleaned.casefold())
                        result.append(cleaned)
                return result

            languages = unique_values(
                metadata.get("language") for metadata in combined_metadata
            )
            aggregate_contributors = {
                "authors": [],
                "editors": [],
                "translators": [],
                "other": [],
            }
            aggregate_subjects = []
            for metadata in combined_metadata:
                contributors = (
                    metadata.get("contributors")
                    if isinstance(metadata.get("contributors"), dict)
                    else {}
                )
                for role in ("authors", "editors", "translators"):
                    aggregate_contributors[role].extend(
                        contributors.get(role, []) or []
                    )
                aggregate_contributors["other"].extend(
                    contributors.get("other", []) or []
                )
                aggregate_subjects.extend(metadata.get("subjects", []) or [])
            for role in ("authors", "editors", "translators"):
                aggregate_contributors[role] = unique_values(
                    aggregate_contributors[role]
                )
            seen_other = set()
            unique_other = []
            for contributor in aggregate_contributors["other"]:
                if not isinstance(contributor, dict):
                    continue
                key = (
                    str(contributor.get("role") or "").casefold(),
                    str(contributor.get("name") or "").casefold(),
                )
                if key[1] and key not in seen_other:
                    seen_other.add(key)
                    unique_other.append(contributor)
            aggregate_contributors["other"] = unique_other

            project_metadata = {
                "title": project.name,
                "language": languages[0]
                if len(languages) == 1
                else "mul"
                if languages
                else "en",
                "contributors": aggregate_contributors,
                "subjects": unique_values(aggregate_subjects),
            }
            publishers = unique_values(
                metadata.get("publisher") for metadata in combined_metadata
            )
            rights = unique_values(
                metadata.get("rights") for metadata in combined_metadata
            )
            if len(publishers) == 1:
                project_metadata["publisher"] = publishers[0]
            if rights:
                project_metadata["rights"] = " | ".join(rights)

            if export_format == "pdf":
                output_path = Path(tempfile.gettempdir()) / f"{safe_name}-{job_id}.pdf"
                try:
                    runner = getattr(_get_client(int(project.user_id)).context, "run_managed_process", None)
                    if not callable(runner):
                        raise RuntimeError("Project PDF export runtime is unavailable.")
                    output_path.write_bytes(render_pdf_export(
                        body_content, metadata=project_metadata,
                        run_managed_process=runner,
                        start_h2_on_new_page=start_h2_on_new_page,
                    ))
                    return {
                        "kind": "file_download", "output_path": str(output_path),
                        "download_name": f"{safe_name}.pdf", "mimetype": "application/pdf",
                        "user_id": project.user_id,
                    }
                except Exception:
                    output_path.unlink(missing_ok=True)
                    raise

            from iarf_assistant.addons.doc_processor.epub_packager import EPUBPackager

            output_filename = f"{safe_name}.epub"
            output_path = Path(tempfile.gettempdir()) / f"{safe_name}-{job_id}.epub"
            virtual_document = SimpleNamespace(
                id=project.id,
                filename=output_filename,
                file_path="",
                settings={
                    "document_metadata": project_metadata,
                    "pdf_options": {"toc_handling": "omit"},
                },
                metadata_record=None,
                output_html=body_content,
                output_markdown=None,
            )
            try:
                task_queue.set_status(
                    job_id,
                    {
                        "status": "processing",
                        "progress": 35,
                        "message": "Building native EPUB 3 project archive...",
                    },
                )
                EPUBPackager(
                    virtual_document,
                    str(output_path),
                    image_mode=epub_image_mode,
                    include_source_toc=include_source_toc,
                    allowed_resource_roots=allowed_image_roots,
                ).build()
                is_valid_epub, validation_error = _validate_epub_file(str(output_path))
                if not is_valid_epub:
                    raise RuntimeError(f"EPUB validation failed: {validation_error}")
                return {
                    "kind": "file_download",
                    "output_path": str(output_path),
                    "download_name": output_filename,
                    "mimetype": "application/epub+zip",
                    "user_id": project.user_id,
                }
            except Exception:
                output_path.unlink(missing_ok=True)
                raise

        source_content = _build_standalone_html(body_content, project.name, "en")

        if export_format == "docx":
            from iarf_assistant.addons.doc_processor.docx_preprocessor import (
                preprocess_html_for_docx,
            )

            source_content = preprocess_html_for_docx(
                source_content,
                uploads_dir=uploads_root(),
                allowed_resource_roots=allowed_image_roots,
            )

        temp_html_path = None
        output_filename = f"{safe_name}.{export_format}"
        output_path = (
            Path(tempfile.gettempdir()) / f"{safe_name}-{job_id}.{export_format}"
        )

        extra_args = [
            "--quiet",
            f"--resource-path={os.pathsep.join(resource_paths)}",
        ]
        extra_args.append(f"--metadata=title:{project.name}")
        extra_args.append(f"--metadata=identifier:urn:uuid:{project.id}")
        extra_args.append("--metadata=lang:en")

        if export_format == "docx":
            extra_args.append(f"--reference-doc={ensure_default_reference_doc()}")

        try:
            with tempfile.NamedTemporaryFile(
                delete=False, suffix=".html", mode="w", encoding="utf-8"
            ) as temp_html:
                temp_html.write(source_content)
                temp_html_path = temp_html.name

            task_queue.set_status(
                job_id,
                {
                    "status": "processing",
                    "progress": 35,
                    "message": f"Building {export_format.upper()} with Pandoc...",
                },
            )
            pypandoc.convert_file(
                temp_html_path,
                to=export_format,
                format="html",
                outputfile=str(output_path),
                extra_args=extra_args,
            )
            from .docx_notes import preserve_repeated_docx_notes

            data = preserve_repeated_docx_notes(output_path.read_bytes(), source_content)
            output_path.write_bytes(finish_docx_export(
                data, body_content, title=project.name,
                start_h2_on_new_page=start_h2_on_new_page,
            ))

            mimetype = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

            return {
                "kind": "file_download",
                "output_path": str(output_path),
                "download_name": output_filename,
                "mimetype": mimetype,
                "user_id": project.user_id,
            }
        except Exception:
            logger.exception(
                "Pandoc %s export failed for project %s",
                export_format.upper(),
                project_id,
            )
            try:
                output_path.unlink(missing_ok=True)
            except Exception:
                pass
            raise
        finally:
            if temp_html_path and os.path.exists(temp_html_path):
                try:
                    os.unlink(temp_html_path)
                except Exception:
                    pass

    # 2. Separate Files ZIP Archive
    elif export_mode == "separate":
        zip_output_path = Path(tempfile.gettempdir()) / f"{safe_name}-{job_id}.zip"
        temp_exported_files = []

        try:
            total_docs = len(completed_entries)

            with zipfile.ZipFile(
                zip_output_path, "w", zipfile.ZIP_DEFLATED
            ) as zip_file:
                seen_filenames = set()

                for idx, entry in enumerate(completed_entries):
                    doc = entry.document
                    doc_safe_name = (
                        secure_filename(doc.filename.rsplit(".", 1)[0])
                        or f"document_{idx}"
                    )

                    progress_pct = int(5 + (idx / total_docs) * 85)
                    task_queue.set_status(
                        job_id,
                        {
                            "status": "processing",
                            "progress": progress_pct,
                            "message": f"Exporting individual {export_format.upper()} {idx + 1}/{total_docs}: {doc.filename}...",
                        },
                    )

                    raw_html = doc.output_html or markdown_to_html_on_the_fly(
                        doc.output_markdown or ""
                    )
                    if not raw_html:
                        continue

                    individual_path = (
                        Path(tempfile.gettempdir())
                        / f"{doc_safe_name}-{job_id}-{idx}.{export_format}"
                    )
                    temp_exported_files.append(individual_path)
                    if export_format == "epub":
                        from iarf_assistant.addons.doc_processor.epub_packager import (
                            EPUBPackager,
                        )

                        EPUBPackager(
                            doc,
                            str(individual_path),
                            image_mode=epub_image_mode,
                            include_source_toc=include_source_toc,
                        ).build()
                        is_valid_epub, validation_error = _validate_epub_file(
                            str(individual_path)
                        )
                        if not is_valid_epub:
                            raise RuntimeError(
                                f"EPUB validation failed for {doc.filename}: {validation_error}"
                            )

                        filename_in_zip = f"{doc_safe_name}.epub"
                        counter = 1
                        while filename_in_zip in seen_filenames:
                            filename_in_zip = f"{doc_safe_name}_{counter}.epub"
                            counter += 1
                        seen_filenames.add(filename_in_zip)
                        zip_file.write(str(individual_path), filename_in_zip)
                        continue

                    if export_format == "pdf":
                        soup = BeautifulSoup(_sanitize_document_html(raw_html), "html.parser")
                        _embed_local_images_in_html(soup, doc)
                        document_metadata = get_canonical_document_metadata(doc)
                        document_title = get_document_display_title(doc, document_metadata) or doc_safe_name
                        document_metadata["title"] = document_title
                        runner = getattr(_get_client(int(doc.user_id)).context, "run_managed_process", None)
                        if not callable(runner):
                            raise RuntimeError("Document PDF export runtime is unavailable.")
                        individual_path.write_bytes(render_pdf_export(
                            str(soup), metadata=document_metadata,
                            run_managed_process=runner,
                            start_h2_on_new_page=start_h2_on_new_page,
                        ))
                        filename_in_zip = f"{doc_safe_name}.pdf"
                        counter = 1
                        while filename_in_zip in seen_filenames:
                            filename_in_zip = f"{doc_safe_name}_{counter}.pdf"
                            counter += 1
                        seen_filenames.add(filename_in_zip)
                        zip_file.write(str(individual_path), filename_in_zip)
                        continue

                    document_metadata = get_canonical_document_metadata(doc)
                    document_title = (
                        get_document_display_title(doc, document_metadata)
                        or doc_safe_name
                    )

                    source_content, resource_paths = _prepare_html_for_epub_export(
                        raw_html,
                        document_title,
                        doc,
                        document_metadata,
                    )

                    if export_format == "docx":
                        from iarf_assistant.addons.doc_processor.docx_preprocessor import (
                            preprocess_html_for_docx,
                        )

                        source_content = preprocess_html_for_docx(
                            source_content,
                            doc_file_path=doc.file_path,
                            uploads_dir=uploads_root(),
                            user_id=doc.user_id,
                            settings=doc.settings,
                        )

                    temp_html_path = None
                    extra_args = [
                        "--quiet",
                        f"--resource-path={os.pathsep.join(resource_paths)}",
                    ]
                    if document_title:
                        extra_args.append(f"--metadata=title:{document_title}")

                    authors = get_primary_creator_names(document_metadata)
                    if authors:
                        extra_args.append(
                            f"--metadata=author:{', '.join(str(author) for author in authors)}"
                        )
                    if document_metadata.get("publisher"):
                        extra_args.append(
                            f"--metadata=publisher:{document_metadata.get('publisher')}"
                        )
                    if document_metadata.get("publication_date"):
                        extra_args.append(
                            f"--metadata=date:{document_metadata.get('publication_date')}"
                        )
                    extra_args.append(f"--metadata=identifier:urn:uuid:{doc.id}")
                    extra_args.append(
                        f"--metadata=lang:{document_metadata.get('language') or 'en'}"
                    )

                    if export_format == "docx":
                        extra_args.append(f"--reference-doc={ensure_default_reference_doc()}")

                    with tempfile.NamedTemporaryFile(
                        delete=False, suffix=".html", mode="w", encoding="utf-8"
                    ) as temp_html:
                        temp_html.write(source_content)
                        temp_html_path = temp_html.name

                    try:
                        pypandoc.convert_file(
                            temp_html_path,
                            to=export_format,
                            format="html",
                            outputfile=str(individual_path),
                            extra_args=extra_args,
                        )
                        from .docx_notes import preserve_repeated_docx_notes

                        data = preserve_repeated_docx_notes(individual_path.read_bytes(), source_content)
                        individual_path.write_bytes(finish_docx_export(
                            data, source_content, title=document_title,
                            start_h2_on_new_page=start_h2_on_new_page,
                        ))

                        filename_in_zip = f"{doc_safe_name}.{export_format}"
                        counter = 1
                        while filename_in_zip in seen_filenames:
                            filename_in_zip = (
                                f"{doc_safe_name}_{counter}.{export_format}"
                            )
                            counter += 1
                        seen_filenames.add(filename_in_zip)

                        zip_file.write(str(individual_path), filename_in_zip)

                    finally:
                        if temp_html_path and os.path.exists(temp_html_path):
                            try:
                                os.unlink(temp_html_path)
                            except Exception:
                                pass

            task_queue.set_status(
                job_id,
                {
                    "status": "processing",
                    "progress": 95,
                    "message": "Finalizing ZIP archive...",
                },
            )
            return {
                "kind": "file_download",
                "output_path": str(zip_output_path),
                "download_name": f"{safe_name}.zip",
                "mimetype": "application/zip",
                "user_id": project.user_id,
            }

        except Exception:
            logger.exception(
                "ZIP of %ss export failed for project %s",
                export_format.upper(),
                project_id,
            )
            try:
                zip_output_path.unlink(missing_ok=True)
            except Exception:
                pass
            raise
        finally:
            for p in temp_exported_files:
                if p.exists():
                    try:
                        p.unlink(missing_ok=True)
                    except Exception:
                        pass

    raise ValueError("Invalid export mode requested.")


if __name__ == "__main__":
    logger.info("Starting DocProcessor Background Worker...")
    run_worker_node(worker)
