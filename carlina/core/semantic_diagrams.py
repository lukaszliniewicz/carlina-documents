"""Validate and materialize a deliberately small inert Mermaid vocabulary.

The source accepted here is data, not executable Mermaid: only flowchart
declarations, node declarations, and simple edges are admitted. Label text is
kept in semantic HTML and encoded before it is inserted into Mermaid source.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, cast

from bs4 import BeautifulSoup, Comment, Tag
from bs4.element import NavigableString

from .semantic_html import SAFE_IDENTIFIER_RE, SAFE_LANGUAGE_RE
from .semantic_images import (
    IMAGE_ALT_CLASS,
    IMAGE_DESCRIPTION_CLASS,
    IMAGE_DESCRIPTION_LINK_CLASS,
    diagnose_image_descriptions,
)

DIAGRAM_CLASS = "mermaid-diagram-v1"
TEMPLATE_CLASS = "mermaid-template"
LABELS_CLASS = "mermaid-labels"

MAX_DIAGRAMS = 128
MAX_TEMPLATE_BYTES = 64 * 1024
MAX_NODES = 256
MAX_LABELS = 256
MAX_EDGES = 512
MAX_LABEL_CHARS = 4096
MAX_DIAGNOSTIC_CODES = 16

_LOCAL_KEY_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}")
_NODE_ID_PATTERN = r"n[0-9]{1,6}"
_NODE_RE = re.compile(
    rf"(?P<node>{_NODE_ID_PATTERN})"
    r'(?:\["\{\{(?P<square>[A-Za-z][A-Za-z0-9_]{0,63})\}\}\"]'
    r'|\("\{\{(?P<round>[A-Za-z][A-Za-z0-9_]{0,63})\}\}\"\)'
    r'|\{"\{\{(?P<diamond>[A-Za-z][A-Za-z0-9_]{0,63})\}\}\"\})'
)
_PLAIN_EDGE_RE = re.compile(
    rf"(?P<left>{_NODE_ID_PATTERN})[ ]+(?:-->|---)[ ]+"
    rf"(?P<right>{_NODE_ID_PATTERN})"
)
_LABELED_EDGE_RE = re.compile(
    rf"(?P<left>{_NODE_ID_PATTERN})[ ]+-->[ ]*\|"
    r'"\{\{(?P<label>[A-Za-z][A-Za-z0-9_]{0,63})\}\}\"\|[ ]+'
    rf"(?P<right>{_NODE_ID_PATTERN})"
)
_FLOWCHART_HEADERS = frozenset(
    {"flowchart TD", "flowchart TB", "flowchart LR", "flowchart BT", "flowchart RL"}
)
_MERMAID_ESCAPES = {
    '"': "#34;",
    "&": "#38;",
    "<": "#60;",
    ">": "#62;",
    "#": "#35;",
    "{": "#123;",
    "}": "#125;",
    "|": "#124;",
    "[": "#91;",
    "]": "#93;",
    "(": "#40;",
    ")": "#41;",
    "`": "#96;",
    "/": "#47;",
    "\\": "#92;",
}


@dataclass(frozen=True, slots=True)
class _Diagram:
    template: str
    labels: dict[str, str]
    placeholders: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FlowchartNode:
    """One validated Mermaid flowchart node with its original plain-text label."""

    identifier: str
    shape: Literal["square", "round", "diamond"]
    label: str
    label_language: str | None = None
    label_id: str | None = None


@dataclass(frozen=True, slots=True)
class FlowchartEdge:
    """One validated flowchart edge; undirected edges still retain endpoints."""

    source: str
    target: str
    directed: bool
    label: str | None = None
    label_language: str | None = None
    label_id: str | None = None


@dataclass(frozen=True, slots=True)
class ParsedFlowchart:
    """Validated topology and labels for a restricted Mermaid flowchart."""

    direction: Literal["TD", "TB", "LR", "BT", "RL"]
    nodes: tuple[FlowchartNode, ...]
    edges: tuple[FlowchartEdge, ...]
    labels: tuple[str, ...]


def _name(tag: Tag) -> str:
    return str(tag.name or "").casefold()


def _classes(tag: Tag) -> tuple[str, ...]:
    raw = tag.get("class")
    if raw is None:
        return ()
    if isinstance(raw, str):
        return tuple(raw.split())
    if isinstance(raw, list):
        return tuple(str(value) for value in raw)
    return ()


def _has_class(tag: Tag, class_name: str) -> bool:
    return class_name in _classes(tag)


def _has_exact_class(tag: Tag, class_name: str) -> bool:
    return _classes(tag) == (class_name,)


def _walk_tags(root: BeautifulSoup | Tag) -> list[Tag]:
    result = [root]
    result.extend(root.find_all(True))
    return result


def _is_whitespace_text(node: object) -> bool:
    return (
        isinstance(node, NavigableString)
        and not isinstance(node, Comment)
        and not str(node).strip()
    )


def _text_only(tag: Tag) -> str | None:
    chunks: list[str] = []
    for child in tag.contents:
        if isinstance(child, (Comment, Tag)):
            return None
        if not isinstance(child, NavigableString):
            return None
        chunks.append(str(child))
    return "".join(chunks)


def _document_root(tag: Tag) -> Tag:
    root = tag
    while isinstance(root.parent, Tag):
        root = root.parent
    return root


def _normalize_template_newlines(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n")


def _template_controls_valid(value: str) -> bool:
    return all(
        character == "\n" or unicodedata.category(character) != "Cc"
        for character in value
    )


def _parse_template(
    template: str, add: Callable[[str], None]
) -> tuple[str, ...] | None:
    try:
        byte_length = len(template.encode("utf-8"))
    except UnicodeEncodeError:
        add("diagram-template-invalid")
        return None
    if byte_length > MAX_TEMPLATE_BYTES:
        add("diagram-limit")
        return None

    normalized = _normalize_template_newlines(template)
    if not _template_controls_valid(normalized):
        add("diagram-template-invalid")
        return None
    source = normalized.removesuffix("\n")
    lines = source.split("\n")
    if len(lines) < 2 or lines[0] not in _FLOWCHART_HEADERS:
        add("diagram-template-invalid")
        return None

    nodes: set[str] = set()
    edges: list[tuple[str, str]] = []
    placeholders: list[str] = []
    invalid = False
    for raw_line in lines[1:]:
        line = raw_line.strip(" ")
        node_match = _NODE_RE.fullmatch(line)
        if node_match is not None:
            node_id = node_match.group("node")
            label_key = next(
                node_match.group(key)
                for key in ("square", "round", "diamond")
                if node_match.group(key) is not None
            )
            if node_id in nodes:
                invalid = True
            nodes.add(node_id)
            placeholders.append(label_key)
            if len(nodes) > MAX_NODES:
                add("diagram-limit")
                return None
            continue

        edge_match = _LABELED_EDGE_RE.fullmatch(line)
        if edge_match is not None:
            edges.append((edge_match.group("left"), edge_match.group("right")))
            placeholders.append(edge_match.group("label"))
        else:
            edge_match = _PLAIN_EDGE_RE.fullmatch(line)
            if edge_match is None:
                invalid = True
                continue
            edges.append((edge_match.group("left"), edge_match.group("right")))

        if len(edges) > MAX_EDGES:
            add("diagram-limit")
            return None

    if not nodes:
        invalid = True
    if any(left not in nodes or right not in nodes for left, right in edges):
        invalid = True
    if invalid:
        add("diagram-template-invalid")
        return None
    return tuple(placeholders)


def _label_value(value: str) -> str | None:
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    if len(normalized) > MAX_LABEL_CHARS:
        return None
    output: list[str] = []
    for character in normalized:
        if character == "\n":
            output.append(" ")
            continue
        category = unicodedata.category(character)
        if category == "Cc" or category == "Cs":
            return None
        output.append(_MERMAID_ESCAPES.get(character, character))
    return "".join(output)


def _validate_figure(figure: Tag, add: Callable[[str], None]) -> _Diagram | None:
    failed = False

    def fail(code: str) -> None:
        nonlocal failed
        failed = True
        add(code)

    if _name(figure) != "figure":
        fail("diagram-marker-element")
    if not _has_exact_class(figure, DIAGRAM_CLASS):
        fail("diagram-marker-class")

    figure_id = figure.get("id")
    if not isinstance(figure_id, str) or not SAFE_IDENTIFIER_RE.fullmatch(figure_id):
        fail("diagram-figure-id")
        figure_id = ""

    direct_tags: list[Tag] = []
    for child in figure.contents:
        if isinstance(child, Tag):
            direct_tags.append(child)
        elif not _is_whitespace_text(child):
            fail("diagram-figure-structure")

    template_tags = [
        child for child in direct_tags if _has_class(child, TEMPLATE_CLASS)
    ]
    labels_tags = [child for child in direct_tags if _has_class(child, LABELS_CLASS)]
    captions = [child for child in direct_tags if _name(child) == "figcaption"]
    descriptions = [
        child
        for child in direct_tags
        if {
            IMAGE_ALT_CLASS,
            IMAGE_DESCRIPTION_CLASS,
            IMAGE_DESCRIPTION_LINK_CLASS,
        }.intersection(_classes(child))
    ]
    for code in diagnose_image_descriptions(figure):
        fail(code)
    expected = {
        id(tag) for tag in (*template_tags, *labels_tags, *captions, *descriptions)
    }
    if (
        len(template_tags) != 1
        or len(labels_tags) != 1
        or len(captions) > 1
        or any(id(child) not in expected for child in direct_tags)
    ):
        fail("diagram-figure-structure")

    if len(template_tags) != 1:
        return None
    template_tag = template_tags[0]
    if _name(template_tag) != "pre" or not _has_exact_class(
        template_tag, TEMPLATE_CLASS
    ):
        fail("diagram-template-marker")
    code_tags = [child for child in template_tag.contents if isinstance(child, Tag)]
    code_structure_invalid = len(code_tags) != 1 or any(
        not isinstance(child, Tag) and not _is_whitespace_text(child)
        for child in template_tag.contents
    )
    if len(code_tags) == 1 and _name(code_tags[0]) != "code":
        code_structure_invalid = True
    if code_structure_invalid:
        fail("diagram-template-invalid")
        return None
    template = _text_only(code_tags[0])
    if template is None:
        fail("diagram-template-invalid")
        return None
    placeholders = _parse_template(template, fail)
    if placeholders is None:
        return None

    if len(labels_tags) != 1:
        return None
    labels_tag = labels_tags[0]
    if _name(labels_tag) != "div" or not _has_exact_class(labels_tag, LABELS_CLASS):
        fail("diagram-labels-marker")

    label_paragraphs: list[Tag] = []
    for child in labels_tag.contents:
        if isinstance(child, Tag):
            if _name(child) != "p":
                fail("diagram-labels-invalid")
            else:
                label_paragraphs.append(child)
        elif not _is_whitespace_text(child):
            fail("diagram-labels-invalid")
    if len(label_paragraphs) > MAX_LABELS:
        fail("diagram-limit")
        return None

    labels: dict[str, str] = {}
    seen_ids: set[str] = set()
    for paragraph in label_paragraphs:
        raw_id = paragraph.get("id")
        if (
            not isinstance(raw_id, str)
            or not SAFE_IDENTIFIER_RE.fullmatch(raw_id)
            or not raw_id.startswith(f"{figure_id}--")
        ):
            fail("diagram-labels-invalid")
            continue
        key = raw_id[len(figure_id) + 2 :]
        if not _LOCAL_KEY_RE.fullmatch(key) or raw_id in seen_ids:
            fail("diagram-labels-invalid")
            continue
        seen_ids.add(raw_id)
        value = _text_only(paragraph)
        if value is None:
            fail("diagram-labels-invalid")
            continue
        if len(value) > MAX_LABEL_CHARS:
            fail("diagram-limit")
            continue
        if _label_value(value) is None:
            fail("diagram-labels-invalid")
            continue
        labels[key] = value

    if len(labels) > MAX_LABELS:
        fail("diagram-limit")
    referenced = set(placeholders)
    if referenced - labels.keys():
        fail("diagram-label-reference")
    if labels.keys() - referenced:
        fail("diagram-label-unused")
    if failed:
        return None
    return _Diagram(
        template=_normalize_template_newlines(template),
        labels=labels,
        placeholders=placeholders,
    )


def diagnose_mermaid_diagrams(soup: BeautifulSoup | Tag) -> tuple[str, ...]:
    """Return unique, bounded, source-free diagnostics for claimed diagrams."""

    codes: list[str] = []

    def add(code: str) -> None:
        if code not in codes and len(codes) < MAX_DIAGNOSTIC_CODES:
            codes.append(code)

    tags = _walk_tags(soup)
    diagram_tags = [tag for tag in tags if _has_class(tag, DIAGRAM_CLASS)]
    for tag in tags:
        if _has_class(tag, TEMPLATE_CLASS):
            parent = tag.parent
            if (
                _name(tag) != "pre"
                or not isinstance(parent, Tag)
                or _name(parent) != "figure"
                or not _has_class(parent, DIAGRAM_CLASS)
            ):
                add("diagram-template-marker")
        if _has_class(tag, LABELS_CLASS):
            parent = tag.parent
            if (
                _name(tag) != "div"
                or not isinstance(parent, Tag)
                or _name(parent) != "figure"
                or not _has_class(parent, DIAGRAM_CLASS)
            ):
                add("diagram-labels-marker")
        if _has_class(tag, DIAGRAM_CLASS) and _name(tag) != "figure":
            add("diagram-marker-element")

    if len(diagram_tags) > MAX_DIAGRAMS:
        add("diagram-limit")

    all_id_counts = Counter(
        tag.get("id") for tag in tags if isinstance(tag.get("id"), str)
    )
    for tag in diagram_tags:
        figure_id = tag.get("id")
        if isinstance(figure_id, str) and all_id_counts[figure_id] > 1:
            add("diagram-duplicate-id")
        labels_tags = [
            child
            for child in tag.find_all(True, recursive=False)
            if _has_class(child, LABELS_CLASS)
        ]
        for labels_tag in labels_tags:
            for paragraph in labels_tag.find_all("p", recursive=False):
                label_id = paragraph.get("id")
                if isinstance(label_id, str) and all_id_counts[label_id] > 1:
                    add("diagram-duplicate-id")

    for figure in diagram_tags[:MAX_DIAGRAMS]:
        _validate_figure(figure, add)
    return tuple(codes)


def materialize_mermaid(figure: Tag) -> str:
    """Validate one figure and return its safe, deterministic Mermaid source."""

    if not isinstance(figure, Tag):
        raise TypeError("figure must be a BeautifulSoup tag")
    local_codes: list[str] = []
    diagram = _validate_figure(figure, local_codes.append)
    if diagram is None or local_codes:
        raise ValueError(local_codes[0] if local_codes else "diagram-template-invalid")

    root_tags = _walk_tags(_document_root(figure))
    if sum(_has_class(tag, DIAGRAM_CLASS) for tag in root_tags) > MAX_DIAGRAMS:
        raise ValueError("diagram-limit")
    all_id_counts = Counter(
        tag.get("id") for tag in root_tags if isinstance(tag.get("id"), str)
    )
    target_ids = [figure.get("id")]
    for child in figure.find_all(True, recursive=False):
        if _has_class(child, LABELS_CLASS):
            target_ids.extend(
                paragraph.get("id")
                for paragraph in child.find_all("p", recursive=False)
            )
        if {
            IMAGE_ALT_CLASS,
            IMAGE_DESCRIPTION_CLASS,
            IMAGE_DESCRIPTION_LINK_CLASS,
        }.intersection(_classes(child)):
            target_ids.extend(
                tag.get("id")
                for tag in (child, *child.find_all(True))
                if tag.has_attr("id")
            )
    if any(
        isinstance(identifier, str) and all_id_counts[identifier] > 1
        for identifier in target_ids
    ):
        raise ValueError("diagram-duplicate-id")

    escaped_labels = {key: _label_value(value) for key, value in diagram.labels.items()}
    if any(value is None for value in escaped_labels.values()):
        raise ValueError("diagram-labels-invalid")
    placeholder_re = re.compile(r"\{\{([A-Za-z][A-Za-z0-9_]{0,63})\}\}")
    return placeholder_re.sub(
        lambda match: escaped_labels[match.group(1)] or "",
        _normalize_template_newlines(diagram.template),
    )


def _label_language(tag: Tag) -> str | None:
    current: Tag | None = tag
    while isinstance(current, Tag):
        value = current.get("lang") or current.get("xml:lang")
        if isinstance(value, str) and SAFE_LANGUAGE_RE.fullmatch(value):
            return value
        current = current.parent if isinstance(current.parent, Tag) else None
    return None


def parse_validated_flowchart(figure: Tag) -> ParsedFlowchart:
    """Return a renderer-neutral DTO after validating the complete figure."""

    materialize_mermaid(figure)
    local_codes: list[str] = []
    diagram = _validate_figure(figure, local_codes.append)
    if diagram is None or local_codes:
        raise ValueError(local_codes[0] if local_codes else "diagram-template-invalid")

    label_languages = {
        str(tag["id"])[len(str(figure["id"])) + 2 :]: _label_language(tag)
        for tag in figure.select("div.mermaid-labels > p")
    }

    lines = (
        _normalize_template_newlines(diagram.template).removesuffix("\n").split("\n")
    )
    direction = cast(
        Literal["TD", "TB", "LR", "BT", "RL"],
        lines[0].removeprefix("flowchart "),
    )
    nodes: list[FlowchartNode] = []
    edges: list[FlowchartEdge] = []

    for raw_line in lines[1:]:
        line = raw_line.strip(" ")
        node_match = _NODE_RE.fullmatch(line)
        if node_match is not None:
            shape_key = next(
                key
                for key in ("square", "round", "diamond")
                if node_match.group(key) is not None
            )
            nodes.append(
                FlowchartNode(
                    identifier=node_match.group("node"),
                    shape=cast(Literal["square", "round", "diamond"], shape_key),
                    label=diagram.labels[node_match.group(shape_key)],
                    label_language=label_languages[node_match.group(shape_key)],
                    label_id=f"{figure['id']}--{node_match.group(shape_key)}",
                )
            )
            continue

        labeled_edge = _LABELED_EDGE_RE.fullmatch(line)
        if labeled_edge is not None:
            edges.append(
                FlowchartEdge(
                    source=labeled_edge.group("left"),
                    target=labeled_edge.group("right"),
                    directed=True,
                    label=diagram.labels[labeled_edge.group("label")],
                    label_language=label_languages[labeled_edge.group("label")],
                    label_id=f"{figure['id']}--{labeled_edge.group('label')}",
                )
            )
            continue

        plain_edge = _PLAIN_EDGE_RE.fullmatch(line)
        if plain_edge is not None:
            edges.append(
                FlowchartEdge(
                    source=plain_edge.group("left"),
                    target=plain_edge.group("right"),
                    directed=line.split()[1] == "-->",
                )
            )

    return ParsedFlowchart(
        direction=direction,
        nodes=tuple(nodes),
        edges=tuple(edges),
        labels=tuple(diagram.labels.values()),
    )
