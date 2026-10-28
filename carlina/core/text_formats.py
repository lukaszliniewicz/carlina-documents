from __future__ import annotations

import logging
import sys

import mistune
from bs4 import BeautifulSoup
from markdownify import markdownify

logger = logging.getLogger(__name__)

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


def html_to_markdown_on_the_fly(html_text: str) -> str:
    """Convert HTML into semantic Markdown with safe, deterministic fallbacks."""

    if not html_text:
        return ""

    try:
        soup = BeautifulSoup(html_text, "html.parser")
        for tag in soup(["script", "style", "head", "meta", "title"]):
            tag.decompose()

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
