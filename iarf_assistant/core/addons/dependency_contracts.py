"""Normalization helpers for legacy and Add-on API dependency declarations."""

from __future__ import annotations


def _dependency_values(value: object) -> list[object]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, (set, frozenset)):
        return sorted(value, key=str)
    if isinstance(value, (list, tuple)):
        return list(value)
    return []


def extract_dependency_addon_ids(dependencies: object) -> list[str]:
    """Return unique owning add-on IDs in declaration order."""

    if not dependencies:
        return []
    if isinstance(dependencies, (list, tuple, set, frozenset)):
        candidates = _dependency_values(dependencies)
    elif isinstance(dependencies, dict):
        candidates = []
        for key in ("plugins", "addons"):
            candidates.extend(_dependency_values(dependencies.get(key)))
        for key in ("helpers", "ui_components"):
            candidates.extend(
                str(component_id).split(".", 1)[0]
                for component_id in _dependency_values(dependencies.get(key))
                if component_id
            )
    else:
        return []

    return list(dict.fromkeys(str(candidate) for candidate in candidates if candidate))


__all__ = ["extract_dependency_addon_ids"]
