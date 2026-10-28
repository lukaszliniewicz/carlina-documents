"""Project retained note markup to current publishing accessibility semantics.

This changes only an export DOM. Stored source/revisions and note identities
remain intact; EPUB types continue to identify the targets for readers.
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup, Tag


def _tokens(tag: Tag, attribute: str) -> set[str]:
    value = tag.get(attribute)
    if isinstance(value, list):
        return {str(token) for token in value}
    return set(str(value or "").split())


def _is_note(tag: Tag) -> bool:
    return bool(
        _tokens(tag, "role") & {"doc-endnote", "doc-footnote"}
        or _tokens(tag, "epub:type") & {"endnote", "footnote"}
    )


def _has_own_note_marker(note_item: Tag) -> bool:
    """Recognize an explicit marker or a declared, visibly printed label."""

    for marker in note_item.find_all(class_="footnote-marker"):
        for ancestor in marker.parents:
            if ancestor is note_item:
                return True
            if ancestor.name == "li" or _is_note(ancestor):
                break

    label = note_item.get("data-footnote-label")
    if not isinstance(label, str) or not label.strip():
        return False
    own_text: list[str] = []
    for text in note_item.strings:
        for ancestor in text.parents:
            if ancestor is note_item:
                own_text.append(str(text))
                break
            if ancestor.name in {"li", "ol", "ul", "script", "style"} or _is_note(
                ancestor
            ):
                break
    return bool(
        re.match(
            r"^" + re.escape(label.strip()) + r"[.)]?(?:\s+|$)",
            "".join(own_text).lstrip(),
        )
    )


def _normalize_marked_note_lists(collection: Tag) -> None:
    """Replace printed-marker note lists with explicit non-numbering semantics."""

    for note_list in collection.find_all(["ol", "ul"]):
        items = note_list.find_all("li", recursive=False)
        if not items or not all(
            _is_note(item) and _has_own_note_marker(item) for item in items
        ):
            continue

        note_list.name = "div"
        note_list["role"] = "list"
        for attribute in ("start", "type", "reversed"):
            note_list.attrs.pop(attribute, None)

        for item in items:
            item.name = "div"
            item["role"] = "listitem"
            item.attrs.pop("value", None)


def normalize_export_note_roles(soup: BeautifulSoup) -> None:
    """Retain note links/labels while removing obsolete or redundant roles.

    Labeled notes keep their printed markers. Explicit list/listitem roles on
    neutral wrappers provide list semantics without adding another visible
    numbering sequence. Unmarked native lists keep their own numbering.
    """

    for collection in soup.find_all(True):
        if "doc-endnotes" not in _tokens(collection, "role"):
            continue
        # Legacy Translator groups contain directly adjacent aside targets.
        # Wrap each consecutive run without moving intervening headings/prose.
        note_list: Tag | None = None
        for child in list(collection.children):
            if not isinstance(child, Tag):
                if str(child).strip():
                    note_list = None
                elif note_list is not None:
                    note_list.append(child.extract())
                continue
            if not _is_note(child):
                note_list = None
                continue
            if note_list is None:
                note_list = soup.new_tag("div", role="list")
                child.insert_before(note_list)
            item = soup.new_tag("div", role="listitem")
            item.append(child.extract())
            note_list.append(item)

    for note in soup.find_all(True):
        roles = _tokens(note, "role")
        obsolete = roles & {"doc-endnote"}
        within_collection = any(
            "doc-endnotes" in _tokens(parent, "role") for parent in note.parents
        )
        if within_collection:
            obsolete |= roles & {"doc-footnote"}
        if not obsolete:
            continue
        if not _tokens(note, "epub:type") & {"footnote", "endnote"}:
            note["epub:type"] = " ".join(
                sorted(_tokens(note, "epub:type") | {"footnote"})
            )
        remaining = roles - obsolete
        if remaining:
            note["role"] = " ".join(sorted(remaining))
        else:
            del note["role"]

    for collection in soup.find_all(True):
        if "doc-endnotes" in _tokens(collection, "role"):
            _normalize_marked_note_lists(collection)
