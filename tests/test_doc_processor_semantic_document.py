from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest
from bs4 import BeautifulSoup

from carlina.core.semantic_html import SEMANTIC_HTML_CONTRACT_VERSION
from iarf_assistant.addons.doc_processor.docx_preprocessor import (
    preprocess_html_for_docx,
)
from iarf_assistant.addons.doc_processor.epub_packager import validate_semantic_export
from iarf_assistant.addons.doc_processor.parsers import pdf_utils
from iarf_assistant.addons.doc_processor.semantic_document import (
    SemanticDocumentError,
    canonicalize_document_html,
    namespace_semantic_fragment,
    semantic_result_for_stored_html,
    semantic_settings,
)
from iarf_assistant.addons.translator.rendering import _sanitize_document_html
from iarf_assistant.addons.translator.segmenter import DomSegmenter

RICH_DOCUMENT = """<!doctype html>
<html lang="en" xml:lang="en"><head><meta charset="utf-8"><title>Book</title></head>
<body><main lang="en" xml:lang="en"><section><h1 id="chapter">Chapter</h1>
<section><h2>Nested heading</h2><ol start="3" reversed="reversed" type="I"><li value="3">One {{template_value}}</li></ol>
<blockquote cite="https://example.test/quote">A quote.</blockquote>
<figure><img alt="Diagram" src="data:image/png;base64,aGVsbG8="/><figcaption>Figure one.</figcaption></figure>
<table summary="A table"><colgroup span="2"><col span="1"/></colgroup><thead><tr><th abbr="Head" scope="col" id="head">Head</th></tr></thead><tbody><tr><td headers="head">Cell <code>x</code> <kbd>K</kbd> <time datetime="2026-01-01">today</time></td></tr></tbody></table>
<p><a href="#chapter">Back</a> <a href="#note-1" role="doc-noteref">1</a> <a href="#note-1" role="doc-noteref">1</a></p>
</section><aside id="note-1" role="doc-endnote">A note body.</aside><aside id="orphan" role="doc-endnote">An unlinked note body.</aside></section></main></body></html>"""


@pytest.mark.parametrize("text", [
    "Zażółć gęślą jaźń — café naïve piñata", "café cafe\u0301 æ œ ß ſ ﬀ",
    "ἄνθρωπος Ελληνικά", "Українська русский", "שָׁלוֹם", "العَرَبِيَّة",
    "日本語 中文 한국어", "a\u00a0b – — ‘quoted’ № § ¹²³",
])
def test_unicode_survives_page_normalization_assembly_and_canonical_storage(text):
    from html import escape

    page = pdf_utils.normalize_extracted_page_html(
        f'<main_content><p id="unicode-text">{escape(text)}</p></main_content><footnotes></footnotes>'
    )
    assembled = pdf_utils.concatenate_html_pages([page])
    result = canonicalize_document_html(assembled, user_id=1)
    assert not result.errors
    stored_bytes = result.html.encode("utf-8")
    soup = BeautifulSoup(stored_bytes.decode("utf-8"), "html.parser")
    assert soup.find(id="unicode-text").get_text() == text
    assert canonicalize_document_html(result.html, user_id=1).html == result.html


def test_mermaid_contract_survives_canonicalization_and_project_namespacing():
    from carlina.core.semantic_diagrams import materialize_mermaid
    from carlina.core.semantic_export import validate_semantic_export_html

    source = ('<figure class="mermaid-diagram-v1" id="diagram-p1-1">'
              '<pre class="mermaid-template"><code>flowchart TD\nn1["{{a}}"]</code></pre>'
              '<div class="mermaid-labels"><p id="diagram-p1-1--a">A &amp; B</p></div></figure>')
    result = canonicalize_document_html(source, user_id=1)
    assert not result.diagnostics
    assert canonicalize_document_html(result.html, user_id=1).html == result.html
    soup = BeautifulSoup(result.html, "html.parser")
    code = materialize_mermaid(soup.figure)
    namespace_semantic_fragment(soup, namespace="book-2")
    assert materialize_mermaid(soup.figure) == code
    validate_semantic_export(soup, export_name="test")
    validate_semantic_export_html(str(soup))

    broken = str(soup).replace('{{a}}', '{{missing}}')
    assert canonicalize_document_html(broken, user_id=1).errors
    with pytest.raises(ValueError, match="diagram"):
        validate_semantic_export_html(broken)


@pytest.mark.parametrize("prefix", ["project-doc-2", "p" * 128])
def test_mermaid_namespacing_shortens_the_whole_id_family(prefix):
    from carlina.core.semantic_diagrams import materialize_mermaid

    figure_id = "diagram" + "x" * 103
    key = "label_key_12"
    source = (f'<figure class="mermaid-diagram-v1" id="{figure_id}">'
              f'<pre class="mermaid-template"><code>flowchart TD\nn1["{{{{{key}}}}}"]</code></pre>'
              f'<div class="mermaid-labels"><p id="{figure_id}--{key}">Label</p></div></figure>')
    soup = BeautifulSoup(source, "html.parser")
    original = materialize_mermaid(soup.figure)
    namespace_semantic_fragment(soup, namespace=prefix)
    assert materialize_mermaid(soup.figure) == original
    assert all(len(tag["id"]) <= 128 for tag in soup.find_all(id=True))
    assert not canonicalize_document_html(str(soup), user_id=1).errors


def test_rich_document_contract_is_idempotent_and_loss_conscious():
    first = canonicalize_document_html(RICH_DOCUMENT, user_id=1)
    second = canonicalize_document_html(first.html, user_id=1)

    assert first.html == second.html
    assert first.contract_version == SEMANTIC_HTML_CONTRACT_VERSION
    assert not first.errors
    assert {item["code"] for item in first.diagnostics} == {"unlinked_note_body"}
    soup = BeautifulSoup(first.html, "html.parser")
    assert soup.html["lang"] == "en"
    assert soup.html["xml:lang"] == "en"
    assert soup.main["lang"] == "en"
    assert soup.h2["id"] == "nested-heading"
    assert soup.ol["start"] == "3"
    assert soup.ol.has_attr("reversed")
    assert soup.blockquote["cite"] == "https://example.test/quote"
    assert soup.th["abbr"] == "Head"
    assert "{{template_value}}" in first.html
    validate_semantic_export(soup, export_name="test")


def test_title_subtitle_boundary_is_canonical_and_idempotent():
    source = '<main><h1>Book title<span class="subtitle">Part I</span></h1></main>'

    first = canonicalize_document_html(source, user_id=1)
    second = canonicalize_document_html(first.html, user_id=1)

    assert first.html == second.html
    assert 'Book title\n<span class="subtitle">Part I</span>' in first.html
    assert BeautifulSoup(first.html, "html.parser").h1.get_text(" ", strip=True) == (
        "Book title Part I"
    )


def test_contract_fails_closed_for_bad_references_and_removes_active_content():
    result = canonicalize_document_html(
        '<main><p onclick="x()"><a href="javascript:alert(1)">x</a>'
        '<a href="#missing" role="doc-noteref">1</a></p><script>boom()</script></main>',
        user_id=1,
    )

    assert result.errors
    assert "script" not in result.html
    assert "onclick" not in result.html
    assert "javascript:" not in result.html
    assert any(
        item["code"] == "noteref_target_not_endnote"
        or item["code"] == "unresolved_fragment_reference"
        for item in result.errors
    )


def test_project_composition_namespaces_ids_and_all_supported_local_references():
    soup = BeautifulSoup(
        '<main aria-labelledby="chapter description"><h1 id="chapter">Chapter</h1>'
        '<p id="description">Description.</p><a id="ref" href="#note">1</a>'
        '<blockquote cite="#description">Quote.</blockquote>'
        '<table><tr><th id="head">Head</th><td headers="head">Cell</td></tr></table>'
        '<aside id="note" aria-describedby="description">Note.</aside></main>',
        "html.parser",
    )

    mapping = namespace_semantic_fragment(soup, namespace="project-doc-2")

    assert mapping["chapter"] == "project-doc-2-chapter"
    assert soup.main["aria-labelledby"] == (
        "project-doc-2-chapter project-doc-2-description"
    )
    assert soup.a["href"] == "#project-doc-2-note"
    assert soup.blockquote["cite"] == "#project-doc-2-description"
    assert soup.td["headers"] == "project-doc-2-head"
    assert soup.aside["aria-describedby"] == "project-doc-2-description"
    assert len({tag["id"] for tag in soup.find_all(id=True)}) == 5


def test_contract_never_promotes_escaped_text_to_live_markup():
    result = canonicalize_document_html(
        "<main><p>&lt;script&gt;alert(1)&lt;/script&gt;&amp;&nbsp;</p></main>",
        user_id=1,
    )

    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in result.html
    assert "&amp;" in result.html
    assert "&nbsp;" in result.html
    reparsed = BeautifulSoup(result.html, "html.parser")
    assert reparsed.find("script") is None
    assert reparsed.p.get_text() == "<script>alert(1)</script>&\xa0"


def test_unicode_serialization_preserves_text_attributes_and_stored_identity():
    source = (
        '<main><h1 id="chapter">Свобода αβ</h1>'
        '<p title="&quot;&gt;&lt;script&gt;">Личность &amp; дух&nbsp;'
        '&lt;img src=x onerror=alert(1)&gt;</p><a href="#chapter">Назад</a></main>'
    )
    result = canonicalize_document_html(source, user_id=1)
    assert not result.errors
    assert "Свобода αβ" in result.html
    assert "Личность &amp; дух&nbsp;" in result.html
    assert "&scy;" not in result.html
    soup = BeautifulSoup(result.html, "html.parser")
    assert soup.find(["script", "img"]) is None
    assert soup.p["title"] == '\"><script>'
    assert soup.a["href"] == "#chapter"
    assert canonicalize_document_html(result.html, user_id=1).html == result.html

    # Previously saved entity-heavy documents remain valid and byte-identical.
    old_html = soup.decode(formatter="html")
    old_settings = semantic_settings({}, result)
    old_settings["semantic_content_hash"] = hashlib.sha256(old_html.encode()).hexdigest()
    assert semantic_result_for_stored_html(old_html, old_settings).html == old_html


def test_contract_removes_nested_active_trees_without_processing_detached_children():
    result = canonicalize_document_html(
        "<svg><g><script>alert(1)</script><text>embedded</text></g></svg><p>safe</p>",
        user_id=1,
    )

    assert result.html == "<p>safe</p>"
    assert result.diagnostics == (
        {"level": "warning", "code": "active_or_embedded_tag_removed"},
    )


def test_contract_rejects_empty_noteref_labels_and_referenced_note_bodies():
    result = canonicalize_document_html(
        '<p><a href="#note" role="doc-noteref"></a></p>'
        '<aside id="note" role="doc-endnote"></aside>',
        user_id=1,
    )

    assert {item["code"] for item in result.errors} == {
        "noteref_missing_visible_label",
        "noteref_target_empty",
    }


def test_contract_preserves_valid_source_toc_and_rejects_malformed_or_linked_variants():
    valid = canonicalize_document_html(
        '<nav class="source-toc"><div class="toc-entry toc-depth-1">'
        '<span class="toc-entry-title">Private title</span> '
        '<span class="toc-page-label">xii</span></div></nav>',
        user_id=1,
    )
    invalid = canonicalize_document_html(
        '<nav class="source-toc"><div class="toc-entry">'
        '<a href="#target">Private title</a></div></nav><h2 id="target">Target</h2>',
        user_id=1,
    )

    assert not valid.errors
    assert 'class="source-toc"' in valid.html
    assert 'class="toc-page-label"' in valid.html
    assert {item["code"] for item in invalid.errors} >= {
        "source_toc_contains_link",
        "source_toc_invalid_depth",
        "source_toc_missing_title",
        "source_toc_unexpected_entry_child",
    }


def test_stored_current_contract_is_hash_verified_and_keeps_diagnostics():
    result = canonicalize_document_html(
        "<main><h1>Book</h1><aside id=orphan role=doc-endnote>Note</aside></main>",
        user_id=1,
    )
    settings = semantic_settings({}, result)

    verified = semantic_result_for_stored_html(result.html, settings)
    assert verified.html == result.html
    assert verified.content_hash == result.content_hash
    assert verified.diagnostics == result.diagnostics

    with pytest.raises(SemanticDocumentError, match="content hash"):
        semantic_result_for_stored_html(result.html + "<!-- changed -->", settings)


def test_current_contract_without_hash_fails_closed():
    with pytest.raises(SemanticDocumentError, match="missing a valid content hash"):
        semantic_result_for_stored_html(
            "<p>Book</p>",
            {
                "semantic_contract_version": SEMANTIC_HTML_CONTRACT_VERSION,
                "semantic_contract_mode": "completed",
            },
        )


def test_contract_canonicalizes_only_owner_scoped_images(tmp_path):
    owner_dir = tmp_path / "1" / "book"
    owner_dir.mkdir(parents=True)
    source = owner_dir / "source.html"
    source.write_text("source", encoding="utf-8")
    (owner_dir / "figure.png").write_bytes(b"png")
    result = canonicalize_document_html(
        '<p><img src="figure.png"><img src="https://example.test/a.png"><img src="/uploads/2/no.png"></p>',
        user_id=1,
        source_file_path="/uploads/1/book/source.html",
        uploads_dir=tmp_path,
    )

    soup = BeautifulSoup(result.html, "html.parser")
    images = soup.find_all("img")
    assert images[0]["src"] == "/uploads/1/book/figure.png"
    assert not images[1].has_attr("src")
    assert not images[2].has_attr("src")


def test_contract_preserves_single_encoded_owner_filename_and_is_idempotent(tmp_path):
    owner_dir = tmp_path / "1" / "book"
    owner_dir.mkdir(parents=True)
    image = owner_dir / "figure one-é.png"
    image.write_bytes(b"synthetic image")
    result = canonicalize_document_html(
        '<p><img src="figure%20one-%C3%A9.png" alt="Diagram"></p>',
        user_id=1,
        source_file_path="/uploads/1/book/source.html",
        uploads_dir=tmp_path,
    )
    assert BeautifulSoup(result.html, "html.parser").img["src"] == (
        "/uploads/1/book/figure one-é.png"
    )
    assert not result.diagnostics
    repeated = canonicalize_document_html(
        result.html,
        user_id=1,
        source_file_path="/uploads/1/book/source.html",
        uploads_dir=tmp_path,
    )
    assert repeated.html == result.html
    assert not repeated.diagnostics


@pytest.mark.parametrize(
    "source",
    [
        "figure%2fone.png",
        "figure%5cone.png",
        "figure%3fone.png",
        "figure%23one.png",
        "figure%25one.png",
        "figure%3aone.png",
        "figure%00one.png",
        "figure%0aone.png",
        "%2e%2e/figure.png",
        "figure%2520one.png",
        "figure%GGone.png",
        "figure%2.png",
        "figure%FF.png",
        "/uploads/2/figure%20one.png",
    ],
)
def test_contract_rejects_ambiguous_or_foreign_encoded_images(tmp_path, source):
    result = canonicalize_document_html(
        f'<p><img src="{source}"></p>',
        user_id=1,
        source_file_path="/uploads/1/book/source.html",
        uploads_dir=tmp_path,
    )
    assert not BeautifulSoup(result.html, "html.parser").img.has_attr("src")


def test_contract_encoded_filename_cannot_follow_a_symlink_to_another_owner(tmp_path):
    owner_dir = tmp_path / "1" / "book"
    other_dir = tmp_path / "2"
    owner_dir.mkdir(parents=True)
    other_dir.mkdir()
    outside = other_dir / "figure one.png"
    outside.write_bytes(b"other owner's image")
    (owner_dir / "figure one.png").symlink_to(outside)
    result = canonicalize_document_html(
        '<p><img src="figure%20one.png"></p>',
        user_id=1,
        source_file_path="/uploads/1/book/source.html",
        uploads_dir=tmp_path,
    )
    assert not BeautifulSoup(result.html, "html.parser").img.has_attr("src")


def test_translator_and_docx_keep_contract_structure():
    result = canonicalize_document_html(RICH_DOCUMENT, user_id=1)
    project = SimpleNamespace(user_id=1)
    rendered = _sanitize_document_html(
        result.html,
        project=project,
        for_export=False,
        uploads_dir=None,
    )
    assert "<colgroup" in rendered
    assert "<blockquote cite=" in rendered
    assert "{{template_value}}" in rendered
    skeleton, segments = DomSegmenter.apply_semantic_segmentation(
        result.html, "project", source_language="en"
    )
    assert skeleton and segments
    assert "{{template_value}}" in result.html
    docx = preprocess_html_for_docx(result.html)
    assert "<colgroup" in docx
    assert 'role="doc-endnote"' in docx


def test_docx_does_not_recast_unrelated_fn_prefixed_ids_as_footnotes():
    source = '<main><h2 id="fnord-chapter">Chapter</h2><p>Body.</p></main>'
    prepared = preprocess_html_for_docx(source)

    soup = BeautifulSoup(prepared, "html.parser")
    assert soup.find(id="fnord-chapter").name == "h2"
    assert soup.find("section", class_="footnotes") is None


def test_docx_accepts_role_only_footnotes_and_preserves_numeric_body():
    source = (
        '<main><p>Text<a id="ref" role="doc-noteref" href="#source-note">1</a>.</p>'
        '<aside id="source-note" role="doc-footnote"><p>3.14159 is an approximation.'
        "</p></aside></main>"
    )
    prepared = preprocess_html_for_docx(source)
    soup = BeautifulSoup(prepared, "html.parser")
    assert soup.select_one(".footnote-ref")["href"] == "#fn1"
    assert soup.find(id="fn1").get_text().startswith("3.14159 is an approximation.")


def test_docx_keeps_ordinary_links_to_renamed_note_targets_and_markers():
    source = (
        '<main><p>Text<a id="marker-first" role="doc-noteref" href="#fn2">1</a>'
        '<a id="marker-second" role="doc-noteref" href="#fn1">2</a>.</p>'
        '<p><a href="#fn2">First note</a> <a href="#fn1">Second note</a></p>'
        '<aside id="fn2" role="doc-footnote"><p>First body '
        '<a href="#marker%2Dfirst">Return to first marker</a> '
        '<a href="#marker-second">Second marker</a> '
        '<a href="#chapter">Chapter</a> '
        '<a href="https://example.test/#marker-first">External link</a>'
        '</p></aside><aside id="fn1" role="doc-footnote"><p>Second body.</p></aside>'
        '<h2 id="chapter">Chapter</h2></main>'
    )

    soup = BeautifulSoup(preprocess_html_for_docx(source), "html.parser")
    refs = soup.select("a.footnote-ref")

    assert [ref["href"] for ref in refs] == ["#fn1", "#fn2"]
    assert soup.find("a", string="First note")["href"] == "#fn1"
    assert soup.find("a", string="Second note")["href"] == "#fn2"
    first_note = soup.find(id="fn1")
    assert first_note.find("a", string="Return to first marker")["href"] == "#fnref1"
    assert first_note.find("a", string="Second marker")["href"] == "#fnref2"
    assert first_note.find("a", string="Chapter")["href"] == "#chapter"
    assert first_note.find("a", string="External link")["href"] == (
        "https://example.test/#marker-first"
    )


def test_docx_removes_target_proven_duplicate_headings_and_all_marker_nodes():
    source = (
        '<main><p>Text <a role="doc-noteref" href="#note-one">[1]</a> '
        '<a role="doc-noteref" href="#note-one">1.</a> '
        '<a role="doc-noteref" href="#note-star">*</a></p>'
        '<section id="note-one" role="doc-footnote">'
        '<h1>(1).</h1><span class="footnote-marker">1</span>'
        '<span class="doc-footnote-marker-item">1</span>'
        '<p>Body <em>with formatting</em> and <a href="#chapter">a link</a>.</p>'
        '</section><section id="note-star" role="doc-footnote">'
        '<h2><span>*</span></h2><p>Star body.</p></section>'
        '<h2 id="chapter">Chapter</h2></main>'
    )

    soup = BeautifulSoup(preprocess_html_for_docx(source), "html.parser")
    notes = soup.select("section.footnotes > ol > li")

    assert len(notes) == 2
    assert notes[0].find(["h1", "h2", "h3", "h4", "h5", "h6"]) is None
    assert notes[1].find(["h1", "h2", "h3", "h4", "h5", "h6"]) is None
    assert notes[0].find(".footnote-marker") is None
    assert notes[0].find(".doc-footnote-marker-item") is None
    assert notes[0].find("em") is not None
    assert notes[0].find("a", href="#chapter") is not None
    refs = soup.select("a.footnote-ref")
    assert len(refs) == 3
    assert refs[0]["href"] == refs[1]["href"]
    assert refs[1]["href"] != refs[2]["href"]


def test_docx_keeps_unproven_headings_and_plain_numeric_note_body():
    source = (
        '<main><p><a role="doc-noteref" href="#date-note">1</a> '
        '<a role="doc-noteref" href="#linked-heading">2</a> '
        '<a role="doc-noteref" href="#title-note">3</a></p>'
        '<section id="date-note" role="doc-footnote">'
        '<h2>2024</h2><p>2024 was a significant year.</p></section>'
        '<section id="linked-heading" role="doc-footnote">'
        '<h2><a href="#chapter">2</a></h2><p>Linked heading remains.</p></section>'
        '<section id="title-note" role="doc-footnote">'
        '<h2>Note on history</h2><p>Substantive title remains.</p></section>'
        '<h2 id="chapter">Chapter</h2></main>'
    )

    soup = BeautifulSoup(preprocess_html_for_docx(source), "html.parser")
    notes = soup.select("section.footnotes > ol > li")
    heading_texts = [
        note.find(["h1", "h2", "h3", "h4", "h5", "h6"]).get_text(
            " ", strip=True
        )
        for note in notes
    ]

    assert heading_texts == [
        "2024",
        "2",
        "Note on history",
    ]
    assert "2024 was a significant year." in notes[0].get_text(" ", strip=True)


def test_docx_only_removes_first_marker_heading_before_note_content():
    source = (
        '<main><p><a role="doc-noteref" href="#body-first">1</a> '
        '<a role="doc-noteref" href="#wrapper-first">1</a> '
        '<a role="doc-noteref" href="#image-first">1</a></p>'
        '<aside id="body-first" role="doc-footnote">'
        '<p>Body first.</p><h2>1</h2><h3>1</h3><p>Body after.</p></aside>'
        '<aside id="wrapper-first" role="doc-footnote">'
        '<div><section><h2>1</h2></section></div><p>Wrapped body.</p></aside>'
        '<aside id="image-first" role="doc-footnote">'
        '<img src="data:image/png;base64,aGVsbG8="/><h2>1</h2>'
        '<p>Image body.</p></aside></main>'
    )

    soup = BeautifulSoup(preprocess_html_for_docx(source), "html.parser")
    notes = soup.select("section.footnotes > ol > li")

    assert notes[0].find("h2").get_text(strip=True) == "1"
    assert notes[0].find("h3").get_text(strip=True) == "1"
    assert notes[1].find("h2") is None
    assert notes[2].find("h2").get_text(strip=True) == "1"


def test_docx_preserves_unmarked_alphabetic_heading_and_removes_alpha_markers():
    source = (
        '<main><p><a role="doc-noteref" href="#alpha-note">A</a></p>'
        '<aside id="alpha-note" role="doc-footnote">'
        '<h2>A</h2><span class="footnote-marker">A</span>'
        '<span class="doc-footnote-marker-item">A</span>'
        '<p>Alpha body.</p></aside></main>'
    )

    soup = BeautifulSoup(preprocess_html_for_docx(source), "html.parser")
    note = soup.select_one("section.footnotes > ol > li")

    assert note.find("h2").get_text(strip=True) == "A"
    assert note.find(class_="footnote-marker") is None
    assert note.find(class_="doc-footnote-marker-item") is None
    assert "Alpha body." in note.get_text(" ", strip=True)


@pytest.mark.parametrize("title", [
    "Über freie Religion", "Wolność religijna", "Ελεύθερη θρησκεία",
    "Свобода совести", "宗教自由", "חופש דת", "İnanç özgürlüğü",
])
def test_generated_multilingual_heading_ids_survive_recanonicalization(title):
    first = canonicalize_document_html(
        f"<main><h2>{title}</h2><h2>{title}</h2><p>Body.</p></main>", user_id=1
    )
    second = canonicalize_document_html(first.html, user_id=1)
    assert first.diagnostics == second.diagnostics == ()
    assert first.html == second.html
    assert first.content_hash == second.content_hash
    ids = [tag["id"] for tag in BeautifulSoup(first.html, "html.parser").find_all("h2")]
    assert len(set(ids)) == 2


def test_unicode_identifiers_and_local_references_survive_all_document_boundaries():
    raw = (
        '<main aria-labelledby="über-religion"><h2 id="über-religion">Heading</h2>'
        '<p><a name="grüße" href="#über-religion">Back</a>'
        '<a role="doc-noteref" href="#fußnote">1</a></p>'
        '<aside role="doc-endnote" id="fußnote">Note.</aside>'
        '<table><tr><th id="größe">Size</th><td headers="größe">Large</td></tr></table>'
        '</main>'
    )
    first = canonicalize_document_html(raw, user_id=1)
    assert not first.diagnostics
    assert canonicalize_document_html(first.html, user_id=1).diagnostics == ()
    rendered = _sanitize_document_html(
        first.html, project=SimpleNamespace(user_id=1), for_export=True, uploads_dir=None
    )
    before = BeautifulSoup(first.html, "html.parser")
    after = BeautifulSoup(rendered, "html.parser")
    for attribute in ("id", "name", "href", "headers", "aria-labelledby"):
        assert [tag[attribute] for tag in before.find_all(attrs={attribute: True})] == [
            tag[attribute] for tag in after.find_all(attrs={attribute: True})
        ]
    validate_semantic_export(after, export_name="unicode-regression")


@pytest.mark.parametrize("identifier", [
    "bad id", "bad\u00a0id", "bad\u202eid", "bad\u200bid", "bad/id", "bad%id",
    "a" * 129,
])
def test_unicode_identifier_support_does_not_allow_unsafe_tokens(identifier):
    result = canonicalize_document_html(
        f'<main><p id="{identifier}">Text.</p><a href="#{identifier}">Back</a></main>',
        user_id=1,
    )
    soup = BeautifulSoup(result.html, "html.parser")
    assert not soup.p.has_attr("id")
    assert not soup.a.has_attr("href")
    assert {item["code"] for item in result.diagnostics} == {"invalid_attribute_removed"}
