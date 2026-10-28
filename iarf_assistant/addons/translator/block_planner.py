"""Lossless, deterministic source planning for ``document-block-v1``.

The planner intentionally does not round-trip HTML through a DOM serializer.
It indexes the original character string and only stores spans into that exact
string.  This is a small strict parser for already-completed semantic HTML,
not a browser-compatible error-recovery parser.
"""

from __future__ import annotations

import math
import re
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from html import unescape
from itertools import pairwise

from carlina.core.semantic_html import (
    SAFE_LANGUAGE_RE,
    SEMANTIC_DOCUMENT_TAGS,
    allowed_attributes,
)

from .block_contract import (
    BLOCK_PLANNER_VERSION,
    BLOCK_PLANNER_V2,
    CORRECTED_BLOCK_PLANNER_VERSION,
    LEGACY_BLOCK_PLANNER_VERSION,
    MAX_SOURCE_SENTENCE_BOUNDARIES,
    MAX_SOURCE_SENTENCE_PROJECTIONS,
    SENTENCE_ANCHOR_PROFILE_VERSION,
    AttestedDocumentSnapshot,
    BlockContractError,
    BlockWirePart,
    CoverageEntry,
    CoverageWitness,
    ProtectedInlineToken,
    SourceAnchor,
    SourceProviderAttestation,
    SourceSlot,
    TranslationBlock,
    TranslationSourcePlan,
    canonical_json,
    normalize_source_sentence_manifest,
    sha256_text,
)
from .source_segmentation import (
    SOURCE_SENTENCE_MANIFEST_KEY,
    SOURCE_SENTENCE_PROFILE,
    SOURCE_SENTENCE_VERSION,
    SentenceSegmenter,
    manifest_spans,
    segment_projection,
)

_VOID = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)
_RAW_TEXT = frozenset({"script", "style"})
_BLOCK_TAG_ROLES = {
    "p": "paragraph",
    "li": "list_item",
    "dt": "definition_term",
    "dd": "definition_body",
    "caption": "table_caption",
    "th": "table_header",
    "td": "table_cell",
    "figcaption": "figure_caption",
    "blockquote": "blockquote",
}
_CONTAINERS = frozenset(
    {
        "body",
        "main",
        "article",
        "section",
        "div",
        "aside",
        "header",
        "footer",
        "figure",
        "blockquote",
        "li",
        "td",
        "th",
    }
)
_INLINE = frozenset(
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
        "mark",
        "q",
        "s",
        "small",
        "span",
        "strong",
        "sub",
        "sup",
        "u",
        "wbr",
        "code",
        "kbd",
        "samp",
        "var",
        "time",
    }
)
_LITERAL_TAGS = frozenset({"code", "pre", "samp", "kbd", "var", "time", "template"})
_EXCLUDED_TAGS = frozenset({"script", "style", "noscript", "svg", "math"})
_LEXICAL_ATTRIBUTES = frozenset({"alt", "title", "aria-label", "data-label"})
_LEGACY_LEXICAL_ATTRIBUTES = _LEXICAL_ATTRIBUTES | {"data-footnote-label"}
_REFERENCE_ATTRIBUTES = frozenset(
    {
        "id",
        "href",
        "src",
        "for",
        "headers",
        "scope",
        "role",
        "epub:type",
        "type",
        "class",
        "name",
        "rel",
        "aria-describedby",
        "aria-labelledby",
    }
)
_TAG_RE = re.compile(r"[A-Za-z][A-Za-z0-9:_-]*")
_ATTR_RE = re.compile(r"[A-Za-z_:][A-Za-z0-9:_.-]*")
_ENTITY_RE = re.compile(r"&(?:#[0-9]+|#[xX][0-9A-Fa-f]+|[A-Za-z][A-Za-z0-9]+);")
_TEMPLATE_RE = re.compile(r"\{\{[^{}]{1,256}\}\}")
_WS_RE = re.compile(r"^\s+$", re.DOTALL)
_SENTENCE_RE = re.compile(r"(?<=[.!?\u061f\u3002\uff01\uff1f])(?=\s+|$)")
_PRINTED_RE = re.compile(
    r"(?:pagebreak|page-break|printed|locator|pagenum)", re.IGNORECASE
)
_MARGINALIA_RE = re.compile(
    r"(?:^|\s)(?:page|running)[_-](?:header|footer)(?:\s|$)", re.IGNORECASE
)
_MAX_NESTING = 256
_MAX_NODES = 100_000
_MAX_TEXT_NODES = 200_000
_MAX_ATTRIBUTES = 100_000
TRANSLATION_MAX_BLOCK_CHARS_SETTING = "translation_max_block_chars"
DEFAULT_TRANSLATION_MAX_BLOCK_CHARS = 4_000
MIN_TRANSLATION_MAX_BLOCK_CHARS = 500
MAX_TRANSLATION_MAX_BLOCK_CHARS = 20_000

try:  # local, deterministic enhancement; conservative fallback remains valid.
    import pysbd
except ImportError:  # pragma: no cover
    pysbd = None


def _bounded_settings(value: object, *, depth: int = 0) -> object:
    if depth > 4:
        raise BlockContractError("plan-settings-depth")
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        if len(value.encode("utf-8")) > 4096:
            raise BlockContractError("plan-settings-scalar")
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise BlockContractError("plan-settings-type")
        return value
    if isinstance(value, Mapping):
        if len(value) > 128 or any(
            not isinstance(key, str) or not key or len(key.encode("utf-8")) > 128
            for key in value
        ):
            raise BlockContractError("plan-settings-items")
        return {
            key: (
                normalize_source_sentence_manifest(item)
                if depth == 0 and key == SOURCE_SENTENCE_MANIFEST_KEY
                else _bounded_settings(item, depth=depth + 1)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        if len(value) > 128:
            raise BlockContractError("plan-settings-items")
        return [_bounded_settings(item, depth=depth + 1) for item in value]
    raise BlockContractError("plan-settings-type")


@dataclass(slots=True)
class _Attr:
    name: str
    name_start: int
    name_end: int
    full_start: int
    full_end: int
    value: str | None
    value_start: int | None
    value_end: int | None


@dataclass(slots=True)
class _Node:
    tag: str
    start: int
    start_tag_end: int
    end: int | None = None
    end_tag_start: int | None = None
    attrs: list[_Attr] = field(default_factory=list)
    parent: _Node | None = None
    children: list[_Node | _Text] = field(default_factory=list)
    sibling_index: int = 1

    def attr(self, name: str) -> str | None:
        for attr in self.attrs:
            if attr.name.lower() == name and attr.value is not None:
                return attr.value
        return None

    def has_attr(self, name: str) -> bool:
        return any(attr.name.lower() == name for attr in self.attrs)

    def classes(self) -> set[str]:
        return set((self.attr("class") or "").lower().split())


@dataclass(slots=True)
class _Text:
    start: int
    end: int
    raw: str
    parent: _Node | None
    entity: bool
    ordinal: int = 1


class _LosslessParser:
    def __init__(self, html: str):
        self.html = html
        self.roots: list[_Node | _Text] = []
        self.stack: list[_Node] = []
        self.text_nodes: list[_Text] = []
        self.doctype_spans: list[tuple[int, int]] = []
        self.node_count = 0
        self.attribute_count = 0

    def parse(self) -> tuple[list[_Node | _Text], list[_Text]]:
        cursor = 0
        length = len(self.html)
        while cursor < length:
            if self.html.startswith("<!--", cursor):
                end = self.html.find("-->", cursor + 4)
                if end < 0:
                    raise BlockContractError("html-comment-unclosed")
                cursor = end + 3
                continue
            if self.html[cursor : cursor + 9].lower() == "<!doctype":
                end = self._tag_end(cursor)
                self.doctype_spans.append((cursor, end + 1))
                cursor = end + 1
                continue
            if self.html.startswith("<!", cursor) or self.html.startswith("<?", cursor):
                raise BlockContractError("html-active-syntax")
            if self.html[cursor] == "<":
                if cursor + 1 < length and self.html[cursor + 1] == "/":
                    cursor = self._close_tag(cursor)
                    continue
                cursor = self._open_tag(cursor)
                continue
            next_tag = self.html.find("<", cursor)
            end = length if next_tag < 0 else next_tag
            self._text(cursor, end)
            cursor = end
        if self.stack:
            raise BlockContractError("html-stack-unclosed")
        self._assign_ordinals(self.roots)
        return self.roots, self.text_nodes

    def _tag_end(self, start: int) -> int:
        quote: str | None = None
        for index in range(start + 1, len(self.html)):
            char = self.html[index]
            if quote:
                if char == quote:
                    quote = None
            elif char in "\"'":
                quote = char
            elif char == ">":
                return index
        raise BlockContractError("html-tag-unclosed")

    def _open_tag(self, start: int) -> int:
        end = self._tag_end(start)
        inner = self.html[start + 1 : end]
        match = _TAG_RE.match(inner.lstrip())
        if match is None:
            raise BlockContractError("html-tag-syntax")
        offset = len(inner) - len(inner.lstrip())
        name = match.group(0).lower()
        if name not in SEMANTIC_DOCUMENT_TAGS:
            raise BlockContractError("html-tag-unknown")
        pos = offset + match.end()
        attrs = self._attrs(inner, pos)
        seen: set[str] = set()
        for attr in attrs:
            normalized = attr.name.lower()
            if normalized in seen:
                raise BlockContractError("html-attribute-duplicate")
            seen.add(normalized)
            if normalized not in allowed_attributes(name):
                raise BlockContractError("html-attribute-unknown")
        stripped = inner.rstrip()
        self_close = name in _VOID and (
            (not attrs and stripped.endswith("/"))
            or (
                bool(attrs)
                and len(stripped) >= 2
                and stripped[-2].isspace()
                and stripped.endswith("/")
            )
        )
        if name not in _VOID and stripped.endswith("/"):
            raise BlockContractError("html-self-closing-nonvoid")
        node = _Node(
            tag=name,
            start=start,
            start_tag_end=end + 1,
            attrs=attrs,
            parent=self.stack[-1] if self.stack else None,
        )
        self._append(node)
        if name not in _VOID and not self_close:
            if len(self.stack) >= _MAX_NESTING:
                raise BlockContractError("html-nesting-limit")
            self.stack.append(node)
        else:
            node.end = end + 1
        return end + 1

    def _close_tag(self, start: int) -> int:
        end = self._tag_end(start)
        inner = self.html[start + 2 : end].strip()
        if not _TAG_RE.fullmatch(inner):
            raise BlockContractError("html-close-syntax")
        if not self.stack or self.stack[-1].tag != inner.lower():
            raise BlockContractError("html-stack-mismatch")
        node = self.stack.pop()
        node.end_tag_start = start
        node.end = end + 1
        return end + 1

    def _attrs(self, inner: str, position: int) -> list[_Attr]:
        attrs: list[_Attr] = []
        length = len(inner)
        while position < length:
            while position < length and inner[position].isspace():
                position += 1
            if position >= length:
                break
            if inner[position] == "/":
                if inner[position:].strip() == "/":
                    break
                raise BlockContractError("html-attribute-trailing")
            name_match = _ATTR_RE.match(inner, position)
            if name_match is None:
                raise BlockContractError("html-attribute-syntax")
            full_start = position
            name = name_match.group(0)
            name_start = position
            position = name_match.end()
            name_end = position
            while position < length and inner[position].isspace():
                position += 1
            value: str | None = None
            value_start: int | None = None
            value_end: int | None = None
            if position < length and inner[position] == "=":
                position += 1
                while position < length and inner[position].isspace():
                    position += 1
                if position >= length:
                    raise BlockContractError("html-attribute-value")
                if inner[position] in "\"'":
                    quote = inner[position]
                    position += 1
                    start = position
                    while position < length and inner[position] != quote:
                        position += 1
                    if position >= length:
                        raise BlockContractError("html-attribute-value")
                    value = inner[start:position]
                    value_start = start
                    value_end = position
                    position += 1
                else:
                    start = position
                    while position < length and not inner[position].isspace():
                        position += 1
                    value = inner[start:position]
                    value_start = start
                    value_end = position
                    if not value:
                        raise BlockContractError("html-attribute-value")
            attrs.append(
                _Attr(
                    name=name,
                    name_start=name_start,
                    name_end=name_end,
                    full_start=full_start,
                    full_end=position,
                    value=value,
                    value_start=value_start,
                    value_end=value_end,
                )
            )
            self.attribute_count += 1
            if self.attribute_count > _MAX_ATTRIBUTES:
                raise BlockContractError("html-attribute-limit")
        # convert offsets from tag-inner to source offsets is deferred in _open_tag.
        tag_start = 0  # retained for readability: adjusted by caller below.
        for attr in attrs:
            if attr.value_start is not None:
                attr.value_start += tag_start
                attr.value_end = (attr.value_end or 0) + tag_start
        return attrs

    def _append(self, node: _Node | _Text) -> None:
        if isinstance(node, _Node):
            self.node_count += 1
            if self.node_count > _MAX_NODES:
                raise BlockContractError("html-node-limit")
        if self.stack:
            self.stack[-1].children.append(node)
        else:
            self.roots.append(node)

    def _text(self, start: int, end: int) -> None:
        raw = self.html[start:end]
        cursor = 0
        for match in _ENTITY_RE.finditer(raw):
            if match.start() > cursor:
                self._append_text(start + cursor, start + match.start(), False)
            self._append_text(start + match.start(), start + match.end(), True)
            cursor = match.end()
        if cursor < len(raw):
            self._append_text(start + cursor, end, False)

    def _append_text(self, start: int, end: int, entity: bool) -> None:
        if start == end:
            return
        text = _Text(
            start,
            end,
            self.html[start:end],
            self.stack[-1] if self.stack else None,
            entity,
        )
        if len(self.text_nodes) >= _MAX_TEXT_NODES:
            raise BlockContractError("html-text-limit")
        self._append(text)
        self.text_nodes.append(text)

    def _assign_ordinals(self, children: list[_Node | _Text]) -> None:
        element_counts: dict[str, int] = {}
        text_count = 0
        entity_count = 0
        for child in children:
            if isinstance(child, _Node):
                element_counts[child.tag] = element_counts.get(child.tag, 0) + 1
                child.sibling_index = element_counts[child.tag]
                self._assign_ordinals(child.children)
            elif child.entity:
                entity_count += 1
                child.ordinal = entity_count
            else:
                text_count += 1
                child.ordinal = text_count


def _source_offsets(node: _Node, html: str) -> None:
    """Correct attr offsets after parsing without ever reconstructing a tag."""
    for attr in node.attrs:
        # _attrs stored offsets inside the tag payload.  Retain the exact
        # attribute name/full spans even for boolean and empty attributes.
        payload_start = node.start + 1
        attr.name_start += payload_start
        attr.name_end += payload_start
        attr.full_start += payload_start
        attr.full_end += payload_start
        if attr.value_start is not None:
            attr.value_start += payload_start
            attr.value_end = (attr.value_end or 0) + payload_start
    for child in node.children:
        if isinstance(child, _Node):
            _source_offsets(child, html)


def _iter_nodes(items: Iterable[_Node | _Text]) -> Iterable[_Node]:
    for item in items:
        if isinstance(item, _Node):
            yield item
            yield from _iter_nodes(item.children)


def _node_locator(node: _Node) -> str:
    parts: list[str] = []
    current: _Node | None = node
    while current is not None:
        parts.append(f"{current.tag}[{current.sibling_index}]")
        current = current.parent
    return "/".join(reversed(parts))


def _text_locator(text: _Text) -> str:
    base = _node_locator(text.parent) if text.parent else "document"
    return f"{base}/{'entity' if text.entity else 'text'}()[{text.ordinal}]"


def _has_ancestor(node: _Node | None, tags: frozenset[str]) -> bool:
    while node:
        if node.tag in tags:
            return True
        node = node.parent
    return False


def _has_class_ancestor(node: _Node | None, pattern: re.Pattern[str]) -> bool:
    while node:
        if pattern.search(" ".join(node.classes())):
            return True
        node = node.parent
    return False


def _is_hidden(node: _Node | None) -> bool:
    while node:
        if (
            node.has_attr("hidden")
            or (node.attr("aria-hidden") or "").lower() == "true"
        ):
            return True
        if "hidden" in node.classes():
            return True
        node = node.parent
    return False


def _noteref(node: _Node | None) -> bool:
    while node:
        role = (node.attr("role") or "").lower()
        epub = (node.attr("epub:type") or "").lower()
        if role == "doc-noteref" or "noteref" in epub:
            return True
        node = node.parent
    return False


def _role(node: _Node) -> str | None:
    if "toc-entry-title" in node.classes():
        return "source_toc_entry"
    role = (node.attr("role") or "").lower()
    epub = (node.attr("epub:type") or "").lower()
    if (
        role in {"doc-footnote", "doc-endnote"}
        or "footnote" in epub
        or "endnote" in epub
    ):
        return "note_body"
    if _note_ancestor(node) and node.tag in {"blockquote", "dd", "dt", "li", "p"}:
        return "note_body"
    if node.tag == "p":
        inherited = _inherited_prose_role(node.parent)
        if inherited is not None:
            return inherited
    if re.fullmatch(r"h[1-6]", node.tag):
        return "heading"
    if node.tag == "title":
        return "document_title"
    if node.tag in _BLOCK_TAG_ROLES:
        return _BLOCK_TAG_ROLES[node.tag]
    return None


def _inherited_prose_role(node: _Node | None) -> str | None:
    """Preserve the nearest structural prose role for a nested paragraph."""

    while node:
        role = (node.attr("role") or "").lower()
        epub = (node.attr("epub:type") or "").lower()
        if (
            role in {"doc-footnote", "doc-endnote"}
            or "footnote" in epub
            or "endnote" in epub
        ):
            return "note_body"
        inherited = {
            "li": "list_item",
            "blockquote": "blockquote",
            "dd": "definition_body",
            "dt": "definition_term",
            "td": "table_cell",
            "th": "table_header",
            "figcaption": "figure_caption",
            "caption": "table_caption",
        }.get(node.tag)
        if inherited is not None:
            return inherited
        if re.fullmatch(r"h[1-6]", node.tag) or node.tag in {"p", "title"}:
            return None
        node = node.parent
    return None


def _note_ancestor(node: _Node | None) -> bool:
    while node:
        role = (node.attr("role") or "").lower()
        epub = (node.attr("epub:type") or "").lower()
        if (
            role in {"doc-footnote", "doc-endnote"}
            or "footnote" in epub
            or "endnote" in epub
        ):
            return True
        node = node.parent
    return False


def _nested_candidate(node: _Node, candidate: _Node) -> bool:
    parent = node.parent
    while parent:
        if parent is candidate:
            return True
        parent = parent.parent
    return False


def _is_descendant_or_self(node: _Node | None, ancestor: _Node) -> bool:
    while node:
        if node is ancestor:
            return True
        node = node.parent
    return False


def _nearest_role(node: _Node | None, candidates: set[int]) -> _Node | None:
    while node:
        if id(node) in candidates:
            return node
        node = node.parent
    return None


def _text_policy(text: _Text, *, decode_entities: bool = False) -> tuple[str, str]:
    parent = text.parent
    if _is_hidden(parent) or _has_ancestor(parent, _EXCLUDED_TAGS):
        return "excluded-with-reason", "hidden-or-nonprose"
    if _has_class_ancestor(parent, _MARGINALIA_RE):
        return "excluded-with-reason", "running-marginalia"
    if _WS_RE.fullmatch(unescape(text.raw) if decode_entities else text.raw):
        return "structural-only", "whitespace"
    if _has_ancestor(parent, _LITERAL_TAGS):
        return "literal-preserved", "literal-element"
    if _has_class_ancestor(parent, _PRINTED_RE):
        return "literal-preserved", "printed-locator"
    if _noteref(parent):
        return "literal-preserved", "note-reference"
    if _has_class_ancestor(parent, re.compile(r"toc-page-label", re.IGNORECASE)):
        return "literal-preserved", "toc-page-label"
    return "translatable", "semantic-prose"


def _text_pieces(text: _Text, *, decode_entities: bool = False) -> list[tuple[int, int, str, str]]:
    """Split ordinary template syntax without changing any source byte."""
    disposition, policy = _text_policy(text, decode_entities=decode_entities)
    if disposition != "translatable" or text.entity:
        return [(text.start, text.end, disposition, policy)]
    result: list[tuple[int, int, str, str]] = []
    cursor = 0
    for match in _TEMPLATE_RE.finditer(text.raw):
        if match.start() > cursor:
            result.append(
                (text.start + cursor, text.start + match.start(), disposition, policy)
            )
        result.append(
            (
                text.start + match.start(),
                text.start + match.end(),
                "literal-preserved",
                "template-token",
            )
        )
        cursor = match.end()
    if cursor < len(text.raw):
        result.append((text.start + cursor, text.end, disposition, policy))
    return result or [(text.start, text.end, disposition, policy)]


def _foreign_language(node: _Node | None, effective_language: str) -> bool:
    expected = effective_language.lower().split("-", 1)[0]
    if expected == "und":
        return False
    while node:
        declared = node.attr("lang") or node.attr("xml:lang")
        if declared and SAFE_LANGUAGE_RE.fullmatch(declared):
            return declared.lower().split("-", 1)[0] != expected
        node = node.parent
    return False


def _attribute_policy(
    node: _Node,
    attr: _Attr,
    *,
    planner_version: str,
) -> tuple[str, str] | None:
    name = attr.name.lower()
    if attr.value is None or not attr.value.strip():
        return "structural-only", "empty-or-boolean-attribute"
    if name in {"lang", "xml:lang"}:
        return "structural-only", "target-derived-language"
    lexical_attributes = (
        _LEGACY_LEXICAL_ATTRIBUTES
        if planner_version == LEGACY_BLOCK_PLANNER_VERSION
        else _LEXICAL_ATTRIBUTES
    )
    if name in lexical_attributes:
        if (
            _is_hidden(node)
            or _noteref(node)
            or _has_class_ancestor(node, _PRINTED_RE)
            or _has_class_ancestor(node, _MARGINALIA_RE)
        ):
            return "literal-preserved", "protected-accessibility"
        return "translatable", "accessibility-lexical"
    if name in _REFERENCE_ATTRIBUTES or name.startswith(("data-", "aria-")):
        return "structural-only", "immutable-reference"
    return "structural-only", "untranslated-structural-attribute"


def _candidate_nodes(
    roots: list[_Node | _Text], texts: list[_Text]
) -> list[tuple[_Node, str]]:
    nodes = list(_iter_nodes(roots))
    candidates: list[tuple[_Node, str]] = []
    for node in nodes:
        role = _role(node)
        if role is not None:
            candidates.append((node, role))
    return sorted(
        candidates,
        key=lambda pair: (
            pair[0].start,
            pair[0].end or pair[0].start,
            _node_locator(pair[0]),
        ),
    )


def _direct_prose_runs(
    roots: list[_Node | _Text], candidate_ids: set[int]
) -> list[tuple[str, _Node, list[_Text]]]:
    """Return non-overlapping runs of direct prose around child block nodes."""

    runs: list[tuple[str, _Node, list[_Text]]] = []

    root_texts = [
        text
        for text in roots
        if isinstance(text, _Text) and _text_policy(text)[0] != "excluded-with-reason"
    ]
    if root_texts:
        document = _Node(
            tag="document", start=0, start_tag_end=0, end=root_texts[-1].end
        )
        runs.append(("document/direct_prose()[1]", document, root_texts))

    def inline_texts(child: _Node | _Text) -> list[_Text]:
        if isinstance(child, _Text):
            return [child] if _text_policy(child)[0] != "excluded-with-reason" else []
        if child.tag in _CONTAINERS or _role(child) is not None:
            return []
        result: list[_Text] = []
        for grandchild in child.children:
            result.extend(inline_texts(grandchild))
        return result

    for parent in _iter_nodes(roots):
        if parent.tag not in _CONTAINERS or _is_hidden(parent):
            continue
        run: list[_Text] = []
        index = 0
        for child in [*parent.children, None]:
            if isinstance(child, _Node) and child.tag in {"br", "wbr"} and run:
                # A line/wrap opportunity inside direct prose is structure within
                # the run, not a semantic block boundary. Its exact token/slot is
                # attached when the run is materialized below.
                continue
            candidate_texts = (
                []
                if child is None
                else [
                    text
                    for text in inline_texts(child)
                    if _text_policy(text)[0] != "excluded-with-reason"
                    and (
                        _nearest_role(text.parent, candidate_ids) is None
                        or (
                            _nearest_role(text.parent, candidate_ids) is parent
                            and any(
                                id(descendant) in candidate_ids
                                for descendant in _iter_nodes(parent.children)
                            )
                        )
                    )
                ]
            )
            if candidate_texts:
                run.extend(candidate_texts)
                continue
            if run:
                index += 1
                key = f"{_node_locator(parent)}/direct_prose()[{index}]"
                runs.append((key, parent, run))
                run = []
    return runs


def _slot_id(namespace: uuid.UUID, locator: str, start: int, kind: str) -> str:
    return str(uuid.uuid5(namespace, f"slot:{kind}:{locator}:{start}"))


def _build_projection(
    slots: list[SourceSlot], html: str
) -> tuple[str, tuple[SourceSlot, ...]]:
    pieces: list[str] = []
    offset = 0
    result: list[SourceSlot] = []
    for slot in sorted(slots, key=lambda item: (item.raw_start, item.raw_end, item.id)):
        raw = html[slot.raw_start : slot.raw_end]
        if slot.disposition == "excluded-with-reason":
            result.append(slot)
            continue
        value = "\n" if slot.kind == "break" else unescape(raw)
        start = offset
        offset += len(value)
        pieces.append(value)
        result.append(
            SourceSlot(
                id=slot.id,
                locator=slot.locator,
                kind=slot.kind,
                disposition=slot.disposition,
                policy_code=slot.policy_code,
                raw_start=slot.raw_start,
                raw_end=slot.raw_end,
                projection_start=start,
                projection_end=offset,
                source_value=slot.source_value,
                source_hash=slot.source_hash,
            )
        )
    return "".join(pieces), tuple(result)


def _break_slots(
    namespace: uuid.UUID, node: _Node, html: str, *, narrow_span: bool = False
) -> list[SourceSlot]:
    result: list[SourceSlot] = []
    for current in _iter_nodes([node]):
        if current.tag != "br" or current is node:
            continue
        locator = _node_locator(current)
        # The protected void token preserves the complete tag. A v3 break slot
        # needs only a source witness for its newline projection; owning the
        # attributes too would overlap their independent coverage entries.
        raw_end = current.start + 1 if narrow_span else current.start_tag_end
        result.append(
            SourceSlot(
                id=_slot_id(namespace, locator, current.start, "break"),
                locator=locator,
                kind="break",
                disposition="structural-only",
                policy_code="line-break",
                raw_start=current.start,
                raw_end=raw_end,
                projection_start=None,
                projection_end=None,
                source_value=html[current.start : raw_end],
                source_hash=sha256_text(html[current.start : raw_end]),
            )
        )
    return result


def _anchors(
    namespace: uuid.UUID,
    block_id: str,
    projection: str,
    language: str,
    *,
    manifest_ends: Sequence[int] | None = None,
) -> tuple[SourceAnchor, ...]:
    if not projection:
        return ()
    if manifest_ends is not None:
        spans = manifest_spans(projection, manifest_ends)
        splitter_profile = SOURCE_SENTENCE_PROFILE
        splitter_version = SOURCE_SENTENCE_VERSION
    else:
        splitter_profile = "fallback"
        splitter_version = SENTENCE_ANCHOR_PROFILE_VERSION
        boundaries = [0]
        supported = language.split("-", 1)[0].lower() in {
            "en",
            "pl",
            "de",
            "fr",
            "es",
            "it",
            "pt",
            "nl",
            "ru",
            "uk",
            "tr",
        }
        if supported and pysbd is not None:
            try:
                splitter = pysbd.Segmenter(
                    language=language.split("-", 1)[0].lower(), clean=False, char_span=True
                )
                proposed = [(item.start, item.end) for item in splitter.segment(projection)]
                if (
                    proposed
                    and proposed[0][0] == 0
                    and proposed[-1][1] == len(projection)
                    and all(a[1] == b[0] for a, b in pairwise(proposed))
                ):
                    spans = proposed
                    splitter_profile = "pysbd-char-span"
                    splitter_version = str(getattr(pysbd, "__version__", "unknown"))
                else:
                    spans = []
            except AttributeError, TypeError, ValueError:
                spans = []
        else:
            spans = []
        if not spans and supported:
            boundaries.extend(match.end() for match in _SENTENCE_RE.finditer(projection))
        boundaries.append(len(projection))
        boundaries = sorted(
            {boundary for boundary in boundaries if 0 <= boundary <= len(projection)}
        )
        if not spans:
            spans = [(start, end) for start, end in pairwise(boundaries) if end > start]
        if (
            not spans
            or spans[0][0] != 0
            or spans[-1][1] != len(projection)
            or any(a[1] != b[0] for a, b in pairwise(spans))
        ):
            spans = [(0, len(projection))]
    return tuple(
        SourceAnchor(
            id=str(uuid.uuid5(namespace, f"anchor:{block_id}:{start}:{end}")),
            order=index,
            start=start,
            end=end,
            source_hash=sha256_text(projection[start:end]),
            splitter_profile=splitter_profile,
            splitter_version=splitter_version,
        )
        for index, (start, end) in enumerate(spans, 1)
    )


def _inline_tokens(
    namespace: uuid.UUID, node: _Node, html: str, owner: _Node, candidate_ids: set[int]
) -> tuple[ProtectedInlineToken, ...]:
    tokens: list[ProtectedInlineToken] = []
    for current in _iter_nodes([node]):
        if (
            current is owner
            or current.tag not in _INLINE
            or not _is_descendant_or_self(current, owner)
            or (
                _nearest_role(current.parent, candidate_ids) is not None
                and _nearest_role(current.parent, candidate_ids) is not owner
            )
        ):
            continue
        locator = _node_locator(current)
        element_id = str(uuid.uuid5(namespace, f"element:{locator}:{current.start}"))
        policy = (
            "fixed"
            if current.tag in {"a", "br", "wbr"} or _noteref(current)
            else "movable"
        )
        kind = "void" if current.tag in _VOID else "open"
        tokens.append(
            ProtectedInlineToken(
                id=str(
                    uuid.uuid5(namespace, f"inline:{locator}:{current.start}:{kind}")
                ),
                kind=kind,
                tag=current.tag,
                locator=locator,
                movement_policy=policy,
                raw_start=current.start,
                raw_end=current.start_tag_end,
                element_id=element_id,
                source_value=html[current.start : current.start_tag_end],
                source_hash=sha256_text(html[current.start : current.start_tag_end]),
            )
        )
        if (
            kind == "open"
            and current.end_tag_start is not None
            and current.end is not None
        ):
            tokens.append(
                ProtectedInlineToken(
                    id=str(
                        uuid.uuid5(
                            namespace, f"inline:{locator}:{current.end_tag_start}:close"
                        )
                    ),
                    kind="close",
                    tag=current.tag,
                    locator=locator,
                    movement_policy=policy,
                    raw_start=current.end_tag_start,
                    raw_end=current.end,
                    element_id=element_id,
                    source_value=html[current.end_tag_start : current.end],
                    source_hash=sha256_text(html[current.end_tag_start : current.end]),
                )
            )
    return tuple(
        sorted(tokens, key=lambda token: (token.raw_start, token.raw_end, token.id))
    )


def _validate_coverage(
    html: str, entries: list[CoverageEntry], blocks: list[TranslationBlock]
) -> None:
    spans = sorted(
        entries, key=lambda entry: (entry.raw_start, entry.raw_end, entry.locator)
    )
    if entries != spans:
        raise BlockContractError("coverage-order")
    if any(left.raw_end > right.raw_start for left, right in pairwise(spans)):
        raise BlockContractError("coverage-overlap")
    if len(
        {(entry.locator, entry.raw_start, entry.raw_end) for entry in entries}
    ) != len(entries):
        raise BlockContractError("coverage-duplicate")
    block_ids = {block.id for block in blocks}
    for entry in entries:
        if sha256_text(html[entry.raw_start : entry.raw_end]) != entry.source_hash:
            raise BlockContractError("coverage-source-hash")
        if entry.block_id is not None and entry.block_id not in block_ids:
            raise BlockContractError("coverage-foreign-owner")
        if entry.disposition == "translatable" and entry.block_id not in block_ids:
            raise BlockContractError("coverage-unowned-translatable")
        if entry.disposition == "excluded-with-reason" and entry.block_id is not None:
            raise BlockContractError("coverage-excluded-owner")


def _wire(
    slots: tuple[SourceSlot, ...], tokens: tuple[ProtectedInlineToken, ...]
) -> tuple[BlockWirePart, ...]:
    parts = [
        (slot.raw_start, slot.raw_end, "slot", slot.id)
        for slot in slots
        if slot.kind != "break"
    ] + [(token.raw_start, token.raw_end, "token", token.id) for token in tokens]
    # Attribute values are contained in tag tokens: the tag is the unique render
    # owner, so retain the attribute value reference after its enclosing opening tag.
    return tuple(
        BlockWirePart(kind=kind, ref_id=ref_id) for _, _, kind, ref_id in sorted(parts)
    )


def _raw_cut_is_inline_safe(block: TranslationBlock, cut: int) -> bool:
    """Return whether a raw split point keeps every protected inline element whole."""

    element_spans: dict[str, tuple[int, int]] = {}
    for token in block.protected_inline:
        start, end = element_spans.get(
            token.element_id, (token.raw_start, token.raw_end)
        )
        element_spans[token.element_id] = (
            min(start, token.raw_start),
            max(end, token.raw_end),
        )
    return all(not (start < cut < end) for start, end in element_spans.values())


def _raw_cut_for_projection_boundary(
    block: TranslationBlock,
    boundary: int,
    html: str,
) -> int | None:
    """Map one sentence boundary to a safe raw-source cut, if representable."""

    if not 0 < boundary < len(block.projection):
        return None
    slots = sorted(
        block.slots,
        key=lambda slot: (
            slot.projection_start if slot.projection_start is not None else -1,
            slot.projection_end if slot.projection_end is not None else -1,
            slot.raw_start,
        ),
    )
    for slot in slots:
        start = slot.projection_start
        end = slot.projection_end
        if start is None or end is None or not (start < boundary < end):
            continue
        if slot.kind != "text":
            return None
        raw = html[slot.raw_start : slot.raw_end]
        projected = unescape(raw)
        if projected != block.projection[start:end] or len(projected) != len(raw):
            return None
        cut = slot.raw_start + (boundary - start)
        return cut if _raw_cut_is_inline_safe(block, cut) else None

    left = [
        slot.raw_end
        for slot in slots
        if slot.projection_end is not None and slot.projection_end <= boundary
    ]
    right = [
        slot.raw_start
        for slot in slots
        if slot.projection_start is not None and slot.projection_start >= boundary
    ]
    low = max(left, default=block.source_start)
    high = min(right, default=block.source_end)
    if not block.source_start < high or not low < block.source_end or low > high:
        return None
    candidates = {low, high}
    for token in block.protected_inline:
        if low <= token.raw_start <= high:
            candidates.add(token.raw_start)
        if low <= token.raw_end <= high:
            candidates.add(token.raw_end)
    for cut in sorted(candidates):
        if block.source_start < cut < block.source_end and _raw_cut_is_inline_safe(
            block, cut
        ):
            return cut
    return None


def _split_anchor_groups(
    block: TranslationBlock,
    *,
    max_chars: int,
    html: str,
) -> tuple[tuple[SourceAnchor, ...], ...]:
    """Group whole sentence anchors into bounded, structurally safe children."""

    anchors = block.anchors
    if len(block.projection) <= max_chars or len(anchors) < 2:
        return (anchors,)
    safe_boundaries = {
        anchor.end: cut
        for anchor in anchors[:-1]
        if (cut := _raw_cut_for_projection_boundary(block, anchor.end, html))
        is not None
    }
    if not safe_boundaries:
        return (anchors,)

    groups: list[tuple[SourceAnchor, ...]] = []
    start = 0
    while start < len(anchors):
        if anchors[-1].end - anchors[start].start <= max_chars:
            groups.append(tuple(anchors[start:]))
            break
        within_limit = [
            index
            for index in range(start, len(anchors) - 1)
            if anchors[index].end - anchors[start].start <= max_chars
            and anchors[index].end in safe_boundaries
        ]
        if within_limit:
            end = within_limit[-1]
        else:
            later_safe = [
                index
                for index in range(start, len(anchors) - 1)
                if anchors[index].end in safe_boundaries
            ]
            if not later_safe:
                groups.append(tuple(anchors[start:]))
                break
            # A sentence or protected-inline scope itself exceeds the threshold.
            # Keep it whole and cut at the first safe boundary instead of corrupting
            # source or token structure. The ordinary oversized-window guard remains
            # the final fail-closed backstop for genuinely indivisible material.
            end = later_safe[0]
        groups.append(tuple(anchors[start : end + 1]))
        start = end + 1
    return tuple(groups)


def _slice_split_slot(
    block: TranslationBlock,
    slot: SourceSlot,
    *,
    projection_start: int,
    projection_end: int,
    raw_start: int,
    raw_end: int,
    html: str,
    namespace: uuid.UUID,
) -> SourceSlot | None:
    start = slot.projection_start
    end = slot.projection_end
    if (
        start is None
        or end is None
        or end <= projection_start
        or start >= projection_end
    ):
        return None
    sliced_start = max(start, projection_start)
    sliced_end = min(end, projection_end)
    slot_raw_start = slot.raw_start
    slot_raw_end = slot.raw_end
    if sliced_start != start or sliced_end != end:
        if slot.kind != "text":
            raise BlockContractError("plan-split-nontext-slot")
        raw = html[slot.raw_start : slot.raw_end]
        projected = unescape(raw)
        if projected != block.projection[start:end] or len(projected) != len(raw):
            raise BlockContractError("plan-split-slot-mapping")
        slot_raw_start += sliced_start - start
        slot_raw_end -= end - sliced_end
    if not raw_start <= slot_raw_start < slot_raw_end <= raw_end:
        raise BlockContractError("plan-split-slot-span")
    source_value = html[slot_raw_start:slot_raw_end]
    return SourceSlot(
        id=str(
            uuid.uuid5(
                namespace,
                f"split-slot:{block.id}:{slot.id}:{slot_raw_start}:{slot_raw_end}",
            )
        ),
        locator=slot.locator,
        kind=slot.kind,
        disposition=slot.disposition,
        policy_code=slot.policy_code,
        raw_start=slot_raw_start,
        raw_end=slot_raw_end,
        projection_start=sliced_start - projection_start,
        projection_end=sliced_end - projection_start,
        source_value=source_value,
        source_hash=sha256_text(source_value),
    )


def _split_oversized_block(
    block: TranslationBlock,
    *,
    max_chars: int,
    html: str,
    language: str,
    namespace: uuid.UUID,
) -> tuple[TranslationBlock, ...]:
    if block.role != "paragraph" or len(block.projection) <= max_chars:
        return (block,)
    groups = _split_anchor_groups(block, max_chars=max_chars, html=html)
    if len(groups) < 2:
        return (block,)

    projection_ranges = [(group[0].start, group[-1].end) for group in groups]
    raw_cuts: list[int] = []
    for _start, end in projection_ranges[:-1]:
        cut = _raw_cut_for_projection_boundary(block, end, html)
        if cut is None:
            raise BlockContractError("plan-split-boundary")
        raw_cuts.append(cut)
    raw_bounds = [block.source_start, *raw_cuts, block.source_end]
    if any(left >= right for left, right in pairwise(raw_bounds)):
        raise BlockContractError("plan-split-source-order")

    children: list[TranslationBlock] = []
    for index, ((projection_start, projection_end), group) in enumerate(
        zip(projection_ranges, groups, strict=True),
        1,
    ):
        child_raw_start = raw_bounds[index - 1]
        child_raw_end = raw_bounds[index]
        child_id = str(
            uuid.uuid5(
                namespace,
                f"block:split:{block.id}:{index}:{projection_start}:{projection_end}",
            )
        )
        suffix = f"/split[{index}]"
        child_locator = (
            f"{block.locator}{suffix}"
            if len(f"{block.locator}{suffix}") <= 2048
            else block.locator
        )
        child_projection = block.projection[projection_start:projection_end]
        child_slots = tuple(
            sliced
            for slot in block.slots
            if (
                sliced := _slice_split_slot(
                    block,
                    slot,
                    projection_start=projection_start,
                    projection_end=projection_end,
                    raw_start=child_raw_start,
                    raw_end=child_raw_end,
                    html=html,
                    namespace=namespace,
                )
            )
            is not None
        )
        child_tokens = tuple(
            token
            for token in block.protected_inline
            if child_raw_start <= token.raw_start and token.raw_end <= child_raw_end
        )
        if any(
            token.raw_start < child_raw_end
            and child_raw_start < token.raw_end
            and token not in child_tokens
            for token in block.protected_inline
        ):
            raise BlockContractError("plan-split-inline-span")
        child_ends = tuple(anchor.end - projection_start for anchor in group)
        children.append(
            TranslationBlock(
                id=child_id,
                order=1,
                role=block.role,
                locator=child_locator,
                source_start=child_raw_start,
                source_end=child_raw_end,
                projection=child_projection,
                projection_hash=sha256_text(child_projection),
                slots=child_slots,
                protected_inline=child_tokens,
                wire=_wire(child_slots, child_tokens),
                anchors=_anchors(
                    namespace,
                    child_id,
                    child_projection,
                    language,
                    manifest_ends=child_ends,
                ),
            )
        )
    return tuple(children)


def _split_oversized_paragraphs(
    blocks: list[TranslationBlock],
    coverage: list[CoverageEntry],
    *,
    html: str,
    language: str,
    namespace: uuid.UUID,
    max_chars: int | None,
) -> tuple[list[TranslationBlock], list[CoverageEntry]]:
    if max_chars is None:
        return blocks, coverage
    if (
        isinstance(max_chars, bool)
        or not isinstance(max_chars, int)
        or not MIN_TRANSLATION_MAX_BLOCK_CHARS
        <= max_chars
        <= MAX_TRANSLATION_MAX_BLOCK_CHARS
    ):
        raise BlockContractError("plan-max-block-chars")

    replacements: dict[str, tuple[TranslationBlock, ...]] = {}
    split_blocks: list[TranslationBlock] = []
    for block in blocks:
        children = _split_oversized_block(
            block,
            max_chars=max_chars,
            html=html,
            language=language,
            namespace=namespace,
        )
        split_blocks.extend(children)
        if len(children) > 1:
            replacements[block.id] = children
    if not replacements:
        return blocks, coverage

    split_coverage: list[CoverageEntry] = []
    for entry in coverage:
        children = replacements.get(entry.block_id or "")
        if children is None:
            split_coverage.append(entry)
            continue
        pieces = [
            (
                child,
                max(entry.raw_start, child.source_start),
                min(entry.raw_end, child.source_end),
            )
            for child in children
            if entry.raw_start < child.source_end and child.source_start < entry.raw_end
        ]
        pieces = [(child, start, end) for child, start, end in pieces if start < end]
        if not pieces:
            raise BlockContractError("plan-split-coverage-owner")
        if len(pieces) > 1 and entry.kind != "text":
            raise BlockContractError("plan-split-coverage-kind")
        for child, start, end in pieces:
            split_coverage.append(
                replace(
                    entry,
                    raw_start=start,
                    raw_end=end,
                    source_hash=sha256_text(html[start:end]),
                    block_id=child.id,
                )
            )
    split_coverage.sort(
        key=lambda entry: (entry.raw_start, entry.raw_end, entry.locator)
    )
    return split_blocks, split_coverage


def plan_document_blocks(
    snapshot_dto: Mapping[str, object] | AttestedDocumentSnapshot,
    *,
    project_id: str,
    source_attestation: Mapping[str, object] | SourceProviderAttestation,
    source_language: str,
    language_profile: str | None = None,
    settings: Mapping[str, object] | None = None,
    planner_version: str = BLOCK_PLANNER_VERSION,
    sentence_segmenter: SentenceSegmenter | None = None,
    sentence_batch_segmenter: Callable[[Sequence[str]], Sequence[Sequence[str]]] | None = None,
) -> TranslationSourcePlan:
    """Build a deterministic, fully witnessed source plan from an attested DTO."""

    snapshot = (
        snapshot_dto
        if isinstance(snapshot_dto, AttestedDocumentSnapshot)
        else AttestedDocumentSnapshot.from_dict(snapshot_dto)
    )
    attestation = (
        source_attestation
        if isinstance(source_attestation, SourceProviderAttestation)
        else SourceProviderAttestation.from_dict(source_attestation)
    )
    if attestation.resource_id != snapshot.document_id:
        raise BlockContractError("source-attestation-resource")
    has_sentence_manifest = isinstance(settings, Mapping) and (
        SOURCE_SENTENCE_MANIFEST_KEY in settings
    )
    if sentence_segmenter is not None:
        if has_sentence_manifest:
            raise BlockContractError("source-sentence-segmentation-manifest-conflict")
        if not callable(sentence_segmenter):
            raise BlockContractError("source-sentence-segmentation-invalid")
        base_plan = plan_document_blocks(
            snapshot,
            project_id=project_id,
            source_attestation=attestation,
            source_language=source_language,
            language_profile=language_profile,
            settings=settings,
            planner_version=planner_version,
        )
        projections: dict[str, str] = {}
        for block in base_plan.blocks:
            projection_hash = sha256_text(block.projection)
            previous = projections.get(projection_hash)
            if previous is not None and previous != block.projection:
                raise BlockContractError("source-sentence-segmentation-hash-collision")
            projections[projection_hash] = block.projection
        if len(projections) > MAX_SOURCE_SENTENCE_PROJECTIONS:
            raise BlockContractError("source-sentence-segmentation-manifest")
        manifest_projections: dict[str, tuple[int, ...]] = {}
        boundary_count = 0
        ordered_hashes = list(projections)
        if sentence_batch_segmenter is not None:
            if not callable(sentence_batch_segmenter):
                raise BlockContractError("source-sentence-segmentation-invalid")
            grouped = sentence_batch_segmenter(
                [projections[projection_hash] for projection_hash in ordered_hashes]
            )
            if (
                not isinstance(grouped, (list, tuple))
                or len(grouped) != len(ordered_hashes)
            ):
                raise BlockContractError("source-sentence-segmentation-invalid")
            for projection_hash, sentences in zip(ordered_hashes, grouped):
                if isinstance(sentences, (str, bytes, bytearray)) or not isinstance(
                    sentences, Sequence
                ):
                    raise BlockContractError("source-sentence-segmentation-invalid")
                fixed = tuple(sentences)
                ends = segment_projection(
                    projections[projection_hash],
                    lambda _projection, _fixed=fixed: _fixed,
                )
                boundary_count += len(ends)
                if boundary_count > MAX_SOURCE_SENTENCE_BOUNDARIES:
                    raise BlockContractError("source-sentence-segmentation-manifest")
                manifest_projections[projection_hash] = ends
            del ordered_hashes, grouped
        else:
            for projection_hash, projection in projections.items():
                ends = segment_projection(projection, sentence_segmenter)
                boundary_count += len(ends)
                if boundary_count > MAX_SOURCE_SENTENCE_BOUNDARIES:
                    raise BlockContractError("source-sentence-segmentation-manifest")
                manifest_projections[projection_hash] = ends
        final_settings = dict(settings) if settings is not None else {}
        final_settings[SOURCE_SENTENCE_MANIFEST_KEY] = {
            "contract_version": "source-sentence-manifest-v1",
            "projections": manifest_projections,
        }
        return plan_document_blocks(
            snapshot,
            project_id=project_id,
            source_attestation=attestation,
            source_language=source_language,
            language_profile=language_profile,
            settings=final_settings,
            planner_version=planner_version,
        )
    parser = _LosslessParser(snapshot.output_html)
    roots, texts = parser.parse()
    for root in roots:
        if isinstance(root, _Node):
            _source_offsets(root, snapshot.output_html)
    if not isinstance(project_id, str) or not project_id.strip():
        raise BlockContractError("plan-project-id")
    if not isinstance(source_language, str):
        raise BlockContractError("plan-source-language")
    if settings is not None and not isinstance(settings, Mapping):
        raise BlockContractError("plan-settings-type")
    if language_profile is not None and not isinstance(language_profile, str):
        raise BlockContractError("plan-language-profile")
    if not isinstance(planner_version, str) or planner_version not in {
        LEGACY_BLOCK_PLANNER_VERSION,
        BLOCK_PLANNER_V2,
        BLOCK_PLANNER_VERSION,
    }:
        raise BlockContractError("plan-planner-version")
    requested_language = source_language.strip()
    root_language = next(
        (
            node.attr("lang") or node.attr("xml:lang")
            for node in _iter_nodes(roots)
            if node.tag == "html"
        ),
        None,
    )
    if requested_language.lower() in {"auto", "auto-detect", "und", ""}:
        requested_language = (
            root_language
            if root_language and SAFE_LANGUAGE_RE.fullmatch(root_language)
            else "und"
        )
    if requested_language != "und" and not SAFE_LANGUAGE_RE.fullmatch(
        requested_language
    ):
        raise BlockContractError("plan-source-language")
    profile = (
        language_profile.strip()
        if isinstance(language_profile, str) and language_profile.strip()
        else requested_language.lower().split("-", 1)[0]
    )
    try:
        frozen_settings = _bounded_settings({} if settings is None else settings)
        fingerprint = sha256_text(canonical_json(frozen_settings))
    except (TypeError, ValueError) as exc:
        raise BlockContractError("plan-settings") from exc
    namespace = uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"carlina:{project_id}:{snapshot.document_id}:{snapshot.content_hash}:{snapshot.semantic_contract_version}:{planner_version}:{requested_language}:{profile}:{fingerprint}",
    )
    namespace_text = str(namespace)
    sentence_manifest = (
        normalize_source_sentence_manifest(
            frozen_settings[SOURCE_SENTENCE_MANIFEST_KEY]
        )
        if isinstance(frozen_settings, Mapping)
        and SOURCE_SENTENCE_MANIFEST_KEY in frozen_settings
        else None
    )
    manifest_projections = (
        sentence_manifest["projections"] if sentence_manifest is not None else None
    )
    used_manifest_hashes: set[str] = set()

    def manifest_ends_for(projection: str) -> Sequence[int] | None:
        if manifest_projections is None:
            return None
        projection_hash = sha256_text(projection)
        if projection_hash not in manifest_projections:
            raise BlockContractError("source-sentence-segmentation-manifest")
        used_manifest_hashes.add(projection_hash)
        return manifest_projections[projection_hash]

    candidates = _candidate_nodes(roots, texts)
    candidate_by_id = {id(node): (node, role) for node, role in candidates}
    # Reuse the fixed membership index across all text and inline-node walks.
    candidate_ids = set(candidate_by_id)
    block_slots: dict[int, list[SourceSlot]] = {id(node): [] for node, _ in candidates}
    direct_runs = _direct_prose_runs(roots, candidate_ids)
    direct_owner_by_text = {
        id(text): (key, parent, run) for key, parent, run in direct_runs for text in run
    }
    direct_slots: dict[str, list[SourceSlot]] = {key: [] for key, _, _ in direct_runs}
    coverage: list[CoverageEntry] = []

    for text in texts:
        base_disposition, _base_policy = _text_policy(text)
        owner = (
            _nearest_role(text.parent, candidate_ids)
            if base_disposition != "excluded-with-reason"
            and id(text) not in direct_owner_by_text
            else None
        )
        locator = _text_locator(text)
        block_id: str | None = None
        if owner is not None:
            block_id = str(
                uuid.uuid5(
                    namespace,
                    f"block:{_node_locator(owner)}:{owner.start}:{candidate_by_id[id(owner)][1]}",
                )
            )
        elif (
            base_disposition != "excluded-with-reason"
            and id(text) in direct_owner_by_text
        ):
            key, _parent, _run = direct_owner_by_text[id(text)]
            first = _run[0]
            block_id = str(uuid.uuid5(namespace, f"block:direct:{key}:{first.start}"))
        for start, end, disposition, policy in _text_pieces(
            text, decode_entities=planner_version == BLOCK_PLANNER_VERSION,
        ):
            if disposition == "translatable" and _foreign_language(
                text.parent, requested_language
            ):
                disposition, policy = "literal-preserved", "foreign-language"
            piece_locator = (
                locator
                if (start, end) == (text.start, text.end)
                else f"{locator}/run[{start - text.start}:{end - text.start}]"
            )
            raw = snapshot.output_html[start:end]
            slot = SourceSlot(
                id=_slot_id(
                    namespace, piece_locator, start, "entity" if text.entity else "text"
                ),
                locator=piece_locator,
                kind="entity" if text.entity else "text",
                disposition=disposition,
                policy_code=policy,
                raw_start=start,
                raw_end=end,
                projection_start=None,
                projection_end=None,
                source_value=raw,
                source_hash=sha256_text(raw),
            )
            if owner is not None and disposition != "excluded-with-reason":
                block_slots[id(owner)].append(slot)
            elif (
                id(text) in direct_owner_by_text
                and disposition != "excluded-with-reason"
            ):
                direct_slots[direct_owner_by_text[id(text)][0]].append(slot)
            coverage.append(
                CoverageEntry(
                    locator=piece_locator,
                    kind="entity" if text.entity else "text",
                    disposition=disposition,
                    policy_code=policy,
                    raw_start=start,
                    raw_end=end,
                    source_hash=sha256_text(raw),
                    block_id=block_id
                    if disposition != "excluded-with-reason"
                    else None,
                )
            )

    # Attribute slots are distinct accessibility blocks; they never become incidental
    # text inside the element's prose block.
    attr_blocks: list[tuple[_Node, _Attr, str, str]] = []
    for node in _iter_nodes(roots):
        for attr in node.attrs:
            classified = _attribute_policy(
                node,
                attr,
                planner_version=planner_version,
            )
            if classified is None:
                continue
            disposition, policy = classified
            locator = f"{_node_locator(node)}/@{attr.name.lower()}"
            if (
                attr.value_start is None
                or attr.value_end is None
                or attr.value_end <= attr.value_start
            ):
                value_start, value_end = attr.full_start, attr.full_end
            else:
                value_start, value_end = attr.value_start, attr.value_end
            attr_block_id: str | None = None
            if (
                disposition == "translatable"
                and attr.value_start is not None
                and attr.value_end is not None
            ):
                attr_blocks.append((node, attr, disposition, policy))
                attr_block_id = str(
                    uuid.uuid5(
                        namespace, f"block:accessibility:{locator}:{attr.value_start}"
                    )
                )
            coverage.append(
                CoverageEntry(
                    locator=locator,
                    kind="attribute",
                    disposition=disposition,
                    policy_code=policy,
                    raw_start=value_start,
                    raw_end=value_end,
                    source_hash=sha256_text(
                        snapshot.output_html[value_start:value_end]
                    ),
                    block_id=attr_block_id,
                )
            )

    blocks: list[TranslationBlock] = []
    for node, role in candidates:
        slots = block_slots[id(node)] + _break_slots(
            namespace, node, snapshot.output_html,
            narrow_span=planner_version == BLOCK_PLANNER_VERSION,
        )
        if not slots or not any(slot.disposition == "translatable" for slot in slots):
            continue
        projection, projected_slots = _build_projection(slots, snapshot.output_html)
        if not projection:
            continue
        locator = _node_locator(node)
        block_id = str(uuid.uuid5(namespace, f"block:{locator}:{node.start}:{role}"))
        tokens = _inline_tokens(
            namespace, node, snapshot.output_html, node, candidate_ids
        )
        blocks.append(
            TranslationBlock(
                id=block_id,
                order=1,
                role=role,
                locator=locator,
                source_start=node.start,
                source_end=node.end or node.start_tag_end,
                projection=projection,
                projection_hash=sha256_text(projection),
                slots=projected_slots,
                protected_inline=tokens,
                wire=_wire(projected_slots, tokens),
                anchors=_anchors(
                    namespace,
                    block_id,
                    projection,
                    requested_language,
                    manifest_ends=manifest_ends_for(projection),
                ),
            )
        )
        for slot in projected_slots:
            if slot.kind == "break":
                coverage.append(
                    CoverageEntry(
                        locator=slot.locator,
                        kind="break",
                        disposition=slot.disposition,
                        policy_code=slot.policy_code,
                        raw_start=slot.raw_start,
                        raw_end=slot.raw_end,
                        source_hash=slot.source_hash,
                        block_id=block_id,
                    )
                )
    for key, parent, run in direct_runs:
        first, last = run[0], run[-1]
        slots = direct_slots[key] + [
            slot
            for slot in _break_slots(
                namespace, parent, snapshot.output_html,
                narrow_span=planner_version == BLOCK_PLANNER_VERSION,
            )
            if first.start <= slot.raw_start and slot.raw_end <= last.end
        ]
        if not slots or not any(slot.disposition == "translatable" for slot in slots):
            continue
        projection, projected_slots = _build_projection(slots, snapshot.output_html)
        if not projection:
            continue
        block_id = str(uuid.uuid5(namespace, f"block:direct:{key}:{first.start}"))
        all_tokens = _inline_tokens(
            namespace, parent, snapshot.output_html, parent, candidate_ids
        )
        run_spans = [(text.start, text.end) for text in run]
        element_bounds: dict[str, tuple[int, int]] = {}
        for token in all_tokens:
            start, end = element_bounds.get(
                token.element_id, (token.raw_start, token.raw_end)
            )
            element_bounds[token.element_id] = (
                min(start, token.raw_start),
                max(end, token.raw_end),
            )
        tokens = tuple(
            token
            for token in all_tokens
            if (
                token.kind == "void"
                and first.start <= token.raw_start
                and token.raw_end <= last.end
            )
            or any(
                token_start <= text_start and text_end <= token_end
                for text_start, text_end in run_spans
                for token_start, token_end in [element_bounds[token.element_id]]
            )
        )
        source_start = min([first.start, *(token.raw_start for token in tokens)])
        source_end = max([last.end, *(token.raw_end for token in tokens)])
        direct_role = (
            "note_body"
            if _note_ancestor(parent)
            else {
                "li": "list_item",
                "blockquote": "blockquote",
                "td": "table_cell",
                "th": "table_header",
            }.get(parent.tag, "direct_prose")
        )
        blocks.append(
            TranslationBlock(
                id=block_id,
                order=1,
                role=direct_role,
                locator=key,
                source_start=source_start,
                source_end=source_end,
                projection=projection,
                projection_hash=sha256_text(projection),
                slots=projected_slots,
                protected_inline=tokens,
                wire=_wire(projected_slots, tokens),
                anchors=_anchors(
                    namespace,
                    block_id,
                    projection,
                    requested_language,
                    manifest_ends=manifest_ends_for(projection),
                ),
            )
        )
        for slot in projected_slots:
            if slot.kind == "break":
                coverage.append(
                    CoverageEntry(
                        locator=slot.locator,
                        kind="break",
                        disposition=slot.disposition,
                        policy_code=slot.policy_code,
                        raw_start=slot.raw_start,
                        raw_end=slot.raw_end,
                        source_hash=slot.source_hash,
                        block_id=block_id,
                    )
                )
    for node, attr, disposition, policy in attr_blocks:
        locator = f"{_node_locator(node)}/@{attr.name.lower()}"
        assert (
            attr.value_start is not None
            and attr.value_end is not None
            and attr.value is not None
        )
        block_id = str(
            uuid.uuid5(namespace, f"block:accessibility:{locator}:{attr.value_start}")
        )
        projection = unescape(attr.value)
        slot = SourceSlot(
            id=_slot_id(namespace, locator, attr.value_start, "attribute"),
            locator=locator,
            kind="attribute",
            disposition=disposition,
            policy_code=policy,
            raw_start=attr.value_start,
            raw_end=attr.value_end,
            projection_start=0,
            projection_end=len(projection),
            source_value=attr.value,
            source_hash=sha256_text(attr.value),
        )
        blocks.append(
            TranslationBlock(
                id=block_id,
                order=1,
                role="accessibility_text",
                locator=locator,
                source_start=attr.value_start,
                source_end=attr.value_end,
                projection=projection,
                projection_hash=sha256_text(projection),
                slots=(slot,),
                protected_inline=(),
                wire=_wire((slot,), ()),
                anchors=_anchors(
                    namespace,
                    block_id,
                    projection,
                    requested_language,
                    manifest_ends=manifest_ends_for(projection),
                ),
            )
        )
    blocks.sort(
        key=lambda block: (
            block.source_start,
            block.source_end,
            block.locator,
            block.id,
        )
    )
    split_limit = (
        frozen_settings.get(TRANSLATION_MAX_BLOCK_CHARS_SETTING)
        if sentence_manifest is not None
        else None
    )
    blocks, coverage = _split_oversized_paragraphs(
        blocks,
        coverage,
        html=snapshot.output_html,
        language=requested_language,
        namespace=namespace,
        max_chars=split_limit,
    )
    blocks.sort(
        key=lambda block: (
            block.source_start,
            block.source_end,
            block.locator,
            block.id,
        )
    )
    blocks = [
        TranslationBlock(
            id=block.id,
            order=index,
            role=block.role,
            locator=block.locator,
            source_start=block.source_start,
            source_end=block.source_end,
            projection=block.projection,
            projection_hash=block.projection_hash,
            slots=block.slots,
            protected_inline=block.protected_inline,
            wire=block.wire,
            anchors=block.anchors,
        )
        for index, block in enumerate(blocks, 1)
    ]
    valid_block_ids = {block.id for block in blocks}
    coverage = [
        replace(
            entry,
            block_id=entry.block_id if entry.block_id in valid_block_ids else None,
        )
        for entry in coverage
    ]
    coverage.sort(key=lambda entry: (entry.raw_start, entry.raw_end, entry.locator))
    _validate_coverage(snapshot.output_html, coverage, blocks)
    if manifest_projections is not None and used_manifest_hashes != set(
        manifest_projections
    ):
        raise BlockContractError("source-sentence-segmentation-manifest")
    return TranslationSourcePlan.create(
        snapshot=snapshot,
        source_attestation=attestation,
        project_id=project_id,
        planner_version=planner_version,
        source_language=requested_language,
        language_profile=profile,
        planner_settings=frozen_settings,
        settings_fingerprint=fingerprint,
        namespace=namespace_text,
        blocks=tuple(blocks),
        coverage=CoverageWitness.create(tuple(coverage)),
    )


build_source_plan = plan_document_blocks


def validate_canonical_plan(plan: TranslationSourcePlan) -> None:
    """Fail closed unless the stored plan is exactly reproducible from inputs."""

    if plan.planner_version == CORRECTED_BLOCK_PLANNER_VERSION:
        from .source_plan_corrections import rebuild_corrected_plan

        rebuilt = rebuild_corrected_plan(plan)
    else:
        rebuilt = plan_document_blocks(
            plan.snapshot,
            project_id=plan.project_id,
            source_attestation=plan.source_attestation,
            source_language=plan.source_language,
            language_profile=plan.language_profile,
            settings=plan.planner_settings,
            planner_version=plan.planner_version,
        )
    if (
        rebuilt._hash_payload() != plan._hash_payload()
        or rebuilt.plan_hash != plan.plan_hash
    ):
        raise BlockContractError("plan-canonical-rebuild")
