"""Internal helpers for Carlina's bounded Python compatibility surface."""

from __future__ import annotations

import sys
from importlib import import_module
from types import ModuleType


def alias_module(legacy_name: str, canonical_name: str) -> ModuleType:
    """Make one legacy import path resolve to the canonical module object.

    Re-exporting names is insufficient for stateful modules because callers can
    otherwise patch or reload two distinct module objects. The old path is
    replaced in ``sys.modules`` so both imports share globals, locks, caches,
    classes, and functions.
    """

    if not legacy_name.startswith("iarf_assistant."):
        raise ValueError("legacy module aliases must use the iarf_assistant namespace")
    if not canonical_name.startswith("carlina."):
        raise ValueError("canonical module aliases must use the carlina namespace")
    module = import_module(canonical_name)
    sys.modules[legacy_name] = module
    return module


__all__ = ["alias_module"]
