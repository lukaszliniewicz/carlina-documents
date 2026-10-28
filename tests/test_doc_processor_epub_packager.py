import io
import sys
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from xml.etree import ElementTree as ET

import pytest
from bs4 import BeautifulSoup
from flask import Flask
from PIL import Image, ImageCms, features

from iarf_assistant.addons.doc_processor import epub_packager, routes
from iarf_assistant.addons.doc_processor.epub_packager import (
    EPUBPackager,
    diagnose_semantic_export,
    validate_semantic_export,
)
from iarf_assistant.addons.doc_processor.parsers.pdf_utils import concatenate_html_pages
from iarf_assistant.addons.doc_processor.routes import _validate_epub_file

OPF_NS = {
    "opf": "http://www.idpf.org/2007/opf",
    "dc": "http://purl.org/dc/elements/1.1/",
}
XHTML_NS = {
    "xhtml": "http://www.w3.org/1999/xhtml",
    "epub": "http://www.idpf.org/2007/ops",
}


def _document(tmp_path: Path, image_path: Path):
    metadata = {
        "title": "Main & Title",
        "subtitle": "Subtitle",
        "language": "en-US",
        "contributors": {
            "authors": ["Alice Author"],
            "editors": ["Eve Editor"],
            "translators": ["Tran Slator"],
            "other": [{"role": "illustrator", "name": "Ivy Image"}],
        },
        "publisher": "Example Press",
        "publication_date": "2026-07-11",
        "rights": "CC BY 4.0",
        "source": "urn:source:test",
        "description": "Description",
        "subjects": ["History", "Religion"],
        "publication_type": "book",
        "identifiers": [{"scheme": "DOI", "value": "10.1234/example"}],
        "isbn": "9781234567890",
        "issn": "1234-5678",
    }
    output_html = (
        "<main><h1>Main &amp; Title</h1><h2>Chapter One</h2><p>Text "
        '<sup><a href="#fn1" epub:type="noteref">1</a></sup></p>'
        '<section role="doc-endnotes" epub:type="endnotes"><h2>Notes</h2><ol>'
        '<li id="fn1" role="doc-endnote" epub:type="endnote">Note</li></ol></section>'
        f'<img src="{image_path.as_posix()}" alt="A red square"/></main>'
    )
    return SimpleNamespace(
        id="11111111-1111-1111-1111-111111111111",
        filename="source.pdf",
        file_path=str(tmp_path / "source.pdf"),
        settings={"document_metadata": metadata},
        metadata_record=None,
        output_html=output_html,
        output_markdown=None,
    )


def _jpeg_mode_normalize(
    tmp_path: Path,
    data: bytes,
    filename: str,
    media_type: str,
    *,
    max_edge: int = 2400,
) -> tuple[bytes, str, str]:
    app = Flask(__name__)
    document = _document(tmp_path, tmp_path / "seed.png")
    with app.app_context():
        packager = EPUBPackager(
            document, str(tmp_path / "unused.epub"), image_mode="jpeg"
        )
        return packager._normalize_image_payload(
            data, filename, media_type, max_edge=max_edge
        )


def _high_color_image(size: tuple[int, int] = (64, 48)) -> Image.Image:
    image = Image.new("RGB", size)
    image.putdata(
        [
            (index % 256, (index // 256) % 256, (index * 73) % 256)
            for index in range(size[0] * size[1])
        ]
    )
    return image


def _image_bytes(image: Image.Image, image_format: str, **options: object) -> bytes:
    output = io.BytesIO()
    image.save(output, format=image_format, **options)
    return output.getvalue()


def test_epub_packager_builds_valid_self_contained_epub3_with_rich_metadata(tmp_path):
    app = Flask(__name__)
    image_path = tmp_path / "figure.png"
    Image.new("RGB", (16, 16), "red").save(image_path)
    output_path = tmp_path / "book.epub"
    document = _document(tmp_path, image_path)
    source_html = document.output_html

    with app.app_context():
        EPUBPackager(document, str(output_path)).build()

    assert document.output_html == source_html
    assert _validate_epub_file(str(output_path)) == (True, None)
    with zipfile.ZipFile(output_path) as archive:
        names = archive.namelist()
        assert names[0] == "mimetype"
        assert archive.getinfo("mimetype").compress_type == zipfile.ZIP_STORED
        package = ET.fromstring(archive.read("EPUB/package.opf"))
        content_members = sorted(
            name
            for name in names
            if name.startswith("EPUB/content-") and name.endswith(".xhtml")
        )
        contents = [ET.fromstring(archive.read(name)) for name in content_members]
        assert not any(
            "doc-endnote" in str(node.get("role", "")).split()
            for content in contents for node in content.iter()
        )
        assert 'role="doc-endnote"' in source_html
        nav = ET.fromstring(archive.read("EPUB/nav.xhtml"))

        manifest = package.find("opf:manifest", OPF_NS)
        manifested = {item.get("href") for item in manifest.findall("opf:item", OPF_NS)}
        image = next(
            (
                node
                for content in contents
                if (node := content.find(".//xhtml:img", XHTML_NS)) is not None
            ),
            None,
        )
        assert image is not None and image.get("src") in manifested
        assert f"EPUB/{image.get('src')}" in names
        assert any(
            content.find(".//xhtml:ol/xhtml:li[@epub:type='endnote']", XHTML_NS)
            is not None
            for content in contents
        )
        assert (
            len(nav.findall(".//xhtml:nav[@epub:type='toc']//xhtml:a", XHTML_NS)) >= 2
        )

        package_text = archive.read("EPUB/package.opf").decode("utf-8")
        for value in (
            "Subtitle",
            "Alice Author",
            "Eve Editor",
            "Tran Slator",
            "Ivy Image",
            "CC BY 4.0",
            "urn:source:test",
            "Description",
            "History",
            "Religion",
            "10.1234/example",
            "9781234567890",
            "1234-5678",
        ):
            assert value in package_text
        assert "2026-01-01T00:00:00Z" not in package_text
        modified = package.find(
            "opf:metadata/opf:meta[@property='dcterms:modified']", OPF_NS
        )
        datetime.strptime(modified.text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        identifier_types = [
            (node.text, node.get("scheme"))
            for node in package.findall(
                "opf:metadata/opf:meta[@property='identifier-type']", OPF_NS
            )
        ]
        assert ("06", "onix:codelist5") in identifier_types
        assert ("15", "onix:codelist5") in identifier_types
        assert ("ISSN", None) in identifier_types
        assert ("02", "onix:codelist5") not in identifier_types


def test_epub_packager_retains_body_siblings_and_resolves_cross_spine_notes(
    tmp_path, monkeypatch
):
    app = Flask(__name__)
    cover_path = tmp_path / "cover.png"
    Image.new("RGB", (16, 16), "blue").save(cover_path)
    document = _document(tmp_path, cover_path)
    document.settings["document_metadata"]["cover_image_id"] = "cover-art"
    document.output_html = f"""
        <html><body>
          <section id="front" epub:type="frontmatter">
            <h1 id="cover-heading">Cover</h1>
            <p>Front matter</p>
            <img id="cover-art" src="{cover_path.as_posix()}" alt="Cover art"/>
          </section>
          <main id="source-main" lang="pl" data-subtitle-unit="body">
            <h1 id="chapter">Chapter One</h1>
            <p>Body <a href="#note-1" epub:type="noteref" role="doc-noteref">1</a></p>
          </main>
          <section id="notes" epub:type="endnotes">
            <h2 id="notes-heading">Notes</h2>
            <ol><li id="note-1" role="doc-endnote" epub:type="endnote">
              <p>Note body <a href="#chapter">Back to chapter</a></p>
            </li></ol>
          </section>
          <section id="appendix">
            <h2 id="appendix-heading">Appendix</h2>
            <p>Appendix text</p>
          </section>
        </body></html>
    """
    monkeypatch.setattr(epub_packager, "MAX_CONTENT_DOCUMENT_BYTES", 1)
    output_path = tmp_path / "siblings.epub"

    with app.app_context():
        EPUBPackager(document, str(output_path)).build()

    with zipfile.ZipFile(output_path) as archive:
        names = archive.namelist()
        content_names = sorted(
            name
            for name in names
            if name.startswith("EPUB/content-") and name.endswith(".xhtml")
        )
        contents = {name: ET.fromstring(archive.read(name)) for name in content_names}
        package = ET.fromstring(archive.read("EPUB/package.opf"))

    rendered_text = " ".join(
        " ".join(
            " ".join(contents[name].find("xhtml:body", XHTML_NS).itertext()).split()
        )
        for name in content_names
    )
    assert rendered_text == (
        "Cover Front matter Chapter One Body 1 Notes Note body Back to chapter "
        "Appendix Appendix text"
    )
    headings = {
        heading.text
        for root in contents.values()
        for heading in root.findall(".//xhtml:h1", XHTML_NS)
        + root.findall(".//xhtml:h2", XHTML_NS)
    }
    assert headings == {"Cover", "Chapter One", "Notes", "Appendix"}
    assert "Note body" in rendered_text

    source_main_root = next(
        root
        for root in contents.values()
        if root.find(".//xhtml:div[@id='source-main']", XHTML_NS) is not None
    )
    outer_main = source_main_root.find("xhtml:body/xhtml:main", XHTML_NS)
    source_main = source_main_root.find(".//xhtml:div[@id='source-main']", XHTML_NS)
    assert outer_main is not None and source_main is not None
    assert source_main.get("lang") == "pl"
    assert source_main.get("data-subtitle-unit") == "body"
    assert outer_main.get("lang") is None
    assert outer_main.findall(".//xhtml:main", XHTML_NS) == []

    anchors = {
        (name, anchor.get("href")): anchor
        for name, root in contents.items()
        for anchor in root.findall(".//xhtml:a[@href]", XHTML_NS)
    }
    noteref_href = next(
        href
        for (name, href), anchor in anchors.items()
        if anchor.get("{http://www.idpf.org/2007/ops}type") == "noteref"
    )
    backlink_href = next(
        href
        for (name, href), anchor in anchors.items()
        if anchor.text == "Back to chapter"
    )
    assert noteref_href == "content-003.xhtml#note-1"
    assert backlink_href == "content-002.xhtml#chapter"
    for source_name, href in anchors:
        target_name, target_id = href.split("#", 1)
        target_root = contents[f"EPUB/{target_name}"]
        assert target_root.find(f".//*[@id='{target_id}']") is not None

    image_items = [
        item
        for item in package.findall("opf:manifest/opf:item", OPF_NS)
        if item.get("media-type", "").startswith("image/")
    ]
    assert len(image_items) == 1
    cover_item = image_items[0]
    assert "cover-image" in str(cover_item.get("properties") or "").split()
    assert f"EPUB/{cover_item.get('href')}" in names
    cover_img = next(
        image
        for root in contents.values()
        if (image := root.find(".//xhtml:img[@id='cover-art']", XHTML_NS)) is not None
    )
    assert cover_img.get("src") == cover_item.get("href")


def test_epub_packager_rejects_unresolved_images(tmp_path):
    app = Flask(__name__)
    document = _document(tmp_path, tmp_path / "missing.png")
    with (
        app.app_context(),
        pytest.raises(FileNotFoundError, match="could not be resolved"),
    ):
        EPUBPackager(document, str(tmp_path / "bad.epub")).build()


def test_epub_packager_resolves_upload_resources_outside_installed_package(tmp_path):
    package_root = tmp_path / "installed-package"
    uploads_root = tmp_path / "live-data" / "uploads"
    package_root.mkdir()
    document_dir = uploads_root / "1" / "doc_processor_docs" / "doc-1"
    document_dir.mkdir(parents=True)
    source_path = document_dir / "source.pdf"
    source_path.write_bytes(b"%PDF")
    image_path = document_dir / "figure.png"
    Image.new("RGB", (16, 16), "red").save(image_path)
    document = _document(document_dir, image_path)
    document.file_path = "/uploads/1/doc_processor_docs/doc-1/source.pdf"
    document.output_html = (
        '<main><h1>Book</h1><img src="figure.png" alt="Figure"/></main>'
    )
    output_path = tmp_path / "split-root.epub"
    app = Flask(__name__, root_path=str(package_root))
    app.config["CARLINA_UPLOADS_DIR"] = str(uploads_root)

    with app.app_context():
        EPUBPackager(document, str(output_path)).build()

    assert _validate_epub_file(str(output_path)) == (True, None)


def test_epub_packager_rejects_images_outside_document_resource_roots(tmp_path):
    app = Flask(__name__)
    document_dir = tmp_path / "document"
    document_dir.mkdir()
    outside_image = tmp_path / "private.png"
    Image.new("RGB", (16, 16), "red").save(outside_image)
    document = _document(document_dir, outside_image)

    with (
        app.app_context(),
        pytest.raises(FileNotFoundError, match="could not be resolved"),
    ):
        EPUBPackager(document, str(tmp_path / "outside.epub")).build()


def test_epub_packager_explicitly_omits_source_toc_but_generates_heading_navigation(
    tmp_path,
):
    app = Flask(__name__)
    image_path = tmp_path / "figure.png"
    Image.new("RGB", (16, 16), "red").save(image_path)
    document = _document(tmp_path, image_path)
    document.settings["pdf_options"] = {"toc_handling": "preserve"}
    document.output_html = document.output_html.replace(
        "<h2>Chapter One</h2>",
        "<h2>Contents</h2><ol class='toc'><li>Chapter One ... 1</li></ol><h2>Chapter One</h2>",
    )
    output_path = tmp_path / "without-source-toc.epub"

    with app.app_context():
        EPUBPackager(
            document,
            str(output_path),
            include_source_toc=False,
        ).build()

    with zipfile.ZipFile(output_path) as archive:
        content_members = sorted(
            name
            for name in archive.namelist()
            if name.startswith("EPUB/content-") and name.endswith(".xhtml")
        )
        contents = [ET.fromstring(archive.read(name)) for name in content_members]
        nav = ET.fromstring(archive.read("EPUB/nav.xhtml"))
        content_text = " ".join(
            text for content in contents for text in content.itertext()
        )
        nav_labels = [
            node.text
            for node in nav.findall(".//xhtml:nav[@epub:type='toc']//xhtml:a", XHTML_NS)
        ]

    assert "Contents" not in content_text
    assert "Chapter One ... 1" not in content_text
    assert "Chapter One" in nav_labels
    assert "Contents" not in nav_labels


def test_epub_packager_ignores_legacy_include_source_toc_for_pdf(
    tmp_path,
):
    app = Flask(__name__)
    image_path = tmp_path / "figure.png"
    Image.new("RGB", (16, 16), "red").save(image_path)
    document = _document(tmp_path, image_path)
    document.settings["pdf_options"] = {"toc_handling": "omit"}
    document.output_html = document.output_html.replace(
        "<h2>Chapter One</h2>",
        (
            '<nav class="source-toc"><div class="toc-entry toc-depth-1">'
            '<span class="toc-entry-title">Chapter One</span> '
            '<span class="toc-page-label">xii</span></div></nav>'
            "<h2>Chapter One</h2>"
        ),
    )
    output_path = tmp_path / "with-source-toc.epub"

    with app.app_context():
        EPUBPackager(
            document,
            str(output_path),
            include_source_toc=True,
        ).build()

    with zipfile.ZipFile(output_path) as archive:
        content = b"\n".join(
            archive.read(name)
            for name in archive.namelist()
            if name.startswith("EPUB/content-") and name.endswith(".xhtml")
        ).decode("utf-8")
        nav = ET.fromstring(archive.read("EPUB/nav.xhtml"))
        nav_labels = [
            node.text
            for node in nav.findall(".//xhtml:nav[@epub:type='toc']//xhtml:a", XHTML_NS)
        ]

    assert 'class="source-toc"' not in content
    assert "xii" not in content
    assert nav_labels.count("Chapter One") == 1
    assert "xii" not in nav_labels


def test_epub_validator_runs_configured_epubcheck(tmp_path, monkeypatch):
    app = Flask(__name__)
    image_path = tmp_path / "figure.png"
    Image.new("RGB", (16, 16), "red").save(image_path)
    output_path = tmp_path / "book.epub"
    with app.app_context():
        EPUBPackager(_document(tmp_path, image_path), str(output_path)).build()

    executable = tmp_path / "epubcheck"
    executable.write_text("test placeholder", encoding="utf-8")
    monkeypatch.setenv("EPUBCHECK_EXECUTABLE", str(executable))
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(routes.subprocess, "run", fake_run)
    assert _validate_epub_file(str(output_path)) == (True, None)
    assert calls[0][0] == [str(executable), str(output_path)]
    assert calls[0][1]["timeout"] == 180


def test_epub_validator_fails_for_missing_configured_epubcheck(tmp_path, monkeypatch):
    app = Flask(__name__)
    image_path = tmp_path / "figure.png"
    Image.new("RGB", (16, 16), "red").save(image_path)
    output_path = tmp_path / "book.epub"
    with app.app_context():
        EPUBPackager(_document(tmp_path, image_path), str(output_path)).build()

    missing = tmp_path / "missing-epubcheck"
    monkeypatch.setenv("EPUBCHECK_EXECUTABLE", str(missing))
    valid, error = _validate_epub_file(str(output_path))
    assert valid is False
    assert "was not found" in error


def test_epub_packager_transcodes_foreign_raster_and_does_not_guess_cover(tmp_path):
    app = Flask(__name__)
    bitmap = tmp_path / "figure.bmp"
    Image.new("RGB", (16, 16), "blue").save(bitmap)
    document = _document(tmp_path, bitmap)
    document.settings["document_metadata"]["cover_image_id"] = "missing-cover-id"
    output_path = tmp_path / "transcoded.epub"

    with app.app_context():
        EPUBPackager(document, str(output_path)).build()

    assert _validate_epub_file(str(output_path)) == (True, None)
    with zipfile.ZipFile(output_path) as archive:
        package = ET.fromstring(archive.read("EPUB/package.opf"))
        image_items = [
            item
            for item in package.findall("opf:manifest/opf:item", OPF_NS)
            if str(item.get("media-type") or "").startswith("image/")
        ]
    assert len(image_items) == 1
    assert image_items[0].get("media-type") == "image/png"
    assert "cover-image" not in str(image_items[0].get("properties") or "").split()


def test_epub_packager_uses_jpeg_for_opaque_photographic_foreign_raster(tmp_path):
    app = Flask(__name__)
    bitmap = tmp_path / "photo.bmp"
    image = Image.new("RGB", (64, 64))
    image.putdata(
        [
            ((x * 17 + y * 3) % 256, (x * 5 + y * 19) % 256, (x * 11 + y * 7) % 256)
            for y in range(64)
            for x in range(64)
        ]
    )
    image.save(bitmap)
    document = _document(tmp_path, bitmap)
    output_path = tmp_path / "photo.epub"

    with app.app_context():
        EPUBPackager(document, str(output_path)).build()

    assert _validate_epub_file(str(output_path)) == (True, None)
    with zipfile.ZipFile(output_path) as archive:
        package = ET.fromstring(archive.read("EPUB/package.opf"))
        image_item = next(
            item
            for item in package.findall("opf:manifest/opf:item", OPF_NS)
            if str(item.get("media-type") or "").startswith("image/")
        )
        payload = archive.read(f"EPUB/{image_item.get('href')}")
    assert image_item.get("media-type") == "image/jpeg"
    assert image_item.get("href").endswith(".jpg")
    with Image.open(io.BytesIO(payload)) as packaged_image:
        assert packaged_image.format == "JPEG"


@pytest.mark.skipif(
    not features.check("webp"), reason="Pillow WebP support is unavailable"
)
def test_epub_packager_webp_mode_converts_raster_images(tmp_path):
    app = Flask(__name__)
    raster = tmp_path / "figure.png"
    Image.new("RGBA", (32, 32), (0, 80, 200, 128)).save(raster)
    document = _document(tmp_path, raster)
    output_path = tmp_path / "webp.epub"

    with app.app_context():
        EPUBPackager(document, str(output_path), image_mode="webp").build()

    assert _validate_epub_file(str(output_path)) == (True, None)
    with zipfile.ZipFile(output_path) as archive:
        package = ET.fromstring(archive.read("EPUB/package.opf"))
        image_item = next(
            item
            for item in package.findall("opf:manifest/opf:item", OPF_NS)
            if str(item.get("media-type") or "").startswith("image/")
        )
        payload = archive.read(f"EPUB/{image_item.get('href')}")
    assert image_item.get("media-type") == "image/webp"
    assert image_item.get("href").endswith(".webp")
    with Image.open(io.BytesIO(payload)) as packaged_image:
        assert packaged_image.format == "WEBP"


def test_epub_packager_normalizes_series_roles_and_builds_hierarchical_nav(tmp_path):
    app = Flask(__name__)
    image_path = tmp_path / "figure.png"
    Image.new("RGB", (16, 16), "red").save(image_path)
    document = _document(tmp_path, image_path)
    document.settings["document_metadata"].update(
        {
            "journal_or_series": "Audit Series",
            "volume": "VII",
            "issue": "4",
            "contributors": {
                "authors": ["Alice Author"],
                "editors": [],
                "translators": [],
                "other": [
                    {"role": "photographer", "name": "Pat Photo"},
                    {"role": "fact checker", "name": "Fran Check"},
                ],
            },
        }
    )
    document.output_html = (
        "<main><h1>Book</h1><h2>Part</h2><h3>Chapter</h3><p>Text.</p></main>"
    )
    output_path = tmp_path / "hierarchical.epub"

    with app.app_context():
        EPUBPackager(document, str(output_path)).build()

    assert _validate_epub_file(str(output_path)) == (True, None)
    with zipfile.ZipFile(output_path) as archive:
        package = ET.fromstring(archive.read("EPUB/package.opf"))
        nav = ET.fromstring(archive.read("EPUB/nav.xhtml"))
        roles = [
            (node.text, node.get("scheme"))
            for node in package.findall(
                "opf:metadata/opf:meta[@property='role']", OPF_NS
            )
        ]
        group_position = package.find(
            "opf:metadata/opf:meta[@property='group-position']", OPF_NS
        )
        spine_items = package.findall("opf:spine/opf:itemref", OPF_NS)
        toc = nav.find(".//xhtml:nav[@epub:type='toc']", XHTML_NS)

    assert group_position is not None and group_position.text == "7.4"
    assert ("pht", "marc:relators") in roles
    assert ("fact checker", None) in roles
    assert len(spine_items) >= 2
    assert len(toc.findall(".//xhtml:li/xhtml:ol", XHTML_NS)) >= 2


def test_pdf_pagebreaks_generate_page_list_and_pagination_metadata(tmp_path):
    app = Flask(__name__)
    output_html = concatenate_html_pages(
        [
            "<main_content><h1>Book</h1><p>First page.</p></main_content><footnotes></footnotes>",
            "<main_content><h2>Chapter</h2><p>Second page.</p></main_content><footnotes></footnotes>",
        ]
    )
    document = SimpleNamespace(
        id="11111111-1111-1111-1111-111111111111",
        filename="source.pdf",
        file_path=str(tmp_path / "source.pdf"),
        settings={"document_metadata": {"title": "Book", "language": "en"}},
        metadata_record=None,
        output_html=output_html,
        output_markdown=None,
    )
    output_path = tmp_path / "pages.epub"

    with app.app_context():
        EPUBPackager(document, str(output_path)).build()

    assert _validate_epub_file(str(output_path)) == (True, None)
    with zipfile.ZipFile(output_path) as archive:
        nav = ET.fromstring(archive.read("EPUB/nav.xhtml"))
        package = ET.fromstring(archive.read("EPUB/package.opf"))
        page_links = nav.findall(
            ".//xhtml:nav[@epub:type='page-list']//xhtml:a", XHTML_NS
        )
        features = [
            node.text
            for node in package.findall(
                "opf:metadata/opf:meta[@property='schema:accessibilityFeature']", OPF_NS
            )
        ]
        source_of = package.find("opf:metadata/opf:meta[@property='source-of']", OPF_NS)

    assert [link.text for link in page_links] == ["1", "2"]
    assert "pageNavigation" in features
    assert source_of is not None and source_of.text == "pagination"


def test_epub_packager_rejects_broken_internal_links_and_duplicate_ids(tmp_path):
    app = Flask(__name__)
    image_path = tmp_path / "figure.png"
    Image.new("RGB", (16, 16), "red").save(image_path)
    broken = _document(tmp_path, image_path)
    broken.output_html = (
        "<main><h1>Book</h1><p><a href='#missing'>Broken</a></p></main>"
    )
    duplicate = _document(tmp_path, image_path)
    duplicate.output_html = (
        "<main><h1 id='same'>Book</h1><p id='same'>Duplicate</p></main>"
    )

    with (
        app.app_context(),
        pytest.raises(ValueError, match="Unresolved internal EPUB link"),
    ):
        EPUBPackager(broken, str(tmp_path / "broken.epub")).build()
    with app.app_context(), pytest.raises(ValueError, match="Duplicate HTML id"):
        EPUBPackager(duplicate, str(tmp_path / "duplicate.epub")).build()


def test_epub_packager_preserves_translator_compatible_rich_semantics(tmp_path):
    app = Flask(__name__)
    image_path = tmp_path / "figure.png"
    Image.new("RGB", (16, 16), "red").save(image_path)
    document = _document(tmp_path, image_path)
    document.output_html = f"""
        <main xml:lang="pl" data-subtitle-unit="chapter-1">
          <h1>Book {{{{template_value}}}}</h1>
          <blockquote cite="https://example.test/source"><p>Quoted text.</p></blockquote>
          <address>Author address</address>
          <ol start="4"><li value="7">List item</li></ol>
          <table summary="Summary"><colgroup span="1"><col span="1"></colgroup><tr><th abbr="Term">Term</th><td abbr="Value">Value</td></tr></table>
          <figure data-label="Figure one"><img src="{image_path.as_posix()}" loading="lazy" alt="Diagram"><figcaption>Caption</figcaption></figure>
          <p><time datetime="2026-08-24">Date</time> <a href="#fn-rich" epub:type="noteref" role="doc-noteref" data-footnote-label="1">1</a></p>
          <section id="fn-rich" epub:type="endnote" role="doc-endnote"><p>Note body</p></section>
          <script>alert('must not ship')</script><style>body {{ color: red }}</style>
        </main>
    """
    output_path = tmp_path / "rich.epub"

    with app.app_context():
        EPUBPackager(document, str(output_path)).build()

    with zipfile.ZipFile(output_path) as archive:
        content_name = next(
            name for name in archive.namelist() if name.endswith("content-001.xhtml")
        )
        root = ET.fromstring(archive.read(content_name))
        main = root.find(".//xhtml:main", XHTML_NS)
        blockquote = root.find(".//xhtml:blockquote", XHTML_NS)
        ordered_list = root.find(".//xhtml:ol", XHTML_NS)
        table = root.find(".//xhtml:table", XHTML_NS)
        figure = root.find(".//xhtml:figure", XHTML_NS)
        note_ref = root.find(".//xhtml:a[@epub:type='noteref']", XHTML_NS)

    assert main is not None
    assert main.get("{http://www.w3.org/XML/1998/namespace}lang") == "pl"
    assert main.get("data-subtitle-unit") == "chapter-1"
    rendered_text = "".join(root.itertext())
    assert "{{template_value}}" in rendered_text
    assert "{template_value}" in rendered_text
    assert (
        blockquote is not None
        and blockquote.get("cite") == "https://example.test/source"
    )
    assert root.find(".//xhtml:address", XHTML_NS).text == "Author address"
    assert ordered_list is not None and ordered_list.get("start") == "4"
    assert ordered_list.find("xhtml:li", XHTML_NS).get("value") == "7"
    assert table is not None and table.get("summary") == "Summary"
    assert table.find("xhtml:colgroup", XHTML_NS).get("span") == "1"
    assert table.find(".//xhtml:th", XHTML_NS).get("abbr") == "Term"
    assert table.find(".//xhtml:td", XHTML_NS).get("abbr") == "Value"
    assert figure is not None
    assert figure.find("xhtml:img", XHTML_NS).get("loading") == "lazy"
    assert figure.find("xhtml:figcaption", XHTML_NS).text == "Caption"
    assert note_ref is not None and note_ref.get("href").endswith("#fn-rich")
    assert not any(
        node.tag.rsplit("}", 1)[-1] in {"script", "style"} for node in root.iter()
    )


@pytest.mark.parametrize("figure_id", ["diagram-ñ", "図形", "d" * 110])
def test_epub_preserves_unicode_diagram_labels_and_bindings(tmp_path, figure_id):
    import hashlib

    app = Flask(__name__)
    document = _document(tmp_path, tmp_path / "unused.png")
    reserved = "diagram-" + hashlib.sha256(figure_id.encode()).hexdigest()[:20]
    label = "Zażółć e\u0301 Ελληνικά עברית العربية 日本語 æ œ ß ſ"
    document.output_html = (
        f'<main aria-labelledby="{figure_id}--a"><p id="{reserved}">Unrelated ID collision</p>'
        f'<figure class="mermaid-diagram-v1" id="{figure_id}">'
        '<pre class="mermaid-template"><code>flowchart TD\nn1["{{a}}"]</code></pre>'
        f'<div class="mermaid-labels"><p id="{figure_id}--a">{label}</p></div></figure>'
        f'<p aria-describedby="{figure_id}--a"><a href="#{figure_id}--a">Label reference</a></p>'
        f'<blockquote cite="#{figure_id}--a">Reference</blockquote></main>'
    )
    original_html = document.output_html
    output = tmp_path / "diagram.epub"
    with app.app_context():
        EPUBPackager(document, str(output)).build()
    assert document.output_html == original_html
    with zipfile.ZipFile(output) as archive:
        content_name = next(name for name in archive.namelist() if name.endswith("content-001.xhtml"))
        soup = BeautifulSoup(archive.read(content_name), "xml")
        assert label.encode() in archive.read("EPUB/" + soup.figure.img["src"])
    assert "diagram-rendered-v1" in soup.figure.get("class", [])
    assert not soup.select(".mermaid-template, .mermaid-labels")
    label_id = soup.main["aria-labelledby"]
    assert soup.find(id=label_id) is not None
    assert soup.find("p", attrs={"aria-describedby": label_id}).a["href"] == "content-001.xhtml#" + label_id
    assert soup.main["aria-labelledby"] == label_id
    assert soup.find("p", attrs={"aria-describedby": True})["aria-describedby"] == label_id
    assert soup.blockquote["cite"] == "#" + label_id
    assert len({tag["id"] for tag in soup.find_all(id=True)}) == len(soup.find_all(id=True))


def test_epub_preserves_multi_token_table_header_references(tmp_path):
    app = Flask(__name__)
    document = _document(tmp_path, tmp_path / "unused.png")
    document.output_html = (
        '<main><table><tr><th id="column-one">One</th>'
        '<th id="column-two">Two</th></tr><tr>'
        '<td headers="column-one column-two">Value</td></tr></table></main>'
    )
    output = tmp_path / "headers.epub"
    with app.app_context():
        EPUBPackager(document, str(output)).build()
    with zipfile.ZipFile(output) as archive:
        content_name = next(name for name in archive.namelist() if name.endswith("content-001.xhtml"))
        root = ET.fromstring(archive.read(content_name))
    cell = root.find(".//xhtml:td", XHTML_NS)
    assert cell is not None
    assert cell.get("headers") == "column-one column-two"


def test_epub_packager_rejects_unsafe_external_link_scheme(tmp_path):
    app = Flask(__name__)
    image_path = tmp_path / "figure.png"
    Image.new("RGB", (16, 16), "red").save(image_path)
    document = _document(tmp_path, image_path)
    document.output_html = (
        '<main><h1>Book</h1><p><a href="ftp://example.test/book">Unsafe</a></p></main>'
    )

    with app.app_context(), pytest.raises(ValueError, match="unsafe link scheme"):
        EPUBPackager(document, str(tmp_path / "unsafe-link.epub")).build()


def test_epub_packager_preserves_full_document_language_and_direction(tmp_path):
    app = Flask(__name__)
    image_path = tmp_path / "figure.png"
    Image.new("RGB", (16, 16), "red").save(image_path)
    document = _document(tmp_path, image_path)
    document.output_html = """
        <!doctype html>
        <html lang="pl" xml:lang="pl" dir="rtl" id="source-html">
          <head><title>Source</title></head>
          <body lang="pl" xml:lang="pl" dir="rtl" id="source-body">
            <main lang="pl" xml:lang="pl" dir="rtl" id="source-main">
              <h1 id="chapter">Rozdział</h1><p>Treść.</p>
              <h1 id="chapter-2">Drugi rozdział</h1><p>Więcej treści.</p>
            </main>
          </body>
        </html>
    """
    output_path = tmp_path / "language.epub"

    with app.app_context():
        EPUBPackager(document, str(output_path)).build()

    with zipfile.ZipFile(output_path) as archive:
        content_names = sorted(
            name
            for name in archive.namelist()
            if name.endswith(".xhtml") and "content-" in name
        )
        roots = [ET.fromstring(archive.read(name)) for name in content_names]
    assert len(roots) == 2
    for root in roots:
        body = root.find("xhtml:body", XHTML_NS)
        main = root.find(".//xhtml:main", XHTML_NS)
        assert root.get("lang") == "pl"
        assert root.get("{http://www.w3.org/XML/1998/namespace}lang") == "pl"
        assert root.get("dir") == "rtl"
        assert (
            body is not None and body.get("lang") == "pl" and body.get("dir") == "rtl"
        )
        assert main is not None
        assert main.get("{http://www.w3.org/XML/1998/namespace}lang") == "pl"
        assert main.get("dir") == "rtl"
        assert (
            root.get("id") is None and body.get("id") is None and main.get("id") is None
        )


def test_semantic_export_diagnostics_bound_adversarial_error_messages():
    long_id = "x" * 10_000
    soup = BeautifulSoup(
        "<main>"
        + "".join(f'<p id="duplicate">{index}</p>' for index in range(2000))
        + f'<p><a href="#{long_id}">broken</a></p></main>',
        "html.parser",
    )

    diagnostics = diagnose_semantic_export(soup)
    assert len(diagnostics) <= 64
    assert max(map(len, diagnostics)) < 400
    long_href_diagnostics = diagnose_semantic_export(
        BeautifulSoup(f'<p><a href="#{long_id}">broken</a></p>', "html.parser")
    )
    assert len(long_href_diagnostics) == 1
    assert len(long_href_diagnostics[0]) < 400
    assert long_href_diagnostics[0].endswith("…'")
    with pytest.raises(ValueError) as error:
        validate_semantic_export(soup, export_name="EPUB export")
    assert len(str(error.value)) < 5000
    assert "; and " in str(error.value)


def test_epub_validator_can_require_official_epubcheck(tmp_path, monkeypatch):
    app = Flask(__name__)
    image_path = tmp_path / "figure.png"
    Image.new("RGB", (16, 16), "red").save(image_path)
    output_path = tmp_path / "book.epub"
    with app.app_context():
        EPUBPackager(_document(tmp_path, image_path), str(output_path)).build()

    monkeypatch.delenv("EPUBCHECK_EXECUTABLE", raising=False)
    monkeypatch.delenv("EPUBCHECK_JAR", raising=False)
    monkeypatch.setenv("EPUBCHECK_REQUIRED", "true")
    monkeypatch.setattr(routes.shutil, "which", lambda _name: None)
    valid, error = _validate_epub_file(str(output_path))
    assert valid is False
    assert "Official EPUBCheck validation is required" in error


def _rewrite_epub(
    source: Path,
    destination: Path,
    replacements: dict[str, bytes],
    *,
    compress_mimetype=False,
):
    with (
        zipfile.ZipFile(source) as source_zip,
        zipfile.ZipFile(destination, "w") as destination_zip,
    ):
        for source_info in source_zip.infolist():
            payload = replacements.get(
                source_info.filename, source_zip.read(source_info.filename)
            )
            compression = source_info.compress_type
            if source_info.filename == "mimetype" and compress_mimetype:
                compression = zipfile.ZIP_DEFLATED
            destination_zip.writestr(
                source_info.filename, payload, compress_type=compression
            )


def test_epub_validator_rejects_corrupt_links_refinements_roles_and_mimetype(tmp_path):
    app = Flask(__name__)
    image_path = tmp_path / "figure.png"
    Image.new("RGB", (16, 16), "red").save(image_path)
    valid_path = tmp_path / "valid.epub"
    with app.app_context():
        EPUBPackager(_document(tmp_path, image_path), str(valid_path)).build()

    with zipfile.ZipFile(valid_path) as archive:
        package = archive.read("EPUB/package.opf")
        content_name = next(
            name
            for name in archive.namelist()
            if name.startswith("EPUB/content-")
            and name.endswith(".xhtml")
            and b"#fn1" in archive.read(name)
        )
        content = archive.read(content_name)

    corruptions = [
        (
            "broken-fragment.epub",
            {content_name: content.replace(b"#fn1", b"#missing-note")},
            "fragment does not resolve",
            False,
        ),
        (
            "broken-refines.epub",
            {
                "EPUB/package.opf": package.replace(
                    b'refines="#title-main"', b'refines="#missing-meta"', 1
                )
            },
            "refines target does not resolve",
            False,
        ),
        (
            "invalid-role.epub",
            {"EPUB/package.opf": package.replace(b">aut</", b">zzz</", 1)},
            "Unsupported MARC relator code",
            False,
        ),
        ("compressed-mimetype.epub", {}, "stored without compression", True),
    ]
    for filename, replacements, expected_error, compress_mimetype in corruptions:
        corrupt_path = tmp_path / filename
        _rewrite_epub(
            valid_path, corrupt_path, replacements, compress_mimetype=compress_mimetype
        )
        valid, error = _validate_epub_file(str(corrupt_path))
        assert valid is False
        assert expected_error in str(error)


def test_jpeg_mode_encodes_opaque_raster_with_jpegli_and_jpeg_media_type(
    tmp_path, monkeypatch
):
    import pyjpegli

    source = _image_bytes(_high_color_image(), "PNG")
    calls = []
    encode = pyjpegli.encode

    def record_encode(pixels, width, height, *, quality):
        calls.append((width, height, quality))
        return encode(pixels, width, height, quality=quality)

    monkeypatch.setattr(pyjpegli, "encode", record_encode)
    payload, filename, media_type = _jpeg_mode_normalize(
        tmp_path, source, "diagram.png", "image/png"
    )

    assert filename == "diagram.jpg"
    assert media_type == "image/jpeg"
    assert calls == [(64, 48, 90)]
    with Image.open(io.BytesIO(payload)) as encoded:
        assert encoded.format == "JPEG"
        assert encoded.size == (64, 48)
        encoded.load()


def test_jpeg_mode_converts_opaque_low_color_illustrations(tmp_path):
    image = Image.new("RGB", (32, 24), "white")
    for x in range(16):
        for y in range(24):
            image.putpixel((x, y), (0, 0, 0))
    source = _image_bytes(image, "PNG")

    payload, filename, media_type = _jpeg_mode_normalize(
        tmp_path, source, "diagram.png", "image/png"
    )

    assert filename == "diagram.jpg"
    assert media_type == "image/jpeg"
    with Image.open(io.BytesIO(payload)) as encoded:
        assert encoded.format == "JPEG"
        assert encoded.size == (32, 24)


def test_jpeg_mode_preserves_in_bounds_jpeg_svg_and_transparent_core_png(tmp_path):
    jpeg = _image_bytes(Image.new("RGB", (40, 20), "navy"), "JPEG")
    assert _jpeg_mode_normalize(
        tmp_path, jpeg, "existing.jpg", "image/jpeg", max_edge=40
    ) == (jpeg, "existing.jpg", "image/jpeg")

    svg = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1 1"/>'
    assert _jpeg_mode_normalize(tmp_path, svg, "diagram.svg", "image/svg+xml") == (
        svg,
        "diagram.svg",
        "image/svg+xml",
    )

    transparent = _image_bytes(Image.new("RGBA", (20, 12), (40, 80, 120, 128)), "PNG")
    assert _jpeg_mode_normalize(tmp_path, transparent, "figure.png", "image/png") == (
        transparent,
        "figure.png",
        "image/png",
    )


@pytest.mark.skipif(
    not features.check("webp"), reason="Pillow WebP support is unavailable"
)
def test_jpeg_mode_converts_transparent_webp_to_lossless_png(tmp_path):
    image = Image.new("RGBA", (19, 17))
    image.putdata(
        [
            (x * 11 % 256, y * 13 % 256, (x + y) * 7 % 256, (x * y) % 256)
            for y in range(17)
            for x in range(19)
        ]
    )
    source = _image_bytes(image, "WEBP", lossless=True)
    with Image.open(io.BytesIO(source)) as decoded_source:
        expected_pixels = decoded_source.convert("RGBA").tobytes()

    payload, filename, media_type = _jpeg_mode_normalize(
        tmp_path, source, "figure.webp", "image/webp"
    )

    assert filename == "figure.png"
    assert media_type == "image/png"
    with Image.open(io.BytesIO(payload)) as decoded:
        assert decoded.format == "PNG"
        assert decoded.convert("RGBA").tobytes() == expected_pixels


def test_jpeg_mode_preserves_animated_gif_byte_for_byte(tmp_path):
    frames = [
        Image.new("RGB", (12, 10), "red"),
        Image.new("RGB", (12, 10), "blue"),
    ]
    source = _image_bytes(
        frames[0],
        "GIF",
        save_all=True,
        append_images=frames[1:],
        duration=120,
        loop=0,
    )

    payload, filename, media_type = _jpeg_mode_normalize(
        tmp_path, source, "animation.gif", "image/gif"
    )

    assert (payload, filename, media_type) == (
        source,
        "animation.gif",
        "image/gif",
    )
    with Image.open(io.BytesIO(payload)) as animation:
        assert animation.is_animated
        assert animation.n_frames == 2


def test_jpeg_mode_applies_exif_orientation_and_downscales_without_upscaling(tmp_path):
    exif = Image.Exif()
    exif[274] = 6
    oriented_source = _image_bytes(_high_color_image((80, 40)), "JPEG", exif=exif)
    payload, _, media_type = _jpeg_mode_normalize(
        tmp_path,
        oriented_source,
        "oriented.jpg",
        "image/jpeg",
        max_edge=30,
    )
    assert media_type == "image/jpeg"
    with Image.open(io.BytesIO(payload)) as oriented:
        assert oriented.size == (15, 30)

    small_source = _image_bytes(_high_color_image((16, 12)), "PNG")
    small_payload, _, _ = _jpeg_mode_normalize(
        tmp_path, small_source, "small.png", "image/png", max_edge=64
    )
    with Image.open(io.BytesIO(small_payload)) as small:
        assert small.size == (16, 12)


def test_jpeg_mode_uses_profile_preserving_pillow_fallback(tmp_path, monkeypatch):
    import pyjpegli

    profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    source = _image_bytes(_high_color_image(), "PNG", icc_profile=profile)

    def unexpected_jpegli(*args, **kwargs):
        raise AssertionError("profile-bearing images must use Pillow")

    monkeypatch.setattr(pyjpegli, "encode", unexpected_jpegli)
    payload, filename, media_type = _jpeg_mode_normalize(
        tmp_path, source, "profiled.png", "image/png"
    )

    assert (filename, media_type) == ("profiled.jpg", "image/jpeg")
    with Image.open(io.BytesIO(payload)) as encoded:
        assert encoded.format == "JPEG"
        assert encoded.info["icc_profile"] == profile
        assert encoded.size == (64, 48)


@pytest.mark.parametrize("profile", [b"invalid", b"x" * 65_537])
def test_jpeg_mode_never_attaches_bad_profiles_and_preserves_core_input(
    tmp_path, profile
):
    source = _image_bytes(_high_color_image(), "PNG", icc_profile=profile)

    assert _jpeg_mode_normalize(tmp_path, source, "profiled.png", "image/png") == (
        source,
        "profiled.png",
        "image/png",
    )
    with pytest.raises(ValueError, match="ICC profile"):
        _jpeg_mode_normalize(tmp_path, source, "profiled.bmp", "image/bmp")


def test_jpeg_mode_preserves_unprofiled_cmyk_core_and_rejects_unsupported_cmyk(
    tmp_path,
):
    source = _image_bytes(Image.new("CMYK", (64, 48), (0, 100, 200, 10)), "JPEG")
    assert _jpeg_mode_normalize(
        tmp_path, source, "cmyk.jpg", "image/jpeg", max_edge=24
    ) == (source, "cmyk.jpg", "image/jpeg")

    unsupported = _image_bytes(Image.new("CMYK", (64, 48), (0, 100, 200, 10)), "TIFF")
    with pytest.raises(ValueError, match="CMYK"):
        _jpeg_mode_normalize(
            tmp_path, unsupported, "cmyk.tif", "image/tiff", max_edge=24
        )


def test_jpeg_mode_reports_missing_and_failed_jpegli_encoder(tmp_path, monkeypatch):
    source = _image_bytes(_high_color_image(), "PNG")
    monkeypatch.setitem(sys.modules, "pyjpegli", None)
    with pytest.raises(ValueError, match="pyjpegli==0.3.2"):
        _jpeg_mode_normalize(tmp_path, source, "photo.png", "image/png")

    monkeypatch.delitem(sys.modules, "pyjpegli")
    import pyjpegli

    def failed_encode(*args, **kwargs):
        raise OSError("Synthetic encoder failure")

    monkeypatch.setattr(pyjpegli, "encode", failed_encode)
    with pytest.raises(ValueError, match="failed while encoding with pyjpegli"):
        _jpeg_mode_normalize(tmp_path, source, "photo.png", "image/png")


def test_jpeg_mode_rejects_over_40_megapixels_before_loading(tmp_path, monkeypatch):
    class OversizedImage:
        width = 8_000
        height = 5_001
        size = (width, height)
        mode = "RGB"
        is_animated = False

        def __init__(self):
            self.info = {}
            self.loaded = False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def load(self):
            self.loaded = True

    oversized = OversizedImage()
    monkeypatch.setattr(epub_packager.Image, "open", lambda _stream: oversized)
    with pytest.raises(ValueError, match="40-megapixel"):
        _jpeg_mode_normalize(tmp_path, b"synthetic", "oversized.png", "image/png")
    assert not oversized.loaded
