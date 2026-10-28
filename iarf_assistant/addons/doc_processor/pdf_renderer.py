"""Standalone, managed-runtime PDF renderer; never import the host application.

Input files are prepared by print_export in an owned temporary directory.
Resources are supplied as exact in-memory archive entries, never search roots.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from copy import deepcopy
from pathlib import Path

import pymupdf as fitz
from bs4 import BeautifulSoup
from bs4.element import AttributeValueList, PageElement, Tag

# The managed Pixi command runs this file as a script from its own project
# directory. Resolve the immutable program root from this file, not from cwd.
if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from carlina.core.diagram_rendering import validate_svg

_MAX_IMAGE_BYTES = 25 * 1024 * 1024

_CSS = """
* { color: #000000 !important; background-color: transparent !important; }
body { font-family: serif; font-size: 12pt; line-height: 15pt; margin: 0; }
p { margin: 0 0 6pt; orphans: 2; widows: 2; }
h1, h2, h3, h4, h5, h6 { font-family: serif; font-weight: bold;
    line-height: 1.1; page-break-after: avoid; }
h1 { font-size: 18pt; margin: 0 0 12pt; page-break-before: always; }
h2 { font-size: 15pt; margin: 18pt 0 8pt; }
h3 { font-size: 13pt; margin: 12pt 0 6pt; }
h4, h5, h6 { font-size: 12pt; margin: 12pt 0 6pt; }
h5, h6 { font-style: italic; }
a, a:link, a:visited { color: #000000 !important; text-decoration: underline; }
blockquote { margin: 8pt 18pt; }
table { border-collapse: collapse; margin: 8pt 0; width: 100%; }
th, td { border: 0.5pt solid black; padding: 4pt; vertical-align: top; }
th { font-weight: bold; }
figure { margin: 10pt 0; }
figcaption, caption { font-size: 10pt; line-height: 12pt; font-style: italic; }
[role="doc-endnotes"], [role="doc-endnote"] { font-size: 10pt; line-height: 12pt; }
pre, code { font-family: monospace; font-size: 10pt; }
hr { border: 0; border-top: 0.5pt solid black; margin: 10pt 0; }
ol[type='1'] { list-style-type: decimal; }
ol[type='a'] { list-style-type: lower-alpha; }
ol[type='A'] { list-style-type: upper-alpha; }
ol[type='i'] { list-style-type: lower-roman; }
ol[type='I'] { list-style-type: upper-roman; }
ol.pdf-list-run { margin-top: 0; margin-bottom: 0; }
ol.pdf-list-run-last { margin-bottom: 6pt; }
ol.pdf-list-manual { list-style-type: none; }
"""

_MAX_LIST_INTEGER_DIGITS = 1000
# Story clips or wraps larger generated markers. Print those labels as text in
# the private rendering copy, while retaining native numbering for short labels.
_MAX_NATIVE_LIST_COUNTER = 999
_LIST_MARKER_BLOCK_TAGS = frozenset(
    {"blockquote", "div", "h1", "h2", "h3", "h4", "h5", "h6", "p", "pre"}
)
_ROMAN_COUNTERS = (
    (1000, "M"),
    (900, "CM"),
    (500, "D"),
    (400, "CD"),
    (100, "C"),
    (90, "XC"),
    (50, "L"),
    (40, "XL"),
    (10, "X"),
    (9, "IX"),
    (5, "V"),
    (4, "IV"),
    (1, "I"),
)


def _parse_list_integer(value: object) -> int | None:
    """Parse a bounded signed HTML integer attribute without digit-limit leaks."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    digits = text[1:] if text.startswith(("+", "-")) else text
    if (
        not digits
        or len(digits) > _MAX_LIST_INTEGER_DIGITS
        or not digits.isascii()
        or not digits.isdigit()
    ):
        return None
    try:
        return int(text, 10)
    except ValueError:
        return None


def _append_pending_nodes(current_run: Tag, pending_nodes: list[PageElement]) -> None:
    for node in pending_nodes:
        current_run.append(node)
    pending_nodes.clear()


def _format_manual_list_marker(value: int, list_type: object) -> str:
    if value <= 0:
        label = str(value)
    elif list_type in {"a", "A"}:
        alphabet = (
            "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
            if list_type == "A"
            else "abcdefghijklmnopqrstuvwxyz"
        )
        letters: list[str] = []
        counter = value
        while counter:
            counter, remainder = divmod(counter - 1, len(alphabet))
            letters.append(alphabet[remainder])
        label = "".join(reversed(letters))
    elif list_type in {"i", "I"} and value <= 3999:
        remainder = value
        roman: list[str] = []
        for amount, symbol in _ROMAN_COUNTERS:
            count, remainder = divmod(remainder, amount)
            roman.append(symbol * count)
        label = "".join(roman)
        if list_type == "i":
            label = label.lower()
    else:
        label = str(value)
    return f"{label}. "


def _prepend_manual_list_marker(soup: BeautifulSoup, item: Tag, label: str) -> None:
    marker = soup.new_tag("span", attrs={"class": "pdf-list-marker"})
    marker.string = label
    target = item
    for child in item.contents:
        if isinstance(child, Tag):
            if child.name in _LIST_MARKER_BLOCK_TAGS:
                target = child
            break
        if str(child).strip():
            break
    target.insert(0, marker)


def _set_list_run_attributes(
    run: Tag,
    source_attributes: dict[str, str | AttributeValueList],
    *,
    start: int,
    first: bool,
    last: bool,
    manual: bool,
) -> None:
    attributes = deepcopy(source_attributes)
    attributes.pop("reversed", None)
    if not first:
        attributes.pop("id", None)
    attributes["start"] = str(start)

    source_classes = attributes.get("class")
    if isinstance(source_classes, str):
        classes = source_classes.split()
    elif isinstance(source_classes, list):
        classes = list(source_classes)
    else:
        classes = []
    classes = [
        class_name
        for class_name in classes
        if class_name not in {"pdf-list-run", "pdf-list-run-last", "pdf-list-manual"}
    ]
    classes.append("pdf-list-run")
    if last:
        classes.append("pdf-list-run-last")
    if manual:
        classes.append("pdf-list-manual")
    attributes["class"] = AttributeValueList(classes)
    run.attrs = attributes


def _normalize_ordered_list(soup: BeautifulSoup, ordered_list: Tag) -> None:
    direct_items = [
        child
        for child in ordered_list.contents
        if isinstance(child, Tag) and child.name == "li"
    ]
    if not direct_items:
        return

    reversed_list = "reversed" in ordered_list.attrs
    list_start = _parse_list_integer(ordered_list.get("start"))
    has_valid_item_value = any(
        _parse_list_integer(item.get("value")) is not None for item in direct_items
    )
    source_attributes = deepcopy(ordered_list.attrs)
    step = -1 if reversed_list else 1
    counter = (
        list_start
        if list_start is not None
        else len(direct_items)
        if reversed_list
        else 1
    )
    if not reversed_list and not has_valid_item_value:
        ordinary_counter = counter
        needs_normalization = False
        for item in direct_items:
            explicit_value = _parse_list_integer(item.get("value"))
            effective_value = (
                ordinary_counter if explicit_value is None else explicit_value
            )
            if effective_value <= 0 or effective_value > _MAX_NATIVE_LIST_COUNTER:
                needs_normalization = True
                break
            ordinary_counter = effective_value + 1
        if not needs_normalization:
            return

    original_children = list(ordered_list.contents)
    ordered_list.clear()
    current_run: Tag | None = None
    current_run_manual: bool | None = None
    pending_nodes: list[PageElement] = []
    output_runs: list[Tag] = []
    run_starts: list[int] = []
    run_manuals: list[bool] = []

    for node in original_children:
        if not isinstance(node, Tag) or node.name != "li":
            pending_nodes.append(node)
            continue

        explicit_value = _parse_list_integer(node.get("value"))
        effective_value = counter if explicit_value is None else explicit_value
        manual = effective_value <= 0 or effective_value > _MAX_NATIVE_LIST_COUNTER
        split = current_run is not None and (
            reversed_list
            or manual != current_run_manual
            or manual
            or effective_value != counter
        )
        if current_run is None:
            current_run = ordered_list
            output_runs.append(current_run)
            run_starts.append(effective_value)
            run_manuals.append(manual)
            _append_pending_nodes(current_run, pending_nodes)
        elif split:
            _append_pending_nodes(current_run, pending_nodes)
            current_run = soup.new_tag("ol")
            output_runs.append(current_run)
            run_starts.append(effective_value)
            run_manuals.append(manual)
        else:
            _append_pending_nodes(current_run, pending_nodes)
        if manual:
            _prepend_manual_list_marker(
                soup,
                node,
                _format_manual_list_marker(
                    effective_value, source_attributes.get("type")
                ),
            )
        current_run.append(node)
        counter = effective_value + step
        current_run_manual = manual

    if current_run is not None:
        _append_pending_nodes(current_run, pending_nodes)

    for index, (run, start, manual) in enumerate(
        zip(output_runs, run_starts, run_manuals, strict=True)
    ):
        _set_list_run_attributes(
            run,
            source_attributes,
            start=start,
            first=index == 0,
            last=index == len(output_runs) - 1,
            manual=manual,
        )

    cursor: PageElement = ordered_list
    for run in output_runs[1:]:
        cursor.insert_after(run)
        cursor = run


def _normalize_ordered_lists(soup: BeautifulSoup) -> None:
    """Adapt HTML ordered-list numbering for the private PDF-rendering soup."""
    for ordered_list in reversed(soup.find_all("ol")):
        _normalize_ordered_list(soup, ordered_list)


def _remove_empty_layout_pages(document) -> None:
    """Discard renderer spacer pages without dropping any navigation target."""
    destinations = {row[2] - 1 for row in document.get_toc() if row[2] > 0}
    destinations.update(
        link["page"]
        for page in document
        for link in page.get_links()
        if link.get("kind") == fitz.LINK_GOTO and link.get("page", -1) >= 0
    )
    empty = [
        page.number
        for page in document
        if page.number not in destinations
        and not page.get_text().strip()
        and not page.get_images()
        and not page.get_drawings()
        and not page.get_links()
    ]
    if empty and len(empty) < len(document):
        # PyMuPDF updates surviving link and outline page destinations.
        document.delete_pages(empty)


def render(job_path: Path, output_path: Path) -> None:
    job = json.loads(job_path.read_text(encoding="utf-8"))
    if type(job.get("start_h2_on_new_page")) is not bool:
        raise ValueError("pdf-option")
    soup = BeautifulSoup(job["html"], "html.parser")
    archive = fitz.Archive()
    dimensions = {}
    archive_names = {}
    svg_images = job.get("svg_images", [])
    if not isinstance(svg_images, list) or any(
        not isinstance(name, str) or name not in job["images"] for name in svg_images
    ):
        raise ValueError("pdf-resource")
    svg_names = set(svg_images)
    if len(svg_names) != len(svg_images):
        raise ValueError("pdf-resource")
    for name, filename in job["images"].items():
        if name != filename or re.fullmatch(r"image-\d+\.bin", filename) is None:
            raise ValueError("pdf-resource")
        data = (job_path.parent / filename).read_bytes()
        if not 0 < len(data) <= _MAX_IMAGE_BYTES:
            raise ValueError("pdf-resource")
        if name in svg_names:
            data = validate_svg(data)
            with fitz.open(stream=data, filetype="svg") as svg_document:
                rect = svg_document[0].rect
                dimensions[name] = (rect.width, rect.height)
            # Story selects its image decoder from the archive name. The owned
            # temporary job still carries exact image-N.bin resource bytes.
            archive_name = name.removesuffix(".bin") + ".svg"
        else:
            pixmap = fitz.Pixmap(data)
            dimensions[name] = (pixmap.width * 0.75, pixmap.height * 0.75)
            archive_name = name
        archive.add(data, archive_name)
        archive_names[name] = archive_name

    page_box = fitz.paper_rect("a4")
    margin = 25 * 72 / 25.4
    content_box = page_box + (margin, margin, -margin, -margin)
    for image in soup.find_all("img"):
        name = str(image.get("src") or "")
        if name not in dimensions:
            raise ValueError("pdf-resource")
        width, height = dimensions[name]
        scale = min(1.0, content_box.width / width, (content_box.height - 60) / height)
        image.attrs.pop("width", None)
        image.attrs.pop("height", None)
        image["src"] = archive_names[name]
        image["style"] = f"width: {width * scale:.3f}pt; height: {height * scale:.3f}pt"

    # Only navigable external URLs and document-local links may become actions.
    for link in soup.find_all("a", href=True):
        href = str(link["href"]).strip()
        if not href.startswith("#") and not re.match(
            r"^(?:https?://|mailto:)", href, re.IGNORECASE
        ):
            del link["href"]
    _normalize_ordered_lists(soup)
    css = _CSS + (
        "h2 { page-break-before: always; }" if job["start_h2_on_new_page"] else ""
    )
    story = fitz.Story(html=str(soup), user_css=css, archive=archive)
    headings = []

    def position(position):
        if position.heading and position.open_close & 1 and position.text:
            headings.append((position.heading, position.text, position.page_num))

    def rectangle(_number, _filled):
        return page_box, content_box, None

    with story.write_with_links(rectangle, positionfn=position) as document:
        outline = []
        previous_level = 0
        for level, title, page in headings:
            level = min(level, previous_level + 1)
            outline.append([level, title, page])
            previous_level = level
        if outline:
            document.set_toc(outline)
        _remove_empty_layout_pages(document)
        for page in document:
            footer = fitz.Rect(
                margin,
                page_box.height - 42,
                page_box.width - margin,
                page_box.height - 25,
            )
            page.insert_textbox(
                footer,
                str(page.number + 1),
                fontsize=9,
                fontname="tiro",
                align=1,
                color=(0, 0, 0),
            )
        metadata = job.get("metadata", {})
        document.set_metadata(
            {
                "title": str(metadata.get("title") or ""),
                "author": str(metadata.get("author") or ""),
                "subject": str(metadata.get("subject") or ""),
                "creator": "Carlina",
            }
        )
        language = metadata.get("language")
        if language:
            document.xref_set_key(
                document.pdf_catalog(), "Lang", fitz.get_pdf_str(language)
            )
        document.save(output_path, garbage=4, deflate=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        render(args.input, args.output)
    except Exception:  # Do not print private document content or filesystem errors.
        print("PDF export failed.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
