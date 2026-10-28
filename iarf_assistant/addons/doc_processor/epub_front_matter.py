"""Evidence-bound editorial choices applied only to the disposable EPUB DOM."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast

from bs4 import BeautifulSoup, Tag

from carlina.core.epub_export import EpubExportContractError

from .epub_notes import is_export_note, note_tokens
from .epub_structure import (
    assign_review_anchors,
    epub_label_language,
    epub_labels,
    governing_heading_level,
    is_cover_image,
    source_epub_role,
)

_HEADING = re.compile(r"h[1-6]\Z")
_FRONT = {"titlepage", "copyright-page", "toc", "contributors"}
_BODY = {
    "chapter",
    "part",
    "introduction",
    "bodymatter",
    "appendix",
    "endnotes",
    "bibliography",
    "backmatter",
}


@dataclass
class _Unit:
    identifier: str
    role: str
    nodes: list[Tag]
    section_start: Tag | None = None


def _digest(nodes: list[Tag]) -> str:
    snapshot = BeautifulSoup("".join(str(node) for node in nodes), "html.parser")
    for image in snapshot.find_all("img"):
        image["src"] = "epub-resource"
    return hashlib.sha256(str(snapshot).encode("utf-8")).hexdigest()


def _role(
    heading: Tag, metadata: Mapping[str, object], options: Mapping[str, object]
) -> str:
    roles = options.get("roles", {})
    reviewed = (
        cast(Mapping[str, object], roles).get(str(heading.get("id") or ""))
        if isinstance(roles, Mapping)
        else None
    )
    role = str(reviewed or source_epub_role(heading) or "")
    title = str(metadata.get("title") or "").strip().casefold()
    if not role and title and heading.get_text(" ", strip=True).casefold() == title:
        role = "titlepage"
    if not role and governing_heading_level(metadata, options) == int(heading.name[1]):
        role = "chapter"
    return role


def _units(
    soup: BeautifulSoup, metadata: Mapping[str, object], options: Mapping[str, object]
) -> tuple[Tag | BeautifulSoup, list[_Unit]]:
    assign_review_anchors(soup)
    root = soup.find("main") or soup.body or soup
    candidates: list[tuple[Tag, str, Tag | None]] = []
    found_body = False
    for node in root.children:
        if not isinstance(node, Tag):
            continue
        if (
            "cover" in str(node.get("epub:type") or "").split()
            or "epub-wrapper-anchor" in note_tokens(node, "class")
            or (
                node.name in {"img", "figure"}
                and any(
                    is_cover_image(image, metadata)
                    for image in (
                        [node] if node.name == "img" else node.find_all("img")
                    )
                )
            )
        ):
            continue
        heading = node if _HEADING.fullmatch(node.name) else node.find(_HEADING)
        role = (
            _role(heading, metadata, options)
            if heading
            else str(source_epub_role(node) or "")
        )
        # Do not interpret a wrapper containing the whole book as front matter.
        if role in _BODY or any(
            _role(child, metadata, options) in _BODY
            for child in node.find_all(_HEADING)
        ):
            found_body = True
            break
        candidates.append((node, role, heading))
    if not found_body:
        return root, []
    used = {str(tag["id"]) for tag in soup.find_all(id=True)}
    ordinal = 0
    for node, _, _ in candidates:
        for tag in [node, *node.find_all("p")]:
            if tag.get("id") or tag.name not in {"p", "section", "article", "div"}:
                continue
            ordinal += 1
            identifier = f"epub-front-block-{ordinal}"
            while identifier in used:
                ordinal += 1
                identifier = f"epub-front-block-{ordinal}"
            tag["id"] = identifier
            used.add(identifier)
    breaks = cast(
        Mapping[str, Mapping[str, str]], options.get("front_matter_breaks", {})
    )
    break_targets = {
        str(node.get("id")): node
        for node, _, _ in candidates
        if node.name == "p" and _editable(node)
    }
    for identifier, section in breaks.items():
        target = break_targets.get(identifier)
        if target is None:
            raise EpubExportContractError("epub-front-matter-break-target")
        if _digest([target]) != section["sha256"]:
            raise EpubExportContractError("epub-front-matter-stale")
    groups: list[tuple[list[Tag], str]] = []
    current: list[Tag] = []
    current_role = ""
    for node, role, heading in candidates:
        section = breaks.get(str(node.get("id")))
        if section:
            role = section["role"]
        if (
            section is not None
            or (
                heading is not None
                and (role in _FRONT or node.name in {"section", "article"})
            )
        ) and current:
            # Keep preceding physical page anchors with their following heading.
            markers: list[Tag] = []
            while (
                current
                and current[-1].name == "span"
                and not current[-1].get_text(strip=True)
            ):
                markers.insert(0, current.pop())
            if current:
                groups.append((current, current_role))
            current = markers
            current_role = role
        elif not current_role and role:
            current_role = role
        current.append(node)
    if current:
        groups.append((current, current_role))
    units: list[_Unit] = []
    for nodes, role in groups[:64]:
        section_start = next(
            (node for node in nodes if str(node.get("id")) in breaks), None
        )
        heading = next(
            (
                tag
                for node in nodes
                for tag in [node, *node.find_all(_HEADING)]
                if _HEADING.fullmatch(tag.name)
            ),
            None,
        )
        identifier = (
            str(section_start["id"])
            if section_start is not None
            else str(heading.get("id"))
            if heading
            else next(
                (
                    str(tag["id"])
                    for node in nodes
                    for tag in [node, *node.find_all(id=True)]
                    if tag.get("id")
                ),
                "",
            )
        )
        if identifier:
            units.append(_Unit(identifier, role, nodes, section_start))
    return root, units


def _editable(tag: Tag) -> bool:
    return (
        tag.name == "p"
        and len(tag.get_text(" ", strip=True).encode("utf-8")) <= 4096
        and not any(
            is_export_note(parent)
            or parent.name == "figure"
            or "image-description-v1" in note_tokens(parent, "class")
            for parent in tag.parents
        )
        and not tag.find(["a", "img", "table"])
        and not any(child.get("id") for child in tag.find_all(True))
    )


def review_front_matter(
    soup: BeautifulSoup, metadata: Mapping[str, object], options: Mapping[str, object]
) -> list[dict[str, object]]:
    root, units = _units(soup, metadata, options)
    seen: dict[str, str] = {}
    rows: list[dict[str, object]] = []
    block_count = 0
    for unit in units:
        source_anchor = unit.identifier
        if source_anchor.startswith("epub-front-block-"):
            previous = unit.nodes[0].find_previous(_HEADING)
            following = unit.nodes[0].find_next(_HEADING)
            heading = previous or following
            if heading is not None and heading.get("id"):
                source_anchor = str(heading["id"])
        blocks: list[dict[str, object]] = []
        for node in unit.nodes:
            for tag in [node, *node.find_all("p")]:
                if tag.name != "p" or not tag.get("id") or block_count >= 512:
                    continue
                text = tag.get_text(" ", strip=True)
                if not text:
                    continue
                identifier = str(tag["id"])
                normalized = " ".join(text.split()).casefold()
                blocks.append(
                    {
                        "id": identifier,
                        "text": text.encode("utf-8")[:4096].decode("utf-8", "ignore"),
                        "truncated": len(text.encode("utf-8")) > 4096,
                        "sha256": _digest([tag]),
                        "editable": _editable(tag),
                        "can_start_section": tag.parent is root and _editable(tag),
                        "duplicate_of": seen.get(normalized),
                    }
                )
                seen.setdefault(normalized, identifier)
                block_count += 1
        rows.append(
            {
                "id": unit.identifier,
                "source_anchor": source_anchor,
                "role": unit.role,
                "title": " ".join(
                    node.get_text(" ", strip=True)
                    or " ".join(
                        str(image.get("alt") or "")
                        for image in (
                            [node] if node.name == "img" else node.find_all("img")
                        )
                    )
                    for node in unit.nodes
                )[:240],
                "sha256": _digest(unit.nodes),
                "blocks": blocks,
            }
        )
    return rows


def apply_front_matter(
    soup: BeautifulSoup, metadata: Mapping[str, object], options: Mapping[str, object]
) -> dict[str, object]:
    """Validate every editorial witness before changing any content."""
    if options.get("profile") != "publishing-v2":
        return dict(options)
    root, units = _units(soup, metadata, options)
    unit_map = {unit.identifier: unit for unit in units}
    eligible = {
        str(tag["id"]): tag
        for unit in units
        for node in unit.nodes
        for tag in [node, *node.find_all("p")]
        if tag.get("id") and tag.name == "p"
    }
    omitted = cast(Mapping[str, str], options.get("omitted_front_matter", {}))
    edits = cast(Mapping[str, Mapping[str, str]], options.get("front_matter_edits", {}))
    removed: list[Tag] = []
    omitted_units: set[str] = set()
    for identifier, witness in omitted.items():
        if identifier in unit_map:
            nodes = unit_map[identifier].nodes
            omitted_units.add(identifier)
        elif identifier in eligible:
            nodes = [eligible[identifier]]
        else:
            raise EpubExportContractError("epub-front-matter-target")
        if _digest(nodes) != witness:
            raise EpubExportContractError("epub-front-matter-stale")
        if any(
            is_export_note(tag) or "image-description-v1" in note_tokens(tag, "class")
            for node in nodes
            for tag in [node, *node.find_all(True), *node.parents]
        ):
            raise EpubExportContractError("epub-front-matter-protected")
        removed.extend(nodes)
    for identifier, edit in edits.items():
        tag = eligible.get(identifier)
        if tag is None or not _editable(tag):
            raise EpubExportContractError("epub-front-matter-edit-target")
        if _digest([tag]) != edit["sha256"]:
            raise EpubExportContractError("epub-front-matter-stale")
        if any(tag is node or node in tag.parents for node in removed):
            raise EpubExportContractError("epub-front-matter-edit-conflict")
    removed_ids = {
        str(tag["id"])
        for node in removed
        for tag in [node, *node.find_all(id=True)]
        if tag.get("id")
    }
    for tag in soup.find_all(True):
        if any(tag is node or node in tag.parents for node in removed):
            continue
        href = str(tag.get("href") or "")
        references: set[str] = {href[1:]} if href.startswith("#") else set()
        for attribute in ("aria-labelledby", "aria-describedby", "headers"):
            value = tag.get(attribute, "")
            references.update(value if isinstance(value, list) else str(value).split())
        if references & removed_ids:
            raise EpubExportContractError("epub-front-matter-linked")
    order = cast(list[str], options.get("front_matter_order", []))
    retained = [
        unit.identifier for unit in units if unit.identifier not in omitted_units
    ]
    if order and (len(order) != len(retained) or set(order) != set(retained)):
        raise EpubExportContractError("epub-front-matter-order")
    for identifier, edit in edits.items():
        eligible[identifier].string = edit["text"]
    # Nested removals are already covered by removing their parent.
    for tag in removed:
        if tag.parent is not None:
            tag.extract()
    used_ids = {str(tag["id"]) for tag in soup.find_all(id=True)}
    language = str(metadata.get("language") or "en")
    for unit in units:
        start = unit.section_start
        if start is None or start.parent is not root:
            continue
        base = f"epub-front-section-{unit.identifier}"
        identifier, suffix = base, 2
        while identifier in used_ids:
            identifier = f"{base}-{suffix}"
            suffix += 1
        used_ids.add(identifier)
        heading = soup.new_tag(
            "h1",
            attrs={
                "id": identifier,
                "epub:type": unit.role,
                "lang": epub_label_language(language),
            },
        )
        heading.string = epub_labels(language)[unit.role]
        start.insert_before(heading)
        unit.nodes.insert(unit.nodes.index(start), heading)
    if order:
        remaining = [
            node
            for identifier in order
            for node in unit_map[identifier].nodes
            if node.parent is root
        ]
        if remaining:
            position = min(list(root.children).index(node) for node in remaining)
            for node in remaining:
                node.extract()
            for offset, node in enumerate(remaining):
                root.insert(position + offset, node)
    active = dict(options)
    for name in (
        "roles",
        "page_labels",
        "table_headers",
        "image_descriptions",
        "long_descriptions",
    ):
        values = options.get(name)
        if isinstance(values, Mapping):
            active[name] = {
                key: value
                for key, value in cast(Mapping[str, object], values).items()
                if key not in removed_ids
            }
    for name in ("decorative_images", "photographic_images"):
        active[name] = [
            identifier
            for identifier in cast(list[str], options.get(name, []))
            if identifier not in removed_ids
        ]
    return active
