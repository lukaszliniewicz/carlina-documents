from __future__ import annotations

import logging
import sys

import mistune
from bs4 import BeautifulSoup, Tag
from bs4.element import Comment, NavigableString
from markdownify import markdownify

from .language import is_unspaced_character

logger = logging.getLogger(__name__)

# Ruby readings (rt) and their bracket fallbacks (rp) annotate base text.
RUBY_ANNOTATION_TAGS = frozenset({"rp", "rt"})

_markdown_parser = mistune.create_markdown(
    escape=False,
    plugins=["table", "url", "strikethrough"],
)


def markdown_to_html_on_the_fly(markdown_text: str) -> str:
    """Compile Markdown, including mixed inline HTML, into an HTML fragment."""

    if not markdown_text:
        return ""

    try:
        rendered = _markdown_parser(markdown_text)
        if not isinstance(rendered, str):
            raise TypeError("the configured Markdown HTML renderer returned tokens")
        return str(BeautifulSoup(rendered, "html.parser"))
    except Exception:
        logger.exception("Error converting Markdown to HTML.")
        return markdown_text



def _is_reading_text(node: NavigableString) -> bool:
    return any(
        isinstance(parent, Tag) and parent.name in RUBY_ANNOTATION_TAGS
        for parent in node.parents
    )


def text_without_readings(tag: Tag, separator: str = "", *, strip: bool = False) -> str:
    """``get_text`` for running text: ruby readings and fallbacks are skipped."""

    texts = [
        str(node)
        for node in tag.find_all(string=True)
        if isinstance(node, NavigableString)
        and not isinstance(node, Comment)
        and not _is_reading_text(node)
    ]
    if strip:
        texts = [text.strip() for text in texts if text.strip()]
    return separator.join(texts)


def _ruby_parts(ruby: Tag) -> tuple[str, str]:
    bases: list[str] = []
    readings: list[str] = []
    for child in ruby.children:
        if isinstance(child, Tag) and child.name == "rt":
            readings.append(child.get_text())
        elif isinstance(child, Tag) and child.name == "rp":
            continue
        elif isinstance(child, Tag):
            bases.append(text_without_readings(child))
        elif isinstance(child, NavigableString) and not isinstance(child, Comment):
            bases.append(str(child))
    return "".join(bases).strip(), "".join(readings).strip()


def ruby_brackets(ruby: Tag) -> tuple[str, str]:
    """Fullwidth brackets after unspaced base text, ASCII ones otherwise."""

    base, _reading = _ruby_parts(ruby)
    if base and is_unspaced_character(base[-1]):
        return "\uff08", "\uff09"
    return "(", ")"


def ruby_fallback_text(ruby: Tag) -> str:
    """Render one ruby as ``base (reading)``, with fullwidth brackets after CJK."""

    base, reading = _ruby_parts(ruby)
    if not reading:
        return base
    opening, closing = ruby_brackets(ruby)
    separator = " " if opening == "(" and base else ""
    return f"{base}{separator}{opening}{reading}{closing}"


def flatten_ruby_for_text_formats(soup: BeautifulSoup | Tag) -> int:
    """Replace ruby with its text fallback where a format cannot show ruby."""

    count = 0
    for ruby in list(soup.find_all("ruby")):
        if ruby.parent is None or ruby.find_parent("ruby") is not None:
            continue
        ruby.replace_with(NavigableString(ruby_fallback_text(ruby)))
        count += 1
    return count


def html_to_markdown_on_the_fly(html_text: str) -> str:
    """Convert HTML into semantic Markdown with safe, deterministic fallbacks."""

    if not html_text:
        return ""

    try:
        soup = BeautifulSoup(html_text, "html.parser")
        for tag in soup(["script", "style", "head", "meta", "title"]):
            tag.decompose()
        flatten_ruby_for_text_formats(soup)

        clean_html = str(soup)
        try:
            from html_to_markdown import convert
        except ImportError:
            # The optional Rust implementation is supplied by add-ons that need
            # high-throughput conversion; the core runtime owns the portable path.
            logger.debug(
                "html-to-markdown is unavailable under %s; using markdownify.",
                sys.executable,
            )
        else:
            try:
                result = convert(clean_html)
                if isinstance(result, dict) and "content" in result:
                    return result["content"] or ""
                if hasattr(result, "content"):
                    return result.content or ""
                return str(result)
            except Exception as exc:  # noqa: BLE001 - optional accelerator boundary
                logger.warning(
                    "html-to-markdown failed under %s; using markdownify: %s",
                    sys.executable,
                    exc,
                )

        try:
            return markdownify(
                clean_html,
                heading_style="ATX",
                strip=["div", "main", "section", "article", "span"],
            )
        except Exception as exc:  # noqa: BLE001 - preserve the plain-text fallback
            logger.warning("markdownify conversion failed: %s", exc)
            return soup.get_text("\n", strip=True)
    except Exception:
        logger.exception("Error converting HTML to Markdown.")
        return html_text


__all__ = ["html_to_markdown_on_the_fly", "markdown_to_html_on_the_fly"]
