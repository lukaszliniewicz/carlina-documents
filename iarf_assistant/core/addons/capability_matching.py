"""Pure capability and source-class matching helpers.

This module deliberately has no Flask, database, or registry dependencies so
capability discovery and source coverage can share matching semantics without
depending on private methods on :class:`AddonManager`.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Any, Final

from .planning_inputs import bounded_planning_text

CAPABILITY_QUERY_STOPWORDS: Final[frozenset[str]] = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "can",
        "could",
        "do",
        "for",
        "from",
        "how",
        "i",
        "in",
        "into",
        "is",
        "it",
        "me",
        "my",
        "of",
        "on",
        "or",
        "please",
        "the",
        "this",
        "to",
        "use",
        "want",
        "with",
        "would",
    }
)
CAPABILITY_TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"[a-z0-9]+")

SOURCE_CLASS_ALIASES: Final[Mapping[str, tuple[str, ...]]] = MappingProxyType(
    {
        "email": (
            "email",
            "mail",
            "mailbox",
            "gmail",
            "imap",
            "smtp",
            "thread",
            "threads",
            "inbox",
        ),
        "gdrive": (
            "gdrive",
            "google drive",
            "googledrive",
            "drive",
            "google docs",
            "docs",
            "sheets",
            "slides",
            "workspace files",
        ),
        "local_files": (
            "local_files",
            "local files",
            "local storage",
            "local folder",
            "local folders",
            "filesystem",
            "directory path",
            "server storage",
            "private storage",
        ),
        "web": (
            "web",
            "website",
            "websites",
            "internet",
            "scrape",
            "scraping",
            "crawl",
            "web browser",
            "page content",
        ),
        "wordpress": (
            "wordpress",
            "wp",
            "publishing",
            "post",
            "posts",
            "page",
            "pages",
            "taxonomy",
            "categories",
            "tags",
            "media library",
        ),
        "documents": (
            "document",
            "documents",
            "doc",
            "docx",
            "pdf",
            "office",
            "epub",
            "msg",
            "markdown",
            "document processor",
        ),
        "images": (
            "image",
            "images",
            "picture",
            "pictures",
            "photo",
            "photos",
            "vision",
            "media",
            "thumbnail",
        ),
    }
)

SOURCE_CLASS_CANONICAL: Final[Mapping[str, str]] = MappingProxyType(
    {
        "gmail": "email",
        "mail": "email",
        "mailbox": "email",
        "imap": "email",
        "smtp": "email",
        "google_drive": "gdrive",
        "googledrive": "gdrive",
        "drive": "gdrive",
        "google docs": "gdrive",
        "docs": "gdrive",
        "local": "local_files",
        "local_storage": "local_files",
        "filesystem": "local_files",
        "files": "local_files",
        "folders": "local_files",
        "folder": "local_files",
        "internet": "web",
        "website": "web",
        "websites": "web",
        "scraping": "web",
        "scrape": "web",
        "wp": "wordpress",
        "pdf": "documents",
        "docx": "documents",
        "document": "documents",
        "pictures": "images",
        "photos": "images",
        "image": "images",
    }
)

SOURCE_CLASS_STRONG_ALIASES: Final[Mapping[str, tuple[str, ...]]] = MappingProxyType(
    {
        "email": ("email", "mailbox", "gmail", "imap", "smtp", "inbox"),
        "gdrive": (
            "gdrive",
            "google drive",
            "googledrive",
            "google docs",
            "drive",
        ),
        "local_files": (
            "local files",
            "local storage",
            "filesystem",
            "server storage",
            "private storage",
        ),
        "web": (
            "web",
            "web_search",
            "web search",
            "scraping",
            "scrape",
            "crawl",
            "website",
        ),
        "wordpress": ("wordpress", "wp"),
        "documents": (
            "doc_processor",
            "document processor",
            "documents",
            "pdf",
            "docx",
            "epub",
            "msg",
        ),
        "images": (
            "image",
            "images",
            "picture",
            "pictures",
            "photo",
            "photos",
        ),
    }
)


def normalized_capability_text(*values: Any) -> str:
    """Normalize arbitrary values to a lowercase, token-delimited string."""

    text = " ".join(str(value or "") for value in values if value is not None)
    return " ".join(CAPABILITY_TOKEN_RE.findall(text.lower()))


def capability_search_tokens(
    value: Any,
    *,
    remove_stopwords: bool = True,
) -> list[str]:
    """Return stable, de-duplicated search tokens."""

    tokens = CAPABILITY_TOKEN_RE.findall(str(value or "").lower())
    if remove_stopwords:
        tokens = [token for token in tokens if token not in CAPABILITY_QUERY_STOPWORDS]
    return list(dict.fromkeys(token for token in tokens if len(token) >= 2))


def source_alias_terms(
    source_class: str,
    aliases: Sequence[str] | None = None,
) -> list[str]:
    terms = [source_class, source_class.replace("_", " ")]
    terms.extend(
        aliases if aliases is not None else SOURCE_CLASS_ALIASES.get(source_class, ())
    )
    expanded: set[str] = set()
    for term in terms:
        text = str(term or "").strip().lower()
        if not text:
            continue
        expanded.update(
            {
                text,
                text.replace("_", " "),
                text.replace(" ", ""),
                text.replace(" ", "_"),
            }
        )
    return sorted(expanded)


def contains_source_alias(text: str, alias: str) -> bool:
    haystack = f" {normalized_capability_text(text)} "
    needle = normalized_capability_text(alias)
    return bool(needle) and f" {needle} " in haystack


def contains_strong_source_alias(
    text: str,
    source_class: str,
    alias: str,
) -> bool:
    if not contains_source_alias(text, alias):
        return False
    needle = normalized_capability_text(alias)
    if source_class != "gdrive" or needle != "drive":
        return True

    # "Drive" is both a product name and a common verb. Require a nearby
    # source/read cue so "drive the project" does not inject Google Drive.
    tokens = capability_search_tokens(text, remove_stopwords=False)
    source_cues = {
        "connected",
        "document",
        "documents",
        "file",
        "files",
        "folder",
        "folders",
        "from",
        "google",
        "in",
        "list",
        "my",
        "on",
        "open",
        "read",
        "search",
        "source",
        "use",
    }
    for index, token in enumerate(tokens):
        if token != "drive":
            continue
        nearby = set(tokens[max(0, index - 3) : index + 4])
        if nearby & source_cues:
            return True
    return False


def infer_source_classes(text: str) -> list[str]:
    lowered = normalized_capability_text(text)
    return [
        source_class
        for source_class, aliases in SOURCE_CLASS_STRONG_ALIASES.items()
        if any(
            contains_strong_source_alias(lowered, source_class, alias)
            for alias in aliases
        )
    ]


def canonical_source_class(raw_source: str) -> str:
    normalized = str(raw_source or "").strip().lower().replace("-", "_")
    normalized_spaced = normalized.replace("_", " ")
    direct = SOURCE_CLASS_CANONICAL.get(normalized) or SOURCE_CLASS_CANONICAL.get(
        normalized_spaced
    )
    if direct:
        return direct
    for source_class, aliases in SOURCE_CLASS_ALIASES.items():
        if normalized_spaced in source_alias_terms(source_class, aliases):
            return source_class
    return normalized


def matches_source_alias(
    text: str,
    source_class: str,
    aliases: Sequence[str],
) -> bool:
    haystack = normalized_capability_text(text)
    return any(
        contains_source_alias(haystack, alias)
        for alias in source_alias_terms(source_class, aliases)
    )


def bounded_text(text: str | None, limit: int = 220) -> str:
    return bounded_planning_text(text, limit)


__all__ = [
    "CAPABILITY_QUERY_STOPWORDS",
    "CAPABILITY_TOKEN_RE",
    "SOURCE_CLASS_ALIASES",
    "SOURCE_CLASS_CANONICAL",
    "SOURCE_CLASS_STRONG_ALIASES",
    "bounded_text",
    "canonical_source_class",
    "capability_search_tokens",
    "contains_source_alias",
    "contains_strong_source_alias",
    "infer_source_classes",
    "matches_source_alias",
    "normalized_capability_text",
    "source_alias_terms",
]
