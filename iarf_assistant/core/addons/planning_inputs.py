"""Bounded, side-effect-free normalization for runtime planning inputs."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from itertools import islice
from typing import Any

DEFAULT_RAW_ITEM_SCAN_LIMIT = 80
_TRUNCATION_MARKER = "... [truncated]"


@dataclass(frozen=True, slots=True)
class NormalizedPlanningIdentifiers:
    items: tuple[tuple[str, str], ...]
    truncated: bool
    ignored_count: int


def bounded_planning_text(value: Any, limit: int) -> str:
    """Normalize only a bounded prefix and never stringify arbitrary objects."""

    if not isinstance(value, str) or limit <= 0:
        return ""
    scan_limit = max(limit + len(_TRUNCATION_MARKER), limit * 4)
    fragment = value[:scan_limit]
    normalized = " ".join(fragment.split())
    truncated = len(value) > scan_limit or len(normalized) > limit
    if not truncated:
        return normalized
    if limit <= len(_TRUNCATION_MARKER):
        return _TRUNCATION_MARKER[:limit]
    prefix = normalized[: limit - len(_TRUNCATION_MARKER)].rstrip()
    return prefix + _TRUNCATION_MARKER


def normalize_planning_identifiers(
    value: Any,
    *,
    max_items: int,
    max_length: int,
    canonicalize: Callable[[str], str] | None = None,
    raw_item_scan_limit: int = DEFAULT_RAW_ITEM_SCAN_LIMIT,
) -> NormalizedPlanningIdentifiers:
    """Validate and canonicalize a fixed amount of untrusted iterable input."""

    if isinstance(value, str):
        values: Iterable[Any] = (value,)
    elif isinstance(value, Iterable) and not isinstance(
        value,
        (bytes, bytearray, Mapping),
    ):
        values = value
    else:
        values = ()

    scan_limit = max(max_items, raw_item_scan_limit)
    sampled = list(islice(values, scan_limit + 1))
    raw_overflow = len(sampled) > scan_limit
    if raw_overflow:
        sampled = sampled[:scan_limit]

    normalized: list[tuple[str, str]] = []
    seen: set[str] = set()
    ignored_count = 0
    truncated = raw_overflow
    for item in sampled:
        if not isinstance(item, str):
            ignored_count += 1
            continue
        raw = item.strip()
        if not raw or len(raw) > max_length:
            ignored_count += 1
            continue
        canonical = canonicalize(raw) if canonicalize else raw
        if not canonical or canonical in seen:
            continue
        seen.add(canonical)
        if len(normalized) >= max_items:
            truncated = True
            continue
        normalized.append((raw, canonical))

    return NormalizedPlanningIdentifiers(
        items=tuple(normalized),
        truncated=truncated,
        ignored_count=ignored_count,
    )


def planning_int(
    value: Any,
    *,
    default: int,
    minimum: int = 1,
    maximum: int,
) -> int:
    """Clamp an integer while treating booleans as malformed input."""

    if isinstance(value, bool):
        return default
    try:
        return max(minimum, min(int(value), maximum))
    except (TypeError, ValueError):
        return default


__all__ = [
    "DEFAULT_RAW_ITEM_SCAN_LIMIT",
    "NormalizedPlanningIdentifiers",
    "bounded_planning_text",
    "normalize_planning_identifiers",
    "planning_int",
]
