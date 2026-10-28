import hashlib
import json
import logging
import re
import secrets
import time
import uuid
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, TypedDict, cast

from bs4 import BeautifulSoup
from flask import (
    Blueprint,
    Response,
    current_app,
    g,
    jsonify,
    render_template,
    request,
    send_file,
    url_for,
)
from flask.typing import ResponseReturnValue
from flask_login import current_user, login_required
from sqlalchemy import func, inspect
from sqlalchemy.orm.attributes import flag_modified
from werkzeug.utils import secure_filename

from carlina.core.language import normalize_language_tag
from carlina.core.semantic_html import SAFE_LANGUAGE_RE
from carlina.core.tool_risk import tool_is_read_only
from carlina.core.utc import as_utc, utc_iso, utc_now_db
from iarf_assistant.core.addons.capability_runtime import CapabilityUseError
from iarf_assistant.core.model_capabilities import resolve_model_capabilities
from iarf_assistant.extensions import db
from iarf_assistant.models import LLMCost, User
from iarf_assistant.pages import render_frontend_shell

from ...core.frontend_orchestration import get_runtime_core_frontend
from ...core.model_readiness import ModelNotReadyError, require_model_ready
from . import scholarly_review
from .shared_glossary import SharedGlossaryError
from .block_continuation import (
    TRANSLATION_SCOPE_CONTRACT_VERSION,
    normalize_translation_scope,
    snapshot_protected_targets,
)
from .block_contract import (
    DOCUMENT_BLOCK_ENGINE_CONTRACT,
    LEGACY_ENGINE_CONTRACT,
    AttestedDocumentSnapshot,
    BlockContractError,
    canonical_json,
)
from .block_export import (
    BlockExportError,
    build_semantic_export_request,
    build_semantic_preview_html,
    merge_project_export_metadata,
    normalize_export_metadata,
    semantic_export_review_hash,
    validate_epub_preflight_result,
)
from .block_notes import normalize_annotation_profile
from .export_cover import ExportCoverError, pin_export_metadata_cover
from .export_metadata import ExportMetadataError
from .block_persistence import (
    effective_engine_contract,
    plan_and_persist_source_snapshot,
)
from .block_planner import (
    DEFAULT_TRANSLATION_MAX_BLOCK_CHARS,
    MAX_TRANSLATION_MAX_BLOCK_CHARS,
    MIN_TRANSLATION_MAX_BLOCK_CHARS,
    TRANSLATION_MAX_BLOCK_CHARS_SETTING,
)
from .block_translation import (
    DEFAULT_TRANSLATION_BATCH_INPUT_TOKENS,
    DEFAULT_TRANSLATION_BATCH_MAX_BLOCKS,
    MAX_TRANSLATION_BATCH_INPUT_TOKENS,
    MAX_TRANSLATION_BATCH_MAX_BLOCKS,
    MIN_TRANSLATION_BATCH_INPUT_TOKENS,
    MIN_TRANSLATION_BATCH_MAX_BLOCKS,
    derive_window_profile,
)
from .block_workflow_config import (
    freeze_block_workflow_config,
    normalize_translation_context_mode,
    persist_block_workflow_config,
    rehydrate_block_workflow_config,
    resolve_translation_context_mode,
    translation_context_policy,
)
from .block_workspace_api import (
    MAX_CANDIDATE_JSON_BYTES,
    MAX_GLOSSARY_JSON_BYTES,
    MAX_WORKSPACE_LIMIT,
    BlockWorkspaceIntegrityError,
    build_block_glossary_payload,
    build_workspace_payload,
    current_block_projection,
    is_document_block_engine,
    load_block_source_plan,
    persist_human_glossary_entry,
    persist_manual_candidate,
    require_segment_engine,
)
from .document_review import DocumentReviewError
from .qa_diagnostics import QaDiagnosticsError, qa_diagnostics
from .project_deletion import (
    cleanup_deleted_project_vectors,
    collect_project_deletion_artifacts,
    delete_project_aggregate,
)
from .models import (
    GlossaryTerm,
    TranslationBlockWorkflowConfig,
    TranslationNote,
    TranslationOperation,
    TranslationProject,
    TranslationRevision,
    TranslationSegment,
    TranslationSourcePlan,
)
from .operations import (
    TranslationOperationActiveConflict,
    TranslationOperationIdempotencyConflict,
    TranslationOperationTransitionConflict,
    TranslationProjectIdempotencyConflict,
    attach_queue_job,
    create_operation,
    create_project_idempotently,
    finish_operation,
    get_operation,
    reconcile_expired_active_operation,
    request_cancellation,
    serialize_operation,
)
from .rendering import (
    render_project_html,
    translated_subtitle_cues,
    translated_subtitle_text,
)
from .research_selection import (
    exact_research_tool_catalog,
    freeze_research_selections,
    normalize_research_tool_selections,
)
from .scholarly_http import (
    IDEMPOTENCY_HEADER,
    project_access_required,
    register_scholarly_routes,
)
from .scholarly_wire import retranslation_request as scholarly_retranslation_request
from .source_plan_correction_persistence import (
    SourcePlanCorrectionError,
    correct_active_source_plan,
)
from .source_providers import (
    resolve_capability_provider,
    resolve_source_provider,
    source_reference,
)
from .source_snapshot import (
    MAX_SOURCE_SNAPSHOT_BYTES,
    MAX_SUBTITLE_UNIT_CHARS,
    MAX_SUBTITLE_UNITS,
    encoded_document_snapshot_html,
    encoded_subtitle_snapshot,
    subtitle_project_metadata,
    subtitle_render_settings,
    subtitle_snapshot_entry_bytes,
    subtitle_unit_metadata,
)
from .target_persistence import render_persisted_target_document
from .translation_memory import translation_memory_store
from .translation_units import normalize_translation_unit_mode
from .window_execution import resolve_execution_profile

DOCUMENT_SOURCE_REQUIREMENT = "document_processor"
DOCUMENT_SOURCE_CAPABILITY = "document.processing"
SEMANTIC_EXPORT_REQUIREMENT = "semantic_export"
SEMANTIC_EXPORT_CAPABILITY = "document.semantic_export"
SEMANTIC_EXPORT_REFERENCE_KEY = "semantic_export_reference"
SUBTITLE_SOURCE_REQUIREMENT = "subtitle_source"
SUBTITLE_SOURCE_CAPABILITY = "subtitles.authoring"
logger = logging.getLogger(__name__)
ACTIVE_PROJECT_STATUSES = {
    "analyzing_structure",
    "analyzing_terminology",
    "processing_pdf",
    "translating",
}
MAX_CHUNK_SIZE_CHARS = 20_000
MIN_CHUNK_SIZE_CHARS = 500
_TRANSLATION_BATCH_INPUT_WHOLE_NUMBER_ERROR = (
    "Translation batch input budget must be a whole number."
)
_TRANSLATION_BATCH_BLOCK_WHOLE_NUMBER_ERROR = (
    "Translation batch block limit must be a whole number."
)
_TRANSLATION_BATCH_BLOCK_RANGE_ERROR = (
    "Translation batch block limit must be between "
    f"{MIN_TRANSLATION_BATCH_MAX_BLOCKS:,} and "
    f"{MAX_TRANSLATION_BATCH_MAX_BLOCKS:,} blocks."
)
MAX_MODEL_ID_LENGTH = 255
MAX_IDEMPOTENCY_KEY_LENGTH = 128
MAX_WORKING_MEMORY_CHARS = 50_000
MAX_RAG_COLLECTION_IDS = 50
MAX_RAG_COLLECTION_ID_CHARS = 128
MAX_GLOSSARY_CSV_BYTES = 2_000_000
MAX_GLOSSARY_CSV_ROWS = 10_000
MAX_PROJECT_GLOSSARY_TERMS = 20_000
MAX_GLOSSARY_TERM_CHARS = 255
MAX_GLOSSARY_NOTES_CHARS = 10_000
MAX_RESEARCH_PLUGIN_IDS = 20
RESEARCH_PLUGIN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
BLOCK_RESEARCH_BUDGET = {
    "max_model_calls": 128,
    "max_tool_calls": 64,
    "output_tokens": 128_000,
}
MAX_PROJECT_NAME_CHARS = 255
MAX_LANGUAGE_LABEL_CHARS = 50
MAX_SOURCE_DOCUMENT_ID_CHARS = 36
MAX_SOURCE_PROVIDER_ADDON_ID_CHARS = 128
SUPPORTED_CREATE_SOURCE_TYPES = frozenset({"document", "subtitle_studio"})


class SourceUnavailableError(ValueError):
    """Safe public error for failures at the selected source boundary."""


def _guest_forbidden(message: str):
    return jsonify({"error": message}), 403


def _segment_surface_guard(project):
    """Keep document-block projects out of timed subtitle segment surfaces."""

    try:
        require_segment_engine(project)
    except BlockWorkspaceIntegrityError as exc:
        engine = getattr(project, "engine_contract", None)
        return (
            jsonify(
                {
                    "error": (
                        "This project is not a timed Subtitle Studio translation; "
                        "the segment surface is unavailable."
                    ),
                    "code": "engine_contract_mismatch",
                    "engine_contract": engine,
                    "workspace_url": url_for(
                        "translator.get_block_workspace", project_id=project.id
                    ),
                    "integrity_code": exc.code
                    if exc.code != "segment-engine-surface"
                    else None,
                }
            ),
            409,
        )
    return None


def _create_text_field(
    data, field: str, *, required: bool, maximum: int, default: str | None = None
) -> str | None:
    """Normalize a small create-project field before provider or DB work.

    ``TranslationProject`` persists name and language labels at these limits;
    rejecting malformed values here keeps provider calls and error handling out
    of the business of coercing arbitrary JSON values.
    """

    value = data.get(field, default)
    if value is None:
        if required:
            raise ValueError(f"{field} is required.")
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be text.")
    normalized = value.strip()
    if required and not normalized:
        raise ValueError(f"{field} is required.")
    if len(normalized) > maximum:
        raise ValueError(f"{field} must be at most {maximum} characters.")
    return normalized


def _project_creation_request_payload(
    *,
    name: str,
    source_lang: str,
    target_lang: str,
    source_type: str,
    source_id: str | int | None,
    settings: dict,
    engine_contract: str = DOCUMENT_BLOCK_ENGINE_CONTRACT,
) -> dict:
    """Canonical owner-scoped create identity, excluding mutable provider data."""

    settings_projection = {
        key: settings.get(key)
        for key in (
            "translator_model",
            "researcher_model",
            "analyzer_model",
            "enable_pre_analysis",
            "enable_research",
            "enable_translator_notes",
            "annotation_profile",
            "research_plugin_ids",
            "research_tool_selections",
            "chunk_size_chars",
            "export_metadata",
            "translation_context_mode",
        )
    }
    if engine_contract == DOCUMENT_BLOCK_ENGINE_CONTRACT:
        settings_projection["translation_unit_mode"] = normalize_translation_unit_mode(
            settings.get("translation_unit_mode")
        )
    return {
        "name": name,
        "source_lang": source_lang,
        "target_lang": target_lang,
        "source_type": source_type,
        "source_id": str(source_id or ""),
        "engine_contract": engine_contract,
        "settings": settings_projection,
    }


def _project_creation_conflict_response(exc: TranslationProjectIdempotencyConflict):
    return (
        jsonify(
            {
                "error": "The idempotency key is already bound to a different project request.",
                "project": _serialize_project(exc.project),
            }
        ),
        409,
    )


def _project_creation_reuse_response(project: TranslationProject):
    payload = _serialize_project(project)
    operation = _latest_operation(project)
    if operation is not None:
        payload["operation"] = _safe_operation_projection(operation)
        payload["latest_operation"] = _safe_operation_projection(operation)
        payload["job_id"] = operation.queue_job_id
        if operation.queue_job_id:
            payload["status_url"] = _job_status_url(project.id, operation.queue_job_id)
    return jsonify(payload), 200


def _validate_create_project_request(data) -> tuple[str, str, str, str, str | None]:
    """Validate the route-owned creation envelope without resolving sources."""

    if not hasattr(data, "get"):
        raise ValueError("Project creation must be a JSON object or form data.")
    name = _create_text_field(
        data, "name", required=True, maximum=MAX_PROJECT_NAME_CHARS
    )
    source_lang = _create_text_field(
        data,
        "source_lang",
        required=False,
        maximum=MAX_LANGUAGE_LABEL_CHARS,
        default="Auto-detect",
    )
    # An omitted source language means automatic detection, but an explicitly
    # blank one should not be stored as a meaningless label.
    source_lang = source_lang or "Auto-detect"
    if source_lang.casefold() in {"auto", "auto-detect", "detect"}:
        source_lang = "Auto-detect"
    else:
        source_lang = normalize_language_tag(source_lang)
        if not source_lang or not SAFE_LANGUAGE_RE.fullmatch(source_lang):
            raise ValueError(
                "source_lang must be Auto-detect, a language name, or a BCP 47 tag."
            )
    target_lang = _create_text_field(
        data, "target_lang", required=True, maximum=MAX_LANGUAGE_LABEL_CHARS
    )
    target_lang = normalize_language_tag(target_lang)
    if not target_lang or not SAFE_LANGUAGE_RE.fullmatch(target_lang):
        raise ValueError("target_lang must be a language name or a BCP 47 tag.")
    raw_source_type = data.get("source_type")
    if raw_source_type is None:
        source_type = (
            "subtitle_studio" if data.get("subtitle_project_id") else "document"
        )
    elif not isinstance(raw_source_type, str):
        raise ValueError("source_type must be text.")
    else:
        source_type = raw_source_type.strip()
    if source_type not in SUPPORTED_CREATE_SOURCE_TYPES:
        raise ValueError("source_type must be document or subtitle_studio.")

    document_id = _create_text_field(
        data,
        "document_id",
        required=False,
        maximum=MAX_SOURCE_DOCUMENT_ID_CHARS,
    )
    # These optional identifiers are not trusted source selection inputs: the
    # selected provider is still resolved by the requirement boundary.  Bound
    # them nevertheless so malformed requests cannot become log/queue baggage.
    for field in ("source_document_id", "source_provider_addon_id"):
        maximum = (
            MAX_SOURCE_DOCUMENT_ID_CHARS
            if field == "source_document_id"
            else MAX_SOURCE_PROVIDER_ADDON_ID_CHARS
        )
        _create_text_field(data, field, required=False, maximum=maximum)
    return name, source_lang, target_lang, source_type, document_id


def _requested_engine_contract(data, source_type: str) -> str:
    """Select the sole supported engine for each source product.

    Document translation is block-only. Subtitle Studio retains its timed
    segment runtime as a separate product implementation, not as a document
    compatibility choice.
    """

    if source_type == "subtitle_studio":
        return LEGACY_ENGINE_CONTRACT
    raw = data.get("engine_contract", DOCUMENT_BLOCK_ENGINE_CONTRACT)
    if not isinstance(raw, str):
        raise ValueError("engine_contract must be text.")
    engine = raw.strip()
    if engine != DOCUMENT_BLOCK_ENGINE_CONTRACT:
        raise ValueError(
            "Document translation requires engine_contract=document-block-v1."
        )
    return DOCUMENT_BLOCK_ENGINE_CONTRACT


def _validate_split_partition(project, segment, part1: str, part2: str) -> None:
    """Prove a structural split preserves the canonical source exactly.

    Trimmed editor values may normalize only the whitespace at their boundary.
    Everything else must remain an exact ordered partition of the source.  A
    missing or duplicate placeholder would make rendered output ambiguous, so
    fail before any subtitle-provider or database mutation.
    """

    placeholder = f"{{{{{segment.id}}}}}"
    skeleton = getattr(project, "skeleton_html", None)
    if not isinstance(skeleton, str) or skeleton.count(placeholder) != 1:
        raise ValueError(
            "Source structure is inconsistent; re-segment before splitting."
        )
    source = str(segment.original_text or "").strip()
    if BeautifulSoup(source, "html.parser").find(True) is not None:
        raise ValueError(
            "Segments containing inline markup cannot be split safely; "
            "adjust the source structure and re-segment instead."
        )
    exact_partition = source == part1 + part2
    middle = source[len(part1) : len(source) - len(part2)] if part2 else ""
    whitespace_partition = (
        source.startswith(part1)
        and source.endswith(part2)
        and bool(middle)
        and middle.isspace()
    )
    if not source or not (exact_partition or whitespace_partition):
        raise ValueError(
            "Split parts must faithfully partition the original source text."
        )


def _cached_source_provider(
    requirement_id: str,
    capability: str,
    user_id: int,
    reference: dict | None = None,
):
    stored = dict(reference or {})
    cache_key = (
        requirement_id,
        int(user_id),
        str(stored.get("provider_addon_id") or ""),
        str(stored.get("provider_capability_id") or ""),
    )
    cache = getattr(g, "_translator_source_providers", None)
    if cache is None:
        cache = {}
        g._translator_source_providers = cache
    if cache_key not in cache:
        cache[cache_key] = resolve_source_provider(
            blueprint.addon_context,
            requirement_id=requirement_id,
            capability=capability,
            user_id=int(user_id),
            reference=stored,
        )
    return cache[cache_key]


def _document_source_provider(user_id: int, settings: dict | None = None):
    return _cached_source_provider(
        DOCUMENT_SOURCE_REQUIREMENT,
        DOCUMENT_SOURCE_CAPABILITY,
        user_id,
        source_reference(settings, capability=DOCUMENT_SOURCE_CAPABILITY),
    )


def _subtitle_source_provider(user_id: int, settings: dict | None = None):
    return _cached_source_provider(
        SUBTITLE_SOURCE_REQUIREMENT,
        SUBTITLE_SOURCE_CAPABILITY,
        user_id,
        source_reference(settings, capability=SUBTITLE_SOURCE_CAPABILITY),
    )


def _semantic_export_provider(user_id: int, settings: dict | None = None):
    return resolve_capability_provider(
        blueprint.addon_context,
        requirement_id=SEMANTIC_EXPORT_REQUIREMENT,
        capability=SEMANTIC_EXPORT_CAPABILITY,
        user_id=int(user_id),
        settings=settings,
        setting_key=SEMANTIC_EXPORT_REFERENCE_KEY,
    )


def _source_sentence_splitter(user_id: int, settings: dict):
    """Resolve strict segmentation only when the owner selected aligned mode."""

    if settings.get("translation_unit_mode") != "aligned_sentences":
        return None
    try:
        provider = resolve_capability_provider(
            blueprint.addon_context,
            requirement_id="sentence_segmentation",
            capability="text.segmentation",
            user_id=int(user_id),
            settings=settings,
            setting_key="sentence_segmenter_reference",
        )
    except Exception as exc:
        raise SourceUnavailableError(
            "Sentence alignment requires a working Sentence Segmenter. "
            "Check its setup and retry, or choose block mode."
        ) from exc
    settings["sentence_segmenter_reference"] = provider.provider_reference()

    def split_projection(projection: str):
        return provider.client.split_text(projection, raise_on_failure=True)

    return split_projection


def _get_document_for_project(project: TranslationProject) -> dict | None:
    if not project.document_id:
        return None
    provider = _document_source_provider(project.user_id, project.settings)
    return provider.client.get_document(project.document_id)


def _translator_queue():
    return blueprint.addon_context.get_task_queue("translator")


def _addon_manager():
    context = getattr(blueprint, "addon_context", None)
    manager = getattr(context, "_addon_manager", None)
    return manager or getattr(current_app, "addon_manager", None)


def _research_provider_catalog() -> dict[str, dict]:
    """List only active/degraded add-ons with registered read-only tools."""

    manager = _addon_manager()
    if manager is None:
        return {}
    providers: dict[str, dict] = {}
    for record in manager.registries.tools.list():
        state = str(getattr(getattr(record, "status", None), "state", "")).lower()
        if state not in {"active", "degraded"} or not tool_is_read_only(record):
            continue
        addon_id = str(getattr(record, "addon_id", "")).strip()
        if not RESEARCH_PLUGIN_ID_RE.fullmatch(addon_id):
            continue
        addon = manager.plugins.get(addon_id)
        manifest = getattr(addon, "manifest", {}) if addon is not None else {}
        provider = providers.setdefault(
            addon_id,
            {
                "id": addon_id,
                "name": str(manifest.get("name") or addon_id),
                "description": str(manifest.get("description") or ""),
                "tools": [],
            },
        )
        function = (getattr(record, "function_schema", {}) or {}).get("function", {})
        provider["tools"].append(
            {
                "id": str(getattr(record, "id", "")),
                "name": str(function.get("name") or getattr(record, "name", "")),
                "description": str(
                    function.get("description") or getattr(record, "name", "")
                ),
            }
        )
    for provider in providers.values():
        provider["tools"].sort(key=lambda tool: (tool["name"], tool["id"]))
    return dict(sorted(providers.items()))


def _validate_research_plugin_ids(settings: dict) -> None:
    raw = settings.get("research_plugin_ids", [])
    if raw is None:
        raw = []
    if not isinstance(raw, list) or len(raw) > MAX_RESEARCH_PLUGIN_IDS:
        raise ValueError("research_plugin_ids must contain at most 20 provider IDs.")
    ids = []
    for value in raw:
        if not isinstance(value, str):
            raise ValueError("research_plugin_ids must contain provider ID strings.")
        addon_id = value.strip()
        if not RESEARCH_PLUGIN_ID_RE.fullmatch(addon_id):
            raise ValueError("research_plugin_ids contains an invalid provider ID.")
        ids.append(addon_id)
    if len(ids) != len(set(ids)):
        raise ValueError("research_plugin_ids must not contain duplicates.")
    ids.sort()
    if settings.get("enable_research") and not ids:
        raise ValueError(
            "Select at least one read-only research provider when research is enabled."
        )
    # Saved projects must be able to turn research off even after a provider
    # disappears.  Availability is an execution/enabling boundary, not a
    # reason to strand a user's otherwise valid settings.
    if settings.get("enable_research"):
        catalog = _research_provider_catalog()
        unavailable = [addon_id for addon_id in ids if addon_id not in catalog]
        if unavailable:
            raise ValueError(
                "Selected research providers are unavailable or not read-only."
            )
    settings["research_plugin_ids"] = ids


def _safe_operation_projection(operation, *, guest: bool = False) -> dict:
    """Keep durable job identity useful without leaking an owner's identity."""

    payload = serialize_operation(operation)
    if guest:
        payload.pop("owner_user_id", None)
        payload.pop("queue_job_id", None)
        payload.pop("source_content_hash", None)
    return payload


def _latest_operation(project: TranslationProject):
    state = inspect(project, raiseerr=False)
    if state is None or getattr(state, "transient", False):
        operations = getattr(project, "operations", ()) or ()
        return operations[0] if operations else None
    return (
        TranslationOperation.query.filter_by(project_id=project.id)
        .order_by(
            TranslationOperation.created_at.desc(),
            TranslationOperation.id.desc(),
        )
        .first()
    )


def _project_status_payload(project: TranslationProject) -> dict[str, object]:
    operation = _latest_operation(project)
    return {
        "contract": "translator-project-status-v1",
        "project_id": str(project.id),
        "status": str(project.status),
        "latest_operation": (
            _safe_operation_projection(
                operation,
                guest=bool(getattr(g, "is_guest", False)),
            )
            if operation is not None
            else {}
        ),
    }


def _operation_conflict_response(operation, *, message: str | None = None):
    guest = bool(getattr(g, "is_guest", False))
    return (
        jsonify(
            {
                "error": message
                or "This project already has an operation in progress.",
                "operation": _safe_operation_projection(operation, guest=guest),
                "latest_operation": _safe_operation_projection(operation, guest=guest),
                "status_url": (
                    _job_status_url(operation.project_id, operation.queue_job_id)
                    if operation.queue_job_id and not guest
                    else None
                ),
            }
        ),
        409,
    )


def _active_job_response(project: TranslationProject):
    """Compatibility wrapper; durable operations, not status strings, fence work."""

    operation = reconcile_expired_active_operation(project.id, project.user_id)
    if operation is None:
        return None
    return _operation_conflict_response(operation)


def _idempotency_key() -> str | None:
    value = request.headers.get("Idempotency-Key")
    if value is None:
        return None
    value = str(value).strip()
    if not value:
        return None
    if len(value) > MAX_IDEMPOTENCY_KEY_LENGTH:
        raise ValueError(
            f"Idempotency-Key must be at most {MAX_IDEMPOTENCY_KEY_LENGTH} characters."
        )
    return value


def _model_error(exc: ModelNotReadyError, *, status: int = 400):
    return (
        jsonify(
            {
                "error": str(exc),
                "reason": exc.reason,
                "setup_url": "/settings/models",
            }
        ),
        status,
    )


def _normalise_model_id(value, *, label: str, required: bool) -> str:
    normalized = str(value or "").strip()
    if required and not normalized:
        raise ValueError(f"A {label} model is required.")
    if len(normalized) > MAX_MODEL_ID_LENGTH:
        raise ValueError(
            f"{label.capitalize()} model identifiers must be at most 255 characters."
        )
    return normalized


def _coerce_bool(value, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off", ""}:
            return False
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    raise ValueError("Boolean settings must be true or false.")


def _coerce_chunk_size(value) -> int:
    try:
        chunk_size = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Chunk size must be a whole number.") from exc
    if not MIN_CHUNK_SIZE_CHARS <= chunk_size <= MAX_CHUNK_SIZE_CHARS:
        raise ValueError(
            f"Chunk size must be between {MIN_CHUNK_SIZE_CHARS:,} and {MAX_CHUNK_SIZE_CHARS:,} characters."
        )
    return chunk_size


def _coerce_translation_batch_input_tokens(value) -> int:
    if isinstance(value, bool) or (isinstance(value, float) and not value.is_integer()):
        raise ValueError(_TRANSLATION_BATCH_INPUT_WHOLE_NUMBER_ERROR)
    try:
        tokens = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(_TRANSLATION_BATCH_INPUT_WHOLE_NUMBER_ERROR) from exc
    if (
        not MIN_TRANSLATION_BATCH_INPUT_TOKENS
        <= tokens
        <= MAX_TRANSLATION_BATCH_INPUT_TOKENS
    ):
        raise ValueError(
            "Translation batch input budget must be between "
            f"{MIN_TRANSLATION_BATCH_INPUT_TOKENS:,} and "
            f"{MAX_TRANSLATION_BATCH_INPUT_TOKENS:,} tokens."
        )
    return tokens


def _coerce_translation_batch_max_blocks(value) -> int:
    if isinstance(value, bool) or (isinstance(value, float) and not value.is_integer()):
        raise ValueError(_TRANSLATION_BATCH_BLOCK_WHOLE_NUMBER_ERROR)
    try:
        blocks = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(_TRANSLATION_BATCH_BLOCK_WHOLE_NUMBER_ERROR) from exc
    if (
        not MIN_TRANSLATION_BATCH_MAX_BLOCKS
        <= blocks
        <= MAX_TRANSLATION_BATCH_MAX_BLOCKS
    ):
        raise ValueError(_TRANSLATION_BATCH_BLOCK_RANGE_ERROR)
    return blocks


def _coerce_translation_max_block_chars(value) -> int:
    if isinstance(value, bool) or (isinstance(value, float) and not value.is_integer()):
        raise ValueError("translation_max_block_chars must be a whole number.")
    try:
        chars = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("translation_max_block_chars must be a whole number.") from exc
    if not MIN_TRANSLATION_MAX_BLOCK_CHARS <= chars <= MAX_TRANSLATION_MAX_BLOCK_CHARS:
        raise ValueError(
            "translation_max_block_chars must be between "
            f"{MIN_TRANSLATION_MAX_BLOCK_CHARS:,} and {MAX_TRANSLATION_MAX_BLOCK_CHARS:,}."
        )
    return chars


def _validate_chat_phase_model_settings(
    settings: dict, *, user_id: int, require_analysis: bool = False
) -> None:
    """Normalize and prove the three independently conditional chat models."""

    translator_model = _normalise_model_id(
        settings.get("translator_model"), label="translator", required=True
    )
    settings["translator_model"] = translator_model
    require_model_ready(translator_model, user_id=user_id, required_model_type="chat")

    pre_analysis = _coerce_bool(settings.get("enable_pre_analysis"), default=False)
    settings["enable_pre_analysis"] = pre_analysis
    analyzer_model = _normalise_model_id(
        settings.get("analyzer_model"),
        label="analyzer",
        required=pre_analysis or require_analysis,
    )
    settings["analyzer_model"] = analyzer_model
    if analyzer_model and (pre_analysis or require_analysis):
        require_model_ready(analyzer_model, user_id=user_id, required_model_type="chat")

    research_enabled = _coerce_bool(settings.get("enable_research"), default=False)
    settings["enable_research"] = research_enabled
    researcher_model = _normalise_model_id(
        settings.get("researcher_model"),
        label="researcher",
        required=research_enabled,
    )
    settings["researcher_model"] = researcher_model
    if research_enabled:
        require_model_ready(
            researcher_model, user_id=user_id, required_model_type="chat"
        )


def _validate_model_settings(
    settings: dict, *, user_id: int, require_analysis: bool = False
) -> None:
    """Validate the legacy segment workflow's model and discovery settings."""

    # Translation-unit selection is a document-block concern.  Strip any
    # stale value from historical subtitle settings rather than letting a
    # shared settings payload influence the timed-segment runtime.
    settings.pop("translation_unit_mode", None)
    settings.pop(TRANSLATION_MAX_BLOCK_CHARS_SETTING, None)

    settings["enable_translator_notes"] = _coerce_bool(
        settings.get("enable_translator_notes"), default=False
    )
    execution_mode = str(settings.get("execution_mode") or "managed").strip().lower()
    if execution_mode == "passive":
        pre_analysis = _coerce_bool(settings.get("enable_pre_analysis"), default=False)
        if require_analysis or pre_analysis:
            raise ValueError(
                "Passive pre-analysis must be run in a separately supported execution later."
            )
        settings["enable_pre_analysis"] = False
        settings["enable_research"] = _coerce_bool(
            settings.get("enable_research"), default=False
        )
        _validate_research_plugin_ids(settings)
        settings["chunk_size_chars"] = _coerce_chunk_size(
            settings.get("chunk_size_chars", 2500)
        )
        settings.pop("translation_batch_input_tokens", None)
        settings.pop("translation_batch_max_blocks", None)
        return
    _validate_chat_phase_model_settings(
        settings, user_id=user_id, require_analysis=require_analysis
    )
    _validate_research_plugin_ids(settings)

    settings["chunk_size_chars"] = _coerce_chunk_size(
        settings.get("chunk_size_chars", 2500)
    )
    settings.pop("translation_batch_input_tokens", None)
    settings.pop("translation_batch_max_blocks", None)


def _validate_block_model_settings(settings: dict, *, user_id: int) -> None:
    """Validate block settings without consulting legacy research discovery."""

    settings["translation_context_mode"] = normalize_translation_context_mode(
        settings.get("translation_context_mode", "self_contained")
    )
    try:
        settings["translation_unit_mode"] = normalize_translation_unit_mode(
            settings.get("translation_unit_mode")
        )
    except BlockContractError as exc:
        raise ValueError(
            "translation_unit_mode must be block or aligned_sentences."
        ) from exc

    legacy_notes = _coerce_bool(settings.get("enable_translator_notes"), default=False)
    try:
        settings["annotation_profile"] = normalize_annotation_profile(
            settings.get("annotation_profile"), legacy_notes
        )
    except BlockContractError as exc:
        raise ValueError("Choose a supported translator-note profile.") from exc
    settings["enable_translator_notes"] = settings["annotation_profile"] != "none"
    execution_mode = str(settings.get("execution_mode") or "managed").strip().lower()
    if execution_mode not in {"managed", "passive"}:
        raise ValueError("execution_mode must be managed or passive.")
    settings["execution_mode"] = execution_mode
    if execution_mode == "passive":
        if _coerce_bool(settings.get("enable_pre_analysis"), default=False):
            raise ValueError(
                "Passive pre-analysis must be run in a separately supported execution later."
            )
        if _coerce_bool(settings.get("enable_research"), default=False):
            raise ValueError(
                "Passive translation currently supports translation only; research is unavailable."
            )
        settings.update(
            {
                "translator_model": "",
                "analyzer_model": "",
                "researcher_model": "",
                "enable_pre_analysis": False,
                "enable_research": False,
            }
        )
    else:
        _validate_chat_phase_model_settings(settings, user_id=user_id)
    settings["translation_batch_input_tokens"] = _coerce_translation_batch_input_tokens(
        settings.get(
            "translation_batch_input_tokens",
            DEFAULT_TRANSLATION_BATCH_INPUT_TOKENS,
        )
    )
    settings["translation_batch_max_blocks"] = _coerce_translation_batch_max_blocks(
        settings.get(
            "translation_batch_max_blocks",
            DEFAULT_TRANSLATION_BATCH_MAX_BLOCKS,
        )
    )
    if settings["translation_unit_mode"] == "aligned_sentences":
        settings[TRANSLATION_MAX_BLOCK_CHARS_SETTING] = (
            _coerce_translation_max_block_chars(
                settings.get(
                    TRANSLATION_MAX_BLOCK_CHARS_SETTING,
                    DEFAULT_TRANSLATION_MAX_BLOCK_CHARS,
                )
            )
        )
    else:
        settings.pop(TRANSLATION_MAX_BLOCK_CHARS_SETTING, None)
    settings.pop("chunk_size_chars", None)
    if not settings["enable_research"]:
        return
    if not settings.get("enable_pre_analysis"):
        raise ValueError(
            "Document-block research requires preliminary terminology analysis."
        )
    manager = _addon_manager()
    if manager is None:
        raise ValueError("The exact research-tool catalog is unavailable.")
    selections = normalize_research_tool_selections(
        manager,
        int(user_id),
        settings.get("research_tool_selections", ()),
    )
    if not selections:
        raise ValueError(
            "Select at least one exact research tool, provider, and connection."
        )
    settings["research_tool_selections"] = [dict(item) for item in selections]


def _frozen_block_research(settings: dict, *, user_id: int) -> dict | None:
    if not settings.get("enable_research"):
        return None
    manager = _addon_manager()
    if manager is None:
        raise BlockContractError("research-tool-catalog")
    frozen = freeze_research_selections(
        manager,
        int(user_id),
        settings.get("research_tool_selections", ()),
    )
    declarations = [dict(item) for item in frozen["tool_declarations"]]
    if not declarations:
        raise BlockContractError("research-tool-selection")
    capability = resolve_model_capabilities(
        str(settings.get("researcher_model") or ""),
        user_id=int(user_id),
    )
    budget = dict(BLOCK_RESEARCH_BUDGET)
    budget["output_tokens"] = min(
        budget["output_tokens"],
        capability.max_output_tokens,
    )
    return {
        "budget": budget,
        "tool_declarations": declarations,
        "selected_provider_ids": sorted(
            {str(item["provider_id"]) for item in declarations}
        ),
        "selected_addon_ids": sorted({str(item["addon_id"]) for item in declarations}),
    }


def _block_translation_request_payload(
    project: TranslationProject,
    source_plan: TranslationSourcePlan,
    settings: dict,
    *,
    translation_scope: Mapping[str, object],
    translation_reasoning: str | None,
    context_mode: str,
) -> dict:
    context_policy = translation_context_policy(context_mode)
    intent = {
        "translator_model": settings.get("translator_model"),
        "analyzer_model": settings.get("analyzer_model"),
        "researcher_model": settings.get("researcher_model"),
        "enable_pre_analysis": settings.get("enable_pre_analysis"),
        "enable_research": settings.get("enable_research"),
        "research_tool_selections": settings.get("research_tool_selections", []),
        "translation_batch_input_tokens": settings.get(
            "translation_batch_input_tokens",
            DEFAULT_TRANSLATION_BATCH_INPUT_TOKENS,
        ),
        "translation_batch_max_blocks": settings.get(
            "translation_batch_max_blocks",
            DEFAULT_TRANSLATION_BATCH_MAX_BLOCKS,
        ),
        "translation_unit_mode": settings.get("translation_unit_mode", "block"),
        "translation_scope": dict(translation_scope),
        "translation_reasoning": translation_reasoning,
        "translation_context": context_policy,
        "owner_policy_hash": hashlib.sha256(
            str(
                settings.get("owner_policy") or settings.get("custom_prompt") or ""
            ).encode("utf-8")
        ).hexdigest(),
    }
    return {
        "engine_contract": DOCUMENT_BLOCK_ENGINE_CONTRACT,
        "source_content_hash": project.source_content_hash,
        "source_plan_id": source_plan.id,
        "source_plan_hash": source_plan.plan_hash,
        "translation_context": context_policy,
        "workflow_intent_hash": hashlib.sha256(
            canonical_json(intent).encode("utf-8")
        ).hexdigest(),
    }


class _BlockTranslationRequestOptions(TypedDict):
    scope: dict[str, object]
    translator_model: str | None
    translation_reasoning: str | None
    context_mode: str | None


_TRANSLATION_REQUEST_JSON_ERROR = "The translation request must be valid JSON."
_TRANSLATION_REQUEST_FIELDS_ERROR = (
    "The translation request contains unsupported fields."
)
_TRANSLATION_SCOPE_SHAPE_ERROR = (
    "Choose either remaining blocks or an explicit block selection."
)
_TRANSLATION_SCOPE_SELECTION_ERROR = "Choose a valid, non-empty block selection."
_TRANSLATION_SCOPE_PLAN_ERROR = (
    "The selected blocks no longer belong to the active source plan."
)
_TRANSLATION_MODEL_SELECTION_ERROR = "Choose a configured translation model."
_TRANSLATION_REASONING_SELECTION_ERROR = "Choose a valid translation reasoning level."


def _normalized_requested_translation_scope(
    plan: object,
    raw_scope: object,
) -> dict[str, object]:
    if not isinstance(raw_scope, Mapping) or set(raw_scope) != {"mode", "block_ids"}:
        raise ValueError(_TRANSLATION_SCOPE_SHAPE_ERROR)
    try:
        scope = normalize_translation_scope(
            {
                "contract_version": TRANSLATION_SCOPE_CONTRACT_VERSION,
                "mode": raw_scope.get("mode"),
                "block_ids": raw_scope.get("block_ids"),
            }
        )
    except BlockContractError as exc:
        raise ValueError(_TRANSLATION_SCOPE_SELECTION_ERROR) from exc
    ordered_plan_ids = [str(block.id) for block in getattr(plan, "blocks", ())]
    requested = set(cast(list[str], scope["block_ids"]))
    if not requested.issubset(ordered_plan_ids):
        raise ValueError(_TRANSLATION_SCOPE_PLAN_ERROR)
    if scope["mode"] == "explicit":
        scope["block_ids"] = [
            block_id for block_id in ordered_plan_ids if block_id in requested
        ]
    return scope


def _block_translation_request_options(
    plan: object,
) -> _BlockTranslationRequestOptions:
    """Normalize one owner request without trusting client block ordering."""

    data = request.get_json(silent=True)
    if data is None:
        if request.get_data(cache=True).strip():
            raise ValueError(_TRANSLATION_REQUEST_JSON_ERROR)
        data = {}
    if not isinstance(data, Mapping) or set(data) - {
        "scope",
        "translator_model",
        "translation_reasoning",
        "context_mode",
    }:
        raise ValueError(_TRANSLATION_REQUEST_FIELDS_ERROR)
    raw_scope = data.get("scope")
    if raw_scope is None:
        raw_scope = {"mode": "pending", "block_ids": []}
    scope = _normalized_requested_translation_scope(plan, raw_scope)

    model = data.get("translator_model")
    if model is not None:
        if not isinstance(model, str) or not model.strip() or model != model.strip():
            raise ValueError(_TRANSLATION_MODEL_SELECTION_ERROR)
        model = model.strip()
    reasoning = data.get("translation_reasoning")
    if reasoning is not None:
        if not isinstance(reasoning, str) or not reasoning.strip():
            raise ValueError(_TRANSLATION_REASONING_SELECTION_ERROR)
        reasoning = reasoning.strip().lower()
    context_mode = (
        normalize_translation_context_mode(data["context_mode"])
        if "context_mode" in data
        else None
    )
    return {
        "scope": scope,
        "translator_model": model,
        "translation_reasoning": reasoning,
        "context_mode": context_mode,
    }


def _translation_operation_settings(
    project: TranslationProject,
    options: _BlockTranslationRequestOptions,
) -> dict:
    settings = dict(project.settings or {})
    if options["translator_model"] is not None:
        settings["translator_model"] = options["translator_model"]
    return settings


def _operation_response(operation, *, status: int = 202, extra: dict | None = None):
    guest = bool(getattr(g, "is_guest", False))
    payload = {
        "success": True,
        "operation": _safe_operation_projection(operation, guest=guest),
        "latest_operation": _safe_operation_projection(operation, guest=guest),
        "job_id": None if guest else operation.queue_job_id,
    }
    if operation.queue_job_id and not guest:
        payload["status_url"] = _job_status_url(
            operation.project_id, operation.queue_job_id
        )
    if extra:
        payload.update(extra)
    return jsonify(payload), status


def _publish_operation(
    project: TranslationProject,
    *,
    kind: str,
    handler: str,
    request_payload: dict,
    queue_payload: dict | None = None,
    idempotency_key: str | None = None,
    source_plan_id: str | None = None,
    source_plan_hash: str | None = None,
    initialize_operation: Callable[[TranslationOperation, bool], None] | None = None,
):
    """Commit the operation before delivery and bind queue identity without races."""

    key = idempotency_key if idempotency_key is not None else _idempotency_key()
    try:
        operation, created = create_operation(
            project,
            kind=kind,
            request_payload=request_payload,
            idempotency_key=key,
            source_content_hash=project.source_content_hash,
            source_plan_id=source_plan_id,
            source_plan_hash=source_plan_hash,
            checkpoint=dict(queue_payload or {}),
        )
        if initialize_operation is not None:
            initialize_operation(operation, created)
        db.session.commit()
    except (
        TranslationOperationActiveConflict,
        TranslationOperationIdempotencyConflict,
    ) as exc:
        db.session.rollback()
        return None, _operation_conflict_response(exc.operation, message=str(exc))
    except Exception:
        db.session.rollback()
        raise

    # An enqueue crash after the first commit leaves a valid queued operation
    # without a delivery.  Retrying the identical request must republish the
    # same operation ID; the worker claim fence makes duplicate delivery safe.
    republish = (
        not created and operation.state == "queued" and not operation.queue_job_id
    )
    if not created and not republish:
        return operation, None

    return _deliver_queued_operation(
        project,
        operation,
        handler=handler,
        queue_payload=queue_payload,
    )


def _deliver_queued_operation(
    project: TranslationProject,
    operation: TranslationOperation,
    *,
    handler: str,
    queue_payload: dict | None = None,
):
    """Publish exactly one queued/unbound operation and bind its queue job."""

    if operation.state != "queued" or operation.queue_job_id:
        return operation, None
    # The admitted ledger identity takes precedence over operation payload fields.
    payload = {
        **dict(queue_payload or {}),
        "project_id": project.id,
        "user_id": project.user_id,
        "operation_id": operation.id,
    }
    try:
        job_id = _translator_queue().enqueue(handler, payload)
    except Exception as exc:  # noqa: BLE001 - delivery boundary must leave durable failure truth.
        db.session.rollback()
        failed = get_operation(operation.id, owner_user_id=project.user_id)
        if failed is not None:
            finish_operation(
                failed,
                "failed",
                error_code="queue_publish_failed",
                error_detail=str(exc),
                message="The operation could not be queued. Retry it when the queue is available.",
            )
            db.session.commit()
        logger.exception("Unable to queue translator operation %s", operation.id)
        return None, (
            jsonify(
                {
                    "error": "Unable to queue translator work.",
                    "retryable": True,
                    "operation": _safe_operation_projection(failed)
                    if failed is not None
                    else None,
                }
            ),
            503,
        )

    try:
        attach_queue_job(operation, job_id)
        db.session.commit()
    except TranslationOperationTransitionConflict:
        # A fast worker may have claimed the queued row and attached exactly
        # this delivery before the route returned.  That is a successful race.
        db.session.rollback()
        operation = get_operation(operation.id, owner_user_id=project.user_id)
        if operation is None or not operation.queue_job_id:
            logger.error(
                "Translator operation %s lost queue binding",
                operation.id if operation else "unknown",
            )
            return None, (
                jsonify({"error": "Unable to bind queued translator work."}),
                503,
            )
    return operation, None


def _repair_reused_document_ingestion(project: TranslationProject):
    """Close a create/outbox crash gap using only the pinned source snapshot."""
    from .source_planning import is_async_source_operation, planning_payload, snapshot_for_operation

    latest = (TranslationOperation.query.filter_by(
        project_id=project.id, owner_user_id=project.user_id, kind="plan_document_blocks",
    ).order_by(TranslationOperation.created_at.desc()).first())
    if latest is not None and is_async_source_operation(latest):
        try:
            snapshot_for_operation(project, latest)
        except ValueError:
            return None, (jsonify({"error": "The pending source snapshot is invalid."}), 409)
        payload = planning_payload(project.source_content_hash)
        if latest.state == "queued" and not latest.queue_job_id:
            return _deliver_queued_operation(project, latest, handler="plan_document_blocks", queue_payload=payload)
        if latest.state == "failed":
            checkpoint = {**latest.checkpoint_json, "phase": "queued"}
            return _publish_operation(
                project, kind="plan_document_blocks", handler="plan_document_blocks",
                request_payload=payload, queue_payload=payload, idempotency_key=f"recover-{latest.id}",
                initialize_operation=lambda op, created: setattr(op, "checkpoint_json", checkpoint) if created else None,
            )
        return latest, None

    try:
        engine_contract = effective_engine_contract(project.engine_contract)
    except BlockContractError:
        return None, (
            jsonify({"error": "The project engine contract is invalid."}),
            409,
        )
    if engine_contract == DOCUMENT_BLOCK_ENGINE_CONTRACT:
        # Block projects are created with a durable plan before their first
        # operation is published.  A missing plan or any legacy segment row is
        # an integrity failure, never a reason to fall back to segmentation.
        if (
            TranslationSegment.query.filter_by(project_id=project.id).first()
            is not None
        ):
            return None, (
                jsonify({"error": "Block project contains legacy segmentation data."}),
                409,
            )
        try:
            workspace_plan = load_block_source_plan(project)
            plan = workspace_plan.record
            rehydrated = workspace_plan.plan
        except Exception:  # bounded integrity boundary
            logger.warning(
                "Unable to load the active block source plan for %s",
                project.id,
                exc_info=True,
            )
            return None, (
                jsonify({"error": "The block source plan is invalid or unavailable."}),
                409,
            )
        if (
            rehydrated.project_id != project.id
            or rehydrated.snapshot.content_hash != plan.source_content_hash
            or project.source_content_hash != plan.source_content_hash
        ):
            return None, (
                jsonify(
                    {"error": "The block source plan pins do not match the project."}
                ),
                409,
            )
        source_hash = plan.source_content_hash
        request_payload = {
            "source_content_hash": source_hash,
            "source_plan_id": plan.id,
            "source_plan_hash": plan.plan_hash,
            "engine_contract": DOCUMENT_BLOCK_ENGINE_CONTRACT,
        }
        active = reconcile_expired_active_operation(project.id, project.user_id)
        if active is not None:
            if active.kind != "plan_document_blocks":
                return None, (
                    jsonify(
                        {"error": "A conflicting legacy ingestion operation exists."}
                    ),
                    409,
                )
            if (
                active.source_plan_id != plan.id
                or active.source_plan_hash != plan.plan_hash
                or active.source_content_hash != source_hash
            ):
                return None, (
                    jsonify(
                        {
                            "error": (
                                "The active block operation has foreign source pins."
                            )
                        }
                    ),
                    409,
                )
            if active.state == "queued" and not active.queue_job_id:
                return _deliver_queued_operation(
                    project,
                    active,
                    handler="plan_document_blocks",
                )
            return active, None
        latest = (
            TranslationOperation.query.filter(
                TranslationOperation.project_id == project.id,
                TranslationOperation.owner_user_id == project.user_id,
                TranslationOperation.kind == "plan_document_blocks",
            )
            .order_by(TranslationOperation.created_at.desc())
            .first()
        )
        if latest is not None and not (
            latest.state == "failed"
            and latest.error_code
            in {"queue_delivery_lost", "queue_publish_failed", "lease_expired"}
        ):
            if (
                latest.source_plan_id != plan.id
                or latest.source_plan_hash != plan.plan_hash
                or latest.source_content_hash != source_hash
            ):
                return None, (
                    jsonify(
                        {
                            "error": "The existing block operation has foreign source pins."
                        }
                    ),
                    409,
                )
            return latest, None
        repair_key = (
            f"recover-{latest.id}"
            if latest is not None
            else f"initial-plan-{plan.plan_hash[:48]}"
        )
        return _publish_operation(
            project,
            kind="plan_document_blocks",
            handler="plan_document_blocks",
            request_payload=request_payload,
            idempotency_key=repair_key,
            source_plan_id=plan.id,
            source_plan_hash=plan.plan_hash,
        )

    return None, (
        jsonify({"error": "Document ingestion supports block projects only."}),
        409,
    )


def _discard_created_project_without_delivery(project: TranslationProject) -> bool:
    """Release a create idempotency key only after a proven no-delivery failure.

    This is intentionally narrower than normal operation failure handling: a
    newly created project has no useful retry path if its initial queue publish
    failed before a job identity existed.  Any binding ambiguity is retained,
    because deleting it could race a real worker delivery.
    """

    operation = (
        TranslationOperation.query.filter_by(
            project_id=project.id,
            owner_user_id=project.user_id,
        )
        .order_by(TranslationOperation.created_at.desc())
        .first()
    )
    if (
        operation is None
        or operation.queue_job_id
        or operation.state != "failed"
        or operation.error_code != "queue_publish_failed"
    ):
        return False
    db.session.delete(project)
    db.session.commit()
    return True


def _workspace_asset_version() -> int:
    asset_path = (
        Path(current_app.root_path)
        / "addons"
        / "translator"
        / "static"
        / "dist"
        / "workspace.js"
    )
    try:
        return int(asset_path.stat().st_mtime)
    except OSError:
        return int(time.time())


def _workspace_theme_stylesheets() -> list[str]:
    """Use the same host theme as the embedded workspace on standalone pages."""
    core = get_runtime_core_frontend(Path(current_app.root_path).resolve().parent)
    return (core.get("assets") or {}).get("css_urls") or []


def _projects_asset_version() -> int:
    asset_path = (
        Path(current_app.root_path)
        / "addons"
        / "translator"
        / "static"
        / "dist"
        / "projects.js"
    )
    try:
        return int(asset_path.stat().st_mtime)
    except OSError:
        return int(time.time())


def _job_status_url(project_id: str, job_id: str) -> str:
    return url_for(
        "translator.get_project_job_status", project_id=project_id, job_id=job_id
    )


def _job_download_url(project_id: str, job_id: str) -> str:
    return url_for(
        "translator.download_project_job_result", project_id=project_id, job_id=job_id
    )


def _public_job_status(
    job_id: str,
    status: dict,
    project_id: str | None = None,
    *,
    allow_download: bool = True,
    operation: TranslationOperation | None = None,
) -> dict:
    """Expose delivery progress only; the operation row is domain truth.

    Queue values may contain worker tracebacks, request payloads, provider
    errors, or filesystem paths.  Do not copy the queue object wholesale.
    """

    raw = status if isinstance(status, dict) else {}
    payload = {
        "status": str(raw.get("status") or "unknown"),
        "progress": raw.get("progress")
        if isinstance(raw.get("progress"), (int, float))
        else None,
    }
    # A worker may fail or be cancelled before its delivery cache catches up.
    # Report the durable terminal state without exposing private diagnostics.
    terminal_state = getattr(operation, "state", None)
    if terminal_state in {"failed", "cancelled", "partial"}:
        payload["status"] = terminal_state
        payload["message"] = (
            {
                "failed": "Document export failed. Please retry; if it fails again, check the local worker logs.",
                "cancelled": "Document export was cancelled.",
                "partial": "Document export did not complete. Please retry.",
            }[terminal_state]
            if getattr(operation, "kind", None) == "document_block_export"
            else f"Background operation {terminal_state}."
        )
        return payload
    result = raw.get("result")
    if (
        allow_download
        and isinstance(result, dict)
        and result.get("kind") == "file_download"
        and project_id
    ):
        payload["result"] = {
            "kind": "file_download",
            "download_url": _job_download_url(project_id, job_id),
        }
    return payload


def _serialize_project(project: TranslationProject):
    payload = project.to_dict()
    payload["engine_contract"] = effective_engine_contract(project.engine_contract)
    guest = bool(getattr(g, "is_guest", False))
    if guest:
        payload.pop("share_token", None)
    settings = project.settings or {}
    if guest:
        # Settings hold prompts, provider identifiers, source capability
        # references, knowledge collection IDs, and subtitle source metadata.
        # A share link is a read-only translation view, not an export of that
        # owner-only configuration.
        payload["settings"] = {
            "source_type": str(settings.get("source_type") or "document"),
        }
    else:
        payload["settings"] = dict(settings)
        payload["settings"]["enable_translator_notes"] = payload["settings"].get(
            "enable_translator_notes", False
        )
        if is_document_block_engine(project):
            payload["settings"]["translation_unit_mode"] = (
                normalize_translation_unit_mode(
                    payload["settings"].get("translation_unit_mode")
                )
            )
            payload["settings"]["annotation_profile"] = normalize_annotation_profile(
                payload["settings"].get("annotation_profile"),
                payload["settings"]["enable_translator_notes"],
            )
            payload["settings"]["enable_translator_notes"] = (
                payload["settings"]["annotation_profile"] != "none"
            )
    source_type = settings.get("source_type") or "document"
    payload["source_type"] = source_type
    payload["source_status"] = "unavailable"
    payload["source_error"] = None
    payload["document"] = None
    payload["document_status"] = None
    payload["subtitle_project"] = None
    try:
        if source_type == "subtitle_studio":
            if guest and project.source_snapshot:
                payload["subtitle_project"] = {
                    "id": None,
                    "name": "Snapshotted subtitle source",
                    "status": "snapshotted",
                }
                payload["source_status"] = "ready"
            else:
                subtitle_project_id = settings.get("subtitle_project_id")
                if subtitle_project_id:
                    provider = _subtitle_source_provider(project.user_id, settings)
                    payload["subtitle_project"] = (
                        provider.client.get_translation_source_summary(
                            int(subtitle_project_id)
                        )
                    )
                    payload["source_status"] = "ready"
        else:
            # A project stays readable after the provider resource changes or
            # disappears: this is a pinned snapshot, not a live document view.
            if project.source_snapshot:
                payload["document"] = {
                    "id": project.document_id,
                    "filename": "Snapshotted document"
                    if guest
                    else settings.get("source_snapshot_label")
                    or "Snapshotted document",
                    "status": "snapshotted",
                }
                payload["document_status"] = "snapshotted"
                payload["source_status"] = "ready"
            else:
                document = _get_document_for_project(project)
                if document:
                    payload["document"] = document
                    payload["document_status"] = document.get("status")
                    payload["source_status"] = "ready"
    except CapabilityUseError as exc:
        payload["source_status"] = "capability_setup_required"
        payload["source_error"] = exc.to_dict()
    except LookupError, PermissionError, RuntimeError, TypeError, ValueError:
        logger.exception("Translator source lookup failed for project %s", project.id)
        payload["source_status"] = "unavailable"
        payload["source_error"] = {
            "code": "source_unavailable",
            "error": "The configured source is unavailable.",
        }
    payload["source_snapshot"] = {
        "pinned": bool(project.source_snapshot),
        "content_hash": None if guest else project.source_content_hash,
        "captured_at": utc_iso(project.source_captured_at)
        if project.source_captured_at
        else None,
        "label": (
            "Snapshotted document"
            if guest
            else settings.get("source_snapshot_label") or "Snapshotted document"
        )
        if source_type != "subtitle_studio"
        else None,
    }
    operation = _latest_operation(project)
    payload["latest_operation"] = (
        _safe_operation_projection(operation, guest=guest) if operation else None
    )
    return payload


blueprint = Blueprint(
    "translator",
    __name__,
    template_folder="templates",
    static_folder="static",
)


@blueprint.route("/api/projects/<project_id>/settings", methods=["GET", "PUT"])
@login_required
def manage_project_settings(project_id):
    project = TranslationProject.query.filter_by(
        id=project_id, user_id=current_user.id
    ).first_or_404()

    if request.method == "GET":
        settings = dict(project.settings or {})
        settings["translation_context_mode"] = normalize_translation_context_mode(
            settings.get("translation_context_mode", "self_contained")
        )
        if is_document_block_engine(project):
            settings["translation_unit_mode"] = normalize_translation_unit_mode(
                settings.get("translation_unit_mode")
            )
            settings.setdefault(
                "translation_batch_input_tokens",
                DEFAULT_TRANSLATION_BATCH_INPUT_TOKENS,
            )
            settings.setdefault(
                "translation_batch_max_blocks",
                DEFAULT_TRANSLATION_BATCH_MAX_BLOCKS,
            )
            settings["annotation_profile"] = normalize_annotation_profile(
                settings.get("annotation_profile"),
                settings.get("enable_translator_notes", False),
            )
            settings["enable_translator_notes"] = (
                settings["annotation_profile"] != "none"
            )
        settings["enable_translator_notes"] = settings.get(
            "enable_translator_notes", False
        )
        return jsonify(settings)

    active = reconcile_expired_active_operation(project.id, current_user.id)
    if active is not None:
        return _operation_conflict_response(
            active,
            message="Project settings cannot change while translator work is active.",
        )

    data = request.get_json(silent=True)
    if data is None:
        data = {}
    if not isinstance(data, dict):
        return jsonify({"error": "Project settings must be a JSON object."}), 400
    settings = dict(project.settings or {})
    for key in (
        "translator_model",
        "researcher_model",
        "analyzer_model",
        "enable_pre_analysis",
        "enable_research",
        "enable_translator_notes",
        "annotation_profile",
        "research_plugin_ids",
        "research_tool_selections",
        "translation_batch_input_tokens",
        "translation_batch_max_blocks",
        "translation_context_mode",
        "translation_unit_mode",
        "chunk_size_chars",
        "custom_prompt",
    ):
        if key in data:
            settings[key] = data[key]
    try:
        settings["translation_context_mode"] = normalize_translation_context_mode(
            settings.get("translation_context_mode", "self_contained")
        )
        if is_document_block_engine(project):
            _validate_block_model_settings(settings, user_id=current_user.id)
            if (
                settings["translation_unit_mode"] == "aligned_sentences"
                and normalize_translation_unit_mode(
                    (project.settings or {}).get("translation_unit_mode")
                )
                != "aligned_sentences"
            ):
                workspace_plan = load_block_source_plan(project)
                if any(
                    anchor.splitter_profile != "sentence-segmenter"
                    for block in workspace_plan.plan.blocks
                    for anchor in block.anchors
                ):
                    return jsonify(
                        {
                            "error": "This project's source was not prepared with the Sentence Segmenter. Create a new sentence-mode project; existing translations and notes will remain unchanged."
                        }
                    ), 409
        else:
            _validate_model_settings(settings, user_id=current_user.id)
    except ModelNotReadyError as exc:
        return _model_error(exc)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    prompt = str(settings.get("custom_prompt") or "").strip()
    if len(prompt) > 20_000:
        return jsonify(
            {"error": "Custom prompt must be at most 20,000 characters."}
        ), 400
    settings["custom_prompt"] = prompt
    project.settings = settings

    flag_modified(project, "settings")
    db.session.commit()
    return jsonify({"success": True})


@blueprint.route("/")
@login_required
def index():
    return render_frontend_shell(
        route_id="addon:translator",
        required_flag="frontend.addon.translator",
    )


@blueprint.route("/shared/<token>")
def shared_workspace(token):
    project = TranslationProject.query.filter_by(share_token=token).first_or_404()
    return render_template(
        "translator_workspace.html",
        project=project,
        guest_mode=True,
        share_token=token,
        asset_version=_workspace_asset_version(),
        frontend_css_urls=_workspace_theme_stylesheets(),
    )


@blueprint.route("/api/projects/<project_id>/share", methods=["POST", "DELETE"])
@login_required
def generate_share_token(project_id):
    project = TranslationProject.query.filter_by(
        id=project_id, user_id=current_user.id
    ).first_or_404()
    if request.method == "DELETE":
        project.share_token = None
        db.session.commit()
        return jsonify({"success": True, "revoked": True})
    data = request.get_json(silent=True)
    if data is None:
        data = {}
    if not isinstance(data, dict):
        return jsonify({"error": "Share settings must be a JSON object."}), 400
    rotate = data.get("rotate", False)
    try:
        rotate = _coerce_bool(rotate, default=False)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if rotate or not project.share_token:
        project.share_token = secrets.token_urlsafe(32)
        db.session.commit()
    return jsonify(
        {
            "share_token": project.share_token,
            "url": url_for(
                "translator.shared_workspace", token=project.share_token, _external=True
            ),
        }
    )


@blueprint.route("/api/projects", methods=["GET"])
@login_required
def get_projects():
    projects = (
        TranslationProject.query.filter_by(user_id=current_user.id)
        .order_by(TranslationProject.updated_at.desc())
        .all()
    )
    return jsonify([_serialize_project(project) for project in projects])


@blueprint.route("/api/document_sources", methods=["GET"])
@login_required
def get_document_sources():
    provider = _document_source_provider(current_user.id)
    documents = provider.client.get_documents(status="completed")
    return jsonify(
        [
            document
            for document in documents
            if isinstance(document, dict)
            and document.get("semantic_contract_mode") == "completed"
        ]
    )


@blueprint.route("/api/subtitle_sources", methods=["GET"])
@login_required
def get_subtitle_sources():
    provider = _subtitle_source_provider(current_user.id)
    return jsonify(provider.client.list_translation_sources())


@blueprint.route("/api/research-providers", methods=["GET"])
@login_required
def get_research_providers():
    return jsonify(list(_research_provider_catalog().values()))


@blueprint.route("/api/block-research-tools", methods=["GET"])
@login_required
def get_block_research_tools():
    manager = _addon_manager()
    if manager is None:
        return (
            jsonify({"error": "The exact research-tool catalog is unavailable."}),
            503,
        )
    try:
        rows = exact_research_tool_catalog(manager, int(current_user.id))
    except BlockContractError as exc:
        return (
            jsonify(
                {
                    "error": "Unable to build the research-tool catalog.",
                    "code": exc.code,
                }
            ),
            409,
        )
    return jsonify(list(rows))


@blueprint.route("/workspace/<project_id>")
@login_required
def workspace(project_id):
    TranslationProject.query.filter_by(
        id=project_id,
        user_id=current_user.id,
    ).first_or_404()
    return render_frontend_shell(
        route_id="addon:translator",
        required_flag="frontend.addon.translator",
    )


@blueprint.route("/review/<project_id>")
def review_workspace(project_id):
    """Render the capability bootstrap without resolving or logging its secret."""

    try:
        canonical_project_id = str(uuid.UUID(project_id))
    except AttributeError, TypeError, ValueError:
        return jsonify({"error": "Project not found or access denied"}), 404
    return render_template(
        "translator_workspace.html",
        project_id=canonical_project_id,
        review_mode=True,
        guest_mode=True,
        asset_version=_workspace_asset_version(),
        frontend_css_urls=_workspace_theme_stylesheets(),
    )


@blueprint.route("/api/projects/<project_id>/block-workspace", methods=["GET"])
@project_access_required
def get_block_workspace(project_id):
    """Return the verified, paginated semantic block workspace projection."""

    try:
        limit = min(
            MAX_WORKSPACE_LIMIT,
            max(1, int(request.args.get("limit", MAX_WORKSPACE_LIMIT))),
        )
        offset = max(0, int(request.args.get("offset", 0)))
    except TypeError, ValueError:
        return jsonify({"error": "limit and offset must be whole numbers."}), 400
    try:
        payload = build_workspace_payload(
            g.project,
            offset=offset,
            limit=limit,
            latest_operation=_latest_operation(g.project),
            access_role=getattr(g, "access_role", "owner"),
            access_label=getattr(g, "access_label", None),
            actor=getattr(g, "review_actor", None),
        )
    except BlockWorkspaceIntegrityError as exc:
        return (
            jsonify(
                {
                    "error": "The block workspace failed its integrity checks.",
                    "code": exc.code,
                    "engine_contract": getattr(g.project, "engine_contract", None),
                }
            ),
            409,
        )
    return jsonify(payload)


@blueprint.route("/api/projects/<project_id>/translation-context", methods=["GET"])
@project_access_required
def get_translation_context(project_id):
    """Return the owner's bounded source-aligned context bootstrap."""

    owner_id, error = _shared_glossary_project_owner_guard()
    if error is not None:
        return error
    allowed = {"before_block_id", "max_segments", "source_plan_id"}
    if set(request.args.keys()) - allowed:
        return jsonify({"error": "Unsupported translation context query field."}), 400

    before_values = request.args.getlist("before_block_id")
    if len(before_values) != 1 or not before_values[0]:
        return jsonify({"error": "before_block_id is required exactly once."}), 400
    max_values = request.args.getlist("max_segments")
    if len(max_values) > 1 or (
        max_values and re.fullmatch(r"[1-3]", max_values[0]) is None
    ):
        return jsonify({"error": "max_segments must be an integer from 1 to 3."}), 400
    max_segments = int(max_values[0]) if max_values else 3
    source_plan_values = request.args.getlist("source_plan_id")
    if len(source_plan_values) > 1 or (
        source_plan_values and not source_plan_values[0]
    ):
        return jsonify({"error": "source_plan_id must be supplied at most once."}), 400
    source_plan_id = source_plan_values[0] if source_plan_values else None

    try:
        payload = _shared_glossary_client(owner_id).context_get(
            project_id,
            before_block_id=before_values[0],
            max_segments=max_segments,
            source_plan_id=source_plan_id,
        )
    except BlockContractError as exc:
        status = (
            404
            if exc.code == "segment-context-before-block"
            else 409
            if exc.code == "segment-context-source-plan"
            else 400
        )
        return jsonify({"error": "The translation context request was rejected.", "code": exc.code}), status
    except BlockWorkspaceIntegrityError as exc:
        return jsonify({"error": "The active source plan failed integrity checks.", "code": exc.code}), 409
    except LookupError:
        return jsonify({"error": "The translation project or active source plan was not found."}), 404
    except RuntimeError:
        return jsonify({"error": "The requested source plan is no longer current."}), 409
    except (TypeError, ValueError):
        return jsonify({"error": "The translation context request is invalid."}), 400
    return jsonify(payload)


@blueprint.route("/api/projects/<project_id>/qa-diagnostics", methods=["GET"])
@project_access_required
def get_qa_diagnostics(project_id):
    """Return paginated deterministic QA evidence for active block targets."""

    del project_id
    categories = tuple(
        item.strip()
        for item in request.args.get("categories", "").split(",")
        if item.strip()
    )
    severities = tuple(
        item.strip()
        for item in request.args.get("severities", "").split(",")
        if item.strip()
    )
    block_ids = tuple(
        item.strip() for item in request.args.getlist("block_id") if item.strip()
    ) or None
    try:
        limit = int(request.args.get("limit", 100))
    except TypeError, ValueError:
        return jsonify({"error": "limit must be a whole number.", "code": "qa-limit-invalid"}), 400
    include_spelling = str(request.args.get("include_spelling", "1")).casefold() not in {
        "0",
        "false",
        "no",
        "off",
    }
    try:
        payload = qa_diagnostics(
            g.project,
            block_ids=block_ids,
            categories=categories,
            severities=severities,
            cursor=request.args.get("cursor") or None,
            limit=limit,
            include_spelling=include_spelling,
            group_by=request.args.get("group_by", "finding"),
            include_excerpts=request.args.get("include_excerpts", "1").casefold() not in {"0", "false", "no", "off"},
            group_id=request.args.get("group_id") or None,
        )
    except QaDiagnosticsError as exc:
        return jsonify({"error": "QA diagnostics request is invalid.", "code": exc.code}), 400
    except DocumentReviewError as exc:
        status = 413 if exc.code == "review-result-too-large" else 400
        return jsonify({"error": "QA diagnostics response is invalid.", "code": exc.code}), status
    except BlockWorkspaceIntegrityError as exc:
        return jsonify({"error": "The block workspace failed its integrity checks.", "code": exc.code}), 409

    if not bool(getattr(g, "is_guest", False)):
        document_id = getattr(g.project, "document_id", None)
        if isinstance(document_id, str) and document_id:
            pages = sorted(
                {
                    page
                    for item in payload.get("items", [])
                    for page in item.get("source_pages", [])
                    if isinstance(page, int) and page > 0
                }
            )
            page_urls: dict[str, str] = {}
            for page in pages:
                try:
                    page_urls[str(page)] = url_for(
                        "doc_processor.get_pdf_page_thumbnail",
                        document_id=document_id,
                        page_index=page - 1,
                        width=1600,
                    )
                except Exception:
                    page_urls = {}
                    break
            if page_urls:
                payload["source_page_urls"] = page_urls
    return jsonify(payload)


@blueprint.route("/api/projects/<project_id>/status", methods=["GET"])
@project_access_required
def get_project_status(project_id):
    """Return the cheap durable status projection used by polling UIs."""

    return jsonify(_project_status_payload(g.project))


def _block_glossary_write(concept_id: str | None):
    if bool(getattr(g, "is_guest", False)):
        return _guest_forbidden("Shared workspaces are read-only.")
    try:
        author_user_id = int(current_user.id)
    except TypeError, ValueError, AttributeError:
        return jsonify({"error": "Only the project owner may edit the glossary."}), 403
    if author_user_id != int(g.project.user_id):
        return jsonify({"error": "Only the project owner may edit the glossary."}), 403
    raw_body = request.get_data(cache=True)
    if len(raw_body) > MAX_GLOSSARY_JSON_BYTES:
        return jsonify({"error": "Glossary payload is too large."}), 413
    data = request.get_json(silent=True)
    required = {
        "source_term",
        "target_term",
        "notes",
        "expected_active_version",
        "expected_proposal_id",
    }
    allowed = required | {"accepted_target_forms"}
    if (
        not isinstance(data, dict)
        or not required.issubset(data)
        or set(data) - allowed
    ):
        return jsonify(
            {
                "error": (
                    "Glossary updates must contain source_term, target_term, notes, "
                    "expected_active_version, and expected_proposal_id; accepted_target_forms "
                    "is optional."
                )
            }
        ), 400
    source_term = data.get("source_term")
    target_term = data.get("target_term")
    notes = data.get("notes")
    expected_version = data.get("expected_active_version")
    expected_proposal_id = data.get("expected_proposal_id")
    accepted_target_forms = data.get("accepted_target_forms")
    if (
        not isinstance(source_term, str)
        or not isinstance(target_term, str)
        or notes is not None
        and not isinstance(notes, str)
        or isinstance(expected_version, bool)
        or not isinstance(expected_version, int)
        or expected_version < 0
        or (
            expected_proposal_id is not None
            and (not isinstance(expected_proposal_id, str) or not expected_proposal_id)
        )
        or (
            accepted_target_forms is not None
            and (
                not isinstance(accepted_target_forms, list)
                or len(accepted_target_forms) > 128
                or any(not isinstance(item, str) for item in accepted_target_forms)
            )
        )
    ):
        return jsonify({"error": "Glossary fields have invalid types."}), 400
    try:
        payload = persist_human_glossary_entry(
            g.project,
            concept_id=concept_id,
            source_term=source_term,
            target_term=target_term,
            notes=notes,
            expected_active_version=expected_version,
            expected_proposal_id=expected_proposal_id,
            accepted_target_forms=accepted_target_forms,
            author_user_id=author_user_id,
        )
    except BlockWorkspaceIntegrityError as exc:
        status = (
            409
            if exc.code
            in {
                "glossary-active-conflict",
                "glossary-corrupt",
                "glossary-duplicate",
                "glossary-owner",
                "glossary-proposal-conflict",
            }
            else 400
        )
        return (
            jsonify(
                {
                    "error": "Unable to save the glossary entry.",
                    "code": exc.code,
                }
            ),
            status,
        )
    return jsonify(payload), 201 if concept_id is None else 200


@blueprint.route("/api/projects/<project_id>/block-glossary", methods=["GET", "POST"])
@project_access_required
def block_glossary(project_id):
    """Read or append authoritative terminology for the next block operation."""

    if request.method == "POST":
        return _block_glossary_write(None)
    try:
        return jsonify(
            build_block_glossary_payload(
                g.project,
                include_proposals=getattr(g, "access_role", "owner") != "viewer",
            )
        )
    except BlockWorkspaceIntegrityError as exc:
        return (
            jsonify(
                {
                    "error": "The block glossary failed its integrity checks.",
                    "code": exc.code,
                }
            ),
            409,
        )


@blueprint.route(
    "/api/projects/<project_id>/block-glossary/<concept_id>", methods=["PUT"]
)
@project_access_required
def update_block_glossary(project_id, concept_id):
    """Append and activate an owner-authored revision of one glossary term."""

    return _block_glossary_write(concept_id)


@blueprint.route(
    "/api/projects/<project_id>/blocks/<block_id>/candidate", methods=["PUT"]
)
@project_access_required
def update_block_candidate(project_id, block_id):
    """Atomically append and activate one owner-authored block candidate."""

    access_role = getattr(g, "access_role", "owner")
    if access_role == "viewer":
        return _guest_forbidden("Viewer links are read-only.")
    if access_role == "reviewer":
        author_user_id = None
        share_grant_id = getattr(g.review_actor, "share_grant_id", None)
    else:
        try:
            author_user_id = int(current_user.id)
        except TypeError, ValueError, AttributeError:
            return jsonify(
                {"error": "Only the project owner or a reviewer may edit blocks."}
            ), 403
        if author_user_id != int(g.project.user_id):
            return jsonify(
                {"error": "Only the project owner or a reviewer may edit blocks."}
            ), 403
        share_grant_id = None
    try:
        idempotency_key = _idempotency_key()
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if idempotency_key is None:
        return jsonify({"error": "Idempotency-Key is required."}), 400
    raw_body = request.get_data(cache=True)
    if len(raw_body) > MAX_CANDIDATE_JSON_BYTES:
        return jsonify({"error": "Candidate payload is too large."}), 413
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or set(data) not in (
        {"candidate", "expected_active_version"},
        {"sentence_edit", "expected_active_version"},
    ):
        return jsonify(
            {
                "error": (
                    "Supply candidate or sentence_edit, together with "
                    "expected_active_version."
                )
            }
        ), 400
    expected_version = data.get("expected_active_version")
    if (
        isinstance(expected_version, bool)
        or not isinstance(expected_version, int)
        or expected_version < 0
    ):
        return jsonify({"error": "expected_active_version must be non-negative."}), 400
    try:
        block = persist_manual_candidate(
            g.project,
            block_id,
            data.get("candidate"),
            expected_version,
            author_user_id=author_user_id,
            share_grant_id=share_grant_id,
            idempotency_key=idempotency_key,
            **({"sentence_edit": data["sentence_edit"]} if "sentence_edit" in data else {}),
        )
    except BlockWorkspaceIntegrityError as exc:
        if exc.code == "target-active-conflict":
            try:
                latest = current_block_projection(g.project, block_id)
            except BlockWorkspaceIntegrityError as latest_exc:
                return (
                    jsonify(
                        {
                            "error": "The block changed and its latest state is invalid.",
                            "code": latest_exc.code,
                            "engine_contract": getattr(
                                g.project, "engine_contract", None
                            ),
                        }
                    ),
                    409,
                )
            active = (latest.get("target") or {}).get("active_version", 0)
            return (
                jsonify(
                    {
                        "error": "The block changed before this edit was saved.",
                        "code": "stale_active_version",
                        "latest_block": latest,
                        "active_version": active,
                    }
                ),
                409,
            )
        status = (
            409
            if exc.code
            in {
                "candidate-owner",
                "candidate-idempotency-conflict",
                "block-owner",
                "source-plan-active-conflict",
                "source-plan-cardinality",
                "source-plan-corrupt",
                "target-active-cardinality",
                "target-active-operation",
            }
            else 400
        )
        return (
            jsonify(
                {
                    "error": "The block candidate failed validation.",
                    "code": exc.code,
                    "engine_contract": getattr(g.project, "engine_contract", None),
                }
            ),
            status,
        )
    return jsonify({"success": True, "block": block}), 200


@blueprint.route(
    "/api/projects/<project_id>/blocks/<block_id>/correction", methods=["POST"]
)
@project_access_required
def correct_block_structure(project_id, block_id):
    """Advance the active source plan after one explicit owner correction."""

    if bool(getattr(g, "is_guest", False)):
        return _guest_forbidden("Shared workspaces are read-only.")
    try:
        owner_user_id = int(current_user.id)
    except TypeError, ValueError, AttributeError:
        return jsonify({"error": "Only the project owner may correct blocks."}), 403
    if owner_user_id != int(g.project.user_id):
        return jsonify({"error": "Only the project owner may correct blocks."}), 403
    raw_body = request.get_data(cache=True)
    if len(raw_body) > 8 * 1024:
        return jsonify({"error": "Block correction payload is too large."}), 413
    data = request.get_json(silent=True)
    required = {"action", "expected_source_plan_id", "expected_active_version"}
    if not isinstance(data, dict) or set(data) != required:
        return (
            jsonify(
                {
                    "error": (
                        "Block correction must contain only action, "
                        "expected_source_plan_id, and expected_active_version."
                    )
                }
            ),
            400,
        )
    action = data.get("action")
    expected_plan_id = data.get("expected_source_plan_id")
    expected_version = data.get("expected_active_version")
    if (
        not isinstance(action, str)
        or not isinstance(expected_plan_id, str)
        or not expected_plan_id
        or isinstance(expected_version, bool)
        or not isinstance(expected_version, int)
        or expected_version < 1
    ):
        return jsonify({"error": "Block correction fields have invalid types."}), 400
    try:
        idempotency_key = _idempotency_key()
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if idempotency_key is None:
        return jsonify({"error": "Idempotency-Key is required."}), 400
    try:
        result = correct_active_source_plan(
            g.project,
            block_id=block_id,
            action=action,
            expected_source_plan_id=expected_plan_id,
            expected_active_version=expected_version,
            idempotency_key=idempotency_key,
            owner_user_id=owner_user_id,
        )
    except SourcePlanCorrectionError as exc:
        conflict_codes = {
            "correction-adjacent-missing",
            "correction-adjacent-owner",
            "correction-block-owner",
            "correction-exclude-overlap",
            "correction-merge-anchors",
            "correction-merge-incompatible",
            "correction-merge-structure",
            "source-plan-active-conflict",
            "source-plan-active-missing",
            "source-plan-correction-active-operation",
            "source-plan-correction-conflict",
            "source-plan-correction-corrupt",
            "source-plan-correction-idempotency-conflict",
            "source-plan-correction-reuse-identity",
            "source-plan-correction-reuse-pair",
        }
        return (
            jsonify(
                {
                    "error": "Unable to correct the semantic block structure.",
                    "code": exc.code,
                }
            ),
            409 if exc.code in conflict_codes else 400,
        )
    return jsonify({"success": True, **result}), 200


def _base_project_settings(data) -> dict:
    if "export_metadata" in data:
        raw_export_metadata = data["export_metadata"]
    else:
        supplied_settings = data.get("settings")
        raw_export_metadata = (
            supplied_settings.get("export_metadata", {})
            if isinstance(supplied_settings, Mapping)
            else {}
        )
    export_metadata = normalize_export_metadata(raw_export_metadata)
    if export_metadata.get("cover_upload_path"):
        export_metadata = pin_export_metadata_cover(export_metadata, user_id=int(current_user.id))
    return {
        "translator_model": data.get("translator_model"),
        "researcher_model": data.get("researcher_model"),
        "analyzer_model": data.get("analyzer_model"),
        "enable_pre_analysis": _coerce_bool(
            data.get("enable_pre_analysis"), default=True
        ),
        "enable_research": _coerce_bool(data.get("enable_research"), default=False),
        "enable_translator_notes": _coerce_bool(
            data.get("enable_translator_notes"), default=False
        ),
        "annotation_profile": data.get("annotation_profile"),
        "research_plugin_ids": data.get("research_plugin_ids", []),
        "research_tool_selections": data.get("research_tool_selections", []),
        "translation_batch_input_tokens": data.get(
            "translation_batch_input_tokens",
            DEFAULT_TRANSLATION_BATCH_INPUT_TOKENS,
        ),
        "translation_batch_max_blocks": data.get(
            "translation_batch_max_blocks",
            DEFAULT_TRANSLATION_BATCH_MAX_BLOCKS,
        ),
        "translation_context_mode": normalize_translation_context_mode(
            data.get("translation_context_mode", "self_contained")
        ),
        TRANSLATION_MAX_BLOCK_CHARS_SETTING: data.get(
            TRANSLATION_MAX_BLOCK_CHARS_SETTING,
            DEFAULT_TRANSLATION_MAX_BLOCK_CHARS,
        ),
        "chunk_size_chars": data.get("chunk_size_chars", data.get("chunk_size", 2500)),
        "export_metadata": export_metadata,
    }


def _split_subtitle_unit_settings(
    settings: dict | None,
    segment_id: str,
    new_segment_id: str,
    part1: str,
    part2: str,
    *,
    subtitle_client,
) -> bool:
    if (
        not isinstance(settings, dict)
        or settings.get("source_type") != "subtitle_studio"
    ):
        return False
    subtitle_settings = settings.get("subtitle")
    if not isinstance(subtitle_settings, dict):
        return False
    units = subtitle_settings.get("units")
    if not isinstance(units, dict):
        return False
    unit = units.get(segment_id)
    if not isinstance(unit, dict):
        return False

    split = subtitle_client.split_translation_unit(unit, part1, part2)
    if not isinstance(split, dict):
        raise ValueError("Subtitle provider returned invalid split metadata.")
    left_unit = subtitle_unit_metadata(split.get("left"))
    left_unit["split_peer_segment_id"] = new_segment_id
    right_unit = subtitle_unit_metadata(split.get("right"))
    right_unit["split_from_segment_id"] = segment_id
    # Source strings live in encrypted TranslationSegment/source_snapshot, not
    # the plaintext JSON settings column.  Rendering needs timing/provenance.
    left_unit.pop("source_text", None)
    left_unit.pop("text", None)
    right_unit.pop("source_text", None)
    right_unit.pop("text", None)
    units[segment_id] = left_unit
    units[new_segment_id] = right_unit
    return True


def _parse_snapshot_time(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return as_utc(value).replace(tzinfo=None)
    try:
        return as_utc(
            datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        ).replace(tzinfo=None)
    except ValueError as exc:
        raise ValueError(
            "Document snapshot has an invalid captured timestamp."
        ) from exc


def _safe_snapshot_provenance(value) -> dict:
    """Allowlist bounded provider provenance before storing it in JSON settings."""

    if not isinstance(value, dict):
        return {}
    allowed = {
        "knowledge_source_id": 128,
        "knowledge_source_version_id": 128,
        "knowledge_source_version_number": 32,
        "knowledge_source_content_hash": 64,
    }
    safe = {}
    for key, maximum in allowed.items():
        raw = value.get(key)
        if raw is None:
            continue
        if not isinstance(raw, (str, int)) or isinstance(raw, bool):
            continue
        normalized = str(raw).strip()
        if normalized and len(normalized) <= maximum:
            safe[key] = normalized
    if len(json.dumps(safe, sort_keys=True).encode("utf-8")) > 512:
        return {}
    return safe


def _document_snapshot(provider, document_id: str, *, strict: bool = False) -> dict:
    """Read and validate one provider snapshot.

    Legacy projects retain the historical compact snapshot shape.  The block
    engine asks for the provider's complete attested DTO and validates it at
    this boundary before handing it to the deterministic planner.
    """

    snapshot = provider.client.get_document_snapshot(str(document_id or ""))
    if not isinstance(snapshot, dict):
        raise ValueError("Document source returned an invalid snapshot.")
    output_html = snapshot.get("output_html")
    content_hash = str(snapshot.get("content_hash") or "").lower()
    if not isinstance(output_html, str) or not output_html.strip():
        raise ValueError("Completed document has no HTML output.")
    encoded_html = encoded_document_snapshot_html(output_html)
    calculated = hashlib.sha256(encoded_html).hexdigest()
    if len(content_hash) != 64 or content_hash != calculated:
        raise ValueError("Document snapshot integrity check failed.")
    if str(snapshot.get("status") or "").lower() != "completed":
        raise ValueError("Document processing has not completed.")
    source_id = str(snapshot.get("document_id") or snapshot.get("id") or "").strip()
    if not source_id:
        raise ValueError("Document snapshot has no document identifier.")
    if len(source_id) > MAX_SOURCE_DOCUMENT_ID_CHARS:
        raise ValueError("Document snapshot identifier must be at most 36 characters.")
    compact = {
        "document_id": source_id,
        "output_html": output_html,
        "content_hash": content_hash,
        "captured_at": _parse_snapshot_time(snapshot.get("captured_at")),
        "filename": str(
            snapshot.get("filename") or snapshot.get("title") or ""
        ).strip(),
        "provenance": _safe_snapshot_provenance(snapshot.get("provenance")),
    }
    if not strict:
        return compact
    try:
        attested = AttestedDocumentSnapshot.from_mapping(snapshot)
    except (BlockContractError, TypeError, ValueError) as exc:
        raise ValueError(
            "Document source did not return a completed strict semantic snapshot."
        ) from exc
    # Keep the complete DTO, including metadata and semantic contract
    # witnesses, for deterministic block planning.
    return attested.to_dict()


def _create_subtitle_translation_project(
    data, name: str, source_lang: str, target_lang: str
):
    subtitle_project_id = data.get("subtitle_project_id")
    try:
        subtitle_project_id = int(subtitle_project_id)
    except TypeError, ValueError:
        return jsonify({"error": "Subtitle project is required."}), 400

    settings = _base_project_settings(data)
    _validate_model_settings(settings, user_id=current_user.id)
    settings["source_type"] = "subtitle_studio"

    project = TranslationProject(
        user_id=current_user.id,
        name=name,
        source_lang=source_lang or "Auto-detect",
        target_lang=target_lang,
        document_id=None,
        status="setup",
        engine_contract=LEGACY_ENGINE_CONTRACT,
        settings=settings,
        skeleton_html="",
    )
    project, created = create_project_idempotently(
        project,
        idempotency_key=_idempotency_key(),
        request_payload=_project_creation_request_payload(
            name=name,
            source_lang=source_lang,
            target_lang=target_lang,
            source_type="subtitle_studio",
            source_id=subtitle_project_id,
            settings=settings,
            engine_contract=LEGACY_ENGINE_CONTRACT,
        ),
    )
    if not created:
        return _project_creation_reuse_response(project)

    try:
        provider = _subtitle_source_provider(current_user.id)
        source = provider.client.prepare_translation_source(subtitle_project_id)
        if not isinstance(source, dict) or not isinstance(source.get("project"), dict):
            raise TypeError("invalid subtitle source payload")
        subtitle_project = subtitle_project_metadata(source["project"])
        if subtitle_project["id"] != str(subtitle_project_id):
            raise TypeError("subtitle provider returned a different project")
    except CapabilityUseError:
        raise
    except Exception as exc:
        logger.exception(
            "Subtitle source lookup failed for project %s", subtitle_project_id
        )
        raise SourceUnavailableError(
            "The configured subtitle source is unavailable."
        ) from exc

    raw_units = source.get("units")
    if not isinstance(raw_units, list):
        raise ValueError("Subtitle source contains no translatable units.")
    if len(raw_units) > MAX_SUBTITLE_UNITS:
        raise ValueError("Subtitle source contains too many translation units.")
    units = []
    for unit in raw_units:
        if not isinstance(unit, dict):
            raise ValueError("Subtitle unit metadata must be an object.")
        raw_text = unit.get("source_text")
        if raw_text is None:
            raw_text = unit.get("text")
        if not isinstance(raw_text, str):
            raise ValueError("Subtitle unit source text must be text.")
        text = raw_text.strip()
        if not text:
            continue
        if len(text) > MAX_SUBTITLE_UNIT_CHARS:
            raise ValueError("Subtitle unit source text exceeds 50,000 characters.")
        units.append((subtitle_unit_metadata(unit), text))
    if not units:
        raise ValueError("Unable to build sentence units from subtitle cues.")

    settings.update(
        {
            "subtitle_project_id": subtitle_project["id"],
            "source_reference": provider.reference(
                resource_type="subtitle_project",
                resource_id=subtitle_project["id"],
            ),
            "subtitle": {
                "source_project_id": subtitle_project["id"],
                "source_project_name": subtitle_project.get("name", ""),
                "source_media_filename": subtitle_project.get("media_filename"),
                "render_settings": subtitle_render_settings(
                    source.get("render_settings")
                ),
                "units": {},
            },
        }
    )
    project.source_lang = (
        source_lang
        if source_lang and source_lang != "Auto-detect"
        else subtitle_project.get("language") or "Auto-detect"
    )

    placeholders = []
    snapshot_units = {}
    snapshot_budget = 128
    for index, (unit, text) in enumerate(units):
        snapshot_budget += subtitle_snapshot_entry_bytes(unit, text)
        if snapshot_budget > MAX_SOURCE_SNAPSHOT_BYTES:
            raise ValueError("Subtitle source snapshot exceeds the 20 MiB limit.")
        segment_id = str(uuid.uuid4())
        segment = TranslationSegment(
            id=segment_id,
            project_id=project.id,
            order_index=index,
            block_index=index,
            html_tag="subtitle",
            outer_tag="subtitle",
            original_text=text,
            status="pending",
        )
        db.session.add(segment)
        settings["subtitle"]["units"][segment_id] = unit
        snapshot_units[segment_id] = {
            **settings["subtitle"]["units"][segment_id],
            "source_text": text,
        }
        placeholders.append(
            f'<p data-subtitle-unit="{segment_id}">{{{{{segment_id}}}}}</p>'
        )

    project.skeleton_html = "\n".join(placeholders)
    project.settings = settings
    snapshot_payload = {
        "source_type": "subtitle_studio",
        "project_id": subtitle_project["id"],
        "units": snapshot_units,
    }
    project.source_snapshot = json.dumps(
        snapshot_payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    project.source_content_hash = hashlib.sha256(
        encoded_subtitle_snapshot(project.source_snapshot)
    ).hexdigest()
    project.source_captured_at = utc_now_db()
    db.session.commit()

    payload = _serialize_project(project)
    payload["subtitle_unit_count"] = len(units)
    if settings["enable_pre_analysis"]:
        operation, error_response = _publish_operation(
            project,
            kind="analyze_terminology",
            handler="analyze_terminology",
            request_payload={"source_content_hash": project.source_content_hash},
        )
        if error_response is not None:
            _discard_created_project_without_delivery(project)
            return error_response
        payload["operation"] = _safe_operation_projection(operation)
        payload["latest_operation"] = _safe_operation_projection(operation)
        payload["job_id"] = operation.queue_job_id
        payload["status_url"] = _job_status_url(project.id, operation.queue_job_id)
    else:
        payload["pre_analysis"] = {"enabled": False, "state": "not_requested"}
    return jsonify(payload), 201


@blueprint.route("/api/projects/<project_id>/source-planning/retry", methods=["POST"])
@login_required
def retry_source_planning(project_id):
    from .source_planning import is_async_source_operation

    project = TranslationProject.query.filter_by(id=project_id, user_id=current_user.id).first_or_404()
    latest = _latest_operation(project)
    if latest is None or not is_async_source_operation(latest):
        return jsonify({"error": "This project has no asynchronous source-planning operation."}), 409
    if latest.state not in {"failed", "queued", "running"}:
        return jsonify({"error": "Only failed or already-active planning can be retried."}), 409
    operation, error_response = _repair_reused_document_ingestion(project)
    if error_response is not None:
        return error_response
    return jsonify({"project_id": project.id, "operation": _safe_operation_projection(operation)}), 202


@blueprint.route("/api/projects", methods=["POST"])
@login_required
def create_project():
    data = request.get_json(silent=True) or request.form
    try:
        name, source_lang, target_lang, source_type, document_id = (
            _validate_create_project_request(data)
        )
        engine_contract = _requested_engine_contract(data, source_type)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    if source_type == "subtitle_studio":
        try:
            return _create_subtitle_translation_project(
                data, name, source_lang, target_lang
            )
        except ModelNotReadyError as exc:
            db.session.rollback()
            return _model_error(exc)
        except CapabilityUseError:
            db.session.rollback()
            raise
        except TranslationProjectIdempotencyConflict as exc:
            db.session.rollback()
            return _project_creation_conflict_response(exc)
        except (
            LookupError,
            PermissionError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as exc:
            db.session.rollback()
            return jsonify({"error": str(exc)}), 400
        except Exception:
            db.session.rollback()
            logger.exception("Error creating subtitle translation project")
            return jsonify(
                {"error": "Unable to create subtitle translation project."}
            ), 500

    if not document_id:
        return jsonify({"error": "Source document is required"}), 400

    try:
        settings = _base_project_settings(data)
        settings["translation_unit_mode"] = data.get("translation_unit_mode")
        _validate_block_model_settings(settings, user_id=current_user.id)
    except ModelNotReadyError as exc:
        return _model_error(exc)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    settings["source_type"] = "document"

    try:
        project = TranslationProject(
            user_id=current_user.id,
            name=name,
            source_lang=source_lang,
            target_lang=target_lang,
            document_id=document_id,
            status="setup",
            engine_contract=engine_contract,
            settings=settings,
        )
        project, created = create_project_idempotently(
            project,
            idempotency_key=_idempotency_key(),
            request_payload=_project_creation_request_payload(
                name=name,
                source_lang=source_lang,
                target_lang=target_lang,
                source_type="document",
                source_id=document_id,
                settings=settings,
                engine_contract=engine_contract,
            ),
        )
        if not created:
            _, error_response = _repair_reused_document_ingestion(project)
            if error_response is not None:
                return error_response
            return _project_creation_reuse_response(project)
        try:
            document_provider = _document_source_provider(current_user.id)
            semantic_export_provider = _semantic_export_provider(current_user.id)
            snapshot = _document_snapshot(
                document_provider,
                document_id,
                strict=True,
            )
        except CapabilityUseError:
            raise
        except Exception as exc:
            logger.exception("Document source lookup failed during project creation")
            raise SourceUnavailableError(
                "The configured document source is unavailable."
            ) from exc
        project.document_id = snapshot["document_id"]
        settings["source_reference"] = document_provider.reference(
            resource_type="document",
            resource_id=snapshot["document_id"],
        )
        if semantic_export_provider is not None:
            settings[SEMANTIC_EXPORT_REFERENCE_KEY] = (
                semantic_export_provider.provider_reference()
            )
        if snapshot["provenance"]:
            settings["source_snapshot_provenance"] = snapshot["provenance"]
        settings["source_snapshot_label"] = str(
            snapshot.get("filename") or snapshot.get("title") or "Snapshotted document"
        )[:255]
        if data.get("defer_source_planning") is True:
            from .source_planning import freeze_planning_checkpoint, planning_payload

            project.settings = settings
            flag_modified(project, "settings")
            checkpoint = freeze_planning_checkpoint(project, snapshot, planner_settings={
                "engine_contract": DOCUMENT_BLOCK_ENGINE_CONTRACT, "source_type": "document",
                **({TRANSLATION_MAX_BLOCK_CHARS_SETTING: settings[TRANSLATION_MAX_BLOCK_CHARS_SETTING]}
                   if settings["translation_unit_mode"] == "aligned_sentences" else {}),
            })
            request_payload = planning_payload(project.source_content_hash)
            operation, error_response = _publish_operation(
                project, kind="plan_document_blocks", handler="plan_document_blocks",
                request_payload=request_payload, queue_payload=request_payload,
                idempotency_key=f"initial-source-{project.source_content_hash[:48]}",
                initialize_operation=lambda op, created: setattr(op, "checkpoint_json", checkpoint) if created else None,
            )
            if error_response is not None:
                return error_response
            payload = _serialize_project(project)
            payload["operation"] = _safe_operation_projection(operation)
            payload["latest_operation"] = _safe_operation_projection(operation)
            payload["job_id"] = operation.queue_job_id
            payload["status_url"] = _job_status_url(project.id, operation.queue_job_id)
            return jsonify(payload), 201

        sentence_splitter = _source_sentence_splitter(current_user.id, settings)
        project.settings = settings
        flag_modified(project, "settings")
        project.engine_contract = engine_contract
        # The provider reference is persisted on the project before the
        # planner derives its attestation. Plan rows and the operation outbox
        # commit together, so a retry can only reuse exact source/plan pins.
        plan_record, _ = plan_and_persist_source_snapshot(
            project,
            snapshot,
            source_language=source_lang,
            language_profile=None,
            sentence_segmenter=sentence_splitter,
            settings={
                "engine_contract": DOCUMENT_BLOCK_ENGINE_CONTRACT,
                "source_type": "document",
                **(
                    {
                        TRANSLATION_MAX_BLOCK_CHARS_SETTING: settings[
                            TRANSLATION_MAX_BLOCK_CHARS_SETTING
                        ]
                    }
                    if settings["translation_unit_mode"] == "aligned_sentences"
                    else {}
                ),
            },
        )
        operation, error_response = _publish_operation(
            project,
            kind="plan_document_blocks",
            handler="plan_document_blocks",
            request_payload={
                "source_content_hash": plan_record.source_content_hash,
                "source_plan_id": plan_record.id,
                "source_plan_hash": plan_record.plan_hash,
                "engine_contract": DOCUMENT_BLOCK_ENGINE_CONTRACT,
            },
            idempotency_key=f"initial-plan-{plan_record.plan_hash[:48]}",
            source_plan_id=plan_record.id,
            source_plan_hash=plan_record.plan_hash,
        )
        if error_response is not None:
            _discard_created_project_without_delivery(project)
            return error_response

        payload = _serialize_project(project)
        payload["operation"] = _safe_operation_projection(operation)
        payload["latest_operation"] = _safe_operation_projection(operation)
        payload["job_id"] = operation.queue_job_id
        payload["status_url"] = _job_status_url(project.id, operation.queue_job_id)
        return jsonify(payload), 201
    except SourceUnavailableError as exc:
        db.session.rollback()
        return jsonify({"error": str(exc)}), 400
    except BlockContractError as exc:
        db.session.rollback()
        if str(exc).startswith("source-sentence-segmentation-"):
            return jsonify(
                {
                    "error": "The Sentence Segmenter could not produce a complete, exact source sentence inventory. Check the local segmenter and retry; the project was not created.",
                    "code": "source_sentence_segmentation_failed",
                }
            ), 400
        logger.exception("Unable to validate the new translation source plan")
        return jsonify(
            {"error": "Unable to validate the translation source plan."}
        ), 400
    except TranslationProjectIdempotencyConflict as exc:
        db.session.rollback()
        return _project_creation_conflict_response(exc)
    except Exception:
        db.session.rollback()
        logger.exception("Error creating translation project")
        return jsonify({"error": "Unable to create translation project."}), 500


@blueprint.route("/api/projects/<project_id>/segments", methods=["GET"])
@project_access_required
def get_segments(project_id):
    segment_guard = _segment_surface_guard(g.project)
    if segment_guard is not None:
        return segment_guard
    requested_pagination = "limit" in request.args or "offset" in request.args
    try:
        limit = min(500, max(1, int(request.args.get("limit", 200))))
        offset = max(0, int(request.args.get("offset", 0)))
    except ValueError:
        return jsonify({"error": "limit and offset must be whole numbers."}), 400
    include_hidden = request.args.get("include_hidden") == "1" and not bool(
        getattr(g, "is_guest", False)
    )
    query = TranslationSegment.query.filter_by(project_id=g.project.id)
    if not include_hidden:
        query = query.filter(TranslationSegment.merged_into_id.is_(None))
    query = query.order_by(TranslationSegment.order_index, TranslationSegment.id)
    total = query.count()
    segments = query.offset(offset).limit(limit).all()
    payload = [segment.to_dict() for segment in segments]
    if not requested_pagination:
        return jsonify(payload)
    return jsonify(
        {
            "segments": payload,
            "pagination": {
                "offset": offset,
                "limit": limit,
                "total": total,
                "has_more": offset + len(payload) < total,
            },
        }
    )


@blueprint.route("/api/projects/<project_id>/segments/<segment_id>", methods=["PUT"])
@project_access_required
def update_segment(project_id, segment_id):
    segment_guard = _segment_surface_guard(g.project)
    if segment_guard is not None:
        return segment_guard
    if bool(getattr(g, "is_guest", False)):
        return _guest_forbidden("Shared workspaces are read-only.")
    segment = TranslationSegment.query.filter_by(
        id=segment_id, project_id=g.project.id
    ).first_or_404()
    is_guest = bool(getattr(g, "is_guest", False))

    data = request.get_json(silent=True) or {}
    new_text = data.get("translated_text")
    status = str(data.get("status", "human_approved")).strip().lower()
    if status not in {"approved", "flagged", "human_approved", "needs_review", "error"}:
        return jsonify({"error": "Invalid segment review status."}), 400
    if new_text is not None:
        if not isinstance(new_text, str):
            return jsonify({"error": "translated_text must be a string."}), 400
        if len(new_text) > 200_000:
            return jsonify(
                {"error": "Translated text exceeds 200,000 characters."}
            ), 400
    expected_version = data.get("expected_version")
    if expected_version is None:
        return jsonify({"error": "expected_version is required."}), 428
    try:
        expected_version = int(expected_version)
    except TypeError, ValueError:
        return jsonify({"error": "expected_version must be an integer."}), 400
    if expected_version < 1:
        return jsonify({"error": "expected_version must be positive."}), 400

    text_changed = new_text is not None and new_text != segment.translated_text
    changed = text_changed or status != segment.status

    if changed:
        values = {
            "translated_text": new_text
            if new_text is not None
            else segment.translated_text,
            "status": status,
            "context_note": None if status != "error" else segment.context_note,
            "version": TranslationSegment.version + 1,
        }
        update_query = TranslationSegment.query.filter_by(
            id=segment.id, project_id=g.project.id
        )
        update_query = update_query.filter(
            TranslationSegment.version == expected_version
        )
        won = update_query.update(values, synchronize_session=False)
        if won != 1:
            db.session.rollback()
            latest = TranslationSegment.query.filter_by(
                id=segment_id, project_id=g.project.id
            ).first_or_404()
            return jsonify(
                {
                    "error": "This segment changed before your edit was saved.",
                    "latest_segment": latest.to_dict(),
                }
            ), 409
        if text_changed:
            TranslationNote.query.filter_by(
                project_id=g.project.id,
                segment_id=segment.id,
                author_type="model",
                state="active",
            ).update(
                {
                    TranslationNote.state: "suppressed",
                    TranslationNote.version: TranslationNote.version + 1,
                },
                synchronize_session=False,
            )
            db.session.add(
                TranslationRevision(
                    segment_id=segment.id,
                    translated_text=new_text,
                    author_type="guest" if is_guest else "human",
                    user_id=None if is_guest else current_user.id,
                )
            )
        db.session.commit()
        db.session.expire_all()
        segment = TranslationSegment.query.filter_by(
            id=segment.id, project_id=g.project.id
        ).first_or_404()

    return jsonify({"success": True, "segment": segment.to_dict()})


@blueprint.route(
    "/api/projects/<project_id>/segments/<segment_id>/translate", methods=["POST"]
)
@project_access_required
def translate_single_segment(project_id, segment_id):
    segment_guard = _segment_surface_guard(g.project)
    if segment_guard is not None:
        return segment_guard
    if bool(getattr(g, "is_guest", False)):
        return _guest_forbidden(
            "Guests cannot start AI translation jobs. Ask the project owner to run them."
        )

    segment = TranslationSegment.query.filter_by(
        id=segment_id, project_id=g.project.id
    ).first_or_404()
    data = request.get_json(silent=True)
    if data is None:
        data = {}
    if not isinstance(data, dict):
        return jsonify({"error": "Translation request must be a JSON object."}), 400
    expected_version = data.get("expected_version")
    if expected_version is None:
        return jsonify({"error": "expected_version is required."}), 428
    try:
        expected_version = int(expected_version)
    except TypeError, ValueError:
        return jsonify({"error": "expected_version must be an integer."}), 400
    if expected_version != segment.version:
        return jsonify(
            {
                "error": "Segment changed before translation.",
                "latest_segment": segment.to_dict(),
            }
        ), 409

    try:
        _validate_model_settings(
            dict(g.project.settings or {}), user_id=current_user.id
        )
        operation, error_response = _publish_operation(
            g.project,
            kind="translate_segment",
            handler="translate_single_segment",
            request_payload={
                "segment_id": segment.id,
                "segment_version": expected_version,
            },
            queue_payload={
                "segment_id": segment.id,
                "segment_version": expected_version,
            },
        )
        if error_response is not None:
            return error_response
    except ModelNotReadyError as exc:
        return _model_error(exc)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    return _operation_response(operation)


@blueprint.route("/api/projects/<project_id>/context", methods=["GET", "PUT"])
@project_access_required
def manage_context(project_id):
    if request.method == "GET":
        glossary = GlossaryTerm.query.filter_by(project_id=g.project.id).all()
        guest = bool(getattr(g, "is_guest", False))
        return jsonify(
            {
                "working_memory": g.project.working_memory or "",
                "glossary": [term.to_dict() for term in glossary],
                "rag_collection_ids": []
                if guest
                else (g.project.settings or {}).get("rag_collection_ids", []),
            }
        )

    if bool(getattr(g, "is_guest", False)):
        return _guest_forbidden("Shared workspaces are read-only.")
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({"error": "Context must be a JSON object."}), 400
    if "working_memory" in data:
        memory = data["working_memory"]
        if not isinstance(memory, str) or len(memory) > MAX_WORKING_MEMORY_CHARS:
            return jsonify(
                {"error": "working_memory must be text up to 50,000 characters."}
            ), 400
        g.project.working_memory = memory
    if "rag_collection_ids" in data:
        collection_ids = data["rag_collection_ids"]
        if (
            not isinstance(collection_ids, list)
            or len(collection_ids) > MAX_RAG_COLLECTION_IDS
        ):
            return jsonify(
                {"error": "rag_collection_ids must contain at most 50 IDs."}
            ), 400
        if any(
            not isinstance(value, str)
            or not value.strip()
            or len(value.strip()) > MAX_RAG_COLLECTION_ID_CHARS
            for value in collection_ids
        ):
            return jsonify(
                {
                    "error": "rag_collection_ids must contain non-empty IDs up to 128 characters."
                }
            ), 400
        if not g.project.settings:
            g.project.settings = {}
        g.project.settings["rag_collection_ids"] = list(
            dict.fromkeys(value.strip() for value in collection_ids)
        )
        flag_modified(g.project, "settings")

    db.session.commit()
    return jsonify({"success": True})


@blueprint.route("/api/projects/<project_id>", methods=["GET"])
@project_access_required
def get_project(project_id):
    payload = _serialize_project(g.project)
    visible_segments = [
        segment for segment in g.project.segments if segment.merged_into_id is None
    ]
    payload["total_segments"] = len(visible_segments)
    payload["completed_segments"] = sum(
        1
        for segment in visible_segments
        if segment.status in ["approved", "human_approved"]
    )
    return jsonify(payload)


@blueprint.route("/api/projects/<project_id>/workspace_state", methods=["GET"])
@project_access_required
def get_workspace_state(project_id):
    segment_guard = _segment_surface_guard(g.project)
    if segment_guard is not None:
        return segment_guard
    try:
        limit = min(500, max(1, int(request.args.get("limit", 200))))
        offset = max(0, int(request.args.get("offset", 0)))
    except ValueError:
        return jsonify({"error": "limit and offset must be whole numbers."}), 400
    include_hidden = request.args.get("include_hidden") == "1" and not bool(
        getattr(g, "is_guest", False)
    )
    segment_query = TranslationSegment.query.filter_by(project_id=g.project.id)
    if not include_hidden:
        segment_query = segment_query.filter(
            TranslationSegment.merged_into_id.is_(None)
        )
    segment_query = segment_query.order_by(
        TranslationSegment.order_index, TranslationSegment.id
    )
    total_segments = segment_query.count()
    segments = segment_query.offset(offset).limit(limit).all()
    glossary = GlossaryTerm.query.filter_by(project_id=g.project.id).all()
    guest = bool(getattr(g, "is_guest", False))

    return jsonify(
        {
            "project": _serialize_project(g.project),
            "segments": [segment.to_dict() for segment in segments],
            "pagination": {
                "offset": offset,
                "limit": limit,
                "total": total_segments,
                "has_more": offset + len(segments) < total_segments,
            },
            "context": {
                "working_memory": g.project.working_memory or "",
                "glossary": [term.to_dict() for term in glossary],
                "rag_collection_ids": []
                if guest
                else (g.project.settings or {}).get("rag_collection_ids", []),
            },
        }
    )


@blueprint.route("/api/projects/<project_id>", methods=["DELETE"])
@login_required
def delete_project(project_id):
    project = TranslationProject.query.filter_by(
        id=project_id, user_id=current_user.id
    ).first()
    if not project:
        return jsonify({"error": "Project not found"}), 404

    active_operation = reconcile_expired_active_operation(project.id, current_user.id)
    if active_operation is not None:
        request_cancellation(active_operation)
        if active_operation.queue_job_id:
            _translator_queue().request_cancel(
                active_operation.queue_job_id,
                requested_by=current_user.id,
                reason="Project deletion requested.",
            )
        db.session.commit()
        return _operation_conflict_response(
            active_operation,
            message="Cancellation was requested. Delete the project after the operation reaches a terminal state.",
        )

    artifacts = collect_project_deletion_artifacts(project)
    delete_project_aggregate(project)
    db.session.commit()
    # The database deletion is authoritative. Physical vector cleanup remains
    # best-effort after commit so an outage cannot leave the project half-deleted.
    cleanup_deleted_project_vectors(current_app, artifacts)
    return jsonify({"success": True})


@blueprint.route("/api/projects/<project_id>/cancel", methods=["POST"])
@login_required
def cancel_project_jobs(project_id):
    project = TranslationProject.query.filter_by(
        id=project_id, user_id=current_user.id
    ).first()
    if not project:
        return jsonify({"error": "Project not found"}), 404

    operation = reconcile_expired_active_operation(project.id, current_user.id)
    if operation is not None:
        fallback_projection = _safe_operation_projection(operation)
        queue_signal_sent = False
        if operation.queue_job_id:
            try:
                queue_state = _translator_queue().request_cancel(
                    operation.queue_job_id,
                    requested_by=current_user.id,
                    reason="Cancelled from translator workspace.",
                )
                queue_signal_sent = bool(
                    isinstance(queue_state, dict)
                    and (
                        queue_state.get("cancel_requested") is True
                        or str(queue_state.get("status") or "").lower()
                        in {"cancelling", "cancelled"}
                    )
                )
            except Exception:
                logger.exception(
                    "Unable to signal Translator queue cancellation for operation %s",
                    operation.id,
                )
        try:
            operation = request_cancellation(operation)
            db.session.commit()
        except Exception:  # noqa: BLE001 - cancellation should not fake success.
            db.session.rollback()
            logger.exception(
                "Unable to request cancellation for translator operation %s",
                operation.id,
            )
            if queue_signal_sent:
                return (
                    jsonify(
                        {
                            "success": True,
                            "message": (
                                "Cancellation signal sent; durable state will update "
                                "at the next safe worker boundary."
                            ),
                            "operation": fallback_projection,
                        }
                    ),
                    202,
                )
            return jsonify({"error": "Unable to request cancellation."}), 503
        return jsonify(
            {
                "success": True,
                "message": "Cancellation signal sent.",
                "operation": _safe_operation_projection(operation),
            }
        )
    try:
        # Reconciliation may have terminalized an expired cancelling row and
        # returned no active operation. Preserve that durable state change
        # instead of rolling it back when this request closes.
        db.session.commit()
    except Exception:  # noqa: BLE001 - cancellation should not fake success.
        db.session.rollback()
        logger.exception(
            "Unable to commit expired Translator cancellation reconciliation"
        )
        return jsonify({"error": "Unable to reconcile cancellation."}), 503
    return jsonify({"success": False, "message": "No active jobs to cancel."})


def _render_preview_diagrams(value: str | bytes) -> str:
    from carlina.core.diagram_rendering import project_diagrams_html

    try:
        return project_diagrams_html(value.decode("utf-8") if isinstance(value, bytes) else value)
    except (OSError, RuntimeError, ValueError) as exc:
        raise BlockExportError("semantic-preview-diagram-unavailable") from exc


_SHARED_GLOSSARY_NOT_FOUND = {
    "shared-glossary-asset-not-found",
    "shared-glossary-project-not-found",
    "shared-glossary-proposal-not-found",
    "shared-glossary-revision-not-found",
}
_SHARED_GLOSSARY_BAD_REQUEST = {
    "shared-glossary-query-invalid",
    "shared-glossary-request-invalid",
    "shared-glossary-offset",
    "shared-glossary-limit",
    "shared-glossary-proposal-offset",
    "shared-glossary-proposal-limit",
    "shared-glossary-revision",
    "shared-glossary-name",
    "shared-glossary-concept-ids",
    "shared-glossary-retire-ids",
    "shared-glossary-reason",
    "shared-glossary-idempotency-key",
    "shared-glossary-preview-token",
    "shared-glossary-decisions",
    "shared-glossary-selection-limit",
    "shared-glossary-mode",
    "shared-glossary-review-decision",
    "shared-glossary-term-invalid",
    "shared-glossary-payload-too-large",
}
_SHARED_GLOSSARY_MESSAGES = {
    "shared-glossary-owner": "Only an authenticated project owner can manage shared glossaries.",
    "shared-glossary-asset-not-found": "The shared glossary was not found.",
    "shared-glossary-project-not-found": "The translation project was not found.",
    "shared-glossary-proposal-not-found": "The shared glossary proposal was not found.",
    "shared-glossary-revision-not-found": "The requested shared glossary revision was not found.",
    "shared-glossary-language-mismatch": "This glossary uses a different source and target language pair.",
    "shared-glossary-project-busy": "This project has an active operation. Retry after it reaches an idle boundary.",
    "shared-glossary-source-plan-conflict": "The project's source plan changed. Refresh its glossary and preview again.",
    "shared-glossary-preview-stale": "The project glossary changed after this preview. Preview again and choose decisions for the updated comparison.",
    "shared-glossary-revision-stale": "The shared glossary revision changed. Read the latest revision and preview again.",
    "shared-glossary-decisions-required": "Choose incoming or local for every changed term before applying.",
    "shared-glossary-incoming-forbidden": "A human-locked term cannot be replaced by an incoming advisory suggestion.",
    "shared-glossary-detach-required": "Detach the current shared glossary before linking another asset.",
    "shared-glossary-link-required": "Link this glossary before applying an update.",
    "shared-glossary-binding-conflict": "The project glossary binding changed. Refresh the project glossary and retry.",
    "shared-glossary-concept-not-current": "One or more selected terms are no longer in the current project glossary.",
    "shared-glossary-idempotency-conflict": "This retry key was already used for a different request. Use a new key for changed input.",
    "shared-glossary-upstream-conflict": "The shared glossary changed since this contribution was based. Prepare a new contribution.",
    "shared-glossary-proposal-conflict": "This proposal no longer matches the current shared glossary. Prepare a new contribution.",
    "shared-glossary-proposal-reviewed": "This proposal has already been reviewed.",
    "shared-glossary-copy-empty": "Choose at least one incoming term when copying a glossary.",
    "shared-glossary-no-changes": "The selected terms already match this shared revision. There is nothing to contribute.",
    "shared-glossary-decisions": "The decisions must name unique comparison rows and choose incoming or local.",
    "shared-glossary-query-invalid": "The query parameters are invalid or include an unsupported field.",
    "shared-glossary-request-invalid": "The request body is invalid or includes an unsupported field.",
    "shared-glossary-unavailable": "The shared glossary request could not be completed. Retry with the same key if the result is uncertain.",
}


def _shared_glossary_error_response(exc: BaseException):
    db.session.rollback()
    code = getattr(exc, "code", None)
    if isinstance(exc, SharedGlossaryError):
        code = code if isinstance(code, str) and re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,127}", code) else "shared-glossary-unavailable"
        if code in _SHARED_GLOSSARY_NOT_FOUND:
            status = 404
        elif code == "shared-glossary-owner":
            status = 403
        elif code in _SHARED_GLOSSARY_BAD_REQUEST:
            status = 400
        else:
            status = 409
    elif isinstance(exc, LookupError):
        code = "shared-glossary-project-not-found"
        status = 404
    elif isinstance(exc, (ValueError, TypeError)):
        code = "shared-glossary-request-invalid"
        status = 400
    else:
        logger.error("Translator shared glossary request failed (%s).", type(exc).__name__)
        code = "shared-glossary-unavailable"
        status = 503
    message = _SHARED_GLOSSARY_MESSAGES.get(
        code,
        "The shared glossary request could not be completed. Refresh the comparison and retry.",
    )
    return jsonify({"error": message, "code": code}), status


def _shared_glossary_call(operation: Callable[[], dict[str, Any]]):
    try:
        return jsonify(operation())
    except Exception as exc:  # noqa: BLE001 - return only a curated public error.
        return _shared_glossary_error_response(exc)


def _shared_glossary_authenticated_owner_id() -> int | None:
    if (
        not bool(getattr(current_user, "is_authenticated", False))
        or bool(getattr(g, "is_guest", False))
        or getattr(g, "access_role", "owner") != "owner"
    ):
        return None
    owner_id = getattr(current_user, "id", None)
    if type(owner_id) is not int or owner_id <= 0:
        return None
    return owner_id


def _shared_glossary_global_owner_guard():
    owner_id = _shared_glossary_authenticated_owner_id()
    if owner_id is None:
        return None, (jsonify({
            "error": "Only an authenticated project owner can manage shared glossaries.",
            "code": "shared-glossary-owner-only",
        }), 403)
    return owner_id, None


def _shared_glossary_project_owner_guard():
    owner_id, error = _shared_glossary_global_owner_guard()
    project_owner_id = getattr(g.project, "user_id", None)
    if error is not None or type(project_owner_id) is not int or project_owner_id != owner_id:
        return None, (jsonify({
            "error": "Only the project owner can manage its shared glossary.",
            "code": "shared-glossary-owner-only",
        }), 403)
    return owner_id, None


def _shared_glossary_client(owner_id: int):
    from .api_client import TranslatorClient

    return TranslatorClient(
        blueprint.addon_context,
        {"current_user_id": owner_id},
    )


def _shared_glossary_body(required: set[str], optional: set[str] | None = None):
    body = request.get_json(silent=True)
    allowed = required | (optional or set())
    if not isinstance(body, dict) or not required.issubset(body) or not set(body).issubset(allowed):
        return None, _shared_glossary_error_response(
            SharedGlossaryError("shared-glossary-request-invalid")
        )
    return body, None


def _shared_glossary_query(allowed: set[str]) -> dict[str, str]:
    if not set(request.args).issubset(allowed):
        raise SharedGlossaryError("shared-glossary-query-invalid")
    values = {}
    for name in request.args:
        items = request.args.getlist(name)
        if len(items) != 1:
            raise SharedGlossaryError("shared-glossary-query-invalid")
        values[name] = items[0]
    return values


def _shared_glossary_query_integer(
    values: Mapping[str, str], name: str, default: int, *, minimum: int, maximum: int
) -> int:
    raw = values.get(name)
    if raw is None:
        return default
    if not re.fullmatch(r"(?:0|[1-9][0-9]*)", raw):
        raise SharedGlossaryError(f"shared-glossary-{name.replace('_', '-')}")
    value = int(raw)
    if not minimum <= value <= maximum:
        raise SharedGlossaryError(f"shared-glossary-{name.replace('_', '-')}")
    return value


@blueprint.route("/api/shared-glossaries", methods=["GET"])
@login_required
def shared_glossary_list_route():
    owner_id, error = _shared_glossary_global_owner_guard()
    if error is not None:
        return error

    def read():
        values = _shared_glossary_query({"offset", "limit"})
        offset = _shared_glossary_query_integer(values, "offset", 0, minimum=0, maximum=2_147_483_647)
        limit = _shared_glossary_query_integer(values, "limit", 50, minimum=1, maximum=100)
        return _shared_glossary_client(owner_id).shared_glossary_list(
            offset=offset, limit=limit
        )

    return _shared_glossary_call(read)


@blueprint.route("/api/shared-glossaries", methods=["POST"])
@login_required
def shared_glossary_create_route():
    owner_id, error = _shared_glossary_global_owner_guard()
    if error is not None:
        return error
    body, error = _shared_glossary_body(
        {"project_id", "name", "concept_ids", "idempotency_key"}
    )
    if error is not None:
        return error
    return _shared_glossary_call(lambda: _shared_glossary_client(owner_id).shared_glossary_create(
        body["project_id"], name=body["name"], concept_ids=body["concept_ids"],
        idempotency_key=body["idempotency_key"],
    ))


@blueprint.route("/api/shared-glossaries/<asset_id>", methods=["GET"])
@login_required
def shared_glossary_get_route(asset_id: str):
    owner_id, error = _shared_glossary_global_owner_guard()
    if error is not None:
        return error

    def read():
        values = _shared_glossary_query(
            {"revision", "offset", "limit", "proposal_offset", "proposal_limit"}
        )
        revision = (
            _shared_glossary_query_integer(
                values, "revision", 1, minimum=1, maximum=2_147_483_647
            )
            if "revision" in values else None
        )
        offset = _shared_glossary_query_integer(values, "offset", 0, minimum=0, maximum=2_147_483_647)
        limit = _shared_glossary_query_integer(values, "limit", 50, minimum=1, maximum=100)
        proposal_offset = _shared_glossary_query_integer(
            values, "proposal_offset", 0, minimum=0, maximum=2_147_483_647
        )
        proposal_limit = _shared_glossary_query_integer(
            values, "proposal_limit", 10, minimum=1, maximum=100
        )
        return _shared_glossary_client(owner_id).shared_glossary_get(
            asset_id,
            revision=revision,
            offset=offset,
            limit=limit,
            proposal_offset=proposal_offset,
            proposal_limit=proposal_limit,
        )

    return _shared_glossary_call(read)


@blueprint.route("/api/projects/<project_id>/shared-glossary", methods=["GET"])
@project_access_required
def shared_glossary_project_route(project_id: str):
    owner_id, error = _shared_glossary_project_owner_guard()
    if error is not None:
        return error

    def read():
        values = _shared_glossary_query({"offset", "limit"})
        offset = _shared_glossary_query_integer(values, "offset", 0, minimum=0, maximum=2_147_483_647)
        limit = _shared_glossary_query_integer(values, "limit", 100, minimum=1, maximum=100)
        return _shared_glossary_client(owner_id).shared_glossary_project(
            project_id, offset=offset, limit=limit
        )

    return _shared_glossary_call(read)


@blueprint.route("/api/projects/<project_id>/shared-glossary/preview", methods=["POST"])
@project_access_required
def shared_glossary_preview_route(project_id: str):
    owner_id, error = _shared_glossary_project_owner_guard()
    if error is not None:
        return error
    body, error = _shared_glossary_body({"asset_id", "revision", "mode"})
    if error is not None:
        return error
    return _shared_glossary_call(lambda: _shared_glossary_client(owner_id).shared_glossary_preview(
        project_id, asset_id=body["asset_id"], revision=body["revision"], mode=body["mode"]
    ))


@blueprint.route("/api/projects/<project_id>/shared-glossary/apply", methods=["POST"])
@project_access_required
def shared_glossary_apply_route(project_id: str):
    owner_id, error = _shared_glossary_project_owner_guard()
    if error is not None:
        return error
    body, error = _shared_glossary_body(
        {"asset_id", "revision", "mode", "preview_token", "decisions", "idempotency_key"}
    )
    if error is not None:
        return error
    return _shared_glossary_call(lambda: _shared_glossary_client(owner_id).shared_glossary_apply(
        project_id,
        asset_id=body["asset_id"], revision=body["revision"], mode=body["mode"],
        preview_token=body["preview_token"], decisions=body["decisions"],
        idempotency_key=body["idempotency_key"],
    ))


@blueprint.route("/api/projects/<project_id>/shared-glossary/detach", methods=["POST"])
@project_access_required
def shared_glossary_detach_route(project_id: str):
    owner_id, error = _shared_glossary_project_owner_guard()
    if error is not None:
        return error
    body, error = _shared_glossary_body({"expected_version", "idempotency_key"})
    if error is not None:
        return error
    return _shared_glossary_call(lambda: _shared_glossary_client(owner_id).shared_glossary_detach(
        project_id,
        expected_version=body["expected_version"],
        idempotency_key=body["idempotency_key"],
    ))


@blueprint.route("/api/shared-glossaries/<asset_id>/proposals", methods=["POST"])
@login_required
def shared_glossary_contribute_route(asset_id: str):
    owner_id, error = _shared_glossary_global_owner_guard()
    if error is not None:
        return error
    body, error = _shared_glossary_body(
        {"project_id", "expected_revision", "concept_ids", "reason", "idempotency_key"},
        {"retire_ids"},
    )
    if error is not None:
        return error
    return _shared_glossary_call(lambda: _shared_glossary_client(owner_id).shared_glossary_contribute(
        body["project_id"],
        asset_id=asset_id,
        expected_revision=body["expected_revision"],
        concept_ids=body["concept_ids"],
        retire_ids=body.get("retire_ids"),
        reason=body["reason"],
        idempotency_key=body["idempotency_key"],
    ))


@blueprint.route(
    "/api/shared-glossaries/<asset_id>/proposals/<proposal_id>/review",
    methods=["POST"],
)
@login_required
def shared_glossary_review_route(asset_id: str, proposal_id: str):
    owner_id, error = _shared_glossary_global_owner_guard()
    if error is not None:
        return error
    body, error = _shared_glossary_body(
        {"expected_revision", "decision", "idempotency_key"}
    )
    if error is not None:
        return error
    return _shared_glossary_call(lambda: _shared_glossary_client(owner_id).shared_glossary_review(
        asset_id,
        proposal_id=proposal_id,
        expected_revision=body["expected_revision"],
        decision=body["decision"],
        idempotency_key=body["idempotency_key"],
    ))


def _numbering_client():
    from .api_client import TranslatorClient

    owner = int(g.project.user_id)
    return TranslatorClient(
        blueprint.addon_context, {"current_user_id": owner},
        document_provider_factory=lambda reference: _cached_source_provider(
            DOCUMENT_SOURCE_REQUIREMENT, DOCUMENT_SOURCE_CAPABILITY, owner, reference,
        ),
    )


@blueprint.route("/api/projects/<project_id>/numbering", methods=["GET", "POST"])
@project_access_required
def numbering_review(project_id):
    from .numbering_sync import NumberingSyncError

    if bool(getattr(g, "is_guest", False)) or getattr(g, "access_role", "owner") != "owner":
        return jsonify({"error": "Only the project owner can synchronize numbering."}), 403
    try:
        client = _numbering_client()
        if request.method == "GET":
            return jsonify(client.numbering_preview(project_id))
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or set(body) != {
            "preview_token", "selected_ids", "idempotency_key",
        }:
            return jsonify({"error": "Invalid numbering request."}), 400
        return jsonify(client.numbering_apply(project_id, **body))
    except (NumberingSyncError, BlockContractError, ValueError, LookupError, CapabilityUseError) as exc:
        db.session.rollback()
        code = getattr(exc, "code", "numbering-unavailable")
        return jsonify({"error": "Numbering could not be synchronized. Refresh the comparison before trying again.", "code": code}), 409


@blueprint.route("/api/projects/<project_id>/numbering/undo", methods=["POST"])
@project_access_required
def numbering_review_undo(project_id):
    from .numbering_sync import NumberingSyncError

    if bool(getattr(g, "is_guest", False)) or getattr(g, "access_role", "owner") != "owner":
        return jsonify({"error": "Only the project owner can undo numbering synchronization."}), 403
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or set(body) != {"preview_token", "idempotency_key"}:
        return jsonify({"error": "Invalid numbering undo request."}), 400
    try:
        return jsonify(_numbering_client().numbering_undo(project_id, **body))
    except (NumberingSyncError, BlockContractError, ValueError, LookupError, CapabilityUseError) as exc:
        db.session.rollback()
        return jsonify({"error": "Numbering could not be restored. Refresh the comparison before trying again.", "code": getattr(exc, "code", "numbering-unavailable")}), 409


@blueprint.route("/api/projects/<project_id>/numbering/source-preview", methods=["GET"])
@project_access_required
def numbering_source_preview(project_id):
    from carlina.core.semantic_export import SemanticExportRequest
    from .numbering_sync import NumberingSyncError, project_source_numbering

    if bool(getattr(g, "is_guest", False)) or getattr(g, "access_role", "owner") != "owner":
        return jsonify({"error": "Only the project owner can review source numbering."}), 403
    try:
        workspace_plan = load_block_source_plan(g.project)
        html = project_source_numbering(workspace_plan.record)
        snapshot = workspace_plan.plan.snapshot
        export_request = SemanticExportRequest(
            semantic_html=html, semantic_html_sha256=hashlib.sha256(html.encode("utf-8")).hexdigest(),
            metadata=snapshot.metadata, resources=snapshot.resources,
            format="html", include_source_toc=True,
            profile_id="numbering-source-preview", profile_version="1",
        )
        provider = _semantic_export_provider(int(g.project.user_id), g.project.settings)
        result = provider.client.semantic_export(export_request.to_dict())
        data = result.get("data") if isinstance(result, dict) else None
        artifact = result.get("artifact") if isinstance(result, dict) else None
        if (
            not isinstance(data, bytes)
            or not isinstance(artifact, dict)
            or artifact.get("format") != "html"
            or not str(artifact.get("media_type") or "").lower().startswith("text/html")
            or artifact.get("byte_length") != len(data)
            or artifact.get("sha256") != hashlib.sha256(data).hexdigest()
        ):
            raise BlockExportError("semantic-preview-provider-result")
        response = Response(build_semantic_preview_html(data), mimetype="text/html")
        response.headers["Content-Security-Policy"] = "sandbox; default-src 'none'; img-src data:; style-src 'unsafe-inline'"
        response.headers["Cache-Control"] = "private, no-store"
        return response
    except (NumberingSyncError, BlockWorkspaceIntegrityError, BlockContractError, BlockExportError, ValueError, CapabilityUseError) as exc:
        return jsonify({"error": "The source numbering preview is unavailable.", "code": getattr(exc, "code", "numbering-unavailable")}), 409


@blueprint.route("/api/projects/<project_id>/preview", methods=["GET"])
@project_access_required
def get_html_preview(project_id):
    if is_document_block_engine(g.project):
        try:
            workspace_plan = load_block_source_plan(g.project)
            final_html = render_persisted_target_document(workspace_plan.record)
            preview_body: str | bytes = final_html
            if workspace_plan.plan.snapshot.resources:
                provider = _semantic_export_provider(
                    int(g.project.user_id),
                    g.project.settings,
                )
                provider_witness = provider.provider_reference()
                export_request = build_semantic_export_request(
                    workspace_plan.record,
                    export_format="html",
                    provider_witness=provider_witness,
                    renderer=lambda _record: final_html,
                )
                try:
                    result = provider.client.semantic_export(export_request.to_dict())
                except (OSError, ValueError) as exc:
                    raise BlockExportError("semantic-preview-provider-failed") from exc
                if not isinstance(result, dict) or set(result) != {"artifact", "data"}:
                    raise BlockExportError("semantic-preview-provider-result")
                artifact = result["artifact"]
                data = result["data"]
                if (
                    not isinstance(artifact, dict)
                    or set(artifact)
                    != {
                        "format",
                        "media_type",
                        "byte_length",
                        "sha256",
                        "validation_profile",
                        "validation_profile_version",
                    }
                    or artifact.get("format") != "html"
                    or not str(artifact.get("media_type") or "")
                    .lower()
                    .startswith("text/html")
                    or not isinstance(artifact.get("validation_profile"), str)
                    or not artifact["validation_profile"]
                    or not isinstance(artifact.get("validation_profile_version"), str)
                    or not artifact["validation_profile_version"]
                    or not isinstance(data, bytes)
                    or artifact.get("byte_length") != len(data)
                    or artifact.get("sha256") != hashlib.sha256(data).hexdigest()
                ):
                    raise BlockExportError("semantic-preview-provider-result")
                preview_body = data
            response = Response(
                build_semantic_preview_html(_render_preview_diagrams(preview_body)),
                mimetype="text/html",
            )
            response.headers["Content-Security-Policy"] = (
                "sandbox; default-src 'none'; img-src 'self' data:; "
                "style-src 'unsafe-inline'"
            )
            return response
        except (
            BlockWorkspaceIntegrityError,
            BlockContractError,
            BlockExportError,
            CapabilityUseError,
        ) as exc:
            return (
                jsonify(
                    {
                        "error": "The verified block preview is not available.",
                        "code": getattr(exc, "code", "target-preview-invalid"),
                    }
                ),
                409,
            )
    segment_guard = _segment_surface_guard(g.project)
    if segment_guard is not None:
        return segment_guard
    try:
        final_html = reassemble_html_for_export(g.project, for_export=False)
        response = Response(final_html, mimetype="text/html")
        response.headers["Content-Security-Policy"] = (
            "sandbox; default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'"
        )
        return response
    except Exception:  # noqa: BLE001 - rendering boundary returns JSON errors.
        logger.exception(
            "Unable to render translation preview for project %s", g.project.id
        )
        return jsonify({"error": "Unable to render preview."}), 500


@blueprint.route("/api/projects/<project_id>/translate_all", methods=["POST"])
@project_access_required
def trigger_translation(project_id):
    if is_document_block_engine(g.project):
        if bool(getattr(g, "is_guest", False)):
            return _guest_forbidden(
                "Guests cannot start AI translation jobs. "
                "Ask the project owner to run them."
            )
        try:
            workspace_plan = load_block_source_plan(g.project)
            options = _block_translation_request_options(workspace_plan.plan)
            settings = _translation_operation_settings(g.project, options)
            _validate_block_model_settings(settings, user_id=current_user.id)
            effective_context_mode = resolve_translation_context_mode(
                settings,
                override=options["context_mode"],
                execution_mode=settings["execution_mode"],
            )
            source_plan = workspace_plan.record
            execution_profile = resolve_execution_profile(
                g.project,
                model_id=str(settings["translator_model"]),
            )
            window_profile = derive_window_profile(
                execution_profile,
                maximum_blocks=settings.get(
                    "translation_batch_max_blocks",
                    DEFAULT_TRANSLATION_BATCH_MAX_BLOCKS,
                ),
                target_input_tokens=settings.get(
                    "translation_batch_input_tokens",
                    DEFAULT_TRANSLATION_BATCH_INPUT_TOKENS,
                ),
            )
            research = _frozen_block_research(
                settings,
                user_id=current_user.id,
            )
            owner = db.session.get(User, int(current_user.id))
            if owner is None:
                return jsonify({"error": "Project owner not found."}), 404

            def initialize_block_operation(
                operation: TranslationOperation, created: bool
            ) -> None:
                if created:
                    config = freeze_block_workflow_config(
                        operation,
                        source_plan,
                        user=owner,
                        settings=settings,
                        window_profile=window_profile.to_dict(),
                        research=research,
                        protected_targets=snapshot_protected_targets(source_plan),
                        translation_scope=options["scope"],
                        translation_reasoning=options["translation_reasoning"],
                        context_mode=effective_context_mode,
                    )
                    persist_block_workflow_config(operation, source_plan, config)
                    return
                row = TranslationBlockWorkflowConfig.query.filter_by(
                    operation_id=operation.id
                ).one_or_none()
                if row is None:
                    raise BlockContractError("workflow-config-missing")
                rehydrate_block_workflow_config(
                    row,
                    operation=operation,
                    source_plan=source_plan,
                )

            operation, error_response = _publish_operation(
                g.project,
                kind="document_block_translate",
                handler="translate_document_blocks",
                request_payload=_block_translation_request_payload(
                    g.project,
                    source_plan,
                    settings,
                    translation_scope=options["scope"],
                    translation_reasoning=options["translation_reasoning"],
                    context_mode=effective_context_mode,
                ),
                source_plan_id=source_plan.id,
                source_plan_hash=source_plan.plan_hash,
                initialize_operation=initialize_block_operation,
            )
            if error_response is not None:
                return error_response
        except ModelNotReadyError as exc:
            return _model_error(exc)
        except (BlockContractError, BlockWorkspaceIntegrityError) as exc:
            db.session.rollback()
            return (
                jsonify(
                    {
                        "error": "The document-block translation could not be started.",
                        "code": getattr(exc, "code", "block-workflow-invalid"),
                    }
                ),
                409,
            )
        except ValueError as exc:
            db.session.rollback()
            return jsonify({"error": str(exc)}), 400
        return _operation_response(operation)

    segment_guard = _segment_surface_guard(g.project)
    if segment_guard is not None:
        return segment_guard
    if bool(getattr(g, "is_guest", False)):
        return _guest_forbidden(
            "Guests cannot start AI translation jobs. Ask the project owner to run them."
        )

    try:
        _validate_model_settings(
            dict(g.project.settings or {}), user_id=current_user.id
        )
        operation, error_response = _publish_operation(
            g.project,
            kind="translate",
            handler="translate_segments",
            request_payload={"source_content_hash": g.project.source_content_hash},
        )
        if error_response is not None:
            return error_response
    except ModelNotReadyError as exc:
        return _model_error(exc)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    return _operation_response(operation)


@blueprint.route("/api/projects/<project_id>/metrics", methods=["GET"])
@project_access_required
def get_project_metrics(project_id):
    if bool(getattr(g, "is_guest", False)):
        return _guest_forbidden("Shared workspaces cannot access project metrics.")
    stats = (
        db.session.query(
            func.sum(LLMCost.cost).label("total_cost"),
            func.sum(LLMCost.total_tokens).label("total_tokens"),
        )
        .filter(LLMCost.source.like(f"%_proj_{g.project.id}"))
        .first()
    )

    return jsonify(
        {"cost": float(stats.total_cost or 0.0), "tokens": int(stats.total_tokens or 0)}
    )


@blueprint.route("/api/projects/<project_id>/analyze", methods=["POST"])
@project_access_required
def trigger_analysis(project_id):
    segment_guard = _segment_surface_guard(g.project)
    if segment_guard is not None:
        return segment_guard
    if bool(getattr(g, "is_guest", False)):
        return _guest_forbidden(
            "Guests cannot run terminology analysis. Ask the project owner to run it."
        )

    project = g.project
    try:
        _validate_model_settings(
            dict(project.settings or {}), user_id=current_user.id, require_analysis=True
        )
        operation, error_response = _publish_operation(
            project,
            kind="analyze_terminology",
            handler="analyze_terminology",
            request_payload={"source_content_hash": project.source_content_hash},
        )
        if error_response is not None:
            return error_response
    except ModelNotReadyError as exc:
        return _model_error(exc)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return _operation_response(operation)


@blueprint.route(
    "/api/projects/<project_id>/segments/<segment_id>/merge_down", methods=["POST"]
)
@project_access_required
def merge_segment_down(project_id, segment_id):
    segment_guard = _segment_surface_guard(g.project)
    if segment_guard is not None:
        return segment_guard
    if bool(getattr(g, "is_guest", False)):
        return _guest_forbidden("Guests cannot change source structure.")
    active_response = _active_job_response(g.project)
    if active_response is not None:
        return active_response
    seg = TranslationSegment.query.filter_by(
        id=segment_id, project_id=g.project.id
    ).first_or_404()
    if seg.merged_into_id is not None or seg.status == "merged_hidden":
        return jsonify(
            {"error": "A hidden merged segment cannot be merged again."}
        ), 409
    data = request.get_json(silent=True) or {}
    expected_version = data.get("expected_version")
    if expected_version is None:
        return jsonify({"error": "expected_version is required."}), 428
    try:
        expected_version = int(expected_version)
    except TypeError, ValueError:
        return jsonify({"error": "expected_version must be an integer."}), 400
    if expected_version != seg.version:
        return jsonify(
            {"error": "Segment changed before merge.", "latest_segment": seg.to_dict()}
        ), 409

    next_seg = (
        TranslationSegment.query.filter(
            TranslationSegment.project_id == g.project.id,
            TranslationSegment.block_index == seg.block_index,
            TranslationSegment.order_index > seg.order_index,
            TranslationSegment.merged_into_id.is_(None),
        )
        .order_by(TranslationSegment.order_index)
        .first()
    )

    if not next_seg:
        return jsonify({"error": "No segment available below to merge with."}), 400

    vector_ids = [value for value in (seg.vec_id, next_seg.vec_id) if value is not None]
    for affected in (seg, next_seg):
        if affected.translated_text:
            db.session.add(
                TranslationRevision(
                    segment_id=affected.id,
                    translated_text=affected.translated_text,
                    author_type="structure_change",
                    user_id=current_user.id,
                )
            )
    merged_source = f"{seg.original_text} {next_seg.original_text}".strip()
    first_won = TranslationSegment.query.filter_by(
        id=seg.id, project_id=g.project.id, version=expected_version
    ).update(
        {
            "original_text": merged_source,
            "translated_text": None,
            "status": "pending",
            "context_note": None,
            "vec_id": None,
            "version": TranslationSegment.version + 1,
        },
        synchronize_session=False,
    )
    second_won = TranslationSegment.query.filter_by(
        id=next_seg.id, project_id=g.project.id, version=next_seg.version
    ).update(
        {
            "merged_into_id": seg.id,
            "translated_text": None,
            "status": "merged_hidden",
            "context_note": None,
            "vec_id": None,
            "version": TranslationSegment.version + 1,
        },
        synchronize_session=False,
    )
    if first_won != 1 or second_won != 1:
        db.session.rollback()
        latest = TranslationSegment.query.filter_by(
            id=segment_id, project_id=g.project.id
        ).first_or_404()
        return jsonify(
            {
                "error": "Segments changed before merge.",
                "latest_segment": latest.to_dict(),
            }
        ), 409

    db.session.commit()
    db.session.expire_all()
    seg = db.session.get(TranslationSegment, seg.id)
    next_seg = db.session.get(TranslationSegment, next_seg.id)
    _delete_stale_vectors(g.project, vector_ids)
    return jsonify(
        {
            "success": True,
            "segments": [seg.to_dict(), next_seg.to_dict()],
            "retranslation_required": True,
            "warning": "Merged source changed; both translations were archived and must be retranslated.",
        }
    )


@blueprint.route(
    "/api/projects/<project_id>/segments/<segment_id>/split", methods=["POST"]
)
@project_access_required
def split_segment(project_id, segment_id):
    segment_guard = _segment_surface_guard(g.project)
    if segment_guard is not None:
        return segment_guard
    if bool(getattr(g, "is_guest", False)):
        return _guest_forbidden("Guests cannot change source structure.")
    active_response = _active_job_response(g.project)
    if active_response is not None:
        return active_response
    seg = TranslationSegment.query.filter_by(
        id=segment_id, project_id=g.project.id
    ).first_or_404()
    if seg.merged_into_id is not None or seg.status == "merged_hidden":
        return jsonify({"error": "A hidden merged segment cannot be split."}), 409
    data = request.get_json(silent=True) or {}
    raw_part1 = data.get("part1", "")
    raw_part2 = data.get("part2", "")
    if not isinstance(raw_part1, str) or not isinstance(raw_part2, str):
        return jsonify({"error": "Both parts must be strings."}), 400
    part1 = raw_part1.strip()
    part2 = raw_part2.strip()
    expected_version = data.get("expected_version")
    if expected_version is None:
        return jsonify({"error": "expected_version is required."}), 428
    try:
        expected_version = int(expected_version)
    except TypeError, ValueError:
        return jsonify({"error": "expected_version must be an integer."}), 400
    if expected_version != seg.version:
        return jsonify(
            {"error": "Segment changed before split.", "latest_segment": seg.to_dict()}
        ), 409

    if not part1 or not part2:
        return jsonify({"error": "Both parts are required."}), 400
    try:
        _validate_split_partition(g.project, seg, part1, part2)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 409

    subtitle_client = None
    if _project_source_type(g.project) == "subtitle_studio":
        subtitle_client = _subtitle_source_provider(
            g.project.user_id,
            g.project.settings,
        ).client

    vector_ids = [seg.vec_id] if seg.vec_id is not None else []
    if seg.translated_text:
        db.session.add(
            TranslationRevision(
                segment_id=seg.id,
                translated_text=seg.translated_text,
                author_type="structure_change",
                user_id=current_user.id,
            )
        )
    won = TranslationSegment.query.filter_by(
        id=seg.id, project_id=g.project.id, version=expected_version
    ).update(
        {
            "original_text": part1,
            "translated_text": None,
            "status": "pending",
            "context_note": None,
            "vec_id": None,
            "version": TranslationSegment.version + 1,
        },
        synchronize_session=False,
    )
    if won != 1:
        db.session.rollback()
        latest = TranslationSegment.query.filter_by(
            id=segment_id, project_id=g.project.id
        ).first_or_404()
        return jsonify(
            {
                "error": "Segment changed before split.",
                "latest_segment": latest.to_dict(),
            }
        ), 409

    TranslationSegment.query.filter(
        TranslationSegment.project_id == g.project.id,
        TranslationSegment.order_index > seg.order_index,
    ).update({"order_index": TranslationSegment.order_index + 1})

    new_seg_id = str(uuid.uuid4())
    new_seg = TranslationSegment(
        id=new_seg_id,
        project_id=g.project.id,
        order_index=seg.order_index + 1,
        block_index=seg.block_index,
        html_tag=seg.html_tag,
        outer_tag=seg.outer_tag,
        original_text=part2,
        status="pending",
    )
    db.session.add(new_seg)

    if subtitle_client is not None and _split_subtitle_unit_settings(
        g.project.settings,
        seg.id,
        new_seg_id,
        part1,
        part2,
        subtitle_client=subtitle_client,
    ):
        flag_modified(g.project, "settings")

    g.project.skeleton_html = g.project.skeleton_html.replace(
        f"{{{{{seg.id}}}}}", f"{{{{{seg.id}}}}} {{{{{new_seg_id}}}}}", 1
    )

    db.session.commit()
    db.session.expire_all()
    seg = db.session.get(TranslationSegment, seg.id)
    _delete_stale_vectors(g.project, vector_ids)
    return jsonify(
        {
            "success": True,
            "segments": [seg.to_dict(), new_seg.to_dict()],
            "retranslation_required": True,
            "warning": "Split source changed; the earlier translation was archived and both parts must be retranslated.",
        }
    )


@blueprint.route("/api/projects/<project_id>/glossary/upload", methods=["POST"])
@project_access_required
def upload_glossary(project_id):
    import csv
    import io

    if bool(getattr(g, "is_guest", False)):
        return _guest_forbidden("Shared workspaces are read-only.")

    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400

    file = request.files["file"]
    if not file.filename.lower().endswith(".csv"):
        return jsonify({"error": "Only CSV files are supported"}), 400

    try:
        contents = file.stream.read(MAX_GLOSSARY_CSV_BYTES + 1)
        if len(contents) > MAX_GLOSSARY_CSV_BYTES:
            return jsonify({"error": "CSV must be at most 2 MB."}), 413
        try:
            decoded = contents.decode("utf-8")
        except UnicodeDecodeError:
            return jsonify({"error": "CSV must be UTF-8 encoded."}), 400
        stream = io.StringIO(decoded, newline=None)
        csv_input = csv.reader(stream)

        first_row = next(csv_input, None)
        if not first_row:
            return jsonify({"error": "CSV file is empty."}), 400

        rows_to_process = []
        if first_row[0].strip().lower() not in [
            "source",
            "source_term",
            "english",
            "term",
        ]:
            rows_to_process.append(first_row)

        rows_to_process.extend(csv_input)
        if len(rows_to_process) > MAX_GLOSSARY_CSV_ROWS:
            return jsonify({"error": "CSV must contain at most 10,000 rows."}), 413

        added = 0
        updated = 0
        existing_count = GlossaryTerm.query.filter_by(project_id=g.project.id).count()
        for row in rows_to_process:
            if len(row) < 2:
                continue

            source_term = row[0].strip()
            target_term = row[1].strip()
            notes = row[2].strip() if len(row) > 2 else ""

            if not source_term or not target_term:
                continue
            if (
                len(source_term) > MAX_GLOSSARY_TERM_CHARS
                or len(target_term) > MAX_GLOSSARY_TERM_CHARS
                or len(notes) > MAX_GLOSSARY_NOTES_CHARS
            ):
                return jsonify(
                    {"error": "Glossary fields exceed their allowed length."}
                ), 400

            existing = GlossaryTerm.query.filter(
                db.func.lower(GlossaryTerm.source_term) == source_term.lower(),
                GlossaryTerm.project_id == g.project.id,
            ).first()

            if existing:
                existing.target_term = target_term
                existing.context_notes = notes
                existing.is_user_defined = True
                existing.needs_review = False
                existing.status = "approved"
                updated += 1
            else:
                if existing_count + added >= MAX_PROJECT_GLOSSARY_TERMS:
                    db.session.rollback()
                    return jsonify(
                        {
                            "error": "Project glossary capacity has been reached "
                            f"({MAX_PROJECT_GLOSSARY_TERMS:,} terms)."
                        }
                    ), 409
                term = GlossaryTerm(
                    project_id=g.project.id,
                    source_term=source_term,
                    target_term=target_term,
                    context_notes=notes,
                    is_user_defined=True,
                    needs_review=False,
                    status="approved",
                )
                db.session.add(term)
                added += 1

        db.session.commit()
        return jsonify(
            {
                "success": True,
                "message": f"Glossary processed. Added {added}, Updated {updated}.",
            }
        )
    except Exception:  # noqa: BLE001 - endpoint transaction boundary.
        db.session.rollback()
        logger.exception("Glossary upload failed for project %s", g.project.id)
        return jsonify({"error": "Glossary upload failed."}), 500


@blueprint.route("/api/projects/<project_id>/glossary", methods=["POST"])
@project_access_required
def add_glossary_term(project_id):
    if bool(getattr(g, "is_guest", False)):
        return _guest_forbidden("Shared workspaces are read-only.")
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({"error": "Glossary term must be a JSON object."}), 400

    source_term = data.get("source_term")
    target_term = data.get("target_term")
    notes = data.get("context_notes", "")
    if (
        not isinstance(source_term, str)
        or not isinstance(target_term, str)
        or not isinstance(notes, str)
    ):
        return jsonify({"error": "Glossary fields must be text."}), 400
    source_term, target_term, notes = (
        source_term.strip(),
        target_term.strip(),
        notes.strip(),
    )
    if not source_term or not target_term:
        return jsonify({"error": "Source and Target terms are required."}), 400
    if (
        len(source_term) > MAX_GLOSSARY_TERM_CHARS
        or len(target_term) > MAX_GLOSSARY_TERM_CHARS
        or len(notes) > MAX_GLOSSARY_NOTES_CHARS
    ):
        return jsonify({"error": "Glossary fields exceed their allowed length."}), 400

    existing = GlossaryTerm.query.filter(
        db.func.lower(GlossaryTerm.source_term) == source_term.lower(),
        GlossaryTerm.project_id == g.project.id,
    ).first()

    if existing:
        return jsonify(
            {"error": "This source term already exists in the glossary."}
        ), 409
    if (
        GlossaryTerm.query.filter_by(project_id=g.project.id).count()
        >= MAX_PROJECT_GLOSSARY_TERMS
    ):
        return jsonify(
            {
                "error": "Project glossary capacity has been reached "
                f"({MAX_PROJECT_GLOSSARY_TERMS:,} terms)."
            }
        ), 409

    term = GlossaryTerm(
        project_id=g.project.id,
        source_term=source_term,
        target_term=target_term,
        context_notes=notes,
        is_user_defined=True,
        needs_review=False,
        status="approved",
    )
    db.session.add(term)
    db.session.commit()
    return jsonify({"success": True, "term": term.to_dict()})


@blueprint.route(
    "/api/projects/<project_id>/glossary/<int:term_id>", methods=["PUT", "DELETE"]
)
@project_access_required
def manage_glossary_term(project_id, term_id):
    if bool(getattr(g, "is_guest", False)):
        return _guest_forbidden("Shared workspaces are read-only.")
    term = GlossaryTerm.query.filter_by(
        id=term_id, project_id=g.project.id
    ).first_or_404()

    if request.method == "DELETE":
        db.session.delete(term)
        db.session.commit()
        return jsonify({"success": True})

    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({"error": "Glossary term must be a JSON object."}), 400
    if "source_term" in data:
        if not isinstance(data["source_term"], str):
            return jsonify({"error": "source_term must be text."}), 400
        term.source_term = data["source_term"].strip()
    if "target_term" in data:
        if not isinstance(data["target_term"], str):
            return jsonify({"error": "target_term must be text."}), 400
        term.target_term = data["target_term"].strip()
    if "context_notes" in data:
        if not isinstance(data["context_notes"], str):
            return jsonify({"error": "context_notes must be text."}), 400
        term.context_notes = data["context_notes"].strip()
    if "needs_review" in data:
        if not isinstance(data["needs_review"], bool):
            return jsonify({"error": "needs_review must be a boolean."}), 400
        term.needs_review = data["needs_review"]

    term.is_user_defined = True
    if (
        not term.source_term
        or not term.target_term
        or len(term.source_term) > MAX_GLOSSARY_TERM_CHARS
        or len(term.target_term) > MAX_GLOSSARY_TERM_CHARS
        or len(term.context_notes or "") > MAX_GLOSSARY_NOTES_CHARS
    ):
        return jsonify({"error": "Glossary fields exceed their allowed length."}), 400
    duplicate = GlossaryTerm.query.filter(
        db.func.lower(GlossaryTerm.source_term) == term.source_term.lower(),
        GlossaryTerm.project_id == g.project.id,
        GlossaryTerm.id != term.id,
    ).first()
    if duplicate is not None:
        return jsonify(
            {"error": "This source term already exists in the glossary."}
        ), 409
    # A human save is an explicit acceptance.  AI candidates may remain
    # flagged until reviewed, but an editor must not silently leave one live.
    if not term.needs_review:
        term.status = "approved"
    db.session.commit()
    return jsonify({"success": True, "term": term.to_dict()})


@blueprint.route("/api/jobs/<job_id>", methods=["GET"])
@login_required
def get_job_status(job_id):
    # This endpoint used to fall through to arbitrary queue/document jobs.
    # Only a Translator operation creates a user-visible translator job.
    operation = TranslationOperation.query.filter_by(
        queue_job_id=str(job_id), owner_user_id=current_user.id
    ).one_or_none()
    if operation is None:
        return jsonify({"error": "Translation job not found."}), 404
    return jsonify(
        _public_job_status(
            job_id,
            _translator_queue().get_status(job_id),
            project_id=operation.project_id,
            operation=operation,
        )
    )


@blueprint.route("/api/projects/<project_id>/jobs/<job_id>", methods=["GET"])
@project_access_required
def get_project_job_status(project_id, job_id):
    if bool(getattr(g, "is_guest", False)):
        return _guest_forbidden("Shared workspaces cannot access export job results.")
    operation = TranslationOperation.query.filter_by(
        queue_job_id=str(job_id), project_id=g.project.id
    ).one_or_none()
    if operation is None:
        return jsonify({"error": "Translation job not found for this project."}), 404
    if (
        not bool(getattr(g, "is_guest", False))
        and operation.owner_user_id != current_user.id
    ):
        return jsonify({"error": "Translation job not found for this project."}), 404
    return jsonify(
        _public_job_status(
            job_id,
            _translator_queue().get_status(job_id),
            project_id=g.project.id,
            operation=operation,
        )
    )


@blueprint.route("/api/projects/<project_id>/jobs/<job_id>/download", methods=["GET"])
@project_access_required
def download_project_job_result(project_id, job_id):
    if bool(getattr(g, "is_guest", False)):
        return _guest_forbidden("Shared workspaces cannot download packaged exports.")
    operation = TranslationOperation.query.filter_by(
        queue_job_id=str(job_id), project_id=g.project.id
    ).one_or_none()
    if operation is None:
        return jsonify({"error": "Translation job not found for this project."}), 404
    if (
        not bool(getattr(g, "is_guest", False))
        and operation.owner_user_id != current_user.id
    ):
        return jsonify({"error": "Translation job not found for this project."}), 404
    status = _translator_queue().get_status(job_id)
    if status.get("status") != "completed":
        return jsonify({"error": "Job output is not ready."}), 409

    result = status.get("result") if isinstance(status.get("result"), dict) else {}
    if result.get("kind") != "file_download":
        return jsonify({"error": "Job does not have a downloadable result."}), 404

    if result.get("project_id") != g.project.id:
        return jsonify({"error": "Download is not available."}), 404

    try:
        result_user_id = int(result.get("user_id"))
    except TypeError, ValueError:
        return jsonify({"error": "Download is not available."}), 404

    if result_user_id != int(g.project.user_id):
        return jsonify({"error": "Download is not available."}), 404

    output_path = Path(result.get("output_path") or "")
    allowed_suffixes = {".docx", ".epub", ".html", ".pdf", ".txt"}
    expected_root = (
        Path(current_app.instance_path) / "translator_exports" / operation.id
    )
    try:
        resolved_path = output_path.resolve(strict=True)
        resolved_root = expected_root.resolve(strict=False)
    except OSError, RuntimeError:
        return jsonify({"error": "Generated file is no longer available."}), 404
    if (
        not resolved_path.is_file()
        or resolved_path.suffix.lower() not in allowed_suffixes
        or resolved_root not in resolved_path.parents
    ):
        return jsonify({"error": "Generated file is no longer available."}), 404

    return send_file(
        resolved_path,
        as_attachment=True,
        download_name=secure_filename(
            str(result.get("download_name") or resolved_path.name)
        )
        or resolved_path.name,
        mimetype=result.get("mimetype"),
    )


def reassemble_html_for_export(project, for_export=False):
    return render_project_html(project, for_export=bool(for_export))


def _project_source_type(project: TranslationProject) -> str:
    return (project.settings or {}).get("source_type") or "document"


def _effective_export_metadata(
    project: TranslationProject, export_metadata: Mapping[str, object], *, use_uploaded_cover: bool = True
) -> dict[str, object]:
    """Merge per-export overrides over the project's saved export defaults."""

    merged = merge_project_export_metadata(
        project, export_metadata, use_uploaded_cover=use_uploaded_cover
    )
    return pin_export_metadata_cover(merged, user_id=int(project.user_id))


@blueprint.get("/api/projects/<project_id>/export-metadata")
@login_required
def get_export_metadata(project_id):
    """Return only the owner's saved edition defaults for the export dialog."""
    from .api_client import TranslatorClient

    client = TranslatorClient(None, {"current_user_id": current_user.id})
    try:
        result = client.get_document_project(project_id, include_export_metadata=True)
    except LookupError:
        return jsonify({"error": "Translation project not found."}), 404
    payload = dict(result["export_metadata"])
    project = TranslationProject.query.filter_by(id=project_id, user_id=current_user.id).first()
    try:
        snapshot = load_block_source_plan(project).plan.snapshot
    except (BlockContractError, BlockWorkspaceIntegrityError, AttributeError):
        snapshot = None
    if snapshot is not None and str(snapshot.filename or "").lower().endswith(".pdf"):
        candidate = snapshot.metadata.get("cover_candidate")
        source = {"pdf": True, "document_id": snapshot.document_id}
        if isinstance(candidate, Mapping) and type(candidate.get("page")) is int and 1 <= candidate["page"] <= 1_000_000:
            source["candidate"] = {"page": candidate["page"], "evidence": str(candidate.get("evidence") or "")[:500]}
        payload["cover_source"] = source
    return jsonify(payload)


@blueprint.post("/api/projects/<project_id>/export-metadata/generate")
@login_required
def generate_export_metadata(project_id):
    """Return editable model suggestions for the owner's translated edition."""
    from .api_client import TranslatorClient

    if len(request.get_data(cache=True)) > 16_384:
        return jsonify({"error": "Book details are too large."}), 413
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or set(data) - {"metadata", "expected_version", "model_id"}:
        return jsonify({"error": "Supply book details, their version, and a model."}), 400
    try:
        client = TranslatorClient(None, {"current_user_id": current_user.id})
        result = client.export_metadata_generate(
            project_id,
            metadata=data.get("metadata", {}),
            expected_version=data.get("expected_version"),
            model_id=data.get("model_id"),
        )
        return jsonify(result)
    except LookupError:
        return jsonify({"error": "Translation project not found."}), 404
    except (BlockExportError, ExportMetadataError, BlockContractError) as exc:
        conflict = exc.code in {
            "export-metadata-version-conflict", "export-metadata-source-conflict",
            "export-metadata-active-operation",
        }
        return jsonify({"error": "Unable to suggest book details. Check the selected model and refresh the saved details before retrying.", "code": exc.code}), 409 if conflict else 400
    except Exception as exc:
        current_app.logger.warning("Translation metadata proposal failed: %s", type(exc).__name__)
        return jsonify({"error": "The model could not prepare book details. Your existing details are kept."}), 502


@blueprint.route("/api/projects/<project_id>/epub/edition", methods=["GET", "PUT", "DELETE"])
@cast(
    Callable[[Callable[[str], ResponseReturnValue]], Callable[[str], ResponseReturnValue]],
    project_access_required,
)
def project_epub_edition(project_id: str) -> ResponseReturnValue:
    from .epub_editions import EpubEditionStoreError, clear_epub_edition, get_epub_edition, save_epub_edition

    if bool(getattr(g, "is_guest", False)) or getattr(g, "access_role", "owner") != "owner" or int(g.project.user_id) != int(current_user.id):
        return jsonify({"error": "Only the project owner can manage edition choices."}), 403
    if request.args or len(request.get_data(cache=True)) > 200_000:
        return jsonify({"error": "Invalid edition request."}), 400
    try:
        scope = {"owner_id": int(current_user.id), "project_id": project_id}
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


@blueprint.post("/api/projects/<project_id>/epub/preflight")
@cast(
    Callable[
        [Callable[[str], ResponseReturnValue]],
        Callable[[str], ResponseReturnValue],
    ],
    project_access_required,
)
def epub_export_preflight(project_id: str) -> ResponseReturnValue:
    """Review the owner's exact persisted target without producing an artifact."""

    if (
        bool(getattr(g, "is_guest", False))
        or getattr(g, "access_role", "owner") != "owner"
        or int(g.project.user_id) != int(current_user.id)
    ):
        return jsonify({"error": "Only the project owner can review EPUB readiness."}), 403
    if not is_document_block_engine(g.project):
        return jsonify({"error": "EPUB preflight requires a block translation project."}), 409
    raw_body = request.get_data(cache=True)
    if len(raw_body) > 200_000:
        return jsonify({"error": "EPUB preflight options are too large."}), 413
    body = request.get_json(silent=True) if raw_body.strip() else None
    if (
        not isinstance(body, Mapping)
        or not set(body).issubset(
            {"epub_options", "export_metadata", "epub_cover", "numbering_revision", "epub_image_mode"}
        )
        or "epub_options" not in body
    ):
        return jsonify({"error": "Supply EPUB options and supported preflight fields."}), 400

    from carlina.core.epub_cover import normalize_epub_cover
    from carlina.core.epub_export import (
        EpubExportContractError,
        normalize_epub_image_mode,
        normalize_epub_options,
    )

    try:
        options = normalize_epub_options(body["epub_options"])
        image_mode = normalize_epub_image_mode(body.get("epub_image_mode", "jpeg"))
        if not options:
            raise BlockExportError("epub-preflight-profile")
        cover_options = normalize_epub_cover(body.get("epub_cover"))
        request_metadata = normalize_export_metadata(
            body.get("export_metadata", {})
        )
    except EpubExportContractError as exc:
        return jsonify({"error": "EPUB preflight options are invalid.", "code": exc.code}), 400
    except (BlockExportError, ExportMetadataError, ValueError) as exc:
        return jsonify(
            {
                "error": "EPUB preflight options are invalid.",
                "code": getattr(exc, "code", "semantic-export-cover-option"),
            }
        ), 400

    from .numbering_sync import numbering_revision

    try:
        current_revision = numbering_revision(str(g.project.id))
        requested_revision = body.get("numbering_revision", current_revision)
        if type(requested_revision) is not int or requested_revision < 0:
            raise BlockExportError("numbering-export-revision")
        if requested_revision != current_revision:
            raise BlockExportError("numbering-export-revision-conflict")
        plan = load_block_source_plan(g.project)
        provider = _semantic_export_provider(
            int(g.project.user_id), g.project.settings
        )
        provider_witness = provider.provider_reference()
        export_metadata = merge_project_export_metadata(
            g.project,
            request_metadata,
            use_uploaded_cover=not cover_options,
        )
        cover_uri = None
        cover_upload_path = export_metadata.get("cover_upload_path")
        if (
            isinstance(cover_upload_path, str)
            and cover_upload_path
            and not cover_options
        ):
            from .export_cover import resolve_export_cover_path

            cover_uri = resolve_export_cover_path(
                user_id=int(g.project.user_id),
                upload_path=cover_upload_path,
            )
        export_request = build_semantic_export_request(
            plan.record,
            export_format="epub",
            provider_witness=provider_witness,
            export_metadata=export_metadata,
            cover_uri=cover_uri,
            epub_cover=cover_options,
            epub_options=options,
            epub_image_mode=image_mode,
        )
        provider_request = export_request.to_dict()
        expected_review_hash = semantic_export_review_hash(provider_request)
        if numbering_revision(str(g.project.id)) != current_revision:
            raise BlockExportError("numbering-export-revision-conflict")
        raw_result = provider.client.semantic_export_preflight(
            provider_request
        )
        if numbering_revision(str(g.project.id)) != current_revision:
            raise BlockExportError("numbering-export-revision-conflict")
        result = validate_epub_preflight_result(
            raw_result,
            semantic_html_sha256=export_request.semantic_html_sha256,
            epub_options=options,
            expected_review_hash=expected_review_hash,
        )
        result["numbering_revision"] = current_revision
        from .epub_editions import current_source_revision

        result["edition_revision"] = current_source_revision(g.project)
        response = jsonify(result)
        response.headers["Cache-Control"] = "private, no-store"
        return response
    except CapabilityUseError as exc:
        return jsonify(exc.to_dict()), 409
    except (
        BlockContractError,
        BlockWorkspaceIntegrityError,
        BlockExportError,
        ExportMetadataError,
        ExportCoverError,
        LookupError,
        ValueError,
    ) as exc:
        db.session.rollback()
        return jsonify(
            {
                "error": "The verified EPUB preflight is unavailable.",
                "code": getattr(exc, "code", "epub-preflight-unavailable"),
            }
        ), 409


def _delete_stale_vectors(project: TranslationProject, vector_ids: list[int]) -> None:
    """Best-effort physical cleanup after transactionally making vectors unreachable."""

    if not vector_ids:
        return
    try:
        store = translation_memory_store(current_app, project)
        if store is not None:
            store.delete(vector_ids)
    except Exception:  # noqa: BLE001 - DB invalidation is already authoritative.
        logger.exception(
            "Failed to delete stale translation vectors for project %s", project.id
        )


def _translated_subtitle_text(segment: TranslationSegment) -> str:
    return translated_subtitle_text(segment)


def _translated_subtitle_cues(
    project: TranslationProject,
    *,
    subtitle_provider=None,
) -> list[dict]:
    provider = subtitle_provider or _subtitle_source_provider(
        project.user_id,
        project.settings,
    )
    return translated_subtitle_cues(
        project,
        subtitle_provider=provider,
    )


@blueprint.route("/api/projects/<project_id>/export", methods=["GET", "POST"])
@project_access_required
def export_project(project_id):
    if is_document_block_engine(g.project):
        export_format = request.args.get("format", "html").lower()
        if bool(getattr(g, "is_guest", False)):
            return _guest_forbidden(
                "Shared workspaces can preview the verified semantic document, "
                "but only the owner can build downloadable exports."
            )
        if export_format not in {"html", "docx", "epub", "pdf"}:
            return (
                jsonify({"error": "Block exports support HTML, DOCX, EPUB, and PDF."}),
                400,
            )
        if request.method != "POST":
            return jsonify({"error": "Block exports must be started with POST."}), 405
        try:
            raw_body = request.get_data(cache=True)
            body = request.get_json(silent=True) if raw_body.strip() else {}
            if not isinstance(body, Mapping):
                raise BlockExportError("export-metadata-body")
            if not set(body).issubset(
                {
                    "export_metadata",
                    "start_h2_on_new_page",
                    "review_requirement",
                    "epub_cover",
                    "epub_options",
                    "epub_review_hash",
                    "epub_image_mode",
                }
            ):
                raise BlockExportError("export-metadata-key")
            from carlina.core.epub_cover import normalize_epub_cover
            from carlina.core.epub_export import (
                EpubExportContractError,
                normalize_epub_options,
                normalize_epub_image_mode,
            )

            try:
                cover_options = normalize_epub_cover(body.get("epub_cover"))
            except ValueError as exc:
                raise BlockExportError("semantic-export-cover-option") from exc
            if cover_options and export_format != "epub":
                raise BlockExportError("semantic-export-cover-option")
            if "epub_options" in body and export_format != "epub":
                raise BlockExportError("semantic-export-epub-options-format")
            if "epub_review_hash" in body and export_format != "epub":
                raise BlockExportError("epub-review-hash-format")
            supplied_epub_review_hash = "epub_review_hash" in body
            normalized_epub_review_hash = None
            if supplied_epub_review_hash:
                from .block_export import normalize_epub_review_hash

                normalized_epub_review_hash = normalize_epub_review_hash(
                    body["epub_review_hash"]
                )
            supplied_epub_options = "epub_options" in body
            try:
                normalized_image_mode = normalize_epub_image_mode(
                    body.get(
                        "epub_image_mode",
                        "jpeg" if export_format == "epub" else "auto",
                    )
                )
            except EpubExportContractError as exc:
                raise BlockExportError(exc.code) from exc
            if normalized_image_mode != "auto" and export_format != "epub":
                raise BlockExportError("semantic-export-epub-image-mode-format")
            normalized_epub_options = None
            if supplied_epub_options and body["epub_options"] is not None:
                try:
                    normalized_epub_options = normalize_epub_options(
                        body["epub_options"]
                    )
                except EpubExportContractError as exc:
                    raise BlockExportError(exc.code) from exc
            start_h2_on_new_page = body.get("start_h2_on_new_page", False)
            if type(start_h2_on_new_page) is not bool or (
                start_h2_on_new_page and export_format not in {"docx", "pdf"}
            ):
                raise BlockExportError("semantic-export-start-h2")
            request_export_metadata = normalize_export_metadata(
                body.get("export_metadata", {})
            )
            export_metadata = _effective_export_metadata(
                g.project, request_export_metadata, use_uploaded_cover=not cover_options
            )
            review_requirement = body.get("review_requirement", "none")
            if review_requirement not in {"none", "structural", "target_reading", "bilingual"}:
                raise BlockExportError("export-review-requirement")
        except (BlockExportError, ExportCoverError) as exc:
            return (
                jsonify(
                    {
                        "error": "Export metadata is invalid.",
                        "code": exc.code,
                    }
                ),
                400,
            )
        try:
            workspace_plan = load_block_source_plan(g.project)
            provider = _semantic_export_provider(
                int(current_user.id),
                g.project.settings,
            )
            provider_reference = provider.provider_reference()
            from .numbering_sync import numbering_revision

            overlay_revision = numbering_revision(str(g.project.id))
            key = _idempotency_key()
            existing = TranslationOperation.query.filter_by(
                project_id=g.project.id,
                owner_user_id=g.project.user_id,
                kind="document_block_export",
                idempotency_key=key,
            ).one_or_none() if key else None
            if existing is not None and "epub_image_mode" not in body:
                try:
                    normalized_image_mode = normalize_epub_image_mode(
                        (existing.checkpoint_json or {}).get("epub_image_mode", "auto")
                    )
                except EpubExportContractError as exc:
                    return jsonify({"error": "The verified block export is unavailable.", "code": exc.code}), 409
            if export_format == "epub" and not supplied_epub_options:
                if existing is None:
                    normalized_epub_options = normalize_epub_options({})
                else:
                    saved = existing.checkpoint_json or {}
                    if "epub_options" in saved:
                        try:
                            normalized_epub_options = normalize_epub_options(
                                saved["epub_options"]
                            )
                        except EpubExportContractError:
                            return (
                                jsonify(
                                    {
                                        "error": "The verified block export is unavailable.",
                                        "code": "semantic-export-options-invalid",
                                    }
                                ),
                                409,
                            )
            if (
                export_format == "epub"
                and not supplied_epub_review_hash
                and existing is not None
            ):
                saved = existing.checkpoint_json or {}
                if "epub_review_hash" in saved:
                    from .block_export import normalize_epub_review_hash

                    try:
                        normalized_epub_review_hash = normalize_epub_review_hash(
                            saved["epub_review_hash"]
                        )
                    except BlockExportError:
                        return (
                            jsonify(
                                {
                                    "error": "The verified block export is unavailable.",
                                    "code": "epub-review-hash-invalid",
                                }
                            ),
                            409,
                        )
            request_payload = {
                "format": export_format,
                "numbering_revision": overlay_revision,
                "export_metadata": normalize_export_metadata(export_metadata),
                "source_content_hash": g.project.source_content_hash,
                "source_plan_id": workspace_plan.record.id,
                "source_plan_hash": workspace_plan.record.plan_hash,
                "semantic_export_provider": provider_reference,
            }
            if normalized_epub_options:
                request_payload["epub_options"] = normalized_epub_options
            if normalized_image_mode != "auto":
                request_payload["epub_image_mode"] = normalized_image_mode
            if normalized_epub_review_hash is not None:
                request_payload["epub_review_hash"] = normalized_epub_review_hash
            from .review_coverage import snapshot_export_review

            if existing is None:
                review_manifest = snapshot_export_review(g.project, requirement=review_requirement)
            else:
                saved = existing.checkpoint_json or {}
                review_manifest = saved.get("review_manifest")
                if review_manifest is None:
                    review_manifest = (existing.result_json or {}).get("review_manifest")
            if review_manifest is not None:
                request_payload["review_manifest"] = review_manifest
            if review_requirement != "none":
                request_payload["review_requirement"] = review_requirement
            queue_payload = {
                "format": export_format,
                "numbering_revision": overlay_revision,
                "export_metadata": normalize_export_metadata(export_metadata),
                "semantic_export_provider": provider_reference,
            }
            if normalized_epub_options:
                queue_payload["epub_options"] = normalized_epub_options
            if normalized_image_mode != "auto":
                queue_payload["epub_image_mode"] = normalized_image_mode
            if normalized_epub_review_hash is not None:
                queue_payload["epub_review_hash"] = normalized_epub_review_hash
            if cover_options:
                request_payload["epub_cover"] = cover_options
                queue_payload["epub_cover"] = cover_options
            if review_manifest is not None:
                queue_payload["review_manifest"] = review_manifest
            if review_requirement != "none":
                queue_payload["review_requirement"] = review_requirement
            if start_h2_on_new_page:
                request_payload["start_h2_on_new_page"] = True
                queue_payload["start_h2_on_new_page"] = True
            operation, error_response = _publish_operation(
                g.project,
                kind="document_block_export",
                handler="export_document_blocks",
                request_payload=request_payload,
                queue_payload=queue_payload,
                idempotency_key=key,
                source_plan_id=workspace_plan.record.id,
                source_plan_hash=workspace_plan.record.plan_hash,
            )
            if error_response is not None:
                return error_response
            return _operation_response(
                operation,
                extra={
                    "kind": "job",
                    "format": export_format,
                    "download_url": _job_download_url(
                        g.project.id,
                        operation.queue_job_id,
                    ),
                },
            )
        except CapabilityUseError as exc:
            return jsonify(exc.to_dict()), 409
        except (BlockContractError, BlockWorkspaceIntegrityError, scholarly_review.ScholarlyReviewError) as exc:
            return (
                jsonify(
                    {
                        "error": "The verified block export is unavailable.",
                        "code": getattr(exc, "code", "block-export-invalid"),
                    }
                ),
                409,
            )

    segment_guard = _segment_surface_guard(g.project)
    if segment_guard is not None:
        return segment_guard
    export_format = request.args.get("format", "html").lower()
    subtitle_export_formats = {"srt", "vtt", "subtitle_json", "subtitle_txt"}
    if bool(getattr(g, "is_guest", False)) and export_format != "html":
        return _guest_forbidden(
            "Guests can only export HTML previews. Ask the project owner for packaged exports."
        )

    try:
        safe_name = secure_filename(g.project.name.replace(" ", "_"))
        if not safe_name:
            safe_name = "translated_document"

        if export_format in subtitle_export_formats:
            if _project_source_type(g.project) != "subtitle_studio":
                return jsonify(
                    {
                        "error": "Subtitle export is only available for subtitle translation projects."
                    }
                ), 400
            provider = _subtitle_source_provider(
                g.project.user_id,
                g.project.settings,
            )
            cues = _translated_subtitle_cues(
                g.project,
                subtitle_provider=provider,
            )
            exported = provider.client.export_translation_cues(
                cues,
                export_format,
            )
            body = exported["content"]
            mimetype = exported["mimetype"]
            filename = f"{safe_name}.{exported['extension']}"
            return Response(
                body,
                mimetype=mimetype,
                headers={"Content-disposition": f'attachment; filename="{filename}"'},
            )

        if export_format == "html":
            final_html = reassemble_html_for_export(g.project, for_export=True)
            return Response(
                final_html,
                mimetype="text/html",
                headers={
                    "Content-disposition": f'attachment; filename="{safe_name}.html"'
                },
            )

        if export_format not in {"docx", "epub", "plain"}:
            return jsonify(
                {
                    "error": "Unsupported format. Allowed: html, docx, epub, plain, srt, vtt, subtitle_txt, subtitle_json"
                }
            ), 400
        if request.method != "POST":
            return jsonify(
                {"error": "Packaged exports must be started with POST."}
            ), 405

        operation, error_response = _publish_operation(
            g.project,
            kind="export",
            handler="export_project",
            request_payload={"format": export_format},
            queue_payload={"format": export_format},
        )
        if error_response is not None:
            return error_response
        return _operation_response(
            operation,
            extra={
                "kind": "job",
                "format": export_format,
                "download_url": _job_download_url(g.project.id, operation.queue_job_id),
            },
        )

    except Exception:
        logger.exception("Error during translation export")
        return jsonify({"error": "Export failed."}), 500


def _retranslation_execute_error(code: str, *, status: int = 409):
    """Return a bounded execution error without exposing durable internals."""

    return jsonify({"error": code, "code": code}), status


def _retranslation_operation_matches(request_row, operation) -> bool:
    return bool(
        operation is not None
        and operation.project_id == g.project.id
        and operation.kind == "document_block_translate"
        and operation.source_plan_id == request_row.source_plan_id
    )


def _retranslation_operation_settings(request_row) -> tuple[dict, dict | None]:
    """Derive the operation-only scholarly choices from current settings."""

    settings = dict(g.project.settings or {})
    profile = scholarly_review.normalize_annotation_profile(
        request_row.annotation_profile
    )
    settings["annotation_profile"] = profile
    settings["enable_translator_notes"] = profile != "none"
    choice = request_row.research_choice
    dossier_pin: dict | None = None
    if choice == "reuse_dossier":
        dossier_pin = scholarly_review.retranslation_dossier_pin(request_row)
        settings["enable_pre_analysis"] = False
        settings["enable_research"] = False
    elif choice == "fresh_document":
        settings["enable_pre_analysis"] = True
        settings["enable_research"] = True
    else:
        raise scholarly_review.ScholarlyReviewError("retranslation-research-choice")

    base_policy = settings.get("owner_policy")
    if base_policy is None:
        base_policy = settings.get("custom_prompt")
    if base_policy is not None and not isinstance(base_policy, str):
        raise ValueError("The current owner policy is invalid.")
    custom_prompt = str(request_row.custom_prompt or "")
    if custom_prompt.strip():
        addendum = f"Retranslation request addendum:\n{custom_prompt}"
        combined_policy = f"{base_policy}\n\n{addendum}" if base_policy else addendum
    else:
        combined_policy = base_policy
    if combined_policy is not None and len(combined_policy.encode("utf-8")) > 20_000:
        raise ValueError(
            "The owner policy and retranslation prompt exceed the 20,000-character limit."
        )
    settings.pop("custom_prompt", None)
    if combined_policy is None:
        settings.pop("owner_policy", None)
    else:
        settings["owner_policy"] = combined_policy
    return settings, dossier_pin


@blueprint.post(
    "/api/projects/<project_id>/retranslation-requests/<request_id>/execute"
)
@project_access_required
def execute_retranslation_request(project_id: str, request_id: str):
    """Queue one owner-approved scholarly retranslation with frozen evidence."""

    if getattr(g, "access_role", None) != "owner":
        return _retranslation_execute_error("retranslation-owner-required", status=403)
    key = request.headers.get(IDEMPOTENCY_HEADER)
    if not isinstance(key, str) or not 1 <= len(key) <= 128:
        return _retranslation_execute_error("idempotency-key-required", status=400)
    if request.content_length is not None and request.content_length > 2 * 1024:
        return _retranslation_execute_error("invalid-request-shape", status=400)
    raw_body = request.get_data(cache=True)
    if len(raw_body) > 2 * 1024:
        return _retranslation_execute_error("invalid-request-shape", status=400)
    try:
        body = json.loads(raw_body.decode("utf-8"))
    except UnicodeDecodeError, json.JSONDecodeError:
        return _retranslation_execute_error("invalid-request-shape", status=400)
    if (
        not isinstance(body, dict)
        or set(body) != {"expected_version"}
        or isinstance(body.get("expected_version"), bool)
        or not isinstance(body.get("expected_version"), int)
        or body["expected_version"] < 1
    ):
        return _retranslation_execute_error("invalid-request-shape", status=400)
    expected_version = body["expected_version"]
    try:
        request_row = scholarly_review.retranslation_request_record(
            g.project, request_id, actor=g.review_actor
        )
        safe_request = lambda: scholarly_retranslation_request(
            scholarly_review.retranslation_request_projection(request_row)
        )
        if request_row.source_plan_id != load_block_source_plan(g.project).record.id:
            return _retranslation_execute_error("retranslation-source-plan-conflict")
        if request_row.state in {"completed", "cancelled", "rejected"}:
            return _retranslation_execute_error("retranslation-terminal")
        recovery_from = None
        if request_row.state == "started":
            operation = (
                get_operation(request_row.operation_id, owner_user_id=g.project.user_id)
                if request_row.operation_id
                else None
            )
            if not _retranslation_operation_matches(request_row, operation):
                return _retranslation_execute_error("retranslation-operation-conflict")
            if operation.state in {"queued", "running", "completed"}:
                return _operation_response(operation, extra={"request": safe_request()})
            if request_row.version != expected_version:
                return jsonify(
                    {
                        "error": "retranslation-version-conflict",
                        "code": "retranslation-version-conflict",
                        "latest": safe_request(),
                    }
                ), 409
            if operation.state in {"partial", "cancelled"}:
                return _retranslation_execute_error("retranslation-recovery-conflict")
            if operation.state != "failed":
                return _retranslation_execute_error("retranslation-operation-conflict")
            recovery_from = operation
        elif request_row.state != "requested":
            return _retranslation_execute_error("retranslation-transition")
        elif request_row.research_choice == "await_external":
            return _retranslation_execute_error("retranslation-await-external")
        if request_row.version != expected_version:
            return jsonify(
                {
                    "error": "retranslation-version-conflict",
                    "code": "retranslation-version-conflict",
                    "latest": safe_request(),
                }
            ), 409

        source_plan = load_block_source_plan(g.project).record
        settings, dossier_pin = _retranslation_operation_settings(request_row)
        _validate_block_model_settings(settings, user_id=current_user.id)
        effective_context_mode = resolve_translation_context_mode(
            settings, execution_mode=settings["execution_mode"]
        )
        execution_profile = resolve_execution_profile(
            g.project, model_id=str(settings["translator_model"])
        )
        window_profile = derive_window_profile(
            execution_profile,
            maximum_blocks=settings.get(
                "translation_batch_max_blocks", DEFAULT_TRANSLATION_BATCH_MAX_BLOCKS
            ),
            target_input_tokens=settings.get(
                "translation_batch_input_tokens", DEFAULT_TRANSLATION_BATCH_INPUT_TOKENS
            ),
        )
        research = (
            _frozen_block_research(settings, user_id=current_user.id)
            if request_row.research_choice == "fresh_document"
            else None
        )
        owner = db.session.get(User, int(current_user.id))
        if owner is None:
            return _retranslation_execute_error(
                "retranslation-owner-required", status=403
            )
        translation_scope = {
            "contract_version": TRANSLATION_SCOPE_CONTRACT_VERSION,
            "mode": "explicit",
            "block_ids": list(request_row.scope_json.get("resolved_block_ids", [])),
        }
        retranslation_payload = {
            "request_id": request_row.id,
            "request_fingerprint": request_row.request_fingerprint,
            "request_version": expected_version,
            "research_choice": request_row.research_choice,
            "dossier_revision_id": dossier_pin["revision_id"] if dossier_pin else None,
            "dossier_record_hash": dossier_pin["record_hash"] if dossier_pin else None,
        }
        operation_key = (
            f"retranslate-recover-{recovery_from.id}"
            if recovery_from is not None
            else f"retranslate-{request_row.id}"
        )

        def initialize_retranslation(
            operation: TranslationOperation, created: bool
        ) -> None:
            if not created:
                row = TranslationBlockWorkflowConfig.query.filter_by(
                    operation_id=operation.id
                ).one_or_none()
                if row is None:
                    raise BlockContractError("workflow-config-missing")
                rehydrate_block_workflow_config(
                    row, operation=operation, source_plan=source_plan
                )
                return
            config = freeze_block_workflow_config(
                operation,
                source_plan,
                user=owner,
                settings=settings,
                window_profile=window_profile.to_dict(),
                research=research,
                protected_targets=snapshot_protected_targets(source_plan),
                translation_scope=translation_scope,
                translation_reasoning=None,
                research_dossier=dossier_pin,
                context_mode=effective_context_mode,
            )
            persist_block_workflow_config(operation, source_plan, config)
            if recovery_from is None:
                scholarly_review.start_retranslation_request(
                    request_row,
                    expected_version=expected_version,
                    operation=operation,
                    actor=g.review_actor,
                )
            else:
                scholarly_review.recover_retranslation_request(
                    request_row,
                    expected_version=expected_version,
                    failed_operation=recovery_from,
                    recovery_operation=operation,
                    actor=g.review_actor,
                )

        payload = _block_translation_request_payload(
            g.project,
            source_plan,
            settings,
            translation_scope=translation_scope,
            translation_reasoning=None,
            context_mode=effective_context_mode,
        )
        payload["retranslation"] = retranslation_payload
        operation, error_response = _publish_operation(
            g.project,
            kind="document_block_translate",
            handler="translate_document_blocks",
            request_payload=payload,
            idempotency_key=operation_key,
            source_plan_id=source_plan.id,
            source_plan_hash=source_plan.plan_hash,
            initialize_operation=initialize_retranslation,
        )
        if error_response is not None:
            return error_response
        return _operation_response(operation, extra={"request": safe_request()})
    except ModelNotReadyError as exc:
        db.session.rollback()
        return _model_error(exc)
    except scholarly_review.ScholarlyReviewError as exc:
        db.session.rollback()
        code = str(getattr(exc, "code", "retranslation-invalid"))
        status = 404 if "missing" in code else 403 if "owner" in code else 409
        return _retranslation_execute_error(code, status=status)
    except (BlockContractError, BlockWorkspaceIntegrityError) as exc:
        db.session.rollback()
        return _retranslation_execute_error(
            getattr(exc, "code", "block-workflow-invalid")
        )
    except ValueError:
        db.session.rollback()
        return _retranslation_execute_error(
            "retranslation-settings-invalid", status=400
        )


register_scholarly_routes(blueprint)
