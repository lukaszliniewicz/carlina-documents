"""The inert semantic-HTML vocabulary shared by Carlina document add-ons.

This module intentionally defines syntax only.  It has no ORM, Flask, upload,
or exporter knowledge: callers apply their own ownership and resource policy at
the boundary where a document is accepted or rendered.
"""

from __future__ import annotations

import re

SEMANTIC_HTML_CONTRACT_VERSION = "1"

# A deliberately small *document* vocabulary.  It is broader than a chat
# fragment (books need tables and figures) but excludes executable, embedded,
# interactive, and styling elements.
SEMANTIC_DOCUMENT_TAGS = frozenset(
    {
        "a", "abbr", "address", "article", "aside", "b", "blockquote", "body",
        "br", "caption", "cite", "code", "col", "colgroup", "dd", "del", "div",
        "dl", "dt", "em", "figcaption", "figure", "footer", "h1", "h2", "h3",
        "h4", "h5", "h6", "head", "header", "html", "hr", "i", "img", "ins",
        "kbd", "li", "main", "mark", "meta", "nav", "ol", "p", "pre", "q", "s",
        "samp", "section", "small", "span", "strong", "sub", "sup", "table",
        "tbody", "td", "tfoot", "th", "thead", "time", "title", "tr", "u", "ul",
        "var", "wbr",
    }
)

SEMANTIC_DROP_CONTENT_TAGS = frozenset(
    {
        "audio", "base", "button", "canvas", "embed", "form", "iframe", "input",
        "link", "map", "math", "noscript", "object", "option", "script", "select",
        "source", "style", "svg", "template", "textarea", "track", "video",
    }
)

SEMANTIC_GLOBAL_ATTRIBUTES = frozenset(
    {
        "aria-describedby", "aria-hidden", "aria-label", "aria-labelledby", "class",
        "data-footnote-label", "data-footnote-scope", "data-label", "data-subtitle-unit", "dir", "epub:type",
        "hidden", "id", "lang", "role", "title", "xml:lang",
    }
)

SEMANTIC_TAG_ATTRIBUTES = {
    "a": frozenset({"href", "name", "rel", "target"}),
    "blockquote": frozenset({"cite"}),
    "col": frozenset({"span"}),
    "colgroup": frozenset({"span"}),
    "img": frozenset({"alt", "height", "loading", "src", "width"}),
    "li": frozenset({"value"}),
    "meta": frozenset({"charset", "name", "content"}),
    "ol": frozenset({"reversed", "start", "type"}),
    "table": frozenset({"summary"}),
    "td": frozenset({"abbr", "colspan", "headers", "rowspan"}),
    "th": frozenset({"abbr", "colspan", "headers", "rowspan", "scope"}),
    "time": frozenset({"datetime"}),
}

SAFE_LINK_SCHEMES = frozenset({"http", "https", "mailto"})
SAFE_LANGUAGE_RE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8}){0,3}$")
SAFE_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
# IDs and their references must accept the Unicode alphanumerics emitted by
# heading slug generation. Keep class/policy tokens ASCII-only. No whitespace,
# controls, bidi formatting, URL separators or normalization of stored IDs.
SAFE_IDENTIFIER_RE = re.compile(r"^[\w.:-]{1,128}$")
SAFE_INTEGER_RE = re.compile(r"^[0-9]{1,6}$")
SAFE_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
SAFE_DATA_IMAGE_RE = re.compile(
    r"^data:image/(?:gif|jpeg|png|webp);base64,[A-Za-z0-9+/]+={0,2}$",
    re.IGNORECASE,
)
MAX_INLINE_IMAGE_CHARS = 1_048_576


def allowed_attributes(tag_name: str) -> frozenset[str]:
    """Return the inert attribute vocabulary for one normalized tag name."""

    return SEMANTIC_GLOBAL_ATTRIBUTES | SEMANTIC_TAG_ATTRIBUTES.get(tag_name, frozenset())
