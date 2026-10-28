"""Compact provider/editor markup for document-block translation.

The source planner and durable target candidate keep their full identities.  This
module deliberately exposes only natural inline markup plus short, packet-local
references, then restores the existing source-bound candidate before validation
or persistence.
"""

from __future__ import annotations

import html
import html.entities
import re
import xml.etree.ElementTree as ET
from collections import defaultdict, deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .alignment_contract import target_alignment_scope
from .block_contract import (
    BlockContractError,
    ProtectedInlineToken,
    TranslationBlock,
    TranslationSourcePlan,
)
from .block_notes import (
    ANNOTATION_PROFILE_NONE,
    BLOCK_NOTE_CONTRACT_VERSION,
    BlockNoteCandidate,
    normalize_annotation_profile,
    note_from_provider,
    notes_allowed_for_block,
    validate_note_candidates,
)
from .target_contract import (
    MAX_LEXICAL_VALUE_BYTES,
    MAX_TARGET_TEXT_BYTES,
    TARGET_LOCAL_FORMAT_TAGS,
    TargetBlockCandidate,
    TargetLexicalAttribute,
    TargetTextPart,
    lexical_slots,
    literal_reference_slots,
    structural_fingerprint,
)

COMPACT_EDITOR_CONTRACT_VERSION = "translation-block-editor-v1"
MAX_COMPACT_MARKUP_BYTES = MAX_TARGET_TEXT_BYTES * 2
MAX_SENTENCE_MARKERS = 4096

_FIELD_NAME_RE = re.compile(r"[^a-z0-9]+")
_OPEN_TAG_RE = re.compile(r"^<\s*[^\s/>]+(?P<attributes>.*?)/?\s*>$", re.DOTALL)
_NAMED_ENTITY_RE = re.compile(r"&(?P<name>[A-Za-z][A-Za-z0-9]+);")
_HTML_ENTITY_RE = re.compile(r"&(?:#[xX][0-9A-Fa-f]+|#[0-9]+|[A-Za-z][A-Za-z0-9]+);")
_XML_ENTITIES = frozenset({"amp", "apos", "gt", "lt", "quot"})


@dataclass(frozen=True, slots=True)
class _ElementDescriptor:
    tag: str
    open_token: ProtectedInlineToken
    close_token: ProtectedInlineToken | None
    local_ref: str | None

    @property
    def token_ids(self) -> tuple[str, ...]:
        if self.close_token is None:
            return (self.open_token.id,)
        return (self.open_token.id, self.close_token.id)


@dataclass(frozen=True, slots=True)
class _MarkupLayout:
    elements: tuple[_ElementDescriptor, ...]
    token_elements: Mapping[str, _ElementDescriptor]
    referenced_elements: Mapping[str, _ElementDescriptor]
    literal_refs: Mapping[str, str]
    refs_to_literals: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class _SourceLocation:
    field: str
    start: int
    offsets: tuple[int, ...]
    break_tag: bool = False


def _character_offsets(value: str) -> tuple[int, ...]:
    offsets = [0]
    for character in value:
        offsets.append(offsets[-1] + len(_escape_markup_text(character)))
    return tuple(offsets)


def compact_result_schema(*, notes_enabled: bool = False) -> dict[str, object]:
    """Return the static provider schema for compact window results.

    Notes are a versioned opt-in response shape.  Keeping the default schema
    unchanged is important for frozen v3/v4 attempts and for callers that do
    not request reader-facing annotations.
    """

    field = {
        "type": "object",
        "additionalProperties": False,
        "required": ["name", "value"],
        "properties": {
            "name": {"type": "string"},
            "value": {"type": "string"},
        },
    }
    result_properties: dict[str, object] = {
        "n": {"type": "integer", "minimum": 1},
        "content": {"type": "string"},
        "fields": {"type": "array", "items": field},
    }
    required = ["n", "content", "fields"]
    if notes_enabled:
        note = {
            "type": "object",
            "additionalProperties": False,
            "required": ["anchor", "body"],
            "properties": {
                "anchor": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 160,
                },
                "body": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 800,
                },
            },
        }
        result_properties["notes"] = {
            "type": "array",
            "maxItems": 1,
            "items": note,
        }
        required.append("notes")
    result = {
        "type": "object",
        "additionalProperties": False,
        "required": required,
        "properties": result_properties,
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["results"],
        "properties": {"results": {"type": "array", "items": result}},
    }


def _opening_has_attributes(token: ProtectedInlineToken) -> bool:
    if token.kind not in {"open", "void"}:
        return False
    match = _OPEN_TAG_RE.fullmatch(token.source_value.strip())
    if match is None:
        raise BlockContractError("compact-source-token")
    return bool(match.group("attributes").strip().rstrip("/").strip())


def _layout(block: TranslationBlock) -> _MarkupLayout:
    grouped: dict[str, list[ProtectedInlineToken]] = defaultdict(list)
    for token in block.protected_inline:
        if token.movement_policy == "source-only":
            continue
        grouped[token.element_id].append(token)

    pending: list[tuple[int, str, str]] = []
    raw_elements: list[
        tuple[int, ProtectedInlineToken, ProtectedInlineToken | None]
    ] = []
    for element_id, tokens in grouped.items():
        ordered = sorted(
            tokens, key=lambda item: (item.raw_start, item.raw_end, item.id)
        )
        starters = [item for item in ordered if item.kind in {"open", "void"}]
        closers = [item for item in ordered if item.kind == "close"]
        if len(starters) != 1 or len(closers) != (
            0 if starters[0].kind == "void" else 1
        ):
            raise BlockContractError("compact-source-token-pair")
        opener = starters[0]
        closer = closers[0] if closers else None
        if closer is not None and (
            closer.tag != opener.tag or closer.element_id != element_id
        ):
            raise BlockContractError("compact-source-token-pair")
        raw_elements.append((opener.raw_start, opener, closer))
        if opener.tag == "a" or _opening_has_attributes(opener):
            pending.append((opener.raw_start, "element", element_id))

    literals = literal_reference_slots(block)
    pending.extend((slot.raw_start, "literal", slot.id) for slot in literals)
    local_refs = {
        (kind, identity): str(index)
        for index, (_start, kind, identity) in enumerate(
            sorted(pending, key=lambda item: (item[0], item[1], item[2])), 1
        )
    }

    elements = tuple(
        _ElementDescriptor(
            tag=opener.tag,
            open_token=opener,
            close_token=closer,
            local_ref=local_refs.get(("element", opener.element_id)),
        )
        for _start, opener, closer in sorted(raw_elements, key=lambda item: item[0])
    )
    token_elements = {
        token_id: descriptor
        for descriptor in elements
        for token_id in descriptor.token_ids
    }
    referenced = {
        descriptor.local_ref: descriptor
        for descriptor in elements
        if descriptor.local_ref is not None
    }
    literal_refs = {slot.id: local_refs[("literal", slot.id)] for slot in literals}
    return _MarkupLayout(
        elements=elements,
        token_elements=token_elements,
        referenced_elements=referenced,
        literal_refs=literal_refs,
        refs_to_literals={value: key for key, value in literal_refs.items()},
    )


def _field_specs(block: TranslationBlock) -> tuple[tuple[str, Any], ...]:
    counts: dict[str, int] = defaultdict(int)
    result: list[tuple[str, Any]] = []
    for index, slot in enumerate(lexical_slots(block), 1):
        locator_tail = slot.locator.rsplit("@", 1)[-1] if "@" in slot.locator else ""
        base = (
            _FIELD_NAME_RE.sub("_", locator_tail.lower()).strip("_") or f"field_{index}"
        )
        counts[base] += 1
        name = base if counts[base] == 1 else f"{base}_{counts[base]}"
        result.append((name, slot))
    return tuple(result)


def _source_candidate(
    block: TranslationBlock,
) -> tuple[tuple[TargetTextPart, ...], tuple[TargetLexicalAttribute, ...]]:
    slots = {slot.id: slot for slot in block.slots}
    tokens = {token.id: token for token in block.protected_inline}
    literal_ids = {slot.id for slot in literal_reference_slots(block)}
    parts: list[TargetTextPart] = []
    for wire_part in block.wire:
        if wire_part.kind == "token":
            token = tokens.get(wire_part.ref_id)
            if token is not None and token.movement_policy == "source-only":
                continue
            parts.append(TargetTextPart(kind="token", token_id=wire_part.ref_id))
            continue
        slot = slots[wire_part.ref_id]
        if slot.kind == "attribute":
            continue
        if slot.kind == "boundary":
            parts.append(TargetTextPart(kind="text", text=" "))
            continue
        if slot.id in literal_ids:
            parts.append(TargetTextPart(kind="literal", literal_slot_id=slot.id))
        elif slot.disposition == "translatable":
            # Source slots retain the exact HTML witness, including named and
            # numeric character references.  The provider/editor boundary is
            # semantic text, so expose the character those references denote;
            # ``_markup_from_parts`` will escape XML-significant characters
            # exactly once when it constructs the compact fragment.
            parts.append(
                TargetTextPart(kind="text", text=html.unescape(slot.source_value))
            )
    attributes = tuple(
        TargetLexicalAttribute(slot_id=slot.id, value=html.unescape(slot.source_value))
        for slot in lexical_slots(block)
    )
    return tuple(parts), attributes


def _source_only_editor_wrapper_tags(block: TranslationBlock) -> tuple[str, ...]:
    """Return deterministic formatting omitted only from model-facing markup."""

    tokens = {token.id: token for token in block.protected_inline}
    openings: list[ProtectedInlineToken] = []
    closing_outer_first: list[ProtectedInlineToken] = []
    for part in block.wire:
        token = tokens.get(part.ref_id) if part.kind == "token" else None
        if (
            token is None
            or token.kind != "open"
            or token.movement_policy != "source-only"
        ):
            break
        openings.append(token)
    for part in reversed(block.wire):
        token = tokens.get(part.ref_id) if part.kind == "token" else None
        if (
            token is None
            or token.kind != "close"
            or token.movement_policy != "source-only"
        ):
            break
        closing_outer_first.append(token)
    if not openings and not closing_outer_first:
        return ()
    if len(openings) != len(closing_outer_first) or any(
        opening.tag != closing.tag
        for opening, closing in zip(
            openings,
            closing_outer_first,
            strict=True,
        )
    ):
        raise BlockContractError("compact-source-only-wrapper")
    return tuple(token.tag for token in openings)


def _wrap_editor_source_only(block: TranslationBlock, content: str) -> str:
    tags = _source_only_editor_wrapper_tags(block)
    if not tags:
        return content
    return (
        "".join(f"<{tag}>" for tag in tags)
        + content
        + "".join(f"</{tag}>" for tag in reversed(tags))
    )


def _strip_editor_source_only(block: TranslationBlock, content: object) -> object:
    tags = _source_only_editor_wrapper_tags(block)
    if not tags:
        return content
    if not isinstance(content, str):
        raise BlockContractError("compact-content")
    opening = "".join(f"<{tag}>" for tag in tags)
    closing = "".join(f"</{tag}>" for tag in reversed(tags))
    if not content.startswith(opening) or not content.endswith(closing):
        raise BlockContractError("compact-source-only-wrapper")
    return content[len(opening) : len(content) - len(closing)]


def _complete_outer_wrappers(
    block: TranslationBlock,
    parts: Sequence[TargetTextPart],
    *,
    layout: _MarkupLayout | None = None,
) -> tuple[_ElementDescriptor, ...]:
    """Return the nested element chain that deterministically wraps the block.

    A wrapper whose opening token is the first candidate part and whose matching
    closing token is the last has no target-language placement ambiguity.  The
    provider contract may therefore omit that ceremonial pair and let the host
    restore it around the complete translated block.  Internal elements, void
    elements, and literals remain visible and source-bound.
    """

    active_layout = layout or _layout(block)
    left = 0
    right = len(parts) - 1
    wrappers: list[_ElementDescriptor] = []
    while left < right:
        opening = parts[left]
        closing = parts[right]
        if opening.kind != "token" or closing.kind != "token":
            break
        descriptor = active_layout.token_elements.get(opening.token_id or "")
        if (
            descriptor is None
            or descriptor.open_token.id != opening.token_id
            or descriptor.close_token is None
            or descriptor.close_token.id != closing.token_id
        ):
            break
        wrappers.append(descriptor)
        left += 1
        right -= 1
    return tuple(wrappers)


def _escape_markup_text(value: str) -> str:
    # XML normalizes literal CR/CRLF before parsing. Character references retain
    # their exact value, which immutable literal slots require for round trips.
    return html.escape(value, quote=False).replace("\r", "&#13;")


def _markup_from_parts(
    block: TranslationBlock,
    parts: Sequence[TargetTextPart],
    *,
    layout: _MarkupLayout | None = None,
    literal_slots: Mapping[str, Any] | None = None,
) -> str:
    layout = layout or _layout(block)
    literal_slots = (
        literal_slots
        if literal_slots is not None
        else {slot.id: slot for slot in literal_reference_slots(block)}
    )
    rendered: list[str] = []
    for part in parts:
        if part.kind == "text":
            rendered.append(_escape_markup_text(part.text or ""))
            continue
        if part.kind == "format":
            tag = part.format_tag or ""
            rendered.append(
                f"<{tag}>" if part.format_boundary == "open" else f"</{tag}>"
            )
            continue
        if part.kind == "literal":
            slot_id = part.literal_slot_id or ""
            slot = literal_slots.get(slot_id)
            local_ref = layout.literal_refs.get(slot_id)
            if slot is None or local_ref is None:
                raise BlockContractError("compact-literal")
            visible = html.unescape(slot.source_value)
            rendered.append(
                f'<lit r="{local_ref}">{_escape_markup_text(visible)}</lit>'
            )
            continue
        descriptor = layout.token_elements.get(part.token_id or "")
        if descriptor is None:
            raise BlockContractError("compact-token")
        token = (
            descriptor.open_token
            if descriptor.open_token.id == part.token_id
            else descriptor.close_token
        )
        if token is None:
            raise BlockContractError("compact-token")
        reference = (
            f' r="{descriptor.local_ref}"' if descriptor.local_ref is not None else ""
        )
        if token.kind == "open":
            rendered.append(f"<{descriptor.tag}{reference}>")
        elif token.kind == "close":
            rendered.append(f"</{descriptor.tag}>")
        else:
            rendered.append(f"<{descriptor.tag}{reference}/>")
    content = "".join(rendered)
    if len(content.encode("utf-8")) > MAX_COMPACT_MARKUP_BYTES:
        raise BlockContractError("compact-content-size")
    return content


def _fields_from_attributes(
    block: TranslationBlock, attributes: Sequence[TargetLexicalAttribute]
) -> list[dict[str, str]]:
    values = {item.slot_id: item.value for item in attributes}
    return [
        {"name": name, "value": values.get(slot.id, "")}
        for name, slot in _field_specs(block)
    ]


def compact_source_block(
    block: TranslationBlock,
    *,
    number: int,
    implicit_outer_wrappers: bool = False,
    include_sentences: bool = False,
    locate_sentences: bool = False,
) -> dict[str, object]:
    """Expose one natural source block with packet-local references only."""

    if not isinstance(number, int) or isinstance(number, bool) or number < 1:
        raise BlockContractError("compact-block-number")
    if include_sentences and locate_sentences:
        raise BlockContractError("compact-source-sentences")
    parts, attributes = _source_candidate(block)
    if implicit_outer_wrappers:
        wrapper_count = len(_complete_outer_wrappers(block, parts))
        if wrapper_count:
            parts = parts[wrapper_count:-wrapper_count]
    result: dict[str, object] = {
        "n": number,
        "role": block.role,
        "content": _markup_from_parts(block, parts),
        "fields": _fields_from_attributes(block, attributes),
    }
    if include_sentences:
        result["sentences"] = [
            {
                "n": anchor.order,
                "text": block.projection[anchor.start : anchor.end],
            }
            for anchor in block.anchors
        ]
        if not result["sentences"]:
            raise BlockContractError("compact-source-sentences")
    if locate_sentences:
        result["sentences"] = _located_source_sentences(
            block, result, implicit_outer_wrappers=implicit_outer_wrappers
        )
    return result


def _located_source_sentences(
    block: TranslationBlock,
    row: Mapping[str, object],
    *,
    implicit_outer_wrappers: bool,
) -> list[dict[str, object]]:
    """Map native projection offsets to exact characters in the single source copy."""

    parts, _attributes = _source_candidate(block)
    layout = _layout(block)
    literal_slots = {slot.id: slot for slot in literal_reference_slots(block)}
    wrappers = (
        len(_complete_outer_wrappers(block, parts, layout=layout))
        if implicit_outer_wrappers
        else 0
    )
    fragments = [
        _markup_from_parts(
            block, (part,), layout=layout, literal_slots=literal_slots
        )
        for part in parts
    ]
    prefix = sum(map(len, fragments[:wrappers]))
    visible = "".join(fragments[wrappers : len(parts) - wrappers])
    if visible != row["content"]:
        raise BlockContractError("compact-source-locator-content")
    locations: dict[str, _SourceLocation] = {}
    slots = {slot.id: slot for slot in block.slots}
    tokens = {token.id: token for token in block.protected_inline}
    literals = set(literal_slots)
    breaks = {slot.raw_start: slot for slot in block.slots if slot.kind == "break"}
    if len(breaks) != sum(slot.kind == "break" for slot in block.slots):
        raise BlockContractError("compact-source-locator-break")
    part_index = 0
    cursor = 0
    for wire in block.wire:
        slot = slots.get(wire.ref_id) if wire.kind == "slot" else None
        token = tokens.get(wire.ref_id) if wire.kind == "token" else None
        if slot is not None and slot.kind == "attribute":
            continue
        if token is not None and token.movement_policy == "source-only":
            continue
        if (
            slot is not None
            and slot.id not in literals
            and slot.disposition != "translatable"
        ):
            raise BlockContractError("compact-source-locator-slot")
        if part_index >= len(parts):
            raise BlockContractError("compact-source-locator-wire")
        part = parts[part_index]
        fragment = fragments[part_index]
        start = cursor - prefix
        if slot is not None:
            value = " " if slot.kind == "boundary" else html.unescape(slot.source_value)
            if slot.id in literals:
                body_start = fragment.find(">") + 1
                body_end = fragment.rfind("</lit>")
                if body_start <= 0 or body_end < body_start:
                    raise BlockContractError("compact-source-locator-literal")
                offsets = _character_offsets(value)
                if body_end - body_start != offsets[-1]:
                    raise BlockContractError("compact-source-locator-literal")
                locations[slot.id] = _SourceLocation(
                    "content", start + body_start, offsets
                )
            elif part.kind == "text" and fragment == _escape_markup_text(value):
                locations[slot.id] = _SourceLocation(
                    "content", start, _character_offsets(value)
                )
            else:
                raise BlockContractError("compact-source-locator-wire")
        elif token is not None and token.kind == "void" and token.tag == "br":
            br_slot = breaks.get(token.raw_start)
            if br_slot is None or br_slot.id in locations or part.kind != "token":
                raise BlockContractError("compact-source-locator-break")
            locations[br_slot.id] = _SourceLocation(
                "content", start, (0, len(fragment)), break_tag=True
            )
        cursor += len(fragment)
        part_index += 1
    if part_index != len(parts):
        raise BlockContractError("compact-source-locator-wire")
    fields = row["fields"]
    if not isinstance(fields, list):
        raise BlockContractError("compact-source-locator-field")
    for (name, slot), field in zip(_field_specs(block), fields, strict=True):
        value = html.unescape(slot.source_value)
        if name == "content" or field != {"name": name, "value": value}:
            raise BlockContractError("compact-source-locator-field")
        locations[slot.id] = _SourceLocation(name, 0, tuple(range(len(value) + 1)))
    ordered = sorted(
        block.slots,
        key=lambda slot: (slot.projection_start, slot.projection_end, slot.id),
    )
    cursor = 0
    for slot in ordered:
        start, end = slot.projection_start, slot.projection_end
        location = locations.get(slot.id)
        if start is None or end is None or start != cursor or location is None:
            raise BlockContractError("compact-source-locator-coverage")
        expected = (
            "\n"
            if slot.kind == "break"
            else " "
            if slot.kind == "boundary"
            else html.unescape(slot.source_value)
        )
        if (
            block.projection[start:end] != expected
            or len(location.offsets) != end - start + 1
        ):
            raise BlockContractError("compact-source-locator-projection")
        cursor = end
    if cursor != len(block.projection) or not block.anchors:
        raise BlockContractError("compact-source-locator-coverage")
    sentences: list[dict[str, object]] = []
    for anchor in block.anchors:
        spans: list[dict[str, object]] = []
        reconstructed: list[str] = []
        for slot in ordered:
            assert slot.projection_start is not None and slot.projection_end is not None
            start = max(anchor.start, slot.projection_start)
            end = min(anchor.end, slot.projection_end)
            if start >= end:
                continue
            location = locations[slot.id]
            left = location.start + location.offsets[start - slot.projection_start]
            right = location.start + location.offsets[end - slot.projection_start]
            field_value = (
                row["content"]
                if location.field == "content"
                else next(
                    field["value"]
                    for field in fields
                    if field["name"] == location.field
                )
            )
            if (
                not isinstance(field_value, str)
                or not 0 <= left < right <= len(field_value)
            ):
                raise BlockContractError("compact-source-locator-range")
            span = {"field": location.field, "start": left, "end": right}
            spans.append(span)
            reconstructed.append(
                "\n"
                if location.break_tag
                else field_value[left:right]
                if location.field != "content"
                else html.unescape(field_value[left:right])
            )
        if not spans or "".join(reconstructed) != block.projection[
            anchor.start : anchor.end
        ]:
            raise BlockContractError("compact-source-locator-anchor")
        sentences.append({"n": anchor.order, "spans": spans})
    return sentences


def validate_located_source_block(
    block: TranslationBlock, row: Mapping[str, object], *, number: int
) -> None:
    """Reject altered v9 locators against the pinned native source plan."""

    expected = compact_source_block(
        block, number=number, implicit_outer_wrappers=True, locate_sentences=True
    )
    if row != expected:
        raise BlockContractError("compact-source-locator-replay")


def compact_source_context_block(block: TranslationBlock) -> dict[str, object]:
    """Expose natural source markup for analysis without target-output size limits."""

    oversized_text = any(
        slot.disposition == "translatable"
        and slot.kind != "attribute"
        and len(html.unescape(slot.source_value).encode("utf-8"))
        > MAX_TARGET_TEXT_BYTES
        for slot in block.slots
    )
    oversized_field = any(
        slot.disposition == "translatable"
        and slot.kind == "attribute"
        and len(html.unescape(slot.source_value).encode("utf-8"))
        > MAX_LEXICAL_VALUE_BYTES
        for slot in block.slots
    )
    if oversized_text or oversized_field:
        return {
            "role": block.role,
            "content": _escape_markup_text(block.projection),
            "fields": [],
        }
    try:
        compact = compact_source_block(block, number=1)
    except BlockContractError as exc:
        if exc.code != "compact-content-size":
            raise
        return {
            "role": block.role,
            "content": _escape_markup_text(block.projection),
            "fields": [],
        }
    return {
        "role": compact["role"],
        "content": compact["content"],
        "fields": compact["fields"],
    }


def compact_candidate_projection(
    block: TranslationBlock, candidate: TargetBlockCandidate
) -> dict[str, object]:
    """Project a validated durable candidate into the continuous editor wire."""

    return {
        "contract_version": COMPACT_EDITOR_CONTRACT_VERSION,
        "content": _wrap_editor_source_only(
            block,
            _markup_from_parts(block, candidate.parts),
        ),
        "fields": _fields_from_attributes(block, candidate.lexical_attributes),
    }


def compact_source_projection(block: TranslationBlock) -> dict[str, object]:
    parts, attributes = _source_candidate(block)
    return {
        "contract_version": COMPACT_EDITOR_CONTRACT_VERSION,
        "content": _wrap_editor_source_only(
            block,
            _markup_from_parts(block, parts),
        ),
        "fields": _fields_from_attributes(block, attributes),
    }


def _entity_lexemes(value: str) -> frozenset[str]:
    return frozenset(match.group(0) for match in _HTML_ENTITY_RE.finditer(value))


def _decode_model_entities(
    value: str, *, preserve: frozenset[str] = frozenset()
) -> str:
    """Decode one explicit entity layer while retaining source-literal notation."""

    return _HTML_ENTITY_RE.sub(
        lambda match: (
            match.group(0)
            if match.group(0) in preserve
            else html.unescape(match.group(0))
        ),
        value,
    )


def _append_text(
    parts: list[TargetTextPart],
    value: str | None,
    *,
    preserve_entities: frozenset[str],
) -> int:
    if not value:
        return 0
    # ``ElementTree`` has already decoded the XML layer.  Models sometimes
    # over-escape ordinary Unicode punctuation (for example, returning
    # ``&amp;lsquo;`` inside the XML fragment), which otherwise leaves the
    # literal string ``&lsquo;`` in the durable candidate and browser.  At this
    # point the value is plain semantic text, so decoding one HTML entity
    # layer is safe: target rendering will escape XML/HTML-significant
    # characters again when it reconstructs markup.
    value = _decode_model_entities(value, preserve=preserve_entities)
    if parts and parts[-1].kind == "text":
        previous = parts[-1].text or ""
        parts[-1] = TargetTextPart(kind="text", text=previous + value)
    else:
        parts.append(TargetTextPart(kind="text", text=value))
    return len(value)


def _xml_compatible_entities(content: str) -> str:
    """Translate HTML named entities to numeric XML references.

    Browser editors and models commonly emit ``&nbsp;`` even when asked for an
    XML fragment.  Numeric references preserve the character without decoding
    XML's structural entities (notably ``&lt;``) before parsing.
    """

    def replace(match: re.Match[str]) -> str:
        name = match.group("name")
        if name in _XML_ENTITIES:
            return match.group(0)
        decoded = html.entities.html5.get(f"{name};")
        if decoded is None:
            return match.group(0)
        return "".join(f"&#{ord(character)};" for character in decoded)

    return _NAMED_ENTITY_RE.sub(replace, content)


def parse_compact_fragment(content: object, *, marked: bool = False) -> ET.Element:
    """Shared bounded XML parsing for normalization and same-lease preflight."""
    if (
        not isinstance(content, str)
        or len(content.encode("utf-8")) > MAX_COMPACT_MARKUP_BYTES
    ):
        raise BlockContractError("compact-content")
    if "<!" in content or "<?" in content:
        raise BlockContractError("compact-markup-active")
    try:
        # The bounded fragment has already rejected declarations and processing
        # instructions, so ElementTree never receives a DTD or entity definition.
        root = ET.fromstring(f"<root>{_xml_compatible_entities(content)}</root>")
    except (ET.ParseError, RecursionError) as exc:
        raise BlockContractError("compact-markup-xml") from exc

    if marked and re.search(
        r"<[^>]*\sxmlns(?::[^\s=/>]+)?\s*=", content
    ):
        raise BlockContractError("sentence-marker-namespace")

    return root


def _parts_from_markup(
    block: TranslationBlock,
    content: object,
    *,
    implicit_outer_wrappers: bool = False,
    allow_target_formatting: bool = False,
    sentence_ends: list[int] | None = None,
) -> tuple[TargetTextPart, ...]:
    root = parse_compact_fragment(content, marked=sentence_ends is not None)

    layout = _layout(block)
    source_parts, _source_attributes = _source_candidate(block)
    outer_wrappers = (
        _complete_outer_wrappers(block, source_parts, layout=layout)
        if implicit_outer_wrappers
        else ()
    )
    omitted_element_ids = {
        descriptor.open_token.element_id for descriptor in outer_wrappers
    }
    active_elements = tuple(
        descriptor
        for descriptor in layout.elements
        if descriptor.open_token.element_id not in omitted_element_ids
    )
    active_referenced_elements = {
        descriptor.local_ref: descriptor
        for descriptor in active_elements
        if descriptor.local_ref is not None
    }
    unreferenced: dict[str, deque[_ElementDescriptor]] = defaultdict(deque)
    for descriptor in active_elements:
        if descriptor.local_ref is None:
            unreferenced[descriptor.tag].append(descriptor)
    literal_slots = {slot.id: slot for slot in literal_reference_slots(block)}
    used_elements: set[str] = set()
    used_literals: set[str] = set()
    parts: list[TargetTextPart] = []
    source_text_runs: list[str] = []
    current_source_run: list[str] = []
    for part in source_parts:
        if part.kind == "text":
            current_source_run.append(part.text or "")
            continue
        if current_source_run:
            source_text_runs.append("".join(current_source_run))
            current_source_run = []
    if current_source_run:
        source_text_runs.append("".join(current_source_run))
    preserve_entities = frozenset(
        entity
        for source_text in source_text_runs
        for entity in _entity_lexemes(source_text)
    )

    projection_length = 0

    def append_text(value: str | None) -> None:
        nonlocal projection_length
        projection_length += _append_text(parts, value, preserve_entities=preserve_entities)

    def element_descriptor(node: ET.Element) -> _ElementDescriptor | None:
        if not isinstance(node.tag, str) or node.tag == "root":
            raise BlockContractError("compact-markup-tag")
        if set(node.attrib) - {"r"}:
            raise BlockContractError("compact-markup-attribute")
        local_ref = node.attrib.get("r")
        if local_ref is not None:
            descriptor = active_referenced_elements.get(local_ref)
            if descriptor is None or descriptor.tag != node.tag:
                raise BlockContractError("compact-markup-reference")
        else:
            if node.attrib:
                raise BlockContractError("compact-markup-attribute")
            queue = unreferenced.get(node.tag)
            if not queue:
                if allow_target_formatting and node.tag in TARGET_LOCAL_FORMAT_TAGS:
                    return None
                raise BlockContractError("compact-markup-tag")
            descriptor = queue.popleft()
        if descriptor.open_token.element_id in used_elements:
            raise BlockContractError("compact-markup-cardinality")
        used_elements.add(descriptor.open_token.element_id)
        return descriptor

    def walk(node: ET.Element) -> None:
        nonlocal projection_length
        if node.tag == "sb" and sentence_ends is not None:
            if node.attrib or len(node) or node.text:
                raise BlockContractError("sentence-marker-shape")
            if len(sentence_ends) >= MAX_SENTENCE_MARKERS:
                raise BlockContractError("sentence-marker-limit")
            sentence_ends.append(projection_length)
            return
        if node.tag == "lit":
            if set(node.attrib) != {"r"} or list(node):
                raise BlockContractError("compact-literal-shape")
            local_ref = node.attrib["r"]
            slot_id = layout.refs_to_literals.get(local_ref)
            slot = literal_slots.get(slot_id or "")
            if slot is None or local_ref in used_literals:
                raise BlockContractError("compact-literal-reference")
            if (node.text or "") != html.unescape(slot.source_value):
                raise BlockContractError("compact-literal-value")
            used_literals.add(local_ref)
            parts.append(TargetTextPart(kind="literal", literal_slot_id=slot.id))
            projection_length += len(slot.source_value)
            return

        descriptor = element_descriptor(node)
        if descriptor is None:
            parts.append(
                TargetTextPart(
                    kind="format",
                    format_tag=node.tag,
                    format_boundary="open",
                )
            )
            append_text(node.text)
            for child in node:
                walk(child)
                append_text(child.tail)
            parts.append(
                TargetTextPart(
                    kind="format",
                    format_tag=node.tag,
                    format_boundary="close",
                )
            )
            return
        parts.append(TargetTextPart(kind="token", token_id=descriptor.open_token.id))
        if descriptor.close_token is None:
            if node.text or list(node):
                raise BlockContractError("compact-void-content")
            return
        append_text(node.text)
        for child in node:
            walk(child)
            append_text(child.tail)
        parts.append(TargetTextPart(kind="token", token_id=descriptor.close_token.id))

    try:
        append_text(root.text)
        for child in root:
            walk(child)
            append_text(child.tail)
    except RecursionError as exc:
        raise BlockContractError("compact-markup-depth") from exc

    if len(used_elements) != len(active_elements) or any(unreferenced.values()):
        raise BlockContractError("compact-markup-cardinality")
    if used_literals != set(layout.refs_to_literals):
        raise BlockContractError("compact-literal-cardinality")
    if not outer_wrappers:
        return tuple(parts)
    return (
        *(
            TargetTextPart(kind="token", token_id=item.open_token.id)
            for item in outer_wrappers
        ),
        *parts,
        *(
            TargetTextPart(kind="token", token_id=item.close_token.id)
            for item in reversed(outer_wrappers)
            if item.close_token is not None
        ),
    )


def _marked_field_value(
    value: str, *, preserve: frozenset[str], sentence_ends: list[int]
) -> str:
    """Decode a v8 attribute text fragment without persisting markup or markers."""
    if len(value.encode("utf-8")) > MAX_COMPACT_MARKUP_BYTES:
        raise BlockContractError("compact-content")
    if "<!" in value or "<?" in value:
        raise BlockContractError("sentence-marker-shape")
    try:
        root = ET.fromstring(f"<root>{_xml_compatible_entities(value)}</root>")
    except (ET.ParseError, RecursionError) as exc:
        raise BlockContractError("compact-markup-xml") from exc
    # Inspect namespace syntax only after the bounded XML parser rejects
    # malformed input, so repeated opening brackets cannot amplify regex work.
    if re.search(r"<[^>]*\sxmlns(?::[^\s=/>]+)?\s*=", value):
        raise BlockContractError("sentence-marker-namespace")
    text = _decode_model_entities(root.text or "", preserve=preserve)
    pieces = [text]
    length = len(text)
    for node in root:
        if node.tag != "sb" or node.attrib or len(node) or node.text:
            raise BlockContractError("sentence-marker-shape")
        if len(sentence_ends) >= MAX_SENTENCE_MARKERS:
            raise BlockContractError("sentence-marker-limit")
        sentence_ends.append(length)
        tail = _decode_model_entities(node.tail or "", preserve=preserve)
        pieces.append(tail)
        length += len(tail)
    return "".join(pieces)


def _attributes_from_fields(
    block: TranslationBlock, fields: object, *, sentence_ends: list[int] | None = None
) -> tuple[TargetLexicalAttribute, ...]:
    if not isinstance(fields, list):
        raise BlockContractError("compact-fields")
    specs = _field_specs(block)
    if len(fields) != len(specs):
        raise BlockContractError("compact-field-cardinality")
    attributes: list[TargetLexicalAttribute] = []
    for item, (expected_name, slot) in zip(fields, specs, strict=True):
        if (
            not isinstance(item, Mapping)
            or set(item) != {"name", "value"}
            or item.get("name") != expected_name
            or not isinstance(item.get("value"), str)
        ):
            raise BlockContractError("compact-field-shape")
        # Field values are semantic text rather than XML.  Accept the common
        # model habit of spelling Unicode characters as HTML entities without
        # persisting the entity source literally into alt text or other
        # translatable attributes.
        attributes.append(
            TargetLexicalAttribute(
                slot_id=slot.id,
                value=(
                    _marked_field_value(
                        item["value"],
                        preserve=_entity_lexemes(html.unescape(slot.source_value)),
                        sentence_ends=sentence_ends,
                    )
                    if sentence_ends is not None
                    else _decode_model_entities(
                        item["value"],
                        preserve=_entity_lexemes(html.unescape(slot.source_value)),
                    )
                ),
            )
        )
    return tuple(attributes)


def normalize_compact_result(
    *,
    plan: TranslationSourcePlan,
    plan_id: str,
    block: TranslationBlock,
    expected_number: int,
    value: Mapping[str, Any],
    implicit_outer_wrappers: bool = False,
    allow_target_formatting: bool = False,
    annotation_profile: object = None,
    notes_enabled: bool = False,
    require_notes_array: bool = False,
    sentence_ends: list[int] | None = None,
) -> TargetBlockCandidate | tuple[TargetBlockCandidate, tuple[BlockNoteCandidate, ...]]:
    """Restore one compact provider result to the strict durable candidate."""

    effective_profile = normalize_annotation_profile(annotation_profile, notes_enabled)
    notes_required = effective_profile != ANNOTATION_PROFILE_NONE or require_notes_array
    expected_keys = {"n", "content", "fields"}
    if notes_required:
        expected_keys.add("notes")
    if not isinstance(value, Mapping) or set(value) != expected_keys:
        raise BlockContractError("compact-result-shape")
    if value.get("n") != expected_number:
        raise BlockContractError("compact-result-number")
    attribute_alignment = (
        sentence_ends is not None and target_alignment_scope(block).startswith("attribute:")
    )
    candidate = TargetBlockCandidate(
        source_plan_id=plan_id,
        source_plan_hash=plan.plan_hash,
        block_id=block.id,
        structural_fingerprint=structural_fingerprint(plan, block),
        parts=_parts_from_markup(
            block,
            value.get("content"),
            implicit_outer_wrappers=implicit_outer_wrappers,
            allow_target_formatting=allow_target_formatting,
            sentence_ends=None if attribute_alignment else sentence_ends,
        ),
        lexical_attributes=_attributes_from_fields(
            block,
            value.get("fields"),
            sentence_ends=sentence_ends if attribute_alignment else None,
        ),
    )
    if not notes_required:
        return candidate
    raw_notes = value.get("notes")
    if not isinstance(raw_notes, list):
        raise BlockContractError("block-note-cardinality")
    if raw_notes and not notes_allowed_for_block(block):
        raise BlockContractError("block-note-attribute")
    notes = tuple(
        note_from_provider(block.id, item, order_index=order_index)
        for order_index, item in enumerate(raw_notes)
    )
    # Import lazily to keep compact source projection independent of target
    # rendering's source-plan implementation.
    from .target_rendering import render_target_block_fragment

    fragment = render_target_block_fragment(
        plan,
        plan_id,
        block,
        candidate,
        target_language=plan.snapshot.source_language
        if hasattr(plan.snapshot, "source_language")
        else "en",
    )
    validate_note_candidates(
        notes,
        {block.id: fragment},
        annotation_profile=effective_profile,
    )
    return candidate, notes


def normalize_editor_candidate(
    *,
    plan: TranslationSourcePlan,
    plan_id: str,
    block: TranslationBlock,
    value: Mapping[str, Any],
) -> TargetBlockCandidate:
    """Restore one owner-edited continuous block to the strict candidate."""

    if (
        not isinstance(value, Mapping)
        or set(value) != {"contract_version", "content", "fields"}
        or value.get("contract_version") != COMPACT_EDITOR_CONTRACT_VERSION
    ):
        raise BlockContractError("compact-editor-shape")
    return normalize_compact_result(
        plan=plan,
        plan_id=plan_id,
        block=block,
        expected_number=1,
        value={
            "n": 1,
            "content": _strip_editor_source_only(block, value.get("content")),
            "fields": value.get("fields"),
        },
        allow_target_formatting=True,
    )


__all__ = [
    "BLOCK_NOTE_CONTRACT_VERSION",
    "COMPACT_EDITOR_CONTRACT_VERSION",
    "MAX_COMPACT_MARKUP_BYTES",
    "compact_candidate_projection",
    "compact_result_schema",
    "compact_source_block",
    "compact_source_context_block",
    "compact_source_projection",
    "normalize_compact_result",
    "normalize_editor_candidate",
]
