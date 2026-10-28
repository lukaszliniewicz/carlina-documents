"""Compatibility alias for :mod:`carlina.core.tool_risk`."""

from carlina._compat import alias_module as _alias_module
from carlina.core.tool_risk import (
    HIGH_RISK_EFFECTS,
    READ_ONLY_EFFECTS,
    WRITE_EFFECTS,
    classify_tool_risk,
    normalized_tool_effects,
    tool_is_read_only,
)

__all__ = [
    "HIGH_RISK_EFFECTS",
    "READ_ONLY_EFFECTS",
    "WRITE_EFFECTS",
    "classify_tool_risk",
    "normalized_tool_effects",
    "tool_is_read_only",
]

_alias_module(__name__, "carlina.core.tool_risk")
