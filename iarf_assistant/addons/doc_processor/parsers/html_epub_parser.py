from __future__ import annotations

import hashlib
import os
import posixpath
import re
import stat
import zipfile
from html import escape
from pathlib import Path
from urllib.parse import unquote, unquote_to_bytes, urlparse, urlsplit
from xml.etree.ElementTree import ParseError

from bs4 import BeautifulSoup

from carlina.core.semantic_html import SAFE_TOKEN_RE
from iarf_assistant.core.upload_paths import uploads_root

from ..archive_safety import (
    MAX_ARCHIVE_HTML_BYTES,
    ArchiveSafetyError,
    parse_safe_xml,
    read_zip_member,
    validate_zip_archive,
)

MAX_DOCX_IMAGE_BYTES = 25 * 1024 * 1024
_MALFORMED_ESCAPE_RE = re.compile(r"%(?![0-9a-fA-F]{2})")
_UNSAFE_ENCODED_PATH_CHARACTER_RE = re.compile(r"%(?:2f|5c|3f|23|25|3a)", re.IGNORECASE)


def _docx_image_member(source: str, archive_members: set[str]) -> str | None:
    """Return one exact package-local image member, without resolving paths."""
    try:
        parsed = urlsplit(source)
    except ValueError:
        return None
    if (
        parsed.scheme
        or parsed.netloc
        or "?" in source
        or "#" in source
        or not parsed.path
        or parsed.path.startswith("/")
        or "\\" in parsed.path
        or _MALFORMED_ESCAPE_RE.search(parsed.path)
        or _UNSAFE_ENCODED_PATH_CHARACTER_RE.search(parsed.path)
    ):
        return None
    try:
        decoded_path = unquote_to_bytes(parsed.path).decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return None
    components = decoded_path.split("/")
    if (
        decoded_path.startswith("/")
        or "\\" in decoded_path
        or any(component in {"", ".", ".."} for component in components)
    ):
        return None
    member = f"word/{decoded_path}"
    if member not in archive_members:
        raise ArchiveSafetyError("DOCX references an image missing from its package.")
    return member


def _docx_image_filename(member: str, data: bytes) -> str:
    suffix = Path(member).suffix.removeprefix(".")
    if not suffix or not re.fullmatch(r"[A-Za-z0-9]{1,10}", suffix):
        suffix = "bin"
    return f"docx_{hashlib.sha256(data).hexdigest()}.{suffix.lower()}"


def _existing_docx_image_matches(output_path: Path, data: bytes) -> bool:
    try:
        existing_stat = output_path.lstat()
    except FileNotFoundError:
        return False
    if (
        not stat.S_ISREG(existing_stat.st_mode)
        or stat.S_ISLNK(existing_stat.st_mode)
        or existing_stat.st_nlink != 1
    ):
        raise ArchiveSafetyError("DOCX image output is not an unlinked regular file.")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(output_path, flags)
    except OSError as exc:
        raise ArchiveSafetyError("DOCX image output cannot be read safely.") from exc
    with os.fdopen(descriptor, "rb") as handle:
        opened_stat = os.fstat(handle.fileno())
        if (
            not stat.S_ISREG(opened_stat.st_mode)
            or opened_stat.st_nlink != 1
            or (opened_stat.st_dev, opened_stat.st_ino)
            != (existing_stat.st_dev, existing_stat.st_ino)
        ):
            raise ArchiveSafetyError("DOCX image output changed during validation.")
        existing_data = handle.read(len(data) + 1)
    if existing_data != data:
        raise ArchiveSafetyError("DOCX image output conflicts with extracted bytes.")
    return True


def _docx_image_output(path: Path, member: str, data: bytes) -> Path:
    """Create one private image file or verify an exact prior extraction."""
    output_path = path.parent / _docx_image_filename(member, data)
    if _existing_docx_image_matches(output_path, data):
        return output_path

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(output_path, flags, 0o600)
    except FileExistsError:
        # A concurrent retry may have completed the same deterministic asset.
        if _existing_docx_image_matches(output_path, data):
            return output_path
        raise ArchiveSafetyError("DOCX image output changed during creation.")
    created_stat = os.fstat(descriptor)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
    except BaseException:
        try:
            current_stat = output_path.lstat()
            if (
                stat.S_ISREG(current_stat.st_mode)
                and not stat.S_ISLNK(current_stat.st_mode)
                and (current_stat.st_dev, current_stat.st_ino)
                == (created_stat.st_dev, created_stat.st_ino)
            ):
                output_path.unlink()
        except OSError:
            pass
        raise
    return output_path


def _materialize_docx_images(
    html: str, path: Path, archive: zipfile.ZipFile, archive_members: set[str]
) -> str:
    soup = BeautifulSoup(html or "", "html.parser")
    extracted_by_member: dict[str, str] = {}
    for image in soup.find_all("img"):
        source = image.get("src")
        if not isinstance(source, str):
            continue
        member = _docx_image_member(source, archive_members)
        if member is None:
            try:
                parsed = urlsplit(source)
            except ValueError:
                parsed = None
            if parsed is None or not (parsed.scheme or parsed.netloc):
                image.attrs.pop("src", None)
            continue
        if member not in extracted_by_member:
            data = read_zip_member(
                archive,
                member,
                max_bytes=MAX_DOCX_IMAGE_BYTES,
            )
            extracted_by_member[member] = _docx_image_output(path, member, data).name
        image["src"] = extracted_by_member[member]
    return str(soup)


def _normalize_xhtml_namespaces(soup: BeautifulSoup) -> None:
    """HTML's tolerant reader still needs XML's namespace aliases resolved."""
    scopes: dict[int, dict[str, str]] = {}
    for node in soup.find_all(True):
        namespaces = dict(scopes.get(id(node.parent), {}))
        namespaces.update(
            (name.removeprefix("xmlns:"), str(value))
            for name, value in node.attrs.items() if name.startswith("xmlns:")
        )
        scopes[id(node)] = namespaces
        prefix, separator, local = node.name.partition(":")
        if separator and namespaces.get(prefix) == "http://www.w3.org/1999/xhtml":
            node.name = local
        for name in list(node.attrs):
            prefix, separator, local = name.partition(":")
            if separator and namespaces.get(prefix) == "http://www.idpf.org/2007/ops":
                value = node.attrs.pop(name)
                node[f"epub:{local}"] = value


class HtmlEpubParser:
    def __init__(self, file_path: str, user_id: int | None, llm_service, settings: dict | None = None):
        self.file_path = file_path
        self.user_id = user_id
        self.llm_service = llm_service
        self.settings = settings or {}
        self.metrics = {}

    def _read_html_file(self, path: Path) -> str:
        return path.read_text(encoding="utf-8", errors="replace")

    def _read_epub(self, path: Path) -> str:
        container_ns = {"container": "urn:oasis:names:tc:opendocument:xmlns:container"}
        opf_ns = {"opf": "http://www.idpf.org/2007/opf"}
        with zipfile.ZipFile(path) as archive:
            archive_names = set(validate_zip_archive(archive))
            spine_members = []
            try:
                container = parse_safe_xml(read_zip_member(archive, "META-INF/container.xml"))
                rootfile = container.find(".//container:rootfile", container_ns)
                opf_path = rootfile.attrib.get("full-path") if rootfile is not None else None
                if not opf_path or opf_path not in archive_names:
                    raise ValueError("EPUB package document is missing.")
                package = parse_safe_xml(read_zip_member(archive, opf_path))
                if package.tag != f"{{{opf_ns['opf']}}}package" or package.get("version") not in {"2.0", "3.0"}:
                    raise ValueError("Unsupported EPUB package version or namespace.")
                manifest_items = package.findall("opf:manifest/opf:item", opf_ns)
                manifest = {
                    item.attrib.get("id"): item
                    for item in manifest_items
                    if item.attrib.get("id")
                }
                if len(manifest) != len(manifest_items):
                    raise ValueError("EPUB manifest identifiers are missing or duplicated.")
                for itemref in package.findall("opf:spine/opf:itemref", opf_ns):
                    item = manifest.get(itemref.attrib.get("idref"))
                    seen = set()
                    while item is not None and item.attrib.get("media-type") != "application/xhtml+xml":
                        fallback = item.attrib.get("fallback")
                        if not fallback or fallback in seen:
                            raise ValueError("EPUB spine content has no supported XHTML fallback.")
                        seen.add(fallback)
                        item = manifest.get(fallback)
                    if item is None:
                        raise ValueError("EPUB spine references an unknown manifest item.")
                    href = urlparse(item.attrib.get("href", ""))
                    if href.scheme or href.netloc or not href.path or href.path.startswith("/"):
                        raise ValueError("EPUB spine requires local content.")
                    member = posixpath.normpath(posixpath.join(posixpath.dirname(opf_path), unquote(href.path)))
                    if member not in archive_names:
                        raise ValueError("EPUB reading-order document is missing.")
                    if member in spine_members:
                        raise ValueError("EPUB reading order repeats a document.")
                    spine_members.append(member)
            except ArchiveSafetyError:
                raise
            except (AttributeError, KeyError, ParseError, ValueError) as exc:
                raise ValueError("EPUB package or reading order is invalid.") from exc

            if not spine_members:
                raise ValueError("EPUB contains no supported reading-order documents.")

            documents = []
            member_indexes = {}
            for index, member in enumerate(spine_members, start=1):
                try:
                    source = read_zip_member(
                        archive,
                        member,
                        max_bytes=MAX_ARCHIVE_HTML_BYTES,
                    )
                except KeyError as exc:
                    raise ValueError("EPUB reading-order document is missing.") from exc
                soup = BeautifulSoup(source, "html.parser")
                _normalize_xhtml_namespaces(soup)
                body = soup.body or soup
                # Navigation is structural evidence, not duplicate book content.
                for nav in body.find_all("nav"):
                    if set(str(nav.get("epub:type", "")).split()) & {"toc", "page-list", "landmarks"}:
                        nav.decompose()
                fragment = BeautifulSoup("".join(str(child) for child in body.children), "html.parser")
                wrapper = fragment.new_tag("section", id=f"epub-document-{index}")
                wrapper["data-epub-source"] = member
                for attribute in ("lang", "xml:lang", "dir", "epub:type", "role"):
                    value = body.get(attribute) or (soup.html.get(attribute) if soup.html else None)
                    if value:
                        wrapper[attribute] = value
                if body.get("id"):
                    wrapper["data-epub-body-id"] = body["id"]
                for child in list(fragment.contents):
                    wrapper.append(child.extract())
                fragment.append(wrapper)
                member_indexes[member] = index
                documents.append((member, index, fragment))

            id_map = {}
            for member, index, fragment in documents:
                wrapper = fragment.section
                body_id = wrapper.attrs.pop("data-epub-body-id", None)
                if body_id:
                    id_map[(member, body_id)] = wrapper["id"]
                for node in fragment.find_all(id=True):
                    if node is wrapper:
                        continue
                    old_id = str(node.get("id") or "")
                    if (member, old_id) in id_map:
                        raise ValueError("EPUB content contains duplicate identifiers.")
                    new_id = f"epub-{index}-{old_id}"
                    if not SAFE_TOKEN_RE.fullmatch(new_id):
                        new_id = f"epub-{index}-id-{hashlib.sha256(old_id.encode()).hexdigest()}"
                    id_map[(member, old_id)] = new_id
                    node["id"] = new_id

            extracted_by_member = {}
            for member, index, fragment in documents:
                for image in fragment.find_all("img"):
                    source = str(image.get("src") or "").strip()
                    parsed = urlparse(source)
                    if not source or parsed.scheme or source.startswith("//"):
                        continue
                    resource_member = posixpath.normpath(
                        posixpath.join(posixpath.dirname(member), unquote(parsed.path))
                    )
                    if resource_member not in archive_names:
                        continue
                    if resource_member not in extracted_by_member:
                        data = read_zip_member(archive, resource_member)
                        digest = hashlib.sha256(data).hexdigest()[:12]
                        filename = f"epub_{digest}_{Path(resource_member).name}"
                        output_path = path.parent / filename
                        output_path.write_bytes(data)
                        try:
                            uploads_relative = output_path.resolve().relative_to(
                                uploads_root()
                            )
                            extracted_by_member[resource_member] = "/uploads/" + uploads_relative.as_posix()
                        except (RuntimeError, ValueError):
                            extracted_by_member[resource_member] = filename
                    image["src"] = extracted_by_member[resource_member]

                for anchor in fragment.find_all("a", href=True):
                    href = str(anchor.get("href") or "")
                    parsed = urlparse(href)
                    if parsed.scheme or href.startswith("//"):
                        continue
                    target_member = member
                    if parsed.path:
                        target_member = posixpath.normpath(
                            posixpath.join(posixpath.dirname(member), unquote(parsed.path))
                        )
                    target_id = unquote(parsed.fragment)
                    if target_id and (target_member, target_id) in id_map:
                        anchor["href"] = "#" + id_map[(target_member, target_id)]
                    elif target_id:
                        raise ValueError("EPUB local link target is missing or unsupported.")
                    elif target_member in member_indexes:
                        anchor["href"] = f"#epub-document-{member_indexes[target_member]}"

            fragments = []
            for member, index, fragment in documents:
                fragments.append(str(fragment))
            return "\n".join(fragments)

    def _pandoc_convert(self, path: Path) -> str:
        try:
            import pypandoc
        except ImportError as exc:
            raise RuntimeError("Pandoc support is required for this document type.") from exc

        if path.suffix.lower() == ".docx":
            try:
                with zipfile.ZipFile(path) as archive:
                    archive_members = set(validate_zip_archive(archive))
                    html = pypandoc.convert_file(
                        str(path),
                        to="html",
                        format=None,
                        extra_args=["--sandbox"],
                    )
                    return _materialize_docx_images(
                        html,
                        path,
                        archive,
                        archive_members,
                    )
            except zipfile.BadZipFile as exc:
                raise ArchiveSafetyError(
                    "DOCX package is not a valid ZIP archive."
                ) from exc

        return pypandoc.convert_file(str(path), to="html", format=None)

    def _text_fallback(self, path: Path) -> str:
        text = path.read_text(encoding="utf-8", errors="replace")
        paragraphs = [line.strip() for line in text.splitlines() if line.strip()]
        return "\n".join(f"<p>{escape(line)}</p>" for line in paragraphs) or "<p></p>"

    def _normalize_html(self, html: str) -> str:
        soup = BeautifulSoup(html or "", "html.parser")
        body = soup.body
        if body is not None:
            return "".join(str(child) for child in body.children).strip()
        return str(soup).strip()

    def process(self):
        path = Path(self.file_path)
        suffix = path.suffix.lower()
        if suffix in {".html", ".htm"}:
            html = self._read_html_file(path)
        elif suffix == ".epub":
            html = self._read_epub(path)
        elif suffix in {".docx", ".doc", ".odt", ".rtf"}:
            html = self._pandoc_convert(path)
        else:
            html = self._text_fallback(path)
        return "COMPLETED", self._normalize_html(html)
