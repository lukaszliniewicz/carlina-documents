from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from iarf_assistant.addons.doc_processor.archive_safety import (
    ArchiveSafetyError,
    parse_safe_xml,
    read_zip_member,
    validate_zip_archive,
)
from iarf_assistant.addons.doc_processor.parsers.html_epub_parser import HtmlEpubParser


def _archive_bytes(members: dict[str, bytes | str]) -> io.BytesIO:
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    payload.seek(0)
    return payload


def test_archive_validation_allows_bounded_members_and_reads_them():
    payload = _archive_bytes({"META-INF/container.xml": "<container/>", "text/chapter.xhtml": "<p>Safe</p>"})

    with zipfile.ZipFile(payload) as archive:
        members = validate_zip_archive(archive)
        assert set(members) == {"META-INF/container.xml", "text/chapter.xhtml"}
        assert read_zip_member(archive, "text/chapter.xhtml", max_bytes=64) == b"<p>Safe</p>"


@pytest.mark.parametrize("member_name", ["../escape.txt", "/absolute.txt", "C:/drive.txt"])
def test_archive_validation_rejects_unsafe_member_paths(member_name):
    payload = _archive_bytes({member_name: "unsafe"})

    with zipfile.ZipFile(payload) as archive, pytest.raises(ArchiveSafetyError):
        validate_zip_archive(archive)


def test_archive_validation_rejects_high_compression_ratio():
    payload = _archive_bytes({"highly-compressible.bin": b"\0" * (1024 * 1024)})

    with zipfile.ZipFile(payload) as archive, pytest.raises(
        ArchiveSafetyError,
        match="compression ratio",
    ):
        validate_zip_archive(archive)


def test_archive_member_read_has_an_independent_size_limit():
    payload = _archive_bytes({"content.xml": b"x" * 128})

    with zipfile.ZipFile(payload) as archive:
        validate_zip_archive(archive)
        with pytest.raises(ArchiveSafetyError, match="read size"):
            read_zip_member(archive, "content.xml", max_bytes=64)


def test_xml_parser_rejects_doctype_and_external_entities():
    malicious_xml = b"""<!DOCTYPE root [
        <!ENTITY xxe SYSTEM "file:///etc/passwd">
    ]><root>&xxe;</root>"""

    with pytest.raises(ArchiveSafetyError, match="prohibited declaration"):
        parse_safe_xml(malicious_xml)


def test_epub_parser_applies_archive_validation_before_package_parsing(tmp_path: Path):
    epub_path = tmp_path / "unsafe.epub"
    with zipfile.ZipFile(epub_path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("../escape.xhtml", "<p>unsafe</p>")
        archive.writestr("META-INF/container.xml", "<container/>")

    parser = HtmlEpubParser(str(epub_path), 7, None)
    with pytest.raises(ArchiveSafetyError):
        parser._read_epub(epub_path)


def test_epub_parser_rejects_entity_expansion_in_container(tmp_path: Path):
    epub_path = tmp_path / "entity.epub"
    container = """<!DOCTYPE container [
        <!ENTITY xxe SYSTEM "file:///etc/passwd">
    ]><container>&xxe;</container>"""
    with zipfile.ZipFile(epub_path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("META-INF/container.xml", container)

    parser = HtmlEpubParser(str(epub_path), 7, None)
    with pytest.raises(ArchiveSafetyError):
        parser._read_epub(epub_path)
