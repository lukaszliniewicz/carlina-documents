from __future__ import annotations

import copy
import hashlib
import json
import re
import tempfile
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from flask import current_app
from sqlalchemy import update
from sqlalchemy.orm.attributes import flag_modified

from carlina.core.language import normalize_language_tag
from carlina.core.semantic_html import SAFE_LANGUAGE_RE
from carlina.core.utc import as_utc, utc_now_db
from iarf_assistant.extensions import db

from ...core.executor_profile import (
    ExecutorProfile,
    normalize_executor_profile,
)
from ...core.model_readiness import require_model_ready
from ...database_utils import run_transaction_with_retry
from ...core.upload_paths import resolve_upload_reference, upload_web_path, uploads_root
from ...models import McpExecution, McpWorkItem, User
from . import (
    document_review,
    qa_diagnostics,
    scholarly_review,
    scholarly_wire,
    shared_glossary,
)
from .block_admission import record_block_translation_job
from .block_contract import (
    DOCUMENT_BLOCK_ENGINE_CONTRACT,
    AttestedDocumentSnapshot,
)
from .block_export import (
    BlockExportError,
    build_semantic_export_request,
    merge_project_export_metadata,
    normalize_export_metadata,
    semantic_export_review_hash,
    validate_epub_preflight_result,
)
from .export_cover import pin_export_metadata_cover
from .block_notes import normalize_annotation_profile
from .block_persistence import (
    active_source_plan_pointer,
    plan_and_persist_source_snapshot,
    rehydrate_source_plan,
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
    derive_window_profile,
)
from .block_workflow_config import (
    normalize_translation_context_mode,
    resolve_translation_context_mode,
    translation_context_policy,
)
from .block_workspace_api import (
    BlockWorkspaceIntegrityError,
    build_block_glossary_payload,
    load_block_source_plan,
    persist_manual_candidate,
    require_document_block_engine,
)
from .models import (
    TranslationBlockWorkflowConfig,
    TranslationNote,
    TranslationOperation,
    TranslationProject,
    TranslationRevision,
    TranslationSegment,
    TranslationSourcePlan,
)
from .mcp_exports import export_job_projection, read_export, validate_read_options
from .operation_delivery import deliver_operation, record_job
from .operations import (
    ACTIVE_OPERATION_STATES,
    TranslationOperationTransitionConflict,
    TranslationProjectIdempotencyConflict,
    _require_idempotency_key,
    create_project_idempotently,
    find_active_operation,
    reconcile_expired_active_operation,
    request_cancellation,
    serialize_operation,
)
from .project_deletion import (
    cleanup_deleted_project_vectors,
    collect_project_deletion_artifacts,
    delete_project_aggregate,
)
from .rendering import render_project_html, translated_subtitle_cues
from .source_providers import source_reference
from .source_snapshot import (
    MAX_SOURCE_SNAPSHOT_BYTES,
    MAX_SUBTITLE_UNIT_CHARS,
    MAX_SUBTITLE_UNITS,
    encoded_subtitle_snapshot,
    subtitle_project_metadata,
    subtitle_render_settings,
    subtitle_snapshot_entry_bytes,
    subtitle_unit_metadata,
)
from .target_persistence import render_persisted_target_document
from .translation_units import normalize_translation_unit_mode
from .window_execution import ModelExecutionProfile, resolve_execution_profile

MAX_CUSTOM_PROMPT_CHARS = 20_000
ACTIVE_PROJECT_STATUSES = {
    "analyzing_structure",
    "analyzing_terminology",
    "processing_pdf",
    "translating",
}
SUBTITLE_EXPORT_FORMATS = {"srt", "vtt", "subtitle_json", "subtitle_txt"}
PACKAGE_EXPORT_FORMATS = {"docx", "epub", "plain"}
MAX_RESEARCH_PLUGIN_IDS = 20
RESEARCH_PLUGIN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
MAX_SOURCE_DOCUMENT_ID_CHARS = 36
TRANSLATOR_EXECUTION_MODES = frozenset({"managed", "passive"})
MCP_EXECUTION_ID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-"
    r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$"
)
_MCP_EXECUTION_NOT_FOUND = "MCP execution was not found."
MAX_EXPORT_COVER_IMPORT_BYTES = 10 * 1024 * 1024
EXPORT_COVER_COPY_CHUNK_BYTES = 1024 * 1024
_EPUB_OPTIONS_UNSET = object()
_EPUB_REVIEW_HASH_UNSET = object()
_EPUB_IMAGE_MODE_UNSET = object()


def _context_policy_epoch_idempotency_key(
    *,
    base_key: str,
    source_plan_hash: str,
    context_mode: str,
    dossier_revision_id: str | None,
    preceding_operation_id: str,
) -> str:
    """Fence a new context policy epoch from any previous policy's operation."""

    suffix = (
        "-after-"
        + hashlib.sha256(preceding_operation_id.encode("utf-8")).hexdigest()[:16]
    )
    candidate = f"{base_key}{suffix}"
    if len(candidate) <= 128:
        return candidate

    bounded_base = (
        f"translate-{source_plan_hash[:48]}-ctxv2-{context_mode}"
    )
    if dossier_revision_id is not None:
        dossier_digest = hashlib.sha256(
            dossier_revision_id.encode("utf-8")
        ).hexdigest()[:16]
        bounded_base = f"{bounded_base}-dossier-{dossier_digest}"
    candidate = f"{bounded_base}{suffix}"
    if len(candidate) > 128:
        raise ValueError("Translation context policy identity exceeds its bound.")
    return candidate


class TranslationProjectDeleteError(RuntimeError):
    """Safe owner-scoped project deletion failure surfaced through MCP."""

    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


class TranslatorClient:
    """Executable, user-scoped document translation capability."""

    def __init__(
        self,
        context: Any,
        config: Mapping[str, Any] | None = None,
        *,
        document_provider_factory: Callable[[dict], Any] | None = None,
        semantic_export_provider_factory: Callable[[dict], Any] | None = None,
        subtitle_provider_factory: Callable[[dict], Any] | None = None,
        sentence_segmenter_factory: Callable[[], Any] | None = None,
    ):
        self.context = context
        self.config = dict(config or {})
        user_id = self.config.get("current_user_id")
        if user_id is None:
            raise ValueError("current_user_id is required for translation.")
        self.user_id = int(user_id)
        self._document_provider_factory = document_provider_factory
        self._semantic_export_provider_factory = semantic_export_provider_factory
        self._subtitle_provider_factory = subtitle_provider_factory
        self._sentence_segmenter_factory = sentence_segmenter_factory
        self._sentence_segmenter_client = None
        self._queue_client = None

    def _queue(self):
        if self._queue_client is None:
            self._queue_client = self.context.get_task_queue("translator")
        return self._queue_client

    def _document_provider(self, reference: dict | None = None):
        if self._document_provider_factory is None:
            raise RuntimeError("The document-processing capability is unavailable.")
        return self._document_provider_factory(dict(reference or {}))

    def _subtitle_provider(self, reference: dict | None = None):
        if self._subtitle_provider_factory is None:
            raise RuntimeError("The subtitle-authoring capability is unavailable.")
        return self._subtitle_provider_factory(dict(reference or {}))

    def _semantic_export_provider(self, reference: dict | None = None):
        if self._semantic_export_provider_factory is None:
            raise RuntimeError("The semantic-export capability is unavailable.")
        return self._semantic_export_provider_factory(dict(reference or {}))

    def _sentence_segmenter(self):
        if self._sentence_segmenter_factory is None:
            return None
        if self._sentence_segmenter_client is None:
            self._sentence_segmenter_client = self._sentence_segmenter_factory()
        return self._sentence_segmenter_client

    def _source_sentence_splitter(self) -> Callable[[str], Sequence[str]]:
        """Resolve the strict splitter required by aligned-sentence projects."""

        try:
            segmenter = self._sentence_segmenter()
            split_text = getattr(segmenter, "split_text", None)
        except Exception:  # noqa: BLE001 - hide provider-internal setup details
            raise RuntimeError(
                "A working Sentence Segmenter is required for aligned-sentence "
                "projects; choose block mode or check setup."
            ) from None
        if not callable(split_text):
            raise RuntimeError(  # noqa: TRY004 - bounded capability setup error
                "A working Sentence Segmenter is required for aligned-sentence "
                "projects; choose block mode or check setup."
            )

        def split_source(projection: str) -> Sequence[str]:
            return split_text(projection, raise_on_failure=True)

        return split_source

    def _project(self, project_id: str) -> TranslationProject:
        normalized_id = str(project_id or "").strip()
        if not normalized_id:
            raise ValueError("project_id is required.")
        project = TranslationProject.query.filter_by(
            id=normalized_id,
            user_id=self.user_id,
        ).one_or_none()
        if project is None:
            raise LookupError("Translation project not found.")
        return project

    def delete_project(
        self,
        project_id: str,
        *,
        idempotency_key: str,
        confirmation: str,
    ) -> dict[str, Any]:
        """Delete one terminal owner-scoped Translator project and its MCP history."""

        if confirmation != "delete":
            raise TranslationProjectDeleteError(
                "confirmation_required",
                "Explicit user confirmation is required before deleting a Translator project.",
            )
        if (
            not isinstance(idempotency_key, str)
            or not idempotency_key.strip()
            or len(idempotency_key) > 128
        ):
            raise TranslationProjectDeleteError(
                "invalid_idempotency_key",
                "A fresh idempotency key of 1-128 characters is required.",
            )
        try:
            project = self._project(project_id)
        except LookupError as exc:
            raise TranslationProjectDeleteError(
                "project_not_found", "Translation project not found."
            ) from exc

        active_operation = reconcile_expired_active_operation(project.id, self.user_id)
        if active_operation is not None:
            raise TranslationProjectDeleteError(
                "project_active",
                "The Translator project has an active operation. Cancel or finish it before "
                "deleting the project.",
            )

        live_execution = (
            McpExecution.query.filter(
                McpExecution.owner_user_id == self.user_id,
                McpExecution.addon_id == "translator",
                McpExecution.resource_kind == "project",
                McpExecution.resource_id == project.id,
                McpExecution.status.in_(
                    ("queued", "running", "waiting_external", "waiting_review")
                ),
            )
            .order_by(McpExecution.created_at.desc())
            .first()
        )
        if live_execution is not None:
            leased = (
                McpWorkItem.query.filter(
                    McpWorkItem.execution_id == live_execution.id,
                    McpWorkItem.state == "leased",
                ).first()
                is not None
            )
            detail = (
                "The Translator project has leased MCP work. Cancel or finish the execution "
                "before deleting the project."
                if leased
                else "The Translator project has a nonterminal MCP execution. Cancel or finish "
                "it before deleting the project."
            )
            raise TranslationProjectDeleteError("project_active", detail)

        artifacts = collect_project_deletion_artifacts(project)
        try:
            # MCP execution rows deliberately have no FK to Translator projects. Remove only
            # terminal owner-scoped rows here; their work items/submissions cascade with them.
            McpExecution.query.filter(
                McpExecution.owner_user_id == self.user_id,
                McpExecution.addon_id == "translator",
                McpExecution.resource_kind == "project",
                McpExecution.resource_id == project.id,
                McpExecution.status.in_(("completed", "failed", "cancelled")),
            ).delete(synchronize_session=False)
            delete_project_aggregate(project)
            db.session.commit()
        except BaseException:
            db.session.rollback()
            raise

        cleanup_deleted_project_vectors(current_app, artifacts)
        return {"success": True, "project_id": artifacts.project_id}

    @staticmethod
    def _require_subtitle_project(project: TranslationProject) -> None:
        if (project.settings or {}).get("source_type") != "subtitle_studio":
            raise ValueError(
                "Segment translation handlers are available only for Subtitle Studio projects."
            )
        if project.engine_contract == DOCUMENT_BLOCK_ENGINE_CONTRACT:
            raise ValueError(
                "Document-block projects must use the block translation workflow."
            )

    @staticmethod
    def _snapshot_captured_at(value: Any) -> datetime:
        if isinstance(value, datetime):
            return as_utc(value).replace(tzinfo=None)
        if value:
            try:
                return as_utc(
                    datetime.fromisoformat(str(value))
                ).replace(tzinfo=None)
            except ValueError as exc:
                raise ValueError(
                    "Source snapshot has an invalid captured timestamp."
                ) from exc
        return utc_now_db()

    @staticmethod
    def _project_payload(project: TranslationProject, *, detail: bool = False) -> dict:
        payload = project.to_dict()
        # The legacy project share token is still accepted for compatibility,
        # but it is a bearer credential and must never cross the owner MCP
        # projection. New review grants have their own one-time secret flow.
        payload.pop("share_token", None)
        payload["engine_contract"] = getattr(project, "engine_contract", None)
        payload_settings = dict(payload.get("settings") or {})
        payload_settings["enable_translator_notes"] = payload_settings.get(
            "enable_translator_notes", False
        )
        if project.engine_contract == DOCUMENT_BLOCK_ENGINE_CONTRACT:
            payload_settings["translation_unit_mode"] = normalize_translation_unit_mode(
                payload_settings.get("translation_unit_mode")
            )
        payload["settings"] = payload_settings
        visible_segments = [
            segment for segment in project.segments if segment.merged_into_id is None
        ]
        payload["total_segments"] = len(visible_segments)
        payload["completed_segments"] = sum(
            segment.status in {"approved", "human_approved"}
            for segment in visible_segments
        )
        if detail:
            payload["segments"] = [segment.to_dict() for segment in project.segments]
            payload["working_memory"] = project.working_memory or ""
            payload["glossary"] = [term.to_dict() for term in project.glossary_terms]
        if project.operations:
            payload["latest_operation"] = project.operations[0].to_dict()
        return payload

    @staticmethod
    def _export_metadata_projection(project: TranslationProject) -> dict[str, Any]:
        """Return the bounded saved export defaults and their CAS version."""

        settings = dict(project.settings or {})
        metadata = normalize_export_metadata(settings.get("export_metadata", {}))
        version = settings.get("export_metadata_version", 0)
        if isinstance(version, bool) or not isinstance(version, int) or version < 0:
            raise BlockExportError("export-metadata-version")
        return {
            "version": version,
            "metadata": copy.deepcopy(metadata),
        }

    def _settings(
        self,
        *,
        translator_model: str | None = None,
        researcher_model: str | None = None,
        analyzer_model: str | None = None,
        chunk_size_chars: int = 2500,
        custom_prompt: str | None = None,
        enable_pre_analysis: bool | None = None,
        enable_research: bool = False,
        enable_translator_notes: bool = False,
        annotation_profile: object = None,
        research_plugin_ids: list[str] | None = None,
        execution_mode: str = "managed",
        translation_batch_input_tokens: int = DEFAULT_TRANSLATION_BATCH_INPUT_TOKENS,
        translation_batch_max_blocks: int = DEFAULT_TRANSLATION_BATCH_MAX_BLOCKS,
        translation_context_mode: object = "self_contained",
        translation_unit_mode: object = None,
        translation_max_block_chars: int = DEFAULT_TRANSLATION_MAX_BLOCK_CHARS,
        export_metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        normalized_export_metadata = normalize_export_metadata(
            {} if export_metadata is None else export_metadata
        )
        normalized_export_metadata = pin_export_metadata_cover(
            normalized_export_metadata, user_id=self.user_id
        )
        translator_notes_enabled = self._parse_bool(
            enable_translator_notes,
            field="enable_translator_notes",
        )
        effective_annotation_profile = normalize_annotation_profile(
            annotation_profile, translator_notes_enabled
        )
        effective_translation_context_mode = normalize_translation_context_mode(
            translation_context_mode
        )
        effective_translation_unit_mode = normalize_translation_unit_mode(
            translation_unit_mode
        )
        try:
            chunk_size = int(chunk_size_chars)
        except (TypeError, ValueError) as exc:
            raise ValueError("chunk_size_chars must be a whole number.") from exc
        if not 500 <= chunk_size <= 20_000:
            raise ValueError("chunk_size_chars must be between 500 and 20,000.")
        if isinstance(translation_max_block_chars, bool):
            raise ValueError("translation_max_block_chars must be a whole number.")
        try:
            max_block_chars = int(translation_max_block_chars)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "translation_max_block_chars must be a whole number."
            ) from exc
        if (
            not MIN_TRANSLATION_MAX_BLOCK_CHARS
            <= max_block_chars
            <= MAX_TRANSLATION_MAX_BLOCK_CHARS
        ):
            raise ValueError(
                "translation_max_block_chars must be between "
                f"{MIN_TRANSLATION_MAX_BLOCK_CHARS:,} and {MAX_TRANSLATION_MAX_BLOCK_CHARS:,}."
            )
        try:
            batch_input_tokens = int(translation_batch_input_tokens)
            batch_max_blocks = int(translation_batch_max_blocks)
        except (TypeError, ValueError) as exc:
            raise ValueError("Block batch settings must be whole numbers.") from exc
        if not 2_048 <= batch_input_tokens <= 1_000_000:
            raise ValueError("Translation batch input budget is out of range.")
        if not 1 <= batch_max_blocks <= 256:
            raise ValueError("Translation batch block limit is out of range.")
        prompt = str(custom_prompt or "").strip()
        if len(prompt) > MAX_CUSTOM_PROMPT_CHARS:
            raise ValueError(
                f"Custom prompt exceeds {MAX_CUSTOM_PROMPT_CHARS:,} characters."
            )
        normalized_execution_mode = self._execution_mode(execution_mode)
        if normalized_execution_mode == "passive":
            # Passive execution receives capability and limits from the MCP
            # executor envelope at start; persisted Carlina model IDs are not
            # selected, resolved, or used for readiness.
            models = {
                "translator_model": "",
                "researcher_model": "",
                "analyzer_model": "",
            }
        else:
            models = {
                "translator_model": str(translator_model or "").strip(),
                "researcher_model": str(researcher_model or "").strip(),
                "analyzer_model": str(analyzer_model or "").strip(),
            }
            if not models["translator_model"]:
                raise ValueError("A translator model is required.")
            if any(len(value) > 255 for value in models.values()):
                raise ValueError("Model identifiers must be at most 255 characters.")
        enable_analysis = self._parse_bool(
            (normalized_execution_mode == "managed")
            if enable_pre_analysis is None
            else enable_pre_analysis,
            field="enable_pre_analysis",
        )
        if normalized_execution_mode == "passive" and enable_analysis:
            raise ValueError(
                "Passive pre-analysis must be run in a separately supported execution later."
            )
        research_enabled = self._parse_bool(enable_research, field="enable_research")
        if normalized_execution_mode == "passive" and research_enabled:
            raise ValueError(
                "Carlina-managed research is disabled in passive mode. Inspect the source, research with your external tools, then submit and pin an optional research dossier."
            )
        plugin_ids = self._research_plugin_ids(research_plugin_ids)
        if normalized_execution_mode == "managed":
            if enable_analysis and not models["analyzer_model"]:
                raise ValueError(
                    "An analyzer model is required when pre-analysis is enabled."
                )
            require_model_ready(
                models["translator_model"],
                user_id=self.user_id,
                required_model_type="chat",
            )
            if enable_analysis:
                require_model_ready(
                    models["analyzer_model"],
                    user_id=self.user_id,
                    required_model_type="chat",
                )
        if research_enabled:
            if not plugin_ids:
                raise ValueError(
                    "Select at least one research provider when research is enabled."
                )
            if normalized_execution_mode == "managed":
                if not models["researcher_model"]:
                    raise ValueError(
                        "A researcher model is required when research is enabled."
                    )
                require_model_ready(
                    models["researcher_model"],
                    user_id=self.user_id,
                    required_model_type="chat",
                )
        return {
            **models,
            "enable_pre_analysis": enable_analysis,
            "enable_research": research_enabled,
            "enable_translator_notes": effective_annotation_profile != "none",
            "annotation_profile": effective_annotation_profile,
            "research_plugin_ids": plugin_ids,
            "chunk_size_chars": chunk_size,
            "custom_prompt": prompt,
            "execution_mode": normalized_execution_mode,
            "translation_batch_input_tokens": batch_input_tokens,
            "translation_batch_max_blocks": batch_max_blocks,
            "translation_context_mode": effective_translation_context_mode,
            "translation_unit_mode": effective_translation_unit_mode,
            **(
                {TRANSLATION_MAX_BLOCK_CHARS_SETTING: max_block_chars}
                if effective_translation_unit_mode == "aligned_sentences"
                else {}
            ),
            "export_metadata": normalized_export_metadata,
            "export_metadata_version": 0,
        }

    @staticmethod
    def _execution_mode(value: Any) -> str:
        """Normalize the Translator's two supported dispatch modes."""

        normalized = str(value).strip().lower() if isinstance(value, str) else value
        if normalized not in TRANSLATOR_EXECUTION_MODES:
            raise ValueError("execution_mode must be either 'managed' or 'passive'.")
        return normalized

    @staticmethod
    def _mcp_execution_id(value: Any) -> str:
        """Validate one bounded UUID-like external execution identity."""

        normalized = str(value or "").strip()
        if not MCP_EXECUTION_ID_RE.fullmatch(normalized):
            raise ValueError("execution_id must be a valid UUID-like identifier.")
        return str(uuid.UUID(normalized))

    @staticmethod
    def _parse_bool(value: Any, *, field: str) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"true", "1", "yes", "on"}:
                return True
            if normalized in {"false", "0", "no", "off", ""}:
                return False
        if isinstance(value, int) and value in {0, 1}:
            return bool(value)
        raise ValueError(f"{field} must be a boolean.")

    @staticmethod
    def _research_plugin_ids(value: Any) -> list[str]:
        raw = [] if value is None else value
        if not isinstance(raw, list) or len(raw) > MAX_RESEARCH_PLUGIN_IDS:
            raise ValueError(
                "research_plugin_ids must contain at most 20 provider IDs."
            )
        ids = []
        for item in raw:
            if not isinstance(item, str):
                raise TypeError(
                    "research_plugin_ids must contain provider ID strings."
                )
            addon_id = item.strip()
            if not RESEARCH_PLUGIN_ID_RE.fullmatch(addon_id):
                raise ValueError("research_plugin_ids contains an invalid provider ID.")
            ids.append(addon_id)
        if len(ids) != len(set(ids)):
            raise ValueError("research_plugin_ids must not contain duplicates.")
        return sorted(ids)

    def _validate_existing_settings(
        self, project: TranslationProject, *, require_analysis: bool
    ) -> None:
        settings = dict(project.settings or {})
        if project.engine_contract == DOCUMENT_BLOCK_ENGINE_CONTRACT:
            normalize_translation_unit_mode(settings.get("translation_unit_mode"))
            normalize_translation_context_mode(
                settings.get("translation_context_mode", "self_contained")
            )
        self._parse_bool(
            settings.get("enable_translator_notes", False),
            field="enable_translator_notes",
        )
        normalize_annotation_profile(
            settings.get("annotation_profile"),
            settings.get("enable_translator_notes", False),
        )
        execution_mode = self._execution_mode(settings.get("execution_mode", "managed"))
        if execution_mode == "passive":
            # Existing passive projects may retain stale Carlina IDs, but they
            # are deliberately ignored. The external executor profile is the
            # only capability admission input for passive completion.
            if require_analysis or self._parse_bool(
                settings.get("enable_pre_analysis", False), field="enable_pre_analysis"
            ):
                raise ValueError(
                    "Passive pre-analysis must be run in a separately supported execution later."
                )
            research_enabled = self._parse_bool(
                settings.get("enable_research", False), field="enable_research"
            )
            if research_enabled:
                raise ValueError(
                    "Carlina-managed research is disabled in passive mode. Inspect the source, research with your external tools, then submit and pin an optional research dossier."
                )
            return

        require_selected_model = require_model_ready
        translator_model = str(settings.get("translator_model") or "").strip()
        if not translator_model:
            raise ValueError("A translator model is required.")
        require_selected_model(
            translator_model, user_id=self.user_id, required_model_type="chat"
        )
        analyzer_model = str(settings.get("analyzer_model") or "").strip()
        if require_analysis or self._parse_bool(
            settings.get("enable_pre_analysis", False), field="enable_pre_analysis"
        ):
            if not analyzer_model:
                raise ValueError(
                    "An analyzer model is required for terminology analysis."
                )
            require_selected_model(
                analyzer_model, user_id=self.user_id, required_model_type="chat"
            )
        research_enabled = self._parse_bool(
            settings.get("enable_research", False), field="enable_research"
        )
        plugin_ids = self._research_plugin_ids(settings.get("research_plugin_ids", []))
        if research_enabled:
            researcher_model = str(settings.get("researcher_model") or "").strip()
            if not researcher_model:
                raise ValueError(
                    "A researcher model is required when research is enabled."
                )
            if not plugin_ids:
                raise ValueError(
                    "Select at least one research provider when research is enabled."
                )
            require_selected_model(
                researcher_model, user_id=self.user_id, required_model_type="chat"
            )

    def _new_project(
        self,
        *,
        name: str,
        source_lang: str,
        target_lang: str,
        settings: dict[str, Any],
        document_id: str | None = None,
        status: str = "setup",
        source_snapshot: str | None = None,
        source_content_hash: str | None = None,
        source_captured_at: datetime | None = None,
        persist: bool = True,
    ) -> TranslationProject:
        normalized_name = str(name or "").strip()
        source_label = str(source_lang or "Auto-detect").strip()
        target_label = str(target_lang or "").strip()
        if not normalized_name or not target_label:
            raise ValueError("Project name and target language are required.")
        if len(normalized_name) > 255:
            raise ValueError("Project name must be at most 255 characters.")
        if len(source_label) > 50 or len(target_label) > 50:
            raise ValueError("Language labels must be at most 50 characters.")
        if source_label.casefold() in {"auto", "auto-detect", "detect"}:
            normalized_source = "Auto-detect"
        else:
            normalized_source = normalize_language_tag(source_label)
            if not normalized_source or not SAFE_LANGUAGE_RE.fullmatch(
                normalized_source
            ):
                raise ValueError(
                    "source_lang must be Auto-detect, a language name, or a BCP 47 tag."
                )
        normalized_target = normalize_language_tag(target_label)
        if not normalized_target or not SAFE_LANGUAGE_RE.fullmatch(normalized_target):
            raise ValueError(
                "target_lang must be a language name or a BCP 47 tag."
            )
        project = TranslationProject(
            user_id=self.user_id,
            name=normalized_name,
            source_lang=normalized_source,
            target_lang=normalized_target,
            document_id=document_id,
            status=status,
            settings=settings,
            source_snapshot=source_snapshot,
            source_content_hash=source_content_hash,
            source_captured_at=source_captured_at,
        )
        if persist:
            db.session.add(project)
            db.session.flush()
        return project

    def _create_project(
        self,
        project: TranslationProject,
        *,
        idempotency_key: str | None,
        request_payload: dict[str, Any],
    ) -> tuple[TranslationProject, bool]:
        try:
            return create_project_idempotently(
                project,
                idempotency_key=idempotency_key,
                request_payload=request_payload,
            )
        except TranslationProjectIdempotencyConflict as exc:
            raise RuntimeError(
                "The idempotency key is already bound to a different project request."
            ) from exc

    def _record_job(
        self,
        project: TranslationProject,
        *,
        handler: str,
        operation: str,
        payload: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        source_plan_id: str | None = None,
        source_plan_hash: str | None = None,
        checkpoint: dict[str, Any] | None = None,
    ) -> str:
        return record_job(
            project,
            handler=handler,
            operation=operation,
            payload=payload,
            idempotency_key=idempotency_key,
            source_plan_id=source_plan_id,
            source_plan_hash=source_plan_hash,
            checkpoint=checkpoint,
            publish=self._deliver_operation,
        )

    def _deliver_operation(
        self,
        project: TranslationProject,
        record: TranslationOperation,
        *,
        handler: str,
        payload: dict[str, Any],
    ) -> str:
        return deliver_operation(
            project,
            record,
            owner_user_id=self.user_id,
            handler=handler,
            payload=payload,
            enqueue=lambda queued_handler, queued_payload: self._queue().enqueue(
                queued_handler, queued_payload
            ),
        )

    def _ensure_document_ingestion_delivery(self, project: TranslationProject) -> str:
        """Repair an initial document outbox gap from its immutable snapshot."""
        from .source_planning import is_async_source_operation, planning_payload, snapshot_for_operation

        latest = (TranslationOperation.query.filter_by(
            project_id=project.id, owner_user_id=self.user_id, kind="plan_document_blocks",
        ).order_by(TranslationOperation.created_at.desc()).first())
        if latest is not None and is_async_source_operation(latest):
            snapshot_for_operation(project, latest)
            payload = planning_payload(project.source_content_hash)
            if latest.state == "queued" and not latest.queue_job_id:
                return self._deliver_operation(project, latest, handler="plan_document_blocks", payload=payload)
            if latest.state == "failed":
                return self._record_job(
                    project, handler="plan_document_blocks", operation="plan_document_blocks",
                    payload=payload, idempotency_key=f"recover-{latest.id}",
                    checkpoint={**latest.checkpoint_json, "phase": "queued"},
                )
            if latest.state == "completed" and active_source_plan_pointer(project.id) is None:
                raise RuntimeError("The completed source plan is missing.")
            return str(latest.queue_job_id or "")

        if project.engine_contract == DOCUMENT_BLOCK_ENGINE_CONTRACT:
            pointer = active_source_plan_pointer(project.id)
            if pointer is None:
                raise RuntimeError("The active document-block source plan is missing.")
            source_plan = db.session.get(TranslationSourcePlan, pointer.source_plan_id)
            if source_plan is None or source_plan.project_id != project.id:
                raise RuntimeError("The active document-block source plan is invalid.")
            plan = rehydrate_source_plan(source_plan)
            if plan.snapshot.content_hash != project.source_content_hash:
                raise RuntimeError("The active document-block source plan is stale.")
            request_payload = {
                "source_content_hash": source_plan.source_content_hash,
                "source_plan_id": source_plan.id,
                "source_plan_hash": source_plan.plan_hash,
                "engine_contract": DOCUMENT_BLOCK_ENGINE_CONTRACT,
            }
            active = reconcile_expired_active_operation(project.id, self.user_id)
            if active is not None:
                if active.kind == "plan_document_blocks" and active.state == "queued" and not active.queue_job_id:
                    return self._deliver_operation(
                        project,
                        active,
                        handler="plan_document_blocks",
                        payload=request_payload,
                    )
                return str(active.queue_job_id or "")
            latest = (
                TranslationOperation.query.filter_by(
                    project_id=project.id,
                    owner_user_id=self.user_id,
                    kind="plan_document_blocks",
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
                    latest.source_plan_id != source_plan.id
                    or latest.source_plan_hash != source_plan.plan_hash
                    or latest.source_content_hash != source_plan.source_content_hash
                ):
                    raise RuntimeError("The existing block-plan operation has foreign source pins.")
                return str(latest.queue_job_id or "")
            repair_key = (
                f"recover-{latest.id}"
                if latest is not None
                else f"initial-plan-{source_plan.plan_hash[:48]}"
            )
            return self._record_job(
                project,
                handler="plan_document_blocks",
                operation="plan_document_blocks",
                payload=request_payload,
                idempotency_key=repair_key,
                source_plan_id=source_plan.id,
                source_plan_hash=source_plan.plan_hash,
            )

        raise ValueError("Document ingestion supports document-block projects only.")

    def list_projects(
        self, *, limit: int = 100, offset: int = 0
    ) -> list[dict[str, Any]]:
        limit = min(500, max(1, int(limit)))
        offset = max(0, int(offset))
        projects = (
            TranslationProject.query.filter_by(user_id=self.user_id)
            .order_by(
                TranslationProject.updated_at.desc(), TranslationProject.id.desc()
            )
            .offset(offset)
            .limit(limit)
            .all()
        )
        return [self._project_payload(project) for project in projects]

    def read_project(
        self,
        project_id: str,
        *,
        detail: bool = True,
        include_export_metadata: bool = False,
    ) -> dict:
        project = self._project(project_id)
        payload = self._project_payload(project, detail=bool(detail))
        if include_export_metadata:
            payload["export_metadata"] = self._export_metadata_projection(project)
        return payload

    def get_document_project(
        self, project_id: str, *, include_export_metadata: bool = False
    ) -> dict:
        """Return a bounded owner project projection without source-book content."""

        return self.read_project(
            project_id,
            detail=False,
            include_export_metadata=include_export_metadata,
        )

    def review_outline(
        self, project_id: str, cursor: str | None = None, limit: int = 100
    ) -> dict[str, Any]:
        """Return the bounded owner-scoped outline for a document-block project."""

        project = self._project(project_id)
        require_document_block_engine(project)
        return document_review.review_outline(project, cursor=cursor, limit=limit)

    def review_fetch(
        self,
        project_id: str,
        block_ids: Sequence[str] | None = None,
        order_start: int | None = None,
        order_end: int | None = None,
        cursor: str | None = None,
        limit: int = 50,
        include_research: bool = False,
        include_proposals: bool = False,
        include_alignment: bool = False,
    ) -> dict[str, Any]:
        """Return selected bounded evidence for a document-block project."""

        project = self._project(project_id)
        require_document_block_engine(project)
        return document_review.review_fetch(
            project,
            block_ids=block_ids,
            order_start=order_start,
            order_end=order_end,
            cursor=cursor,
            limit=limit,
            include_research=include_research,
            include_proposals=include_proposals,
            include_alignment=include_alignment,
        )


    def review_source_pages(
        self, project_id: str, block_ids: Sequence[str]
    ) -> dict[str, Any]:
        """Resolve Translator block IDs to physical source PDF page numbers."""

        project = self._project(project_id)
        require_document_block_engine(project)
        return document_review.review_source_pages(project, block_ids=block_ids)

    def qa_diagnostics(
        self,
        project_id: str,
        block_ids: Sequence[str] | None = None,
        categories: Sequence[str] | None = None,
        severities: Sequence[str] | None = None,
        cursor: str | None = None,
        limit: int = 100,
        include_spelling: bool = True,
        group_by: str = "finding",
        include_excerpts: bool = True,
        group_id: str | None = None,
    ) -> dict[str, Any]:
        """Run deterministic post-translation QA over active target revisions."""

        project = self._project(project_id)
        require_document_block_engine(project)
        return qa_diagnostics.qa_diagnostics(
            project,
            block_ids=block_ids,
            categories=() if categories is None else categories,
            severities=() if severities is None else severities,
            cursor=cursor,
            limit=limit,
            include_spelling=include_spelling,
            group_by=group_by,
            include_excerpts=include_excerpts,
            group_id=group_id,
        )

    def review_coverage_record(
        self, project_id: str, *, entries: list[dict[str, Any]], kind: str,
        idempotency_key: str, reviewer_kind: str = "agent", comment: str = "",
    ) -> dict[str, Any]:
        from .review_coverage import record_review_coverage

        try:
            project = self._project(project_id)
            require_document_block_engine(project)
            result = record_review_coverage(
                project, entries=entries, kind=kind, reviewer_kind=reviewer_kind,
                comment=comment, actor=scholarly_review.ReviewActor("owner", self.user_id),
                idempotency_key=idempotency_key,
            )
            db.session.commit()
            return result
        except BaseException:
            db.session.rollback()
            raise

    def review_coverage_get(
        self, project_id: str, *, block_ids: list[str] | None = None,
        offset: int = 0, limit: int = 100, include_anchor_ids: bool = False,
    ) -> dict[str, Any]:
        from .review_coverage import review_coverage_manifest

        project = self._project(project_id)
        require_document_block_engine(project)
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Coverage offset must be nonnegative and limit must be 1-100.")
        if type(include_anchor_ids) is not bool:
            raise ValueError("include_anchor_ids must be a boolean.")
        manifest = review_coverage_manifest(project, block_ids=block_ids)
        blocks = manifest["blocks"]
        manifest["blocks"] = blocks[offset:offset + limit]
        manifest["pagination"] = {
            "offset": offset, "limit": limit, "total": len(blocks),
            "has_more": offset + len(manifest["blocks"]) < len(blocks),
        }
        if not include_anchor_ids:
            for block in manifest["blocks"]:
                for coverage in block["coverage"]:
                    coverage["source_anchor_count"] = len(coverage.pop("source_anchor_ids"))
                for kind in block["kinds"].values():
                    kind.pop("reviewed_source_anchor_ids")
        return manifest

    def review_search(
        self,
        project_id: str,
        query: str,
        fields: Sequence[str] | None = None,
        roles: Sequence[str] | None = None,
        before: int = 0,
        after: int = 0,
        cursor: str | None = None,
        limit: int = 20,
        include_proposals: bool = False,
        mode: str = "literal",
        regex_flags: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        """Search bounded verified document evidence literally or by safe regex."""

        project = self._project(project_id)
        require_document_block_engine(project)
        return document_review.review_search(
            project,
            query=query,
            fields=("source", "target") if fields is None else fields,
            roles=() if roles is None else roles,
            before=before,
            after=after,
            cursor=cursor,
            limit=limit,
            include_proposals=include_proposals,
            mode=mode,
            regex_flags=() if regex_flags is None else regex_flags,
        )

    def review_explain(
        self, project_id: str, block_ids: Sequence[str], question: str
    ) -> dict[str, Any]:
        """Return bounded evidence, never hidden reasoning or a model answer."""

        project = self._project(project_id)
        require_document_block_engine(project)
        return document_review.explanation_evidence(
            project, block_ids=block_ids, question=question
        )

    def _numbering_source_loader(self, project):
        provider = self._document_provider(
            source_reference(project.settings, capability="document.processing")
        )
        return lambda: provider.client.get_document_snapshot(str(project.document_id))

    def numbering_preview(self, project_id: str) -> dict[str, Any]:
        from .numbering_sync import preview_numbering_sync

        project = self._project(project_id)
        return preview_numbering_sync(
            project, owner_user_id=self.user_id,
            load_current_source=self._numbering_source_loader(project),
        )

    def numbering_apply(
        self, project_id: str, *, preview_token: str,
        selected_ids: list[str], idempotency_key: str,
    ) -> dict[str, Any]:
        from .numbering_sync import apply_numbering_sync

        project = self._project(project_id)
        return apply_numbering_sync(
            project, owner_user_id=self.user_id,
            load_current_source=self._numbering_source_loader(project),
            preview_token=preview_token, selected_ids=selected_ids,
            idempotency_key=idempotency_key,
        )

    def numbering_undo(
        self, project_id: str, *, preview_token: str, idempotency_key: str,
    ) -> dict[str, Any]:
        from .numbering_sync import undo_numbering_sync

        project = self._project(project_id)
        return undo_numbering_sync(
            project, owner_user_id=self.user_id,
            load_current_source=self._numbering_source_loader(project),
            preview_token=preview_token, idempotency_key=idempotency_key,
        )

    def target_update(
        self,
        project_id: str,
        block_id: str,
        candidate: dict[str, Any] | None,
        expected_active_version: int,
        idempotency_key: str,
        restore_parent_alignment: bool = False,
        sentence_edit: dict[str, Any] | None = None,
        response_mode: str = "full",
    ) -> dict[str, Any]:
        """Append one owner-authored target correction through the block service."""

        try:
            if response_mode not in {"compact", "full"}:
                raise ValueError("response_mode must be compact or full.")
            if not isinstance(idempotency_key, str) or not idempotency_key.strip():
                raise BlockWorkspaceIntegrityError("candidate-idempotency")
            project = self._project(project_id)
            require_document_block_engine(project)
            block = persist_manual_candidate(
                project,
                block_id,
                candidate,
                expected_active_version,
                author_user_id=self.user_id,
                idempotency_key=idempotency_key,
                restore_parent_alignment=restore_parent_alignment,
                **({"sentence_edit": sentence_edit} if sentence_edit is not None else {}),
            )
            db.session.commit()
            if response_mode == "compact":
                from .models import TranslationTargetEditIdempotency

                target = block.get("target") or {}
                editor = target.get("editor") or {}
                alignment = target.get("alignment") or {}
                receipt = TranslationTargetEditIdempotency.query.filter_by(
                    project_id=project.id, actor_type="owner", actor_id=f"user:{self.user_id}",
                    idempotency_key=idempotency_key.strip(),
                ).one()
                review = block.get("review") or {}
                return {
                    "success": True,
                    "block_id": block_id,
                    "target_version": target.get("active_version"),
                    "target_revision_id": receipt.target_revision_id,
                    "editor_sha256": hashlib.sha256(
                        json.dumps(editor, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
                    ).hexdigest(),
                    "alignment": {
                        "review_state": alignment.get("review_state"),
                        "group_count": len(alignment.get("groups") or []),
                    },
                    "review": {key: review[key] for key in (
                        "decision", "effective_decision", "stale", "version",
                        "target_revision_id", "current_target_revision_id", "current_target_version",
                    ) if key in review},
                    "reader_notes_needing_review": sum(
                        note.get("state") == "needs_review" for note in block.get("reader_notes", [])
                    ),
                    "inspect_tool": "carlina_translator_review_fetch",
                }
            return {"success": True, "block": block}
        except BaseException:
            db.session.rollback()
            raise

    def reader_note_create(
        self,
        project_id: str,
        block_id: str,
        body: str,
        anchor: Mapping[str, Any],
        idempotency_key: str,
        citation_ids: Sequence[str] | None = None,
        state: str = "active",
    ) -> dict[str, Any]:
        """Create an owner-authored reader note and return its public projection."""

        try:
            project = self._project(project_id)
            require_document_block_engine(project)
            actor = scholarly_review.ReviewActor("owner", self.user_id)
            result = scholarly_review.create_reader_note(
                project,
                block_id=block_id,
                body=body,
                citations=citation_ids,
                actor=actor,
                anchor=anchor,
                state=state,
                idempotency_key=idempotency_key,
            )
            projected = scholarly_wire.reader_note(result)
            db.session.commit()
            return projected
        except BaseException:
            db.session.rollback()
            raise

    def reader_note_revise(
        self,
        project_id: str,
        note_id: str,
        body: str,
        expected_version: int,
        idempotency_key: str,
        anchor: Mapping[str, Any] | None = None,
        citation_ids: Sequence[str] | None = None,
        state: str | None = None,
    ) -> dict[str, Any]:
        """Revise an owner-scoped reader note with an explicit version fence."""

        try:
            project = self._project(project_id)
            require_document_block_engine(project)
            actor = scholarly_review.ReviewActor("owner", self.user_id)
            result = scholarly_review.revise_reader_note(
                project,
                note_id,
                expected_head_version=expected_version,
                actor=actor,
                body=body,
                citations=citation_ids,
                anchor=anchor,
                state=state,
                idempotency_key=idempotency_key,
            )
            projected = scholarly_wire.reader_note(result)
            db.session.commit()
            return projected
        except BaseException:
            db.session.rollback()
            raise

    def reader_note_set_state(
        self,
        project_id: str,
        note_id: str,
        state: str,
        expected_version: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Set an owner-authored reader note state with an explicit version fence."""

        try:
            project = self._project(project_id)
            require_document_block_engine(project)
            actor = scholarly_review.ReviewActor("owner", self.user_id)
            result = scholarly_review.transition_reader_note(
                project,
                note_id,
                expected_head_version=expected_version,
                next_state=state,
                actor=actor,
                idempotency_key=idempotency_key,
            )
            projected = scholarly_wire.reader_note(result)
            db.session.commit()
            return projected
        except BaseException:
            db.session.rollback()
            raise

    def glossary_get(self, project_id: str, *, query: str = "",
                     offset: int = 0, limit: int = 50,
                     include_observations: bool = True) -> dict[str, Any]:
        """Read current live terminology, including lemma forms and revision fences."""
        from .working_glossary import read_glossary
        return read_glossary(self._project(project_id), query=query, offset=offset,
                             limit=limit, include_observations=include_observations)

    def glossary_propose(self, project_id: str, *, source_plan_id: str,
                         entries: Sequence[Mapping[str, Any]], idempotency_key: str,
                         provenance: str = "passive-executor") -> dict[str, Any]:
        """Save an atomic model-authored terminology delta, never a human lock."""
        from pydantic import ValidationError
        from .block_contract import BlockContractError
        from .working_glossary import propose_glossary
        request = copy.deepcopy({"source_plan_id": source_plan_id, "entries": list(entries),
                                 "idempotency_key": idempotency_key, "provenance": provenance})
        def submit_once(_session: Any) -> dict[str, Any]:
            try:
                return propose_glossary(self._project(project_id), **copy.deepcopy(request))
            except ValidationError as exc:
                raise BlockContractError("glossary-proposal-shape") from exc
        return run_transaction_with_retry(db.session, submit_once,
                                          operation_name="translator_glossary_propose")

    def shared_glossary_list(
        self, *, offset: int = 0, limit: int = 50
    ) -> dict[str, Any]:
        """List this owner's independently retained shared terminology assets."""
        return shared_glossary.list_assets(
            self.user_id, offset=offset, limit=limit
        )

    def shared_glossary_get(
        self,
        asset_id: str,
        *,
        revision: int | None = None,
        offset: int = 0,
        limit: int = 50,
        proposal_offset: int = 0,
        proposal_limit: int = 10,
    ) -> dict[str, Any]:
        """Read one owner-scoped immutable asset revision and its proposals."""
        return shared_glossary.get_asset(
            self.user_id,
            asset_id=asset_id,
            revision=revision,
            offset=offset,
            limit=limit,
            proposal_offset=proposal_offset,
            proposal_limit=proposal_limit,
        )

    def shared_glossary_create(
        self,
        project_id: str,
        *,
        name: str,
        concept_ids: list[str],
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Publish selected current project concepts as advisory shared terms."""
        self._project(project_id)
        return shared_glossary.create_asset(
            self.user_id,
            project_id=project_id,
            name=name,
            concept_ids=concept_ids,
            idempotency_key=idempotency_key,
        )

    def shared_glossary_project(
        self, project_id: str, *, offset: int = 0, limit: int = 100
    ) -> dict[str, Any]:
        """Read this project's local terms and current shared glossary binding."""
        self._project(project_id)
        return shared_glossary.project_state(
            self.user_id, project_id=project_id, offset=offset, limit=limit
        )

    def shared_glossary_preview(
        self,
        project_id: str,
        *,
        asset_id: str,
        revision: int,
        mode: str,
    ) -> dict[str, Any]:
        """Compare one pinned shared revision with the current local glossary."""
        self._project(project_id)
        return shared_glossary.preview_import(
            self.user_id,
            project_id=project_id,
            asset_id=asset_id,
            revision=revision,
            mode=mode,
        )

    def shared_glossary_apply(
        self,
        project_id: str,
        *,
        asset_id: str,
        revision: int,
        mode: str,
        preview_token: str,
        decisions: list[dict[str, str]],
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Apply explicitly reviewed advisory terms at an idle project boundary."""
        self._project(project_id)
        return shared_glossary.apply_import(
            self.user_id,
            project_id=project_id,
            asset_id=asset_id,
            revision=revision,
            mode=mode,
            preview_token=preview_token,
            decisions=decisions,
            idempotency_key=idempotency_key,
        )

    def shared_glossary_detach(
        self,
        project_id: str,
        *,
        expected_version: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Detach the current shared asset using its expected binding version."""
        self._project(project_id)
        return shared_glossary.detach_asset(
            self.user_id,
            project_id=project_id,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
        )

    def shared_glossary_contribute(
        self,
        project_id: str,
        *,
        asset_id: str,
        expected_revision: int,
        concept_ids: list[str],
        retire_ids: list[str] | None = None,
        reason: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Prepare advisory changes for the shared asset owner to review."""
        self._project(project_id)
        return shared_glossary.propose_contribution(
            self.user_id,
            project_id=project_id,
            asset_id=asset_id,
            expected_revision=expected_revision,
            concept_ids=concept_ids,
            retire_ids=retire_ids,
            reason=reason,
            idempotency_key=idempotency_key,
        )

    def shared_glossary_review(
        self,
        asset_id: str,
        *,
        proposal_id: str,
        expected_revision: int,
        decision: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Accept or reject one owner-scoped proposal without implicit adoption."""
        return shared_glossary.review_contribution(
            self.user_id,
            asset_id=asset_id,
            proposal_id=proposal_id,
            expected_revision=expected_revision,
            decision=decision,
            idempotency_key=idempotency_key,
        )

    @staticmethod
    def _batch_settings(project: Any) -> dict[str, Any]:
        settings = project.settings or {}
        payload = {
            "input_tokens": settings.get("translation_batch_input_tokens", DEFAULT_TRANSLATION_BATCH_INPUT_TOKENS),
            "max_blocks": settings.get("translation_batch_max_blocks", DEFAULT_TRANSLATION_BATCH_MAX_BLOCKS),
            "context_mode": normalize_translation_context_mode(
                settings.get("translation_context_mode", "self_contained")
            ),
        }
        version = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        return {**payload, "version": version}

    def configure_batches(self, project_id: str, *, input_tokens: int, max_blocks: int,
                          expected_version: str,
                          context_mode: object = None) -> dict[str, Any]:
        """Change only future batching at an idle boundary, without replanning."""
        from .block_contract import BlockContractError
        if (type(input_tokens) is not int or not 2048 <= input_tokens <= 1_000_000
                or type(max_blocks) is not int or not 1 <= max_blocks <= 256):
            raise BlockContractError("translation-batch-settings")
        requested_context_mode = (
            normalize_translation_context_mode(context_mode)
            if context_mode is not None
            else None
        )
        def configure_once(_session: Any) -> dict[str, Any]:
            project = self._project(project_id)
            require_document_block_engine(project)
            db.session.execute(update(TranslationProject).where(
                TranslationProject.id == project.id, TranslationProject.user_id == self.user_id
            ).values(status=TranslationProject.status))
            db.session.refresh(project)
            if find_active_operation(project.id) is not None:
                raise BlockContractError("translation-batch-operation-active")
            pending = McpWorkItem.query.join(McpExecution).filter(
                McpExecution.resource_id == project.id,
                McpExecution.owner_user_id == self.user_id,
                McpWorkItem.state.in_(("pending", "leased")),
            ).first()
            if pending is not None:
                raise BlockContractError("translation-batch-work-active")
            current = self._batch_settings(project)
            effective_context_mode = requested_context_mode or current["context_mode"]
            if (
                current["input_tokens"] == input_tokens
                and current["max_blocks"] == max_blocks
                and current["context_mode"] == effective_context_mode
            ):
                return {"project_id": project.id, "batch_settings": current, "unchanged": True}
            if current["version"] != expected_version:
                raise BlockContractError("translation-batch-version-conflict")
            settings = dict(project.settings or {})
            settings.update(translation_batch_input_tokens=input_tokens,
                            translation_batch_max_blocks=max_blocks)
            if requested_context_mode is not None:
                settings["translation_context_mode"] = requested_context_mode
            project.settings = settings
            return {"project_id": project.id, "batch_settings": self._batch_settings(project),
                    "source_plan_changed": False}
        return run_transaction_with_retry(db.session, configure_once,
                                          operation_name="translator_configure_batches")

    def context_get(
        self,
        project_id: str,
        *,
        before_block_id: str,
        max_segments: int = 3,
        source_plan_id: str | None = None,
    ) -> dict[str, Any]:
        """Read a bounded source-aligned context tail for one owned project."""

        from .block_contract import BlockContractError
        from .segment_context import context_before_block

        if not isinstance(before_block_id, str) or not before_block_id:
            raise BlockContractError("segment-context-request")
        if type(max_segments) is not int or not 1 <= max_segments <= 3:
            raise BlockContractError("segment-context-request")
        if source_plan_id is not None and (
            not isinstance(source_plan_id, str) or not source_plan_id
        ):
            raise BlockContractError("segment-context-source-plan")

        project = self._project(project_id)
        require_document_block_engine(project)
        workspace_plan = load_block_source_plan(project)
        source_plan = workspace_plan.record
        if source_plan_id is not None and source_plan_id != source_plan.id:
            raise BlockContractError("segment-context-source-plan")
        payload = context_before_block(
            source_plan,
            plan=workspace_plan.plan,
            before_block_id=before_block_id,
            max_segments=max_segments,
        )
        if (
            not isinstance(payload, Mapping)
            or payload.get("source_plan_id") != source_plan.id
        ):
            raise RuntimeError("Translator context returned an invalid source-plan witness.")
        return {"project_id": project.id, **dict(payload)}

    def research_dossier_submit(
        self,
        project_id: str,
        client_submission_id: str,
        payload: Mapping[str, Any],
        provenance: Mapping[str, Any] | None,
        scope: Mapping[str, Any],
        dossier_id: str | None = None,
        expected_revision: int = 0,
    ) -> dict[str, Any]:
        """Submit an owner-scoped research dossier and return its public projection."""

        # Keep only copied request material outside the retry callback.  The
        # callback must rediscover all ORM state after a rollback and each
        # attempt receives fresh mutable inputs in case validation ever starts
        # normalizing them in place.
        request_project_id = copy.deepcopy(project_id)
        request_client_submission_id = copy.deepcopy(client_submission_id)
        request_payload = copy.deepcopy(payload)
        request_provenance = copy.deepcopy(provenance)
        request_scope = copy.deepcopy(scope)
        request_dossier_id = copy.deepcopy(dossier_id)
        request_expected_revision = copy.deepcopy(expected_revision)
        request_user_id = copy.deepcopy(self.user_id)

        def submit_once(_session: Any) -> dict[str, Any]:
            project = self._project(copy.deepcopy(request_project_id))
            require_document_block_engine(project)
            actor = scholarly_review.ReviewActor("owner", request_user_id)
            result = scholarly_review.submit_research_dossier(
                project,
                client_submission_id=copy.deepcopy(request_client_submission_id),
                payload=copy.deepcopy(request_payload),
                provenance=copy.deepcopy(request_provenance),
                scope=copy.deepcopy(request_scope),
                actor=actor,
                dossier_id=copy.deepcopy(request_dossier_id),
                expected_revision=copy.deepcopy(request_expected_revision),
            )
            return scholarly_wire.dossier(
                result, include_payload=True, include_proposals=True
            )

        return run_transaction_with_retry(
            db.session,
            submit_once,
            operation_name="translator_research_dossier_submit",
        )

    def set_mcp_execution(
        self,
        project_id: str,
        execution_id: str,
        *,
        mode: str,
        executor_profile: ExecutorProfile | Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Bind one owner-scoped project to a managed or passive execution.

        The binding is deliberately refused while project work is active.  A
        caller must choose the execution mode before creating the next
        operation; changing it under a worker would make the durable queue
        delivery ambiguous.
        """

        project = self._project(project_id)
        normalized_execution_id = self._mcp_execution_id(execution_id)
        normalized_mode = self._execution_mode(mode)
        normalized_profile = None
        if normalized_mode == "passive":
            if executor_profile is None:
                raise ValueError("Passive execution requires executor_profile.")
            normalized_profile = normalize_executor_profile(executor_profile)
        elif executor_profile is not None:
            raise ValueError("executor_profile is only accepted for passive execution.")
        if reconcile_expired_active_operation(project.id, self.user_id) is not None:
            raise RuntimeError("Project already has translator work in progress.")
        settings = dict(project.settings or {})
        if project.engine_contract == DOCUMENT_BLOCK_ENGINE_CONTRACT:
            settings["translation_unit_mode"] = normalize_translation_unit_mode(
                settings.get("translation_unit_mode")
            )
        settings.update(
            {
                "mcp_execution_id": normalized_execution_id,
                "execution_mode": normalized_mode,
            }
        )
        if normalized_profile is None:
            settings.pop("executor_profile", None)
        else:
            settings["executor_profile"] = normalized_profile.to_dict()
        project.settings = settings
        flag_modified(project, "settings")
        db.session.commit()
        return {
            "project_id": project.id,
            "mcp_execution_id": normalized_execution_id,
            "execution_mode": normalized_mode,
            "executor_profile": (
                normalized_profile.to_dict() if normalized_profile is not None else None
            ),
        }

    def export_metadata_prepare(
        self,
        project_id: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Prepare bounded evidence for the calling executor to author metadata."""

        from .export_metadata_generation import prepare_metadata_draft

        project = self._project(project_id)
        require_document_block_engine(project)
        saved = self._export_metadata_projection(project)
        current = dict(saved["metadata"])
        if metadata is not None:
            current.update(normalize_export_metadata(metadata))
        return prepare_metadata_draft(project, metadata=current, expected_version=saved["version"])

    def export_metadata_generate(
        self,
        project_id: str,
        *,
        metadata: Mapping[str, Any],
        expected_version: int,
        model_id: str | None = None,
    ) -> dict[str, Any]:
        """Generate an editable proposal; never replace saved edition details."""

        from .export_metadata_generation import generate_metadata_draft

        project = self._project(project_id)
        require_document_block_engine(project)
        if isinstance(expected_version, bool) or not isinstance(expected_version, int) or expected_version < 0:
            raise BlockExportError("export-metadata-version")
        if find_active_operation(project.id, owner_user_id=self.user_id) is not None:
            raise BlockExportError("export-metadata-active-operation")
        packet = self.export_metadata_prepare(project_id, metadata)
        if packet["expected_version"] != expected_version:
            raise BlockExportError("export-metadata-version-conflict")
        selected_model = model_id or (project.settings or {}).get("translator_model")
        if not isinstance(selected_model, str) or not selected_model.strip():
            raise BlockExportError("export-metadata-model-required")
        if not packet["editable_fields"]:
            return {"expected_version": expected_version, "metadata": packet["metadata"]}
        result = generate_metadata_draft(project, packet, model_id=selected_model)
        db.session.refresh(project)
        if self._export_metadata_projection(project)["version"] != expected_version:
            raise BlockExportError("export-metadata-version-conflict")
        if load_block_source_plan(project).record.id != packet["source_plan_id"]:
            raise BlockExportError("export-metadata-source-conflict")
        return result

    def export_metadata_update(
        self,
        project_id: str,
        metadata: Mapping[str, Any],
        expected_version: int,
    ) -> dict[str, Any]:
        """CAS-replace saved export metadata for one owner-scoped project.

        The metadata object is a full replacement: an empty object clears all
        saved defaults.  The project row is write-fenced before its state is
        checked, so concurrent SQLite writers serialize and only the caller
        holding the expected version can advance it.  An identical retry of
        the immediately preceding version returns the current receipt without
        writing, including while another Translator operation is active.
        """

        if (
            isinstance(expected_version, bool)
            or not isinstance(expected_version, int)
            or expected_version < 0
        ):
            raise BlockExportError("export-metadata-version")
        request_metadata = normalize_export_metadata(metadata)
        normalized_project_id = str(project_id or "").strip()
        if not normalized_project_id:
            raise ValueError("project_id is required.")

        require_document_block_engine(self._project(normalized_project_id))
        request_metadata = pin_export_metadata_cover(request_metadata, user_id=self.user_id)

        def update_once(_session: Any) -> dict[str, Any]:
            project = self._project(normalized_project_id)
            require_document_block_engine(project)

            # A no-op UPDATE is the repository's cross-dialect write-fence
            # pattern.  It acquires SQLite's single-writer lock (and a row lock
            # on databases that support one) before the version and operation
            # checks, then the refresh discards any stale identity-map state.
            fence = db.session.execute(
                update(TranslationProject)
                .where(
                    TranslationProject.id == project.id,
                    TranslationProject.user_id == self.user_id,
                )
                .values(updated_at=TranslationProject.updated_at)
            )
            if fence.rowcount != 1:
                raise LookupError("Translation project not found.")
            db.session.flush()
            db.session.refresh(project)

            current = self._export_metadata_projection(project)
            current_version = current["version"]
            current_metadata = current["metadata"]
            if (
                current_version == expected_version + 1
                and current_metadata == request_metadata
            ):
                return {"project_id": project.id, **current}
            if current_version != expected_version:
                raise BlockExportError("export-metadata-version-conflict")

            active_operation = (
                TranslationOperation.query.filter(
                    TranslationOperation.project_id == project.id,
                    TranslationOperation.owner_user_id == self.user_id,
                    TranslationOperation.state.in_(ACTIVE_OPERATION_STATES),
                )
                .order_by(
                    TranslationOperation.created_at.desc(),
                    TranslationOperation.id.desc(),
                )
                .first()
            )
            if active_operation is not None:
                raise BlockExportError("export-metadata-active-operation")

            settings = dict(project.settings or {})
            settings["export_metadata"] = copy.deepcopy(request_metadata)
            settings["export_metadata_version"] = expected_version + 1
            project.settings = settings
            flag_modified(project, "settings")
            db.session.flush()
            return {
                "project_id": project.id,
                "version": expected_version + 1,
                "metadata": copy.deepcopy(request_metadata),
            }

        return run_transaction_with_retry(
            db.session,
            update_once,
            operation_name="translator_export_metadata_update",
        )

    def prepare_export_cover(
        self,
        project_id: str,
        source_path: Path,
    ) -> dict[str, Any]:
        """Validate and capture one owner-scoped export cover asset.

        The gateway authorizes ``source_path`` before invoking this method.  A
        project lookup remains the first operation here so a caller cannot use
        this preparation endpoint to probe local files belonging to another
        owner.  The authorized source is copied into the caller's uploads tree
        with a hard byte bound, then passed to the export-cover capability by
        its canonical web path.  Capture never changes project settings; the
        returned path can be supplied later to the metadata CAS update.
        """

        # Keep the owner/project check before any source-path type or filesystem
        # operation.  This is the same owner fence used by every client method.
        project = self._project(project_id)
        require_document_block_engine(project)
        if not isinstance(source_path, Path):
            raise BlockExportError("cover-source-path")

        try:
            if not source_path.is_file():
                raise BlockExportError("cover-source-invalid")
            source_size = source_path.stat().st_size
        except BlockExportError:
            raise
        except (OSError, RuntimeError, ValueError) as exc:
            raise BlockExportError("cover-source-unavailable") from exc
        if source_size > MAX_EXPORT_COVER_IMPORT_BYTES:
            raise BlockExportError("cover-upload-too-large")

        uploads_dir = uploads_root()
        import_dir = uploads_dir / str(self.user_id) / "translator-cover-imports"
        temporary_path: Path | None = None
        try:
            try:
                import_dir.mkdir(parents=True, exist_ok=True)
            except (OSError, RuntimeError, ValueError) as exc:
                raise BlockExportError("cover-upload-unavailable") from exc

            try:
                with tempfile.NamedTemporaryFile(
                    mode="w+b",
                    prefix=".cover-",
                    suffix=".upload",
                    dir=import_dir,
                    delete=False,
                ) as destination:
                    temporary_path = Path(destination.name)
                    try:
                        with source_path.open("rb") as source:
                            copied = 0
                            while True:
                                chunk = source.read(
                                    min(
                                        EXPORT_COVER_COPY_CHUNK_BYTES,
                                        MAX_EXPORT_COVER_IMPORT_BYTES - copied + 1,
                                    )
                                )
                                if not chunk:
                                    break
                                copied += len(chunk)
                                if copied > MAX_EXPORT_COVER_IMPORT_BYTES:
                                    raise BlockExportError("cover-upload-too-large")
                                destination.write(chunk)
                    except BlockExportError:
                        raise
                    except (OSError, RuntimeError, ValueError) as exc:
                        raise BlockExportError("cover-upload-failed") from exc
            except BlockExportError:
                raise
            except (OSError, RuntimeError, ValueError) as exc:
                raise BlockExportError("cover-upload-failed") from exc

            try:
                temporary_web_path = upload_web_path(
                    temporary_path,
                    uploads_dir=uploads_dir,
                )
            except (OSError, RuntimeError, ValueError, TypeError) as exc:
                raise BlockExportError("cover-upload-path") from exc

            try:
                from . import export_cover

                witness = export_cover.capture_export_cover(
                    user_id=self.user_id,
                    upload_path=temporary_web_path,
                )
            except BlockExportError:
                raise
            except Exception as exc:
                code = getattr(exc, "code", None)
                if not isinstance(code, str) or re.fullmatch(
                    r"[a-z0-9][a-z0-9._-]{0,95}", code
                ) is None:
                    code = "cover-capture-failed"
                raise BlockExportError(code) from exc

            if not isinstance(witness, Mapping):
                raise BlockExportError("cover-witness-invalid")
            cover_upload_path = witness.get("upload_path")
            media_type = witness.get("media_type")
            sha256 = witness.get("sha256")
            byte_length = witness.get("byte_length")
            if (
                not isinstance(cover_upload_path, str)
                or not cover_upload_path.startswith("/uploads/")
                or len(cover_upload_path) > 1_000
                or any(marker in cover_upload_path for marker in ("\\", "%", "?", "#"))
                or not isinstance(media_type, str)
                or media_type != "image/jpeg"
                or not isinstance(sha256, str)
                or re.fullmatch(r"[0-9a-f]{64}", sha256) is None
                or isinstance(byte_length, bool)
                or not isinstance(byte_length, int)
                or not 1 <= byte_length <= MAX_EXPORT_COVER_IMPORT_BYTES
            ):
                raise BlockExportError("cover-witness-invalid")
            try:
                canonical_path = upload_web_path(
                    resolve_upload_reference(
                        cover_upload_path,
                        uploads_dir=uploads_dir,
                        require_within_uploads=True,
                    ),
                    uploads_dir=uploads_dir,
                )
            except (OSError, RuntimeError, ValueError, TypeError) as exc:
                raise BlockExportError("cover-witness-invalid") from exc
            if canonical_path != cover_upload_path:
                raise BlockExportError("cover-witness-invalid")
            if cover_upload_path != f"/uploads/{self.user_id}/translator-covers/{sha256}.jpg":
                raise BlockExportError("cover-witness-invalid")
            return {
                "project_id": project.id,
                "cover_upload_path": cover_upload_path,
                "media_type": media_type,
                "sha256": sha256,
                "byte_length": byte_length,
            }
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    # Cleanup is best effort; capture receives no source path
                    # and the exact temporary name is never part of a result.
                    pass

    @staticmethod
    def _mcp_job_state(
        project: TranslationProject,
        operation: TranslationOperation | None,
        *,
        waiting_external: bool = False,
    ) -> str:
        """Map product and operation state into the bounded MCP vocabulary."""

        if operation is not None:
            state = str(operation.state or "").strip().lower()
            if state == "queued":
                return "queued"
            if state == "running":
                return "waiting_external" if waiting_external else "running"
            if state == "cancel_requested":
                return "cancelled"
            if state == "completed":
                return "completed"
            if state in {"partial", "failed"}:
                return "failed"
            if state == "cancelled":
                return "cancelled"

        status = str(project.status or "").strip().lower()
        if status in {"ready", "completed"}:
            return "completed"
        if status in {"partial", "failed", "error"}:
            return "failed"
        if status in {"cancelled", "cancelling"}:
            return "cancelled"
        if waiting_external and status in ACTIVE_PROJECT_STATUSES | {"exporting"}:
            return "waiting_external"
        if status in ACTIVE_PROJECT_STATUSES | {"exporting"}:
            return "running"
        return "queued"

    def _mcp_external_work_pending(
        self,
        project: TranslationProject,
        operation: TranslationOperation | None,
        *,
        execution: McpExecution | None = None,
    ) -> bool:
        """Report pending work only within one owned execution/queue binding."""

        settings = dict(project.settings or {})
        execution_id = (
            execution.id if execution is not None
            else str(settings.get("mcp_execution_id") or "").strip()
        )
        mode = execution.mode if execution is not None else settings.get("execution_mode")
        if (
            str(mode or "managed").strip().lower() != "passive"
            or not execution_id
            or (operation is not None and operation.state != "running")
        ):
            return False
        query = db.session.query(McpWorkItem.id).join(
            McpExecution, McpWorkItem.execution_id == McpExecution.id
        ).filter(
            McpExecution.id == execution_id,
            McpExecution.owner_user_id == self.user_id,
            McpExecution.addon_id == "translator",
            McpExecution.resource_kind == "project",
            McpExecution.resource_id == project.id,
            McpExecution.mode == "passive",
            McpExecution.status.in_(("queued", "running", "waiting_external")),
            McpWorkItem.state.in_(("pending", "leased")),
        )
        if operation is not None:
            query = query.filter(McpExecution.queue_job_id == operation.queue_job_id)
        return query.first() is not None

    def mcp_job_projection(
        self,
        project_id: str,
        *,
        execution_id: str | None = None,
        include_output: bool = False,
        include_glossary: bool = False,
        include_export_metadata: bool = False,
    ) -> dict[str, Any]:
        """Return a bounded owner-scoped projection for MCP job polling."""

        project = self._project(project_id)
        execution = None
        operation_query = TranslationOperation.query.filter_by(
            project_id=project.id,
            owner_user_id=self.user_id,
        )
        if execution_id is not None:
            execution = McpExecution.query.filter_by(
                id=execution_id,
                owner_user_id=self.user_id,
                addon_id="translator",
                resource_kind="project",
                resource_id=project.id,
            ).one_or_none()
            if execution is None:
                raise LookupError(_MCP_EXECUTION_NOT_FOUND)
            operation = (
                operation_query.filter_by(queue_job_id=execution.queue_job_id).one_or_none()
                if execution.queue_job_id else None
            )
        else:
            operation = operation_query.order_by(
                TranslationOperation.created_at.desc(),
                TranslationOperation.id.desc(),
            ).first()
        waiting_external = self._mcp_external_work_pending(
            project, operation, execution=execution,
        )
        state = (
            execution.status
            if execution is not None and (
                execution.status in {"completed", "failed", "cancelled"}
                or operation is None
            )
            else self._mcp_job_state(project, operation, waiting_external=waiting_external)
        )
        current_project_view = execution is None
        if execution is not None and operation is not None:
            latest_id = operation_query.with_entities(TranslationOperation.id).order_by(
                TranslationOperation.created_at.desc(), TranslationOperation.id.desc(),
            ).first()
            current_project_view = (
                str((project.settings or {}).get("mcp_execution_id") or "") == execution.id
                and latest_id is not None and latest_id[0] == operation.id
            )
        latest_operation = None
        if operation is not None:
            latest_operation = serialize_operation(operation)
            for field in ("project_id", "source_content_hash"):
                latest_operation.pop(field, None)
            # MCP has no partial terminal state. Fail the execution while
            # retaining the product outcome for callers inspecting recovery.
            if operation.state != "partial":
                latest_operation["state"] = state
        result = {
            "contract": "translator-mcp-job-projection-v1",
            "project_id": project.id,
            "state": state,
            "status": state,
            "latest_operation": latest_operation,
            "queue_job_id": (execution.queue_job_id if execution is not None else
                             operation.queue_job_id if operation is not None else None),
        }
        if execution is not None:
            result["execution_id"] = execution.id
        if operation is not None and operation.kind == "plan_document_blocks":
            checkpoint = operation.checkpoint_json or {}
            if isinstance(checkpoint, dict):
                phase = checkpoint.get("phase")
                if phase in {"queued", "preparing_source", "segmenter_starting", "segmenting_source", "waiting_for_segmenter_capacity", "plan_verified"}:
                    result["source_planning"] = {"phase": phase}
                    for key in ("source_projections_segmented", "source_sentences_discovered", "segmentation_cache_hits", "segmentation_cache_misses", "blocks", "source_anchors"):
                        value = checkpoint.get(key)
                        if type(value) is int and value >= 0:
                            result["source_planning"][key] = value
        if operation is not None and operation.state == "partial":
            product_result = operation.result_json or {}
            failure = product_result.get("block_failure") if isinstance(product_result, dict) else None
            if isinstance(failure, dict):
                safe = {}
                for key in ("attempt_id", "failure_kind", "dispatch_state", "recovery_action", "validator"):
                    value = failure.get(key)
                    if isinstance(value, str) and re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", value):
                        safe[key] = value
                for key in ("window_order", "remaining_dispatches"):
                    value = failure.get(key)
                    if type(value) is int and 0 <= value <= 1000000:
                        safe[key] = value
                identifiers = failure.get("affected_block_ids")
                if isinstance(identifiers, list):
                    safe["affected_block_ids"] = [
                        value for value in identifiers[:200]
                        if isinstance(value, str) and re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", value)
                    ]
                    safe["affected_blocks_truncated"] = len(identifiers) > 200
                codes = failure.get("validation_codes")
                if isinstance(codes, list):
                    safe["validation_codes"] = [
                        code for code in codes[:21]
                        if isinstance(code, str) and re.fullmatch(r"[a-z0-9_-]{1,128}", code)
                    ]
                result["block_failure"] = safe
        if include_output:
            output: dict[str, Any] = {"state": "unavailable"}
            translation_operation = operation if execution is not None else (
                TranslationOperation.query.filter_by(
                    project_id=project.id,
                    owner_user_id=self.user_id,
                    kind="document_block_translate",
                )
                .order_by(
                    TranslationOperation.created_at.desc(),
                    TranslationOperation.id.desc(),
                )
                .first()
            )
            if (
                project.engine_contract == DOCUMENT_BLOCK_ENGINE_CONTRACT
                and current_project_view
                and translation_operation is not None
                and translation_operation.kind == "document_block_translate"
                and translation_operation.state == "completed"
            ):
                try:
                    if translation_operation.source_plan_id is None:
                        raise RuntimeError("missing source plan")
                    workspace_plan = load_block_source_plan(project)
                    if workspace_plan.record.id != translation_operation.source_plan_id:
                        raise RuntimeError("stale source plan")
                    output = {
                        "state": "available",
                        "format": "html",
                        "mimetype": "text/html",
                        "content": render_persisted_target_document(
                            workspace_plan.record
                        ),
                    }
                except Exception:  # noqa: BLE001 - output projection fails closed.
                    output = {
                        "state": "unavailable",
                        "reason": "verified_target_unavailable",
                    }
            else:
                output["reason"] = (
                    "translation_not_complete" if current_project_view else "execution_not_current"
                )
            result["output"] = output
        if current_project_view and project.engine_contract == DOCUMENT_BLOCK_ENGINE_CONTRACT:
            from .progress_projection import translation_progress

            result["translation_progress"] = translation_progress(project.id)
            result["batch_settings"] = self._batch_settings(project)
        if include_glossary and current_project_view:
            require_document_block_engine(project)
            result["glossary"] = build_block_glossary_payload(
                project, include_proposals=True
            )
        if include_export_metadata and current_project_view:
            result["export_metadata"] = self._export_metadata_projection(project)
        return result

    def cancel_mcp_execution(
        self,
        project_id: str,
        *,
        execution_id: str,
        queue_job_id: str | None,
    ) -> dict[str, Any]:
        """Signal and durably request cancellation for one exact MCP run."""

        project = self._project(project_id)
        settings = dict(project.settings or {})
        if str(settings.get("mcp_execution_id") or "").strip() != str(
            execution_id or ""
        ).strip():
            raise RuntimeError("The project is not bound to this MCP execution.")
        normalized_job_id = str(queue_job_id or "").strip()
        if not normalized_job_id:
            return self._cancel_unbound_queued_operation(project)
        operation = TranslationOperation.query.filter_by(
            project_id=project.id,
            owner_user_id=self.user_id,
            queue_job_id=normalized_job_id,
        ).one_or_none()
        if operation is None:
            raise RuntimeError("The Translator queue binding changed before cancellation.")
        if operation.state in {"completed", "partial", "failed", "cancelled"}:
            return self.mcp_job_projection(project.id)

        try:
            self._queue().request_cancel(
                normalized_job_id,
                requested_by=self.user_id,
                reason="Cancelled through the MCP execution.",
            )
            request_cancellation(operation)
            db.session.commit()
        except TranslationOperationTransitionConflict:
            db.session.rollback()
            winner = TranslationOperation.query.filter_by(
                id=operation.id,
                project_id=project.id,
                owner_user_id=self.user_id,
                queue_job_id=normalized_job_id,
            ).one_or_none()
            if winner is not None and winner.state in {
                "completed",
                "partial",
                "failed",
                "cancelled",
            }:
                return self.mcp_job_projection(project.id)
            raise
        except Exception:
            db.session.rollback()
            raise
        return self.mcp_job_projection(project.id)

    def _cancel_unbound_queued_operation(self, project: TranslationProject) -> dict:
        """Cancel the exact current queued-unbound operation after a bind crash.

        A publish/bind crash commits the queued ledger row without a queue
        delivery, so there is no queue job to signal.  The owner and
        execution fences are already checked by the caller; this resolves
        the single current active operation and requires it to still be
        queued and unbound, then uses the existing ``request_cancellation``
        semantics (queued work finalises immediately to ``cancelled``).  A
        bound active row or no active row means the caller's unbound view
        is stale and is rejected without writing.
        """

        active = find_active_operation(project.id, owner_user_id=self.user_id)
        if (
            active is None
            or str(active.state or "") != "queued"
            or (active.queue_job_id or None) is not None
        ):
            raise RuntimeError(
                "The Translator queue binding changed before cancellation."
            )
        operation_id = active.id
        try:
            request_cancellation(active)
            db.session.commit()
        except TranslationOperationTransitionConflict:
            db.session.rollback()
            winner = TranslationOperation.query.filter_by(
                id=operation_id,
                project_id=project.id,
                owner_user_id=self.user_id,
            ).one_or_none()
            if winner is not None and winner.state in {
                "completed",
                "partial",
                "failed",
                "cancelled",
            }:
                return self.mcp_job_projection(project.id)
            raise
        except Exception:
            db.session.rollback()
            raise
        return self.mcp_job_projection(project.id)

    def create_from_document(
        self,
        document_id: str,
        *,
        name: str,
        source_lang: str = "Auto-detect",
        target_lang: str,
        idempotency_key: str | None = None,
        execution_mode: str = "managed",
        defer_source_planning: bool = False,
        **settings: Any,
    ) -> dict[str, Any]:
        from .source_planning import automatic_creation_key, freeze_planning_checkpoint, planning_payload

        if type(defer_source_planning) is not bool:
            raise ValueError("defer_source_planning must be a boolean.")
        requested_document_id = str(document_id or "").strip()
        if (
            not requested_document_id
            or len(requested_document_id) > MAX_SOURCE_DOCUMENT_ID_CHARS
        ):
            raise ValueError("Source document identifier must be 1-36 characters.")
        project_settings = self._settings(**settings, execution_mode=execution_mode)
        project_settings["source_type"] = "document"
        project = self._new_project(
            name=name,
            source_lang=source_lang,
            target_lang=target_lang,
            document_id=requested_document_id,
            status="setup",
            settings=project_settings,
            persist=False,
        )
        creation_request = {
            "name": name, "source_lang": project.source_lang,
            "target_lang": project.target_lang, "source_type": "document",
            "document_id": requested_document_id, "settings": project_settings,
        }
        captured_snapshot = None
        if defer_source_planning and idempotency_key is None:
            # Snapshot-aware fallback: identical retries coalesce, but edited
            # extractions and intentional new keys create distinct projects.
            provider = self._document_provider()
            captured_snapshot = AttestedDocumentSnapshot.from_mapping(
                provider.client.get_document_snapshot(requested_document_id)
            )
            idempotency_key = automatic_creation_key(captured_snapshot, creation_request)
        project, created = self._create_project(
            project, idempotency_key=idempotency_key, request_payload=creation_request,
        )
        if not created:
            if project.engine_contract != DOCUMENT_BLOCK_ENGINE_CONTRACT:
                raise ValueError(
                    "Existing Translator document projects must use the document-block engine."
                )
            job_id = self._ensure_document_ingestion_delivery(project)
            return {**self._project_payload(project, detail=True), "job_id": job_id}
        try:
            sentence_segmenter = None
            if not defer_source_planning and project_settings["translation_unit_mode"] == "aligned_sentences":
                sentence_segmenter = self._source_sentence_splitter()
            provider = self._document_provider()
            semantic_export_provider = self._semantic_export_provider()
            snapshot = captured_snapshot or AttestedDocumentSnapshot.from_mapping(
                provider.client.get_document_snapshot(requested_document_id)
            )
            resolved_document_id = snapshot.document_id
            if resolved_document_id != requested_document_id:
                raise ValueError(
                    "Source document snapshot identifier does not match the request."
                )
            project.engine_contract = DOCUMENT_BLOCK_ENGINE_CONTRACT
            project.document_id = resolved_document_id
            project_settings.update(
                {
                    "source_type": "document",
                    "source_reference": provider.reference(
                        resource_type="document",
                        resource_id=resolved_document_id,
                    ),
                    "semantic_export_reference": semantic_export_provider.provider_reference(),
                    "source_snapshot_provenance": dict(snapshot.provenance),
                    "source_snapshot_label": str(snapshot.filename or "Snapshotted document")[:255],
                }
            )
            project.settings = dict(project_settings)
            flag_modified(project, "settings")
            project.source_snapshot = snapshot.output_html
            project.source_content_hash = snapshot.content_hash
            project.source_captured_at = self._snapshot_captured_at(snapshot.captured_at)

            if defer_source_planning:
                planner_settings = {
                    "engine_contract": DOCUMENT_BLOCK_ENGINE_CONTRACT,
                    "source_type": "document",
                    **({TRANSLATION_MAX_BLOCK_CHARS_SETTING: project_settings[TRANSLATION_MAX_BLOCK_CHARS_SETTING]}
                       if project_settings["translation_unit_mode"] == "aligned_sentences" else {}),
                }
                checkpoint = freeze_planning_checkpoint(project, snapshot, planner_settings=planner_settings)
                job_id = self._record_job(
                    project, handler="plan_document_blocks", operation="plan_document_blocks",
                    payload=planning_payload(snapshot.content_hash),
                    idempotency_key=f"initial-source-{snapshot.content_hash[:48]}",
                    checkpoint=checkpoint,
                )
                return {**self._project_payload(project), "job_id": job_id}

            # Release SQLite's single writer slot before document planning. In
            # aligned-sentence mode planning may start an on-demand segmenter
            # container and perform substantial CPU inference. Keeping the
            # project's initial INSERT/UPDATE transaction open across that
            # pure computation blocks every worker that needs to renew a lease
            # or report progress. The captured source state is durable here.
            db.session.commit()

            plan_record, _ = plan_and_persist_source_snapshot(
                project,
                snapshot,
                source_language=project.source_lang,
                language_profile=None,
                settings={
                    "engine_contract": DOCUMENT_BLOCK_ENGINE_CONTRACT,
                    "source_type": "document",
                    **(
                        {
                            TRANSLATION_MAX_BLOCK_CHARS_SETTING: project_settings[
                                TRANSLATION_MAX_BLOCK_CHARS_SETTING
                            ]
                        }
                        if project_settings["translation_unit_mode"]
                        == "aligned_sentences"
                        else {}
                    ),
                },
                sentence_segmenter=sentence_segmenter,
            )
        except Exception:
            db.session.rollback()
            # The initial project/snapshot may already have been committed to
            # release SQLite's writer lock before expensive planning. Preserve
            # the historical atomic-create behavior when planning fails before
            # any delivery operation exists.
            persisted = db.session.get(TranslationProject, project.id)
            if persisted is not None:
                operation = (
                    TranslationOperation.query.filter_by(
                        project_id=project.id,
                        owner_user_id=project.user_id,
                    )
                    .order_by(TranslationOperation.created_at.desc())
                    .first()
                )
                if operation is None:
                    db.session.delete(persisted)
                    db.session.commit()
            raise
        job_id = self._record_job(
            project,
            handler="plan_document_blocks",
            operation="plan_document_blocks",
            payload={
                "source_content_hash": plan_record.source_content_hash,
                "source_plan_id": plan_record.id,
                "source_plan_hash": plan_record.plan_hash,
                "engine_contract": DOCUMENT_BLOCK_ENGINE_CONTRACT,
            },
            idempotency_key=f"initial-plan-{plan_record.plan_hash[:48]}",
            source_plan_id=plan_record.id,
            source_plan_hash=plan_record.plan_hash,
        )
        return {**self._project_payload(project), "job_id": job_id}

    def create_from_subtitles(
        self,
        subtitle_project_id: int,
        *,
        name: str,
        source_lang: str = "Auto-detect",
        target_lang: str,
        idempotency_key: str | None = None,
        execution_mode: str = "managed",
        **settings: Any,
    ) -> dict:
        try:
            requested_subtitle_project_id = int(subtitle_project_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("Subtitle project identifier must be an integer.") from exc
        if not 1 <= requested_subtitle_project_id <= 1_000_000_000:
            raise ValueError("Subtitle project identifier is out of range.")
        project_settings = self._settings(**settings, execution_mode=execution_mode)
        project_settings["source_type"] = "subtitle_studio"
        project_settings.pop("translation_unit_mode", None)
        project = self._new_project(
            name=name,
            source_lang=source_lang,
            target_lang=target_lang,
            settings=project_settings,
            persist=False,
        )
        project, created = self._create_project(
            project,
            idempotency_key=idempotency_key,
            request_payload={
                "name": name,
                "source_lang": project.source_lang,
                "target_lang": project.target_lang,
                "source_type": "subtitle_studio",
                "subtitle_project_id": requested_subtitle_project_id,
                "settings": project_settings,
            },
        )
        if not created:
            return self._project_payload(project, detail=True)
        try:
            provider = self._subtitle_provider()
            source = provider.client.prepare_translation_source(
                requested_subtitle_project_id
            )
            if not isinstance(source, Mapping):
                raise TypeError(
                    "Subtitle provider returned an invalid translation source."
                )
            subtitle_project = subtitle_project_metadata(source.get("project"))
            if subtitle_project["id"] != str(requested_subtitle_project_id):
                raise ValueError("Subtitle provider returned a different project.")
            raw_units = source.get("units")
            if not isinstance(raw_units, list) or not raw_units:
                raise ValueError("Subtitle source contains no translatable units.")
            if len(raw_units) > MAX_SUBTITLE_UNITS:
                raise ValueError("Subtitle source contains too many translation units.")
            prepared_units = []
            for unit in raw_units:
                if not isinstance(unit, Mapping):
                    raise TypeError("Subtitle unit metadata must be an object.")
                raw_text = unit.get("source_text", unit.get("text"))
                if not isinstance(raw_text, str):
                    raise TypeError("Subtitle unit source text must be text.")
                text = raw_text.strip()
                if not text:
                    continue
                if len(text) > MAX_SUBTITLE_UNIT_CHARS:
                    raise ValueError(
                        "Subtitle unit source text exceeds 50,000 characters."
                    )
                prepared_units.append((subtitle_unit_metadata(unit), text))
            if not prepared_units:
                raise ValueError(
                    "Subtitle source contains no usable translation units."
                )
            render_settings = subtitle_render_settings(source.get("render_settings"))
            project_settings.update(
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
                        "render_settings": render_settings,
                        "units": {},
                    },
                }
            )
            if source_lang == "Auto-detect":
                provider_language = subtitle_project.get("language")
                if provider_language:
                    if len(provider_language) > 50:
                        raise ValueError("Subtitle project language is too long.")
                    project.source_lang = provider_language

            placeholders = []
            snapshot_units = {}
            snapshot_budget = 128
            for index, (unit_metadata, text) in enumerate(prepared_units):
                snapshot_budget += subtitle_snapshot_entry_bytes(unit_metadata, text)
                if snapshot_budget > MAX_SOURCE_SNAPSHOT_BYTES:
                    raise ValueError(
                        "Subtitle source snapshot exceeds the 20 MiB limit."
                    )
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
                project_settings["subtitle"]["units"][segment_id] = unit_metadata
                snapshot_units[segment_id] = {
                    **unit_metadata,
                    "source_text": text,
                }
                placeholders.append(
                    f'<p data-subtitle-unit="{segment_id}">{{{{{segment_id}}}}}</p>'
                )
            project.skeleton_html = "\n".join(placeholders)
            project.settings = project_settings
            flag_modified(project, "settings")
            project.source_snapshot = json.dumps(
                {
                    "source_type": "subtitle_studio",
                    "project_id": subtitle_project["id"],
                    "units": snapshot_units,
                },
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            project.source_content_hash = hashlib.sha256(
                encoded_subtitle_snapshot(project.source_snapshot)
            ).hexdigest()
            project.source_captured_at = utc_now_db()
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise
        payload = self._project_payload(project, detail=True)
        payload["pre_analysis"] = {
            "enabled": bool(project_settings["enable_pre_analysis"]),
            "state": "requires_analysis"
            if project_settings["enable_pre_analysis"]
            else "not_requested",
            "action": "analyze" if project_settings["enable_pre_analysis"] else None,
        }
        return payload

    def _record_block_translation_job(
        self,
        project: TranslationProject,
        *,
        source_plan: TranslationSourcePlan,
        execution_profile: Mapping[str, Any],
        request_payload: dict[str, Any],
        idempotency_key: str,
        context_mode: str,
        translation_reasoning: str | None = None,
        research_dossier: Mapping[str, Any] | None = None,
    ) -> str:
        return record_block_translation_job(
            project,
            owner_user_id=self.user_id,
            source_plan=source_plan,
            execution_profile=execution_profile,
            request_payload=request_payload,
            idempotency_key=idempotency_key,
            context_mode=context_mode,
            translation_reasoning=translation_reasoning,
            research_dossier=research_dossier,
            publish=self._deliver_operation,
        )

    def start_block_translation(
        self,
        project_id: str,
        *,
        executor_profile: ExecutorProfile | Mapping[str, Any] | None = None,
        research_dossier_revision_id: str | None = None,
        context_mode: object = None,
    ) -> dict[str, Any]:
        """Start the active document-block plan using one exact mode binding."""
        from .source_planning import SourcePlanningNotReady, is_async_source_operation

        project = self._project(project_id)
        if active_source_plan_pointer(project.id) is None:
            source_operation = TranslationOperation.query.filter_by(
                project_id=project.id, owner_user_id=self.user_id, kind="plan_document_blocks",
            ).order_by(TranslationOperation.created_at.desc()).first()
            if source_operation is not None and is_async_source_operation(source_operation):
                raise SourcePlanningNotReady()
        if project.engine_contract != DOCUMENT_BLOCK_ENGINE_CONTRACT:
            raise ValueError("Translator start supports document-block projects only.")
        if context_mode is not None:
            normalize_translation_context_mode(context_mode)
        if reconcile_expired_active_operation(project.id, self.user_id) is not None:
            raise RuntimeError("Project already has translator work in progress.")
        self._validate_existing_settings(project, require_analysis=False)
        settings = dict(project.settings or {})
        execution_mode = self._execution_mode(settings.get("execution_mode", "managed"))
        effective_context_mode = resolve_translation_context_mode(
            settings, override=context_mode, execution_mode=execution_mode
        )
        normalized_profile = None
        if execution_mode == "passive":
            if executor_profile is None:
                raise ValueError("Passive block translation requires executor_profile.")
            try:
                normalized_profile = normalize_executor_profile(executor_profile)
            except (TypeError, ValueError) as exc:
                raise ValueError(str(exc)) from exc
            model_capabilities = normalized_profile.capability_profile("translator")
            capability_hash = hashlib.sha256(
                json.dumps(
                    model_capabilities.to_dict(),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            execution = ModelExecutionProfile(
                "translator",
                {
                    "capability_profile": model_capabilities.to_dict(),
                    "capability_profile_hash": capability_hash,
                },
                "native_json_schema"
                if model_capabilities.supports_structured_output
                else "strict_text_json",
                capability_hash,
            )
        else:
            execution = resolve_execution_profile(
                project,
                model_id=str(settings.get("translator_model") or ""),
            )
        pointer = active_source_plan_pointer(project.id)
        if pointer is None:
            raise RuntimeError("The active document-block source plan is missing.")
        source_plan = db.session.get(TranslationSourcePlan, pointer.source_plan_id)
        if source_plan is None or source_plan.project_id != project.id:
            raise RuntimeError("The active document-block source plan is invalid.")
        hydrated_source_plan = rehydrate_source_plan(source_plan)
        research_dossier = None
        if research_dossier_revision_id is not None:
            research_dossier = scholarly_review.block_translation_dossier_pin(
                project,
                source_plan,
                research_dossier_revision_id,
                blocks=hydrated_source_plan.blocks,
            )
        window_profile = derive_window_profile(
            execution,
            maximum_blocks=int(
                settings.get(
                    "translation_batch_max_blocks", DEFAULT_TRANSLATION_BATCH_MAX_BLOCKS
                )
            ),
            target_input_tokens=int(
                settings.get(
                    "translation_batch_input_tokens",
                    DEFAULT_TRANSLATION_BATCH_INPUT_TOKENS,
                )
            ),
        )
        request_payload = {
            "engine_contract": DOCUMENT_BLOCK_ENGINE_CONTRACT,
            "source_content_hash": source_plan.source_content_hash,
            "source_plan_id": source_plan.id,
            "source_plan_hash": source_plan.plan_hash,
            "execution_mode": execution_mode,
            "translation_context": translation_context_policy(
                effective_context_mode
            ),
            "translation_unit_mode": normalize_translation_unit_mode(
                settings.get("translation_unit_mode")
            ),
        }
        if normalized_profile is not None:
            request_payload["executor_profile"] = normalized_profile.to_dict()
        idempotency_key = (
            f"translate-{source_plan.plan_hash[:48]}-ctxv2-{effective_context_mode}"
        )
        if research_dossier is not None:
            idempotency_key = (
                f"{idempotency_key}-dossier-{research_dossier['revision_id']}"
            )
            if len(idempotency_key) > 128:
                idempotency_key = (
                    f"translate-{source_plan.plan_hash[:48]}-ctxv2-"
                    f"{effective_context_mode}-dossier-"
                    f"{hashlib.sha256(research_dossier['revision_id'].encode('utf-8')).hexdigest()[:16]}"
                )
            request_payload["research_dossier_revision_id"] = research_dossier[
                "revision_id"
            ]
        translation_history = (
            TranslationOperation.query.filter_by(
                project_id=project.id,
                owner_user_id=self.user_id,
                kind="document_block_translate",
                source_plan_id=source_plan.id,
                source_plan_hash=source_plan.plan_hash,
                source_content_hash=source_plan.source_content_hash,
            )
            .order_by(
                TranslationOperation.created_at.desc(),
                TranslationOperation.id.desc(),
            )
            .all()
        )
        workflow_configs = {
            row.operation_id: row
            for row in (
                TranslationBlockWorkflowConfig.query.filter(
                    TranslationBlockWorkflowConfig.operation_id.in_(
                        [item.id for item in translation_history]
                    )
                ).all()
                if translation_history
                else []
            )
        }
        missing_config = object()

        def frozen_dossier_revision_id(item: TranslationOperation):
            row = workflow_configs.get(item.id)
            if row is None:
                return missing_config
            frozen = row.config_json if isinstance(row.config_json, Mapping) else {}
            dossier = frozen.get("research_dossier")
            return (
                dossier.get("revision_id")
                if isinstance(dossier, Mapping)
                else None
            )

        def frozen_context_matches(item: TranslationOperation) -> bool:
            row = workflow_configs.get(item.id)
            if row is None or not isinstance(row.config_json, Mapping):
                return False
            return row.config_json.get("translation_context") == translation_context_policy(
                effective_context_mode
            )

        latest_overall_translation = (
            translation_history[0] if translation_history else None
        )
        if (
            latest_overall_translation is not None
            and not frozen_context_matches(latest_overall_translation)
        ):
            idempotency_key = _context_policy_epoch_idempotency_key(
                base_key=idempotency_key,
                source_plan_hash=source_plan.plan_hash,
                context_mode=effective_context_mode,
                dossier_revision_id=research_dossier_revision_id,
                preceding_operation_id=latest_overall_translation.id,
            )
            latest_translation = None
        else:
            latest_translation = next(
                (
                    item
                    for item in translation_history
                    if frozen_dossier_revision_id(item)
                    == research_dossier_revision_id
                    and frozen_context_matches(item)
                ),
                None,
            )
        if latest_translation is not None:
            if latest_translation.state in {"failed", "partial", "cancelled"}:
                idempotency_key = f"recover-{latest_translation.id}"
                if research_dossier_revision_id is not None:
                    idempotency_key = (
                        f"{idempotency_key}-dossier-{research_dossier_revision_id}"
                    )
            else:
                # Once a recovery operation exists, idempotent starts must keep
                # addressing the newest operation with the same dossier selection
                # instead of being diverted by a newer run with a different pin.
                idempotency_key = latest_translation.idempotency_key
        job_id = self._record_block_translation_job(
            project,
            source_plan=source_plan,
            execution_profile={
                "window_profile": window_profile.to_dict(),
                "executor_profile": normalized_profile,
            },
            request_payload=request_payload,
            idempotency_key=idempotency_key,
            context_mode=effective_context_mode,
            translation_reasoning=None,
            research_dossier=research_dossier,
        )
        return {**self._project_payload(project), "job_id": job_id}

    def translate(
        self,
        project_id: str | None = None,
        *,
        text: str | None = None,
        name: str = "Translation",
        source_lang: str = "Auto-detect",
        target_lang: str | None = None,
        segment_id: str | None = None,
        expected_version: int | None = None,
        **settings: Any,
    ) -> dict:
        """Queue one Subtitle Studio segment or the subtitle project."""

        if project_id is None:
            raise ValueError("project_id is required; document translation is block-only.")
        project = self._project(project_id)
        self._require_subtitle_project(project)
        if reconcile_expired_active_operation(project.id, self.user_id) is not None:
            raise RuntimeError("Project already has translator work in progress.")
        self._validate_existing_settings(project, require_analysis=False)
        if segment_id:
            segment = TranslationSegment.query.filter_by(
                id=str(segment_id),
                project_id=project.id,
            ).one_or_none()
            if segment is None:
                raise LookupError("Translation segment not found.")
            if expected_version is None:
                raise ValueError(
                    "expected_version is required for segment translation."
                )
            try:
                expected_version = int(expected_version)
            except (TypeError, ValueError) as exc:
                raise ValueError("expected_version must be an integer.") from exc
            if expected_version != segment.version:
                raise RuntimeError(
                    "Segment changed before translation; latest_version="
                    + str(segment.version)
                )
            job_id = self._record_job(
                project,
                handler="translate_single_segment",
                operation="translate_segment",
                payload={
                    "segment_id": segment.id,
                    "segment_version": expected_version,
                },
            )
        else:
            pending_count = TranslationSegment.query.filter(
                TranslationSegment.project_id == project.id,
                TranslationSegment.merged_into_id.is_(None),
                TranslationSegment.status.in_(["pending", "error"]),
            ).count()
            if pending_count < 1:
                raise ValueError("Project has no pending segments to translate.")
            project.status = "translating"
            job_id = self._record_job(
                project,
                handler="translate_segments",
                operation="translate",
            )
        return {**self._project_payload(project), "job_id": job_id}

    def analyze(self, project_id: str) -> dict:
        project = self._project(project_id)
        self._require_subtitle_project(project)
        if reconcile_expired_active_operation(project.id, self.user_id) is not None:
            raise RuntimeError("Project already has translator work in progress.")
        self._validate_existing_settings(project, require_analysis=True)
        job_id = self._record_job(
            project,
            handler="analyze_terminology",
            operation="analyze",
        )
        return {**self._project_payload(project), "job_id": job_id}

    def review(
        self,
        project_id: str,
        segment_id: str,
        *,
        translated_text: str,
        status: str = "human_approved",
        expected_version: int | None = None,
    ) -> dict:
        project = self._project(project_id)
        self._require_subtitle_project(project)
        segment = TranslationSegment.query.filter_by(
            id=str(segment_id),
            project_id=project.id,
        ).one_or_none()
        if segment is None:
            raise LookupError("Translation segment not found.")
        normalized_text = str(translated_text or "").strip()
        if not normalized_text:
            raise ValueError("Reviewed translation cannot be empty.")
        if len(normalized_text) > 200_000:
            raise ValueError("Reviewed translation exceeds 200,000 characters.")
        normalized_status = str(status or "human_approved").strip().lower()
        if normalized_status not in {
            "approved",
            "flagged",
            "human_approved",
            "needs_review",
        }:
            raise ValueError("Invalid review status.")
        if expected_version is None:
            raise ValueError("expected_version is required for review.")
        try:
            expected_version = int(expected_version)
        except (TypeError, ValueError) as exc:
            raise ValueError("expected_version must be an integer.") from exc
        if expected_version < 1:
            raise ValueError("expected_version must be positive.")
        text_changed = normalized_text != segment.translated_text
        changed = text_changed or normalized_status != segment.status
        if not changed:
            return segment.to_dict()
        won = TranslationSegment.query.filter_by(
            id=segment.id,
            project_id=project.id,
            version=expected_version,
        ).update(
            {
                "translated_text": normalized_text,
                "status": normalized_status,
                "context_note": None
                if normalized_status != "flagged"
                else segment.context_note,
                "version": TranslationSegment.version + 1,
            },
            synchronize_session=False,
        )
        if won != 1:
            db.session.rollback()
            latest = TranslationSegment.query.filter_by(
                id=segment.id, project_id=project.id
            ).one()
            raise RuntimeError(
                "Segment changed before review; latest_version=" + str(latest.version)
            )
        if text_changed:
            TranslationNote.query.filter_by(
                project_id=project.id,
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
                    translated_text=normalized_text,
                    author_type="human",
                    user_id=self.user_id,
                )
            )
        db.session.commit()
        db.session.expire_all()
        return (
            TranslationSegment.query.filter_by(id=segment.id, project_id=project.id)
            .one()
            .to_dict()
        )

    def export(self, project_id: str, *, export_format: str = "html") -> dict:
        project = self._project(project_id)
        self._require_subtitle_project(project)
        normalized_format = str(export_format or "html").strip().lower()
        if normalized_format == "html":
            return {
                "kind": "inline",
                "format": "html",
                "mimetype": "text/html",
                "content": render_project_html(project, for_export=True),
            }
        if normalized_format in SUBTITLE_EXPORT_FORMATS:
            if (project.settings or {}).get("source_type") != "subtitle_studio":
                raise ValueError(
                    "Subtitle export is only available for subtitle projects."
                )
            reference = source_reference(
                project.settings,
                capability="subtitles.authoring",
            )
            provider = self._subtitle_provider(reference)
            cues = translated_subtitle_cues(project, subtitle_provider=provider)
            exported = provider.client.export_translation_cues(
                cues,
                normalized_format,
            )
            if not isinstance(exported, dict) or "content" not in exported:
                raise TypeError("Subtitle provider returned an invalid export.")
            return {"kind": "inline", "format": normalized_format, **exported}
        if normalized_format not in PACKAGE_EXPORT_FORMATS:
            raise ValueError(f"Unsupported translation export: {export_format!r}.")
        job_id = self._record_job(
            project,
            handler="export_project",
            operation="export",
            payload={"format": normalized_format},
        )
        return {
            "kind": "job",
            "format": normalized_format,
            "project_id": project.id,
            "job_id": job_id,
        }

    def semantic_export_preflight(
        self,
        project_id: str,
        *,
        epub_options: object,
        epub_cover: object = None,
        epub_image_mode: object = "jpeg",
        numbering_revision: int | None = None,
    ) -> dict[str, Any]:
        """Review one owner's exact persisted EPUB target without building it."""

        project = self._project(project_id)
        require_document_block_engine(project)
        from carlina.core.epub_cover import normalize_epub_cover
        from carlina.core.epub_export import (
            EpubExportContractError,
            normalize_epub_image_mode,
            normalize_epub_options,
        )

        try:
            normalized_options = normalize_epub_options(epub_options)
            image_mode = normalize_epub_image_mode(epub_image_mode)
        except EpubExportContractError as exc:
            raise BlockExportError(exc.code) from None
        if not normalized_options:
            raise BlockExportError("epub-preflight-profile")
        try:
            cover_options = normalize_epub_cover(epub_cover)
        except ValueError:
            raise BlockExportError("semantic-export-cover-option") from None
        from .numbering_sync import numbering_revision as current_numbering_revision

        current_revision = current_numbering_revision(str(project.id))
        if numbering_revision is not None and (
            type(numbering_revision) is not int
            or numbering_revision < 0
            or numbering_revision != current_revision
        ):
            raise BlockExportError("numbering-export-revision-conflict")
        workspace_plan = load_block_source_plan(project)
        settings = project.settings or {}
        reference = settings.get("semantic_export_reference")
        if reference is not None and not isinstance(reference, dict):
            raise BlockExportError("semantic-export-provider-reference")
        provider = self._semantic_export_provider(reference)
        provider_witness = provider.provider_reference()
        effective_metadata = merge_project_export_metadata(
            project,
            use_uploaded_cover=not cover_options,
        )
        cover_uri = None
        cover_upload_path = effective_metadata.get("cover_upload_path")
        if (
            isinstance(cover_upload_path, str)
            and cover_upload_path
            and not cover_options
        ):
            from .export_cover import resolve_export_cover_path

            cover_uri = resolve_export_cover_path(
                user_id=int(project.user_id),
                upload_path=cover_upload_path,
            )
        request_value = build_semantic_export_request(
            workspace_plan.record,
            export_format="epub",
            provider_witness=provider_witness,
            export_metadata=effective_metadata,
            cover_uri=cover_uri,
            epub_cover=cover_options,
            epub_options=normalized_options,
            epub_image_mode=image_mode,
        )
        provider_request = request_value.to_dict()
        expected_review_hash = semantic_export_review_hash(provider_request)
        if current_numbering_revision(str(project.id)) != current_revision:
            raise BlockExportError("numbering-export-revision-conflict")
        raw_result = provider.client.semantic_export_preflight(
            provider_request
        )
        if current_numbering_revision(str(project.id)) != current_revision:
            raise BlockExportError("numbering-export-revision-conflict")
        result = validate_epub_preflight_result(
            raw_result,
            semantic_html_sha256=request_value.semantic_html_sha256,
            epub_options=normalized_options,
            expected_review_hash=expected_review_hash,
        )
        result["numbering_revision"] = current_revision
        from .epub_editions import current_source_revision

        result["edition_revision"] = current_source_revision(project)
        return result

    def epub_edition_get(self, project_id: str) -> dict[str, Any]:
        from .epub_editions import get_epub_edition

        return get_epub_edition(owner_id=self.user_id, project_id=project_id)

    def epub_edition_save(self, project_id: str, *, recipe: object, expected_version: int, expected_source_revision: str) -> dict[str, Any]:
        from .epub_editions import save_epub_edition

        return save_epub_edition(owner_id=self.user_id, project_id=project_id, recipe=recipe, expected_version=expected_version, expected_source_revision=expected_source_revision)

    def epub_edition_clear(self, project_id: str, *, expected_version: int) -> dict[str, Any]:
        from .epub_editions import clear_epub_edition

        return clear_epub_edition(owner_id=self.user_id, project_id=project_id, expected_version=expected_version)

    def export_document(
        self,
        project_id: str,
        *,
        export_format: str = "epub",
        idempotency_key: str,
        start_h2_on_new_page: bool = False,
        review_requirement: str = "none",
        epub_cover: object = None,
        epub_options: object = _EPUB_OPTIONS_UNSET,
        epub_review_hash: object = _EPUB_REVIEW_HASH_UNSET,
        epub_image_mode: object = _EPUB_IMAGE_MODE_UNSET,
    ) -> dict[str, Any]:
        """Queue one pinned semantic export for an owner-scoped block project."""

        project = self._project(project_id)
        require_document_block_engine(project)
        from carlina.core.epub_cover import normalize_epub_cover

        cover_options = normalize_epub_cover(epub_cover)
        if cover_options and export_format != "epub":
            raise ValueError("Cover selection requires EPUB.")
        if review_requirement not in {"none", "structural", "target_reading", "bilingual"}:
            raise ValueError("Unknown export review requirement.")
        if not isinstance(export_format, str) or export_format not in {"epub", "docx", "html", "pdf"}:
            raise ValueError("Document export format must be epub, docx, html, or pdf.")
        if type(start_h2_on_new_page) is not bool or (
            start_h2_on_new_page and export_format not in {"docx", "pdf"}
        ):
            raise ValueError("start_h2_on_new_page requires a DOCX or PDF export boolean.")
        if (
            not isinstance(idempotency_key, str)
            or not idempotency_key.strip()
            or len(idempotency_key.strip()) > 128
        ):
            raise ValueError("A nonblank document export idempotency key of at most 128 characters is required.")
        normalized_key = _require_idempotency_key(idempotency_key)
        existing = TranslationOperation.query.filter_by(
            project_id=project.id,
            owner_user_id=self.user_id,
            kind="document_block_export",
            idempotency_key=normalized_key,
        ).one_or_none()
        from carlina.core.epub_export import normalize_epub_image_mode

        saved_mode = (
            (existing.checkpoint_json or {}).get("epub_image_mode", "auto")
            if existing
            else "jpeg"
            if export_format == "epub"
            else "auto"
        )
        normalized_image_mode = normalize_epub_image_mode(
            saved_mode if epub_image_mode is _EPUB_IMAGE_MODE_UNSET else epub_image_mode
        )
        if (
            epub_image_mode is not _EPUB_IMAGE_MODE_UNSET
            and normalized_image_mode != "auto"
            and export_format != "epub"
        ):
            raise ValueError("EPUB image conversion requires an EPUB export.")
        supplied_epub_options = epub_options is not _EPUB_OPTIONS_UNSET
        if supplied_epub_options and export_format != "epub":
            raise ValueError("EPUB publishing options require an EPUB export.")
        supplied_epub_review_hash = epub_review_hash is not _EPUB_REVIEW_HASH_UNSET
        if supplied_epub_review_hash and export_format != "epub":
            raise ValueError("EPUB review hashes require an EPUB export.")
        from .block_export import normalize_epub_review_hash

        normalized_epub_review_hash = None
        if supplied_epub_review_hash:
            normalized_epub_review_hash = normalize_epub_review_hash(
                epub_review_hash
            )
        elif export_format == "epub" and existing is not None:
            saved = existing.checkpoint_json or {}
            if isinstance(saved, Mapping) and "epub_review_hash" in saved:
                normalized_epub_review_hash = normalize_epub_review_hash(
                    saved["epub_review_hash"]
                )
        from carlina.core.epub_export import normalize_epub_options

        normalized_epub_options = None
        if supplied_epub_options:
            if epub_options is not None:
                normalized_epub_options = normalize_epub_options(epub_options)
        elif export_format == "epub":
            if existing is None:
                normalized_epub_options = normalize_epub_options({})
            else:
                saved = existing.checkpoint_json or {}
                if isinstance(saved, Mapping) and "epub_options" in saved:
                    normalized_epub_options = normalize_epub_options(
                        saved["epub_options"]
                    )
        workspace_plan = load_block_source_plan(project)
        settings = project.settings or {}
        reference = settings.get("semantic_export_reference")
        if reference is not None and not isinstance(reference, dict):
            raise ValueError("The saved semantic export provider reference is invalid.")
        provider = self._semantic_export_provider(reference)
        provider_reference = provider.provider_reference()
        export_metadata = normalize_export_metadata(settings.get("export_metadata", {}))
        if cover_options:
            export_metadata.pop("cover_upload_path", None)
        export_metadata = pin_export_metadata_cover(export_metadata, user_id=self.user_id)
        from .numbering_sync import numbering_revision

        request_payload = {
            "format": export_format,
            "numbering_revision": numbering_revision(str(project.id)),
            "export_metadata": export_metadata,
            "source_content_hash": project.source_content_hash,
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
        if start_h2_on_new_page:
            request_payload["start_h2_on_new_page"] = True
        if cover_options:
            request_payload["epub_cover"] = cover_options
        if review_requirement != "none":
            request_payload["review_requirement"] = review_requirement
        if existing is None:
            from .review_coverage import snapshot_export_review

            request_payload["review_manifest"] = snapshot_export_review(
                project, requirement=review_requirement,
            )
        else:
            saved = existing.checkpoint_json or {}
            manifest = saved.get("review_manifest")
            if manifest is None:
                manifest = (existing.result_json or {}).get("review_manifest")
            if manifest is not None:
                request_payload["review_manifest"] = manifest
        job_id = self._record_job(
            project,
            handler="export_document_blocks",
            operation="document_block_export",
            payload=request_payload,
            source_plan_id=workspace_plan.record.id,
            source_plan_hash=workspace_plan.record.plan_hash,
            idempotency_key=normalized_key,
        )
        operation = TranslationOperation.query.filter_by(
            project_id=project.id,
            owner_user_id=self.user_id,
            kind="document_block_export",
            idempotency_key=normalized_key,
        ).one_or_none()
        if operation is None or (job_id and operation.queue_job_id != job_id):
            raise RuntimeError("The document export operation could not be found.")
        return export_job_projection(project, operation)

    def export_document_get(
        self,
        project_id: str,
        operation_id: str,
        *,
        include_content: bool = False,
        offset: int = 0,
        limit: int = 65536,
        expected_sha256: str | None = None,
    ) -> dict[str, Any]:
        """Read one exact export operation and optionally one bounded file chunk."""

        project = self._project(project_id)
        validate_read_options(operation_id, include_content, offset, limit, expected_sha256)
        operation = TranslationOperation.query.filter_by(
            id=operation_id if isinstance(operation_id, str) else "",
            project_id=project.id,
            owner_user_id=self.user_id,
            kind="document_block_export",
        ).one_or_none()
        if operation is None:
            raise LookupError("Document export operation not found.")
        return read_export(
            project=project,
            operation=operation,
            queue=self._queue(),
            operation_id=operation_id,
            include_content=include_content,
            offset=offset,
            limit=limit,
            expected_sha256=expected_sha256,
        )

    def job_status(self, project_id: str, job_id: str) -> dict:
        project = self._project(project_id)
        operation = TranslationOperation.query.filter_by(
            queue_job_id=str(job_id), project_id=project.id, owner_user_id=self.user_id
        ).one_or_none()
        if operation is None:
            raise LookupError("Translation job not found for this project.")
        status = self._queue().get_status(str(job_id))
        result = status.get("result") if isinstance(status, dict) else None
        if isinstance(result, dict):
            result_user_id = result.get("user_id")
            result_project_id = result.get("project_id")
            if result_user_id is not None and int(result_user_id) != self.user_id:
                raise PermissionError("Translation job is not visible to this user.")
            if result_project_id is not None and str(result_project_id) != project.id:
                raise PermissionError("Translation job belongs to another project.")
        raw = status if isinstance(status, dict) else {}
        safe_operation = serialize_operation(operation)
        safe_operation.pop("project_id", None)
        safe_operation.pop("queue_job_id", None)
        safe_operation.pop("source_content_hash", None)
        payload = {
            "status": str(raw.get("status") or "unknown"),
            "progress": raw.get("progress")
            if isinstance(raw.get("progress"), (int, float))
            else None,
            "operation": safe_operation,
        }
        if isinstance(result, dict) and result.get("kind") == "file_download":
            payload["result"] = {"kind": "file_download"}
        return payload


__all__ = ["TranslatorClient"]
