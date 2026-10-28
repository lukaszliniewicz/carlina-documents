from __future__ import annotations

import hashlib
import html as html_lib
import logging
import re
from collections.abc import Callable, Sequence
from itertools import pairwise
from typing import Any, Dict, List, Optional
from urllib.parse import unquote

from bs4 import BeautifulSoup, NavigableString, Tag
from .pdf_rendering import get_optimal_scale_factor as get_optimal_scale_factor
from .table_assembly import merge_continued_table
from ..image_crops import process_extracted_images as _process_extracted_images

logger = logging.getLogger(__name__)


class ListContinuationError(ValueError):
    """A content-free list-boundary conflict with physical page context."""

    def __init__(
        self,
        reason_code: str,
        *,
        previous_page: int,
        page_number: int,
        section: str,
    ) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.previous_page = previous_page
        self.page_number = page_number
        self.section = section


# --- 2. CLEANING & IMAGE EXTRACTION (From html_cleaner.py) ---
def clean_markdown_artifacts(content: str) -> str:
    content = (content or "").strip()
    fenced = re.fullmatch(
        r"```(?:html|xml|json)?\s*\n([\s\S]*?)\n```\s*", content, flags=re.IGNORECASE
    )
    return (fenced.group(1) if fenced else content).strip()


def normalize_extracted_page_html(content: str, toc_handling: str = "omit") -> str:
    """Normalize an LLM page response to the extraction HTML contract."""
    cleaned = clean_markdown_artifacts(content)
    soup = BeautifulSoup(cleaned, "html.parser")

    for tag in list(
        soup.find_all(
            ["script", "style", "iframe", "object", "embed", "form", "input", "button"]
        )
    ):
        tag.decompose()

    for tag in soup.find_all(True):
        for attr_name in list(tag.attrs):
            lowered = str(attr_name).lower()
            if lowered.startswith("on") or lowered == "style":
                del tag.attrs[attr_name]
        for attr_name in ("href", "src"):
            value = str(tag.get(attr_name) or "").strip().lower()
            if value.startswith(("javascript:", "vbscript:")):
                del tag.attrs[attr_name]

    main_sections = soup.find_all("main_content")
    footnote_sections = soup.find_all("footnotes")
    if main_sections:
        main_html = "".join(section.decode_contents() for section in main_sections)
    else:
        excluded_ids = {id(section) for section in footnote_sections}
        body = soup.body or soup
        main_html = "".join(
            str(child) for child in body.contents if id(child) not in excluded_ids
        )
    footnotes_html = "".join(section.decode_contents() for section in footnote_sections)

    main_soup = BeautifulSoup(main_html, "html.parser")
    footnote_soup = BeautifulSoup(footnotes_html, "html.parser")
    for nested in main_soup.find_all(["main_content", "footnotes"]):
        nested.unwrap()
    for nested in footnote_soup.find_all(["main_content", "footnotes"]):
        nested.unwrap()

    # Models sometimes escape an already escaped entity (``&amp;ndash;``).
    # Decode exactly one extra layer inside text nodes.  Replacing a text node
    # cannot create active markup: BeautifulSoup escapes any resulting ``<``
    # again when serializing the document.
    for output_soup in (main_soup, footnote_soup):
        for text_node in list(output_soup.find_all(string=True)):
            decoded = html_lib.unescape(str(text_node))
            if decoded != str(text_node):
                text_node.replace_with(decoded)

    if str(toc_handling or "omit").strip().lower() != "preserve":
        remove_source_toc_blocks(main_soup)

    return (
        "<main_content>\n"
        + str(main_soup).strip()
        + "\n</main_content>\n<footnotes>\n"
        + str(footnote_soup).strip()
        + "\n</footnotes>"
    )


def process_extracted_images(
    html_content: str,
    png_path: str,
    output_folder: str,
    web_path_prefix: str,
    *,
    page_index: int | None = None,
    crop_sources: dict | None = None,
) -> str:
    """Backward-compatible entry point for PDF image cropping."""
    return _process_extracted_images(
        html_content,
        png_path,
        output_folder,
        web_path_prefix,
        page_index=page_index,
        crop_sources=crop_sources,
    )


# --- 3. HTML MERGING (From html_merger.py) ---
def has_continuation_class(tag: Tag) -> bool:
    """Check if the tag or any of its descendants have continuation classes."""
    if not isinstance(tag, Tag):
        return False
    classes = tag.get("class", [])
    if any(
        c in ["continues", "continues-next", "continued", "continued-from-previous"]
        for c in classes
    ):
        return True
    for child in tag.find_all(True):
        classes = child.get("class", [])
        if any(
            c in ["continues", "continues-next", "continued", "continued-from-previous"]
            for c in classes
        ):
            return True
    return False


def has_boundary_continuation(previous: Tag, following: Tag) -> bool:
    """Read directional evidence on this boundary, not on unrelated descendants."""
    if not _same_valid_footnote_scope(previous, following):
        return False
    return bool(
        _tag_classes(previous).intersection({"continues", "continues-next"})
        or _tag_classes(following).intersection(
            {"continued", "continued-from-previous"}
        )
    )


def is_structural_boundary_block(tag: Tag) -> bool:
    """Explicit title/header matter is a hard boundary, unlike punctuation."""
    return isinstance(tag, Tag) and (
        tag.name in {"h1", "h2", "h3", "h4", "h5", "h6"}
        or bool(
            _tag_classes(tag).intersection(
                {"title", "author", "editor", "translator", "page_header", "page_footer"}
            )
        )
    )


def is_merge_container(tag: Tag) -> bool:
    """Containers that are safe to merge structurally when a boundary says auto/merge."""
    if not isinstance(tag, Tag):
        return False
    if tag.name in ["ul", "ol", "table"]:
        return True
    if tag.name == "blockquote":
        return has_continuation_class(tag)
    return tag.name in ["div", "article", "section"] and has_continuation_class(tag)


def _is_source_toc(tag: Tag) -> bool:
    return isinstance(tag, Tag) and tag.name == "nav" and _has_class(
        tag, "source-toc"
    )


def _direct_source_toc_title(nav: Tag) -> Tag | None:
    for child in nav.children:
        if not isinstance(child, Tag):
            continue
        if child.name in {"h1", "h2", "h3", "h4", "h5", "h6"} and _has_class(
            child, "source-toc-title"
        ):
            return child
    return None


def _source_toc_title_key(title: Tag) -> str:
    return re.sub(r"\s+", " ", title.get_text(" ", strip=True)).strip().casefold()


def _source_toc_pair_can_merge(first: Tag, second: Tag) -> bool:
    """Keep a newly titled printed list separate from a continued source TOC."""

    first_title = _direct_source_toc_title(first)
    second_title = _direct_source_toc_title(second)
    if second_title is None:
        return True
    return (
        first_title is not None
        and _source_toc_title_key(first_title) == _source_toc_title_key(second_title)
    )


def has_source_toc_omission_marker(content: str) -> bool:
    """Return whether a model explicitly classified the page as TOC-only.

    The marker is intentionally empty: the extraction model does not spend
    tokens transcribing a printed TOC, and normalization removes the marker
    before document content is stored.  Requiring the exact inert ``nav``
    shape keeps an ordinary empty response distinct from an explicit
    policy-backed omission.
    """

    soup = BeautifulSoup(clean_markdown_artifacts(content), "html.parser")
    roots = [node for node in soup.contents if str(node).strip()]
    if len(roots) != 2 or not all(isinstance(node, Tag) for node in roots):
        return False

    main, footnotes = roots
    if main.name != "main_content" or main.attrs:
        return False
    if footnotes.name != "footnotes" or footnotes.attrs or footnotes.decode_contents().strip():
        return False

    main_children = [node for node in main.contents if str(node).strip()]
    if len(main_children) != 1 or not isinstance(main_children[0], Tag):
        return False
    nav = main_children[0]
    return (
        nav.name == "nav"
        and nav.attrs == {"class": ["source-toc"]}
        and not nav.decode_contents().strip()
    )


def _source_toc_boundary_target(nav: Tag) -> Tag:
    """Return a contract-valid home for a page marker in a continued TOC."""

    for entry in nav.find_all("div", class_="toc-entry", recursive=False):
        title = entry.find("span", class_="toc-entry-title", recursive=False)
        if title is not None:
            return title
    return nav


def _merge_source_toc(first: Tag, second: Tag) -> None:
    """Append continued entries without duplicating the one allowed TOC title."""

    repeated_title = _direct_source_toc_title(second)
    if repeated_title is not None:
        repeated_title.decompose()
    for child in list(second.contents):
        first.append(child)
    second.decompose()


def get_boundary_decision(
    boundary_flags: dict, boundary_index: int, section_name: str
) -> str:
    flags = boundary_flags or {}
    raw_decision = flags.get(boundary_index, flags.get(str(boundary_index), "auto"))

    if isinstance(raw_decision, bool):
        return "merge" if raw_decision else "auto"
    if isinstance(raw_decision, str):
        decision = raw_decision.lower()
    elif isinstance(raw_decision, dict):
        section_decision = raw_decision.get(
            section_name, raw_decision.get("action", "auto")
        )
        if isinstance(section_decision, dict):
            section_decision = section_decision.get("action", "auto")
        decision = str(section_decision or "auto").lower()
    else:
        decision = "auto"

    return decision if decision in {"merge", "separate", "auto"} else "auto"


def _tag_classes(tag: Tag) -> set[str]:
    if not isinstance(tag, Tag):
        return set()
    classes = tag.get("class", []) or []
    if isinstance(classes, str):
        classes = classes.split()
    return {str(class_name) for class_name in classes}


def _has_class(tag: Tag, class_name: str) -> bool:
    return class_name in _tag_classes(tag)


_LEGACY_SOURCE_TOC_TITLES = {
    "contents",
    "table of contents",
    "contents page",
    "inhalt",
    "inhaltsverzeichnis",
    "sommaire",
    "table des matières",
    "índice",
    "índice general",
    "sumario",
    "spis treści",
    "obsah",
    "sadržaj",
    "innehåll",
    "indholdsfortegnelse",
    "innholdsfortegnelse",
    "sisällys",
    "içindekiler",
    "περιεχόμενα",
    "содержание",
    "оглавление",
    "зміст",
    "תוכן העניינים",
    "فهرس المحتويات",
    "目录",
    "目錄",
    "目次",
    "차례",
    "목차",
    "daftar isi",
}


def _is_legacy_source_toc_title(tag: Tag) -> bool:
    """Recognize only generic labels used by the pre-contract ``*.toc`` form."""
    if tag.name not in {"h1", "h2", "h3", "h4", "h5", "h6"}:
        return False
    title = re.sub(r"\s+", " ", tag.get_text(" ", strip=True)).strip()
    title = title.strip(" .,:;!?\u2013\u2014-\u2026。：").casefold()
    return title in _LEGACY_SOURCE_TOC_TITLES


def remove_source_toc_blocks(soup: BeautifulSoup) -> int:
    """Remove model-marked source TOCs without dropping other page content."""
    removed = 0

    for container in list(
        soup.find_all(
            True, class_=lambda value: value and "source-toc" in str(value).split()
        )
    ):
        if container.parent is None:
            continue
        previous = container.previous_sibling
        while previous is not None and not isinstance(previous, Tag):
            if str(previous).strip():
                previous = None
                break
            previous = previous.previous_sibling
        if (
            isinstance(previous, Tag)
            and _has_class(previous, "source-toc-title")
            and previous.name in {"h1", "h2", "h3", "h4", "h5", "h6"}
        ):
            previous.decompose()
        parent = container.parent if isinstance(container.parent, Tag) else None
        container.decompose()
        removed += 1
        if (
            isinstance(parent, Tag)
            and parent.name in {"div", "section", "nav", "aside"}
            and not parent.get_text(" ", strip=True)
            and not parent.find(["img", "table", "figure"])
        ):
            parent.decompose()

    for toc in list(soup.find_all(["ol", "ul"])):
        if toc.parent is None or not _has_class(toc, "toc"):
            continue

        parent = toc.parent if isinstance(toc.parent, Tag) else None
        previous = toc.previous_sibling
        while previous is not None and not isinstance(previous, Tag):
            if str(previous).strip():
                previous = None
                break
            previous = previous.previous_sibling

        # Canonical titles are explicit. For the pre-contract ``ol.toc`` form,
        # retain a deliberately narrow compatibility list of generic labels;
        # never infer ownership for an arbitrary adjacent chapter heading.
        if (
            isinstance(previous, Tag)
            and (
                _has_class(previous, "source-toc-title")
                or _is_legacy_source_toc_title(previous)
            )
        ):
            previous.decompose()

        toc.decompose()
        removed += 1

        if (
            isinstance(parent, Tag)
            and parent.name in {"div", "section", "nav", "aside"}
            and not parent.get_text(" ", strip=True)
            and not parent.find(["img", "table", "figure"])
        ):
            parent.decompose()

    return removed


def _is_toc_list(tag: Tag) -> bool:
    return isinstance(tag, Tag) and tag.name in ["ol", "ul"] and _has_class(tag, "toc")


def _split_toc_entry_text(text: str) -> tuple[str, str | None]:
    """Separate an explicit dot-leader locator without discarding source text."""
    compact = re.sub(r"\s+", " ", str(text or "")).strip()
    match = re.fullmatch(
        r"(?P<title>.+?)\s*(?:\.{2,}|…+|·{2,}|•{2,})\s*(?P<label>\S{1,20})",
        compact,
    )
    if not match:
        return compact, None
    return match.group("title").strip(), match.group("label").strip()


def _direct_text_without_child_lists(tag: Tag) -> str:
    parts = []
    for child in tag.contents:
        if isinstance(child, Tag) and child.name in ["ol", "ul"]:
            continue
        if isinstance(child, Tag):
            parts.append(child.get_text(" ", strip=True))
        else:
            parts.append(str(child).strip())
    return " ".join(part for part in parts if part).strip()


def _append_toc_entries(soup: BeautifulSoup, nav: Tag, toc: Tag, depth: int) -> None:
    for item in toc.find_all("li", recursive=False):
        title_node = item.find("span", class_="toc-entry-title", recursive=False)
        label_node = item.find("span", class_="toc-page-label", recursive=False)
        if title_node is not None:
            title = re.sub(r"\s+", " ", title_node.get_text(" ", strip=True)).strip()
            label = (
                re.sub(r"\s+", " ", label_node.get_text(" ", strip=True)).strip()
                if label_node is not None
                else None
            )
        else:
            title, label = _split_toc_entry_text(_direct_text_without_child_lists(item))

        if title or label:
            entry = soup.new_tag(
                "div",
                **{"class": ["toc-entry", f"toc-depth-{max(1, min(depth, 6))}"]},
            )
            if title:
                title_span = soup.new_tag("span", **{"class": "toc-entry-title"})
                title_span.string = title
                entry.append(title_span)
            if label:
                if title:
                    entry.append(" ")
                label_span = soup.new_tag("span", **{"class": "toc-page-label"})
                label_span.string = label
                entry.append(label_span)
            nav.append(entry)

        for child_list in item.find_all(["ol", "ul"], recursive=False):
            _append_toc_entries(soup, nav, child_list, depth + 1)


def normalize_toc_lists(soup: BeautifulSoup):
    for toc in list(soup.find_all(["ol", "ul"])):
        if not _is_toc_list(toc):
            continue
        classes = ["source-toc"]
        if "continued" in _tag_classes(toc):
            classes.append("continued")
        nav = soup.new_tag("nav", **{"class": classes})
        _append_toc_entries(soup, nav, toc, 1)
        toc.replace_with(nav)


_SUPERSCRIPT_DIGIT_TRANSLATION = {
    "⁰": "0",
    "¹": "1",
    "²": "2",
    "³": "3",
    "⁴": "4",
    "⁵": "5",
    "⁶": "6",
    "⁷": "7",
    "⁸": "8",
    "⁹": "9",
}

# Keep common mojibake variants as fallbacks for noisy OCR/extraction payloads.
_MOJIBAKE_SUPERSCRIPT_TRANSLATION = {
    "â°": "0",
    "Â¹": "1",
    "Â²": "2",
    "Â³": "3",
    "â´": "4",
    "âµ": "5",
    "â¶": "6",
    "â·": "7",
    "â¸": "8",
    "â¹": "9",
}

_SUPERSCRIPT_TRANSLATION = str.maketrans(_SUPERSCRIPT_DIGIT_TRANSLATION)
_SOURCE_PAGE_ATTR = "data-docproc-source-page"
_FOOTNOTE_SCOPE_ATTR = "data-footnote-scope"
_FOOTNOTE_SCOPE_PATTERN = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")


def _normalize_footnote_label(value: str) -> str:
    label = str(value or "")
    for bad_token, replacement in _MOJIBAKE_SUPERSCRIPT_TRANSLATION.items():
        label = label.replace(bad_token, replacement)
    label = label.translate(_SUPERSCRIPT_TRANSLATION).strip()
    label = re.sub(r"^[\[(\s]+|[\])\s]+$", "", label)
    label = re.sub(r"\s+", " ", label).strip()
    label = re.sub(r"[.)]+$", "", label).strip()
    return label


def _display_footnote_label(value: str) -> str:
    """Normalize superscript digits without removing printed label punctuation."""
    label = str(value or "")
    for bad_token, replacement in _MOJIBAKE_SUPERSCRIPT_TRANSLATION.items():
        label = label.replace(bad_token, replacement)
    return label.translate(_SUPERSCRIPT_TRANSLATION).strip()


def _footnote_label_from_id(raw_id: str) -> str:
    raw_id = str(raw_id or "").strip()
    if raw_id.startswith("fm"):
        return _normalize_footnote_label(raw_id[2:])
    if raw_id.startswith("f"):
        return _normalize_footnote_label(raw_id[1:])
    return ""


def _safe_footnote_id_part(label: str) -> str:
    safe_label = re.sub(r"[^A-Za-z0-9_-]+", "-", str(label or "").strip()).strip("-")
    return safe_label[:32] or "note"


def _normalized_footnote_text(tag: Tag) -> str:
    return re.sub(r"\s+", " ", tag.get_text(" ", strip=True)).strip().lower()


def _extract_footnote_label(footnote: Tag, *, preserve_note_labels: bool = False) -> str:
    label = _normalize_footnote_label(
        footnote.get("data-label") or footnote.get("data-footnote-label") or ""
    )
    if label:
        return label

    label = _footnote_label_from_id(footnote.get("id"))
    if label:
        return label

    first_text = footnote.get_text(" ", strip=True)
    pattern = (
        r"^((?:[0-9]+|[*†‡§#]+)[.)]?|[A-Za-z]{1,3}[.)]|\[[0-9A-Za-z*†‡§#]+\][.]?|\([0-9A-Za-z*†‡§#]+\)[.]?)\s+"
        if preserve_note_labels
        else r"^([\[(]?[0-9A-Za-z*†‡§#]+[\])]?[.)]?)\s+"
    )
    match = re.match(pattern, first_text)
    return _normalize_footnote_label(match.group(1)) if match else ""


def _printed_note_label(footnote: Tag, match_label: str) -> str:
    declared = footnote.get("data-label") or footnote.get("data-footnote-label")
    if declared:
        return _display_footnote_label(str(declared))
    marker = footnote.find(class_="footnote-marker")
    if marker is not None:
        return _display_footnote_label(marker.get_text("", strip=True))
    text = footnote.get_text(" ", strip=True)
    match = re.match(r"^([\[(]?[0-9A-Za-z*†‡§#]+[\])]?[.)]?)\s+", text)
    if match and _normalize_footnote_label(match.group(1)) == match_label:
        return _display_footnote_label(match.group(1))
    return match_label


def _normalized_label_candidates(*raw_values) -> list[str]:
    candidates = []
    for raw_value in raw_values:
        label = _normalize_footnote_label(raw_value)
        if label and label not in candidates:
            candidates.append(label)
    return candidates


def _source_page_index(tag: Tag) -> int | None:
    try:
        value = int(tag.get(_SOURCE_PAGE_ATTR))
    except TypeError, ValueError:
        return None
    return value if value >= 0 else None


def _footnote_scope_state(tag: Tag) -> tuple[bool, bool, str | None]:
    """Return the nearest declared footnote scope and whether it is valid."""
    current = tag
    while isinstance(current, Tag):
        if _FOOTNOTE_SCOPE_ATTR in current.attrs:
            raw_scope = current.get(_FOOTNOTE_SCOPE_ATTR)
            scope = raw_scope if isinstance(raw_scope, str) else str(raw_scope)
            return True, bool(_FOOTNOTE_SCOPE_PATTERN.fullmatch(scope)), scope
        current = current.parent
    return False, True, None


def _same_valid_footnote_scope(previous: Tag, following: Tag) -> bool:
    """Allow boundaries only when both sides have the same valid scope state."""
    previous_state = _footnote_scope_state(previous)
    following_state = _footnote_scope_state(following)
    previous_declared, previous_valid, previous_scope = previous_state
    following_declared, following_valid, following_scope = following_state
    if not previous_valid or not following_valid:
        return False
    if previous_declared != following_declared:
        return False
    return not previous_declared or previous_scope == following_scope


def _capture_inherited_footnote_scope(tag: Tag) -> None:
    """Materialize the nearest source scope before page wrappers are removed."""
    declared, _valid, scope = _footnote_scope_state(tag)
    if declared:
        tag[_FOOTNOTE_SCOPE_ATTR] = scope


def _scope_compatible_entries(
    entries: list[dict], scope_state: tuple[bool, bool, str | None]
) -> list[dict]:
    declared, valid, scope = scope_state
    if not valid:
        return []
    if declared:
        return [
            entry
            for entry in entries
            if entry["scope_declared"]
            and entry["scope_valid"]
            and entry["scope"] == scope
        ]
    return [
        entry
        for entry in entries
        if not entry["scope_declared"] and entry["scope_valid"]
    ]


def _unique_entry(entries: list[dict]) -> dict | None:
    return entries[0] if len(entries) == 1 else None


def _unique_same_page_entry(
    entries: list[dict], marker_page: int | None
) -> dict | None:
    if marker_page is None:
        return None
    return _unique_entry(
        [entry for entry in entries if entry.get("page_index") == marker_page]
    )


def _resolve_label_entries(
    entries: list[dict],
    scope_state: tuple[bool, bool, str | None],
    marker_page: int | None,
) -> dict | None:
    candidates = _scope_compatible_entries(entries, scope_state)
    declared, valid, _scope = scope_state
    if not valid:
        return None
    if declared:
        if len(candidates) == 1:
            return candidates[0]
        return _unique_same_page_entry(candidates, marker_page)
    return _unique_same_page_entry(candidates, marker_page)


def _is_conventional_footnote_target_id(target_id: str) -> bool:
    if not target_id.startswith("f") or len(target_id) < 2:
        return False
    label = _normalize_footnote_label(target_id[1:])
    return 1 <= len(label) <= 8 and all(
        character.isalnum() or character in "*†‡§#" for character in label
    )


def _explicit_footnote_target(marker: Tag) -> tuple[bool, str | None, bool]:
    target_id = _local_footnote_target_id(marker)
    if target_id is not None:
        return True, target_id, _is_conventional_footnote_target_id(target_id)
    target_id = _conventional_footnote_target_id(marker)
    if target_id is not None:
        return True, target_id, True
    return False, None, False


def _resolve_explicit_target(
    target_id: str,
    conventional: bool,
    entries: list[dict],
    scope_state: tuple[bool, bool, str | None],
    marker_page: int | None,
) -> dict | None:
    target_entries = [entry for entry in entries if entry["original_id"] == target_id]
    candidates = _scope_compatible_entries(target_entries, scope_state)
    declared, valid, _scope = scope_state
    if not valid:
        return None
    if declared:
        if len(candidates) == 1:
            return candidates[0]
        return _unique_same_page_entry(candidates, marker_page)
    if conventional:
        return _unique_same_page_entry(candidates, marker_page)
    if len(target_entries) == 1 and len(candidates) == 1:
        return candidates[0]
    return _unique_same_page_entry(candidates, marker_page)


def _extract_marker_label_candidates(marker: Tag) -> list[str]:
    id_label = _footnote_label_from_id(marker.get("id"))
    return _normalized_label_candidates(
        id_label,
        marker.get("data-label"),
        marker.get("data-footnote-label"),
        marker.get_text("", strip=True),
    )


def _local_footnote_target_id(marker: Tag) -> str | None:
    href = str(marker.get("href") or "").strip()
    if not href.startswith("#"):
        href = next(
            (
                str(anchor.get("href") or "").strip()
                for anchor in marker.find_all("a", href=True)
                if str(anchor.get("href") or "").strip().startswith("#")
            ),
            "",
        )
    if href.startswith("#") and len(href) > 1:
        return unquote(href[1:])
    return None


def _conventional_footnote_target_id(marker: Tag) -> str | None:
    raw_id = str(marker.get("id") or "").strip()
    if raw_id.startswith("fm") and len(raw_id) > 2:
        return f"f{raw_id[2:]}"
    return None


def _is_symbolic_footnote_label(label: str) -> bool:
    return bool(re.fullmatch(r"[*†‡§#]+", label))


def _ensure_visible_footnote_marker(
    soup: BeautifulSoup, footnote: Tag, label: str, *, preserve_note_labels: bool = True
):
    if not label:
        return

    for child in footnote.contents:
        if isinstance(child, Tag):
            child_classes = _tag_classes(child)
            if child.name == "strong" and "footnote-marker" in child_classes:
                if not preserve_note_labels:
                    child.string = f"{label}. "
                return
            break
        if str(child).strip():
            break

    # A source-authored body may already contain its printed label. Do not
    # prepend a second one or rewrite its punctuation.
    if preserve_note_labels and re.match(r"^" + re.escape(label) + r"[.)]?(?:\s+|$)", footnote.get_text(" ", strip=True)):
        return
    marker_span = soup.new_tag("strong", **{"class": "footnote-marker"})
    # Printed labels supply their own punctuation; only retained assembly adds it.
    suffix = "" if preserve_note_labels else "."
    marker_span.string = f"{label}{suffix} "
    footnote.insert(0, marker_span)


def normalize_footnote_links(
    soup_main: BeautifulSoup, soup_footnotes: BeautifulSoup, *, preserve_note_labels: bool = True
):
    footnotes = soup_footnotes.find_all(class_="footnote")
    label_entries = {}
    entries = []
    reference_ids = {}

    for index, footnote in enumerate(footnotes, start=1):
        original_id = str(footnote.get("id") or "").strip()
        original_id_label = _footnote_label_from_id(footnote.get("id"))
        extracted_label = _extract_footnote_label(footnote, preserve_note_labels=preserve_note_labels)
        label = extracted_label or ("" if preserve_note_labels else str(index))
        printed_label = _printed_note_label(footnote, label) if preserve_note_labels else label
        scope_declared, scope_valid, scope = _footnote_scope_state(footnote)
        if scope_declared and scope_valid:
            footnote[_FOOTNOTE_SCOPE_ATTR] = scope
        match_keys = _normalized_label_candidates(
            original_id_label,
            footnote.get("data-label"),
            footnote.get("data-footnote-label"),
            extracted_label,
        )

        text_hash = hashlib.sha1(
            f"{label}\n{_normalized_footnote_text(footnote)}".encode("utf-8")
        ).hexdigest()[:10]
        footnote_id = f"fn-{index}-{_safe_footnote_id_part(label)}-{text_hash}"

        footnote["id"] = footnote_id
        if printed_label:
            footnote["data-footnote-label"] = printed_label
        footnote.name = "li"
        footnote["role"] = "doc-endnote"
        footnote["epub:type"] = "endnote"
        _ensure_visible_footnote_marker(soup_footnotes, footnote, printed_label, preserve_note_labels=preserve_note_labels)

        entry = {
            "id": footnote_id,
            "original_id": original_id,
            "label": printed_label,
            "keys": match_keys,
            "order": index,
            "page_index": _source_page_index(footnote),
            "scope_declared": scope_declared,
            "scope_valid": scope_valid,
            "scope": scope,
        }
        entries.append(entry)
        for key in match_keys:
            label_entries.setdefault(key, []).append(entry)

    def marker_label_entries(marker_label_candidates: list[str]) -> list[dict]:
        found = []
        seen_orders = set()
        for label_candidate in marker_label_candidates:
            for candidate in label_entries.get(label_candidate, []):
                if candidate["order"] not in seen_orders:
                    seen_orders.add(candidate["order"])
                    found.append(candidate)
        return found

    def is_marker_candidate(tag: Tag) -> bool:
        if tag.name == "span" and _tag_classes(tag).intersection(
            {"inline-footnote", "footnote_marker"}
        ):
            return True
        if tag.name != "sup" or tag.find(True) is not None:
            return False
        label = _normalize_footnote_label(tag.get_text("", strip=True))
        if not re.fullmatch(r"[0-9A-Za-z*†‡§#]{1,8}", label):
            return False
        explicit_target, target_id, _conventional = _explicit_footnote_target(tag)
        scope_state = _footnote_scope_state(tag)
        if explicit_target and target_id is not None:
            # A local fragment explicitly marks a reference even when its
            # target is stale or ambiguous; normalization will retain it as
            # unresolved instead of treating it as ordinary superscript text.
            return True
        if scope_state[0] and not scope_state[1]:
            return False
        marker_label_candidates = _extract_marker_label_candidates(tag)
        marker_page = _source_page_index(tag)
        return _resolve_label_entries(
            marker_label_entries(marker_label_candidates),
            scope_state,
            marker_page,
        ) is not None

    for marker_index, marker in enumerate(
        soup_main.find_all(is_marker_candidate),
        start=1,
    ):
        original_marker_id = str(marker.get("id") or "").strip()
        marker_label_candidates = _extract_marker_label_candidates(marker)
        display_label = _display_footnote_label if preserve_note_labels else _normalize_footnote_label
        marker_visible_label = display_label(
            marker.get_text("", strip=True)
        )
        marker_page = _source_page_index(marker)
        scope_state = _footnote_scope_state(marker)
        scope_declared, scope_valid, scope = scope_state
        if scope_declared and scope_valid:
            marker[_FOOTNOTE_SCOPE_ATTR] = scope
        symbolic_label = _is_symbolic_footnote_label(_normalize_footnote_label(marker_visible_label))
        explicit_identity, explicit_target_id, explicit_conventional = (
            _explicit_footnote_target(marker)
        )
        entry = None

        if explicit_identity:
            if explicit_target_id is not None:
                entry = _resolve_explicit_target(
                    explicit_target_id,
                    explicit_conventional,
                    entries,
                    scope_state,
                    marker_page,
                )
            # A recognized target identity is authoritative. Never substitute
            # a visible label when that target is absent or ambiguous.
        elif symbolic_label:
            # Symbolic labels are presentation-only unless an explicit target
            # identifies one note. Scope-free legacy symbols may use a single
            # note on their known page. A declared scope must also have a
            # matching label, so scope alone cannot assign an arbitrary note.
            if scope_declared:
                entry = _resolve_label_entries(
                    marker_label_entries(marker_label_candidates),
                    scope_state,
                    marker_page,
                )
            else:
                page_entries = (
                    [
                        candidate
                        for candidate in _scope_compatible_entries(
                            entries, scope_state
                        )
                        if candidate.get("page_index") == marker_page
                    ]
                    if marker_page is not None
                    else []
                )
                entry = _unique_entry(page_entries)
        else:
            entry = _resolve_label_entries(
                marker_label_entries(marker_label_candidates),
                scope_state,
                marker_page,
            )

        visible_label = marker_visible_label or (
            entry["label"]
            if entry
            else (
                marker_label_candidates[0]
                if marker_label_candidates
                else marker.get_text("", strip=True)
            )
        )
        ref_id_seed = (
            f"{marker_index}\n{visible_label}\n{entry['id'] if entry else 'unlinked'}"
        )
        ref_id = f"fnref-{marker_index}-{_safe_footnote_id_part(visible_label)}-{hashlib.sha1(ref_id_seed.encode('utf-8')).hexdigest()[:8]}"
        if original_marker_id and preserve_note_labels:
            reference_ids.setdefault(original_marker_id, []).append(ref_id)

        new_sup = soup_main.new_tag("sup")
        if entry:
            new_a = soup_main.new_tag("a", href=f"#{entry['id']}")
            new_a["id"] = ref_id
            new_a["class"] = "footnote-ref"
            new_a["role"] = "doc-noteref"
            new_a["epub:type"] = "noteref"
            new_a["data-footnote-label"] = visible_label if preserve_note_labels else entry["label"]
            if scope_declared and scope_valid:
                new_a[_FOOTNOTE_SCOPE_ATTR] = scope
            new_a.string = visible_label
            new_sup.append(new_a)
        else:
            new_sup["id"] = ref_id
            new_sup["class"] = ["footnote-unresolved"]
            if scope_declared and scope_valid:
                new_sup[_FOOTNOTE_SCOPE_ATTR] = scope
            new_sup.string = visible_label
        marker.replace_with(new_sup)

    # Known unique marker identities can carry source-authored return links.
    # Repeated legacy IDs cannot authorize a guessed backlink destination.
    for anchor in soup_footnotes.find_all("a", href=True):
        href = str(anchor.get("href") or "")
        destinations = reference_ids.get(unquote(href[1:]), []) if href.startswith("#") else []
        if len(destinations) == 1:
            anchor["href"] = f"#{destinations[0]}"

    # Invalid scope metadata cannot authorize resolution or escape into the
    # semantic document. Valid declared scopes stay on notes and references.
    for tagged in [
        *soup_main.find_all(attrs={_FOOTNOTE_SCOPE_ATTR: True}),
        *soup_footnotes.find_all(attrs={_FOOTNOTE_SCOPE_ATTR: True}),
    ]:
        raw_scope = tagged.get(_FOOTNOTE_SCOPE_ATTR)
        scope_value = raw_scope if isinstance(raw_scope, str) else str(raw_scope)
        if not _FOOTNOTE_SCOPE_PATTERN.fullmatch(scope_value):
            tagged.attrs.pop(_FOOTNOTE_SCOPE_ATTR, None)

    # Source-page provenance exists only to disambiguate the merge.  It is not
    # part of the public semantic HTML vocabulary and must never escape into a
    # stored document, Translator snapshot, or export.
    for tagged in [
        *soup_main.find_all(attrs={_SOURCE_PAGE_ATTR: True}),
        *soup_footnotes.find_all(attrs={_SOURCE_PAGE_ATTR: True}),
    ]:
        tagged.attrs.pop(_SOURCE_PAGE_ATTR, None)


def text_heuristic_match(tag1: Tag, tag2: Tag, force_merge: bool = False) -> bool:
    """Secondary check to see if text flows across a boundary."""
    if force_merge:
        return True

    text1 = tag1.get_text(strip=True)
    text2 = tag2.get_text(strip=True)
    if not text1 or not text2:
        return False

    # If tag1 ends with strong sentence-ending punctuation, it's likely NOT cut
    if text1[-1] in ".!?\"'”":
        return False

    # If tag2 starts with a lowercase letter, VERY strong signal it IS cut
    if text2[0].islower():
        return True

    # Punctuation or an English function word alone cannot establish that the
    # next capitalized block continues this paragraph.
    return False


def _advance_continuation_classes(node1: Tag, node2: Tag) -> None:
    # A forward marker belongs to the consumed physical boundary. Carry it
    # onward only if the newly appended fragment itself continues again.
    forward_classes = {"continues", "continues-next"}
    classes = [c for c in node1.get("class", []) if c not in forward_classes]
    classes.extend(c for c in node2.get("class", []) if c in forward_classes)
    if classes:
        node1["class"] = list(dict.fromkeys(classes))
    else:
        node1.attrs.pop("class", None)


def _boundary_child_list(tag: Tag, *, trailing: bool) -> Tag | None:
    children = reversed(tag.contents) if trailing else iter(tag.contents)
    for child in children:
        if isinstance(child, NavigableString) and not str(child).strip():
            continue
        if isinstance(child, Tag) and "pagebreak" in _tag_classes(child):
            continue
        if isinstance(child, Tag) and child.name in {"ol", "ul"}:
            return child
        return None
    return None


def _has_list_boundary_continuation(previous: Tag, following: Tag) -> bool:
    """Require explicit evidence on the nested list or its boundary items."""

    if has_boundary_continuation(previous, following):
        return True
    previous_items = previous.find_all("li", recursive=False)
    following_items = following.find_all("li", recursive=False)
    return bool(
        previous_items
        and following_items
        and has_boundary_continuation(previous_items[-1], following_items[0])
    )


def _merge_continued_child_list(node1: Tag, node2: Tag) -> None:
    """Merge only explicitly continued direct child lists at a text boundary."""

    previous = _boundary_child_list(node1, trailing=True)
    following = _boundary_child_list(node2, trailing=False)
    if previous is None or following is None:
        return
    if _has_list_boundary_continuation(previous, following):
        # merge_nodes validates list kind, ordered type/start/reversed state,
        # and recursively handles a split nested item. The parent text join
        # still runs below with its original split fragments and separator.
        merge_nodes(previous, following)


def _ordered_list_integer(tag: Tag, attribute: str) -> int | None:
    """Read an HTML list ordinal without changing malformed source attributes."""
    if not tag.has_attr(attribute):
        return None
    try:
        return int(str(tag.get(attribute)).strip(), 10)
    except (TypeError, ValueError):
        return None


def _ordered_list_ordinals(list_tag: Tag, items: list[Tag]) -> list[int]:
    """Return effective ordinals for direct ``li`` children only."""
    reversed_list = list_tag.has_attr("reversed")
    start = _ordered_list_integer(list_tag, "start")
    if start is None:
        start = len(items) if reversed_list else 1

    step = -1 if reversed_list else 1
    ordinals = []
    current = start
    for item in items:
        value = _ordered_list_integer(item, "value")
        if value is not None:
            current = value
        ordinals.append(current)
        current += step
    return ordinals


def _prepare_ordered_list_merge(node1: Tag, node2: Tag) -> None:
    """Validate list styles and preserve ordinals before dropping ``node2``."""
    if node1.name != node2.name:
        raise ValueError("list-continuation-style-conflict")

    if node1.name == "ul":
        return

    allowed_types = {"1", "a", "A", "i", "I"}
    first_type = str(node1.get("type") or "1")
    incoming_type = node2.get("type")
    if incoming_type is not None:
        normalized_first_type = first_type if first_type in allowed_types else "1"
        normalized_incoming_type = str(incoming_type)
        if normalized_incoming_type not in allowed_types:
            normalized_incoming_type = "1"
        if normalized_incoming_type != normalized_first_type:
            raise ValueError("list-continuation-style-conflict")

    first_reversed = node1.has_attr("reversed")
    incoming_reversed = node2.has_attr("reversed")
    if first_reversed != incoming_reversed:
        raise ValueError("list-continuation-style-conflict")

    items1 = node1.find_all("li", recursive=False)
    items2 = node2.find_all("li", recursive=False)
    incoming_start = _ordered_list_integer(node2, "start")
    if node2.has_attr("start") and incoming_start is None:
        raise ValueError("list-continuation-ordinal-conflict")
    incoming_ordinals = _ordered_list_ordinals(node2, items2)

    if (
        items1
        and items2
        and has_boundary_continuation(items1[-1], items2[0])
        and (
            incoming_start is not None
            or _ordered_list_integer(items2[0], "value") is not None
        )
    ):
        previous_ordinal = _ordered_list_ordinals(node1, items1)[-1]
        if incoming_ordinals[0] != previous_ordinal:
            raise ValueError("list-continuation-ordinal-conflict")

    if incoming_start is not None and any(
        item.has_attr("value")
        and _ordered_list_integer(item, "value") is None
        for item in items2
    ):
        raise ValueError("list-continuation-ordinal-conflict")

    # A reversed list without a valid start derives its first ordinal from its
    # direct item count. Freeze that value before appending more direct items.
    if first_reversed and _ordered_list_integer(node1, "start") is None:
        node1["start"] = str(len(items1))

    # A removed explicit start would otherwise be lost with the second
    # wrapper. An implicit incoming list continues from node1 as historically.
    # Preserve the explicit list's effective ordinals, including resets from
    # existing li[value].
    if incoming_start is not None:
        for item, ordinal in zip(items2, incoming_ordinals):
            if not item.has_attr("value"):
                item["value"] = str(ordinal)


def merge_nodes(node1: Tag, node2: Tag, force_text_merge: bool = False) -> None:
    """
    Recursively merges node2 into node1 based on structural hierarchy.
    Lists and tables are merged structurally; blockquotes require continuation evidence.
    Text blocks (p, li, td) are merged based on heuristics or force_text_merge.
    """
    if node1.name in {"ul", "ol"} and node2.name in {"ul", "ol"}:
        _prepare_ordered_list_merge(node1, node2)

    _advance_continuation_classes(node1, node2)

    # 1. TABLE STRUCTURE (Unconditional Container Merge)
    if node1.name == "table":
        merge_continued_table(
            node1,
            node2,
            row_continues=has_boundary_continuation,
            merge_row=merge_nodes,
        )
        return

    # 2. LIST STRUCTURE (Unconditional Container Merge)
    elif node1.name in ["ul", "ol"]:
        items1 = node1.find_all("li", recursive=False)
        items2 = node2.find_all("li", recursive=False)

        if items1 and items2:
            last_li1 = items1[-1]
            first_li2 = items2[0]
            if has_boundary_continuation(last_li1, first_li2):
                merge_nodes(last_li1, first_li2)
                items2 = items2[1:]

        for item in items2:
            node1.append(item)
        node2.decompose()
        return

    # 3. TABLE ROW (Unconditional Container Merge)
    elif node1.name == "tr":
        cells1 = node1.find_all(["td", "th"], recursive=False)
        cells2 = node2.find_all(["td", "th"], recursive=False)

        if cells1 and cells2:
            last_c1 = cells1[-1]
            first_c2 = cells2[0]
            if has_boundary_continuation(last_c1, first_c2):
                merge_nodes(last_c1, first_c2)
                cells2 = cells2[1:]

        for cell in cells2:
            node1.append(cell)
        node2.decompose()
        return

    # 4. BLOCK CONTAINERS (blockquote, div, article, section)
    elif node1.name in ["blockquote", "div", "article", "section"]:
        children1 = [c for c in node1.contents if isinstance(c, Tag)]
        children2 = [c for c in node2.contents if isinstance(c, Tag)]

        if children1 and children2:
            last_c1 = children1[-1]
            first_c2 = children2[0]

            if last_c1.name == first_c2.name:
                # A container-level merge never authorizes merging its children.
                # The extraction contract marks a split child separately.
                if has_boundary_continuation(last_c1, first_c2):
                    merge_nodes(last_c1, first_c2)
                    first_c2.extract()

        for child in list(node2.contents):
            node1.append(child)
        node2.decompose()
        return

    # 5. PURE TEXT LEVEL MERGE (p, span, h1-h6, or text-only li/td)
    html1 = "".join(str(c) for c in node1.contents).rstrip()
    html2 = "".join(str(c) for c in node2.contents).lstrip()

    sep = " "
    text2 = node2.get_text(strip=True)
    if text2 and text2[0] in (",", ".", ";", ":", ")", "]", "’", "'", "”"):
        sep = ""

    text1 = node1.get_text(strip=False).rstrip()
    _merge_continued_child_list(node1, node2)
    html1 = "".join(str(c) for c in node1.contents).rstrip()
    html2 = "".join(str(c) for c in node2.contents).lstrip()
    if text1 and text1.endswith("-"):
        sep = ""
        if html1.endswith("-"):
            html1 = html1[:-1]
        else:
            html1 = re.sub(r"-\s*(</[^>]+>)$", r"\1", html1)

    node1.clear()
    node1.append(BeautifulSoup(html1 + sep + html2, "html.parser"))
    node2.decompose()


def merge_paragraph_into_blockquote(
    blockquote: Tag, paragraph: Tag, force_text_merge: bool = False
):
    if not isinstance(blockquote, Tag) or not isinstance(paragraph, Tag):
        return
    if blockquote.name != "blockquote" or paragraph.name != "p":
        return

    _advance_continuation_classes(blockquote, paragraph)
    quote_children = [child for child in blockquote.contents if isinstance(child, Tag)]
    last_quote_paragraph = (
        quote_children[-1]
        if quote_children and quote_children[-1].name == "p"
        else None
    )
    if last_quote_paragraph and (force_text_merge or has_continuation_class(paragraph)):
        merge_nodes(last_quote_paragraph, paragraph, True)
        return

    blockquote.append(paragraph.extract())


def _move_pagebreak_into_boundary(soup: BeautifulSoup, boundary: Tag) -> None:
    """Keep a physical page marker inside content that crosses the boundary."""

    pagebreak = soup.find(
        "span",
        class_="pagebreak",
        recursive=False,
    )
    if pagebreak is None:
        return
    boundary_target = boundary
    while not any(
        isinstance(child, NavigableString) and child.strip()
        for child in boundary_target.contents
    ):
        first_child = next(
            (
                child
                for child in boundary_target.contents
                if isinstance(child, Tag)
            ),
            None,
        )
        if first_child is None:
            break
        boundary_target = first_child
    boundary_target.insert(0, pagebreak.extract())


def concatenate_html_pages(
    html_parts: list[str],
    boundary_flags: dict | None = None,
    toc_handling: str = "omit",
    page_numbers: Sequence[int] | None = None,
    *,
    preserve_note_labels: bool = True,
) -> str:
    """
    boundary_flags maps a zero-based previous page index to section decisions:
    {0: {"main_content": "merge", "footnotes": "separate"}}.
    Legacy bool values are still accepted, where True means force merge.
    """
    if boundary_flags is None:
        boundary_flags = {}

    if page_numbers is None:
        page_indexes = list(range(len(html_parts)))
    else:
        if (
            len(page_numbers) != len(html_parts)
            or any(type(number) is not int or number <= 0 for number in page_numbers)
            or any(left >= right for left, right in pairwise(page_numbers))
        ):
            raise ValueError("invalid-physical-page-numbers")
        page_indexes = [number - 1 for number in page_numbers]

    main_content_parts = []
    footnotes_parts = []

    for page_index, html in zip(page_indexes, html_parts):
        if not html or html.strip().lower() == "blank":
            continue
        soup = BeautifulSoup(html, "html.parser")

        main_tag = soup.find("main_content")
        if main_tag:
            explicit_markers = main_tag.find_all(
                "span", class_=["inline-footnote", "footnote_marker"]
            )
            for marker in [*explicit_markers, *main_tag.find_all("sup")]:
                marker[_SOURCE_PAGE_ATTR] = str(page_index)
                _capture_inherited_footnote_scope(marker)
        main_content_parts.append(
            (page_index, main_tag.decode_contents() if main_tag else html)
        )

        footnotes_tag = soup.find("footnotes")
        if footnotes_tag:
            for footnote in footnotes_tag.find_all(class_="footnote"):
                footnote[_SOURCE_PAGE_ATTR] = str(page_index)
                _capture_inherited_footnote_scope(footnote)
            footnotes_parts.append((page_index, footnotes_tag.decode_contents()))

    def merge_parts(parts, section_name):
        combined = []

        def merge_boundary(
            merge: Callable[..., None], *args: Any, **kwargs: Any
        ) -> None:
            try:
                merge(*args, **kwargs)
            except ValueError as exc:
                reason_code = str(exc)
                if reason_code not in {
                    "list-continuation-style-conflict",
                    "list-continuation-ordinal-conflict",
                }:
                    raise
                raise ListContinuationError(
                    reason_code,
                    previous_page=prev_page_index + 1,
                    page_number=page_index + 1,
                    section=section_name,
                ) from None

        for page_index, part in parts:
            soup = BeautifulSoup(part, "html.parser")
            has_page_content = bool(
                soup.get_text(strip=True) or soup.find("img") or soup.find("table")
            )
            if section_name == "main_content" and has_page_content:
                pagebreak = soup.new_tag("span")
                pagebreak["id"] = f"doc-page-{page_index + 1}"
                pagebreak["class"] = "pagebreak"
                pagebreak["role"] = "doc-pagebreak"
                pagebreak["epub:type"] = "pagebreak"
                pagebreak["title"] = str(page_index + 1)
                # The role supplies page-break semantics; keep the label
                # language-neutral instead of inventing English source text.
                pagebreak["aria-label"] = str(page_index + 1)
                soup.insert(0, pagebreak)

            if not combined:
                if has_page_content:
                    combined.append((page_index, str(soup)))
                continue

            prev_page_index, prev_html = combined[-1]
            prev_soup = BeautifulSoup(prev_html, "html.parser")
            top_tags_1 = [
                t
                for t in prev_soup.contents
                if isinstance(t, Tag) and "pagebreak" not in _tag_classes(t)
            ]
            top_tags_2 = [
                t
                for t in soup.contents
                if isinstance(t, Tag) and "pagebreak" not in _tag_classes(t)
            ]
            merged_boundary = False

            if page_index == prev_page_index + 1 and top_tags_1 and top_tags_2:
                last_t1 = top_tags_1[-1]
                first_t2 = top_tags_2[0]

                decision = get_boundary_decision(
                    boundary_flags, prev_page_index, section_name
                )
                has_class_signal = has_boundary_continuation(last_t1, first_t2)
                force_text_merge = decision == "merge"

                # Explicit correction wins over extraction hints. Outside that
                # opt-in pass, only unambiguous semantic structure vetoes a hint;
                # sentence-ending punctuation does not end a paragraph reliably.
                # A requested merge still cannot cross a footnote numbering scope.
                can_merge = (
                    _same_valid_footnote_scope(last_t1, first_t2)
                    and decision != "separate"
                    and not (
                        is_structural_boundary_block(last_t1)
                        or is_structural_boundary_block(first_t2)
                    )
                )

                source_toc_pair = _is_source_toc(last_t1) and _is_source_toc(
                    first_t2
                )
                if (
                    source_toc_pair
                    and can_merge
                    and (has_class_signal or force_text_merge)
                    and _source_toc_pair_can_merge(last_t1, first_t2)
                ):
                    if section_name == "main_content":
                        _move_pagebreak_into_boundary(
                            soup, _source_toc_boundary_target(first_t2)
                        )
                    _advance_continuation_classes(last_t1, first_t2)
                    _merge_source_toc(last_t1, first_t2)
                    merged_boundary = True

                if (
                    can_merge
                    and not source_toc_pair
                    and (
                        last_t1.name == "blockquote"
                        and first_t2.name == "p"
                        and (force_text_merge or has_class_signal)
                    )
                ):
                    if section_name == "main_content":
                        _move_pagebreak_into_boundary(soup, first_t2)
                    merge_boundary(
                        merge_paragraph_into_blockquote, last_t1, first_t2, True
                    )
                    merged_boundary = True

                # Containers only merge with explicit continuation evidence.
                blockquote_pair = (
                    last_t1.name == "blockquote" and first_t2.name == "blockquote"
                )
                is_container = is_merge_container(last_t1)
                should_merge = (
                    has_class_signal
                    or force_text_merge
                    or (
                        not blockquote_pair
                        and not is_container
                        and text_heuristic_match(last_t1, first_t2, force_text_merge)
                    )
                )
                if (
                    can_merge
                    and not source_toc_pair
                    and last_t1.name == first_t2.name
                    and should_merge
                ):
                    if section_name == "main_content":
                        _move_pagebreak_into_boundary(soup, first_t2)
                    merge_boundary(merge_nodes, last_t1, first_t2, force_text_merge)
                    merged_boundary = True

            combined[-1] = (prev_page_index, str(prev_soup))
            has_remaining_content = bool(
                soup.get_text(strip=True)
                or soup.find("img")
                or soup.find("table")
                or soup.find(class_="pagebreak")
            )
            if has_page_content and has_remaining_content:
                combined.append((page_index, str(soup)))
            elif has_page_content and merged_boundary:
                # A fully merged page still advances the physical tail.  The
                # next page must compare against this page, not the page where
                # the now-spanning block originally began.
                combined[-1] = (page_index, str(prev_soup))

        return [html for _, html in combined]

    final_main = merge_parts(main_content_parts, "main_content")
    final_footnotes = merge_parts(footnotes_parts, "footnotes")

    full_footnotes_html = "".join(final_footnotes)
    soup_footnotes = BeautifulSoup(full_footnotes_html, "html.parser")

    full_main_html = "".join(final_main)
    soup_main = BeautifulSoup(full_main_html, "html.parser")
    if str(toc_handling or "omit").strip().lower() == "preserve":
        normalize_toc_lists(soup_main)
    else:
        remove_source_toc_blocks(soup_main)
    normalize_footnote_links(soup_main, soup_footnotes, preserve_note_labels=preserve_note_labels)

    footnote_items = list(soup_footnotes.find_all(class_="footnote"))
    footnotes_html = ""
    if footnote_items:
        notes_list = soup_footnotes.new_tag("ol", **{"class": "endnotes-list"})
        for footnote in footnote_items:
            notes_list.append(footnote.extract())
        footnotes_html = str(notes_list)
    # The source may be Polish, Arabic, multilingual, or may not display an
    # endnote heading at all.  Do not fabricate an English "Notes" heading;
    # the semantic section role is sufficient for exporters and assistive
    # technology, while any visually present source heading remains in the
    # extracted content.
    footnotes_section = (
        f'<section id="footnotes" role="doc-endnotes" epub:type="endnotes">\n{footnotes_html}\n</section>'
        if footnotes_html
        else ""
    )

    return f"<main>\n{''.join(str(soup_main))}\n{footnotes_section}\n</main>"


def update_headings(
    html_content: str, current_headings: Dict[str, Optional[str]]
) -> Dict[str, Optional[str]]:
    """Update current headings state based on the content."""
    soup = BeautifulSoup(html_content, "html.parser")
    headings = soup.find_all(["h1", "h2", "h3", "h4"])

    for heading in headings:
        text = heading.get_text().strip()
        if heading.name == "h1":
            current_headings["h1"] = text
            current_headings["h2"] = None
            current_headings["h3"] = None
            current_headings["h4"] = None
        elif heading.name == "h2":
            current_headings["h2"] = text
            current_headings["h3"] = None
            current_headings["h4"] = None
        elif heading.name == "h3":
            current_headings["h3"] = text
            current_headings["h4"] = None
        elif heading.name == "h4":
            current_headings["h4"] = text

    return current_headings


def extract_contributors(html_content: str) -> Dict[str, Optional[str]]:
    """Extracts authors, editors, and translators to track context."""
    soup = BeautifulSoup(html_content, "html.parser")
    contributors = {}

    for role in ["author", "editor", "translator"]:
        tags = soup.find_all(class_=role)
        if tags:
            contributors[role] = ", ".join(
                tag.get_text().strip() for tag in tags if tag.get_text().strip()
            )

    return contributors


def convert_html_to_clean_markdown(html_content: str) -> str:
    """Converts rich HTML into clean Markdown for AI tools and RAG while preserving source HTML elsewhere."""
    if not html_content:
        return ""

    from markdownify import markdownify as html_to_markdown

    soup = BeautifulSoup(html_content, "html.parser")

    for figcaption in soup.find_all("figcaption"):
        replacement = soup.new_tag("p")
        emphasis = soup.new_tag("em")
        emphasis.append(BeautifulSoup(figcaption.decode_contents(), "html.parser"))
        replacement.append(emphasis)
        figcaption.replace_with(replacement)

    for anchor in soup.find_all("a", class_="footnote-ref"):
        anchor.replace_with(anchor.get_text(strip=True))

    for sup in soup.find_all("sup"):
        if sup.find("a"):
            sup.string = sup.get_text(strip=True)

    for footnote_marker in soup.find_all(
        "span", class_=["inline-footnote", "footnote_marker"]
    ):
        marker_text = footnote_marker.get_text(strip=True)
        replacement = soup.new_tag("sup")
        replacement.string = marker_text
        footnote_marker.replace_with(replacement)

    clean_html = str(soup)
    markdown_text = html_to_markdown(
        clean_html,
        heading_style="ATX",
        strip=[
            "div",
            "main",
            "section",
            "article",
            "nav",
            "aside",
            "span",
            "main_content",
            "footnotes",
        ],
    )
    return markdown_text.strip()
