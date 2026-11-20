"""Ruby annotations (furigana, bopomofo and similar interlinear readings).

The canonical document stores ``<ruby>base<rt>reading</rt></ruby>`` with no
``<rp>`` fallback. Readings are not part of the running text: search, outline,
heading labels and Translator see the base text only. Each export renders the
readings in the best form its format supports: native ruby in HTML, native
ruby with bracket fallbacks in EPUB, and ``base (reading)`` text (fullwidth
brackets after CJK) in DOCX, PDF and Markdown (see ``carlina.core.text_formats``).
"""

from __future__ import annotations

from collections.abc import Callable

from bs4 import BeautifulSoup, Tag

from carlina.core.text_formats import (
    RUBY_ANNOTATION_TAGS,
    flatten_ruby_for_text_formats,
    ruby_brackets,
    ruby_fallback_text,
    text_without_readings,
)

__all__ = [
    "RUBY_ANNOTATION_TAGS",
    "add_ruby_fallback_brackets",
    "flatten_ruby_for_text_formats",
    "normalize_ruby",
    "ruby_fallback_text",
    "text_without_readings",
]


def normalize_ruby(soup: BeautifulSoup | Tag, report: Callable[[str], None]) -> None:
    """Bring ruby markup to its canonical shape.

    ``rp`` fallbacks are removed (exports add their own). A reading outside a
    ruby container, or a ruby with no reading, cannot be rendered as ruby and
    is unwrapped so its text stays visible; ``report`` receives a warning code.
    """

    for fallback in list(soup.find_all("rp")):
        if fallback.parent is not None:
            fallback.decompose()
    for reading in list(soup.find_all("rt")):
        if reading.parent is not None and reading.find_parent("ruby") is None:
            reading.unwrap()
            report("ruby_reading_outside_ruby")
    for ruby in list(soup.find_all("ruby")):
        if ruby.parent is None:
            continue
        if ruby.find("rt") is None:
            ruby.unwrap()
            report("ruby_without_reading_unwrapped")
            continue
        for nested in list(ruby.find_all("ruby")):
            nested.unwrap()
            report("nested_ruby_unwrapped")


def add_ruby_fallback_brackets(soup: BeautifulSoup) -> None:
    """Add ``<rp>`` brackets so readers without ruby support stay legible."""

    for ruby in soup.find_all("ruby"):
        if ruby.find("rp") is not None:
            continue
        opening, closing = ruby_brackets(ruby)
        for reading in list(ruby.find_all("rt", recursive=False)):
            before = soup.new_tag("rp")
            before.string = opening
            after = soup.new_tag("rp")
            after.string = closing
            reading.insert_before(before)
            reading.insert_after(after)
