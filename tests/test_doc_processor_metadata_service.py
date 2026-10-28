import zipfile

from iarf_assistant.addons.doc_processor.metadata_service import (
    extract_source_document_metadata,
    normalize_document_metadata_payload,
)


def test_language_names_are_mapped_to_bcp47_and_invalid_values_are_rejected():
    assert normalize_document_metadata_payload({"language": "English"})["language"] == "en"
    assert normalize_document_metadata_payload({"language": "pt_BR"})["language"] == "pt-BR"
    assert "language" not in normalize_document_metadata_payload({"language": "not a language"})


def test_epub_onix_identifier_type_is_mapped_to_isbn(tmp_path):
    epub_path = tmp_path / "book.epub"
    container = """<?xml version="1.0"?>
    <container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">
      <rootfiles><rootfile full-path="EPUB/package.opf" media-type="application/oebps-package+xml"/></rootfiles>
    </container>"""
    package = """<?xml version="1.0"?>
    <package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="pub">
      <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
        <dc:identifier id="pub">urn:uuid:11111111-1111-1111-1111-111111111111</dc:identifier>
        <dc:identifier id="isbn">urn:isbn:9780101010101</dc:identifier>
        <meta refines="#isbn" property="identifier-type" scheme="onix:codelist5">15</meta>
        <dc:title>Book</dc:title><dc:language>en</dc:language>
      </metadata>
      <manifest><item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/></manifest>
      <spine/>
    </package>"""
    with zipfile.ZipFile(epub_path, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("META-INF/container.xml", container)
        archive.writestr("EPUB/package.opf", package)

    metadata = extract_source_document_metadata(epub_path)

    assert metadata["isbn"] == "9780101010101"
    assert {entry.get("scheme") for entry in metadata["identifiers"]} >= {"ISBN"}
