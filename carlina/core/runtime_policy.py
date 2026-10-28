from __future__ import annotations

from collections.abc import Mapping
from typing import Any

RUNTIME_POLICY_SCHEMA_VERSION = 1
REVIEW = "review"
FULL_ACCESS = "full_access"
VALID_POLICY_PROFILES = {REVIEW, FULL_ACCESS}
CONTEXT_MANUAL = "manual"
CONTEXT_ASSISTED = "assisted"
CONTEXT_AGGRESSIVE = "aggressive"
VALID_CONTEXT_STEWARDSHIP_MODES = {CONTEXT_MANUAL, CONTEXT_ASSISTED, CONTEXT_AGGRESSIVE}
MAX_TOOL_CALL_ITERATIONS = 50


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on", "enabled"}


def _as_int(
    value: Any, default: int, *, minimum: int | None = None, maximum: int | None = None
) -> int:
    try:
        resolved = int(str(value if value is not None else default).strip())
    except TypeError, ValueError:
        resolved = int(default)
    if minimum is not None:
        resolved = max(minimum, resolved)
    if maximum is not None:
        resolved = min(maximum, resolved)
    return resolved


def clamp_tool_call_iterations(value: Any, *, default: int = 30) -> int:
    """Clamp every executable agent-loop budget to the server hard ceiling."""

    return _as_int(
        value,
        default,
        minimum=1,
        maximum=MAX_TOOL_CALL_ITERATIONS,
    )


def normalize_policy_profile(value: Any, *, default: str = REVIEW) -> str:
    text = str(value or "").strip().lower().replace("-", "_")
    if text in {"full", "fullaccess", "full_access", "full_access_mode"}:
        return FULL_ACCESS
    if text in {"review", "ask", "ask_first", "safe"}:
        return REVIEW
    return default if default in VALID_POLICY_PROFILES else REVIEW


def normalize_context_stewardship_mode(
    value: Any, *, default: str = CONTEXT_ASSISTED
) -> str:
    text = str(value or "").strip().lower().replace("-", "_")
    if text in {"off", "none", "disabled", "manual", "user"}:
        return CONTEXT_MANUAL
    if text in {"auto", "assisted", "default", "balanced"}:
        return CONTEXT_ASSISTED
    if text in {"aggressive", "compact", "compact_refs", "minimal"}:
        return CONTEXT_AGGRESSIVE
    return default if default in VALID_CONTEXT_STEWARDSHIP_MODES else CONTEXT_ASSISTED


def normalize_runtime_policy_override(value: Any) -> dict[str, Any]:
    payload = value if isinstance(value, Mapping) else {}
    if not payload:
        return {}

    profile_value = (
        payload.get("profile")
        or payload.get("approval_profile")
        or payload.get("mode")
        or payload.get("policy_profile")
    )
    result: dict[str, Any] = {"schema_version": RUNTIME_POLICY_SCHEMA_VERSION}
    # A partial override (for example context stewardship) must not silently
    # manufacture a Review profile and defeat the administrator's Full Access
    # default. Only an explicitly supplied profile changes approvals.
    if profile_value is not None and str(profile_value).strip():
        result["profile"] = normalize_policy_profile(profile_value)
    if "disable_tool_approvals" in payload:
        result["disable_tool_approvals"] = _as_bool(
            payload.get("disable_tool_approvals")
        )
    if "max_tool_calls" in payload:
        result["max_tool_calls"] = clamp_tool_call_iterations(
            payload.get("max_tool_calls"),
        )
    if "chat_tool_budget_extension_max" in payload:
        result["chat_tool_budget_extension_max"] = _as_int(
            payload.get("chat_tool_budget_extension_max"),
            4,
            minimum=0,
            maximum=12,
        )
    if "context_stewardship" in payload or "context_stewardship_mode" in payload:
        result["context_stewardship"] = normalize_context_stewardship_mode(
            payload.get("context_stewardship")
            or payload.get("context_stewardship_mode")
        )
    return result


def resolve_runtime_policy(
    settings: Mapping[str, Any] | None = None,
    override: Any = None,
) -> dict[str, Any]:
    settings = settings if isinstance(settings, Mapping) else {}
    normalized_override = normalize_runtime_policy_override(override)

    settings_disable_approvals = _as_bool(settings.get("disable_tool_approvals"), False)
    default_profile = FULL_ACCESS if settings_disable_approvals else REVIEW
    profile = normalize_policy_profile(
        normalized_override.get("profile"), default=default_profile
    )
    disable_tool_approvals = normalized_override.get(
        "disable_tool_approvals",
        profile == FULL_ACCESS,
    )
    max_tool_calls = (
        normalized_override["max_tool_calls"]
        if "max_tool_calls" in normalized_override
        else clamp_tool_call_iterations(settings.get("max_tool_calls"))
    )
    extension_max = (
        normalized_override["chat_tool_budget_extension_max"]
        if "chat_tool_budget_extension_max" in normalized_override
        else _as_int(
            settings.get("chat_tool_budget_extension_max"), 4, minimum=0, maximum=12
        )
    )
    context_stewardship = (
        normalized_override["context_stewardship"]
        if "context_stewardship" in normalized_override
        else normalize_context_stewardship_mode(
            settings.get("context_stewardship"), default=CONTEXT_ASSISTED
        )
    )

    return {
        "schema_version": RUNTIME_POLICY_SCHEMA_VERSION,
        "profile": profile,
        "approval_profile": FULL_ACCESS if disable_tool_approvals else REVIEW,
        "disable_tool_approvals": bool(disable_tool_approvals),
        "max_tool_calls": max_tool_calls,
        "chat_tool_budget_extension_max": extension_max,
        "context_stewardship": context_stewardship,
        "override_active": bool(normalized_override),
        "override": normalized_override,
    }


def apply_runtime_policy_to_settings(
    settings: dict[str, Any] | None = None, override: Any = None
) -> tuple[dict[str, str], dict[str, Any]]:
    effective = dict(settings or {})
    policy = resolve_runtime_policy(effective, override)
    effective["disable_tool_approvals"] = (
        "true" if policy["disable_tool_approvals"] else "false"
    )
    effective["max_tool_calls"] = str(policy["max_tool_calls"])
    effective["chat_tool_budget_extension_max"] = str(
        policy["chat_tool_budget_extension_max"]
    )
    effective["runtime_policy_profile"] = policy["profile"]
    effective["context_stewardship"] = policy["context_stewardship"]
    return effective, policy


def runtime_policy_summary(policy: dict[str, Any] | None) -> str:
    policy = policy if isinstance(policy, dict) else resolve_runtime_policy({})
    extension_max = policy.get("chat_tool_budget_extension_max", 0)
    extension_text = (
        f"one granted request adds up to {extension_max} tool-call iterations"
        if extension_max
        else "budget extensions disabled"
    )
    return (
        f"profile={policy.get('profile')}; "
        f"approval_profile={policy.get('approval_profile')}; "
        f"max_tool_calls={policy.get('max_tool_calls')}; "
        f"context_stewardship={policy.get('context_stewardship')}; "
        "extension_policy=at most one request_tool_budget_extension call per turn, "
        f"{extension_text}"
    )


__all__ = [
    "CONTEXT_AGGRESSIVE",
    "CONTEXT_ASSISTED",
    "CONTEXT_MANUAL",
    "FULL_ACCESS",
    "MAX_TOOL_CALL_ITERATIONS",
    "REVIEW",
    "RUNTIME_POLICY_SCHEMA_VERSION",
    "VALID_CONTEXT_STEWARDSHIP_MODES",
    "VALID_POLICY_PROFILES",
    "apply_runtime_policy_to_settings",
    "clamp_tool_call_iterations",
    "normalize_context_stewardship_mode",
    "normalize_policy_profile",
    "normalize_runtime_policy_override",
    "resolve_runtime_policy",
    "runtime_policy_summary",
]
