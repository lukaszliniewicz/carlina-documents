"""Pure parsing and validation for Translator target XML output."""

from __future__ import annotations

import re
from typing import Literal, NotRequired, TypedDict, cast
from urllib.parse import urlparse

from bs4 import BeautifulSoup, Tag
from bs4.element import NavigableString

from carlina.core.semantic_html import (
    SEMANTIC_GLOBAL_ATTRIBUTES,
    SEMANTIC_TAG_ATTRIBUTES,
)

TRANSLATABLE_INLINE_TAGS = frozenset(
    {
        "a",
        "abbr",
        "b",
        "br",
        "cite",
        "del",
        "em",
        "i",
        "ins",
        "kbd",
        "mark",
        "q",
        "s",
        "samp",
        "small",
        "span",
        "strong",
        "sub",
        "sup",
        "time",
        "u",
        "var",
        "wbr",
    }
)
TRANSLATABLE_INLINE_GLOBAL_ATTRIBUTES = SEMANTIC_GLOBAL_ATTRIBUTES
TRANSLATABLE_INLINE_TAG_ATTRIBUTES = {
    name: attrs
    for name, attrs in SEMANTIC_TAG_ATTRIBUTES.items()
    if name in {"a", "time"}
}

MAX_TARGET_TEXT_LENGTH = 20_000
MAX_TARGET_NOTE_LENGTH = 10_000
MAX_TRANSLATOR_NOTE_ANCHOR_LENGTH = 160
MAX_TRANSLATOR_NOTE_BODY_LENGTH = 800
MAX_TRANSLATOR_NOTES_PER_BATCH = 3

_SAFE_INLINE_TAGS = TRANSLATABLE_INLINE_TAGS
_SAFE_INLINE_GLOBAL_ATTRIBUTES = TRANSLATABLE_INLINE_GLOBAL_ATTRIBUTES
_SAFE_INLINE_ATTRIBUTES = TRANSLATABLE_INLINE_TAG_ATTRIBUTES
_SAFE_LINK_SCHEMES = frozenset({"http", "https", "mailto"})
_SAFE_ATTRIBUTE_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_SAFE_LANG_RE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8}){0,3}$")
_MARKDOWN_LINK_RE = re.compile(r"\[[^\]\r\n]{1,800}\]\([^\)\r\n]{1,2000}\)")
_NOTE_MARKUP_RE = re.compile(r"<[^>]*>")
_NOTE_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


class TranslatorNote(TypedDict):
    anchor: str
    body: str


class ParsedTarget(TypedDict):
    text: str
    note: str | None
    needs_review: bool
    translator_note: NotRequired[TranslatorNote]


type InlineAttributeSignature = str | tuple[str, ...]
type InlineNodeSignature = (
    tuple[Literal["text"]]
    | tuple[
        Literal["tag"],
        str,
        tuple[tuple[str, InlineAttributeSignature], ...],
        tuple[InlineNodeSignature, ...],
    ]
)
type InlineMarkupSignature = tuple[InlineNodeSignature, ...]


def validate_target_output(
    content: str,
    valid_ids: list[str],
    *,
    source_markup_by_id: dict[str, str] | None = None,
    translator_notes_enabled: bool = False,
) -> tuple[dict[str, ParsedTarget], list[str], list[str]]:
    soup = BeautifulSoup(f"<root>{content or ''}</root>", "html.parser")
    valid: set[str] = set(valid_ids)
    seen: set[str] = set()
    parsed: dict[str, ParsedTarget] = {}
    errors: list[str] = []
    root = soup.find("root")
    if root is None:
        return {}, list(valid_ids), ["missing output root"]
    for child in root.contents:
        if isinstance(child, NavigableString) and not child.strip():
            continue
        if getattr(child, "name", None) != "target":
            errors.append("output contains non-target top-level content")
            return {}, list(valid_ids), errors
    for target in soup.find_all("target"):
        raw_id = str(target.get("id") or "").strip()
        if not raw_id or raw_id not in valid:
            errors.append(f"invalid target id {raw_id or '<missing>'}")
            continue
        if raw_id in seen:
            errors.append(f"duplicate target id {raw_id}")
            continue
        if set(target.attrs) - {"id", "needs_review"}:
            errors.append(f"unsafe target attributes for {raw_id}")
            continue
        all_translator_notes = target.find_all("translator_note")
        if all_translator_notes and not translator_notes_enabled:
            errors.append(f"translator_note is disabled for {raw_id}")
            continue
        needs_review_value = target.get("needs_review")
        if needs_review_value is not None and str(needs_review_value) not in {
            "true",
            "false",
        }:
            errors.append(f"invalid needs_review attribute for {raw_id}")
            continue
        children = [
            child
            for child in target.contents
            if not isinstance(child, NavigableString) or child.strip()
        ]
        allowed_target_children = {"text", "note"}
        if translator_notes_enabled:
            allowed_target_children.add("translator_note")
        if any(
            getattr(child, "name", None) not in allowed_target_children
            for child in children
        ):
            errors.append(f"invalid target wrapper for {raw_id}")
            continue
        text_nodes = [
            child for child in children if getattr(child, "name", None) == "text"
        ]
        note_nodes = [
            child for child in children if getattr(child, "name", None) == "note"
        ]
        translator_note_nodes = [
            child
            for child in children
            if getattr(child, "name", None) == "translator_note"
        ]
        if (
            len(text_nodes) != 1
            or len(note_nodes) > 1
            or len(translator_note_nodes) > 1
            or len(translator_note_nodes) != len(all_translator_notes)
        ):
            errors.append(f"invalid target shape for {raw_id}")
            continue
        text_node = _tag_node(text_nodes[0])
        note_node = _tag_node(note_nodes[0]) if note_nodes else None
        translator_note_node = (
            _tag_node(translator_note_nodes[0]) if translator_note_nodes else None
        )
        payload = text_node.decode_contents().strip() if text_node else ""
        payload_soup = BeautifulSoup(payload, "html.parser")
        note = note_node.get_text(" ", strip=True) if note_node else None
        if (
            not payload.strip()
            or len(payload) > MAX_TARGET_TEXT_LENGTH
            or (note is not None and len(note) > MAX_TARGET_NOTE_LENGTH)
            or not _safe_inline_markup(payload_soup)
        ):
            errors.append(f"unsafe or empty target {raw_id}")
            continue
        if source_markup_by_id is not None and _inline_markup_signature(
            payload_soup
        ) != _inline_markup_signature(
            BeautifulSoup(source_markup_by_id.get(raw_id, ""), "html.parser")
        ):
            errors.append(f"inline structure mismatch for {raw_id}")
            continue
        translator_note = None
        if translator_note_node is not None:
            translator_note = _parse_translator_note(
                translator_note_node,
                payload_soup,
                raw_id,
                errors,
            )
            if translator_note is None:
                continue
        seen.add(raw_id)
        parsed_value: ParsedTarget = {
            "text": payload_soup.decode_contents(),
            "note": note,
            "needs_review": needs_review_value == "true",
        }
        if translator_note is not None:
            parsed_value["translator_note"] = translator_note
        parsed[raw_id] = parsed_value
    missing = [item for item in valid_ids if item not in seen]
    if translator_notes_enabled:
        note_count = sum(bool(item.get("translator_note")) for item in parsed.values())
        if note_count > MAX_TRANSLATOR_NOTES_PER_BATCH:
            errors.append(
                "a translation batch may contain at most "
                f"{MAX_TRANSLATOR_NOTES_PER_BATCH} translator_note elements"
            )
    return parsed, missing, errors


def _tag_node(node: object) -> Tag | None:
    """Narrow a BeautifulSoup child without accepting string-like nodes as tags."""

    return node if isinstance(node, Tag) else None


def _plain_translator_note_value(node: Tag, *, maximum: int) -> str | None:
    """Return one bounded plain-text note field, rejecting markup and controls."""

    if any(isinstance(child, Tag) for child in node.children):
        return None
    value = "".join(str(child) for child in node.children).strip()
    if (
        not value
        or len(value) > maximum
        or _NOTE_CONTROL_RE.search(value)
        or _MARKDOWN_LINK_RE.search(value)
        or _NOTE_MARKUP_RE.search(value)
    ):
        return None
    return value


def _parse_translator_note(
    node: Tag,
    payload_soup: BeautifulSoup,
    target_id: str,
    errors: list[str],
) -> TranslatorNote | None:
    children = [
        child
        for child in node.contents
        if not isinstance(child, NavigableString) or child.strip()
    ]
    if (
        len(children) != 2
        or {getattr(child, "name", None) for child in children}
        != {
            "anchor",
            "body",
        }
        or node.attrs
    ):
        errors.append(f"invalid translator_note shape for {target_id}")
        return None
    anchor_node = _tag_node(
        next(child for child in children if getattr(child, "name", None) == "anchor")
    )
    body_node = _tag_node(
        next(child for child in children if getattr(child, "name", None) == "body")
    )
    if anchor_node is None or body_node is None or anchor_node.attrs or body_node.attrs:
        errors.append(f"invalid translator_note shape for {target_id}")
        return None
    anchor = _plain_translator_note_value(
        anchor_node,
        maximum=MAX_TRANSLATOR_NOTE_ANCHOR_LENGTH,
    )
    body = _plain_translator_note_value(
        body_node,
        maximum=MAX_TRANSLATOR_NOTE_BODY_LENGTH,
    )
    if anchor is None or body is None:
        errors.append(f"invalid translator_note text for {target_id}")
        return None
    matching_nodes = [
        text_node
        for text_node in payload_soup.find_all(string=True)
        if anchor in str(text_node)
    ]
    if len(matching_nodes) != 1 or str(matching_nodes[0]).count(anchor) != 1:
        errors.append(
            f"translator_note anchor does not occur exactly once for {target_id}"
        )
        return None
    return {"anchor": anchor, "body": body}


def _safe_inline_markup(soup: BeautifulSoup) -> bool:
    """Reject active/event/style markup and normalize the safe inline subset."""
    for tag in soup.find_all(True):
        if tag.name not in _SAFE_INLINE_TAGS:
            return False
        allowed = _SAFE_INLINE_GLOBAL_ATTRIBUTES | _SAFE_INLINE_ATTRIBUTES.get(
            tag.name, frozenset()
        )
        for name, value in tuple(tag.attrs.items()):
            normalized = str(name).lower()
            if (
                normalized.startswith("on")
                or normalized == "style"
                or normalized not in allowed
            ):
                return False
            scalar = _inline_attribute_value(value)
            if normalized == "hidden":
                if scalar.casefold() not in {"", "hidden"}:
                    return False
                continue
            if (
                not scalar
                or len(scalar) > (4096 if normalized == "href" else 512)
                or _SAFE_ATTRIBUTE_CONTROL_RE.search(scalar)
            ):
                return False
            if normalized in {"lang", "xml:lang"} and not _SAFE_LANG_RE.fullmatch(
                scalar
            ):
                return False
            if tag.name == "a" and normalized == "href":
                if "\\" in scalar or scalar.startswith("//"):
                    return False
                parsed = urlparse(scalar)
                if parsed.scheme:
                    if parsed.scheme.lower() not in _SAFE_LINK_SCHEMES:
                        return False
                elif parsed.netloc:  # protocol-relative links inherit an unsafe scheme.
                    return False
    return True


def _inline_attribute_value(value: object) -> str:
    if isinstance(value, (list, tuple)):
        items = cast(list[object] | tuple[object, ...], value)
        return " ".join(str(item) for item in items).strip()
    return str(value or "").strip()


def _inline_attribute_signature(value: object) -> InlineAttributeSignature:
    if isinstance(value, (list, tuple)):
        items = cast(list[object] | tuple[object, ...], value)
        return tuple(sorted(str(item) for item in items))
    return str(value)


def _inline_markup_signature(soup: BeautifulSoup) -> InlineMarkupSignature:
    """Return exact tag/attribute topology plus every prose-node position."""

    def node_signature(node: object) -> InlineNodeSignature | None:
        if isinstance(node, NavigableString):
            # Content and length may change in translation, but the text must
            # remain in the same parent and sibling position. This protects
            # link, emphasis, and EPUB footnote scope.
            return ("text",) if str(node) else None
        if not isinstance(node, Tag):
            return None
        attributes = tuple(
            sorted(
                (
                    str(name).lower(),
                    _inline_attribute_signature(value),
                )
                for name, value in node.attrs.items()
            )
        )
        children = tuple(
            signature
            for child in node.children
            if (signature := node_signature(child)) is not None
        )
        return "tag", str(node.name), attributes, children

    return tuple(
        signature
        for child in soup.contents
        if (signature := node_signature(child)) is not None
    )


def parse_target_output(
    content: str,
    valid_ids: list[str],
    *,
    source_markup_by_id: dict[str, str] | None = None,
    translator_notes_enabled: bool = False,
) -> tuple[dict[str, ParsedTarget], list[str]]:
    parsed, missing, errors = validate_target_output(
        content,
        valid_ids,
        source_markup_by_id=source_markup_by_id,
        translator_notes_enabled=translator_notes_enabled,
    )
    return ({}, list(valid_ids)) if errors else (parsed, missing)
