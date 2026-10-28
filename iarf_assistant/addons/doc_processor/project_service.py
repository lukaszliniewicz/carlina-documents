from __future__ import annotations

import logging
import uuid
from copy import deepcopy
from pathlib import Path

from sqlalchemy.orm.attributes import flag_modified

from carlina.core.utc import utc_now_db
from iarf_assistant.core.upload_paths import resolve_upload_reference, uploads_root
from iarf_assistant.extensions import db

from .intake import build_effective_settings as _build_effective_settings
from .models import ProcessedDocument, ProjectRun, ProjectRunDocument

logger = logging.getLogger(__name__)
ACTIVE_STATUSES = {"scheduling", "preparing_crop_review", "processing"}
MANUAL_STATUSES = {"needs_cropping", "reviewing_images"}
TERMINAL_STATUSES = {"completed", "error", "cancelled"}
MAX_PROJECT_CONCURRENCY = 10
SOURCE_MISSING_ERROR = "Source file missing."
PROJECT_SCHEDULER_LEASE_SECONDS = 30.0
PROJECT_SCHEDULER_SIGNAL_SECONDS = 30.0


def build_effective_settings(
    document_settings: dict | None,
    overrides: dict | None = None,
    *,
    user=None,
) -> dict:
    """Compatibility export for the shared project settings contract."""

    return _build_effective_settings(document_settings, overrides, user=user)


def clear_project_document_activity(document: ProcessedDocument):
    if not isinstance(document.settings, dict):
        return
    document.settings.pop("active_project_run_document_id", None)
    document.settings.pop("active_job_id", None)
    document.settings.pop("progress", None)
    document.settings.pop("progress_msg", None)
    flag_modified(document, "settings")


def clear_project_document_job(document: ProcessedDocument):
    if not isinstance(document.settings, dict):
        return
    if "active_job_id" not in document.settings:
        return
    document.settings.pop("active_job_id", None)
    flag_modified(document, "settings")


def _initial_task(entry: ProjectRunDocument) -> tuple[str, str]:
    document = entry.document
    settings = entry.effective_settings or {}
    engine_id = settings.get("engine_id", "auto")
    pdf_options = settings.get("pdf_options") or {}
    is_pdf = str(document.file_path).lower().endswith(".pdf")
    crop_review = is_pdf and engine_id in {"auto", "llm_vision"} and not pdf_options.get("skip_cropping", False)
    if crop_review and pdf_options.get("metadata_prepass", True):
        return "prepare_crop_review", "preparing_crop_review"
    if crop_review:
        return "", "needs_cropping"
    return "process_document", "processing"


def _document_source_path(document: ProcessedDocument) -> Path | None:
    """Resolve a document source using the canonical, mutable uploads root."""
    try:
        source_path = resolve_upload_reference(
            document.file_path,
            uploads_dir=uploads_root(),
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return None
    if source_path is None or not source_path.is_file():
        return None
    return source_path


def _mark_source_missing(entry: ProjectRunDocument, document: ProcessedDocument | None = None):
    """Close a stale queue entry without leaving document activity markers behind."""
    entry.status = "error"
    entry.error_message = SOURCE_MISSING_ERROR
    entry.job_id = None
    entry.completed_at = utc_now_db()
    if document is not None:
        clear_project_document_activity(document)
    db.session.commit()


def _restore_dispatch_snapshot(
    entry: ProjectRunDocument,
    document: ProcessedDocument,
    *,
    entry_status: str,
    entry_started_at,
    entry_completed_at,
    entry_error_message,
    entry_job_id,
    document_status: str,
    document_settings: dict | None,
    document_error_message,
):
    """Undo an unqueued dispatch mutation after losing scheduler ownership."""

    entry.status = entry_status
    entry.started_at = entry_started_at
    entry.completed_at = entry_completed_at
    entry.error_message = entry_error_message
    entry.job_id = entry_job_id
    document.status = document_status
    document.settings = deepcopy(document_settings)
    document.error_message = document_error_message
    flag_modified(document, "settings")
    db.session.commit()


def refresh_project_run(run_id: str) -> ProjectRun | None:
    run = db.session.get(ProjectRun, run_id)
    if not run:
        return None

    statuses = [entry.status for entry in run.documents]
    now = utc_now_db()
    if statuses and all(status in TERMINAL_STATUSES for status in statuses):
        if run.stop_requested:
            run.status = "cancelled"
        elif any(status == "error" for status in statuses):
            run.status = "completed_with_errors"
        else:
            run.status = "completed"
        run.completed_at = run.completed_at or now
    elif run.pause_requested and not any(status in ACTIVE_STATUSES for status in statuses):
        run.status = "paused"
    elif run.stop_requested:
        run.status = "stopping"
    elif any(status in ACTIVE_STATUSES for status in statuses):
        run.status = "running"
    elif any(status in MANUAL_STATUSES for status in statuses):
        run.status = "waiting_for_review"
    else:
        run.status = "queued"
    db.session.commit()
    return run


def schedule_project_run(run_id: str, task_queue) -> ProjectRun | None:
    cache = getattr(task_queue, "cache", None)
    lock_key = f"project_schedule_lock:{run_id}"
    signal_key = f"project_schedule_wake:{run_id}"
    lock_token = uuid.uuid4().hex
    lock_store = (
        cache
        if cache is not None
        and callable(getattr(cache, "acquire_or_signal", None))
        and callable(getattr(cache, "heartbeat", None))
        and callable(getattr(cache, "renew_or_release", None))
        and callable(getattr(cache, "delete_if_value", None))
        else None
    )
    if lock_store is None:
        return _schedule_project_run_unlocked(run_id, task_queue)
    try:
        acquisition = lock_store.acquire_or_signal(
            lock_key,
            lock_token,
            expire=PROJECT_SCHEDULER_LEASE_SECONDS,
            signal_key=signal_key,
            signal_expire=PROJECT_SCHEDULER_SIGNAL_SECONDS,
        )
    except Exception:
        logger.warning(
            "Unable to acquire project scheduler lock for run %s.",
            run_id,
            exc_info=True,
        )
        return refresh_project_run(run_id)
    if acquisition != "acquired":
        return refresh_project_run(run_id)

    rerun_requested = False
    ownership_lost = False

    def heartbeat() -> bool:
        nonlocal ownership_lost, rerun_requested
        lease_state, demand_consumed = lock_store.heartbeat(
            lock_key,
            lock_token,
            expire=PROJECT_SCHEDULER_LEASE_SECONDS,
            signal_key=signal_key,
            signal_expire=PROJECT_SCHEDULER_SIGNAL_SECONDS,
        )
        if lease_state != "owned":
            ownership_lost = True
            return False
        rerun_requested = rerun_requested or demand_consumed
        return True

    try:
        while True:
            rerun_requested = False
            result = _schedule_project_run_unlocked(run_id, task_queue, heartbeat=heartbeat)
            if ownership_lost:
                return result
            if rerun_requested:
                continue
            lease_state = lock_store.renew_or_release(
                lock_key,
                lock_token,
                expire=PROJECT_SCHEDULER_LEASE_SECONDS,
                signal_key=signal_key,
                signal_expire=PROJECT_SCHEDULER_SIGNAL_SECONDS,
            )
            if lease_state != "signaled":
                return result
    except Exception:
        try:
            lock_store.delete_if_value(lock_key, lock_token)
        except Exception:
            logger.warning(
                "Unable to clean up project scheduler lock for run %s.",
                run_id,
                exc_info=True,
            )
        raise


def _schedule_project_run_unlocked(run_id: str, task_queue, heartbeat=None) -> ProjectRun | None:
    run = db.session.get(ProjectRun, run_id)
    if not run:
        return None
    if run.pause_requested or run.stop_requested or run.status in {"completed", "completed_with_errors", "cancelled"}:
        return refresh_project_run(run_id)

    active_count = ProjectRunDocument.query.filter(
        ProjectRunDocument.run_id == run.id,
        ProjectRunDocument.status.in_(ACTIVE_STATUSES),
    ).count()
    concurrency_limit = max(1, min(MAX_PROJECT_CONCURRENCY, int(run.concurrency_limit or 1)))
    slots = max(0, concurrency_limit - active_count)
    if not slots:
        return refresh_project_run(run_id)

    attempts_remaining = ProjectRunDocument.query.filter_by(run_id=run.id, status="queued").count()
    while slots > 0 and attempts_remaining > 0:
        candidate = (
            ProjectRunDocument.query.filter_by(run_id=run.id, status="queued")
            .order_by(ProjectRunDocument.position.asc())
            .first()
        )
        if not candidate:
            break
        attempts_remaining -= 1

        if heartbeat is not None and not heartbeat():
            return refresh_project_run(run_id)

        original_entry_status = candidate.status
        original_entry_started_at = candidate.started_at
        original_entry_completed_at = candidate.completed_at
        original_entry_error_message = candidate.error_message
        original_entry_job_id = candidate.job_id
        candidate_document = candidate.document
        original_document_status = candidate_document.status if candidate_document else "created"
        original_document_settings = deepcopy(candidate_document.settings) if candidate_document else None
        original_document_error = candidate_document.error_message if candidate_document else None

        # Preflight before claiming the entry.  A project can outlive its
        # uploaded source, and claiming a stale entry only creates a state
        # that can never be serviced.
        if candidate_document is None or _document_source_path(candidate_document) is None:
            _mark_source_missing(candidate, candidate_document)
            continue

        if heartbeat is not None and not heartbeat():
            return refresh_project_run(run_id)

        claimed = ProjectRunDocument.query.filter_by(id=candidate.id, status="queued").update(
            {"status": "scheduling", "started_at": candidate.started_at or utc_now_db()}
        )
        db.session.commit()
        if claimed != 1:
            continue

        if heartbeat is not None and not heartbeat():
            entry = db.session.get(ProjectRunDocument, candidate.id)
            if entry is not None and entry.document is not None:
                _restore_dispatch_snapshot(
                    entry,
                    entry.document,
                    entry_status=original_entry_status,
                    entry_started_at=original_entry_started_at,
                    entry_completed_at=original_entry_completed_at,
                    entry_error_message=original_entry_error_message,
                    entry_job_id=original_entry_job_id,
                    document_status=original_document_status,
                    document_settings=original_document_settings,
                    document_error_message=original_document_error,
                )
            return refresh_project_run(run_id)

        db.session.refresh(run)
        entry = db.session.get(ProjectRunDocument, candidate.id)
        if run.stop_requested or run.pause_requested:
            entry.status = "cancelled" if run.stop_requested else "queued"
            if run.stop_requested:
                entry.completed_at = utc_now_db()
            db.session.commit()
            break
        document = entry.document
        # The source may disappear after the claim (for example, cleanup of
        # an upload racing with the scheduler).  Fence every subsequent state
        # transition on a fresh canonical-file check.
        if document is None or _document_source_path(document) is None:
            _mark_source_missing(entry, document)
            continue

        if heartbeat is not None and not heartbeat():
            _restore_dispatch_snapshot(
                entry,
                document,
                entry_status=original_entry_status,
                entry_started_at=original_entry_started_at,
                entry_completed_at=original_entry_completed_at,
                entry_error_message=original_entry_error_message,
                entry_job_id=original_entry_job_id,
                document_status=original_document_status,
                document_settings=original_document_settings,
                document_error_message=original_document_error,
            )
            return refresh_project_run(run_id)

        document.settings = deepcopy(entry.effective_settings or {})
        document.settings["active_project_run_document_id"] = entry.id
        task_name, status = _initial_task(entry)
        entry.status = status
        document.status = status
        document.error_message = None
        flag_modified(document, "settings")
        db.session.commit()

        if heartbeat is not None and not heartbeat():
            _restore_dispatch_snapshot(
                entry,
                document,
                entry_status=original_entry_status,
                entry_started_at=original_entry_started_at,
                entry_completed_at=original_entry_completed_at,
                entry_error_message=original_entry_error_message,
                entry_job_id=original_entry_job_id,
                document_status=original_document_status,
                document_settings=original_document_settings,
                document_error_message=original_document_error,
            )
            return refresh_project_run(run_id)

        if not task_name:
            continue

        # Do not enqueue a job for a source deleted after document mutation.
        # Restore the document snapshot so no active-project marker remains.
        if _document_source_path(document) is None:
            document.status = original_document_status
            document.settings = original_document_settings
            document.error_message = original_document_error
            flag_modified(document, "settings")
            entry.status = "error"
            entry.error_message = SOURCE_MISSING_ERROR
            entry.job_id = None
            entry.completed_at = utc_now_db()
            db.session.commit()
            continue

        if heartbeat is not None and not heartbeat():
            _restore_dispatch_snapshot(
                entry,
                document,
                entry_status=original_entry_status,
                entry_started_at=original_entry_started_at,
                entry_completed_at=original_entry_completed_at,
                entry_error_message=original_entry_error_message,
                entry_job_id=original_entry_job_id,
                document_status=original_document_status,
                document_settings=original_document_settings,
                document_error_message=original_document_error,
            )
            return refresh_project_run(run_id)

        payload = {
            "document_id": document.id,
            "engine_id": (document.settings or {}).get("engine_id", "auto"),
            "project_run_document_id": entry.id,
            "user_id": run.user_id,
        }
        try:
            job_id = task_queue.enqueue(task_name, payload)
        except Exception:
            logger.exception(
                "Unable to enqueue project run document %s for run %s.",
                entry.id,
                run.id,
            )
            entry.status = "error"
            entry.error_message = "Processing could not be queued. Please try a new run."
            entry.completed_at = utc_now_db()
            document.status = "error"
            document.error_message = entry.error_message
            clear_project_document_activity(document)
            db.session.commit()
            continue

        if heartbeat is not None and not heartbeat():
            # The bounded enqueue has already happened.  Do not write a job
            # marker or dispatch another entry after ownership is lost; the
            # worker will synchronize this entry when it starts.
            return refresh_project_run(run_id)
        # Claim the persisted job marker with a conditional update.  This
        # closes the refresh-to-commit gap: a worker that synchronously moves
        # the entry to terminal/manual/paused state wins, and this scheduler
        # transaction must not write stale markers over it.
        claimed = ProjectRunDocument.query.filter(
            ProjectRunDocument.id == entry.id,
            ProjectRunDocument.status == status,
            ProjectRunDocument.job_id.is_(None),
        ).update({"job_id": job_id}, synchronize_session=False)
        if claimed != 1:
            db.session.rollback()
            continue
        db.session.expire(document)
        document.settings = dict(document.settings or {})
        document.settings["active_job_id"] = job_id
        flag_modified(document, "settings")
        db.session.commit()
        slots -= 1

    return refresh_project_run(run_id)


def sync_project_run_document(project_run_document_id: str | None, status: str, task_queue=None, error=None):
    if not project_run_document_id:
        return None
    entry = db.session.get(ProjectRunDocument, project_run_document_id)
    if not entry:
        return None

    if entry.run.stop_requested and status not in TERMINAL_STATUSES:
        status = "cancelled"
        if entry.document:
            entry.document.status = "cancelled"
    entry.status = status
    entry.error_message = str(error) if error else None
    if status not in ACTIVE_STATUSES:
        entry.job_id = None
        if entry.document:
            clear_project_document_job(entry.document)
    if entry.document and status != "completed":
        entry.effective_settings = deepcopy(entry.document.settings or {})
        entry.effective_settings.pop("active_job_id", None)
        entry.effective_settings.pop("active_project_run_document_id", None)
    if status in TERMINAL_STATUSES:
        entry.completed_at = utc_now_db()
        if entry.document:
            clear_project_document_activity(entry.document)
    db.session.commit()

    run_id = entry.run_id
    refresh_project_run(run_id)
    if task_queue is not None:
        schedule_project_run(run_id, task_queue)
    return entry


def attach_manual_review_job(document: ProcessedDocument, job_id: str, status: str):
    entry_id = (document.settings or {}).get("active_project_run_document_id")
    if not entry_id:
        return
    entry = db.session.get(ProjectRunDocument, entry_id)
    if not entry:
        return
    entry.status = status
    entry.job_id = job_id
    db.session.commit()


def find_and_link_active_project_run_document(document: ProcessedDocument) -> str | None:
    """
    Finds if this document is part of any active/incomplete project run,
    links it in the document settings, and returns the project run document ID.
    """
    if not document:
        return None

    from .models import ProjectRun, ProjectRunDocument
    active_query = (
        ProjectRunDocument.query
        .join(ProjectRun)
        .filter(
            ProjectRunDocument.document_id == document.id,
            ~ProjectRun.status.in_({"completed", "completed_with_errors", "cancelled"})
        )
    )
    existing_entry_id = (document.settings or {}).get("active_project_run_document_id")
    run_doc = active_query.filter(ProjectRunDocument.id == existing_entry_id).first() if existing_entry_id else None
    if run_doc is None:
        run_doc = active_query.order_by(ProjectRun.created_at.desc(), ProjectRunDocument.position.asc()).first()
    if run_doc:
        if document.settings is None:
            document.settings = {}
        document.settings["active_project_run_document_id"] = run_doc.id
        flag_modified(document, "settings")
        return run_doc.id
    return None
