"""Versioned identity for deterministic Translator analysis scope planning."""

from __future__ import annotations

ANALYSIS_SCOPE_PLANNER_CONTRACT_VERSION = "document-block-analysis-scope-planner-v2"
SUPPORTED_ANALYSIS_SCOPE_PLANNER_CONTRACTS = frozenset(
    {ANALYSIS_SCOPE_PLANNER_CONTRACT_VERSION}
)

__all__ = [
    "ANALYSIS_SCOPE_PLANNER_CONTRACT_VERSION",
    "SUPPORTED_ANALYSIS_SCOPE_PLANNER_CONTRACTS",
]
