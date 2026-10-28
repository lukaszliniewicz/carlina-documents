"""Secure FictionBook 2 (FB2) extraction to inert semantic HTML.

FB2 is XML, but the files encountered in the wild are not necessarily small,
well formed, or honest.  This parser therefore keeps the XML and resource
boundaries in one place and emits only the semantic HTML vocabulary accepted by
the document processor.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import html
import lzma
import re
import zipfile
import zlib
from collections.abc import Sequence
from io import BytesIO
from pathlib import Path
from urllib.parse import unquote, urlsplit
from xml.etree.ElementTree import Element, ParseError

from defusedxml import ElementTree as SafeElementTree
from defusedxml.common import DefusedXmlException

from iarf_assistant.core.upload_paths import uploads_root

from ..archive_safety import ArchiveSafetyError, read_zip_member, validate_zip_archive

FB2_NAMESPACE = "http://www.gribuser.ru/xml/fictionbook/2.0"
MAX_FB2_BYTES = 64 * 1024 * 1024
MAX_FB2_IMAGE_BYTES = 48 * 1024 * 1024
MAX_FB2_IMAGE_PIXELS = 40_000_000
MAX_FB2_DEPTH = 128
MAX_FB2_ELEMENTS = 200_000
_ALLOWED_IMAGE_TYPES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
}
_LANGUAGE_RE = re.compile(r"^[A-Za-z]{2,8}(?:[-_][A-Za-z0-9]{1,8})*$")
_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9_.:-]+")
_FORBIDDEN_ELEMENTS = {"binary", "description", "document-info", "script", "stylesheet"}


def _local_name(tag: object) -> str:
    text = str(tag or "")
    return text.rsplit("}", 1)[-1].casefold()


def _namespace(tag: object) -> str | None:
    text = str(tag or "")
    if text.startswith("{") and "}" in text:
        return text[1:].split("}", 1)[0]
    return None


def _attribute(element: Element, name: str) -> str | None:
    for raw_name, value in element.attrib.items():
        if raw_name == name or _local_name(raw_name) == name.casefold():
            return str(value)
    return None


def _children(element: Element | None, name: str) -> list[Element]:
    if element is None:
        return []
    return [child for child in list(element) if _local_name(child.tag) == name]


def _first_child(element: Element | None, name: str) -> Element | None:
    if element is None:
        return None
    return next((child for child in list(element) if _local_name(child.tag) == name), None)


def _text_content(element: Element | None) -> str:
    if element is None:
        return ""
    return re.sub(r"\s+", " ", "".join(element.itertext())).strip()


def _escape_text(value: str | None) -> str:
    return html.escape(value or "", quote=False)


def _attrs(attributes: Sequence[tuple[str, str | None]]) -> str:
    rendered = []
    for name, value in attributes:
        if value is None:
            continue
        rendered.append(f' {name}="{html.escape(str(value), quote=True)}"')
    return "".join(rendered)


def _normalized_language(value: str | None) -> str | None:
    candidate = (value or "").strip()
    if not candidate or not _LANGUAGE_RE.fullmatch(candidate):
        return None
    parts = candidate.replace("_", "-").split("-")
    normalized = [parts[0].lower()]
    for part in parts[1:]:
        normalized.append(part.upper() if len(part) == 2 and part.isalpha() else part)
    return "-".join(normalized)


def _read_fb2_root(path: Path, *, require_body: bool) -> Element:
    """Read and validate one bounded FB2 XML document."""

    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ValueError(f"Unable to inspect FB2 source: {path}") from exc
    if size > MAX_FB2_BYTES:
        raise ValueError("FB2 input exceeds the 64 MiB limit.")

    try:
        with path.open("rb") as handle:
            data = handle.read(MAX_FB2_BYTES + 1)
    except OSError as exc:
        raise ValueError(f"Unable to read FB2 source: {path}") from exc
    if len(data) > MAX_FB2_BYTES:
        raise ValueError("FB2 input exceeds the 64 MiB limit.")

    if data.startswith(b"PK"):
        try:
            with zipfile.ZipFile(BytesIO(data)) as archive:
                members = validate_zip_archive(archive)
                payloads = [
                    (name, info)
                    for name, info in members.items()
                    if not info.is_dir()
                ]
                if len(payloads) != 1 or not payloads[0][0].casefold().endswith(".fb2"):
                    raise ValueError("FB2 ZIP must contain exactly one non-directory .fb2 member.")
                data = read_zip_member(
                    archive,
                    payloads[0][0],
                    max_bytes=MAX_FB2_BYTES,
                )
        except ArchiveSafetyError:
            raise
        except (
            zipfile.BadZipFile,
            EOFError,
            NotImplementedError,
            OSError,
            lzma.LZMAError,
            zlib.error,
        ) as exc:
            raise ValueError("Invalid FB2 ZIP.") from exc

    try:
        parser = SafeElementTree.iterparse(
            BytesIO(data), events=("start", "end"),
            forbid_dtd=True, forbid_entities=True, forbid_external=True,
        )
        depth = -1
        count = 0
        root: Element | None = None
        for event, element in parser:
            if event == "start":
                if root is None:
                    root = element
                depth += 1
                count += 1
                if depth > MAX_FB2_DEPTH:
                    raise ValueError("FB2 XML exceeds the maximum tree depth of 128.")
                if count > MAX_FB2_ELEMENTS:
                    raise ValueError("FB2 XML exceeds the maximum element count.")
            else:
                depth -= 1
    except DefusedXmlException as exc:
        raise ValueError("FB2 XML contains a prohibited DTD, entity, or external reference.") from exc
    except (ParseError, UnicodeError) as exc:
        raise ValueError("Invalid FB2 XML.") from exc

    if root is None or _local_name(root.tag) != "fictionbook":
        raise ValueError("Invalid FB2 root: expected FictionBook.")
    if _namespace(root.tag) not in {None, FB2_NAMESPACE}:
        raise ValueError("Invalid FB2 root namespace.")

    seen_ids: set[str] = set()
    for element in root.iter():
        identifier = _attribute(element, "id")
        if identifier is None:
            continue
        identifier = identifier.strip()
        if not identifier:
            continue
        if identifier in seen_ids:
            raise ValueError("FB2 contains duplicate source ID.")
        seen_ids.add(identifier)

    direct_bodies = [
        child
        for child in list(root)
        if _local_name(child.tag) == "body"
        and _namespace(child.tag) in {None, FB2_NAMESPACE}
    ]
    if require_body and not direct_bodies:
        raise ValueError("FB2 document has no body.")
    return root


def _person_name(person: Element) -> str:
    parts = [
        _text_content(_first_child(person, field))
        for field in ("first-name", "middle-name", "last-name")
    ]
    parts = [part for part in parts if part]
    if parts:
        return " ".join(parts)
    nickname = _text_content(_first_child(person, "nickname"))
    return nickname or _text_content(person)


def _split_keywords(value: str) -> list[str]:
    values = []
    for part in re.split(r"[,;|\r\n]+", value):
        text = re.sub(r"\s+", " ", part).strip()
        if text and text.casefold() not in {item.casefold() for item in values}:
            values.append(text)
    return values


def extract_fb2_metadata(path: Path) -> dict:
    """Extract title-info and publish-info metadata from an FB2 source.

    ``document-info`` is intentionally consulted only for the document ID;
    its author is provenance for the file and is not the book's author.
    """

    root = _read_fb2_root(Path(path), require_body=False)
    description = _first_child(root, "description")
    title_info = _first_child(description, "title-info")
    publish_info = _first_child(description, "publish-info")
    metadata: dict[str, object] = {}

    title = _text_content(_first_child(title_info, "book-title"))
    if title:
        metadata["title"] = title
    subtitle = _text_content(_first_child(title_info, "subtitle"))
    if subtitle:
        metadata["subtitle"] = subtitle

    language = _normalized_language(_text_content(_first_child(title_info, "lang")))
    if language:
        metadata["language"] = language

    contributors: dict[str, list[str]] = {"authors": [], "translators": []}
    for author in _children(title_info, "author"):
        name = _person_name(author)
        if name:
            contributors["authors"].append(name)
    for translator in _children(title_info, "translator"):
        name = _person_name(translator)
        if name:
            contributors["translators"].append(name)
    if any(contributors.values()):
        contributors["editors"] = []
        contributors["other"] = []
        metadata["contributors"] = contributors

    subjects: list[str] = []
    for genre in _children(title_info, "genre"):
        value = _text_content(genre)
        if value:
            subjects.extend(_split_keywords(value))
    keywords = _text_content(_first_child(title_info, "keywords"))
    if keywords:
        subjects.extend(_split_keywords(keywords))
    deduped_subjects = []
    for subject in subjects:
        if subject.casefold() not in {item.casefold() for item in deduped_subjects}:
            deduped_subjects.append(subject)
    if deduped_subjects:
        metadata["subjects"] = deduped_subjects

    annotation = _text_content(_first_child(title_info, "annotation"))
    if annotation:
        metadata["description"] = annotation

    publisher = _text_content(_first_child(publish_info, "publisher"))
    if publisher:
        metadata["publisher"] = publisher
    year = _first_child(publish_info, "year")
    year_value = (_attribute(year, "value") or _text_content(year)).strip() if year is not None else ""
    if year_value:
        metadata["publication_date"] = year_value
    isbn = _text_content(_first_child(publish_info, "isbn"))
    if isbn:
        metadata["isbn"] = isbn

    sequence = _first_child(title_info, "sequence")
    series = _attribute(sequence, "name") if sequence is not None else None
    volume = _attribute(sequence, "number") if sequence is not None else None
    if series and series.strip():
        metadata["journal_or_series"] = series.strip()
    if volume and volume.strip():
        metadata["volume"] = volume.strip()

    document_info = _first_child(description, "document-info")
    identifiers = []
    root_id = (_attribute(root, "id") or "").strip()
    document_id = _text_content(_first_child(document_info, "id"))
    for identifier in (root_id, document_id):
        if identifier and identifier.casefold() not in {
            str(item["value"]).casefold() for item in identifiers
        }:
            identifiers.append({"value": identifier, "scheme": "FB2"})
    if identifiers:
        metadata["identifiers"] = identifiers

    return metadata


class _Fb2Renderer:
    def __init__(self, root: Element, source_path: Path, settings: dict | None = None):
        self.root = root
        self.source_path = source_path.resolve()
        self.settings = settings or {}
        self.id_map: dict[str, str] = {}
        self.body_by_id: dict[str, str | None] = {}
        self.binary_by_id: dict[str, Element] = {}
        self.note_target_ids: set[str] = set()
        self.image_paths: dict[str, str] = {}
        self._index_source()

    def _index_source(self) -> None:
        source_ids = []
        stack: list[tuple[Element, str | None]] = [(self.root, None)]
        while stack:
            element, inherited_body = stack.pop()
            name = _local_name(element.tag)
            body_kind = inherited_body
            if name == "body":
                body_name = (_attribute(element, "name") or "").casefold()
                body_kind = "notes" if body_name in {"notes", "footnotes", "endnotes"} else "main"
            identifier = (_attribute(element, "id") or "").strip()
            if identifier:
                source_ids.append(identifier)
                self.body_by_id[identifier] = body_kind
            if name == "binary" and identifier:
                self.binary_by_id[identifier] = element
            for child in reversed(list(element)):
                stack.append((child, body_kind))

        for identifier in source_ids:
            slug = _SAFE_ID_RE.sub("-", identifier.casefold()).strip("-._:") or "id"
            digest = hashlib.sha256(identifier.encode("utf-8")).hexdigest()[:12]
            self.id_map[identifier] = f"fb2-src-{slug[:72]}-{digest}"

        for element in self.root.iter():
            if _local_name(element.tag) != "a":
                continue
            href = (_attribute(element, "href") or "").strip()
            if href.startswith("#"):
                target = unquote(href[1:]).strip()
                if self.body_by_id.get(target) == "notes":
                    self.note_target_ids.add(target)

    def _source_id(self, element: Element) -> str | None:
        identifier = (_attribute(element, "id") or "").strip()
        return self.id_map.get(identifier)

    def _base_attributes(self, element: Element) -> list[tuple[str, str | None]]:
        attributes = [("id", self._source_id(element))]
        language = _normalized_language(_attribute(element, "lang"))
        if language:
            attributes.extend([("lang", language), ("xml:lang", language)])
        return attributes

    def _generated_id(self, stem: str, index: int) -> str:
        candidate = f"fb2-{stem}" if index == 1 else f"fb2-{stem}-{index}"
        if candidate not in self.id_map.values():
            return candidate
        suffix = hashlib.sha256(candidate.encode("utf-8")).hexdigest()[:8]
        return f"{candidate}-{suffix}"

    def _body_attributes(self, body: Element, index: int) -> str:
        body_name = (_attribute(body, "name") or "").casefold()
        notes = body_name in {"notes", "footnotes", "endnotes"}
        identifier = self._source_id(body) or self._generated_id("body-notes" if notes else "body-main", index)
        description = _first_child(self.root, "description")
        title_info = _first_child(description, "title-info")
        language = _normalized_language(
            _attribute(body, "lang")
            or _attribute(body, "xml:lang")
            or _attribute(self.root, "xml:lang")
        )
        language = language or _normalized_language(_text_content(_first_child(title_info, "lang")))
        attributes = [("id", identifier)]
        if language:
            attributes.extend([("lang", language), ("xml:lang", language)])
        if notes:
            attributes.extend([("role", "doc-endnotes"), ("epub:type", "endnotes")])
        return _attrs(attributes)

    def _render_title(self, element: Element, level: int, *, subtitle: bool = False) -> str:
        level = min(6, max(1, level))
        content: list[str] = [_escape_text(element.text)]
        paragraph_seen = False
        for child in list(element):
            if _local_name(child.tag) == "p":
                if paragraph_seen:
                    content.append("<br/>")
                paragraph = self._render_inline_children(child)
                if self._source_id(child) or _attribute(child, "lang"):
                    paragraph = f'<span{_attrs(self._base_attributes(child))}>{paragraph}</span>'
                content.append(paragraph)
                paragraph_seen = True
            else:
                content.append(self._render_inline(child))
            content.append(_escape_text(child.tail))
        attributes = self._base_attributes(element)
        if subtitle:
            attributes.append(("class", "subtitle"))
        return f"<h{level}{_attrs(attributes)}>{''.join(content)}</h{level}>"

    def _render_inline_children(self, element: Element) -> str:
        rendered = [_escape_text(element.text)]
        for child in list(element):
            rendered.append(self._render_inline(child))
            rendered.append(_escape_text(child.tail))
        return "".join(rendered)

    def _render_anchor(self, element: Element) -> str:
        raw_href = (_attribute(element, "href") or "").strip()
        href = None
        note_ref = False
        if raw_href.startswith("#"):
            target = unquote(raw_href[1:]).strip()
            mapped_target = self.id_map.get(target)
            if mapped_target:
                href = f"#{mapped_target}"
                note_ref = self.body_by_id.get(target) == "notes"
            else:
                raise ValueError("FB2 local link target is missing.")
        else:
            parsed = urlsplit(raw_href)
            scheme = parsed.scheme.casefold()
            if scheme in {"http", "https"} and parsed.netloc:
                href = raw_href
            elif scheme == "mailto" and parsed.path:
                href = raw_href
        attributes = [*self._base_attributes(element), ("href", href)]
        if note_ref:
            attributes.extend([("role", "doc-noteref"), ("epub:type", "noteref")])
        return f"<a{_attrs(attributes)}>{self._render_inline_children(element)}</a>"

    def _image_source(self, element: Element) -> str:
        raw_href = (_attribute(element, "href") or "").strip()
        if not raw_href.startswith("#"):
            raise ValueError("FB2 image reference must point to an embedded binary.")
        binary_id = unquote(raw_href[1:]).strip()
        binary = self.binary_by_id.get(binary_id)
        if binary is None:
            raise ValueError("FB2 image references missing binary.")
        if binary_id in self.image_paths:
            return self.image_paths[binary_id]

        content_type = (_attribute(binary, "content-type") or "").split(";", 1)[0].strip().casefold()
        suffix = _ALLOWED_IMAGE_TYPES.get(content_type)
        if suffix is None:
            raise ValueError("Unsupported FB2 image MIME type.")
        encoded = re.sub(r"\s+", "", "".join(binary.itertext()))
        if not encoded:
            raise ValueError("FB2 image binary is empty.")
        try:
            decoded = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("FB2 image binary is not valid base64.") from exc
        if len(decoded) > MAX_FB2_IMAGE_BYTES:
            raise ValueError("FB2 image binary exceeds the image size limit.")
        try:
            from PIL import Image

            with Image.open(BytesIO(decoded)) as image:
                if image.width * image.height > MAX_FB2_IMAGE_PIXELS:
                    raise ValueError("FB2 image exceeds the pixel limit.")
                if Image.MIME.get(image.format or "") != content_type:
                    raise ValueError("FB2 image MIME type does not match its data.")
                image.verify()
        except Exception as exc:
            raise ValueError("FB2 image binary is not a valid raster image.") from exc

        digest = hashlib.sha256(decoded).hexdigest()
        output = self.source_path.parent / f"fb2_{digest}{suffix}"
        output.write_bytes(decoded)
        try:
            root = uploads_root(uploads_dir=self.settings.get("uploads_dir"))
            relative = output.resolve().relative_to(root.resolve())
        except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
            rendered_source = output.name
        else:
            rendered_source = f"/uploads/{relative.as_posix()}"
        self.image_paths[binary_id] = rendered_source
        return rendered_source

    def _render_image(self, element: Element) -> str:
        source = self._image_source(element)
        attributes = [*self._base_attributes(element), ("src", source)]
        alt = (_attribute(element, "alt") or "").strip()
        if alt:
            attributes.append(("alt", alt))
        return f"<img{_attrs(attributes)}/>"

    def _render_inline(self, element: Element) -> str:
        name = _local_name(element.tag)
        if name in _FORBIDDEN_ELEMENTS:
            return ""
        if name == "a":
            return self._render_anchor(element)
        if name == "image":
            return self._render_image(element)
        if name in {"br", "line-break"}:
            return "<br/>"
        if name == "empty-line":
            return "<br/>"

        wrappers = {
            "emphasis": "em",
            "strong": "strong",
            "strikethrough": "s",
            "sub": "sub",
            "sup": "sup",
            "code": "code",
            "cite": "cite",
        }
        if name == "style":
            style_name = (_attribute(element, "name") or "").casefold()
            wrappers_for_style = {
                "emphasis": "em",
                "strong": "strong",
                "strikethrough": "s",
                "sub": "sub",
                "sup": "sup",
                "code": "code",
            }
            tag = wrappers_for_style.get(style_name)
            if tag is None:
                return self._render_unknown_inline(element)
        else:
            tag = wrappers.get(name)
        if tag:
            attributes = self._base_attributes(element)
            return f"<{tag}{_attrs(attributes)}>{self._render_inline_children(element)}</{tag}>"
        return self._render_unknown_inline(element)

    def _render_unknown_inline(self, element: Element) -> str:
        content = _escape_text(element.text) + "".join(
            self._render_inline(child) + _escape_text(child.tail) for child in list(element)
        )
        identifier = self._source_id(element)
        if identifier or _attribute(element, "lang"):
            return f'<span{_attrs(self._base_attributes(element))}>{content}</span>'
        return content

    def _cell_attributes(self, element: Element) -> str:
        attributes = self._base_attributes(element)
        for name in ("rowspan", "colspan"):
            value = (_attribute(element, name) or "").strip()
            if re.fullmatch(r"[1-9][0-9]{0,5}", value):
                attributes.append((name, value))
        return _attrs(attributes)

    def _render_cell_content(self, element: Element, heading_level: int) -> str:
        rendered = [_escape_text(element.text)]
        for child in list(element):
            if _local_name(child.tag) in {"p", "empty-line", "table", "poem", "epigraph"}:
                rendered.append(self._render_block(child, heading_level))
            else:
                rendered.append(self._render_inline(child))
            rendered.append(_escape_text(child.tail))
        return "".join(rendered)

    def _render_block(self, element: Element, heading_level: int) -> str:
        name = _local_name(element.tag)
        if name in _FORBIDDEN_ELEMENTS:
            return ""
        if name == "title":
            return self._render_title(element, heading_level)
        if name == "subtitle":
            return self._render_title(element, heading_level + 1, subtitle=True)
        if name == "p":
            attributes = self._base_attributes(element)
            if (_attribute(element, "id") or "").strip() in self.note_target_ids:
                attributes.extend([("role", "doc-footnote"), ("epub:type", "footnote")])
            return f'<p{_attrs(attributes)}>{self._render_inline_children(element)}</p>'
        if name == "empty-line":
            return f'<p{_attrs(self._base_attributes(element))}><br/></p>'
        if name == "image":
            return self._render_image(element)
        if name == "epigraph":
            content = [_escape_text(element.text)]
            for child in list(element):
                child_name = _local_name(child.tag)
                if child_name == "text-author":
                    content.append(
                        f'<cite{_attrs([("id", self._source_id(child))])}>{self._render_inline_children(child)}</cite>'
                    )
                else:
                    content.append(self._render_block(child, heading_level))
                content.append(_escape_text(child.tail))
            return f'<blockquote{_attrs(self._base_attributes(element))}>{"".join(content)}</blockquote>'
        if name == "cite":
            return f'<blockquote{_attrs(self._base_attributes(element))}>{self._render_block_children(element, heading_level)}</blockquote>'
        if name == "poem":
            return f'<div class="poem"{_attrs(self._base_attributes(element))}>{self._render_block_children(element, heading_level)}</div>'
        if name == "stanza":
            return f'<div class="stanza"{_attrs(self._base_attributes(element))}>{self._render_block_children(element, heading_level)}</div>'
        if name in {"v", "text-author"}:
            class_name = "text-author" if name == "text-author" else None
            return f'<p{_attrs([*self._base_attributes(element), ("class", class_name)])}>{self._render_inline_children(element)}</p>'
        if name == "table":
            return f'<table{_attrs(self._base_attributes(element))}>{self._render_block_children(element, heading_level)}</table>'
        if name == "tr":
            return f'<tr{_attrs(self._base_attributes(element))}>{self._render_block_children(element, heading_level)}</tr>'
        if name in {"th", "td"}:
            tag = name
            return f'<{tag}{self._cell_attributes(element)}>{self._render_cell_content(element, heading_level)}</{tag}>'
        if name == "section":
            return self._render_section(element, heading_level, notes=False)
        return self._render_unknown_block(element, heading_level)

    def _render_block_children(self, element: Element, heading_level: int) -> str:
        rendered = [_escape_text(element.text)]
        for child in list(element):
            rendered.append(self._render_block(child, heading_level))
            rendered.append(_escape_text(child.tail))
        return "".join(rendered)

    def _render_unknown_block(self, element: Element, heading_level: int) -> str:
        content = self._render_block_children(element, heading_level)
        identifier = self._source_id(element)
        if identifier or _attribute(element, "lang"):
            attributes = self._base_attributes(element)
            if (_attribute(element, "id") or "").strip() in self.note_target_ids:
                attributes.extend([("role", "doc-footnote"), ("epub:type", "footnote")])
            return f'<div{_attrs(attributes)}>{content}</div>'
        return content

    def _render_section(self, element: Element, heading_level: int, *, notes: bool) -> str:
        attributes = self._base_attributes(element)
        if notes:
            attributes.extend([("role", "doc-footnote"), ("epub:type", "footnote")])
        content = [_escape_text(element.text)]
        for child in list(element):
            name = _local_name(child.tag)
            if name == "section":
                content.append(self._render_section(child, min(6, heading_level + 1), notes=notes))
            else:
                content.append(self._render_block(child, heading_level))
            content.append(_escape_text(child.tail))
        tag = "section"
        return f"<{tag}{_attrs(attributes)}>{''.join(content)}</{tag}>"

    def render(self) -> str:
        bodies = [
            child
            for child in list(self.root)
            if _local_name(child.tag) == "body"
            and _namespace(child.tag) in {None, FB2_NAMESPACE}
        ]
        fragments = []
        description = _first_child(self.root, "description")
        coverpage = _first_child(_first_child(description, "title-info"), "coverpage")
        body_images = {
            _attribute(node, "href")
            for body in bodies for node in body.iter() if _local_name(node.tag) == "image"
        }
        for cover in _children(coverpage, "image"):
            if _attribute(cover, "href") not in body_images:
                fragments.append(f'<figure class="cover">{self._render_image(cover)}</figure>')
        body_counts = {"main": 0, "notes": 0}
        for body in bodies:
            body_name = (_attribute(body, "name") or "").casefold()
            notes = body_name in {"notes", "footnotes", "endnotes"}
            kind = "notes" if notes else "main"
            body_counts[kind] += 1
            index = body_counts[kind]
            tag = "section" if notes else "main"
            body_attributes = self._body_attributes(body, index)
            body_has_title = any(_local_name(child.tag) == "title" for child in list(body))
            base_level = 2 if body_has_title else 1
            content = [_escape_text(body.text)]
            for child in list(body):
                name = _local_name(child.tag)
                if name == "title":
                    content.append(self._render_title(child, 1))
                elif name == "subtitle":
                    content.append(self._render_title(child, 2, subtitle=True))
                elif name == "section":
                    content.append(self._render_section(child, base_level, notes=notes))
                else:
                    content.append(self._render_block(child, base_level))
                content.append(_escape_text(child.tail))
            fragments.append(f"<{tag}{body_attributes}>{''.join(content)}</{tag}>")
        return "".join(fragments)


class Fb2Parser:
    def __init__(self, file_path: str, user_id: int | None, llm_service, settings: dict | None = None):
        self.file_path = file_path
        self.user_id = user_id
        self.llm_service = llm_service
        self.settings = settings or {}
        self.metrics = {}

    def process(self):
        path = Path(self.file_path)
        root = _read_fb2_root(path, require_body=True)
        return "COMPLETED", _Fb2Renderer(root, path, self.settings).render()
