"""Request-scoped caches for repeated immutable Translator verification."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class TranslationRehydrationCache:
    """Memoize already verified immutable rows within one trusted call graph.

    Keys include the source-plan identifier so a cache cannot accidentally
    satisfy verification for a different owner or source snapshot.
    """

    target_candidates: dict[tuple[str, str], Any] = field(default_factory=dict)
    target_span_sets: dict[tuple[str, str], Any] = field(default_factory=dict)
    alignments: dict[tuple[str, str], Any] = field(default_factory=dict)


__all__ = ["TranslationRehydrationCache"]
