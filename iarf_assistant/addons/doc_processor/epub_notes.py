"""Place evidenced local note sequences in an EPUB's disposable export DOM."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from urllib.parse import unquote

from bs4 import BeautifulSoup, Tag

from .note_arrangement import normalize_note_arrangement

_SCOPE = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")
_HEADING = re.compile(r"h([1-6])\Z")


def _tokens(tag: Tag, name: str) -> set[str]:
    value = tag.get(name)
    return (
        {str(item) for item in value}
        if isinstance(value, list)
        else set(str(value or "").split())
    )


def _is_note(tag: Tag) -> bool:
    return bool(
        _tokens(tag, "epub:type") & {"footnote", "endnote"}
        or _tokens(tag, "role") & {"doc-footnote", "doc-endnote"}
    )


def _is_collection(tag: Tag) -> bool:
    return "doc-endnotes" in _tokens(tag, "role") or "endnotes" in _tokens(
        tag, "epub:type"
    )


def _within_notes(tag: Tag) -> bool:
    return any(
        _is_note(parent) or _is_collection(parent) for parent in [tag, *tag.parents]
    )


def _scope(tag: Tag) -> tuple[bool, str | None]:
    for ancestor in [tag, *tag.parents]:
        if "data-footnote-scope" in ancestor.attrs:
            value = str(ancestor.get("data-footnote-scope") or "")
            return bool(_SCOPE.fullmatch(value)), value
    return True, None


def _local_link_targets(tag: Tag) -> set[str]:
    attributes = ("href", "cite") if tag.name == "a" else ("cite",)
    return {
        unquote(target[1:])
        for attribute in attributes
        if (target := str(tag.get(attribute) or "")).startswith("#") and len(target) > 1
    }


@dataclass(eq=False)
class _Unit:
    heading: Tag
    boundary: Tag | None = None


@dataclass
class _Reference:
    unit: _Unit | None
    scope_valid: bool
    scope: str | None


@dataclass
class _Placement:
    note: Tag
    list_kind: str
    list_type: str | None
    ordinal: int | None


def _native_list_context(note: Tag) -> _Placement:
    parent = note.parent
    if (
        note.name != "li"
        or not isinstance(parent, Tag)
        or parent.name not in {"ol", "ul"}
    ):
        return _Placement(note, "div", None, None)
    if parent.name == "ul":
        return _Placement(note, "ul", None, None)
    items = parent.find_all("li", recursive=False)
    step = -1 if parent.has_attr("reversed") else 1
    try:
        ordinal = int(str(parent.get("start", str(len(items) if step < 0 else 1))))
    except ValueError:
        ordinal = 1
    for item in items:
        with suppress(ValueError):
            ordinal = int(str(item.get("value", str(ordinal))))
        if item is note:
            break
        ordinal += step
    return _Placement(note, "ol", str(parent.get("type") or "1"), ordinal)


def _index_units(
    soup: BeautifulSoup, level: int, note_ids: set[str]
) -> tuple[list[_Unit], dict[str, list[_Reference]], dict[str, set[_Unit | None]]]:
    units: list[_Unit] = []
    references: dict[str, list[_Reference]] = defaultdict(list)
    scope_units: dict[str, set[_Unit | None]] = defaultdict(set)
    current: _Unit | None = None
    for tag in soup.find_all(True):
        inside_notes = _within_notes(tag)
        heading = _HEADING.fullmatch(tag.name)
        if heading and not inside_notes and int(heading[1]) <= level:
            if current is not None:
                current.boundary = tag
            current = _Unit(tag) if int(heading[1]) == level else None
            if current is not None:
                units.append(current)
        for target in _local_link_targets(tag) & note_ids:
            valid, scope = _scope(tag)
            unit = None if inside_notes else current
            references[target].append(_Reference(unit, valid, scope))
            if valid and scope:
                scope_units[scope].add(unit)
    return units, references, scope_units


def _owning_unit(
    note: Tag,
    incoming: list[_Reference],
    scope_units: dict[str, set[_Unit | None]],
    *,
    document_numbering: bool = False,
) -> _Unit | None:
    if not incoming or incoming[0].unit is None:
        return None
    unit = incoming[0].unit
    valid, scope = _scope(note)
    witnessed_units: set[_Unit | None] = (
        scope_units.get(scope, set()) if scope else set()
    )
    if not valid or (
        not document_numbering and witnessed_units and witnessed_units != {unit}
    ):
        return None
    for reference in incoming:
        if reference.unit is not unit or not reference.scope_valid:
            return None
        if (
            not document_numbering
            and reference.scope
            and len(scope_units.get(reference.scope, set())) > 1
        ):
            return None
        if scope and reference.scope and scope != reference.scope:
            return None
    declared_scopes = {reference.scope for reference in incoming if reference.scope}
    if len(declared_scopes) > 1:
        return None
    return unit


def _append_note(soup: BeautifulSoup, collection: Tag, placement: _Placement) -> None:
    kind = placement.list_kind
    previous = collection.contents[-1] if collection.contents else None
    list_tag = previous if isinstance(previous, Tag) else None
    if (
        list_tag is None
        or list_tag.name != kind
        or list_tag.get("type") != placement.list_type
    ):
        list_tag = soup.new_tag(kind)
        if kind == "div":
            list_tag["role"] = "list"
        if placement.list_type is not None:
            list_tag["type"] = placement.list_type
        collection.append(list_tag)
    _, source_scope = _scope(placement.note)
    note = placement.note.extract()
    if source_scope:
        note["data-footnote-scope"] = source_scope
    if placement.ordinal is not None:
        note["value"] = str(placement.ordinal)
    if kind == "div" and "listitem" not in _tokens(note, "role"):
        item = soup.new_tag("div", role="listitem")
        item.append(note)
        list_tag.append(item)
    else:
        list_tag.append(note)


def _protected_ids(soup: BeautifulSoup) -> set[str]:
    targets = {
        target for tag in soup.find_all(True) for target in _local_link_targets(tag)
    }
    for tag in soup.find_all(True):
        for attribute in ("aria-labelledby", "aria-describedby", "headers"):
            targets.update(_tokens(tag, attribute))
    return targets


def _addressed_collections(soup: BeautifulSoup) -> set[int]:
    protected = _protected_ids(soup)
    return {
        id(collection)
        for collection in soup.find_all(_is_collection)
        if any(
            str(tag.get("id") or "") in protected
            and not _is_note(tag)
            and not any(_is_note(parent) for parent in tag.parents)
            for tag in [collection, *collection.find_all(True)]
        )
    }


def _clean_collections(soup: BeautifulSoup) -> None:
    protected = _protected_ids(soup)
    for collection in list(soup.find_all(_is_collection)):
        for wrapper in reversed(list(collection.find_all(["ol", "ul", "div"]))):
            empty = not any(
                isinstance(child, Tag) or str(child).strip()
                for child in wrapper.contents
            )
            if empty and str(wrapper.get("id") or "") not in protected:
                wrapper.decompose()
        if collection.find(_is_note) is not None:
            continue
        # An emptied collection's heading is decoration. Keep ordinary prose,
        # media, and any still-addressable collection/heading anchors.
        if any(
            str(tag.get("id") or "") in protected
            for tag in [collection, *collection.find_all(True)]
        ):
            continue
        if collection.find(["p", "img", "table", "figure", "svg"]) is not None:
            continue
        strings = [
            text for text in collection.find_all(string=True) if str(text).strip()
        ]
        if all(
            any(_HEADING.fullmatch(parent.name) for parent in text.parents)
            for text in strings
        ):
            collection.decompose()


def _branch_below(tag: Tag, ancestor: Tag) -> Tag:
    """Lift insertion past wrappers only while no preceding content is crossed."""
    while isinstance(tag.parent, Tag) and tag.parent is not ancestor:
        if any(
            isinstance(sibling, Tag) or str(sibling).strip()
            for sibling in tag.previous_siblings
        ):
            break
        tag = tag.parent
    return tag


def _insert_collection(soup: BeautifulSoup, unit: _Unit, collection: Tag) -> None:
    boundary = unit.boundary
    if boundary is not None:
        parents = {id(parent) for parent in unit.heading.parents}
        common = next(parent for parent in boundary.parents if id(parent) in parents)
        _branch_below(boundary, common).insert_before(collection)
        return
    container = unit.heading.find_parent("main") or soup
    after_heading = False
    for tag in container.find_all(True):
        if tag is unit.heading:
            after_heading = True
        if after_heading and _is_collection(tag):
            _branch_below(tag, container).insert_before(collection)
            return
    container.append(collection)


def place_chapter_notes(soup: BeautifulSoup, metadata: Mapping[str, object]) -> None:
    """Move only notes whose explicit incoming links establish one local unit.

    The arrangement controls eligibility; neither label similarity nor a
    nearest-heading guess supplies note identity. Unreferenced, conflicting and
    cross-unit notes keep their source position. IDs and backlinks stay intact.
    """
    arrangement = normalize_note_arrangement(metadata.get("note_arrangement"))
    level = arrangement.get("scope_heading_level")
    local = (
        arrangement.get("numbering") in {"chapter", "section"}
        or arrangement.get("placement") == "chapter_endnotes"
    )
    if (
        arrangement.get("placement") == "none"
        or not local
        or not isinstance(level, int)
        or isinstance(level, bool)
    ):
        return
    notes = [
        tag
        for tag in soup.find_all(_is_note)
        if not any(_is_note(parent) for parent in tag.parents)
    ]
    note_ids = {str(note.get("id") or "") for note in notes}
    units, references, scope_units = _index_units(soup, level, note_ids)
    addressed = _addressed_collections(soup)
    groups: dict[_Unit, list[_Placement]] = defaultdict(list)
    for note in notes:
        if any(id(parent) in addressed for parent in note.parents):
            continue
        unit = _owning_unit(
            note,
            references.get(str(note.get("id") or ""), []),
            scope_units,
            document_numbering=arrangement.get("numbering") == "document",
        )
        if unit is not None:
            groups[unit].append(_native_list_context(note))
    if not groups:
        return
    collections: dict[_Unit, Tag] = {}
    for unit, placements in groups.items():
        collection = soup.new_tag(
            "section",
            attrs={
                "class": "chapter-endnotes",
                "role": "doc-endnotes",
                "epub:type": "endnotes",
            },
        )
        # A long unit may span several XHTML files; an ARIA IDREF to its
        # opener would then be invalid. Its source title remains a local label.
        heading_text = unit.heading.get_text(" ", strip=True)
        if heading_text:
            collection["aria-label"] = heading_text[:512]
        collection.append(soup.new_tag("hr"))
        for placement in placements:
            _append_note(soup, collection, placement)
        collections[unit] = collection
    _clean_collections(soup)
    for unit in units:
        if unit in collections:
            _insert_collection(soup, unit, collections[unit])


# Shared projection helpers. Placement and grouping use the same identity and
# ownership rules; these aliases preserve the retained implementation's API.
index_note_units = _index_units
is_note_collection = _is_collection
is_export_note = _is_note
owning_note_unit = _owning_unit
is_within_notes = _within_notes
note_tokens = _tokens
