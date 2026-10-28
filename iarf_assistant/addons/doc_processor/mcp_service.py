"""Owner-scoped Document Processor service for the MCP product adapter.

The MCP boundary deliberately talks to the existing document row, metadata,
knowledge-source, and queue contracts.  It does not expose the route's ORM
projection or any of the provider's private source/settings details.
"""

from __future__ import annotations

import logging
import mimetypes
import os
import shutil
import stat
import time
import uuid
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from sqlalchemy import update
from sqlalchemy.orm.attributes import flag_modified
from werkzeug.utils import secure_filename

from carlina.core.utc import as_utc, utc_now
from iarf_assistant.core.model_router import ModelRouter
from iarf_assistant.core.upload_paths import (
    resolve_upload_reference,
    upload_web_path,
    uploads_root,
)
from iarf_assistant.extensions import db
from iarf_assistant.models import McpExecution, McpWorkItem, User

from ...core.executor_profile import (
    ExecutorProfile,
    require_executor_profile,
)
from .api_client import DocProcessorClient
from .intake import (
    PROFILE_BOOK_QUALITY,
    build_document_settings,
    get_processing_profile,
    validate_pdf_model_readiness,
)
from .knowledge_adapter import ensure_processed_document_source
from .mcp_settings import DocumentSettings, document_settings_to_dict
from .metadata_service import initialize_document_metadata
from .models import ProcessedDocument
from .parsers.semantic_validation import (
    EXTRACTION_NOTE_INPUT_POLICY_SETTING,
    EXTRACTION_NOTE_INPUT_POLICY_VERSION,
)
from .performance_metrics import safe_preparation_timings

logger = logging.getLogger(__name__)
_EPUB_IMAGE_MODE_UNSET = object()


DEFAULT_MAX_UPLOAD_BYTES = 512 * 1024 * 1024
SUPPORTED_DOCUMENT_EXTENSIONS = frozenset(
    {
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
)

# These are the same supported processing inputs as the standalone retry
# route.  Filtering here also ensures that ``file_path``/IDs in an MCP
# settings object never become durable document settings.
_PDF_OPTION_KEYS = frozenset(
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
_ENGINE_OPTION_KEYS = frozenset(
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
_PUBLIC_PDF_OPTION_KEYS = _PDF_OPTION_KEYS - {
    "evaluate_pdf",
    "metadata_exclude_flagged_pages",
}
_PUBLIC_ENGINE_OPTION_KEYS = _ENGINE_OPTION_KEYS
_ACTIVE_DOCUMENT_STATUSES = frozenset(
    {"processing", "preparing_crop_review", "pausing", "suspended"}
)
_REVIEW_DOCUMENT_STATUSES = frozenset({"needs_cropping", "reviewing_images"})


class DocProcessorMcpServiceError(RuntimeError):
    """Safe, typed failure at the Document Processor MCP boundary."""

    def __init__(self, message: str, *, code: str = "document_processor_error"):
        self.code = code
        super().__init__(message)


# A compatibility alias is useful to callers that name the boundary error
# after the service rather than the add-on product.
DocProcessorMcpError = DocProcessorMcpServiceError


def _crop_box(value: object) -> list[float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    try:
        x0, y0, x1, y1 = [float(item) for item in value]
    except TypeError, ValueError:
        return None
    x_min, x_max = sorted((max(0.0, min(1.0, x0)), max(0.0, min(1.0, x1))))
    y_min, y_max = sorted((max(0.0, min(1.0, y0)), max(0.0, min(1.0, y1))))
    if x_max - x_min < 0.001 or y_max - y_min < 0.001:
        return None
    return [x_min, y_min, x_max, y_max]


def _crop_boxes(value: object) -> dict[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    return {
        "mode": "single"
        if str(value.get("mode") or "").lower() == "single"
        else "left_right",
        "left": _crop_box(value.get("left")),
        "right": _crop_box(value.get("right")),
        "first_page_side": "left"
        if str(value.get("first_page_side") or "").lower() == "left"
        else "right",
    }


def _safe_setting_input(
    settings: Mapping[str, Any] | DocumentSettings | None,
) -> dict[str, Any]:
    """Validate the closed public settings contract before any mutation."""

    if settings is None:
        return {}
    if not isinstance(settings, (Mapping, DocumentSettings)):
        raise DocProcessorMcpServiceError(
            "Document settings must be an object.", code="invalid_settings"
        )
    try:
        return document_settings_to_dict(settings)
    except ValidationError as exc:
        errors = exc.errors()
        paths = sorted(
            {
                ".".join(str(part) for part in error.get("loc", ())) or "settings"
                for error in errors
            }
        )
        unknown = any(error.get("type") == "extra_forbidden" for error in errors)
        prefix = (
            "Unknown document setting field(s)"
            if unknown
            else "Invalid document setting field(s)"
        )
        raise DocProcessorMcpServiceError(
            f"{prefix}: {', '.join(paths)}.", code="invalid_settings"
        ) from exc


def _engine_options(engine_id: str, values: Mapping[str, Any]) -> dict[str, Any]:
    raw = dict(values.get("engine_options") or {})
    for key in _ENGINE_OPTION_KEYS:
        if key in values:
            raw[key] = values[key]
    if engine_id == "marker":
        return {
            "force_ocr": raw.get("force_ocr", False),
            "paginate_output": raw.get("paginate_output", False),
        }
    if engine_id == "paddleocr":
        return {
            "use_doc_unwarping": raw.get("use_doc_unwarping", False),
            "use_doc_orientation_classify": raw.get(
                "use_doc_orientation_classify", False
            ),
            "layout_shape_mode": raw.get("layout_shape_mode", "auto"),
            "merge_tables": raw.get("merge_tables", True),
            "relevel_titles": raw.get("relevel_titles", True),
            "concatenate_pages": raw.get("concatenate_pages", False),
            "paddle_remove_hf": raw.get("paddle_remove_hf", True),
            "paddle_preserve_footnotes": raw.get("paddle_preserve_footnotes", False),
            "paddle_chart_rec": raw.get("paddle_chart_rec", False),
            "paddle_seal_rec": raw.get("paddle_seal_rec", False),
            "batch_size": raw.get("paddle_batch_size", 4),
        }
    if engine_id == "pymupdf":
        return {
            "use_ocr": raw.get("use_ocr", True),
            "force_ocr": raw.get("force_ocr", False),
            "paginate_output": raw.get("paginate_output", False),
            "exclude_footers": raw.get("exclude_footers", True),
            "embed_images": raw.get("embed_images", False),
        }
    return {}


class DocProcessorMcpService:
    """Owner-scoped Document Processor domain adapter."""

    def __init__(self, context, owner_user_id: int):
        self.context = context
        try:
            self.owner_user_id = int(owner_user_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("A valid owner user id is required.") from exc
        if self.owner_user_id <= 0:
            raise ValueError("A valid owner user id is required.")

    def _app_config(self, key: str, default: Any) -> Any:
        application = getattr(self.context, "application", None)
        config = getattr(application, "config", None)
        if config is not None and config.get(key) is not None:
            return config.get(key)
        try:
            from flask import current_app

            if current_app.config.get(key) is not None:
                return current_app.config.get(key)
        except ImportError, RuntimeError:
            pass
        return default

    def _max_upload_bytes(self) -> int:
        try:
            return max(
                1,
                int(
                    self._app_config(
                        "DOC_PROCESSOR_MAX_UPLOAD_BYTES", DEFAULT_MAX_UPLOAD_BYTES
                    )
                ),
            )
        except TypeError, ValueError:
            return DEFAULT_MAX_UPLOAD_BYTES

    def _uploads_dir(self) -> Path:
        configured = self._app_config("CARLINA_UPLOADS_DIR", None)
        if configured:
            return uploads_root(uploads_dir=configured)
        return uploads_root()

    def _client(self) -> DocProcessorClient:
        return DocProcessorClient(
            self.context,
            {"current_user_id": self.owner_user_id},
        )

    def _resolve_engine(
        self,
        filename: str,
        values: Mapping[str, Any],
        requested_engine: str,
    ) -> str:
        requested = str(requested_engine or "auto").strip().lower() or "auto"
        profile = get_processing_profile(values)
        suffix = Path(filename).suffix.lower().lstrip(".")
        selection = requested
        if profile == PROFILE_BOOK_QUALITY and suffix == "pdf":
            selection = "llm_vision"
        elif (
            profile == PROFILE_BOOK_QUALITY
            and suffix != "pdf"
            and requested == "llm_vision"
        ):
            selection = "auto"

        try:
            client = self._client()
            selected = (
                str(
                    client.select_engine(
                        filename,
                        engine=selection,
                        **{
                            key: values[key]
                            for key in ("force_ocr", "no_ocr")
                            if key in values
                        },
                    )
                )
                .strip()
                .lower()
            )
            catalog_entry = next(
                (
                    engine
                    for engine in client.get_engines()
                    if str(engine.get("id") or "").strip().lower() == selected
                ),
                None,
            )
            if catalog_entry is None:
                raise ValueError("The selected extraction engine is not active.")
            if str(catalog_entry.get("status") or "").strip().lower() not in {
                "active",
                "ready",
            }:
                raise ValueError("The selected extraction engine is not active.")
            if not client._engine_supports_suffix(catalog_entry, suffix):
                raise ValueError(
                    "The selected extraction engine does not support this file type."
                )
            return selected
        except DocProcessorMcpServiceError:
            raise
        except Exception as exc:
            raise DocProcessorMcpServiceError(
                "Unable to resolve an extraction engine.",
                code="engine_unavailable",
            ) from exc

    def _build_settings(
        self,
        filename: str,
        values: Mapping[str, Any] | None,
        *,
        stored: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        incoming = _safe_setting_input(values)
        previous = deepcopy(dict(stored or {}))
        requested = (
            str(
                incoming.get(
                    "engine",
                    previous.get(
                        "requested_engine_id", previous.get("engine_id", "auto")
                    ),
                )
                or "auto"
            )
            .strip()
            .lower()
        )

        seed: dict[str, Any] = {
            "engine": requested,
            "parsing_model": previous.get("parsing_model"),
            "metadata_model": previous.get("metadata_model"),
            "pdf_options": deepcopy(dict(previous.get("pdf_options") or {})),
        }
        if previous.get("processing_profile") not in (None, ""):
            seed["processing_profile"] = previous["processing_profile"]
        for key in ("crop_box", "crop_boxes"):
            if key in previous:
                seed[key] = deepcopy(previous[key])
        profile_override = "processing_profile" in incoming
        if profile_override:
            seed.pop("processing_profile", None)
        seed.update(incoming)
        if isinstance(incoming.get("pdf_options"), Mapping):
            seed["pdf_options"] = {
                **dict(previous.get("pdf_options") or {}),
                **dict(incoming["pdf_options"]),
            }
        effective_engine = self._resolve_engine(filename, seed, requested)
        models_changed = any(
            key in incoming for key in ("parsing_model", "metadata_model")
        )
        default_model = (
            previous.get("parsing_model")
            or previous.get("metadata_model")
            or ModelRouter.get_model_for_tier("medium", self.owner_user_id)
        )
        owner = db.session.get(User, self.owner_user_id)
        if owner is None:
            raise DocProcessorMcpServiceError(
                "Document owner was not found.", code="owner_not_found"
            )
        rebuilt = build_document_settings(
            seed,
            default_model=default_model,
            effective_engine=effective_engine,
            requested_engine=requested,
            user=owner
            if stored is None or models_changed or profile_override
            else None,
        )
        if (
            "engine_options" in incoming
            or "engine" in incoming
        ):
            options = _engine_options(effective_engine, incoming)
            if options:
                rebuilt["engine_options"] = options
            else:
                rebuilt.pop("engine_options", None)

        merged = deepcopy(previous)
        merged.update(rebuilt)
        # Crop values are normalized by the route wrapper; perform the same
        # small normalization at this boundary without importing Flask routes.
        if "crop_box" in merged:
            merged["crop_box"] = _crop_box(merged.get("crop_box"))
        if "crop_boxes" in merged:
            normalized_boxes = _crop_boxes(merged.get("crop_boxes"))
            if normalized_boxes is not None:
                merged["crop_boxes"] = normalized_boxes
        return merged

    def _readiness(
        self,
        filename: str,
        settings: Mapping[str, Any],
        *,
        require_credential: bool,
    ) -> None:
        error = validate_pdf_model_readiness(
            filename,
            settings,
            user_id=self.owner_user_id,
            require_credential=require_credential,
        )
        if error:
            raise DocProcessorMcpServiceError(error, code="model_not_ready")

    @staticmethod
    def _source_file(path: object) -> Path:
        try:
            candidate = Path(path)  # type: ignore[arg-type]
            stat_result = candidate.lstat()
        except (OSError, TypeError, ValueError) as exc:
            raise DocProcessorMcpServiceError(
                "Source document is unavailable.", code="source_unavailable"
            ) from exc
        if (
            candidate.is_symlink()
            or not candidate.is_file()
            or not stat.S_ISREG(stat_result.st_mode)
        ):
            raise DocProcessorMcpServiceError(
                "Source document must be a regular file.", code="invalid_source"
            )
        return candidate

    def _stored_source(self, document: ProcessedDocument) -> Path:
        try:
            source = resolve_upload_reference(
                document.file_path,
                uploads_dir=self._uploads_dir(),
            )
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise DocProcessorMcpServiceError(
                "Source document is unavailable.", code="source_unavailable"
            ) from exc
        return self._source_file(source)

    @staticmethod
    def _pdf_page_count(source: Path) -> int | None:
        """Inspect a PDF page count opportunistically for the public projection."""

        try:
            import fitz

            pdf = fitz.open(source)
            try:
                return len(pdf)
            finally:
                pdf.close()
        except Exception:  # noqa: BLE001 - optional inspection must not fail reads
            try:
                from pypdf import PdfReader

                return len(PdfReader(source).pages)
            except Exception:  # noqa: BLE001 - optional inspection is best effort
                return None

    def _source_descriptor(self, document: ProcessedDocument) -> dict[str, Any]:
        """Return safe source facts without exposing the stored path."""

        filename = str(document.filename or "")
        extension = Path(filename).suffix.lower()
        descriptor: dict[str, Any] = {
            "extension": extension,
            "media_type": mimetypes.guess_type(filename)[0]
            or "application/octet-stream",
            "byte_size": None,
            "page_count": None,
        }
        try:
            source = self._stored_source(document)
            descriptor["byte_size"] = source.stat().st_size
            if extension == ".pdf":
                descriptor["page_count"] = self._pdf_page_count(source)
        except (DocProcessorMcpServiceError, OSError):
            # A stale/missing source must not hide an otherwise readable row.
            pass
        return descriptor

    @staticmethod
    def _effective_settings_projection(
        settings: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        """Project persisted settings to canonical user-configurable fields."""

        raw = dict(settings or {})
        projected: dict[str, Any] = {}
        engine = raw.get("engine_id", raw.get("engine"))
        if engine not in (None, ""):
            projected["engine"] = str(engine)
        for key in ("parsing_model", "metadata_model", "processing_profile"):
            if raw.get(key) not in (None, ""):
                projected[key] = deepcopy(raw[key])

        pdf_options = raw.get("pdf_options")
        if isinstance(pdf_options, Mapping):
            pdf_projection: dict[str, Any] = {}
            for key in _PUBLIC_PDF_OPTION_KEYS:
                if key in pdf_options:
                    pdf_projection[key] = deepcopy(pdf_options[key])
            for legacy, canonical in (
                ("evaluate_pdf", "evaluate"),
                ("metadata_exclude_flagged_pages", "metadata_delete_flagged_pages"),
            ):
                if canonical not in pdf_projection and legacy in pdf_options:
                    pdf_projection[canonical] = deepcopy(pdf_options[legacy])
            if pdf_projection:
                projected["pdf_options"] = pdf_projection

        if "crop_box" in raw:
            crop_box = _crop_box(raw.get("crop_box"))
            if crop_box is not None:
                projected["crop_box"] = crop_box
        if "crop_boxes" in raw:
            crop_boxes = _crop_boxes(raw.get("crop_boxes"))
            if crop_boxes is not None:
                projected["crop_boxes"] = crop_boxes

        engine_options = raw.get("engine_options")
        if isinstance(engine_options, Mapping):
            engine_projection: dict[str, Any] = {}
            for key in _PUBLIC_ENGINE_OPTION_KEYS:
                if key in engine_options:
                    engine_projection[key] = deepcopy(engine_options[key])
            if (
                "paddle_batch_size" not in engine_projection
                and "batch_size" in engine_options
            ):
                engine_projection["paddle_batch_size"] = deepcopy(
                    engine_options["batch_size"]
                )
            if engine_projection:
                projected["engine_options"] = engine_projection
        return projected

    def _copy_source(self, source: Path, destination: Path) -> None:
        """Copy from one inode-verified descriptor without following a raced link."""

        try:
            before = source.lstat()
            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
            flags |= getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(source, flags)
        except OSError as exc:
            raise DocProcessorMcpServiceError(
                "Source document is unavailable.", code="source_unavailable"
            ) from exc
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_dev != before.st_dev
                or opened.st_ino != before.st_ino
            ):
                raise DocProcessorMcpServiceError(
                    "Source document changed during import.", code="invalid_source"
                )
            maximum = self._max_upload_bytes()
            copied = 0
            with os.fdopen(descriptor, "rb", closefd=False) as source_handle:
                with destination.open("xb") as destination_handle:
                    while True:
                        chunk = source_handle.read(
                            min(1024 * 1024, maximum - copied + 1)
                        )
                        if not chunk:
                            break
                        copied += len(chunk)
                        if copied > maximum:
                            raise DocProcessorMcpServiceError(
                                "Document exceeds the configured upload limit.",
                                code="upload_too_large",
                            )
                        destination_handle.write(chunk)
        finally:
            os.close(descriptor)

    def _document(self, document_id: str, *, fresh: bool = False) -> ProcessedDocument:
        normalized = str(document_id or "").strip()
        query = ProcessedDocument.query.filter_by(
            id=normalized,
            user_id=self.owner_user_id,
        )
        document = (query.populate_existing() if fresh else query).first()
        if document is None:
            raise DocProcessorMcpServiceError(
                "Document was not found.", code="not_found"
            )
        return document

    def _safe_document(
        self,
        document_id: str,
        *,
        include_output: bool = False,
        document_record: ProcessedDocument | None = None,
    ) -> dict:
        if document_record is None:
            document_record = self._document(document_id)
        document = self._client()._source_payload(
            document_record, include_content=include_output
        )
        document["source"] = self._source_descriptor(document_record)
        document["effective_settings"] = self._effective_settings_projection(
            document_record.settings
        )
        checkpoint = self._checkpoint_projection(document_record)
        if checkpoint:
            document["checkpoint"] = checkpoint
        preparation = self._preparation_projection(document_record)
        if preparation:
            document["metadata_preparation"] = preparation
        preparation_timings = self._preparation_timings_projection(
            document_record
        )
        if preparation_timings:
            document["preparation_timings"] = preparation_timings
        return document

    @staticmethod
    def _preparation_projection(document: ProcessedDocument) -> list[dict]:
        metrics = (document.settings or {}).get("metrics")
        entries = (
            metrics.get("metadata_preparation")
            if isinstance(metrics, Mapping)
            else None
        )
        if not isinstance(entries, list):
            return []
        stages = {
            "metadata_initial",
            "metadata_follow_up",
            "metadata_follow_up_budget",
            "heading_initial",
            "heading_refinement",
            "metadata_preparation",
            *(
                f"heading_round_{index}{suffix}"
                for index in range(3)
                for suffix in ("", "_budget")
            ),
        }
        reasons = {
            "heading_evidence_limit",
            "evidence_limit",
            "input_budget",
            "refinement_failed",
            "required_failure",
            "toc_window_limit",
            "optional_follow_up_failed",
        }
        result = []
        for entry in entries[-12:]:
            if not isinstance(entry, Mapping):
                continue
            stage, status = entry.get("stage"), entry.get("status")
            if not isinstance(stage, str) or stage not in stages:
                continue
            if not isinstance(status, str) or status not in {
                "admitted",
                "limited",
                "provisional",
                "completed",
                "failed",
            }:
                continue
            safe = {"stage": stage, "status": status}
            reason = entry.get("reason")
            if isinstance(reason, str) and reason in reasons:
                safe["reason"] = reason
            for key in (
                "requested_input_tokens",
                "admitted_input_tokens",
                "declared_input_tokens",
                "output_token_limit",
                "message_tokens",
                "schema_tokens",
            ):
                value = entry.get(key)
                if type(value) is int and 0 <= value <= 1_000_000_000:
                    safe[key] = value
            result.append(safe)
        return result

    @staticmethod
    def _preparation_timings_projection(
        document: ProcessedDocument,
    ) -> dict[str, dict[str, int | float]]:
        metrics = (document.settings or {}).get("metrics")
        if not isinstance(metrics, Mapping):
            return {}
        return safe_preparation_timings(metrics.get("preparation_timings"))

    def _external_work_timing_projection(
        self, document: ProcessedDocument, execution_id: str | None
    ) -> dict[str, Any]:
        normalized_execution_id = str(execution_id or "").strip()
        if not normalized_execution_id:
            return {}

        authorized_execution = (
            McpExecution.query.filter_by(
                id=normalized_execution_id,
                owner_user_id=self.owner_user_id,
                addon_id="doc_processor",
                resource_kind="document",
                resource_id=document.id,
            )
            .with_entities(McpExecution.id)
            .first()
        )
        if authorized_execution is None:
            return {}

        rows = (
            db.session.query(
                McpWorkItem.id,
                McpWorkItem.state,
                McpWorkItem.created_at,
                McpWorkItem.leased_at,
                McpWorkItem.completed_at,
                McpWorkItem.attempt_count,
            )
            .join(McpExecution, McpExecution.id == McpWorkItem.execution_id)
            .filter(
                McpExecution.id == normalized_execution_id,
                McpExecution.owner_user_id == self.owner_user_id,
                McpExecution.addon_id == "doc_processor",
                McpExecution.resource_kind == "document",
                McpExecution.resource_id == document.id,
            )
            .order_by(McpWorkItem.created_at.asc(), McpWorkItem.id.asc())
            .limit(5001)
            .all()
        )
        if len(rows) > 5000:
            return {
                "pending_count": None,
                "leased_count": None,
                "completed_count": None,
                "oldest_unclaimed_seconds": None,
                "latest_lease_to_completion_seconds": {
                    "sum": None,
                    "count": None,
                },
                "truncated": True,
            }

        pending_count = sum(row.state == "pending" for row in rows)
        leased_count = sum(row.state == "leased" for row in rows)
        completed_count = sum(row.state == "completed" for row in rows)
        now = as_utc(utc_now())
        unclaimed_created_at = [
            as_utc(row.created_at)
            for row in rows
            if row.state == "pending"
            and row.attempt_count == 0
            and row.created_at is not None
        ]
        oldest_unclaimed_seconds = (
            round(max(0.0, (now - min(unclaimed_created_at)).total_seconds()), 6)
            if unclaimed_created_at
            else None
        )
        latest_lease_durations = []
        for row in rows:
            if (
                row.state != "completed"
                or row.leased_at is None
                or row.completed_at is None
            ):
                continue
            leased_at = as_utc(row.leased_at)
            completed_at = as_utc(row.completed_at)
            if completed_at >= leased_at:
                latest_lease_durations.append(
                    (completed_at - leased_at).total_seconds()
                )
        latest_lease_to_completion = {
            "sum": round(sum(latest_lease_durations), 6),
            "count": len(latest_lease_durations),
        }
        return {
            "pending_count": pending_count,
            "leased_count": leased_count,
            "completed_count": completed_count,
            "oldest_unclaimed_seconds": oldest_unclaimed_seconds,
            "latest_lease_to_completion_seconds": latest_lease_to_completion,
            "truncated": False,
        }

    @staticmethod
    def _checkpoint_projection(document: ProcessedDocument) -> dict:
        state = (document.settings or {}).get("pdf_state")
        if not isinstance(state, Mapping):
            return {}
        parts = state.get("html_parts")
        if not isinstance(parts, list):
            return {}
        page_count = sum(isinstance(part, str) and bool(part.strip()) for part in parts)
        if not page_count:
            return {}
        boundaries = state.get("batch_boundaries")
        return {
            "validated_pages": page_count,
            "validated_batches": len(boundaries) if isinstance(boundaries, Mapping) else 0,
            "new_execution_resume_supported": False,
        }

    def _check_passive_checkpoint(self, document, *, restart_from_scratch: bool) -> None:
        if not isinstance(restart_from_scratch, bool):
            raise DocProcessorMcpServiceError(
                "restart_from_scratch must be boolean.", code="invalid_restart_choice"
            )
        if (
            not restart_from_scratch
            and document.status not in {"completed", "reviewing_images"}
            and self._checkpoint_projection(document)
        ):
            raise DocProcessorMcpServiceError(
                "Validated PDF pages are saved in this document. A new passive execution "
                "cannot resume that checkpoint. Preserve it for recovery, or explicitly "
                "set restart_from_scratch=true to discard it and extract again.",
                code="checkpoint_restart_required",
            )

    def import_document(
        self,
        source_path: Path,
        *,
        settings: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        source = self._source_file(source_path)
        safe_filename = secure_filename(source.name)
        if not safe_filename:
            raise DocProcessorMcpServiceError(
                "Source filename is not usable.", code="invalid_source"
            )
        if Path(safe_filename).suffix.lower() not in SUPPORTED_DOCUMENT_EXTENSIONS:
            raise DocProcessorMcpServiceError(
                "Unsupported document type.", code="unsupported_type"
            )
        try:
            source_size = source.stat().st_size
        except OSError as exc:
            raise DocProcessorMcpServiceError(
                "Source document is unavailable.", code="source_unavailable"
            ) from exc
        if source_size > self._max_upload_bytes():
            raise DocProcessorMcpServiceError(
                "Document exceeds the configured upload limit.", code="upload_too_large"
            )

        try:
            effective_settings = self._build_settings(source.name, settings)
        except DocProcessorMcpServiceError:
            raise
        except (LookupError, RuntimeError, TypeError, ValueError) as exc:
            raise DocProcessorMcpServiceError(
                "Document settings are invalid.", code="invalid_settings"
            ) from exc
        upload_root = self._uploads_dir()
        document_uuid = uuid.uuid4().hex
        document_dir = (
            upload_root / str(self.owner_user_id) / "doc_processor_docs" / document_uuid
        )
        created_source_dir = False
        try:
            document_dir.mkdir(parents=True, exist_ok=False)
            created_source_dir = True
            destination = document_dir / safe_filename
            self._copy_source(source, destination)
            document = ProcessedDocument(
                user_id=self.owner_user_id,
                filename=source.name,
                file_path=upload_web_path(destination, uploads_dir=upload_root),
                status="created",
                settings=effective_settings,
            )
            db.session.add(document)
            db.session.flush()
            initialize_document_metadata(document, destination)
            if document.metadata_record is not None:
                db.session.add(document.metadata_record)
            ensure_processed_document_source(document)
            db.session.commit()
        except DocProcessorMcpServiceError:
            db.session.rollback()
            if created_source_dir:
                shutil.rmtree(document_dir, ignore_errors=True)
            raise
        except Exception as exc:
            db.session.rollback()
            if created_source_dir:
                shutil.rmtree(document_dir, ignore_errors=True)
            logger.exception("Failed to initialize MCP document import.")
            raise DocProcessorMcpServiceError(
                "Unable to save the document.", code="import_failed"
            ) from exc
        return self._safe_document(document.id)

    def configure_document(
        self,
        document_id: str,
        settings: Mapping[str, Any],
    ) -> dict[str, Any]:
        document = self._document(document_id)
        if document.status in _ACTIVE_DOCUMENT_STATUSES:
            raise DocProcessorMcpServiceError(
                "Document processing is already active.", code="document_active"
            )
        source = self._stored_source(document)
        try:
            effective_settings = self._build_settings(
                document.filename or source.name,
                settings,
                stored=document.settings,
            )
        except DocProcessorMcpServiceError:
            raise
        except (LookupError, RuntimeError, TypeError, ValueError) as exc:
            raise DocProcessorMcpServiceError(
                "Document settings are invalid.", code="invalid_settings"
            ) from exc
        document.settings = effective_settings
        flag_modified(document, "settings")
        try:
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            raise DocProcessorMcpServiceError(
                "Unable to save document settings.", code="configure_failed"
            ) from exc
        return self._safe_document(document.id)

    def _queue(self):
        if self.context is None:
            raise DocProcessorMcpServiceError(
                "Document Processor queue is unavailable.", code="queue_unavailable"
            )
        try:
            self.context.ensure_worker_pool("worker.py", num_workers=1)
            return self.context.get_task_queue("doc_processor")
        except Exception as exc:
            raise DocProcessorMcpServiceError(
                "Document Processor queue is unavailable.", code="queue_unavailable"
            ) from exc

    def _persist_queue_marker(self, document: ProcessedDocument, job_id: str) -> None:
        execution_id = (document.settings or {}).get("mcp_execution_id")
        with db.session.no_autoflush:
            reserved = db.session.execute(
                update(ProcessedDocument)
                .where(
                    ProcessedDocument.id == document.id,
                    ProcessedDocument.status.in_(
                        {"processing", "preparing_crop_review"}
                    ),
                    ProcessedDocument.settings["mcp_execution_id"].as_string()
                    == execution_id,
                )
                .values(
                    status=ProcessedDocument.status,
                    updated_at=ProcessedDocument.updated_at,
                )
                .execution_options(synchronize_session=False)
            ).rowcount
        if reserved != 1:
            db.session.rollback()
            return
        # Serialize with terminal worker publication, then merge fresh settings
        # instead of restoring the producer's pre-enqueue snapshot.
        db.session.refresh(document)
        settings = dict(document.settings or {})
        settings["active_job_id"] = str(job_id)
        settings.pop("enqueue_claim", None)
        document.settings = settings
        flag_modified(document, "settings")
        db.session.commit()

    def _clear_failed_enqueue(
        self,
        document_id: str,
        previous_status: str,
    ) -> None:
        db.session.rollback()
        document = db.session.get(ProcessedDocument, document_id)
        if document is None:
            return
        document.status = previous_status
        settings = dict(document.settings or {})
        settings.pop("active_job_id", None)
        settings.pop("enqueue_claim", None)
        settings.pop("mcp_execution_id", None)
        settings.pop("mcp_execution_mode", None)
        document.settings = settings
        document.error_message = None
        flag_modified(document, "settings")
        db.session.commit()

    def preflight_passive_document(
        self, document_id: str, executor_profile: ExecutorProfile,
        *, restart_from_scratch: bool = False,
    ) -> None:
        """Reject an impossible initial image window before rendering or queuing.

        This is a lower-bound check. Exact prompt/schema admission still runs on
        every prepared packet, including later metadata evidence expansions.
        """
        from ...core.model_capabilities import FALLBACK_IMAGE_INPUT_TOKENS_HIGH

        document = self._document(document_id)
        if Path(document.filename or "").suffix.lower() != ".pdf":
            return
        if document.status == "reviewing_images":
            return
        self._check_passive_checkpoint(document, restart_from_scratch=restart_from_scratch)
        options = dict((document.settings or {}).get("pdf_options") or {})
        if not options.get("metadata_prepass", True):
            return
        page_count = self._source_descriptor(document).get("page_count")
        if not isinstance(page_count, int) or page_count < 1:
            return
        excluded = options.get("excluded_pages") or []
        values = excluded if isinstance(excluded, list) else str(excluded).split(",")
        excluded_indexes = set()
        for value in values:
            try:
                index = int(value)
            except (TypeError, ValueError):
                continue
            if 0 <= index < page_count:
                excluded_indexes.add(index)
        available_pages = page_count - len(excluded_indexes)
        start_pages = max(1, int(options.get("metadata_pages", 20) or 20))
        end_pages = max(0, int(options.get("metadata_end_pages", 0) or 0))
        image_count = min(available_pages, start_pages + end_pages)
        image_tokens = image_count * FALLBACK_IMAGE_INPUT_TOKENS_HIGH
        if image_tokens >= executor_profile.max_input_tokens:
            raise DocProcessorMcpServiceError(
                f"Metadata preparation needs {image_count} images × "
                f"{FALLBACK_IMAGE_INPUT_TOKENS_HIGH} = {image_tokens} input tokens "
                "before prompt/schema overhead, but executor_profile.max_input_tokens "
                f"is {executor_profile.max_input_tokens}. Reduce metadata_pages/metadata_end_pages "
                "and leave room for text, or declare a larger profile within actual executor capacity.",
                code="metadata_input_budget_exceeded",
            )

    def start_document(
        self,
        document_id: str,
        *,
        execution_id: str,
        mode: str,
        executor_profile: ExecutorProfile | Mapping[str, Any] | None = None,
        batch_plan: Mapping[str, Any] | None = None,
        restart_from_scratch: bool = False,
    ) -> dict[str, Any]:
        document = self._document(document_id)
        if document.status in _ACTIVE_DOCUMENT_STATUSES:
            raise DocProcessorMcpServiceError(
                "Document processing is already active.", code="document_active"
            )
        normalized_execution_id = str(execution_id or "").strip()
        normalized_mode = str(mode or "").strip().lower()
        if not normalized_execution_id:
            raise DocProcessorMcpServiceError(
                "An execution id is required.", code="invalid_execution"
            )
        if normalized_mode not in {"managed", "passive"}:
            raise DocProcessorMcpServiceError(
                "Execution mode is invalid.", code="invalid_execution_mode"
            )
        if restart_from_scratch and normalized_mode != "passive":
            raise DocProcessorMcpServiceError(
                "restart_from_scratch is only supported for passive PDF execution.",
                code="invalid_restart_choice",
            )
        if normalized_mode == "passive":
            try:
                normalized_profile = (
                    executor_profile
                    if isinstance(executor_profile, ExecutorProfile)
                    else require_executor_profile(executor_profile)
                )
            except ValueError as exc:
                raise DocProcessorMcpServiceError(
                    "A declared, task-authorized executor profile is required for passive execution.",
                    code="invalid_executor_profile",
                ) from exc
        elif executor_profile is not None:
            raise DocProcessorMcpServiceError(
                "executor_profile is only accepted for passive execution.",
                code="invalid_executor_profile",
            )
        source = self._stored_source(document)
        is_pdf = Path(document.filename or source.name).suffix.lower() == ".pdf"
        if normalized_mode == "passive" and is_pdf:
            self._check_passive_checkpoint(document, restart_from_scratch=restart_from_scratch)
            if batch_plan is None:
                page_count = self._source_descriptor(document).get("page_count")
                page_hint = str(page_count) if page_count is not None else "unavailable"
                raise DocProcessorMcpServiceError(
                    "Passive PDF execution requires a valid orchestrator-authored "
                    f"batch_plan derived from source.page_count (currently {page_hint}).",
                    code="missing_batch_plan",
                )
            try:
                from .batching import normalize_document_batch_plan

                batch_plan = normalize_document_batch_plan(batch_plan)
            except (TypeError, ValueError) as exc:
                raise DocProcessorMcpServiceError(
                    "Passive PDF batch_plan is invalid; derive its ranges from source.page_count.",
                    code="invalid_batch_plan",
                ) from exc
        elif batch_plan is not None:
            if normalized_mode != "passive" or not is_pdf:
                raise DocProcessorMcpServiceError(
                    "Document batching is available only for passive PDF execution.",
                    code="invalid_batch_plan",
                )

        previous_status = str(document.status or "created")
        settings = dict(document.settings or {})
        previous_execution_id = str(
            settings.get("mcp_execution_id") or settings.get("mcp_last_execution_id") or ""
        ).strip()
        if previous_execution_id != normalized_execution_id:
            metrics = settings.get("metrics")
            if isinstance(metrics, Mapping) and "preparation_timings" in metrics:
                metrics = dict(metrics)
                metrics.pop("preparation_timings", None)
                settings["metrics"] = metrics
        settings["mcp_execution_id"] = normalized_execution_id
        settings["mcp_execution_mode"] = normalized_mode
        engine_id = str(settings.get("engine_id") or "auto").strip().lower() or "auto"
        if batch_plan is not None:
            settings["mcp_batch_plan"] = dict(batch_plan)
            if document.status != "reviewing_images":
                # Freeze packet semantics for this new run. Existing executions
                # without this marker retain their original page-wave replay.
                settings["mcp_page_batch_protocol"] = "carlina.pdf-page-batch.v1"
                settings["mcp_page_blank_policy"] = "agent-declared-v1"
                settings["mcp_image_transport"] = "png-2000-v1"
                settings["mcp_batch_feedback"] = "validation-v1"
                settings["mcp_metadata_policy"] = "budgeted-v1"
                settings["mcp_semantic_structure_policy"] = "lists-diagrams-v1"
                settings["mcp_image_accessibility_policy"] = "image-descriptions-v1"
                settings["mcp_native_vision_authoring_policy"] = "direct-v1"
                settings["mcp_note_arrangement_policy"] = "source-arrangement-v1"
                settings["mcp_cover_candidate_policy"] = "pdf-page-v1"
                settings["mcp_extraction_issues_policy"] = "issues-v1"
                settings[EXTRACTION_NOTE_INPUT_POLICY_SETTING] = (
                    EXTRACTION_NOTE_INPUT_POLICY_VERSION
                )
                settings.pop("mcp_metadata_complete", None)
                settings.pop("mcp_last_execution_id", None)
                metrics = dict(settings.get("metrics") or {})
                metrics.pop("batch_validation_error", None)
                metrics.pop("metadata_preparation", None)
                settings["metrics"] = metrics
                settings.pop("mcp_document_context", None)
                settings.pop("pdf_state", None)
                settings.pop("extraction_issues_checkpoint", None)
        else:
            settings.pop("mcp_batch_plan", None)
            settings.pop("mcp_page_batch_protocol", None)
            settings.pop("mcp_page_blank_policy", None)
            settings.pop("mcp_image_transport", None)
            settings.pop("mcp_batch_feedback", None)
            settings.pop("mcp_metadata_policy", None)
            settings.pop("mcp_semantic_structure_policy", None)
            settings.pop("mcp_image_accessibility_policy", None)
            settings.pop("mcp_native_vision_authoring_policy", None)
            settings.pop("mcp_note_arrangement_policy", None)
            settings.pop("mcp_cover_candidate_policy", None)
            settings.pop("mcp_extraction_issues_policy", None)
            settings.pop(EXTRACTION_NOTE_INPUT_POLICY_SETTING, None)
            settings.pop("mcp_metadata_complete", None)
            settings.pop("mcp_document_context", None)
            settings.pop("extraction_issues_checkpoint", None)
        if normalized_mode == "managed":
            self._readiness(
                document.filename or "",
                settings,
                require_credential=True,
            )
        elif (
            is_pdf
            and engine_id == "llm_vision"
            and "image" not in normalized_profile.input_modalities
        ):
            raise DocProcessorMcpServiceError(
                "Passive PDF llm_vision execution requires image input in executor_profile.",
                code="executor_profile_capability",
            )
        pdf_options = dict(settings.get("pdf_options") or {})
        if normalized_mode == "passive" and is_pdf:
            self.preflight_passive_document(
                document_id, normalized_profile, restart_from_scratch=restart_from_scratch
            )
            pdf_options["skip_cropping"] = True
            settings["pdf_options"] = pdf_options
        if document.status == "reviewing_images" or (
            document.status == "needs_cropping"
            and not (normalized_mode == "passive" and is_pdf)
        ):
            document.settings = settings
            flag_modified(document, "settings")
            db.session.commit()
            return {
                "document": self._safe_document(document.id),
                "queue_job_id": None,
                "state": "waiting_review",
            }
        needs_crop = bool(
            is_pdf
            and engine_id in {"auto", "llm_vision"}
            and not pdf_options.get("skip_cropping", False)
        )
        if needs_crop and pdf_options.get("metadata_prepass", True):
            task_name = "prepare_crop_review"
            document_state = "preparing_crop_review"
            response_state = "queued"
        elif needs_crop:
            task_name = None
            document_state = "needs_cropping"
            response_state = "waiting_review"
        else:
            task_name = "process_document"
            document_state = "processing"
            response_state = "queued"

        document.settings = settings
        document.status = document_state
        document.error_message = None
        initial_job_id = None
        if normalized_mode == "passive" and is_pdf and task_name == "process_document":
            from .execution_recovery import freeze_recovery_contract, settings_fingerprint

            execution = db.session.get(McpExecution, normalized_execution_id)
            if execution is not None:
                initial_job_id = str(uuid.uuid4())
                execution.request_json = {
                    **dict(execution.request_json or {}),
                    "recovery": {
                        **freeze_recovery_contract(document, source, settings),
                        "initial_settings_sha256": settings_fingerprint(settings),
                        "initial_attempt": {
                            "job_id": initial_job_id,
                            "payload": {
                                "document_id": document.id,
                                "user_id": self.owner_user_id,
                                "mcp_execution_id": normalized_execution_id,
                                "mcp_execution_mode": normalized_mode,
                                **({"engine_id": engine_id} if engine_id else {}),
                            },
                        },
                    },
                }
                execution.queue_job_id = initial_job_id
                settings["active_job_id"] = initial_job_id
        if task_name is not None:
            settings["enqueue_claim"] = {
                "token": uuid.uuid4().hex,
                "created_at": time.time(),
            }
        flag_modified(document, "settings")
        db.session.commit()

        if task_name is None:
            return {
                "document": self._safe_document(document.id),
                "queue_job_id": None,
                "state": response_state,
            }

        payload = {
            "document_id": document.id,
            "user_id": self.owner_user_id,
            "mcp_execution_id": normalized_execution_id,
            "mcp_execution_mode": normalized_mode,
        }
        if engine_id:
            payload["engine_id"] = engine_id
        try:
            queue = self._queue()
            queue_job_id = (
                queue.enqueue_once(task_name, payload, job_id=initial_job_id)
                if initial_job_id is not None
                else queue.enqueue(task_name, payload)
            )
            if not queue_job_id:
                raise RuntimeError("Queue did not return a job id.")
            queue_job_id = str(queue_job_id)
            self._persist_queue_marker(document, queue_job_id)
        except Exception as exc:
            if initial_job_id is not None:
                # The application transaction already owns this intent. Polling
                # retries its exact queue ID, even after an uncertain enqueue.
                db.session.rollback()
                return {
                    "document": self._safe_document(document.id),
                    "queue_job_id": initial_job_id,
                    "state": "queued",
                    "dispatch_pending": True,
                }
            self._clear_failed_enqueue(document.id, previous_status)
            if isinstance(exc, DocProcessorMcpServiceError):
                raise
            raise DocProcessorMcpServiceError(
                "Processing could not be queued. Please retry.",
                code="enqueue_failed",
            ) from exc

        return {
            "document": self._safe_document(document.id),
            "queue_job_id": queue_job_id,
            "state": {"completed": "completed", "error": "failed", "cancelled": "cancelled"}.get(
                document.status, response_state
            ),
        }

    def cancel_execution(
        self,
        document_id: str,
        *,
        execution_id: str,
        queue_job_id: str | None,
    ) -> dict:
        """Cancel an exact MCP-bound document run without starting a worker."""

        document = self._document(document_id)
        if document.status in {"completed", "error", "cancelled"}:
            return self.job_projection(document.id)
        observed_status = str(document.status)
        settings = dict(document.settings or {})
        bound_execution_id = str(settings.get("mcp_execution_id") or "").strip()
        if bound_execution_id != str(execution_id or "").strip():
            raise DocProcessorMcpServiceError(
                "The document is not bound to this MCP execution.",
                code="execution_conflict",
            )
        active_job_id = str(settings.get("active_job_id") or "").strip() or None
        expected_job_id = str(queue_job_id or "").strip() or None
        if (
            expected_job_id is not None
            and active_job_id is not None
            and active_job_id != expected_job_id
        ):
            raise DocProcessorMcpServiceError(
                "The document queue binding changed before cancellation.",
                code="queue_job_conflict",
            )
        if active_job_id is not None:
            if self.context is None:
                raise DocProcessorMcpServiceError(
                    "Document Processor queue is unavailable.",
                    code="queue_unavailable",
                )
            try:
                self.context.get_task_queue("doc_processor").request_cancel(
                    active_job_id,
                    requested_by=self.owner_user_id,
                    reason="Cancelled through the MCP execution.",
                )
            except Exception as exc:
                raise DocProcessorMcpServiceError(
                    "Document Processor cancellation could not be signalled.",
                    code="queue_unavailable",
                ) from exc

        settings["progress_msg"] = "Cancelled through MCP."
        for key in (
            "active_job_id",
            "enqueue_claim",
            "mcp_execution_id",
            "mcp_execution_mode",
        ):
            settings.pop(key, None)
        with db.session.no_autoflush:
            changed = db.session.execute(
                update(ProcessedDocument)
                .where(
                    ProcessedDocument.id == document.id,
                    ProcessedDocument.user_id == self.owner_user_id,
                    ProcessedDocument.status == observed_status,
                )
                .values(
                    status="cancelled",
                    error_message=None,
                    settings=settings,
                )
                .execution_options(synchronize_session=False)
            ).rowcount
        if changed != 1:
            db.session.rollback()
            fresh = self._document(document.id)
            if fresh.status in {"completed", "error", "cancelled"}:
                return self.job_projection(fresh.id)
            raise DocProcessorMcpServiceError(
                "The document changed concurrently during cancellation.",
                code="execution_conflict",
            )
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise
        return self.job_projection(document.id)

    def get_document(
        self,
        document_id: str,
        include_output: bool = False,
        expected_revision: str | None = None,
    ) -> dict[str, Any]:
        from .mcp_review import REVIEW_SCHEMA, _revision

        document = self._document(document_id, fresh=True)
        revision = (
            _revision(str(document.output_html))
            if document.status == "completed" and document.output_html
            else None
        )
        if expected_revision is not None and expected_revision != revision:
            raise DocProcessorMcpServiceError(
                "The extraction changed; fetch fresh evidence before editing.",
                code="revision_conflict",
            )
        payload = dict(
            self._safe_document(
                document_id, include_output=include_output, document_record=document
            )
        )
        if revision is not None:
            payload.update({"revision": revision, "review_schema": REVIEW_SCHEMA})
            if include_output:
                payload["output_revision"] = revision
                payload["output_origin"] = "current_completed_record"
        review_kind, deep_path, review_details = self._extract_review_info(document)
        if review_kind is not None:
            payload["review_kind"] = review_kind
            payload["ui_path"] = "/addons/doc_processor/"
            payload["manual_review_path"] = "/addons/doc_processor/"
            payload["review_details"] = review_details
        return payload

    def list_documents(
        self,
        *,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        limit = min(200, max(1, int(limit)))
        offset = max(0, int(offset))
        normalized_status = str(status).strip().lower() if status else None
        query = ProcessedDocument.query.filter_by(user_id=self.owner_user_id)
        if normalized_status:
            query = query.filter(ProcessedDocument.status == normalized_status)
        orm_docs = (
            query.order_by(
                ProcessedDocument.created_at.desc(), ProcessedDocument.id.desc()
            )
            .offset(offset)
            .limit(limit)
            .all()
        )
        client = self._client()
        results: list[dict[str, Any]] = []
        for doc in orm_docs:
            payload = dict(client._source_payload(doc, include_content=False))
            review_kind, _, review_details = self._extract_review_info(doc)
            if review_kind is not None:
                payload["review_kind"] = review_kind
                payload["ui_path"] = "/addons/doc_processor/"
                payload["manual_review_path"] = "/addons/doc_processor/"
                payload["review_details"] = review_details
            payload["has_output"] = bool(doc.output_html)
            results.append(payload)
        return results

    @staticmethod
    def _extract_review_info(
        document: ProcessedDocument,
    ) -> tuple[str | None, str | None, dict[str, Any] | None]:
        if document.status == "reviewing_images":
            review_kind = "images"
            deep_path = f"/addons/doc_processor/?document_id={document.id}&review=images"
            pdf_state = (document.settings or {}).get("pdf_state", {})
            html_parts = pdf_state.get("html_parts", [])
            pages_with_figures: list[int] = []
            figure_count = 0
            for idx, part in enumerate(html_parts):
                if part and "<img" in part:
                    pages_with_figures.append(idx + 1)
                    figure_count += part.count("<img")
            review_details = {
                "kind": "images",
                "ui_path": deep_path,
                "figure_count": figure_count,
                "pages": pages_with_figures,
                "instructions": (
                    f"{figure_count} extracted image(s) on page(s) {pages_with_figures} "
                    "require visual bounding box confirmation in the Carlina workspace before the document can be finalized."
                ),
            }
            return review_kind, deep_path, review_details
        if document.status == "needs_cropping":
            review_kind = "cropping"
            deep_path = f"/addons/doc_processor/?document_id={document.id}&review=crop"
            review_details = {
                "kind": "cropping",
                "ui_path": deep_path,
                "instructions": "PDF page crop margins require visual confirmation in the Carlina workspace before processing can begin.",
            }
            return review_kind, deep_path, review_details
        return None, None, None

    def job_projection(
        self, document_id: str, *, execution_id: str | None = None
    ) -> dict:
        document = self._document(document_id)
        settings = dict(document.settings or {})
        queue_job_id = str(settings.get("active_job_id") or "").strip() or None
        queue_status: dict[str, Any] = {}
        if queue_job_id and self.context is not None:
            try:
                queue_status = dict(
                    self.context.get_task_queue("doc_processor").get_status(
                        queue_job_id
                    )
                    or {}
                )
            except OSError, RuntimeError, TypeError, ValueError:
                queue_status = {}
        raw_queue_state = str(queue_status.get("status") or "").strip().lower()
        mode = str(settings.get("mcp_execution_mode") or "").strip().lower()

        if document.status == "suspended":
            state = "suspended"
        elif document.status in {"needs_cropping", "reviewing_images"}:
            state = "waiting_review"
        elif document.status == "completed":
            state = "completed"
        elif document.status == "cancelled":
            state = "cancelled"
        elif document.status == "error":
            state = "failed"
        elif raw_queue_state in {"completed", "cancelled", "failed"}:
            state = raw_queue_state
        elif queue_status.get("waiting_for_external_work"):
            state = "waiting_external"
        elif raw_queue_state in {"queued", "pending"}:
            state = "queued"
        elif (
            raw_queue_state in {"processing", "running"}
            or document.status in _ACTIVE_DOCUMENT_STATUSES
        ):
            state = "waiting_external" if mode == "passive" else "running"
        else:
            state = "queued"

        review_kind, _, review_details = self._extract_review_info(document)
        review_path = "/addons/doc_processor/" if state == "waiting_review" else None
        projection = {
            "document_id": document.id,
            "execution_id": settings.get("mcp_execution_id") or settings.get("mcp_last_execution_id"),
            "state": state,
            "status": state,
            "queue_job_id": queue_job_id,
            "ui_path": review_path,
            "manual_review_path": review_path,
        }
        if mode == "passive" and queue_job_id and raw_queue_state in {"", "unknown"}:
            execution = db.session.get(McpExecution, settings.get("mcp_execution_id"))
            recovery = (execution.request_json or {}).get("recovery", {}) if execution else {}
            if execution is not None and execution.status == "queued" and recovery:
                from .execution_recovery import validate_delivery

                reason = "queue_dispatch_pending"
                try:
                    valid = validate_delivery(
                        document,
                        {"document_id": document.id, "user_id": document.user_id,
                         "mcp_execution_id": execution.id},
                        queue_job_id, self._stored_source(document),
                    )
                    if not valid:
                        reason = "execution_conflict"
                except Exception as exc:
                    reason = getattr(exc, "code", "source_unavailable")
                projection.update(state="queued", status="queued")
                projection["delivery"] = {
                    "pending": True, "reason": reason,
                    "next_action": "poll_job" if reason == "queue_dispatch_pending" else "resolve_blocker",
                    "call": {"tool": "carlina_job_get", "arguments": {"execution_id": execution.id}},
                }
        if state == "suspended":
            from .execution_recovery import recovery_projection

            execution = db.session.get(McpExecution, settings.get("mcp_execution_id"))
            if execution is not None:
                projection["recovery"] = recovery_projection(document, execution)
        checkpoint = self._checkpoint_projection(document)
        if checkpoint:
            projection["checkpoint"] = checkpoint
        metrics = settings.get("metrics")
        if isinstance(metrics, Mapping) and isinstance(metrics.get("batch_local_timings"), Mapping):
            projection["local_timings"] = {
                stage: {key: values[key] for key in ("calls", "seconds", "max_seconds") if key in values}
                for stage, values in metrics["batch_local_timings"].items()
                if stage in {"packet_preparation", "product_validation"} and isinstance(values, Mapping)
            }
        feedback = metrics.get("batch_validation_error") if isinstance(metrics, Mapping) else None
        if isinstance(feedback, Mapping):
            projection["validation_error"] = deepcopy(dict(feedback))
        if (
            document.status in {"error", "suspended"}
            and (
                document.error_message in {
                    "list-continuation-style-conflict", "list-continuation-ordinal-conflict"
                }
                or (settings.get("mcp_recovery") or {}).get("reason", {}).get("error_code")
                in {"list_continuation_style_conflict", "list_continuation_ordinal_conflict"}
            )
        ):
            projection["assembly_recovery"] = {
                "supported": True,
                "call": {"tool": "carlina_document_recover", "arguments": {
                    "execution_id": projection["execution_id"], "operation": "inspect"
                }},
            }
        receipt = settings.get("mcp_assembly_recovery_receipt")
        if isinstance(receipt, Mapping):
            projection["assembly_recovery_receipt"] = deepcopy(dict(receipt))
            if document.status == "completed" and receipt.get("new_execution_id") == projection["execution_id"]:
                projection["completed_batch_ids"] = [
                    str(batch["batch_id"]) for batch in (settings.get("mcp_batch_plan") or {}).get("batches", [])
                ]
        preparation = self._preparation_projection(document)
        if preparation:
            projection["metadata_preparation"] = preparation
        preparation_timings = self._preparation_timings_projection(document)
        if preparation_timings:
            projection["preparation_timings"] = preparation_timings
        external_work_timing = self._external_work_timing_projection(
            document, execution_id
        )
        if external_work_timing:
            projection["external_work_timing"] = external_work_timing
        pdf_state = settings.get("pdf_state")
        if isinstance(pdf_state, Mapping):
            boundaries = pdf_state.get("batch_boundaries")
            if isinstance(boundaries, Mapping):
                projection["completed_batch_ids"] = sorted(
                    str(batch_id)
                    for batch_id in boundaries
                    if str(batch_id).strip()
                )
        if review_kind is not None:
            projection["review_kind"] = review_kind
            projection["review_details"] = review_details
        return projection

    def export_document(
        self,
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
        """Queue one revision-fenced export for a completed extraction."""

        from .mcp_exports import export_document

        return export_document(
            self,
            document_id,
            export_format=export_format,
            idempotency_key=idempotency_key,
            expected_revision=expected_revision,
            start_h2_on_new_page=start_h2_on_new_page,
            epub_cover=epub_cover,
            epub_options=epub_options,
            epub_review_hash=epub_review_hash,
            **(
                {"epub_image_mode": epub_image_mode}
                if epub_image_mode is not _EPUB_IMAGE_MODE_UNSET
                else {}
            ),
        )

    def epub_preflight(
        self,
        document_id: str,
        *,
        epub_options: object = None,
        epub_cover: object = None,
        expected_revision: str | None = None,
        epub_image_mode: object = "jpeg",
    ) -> dict[str, object]:
        """Read-only EPUB diagnostics for one owned document revision."""

        from .mcp_exports import epub_preflight

        return epub_preflight(
            self,
            document_id,
            epub_options=epub_options,
            epub_cover=epub_cover,
            expected_revision=expected_revision,
            epub_image_mode=epub_image_mode,
        )

    def epub_edition_get(self, document_id: str) -> dict[str, Any]:
        from .epub_editions import get_epub_edition

        return get_epub_edition(owner_id=self.owner_user_id, document_id=document_id)

    def epub_edition_save(self, document_id: str, *, recipe: object, expected_version: int, expected_source_revision: str) -> dict[str, Any]:
        from .epub_editions import save_epub_edition

        return save_epub_edition(owner_id=self.owner_user_id, document_id=document_id, recipe=recipe, expected_version=expected_version, expected_source_revision=expected_source_revision)

    def epub_edition_clear(self, document_id: str, *, expected_version: int) -> dict[str, Any]:
        from .epub_editions import clear_epub_edition

        return clear_epub_edition(owner_id=self.owner_user_id, document_id=document_id, expected_version=expected_version)

    def export_document_get(
        self,
        document_id: str,
        operation_id: str,
        *,
        include_content: bool = False,
        offset: int = 0,
        limit: int = 65_536,
        expected_sha256: str | None = None,
    ) -> dict[str, Any]:
        """Read safe export status or one bounded, checksum-witnessed chunk."""

        from .mcp_exports import export_document_get

        return export_document_get(
            self,
            document_id,
            operation_id,
            include_content=include_content,
            offset=offset,
            limit=limit,
            expected_sha256=expected_sha256,
        )


__all__ = [
    "SUPPORTED_DOCUMENT_EXTENSIONS",
    "DocProcessorMcpError",
    "DocProcessorMcpService",
    "DocProcessorMcpServiceError",
]
