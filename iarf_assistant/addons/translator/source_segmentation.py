"""Strict, source-preserving sentence segmentation adapters.

The planner stores sentence boundaries as character offsets rather than source
text.  This keeps provider output out of the plain planner settings while
making a callback-produced plan exactly replayable during canonical
validation.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from .block_contract import (
    MAX_SOURCE_SENTENCE_SEGMENTS,
    BlockContractError,
    normalize_source_sentence_manifest,
)

SOURCE_SENTENCE_MANIFEST_KEY = "source_sentence_manifest"
SOURCE_SENTENCE_PROFILE = "sentence-segmenter"
SOURCE_SENTENCE_VERSION = "text.segmentation-v1"
type SentenceSegmenter = Callable[[str], Sequence[str]]


def _segmentation_error(suffix: str) -> BlockContractError:
    return BlockContractError(f"source-sentence-segmentation-{suffix}")


def segment_projection(
    projection: str,
    segmenter: SentenceSegmenter,
) -> tuple[int, ...]:
    """Call a strict segmenter and return full-coverage end offsets.

    Leading, inter-sentence, and trailing whitespace is retained in the
    surrounding anchor spans.  A whitespace-only projection has one valid span
    and is never sent to a provider segmenter.
    """

    if not isinstance(projection, str) or not callable(segmenter):
        raise _segmentation_error("invalid")
    if not projection.strip():
        return (len(projection),)
    try:
        raw_segments = segmenter(projection)
    except Exception as exc:
        raise _segmentation_error("failed") from exc
    if isinstance(raw_segments, (str, bytes, bytearray)) or not isinstance(
        raw_segments, Sequence
    ):
        raise _segmentation_error("invalid")
    try:
        count = len(raw_segments)
    except Exception as exc:
        raise _segmentation_error("invalid") from exc
    if not (1 <= count <= MAX_SOURCE_SENTENCE_SEGMENTS):
        raise _segmentation_error("invalid")
    try:
        segments = list(raw_segments)
    except Exception as exc:
        raise _segmentation_error("invalid") from exc
    if not (1 <= len(segments) <= MAX_SOURCE_SENTENCE_SEGMENTS):
        raise _segmentation_error("invalid")

    cursor = 0
    text_starts: list[int] = []
    for segment in segments:
        if not isinstance(segment, str):
            raise _segmentation_error("invalid")
        text = segment.strip()
        if not text:
            raise _segmentation_error("invalid")
        while cursor < len(projection) and projection[cursor].isspace():
            cursor += 1
        start = cursor
        if not projection.startswith(text, start):
            raise _segmentation_error("coverage")
        cursor += len(text)
        text_starts.append(start)

    while cursor < len(projection) and projection[cursor].isspace():
        cursor += 1
    if cursor != len(projection):
        raise _segmentation_error("coverage")
    return (*text_starts[1:], len(projection))


def manifest_spans(projection: str, ends: Sequence[int]) -> tuple[tuple[int, int], ...]:
    """Validate manifest ends against one exact projection and return spans."""

    if not isinstance(projection, str) or isinstance(ends, (str, bytes, bytearray)):
        raise _segmentation_error("manifest")
    try:
        values = tuple(ends)
    except Exception as exc:
        raise _segmentation_error("manifest") from exc
    if not (1 <= len(values) <= MAX_SOURCE_SENTENCE_SEGMENTS):
        raise _segmentation_error("manifest")
    spans: list[tuple[int, int]] = []
    start = 0
    all_whitespace = not projection.strip()
    if all_whitespace and len(values) != 1:
        raise _segmentation_error("manifest")
    for end in values:
        if isinstance(end, bool) or not isinstance(end, int) or end <= start:
            raise _segmentation_error("manifest")
        if end > len(projection):
            raise _segmentation_error("manifest")
        if not all_whitespace and not projection[start:end].strip():
            raise _segmentation_error("manifest")
        spans.append((start, end))
        start = end
    if start != len(projection):
        raise _segmentation_error("manifest")
    return tuple(spans)


__all__ = [
    "MAX_SOURCE_SENTENCE_SEGMENTS",
    "SOURCE_SENTENCE_MANIFEST_KEY",
    "SOURCE_SENTENCE_PROFILE",
    "SOURCE_SENTENCE_VERSION",
    "SentenceSegmenter",
    "manifest_spans",
    "normalize_source_sentence_manifest",
    "segment_projection",
]
