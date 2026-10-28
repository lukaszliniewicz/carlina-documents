"""Explicit note repair preserves source and all existing reference topology."""

import pytest
from bs4 import BeautifulSoup

from iarf_assistant.addons.doc_processor.document_review import DocumentReview
from iarf_assistant.addons.doc_processor.note_reference_repair import (
    repair_note_reference,
)


def _review(body="A re-<em>acquired</em> item."):
    return DocumentReview(
        '<main><span id="doc-page-4" class="pagebreak"></span>'
        f'<p id="body">{body}</p>'
        '<p id="other">Other<a role="doc-noteref" href="#old">1</a>.</p></main>'
        '<section role="doc-endnotes"><aside id="old" role="doc-endnote">Old note.</aside>'
        '<aside id="note" role="doc-endnote">A cited note.</aside></section>'
    )


def _args(review):
    return {
        "element_id": next(
            n.element_id for n in review._nodes if n.tag.get("id") == "body"
        ),
        "note_element_id": next(
            n.element_id for n in review._nodes if n.tag.get("id") == "note"
        ),
        "after_text": "re-acquired",
        "label": "7",
    }


def test_inserts_one_reference_across_inline_text_and_keeps_original_untouched():
    review = _review()
    before = str(review._soup)
    output, summary = repair_note_reference(review, **_args(review))
    soup = BeautifulSoup(output, "html.parser")
    assert str(review._soup) == before
    assert soup.find(id="body").get_text() == "A re-acquired7 item."
    assert str(soup.find(id="old")) == str(review._soup.find(id="old"))
    assert str(soup.find(id="note")) == str(review._soup.find(id="note"))
    assert soup.find(id="other").a["href"] == "#old"
    assert soup.find(id="doc-page-4") is not None
    assert soup.find(id=summary["reference_id"])["href"] == "#note"
    assert summary["reference_page_numbers"] == [4]
    revised = DocumentReview(output)
    with pytest.raises(ValueError, match="already follows"):
        repair_note_reference(revised, **_args(revised))


@pytest.mark.parametrize(
    "body",
    [
        "re-acquired and re-acquired",
        "No matching text.",
        '<a href="#old">re-acquired</a>',
        're-<span class="pagebreak" id="doc-page-5"></span>acquired',
        "re-<br>acquired",
        "<span hidden>re-acquired</span>",
        "<code>re-acquired</code>",
        "re-<code>acq</code>uired",
    ],
)
def test_ambiguous_missing_or_protected_anchor_rejected(body):
    review = _review(body)
    with pytest.raises(ValueError, match="match exactly once"):
        repair_note_reference(review, **_args(review))


def test_target_must_be_a_unique_existing_note_and_label_is_plain_text():
    review = _review()
    args = _args(review)
    with pytest.raises(ValueError, match="existing note body"):
        repair_note_reference(review, **{**args, "note_element_id": args["element_id"]})
    output, _summary = repair_note_reference(review, **{**args, "label": "<7>"})
    soup = BeautifulSoup(output, "html.parser")
    assert soup.find(id="body").a.get_text() == "<7>"
    assert soup.find(id="body").a.find(True) is None


def test_generic_edit_still_cannot_insert_a_note_reference():
    review = _review("A re-acquired item.")
    with pytest.raises(ValueError, match="protected"):
        review.apply_edits(
            [
                {
                    "element_id": _args(review)["element_id"],
                    "replacement_html": '<p id="body">A re-acquired<a role="doc-noteref" href="#note">7</a> item.</p>',
                }
            ]
        )
