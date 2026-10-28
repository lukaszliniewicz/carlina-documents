"""Bounded inverse text splices for exact restoration of serialized HTML.

Unique serialization chunks are patience-diff anchors. Computing their longest
increasing subsequence is bounded O(n log n), including for repetitive books;
unanchored gaps are trimmed rather than subjected to quadratic character diff.
This is a storage delta, not an HTML parser or an editing authority.
"""

from __future__ import annotations

import hashlib
import json
import re
from bisect import bisect_left
from typing import Any

MAX_DELTA_BYTES = 256 * 1024
MAX_HISTORY_BYTES = 1024 * 1024
MAX_HISTORY_ENTRIES = 32
MAX_PREVIEW_CHARS = 16000


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _chunks(text: str) -> tuple[list[str], list[int]]:
    chunks = re.findall(r"[^>]*>|[^>]+$", text)
    offsets = [0]
    for chunk in chunks:
        offsets.append(offsets[-1] + len(chunk))
    return chunks, offsets


def inverse_splices(before: str, after: str) -> list[dict[str, Any]]:
    """Return inverse splices addressed to the exact successor string."""
    if before == after:
        return []
    old, old_offsets = _chunks(before)
    new, new_offsets = _chunks(after)
    indexes: dict[str, int | None] = {}
    for index, chunk in enumerate(old):
        indexes[chunk] = None if chunk in indexes else index
    seen: set[str] = set()
    repeated: set[str] = set()
    for chunk in new:
        if chunk in seen:
            repeated.add(chunk)
        seen.add(chunk)
    pairs = [
        (indexes[chunk], index)
        for index, chunk in enumerate(new)
        if indexes.get(chunk) is not None and chunk not in repeated
    ]
    tails: list[int] = []
    tail_pairs: list[int] = []
    previous: list[int] = []
    for index, (old_index, _new_index) in enumerate(pairs):
        assert old_index is not None
        slot = bisect_left(tails, old_index)
        previous.append(tail_pairs[slot - 1] if slot else -1)
        if slot == len(tails):
            tails.append(old_index)
            tail_pairs.append(index)
        else:
            tails[slot] = old_index
            tail_pairs[slot] = index
    anchors = []
    cursor = tail_pairs[-1] if tail_pairs else -1
    while cursor >= 0:
        anchors.append(pairs[cursor])
        cursor = previous[cursor]
    anchors.reverse()
    splices = []
    old_start = new_start = 0
    used_bytes = 0
    for old_index, new_index in [*anchors, (len(old), len(new))]:
        assert old_index is not None
        left = before[old_start : old_offsets[old_index]]
        right = after[new_start : new_offsets[new_index]]
        prefix = 0
        while prefix < min(len(left), len(right)) and left[prefix] == right[prefix]:
            prefix += 1
        suffix = 0
        while (
            suffix < min(len(left), len(right)) - prefix
            and left[len(left) - suffix - 1] == right[len(right) - suffix - 1]
        ):
            suffix += 1
        old_end, new_end = len(left) - suffix, len(right) - suffix
        if prefix != old_end or prefix != new_end:
            splice = {
                "start": new_start + prefix,
                "end": new_start + new_end,
                "old": left[prefix:old_end],
                "after_hash": _hash(right[prefix:new_end]),
            }
            used_bytes += len(json.dumps(splice, ensure_ascii=False).encode("utf-8"))
            if used_bytes > MAX_DELTA_BYTES:
                raise ValueError(
                    "Undo fragments exceed 256 KiB; split the correction into smaller batches."
                )
            splices.append(splice)
        old_start = old_offsets[min(old_index + 1, len(old))]
        new_start = new_offsets[min(new_index + 1, len(new))]
    return splices


def restore_splices(after: str, splices: list[dict[str, Any]]) -> str:
    pieces = []
    cursor = 0
    for splice in splices:
        start, end, old = splice["start"], splice["end"], splice["old"]
        if (
            type(start) is not int
            or type(end) is not int
            or not cursor <= start <= end <= len(after)
            or not isinstance(old, str)
            or _hash(after[start:end]) != splice["after_hash"]
        ):
            raise ValueError("Undo fragments do not match the current document.")
        pieces.extend((after[cursor:start], old))
        cursor = end
    pieces.append(after[cursor:])
    return "".join(pieces)


def preview_splices(after: str, splices: list[dict[str, Any]]) -> dict[str, Any]:
    """Display evidence only; preview fragments are not standalone editable HTML."""
    items = []
    remaining = MAX_PREVIEW_CHARS
    for splice in splices[:50]:
        old, new = splice["old"], after[splice["start"] : splice["end"]]
        allowance = min(1000, remaining // 4)
        if allowance == 0:
            break
        context_before = after[
            max(0, splice["start"] - min(120, allowance)) : splice["start"]
        ]
        context_after = after[splice["end"] : splice["end"] + min(120, allowance)]
        items.append(
            {
                "before": old[:allowance],
                "after": new[:allowance],
                "before_chars": len(old),
                "after_chars": len(new),
                "truncated": len(old) > allowance or len(new) > allowance,
                "context_before": context_before,
                "context_after": context_after,
            }
        )
        remaining -= (
            len(old[:allowance])
            + len(new[:allowance])
            + len(context_before)
            + len(context_after)
        )
    return {
        "fragments": items,
        "total_fragments": len(splices),
        "incomplete": len(items) < len(splices),
        "format": "serialized_html_differences_not_editable_elements",
    }
