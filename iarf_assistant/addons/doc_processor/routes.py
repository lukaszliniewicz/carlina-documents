from __future__ import annotations

import base64
import html as html_lib
import io
import json
import logging
import math
import os
import tempfile
import time
import uuid
from collections import OrderedDict
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from urllib.parse import unquote, urlparse

import numpy as np
from bs4 import BeautifulSoup
from flask import (
    Blueprint,
    Response,
    current_app,
    has_app_context,
    has_request_context,
    jsonify,
    request,
    send_file,
    url_for,
)
from flask_login import current_user, login_required
from PIL import Image, ImageFilter, ImageOps
from sqlalchemy.orm import selectinload
from sqlalchemy.orm.attributes import flag_modified
from werkzeug.utils import secure_filename

from carlina.core.export_notes import normalize_export_note_roles
from carlina.core.utc import utc_now_db
from iarf_assistant.core.upload_paths import resolve_upload_reference, uploads_root
from iarf_assistant.extensions import db
from iarf_assistant.models import User
from iarf_assistant.pages import render_frontend_shell

from . import epub_validation
from .format_utils import html_to_markdown_on_the_fly, markdown_to_html_on_the_fly
from .image_crops import (
    ImageCropError,
    build_completed_image_groups,
    crop_image_file,
    normalize_completed_crop_payload,
    render_pdf_page_png,
    retain_source_png,
    upload_url,
)
from .image_crops import (
    build_page_image_payload as _build_pending_page_image_payload,
)
from .image_crops import (
    normalize_image_review_payload as _normalize_pending_image_review_payload,
)
from .intake import (
    PROFILE_BOOK_QUALITY,
    get_processing_profile,
    validate_pdf_model_readiness,
    validate_pdf_model_readiness_batch,
)
from .intake import (
    build_document_settings as _shared_build_document_settings,
)
from .intake import (
    build_effective_settings as _shared_build_effective_settings,
)
from .intake import (
    normalize_pdf_processing_options as _shared_normalize_pdf_processing_options,
)
from .metadata_service import (
    extract_source_document_metadata,
    get_canonical_document_metadata,
    get_document_display_title,
    initialize_document_metadata,
    sync_document_metadata,
)
from .models import (
    DocumentProject,
    ProcessedDocument,
    ProjectDocument,
    ProjectRun,
    ProjectRunDocument,
)
from .pdf_recovery import is_complete_pdf_image_review_state
from .prompting import (
    PROMPT_FIELDS_BY_ID,
    DocProcessorPromptPreviewBuilder,
    build_prompt_editor_sections,
    normalize_prompt_text,
)
from .reasoning_snapshot import REASONING_SNAPSHOT_KEY
from .semantic_document import (
    canonicalize_document_html,
    is_current_semantic_document,
    namespace_semantic_fragment,
    semantic_result_for_stored_html,
    semantic_settings,
)

try:
    import fitz  # PyMuPDF
except ImportError:
    fitz = None

# Preserve route-level monkeypatch handles used by existing EPUB tests.
shutil = epub_validation.shutil
subprocess = epub_validation.subprocess

blueprint = Blueprint(
    "doc_processor",
    __name__,
    template_folder="templates",
    static_folder="static",
    url_prefix="/addons/doc_processor",
)

PREVIEW_PREVIOUS_PAGE_CONTEXT_HTML = (
    "<main_content><p>Previous page context preview.</p></main_content>"
)
DEFAULT_MAX_UPLOAD_BYTES = 512 * 1024 * 1024
# A project request may contain many individually-valid source files.  Keep
# the aggregate bounded independently: four maximum-sized source PDFs are a
# useful upper bound for one deliberate batch without allowing the 100-file
# count limit to turn into a 50 GiB write.
DEFAULT_MAX_PROJECT_UPLOAD_BYTES = 4 * DEFAULT_MAX_UPLOAD_BYTES
MAX_PROJECT_UPLOAD_FILES = 100
MAX_PROJECT_CONCURRENCY = 10
MAX_EMBEDDED_IMAGE_BYTES = 25 * 1024 * 1024
MAX_EDITED_HTML_BYTES = 20 * 1024 * 1024
DOCUMENT_ACTIVE_STATUSES = frozenset({"processing", "preparing_crop_review", "pausing", "suspended"})
TERMINAL_QUEUE_STATUSES = frozenset({"completed", "failed", "cancelled", "unknown"})
ENQUEUE_CLAIM_TTL_SECONDS = 120
SUPPORTED_DOCUMENT_EXTENSIONS = {
    ".csv",
    ".doc",
    ".docx",
    ".epub",
    ".fb2",
    ".htm",
    ".html",
    ".msg",
    ".odt",
    ".pdf",
    ".rtf",
    ".txt",
}
EXPORT_IMAGE_MIME_TYPES = {
    ".bmp": "image/bmp",
    ".gif": "image/gif",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".png": "image/png",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".webp": "image/webp",
}


def _get_addon():
    return current_app.addon_manager.get_loaded_addon("doc_processor")


def _get_task_queue():
    return blueprint.addon_context.get_task_queue("doc_processor")


def _ensure_workers(num_workers=1):
    context = getattr(blueprint, "addon_context", None)
    if context is None:
        raise RuntimeError("Document Processor add-on context is unavailable.")
    return context.ensure_worker_pool("worker.py", num_workers=num_workers)


def _enqueue_job(task_name, payload, num_workers=1):
    queue = _get_task_queue()
    _ensure_workers(num_workers)
    owned_payload = dict(payload or {})
    if has_request_context() and getattr(current_user, "id", None) not in (None, ""):
        owned_payload["user_id"] = int(current_user.id)
    return queue.enqueue(task_name, owned_payload)


def _enqueue_export_job(task_name: str, payload: dict) -> str | None:
    try:
        return _enqueue_job(task_name, payload, num_workers=1)
    except Exception:
        current_app.logger.exception(
            "Failed to enqueue Document Processor export task '%s'.",
            task_name,
        )
        return None


def _get_user_document(document_id: str):
    return ProcessedDocument.query.filter_by(
        id=document_id, user_id=current_user.id
    ).first_or_404()


def _job_status_url(job_id: str) -> str:
    return url_for("doc_processor.get_job_status", job_id=job_id)


def _job_download_url(job_id: str) -> str:
    return url_for("doc_processor.download_job_result", job_id=job_id)


def _diagram_preview(html: str) -> tuple[str, str | None]:
    from carlina.core.diagram_rendering import project_diagrams_html

    try:
        return project_diagrams_html(html), None
    except (OSError, RuntimeError, ValueError):
        return html, "Diagram rendering is unavailable. The stored diagram code and labels are shown below."


def _build_document_payload(
    document: ProcessedDocument, include_output: bool = False
) -> dict:
    payload = document.to_dict()
    if not include_output:
        return payload

    output_html, semantic = _canonical_output_for_read(document)
    payload["output_html"] = output_html
    # Only the read-only display projects diagrams; editing retains canonical
    # templates and labels rather than saving generated SVG back to the source.
    payload["preview_html"], payload["preview_warning"] = _diagram_preview(output_html)
    payload["output_markdown"] = (
        document.output_markdown
        if document.output_markdown
        else (html_to_markdown_on_the_fly(output_html) if output_html else "")
    )
    payload["semantic_contract_version"] = semantic.contract_version
    payload["semantic_content_hash"] = semantic.content_hash
    payload["semantic_diagnostics"] = [dict(item) for item in semantic.diagnostics]
    return payload


def _document_revision(document: ProcessedDocument) -> str | None:
    """Return the API revision token for metadata optimistic concurrency."""

    return document.updated_at.isoformat() if document.updated_at is not None else None


def _requested_document_revision(data: dict) -> str | None:
    expected_revision = (
        data.get("expected_updated_at") if isinstance(data, dict) else None
    )
    if expected_revision is None:
        expected_revision = request.headers.get("If-Match")
    if not isinstance(expected_revision, str):
        return None
    return expected_revision.strip().strip('"') or None


def _metadata_mutation_conflict(
    document: ProcessedDocument,
    data: dict,
    *,
    active_only: bool = False,
    stale_only: bool = False,
    include_document: bool = True,
):
    """Return a 409 response when a metadata mutation no longer owns the row."""

    if not stale_only and document.status in DOCUMENT_ACTIVE_STATUSES:
        message = "Metadata cannot be changed while document processing is active."
    else:
        expected_revision = _requested_document_revision(data)
        current_revision = _document_revision(document)
        if (
            active_only
            or stale_only
            or (expected_revision and expected_revision != current_revision)
        ):
            message = "Document metadata changed while this edit was in progress. Reload it and try again."
        else:
            return None

    response: dict[str, object] = {"error": message}
    if include_document:
        response["document"] = _build_document_payload(
            document, include_output=True
        )
    return jsonify(response), 409


def _claim_metadata_mutation(
    document: ProcessedDocument,
    data: dict,
    *,
    expected_revision: str | None = None,
    include_document: bool = True,
):
    """Atomically claim the current document revision for a metadata write.

    A comparison performed only in Python leaves a last-writer-wins gap
    between the check and commit. Advancing ``updated_at`` with a conditional
    update makes concurrent tabs, provider callbacks, and workers contend on
    one database compare-and-set. The later metadata changes remain in this
    transaction, so a failure still rolls the claim back.
    """

    conflict = _metadata_mutation_conflict(
        document, data, include_document=include_document
    )
    if conflict is not None:
        return document, conflict

    current_revision = _document_revision(document)
    requested_revision = _requested_document_revision(data)
    claim_revision = expected_revision or requested_revision or current_revision
    if claim_revision != current_revision:
        return document, _metadata_mutation_conflict(
            document,
            data,
            stale_only=True,
            include_document=include_document,
        )

    current_timestamp = document.updated_at
    next_timestamp = utc_now_db()
    if current_timestamp is not None and next_timestamp <= current_timestamp:
        next_timestamp = current_timestamp + timedelta(microseconds=1)
    changed = ProcessedDocument.query.filter(
        ProcessedDocument.id == document.id,
        ProcessedDocument.user_id == document.user_id,
        ProcessedDocument.updated_at == current_timestamp,
        ~ProcessedDocument.status.in_(DOCUMENT_ACTIVE_STATUSES),
    ).update(
        {ProcessedDocument.updated_at: next_timestamp},
        synchronize_session=False,
    )
    if changed != 1:
        db.session.expire_all()
        current_document = ProcessedDocument.query.filter_by(
            id=document.id,
            user_id=document.user_id,
        ).first_or_404()
        return current_document, _metadata_mutation_conflict(
            current_document,
            data,
            stale_only=True,
            include_document=include_document,
        )

    db.session.flush()
    db.session.refresh(document)
    return document, None


def _canonical_output_for_read(document: ProcessedDocument):
    raw_html = document.output_html or (
        markdown_to_html_on_the_fly(document.output_markdown)
        if document.output_markdown
        else ""
    )
    if is_current_semantic_document(document.settings):
        # Current rows keep their stored bytes exactly; this is the identity
        # boundary shared with snapshots and Translator.
        result = semantic_result_for_stored_html(raw_html, document.settings)
        return raw_html, result
    result = canonicalize_document_html(
        raw_html,
        user_id=document.user_id,
        source_file_path=document.file_path,
        relaxed=True,
    )
    return result.html, result


def _workspace_asset_version() -> int:
    asset_path = (
        Path(current_app.root_path)
        / "addons"
        / "doc_processor"
        / "static"
        / "dist"
        / "workspace.js"
    )
    try:
        return int(asset_path.stat().st_mtime)
    except OSError:
        return int(time.time())


def _public_job_status(job_id: str, status: dict) -> dict:
    public_status = dict(status or {})
    for internal_key in ("traceback", "user_id", "cancel_requested_by"):
        public_status.pop(internal_key, None)
    result = public_status.get("result")
    if isinstance(result, dict) and result.get("kind") == "file_download":
        public_result = dict(result)
        public_result.pop("output_path", None)
        public_result.pop("user_id", None)
        public_result["download_url"] = _job_download_url(job_id)
        public_status["result"] = public_result
    return public_status


def _owned_job_status(queue, job_id: str) -> dict | None:
    status = queue.get_status(job_id)
    if not isinstance(status, dict):
        return None
    owner_id = status.get("user_id")
    result = status.get("result")
    if owner_id in (None, "") and isinstance(result, dict):
        owner_id = result.get("user_id")
    try:
        if int(owner_id) != int(current_user.id):
            return None
    except TypeError, ValueError:
        return None
    return status


def _parse_value(value):
    if isinstance(value, str):
        lowered = value.lower()
        if lowered == "true":
            return True
        if lowered == "false":
            return False
    return value


def _requested_epub_image_mode() -> str | None:
    value = str(request.args.get("epub_image_mode", "jpeg") or "auto").strip().lower()
    aliases = {
        "1": "webp",
        "true": "webp",
        "0": "auto",
        "false": "auto",
        "preserve": "auto",
    }
    normalized = aliases.get(value, value)
    from carlina.core.epub_export import normalize_epub_image_mode

    try:
        return normalize_epub_image_mode(normalized)
    except ValueError:
        return None


def _coerce_request_data():
    if request.is_json:
        payload = request.get_json(silent=True) or {}
        return {key: _parse_value(value) for key, value in payload.items()}
    return {key: _parse_value(value) for key, value in request.form.items()}


def _get_current_user_addon_settings() -> dict:
    user = db.session.get(User, current_user.id)
    if not user or not getattr(user, "addon_settings", None):
        return {}
    return dict(user.addon_settings or {})


def _get_doc_processor_prompt_overrides(
    addon_settings: dict | None = None,
) -> dict[str, str]:
    settings = dict(addon_settings or {})
    doc_processor_settings = dict(settings.get("doc_processor", {}) or {})
    overrides: dict[str, str] = {}
    for prompt_id in PROMPT_FIELDS_BY_ID:
        value = normalize_prompt_text(doc_processor_settings.get(prompt_id))
        if value:
            overrides[prompt_id] = value
    return overrides


def _resolve_local_path(file_path: str) -> Path | None:
    try:
        return resolve_upload_reference(file_path, uploads_dir=uploads_root())
    except TypeError, ValueError:
        current_app.logger.warning("Rejected invalid local file path: %s", file_path)
        return None


def _max_upload_bytes() -> int:
    configured = current_app.config.get(
        "DOC_PROCESSOR_MAX_UPLOAD_BYTES", DEFAULT_MAX_UPLOAD_BYTES
    )
    try:
        return max(1, int(configured))
    except TypeError, ValueError:
        return DEFAULT_MAX_UPLOAD_BYTES


def _max_project_upload_bytes() -> int:
    configured = current_app.config.get(
        "DOC_PROCESSOR_MAX_PROJECT_UPLOAD_BYTES",
        DEFAULT_MAX_PROJECT_UPLOAD_BYTES,
    )
    try:
        return max(1, int(configured))
    except TypeError, ValueError:
        return DEFAULT_MAX_PROJECT_UPLOAD_BYTES


def _uploaded_file_size(file) -> int | None:
    """Return an uploaded file's size without consuming its stream.

    Project uploads must be rejected before the first owner-scoped directory
    is made.  Werkzeug's multipart streams are seekable; fail closed for an
    unusual non-seekable stream rather than partially writing an over-limit
    batch and trying to clean it up afterwards.
    """

    stream = getattr(file, "stream", None)
    if stream is None:
        return None
    try:
        position = stream.tell()
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        stream.seek(position)
        return max(0, int(size))
    except AttributeError, OSError, ValueError:
        return None


def _validate_upload(file) -> tuple[str | None, str | None]:
    original_name = str(getattr(file, "filename", "") or "").strip()
    if not original_name:
        return None, "Empty filename."
    safe_name = secure_filename(original_name)
    if not safe_name:
        return None, "Filename does not contain any safe characters."
    if Path(safe_name).suffix.lower() not in SUPPORTED_DOCUMENT_EXTENSIONS:
        allowed = ", ".join(sorted(SUPPORTED_DOCUMENT_EXTENSIONS))
        return None, f"Unsupported document type. Allowed extensions: {allowed}."
    return safe_name, None


def _request_exceeds_upload_limit(file_count: int = 1) -> bool:
    content_length = request.content_length
    if content_length in (None, 0):
        return False
    multipart_allowance = max(1, file_count) * 1024 * 1024
    return (
        int(content_length)
        > (_max_upload_bytes() * max(1, file_count)) + multipart_allowance
    )


def _normalize_project_concurrency(value, *, default: int = 2) -> int:
    try:
        normalized = int(value)
    except TypeError, ValueError:
        normalized = default
    return max(1, min(MAX_PROJECT_CONCURRENCY, normalized))


def _missing_project_run_sources(run: ProjectRun) -> list[dict[str, str]]:
    """Return runnable run entries whose source has disappeared.

    This is deliberately read-only: resume must reject the whole operation
    before changing run, entry, or document state.  Terminal and manual
    review entries are not runnable until a later explicit action.
    """
    runnable_statuses = {
        "queued",
        "paused",
        "cancelled",
        "scheduling",
        "preparing_crop_review",
        "processing",
    }
    missing = []
    for entry in run.documents:
        if entry.status not in runnable_statuses:
            continue
        if entry.status == "cancelled" and entry.completed_at is not None:
            continue
        document = entry.document
        path = _resolve_local_path(document.file_path) if document is not None else None
        if path is None or not path.is_file():
            missing.append(
                {
                    "id": document.id if document is not None else entry.document_id,
                    "filename": document.filename
                    if document is not None
                    else entry.document_id,
                }
            )
    return missing


def _mark_document_enqueue_failed(
    document_id: str,
    *,
    project_run_document_id: str | None = None,
) -> ProcessedDocument | None:
    db.session.rollback()
    document = db.session.get(ProcessedDocument, document_id)
    if document is not None:
        document.status = "error"
        document.error_message = "Processing could not be queued. Please try again."
        document.settings = dict(document.settings or {})
        document.settings.pop("active_job_id", None)
        document.settings.pop("enqueue_claim", None)
        document.settings.pop("progress", None)
        document.settings["progress_msg"] = "Queue unavailable"
        flag_modified(document, "settings")

    run_id = None
    if project_run_document_id:
        entry = db.session.get(ProjectRunDocument, project_run_document_id)
        if entry is not None:
            entry.status = "error"
            entry.error_message = "Processing could not be queued. Please try again."
            entry.completed_at = utc_now_db()
            run_id = entry.run_id
    db.session.commit()

    if run_id:
        from .project_service import refresh_project_run

        refresh_project_run(run_id)
    return document


def _document_active_job(document: ProcessedDocument) -> tuple[str | None, str]:
    """Return ``(job_id, state)`` for a document's current queue marker.

    ``state`` is ``active`` only when the queue has a known non-terminal
    status. A missing, terminal, or unknown queue record is stale and may be
    retried. Queue read failures are treated as ``unavailable`` so a refresh
    cannot silently enqueue duplicate work while the queue is unhealthy.
    """

    settings = document.settings if isinstance(document.settings, dict) else {}
    job_id = str(settings.get("active_job_id") or "").strip()
    if not job_id:
        claim = settings.get("enqueue_claim")
        if claim:
            if isinstance(claim, dict):
                try:
                    claim_age = time.time() - float(claim.get("created_at"))
                except TypeError, ValueError:
                    claim_age = 0
                if claim_age > ENQUEUE_CLAIM_TTL_SECONDS:
                    return str(claim.get("token") or ""), "stale"
                return str(claim.get("token") or ""), "claim"
            # Legacy/manual claims without a timestamp are kept fail-closed;
            # clearing them here could permit a duplicate enqueue after a
            # process crash.
            return str(claim), "claim"
        return None, "missing"

    try:
        status = _get_task_queue().get_status(job_id)
    except Exception:
        current_app.logger.warning(
            "Unable to inspect active Document Processor job %s for document %s.",
            job_id,
            document.id,
            exc_info=True,
        )
        return job_id, "unavailable"

    queue_state = str((status or {}).get("status") or "unknown").lower()
    if queue_state not in TERMINAL_QUEUE_STATUSES:
        return job_id, "active"
    return job_id, "stale"


def _clear_stale_document_job_marker(document: ProcessedDocument) -> None:
    """Remove only an obsolete queue marker before a deliberate retry."""

    if not isinstance(document.settings, dict):
        return
    if (
        "active_job_id" not in document.settings
        and "enqueue_claim" not in document.settings
    ):
        return
    document.settings = dict(document.settings)
    document.settings.pop("active_job_id", None)
    document.settings.pop("enqueue_claim", None)
    flag_modified(document, "settings")


def _persist_active_job_marker(
    document_id: str,
    job_id: str,
    *,
    queue=None,
) -> bool:
    """Persist a successful enqueue without resurrecting a finished job.

    Workers normally run in a separate process, but tests and embedded
    deployments can complete a job synchronously. Reloading after enqueue and
    checking the queue status before and after the write keeps terminal worker
    cleanup from being overwritten in the common case.
    """

    queue = queue or _get_task_queue()

    def _is_terminal() -> bool:
        try:
            status = queue.get_status(job_id)
        except Exception:
            current_app.logger.warning(
                "Unable to verify queued job %s while persisting its document marker.",
                job_id,
                exc_info=True,
            )
            return False
        return (
            str((status or {}).get("status") or "unknown").lower()
            in TERMINAL_QUEUE_STATUSES
        )

    if _is_terminal():
        refreshed = db.session.get(ProcessedDocument, document_id)
        if refreshed is not None:
            refreshed_settings = dict(refreshed.settings or {})
            if "enqueue_claim" in refreshed_settings:
                refreshed_settings.pop("enqueue_claim", None)
                refreshed.settings = refreshed_settings
                flag_modified(refreshed, "settings")
                db.session.commit()
        return False

    document = db.session.get(ProcessedDocument, document_id)
    if document is None:
        return False
    settings = dict(document.settings or {})
    settings["active_job_id"] = str(job_id)
    settings.pop("enqueue_claim", None)
    document.settings = settings
    flag_modified(document, "settings")
    db.session.commit()

    # A synchronous worker may have crossed the terminal boundary during the
    # commit. Clear only our exact marker; never overwrite newer worker state.
    if _is_terminal():
        refreshed = db.session.get(ProcessedDocument, document_id)
        if refreshed is not None:
            refreshed_settings = dict(refreshed.settings or {})
            if str(refreshed_settings.get("active_job_id") or "") == str(job_id):
                refreshed_settings.pop("active_job_id", None)
                refreshed_settings.pop("enqueue_claim", None)
                refreshed.settings = refreshed_settings
                flag_modified(refreshed, "settings")
                db.session.commit()
        return False
    return True


def _enqueue_document_job(
    document: ProcessedDocument,
    task_name: str,
    payload: dict,
    *,
    num_workers: int = 1,
    project_run_document_id: str | None = None,
) -> str | None:
    try:
        return _enqueue_job(task_name, payload, num_workers=num_workers)
    except Exception:
        current_app.logger.exception(
            "Failed to enqueue Document Processor task '%s' for document %s.",
            task_name,
            document.id,
        )
        _mark_document_enqueue_failed(
            document.id,
            project_run_document_id=project_run_document_id,
        )
        return None


def _safe_document_asset_path(src: str, source_path: Path | None) -> Path | None:
    raw_src = str(src or "").strip()
    if not raw_src or source_path is None:
        return None

    parsed = urlparse(raw_src)
    if parsed.scheme or parsed.netloc or raw_src.startswith("//"):
        return None

    decoded_path = unquote(parsed.path or "")
    source_root = (
        source_path if source_path.is_dir() else source_path.parent
    ).resolve()
    if decoded_path.startswith("/uploads/"):
        candidate = _resolve_local_path(decoded_path)
    elif decoded_path.startswith("/"):
        return None
    else:
        candidate = source_root / decoded_path
    if candidate is None:
        return None

    try:
        resolved = candidate.resolve()
        resolved.relative_to(source_root)
        if (
            not resolved.is_file()
            or resolved.suffix.lower() not in EXPORT_IMAGE_MIME_TYPES
            or resolved.stat().st_size > MAX_EMBEDDED_IMAGE_BYTES
        ):
            return None
    except OSError, RuntimeError, ValueError:
        return None
    return resolved


def _managed_document_storage_dir(document: ProcessedDocument) -> Path | None:
    base_uploads_dir = uploads_root()
    document_root = (
        base_uploads_dir / str(document.user_id) / "doc_processor_docs"
    ).resolve()
    try:
        local_path = resolve_upload_reference(
            document.file_path,
            uploads_dir=base_uploads_dir,
            require_within_uploads=True,
        )
        if local_path is None:
            return None
        target_dir = (
            local_path if local_path.is_dir() else local_path.parent
        ).resolve()
        target_dir.relative_to(document_root)
        if target_dir == document_root:
            return None
        return target_dir
    except OSError, RuntimeError, TypeError, ValueError:
        current_app.logger.warning(
            "Rejected unmanaged Document Processor storage path for document %s.",
            document.id,
        )
        return None


def _parse_page_range(range_str: str, total_pages: int) -> set[int]:
    pages = set()

    for part in str(range_str or "").split(","):
        part = part.strip()
        if not part:
            continue

        if "-" in part:
            try:
                start, end = map(int, part.split("-", 1))
            except ValueError:
                continue

            start = max(1, start)
            end = min(total_pages, end)
            if start <= end:
                pages.update(range(start - 1, end))
            continue

        try:
            page_index = int(part) - 1
        except ValueError:
            continue

        if 0 <= page_index < total_pages:
            pages.add(page_index)

    return pages if pages else set(range(total_pages))


def _parse_page_index_list(value) -> list[int]:
    if value is None:
        return []

    if isinstance(value, (list, tuple, set)):
        raw_values = value
    else:
        raw_values = str(value).split(",")

    page_indices = set()
    for raw_value in raw_values:
        try:
            page_index = int(raw_value)
        except TypeError, ValueError:
            continue

        if page_index >= 0:
            page_indices.add(page_index)

    return sorted(page_indices)


def _sanitize_crop_box(crop_box) -> list[float] | None:
    if not isinstance(crop_box, (list, tuple)) or len(crop_box) != 4:
        return None

    try:
        x0, y0, x1, y1 = [float(value) for value in crop_box]
    except TypeError, ValueError:
        return None

    x_min, x_max = sorted((max(0.0, min(1.0, x0)), max(0.0, min(1.0, x1))))
    y_min, y_max = sorted((max(0.0, min(1.0, y0)), max(0.0, min(1.0, y1))))

    if (x_max - x_min) < 0.001 or (y_max - y_min) < 0.001:
        return None

    return [x_min, y_min, x_max, y_max]


def _normalize_crop_mode(value: str | None) -> str:
    return "single" if str(value or "").lower() == "single" else "left_right"


def _normalize_first_page_side(value: str | None) -> str:
    return "left" if str(value or "").lower() == "left" else "right"


def _get_document_pdf_options(document: ProcessedDocument) -> dict:
    return dict((document.settings or {}).get("pdf_options", {}) or {})


def _get_document_crop_settings(document: ProcessedDocument) -> dict:
    settings = document.settings or {}
    pdf_options = _get_document_pdf_options(document)
    stored_crop_boxes = (
        settings.get("crop_boxes")
        if isinstance(settings.get("crop_boxes"), dict)
        else {}
    )

    return {
        "crop_box": _sanitize_crop_box(settings.get("crop_box")),
        "crop_boxes": {
            "mode": _normalize_crop_mode(stored_crop_boxes.get("mode")),
            "left": _sanitize_crop_box(stored_crop_boxes.get("left")),
            "right": _sanitize_crop_box(stored_crop_boxes.get("right")),
            "first_page_side": _normalize_first_page_side(
                stored_crop_boxes.get("first_page_side")
            ),
        },
        "excluded_pages": _parse_page_index_list(pdf_options.get("excluded_pages")),
        "page_range": str(pdf_options.get("page_range", "") or "").strip(),
    }


def _build_page_selection(
    total_pages: int, page_range: str, excluded_pages, first_page_side: str
) -> tuple[set[int], list[int], dict[int, str], set[int]]:
    allowed_pages = _parse_page_range(page_range, total_pages)
    excluded_set = {
        page
        for page in _parse_page_index_list(excluded_pages)
        if 0 <= page < total_pages
    }
    included_pages = [
        page_index
        for page_index in range(total_pages)
        if page_index in allowed_pages and page_index not in excluded_set
    ]

    page_sides = {}
    current_side = _normalize_first_page_side(first_page_side)
    for page_index in range(total_pages):
        if page_index not in allowed_pages:
            continue
        page_sides[page_index] = current_side
        current_side = "left" if current_side == "right" else "right"

    return allowed_pages, included_pages, page_sides, excluded_set


def _pdf_page_count(pdf_path: Path) -> int:
    """Return a PDF page count without requiring the licensed crop renderer."""
    if fitz:
        pdf_doc = fitz.open(pdf_path)
        try:
            return len(pdf_doc)
        finally:
            pdf_doc.close()

    from pypdf import PdfReader

    return len(PdfReader(pdf_path).pages)


def _metadata_flagged_page_indexes(metadata: dict, total_pages: int) -> set[int]:
    flagged_pages = set()
    source_pages = set((metadata or {}).get("source_pages") or [])
    for entry in (metadata or {}).get("pages_to_remove", []) or []:
        if not isinstance(entry, dict) or entry.get("safe_to_remove") is not True:
            continue
        try:
            confidence = float(entry.get("confidence", 0) or 0)
        except TypeError, ValueError:
            confidence = 0
        if confidence < 0.98:
            continue
        raw_page = entry.get("page") if isinstance(entry, dict) else entry
        try:
            page_number = int(raw_page)
        except TypeError, ValueError:
            continue
        if page_number <= 0:
            continue
        if source_pages and page_number not in source_pages:
            continue
        page_index = page_number - 1
        if 0 <= page_index < total_pages:
            flagged_pages.add(page_index)
    return flagged_pages


def _requires_pdf_crop_review(is_pdf: bool, engine_id: str, settings: dict) -> bool:
    pdf_options = dict((settings or {}).get("pdf_options", {}) or {})
    return bool(
        is_pdf
        and engine_id in ["auto", "llm_vision"]
        and not pdf_options.get("skip_cropping", False)
    )


def _requires_crop_review_prepass(settings: dict) -> bool:
    pdf_options = dict((settings or {}).get("pdf_options", {}) or {})
    return bool(pdf_options.get("metadata_prepass", True))


def _ensure_crop_review_metadata_suggestions(
    document: ProcessedDocument,
    doc,
    total_pages: int,
    progress_callback=None,
) -> set[int]:
    settings = dict(document.settings or {})
    pdf_options = dict(settings.get("pdf_options", {}) or {})
    if not pdf_options.get("metadata_prepass", True) or not (
        pdf_options.get("metadata_delete_flagged_pages", False)
        or pdf_options.get("metadata_exclude_flagged_pages", False)
    ):
        return set()

    if not settings.get("document_metadata"):
        canonical_metadata = get_canonical_document_metadata(document)
        if canonical_metadata:
            settings["document_metadata"] = canonical_metadata
            document.settings = settings
            flag_modified(document, "settings")
            db.session.commit()

    if not settings.get("document_metadata"):
        try:
            from iarf_assistant.llm_service import LLMService

            from .parsers.pdf_parser import PdfParser

            parser = PdfParser(
                document.file_path,
                document.user_id,
                LLMService(user_id=document.user_id),
                settings=settings,
            )
            pdf_options = parser._normalize_pdf_options()
            first_page_side = (
                settings.get("crop_boxes", {}).get("first_page_side")
                if isinstance(settings.get("crop_boxes"), dict)
                else None
            )
            _allowed_pages, _included_pages, _page_sides, excluded_pages = (
                _build_page_selection(
                    total_pages,
                    str(pdf_options.get("page_range", "") or ""),
                    pdf_options.get("excluded_pages"),
                    first_page_side,
                )
            )
            from ...core.mcp_completion import passive_execution_context

            if passive_execution_context() is not None:
                # Queued crop review can run inside a passive MCP execution.
                # Its executor profile is already validated; do not resolve a
                # managed default or reuse a configured provider model.
                from .parsers.pdf_parser import _passive_document_model

                metadata_model = _passive_document_model("metadata")
            else:
                metadata_model = settings.get("metadata_model") or settings.get(
                    "parsing_model"
                )
                if not metadata_model:
                    from ...core.model_router import ModelRouter

                    metadata_model = ModelRouter.get_model_for_tier(
                        "medium", document.user_id
                    )
            metadata_scope_pages = [
                page_index
                for page_index in range(total_pages)
                if page_index not in excluded_pages
            ]

            parser._extract_document_metadata(
                doc,
                metadata_scope_pages,
                {},
                None,
                metadata_model,
                progress_callback=progress_callback,
            )
            settings = dict(parser.settings or {})
            document.settings = settings
            flag_modified(document, "settings")
            sync_document_metadata(
                document,
                settings.get("document_metadata"),
                ensure_record=True,
                mark_stale=False,
            )
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            logging.warning("Failed to prepare PDF metadata crop suggestions: %s", exc)
            return set()

    suggested_pages = _metadata_flagged_page_indexes(
        get_canonical_document_metadata(document), total_pages
    )
    if suggested_pages:
        settings = dict(document.settings or {})
        pdf_options = dict(settings.get("pdf_options", {}) or {})
        pdf_options["metadata_suggested_excluded_pages"] = sorted(suggested_pages)
        settings["pdf_options"] = pdf_options
        document.settings = settings
        flag_modified(document, "settings")
        db.session.commit()
    return suggested_pages


def _render_pdf_page_image(
    page: fitz.Page, target_longest_edge: int = 1400
) -> Image.Image:
    longest_edge = max(float(target_longest_edge or 0), 64.0)
    page_longest_edge = max(float(page.rect.width), float(page.rect.height), 1.0)
    scale = max(0.2, min(3.0, longest_edge / page_longest_edge))
    pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
    return Image.frombytes("RGB", [pix.width, pix.height], pix.samples)


def _render_pdf_page_luminance(
    page: fitz.Page, target_longest_edge: int = 850
) -> Image.Image:
    longest_edge = max(float(target_longest_edge or 0), 64.0)
    page_longest_edge = max(float(page.rect.width), float(page.rect.height), 1.0)
    scale = max(0.2, min(2.0, longest_edge / page_longest_edge))
    pix = page.get_pixmap(
        matrix=fitz.Matrix(scale, scale), colorspace=fitz.csGRAY, alpha=False
    )
    return Image.frombytes("L", [pix.width, pix.height], pix.samples)


def _scaled_preview_size(page, target_longest_edge: int) -> tuple[int, int]:
    page_longest_edge = max(float(page.rect.width), float(page.rect.height), 1.0)
    scale = max(
        0.2, min(2.0, max(float(target_longest_edge or 0), 64.0) / page_longest_edge)
    )
    width = max(1, int(round(float(page.rect.width) * scale)))
    height = max(1, int(round(float(page.rect.height) * scale)))
    return width, height


def _stack_preview_longest_edge(page_count: int) -> int:
    if page_count > 600:
        return 620
    if page_count > 250:
        return 720
    return 850


def _stack_preview_page_limit(page_count: int) -> int:
    if page_count > 400:
        return 40
    if page_count > 200:
        return 32
    if page_count > 80:
        return 24
    return min(max(page_count, 1), 18)


def _sample_stack_page_indices(page_indices: list[int]) -> list[int]:
    unique_pages = sorted(dict.fromkeys(page_indices))
    if not unique_pages:
        return []

    limit = _stack_preview_page_limit(len(unique_pages))
    if len(unique_pages) <= limit:
        return unique_pages
    if limit == 1:
        return [unique_pages[0]]

    sampled_positions = []
    for step in range(limit):
        ratio = step / (limit - 1)
        sampled_positions.append(round(ratio * (len(unique_pages) - 1)))
    return [unique_pages[position] for position in sorted(set(sampled_positions))]


def _representative_stack_base_size(
    doc, page_indices: list[int], target_longest_edge: int
) -> tuple[int, int] | None:
    if not page_indices:
        return None

    candidate_sizes = []
    for page_index in _sample_stack_page_indices(
        page_indices[: min(len(page_indices), 9)]
    ):
        try:
            candidate_sizes.append(
                _scaled_preview_size(doc[page_index], target_longest_edge)
            )
        except Exception:
            continue

    if not candidate_sizes:
        return None

    widths = sorted(size[0] for size in candidate_sizes)
    heights = sorted(size[1] for size in candidate_sizes)
    median_index = len(candidate_sizes) // 2
    return widths[median_index], heights[median_index]


def _build_text_support_mask(
    grayscale: Image.Image, base_size: tuple[int, int] | None = None
) -> np.ndarray:
    if base_size and grayscale.size != base_size:
        grayscale = grayscale.resize(base_size, Image.Resampling.BILINEAR)

    prepared = ImageOps.autocontrast(grayscale, cutoff=1).filter(
        ImageFilter.MedianFilter(size=3)
    )
    pixels = np.asarray(prepared, dtype=np.uint8)
    height, width = pixels.shape
    tile_size = 8
    block_height = height // tile_size
    block_width = width // tile_size
    if block_height < 1 or block_width < 1:
        return np.zeros((height, width), dtype=np.float32)

    cropped = pixels[: block_height * tile_size, : block_width * tile_size]
    dark = cropped < 205
    very_dark = cropped < 90
    shaped_dark = dark.reshape(block_height, tile_size, block_width, tile_size)
    shaped_very_dark = very_dark.reshape(
        block_height, tile_size, block_width, tile_size
    )
    ink_density = shaped_dark.mean(axis=(1, 3))
    very_dark_density = shaped_very_dark.mean(axis=(1, 3))

    text_tiles = (
        (ink_density >= 0.015)
        & (ink_density <= 0.46)
        & ~((ink_density > 0.30) & (very_dark_density > 0.12))
    )
    tile_mask = np.repeat(np.repeat(text_tiles, tile_size, axis=0), tile_size, axis=1)
    text_pixels = dark & tile_mask

    mask = np.zeros((height, width), dtype=np.uint8)
    mask[: text_pixels.shape[0], : text_pixels.shape[1]] = (
        text_pixels.astype(np.uint8) * 255
    )
    mask_image = (
        Image.fromarray(mask)
        .filter(ImageFilter.MaxFilter(size=5))
        .filter(ImageFilter.GaussianBlur(radius=0.7))
    )
    return np.asarray(mask_image, dtype=np.float32) / 255.0


def _build_text_layer_support_mask(
    page, base_size: tuple[int, int] | None
) -> np.ndarray | None:
    if base_size is None:
        return None

    try:
        blocks = page.get_text("blocks")
    except Exception:
        return None

    page_width = max(float(page.rect.width), 1.0)
    page_height = max(float(page.rect.height), 1.0)
    mask = np.zeros((base_size[1], base_size[0]), dtype=np.float32)
    has_text = False

    for block in blocks or []:
        if not isinstance(block, (list, tuple)) or len(block) < 5:
            continue
        text = str(block[4] or "").strip()
        if not text:
            continue

        try:
            x0, y0, x1, y1 = [float(value) for value in block[:4]]
        except TypeError, ValueError:
            continue

        left = max(0, min(base_size[0], int(round((x0 / page_width) * base_size[0]))))
        top = max(0, min(base_size[1], int(round((y0 / page_height) * base_size[1]))))
        right = max(
            left + 1, min(base_size[0], int(round((x1 / page_width) * base_size[0])))
        )
        bottom = max(
            top + 1, min(base_size[1], int(round((y1 / page_height) * base_size[1])))
        )
        mask[top:bottom, left:right] = 1.0
        has_text = True

    if not has_text:
        return None

    mask_image = Image.fromarray((mask * 255.0).astype(np.uint8)).filter(
        ImageFilter.MaxFilter(size=3)
    )
    return np.asarray(mask_image, dtype=np.float32) / 255.0


def _density_stack_to_preview(
    accumulator: np.ndarray, page_count: int
) -> Image.Image | None:
    if page_count < 1 or accumulator.size == 0:
        return None

    scale = np.log1p(max(1, page_count) * 2.5)
    ink = np.log1p(accumulator * 2.5) / scale
    ink = np.clip(ink, 0.0, 1.0) ** 0.85
    preview = (255.0 - (ink * 235.0)).astype(np.uint8)
    return Image.fromarray(preview).convert("RGB")


def _crop_mask_cache_context(doc) -> tuple[str, int] | None:
    raw_name = getattr(doc, "name", "") or ""
    if not raw_name:
        return None
    try:
        path = Path(raw_name)
        stat = path.stat()
        return str(path.resolve()), int(stat.st_mtime_ns)
    except OSError:
        return None


def _crop_mask_cache_get(key):
    if not has_app_context():
        return None
    cache = current_app.config.setdefault("_docproc_crop_mask_cache", OrderedDict())
    entry = cache.get(key)
    if entry is None:
        return None
    cache.move_to_end(key)
    mask_uint8, _byte_size = entry
    return mask_uint8.astype(np.float32) / 255.0


def _crop_mask_cache_set(key, mask: np.ndarray) -> None:
    if not has_app_context():
        return
    cache = current_app.config.setdefault("_docproc_crop_mask_cache", OrderedDict())
    byte_budget = int(
        current_app.config.get("DOCPROC_CROP_MASK_CACHE_BYTES", 96 * 1024 * 1024)
    )
    current_bytes = int(current_app.config.get("_docproc_crop_mask_cache_bytes", 0))
    mask_uint8 = np.clip(mask * 255.0, 0, 255).astype(np.uint8)
    byte_size = int(mask_uint8.nbytes)

    existing = cache.pop(key, None)
    if existing is not None:
        current_bytes -= int(existing[1])

    cache[key] = (mask_uint8, byte_size)
    current_bytes += byte_size

    while cache and current_bytes > byte_budget:
        _old_key, (_old_mask, old_size) = cache.popitem(last=False)
        current_bytes -= int(old_size)

    current_app.config["_docproc_crop_mask_cache_bytes"] = max(0, current_bytes)


def _get_crop_preview_mask(
    doc, page_index: int, target_longest_edge: int, base_size: tuple[int, int] | None
):
    page = doc[page_index]
    if base_size is None:
        base_size = _scaled_preview_size(page, target_longest_edge)

    cache_context = _crop_mask_cache_context(doc)
    cache_key = None
    if cache_context and base_size is not None:
        cache_key = (
            *cache_context,
            page_index,
            int(target_longest_edge),
            tuple(base_size),
        )
        cached = _crop_mask_cache_get(cache_key)
        if cached is not None:
            return cached, base_size

    text_layer_mask = _build_text_layer_support_mask(page, base_size)
    if text_layer_mask is not None and float(text_layer_mask.sum()) > 0.0:
        if cache_key:
            _crop_mask_cache_set(cache_key, text_layer_mask)
        return text_layer_mask, base_size

    grayscale = _render_pdf_page_luminance(
        page, target_longest_edge=target_longest_edge
    )
    if cache_context:
        cache_key = (
            *cache_context,
            page_index,
            int(target_longest_edge),
            tuple(base_size),
        )
        cached = _crop_mask_cache_get(cache_key)
        if cached is not None:
            return cached, base_size

    mask = _build_text_support_mask(grayscale, base_size=base_size)
    if cache_key:
        _crop_mask_cache_set(cache_key, mask)
    return mask, base_size


def _suggest_crop_box_from_mask(mask: np.ndarray) -> list[float] | None:
    if mask.size == 0:
        return None

    threshold = max(0.08, float(mask.max()) * 0.18)
    active_rows = np.where(mask.max(axis=1) >= threshold)[0]
    active_cols = np.where(mask.max(axis=0) >= threshold)[0]
    if not len(active_rows) or not len(active_cols):
        return None

    height, width = mask.shape
    pad_x = max(1, int(round(width * 0.015)))
    pad_y = max(1, int(round(height * 0.015)))
    left = max(0, int(active_cols[0]) - pad_x)
    top = max(0, int(active_rows[0]) - pad_y)
    right = min(width, int(active_cols[-1]) + 1 + pad_x)
    bottom = min(height, int(active_rows[-1]) + 1 + pad_y)
    return [
        round(left / width, 4),
        round(top / height, 4),
        round(right / width, 4),
        round(bottom / height, 4),
    ]


def _empty_stack_preview(
    reason: str, used_page_total: int, excluded_page_count: int = 0
) -> dict:
    return {
        "used_pages": [],
        "used_page_count": 0,
        "used_page_total": used_page_total,
        "excluded_page_count": excluded_page_count,
        "is_density_stack": True,
        "suggested_crop_box": None,
        "empty_reason": reason,
    }


def _build_density_stack_preview(
    doc, page_indices: list[int], excluded_page_indices=None
) -> dict | None:
    unique_pages = sorted(dict.fromkeys(page_indices))
    if not unique_pages:
        return _empty_stack_preview("no_pages_for_stack", 0, 0)

    excluded_set = {
        page_index
        for page_index in _parse_page_index_list(excluded_page_indices)
        if page_index in unique_pages
    }
    candidate_pages = [
        page_index for page_index in unique_pages if page_index not in excluded_set
    ]
    if not candidate_pages:
        return _empty_stack_preview("all_pages_excluded", 0, len(excluded_set))

    sampled_pages = _sample_stack_page_indices(candidate_pages)
    target_longest_edge = _stack_preview_longest_edge(len(candidate_pages))
    accumulator = None
    base_size = _representative_stack_base_size(doc, sampled_pages, target_longest_edge)
    processed_pages = []

    for page_index in sampled_pages:
        try:
            mask, base_size = _get_crop_preview_mask(
                doc, page_index, target_longest_edge, base_size
            )
            if accumulator is None:
                accumulator = np.zeros((base_size[1], base_size[0]), dtype=np.float32)
            accumulator += mask
            processed_pages.append(page_index)
        except Exception as exc:
            logging.warning(
                "Failed to add PDF page %s to crop density stack: %s",
                page_index + 1,
                exc,
            )

    if accumulator is None or not processed_pages:
        return None

    composite = _density_stack_to_preview(accumulator, len(processed_pages))
    if not composite:
        return None

    suggested_crop_box = _suggest_crop_box_from_mask(
        accumulator / max(1, len(processed_pages))
    )
    preview_pages = [page_index + 1 for page_index in processed_pages[:12]]
    return {
        "image_data": _encode_image_data(composite),
        "width": composite.width,
        "height": composite.height,
        "used_pages": preview_pages,
        "used_page_count": len(processed_pages),
        "used_page_total": len(candidate_pages),
        "excluded_page_count": len(excluded_set),
        "is_density_stack": True,
        "suggested_crop_box": suggested_crop_box,
    }


def _encode_image_data(image: Image.Image) -> str:
    buffered = io.BytesIO()
    image.save(buffered, format="PNG")
    return (
        f"data:image/png;base64,{base64.b64encode(buffered.getvalue()).decode('utf-8')}"
    )


def _build_document_settings(
    data: dict,
    *,
    default_model: str | None = None,
    effective_engine: str | None = None,
    requested_engine: str | None = None,
    user=None,
) -> dict:
    """Route-compatible wrapper around the shared intake settings contract."""

    settings = _shared_build_document_settings(
        data,
        default_model=default_model,
        effective_engine=effective_engine,
        requested_engine=requested_engine,
        user=user,
    )
    if "crop_box" in settings:
        settings["crop_box"] = _sanitize_crop_box(settings.get("crop_box"))
    if isinstance(settings.get("crop_boxes"), dict):
        crop_boxes = settings["crop_boxes"]
        settings["crop_boxes"] = {
            "mode": _normalize_crop_mode(crop_boxes.get("mode")),
            "left": _sanitize_crop_box(crop_boxes.get("left")),
            "right": _sanitize_crop_box(crop_boxes.get("right")),
            "first_page_side": _normalize_first_page_side(
                crop_boxes.get("first_page_side")
            ),
        }
    return settings


_RETRY_PDF_OPTION_KEYS = frozenset(
    {
        "removehf",
        "evaluate_pdf",
        "evaluate",
        "append_previous",
        "ignore_footnotes",
        "simple_footnotes",
        "llm_boundary_merge",
        "correct_images",
        "extract_images",
        "parallel_pages",
        "page_range",
        "excluded_pages",
        "metadata_prepass",
        "metadata_pages",
        "metadata_end_pages",
        "metadata_exclude_flagged_pages",
        "metadata_delete_flagged_pages",
        "toc_handling",
        "prompt_cache",
        "skip_cropping",
        "store_debug_artifacts",
    }
)
_RETRY_PDF_OPTION_ALIASES = {
    "evaluate_pdf": "evaluate",
    "metadata_exclude_flagged_pages": "metadata_delete_flagged_pages",
}
_RETRY_ENGINE_OPTION_KEYS = frozenset(
    {
        "force_ocr",
        "paginate_output",
        "use_doc_unwarping",
        "use_doc_orientation_classify",
        "layout_shape_mode",
        "merge_tables",
        "relevel_titles",
        "concatenate_pages",
        "paddle_remove_hf",
        "paddle_preserve_footnotes",
        "paddle_chart_rec",
        "paddle_seal_rec",
        "paddle_batch_size",
        "use_ocr",
        "exclude_footers",
        "embed_images",
    }
)
_RETRY_SETTINGS_KEYS = (
    frozenset(
        {
            "engine",
            "engine_id",
            "parsing_model",
            "metadata_model",
            "processing_profile",
            "profile_id",
            "profile",
            "pdf_options",
            "crop_box",
            "crop_boxes",
        }
    )
    | _RETRY_PDF_OPTION_KEYS
    | _RETRY_ENGINE_OPTION_KEYS
)


def _has_resumable_pdf_state(document: ProcessedDocument) -> bool:
    """Whether the row is at the explicit post-extraction image-review phase.

    ``resume_pdf_parsing`` only performs image materialisation and final
    assembly. It must never receive an interrupted extraction state: doing so
    used to mark missing pages complete. Ordinary partial/failed/paused
    extraction is resumed by the regular parser path, which deliberately
    reuses ``pdf_state`` and fills its remaining pages. A complete review state
    remains directly resumable when final semantic validation fails after the
    review: sending that row through the regular parser would repeat metadata
    work and pause for the same image review again.
    """

    if Path(str(document.file_path or "")).suffix.lower() != ".pdf":
        return False
    state = (document.settings or {}).get("pdf_state")
    if not isinstance(state, dict):
        return False
    return getattr(document, "status", None) in {
        "reviewing_images",
        "error",
    } and is_complete_pdf_image_review_state(state)


def _has_pdf_recovery_state(document: ProcessedDocument) -> bool:
    """Whether a PDF retry must retain its durable parser recovery state."""

    if Path(str(document.file_path or "")).suffix.lower() != ".pdf":
        return False
    state = (document.settings or {}).get("pdf_state")
    html_parts = state.get("html_parts") if isinstance(state, dict) else None
    return (
        isinstance(html_parts, list)
        and any(isinstance(part, str) and part.strip() for part in html_parts)
        and not _has_resumable_pdf_state(document)
    )


def _retry_settings_with_explicit_overrides(
    document: ProcessedDocument,
    data: dict,
) -> tuple[dict, bool]:
    """Return durable settings with only explicitly supplied retry overrides.

    ``build_document_settings`` is intentionally an ingestion constructor and
    therefore fills defaults.  Calling it with an empty retry request used to
    rewrite a stored Book Quality/custom snapshot.  Seed it from the document
    instead, then merge its known configuration keys back into the durable
    dictionary so runtime markers and future opaque settings survive.
    """

    stored = deepcopy(dict(document.settings or {}))
    explicit = {
        key: value for key, value in data.items() if key in _RETRY_SETTINGS_KEYS
    }
    if not explicit:
        return stored, False

    requested_engine = (
        str(
            explicit.get(
                "engine",
                explicit.get(
                    "engine_id",
                    stored.get("requested_engine_id", stored.get("engine_id", "auto")),
                ),
            )
            or "auto"
        )
        .strip()
        .lower()
    )
    seed = {
        "engine": requested_engine,
        "parsing_model": stored.get("parsing_model"),
        "metadata_model": stored.get("metadata_model"),
        "pdf_options": deepcopy(dict(stored.get("pdf_options") or {})),
    }
    if stored.get("processing_profile") not in (None, ""):
        seed["processing_profile"] = stored["processing_profile"]
    for key in ("crop_box", "crop_boxes"):
        if key in stored:
            seed[key] = deepcopy(stored[key])
    if any(key in explicit for key in ("processing_profile", "profile_id", "profile")):
        # The shared intake contract gives ``processing_profile`` precedence
        # over legacy aliases.  Drop the stored value before applying an
        # explicit alias so a caller can deliberately switch profiles.
        seed.pop("processing_profile", None)
    seed.update(explicit)
    if isinstance(explicit.get("pdf_options"), dict):
        seed["pdf_options"] = {
            **dict(stored.get("pdf_options") or {}),
            **dict(explicit["pdf_options"]),
        }

    profile_override = any(
        key in explicit for key in ("processing_profile", "profile_id", "profile")
    )
    engine_override = "engine" in explicit or "engine_id" in explicit
    effective_engine = (
        _resolve_effective_engine(document.filename, requested_engine, seed)
        if engine_override or profile_override
        else str(stored.get("engine_id") or requested_engine)
    )
    models_changed = any(key in explicit for key in ("parsing_model", "metadata_model"))
    rebuilt = _build_document_settings(
        seed,
        default_model=stored.get("parsing_model") or stored.get("metadata_model"),
        effective_engine=effective_engine,
        requested_engine=requested_engine,
        # A retry that changes only crop/PDF/engine options must retain the
        # immutable reasoning decision from the original run.  Rebuild it
        # only when the caller deliberately changes a model or profile.
        user=current_user if models_changed or profile_override else None,
    )
    engine_options_changed = engine_override or any(
        key in explicit for key in _RETRY_ENGINE_OPTION_KEYS
    )
    if engine_options_changed:
        engine_options = _build_engine_options(effective_engine, seed)
        if engine_options:
            rebuilt["engine_options"] = engine_options
        else:
            rebuilt.pop("engine_options", None)

    merged = deepcopy(stored)
    merged.update(rebuilt)
    if not profile_override:
        # A sparse model/crop/options retry may change only the requested
        # fields.  In particular, do not expand a legacy/custom PDF options
        # dictionary with fresh ingestion defaults merely because it was
        # retried from a browser that did not submit the rest of the form.
        explicit_pdf_values = dict(explicit.get("pdf_options") or {})
        explicit_pdf_values.update(
            {
                key: value
                for key, value in explicit.items()
                if key in _RETRY_PDF_OPTION_KEYS
            }
        )
        if explicit_pdf_values:
            preserved_pdf_options = deepcopy(dict(stored.get("pdf_options") or {}))
            for source_key in explicit_pdf_values:
                target_key = _RETRY_PDF_OPTION_ALIASES.get(source_key, source_key)
                if target_key in rebuilt["pdf_options"]:
                    preserved_pdf_options[target_key] = rebuilt["pdf_options"][
                        target_key
                    ]
            if "parallel_pages" in explicit_pdf_values:
                preserved_pdf_options["append_previous"] = rebuilt["pdf_options"][
                    "append_previous"
                ]
            merged["pdf_options"] = preserved_pdf_options
    if models_changed and REASONING_SNAPSHOT_KEY not in rebuilt:
        merged.pop(REASONING_SNAPSHOT_KEY, None)
    if "requested_engine_id" not in rebuilt:
        merged.pop("requested_engine_id", None)
    return merged, True


def _normalize_pdf_processing_options(pdf_options: dict) -> dict:
    return _shared_normalize_pdf_processing_options(pdf_options)


def _legacy_sanitize_document_html(html_content: str) -> str:
    soup = BeautifulSoup(str(html_content or ""), "html.parser")
    allowed_tags = {
        "a",
        "abbr",
        "article",
        "aside",
        "b",
        "blockquote",
        "body",
        "br",
        "caption",
        "cite",
        "code",
        "col",
        "colgroup",
        "dd",
        "del",
        "div",
        "dl",
        "dt",
        "em",
        "figcaption",
        "figure",
        "footer",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "html",
        "i",
        "img",
        "ins",
        "li",
        "main",
        "mark",
        "nav",
        "ol",
        "p",
        "pre",
        "q",
        "s",
        "section",
        "small",
        "span",
        "strong",
        "sub",
        "sup",
        "table",
        "tbody",
        "td",
        "tfoot",
        "th",
        "thead",
        "tr",
        "u",
        "ul",
    }
    remove_with_contents = {
        "script",
        "style",
        "iframe",
        "object",
        "embed",
        "form",
        "input",
        "button",
    }
    global_attrs = {
        "aria-describedby",
        "aria-label",
        "aria-labelledby",
        "class",
        "data-footnote-label",
        "data-footnote-scope",
        "data-label",
        "dir",
        "epub:type",
        "id",
        "lang",
        "role",
        "title",
    }
    tag_attrs = {
        "a": {"href", "name", "rel", "target"},
        "img": {"alt", "height", "src", "width"},
        "ol": {"reversed", "start", "type"},
        "li": {"value"},
        "td": {"colspan", "headers", "rowspan"},
        "th": {"colspan", "headers", "rowspan", "scope"},
        "col": {"span"},
        "colgroup": {"span"},
        "table": {"summary"},
    }
    uri_attrs = {"href", "src"}

    for tag in list(soup.find_all(True)):
        tag_name = tag.name.lower()
        if tag_name in remove_with_contents:
            tag.decompose()
            continue
        if tag_name not in allowed_tags:
            tag.unwrap()
            continue

        allowed_attrs = global_attrs | tag_attrs.get(tag_name, set())
        for attr_name in list(tag.attrs.keys()):
            normalized_attr = attr_name.lower()
            if (
                normalized_attr.startswith("on")
                or normalized_attr == "style"
                or normalized_attr not in allowed_attrs
            ):
                del tag.attrs[attr_name]
                continue

            if normalized_attr in uri_attrs:
                attr_value = tag.attrs.get(attr_name)
                value = (
                    " ".join(attr_value)
                    if isinstance(attr_value, list)
                    else str(attr_value or "")
                )
                lowered_value = value.strip().lower()
                safe_data_image = (
                    tag_name == "img"
                    and normalized_attr == "src"
                    and lowered_value.startswith(
                        (
                            "data:image/bmp;base64,",
                            "data:image/gif;base64,",
                            "data:image/jpeg;base64,",
                            "data:image/png;base64,",
                            "data:image/tiff;base64,",
                            "data:image/webp;base64,",
                        )
                    )
                )
                if (
                    lowered_value.startswith(("javascript:", "vbscript:", "data:"))
                    and not safe_data_image
                ):
                    del tag.attrs[attr_name]
            elif normalized_attr == "class":
                classes = tag.attrs.get(attr_name)
                if isinstance(classes, str):
                    classes = classes.split()
                cleaned_classes = [
                    class_name
                    for class_name in (classes or [])
                    if class_name not in {"docproc-selected-block", "docproc-editing"}
                ]
                if cleaned_classes:
                    tag.attrs[attr_name] = cleaned_classes
                else:
                    del tag.attrs[attr_name]

    _normalize_document_html(soup)

    if soup.body:
        return soup.body.decode_contents()
    return str(soup)


def _sanitize_document_html(html_content: str) -> str:
    """Compatibility helper for export call sites without a document object.

    Persistent paths must call :func:`canonicalize_document_html` with owner
    context.  This helper deliberately has no authority to preserve local
    images, so it is safe for old fragment-only callers.
    """

    return _legacy_sanitize_document_html(html_content)


def _slugify_heading_id(text: str, seen_ids: set[str]) -> str:
    base = "-".join(str(text or "").lower().split())
    base = "".join(
        char if char.isalnum() or char in "-_" else "-" for char in base
    ).strip("-")
    base = base[:64] or "section"
    candidate = base
    suffix = 2
    while candidate in seen_ids:
        candidate = f"{base}-{suffix}"
        suffix += 1
    seen_ids.add(candidate)
    return candidate


def _normalize_document_html(soup: BeautifulSoup) -> None:
    # 1. Clean up empty/whitespace paragraphs
    for p in soup.find_all("p"):
        text = p.get_text(strip=True)
        if not text and not p.find_all(["img", "iframe", "object", "embed", "table"]):
            p.decompose()
            continue

        # 2. Convert double <br> inside paragraphs to separate paragraphs
        content = p.decode_contents()
        if "<br" in content:
            import re

            parts = re.split(r"(?:<br\s*/?>\s*){2,}", content, flags=re.IGNORECASE)
            if len(parts) > 1:
                parent = p.parent
                if parent:
                    try:
                        p_index = parent.contents.index(p)
                    except ValueError:
                        continue

                    inserted_count = 0
                    for part in parts:
                        if part.strip():
                            new_p = soup.new_tag("p")
                            new_p.append(BeautifulSoup(part, "html.parser"))
                            if p.has_attr("class"):
                                new_p["class"] = p["class"]
                            parent.insert(p_index + inserted_count, new_p)
                            inserted_count += 1
                    p.decompose()

    # 3. Ensure headings have unique IDs for anchor links and EPUB chapter generation
    seen_ids = set()
    for tag in soup.find_all(id=True):
        val = tag.get("id")
        if val:
            if isinstance(val, list):
                seen_ids.update(val)
            else:
                seen_ids.add(val)

    for heading in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"]):
        if not heading.get("id"):
            heading_text = heading.get_text(" ", strip=True)
            heading["id"] = _slugify_heading_id(heading_text, seen_ids)


_PORTABLE_EXPORT_STYLES = """
:root {
  color-scheme: light dark;
  font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  line-height: 1.55;
}
* {
  box-sizing: border-box;
}
body {
  width: min(100% - 2rem, 72rem);
  margin: 0 auto;
  padding: 2rem 0 4rem;
  color: CanvasText;
  background: Canvas;
}
h1, h2, h3, h4, h5, h6 {
  margin: 1.4em 0 0.45em;
  line-height: 1.2;
}
p, ul, ol, blockquote, pre, table {
  margin: 0.75rem 0;
}
img, svg, video {
  display: block;
  max-width: 100%;
  height: auto;
}
table {
  width: 100%;
  border-collapse: collapse;
}
th, td {
  padding: 0.45rem 0.6rem;
  border: 1px solid color-mix(in srgb, CanvasText 24%, transparent);
  text-align: left;
  vertical-align: top;
}
blockquote {
  margin-inline: 0;
  padding-inline: 0.9rem;
  border-inline-start: 3px solid color-mix(in srgb, CanvasText 34%, transparent);
}
pre {
  max-width: 100%;
  overflow-x: auto;
  padding: 0.8rem;
  border: 1px solid color-mix(in srgb, CanvasText 18%, transparent);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
code {
  font-family: ui-monospace, SFMono-Regular, Consolas, "Liberation Mono", monospace;
}
a {
  color: LinkText;
}
.project-document + .project-document,
.project-separator {
  margin-top: 2.5rem;
}
@media print {
  body {
    width: auto;
    padding: 0;
  }
}
""".strip()


def _build_standalone_html(
    content: str, title: str, language: str | None = None, styles: str | None = None
) -> str:
    soup = BeautifulSoup(content or "", "html.parser")
    normalize_export_note_roles(soup)
    content = str(soup)
    if content and "<html" in content.lower():
        return content

    lang_attr = f" lang='{html_lib.escape(language)}'" if language else ""
    style_tag = f"<style>\n{styles}\n</style>\n" if styles else ""
    return (
        "<!DOCTYPE html>\n"
        f"<html{lang_attr}>\n"
        "<head>\n"
        "<meta charset='utf-8'>\n"
        f"<title>{html_lib.escape(title or 'Processed Document')}</title>\n"
        f"{style_tag}"
        "</head>\n"
        "<body>\n"
        f"{content or ''}\n"
        "</body>\n"
        "</html>"
    )


def _resolve_export_image_src(src: str, source_path: Path | None) -> str:
    raw_src = str(src or "").strip()
    if not raw_src:
        return raw_src
    if raw_src.lower().startswith(
        (
            "data:image/bmp;base64,",
            "data:image/gif;base64,",
            "data:image/jpeg;base64,",
            "data:image/png;base64,",
            "data:image/tiff;base64,",
            "data:image/webp;base64,",
        )
    ):
        return raw_src

    safe_path = _safe_document_asset_path(raw_src, source_path)
    return safe_path.as_uri() if safe_path else ""


def _normalize_footnote_refs_for_epub(soup: BeautifulSoup) -> None:
    """Keep explicit footnote href mappings intact during Pandoc EPUB conversion.

    Pandoc interprets anchors with role="doc-noteref" as semantic footnotes and rewrites
    them into chapter-local `#fn1` links, which can break disambiguated numbering.
    Also strip semantic roles/types from footnote containers and divs so Pandoc does
    not strip them as orphan endnotes when the referrers are plain links.
    """
    for anchor in soup.find_all("a"):
        role = str(anchor.get("role") or "").strip().lower()
        if role != "doc-noteref":
            continue

        classes = set(anchor.get("class") or [])
        epub_type = str(anchor.get("epub:type") or "").strip().lower()
        is_footnote_ref = "footnote-ref" in classes or epub_type == "noteref"
        if is_footnote_ref:
            del anchor["role"]

    fn_container = soup.find(id="footnotes")
    if fn_container:
        fn_container["id"] = "document-footnotes"
        if fn_container.has_attr("epub:type"):
            del fn_container["epub:type"]
        if fn_container.has_attr("role"):
            del fn_container["role"]

    for fn_div in soup.find_all(class_="footnote"):
        if fn_div.has_attr("role"):
            del fn_div["role"]
        if fn_div.has_attr("epub:type"):
            del fn_div["epub:type"]


def _prepare_html_for_epub_export(
    content: str, title: str, document: ProcessedDocument, metadata: dict
) -> tuple[str, list[str]]:
    language = metadata.get("language") or metadata.get("lang") or "en"
    html_content = _build_standalone_html(content, title, language)
    soup = BeautifulSoup(html_content, "html.parser")
    source_path = _resolve_local_path(document.file_path)

    if str(document.file_path or "").lower().endswith(".pdf"):
        from .parsers.pdf_utils import remove_source_toc_blocks

        remove_source_toc_blocks(soup)

    seen_ids = {tag.get("id") for tag in soup.find_all(id=True) if tag.get("id")}
    for heading in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"]):
        if not heading.get("id"):
            heading["id"] = _slugify_heading_id(
                heading.get_text(" ", strip=True), seen_ids
            )

    for img in soup.find_all("img"):
        if img.get("src"):
            img["src"] = _resolve_export_image_src(img.get("src"), source_path)

    _normalize_footnote_refs_for_epub(soup)

    resource_paths = [tempfile.gettempdir()]
    if source_path:
        resource_paths.append(str(source_path.parent))

    return str(soup), resource_paths


def _validate_epub_file(output_path: str) -> tuple[bool, str | None]:
    """Preserve the route module's legacy validator interface."""
    passed, message = epub_validation.validate_epub_structure(output_path)
    if not passed:
        return passed, message
    return _run_official_epubcheck(output_path)


def _run_official_epubcheck(output_path: str) -> tuple[bool, str | None]:
    """Preserve the route module's legacy EPUBCheck interface."""
    config = current_app.config if has_app_context() else None
    return epub_validation.run_official_epubcheck(
        output_path, config=config
    ).as_legacy_tuple()


def _build_engine_options(engine_id: str, data: dict) -> dict:
    if engine_id == "marker":
        return {
            "force_ocr": data.get("force_ocr", False),
            "paginate_output": data.get("paginate_output", False),
        }

    if engine_id == "paddleocr":
        return {
            "use_doc_unwarping": data.get("use_doc_unwarping", False),
            "use_doc_orientation_classify": data.get(
                "use_doc_orientation_classify", False
            ),
            "layout_shape_mode": data.get("layout_shape_mode", "auto"),
            "merge_tables": data.get("merge_tables", True),
            "relevel_titles": data.get("relevel_titles", True),
            "concatenate_pages": data.get("concatenate_pages", False),
            "paddle_remove_hf": data.get("paddle_remove_hf", True),
            "paddle_preserve_footnotes": data.get("paddle_preserve_footnotes", False),
            "paddle_chart_rec": data.get("paddle_chart_rec", False),
            "paddle_seal_rec": data.get("paddle_seal_rec", False),
            "batch_size": data.get("paddle_batch_size", 4),
        }

    if engine_id == "pymupdf":
        return {
            "use_ocr": data.get("use_ocr", True),
            "force_ocr": data.get("force_ocr", False),
            "paginate_output": data.get("paginate_output", False),
            "exclude_footers": data.get("exclude_footers", True),
            "embed_images": data.get("embed_images", False),
        }

    return {}


def _resolve_effective_engine(
    filename: str,
    requested_engine: str,
    data: dict | None = None,
) -> str:
    """Resolve an engine before upload storage or database mutation."""

    requested = str(requested_engine or "auto").strip().lower() or "auto"
    values = dict(data or {})
    suffix = Path(str(filename)).suffix.lower().lstrip(".")
    profile = get_processing_profile(values)
    selection_request = requested
    if profile == PROFILE_BOOK_QUALITY and suffix == "pdf":
        # A quality book PDF is a semantic/vision workflow by definition. Do
        # this before catalog lookup so a stale UI engine choice cannot weaken
        # the profile's guarantees; the original request is persisted by the
        # settings builder as requested_engine_id.
        selection_request = "llm_vision"
    elif (
        profile == PROFILE_BOOK_QUALITY
        and suffix != "pdf"
        and requested == "llm_vision"
    ):
        # The PDF quality profile must not route TXT/DOCX/etc. through a PDF
        # vision parser. Treat a UI-carried vision default as auto-selection.
        selection_request = "auto"

    try:
        try:
            addon = _get_addon()
        except AttributeError, RuntimeError:
            # Unit callers may not have a host add-on manager.  Once an
            # application has one, lookup failures must remain fail-closed so
            # an unavailable external catalog cannot masquerade as built-in
            # readiness.
            addon = None
        if addon is not None:
            client = addon.initialize_client({"current_user_id": current_user.id})
        else:
            # The built-in catalog remains available for lightweight route
            # tests before the host has activated the add-on. Production
            # external engines always come through the activated catalog.
            from .api_client import DocProcessorClient

            client = DocProcessorClient(None, {})
        engines = list(client.get_engines() or [])
        selection_options = {}
        for key in ("force_ocr", "no_ocr"):
            if key in values:
                selection_options[key] = values[key]
        selected = (
            str(
                client.select_engine(
                    str(filename),
                    engine=selection_request,
                    **selection_options,
                )
            )
            .strip()
            .lower()
        )
        catalog_entry = next(
            (
                engine
                for engine in engines
                if str(engine.get("id") or "").strip().lower() == selected
            ),
            None,
        )
        if catalog_entry is None:
            raise ValueError(
                f"Extraction engine '{selection_request}' is not installed or active."
            )
        status = str(catalog_entry.get("status") or "").strip().lower()
        if status not in {"active", "ready"}:
            raise ValueError(f"Extraction engine '{selected}' is not active.")
        if not client._engine_supports_suffix(catalog_entry, suffix):
            raise ValueError(
                f"Extraction engine '{selected}' does not support '.{suffix}' files."
            )
        return selected
    except Exception as exc:
        raise ValueError(f"Unable to resolve an extraction engine: {exc}") from exc


def _build_page_image_payload(html_parts, png_paths):
    """Compatibility wrapper for resumable pre-completion image review."""
    return _build_pending_page_image_payload(
        html_parts, png_paths, uploads_dir=uploads_root()
    )


def _normalize_image_review_payload(
    payload, html_parts: list
) -> tuple[list[dict] | None, str | None]:
    """Compatibility wrapper for the existing resumable review contract."""
    return _normalize_pending_image_review_payload(payload, html_parts)


@blueprint.route("/")
@login_required
def index():
    return render_frontend_shell(
        route_id="addon:doc_processor",
        required_flag="frontend.addon.doc_processor",
    )


@blueprint.route("/api/engines", methods=["GET"])
@login_required
def list_engines():
    addon = _get_addon()
    if not addon:
        return jsonify({"error": "Document Processor addon is not active."}), 503

    client = addon.initialize_client({"current_user_id": current_user.id})
    return jsonify(client.get_engines())


@blueprint.route("/api/prompts", methods=["GET", "PUT"])
@login_required
def manage_prompts():
    if request.method == "GET":
        addon_settings = _get_current_user_addon_settings()
        return jsonify(
            {
                "sections": build_prompt_editor_sections(
                    _get_doc_processor_prompt_overrides(addon_settings)
                ),
            }
        )

    payload = request.get_json(silent=True) or {}
    raw_prompts = payload.get("prompts", payload)
    if not isinstance(raw_prompts, dict):
        return jsonify({"error": "Expected a JSON object of prompt values."}), 400

    user = db.session.get(User, current_user.id)
    if not user:
        return jsonify({"error": "User not found."}), 404

    current_settings = dict(user.addon_settings or {})
    doc_processor_settings = dict(current_settings.get("doc_processor", {}) or {})
    for prompt_id, value in raw_prompts.items():
        if prompt_id not in PROMPT_FIELDS_BY_ID:
            continue

        normalized_value = normalize_prompt_text(value)
        default_text = PROMPT_FIELDS_BY_ID[prompt_id].default_text
        if (
            not normalized_value.strip()
            or normalized_value.strip() == default_text.strip()
        ):
            doc_processor_settings.pop(prompt_id, None)
        else:
            doc_processor_settings[prompt_id] = normalized_value

    current_settings["doc_processor"] = doc_processor_settings
    user.addon_settings = current_settings
    flag_modified(user, "addon_settings")
    db.session.commit()

    addon = _get_addon()
    if addon and hasattr(addon, "on_settings_updated"):
        addon.on_settings_updated()

    return jsonify(
        {
            "success": True,
            "sections": build_prompt_editor_sections(
                _get_doc_processor_prompt_overrides(current_settings)
            ),
        }
    )


@blueprint.route("/api/prompts/preview", methods=["POST"])
@login_required
def preview_prompts():
    payload = request.get_json(silent=True) or {}
    raw_prompts = payload.get("prompts", {})
    processing_settings = payload.get("processing_settings", {})

    if raw_prompts is not None and not isinstance(raw_prompts, dict):
        return jsonify({"error": "The prompts payload must be an object."}), 400
    if processing_settings is not None and not isinstance(processing_settings, dict):
        return jsonify(
            {"error": "The processing settings payload must be an object."}
        ), 400

    prompt_overrides = _get_doc_processor_prompt_overrides(
        _get_current_user_addon_settings()
    )
    for prompt_id, value in (raw_prompts or {}).items():
        if prompt_id not in PROMPT_FIELDS_BY_ID:
            continue
        prompt_overrides[prompt_id] = normalize_prompt_text(value)

    try:
        preview_settings = _build_document_settings(dict(processing_settings or {}))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    pdf_options = _normalize_pdf_processing_options(
        preview_settings.get("pdf_options", {})
    )
    preview_settings["pdf_options"] = pdf_options

    page_number = max(1, int(payload.get("page_number", 1) or 1))
    assume_previous_context = bool(
        payload.get("assume_previous_context", pdf_options.get("append_previous", True))
    )
    previous_page_html = normalize_prompt_text(payload.get("previous_page_html"))
    placeholder_context_used = False
    previous_outputs = []
    if assume_previous_context:
        if not previous_page_html:
            previous_page_html = PREVIEW_PREVIOUS_PAGE_CONTEXT_HTML
            placeholder_context_used = True
        previous_outputs.append(
            {"page_index": page_number - 1, "content": previous_page_html}
        )

    document_metadata = (
        payload.get("document_metadata")
        if isinstance(payload.get("document_metadata"), dict)
        else {}
    )
    current_headings = (
        payload.get("current_headings")
        if isinstance(payload.get("current_headings"), dict)
        else {}
    )
    current_author = normalize_prompt_text(payload.get("current_author")) or None

    builder = DocProcessorPromptPreviewBuilder(
        prompt_overrides,
        preview_settings,
        document_metadata=document_metadata,
        current_headings=current_headings,
        current_author=current_author,
        previous_outputs=previous_outputs,
    )
    extraction_preview = builder.build_extraction_preview(
        page_number=page_number,
        include_previous_context=assume_previous_context,
    )
    extraction_preview["placeholder_context_used"] = placeholder_context_used

    return jsonify(
        {
            "extraction": extraction_preview,
            # JSON auxiliary stages intentionally use their own fixed system
            # policy. The editable HTML extraction system prompt does not
            # participate in metadata, heading, or boundary analysis.
            "metadata": builder.build_json_stage_preview("metadata"),
            "boundary": builder.build_json_stage_preview("boundary"),
            "evaluation": {
                "template": builder.get_evaluation_prompt(),
            },
            "txt_structuring": {
                "template": builder.get_txt_structuring_prompt(),
            },
        }
    )


@blueprint.route("/api/documents", methods=["GET"])
@login_required
def list_documents():
    documents = (
        ProcessedDocument.query.filter_by(user_id=current_user.id)
        .options(selectinload(ProcessedDocument.metadata_record))
        .order_by(ProcessedDocument.created_at.desc())
        .all()
    )
    return jsonify([doc.to_dict() for doc in documents])


@blueprint.route("/api/documents", methods=["POST"])
@login_required
def upload_document():
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded."}), 400

    file = request.files["file"]
    safe_filename, upload_error = _validate_upload(file)
    if upload_error:
        return jsonify({"error": upload_error}), 400
    if _request_exceeds_upload_limit():
        return jsonify({"error": "Document exceeds the configured upload limit."}), 413

    data = _coerce_request_data()
    requested_engine = str(data.get("engine", "auto") or "auto").strip().lower()

    from iarf_assistant.core.model_router import ModelRouter

    default_model = ModelRouter.get_model_for_tier("medium", current_user.id)
    try:
        engine_id = _resolve_effective_engine(safe_filename, requested_engine, data)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    try:
        settings = _build_document_settings(
            data,
            default_model=default_model,
            effective_engine=engine_id,
            requested_engine=requested_engine,
            user=current_user,
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    engine_options = _build_engine_options(engine_id, data)
    if engine_options:
        settings["engine_options"] = engine_options

    model_error = validate_pdf_model_readiness(
        safe_filename,
        settings,
        user_id=current_user.id,
    )
    if model_error:
        return jsonify({"error": model_error}), 400

    doc_uuid = uuid.uuid4().hex

    user_dir = uploads_root() / str(current_user.id) / "doc_processor_docs" / doc_uuid
    created_source_dir = False
    try:
        # UUID directories are created exclusively.  That makes the cleanup
        # target provably this request's owner-scoped source directory rather
        # than a broad or pre-existing upload path.
        user_dir.mkdir(parents=True, exist_ok=False)
        created_source_dir = True
        file_path = user_dir / safe_filename
        file.save(file_path)
        if file_path.stat().st_size > _max_upload_bytes():
            shutil.rmtree(user_dir, ignore_errors=True)
            return jsonify(
                {"error": "Document exceeds the configured upload limit."}
            ), 413

        web_path = (
            f"/uploads/{current_user.id}/doc_processor_docs/{doc_uuid}/{safe_filename}"
        )
        is_pdf = file_path.suffix.lower() == ".pdf"
        requires_crop_review = _requires_pdf_crop_review(is_pdf, engine_id, settings)
        if requires_crop_review and _requires_crop_review_prepass(settings):
            initial_status = "preparing_crop_review"
        elif requires_crop_review:
            initial_status = "needs_cropping"
        else:
            initial_status = "processing"

        if initial_status in {"processing", "preparing_crop_review"}:
            settings["enqueue_claim"] = {
                "token": uuid.uuid4().hex,
                "created_at": time.time(),
            }

        document = ProcessedDocument(
            user_id=current_user.id,
            filename=file.filename,
            file_path=web_path,
            status=initial_status,
            settings=settings,
        )
        db.session.add(document)
        db.session.flush()
        initialize_document_metadata(document, file_path)
        if document.metadata_record is not None:
            db.session.add(document.metadata_record)
        from .knowledge_adapter import ensure_processed_document_source

        ensure_processed_document_source(document)
        db.session.commit()
    except Exception:
        db.session.rollback()
        if created_source_dir:
            shutil.rmtree(user_dir, ignore_errors=True)
        current_app.logger.exception("Failed to initialize standalone document upload.")
        return jsonify({"error": "Unable to save the uploaded document."}), 500

    job_id = None
    if initial_status == "processing":
        job_id = _enqueue_document_job(
            document,
            "process_document",
            {"document_id": document.id, "engine_id": engine_id},
        )
    elif initial_status == "preparing_crop_review":
        job_id = _enqueue_document_job(
            document,
            "prepare_crop_review",
            {"document_id": document.id},
        )

    if initial_status in {"processing", "preparing_crop_review"} and not job_id:
        failed_document = db.session.get(ProcessedDocument, document.id)
        return jsonify(
            {
                "error": "Document was saved, but processing is temporarily unavailable. You can retry it from the library.",
                "document": failed_document.to_dict() if failed_document else None,
            }
        ), 503

    if job_id:
        # Persist the delivery witness for standalone uploads as well as
        # project-run entries.  The worker still owns terminal cleanup.
        _persist_active_job_marker(document.id, job_id)

    payload = document.to_dict()
    if job_id:
        payload["job_id"] = job_id

    return jsonify(payload), 201


@blueprint.route("/api/documents/<document_id>", methods=["GET"])
@login_required
def get_document(document_id):
    document = ProcessedDocument.query.filter_by(
        id=document_id, user_id=current_user.id
    ).first_or_404()
    return jsonify(_build_document_payload(document, include_output=True))


@blueprint.route("/api/documents/<document_id>/issues", methods=["GET", "POST"])
@blueprint.route("/api/documents/<document_id>/issues/<issue_id>", methods=["GET", "PATCH"])
@login_required
def document_issues(document_id, issue_id=None):
    """Human review and MCP share the same encrypted issue ledger."""
    from .issue_ledger import IssueLedgerService
    from .mcp_service import DocProcessorMcpService, DocProcessorMcpServiceError

    try:
        service = IssueLedgerService(DocProcessorMcpService(None, current_user.id))
        if request.method == "GET":
            if issue_id:
                return jsonify(service.get(document_id, issue_id))
            allowed = {"status", "kind", "page_number", "offset", "limit"}
            if set(request.args) - allowed:
                return jsonify({"error": "Invalid issue filters."}), 400
            filters = dict(request.args)
            for key in ("page_number", "offset", "limit"):
                if key in filters:
                    try:
                        filters[key] = int(filters[key])
                    except (ValueError, TypeError):
                        return jsonify({"error": "Invalid issue pagination or page."}), 400
            return jsonify(service.list(document_id, **filters))
        data = request.get_json(silent=True)
        required = {"expected_revision", "idempotency_key"}
        required |= {"expected_version", "status", "note"} if issue_id else {"page_number", "report"}
        if not isinstance(data, dict) or set(data) != required:
            return jsonify({"error": "Invalid issue review request."}), 400
        result = service.update(document_id, issue_id, **data) if issue_id else service.report(document_id, **data)
        return jsonify(result)
    except DocProcessorMcpServiceError as exc:
        db.session.rollback()
        return jsonify({"error": str(exc), "code": exc.code}), (
            409 if "conflict" in exc.code else 404 if exc.code in {"not_found", "document_not_found", "issue_not_found"} else 400
        )


@blueprint.route("/api/documents/<document_id>/repair-boundaries", methods=["POST"])
@login_required
def repair_document_boundaries(document_id):
    """Preview and explicitly select source joins without changing frozen translations."""
    from .mcp_review import DocumentReviewService
    from .mcp_service import DocProcessorMcpService, DocProcessorMcpServiceError

    data = request.get_json(silent=True)
    allowed = {"decisions", "page_numbers", "offset", "limit", "expected_revision", "idempotency_key", "dry_run"}
    if not isinstance(data, dict) or set(data) - allowed:
        return jsonify({"error": "Invalid boundary repair request."}), 400
    try:
        service = DocumentReviewService(DocProcessorMcpService(None, current_user.id))
        return jsonify(service.repair_boundaries(document_id, **data))
    except DocProcessorMcpServiceError as exc:
        db.session.rollback()
        return jsonify({"error": str(exc), "code": exc.code}), (
            409 if exc.code == "revision_conflict" else 400
        )


@blueprint.route("/api/documents/<document_id>/cleanup", methods=["POST"])
@login_required
def cleanup_document(document_id):
    """Shared owner/revision/undo contract, not whole-HTML replacement."""
    from .mcp_review import DocumentReviewService
    from .mcp_service import DocProcessorMcpService, DocProcessorMcpServiceError

    data = request.get_json(silent=True)
    allowed = {"roles", "page_numbers", "expected_revision", "idempotency_key", "dry_run"}
    if not isinstance(data, dict) or set(data) - allowed:
        return jsonify({"error": "Invalid cleanup request."}), 400
    try:
        service = DocumentReviewService(DocProcessorMcpService(None, current_user.id))
        return jsonify(service.cleanup(document_id, **data))
    except DocProcessorMcpServiceError as exc:
        db.session.rollback()
        return jsonify({"error": str(exc), "code": exc.code}), (
            409 if exc.code == "revision_conflict" else 400
        )


@blueprint.route("/api/documents/<document_id>/cleanup/undo", methods=["POST"])
@login_required
def undo_document_cleanup(document_id):
    from .mcp_review import DocumentReviewService
    from .mcp_service import DocProcessorMcpService, DocProcessorMcpServiceError

    data = request.get_json(silent=True)
    required = {"change_id", "expected_revision", "idempotency_key"}
    if not isinstance(data, dict) or not required <= set(data) or set(data) - (required | {"dry_run"}):
        return jsonify({"error": "Invalid undo request."}), 400
    try:
        service = DocumentReviewService(DocProcessorMcpService(None, current_user.id))
        return jsonify(service.undo(document_id, **data))
    except DocProcessorMcpServiceError as exc:
        db.session.rollback()
        return jsonify({"error": str(exc), "code": exc.code}), (
            409 if exc.code == "revision_conflict" else 400
        )


@blueprint.route("/api/documents/<document_id>/content", methods=["PUT"])
@login_required
def update_document_content(document_id):
    document = ProcessedDocument.query.filter_by(
        id=document_id, user_id=current_user.id
    ).first_or_404()
    data = request.get_json(silent=True) or {}
    if "output_html" not in data:
        return jsonify({"error": "Missing output_html."}), 400
    if not isinstance(data.get("output_html"), str):
        return jsonify({"error": "output_html must be a string."}), 400
    if len(data["output_html"].encode("utf-8")) > MAX_EDITED_HTML_BYTES:
        return jsonify(
            {"error": "Edited document content exceeds the allowed size."}
        ), 413

    semantic = canonicalize_document_html(
        data.get("output_html") or "",
        user_id=document.user_id,
        source_file_path=document.file_path,
    )
    if semantic.errors:
        return jsonify(
            {
                "error": "The edited document has invalid semantic references and was not saved."
            }
        ), 400
    document.settings = semantic_settings(document.settings, semantic)
    document.output_html = semantic.html
    document.output_markdown = html_to_markdown_on_the_fly(semantic.html)
    document.status = "completed"
    document.error_message = None
    document.settings["metadata_stale"] = True
    flag_modified(document, "settings")
    from .knowledge_adapter import sync_processed_document_source

    sync_processed_document_source(document)
    db.session.commit()

    payload = _build_document_payload(document, include_output=True)
    return jsonify(payload)


@blueprint.route("/api/documents/<document_id>/metadata", methods=["PUT"])
@login_required
def update_document_metadata(document_id):
    document = ProcessedDocument.query.filter_by(
        id=document_id, user_id=current_user.id
    ).first_or_404()
    data = request.get_json(silent=True) or {}
    raw_metadata = data.get("metadata", data)
    if not isinstance(raw_metadata, dict):
        return jsonify({"error": "The metadata payload must be an object."}), 400
    document, conflict = _claim_metadata_mutation(document, data)
    if conflict is not None:
        return conflict

    merged_metadata = {**get_canonical_document_metadata(document), **raw_metadata}
    if isinstance(raw_metadata.get("contributors"), dict):
        merged_metadata["contributors"] = {
            **(get_canonical_document_metadata(document).get("contributors") or {}),
            **raw_metadata.get("contributors", {}),
        }

    sync_document_metadata(
        document, merged_metadata, ensure_record=True, mark_stale=False
    )
    if document.metadata_record is not None:
        db.session.add(document.metadata_record)
    if document.output_markdown or document.output_html:
        from .knowledge_adapter import sync_processed_document_source

        sync_processed_document_source(document)
    db.session.commit()
    return jsonify(_build_document_payload(document, include_output=True))


@blueprint.route("/api/documents/<document_id>/metadata/reextract", methods=["POST"])
@login_required
def reextract_document_metadata(document_id):
    document = ProcessedDocument.query.filter_by(
        id=document_id, user_id=current_user.id
    ).first_or_404()
    source_path = _resolve_local_path(document.file_path)
    if not source_path or not source_path.is_file():
        return jsonify({"error": "Source file not found."}), 404

    data = request.get_json(silent=True) or {}
    conflict = _metadata_mutation_conflict(document, data)
    if conflict is not None:
        return conflict
    extraction_revision = _document_revision(document)
    requested_model = (
        str(data.get("metadata_model") or "").strip() if isinstance(data, dict) else ""
    )

    if source_path.suffix.lower() == ".pdf":
        if not fitz:
            return jsonify({"error": "PyMuPDF not installed."}), 500

        from iarf_assistant.core.model_router import ModelRouter
        from iarf_assistant.llm_service import LLMService

        from .parsers.pdf_parser import PdfParser

        work_settings = dict(document.settings or {})
        work_settings.pop("document_metadata", None)
        parser = PdfParser(
            document.file_path,
            document.user_id,
            LLMService(user_id=document.user_id),
            settings=work_settings,
        )
        parser.document_metadata = {}

        pdf_doc = None
        try:
            pdf_doc = fitz.open(source_path)
            crop_settings = _get_document_crop_settings(document)
            _allowed_pages, included_pages, _page_sides, excluded_pages = (
                _build_page_selection(
                    len(pdf_doc),
                    crop_settings["page_range"],
                    crop_settings["excluded_pages"],
                    crop_settings["crop_boxes"]["first_page_side"],
                )
            )
            if not included_pages:
                return jsonify(
                    {
                        "error": "At least one page must remain included for metadata extraction."
                    }
                ), 400

            metadata_scope_pages = [
                page_index
                for page_index in range(len(pdf_doc))
                if page_index not in excluded_pages
            ]
            default_model = ModelRouter.get_model_for_tier("medium", document.user_id)
            metadata_model = (
                requested_model
                or work_settings.get("metadata_model")
                or work_settings.get("parsing_model")
                or default_model
            )

            parser._extract_document_metadata(
                pdf_doc,
                metadata_scope_pages,
                {},
                None,
                metadata_model,
            )
        finally:
            if pdf_doc is not None:
                pdf_doc.close()

        extracted_metadata = (parser.settings or {}).get("document_metadata")
        if not extracted_metadata:
            return jsonify(
                {"error": "No metadata could be extracted from the source PDF."}
            ), 422

        # The provider call can take minutes. Its parser received an initial
        # snapshot, so never assign that snapshot back over a worker's
        # progress/recovery state or a user's concurrent metadata correction.
    else:
        extracted_metadata = extract_source_document_metadata(
            source_path, filename=document.filename
        )
        if not extracted_metadata:
            return jsonify(
                {"error": "No source metadata could be extracted from this document."}
            ), 422

    # Recheck and atomically claim the original revision only after the slow
    # extraction. This closes the final check/commit race as well as detecting
    # any worker or manual edit made during provider I/O.
    db.session.expire(document)
    db.session.refresh(document)
    document, conflict = _claim_metadata_mutation(
        document,
        data,
        expected_revision=extraction_revision,
    )
    if conflict is not None:
        return conflict
    sync_document_metadata(
        document,
        extracted_metadata,
        ensure_record=True,
        mark_stale=False,
    )

    if requested_model:
        document.settings = dict(document.settings or {})
        document.settings["metadata_model"] = requested_model
        flag_modified(document, "settings")

    if document.metadata_record is not None:
        db.session.add(document.metadata_record)
    if document.output_markdown or document.output_html:
        from .knowledge_adapter import sync_processed_document_source

        sync_processed_document_source(document)
    db.session.commit()
    return jsonify(_build_document_payload(document, include_output=True))


@blueprint.route("/api/documents/<document_id>", methods=["DELETE"])
@login_required
def delete_document(document_id):
    document = ProcessedDocument.query.filter_by(
        id=document_id, user_id=current_user.id
    ).first_or_404()
    target_dir = _managed_document_storage_dir(document)
    job_id = (document.settings or {}).get("active_job_id")
    if job_id:
        try:
            _get_task_queue().request_cancel(
                job_id,
                requested_by=current_user.id,
                reason="Document deleted",
            )
        except Exception:
            current_app.logger.warning(
                "Failed to request cancellation for deleted document %s.",
                document.id,
                exc_info=True,
            )

    from .knowledge_adapter import delete_processed_document_source

    delete_processed_document_source(document)
    db.session.delete(document)
    db.session.commit()

    if target_dir and target_dir.exists():
        try:
            shutil.rmtree(target_dir)
        except OSError:
            current_app.logger.warning(
                "Failed to delete source document folder '%s'.",
                target_dir,
                exc_info=True,
            )
    return jsonify({"success": True})


@blueprint.route("/api/documents/<document_id>/process", methods=["POST"])
@login_required
def process_document(document_id):
    document = ProcessedDocument.query.filter_by(
        id=document_id, user_id=current_user.id
    ).first_or_404()

    bound_execution_id = (document.settings or {}).get("mcp_execution_id")
    if bound_execution_id:
        from ...models import McpExecution
        from .execution_recovery import reconcile_resume_dispatch
        from .mcp_service import DocProcessorMcpService

        execution = db.session.get(McpExecution, bound_execution_id)
        recovery = (execution.request_json or {}).get("recovery", {}) if execution else {}
        if (
            execution is not None
            and execution.owner_user_id == current_user.id
            and execution.resource_id == document.id
            and execution.addon_id == "doc_processor"
            and document.status == "processing"
            and (recovery.get("attempts") or recovery.get("initial_attempt"))
            and execution.status in {"queued", "running", "waiting_external"}
        ):
            reconcile_resume_dispatch(
                DocProcessorMcpService(blueprint.addon_context, current_user.id), execution
            )
            return jsonify({"success": True, "job_id": execution.queue_job_id, "idempotent": True}), 202

    if document.status == "suspended":
        from .execution_recovery import resume_execution
        from .mcp_service import DocProcessorMcpService
        from ...core.mcp_jobs import McpWorkError

        settings = document.settings or {}
        execution_id = str(settings.get("mcp_execution_id") or "")
        recovery = settings.get("mcp_recovery") or {}
        # Stable across repeated clicks/reloads for this suspension only.
        key = f"browser-resume:{execution_id}:{recovery.get('generation', 0)}"
        try:
            result = resume_execution(
                DocProcessorMcpService(blueprint.addon_context, current_user.id),
                execution_id,
                key,
            )
        except McpWorkError as exc:
            return jsonify({"error": str(exc), "code": exc.code}), exc.status_code
        return jsonify({"success": True, **result}), 202

    # The document row is the durable intent and the queue marker is the
    # delivery witness.  A browser refresh can arrive after the row is marked
    # active but before the worker starts; in that window an active queue job
    # must be returned rather than duplicated.
    if document.status in DOCUMENT_ACTIVE_STATUSES:
        active_job_id, marker_state = _document_active_job(document)
        if marker_state == "active":
            return jsonify(
                {
                    "success": True,
                    "job_id": active_job_id,
                    "idempotent": True,
                    "message": "Document processing is already in progress.",
                }
            ), 202
        if marker_state == "claim":
            return jsonify(
                {
                    "error": "A processing request is already being queued. Please retry shortly.",
                }
            ), 409
        if marker_state == "unavailable":
            return jsonify(
                {
                    "error": "Document processing is already active, but its queue status is temporarily unavailable. Please retry shortly.",
                }
            ), 503

        # Missing/terminal markers are recoverable stale state.  Clear them
        # before a deliberate retry, but keep the crop-review invariant: a
        # failed preparation is retried as preparation, never as an unreviewed
        # full parse.
        _clear_stale_document_job_marker(document)
        if document.status == "preparing_crop_review":
            source_path = _resolve_local_path(document.file_path)
            if source_path is None or not source_path.is_file():
                db.session.rollback()
                return jsonify({"error": "Source file missing."}), 409
            db.session.commit()
            job_id = _enqueue_document_job(
                document,
                "prepare_crop_review",
                {"document_id": document.id},
            )
            if not job_id:
                return jsonify(
                    {
                        "error": "Processing is temporarily unavailable. Please try again."
                    }
                ), 503
            _persist_active_job_marker(document.id, job_id)
            return jsonify({"success": True, "job_id": job_id, "retried": True}), 202

    if document.status == "preparing_crop_review":
        return jsonify(
            {"error": "PDF metadata pre-pass is still preparing crop review."}
        ), 409

    if document.status == "needs_cropping":
        return jsonify(
            {"error": "PDF crop box is required before processing can begin."}
        ), 409

    if document.status == "reviewing_images":
        return jsonify({"error": "Document is waiting for image review."}), 409

    # Do not mutate settings/status or enqueue work for stale records.  The
    # canonical uploads root is independent of the installed package root.
    source_path = _resolve_local_path(document.file_path)
    if source_path is None or not source_path.is_file():
        return jsonify({"error": "Source file missing."}), 409

    data = _coerce_request_data()
    resumable_pdf = _has_resumable_pdf_state(document)
    recovery_pdf = _has_pdf_recovery_state(document)
    try:
        # A partial PDF state is a continuation contract, not a fresh parse.
        # In particular, ignore browser model/options submitted by a refreshed
        # page so an interrupted run cannot silently change model halfway
        # through its page sequence.
        candidate_settings, _ = (
            (deepcopy(dict(document.settings or {})), False)
            if resumable_pdf or recovery_pdf
            else _retry_settings_with_explicit_overrides(document, data)
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    engine_id = candidate_settings.get("engine_id", "auto")
    model_error = validate_pdf_model_readiness(
        str(document.file_path),
        candidate_settings,
        user_id=current_user.id,
    )
    if model_error:
        return jsonify({"error": model_error}), 400

    document.settings = candidate_settings
    document.settings["enqueue_claim"] = {
        "token": uuid.uuid4().hex,
        "created_at": time.time(),
    }
    flag_modified(document, "settings")
    document.status = "processing"
    db.session.commit()

    from iarf_assistant.addons.doc_processor.project_service import (
        attach_manual_review_job,
        find_and_link_active_project_run_document,
        sync_project_run_document,
    )

    payload = {"document_id": document.id, "engine_id": engine_id}
    entry_id = find_and_link_active_project_run_document(document)
    if entry_id:
        payload["project_run_document_id"] = entry_id
        sync_project_run_document(entry_id, "processing")

    task_name = "resume_pdf_parsing" if resumable_pdf else "process_document"
    job_id = _enqueue_document_job(
        document,
        task_name,
        payload,
        project_run_document_id=entry_id,
    )
    if not job_id:
        return jsonify(
            {"error": "Processing is temporarily unavailable. Please try again."}
        ), 503

    _persist_active_job_marker(document.id, job_id)

    if entry_id:
        attach_manual_review_job(document, job_id, "processing")

    return jsonify({"success": True, "job_id": job_id})


@blueprint.route("/api/documents/<document_id>/pdf-preview", methods=["GET"])
@login_required
def get_pdf_preview(document_id):
    document = ProcessedDocument.query.filter_by(
        id=document_id, user_id=current_user.id
    ).first_or_404()
    if not str(document.file_path).lower().endswith(".pdf"):
        return jsonify({"error": "Document is not a PDF."}), 400

    pdf_path = _resolve_local_path(document.file_path)
    if not pdf_path or not pdf_path.is_file():
        return jsonify({"error": "Source PDF file not found."}), 404

    if not fitz:
        return jsonify({"error": "PyMuPDF not installed."}), 500

    current_crop_settings = _get_document_crop_settings(document)
    crop_mode = _normalize_crop_mode(
        request.args.get("crop_mode") or current_crop_settings["crop_boxes"]["mode"]
    )
    first_page_side = _normalize_first_page_side(
        request.args.get("first_page_side")
        or current_crop_settings["crop_boxes"]["first_page_side"]
    )

    raw_excluded_pages = request.args.get("excluded_pages")
    excluded_pages = (
        current_crop_settings["excluded_pages"]
        if raw_excluded_pages is None
        else _parse_page_index_list(raw_excluded_pages)
    )

    doc = None
    try:
        doc = fitz.open(pdf_path)
        total_pages = len(doc)
        allowed_pages, included_pages, page_sides, _ = _build_page_selection(
            total_pages,
            current_crop_settings["page_range"],
            excluded_pages,
            first_page_side,
        )

        if not included_pages:
            return jsonify(
                {
                    "error": "At least one page must remain included in the crop workflow."
                }
            ), 400

        preview_groups = {}
        if crop_mode == "left_right":
            preview_groups["left"] = [
                page_index
                for page_index in included_pages
                if page_sides.get(page_index) == "left"
            ]
            preview_groups["right"] = [
                page_index
                for page_index in included_pages
                if page_sides.get(page_index) == "right"
            ]
        else:
            preview_groups["single"] = included_pages

        previews = {}
        for view_key, group_pages in preview_groups.items():
            preview = _build_density_stack_preview(
                doc,
                group_pages,
                excluded_page_indices=excluded_pages,
            )
            previews[view_key] = preview or _empty_stack_preview(
                "stack_unavailable", len(group_pages)
            )

        return jsonify(
            {
                "success": True,
                "crop_mode": crop_mode,
                "first_page_side": first_page_side,
                "total_pages": total_pages,
                "page_range_count": len(allowed_pages),
                "included_page_count": len(included_pages),
                "stacking_mode": "all_pages_density",
                "previews": previews,
            }
        )
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    finally:
        if doc is not None:
            try:
                doc.close()
            except Exception:
                pass


@blueprint.route("/api/documents/<document_id>/pdf-pages", methods=["GET"])
@login_required
def get_pdf_pages(document_id):
    document = ProcessedDocument.query.filter_by(
        id=document_id, user_id=current_user.id
    ).first_or_404()
    if not str(document.file_path).lower().endswith(".pdf"):
        return jsonify({"error": "Document is not a PDF."}), 400

    pdf_path = _resolve_local_path(document.file_path)
    if not pdf_path or not pdf_path.is_file():
        return jsonify({"error": "Source PDF file not found."}), 404

    if not fitz:
        return jsonify({"error": "PyMuPDF not installed."}), 500

    crop_settings = _get_document_crop_settings(document)

    doc = None
    try:
        doc = fitz.open(pdf_path)
        total_pages = len(doc)
        metadata_suggested_pages = _metadata_flagged_page_indexes(
            get_canonical_document_metadata(document),
            total_pages,
        )
        crop_settings = _get_document_crop_settings(document)
        pdf_options = dict((document.settings or {}).get("pdf_options", {}) or {})
        apply_metadata_suggestions = (
            bool(metadata_suggested_pages)
            and (
                pdf_options.get("metadata_delete_flagged_pages", False)
                or pdf_options.get("metadata_exclude_flagged_pages", False)
            )
            and not pdf_options.get("metadata_review_applied", False)
        )
        effective_excluded_pages = sorted(
            set(crop_settings["excluded_pages"])
            | (metadata_suggested_pages if apply_metadata_suggestions else set())
        )
        allowed_pages, _, page_sides, excluded_set = _build_page_selection(
            total_pages,
            crop_settings["page_range"],
            effective_excluded_pages,
            crop_settings["crop_boxes"]["first_page_side"],
        )

        pages = []
        for page_index in range(total_pages):
            pages.append(
                {
                    "page_index": page_index,
                    "page_number": page_index + 1,
                    "in_page_range": page_index in allowed_pages,
                    "excluded": page_index in excluded_set,
                    "metadata_suggested_exclusion": page_index
                    in metadata_suggested_pages,
                    "side": page_sides.get(page_index),
                    "thumbnail_url": url_for(
                        "doc_processor.get_pdf_page_thumbnail",
                        document_id=document.id,
                        page_index=page_index,
                        width=220,
                    ),
                }
            )

        crop_settings["excluded_pages"] = effective_excluded_pages
        crop_settings["metadata_suggested_excluded_pages"] = sorted(
            metadata_suggested_pages
        )
        crop_settings["metadata_suggestions_applied"] = apply_metadata_suggestions

        return jsonify(
            {
                "success": True,
                "total_pages": total_pages,
                "pages": pages,
                "crop_settings": crop_settings,
            }
        )
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    finally:
        if doc is not None:
            try:
                doc.close()
            except Exception:
                pass


@blueprint.route(
    "/api/documents/<document_id>/pdf-pages/<int:page_index>/thumbnail", methods=["GET"]
)
@login_required
def get_pdf_page_thumbnail(document_id, page_index):
    document = ProcessedDocument.query.filter_by(
        id=document_id, user_id=current_user.id
    ).first_or_404()
    if not str(document.file_path).lower().endswith(".pdf"):
        return jsonify({"error": "Document is not a PDF."}), 400

    pdf_path = _resolve_local_path(document.file_path)
    if not pdf_path or not pdf_path.is_file():
        return jsonify({"error": "Source PDF file not found."}), 404

    if not fitz:
        return jsonify({"error": "PyMuPDF not installed."}), 500

    try:
        target_longest_edge = max(
            96, min(int(request.args.get("width", 220) or 220), 1800)
        )
    except TypeError, ValueError:
        target_longest_edge = 220

    doc = None
    try:
        doc = fitz.open(pdf_path)
        if page_index < 0 or page_index >= len(doc):
            return jsonify({"error": "PDF page index out of range."}), 404

        image = _render_pdf_page_image(
            doc[page_index], target_longest_edge=target_longest_edge
        )
        buffered = io.BytesIO()
        image.save(buffered, format="PNG")
        buffered.seek(0)
        return send_file(buffered, mimetype="image/png")
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    finally:
        if doc is not None:
            try:
                doc.close()
            except Exception:
                pass


@blueprint.route("/api/documents/<document_id>/source-file", methods=["GET"])
@login_required
def get_source_file(document_id):
    """Serve the original uploaded source file (PDF, etc.) for inline preview or download."""
    document = ProcessedDocument.query.filter_by(
        id=document_id, user_id=current_user.id
    ).first_or_404()

    local_path = _resolve_local_path(document.file_path)
    if not local_path or not local_path.is_file():
        return jsonify({"error": "Source file not found."}), 404

    disposition = request.args.get("disposition", "inline")
    if disposition not in ("inline", "attachment"):
        disposition = "inline"

    suffix = local_path.suffix.lower()
    mime_map = {
        ".pdf": "application/pdf",
        ".txt": "text/plain",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".doc": "application/msword",
        ".epub": "application/epub+zip",
    }
    mimetype = mime_map.get(suffix, "application/octet-stream")
    download_name = secure_filename(document.filename) or local_path.name

    response = send_file(
        local_path,
        mimetype=mimetype,
        as_attachment=(disposition == "attachment"),
        download_name=download_name,
    )
    if disposition == "inline" and suffix == ".pdf":
        # The global application policy deliberately forbids framing. This
        # authenticated source endpoint is the narrow exception used by the
        # Document Processor's same-origin PDF/text comparison view.
        response.headers["Content-Security-Policy"] = "frame-ancestors 'self'"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
    return response


@blueprint.route("/api/documents/<document_id>/set-crop", methods=["POST"])
@login_required
def set_pdf_crop(document_id):
    document = ProcessedDocument.query.filter_by(
        id=document_id, user_id=current_user.id
    ).first_or_404()
    data = request.get_json(silent=True) or {}
    crop_box = _sanitize_crop_box(data.get("crop_box"))
    raw_crop_boxes = (
        data.get("crop_boxes") if isinstance(data.get("crop_boxes"), dict) else {}
    )
    crop_boxes = {
        "mode": _normalize_crop_mode(raw_crop_boxes.get("mode")),
        "left": _sanitize_crop_box(raw_crop_boxes.get("left")),
        "right": _sanitize_crop_box(raw_crop_boxes.get("right")),
        "first_page_side": _normalize_first_page_side(
            raw_crop_boxes.get("first_page_side")
        ),
    }
    excluded_pages = _parse_page_index_list(data.get("excluded_pages"))

    pdf_path = _resolve_local_path(document.file_path)
    if not pdf_path or not pdf_path.is_file():
        return jsonify({"error": "Source PDF file not found."}), 404

    try:
        page_range = _get_document_crop_settings(document)["page_range"]
        _, included_pages, _, _ = _build_page_selection(
            _pdf_page_count(pdf_path),
            page_range,
            excluded_pages,
            crop_boxes["first_page_side"],
        )
        if not included_pages:
            return jsonify(
                {
                    "error": "At least one page must remain included in the crop workflow."
                }
            ), 400
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    if document.settings is None:
        document.settings = {}
    document.settings["crop_box"] = crop_box
    document.settings["crop_boxes"] = crop_boxes
    pdf_options = dict(document.settings.get("pdf_options", {}) or {})
    pdf_options["excluded_pages"] = excluded_pages
    pdf_options["metadata_review_applied"] = True
    if pdf_options.get("metadata_exclude_flagged_pages"):
        pdf_options["metadata_exclude_flagged_pages"] = False
    if pdf_options.get("metadata_delete_flagged_pages"):
        pdf_options["metadata_delete_flagged_pages"] = False
    document.settings["pdf_options"] = pdf_options
    document.settings["engine_id"] = "llm_vision"
    flag_modified(document, "settings")
    document.status = "processing"
    db.session.commit()

    payload = {"document_id": document.id, "engine_id": "llm_vision"}
    from iarf_assistant.addons.doc_processor.project_service import (
        find_and_link_active_project_run_document,
    )

    entry_id = find_and_link_active_project_run_document(document)
    if entry_id:
        payload["project_run_document_id"] = entry_id

    job_id = _enqueue_document_job(
        document,
        "process_document",
        payload,
        project_run_document_id=entry_id,
    )
    if not job_id:
        return jsonify(
            {"error": "Processing is temporarily unavailable. Please try again."}
        ), 503

    if entry_id:
        from iarf_assistant.addons.doc_processor.project_service import (
            attach_manual_review_job,
        )

        attach_manual_review_job(document, job_id, "processing")

    return jsonify({"success": True, "job_id": job_id})


@blueprint.route("/api/documents/<document_id>/pdf-images", methods=["GET"])
@login_required
def get_pdf_images(document_id):
    document = ProcessedDocument.query.filter_by(
        id=document_id, user_id=current_user.id
    ).first_or_404()
    state = (document.settings or {}).get("pdf_state", {})
    html_parts = state.get("html_parts", [])
    png_paths = state.get("png_paths", [])
    return jsonify(_build_page_image_payload(html_parts, png_paths))


@blueprint.route("/api/documents/<document_id>/pdf-images", methods=["PUT"])
@login_required
def update_pdf_images(document_id):
    document = ProcessedDocument.query.filter_by(
        id=document_id, user_id=current_user.id
    ).first_or_404()
    source_path = _resolve_local_path(document.file_path)
    if source_path is None or not source_path.is_file():
        return jsonify({"error": "Source file missing."}), 409
    state = (document.settings or {}).get("pdf_state", {})
    if not _has_resumable_pdf_state(document):
        return jsonify(
            {
                "error": "PDF image review can resume only after every selected page has been extracted."
            }
        ), 409
    html_parts = state.get("html_parts", [])

    if not html_parts:
        return jsonify({"error": "No PDF review state found for this document."}), 400

    review_pages, validation_error = _normalize_image_review_payload(
        request.get_json(silent=True),
        html_parts,
    )
    if validation_error:
        return jsonify({"error": validation_error}), 400

    for page_data in review_pages:
        idx = page_data["page_index"]
        images = page_data["images"]

        if idx < 0 or idx >= len(html_parts) or html_parts[idx] is None:
            continue

        soup = BeautifulSoup(html_parts[idx], "html.parser")
        img_tags = soup.find_all("img")

        for img_info in images:
            img_idx = img_info["img_idx"]
            if img_idx >= len(img_tags):
                if not img_info.get("keep", True):
                    continue

                bbox = img_info["bbox"]
                new_img = soup.new_tag("img")
                new_img["src"] = f"[{bbox[0]}, {bbox[1]}, {bbox[2]}, {bbox[3]}]"

                caption_text = img_info.get("caption", "").strip()
                target_container = (
                    soup.find("main_content") or soup.find("main") or soup
                )

                if caption_text:
                    new_figure = soup.new_tag("figure")
                    new_figure.append(new_img)
                    new_caption = soup.new_tag("figcaption")
                    new_caption.string = caption_text
                    new_figure.append(new_caption)
                    target_container.append(new_figure)
                else:
                    target_container.append(new_img)
                continue

            img_tag = img_tags[img_idx]

            if not img_info.get("keep", True):
                parent = img_tag.find_parent("figure")
                if parent:
                    parent.decompose()
                else:
                    img_tag.decompose()
                continue

            bbox = img_info["bbox"]
            img_tag["src"] = f"[{bbox[0]}, {bbox[1]}, {bbox[2]}, {bbox[3]}]"

            caption_text = img_info.get("caption", "").strip()
            parent = img_tag.find_parent("figure")
            if parent:
                figcaption = parent.find("figcaption")
                if figcaption:
                    if caption_text:
                        figcaption.string = caption_text
                    else:
                        figcaption.decompose()
                elif caption_text:
                    new_caption = soup.new_tag("figcaption")
                    new_caption.string = caption_text
                    parent.append(new_caption)

        html_parts[idx] = str(soup)

    document.settings["pdf_state"]["html_parts"] = html_parts
    flag_modified(document, "settings")
    document.status = "processing"
    db.session.commit()

    payload = {"document_id": document.id}
    from iarf_assistant.addons.doc_processor.project_service import (
        find_and_link_active_project_run_document,
    )

    entry_id = find_and_link_active_project_run_document(document)
    if entry_id:
        payload["project_run_document_id"] = entry_id

    job_id = _enqueue_document_job(
        document,
        "resume_pdf_parsing",
        payload,
        project_run_document_id=entry_id,
    )
    if not job_id:
        return jsonify(
            {"error": "Processing is temporarily unavailable. Please try again."}
        ), 503

    if entry_id:
        from iarf_assistant.addons.doc_processor.project_service import (
            attach_manual_review_job,
        )

        attach_manual_review_job(document, job_id, "processing")

    return jsonify({"success": True, "job_id": job_id})


def _completed_pdf_crop_paths(document: ProcessedDocument):
    uploads_dir = uploads_root().resolve()
    owner_root = (uploads_dir / str(document.user_id)).resolve()
    try:
        source_path = resolve_upload_reference(
            document.file_path, uploads_dir=uploads_dir
        )
        if source_path is None:
            return None
        source_path = source_path.resolve(strict=True)
        source_path.relative_to(owner_root)
        document_dir = source_path.parent.resolve(strict=True)
        document_dir.relative_to(owner_root)
    except (OSError, RuntimeError, TypeError, ValueError):
        return None
    if not source_path.is_file() or source_path.suffix.casefold() != ".pdf":
        return None
    return uploads_dir, owner_root, source_path, document_dir


def _completed_pdf_crop_html(document: ProcessedDocument) -> str | None:
    if document.status not in {"completed", "completed_with_errors"}:
        return None
    if not isinstance(document.output_html, str) or not document.output_html.strip():
        return None
    if len(document.output_html.encode("utf-8")) > MAX_EDITED_HTML_BYTES:
        return None
    try:
        html_content, _semantic = _canonical_output_for_read(document)
    except Exception:
        return None
    return html_content if html_content.strip() else None


@blueprint.route("/api/documents/<document_id>/image-crops", methods=["GET"])
@login_required
def get_completed_image_crops(document_id):
    document = ProcessedDocument.query.filter_by(
        id=document_id, user_id=current_user.id
    ).first_or_404()
    html_content = _completed_pdf_crop_html(document)
    paths = _completed_pdf_crop_paths(document)
    if html_content is None or paths is None:
        return jsonify({"error": "Completed PDF image crops are unavailable."}), 409
    uploads_dir, _owner_root, source_path, document_dir = paths
    try:
        page_count = _pdf_page_count(source_path)
        pages = build_completed_image_groups(
            html_content,
            (document.settings or {}).get("image_crops"),
            uploads_dir=uploads_dir,
            owner_id=document.user_id,
            document_dir=document_dir,
        )
    except (ImageCropError, OSError, RuntimeError, ValueError):
        return jsonify({"error": "A document image is unavailable."}), 409
    revision = _document_revision(document)
    if revision is None:
        return jsonify({"error": "Document revision is unavailable."}), 409
    return jsonify(
        {
            "pages": pages,
            "expected_updated_at": revision,
            "page_count": page_count,
        }
    )


@blueprint.route("/api/documents/<document_id>/image-crops", methods=["PUT"])
@login_required
def update_completed_image_crops(document_id):
    if request.content_length is not None and request.content_length > MAX_EDITED_HTML_BYTES:
        return jsonify({"error": "Image-crop request is too large."}), 413
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or set(data) != {"expected_updated_at", "pages"}:
        return jsonify({"error": "Invalid image-crop request."}), 400
    expected_updated_at = data.get("expected_updated_at")
    if (
        not isinstance(expected_updated_at, str)
        or not expected_updated_at.strip()
        or len(expected_updated_at) > 128
    ):
        return jsonify({"error": "A document revision is required."}), 400

    document = ProcessedDocument.query.filter_by(
        id=document_id, user_id=current_user.id
    ).first_or_404()
    html_content = _completed_pdf_crop_html(document)
    paths = _completed_pdf_crop_paths(document)
    if html_content is None or paths is None:
        return jsonify({"error": "Completed PDF image crops are unavailable."}), 409
    uploads_dir, _owner_root, source_path, document_dir = paths
    current_revision = _document_revision(document)
    if expected_updated_at != current_revision:
        conflict = _metadata_mutation_conflict(
            document, data, stale_only=True, include_document=False
        )
        assert conflict is not None
        return conflict

    try:
        page_count = _pdf_page_count(source_path)
        groups = build_completed_image_groups(
            html_content,
            (document.settings or {}).get("image_crops"),
            uploads_dir=uploads_dir,
            owner_id=document.user_id,
            document_dir=document_dir,
        )
    except (ImageCropError, OSError, RuntimeError, ValueError):
        return jsonify({"error": "A document image is unavailable."}), 409

    normalized_pages, validation_error = normalize_completed_crop_payload(
        data, groups, page_count=page_count
    )
    if validation_error or normalized_pages is None:
        return jsonify({"error": validation_error or "Invalid image-crop request."}), 400

    pages_by_index = {page["page_index"]: page for page in normalized_pages}
    has_changes = False
    for group in groups:
        page = pages_by_index[group["page_index"]]
        submitted_by_index = {
            image["img_idx"]: image for image in page["images"]
        }
        if page.get("source_page_index") is not None or any(
            submitted_by_index[image["img_idx"]]["bbox"] != image["bbox"]
            for image in group["images"]
        ):
            has_changes = True
            break
    if not has_changes:
        return jsonify({"success": True})

    soup = BeautifulSoup(html_content, "html.parser")
    img_tags = soup.find_all("img")
    old_image_crops = (document.settings or {}).get("image_crops")
    old_image_crops = old_image_crops if isinstance(old_image_crops, dict) else {}
    updated_image_crops: dict[str, dict] = {}
    changed_image_indexes = set()
    web_path_prefix = upload_url(document_dir, uploads_dir)
    prepared_sources: dict[int, tuple[Path, str, int | None]] = {}
    rendered_paths: list[Path] = []

    try:
        for group in groups:
            page = pages_by_index[group["page_index"]]
            requested_source_page = page.get("source_page_index")
            if requested_source_page is not None:
                rendered_path = render_pdf_page_png(
                    source_path,
                    requested_source_page,
                    document_dir,
                    max_edge=2048,
                )
                rendered_paths.append(rendered_path)
                retained_url = retain_source_png(
                    rendered_path, document_dir, web_path_prefix
                )
                retained_path = resolve_upload_reference(
                    retained_url, uploads_dir=uploads_dir
                )
                if retained_path is None:
                    raise ImageCropError("Rendered PDF source image is unavailable.")
                prepared_sources[group["page_index"]] = (
                    retained_path.resolve(strict=True),
                    retained_url,
                    requested_source_page,
                )
            else:
                source_path_for_group = resolve_upload_reference(
                    group["image_url"], uploads_dir=uploads_dir
                )
                if source_path_for_group is None:
                    raise ImageCropError("Document image source is unavailable.")
                source_path_for_group = source_path_for_group.resolve(strict=True)
                source_path_for_group.relative_to(document_dir)
                source_page_index = group.get("source_page_index")
                prepared_sources[group["page_index"]] = (
                    source_path_for_group,
                    group["image_url"],
                    source_page_index,
                )

        for group in groups:
            page = pages_by_index[group["page_index"]]
            source_path_for_group, source_url, source_page_index = prepared_sources[
                group["page_index"]
            ]
            reanchored = page.get("source_page_index") is not None
            submitted_images = {
                image["img_idx"]: image for image in page["images"]
            }
            current_images = {
                image["img_idx"]: image for image in group["images"]
            }
            for img_idx, current_image in current_images.items():
                submitted = submitted_images[img_idx]
                bbox = submitted["bbox"]
                if img_idx >= len(img_tags):
                    raise ImageCropError("A document image is unavailable.")
                img_tag = img_tags[img_idx]
                current_src = str(img_tag.get("src") or "")
                changed = reanchored or bbox != current_image["bbox"]
                if changed:
                    crop_url = crop_image_file(
                        source_path_for_group,
                        bbox,
                        document_dir,
                        web_path_prefix,
                    )
                    img_tag["src"] = crop_url
                    img_tag.attrs.pop("width", None)
                    img_tag.attrs.pop("height", None)
                    changed_image_indexes.add(img_idx)
                    updated_image_crops[crop_url] = {
                        "source_url": source_url,
                        "page_index": source_page_index,
                        "bbox": bbox,
                    }
                    continue

                existing_metadata = old_image_crops.get(current_src)
                if isinstance(existing_metadata, dict):
                    updated_image_crops[current_src] = existing_metadata
                else:
                    updated_image_crops[current_src] = {
                        "source_url": source_url,
                        "page_index": source_page_index,
                        "bbox": current_image["bbox"],
                    }

        if not changed_image_indexes:
            return jsonify({"success": True})

        updated_html = str(soup)
        semantic = canonicalize_document_html(
            updated_html,
            user_id=document.user_id,
            source_file_path=document.file_path,
            relaxed=False,
        )
        if semantic.errors:
            db.session.rollback()
            return jsonify({"error": "Updated image crops failed semantic validation."}), 409
        canonical_sources = {
            str(tag.get("src") or "")
            for tag in BeautifulSoup(semantic.html, "html.parser").find_all("img")
        }
        if canonical_sources != set(updated_image_crops):
            db.session.rollback()
            return jsonify({"error": "Updated image crops failed semantic validation."}), 409
        markdown = html_to_markdown_on_the_fly(semantic.html)

        claimed_document, conflict = _claim_metadata_mutation(
            document, data, include_document=False
        )
        if conflict is not None:
            db.session.rollback()
            return conflict
        document = claimed_document
        document.output_html = semantic.html
        document.output_markdown = markdown
        document.settings = semantic_settings(
            document.settings, semantic, mode="completed"
        )
        document.settings["image_crops"] = updated_image_crops
        flag_modified(document, "settings")
        from .knowledge_adapter import sync_processed_document_source

        sync_processed_document_source(document)
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        current_app.logger.error(
            "Completed PDF image-crop update failed for document %s (%s).",
            document_id,
            type(exc).__name__,
        )
        return jsonify({"error": "Unable to update document image crops."}), 409
    finally:
        for rendered_path in rendered_paths:
            try:
                rendered_path.unlink(missing_ok=True)
            except OSError:
                pass

    return jsonify({"success": True})


@blueprint.route("/api/jobs/<job_id>", methods=["GET"])
@login_required
def get_job_status(job_id):
    queue = _get_task_queue()
    status = _owned_job_status(queue, job_id)
    if status is None:
        return jsonify({"error": "Job was not found."}), 404
    return jsonify(_public_job_status(job_id, status))


@blueprint.route("/api/jobs/<job_id>/download", methods=["GET"])
@login_required
def download_job_result(job_id):
    queue = _get_task_queue()
    status = _owned_job_status(queue, job_id)
    if status is None:
        return jsonify({"error": "Download is not available."}), 404
    if status.get("status") != "completed":
        return jsonify({"error": "Job output is not ready."}), 409

    result = status.get("result") if isinstance(status.get("result"), dict) else {}
    if result.get("kind") != "file_download":
        return jsonify({"error": "Job does not have a downloadable result."}), 404

    try:
        result_user_id = int(result.get("user_id"))
    except TypeError, ValueError:
        return jsonify({"error": "Download is not available."}), 404

    if result_user_id != int(current_user.id):
        return jsonify({"error": "Download is not available."}), 404

    output_path = Path(result.get("output_path") or "")
    if not output_path.is_file():
        return jsonify({"error": "Generated file is no longer available."}), 404

    return send_file(
        output_path,
        as_attachment=True,
        download_name=result.get("download_name") or output_path.name,
        mimetype=result.get("mimetype"),
    )


def _render_text_document_export(document, export_format: str) -> tuple[str, str]:
    """Render the same portable text artifact for HTTP and queued MCP exports."""
    safe_name = secure_filename(document.filename.rsplit(".", 1)[0])
    if not safe_name:
        safe_name = "processed_document"
    document_metadata = get_canonical_document_metadata(document)
    document_title = (
        get_document_display_title(document, document_metadata) or safe_name
    )

    if export_format == "md":
        if document.output_html:
            markdown_source = BeautifulSoup(document.output_html, "html.parser")
            if str(document.file_path or "").lower().endswith(".pdf"):
                from .parsers.pdf_utils import remove_source_toc_blocks

                remove_source_toc_blocks(markdown_source)
            content = html_to_markdown_on_the_fly(str(markdown_source))
        else:
            content = document.output_markdown or "No markdown content available."
        return content, "text/markdown"

    if export_format == "html":
        raw_content = document.output_html or "No HTML content available."
        sanitized_content = _sanitize_document_html(raw_content)
        soup = BeautifulSoup(sanitized_content, "html.parser")
        if str(document.file_path or "").lower().endswith(".pdf"):
            from .parsers.pdf_utils import remove_source_toc_blocks

            remove_source_toc_blocks(soup)
        _embed_local_images_in_html(soup, document)
        content = str(soup)

        content = _build_standalone_html(
            content,
            document_title,
            document_metadata.get("language"),
            styles=_PORTABLE_EXPORT_STYLES,
        )
        from carlina.core.diagram_rendering import project_diagrams_html

        return project_diagrams_html(content), "text/html"

    raise ValueError("Unsupported text export format.")


@blueprint.route("/api/documents/<document_id>/export", methods=["GET", "POST"])
@login_required
def export_document(document_id):
    """Export a processed document as Markdown, HTML, EPUB, DOCX, or PDF."""
    document = _get_user_document(document_id)
    export_format = request.args.get("format", "md").lower()
    from carlina.core.epub_cover import normalize_epub_cover

    if export_format not in {"md", "html", "epub", "docx", "pdf"}:
        return jsonify({"error": "Invalid format requested."}), 400

    try:
        epub_options = None
        expected_revision = None
        body = {}
        if request.method == "POST":
            body = request.get_json(silent=True)
            if not isinstance(body, dict) or not set(body) <= {
                "epub_options",
                "expected_revision",
                "epub_review_hash",
                "epub_cover",
                "epub_image_mode",
            }:
                raise ValueError("Invalid EPUB export request.")
            if {
                "epub_options",
                "expected_revision",
                "epub_review_hash",
            }.intersection(request.args):
                raise ValueError("Supply EPUB options and revision in the JSON body.")
            options_supplied = "epub_options" in body
            options_value = body.get("epub_options")
            revision_supplied = "expected_revision" in body
            expected_revision = body.get("expected_revision")
            review_hash_supplied = "epub_review_hash" in body
            epub_review_hash = body.get("epub_review_hash")
        else:
            option_values = request.args.getlist("epub_options")
            revision_values = request.args.getlist("expected_revision")
            review_hash_values = request.args.getlist("epub_review_hash")
            if (
                len(option_values) > 1
                or len(revision_values) > 1
                or len(review_hash_values) > 1
            ):
                raise ValueError("Supply EPUB options, revision, and review witness only once.")
            options_supplied = bool(option_values)
            options_value = json.loads(option_values[0]) if option_values else None
            revision_supplied = bool(revision_values)
            expected_revision = revision_values[0] if revision_values else None
            review_hash_supplied = bool(review_hash_values)
            epub_review_hash = review_hash_values[0] if review_hash_values else None
        if export_format != "epub" and (
            options_supplied or revision_supplied or review_hash_supplied
        ):
            raise ValueError("EPUB options, revision, and review witness require EPUB.")
        if export_format == "epub":
            from carlina.core.epub_export import normalize_epub_options

            if options_supplied and not isinstance(options_value, dict):
                raise ValueError("Invalid EPUB publishing options.")
            epub_options = normalize_epub_options(
                options_value if options_supplied else {}
            )
            if revision_supplied and not isinstance(expected_revision, str):
                raise ValueError("Invalid document revision.")
            if review_hash_supplied and (
                not isinstance(epub_review_hash, str)
                or len(epub_review_hash) != 64
                or any(char not in "0123456789abcdef" for char in epub_review_hash)
            ):
                raise ValueError("Invalid EPUB review witness.")
        cover_fields = {"epub_cover_mode", "epub_cover_page"}
        if any(
            len(request.args.getlist(key)) > 1
            for key in cover_fields | {"epub_image_mode"}
        ):
            raise ValueError("Supply each EPUB image and cover option only once.")
        if export_format != "epub" and (
            cover_fields.intersection(request.args) or "epub_image_mode" in request.args
            or "epub_image_mode" in body
        ):
            raise ValueError("Cover selection requires EPUB.")
        cover_page = request.args.get("epub_cover_page")
        if cover_page is not None and (
            not cover_page.isascii()
            or not cover_page.isdecimal()
            or len(cover_page) > 7
        ):
            raise ValueError("Invalid physical PDF cover page.")
        if request.method == "POST" and "epub_cover" in body and (export_format != "epub" or cover_fields.intersection(request.args)):
            raise ValueError("Supply one EPUB cover choice for EPUB export.")
        epub_cover = normalize_epub_cover(
            body["epub_cover"] if request.method == "POST" and "epub_cover" in body else {
                "mode": request.args.get("epub_cover_mode", "existing"),
                "page": int(cover_page) if cover_page is not None else None,
            }
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    heading_value = request.args.get("start_h2_on_new_page")
    if heading_value is not None and heading_value not in {"true", "false", "1", "0"}:
        return jsonify({"error": "Invalid heading pagination option."}), 400
    start_h2_on_new_page = heading_value in {"true", "1"}
    if start_h2_on_new_page and export_format not in {"docx", "pdf"}:
        return jsonify({"error": "Heading pagination requires DOCX or PDF."}), 400

    if export_format in {"md", "html"}:
        if export_format == "md":
            content, mimetype = _render_text_document_export(document, export_format)
        else:
            try:
                content, mimetype = _render_text_document_export(document, export_format)
            except (OSError, RuntimeError, ValueError):
                return jsonify({"error": "Diagram rendering is unavailable. Please try the export again."}), 503
        safe_name = secure_filename(document.filename.rsplit(".", 1)[0]) or "processed_document"
        return Response(
            content, mimetype=mimetype,
            headers={"Content-disposition": f'attachment; filename="{safe_name}.{export_format}"'},
        )

    if export_format in {"epub", "docx", "pdf"}:
        epub_image_mode = (
            _requested_epub_image_mode() if export_format == "epub" else "auto"
        )
        if "epub_image_mode" in body:
            from carlina.core.epub_export import normalize_epub_image_mode

            if "epub_image_mode" in request.args:
                return jsonify({"error": "Supply the EPUB image mode only once."}), 400
            try:
                epub_image_mode = normalize_epub_image_mode(body["epub_image_mode"])
            except ValueError:
                return jsonify({"error": "Invalid EPUB image mode requested."}), 400
        include_source_toc = False
        if epub_image_mode is None:
            return jsonify({"error": "Invalid EPUB image mode requested."}), 400
        export_payload = {
            "document_id": document.id,
            "user_id": current_user.id,
            "format": export_format,
        }
        if export_format == "epub":
            from .epub_quality import direct_epub_review_sha256
            from .mcp_review import _revision

            current_revision = _revision(document.output_html or "")
            if expected_revision is not None and (
                len(expected_revision) != 64
                or any(char not in "0123456789abcdef" for char in expected_revision)
            ):
                return jsonify({"error": "Invalid document revision."}), 400
            if expected_revision is not None and expected_revision != current_revision:
                return jsonify({"error": "The document changed before export."}), 409
            export_payload.update(
                {
                    "epub_image_mode": epub_image_mode,
                    "include_source_toc": include_source_toc,
                    "epub_options": epub_options,
                    "expected_revision": current_revision,
                }
            )
            if epub_review_hash is not None:
                if epub_options is None:
                    return jsonify({"error": "Invalid EPUB publishing options."}), 400
                expected_review_hash = direct_epub_review_sha256(
                    current_revision,
                    get_canonical_document_metadata(document),
                    epub_options,
                    cover=epub_cover,
                    image_mode=epub_image_mode,
                )
                if epub_review_hash != expected_review_hash:
                    return jsonify(
                        {
                            "error": "The EPUB choices changed after preflight.",
                            "error_code": "epub-review-stale",
                        }
                    ), 409
                export_payload["epub_review_hash"] = epub_review_hash
        if start_h2_on_new_page:
            export_payload["start_h2_on_new_page"] = True
        if epub_cover:
            export_payload["epub_cover"] = epub_cover
        job_id = _enqueue_export_job("export_document", export_payload)
        if not job_id:
            return jsonify(
                {"error": "Export is temporarily unavailable. Please try again."}
            ), 503
        return jsonify(
            {
                "success": True,
                "job_id": job_id,
                "status_url": _job_status_url(job_id),
                "download_url": _job_download_url(job_id),
            }
        ), 202

    return jsonify({"error": "Invalid format requested."}), 400


@blueprint.route("/api/documents/<document_id>/epub/edition", methods=["GET", "PUT", "DELETE"])
@login_required
def document_epub_edition(document_id):
    from .epub_editions import EpubEditionStoreError, clear_epub_edition, get_epub_edition, save_epub_edition

    if request.args or len(request.get_data(cache=True)) > 200_000:
        return jsonify({"error": "Invalid edition request."}), 400
    try:
        scope = {"owner_id": int(current_user.id), "document_id": document_id}
        if request.method == "GET":
            response = jsonify(get_epub_edition(**scope))
            response.headers["Cache-Control"] = "private, no-store"
            return response
        body = request.get_json(silent=True)
        keys = {"expected_version"} if request.method == "DELETE" else {"recipe", "expected_version", "expected_source_revision"}
        if not isinstance(body, dict) or set(body) != keys:
            return jsonify({"error": "Supply the edition choices and revision witnesses."}), 400
        action = clear_epub_edition if request.method == "DELETE" else save_epub_edition
        response = jsonify(action(**scope, **body))
        response.headers["Cache-Control"] = "private, no-store"
        return response
    except EpubEditionStoreError as exc:
        status = 404 if exc.code == "epub-edition-not-found" else 400 if exc.code == "epub-edition-contract" else 409
        return jsonify({"error": "Edition choices could not be read or saved. Refresh and review the current source.", "code": exc.code}), status
    except ValueError:
        return jsonify({"error": "Invalid edition choices."}), 400


@blueprint.route("/api/documents/<document_id>/epub/preflight", methods=["POST"])
@login_required
def preflight_document_epub(document_id):
    """Return revision-bound EPUB diagnostics without building an artifact."""

    document = _get_user_document(document_id)
    if request.args:
        return jsonify({"error": "Invalid EPUB preflight request."}), 400
    body = request.get_json(silent=True)
    allowed = {
        "epub_options",
        "expected_revision",
        "epub_cover",
        "epub_image_mode",
    }
    if not isinstance(body, dict) or not set(body) <= allowed:
        return jsonify({"error": "Invalid EPUB preflight request."}), 400
    if (
        document.status != "completed"
        or not isinstance(document.output_html, str)
        or not document.output_html.strip()
    ):
        return jsonify({"error": "The document is not ready for EPUB preflight."}), 409

    from .mcp_review import _revision

    revision = _revision(document.output_html)
    expected_revision = body.get("expected_revision")
    if expected_revision is not None:
        if (
            not isinstance(expected_revision, str)
            or len(expected_revision) != 64
            or any(char not in "0123456789abcdef" for char in expected_revision)
        ):
            return jsonify({"error": "Invalid document revision."}), 400
        if expected_revision != revision:
            return jsonify({"error": "The document changed before preflight."}), 409

    from carlina.core.epub_cover import normalize_epub_cover
    from carlina.core.epub_export import normalize_epub_options

    try:
        options = normalize_epub_options(body.get("epub_options", {}))
        cover_value = body.get("epub_cover", {})
        if cover_value is not None and not isinstance(cover_value, dict):
            raise ValueError("Invalid EPUB cover option.")
        epub_cover = normalize_epub_cover(cover_value)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    from carlina.core.epub_export import normalize_epub_image_mode

    try:
        epub_image_mode = normalize_epub_image_mode(body.get("epub_image_mode", "jpeg"))
    except ValueError:
        return jsonify({"error": "Invalid EPUB image mode."}), 400

    from .epub_packager import EPUBPackager
    from .epub_quality import direct_epub_review_sha256, preflight_epub
    from .epub_editions import current_source_revision

    packager = EPUBPackager(document, "", options=options, cover=epub_cover, image_mode=epub_image_mode)
    metadata = get_canonical_document_metadata(document)
    result = preflight_epub(packager._raw_content(), metadata, options, cover=epub_cover)
    quality = result.get("quality")
    if not isinstance(quality, dict):
        return jsonify({"error": "The EPUB preflight did not complete."}), 503
    quality["input_sha256"] = revision
    return jsonify(
        {
            "revision": revision,
            "edition_revision": current_source_revision(document),
            "review_hash": direct_epub_review_sha256(
                revision,
                metadata,
                options,
                cover=epub_cover,
                image_mode=epub_image_mode,
            ),
            **result,
        }
    )


def _get_document_output_folder(document: ProcessedDocument) -> Path:
    base_uploads_dir = uploads_root()
    managed_root = (
        base_uploads_dir / str(document.user_id) / "doc_processor_docs"
    ).resolve()
    try:
        absolute_path = resolve_upload_reference(
            document.file_path, uploads_dir=base_uploads_dir
        )
    except OSError, RuntimeError, TypeError, ValueError:
        return managed_root
    if absolute_path is None:
        return managed_root

    try:
        output_folder = absolute_path.parent.resolve()
        output_folder.relative_to(managed_root)
    except OSError, RuntimeError, ValueError:
        # A row must never direct writable/debug output into another user's
        # managed subtree.  Legacy absolute source paths remain readable, but
        # all generated artifacts fall back to this document owner's root.
        return managed_root
    return output_folder


@blueprint.route("/api/documents/<document_id>/export-debug", methods=["GET"])
@login_required
def export_debug_artifacts(document_id):
    """Packages and downloads the debug_pipeline directory as a ZIP file."""
    document = _get_user_document(document_id)
    output_folder = _get_document_output_folder(document)
    debug_dir = output_folder / "debug_pipeline"

    if not debug_dir.exists() or not debug_dir.is_dir():
        return jsonify(
            {
                "error": "Debug pipeline artifacts not found for this document. Make sure 'store_debug_artifacts' was enabled during processing."
            }
        ), 404

    import io
    import zipfile

    memory_file = io.BytesIO()
    with zipfile.ZipFile(memory_file, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(debug_dir):
            for file in files:
                file_path = Path(root) / file
                arcname = file_path.relative_to(output_folder)
                zf.write(file_path, arcname)

    memory_file.seek(0)

    safe_name = secure_filename(document.filename.rsplit(".", 1)[0])
    if not safe_name:
        safe_name = "document"

    return Response(
        memory_file.getvalue(),
        mimetype="application/zip",
        headers={
            "Content-disposition": f'attachment; filename="{safe_name}_debug_pipeline.zip"'
        },
    )


@blueprint.route("/api/convert", methods=["POST"])
@login_required
def convert_document():
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded."}), 400

    file = request.files["file"]
    safe_original_name, validation_error = _validate_upload(file)
    if validation_error:
        return jsonify({"error": validation_error}), 400
    if _request_exceeds_upload_limit():
        return jsonify({"error": "File exceeds the configured upload limit."}), 413

    engine_id = request.form.get("engine", "auto")
    kwargs = {}
    for key, value in request.form.items():
        if key in {"file", "engine"}:
            continue
        kwargs[key] = _parse_value(value)

    if "page_range" in request.form:
        kwargs["page_range"] = request.form.get("page_range")

    if "paddle_batch_size" in kwargs and "batch_size" not in kwargs:
        kwargs["batch_size"] = kwargs.pop("paddle_batch_size")

    user_dir = uploads_root() / str(current_user.id) / "temp_docs"
    user_dir.mkdir(parents=True, exist_ok=True)

    safe_filename = f"{uuid.uuid4().hex}_{safe_original_name}"
    file_path = user_dir / safe_filename
    file.save(file_path)
    if file_path.stat().st_size > _max_upload_bytes():
        file_path.unlink(missing_ok=True)
        return jsonify({"error": "File exceeds the configured upload limit."}), 413

    try:
        job_id = _enqueue_job(
            "convert_document",
            {
                "file_path": str(file_path),
                "filename": file.filename,
                "user_id": current_user.id,
                "engine_id": engine_id,
                "options": kwargs,
            },
            num_workers=1,
        )
        return jsonify(
            {
                "success": True,
                "job_id": job_id,
                "status_url": _job_status_url(job_id),
            }
        ), 202
    except Exception:
        logging.exception("DocProcessor UI conversion failed")
        file_path.unlink(missing_ok=True)
        return jsonify(
            {"error": "Processing is temporarily unavailable. Please try again."}
        ), 503


def _embed_local_images_in_html(
    soup: BeautifulSoup, document: ProcessedDocument
) -> None:
    source_path = _resolve_local_path(document.file_path)
    for img in soup.find_all("img"):
        src = img.get("src")
        if not src or str(src).lower().startswith("data:"):
            continue

        local_img_path = _safe_document_asset_path(str(src), source_path)
        if local_img_path is None:
            del img["src"]
            continue
        try:
            data = local_img_path.read_bytes()
            mime_type = EXPORT_IMAGE_MIME_TYPES[local_img_path.suffix.lower()]
            base64_data = base64.b64encode(data).decode("utf-8")
            img["src"] = f"data:{mime_type};base64,{base64_data}"
        except OSError:
            current_app.logger.warning(
                "Unable to embed export image '%s'.",
                local_img_path,
                exc_info=True,
            )


@blueprint.route("/api/documents/<document_id>/control", methods=["POST"])
@login_required
def control_document_processing(document_id):
    document = _get_user_document(document_id)
    payload = request.get_json(silent=True)
    action = payload.get("action") if isinstance(payload, dict) else None
    if action not in ["pause", "stop"]:
        return jsonify({"error": "Invalid action."}), 400

    queue = _get_task_queue()
    job_id = document.settings.get("active_job_id")

    if action == "pause":
        execution_id = (document.settings or {}).get("mcp_execution_id")
        if execution_id:
            from ...models import McpExecution
            from .execution_recovery import suspend_execution

            execution = db.session.get(McpExecution, execution_id)
            recovery = (execution.request_json or {}).get("recovery", {}) if execution else {}
            if execution is not None and recovery.get("schema_version") == 1:
                if document.status == "suspended" and execution.status == "suspended":
                    return jsonify({"success": True, "action": "pause", "idempotent": True})
                if not suspend_execution(document.id, execution_id, str(job_id or ""), {"error_code": "user_paused"}):
                    return jsonify({"error": "The active extraction changed. Refresh its status."}), 409
                # The durable suspension fences writes even if the queue signal
                # cannot be delivered to the old worker.
                try:
                    queue.set_status(job_id, {"status": "pausing", "pause_requested": True})
                except Exception:
                    current_app.logger.warning("Could not signal the suspended extraction worker.")
                return jsonify({"success": True, "action": "pause"})
        if job_id:
            queue.set_status(job_id, {"status": "pausing", "pause_requested": True})
        if not document.settings:
            document.settings = {}
        document.settings["progress_msg"] = "Pausing processing..."
        document.status = "pausing"
        flag_modified(document, "settings")
        db.session.commit()
        return jsonify({"success": True, "action": "pause"})

    elif action == "stop":
        if job_id:
            queue.request_cancel(
                job_id, requested_by=current_user.id, reason="User requested stop"
            )
        if not document.settings:
            document.settings = {}
        document.settings["progress_msg"] = "Stopping..."
        document.status = "cancelled"
        flag_modified(document, "settings")
        db.session.commit()
        return jsonify({"success": True, "action": "stop"})


@blueprint.route("/api/projects/<project_id>/documents/upload", methods=["POST"])
@login_required
def upload_project_documents(project_id):
    from copy import deepcopy

    project = DocumentProject.query.filter_by(
        id=project_id, user_id=current_user.id
    ).first_or_404()
    files = request.files.getlist("files")
    if not files:
        return jsonify({"error": "No files uploaded."}), 400
    if len(files) > MAX_PROJECT_UPLOAD_FILES:
        return jsonify(
            {"error": f"Upload at most {MAX_PROJECT_UPLOAD_FILES} documents at a time."}
        ), 400
    validated_files = []
    aggregate_bytes = 0
    for file in files:
        safe_name, upload_error = _validate_upload(file)
        if upload_error:
            return jsonify({"error": upload_error}), 400
        file_size = _uploaded_file_size(file)
        if file_size is None:
            return jsonify(
                {"error": "Unable to determine an uploaded document's size safely."}
            ), 413
        if file_size > _max_upload_bytes():
            return jsonify(
                {"error": f"'{file.filename}' exceeds the configured upload limit."}
            ), 413
        aggregate_bytes += file_size
        if aggregate_bytes > _max_project_upload_bytes():
            return jsonify(
                {
                    "error": "Project upload exceeds the configured aggregate upload limit."
                }
            ), 413
        validated_files.append((file, safe_name))
    if _request_exceeds_upload_limit(len(validated_files)):
        return jsonify(
            {"error": "One or more documents exceed the configured upload limit."}
        ), 413

    from iarf_assistant.core.model_router import ModelRouter

    default_model = ModelRouter.get_model_for_tier("medium", current_user.id)

    requested_engine = str(request.form.get("engine", "auto") or "auto").strip().lower()
    raw_pdf_options = {
        "removehf": _parse_value(request.form.get("removehf", False)),
        "evaluate": _parse_value(request.form.get("evaluate_pdf", False)),
        "append_previous": _parse_value(request.form.get("append_previous", True)),
        "ignore_footnotes": _parse_value(request.form.get("ignore_footnotes", False)),
        "simple_footnotes": _parse_value(request.form.get("simple_footnotes", False)),
        "llm_boundary_merge": _parse_value(
            request.form.get("llm_boundary_merge", False)
        ),
        "correct_images": _parse_value(request.form.get("correct_images", True)),
        "extract_images": _parse_value(request.form.get("extract_images", True)),
        "parallel_pages": request.form.get("parallel_pages", 1),
        "page_range": request.form.get("page_range", ""),
        "excluded_pages": request.form.get("excluded_pages", ""),
        "metadata_prepass": _parse_value(request.form.get("metadata_prepass", True)),
        "metadata_pages": request.form.get("metadata_pages", 20),
        "metadata_end_pages": request.form.get("metadata_end_pages", 0),
        "metadata_delete_flagged_pages": _parse_value(
            request.form.get(
                "metadata_delete_flagged_pages",
                request.form.get("metadata_exclude_flagged_pages", False),
            )
        ),
        "toc_handling": request.form.get("toc_handling", "omit"),
        "prompt_cache": _parse_value(request.form.get("prompt_cache", True)),
        "skip_cropping": _parse_value(request.form.get("skip_cropping", False)),
        "store_debug_artifacts": _parse_value(
            request.form.get("store_debug_artifacts", False)
        ),
    }
    raw_settings = {
        "engine": requested_engine,
        "processing_profile": request.form.get(
            "processing_profile",
            request.form.get("profile_id", request.form.get("profile")),
        ),
        "parsing_model": request.form.get("parsing_model") or default_model,
        "metadata_model": request.form.get("metadata_model") or default_model,
        "pdf_options": raw_pdf_options,
    }
    for key in (
        "force_ocr",
        "paginate_output",
        "use_doc_unwarping",
        "use_doc_orientation_classify",
        "layout_shape_mode",
        "merge_tables",
        "relevel_titles",
        "concatenate_pages",
        "paddle_remove_hf",
        "paddle_preserve_footnotes",
        "paddle_chart_rec",
        "paddle_seal_rec",
        "paddle_batch_size",
        "use_ocr",
        "exclude_footers",
        "embed_images",
    ):
        if key in request.form:
            raw_settings[key] = _parse_value(request.form.get(key))

    # Resolve every file and validate every effective model before the first
    # directory, file, ORM row, or queue action.  A project upload is a batch
    # boundary, so one bad PDF must leave the entire batch untouched.
    effective_settings_by_file = []
    for _file, safe_filename in validated_files:
        try:
            effective_engine = _resolve_effective_engine(
                safe_filename,
                requested_engine,
                raw_settings,
            )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        try:
            settings = _build_document_settings(
                raw_settings,
                default_model=default_model,
                effective_engine=effective_engine,
                requested_engine=requested_engine,
                user=current_user,
            )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        # ``_build_document_settings`` already snapshots visible model
        # reasoning.  Keep this normalization pass side-effect free for the
        # batch boundary; project-run creation below is where model overrides
        # may deliberately recompute a snapshot.
        settings = _shared_build_effective_settings(settings)
        if (
            settings.get("processing_profile") != "book_quality"
            and "append_previous" in raw_pdf_options
        ):
            # Legacy project uploads serialize pages but preserve an explicit
            # previous-page choice, matching the pre-profile API contract.
            settings["pdf_options"]["append_previous"] = raw_pdf_options[
                "append_previous"
            ]
        engine_options = _build_engine_options(effective_engine, raw_settings)
        if engine_options:
            settings["engine_options"] = engine_options
        effective_settings_by_file.append((safe_filename, settings))

    model_error = validate_pdf_model_readiness_batch(
        [
            (safe_filename, settings)
            for safe_filename, settings in effective_settings_by_file
        ],
        user_id=current_user.id,
    )
    if model_error:
        return jsonify({"error": model_error}), 400

    start_pos = len(project.documents)
    created_dirs = []
    try:
        for index, (file, safe_filename) in enumerate(validated_files):
            _resolved_filename, settings = effective_settings_by_file[index]
            doc_uuid = uuid.uuid4().hex
            user_dir = (
                uploads_root() / str(current_user.id) / "doc_processor_docs" / doc_uuid
            )
            user_dir.mkdir(parents=True, exist_ok=True)
            created_dirs.append(user_dir)
            file_path = user_dir / safe_filename
            file.save(file_path)
            if file_path.stat().st_size > _max_upload_bytes():
                db.session.rollback()
                for created_dir in created_dirs:
                    shutil.rmtree(created_dir, ignore_errors=True)
                return jsonify(
                    {"error": f"'{file.filename}' exceeds the configured upload limit."}
                ), 413

            web_path = f"/uploads/{current_user.id}/doc_processor_docs/{doc_uuid}/{safe_filename}"
            document = ProcessedDocument(
                user_id=current_user.id,
                filename=file.filename,
                file_path=web_path,
                status="created",
                settings=deepcopy(settings),
            )
            db.session.add(document)
            db.session.flush()
            initialize_document_metadata(document, file_path)
            if document.metadata_record is not None:
                db.session.add(document.metadata_record)

            project_doc = ProjectDocument(
                project_id=project.id,
                document_id=document.id,
                position=start_pos + index,
            )
            db.session.add(project_doc)

        db.session.commit()
    except Exception:
        db.session.rollback()
        for created_dir in created_dirs:
            shutil.rmtree(created_dir, ignore_errors=True)
        current_app.logger.exception("Failed to save project document upload.")
        return jsonify({"error": "Unable to save the uploaded documents."}), 500
    return jsonify({"project": project.to_dict()}), 201


@blueprint.route("/api/projects/<project_id>/runs", methods=["POST"])
@login_required
def start_project_run(project_id):
    from copy import deepcopy

    from iarf_assistant.addons.doc_processor.project_service import (
        build_effective_settings,
        schedule_project_run,
    )

    project = DocumentProject.query.filter_by(
        id=project_id, user_id=current_user.id
    ).first_or_404()
    if not project.documents:
        return jsonify(
            {"error": "Add at least one document before starting a project run."}
        ), 400

    project_document_ids = [entry.document_id for entry in project.documents]
    conflicting_entries = (
        ProjectRunDocument.query.join(ProjectRun)
        .filter(
            ProjectRun.user_id == current_user.id,
            ~ProjectRun.status.in_({"completed", "completed_with_errors", "cancelled"}),
            ProjectRunDocument.document_id.in_(project_document_ids),
            ~ProjectRunDocument.status.in_({"completed", "error", "cancelled"}),
        )
        .all()
    )
    if conflicting_entries:
        conflicting_ids = {entry.document_id for entry in conflicting_entries}
        return jsonify(
            {
                "error": "One or more documents are already part of an active project run.",
                "conflicting_documents": [
                    {
                        "id": entry.document_id,
                        "filename": entry.document.filename
                        if entry.document
                        else entry.document_id,
                    }
                    for entry in project.documents
                    if entry.document_id in conflicting_ids
                ],
            }
        ), 409

    missing_docs = []
    for entry in project.documents:
        document = entry.document
        path = _resolve_local_path(document.file_path)
        if not path or not path.is_file():
            missing_docs.append({"id": document.id, "filename": document.filename})
    if missing_docs:
        return jsonify(
            {"error": "Missing source files", "missing_documents": missing_docs}
        ), 409

    project_settings = project.settings if isinstance(project.settings, dict) else {}
    concurrency_limit = _normalize_project_concurrency(
        project_settings.get("document_concurrency", 2)
    )
    run = ProjectRun(
        project_id=project.id,
        user_id=current_user.id,
        status="queued",
        concurrency_limit=concurrency_limit,
        settings_snapshot=deepcopy(project_settings),
    )
    db.session.add(run)
    db.session.flush()

    for position, proj_doc in enumerate(project.documents):
        document = proj_doc.document
        effective_settings = build_effective_settings(
            document.settings,
            proj_doc.settings_overrides,
            user=current_user,
        )
        initial_status = (
            "completed"
            if document.status in {"completed", "completed_with_errors"}
            else "queued"
        )
        run_doc = ProjectRunDocument(
            run_id=run.id,
            document_id=document.id,
            position=position,
            status=initial_status,
            effective_settings=effective_settings,
        )
        if initial_status == "completed":
            run_doc.completed_at = utc_now_db()
        db.session.add(run_doc)

    db.session.commit()

    queue = _get_task_queue()
    _ensure_workers(run.concurrency_limit)
    schedule_project_run(run.id, queue)
    db.session.commit()

    return jsonify(run.to_dict()), 201


@blueprint.route("/api/project-runs/<run_id>/control", methods=["POST"])
@login_required
def control_project_run(run_id):
    from iarf_assistant.addons.doc_processor.project_service import (
        refresh_project_run,
        schedule_project_run,
    )

    run = ProjectRun.query.filter_by(id=run_id, user_id=current_user.id).first_or_404()
    data = request.get_json(silent=True) or {}
    action = data.get("action")
    if action not in {"pause", "resume", "stop"}:
        return jsonify({"error": "Invalid project run action."}), 400

    queue = _get_task_queue()
    now = utc_now_db()

    if action == "pause":
        run.pause_requested = True
        if run.status not in {"completed", "completed_with_errors", "cancelled"}:
            run.status = "pausing"
        for entry in run.documents:
            if entry.job_id and entry.status in {
                "scheduling",
                "preparing_crop_review",
                "processing",
            }:
                queue.set_status(
                    entry.job_id, {"status": "pausing", "pause_requested": True}
                )
        db.session.commit()
        refreshed = refresh_project_run(run.id)
        return jsonify(refreshed.to_dict() if refreshed else run.to_dict())

    if action == "resume":
        missing_sources = _missing_project_run_sources(run)
        if missing_sources:
            return jsonify(
                {
                    "error": "Source file missing.",
                    "missing_documents": missing_sources,
                }
            ), 409
        run.pause_requested = False
        run.stop_requested = False
        for entry in run.documents:
            if entry.status in {"cancelled", "paused"} and not entry.completed_at:
                entry.status = "queued"
        db.session.commit()
        refreshed = schedule_project_run(run.id, queue)
        return jsonify(refreshed.to_dict() if refreshed else run.to_dict())

    run.stop_requested = True
    run.status = "stopping"
    for entry in run.documents:
        if entry.job_id and entry.status in {
            "scheduling",
            "preparing_crop_review",
            "processing",
        }:
            queue.request_cancel(
                entry.job_id,
                requested_by=current_user.id,
                reason="User requested project stop",
            )
        if entry.status in {"queued", "needs_cropping", "reviewing_images"}:
            entry.status = "cancelled"
            entry.completed_at = entry.completed_at or now
            if entry.document:
                entry.document.status = "cancelled"
    db.session.commit()
    refreshed = refresh_project_run(run.id)
    return jsonify(refreshed.to_dict() if refreshed else run.to_dict())


@blueprint.route("/api/projects", methods=["GET"])
@login_required
def list_projects():
    projects = (
        DocumentProject.query.filter_by(user_id=current_user.id)
        .options(
            selectinload(DocumentProject.documents)
            .selectinload(ProjectDocument.document)
            .selectinload(ProcessedDocument.metadata_record),
            selectinload(DocumentProject.runs)
            .selectinload(ProjectRun.documents)
            .selectinload(ProjectRunDocument.document)
            .selectinload(ProcessedDocument.metadata_record),
        )
        .order_by(DocumentProject.created_at.desc())
        .all()
    )
    return jsonify([project.to_dict() for project in projects])


@blueprint.route("/api/projects", methods=["POST"])
@login_required
def create_project():
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({"error": "Project payload must be an object."}), 400
    name = str(data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Project name is required."}), 400
    if len(name) > 255:
        return jsonify({"error": "Project name must be 255 characters or fewer."}), 400

    try:
        concurrency = int(data.get("document_concurrency", 2))
    except TypeError, ValueError:
        return jsonify({"error": "Project concurrency must be an integer."}), 400
    if not 1 <= concurrency <= MAX_PROJECT_CONCURRENCY:
        return jsonify(
            {
                "error": f"Project concurrency must be between 1 and {MAX_PROJECT_CONCURRENCY}."
            }
        ), 400
    project = DocumentProject(
        user_id=current_user.id,
        name=name,
        settings={"document_concurrency": concurrency},
    )
    db.session.add(project)
    db.session.commit()
    return jsonify(project.to_dict()), 201


@blueprint.route("/api/projects/<project_id>", methods=["DELETE"])
@login_required
def delete_project(project_id):
    project = DocumentProject.query.filter_by(
        id=project_id, user_id=current_user.id
    ).first_or_404()
    has_active_run = ProjectRun.query.filter(
        ProjectRun.project_id == project.id,
        ~ProjectRun.status.in_({"completed", "completed_with_errors", "cancelled"}),
    ).first()
    if has_active_run:
        return jsonify(
            {"error": "Stop the active project run before deleting this project."}
        ), 409
    db.session.delete(project)
    db.session.commit()
    return jsonify({"success": True})


@blueprint.route("/api/projects/<project_id>", methods=["PUT"])
@login_required
def update_project(project_id):
    project = DocumentProject.query.filter_by(
        id=project_id, user_id=current_user.id
    ).first_or_404()
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({"error": "Project payload must be an object."}), 400

    if "document_concurrency" in data:
        try:
            concurrency = int(data["document_concurrency"])
        except TypeError, ValueError:
            return jsonify({"error": "Project concurrency must be an integer."}), 400
        if not 1 <= concurrency <= MAX_PROJECT_CONCURRENCY:
            return jsonify(
                {
                    "error": f"Project concurrency must be between 1 and {MAX_PROJECT_CONCURRENCY}."
                }
            ), 400
        if not project.settings:
            project.settings = {}
        project.settings["document_concurrency"] = concurrency
        flag_modified(project, "settings")

    db.session.commit()
    return jsonify({"success": True})


@blueprint.route("/api/projects/<project_id>/documents", methods=["POST"])
@login_required
def add_project_documents(project_id):
    project = DocumentProject.query.filter_by(
        id=project_id, user_id=current_user.id
    ).first_or_404()
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({"error": "Project document payload must be an object."}), 400
    document_ids = data.get("document_ids")
    if (
        not isinstance(document_ids, list)
        or not document_ids
        or any(
            not isinstance(document_id, str) or not document_id
            for document_id in document_ids
        )
        or len(document_ids) != len(set(document_ids))
    ):
        return jsonify(
            {"error": "document_ids must be a non-empty list of unique document IDs."}
        ), 400

    existing_ids = {entry.document_id for entry in project.documents}
    new_ids = [
        document_id for document_id in document_ids if document_id not in existing_ids
    ]
    if len(existing_ids) + len(new_ids) > MAX_PROJECT_UPLOAD_FILES:
        return jsonify(
            {
                "error": f"A project may contain at most {MAX_PROJECT_UPLOAD_FILES} documents."
            }
        ), 400
    documents = (
        ProcessedDocument.query.filter(
            ProcessedDocument.user_id == current_user.id,
            ProcessedDocument.id.in_(new_ids),
        ).all()
        if new_ids
        else []
    )
    documents_by_id = {document.id: document for document in documents}
    missing_ids = [
        document_id for document_id in new_ids if document_id not in documents_by_id
    ]
    if missing_ids:
        return jsonify(
            {
                "error": "One or more documents were not found.",
                "missing_document_ids": missing_ids,
            }
        ), 404

    start_pos = len(project.documents)
    for added_count, doc_id in enumerate(new_ids):
        project_doc = ProjectDocument(
            project_id=project.id, document_id=doc_id, position=start_pos + added_count
        )
        db.session.add(project_doc)

    if new_ids:
        db.session.commit()
        db.session.refresh(project)

    return jsonify({"project": project.to_dict()})


@blueprint.route(
    "/api/projects/<project_id>/documents/<document_id>", methods=["DELETE"]
)
@login_required
def remove_project_document(project_id, document_id):
    project = DocumentProject.query.filter_by(
        id=project_id, user_id=current_user.id
    ).first_or_404()
    proj_doc = ProjectDocument.query.filter_by(
        project_id=project.id, document_id=document_id
    ).first_or_404()

    db.session.delete(proj_doc)
    db.session.commit()

    # Re-normalize position order on remaining documents
    remaining = (
        ProjectDocument.query.filter_by(project_id=project.id)
        .order_by(ProjectDocument.position.asc())
        .all()
    )
    for index, entry in enumerate(remaining):
        entry.position = index

    db.session.commit()
    db.session.refresh(project)
    return jsonify({"project": project.to_dict()})


@blueprint.route("/api/projects/<project_id>/documents/order", methods=["PUT"])
@login_required
def reorder_project_documents(project_id):
    project = DocumentProject.query.filter_by(
        id=project_id, user_id=current_user.id
    ).first_or_404()
    data = request.get_json(silent=True) or {}
    document_ids = data.get("document_ids")
    current_ids = [entry.document_id for entry in project.documents]
    if (
        not isinstance(document_ids, list)
        or any(not isinstance(document_id, str) for document_id in document_ids)
        or len(document_ids) != len(set(document_ids))
        or set(document_ids) != set(current_ids)
    ):
        return jsonify(
            {"error": "document_ids must contain every project document exactly once."}
        ), 400

    pos_map = {doc_id: pos for pos, doc_id in enumerate(document_ids)}
    for entry in project.documents:
        entry.position = pos_map[entry.document_id]

    db.session.commit()
    db.session.refresh(project)
    return jsonify({"project": project.to_dict()})


@blueprint.route("/api/projects/<project_id>/export", methods=["GET"])
@login_required
def export_project(project_id):
    """Exports a project's completed documents, either concatenated or as a ZIP archive."""
    import io
    import re
    import zipfile

    from bs4 import BeautifulSoup

    from iarf_assistant.addons.doc_processor.format_utils import (
        html_to_markdown_on_the_fly,
    )
    from iarf_assistant.addons.doc_processor.routes import (
        _build_standalone_html,
        _embed_local_images_in_html,
        _sanitize_document_html,
    )

    project = DocumentProject.query.filter_by(
        id=project_id, user_id=current_user.id
    ).first_or_404()

    export_format = request.args.get("format", "md").lower()
    export_mode = request.args.get("mode", "concatenated").lower()
    epub_image_mode = (
        _requested_epub_image_mode() if export_format == "epub" else "auto"
    )
    include_source_toc = False

    if export_format not in ["md", "html", "epub", "docx", "pdf"]:
        return jsonify({"error": "Invalid format requested."}), 400
    heading_value = request.args.get("start_h2_on_new_page")
    if heading_value is not None and heading_value not in {"true", "false", "1", "0"}:
        return jsonify({"error": "Invalid heading pagination option."}), 400
    start_h2_on_new_page = heading_value in {"true", "1"}
    if start_h2_on_new_page and export_format not in {"docx", "pdf"}:
        return jsonify({"error": "Heading pagination requires DOCX or PDF."}), 400

    if epub_image_mode is None:
        return jsonify({"error": "Invalid EPUB image mode requested."}), 400

    if export_mode not in ["concatenated", "separate"]:
        return jsonify({"error": "Invalid mode requested."}), 400

    if not project.documents:
        return jsonify({"error": "This project has no documents."}), 400

    completed_entries = [
        entry
        for entry in project.documents
        if entry.document
        and entry.document.status in ["completed", "completed_with_errors"]
    ]
    if not completed_entries:
        return jsonify(
            {"error": "No completed documents available to export in this project."}
        ), 400

    safe_project_name = secure_filename(project.name) or "project_export"

    if export_mode == "separate":
        if export_format in ["epub", "docx", "pdf"]:
            # ZIP of EPUB/DOCX files - enqueued as a background job since it takes time to generate them all
            export_payload = {
                "project_id": project.id,
                "user_id": current_user.id,
                "format": export_format,
                "mode": "separate",
            }
            if export_format == "epub":
                export_payload.update(
                    {
                        "epub_image_mode": epub_image_mode,
                        "include_source_toc": include_source_toc,
                    }
                )
            if start_h2_on_new_page:
                export_payload["start_h2_on_new_page"] = True
            job_id = _enqueue_export_job(
                "export_project",
                export_payload,
            )
            if not job_id:
                return jsonify(
                    {"error": "Export is temporarily unavailable. Please try again."}
                ), 503
            return jsonify(
                {
                    "success": True,
                    "job_id": job_id,
                    "status_url": _job_status_url(job_id),
                    "download_url": _job_download_url(job_id),
                }
            ), 202

        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
            seen_filenames = set()
            for entry in completed_entries:
                doc = entry.document
                if export_format == "md":
                    content = doc.output_markdown or (
                        html_to_markdown_on_the_fly(doc.output_html)
                        if doc.output_html
                        else ""
                    )
                    ext = "md"
                else:  # html
                    raw_content = doc.output_html or ""
                    sanitized_content = _sanitize_document_html(raw_content)
                    soup = BeautifulSoup(sanitized_content, "html.parser")
                    _embed_local_images_in_html(soup, doc)

                    document_metadata = get_canonical_document_metadata(doc)
                    document_title = get_document_display_title(doc, document_metadata)
                    content = _build_standalone_html(
                        str(soup),
                        document_title,
                        document_metadata.get("language"),
                        styles=_PORTABLE_EXPORT_STYLES,
                    )
                    ext = "html"
                    from carlina.core.diagram_rendering import project_diagrams_html

                    content = project_diagrams_html(content)

                base_name = doc.filename.rsplit(".", 1)[0]
                filename = f"{base_name}.{ext}"
                counter = 1
                while filename in seen_filenames:
                    filename = f"{base_name}_{counter}.{ext}"
                    counter += 1
                seen_filenames.add(filename)

                zip_file.writestr(filename, content)

        zip_buffer.seek(0)
        return Response(
            zip_buffer.getvalue(),
            mimetype="application/zip",
            headers={
                "Content-disposition": f'attachment; filename="{safe_project_name}.zip"'
            },
        )

    if export_mode == "concatenated":
        if export_format == "md":
            concatenated_md = []
            for entry in completed_entries:
                doc = entry.document
                content = doc.output_markdown or (
                    html_to_markdown_on_the_fly(doc.output_html)
                    if doc.output_html
                    else ""
                )
                if not content:
                    continue

                document_metadata = get_canonical_document_metadata(doc)
                doc_title = get_document_display_title(doc, document_metadata)

                # Demote headings by 1
                lines = content.splitlines()
                demoted_lines = []
                for line in lines:
                    m = re.match(r"^(#{1,5})\s+(.*)$", line)
                    if m:
                        hashes, text = m.groups()
                        demoted_lines.append(f"{'#' * (len(hashes) + 1)} {text}")
                    elif re.match(r"^######\s+(.*)$", line):
                        demoted_lines.append(line)
                    else:
                        demoted_lines.append(line)
                demoted_content = "\n".join(demoted_lines)

                concatenated_md.append(f"# {doc_title}\n\n{demoted_content}")

            final_content = "\n\n---\n\n".join(concatenated_md)
            return Response(
                final_content,
                mimetype="text/markdown",
                headers={
                    "Content-disposition": f'attachment; filename="{safe_project_name}.md"'
                },
            )

        elif export_format == "html":
            combined_body_html = []
            for document_index, entry in enumerate(completed_entries, start=1):
                doc = entry.document
                raw_html = doc.output_html or ""
                if not raw_html:
                    continue

                sanitized_html = _sanitize_document_html(raw_html)
                soup = BeautifulSoup(sanitized_html, "html.parser")
                namespace_semantic_fragment(
                    soup, namespace=f"project-doc-{document_index}",
                )

                for level in range(5, 0, -1):
                    old_tag = f"h{level}"
                    new_tag = f"h{level + 1}"
                    for elem in soup.find_all(old_tag):
                        elem.name = new_tag

                _embed_local_images_in_html(soup, doc)

                document_metadata = get_canonical_document_metadata(doc)
                doc_title = get_document_display_title(doc, document_metadata)

                section_html = (
                    f'<section class="project-document" data-filename="{html_lib.escape(doc.filename)}">\n'
                    f"  <h1>{html_lib.escape(doc_title)}</h1>\n"
                    f"  {str(soup)}\n"
                    f"</section>"
                )
                combined_body_html.append(section_html)

            body_content = '\n<hr class="project-separator" />\n'.join(
                combined_body_html
            )

            final_html = _build_standalone_html(
                body_content,
                project.name,
                "en",
                styles=_PORTABLE_EXPORT_STYLES,
            )
            from carlina.core.diagram_rendering import project_diagrams_html

            return Response(
                project_diagrams_html(final_html),
                mimetype="text/html",
                headers={
                    "Content-disposition": f'attachment; filename="{safe_project_name}.html"'
                },
            )

        elif export_format in ["epub", "docx", "pdf"]:
            # Concatenated EPUB/DOCX - enqueued as a background job
            export_payload = {
                "project_id": project.id,
                "user_id": current_user.id,
                "format": export_format,
                "mode": "concatenated",
            }
            if export_format == "epub":
                export_payload.update(
                    {
                        "epub_image_mode": epub_image_mode,
                        "include_source_toc": include_source_toc,
                    }
                )
            if start_h2_on_new_page:
                export_payload["start_h2_on_new_page"] = True
            job_id = _enqueue_export_job(
                "export_project",
                export_payload,
            )
            if not job_id:
                return jsonify(
                    {"error": "Export is temporarily unavailable. Please try again."}
                ), 503
            return jsonify(
                {
                    "success": True,
                    "job_id": job_id,
                    "status_url": _job_status_url(job_id),
                    "download_url": _job_download_url(job_id),
                }
            ), 202

    return jsonify({"error": "Invalid combination of format and mode."}), 400
