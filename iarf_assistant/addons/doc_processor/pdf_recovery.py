"""Fail-closed validation for the PDF image-review continuation boundary."""

from __future__ import annotations

from collections.abc import Mapping


def is_complete_pdf_image_review_state(state: object) -> bool:
    """Return whether post-extraction resume may safely finalize this state.

    The resume worker does not extract pages. Its durable input must therefore
    carry the explicit image-review phase and non-empty HTML for every selected
    page. Excluded pages may remain empty because they are not part of the
    semantic document.
    """

    if not isinstance(state, Mapping) or state.get("phase") != "PAUSED_IMAGE_REVIEW":
        return False
    html_parts = state.get("html_parts")
    if not isinstance(html_parts, list):
        return False
    included_pages = state.get("included_pages")
    if included_pages is None:
        required_indexes = list(range(len(html_parts)))
    elif isinstance(included_pages, list):
        required_indexes = list(included_pages)
    else:
        return False
    if not required_indexes or any(
        not isinstance(index, int)
        or isinstance(index, bool)
        or index < 0
        or index >= len(html_parts)
        for index in required_indexes
    ):
        return False
    return all(
        isinstance(html_parts[index], str) and html_parts[index].strip()
        for index in required_indexes
    )


__all__ = ["is_complete_pdf_image_review_state"]
