"""Canonical product and token-scope projection for MCP runtime profiles."""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from carlina.runtime_composition import RuntimeComposition

_MCP_PRODUCTS: tuple[tuple[str, str], ...] = (
    ("doc_processor", "mcp.doc_processor"),
    ("translator", "mcp.translator"),
    ("news_pipeline", "mcp.news_pipeline"),
)


def selected_mcp_products(
    composition: RuntimeComposition,
) -> tuple[tuple[str, str], ...]:
    """Return MCP products selected by the application's fixed composition."""

    if composition.addon_ids is None:
        return _MCP_PRODUCTS
    return tuple(
        (addon_id, scope)
        for addon_id, scope in _MCP_PRODUCTS
        if addon_id in composition.addon_ids
    )


def default_mcp_scopes(composition: RuntimeComposition) -> tuple[str, ...]:
    """Return the profile's ordered default MCP authorization scopes."""

    return (
        "mcp.access",
        *(scope for _addon_id, scope in selected_mcp_products(composition)),
        "mcp.work",
    )


def effective_mcp_scopes(
    granted: Iterable[str], composition: RuntimeComposition
) -> frozenset[str]:
    """Project stored grants through the selected profile's authority boundary."""

    scopes = frozenset(granted)
    if composition.addon_ids is None:
        return scopes
    return scopes.intersection(default_mcp_scopes(composition))


__all__ = [
    "default_mcp_scopes",
    "effective_mcp_scopes",
    "selected_mcp_products",
]
