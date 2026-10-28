from __future__ import annotations

from typing import Any

READ_ONLY_EFFECTS = frozenset({"read", "external_read", "local_read"})
HIGH_RISK_EFFECTS = frozenset(
    {"delete", "publish", "external_delete", "external_publish"}
)
WRITE_EFFECTS = frozenset({"write", "external_write", "mutate"})


def normalized_tool_effects(tool_record: Any) -> set[str]:
    return {
        str(effect).strip().lower()
        for effect in (getattr(tool_record, "side_effects", None) or [])
        if str(effect).strip()
    }


def classify_tool_risk(tool_record: Any) -> str:
    """Classify a tool without treating external reads as writes."""
    effects = normalized_tool_effects(tool_record)
    if (
        bool(getattr(tool_record, "approval_required", False))
        or effects & HIGH_RISK_EFFECTS
    ):
        return "high"
    if effects - READ_ONLY_EFFECTS or effects & WRITE_EFFECTS:
        return "write"
    return "read"


def tool_is_read_only(tool_record: Any) -> bool:
    return classify_tool_risk(tool_record) == "read"


__all__ = [
    "HIGH_RISK_EFFECTS",
    "READ_ONLY_EFFECTS",
    "WRITE_EFFECTS",
    "classify_tool_risk",
    "normalized_tool_effects",
    "tool_is_read_only",
]
