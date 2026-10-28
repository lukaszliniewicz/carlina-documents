"""Collision-resistant storage identifiers for add-on-owned resources."""

from __future__ import annotations

import hashlib
import re


def addon_storage_key(addon_id: str) -> str:
    """Return a readable key with a digest that prevents slug collisions."""

    normalized = re.sub(r"[^A-Za-z0-9]+", "_", str(addon_id)).strip("_").lower()
    digest = hashlib.sha256(str(addon_id).encode("utf-8")).hexdigest()[:10]
    return f"{normalized or 'addon'}_{digest}"


def legacy_addon_storage_key(addon_id: str) -> str:
    return str(addon_id).replace("-", "_")


def escape_sql_like(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


__all__ = ["addon_storage_key", "escape_sql_like", "legacy_addon_storage_key"]
