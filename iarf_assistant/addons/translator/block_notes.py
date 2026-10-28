"""Validation and rendering primitives for immutable document-block notes.

Notes deliberately live beside, rather than inside, the target candidate.  A
candidate hash therefore remains a hash of translation material only, while a
note receipt separately binds its reader-facing content to one target revision.
"""

from __future__ import annotations

import hashlib
import html
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

from mistune import InlineParser

from carlina.core.utc import utc_iso

from .block_contract import BlockContractError
from .integrity_receipts import make_receipt, verify_receipt

BLOCK_NOTE_CONTRACT_VERSION = "translation-block-note-v1"
MAX_BLOCK_NOTES_PER_WINDOW = 3
MAX_BLOCK_NOTES_PER_BLOCK = 1
MAX_STUDY_NOTES_PER_BLOCK = 6
MAX_STUDY_NOTES_PER_WINDOW = 24
MAX_BLOCK_NOTE_ANCHOR_CHARS = 160
MAX_BLOCK_NOTE_BODY_CHARS = 800
MAX_READER_NOTE_ANCHOR_CHARS = 160
MAX_READER_NOTE_BODY_CHARS = 4000

ANNOTATION_PROFILE_NONE = "none"
ANNOTATION_PROFILE_RESTRAINED = "restrained"
ANNOTATION_PROFILE_STUDY = "study"
ANNOTATION_PROFILES = frozenset(
    {
        ANNOTATION_PROFILE_NONE,
        ANNOTATION_PROFILE_RESTRAINED,
        ANNOTATION_PROFILE_STUDY,
    }
)

_UNSET = object()

_MARKDOWN_LINK_RE = re.compile(r"\[[^\]]+\]\([^\)]*\)")
_TAG_RE = re.compile(r"<!--.*?-->|<[^>]*>", re.DOTALL)
_NOTE_ID_RE = re.compile(r"[^A-Za-z0-9_.:-]+")


def render_note_body(body: str) -> str:
    """Render limited inline Markdown without interpreting HTML, links or images.

    Raw note bodies and their integrity receipts remain unchanged. Only this
    derived reading representation interprets emphasis, strong emphasis, code
    spans and Markdown escapes. No token can introduce attributes or URLs.
    """

    parser = InlineParser()
    parser.rules = ["escape", "codespan", "emphasis", "softbreak"]

    def render_tokens(tokens: list[dict[str, Any]]) -> str:
        fragments = []
        for token in tokens:
            kind = token["type"]
            if kind in {"emphasis", "strong"}:
                tag = "em" if kind == "emphasis" else "strong"
                fragments.append(f"<{tag}>{render_tokens(token['children'])}</{tag}>")
            elif kind == "codespan":
                fragments.append(f"<code>{html.escape(token['raw'], quote=False)}</code>")
            elif kind == "softbreak":
                fragments.append("<br>")
            else:
                fragments.append(html.escape(token.get("raw", ""), quote=False))
        return "".join(fragments)

    return render_tokens(parser(body, {}))


def normalize_annotation_profile(
    profile: object = None, enable_translator_notes: object = None
) -> str:
    """Return the canonical annotation profile, preserving legacy settings.

    A supplied canonical profile always wins over the compatibility boolean.
    Booleans are accepted as the historical profile spelling so old callers
    can migrate without changing their request shape.  Invalid explicit
    values fail closed rather than silently falling back to the legacy flag.
    """

    if profile is not None:
        if isinstance(profile, bool):
            return ANNOTATION_PROFILE_RESTRAINED if profile else ANNOTATION_PROFILE_NONE
        if isinstance(profile, str) and profile in ANNOTATION_PROFILES:
            return profile
        raise BlockContractError("block-note-profile")
    if enable_translator_notes is None:
        return ANNOTATION_PROFILE_NONE
    if not isinstance(enable_translator_notes, bool):
        raise BlockContractError("block-note-profile")
    return (
        ANNOTATION_PROFILE_RESTRAINED
        if enable_translator_notes
        else ANNOTATION_PROFILE_NONE
    )


@dataclass(frozen=True, slots=True)
class BlockNoteCandidate:
    """One validated model note before it is attached to a target revision."""

    block_id: str
    anchor_text: str
    body: str
    order_index: int = 0

    def __post_init__(self) -> None:
        if (
            not isinstance(self.block_id, str)
            or not self.block_id
            or self.block_id != self.block_id.strip()
        ):
            raise BlockContractError("block-note-block")
        _plain_note_text(
            self.anchor_text,
            maximum=MAX_BLOCK_NOTE_ANCHOR_CHARS,
            code="block-note-anchor",
        )
        _plain_note_text(
            self.body,
            maximum=MAX_BLOCK_NOTE_BODY_CHARS,
            code="block-note-body",
        )
        if (
            not isinstance(self.order_index, int)
            or isinstance(self.order_index, bool)
            or self.order_index < 0
        ):
            raise BlockContractError("block-note-order")

    def to_provider_dict(self) -> dict[str, str]:
        return {"anchor": self.anchor_text, "body": self.body}


@dataclass(frozen=True, slots=True)
class ReaderNoteCandidate:
    """Validated, render-only projection of one durable reader-note head.

    Reader notes are deliberately a separate representation from model note
    candidates.  Their anchors may select a repeated target occurrence or
    span across protected inline markup, and their identity is the durable
    note id rather than a target-revision/order pair.
    """

    id: str
    block_id: str
    anchor_kind: str
    anchor_text: str | None
    occurrence_index: int | None
    body: str
    citations: tuple[str, ...] = ()
    target_revision_id: str | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.id, str)
            or not self.id
            or self.id != self.id.strip()
            or len(self.id.encode("utf-8")) > 256
            or _unsafe_reader_text(self.id)
        ):
            raise BlockContractError("reader-note-id")
        if (
            not isinstance(self.block_id, str)
            or not self.block_id
            or self.block_id != self.block_id.strip()
        ):
            raise BlockContractError("reader-note-block")
        if self.anchor_kind not in {"target", "source", "block"}:
            raise BlockContractError("reader-note-anchor-shape")
        if self.anchor_kind == "block":
            if (
                self.anchor_text is not None
                or self.occurrence_index is not None
                or self.target_revision_id is not None
            ):
                raise BlockContractError("reader-note-anchor-shape")
        else:
            _reader_note_text(
                self.anchor_text,
                maximum=MAX_READER_NOTE_ANCHOR_CHARS,
                code="reader-note-anchor-text",
            )
            if (
                isinstance(self.occurrence_index, bool)
                or not isinstance(self.occurrence_index, int)
                or self.occurrence_index < 0
            ):
                raise BlockContractError("reader-note-occurrence")
            if self.anchor_kind != "target" and self.target_revision_id is not None:
                raise BlockContractError("reader-note-anchor-shape")
        if (
            not isinstance(self.body, str)
            or not self.body.strip()
            or len(self.body) > MAX_READER_NOTE_BODY_CHARS
            or _unsafe_reader_text(self.body)
        ):
            raise BlockContractError("reader-note-body")
        if not isinstance(self.citations, tuple) or any(
            not isinstance(citation, str) for citation in self.citations
        ):
            raise BlockContractError("reader-note-citations")
        if self.target_revision_id is not None and (
            not isinstance(self.target_revision_id, str)
            or not self.target_revision_id
            or self.target_revision_id != self.target_revision_id.strip()
        ):
            raise BlockContractError("reader-note-target")


def _reader_note_text(value: object, *, maximum: int, code: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > maximum
        or _unsafe_reader_text(value)
    ):
        raise BlockContractError(code)
    return value


def _unsafe_reader_text(value: str) -> bool:
    return any(
        unicodedata.category(char).startswith("C") and char not in "\t\n\r"
        for char in value
    )


def _plain_note_text(value: object, *, maximum: int, code: str) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= maximum
        or not value.strip()
    ):
        raise BlockContractError(code)
    if any(unicodedata.category(char).startswith("C") for char in value):
        raise BlockContractError(code)
    if "<" in value or ">" in value or _MARKDOWN_LINK_RE.search(value):
        raise BlockContractError(code)
    return value


def note_from_provider(
    block_id: str, value: object, *, order_index: int = 0
) -> BlockNoteCandidate:
    """Parse the exact provider note shape; no aliases or extra keys survive."""

    if not isinstance(value, Mapping) or set(value) != {"anchor", "body"}:
        raise BlockContractError("block-note-shape")
    return BlockNoteCandidate(
        block_id=block_id,
        anchor_text=value["anchor"],
        body=value["body"],
        order_index=order_index,
    )


def _decoded_with_raw_offsets(value: str) -> tuple[str, tuple[tuple[int, int], ...]]:
    decoded: list[str] = []
    offsets: list[tuple[int, int]] = []
    index = 0
    while index < len(value):
        if value[index] == "&":
            end = value.find(";", index + 1)
            if end != -1:
                token = value[index : end + 1]
                expanded = html.unescape(token)
                if expanded != token:
                    for char in expanded:
                        decoded.append(char)
                        offsets.append((index, end + 1))
                    index = end + 1
                    continue
        decoded.append(value[index])
        offsets.append((index, index + 1))
        index += 1
    return "".join(decoded), tuple(offsets)


def note_anchor_span(fragment: str, anchor_text: str) -> tuple[int, int]:
    """Find an anchor exactly once in one rendered text node.

    Returned offsets address the original rendered fragment, including entity
    spellings.  This lets the renderer inject a reference without reparsing or
    serializing the target markup.
    """

    _plain_note_text(
        anchor_text,
        maximum=MAX_BLOCK_NOTE_ANCHOR_CHARS,
        code="block-note-anchor",
    )
    matches: list[tuple[int, int]] = []
    cursor = 0
    for tag_match in _TAG_RE.finditer(fragment):
        text_end = tag_match.start()
        if text_end > cursor:
            decoded, offsets = _decoded_with_raw_offsets(fragment[cursor:text_end])
            start = 0
            while True:
                found = decoded.find(anchor_text, start)
                if found < 0:
                    break
                end = found + len(anchor_text)
                if found < len(offsets) and end <= len(offsets):
                    matches.append(
                        (
                            cursor + offsets[found][0],
                            cursor + offsets[end - 1][1],
                        )
                    )
                # Advance one character so overlapping occurrences (for
                # example ``aa`` in ``aaa``) also invalidate the anchor.
                start = found + 1
        cursor = tag_match.end()
    if cursor < len(fragment):
        decoded, offsets = _decoded_with_raw_offsets(fragment[cursor:])
        start = 0
        while True:
            found = decoded.find(anchor_text, start)
            if found < 0:
                break
            end = found + len(anchor_text)
            if found < len(offsets) and end <= len(offsets):
                matches.append(
                    (cursor + offsets[found][0], cursor + offsets[end - 1][1])
                )
            start = found + 1
    if len(matches) != 1:
        raise BlockContractError("block-note-anchor-topology")
    return matches[0]


def _has_rendered_text(fragment: str) -> bool:
    text = _TAG_RE.sub("", fragment)
    return bool(html.unescape(text).strip())


def validate_note_candidates(
    notes: Sequence[BlockNoteCandidate],
    rendered_fragments: Mapping[str, str],
    annotation_profile: object = _UNSET,
    *,
    profile: object = _UNSET,
    enable_translator_notes: object = None,
) -> tuple[BlockNoteCandidate, ...]:
    """Validate candidates against the frozen annotation profile.

    Omitting the profile retains the pre-profile restrained policy for direct
    legacy callers.  New callers should pass ``annotation_profile`` (or the
    ``profile`` alias) explicitly; ``none`` accepts only an empty candidate
    sequence.
    """

    if profile is not _UNSET:
        annotation_profile = profile
    if annotation_profile is _UNSET:
        effective_profile = (
            ANNOTATION_PROFILE_RESTRAINED
            if enable_translator_notes is None
            else normalize_annotation_profile(None, enable_translator_notes)
        )
    else:
        effective_profile = normalize_annotation_profile(
            annotation_profile, enable_translator_notes
        )

    if effective_profile == ANNOTATION_PROFILE_NONE and notes:
        raise BlockContractError("block-note-disabled")

    max_per_block = (
        MAX_STUDY_NOTES_PER_BLOCK
        if effective_profile == ANNOTATION_PROFILE_STUDY
        else MAX_BLOCK_NOTES_PER_BLOCK
    )
    max_per_window = (
        MAX_STUDY_NOTES_PER_WINDOW
        if effective_profile == ANNOTATION_PROFILE_STUDY
        else MAX_BLOCK_NOTES_PER_WINDOW
    )

    if len(notes) > max_per_window:
        raise BlockContractError("block-note-window-cap")
    result = tuple(notes)
    if any(not isinstance(note, BlockNoteCandidate) for note in result):
        raise BlockContractError("block-note-type")
    block_ids = {unicodedata.normalize("NFC", note.block_id).strip() for note in result}
    if effective_profile != ANNOTATION_PROFILE_STUDY and len(block_ids) != len(result):
        raise BlockContractError("block-note-block-duplicate")
    identities: set[tuple[str, str]] = set()
    per_block_counts: dict[str, int] = {}
    for note in result:
        fragment = rendered_fragments.get(note.block_id)
        if not isinstance(fragment, str) or not _has_rendered_text(fragment):
            raise BlockContractError("block-note-empty-block")
        normalized_block_id = unicodedata.normalize("NFC", note.block_id).strip()
        normalized_anchor = unicodedata.normalize("NFC", note.anchor_text)
        identity = (normalized_block_id, normalized_anchor)
        if identity in identities:
            raise BlockContractError("block-note-anchor-duplicate")
        count = per_block_counts.get(normalized_block_id, 0) + 1
        if count > max_per_block:
            raise BlockContractError("block-note-block-cap")
        note_anchor_span(fragment, note.anchor_text)
        identities.add(identity)
        per_block_counts[normalized_block_id] = count
    return result


def notes_allowed_for_block(block: Any) -> bool:
    """Return whether a planned block has reader-visible content to annotate."""

    return any(
        getattr(slot, "kind", None) != "attribute"
        for slot in getattr(block, "slots", ())
    )


def note_receipt_values(
    *,
    project_id: str,
    source_plan_id: str,
    block_id: str,
    revision_id: str,
    order_index: int,
    anchor_text: str,
    body: str,
    author_type: str,
    created_at: datetime,
) -> dict[str, object]:
    return {
        "receipt_version": 1,
        "project_id": project_id,
        "source_plan_id": source_plan_id,
        "block_id": block_id,
        "revision_id": revision_id,
        "order_index": order_index,
        "anchor_text": anchor_text,
        "body": body,
        "author_type": author_type,
        "created_at": utc_iso(created_at, timespec="microseconds"),
    }


def make_note_receipt(**values: object) -> str:
    return make_receipt(
        purpose=BLOCK_NOTE_CONTRACT_VERSION,
        values=note_receipt_values(**cast(dict[str, Any], values)),
    )


def verify_note_receipt(
    *, stored: object, code: str = "block-note-receipt", **values: object
) -> str:
    return verify_receipt(
        stored=stored,
        purpose=BLOCK_NOTE_CONTRACT_VERSION,
        values=note_receipt_values(**cast(dict[str, Any], values)),
        code=code,
    )


def persisted_note_projection(
    rows: Sequence[Any],
    *,
    project_id: str,
    source_plan_id: str,
    block_id: str,
    revision_id: str,
    rendered_fragment: str,
    annotation_profile: object = _UNSET,
    profile: object = _UNSET,
    enable_translator_notes: object = None,
) -> tuple[dict[str, str], ...]:
    """Verify immutable rows and return only the workspace-safe projection."""

    if profile is not _UNSET:
        annotation_profile = profile
    if annotation_profile is _UNSET:
        effective_profile = (
            ANNOTATION_PROFILE_RESTRAINED
            if enable_translator_notes is None
            else normalize_annotation_profile(None, enable_translator_notes)
        )
    else:
        effective_profile = normalize_annotation_profile(
            annotation_profile, enable_translator_notes
        )
    max_per_window = (
        MAX_STUDY_NOTES_PER_WINDOW
        if effective_profile == ANNOTATION_PROFILE_STUDY
        else MAX_BLOCK_NOTES_PER_WINDOW
    )
    if len(rows) > max_per_window:
        raise BlockContractError("block-note-window-cap")
    ordered = sorted(rows, key=lambda row: (row.order_index, str(row.id)))
    if [row.order_index for row in ordered] != list(range(len(ordered))):
        raise BlockContractError("block-note-order")
    notes: list[BlockNoteCandidate] = []
    for row in ordered:
        receipt = getattr(row, "record_hash", None)
        if receipt is None:
            receipt = getattr(row, "integrity_receipt", None)
        if (
            getattr(row, "project_id", None) != project_id
            or getattr(row, "source_plan_id", None) != source_plan_id
            or getattr(row, "block_id", None) != block_id
            or getattr(row, "revision_id", None) != revision_id
            or getattr(row, "author_type", None) != "model"
            or not isinstance(getattr(row, "created_at", None), datetime)
        ):
            raise BlockContractError("block-note-owner")
        note = BlockNoteCandidate(
            block_id=block_id,
            anchor_text=row.anchor_text,
            body=row.body,
            order_index=row.order_index,
        )
        verify_note_receipt(
            stored=receipt,
            project_id=project_id,
            source_plan_id=source_plan_id,
            block_id=block_id,
            revision_id=revision_id,
            order_index=note.order_index,
            anchor_text=note.anchor_text,
            body=note.body,
            author_type="model",
            created_at=row.created_at,
        )
        notes.append(note)
    validate_note_candidates(
        notes,
        {block_id: rendered_fragment},
        annotation_profile=effective_profile,
    )
    return tuple(
        {
            "id": str(row.id),
            "anchor_text": note.anchor_text,
            "body": note.body,
            "body_html": render_note_body(note.body),
            "author_type": "model",
        }
        for row, note in zip(ordered, notes, strict=True)
    )


def stable_note_ids(block_id: str, order_index: int) -> tuple[str, str]:
    safe_block = (
        _NOTE_ID_RE.sub("-", block_id).strip("-")
        or hashlib.sha256(block_id.encode("utf-8")).hexdigest()[:16]
    )
    note_id = f"translator-block-note-{safe_block}-{order_index + 1}"
    return note_id, f"{note_id}-ref"


def stable_reader_note_ids(note_id: str) -> tuple[str, str]:
    """Derive semantic HTML ids from a durable reader-note identity."""

    if (
        not isinstance(note_id, str)
        or not note_id
        or note_id != note_id.strip()
        or len(note_id.encode("utf-8")) > 256
        or _unsafe_reader_text(note_id)
    ):
        raise BlockContractError("reader-note-id")
    safe_note = _NOTE_ID_RE.sub("-", note_id).strip("-")
    if not safe_note:
        safe_note = hashlib.sha256(note_id.encode("utf-8")).hexdigest()
    stable_id = f"translator-reader-note-{safe_note}"
    return stable_id, f"{stable_id}-ref"


def inject_note_reference(
    fragment: str,
    note: BlockNoteCandidate,
    *,
    note_id: str,
    reference_id: str,
    number: int,
) -> str:
    _start, end = note_anchor_span(fragment, note.anchor_text)
    reference = (
        f'<a id="{html.escape(reference_id, quote=True)}" '
        f'href="#{html.escape(note_id, quote=True)}" role="doc-noteref" '
        f'epub:type="noteref" aria-label="Translator note T{number}">[T{number}]</a>'
    )
    return fragment[:end] + reference + fragment[end:]


def render_endnotes(
    notes: Sequence[tuple[BlockNoteCandidate | ReaderNoteCandidate, str, str, int]],
) -> str:
    if not notes:
        return ""
    entries = []
    for note, note_id, reference_id, number in notes:
        reader_attributes = ' class="translator-note" data-label="Translator note"'
        citations = ""
        if isinstance(note, ReaderNoteCandidate):
            reader_attributes = (
                ' class="translator-reader-note" data-label="Translator note"'
            )
            if note.citations:
                citation_items = ", ".join(
                    f"<cite>{html.escape(citation, quote=False)}</cite>"
                    for citation in note.citations
                )
                citations = (
                    '<span class="translator-note-citations">References: '
                    f"{citation_items}</span> "
                )
        entries.append(
            f'<div id="{html.escape(note_id, quote=True)}" role="listitem" '
            f'epub:type="footnote"{reader_attributes}><p>'
            f'<span class="footnote-marker">T{number}. </span>'
            '<span class="translator-note-label">Translator’s note: </span>'
            f"{render_note_body(note.body)} {citations}"
            f'<a href="#{html.escape(reference_id, quote=True)}" role="doc-backlink" '
            f'epub:type="backlink">↩</a></p></div>'
        )
    return (
        '<section id="translator-doc-endnotes" role="doc-endnotes" '
        f'epub:type="endnotes"><div role="list">{"".join(entries)}</div></section>'
    )


__all__ = [
    "ANNOTATION_PROFILES",
    "ANNOTATION_PROFILE_NONE",
    "ANNOTATION_PROFILE_RESTRAINED",
    "ANNOTATION_PROFILE_STUDY",
    "BLOCK_NOTE_CONTRACT_VERSION",
    "MAX_BLOCK_NOTES_PER_BLOCK",
    "MAX_BLOCK_NOTES_PER_WINDOW",
    "MAX_BLOCK_NOTE_ANCHOR_CHARS",
    "MAX_BLOCK_NOTE_BODY_CHARS",
    "MAX_READER_NOTE_ANCHOR_CHARS",
    "MAX_READER_NOTE_BODY_CHARS",
    "MAX_STUDY_NOTES_PER_BLOCK",
    "MAX_STUDY_NOTES_PER_WINDOW",
    "BlockNoteCandidate",
    "ReaderNoteCandidate",
    "inject_note_reference",
    "make_note_receipt",
    "normalize_annotation_profile",
    "note_anchor_span",
    "note_from_provider",
    "note_receipt_values",
    "notes_allowed_for_block",
    "persisted_note_projection",
    "render_endnotes",
    "stable_note_ids",
    "stable_reader_note_ids",
    "validate_note_candidates",
    "verify_note_receipt",
]
