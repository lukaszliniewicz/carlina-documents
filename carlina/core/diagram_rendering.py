"""Render validated semantic flowcharts with the host's pinned Graphviz dot."""

from __future__ import annotations

import base64
import hashlib
import html as html_lib
import math
import os
import re
import subprocess
import sys
import tempfile
import time
import unicodedata
import xml.etree.ElementTree as ET
from collections.abc import Callable
from html.parser import HTMLParser
from itertools import pairwise
from pathlib import Path
from typing import Literal

from bs4 import BeautifulSoup, Tag

from .semantic_diagrams import (
    DIAGRAM_CLASS,
    LABELS_CLASS,
    ParsedFlowchart,
    diagnose_mermaid_diagrams,
    parse_validated_flowchart,
)
from .semantic_html import SAFE_LANGUAGE_RE
from .semantic_images import diagnose_image_descriptions, image_description_parts

OutputFormat = Literal["svg", "png"]
Runner = Callable[..., subprocess.CompletedProcess[bytes]]

MAX_HTML_BYTES = 20 * 1024 * 1024
MAX_OUTPUT_BYTES = 25 * 1024 * 1024
MAX_SVG_BYTES = 5 * 1024 * 1024
MAX_PNG_BYTES = MAX_OUTPUT_BYTES
MAX_RENDER_SECONDS = 5.0
MAX_PROJECTION_SECONDS = 30.0
MAX_SVG_COORDINATE = 100_000.0

_SVG_NAMESPACE = "http://www.w3.org/2000/svg"
_SVG = f"{{{_SVG_NAMESPACE}}}"
_XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
_XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"
_SVG_TAGS = frozenset(
    {
        "svg",
        "g",
        "title",
        "desc",
        "polygon",
        "polyline",
        "path",
        "ellipse",
        "rect",
        "text",
        "tspan",
    }
)
_SVG_TAG_ATTRIBUTES = {
    "svg": frozenset(
        {
            "width",
            "height",
            "viewBox",
            "preserveAspectRatio",
            "role",
            "aria-labelledby",
            _XML_LANG,
        }
    ),
    "g": frozenset({"class", "id", "transform"}),
    "title": frozenset({"id", _XML_LANG}),
    "desc": frozenset({"id"}),
    "polygon": frozenset({"fill", "stroke", "fill-rule", "points", "stroke-width"}),
    "polyline": frozenset({"fill", "stroke", "fill-rule", "points", "stroke-width"}),
    "path": frozenset({"fill", "stroke", "fill-rule", "d", "stroke-width"}),
    "ellipse": frozenset({"fill", "stroke", "cx", "cy", "rx", "ry", "stroke-width"}),
    "rect": frozenset(
        {"fill", "stroke", "x", "y", "width", "height", "rx", "ry", "stroke-width"}
    ),
    "text": frozenset(
        {"x", "y", "text-anchor", "font-family", "font-size", "font-weight", _XML_SPACE}
    ),
    "tspan": frozenset(
        {
            "x",
            "y",
            "dx",
            "dy",
            "text-anchor",
            "font-family",
            "font-size",
            "font-weight",
            _XML_SPACE,
        }
    ),
}
_SVG_ATTRIBUTES = frozenset(
    {
        "class",
        "id",
        "width",
        "height",
        "viewBox",
        "preserveAspectRatio",
        "transform",
        "fill",
        "fill-rule",
        "fill-opacity",
        "stroke",
        "stroke-width",
        "stroke-linecap",
        "stroke-linejoin",
        "stroke-dasharray",
        "stroke-dashoffset",
        "stroke-opacity",
        "opacity",
        "points",
        "d",
        "cx",
        "cy",
        "rx",
        "ry",
        "x",
        "y",
        "dx",
        "dy",
        "text-anchor",
        "font-family",
        "font-size",
        "font-weight",
        "textLength",
        "lengthAdjust",
        _XML_SPACE,
        _XML_LANG,
        "role",
        "aria-labelledby",
    }
)
_NUMBER = re.compile(r"[-+]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][-+]?[0-9]+)?")
_NUMBER_LIST = re.compile(r"^[0-9eE+.,\-\s]+$")
_PATH_DATA = re.compile(r"^[MmLlHhVvCcSsQqTtAaZz0-9eE+.,\-\s]+$")
_TRANSFORM = re.compile(
    r"^(?:(?:matrix|translate|scale|rotate|skewX|skewY)\(\s*[-+0-9.eE,\s]+\)\s*)+$"
)
_SVG_DOCTYPE = re.compile(
    r"<!DOCTYPE\s+svg\s+PUBLIC\s+(?P<q1>['\"])[^'\"]*(?P=q1)\s+"
    r"(?P<q2>['\"])[^'\"]*(?P=q2)\s*>",
    re.IGNORECASE | re.DOTALL,
)
_SVG_DTD_TOKEN = re.compile(r"<!\s*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)
_SVG_STYLESHEET_PI = re.compile(r"<\?xml-stylesheet\b", re.IGNORECASE)


def _source_free_error(message: str) -> ValueError:
    return ValueError(message)


def _parse_number(value: str) -> float:
    if not _NUMBER.fullmatch(value):
        raise _source_free_error("diagram-svg-invalid")
    parsed = float(value)
    if not math.isfinite(parsed) or abs(parsed) > MAX_SVG_COORDINATE:
        raise _source_free_error("diagram-svg-invalid")
    return parsed


def _parse_number_list(value: str) -> tuple[float, ...]:
    if not _NUMBER_LIST.fullmatch(value):
        raise _source_free_error("diagram-svg-invalid")
    matches = tuple(_NUMBER.finditer(value))
    if not matches:
        raise _source_free_error("diagram-svg-invalid")
    remainder = _NUMBER.sub("", value)
    if remainder.strip(" ,\t\r\n"):
        raise _source_free_error("diagram-svg-invalid")
    return tuple(_parse_number(match.group()) for match in matches)


def _validate_root_dimensions(root: ET.Element) -> None:
    width_text = root.get("width", "")
    height_text = root.get("height", "")
    view_box_text = root.get("viewBox", "")

    def dimension(value: str) -> float:
        value = value.removesuffix("pt")
        number = _parse_number(value)
        if number <= 0:
            raise _source_free_error("diagram-svg-invalid")
        return number

    width = dimension(width_text)
    height = dimension(height_text)
    view_box = _parse_number_list(view_box_text)
    if len(view_box) != 4 or view_box[2] <= 0 or view_box[3] <= 0:
        raise _source_free_error("diagram-svg-invalid")
    if any(
        number > MAX_SVG_COORDINATE
        for number in (width, height, view_box[2], view_box[3])
    ):
        raise _source_free_error("diagram-svg-invalid")


def _validate_attribute(name: str, value: str, tag: str) -> None:
    if name.startswith("on") or "url(" in value.casefold():
        raise _source_free_error("diagram-svg-invalid")
    if name not in _SVG_ATTRIBUTES:
        raise _source_free_error("diagram-svg-invalid")

    if name == "id":
        allowed = {"title": "diagram-title", "desc": "diagram-description"}.get(tag)
        if (allowed is not None and value != allowed) or (
            allowed is None
            and not re.fullmatch(r"(?:graph0|node[0-9]+|edge[0-9]+)", value)
        ):
            raise _source_free_error("diagram-svg-invalid")
    elif name == "role":
        if value != "img":
            raise _source_free_error("diagram-svg-invalid")
    elif name == "aria-labelledby":
        if value != "diagram-title diagram-description":
            raise _source_free_error("diagram-svg-invalid")
    elif name == _XML_LANG:
        if not SAFE_LANGUAGE_RE.fullmatch(value):
            raise _source_free_error("diagram-svg-invalid")
    elif name == "class":
        if value not in {"graph", "node", "edge"}:
            raise _source_free_error("diagram-svg-invalid")
    elif name in {"fill", "stroke"}:
        if value not in {"none", "black", "white"}:
            raise _source_free_error("diagram-svg-invalid")
    elif name == "font-family":
        if value != "DejaVu Sans":
            raise _source_free_error("diagram-svg-invalid")
    elif name == "font-size":
        if _parse_number(value) != 12:
            raise _source_free_error("diagram-svg-invalid")
    elif name == "font-weight":
        if value not in {"normal", "bold"}:
            raise _source_free_error("diagram-svg-invalid")
    elif name == "text-anchor":
        if value not in {"start", "middle", "end"}:
            raise _source_free_error("diagram-svg-invalid")
    elif name == _XML_SPACE:
        if value != "preserve":
            raise _source_free_error("diagram-svg-invalid")
    elif name == "fill-rule":
        if value not in {"nonzero", "evenodd"}:
            raise _source_free_error("diagram-svg-invalid")
    elif name == "stroke-linecap":
        if value not in {"butt", "round", "square"}:
            raise _source_free_error("diagram-svg-invalid")
    elif name == "stroke-linejoin":
        if value not in {"miter", "round", "bevel"}:
            raise _source_free_error("diagram-svg-invalid")
    elif name == "preserveAspectRatio":
        if not re.fullmatch(
            r"(?:none|x(?:Min|Mid|Max)Y(?:Min|Mid|Max)(?:\s+(?:meet|slice))?)",
            value,
        ):
            raise _source_free_error("diagram-svg-invalid")
    elif name == "transform":
        if not _TRANSFORM.fullmatch(value):
            raise _source_free_error("diagram-svg-invalid")
        _parse_number_list(re.sub(r"[A-Za-z]+\(|\)", " ", value))
    elif name == "points":
        numbers = _parse_number_list(value)
        if len(numbers) < 4 or len(numbers) % 2:
            raise _source_free_error("diagram-svg-invalid")
    elif name == "d":
        if not _PATH_DATA.fullmatch(value):
            raise _source_free_error("diagram-svg-invalid")
        _parse_number_list(re.sub(r"[MmLlHhVvCcSsQqTtAaZz]", " ", value))
    elif name in {
        "width",
        "height",
        "cx",
        "cy",
        "rx",
        "ry",
        "x",
        "y",
        "dx",
        "dy",
        "stroke-width",
        "stroke-dashoffset",
        "textLength",
    }:
        if tag == "svg" and name in {"width", "height"}:
            dimension = value.removesuffix("pt")
            _parse_number(dimension)
        else:
            _parse_number(value)
    elif name in {"opacity", "fill-opacity", "stroke-opacity"}:
        number = _parse_number(value)
        if not 0 <= number <= 1:
            raise _source_free_error("diagram-svg-invalid")
    elif name == "stroke-dasharray":
        if value != "none":
            _parse_number_list(value)
    elif name == "lengthAdjust" and value not in {"spacing", "spacingAndGlyphs"}:
        raise _source_free_error("diagram-svg-invalid")


def _sanitize_svg_tree(root: ET.Element) -> None:
    if root.tag != f"{_SVG}svg":
        raise _source_free_error("diagram-svg-invalid")
    _validate_root_dimensions(root)

    for element in root.iter():
        if not isinstance(element.tag, str) or not element.tag.startswith(_SVG):
            raise _source_free_error("diagram-svg-invalid")
        local_name = element.tag[len(_SVG) :]
        if local_name not in _SVG_TAGS:
            raise _source_free_error("diagram-svg-invalid")
        for name, value in element.attrib.items():
            if name not in _SVG_TAG_ATTRIBUTES[local_name]:
                raise _source_free_error("diagram-svg-invalid")
            _validate_attribute(name, value, local_name)
        if (
            local_name not in {"title", "desc", "text", "tspan"}
            and element.text
            and element.text.strip()
        ):
            raise _source_free_error("diagram-svg-invalid")
        if element.tail and element.tail.strip():
            raise _source_free_error("diagram-svg-invalid")
    if root.get("aria-labelledby") is not None:
        for tag, identifier in (
            ("title", "diagram-title"),
            ("desc", "diagram-description"),
        ):
            matching = [
                element for element in root.iter() if element.get("id") == identifier
            ]
            if (
                len(matching) != 1
                or matching[0].tag != f"{_SVG}{tag}"
                or not (matching[0].text or "").strip()
            ):
                raise _source_free_error("diagram-svg-invalid")


def validate_svg(data: bytes) -> bytes:
    """Return canonical safe SVG bytes or reject the complete document."""

    if not isinstance(data, bytes) or not data or len(data) > MAX_SVG_BYTES:
        raise _source_free_error("diagram-svg-invalid")
    try:
        source = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise _source_free_error("diagram-svg-invalid") from error

    source = _SVG_DOCTYPE.sub("", source, count=1)
    if _SVG_DTD_TOKEN.search(source) or _SVG_STYLESHEET_PI.search(source):
        raise _source_free_error("diagram-svg-invalid")
    try:
        root = ET.fromstring(source)
    except ET.ParseError as error:
        raise _source_free_error("diagram-svg-invalid") from error

    _sanitize_svg_tree(root)
    ET.register_namespace("", _SVG_NAMESPACE)
    sanitized = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    if len(sanitized) > MAX_SVG_BYTES:
        raise _source_free_error("diagram-svg-invalid")
    return sanitized


def _dot_executable() -> Path:
    prefix = Path(sys.prefix).resolve()
    if os.name == "nt":
        candidates = (
            prefix / "Library" / "bin" / "dot.exe",
            prefix / "Scripts" / "dot.exe",
        )
    else:
        candidates = (prefix / "bin" / "dot",)
    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            continue
        if not resolved.is_relative_to(prefix) or not resolved.is_file():
            continue
        if os.name != "nt" and not os.access(resolved, os.X_OK):
            continue
        return resolved
    raise RuntimeError("diagram-render-unavailable")


def _dot_quote(value: str) -> str:
    value = value.replace("\r\n", "\n").replace("\r", "\n").replace("\n", " ")
    wrapped: list[str] = []
    remaining = value
    while len(remaining) > 40:
        split = remaining.rfind(" ", 0, 41)
        if split <= 0:
            split = 40
            wrapped.append(remaining[:split])
            remaining = remaining[split:]
        else:
            wrapped.append(remaining[:split])
            remaining = remaining[split + 1 :]
    wrapped.append(remaining)

    escaped_lines: list[str] = []
    for line in wrapped:
        escaped: list[str] = []
        for character in line:
            category = unicodedata.category(character)
            if category in {"Cc", "Cs"}:
                raise _source_free_error("diagram-labels-invalid")
            if character == "\\":
                escaped.append("\\\\")
            elif character == '"':
                escaped.append('\\"')
            else:
                escaped.append(character)
        escaped_lines.append("".join(escaped))
    return '"' + r"\n".join(escaped_lines) + '"'


def _flowchart_dot(flowchart: ParsedFlowchart) -> str:
    rankdir = {"TD": "TB", "TB": "TB", "LR": "LR", "BT": "BT", "RL": "RL"}[
        flowchart.direction
    ]
    lines = [
        "digraph {",
        (
            f'  graph [rankdir="{rankdir}", bgcolor="white", nodesep="0.5", '
            'ranksep="0.7", overlap="false"];'
        ),
        (
            '  node [fontname="DejaVu Sans", fontsize="12", color="black", '
            'penwidth="1", margin="0.14,0.08"];'
        ),
        (
            '  edge [fontname="DejaVu Sans", fontsize="12", color="black", '
            'penwidth="1", arrowsize="0.75"];'
        ),
    ]
    shapes = {"square": "box", "round": "box", "diamond": "diamond"}
    for node in flowchart.nodes:
        attributes = [f'shape="{shapes[node.shape]}"']
        if node.shape == "round":
            attributes.append('style="rounded"')
        attributes.append(f"label={_dot_quote(node.label)}")
        lines.append(f"  {node.identifier} [{', '.join(attributes)}];")

    for edge in flowchart.edges:
        attributes: list[str] = []
        if not edge.directed:
            attributes.append('dir="none"')
        if edge.label is not None:
            attributes.append(f"label={_dot_quote(edge.label)}")
        suffix = f" [{', '.join(attributes)}]" if attributes else ""
        lines.append(f"  {edge.source} -> {edge.target}{suffix};")
    lines.append("}")
    return "\n".join(lines)


_DESCRIPTION_WORDS = {
    "en": {
        "title": "Flowchart",
        "short": "Flowchart. Full description follows.",
        "link": "Flowchart structure",
        "nodes": "Flowchart nodes",
        "connections": "Flowchart connections",
        "node": "Node",
        "label": "Label",
        "shape": "Shape",
        "from": "From node",
        "to": "To node",
        "connection": "Connection",
        "edge_label": "Connection label",
        "directed": "Directed",
        "undirected": "Undirected",
        "square": "Rectangle",
        "round": "Rounded rectangle",
        "diamond": "Diamond",
        "none": "No connections are shown.",
        "description": "Image description",
        "first": "First node",
        "second": "Second node",
        "intro": "For directed connections, the arrow goes from the first node to the second. Undirected connections have no arrow. Node numbers identify distinct nodes, including nodes with the same label.",
    },
    "pl": {
        "title": "Schemat blokowy",
        "short": "Schemat blokowy. Pełny opis poniżej.",
        "link": "Struktura schematu",
        "nodes": "Węzły schematu",
        "connections": "Połączenia schematu",
        "node": "Węzeł",
        "label": "Etykieta",
        "shape": "Kształt",
        "from": "Od węzła",
        "to": "Do węzła",
        "connection": "Połączenie",
        "edge_label": "Etykieta połączenia",
        "directed": "Skierowane",
        "undirected": "Nieskierowane",
        "square": "Prostokąt",
        "round": "Zaokrąglony prostokąt",
        "diamond": "Romb",
        "none": "Nie przedstawiono żadnych połączeń.",
        "description": "Opis ilustracji",
        "first": "Pierwszy węzeł",
        "second": "Drugi węzeł",
        "intro": "W połączeniach skierowanych strzałka prowadzi od pierwszego węzła do drugiego. Połączenia nieskierowane nie mają strzałki. Numery rozróżniają węzły, także te o takich samych etykietach.",
    },
    "de": {
        "title": "Flussdiagramm",
        "short": "Flussdiagramm. Die vollständige Beschreibung folgt.",
        "link": "Struktur des Flussdiagramms",
        "nodes": "Knoten des Flussdiagramms",
        "connections": "Verbindungen des Flussdiagramms",
        "node": "Knoten",
        "label": "Beschriftung",
        "shape": "Form",
        "from": "Von Knoten",
        "to": "Zu Knoten",
        "connection": "Verbindung",
        "edge_label": "Verbindungsbeschriftung",
        "directed": "Gerichtet",
        "undirected": "Ungerichtet",
        "square": "Rechteck",
        "round": "Abgerundetes Rechteck",
        "diamond": "Raute",
        "none": "Es sind keine Verbindungen dargestellt.",
        "description": "Bildbeschreibung",
        "first": "Erster Knoten",
        "second": "Zweiter Knoten",
        "intro": "Bei gerichteten Verbindungen zeigt der Pfeil vom ersten zum zweiten Knoten. Ungerichtete Verbindungen haben keinen Pfeil. Die Nummern unterscheiden die Knoten, auch bei gleicher Beschriftung.",
    },
    "fr": {
        "title": "Organigramme",
        "short": "Organigramme. La description complète suit.",
        "link": "Structure de l’organigramme",
        "nodes": "Nœuds de l’organigramme",
        "connections": "Connexions de l’organigramme",
        "node": "Nœud",
        "label": "Libellé",
        "shape": "Forme",
        "from": "Depuis le nœud",
        "to": "Vers le nœud",
        "connection": "Connexion",
        "edge_label": "Libellé de la connexion",
        "directed": "Orientée",
        "undirected": "Non orientée",
        "square": "Rectangle",
        "round": "Rectangle arrondi",
        "diamond": "Losange",
        "none": "Aucune connexion n’est représentée.",
        "description": "Description de l’image",
        "first": "Premier nœud",
        "second": "Second nœud",
        "intro": "Pour les connexions orientées, la flèche va du premier nœud au second. Les connexions non orientées n’ont pas de flèche. Les numéros distinguent les nœuds, y compris ceux portant le même libellé.",
    },
    "es": {
        "title": "Diagrama de flujo",
        "short": "Diagrama de flujo. A continuación se ofrece la descripción completa.",
        "link": "Estructura del diagrama",
        "nodes": "Nodos del diagrama",
        "connections": "Conexiones del diagrama",
        "node": "Nodo",
        "label": "Etiqueta",
        "shape": "Forma",
        "from": "Desde el nodo",
        "to": "Hasta el nodo",
        "connection": "Conexión",
        "edge_label": "Etiqueta de la conexión",
        "directed": "Dirigida",
        "undirected": "No dirigida",
        "square": "Rectángulo",
        "round": "Rectángulo redondeado",
        "diamond": "Rombo",
        "none": "No se muestran conexiones.",
        "description": "Descripción de la imagen",
        "first": "Primer nodo",
        "second": "Segundo nodo",
        "intro": "En las conexiones dirigidas, la flecha va del primer nodo al segundo. Las conexiones no dirigidas no tienen flecha. Los números distinguen los nodos, incluso cuando tienen la misma etiqueta.",
    },
    "ar": {
        "title": "مخطط انسيابي",
        "short": "مخطط انسيابي. يليه الوصف الكامل.",
        "link": "بنية المخطط",
        "nodes": "عقد المخطط",
        "connections": "روابط المخطط",
        "node": "العقدة",
        "label": "التسمية",
        "shape": "الشكل",
        "from": "من العقدة",
        "to": "إلى العقدة",
        "connection": "الرابط",
        "edge_label": "تسمية الرابط",
        "directed": "موجّه",
        "undirected": "غير موجّه",
        "square": "مستطيل",
        "round": "مستطيل بزوايا مستديرة",
        "diamond": "معيّن",
        "none": "لا تظهر أي روابط.",
        "description": "وصف الصورة",
        "first": "العقدة الأولى",
        "second": "العقدة الثانية",
        "intro": "في الروابط الموجّهة، يشير السهم من العقدة الأولى إلى الثانية. الروابط غير الموجّهة لا تحتوي على سهم. تميّز الأرقام بين العقد، بما فيها العقد ذات التسمية نفسها.",
    },
}


def _inherited_language(figure: Tag, default_language: str = "en") -> str:
    current: Tag | None = figure
    while isinstance(current, Tag):
        value = current.get("lang") or current.get("xml:lang")
        if isinstance(value, str) and SAFE_LANGUAGE_RE.fullmatch(value):
            return value
        current = current.parent if isinstance(current.parent, Tag) else None
    return default_language


def _description_words(language: str) -> tuple[str, dict[str, str]]:
    base = language.split("-", 1)[0].casefold()
    selected = base if base in _DESCRIPTION_WORDS else "en"
    return selected, _DESCRIPTION_WORDS[selected]


def _diagram_language(figure: Tag, default_language: str = "en") -> str:
    labels = figure.find("div", class_=LABELS_CLASS, recursive=False)
    if isinstance(labels, Tag):
        languages = {
            _inherited_language(tag, default_language)
            for tag in labels.find_all("p", recursive=False)
        }
        if len(languages) == 1:
            return next(iter(languages))
    return _inherited_language(figure, default_language)


def _flowchart_description_text(flowchart: ParsedFlowchart, language: str) -> str:
    _, words = _description_words(language)
    numbers = {node.identifier: index for index, node in enumerate(flowchart.nodes, 1)}
    parts = [words["nodes"] + "."]
    parts.extend(
        f"{words['node']} {numbers[node.identifier]} ({words[node.shape]}): {node.label}."
        for node in flowchart.nodes
    )
    parts.append(words["connections"] + ".")
    if not flowchart.edges:
        parts.append(words["none"])
    for edge in flowchart.edges:
        connection = words["directed" if edge.directed else "undirected"]
        first = words["from"] if edge.directed else words["first"]
        second = words["to"] if edge.directed else words["second"]
        text = f"{first} {numbers[edge.source]}; {second} {numbers[edge.target]}; {connection}."
        if edge.label is not None:
            text += f" {words['edge_label']}: {edge.label}."
        parts.append(text)
    return "\n".join(parts)


def _add_accessible_description(
    data: bytes,
    flowchart: ParsedFlowchart,
    *,
    title_text: str,
    language: str,
    title_language: str,
) -> bytes:
    try:
        root = ET.fromstring(data)
    except ET.ParseError as error:
        raise _source_free_error("diagram-svg-invalid") from error
    title = root.find(f"{_SVG}title")
    if title is None:
        title = ET.Element(f"{_SVG}title")
        root.insert(0, title)
    title.text = title_text
    title.set("id", "diagram-title")
    title.set(_XML_LANG, title_language)
    description = root.find(f"{_SVG}desc")
    if description is None:
        description = ET.Element(f"{_SVG}desc")
        root.insert(list(root).index(title) + 1, description)
    description.text = _flowchart_description_text(flowchart, language)
    description.set("id", "diagram-description")
    root.set("role", "img")
    root.set("aria-labelledby", "diagram-title diagram-description")
    root.set(_XML_LANG, language)
    ET.register_namespace("", _SVG_NAMESPACE)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _run_graphviz(
    dot_source: str,
    output_format: OutputFormat,
    runner: Runner | None,
    timeout: float,
) -> bytes:
    executable = _dot_executable()
    limit = MAX_SVG_BYTES if output_format == "svg" else MAX_PNG_BYTES
    try:
        with tempfile.TemporaryDirectory(
            prefix="carlina-diagram-"
        ) as temporary_directory:
            output_path = Path(temporary_directory) / f"rendered.{output_format}"
            command = [
                str(executable),
                f"-T{output_format}",
                "-o",
                str(output_path),
            ]
            run = subprocess.run if runner is None else runner
            completed = run(
                command,
                input=dot_source.encode("utf-8"),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=timeout,
                check=False,
                shell=False,
            )
            if completed.returncode != 0:
                raise RuntimeError("diagram-render-failed")
            if not output_path.is_file() or output_path.stat().st_size > limit:
                raise RuntimeError("diagram-output-limit")
            output = output_path.read_bytes()
    except subprocess.TimeoutExpired:
        raise TimeoutError("diagram-render-timeout") from None
    except OSError, subprocess.SubprocessError:
        raise RuntimeError("diagram-render-failed") from None

    if len(output) > limit:
        raise RuntimeError("diagram-output-limit")
    if output_format == "svg":
        return validate_svg(output)
    if not output.startswith(b"\x89PNG\r\n\x1a\n"):
        raise RuntimeError("diagram-output-invalid")
    return output


def _render_flowchart(
    flowchart: ParsedFlowchart,
    output_format: OutputFormat,
    runner: Runner | None,
    timeout: float,
    *,
    title_text: str = "Flowchart",
    language: str = "en",
    title_language: str = "en",
) -> bytes:
    rendered = _run_graphviz(_flowchart_dot(flowchart), output_format, runner, timeout)
    if output_format == "svg":
        rendered = _add_accessible_description(
            rendered,
            flowchart,
            title_text=title_text,
            language=language,
            title_language=title_language,
        )
        rendered = validate_svg(rendered)
    return rendered


def _validate_format(output_format: str) -> OutputFormat:
    if output_format not in {"svg", "png"}:
        raise _source_free_error("diagram-format-invalid")
    return output_format  # type: ignore[return-value]


def _validate_figure_context(figure: Tag) -> ParsedFlowchart:
    if not isinstance(figure, Tag):
        raise TypeError("figure must be a BeautifulSoup tag")
    root: Tag = figure
    while isinstance(root.parent, Tag):
        root = root.parent
    try:
        root_size = len(str(root).encode("utf-8"))
    except UnicodeEncodeError as error:
        raise _source_free_error("diagram-html-invalid") from error
    if root_size > MAX_HTML_BYTES:
        raise _source_free_error("diagram-limit")
    diagnostics = diagnose_mermaid_diagrams(root) + diagnose_image_descriptions(root)
    if diagnostics:
        raise _source_free_error(diagnostics[0])
    return parse_validated_flowchart(figure)


def render_diagram(
    figure: Tag,
    *,
    output_format: OutputFormat = "svg",
    runner: Runner | None = None,
) -> bytes:
    """Compile one validated semantic flowchart to sanitized SVG or PNG bytes."""

    normalized_format = _validate_format(output_format)
    flowchart = _validate_figure_context(figure)
    language = _diagram_language(figure)
    short, _, _ = image_description_parts(figure)
    generated_language, words = _description_words(language)
    return _render_flowchart(
        flowchart,
        normalized_format,
        runner,
        MAX_RENDER_SECONDS,
        title_text=short.get_text(" ", strip=True)
        if short is not None
        else words["title"],
        language=language,
        title_language=_inherited_language(short)
        if short is not None
        else generated_language,
    )


class _FigureSpanParser(HTMLParser):
    def __init__(self, source: str) -> None:
        super().__init__(convert_charrefs=True)
        self._source = source
        self._line_starts = [0]
        self._line_starts.extend(match.end() for match in re.finditer("\n", source))
        self._figures: list[tuple[int, bool]] = []
        self.spans: list[tuple[int, int]] = []
        self.invalid = False

    def _offset(self) -> int:
        line, column = self.getpos()
        return self._line_starts[line - 1] + column

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() != "figure":
            return
        values = dict(attrs)
        classes = (values.get("class") or "").split()
        self._figures.append((self._offset(), DIAGRAM_CLASS in classes))

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() != "figure":
            return
        if not self._figures:
            self.invalid = True
            return
        start, is_diagram = self._figures.pop()
        if is_diagram:
            end = self._source.find(">", self._offset())
            if end < 0:
                self.invalid = True
            else:
                self.spans.append((start, end + 1))


class _DiagramMarkerParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.found = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        classes = (dict(attrs).get("class") or "").split()
        if {DIAGRAM_CLASS, LABELS_CLASS, "mermaid-template"}.intersection(classes):
            self.found = True


def _has_diagram_markers(source: str) -> bool:
    parser = _DiagramMarkerParser()
    parser.feed(source)
    parser.close()
    return parser.found


def _figure_source_spans(source: str) -> tuple[tuple[int, int], ...]:
    parser = _FigureSpanParser(source)
    parser.feed(source)
    parser.close()
    if parser.invalid or parser._figures:
        raise _source_free_error("diagram-html-invalid")
    return tuple(sorted(parser.spans))


def _figure_classes(figure: Tag) -> tuple[str, ...]:
    raw = figure.get("class")
    if isinstance(raw, list):
        return tuple(str(value) for value in raw)
    if isinstance(raw, str):
        return tuple(raw.split())
    return ()


def _structure_description_html(
    flowchart: ParsedFlowchart, *, identifier: str, language: str
) -> str:
    generated_language, words = _description_words(language)
    lang = f' lang="{generated_language}"'
    numbers = {node.identifier: index for index, node in enumerate(flowchart.nodes, 1)}
    used_label_ids: set[str] = set()

    def label_cell(text: str, label_language: str | None, label_id: str | None) -> str:
        attribute = ""
        if label_id is not None and label_id not in used_label_ids:
            used_label_ids.add(label_id)
            attribute = f' id="{html_lib.escape(label_id, quote=True)}"'
        content = html_lib.escape(text)
        if attribute:
            # Pandoc retains span bookmarks inside table cells across DOCX
            # export; a cell ID alone would lose the original label target.
            content = f"<span{attribute}>{content}</span>"
        return (
            f'<td lang="{html_lib.escape(label_language or language, quote=True)}">'
            f"{content}</td>"
        )

    node_rows = "".join(
        f'<tr><th scope="row">{numbers[node.identifier]}</th>'
        f"{label_cell(node.label, node.label_language, node.label_id)}"
        f"<td{lang}>{words[node.shape]}</td></tr>"
        for node in flowchart.nodes
    )
    node_headers = "".join(
        f'<th scope="col"{lang}>{words[key]}</th>' for key in ("node", "label", "shape")
    )
    connections = f"<p{lang}>{words['none']}</p>"
    if flowchart.edges:
        edge_rows = "".join(
            f"<tr><td>{numbers[edge.source]}</td><td>{numbers[edge.target]}</td>"
            f"<td{lang}>{words['directed' if edge.directed else 'undirected']}</td>"
            f"{label_cell(edge.label or '', edge.label_language, edge.label_id)}</tr>"
            for edge in flowchart.edges
        )
        edge_headers = "".join(
            f'<th scope="col"{lang}>{words[key]}</th>'
            for key in ("first", "second", "connection", "edge_label")
        )
        connections = (
            f'<table class="diagram-connections"><caption{lang}>{words["connections"]}</caption>'
            f"<thead><tr>{edge_headers}</tr></thead><tbody>{edge_rows}</tbody></table>"
        )
    return (
        f'<p class="diagram-description-link"{lang}><a href="#{identifier}">{words["link"]}</a></p>'
        f'<div class="diagram-description-v1" id="{identifier}" lang="{html_lib.escape(language, quote=True)}">'
        f"<p{lang}>{words['intro']}</p>"
        f'<table class="diagram-nodes"><caption{lang}>{words["nodes"]}</caption>'
        f"<thead><tr>{node_headers}</tr></thead><tbody>{node_rows}</tbody></table>"
        f"{connections}</div>"
    )


def _structure_description_id(figure: Tag, reserved_ids: set[str]) -> str:
    digest = hashlib.sha256(str(figure["id"]).encode("utf-8")).hexdigest()[:20]
    base = f"diagram-structure-{digest}"
    identifier = base
    index = 1
    while identifier in reserved_ids:
        identifier = f"{base}-{index}"
        index += 1
    reserved_ids.add(identifier)
    return identifier


def _projected_figure(
    figure: Tag,
    flowchart: ParsedFlowchart,
    rendered: bytes,
    output_format: OutputFormat,
    *,
    description_id: str,
    language: str,
) -> str:
    generated_language, words = _description_words(language)
    short, description, link = image_description_parts(figure)
    alt = html_lib.escape(
        short.get_text(" ", strip=True) if short is not None else words["short"],
        quote=True,
    )
    alt_language = (
        _inherited_language(short, language)
        if short is not None
        else generated_language
    )
    image_attributes = ""
    if short is not None and short.has_attr("id"):
        image_attributes += f' id="{html_lib.escape(str(short["id"]), quote=True)}"'
    # These annotations are applied to a validated export copy by the EPUB
    # review projection; incoming canonical HTML cannot assert them.
    if figure.get("data-epub-description-reviewed") == "true":
        image_attributes += ' data-epub-description-reviewed="true"'
    if figure.get("data-epub-decorative-image") == "true":
        alt = ""
        image_attributes += ' role="presentation"'
    attributes = ' class="diagram-rendered-v1"' + "".join(
        f' {key}="{html_lib.escape(str(figure[key]), quote=True)}"'
        for key in (
            "id",
            "lang",
            "dir",
            "role",
            "aria-label",
            "aria-labelledby",
            "aria-describedby",
            "title",
        )
        if figure.has_attr(key) and not (key == "role" and figure.get(key) == "img")
    )
    if figure.has_attr("hidden"):
        attributes += " hidden"
    data = base64.b64encode(rendered).decode("ascii")
    media_type = "image/svg+xml" if output_format == "svg" else "image/png"
    labels_tag = figure.find("div", class_=LABELS_CLASS, recursive=False)
    if not isinstance(labels_tag, Tag):
        raise _source_free_error("diagram-labels-invalid")
    caption = figure.find("figcaption", recursive=False)
    retained = {id(tag) for tag in (caption, description, link) if tag is not None}
    removed = [
        child
        for child in figure.find_all(True, recursive=False)
        if id(child) not in retained
    ]
    label_ids = {str(tag["id"]) for tag in labels_tag.find_all("p", recursive=False)}
    if short is not None and short.has_attr("id"):
        label_ids.add(str(short["id"]))
    anchors = "".join(
        f'<span id="{html_lib.escape(str(tag["id"]), quote=True)}"></span>'
        for child in removed
        for tag in [child, *child.find_all(True)]
        if tag.has_attr("id") and str(tag["id"]) not in label_ids
    )
    caption_markup = str(caption) if caption is not None else ""
    description_markup = ""
    if description is not None:
        description_markup = (
            str(link)
            if link is not None
            else f'<p class="image-description-link" lang="{generated_language}"><a href="#{html_lib.escape(str(description["id"]), quote=True)}">{words["description"]}</a></p>'
        ) + str(description)
    structure = _structure_description_html(
        flowchart, identifier=description_id, language=language
    )
    return (
        f"<figure{attributes}>"
        f"{caption_markup}"
        f'<img src="data:{media_type};base64,{data}" alt="{alt}" lang="{html_lib.escape(alt_language, quote=True)}"{image_attributes}>'
        f"{anchors}{description_markup}{structure}</figure>"
    )


def project_diagrams_html(
    html: str,
    *,
    output_format: OutputFormat = "svg",
    runner: Runner | None = None,
    default_language: str = "en",
) -> str:
    """Render validated diagrams while retaining every other source byte."""

    normalized_format = _validate_format(output_format)
    if not isinstance(html, str):
        raise TypeError("html must be text")
    if not any(
        marker in html for marker in (DIAGRAM_CLASS, LABELS_CLASS, "mermaid-template")
    ):
        return html
    deadline = time.monotonic() + MAX_PROJECTION_SECONDS
    if not _has_diagram_markers(html):
        return html
    if not SAFE_LANGUAGE_RE.fullmatch(default_language):
        raise _source_free_error("diagram-language-invalid")
    try:
        input_size = len(html.encode("utf-8"))
    except UnicodeEncodeError as error:
        raise _source_free_error("diagram-html-invalid") from error
    if input_size > MAX_HTML_BYTES:
        raise _source_free_error("diagram-limit")

    soup = BeautifulSoup(html, "html.parser")
    diagnostics = diagnose_mermaid_diagrams(soup) + diagnose_image_descriptions(soup)
    if diagnostics:
        raise _source_free_error(diagnostics[0])
    figures = [
        tag
        for tag in soup.find_all(True)
        if "figure" == str(tag.name).casefold()
        and DIAGRAM_CLASS in _figure_classes(tag)
    ]
    if not figures:
        if time.monotonic() > deadline:
            raise TimeoutError("diagram-projection-timeout")
        return html

    spans = _figure_source_spans(html)
    if len(spans) != len(figures):
        raise _source_free_error("diagram-html-invalid")
    for previous, current in pairwise(spans):
        if current[0] < previous[1]:
            raise _source_free_error("diagram-html-invalid")

    replacements: list[str] = []
    reserved_ids = {str(tag["id"]) for tag in soup.find_all(True) if tag.has_attr("id")}
    total_image_bytes = 0
    for figure in figures:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("diagram-projection-timeout")
        flowchart = parse_validated_flowchart(figure)
        language = _diagram_language(figure, default_language)
        short, _, _ = image_description_parts(figure)
        generated_language, words = _description_words(language)
        rendered = _render_flowchart(
            flowchart,
            normalized_format,
            runner,
            min(MAX_RENDER_SECONDS, remaining),
            title_text=short.get_text(" ", strip=True)
            if short is not None
            else words["title"],
            language=language,
            title_language=_inherited_language(short, language)
            if short is not None
            else generated_language,
        )
        total_image_bytes += len(rendered)
        if total_image_bytes > MAX_OUTPUT_BYTES:
            raise _source_free_error("diagram-output-limit")
        replacements.append(
            _projected_figure(
                figure,
                flowchart,
                rendered,
                normalized_format,
                description_id=_structure_description_id(figure, reserved_ids),
                language=language,
            )
        )
        if time.monotonic() > deadline:
            raise TimeoutError("diagram-projection-timeout")

    output_parts: list[str] = []
    cursor = 0
    for (start, end), replacement in zip(spans, replacements, strict=True):
        output_parts.extend((html[cursor:start], replacement))
        cursor = end
    output_parts.append(html[cursor:])
    output = "".join(output_parts)
    try:
        output_size = len(output.encode("utf-8"))
    except UnicodeEncodeError as error:
        raise _source_free_error("diagram-html-invalid") from error
    if output_size > MAX_OUTPUT_BYTES:
        raise _source_free_error("diagram-output-limit")
    if time.monotonic() > deadline:
        raise TimeoutError("diagram-projection-timeout")
    return output
