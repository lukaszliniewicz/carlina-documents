"""Compatibility exports for document-processing callers.

The conversion implementation belongs to core because core knowledge ingestion
uses it even when the document-processing add-on is absent or inactive.
"""

from carlina.core.text_formats import (
    html_to_markdown_on_the_fly,
    markdown_to_html_on_the_fly,
)

__all__ = ["html_to_markdown_on_the_fly", "markdown_to_html_on_the_fly"]
