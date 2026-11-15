"""Owner-defined house-style patterns checked by QA against the target text.

An owner lists what the house style forbids or wants looked at (a banned word,
a spelling variant, straight apostrophes) as bounded regular expressions with
a message. QA reports each match as an advisory ``style`` finding. Patterns
are validated when saved and evaluated with a per-pattern time limit, so a
pathological expression cannot stall a scan.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import cast

import regex

from .block_contract import BlockContractError, canonical_json

MAX_QA_PATTERNS = 30
MAX_QA_PATTERN_CHARS = 200
MAX_QA_PATTERN_MESSAGE_CHARS = 200
QA_PATTERN_TIMEOUT_SECONDS = 0.05


def normalize_qa_patterns(value: object) -> list[dict[str, object]] | None:
    """Return validated patterns, or ``None`` for an empty list."""

    if value is None:
        return None
    if not isinstance(value, list):
        raise BlockContractError("qa-patterns")
    items = cast(list[object], value)
    if len(items) > MAX_QA_PATTERNS:
        raise BlockContractError("qa-patterns")
    patterns: list[dict[str, object]] = []
    seen: set[tuple[str, bool]] = set()
    for item in items:
        if not isinstance(item, Mapping):
            raise BlockContractError("qa-pattern")
        mapping = cast(Mapping[str, object], item)
        if not set(mapping) <= {"pattern", "message", "ignore_case"}:
            raise BlockContractError("qa-pattern")
        pattern = mapping.get("pattern")
        message = mapping.get("message")
        ignore_case = mapping.get("ignore_case", False)
        if (
            not isinstance(pattern, str)
            or not pattern
            or len(pattern) > MAX_QA_PATTERN_CHARS
            or "\x00" in pattern
            or not isinstance(message, str)
            or not message.strip()
            or len(message.strip()) > MAX_QA_PATTERN_MESSAGE_CHARS
            or not isinstance(ignore_case, bool)
        ):
            raise BlockContractError("qa-pattern")
        try:
            compiled = compile_qa_pattern(pattern, ignore_case=ignore_case)
        except regex.error as exc:
            raise BlockContractError("qa-pattern-syntax") from exc
        # A pattern that matches nothing at all would flag every position.
        if compiled.fullmatch("") is not None:
            raise BlockContractError("qa-pattern-empty-match")
        if (pattern, ignore_case) in seen:
            continue
        seen.add((pattern, ignore_case))
        patterns.append(
            {
                "pattern": pattern,
                "message": " ".join(message.split()),
                "ignore_case": ignore_case,
            }
        )
    return patterns or None


def compile_qa_pattern(pattern: str, *, ignore_case: bool) -> regex.Pattern[str]:
    flags = regex.VERSION1 | (regex.IGNORECASE if ignore_case else 0)
    return regex.compile(pattern, flags)


def qa_patterns_fingerprint(patterns: Sequence[Mapping[str, object]]) -> str:
    """Identify a pattern list for QA caching."""

    return hashlib.sha256(canonical_json(list(patterns)).encode("utf-8")).hexdigest()


__all__ = [
    "MAX_QA_PATTERNS",
    "MAX_QA_PATTERN_CHARS",
    "MAX_QA_PATTERN_MESSAGE_CHARS",
    "QA_PATTERN_TIMEOUT_SECONDS",
    "compile_qa_pattern",
    "normalize_qa_patterns",
    "qa_patterns_fingerprint",
]
