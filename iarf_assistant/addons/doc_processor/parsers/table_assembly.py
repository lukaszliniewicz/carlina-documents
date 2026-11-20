"""Assemble continued HTML tables while retaining physical page markers."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from bs4 import BeautifulSoup, Tag

from .assembly_report import record_table_row

_BLOCK_TAGS = frozenset(
    {
        "blockquote",
        "div",
        "dl",
        "figure",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "ol",
        "p",
        "pre",
        "table",
        "ul",
    }
)


def _place_page_markers(
    first: Tag, rows: Sequence[Tag], markers: Sequence[Tag]
) -> None:
    """Put discarded markers before incoming text or after the preceding data."""
    incoming_cell = next(
        (
            cells[0]
            for row in rows
            if (cells := row.find_all(["td", "th"], recursive=False))
        ),
        None,
    )
    if incoming_cell is not None:
        for marker in reversed(markers):
            incoming_cell.insert(0, marker)
        return

    target_cell = next(
        (
            cell
            for cell in reversed(first.find_all(["td", "th"]))
            if cell.find_parent("table") is first
        ),
        None,
    )
    destination = (
        target_cell
        if target_cell is not None
        else first.find("caption", recursive=False)
    )
    for marker in markers:
        if isinstance(destination, Tag):
            destination.append(marker)
        else:
            first.insert_before(marker)


def merge_continued_table(
    first: Tag,
    second: Tag,
    *,
    row_continues: Callable[[Tag, Tag], bool],
    merge_row: Callable[[Tag, Tag], None],
) -> None:
    """Merge one continued table into its predecessor without losing pagebreaks."""

    target_container = first.find("tbody") or first
    source_container = second.find("tbody") or second
    page_markers = list(second.find_all("span", class_="pagebreak"))

    repeated_thead = second.find("thead")
    removed_thead_marker_ids: set[int] = set()
    if repeated_thead:
        removed_thead_id = id(repeated_thead)
        removed_thead_marker_ids = {
            id(marker)
            for marker in page_markers
            if any(id(ancestor) == removed_thead_id for ancestor in marker.parents)
        }
        for marker in page_markers:
            if id(marker) in removed_thead_marker_ids:
                marker.extract()
        repeated_thead.decompose()

    rows1 = target_container.find_all("tr", recursive=False)
    rows2 = source_container.find_all("tr", recursive=False)

    # Strip repeated header rows that weren't in a thead, using the original
    # first-th text comparison exactly.
    repeated_header_row = None
    if rows2:
        incoming_header = rows2[0].find("th")
        existing_header = first.find("th")
        if (
            incoming_header
            and existing_header
            and existing_header.get_text().strip() == incoming_header.get_text().strip()
        ):
            repeated_header_row = rows2[0]

    retained_rows = rows2[1:] if repeated_header_row is not None else rows2
    retained_row_ids = {id(row) for row in retained_rows}
    pending_markers = [
        marker
        for marker in page_markers
        if id(marker) in removed_thead_marker_ids
        or not any(id(ancestor) in retained_row_ids for ancestor in marker.parents)
    ]

    # Detach all pending markers before removing the matching inline row.
    for marker in pending_markers:
        if id(marker) not in removed_thead_marker_ids:
            marker.extract()

    if repeated_header_row is not None:
        repeated_header_row.decompose()
        rows2 = rows2[1:]

    if pending_markers:
        _place_page_markers(first, rows2, pending_markers)

    if rows1 and rows2:
        last_r1 = rows1[-1]
        first_r2 = rows2[0]
        if row_continues(last_r1, first_r2) and merge_split_row(
            last_r1,
            first_r2,
            cell_continues=row_continues,
            join_cells=merge_row,
        ):
            rows2 = rows2[1:]

    for row in rows2:
        target_container.append(row)
    second.decompose()


def _direct_cells(row: Tag) -> list[Tag]:
    return list(row.find_all(["td", "th"], recursive=False))


def _span(cell: Tag) -> int:
    value = str(cell.get("colspan") or "1").strip()
    return int(value) if value.isdigit() and int(value) > 0 else 1


def _has_content(cell: Tag) -> bool:
    if cell.find(["img", "figure", "table"]) is not None:
        return True
    return any(
        str(text).strip()
        for text in cell.find_all(string=True)
        if not any(
            "pagebreak" in (parent.get("class") or ()) for parent in text.parents
        )
    )


def _block_children(cell: Tag) -> list[Tag]:
    children = [child for child in cell.contents if isinstance(child, Tag)]
    if children and all(child.name in _BLOCK_TAGS for child in children):
        return children
    return []


def _join_continued_cell(
    previous: Tag,
    incoming: Tag,
    join_cells: Callable[[Tag, Tag], None],
) -> None:
    """Join text cut mid-passage, keeping block boundaries inside the cells."""

    previous_blocks = _block_children(previous)
    incoming_blocks = _block_children(incoming)
    if previous_blocks and incoming_blocks:
        last, first = previous_blocks[-1], incoming_blocks[0]
        if last.name == first.name and last.name not in {"table", "figure"}:
            join_cells(last, first)
            if first.parent is incoming:
                first.extract()
        for child in list(incoming.contents):
            previous.append(child)
        incoming.decompose()
        return
    if previous_blocks or incoming_blocks:
        _append_cell_block(previous, incoming)
        return
    join_cells(previous, incoming)


def _append_cell_block(previous: Tag, incoming: Tag) -> None:
    """Add new content to the same column without joining it to earlier text."""

    if not _block_children(previous) and _has_content(previous):
        wrapper = _new_paragraph(previous)
        for child in list(previous.contents):
            wrapper.append(child.extract())
        previous.append(wrapper)
    if _block_children(incoming):
        for child in list(incoming.contents):
            previous.append(child.extract())
    else:
        wrapper = _new_paragraph(previous)
        for child in list(incoming.contents):
            wrapper.append(child.extract())
        previous.append(wrapper)
    incoming.decompose()


def _new_paragraph(tag: Tag) -> Tag:
    root = tag
    while root.parent is not None:
        root = root.parent
    if not isinstance(root, BeautifulSoup):
        raise TypeError("table-cell-without-document")
    return root.new_tag("p")


def merge_split_row(
    previous: Tag,
    incoming: Tag,
    *,
    cell_continues: Callable[[Tag, Tag], bool],
    join_cells: Callable[[Tag, Tag], None],
) -> bool:
    """Merge a row split across a page boundary column by column.

    Each incoming cell belongs to the column it occupies: a cell marked as
    continuing joins the text of the same column, other non-empty cells add
    new content to that column, and empty cells only hold the column open.
    A lone incoming cell keeps the historical contract and continues the last
    column. Any other shape is ambiguous: the incoming row stays separate and
    the boundary is reported for review. Returns whether the row was consumed.
    """

    previous_cells = _direct_cells(previous)
    incoming_cells = _direct_cells(incoming)
    if not incoming_cells:
        incoming.decompose()
        return True
    if not previous_cells:
        record_table_row("ambiguous")
        return False

    if len(incoming_cells) == 1 and len(previous_cells) > 1:
        cell = incoming_cells[0]
        if not cell_continues(previous_cells[-1], cell):
            record_table_row("ambiguous")
            return False
        _join_continued_cell(previous_cells[-1], cell, join_cells)
        incoming.decompose()
        record_table_row("single_cell")
        return True

    if [_span(cell) for cell in previous_cells] != [
        _span(cell) for cell in incoming_cells
    ]:
        record_table_row("ambiguous")
        return False

    for previous_cell, incoming_cell in zip(
        previous_cells, incoming_cells, strict=True
    ):
        if not _has_content(incoming_cell):
            for marker in incoming_cell.find_all("span", class_="pagebreak"):
                previous_cell.append(marker.extract())
            incoming_cell.decompose()
        elif cell_continues(previous_cell, incoming_cell):
            _join_continued_cell(previous_cell, incoming_cell, join_cells)
        elif _has_content(previous_cell):
            _append_cell_block(previous_cell, incoming_cell)
        else:
            for child in list(incoming_cell.contents):
                previous_cell.append(child.extract())
            incoming_cell.decompose()
    incoming.decompose()
    record_table_row("column_aligned")
    return True
