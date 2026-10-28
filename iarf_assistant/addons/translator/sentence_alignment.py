"""Pure provider-manifest sentence alignment contracts.

The provider-facing manifest intentionally contains only packet-local ordinal
references.  This module turns that untrusted shape into exact target offsets
and a monotonic source/target partition after the candidate itself has passed
the ordinary target validators.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from difflib import Match, SequenceMatcher
from typing import Any

from .alignment_contract import (
    AlignmentGroup,
    TargetSpan,
    target_alignment_projection,
    target_alignment_scope,
)
from .block_contract import BlockContractError, TranslationBlock, sha256_text
from .target_contract import (
    TargetBlockCandidate,
    TargetLexicalAttribute,
    TargetTextPart,
    literal_reference_slots,
)

MAX_ALIGNMENT_SENTENCES = 4096
MAX_ALIGNMENT_GROUPS = 4096
_SENTENCE_DELIMITERS = frozenset(".!?…。！？׃\r\n")
_MAX_REBASE_CHARACTERS = 32_768
_MAX_REBASE_MATCH_PAIRS = 2_000_000


class _RebaseWorkLimit(Exception):
    """The conservative edit comparison exhausted its work allowance."""


class _BoundedRebaseMatcher(SequenceMatcher):
    def __init__(self, previous: str, current: str) -> None:
        super().__init__(None, previous, current, autojunk=False)
        self._previous = previous
        self._current = current
        self._target_counts = Counter(current)
        self._remaining_work = _MAX_REBASE_MATCH_PAIRS

    def find_longest_match(self, alo=0, ahi=None, blo=0, bhi=None) -> Match:
        # SequenceMatcher recursively searches smaller regions. Charge every
        # search, including the whole b2j lists it scans outside that region,
        # rather than assuming the first search bounds the complete diff.
        ahi = len(self._previous) if ahi is None else ahi
        bhi = len(self._current) if bhi is None else bhi
        self._remaining_work -= 2 * (ahi - alo + bhi - blo)
        for index in range(alo, ahi):
            self._remaining_work -= 1 + self._target_counts[self._previous[index]]
            if self._remaining_work < 0:
                raise _RebaseWorkLimit
        return super().find_longest_match(alo, ahi, blo, bhi)


@dataclass(frozen=True, slots=True)
class TargetSentenceSpan:
    """One exact offset-bearing target sentence partition member."""

    order: int
    start: int
    end: int
    text: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.order, int)
            or isinstance(self.order, bool)
            or self.order < 1
            or not isinstance(self.start, int)
            or isinstance(self.start, bool)
            or self.start < 0
            or not isinstance(self.end, int)
            or isinstance(self.end, bool)
            or self.end <= self.start
            or not isinstance(self.text, str)
            or not self.text
        ):
            raise BlockContractError("sentence-alignment-target-span")

    def to_dict(self) -> dict[str, object]:
        return {
            "n": self.order,
            "start": self.start,
            "end": self.end,
            "text": self.text,
        }


@dataclass(frozen=True, slots=True)
class SentenceAlignmentGroup:
    """One packet-local monotonic source-to-target ordinal group."""

    source_ordinals: tuple[int, ...]
    target_ordinals: tuple[int, ...]

    def __post_init__(self) -> None:
        source = tuple(self.source_ordinals)
        target = tuple(self.target_ordinals)
        if (
            not source
            or not target
            or any(
                not isinstance(item, int) or isinstance(item, bool) or item < 1
                for item in (*source, *target)
            )
            or len(set(source)) != len(source)
            or len(set(target)) != len(target)
        ):
            raise BlockContractError("sentence-alignment-group")

    def to_dict(self) -> dict[str, list[int]]:
        return {
            "source": list(self.source_ordinals),
            "target": list(self.target_ordinals),
        }


@dataclass(frozen=True, slots=True)
class SentenceAlignmentManifest:
    """Verified v7 manifest with a complete target offset partition."""

    block_id: str
    target_sentences: tuple[str, ...]
    target_spans: tuple[TargetSentenceSpan, ...]
    groups: tuple[SentenceAlignmentGroup, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.block_id, str) or not self.block_id:
            raise BlockContractError("sentence-alignment-block")
        if (
            not self.target_sentences
            or len(self.target_sentences) > MAX_ALIGNMENT_SENTENCES
            or len(self.target_sentences) != len(self.target_spans)
            or not all(
                isinstance(item, str) and item.strip() for item in self.target_sentences
            )
            or not self.groups
            or len(self.groups) > MAX_ALIGNMENT_GROUPS
        ):
            raise BlockContractError("sentence-alignment-manifest")
        if tuple(span.order for span in self.target_spans) != tuple(
            range(1, len(self.target_spans) + 1)
        ):
            raise BlockContractError("sentence-alignment-target-order")

    def to_dict(self) -> dict[str, object]:
        return {
            "block_id": self.block_id,
            "target_sentences": list(self.target_sentences),
            "target_spans": [span.to_dict() for span in self.target_spans],
            "groups": [group.to_dict() for group in self.groups],
        }


def _target_partition(
    projection: str,
    reported: Sequence[object],
    *,
    target_language: str,
) -> tuple[tuple[str, ...], tuple[TargetSentenceSpan, ...]]:
    if (
        not projection
        or not reported
        or len(reported) > MAX_ALIGNMENT_SENTENCES
        or not isinstance(target_language, str)
        or not target_language.strip()
    ):
        raise BlockContractError("sentence-alignment-target-sentences")
    sentences: list[str] = []
    stripped: list[str] = []
    for item in reported:
        if not isinstance(item, str) or not item.strip():
            raise BlockContractError("sentence-alignment-target-sentence")
        value = item.strip()
        # The executor owns linguistic segmentation. A punctuation heuristic
        # cannot distinguish initials, URLs, note labels or quoted asides from
        # sentence boundaries. Validate exact coverage below, not grammar.
        sentences.append(value)
        stripped.append(value)

    # Find each exact sentence in order.  Only whitespace may occur between
    # reported sentences; choosing among two whitespace-separated occurrences
    # would make the model's partition ambiguous, so reject it.
    def whitespace_only(value: str) -> bool:
        return not value or value.isspace()

    raw_ranges: list[tuple[int, int]] = []
    cursor = 0
    for value in stripped:
        matches: list[int] = []
        search = cursor
        while True:
            found = projection.find(value, search)
            if found < 0:
                break
            if whitespace_only(projection[cursor:found]):
                matches.append(found)
            search = found + 1
        if len(matches) != 1:
            raise BlockContractError(
                "sentence-alignment-target-ambiguity"
                if len(matches) > 1
                else "sentence-alignment-target-mismatch"
            )
        start = matches[0]
        end = start + len(value)
        raw_ranges.append((start, end))
        cursor = end
    if not whitespace_only(projection[cursor:]):
        raise BlockContractError("sentence-alignment-target-coverage")

    # Boundary whitespace is assigned deterministically: leading whitespace to
    # the first sentence, inter-sentence whitespace to the preceding sentence,
    # and trailing whitespace to the last sentence.
    spans = tuple(
        TargetSentenceSpan(
            order=index,
            start=0 if index == 1 else raw_ranges[index - 1][0],
            end=len(projection) if index == len(raw_ranges) else raw_ranges[index][0],
            text=projection[
                0 if index == 1 else raw_ranges[index - 1][0] : len(projection)
                if index == len(raw_ranges)
                else raw_ranges[index][0]
            ],
        )
        for index in range(1, len(raw_ranges) + 1)
    )
    return tuple(sentences), spans


def _manifest_groups(
    value: object, *, source_count: int, target_count: int
) -> tuple[SentenceAlignmentGroup, ...]:
    if not isinstance(value, list) or not value or len(value) > MAX_ALIGNMENT_GROUPS:
        raise BlockContractError("sentence-alignment-groups")
    groups: list[SentenceAlignmentGroup] = []
    source_cursor = 1
    target_cursor = 1
    for item in value:
        if not isinstance(item, Mapping) or set(item) != {"source", "target"}:
            raise BlockContractError("sentence-alignment-group-shape")
        source = item["source"]
        target = item["target"]
        if not isinstance(source, list) or not isinstance(target, list):
            raise BlockContractError("sentence-alignment-group-shape")
        group = SentenceAlignmentGroup(tuple(source), tuple(target))
        if group.source_ordinals != tuple(
            range(source_cursor, source_cursor + len(group.source_ordinals))
        ) or group.target_ordinals != tuple(
            range(target_cursor, target_cursor + len(group.target_ordinals))
        ):
            raise BlockContractError("sentence-alignment-group-order")
        source_cursor += len(group.source_ordinals)
        target_cursor += len(group.target_ordinals)
        groups.append(group)
    if source_cursor != source_count + 1 or target_cursor != target_count + 1:
        raise BlockContractError("sentence-alignment-group-coverage")
    return tuple(groups)


def normalize_marked_sentence_alignment(
    *,
    block: TranslationBlock,
    candidate: TargetBlockCandidate,
    value: Mapping[str, Any],
    sentence_ends: Sequence[int],
    target_language: str,
) -> SentenceAlignmentManifest:
    """Derive the internal manifest from a single-copy v8/v9 translation."""
    if not isinstance(value, Mapping) or set(value) != {"groups"}:
        raise BlockContractError("sentence-alignment-shape")
    if not sentence_ends or len(sentence_ends) > MAX_ALIGNMENT_SENTENCES:
        raise BlockContractError("sentence-marker-missing")
    projection = target_alignment_projection(block, candidate)
    reported: list[str] = []
    cursor = 0
    for end in sentence_ends:
        if (
            isinstance(end, bool)
            or not isinstance(end, int)
            or not cursor < end <= len(projection)
        ):
            raise BlockContractError("sentence-marker-empty")
        sentence = projection[cursor:end].strip()
        if not sentence:
            raise BlockContractError("sentence-marker-empty")
        reported.append(sentence)
        cursor = end
    if projection[cursor:].strip():
        raise BlockContractError("sentence-marker-final")
    return normalize_sentence_alignment(
        block=block,
        candidate=candidate,
        value={"target_sentences": reported, "groups": value["groups"]},
        target_language=target_language,
    )


def normalize_sentence_alignment(
    *,
    block: TranslationBlock,
    candidate: TargetBlockCandidate,
    value: Mapping[str, Any],
    target_language: str,
) -> SentenceAlignmentManifest:
    """Validate a v7 model manifest against the normalized target projection."""

    if not isinstance(value, Mapping) or set(value) != {
        "target_sentences",
        "groups",
    }:
        raise BlockContractError("sentence-alignment-shape")
    reported = value["target_sentences"]
    if not isinstance(reported, list):
        raise BlockContractError("sentence-alignment-target-sentences")
    projection = target_alignment_projection(block, candidate)
    target_sentences, target_spans = _target_partition(
        projection,
        reported,
        target_language=target_language,
    )
    groups = _manifest_groups(
        value["groups"],
        source_count=len(block.anchors),
        target_count=len(target_spans),
    )
    return SentenceAlignmentManifest(
        block_id=block.id,
        target_sentences=target_sentences,
        target_spans=target_spans,
        groups=groups,
    )


def _rebase_old_partition(
    *,
    block: TranslationBlock,
    previous_projection: str,
    previous_spans: Sequence[TargetSpan],
    previous_groups: Sequence[AlignmentGroup],
) -> tuple[tuple[TargetSpan, ...], tuple[AlignmentGroup, ...], tuple[str, ...]]:
    """Validate and ordinalize the saved span/group partition."""

    if not isinstance(previous_projection, str) or not previous_projection:
        raise BlockContractError("sentence-alignment-rebase-projection")
    if isinstance(previous_spans, (str, bytes)):
        raise BlockContractError("sentence-alignment-rebase-spans")
    try:
        spans = tuple(previous_spans)
    except TypeError as exc:
        raise BlockContractError("sentence-alignment-rebase-spans") from exc
    if not spans or len(spans) > MAX_ALIGNMENT_SENTENCES:
        raise BlockContractError("sentence-alignment-rebase-spans")

    cursor = 0
    spans_by_id: dict[str, int] = {}
    for expected_order, span in enumerate(spans, start=1):
        if not isinstance(span, TargetSpan):
            raise BlockContractError("sentence-alignment-rebase-span")
        if (
            not isinstance(span.id, str)
            or not span.id
            or span.id in spans_by_id
            or isinstance(span.order, bool)
            or not isinstance(span.order, int)
            or isinstance(span.start, bool)
            or not isinstance(span.start, int)
            or isinstance(span.end, bool)
            or not isinstance(span.end, int)
            or span.order != expected_order
            or span.start != cursor
            or span.end <= span.start
            or span.end > len(previous_projection)
            or span.text_hash != sha256_text(previous_projection[span.start : span.end])
        ):
            raise BlockContractError("sentence-alignment-rebase-span")
        spans_by_id[span.id] = expected_order
        cursor = span.end
    if cursor != len(previous_projection):
        raise BlockContractError("sentence-alignment-rebase-span-coverage")

    try:
        anchors = tuple(block.anchors)
        block_id = block.id
    except (AttributeError, TypeError) as exc:
        raise BlockContractError("sentence-alignment-rebase-block") from exc
    if not isinstance(block_id, str) or not block_id or not anchors:
        raise BlockContractError("sentence-alignment-rebase-block")
    anchor_ids: list[str] = []
    for expected_order, anchor in enumerate(anchors, start=1):
        try:
            anchor_id = anchor.id
            anchor_order = anchor.order
        except AttributeError as exc:
            raise BlockContractError("sentence-alignment-rebase-anchor") from exc
        if (
            not isinstance(anchor_id, str)
            or not anchor_id
            or anchor_id in anchor_ids
            or isinstance(anchor_order, bool)
            or not isinstance(anchor_order, int)
            or anchor_order != expected_order
        ):
            raise BlockContractError("sentence-alignment-rebase-anchor")
        anchor_ids.append(anchor_id)

    if isinstance(previous_groups, (str, bytes)):
        raise BlockContractError("sentence-alignment-rebase-groups")
    try:
        groups = tuple(previous_groups)
    except TypeError as exc:
        raise BlockContractError("sentence-alignment-rebase-groups") from exc
    if not groups:
        raise BlockContractError("sentence-alignment-rebase-groups")
    anchor_ordinals = {
        anchor_id: index for index, anchor_id in enumerate(anchor_ids, 1)
    }
    ordinal_groups: list[dict[str, list[int]]] = []
    for group in groups:
        if not isinstance(group, AlignmentGroup):
            raise BlockContractError("sentence-alignment-rebase-group")
        try:
            source_ids = tuple(group.source_anchor_ids)
            target_ids = tuple(group.target_span_ids)
        except (AttributeError, TypeError) as exc:
            raise BlockContractError("sentence-alignment-rebase-group") from exc
        if (
            not source_ids
            or not target_ids
            or any(not isinstance(item, str) or not item for item in source_ids)
            or any(not isinstance(item, str) or not item for item in target_ids)
            or len(set(source_ids)) != len(source_ids)
            or len(set(target_ids)) != len(target_ids)
            or any(item not in anchor_ordinals for item in source_ids)
            or any(item not in spans_by_id for item in target_ids)
        ):
            raise BlockContractError("sentence-alignment-rebase-group-membership")
        ordinal_groups.append(
            {
                "source": [anchor_ordinals[item] for item in source_ids],
                "target": [spans_by_id[item] for item in target_ids],
            }
        )
    _manifest_groups(
        ordinal_groups,
        source_count=len(anchor_ids),
        target_count=len(spans),
    )
    return spans, groups, tuple(anchor_ids)


def _rebase_owner(
    start: int,
    end: int,
    *,
    previous_projection_length: int,
    spans: Sequence[TargetSpan],
) -> int | None:
    """Return the only old span that can own one changed opcode."""

    if start == end:
        if start == 0:
            return 0
        if start == previous_projection_length:
            return len(spans) - 1
        for index, span in enumerate(spans):
            if span.start < start < span.end:
                return index
        return None
    for index, span in enumerate(spans):
        if span.start <= start and end <= span.end:
            return index
    return None


def rebase_sentence_alignment(
    block: TranslationBlock,
    candidate: TargetBlockCandidate,
    *,
    previous_projection: str,
    previous_spans: Sequence[TargetSpan],
    previous_groups: Sequence[AlignmentGroup],
) -> SentenceAlignmentManifest | None:
    """Carry a verified sentence partition through conservative text edits.

    The saved spans remain authoritative.  A changed opcode may only replace
    material inside one saved span, and punctuation/newline changes are too
    ambiguous to carry automatically.
    """

    if not isinstance(candidate, TargetBlockCandidate):
        raise BlockContractError("sentence-alignment-rebase-candidate")
    try:
        block_id = block.id
        candidate_block_id = candidate.block_id
    except AttributeError as exc:
        raise BlockContractError("sentence-alignment-rebase-owner") from exc
    if not isinstance(block_id, str) or not block_id or candidate_block_id != block_id:
        raise BlockContractError("sentence-alignment-rebase-owner")

    previous_spans_tuple, previous_groups_tuple, anchor_ids = _rebase_old_partition(
        block=block,
        previous_projection=previous_projection,
        previous_spans=previous_spans,
        previous_groups=previous_groups,
    )
    projection = target_alignment_projection(block, candidate)
    if not isinstance(projection, str) or not projection:
        return None

    if projection == previous_projection:
        opcodes = [("equal", 0, len(projection), 0, len(projection))]
    else:
        # Shared-reviewer candidates are untrusted. Bound both input size and
        # repeated-character comparisons before the non-autojunk diff; large
        # or repetitive edits retain the ordinary labelled fallback instead.
        if max(len(previous_projection), len(projection)) > _MAX_REBASE_CHARACTERS:
            return None
        before_counts, after_counts = Counter(previous_projection), Counter(projection)
        match_pairs = sum(
            count * after_counts[char] for char, count in before_counts.items()
        )
        if match_pairs > _MAX_REBASE_MATCH_PAIRS:
            return None
        try:
            opcodes = _BoundedRebaseMatcher(
                previous_projection, projection
            ).get_opcodes()
        except _RebaseWorkLimit:
            return None
    if len(opcodes) * len(previous_spans_tuple) > _MAX_REBASE_MATCH_PAIRS:
        return None
    pieces: list[list[tuple[int, int, str, int]]] = [[] for _ in previous_spans_tuple]
    for opcode_index, (tag, old_start, old_end, new_start, new_end) in enumerate(
        opcodes
    ):
        if tag == "equal":
            for span_index, span in enumerate(previous_spans_tuple):
                overlap_start = max(old_start, span.start)
                overlap_end = min(old_end, span.end)
                if overlap_start < overlap_end:
                    new_overlap_start = new_start + (overlap_start - old_start)
                    new_overlap_end = new_start + (overlap_end - old_start)
                    pieces[span_index].append(
                        (
                            overlap_start - span.start,
                            overlap_end - span.start,
                            projection[new_overlap_start:new_overlap_end],
                            opcode_index,
                        )
                    )
            continue

        owner = _rebase_owner(
            old_start,
            old_end,
            previous_projection_length=len(previous_projection),
            spans=previous_spans_tuple,
        )
        if owner is None:
            return None
        if _SENTENCE_DELIMITERS.intersection(
            previous_projection[old_start:old_end]
        ) or _SENTENCE_DELIMITERS.intersection(projection[new_start:new_end]):
            return None
        span = previous_spans_tuple[owner]
        pieces[owner].append(
            (
                old_start - span.start,
                old_end - span.start,
                projection[new_start:new_end],
                opcode_index,
            )
        )

    chunks: list[str] = []
    for span, span_pieces in zip(previous_spans_tuple, pieces, strict=True):
        local_cursor = 0
        chunk_parts: list[str] = []
        for local_start, local_end, replacement, _opcode_index in sorted(
            span_pieces, key=lambda item: (item[0], item[3])
        ):
            if local_start < local_cursor:
                return None
            chunk_parts.append(
                previous_projection[
                    span.start + local_cursor : span.start + local_start
                ]
            )
            chunk_parts.append(replacement)
            local_cursor = max(local_cursor, local_end)
        chunk_parts.append(previous_projection[span.start + local_cursor : span.end])
        chunk = "".join(chunk_parts)
        if not chunk.strip():
            return None
        chunks.append(chunk)
    if "".join(chunks) != projection:
        return None

    target_sentences: list[str] = []
    target_spans: list[TargetSentenceSpan] = []
    cursor = 0
    for order, chunk in enumerate(chunks, start=1):
        stripped = chunk.strip()
        if not stripped:
            return None
        end = cursor + len(chunk)
        target_sentences.append(stripped)
        target_spans.append(TargetSentenceSpan(order, cursor, end, chunk))
        cursor = end

    old_anchor_ordinals = {
        anchor_id: index for index, anchor_id in enumerate(anchor_ids, 1)
    }
    old_span_ordinals = {
        span.id: index for index, span in enumerate(previous_spans_tuple, 1)
    }
    groups = tuple(
        SentenceAlignmentGroup(
            tuple(old_anchor_ordinals[item] for item in group.source_anchor_ids),
            tuple(old_span_ordinals[item] for item in group.target_span_ids),
        )
        for group in previous_groups_tuple
    )
    return SentenceAlignmentManifest(
        block_id=block_id,
        target_sentences=tuple(target_sentences),
        target_spans=tuple(target_spans),
        groups=groups,
    )


@dataclass(frozen=True, slots=True)
class _ProjectionRun:
    """One candidate part's visible range in the alignment projection."""

    part_index: int
    kind: str
    start: int
    end: int


def _sentence_edit_input(
    edit: Mapping[str, Any], *, sentence_count: int
) -> tuple[str, int, str | None, int | None]:
    """Validate the deliberately small, exact sentence-edit DTO."""

    if not isinstance(edit, Mapping):
        raise BlockContractError("sentence-edit-shape")
    action = edit.get("action")
    if action == "replace":
        expected_keys = {"action", "sentence_order", "text"}
    elif action == "split":
        expected_keys = {"action", "sentence_order", "text", "offset"}
    elif action == "merge_next":
        expected_keys = {"action", "sentence_order"}
    else:
        raise BlockContractError("sentence-edit-action")
    if set(edit) != expected_keys:
        raise BlockContractError("sentence-edit-shape")
    sentence_order = edit["sentence_order"]
    if (
        isinstance(sentence_order, bool)
        or not isinstance(sentence_order, int)
        or not 1 <= sentence_order <= sentence_count
    ):
        raise BlockContractError("sentence-edit-order")
    if action == "merge_next" and sentence_order == sentence_count:
        raise BlockContractError("sentence-edit-no-next")
    if action == "merge_next":
        return action, sentence_order, None, None

    text = edit["text"]
    if not isinstance(text, str) or not text.strip():
        raise BlockContractError("sentence-edit-text")
    if action == "replace":
        return action, sentence_order, text, None

    offset = edit["offset"]
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise BlockContractError("sentence-edit-offset")
    return action, sentence_order, text, offset


def _sentence_edit_opcodes(
    previous: str, current: str
) -> list[tuple[str, int, int, int, int]]:
    """Run the existing bounded matcher, rejecting unsafe comparisons."""

    if max(len(previous), len(current)) > _MAX_REBASE_CHARACTERS:
        raise BlockContractError("sentence-edit-work-limit")
    previous_counts, current_counts = Counter(previous), Counter(current)
    match_pairs = sum(
        count * current_counts[char] for char, count in previous_counts.items()
    )
    if match_pairs > _MAX_REBASE_MATCH_PAIRS:
        raise BlockContractError("sentence-edit-work-limit")
    try:
        return _BoundedRebaseMatcher(previous, current).get_opcodes()
    except _RebaseWorkLimit as exc:
        raise BlockContractError("sentence-edit-work-limit") from exc


def _candidate_projection_runs(
    block: TranslationBlock, candidate: TargetBlockCandidate
) -> tuple[_ProjectionRun, ...]:
    """Map editable and protected candidate parts to projection offsets."""

    literals = {slot.id: slot.source_value for slot in literal_reference_slots(block)}
    runs: list[_ProjectionRun] = []
    cursor = 0
    for part_index, part in enumerate(candidate.parts):
        if part.kind == "text":
            end = cursor + len(part.text or "")
            if end > cursor:
                runs.append(_ProjectionRun(part_index, "text", cursor, end))
            cursor = end
        elif part.kind == "literal":
            literal = literals.get(part.literal_slot_id or "")
            if literal is None:
                raise BlockContractError("sentence-edit-protected-text")
            end = cursor + len(literal)
            if end > cursor:
                runs.append(_ProjectionRun(part_index, "literal", cursor, end))
            cursor = end
        elif part.kind not in {"token", "format"}:
            raise BlockContractError("sentence-edit-projection")
    return tuple(runs)


def _edit_text_parts(
    block: TranslationBlock,
    candidate: TargetBlockCandidate,
    *,
    selected_start: int,
    selected_end: int,
    previous_segment: str,
    desired_segment: str,
) -> TargetBlockCandidate:
    """Apply a bounded text diff without rewriting protected candidate parts."""

    runs = _candidate_projection_runs(block, candidate)
    selected_runs = tuple(
        run for run in runs if run.end > selected_start and run.start < selected_end
    )
    opcodes = _sentence_edit_opcodes(previous_segment, desired_segment)
    patches: dict[int, list[tuple[int, int, str, int]]] = {}
    for opcode_index, (tag, old_start, old_end, new_start, new_end) in enumerate(
        opcodes
    ):
        if tag == "equal":
            continue
        global_start = selected_start + old_start
        global_end = selected_start + old_end
        literal_overlap = any(
            run.kind == "literal"
            and max(global_start, run.start) < min(global_end, run.end)
            for run in runs
        )
        if literal_overlap:
            raise BlockContractError("sentence-edit-protected-text")
        replacement = desired_segment[new_start:new_end]
        if old_start == old_end:
            # SequenceMatcher reports insertion points. Prefer the containing
            # text run, then the nearest editable run in the selected span;
            # this keeps the insertion local and deterministic at part edges.
            owner = next(
                (
                    run
                    for run in selected_runs
                    if run.kind == "text" and run.start <= global_start <= run.end
                ),
                None,
            )
            if owner is None:
                owner = next(
                    (
                        run
                        for run in reversed(selected_runs)
                        if run.kind == "text" and run.end <= global_start
                    ),
                    None,
                )
            if owner is None:
                owner = next(
                    (
                        run
                        for run in selected_runs
                        if run.kind == "text" and run.start >= global_start
                    ),
                    None,
                )
            if owner is None:
                raise BlockContractError("sentence-edit-protected-text")
            local = global_start - owner.start
            patches.setdefault(owner.part_index, []).append(
                (local, local, replacement, opcode_index)
            )
            continue

        affected = tuple(
            run
            for run in selected_runs
            if run.kind == "text"
            and max(global_start, run.start) < min(global_end, run.end)
        )
        if not affected:
            # A non-empty changed range that is not editable can only be a
            # protected literal; retain a distinct error for that invariant.
            raise BlockContractError("sentence-edit-protected-text")
        for run_index, run in enumerate(affected):
            local_start = max(global_start, run.start) - run.start
            local_end = min(global_end, run.end) - run.start
            patches.setdefault(run.part_index, []).append(
                (
                    local_start,
                    local_end,
                    replacement if run_index == 0 else "",
                    opcode_index,
                )
            )

    if not patches:
        updated_parts = candidate.parts
    else:
        updated: list[TargetTextPart] = list(candidate.parts)
        for part_index, part_patches in patches.items():
            part = updated[part_index]
            if part.kind != "text":
                raise BlockContractError("sentence-edit-protected-text")
            original = part.text or ""
            cursor = 0
            pieces: list[str] = []
            for local_start, local_end, replacement, _opcode_index in sorted(
                part_patches, key=lambda item: (item[0], item[3])
            ):
                if local_start < cursor:
                    raise BlockContractError("sentence-edit-work-limit")
                pieces.append(original[cursor:local_start])
                pieces.append(replacement)
                cursor = max(cursor, local_end)
            pieces.append(original[cursor:])
            updated[part_index] = TargetTextPart(kind="text", text="".join(pieces))
        updated_parts = tuple(updated)

    result = replace(candidate, parts=updated_parts)
    resulting_projection = target_alignment_projection(block, result)
    expected_projection = (
        target_alignment_projection(block, candidate)[:selected_start]
        + desired_segment
        + target_alignment_projection(block, candidate)[selected_end:]
    )
    if resulting_projection != expected_projection:
        raise BlockContractError("sentence-edit-protected-text")
    if (
        resulting_projection[:selected_start]
        != target_alignment_projection(block, candidate)[:selected_start]
        or resulting_projection[selected_start + len(desired_segment) :]
        != target_alignment_projection(block, candidate)[selected_end:]
    ):
        raise BlockContractError("sentence-edit-protected-text")
    return result


def _edit_groups(
    groups: Sequence[SentenceAlignmentGroup],
    *,
    action: str,
    sentence_order: int,
    target_count: int,
) -> tuple[SentenceAlignmentGroup, ...]:
    """Transform target ordinals while retaining source group membership."""

    def split_ordinals(ordinals: tuple[int, ...]) -> tuple[int, ...]:
        result: list[int] = []
        for ordinal in ordinals:
            if ordinal < sentence_order:
                result.append(ordinal)
            elif ordinal == sentence_order:
                result.extend((ordinal, ordinal + 1))
            else:
                result.append(ordinal + 1)
        return tuple(result)

    def merge_ordinals(ordinals: tuple[int, ...]) -> tuple[int, ...]:
        result: list[int] = []
        for ordinal in ordinals:
            if ordinal < sentence_order:
                mapped = ordinal
            elif ordinal == sentence_order:
                mapped = sentence_order
            elif ordinal == sentence_order + 1:
                continue
            else:
                mapped = ordinal - 1
            if not result or result[-1] != mapped:
                result.append(mapped)
        return tuple(result)

    if action == "replace":
        return tuple(groups)
    if action == "split":
        return tuple(
            SentenceAlignmentGroup(
                group.source_ordinals, split_ordinals(group.target_ordinals)
            )
            for group in groups
        )

    mapped = [
        (group, group.source_ordinals, merge_ordinals(group.target_ordinals))
        for group in groups
    ]
    # A merge that joins two groups must also join their source ordinals. The
    # old group order is already monotonic, so the affected groups are adjacent.
    merged: list[SentenceAlignmentGroup] = []
    previous_merge_group: SentenceAlignmentGroup | None = None
    for original, source_ordinals, target_ordinals in mapped:
        joins_previous = (
            previous_merge_group is not None
            and sentence_order in previous_merge_group.target_ordinals
            and sentence_order + 1 in original.target_ordinals
        )
        if joins_previous and merged:
            merged[-1] = SentenceAlignmentGroup(
                merged[-1].source_ordinals + source_ordinals,
                merged[-1].target_ordinals
                + tuple(
                    ordinal
                    for ordinal in target_ordinals
                    if ordinal not in merged[-1].target_ordinals
                ),
            )
        elif not target_ordinals:
            raise BlockContractError("sentence-edit-group-coverage")
        else:
            merged.append(SentenceAlignmentGroup(source_ordinals, target_ordinals))
        previous_merge_group = original
    if any(not group.target_ordinals for group in merged):
        raise BlockContractError("sentence-edit-group-coverage")
    _manifest_groups(
        [group.to_dict() for group in merged],
        source_count=sum(len(group.source_ordinals) for group in merged),
        target_count=target_count,
    )
    return tuple(merged)


def apply_sentence_edit(
    block: TranslationBlock,
    candidate: TargetBlockCandidate,
    previous_spans: Sequence[TargetSpan],
    previous_groups: Sequence[AlignmentGroup],
    edit: Mapping[str, Any],
) -> tuple[TargetBlockCandidate, SentenceAlignmentManifest]:
    """Apply one explicit sentence edit and return its fresh candidate/manifest.

    The candidate and saved partition must already be verified by persistence;
    this helper repeats the partition checks before changing any target data.
    User supplied boundaries are authoritative here, so no punctuation or
    target-language atomicity check is performed.
    """

    if not isinstance(candidate, TargetBlockCandidate):
        raise BlockContractError("sentence-edit-candidate")
    if candidate.block_id != block.id:
        raise BlockContractError("sentence-edit-owner")
    saved_spans, saved_groups, _anchor_ids = _rebase_old_partition(
        block=block,
        previous_projection=target_alignment_projection(block, candidate),
        previous_spans=previous_spans,
        previous_groups=previous_groups,
    )
    action, sentence_order, text, offset = _sentence_edit_input(
        edit, sentence_count=len(saved_spans)
    )
    old_projection = target_alignment_projection(block, candidate)
    selected = saved_spans[sentence_order - 1]
    old_segment = old_projection[selected.start : selected.end]
    if not old_segment.strip():
        raise BlockContractError("sentence-edit-span")
    leading = len(old_segment) - len(old_segment.lstrip())
    trailing_start = len(old_segment.rstrip())
    leading_text = old_segment[:leading]
    trailing_text = old_segment[trailing_start:]

    if action == "merge_next":
        next_span = saved_spans[sentence_order]
        edit_end = next_span.end
        desired_segment = old_segment + old_projection[next_span.start : next_span.end]
    else:
        edit_end = selected.end
        assert text is not None
        body = text.strip()
        if action == "replace":
            desired_segment = leading_text + body + trailing_text
        else:
            assert offset is not None
            boundary = offset
            while boundary < len(body) and body[boundary].isspace():
                boundary += 1
            first_body, second_body = body[:boundary], body[boundary:]
            if not first_body.strip() or not second_body.strip():
                raise BlockContractError("sentence-edit-offset")
            desired_segment = leading_text + first_body + second_body + trailing_text

    if target_alignment_scope(block).startswith("attribute:"):
        slot_id = target_alignment_scope(block).removeprefix("attribute:")
        attrs = tuple(
            item for item in candidate.lexical_attributes if item.slot_id == slot_id
        )
        if len(attrs) != 1:
            raise BlockContractError("sentence-edit-attribute")
        if desired_segment != old_segment:
            candidate = replace(
                candidate,
                lexical_attributes=(
                    TargetLexicalAttribute(
                        slot_id=slot_id,
                        value=old_projection[: selected.start] + desired_segment + old_projection[edit_end:],
                    ),
                ),
            )
    elif action != "merge_next" and desired_segment != old_segment:
        candidate = _edit_text_parts(
            block,
            candidate,
            selected_start=selected.start,
            selected_end=selected.end,
            previous_segment=old_segment,
            desired_segment=desired_segment,
        )

    projection = target_alignment_projection(block, candidate)
    expected = (
        old_projection[: selected.start] + desired_segment + old_projection[edit_end:]
    )
    if projection != expected:
        raise BlockContractError("sentence-edit-protected-text")

    chunks: list[str]
    if action == "split":
        assert text is not None and offset is not None
        body = text.strip()
        boundary = offset
        while boundary < len(body) and body[boundary].isspace():
            boundary += 1
        chunks = [
            leading_text + body[:boundary],
            body[boundary:] + trailing_text,
        ]
    elif action == "merge_next":
        next_span = saved_spans[sentence_order]
        chunks = [old_segment + old_projection[next_span.start : next_span.end]]
    else:
        chunks = [desired_segment]
    if any(not chunk.strip() for chunk in chunks):
        raise BlockContractError("sentence-edit-span")

    new_spans: list[TargetSentenceSpan] = []
    new_sentences: list[str] = []
    cursor = 0
    for index, old_span in enumerate(saved_spans):
        if action == "split" and index == sentence_order - 1:
            parts = chunks
        elif action == "merge_next" and index == sentence_order - 1:
            parts = chunks
        elif action == "merge_next" and index == sentence_order:
            continue
        elif action == "replace" and index == sentence_order - 1:
            parts = chunks
        else:
            parts = [old_projection[old_span.start : old_span.end]]
        for part in parts:
            end = cursor + len(part)
            new_spans.append(TargetSentenceSpan(len(new_spans) + 1, cursor, end, part))
            new_sentences.append(part.strip())
            cursor = end
    if "".join(span.text for span in new_spans) != projection:
        raise BlockContractError("sentence-edit-coverage")
    old_anchor_ordinals = {
        anchor.id: index for index, anchor in enumerate(block.anchors, start=1)
    }
    ordinal_groups = tuple(
        SentenceAlignmentGroup(
            tuple(old_anchor_ordinals[item] for item in group.source_anchor_ids),
            tuple(
                next(
                    index
                    for index, span in enumerate(saved_spans, start=1)
                    if span.id == target_id
                )
                for target_id in group.target_span_ids
            ),
        )
        for group in saved_groups
    )
    new_groups = _edit_groups(
        ordinal_groups,
        action=action,
        sentence_order=sentence_order,
        target_count=len(new_spans),
    )
    # Keep the helper's result independent from the caller's candidate even
    # when a merge or wording replacement leaves all part values unchanged.
    candidate = replace(candidate)
    return candidate, SentenceAlignmentManifest(
        block_id=block.id,
        target_sentences=tuple(new_sentences),
        target_spans=tuple(new_spans),
        groups=new_groups,
    )


__all__ = [
    "MAX_ALIGNMENT_GROUPS",
    "MAX_ALIGNMENT_SENTENCES",
    "SentenceAlignmentGroup",
    "SentenceAlignmentManifest",
    "TargetSentenceSpan",
    "apply_sentence_edit",
    "normalize_marked_sentence_alignment",
    "normalize_sentence_alignment",
    "rebase_sentence_alignment",
]
