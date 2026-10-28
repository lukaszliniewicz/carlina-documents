from __future__ import annotations

import base64
import hashlib
import io
import zipfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from xml.etree import ElementTree as ET

import pytest
from flask import Flask
from PIL import Image

from carlina.core.epub_export import (
    EpubExportContractError,
    epub_projection_options_sha256,
    epub_value_sha256,
    normalize_epub_options,
)
from iarf_assistant.addons.doc_processor import epub_packager
from iarf_assistant.addons.doc_processor.epub_packager import EPUBPackager
from iarf_assistant.addons.doc_processor.epub_quality import preflight_epub
from iarf_assistant.addons.doc_processor.epub_validation import (
    validate_epub_structure,
)

OPF_NS = {
    "opf": "http://www.idpf.org/2007/opf",
    "dc": "http://purl.org/dc/elements/1.1/",
}
XHTML_NS = {
    "xhtml": "http://www.w3.org/1999/xhtml",
    "epub": "http://www.idpf.org/2007/ops",
}
XML_NS = "http://www.w3.org/XML/1998/namespace"


def _document(
    tmp_path: Path,
    html: str,
    *,
    title: str = "Synthetic publication",
    language: str = "en",
    filename: str = "source.html",
    note_arrangement: dict[str, object] | None = None,
    document_id: str | None = "11111111-1111-1111-1111-111111111111",
) -> SimpleNamespace:
    metadata: dict[str, object] = {"title": title, "language": language}
    if note_arrangement is not None:
        metadata["note_arrangement"] = note_arrangement
    return SimpleNamespace(
        id=document_id,
        filename=filename,
        file_path=str(tmp_path / filename),
        settings={"document_metadata": metadata},
        metadata_record=None,
        output_html=html,
        output_markdown=None,
    )


@pytest.mark.parametrize("review", ["unreviewed", "description", "decorative"])
def test_editable_diagram_review_is_visible_and_survives_projection(tmp_path, review):
    from bs4 import BeautifulSoup

    from iarf_assistant.addons.doc_processor.epub_structure import review_epub_outline

    source = (
        '<main><h1>Process</h1><figure class="mermaid-diagram-v1" id="diagram1">'
        '<pre class="mermaid-template"><code>flowchart TD\nn1["{{a}}"]\nn2["{{b}}"]\nn1 --&gt; n2</code></pre>'
        '<div class="mermaid-labels"><p id="diagram1--a">First</p><p id="diagram1--b">Second</p></div></figure></main>'
    )
    outline = review_epub_outline(source)
    assert [
        {
            key: row[key]
            for key in ("id", "alt", "decorative", "is_cover", "is_selected_cover")
        }
        for row in outline["images"]
    ] == [
        {
            "id": "diagram1",
            "alt": "",
            "decorative": False,
            "is_cover": False,
            "is_selected_cover": False,
        }
    ]
    options = {}
    if review == "description":
        options["image_descriptions"] = {
            "diagram1": "A reviewed two-step process. Structure follows."
        }
    elif review == "decorative":
        options["decorative_images"] = ["diagram1"]
    packager, output, document = _build(tmp_path, source, options=options)
    assert document.output_html == source
    report = packager.quality_report()
    assert any(
        issue["code"] == "image-description-review" for issue in report["issues"]
    ) == (review == "unreviewed")
    assert report["checks"]["manual"] == "not_reviewed"
    with zipfile.ZipFile(output) as archive:
        content = next(
            archive.read(name)
            for name in archive.namelist()
            if name.endswith("content-001.xhtml")
        )
    markup = BeautifulSoup(content, "xml")
    assert markup.select_one(".diagram-description-v1") is not None
    if review == "description":
        assert markup.img["alt"] == "A reviewed two-step process. Structure follows."
    elif review == "decorative":
        assert markup.img["alt"] == "" and markup.img["role"] == "presentation"


@pytest.mark.parametrize("position", ["first", "middle", "last"])
def test_publishing_preserves_captions_at_valid_figure_edges(tmp_path, position):
    from bs4 import BeautifulSoup

    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), "blue").save(buffer, "PNG")
    image = (
        '<img id="picture" src="data:image/png;base64,'
        + base64.b64encode(buffer.getvalue()).decode()
        + '" alt="A blue square">'
    )
    caption = '<figcaption id="printed-caption"><b>Printed caption.</b></figcaption>'
    description = (
        '<p class="image-description-link"><a href="#description">Description</a></p>'
        '<div class="image-description-v1" id="description"><p>A blue square.</p></div>'
    )
    contents = {
        "first": caption + image + description,
        "middle": image + caption + description,
        "last": image + description + caption,
    }[position]
    source = "<main><h1>Figure</h1><figure>" + contents + "</figure></main>"
    _, target, document = _build(tmp_path, source, options={})
    with zipfile.ZipFile(target) as archive:
        soup = BeautifulSoup(archive.read("EPUB/content-001.xhtml"), "xml")
    children = soup.figure.find_all(True, recursive=False)
    assert children[-1 if position == "last" else 0].name == "figcaption"
    assert soup.find(id="printed-caption").b.get_text() == "Printed caption."
    assert soup.get_text().count("Printed caption.") == 1
    assert any(
        str(anchor.get("href", "")).endswith("#description")
        for anchor in soup.find_all("a")
    )
    assert document.output_html == source


def test_diagram_fragment_uses_edition_language_without_rewriting_source(tmp_path):
    from bs4 import BeautifulSoup

    source = '<main><h1>Schemat</h1><figure class="mermaid-diagram-v1" id="d"><pre class="mermaid-template"><code>flowchart TD\nn1["{{a}}"]</code></pre><div class="mermaid-labels"><p id="d--a">Początek</p></div></figure></main>'
    _, target, document = _build(tmp_path, source, options={}, language="pl")
    with zipfile.ZipFile(target) as archive:
        soup = BeautifulSoup(archive.read("EPUB/content-001.xhtml"), "xml")
    assert soup.select_one(".diagram-description-v1")["lang"] == "pl"
    assert soup.select_one(".diagram-nodes caption").get_text() == "Węzły schematu"
    assert soup.select_one(".diagram-nodes td")["lang"] == "pl"
    assert document.output_html == source


def test_reviewed_legacy_diagram_cannot_hide_its_new_alternative(tmp_path):
    from bs4 import BeautifulSoup

    source = '<main><h1>Graph</h1><figure role="img" aria-hidden="true" aria-labelledby="d--a" class="mermaid-diagram-v1" id="d"><pre class="mermaid-template"><code>flowchart TD\nn1["{{a}}"]</code></pre><div class="mermaid-labels"><p id="d--a">Start</p></div></figure></main>'
    _, target, document = _build(
        tmp_path,
        source,
        options={"image_descriptions": {"d": "Single start node. Structure follows."}},
    )
    with zipfile.ZipFile(target) as archive:
        soup = BeautifulSoup(archive.read("EPUB/content-001.xhtml"), "xml")
    assert not soup.figure.has_attr("role") and not soup.figure.has_attr("aria-hidden")
    assert soup.find(id=soup.figure["aria-labelledby"]).get_text() == "Start"
    assert document.output_html == source


def test_diagram_review_rejects_utf8_overflow_during_preflight():
    from carlina.core.epub_export import EpubExportContractError

    source = '<figure class="mermaid-diagram-v1" id="d"><pre class="mermaid-template"><code>flowchart TD\nn1["{{a}}"]</code></pre><div class="mermaid-labels"><p id="d--a">A</p></div></figure>'
    with pytest.raises(EpubExportContractError, match="epub-options-review"):
        preflight_epub(source, {}, {"image_descriptions": {"d": "Ą" * 2049}})
    assert (
        preflight_epub(source, {}, {"image_descriptions": {"d": "Ą" * 2048}})[
            "quality"
        ]["checks"]["structure"]
        == "not_run"
    )


def _build(
    tmp_path: Path,
    html: str,
    *,
    options: object = None,
    title: str = "Synthetic publication",
    language: str = "en",
    filename: str = "source.html",
    note_arrangement: dict[str, object] | None = None,
    document_id: str | None = "11111111-1111-1111-1111-111111111111",
) -> tuple[EPUBPackager, Path, SimpleNamespace]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    document = _document(
        tmp_path,
        html,
        title=title,
        language=language,
        filename=filename,
        note_arrangement=note_arrangement,
        document_id=document_id,
    )
    output = tmp_path / f"{Path(filename).stem}-{len(list(tmp_path.iterdir()))}.epub"
    with Flask(__name__).app_context():
        packager = EPUBPackager(
            document,
            str(output),
            options=options,
            allowed_resource_roots=[tmp_path, tmp_path.parent],
        )
        packager.build()
    return packager, output, document


def _package(path: Path) -> ET.Element:
    with zipfile.ZipFile(path) as archive:
        return ET.fromstring(archive.read("EPUB/package.opf"))


def _nav(path: Path) -> ET.Element:
    with zipfile.ZipFile(path) as archive:
        return ET.fromstring(archive.read("EPUB/nav.xhtml"))


def _contents(path: Path) -> list[tuple[str, ET.Element]]:
    with zipfile.ZipFile(path) as archive:
        names = sorted(
            name
            for name in archive.namelist()
            if name.startswith("EPUB/content-") and name.endswith(".xhtml")
        )
        return [
            (name.removeprefix("EPUB/"), ET.fromstring(archive.read(name)))
            for name in names
        ]


def _all_ids(contents: list[tuple[str, ET.Element]]) -> list[str]:
    return [
        str(node.get("id"))
        for _, root in contents
        for node in root.iter()
        if node.get("id")
    ]


def test_selected_cover_gets_a_fresh_review_identity_in_preflight_and_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from iarf_assistant.addons.doc_processor import epub_cover

    html = (
        '<main><span id="epub-cover-image"></span><figure class="cover">'
        '<img id="old-cover" src="missing-original.png" alt="Old painting" role="presentation">'
        '</figure><h2 id="chapter">Chapter</h2><p><a href="#old-cover">Cover link</a> '
        "Unchanged body.</p></main>"
    )
    metadata = {"title": "New edition", "language": "en"}
    cover = {"mode": "generated"}

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Preflight rendered a resource")

    monkeypatch.setattr(epub_cover, "render_title_cover", forbidden)
    monkeypatch.setattr(epub_cover, "render_pdf_cover", forbidden)
    preflight = preflight_epub(html, metadata, {}, cover=cover)
    image = preflight["outline"]["images"][0]
    assert image["id"] == "epub-cover-image-2"
    assert image["is_cover"] and image["is_selected_cover"]
    assert image["alt"] == "New edition — Cover"
    assert not image["decorative"]
    assert (
        preflight["outline"]["input_sha256"]
        == hashlib.sha256(html.encode()).hexdigest()
    )
    with pytest.raises(EpubExportContractError, match="epub-options-review-target"):
        preflight_epub(
            html,
            metadata,
            {"image_descriptions": {"old-cover": "Old painting"}},
            cover=cover,
        )
    with pytest.raises(EpubExportContractError, match="epub-options-stale-anchor"):
        preflight_epub(
            html, metadata, {"decorative_images": ["old-cover"]}, cover=cover
        )

    buffer = io.BytesIO()
    Image.new("RGB", (10, 12), "blue").save(buffer, "PNG")
    monkeypatch.setattr(
        epub_cover, "render_title_cover", lambda *_args, **_kwargs: buffer.getvalue()
    )
    options = {
        "chapter_level": 2,
        "image_descriptions": {image["id"]: "A reviewed blue cover"},
    }
    preflight_epub(html, metadata, options, cover=cover)
    document = _document(tmp_path, html, title="New edition")
    output = tmp_path / "replacement.epub"
    with Flask(__name__).app_context():
        EPUBPackager(document, str(output), options=options, cover=cover).build()
    contents = _contents(output)
    final_image = next(
        node
        for _, root in contents
        for node in root.iter()
        if node.tag.endswith("}img")
    )
    assert final_image.get("id") == image["id"]
    assert final_image.get("alt") == "A reviewed blue cover"
    assert final_image.get("role") != "presentation"
    assert "old-cover" in _all_ids(contents)
    _assert_internal_targets_resolve(contents)
    assert document.output_html == html


def _assert_internal_targets_resolve(contents: list[tuple[str, ET.Element]]) -> None:
    targets = {
        f"{name}#{node.get('id')}"
        for name, root in contents
        for node in root.iter()
        if node.get("id")
    }
    for name, root in contents:
        for node in root.iter():
            for attribute in ("href", "cite"):
                value = str(node.get(attribute) or "")
                if not value.startswith("#") and "#" not in value:
                    continue
                target_name, fragment = (
                    (name, value[1:])
                    if value.startswith("#")
                    else tuple(value.split("#", 1))
                )
                assert f"{target_name}#{fragment}" in targets


def _note_arrangement(**overrides: object) -> dict[str, object]:
    return {
        "placement": "page_footnotes",
        "numbering": "chapter",
        "scope_heading_level": 2,
        "source_pages": [1, 2],
        "evidence": "Synthetic fixture declares chapter-local printed numbering.",
        **overrides,
    }


def _restarted_notes_html(*, cross_chapter_reference: bool = False) -> str:
    second_ref = (
        '<a id="ref-2" href="#note-1" role="doc-noteref" '
        'epub:type="noteref" data-footnote-scope="chapter-2">[1]</a>'
        if cross_chapter_reference
        else '<a id="ref-2" href="#note-2" role="doc-noteref" '
        'epub:type="noteref" data-footnote-scope="chapter-2">[1]</a>'
    )
    notes = (
        '<li id="note-1" role="doc-endnote" epub:type="endnote" '
        'data-footnote-scope="chapter-1"><strong class="footnote-marker">[1]</strong> '
        'First chapter note. <blockquote cite="#body-1">Quoted source.</blockquote></li>'
    )
    if not cross_chapter_reference:
        notes += (
            '<li id="note-2" role="doc-endnote" epub:type="endnote" '
            'data-footnote-scope="chapter-2"><strong class="footnote-marker">[1]</strong> '
            "Second chapter note.</li>"
        )
    return (
        '<main><h1 id="publication-title">Restarted notes</h1>'
        '<section><h2 id="chapter-1">Chapter One</h2>'
        '<p id="body-1">First body text '
        '<a id="ref-1" href="#note-1" role="doc-noteref" epub:type="noteref" '
        'data-footnote-scope="chapter-1">[1]</a></p>'
        '<h2 id="chapter-2">Chapter Two</h2>'
        '<p id="body-2">Second body text '
        f"{second_ref}</p></section></main>"
        '<section id="collected-notes" role="doc-endnotes" epub:type="endnotes">'
        '<h2 id="notes-heading">Collected notes</h2><ol>'
        f"{notes}</ol></section>"
    )


def test_empty_options_select_publishing_v1_while_absent_options_keep_legacy_export(
    tmp_path: Path,
):
    html = '<main><h1 id="title">Synthetic publication</h1><p>Body.</p></main>'
    legacy, _, _ = _build(tmp_path, html, options=None)
    publishing, _, _ = _build(tmp_path, html, options={})

    assert legacy.options == {}
    assert publishing.options["profile"] == "publishing-v2"
    assert legacy.quality_report() is None
    assert publishing.quality_report() is not None


def test_nav_and_bodymatter_follow_publication_roles_and_preserve_source_content(
    tmp_path: Path,
):
    html = (
        '<html lang="en"><body><main>'
        '<section epub:type="cover"><h1 id="cover-heading">Cover words</h1></section>'
        '<section epub:type="titlepage"><h1 id="title-heading">Synthetic publication</h1></section>'
        '<section epub:type="chapter"><h2 id="chapter-one">Chapter One</h2>'
        '<h3 id="section-one">First subsection</h3><p id="body-one">First words.</p></section>'
        '<section epub:type="chapter"><h2 id="chapter-two">Chapter Two</h2>'
        '<h3 id="section-two">Second subsection</h3><p id="body-two">Second words.</p></section>'
        "</main></body></html>"
    )
    packager, path, document = _build(
        tmp_path,
        html,
        options={"chapter_level": 2},
        title="Synthetic publication",
    )

    assert document.output_html == html
    contents = _contents(path)
    ids = _all_ids(contents)
    assert len(ids) == len(set(ids))
    assert {
        "cover-heading",
        "title-heading",
        "chapter-one",
        "section-one",
        "chapter-two",
        "section-two",
    } <= set(ids)
    body_text = " ".join(
        " ".join(" ".join(root.find("xhtml:body", XHTML_NS).itertext()).split())
        for _, root in contents
    )
    for word in (
        "Cover words",
        "Synthetic publication",
        "First subsection",
        "First words.",
        "Second subsection",
        "Second words.",
    ):
        assert word in body_text
    assert any(
        node.tag == f"{{{XHTML_NS['xhtml']}}}h1" and node.get("id") == "cover-heading"
        for _, root in contents
        for node in root.iter()
    )

    navigation = _nav(path)
    toc = navigation.find(".//xhtml:nav[@epub:type='toc']", XHTML_NS)
    assert toc is not None
    root_items = toc.findall("xhtml:ol/xhtml:li", XHTML_NS)
    root_targets = [
        item.find("xhtml:a", XHTML_NS).get("href")
        for item in root_items
        if item.find("xhtml:a", XHTML_NS) is not None
    ]
    assert [href.rsplit("#", 1)[-1] for href in root_targets] == [
        "chapter-one",
        "chapter-two",
    ]
    assert all(
        item.find("xhtml:ol/xhtml:li", XHTML_NS) is not None for item in root_items
    )

    landmarks = navigation.find(".//xhtml:nav[@epub:type='landmarks']", XHTML_NS)
    assert landmarks is not None
    bodymatter = landmarks.find(".//xhtml:a[@epub:type='bodymatter']", XHTML_NS)
    assert bodymatter is not None
    assert bodymatter.get("href", "").endswith("#chapter-one")
    assert packager._structure.roles["chapter-one"] == "chapter"


def test_reviewed_table_headers_and_image_alternatives_have_truthful_semantics(
    tmp_path: Path,
):
    image_path = tmp_path / "synthetic.png"
    Image.new("RGB", (8, 8), "blue").save(image_path)
    source_only_html = (
        '<main><h1 id="title">Synthetic publication</h1>'
        f'<figure><img id="source-image" src="{image_path}" alt="Source description"></figure>'
        '<table id="data-table"><caption>Sample values</caption>'
        "<tr><td>Row label</td><td>Value one</td></tr>"
        "<tr><td>Second row</td><td>Value two</td></tr></table></main>"
    )
    source_only, source_path, _ = _build(
        tmp_path / "source-only", source_only_html, options={}
    )
    source_package = _package(source_path)
    source_meta = source_package.find("opf:metadata", OPF_NS)
    assert source_meta is not None
    source_properties = {
        node.get("property"): node.text
        for node in source_meta.findall("opf:meta", OPF_NS)
    }
    assert "schema:accessModeSufficient" not in source_properties
    assert "dcterms:conformsTo" not in source_properties
    assert "alternativeText" not in source_properties.values()

    reviewed, reviewed_path, _ = _build(
        tmp_path / "reviewed",
        source_only_html,
        options={
            "table_headers": {"data-table": "row"},
            "image_descriptions": {"source-image": "Reviewed image description."},
        },
    )
    contents = _contents(reviewed_path)
    image = next(
        node
        for _, root in contents
        for node in root.iter(f"{{{XHTML_NS['xhtml']}}}img")
        if node.get("id") == "source-image"
    )
    assert image.get("alt") == "Reviewed image description."
    table = next(
        node
        for _, root in contents
        for node in root.iter(f"{{{XHTML_NS['xhtml']}}}table")
        if node.get("id") == "data-table"
    )
    assert "Sample values" in " ".join(table.itertext())
    cells = table.findall(".//xhtml:tr", XHTML_NS)
    assert [cell[0].tag.rsplit("}", 1)[-1] for cell in cells] == ["th", "th"]
    assert [cell[0].get("scope") for cell in cells] == ["row", "row"]
    assert [cell[1].tag.rsplit("}", 1)[-1] for cell in cells] == ["td", "td"]
    reviewed_meta = _package(reviewed_path).find("opf:metadata", OPF_NS)
    assert reviewed_meta is not None
    reviewed_properties = {
        node.get("property"): node.text
        for node in reviewed_meta.findall("opf:meta", OPF_NS)
    }
    assert "schema:accessModeSufficient" not in reviewed_properties
    assert "dcterms:conformsTo" not in reviewed_properties
    assert "alternativeText" in reviewed_properties.values()
    assert source_only._quality_soup is not None
    assert reviewed._quality_soup is not None


def test_explicit_decorative_image_is_empty_alternative_and_marked_presentation(
    tmp_path: Path,
):
    image_path = tmp_path / "ornament.png"
    Image.new("RGB", (6, 6), "green").save(image_path)
    html = (
        '<main><h1 id="title">Synthetic publication</h1>'
        f'<img id="ornament" src="{image_path}" alt="Old prose">'
        "</main>"
    )
    _, path, _ = _build(
        tmp_path,
        html,
        options={"decorative_images": ["ornament"]},
    )
    image = next(
        node
        for _, root in _contents(path)
        for node in root.iter(f"{{{XHTML_NS['xhtml']}}}img")
    )
    assert image.get("alt") == ""
    assert image.get("role") == "presentation"


def test_pdf_page_markers_are_printed_only_after_explicit_review(tmp_path: Path):
    html = (
        '<main><h1 id="title">Synthetic publication</h1><h2 id="chapter">Chapter</h2>'
        '<p>Text.</p><span id="pdf-page-1" epub:type="pagebreak" '
        'role="doc-pagebreak" title="1"></span></main>'
    )
    unreviewed, unreviewed_path, _ = _build(
        tmp_path / "unreviewed", html, options={}, filename="scan.pdf"
    )
    assert unreviewed._page_entries == [
        {"label": "1", "href": "content-001.xhtml#pdf-page-1", "printed": False}
    ]
    unreviewed_marker = next(
        node
        for _, root in _contents(unreviewed_path)
        for node in root.iter()
        if node.get("id") == "pdf-page-1"
    )
    assert unreviewed_marker.get("data-epub-printed-page") is None

    reviewed, reviewed_path, _ = _build(
        tmp_path / "reviewed",
        html,
        options={"page_labels": {"pdf-page-1": "iv"}},
        filename="scan.pdf",
    )
    assert reviewed._page_entries[0]["printed"] is True
    reviewed_marker = next(
        node
        for _, root in _contents(reviewed_path)
        for node in root.iter()
        if node.get("id") == "pdf-page-1"
    )
    assert reviewed_marker.get("title") == "iv"
    assert reviewed_marker.get("data-epub-printed-page") == "true"
    page_list = _nav(reviewed_path).find(
        ".//xhtml:nav[@epub:type='page-list']", XHTML_NS
    )
    assert page_list is not None
    assert (
        page_list.find(".//xhtml:a", XHTML_NS).get("href", "").endswith("#pdf-page-1")
    )


def test_target_language_metadata_and_rtl_progression_are_serialized(tmp_path: Path):
    html = (
        '<html lang="en"><body><main><h1 id="title">Synthetic publication</h1>'
        '<h2 id="chapter">Chapter</h2><p>Body.</p></main></body></html>'
    )
    _, path, _ = _build(tmp_path, html, options={}, language="ar")
    package = _package(path)
    language = package.find("opf:metadata/dc:language", OPF_NS)
    spine = package.find("opf:spine", OPF_NS)
    assert language is not None and language.text == "ar"
    assert spine is not None and spine.get("page-progression-direction") == "rtl"
    for _, root in _contents(path):
        assert root.get("lang") == "ar"
        assert root.get(f"{{{XML_NS}}}lang") == "ar"
        assert root.get("dir") == "rtl"


@pytest.mark.parametrize(
    "source_language,source_direction,edition,direction",
    [("en", "ltr", "ar", "rtl"), ("ar", "rtl", "en", "ltr")],
)
def test_edition_language_overrides_old_wrapper_language_and_direction(
    tmp_path: Path,
    source_language: str,
    source_direction: str,
    edition: str,
    direction: str,
):
    html = (
        f'<html lang="{source_language}" dir="{source_direction}"><body lang="{source_language}">'
        f'<main lang="{source_language}" role="doc-chapter" epub:type="chapter"><h2 id="chapter">Chapter</h2>'
        '<p><span lang="de">Ein Zitat.</span></p></main></body></html>'
    )
    _, path, _ = _build(tmp_path, html, options={}, language=edition)
    for _, root in _contents(path):
        assert root.get("lang") == edition
        assert root.get("dir") == direction
        main = root.find("xhtml:body/xhtml:main", XHTML_NS)
        assert (
            main is not None and main.get("role") is None and main.get("lang") is None
        )
        chapter = main.find("xhtml:section", XHTML_NS)
        assert chapter is not None and chapter.get("role") == "doc-chapter"
        assert root.find(".//xhtml:span[@lang='de']", XHTML_NS) is not None


def test_source_wrapper_link_destinations_survive_reading_section_split(tmp_path: Path):
    html = (
        '<html id="publication"><body id="body"><main id="text" aria-labelledby="chapter-one">'
        '<h2 id="chapter-one">One</h2><p>First chapter.</p><h2 id="chapter-two">Two</h2>'
        '<p><a href="#publication">Publication</a><a href="#body">Body</a><a href="#text">Text</a></p>'
        "</main></body></html>"
    )
    _, path, document = _build(tmp_path, html, options={"chapter_level": 2})
    assert document.output_html == html
    contents = _contents(path)
    assert len(contents) == 2
    for identifier in ("publication", "body", "text"):
        assert _all_ids(contents).count(identifier) == 1
    _assert_internal_targets_resolve(contents)
    second_main = contents[1][1].find("xhtml:body/xhtml:main", XHTML_NS)
    assert second_main is not None and second_main.get("aria-labelledby") is None
    assert second_main.get("aria-label") == "Two"
    assert validate_epub_structure(str(path)) == (True, None)


def test_cross_section_accessibility_relationship_blocks_export(tmp_path: Path):
    html = (
        '<main><h2 id="one">One</h2><p id="description">Description.</p>'
        '<h2 id="two">Two</h2><p aria-describedby="description">Second chapter.</p></main>'
    )
    with pytest.raises(ValueError, match="epub-relationship-cross-document"):
        _build(tmp_path, html, options={"chapter_level": 2})


def test_preflight_blocks_a_broken_source_link_without_quoting_source():
    with pytest.raises(EpubExportContractError, match="epub-source-invalid"):
        preflight_epub(
            '<main><p><a href="#missing-private-target">Link</a></p></main>',
            {"language": "en"},
            {},
        )


@pytest.mark.parametrize("language,notes_title", [("en", "Notes"), ("pl", "Przypisy")])
def test_headingless_notes_after_index_get_own_title_navigation_and_one_landmark(
    tmp_path: Path, language: str, notes_title: str
):
    source = (
        '<main><h2 id="chapter">Chapter</h2><p>Body '
        '<a id="reference" href="#note" epub:type="noteref" '
        'role="doc-noteref" data-footnote-label="391">391</a>.</p>'
        '<h2 id="index">Index</h2><p>Index entry.</p></main>'
        '<section id="notes" epub:type="endnotes" role="doc-endnotes">'
        '<ol><li id="note" epub:type="endnote" role="doc-endnote" '
        'data-footnote-label="391">Exact note body.</li></ol></section>'
    )
    preflight = preflight_epub(source, {"language": language}, {"chapter_level": 2})
    packager, path, document = _build(
        tmp_path, source, options={"chapter_level": 2}, language=language
    )
    assert document.output_html == source
    assert preflight["quality"]["counts"] == packager.quality_report()["counts"]
    assert not any(
        issue["code"] == "notes-heading-needs-review"
        for issue in preflight["quality"]["issues"]
    )
    contents = _contents(path)
    notes_file, notes_root = next(
        (name, root)
        for name, root in contents
        if root.find(".//*[@id='notes']") is not None
    )
    collection = notes_root.find(".//*[@id='notes']")
    assert collection is not None
    heading = collection.find("xhtml:h1", XHTML_NS)
    assert heading is not None and "".join(heading.itertext()) == notes_title
    assert collection.get("aria-labelledby") == heading.get("id")
    assert notes_root.find("xhtml:head/xhtml:title", XHTML_NS).text == notes_title
    landmarks = [
        node for node in notes_root.iter() if node.get("role") == "doc-endnotes"
    ]
    assert len(landmarks) == 1
    nav = _nav(path)
    toc = nav.find(".//xhtml:nav[@epub:type='toc']", XHTML_NS)
    assert any(
        link.get("href") == f"{notes_file.split('/')[-1]}#{heading.get('id')}"
        and "".join(link.itertext()) == notes_title
        for link in toc.findall(".//xhtml:a", XHTML_NS)
    )
    landmark = nav.find(
        ".//xhtml:nav[@epub:type='landmarks']/xhtml:ol/xhtml:li/xhtml:a[@epub:type='endnotes']",
        XHTML_NS,
    )
    assert landmark is not None and "".join(landmark.itertext()) == notes_title
    assert validate_epub_structure(str(path)) == (True, None)


def test_heading_citations_stay_live_while_labels_and_subheadings_remain_exact(
    tmp_path: Path,
):
    source = (
        '<main><h2 id="chapter">A<sup>2</sup> perspective '
        '<sup><a id="first-ref" href="#note" epub:type="noteref" '
        'role="doc-noteref" data-footnote-label="450">450</a></sup></h2>'
        '<h3 id="first-topic">First topic</h3><p>First paragraph.</p>'
        '<h3 id="second-topic">Second topic</h3><p>Second paragraph '
        '<a id="second-ref" href="#note" epub:type="noteref" '
        'role="doc-noteref" data-footnote-label="450">450</a>.</p></main>'
        '<section id="notes" epub:type="endnotes" role="doc-endnotes">'
        '<aside id="note" epub:type="endnote" role="doc-endnote" '
        'data-footnote-label="450">Exact note body.</aside></section>'
    )
    _, path, document = _build(
        tmp_path, source, options={"chapter_level": 2, "group_endnotes": False}
    )
    assert document.output_html == source
    contents = _contents(path)
    chapter_root = next(
        root for _, root in contents if root.find(".//*[@id='chapter']") is not None
    )
    heading = chapter_root.find(".//*[@id='chapter']")
    assert heading.find(".//*[@id='first-ref']") is not None
    assert [
        "".join(sup.itertext()) for sup in heading.findall("xhtml:sup", XHTML_NS)
    ] == ["2", "450"]
    assert (
        chapter_root.find("xhtml:head/xhtml:title", XHTML_NS).text == "A 2 perspective"
    )
    toc = _nav(path).find(".//xhtml:nav[@epub:type='toc']", XHTML_NS)
    labels = {
        str(link.get("href")).split("#")[-1]: "".join(link.itertext())
        for link in toc.findall(".//xhtml:a", XHTML_NS)
    }
    assert {key: labels[key] for key in ("chapter", "first-topic", "second-topic")} == {
        "chapter": "A 2 perspective",
        "first-topic": "First topic",
        "second-topic": "Second topic",
    }
    returns = [
        link
        for _, root in contents
        for link in root.iter(f"{{{XHTML_NS['xhtml']}}}a")
        if link.get(f"{{{XHTML_NS['epub']}}}type") == "backlink"
    ]
    assert [link.get("aria-label") for link in returns] == [
        "Return to reference 450 (1)",
        "Return to reference 450 (2)",
    ]
    _assert_internal_targets_resolve(contents)
    assert validate_epub_structure(str(path)) == (True, None)
    _, legacy_path, _ = _build(tmp_path / "legacy", source, options=None)
    legacy_toc = _nav(legacy_path).find(".//xhtml:nav[@epub:type='toc']", XHTML_NS)
    legacy_chapter_label = next(
        "".join(link.itertext())
        for link in legacy_toc.findall(".//xhtml:a", XHTML_NS)
        if str(link.get("href")).endswith("#chapter")
    )
    assert legacy_chapter_label == "A 2 perspective 450"


def test_global_restarted_notes_keep_order_labels_ids_citations_and_all_backlinks(
    tmp_path: Path,
):
    html = _restarted_notes_html()
    arrangement = _note_arrangement()
    packager, path, document = _build(
        tmp_path,
        html,
        options={"chapter_level": 2},
        note_arrangement=arrangement,
        title="Restarted notes",
    )

    assert document.output_html == html
    contents = _contents(path)
    ids = _all_ids(contents)
    assert len(ids) == len(set(ids))
    assert {"ref-1", "ref-2", "note-1", "note-2", "collected-notes"} <= set(ids)
    assert ids.index("body-2") < ids.index("note-1") < ids.index("note-2")
    refs = [
        node
        for _, root in contents
        for node in root.iter(f"{{{XHTML_NS['xhtml']}}}a")
        if node.get(f"{{{XHTML_NS['epub']}}}type") == "noteref"
    ]
    backlinks = [
        node
        for _, root in contents
        for node in root.iter(f"{{{XHTML_NS['xhtml']}}}a")
        if node.get(f"{{{XHTML_NS['epub']}}}type") == "backlink"
    ]
    assert len(refs) == 2
    assert len(backlinks) == 2
    assert [" ".join(node.itertext()).strip() for node in refs] == ["[1]", "[1]"]
    markers = [
        node
        for _, root in contents
        for node in root.iter(f"{{{XHTML_NS['xhtml']}}}strong")
        if "footnote-marker" in str(node.get("class") or "")
    ]
    assert [" ".join(node.itertext()).strip() for node in markers] == ["[1]", "[1]"]
    for backlink in backlinks:
        assert backlink.get("href", "").split("#")[-1] in {"ref-1", "ref-2"}
    citation = next(
        node
        for _, root in contents
        for node in root.iter(f"{{{XHTML_NS['xhtml']}}}blockquote")
    )
    assert citation.get("cite", "").endswith("#body-1")
    assert "Quoted source." in " ".join(citation.itertext())
    _assert_internal_targets_resolve(contents)

    group_headings = [
        node
        for _, root in contents
        for node in root.iter()
        if "epub-note-group" in str(node.get("class") or "")
    ]
    assert len(group_headings) == 2
    navigation = _nav(path)
    toc = navigation.find(".//xhtml:nav[@epub:type='toc']", XHTML_NS)
    assert toc is not None
    toc_fragments = {
        str(anchor.get("href") or "").split("#")[-1]
        for anchor in toc.findall(".//xhtml:a", XHTML_NS)
    }
    assert "None" not in toc_fragments
    assert toc_fragments <= {
        "publication-title",
        "chapter-1",
        "chapter-2",
        "notes-heading",
    }
    assert {str(node.get("id")) for node in group_headings}.isdisjoint(toc_fragments)
    assert packager.quality_report()["counts"]["backlinks"] == 2


@pytest.mark.parametrize("placement", ["chapter", "source"])
def test_document_numbering_keeps_chapter_ownership_in_preflight_and_export(
    tmp_path: Path, placement: str
):
    source = (
        '<main><h2 id="part">Part One</h2><h3 id="one">Chapter One</h3>'
        '<p>First body <a id="ref-one" href="#note-one" epub:type="noteref" '
        'data-footnote-scope="printed-sequence" data-footnote-label="391">391</a>.</p>'
        '<h3 id="two">Chapter Two</h3><p>Second body '
        '<a id="ref-two" href="#note-two" epub:type="noteref" '
        'data-footnote-scope="printed-sequence" data-footnote-label="392">392</a>.</p></main>'
        '<section id="notes" epub:type="endnotes"><ol>'
        '<li id="note-one" epub:type="endnote" data-footnote-scope="printed-sequence" '
        'data-footnote-label="391">First exact note.</li>'
        '<li id="note-two" epub:type="endnote" data-footnote-scope="printed-sequence" '
        'data-footnote-label="392">Second exact note.</li></ol></section>'
    )
    arrangement = _note_arrangement(numbering="document", scope_heading_level=None)
    options = {"chapter_level": 3, "note_placement": placement}
    preflight = preflight_epub(
        source, {"language": "en", "note_arrangement": arrangement}, options
    )
    packager, path, document = _build(
        tmp_path, source, options=options, note_arrangement=arrangement
    )
    assert document.output_html == source
    report = packager.quality_report()
    assert preflight["quality"]["counts"] == report["counts"]
    assert not any(
        issue["code"] == "note-ownership-needs-review" for issue in report["issues"]
    )
    contents = _contents(path)
    identifiers = _all_ids(contents)
    if placement == "chapter":
        assert (
            identifiers.index("one")
            < identifiers.index("note-one")
            < identifiers.index("two")
        )
        assert identifiers.index("two") < identifiers.index("note-two")
        assert "notes" not in identifiers
    else:
        assert (
            identifiers.index("two")
            < identifiers.index("note-one")
            < identifiers.index("note-two")
        )
        groups = [
            node
            for _, root in contents
            for node in root.iter()
            if "epub-note-group" in str(node.get("class") or "")
        ]
        assert ["".join(node.itertext()) for node in groups] == [
            "Notes — Chapter One",
            "Notes — Chapter Two",
        ]
    for note_id, reference_id, label in (
        ("note-one", "ref-one", "391"),
        ("note-two", "ref-two", "392"),
    ):
        note = next(
            root.find(f".//*[@id='{note_id}']")
            for _, root in contents
            if root.find(f".//*[@id='{note_id}']") is not None
        )
        assert note.get("data-footnote-label") == label
        backlink = next(
            node
            for node in note.iter(f"{{{XHTML_NS['xhtml']}}}a")
            if node.get("href", "").endswith(f"#{reference_id}")
        )
        assert backlink.get("aria-label") == f"Return to reference {label}"
    _assert_internal_targets_resolve(contents)
    assert validate_epub_structure(str(path)) == (True, None)


@pytest.mark.parametrize(
    "options,arrangement",
    [
        ({"chapter_level": 2, "note_placement": "chapter"}, _note_arrangement()),
        ({"chapter_level": 2}, _note_arrangement(placement="chapter_endnotes")),
    ],
)
def test_explicit_or_verified_chapter_placement_moves_only_owned_notes_and_keeps_targets(
    tmp_path: Path,
    options: dict[str, object],
    arrangement: dict[str, object],
):
    _, path, _ = _build(
        tmp_path,
        _restarted_notes_html(),
        options=options,
        note_arrangement=arrangement,
        title="Restarted notes",
    )
    contents = _contents(path)
    ids = _all_ids(contents)
    assert "collected-notes" not in ids
    assert ids.index("chapter-1") < ids.index("note-1") < ids.index("chapter-2")
    assert ids.index("chapter-2") < ids.index("note-2")
    assert {"ref-1", "ref-2", "note-1", "note-2", "body-1", "body-2"} <= set(ids)
    _assert_internal_targets_resolve(contents)
    backlinks = [
        node
        for _, root in contents
        for node in root.iter(f"{{{XHTML_NS['xhtml']}}}a")
        if node.get(f"{{{XHTML_NS['epub']}}}type") == "backlink"
    ]
    assert len(backlinks) == 2


def test_unknown_note_ownership_remains_collected_and_is_reported_for_review(
    tmp_path: Path,
):
    _, path, _ = _build(
        tmp_path,
        _restarted_notes_html(cross_chapter_reference=True),
        options={"chapter_level": 2, "note_placement": "chapter"},
        note_arrangement=_note_arrangement(),
        title="Restarted notes",
    )
    contents = _contents(path)
    ids = _all_ids(contents)
    assert "collected-notes" in ids
    assert ids.index("chapter-2") < ids.index("note-1")
    report_packager, _, _ = _build(
        tmp_path / "report",
        _restarted_notes_html(cross_chapter_reference=True),
        options={"chapter_level": 2, "note_placement": "chapter"},
        note_arrangement=_note_arrangement(),
        title="Restarted notes",
    )
    report = report_packager.quality_report()
    assert report is not None
    assert any(
        issue["code"] == "note-ownership-needs-review" for issue in report["issues"]
    )


def test_reviewed_endnotes_heading_moves_collection_intact_and_keeps_backcover_prose(
    tmp_path: Path,
):
    html = (
        '<main><h2 id="chapter" epub:type="chapter">Chapter</h2>'
        '<p id="chapter-body">Chapter body.</p>'
        '<div id="back-cover"><h2 id="endnotes-heading">Endnotes</h2>'
        '<p id="back-cover-prose">Back-cover prose remains in place.</p></div>'
        '</main><section id="protected-notes" role="doc-endnotes" epub:type="endnotes">'
        '<ol><li id="note-1" role="doc-endnote" epub:type="endnote">'
        "Retained note body.</li></ol></section>"
    )
    _, path, document = _build(
        tmp_path,
        html,
        options={"roles": {"endnotes-heading": "endnotes"}},
    )
    assert document.output_html == html
    contents = _contents(path)
    root = next(
        root for _, root in contents if root.find(".//*[@id='back-cover']") is not None
    )
    back_cover = root.find(".//*[@id='back-cover']")
    assert back_cover is not None
    children = [child for child in list(back_cover) if isinstance(child.tag, str)]
    heading_index = next(
        index
        for index, node in enumerate(children)
        if node.get("id") == "endnotes-heading"
    )
    assert children[heading_index + 1].get("id") == "protected-notes"
    assert children[heading_index + 2].get("id") == "back-cover-prose"
    assert "Retained note body." in " ".join(root.itertext())
    assert "Back-cover prose remains in place." in " ".join(root.itertext())
    assert "protected-notes" in _all_ids(contents)


def test_large_chunk_continuation_links_resolve_in_shared_epub_validator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(epub_packager, "MAX_CONTENT_DOCUMENT_BYTES", 380)
    long_paragraphs = "".join(
        f'<p id="paragraph-{index}">{"Synthetic text. " * 12}</p>' for index in range(4)
    )
    html = (
        '<main><h1 id="title">Synthetic publication</h1>'
        '<h2 id="chapter">Chapter</h2><p id="opening">'
        '<a id="forward" href="#target">Continue to the end.</a></p>'
        f'{long_paragraphs}<p id="target">Final target.</p></main>'
    )
    _, path, _ = _build(tmp_path, html, options={"chapter_level": 2})

    assert len(_contents(path)) >= 3
    passed, message = validate_epub_structure(str(path))
    assert passed, message
    contents = _contents(path)
    _assert_internal_targets_resolve(contents)
    forward = next(
        node
        for _, root in contents
        for node in root.iter(f"{{{XHTML_NS['xhtml']}}}a")
        if node.get("id") == "forward"
    )
    assert forward.get("href", "").endswith("#target")
    assert not forward.get("href", "").startswith("#")
    assert Path(forward.get("href", "").split("#", 1)[0]).name.endswith(".xhtml")


@pytest.mark.parametrize(
    "html,options",
    [
        (
            '<main><h2 id="chapter">Chapter</h2><p>Text.</p></main>',
            {"roles": {"missing-anchor": "chapter"}},
        ),
        (
            '<main><h2 id="chapter">Chapter</h2><p><a href="#missing" role="doc-noteref">1</a></p></main>',
            {},
        ),
        ('<main><p id="duplicate">One</p><p id="duplicate">Two</p></main>', {}),
    ],
)
def test_stale_options_and_malformed_source_relationships_fail_closed(
    tmp_path: Path, html: str, options: dict[str, object]
):
    document = _document(tmp_path, html)
    output = tmp_path / "invalid.epub"
    with Flask(__name__).app_context():
        packager = EPUBPackager(document, str(output), options=options)
        with pytest.raises(ValueError):
            packager.build()


def test_preflight_is_source_free_non_io_and_keeps_ids_options_and_not_run_checks():
    html = (
        '<html lang="pl"><main><h2 id="chapter-one">Chapter One</h2>'
        '<p>Body.</p><img id="image-1" src="/synthetic/nonexistent/image.png" alt="Source words">'
        "</main></html>"
    )
    original = html
    options = {
        "chapter_level": 2,
        "image_descriptions": {"image-1": "Reviewed description."},
    }

    result = preflight_epub(html, {"title": "Synthetic", "language": "en"}, options)

    assert html == original
    assert result["options"] == normalize_epub_options(options)
    assert (
        result["outline"]["input_sha256"] == hashlib.sha256(html.encode()).hexdigest()
    )
    assert result["outline"]["headings"][0]["id"] == "chapter-one"
    assert result["outline"]["images"][0]["id"] == "image-1"
    assert (
        result["quality"]["input_sha256"] == hashlib.sha256(html.encode()).hexdigest()
    )
    assert result["quality"]["checks"] == {
        "structure": "not_run",
        "epubcheck": "not_run",
        "ace": "not_run",
        "manual": "not_reviewed",
    }


def test_quality_report_binds_actual_validation_raw_input_options_and_stable_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    html = (
        '<main><h1 id="title">Synthetic publication</h1>'
        '<h2 id="chapter">Chapter</h2><p id="body">Stable words.</p></main>'
    )
    normalized_options = normalize_epub_options({"chapter_level": 2})
    monkeypatch.setattr(
        epub_packager,
        "datetime",
        SimpleNamespace(now=lambda timezone: datetime(2026, 1, 1, tzinfo=timezone)),
    )
    first, first_path, first_document = _build(
        tmp_path / "first",
        html,
        options={"chapter_level": 2},
        document_id=None,
    )
    passed, message = validate_epub_structure(str(first_path))
    assert passed, message
    first_report = first.quality_report(structure="passed", epubcheck="not_run")
    assert first_report is not None
    assert first_report["input_sha256"] == hashlib.sha256(html.encode()).hexdigest()
    assert first_report["options_sha256"] == epub_value_sha256(normalized_options)
    assert (
        first_report["artifact_sha256"]
        == hashlib.sha256(first_path.read_bytes()).hexdigest()
    )
    assert first_report["checks"]["structure"] == "passed"
    assert first_report["checks"]["epubcheck"] == "not_run"
    assert first_document.output_html == html

    monkeypatch.setattr(
        epub_packager,
        "datetime",
        SimpleNamespace(now=lambda timezone: datetime(2027, 1, 1, tzinfo=timezone)),
    )
    second, second_path, second_document = _build(
        tmp_path / "second",
        html,
        options={"chapter_level": 2},
        document_id=None,
    )
    second_report = second.quality_report(structure="passed", epubcheck="not_run")
    assert second_report is not None
    assert first_report["content_sha256"] == second_report["content_sha256"]
    assert first_report["artifact_sha256"] != second_report["artifact_sha256"]
    assert second_document.output_html == html
    with (
        zipfile.ZipFile(first_path) as first_archive,
        zipfile.ZipFile(second_path) as second_archive,
    ):
        assert first_archive.read("EPUB/package.opf") != second_archive.read(
            "EPUB/package.opf"
        )


def _evaluation_for(
    content_sha256: str, options: dict[str, object]
) -> dict[str, object]:
    return {
        "content_sha256": content_sha256,
        "options_sha256": epub_projection_options_sha256(
            normalize_epub_options(options)
        ),
        "evaluator": "Independent synthetic reviewer",
        "summary": "Bounded fixture review only.",
        "sufficient_modes": ["textual"],
        "conforms_to": "EPUB Accessibility 1.1 - WCAG 2.2 Level AA",
        "credential": None,
        "hazards": ["none"],
    }


def _metadata_values(package: ET.Element) -> dict[str, list[str]]:
    metadata = package.find("opf:metadata", OPF_NS)
    assert metadata is not None
    values: dict[str, list[str]] = {}
    for node in metadata.findall("opf:meta", OPF_NS):
        values.setdefault(str(node.get("property")), []).append(node.text or "")
    return values


def test_matching_accessibility_evaluation_emits_primitive_edition_metadata(
    tmp_path: Path,
):
    html = (
        '<main><h1 id="title">Synthetic publication</h1>'
        '<h2 id="chapter">Chapter</h2><p id="body">Stable words.</p></main>'
    )
    projection_options = {"chapter_level": 2}
    baseline, _, _ = _build(tmp_path / "baseline", html, options=projection_options)
    baseline_report = baseline.quality_report()
    assert baseline_report is not None
    evaluation = _evaluation_for(baseline_report["content_sha256"], projection_options)
    evaluated, evaluated_path, _ = _build(
        tmp_path / "evaluated",
        html,
        options={**projection_options, "accessibility_evaluation": evaluation},
    )

    values = _metadata_values(_package(evaluated_path))
    assert values["schema:accessModeSufficient"] == ["textual"]
    assert values["dcterms:conformsTo"] == [
        "EPUB Accessibility 1.1 - WCAG 2.2 Level AA"
    ]
    assert values["a11y:certifiedBy"] == ["Independent synthetic reviewer"]
    metadata = _package(evaluated_path).find("opf:metadata", OPF_NS)
    assert metadata is not None
    certification = metadata.find("opf:meta[@property='a11y:certifiedBy']", OPF_NS)
    assert certification is not None
    assert certification.get("refines") == "#accessibility-conformance"
    report = evaluated.quality_report()
    assert report is not None
    assert report["checks"]["manual"] == "reviewed"
    assert report["checks"]["epubcheck"] == "not_run"
    assert report["checks"]["ace"] == "not_run"


@pytest.mark.parametrize("stale_dimension", ["content", "options"])
def test_stale_accessibility_evaluation_omits_claims_and_reports_staleness(
    tmp_path: Path, stale_dimension: str
):
    original_html = (
        '<main><h1 id="title">Synthetic publication</h1>'
        '<h2 id="chapter">Chapter</h2><p id="body">Stable words.</p></main>'
    )
    original_options = {"chapter_level": 2}
    baseline, _, _ = _build(
        tmp_path / "baseline", original_html, options=original_options
    )
    baseline_report = baseline.quality_report()
    assert baseline_report is not None
    evaluation = _evaluation_for(baseline_report["content_sha256"], original_options)
    if stale_dimension == "content":
        changed_html = original_html.replace("Stable words.", "Edited words.")
        changed_options = original_options
    else:
        changed_html = original_html
        changed_options = {**original_options, "group_endnotes": False}

    packager, path, _ = _build(
        tmp_path / "stale",
        changed_html,
        options={**changed_options, "accessibility_evaluation": evaluation},
    )
    values = _metadata_values(_package(path))
    assert "schema:accessModeSufficient" not in values
    assert "dcterms:conformsTo" not in values
    assert "a11y:certifiedBy" not in values
    report = packager.quality_report()
    assert report is not None
    assert any(
        issue["code"] == "accessibility-evaluation-stale" for issue in report["issues"]
    )
