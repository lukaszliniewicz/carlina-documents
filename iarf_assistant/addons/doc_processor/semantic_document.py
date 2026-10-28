"""Canonical semantic HTML at the Doc Processor completion boundary."""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from bs4 import BeautifulSoup, Comment, Tag

from carlina.core.semantic_diagrams import diagnose_mermaid_diagrams
from carlina.core.semantic_html import (
    MAX_INLINE_IMAGE_CHARS,
    SAFE_CONTROL_RE,
    SAFE_DATA_IMAGE_RE,
    SAFE_IDENTIFIER_RE,
    SAFE_INTEGER_RE,
    SAFE_LANGUAGE_RE,
    SAFE_LINK_SCHEMES,
    SAFE_TOKEN_RE,
    SEMANTIC_DOCUMENT_TAGS,
    SEMANTIC_DROP_CONTENT_TAGS,
    SEMANTIC_HTML_CONTRACT_VERSION,
    allowed_attributes,
)
from carlina.core.semantic_images import diagnose_image_descriptions

from ...core.upload_paths import resolve_upload_reference, uploads_root
from .parsers.semantic_validation import diagnose_source_toc_blocks

MAX_SEMANTIC_DIAGNOSTICS = 64
MAX_DIAGNOSTIC_VALUE_BYTES = 256
MAX_ATTRIBUTE_CHARS = 4_096
_HEADING_RE = re.compile(r"^h[1-6]$")
_LEGACY_NOTE_ID_RE = re.compile(r"^fn-?[0-9]{1,6}$", re.IGNORECASE)


class SemanticDocumentError(ValueError):
    """A document cannot become a completed semantic source."""


def serialize_semantic_html(soup: BeautifulSoup) -> str:
    """Serialize new or explicitly rewritten HTML with safe, compact Unicode.

    Keep HTML metacharacters escaped and nonbreaking spaces explicit, without
    turning ordinary Cyrillic/Greek letters into separate entity nodes. Stored
    identity snapshots must continue to be read as their original bytes.
    """

    return soup.decode(formatter="minimal").replace("\xa0", "&nbsp;")


@dataclass(frozen=True)
class SemanticDocumentResult:
    html: str
    contract_version: str
    content_hash: str
    diagnostics: tuple[dict[str, str], ...]

    @property
    def errors(self) -> tuple[dict[str, str], ...]:
        return tuple(item for item in self.diagnostics if item["level"] == "error")


def _attribute_text(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return " ".join(str(item) for item in value)
    return str(value or "")


def _diagnostic(recorder: list[dict[str, str]], level: str, code: str) -> None:
    if len(recorder) >= MAX_SEMANTIC_DIAGNOSTICS:
        return
    item = {"level": level, "code": code}
    if item not in recorder:
        recorder.append(item)


def _safe_scalar(value: Any, maximum: int = MAX_ATTRIBUTE_CHARS) -> str | None:
    candidate = _attribute_text(value).strip()
    if (
        not candidate
        or len(candidate.encode("utf-8")) > maximum
        or SAFE_CONTROL_RE.search(candidate)
    ):
        return None
    return candidate


def _safe_uri(value: Any) -> str | None:
    candidate = _safe_scalar(value)
    if candidate is None or "\\" in candidate or candidate.startswith("//"):
        return None
    if candidate.startswith("#"):
        return (
            candidate
            if len(candidate) > 1 and SAFE_IDENTIFIER_RE.fullmatch(candidate[1:])
            else None
        )
    parsed = urlsplit(candidate)
    return candidate if parsed.scheme.lower() in SAFE_LINK_SCHEMES else None


def _owner_root(
    user_id: int | str, uploads_dir: Path | str | None
) -> tuple[Path, Path] | None:
    owner = str(user_id or "").strip()
    if not owner.isdigit():
        return None
    base = uploads_root(uploads_dir=uploads_dir).resolve()
    return base, (base / owner).resolve()


def _source_directory(
    source_file_path: str | None,
    *,
    uploads_dir: Path | str | None,
) -> Path | None:
    if not source_file_path:
        return None
    try:
        source = resolve_upload_reference(source_file_path, uploads_dir=uploads_dir)
    except OSError, RuntimeError, TypeError, ValueError:
        source = None
    if source is None:
        try:
            source = Path(source_file_path)
        except OSError, TypeError, ValueError:
            return None
    try:
        return source.resolve().parent
    except OSError:
        return None


def _canonical_image_source(
    value: Any,
    *,
    user_id: int | str,
    source_file_path: str | None,
    uploads_dir: Path | str | None,
) -> str | None:
    candidate = _safe_scalar(value, maximum=MAX_INLINE_IMAGE_CHARS)
    if candidate is None:
        return None
    if (
        SAFE_DATA_IMAGE_RE.fullmatch(candidate)
        and len(candidate) <= MAX_INLINE_IMAGE_CHARS
    ):
        return candidate
    if "\\" in candidate:
        return None
    parsed = urlsplit(candidate)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        return None
    path = parsed.path
    if "%" in path:
        # Accept a single URI encoding of ordinary filename characters. Never
        # leave another decoding step, separators or traversal for a web server.
        if re.search(r"%(?![0-9a-fA-F]{2})|%(?:2f|5c|3f|23|25|3a)", path, re.I):
            return None
        try:
            path = unquote(path, encoding="utf-8", errors="strict")
        except UnicodeDecodeError:
            return None
        if SAFE_CONTROL_RE.search(path) or any(
            part in {".", ".."} for part in path.split("/")
        ):
            return None
    roots = _owner_root(user_id, uploads_dir)
    if roots is None:
        return None
    base, owner_root = roots
    source_dir = _source_directory(source_file_path, uploads_dir=uploads_dir)
    if path.startswith("/uploads/"):
        candidate_path = base.joinpath(*path.removeprefix("/uploads/").split("/"))
    elif path.startswith("/"):
        candidate_path = Path(path)
    elif source_dir is not None:
        candidate_path = source_dir / path
    else:
        return None
    try:
        resolved = candidate_path.resolve()
        relative = resolved.relative_to(owner_root)
    except OSError, ValueError:
        return None
    return f"/uploads/{user_id}/{relative.as_posix()}"


def _safe_attribute(
    tag_name: str,
    attr_name: str,
    value: Any,
    *,
    user_id: int | str,
    source_file_path: str | None,
    uploads_dir: Path | str | None,
) -> str | None:
    if attr_name == "src":
        return _canonical_image_source(
            value,
            user_id=user_id,
            source_file_path=source_file_path,
            uploads_dir=uploads_dir,
        )
    if attr_name in {"href", "cite"}:
        return _safe_uri(value)
    if attr_name == "hidden":
        return (
            "hidden"
            if _attribute_text(value).strip().casefold() in {"", "hidden"}
            else None
        )
    value = _safe_scalar(value)
    if value is None:
        return None
    if attr_name == "charset":
        return "utf-8" if value.casefold() == "utf-8" else None
    if attr_name in {"lang", "xml:lang"}:
        return value if SAFE_LANGUAGE_RE.fullmatch(value) else None
    if attr_name == "dir":
        return value.casefold() if value.casefold() in {"ltr", "rtl", "auto"} else None
    if attr_name == "class":
        tokens = [token for token in value.split() if SAFE_TOKEN_RE.fullmatch(token)]
        return " ".join(tokens[:32]) or None
    if attr_name in {"id", "name"}:
        return value if SAFE_IDENTIFIER_RE.fullmatch(value) else None
    if attr_name == "data-footnote-scope":
        return value if SAFE_TOKEN_RE.fullmatch(value) else None
    if attr_name == "headers":
        tokens = value.split()
        return (
            " ".join(tokens)
            if tokens and all(SAFE_IDENTIFIER_RE.fullmatch(token) for token in tokens)
            else None
        )
    if attr_name in {"colspan", "rowspan", "span", "width", "height", "start", "value"}:
        return value if SAFE_INTEGER_RE.fullmatch(value) else None
    if attr_name == "reversed":
        return "reversed" if value.casefold() in {"", "reversed"} else None
    if attr_name == "type" and tag_name == "ol":
        return value if value in {"1", "a", "A", "i", "I"} else None
    if attr_name == "scope":
        return (
            value.casefold()
            if value.casefold() in {"row", "col", "rowgroup", "colgroup"}
            else None
        )
    if attr_name == "loading":
        return value.casefold() if value.casefold() in {"lazy", "eager"} else None
    if attr_name == "target":
        return value.casefold() if value.casefold() in {"_blank", "_self"} else None
    if attr_name == "rel":
        allowed = {"noopener", "noreferrer", "nofollow", "ugc", "sponsored"}
        return (
            " ".join(sorted({item.casefold() for item in value.split()} & allowed))
            or None
        )
    if attr_name == "aria-hidden":
        return value.casefold() if value.casefold() in {"true", "false"} else None
    return value


def _slugify_heading_id(text: str, seen: set[str]) -> str:
    base = "-".join(text.casefold().split())
    base = (
        "".join(char if char.isalnum() or char in "-_" else "-" for char in base).strip(
            "-"
        )[:64]
        or "section"
    )
    candidate, suffix = base, 2
    while candidate in seen:
        candidate = f"{base}-{suffix}"
        suffix += 1
    seen.add(candidate)
    return candidate


def _is_noteref(tag: Tag) -> bool:
    return (
        str(tag.get("role") or "").casefold() == "doc-noteref"
        or "noteref" in str(tag.get("epub:type") or "").casefold().split()
        or "footnote-ref" in set(tag.get("class") or [])
    )


def _is_endnote(tag: Tag) -> bool:
    epub_types = set(str(tag.get("epub:type") or "").casefold().split())
    return (
        str(tag.get("role") or "").casefold() in {"doc-endnote", "doc-footnote"}
        or bool(epub_types & {"endnote", "footnote"})
        or "footnote" in set(tag.get("class") or [])
    )


# Public predicates are intentionally defined below the canonicalizer's own
# helpers so EPUB and DOCX share the same note vocabulary without importing
# either exporter.  The ``fn-12`` fallback is only for legacy HTML; arbitrary
# IDs such as ``fnord-chapter`` are never notes.
is_doc_noteref = _is_noteref
is_doc_endnote = _is_endnote


def is_legacy_note_id(value: object) -> bool:
    return bool(_LEGACY_NOTE_ID_RE.fullmatch(str(value or "").strip()))


def namespace_semantic_fragment(
    soup: BeautifulSoup, *, namespace: str
) -> dict[str, str]:
    """Namespace one canonical document before composing a multi-document export.

    Canonical IDs are document-local.  Project exports combine several canonical
    trees, so repeated page-break, heading, table-header, and footnote IDs must be
    made unique together with every local ID reference.  The stored source is never
    modified; this operates only on the export copy.
    """

    prefix = str(namespace or "").strip()
    if not SAFE_TOKEN_RE.fullmatch(prefix):
        raise ValueError("Semantic fragment namespace must be one safe HTML token.")

    identifiers: list[str] = []
    seen_identifiers: set[str] = set()
    for tag in soup.find_all(id=True):
        identifier = str(tag.get("id") or "").strip()
        if not identifier:
            continue
        if identifier in seen_identifiers:
            raise ValueError(f"Duplicate HTML id {identifier!r} before composition.")
        seen_identifiers.add(identifier)
        identifiers.append(identifier)

    mapping: dict[str, str] = {}
    generated: set[str] = set()
    # Diagram labels bind to their figure by ID prefix. Shorten the family
    # together, reserving space for the longest key, rather than hashing each
    # overlong child ID independently and breaking that binding.
    diagram_mapping: dict[str, str] = {}
    if diagnose_mermaid_diagrams(soup):
        raise ValueError("Invalid semantic diagram before composition.")
    for figure in soup.select("figure.mermaid-diagram-v1"):
        figure_id = str(figure["id"])
        labels = figure.select("div.mermaid-labels > p")
        keys = [str(label["id"])[len(figure_id) + 2:] for label in labels]
        budget = 128 - 2 - max(map(len, keys), default=0)
        candidate = f"{prefix}-{figure_id}"
        if len(candidate) > budget:
            suffix = hashlib.sha256(candidate.encode("utf-8")).hexdigest()[:12]
            candidate = f"{candidate[:budget - 13]}-{suffix}"
        diagram_mapping[figure_id] = candidate
        for label, key in zip(labels, keys):
            diagram_mapping[str(label["id"])] = f"{candidate}--{key}"
    for identifier in identifiers:
        candidate = diagram_mapping.get(identifier, f"{prefix}-{identifier}")
        if len(candidate) > 128:
            suffix = hashlib.sha256(identifier.encode("utf-8")).hexdigest()[:12]
            budget = max(1, 128 - len(prefix) - len(suffix) - 2)
            candidate = f"{prefix}-{identifier[:budget]}-{suffix}"
        if candidate in generated:
            raise ValueError("Semantic fragment namespace produced an ID collision.")
        generated.add(candidate)
        mapping[identifier] = candidate

    for tag in soup.find_all(True):
        identifier = str(tag.get("id") or "").strip()
        if identifier in mapping:
            tag["id"] = mapping[identifier]

        anchor_name = str(tag.get("name") or "").strip()
        if anchor_name in mapping:
            tag["name"] = mapping[anchor_name]

        for attribute in ("href", "cite"):
            value = str(tag.get(attribute) or "").strip()
            if not value.startswith("#"):
                continue
            target = unquote(value[1:])
            if target in mapping:
                tag[attribute] = f"#{mapping[target]}"

        for attribute in ("aria-describedby", "aria-labelledby", "headers"):
            value = _attribute_text(tag.get(attribute)).strip()
            if not value:
                continue
            tokens = value.split()
            tag[attribute] = " ".join(mapping.get(token, token) for token in tokens)

    return mapping


def diagnose_semantic_export(soup: BeautifulSoup) -> list[str]:
    """Return bounded structural/reference diagnostics before an export."""

    errors: list[str] = []
    error_count = 0

    def record(message: str) -> None:
        nonlocal error_count
        error_count += 1
        if len(errors) < MAX_SEMANTIC_DIAGNOSTICS - 1:
            errors.append(message)

    def bounded(value: object) -> str:
        text = str(value)
        if len(text) <= MAX_DIAGNOSTIC_VALUE_BYTES:
            return text
        return f"{text[: MAX_DIAGNOSTIC_VALUE_BYTES - 1]}…"

    for code in diagnose_mermaid_diagrams(soup):
        record(code)
    for code in diagnose_image_descriptions(soup):
        record(code)

    ids: dict[str, Tag] = {}
    for tag in soup.find_all(id=True):
        raw_id = str(tag.get("id") or "").strip()
        if not raw_id:
            record(f"empty id on <{tag.name}>")
        elif raw_id in ids:
            record(f"Duplicate HTML id {bounded(raw_id)!r}")
        else:
            ids[raw_id] = tag

    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href") or "").strip()
        if not href.startswith("#"):
            continue
        target_id = unquote(href[1:])
        if not target_id:
            record("empty internal href fragment")
        elif target_id not in ids:
            record(f"Unresolved internal EPUB link {bounded(href)!r}")

    for ref in soup.find_all(is_doc_noteref):
        href = str(ref.get("href") or "").strip()
        if not ref.get_text(" ", strip=True):
            record("doc-noteref has no stable visible label")
        if not href.startswith("#"):
            record("doc-noteref must use a local fragment href")
            continue
        target_id = unquote(href[1:])
        target = ids.get(target_id)
        if target is None:
            continue
        if not is_doc_endnote(target):
            record(
                f"doc-noteref {bounded(href)!r} does not resolve to a doc-endnote target"
            )
        if not target.get_text(" ", strip=True):
            record(f"doc-endnote target {bounded(target_id)!r} has no visible content")

    if error_count > len(errors):
        errors.append(
            f"{error_count - len(errors)} additional semantic diagnostics omitted"
        )
    return errors


def validate_semantic_export(soup: BeautifulSoup, *, export_name: str) -> None:
    errors = diagnose_semantic_export(soup)
    if errors:
        detail = "; ".join(errors[:12])
        if len(errors) > 12:
            detail += f"; and {len(errors) - 12} more"
        raise ValueError(f"{export_name} semantic diagnostics failed: {detail}")


def canonicalize_document_html(
    html_content: str | None,
    *,
    user_id: int | str,
    source_file_path: str | None = None,
    uploads_dir: Path | str | None = None,
    relaxed: bool = False,
) -> SemanticDocumentResult:
    """Canonicalize untrusted document HTML without changing persistent state.

    ``relaxed`` is only for paused/error previews.  It retains the same inert
    tree but downgrades reference-integrity failures; callers must never mark
    relaxed output as completed or hand it to Translator.
    """

    diagnostics: list[dict[str, str]] = []
    soup = BeautifulSoup(str(html_content or ""), "html.parser")
    for comment in soup.find_all(string=lambda value: isinstance(value, Comment)):
        comment.extract()
        _diagnostic(diagnostics, "warning", "comment_removed")

    for tag in list(soup.find_all(True)):
        # ``decompose()`` removes every descendant from the tree as well.  The
        # snapshot returned by ``find_all`` still contains those detached tag
        # objects, so skip them rather than attempting to unwrap a node that no
        # longer belongs to this document.
        if tag.parent is None:
            continue
        name = str(tag.name or "").casefold()
        if name in SEMANTIC_DROP_CONTENT_TAGS:
            tag.decompose()
            _diagnostic(diagnostics, "warning", "active_or_embedded_tag_removed")
            continue
        if name not in SEMANTIC_DOCUMENT_TAGS:
            tag.unwrap()
            _diagnostic(diagnostics, "warning", "unsupported_tag_unwrapped")
            continue
        if name == "meta" and _attribute_text(tag.get("charset")).casefold() != "utf-8":
            tag.decompose()
            _diagnostic(diagnostics, "warning", "unsafe_meta_removed")
            continue
        for raw_name in list(tag.attrs):
            attr_name = str(raw_name or "").casefold()
            if (
                not attr_name
                or attr_name.startswith("on")
                or attr_name in {"style", "srcdoc", "formaction"}
                or attr_name not in allowed_attributes(name)
            ):
                del tag.attrs[raw_name]
                _diagnostic(diagnostics, "warning", "unsafe_attribute_removed")
                continue
            cleaned = _safe_attribute(
                name,
                attr_name,
                tag.attrs[raw_name],
                user_id=user_id,
                source_file_path=source_file_path,
                uploads_dir=uploads_dir,
            )
            if cleaned is None:
                del tag.attrs[raw_name]
                _diagnostic(diagnostics, "warning", "invalid_attribute_removed")
            else:
                tag.attrs[attr_name] = cleaned
                if raw_name != attr_name:
                    del tag.attrs[raw_name]
        if name == "a" and tag.get("target") == "_blank":
            tag["rel"] = " ".join(
                sorted(
                    set(str(tag.get("rel") or "").split()) | {"noopener", "noreferrer"}
                )
            )

    ids: dict[str, Tag] = {}
    for tag in soup.find_all(id=True):
        identifier = str(tag.get("id") or "")
        if identifier in ids:
            _diagnostic(diagnostics, "error", "duplicate_id")
        else:
            ids[identifier] = tag
    seen_ids = set(ids)
    for heading in soup.find_all(_HEADING_RE):
        if not heading.get("id"):
            heading["id"] = _slugify_heading_id(
                heading.get_text(" ", strip=True), seen_ids
            )
            ids[str(heading["id"])] = heading

    referenced_endnotes: set[int] = set()
    for link in soup.find_all("a"):
        href = str(link.get("href") or "")
        noteref = _is_noteref(link)
        if noteref and not link.get_text(" ", strip=True):
            _diagnostic(
                diagnostics,
                "error" if not relaxed else "warning",
                "noteref_missing_visible_label",
            )
        if noteref and not href.startswith("#"):
            _diagnostic(
                diagnostics,
                "error" if not relaxed else "warning",
                "noteref_missing_local_target",
            )
            continue
        if not href.startswith("#"):
            continue
        target = ids.get(href[1:])
        if target is None:
            _diagnostic(
                diagnostics,
                "error" if not relaxed else "warning",
                "unresolved_fragment_reference",
            )
            continue
        if noteref:
            if not _is_endnote(target):
                _diagnostic(
                    diagnostics,
                    "error" if not relaxed else "warning",
                    "noteref_target_not_endnote",
                )
            else:
                referenced_endnotes.add(id(target))
                if not target.get_text(" ", strip=True):
                    _diagnostic(
                        diagnostics,
                        "error" if not relaxed else "warning",
                        "noteref_target_empty",
                    )
    for target in soup.find_all(True):
        if _is_endnote(target) and id(target) not in referenced_endnotes:
            _diagnostic(diagnostics, "warning", "unlinked_note_body")

    for code in diagnose_source_toc_blocks(soup):
        _diagnostic(
            diagnostics,
            "warning" if relaxed else "error",
            code,
        )

    for code in diagnose_mermaid_diagrams(soup):
        _diagnostic(diagnostics, "warning" if relaxed else "error", code)
    for code in diagnose_image_descriptions(soup):
        _diagnostic(diagnostics, "warning" if relaxed else "error", code)

    # A title subtitle is visually and semantically a separate line even when
    # a provider emits the opening span immediately after the title text.  Keep
    # that boundary as source whitespace so textContent consumers and the
    # Translator projection cannot collapse ``Title`` and ``Subtitle`` into a
    # fabricated word.
    for subtitle in soup.select("h1 > span.subtitle"):
        previous = subtitle.previous_sibling
        if previous is not None and not str(previous).endswith(
            (" ", "\t", "\r", "\n")
        ):
            subtitle.insert_before("\n")

    canonical = serialize_semantic_html(soup)
    return SemanticDocumentResult(
        html=canonical,
        contract_version=SEMANTIC_HTML_CONTRACT_VERSION,
        content_hash=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        diagnostics=tuple(diagnostics),
    )


def semantic_settings(
    settings: dict[str, Any] | None,
    result: SemanticDocumentResult,
    *,
    mode: str = "completed",
) -> dict[str, Any]:
    """Return settings with bounded, source-safe semantic contract facts."""

    updated = dict(settings or {})
    updated["semantic_contract_version"] = result.contract_version
    updated["semantic_content_hash"] = result.content_hash
    updated["semantic_contract_mode"] = mode
    updated["semantic_diagnostics"] = [
        dict(item) for item in result.diagnostics[:MAX_SEMANTIC_DIAGNOSTICS]
    ]
    return updated


def semantic_result_for_stored_html(
    html_content: str | None,
    settings: object,
) -> SemanticDocumentResult:
    """Verify and describe a persisted current-contract document.

    Current semantic rows are identity snapshots: readers and exporters must
    preserve their bytes rather than reparse/re-serialize them. That promise is
    only meaningful when the stored digest is checked before those bytes cross
    an add-on or export boundary.
    """

    if not is_current_semantic_document(settings):
        raise SemanticDocumentError(
            "The document does not use the current completed semantic contract."
        )
    assert isinstance(settings, dict)
    raw_html = str(html_content or "")
    expected_hash = str(settings.get("semantic_content_hash") or "").strip().casefold()
    if not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
        raise SemanticDocumentError(
            "The stored semantic document is missing a valid content hash."
        )
    actual_hash = hashlib.sha256(raw_html.encode("utf-8")).hexdigest()
    if not hmac.compare_digest(actual_hash, expected_hash):
        raise SemanticDocumentError(
            "The stored semantic document no longer matches its content hash."
        )

    diagnostics: list[dict[str, str]] = []
    raw_diagnostics = settings.get("semantic_diagnostics")
    if isinstance(raw_diagnostics, list):
        for item in raw_diagnostics[:MAX_SEMANTIC_DIAGNOSTICS]:
            if not isinstance(item, dict):
                continue
            level = str(item.get("level") or "").strip().casefold()
            code = str(item.get("code") or "").strip().casefold()
            if level in {"warning", "error"} and re.fullmatch(r"[a-z0-9_]{1,64}", code):
                diagnostic = {"level": level, "code": code}
                if diagnostic not in diagnostics:
                    diagnostics.append(diagnostic)
    return SemanticDocumentResult(
        html=raw_html,
        contract_version=SEMANTIC_HTML_CONTRACT_VERSION,
        content_hash=actual_hash,
        diagnostics=tuple(diagnostics),
    )


def is_current_semantic_document(settings: object) -> bool:
    return (
        isinstance(settings, dict)
        and settings.get("semantic_contract_version") == SEMANTIC_HTML_CONTRACT_VERSION
        and settings.get("semantic_contract_mode", "completed") == "completed"
    )
