from __future__ import annotations

import copy
import re
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import unquote, urlparse

from bs4 import BeautifulSoup, NavigableString, Tag
from carlina.core.diagram_rendering import project_diagrams_html

from iarf_assistant.core.upload_paths import resolve_upload_reference

from .semantic_document import (
    canonicalize_document_html,
    is_current_semantic_document,
    is_doc_endnote,
    is_doc_noteref,
    is_legacy_note_id,
    semantic_result_for_stored_html,
    validate_semantic_export,
)

MAX_DOCX_IMAGE_BYTES = 25 * 1024 * 1024
SAFE_DOCX_IMAGE_SUFFIXES = {
    ".bmp",
    ".gif",
    ".jpeg",
    ".jpg",
    ".png",
    ".tif",
    ".tiff",
    ".webp",
}


def _attribute_tokens(value: object) -> set[str]:
    if isinstance(value, (list, tuple, set)):
        return {token.casefold() for item in value for token in str(item).split()}
    return set(str(value or "").casefold().split())


def _is_semantic_note_backlink(tag: Tag) -> bool:
    """Match only note backlinks, not ordinary internal links in note bodies."""
    if tag.name != "a":
        return False
    return bool(
        "doc-backlink" in _attribute_tokens(tag.get("role"))
        or "backlink" in _attribute_tokens(tag.get("epub:type"))
        or _attribute_tokens(tag.get("class")) & {"footnote-backlink", "footnote-back"}
    )


_DOCX_NOTE_MARKER_CLASSES = {"footnote-marker", "doc-footnote-marker-item"}
_DOCX_NOTE_HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
_DOCX_NOTE_FORMATTING_TAGS = {
    "b",
    "br",
    "code",
    "del",
    "em",
    "i",
    "ins",
    "mark",
    "s",
    "small",
    "span",
    "strong",
    "sub",
    "sup",
    "u",
    "wbr",
}
_DOCX_NOTE_RETURN_LINK_FORMATTING_TAGS = _DOCX_NOTE_FORMATTING_TAGS - {"br", "wbr"}
_DOCX_NOTE_NON_PROSE_TAGS = {
    "audio",
    "canvas",
    "embed",
    "figure",
    "hr",
    "iframe",
    "img",
    "math",
    "object",
    "svg",
    "table",
    "video",
}
_DOCX_NOTE_RETURN_GLYPHS = {"↩", "↩\ufe0e", "↩\ufe0f"}


def _is_docx_note_return_link(tag: Tag, reference_ids: Sequence[str]) -> bool:
    """Match semantic backlinks or a plain arrow to this note's own marker."""
    if _is_semantic_note_backlink(tag):
        return True
    if tag.name != "a" or not reference_ids:
        return False

    href = tag.get("href")
    if not isinstance(href, str) or not href.startswith("#"):
        return False
    target_id = unquote(urlparse(href).fragment)
    if target_id not in reference_ids:
        return False
    if tag.get_text(strip=True) not in _DOCX_NOTE_RETURN_GLYPHS:
        return False
    return all(
        descendant.name in _DOCX_NOTE_RETURN_LINK_FORMATTING_TAGS
        for descendant in tag.find_all(True)
    )


def _docx_note_marker_token(value: object) -> str | None:
    """Return a bounded marker token with only numeric punctuation normalized."""
    token = re.sub(r"\s+", " ", str(value or "")).strip()
    if not re.fullmatch(r"[0-9A-Za-z*†‡§#]{1,8}(?:[.]?)", token):
        numeric = re.fullmatch(r"(?:\[(\d+)\]|\((\d+)\))\.?", token)
        if numeric:
            return numeric.group(1) or numeric.group(2)
        return None
    if token.endswith(".") and token[:-1].isdigit():
        token = token[:-1]
    return token


def _docx_note_heading_is_presentation_only(heading: Tag) -> bool:
    return all(
        descendant.name in _DOCX_NOTE_FORMATTING_TAGS
        for descendant in heading.find_all(True)
    )


def _docx_note_has_prior_substantive_content(note: Tag, heading: Tag) -> bool:
    for node in note.descendants:
        if node is heading:
            return False
        if isinstance(node, NavigableString):
            if str(node).strip():
                return True
        elif node.name in _DOCX_NOTE_NON_PROSE_TAGS:
            return True
        elif _attribute_tokens(node.get("class")) & {
            "math",
            "display-math",
            "inline-math",
        }:
            return True
    return False


def _remove_duplicate_docx_note_heading(
    fn_copy: Tag, reference_labels: set[str]
) -> None:
    """Drop one marker-only first heading when its target-local label proves it."""
    if not reference_labels:
        return
    heading = fn_copy.find(list(_DOCX_NOTE_HEADING_TAGS))
    if (
        heading is None
        or not _docx_note_heading_is_presentation_only(heading)
        or _docx_note_has_prior_substantive_content(fn_copy, heading)
    ):
        return
    heading_token = _docx_note_marker_token(heading.get_text(" ", strip=True))
    if heading_token in reference_labels and re.fullmatch(
        r"(?:\d+|[*†‡§#]+)", heading_token or ""
    ):
        heading.decompose()


def preprocess_html_for_docx(
    html_content: str,
    doc_file_path: str | None = None,
    uploads_dir: Path | None = None,
    allowed_resource_roots: list[Path] | None = None,
    user_id: int | None = None,
    settings: dict | None = None,
) -> str:
    """
    Preprocesses rich HTML to optimize it for Pandoc conversion to DOCX.
    This guarantees that footnotes, math, headers, and images are formatted
    exactly as Pandoc expects to build native Word objects.
    """
    if not html_content:
        return ""

    if is_current_semantic_document(settings):
        html_content = semantic_result_for_stored_html(html_content, settings).html
    elif user_id is not None:
        html_content = canonicalize_document_html(
            html_content,
            user_id=user_id,
            source_file_path=doc_file_path,
            uploads_dir=uploads_dir,
            relaxed=True,
        ).html
    soup = BeautifulSoup(html_content, "html.parser")

    if str(doc_file_path or "").lower().endswith(".pdf"):
        from .parsers.pdf_utils import remove_source_toc_blocks

        remove_source_toc_blocks(soup)

    # 1. Image path resolution
    # Ensure images use absolute paths so Pandoc can find them during zip packaging.
    source_dir = None
    allowed_roots = []
    if doc_file_path:
        source_path = (
            resolve_upload_reference(doc_file_path, uploads_dir=uploads_dir)
            if uploads_dir
            else None
        )
        source_path = source_path or Path(doc_file_path)
        source_dir = (
            source_path if source_path.is_dir() else source_path.parent
        ).resolve()
        allowed_roots.append(source_dir)
    for root in allowed_resource_roots or []:
        try:
            allowed_roots.append(Path(root).resolve())
        except OSError:
            continue

    for img in soup.find_all("img"):
        src = str(img.get("src") or "").strip()
        if src and not src.lower().startswith("data:"):
            parsed = urlparse(src)
            candidate = None
            if src.startswith("//"):
                candidate = None
            elif parsed.scheme == "file":
                decoded_file_path = unquote(parsed.path or "")
                if re.match(r"^/[A-Za-z]:/", decoded_file_path):
                    decoded_file_path = decoded_file_path[1:]
                candidate = Path(decoded_file_path)
            elif parsed.scheme and not (
                len(parsed.scheme) == 1 and re.match(r"^[A-Za-z]:[\\/]", src)
            ):
                candidate = None
            elif parsed.scheme:
                candidate = Path(src)
            elif source_dir is not None:
                decoded = unquote(parsed.path or src)
                if decoded.startswith("/uploads/") and uploads_dir is not None:
                    candidate = Path(uploads_dir) / decoded.removeprefix("/uploads/")
                elif decoded.startswith("/"):
                    candidate = None
                else:
                    candidate = source_dir / decoded

            try:
                resolved = candidate.resolve() if candidate is not None else None
                if resolved is None or not any(
                    resolved.is_relative_to(root) for root in allowed_roots
                ):
                    raise ValueError
                if (
                    not resolved.is_file()
                    or resolved.suffix.lower() not in SAFE_DOCX_IMAGE_SUFFIXES
                    or resolved.stat().st_size > MAX_DOCX_IMAGE_BYTES
                ):
                    raise ValueError
                img["src"] = str(resolved)
            except OSError, RuntimeError, ValueError:
                img.attrs.pop("src", None)

    # 2. Footnote restructuring
    # We find all footnote references in the text: <a class="footnote-ref"> or <a role="doc-noteref">
    # and map them to their corresponding footnote target.
    # Then we reconstruct them using standard Pandoc HTML structure.

    # Validate before any target is removed.  The same diagnostic is used by
    # EPUB packaging, so the semantic representation has one reference
    # contract across both export paths.
    validate_semantic_export(soup, export_name="DOCX export input")

    refs = soup.find_all(is_doc_noteref)
    if not refs:
        # Fallback to links pointing to targets starting with fn-
        refs = soup.find_all(
            "a",
            href=lambda h: (
                h
                and h.startswith("#fn-")
                and (target := soup.find(id=h[1:])) is not None
                and is_doc_endnote(target)
            ),
        )

    footnote_targets: dict[str, Tag] = {}
    for element in soup.find_all(True):
        fn_id = str(element.get("id") or "").strip()
        if not fn_id or element.name == "a":
            continue
        if is_doc_endnote(element) or is_legacy_note_id(fn_id):
            footnote_targets[fn_id] = element

    # Re-map references and assign sequential numbers
    ref_mappings = {}  # original_target_id -> (num, [unique ref ids])
    reference_labels_by_target: dict[str, set[str]] = {}
    rewritten_reference_nodes = {id(ref) for ref in refs}
    link_target_mappings: dict[str, str] = {}
    counter = 1

    for ref in refs:
        href = ref.get("href")
        if not isinstance(href, str) or not href.startswith("#"):
            continue
        target_id = unquote(href[1:])
        marker_token = _docx_note_marker_token(ref.get_text(" ", strip=True))
        if marker_token is not None:
            reference_labels_by_target.setdefault(target_id, set()).add(marker_token)

        # If this target has not been numbered yet, number it
        if target_id not in ref_mappings:
            ref_mappings[target_id] = (counter, [])
            counter += 1

        num, ref_ids = ref_mappings[target_id]
        ref_id = f"fnref{num}" if not ref_ids else f"fnref{num}-{len(ref_ids) + 1}"
        ref_ids.append(ref_id)
        original_ref_id = str(ref.get("id") or "").strip()
        if original_ref_id:
            link_target_mappings[original_ref_id] = ref_id
        link_target_mappings[target_id] = f"fn{num}"

        # Rewrite the reference link to match Pandoc's expectation
        ref["href"] = f"#fn{num}"
        ref["id"] = ref_id
        ref["role"] = "doc-noteref"
        ref["class"] = "footnote-ref"

        # Pandoc reads the text content inside the reference link
        # We replace the content inside with the clean number wrapped in sup
        ref.clear()
        sup = soup.new_tag("sup")
        sup.string = str(num)
        ref.append(sup)

        # Adjacent source and translator references must read as "1,2", not
        # the single footnote number "12", after Word assigns native numbers.
        previous = ref.previous_sibling
        while isinstance(previous, NavigableString) and not previous.strip():
            previous = previous.previous_sibling
        if isinstance(previous, Tag) and is_doc_noteref(previous):
            separator = soup.new_tag("sup")
            separator.string = ","
            ref.insert_before(separator)

    # Note markers and targets receive Pandoc IDs. Keep ordinary links to
    # those nodes valid as well, including links inside copied note bodies.
    # Already rewritten noterefs must not be mapped twice when source IDs
    # overlap the generated namespace (for example fn2 followed by fn1).
    for link in soup.find_all("a", href=True):
        if id(link) in rewritten_reference_nodes:
            continue
        href = str(link.get("href") or "")
        if href.startswith("#"):
            replacement = link_target_mappings.get(unquote(href[1:]))
            if replacement is not None:
                link["href"] = f"#{replacement}"

    # Now, gather the footnote contents and build a clean Pandoc footnotes section
    ordered_footnotes = []
    processed_targets = set()

    # First, append the ones that were referenced, in order of reference appearance
    for target_id, (num, ref_ids) in ref_mappings.items():
        fn_element = footnote_targets.get(target_id)
        if fn_element:
            processed_targets.add(target_id)
            # Create standard li element
            li = soup.new_tag("li", id=f"fn{num}", role="doc-endnote")

            # Remove only explicit presentation markers. A leading number in
            # the body can be a date, quantity, quotation, or citation.
            fn_copy = copy.deepcopy(fn_element)

            # Remove every explicitly marked presentation node before heading
            # inspection. A leading heading is handled separately below, only
            # when its target-local reference label proves that it is a
            # duplicate marker.
            for marker in fn_copy.find_all(True):
                if (
                    marker.parent is not None
                    and _attribute_tokens(marker.get("class"))
                    & _DOCX_NOTE_MARKER_CLASSES
                ):
                    marker.decompose()
            # Remove semantic backlinks, plus unmarked return arrows aimed at
            # this note's own rewritten references. Ordinary links remain.
            for backlink in fn_copy.find_all("a"):
                if _is_docx_note_return_link(backlink, ref_ids):
                    backlink.decompose()
            _remove_duplicate_docx_note_heading(
                fn_copy, reference_labels_by_target.get(target_id, set())
            )

            # Preserve the source order and all body nodes.  The previous
            # paragraph-only branch could silently drop labels/text that sat
            # beside a paragraph, which is especially damaging for OCR notes.
            for child in list(fn_copy.contents):
                li.append(child)

            # Add backlink to the last paragraph of the footnote
            last_p = li.find_all("p")[-1] if li.find_all("p") else li
            backlink_a = soup.new_tag(
                "a",
                href=f"#{ref_ids[0]}",
                role="doc-backlink",
                **{"class": "footnote-back"},
            )
            backlink_a.string = " ↩"
            last_p.append(backlink_a)

            ordered_footnotes.append(li)

    unlinked_targets = [
        (target_id, copy.deepcopy(target))
        for target_id, target in footnote_targets.items()
        if target_id not in processed_targets
    ]

    # Clean up the original footnote tags from the body so they don't render twice
    for target in footnote_targets.values():
        target.decompose()

    # Remove any empty container sections for footnotes (e.g. #footnotes, #document-footnotes)
    for footnotes_sec in soup.find_all(id=["footnotes", "document-footnotes"]):
        footnotes_sec.decompose()

    # Append the new clean footnotes section at the bottom of the body
    if ordered_footnotes or unlinked_targets:
        section = soup.new_tag("section", role="doc-endnotes", **{"class": "footnotes"})
        hr = soup.new_tag("hr")
        section.append(hr)
        if ordered_footnotes:
            ol = soup.new_tag("ol")
            for li in ordered_footnotes:
                ol.append(li)
            section.append(ol)
        if unlinked_targets:
            unlinked_list = soup.new_tag(
                "ol",
                **{"class": "unlinked-notes", "data-unlinked-notes": "true"},
            )
            for target_id, fn_element in unlinked_targets:
                li = soup.new_tag(
                    "li",
                    id=target_id,
                    **{"class": "unlinked-footnote", "data-unlinked-footnote": "true"},
                )
                fn_copy = fn_element
                fn_copy.attrs.pop("id", None)
                fn_copy.attrs.pop("role", None)
                fn_copy.attrs.pop("epub:type", None)
                if fn_copy.name in {"li", "div", "section"}:
                    for child in list(fn_copy.contents):
                        li.append(child)
                else:
                    li.append(fn_copy)
                unlinked_list.append(li)
            section.append(unlinked_list)
        soup.append(section)

    validate_semantic_export(soup, export_name="DOCX export")

    # 3. Math delimiter sanitation
    # Pandoc reads \( ... \) for inline math and \[ ... \] for display math.
    # Standard output might contain $ ... $ or $$ ... $$.
    for text_node in soup.find_all(string=True):
        if text_node.parent and text_node.parent.name in [
            "script",
            "style",
            "code",
            "pre",
        ]:
            continue
        text = text_node.string
        if text:
            # Replace $$ ... $$ with \[ ... \]
            text = re.sub(r"\$\$(.*?)\$\$", r"\[\1\]", text, flags=re.DOTALL)
            # Replace $ ... $ with \( ... \)
            text = re.sub(r"\$(.*?)\$", r"\(\1\)", text)
            text_node.replace_with(text)

    # Canonical and owned-resource checks above apply to the source. Render the
    # restricted diagram only after the final semantic validation.
    return project_diagrams_html(str(soup))
