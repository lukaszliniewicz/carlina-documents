"""Shared print-export preparation without changing stored document content."""

from __future__ import annotations

import base64
import binascii
import io
import json
import re
import subprocess
import tempfile
import unicodedata
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from zipfile import ZipFile

from bs4 import BeautifulSoup, Comment, NavigableString, Tag
from lxml import etree
from carlina.core.diagram_rendering import project_diagrams_html, validate_svg
from carlina.core.export_notes import normalize_export_note_roles

from .semantic_document import validate_semantic_export

_WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_NS = {"w": _WORD_NS}
_DATA_IMAGE = re.compile(
    r"data:image/(?:png|jpeg|gif|webp);base64,([A-Za-z0-9+/=\s]+)", re.IGNORECASE
)
_MAX_IMAGE_BYTES = 25 * 1024 * 1024


def docx_reference_path() -> str:
    """Require the immutable, packaged template; never write into program files."""
    path = Path(__file__).with_name("templates") / "reference.docx"
    if not path.is_file():
        raise RuntimeError("document-export-template-unavailable")
    return str(path)


def _title_key(value: str) -> str:
    value = unicodedata.normalize("NFKC", value)
    return " ".join(value.split()).strip().rstrip(".?!:;。！？：；").casefold()


def has_leading_source_title(html: str, title: str | None = None) -> bool:
    soup = BeautifulSoup(html, "html.parser")
    if not title and soup.title:
        title = soup.title.get_text(" ", strip=True)
    if not title or not _title_key(title):
        return False
    root = soup.body or soup
    for node in root.descendants:
        if isinstance(node, Tag) and node.name == "h1":
            if _title_key(node.get_text(" ", strip=True)) == _title_key(title):
                return True
            # Subtitles remain visible source content, but are not part of
            # the main-title comparison for suppressing a generated prefix.
            for subtitle in node.select(".subtitle"):
                subtitle.decompose()
            return _title_key(node.get_text(" ", strip=True)) == _title_key(title)
        if (
            isinstance(node, NavigableString)
            and not isinstance(node, Comment)
            and node.strip()
        ):
            if any(
                parent.name in {"head", "script", "style", "title"}
                or parent.get("role") == "doc-pagebreak"
                or parent.get("epub:type") == "pagebreak"
                for parent in node.parents
            ):
                continue
            return False
    return False


def finish_docx_export(
    data: bytes,
    source_html: str,
    *,
    title: str | None = None,
    start_h2_on_new_page: bool = False,
) -> bytes:
    """Drop only a redundant generated title block and apply the print option.

    The source H1, its bookmarks, all source prose and package metadata survive.
    Pandoc's generated Title/Subtitle/Author/Date paragraphs are a single block.
    """
    if type(start_h2_on_new_page) is not bool:
        raise ValueError("document-export-page-break-option")
    source = BeautifulSoup(source_html, "html.parser")
    effective_title = title or (
        source.title.get_text(" ", strip=True) if source.title else ""
    )
    parser = etree.XMLParser(resolve_entities=False, no_network=True)
    with ZipFile(io.BytesIO(data)) as archive:
        document = etree.fromstring(archive.read("word/document.xml"), parser)
        body = document.find("w:body", _NS)
        if body is not None and has_leading_source_title(source_html, effective_title):
            prefix = []
            seen_title = False
            for paragraph in list(body):
                if paragraph.tag != f"{{{_WORD_NS}}}p":
                    break
                style = paragraph.find("w:pPr/w:pStyle", _NS)
                style_id = (
                    style.get(f"{{{_WORD_NS}}}val", "") if style is not None else ""
                )
                visible = "".join(paragraph.xpath(".//w:t/text()", namespaces=_NS))
                if style_id == "Title" and _title_key(visible) == _title_key(
                    effective_title
                ):
                    seen_title = True
                elif visible and (
                    not seen_title or style_id not in {"Subtitle", "Author", "Date"}
                ):
                    break
                if paragraph.xpath(
                    ".//w:drawing | .//w:pict | .//w:bookmarkStart | .//w:sectPr",
                    namespaces=_NS,
                ):
                    break
                prefix.append(paragraph)
            if seen_title:
                for paragraph in prefix:
                    body.remove(paragraph)

        # A leading source page-anchor paragraph is not a title page. Prevent
        # the first visible heading's style from manufacturing a blank page,
        # while retaining every bookmark and any genuine preceding illustration.
        if body is not None:
            # Pandoc wraps source page anchors in otherwise empty paragraphs.
            # They can spill onto a page of their own before a chapter break.
            # Word permits bookmarks directly in the body; hoist them in the
            # same order, without touching prose, ranges, or deliberate spacing.
            bookmark_tags = {
                f"{{{_WORD_NS}}}bookmarkStart",
                f"{{{_WORD_NS}}}bookmarkEnd",
            }
            for paragraph in list(body):
                children = list(paragraph)
                if (
                    paragraph.tag == f"{{{_WORD_NS}}}p"
                    and any(child.tag in bookmark_tags for child in children)
                    and all(
                        child.tag in bookmark_tags | {f"{{{_WORD_NS}}}pPr"}
                        for child in children
                    )
                    and paragraph.find("w:pPr/w:sectPr", _NS) is None
                ):
                    index = body.index(paragraph)
                    for bookmark in [
                        child for child in children if child.tag in bookmark_tags
                    ]:
                        body.insert(index, bookmark)
                        index += 1
                    body.remove(paragraph)
            for paragraph in body:
                if paragraph.tag in {
                    f"{{{_WORD_NS}}}bookmarkStart",
                    f"{{{_WORD_NS}}}bookmarkEnd",
                }:
                    continue
                if paragraph.tag != f"{{{_WORD_NS}}}p" or paragraph.xpath(
                    ".//w:drawing | .//w:pict | .//w:sectPr", namespaces=_NS
                ):
                    break
                if not "".join(
                    paragraph.xpath(".//w:t/text()", namespaces=_NS)
                ).strip():
                    continue
                style = paragraph.find("w:pPr/w:pStyle", _NS)
                if style is not None and style.get(f"{{{_WORD_NS}}}val") in {
                    "Heading1",
                    "Heading2",
                }:
                    properties = paragraph.find("w:pPr", _NS)
                    setting = properties.find("w:pageBreakBefore", _NS)
                    if setting is None:
                        setting = etree.SubElement(
                            properties, f"{{{_WORD_NS}}}pageBreakBefore"
                        )
                    setting.set(f"{{{_WORD_NS}}}val", "0")
                break

        styles = etree.fromstring(archive.read("word/styles.xml"), parser)
        for heading in styles.xpath('//w:style[@w:styleId="Heading2"]', namespaces=_NS):
            properties = heading.find("w:pPr", _NS)
            if properties is None:
                properties = etree.SubElement(heading, f"{{{_WORD_NS}}}pPr")
            setting = properties.find("w:pageBreakBefore", _NS)
            if setting is None:
                setting = etree.SubElement(properties, f"{{{_WORD_NS}}}pageBreakBefore")
            setting.set(f"{{{_WORD_NS}}}val", "1" if start_h2_on_new_page else "0")
        changed = {
            "word/document.xml": etree.tostring(
                document, xml_declaration=True, encoding="UTF-8", standalone=True
            ),
            "word/styles.xml": etree.tostring(
                styles, xml_declaration=True, encoding="UTF-8", standalone=True
            ),
        }
        buffer = io.BytesIO()
        with ZipFile(buffer, "w") as output:
            for entry in archive.infolist():
                output.writestr(
                    entry, changed.get(entry.filename, archive.read(entry.filename))
                )
    return buffer.getvalue()


def render_pdf_export(
    html: str,
    *,
    metadata: Mapping[str, Any],
    images: Mapping[str, bytes] | None = None,
    run_managed_process: Callable[..., Any],
    start_h2_on_new_page: bool = False,
) -> bytes:
    """Render with the locked add-on runtime in a timeout-bounded subprocess.

    Only exact supplied image bytes or embedded raster data reach the renderer.
    Neither source URLs nor filesystem paths become renderer resource roots.
    """
    if type(start_h2_on_new_page) is not bool:
        raise ValueError("document-export-page-break-option")
    if not callable(run_managed_process):
        raise RuntimeError("document-export-pdf-unavailable")
    soup = BeautifulSoup(html, "html.parser")
    validate_semantic_export(soup, export_name="PDF export input")
    normalize_export_note_roles(soup)
    # Existing image sources keep the raster-only policy. Generated SVGs are
    # recognized only after the canonical diagram has passed validation.
    for image in soup.find_all("img"):
        if str(image.get("src") or "").lower().startswith("data:image/svg"):
            raise ValueError("document-export-pdf-image-unavailable")
    figure_ids = [
        str(figure["id"]) for figure in soup.select("figure.mermaid-diagram-v1[id]")
    ]
    generated_svg_sources: set[str] = set()
    if figure_ids:
        soup = BeautifulSoup(project_diagrams_html(str(soup)), "html.parser")
        for figure_id in figure_ids:
            figure = soup.find("figure", id=figure_id)
            if figure is None or "diagram-rendered-v1" not in figure.get("class", []):
                raise ValueError("document-export-pdf-diagram-projection")
            image = figure.find("img")
            if image is None:
                raise ValueError("document-export-pdf-diagram-projection")
            source = str(image.get("src") or "")
            if not source.startswith("data:image/svg+xml;base64,"):
                raise ValueError("document-export-pdf-diagram-projection")
            generated_svg_sources.add(source)
    for tag in soup.find_all(
        ["script", "style", "link", "base", "iframe", "object", "embed"]
    ):
        tag.decompose()
    for tag in soup.find_all(True):
        for name in list(tag.attrs):
            if name in {"style", "srcset", "background"} or name.lower().startswith(
                "on"
            ):
                del tag[name]
    title = str(metadata.get("title") or "").strip()
    if title and not has_leading_source_title(str(soup), title):
        heading = soup.new_tag("h1")
        heading.string = title
        (soup.body or soup).insert(0, heading)
    with tempfile.TemporaryDirectory(prefix="carlina-pdf-export-") as temporary:
        directory = Path(temporary)
        resources = {}
        svg_images = []
        for index, image in enumerate(soup.find_all("img")):
            source = str(image.get("src") or "")
            generated_svg = source in generated_svg_sources
            data = None if generated_svg else (images or {}).get(source)
            if data is None:
                match = (
                    re.fullmatch(
                        r"data:image/svg\+xml;base64,([A-Za-z0-9+/=]+)", source
                    )
                    if generated_svg
                    else _DATA_IMAGE.fullmatch(source)
                )
                if match is None:
                    raise ValueError("document-export-pdf-image-unavailable")
                try:
                    data = base64.b64decode(
                        re.sub(r"\s+", "", match.group(1)), validate=True
                    )
                except binascii.Error, ValueError:
                    raise ValueError("document-export-pdf-image-invalid") from None
            if not isinstance(data, bytes) or not 0 < len(data) <= _MAX_IMAGE_BYTES:
                raise ValueError("document-export-pdf-image-size")
            if generated_svg:
                data = validate_svg(data)
                if not 0 < len(data) <= _MAX_IMAGE_BYTES:
                    raise ValueError("document-export-pdf-image-size")
            name = f"image-{index}.bin"
            (directory / name).write_bytes(data)
            resources[name] = name
            if generated_svg:
                svg_images.append(name)
            image["src"] = name
        job = directory / "input.json"
        author_values = metadata.get("authors") or metadata.get("creators") or []
        if isinstance(author_values, str):
            author_values = [author_values]
        authors = []
        for author in author_values:
            if isinstance(author, str):
                authors.append(author)
            elif (
                isinstance(author, Mapping)
                and author.get("name")
                and author.get("role", "author") == "author"
            ):
                authors.append(str(author["name"]))
        job.write_text(
            json.dumps(
                {
                    "html": str(soup),
                    "metadata": {
                        "title": title,
                        "author": "; ".join(authors),
                        "subject": str(metadata.get("description") or ""),
                        "language": str(metadata.get("language") or ""),
                    },
                    "images": resources,
                    "svg_images": svg_images,
                    "start_h2_on_new_page": start_h2_on_new_page,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        output = directory / "document.pdf"
        try:
            result = run_managed_process(
                [
                    "python",
                    str(Path(__file__).with_name("pdf_renderer.py")),
                    "--input",
                    str(job),
                    "--output",
                    str(output),
                ],
                runtime_unit="document_processing",
                timeout_seconds=120,
                check=False,
            )
            if getattr(result, "returncode", None) != 0 or not output.is_file():
                raise RuntimeError("document-export-pdf-conversion-failed")
            data = output.read_bytes()
        except OSError, subprocess.SubprocessError:
            raise RuntimeError("document-export-pdf-conversion-failed") from None
        if not data.startswith(b"%PDF-"):
            raise RuntimeError("document-export-pdf-conversion-failed")
        return data
