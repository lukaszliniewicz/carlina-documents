"""Reviewed EPUB structure projected into a disposable semantic DOM.

Publication roles and stable relationships govern navigation. Neither translated
words such as 'Notes' nor a printed note number establish identity or placement.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import cast
from urllib.parse import urlparse

from bs4 import BeautifulSoup, Tag

from carlina.core.epub_export import (
    EPUB_SECTION_ROLES,
    EpubExportContractError,
    epub_value_sha256,
)
from carlina.core.semantic_images import (
    IMAGE_ALT_CLASS,
    image_description_parts,
)

from .epub_notes import (
    index_note_units as _index_units,
)
from .epub_notes import (
    is_export_note as _is_note,
)
from .epub_notes import (
    is_note_collection as _is_collection,
)
from .epub_notes import (
    is_within_notes as _within_notes,
)
from .epub_notes import (
    owning_note_unit as _owning_unit,
)
from .note_arrangement import normalize_note_arrangement

_HEADING = re.compile(r"h([1-6])\Z")
_FRONT_ROLES = {"cover", "titlepage", "copyright-page", "toc", "contributors"}
_BODY_ROLES = {"bodymatter", "introduction", "chapter", "part"}
_BACK_ROLES = {"endnotes", "appendix", "bibliography", "backmatter"}
_LABELS = {
    "en": {
        "toc": "Contents",
        "pages": "Pages",
        "pdf_pages": "Source PDF pages",
        "landmarks": "Landmarks",
        "start": "Start reading",
        "cover": "Cover",
        "titlepage": "Title page",
        "copyright-page": "Copyright",
        "contributors": "Contributors",
        "notes": "Notes",
        "return": "Return to reference",
        "continued": "continued",
    },
    "pl": {
        "toc": "Spis treści",
        "pages": "Strony",
        "pdf_pages": "Strony źródłowego PDF",
        "landmarks": "Punkty nawigacyjne",
        "start": "Początek tekstu",
        "cover": "Okładka",
        "titlepage": "Strona tytułowa",
        "copyright-page": "Prawa autorskie",
        "contributors": "Autorzy",
        "notes": "Przypisy",
        "return": "Powrót do odsyłacza",
        "continued": "ciąg dalszy",
    },
    "de": {
        "toc": "Inhalt",
        "pages": "Seiten",
        "pdf_pages": "Seiten der PDF-Quelle",
        "landmarks": "Orientierung",
        "start": "Textbeginn",
        "cover": "Umschlag",
        "titlepage": "Titelseite",
        "copyright-page": "Urheberrecht",
        "contributors": "Mitwirkende",
        "notes": "Anmerkungen",
        "return": "Zurück zur Verweisstelle",
        "continued": "Fortsetzung",
    },
    "fr": {
        "toc": "Table des matières",
        "pages": "Pages",
        "pdf_pages": "Pages du PDF source",
        "landmarks": "Repères",
        "start": "Début du texte",
        "cover": "Couverture",
        "titlepage": "Page de titre",
        "copyright-page": "Droits d'auteur",
        "contributors": "Contributeurs",
        "notes": "Notes",
        "return": "Retour au renvoi",
        "continued": "suite",
    },
    "es": {
        "toc": "Índice",
        "pages": "Páginas",
        "pdf_pages": "Páginas del PDF original",
        "landmarks": "Puntos de referencia",
        "start": "Inicio del texto",
        "cover": "Cubierta",
        "titlepage": "Portada interior",
        "copyright-page": "Derechos de autor",
        "contributors": "Colaboradores",
        "notes": "Notas",
        "return": "Volver a la referencia",
        "continued": "continuación",
    },
    "ar": {
        "toc": "المحتويات",
        "pages": "الصفحات",
        "pdf_pages": "صفحات ملف PDF الأصلي",
        "landmarks": "معالم التنقل",
        "start": "بداية النص",
        "cover": "الغلاف",
        "titlepage": "صفحة العنوان",
        "copyright-page": "حقوق النشر",
        "contributors": "المساهمون",
        "notes": "الحواشي",
        "return": "العودة إلى المرجع",
        "continued": "تتمة",
    },
}


def epub_labels(language: str) -> dict[str, str]:
    return _LABELS.get(language.split("-", 1)[0].lower(), _LABELS["en"])


def epub_label_language(language: str) -> str:
    return language if language.split("-", 1)[0].lower() in _LABELS else "en"


def _tokens(tag: Tag, attribute: str) -> set[str]:
    value = tag.get(attribute)
    return (
        set(map(str, value))
        if isinstance(value, list)
        else set(str(value or "").split())
    )


def publication_heading_text(heading: Tag) -> str:
    """Label a heading without reproducing its live citations or page markers."""
    projected = deepcopy(heading)
    for marker in projected.find_all(
        lambda tag: (
            bool(_tokens(tag, "epub:type") & {"noteref", "pagebreak"})
            or bool(_tokens(tag, "role") & {"doc-noteref", "doc-pagebreak"})
            or "footnote-ref" in _tokens(tag, "class")
            or tag.has_attr("hidden")
            or str(tag.get("aria-hidden") or "").lower() == "true"
        )
    ):
        marker.decompose()
    return " ".join(projected.get_text(" ", strip=True).split())


def assign_review_anchors(soup: BeautifulSoup) -> None:
    """Give otherwise unaddressable review items deterministic, unique anchors."""

    used = {str(tag["id"]) for tag in soup.find_all(id=True)}
    for kind, nodes in (
        ("heading", soup.find_all(_HEADING)),
        ("table", soup.find_all("table")),
        ("image", soup.find_all("img")),
    ):
        ordinal = 0
        for tag in nodes:
            if tag.get("id"):
                continue
            if kind == "image" and (
                "cover" in _tokens(tag, "class") or "cover" in _tokens(tag, "epub:type")
            ):
                base = "epub-cover-image"
            else:
                ordinal += 1
                base = f"epub-{kind}-{ordinal}"
            candidate = base
            suffix = 2
            while candidate in used:
                candidate = f"{base}-{suffix}"
                suffix += 1
            tag["id"] = candidate
            used.add(candidate)


def is_cover_image(image: Tag, metadata: Mapping[str, object]) -> bool:
    hint = str(metadata.get("cover_image_id") or "").strip()
    source = str(image.get("src") or "")
    path = PurePosixPath(urlparse(source).path)
    return any(
        "cover" in _tokens(node, "class")
        or "doc-cover" in _tokens(node, "role")
        or "cover" in _tokens(node, "epub:type")
        for node in [image, *image.parents]
    ) or bool(
        hint and hint in {str(image.get("id") or ""), source, path.name, path.stem}
    )


def project_selected_cover(
    soup: BeautifulSoup, metadata: Mapping[str, object], *, title: str
) -> Tag:
    """Give a replacement cover its own review identity, without resource I/O.

    Old image destinations remain linkable, but an old description or decorative
    choice must not silently attach to a different picture. Preflight and the
    final packager call this identical projection before applying review choices.
    """
    covers = [
        image for image in soup.find_all("img") if is_cover_image(image, metadata)
    ]
    if len(covers) > 1:
        raise EpubExportContractError("epub-cover-ambiguous")
    used = {str(tag["id"]) for tag in soup.find_all(id=True)}
    identifier = "epub-cover-image"
    suffix = 2
    while identifier in used:
        identifier = f"epub-cover-image-{suffix}"
        suffix += 1
    if covers:
        original = covers[0]
        if original.get("id"):
            original.replace_with(
                soup.new_tag("span", attrs={"id": str(original["id"])})
            )
        else:
            original.decompose()
    image = soup.new_tag(
        "img",
        attrs={
            "id": identifier,
            "class": "cover",
            "alt": f"{title} — {epub_labels(str(metadata.get('language') or 'en'))['cover']}",
            "data-epub-selected-cover": "true",
        },
    )
    figure = soup.new_tag("figure", attrs={"class": "cover", "epub:type": "cover"})
    figure.append(image)
    container = soup.find("main") or soup.body or soup
    container.insert(0, figure)
    return image


def governing_heading_level(
    metadata: Mapping[str, object], options: Mapping[str, object]
) -> int | None:
    explicit = options.get("chapter_level")
    if type(explicit) is int:
        return explicit
    arrangement = normalize_note_arrangement(metadata.get("note_arrangement"))
    level = arrangement.get("scope_heading_level")
    if type(level) is int:
        return level
    structure = metadata.get("heading_structure")
    if isinstance(structure, Mapping):
        structure = cast(Mapping[str, object], structure)
        roles = structure.get("roles")
        if isinstance(roles, (list, tuple)):
            for row in cast(list[object] | tuple[object, ...], roles):
                if not isinstance(row, Mapping):
                    continue
                row = cast(Mapping[str, object], row)
                if row.get("role") != "chapter":
                    continue
                level = row.get("html_level")
                if (
                    row.get("extractor_ready") is True
                    and type(level) is int
                    and 1 <= level <= 6
                ):
                    return level
    return None


@dataclass
class EpubStructure:
    chapter_level: int | None
    roles: dict[str, str] = field(default_factory=dict)
    issues: list[dict[str, str]] = field(default_factory=list)
    document_numbering: bool = False

    def issue(self, code: str, target: str = "", *, severity: str = "review") -> None:
        row = {"code": code, "severity": severity, "target": target}
        if row not in self.issues:
            self.issues.append(row)


def source_epub_role(tag: Tag) -> str | None:
    for candidate in [tag, *tag.parents]:
        if (
            candidate is not tag
            and _HEADING.fullmatch(tag.name)
            and candidate.find(_HEADING) is not tag
        ):
            continue
        known = _tokens(candidate, "epub:type") & EPUB_SECTION_ROLES
        if known:
            return min(known)
        roles = _tokens(candidate, "role")
        for role in sorted(EPUB_SECTION_ROLES):
            if f"doc-{role}" in roles:
                return role
    return None


def _apply_reviewed_semantics(
    soup: BeautifulSoup, options: Mapping[str, object]
) -> None:
    specifications = (
        ("roles", None),
        ("page_labels", "pagebreak"),
        ("table_headers", "table"),
        ("image_descriptions", "img"),
    )
    for key, kind in specifications:
        values = options.get(key, {})
        if not isinstance(values, Mapping):
            continue
        for anchor, value in cast(Mapping[str, str], values).items():
            tag = soup.find(id=str(anchor))
            if tag is None:
                raise EpubExportContractError("epub-options-stale-anchor")
            if key == "roles":
                if not _HEADING.fullmatch(tag.name) and tag.name not in {
                    "section",
                    "article",
                    "figure",
                    "div",
                }:
                    raise EpubExportContractError("epub-options-role-target")
                tag["data-epub-role"] = str(value)
                if not _HEADING.fullmatch(tag.name):
                    tag["epub:type"] = " ".join(
                        sorted(
                            (_tokens(tag, "epub:type") - EPUB_SECTION_ROLES)
                            | {str(value)}
                        )
                    )
            elif kind == "pagebreak":
                if "pagebreak" not in _tokens(
                    tag, "epub:type"
                ) and "doc-pagebreak" not in _tokens(tag, "role"):
                    raise EpubExportContractError("epub-options-page-target")
                tag["title"] = str(value)
                tag["aria-label"] = str(value)
                tag["data-epub-printed-page"] = "true"
            elif key == "image_descriptions" and "mermaid-diagram-v1" in _tokens(
                tag, "class"
            ):
                tag.attrs.pop("aria-hidden", None)
                if tag.get("role") in {"img", "none", "presentation"}:
                    tag.attrs.pop("role", None)
                short, _, _ = image_description_parts(tag)
                if short is None:
                    short = soup.new_tag("p", attrs={"class": IMAGE_ALT_CLASS})
                    tag.append(short)
                short.string = str(value)
                tag["data-epub-description-reviewed"] = "true"
            elif tag.name != kind:
                raise EpubExportContractError("epub-options-review-target")
            elif key == "image_descriptions":
                tag["alt"] = str(value)
                tag["data-epub-description-reviewed"] = "true"
                tag.attrs.pop("aria-hidden", None)
                if tag.get("role") in {"none", "presentation"}:
                    tag.attrs.pop("role", None)
            elif key == "table_headers":
                rows = tag.find_all("tr")
                if str(value) == "row":
                    for row in rows:
                        cells = row.find_all(["td", "th"], recursive=False)
                        if cells:
                            cells[0].name = "th"
                            cells[0]["scope"] = "row"
                elif rows:
                    for cell in rows[0].find_all(["td", "th"], recursive=False):
                        cell.name = "th"
                        cell["scope"] = "col"
    decorative = options.get("decorative_images", [])
    if isinstance(decorative, (list, tuple)):
        for anchor in cast(list[str] | tuple[str, ...], decorative):
            image = soup.find(id=str(anchor))
            if image is None:
                raise EpubExportContractError("epub-options-stale-anchor")
            if image.name == "figure" and "mermaid-diagram-v1" in _tokens(
                image, "class"
            ):
                image["data-epub-decorative-image"] = "true"
            elif image.name == "img":
                image["alt"] = ""
                image["role"] = "presentation"
            else:
                raise EpubExportContractError("epub-options-stale-anchor")


def extended_description_witness(image: Tag) -> tuple[str | None, str | None]:
    figure = image if image.name == "figure" else image.find_parent("figure")
    if figure is None:
        return None, None
    _, description, _ = image_description_parts(figure)
    diagram = (
        figure.find("pre") if "mermaid-diagram-v1" in _tokens(figure, "class") else None
    )
    if description is None and diagram is None:
        return None, None
    return epub_value_sha256(
        {
            "description": str(description) if description else None,
            "diagram": diagram.get_text() if diagram else None,
            "labels": str(figure.select_one(".mermaid-labels")) if diagram else None,
        }
    ), str(description.get("id") or "") if description else str(figure.get("id") or "")


def apply_epub_structure(
    soup: BeautifulSoup,
    metadata: Mapping[str, object],
    options: Mapping[str, object],
) -> EpubStructure:
    assign_review_anchors(soup)
    _apply_reviewed_semantics(soup, options)
    for identifier in cast(list[str], options.get("photographic_images", [])):
        image = soup.find("img", id=identifier)
        if image is None:
            raise EpubExportContractError("epub-options-review-target")
    for identifier, digest in cast(
        Mapping[str, str], options.get("long_descriptions", {})
    ).items():
        image = soup.find(id=identifier)
        if image is None or extended_description_witness(image)[0] != digest:
            raise EpubExportContractError("epub-options-description-stale")
        image["data-epub-long-description-reviewed"] = "true"
    arrangement = normalize_note_arrangement(metadata.get("note_arrangement"))
    structure = EpubStructure(
        governing_heading_level(metadata, options),
        document_numbering=arrangement.get("numbering") == "document",
    )
    known_body = False
    title = str(metadata.get("title") or "").strip().casefold()
    part_depth = 0
    for heading in soup.find_all(_HEADING):
        if any(_is_note(parent) for parent in heading.parents):
            continue
        role = str(heading.get("data-epub-role") or "") or source_epub_role(heading)
        if role is None and any(_is_collection(parent) for parent in heading.parents):
            role = "endnotes"
        if (
            role is None
            and not known_body
            and title
            and publication_heading_text(heading).casefold() == title
        ):
            role = "titlepage"
        level = int(heading.name[1])
        if role is None and structure.chapter_level == level:
            role = "chapter"
        if role:
            heading["data-epub-role"] = role
            structure.roles[str(heading["id"])] = role
            if role in _BODY_ROLES:
                known_body = True
            if role == "part":
                part_depth = 1
            elif role in _FRONT_ROLES | _BACK_ROLES:
                part_depth = 0
            nav_level = part_depth + 1 if role == "chapter" else 1
            heading["data-epub-nav-level"] = str(nav_level)
            if role in {"cover", "titlepage", "copyright-page", "toc"}:
                heading["data-epub-nav-skip"] = "true"
        elif known_body and structure.chapter_level is not None:
            heading["data-epub-nav-level"] = str(
                min(6, part_depth + 1 + max(1, level - structure.chapter_level))
            )
    if structure.chapter_level is None and not any(
        role in _BODY_ROLES for role in structure.roles.values()
    ):
        structure.issue("structure-needs-review")
    if str(options.get("direction")) in {"ltr", "rtl"}:
        container = soup.find("main")
        if container is not None:
            container["dir"] = str(options["direction"])
    _attach_notes_heading(soup, structure)
    return structure


def _attach_notes_heading(soup: BeautifulSoup, structure: EpubStructure) -> None:
    headings = soup.find_all(
        lambda tag: (
            _HEADING.fullmatch(tag.name) is not None
            and tag.get("data-epub-role") == "endnotes"
        )
    )
    collections = [
        tag for tag in soup.find_all(_is_collection) if tag.find(_is_note) is not None
    ]
    if len(headings) == 1 and len(collections) == 1:
        heading, collection = headings[0], collections[0]
        if collection in heading.parents or heading in collection.parents:
            return
        # The reviewed role authorizes movement of the intact collection, not
        # deletion/reordering of any intervening prose or collection descendants.
        collection.extract()
        heading.insert_after(collection)


def finalize_note_sections(
    soup: BeautifulSoup, structure: EpubStructure, language: str
) -> None:
    """Name surviving collections after placement, without changing notes."""
    labels = epub_labels(language)
    used = {str(tag["id"]) for tag in soup.find_all(id=True)}
    notes = [tag for tag in soup.find_all(_is_note) if tag.get("id")]
    _, references, scopes = _index_units(
        soup, structure.chapter_level or 6, {str(note["id"]) for note in notes}
    )
    for ordinal, collection in enumerate(soup.find_all(_is_collection), start=1):
        note = collection.find(_is_note)
        if note is None:
            continue
        heading = collection.find(
            lambda tag: (
                _HEADING.fullmatch(tag.name) is not None
                and tag.get("data-epub-generated") != "note-group"
                and not any(_is_note(parent) for parent in tag.parents)
            )
        )
        if heading is None:
            previous = collection.find_previous_sibling(True)
            if (
                previous is not None
                and _HEADING.fullmatch(previous.name)
                and previous.get("data-epub-role") == "endnotes"
            ):
                heading = previous
        if heading is None:
            title = labels["notes"]
            level = 2
            nav_level = 1
            if "chapter-endnotes" in _tokens(collection, "class"):
                owner = _owning_unit(
                    note,
                    references.get(str(note.get("id") or ""), []),
                    scopes,
                    document_numbering=structure.document_numbering,
                )
                if owner is not None:
                    title += f" — {publication_heading_text(owner.heading)}"
                    level = min(6, int(owner.heading.name[1]) + 1)
                    nav_level = min(
                        6, int(str(owner.heading.get("data-epub-nav-level") or "1")) + 1
                    )
            stem = f"epub-notes-heading-{ordinal}"
            identifier = stem
            suffix = 2
            while identifier in used:
                identifier = f"{stem}-{suffix}"
                suffix += 1
            used.add(identifier)
            heading = soup.new_tag(
                f"h{level}",
                attrs={
                    "id": identifier,
                    "lang": epub_label_language(language),
                    "data-epub-generated": "notes-heading",
                    "data-epub-role": "endnotes",
                    "data-epub-nav-level": str(nav_level),
                },
            )
            heading.string = title
            collection.insert(0, heading)
        collection["data-epub-role"] = "endnotes"
        collection["role"] = "doc-endnotes"
        collection["epub:type"] = "endnotes"
        collection.attrs.pop("aria-label", None)
        collection.attrs.pop("aria-labelledby", None)
        if collection in heading.parents:
            collection["aria-labelledby"] = str(heading["id"])
        else:
            # A retained external heading may be in another spine fragment.
            collection["aria-label"] = publication_heading_text(heading)


def group_global_endnotes(
    soup: BeautifulSoup, structure: EpubStructure, language: str
) -> None:
    level = structure.chapter_level
    if level is None:
        return
    notes = [
        tag
        for tag in soup.find_all(_is_note)
        if not any(_is_note(parent) for parent in tag.parents)
    ]
    note_ids = {str(note.get("id") or "") for note in notes}
    _, references, scopes = _index_units(soup, level, note_ids)
    labels = epub_labels(language)
    for collection in list(soup.find_all(_is_collection)):
        if "chapter-endnotes" in _tokens(collection, "class"):
            continue
        for listing in collection.find_all(
            lambda tag: tag.name in {"ol", "ul"} or "list" in _tokens(tag, "role"),
            recursive=False,
        ):
            previous: Tag | None = None
            for item in list(listing.find_all(True, recursive=False)):
                note = item if _is_note(item) else item.find(_is_note)
                if note is None:
                    previous = None
                    continue
                unit = _owning_unit(
                    note,
                    references.get(str(note.get("id") or ""), []),
                    scopes,
                    document_numbering=structure.document_numbering,
                )
                if unit is None:
                    structure.issue(
                        "note-ownership-needs-review", str(note.get("id") or "")
                    )
                    previous = None
                    continue
                if previous is unit.heading:
                    continue
                previous = unit.heading
                if (
                    item.find(
                        lambda tag: tag.get("data-epub-generated") == "note-group"
                    )
                    is not None
                ):
                    continue
                heading = soup.new_tag(
                    "h3",
                    attrs={
                        "class": "epub-note-group",
                        "data-epub-generated": "note-group",
                        "data-epub-nav-skip": "true",
                    },
                )
                heading["lang"] = epub_label_language(language)
                heading.string = (
                    f"{labels['notes']} — {publication_heading_text(unit.heading)}"
                )
                # Keep the list's address and every item in their existing order.
                # A group heading inside the item's flow avoids splitting a
                # protected collection or changing native source list counters.
                item.insert(0, heading)


def add_note_backlinks(soup: BeautifulSoup, language: str) -> None:
    notes = {str(tag["id"]): tag for tag in soup.find_all(_is_note) if tag.get("id")}
    used = {str(tag["id"]) for tag in soup.find_all(id=True)}
    incoming: dict[str, list[Tag]] = {}
    for anchor in soup.find_all("a", href=True):
        target = str(anchor.get("href") or "")
        if (
            not target.startswith("#")
            or target[1:] not in notes
            or _within_notes(anchor)
        ):
            continue
        if "noteref" not in _tokens(
            anchor, "epub:type"
        ) and "doc-noteref" not in _tokens(anchor, "role"):
            continue
        if not anchor.get("id"):
            stem = f"epub-ref-{len(incoming.get(target[1:], [])) + 1}-{target[1:]}"
            candidate = stem[:120]
            suffix = 2
            while candidate in used:
                candidate = f"{stem[:110]}-{suffix}"
                suffix += 1
            anchor["id"] = candidate
            used.add(candidate)
        incoming.setdefault(target[1:], []).append(anchor)
    label = epub_labels(language)["return"]
    for note_id, anchors in incoming.items():
        note = notes[note_id]
        existing = {
            str(link.get("href") or "") for link in note.find_all("a", href=True)
        }
        for ordinal, anchor in enumerate(anchors, start=1):
            href = f"#{anchor['id']}"
            if href in existing:
                continue
            printed_label = str(
                note.get("data-footnote-label")
                or anchor.get("data-footnote-label")
                or anchor.get_text(" ", strip=True)
            ).strip()
            return_label = f"{label} {printed_label}" if printed_label else label
            if len(anchors) > 1:
                return_label += f" ({ordinal})"
            backlink = soup.new_tag(
                "a",
                attrs={
                    "href": href,
                    "role": "doc-backlink",
                    "epub:type": "backlink",
                    "class": "epub-backlink",
                    "aria-label": return_label,
                },
            )
            backlink["lang"] = epub_label_language(language)
            backlink.string = f"↩{ordinal}" if len(anchors) > 1 else "↩"
            note.append(" ")
            note.append(backlink)
            existing.add(href)


def review_epub_outline(
    html: str, metadata: Mapping[str, object] | None = None
) -> dict[str, object]:
    soup = BeautifulSoup(html, "html.parser")
    assign_review_anchors(soup)
    return {
        "input_sha256": hashlib.sha256(html.encode("utf-8")).hexdigest(),
        "headings": [
            {
                "id": str(tag["id"]),
                "level": int(tag.name[1]),
                "title": publication_heading_text(tag)[:240],
                "role": source_epub_role(tag),
            }
            for tag in soup.find_all(_HEADING)
            if not any(_is_note(parent) for parent in tag.parents)
        ][:512],
        "tables": [
            {
                "id": str(tag["id"]),
                "caption": tag.caption.get_text(" ", strip=True)[:240]
                if tag.caption
                else "",
                "has_headers": tag.find("th") is not None,
            }
            for tag in soup.find_all("table")
        ][:512],
        "images": [
            {
                "id": str(tag["id"]),
                "alt": (
                    short.get_text(" ", strip=True)
                    if short is not None
                    else str(tag.get("alt") or "")
                )[:4096],
                "decorative": tag.get("role") in {"none", "presentation"},
                "is_cover": tag.name == "img" and is_cover_image(tag, metadata or {}),
                "is_selected_cover": tag.get("data-epub-selected-cover") == "true",
                "description_sha256": extended_description_witness(tag)[0],
                "description_id": extended_description_witness(tag)[1],
            }
            for tag in soup.select("img, figure.mermaid-diagram-v1")
            for short, _, _ in [image_description_parts(tag)]
        ][:512],
        "pages": [
            {
                "id": str(tag.get("id") or ""),
                "label": str(tag.get("title") or tag.get("aria-label") or "")[:128],
            }
            for tag in soup.find_all(
                lambda node: (
                    "pagebreak" in _tokens(node, "epub:type")
                    or "doc-pagebreak" in _tokens(node, "role")
                )
            )
        ][:2048],
    }
