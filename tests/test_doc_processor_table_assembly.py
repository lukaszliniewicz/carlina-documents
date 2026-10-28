"""Continued tables retain physical boundaries, rows and linked notes."""

from __future__ import annotations

import hashlib
import io
import zipfile
from pathlib import PurePosixPath

import pytest
from bs4 import BeautifulSoup, Tag

from carlina.core.semantic_export import SemanticExportRequest
from iarf_assistant.addons.doc_processor.document_review import DocumentReview
from iarf_assistant.addons.doc_processor.parsers.pdf_utils import concatenate_html_pages
from iarf_assistant.addons.doc_processor.parsers.table_assembly import (
    merge_continued_table,
)
from iarf_assistant.addons.doc_processor.semantic_export import (
    DocumentResourceResolver,
    export_semantic_document,
)


def _page(
    number: int,
    rows: int,
    *,
    header: str = "thead",
    continued: bool = False,
    nested: bool = False,
) -> str:
    title = "<tr><th>Record</th><th>Value</th></tr>"
    entries = "".join(
        f"<tr><td>P{number}R{row}</td><td>value-{number}-{row}"
        + ("<span class='footnote_marker' id='fm1'>1</span>" if row == 1 else "")
        + "</td></tr>"
        for row in range(1, rows + 1)
    )
    if header == "direct":
        content = title + entries
    elif header == "inline":
        content = f"<tbody>{title}{entries}</tbody>"
    else:
        content = f"<thead>{title}</thead>" if header != "none" else ""
        content += f"<tbody>{entries}</tbody>"
        if header == "caption":
            content = "<caption>Synthetic records</caption>" + content
        elif header == "colgroup":
            content = "<colgroup><col/><col/></colgroup>" + content
    classes = "continued continues" if continued else "continues"
    table = f"<table class='{classes}'>{content}</table>"
    if nested:
        table = f"<blockquote class='{classes}'>{table}</blockquote>"
    note = (
        f"<p class='footnote' id='f1'>Synthetic note from page {number}.</p>"
        if rows
        else ""
    )
    return f"<main_content>{table}</main_content><footnotes>{note}</footnotes>"


def _assert_page_markers(soup: BeautifulSoup, pages: list[int]) -> None:
    markers = soup.select("span.pagebreak")
    assert [marker.get("id") for marker in markers] == [
        f"doc-page-{page}" for page in pages
    ]
    for marker, page in zip(markers, pages, strict=True):
        assert marker.get("role") == "doc-pagebreak"
        assert marker.get("epub:type") == "pagebreak"
        assert marker.get("title") == str(page)
        assert marker.get("aria-label") == str(page)


def _assert_row_boundary(soup: BeautifulSoup, page: int) -> None:
    marker = soup.find(id=f"doc-page-{page}")
    assert isinstance(marker, Tag)
    cell = marker.find_parent(["td", "th"])
    assert isinstance(cell, Tag)
    assert cell.get_text(strip=True) == f"P{page}R1"
    assert marker is cell.contents[0]
    assert marker.find_parent("thead") is None


@pytest.mark.parametrize(
    "header", ["thead", "inline", "direct", "caption", "colgroup", "none"]
)
def test_absorbed_table_pages_keep_rows_markers_and_note_links(header: str) -> None:
    counts = {394: 1, 395: 6, 396: 5}
    pages = list(counts)
    output = concatenate_html_pages(
        [
            _page(page, rows, header=header, continued=page != 394)
            for page, rows in counts.items()
        ],
        page_numbers=pages,
    )
    soup = BeautifulSoup(output, "html.parser")

    _assert_page_markers(soup, pages)
    assert len(soup.find_all("table")) == 1
    rows = [row for row in soup.select("table tr") if row.find("td", recursive=False)]
    assert [row.find("td", recursive=False).get_text(strip=True) for row in rows] == [
        f"P{page}R{row}"
        for page, count in counts.items()
        for row in range(1, count + 1)
    ]
    assert len(soup.select("table tr:has(th)")) == (0 if header == "none" else 1)
    for page in (395, 396):
        _assert_row_boundary(soup, page)
    refs = soup.select("table a.footnote-ref")
    assert len(refs) == len(pages)
    assert len({ref.get("href") for ref in refs}) == len(pages)
    for ref, page in zip(refs, pages, strict=True):
        note = soup.find(id=str(ref.get("href"))[1:])
        assert isinstance(note, Tag)
        assert f"Synthetic note from page {page}." in note.get_text(" ", strip=True)
    assert "data-docproc-source-page" not in output


def test_nested_continued_table_keeps_the_absorbed_boundary() -> None:
    output = concatenate_html_pages(
        [_page(395, 1, nested=True), _page(396, 1, nested=True, continued=True)],
        page_numbers=[395, 396],
    )
    soup = BeautifulSoup(output, "html.parser")

    _assert_page_markers(soup, [395, 396])
    _assert_row_boundary(soup, 396)
    assert len(soup.find_all("blockquote")) == 1
    assert len(soup.find_all("table")) == 1


def test_discarded_markers_keep_order_and_nested_markers_stay_put() -> None:
    soup = BeautifulSoup(
        "<table id='first'><thead><tr><th>Record</th></tr></thead>"
        "<tbody><tr><td>Original record</td></tr></tbody></table>"
        "<table id='second'><caption><span class='pagebreak' id='doc-page-395'>"
        "</span>Repeated caption</caption><thead><tr><th>"
        "<span class='pagebreak' id='doc-page-396'></span>Record</th></tr></thead>"
        "<tbody><tr><td>Incoming record<table><tbody><tr><td>Nested before"
        "<span class='pagebreak' id='doc-page-397'></span>Nested after</td></tr>"
        "</tbody></table></td></tr></tbody></table>",
        "html.parser",
    )
    first, second = soup.find(id="first"), soup.find(id="second")
    nested_marker = soup.find(id="doc-page-397")
    assert isinstance(first, Tag)
    assert isinstance(second, Tag)
    assert isinstance(nested_marker, Tag)
    nested_parent = nested_marker.parent

    merge_continued_table(
        first,
        second,
        row_continues=lambda _previous, _following: False,
        merge_row=lambda _previous, _following: None,
    )

    markers = first.select("span.pagebreak")
    assert [marker.get("id") for marker in markers] == [
        "doc-page-395",
        "doc-page-396",
        "doc-page-397",
    ]
    assert soup.find(id="second") is None
    assert len(first.find_all("thead")) == 1
    assert len(first.find_all("caption")) == 0
    assert nested_marker.parent is nested_parent
    assert isinstance(nested_parent, Tag)
    assert nested_parent.contents[0] == "Nested before"
    assert nested_parent.contents[-1] == "Nested after"
    incoming = nested_marker.find_parent("table").find_parent("td")
    assert isinstance(incoming, Tag)
    assert incoming.contents[:2] == markers[:2]
    assert incoming.contents[2] == "Incoming record"


def test_removed_nested_header_keeps_marker_and_its_outer_data_row() -> None:
    output = concatenate_html_pages(
        [
            "<main_content><table class='continues'><thead><tr><th>Record</th>"
            "</tr></thead><tbody><tr><td>Original record</td></tr></tbody>"
            "</table></main_content>",
            "<main_content><table class='continued'><tbody><tr><td><table>"
            "<thead><tr><th>Record</th></tr></thead><tbody><tr><td>Nested record"
            "</td></tr></tbody></table></td></tr></tbody></table></main_content>",
        ],
        page_numbers=[395, 396],
    )
    soup = BeautifulSoup(output, "html.parser")

    _assert_page_markers(soup, [395, 396])
    table = soup.find("table")
    assert isinstance(table, Tag)
    tbody = table.find("tbody", recursive=False)
    assert isinstance(tbody, Tag)
    rows = tbody.find_all("tr", recursive=False)
    assert len(rows) == 2
    assert rows[0].get_text(strip=True) == "Original record"
    assert rows[1].get_text(strip=True) == "Nested record"
    assert len(table.find_all("thead")) == 1
    cell = rows[1].find("td", recursive=False)
    assert isinstance(cell, Tag)
    assert cell.contents[0] is soup.find(id="doc-page-396")
    assert isinstance(cell.contents[1], Tag)
    assert cell.contents[1].name == "table"


def test_split_table_row_keeps_boundary_between_original_and_continued_text() -> None:
    pages = [
        "<main_content><table class='continues'><thead><tr><th>Record</th></tr></thead>"
        "<tbody><tr class='continues'><td class='continues'>Original part</td></tr>"
        "</tbody></table></main_content>",
        "<main_content><table class='continued'><thead><tr><th>Record</th></tr></thead>"
        "<tbody><tr class='continued'><td class='continued'>continued part</td></tr>"
        "<tr><td>Next record</td></tr></tbody></table></main_content>",
    ]
    output = concatenate_html_pages(pages, page_numbers=[395, 396])
    soup = BeautifulSoup(output, "html.parser")

    _assert_page_markers(soup, [395, 396])
    rows = soup.select("table tbody > tr")
    assert len(rows) == 2
    cell = rows[0].find("td")
    assert isinstance(cell, Tag)
    assert cell.get_text(" ", strip=True) == "Original part continued part"
    contents = cell.decode_contents()
    assert (
        contents.index("Original part")
        < contents.index("doc-page-396")
        < contents.index("continued part")
    )


def test_header_only_continuation_keeps_marker_order_without_inventing_rows() -> None:
    output = concatenate_html_pages(
        [_page(394, 1), _page(395, 0, continued=True), _page(396, 1, continued=True)],
        page_numbers=[394, 395, 396],
    )
    soup = BeautifulSoup(output, "html.parser")

    _assert_page_markers(soup, [394, 395, 396])
    assert len(soup.find_all("table")) == 1
    rows = soup.select("table tbody > tr")
    assert len(rows) == 2
    marker = soup.find(id="doc-page-395")
    assert isinstance(marker, Tag)
    assert marker.find_parent("tr") is rows[0]
    _assert_row_boundary(soup, 396)


@pytest.mark.parametrize("caption", [False, True])
def test_empty_table_retains_header_only_boundary_before_later_rows(
    caption: bool,
) -> None:
    decoration = "<caption>Synthetic records</caption>" if caption else ""
    output = concatenate_html_pages(
        [
            f"<main_content><table class='continues'>{decoration}</table></main_content>",
            _page(395, 0, continued=True),
            _page(396, 1, continued=True),
        ],
        page_numbers=[394, 395, 396],
    )
    soup = BeautifulSoup(output, "html.parser")

    _assert_page_markers(soup, [394, 395, 396])
    assert len(soup.find_all("table")) == 1
    assert len(soup.select("table tr")) == 1
    assert len(soup.select("table td")) == 2
    assert len(soup.find_all("caption")) == int(caption)
    marker = soup.find(id="doc-page-395")
    assert isinstance(marker, Tag)
    assert marker.parent is (soup.caption if caption else soup.main)
    _assert_row_boundary(soup, 396)


def test_page_scoped_review_and_epub_keep_absorbed_table_provenance() -> None:
    counts = {394: 1, 395: 6, 396: 5}
    output = concatenate_html_pages(
        [_page(page, rows, continued=page != 394) for page, rows in counts.items()],
        page_numbers=list(counts),
    )
    review = DocumentReview(output)
    for page in counts:
        result = review.search(f"P{page}R1", page_number=page, scope="body")
        assert result["total_matches"] == 1
        assert result["items"][0]["page_numbers"] == [page]
        other_page = 396 if page == 395 else 395
        assert (
            review.search(f"P{page}R1", page_number=other_page, scope="body")[
                "total_matches"
            ]
            == 0
        )

    request = SemanticExportRequest(
        semantic_html=output,
        semantic_html_sha256=hashlib.sha256(output.encode()).hexdigest(),
        metadata={"title": "Synthetic table provenance", "language": "en"},
        resources=(),
        format="epub",
        include_source_toc=False,
        profile_id="semantic-default",
        profile_version="1",
    )
    artifact = export_semantic_document(
        request, resolver=DocumentResourceResolver(user_id=1)
    )
    assert artifact.receipt.media_type == "application/epub+zip"
    assert artifact.receipt.sha256 == hashlib.sha256(artifact.data).hexdigest()
    with zipfile.ZipFile(io.BytesIO(artifact.data)) as archive:
        nav = BeautifulSoup(archive.read("EPUB/nav.xhtml"), "xml")
        page_list = nav.find("nav", id="page-list")
        assert isinstance(page_list, Tag)
        links = page_list.find_all("a")
        assert [link.get_text() for link in links] == [str(page) for page in counts]
        for link, page in zip(links, counts, strict=True):
            filename, fragment = str(link["href"]).split("#")
            content_path = PurePosixPath("EPUB") / filename
            content = BeautifulSoup(archive.read(str(content_path)), "xml")
            assert fragment == f"doc-page-{page}"
            marker = content.find(id=fragment)
            assert isinstance(marker, Tag)
            if page != 394:
                _assert_row_boundary(content, page)
            rows = [row for row in content.find_all("tr") if row.find("td")]
            assert len(rows) == sum(counts.values())
            refs = content.select("table a.footnote-ref")
            assert len(refs) == len(counts)
            for ref, note_page in zip(refs, counts, strict=True):
                note_file, note_fragment = str(ref["href"]).split("#")
                note_content = BeautifulSoup(
                    archive.read(str(content_path.parent / note_file)), "xml"
                )
                note = note_content.find(id=note_fragment)
                assert isinstance(note, Tag)
                assert f"Synthetic note from page {note_page}." in note.get_text(
                    " ", strip=True
                )


@pytest.mark.parametrize("reason", ["separate", "gap"])
def test_unjoined_tables_keep_headers_and_page_markers(reason: str) -> None:
    pages = [395, 397 if reason == "gap" else 396]
    flags = {394: {"main_content": "separate"}} if reason == "separate" else None
    output = concatenate_html_pages(
        [_page(pages[0], 1), _page(pages[1], 1, continued=True)],
        page_numbers=pages,
        boundary_flags=flags,
    )
    soup = BeautifulSoup(output, "html.parser")

    _assert_page_markers(soup, pages)
    assert len(soup.find_all("table")) == 2
    assert len(soup.find_all("thead")) == 2
    for page in pages:
        marker = soup.find(id=f"doc-page-{page}")
        assert isinstance(marker, Tag)
        assert marker.parent is soup.main
