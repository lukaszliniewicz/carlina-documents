from __future__ import annotations

import html
import logging
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from bs4 import BeautifulSoup, Comment

from carlina.core.export_notes import normalize_export_note_roles
from carlina.core.semantic_html import (
    MAX_INLINE_IMAGE_CHARS as _MAX_INLINE_IMAGE_CHARS,
)
from carlina.core.semantic_html import (
    SAFE_CONTROL_RE as _SAFE_CONTROL_RE,
)
from carlina.core.semantic_html import (
    SAFE_DATA_IMAGE_RE as _SAFE_DATA_IMAGE_RE,
)
from carlina.core.semantic_html import (
    SAFE_IDENTIFIER_RE as _SAFE_IDENTIFIER_RE,
)
from carlina.core.semantic_html import (
    SAFE_INTEGER_RE as _SAFE_INT_RE,
)
from carlina.core.semantic_html import (
    SAFE_LANGUAGE_RE as _SAFE_LANG_RE,
)
from carlina.core.semantic_html import (
    SAFE_LINK_SCHEMES as _SAFE_LINK_SCHEMES,
)
from carlina.core.semantic_html import (
    SAFE_TOKEN_RE as _SAFE_TOKEN_RE,
)
from carlina.core.semantic_html import (
    SEMANTIC_DOCUMENT_TAGS as _DOCUMENT_TAGS,
)
from carlina.core.semantic_html import (
    SEMANTIC_DROP_CONTENT_TAGS as _DROP_CONTENT_TAGS,
)
from carlina.core.semantic_html import (
    SEMANTIC_GLOBAL_ATTRIBUTES as _GLOBAL_ATTRIBUTES,
)
from carlina.core.semantic_html import (
    SEMANTIC_TAG_ATTRIBUTES as _TAG_ATTRIBUTES,
)
from iarf_assistant.core.upload_paths import uploads_root

from .block_notes import render_note_body
from .models import TranslationProject, TranslationSegment

logger = logging.getLogger(__name__)

_TRANSPARENT_PIXEL = (
    "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"
)

# This is deliberately a document allowlist rather than the much smaller
# generic assistant-message allowlist.  Translator output needs to retain
# tables, figures, lists, and ordinary semantic markup, but it is still
# untrusted input (both source documents and model responses).
_PLACEHOLDER_RE = re.compile(r"^\{\{[A-Za-z0-9_-]{1,80}\}\}$")
_PLACEHOLDER_TOKEN_RE = re.compile(r"\{\{[^{}]{1,80}\}\}")
_NOTE_MARKUP_RE = re.compile(r"<[^>]*>")
_NOTE_LINK_RE = re.compile(r"\[[^\]\r\n]{1,800}\]\([^\)\r\n]{1,2000}\)")
_NOTE_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _translated_segment_text(segment: TranslationSegment) -> str:
    value = (
        segment.translated_text
        if segment.translated_text and segment.translated_text.strip()
        else segment.original_text
    )
    return str(value or "")


def _attribute_value(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return " ".join(str(item) for item in value)
    return str(value or "")


def _safe_scalar(value: Any, *, maximum: int = 512) -> str | None:
    candidate = _attribute_value(value).strip()
    if not candidate or len(candidate) > maximum or _SAFE_CONTROL_RE.search(candidate):
        return None
    return candidate


def _safe_uri(value: Any, *, preserve_placeholder: bool = False) -> str | None:
    candidate = _safe_scalar(value, maximum=4096)
    if candidate is None:
        return None
    if preserve_placeholder and _PLACEHOLDER_RE.fullmatch(candidate):
        return candidate
    # Backslashes are interpreted as URL separators by some browsers.  Treat
    # them as invalid instead of trying to normalize browser-specific syntax.
    if "\\" in candidate or candidate.startswith("//"):
        return None
    parsed = urlsplit(candidate)
    scheme = parsed.scheme.lower()
    if scheme and scheme not in _SAFE_LINK_SCHEMES:
        return None
    return candidate


def _owner_upload_file(
    source: str,
    *,
    project: TranslationProject,
    uploads_dir: str | Path | None,
) -> Path | None:
    parsed = urlsplit(source)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        return None
    path = parsed.path
    # Do not leave percent-encoded separators/traversal for a web server to
    # decode after this boundary has approved the owner prefix.
    if "%" in path:
        return None
    owner_id = str(getattr(project, "user_id", "") or "").strip()
    prefix = f"/uploads/{owner_id}/"
    if not owner_id or not path.startswith(prefix):
        return None

    base_uploads_dir = uploads_root(uploads_dir=uploads_dir).resolve()
    owner_root = (base_uploads_dir / owner_id).resolve()
    relative_parts = path.removeprefix("/uploads/").split("/")
    candidate = (base_uploads_dir.joinpath(*relative_parts)).resolve()
    try:
        candidate.relative_to(owner_root)
    except ValueError:
        return None
    return candidate


def _safe_image_source(
    value: Any,
    *,
    project: TranslationProject,
    for_export: bool,
    uploads_dir: str | Path | None,
    preserve_placeholder: bool = False,
) -> str | None:
    candidate = _safe_scalar(value, maximum=2_000_000)
    if candidate is None:
        return None
    if preserve_placeholder and _PLACEHOLDER_RE.fullmatch(candidate):
        return candidate

    lowered = candidate.lower()
    if (
        _SAFE_DATA_IMAGE_RE.fullmatch(candidate)
        and len(candidate) <= _MAX_INLINE_IMAGE_CHARS
    ):
        return candidate

    if for_export and Path(candidate).is_absolute():
        base_uploads_dir = uploads_root(uploads_dir=uploads_dir).resolve()
        owner_root = (base_uploads_dir / str(project.user_id)).resolve()
        local_candidate = Path(candidate).resolve()
        try:
            local_candidate.relative_to(owner_root)
        except ValueError:
            local_candidate = None
        if local_candidate is not None and local_candidate.is_file():
            return str(local_candidate)

    owner_file = _owner_upload_file(
        candidate,
        project=project,
        uploads_dir=uploads_dir,
    )
    if owner_file is not None:
        if for_export:
            if owner_file.is_file():
                return str(owner_file)
            logger.warning(
                "Translator export image is unavailable for project %s.",
                project.id,
            )
            return _TRANSPARENT_PIXEL
        # Keep the canonical web reference in previews.  It is already
        # confined to this project's owner subtree by _owner_upload_file.
        return candidate

    # Exported HTML is consumed by Pandoc, so no untrusted URL or local path
    # may remain for it to fetch.  A transparent pixel keeps the document's
    # layout stable and gives the same safe result in previews.
    if lowered.startswith("/uploads/") or for_export:
        logger.warning(
            "Translator blocked an unsafe image source for project %s.",
            project.id,
        )
    return _TRANSPARENT_PIXEL


def _safe_attr(
    tag_name: str,
    attr_name: str,
    raw_value: Any,
    *,
    project: TranslationProject,
    for_export: bool,
    uploads_dir: str | Path | None,
    preserve_placeholder: bool,
) -> str | None:
    # Images have a larger (but still conservative) bound than ordinary
    # scalar attributes.  Dispatch before the generic cap so valid inline
    # images are not rejected merely because their base64 payload is >4 KiB.
    if attr_name == "src":
        return _safe_image_source(
            raw_value,
            project=project,
            for_export=for_export,
            uploads_dir=uploads_dir,
            preserve_placeholder=preserve_placeholder,
        )

    if attr_name == "hidden":
        boolean_value = _attribute_value(raw_value).strip().casefold()
        return "hidden" if boolean_value in {"", "hidden"} else None

    value = _safe_scalar(raw_value, maximum=4096)
    if value is None:
        return None

    if attr_name == "href":
        return _safe_uri(value, preserve_placeholder=preserve_placeholder)
    if attr_name == "target":
        return value.lower() if value.lower() in {"_blank", "_self"} else None
    if attr_name == "rel":
        tokens = [token.lower() for token in value.split()]
        allowed = {"noopener", "noreferrer", "nofollow", "ugc", "sponsored"}
        cleaned = sorted({token for token in tokens if token in allowed})
        return " ".join(cleaned) or None
    if attr_name == "charset":
        return "utf-8" if value.casefold() == "utf-8" else None
    if attr_name in {"lang", "xml:lang"}:
        return value if _SAFE_LANG_RE.fullmatch(value) else None
    if attr_name == "dir":
        return value.lower() if value.lower() in {"ltr", "rtl", "auto"} else None
    if attr_name == "class":
        classes = [token for token in value.split() if _SAFE_TOKEN_RE.fullmatch(token)]
        return " ".join(classes[:32]) or None
    if attr_name in {"id", "name", "headers"}:
        tokens = value.split() if attr_name == "headers" else [value]
        if not tokens or any(not _SAFE_IDENTIFIER_RE.fullmatch(token) for token in tokens):
            return None
        return " ".join(tokens)
    if attr_name in {"colspan", "rowspan", "span", "width", "height", "start", "value"}:
        return value if _SAFE_INT_RE.fullmatch(value) else None
    if attr_name == "reversed":
        return "reversed" if value.casefold() in {"", "reversed"} else None
    if attr_name == "type" and tag_name == "ol":
        return value if value in {"1", "a", "A", "i", "I"} else None
    if attr_name == "scope" and tag_name == "th":
        return (
            value.casefold()
            if value.casefold() in {"row", "col", "rowgroup", "colgroup"}
            else None
        )
    if attr_name == "loading" and tag_name == "img":
        return value.lower() if value.lower() in {"lazy", "eager"} else None
    if attr_name == "datetime" and tag_name == "time":
        return value if len(value) <= 128 else None
    if attr_name in {"aria-hidden"}:
        return value.lower() if value.lower() in {"true", "false"} else None
    return value[:4096]


def _sanitize_soup(
    soup: BeautifulSoup,
    *,
    project: TranslationProject,
    for_export: bool,
    uploads_dir: str | Path | None,
    preserve_placeholder: bool = False,
) -> BeautifulSoup:
    for comment in soup.find_all(string=lambda value: isinstance(value, Comment)):
        comment.extract()

    for tag in list(soup.find_all(True)):
        tag_name = str(tag.name or "").lower()
        if not tag_name:
            continue
        if tag_name in _DROP_CONTENT_TAGS:
            tag.decompose()
            continue
        if tag_name not in _DOCUMENT_TAGS:
            tag.unwrap()
            continue
        if tag_name == "meta":
            # Only a harmless encoding declaration is useful in a translated
            # document.  This also drops http-equiv=refresh without relying
            # on a fragile attribute spelling check.
            charset = _attribute_value(tag.get("charset")).casefold()
            if charset != "utf-8":
                tag.decompose()
                continue

        allowed = set(_GLOBAL_ATTRIBUTES) | _TAG_ATTRIBUTES.get(tag_name, set())
        for raw_name in list(tag.attrs):
            attr_name = str(raw_name or "").lower()
            if (
                not attr_name
                or attr_name.startswith("on")
                or attr_name in {"style", "srcdoc", "formaction"}
                or attr_name not in allowed
            ):
                del tag.attrs[raw_name]
                continue
            cleaned = _safe_attr(
                tag_name,
                attr_name,
                tag.attrs.get(raw_name),
                project=project,
                for_export=for_export,
                uploads_dir=uploads_dir,
                preserve_placeholder=preserve_placeholder,
            )
            if cleaned is None:
                del tag.attrs[raw_name]
            else:
                tag.attrs[attr_name] = cleaned
                if raw_name != attr_name:
                    del tag.attrs[raw_name]

        if tag_name == "a" and tag.get("target") == "_blank":
            rel = set(str(tag.get("rel") or "").split())
            rel.update({"noopener", "noreferrer"})
            tag["rel"] = " ".join(sorted(rel))
    return soup


def _sanitize_document_html(
    value: str | None,
    *,
    project: TranslationProject,
    for_export: bool,
    uploads_dir: str | Path | None,
    preserve_placeholder: bool = False,
) -> str:
    soup = BeautifulSoup(str(value or ""), "html.parser")
    _sanitize_soup(
        soup,
        project=project,
        for_export=for_export,
        uploads_dir=uploads_dir,
        preserve_placeholder=preserve_placeholder,
    )
    return str(soup)


def _sanitize_fragment(
    value: str | None,
    *,
    project: TranslationProject,
    for_export: bool,
    uploads_dir: str | Path | None,
) -> str:
    soup = BeautifulSoup(str(value or ""), "html.parser")
    _sanitize_soup(
        soup,
        project=project,
        for_export=for_export,
        uploads_dir=uploads_dir,
    )
    if soup.body is not None:
        return soup.body.decode_contents()
    # A segment is a flow-content fragment, not a document head.  Drop any
    # document-level nodes a provider attempted to smuggle into it.
    for tag in soup.find_all(["html", "head", "meta", "title"]):
        tag.decompose()
    return soup.decode_contents()


def _translator_note_id(note: Any) -> str | None:
    """Build deterministic, HTML-safe IDs from a persisted note identity."""

    raw_id = str(getattr(note, "id", "") or "").strip()
    if not raw_id:
        return None
    normalized = re.sub(r"[^A-Za-z0-9_.:-]", "-", raw_id)
    return normalized if normalized else None


def _inject_translator_note(
    fragment: BeautifulSoup,
    note: Any,
    *,
    reference_id: str,
    footnote_id: str,
    number: int,
) -> bool:
    """Insert one noteref into a parsed target fragment text node."""

    anchor = str(getattr(note, "anchor_text", "") or "")
    matching_nodes = [
        node
        for node in fragment.find_all(string=True)
        if anchor and anchor in str(node)
    ]
    if len(matching_nodes) != 1 or str(matching_nodes[0]).count(anchor) != 1:
        return False

    node = matching_nodes[0]
    value = str(node)
    before, after = value.split(anchor, 1)
    reference = fragment.new_tag(
        "a",
        id=reference_id,
        href=f"#{footnote_id}",
        role="doc-noteref",
    )
    reference["epub:type"] = "noteref"
    reference["aria-label"] = f"Translator note T{number}"
    reference.string = f"[T{number}]"
    for replacement in (
        before,
        anchor,
        reference,
        after,
    ):
        if replacement == "":
            continue
        if isinstance(replacement, str):
            node.insert_before(replacement)
        else:
            node.insert_before(replacement)
    node.extract()
    return True


def _append_translator_footnotes(
    soup: BeautifulSoup,
    records: list[tuple[str, str, str]],
    *,
    container_id: str,
) -> None:
    """Append semantic reader notes in stable segment/order order."""

    if not records:
        return
    container = soup.new_tag(
        "section",
        id=container_id,
        role="doc-endnotes",
    )
    container["epub:type"] = "endnotes"
    note_list = soup.new_tag("div", role="list")
    container.append(note_list)
    for number, (footnote_id, reference_id, body) in enumerate(records, start=1):
        aside = soup.new_tag(
            "div",
            id=footnote_id,
            role="listitem",
        )
        aside["epub:type"] = "footnote"
        aside["class"] = "translator-note"
        aside["data-label"] = "Translator note"
        paragraph = soup.new_tag("p")
        marker = soup.new_tag("span", **{"class": "footnote-marker"})
        marker.string = f"T{number}. "
        paragraph.append(marker)
        label = soup.new_tag("span", **{"class": "translator-note-label"})
        label.string = "Translator’s note: "
        paragraph.append(label)
        formatted = BeautifulSoup(render_note_body(body), "html.parser")
        for child in list(formatted.contents):
            paragraph.append(child)
        backlink = soup.new_tag(
            "a",
            href=f"#{reference_id}",
            role="doc-backlink",
        )
        backlink["epub:type"] = "backlink"
        backlink.string = " ↩"
        paragraph.append(backlink)
        aside.append(paragraph)
        note_list.append(aside)
    body = soup.find("body")
    if body is not None:
        body.append(container)
    else:
        soup.append(container)


def render_project_html(
    project: TranslationProject,
    *,
    for_export: bool = False,
    uploads_dir: str | Path | None = None,
) -> str:
    """Reassemble one translation without exposing another user's local media."""

    if not project.skeleton_html:
        return ""
    # Sanitize the immutable skeleton before substitution.  In particular,
    # this prevents a placeholder inside a script or event-bearing element
    # from becoming a live node after the translation is inserted.
    html_content = _sanitize_document_html(
        project.skeleton_html,
        project=project,
        for_export=for_export,
        uploads_dir=uploads_dir,
        preserve_placeholder=True,
    )
    replacements: dict[str, str] = {}
    note_records: list[tuple[str, str, str]] = []
    note_ids: set[str] = set()
    existing_ids = {
        str(tag.get("id"))
        for tag in BeautifulSoup(html_content, "html.parser").find_all(True)
        if tag.get("id")
    }
    notes_enabled = (getattr(project, "settings", None) or {}).get(
        "enable_translator_notes"
    ) is True
    for segment in project.segments:
        placeholder = f"{{{{{segment.id}}}}}"
        if segment.merged_into_id:
            replacements[placeholder] = ""
        else:
            replacement = _sanitize_fragment(
                _translated_segment_text(segment),
                project=project,
                for_export=for_export,
                uploads_dir=uploads_dir,
            )
            if notes_enabled:
                fragment = BeautifulSoup(replacement, "html.parser")
                active_notes = [
                    note
                    for note in getattr(segment, "translator_notes", ())
                    if getattr(note, "state", None) == "active"
                    and getattr(note, "segment_version", None)
                    == getattr(segment, "version", None)
                ]
                active_notes.sort(
                    key=lambda note: (
                        int(getattr(note, "order_index", 0)),
                        str(getattr(note, "id", "")),
                    )
                )
                for note in active_notes:
                    note_slug = _translator_note_id(note)
                    anchor = str(getattr(note, "anchor_text", "") or "")
                    body = str(getattr(note, "body", "") or "")
                    if (
                        note_slug is None
                        or note_slug in note_ids
                        or not anchor
                        or len(anchor) > 160
                        or len(body) > 800
                        or not body.strip()
                        or _NOTE_CONTROL_RE.search(anchor)
                        or _NOTE_CONTROL_RE.search(body)
                        or _NOTE_MARKUP_RE.search(anchor)
                        or _NOTE_MARKUP_RE.search(body)
                        or _NOTE_LINK_RE.search(anchor)
                        or _NOTE_LINK_RE.search(body)
                    ):
                        logger.warning(
                            "Translator note omitted during rendering for project %s.",
                            project.id,
                        )
                        continue
                    footnote_id = f"translator-note-{note_slug}"
                    reference_id = f"translator-note-ref-{note_slug}"
                    if footnote_id in existing_ids or reference_id in existing_ids:
                        logger.warning(
                            "Translator note omitted during rendering for project %s.",
                            project.id,
                        )
                        continue
                    if not _inject_translator_note(
                        fragment,
                        note,
                        reference_id=reference_id,
                        footnote_id=footnote_id,
                        number=len(note_records) + 1,
                    ):
                        logger.warning(
                            "Translator note omitted during rendering for project %s.",
                            project.id,
                        )
                        continue
                    note_ids.add(note_slug)
                    existing_ids.update({footnote_id, reference_id})
                    note_records.append((footnote_id, reference_id, body))
                replacement = fragment.decode_contents()
            if str(segment.outer_tag or "").lower() == "img":
                replacement = html.escape(
                    BeautifulSoup(replacement, "html.parser").get_text(" ", strip=True),
                    quote=False,
                )
            replacements[placeholder] = replacement

    def _replace_placeholder(match: re.Match[str]) -> str:
        token = match.group(0)
        return replacements.get(token, token)

    # A single pass avoids the O(number-of-segments * document-size) behavior
    # of repeated str.replace calls while keeping placeholders literal.
    html_content = _PLACEHOLDER_TOKEN_RE.sub(_replace_placeholder, html_content)

    soup = BeautifulSoup(html_content, "html.parser")
    _sanitize_soup(
        soup,
        project=project,
        for_export=for_export,
        uploads_dir=uploads_dir,
    )
    if notes_enabled:
        project_slug = re.sub(r"[^A-Za-z0-9_-]", "-", str(project.id))[:80] or "project"
        container_base = f"translator-notes-{project_slug}"
        container_id = container_base
        suffix = 2
        while container_id in existing_ids:
            container_id = f"{container_base}-{suffix}"
            suffix += 1
        _append_translator_footnotes(
            soup,
            note_records,
            container_id=container_id,
        )

    if for_export:
        normalize_export_note_roles(soup)

    target_lang = str(project.target_lang or "").strip()
    safe_language = target_lang if _SAFE_LANG_RE.fullmatch(target_lang) else "und"
    root = soup.find("html")
    if root is not None:
        # A source document may already provide a complete HTML frame.  Its
        # language still needs to follow the translation target rather than
        # leaking the source language (or an attacker-controlled value).
        root["lang"] = safe_language
        return str(soup)

    final_body = str(soup)
    safe_title = html.escape(str(project.name or ""), quote=True)
    return f"""<!DOCTYPE html>
<html lang="{safe_language}">
<head>
    <meta charset="utf-8">
    <title>{safe_title}</title>
</head>
<body>
{final_body}
</body>
</html>"""


def translated_subtitle_text(segment: TranslationSegment) -> str:
    # Subtitle output is plain text, but the source/model value can still
    # contain active markup.  Reuse the same fragment policy so script/style
    # contents do not become subtitle text either.
    value = _translated_segment_text(segment)
    soup = BeautifulSoup(value, "html.parser")
    for tag in soup.find_all(_DROP_CONTENT_TAGS):
        tag.decompose()
    return soup.get_text(" ", strip=True)


def translated_subtitle_cues(
    project: TranslationProject,
    *,
    subtitle_provider: Any,
) -> list[dict]:
    settings = project.settings or {}
    subtitle_settings = (
        settings.get("subtitle") if isinstance(settings.get("subtitle"), dict) else {}
    )
    unit_metadata = (
        subtitle_settings.get("units")
        if isinstance(subtitle_settings.get("units"), dict)
        else {}
    )
    render_settings = (
        subtitle_settings.get("render_settings")
        if isinstance(subtitle_settings.get("render_settings"), dict)
        else {}
    )
    segments = list(
        TranslationSegment.query.filter_by(project_id=project.id)
        .order_by(TranslationSegment.order_index.asc())
        .all()
    )
    merged_children: dict[str, list[TranslationSegment]] = {}
    for segment in segments:
        if segment.merged_into_id:
            merged_children.setdefault(segment.merged_into_id, []).append(segment)

    output = []
    for segment in segments:
        if segment.merged_into_id:
            continue
        span_segments = [segment] + sorted(
            merged_children.get(segment.id, []),
            key=lambda item: item.order_index,
        )
        span_meta = [unit_metadata.get(item.id) or {} for item in span_segments]
        text = translated_subtitle_text(segment)
        if text:
            output.append({"id": segment.id, "text": text, "spans": span_meta})
    return subtitle_provider.client.render_translation_segments(
        output,
        settings=render_settings,
    )


__all__ = [
    "render_project_html",
    "translated_subtitle_cues",
    "translated_subtitle_text",
]
