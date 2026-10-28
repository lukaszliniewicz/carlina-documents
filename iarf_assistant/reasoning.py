"""Shared persistence and resolution for add-on reasoning preferences."""

from __future__ import annotations

import re
from typing import Any

from .models import LLMModel, User


REASONING_LEVELS = ("default", "off", "minimal", "low", "medium", "high", "xhigh")
CONFIGURABLE_REASONING_LEVELS = REASONING_LEVELS[1:]
_CONFIGURABLE_LEVELS = frozenset(CONFIGURABLE_REASONING_LEVELS)
_SLOT_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,79}$")


def normalize_reasoning_level(value: Any, *, allow_default: bool = True) -> str:
    """Return a validated, canonical reasoning level."""
    level = str(value or "default").strip().lower()
    allowed = REASONING_LEVELS if allow_default else _CONFIGURABLE_LEVELS
    if level not in allowed:
        raise ValueError(f"Unsupported reasoning level: {value!r}.")
    return level


def supported_reasoning_levels(model: LLMModel | None) -> list[str]:
    """Return only the reasoning levels explicitly recorded for a model."""
    if not model or not model.supports_reasoning:
        return []

    raw_levels = model.supported_reasoning_levels or ""
    if isinstance(raw_levels, str):
        candidates = raw_levels.split(",")
    elif isinstance(raw_levels, (list, tuple, set)):
        candidates = raw_levels
    else:
        candidates = []

    levels: list[str] = []
    for candidate in candidates:
        try:
            level = normalize_reasoning_level(candidate, allow_default=False)
        except ValueError:
            continue
        if level not in levels:
            levels.append(level)

    return levels


def model_reasoning_metadata(model: LLMModel | None) -> dict[str, Any]:
    return {
        "supports_reasoning": bool(model and model.supports_reasoning),
        "supported_reasoning_levels": supported_reasoning_levels(model),
    }


def get_visible_model(user: User | None, model_id: str) -> LLMModel | None:
    """Find the model effective for a user, preferring their private override."""
    model_id = str(model_id or "").strip()
    if not model_id:
        return None

    if user:
        private_model = LLMModel.query.filter_by(user_id=user.id, model_id=model_id).first()
        if private_model and (private_model.is_active or private_model.manually_added):
            return private_model

    return LLMModel.query.filter_by(user_id=None, model_id=model_id, is_active=True).first()


def is_reasoning_level_supported(user: User | None, model_id: str, level: str) -> bool:
    """Whether a persisted override is safe to send for the selected model."""
    level = normalize_reasoning_level(level)
    if level == "default":
        return True

    model = get_visible_model(user, model_id)
    return bool(model and level in supported_reasoning_levels(model))


def get_addon_reasoning_settings(user: User | None, addon_id: str) -> dict[str, dict[str, dict[str, str]]]:
    """Return normalized reasoning settings stored under one add-on."""
    addon_settings = user.addon_settings if user and isinstance(user.addon_settings, dict) else {}
    addon_settings = addon_settings.get(addon_id)
    if not isinstance(addon_settings, dict):
        return {"slots": {}}

    reasoning = addon_settings.get("reasoning")
    slots = reasoning.get("slots") if isinstance(reasoning, dict) else {}
    if not isinstance(slots, dict):
        return {"slots": {}}

    normalized_slots: dict[str, dict[str, str]] = {}
    for slot, config in slots.items():
        if not isinstance(slot, str) or not _SLOT_PATTERN.fullmatch(slot) or not isinstance(config, dict):
            continue
        try:
            level = normalize_reasoning_level(config.get("level"))
        except ValueError:
            continue
        model_id = str(config.get("model_id") or "").strip()
        normalized_slots[slot] = {"model_id": model_id, "level": level}
    return {"slots": normalized_slots}


def validate_reasoning_slot_updates(user: User, slots: Any) -> dict[str, dict[str, str]]:
    """Validate API updates before they are persisted to encrypted user JSON."""
    if not isinstance(slots, dict):
        raise ValueError("slots must be an object.")

    validated: dict[str, dict[str, str]] = {}
    for slot, config in slots.items():
        if not isinstance(slot, str) or not _SLOT_PATTERN.fullmatch(slot):
            raise ValueError("Reasoning slot names must contain only lowercase letters, digits, underscores, or hyphens.")
        if not isinstance(config, dict):
            raise ValueError(f"Reasoning configuration for '{slot}' must be an object.")

        level = normalize_reasoning_level(config.get("level"))
        model_id = str(config.get("model_id") or "").strip()
        if level != "default":
            if not model_id:
                raise ValueError(f"A model is required when setting reasoning for '{slot}'.")
            if not is_reasoning_level_supported(user, model_id, level):
                raise ValueError(f"{model_id} does not support the '{level}' reasoning level.")

        validated[slot] = {"model_id": model_id, "level": level}
    return validated


def resolve_reasoning_config(
    user: User | None,
    model_id: str,
    explicit_config: dict[str, Any] | None,
    *,
    addon_id: str | None = None,
    reasoning_slot: str = "default",
) -> dict[str, str] | None:
    """Resolve a completion's reasoning config without leaking chat settings into add-ons.

    Explicit caller input always wins. Calls with an add-on scope use a matching
    add-on slot (then its ``default`` slot). Only unscoped calls retain the
    legacy chat-wide ``llm_settings.thinking_configs`` fallback.
    """
    if explicit_config is not None:
        if not isinstance(explicit_config, dict):
            return None
        try:
            return {"level": normalize_reasoning_level(explicit_config.get("level"))}
        except ValueError:
            return None

    if addon_id:
        slots = get_addon_reasoning_settings(user, addon_id)["slots"]
        config = slots.get(reasoning_slot) or slots.get("default")
        if not config or config.get("model_id") != model_id:
            return None
        level = config.get("level", "default")
        if level == "default" or not is_reasoning_level_supported(user, model_id, level):
            return None
        return {"level": level}

    addon_settings = user.addon_settings if user and isinstance(user.addon_settings, dict) else {}
    llm_settings = addon_settings.get("llm_settings") if isinstance(addon_settings, dict) else {}
    thinking_configs = llm_settings.get("thinking_configs") if isinstance(llm_settings, dict) else {}
    legacy_config = thinking_configs.get(model_id) if isinstance(thinking_configs, dict) else None
    if not isinstance(legacy_config, dict):
        return None
    try:
        return {"level": normalize_reasoning_level(legacy_config.get("level"))}
    except ValueError:
        return None
