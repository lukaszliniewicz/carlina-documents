from __future__ import annotations

import pytest
from bs4 import BeautifulSoup

from iarf_assistant.addons.doc_processor.document_review import DocumentReview
from iarf_assistant.addons.doc_processor.page_boundary_repair import repair_page_boundaries
from iarf_assistant.addons.doc_processor.review_history import inverse_splices, restore_splices


def marker(page=2):
    return f'<span class="pagebreak" id="doc-page-{page}" role="doc-pagebreak" title="{page}"></span>'


def scan(html, **kwargs):
    return repair_page_boundaries(DocumentReview(html), **kwargs)


def apply_all(html, action=None):
    _, report = scan(html)
    decisions = [{"id": c["id"], "action": action or c["suggested_action"]} for c in report["candidates"]]
    assert decisions
    return scan(html, decisions=decisions)


def test_scan_never_changes_source_and_ids_are_stable():
    html = f'<p>Menschenhände ge-</p>\n{marker()}\n<p>macht haben.</p>'
    candidate, report = scan(html)
    assert candidate is None
    assert report["candidate_count"] == 1
    assert report["candidates"][0]["suggested_action"] == "dehyphenate"
    assert report["candidates"] == scan(html)[1]["candidates"]


@pytest.mark.parametrize("placement", ["between", "left", "right"])
def test_dehyphenate_keeps_physical_provenance_and_exact_undo(placement):
    boundary = marker()
    left = "Menschenhände <em>ge-</em>  " + (boundary if placement == "left" else "")
    right = (boundary if placement == "right" else "") + "  macht haben."
    html = f'<p id="first">{left}</p>{boundary if placement == "between" else ""}<p id="second">{right}</p>'
    repaired, report = apply_all(html)
    soup = BeautifulSoup(repaired, "html.parser")
    assert len(soup.find_all("p")) == 1
    assert soup.p.get_text() == "Menschenhände gemacht haben."
    assert str(soup.find(id="doc-page-2")) == str(BeautifulSoup(boundary, "html.parser").span)
    assert soup.find(id="second") is not None
    assert soup.em.get_text() == "ge"
    assert report["selected_count"] == 1
    assert restore_splices(repaired, inverse_splices(html, repaired)) == html


def test_sentence_continuation_allows_german_capitalized_noun():
    html = f'<p>das Wohl Ihrer Gemeinden, die</p>{marker()}<p>Ehre, die Freiheit bewahren.</p>'
    repaired, report = apply_all(html)
    assert report["candidates"][0]["suggested_action"] == "join_with_space"
    assert BeautifulSoup(repaired, "html.parser").p.get_text() == "das Wohl Ihrer Gemeinden, die Ehre, die Freiheit bewahren."


@pytest.mark.parametrize("action,expected", [("keep_hyphen", "Kultur-geschichte"), ("dehyphenate", "Kulturgeschichte"), ("join_with_space", "Kultur- geschichte")])
def test_printed_hyphen_requires_explicit_editorial_choice(action, expected):
    repaired, _ = apply_all(f'<p>Kultur-</p>{marker()}<p>geschichte</p>', action)
    assert BeautifulSoup(repaired, "html.parser").p.get_text() == expected


def test_soft_hyphen_can_be_removed():
    repaired, _ = apply_all(f'<p>ge\u00ad</p>{marker()}<p>macht</p>')
    assert BeautifulSoup(repaired, "html.parser").p.get_text() == "gemacht"


def test_multiple_page_chain_is_one_consistent_candidate_with_all_markers():
    html = f'<p>first,</p>{marker(2)}<p>second,</p>{marker(3)}<p>third.</p>'
    repaired, report = apply_all(html)
    soup = BeautifulSoup(repaired, "html.parser")
    assert len(soup.find_all("p")) == 1
    assert soup.p.get_text() == "first, second, third."
    assert [s["id"] for s in soup.select(".pagebreak")] == ["doc-page-2", "doc-page-3"]
    assert report["selected_count"] == 2


@pytest.mark.parametrize("wrapper", ["blockquote", "aside", "li", "td", "pre", "figure"])
def test_protected_context_is_never_joined(wrapper):
    html = f'<{wrapper}><p>ge-</p>{marker()}<p>macht</p></{wrapper}>'
    assert scan(html)[1]["candidate_count"] == 0


@pytest.mark.parametrize("middle", ['<h2>Chapter</h2>', '<p class="page_header">Running title</p>', '<aside role="doc-footnote">Note.</aside>', '<!-- do not relocate -->'])
def test_intervening_structures_are_not_crossed(middle):
    html = f'<p>ge-</p>{middle}{marker()}<p>macht</p>'
    assert scan(html)[1]["candidate_count"] == 0


@pytest.mark.parametrize("html", [
    f'<p>Complete.</p>{marker()}<p>Next.</p>',
    f'<p lang="de">ge-</p>{marker()}<p lang="en">macht</p>',
    f'<section><p>ge-</p></section>{marker()}<section><p>macht</p></section>',
    f'<p>ge<br/>-</p>{marker()}<p>macht</p>',
    f'<p>already {marker()} joined</p>',
    f'<p class="verse">ge-</p>{marker()}<p class="verse">macht</p>',
])
def test_ambiguous_layouts_fail_closed(html):
    assert scan(html)[1]["candidate_count"] == 0


def test_links_notes_and_reference_targets_survive():
    html = f'<p>Text <a href="#note" role="doc-noteref">1</a>,</p>{marker()}<p id="target">continued <strong>here</strong>.</p><aside id="note" role="doc-footnote">Note body.</aside>'
    repaired, _ = apply_all(html)
    soup = BeautifulSoup(repaired, "html.parser")
    assert soup.find("a")["href"] == "#note"
    assert soup.find(id="note").get_text() == "Note body."
    assert soup.find(id="target") is not None
    assert soup.strong.get_text() == "here"


def test_pagination_and_scope_never_apply_unseen_candidates():
    html = f'<p>first,</p>{marker(2)}<p>second,</p>{marker(3)}<p>third.</p>'
    _, report = scan(html, limit=1)
    assert report["candidate_count"] == 2 and report["has_more"]
    decisions = [{"id": report["candidates"][0]["id"], "action": "join_with_space"}]
    repaired, report = scan(html, decisions=decisions)
    assert report["selected_count"] == 1
    assert len(BeautifulSoup(repaired, "html.parser").find_all("p")) == 2
    assert scan(html, page_numbers=[3])[1]["candidate_count"] == 1


@pytest.mark.parametrize("options", [{"offset": True}, {"limit": 51}, {"page_numbers": [True]}, {"page_numbers": []}, {"decisions": []}, {"decisions": [{"id": "pb-" + "x" * 24, "action": "dehyphenate"}]}])
def test_invalid_or_stale_selections_fail_closed(options):
    with pytest.raises(ValueError):
        scan(f'<p>ge-</p>{marker()}<p>macht</p>', **options)


def test_non_hyphen_continuation_cannot_delete_a_letter():
    html = f'<p>first,</p>{marker()}<p>second.</p>'
    _, report = scan(html)
    with pytest.raises(ValueError, match="Hyphen"):
        scan(html, decisions=[{"id": report["candidates"][0]["id"], "action": "dehyphenate"}])
