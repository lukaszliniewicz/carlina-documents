from __future__ import annotations

import re

import pytest
from bs4 import BeautifulSoup, Tag

from iarf_assistant.addons.doc_processor.parsers.pdf_utils import (
    concatenate_html_pages,
    normalize_footnote_links,
)


def _concatenate(pages: list[str]) -> BeautifulSoup:
    return BeautifulSoup(concatenate_html_pages(pages), "html.parser")


def _target_for_reference(soup: BeautifulSoup, reference) -> Tag | None:
    return soup.find(id=str(reference.get("href", "")).removeprefix("#"))


@pytest.mark.parametrize(
    ("name", "marker"),
    [
        ("numeric-label", '<span class="footnote_marker">29</span>'),
        (
            "conventional-marker-id",
            '<span class="footnote_marker" id="fm29">29</span>',
        ),
        (
            "conventional-href",
            '<span class="footnote_marker" href="#f29">29</span>',
        ),
    ],
    ids=["numeric-label", "conventional-marker-id", "conventional-href"],
)
def test_numeric_29_resolves_only_to_the_note_on_its_source_page(name, marker):
    pages = []
    for page_index in range(4):
        note = (
            '<div class="footnote" id="f29">The recognized note.</div>'
            if page_index == 0
            else ""
        )
        pages.append(
            f"<main_content><p>{marker}</p>"
            f"<ul><li>Ordinary later list item {page_index}.</li></ul>"
            f"</main_content><footnotes>{note}</footnotes>"
        )

    soup = _concatenate(pages)
    references = soup.find_all("a", class_="footnote-ref")
    unresolved = soup.find_all("sup", class_="footnote-unresolved")

    assert len(references) == 1, name
    assert len(unresolved) == 3, name
    assert references[0].get_text(strip=True) == "29"
    assert references[0]["href"] == f"#{soup.find(class_='footnote')['id']}"
    assert all(marker.get_text(strip=True) == "29" for marker in unresolved)
    assert all(marker.get("id", "").startswith("fnref-") for marker in unresolved)
    assert len(soup.find_all("li", string=re.compile("Ordinary later list item"))) == 4


def test_repeated_local_numeric_references_share_the_unique_same_page_note():
    soup = _concatenate(
        [
            '<main_content><p><span class="footnote_marker">7</span> and '
            '<span class="footnote_marker">7</span></p></main_content>'
            '<footnotes><div class="footnote" id="f7">One note.</div></footnotes>'
        ]
    )

    references = soup.find_all("a", class_="footnote-ref")
    assert len(references) == 2
    assert references[0]["href"] == references[1]["href"]


def test_identical_labels_and_bodies_remain_distinct_and_ambiguous():
    soup = _concatenate(
        [
            '<main_content><span class="footnote_marker">1</span></main_content>'
            '<footnotes><div class="footnote" id="f1">Same body.</div>'
            '<div class="footnote" id="f1">Same body.</div></footnotes>'
        ]
    )

    assert not soup.find_all("a", class_="footnote-ref")
    assert soup.find("sup", class_="footnote-unresolved").get_text(strip=True) == "1"
    notes = soup.find_all(class_="footnote")
    assert len(notes) == 2
    assert len({note["id"] for note in notes}) == 2
    assert [note.get_text(" ", strip=True).endswith("Same body.") for note in notes] == [
        True,
        True,
    ]


def test_missing_numeric_label_never_uses_an_arbitrary_unused_note():
    soup = _concatenate(
        [
            '<main_content><span class="footnote_marker">99</span></main_content>'
            '<footnotes><div class="footnote" id="f1">First note.</div>'
            '<div class="footnote" id="f2">Second note.</div></footnotes>'
        ]
    )

    assert not soup.find_all("a", class_="footnote-ref")
    unresolved = soup.find("sup", class_="footnote-unresolved")
    assert unresolved is not None
    assert unresolved.get_text(strip=True) == "99"
    assert unresolved.get("id", "").startswith("fnref-")


def test_display_order_does_not_turn_an_unlabelled_note_into_a_match():
    soup = _concatenate(
        [
            '<main_content><span class="footnote_marker">1</span></main_content>'
            '<footnotes><div class="footnote">Unlabelled note body.</div></footnotes>'
        ]
    )

    assert not soup.find_all("a", class_="footnote-ref")
    unresolved = soup.find("sup", class_="footnote-unresolved")
    assert unresolved is not None
    assert unresolved.get_text(strip=True) == "1"


def test_nearest_competing_notes_remain_unresolved_without_same_page_evidence():
    soup = _concatenate(
        [
            '<main_content><p>Page one.</p></main_content>'
            '<footnotes><div class="footnote" id="f8">Earlier note.</div></footnotes>',
            '<main_content><span class="footnote_marker">8</span></main_content>'
            "<footnotes></footnotes>",
            '<main_content><p>Page three.</p></main_content>'
            '<footnotes><div class="footnote" id="f8">Later note.</div></footnotes>',
        ]
    )

    assert not soup.find_all("a", class_="footnote-ref")
    assert soup.find("sup", class_="footnote-unresolved").get_text(strip=True) == "8"


def test_explicit_scopes_separate_same_page_sequences_and_survive_inheritance():
    soup = _concatenate(
        [
            '<main_content><section data-footnote-scope="chapter-a">'
            '<span class="footnote_marker">1</span></section>'
            '<section data-footnote-scope="chapter-b">'
            '<span class="footnote_marker">1</span></section></main_content>'
            '<footnotes><section data-footnote-scope="chapter-a">'
            '<div class="footnote" id="f1">Chapter A note.</div></section>'
            '<section data-footnote-scope="chapter-b">'
            '<div class="footnote" id="f1">Chapter B note.</div></section></footnotes>'
        ]
    )

    references = soup.find_all("a", class_="footnote-ref")
    assert len(references) == 2
    assert [reference["data-footnote-scope"] for reference in references] == [
        "chapter-a",
        "chapter-b",
    ]
    targets = [_target_for_reference(soup, reference) for reference in references]
    assert "Chapter A note." in targets[0].get_text(" ", strip=True)
    assert "Chapter B note." in targets[1].get_text(" ", strip=True)
    assert [target["data-footnote-scope"] for target in targets] == [
        "chapter-a",
        "chapter-b",
    ]


def test_explicit_scope_allows_a_unique_cross_page_endnote():
    soup = _concatenate(
        [
            '<main_content><p data-footnote-scope="endnotes">'
            '<span class="footnote_marker">12</span></p></main_content>'
            "<footnotes></footnotes>",
            "<main_content><p>Later page.</p></main_content>"
            '<footnotes><div data-footnote-scope="endnotes" '
            'class="footnote" id="f12">Endnote body.</div></footnotes>',
        ]
    )

    reference = soup.find("a", class_="footnote-ref")
    assert reference is not None
    assert reference["data-footnote-scope"] == "endnotes"
    target = _target_for_reference(soup, reference)
    assert target is not None
    assert target["data-footnote-scope"] == "endnotes"
    assert "Endnote body." in target.get_text(" ", strip=True)


def test_declared_scope_never_falls_back_to_missing_or_conflicting_scope():
    main = BeautifulSoup(
        '<main><span class="footnote_marker" data-footnote-scope="chapter-a" '
        'data-docproc-source-page="0">1</span></main>',
        "html.parser",
    )
    footnotes = BeautifulSoup(
        '<div class="footnote" id="f1" data-docproc-source-page="0">Unscoped.</div>'
        '<div class="footnote" id="f1" data-footnote-scope="chapter-b" '
        'data-docproc-source-page="0">Other scope.</div>',
        "html.parser",
    )

    normalize_footnote_links(main, footnotes)

    assert not main.find_all("a", class_="footnote-ref")
    unresolved = main.find("sup", class_="footnote-unresolved")
    assert unresolved is not None
    assert unresolved["data-footnote-scope"] == "chapter-a"
    assert unresolved.get_text(strip=True) == "1"


def test_invalid_explicit_scope_fails_closed_and_is_not_copied_to_reference():
    main = BeautifulSoup(
        '<main><span class="footnote_marker" data-footnote-scope="bad scope" '
        'data-docproc-source-page="0">1</span></main>',
        "html.parser",
    )
    footnotes = BeautifulSoup(
        '<div class="footnote" id="f1" data-docproc-source-page="0">Only note.</div>',
        "html.parser",
    )

    normalize_footnote_links(main, footnotes)

    assert not main.find_all("a", class_="footnote-ref")
    unresolved = main.find("sup", class_="footnote-unresolved")
    assert unresolved is not None
    assert unresolved.get_text(strip=True) == "1"
    assert not unresolved.has_attr("data-footnote-scope")


def test_repeated_explicit_ids_are_disambiguated_by_same_source_page():
    main = BeautifulSoup(
        '<main><span class="footnote_marker" href="#f1" '
        'data-docproc-source-page="1">1</span></main>',
        "html.parser",
    )
    footnotes = BeautifulSoup(
        '<div class="footnote" id="f1" data-docproc-source-page="1">Local note.</div>'
        '<div class="footnote" id="f1" data-docproc-source-page="4">Other note.</div>',
        "html.parser",
    )

    normalize_footnote_links(main, footnotes)

    reference = main.find("a", class_="footnote-ref")
    assert reference is not None
    target = footnotes.find(id=reference["href"].removeprefix("#"))
    assert target is not None
    assert "Local note." in target.get_text(" ", strip=True)


def test_unique_unscoped_nonconventional_href_can_reach_later_note():
    main = BeautifulSoup(
        '<main><span class="footnote_marker" href="#later-note" '
        'data-docproc-source-page="1">*</span></main>',
        "html.parser",
    )
    footnotes = BeautifulSoup(
        '<div class="footnote" id="later-note" '
        'data-docproc-source-page="10">Later note.</div>',
        "html.parser",
    )

    normalize_footnote_links(main, footnotes)

    reference = main.find("a", class_="footnote-ref")
    assert reference is not None
    target = footnotes.find(id=reference["href"].removeprefix("#"))
    assert target is not None
    assert "Later note." in target.get_text(" ", strip=True)


@pytest.mark.parametrize("label", ["α", "٢"])
def test_unicode_shorthand_target_is_not_a_document_global_identity(label):
    main = BeautifulSoup(
        f'<main><span class="footnote_marker" href="#f{label}" '
        f'data-docproc-source-page="10">{label}</span></main>', "html.parser",
    )
    notes = BeautifulSoup(
        f'<div class="footnote" id="f{label}" data-docproc-source-page="1">Earlier note.</div>',
        "html.parser",
    )
    normalize_footnote_links(main, notes)
    assert not main.find("a", class_="footnote-ref")
    assert main.find("sup", class_="footnote-unresolved").get_text() == label


def test_nonconventional_href_does_not_cross_a_declared_note_scope():
    main = BeautifulSoup(
        '<main><span class="footnote_marker" href="#later-note" '
        'data-docproc-source-page="1">*</span></main>',
        "html.parser",
    )
    footnotes = BeautifulSoup(
        '<div class="footnote" id="later-note" data-footnote-scope="appendix" '
        'data-docproc-source-page="10">Scoped later note.</div>',
        "html.parser",
    )

    normalize_footnote_links(main, footnotes)

    assert not main.find_all("a", class_="footnote-ref")
    assert main.find("sup", class_="footnote-unresolved").get_text(strip=True) == "*"


def test_scope_free_conventional_href_is_page_local_but_scoped_href_can_continue():
    main = BeautifulSoup(
        '<main><span class="footnote_marker" href="#f29" '
        'data-docproc-source-page="1">29</span>'
        '<span class="footnote_marker" href="#f30" '
        'data-docproc-source-page="1" data-footnote-scope="endnotes">30</span></main>',
        "html.parser",
    )
    footnotes = BeautifulSoup(
        '<div class="footnote" id="f29" data-docproc-source-page="10">Wrong page.</div>'
        '<div class="footnote" id="f30" data-docproc-source-page="10" '
        'data-footnote-scope="endnotes">Scoped endnote.</div>',
        "html.parser",
    )

    normalize_footnote_links(main, footnotes)

    references = main.find_all("a", class_="footnote-ref")
    assert len(references) == 1
    assert references[0]["data-footnote-scope"] == "endnotes"
    target = footnotes.find(id=references[0]["href"].removeprefix("#"))
    assert target is not None
    assert "Scoped endnote." in target.get_text(" ", strip=True)
    unresolved = main.find("sup", class_="footnote-unresolved")
    assert unresolved is not None
    assert unresolved.get_text(strip=True) == "29"


def test_bare_sup_requires_same_page_or_unambiguous_declared_scope():
    soup = _concatenate(
        [
            '<main_content><p>x<sup>2</sup> and x<sup>99</sup>; '
            '<span data-footnote-scope="endnotes"><sup>3</sup></span>.</p>'
            "</main_content><footnotes></footnotes>",
            '<main_content><p>Notes.</p></main_content><footnotes>'
            '<div class="footnote" id="f2">Another-page note 2.</div>'
            '<div class="footnote" id="f3" data-footnote-scope="endnotes">'
            "Scoped note 3.</div></footnotes>",
        ]
    )

    references = soup.find_all("a", class_="footnote-ref")
    assert len(references) == 1
    assert references[0].get_text(strip=True) == "3"
    assert references[0]["data-footnote-scope"] == "endnotes"
    assert "Scoped note 3." in _target_for_reference(soup, references[0]).get_text(
        " ", strip=True
    )
    ordinary_superscripts = [
        sup for sup in soup.find_all("sup") if not sup.find("a", class_="footnote-ref")
    ]
    assert [sup.get_text(strip=True) for sup in ordinary_superscripts] == ["2", "99"]
    assert all(not sup.has_attr("id") for sup in ordinary_superscripts)


def test_scope_free_label_and_symbolic_singletons_need_known_page_metadata():
    main = BeautifulSoup(
        '<main><span class="footnote_marker">1</span> '
        '<span class="footnote_marker">*</span></main>',
        "html.parser",
    )
    footnotes = BeautifulSoup(
        '<div class="footnote" id="f1">The only note.</div>',
        "html.parser",
    )

    normalize_footnote_links(main, footnotes)

    assert not main.find_all("a", class_="footnote-ref")
    assert [
        marker.get_text(strip=True)
        for marker in main.find_all("sup", class_="footnote-unresolved")
    ] == ["1", "*"]


def test_unresolved_generated_reference_id_is_stable_and_label_is_preserved():
    def normalized_id() -> str:
        main = BeautifulSoup(
            '<main><span class="footnote_marker">404</span></main>',
            "html.parser",
        )
        footnotes = BeautifulSoup(
            '<div class="footnote" id="f1">Unrelated note.</div>',
            "html.parser",
        )
        normalize_footnote_links(main, footnotes)
        unresolved = main.find("sup", class_="footnote-unresolved")
        assert unresolved is not None
        assert unresolved.get_text(strip=True) == "404"
        return str(unresolved.get("id"))

    first_id = normalized_id()
    assert first_id == normalized_id()
    assert re.fullmatch(r"fnref-1-404-[0-9a-f]{8}", first_id)


def test_different_scoped_note_bodies_survive_continuation_and_forced_merge():
    output = concatenate_html_pages(
        [
            '<main_content><p data-footnote-scope="chapter-a">A '
            '<span class="footnote_marker">1</span> ends.</p></main_content>'
            '<footnotes><div class="footnote continues" id="f1" '
            'data-footnote-scope="chapter-a">Chapter A body.</div></footnotes>',
            '<main_content><p data-footnote-scope="chapter-b">B '
            '<span class="footnote_marker">1</span> ends.</p></main_content>'
            '<footnotes><div class="footnote continued-from-previous" id="f1" '
            'data-footnote-scope="chapter-b">Chapter B body.</div></footnotes>',
        ],
        boundary_flags={0: {"footnotes": "merge"}},
    )
    soup = BeautifulSoup(output, "html.parser")

    references = soup.find_all("a", class_="footnote-ref")
    notes = soup.find_all(class_="footnote")
    assert len(references) == 2
    assert len(notes) == 2
    assert references[0]["href"] != references[1]["href"]
    assert [note["data-footnote-scope"] for note in notes] == [
        "chapter-a",
        "chapter-b",
    ]
    assert "Chapter A body." in _target_for_reference(soup, references[0]).get_text(
        " ", strip=True
    )
    assert "Chapter B body." in _target_for_reference(soup, references[1]).get_text(
        " ", strip=True
    )


def test_nested_scoped_note_children_survive_merge_of_unscoped_wrappers():
    soup = _concatenate(
        [
            '<main_content><p data-footnote-scope="chapter-a">A '
            '<span class="footnote_marker">1</span> ends.</p></main_content>'
            '<footnotes><section class="continues"><div class="footnote continues" '
            'id="f1" data-footnote-scope="chapter-a">Nested A body.</div></section>'
            '</footnotes>',
            '<main_content><p data-footnote-scope="chapter-b">B '
            '<span class="footnote_marker">1</span> ends.</p></main_content>'
            '<footnotes><section class="continued-from-previous">'
            '<div class="footnote continued-from-previous" id="f1" '
            'data-footnote-scope="chapter-b">Nested B body.</div></section></footnotes>',
        ]
    )

    references = soup.find_all("a", class_="footnote-ref")
    notes = soup.find_all(class_="footnote")
    assert len(references) == 2
    assert len(notes) == 2
    assert references[0]["href"] != references[1]["href"]
    assert "Nested A body." in _target_for_reference(soup, references[0]).get_text(
        " ", strip=True
    )
    assert "Nested B body." in _target_for_reference(soup, references[1]).get_text(
        " ", strip=True
    )
