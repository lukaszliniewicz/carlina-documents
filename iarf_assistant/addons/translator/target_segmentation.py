"""Deterministic, versioned target-projection sentence splitting."""

from __future__ import annotations

from .alignment_contract import TARGET_SPLITTER_VERSION, TargetSpan
from .block_contract import sha256_text

_ABBREVIATIONS = frozenset({"mr", "mrs", "ms", "dr", "prof", "sr", "jr", "etc", "e.g", "i.e", "vs", "np", "itd", "m.in", "tj", "r"})
_CLOSING_MARKS = "\"'”’»）)]】」』"
_CLOSING_QUOTE_MARKS = "\"'”’»"
_QUOTED_CONTINUATION_MARKS = ",:;–—"


def target_split_profile(language: str) -> tuple[str, str]:
    normalized = language.lower().split("-", 1)[0]
    if normalized == "pl":
        return "target-polish", TARGET_SPLITTER_VERSION
    if normalized in {"zh", "ja", "ko"}:
        return "target-cjk", TARGET_SPLITTER_VERSION
    if normalized in {"ar", "fa", "ur"}:
        return "target-arabic", TARGET_SPLITTER_VERSION
    if normalized == "en":
        return "target-english", TARGET_SPLITTER_VERSION
    return "target-unknown", TARGET_SPLITTER_VERSION


def _is_abbreviation(text: str, period_at: int, profile: str) -> bool:
    if profile not in {"target-english", "target-polish"}:
        return False
    prefix = text[:period_at].rstrip()
    if not prefix:
        return False
    word = prefix.rsplit(None, 1)[-1].lower()
    return word in _ABBREVIATIONS or (len(word) == 1 and word.isalpha())


def _quoted_continuation(
    projection: str,
    *,
    terminal_index: int,
    after_closing_marks: int,
    profile: str,
) -> bool:
    if profile not in {"target-english", "target-polish"}:
        return False
    if (
        after_closing_marks == terminal_index + 1
        or projection[terminal_index + 1] not in _CLOSING_QUOTE_MARKS
    ):
        return False
    cursor = after_closing_marks
    while cursor < len(projection) and projection[cursor].isspace():
        cursor += 1
    if cursor >= len(projection):
        return False
    if projection[cursor] in _QUOTED_CONTINUATION_MARKS:
        cursor += 1
        while cursor < len(projection) and projection[cursor].isspace():
            cursor += 1
        return cursor < len(projection) and bool(projection[cursor:].strip())
    return projection[cursor].islower()


def _boundary_offsets(
    projection: str,
    profile: str,
    *,
    allow_quoted_continuation: bool = False,
) -> list[int]:
    endings = "。！？" if profile == "target-cjk" else ".!?؟"
    boundaries: list[int] = []
    for index, char in enumerate(projection):
        if char not in endings:
            continue
        if (
            char == "."
            and 0 < index < len(projection) - 1
            and projection[index - 1].isdigit()
            and projection[index + 1].isdigit()
        ):
            continue
        # Terminal runs such as ``?!``, ``...)`` and ``...`` are one boundary.
        # Splitting between punctuation marks or before a closing bracket
        # creates a meaningless zero-context span.
        lookahead = index + 1
        while lookahead < len(projection) and projection[lookahead] in _CLOSING_MARKS:
            lookahead += 1
        if lookahead < len(projection) and projection[lookahead] in endings:
            continue
        if allow_quoted_continuation and _quoted_continuation(
            projection,
            terminal_index=index,
            after_closing_marks=lookahead,
            profile=profile,
        ):
            continue
        if char == "." and _is_abbreviation(projection, index, profile):
            continue
        end = index + 1
        # Closing quotes/brackets belong to the terminating sentence, rather
        # than becoming a detached next span.  These are ordinary projection
        # characters, so retaining them changes neither text nor offsets.
        while end < len(projection) and projection[end] in _CLOSING_MARKS:
            end += 1
        # Whitespace belongs to the preceding unit.  This keeps all offsets
        # contiguous without trimming/reconstruction and makes a rerun exact.
        while end < len(projection) and projection[end].isspace():
            end += 1
        boundaries.append(end)
    return boundaries


def split_target_projection(projection: str, language: str) -> tuple[TargetSpan, ...]:
    """Partition a target projection into exact, ordered offset ranges."""

    profile, _version = target_split_profile(language)
    if not projection:
        return ()
    offsets = _boundary_offsets(projection, profile)
    if not offsets or offsets[-1] != len(projection):
        offsets.append(len(projection))
    cursor = 0
    spans: list[TargetSpan] = []
    for offset in offsets:
        if offset <= cursor:
            continue
        value = projection[cursor:offset]
        spans.append(TargetSpan(id=f"span-{len(spans) + 1}", order=len(spans) + 1, start=cursor, end=offset, text_hash=sha256_text(value)))
        cursor = offset
    return tuple(spans)


def is_atomic_target_sentence(projection: str, language: str) -> bool:
    """Validate a v7 sentence while allowing quoted-speech continuations.

    This deliberately leaves the versioned deterministic splitter unchanged:
    historical span receipts must retain their original boundaries and hashes.
    """

    if not projection or not projection.strip():
        return False
    profile, _version = target_split_profile(language)
    offsets = _boundary_offsets(
        projection,
        profile,
        allow_quoted_continuation=True,
    )
    return not any(0 < offset < len(projection) for offset in offsets)


__all__ = [
    "is_atomic_target_sentence",
    "split_target_projection",
    "target_split_profile",
]
