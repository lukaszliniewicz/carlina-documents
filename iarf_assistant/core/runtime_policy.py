"""Compatibility alias for :mod:`carlina.core.runtime_policy`."""

from carlina._compat import alias_module as _alias_module
from carlina.core.runtime_policy import (
    CONTEXT_AGGRESSIVE,
    CONTEXT_ASSISTED,
    CONTEXT_MANUAL,
    FULL_ACCESS,
    MAX_TOOL_CALL_ITERATIONS,
    REVIEW,
    RUNTIME_POLICY_SCHEMA_VERSION,
    VALID_CONTEXT_STEWARDSHIP_MODES,
    VALID_POLICY_PROFILES,
    apply_runtime_policy_to_settings,
    normalize_context_stewardship_mode,
    normalize_policy_profile,
    normalize_runtime_policy_override,
    resolve_runtime_policy,
    runtime_policy_summary,
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
    "normalize_context_stewardship_mode",
    "normalize_policy_profile",
    "normalize_runtime_policy_override",
    "resolve_runtime_policy",
    "runtime_policy_summary",
]

_alias_module(__name__, "carlina.core.runtime_policy")
