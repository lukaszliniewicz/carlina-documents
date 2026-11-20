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
from carlina.core.language import join_separator, seam_whitespace_is_spurious

_JOIN_ACTIONS = frozenset({"join_with_space", "join_without_space"})
_ACTIONS = _JOIN_ACTIONS | {"dehyphenate", "keep_hyphen", "remove_space"}
# Suggestions that may be accepted in bulk. Hyphen decisions stay individual:
# a printed hyphen can be intentional, so each is reviewed against the scan.
BULK_ACTIONS = frozenset({"join_with_space", "join_without_space", "remove_space"})
MAX_ACCEPTED_SUGGESTIONS = 1000
_SEAM_BLOCKS = frozenset({
    "p", "li", "td", "th", "dd", "dt", "figcaption", "h1", "h2", "h3", "h4", "h5", "h6",
})
_CLAUSE_PUNCTUATION = frozenset({",", ";", "\u3001", "\uff0c", "\uff1b"})
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
        if self.action == "dehyphenate":
            joined = left[:-1] + right
        elif self.action == "join_with_space":
            joined = left + " " + right
        else:
            joined = left + right
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


def _seam_block(marker: Tag) -> Tag | None:
    for parent in marker.parents:
        if not isinstance(parent, Tag):
            continue
        if parent.name in {"pre", "code"}:
            return None
        if parent.name in _SEAM_BLOCKS:
            return parent
    return None


def _seam_nodes(
    block: Tag, marker: Tag
) -> tuple[NavigableString, NavigableString] | None:
    """Return the text nodes that touch a page marker inside one block."""

    before: NavigableString | None = None
    after: NavigableString | None = None
    seen_marker = False
    for node in block.descendants:
        if node is marker:
            seen_marker = True
            continue
        if not isinstance(node, NavigableString) or isinstance(node, Comment):
            continue
        if any(
            _is_page_marker(parent) or _is_noteref(parent)
            for parent in node.parents
            if isinstance(parent, Tag) and parent is not block
        ):
            continue
        if not seen_marker:
            before = node
        elif after is None and str(node):
            after = node
            break
    if before is None or after is None:
        return None
    return before, after


def _joined_seam_candidate(page: int, marker: Tag) -> _Candidate | None:
    """A space that assembly put inside unspaced text at a joined page break."""

    block = _seam_block(marker)
    if block is None:
        return None
    nodes = _seam_nodes(block, marker)
    if nodes is None:
        return None
    before, after = nodes
    if not seam_whitespace_is_spurious(str(before), str(after)):
        return None
    identity = hashlib.sha256(
        f"{page}\0seam\0{block}\0{marker}".encode("utf-8")
    ).hexdigest()[:24]
    return _Candidate(
        "pb-" + identity,
        page,
        marker,
        block,
        block,
        "remove_space",
        "space_inside_unspaced_text",
        str(before),
        str(after),
    )


def repair_page_boundaries(
    review: DocumentReview, *, decisions: list[dict[str, str]] | None = None,
    page_numbers: list[int] | None = None, offset: int = 0, limit: int = 50,
    accept_suggested: list[str] | None = None,
) -> tuple[str | None, dict[str, Any]]:
    """Scan once, then apply reviewed joins as one reversible edit.

    ``decisions`` selects at most 50 candidates by ID. ``accept_suggested``
    instead accepts every candidate (within ``page_numbers``) whose suggested
    action is listed, up to MAX_ACCEPTED_SUGGESTIONS, so a repeated repair
    such as ``remove_space`` needs one preview and one save.

    A ``None`` HTML result means no edit: preserve the exact original bytes.
    Only adjacent same-context paragraphs qualify. Headings, notes, lists,
    quotations, figures, different attributes and intervening matter are not
    crossed. The physical marker and both paragraphs' target IDs survive.
    """
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError(
            "Boundary pagination is invalid: offset must be non-negative and limit between 1 and 50."
        )
    if page_numbers is not None and (
        not isinstance(page_numbers, list) or not 1 <= len(page_numbers) <= 10000
        or any(type(page) is not int or page < 1 for page in page_numbers)
    ):
        raise ValueError("Select 1-10000 positive physical page numbers.")
    if decisions is not None and accept_suggested is not None:
        raise ValueError("Use either decisions or accept_suggested, not both.")
    accepted_actions: frozenset[str] = frozenset()
    if accept_suggested is not None:
        if (
            not isinstance(accept_suggested, list)
            or not 1 <= len(accept_suggested) <= len(BULK_ACTIONS)
            or any(action not in BULK_ACTIONS for action in accept_suggested)
            or len(set(accept_suggested)) != len(accept_suggested)
        ):
            raise ValueError(
                "accept_suggested lists distinct suggested actions: "
                + ", ".join(sorted(BULK_ACTIONS))
                + "."
            )
        accepted_actions = frozenset(accept_suggested)
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
            seam = _joined_seam_candidate(page, marker.tag)
            if seam is not None:
                candidates.append(seam)
            else:
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
        elif lt[-1] in _CLAUSE_PUNCTUATION or lt[-1].isalnum():
            action = (
                "join_with_space" if join_separator(lt, rt) else "join_without_space"
            )
            reason = "possible_sentence_continuation"
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
    if accepted_actions:
        selected = {
            candidate.identifier: candidate.action
            for candidate in candidates
            if candidate.action in accepted_actions
        }
        if len(selected) > MAX_ACCEPTED_SUGGESTIONS:
            raise ValueError(
                f"{len(selected)} suggestions match; accept at most "
                f"{MAX_ACCEPTED_SUGGESTIONS} per save. Narrow with page_numbers."
            )
    if set(selected) - set(by_id):
        raise ValueError("A selected boundary is unavailable; scan the current revision again.")
    for identifier, action in selected.items():
        suggested = by_id[identifier].action
        if action in {"dehyphenate", "keep_hyphen"} and suggested != "dehyphenate":
            raise ValueError("Hyphen actions require a witnessed split-word candidate.")
        if (action == "remove_space") != (suggested == "remove_space"):
            raise ValueError("remove_space applies only to a joined seam candidate.")
    summaries = [candidate.public() for candidate in candidates[offset:offset + limit]]
    survivors: dict[int, Tag] = {}
    for candidate in candidates:
        action = selected.get(candidate.identifier)
        if action is None:
            continue
        if action == "remove_space":
            # An earlier join in this save may have moved the marker into
            # another paragraph, so find its block again.
            block = _seam_block(candidate.marker)
            seam = _seam_nodes(block, candidate.marker) if block is not None else None
            if seam is None or not seam_whitespace_is_spurious(
                str(seam[0]), str(seam[1])
            ):
                raise ValueError("The selected seam changed; review it again.")
            seam[0].replace_with(NavigableString(str(seam[0]).rstrip()))
            seam[1].replace_with(NavigableString(str(seam[1]).lstrip()))
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
    summary: dict[str, Any] = {
        "candidate_count": len(candidates), "selected_count": len(selected),
        "offset": offset, "limit": limit, "has_more": offset + limit < len(candidates),
        "candidates": summaries, "skipped_count": sum(skipped.values()),
        "skipped_reasons": dict(skipped),
        "policy": "Review each proposal. Printed hyphens can be intentional. Existing Translator snapshots are unchanged.",
    }
    if accepted_actions:
        summary["accepted"] = dict(sorted(Counter(selected.values()).items()))
    return (str(review._soup) if selected else None), summary
