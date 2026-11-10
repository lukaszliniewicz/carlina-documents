"""Create an explicitly paired, conservative bilingual semantic document.

The input source must be the verified immutable plan identity and the target must
already have passed persisted-target verification. This module never loads an
unversioned source, guesses sentence alignments, or changes existing revisions.
"""

from __future__ import annotations

from copy import deepcopy

from bs4 import BeautifulSoup, Tag
from bs4.element import NavigableString

from carlina.core.semantic_export import validate_semantic_export_html
from carlina.core.semantic_html import SAFE_LANGUAGE_RE


class BilingualExportError(ValueError):
    """An alignment or document-shape rejection with no source text in the error."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


BILINGUAL_LAYOUTS = frozenset({"none", "parallel", "alternating"})
_LEAF = frozenset(
    {
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "p",
        "li",
        "dt",
        "dd",
        "td",
        "th",
        "caption",
        "figcaption",
        "blockquote",
        "pre",
        "address",
    }
)
_INLINE_ONLY = frozenset({"caption", "dt"})
_INSIDE_WRAPPER = frozenset({"li", "td", "th", "dd", "dt", "caption"})
_RTL_LANGUAGES = frozenset({"ar", "fa", "he", "ps", "ur", "yi"})


def validate_bilingual_layout(value: object, export_format: str) -> str:
    """Admit only one supported per-format layout, with no silent fallback."""

    if not isinstance(value, str) or value not in BILINGUAL_LAYOUTS:
        raise BilingualExportError("bilingual-layout-invalid")
    if (value == "parallel" and export_format == "epub") or (
        value == "alternating" and export_format != "epub"
    ):
        raise BilingualExportError("bilingual-layout-format")
    return value


def _root(soup: BeautifulSoup) -> Tag:
    # Source snapshots may be a semantic <main> fragment, whereas targets may
    # already contain an HTML root. Prefer the same visible subtree in both.
    root = soup.find("main") or soup.find("body") or soup.find(True)
    if not isinstance(root, Tag):
        raise BilingualExportError("bilingual-document-root")
    return root


def _tag_children(node: Tag) -> list[Tag]:
    return [child for child in node.children if isinstance(child, Tag)]


def _has_unwrapped_text(node: Tag) -> bool:
    return any(
        isinstance(child, NavigableString) and str(child).strip()
        for child in node.children
    )


def _classes(node: Tag) -> list[str]:
    value = node.get("class")
    if isinstance(value, list):
        return [str(item) for item in value]
    return str(value or "").split()


def _source_copy(node: Tag) -> Tag:
    """Make the source visual-only, preserving its wording and inline formatting.

    Target anchors/notes remain the publication authorities. Copies must not
    participate as additional headings, backlinks, note markers or ARIA labels.
    """
    copied = deepcopy(node)
    for element in [copied, *copied.find_all(True)]:
        was_noteref = element.name == "a" and (
            element.get("role") == "doc-noteref"
            or "noteref" in str(element.get("epub:type") or "").split()
            or "footnote-ref" in _classes(element)
        )
        for attr in (
            "id",
            "name",
            "role",
            "epub:type",
            "aria-labelledby",
            "aria-describedby",
            "headers",
            "data-footnote-label",
            "data-footnote-scope",
        ):
            element.attrs.pop(attr, None)
        if was_noteref:
            element.name = "span"
            element.attrs.pop("href", None)
        if element.name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            element.name = "p"
            element["class"] = " ".join(
                [*_classes(element), "carlina-bilingual-source-heading"]
            )
    return copied


def _pane(soup: BeautifulSoup, language: str, side: str, *, inline: bool) -> Tag:
    pane = soup.new_tag("span" if inline else "div")
    pane["class"] = f"carlina-bilingual-{side}"
    pane["lang"] = language
    pane["xml:lang"] = language
    if language.split("-")[0].lower() in _RTL_LANGUAGES:
        pane["dir"] = "rtl"
    return pane


def _pair(
    soup: BeautifulSoup,
    source: Tag,
    target: Tag,
    *,
    layout: str,
    source_language: str,
    target_language: str,
) -> None:
    if source.name != target.name:
        raise BilingualExportError("bilingual-structure-drift")
    inline = target.name in _INLINE_ONLY
    original = _source_copy(source)
    source_pane = _pane(soup, source_language, "source", inline=inline)
    target_pane = _pane(soup, target_language, "target", inline=inline)
    if target.name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
        # EPUB chapter segmentation and PDF/DOCX heading semantics depend on
        # the target heading staying in its original position and ancestry.
        source_pane.append(original)
        target.insert_after(source_pane)
        return
    if target.name in _INSIDE_WRAPPER:
        # Never create a nested table: pandoc drops or flattens those cells.
        while original.contents:
            source_pane.append(original.contents[0].extract())
        while target.contents:
            target_pane.append(target.contents[0].extract())
        destination = target
    else:
        source_pane.append(original)
        marker = soup.new_tag("div", attrs={"class": "carlina-bilingual-placeholder"})
        target.replace_with(marker)
        target_pane.append(target)
        destination = marker

    if inline:
        wrapper = soup.new_tag("span", attrs={"class": "carlina-bilingual-inline"})
        wrapper.append(source_pane)
        wrapper.append(NavigableString(" / "))
        wrapper.append(target_pane)
    elif target.name in _INSIDE_WRAPPER or layout == "alternating":
        wrapper = soup.new_tag("div", attrs={"class": "carlina-bilingual-alternating"})
        wrapper.append(source_pane)
        wrapper.append(target_pane)
    else:
        wrapper = soup.new_tag("table", attrs={"class": "carlina-bilingual-pair"})
        tbody = soup.new_tag("tbody")
        tr = soup.new_tag("tr")
        for pane in (source_pane, target_pane):
            td = soup.new_tag("td")
            td.append(pane)
            tr.append(td)
        tbody.append(tr)
        wrapper.append(tbody)
    if destination is target:
        target.append(wrapper)
    else:
        destination.replace_with(wrapper)


def compose_bilingual_html(
    original_html: str,
    translated_html: str,
    *,
    layout: str,
    source_language: str,
    target_language: str,
) -> str:
    """Pair corresponding source and target semantic units, rejecting drift.

    Block and inline markup is preserved inside each pane. Existing target
    navigation, figures and notes remain authoritative. Original IDs are
    removed to prevent collisions, while fragment links resolve to the target.
    """

    if layout not in {"parallel", "alternating"}:
        raise BilingualExportError("bilingual-layout-invalid")
    if not SAFE_LANGUAGE_RE.fullmatch(
        source_language
    ) or not SAFE_LANGUAGE_RE.fullmatch(target_language):
        raise BilingualExportError("bilingual-language-invalid")
    validate_semantic_export_html(original_html)
    validate_semantic_export_html(translated_html)
    source_soup = BeautifulSoup(original_html, "html.parser")
    target_soup = BeautifulSoup(translated_html, "html.parser")
    # When both documents have a body, pair its entire authored content,
    # including notes outside <main>. A fragment-based source instead pairs
    # its <main> with the corresponding target subtree.
    if isinstance(source_soup.body, Tag) and isinstance(target_soup.body, Tag):
        source_root, target_root = source_soup.body, target_soup.body
    else:
        source_root, target_root = _root(source_soup), _root(target_soup)
    if source_root.name != target_root.name:
        raise BilingualExportError("bilingual-document-root")

    pairs = 0

    def visit(source: Tag, target: Tag) -> None:
        nonlocal pairs
        if source.name != target.name:
            raise BilingualExportError("bilingual-structure-drift")
        if source.name in _LEAF:
            _pair(
                target_soup,
                source,
                target,
                layout=layout,
                source_language=source_language,
                target_language=target_language,
            )
            pairs += 1
            return
        source_children = _tag_children(source)
        target_children = _tag_children(target)
        # The persisted renderer may append reader/translator endnotes to the
        # target outside the immutable original document's visible main scope.
        if len(target_children) > len(source_children):
            extras = target_children[len(source_children) :]
            if not all(
                child.get("role") == "doc-endnotes" or "footnotes" in _classes(child)
                for child in extras
            ):
                raise BilingualExportError("bilingual-structure-drift")
            target_children = target_children[: len(source_children)]
        if len(source_children) != len(target_children):
            raise BilingualExportError("bilingual-structure-drift")
        if _has_unwrapped_text(source) or _has_unwrapped_text(target):
            if source_children:
                raise BilingualExportError("bilingual-unwrapped-text")
            if bool(str(source.get_text()).strip()) != bool(
                str(target.get_text()).strip()
            ):
                raise BilingualExportError("bilingual-unpaired-text")
            if str(source.get_text()).strip():
                _pair(
                    target_soup,
                    source,
                    target,
                    layout=layout,
                    source_language=source_language,
                    target_language=target_language,
                )
                pairs += 1
                return
        for before, after in zip(source_children, target_children, strict=True):
            visit(before, after)

    visit(source_root, target_root)
    if not pairs:
        raise BilingualExportError("bilingual-no-pairs")
    identifiers = [
        str(element["id"])
        for element in target_soup.find_all(True)
        if element.has_attr("id")
    ]
    if len(identifiers) != len(set(identifiers)):
        raise BilingualExportError("bilingual-duplicate-id")
    ids = set(identifiers)
    for element in target_soup.find_all(True):
        for name in ("aria-labelledby", "aria-describedby", "headers"):
            reference = element.get(name)
            if isinstance(reference, str):
                refs = reference.split()
            elif isinstance(reference, list):
                refs = [str(value) for value in reference]
            else:
                refs = []
            if any(ref not in ids for ref in refs):
                raise BilingualExportError("bilingual-missing-reference")
    for anchor in target_soup.find_all("a", href=True):
        href = anchor["href"]
        if isinstance(href, str) and href.startswith("#") and href[1:] not in ids:
            raise BilingualExportError("bilingual-missing-reference")
    output = target_soup.decode(formatter="html")
    validate_semantic_export_html(output)
    return output


__all__ = [
    "BILINGUAL_LAYOUTS",
    "BilingualExportError",
    "compose_bilingual_html",
    "validate_bilingual_layout",
]
