"""Read-only structural review and bounded editing for semantic document HTML.

This module intentionally knows nothing about persistence or sanitization.  It
parses one HTML snapshot, gives its candidate elements stable identifiers for
the lifetime of the instance, and returns a serialized successor when edits
are requested.
"""

from __future__ import annotations

import copy
import json
import re
import unicodedata
from bisect import bisect_right
from collections import Counter
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import unquote, urlsplit

from bs4 import BeautifulSoup, Comment, Tag
from bs4.element import NavigableString, PageElement

CANDIDATE_TAGS = frozenset(
    {
        "address",
        "article",
        "caption",
        "dd",
        "dl",
        "dt",
        "footer",
        "header",
        "nav",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "p",
        "blockquote",
        "ul",
        "ol",
        "li",
        "table",
        "thead",
        "tbody",
        "tfoot",
        "tr",
        "td",
        "th",
        "figure",
        "figcaption",
        "pre",
        "div",
        "section",
        "aside",
    }
)
_HEADING_RE = re.compile(r"^h([1-6])$")

_ARABIC_ENUMERATOR_RE = re.compile(r"[0-9]+[.)]")
_PAREN_ENUMERATOR_RE = re.compile(r"\([0-9]+\)")
_ROMAN_ENUMERATOR_RE = re.compile(r"[IVXLCDM]+\.")


def _heading_enumerator_shape(text: str) -> str | None:
    """Classify a heading's leading enumerator, if any, by notation shape.

    Only the notation shape is returned (arabic/roman/paren), never the
    number itself, so that "1." and "2." compare equal while "1." and "I."
    do not. Structural rank drift (h2 versus h3) is only meaningful for
    h1-h3 callers to check; within-paper numbering at h4 and below is
    ignored by only calling this for structural levels.
    """

    stripped = (text or "").lstrip()
    if _ARABIC_ENUMERATOR_RE.match(stripped):
        return "arabic"
    if _PAREN_ENUMERATOR_RE.match(stripped):
        return "paren"
    if _ROMAN_ENUMERATOR_RE.match(stripped):
        return "roman"
    return None
_PAGE_ID_RE = re.compile(r"^doc-page-(\d{1,9})$", re.IGNORECASE)
_PAGE_TITLE_RE = re.compile(r"^\d{1,9}$")
_WHITESPACE_RE = re.compile(r"\s+")
_HIDDEN_TAGS = frozenset({"head", "script", "style", "noscript", "template"})
_WRAPPER_TAGS = frozenset({"html", "head", "body", "main"})
_BLOCK_TAGS = CANDIDATE_TAGS | {"body", "main"}
_PUNCTUATION = frozenset(".,;:!?)]}»”'\"、。，；：！？）］｝»”")

MAX_OUTLINE_LIMIT = 200
MAX_SEARCH_LIMIT = 50
MAX_FETCH_LIMIT = 20
MAX_BOUNDARY_LIMIT = 50
MAX_NOTES_LIMIT = 50
MAX_FETCH_HTML_CHARS = 64_000
MAX_FETCH_RESPONSE_CHARS = 128_000
MAX_CONTEXT_HTML_CHARS = 8_000
MAX_CONTEXT_HTML_TOTAL = 64_000
MAX_NOTE_TEXT_CHARS = 600
MAX_NOTE_LABEL_CHARS = 500
MAX_NOTE_HTML_CHARS = 8_000
MAX_NOTE_HTML_TOTAL = 64_000
MAX_NOTE_RESPONSE_CHARS = 128_000
MAX_NOTE_ISSUE_CHARS = 16_000
MAX_NOTE_REFERENCES = 20
MAX_NOTE_REFERENCE_PAGES = 20
MAX_NOTE_BACKLINKS = 20
MAX_NOTE_ISSUES = 100
MAX_EXPLICIT_ELEMENT_IDS = 200
MAX_HEADING_TEXT_CHARS = 500
MAX_SEARCH_SNIPPET_CHARS = 600
MAX_CLASS_COUNT = 32
MAX_CLASS_CHARS = 64


def _error(message: str = "invalid document review request") -> ValueError:
    """Return a safe public error that never contains supplied document text."""

    return ValueError(message)


def _strict_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _error(f"{name} must be an integer")
    return value


def _pagination(offset: Any, limit: Any, maximum: int) -> tuple[int, int]:
    offset_value = _strict_int(offset, "offset")
    limit_value = _strict_int(limit, "limit")
    if offset_value < 0:
        raise _error("offset must be non-negative")
    if not 1 <= limit_value <= maximum:
        raise _error("limit is outside the allowed range")
    return offset_value, limit_value


def _normalized_text(value: str) -> str:
    return _WHITESPACE_RE.sub(" ", value).strip()


def _is_descendant(tag: PageElement, ancestor: PageElement) -> bool:
    current: PageElement | None = tag
    while current is not None:
        if current is ancestor:
            return True
        current = current.parent
    return False


def _identity_index(values: list[PageElement], target: PageElement) -> int:
    for index, value in enumerate(values):
        if value is target:
            return index
    return -1


def _under_head(tag: PageElement) -> bool:
    current: PageElement | None = tag
    while current is not None:
        if isinstance(current, Tag) and str(current.name).casefold() == "head":
            return True
        current = current.parent
    return False


def _tag_classes(tag: Tag) -> list[str]:
    raw = tag.get("class")
    if isinstance(raw, (list, tuple)):
        values = [str(item) for item in raw]
    elif raw:
        values = str(raw).split()
    else:
        values = []
    return [item[:MAX_CLASS_CHARS] for item in values[:MAX_CLASS_COUNT]]


def _is_page_marker(tag: Tag) -> bool:
    role = str(tag.get("role") or "").casefold()
    return role == "doc-pagebreak" or any(
        item.casefold() == "pagebreak" for item in _tag_classes(tag)
    )


def _marker_page_number(tag: Tag) -> int | None:
    identifier = str(tag.get("id") or "").strip()
    match = _PAGE_ID_RE.fullmatch(identifier)
    if match:
        number = int(match.group(1))
        if number > 0:
            return number
    title = str(tag.get("title") or "").strip()
    if _PAGE_TITLE_RE.fullmatch(title):
        number = int(title)
        return number if number > 0 else None
    return None


def _is_noteref(tag: Tag) -> bool:
    classes = tag.get("class") or []
    return (
        str(tag.get("role") or "").casefold() == "doc-noteref"
        or "noteref" in str(tag.get("epub:type") or "").casefold().split()
        or "footnote-ref" in classes
    )


def _is_note_body(tag: Tag) -> bool:
    role = {item.casefold() for item in str(tag.get("role") or "").split()}
    epub_type = {item.casefold() for item in str(tag.get("epub:type") or "").split()}
    return bool(role & {"doc-footnote", "doc-endnote"}) or bool(
        epub_type & {"footnote", "endnote"}
    )


def _is_backlink(tag: Tag) -> bool:
    if str(tag.name or "").casefold() != "a":
        return False
    role = {item.casefold() for item in str(tag.get("role") or "").split()}
    epub_type = {item.casefold() for item in str(tag.get("epub:type") or "").split()}
    classes = set(tag.get("class") or [])
    return bool(
        role & {"backlink", "doc-backlink"}
        or epub_type & {"backlink"}
        or classes
        & {
            "backlink",
            "footnote-backlink",
            "footnote-backref",
            "footnote-back",
            "endnote-backlink",
            "endnote-backref",
            "endnote-back",
        }
    )


def _is_endnotes_container(tag: Tag) -> bool:
    if _is_noteref(tag):
        return False
    values: list[str] = []
    for attribute in ("id", "role", "epub:type"):
        values.extend(str(tag.get(attribute) or "").casefold().split())
    values.extend(item.casefold() for item in _tag_classes(tag))
    return any(
        re.search(r"(?:^|[-_])(?:endnotes?|footnotes?)(?:$|[-_])", value)
        for value in values
    )


def _walk_visible_text(tag: Tag, candidate_ids: set[int]) -> str:
    parts: list[str] = []

    def walk(node: PageElement, *, root: bool = False) -> None:
        if isinstance(node, Comment):
            return
        if isinstance(node, NavigableString):
            parts.append(str(node))
            return
        if not isinstance(node, Tag):
            return
        name = str(node.name or "").casefold()
        if name in _HIDDEN_TAGS:
            return
        if not root and id(node) in candidate_ids:
            parts.append(" ")
            return
        if (
            node.has_attr("hidden")
            or str(node.get("aria-hidden") or "").casefold() == "true"
        ):
            return
        if name == "br":
            parts.append(" ")
        for child in node.contents:
            walk(child)

    walk(tag, root=True)
    return _normalized_text("".join(parts))


def _walk_all_visible_text(tag: Tag) -> str:
    parts: list[str] = []

    def walk(node: PageElement, *, root: bool = False) -> None:
        if isinstance(node, Comment):
            return
        if isinstance(node, NavigableString):
            parts.append(str(node))
            return
        if not isinstance(node, Tag):
            return
        name = str(node.name or "").casefold()
        if name in _HIDDEN_TAGS:
            return
        if not root and (
            node.has_attr("hidden")
            or str(node.get("aria-hidden") or "").casefold() == "true"
        ):
            return
        if name == "br":
            parts.append(" ")
        for child in node.contents:
            walk(child)

    walk(tag, root=True)
    return _normalized_text("".join(parts))


def _bounded_text(value: str, maximum: int) -> tuple[str, bool]:
    if len(value) <= maximum:
        return value, False
    return value[:maximum], True


def _bounded_classes_for_output(tag: Tag) -> list[str]:
    return _tag_classes(tag)


@dataclass(frozen=True)
class _Node:
    element_id: str
    tag: Tag
    own_text: str
    page_numbers: tuple[int, ...]
    order: int


@dataclass(frozen=True)
class _Marker:
    tag: Tag
    page_number: int | None
    position: int


@dataclass(frozen=True)
class _PreparedEdit:
    element_id: str
    target: Tag
    scope: tuple[PageElement, ...]
    replacement_html: str | None
    heading_level: int | None
    delete_element: bool = False
    replace_find: str | None = None
    replace_replacement: str | None = None
    replace_expected_matches: int | None = None
    demote_heading: bool = False
    text_changes: tuple[tuple[NavigableString, int, int, str], ...] = ()
    edit_index: int = -1
    replacement_index: int | None = None


class _EditError(ValueError):
    """Public diagnostics contain locations/counts, never submitted text."""

    def __init__(self, diagnostics: list[dict[str, Any]]):
        self.diagnostics = diagnostics
        descriptions = []
        for item in diagnostics[:64]:
            fields = " ".join(
                f"{key}={value}" for key, value in item.items() if key != "message"
            )
            descriptions.append(f"{fields}: {item.get('message', 'edit rejected')}")
        super().__init__("; ".join(descriptions))


def _word_character(character: str) -> bool:
    category = unicodedata.category(character)
    return category[0] in {"L", "M", "N"} or category == "Pc"


def _literal_matches(text: str, query: str, case_sensitive: bool, whole_word: bool):
    """Yield original codepoint spans, including casefold expansions safely."""
    origins = None
    haystack, needle = text, query
    if not case_sensitive:
        folded_parts = []
        origins = []
        for index, character in enumerate(text):
            folded = character.casefold()
            folded_parts.append(folded)
            origins.extend([index] * len(folded))
        haystack, needle = "".join(folded_parts), query.casefold()
    position = 0
    while needle:
        position = haystack.find(needle, position)
        if position < 0:
            return
        stop = position + len(needle)
        start = origins[position] if origins is not None else position
        end = origins[stop - 1] + 1 if origins is not None else stop
        aligned = origins is None or (
            (position == 0 or origins[position - 1] != origins[position])
            and (stop == len(origins) or origins[stop - 1] != origins[stop])
        )
        if not whole_word or (
            aligned
            and (start == 0 or not _word_character(text[start - 1]))
            and (end == len(text) or not _word_character(text[end]))
        ):
            yield start, end
            position = stop
        else:
            position += 1


class DocumentReview:
    """Review one HTML document snapshot with deterministic node identity."""

    def __init__(self, html: str):
        if not isinstance(html, str):
            raise _error("html must be a string")
        self._soup = BeautifulSoup(html, "html.parser")
        self._positions: dict[int, int] = {
            id(node): index for index, node in enumerate(self._soup.descendants)
        }
        # Descendant intervals and sorted text/marker positions make per-node
        # physical page lookup logarithmic, rather than rescanning the book.
        self._subtree_end = dict(self._positions)
        for element in reversed(list(self._soup.descendants)):
            parent = element.parent
            if parent is not None and id(parent) in self._subtree_end:
                self._subtree_end[id(parent)] = max(
                    self._subtree_end[id(parent)], self._subtree_end[id(element)]
                )
        self._body = self._soup.body
        self._page_scope = self._find_page_scope()
        self._candidate_tags = self._find_candidate_tags(self._soup)
        candidate_ids = {id(tag) for tag in self._candidate_tags}
        self._nodes: list[_Node] = []
        self._id_to_node: dict[str, _Node] = {}
        self._tag_id: dict[int, str] = {}
        for index, tag in enumerate(self._candidate_tags, start=1):
            element_id = f"n{index:06d}"
            node = _Node(
                element_id=element_id,
                tag=tag,
                own_text=_walk_visible_text(tag, candidate_ids),
                page_numbers=(),
                order=index - 1,
            )
            self._nodes.append(node)
            self._id_to_node[element_id] = node
            self._tag_id[id(tag)] = element_id

        self._markers = self._find_markers(self._soup)
        self._numbered_markers = [m for m in self._markers if m.page_number is not None]
        self._marker_positions = [m.position for m in self._numbered_markers]
        self._visible_text_positions = tuple(
            self._positions[id(node)]
            for node in self._visible_text_nodes()
            if str(node).strip()
        )
        self._nodes = [
            _Node(
                element_id=node.element_id,
                tag=node.tag,
                own_text=node.own_text,
                page_numbers=self._page_numbers_for(node.tag),
                order=node.order,
            )
            for node in self._nodes
        ]
        self._id_to_node = {node.element_id: node for node in self._nodes}
        self._note_index: dict[str, Any] | None = None

    def _find_page_scope(self) -> Tag | BeautifulSoup:
        scope = self._body if self._body is not None else self._soup
        main = scope.find("main")
        return main if isinstance(main, Tag) else scope

    @staticmethod
    def _find_candidate_tags(soup: BeautifulSoup) -> list[Tag]:
        body = soup.body
        tags = body.find_all(True) if body is not None else soup.find_all(True)
        return [
            tag
            for tag in tags
            if str(tag.name or "").casefold() in CANDIDATE_TAGS and not _under_head(tag)
        ]

    def _in_page_scope(self, tag: PageElement) -> bool:
        if _under_head(tag):
            return False
        if self._page_scope is not self._soup and not _is_descendant(
            tag, self._page_scope
        ):
            return False
        current: PageElement | None = tag
        while current is not None:
            if isinstance(current, Tag) and _is_endnotes_container(current):
                return False
            if current is self._page_scope:
                return True
            current = current.parent
        return False

    def _find_markers(self, soup: BeautifulSoup) -> list[_Marker]:
        markers: list[_Marker] = []
        for tag in soup.find_all(True):
            if not _is_page_marker(tag) or not self._in_page_scope(tag):
                continue
            position = self._positions.get(id(tag))
            if position is None:
                continue
            markers.append(_Marker(tag, _marker_page_number(tag), position))
        return markers

    def _page_numbers_for(self, tag: Tag) -> tuple[int, ...]:
        if not self._in_page_scope(tag):
            return ()
        start = self._positions.get(id(tag))
        if start is None:
            return ()
        end = self._subtree_end.get(id(tag), start)
        left = bisect_right(self._visible_text_positions, start)
        right = bisect_right(self._visible_text_positions, end)
        first = self._visible_text_positions[left] if left < right else start
        last = self._visible_text_positions[right - 1] if left < right else end
        first_marker = bisect_right(self._marker_positions, first)
        last_marker = bisect_right(self._marker_positions, last)
        pages = {
            marker.page_number
            for marker in self._numbered_markers[max(0, first_marker - 1):last_marker]
        }
        return tuple(sorted(pages))

    def _require_node(self, element_id: Any) -> _Node:
        if not isinstance(element_id, str) or element_id not in self._id_to_node:
            raise _error("unknown element id")
        return self._id_to_node[element_id]

    def _page_list(self, node: _Node) -> list[int]:
        return [] if self._is_note_tag(node.tag) else list(node.page_numbers)

    def _is_leaf_review_node(self, node: _Node) -> bool:
        return not any(
            isinstance(descendant, Tag) and id(descendant) in self._tag_id
            for descendant in node.tag.descendants
        )

    def outline(self, offset: int = 0, limit: int = 100) -> dict[str, Any]:
        offset_value, limit_value = _pagination(offset, limit, MAX_OUTLINE_LIMIT)
        headings = [
            node for node in self._nodes if _HEADING_RE.fullmatch(node.tag.name or "")
        ]
        note_index = self._build_note_index()
        note_tags: set[int] = note_index["note_tags"]
        body_headings = [node for node in headings if id(node.tag) not in note_tags]
        root_level = min(
            (int(str(node.tag.name)[1]) for node in body_headings), default=None
        )
        parents: dict[str, str | None] = {}
        issues: dict[str, list[str]] = {}
        body_stack: list[tuple[int, str]] = []
        note_stacks: dict[int, list[tuple[int, str]]] = {}
        next_text = {}
        following = None
        for candidate in reversed(self._nodes):
            next_text[candidate.element_id] = following
            if candidate.own_text:
                following = candidate
        qa_ids = set()
        run = []
        for candidate in headings:
            if candidate.own_text.endswith("?") and (
                not run or candidate.tag.name == run[-1].tag.name
            ):
                run.append(candidate)
            else:
                if len(run) >= 3:
                    qa_ids.update(item.element_id for item in run)
                run = [candidate] if candidate.own_text.endswith("?") else []
        if len(run) >= 3:
            qa_ids.update(item.element_id for item in run)
        # Same-notation enumerators (1./2., I./II.) at mixed structural
        # levels usually mean rank drift across batches, not intent: a
        # document numbers sibling divisions alike. h4 and below carry
        # within-paper numbering and are excluded.
        shape_levels: dict[str, set[int]] = {}
        node_shapes: dict[str, str | None] = {}
        for candidate in headings:
            shape = _heading_enumerator_shape(candidate.own_text)
            node_shapes[candidate.element_id] = shape
            if shape is not None:
                shape_levels.setdefault(shape, set()).add(int(candidate.tag.name[1]))
        mixed_shapes = {
            shape
            for shape, levels in shape_levels.items()
            if len({level for level in levels if level <= 3}) > 1
        }
        for node in headings:
            level = int(node.tag.name[1])
            is_note = id(node.tag) in note_tags
            note_body = note_index["body_by_tag"].get(id(node.tag))
            stack = (
                note_stacks.setdefault(id(note_body) if note_body else id(node.tag), [])
                if is_note
                else body_stack
            )
            while stack and stack[-1][0] >= level:
                stack.pop()
            parents[node.element_id] = stack[-1][1] if stack else None
            node_issues: list[str] = []
            if not node.own_text:
                node_issues.append("empty_heading")
            if (
                (not stack and root_level is not None and level > root_level)
                or (stack and level > stack[-1][0] + 1)
            ):
                node_issues.append("level_jump")
            if re.fullmatch(r"(?:[0-9]+[.)]?|\([0-9]+\))", node.own_text):
                node_issues.append("ordinal_only_heading")
            following = next_text.get(node.element_id)
            if (
                following is not None
                and following.tag.name == "p"
                and following.own_text[:1].islower()
                and len(node.own_text.split()) <= 12
                and (
                    node.own_text.endswith((",", ";", ":"))
                    or (
                        len(node.own_text.split()) <= 6
                        and not node.own_text.endswith((".", "!", "?"))
                    )
                )
            ):
                node_issues.append("possible_run_in_heading")
            if node.element_id in qa_ids:
                node_issues.append("question_answer_sequence")
            if node_shapes[node.element_id] in mixed_shapes:
                node_issues.append("mixed_enumerator_levels")
            issues[node.element_id] = node_issues
            stack.append((level, node.element_id))

        page = headings[offset_value : offset_value + limit_value]
        items: list[dict[str, Any]] = []
        for node in page:
            text, truncated = _bounded_text(node.own_text, MAX_HEADING_TEXT_CHARS)
            items.append(
                {
                    "element_id": node.element_id,
                    "level": int(node.tag.name[1]),
                    "text": text,
                    "truncated": truncated,
                    "page_numbers": self._page_list(node),
                    "parent_heading_id": parents[node.element_id],
                    "issues": list(issues[node.element_id]),
                }
            )
        next_offset = (
            offset_value + len(page)
            if offset_value + len(page) < len(headings)
            else None
        )
        counts = {
            "heading_levels": {
                f"h{level}": sum(1 for node in headings if node.tag.name == f"h{level}")
                for level in range(1, 7)
            },
            "elements": len(self._nodes),
            "page_markers": len(self._markers),
        }
        return {
            "headings": items,
            "counts": counts,
            "total_headings": len(headings),
            "hints_are_advisory": True,
            "next_offset": next_offset,
        }

    def inventory(
        self,
        offset: int = 0,
        limit: int = 100,
        scope: str = "all",
        query: str = "",
        case_sensitive: bool = False,
    ) -> dict[str, Any]:
        """Complete paginated lexical candidates, not automatic spelling corrections."""
        offset, limit = _pagination(offset, limit, 200)
        if not isinstance(scope, str) or scope not in {
            "all",
            "body",
            "headings",
            "notes",
        }:
            raise _error("scope is invalid")
        if (
            not isinstance(query, str)
            or len(query) > 1000
            or type(case_sensitive) is not bool
        ):
            raise _error("query and case_sensitive are invalid")
        counts: Counter[str] = Counter()
        examples = {}
        selected_nodes = [
            node for node in self._search_nodes() if self._scope_matches(node, scope)
        ]
        for node in selected_nodes:
            characters = []
            tokens = []
            for character in node.own_text:
                if _word_character(character):
                    characters.append(character)
                elif characters:
                    tokens.append("".join(characters))
                    characters = []
            if characters:
                tokens.append("".join(characters))
            for token in tokens:
                counts[token] += 1
            for token in set(tokens):
                witnesses = examples.setdefault(token, [])
                if len(witnesses) < 3:
                    pages = list(self._node_filter_pages(node))
                    witnesses.append(
                        {
                            "element_id": node.element_id,
                            "page_numbers": pages[:5],
                            "omitted_page_numbers": max(0, len(pages) - 5),
                        }
                    )
        filter_query = query if case_sensitive else query.casefold()
        tokens = sorted(
            token
            for token in counts
            if filter_query in (token if case_sensitive else token.casefold())
        )
        items = []
        budget = 0
        for token in tokens[offset : offset + limit]:
            item = {
                "token": token[:256],
                "token_length": len(token),
                "token_truncated": len(token) > 256,
                "occurrences": counts[token],
                "examples": examples[token],
            }
            size = len(json.dumps(item, ensure_ascii=False))
            if items and budget + size > 64_000:
                break
            items.append(item)
            budget += size
        consumed = offset + len(items)
        return {
            "items": items,
            "next_offset": consumed if consumed < len(tokens) else None,
            "total_matching_tokens": len(tokens),
            "total_distinct_tokens": len(counts),
            "total_occurrences": sum(counts.values()),
            "overlong_distinct_tokens": sum(len(token) > 256 for token in counts),
            "inventory_options": {
                "scope": scope,
                "query": query,
                "case_sensitive": case_sensitive,
            },
            "coverage": {
                "selected_text_elements": len(selected_nodes),
                "heading_levels": dict(
                    Counter(
                        node.tag.name
                        for node in self._nodes
                        if _HEADING_RE.fullmatch(node.tag.name or "")
                    )
                ),
                "note_bodies": self.notes(limit=1)["total_notes"],
                "physical_page_markers": len(self._markers),
            },
            "review_completion": "not_tracked",
            "guidance": "Inventory and dictionary flags are candidates, not errors. Inspect source pages; rerun on the final revision. Fetch truncated tokens via their element IDs.",
        }

    @staticmethod
    def _bounded_attribute(tag: Tag, name: str) -> str | None:
        value = tag.get(name)
        if value is None:
            return None
        return str(value)[:MAX_NOTE_LABEL_CHARS]

    @staticmethod
    def _local_fragment(href: Any) -> str | None:
        if not isinstance(href, str) or not href:
            return None
        try:
            parsed = urlsplit(href)
        except ValueError:
            return None
        if parsed.path or parsed.query or parsed.netloc or not href.startswith("#"):
            return None
        fragment = unquote(parsed.fragment)
        return fragment or None

    def _note_body_candidates(self) -> list[Tag]:
        candidates: list[Tag] = []
        for tag in self._soup.find_all(True):
            if _under_head(tag):
                continue
            name = str(tag.name or "").casefold()
            in_note_container = any(
                isinstance(parent, Tag) and _is_endnotes_container(parent)
                for parent in self._candidate_ancestors_including_self(tag)[1:]
            )
            if _is_note_body(tag) or (
                tag.has_attr("id")
                and name in {"li", "p", "aside"}
                and in_note_container
            ):
                candidates.append(tag)
        return candidates

    @staticmethod
    def _candidate_ancestors_including_self(node: PageElement) -> list[PageElement]:
        ancestors: list[PageElement] = []
        current: PageElement | None = node
        while current is not None:
            ancestors.append(current)
            current = current.parent
        return ancestors

    def _note_issue(
        self,
        code: str,
        *,
        anchor: Tag | None = None,
        href: Any = None,
        label: str | None = None,
        html_id: str | None = None,
    ) -> dict[str, Any]:
        return {
            "code": code,
            "element_id": self._nearest_candidate_id(anchor) if anchor else None,
            "html_id": (
                html_id[:MAX_NOTE_LABEL_CHARS]
                if html_id
                else self._bounded_attribute(anchor, "id")
                if anchor
                else None
            ),
            "href": str(href)[:MAX_NOTE_LABEL_CHARS] if href is not None else None,
            "label": (label or "")[:MAX_NOTE_LABEL_CHARS],
        }

    def _build_note_index(self) -> dict[str, Any]:
        if self._note_index is not None:
            return self._note_index

        ids: dict[str, list[Tag]] = {}
        for tag in self._soup.find_all(True):
            if tag.has_attr("id"):
                identifier = str(tag.get("id") or "")
                if identifier:
                    ids.setdefault(identifier, []).append(tag)

        explicit = self._note_body_candidates()
        global_issues: list[dict[str, Any]] = []
        targets: list[Tag] = []
        refs_by_target: dict[int, list[Tag]] = {}
        referenced_fragments: set[str] = set()
        for anchor in self._soup.find_all(True):
            if _under_head(anchor) or not _is_noteref(anchor):
                continue
            href = anchor.get("href")
            fragment = self._local_fragment(href)
            label = _walk_all_visible_text(anchor) or str(
                anchor.get("aria-label") or ""
            )
            if fragment is None:
                global_issues.append(
                    self._note_issue(
                        "missing_target", anchor=anchor, href=href, label=label
                    )
                )
                continue
            referenced_fragments.add(fragment)
            matches = ids.get(fragment, [])
            if not matches:
                global_issues.append(
                    self._note_issue(
                        "missing_target",
                        anchor=anchor,
                        href=href,
                        label=label,
                        html_id=fragment,
                    )
                )
                continue
            if len(matches) != 1:
                global_issues.append(
                    self._note_issue(
                        "duplicate_id",
                        anchor=anchor,
                        href=href,
                        label=label,
                        html_id=fragment,
                    )
                )
                continue
            target = matches[0]
            if _under_head(target):
                global_issues.append(
                    self._note_issue(
                        "missing_target",
                        anchor=anchor,
                        href=href,
                        label=label,
                        html_id=fragment,
                    )
                )
                continue
            targets.append(target)
            refs_by_target.setdefault(id(target), []).append(anchor)

        relevant_duplicate_ids = {
            str(tag.get("id") or "") for tag in explicit if tag.has_attr("id")
        } | referenced_fragments
        for identifier, tags in ids.items():
            if len(tags) > 1 and identifier in relevant_duplicate_ids:
                global_issues.append(
                    self._note_issue("duplicate_id", html_id=identifier)
                )

        # A target can be a note container while its explicitly identified
        # children are also discoverable. Keep the outer body once.
        roots: list[Tag] = []
        for body in sorted(
            [*explicit, *targets], key=lambda tag: self._positions.get(id(tag), 0)
        ):
            if any(root is body or _is_descendant(body, root) for root in roots):
                continue
            roots.append(body)

        body_refs: dict[int, list[Tag]] = {id(body): [] for body in roots}
        for target_id, refs in refs_by_target.items():
            target = next((body for body in roots if id(body) == target_id), None)
            if target is None:
                target_tag = next(
                    (tag for tag in targets if id(tag) == target_id), None
                )
                if target_tag is not None:
                    target = next(
                        (body for body in roots if _is_descendant(target_tag, body)),
                        None,
                    )
            if target is not None:
                body_refs.setdefault(id(target), []).extend(refs)

        body_issues: dict[int, list[str]] = {id(body): [] for body in roots}
        for body in roots:
            if len(ids.get(str(body.get("id") or ""), [])) > 1:
                body_issues[id(body)].append("duplicate_id")

        backlinks: dict[int, list[Tag]] = {id(body): [] for body in roots}
        for body in roots:
            refs = body_refs[id(body)]
            ref_ids = {str(ref.get("id")) for ref in refs if ref.has_attr("id")}
            for anchor in body.find_all("a"):
                fragment = self._local_fragment(anchor.get("href"))
                if not _is_backlink(anchor) and fragment not in ref_ids:
                    continue
                backlinks[id(body)].append(anchor)
                href = anchor.get("href")
                matches = ids.get(fragment or "", [])
                valid = len(matches) == 1 and any(
                    _is_descendant(ref, matches[0]) for ref in refs
                )
                if not valid:
                    body_issues[id(body)].append("invalid_backlink")
                    global_issues.append(
                        self._note_issue(
                            "invalid_backlink",
                            anchor=anchor,
                            href=href,
                            label=_walk_all_visible_text(anchor),
                            html_id=fragment,
                        )
                    )

        note_tags: set[int] = set()
        body_ids = {id(body) for body in roots}
        for tag in self._soup.find_all(True):
            if (
                id(tag) in body_ids
                or _is_endnotes_container(tag)
                or id(tag.parent) in note_tags
            ):
                note_tags.add(id(tag))
        body_by_id = {id(body): body for body in roots}
        body_by_tag: dict[int, Tag] = {}
        for tag in self._soup.find_all(True):
            if id(tag) not in note_tags:
                continue
            current: PageElement | None = tag
            while current is not None:
                body = body_by_id.get(id(current))
                if body is not None:
                    body_by_tag[id(tag)] = body
                    break
                current = current.parent
        self._note_index = {
            "bodies": roots,
            "refs": body_refs,
            "backlinks": backlinks,
            "body_issues": body_issues,
            "global_issues": global_issues,
            "note_tags": note_tags,
            "body_by_tag": body_by_tag,
        }
        return self._note_index

    def _is_note_tag(self, tag: Tag) -> bool:
        return id(tag) in self._build_note_index()["note_tags"]

    def _node_filter_pages(self, node: _Node) -> tuple[int, ...]:
        return tuple(self._page_list(node))

    def _search_nodes(self) -> list[_Node]:
        return [node for node in self._nodes if node.own_text]

    def _scope_matches(self, node: _Node, scope: str) -> bool:
        if scope == "all":
            return True
        is_note = self._is_note_tag(node.tag)
        is_heading = bool(_HEADING_RE.fullmatch(str(node.tag.name or "")))
        if scope == "notes":
            return is_note
        if scope == "headings":
            return is_heading
        return not is_note and not is_heading

    @staticmethod
    def _heading_path_from_stack(
        stack: list[tuple[int, _Node]],
    ) -> list[dict[str, Any]]:
        return [
            {
                "element_id": ancestor.element_id,
                "level": level,
                "text": _bounded_text(ancestor.own_text, MAX_HEADING_TEXT_CHARS)[0],
            }
            for level, ancestor in stack[-6:]
        ]

    @staticmethod
    def _common_heading_prefix(
        paths: list[list[dict[str, Any]]],
    ) -> list[dict[str, Any]]:
        if not paths:
            return []
        length = min(len(path) for path in paths)
        common_length = 0
        while common_length < length and all(
            path[common_length]["element_id"]
            == paths[0][common_length]["element_id"]
            for path in paths[1:]
        ):
            common_length += 1
        return [dict(item) for item in paths[0][:common_length]]

    @staticmethod
    def _add_bounded_reference_page(pages: set[int], page: int) -> bool:
        if page in pages:
            return False
        if len(pages) < MAX_NOTE_REFERENCE_PAGES:
            pages.add(page)
            return False
        largest = max(pages)
        if page < largest:
            pages.remove(largest)
            pages.add(page)
        return True

    def _heading_metadata(
        self,
    ) -> tuple[
        dict[str, list[dict[str, Any]]],
        dict[int, dict[str, Any]],
        dict[str, dict[str, Any]],
        dict[int, dict[int, list[dict[str, Any]]]],
    ]:
        """Build body-only hierarchy and reference-derived note context."""

        index = self._build_note_index()
        note_tags: set[int] = index["note_tags"]
        paths: dict[str, list[dict[str, Any]]] = {}
        body_paths: dict[str, list[dict[str, Any]]] = {}
        stack: list[tuple[int, _Node]] = []
        for node in self._nodes:
            if id(node.tag) in note_tags:
                continue
            if _HEADING_RE.fullmatch(str(node.tag.name or "")):
                level = int(str(node.tag.name)[1])
                while stack and stack[-1][0] >= level:
                    stack.pop()
                stack.append((level, node))
            body_paths[node.element_id] = self._heading_path_from_stack(stack)

        body_headings = [
            node
            for node in self._nodes
            if _HEADING_RE.fullmatch(str(node.tag.name or ""))
            and id(node.tag) not in note_tags
        ]
        body_headings.sort(key=lambda node: self._positions.get(id(node.tag), 0))
        note_reference_anchors = [
            (body_id, anchor)
            for body_id, refs in index["refs"].items()
            for anchor in refs
            if id(anchor) not in note_tags
        ]
        note_reference_anchors.sort(
            key=lambda item: self._positions.get(id(item[1]), 0)
        )
        displayed_reference_ids = {
            body_id: {id(anchor) for anchor in refs[:MAX_NOTE_REFERENCES]}
            for body_id, refs in index["refs"].items()
        }
        heading_index = 0
        reference_paths: dict[int, dict[int, list[dict[str, Any]]]] = {}
        common_paths: dict[int, list[dict[str, Any]]] = {}
        body_reference_ids: set[int] = set()
        reference_stack: list[tuple[int, _Node]] = []
        for body_id, anchor in note_reference_anchors:
            anchor_position = self._positions.get(id(anchor), 0)
            while (
                heading_index < len(body_headings)
                and self._positions.get(id(body_headings[heading_index].tag), 0)
                < anchor_position
            ):
                heading = body_headings[heading_index]
                level = int(str(heading.tag.name)[1])
                while reference_stack and reference_stack[-1][0] >= level:
                    reference_stack.pop()
                reference_stack.append((level, heading))
                heading_index += 1
            path = self._heading_path_from_stack(reference_stack)
            if body_id in body_reference_ids:
                common_paths[body_id] = self._common_heading_prefix(
                    [common_paths[body_id], path]
                )
            else:
                common_paths[body_id] = path
                body_reference_ids.add(body_id)
            if id(anchor) in displayed_reference_ids[body_id]:
                reference_paths.setdefault(body_id, {})[id(anchor)] = path

        body_contexts: dict[int, dict[str, Any]] = {}
        for body in index["bodies"]:
            refs = index["refs"].get(id(body), [])
            reference_pages: set[int] = set()
            reference_pages_truncated = False
            for anchor in refs:
                if id(anchor) in note_tags:
                    continue
                for page_number in self._page_numbers_for(anchor):
                    reference_pages_truncated = (
                        self._add_bounded_reference_page(reference_pages, page_number)
                        or reference_pages_truncated
                    )
            body_contexts[id(body)] = {
                "heading_path": [
                    dict(item) for item in common_paths.get(id(body), [])
                ],
                "heading_context_basis": "references"
                if id(body) in body_reference_ids
                else "unknown",
                "reference_page_numbers": sorted(reference_pages),
                "reference_page_numbers_truncated": reference_pages_truncated,
            }

        note_metadata: dict[str, dict[str, Any]] = {}
        body_by_tag: dict[int, Tag] = index["body_by_tag"]
        for node in self._nodes:
            if id(node.tag) not in note_tags:
                paths[node.element_id] = [dict(item) for item in body_paths[node.element_id]]
                continue
            body = body_by_tag.get(id(node.tag))
            context = body_contexts.get(
                id(body) if body is not None else -1,
                {
                    "heading_path": [],
                    "heading_context_basis": "unknown",
                    "reference_page_numbers": [],
                    "reference_page_numbers_truncated": False,
                },
            )
            paths[node.element_id] = [dict(item) for item in context["heading_path"]]
            note_metadata[node.element_id] = {
                "heading_path": [dict(item) for item in context["heading_path"]],
                "heading_context_basis": context["heading_context_basis"],
                "reference_page_numbers": list(context["reference_page_numbers"]),
                "reference_page_numbers_truncated": context[
                    "reference_page_numbers_truncated"
                ],
            }
        return paths, body_contexts, note_metadata, reference_paths

    def _heading_paths(self) -> dict[str, list[dict[str, Any]]]:
        return self._heading_metadata()[0]

    def _note_context_fields(
        self, node: _Node, note_metadata: dict[str, dict[str, Any]]
    ) -> dict[str, Any]:
        if not self._is_note_tag(node.tag):
            return {}
        metadata = note_metadata.get(
            node.element_id,
            {
                "heading_path": [],
                "heading_context_basis": "unknown",
                "reference_page_numbers": [],
                "reference_page_numbers_truncated": False,
            },
        )
        return {
            "heading_path": [dict(item) for item in metadata["heading_path"]],
            "heading_context_basis": metadata["heading_context_basis"],
            "reference_page_numbers": list(metadata["reference_page_numbers"]),
            "reference_page_numbers_truncated": metadata[
                "reference_page_numbers_truncated"
            ],
        }

    def notes(
        self, offset: int = 0, limit: int = 20, include_html: bool = False
    ) -> dict[str, Any]:
        offset_value, limit_value = _pagination(offset, limit, MAX_NOTES_LIMIT)
        if not isinstance(include_html, bool):
            raise _error("include_html must be a boolean")
        index = self._build_note_index()
        bodies: list[Tag] = index["bodies"]
        page = bodies[offset_value : offset_value + limit_value]
        body_refs: dict[int, list[Tag]] = index["refs"]
        body_backlinks: dict[int, list[Tag]] = index["backlinks"]
        _, body_contexts, _, reference_paths = self._heading_metadata()
        global_issues = index["global_issues"]
        bounded_issues = []
        used_response = 0
        for issue in global_issues[:MAX_NOTE_ISSUES]:
            size = len(json.dumps(issue, ensure_ascii=False))
            if used_response + size > MAX_NOTE_ISSUE_CHARS:
                break
            bounded_issues.append(issue)
            used_response += size
        used_response += 2048  # Envelope and list delimiters.
        used_html = 0
        items: list[dict[str, Any]] = []
        for body in page:
            body_id = self._nearest_candidate_id(body)
            body_text = _walk_all_visible_text(body)
            text, truncated = _bounded_text(body_text, MAX_NOTE_TEXT_CHARS)
            refs = body_refs.get(id(body), [])
            references: list[dict[str, Any]] = []
            for anchor in refs[:MAX_NOTE_REFERENCES]:
                ref_id = self._nearest_candidate_id(anchor)
                references.append(
                    {
                        "element_id": ref_id,
                        "html_id": self._bounded_attribute(anchor, "id"),
                        "href": str(anchor.get("href") or "")[:MAX_NOTE_LABEL_CHARS],
                        "scope_id": self._bounded_attribute(anchor, "data-footnote-scope"),
                        "label": (
                            _walk_all_visible_text(anchor)
                            or str(anchor.get("aria-label") or "")
                        )[:MAX_NOTE_LABEL_CHARS],
                        "page_numbers": list(self._page_numbers_for(anchor)),
                        "heading_path": [
                            dict(item)
                            for item in reference_paths.get(id(body), {}).get(
                                id(anchor), []
                            )
                        ],
                    }
                )
            backlinks = [
                {
                    "href": str(anchor.get("href") or "")[:MAX_NOTE_LABEL_CHARS],
                    "label": (
                        _walk_all_visible_text(anchor)
                        or str(anchor.get("aria-label") or "")
                    )[:MAX_NOTE_LABEL_CHARS],
                }
                for anchor in body_backlinks.get(id(body), [])[:MAX_NOTE_BACKLINKS]
            ]
            label = (
                str(
                    body.get("aria-label")
                    or body.get("title")
                    or body.get("data-footnote-label")
                    or body.get("data-label")
                    or ""
                )
                or (references[0]["label"] if references else "")
                or str(body.get("id") or "")
            )[:MAX_NOTE_LABEL_CHARS]
            item: dict[str, Any] = {
                "element_id": body_id,
                "html_id": self._bounded_attribute(body, "id"),
                "scope_id": self._bounded_attribute(body, "data-footnote-scope"),
                "label": label,
                "text": text,
                "truncated": truncated,
                "page_numbers": [],
                "reference_status": "referenced" if refs else "unreferenced",
                "reference_page_numbers": list(
                    body_contexts[id(body)]["reference_page_numbers"]
                ),
                "reference_page_numbers_truncated": body_contexts[id(body)][
                    "reference_page_numbers_truncated"
                ],
                "heading_path": [
                    dict(part) for part in body_contexts[id(body)]["heading_path"]
                ],
                "heading_context_basis": body_contexts[id(body)][
                    "heading_context_basis"
                ],
                "reference_count": len(refs),
                "references": references,
                "references_truncated": len(refs) > MAX_NOTE_REFERENCES,
                "backlinks": backlinks,
                "backlink_count": len(body_backlinks.get(id(body), [])),
                "backlinks_truncated": len(body_backlinks.get(id(body), []))
                > MAX_NOTE_BACKLINKS,
                "issues": list(dict.fromkeys(index["body_issues"].get(id(body), []))),
            }
            if include_html:
                html = str(body)
                if (
                    len(html) <= MAX_NOTE_HTML_CHARS
                    and used_html + len(html) <= MAX_NOTE_HTML_TOTAL
                ):
                    item["html"] = html
                    used_html += len(html)
                else:
                    item["html_omitted"] = True
            size = len(json.dumps(item, ensure_ascii=False))
            if used_response + size > MAX_NOTE_RESPONSE_CHARS:
                if not items:
                    raise _error(
                        "first note evidence exceeds response budget; fetch smaller elements"
                    )
                break
            used_response += size
            items.append(item)
        result: dict[str, Any] = {
            "items": items,
            "total_notes": len(bodies),
            "next_offset": offset_value + len(items)
            if offset_value + len(items) < len(bodies)
            else None,
            "issues": bounded_issues,
            "issues_truncated": len(global_issues) > len(bounded_issues),
        }
        return result

    @staticmethod
    def _first_match(
        text: str, query: str, case_sensitive: bool, whole_word: bool = False
    ) -> tuple[int, int] | None:
        return next(_literal_matches(text, query, case_sensitive, whole_word), None)

    @staticmethod
    def _snippet(text: str, match: tuple[int, int]) -> str:
        start, end = match
        if len(text) <= MAX_SEARCH_SNIPPET_CHARS:
            return text
        radius = (MAX_SEARCH_SNIPPET_CHARS - (end - start)) // 2
        left = max(0, start - radius)
        right = min(len(text), end + radius)
        if right - left < MAX_SEARCH_SNIPPET_CHARS:
            if left == 0:
                right = min(len(text), MAX_SEARCH_SNIPPET_CHARS)
            elif right == len(text):
                left = max(0, len(text) - MAX_SEARCH_SNIPPET_CHARS)
        return text[left:right][:MAX_SEARCH_SNIPPET_CHARS]

    def search(
        self,
        query: str,
        case_sensitive: bool = False,
        before: int = 0,
        after: int = 0,
        offset: int = 0,
        limit: int = 20,
        scope: str = "all",
        page_number: int | None = None,
        include_html: bool = False,
        whole_word: bool = False,
    ) -> dict[str, Any]:
        if not isinstance(query, str) or not query or len(query) > 1000:
            raise _error("query must be a non-empty string of at most 1000 characters")
        if not isinstance(case_sensitive, bool):
            raise _error("case_sensitive must be a boolean")
        if type(whole_word) is not bool:
            raise _error("whole_word must be a boolean")
        if not isinstance(scope, str) or scope not in {
            "all",
            "body",
            "headings",
            "notes",
        }:
            raise _error("scope is invalid")
        if page_number is not None:
            page_number = _strict_int(page_number, "page_number")
            if page_number <= 0:
                raise _error("page_number must be positive")
        if not isinstance(include_html, bool):
            raise _error("include_html must be a boolean")
        before_value = _strict_int(before, "before")
        after_value = _strict_int(after, "after")
        if before_value < 0 or after_value < 0:
            raise _error("before and after must be non-negative")
        offset_value, limit_value = _pagination(offset, limit, MAX_SEARCH_LIMIT)
        searchable = self._search_nodes()
        matches: list[tuple[_Node, tuple[int, int]]] = []
        for node in searchable:
            if not self._scope_matches(node, scope):
                continue
            if page_number is not None and page_number not in self._node_filter_pages(
                node
            ):
                continue
            found = self._first_match(node.own_text, query, case_sensitive, whole_word)
            if found is not None:
                matches.append((node, found))
        page = matches[offset_value : offset_value + limit_value]
        items: list[dict[str, Any]] = []
        searchable_indexes = {
            node.element_id: index for index, node in enumerate(searchable)
        }
        heading_paths, _, note_metadata, _ = self._heading_metadata()
        context_budget = [0]
        for node, match in page:
            index = searchable_indexes[node.element_id]
            before_nodes = searchable[max(0, index - min(before_value, 3)) : index]
            after_nodes = searchable[index + 1 : index + 1 + min(after_value, 3)]
            node_text = node.own_text
            item: dict[str, Any] = {
                "element_id": node.element_id,
                "tag": str(node.tag.name),
                "classes": _bounded_classes_for_output(node.tag),
                "page_numbers": self._page_list(node),
                "text_length": len(node_text),
                "snippet": self._snippet(node_text, match),
                "match_offset": match[0],
                "matched_text": node_text[match[0] : match[1]],
                "heading_path": heading_paths.get(node.element_id, []),
                "nearby": {
                    "before": [
                        self._context_item(
                            item,
                            include_html=include_html,
                            budget=context_budget,
                            note_metadata=note_metadata,
                        )
                        for item in before_nodes
                    ],
                    "after": [
                        self._context_item(
                            item,
                            include_html=include_html,
                            budget=context_budget,
                            note_metadata=note_metadata,
                        )
                        for item in after_nodes
                    ],
                },
            }
            item.update(self._note_context_fields(node, note_metadata))
            if include_html:
                serialized = str(node.tag)
                if (
                    len(serialized) <= MAX_CONTEXT_HTML_CHARS
                    and context_budget[0] + len(serialized) <= MAX_CONTEXT_HTML_TOTAL
                ):
                    item["html"] = serialized
                    context_budget[0] += len(serialized)
                else:
                    item["html_omitted"] = True
            items.append(item)
        next_offset = (
            offset_value + len(page)
            if offset_value + len(page) < len(matches)
            else None
        )
        return {
            "items": items,
            "total_matches": len(matches),
            "search_options": {
                "query": query,
                "case_sensitive": case_sensitive,
                "whole_word": whole_word,
                "scope": scope,
                "page_number": page_number,
                "offset": offset_value,
                "limit": limit_value,
            },
            "next_offset": next_offset,
        }

    def _context_item(
        self,
        node: _Node,
        *,
        include_html: bool = False,
        budget: list[int] | None = None,
        note_metadata: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        text = node.own_text
        item: dict[str, Any] = {
            "element_id": node.element_id,
            "tag": str(node.tag.name),
            "page_numbers": self._page_list(node),
            "text": text[:MAX_SEARCH_SNIPPET_CHARS],
            "truncated": len(text) > MAX_SEARCH_SNIPPET_CHARS,
        }
        if note_metadata is not None:
            item.update(self._note_context_fields(node, note_metadata))
        if include_html:
            serialized = str(node.tag)
            used = budget[0] if budget else 0
            if (
                len(serialized) <= MAX_CONTEXT_HTML_CHARS
                and used + len(serialized) <= MAX_CONTEXT_HTML_TOTAL
            ):
                item["html"] = serialized
                if budget is not None:
                    budget[0] = used + len(serialized)
            else:
                item["html_omitted"] = True
        return item

    def fetch(
        self,
        element_ids: list[str] | None = None,
        offset: int = 0,
        limit: int = 20,
        scope: str = "all",
        page_number: int | None = None,
        before: int = 0,
        after: int = 0,
    ) -> dict[str, Any]:
        offset_value, limit_value = _pagination(offset, limit, MAX_FETCH_LIMIT)
        if not isinstance(scope, str) or scope not in {
            "all",
            "body",
            "headings",
            "notes",
        }:
            raise _error("scope is invalid")
        if page_number is not None:
            page_number = _strict_int(page_number, "page_number")
            if page_number <= 0:
                raise _error("page_number must be positive")
        before_value = _strict_int(before, "before")
        after_value = _strict_int(after, "after")
        if before_value < 0 or after_value < 0:
            raise _error("before and after must be non-negative")
        if element_ids is not None and not isinstance(element_ids, list):
            raise _error("element_ids must be a list or null")
        if element_ids is not None and len(element_ids) > MAX_EXPLICIT_ELEMENT_IDS:
            raise _error("element_ids is limited to 200 items")
        searchable = self._search_nodes()
        searchable_ids = {node.element_id for node in searchable}
        if element_ids is None:
            # The default sequence is intended for progressive reading.  A
            # structural container whose own text is empty is represented by
            # its text-bearing descendants on later calls; callers may still
            # request that container explicitly when they need its markup.
            selected = [
                node
                for node in self._nodes
                if (
                    (
                        node.element_id in searchable_ids
                        or self._is_leaf_review_node(node)
                    )
                    and self._scope_matches(node, scope)
                    and (
                        page_number is None
                        or page_number in self._node_filter_pages(node)
                    )
                )
            ]
        else:
            selected_ids: set[str] = set()
            for element_id in element_ids:
                self._require_node(element_id)
                selected_ids.add(element_id)
            selected = [
                node
                for node in self._nodes
                if node.element_id in selected_ids
                and self._scope_matches(node, scope)
                and (
                    page_number is None or page_number in self._node_filter_pages(node)
                )
            ]
        total_selected = len(selected)
        selected = selected[offset_value:]
        entries: list[dict[str, Any]] = []
        used_html = 0
        context_budget = [0]
        heading_paths, _, note_metadata, _ = self._heading_metadata()
        for node in selected[:limit_value]:
            serialized = str(node.tag)
            if len(serialized) > MAX_FETCH_HTML_CHARS and not entries:
                raise _error("first element is too large; fetch smaller descendants")
            if used_html + len(serialized) > MAX_FETCH_HTML_CHARS:
                break
            text = _walk_all_visible_text(node.tag)
            searchable_index = next(
                (
                    index
                    for index, item in enumerate(searchable)
                    if item.element_id == node.element_id
                ),
                None,
            )
            nearby_before = (
                searchable[
                    max(0, searchable_index - min(before_value, 3)) : searchable_index
                ]
                if searchable_index is not None
                else []
            )
            nearby_after = (
                searchable[
                    searchable_index + 1 : searchable_index + 1 + min(after_value, 3)
                ]
                if searchable_index is not None
                else []
            )
            if (
                len(serialized)
                + len(text)
                + sum(
                    len(str(entry.get("html") or ""))
                    + len(str(entry.get("text") or ""))
                    for entry in entries
                )
                > MAX_FETCH_RESPONSE_CHARS
            ):
                break
            entry = {
                    "element_id": node.element_id,
                    "tag": str(node.tag.name),
                    "classes": _bounded_classes_for_output(node.tag),
                    "page_numbers": self._page_list(node),
                    "html": serialized,
                    "text": text,
                    "heading_path": heading_paths.get(node.element_id, []),
                    "nearby": {
                        "before": [
                            self._context_item(
                                item,
                                include_html=True,
                                budget=context_budget,
                                note_metadata=note_metadata,
                            )
                            for item in nearby_before
                        ],
                        "after": [
                            self._context_item(
                                item,
                                include_html=True,
                                budget=context_budget,
                                note_metadata=note_metadata,
                            )
                            for item in nearby_after
                        ],
                    },
                }
            entry.update(self._note_context_fields(node, note_metadata))
            entries.append(entry)
            used_html += len(serialized)
        consumed = len(entries)
        more = offset_value + consumed < total_selected
        if (
            consumed == 0
            and selected
            and len(str(selected[0].tag)) > MAX_FETCH_HTML_CHARS
        ):
            raise _error("first element is too large; fetch smaller descendants")
        next_offset = offset_value + consumed if more else None
        return {
            "items": entries,
            "next_offset": next_offset,
            "incomplete": bool(more),
        }

    def _visible_text_nodes(self) -> list[NavigableString]:
        scope = self._page_scope
        nodes: list[NavigableString] = []
        marker_ids = {id(marker.tag) for marker in self._markers}
        for node in scope.descendants:
            if not isinstance(node, NavigableString) or isinstance(node, Comment):
                continue
            marker_parent: PageElement | None = node.parent
            under_marker = False
            while marker_parent is not None and marker_parent is not scope:
                if isinstance(marker_parent, Tag) and id(marker_parent) in marker_ids:
                    under_marker = True
                    break
                marker_parent = marker_parent.parent
            if under_marker:
                continue
            current: PageElement | None = node.parent
            hidden = False
            while current is not None and current is not scope:
                if isinstance(current, Tag):
                    name = str(current.name or "").casefold()
                    if (
                        name in _HIDDEN_TAGS
                        or current.has_attr("hidden")
                        or str(current.get("aria-hidden") or "").casefold() == "true"
                    ):
                        hidden = True
                        break
                current = current.parent
            if hidden or not self._in_page_scope(node):
                continue
            nodes.append(node)
        return nodes

    def _nearest_candidate_id(self, node: PageElement) -> str | None:
        current: PageElement | None = node if isinstance(node, Tag) else node.parent
        while current is not None:
            element_id = self._tag_id.get(id(current))
            if element_id is not None:
                return element_id
            current = current.parent
        return None

    def _candidate_ancestors(self, node: PageElement) -> list[Tag]:
        ancestors: list[Tag] = []
        current: PageElement | None = node if isinstance(node, Tag) else node.parent
        while current is not None:
            if isinstance(current, Tag) and id(current) in self._tag_id:
                ancestors.append(current)
            current = current.parent
        return ancestors

    def _boundary_text(self, marker: _Marker, context_chars: int) -> dict[str, Any]:
        marker_position = marker.position
        text_nodes = self._visible_text_nodes()
        left_nodes = [
            node
            for node in text_nodes
            if self._positions.get(id(node), -1) < marker_position
        ]
        right_nodes = [
            node
            for node in text_nodes
            if self._positions.get(id(node), -1) > marker_position
        ]
        left = self._joined_text(left_nodes)
        right = self._joined_text(right_nodes)
        left_excerpt = left[-context_chars:]
        right_excerpt = right[:context_chars]
        containing = self._nearest_candidate_id(marker.tag)
        joined = False
        joined_id: str | None = None
        meaningful_left = [node for node in left_nodes if str(node).strip()]
        meaningful_right = [node for node in right_nodes if str(node).strip()]
        left_id = (
            self._nearest_candidate_id(meaningful_left[-1]) if meaningful_left else None
        )
        right_id = (
            self._nearest_candidate_id(meaningful_right[0])
            if meaningful_right
            else None
        )
        candidate_ancestors = self._candidate_ancestors(marker.tag)
        # Sharing a section/quote/list container is not evidence that its two
        # paragraphs were joined. Both sides must belong to the same text-bearing
        # element containing the marker.
        candidates = [
            tag
            for tag in candidate_ancestors
            if self._tag_id.get(id(tag)) == left_id == right_id
        ]
        for candidate in candidates:
            candidate_position = self._positions.get(id(candidate), -1)
            candidate_end = max(
                (
                    self._positions.get(id(descendant), candidate_position)
                    for descendant in candidate.descendants
                ),
                default=candidate_position,
            )
            inside_before = _normalized_text(
                "".join(
                    str(node)
                    for node in text_nodes
                    if candidate_position
                    < self._positions.get(id(node), -1)
                    < marker_position
                )
            )
            inside_after = _normalized_text(
                "".join(
                    str(node)
                    for node in text_nodes
                    if marker_position
                    < self._positions.get(id(node), -1)
                    <= candidate_end
                )
            )
            if inside_before and inside_after:
                joined = True
                joined_id = self._tag_id.get(id(candidate))
                break
        previous_page: int | None = None
        for prior in self._markers:
            if prior.position >= marker.position:
                break
            if prior.page_number is not None:
                previous_page = prior.page_number
        nonconsecutive = (
            previous_page is None or marker.page_number != previous_page + 1
        )
        punctuation = bool(
            left_excerpt
            and right_excerpt
            and (left_excerpt[-1] in _PUNCTUATION or right_excerpt[0] in _PUNCTUATION)
        )
        inside_heading = any(
            _HEADING_RE.fullmatch(str(tag.name or ""))
            for tag in self._candidate_ancestors(marker.tag)
        )
        warnings: list[str] = []
        if inside_heading:
            warnings.append("inside_heading")
        if nonconsecutive:
            warnings.append("nonconsecutive_page_markers")
        if punctuation and joined:
            warnings.append("punctuation_at_join")
        left_element = self._id_to_node.get(left_id) if left_id else None
        right_element = self._id_to_node.get(right_id) if right_id else None
        return {
            "page_number": marker.page_number,
            "previous_retained_page_number": previous_page,
            "left_text": left_excerpt,
            "right_text": right_excerpt,
            "left_element_id": left_id,
            "right_element_id": right_id,
            "left_tag": str(left_element.tag.name) if left_element else None,
            "right_tag": str(right_element.tag.name) if right_element else None,
            "left_classes": _bounded_classes_for_output(left_element.tag)
            if left_element
            else [],
            "right_classes": _bounded_classes_for_output(right_element.tag)
            if right_element
            else [],
            "containing_element_id": containing,
            "joined_within_element": joined,
            "joined_element_id": joined_id,
            "warnings": warnings,
            "inside_heading": inside_heading,
            "nonconsecutive_page_markers": nonconsecutive,
            "punctuation_at_join": punctuation and joined,
        }

    def _joined_text(self, nodes: list[NavigableString]) -> str:
        parts: list[str] = []
        previous: int | None = None
        for node in nodes:
            ancestor = node.parent
            while ancestor is not None and str(ancestor.name) not in _BLOCK_TAGS:
                ancestor = ancestor.parent
            current = id(ancestor) if ancestor is not None else None
            if previous is not None and current != previous:
                parts.append(" ")
            parts.append(str(node))
            if str(node).strip():
                previous = current
        return _normalized_text("".join(parts))

    def boundaries(
        self,
        page_number: int | None = None,
        offset: int = 0,
        limit: int = 20,
        context_chars: int = 500,
    ) -> dict[str, Any]:
        offset_value, limit_value = _pagination(offset, limit, MAX_BOUNDARY_LIMIT)
        if page_number is not None:
            page_number = _strict_int(page_number, "page_number")
            if page_number <= 0:
                raise _error("page_number must be positive")
        context_value = _strict_int(context_chars, "context_chars")
        if not 100 <= context_value <= 2000:
            raise _error("context_chars is outside the allowed range")
        markers = [
            marker
            for marker in self._markers
            if marker.page_number is not None
            and marker.page_number > 1
            and (page_number is None or marker.page_number == page_number)
        ]
        page = [
            self._boundary_text(marker, context_value)
            for marker in markers[offset_value : offset_value + limit_value]
        ]
        next_offset = (
            offset_value + len(page)
            if offset_value + len(page) < len(markers)
            else None
        )
        return {"items": page, "total": len(markers), "next_offset": next_offset}

    @staticmethod
    def _replacement_nodes(replacement_html: str) -> list[PageElement]:
        if not isinstance(replacement_html, str) or not replacement_html.strip():
            raise _error("replacement_html must be non-empty")
        fragment = BeautifulSoup(replacement_html, "html.parser")
        if any(
            str(tag.name or "").casefold() in _WRAPPER_TAGS
            for tag in fragment.find_all(True)
        ):
            raise _error("replacement contains a document wrapper")
        nodes = [node for node in fragment.contents if not isinstance(node, Comment)]
        if not nodes or not any(
            isinstance(node, Tag) or str(node).strip() for node in nodes
        ):
            raise _error("replacement must contain content")
        return nodes

    @staticmethod
    def _scope_contains(scope: tuple[PageElement, ...], target: Tag) -> bool:
        return any(_is_descendant(target, root) for root in scope)

    def _replace_text_runs(self, target: Tag) -> list[list[NavigableString]]:
        runs: list[list[NavigableString]] = []
        current: list[NavigableString] = []

        def flush() -> None:
            nonlocal current
            if current:
                runs.append(current)
                current = []

        def walk(node: PageElement, *, root: bool = False) -> None:
            if isinstance(node, Comment):
                flush()
                return
            if isinstance(node, NavigableString):
                current.append(node)
                return
            if not isinstance(node, Tag):
                flush()
                return
            name = str(node.name or "").casefold()
            if (
                name in _HIDDEN_TAGS
                or node.has_attr("hidden")
                or str(node.get("aria-hidden") or "").casefold() == "true"
            ):
                flush()
                return
            if name in {"a", "br", "img"} or _is_page_marker(node) or _is_noteref(node):
                flush()
                return
            if not root and (name in _BLOCK_TAGS or id(node) in self._tag_id):
                flush()
                for child in node.contents:
                    walk(child)
                flush()
                return
            for child in node.contents:
                walk(child)

        walk(target, root=True)
        flush()
        return runs

    def _replacement_word_context(self, target: Tag) -> tuple[str, dict[int, int]]:
        """Protected inline content still participates in lexical boundaries."""
        parts: list[str] = []
        positions: dict[int, int] = {}
        offset = 0

        def append(text: str) -> None:
            nonlocal offset
            parts.append(text)
            offset += len(text)

        def walk(element: PageElement, *, root: bool = False) -> None:
            if isinstance(element, Comment):
                return
            if isinstance(element, NavigableString):
                positions[id(element)] = offset
                append(str(element))
                return
            if not isinstance(element, Tag):
                return
            name = str(element.name or "").casefold()
            if (
                name in _HIDDEN_TAGS
                or element.has_attr("hidden")
                or str(element.get("aria-hidden") or "").casefold() == "true"
                or _is_page_marker(element)
                or _is_noteref(element)
            ):
                return
            if name in {"br", "img"}:
                append(" ")
                return
            block = not root and name in _BLOCK_TAGS
            if block:
                append(" ")
            for child in element.contents:
                walk(child)
            if block:
                append(" ")

        walk(target, root=True)
        return "".join(parts), positions

    def _plan_text_replacement(
        self, element_id, payload, edit_index, replacement_index
    ):
        location = {
            "edit_index": edit_index,
            "element_id": element_id,
            "replacement_index": replacement_index,
        }
        if (
            not isinstance(payload, dict)
            or not {"find", "replacement", "expected_matches"} <= set(payload)
            or set(payload) - {"find", "replacement", "expected_matches", "whole_word"}
        ):
            raise _error(
                "replace_text must specify find, replacement, and expected_matches"
            )
        find, replacement = payload["find"], payload["replacement"]
        expected = _strict_int(payload["expected_matches"], "expected_matches")
        whole_word = payload.get("whole_word", False)
        if not isinstance(find, str) or not find or len(find) > 1000:
            raise _error(
                "replace_text find must be non-empty and at most 1000 characters"
            )
        if not isinstance(replacement, str) or len(replacement) > 10000:
            raise _error("replace_text replacement is too long")
        if not 1 <= expected <= 1000:
            raise _error("expected_matches is outside the allowed range")
        if type(whole_word) is not bool:
            raise _error("whole_word must be a boolean")
        node = self._require_node(element_id)
        changes = []
        actual = 0
        word_text, word_positions = (
            self._replacement_word_context(node.tag) if whole_word else ("", {})
        )
        for run in self._replace_text_runs(node.tag):
            text = "".join(str(item) for item in run)
            offsets = []
            cursor = 0
            for item in run:
                offsets.append((item, cursor, cursor + len(str(item))))
                cursor += len(str(item))
            for left, right in _literal_matches(text, find, True, whole_word):
                if whole_word:
                    first_node, first_start, _ = next(
                        entry for entry in offsets if left < entry[2]
                    )
                    last_node, last_start, _ = next(
                        entry for entry in offsets if right <= entry[2]
                    )
                    global_start = word_positions[id(first_node)] + left - first_start
                    global_end = word_positions[id(last_node)] + right - last_start
                    if (
                        global_start > 0
                        and _word_character(word_text[global_start - 1])
                    ) or (
                        global_end < len(word_text)
                        and _word_character(word_text[global_end])
                    ):
                        continue
                actual += 1
                # Count all hits, but bound retained edit data on rejected counts.
                if actual > 1000:
                    continue
                first = True
                for item, start, end in offsets:
                    if left < end and right > start:
                        changes.append(
                            (
                                item,
                                max(0, left - start),
                                min(end, right) - start,
                                replacement if first else "",
                            )
                        )
                        first = False
        if actual != expected:
            raise _EditError(
                [
                    {
                        **location,
                        "code": "match_count_mismatch",
                        "expected_matches": expected,
                        "actual_matches": actual,
                        "message": "replace_text match count does not equal expected_matches",
                    }
                ]
            )
        return _PreparedEdit(
            element_id,
            node.tag,
            (node.tag,),
            None,
            None,
            replace_find=find,
            replace_replacement=replacement,
            replace_expected_matches=expected,
            text_changes=tuple(changes),
            edit_index=edit_index,
            replacement_index=replacement_index,
        )

    def _prepare_edits(self, edits: list[dict[str, Any]]) -> list[_PreparedEdit]:
        if not isinstance(edits, list) or len(edits) > 50:
            raise _error("edits must be a list of at most 50 items")
        prepared = []
        diagnostics = []
        for edit_index, edit in enumerate(edits):
            element_id = edit.get("element_id") if isinstance(edit, dict) else None
            try:
                if not isinstance(edit, dict):
                    raise _error("each edit must be an object")
                node = self._require_node(element_id)
                if "replace_text" in edit or "replace_texts" in edit:
                    operation = (
                        "replace_texts" if "replace_texts" in edit else "replace_text"
                    )
                    if set(edit) != {"element_id", operation}:
                        raise _error("each edit must specify exactly one operation")
                    payloads = (
                        edit[operation]
                        if operation == "replace_texts"
                        else [edit[operation]]
                    )
                    if not isinstance(payloads, list) or not 1 <= len(payloads) <= 50:
                        raise _error("replace_texts requires 1-50 replacements")
                    for replacement_index, payload in enumerate(payloads):
                        try:
                            prepared.append(
                                self._plan_text_replacement(
                                    element_id, payload, edit_index, replacement_index
                                )
                            )
                        except _EditError as exc:
                            diagnostics.extend(exc.diagnostics)
                        except ValueError as exc:
                            diagnostics.append(
                                {
                                    "edit_index": edit_index,
                                    "element_id": element_id,
                                    "replacement_index": replacement_index,
                                    "code": "invalid_replacement",
                                    "message": str(exc),
                                }
                            )
                elif "demote_heading" in edit:
                    if (
                        set(edit) != {"element_id", "demote_heading"}
                        or edit["demote_heading"] is not True
                    ):
                        raise _error(
                            "heading demotion requires only demote_heading=true"
                        )
                    if not _HEADING_RE.fullmatch(node.tag.name or ""):
                        raise _error("demote_heading requires an existing heading")
                    prepared.append(
                        _PreparedEdit(
                            element_id,
                            node.tag,
                            (node.tag,),
                            None,
                            None,
                            demote_heading=True,
                            edit_index=edit_index,
                        )
                    )
                else:
                    planned = self._prepare_structural_edits([edit])[0]
                    if planned.replacement_html is not None:
                        before = BeautifulSoup(
                            "".join(str(root) for root in planned.scope), "html.parser"
                        )
                        after = BeautifulSoup(planned.replacement_html, "html.parser")
                        for signature in (
                            self._id_multiset,
                            self._pagebreak_id_multiset,
                            self._image_src_multiset,
                            self._noteref_multiset,
                        ):
                            if signature(before) != signature(after):
                                raise _error(
                                    "edit would change protected document identifiers"
                                )
                    prepared.append(replace(planned, edit_index=edit_index))
            except ValueError as exc:
                diagnostics.append(
                    {
                        "edit_index": edit_index,
                        "element_id": element_id,
                        "code": "invalid_operation",
                        "message": str(exc),
                    }
                )
        # Compare text spans with a sweep, not a quadratic hit-by-hit product.
        # Structural conflicts remain element-scoped, including parent/child edits.
        by_element = {}
        by_text_node = {}
        for item in prepared:
            by_element.setdefault(item.element_id, []).append(item)
            for text_node, left, right, _ in item.text_changes:
                by_text_node.setdefault(id(text_node), []).append((left, right, item))

        def conflict(current, other):
            diagnostic = {
                "edit_index": other.edit_index,
                "element_id": other.element_id,
                "replacement_index": other.replacement_index,
                "conflicting_edit_index": current.edit_index,
                "conflicting_replacement_index": current.replacement_index,
                "code": "overlapping_edits",
                "message": "edits overlap",
            }
            if diagnostic not in diagnostics:
                diagnostics.append(diagnostic)

        for spans in by_text_node.values():
            previous = None
            for left, right, item in sorted(spans, key=lambda span: (span[0], span[1])):
                if previous is not None and left < previous[1]:
                    conflict(previous[2], item)
                if previous is None or right > previous[1]:
                    previous = (left, right, item)
        representatives = []
        for group in by_element.values():
            structural = next(
                (item for item in group if item.replace_find is None), None
            )
            if structural is not None and len(group) > 1:
                for item in group:
                    if item is not structural:
                        conflict(structural, item)
            representatives.append(structural or group[0])
        for index, current in enumerate(representatives):
            for other in representatives[index + 1 :]:
                if any(
                    (isinstance(b, Tag) and self._scope_contains((a,), b))
                    or (isinstance(a, Tag) and self._scope_contains((b,), a))
                    for a in current.scope
                    for b in other.scope
                ):
                    conflict(current, other)
        if diagnostics:
            raise _EditError(diagnostics)
        return prepared

    def _prepare_structural_edits(
        self, edits: list[dict[str, Any]]
    ) -> list[_PreparedEdit]:
        if not isinstance(edits, list) or len(edits) > 50:
            raise _error("edits must be a list of at most 50 items")
        prepared: list[_PreparedEdit] = []
        allowed_replacement = {"element_id", "end_element_id", "replacement_html"}
        allowed_heading = {"element_id", "end_element_id", "heading_level"}
        allowed_delete = {"element_id", "delete_element", "expected_text"}
        for edit in edits:
            if not isinstance(edit, dict):
                raise _error("each edit must be an object")
            element_id = edit.get("element_id")
            if not isinstance(element_id, str):
                raise _error("unknown element id")
            node = self._require_node(element_id)
            has_replacement = "replacement_html" in edit
            has_heading = "heading_level" in edit
            has_delete = "delete_element" in edit
            if sum((has_replacement, has_heading, has_delete)) != 1:
                raise _error("each edit must specify exactly one operation")
            if has_delete:
                allowed = allowed_delete
            elif has_replacement:
                allowed = allowed_replacement
            else:
                allowed = allowed_heading
            if set(edit) - allowed:
                raise _error("edit contains an unknown field")
            if has_delete:
                if edit.get("delete_element") is not True:
                    raise _error("delete_element must be true")
                expected_text = edit.get("expected_text")
                if not isinstance(expected_text, str):
                    raise _error("expected_text must be a string")
                if expected_text != _walk_all_visible_text(node.tag):
                    raise _error("expected_text does not match element text")
                protected = any(
                    isinstance(descendant, Tag)
                    and (
                        descendant.has_attr("id")
                        or str(descendant.name or "").casefold() == "img"
                        or _is_page_marker(descendant)
                        or _is_noteref(descendant)
                    )
                    for descendant in [node.tag, *node.tag.find_all(True)]
                )
                if protected:
                    raise _error("protected element cannot be deleted")
                prepared.append(
                    _PreparedEdit(
                        element_id,
                        node.tag,
                        (node.tag,),
                        None,
                        None,
                        True,
                    )
                )
                continue
            if has_heading:
                if edit.get("end_element_id") is not None:
                    raise _error("heading-level edits cannot use a range")
                level = _strict_int(edit.get("heading_level"), "heading_level")
                if not 1 <= level <= 6 or not _HEADING_RE.fullmatch(
                    node.tag.name or ""
                ):
                    raise _error("heading_level requires an existing heading")
                prepared.append(
                    _PreparedEdit(element_id, node.tag, (node.tag,), None, level)
                )
                continue
            replacement_html = edit.get("replacement_html")
            if not isinstance(replacement_html, str):
                raise _error("replacement_html must be non-empty")
            DocumentReview._replacement_nodes(replacement_html)
            end_id = edit.get("end_element_id")
            if end_id is None:
                scope: tuple[PageElement, ...] = (node.tag,)
            else:
                if not isinstance(end_id, str):
                    raise _error("unknown element id")
                end_node = self._require_node(end_id)
                if (
                    node.tag.parent is None
                    or node.tag.parent is not end_node.tag.parent
                ):
                    raise _error("edit range must use sibling elements")
                siblings = list(node.tag.parent.contents)
                try:
                    start_index = _identity_index(siblings, node.tag)
                    end_index = _identity_index(siblings, end_node.tag)
                except (AttributeError, TypeError) as exc:
                    raise _error("invalid edit range") from exc
                if start_index < 0 or end_index < 0:
                    raise _error("invalid edit range")
                if start_index > end_index:
                    raise _error("edit range is reversed")
                scope = tuple(siblings[start_index : end_index + 1])
            prepared.append(
                _PreparedEdit(element_id, node.tag, scope, replacement_html, None)
            )
        return prepared

    @staticmethod
    def _id_multiset(soup: BeautifulSoup) -> Counter[str]:
        return Counter(
            str(tag.get("id")) for tag in soup.find_all(True) if tag.has_attr("id")
        )

    @staticmethod
    def _pagebreak_id_multiset(soup: BeautifulSoup) -> Counter[str]:
        return Counter(
            str(tag.get("id"))
            for tag in soup.find_all(True)
            if _is_page_marker(tag) and tag.has_attr("id")
        )

    @staticmethod
    def _image_src_multiset(soup: BeautifulSoup) -> Counter[str]:
        return Counter(
            str(tag.get("src")) for tag in soup.find_all("img") if tag.has_attr("src")
        )

    @staticmethod
    def _noteref_multiset(soup: BeautifulSoup) -> Counter[tuple[str, str]]:
        # Keep anonymous references too: preserving IDs alone cannot prevent an
        # edit from leaving a note body orphaned. Labels may be corrected, but
        # this edit operation does not remove or retarget source notes.
        return Counter(
            (str(tag.get("id") or ""), str(tag.get("href") or ""))
            for tag in soup.find_all(True)
            if _is_noteref(tag)
        )

    def remove_page_furniture(
        self, roles: list[str], page_numbers: list[int] | None = None
    ) -> tuple[str, dict[str, Any]]:
        """Remove only explicitly classified running matter, never infer from text.

        Referenced targets, page anchors, images and notes are retained even when
        nested inside a mistakenly classified header/footer. The original tree
        remains unchanged so previews and repeated calls are deterministic.
        """
        if (
            not isinstance(roles, list)
            or not roles
            or any(not isinstance(role, str) or role not in {"header", "footer"} for role in roles)
            or len(set(roles)) != len(roles)
        ):
            raise _error("roles must contain header, footer, or both without duplicates")
        if page_numbers is not None and (
            not isinstance(page_numbers, list)
            or not page_numbers
            or len(page_numbers) > 10000
            or any(type(page) is not int or page < 1 for page in page_numbers)
            or len(set(page_numbers)) != len(page_numbers)
        ):
            raise _error("page_numbers must be distinct positive physical page numbers")
        pages = set(page_numbers) if page_numbers is not None else None
        selected_classes = {f"page_{role}" for role in roles}
        tags = list(self._soup.find_all(True))
        referenced_ids: set[str] = set()
        for tag in tags:
            for attribute in ("href", "xlink:href"):
                target = tag.get(attribute)
                if isinstance(target, str) and target.startswith("#"):
                    referenced_ids.add(unquote(target[1:]))
            for attribute in ("aria-describedby", "aria-labelledby", "headers"):
                values = tag.get(attribute, [])
                referenced_ids.update(values.split() if isinstance(values, str) else values)
        candidates = [
            tag for tag in tags
            if selected_classes.intersection(tag.get("class", [])) and not _under_head(tag)
        ]
        accepted: set[int] = set()
        removed = {role: 0 for role in roles}
        affected_pages: set[int] = set()
        examples: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        skipped_count = 0
        for tag in candidates:
            if any(id(parent) in accepted for parent in tag.parents):
                continue
            tag_pages = self._page_numbers_for(tag)
            if pages is not None and (not tag_pages or not set(tag_pages).issubset(pages)):
                continue
            descendants = [tag, *tag.find_all(True)]
            reason = None
            if any(
                item.name in _WRAPPER_TAGS
                or _is_page_marker(item)
                or _is_noteref(item)
                or self._is_note_tag(item)
                or item.name == "img"
                for item in descendants
            ):
                reason = "contains_protected_structure"
            elif any(item.get("id") in referenced_ids for item in descendants):
                reason = "contains_referenced_target"
            role = next(role for role in roles if f"page_{role}" in tag.get("class", []))
            example = {
                "role": role,
                "page_numbers": list(tag_pages),
                "text": _WHITESPACE_RE.sub(" ", tag.get_text(" ", strip=True))[:240],
            }
            if reason:
                skipped_count += 1
                if len(skipped) < 12:
                    skipped.append({**example, "reason": reason})
                continue
            accepted.add(id(tag))
            removed[role] += 1
            affected_pages.update(tag_pages)
            if len(examples) < 12:
                examples.append(example)
        successor = copy.deepcopy(self._soup)
        cloned_tags = list(successor.find_all(True))
        if len(tags) != len(cloned_tags):
            raise _error("unable to prepare cleanup")
        for original, cloned in zip(tags, cloned_tags):
            if id(original) in accepted:
                cloned.extract()
        return str(successor), {
            "roles": sorted(roles),
            "scope": "document" if pages is None else "pages",
            "page_numbers": sorted(pages) if pages is not None else None,
            "removed_count": len(accepted),
            "removed_by_role": removed,
            "affected_page_numbers": sorted(affected_pages),
            "examples": examples,
            "examples_truncated": len(accepted) > len(examples),
            "skipped_count": skipped_count,
            "skipped": skipped,
            "skipped_truncated": skipped_count > len(skipped),
        }

    def apply_edits(self, edits: list[dict[str, Any]]) -> str:
        prepared = self._prepare_edits(edits)
        original_ids = self._id_multiset(self._soup)
        original_pagebreak_ids = self._pagebreak_id_multiset(self._soup)
        original_image_srcs = self._image_src_multiset(self._soup)
        original_noterefs = self._noteref_multiset(self._soup)

        successor = copy.deepcopy(self._soup)
        original_elements = list(self._soup.descendants)
        cloned_elements = list(successor.descendants)
        if len(original_elements) != len(cloned_elements):
            raise _error("unable to prepare edited document")
        clone_lookup = {
            id(original): cloned
            for original, cloned in zip(original_elements, cloned_elements)
        }
        cloned_candidates = self._find_candidate_tags(successor)
        if len(cloned_candidates) != len(self._candidate_tags):
            raise _error("unable to prepare edited document")
        cloned_by_id = {
            node.element_id: cloned_candidates[node.order] for node in self._nodes
        }
        actions: list[tuple[_PreparedEdit, Tag]] = [
            (edit, cloned_by_id[edit.element_id]) for edit in prepared
        ]
        text_changes: dict[int, list[tuple[int, int, str]]] = {}
        for edit in prepared:
            for original, left, right, replacement in edit.text_changes:
                text_changes.setdefault(id(original), []).append(
                    (left, right, replacement)
                )
        for original_id, changes in text_changes.items():
            cloned = clone_lookup[original_id]
            value = str(cloned)
            for left, right, replacement in sorted(changes, reverse=True):
                value = value[:left] + replacement + value[right:]
            cloned.replace_with(NavigableString(value))
        for edit, target in actions:
            if edit.heading_level is not None:
                target.name = f"h{edit.heading_level}"
                continue
            if edit.delete_element:
                target.extract()
                continue
            if edit.replace_find is not None:
                continue
            if edit.demote_heading:
                target.name = "p"
                if not (
                    len(target.contents) == 1
                    and isinstance(target.contents[0], Tag)
                    and target.contents[0].name in {"strong", "b"}
                ):
                    emphasis = successor.new_tag("strong")
                    for child in list(target.contents):
                        emphasis.append(child.extract())
                    target.append(emphasis)
                continue
            replacement_nodes = self._replacement_nodes(edit.replacement_html or "")
            clone_scope = [clone_lookup.get(id(root)) for root in edit.scope]
            if any(root is None for root in clone_scope):
                raise _error("unable to prepare edited range")
            clone_scope = [root for root in clone_scope if root is not None]
            if not clone_scope:
                raise _error("unable to prepare edited range")
            parent = clone_scope[0].parent
            if parent is None:
                raise _error("unable to prepare edited range")
            insert_index = _identity_index(list(parent.contents), clone_scope[0])
            if insert_index < 0:
                raise _error("unable to prepare edited range")
            for old in clone_scope:
                old.extract()
            for index, replacement in enumerate(replacement_nodes):
                parent.insert(insert_index + index, replacement)

        if (
            self._id_multiset(successor) != original_ids
            or self._pagebreak_id_multiset(successor) != original_pagebreak_ids
            or self._image_src_multiset(successor) != original_image_srcs
            or self._noteref_multiset(successor) != original_noterefs
        ):
            raise _error("edit would change protected document identifiers")
        return str(successor)


__all__ = ["DocumentReview"]
