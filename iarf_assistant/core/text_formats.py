"""Compatibility alias for :mod:`carlina.core.text_formats`."""

from carlina._compat import alias_module as _alias_module
from carlina.core.text_formats import (
    html_to_markdown_on_the_fly,
    markdown_to_html_on_the_fly,
)

__all__ = ["html_to_markdown_on_the_fly", "markdown_to_html_on_the_fly"]

_alias_module(__name__, "carlina.core.text_formats")
