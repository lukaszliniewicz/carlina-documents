"""Assemble continued HTML tables while retaining physical page markers."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from bs4 import Tag


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
        if row_continues(last_r1, first_r2):
            merge_row(last_r1, first_r2)
            rows2 = rows2[1:]

    for row in rows2:
        target_container.append(row)
    second.decompose()
