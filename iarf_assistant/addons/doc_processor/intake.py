"""Shared document-ingestion settings and model-readiness contracts.

This module is intentionally independent from Flask routes so standalone and
project uploads cannot quietly drift apart.  The parser remains the owner of
the prompt text and the detailed PDF workflow; this module only normalizes the
settings that are persisted before a job is queued.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any

from ...core.model_capabilities import resolve_model_capabilities
from ...core.model_readiness import (
    resolve_model_profile_readiness,
    resolve_model_readiness,
)
from .reasoning_snapshot import (
    REASONING_SNAPSHOT_KEY,
    ReasoningSnapshotError,
    build_reasoning_snapshot,
    snapshot_matches_models,
)

PROFILE_BOOK_QUALITY = "book_quality"
PROFILE_CUSTOM = "custom"
SUPPORTED_PROFILES = frozenset({PROFILE_BOOK_QUALITY, PROFILE_CUSTOM})

# Start with a bounded representative window so multimodal providers do not
# pay the latency and retry cost of re-uploading dozens of pages.  A partial
# visible TOC can request one explicit expansion after this initial pass.
BOOK_QUALITY_METADATA_START_PAGES = 12
BOOK_QUALITY_METADATA_END_PAGES = 4

_PDF_OPTION_MAP = {
    "removehf": "removehf",
    "evaluate_pdf": "evaluate",
    "evaluate": "evaluate",
    "append_previous": "append_previous",
    "ignore_footnotes": "ignore_footnotes",
    "simple_footnotes": "simple_footnotes",
    "llm_boundary_merge": "llm_boundary_merge",
    "correct_images": "correct_images",
    "extract_images": "extract_images",
    "parallel_pages": "parallel_pages",
    "page_range": "page_range",
    "excluded_pages": "excluded_pages",
    "metadata_prepass": "metadata_prepass",
    "metadata_pages": "metadata_pages",
    "metadata_end_pages": "metadata_end_pages",
    "metadata_exclude_flagged_pages": "metadata_delete_flagged_pages",
    "metadata_delete_flagged_pages": "metadata_delete_flagged_pages",
    "toc_handling": "toc_handling",
    "prompt_cache": "prompt_cache",
    "skip_cropping": "skip_cropping",
    "store_debug_artifacts": "store_debug_artifacts",
}

_BOOLEAN_PDF_OPTIONS = frozenset(
    {
        "removehf",
        "append_previous",
        "evaluate",
        "extract_images",
        "ignore_footnotes",
        "simple_footnotes",
        "llm_boundary_merge",
        "correct_images",
        "metadata_prepass",
        "metadata_delete_flagged_pages",
        "prompt_cache",
        "skip_cropping",
        "store_debug_artifacts",
    }
)


def _coerce_bool(value: Any, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False
    return default


def normalize_pdf_processing_options(
    pdf_options: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Normalize all supported PDF options while preserving legacy defaults."""

    normalized = dict(pdf_options or {})
    # This legacy flag was never consumed by the parser; retaining it creates
    # the illusion that it changes behavior, so do not persist it.
    normalized.pop("append_next", None)
    legacy_delete_flagged_pages = normalized.pop("metadata_exclude_flagged_pages", None)
    if (
        "metadata_delete_flagged_pages" not in normalized
        and legacy_delete_flagged_pages is not None
    ):
        normalized["metadata_delete_flagged_pages"] = legacy_delete_flagged_pages
    for key in _BOOLEAN_PDF_OPTIONS:
        if key in normalized:
            normalized[key] = _coerce_bool(normalized[key], default=False)
    try:
        parallel_pages = int(normalized.get("parallel_pages", 1) or 1)
    except TypeError, ValueError:
        parallel_pages = 1
    normalized["parallel_pages"] = max(1, min(parallel_pages, 10))

    try:
        metadata_pages = int(normalized.get("metadata_pages", 20) or 20)
    except TypeError, ValueError:
        metadata_pages = 20
    normalized["metadata_pages"] = max(1, min(metadata_pages, 100))

    try:
        metadata_end_pages = int(normalized.get("metadata_end_pages", 0) or 0)
    except TypeError, ValueError:
        metadata_end_pages = 0
    normalized["metadata_end_pages"] = max(0, min(metadata_end_pages, 100))

    # Previous-page context and parallel extraction are mutually exclusive.
    # Keep this safety invariant in the shared contract, not only in the UI.
    if normalized["parallel_pages"] > 1:
        normalized["append_previous"] = False

    normalized.setdefault("removehf", False)
    normalized.setdefault("append_previous", True)
    normalized.setdefault("evaluate", False)
    normalized.setdefault("extract_images", True)
    normalized.setdefault("ignore_footnotes", False)
    normalized.setdefault("simple_footnotes", False)
    normalized.setdefault("llm_boundary_merge", False)
    normalized.setdefault("metadata_prepass", True)
    normalized.setdefault("metadata_delete_flagged_pages", False)
    normalized.setdefault("prompt_cache", True)
    normalized.setdefault("correct_images", True)
    normalized.setdefault("skip_cropping", False)
    normalized.setdefault("store_debug_artifacts", False)
    # Printed TOCs are metadata evidence, not canonical body content. The
    # metadata pre-pass transcribes their hierarchy and locators; page
    # extraction and every derived document omit the printed duplicate.
    normalized["toc_handling"] = "omit"
    return normalized


def _profile_id(data: Mapping[str, Any]) -> str | None:
    raw = data.get("processing_profile")
    if raw in (None, ""):
        raw = data.get("profile_id", data.get("profile"))
    if raw in (None, ""):
        return None
    profile = str(raw).strip().lower()
    if profile not in SUPPORTED_PROFILES:
        raise ValueError(
            f"Unknown document processing profile '{profile}'. "
            f"Expected one of: {', '.join(sorted(SUPPORTED_PROFILES))}."
        )
    return profile


def _effective_profile_options(
    pdf_options: Mapping[str, Any],
    profile: str | None,
) -> dict[str, Any]:
    options = normalize_pdf_processing_options(pdf_options)
    if profile != PROFILE_BOOK_QUALITY:
        return options

    # Quality profile values are hard invariants. Evaluation, visual boundary
    # correction, and debug-artifact retention remain user-controlled.
    # Printed TOCs are captured by the metadata pass
    # and omitted from canonical body extraction.
    options.update(
        {
            "parallel_pages": 1,
            "append_previous": True,
            "metadata_prepass": True,
            "ignore_footnotes": False,
            "simple_footnotes": False,
            "extract_images": True,
            "correct_images": True,
            "skip_cropping": False,
            "prompt_cache": True,
            "removehf": True,
            "metadata_pages": BOOK_QUALITY_METADATA_START_PAGES,
            "metadata_end_pages": BOOK_QUALITY_METADATA_END_PAGES,
            "toc_handling": "omit",
        }
    )
    # Re-normalize so the effective profile snapshot carries the same
    # canonical defaults as every other intake path.
    return normalize_pdf_processing_options(options)


def get_processing_profile(data: Mapping[str, Any] | None) -> str | None:
    """Return the canonical profile ID while accepting legacy input aliases."""

    return _profile_id(dict(data or {}))


def build_document_settings(
    data: Mapping[str, Any] | None,
    *,
    default_model: str | None = None,
    effective_engine: str | None = None,
    requested_engine: str | None = None,
    user: Any | None = None,
) -> dict[str, Any]:
    """Build one persisted settings object for standalone/project ingestion."""

    values = dict(data or {})
    profile = _profile_id(values)
    requested = (
        str(
            requested_engine
            if requested_engine is not None
            else values.get("engine", values.get("engine_id", "auto"))
        ).strip()
        or "auto"
    )
    engine = str(effective_engine or requested).strip() or "auto"

    settings: dict[str, Any] = {
        "engine_id": engine,
        "parsing_model": values.get("parsing_model") or default_model,
        "metadata_model": values.get("metadata_model") or default_model,
    }
    if requested != engine:
        settings["requested_engine_id"] = requested

    if profile is not None:
        settings["processing_profile"] = profile

    crop_box = values.get("crop_box")
    if crop_box is not None:
        settings["crop_box"] = crop_box
    crop_boxes = values.get("crop_boxes")
    if isinstance(crop_boxes, dict):
        settings["crop_boxes"] = deepcopy(crop_boxes)

    pdf_options = dict(values.get("pdf_options") or {})
    for source_key, target_key in _PDF_OPTION_MAP.items():
        if source_key in values:
            pdf_options[target_key] = values[source_key]
    settings["pdf_options"] = _effective_profile_options(pdf_options, profile)
    if (
        user is not None
        and settings.get("parsing_model")
        and settings.get("metadata_model")
    ):
        try:
            settings[REASONING_SNAPSHOT_KEY] = build_reasoning_snapshot(settings, user)
        except ReasoningSnapshotError as exc:
            # Model readiness is checked immediately before persistence by the
            # route.  Keep that older, more actionable error for a completely
            # unconfigured model; once a visible model exists, metadata errors
            # (including missing advertised levels) remain fail-closed.
            if "not visible to this user" not in str(exc):
                raise
    return settings


def build_effective_settings(
    document_settings: Mapping[str, Any] | None,
    overrides: Mapping[str, Any] | None = None,
    *,
    user: Any | None = None,
) -> dict[str, Any]:
    """Merge project overrides and reapply the persisted profile invariants."""

    settings = deepcopy(dict(document_settings or {}))
    original_models = {
        key: str(settings.get(key) or "").strip()
        for key in ("parsing_model", "metadata_model")
    }
    original_profile = str(settings.get("processing_profile") or "").strip().lower()
    existing_snapshot = deepcopy(settings.get(REASONING_SNAPSHOT_KEY))
    original_pdf_options = dict(settings.get("pdf_options") or {})
    for key, value in (overrides or {}).items():
        if key == "pdf_options" and isinstance(value, Mapping):
            settings["pdf_options"] = {
                **dict(settings.get("pdf_options") or {}),
                **dict(value),
            }
        else:
            settings[key] = deepcopy(value)
    profile = _profile_id(settings)
    if profile is not None:
        settings["processing_profile"] = profile
        settings.pop("profile_id", None)
        settings.pop("profile", None)
    settings["pdf_options"] = _effective_profile_options(
        settings.get("pdf_options") or {}, profile
    )
    # Project runs historically execute pages sequentially so previous-page
    # context remains deterministic.  The quality profile already requires it;
    # retain the invariant for legacy project runs too.
    settings["pdf_options"]["parallel_pages"] = 1
    if profile != PROFILE_BOOK_QUALITY and "append_previous" in original_pdf_options:
        # Project scheduling has always serialized pages.  Preserve the
        # caller's explicit context choice when normalizing a legacy project
        # snapshot whose input happened to request parallel pages.
        settings["pdf_options"]["append_previous"] = original_pdf_options[
            "append_previous"
        ]
    effective_models = {
        key: str(settings.get(key) or "").strip()
        for key in ("parsing_model", "metadata_model")
    }
    models_changed = effective_models != original_models
    profile_changed = (str(profile or "").strip().lower() or "") != original_profile
    if existing_snapshot is not None and not models_changed and not profile_changed:
        if snapshot_matches_models(existing_snapshot, settings):
            settings[REASONING_SNAPSHOT_KEY] = existing_snapshot
        else:
            raise ReasoningSnapshotError(
                "The persisted reasoning snapshot is invalid for these document settings."
            )
    elif (
        user is not None
        and effective_models["parsing_model"]
        and effective_models["metadata_model"]
    ):
        settings[REASONING_SNAPSHOT_KEY] = build_reasoning_snapshot(settings, user)
    elif models_changed and existing_snapshot is not None:
        raise ReasoningSnapshotError(
            "A project model override requires a visible user model to rebuild the reasoning snapshot."
        )
    return settings


def validate_pdf_model_readiness(
    filename: str,
    settings: Mapping[str, Any],
    *,
    user_id: int,
    require_credential: bool = True,
) -> str | None:
    """Return a safe validation message when a vision PDF model is unusable."""

    if Path(str(filename or "")).suffix.lower() != ".pdf":
        return None
    if str(settings.get("engine_id") or "").strip().lower() != "llm_vision":
        return None

    pdf_options = dict(settings.get("pdf_options") or {})
    model_keys = ["parsing_model"]
    if pdf_options.get("metadata_prepass", True):
        model_keys.append("metadata_model")

    for model_key in model_keys:
        model_id = str(settings.get(model_key) or "").strip()
        readiness = (
            resolve_model_readiness(model_id, user_id=user_id)
            if require_credential
            else resolve_model_profile_readiness(model_id, user_id=user_id)
        )
        if not readiness.ready:
            return (
                f"{model_key.replace('_', ' ').capitalize()} '{model_id}' is not ready: "
                f"{readiness.message}"
            )
        try:
            capabilities = resolve_model_capabilities(
                model_id,
                user_id=user_id,
            )
        except LookupError, RuntimeError, TypeError, ValueError:
            return (
                f"{model_key.replace('_', ' ').capitalize()} '{model_id}' "
                "capabilities could not be resolved."
            )
        if not capabilities.supports_vision:
            return (
                f"Model '{model_id}' does not support image input and cannot "
                "process PDF pages."
            )
    return None


def validate_pdf_model_readiness_batch(
    filenames_and_settings: list[tuple[str, Mapping[str, Any]]],
    *,
    user_id: int,
) -> str | None:
    """Validate every effective PDF model before a project batch is stored."""

    for filename, settings in filenames_and_settings:
        error = validate_pdf_model_readiness(filename, settings, user_id=user_id)
        if error:
            return f"'{Path(str(filename)).name}': {error}"
    return None


__all__ = [
    "BOOK_QUALITY_METADATA_END_PAGES",
    "BOOK_QUALITY_METADATA_START_PAGES",
    "PROFILE_BOOK_QUALITY",
    "PROFILE_CUSTOM",
    "SUPPORTED_PROFILES",
    "build_document_settings",
    "build_effective_settings",
    "get_processing_profile",
    "normalize_pdf_processing_options",
    "validate_pdf_model_readiness",
    "validate_pdf_model_readiness_batch",
]
