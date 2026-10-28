"""Shared model-tier resolution for execution and planning surfaces."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, Final

from iarf_assistant.extensions import db
from iarf_assistant.models import User

from .settings_cache import get_settings_snapshot

MODEL_TIER_FALLBACKS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "fast": "gemini/gemini-2.5-flash",
        "medium": "gemini/gemini-2.5-flash",
        "powerful": "gemini/gemini-2.5-pro",
    }
)


def user_llm_settings(user_settings: Any) -> dict[str, Any]:
    """Return a defensive copy of the user's model-tier preferences."""

    if not isinstance(user_settings, Mapping):
        return {}
    nested = user_settings.get("llm_settings")
    result = dict(nested) if isinstance(nested, Mapping) else {}
    # Preserve the legacy top-level shape without letting it override the
    # explicit llm_settings namespace.
    for tier in MODEL_TIER_FALLBACKS:
        key = f"default_{tier}_model"
        if key not in result and key in user_settings:
            result[key] = user_settings[key]
    return result


def resolve_model_tier(
    tier: str,
    *,
    system_settings: Mapping[str, Any],
    user_settings: Mapping[str, Any] | None = None,
) -> tuple[str, str]:
    """Resolve a tier to ``(model_id, source)`` with one shared precedence."""

    normalized_tier = tier if tier in MODEL_TIER_FALLBACKS else "fast"
    setting_key = f"default_{normalized_tier}_model"
    user_value = str((user_settings or {}).get(setting_key) or "").strip()
    if user_value:
        return user_value, "user_settings"
    system_value = str(system_settings.get(setting_key) or "").strip()
    if system_value:
        return system_value, "runtime_settings"
    return MODEL_TIER_FALLBACKS[normalized_tier], "built_in_fallback"


class ModelRouter:
    """Resolve model tiers or explicit model IDs for execution."""

    ROLE_TIER_MAPPING: Final[dict[str, str]] = {}
    DEFAULT_FALLBACKS = MODEL_TIER_FALLBACKS

    @classmethod
    def get_model_for_role(cls, role: str, user_id: int | None = None) -> str:
        tier = role if role in cls.DEFAULT_FALLBACKS else "fast"
        return cls.get_model_for_tier(tier, user_id)

    @classmethod
    def get_model_for_tier(cls, tier: str, user_id: int | None = None) -> str:
        """Resolve user preference, system default, then built-in fallback."""

        preferences: dict[str, Any] = {}
        if user_id is not None:
            user = db.session.get(User, int(user_id))
            if user is not None:
                preferences = user_llm_settings(user.addon_settings)
        model_id, _source = resolve_model_tier(
            tier,
            system_settings=get_settings_snapshot(),
            user_settings=preferences,
        )
        return model_id

    @classmethod
    def resolve_model(
        cls,
        assigned_model_role: str,
        default_tier: str = "fast",
        user_id: int | None = None,
    ) -> str:
        if not assigned_model_role:
            return cls.get_model_for_tier(default_tier, user_id)

        direct_model_prefixes = (
            "gpt-",
            "o1",
            "o3",
            "o4",
            "o5",
            "claude-",
            "gemini-",
            "llama",
            "mistral",
            "deepseek",
            "qwen",
        )
        if "/" in assigned_model_role or assigned_model_role.startswith(
            direct_model_prefixes
        ):
            return assigned_model_role
        return cls.get_model_for_role(assigned_model_role, user_id)


__all__ = [
    "MODEL_TIER_FALLBACKS",
    "ModelRouter",
    "resolve_model_tier",
    "user_llm_settings",
]
