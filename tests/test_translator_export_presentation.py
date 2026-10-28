import base64

import pytest
from bs4 import BeautifulSoup

from carlina.core.semantic_export import validate_semantic_export_html
from iarf_assistant.addons.translator.block_contract import BlockContractError
from iarf_assistant.addons.translator.export_presentation import present_export


def test_publishing_replacement_retires_old_cover_semantics_and_preserves_links():
    html = (
        '<html><body><span id="epub-cover-image"></span><figure class="cover">'
        '<img id="old-cover" src="old.png" alt="Old painting" role="presentation" aria-hidden="true">'
        '</figure><p><a href="#old-cover">Cover</a> and unchanged prose.</p></body></html>'
    )
    output = present_export(
        html, {"title": "Edition"}, cover_uri="new.png", renew_cover_identity=True
    )
    soup = BeautifulSoup(output, "html.parser")
    assert soup.img["id"] == "epub-cover-image-2"
    assert soup.img["alt"] == "Edition — Cover"
    assert soup.img.get("role") is None
    assert soup.img.get("aria-hidden") is None
    assert soup.img["data-epub-selected-cover"] == "true"
    assert soup.find("span", id="old-cover") is not None
    assert '<p><a href="#old-cover">Cover</a> and unchanged prose.</p>' in output


def test_metadata_projection_keeps_exact_body_and_escapes_values():
    body = "<main><p id='p'>A &amp; B <a href='#note'>1</a>.</p></main><section id='note'><p>Note.</p></section>"
    original = f'<html lang="en"><head><meta charset="utf-8"><title>Old</title><meta name="DC.publisher" content="Old publisher"></head><body>{body}</body></html>'
    result = present_export(
        original,
        {
            "title": 'New <title> & "text"',
            "language": "en",
            "contributors": {"authors": ["Author"], "translators": ['A "translator"']},
            "description": "Two\nlines",
        },
    )
    assert result.split("<body>")[1].split("</body>")[0] == body
    soup = BeautifulSoup(result, "html.parser")
    assert soup.title.string == 'New <title> & "text"'
    assert len(soup.find_all("title")) == 1
    assert (
        soup.find("meta", attrs={"name": "DC.contributor.translator"})["content"]
        == 'A "translator"'
    )
    assert soup.find("meta", attrs={"name": "DC.publisher"}) is None
    validate_semantic_export_html(result)


def test_replacement_cover_keeps_notes_image_ids_and_body_text():
    uri = "data:image/jpeg;base64," + base64.b64encode(b"synthetic").decode()
    html = "<html><body><figure class='cover' id='front'><img id='cover-image' src='.carlina-resources/res_abcdefghijklmnop' width='9' alt='Cover'></figure><main><p>Unchanged.</p></main><section id='notes'><p>Note.</p></section></body></html>"
    result = present_export(html, {"title": "Book", "language": "en"}, cover_uri=uri)
    soup = BeautifulSoup(result, "html.parser")
    assert len(soup.find_all("img")) == 1
    assert soup.img["src"] == uri
    assert soup.img["id"] == "cover-image"
    assert "cover" in soup.img["class"]
    assert "width" not in soup.img.attrs
    assert (
        "<main><p>Unchanged.</p></main><section id='notes'><p>Note.</p></section>"
        in result
    )
    validate_semantic_export_html(result)


def test_cover_is_inserted_when_absent_and_ambiguous_covers_fail():
    uri = "data:image/jpeg;base64,eA=="
    result = present_export(
        "<main><p>Book.</p></main>", {"title": "Book"}, cover_uri=uri
    )
    soup = BeautifulSoup(result, "html.parser")
    assert soup.body.find_all(recursive=False)[0].name == "figure"
    with pytest.raises(BlockContractError, match="export-cover-ambiguous"):
        present_export(
            '<html><body><img class="cover" src="data:image/jpeg;base64,eA=="><img role="doc-cover" src="data:image/jpeg;base64,eA=="></body></html>',
            {},
            cover_uri=uri,
        )


def test_metadata_vocabulary_does_not_allow_http_equiv_refresh():
    with pytest.raises(ValueError, match="semantic-export-html-safety"):
        validate_semantic_export_html(
            '<html><head><meta http-equiv="refresh" content="0;url=https://example.com"></head><body></body></html>'
        )


def test_omitting_a_cover_retires_its_image_but_preserves_link_targets_and_body_bytes():
    body = '<p id="body">Keep <a href="#cover">cover link</a> and notes.</p>'
    html = (
        '<html><body><figure class="cover"><img id="cover" src=".carlina-resources/res_old"></figure>'
        + body
        + "</body></html>"
    )
    output = present_export(html, {"title": "Edition"}, omit_cover=True)
    assert body in output
    assert '<span id="cover"></span>' in output
    assert "res_old" not in output
