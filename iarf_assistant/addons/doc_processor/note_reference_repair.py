"""Explicit insertion of one witnessed missing reference to an existing note.

The review service owns revision fencing, canonical validation, publication and
encrypted undo. Generic HTML edits retain their stricter reference invariants.
"""

from __future__ import annotations

import hashlib
import json

from bs4 import NavigableString, Tag

from .document_review import DocumentReview, _is_noteref


def _unprotected_runs(review: DocumentReview, tag: Tag):
    for run in review._replace_text_runs(tag):
        segment = []
        for node in run:
            if any(parent.name in {"code", "pre"} for parent in node.parents):
                if segment:
                    yield segment
                    segment = []
            else:
                segment.append(node)
        if segment:
            yield segment


def repair_note_reference(
    review: DocumentReview,
    *,
    element_id: str,
    note_element_id: str,
    after_text: str,
    label: str,
) -> tuple[str, dict]:
    """Insert after one unique, case-sensitive literal in unprotected body text."""
    if (
        not isinstance(after_text, str)
        or not after_text.strip()
        or len(after_text) > 1000
    ):
        raise ValueError("after_text must contain 1-1000 characters of source text.")
    if (
        not isinstance(label, str)
        or not label.strip()
        or len(label) > 64
        or not label.isprintable()
    ):
        raise ValueError("label must contain 1-64 printable characters.")
    # Work on an independent parse, leaving the caller's reviewed snapshot intact.
    successor = DocumentReview(str(review._soup))
    target = successor._require_node(element_id)
    note = successor._require_node(note_element_id)
    index = successor._build_note_index()
    if not any(body is note.tag for body in index["bodies"]):
        raise ValueError("note_element_id must identify an existing note body.")
    note_id = str(note.tag.get("id") or "")
    if not note_id or len(successor._soup.find_all(id=note_id)) != 1:
        raise ValueError("The note must have one unique HTML identifier.")
    if (
        target.tag.name
        not in {"p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "td", "th", "figcaption"}
        or successor._is_note_tag(target.tag)
        or not successor._in_page_scope(target.tag)
        or not successor._is_leaf_review_node(target)
    ):
        raise ValueError("The insertion element must be a body text block.")
    if any(
        parent.name in {"a", "script", "style", "head", "nav", "code", "pre"}
        or parent.has_attr("hidden")
        or str(parent.get("aria-hidden") or "").lower() == "true"
        or _is_noteref(parent)
        for parent in (target.tag, *target.tag.parents)
        if isinstance(parent, Tag)
    ):
        raise ValueError("The insertion element is in a protected context.")

    matches = []
    for run in _unprotected_runs(successor, target.tag):
        text = "".join(map(str, run))
        start = 0
        while (found := text.find(after_text, start)) >= 0:
            end = found + len(after_text)
            offset = 0
            for node in run:
                if offset < end <= offset + len(str(node)):
                    matches.append((node, end - offset))
                    break
                offset += len(str(node))
            if len(matches) > 1:
                raise ValueError(
                    "after_text must match exactly once in unprotected text."
                )
            start = found + 1
    if len(matches) != 1:
        raise ValueError("after_text must match exactly once in unprotected text.")
    node, offset = matches[0]
    if offset == len(str(node)):
        cursor = node
        while cursor is not target.tag and cursor.next_sibling is None:
            cursor = cursor.parent
        following = cursor.next_sibling if cursor is not target.tag else None
        if (
            isinstance(following, Tag)
            and _is_noteref(following)
            and following.get("href") == "#" + note_id
        ):
            raise ValueError(
                "A reference to this note already follows the selected text."
            )

    original_ids = successor._id_multiset(successor._soup)
    original_refs = successor._noteref_multiset(successor._soup)
    original_pages = successor._pagebreak_id_multiset(successor._soup)
    original_images = successor._image_src_multiset(successor._soup)
    identity = json.dumps(
        [str(review._soup), element_id, note_element_id, after_text, label]
    )
    reference_id = (
        "note-ref-repair-" + hashlib.sha256(identity.encode()).hexdigest()[:24]
    )
    if reference_id in original_ids:
        raise ValueError("The reference identifier already exists.")
    anchor = successor._soup.new_tag("a", href="#" + note_id)
    anchor["id"] = reference_id
    anchor["role"] = "doc-noteref"
    anchor["class"] = ["footnote-ref"]
    if note.tag.get("data-footnote-scope"):
        anchor["data-footnote-scope"] = note.tag["data-footnote-scope"]
    anchor.string = label
    before, after = str(node)[:offset], str(node)[offset:]
    node.replace_with(NavigableString(before), anchor, NavigableString(after))

    # Only the explicitly requested reference and its new ID may be added.
    expected_ids = original_ids.copy()
    expected_ids[reference_id] += 1
    expected_refs = original_refs.copy()
    expected_refs[(reference_id, "#" + note_id)] += 1
    if (
        successor._id_multiset(successor._soup) != expected_ids
        or successor._noteref_multiset(successor._soup) != expected_refs
        or successor._pagebreak_id_multiset(successor._soup) != original_pages
        or successor._image_src_multiset(successor._soup) != original_images
    ):
        raise ValueError("Note repair would change unrelated protected content.")
    return str(successor._soup), {
        "element_id": element_id,
        "note_element_id": note_element_id,
        "reference_id": reference_id,
        "target_id": note_id,
        "label": label,
        "reference_page_numbers": list(target.page_numbers),
    }
