"""EPUB 2/3 package-driven ingestion, independent of ZIP member order."""

import zipfile

import pytest
from bs4 import BeautifulSoup

from iarf_assistant.addons.doc_processor.metadata_service import extract_source_document_metadata
from iarf_assistant.addons.doc_processor.parsers.html_epub_parser import HtmlEpubParser


def write_epub(path, *, version="3.0", spine=None, chapter=None, package_encoding="utf-8"):
    metadata = (
        '<dc:creator opf:role="aut">Author</dc:creator>'
        '<dc:creator opf:role="trl">Translator</dc:creator>'
        if version == "2.0" else
        '<dc:creator id="author">Author</dc:creator>'
        '<dc:contributor id="translator">Translator</dc:contributor>'
        '<meta refines="#translator" property="role" scheme="marc:relators">trl</meta>'
    )
    package = f'''<?xml version="1.0" encoding="{package_encoding}"?>
    <package xmlns="http://www.idpf.org/2007/opf" xmlns:opf="http://www.idpf.org/2007/opf" version="{version}" unique-identifier="book">
    <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
      <dc:identifier id="book" opf:scheme="ISBN">9780101010101</dc:identifier>
      <dc:title>Żółw</dc:title><dc:language>pl</dc:language>{metadata}
    </metadata><manifest>
      <item id="chapter" href="text/z%20chapter.xhtml" media-type="application/xhtml+xml"/>
      <item id="notes" href="text/a-notes.xhtml" media-type="application/xhtml+xml"/>
      <item id="nav" href="navigation.xhtml" media-type="application/xhtml+xml" properties="nav"/>
      <item id="foreign" href="unsupported.bin" media-type="application/x-unknown" fallback="chapter"/>
    </manifest><spine>{spine or '<itemref idref="chapter"/><itemref idref="notes" linear="no"/>'}</spine></package>'''
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("META-INF/container.xml", '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OPS/package.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
        archive.writestr("OPS/package.opf", package.encode(package_encoding))
        archive.writestr("OPS/navigation.xhtml", '<html><body><nav epub:type="toc"><ol><li><a href="text/z%20chapter.xhtml">Duplicate TOC</a></li></ol></nav></body></html>')
        archive.writestr("OPS/text/a-notes.xhtml", '<html><body epub:type="endnotes"><aside id="note" role="doc-footnote" epub:type="footnote"><p>Note text <a href="z%20chapter.xhtml#r%C3%A9f">Back</a></p></aside></body></html>')
        archive.writestr("OPS/text/z chapter.xhtml", chapter or '<html xml:lang="pl" dir="ltr"><body id="start"><h1>Chapter</h1><p id="réf">Text <em>with emphasis</em> <a href="a-notes.xhtml#note" epub:type="noteref" role="doc-noteref">1</a></p><a href="#start">Top</a></body></html>')
    return path


@pytest.mark.parametrize("version", ["2.0", "3.0"])
def test_epub_versions_preserve_order_notes_links_language_and_metadata(tmp_path, version):
    path = write_epub(tmp_path / "book.epub", version=version)
    status, html = HtmlEpubParser(str(path), 1, None).process()
    assert status == "COMPLETED"
    soup = BeautifulSoup(html, "html.parser")
    assert html.index("Chapter") < html.index("Note text")
    assert "Duplicate TOC" not in html
    assert soup.section["xml:lang"] == "pl"
    assert soup.section["dir"] == "ltr"
    assert soup.select_one('[epub\\:type="endnotes"]')
    for anchor in soup.find_all("a"):
        assert soup.find(id=anchor["href"][1:]) is not None
    metadata = extract_source_document_metadata(path)
    assert metadata["title"] == "Żółw"
    assert metadata["contributors"]["authors"] == ["Author"]
    assert metadata["contributors"]["translators"] == ["Translator"]
    assert metadata["isbn"] == "9780101010101"


def test_epub_xml_encodings_are_respected(tmp_path):
    chapter = '<?xml version="1.0" encoding="utf-16"?><html><body><p>Żółw 日本</p></body></html>'.encode("utf-16")
    path = write_epub(tmp_path / "book.epub", chapter=chapter, package_encoding="utf-16", spine='<itemref idref="chapter"/>')
    assert "Żółw 日本" in HtmlEpubParser(str(path), 1, None).process()[1]
    assert extract_source_document_metadata(path)["title"] == "Żółw"


def test_epub_nav_in_spine_does_not_duplicate_toc(tmp_path):
    path = write_epub(tmp_path / "book.epub", spine='<itemref idref="nav"/><itemref idref="chapter"/><itemref idref="notes"/>')
    html = HtmlEpubParser(str(path), 1, None).process()[1]
    assert "Duplicate TOC" not in html
    assert "Chapter" in html and "Note text" in html


def test_epub_manifest_fallback_is_used_in_spine_order(tmp_path):
    path = write_epub(tmp_path / "book.epub", spine='<itemref idref="foreign"/><itemref idref="notes"/>')
    html = HtmlEpubParser(str(path), 1, None).process()[1]
    assert html.index("Chapter") < html.index("Note text")


@pytest.mark.parametrize("spine", ['<itemref idref="missing"/>', '<itemref idref="chapter"/><itemref idref="chapter"/>', ' '])
def test_invalid_epub_spine_never_falls_back_to_alphabetical_content(tmp_path, spine):
    path = write_epub(tmp_path / "book.epub", spine=spine)
    with pytest.raises(ValueError, match="EPUB"):
        HtmlEpubParser(str(path), 1, None).process()


def test_epub_missing_local_target_never_retargets_link_to_chapter(tmp_path):
    path = write_epub(tmp_path / "book.epub", chapter='<html><body><p><a href="a-notes.xhtml#missing">1</a></p></body></html>')
    with pytest.raises(ValueError, match="link target"):
        HtmlEpubParser(str(path), 1, None).process()


def test_epub_xhtml_and_semantic_namespace_aliases(tmp_path):
    chapter = '<h:html xmlns:h="http://www.w3.org/1999/xhtml" xmlns:e="http://www.idpf.org/2007/ops"><h:body id="réf"><h:nav e:type="toc"><h:p>Duplicate TOC</h:p></h:nav><h:p>Body <h:a href="a-notes.xhtml#note" e:type="noteref">1</h:a></h:p></h:body></h:html>'
    path = write_epub(tmp_path / "book.epub", chapter=chapter)
    html = HtmlEpubParser(str(path), 1, None).process()[1]
    assert "Duplicate TOC" not in html
    assert 'epub:type="noteref"' in html
    assert "<h:p>" not in html
    assert '<p>Body ' in html
