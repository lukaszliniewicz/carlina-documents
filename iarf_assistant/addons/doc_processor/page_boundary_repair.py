"""Conservative, explicitly selected repairs at witnessed physical page breaks.

This is source review, not OCR or translation. Suggestions are not proof that a
printed hyphen is discretionary. Nothing is applied without a selected action;
revision fencing, canonical validation and encrypted undo belong to the caller.
"""
from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass
from typing import Any

from bs4 import Comment, NavigableString, Tag

from .document_review import DocumentReview, _is_note_body, _is_noteref, _is_page_marker

_ACTIONS = frozenset({"join_with_space", "dehyphenate", "keep_hyphen"})
_PROTECTED = frozenset({
    "aside", "blockquote", "pre", "code", "table", "td", "th", "li", "ul", "ol",
    "figure", "figcaption", "header", "footer", "nav", "head", "script", "style",
})
_PROTECTED_CLASSES = frozenset({
    "page_header", "page_footer", "footnote", "endnote", "poetry", "verse", "stanza",
})


def _text_nodes(tag: Tag) -> list[NavigableString]:
    result = []
    for node in tag.descendants:
        if not isinstance(node, NavigableString) or isinstance(node, Comment):
            continue
        if any(
            _is_page_marker(parent) or _is_noteref(parent)
            or parent.name in {"script", "style"} or parent.has_attr("hidden")
            for parent in node.parents if isinstance(parent, Tag) and parent is not tag
        ):
            continue
        result.append(node)
    return result


def _text(tag: Tag) -> str:
    return "".join(map(str, _text_nodes(tag)))


def _sibling(tag: Tag, *, previous: bool) -> Tag | None:
    node = tag.previous_sibling if previous else tag.next_sibling
    while isinstance(node, NavigableString) and not isinstance(node, Comment) and not str(node).strip():
        node = node.previous_sibling if previous else node.next_sibling
    return node if isinstance(node, Tag) and node.name == "p" else None


def _protected(tag: Tag) -> bool:
    return any(
        parent.name in _PROTECTED or _is_note_body(parent)
        or set(parent.get("class") or ()) & _PROTECTED_CLASSES
        or parent.has_attr("hidden")
        for parent in (tag, *tag.parents) if isinstance(parent, Tag)
    ) or tag.find(["br", "img", "table", "pre"]) is not None


def _pair(marker: Tag) -> tuple[Tag, Tag] | None:
    # Block-level page markers cannot be put inside a paragraph unchanged.
    if marker.name != "span":
        return None
    parent = marker.parent
    if isinstance(parent, Tag) and parent.name == "p":
        contents = list(parent.descendants)
        marker_index = next(i for i, node in enumerate(contents) if node is marker)
        text_nodes = _text_nodes(parent)
        positions = {id(node): i for i, node in enumerate(contents)}
        before = any(str(node).strip() and positions[id(node)] < marker_index for node in text_nodes)
        after = any(str(node).strip() and positions[id(node)] > marker_index for node in text_nodes)
        if not before and after:
            left = _sibling(parent, previous=True)
            return (left, parent) if left is not None else None
        if before and not after:
            right = _sibling(parent, previous=False)
            return (parent, right) if right is not None else None
        return None  # Already joined, or no text on either side.
    left, right = _sibling(marker, previous=True), _sibling(marker, previous=False)
    return (left, right) if left is not None and right is not None else None


@dataclass
class _Candidate:
    identifier: str
    page: int
    marker: Tag
    left: Tag
    right: Tag
    action: str
    reason: str
    left_text: str
    right_text: str

    def public(self) -> dict[str, Any]:
        left, right = self.left_text[-180:].strip(), self.right_text[:180].strip()
        joined = (left[:-1] + right) if self.action == "dehyphenate" else left + " " + right
        return {
            "id": self.identifier, "page_number": self.page,
            "suggested_action": self.action, "reason": self.reason,
            "left": left, "right": right, "preview": joined,
            "requires_review": True,
        }


def _trim(tag: Tag, *, trailing: bool) -> None:
    nodes = _text_nodes(tag)
    for node in reversed(nodes) if trailing else nodes:
        original = str(node)
        changed = original.rstrip() if trailing else original.lstrip()
        if changed != original:
            node.replace_with(NavigableString(changed))
        if changed:
            break


def repair_page_boundaries(
    review: DocumentReview, *, decisions: list[dict[str, str]] | None = None,
    page_numbers: list[int] | None = None, offset: int = 0, limit: int = 50,
) -> tuple[str | None, dict[str, Any]]:
    """Scan once, then apply at most 50 reviewed joins as one reversible edit.

    A ``None`` HTML result means no edit: preserve the exact original bytes.
    Only adjacent same-context paragraphs qualify. Headings, notes, lists,
    quotations, figures, different attributes and intervening matter are not
    crossed. The physical marker and both paragraphs' target IDs survive.
    """
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError("Boundary pagination is invalid.")
    if page_numbers is not None and (
        not isinstance(page_numbers, list) or not 1 <= len(page_numbers) <= 10000
        or any(type(page) is not int or page < 1 for page in page_numbers)
    ):
        raise ValueError("Select 1-10000 positive physical page numbers.")
    selected: dict[str, str] = {}
    if decisions is not None:
        if not isinstance(decisions, list) or not 1 <= len(decisions) <= 50:
            raise ValueError("Select 1-50 boundary decisions.")
        for decision in decisions:
            if (
                not isinstance(decision, dict) or set(decision) != {"id", "action"}
                or not isinstance(decision["id"], str)
                or len(decision["id"]) != 27 or not decision["id"].startswith("pb-")
                or not isinstance(decision["action"], str) or decision["action"] not in _ACTIONS
                or decision["id"] in selected
            ):
                raise ValueError("Boundary decisions require distinct candidate IDs and valid actions.")
            selected[decision["id"]] = decision["action"]
    pages = set(page_numbers) if page_numbers is not None else None
    candidates = []
    skipped: Counter[str] = Counter()
    for marker in review._markers:
        page = marker.page_number
        if page is None or (pages is not None and page not in pages):
            continue
        pair = _pair(marker.tag)
        if pair is None:
            skipped["not_adjacent_paragraphs_or_already_joined"] += 1
            continue
        left, right = pair
        if left.parent is not right.parent or _protected(left) or _protected(right):
            skipped["protected_structure"] += 1
            continue
        if (
            {k: v for k, v in left.attrs.items() if k != "id"}
            != {k: v for k, v in right.attrs.items() if k != "id"}
        ):
            skipped["different_paragraph_attributes"] += 1
            continue
        lt, rt = _text(left).rstrip(), _text(right).lstrip()
        if not lt or not rt or not rt[0].isalpha():
            skipped["no_continuation_evidence"] += 1
            continue
        if len(lt) >= 2 and lt[-1] in {"-", "\u00ad"} and lt[-2].isalpha() and rt[0].islower():
            action, reason = "dehyphenate", "possible_page_split_word"
        elif lt[-1] in {",", ";"} or lt[-1].isalnum():
            action, reason = "join_with_space", "possible_sentence_continuation"
        else:
            skipped["no_continuation_evidence"] += 1
            continue
        identity = hashlib.sha256(
            f"{page}\0{left}\0{marker.tag}\0{right}".encode("utf-8")
        ).hexdigest()[:24]
        candidates.append(_Candidate("pb-" + identity, page, marker.tag, left, right, action, reason, lt, rt))
    by_id = {candidate.identifier: candidate for candidate in candidates}
    if len(by_id) != len(candidates):
        raise ValueError("Ambiguous physical page boundaries need manual source review.")
    if set(selected) - set(by_id):
        raise ValueError("A selected boundary is unavailable; scan the current revision again.")
    for identifier, action in selected.items():
        if action in {"dehyphenate", "keep_hyphen"} and by_id[identifier].action != "dehyphenate":
            raise ValueError("Hyphen actions require a witnessed split-word candidate.")
    summaries = [candidate.public() for candidate in candidates[offset:offset + limit]]
    survivors: dict[int, Tag] = {}
    for candidate in candidates:
        action = selected.get(candidate.identifier)
        if action is None:
            continue
        left, right = candidate.left, candidate.right
        while id(left) in survivors:
            left = survivors[id(left)]
        _trim(left, trailing=True)
        _trim(right, trailing=False)
        if action == "dehyphenate":
            end = next(node for node in reversed(_text_nodes(left)) if str(node))
            if str(end)[-1] not in {"-", "\u00ad"}:
                raise ValueError("The selected word boundary changed; review it again.")
            end.replace_with(NavigableString(str(end)[:-1]))
        if candidate.marker.parent is not left and candidate.marker.parent is not right:
            left.append(candidate.marker.extract())
        if action == "join_with_space":
            left.append(NavigableString(" "))
        if right.get("id"):
            anchor = review._soup.new_tag("span", id=right["id"])
            left.append(anchor)
        for child in list(right.contents):
            left.append(child.extract())
        survivors[id(right)] = left
        right.extract()
    return (str(review._soup) if selected else None), {
        "candidate_count": len(candidates), "selected_count": len(selected),
        "offset": offset, "limit": limit, "has_more": offset + limit < len(candidates),
        "candidates": summaries, "skipped_count": sum(skipped.values()),
        "skipped_reasons": dict(skipped),
        "policy": "Review each proposal. Printed hyphens can be intentional. Existing Translator snapshots are unchanged.",
    }
