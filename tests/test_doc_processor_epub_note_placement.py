from __future__ import annotations

import hashlib
import posixpath
import zipfile
from pathlib import Path
from types import SimpleNamespace
from xml.etree import ElementTree as ET

import pytest
from bs4 import BeautifulSoup
from flask import Flask

from carlina.core.epub_export import epub_value_sha256, normalize_epub_options
from carlina.core.export_notes import normalize_export_note_roles
from carlina.core.semantic_export import SemanticExportRequest
from iarf_assistant.addons.doc_processor import epub_packager, epub_validation
from iarf_assistant.addons.doc_processor.epub_notes import (
    index_note_units,
    owning_note_unit,
    place_chapter_notes,
)
from iarf_assistant.addons.doc_processor.epub_packager import EPUBPackager
from iarf_assistant.addons.doc_processor.epub_validation import EpubValidationResult
from iarf_assistant.addons.doc_processor.parsers.pdf_utils import concatenate_html_pages
from iarf_assistant.addons.doc_processor.routes import _validate_epub_file
from iarf_assistant.addons.doc_processor.semantic_export import (
    DocumentResourceResolver,
    export_semantic_document,
)
from iarf_assistant.addons.translator.export_metadata import apply_export_metadata

_XHTML = "{http://www.w3.org/1999/xhtml}"


def _arrangement(**overrides: object) -> dict[str, object]:
    return {
        "placement": "page_footnotes",
        "numbering": "chapter",
        "scope_heading_level": 2,
        "source_pages": [1, 2],
        "evidence": "Two supplied chapter openers restart the printed sequence.",
        **overrides,
    }


def _source(*, nested: bool = False, extra: str = "") -> str:
    chapters = []
    for index in (1, 2):
        chapter = (
            f'<h2>Chapter {index}</h2><p id="body-{index}">Prose '
            f'<sup><a id="ref-{index}" href="#note-{index}" role="doc-noteref" '
            f'data-footnote-scope="chapter-{index}">[1]</a></sup></p>'
            f"{extra if index == 1 else ''}<h3>Subheading {index}</h3>"
            f'<p id="last-{index}">Final chapter paragraph.</p>'
        )
        chapters.append(f"<section>{chapter}</section>" if nested else chapter)
    notes = "".join(
        f'<li id="note-{index}" role="doc-endnote" epub:type="endnote" '
        f'data-footnote-label="[1]" data-footnote-scope="chapter-{index}">'
        f'<strong class="footnote-marker">[1] </strong>Chapter {index} note. '
        f'<a href="#ref-{index}" role="doc-backlink">Return</a></li>'
        for index in (1, 2)
    )
    return (
        "<main><h1>Restarted notes</h1>" + "".join(chapters) + "</main>"
        '<section id="collected-notes" role="doc-endnotes" epub:type="endnotes">'
        "<h2>Notes</h2><ol>" + notes + "</ol></section>"
    )


def _build(tmp_path: Path, html: str, arrangement: dict[str, object]) -> Path:
    document = SimpleNamespace(
        id="11111111-1111-1111-1111-111111111111",
        filename="fixture.pdf",
        file_path=str(tmp_path / "fixture.pdf"),
        settings={
            "document_metadata": {
                "title": "Restarted notes",
                "language": "en",
                "note_arrangement": arrangement,
            }
        },
        metadata_record=None,
        output_html=html,
        output_markdown=None,
    )
    output = tmp_path / "fixture.epub"
    with Flask(__name__).app_context():
        EPUBPackager(document, str(output)).build()
    assert document.output_html == html
    return output


def _contents(path: Path) -> list[tuple[str, ET.Element]]:
    with zipfile.ZipFile(path) as archive:
        return [
            (name.removeprefix("EPUB/"), ET.fromstring(archive.read(name)))
            for name in sorted(archive.namelist())
            if name.startswith("EPUB/content-") and name.endswith(".xhtml")
        ]


def _assert_local_notes_and_links(path: Path) -> None:
    contents = _contents(path)
    ordered_ids = [
        str(node.get("id"))
        for _, root in contents
        for node in root.iter()
        if node.get("id")
    ]
    assert (
        ordered_ids.index("last-1")
        < ordered_ids.index("note-1")
        < ordered_ids.index("body-2")
    )
    assert ordered_ids.index("last-2") < ordered_ids.index("note-2")
    assert "collected-notes" not in ordered_ids
    targets = {
        f"{name}#{node.get('id')}"
        for name, root in contents
        for node in root.iter()
        if node.get("id")
    }
    links = [node for _, root in contents for node in root.iter(f"{_XHTML}a")]
    assert len(links) == 4
    assert all(node.get("href") in targets for node in links)
    for _, root in contents:
        for section in root.iter(f"{_XHTML}section"):
            if section.get("class") != "chapter-endnotes":
                continue
            assert section.get("aria-label") in {"Chapter 1", "Chapter 2"}
            assert section.find(f".//{_XHTML}ol") is None
            markers = section.findall(f".//{_XHTML}strong")
            assert [marker.text for marker in markers] == ["[1] "]


@pytest.mark.parametrize("nested", [False, True])
def test_restarted_notes_follow_whole_chapter_and_keep_both_link_directions(
    tmp_path: Path, nested: bool
) -> None:
    path = _build(tmp_path, _source(nested=nested), _arrangement())
    _assert_local_notes_and_links(path)
    assert _validate_epub_file(str(path)) == (True, None)
    contents = _contents(path)
    headings = [node for _, root in contents for node in root.iter(f"{_XHTML}h2")]
    assert [node.text for node in headings] == ["Chapter 1", "Chapter 2"]
    assert all(node.get("id") for node in headings)


def test_large_chapter_notes_follow_final_fragment_not_first_chunk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(epub_packager, "MAX_CONTENT_DOCUMENT_BYTES", 800)
    extra = "".join(
        f'<p id="fragment-{index}">{"Long prose. " * 35}</p>' for index in range(4)
    )
    extra += '<blockquote cite="#note-1">Quoted chapter note.</blockquote>'
    path = _build(tmp_path, _source(extra=extra), _arrangement())
    assert len(_contents(path)) >= 5
    _assert_local_notes_and_links(path)
    contents = _contents(path)
    targets = {
        f"{name}#{node.get('id')}"
        for name, root in contents
        for node in root.iter()
        if node.get("id")
    }
    citation = next(
        node for _, root in contents for node in root.iter(f"{_XHTML}blockquote")
    )
    assert citation.get("cite") in targets
    assert _validate_epub_file(str(path)) == (True, None)


@pytest.mark.parametrize(
    "arrangement",
    [
        {},
        _arrangement(numbering="document", placement="document_endnotes"),
        _arrangement(scope_heading_level=None),
        _arrangement(numbering="unknown", placement="unknown"),
    ],
)
def test_global_or_uncertain_arrangement_retains_collected_position(
    tmp_path: Path, arrangement: dict[str, object]
) -> None:
    path = _build(tmp_path, _source(), arrangement)
    ordered_ids = [
        str(node.get("id"))
        for _, root in _contents(path)
        for node in root.iter()
        if node.get("id")
    ]
    assert ordered_ids.index("last-2") < ordered_ids.index("note-1")
    assert "collected-notes" in ordered_ids


@pytest.mark.parametrize("placement", ["chapter_endnotes", "mixed"])
def test_local_numbering_also_groups_collected_chapter_endnotes(
    tmp_path: Path, placement: str
) -> None:
    _assert_local_notes_and_links(
        _build(tmp_path, _source(), _arrangement(placement=placement))
    )


def test_cross_chapter_references_conflicting_scope_and_unreferenced_notes_stay_put() -> (
    None
):
    html = (
        '<main><h2 id="c1">One</h2><p><a href="#shared" role="doc-noteref">1</a>'
        '<a href="#conflict" role="doc-noteref" data-footnote-scope="wrong">2</a></p>'
        '<h2 id="c2">Two</h2><p><a href="#shared" role="doc-noteref">1</a></p></main>'
        '<section role="doc-endnotes"><aside id="shared" epub:type="endnote">Shared.</aside>'
        '<aside id="conflict" epub:type="endnote" data-footnote-scope="chapter-1">Conflicting.</aside>'
        '<aside id="unreferenced" epub:type="endnote">Unreferenced.</aside></section>'
    )
    soup = BeautifulSoup(html, "html.parser")
    before = str(soup)
    place_chapter_notes(soup, {"note_arrangement": _arrangement()})
    assert str(soup) == before


@pytest.mark.parametrize("citation_unit", ["One", "Two"])
def test_note_citations_participate_in_chapter_ownership(citation_unit: str) -> None:
    citation = '<blockquote cite="#n">Quoted note.</blockquote>'
    soup = BeautifulSoup(
        '<main><h2>One</h2><p><a href="#n">1</a></p>'
        f"{citation if citation_unit == 'One' else ''}<h2>Two</h2>"
        f"{citation if citation_unit == 'Two' else ''}</main>"
        '<section id="notes" role="doc-endnotes"><aside id="n" epub:type="endnote">Body.</aside></section>',
        "html.parser",
    )
    place_chapter_notes(soup, {"note_arrangement": _arrangement()})
    assert (soup.select_one("#notes #n") is not None) == (citation_unit == "Two")
    assert (soup.select_one('.chapter-endnotes[aria-label="One"] #n') is not None) == (
        citation_unit == "One"
    )


def test_notes_follow_trailing_prose_before_a_chapter_opener_in_one_wrapper() -> None:
    soup = BeautifulSoup(
        '<main><section><h2>One</h2><p><a href="#n">1</a></p></section>'
        '<section><p id="tail">Still chapter one.</p><h2 id="two">Two</h2></section></main>'
        '<section role="doc-endnotes"><aside id="n" epub:type="endnote">Body.</aside></section>',
        "html.parser",
    )
    place_chapter_notes(soup, {"note_arrangement": _arrangement()})
    ids = [tag.get("id") for tag in soup.find_all(id=True)]
    assert ids.index("tail") < ids.index("n") < ids.index("two")
    assert len(soup.find_all(id="tail")) == 1


def test_relocated_note_retains_scope_inherited_from_its_source_container() -> None:
    soup = BeautifulSoup(
        '<main><h2>One</h2><p><a href="#n" data-footnote-scope="local-one">1</a></p>'
        '<h2>Two</h2></main><section role="doc-endnotes" data-footnote-scope="local-one">'
        '<aside id="n" epub:type="endnote">Body.</aside></section>',
        "html.parser",
    )
    place_chapter_notes(soup, {"note_arrangement": _arrangement()})
    note = soup.select_one('.chapter-endnotes[aria-label="One"] #n')
    assert note is not None and note.get("data-footnote-scope") == "local-one"


def test_native_list_note_counters_keep_source_values_after_grouping() -> None:
    soup = BeautifulSoup(
        '<main><h2>One</h2><p><a href="#n8">8</a><a href="#n12">12</a></p><h2>Two</h2></main>'
        '<section role="doc-endnotes"><ol start="8"><li id="n8" epub:type="endnote" data-footnote-label="8">Eighth.</li>'
        '<li id="n12" value="12" epub:type="endnote">Twelfth.</li></ol></section>',
        "html.parser",
    )
    normalize_export_note_roles(soup)
    place_chapter_notes(soup, {"note_arrangement": _arrangement()})
    normalize_export_note_roles(soup)
    group = soup.find(class_="chapter-endnotes")
    assert group is not None
    assert [note.get("value") for note in group.find_all("li")] == ["8", "12"]


@pytest.mark.parametrize("label", ["1", "[1]", "a)", "*", "†"])
def test_plain_printed_labels_do_not_receive_another_list_counter(label: str) -> None:
    printed = "1." if label == "1" else label
    soup = BeautifulSoup(
        '<main><h2>One</h2><p><a href="#note">Note</a></p><h2>Two</h2></main>'
        '<section role="doc-endnotes"><ol><li id="note" epub:type="endnote" '
        f'data-footnote-label="{label}"><p>{printed} Note body.</p></li></ol></section>',
        "html.parser",
    )
    normalize_export_note_roles(soup)
    place_chapter_notes(soup, {"note_arrangement": _arrangement()})
    normalize_export_note_roles(soup)
    group = soup.find(class_="chapter-endnotes")
    assert group is not None and group.find("ol") is None
    note = group.find(id="note")
    assert note is not None
    assert note.get_text(strip=True) == f"{printed} Note body."
    assert note.get("role") == "listitem"


def test_nested_list_label_does_not_suppress_the_parent_note_counter() -> None:
    soup = BeautifulSoup(
        '<section role="doc-endnotes"><ol><li id="note" epub:type="endnote" '
        'data-footnote-label="1"><ul><li>1. Nested item.</li></ul>Own body.</li>'
        "</ol></section>",
        "html.parser",
    )
    normalize_export_note_roles(soup)
    note = soup.find(id="note")
    assert note is not None and note.name == "li"
    assert note.parent is not None and note.parent.name == "ol"


def test_translated_semantic_provider_receives_source_arrangement(
    tmp_path: Path,
) -> None:
    html = _source()
    metadata = apply_export_metadata(
        {"title": "Source", "note_arrangement": _arrangement()},
        {"title": "Translation"},
        "pl",
    )
    request = SemanticExportRequest(
        semantic_html=html,
        semantic_html_sha256=hashlib.sha256(html.encode()).hexdigest(),
        metadata=metadata,
        resources=(),
        format="epub",
        include_source_toc=False,
        profile_id="translation",
        profile_version="1",
    )
    artifact = export_semantic_document(
        request, resolver=DocumentResourceResolver(user_id=1)
    )
    path = tmp_path / "translated.epub"
    path.write_bytes(artifact.data)
    _assert_local_notes_and_links(path)


def test_semantic_provider_validates_publishing_epub_and_binds_request_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    html = _source()
    options = normalize_epub_options({"direction": "rtl"})
    request = SemanticExportRequest(
        semantic_html=html,
        semantic_html_sha256=hashlib.sha256(html.encode("utf-8")).hexdigest(),
        metadata={"title": "Translated", "language": "pl"},
        resources=(),
        format="epub",
        include_source_toc=False,
        profile_id="translation",
        profile_version="1",
        epub_options=options,
    )
    calls: list[tuple[str, object]] = []

    def validate(path: str, *, config=None) -> EpubValidationResult:
        assert Path(path).is_file()
        assert Path(path).stat().st_size > 0
        calls.append((path, config))
        return EpubValidationResult(True, None, "not_run")

    monkeypatch.setattr(epub_validation, "validate_epub_file", validate)
    exported = export_semantic_document(
        request, resolver=DocumentResourceResolver(user_id=1)
    )

    assert len(calls) == 1
    assert calls[0][1] is None
    receipt = exported.receipt.to_dict()
    quality = receipt["quality"]
    assert quality["input_sha256"] == request.semantic_html_sha256
    assert quality["options_sha256"] == epub_value_sha256(options)
    assert quality["artifact_sha256"] == hashlib.sha256(exported.data).hexdigest()
    assert quality["checks"]["structure"] == "passed"
    assert quality["checks"]["epubcheck"] == "not_run"
    assert quality["epubcheck_version"] is None


def test_semantic_provider_rejects_an_invalid_publishing_epub(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from iarf_assistant.addons.doc_processor.semantic_export import SemanticExportError

    html = "<main><h1>Private source</h1></main>"
    request = SemanticExportRequest(
        semantic_html=html,
        semantic_html_sha256=hashlib.sha256(html.encode("utf-8")).hexdigest(),
        metadata={"title": "Synthetic"},
        resources=(),
        format="epub",
        include_source_toc=False,
        profile_id="translation",
        profile_version="1",
        epub_options=normalize_epub_options({}),
    )
    monkeypatch.setattr(
        epub_validation,
        "validate_epub_file",
        lambda _path, **_kwargs: EpubValidationResult(
            False, "private archive details", "not_run"
        ),
    )

    with pytest.raises(
        SemanticExportError,
        match=r"^semantic-export-epub-validation-failed$",
    ):
        export_semantic_document(request, resolver=DocumentResourceResolver(user_id=1))


@pytest.mark.parametrize(
    "label", ["1", "1.", "[1]", "1)", "a", "a)", "*", "†", "¶", "‖", "※", "¹"]
)
def test_extraction_matching_preserves_printed_label_punctuation(label: str) -> None:
    html = concatenate_html_pages(
        [
            (
                f'<main_content><p><span class="footnote_marker" id="ref-a"><a href="#note-a">{label}</a></span></p></main_content>'
                f'<footnotes><div class="footnote" id="note-a" data-footnote-label="{label}">Note text.</div></footnotes>'
            )
        ]
    )
    soup = BeautifulSoup(html, "html.parser")
    expected = "1" if label == "¹" else label
    reference = soup.find("a", class_="footnote-ref")
    note = soup.find(class_="footnote")
    assert reference is not None and note is not None
    assert reference.get_text(strip=True) == expected
    assert reference.get("href") == f"#{note.get('id')}"
    assert note.get("data-footnote-label") == expected
    body_marker = note.find(class_="footnote-marker")
    assert body_marker is not None and body_marker.get_text() == f"{expected} "


def test_unlabelled_notes_receive_no_invented_global_counter() -> None:
    soup = BeautifulSoup(
        concatenate_html_pages(
            [
                (
                    "<main_content><p>Prose.</p></main_content><footnotes>"
                    '<div class="footnote" id="note-opaque">Unlabelled note.</div></footnotes>'
                )
            ]
        ),
        "html.parser",
    )
    note = soup.find(class_="footnote")
    assert note is not None
    assert note.get_text(strip=True) == "Unlabelled note."
    assert note.find(class_="footnote-marker") is None


def test_source_authored_return_link_follows_unique_opaque_reference_id() -> None:
    soup = BeautifulSoup(
        concatenate_html_pages(
            [
                (
                    '<main_content><p><span class="footnote_marker" id="ref-p1-a">'
                    '<a href="#note-a">[1]</a></span></p></main_content><footnotes>'
                    '<div class="footnote" id="note-a" data-footnote-label="[1]">Note. '
                    '<a href="#ref-p1-a" role="doc-backlink">Return</a></div></footnotes>'
                )
            ]
        ),
        "html.parser",
    )
    reference = soup.find("a", class_="footnote-ref")
    backlink = soup.find("a", role="doc-backlink")
    assert reference is not None and backlink is not None
    assert backlink.get("href") == f"#{reference.get('id')}"


def test_witnessed_scope_in_another_chapter_blocks_unscoped_reference_assignment() -> (
    None
):
    soup = BeautifulSoup(
        '<main><h2>One</h2><p><a href="#a">1</a></p>'
        '<h2>Two</h2><p><a href="#b" data-footnote-scope="local-two">1</a></p></main>'
        '<section id="notes" role="doc-endnotes"><aside id="a" epub:type="endnote" '
        'data-footnote-scope="local-two">A.</aside><aside id="b" epub:type="endnote" '
        'data-footnote-scope="local-two">B.</aside></section>',
        "html.parser",
    )
    place_chapter_notes(soup, {"note_arrangement": _arrangement()})
    assert soup.select_one("#notes #a") is not None
    assert soup.select_one('.chapter-endnotes[aria-label="Two"] #b') is not None


def test_owning_note_unit_alias_keeps_positional_api_and_keyword_override() -> None:
    soup = BeautifulSoup(
        '<main><h2>One</h2><p><a href="#note" data-footnote-scope="shared">1</a></p>'
        '<h2>Two</h2><p><a href="#other" data-footnote-scope="shared">2</a></p></main>'
        '<section role="doc-endnotes" data-footnote-scope="shared">'
        '<aside id="note" epub:type="endnote">First.</aside>'
        '<aside id="other" epub:type="endnote">Second.</aside></section>',
        "html.parser",
    )
    units, references, scope_units = index_note_units(soup, 2, {"note", "other"})
    note = soup.find(id="note")
    assert note is not None

    assert owning_note_unit(note, references["note"], scope_units) is None
    assert (
        owning_note_unit(
            note,
            references["note"],
            scope_units,
            document_numbering=True,
        )
        is units[0]
    )
    with pytest.raises(TypeError):
        owning_note_unit(note, references["note"], scope_units, True)


def test_document_numbering_places_opaque_notes_by_unique_h3_ownership(
    tmp_path: Path,
) -> None:
    html = (
        '<main><h1>Document notes</h1><h2 id="part-one">Part One</h2>'
        '<h3 id="alpha">Alpha</h3><p id="alpha-last">A. '
        '<a id="ref-a1" href="#opaque-a" data-footnote-scope="shared">9</a> '
        '<a id="ref-a2" href="#opaque-a" data-footnote-scope="shared">1</a></p>'
        '<h3 id="beta">Beta</h3><p id="beta-last">B. '
        '<a id="ref-b" href="#opaque-b" data-footnote-scope="shared">2</a></p>'
        '<h2 id="part-two">Part Two</h2><h3 id="gamma">Gamma</h3>'
        '<p id="gamma-last">C. '
        '<a id="ref-g" href="#opaque-g" data-footnote-scope="shared">3</a></p>'
        '</main><section role="doc-endnotes" data-footnote-scope="shared">'
        '<aside id="opaque-a" epub:type="endnote" data-footnote-label="3">'
        "A note—exact <em>source</em>. "
        '<a id="return-a" href="#ref-a1" role="doc-backlink">Return.</a></aside>'
        '<aside id="opaque-b" epub:type="endnote" data-footnote-label="1">'
        "B note—exact <em>source</em>. "
        '<a id="return-b" href="#ref-b" role="doc-backlink">Return.</a></aside>'
        '<aside id="opaque-g" epub:type="endnote" data-footnote-label="*">'
        "G note—exact <em>source</em>. "
        '<a id="return-g" href="#ref-g" role="doc-backlink">Return.</a></aside>'
        "</section>"
    )
    path = _build(
        tmp_path,
        html,
        _arrangement(
            placement="chapter_endnotes",
            numbering="document",
            scope_heading_level=3,
        ),
    )
    content = _contents(path)
    ordered_ids = [
        str(node.get("id"))
        for _, root in content
        for node in root.iter()
        if node.get("id")
    ]

    expected_ids = {
        "part-one",
        "alpha",
        "alpha-last",
        "ref-a1",
        "ref-a2",
        "opaque-a",
        "return-a",
        "beta",
        "beta-last",
        "ref-b",
        "opaque-b",
        "return-b",
        "part-two",
        "gamma",
        "gamma-last",
        "ref-g",
        "opaque-g",
        "return-g",
    }
    assert all(ordered_ids.count(node_id) == 1 for node_id in expected_ids)
    assert ordered_ids.index("alpha-last") < ordered_ids.index("opaque-a")
    assert ordered_ids.index("opaque-a") < ordered_ids.index("beta")
    assert ordered_ids.index("beta-last") < ordered_ids.index("opaque-b")
    assert ordered_ids.index("opaque-b") < ordered_ids.index("part-two")
    assert ordered_ids.index("gamma-last") < ordered_ids.index("opaque-g")
    for note_id in ("opaque-a", "opaque-b", "opaque-g"):
        assert ordered_ids.count(note_id) == 1

    nodes = {
        node.get("id"): node
        for _, root in content
        for node in root.iter()
        if node.get("id")
    }
    assert {
        note_id: nodes[note_id].get("data-footnote-label")
        for note_id in ("opaque-a", "opaque-b", "opaque-g")
    } == {"opaque-a": "3", "opaque-b": "1", "opaque-g": "*"}
    assert {
        note_id: "".join(nodes[note_id].itertext())
        for note_id in ("opaque-a", "opaque-b", "opaque-g")
    } == {
        "opaque-a": "A note—exact source. Return.",
        "opaque-b": "B note—exact source. Return.",
        "opaque-g": "G note—exact source. Return.",
    }
    assert {
        link_id: str(nodes[link_id].get("href") or "").rsplit("#", 1)[-1]
        for link_id in (
            "ref-a1",
            "ref-a2",
            "ref-b",
            "ref-g",
            "return-a",
            "return-b",
            "return-g",
        )
    } == {
        "ref-a1": "opaque-a",
        "ref-a2": "opaque-a",
        "ref-b": "opaque-b",
        "ref-g": "opaque-g",
        "return-a": "ref-a1",
        "return-b": "ref-b",
        "return-g": "ref-g",
    }
    assert {
        link_id: "".join(nodes[link_id].itertext())
        for link_id in ("ref-a1", "ref-a2", "ref-b", "ref-g")
    } == {"ref-a1": "9", "ref-a2": "1", "ref-b": "2", "ref-g": "3"}
    targets = {
        f"{name}#{node.get('id')}"
        for name, root in content
        for node in root.iter()
        if node.get("id")
    }
    for name, root in content:
        for link in root.iter(f"{_XHTML}a"):
            destination, separator, fragment = str(link.get("href") or "").partition(
                "#"
            )
            assert separator and fragment
            target_name = (
                posixpath.normpath(posixpath.join(posixpath.dirname(name), destination))
                if destination
                else name
            )
            assert f"{target_name}#{fragment}" in targets


def test_document_numbering_keeps_cross_unit_and_bad_scope_notes_collected() -> None:
    html = (
        "<main><h2>Part One</h2><h3>One</h3>"
        '<p><a href="#cross" data-footnote-scope="shared">1</a> '
        '<a href="#with-none" data-footnote-scope="shared">2</a> '
        '<a href="#none-only" data-footnote-scope="shared">3</a> '
        '<a href="#bad-reference-scope" data-footnote-scope="bad scope">4</a> '
        '<a href="#bad-note-scope" data-footnote-scope="shared">5</a> '
        '<a href="#mismatched-scope" data-footnote-scope="reference-scope">6</a> '
        '<a href="#conflicting-scopes" data-footnote-scope="left">7</a> '
        '<a href="#conflicting-scopes" data-footnote-scope="right">8</a></p>'
        '<h3>Two</h3><p><a href="#cross" data-footnote-scope="shared">1</a></p>'
        "<h2>Back matter</h2><p>"
        '<a href="#with-none" data-footnote-scope="shared">2</a> '
        '<a href="#none-only" data-footnote-scope="shared">3</a></p></main>'
        '<section id="notes" role="doc-endnotes" data-footnote-scope="shared">'
        '<aside id="cross" epub:type="endnote">Cross chapter.</aside>'
        '<aside id="with-none" epub:type="endnote">Chapter and unowned.</aside>'
        '<aside id="none-only" epub:type="endnote">Unowned reference.</aside>'
        '<aside id="bad-reference-scope" epub:type="endnote">Malformed reference scope.</aside>'
        '<aside id="bad-note-scope" epub:type="endnote" '
        'data-footnote-scope="bad scope">Malformed note scope.</aside>'
        '<aside id="mismatched-scope" epub:type="endnote" '
        'data-footnote-scope="note-scope">Mismatched scope.</aside>'
        '<aside id="conflicting-scopes" epub:type="endnote">Conflicting declarations.</aside>'
        "</section>"
    )
    soup = BeautifulSoup(html, "html.parser")
    before = str(soup)

    place_chapter_notes(
        soup,
        {
            "note_arrangement": _arrangement(
                placement="chapter_endnotes",
                numbering="document",
                scope_heading_level=3,
            )
        },
    )

    assert str(soup) == before


@pytest.mark.parametrize(
    "reference",
    [
        '<blockquote cite="#notes">Quoted prose.</blockquote>',
        '<a href="#notes">All notes</a>',
    ],
)
def test_addressed_note_collection_keeps_its_contents_and_anchor(
    reference: str,
) -> None:
    soup = BeautifulSoup(
        f'<main><h2>One</h2><p><a href="#a">1</a></p>{reference}</main>'
        '<section id="notes" role="doc-endnotes"><aside id="a" epub:type="endnote">A.</aside></section>',
        "html.parser",
    )
    before = str(soup)
    place_chapter_notes(soup, {"note_arrangement": _arrangement()})
    assert str(soup) == before


def test_retained_note_assembly_reproduces_original_bytes() -> None:
    pages = [
        (
            '<main_content><p>First<span class="footnote_marker" id="fm1">[1]</span></p></main_content>'
            '<footnotes><div class="footnote" id="f1" data-footnote-label="[1]">First body.</div></footnotes>'
        ),
        (
            "<main_content><p>Second.</p></main_content><footnotes>"
            '<div class="footnote" id="note-opaque">— body text</div></footnotes>'
        ),
    ]
    retained = concatenate_html_pages(pages, preserve_note_labels=False)
    # Captured from the actual f27cd587 implementation, not rebuilt by a
    # second copy of the algorithm under test.
    assert (
        hashlib.sha256(retained.encode()).hexdigest()
        == "51360cf79d6c23d552b7aaf36019231dfd39162cf2f4c6ed246147a5031812b9"
    )
    modern = BeautifulSoup(concatenate_html_pages(pages), "html.parser")
    notes = modern.find_all(class_="footnote")
    assert notes[0].get("data-footnote-label") == "[1]"
    assert notes[1].get_text(strip=True) == "— body text"
    assert notes[1].get("data-footnote-label") is None


@pytest.mark.parametrize(
    ("label", "original_digest"),
    [
        ("*", "1c94c5c033d135c52207e9c2a77bfc7604d93b657f73f1d8ac9593a715f78775"),
        ("†", "e92d2d8ef57544f42971d913ec252fed4335710cbc5b03c1d89801c89f743b6b"),
    ],
)
def test_retained_symbolic_labels_reproduce_original_bytes(
    label: str, original_digest: str
) -> None:
    pages = [
        (
            '<main_content><p>Body<span class="footnote_marker" id="ref-a">'
            f'<a href="#note-a">{label}</a></span></p></main_content><footnotes>'
            f'<div class="footnote" id="note-a" data-footnote-label="{label}">Note text.</div></footnotes>'
        )
    ]
    retained = concatenate_html_pages(pages, preserve_note_labels=False)
    assert hashlib.sha256(retained.encode()).hexdigest() == original_digest
    modern = BeautifulSoup(concatenate_html_pages(pages), "html.parser")
    marker = modern.find(class_="footnote-marker")
    assert marker is not None and marker.get_text() == f"{label} "


def test_collection_citation_keeps_notes_and_resolves_across_spine_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(epub_packager, "MAX_CONTENT_DOCUMENT_BYTES", 800)
    html = _source(extra='<blockquote cite="#collected-notes">Quoted.</blockquote>')
    path = _build(tmp_path, html, _arrangement())
    contents = _contents(path)
    assert len(contents) >= 2
    targets = {
        f"{name}#{node.get('id')}"
        for name, root in contents
        for node in root.iter()
        if node.get("id")
    }
    citation = next(
        node for _, root in contents for node in root.iter(f"{_XHTML}blockquote")
    )
    assert citation.get("cite") in targets
    assert citation.get("cite", "").endswith("#collected-notes")
    assert not any(
        node.get("class") == "chapter-endnotes"
        for _, root in contents
        for node in root.iter()
    )
    assert _validate_epub_file(str(path)) == (True, None)
