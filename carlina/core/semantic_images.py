"""Bounded, inert accessibility descriptions carried in semantic documents.

The markers identify generated accessibility adaptations, not printed captions
or evaluated conformance. Their text remains ordinary translatable content.
"""

from __future__ import annotations

from collections import Counter

from bs4 import BeautifulSoup, Tag

from .semantic_html import SAFE_IDENTIFIER_RE

IMAGE_ALT_CLASS = "image-alt-v1"
IMAGE_DESCRIPTION_CLASS = "image-description-v1"
IMAGE_DESCRIPTION_LINK_CLASS = "image-description-link"
MAX_IMAGE_DESCRIPTIONS = 512
MAX_IMAGE_DESCRIPTION_BYTES = 128 * 1024
MAX_IMAGE_DESCRIPTION_ELEMENTS = 2048
MAX_IMAGE_ALT_BYTES = 4096
MAX_IMAGE_DIAGNOSTICS = 16
_DESCRIPTION_TAGS = frozenset(
    {
        "p",
        "span",
        "strong",
        "em",
        "b",
        "i",
        "abbr",
        "sub",
        "sup",
        "br",
        "ul",
        "ol",
        "li",
        "dl",
        "dt",
        "dd",
        "table",
        "caption",
        "thead",
        "tbody",
        "tfoot",
        "tr",
        "th",
        "td",
        "code",
    }
)
_MARKERS = frozenset(
    {IMAGE_ALT_CLASS, IMAGE_DESCRIPTION_CLASS, IMAGE_DESCRIPTION_LINK_CLASS}
)


def _classes(tag: Tag) -> tuple[str, ...]:
    value = tag.get("class")
    if isinstance(value, str):
        return tuple(value.split())
    if isinstance(value, list):
        return tuple(str(item) for item in value)
    return ()


def image_description_parts(
    figure: Tag,
) -> tuple[Tag | None, Tag | None, Tag | None]:
    """Locate direct short-alt, extended-description and navigation children."""

    parts = [
        next(
            (
                tag
                for tag in figure.find_all(True, recursive=False)
                if marker in _classes(tag)
            ),
            None,
        )
        for marker in (
            IMAGE_ALT_CLASS,
            IMAGE_DESCRIPTION_CLASS,
            IMAGE_DESCRIPTION_LINK_CLASS,
        )
    ]
    return parts[0], parts[1], parts[2]


def diagnose_image_descriptions(soup: BeautifulSoup | Tag) -> tuple[str, ...]:
    """Validate only claimed markers; historical unmarked HTML is unchanged."""

    tags = [soup, *soup.find_all(True)]
    marked = [tag for tag in tags if _MARKERS.intersection(_classes(tag))]
    if not marked:
        return ()
    codes: list[str] = []

    def add(code: str) -> None:
        if code not in codes and len(codes) < MAX_IMAGE_DIAGNOSTICS:
            codes.append(code)

    def exceeds_bytes(text: str, limit: int) -> bool:
        try:
            return len(text.encode("utf-8")) > limit
        except UnicodeEncodeError:
            add("image-description-encoding")
            return True

    ids = Counter(str(tag["id"]) for tag in tags if isinstance(tag.get("id"), str))
    if len(marked) > 3 * MAX_IMAGE_DESCRIPTIONS:
        add("image-description-limit")
    owners: dict[int, Tag] = {}
    for tag in marked[: 3 * MAX_IMAGE_DESCRIPTIONS]:
        markers = _MARKERS.intersection(_classes(tag))
        parent = tag.parent
        if (
            len(markers) != 1
            or len(_classes(tag)) != 1
            or not isinstance(parent, Tag)
            or parent.name != "figure"
        ):
            add("image-description-marker")
            continue
        owners[id(parent)] = parent
        if any(
            child.has_attr("hidden")
            or str(child.get("aria-hidden", "")).strip().casefold() == "true"
            or child.get("role") == "img"
            for child in [parent, *parent.parents, tag, *tag.find_all(True)]
        ):
            add("image-description-hidden")
        marker = next(iter(markers))
        text = tag.get_text(" ", strip=True)
        if not text:
            add("image-description-empty")
        if marker == IMAGE_ALT_CLASS:
            if (
                tag.name != "p"
                or tag.find(True) is not None
                or "mermaid-diagram-v1" not in _classes(parent)
            ):
                add("image-alt-structure")
            if exceeds_bytes(text, MAX_IMAGE_ALT_BYTES):
                add("image-description-limit")
        elif marker == IMAGE_DESCRIPTION_CLASS:
            identifier = tag.get("id")
            if (
                tag.name != "div"
                or not isinstance(identifier, str)
                or not SAFE_IDENTIFIER_RE.fullmatch(identifier)
                or ids[identifier] != 1
            ):
                add("image-description-id")
            descendants = tag.find_all(True)
            if (
                exceeds_bytes(str(tag), MAX_IMAGE_DESCRIPTION_BYTES)
                or len(descendants) > MAX_IMAGE_DESCRIPTION_ELEMENTS
            ):
                add("image-description-limit")
            if any(
                child.name not in _DESCRIPTION_TAGS
                or _MARKERS.intersection(_classes(child))
                for child in descendants
            ):
                add("image-description-content")
        elif marker == IMAGE_DESCRIPTION_LINK_CLASS:
            children = tag.find_all(True, recursive=False)
            if (
                tag.name != "p"
                or len(children) != 1
                or children[0].name != "a"
                or children[0].find(True) is not None
                or tag.get_text(strip=True) != children[0].get_text(strip=True)
            ):
                add("image-description-link")
            if exceeds_bytes(text, MAX_IMAGE_ALT_BYTES):
                add("image-description-limit")
    if len(owners) > MAX_IMAGE_DESCRIPTIONS:
        add("image-description-limit")
    for figure in owners.values():
        _short, description, link = image_description_parts(figure)
        for marker in _MARKERS:
            if (
                sum(
                    marker in _classes(child)
                    for child in figure.find_all(True, recursive=False)
                )
                > 1
            ):
                add("image-description-duplicate")
        is_diagram = "mermaid-diagram-v1" in _classes(figure)
        if not is_diagram and len(figure.find_all("img", recursive=False)) != 1:
            add("image-description-image")
        # Editable graphs always receive complete topology descriptions during
        # projection, including legacy figures with a reviewed short alt only.
        if description is None:
            if link is not None:
                add("image-description-link")
            continue
        if not is_diagram and link is None:
            add("image-description-link")
        if link is not None:
            anchor = link.find("a", recursive=False)
            if anchor is None or anchor.get("href") != f"#{description.get('id')}":
                add("image-description-link")
    return tuple(codes)
