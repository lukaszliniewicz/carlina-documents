"""Focused tests for the pure HTML Document Processor review contract."""

from __future__ import annotations

import json
import re

import pytest
from bs4 import BeautifulSoup

from iarf_assistant.addons.doc_processor.document_review import (
    DocumentReview,
    _literal_matches,
)

SOURCE = """<!doctype html>
<html><head><h1>Do not expose this heading</h1></head><body><main>
<span id="doc-page-1" class="pagebreak" role="doc-pagebreak"></span>
<h1 id="title">Chapter <em>One</em></h1>
<h3>Skipped level</h3>
<h1>Second root</h1>
<p id="joined">Left <strong>NEEDLE</strong> <span id="doc-page-4" class="pagebreak" role="doc-pagebreak"></span>right.</p>
<p id="start"><span id="doc-page-5" class="pagebreak" role="doc-pagebreak"></span>Starts on a new page.</p>
<p id="before">Before duplicate.</p><p id="same-a">duplicate</p><p id="same-b">duplicate</p>
<div id="container"><p id="nested">Needle inside a paragraph.</p></div>
<aside id="footnote-1" role="doc-endnote">A note with <span id="doc-page-99" class="pagebreak"></span>unknown page.</aside>
<p id="after-notes">Main text resumes after an inline note.</p>
</main></body></html>"""


def _review() -> DocumentReview:
    return DocumentReview(SOURCE)


def test_outline_is_body_scoped_and_builds_full_revision_hierarchy():
    review = _review()
    first = review.outline(limit=2)
    second = review.outline(offset=2, limit=2)

    assert [item["text"] for item in first["headings"]] == [
        "Chapter One",
        "Skipped level",
    ]
    assert (
        first["headings"][1]["parent_heading_id"] == first["headings"][0]["element_id"]
    )
    assert first["headings"][1]["issues"] == ["level_jump"]
    assert second["headings"][0]["parent_heading_id"] is None
    assert second["headings"][0]["issues"] == []
    assert first["next_offset"] == 2
    assert second["next_offset"] is None
    assert first["counts"]["heading_levels"] == {
        "h1": 2,
        "h2": 0,
        "h3": 1,
        "h4": 0,
        "h5": 0,
        "h6": 0,
    }
    assert first["counts"]["page_markers"] == 3
    assert all(item["element_id"].startswith("n") for item in first["headings"])


def test_initial_level_jump_and_empty_heading_are_hints_only():
    review = DocumentReview("<h3></h3><h1>Root</h1><h1>Another root</h1>")
    headings = review.outline()["headings"]

    assert headings[0]["issues"] == ["empty_heading", "level_jump"]
    assert headings[0]["parent_heading_id"] is None
    assert headings[1]["issues"] == []
    assert headings[2]["parent_heading_id"] is None


@pytest.mark.parametrize("level", [2, 3])
def test_outline_accepts_uniform_non_h1_body_roots(level):
    review = DocumentReview(
        f"<h{level}>First root</h{level}><h{level}>Second root</h{level}>"
    )

    assert [item["issues"] for item in review.outline()["headings"]] == [[], []]


def test_note_headings_do_not_reset_body_outline_hierarchy():
    review = DocumentReview(
        '<h2>Chapter</h2><h3>Body child</h3>'
        '<aside id="note" role="doc-endnote"><h1>Note title</h1>'
        "<h2>Note section</h2></aside>"
        "<h3>After note</h3>"
    )
    headings = {item["text"]: item for item in review.outline()["headings"]}

    assert headings["Chapter"]["issues"] == []
    assert headings["Body child"]["issues"] == []
    assert headings["Note section"]["parent_heading_id"] == headings["Note title"][
        "element_id"
    ]
    assert headings["After note"]["parent_heading_id"] == headings["Chapter"][
        "element_id"
    ]


def test_literal_search_keeps_inline_order_deduplicates_container_text_and_paginates():
    review = DocumentReview(
        "<div><p>before</p><div><p><b>Need</b>le text</p></div>"
        "<p>needle after</p></div>"
    )
    result = review.search("NEEDLE", before=20, after=20, limit=1)

    assert result["total_matches"] == 2
    assert len(result["items"]) == 1
    first = result["items"][0]
    assert first["snippet"] == "Needle text"
    assert first["match_offset"] == 0
    assert len(first["nearby"]["before"]) <= 3
    assert len(first["nearby"]["after"]) <= 3
    assert first["nearby"]["before"][0]["text"] == "before"
    assert result["next_offset"] == 1
    second = review.search("NEEDLE", offset=1)["items"][0]
    assert second["element_id"] != first["element_id"]
    assert review.search(r".*")["total_matches"] == 0


def test_boundaries_distinguish_joined_split_new_page_source_gap_and_unknown_notes():
    review = _review()
    boundaries = review.boundaries(context_chars=100)["items"]

    assert [item["page_number"] for item in boundaries] == [4, 5]
    joined, unjoined = boundaries
    assert joined["joined_within_element"] is True
    assert (
        joined["containing_element_id"]
        == joined["left_element_id"]
        == joined["right_element_id"]
    )
    assert joined["previous_retained_page_number"] == 1
    assert joined["nonconsecutive_page_markers"] is True
    assert "nonconsecutive_page_markers" in joined["warnings"]
    assert unjoined["joined_within_element"] is False
    assert unjoined["containing_element_id"] == unjoined["right_element_id"]
    assert unjoined["left_element_id"] != unjoined["right_element_id"]
    assert unjoined["previous_retained_page_number"] == 4
    assert re.search(r"Starts on a new page", unjoined["right_text"])

    fetched = review.fetch(element_ids=None, limit=20)["items"]
    page_by_id = {item["element_id"]: item["page_numbers"] for item in fetched}
    assert joined["containing_element_id"] in page_by_id
    assert page_by_id[joined["containing_element_id"]] == [1, 4]
    assert page_by_id[unjoined["containing_element_id"]] == [5]
    footnote_id = next(item["element_id"] for item in fetched if item["tag"] == "aside")
    assert page_by_id[footnote_id] == []
    resumed = next(item for item in fetched if "Main text resumes" in item["text"])
    assert resumed["page_numbers"] == [5]


def test_fetch_pagination_reaches_every_element_once():
    review = DocumentReview("".join(f"<p>Paragraph {i}</p>" for i in range(53)))
    offset = 0
    seen = []
    while offset is not None:
        page = review.fetch(offset=offset, limit=10)
        seen.extend(item["element_id"] for item in page["items"])
        offset = page["next_offset"]
    assert len(seen) == len(set(seen)) == 53


def test_shared_container_does_not_report_a_paragraph_join():
    review = DocumentReview(
        '<main><section><p>One.</p><p><span id="doc-page-2" class="pagebreak"></span>Two.</p></section></main>'
    )
    boundary = review.boundaries()["items"][0]
    assert not boundary["joined_within_element"]
    assert not boundary["punctuation_at_join"]
    assert boundary["left_element_id"] != boundary["right_element_id"]


def test_boundary_excerpts_keep_words_separate_and_inline_words_joined():
    review = DocumentReview(
        '<main><p>One.</p><p>T<b>w</b>o.<span id="doc-page-2" class="pagebreak"></span>Three.</p></main>'
    )
    boundary = review.boundaries()["items"][0]
    assert boundary["left_text"] == "One. Two."
    assert boundary["right_text"] == "Three."
    assert boundary["joined_within_element"]


def test_generic_asides_and_articles_retain_search_and_page_evidence():
    review = DocumentReview(
        '<main><span id="doc-page-1" class="pagebreak"></span><article>One.</article><article>Two.</article><aside>Alpha<span id="doc-page-2" class="pagebreak"></span>Beta</aside></main>'
    )
    assert review.search("Two.")["total_matches"] == 1
    boundary = review.boundaries()["items"][0]
    assert boundary["left_text"] == "One. Two. Alpha"
    assert boundary["right_text"] == "Beta"
    assert boundary["joined_within_element"]
    aside = next(item for item in review.fetch()["items"] if item["tag"] == "aside")
    assert aside["page_numbers"] == [1, 2]


@pytest.mark.parametrize(
    "attributes", ['role="doc-noteref"', 'epub:type="noteref"', 'class="footnote-ref"']
)
def test_scoped_edits_cannot_drop_or_retarget_anonymous_note_references(attributes):
    review = DocumentReview(
        f'<main><p id="p">Text<a {attributes} href="#note">1</a></p><aside role="doc-endnote" id="note">Note</aside><aside role="doc-endnote" id="other">Other</aside></main>'
    )
    element = review.fetch()["items"][0]["element_id"]
    for replacement in [
        '<p id="p">Corrected text</p>',
        f'<p id="p">Corrected<a {attributes} href="#other">1</a></p>',
    ]:
        with pytest.raises(ValueError, match="protected"):
            review.apply_edits(
                [{"element_id": element, "replacement_html": replacement}]
            )
    corrected = review.apply_edits(
        [
            {
                "element_id": element,
                "replacement_html": f'<p id="p">Corrected<a {attributes} href="#note">1</a></p>',
            }
        ]
    )
    assert 'href="#note"' in corrected
    boundary_review = DocumentReview(
        f'<main><p>Alpha<a {attributes} href="#note">1</a><span class="pagebreak" id="doc-page-2"></span>Beta</p><aside role="doc-endnote" id="note">Note</aside></main>'
    )
    assert boundary_review.boundaries()["items"][0]["left_text"] == "Alpha1"


def test_fetch_defaults_to_text_bearing_sequence_and_explicitly_fetches_containers():
    review = DocumentReview("<section id=s><p id=p>Text</p></section>")
    default = review.fetch(limit=20)["items"]
    assert [item["tag"] for item in default] == ["p"]
    section_id = review.outline()["counts"]  # prove this is a distinct review tree
    assert section_id["elements"] == 2
    explicit = review.fetch(element_ids=["n000001"], limit=20)["items"]
    assert explicit[0]["tag"] == "section"
    assert explicit[0]["html"].startswith("<section")


def test_heading_edit_and_split_preserve_ids_images_notes_and_outside_markup():
    source = (
        '<main><h3 id="heading">Heading</h3>'
        '<p id="para">Left <span id="doc-page-2" class="pagebreak" role="doc-pagebreak"></span>right</p>'
        '<p id="outside"><a href="#note">keep</a></p>'
        '<img id="image" src="/uploads/1/image.png">'
        '<aside id="note" role="doc-endnote">Note</aside></main>'
    )
    review = DocumentReview(source)
    heading_id = review.outline()["headings"][0]["element_id"]
    para_id = next(
        item["element_id"]
        for item in review.fetch(element_ids=None, limit=20)["items"]
        if item["tag"] == "p" and "right" in item["text"]
    )
    replacement = (
        '<p id="para">Left <span id="doc-page-2" class="pagebreak" role="doc-pagebreak"></span></p>'
        "<p>right</p>"
    )
    edited = review.apply_edits(
        [
            {"element_id": heading_id, "heading_level": 2},
            {"element_id": para_id, "replacement_html": replacement},
        ]
    )
    soup = BeautifulSoup(edited, "html.parser")
    assert soup.find("h2", id="heading").get_text(strip=True) == "Heading"
    assert [item.get("id") for item in soup.find_all("p")] == [
        "para",
        None,
        "outside",
    ]
    assert soup.find("img")["src"] == "/uploads/1/image.png"
    assert soup.find("a")["href"] == "#note"
    assert soup.find("aside", id="note").get_text(strip=True) == "Note"
    # The read instance remains a valid, unmodified snapshot after editing.
    assert review.fetch(element_ids=[para_id])["items"][0]["text"] == "Left right"


def test_sibling_range_includes_whitespace_and_standalone_page_marker():
    source = (
        '<main><p id="first">First</p> \n '
        '<span id="doc-page-2" class="pagebreak" role="doc-pagebreak"></span> \n '
        '<p id="second">Second</p><p id="outside">Outside</p></main>'
    )
    review = DocumentReview(source)
    entries = review.fetch(element_ids=None, limit=20)["items"]
    first_id = next(item["element_id"] for item in entries if item["text"] == "First")
    second_id = next(item["element_id"] for item in entries if item["text"] == "Second")
    merged = review.apply_edits(
        [
            {
                "element_id": first_id,
                "end_element_id": second_id,
                "replacement_html": '<p id="first">Merged <span id="doc-page-2" class="pagebreak" role="doc-pagebreak"></span>content<span id="second"></span></p>',
            }
        ]
    )
    soup = BeautifulSoup(merged, "html.parser")
    assert [item.get("id") for item in soup.find_all("p")] == ["first", "outside"]
    assert soup.find(id="doc-page-2") is not None
    assert soup.find(id="outside").get_text(strip=True) == "Outside"


@pytest.mark.parametrize(
    "edits",
    [
        [{"element_id": "n999999", "replacement_html": "<p>x</p>"}],
        [
            {
                "element_id": "n000001",
                "end_element_id": "n000002",
                "replacement_html": "<p>x</p>",
            }
        ],
        [{"element_id": "n000001", "replacement_html": "<html><p>x</p></html>"}],
        [{"element_id": "n000001", "replacement_html": "   "}],
    ],
)
def test_invalid_edits_are_rejected_without_partial_result(edits):
    review = DocumentReview("<main><p id=a>A</p><p id=b>B</p></main>")
    before = review.fetch(element_ids=None, limit=20)

    with pytest.raises(ValueError):
        review.apply_edits(edits)

    assert review.fetch(element_ids=None, limit=20) == before


def test_ancestor_descendant_overlap_and_protected_reference_loss_are_rejected():
    review = DocumentReview(
        '<main><div id="outer"><p id="inner">Text</p></div>'
        '<p id="outside"><a href="#note">ref</a></p>'
        '<img id="image" src="/image.png"><aside id="note">Note</aside></main>'
    )
    explicit = review.fetch(element_ids=["n000001", "n000002"], limit=20)["items"]
    outer_id = explicit[0]["element_id"]
    inner_id = explicit[1]["element_id"]
    with pytest.raises(ValueError, match="overlap"):
        review.apply_edits(
            [
                {
                    "element_id": outer_id,
                    "replacement_html": "<div id=outer><p id=inner>one</p></div>",
                },
                {"element_id": inner_id, "replacement_html": "<p id=inner>two</p>"},
            ]
        )

    note_id = next(
        item["element_id"]
        for item in review.fetch(element_ids=None, limit=20)["items"]
        if item["tag"] == "aside"
    )
    with pytest.raises(ValueError, match="protected"):
        review.apply_edits(
            [{"element_id": note_id, "replacement_html": "<aside>lost</aside>"}]
        )


@pytest.mark.parametrize(
    "call",
    [
        lambda review: review.outline(offset=True),
        lambda review: review.search("x", limit=False),
        lambda review: review.fetch(offset=-1),
        lambda review: review.boundaries(context_chars=99),
    ],
)
def test_public_limits_reject_bool_or_out_of_range_values(call):
    with pytest.raises(ValueError):
        call(DocumentReview("<p>x</p>"))


def test_notes_inventory_resolves_targets_backlinks_and_bounded_diagnostics():
    review = DocumentReview(
        '<main><p id="source">Source '
        '<a role="doc-noteref" href="#one">1</a> '
        '<a role="doc-noteref" href="#missing">?</a> '
        '<a role="doc-noteref" href="#dup">D</a></p>'
        '<section id="footnotes" role="doc-endnotes"><ol>'
        '<li id="one" role="doc-endnote">One note '
        '<a epub:type="backlink" href="#source">back</a></li>'
        '<li id="unreferenced">Unreferenced note</li></ol></section>'
        '<aside id="dup" role="doc-endnote">Duplicate target</aside>'
        '<p id="dup">Ordinary duplicate</p></main>'
    )

    result = review.notes(include_html=True)
    assert result["total_notes"] == 3
    assert [item["html_id"] for item in result["items"]] == [
        "one",
        "unreferenced",
        "dup",
    ]
    one = result["items"][0]
    assert one["reference_count"] == 1
    assert one["references"][0]["label"] == "1"
    assert one["references"][0]["element_id"] is not None
    assert one["page_numbers"] == []
    assert one["reference_status"] == "referenced"
    assert one["heading_context_basis"] == "references"
    assert one["backlinks"] == [{"href": "#source", "label": "back"}]
    unreferenced = result["items"][1]
    assert unreferenced["reference_status"] == "unreferenced"
    assert unreferenced["reference_page_numbers"] == []
    assert unreferenced["heading_path"] == []
    assert unreferenced["heading_context_basis"] == "unknown"
    assert "unreferenced_note" not in unreferenced["issues"]
    assert {issue["code"] for issue in result["issues"]} >= {
        "missing_target",
        "duplicate_id",
    }


def test_note_context_comes_from_earlier_body_reference_and_keeps_pages_separate():
    review = DocumentReview(
        '<main><h1>Chapter One</h1><span id="doc-page-7" class="pagebreak"></span>'
        '<p>Body citation <a role="doc-noteref" href="#note">1</a></p>'
        '<aside id="note" role="doc-endnote"><h2>Note heading</h2>'
        '<p>Nested note detail.</p></aside></main>'
    )

    note = review.notes()["items"][0]
    chapter = review.outline()["headings"][0]
    assert note["reference_status"] == "referenced"
    assert note["page_numbers"] == []
    assert note["reference_page_numbers"] == [7]
    assert note["heading_path"] == [
        {
            "element_id": chapter["element_id"],
            "level": 1,
            "text": "Chapter One",
        }
    ]
    assert note["heading_context_basis"] == "references"
    assert note["references"][0]["page_numbers"] == [7]
    assert note["references"][0]["heading_path"] == note["heading_path"]

    nested_note = next(
        item for item in review.search("Nested note", scope="notes")["items"]
    )
    assert nested_note["page_numbers"] == []
    assert nested_note["reference_page_numbers"] == [7]
    assert nested_note["heading_context_basis"] == "references"
    assert nested_note["heading_path"] == note["heading_path"]
    body_context = review.search("Body citation", after=3)["items"][0]
    nested_context = next(
        item
        for item in body_context["nearby"]["after"]
        if "Nested note detail" in item["text"]
    )
    assert nested_context["reference_page_numbers"] == [7]
    assert nested_context["heading_context_basis"] == "references"
    assert nested_context["heading_path"] == note["heading_path"]
    fetched_note = next(
        item
        for item in review.fetch(scope="notes")["items"]
        if item["tag"] == "p" and "Nested note detail" in item["text"]
    )
    assert fetched_note["reference_page_numbers"] == [7]
    assert fetched_note["heading_context_basis"] == "references"
    assert review.fetch(scope="notes", page_number=7)["items"] == []


def test_multiple_note_references_use_common_heading_prefix_and_unreferenced_is_neutral():
    review = DocumentReview(
        '<main><h1>Book</h1><h2>First chapter</h2>'
        '<span id="doc-page-2" class="pagebreak"></span>'
        '<p>First <a role="doc-noteref" href="#shared">1</a></p>'
        '<h2>Second chapter</h2><span id="doc-page-4" class="pagebreak"></span>'
        '<p>Second <a role="doc-noteref" href="#shared">2</a></p>'
        '<aside id="shared" role="doc-endnote"><h3>Note heading</h3>'
        '<p>Shared note.</p></aside>'
        '<aside id="bio" role="doc-endnote"><p>Author biography.</p></aside></main>'
    )

    notes = {item["html_id"]: item for item in review.notes()["items"]}
    shared = notes["shared"]
    assert shared["reference_page_numbers"] == [2, 4]
    assert shared["heading_context_basis"] == "references"
    assert [part["text"] for part in shared["heading_path"]] == ["Book"]
    assert [
        [part["text"] for part in reference["heading_path"]]
        for reference in shared["references"]
    ] == [["Book", "First chapter"], ["Book", "Second chapter"]]
    assert notes["bio"]["reference_status"] == "unreferenced"
    assert notes["bio"]["issues"] == []
    assert "unreferenced_note" not in {issue["code"] for issue in review.notes()["issues"]}


def test_search_and_fetch_scopes_page_filter_heading_path_and_context_html():
    review = DocumentReview(
        '<main><span id="doc-page-2" class="pagebreak"></span>'
        '<h1>Chapter</h1><p id="body">Body text '
        '<a role="doc-noteref" href="#note">1</a></p>'
        '<aside id="note" role="doc-endnote">Note text</aside></main>'
    )

    body = review.search(
        "Body", scope="body", page_number=2, include_html=True, before=20, after=20
    )
    assert body["total_matches"] == 1
    body_item = body["items"][0]
    assert body_item["heading_path"][0]["text"] == "Chapter"
    assert body_item["html"].startswith("<p")
    assert len(body_item["nearby"]["before"]) <= 3
    assert any(item["tag"] == "h1" for item in body_item["nearby"]["before"])
    assert any(item["tag"] == "aside" for item in body_item["nearby"]["after"])
    assert review.search("Note", scope="notes")["total_matches"] == 1
    assert review.search("Chapter", scope="headings")["total_matches"] == 1
    fetched = review.fetch(scope="body", before=20, after=20)
    assert [item["tag"] for item in fetched["items"]] == ["p"]
    assert fetched["items"][0]["nearby"]["after"][0]["tag"] == "aside"


def test_replace_text_matches_raw_unicode_across_inline_nodes_and_respects_boundaries():
    review = DocumentReview(
        '<p id="target">Pré <strong>fix</strong> unicode <em>café</em> '
        '<a href="#note">fix</a> '
        '<span id="doc-page-2" class="pagebreak"></span>fix</p>'
    )
    target_id = review.fetch()["items"][0]["element_id"]
    edited = review.apply_edits(
        [
            {
                "element_id": target_id,
                "replace_text": {
                    "find": "Pré fix",
                    "replacement": "Réparé",
                    "expected_matches": 1,
                },
            }
        ]
    )
    assert "Réparé<strong></strong>" in edited
    assert 'href="#note">fix</a>' in edited
    assert edited.endswith("fix</p>")
    with pytest.raises(ValueError, match="match count"):
        review.apply_edits(
            [
                {
                    "element_id": target_id,
                    "replace_text": {
                        "find": "fix",
                        "replacement": "X",
                        "expected_matches": 3,
                    },
                }
            ]
        )


def test_delete_element_requires_exact_text_and_rejects_protected_subtrees():
    review = DocumentReview(
        '<main><p id="protected">Delete <img src="x"></p><p>Keep</p></main>'
    )
    protected_id = review.fetch()["items"][0]["element_id"]
    with pytest.raises(ValueError, match="expected_text"):
        review.apply_edits(
            [
                {
                    "element_id": protected_id,
                    "delete_element": True,
                    "expected_text": "wrong",
                }
            ]
        )
    with pytest.raises(ValueError, match="protected"):
        review.apply_edits(
            [
                {
                    "element_id": protected_id,
                    "delete_element": True,
                    "expected_text": "Delete",
                }
            ]
        )
    keep_id = review.fetch()["items"][1]["element_id"]
    assert (
        review.apply_edits(
            [
                {
                    "element_id": keep_id,
                    "delete_element": True,
                    "expected_text": "Keep",
                }
            ]
        )
        == '<main><p id="protected">Delete <img src="x"/></p></main>'
    )


def test_fetch_container_keeps_full_text_and_note_descendants_are_not_body():
    review = DocumentReview(
        '<main><h1>Book</h1><span class="pagebreak" id="doc-page-3"></span>'
        '<p>Source<a id="ref" role="doc-noteref" href="#note">1</a></p>'
        '<div id="container"><p>First </p><p>second.</p></div>'
        '<section role="doc-endnotes"><ol><li id="note"><p>Nested note.</p></li></ol></section></main>'
    )
    container = next(
        node.element_id for node in review._nodes if node.tag.get("id") == "container"
    )
    assert review.fetch(element_ids=[container])["items"][0]["text"] == "First second."
    note = review.search("Nested", scope="notes")
    assert note["total_matches"] == 1 and note["items"][0]["page_numbers"] == []
    assert review.search("Nested", scope="body")["total_matches"] == 0
    assert review.search("Nested", scope="notes", page_number=3)["total_matches"] == 0
    assert all(
        "Nested" not in item["text"] for item in review.fetch(scope="body")["items"]
    )
    note_id = note["items"][0]["element_id"]
    assert (
        review.fetch(element_ids=[note_id], scope="notes")["items"][0]["text"]
        == "Nested note."
    )


def test_notes_relationships_validate_same_note_and_bound_reference_lists():
    references = "".join(
        f'<a id="r{i}" role="doc-noteref" href="#note">{i}</a>' for i in range(25)
    )
    backlinks = "".join(
        f'<a class="footnote-backref" href="#r{i}">back</a>' for i in range(25)
    )
    review = DocumentReview(
        f'<main><h1 id="wrong">Book</h1><p>{references}</p>'
        f'<aside role="doc-footnote" id="note">Note {backlinks}<a role="doc-backlink" href="#wrong">bad</a></aside></main>'
    )
    note = review.notes()["items"][0]
    assert (
        note["reference_count"] == 25
        and len(note["references"]) == 20
        and note["references_truncated"]
    )
    assert (
        note["backlink_count"] == 26
        and len(note["backlinks"]) == 20
        and note["backlinks_truncated"]
    )
    assert "invalid_backlink" in note["issues"]
    assert [issue["code"] for issue in review.notes()["issues"]] == ["invalid_backlink"]


def test_notes_bounded_diagnostics_html_and_complete_pagination():
    missing = "".join(
        f'<a role="doc-noteref" href="#{i}{"x" * 1000}">missing</a>' for i in range(130)
    )
    bodies = "".join(
        f'<aside role="doc-footnote" id="n{i}">{"text " * 1600}</aside>'
        for i in range(12)
    )
    review = DocumentReview(f"<main><p>{missing}</p>{bodies}</main>")
    result = review.notes(include_html=True, limit=5)
    assert result["total_notes"] == 12 and result["next_offset"] == 5
    assert result["issues_truncated"]
    assert all(len(issue["html_id"] or "") <= 500 for issue in result["issues"])
    assert all(item["html_omitted"] and item["truncated"] for item in result["items"])
    assert len(json.dumps(result, ensure_ascii=False)) <= 128000
    seen = [item["html_id"] for item in result["items"]]
    while result["next_offset"] is not None:
        result = review.notes(offset=result["next_offset"], limit=5)
        seen.extend(item["html_id"] for item in result["items"])
    assert len(seen) == len(set(seen)) == 12


def test_search_and_fetch_html_context_stays_within_shared_budget():
    review = DocumentReview(
        "<main><h1>Heading</h1>"
        + "".join(f"<p>needle {i} {'x' * 7800}</p>" for i in range(20))
        + "</main>"
    )
    searched = review.search("needle", include_html=True, before=3, after=3, limit=20)
    contexts = [
        entry
        for item in searched["items"]
        for direction in ("before", "after")
        for entry in item["nearby"][direction]
    ]
    assert (
        sum(len(item.get("html", "")) for item in [*searched["items"], *contexts])
        <= 64000
    )
    assert any(item.get("html_omitted") for item in searched["items"])
    fetched = review.fetch(before=3, after=3)
    contexts = [
        entry
        for item in fetched["items"]
        for direction in ("before", "after")
        for entry in item["nearby"][direction]
    ]
    assert sum(len(item["html"]) for item in fetched["items"]) <= 64000
    assert sum(len(item.get("html", "")) for item in contexts) <= 64000
    assert fetched["next_offset"] is not None


def test_note_response_budget_paginates_without_losing_entries():
    references = "".join(
        f'<a role="doc-noteref" href="#note{note}">{"r" * 500}</a>'
        for note in range(10)
        for _ in range(20)
    )
    bodies = "".join(
        f'<aside role="doc-endnote" id="note{note}">{"body " * 1500}</aside>'
        for note in range(10)
    )
    review = DocumentReview(f"<main><p>{references}</p>{bodies}</main>")
    result = review.notes(limit=50, include_html=True)
    assert 0 < len(result["items"]) < 10
    seen = []
    while True:
        assert len(json.dumps(result, ensure_ascii=False)) <= 128000
        seen.extend(item["html_id"] for item in result["items"])
        if result["next_offset"] is None:
            break
        result = review.notes(offset=result["next_offset"], limit=50, include_html=True)
    assert len(seen) == len(set(seen)) == 10


@pytest.mark.parametrize(
    "divider",
    [
        '<a href="#n">skip</a>',
        '<span role="doc-pagebreak"></span>',
        "<img>",
        "<!-- comment -->",
        "<span hidden>skip</span>",
        '<span role="doc-noteref">1</span>',
        "<p>middle</p>",
        "<br>",
    ],
)
def test_literal_replacement_cannot_cross_protected_or_block_boundaries(divider):
    review = DocumentReview(f"<div>left{divider}right</div>")
    element_id = review._nodes[0].element_id
    with pytest.raises(ValueError, match="match count"):
        review.apply_edits(
            [
                {
                    "element_id": element_id,
                    "replace_text": {
                        "find": "leftright",
                        "replacement": "joined",
                        "expected_matches": 1,
                    },
                }
            ]
        )


def test_literal_replacement_multiple_inline_matches_escapes_markup():
    review = DocumentReview("<p>ab<strong>cd</strong> ab<em>cd</em> abcd</p>")
    element_id = review._nodes[0].element_id
    edited = review.apply_edits(
        [
            {
                "element_id": element_id,
                "replace_text": {
                    "find": "abcd",
                    "replacement": "<fixed>",
                    "expected_matches": 3,
                },
            }
        ]
    )
    assert BeautifulSoup(edited, "html.parser").get_text() == "<fixed> <fixed> <fixed>"
    assert "<fixed>" not in edited and "<strong></strong>" in edited


@pytest.mark.parametrize(
    "resource",
    [
        "<img>",
        '<span role="doc-pagebreak"></span>',
        '<span role="doc-noteref">1</span>',
    ],
)
def test_delete_rejects_anonymous_protected_resources(resource):
    review = DocumentReview(f"<p>Header{resource}</p>")
    fetched = review.fetch()["items"][0]
    with pytest.raises(ValueError, match="protected"):
        review.apply_edits(
            [
                {
                    "element_id": fetched["element_id"],
                    "delete_element": True,
                    "expected_text": fetched["text"],
                }
            ]
        )


def test_marked_furniture_cleanup_protects_references_notes_and_real_headings():
    html = '''<main><span id="doc-page-1" role="doc-pagebreak" title="1"></span>
    <header>Real article header</header><h2>Actual section</h2>
    <div class="page_header">Remove <span>this</span></div>
    <div class="page_header"><span class="page_header">Nested</span></div>
    <div class="page_header" id="target">Keep referenced header</div><a href="#target">See header</a>
    <section class="page_footer"><img src="picture.png" alt="Keep image"/></section>
    <section class="page_footer"><span role="doc-pagebreak" id="doc-page-2" title="2"></span>Keep anchor</section>
    <aside role="doc-endnotes"><p role="doc-endnote" id="fn1" class="page_footer">Keep note</p></aside>
    <p>Body text.</p></main>'''
    review = DocumentReview(html)
    cleaned, summary = review.remove_page_furniture(["header", "footer"])
    assert summary["removed_count"] == 2
    assert summary["skipped_count"] == 4
    assert "Remove" not in cleaned and "Nested" not in cleaned
    for text in ("Real article header", "Actual section", "Keep referenced", "Keep image", "Keep anchor", "Keep note"):
        assert text in cleaned
    assert review.remove_page_furniture(["header", "footer"])[0] == cleaned


@pytest.mark.parametrize("grouped", [False, True])
def test_multiple_replacements_share_original_coordinates_and_preserve_markup(grouped):
    review = DocumentReview(
        '<p id="target">alpha <em id="em">beta</em> gamma alpha</p>'
    )
    operations = [
        {"find": "alpha", "replacement": "beta", "expected_matches": 2},
        {"find": "beta", "replacement": "δ", "expected_matches": 1},
        {"find": "gamma", "replacement": "longer text", "expected_matches": 1},
    ]
    edits = (
        [{"element_id": "n000001", "replace_texts": operations}]
        if grouped
        else [{"element_id": "n000001", "replace_text": item} for item in operations]
    )
    result = BeautifulSoup(review.apply_edits(edits), "html.parser")
    assert result.p.get_text() == "beta δ longer text beta"
    assert result.p["id"] == "target" and result.em["id"] == "em"
    assert review.fetch()["items"][0]["text"] == "alpha beta gamma alpha"


@pytest.mark.parametrize(
    ("find", "text", "expected"),
    [
        ("ueber", "ueber darueber", 1),
        ("weiss", "weiss weisst", 1),
        ("wuerde", "wuerde wuerden", 1),
        ("menleben", "menleben Zusammenleben", 1),
        ("a", "a a\u0308 a_ a2", 1),
        ("s", "ß", 0),
        ("ss", "ß", 1),
        ("Vernunft", "Vernunft vernunft", 2),
    ],
)
def test_unicode_word_search_has_exact_boundaries(find, text, expected):
    review = DocumentReview(f"<p>{text}</p>")
    matches = list(_literal_matches(text, find, False, True))
    assert len(matches) == expected
    result = review.search(find, whole_word=True)
    assert result["total_matches"] == int(expected > 0)
    assert result["search_options"]["whole_word"] is True


def test_case_sensitive_search_identifies_hit_not_merely_uppercase_context():
    review = DocumentReview(
        "<p>Vernunft alone.</p><p>Vernunft and vernunft together.</p>"
    )
    result = review.search("vernunft", case_sensitive=True, before=1)
    assert result["total_matches"] == 1
    assert result["items"][0]["matched_text"] == "vernunft"
    assert result["items"][0]["nearby"]["before"][0]["text"] == "Vernunft alone."
    assert result["search_options"]["case_sensitive"] is True
    with pytest.raises(ValueError):
        review.search("vernunft", case_sensitive="false")


def test_batch_reports_all_count_failures_without_writing_or_source_leakage():
    review = DocumentReview("<p>alpha beta</p><p>gamma</p>")
    before = review.fetch()["items"]
    with pytest.raises(ValueError) as caught:
        review.apply_edits(
            [
                {
                    "element_id": "n000001",
                    "replace_texts": [
                        {
                            "find": "alpha",
                            "replacement": "secret replacement",
                            "expected_matches": 2,
                        },
                        {
                            "find": "absent",
                            "replacement": "secret replacement",
                            "expected_matches": 1,
                        },
                    ],
                },
                {
                    "element_id": "n000002",
                    "replace_text": {
                        "find": "gamma",
                        "replacement": "changed",
                        "expected_matches": 2,
                    },
                },
            ]
        )
    errors = caught.value.diagnostics
    assert [
        (d["edit_index"], d["replacement_index"], d["actual_matches"]) for d in errors
    ] == [(0, 0, 1), (0, 1, 0), (1, 0, 1)]
    assert errors[2]["element_id"] == "n000002"
    assert "secret replacement" not in str(caught.value)
    assert "alpha" not in str(caught.value)
    assert review.fetch()["items"] == before


def test_overlapping_same_element_replacements_are_rejected_atomically():
    review = DocumentReview("<p>alpha</p>")
    with pytest.raises(ValueError) as caught:
        review.apply_edits(
            [
                {
                    "element_id": "n000001",
                    "replace_texts": [
                        {"find": "alpha", "replacement": "x", "expected_matches": 1},
                        {"find": "pha", "replacement": "y", "expected_matches": 1},
                    ],
                }
            ]
        )
    assert caught.value.diagnostics[0]["code"] == "overlapping_edits"
    assert review.fetch()["items"][0]["text"] == "alpha"


def test_heading_demotion_preserves_ids_inline_emphasis_and_page_anchors():
    review = DocumentReview(
        '<h3 id="phrase">Here <em id="word">is</em><span class="pagebreak" id="doc-page-2" role="doc-pagebreak"></span> the beginning,</h3><p>and its continuation.</p>'
    )
    result = BeautifulSoup(
        review.apply_edits([{"element_id": "n000001", "demote_heading": True}]),
        "html.parser",
    )
    assert result.h3 is None
    assert result.find(id="phrase").name == "p"
    assert result.find(id="phrase").strong.em["id"] == "word"
    assert result.find(id="doc-page-2") is not None
    assert len(result.find_all("p")) == 2  # No implicit sentence merge.
    for edit in (
        {"element_id": "n000002", "demote_heading": True},
        {"element_id": "n000001", "demote_heading": False},
    ):
        with pytest.raises(ValueError):
            review.apply_edits([edit])


def test_structure_hints_are_advisory_and_keep_titled_numbered_sections():
    review = DocumentReview(
        "<h1>Book</h1><h3>1.</h3><p>First proposition.</p>"
        "<h3>This is</h3><p>part of the statement.</p>"
        "<h3>1. Origin</h3><p>A complete section.</p>"
        "<h3>What?</h3><p>Answer.</p><h3>Why?</h3><p>Answer.</p>"
        "<h3>How?</h3><p>Answer.</p>"
    )
    result = review.outline()
    headings = {h["text"]: h for h in result["headings"]}
    assert "ordinal_only_heading" in headings["1."]["issues"]
    assert "possible_run_in_heading" in headings["This is"]["issues"]
    assert "ordinal_only_heading" not in headings["1. Origin"]["issues"]
    assert "question_answer_sequence" in headings["Why?"]["issues"]
    assert result["hints_are_advisory"] is True
    assert result["total_headings"] == 7


def test_inventory_is_complete_paginated_unicode_aware_and_includes_notes():
    review = DocumentReview(
        "<h1>Book</h1><h4>Detail</h4><p>über über a\u0308 note</p>"
        '<aside role="doc-endnote" id="fn">footnote anzuhoeren</aside>'
    )
    first = review.inventory(limit=2)
    items = list(first["items"])
    offset = first["next_offset"]
    while offset is not None:
        page = review.inventory(offset=offset, limit=2)
        items.extend(page["items"])
        offset = page["next_offset"]
    tokens = {i["token"]: i for i in items}
    assert tokens["über"]["occurrences"] == 2
    assert "a\u0308" in tokens and "anzuhoeren" in tokens
    assert first["coverage"]["heading_levels"]["h4"] == 1
    assert first["coverage"]["note_bodies"] == 1
    assert first["review_completion"] == "not_tracked"
    assert len(items) == first["total_distinct_tokens"]
    assert review.inventory(scope="notes")["total_occurrences"] == 2


@pytest.mark.parametrize(
    "html",
    [
        '<p>weiss<a href="#x">t</a></p>',
        '<p><a href="#x">dar</a>ueber</p>',
    ],
)
def test_whole_word_does_not_treat_protected_inline_edges_as_word_boundaries(html):
    review = DocumentReview(html)
    word = "weiss" if "weiss" in html else "ueber"
    with pytest.raises(ValueError) as caught:
        review.apply_edits(
            [
                {
                    "element_id": "n000001",
                    "replace_text": {
                        "find": word,
                        "replacement": "X",
                        "expected_matches": 1,
                        "whole_word": True,
                    },
                }
            ]
        )
    assert caught.value.diagnostics[0]["actual_matches"] == 0


def test_multiple_cross_inline_replacements_keep_original_positions():
    review = DocumentReview('<p id="p">abc<strong id="s">def</strong>ghij</p>')
    result = BeautifulSoup(
        review.apply_edits(
            [
                {
                    "element_id": "n000001",
                    "replace_texts": [
                        {"find": "bcde", "replacement": "X", "expected_matches": 1},
                        {"find": "fghi", "replacement": "Y", "expected_matches": 1},
                    ],
                }
            ]
        ),
        "html.parser",
    )
    assert result.p.get_text() == "aXYj"
    assert result.strong["id"] == "s" and result.strong.get_text() == "Y"


@pytest.mark.parametrize(
    "html",
    [
        "<p>weiss<br/>t</p>",
        "<p>weiss<span hidden>t</span></p>",
        '<p>weiss<a role="doc-noteref" href="#fn">1</a></p>',
    ],
)
def test_whole_word_observes_visible_not_structural_boundaries(html):
    review = DocumentReview(html)
    result = review.apply_edits(
        [
            {
                "element_id": "n000001",
                "replace_text": {
                    "find": "weiss",
                    "replacement": "weiß",
                    "expected_matches": 1,
                    "whole_word": True,
                },
            }
        ]
    )
    assert "weiß" in result


def test_mixed_enumerator_levels_flagged_across_structural_levels():
    review = DocumentReview(
        "<h1>Book</h1><h2>IV</h2><h3>1. Catholic</h3><p>Text.</p>"
        "<h2>2. Jews</h2><p>Text.</p>"
        "<h3>3. Free</h3><p>Text.</p>"
    )
    headings = {h["text"]: h for h in review.outline()["headings"]}

    assert headings["1. Catholic"]["issues"] == ["mixed_enumerator_levels"]
    assert headings["2. Jews"]["issues"] == ["mixed_enumerator_levels"]
    assert headings["3. Free"]["issues"] == ["mixed_enumerator_levels"]
    assert headings["IV"]["issues"] == []


def test_uniform_enumerator_levels_and_deep_numbering_stay_clean():
    review = DocumentReview(
        "<h2>IV</h2><h3>1. Catholic</h3><p>Text.</p>"
        "<h3>2. Jews</h3><p>Text.</p>"
        "<h4>1. First point</h4><p>Text.</p>"
        "<h4>II. Second point</h4><p>Text.</p>"
    )
    headings = {h["text"]: h for h in review.outline()["headings"]}

    for text, heading in headings.items():
        assert "mixed_enumerator_levels" not in heading["issues"], text
