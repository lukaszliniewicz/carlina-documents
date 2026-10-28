"""Pure, bounded projection helpers for semantic HTML list numbering.

The helpers in this module identify unchanged list subtrees without depending
on translated wording, and splice only ``ol``/``li`` numbering attributes into
an already rendered HTML string.  They deliberately do not canonicalize or
serialize the surrounding document.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from html import unescape
from html.parser import HTMLParser
from typing import Any

from carlina.core.semantic_html import SAFE_INTEGER_RE

MAX_HTML_BYTES = 20 * 1024 * 1024
MAX_LIST_NODES = 10_000
MAX_HTML_DEPTH = 10_000
MAX_EXCERPT_CHARS = 160

_LIST_TAGS = frozenset({"ol", "ul", "li"})
_OL_ATTRIBUTES = ("start", "type", "reversed")
_LI_ATTRIBUTES = ("value",)
_NUMBERING_ATTRIBUTES = {"ol": _OL_ATTRIBUTES, "li": _LI_ATTRIBUTES, "ul": ()}
_NUMBERING_ATTRIBUTE_SETS = {
    tag: frozenset(attributes) for tag, attributes in _NUMBERING_ATTRIBUTES.items()
}
_LIST_TYPE_VALUES = frozenset({"1", "a", "A", "i", "I"})
_HEX_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_VOID_TAGS = frozenset(
    {
        "area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr",
    }
)
_NOTE_SECTION_ID = "translator-doc-endnotes"
_NOTE_SECTION_ROLE = "doc-endnotes"


class NumberingProjectionError(ValueError):
    """A source-free validation or projection failure."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(slots=True)
class _ListNode:
    tag: str
    attrs: dict[str, str | None]
    numbering_attrs: dict[str, str]
    start: int
    open_end: int
    local_index: int
    parent_list_index: int | None
    root: _ListRoot


@dataclass(slots=True)
class _ListRoot:
    root_index: int
    start: int
    node: _ListNode | None = None
    end: int | None = None
    nodes: list[_ListNode] = field(default_factory=list)
    excerpt: str = ""
    in_note_section: bool = False
    fingerprint: str = ""
    _hasher: Any = field(default=None, repr=False)


@dataclass(slots=True)
class _NoteSection:
    start: int
    end: int | None = None


@dataclass(slots=True)
class _Frame:
    tag: str
    root: _ListRoot | None
    list_node: _ListNode | None
    is_note_section: bool = False
    note_section: _NoteSection | None = None


@dataclass(frozen=True, slots=True)
class _AttributeLexeme:
    name: str
    start: int
    end: int
    value_start: int | None
    value_end: int | None
    has_value: bool


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=False
    ).encode("utf-8")


def _identity_id(root_index: int, fingerprint: str) -> str:
    return hashlib.sha256(_json_bytes([root_index, fingerprint])).hexdigest()


def _canonical_attr_value(name: str, value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if name in {"class", "headers", "aria-describedby", "aria-labelledby"}:
        normalized = " ".join(normalized.split())
    if name == "reversed":
        return ""
    return normalized


def _fingerprint_attrs(
    tag: str, attrs: list[tuple[str, str | None]]
) -> list[list[str | None]]:
    ignored = _NUMBERING_ATTRIBUTE_SETS.get(tag, frozenset())
    normalized = [
        [name, _canonical_attr_value(name, value)]
        for name, value in attrs
        if name not in ignored
    ]
    normalized.sort(key=lambda item: (item[0], item[1] is not None, item[1] or ""))
    return normalized


def _event(root: _ListRoot, value: Any) -> None:
    encoded = _json_bytes(value)
    root._hasher.update(len(encoded).to_bytes(8, "big"))
    root._hasher.update(encoded)


def _source_offset(source: str, line_starts: list[int], position: tuple[int, int]) -> int:
    line, column = position
    if line < 1 or line > len(line_starts):
        raise NumberingProjectionError("invalid_html")
    return line_starts[line - 1] + column


def _list_numbering_attrs(
    tag: str,
    attrs: list[tuple[str, str | None]],
    parent_tag: str | None,
) -> tuple[dict[str, str | None], dict[str, str]]:
    all_attrs: dict[str, str | None] = {}
    for name, value in attrs:
        if name in all_attrs:
            raise NumberingProjectionError("invalid_list_attributes")
        all_attrs[name] = _canonical_attr_value(name, value)

    numbering: dict[str, str] = {}
    if tag == "ol":
        if "start" in all_attrs:
            value = all_attrs["start"]
            if value is None or SAFE_INTEGER_RE.fullmatch(value) is None:
                raise NumberingProjectionError("invalid_list_attribute")
            numbering["start"] = value
        if "type" in all_attrs:
            value = all_attrs["type"]
            if value not in _LIST_TYPE_VALUES:
                raise NumberingProjectionError("invalid_list_attribute")
            numbering["type"] = value
        if "reversed" in all_attrs:
            raw_value = next(value for name, value in attrs if name == "reversed")
            if raw_value is not None and raw_value.strip().casefold() not in {"", "reversed"}:
                raise NumberingProjectionError("invalid_list_attribute")
            numbering["reversed"] = ""
    elif tag == "li" and "value" in all_attrs:
        value = all_attrs["value"]
        if parent_tag != "ol" or value is None or SAFE_INTEGER_RE.fullmatch(value) is None:
            raise NumberingProjectionError("invalid_list_attribute")
        numbering["value"] = value
    return all_attrs, numbering


class _SemanticListParser(HTMLParser):
    def __init__(self, source: str):
        super().__init__(convert_charrefs=False)
        self.source = source
        self.line_starts = [0]
        self.line_starts.extend(index + 1 for index, char in enumerate(source) if char == "\n")
        self.frames: list[_Frame] = []
        self.roots: list[_ListRoot] = []
        self.note_sections: list[_NoteSection] = []
        self.list_node_count = 0
        self._current_root: _ListRoot | None = None

    def _offset(self) -> int:
        return _source_offset(self.source, self.line_starts, self.getpos())

    def _active_root(self) -> _ListRoot | None:
        return self.frames[-1].root if self.frames else None

    def _record_text(self, value: str) -> None:
        root = self._active_root()
        if root is None:
            return
        _event(root, ["text", value])
        if len(root.excerpt) < MAX_EXCERPT_CHARS:
            root.excerpt = (root.excerpt + value)[:MAX_EXCERPT_CHARS]
        if self.frames and self.frames[-1].tag in {"ol", "ul"} and value.strip():
            raise NumberingProjectionError("invalid_list_nesting")

    def _start(
        self, tag: str, attrs: list[tuple[str, str | None]], raw: str, *, self_closing: bool
    ) -> None:
        tag = tag.lower()
        offset = self._offset()
        open_end = offset + len(raw)
        parent = self.frames[-1] if self.frames else None
        parent_tag = parent.tag if parent is not None else None
        inherited_root = parent.root if parent is not None else None
        has_list_ancestor = any(frame.tag in {"ol", "ul"} for frame in self.frames)

        if tag in _LIST_TAGS:
            if self_closing:
                raise NumberingProjectionError("invalid_list_nesting")
            if tag == "li" and parent_tag not in {"ol", "ul"}:
                raise NumberingProjectionError("invalid_list_nesting")
            if tag in {"ol", "ul"} and has_list_ancestor and parent_tag != "li":
                raise NumberingProjectionError("invalid_list_nesting")
            if parent_tag in {"ol", "ul"} and tag != "li":
                raise NumberingProjectionError("invalid_list_nesting")
            self.list_node_count += 1
            if self.list_node_count > MAX_LIST_NODES:
                raise NumberingProjectionError("too_many_list_nodes")

        if parent_tag in {"ol", "ul"} and tag != "li":
            raise NumberingProjectionError("invalid_list_nesting")

        root = inherited_root
        if tag in {"ol", "ul"} and not has_list_ancestor:
            root = _ListRoot(root_index=len(self.roots), start=offset)
            root._hasher = hashlib.sha256()
            root._hasher.update(b"semantic-numbering-subtree-v1\0")
            root.in_note_section = any(frame.is_note_section for frame in self.frames)
            self.roots.append(root)
            self._current_root = root

        list_node: _ListNode | None = None
        all_attrs: dict[str, str | None] = {}
        numbering_attrs: dict[str, str] = {}
        if tag in _LIST_TAGS:
            if root is None:
                raise NumberingProjectionError("invalid_list_nesting")
            all_attrs, numbering_attrs = _list_numbering_attrs(tag, attrs, parent_tag)
            local_index = len(root.nodes)
            parent_list_index = parent.list_node.local_index if parent and parent.list_node else None
            list_node = _ListNode(
                tag=tag,
                attrs=all_attrs,
                numbering_attrs=numbering_attrs,
                start=offset,
                open_end=open_end,
                local_index=local_index,
                parent_list_index=parent_list_index,
                root=root,  # type: ignore[arg-type]
            )
            root.nodes.append(list_node)
            if tag in {"ol", "ul"} and root.node is None and root.start == offset:
                root.node = list_node

        marker_attrs: dict[str, str | None] = {}
        for name, value in attrs:
            if name in marker_attrs and name in {"id", "role"}:
                raise NumberingProjectionError("invalid_html")
            marker_attrs[name] = _canonical_attr_value(name, value)
        is_note_section = (
            tag == "section"
            and marker_attrs.get("id") == _NOTE_SECTION_ID
            and marker_attrs.get("role") == _NOTE_SECTION_ROLE
        )
        note_section = None
        if is_note_section:
            note_section = _NoteSection(start=offset)
            self.note_sections.append(note_section)

        if root is not None:
            _event(root, ["start", tag, _fingerprint_attrs(tag, attrs)])

        if self_closing or tag in _VOID_TAGS:
            if root is not None and self_closing and tag not in _VOID_TAGS:
                _event(root, ["end", tag])
            return
        if len(self.frames) >= MAX_HTML_DEPTH:
            raise NumberingProjectionError("html_too_deep")
        self.frames.append(
            _Frame(
                tag=tag,
                root=root,
                list_node=list_node,
                is_note_section=is_note_section,
                note_section=note_section,
            )
        )

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        raw = self.get_starttag_text() or ""
        self._start(tag, attrs, raw, self_closing=False)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        raw = self.get_starttag_text() or ""
        self._start(tag, attrs, raw, self_closing=True)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _VOID_TAGS or not self.frames or self.frames[-1].tag != tag:
            raise NumberingProjectionError("invalid_html")
        offset = self._offset()
        close_end = self.source.find(">", offset)
        if close_end < 0:
            raise NumberingProjectionError("invalid_html")
        close_end += 1
        frame = self.frames.pop()
        root = frame.root
        if root is not None:
            _event(root, ["end", tag])
        if (
            frame.list_node is not None
            and tag in {"ol", "ul"}
            and frame.list_node.root.node is frame.list_node
        ):
            # The outer root's end is the close of its root list node.
            frame.list_node.root.end = close_end
            frame.list_node.root.fingerprint = frame.list_node.root._hasher.hexdigest()
            self._current_root = None
        if frame.note_section is not None:
            frame.note_section.end = close_end

    def handle_data(self, data: str) -> None:
        self._record_text(data)

    def handle_entityref(self, name: str) -> None:
        self._record_text(unescape(f"&{name};"))

    def handle_charref(self, name: str) -> None:
        self._record_text(unescape(f"&#{name};"))

    def handle_comment(self, data: str) -> None:
        root = self._active_root()
        if root is not None:
            if self.frames and self.frames[-1].tag in {"ol", "ul"} and data.strip():
                raise NumberingProjectionError("invalid_list_nesting")
            _event(root, ["comment", data])

    def handle_decl(self, decl: str) -> None:
        root = self._active_root()
        if root is not None:
            if self.frames and self.frames[-1].tag in {"ol", "ul"} and decl.strip():
                raise NumberingProjectionError("invalid_list_nesting")
            _event(root, ["decl", decl])

    def handle_pi(self, data: str) -> None:
        root = self._active_root()
        if root is not None:
            if self.frames and self.frames[-1].tag in {"ol", "ul"} and data.strip():
                raise NumberingProjectionError("invalid_list_nesting")
            _event(root, ["pi", data])

    def close(self) -> None:
        super().close()
        if self.frames or self._current_root is not None:
            raise NumberingProjectionError("invalid_html")


def _parse_html(source: str) -> _SemanticListParser:
    if not isinstance(source, str):
        raise NumberingProjectionError("invalid_html")
    try:
        encoded_size = len(source.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise NumberingProjectionError("invalid_html") from exc
    if encoded_size > MAX_HTML_BYTES:
        raise NumberingProjectionError("html_too_large")
    parser = _SemanticListParser(source)
    try:
        parser.feed(source)
        parser.close()
    except NumberingProjectionError:
        raise
    except Exception as exc:
        raise NumberingProjectionError("invalid_html") from exc
    if any(root.end is None or not root.fingerprint for root in parser.roots):
        raise NumberingProjectionError("invalid_html")
    return parser


def _root_metadata(root: _ListRoot) -> dict[str, Any]:
    assert root.end is not None
    return {
        "id": _identity_id(root.root_index, root.fingerprint),
        "root_index": root.root_index,
        "fingerprint": root.fingerprint,
        "excerpt": root.excerpt[:MAX_EXCERPT_CHARS],
        "root_start": root.start,
        "root_end": root.end,
    }


def _conflict(root: _ListRoot, reason: str) -> dict[str, Any]:
    return {
        "root_index": root.root_index,
        "fingerprint": root.fingerprint,
        "reason": reason,
        "excerpt": root.excerpt[:MAX_EXCERPT_CHARS],
        "root_start": root.start,
        "root_end": root.end,
    }


def _validate_attr_map(tag: str, attrs: object) -> dict[str, str | None]:
    if not isinstance(attrs, dict) or not attrs:
        raise NumberingProjectionError("invalid_entries")
    permitted = _NUMBERING_ATTRIBUTE_SETS[tag]
    normalized: dict[str, str | None] = {}
    for name, value in attrs.items():
        if not isinstance(name, str) or name not in permitted:
            raise NumberingProjectionError("invalid_entries")
        if value is not None and not isinstance(value, str):
            raise NumberingProjectionError("invalid_entries")
        if tag == "ol" and name == "start":
            if value is not None and SAFE_INTEGER_RE.fullmatch(value) is None:
                raise NumberingProjectionError("invalid_entries")
        elif tag == "ol" and name == "type":
            if value is not None and value not in _LIST_TYPE_VALUES:
                raise NumberingProjectionError("invalid_entries")
        elif tag == "ol" and name == "reversed":
            if value not in {None, ""}:
                raise NumberingProjectionError("invalid_entries")
        elif (
            tag == "li"
            and name == "value"
            and value is not None
            and SAFE_INTEGER_RE.fullmatch(value) is None
        ):
            raise NumberingProjectionError("invalid_entries")
        normalized[name] = value
    return normalized


def _validate_entry_list(entries: object) -> list[dict[str, Any]]:
    if not isinstance(entries, list) or len(entries) > MAX_LIST_NODES:
        raise NumberingProjectionError("invalid_entries")
    result: list[dict[str, Any]] = []
    seen_roots: set[int] = set()
    total_nodes = 0
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {
            "id", "root_index", "fingerprint", "excerpt", "root_start", "root_end", "nodes"
        }:
            raise NumberingProjectionError("invalid_entries")
        root_index = entry["root_index"]
        fingerprint = entry["fingerprint"]
        if (
            type(root_index) is not int
            or root_index < 0
            or root_index in seen_roots
            or not isinstance(fingerprint, str)
            or _HEX_DIGEST_RE.fullmatch(fingerprint) is None
            or entry["id"] != _identity_id(root_index, fingerprint)
            or not isinstance(entry["excerpt"], str)
            or len(entry["excerpt"]) > MAX_EXCERPT_CHARS
            or type(entry["root_start"]) is not int
            or type(entry["root_end"]) is not int
            or entry["root_start"] < 0
            or entry["root_start"] >= entry["root_end"]
            or not isinstance(entry["nodes"], list)
            or len(entry["nodes"]) > MAX_LIST_NODES
        ):
            raise NumberingProjectionError("invalid_entries")
        total_nodes += len(entry["nodes"])
        if total_nodes > MAX_LIST_NODES:
            raise NumberingProjectionError("invalid_entries")
        seen_roots.add(root_index)
        nodes: list[dict[str, Any]] = []
        seen_nodes: set[int] = set()
        for node in entry["nodes"]:
            if not isinstance(node, dict) or set(node) != {
                "node_index", "tag", "original_attrs", "desired_attrs"
            }:
                raise NumberingProjectionError("invalid_entries")
            node_index, tag = node["node_index"], node["tag"]
            if (
                type(node_index) is not int
                or node_index < 0
                or node_index in seen_nodes
                or not isinstance(tag, str)
                or tag not in _LIST_TAGS
            ):
                raise NumberingProjectionError("invalid_entries")
            seen_nodes.add(node_index)
            original = _validate_attr_map(tag, node["original_attrs"])
            desired = _validate_attr_map(tag, node["desired_attrs"])
            if set(original) != set(desired) or all(original[key] == desired[key] for key in original):
                raise NumberingProjectionError("invalid_entries")
            nodes.append(
                {
                    "node_index": node_index,
                    "tag": tag,
                    "original_attrs": dict(original),
                    "desired_attrs": dict(desired),
                }
            )
        if not nodes:
            raise NumberingProjectionError("invalid_entries")
        nodes.sort(key=lambda item: item["node_index"])
        result.append({**_root_metadata_from_entry(entry), "nodes": nodes})
    result.sort(key=lambda item: item["root_index"])
    return result


def _root_metadata_from_entry(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": entry["id"],
        "root_index": entry["root_index"],
        "fingerprint": entry["fingerprint"],
        "excerpt": entry["excerpt"],
        "root_start": entry["root_start"],
        "root_end": entry["root_end"],
    }


def _entry_matches_root(entry: dict[str, Any], root: _ListRoot) -> bool:
    return (
        entry["root_index"] == root.root_index
        and entry["fingerprint"] == root.fingerprint
        and entry["id"] == _identity_id(root.root_index, root.fingerprint)
        and entry["root_start"] == root.start
        and entry["root_end"] == root.end
    )


def _entry_nodes_for_root(
    entries: list[dict[str, Any]], parser: _SemanticListParser
) -> tuple[dict[int, dict[int, dict[str, str | None]]], set[int]]:
    overlays: dict[int, dict[int, dict[str, str | None]]] = {}
    blocked: set[int] = set()
    for entry in entries:
        index = entry["root_index"]
        if index >= len(parser.roots) or not _entry_matches_root(entry, parser.roots[index]):
            blocked.add(index)
            continue
        root = parser.roots[index]
        by_node: dict[int, dict[str, str | None]] = {}
        stale = False
        for node in entry["nodes"]:
            node_index = node["node_index"]
            if node_index >= len(root.nodes) or root.nodes[node_index].tag != node["tag"]:
                stale = True
                break
            source_node = root.nodes[node_index]
            for name, original_value in node["original_attrs"].items():
                if source_node.numbering_attrs.get(name) != original_value:
                    stale = True
                    break
            if stale:
                break
            by_node[node_index] = dict(node["desired_attrs"])
        if stale:
            blocked.add(index)
        else:
            overlays[index] = by_node
    return overlays, blocked


def numbering_candidates(
    pinned_html: str,
    corrected_html: str,
    entries: list[dict] | None = None,
) -> dict[str, list[dict]]:
    """Compare corrected source lists with the immutable pinned source."""

    pinned = _parse_html(pinned_html)
    corrected = _parse_html(corrected_html)
    normalized_entries = _validate_entry_list([] if entries is None else entries)
    overlays, blocked = _entry_nodes_for_root(normalized_entries, pinned)

    pinned_groups: dict[str, list[_ListRoot]] = {}
    corrected_groups: dict[str, list[_ListRoot]] = {}
    for root in pinned.roots:
        pinned_groups.setdefault(root.fingerprint, []).append(root)
    for root in corrected.roots:
        corrected_groups.setdefault(root.fingerprint, []).append(root)

    changes: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    blocked_reported: set[int] = set()
    for root in pinned.roots:
        if root.root_index in blocked:
            conflicts.append(_conflict(root, "stale_overlay"))
            blocked_reported.add(root.root_index)
            continue
        pinned_matches = pinned_groups[root.fingerprint]
        corrected_matches = corrected_groups.get(root.fingerprint, [])
        if len(pinned_matches) > 1 or len(corrected_matches) > 1:
            conflicts.append(_conflict(root, "ambiguous_root"))
            continue
        if not corrected_matches:
            conflicts.append(_conflict(root, "unmatched_root"))
            continue
        corrected_root = corrected_matches[0]
        effective_overlay = overlays.get(root.root_index, {})
        node_deltas: list[dict[str, Any]] = []
        for source_node, corrected_node in zip(root.nodes, corrected_root.nodes, strict=True):
            before: dict[str, str | None] = {
                name: source_node.numbering_attrs.get(name)
                for name in _NUMBERING_ATTRIBUTES[source_node.tag]
            }
            before.update(effective_overlay.get(source_node.local_index, {}))
            after = corrected_node.numbering_attrs
            attrs = _NUMBERING_ATTRIBUTES[source_node.tag]
            changed_names = [name for name in attrs if before.get(name) != after.get(name)]
            if not changed_names:
                continue
            node_deltas.append(
                {
                    "node_index": source_node.local_index,
                    "tag": source_node.tag,
                    "before": {name: before.get(name) for name in changed_names},
                    "after": {name: after.get(name) for name in changed_names},
                    "original_attrs": {
                        name: source_node.numbering_attrs.get(name) for name in changed_names
                    },
                }
            )
        if node_deltas:
            changes.append({**_root_metadata(root), "changes": node_deltas})

    for entry in normalized_entries:
        index = entry["root_index"]
        if index >= len(pinned.roots) and index not in blocked_reported:
            conflicts.append(
                {
                    "root_index": index,
                    "fingerprint": entry["fingerprint"],
                    "reason": "stale_overlay",
                    "excerpt": entry["excerpt"][:MAX_EXCERPT_CHARS],
                    "root_start": None,
                    "root_end": None,
                }
            )
    return {"changes": changes, "conflicts": conflicts}


def _validate_delta(delta: object) -> dict[str, Any]:
    if not isinstance(delta, dict) or set(delta) != {
        "node_index", "tag", "before", "after", "original_attrs"
    }:
        raise NumberingProjectionError("invalid_selection")
    node_index, tag = delta["node_index"], delta["tag"]
    if (
        type(node_index) is not int
        or node_index < 0
        or not isinstance(tag, str)
        or tag not in _LIST_TAGS
    ):
        raise NumberingProjectionError("invalid_selection")
    before = _validate_attr_map(tag, delta["before"])
    after = _validate_attr_map(tag, delta["after"])
    original = _validate_attr_map(tag, delta["original_attrs"])
    if set(before) != set(after) or set(before) != set(original):
        raise NumberingProjectionError("invalid_selection")
    if all(before[name] == after[name] for name in before):
        raise NumberingProjectionError("invalid_selection")
    return {
        "node_index": node_index,
        "tag": tag,
        "before": before,
        "after": after,
        "original_attrs": original,
    }


def _validate_change(change: object) -> dict[str, Any]:
    required = {
        "id", "root_index", "fingerprint", "excerpt", "root_start", "root_end", "changes"
    }
    if not isinstance(change, dict) or not required.issubset(change):
        raise NumberingProjectionError("invalid_selection")
    metadata = {
        "id": change["id"],
        "root_index": change["root_index"],
        "fingerprint": change["fingerprint"],
        "excerpt": change["excerpt"],
        "root_start": change["root_start"],
        "root_end": change["root_end"],
    }
    if (
        type(metadata["root_index"]) is not int
        or metadata["root_index"] < 0
        or not isinstance(metadata["fingerprint"], str)
        or _HEX_DIGEST_RE.fullmatch(metadata["fingerprint"]) is None
        or metadata["id"] != _identity_id(metadata["root_index"], metadata["fingerprint"])
        or not isinstance(metadata["excerpt"], str)
        or len(metadata["excerpt"]) > MAX_EXCERPT_CHARS
        or type(metadata["root_start"]) is not int
        or type(metadata["root_end"]) is not int
        or metadata["root_start"] < 0
        or metadata["root_start"] >= metadata["root_end"]
        or not isinstance(change["changes"], list)
        or not change["changes"]
        or len(change["changes"]) > MAX_LIST_NODES
    ):
        raise NumberingProjectionError("invalid_selection")
    deltas = [_validate_delta(delta) for delta in change["changes"]]
    indexes = [delta["node_index"] for delta in deltas]
    if len(indexes) != len(set(indexes)):
        raise NumberingProjectionError("invalid_selection")
    return {**metadata, "changes": deltas}


def merge_numbering_entries(existing: list, selected_changes: list) -> list:
    """Merge selected root deltas into immutable, original-relative entries."""

    normalized_existing = _validate_entry_list(existing)
    if not isinstance(selected_changes, list) or len(selected_changes) > MAX_LIST_NODES:
        raise NumberingProjectionError("invalid_selection")
    selected = [_validate_change(change) for change in selected_changes]
    if sum(len(change["changes"]) for change in selected) > MAX_LIST_NODES:
        raise NumberingProjectionError("invalid_selection")
    selected_ids = [item["id"] for item in selected]
    selected_roots = [item["root_index"] for item in selected]
    if len(selected_ids) != len(set(selected_ids)) or len(selected_roots) != len(set(selected_roots)):
        raise NumberingProjectionError("invalid_selection")

    entries_by_root = {entry["root_index"]: entry for entry in normalized_existing}
    for change in selected:
        root_index = change["root_index"]
        prior = entries_by_root.get(root_index)
        if prior is not None and prior["id"] != change["id"]:
            raise NumberingProjectionError("stale_selection")
        nodes_by_index: dict[int, dict[str, Any]] = {}
        if prior is not None:
            nodes_by_index = {
                node["node_index"]: {
                    "node_index": node["node_index"],
                    "tag": node["tag"],
                    "original_attrs": dict(node["original_attrs"]),
                    "desired_attrs": dict(node["desired_attrs"]),
                }
                for node in prior["nodes"]
            }

        for delta in change["changes"]:
            node_index = delta["node_index"]
            node = nodes_by_index.get(node_index)
            if node is not None and node["tag"] != delta["tag"]:
                raise NumberingProjectionError("stale_selection")
            if node is None:
                node = {
                    "node_index": node_index,
                    "tag": delta["tag"],
                    "original_attrs": {},
                    "desired_attrs": {},
                }
                nodes_by_index[node_index] = node
            for name in delta["after"]:
                original_value = delta["original_attrs"][name]
                prior_original = node["original_attrs"].get(name, original_value)
                effective_before = node["desired_attrs"].get(name, prior_original)
                if prior_original != original_value or effective_before != delta["before"][name]:
                    raise NumberingProjectionError("stale_selection")
                if delta["after"][name] == original_value:
                    node["original_attrs"].pop(name, None)
                    node["desired_attrs"].pop(name, None)
                else:
                    node["original_attrs"][name] = original_value
                    node["desired_attrs"][name] = delta["after"][name]
            if not node["desired_attrs"]:
                nodes_by_index.pop(node_index, None)

        if nodes_by_index:
            entries_by_root[root_index] = {
                "id": change["id"],
                "root_index": root_index,
                "fingerprint": change["fingerprint"],
                "excerpt": change["excerpt"][:MAX_EXCERPT_CHARS],
                "root_start": change["root_start"],
                "root_end": change["root_end"],
                "nodes": sorted(nodes_by_index.values(), key=lambda item: item["node_index"]),
            }
        else:
            entries_by_root.pop(root_index, None)
    return [entries_by_root[index] for index in sorted(entries_by_root)]


def _topology(root: _ListRoot) -> list[tuple[str, str | None, int | None]]:
    return [
        (node.tag, node.attrs.get("id"), node.parent_list_index)
        for node in root.nodes
    ]


def _all_numbering_attrs(root: _ListRoot) -> list[dict[str, str]]:
    return [dict(node.numbering_attrs) for node in root.nodes]


def _attribute_lexemes(raw_tag: str) -> list[_AttributeLexeme]:
    if not raw_tag.startswith("<") or not raw_tag.endswith(">"):
        raise NumberingProjectionError("unsafe_projection")
    end = len(raw_tag) - 1
    position = 1
    while position < end and not raw_tag[position].isspace() and raw_tag[position] not in "/>":
        position += 1
    attributes: list[_AttributeLexeme] = []
    while position < end:
        while position < end and raw_tag[position].isspace():
            position += 1
        if position >= end or raw_tag[position] == "/":
            break
        start = position
        while (
            position < end
            and not raw_tag[position].isspace()
            and raw_tag[position] not in "=/>"
        ):
            position += 1
        if position == start:
            raise NumberingProjectionError("unsafe_projection")
        name = raw_tag[start:position].lower()
        name_end = position
        while position < end and raw_tag[position].isspace():
            position += 1
        value_start = value_end = None
        has_value = False
        if position < end and raw_tag[position] == "=":
            has_value = True
            position += 1
            while position < end and raw_tag[position].isspace():
                position += 1
            if position < end and raw_tag[position] in {"'", '"'}:
                quote = raw_tag[position]
                value_start = position + 1
                closing = raw_tag.find(quote, value_start, end)
                if closing < 0:
                    raise NumberingProjectionError("unsafe_projection")
                value_end = closing
                position = closing + 1
            else:
                value_start = position
                while position < end and not raw_tag[position].isspace() and raw_tag[position] != ">":
                    if raw_tag[position] == "/" and position + 1 == end:
                        break
                    position += 1
                value_end = position
                if value_start == value_end:
                    raise NumberingProjectionError("unsafe_projection")
        else:
            position = name_end
        attributes.append(
            _AttributeLexeme(
                name=name,
                start=start,
                end=position,
                value_start=value_start,
                value_end=value_end,
                has_value=has_value,
            )
        )
    return attributes


def _patch_open_tag(
    raw_tag: str,
    tag: str,
    current_attrs: dict[str, str],
    desired_attrs: dict[str, str | None],
) -> str:
    lexemes = _attribute_lexemes(raw_tag)
    by_name: dict[str, _AttributeLexeme] = {}
    for lexeme in lexemes:
        if lexeme.name in by_name and lexeme.name in _NUMBERING_ATTRIBUTE_SETS[tag]:
            raise NumberingProjectionError("unsafe_projection")
        by_name.setdefault(lexeme.name, lexeme)

    edits: list[tuple[int, int, str]] = []
    additions: list[tuple[str, str | None]] = []
    for name in _NUMBERING_ATTRIBUTES[tag]:
        if name not in desired_attrs:
            continue
        desired = desired_attrs[name]
        lexeme = by_name.get(name)
        if desired is None:
            if lexeme is not None:
                edits.append((lexeme.start, lexeme.end, ""))
            continue
        if lexeme is None:
            additions.append((name, desired))
            continue
        if name == "reversed":
            if not lexeme.has_value:
                continue
            raw_value = ""
            if lexeme.value_start is not None and lexeme.value_end is not None:
                raw_value = unescape(raw_tag[lexeme.value_start:lexeme.value_end]).strip().casefold()
            if raw_value in {"", "reversed"}:
                # Presence is the entire semantic value. Preserve its original spelling.
                continue
            raise NumberingProjectionError("unsafe_projection")
        actual = current_attrs.get(name)
        if actual == desired:
            continue
        if not lexeme.has_value or lexeme.value_start is None or lexeme.value_end is None:
            raise NumberingProjectionError("unsafe_projection")
        edits.append((lexeme.value_start, lexeme.value_end, desired))

    if additions:
        insertion = raw_tag.rfind(">")
        if insertion <= 0:
            raise NumberingProjectionError("unsafe_projection")
        if raw_tag[insertion - 1] == "/":
            insertion -= 1
        addition_text = "".join(
            f" {name}" if name == "reversed" else f' {name}="{value}"'
            for name, value in sorted(
                additions,
                key=lambda item: _NUMBERING_ATTRIBUTES[tag].index(item[0]),
            )
        )
        edits.append((insertion, insertion, addition_text))

    result = raw_tag
    for start, end, replacement in sorted(edits, key=lambda edit: (edit[0], edit[1]), reverse=True):
        result = result[:start] + replacement + result[end:]
    return result


def _validated_projection_entries(
    entries: object, pinned: _SemanticListParser
) -> list[dict[str, Any]]:
    normalized = _validate_entry_list(entries)
    for entry in normalized:
        index = entry["root_index"]
        if (
            index >= len(pinned.roots)
            or not _entry_matches_root(entry, pinned.roots[index])
            or sum(root.fingerprint == entry["fingerprint"] for root in pinned.roots) != 1
        ):
            raise NumberingProjectionError("stale_overlay")
        root = pinned.roots[index]
        for node in entry["nodes"]:
            node_index = node["node_index"]
            if node_index >= len(root.nodes) or root.nodes[node_index].tag != node["tag"]:
                raise NumberingProjectionError("stale_overlay")
            source_node = root.nodes[node_index]
            for name, value in node["original_attrs"].items():
                if source_node.numbering_attrs.get(name) != value:
                    raise NumberingProjectionError("stale_overlay")
    return normalized


def apply_numbering_overlay(
    pinned_html: str,
    rendered_html: str,
    entries: list[dict],
) -> str:
    """Apply original-relative list attributes to a structurally matching render."""

    pinned = _parse_html(pinned_html)
    rendered = _parse_html(rendered_html)
    normalized_entries = _validated_projection_entries(entries, pinned)

    if len(rendered.roots) < len(pinned.roots):
        raise NumberingProjectionError("projection_conflict")
    for index, source_root in enumerate(pinned.roots):
        target_root = rendered.roots[index]
        if _topology(source_root) != _topology(target_root):
            raise NumberingProjectionError("projection_conflict")
        if _all_numbering_attrs(source_root) != _all_numbering_attrs(target_root):
            raise NumberingProjectionError("stale_projection")

    additional_roots = rendered.roots[len(pinned.roots):]
    if additional_roots:
        markers = rendered.note_sections
        if pinned.note_sections or len(markers) != 1:
            raise NumberingProjectionError("projection_conflict")
        marker = markers[0]
        marker_end = marker.end
        if marker_end is None:
            raise NumberingProjectionError("projection_conflict")
        last_source_end = -1
        if pinned.roots:
            mapped_end = rendered.roots[len(pinned.roots) - 1].end
            if mapped_end is None:
                raise NumberingProjectionError("projection_conflict")
            last_source_end = mapped_end
        if marker.start < last_source_end:
            raise NumberingProjectionError("projection_conflict")
        for root in additional_roots:
            if (
                not root.in_note_section
                or root.start < marker.start
                or root.end is None
                or root.end > marker_end
            ):
                raise NumberingProjectionError("projection_conflict")

    if not normalized_entries:
        return rendered_html

    patches: list[tuple[int, int, str]] = []
    for entry in normalized_entries:
        source_root = pinned.roots[entry["root_index"]]
        target_root = rendered.roots[entry["root_index"]]
        for entry_node in entry["nodes"]:
            node_index = entry_node["node_index"]
            source_node = source_root.nodes[node_index]
            target_node = target_root.nodes[node_index]
            for name, original_value in entry_node["original_attrs"].items():
                if source_node.numbering_attrs.get(name) != original_value:
                    raise NumberingProjectionError("stale_overlay")
            raw_tag = rendered_html[target_node.start:target_node.open_end]
            patched = _patch_open_tag(
                raw_tag,
                target_node.tag,
                target_node.numbering_attrs,
                entry_node["desired_attrs"],
            )
            if patched != raw_tag:
                patches.append((target_node.start, target_node.open_end, patched))

    if not patches:
        return rendered_html
    output: list[str] = []
    cursor = 0
    for start, end, replacement in sorted(patches):
        if start < cursor or end < start:
            raise NumberingProjectionError("unsafe_projection")
        output.append(rendered_html[cursor:start])
        output.append(replacement)
        cursor = end
    output.append(rendered_html[cursor:])
    return "".join(output)


__all__ = [
    "NumberingProjectionError",
    "apply_numbering_overlay",
    "merge_numbering_entries",
    "numbering_candidates",
]
