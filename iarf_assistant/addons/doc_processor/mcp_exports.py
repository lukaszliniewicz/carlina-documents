"""Owner-scoped admission and bounded reads for document exports."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from flask import current_app

from carlina.core.epub_export import (
    EpubExportContractError,
    epub_value_sha256,
    normalize_epub_image_mode,
    normalize_epub_options,
    normalize_epub_quality,
)
from carlina.core.windows_artifacts import (
    WindowsArtifactDirectory,
    WindowsArtifactError,
)

from ...core.safe_sqlite_store import SafeSQLiteStoreError
from .mcp_review import _revision
from .mcp_service import DocProcessorMcpServiceError
from .models import ProcessedDocument

EXPORT_CONTRACT = "document-mcp-export-v1"
EXPORT_TASK = "mcp_export_document"
EXPORT_FORMATS = frozenset({"epub", "docx", "html", "pdf", "md"})
MAX_EXPORT_CHUNK_BYTES = 65_536
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_OPERATION_ID_RE = re.compile(r"[0-9a-f]{64}\Z")
_SAFE_STATE = frozenset(
    {
        "queued",
        "pending",
        "processing",
        "running",
        "completed",
        "failed",
        "cancelled",
        "cancel_requested",
        "cancelling",
    }
)
_MIMETYPES = {
    "epub": "application/epub+zip",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "html": "text/html",
    "pdf": "application/pdf",
    "md": "text/markdown",
}


def _error(code: str, message: str) -> DocProcessorMcpServiceError:
    return DocProcessorMcpServiceError(message, code=code)


def _request_identity(
    *,
    user_id: int,
    document_id: str,
    export_format: str,
    expected_revision: str,
    start_h2_on_new_page: bool,
    epub_cover: object = None,
    epub_options: object = None,
    epub_review_hash: str | None = None,
    epub_image_mode: str = "auto",
) -> dict[str, Any]:
    identity = {
        "contract": EXPORT_CONTRACT,
        "user_id": user_id,
        "document_id": document_id,
        "format": export_format,
        "expected_revision": expected_revision,
        "start_h2_on_new_page": start_h2_on_new_page,
    }
    if epub_cover:
        identity["epub_cover"] = epub_cover
    if epub_options:
        identity["epub_options"] = epub_options
    if epub_review_hash is not None:
        identity["epub_review_hash"] = epub_review_hash
    if epub_image_mode != "auto":
        identity["epub_image_mode"] = epub_image_mode
    return identity


def _canonical_hash(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        dict(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _operation_id(user_id: int, document_id: str, idempotency_key: str) -> str:
    return _canonical_hash(
        {
            "domain": EXPORT_CONTRACT,
            "user_id": user_id,
            "document_id": document_id,
            "idempotency_key": idempotency_key,
        }
    )


def _validate_request(
    document_id: object,
    export_format: object,
    idempotency_key: object,
    expected_revision: object,
    start_h2_on_new_page: object,
) -> tuple[str, str, bool]:
    if not isinstance(document_id, str) or not document_id.strip():
        raise _error(
            "invalid_export_request", "The document export request is invalid."
        )
    if not isinstance(export_format, str) or export_format not in EXPORT_FORMATS:
        raise _error(
            "invalid_export_request", "The document export request is invalid."
        )
    if (
        not isinstance(idempotency_key, str)
        or not 1 <= len(idempotency_key) <= 128
        or not idempotency_key.strip()
    ):
        raise _error(
            "invalid_export_request", "The document export request is invalid."
        )
    if (
        not isinstance(expected_revision, str)
        or _SHA256_RE.fullmatch(expected_revision) is None
    ):
        raise _error(
            "invalid_export_request", "The document export request is invalid."
        )
    if type(start_h2_on_new_page) is not bool or (
        start_h2_on_new_page and export_format not in {"docx", "pdf"}
    ):
        raise _error(
            "invalid_export_request", "The document export request is invalid."
        )
    return document_id, export_format, start_h2_on_new_page


def _queue(service: Any):
    context = getattr(service, "context", None)
    get_task_queue = getattr(context, "get_task_queue", None)
    if not callable(get_task_queue):
        raise _error("export_failed", "The document export did not complete.")
    try:
        queue = get_task_queue("doc_processor")
    except Exception:  # noqa: BLE001 - normalize arbitrary host queue failures.
        raise _error("export_failed", "The document export did not complete.") from None
    if not callable(getattr(queue, "get_status", None)):
        raise _error("export_failed", "The document export did not complete.")
    return queue


def _ensure_worker_pool(service: Any) -> None:
    context = getattr(service, "context", None)
    ensure_worker_pool = getattr(context, "ensure_worker_pool", None)
    if not callable(ensure_worker_pool):
        raise _error("export_failed", "The document export did not complete.")
    try:
        ensure_worker_pool("worker.py", num_workers=1)
    except Exception:  # noqa: BLE001 - normalize arbitrary worker-launch failures.
        raise _error("export_failed", "The document export did not complete.") from None


def _queue_status(queue: Any, operation_id: str) -> dict[str, Any] | None:
    try:
        status = queue.get_status(operation_id)
    except Exception:  # noqa: BLE001 - normalize arbitrary queue read failures.
        raise _error("export_failed", "The document export did not complete.") from None
    if not isinstance(status, dict) or status.get("status") in (None, "unknown"):
        return None
    return status


def _status_matches(
    status: Mapping[str, Any],
    *,
    user_id: int,
    document_id: str,
    export_format: str,
    revision: str,
    request_hash: str,
):
    return (
        status.get("contract") == EXPORT_CONTRACT
        and status.get("task_name") == EXPORT_TASK
        and type(status.get("user_id")) is int
        and status.get("user_id") == user_id
        and status.get("document_id") == document_id
        and status.get("format") == export_format
        and status.get("revision") == revision
        and status.get("request_hash") == request_hash
    )


def _state(status: Mapping[str, Any]) -> str:
    value = status.get("status")
    if not isinstance(value, str):
        return "processing"
    normalized = value.strip().lower()
    return normalized if normalized in _SAFE_STATE else "processing"


def _projection(
    *,
    document_id: str,
    operation_id: str,
    status: Mapping[str, Any],
    export_format: str,
    revision: str,
) -> dict[str, Any]:
    state = _state(status)
    payload: dict[str, Any] = {
        "contract": EXPORT_CONTRACT,
        "document_id": document_id,
        "operation_id": operation_id,
        "queue_job_id": operation_id,
        "status": state,
        "state": state,
        "ready": False,
        "format": export_format,
        "revision": revision,
    }
    if state in {"failed", "cancelled"}:
        payload["error_code"] = (
            "epub-review-stale"
            if status.get("error_code") == "epub-review-stale"
            else "export_failed"
        )
        payload["message"] = "The document export did not complete."
    if "epub_review_hash" in status:
        payload["epub_review_hash"] = status["epub_review_hash"]
    return payload


_EPUB_IMAGE_MODE_UNSET = object()


def export_document(
    service: Any,
    document_id: str,
    *,
    export_format: str = "epub",
    idempotency_key: str,
    expected_revision: str,
    start_h2_on_new_page: bool = False,
    epub_cover: object = None,
    epub_options: object = None,
    epub_review_hash: object = None,
    epub_image_mode: object = _EPUB_IMAGE_MODE_UNSET,
) -> dict[str, Any]:
    """Enqueue or replay one immutable owner-keyed export request."""

    document_id, export_format, start_h2_on_new_page = _validate_request(
        document_id,
        export_format,
        idempotency_key,
        expected_revision,
        start_h2_on_new_page,
    )
    from carlina.core.epub_cover import normalize_epub_cover

    try:
        cover_options = normalize_epub_cover(epub_cover)
        if cover_options and export_format != "epub":
            raise ValueError("format")
    except ValueError:
        raise _error(
            "invalid_export_request", "The EPUB cover option is invalid."
        ) from None
    requested_epub_options = None
    if epub_options is not None:
        try:
            requested_epub_options = normalize_epub_options(epub_options)
        except EpubExportContractError:
            raise _error(
                "invalid_export_request", "The EPUB publishing options are invalid."
            ) from None
        if export_format != "epub":
            raise _error(
                "invalid_export_request", "EPUB publishing options require EPUB."
            )
    if epub_review_hash is not None and (
        not isinstance(epub_review_hash, str)
        or _SHA256_RE.fullmatch(epub_review_hash) is None
        or export_format != "epub"
    ):
        raise _error(
            "invalid_export_request", "The EPUB review witness is invalid."
        )
    try:
        document = service._document(document_id, fresh=True)
    except DocProcessorMcpServiceError as exc:
        if exc.code == "not_found":
            raise _error(
                "invalid_export_request", "The document export request is invalid."
            ) from None
        raise

    operation_id = _operation_id(
        int(service.owner_user_id), document.id, idempotency_key
    )
    queue = _queue(service)

    existing = _queue_status(queue, operation_id)
    try:
        effective_image_mode = normalize_epub_image_mode(
            epub_image_mode
            if epub_image_mode is not _EPUB_IMAGE_MODE_UNSET
            else (
                existing.get("epub_image_mode", "auto")
                if existing is not None
                else "jpeg"
                if export_format == "epub"
                else "auto"
            )
        )
        if (
            epub_image_mode is not _EPUB_IMAGE_MODE_UNSET
            and effective_image_mode != "auto"
            and export_format != "epub"
        ):
            raise ValueError("format")
    except ValueError:
        raise _error(
            "invalid_export_request", "The EPUB image mode is invalid."
        ) from None
    effective_cover_options = cover_options
    if (
        existing is not None
        and export_format == "epub"
        and epub_cover is None
        and "epub_cover" in existing
    ):
        try:
            effective_cover_options = normalize_epub_cover(existing["epub_cover"])
        except ValueError:
            raise _error(
                "idempotency_conflict",
                "The retry key is already bound to a different export request.",
            ) from None
    if requested_epub_options is None:
        saved_options = (
            existing.get("epub_options")
            if existing is not None
            and existing.get("format") == export_format == "epub"
            else None
        )
        if saved_options is not None:
            try:
                effective_epub_options = normalize_epub_options(saved_options)
            except EpubExportContractError:
                raise _error(
                    "idempotency_conflict",
                    "The retry key is already bound to a different export request.",
                ) from None
        elif existing is not None:
            # A retained operation with no publishing options is historical.
            # Do not select the new profile default while replaying it.
            effective_epub_options = None
        elif export_format == "epub":
            try:
                effective_epub_options = normalize_epub_options({})
            except EpubExportContractError:
                raise _error(
                    "invalid_export_request", "The EPUB publishing options are invalid."
                ) from None
        else:
            effective_epub_options = None
    else:
        effective_epub_options = requested_epub_options

    saved_review_hash = None
    if existing is not None and existing.get("format") == export_format == "epub":
        saved_review_hash = existing.get("epub_review_hash")
        if saved_review_hash is not None and (
            not isinstance(saved_review_hash, str)
            or _SHA256_RE.fullmatch(saved_review_hash) is None
        ):
            raise _error(
                "idempotency_conflict",
                "The retry key is already bound to a different export request.",
            )
    effective_review_hash = (
        epub_review_hash if epub_review_hash is not None else saved_review_hash
    )
    if effective_review_hash is not None and (
        export_format != "epub" or effective_epub_options is None
    ):
        raise _error(
            "invalid_export_request", "An EPUB review witness requires publishing options."
        )

    identity = _request_identity(
        user_id=int(service.owner_user_id),
        document_id=document.id,
        export_format=export_format,
        expected_revision=expected_revision,
        start_h2_on_new_page=start_h2_on_new_page,
        epub_cover=effective_cover_options,
        epub_options=effective_epub_options,
        epub_review_hash=effective_review_hash,
        epub_image_mode=effective_image_mode,
    )
    request_hash = _canonical_hash(identity)
    if existing is not None:
        if not _status_matches(
            existing,
            user_id=int(service.owner_user_id),
            document_id=document.id,
            export_format=export_format,
            revision=expected_revision,
            request_hash=request_hash,
        ):
            raise _error(
                "idempotency_conflict",
                "The retry key is already bound to a different export request.",
            )
        if _state(existing) not in {"completed", "failed", "cancelled"}:
            _ensure_worker_pool(service)
        return _projection(
            document_id=document.id,
            operation_id=operation_id,
            status=existing,
            export_format=export_format,
            revision=expected_revision,
        )

    if (
        document.status != "completed"
        or not isinstance(document.output_html, str)
        or not document.output_html
    ):
        raise _error(
            "document_not_completed", "Export requires a completed extraction."
        )
    current_revision = _revision(document.output_html)
    if current_revision != expected_revision:
        raise _error(
            "revision_conflict",
            "The extraction changed; fetch fresh evidence before exporting.",
        )
    if effective_review_hash is not None:
        from .epub_quality import direct_epub_review_sha256
        from .metadata_service import get_canonical_document_metadata

        if effective_epub_options is None:
            raise _error(
                "invalid_export_request",
                "An EPUB review witness requires publishing options.",
            )
        expected_review_hash = direct_epub_review_sha256(
            current_revision,
            get_canonical_document_metadata(document),
            effective_epub_options,
            cover=effective_cover_options,
            image_mode=effective_image_mode,
        )
        if effective_review_hash != expected_review_hash:
            raise _error(
                "epub-review-stale",
                "The EPUB choices changed after preflight; review the current edition again.",
            )

    payload = dict(identity)
    enqueue = getattr(queue, "enqueue_once", None)
    if not callable(enqueue):
        raise _error("export_failed", "The document export did not complete.")
    try:
        status_metadata = {
            "contract": EXPORT_CONTRACT,
            "document_id": document.id,
            "format": export_format,
            "revision": expected_revision,
            "request_hash": request_hash,
        }
        if effective_epub_options:
            status_metadata["epub_options"] = effective_epub_options
        if effective_image_mode != "auto":
            status_metadata["epub_image_mode"] = effective_image_mode
        if effective_cover_options:
            status_metadata["epub_cover"] = effective_cover_options
        if effective_review_hash is not None:
            status_metadata["epub_review_hash"] = effective_review_hash
        enqueue(
            EXPORT_TASK,
            payload,
            job_id=operation_id,
            status_metadata=status_metadata,
        )
    except SafeSQLiteStoreError as exc:
        if str(exc) == "Queue job id is already bound to different work.":
            raise _error(
                "idempotency_conflict",
                "The retry key is already bound to a different export request.",
            ) from None
        raise _error("export_failed", "The document export did not complete.") from None
    except Exception:  # noqa: BLE001 - normalize storage/platform errors safely.
        # Queue/storage errors are kept path-free and do not claim an
        # idempotency conflict unless the durable queue reports that conflict.
        raise _error("export_failed", "The document export did not complete.") from None
    # A later same-key retry can launch the retained durable job if worker
    # startup fails; _ensure_worker_pool sanitizes that failure.
    _ensure_worker_pool(service)

    current_status = _queue_status(queue, operation_id)
    if current_status is None:
        raise _error("export_failed", "The document export did not complete.")
    if not _status_matches(
        current_status,
        user_id=int(service.owner_user_id),
        document_id=document.id,
        export_format=export_format,
        revision=expected_revision,
        request_hash=request_hash,
    ):
        raise _error(
            "idempotency_conflict",
            "The retry key is already bound to a different export request.",
        )
    return _projection(
        document_id=document.id,
        operation_id=operation_id,
        status=current_status,
        export_format=export_format,
        revision=expected_revision,
    )


def epub_preflight(
    service: Any,
    document_id: str,
    *,
    epub_options: object = None,
    epub_cover: object = None,
    expected_revision: str | None = None,
    epub_image_mode: object = "jpeg",
) -> dict[str, object]:
    """Return bounded EPUB diagnostics for one owned, completed revision."""

    try:
        image_mode = normalize_epub_image_mode(epub_image_mode)
        options = normalize_epub_options(
            {} if epub_options is None else epub_options
        )
    except EpubExportContractError:
        raise _error(
            "invalid_export_request", "The EPUB publishing options are invalid."
        ) from None
    from carlina.core.epub_cover import normalize_epub_cover

    try:
        cover_options = normalize_epub_cover(epub_cover)
    except ValueError:
        raise _error("invalid_export_request", "The EPUB cover option is invalid.") from None
    if expected_revision is not None and (
        not isinstance(expected_revision, str)
        or _SHA256_RE.fullmatch(expected_revision) is None
    ):
        raise _error(
            "invalid_export_request", "The document revision is invalid."
        )

    try:
        document = service._document(document_id, fresh=True)
    except DocProcessorMcpServiceError as exc:
        if exc.code == "not_found":
            raise _error("not_found", "The document was not found.") from None
        raise
    if (
        document.status != "completed"
        or not isinstance(document.output_html, str)
        or not document.output_html.strip()
    ):
        raise _error(
            "document_not_completed", "EPUB preflight requires a completed extraction."
        )
    revision = _revision(document.output_html)
    if expected_revision is not None and expected_revision != revision:
        raise _error(
            "revision_conflict",
            "The extraction changed; fetch fresh evidence before preflight.",
        )

    from .epub_packager import EPUBPackager
    from .epub_quality import direct_epub_review_sha256, preflight_epub
    from .metadata_service import get_canonical_document_metadata

    metadata = get_canonical_document_metadata(document)
    packager = EPUBPackager(
        document, "", options=options, cover=cover_options, image_mode=image_mode
    )
    result = preflight_epub(
        packager._raw_content(),
        metadata,
        options,
        cover=cover_options,
    )
    quality = result.get("quality")
    if not isinstance(quality, dict):
        raise _error("export_failed", "The EPUB preflight did not complete.")
    quality["input_sha256"] = revision
    from .epub_editions import current_source_revision

    return {
        "revision": revision,
        "edition_revision": current_source_revision(document),
        "review_hash": direct_epub_review_sha256(
            revision, metadata, options, cover=cover_options, image_mode=image_mode
        ),
        **result,
    }


def validate_read_options(
    operation_id: object,
    include_content: object,
    offset: object,
    limit: object,
    expected_sha256: object,
) -> tuple[str, bool, int, int, str | None]:
    if (
        not isinstance(operation_id, str)
        or _OPERATION_ID_RE.fullmatch(operation_id) is None
    ):
        raise _error(
            "invalid_export_request", "The document export request is invalid."
        )
    if type(include_content) is not bool:
        raise _error(
            "invalid_export_request", "The document export request is invalid."
        )
    if type(offset) is not int or offset < 0:
        raise _error(
            "invalid_export_request", "The document export request is invalid."
        )
    if type(limit) is not int or not 1 <= limit <= MAX_EXPORT_CHUNK_BYTES:
        raise _error(
            "invalid_export_request", "The document export request is invalid."
        )
    if expected_sha256 is not None and (
        not isinstance(expected_sha256, str)
        or _SHA256_RE.fullmatch(expected_sha256) is None
    ):
        raise _error(
            "invalid_export_request", "The document export request is invalid."
        )
    return operation_id, include_content, offset, limit, expected_sha256


def _artifact_record(
    document: ProcessedDocument,
    operation_id: str,
    status: Mapping[str, Any],
) -> tuple[Path, str, str, int, str]:
    result = status.get("result")
    export_format = status.get("format")
    revision = status.get("revision")
    if (
        not isinstance(result, dict)
        or not isinstance(export_format, str)
        or export_format not in EXPORT_FORMATS
        or not isinstance(revision, str)
        or _SHA256_RE.fullmatch(revision) is None
    ):
        raise _error("artifact_invalid", "The export artifact record is invalid.")
    download_name = result.get("download_name")
    byte_length = result.get("byte_length")
    sha256 = result.get("sha256")
    if (
        result.get("kind") != "file_download"
        or type(result.get("user_id")) is not int
        or result.get("user_id") != document.user_id
        or result.get("document_id") != document.id
        or result.get("operation_id") != operation_id
        or result.get("format") != export_format
        or result.get("revision") != revision
        or not isinstance(download_name, str)
        or len(download_name) > 255
        or not re.fullmatch(
            rf"[A-Za-z0-9_.-]+\.{re.escape(export_format)}", download_name
        )
        or result.get("mimetype") != _MIMETYPES[export_format]
        or type(byte_length) is not int
        or byte_length < 0
        or not isinstance(sha256, str)
        or _SHA256_RE.fullmatch(sha256) is None
        or not isinstance(result.get("output_path"), str)
    ):
        raise _error("artifact_invalid", "The export artifact record is invalid.")
    path = Path(result["output_path"])
    root = Path(current_app.instance_path) / "doc_processor_exports"
    expected_dir = root / operation_id
    if (
        not path.is_absolute()
        or str(path) != result["output_path"]
        or path.parent != expected_dir
        or path.name != download_name
        or any(part in {".", ".."} for part in path.parts)
    ):
        raise _error("artifact_invalid", "The export artifact record is invalid.")
    return path, export_format, _MIMETYPES[export_format], byte_length, sha256


def _validated_artifact_quality(
    result: Mapping[str, Any],
    export_format: str,
    artifact_sha256: str,
    *,
    status: Mapping[str, Any],
    expected_revision: str,
) -> dict[str, object] | None:
    has_publishing_options = "epub_options" in status
    has_review_hash = "epub_review_hash" in status
    quality = result.get("quality")
    if not has_publishing_options:
        if quality is not None or has_review_hash or "epub_review_hash" in result:
            raise _error(
                "artifact_invalid", "The export artifact record is invalid."
            )
        return None
    if export_format != "epub":
        raise _error("artifact_invalid", "The export artifact record is invalid.")
    if has_review_hash:
        review_hash = status.get("epub_review_hash")
        if (
            not isinstance(review_hash, str)
            or _SHA256_RE.fullmatch(review_hash) is None
            or result.get("epub_review_hash") != review_hash
        ):
            raise _error("artifact_invalid", "The export artifact record is invalid.")
    elif "epub_review_hash" in result:
        raise _error("artifact_invalid", "The export artifact record is invalid.")
    try:
        options = normalize_epub_options(status["epub_options"])
        if options != status["epub_options"]:
            raise EpubExportContractError("epub-options-normalization")
        normalized = normalize_epub_quality(quality)
    except EpubExportContractError:
        raise _error(
            "artifact_invalid", "The export artifact record is invalid."
        ) from None
    if (
        normalized["artifact_sha256"] != artifact_sha256
        or normalized["input_sha256"] != expected_revision
        or normalized["options_sha256"] != epub_value_sha256(options)
    ):
        raise _error("artifact_invalid", "The export artifact record is invalid.")
    return normalized


def _open_artifact(path: Path, operation_id: str) -> int:
    """Open an artifact through no-follow instance, export, and operation dirs."""

    if os.name == "nt":
        try:
            directory = WindowsArtifactDirectory.open_existing(
                Path(current_app.instance_path), "doc_processor_exports", operation_id
            )
            try:
                return directory.open_file(path.name)
            finally:
                directory.close()
        except WindowsArtifactError as exc:
            if exc.code == "invalid":
                raise _error(
                    "artifact_invalid", "The export file is invalid."
                ) from None
            raise _error(
                "artifact_unavailable", "The export file is unavailable."
            ) from None
        except OSError, ValueError:
            raise _error(
                "artifact_unavailable", "The export file is unavailable."
            ) from None

    if (
        not hasattr(os, "O_DIRECTORY")
        or not hasattr(os, "O_NOFOLLOW")
        or os.open not in os.supports_dir_fd
    ):
        raise _error("artifact_unavailable", "The export file is unavailable.")
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    file_flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
    instance_root = Path(current_app.instance_path)
    fds: list[int] = []
    try:
        fds.append(os.open(instance_root, directory_flags))
        fds.append(os.open("doc_processor_exports", directory_flags, dir_fd=fds[-1]))
        fds.append(os.open(operation_id, directory_flags, dir_fd=fds[-1]))
        opened = os.open(path.name, file_flags, dir_fd=fds[-1])
        if not stat.S_ISREG(os.fstat(opened).st_mode):
            os.close(opened)
            raise _error("artifact_invalid", "The export file is invalid.")
        return opened
    except DocProcessorMcpServiceError:
        raise
    except OSError, ValueError:
        raise _error(
            "artifact_unavailable", "The export file is unavailable."
        ) from None
    finally:
        for fd in reversed(fds):
            os.close(fd)


def export_document_get(
    service: Any,
    document_id: str,
    operation_id: str,
    *,
    include_content: bool = False,
    offset: int = 0,
    limit: int = MAX_EXPORT_CHUNK_BYTES,
    expected_sha256: str | None = None,
) -> dict[str, Any]:
    """Return a safe status or a bounded, integrity-witnessed artifact chunk."""

    operation_id, include_content, offset, limit, expected_sha256 = (
        validate_read_options(
            operation_id, include_content, offset, limit, expected_sha256
        )
    )
    try:
        document = service._document(document_id, fresh=True)
    except DocProcessorMcpServiceError as exc:
        if exc.code == "not_found":
            raise _error(
                "export_not_found", "Document export operation not found."
            ) from None
        raise
    queue = _queue(service)
    status = _queue_status(queue, operation_id)
    if status is None:
        raise _error("export_not_found", "Document export operation not found.")
    export_format = status.get("format")
    revision = status.get("revision")
    request_hash = status.get("request_hash")
    if (
        status.get("contract") != EXPORT_CONTRACT
        or status.get("task_name") != EXPORT_TASK
        or type(status.get("user_id")) is not int
        or status.get("user_id") != service.owner_user_id
        or status.get("document_id") != document.id
        or not isinstance(export_format, str)
        or export_format not in EXPORT_FORMATS
        or not isinstance(revision, str)
        or _SHA256_RE.fullmatch(revision) is None
        or not isinstance(request_hash, str)
        or _SHA256_RE.fullmatch(request_hash) is None
    ):
        raise _error("export_not_found", "Document export operation not found.")
    if "epub_review_hash" in status and (
        export_format != "epub"
        or "epub_options" not in status
        or not isinstance(status.get("epub_review_hash"), str)
        or _SHA256_RE.fullmatch(status["epub_review_hash"]) is None
    ):
        raise _error("export_not_found", "Document export operation not found.")

    payload = _projection(
        document_id=document.id,
        operation_id=operation_id,
        status=status,
        export_format=export_format,
        revision=revision,
    )
    if _state(status) != "completed":
        return payload

    path, export_format, media_type, byte_length, sha256 = _artifact_record(
        document, operation_id, status
    )
    result = status.get("result")
    if not isinstance(result, Mapping):
        raise _error("artifact_invalid", "The export artifact record is invalid.")
    quality = _validated_artifact_quality(
        result,
        export_format,
        sha256,
        status=status,
        expected_revision=revision,
    )
    if expected_sha256 is not None and expected_sha256 != sha256:
        raise _error(
            "checksum_mismatch", "The export checksum does not match expected_sha256."
        )
    if offset > byte_length:
        raise _error(
            "invalid_export_request", "The document export request is invalid."
        )

    fd = _open_artifact(path, operation_id)
    chunk = b""
    try:
        before = os.fstat(fd)
        if before.st_size != byte_length:
            raise _error(
                "artifact_invalid", "The export file size does not match its record."
            )
        if include_content:
            os.lseek(fd, offset, os.SEEK_SET)
            expected_size = min(limit, byte_length - offset)
            chunks: list[bytes] = []
            remaining = expected_size
            while remaining:
                part = os.read(fd, remaining)
                if not part:
                    raise _error(
                        "artifact_invalid", "The export file changed during the read."
                    )
                chunks.append(part)
                remaining -= len(part)
            chunk = b"".join(chunks)
        after = os.fstat(fd)
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
        ):
            raise _error("artifact_invalid", "The export file changed during the read.")
    except DocProcessorMcpServiceError:
        raise
    except OSError:
        raise _error(
            "artifact_unavailable", "The export file is unavailable."
        ) from None
    finally:
        os.close(fd)

    payload.update(
        {
            "ready": True,
            "filename": path.name,
            "media_type": media_type,
            "format": export_format,
            "byte_length": byte_length,
            "sha256": sha256,
        }
    )
    if quality is not None:
        payload["quality"] = quality
    if include_content:
        next_offset = offset + len(chunk)
        payload.update(
            {
                "content_base64": base64.b64encode(chunk).decode("ascii"),
                "offset": offset,
                "next_offset": next_offset,
                "eof": next_offset == byte_length,
                "chunk_sha256": hashlib.sha256(chunk).hexdigest(),
            }
        )
    return payload


__all__ = [
    "EXPORT_CONTRACT",
    "EXPORT_FORMATS",
    "EXPORT_TASK",
    "MAX_EXPORT_CHUNK_BYTES",
    "epub_preflight",
    "export_document",
    "export_document_get",
    "validate_read_options",
]
