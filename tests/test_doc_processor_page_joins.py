"""Page joins respect the script at the seam, and split table rows keep columns."""

from __future__ import annotations

import pytest
from bs4 import BeautifulSoup, Tag

from carlina.core.language import (
    join_separator,
    seam_whitespace_is_spurious,
)
from iarf_assistant.addons.doc_processor.document_review import DocumentReview
from iarf_assistant.addons.doc_processor.page_boundary_repair import (
    repair_page_boundaries,
)
from iarf_assistant.addons.doc_processor.parsers.assembly_report import (
    AssemblyReport,
    assembly_check_projection,
    finish_assembly_report,
)
from iarf_assistant.addons.doc_processor.parsers.pdf_utils import (
    concatenate_html_pages,
    remove_source_toc_blocks,
)


def _page(main: str) -> str:
    return f"<main_content>{main}</main_content><footnotes></footnotes>"


def _assemble(*pages: str, report: AssemblyReport | None = None) -> BeautifulSoup:
    html = concatenate_html_pages(
        [_page(page) for page in pages], assembly_report=report
    )
    return BeautifulSoup(html, "html.parser")


def _text_without_markers(tag: Tag) -> str:
    return "".join(
        str(node)
        for node in tag.find_all(string=True)
        if not any(
            "pagebreak" in (parent.get("class") or ())
            for parent in node.parents
            if isinstance(parent, Tag)
        )
    )


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        ("the quick", "brown fox", " "),
        ("取敢へ", "ずこれを記す", ""),
        ("IARF", "の会議", ""),
        ("会議。", "Next", ""),
        ("The term", "漢字 means", " "),  # a cited CJK word keeps its space
        ("called 漢字", "in Japanese", " "),
        ("会議の名称はIARF", "の会議", ""),
        ("ภาษา", "ไทย", ""),
        ("한국", "사람", " "),
        ("word", ", and", ""),
        ("\uff37\uff4f\uff52\uff44", "\uff53", ""),  # fullwidth Latin
        ("", "anything", ""),
    ],
)
def test_join_separator_follows_the_script_at_the_seam(
    left: str, right: str, expected: str
) -> None:
    assert join_separator(left, right) == expected


def test_spurious_seam_whitespace_only_inside_unspaced_text() -> None:
    assert seam_whitespace_is_spurious("取敢へ ", "ず")
    assert not seam_whitespace_is_spurious("the ", "end")
    assert not seam_whitespace_is_spurious("取敢へ", "ず")
    assert not seam_whitespace_is_spurious("The term ", "漢字 means")


def test_japanese_paragraph_joins_without_inventing_a_space() -> None:
    report = AssemblyReport()
    soup = _assemble(
        "<p>今は取敢へ</p>", '<p class="continued">ずこれを記す。</p>', report=report
    )
    paragraph = soup.find("p")
    assert isinstance(paragraph, Tag)
    assert _text_without_markers(paragraph) == "今は取敢へずこれを記す。"
    assert paragraph.find(id="doc-page-2") is not None
    assert report.joins["unspaced"] == 1


def test_english_and_mixed_script_joins() -> None:
    english = _assemble(
        "<p>World Conference on</p>", '<p class="continued">Religion and Peace</p>'
    )
    mixed = _assemble(
        "<p>会議の名称は World</p>", '<p class="continued">Conference です。</p>'
    )
    assert _text_without_markers(english.p) == "World Conference on Religion and Peace"
    assert _text_without_markers(mixed.p) == "会議の名称は World Conference です。"


def test_parallel_streams_continue_in_their_own_column() -> None:
    report = AssemblyReport()
    soup = _assemble(
        "<table><tr><td>指導者と</td><td>鈴木大拙と会う</td></tr></table>",
        '<table class="continued"><tr class="continued">'
        '<td class="continued">会見し</td><td>新しい出来事</td></tr></table>',
        report=report,
    )
    rows = soup.find_all("tr")
    assert len(rows) == 1
    upper, lower = rows[0].find_all("td", recursive=False)
    assert _text_without_markers(upper) == "指導者と会見し"
    assert [p.get_text() for p in lower.find_all("p")] == [
        "鈴木大拙と会う",
        "新しい出来事",
    ]
    assert upper.find(id="doc-page-2") is not None
    assert report.table_rows["column_aligned"] == 1


def test_split_row_merges_every_column() -> None:
    soup = _assemble(
        "<table><tr><th>A</th><th>B</th><th>C</th></tr>"
        "<tr><td>alpha one</td><td>beta one</td><td>gamma one</td></tr></table>",
        '<table class="continued"><tr class="continued"><td class="continued">alpha two</td>'
        '<td class="continued">beta two</td><td></td></tr><tr><td>x</td><td>y</td><td>z</td></tr></table>',
    )
    rows = soup.find_all("tr")
    assert len(rows) == 3
    cells = rows[1].find_all("td", recursive=False)
    assert [_text_without_markers(cell) for cell in cells] == [
        "alpha one alpha two",
        "beta one beta two",
        "gamma one",
    ]


def test_lone_continued_cell_keeps_the_historical_last_column_contract() -> None:
    report = AssemblyReport()
    soup = _assemble(
        "<table><thead><tr><th>Item</th><th>Desc</th></tr></thead>"
        "<tbody><tr><td>Apples</td><td>These are very</td></tr></tbody></table>",
        '<table class="continued"><tbody><tr class="continued"><td class="continued">sweet and juicy.</td></tr>'
        "<tr><td>Bananas</td><td>Also good.</td></tr></tbody></table>",
        report=report,
    )
    body_rows = soup.find("tbody").find_all("tr", recursive=False)
    assert [len(row.find_all("td")) for row in body_rows] == [2, 2]
    assert (
        _text_without_markers(body_rows[0].find_all("td")[1])
        == "These are very sweet and juicy."
    )
    assert report.table_rows["single_cell"] == 1


def test_mismatched_split_row_stays_separate_and_is_reported() -> None:
    report = AssemblyReport()
    soup = _assemble(
        "<table><tr><td>a</td><td>b</td><td>c</td></tr></table>",
        '<table class="continued"><tr class="continued"><td class="continued">x</td><td>y</td></tr></table>',
        report=report,
    )
    rows = soup.find_all("tr")
    assert len(rows) == 2
    assert [len(row.find_all("td")) for row in rows] == [3, 2]
    assert report.ambiguous_table_boundaries == [(1, 2)]
    inputs = report.to_inputs()
    check = finish_assembly_report(inputs, str(soup))
    assert check is not None
    assert "table_row_continuation_ambiguous" in check["advisories"]
    assert check["ambiguous_table_boundaries"] == [[1, 2]]


def test_completion_check_names_lost_markup_and_seam_spaces() -> None:
    report = AssemblyReport()
    pages = [
        _page("<p>彼は<ruby>漢字<rt>かんじ</rt></ruby>を</p>"),
        _page('<p class="continued">読む。</p>'),
    ]
    concatenate_html_pages(pages, assembly_report=report)
    flattened = '<main><p>彼は漢字かんじを <span class="pagebreak" id="doc-page-2"></span>読む。</p></main>'
    check = finish_assembly_report(report.to_inputs(), flattened)
    assert check is not None
    assert check["lost_elements"] == {"rt": 1, "ruby": 1}
    assert check["spurious_seam_spaces"] == 1
    assert check["letters"] == {"missing": 0, "added": 0}
    assert set(check["advisories"]) == {
        "inline_elements_lost",
        "space_inside_unspaced_text_at_page_join",
    }
    projection = assembly_check_projection(check)
    assert projection["status"] == "advisories"
    assert "review" in projection


def test_consistent_assembly_projection_stays_small() -> None:
    report = AssemblyReport()
    html = concatenate_html_pages(
        [_page("<p>One <em>page</em>.</p>"), _page("<p>Two.</p>")],
        assembly_report=report,
    )
    check = finish_assembly_report(report.to_inputs(), html)
    assert check is not None
    assert check["status"] == "consistent"
    assert assembly_check_projection(check) == {"status": "consistent"}
    assert finish_assembly_report({"version": 0}, html) is None


def test_omitted_contents_fallbacks_and_note_labels_are_not_reported_lost() -> None:
    report = AssemblyReport()
    report.observe_page(
        '<h2 class="source-toc-title">Contents</h2>'
        '<ol class="source-toc"><li>Chapter one</li></ol>'
        "<p>彼は<ruby>漢字<rp>(</rp><rt>かんじ</rt><rp>)</rp></ruby>を読む"
        '<a class="footnote-ref" href="#n1">a</a>。</p>',
        remove_source_toc=remove_source_toc_blocks,
    )
    canonical = (
        "<main><p>彼は<ruby>漢字<rt>かんじ</rt></ruby>を読む"
        '<a class="footnote-ref" href="#n1">b</a>。</p></main>'
    )
    check = finish_assembly_report(report.to_inputs(), canonical)
    assert check is not None
    assert check["status"] == "consistent"


def _marker(page: int = 2) -> str:
    return f'<span class="pagebreak" id="doc-page-{page}" role="doc-pagebreak" title="{page}"></span>'


def test_boundary_repair_suggests_unspaced_join_for_japanese_paragraphs() -> None:
    html = f"<p>今は取敢へ</p>{_marker()}<p>ずこれを記す。</p>"
    _, report = repair_page_boundaries(DocumentReview(html))
    candidate = report["candidates"][0]
    assert candidate["suggested_action"] == "join_without_space"
    assert candidate["preview"] == "今は取敢へずこれを記す。"
    repaired, _ = repair_page_boundaries(
        DocumentReview(html),
        decisions=[{"id": candidate["id"], "action": "join_without_space"}],
    )
    assert repaired is not None
    assert (
        BeautifulSoup(repaired, "html.parser").p.get_text()
        == "今は取敢へずこれを記す。"
    )


def test_boundary_repair_removes_a_space_left_inside_joined_text() -> None:
    html = f"<p>今は取敢へ {_marker()}ずこれを記す。</p><p>English text {_marker(3)}continues.</p>"
    _, report = repair_page_boundaries(DocumentReview(html))
    assert [c["suggested_action"] for c in report["candidates"]] == ["remove_space"]
    candidate = report["candidates"][0]
    assert candidate["page_number"] == 2
    repaired, _ = repair_page_boundaries(
        DocumentReview(html),
        decisions=[{"id": candidate["id"], "action": "remove_space"}],
    )
    assert repaired is not None
    soup = BeautifulSoup(repaired, "html.parser")
    assert soup.p.get_text() == "今は取敢へずこれを記す。"
    assert soup.find(id="doc-page-2") is not None
    assert soup.find_all("p")[1].get_text() == "English text continues."
    with pytest.raises(ValueError, match="remove_space"):
        repair_page_boundaries(
            DocumentReview(html),
            decisions=[{"id": candidate["id"], "action": "join_with_space"}],
        )


def test_boundary_review_names_a_space_inside_unspaced_text() -> None:
    html = f"<main><p>前のページ{_marker(1)}今は取敢へ {_marker()}ずこれを記す。</p></main>"
    items = DocumentReview(html).boundaries()["items"]
    seam = next(item for item in items if item["page_number"] == 2)
    assert seam["joined_within_element"] is True
    assert "space_inside_unspaced_text" in seam["warnings"]


def test_accept_suggested_repairs_every_matching_seam_in_one_save() -> None:
    html = (
        f"<p>一つ {_marker()}二つ</p><p>三つ {_marker(3)}四つ</p>"
        f"<p>The end</p>{_marker(4)}<p>of it</p>"
    )
    repaired, report = repair_page_boundaries(
        DocumentReview(html), accept_suggested=["remove_space"]
    )
    assert repaired is not None
    assert report["accepted"] == {"remove_space": 2}
    soup = BeautifulSoup(repaired, "html.parser")
    assert [p.get_text() for p in soup.find_all("p")] == [
        "一つ二つ",
        "三つ四つ",
        "The end",
        "of it",
    ]
    with pytest.raises(ValueError, match="either decisions or accept_suggested"):
        repair_page_boundaries(
            DocumentReview(html),
            decisions=[{"id": report["candidates"][0]["id"], "action": "remove_space"}],
            accept_suggested=["remove_space"],
        )
    for individual in ("keep_hyphen", "dehyphenate"):
        with pytest.raises(ValueError, match="accept_suggested lists"):
            repair_page_boundaries(DocumentReview(html), accept_suggested=[individual])


def test_space_removal_follows_its_seam_into_a_joined_paragraph() -> None:
    html = (
        f"<main><p>前のページの続き</p>{_marker(2)}"
        f"<p>次の文は {_marker(3)}ここで終わる。</p></main>"
    )
    repaired, report = repair_page_boundaries(
        DocumentReview(html),
        accept_suggested=["join_without_space", "remove_space"],
    )
    assert repaired is not None
    assert report["accepted"] == {"join_without_space": 1, "remove_space": 1}
    soup = BeautifulSoup(repaired, "html.parser")
    assert [p.get_text() for p in soup.find_all("p")] == [
        "前のページの続き次の文はここで終わる。"
    ]
    assert soup.find(id="doc-page-3") is not None
