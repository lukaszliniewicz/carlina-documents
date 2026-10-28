"""Immutable reasoning settings for a Document Processor run.

The add-on reasoning preference is mutable user configuration.  A document
run, however, must not change its thinking level because somebody changed a
preference while a queue worker was waiting.  This module turns the effective
models and the visible model metadata into a small, content-free snapshot at
the persistence boundary.

This is deliberately separate from the host reasoning-preference module: that
module owns the user preference API and keeps a backwards-compatible fallback
for old model rows.  Quality-sensitive document processing cannot use that
fallback, because a model row that claims reasoning support but has no
advertised levels is not enough evidence to select a level.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any

from ...llm_provider_utils import (
    canonical_provider_name,
    infer_provider_name,
)
from ...reasoning import (
    get_addon_reasoning_settings,
    get_visible_model,
    normalize_reasoning_level,
)

REASONING_SNAPSHOT_VERSION = 1
REASONING_SNAPSHOT_KEY = "reasoning_snapshot"
REASONING_ADDON_ID = "doc_processor"
REASONING_SLOTS = ("parsing", "metadata")

SOURCE_EXPLICIT_ADDON_PREFERENCE = "explicit_addon_preference"
SOURCE_BOOK_QUALITY_DEFAULT = "book_quality_default"
SOURCE_MODEL_DEFAULT = "model_default"
SOURCE_NO_REASONING = "no_reasoning"
SNAPSHOT_SOURCES = frozenset(
    {
        SOURCE_EXPLICIT_ADDON_PREFERENCE,
        SOURCE_BOOK_QUALITY_DEFAULT,
        SOURCE_MODEL_DEFAULT,
        SOURCE_NO_REASONING,
    }
)

# A model may advertise xhigh, but quality defaults must never silently select
# it.  The order intentionally reflects the levels understood by the current
# provider adapter.
BOOK_QUALITY_LEVEL_ORDER = ("high", "medium", "low", "minimal", "off")
_MAX_MODEL_ID_LENGTH = 255
_MAX_PROVIDER_LENGTH = 80
_MAX_LEVELS = 16


class ReasoningSnapshotError(ValueError):
    """A run cannot safely be persisted or executed with its model settings."""


def _normal_model_id(value: Any) -> str:
    model_id = str(value or "").strip()
    if not model_id:
        raise ReasoningSnapshotError(
            "A reasoning snapshot requires a model identifier."
        )
    if len(model_id) > _MAX_MODEL_ID_LENGTH:
        raise ReasoningSnapshotError("The reasoning model identifier is too long.")
    return model_id


def _raw_supported_levels(model: Any) -> list[str]:
    """Parse only levels explicitly advertised by the visible model row."""

    raw = getattr(model, "supported_reasoning_levels", None)
    if isinstance(raw, str):
        candidates: Sequence[Any] = raw.split(",")
    elif isinstance(raw, (list, tuple, set, frozenset)):
        candidates = list(raw)
    else:
        candidates = []

    levels: list[str] = []
    for candidate in candidates[:_MAX_LEVELS]:
        try:
            level = normalize_reasoning_level(candidate, allow_default=False)
        except ValueError:
            continue
        if level not in levels:
            levels.append(level)
    return levels


def _provider_for_model(model_id: str, model: Any) -> str:
    provider = getattr(model, "provider", None)
    provider_name = getattr(provider, "name", None)
    canonical = canonical_provider_name(provider_name)
    # A configured provider row is authoritative even when a model has a
    # provider-neutral identifier such as ``gemini-3.7-flash``.  Infer only
    # when the visible record has no provider relationship.
    inferred = infer_provider_name(model_id, None) if not canonical else None
    normalized = canonical_provider_name(canonical or inferred)
    if not normalized:
        raise ReasoningSnapshotError(
            f"The provider for model '{model_id}' could not be resolved."
        )
    if len(normalized) > _MAX_PROVIDER_LENGTH:
        raise ReasoningSnapshotError("The reasoning provider identifier is too long.")
    return normalized


def _user_slot_config(user: Any, slot: str) -> dict[str, str] | None:
    settings = get_addon_reasoning_settings(user, REASONING_ADDON_ID)
    slots = settings.get("slots", {}) if isinstance(settings, Mapping) else {}
    config = slots.get(slot) or slots.get("default")
    return dict(config) if isinstance(config, Mapping) else None


def _explicit_level(user: Any, model_id: str, slot: str) -> str | None:
    config = _user_slot_config(user, slot)
    if not config or str(config.get("model_id") or "").strip() != model_id:
        return None
    try:
        level = normalize_reasoning_level(config.get("level"), allow_default=True)
    except ValueError:
        return None
    return None if level == "default" else level


def _snapshot_entry(
    *,
    user: Any,
    model_id: str,
    slot: str,
    profile: str | None,
) -> dict[str, Any]:
    model_id = _normal_model_id(model_id)
    model = get_visible_model(user, model_id)
    if model is None:
        raise ReasoningSnapshotError(
            f"The selected {slot} model '{model_id}' is not visible to this user; "
            "refresh model settings before queuing the document."
        )

    supports_reasoning = bool(getattr(model, "supports_reasoning", False))
    supported_levels = _raw_supported_levels(model) if supports_reasoning else []
    explicit_level = _explicit_level(user, model_id, slot)

    if explicit_level is not None:
        if explicit_level not in supported_levels:
            raise ReasoningSnapshotError(
                f"The configured {slot} reasoning level '{explicit_level}' is not "
                f"advertised by model '{model_id}'. Update the add-on reasoning "
                "preference or select a compatible model."
            )
        level = explicit_level
        source = SOURCE_EXPLICIT_ADDON_PREFERENCE
    elif not supports_reasoning:
        # This is a visible quality downgrade.  Passing off explicitly also
        # prevents an old mutable add-on preference from being picked up by
        # LLMService's compatibility resolver.
        level = "off"
        source = SOURCE_NO_REASONING
    elif profile == "book_quality":
        if not supported_levels:
            raise ReasoningSnapshotError(
                f"Model '{model_id}' claims reasoning support but advertises no "
                "reasoning levels. Book Quality requires explicit model metadata; "
                "refresh the model catalogue or choose a model with advertised levels."
            )
        level = next(
            (
                candidate
                for candidate in BOOK_QUALITY_LEVEL_ORDER
                if candidate in supported_levels
            ),
            None,
        )
        if level is None:
            raise ReasoningSnapshotError(
                f"Model '{model_id}' advertises no sane Book Quality reasoning level."
            )
        source = SOURCE_BOOK_QUALITY_DEFAULT
    else:
        # ``default`` is intentional: LLMService will not add a provider
        # thinking parameter, and the model's own default remains authoritative.
        level = "default"
        source = SOURCE_MODEL_DEFAULT

    return {
        "model_id": model_id,
        "provider": _provider_for_model(model_id, model),
        "level": level,
        "supported_levels": list(supported_levels),
        "source": source,
    }


def build_reasoning_snapshot(
    settings: Mapping[str, Any],
    user: Any,
    *,
    profile: str | None = None,
) -> dict[str, Any]:
    """Build a bounded, content-free snapshot from finalized settings."""

    if not isinstance(settings, Mapping):
        raise ReasoningSnapshotError(
            "Document settings are required for reasoning snapshotting."
        )
    effective_profile = (
        profile or str(settings.get("processing_profile") or "").strip().lower() or None
    )
    entries = {
        slot: _snapshot_entry(
            user=user,
            model_id=settings.get(f"{slot}_model"),
            slot=slot,
            profile=effective_profile,
        )
        for slot in REASONING_SLOTS
    }
    return {"version": REASONING_SNAPSHOT_VERSION, "slots": entries}


def validate_reasoning_snapshot(
    snapshot: Any,
    *,
    settings: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate and return a deep-copied snapshot suitable for execution."""

    if (
        not isinstance(snapshot, Mapping)
        or snapshot.get("version") != REASONING_SNAPSHOT_VERSION
    ):
        raise ReasoningSnapshotError(
            "Unsupported or missing reasoning snapshot version."
        )
    raw_slots = snapshot.get("slots")
    if not isinstance(raw_slots, Mapping) or set(raw_slots) != set(REASONING_SLOTS):
        raise ReasoningSnapshotError(
            "Reasoning snapshot must contain parsing and metadata slots."
        )

    normalized: dict[str, Any] = {"version": REASONING_SNAPSHOT_VERSION, "slots": {}}
    for slot in REASONING_SLOTS:
        entry = raw_slots.get(slot)
        if not isinstance(entry, Mapping):
            raise ReasoningSnapshotError(
                f"Reasoning snapshot slot '{slot}' is invalid."
            )
        model_id = _normal_model_id(entry.get("model_id"))
        provider = str(entry.get("provider") or "").strip().lower()
        if not provider or len(provider) > _MAX_PROVIDER_LENGTH:
            raise ReasoningSnapshotError(
                f"Reasoning snapshot provider for '{slot}' is invalid."
            )
        try:
            level = normalize_reasoning_level(entry.get("level"), allow_default=True)
        except ValueError as exc:
            raise ReasoningSnapshotError(
                f"Reasoning snapshot level for '{slot}' is invalid."
            ) from exc
        supported = entry.get("supported_levels")
        if not isinstance(supported, (list, tuple)) or len(supported) > _MAX_LEVELS:
            raise ReasoningSnapshotError(
                f"Reasoning snapshot supported levels for '{slot}' are invalid."
            )
        normalized_supported: list[str] = []
        for value in supported:
            try:
                candidate = normalize_reasoning_level(value, allow_default=False)
            except ValueError as exc:
                raise ReasoningSnapshotError(
                    f"Reasoning snapshot supported level for '{slot}' is invalid."
                ) from exc
            if candidate not in normalized_supported:
                normalized_supported.append(candidate)
        source = str(entry.get("source") or "").strip()
        if source not in SNAPSHOT_SOURCES:
            raise ReasoningSnapshotError(
                f"Reasoning snapshot source for '{slot}' is invalid."
            )
        if (
            level != "default"
            and level not in normalized_supported
            and source != SOURCE_NO_REASONING
        ):
            raise ReasoningSnapshotError(
                f"Reasoning snapshot level for '{slot}' is not in its advertised levels."
            )
        if source == SOURCE_NO_REASONING and level != "off":
            raise ReasoningSnapshotError(
                "A no-reasoning snapshot must use the off level."
            )
        if settings is not None:
            expected = str(settings.get(f"{slot}_model") or "").strip()
            if expected and expected != model_id:
                raise ReasoningSnapshotError(
                    f"Reasoning snapshot model mismatch for {slot}: expected '{expected}'."
                )
        normalized["slots"][slot] = {
            "model_id": model_id,
            "provider": provider,
            "level": level,
            "supported_levels": normalized_supported,
            "source": source,
        }
    return deepcopy(normalized)


def snapshot_matches_models(snapshot: Any, settings: Mapping[str, Any]) -> bool:
    try:
        validate_reasoning_snapshot(snapshot, settings=settings)
    except ReasoningSnapshotError:
        return False
    return True


def thinking_config_for_call(
    snapshot: Any,
    slot: str,
    actual_model_id: str,
) -> dict[str, str] | None:
    """Return explicit provider-neutral thinking config, or legacy ``None``."""

    if snapshot is None:
        return None
    if slot not in REASONING_SLOTS:
        raise ReasoningSnapshotError(f"Unknown reasoning snapshot slot '{slot}'.")
    normalized = validate_reasoning_snapshot(snapshot)
    entry = normalized["slots"][slot]
    actual = _normal_model_id(actual_model_id)
    if entry["model_id"] != actual:
        raise ReasoningSnapshotError(
            f"Reasoning snapshot model mismatch for {slot}: expected '{entry['model_id']}', got '{actual}'."
        )
    return {"level": entry["level"]}


def reasoning_debug_metadata(snapshot: Any) -> dict[str, Any]:
    """Return the only safe reasoning fields suitable for metrics/debug data."""

    normalized = validate_reasoning_snapshot(snapshot)
    return {
        "version": normalized["version"],
        "slots": {
            slot: {
                "model_id": entry["model_id"],
                "provider": entry["provider"],
                "level": entry["level"],
                "source": entry["source"],
            }
            for slot, entry in normalized["slots"].items()
        },
    }


__all__ = [
    "BOOK_QUALITY_LEVEL_ORDER",
    "REASONING_ADDON_ID",
    "REASONING_SLOTS",
    "REASONING_SNAPSHOT_KEY",
    "REASONING_SNAPSHOT_VERSION",
    "ReasoningSnapshotError",
    "build_reasoning_snapshot",
    "reasoning_debug_metadata",
    "snapshot_matches_models",
    "thinking_config_for_call",
    "validate_reasoning_snapshot",
]
