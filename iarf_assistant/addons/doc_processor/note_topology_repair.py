"""Explicit, revision-local repair of source note relationships.

This module never guesses a destination from a number or moves note prose.
The service supplies owner/revision fences, publication and encrypted undo.
"""

from __future__ import annotations

import hashlib
import json
import re

from bs4 import Tag

from carlina.core.semantic_html import SAFE_TOKEN_RE

from .document_review import (
    MAX_NOTE_RESPONSE_CHARS,
    DocumentReview,
    _is_note_body,
    _is_noteref,
    _pagination,
)

MAX_OPERATIONS = 50
_BODY_TAGS = {"p", "li", "td", "th", "figcaption", "h1", "h2", "h3", "h4", "h5", "h6"}
_LABEL = re.compile(r"(?:[^\W_]|[*†‡§#]){1,8}")
_SCOPE = "data-footnote-scope"


def _protected(tag: Tag, *, allow_reference: bool = False) -> bool:
    return any(
        parent.name in {"script", "style", "head", "nav", "code", "pre"}
        or parent.has_attr("hidden")
        or str(parent.get("aria-hidden") or "").lower() == "true"
        or (
            parent.name == "a"
            and not (allow_reference and parent is tag and _is_noteref(tag))
        )
        for parent in (tag, *tag.parents)
        if isinstance(parent, Tag)
    )


def _markers(tag: Tag) -> list[Tag]:
    return [
        child
        for child in tag.find_all(["a", "sup"])
        if not _protected(child, allow_reference=True)
        and (
            (child.name == "a" and _is_noteref(child))
            or (
                child.name == "sup"
                and child.find(True) is None
                and _LABEL.fullmatch(child.get_text(strip=True))
            )
        )
    ]


def _body(review: DocumentReview, element_id: str):
    node = review._require_node(element_id)
    if (
        node.tag.name not in _BODY_TAGS
        or not review._is_leaf_review_node(node)
        or review._is_note_tag(node.tag)
        or not review._in_page_scope(node.tag)
        or _protected(node.tag)
    ):
        raise ValueError(
            "The reference element must be an unprotected body text block."
        )
    return node


def reference_candidates(
    review: DocumentReview, *, offset: int = 0, limit: int = 50
) -> dict:
    """Expose exact marker positions; bare superscripts are candidates, not diagnoses."""
    offset, limit = _pagination(offset, limit, 50)
    paths = review._heading_paths()
    candidates = []
    for node in review._nodes:
        try:
            _body(review, node.element_id)
        except ValueError:
            continue
        for marker_index, marker in enumerate(_markers(node.tag)):
            candidates.append((node, marker_index, marker))
    items = []
    response_size = 2048
    for node, marker_index, marker in candidates[offset : offset + limit]:
        item = {
            "element_id": node.element_id,
            "marker_index": marker_index,
            "kind": "reference" if _is_noteref(marker) else "superscript_candidate",
            "label": marker.get_text("", strip=True)[:64],
            "label_truncated": len(marker.get_text("", strip=True)) > 64,
            "href": str(marker.get("href"))[:500] if marker.has_attr("href") else None,
            "html_id": review._bounded_attribute(marker, "id"),
            "href_truncated": len(str(marker.get("href") or "")) > 500,
            "scope_id": str(marker.get(_SCOPE) or "")[:128] or None,
            "page_numbers": list(review._page_numbers_for(marker)),
            "heading_path": paths.get(node.element_id, []),
        }
        item_size = len(json.dumps(item, ensure_ascii=False))
        if response_size + item_size > MAX_NOTE_RESPONSE_CHARS:
            break
        response_size += item_size
        items.append(item)
    return {
        "items": items,
        "total_references_and_candidates": len(candidates),
        "next_offset": offset + len(items)
        if offset + len(items) < len(candidates)
        else None,
    }


def _plain_label(value) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 64
        or not value.isprintable()
    ):
        raise ValueError("A note label must contain 1-64 printable characters.")
    return value


def _scope(value) -> str:
    if not isinstance(value, str) or not SAFE_TOKEN_RE.fullmatch(value):
        raise ValueError(
            "scope_id must contain 1-128 letters, digits or ._:- characters."
        )
    return value


def repair_note_topology(
    review: DocumentReview, operations: list[dict]
) -> tuple[str, list[dict]]:
    """Promote witnessed bodies and link exact markers in one all-or-nothing batch."""
    if not isinstance(operations, list) or not 1 <= len(operations) <= MAX_OPERATIONS:
        raise ValueError("Supply between 1 and 50 note repair operations.")
    successor = DocumentReview(str(review._soup))
    soup = successor._soup
    original_ids = successor._id_multiset(soup)
    expected_ids = original_ids.copy()
    expected_refs = successor._noteref_multiset(soup)
    original_pages = successor._pagebreak_id_multiset(soup)
    original_images = successor._image_src_multiset(soup)
    seed = hashlib.sha256(str(review._soup).encode()).hexdigest()
    touched = set()
    results = []

    def identifier(tag: Tag, element_id: str, kind: str) -> str:
        existing = str(tag.get("id") or "")
        if existing:
            if len(soup.find_all(id=existing)) != 1:
                raise ValueError(
                    "A repaired element must have a unique HTML identifier."
                )
            return existing
        value = (
            "note-repair-"
            + hashlib.sha256(f"{seed}:{kind}:{element_id}".encode()).hexdigest()[:24]
        )
        if soup.find(id=value) is not None:
            raise ValueError("The repair identifier already exists.")
        tag["id"] = value
        expected_ids[value] += 1
        return value

    # Resolve every marker against the original indexing, before promotions can
    # turn an ordinary block into a note. Operation order cannot shift indices.
    prepared = []
    allowed = {
        "promote_note": {"action", "element_id", "label", "scope_id"},
        "set_note_scope": {"action", "element_id", "scope_id"},
        "link_reference": {
            "action",
            "element_id",
            "marker_index",
            "note_element_id",
            "expected_label",
            "expected_href",
        },
    }
    for operation in operations:
        if (
            not isinstance(operation, dict)
            or not isinstance(operation.get("action"), str)
            or operation["action"] not in allowed
        ):
            raise ValueError("Unknown note repair operation.")
        action = operation["action"]
        if set(operation) != allowed[action]:
            raise ValueError("The note repair operation has missing or extra fields.")
        node = successor._require_node(operation["element_id"])
        marker = None
        if action == "link_reference":
            node = _body(successor, operation["element_id"])
            markers = _markers(node.tag)
            index = operation["marker_index"]
            if type(index) is not int or not 0 <= index < len(markers):
                raise ValueError("Unknown reference marker index.")
            marker = markers[index]
            if (
                marker.get_text("", strip=True)
                != _plain_label(operation["expected_label"])
                or marker.get("href") != operation["expected_href"]
            ):
                raise ValueError("The reference witness does not match.")
            key = ("marker", id(marker))
        else:
            key = ("note", id(node.tag))
            _scope(operation["scope_id"])
        if key in touched:
            raise ValueError("The same note or reference is repaired more than once.")
        touched.add(key)
        prepared.append((operation, node, marker))

    for operation, node, _ in prepared:
        action = operation["action"]
        if action == "link_reference":
            continue
        tag = node.tag
        if _protected(tag):
            raise ValueError("The note body is in a protected context.")
        if action == "promote_note":
            if tag.name not in {
                "li",
                "p",
                "aside",
            } or not successor._is_leaf_review_node(node):
                raise ValueError(
                    "Promote one leaf list item, paragraph or aside at a time."
                )
            if (
                _is_note_body(tag)
                or any(
                    _is_note_body(parent)
                    for parent in tag.parents
                    if isinstance(parent, Tag)
                )
                or tag.find(_is_noteref) is not None
            ):
                raise ValueError(
                    "The promotion target is already a note or contains note references."
                )
            tag["role"] = "doc-endnote"
            tag["epub:type"] = "endnote"
            tag["data-footnote-label"] = _plain_label(operation["label"])
        elif not _is_note_body(tag):
            raise ValueError("set_note_scope requires an existing semantic note body.")
        note_id = identifier(tag, node.element_id, "note")
        tag[_SCOPE] = operation["scope_id"]
        results.append(
            {
                "action": action,
                "element_id": node.element_id,
                "target_id": note_id,
                "scope_id": operation["scope_id"],
            }
        )

    # Rebuild the note index after explicit promotions, retaining node identities.
    successor._note_index = None
    for operation, node, marker in prepared:
        if operation["action"] != "link_reference":
            continue
        assert marker is not None
        _body(successor, node.element_id)
        note = successor._require_node(operation["note_element_id"]).tag
        if not _is_note_body(note) or _protected(note):
            raise ValueError(
                "The destination must be an unprotected semantic note body."
            )
        note_id = identifier(note, operation["note_element_id"], "note")
        was_reference = _is_noteref(marker)
        old_href = str(marker.get("href") or "") if was_reference else None
        old_id = str(marker.get("id") or "")
        if was_reference:
            expected_refs[(old_id, old_href)] -= 1
        ref_id = identifier(
            marker, f"{node.element_id}:{operation['marker_index']}", "reference"
        )
        backlinks = []
        if old_id and old_href != "#" + note_id:
            old_target_id = successor._local_fragment(old_href)
            old_note = soup.find(id=old_target_id) if old_target_id else None
            if isinstance(old_note, Tag) and _is_note_body(old_note):
                backlinks = list(old_note.find_all("a", href="#" + old_id))
        if not was_reference:
            # Keep the superscript and its identity; introduce only its anchor.
            anchor = soup.new_tag("a")
            for child in list(marker.contents):
                anchor.append(child.extract())
            marker.attrs.pop("id", None)
            remaining_classes = [
                c for c in marker.get("class", []) if c != "footnote-unresolved"
            ]
            if remaining_classes:
                marker["class"] = remaining_classes
            else:
                marker.attrs.pop("class", None)
            marker.append(anchor)
            marker = anchor
        marker["id"] = ref_id
        marker["href"] = "#" + note_id
        marker["role"] = "doc-noteref"
        marker["epub:type"] = "noteref"
        marker["class"] = list(
            dict.fromkeys([*marker.get("class", []), "footnote-ref"])
        )
        if note.get(_SCOPE):
            marker[_SCOPE] = note[_SCOPE]
        else:
            marker.attrs.pop(_SCOPE, None)
        for backlink in backlinks:
            note.append(backlink.extract())
        expected_refs[(ref_id, "#" + note_id)] += 1
        results.append(
            {
                "action": "link_reference",
                "element_id": node.element_id,
                "marker_index": operation["marker_index"],
                "reference_id": ref_id,
                "previous_href": old_href,
                "href": "#" + note_id,
                "note_element_id": operation["note_element_id"],
                "backlinks_moved": len(backlinks),
            }
        )
    if (
        successor._id_multiset(soup) != +expected_ids
        or successor._noteref_multiset(soup) != +expected_refs
        or successor._pagebreak_id_multiset(soup) != original_pages
        or successor._image_src_multiset(soup) != original_images
    ):
        raise ValueError("Note repair would change unrelated protected content.")
    return str(soup), results
