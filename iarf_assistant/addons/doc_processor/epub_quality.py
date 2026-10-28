"""Bounded, source-free diagnostics shared by EPUB preflight and delivery."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping

from bs4 import BeautifulSoup

from carlina.core.epub_cover import normalize_epub_cover
from carlina.core.epub_export import (
    EPUB_EXPORT_PROFILE,
    EPUB_QUALITY_SCHEMA,
    MAX_EPUB_QUALITY_ISSUES,
    EpubExportContractError,
    epub_review_sha256,
    epub_value_sha256,
    normalize_epub_options,
)
from carlina.core.export_notes import normalize_export_note_roles
from carlina.core.semantic_html import SAFE_LANGUAGE_RE

from .epub_front_matter import apply_front_matter, review_front_matter
from .epub_notes import is_export_note as _is_note
from .epub_notes import is_within_notes as _within_notes
from .epub_notes import note_tokens as _tokens
from .epub_notes import place_chapter_notes
from .epub_structure import (
    EpubStructure,
    add_note_backlinks,
    apply_epub_structure,
    assign_review_anchors,
    extended_description_witness,
    finalize_note_sections,
    group_global_endnotes,
    project_selected_cover,
    review_epub_outline,
)
from .note_arrangement import normalize_note_arrangement
from .semantic_document import diagnose_semantic_export


def direct_epub_review_sha256(
    input_sha256: str,
    metadata: Mapping[str, object],
    options: Mapping[str, object],
    *,
    cover: object = None,
    image_mode: str = "auto",
) -> str:
    """Witness a direct edition's declared inputs without reading resources."""
    from carlina.core.epub_cover import normalize_epub_cover

    return epub_review_sha256(
        {
            "input_sha256": input_sha256,
            "metadata": dict(metadata),
            "epub_options": normalize_epub_options(options),
            "epub_cover": normalize_epub_cover(cover),
            "image_mode": image_mode,
        }
    )


def publication_language(metadata: Mapping[str, object], soup: BeautifulSoup) -> str:
    root = soup.find("html")
    candidates = (
        metadata.get("language"),
        root.get("lang") if root else None,
        root.get("xml:lang") if root else None,
    )
    return next(
        (
            str(value)
            for value in candidates
            if isinstance(value, str) and SAFE_LANGUAGE_RE.fullmatch(value)
        ),
        "und",
    )


def publishing_note_placement(
    soup: BeautifulSoup,
    metadata: Mapping[str, object],
    options: Mapping[str, object],
    structure: EpubStructure,
) -> None:
    arrangement = normalize_note_arrangement(metadata.get("note_arrangement"))
    choice = options.get("note_placement", "source")
    placement = arrangement.get("placement")
    if choice == "document":
        # Keeping an already collected document is safe. Moving scattered page
        # notes is an editorial action whose order cannot be inferred here.
        scattered = [
            tag
            for tag in soup.find_all(_is_note)
            if not any(
                "endnotes" in _tokens(parent, "epub:type") for parent in tag.parents
            )
        ]
        if scattered:
            structure.issue(
                "document-notes-needs-review", str(scattered[0].get("id") or "")
            )
    elif choice == "chapter" or (
        choice == "source" and placement == "chapter_endnotes"
    ):
        if structure.chapter_level is None:
            structure.issue("chapter-notes-needs-level")
        else:
            projected = {
                **arrangement,
                "placement": "chapter_endnotes",
                "scope_heading_level": structure.chapter_level,
            }
            place_chapter_notes(soup, {"note_arrangement": projected})
    elif placement in {None, "unknown", "mixed"} and soup.find(_is_note) is not None:
        structure.issue("note-arrangement-needs-review")


def project_publication_notes(
    soup: BeautifulSoup,
    metadata: Mapping[str, object],
    options: Mapping[str, object],
    structure: EpubStructure,
    language: str,
) -> None:
    """Use the same note placement, navigation and return links in both routes."""
    normalize_export_note_roles(soup)
    publishing_note_placement(soup, metadata, options, structure)
    normalize_export_note_roles(soup)
    add_note_backlinks(soup, language)
    if options.get("group_endnotes"):
        group_global_endnotes(soup, structure, language)
    finalize_note_sections(soup, structure, language)


def quality_counts(soup: BeautifulSoup) -> dict[str, int]:
    notes = [
        tag
        for tag in soup.find_all(_is_note)
        if not any(_is_note(parent) for parent in tag.parents)
    ]
    return {
        "notes": len(notes),
        "references": sum(
            "noteref" in _tokens(tag, "epub:type")
            or "doc-noteref" in _tokens(tag, "role")
            for tag in soup.find_all("a")
        ),
        "backlinks": sum(
            "backlink" in _tokens(tag, "epub:type")
            or "doc-backlink" in _tokens(tag, "role")
            for tag in soup.find_all("a")
        ),
        "headings": len(soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"])),
        "images": len(soup.find_all("img"))
        + len(soup.select("figure.mermaid-diagram-v1")),
        "tables": len(soup.find_all("table")),
        "pages": sum(
            "pagebreak" in _tokens(tag, "epub:type")
            or "doc-pagebreak" in _tokens(tag, "role")
            for tag in soup.find_all(True)
        ),
    }


def diagnose_publication(
    soup: BeautifulSoup,
    structure: EpubStructure,
    options: Mapping[str, object],
    language: str,
) -> None:
    notes = {str(tag["id"]): tag for tag in soup.find_all(_is_note) if tag.get("id")}
    referenced: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href") or "")
        if href.startswith("#") and href[1:] in notes and not _within_notes(anchor):
            referenced.add(href[1:])
            if not (
                _tokens(anchor, "epub:type") & {"noteref"}
                or _tokens(anchor, "role") & {"doc-noteref"}
            ):
                structure.issue("note-reference-semantics", str(anchor.get("id") or ""))
    for identifier in notes.keys() - referenced:
        structure.issue("note-unreferenced", identifier)
    descriptions = options.get("image_descriptions", {})
    for image in soup.find_all("img"):
        identifier = str(image.get("id") or "")
        decorative = image.get("role") in {"none", "presentation"} and not str(
            image.get("alt") or ""
        )
        if not decorative:
            if not str(image.get("alt") or "").strip():
                structure.issue(
                    "image-description-missing", identifier, severity="warning"
                )
            elif image.get("data-epub-description-reviewed") != "true" and (
                not isinstance(descriptions, Mapping) or identifier not in descriptions
            ):
                structure.issue("image-description-review", identifier)
    for figure in soup.select("figure.mermaid-diagram-v1"):
        if (
            figure.get("data-epub-description-reviewed") != "true"
            and figure.get("data-epub-decorative-image") != "true"
        ):
            structure.issue("image-description-review", str(figure.get("id") or ""))
    for image in soup.select("img, figure.mermaid-diagram-v1"):
        if (
            extended_description_witness(image)[0]
            and image.get("data-epub-long-description-reviewed") != "true"
            and image.get("role") not in {"none", "presentation"}
        ):
            structure.issue("long-description-review", str(image.get("id") or ""))
    for table in soup.find_all("table"):
        if table.find("th") is None:
            structure.issue(
                "table-headers-missing", str(table.get("id") or ""), severity="warning"
            )
    if language == "und":
        structure.issue("language-needs-review", severity="warning")
    if language.split("-", 1)[0].lower() not in {"en", "pl", "de", "fr", "es", "ar"}:
        structure.issue("generated-label-language")
    markers = soup.find_all(
        lambda tag: (
            "pagebreak" in _tokens(tag, "epub:type")
            or "doc-pagebreak" in _tokens(tag, "role")
        )
    )
    if markers and any(tag.get("data-epub-printed-page") != "true" for tag in markers):
        structure.issue("page-label-context")


def make_quality_report(
    soup: BeautifulSoup,
    structure: EpubStructure,
    *,
    input_sha256: str,
    options_sha256: str,
    artifact_sha256: str = "0" * 64,
    content_sha256: str = "0" * 64,
    validation: str = "not_run",
    epubcheck: str = "not_run",
    epubcheck_version: str | None = None,
    profile: str = EPUB_EXPORT_PROFILE,
    ace: str = "not_run",
    ace_version: str | None = None,
) -> dict[str, object]:
    issues = list(structure.issues)
    if ace == "failed":
        issues.append({"code": "ace-findings", "severity": "review", "target": ""})
    return {
        "schema": EPUB_QUALITY_SCHEMA,
        "profile": profile,
        "input_sha256": input_sha256,
        "options_sha256": options_sha256,
        "artifact_sha256": artifact_sha256,
        "content_sha256": content_sha256,
        "checks": {
            "structure": validation,
            "epubcheck": epubcheck,
            "ace": ace,
            "manual": "not_reviewed",
        },
        "counts": quality_counts(soup),
        "issues": issues[:MAX_EPUB_QUALITY_ISSUES],
        "issue_count": len(issues),
        "epubcheck_version": epubcheck_version,
        "ace_version": ace_version,
    }


def preflight_epub(
    html: str,
    metadata: Mapping[str, object],
    options_value: object,
    *,
    cover: object = None,
) -> dict[str, object]:
    """Inspect owned semantic HTML without resources, files or model calls."""

    options = normalize_epub_options(options_value)
    soup = BeautifulSoup(html, "html.parser")
    if diagnose_semantic_export(soup):
        raise EpubExportContractError("epub-source-invalid")
    language = publication_language(metadata, soup)
    assign_review_anchors(soup)
    cover_options = normalize_epub_cover(
        cover if cover is not None else metadata.get("epub-cover")
    )
    if cover_options:
        project_selected_cover(
            soup, metadata, title=str(metadata.get("title") or "Document")
        )
    front_matter = (
        review_front_matter(soup, metadata, options)
        if options.get("profile") == "publishing-v2"
        else None
    )
    outline = review_epub_outline(str(soup), metadata)
    if front_matter is not None:
        outline["front_matter"] = front_matter
    input_sha256 = hashlib.sha256(html.encode("utf-8")).hexdigest()
    outline["input_sha256"] = input_sha256
    active_options = apply_front_matter(soup, metadata, options)
    structure = apply_epub_structure(soup, metadata, active_options)
    project_publication_notes(soup, metadata, options, structure, language)
    diagnose_publication(soup, structure, options, language)
    return {
        "options": options,
        "suggested_chapter_level": structure.chapter_level,
        "outline": outline,
        "quality": make_quality_report(
            soup,
            structure,
            input_sha256=input_sha256,
            options_sha256=epub_value_sha256(options),
            profile=str(options["profile"]),
        ),
    }
